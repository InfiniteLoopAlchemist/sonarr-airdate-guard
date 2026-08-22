#!/usr/bin/env python3
"""Watcher for the Sonarr v5 release.

Sonarr publishes no release date, so this watches for the artefact instead
and, ONLY when v5 actually appears, creates an Apple Reminder (the ping)
and an Apple Note (the detail). Silent while there is still no v5. First
run seeds state silently.

Installed as a launchd agent (see install-sonarr-v5-watch.sh); state lives
at ~/Library/Application Support/sonarr-v5-watch/.

Why v5 matters: it adds AirDateSpecification, driven by the release-profile
fields AirDateRestriction / AirDateGracePeriod /
AllowSeasonPackWithoutAllEpisodesAired. That takes over the grab side of
sonarr-airdate-guard. The cleanup and monitoring sides stay ours, Sonarr
declined those in #8896.

Signals watched:
  - GitHub releases for a v5 tag ......... the official cut
  - GitHub tags for v5 ................... often lands before the release
  - linuxserver/sonarr image tags ........ what the containers actually pull
"""

import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

STATE_DIR = Path.home() / "Library/Application Support/sonarr-v5-watch"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "watch.log"

UA = {"User-Agent": "sonarr-v5-watch/1.0"}

IS_V5 = re.compile(r"^v?5\.")
IS_V5_IMAGE = re.compile(r"^(?:amd64-|arm64v8-)?(?:develop-)?(?:version-)?v?5\.")


def log(msg: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"[{stamp}] {msg}\n")


def fetch_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def gh_v5_release() -> str:
    """Newest v5 release tag, marked when it is still a prerelease."""
    for rel in fetch_json("https://api.github.com/repos/Sonarr/Sonarr/releases?per_page=100"):
        tag = rel.get("tag_name", "")
        if IS_V5.match(tag):
            return f"{tag} (prerelease)" if rel.get("prerelease") else tag
    return ""


def gh_v5_tag() -> str:
    for tag in fetch_json("https://api.github.com/repos/Sonarr/Sonarr/tags?per_page=100"):
        if IS_V5.match(tag.get("name", "")):
            return tag["name"]
    return ""


def lsio_v5_image() -> str:
    data = fetch_json("https://hub.docker.com/v2/repositories/linuxserver/sonarr/tags?page_size=100")
    hits = sorted(t["name"] for t in data.get("results", []) if IS_V5_IMAGE.match(t.get("name", "")))
    return hits[0] if hits else ""


def gather() -> dict:
    return {
        "gh_release": gh_v5_release(),
        "gh_tag": gh_v5_tag(),
        "lsio_image": lsio_v5_image(),
    }


# Human wording per changed key: (headline, what to actually do about it).
MEANINGS = {
    "gh_release": (
        "Sonarr v5 released on GitHub",
        "the grab side can move to a release profile, set AirDateRestriction and "
        "AirDateGracePeriod, then turn off the guard's queue pass with --queue-only "
        "removed from cron. Keep the guard for cleanup and unmonitoring, Sonarr "
        "declined those in #8896",
    ),
    "gh_tag": (
        "Sonarr v5 tagged on GitHub",
        "a build is being cut, the release usually follows within days. Nothing to "
        "do yet",
    ),
    "lsio_image": (
        "linuxserver published a Sonarr v5 image",
        "this is the one that matters for the four containers on the Pop OS box. "
        "Pin a version tag before pulling, do not let watchtower jump a major "
        "version unattended",
    ),
}


def osascript(script: str) -> None:
    subprocess.run(["/usr/bin/osascript", "-e", script], check=True, capture_output=True)


def notify(changes: list[tuple[str, str, str]], test: bool = False) -> None:
    """changes: list of (key, old, new)."""
    date = datetime.now().strftime("%Y-%m-%d")
    lines = []
    for key, old, new in changes:
        head, meaning = MEANINGS[key]
        lines.append(f"{head}: {old or 'none'} -> {new}. {meaning}.")
    body = "\n\n".join(lines)

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    title = "Sonarr v5 is out"
    if test:
        title = "Sonarr v5 watch SELF-TEST, not a real release"
        date = f"{date} SELF-TEST"
    osascript(
        f'tell application "Reminders" to make new reminder with properties '
        f'{{name:"{esc(title)}, see Notes", due date:(current date) + 8 * hours}}'
    )
    note_body = esc(body).replace("\n", "<br>")
    osascript(
        f'tell application "Notes" to make new note at folder "Notes" of account "iCloud" '
        f'with properties {{name:"Sonarr v5 watch {date}", body:"<b>{esc(title)}</b><br><br>{note_body}"}}'
    )


def main() -> int:
    force_test = "--test" in sys.argv
    try:
        current = gather()
    except Exception as e:
        log(f"fetch failed: {e}")
        return 1

    if force_test:
        notify([("gh_release", "self-test", "self-test")], test=True)
        log("test notification sent")
        return 0

    if "--status" in sys.argv:
        print(json.dumps(current, indent=2))
        return 0

    if not STATE_FILE.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(current, indent=2))
        log(f"seeded baseline: {current}")
        return 0

    previous = json.loads(STATE_FILE.read_text())
    # Keys not yet in the state file are newly watched, not upstream
    # changes; seed them silently instead of alerting "none -> X".
    new_keys = [k for k in current if k not in previous]
    changes = [(k, previous[k], v) for k, v in current.items() if k in previous and previous[k] != v]

    if new_keys and not changes:
        STATE_FILE.write_text(json.dumps(current, indent=2))
        log(f"seeded new keys: {new_keys}")

    if changes:
        try:
            notify(changes)
            log(f"notified: {[c[0] for c in changes]}")
        except Exception as e:
            log(f"notify failed, keeping old state so it retries next week: {e}")
            return 1
        STATE_FILE.write_text(json.dumps(current, indent=2))
    elif not new_keys:
        log("no changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
