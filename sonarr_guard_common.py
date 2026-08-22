#!/usr/bin/env python3
"""
sonarr_guard_common.py

Shared config loading, logging, and Sonarr API access for the air-date guard.
Runs on the Docker host (Python 3.10+), not inside the containers.
Not meant to be run directly, except as a config check:

    python3 sonarr_guard_common.py

Requires only the Python 3 standard library.
"""

import configparser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_GRACE_DAYS = 3

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATHS = [
    os.environ.get("SONARR_GUARD_CONFIG", ""),
    os.path.join(_SCRIPT_DIR, "sonarr-guard.conf"),
    "/etc/sonarr-guard.conf",
    os.path.expanduser("~/.sonarr-guard.conf"),
]

LOG_CANDIDATES = [
    os.environ.get("SONARR_GUARD_LOG", ""),
    os.path.join(_SCRIPT_DIR, "sonarr-guard.log"),
    os.path.expanduser("~/sonarr-guard.log"),
    "/tmp/sonarr-guard.log",
]

# Sonarr history eventType values.
HISTORY_GRABBED = 1
HISTORY_IMPORTED = 3

_log_handle = None
_log_prefix = ""

_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)


class ConfigError(Exception):
    pass


class Instance:
    """One Sonarr server."""

    def __init__(self, name, url, api_key, grace_days, mode, enabled,
                 delete_early_files, delete_scope, delete_blocklist,
                 early_import_margin_hours, future_window_days):
        self.name = name
        self.url = url.rstrip("/")
        self.api_key = api_key
        self.grace_days = grace_days
        self.mode = mode
        self.enabled = enabled
        self.delete_early_files = delete_early_files
        self.delete_scope = delete_scope
        self.delete_blocklist = delete_blocklist
        self.early_import_margin_hours = early_import_margin_hours
        self.future_window_days = future_window_days

    def __repr__(self):
        return f"<Instance {self.name} {self.url} grace={self.grace_days}d mode={self.mode}>"


def init_log(prefix=""):
    global _log_handle, _log_prefix
    _log_prefix = prefix
    for path in LOG_CANDIDATES:
        if not path:
            continue
        try:
            _log_handle = open(path, "a", encoding="utf-8")
            return path
        except OSError:
            continue
    return None


def log(msg, instance=None):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tags = ""
    if _log_prefix:
        tags += f"[{_log_prefix}]"
    if instance is not None:
        name = instance.name if isinstance(instance, Instance) else str(instance)
        tags += f"[{name}]"
    line = f"[{stamp}]{tags} {msg}" if tags else f"[{stamp}] {msg}"

    print(line, flush=True)
    if _log_handle:
        try:
            _log_handle.write(line + "\n")
            _log_handle.flush()
        except OSError:
            pass


def _truthy(value, default=True):
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "enabled")


def _number(section, key, default, name, cast=float):
    raw = section.get(key, str(default))
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"[{name}] has invalid {key} '{raw}'")


def load_instances(config_path=None):
    """Read the multi-instance config. Returns (dict of name -> Instance, path)."""
    paths = [config_path] if config_path else CONFIG_PATHS

    parser = configparser.ConfigParser()
    used = None
    for path in paths:
        if not path:
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                parser.read_file(fh)
            used = path
            break
        except OSError:
            continue
        except configparser.Error as e:
            raise ConfigError(f"Malformed config at {path}: {e}") from e

    if used is None:
        raise ConfigError(
            "No config file found. Looked in: " + ", ".join(p for p in paths if p)
        )

    instances = {}
    for name in parser.sections():
        section = parser[name]
        api_key = section.get("api_key", "").strip()
        url = section.get("url", "").strip()

        if not url:
            raise ConfigError(f"[{name}] is missing 'url'")
        if not api_key:
            raise ConfigError(f"[{name}] is missing 'api_key'")

        mode = section.get("mode", "strict").strip().lower()
        if mode not in ("strict", "lenient"):
            raise ConfigError(f"[{name}] has invalid mode '{mode}' (use strict or lenient)")

        scope = section.get("delete_scope", "unaired").strip().lower()
        if scope not in ("unaired", "imported_early"):
            raise ConfigError(
                f"[{name}] has invalid delete_scope '{scope}' "
                "(use unaired or imported_early)"
            )

        instances[name] = Instance(
            name=name,
            url=url,
            api_key=api_key,
            grace_days=_number(section, "grace_days", DEFAULT_GRACE_DAYS, name),
            mode=mode,
            enabled=_truthy(section.get("enabled"), True),
            delete_early_files=_truthy(section.get("delete_early_files"), True),
            delete_scope=scope,
            delete_blocklist=_truthy(section.get("delete_blocklist"), True),
            early_import_margin_hours=_number(section, "early_import_margin_hours", 24, name),
            future_window_days=_number(section, "future_window_days", 400, name, int),
        )

    if not instances:
        raise ConfigError(f"{used} has no instance sections defined")

    return instances, used


def api_request(instance, path, method="GET", params=None, payload=None, timeout=20):
    url = instance.url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    body = None
    req = urllib.request.Request(url, method=method)
    req.add_header("X-Api-Key", instance.api_key)
    req.add_header("Accept", "application/json")
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.data = body

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None


def _paged(instance, path, params=None, page_size=200):
    """Walk a paginated Sonarr endpoint and return every record."""
    records = []
    page = 1
    while True:
        query = dict(params or {})
        query.update({"page": page, "pageSize": page_size})
        data = api_request(instance, path, params=query)
        if not data:
            break
        batch = data.get("records", [])
        records.extend(batch)
        total = data.get("totalRecords", len(records))
        if len(records) >= total or not batch:
            break
        page += 1
    return records


def get_queue(instance, page_size=200):
    """Full queue for one instance, following pagination."""
    return _paged(
        instance,
        "/api/v3/queue",
        {"includeEpisode": "true", "includeSeries": "true"},
        page_size,
    )


def get_calendar(instance, start, end, chunk_days=90):
    """Calendar episodes between two datetimes, with series and file attached.

    Sonarr gets unhappy with very wide ranges, so walk it in chunks.
    """
    records = []
    cursor = start
    while cursor < end:
        stop = min(cursor + timedelta(days=chunk_days), end)
        batch = api_request(
            instance,
            "/api/v3/calendar",
            params={
                "start": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end": stop.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "unmonitored": "true",
                "includeSeries": "true",
                "includeEpisodeFile": "true",
            },
            timeout=60,
        )
        if batch:
            records.extend(batch)
        cursor = stop
    return records


def get_series(instance):
    data = api_request(instance, "/api/v3/series", timeout=60)
    return data or []


def get_episodes_with_files(instance, series_id):
    """Every episode of one series, with its episodeFile object attached."""
    data = api_request(
        instance,
        "/api/v3/episode",
        params={"seriesId": series_id, "includeEpisodeFile": "true"},
        timeout=60,
    )
    return data or []


def parse_air_date(raw):
    """Sonarr returns ISO8601 UTC, sometimes with a trailing Z, sometimes with
    fractional seconds of arbitrary length. Python 3.10's fromisoformat handles
    neither, so parse it by hand."""
    if not raw:
        return None
    match = _ISO_RE.match(str(raw).strip())
    if not match:
        return None

    year, month, day, hour, minute, second, frac, offset = match.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])

    try:
        dt = datetime(
            int(year), int(month), int(day),
            int(hour), int(minute), int(second), micro,
        )
    except ValueError:
        return None

    if offset in (None, "", "Z"):
        return dt.replace(tzinfo=timezone.utc)

    sign = 1 if offset[0] == "+" else -1
    digits = offset[1:].replace(":", "")
    delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    return (dt - sign * delta).replace(tzinfo=timezone.utc)


def parse_air_date_list(raw):
    """Grab events pass comma-separated air dates for multi-episode releases.
    Some builds use a pipe separator, so handle both."""
    if not raw:
        return []
    parsed = []
    for chunk in str(raw).replace("|", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        dt = parse_air_date(chunk)
        if dt is None:
            log(f"WARN: could not parse air date '{chunk}'")
            continue
        parsed.append(dt)
    return parsed


def remove_from_queue(instance, queue_id, blocklist=True, remove_from_client=True):
    """Sonarr v4 uses 'blocklist'; older builds used 'blacklist'. Try the modern
    spelling first and fall back."""
    base_params = {
        "removeFromClient": "true" if remove_from_client else "false",
        "skipRedownload": "false",
    }

    for key in ("blocklist", "blacklist"):
        params = dict(base_params)
        params[key] = "true" if blocklist else "false"
        try:
            api_request(instance, f"/api/v3/queue/{queue_id}", method="DELETE", params=params)
            return True, None
        except urllib.error.HTTPError as e:
            if e.code == 400 and key == "blocklist":
                continue
            return False, f"HTTP {e.code}"
        except (urllib.error.URLError, OSError) as e:
            return False, str(e)
    return False, "server rejected both blocklist and blacklist parameters"


def delete_episode_file(instance, file_id):
    """Delete one episode file from disk via Sonarr, so its history and stats stay
    consistent. Returns (ok, error)."""
    try:
        api_request(instance, f"/api/v3/episodefile/{file_id}", method="DELETE", timeout=60)
        return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, str(e)


def blocklist_episode_grab(instance, episode_id):
    """Find the grab that produced this episode's file and mark it failed, which
    is the only way to get a release onto Sonarr's blocklist. Returns (ok, detail)."""
    try:
        records = _paged(
            instance,
            "/api/v3/history",
            {"episodeId": episode_id, "sortKey": "date", "sortDirection": "descending"},
            page_size=50,
        )
    except urllib.error.HTTPError as e:
        return False, f"history lookup HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"history lookup failed: {e}"

    grab = next((r for r in records if r.get("eventType") in (HISTORY_GRABBED, "grabbed")), None)
    if grab is None:
        return False, "no grab found in history"

    try:
        api_request(instance, f"/api/v3/history/failed/{grab['id']}", method="POST", timeout=30)
        return True, grab.get("sourceTitle", "")
    except urllib.error.HTTPError as e:
        return False, f"mark-failed HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return False, f"mark-failed failed: {e}"


def resolve_instance(instances, requested, source_hint=""):
    """Pick an instance by name, case-insensitively. If none requested and only
    one is configured, use it."""
    if requested:
        for name, inst in instances.items():
            if name.lower() == requested.strip().lower():
                return inst
        raise ConfigError(
            f"No instance named '{requested}' in config{source_hint}. "
            f"Configured: {', '.join(sorted(instances))}"
        )

    enabled = [i for i in instances.values() if i.enabled]
    if len(enabled) == 1:
        return enabled[0]

    raise ConfigError(
        "No instance specified and more than one is configured. "
        f"Pass one of: {', '.join(sorted(instances))}"
    )


if __name__ == "__main__":
    init_log("check")
    try:
        instances, path = load_instances()
    except ConfigError as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    log(f"Config OK: {path}")
    bad = 0
    for name in sorted(instances):
        inst = instances[name]
        state = "enabled" if inst.enabled else "DISABLED"
        delete = f"{inst.delete_scope}" if inst.delete_early_files else "off"
        log(f"  {name}: {inst.url} grace={inst.grace_days}d mode={inst.mode} "
            f"delete={delete} [{state}]")
        if not inst.enabled:
            continue
        try:
            status = api_request(inst, "/api/v3/system/status", timeout=10)
            version = (status or {}).get("version", "unknown")
            log(f"    reachable, Sonarr {version}")
        except urllib.error.HTTPError as e:
            bad += 1
            if e.code == 401:
                log("    UNREACHABLE: 401, api_key is wrong")
            else:
                log(f"    UNREACHABLE: HTTP {e.code}")
        except (urllib.error.URLError, OSError) as e:
            bad += 1
            log(f"    UNREACHABLE: {e}")

    sys.exit(1 if bad else 0)
