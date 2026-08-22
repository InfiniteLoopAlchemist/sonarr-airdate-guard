# Configuration reference

One file, `<guard dir>/sonarr-guard.conf`, drives both the in-container hook and
the host-side poller. It is standard INI, parsed by Python's `configparser` on the
host and by `awk` inside the containers.

`[DEFAULT]` applies to every instance. Anything set inside a `[section]` overrides
it for that instance only.

```ini
[DEFAULT]
grace_days = 3
mode = strict

[main]
url = http://127.0.0.1:8989
api_key = ...

[anime]
url = http://127.0.0.1:9779
api_key = ...
grace_days = 5      ; this instance only
mode = lenient      ; this instance only
```

The section name is the instance's identity, and what `-i` accepts on the poller.
Matching is case insensitive.

You do not have to tell the hook which section it is. Sonarr 4.0.19 removed the
Arguments field from custom scripts, so the hook reads its own key and port out
of `/config/config.xml` and finds the matching section itself, in this order:

1. an explicit argument, on older Sonarr builds that still allow one
2. the section whose `api_key` matches this container's
3. the section whose `url` ends in this container's port
4. Sonarr's own instance name, if it matches a section name
5. the only section, if just one is configured

## Connection

### `url`

Where the **host-side poller** reaches this instance. The installer fills this in
from the container's published port, for example `http://127.0.0.1:9779`. If the
port is not published it falls back to the container's address on its Docker
network, which works but breaks whenever the container gets a new IP.

If Sonarr has a `UrlBase` set, it is included here.

The in-container hook ignores this entirely. It builds its own URL from
`/config/config.xml`, so it always talks to `127.0.0.1` inside its own container.

### `api_key`

Read by the poller to talk to the instance, and used by the hook to work out
which section describes it. The installer fills it in from the container's
`config.xml`.

If you regenerate a key in the Sonarr UI, update it here too. The poller will
start returning 401, and the hook will fall back to matching on port.

### `enabled`

Default `true`. Set `false` to make both layers ignore this instance without
deleting its section. The hook logs that it is disabled and takes no action.

## Queue guard

These control what happens when Sonarr grabs a release.

### `grace_days`

Default `3`. A release is considered early until `air time + grace_days`. Anything
grabbed before that is removed from the queue and blocklisted.

Fractional values work, so `0.5` is twelve hours.

Why have a grace period at all: air dates in TVDB are the network's slot, and
a genuine web release often shows up hours later. Blocking a grab for a few days
after air costs you nothing, because Sonarr will retry, and it catches
mislabelled releases that are only slightly early.

Set it to `0` to block only releases for episodes that have not aired at all.

### `mode`

Default `strict`. Decides how a release covering more than one episode is judged.

| Mode | Reference date | Effect |
| --- | --- | --- |
| `strict` | the **latest** air date in the release | reject if any episode is still early |
| `lenient` | the **earliest** air date in the release | reject only if every episode is still early |

`strict` is right for almost everything. A season pack that includes next month's
episode is a fake, no matter how many aired episodes it also contains.

`lenient` exists for anime, where absolute numbering, split cours, and
inconsistent metadata mean a legitimate batch release often carries one episode
whose recorded air date is wrong. It lets a partial pack through as long as its
first episode has aired.

## File deletion

These control what happens to files already on disk.

### `delete_early_files`

Default `true`. Set `false` to guard the queue but never touch the library. The
hook still blocks grabs; only the deletion behaviour is switched off, in both the
On Import hook and the poller's file pass.

### `delete_scope`

Default `unaired`.

**`unaired`** deletes files for episodes whose air time is still in the future.
One calendar call per instance covers the whole library, so it is cheap enough to
run every hour. This is the case that cannot be anything but wrong: the episode
does not exist yet.

**`imported_early`** does everything `unaired` does, and additionally deletes
files whose import timestamp predates the episode's air time by more than
`early_import_margin_hours`, even if the episode has since aired. This is what
finds the fake that landed two months ago and is still sitting in your library
looking like a real episode.

The deep scope costs one API call per series, so it is worth running on its own
schedule rather than hourly. The installer sets it up as a weekly cron entry.

Override for one run with `--scope`.

### `early_import_margin_hours`

Default `24`. Used by `imported_early` only.

How far ahead of air time an import is still allowed to be. Streaming releases
sometimes land legitimately before the recorded air time, because the platform
drops at midnight in one region while TVDB records a slot in another. Twenty-four
hours absorbs that. A fake grabbed a month early clears this bar by a mile.

Lower it if you want to be aggressive, raise it if you have shows with
consistently sloppy metadata.

### `future_window_days`

Default `400`. Used by `unaired` only.

How far into the future the calendar scan reaches. Anything beyond this is not
checked. The default covers just over a year, which is well past any realistic
early grab. Lower it slightly if you have an enormous library and want the hourly
run faster.

### `delete_blocklist`

Default `true`. After deleting a file, find the grab that produced it in Sonarr's
history and mark it failed, which is the only way to get a release onto Sonarr's
blocklist.

It stops that exact release coming back, which is worth having, but do not
mistake it for the protection. Blocklists match release names, and the same fake
returns moments later under a slightly different one. `unmonitor_unaired` is what
actually stops the grabbing.

Note that Sonarr's **Redownload Failed** setting, on by default, makes marking a
release failed trigger an immediate search for a replacement. Every queue removal
this tool makes therefore passes `skipRedownload=true`, because a replacement for
an unaired episode is just the next fake. Without that flag, blocklisting builds
an endless grab, block, search, grab loop.

If no grab record can be found, for example on a file that was manually imported,
the deletion still happens and the log says the release was not blocklisted.



## Monitoring

The pass that prevents the problem rather than cleaning up after it. See
[how it works](how-it-works.md) for why deleting and blocklisting alone cannot
succeed.

### `unmonitor_unaired`

Default `true`. Unmonitor every episode that has not aired, and re-monitor it
once it has.

Sonarr will not search for an unmonitored episode, so the fakes stop being
grabbed at all. Without this the guard is stuck cleaning up the same episodes
forever, because Sonarr keeps searching for anything monitored and missing, and
the blocklist cannot keep up with the supply of release names.

Every episode this tool unmonitors is recorded in `sonarr-guard-state.json`,
next to the config. Only ids in that file are ever re-monitored, so an episode
you unmonitored yourself is never silently switched back on.

The window is `future_window_days` ahead, and the re-monitor happens on the first
run after the air time passes, so with the default hourly cron an episode is
monitored again within an hour of airing.

**Before uninstalling, run `--restore-monitoring`.** Delete the state file or the
scripts without doing that and every unaired episode stays unmonitored, silently,
forever.

### `ignore_tags`

Comma-separated Sonarr tag labels. Any series carrying one of these tags is left
completely alone by the monitoring pass.

```ini
ignore_tags = early, live-sport
```

Use it for shows that legitimately publish ahead of their listed air date, where
you would rather have the early release than the protection. Tag the series in
Sonarr, and this tool stops touching its monitoring.

`--exclude-series` does the same thing by title for a single run.

## Grace does not apply to deletion or monitoring

Worth stating plainly, because it surprises people.

`grace_days` is used by the queue guard and nothing else. File deletion and
unmonitoring both use the raw air time.

The reasoning: blocking a download is cheap and reversible, so it is fine to be
cautious and block for a few days after air. Deleting a file is not, so it only
happens in the case that admits no doubt, which is an episode that has not aired.
A file for an episode that aired two hours ago is left alone, whatever
`grace_days` says.
