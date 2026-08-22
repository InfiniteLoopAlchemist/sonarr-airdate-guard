#!/usr/bin/env python3
"""
sonarr-v5-watch.py

Tells you when Sonarr v5 actually ships. Nothing to do with the air date guard,
it just lives next to it.

Sonarr does not announce release dates, so the only reliable signal is an
artefact appearing. This watches three places and fires once per stage:

    PRERELEASE  a v5 tag or prerelease on GitHub, or a linuxserver develop-5.x
                image. This is the point where you could try it if you wanted.
    STABLE      a non-prerelease v5 GitHub release, or linuxserver publishing
                5.x on its stable channel. This is the one that matters.

Each stage notifies exactly once, tracked in the state file. Safe to run as
often as you like.

    sonarr-v5-watch.py              normal run, notifies on a new stage
    sonarr-v5-watch.py --status     print what it can see right now
    sonarr-v5-watch.py --test       send a test notification and exit
    sonarr-v5-watch.py --reset      forget what has been notified

Standard library only.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

# --- config ---------------------------------------------------------------

# ntfy.sh needs no account, and the topic name is the only thing keeping it
# private, so generate a random one rather than picking something guessable:
#
#     python3 -c "import secrets,string; a=string.ascii_lowercase+string.digits; print('sonarr-v5-'+''.join(secrets.choice(a) for _ in range(16)))"
#
# Set it here or via SONARR_V5_NTFY_TOPIC, then subscribe at
# https://ntfy.sh/<topic> in a browser or the phone app.
NTFY_TOPIC = os.environ.get("SONARR_V5_NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("SONARR_V5_NTFY_SERVER", "https://ntfy.sh")

# Optional best effort second channel. Point this at a notifiarr.conf and it
# will try the passthrough API. Silently skipped unless you have enabled the
# passthrough integration on notifiarr.com, at which point it starts working on
# its own with no further changes here.
NOTIFIARR_CONF = os.environ.get("SONARR_V5_NOTIFIARR_CONF", "")

STATE_PATH = os.environ.get(
    "SONARR_V5_STATE",
    os.path.expanduser("~/.local/state/sonarr-v5-watch.json"),
)
LOG_PATH = os.environ.get(
    "SONARR_V5_LOG",
    os.path.expanduser("~/.local/state/sonarr-v5-watch.log"),
)

TIMEOUT = 25
UA = "sonarr-v5-watch (+https://github.com/InfiniteLoopAlchemist/sonarr-airdate-guard)"

V5_TAG = re.compile(r"^v?5\.")
V5_DOCKER = re.compile(r"^(?:amd64-|arm64v8-)?(?:develop-)?(?:version-)?v?5\.")
V5_DOCKER_DEVELOP = re.compile(r"develop", re.I)


def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read())


# --- the three signals ----------------------------------------------------

def check_github_releases():
    """Returns (stage, detail) or (None, note)."""
    try:
        releases = get_json("https://api.github.com/repos/Sonarr/Sonarr/releases?per_page=100")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return None, f"github releases unreachable: {e}"

    v5 = [r for r in releases if V5_TAG.match(r.get("tag_name", ""))]
    if not v5:
        return None, "no v5 releases"

    stable = [r for r in v5 if not r.get("prerelease")]
    if stable:
        r = stable[0]
        return "STABLE", f"GitHub release {r['tag_name']} ({r.get('published_at','')[:10]}) {r.get('html_url','')}"
    r = v5[0]
    return "PRERELEASE", f"GitHub prerelease {r['tag_name']} ({r.get('published_at','')[:10]}) {r.get('html_url','')}"


def check_github_tags():
    try:
        tags = get_json("https://api.github.com/repos/Sonarr/Sonarr/tags?per_page=100")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return None, f"github tags unreachable: {e}"

    v5 = [t["name"] for t in tags if V5_TAG.match(t.get("name", ""))]
    if not v5:
        return None, "no v5 tags"
    return "PRERELEASE", f"GitHub tag {v5[0]}"


def check_docker():
    """linuxserver publishes long before most people notice a GitHub tag."""
    url = "https://hub.docker.com/v2/repositories/linuxserver/sonarr/tags?page_size=100"
    try:
        data = get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
        return None, f"docker hub unreachable: {e}"

    names = [t.get("name", "") for t in data.get("results", [])]
    v5 = [n for n in names if V5_DOCKER.match(n)]
    if not v5:
        # 'latest' pointing at a 5.x build is the other stable signal.
        return None, "no v5 image tags"

    stable = [n for n in v5 if not V5_DOCKER_DEVELOP.search(n)]
    if stable:
        return "STABLE", f"linuxserver/sonarr:{sorted(stable)[0]}"
    return "PRERELEASE", f"linuxserver/sonarr:{sorted(v5)[0]}"


CHECKS = (
    ("github-releases", check_github_releases),
    ("github-tags", check_github_tags),
    ("docker-hub", check_docker),
)


# --- notifying ------------------------------------------------------------

def notify_ntfy(title, body, priority="default", tags="satellite"):
    if not NTFY_TOPIC:
        return False, "no topic configured"
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "User-Agent": UA,
            "Title": title,
            "Priority": priority,
            "Tags": tags,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return False, str(e)


def notify_notifiarr(title, body):
    """Only works if the passthrough integration is enabled on notifiarr.com.
    Failure is expected and ignored."""
    try:
        with open(NOTIFIARR_CONF, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("api_key"):
                    key = line.split("=", 1)[1].strip().strip('"')
                    break
            else:
                return False, "no key"
    except OSError:
        return False, "no conf"

    payload = {
        "notification": {"update": False, "name": "Sonarr v5 Watch", "event": "0"},
        "discord": {"color": "5865F2", "text": {"title": title, "description": body}},
    }
    req = urllib.request.Request(
        "https://notifiarr.com/api/v1/notification/passthrough",
        data=json.dumps(payload).encode("utf-8"),
        headers={"X-API-Key": key, "Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            resp.read()
        return True, None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        return False, str(e)


def announce(title, body, priority):
    ok, err = notify_ntfy(title, body, priority=priority, tags="tada")
    log(f"  ntfy: {'sent' if ok else 'FAILED ' + str(err)}")
    ok2, err2 = notify_notifiarr(title, body)
    if ok2:
        log("  notifiarr: sent")
    return ok or ok2


# --- state ----------------------------------------------------------------

def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            return d
    except (OSError, ValueError):
        pass
    return {"notified": []}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except OSError as e:
        log(f"WARN: could not write state: {e}")


# --- main -----------------------------------------------------------------

def gather():
    findings = {}
    notes = []
    for name, fn in CHECKS:
        stage, detail = fn()
        if stage:
            findings.setdefault(stage, []).append(f"{name}: {detail}")
        else:
            notes.append(f"{name}: {detail}")
    return findings, notes


def main():
    ap = argparse.ArgumentParser(description="Notify when Sonarr v5 ships.")
    ap.add_argument("--status", action="store_true", help="Report what is visible and exit.")
    ap.add_argument("--test", action="store_true", help="Send a test notification and exit.")
    ap.add_argument("--reset", action="store_true", help="Forget which stages were notified.")
    ap.add_argument("--quiet", action="store_true", help="Only log when something changes.")
    args = ap.parse_args()

    if args.test:
        ok = announce(
            "Sonarr v5 watcher",
            "Test notification. You will only hear from this again when Sonarr v5 actually ships.",
            "default",
        )
        return 0 if ok else 1

    if args.reset:
        save_state({"notified": []})
        log("State reset, both stages can fire again.")
        return 0

    state = load_state()
    already = set(state.get("notified", []))
    findings, notes = gather()

    if args.status or not args.quiet:
        log(f"Checked. Seen: {findings or 'nothing v5 yet'}")
        for n in notes:
            log(f"  {n}")
        if already:
            log(f"  already notified: {', '.join(sorted(already))}")

    if args.status:
        return 0

    # STABLE supersedes PRERELEASE, so report the strongest new stage only.
    for stage, priority, headline in (
        ("STABLE", "high", "Sonarr v5 is out"),
        ("PRERELEASE", "default", "Sonarr v5 has a prerelease"),
    ):
        if stage in findings and stage not in already:
            body = "\n".join(findings[stage])
            body += (
                "\n\nYour air date guard still handles the parts v5 does not: "
                "deleting early imports and unmonitoring unaired episodes. "
                "The native release profile can take over the grab side."
            )
            log(f"{stage} detected")
            for f in findings[stage]:
                log(f"  {f}")
            if announce(headline, body, priority):
                already.add(stage)
                if stage == "STABLE":
                    already.add("PRERELEASE")
                state["notified"] = sorted(already)
                state["last_hit"] = datetime.now().isoformat(timespec="seconds")
                save_state(state)
            return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
