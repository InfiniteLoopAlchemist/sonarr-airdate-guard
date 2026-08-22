# Sonarr Air Date Guard

Stops Sonarr grabbing episodes that have not aired yet, and deletes the ones that
already slipped through.

Works across any number of Sonarr instances from a single config file and a
single copy of the scripts.

## The problem

Sonarr trusts your indexer's episode numbering. When a release is mislabelled, or
an indexer is seeded with fakes, Sonarr happily matches a release to an episode
that is still weeks away and grabs it. You end up with a library full of files
for episodes that do not exist yet, and Sonarr thinks those episodes are done, so
it never grabs the real ones.

Two shapes this takes in practice:

**A numbering mismatch.** The release says `S21E12`, your indexer's metadata maps
it onto `S22E12`, which airs next month:

```
EARLY American Dad! S22E12 | air date 2026-09-14 | clears in 25d 12h
  | American.Dad.S21E12.The.Legend.of.Mike.Madonia.the.Rototiller.Man.1080p.AMZN.WEB-DL...
```

**A fake season pack.** Five files land in one batch, months before the episodes
air. Note that episodes 1 to 4 were imported on or after their air dates and are
genuine, while 5 to 9 all arrived on the same afternoon in July for episodes
airing from late August into September:

```
S11E01  air=2026-08-03  added=2026-08-19   legitimate
S11E02  air=2026-08-03  added=2026-08-13   legitimate
S11E03  air=2026-08-10  added=2026-08-13   legitimate
S11E04  air=2026-08-17  added=2026-08-17   legitimate
S11E05  air=2026-08-24  added=2026-07-21   fake, imported before E01 even aired
S11E06  air=2026-08-31  added=2026-07-21   fake
S11E07  air=2026-09-07  added=2026-07-21   fake
S11E08  air=2026-09-14  added=2026-07-21   fake
S11E09  air=2026-09-21  added=2026-07-21   fake
```

The guard catches the first case before the download finishes and removes the
second from disk.

## How it works

Two layers, because neither one alone is enough.

**In-container hook.** A Custom Script bound to each instance's On Grab and
On Import events. On Grab it pulls an early release out of the queue and
blocklists it, usually within a couple of seconds, before the download completes.
On Import it deletes an early file that got in anyway and blocklists its release.

Sonarr has no way for a script to veto a grab, so the hook removes the queue item
immediately after the fact. It is written in bash with `curl` and `jq` because the
linuxserver Sonarr images are Alpine and ship no `python3`. All three tools are
already in the image, so nothing has to be installed into your containers.

**Host-side poller.** A cron job that sweeps every instance as a backstop, for
everything the hook missed: Sonarr restarting mid-grab, a download that took too
long to register in the queue, a script error, or files that predate the install.

The two layers share one config file, so grace periods and modes cannot drift
apart between them.

### What it will and will not delete

The queue guard uses `grace_days`. The file deletion does not, deliberately.

A file for an episode that aired two hours ago might be perfectly real. A file for
an episode that has not aired **cannot** be. Only the unambiguous case gets
deleted, so a legitimate release that lands slightly ahead of its recorded air
time is never at risk. The optional `imported_early` scope widens this to files
whose import predates air time by more than a configurable margin, for cleaning up
fakes that are still in your library from months ago.

Deletion goes through Sonarr's API rather than `rm`, so history, episode state,
and disk statistics all stay consistent, and the file lands in your recycle bin if
you have one configured.

## Requirements

- Docker host with `python3` (3.8 or newer) for the poller
- Sonarr v3 or v4 in containers, any image
- `bash`, `curl`, and `jq` inside the containers, which linuxserver and hotio
  images already have
- Standard library only, no `pip install`

## Install

```bash
git clone https://github.com/InfiniteLoopAlchemist/sonarr-airdate-guard.git
cd sonarr-airdate-guard
./install.sh
```

Look before you leap:

```bash
DRY_RUN=1 ./install.sh
```

The installer finds every running container with `sonarr` in its image name and,
for each one:

1. reads its port and key from its own `/config/config.xml`
2. writes a config section named after the container
3. adds a `/guard` bind mount to its compose file, keeping a timestamped backup
4. recreates only the containers whose compose file actually changed
5. installs the cron entries

It never overwrites an existing config, so re-running it is how you upgrade.

By default everything lands in `$HOME/sonarr-guard`. Override with
`SONARR_GUARD_DIR=/opt/sonarr-guard ./install.sh`. If a container already mounts
`/guard`, that host path is reused automatically.

### Wiring up the hook

The installer prints the section name for each container. In each Sonarr UI:

**Settings > Connect > + > Custom Script**

| Field | Value |
| --- | --- |
| Name | `Air Date Guard` |
| On Grab | on |
| On Import | on |
| everything else | off |
| Path | `/guard/sonarr-grab-hook.sh` |
| Arguments | the section name for that container, e.g. `anime` |

Saving fires a Test event, and the hook reports which config section it bound to:

```
Test OK. Bound to [anime] on http://127.0.0.1:9779 (Sonarr 4.0.19.2979),
grace 5d, mode lenient, delete_early_files=true
```

A typo in Arguments fails loudly right there, listing the valid sections, instead
of silently doing nothing at 3am.

## Before you let it delete anything

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py --dry-run
```

This reports every queue item and every file it would touch, and changes nothing.
Read it. Confirm the air dates look right for the shows involved. Then drop the
flag.

## Usage

```bash
# everything, all instances, no changes
python3 sonarr-airdate-guard.py --dry-run

# everything, live
python3 sonarr-airdate-guard.py

# only these two instances
python3 sonarr-airdate-guard.py -i anime -i main

# the deep sweep for old fakes still in the library
python3 sonarr-airdate-guard.py --files-only --scope imported_early --dry-run

# guard the queue but never touch files on disk
python3 sonarr-airdate-guard.py --queue-only

# leave one show alone
python3 sonarr-airdate-guard.py --exclude-series "Doctor Who,Taskmaster"

# check config and reachability of every instance
python3 sonarr_guard_common.py
```

| Flag | Effect |
| --- | --- |
| `-i`, `--instance` | Limit to named instances. Repeatable. |
| `--config` | Path to `sonarr-guard.conf`. |
| `--dry-run` | Report only, change nothing. |
| `--scope` | Override `delete_scope` for this run. |
| `--grace-days` | Override the queue grace period for this run. |
| `--queue-only` | Skip the file deletion pass. |
| `--files-only` | Skip the queue pass. |
| `--no-delete` | Never delete files, whatever the config says. |
| `--no-blocklist` | Remove without blocklisting. See the warning below. |
| `--keep-files` | Queue pass only: leave the download in the torrent or usenet client. |
| `--exclude-series` | Comma-separated series titles to skip everywhere. |

`--no-blocklist` is usually a mistake. Without blocklisting, Sonarr will grab the
same release again on the next RSS sync, the guard will remove it again, and you
get an infinite loop that hammers your indexer.

## Schedule

The installer sets up two cron entries:

```cron
0 * * * *   python3 ~/sonarr-guard/sonarr-airdate-guard.py
30 4 * * 0  python3 ~/sonarr-guard/sonarr-airdate-guard.py --files-only --scope imported_early
```

Hourly is the queue guard plus the cheap unaired file check. The weekly one is the
deep sweep, on its own schedule because it costs an API call per series.

Skip cron entirely with `SONARR_GUARD_CRON=0 ./install.sh` if you would rather use
systemd timers.

## Logs

Everything, from every instance and from both layers, goes to one file at
`<guard dir>/sonarr-guard.log`. Lines are tagged so you can tell them apart:

```
[2026-08-22 09:09:53][poll][animation] EARLY American Dad! S22E12 | air date 2026-09-14 ...
[2026-08-22 09:09:54][poll][animation]   -> deleted from disk
[2026-08-22 09:09:54][poll][animation]   -> blocklisted the release: Futurama.S11E05...
[2026-08-22 09:10:07][hook][main] Test OK. Bound to [main] on http://127.0.0.1:8989 ...
```

`[hook]` lines come from inside a container, `[poll]` from the host cron job, and
the second tag is the instance.

Follow one instance:

```bash
tail -f ~/sonarr-guard/sonarr-guard.log | grep '\[anime\]'
```

There is no log rotation built in. Add a `logrotate` snippet if yours grows.

## Documentation

- [Configuration reference](docs/configuration.md), every option and what it does
- [How it works](docs/how-it-works.md), the decision logic and API calls in detail
- [Troubleshooting](docs/troubleshooting.md), when something is not firing

## Uninstall

```bash
crontab -l | grep -v sonarr-airdate-guard.py | crontab -
```

Delete the Custom Script connection in each Sonarr UI, remove the `- ...:/guard`
line from each compose file, `docker compose up -d` each project, then delete the
guard directory. The installer leaves a timestamped `.bak` of every compose file
it touched, next to the original.

## Licence

All rights reserved. See [LICENSE](LICENSE).
