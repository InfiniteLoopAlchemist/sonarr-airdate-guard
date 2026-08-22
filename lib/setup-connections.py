#!/usr/bin/env python3
"""
setup-connections.py

Creates or updates the "Air Date Guard" Custom Script connection on every
configured Sonarr instance, so the hook is actually wired up.

This is the step that would otherwise be a handful of clicks per instance in
each Sonarr UI. Leaving it manual is not a cosmetic problem: until the On Grab
hook is live, nothing stops Sonarr re-grabbing a fake it just had deleted, so
the poller ends up cleaning up the same episodes hour after hour.

    python3 lib/setup-connections.py              # all instances
    python3 lib/setup-connections.py -i anime     # one instance
    python3 lib/setup-connections.py --dry-run
    python3 lib/setup-connections.py --remove     # tear the connections down

Requires only the Python 3 standard library.
"""

import argparse
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sonarr_guard_common import (  # noqa: E402
    ConfigError,
    api_request,
    init_log,
    load_instances,
    log,
)

CONNECTION_NAME = "Air Date Guard"
HOOK_PATH = "/guard/sonarr-grab-hook.sh"

# Only these fire. Everything else the schema offers stays off.
WANTED_EVENTS = {
    "onGrab": True,
    "onDownload": True,   # Sonarr's name for the import event
    "onUpgrade": True,    # so an "upgrade" to a fake is caught too
}


def build_payload(schema, existing=None):
    payload = dict(existing) if existing else dict(schema)

    for key in list(payload):
        if key.startswith("on") and isinstance(payload[key], bool):
            payload[key] = False
    for key, value in WANTED_EVENTS.items():
        if schema.get("supports" + key[0].upper() + key[1:], False):
            payload[key] = value

    payload["name"] = CONNECTION_NAME
    payload["implementation"] = schema["implementation"]
    payload["implementationName"] = schema["implementationName"]
    payload["configContract"] = schema["configContract"]
    payload["includeHealthWarnings"] = False
    payload.setdefault("tags", [])

    # Sonarr 4.0.19 rejects any non-empty "arguments" value outright:
    #   "Arguments are no longer supported for custom scripts"
    # so the field is dropped entirely and the hook works out which instance
    # it is from its own config.xml.
    fields = []
    for field in schema.get("fields", []):
        if field["name"] == "arguments":
            continue
        entry = {"name": field["name"]}
        entry["value"] = HOOK_PATH if field["name"] == "path" else field.get("value", "")
        fields.append(entry)
    payload["fields"] = fields

    payload.pop("presets", None)
    payload.pop("message", None)
    return payload


def process(instance, args):
    try:
        existing_all = api_request(instance, "/api/v3/notification") or []
    except urllib.error.HTTPError as e:
        log(f"ERROR: HTTP {e.code} listing connections", instance)
        return 1
    except (urllib.error.URLError, OSError) as e:
        log(f"ERROR: cannot reach {instance.url}: {e}", instance)
        return 1

    mine = next((n for n in existing_all if n.get("name") == CONNECTION_NAME), None)

    if args.remove:
        if mine is None:
            log("No Air Date Guard connection to remove", instance)
            return 0
        if args.dry_run:
            log(f"Would delete connection id {mine['id']}", instance)
            return 0
        try:
            api_request(instance, f"/api/v3/notification/{mine['id']}", method="DELETE")
            log(f"Removed connection id {mine['id']}", instance)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            log(f"ERROR: could not remove connection: {e}", instance)
            return 1
        return 0

    try:
        schemas = api_request(instance, "/api/v3/notification/schema") or []
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
        log(f"ERROR: could not read the notification schema: {e}", instance)
        return 1

    schema = next((s for s in schemas if s.get("implementation") == "CustomScript"), None)
    if schema is None:
        log("ERROR: this Sonarr has no CustomScript notification type", instance)
        return 1

    payload = build_payload(schema, mine)

    if mine is not None:
        current = {f["name"]: f["value"] for f in mine.get("fields", []) if f.get("value")}
        wanted = {f["name"]: f["value"] for f in payload["fields"] if f.get("value")}
        events_ok = all(mine.get(k) == v for k, v in WANTED_EVENTS.items()
                        if schema.get("supports" + k[0].upper() + k[1:], False))
        if current == wanted and events_ok:
            log(f"Connection already correct (id {mine['id']})", instance)
            return 0

    verb = "update" if mine else "create"
    if args.dry_run:
        log(f"Would {verb} the connection", instance)
        return 0

    try:
        if mine:
            result = api_request(
                instance, f"/api/v3/notification/{mine['id']}",
                method="PUT", payload=payload,
            )
        else:
            result = api_request(
                instance, "/api/v3/notification",
                method="POST", payload=payload,
            )
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = " " + e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        log(f"ERROR: could not {verb} the connection: HTTP {e.code}{detail}", instance)
        return 1
    except (urllib.error.URLError, OSError) as e:
        log(f"ERROR: could not {verb} the connection: {e}", instance)
        return 1

    new_id = (result or {}).get("id", "?")
    log(f"Connection {verb}d (id {new_id}) -> {HOOK_PATH}, On Grab + On Import", instance)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Wire the Air Date Guard Custom Script into every Sonarr instance."
    )
    parser.add_argument("-i", "--instance", action="append", default=[])
    parser.add_argument("--config", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove", action="store_true",
                        help="Delete the connection instead of creating it.")
    args = parser.parse_args()

    init_log("connect")

    try:
        instances, path = load_instances(args.config)
    except ConfigError as e:
        log(f"ERROR: {e}")
        return 2

    if args.instance:
        wanted = {n.strip().lower() for n in args.instance}
        missing = wanted - {n.lower() for n in instances}
        if missing:
            log(f"ERROR: no such instance(s): {', '.join(sorted(missing))}")
            return 2
        selected = [i for n, i in instances.items() if n.lower() in wanted]
    else:
        selected = [i for i in instances.values() if i.enabled]

    log(f"Config {path} | {len(selected)} instance(s)" + (" | DRY RUN" if args.dry_run else ""))

    failures = sum(process(i, args) for i in selected)
    if failures:
        log(f"Finished with {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
