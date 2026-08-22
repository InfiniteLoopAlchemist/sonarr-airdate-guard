#!/usr/bin/env python3
"""
sonarr-airdate-guard.py

Runs on the Docker host, against every Sonarr instance in sonarr-guard.conf.

Two passes per instance:

  1. QUEUE   remove and blocklist downloads for episodes that have not aired
             yet, plus a grace period. Backstop for the On Grab hook, which
             catches the same thing sooner but can miss it (Sonarr restarting,
             a download that takes a while to register in the queue).

  2. FILES   delete episode files that are already on disk but should never
             have been imported. Scope comes from the config:
               unaired        the episode has not aired yet, so the file cannot
                              be real. One calendar call per instance.
               imported_early also files whose import predates the air time by
                              more than early_import_margin_hours, even if the
                              episode has since aired. One call per series.

Grace does not apply to the file pass. A file for an episode that aired two
hours ago may well be legitimate; a file for an episode that has not aired
cannot be. The delete pass only touches the unambiguous cases.

Usage:
    python3 sonarr-airdate-guard.py --dry-run            # everything, no changes
    python3 sonarr-airdate-guard.py                      # everything, live
    python3 sonarr-airdate-guard.py -i anime -i main     # only these two
    python3 sonarr-airdate-guard.py --scope imported_early --dry-run
    python3 sonarr-airdate-guard.py --queue-only         # skip the file pass
    python3 sonarr-airdate-guard.py --files-only         # skip the queue pass

Requires only the Python 3 standard library.
"""

import argparse
import os
import sys
import urllib.error
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sonarr_guard_common import (  # noqa: E402
    ConfigError,
    blocklist_episode_grab,
    delete_episode_file,
    get_calendar,
    get_episodes_with_files,
    get_queue,
    get_series,
    init_log,
    load_instances,
    log,
    parse_air_date,
    remove_from_queue,
)


class Tally:
    def __init__(self):
        self.checked = 0
        self.flagged = 0
        self.removed = 0
        self.files_flagged = 0
        self.files_deleted = 0
        self.bytes_freed = 0
        self.failed = 0

    def merge(self, other):
        for field in vars(self):
            setattr(self, field, getattr(self, field) + getattr(other, field))


def human_size(num):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024 or unit == "TB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0


def episode_label(episode):
    s = episode.get("seasonNumber")
    e = episode.get("episodeNumber")
    if isinstance(s, int) and isinstance(e, int):
        return f"S{s:02d}E{e:02d}"
    return "S??E??"


# --------------------------------------------------------------------------
# pass 1: the queue
# --------------------------------------------------------------------------

def guard_queue(instance, args, now, excluded, tally):
    try:
        queue = get_queue(instance)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            log("ERROR: 401 unauthorized, api_key is wrong", instance)
        else:
            log(f"ERROR: HTTP {e.code} fetching queue", instance)
        tally.failed += 1
        return
    except (urllib.error.URLError, OSError) as e:
        log(f"ERROR: cannot reach {instance.url}: {e}", instance)
        tally.failed += 1
        return

    if not queue:
        log("Queue empty", instance)
        return

    grace = timedelta(
        days=args.grace_days if args.grace_days is not None else instance.grace_days
    )
    log(f"Queue: {len(queue)} item(s), grace {grace.days}d, mode {instance.mode}", instance)

    for item in queue:
        title = item.get("title", "<untitled>")
        series = item.get("series") or {}
        series_title = series.get("title", "Unknown Series")

        if series_title.lower() in excluded:
            continue

        episode = item.get("episode")
        if not episode:
            log(f"SKIP  {series_title} | no single episode attached (season pack?) | {title}",
                instance)
            continue

        air_utc = parse_air_date(episode.get("airDateUtc"))
        if air_utc is None:
            log(f"SKIP  {series_title} | no air date in metadata | {title}", instance)
            continue

        tally.checked += 1
        threshold = air_utc + grace
        if now >= threshold:
            continue

        tally.flagged += 1
        early_by = threshold - now
        log(
            f"EARLY {series_title} {episode_label(episode)} | air date {air_utc:%Y-%m-%d} | "
            f"clears in {early_by.days}d {early_by.seconds // 3600}h | {title}",
            instance,
        )

        if args.dry_run:
            continue

        ok, err = remove_from_queue(
            instance,
            item.get("id"),
            blocklist=not args.no_blocklist,
            remove_from_client=not args.keep_files,
        )
        if ok:
            tally.removed += 1
            log(f"  -> removed from queue (blocklist={not args.no_blocklist})", instance)
        else:
            tally.failed += 1
            log(f"  -> removal failed: {err}", instance)


# --------------------------------------------------------------------------
# pass 2: files already on disk
# --------------------------------------------------------------------------

def collect_unaired(instance, now, excluded):
    """Cheap path. One calendar walk covering every series at once."""
    window = timedelta(days=instance.future_window_days)
    records = get_calendar(instance, now, now + window)
    found = []

    for episode in records:
        if not episode.get("hasFile"):
            continue
        series_title = (episode.get("series") or {}).get("title", "Unknown Series")
        if series_title.lower() in excluded:
            continue
        air_utc = parse_air_date(episode.get("airDateUtc"))
        if air_utc is None or now >= air_utc:
            continue
        found.append((series_title, episode, air_utc, "has not aired yet"))

    return found


def collect_imported_early(instance, now, excluded, margin):
    """Deep path. One call per series, so this is the manual or nightly sweep."""
    series_list = get_series(instance)
    log(f"Files: deep sweep over {len(series_list)} series, "
        f"import margin {margin.total_seconds() / 3600:.0f}h", instance)

    found = []
    for index, series in enumerate(series_list, 1):
        series_title = series.get("title", "Unknown Series")
        if series_title.lower() in excluded:
            continue
        if index % 50 == 0:
            log(f"  ... {index}/{len(series_list)} series scanned", instance)

        try:
            episodes = get_episodes_with_files(instance, series.get("id"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            log(f"WARN  {series_title} | episode lookup failed: {e}", instance)
            continue

        for episode in episodes:
            if not episode.get("hasFile"):
                continue
            air_utc = parse_air_date(episode.get("airDateUtc"))
            if air_utc is None:
                continue

            if now < air_utc:
                found.append((series_title, episode, air_utc, "has not aired yet"))
                continue

            added = parse_air_date((episode.get("episodeFile") or {}).get("dateAdded"))
            if added is None:
                continue
            if added < air_utc - margin:
                ahead = air_utc - added
                found.append((
                    series_title, episode, air_utc,
                    f"imported {ahead.days}d {ahead.seconds // 3600}h before air",
                ))

    return found


def cleanup_files(instance, args, now, excluded, tally):
    scope = args.scope or instance.delete_scope
    margin = timedelta(hours=instance.early_import_margin_hours)

    try:
        if scope == "imported_early":
            found = collect_imported_early(instance, now, excluded, margin)
        else:
            found = collect_unaired(instance, now, excluded)
    except urllib.error.HTTPError as e:
        log(f"ERROR: HTTP {e.code} during {scope} file scan", instance)
        tally.failed += 1
        return
    except (urllib.error.URLError, OSError) as e:
        log(f"ERROR: {scope} file scan failed: {e}", instance)
        tally.failed += 1
        return

    if not found:
        log(f"Files: nothing early on disk ({scope})", instance)
        return

    # A multi-episode file shows up once per episode. Delete it once.
    seen_files = set()

    for series_title, episode, air_utc, reason in found:
        episode_file = episode.get("episodeFile") or {}
        file_id = episode.get("episodeFileId") or episode_file.get("id")
        if not file_id or file_id in seen_files:
            continue
        seen_files.add(file_id)

        size = episode_file.get("size") or 0
        path = episode_file.get("relativePath") or episode_file.get("path") or "<unknown path>"

        tally.files_flagged += 1
        log(
            f"EARLY FILE {series_title} {episode_label(episode)} | "
            f"air date {air_utc:%Y-%m-%d} | {reason} | {human_size(size)} | {path}",
            instance,
        )

        if args.dry_run:
            continue

        ok, err = delete_episode_file(instance, file_id)
        if not ok:
            tally.failed += 1
            log(f"  -> delete failed: {err}", instance)
            continue

        tally.files_deleted += 1
        tally.bytes_freed += size
        log("  -> deleted from disk", instance)

        if not instance.delete_blocklist or args.no_blocklist:
            continue

        ok, detail = blocklist_episode_grab(instance, episode.get("id"))
        if ok:
            log(f"  -> blocklisted the release: {detail}", instance)
        else:
            log(f"  -> not blocklisted ({detail}), Sonarr may grab it again", instance)


# --------------------------------------------------------------------------

def process_instance(instance, args):
    tally = Tally()
    if not instance.enabled:
        log("Disabled in config, skipping", instance)
        return tally

    now = datetime.now(timezone.utc)
    excluded = {s.strip().lower() for s in args.exclude_series.split(",") if s.strip()}

    if not args.files_only:
        guard_queue(instance, args, now, excluded, tally)

    if args.queue_only:
        return tally
    if args.no_delete or not instance.delete_early_files:
        log("Files: delete pass disabled", instance)
        return tally

    cleanup_files(instance, args, now, excluded, tally)
    return tally


def main():
    parser = argparse.ArgumentParser(
        description="Block Sonarr grabs for unaired episodes and delete early imports."
    )
    parser.add_argument(
        "-i", "--instance", action="append", default=[],
        help="Instance name from the config. Repeatable. Default: all enabled.",
    )
    parser.add_argument("--config", default=None, help="Path to sonarr-guard.conf")
    parser.add_argument(
        "--grace-days", type=float, default=None,
        help="Override the per-instance queue grace period for this run.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only, change nothing.")
    parser.add_argument(
        "--scope", choices=("unaired", "imported_early"), default=None,
        help="Override delete_scope for this run.",
    )
    parser.add_argument("--queue-only", action="store_true", help="Skip the file delete pass.")
    parser.add_argument("--files-only", action="store_true", help="Skip the queue pass.")
    parser.add_argument("--no-delete", action="store_true", help="Never delete files on disk.")
    parser.add_argument(
        "--no-blocklist", action="store_true",
        help="Remove and delete without blocklisting. Warning: Sonarr will usually "
             "re-grab the same release on the next RSS sync, causing a loop.",
    )
    parser.add_argument(
        "--keep-files", action="store_true",
        help="Queue pass only: leave the download in the torrent/usenet client.",
    )
    parser.add_argument(
        "--exclude-series", default="",
        help="Comma-separated series titles to skip on every instance.",
    )
    args = parser.parse_args()

    if args.queue_only and args.files_only:
        parser.error("--queue-only and --files-only are mutually exclusive")

    init_log("poll")

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
            log(f"Configured: {', '.join(sorted(instances))}")
            return 2
        selected = [i for n, i in instances.items() if n.lower() in wanted]
    else:
        selected = list(instances.values())

    log(f"Config {path} | {len(selected)} instance(s)" + (" | DRY RUN" if args.dry_run else ""))

    totals = Tally()
    for instance in selected:
        totals.merge(process_instance(instance, args))

    if args.dry_run:
        log(
            f"Dry run complete. Queue: checked {totals.checked}, would remove "
            f"{totals.flagged}. Files: would delete {totals.files_flagged}. "
            "Nothing changed."
        )
    else:
        log(
            f"Done. Queue: checked {totals.checked}, flagged {totals.flagged}, "
            f"removed {totals.removed}. Files: flagged {totals.files_flagged}, "
            f"deleted {totals.files_deleted} ({human_size(totals.bytes_freed)} freed). "
            f"Failures: {totals.failed}."
        )

    return 1 if totals.failed else 0


if __name__ == "__main__":
    sys.exit(main())
