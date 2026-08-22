# How it works

## Layout

```
<guard dir>/
  sonarr-guard.conf          the single source of truth for every instance
  sonarr-guard-state.json    ids of every episode this tool unmonitored
  sonarr-guard.log           one log, host and containers both append to it
  sonarr_guard_common.py     config, logging, Sonarr API (host)
  sonarr-airdate-guard.py    the cron poller (host)
  sonarr-grab-hook.sh        the Custom Script (runs inside every container)
  lib/patch-compose.py       installer helper
  lib/setup-connections.py   creates the Custom Script connections over the API
```

The same directory is bind-mounted into every Sonarr container at `/guard`. One
copy of everything, four containers reading it. Editing the config takes effect
everywhere with no restart, because the hook re-reads it on every event.

The mount is read-write so all layers can append to the shared log. The
containers run as the same UID that owns the directory, which is what makes a
single log file work.

## Why bash inside the container and Python outside

Sonarr Custom Scripts execute **inside** the Sonarr container. The linuxserver and
hotio images are Alpine and ship no `python3`, so an in-container Python hook
would mean installing a package into every container on every start.

Those images do ship `bash`, `curl`, `jq`, and GNU `date`. That is everything the
hook needs, so the hook is bash and installs nothing.

The poller runs on the host, where Python is a reasonable assumption, and it needs
real data structures for the calendar and library sweeps.

## The hook

Bound to two events.

### On Grab

Sonarr fires this the moment it sends a release to your download client, before
any of it has transferred.

1. Read this instance's own key and port from `/config/config.xml`.
2. Work out which config section that is (see "Self-identification" below).
3. Read `grace_days`, `mode`, and `enabled` from the shared config.
4. Parse `sonarr_release_episodeairdatesutc`, which is a comma-separated list of
   the UTC air times of every episode the release covers.
5. Pick the reference date: the **latest** under `strict`, the **earliest** under
   `lenient`.
6. If `now >= reference + grace_days`, log `ALLOW` and exit.
7. Otherwise log `EARLY`, then find the queue item and delete it with
   `blocklist=true`, `removeFromClient=true`, and `skipRedownload=true`.

The queue item is matched on `sonarr_download_id`, compared case insensitively
because download clients differ on hex casing, falling back to the release
title.

Step 7 exists because Sonarr gives a script no way to veto a grab. The release is
already on its way to the download client, so the hook removes it immediately
afterwards. In practice this happens within a couple of seconds, long before any
meaningful amount has transferred.

The grab event can fire slightly before the download registers in the queue, so
the hook polls for the queue item every two seconds. It gives up after
`MAX_RUNTIME` (45 seconds) and leaves it to the poller, because a Custom Script
that hangs blocks Sonarr's notification thread.

### On Import

Sonarr calls this event `Download`. It fires when a file is moved into your
library.

1. Parse `sonarr_episodefile_episodeairdatesutc`.
2. Pick the reference date the same way.
3. If `now >= reference`, exit silently. No grace here, see the note in the
   configuration reference.
4. Otherwise log `EARLY IMPORT`, `DELETE /api/v3/episodefile/{id}`, then
   blocklist the release that produced it.

This is the same job the poller's file pass does, but immediately rather than up
to an hour later.

### Self-identification

Sonarr 4.0.19 removed the Arguments field from custom scripts. Sending any value
now fails validation with *"Arguments are no longer supported for custom
scripts"*, so a hook cannot be told which instance it belongs to.

It works it out instead. `/config/config.xml` inside the container holds that
instance's key, port, and URL base, which is enough to both talk to Sonarr and
find the matching config section. The order is: explicit argument if one was
somehow passed, then key match, then port match, then Sonarr's instance name,
then the only section if just one exists.

Matching on the key rather than the port is deliberate. Two instances can share a
port number in different containers, but not a key.

### Environment variable casing

Sonarr passes its variables through a .NET `StringDictionary`, which lowercases
keys on some builds and not others. Scripts in the wild that only read
`$sonarr_eventtype` break on the builds that preserve `Sonarr_EventType`.

The hook normalises both at startup by walking the exported environment and
copying every `sonarr_*` variable, in either casing, to a known-lowercase name.
Both forms are covered by the test suite in the troubleshooting doc.

### Failure behaviour

The hook always exits `0`, on every path. A guard that returns non-zero puts
errors in Sonarr's log and can wedge the notification thread. Every failure is
logged and then swallowed, and the poller picks up whatever the hook could not
finish.

## The poller

Three passes per instance.

### Pass 1, the queue

`GET /api/v3/queue` with `includeEpisode` and `includeSeries`, following
pagination. For each item with a single episode attached, compare
`airDateUtc + grace_days` to now, and `DELETE /api/v3/queue/{id}` anything still
early.

Items with no single episode attached, which is what a season pack looks like in
the queue, are logged and skipped. The hook covers those at grab time, where the
full episode list is available.

Sonarr v3 spelled the query parameter `blacklist` and v4 spells it `blocklist`.
The poller tries the modern spelling and falls back on a 400.

### Pass 2, the files

**`unaired` scope.** `GET /api/v3/calendar` from now to
`now + future_window_days`, with `includeEpisodeFile`, walked in ninety-day chunks
because Sonarr dislikes very wide ranges. Any entry with `hasFile` set and an air
time still in the future is an early import.

One call chain covers the entire library, which is why this can run hourly.

**`imported_early` scope.** `GET /api/v3/series`, then
`GET /api/v3/episode?seriesId=N&includeEpisodeFile=true` for each. Flags two
things:

- an episode with a file whose air time is still in the future
- an episode with a file whose `dateAdded` is more than
  `early_import_margin_hours` before its air time

One call per series, so this is the weekly job rather than the hourly one.

### Deleting

`DELETE /api/v3/episodefile/{id}` rather than `rm`. Going through Sonarr keeps
history, episode state, and disk statistics consistent, and honours your recycle
bin setting if you have one.

A file covering several episodes appears once per episode in the scan, and is
deduplicated by file id so it is only deleted once.

### Pass 3, monitoring

The one that matters. Passes 1 and 2 clean up; this one stops it happening.

Sonarr will not search for an unmonitored episode. So the poller unmonitors
every episode whose air time is still in the future, and re-monitors it once that
time passes:

```
PUT /api/v3/episode/monitor   {"episodeIds": [...], "monitored": false}
```

The episode ids go into `sonarr-guard-state.json`, and only ids in that file are
ever re-monitored. This is the difference from
[swurApp](https://github.com/OwlCaribou/swurApp), which re-monitors every aired
episode in the latest season and so cannot distinguish its own changes from the
user's, needing an "ignore" tag as a workaround. Both mechanisms are supported
here, but the bookkeeping means the tag is optional rather than load-bearing.

The state file is written atomically, and re-read and merged immediately before
writing, so a hook firing mid-run cannot lose an entry.

`--restore-monitoring` re-monitors everything in the state file regardless of air
date and empties it. Run it before uninstalling.

### Why the queue removal always skips redownload

Sonarr's **Redownload Failed** setting is on by default. Marking a release failed
publishes a `DownloadFailedEvent`, and Sonarr responds by searching for a
replacement immediately.

For an unaired episode the replacement is simply the next fake. So a guard that
blocklists without `skipRedownload=true` creates its own perpetual loop: grab,
block, search, grab, at whatever rate the indexer can serve new release names.
This was observed in practice at roughly one new fake every thirty to forty
seconds.

Every queue removal here therefore passes `skipRedownload=true`. We never want a
replacement for an episode that has not aired.

The file-deletion path has no equivalent flag, because
`POST /api/v3/history/failed/{id}` takes only an id. It can still trigger one
redownload, but that grab is caught by the On Grab hook and removed with
`skipRedownload=true`, so the chain terminates after a single extra round.

### Blocklisting

Sonarr has no endpoint that adds an arbitrary release to the blocklist. The only
route is to mark a history record as failed:

```
GET  /api/v3/history?episodeId=N&sortKey=date&sortDirection=descending
POST /api/v3/history/failed/{id}
```

The poller takes the most recent `grabbed` record for the episode and marks it
failed, which blocklists the release. If there is no grab record, for instance
because the file was manually imported, the deletion still stands and the log says
so.

## Date parsing

Sonarr returns ISO 8601 in UTC, sometimes with a trailing `Z`, sometimes with
fractional seconds of arbitrary length. Python 3.10's `datetime.fromisoformat`
handles neither, so `parse_air_date` uses an explicit regex and builds the
`datetime` by hand. This matters, because a parse failure that silently returns
`None` would make the guard skip exactly the releases it exists to catch.

The bash side uses GNU `date -u -d`, which the Alpine images provide via
coreutils and which parses the `Z` suffix correctly. Busybox `date` does not, so
if you are on an image that only has busybox, check this first.

## What the installer does to your compose files

It inserts one line into the `volumes:` list of each Sonarr service:

```yaml
    volumes:
      - /home/you/sonarr-guard:/guard      <- added
      - ./config:/config
      - /mnt/nas:/media
```

The patcher is line-based rather than a YAML round trip, on purpose. Loading a
compose file with PyYAML and dumping it back strips every comment and reflows the
whole document, which is a rude thing to do to somebody's infrastructure.

A timestamped `.bak` is written next to each file before it is touched, and only
the projects that actually changed are recreated.
