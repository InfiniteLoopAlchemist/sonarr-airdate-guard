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

The section name is the instance's identity. It goes in the Arguments field of
that instance's Custom Script connection, and it is what `-i` accepts on the
poller. Matching is case insensitive.

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

Used by the poller only. The installer reads it out of the container's
`config.xml`.

The hook reads the key from `/config/config.xml` at run time instead, which means
regenerating the key in the Sonarr UI cannot desync the hook. It can desync the
poller, so re-run `install.sh` after deleting the config, or edit the value by
hand.

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

Leaving this off means Sonarr sees a missing episode, searches, finds the same
fake, and grabs it again. The guard deletes it again. That loop hammers your
indexer, so keep it on unless you have a specific reason.

If no grab record can be found, for example on a file that was manually imported,
the deletion still happens and the log says the release was not blocklisted.

## Grace does not apply to deletion

Worth stating plainly, because it surprises people.

`grace_days` is used by the queue guard and nothing else. File deletion uses the
raw air time.

The reasoning: blocking a download is cheap and reversible, so it is fine to be
cautious and block for a few days after air. Deleting a file is neither, so it
only happens in the case that admits no doubt, which is an episode that has not
aired. A file for an episode that aired two hours ago is left alone, whatever
`grace_days` says.
