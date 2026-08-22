# Sonarr Air Date Guard

Stops Sonarr grabbing episodes that have not aired yet, and deletes the ones that
already slipped through.

Works across any number of Sonarr instances from a single config file and a
single copy of the scripts. Nothing is installed inside your containers.

## The problem

Sonarr trusts your indexer's episode numbering. When a release is mislabelled, or
an indexer is seeded with fakes, Sonarr matches a release to an episode that is
still weeks away and grabs it. You end up with a library full of files for
episodes that do not exist yet, and Sonarr thinks those episodes are done, so it
never grabs the real ones.

**Sonarr v5 will fix half of this natively.**
[#969](https://github.com/Sonarr/Sonarr/issues/969) was closed as completed in
February 2026, and `v5-develop` carries an `AirDateSpecification` driven by three
new release profile fields, `AirDateRestriction`, `AirDateGracePeriod`, and
`AllowSeasonPackWithoutAllEpisodesAired`. If you are running v5, use that for the
grab side rather than this.

On Sonarr v4, which is the only stable line at the time of writing, there is
nothing. Delay profiles do not help: they key on file age, not air date, so a file
that appeared a week before air is grabbed as soon as the delay elapses. Earlier
requests ([#5312](https://github.com/Sonarr/Sonarr/issues/5312),
[forum](https://forums.sonarr.tv/t/reject-unaired-episodes/39115)) were closed in
favour of the v5 work.

Even on v5, two things remain uncovered, and this tool handles both: files that
were **already imported** before you turned the setting on, and the fact that
Sonarr keeps the episode monitored and so keeps searching for it.

Neither is coming to Sonarr. Asked directly in
[#8896](https://github.com/Sonarr/Sonarr/issues/8896), the answer was:

> This is better as an external script that uses the API to determine which
> episodes should be cleaned up, instead of a feature built into Sonarr itself.
> No plans to add or support this functionality directly in Sonarr.
>
> — markus101, Sonarr maintainer

So this is the intended shape of the fix, not a stopgap waiting to be obsoleted.

Two shapes this takes in practice:

**A numbering mismatch.** The release says `S21E12`, your indexer's metadata maps
it onto `S22E12`, which airs next month:

```
EARLY American Dad! S22E12 | air date 2026-09-14 | clears in 25d 12h
  | American.Dad.S21E12.The.Legend.of.Mike.Madonia.the.Rototiller.Man.1080p.AMZN.WEB-DL...
```

**A fake season pack.** Episodes 1 to 4 below were imported on or after their air
dates and are genuine. Episodes 5 to 9 all arrived on the same afternoon in July,
for episodes airing from late August into September, before episode 1 had even
aired:

```
S11E01  air=2026-08-03  added=2026-08-19   legitimate
S11E02  air=2026-08-03  added=2026-08-13   legitimate
S11E03  air=2026-08-10  added=2026-08-13   legitimate
S11E04  air=2026-08-17  added=2026-08-17   legitimate
S11E05  air=2026-08-24  added=2026-07-21   fake
S11E06  air=2026-08-31  added=2026-07-21   fake
S11E07  air=2026-09-07  added=2026-07-21   fake
S11E08  air=2026-09-14  added=2026-07-21   fake
S11E09  air=2026-09-21  added=2026-07-21   fake
```

## Why deleting and blocklisting is not enough

This is the part most scripts get wrong, so it is worth being explicit.

Delete the fake and blocklist it, and Sonarr sees a monitored episode with no
file, searches, and grabs the next fake. The blocklist does not save you: the
same content comes back seconds later as `-SuccessfulCrab` instead of
`-SuccessfulCrab-FTP`, or from a different indexer with the same content
([#6108](https://github.com/Sonarr/Sonarr/issues/6108)). Blocklists match
releases, and the fakers have an unlimited supply of release names.

Worse, blocklisting is itself the trigger. Sonarr's **Redownload Failed** setting
is on by default, so marking a release failed makes Sonarr immediately search for
a replacement. A naive guard therefore builds its own infinite loop: grab, block,
search, grab, with a new fake every thirty seconds, forever.

So this tool does three things, and the third is the one that actually works:

1. **Guard the queue.** Pull an early release before the download finishes, and
   blocklist it *with `skipRedownload=true`* so Sonarr does not go looking for a
   replacement it should not have.
2. **Clean the library.** Delete files for episodes that have not aired.
3. **Unmonitor unaired episodes**, and give the monitoring back the moment they
   air. With nothing monitored, Sonarr has no reason to search, and the problem
   stops happening rather than being cleaned up afterwards.

The third idea is not original; the well-known
[swurApp](https://github.com/OwlCaribou/swurApp) is built entirely around it.
The difference here is bookkeeping: swurApp re-monitors every aired episode in
the latest season, so it cannot tell its own changes from ones you made
deliberately, and works around that with an "ignore" tag. This tool records the
id of every episode it unmonitors and only ever re-monitors those.

## How it works

**In-container hook.** A Custom Script bound to each instance's On Grab and
On Import events. On Grab it pulls an early release out of the queue and
blocklists it, usually within a couple of seconds. On Import it deletes an early
file that got in anyway.

It is written in bash with `curl` and `jq` because the linuxserver Sonarr images
are Alpine and ship no `python3`. All three tools are already in the image, so
nothing has to be installed into your containers.

**Host-side poller.** A cron job that sweeps every instance: the queue, the
library, and the monitoring state. It is the backstop for anything the hook
missed, and it owns the unmonitor and re-monitor passes.

Both read the same config file, so grace periods and modes cannot drift apart.

### What it will and will not delete

The queue guard uses `grace_days`. File deletion does not, deliberately.

A file for an episode that aired two hours ago might be real. A file for an
episode that has not aired **cannot** be. Only the unambiguous case is deleted,
so a legitimate release that lands slightly ahead of its recorded air time is
never at risk. The optional `imported_early` scope widens this to files whose
import predates air time by more than a configurable margin, for clearing out
fakes that have been sitting in your library for months.

Deletion goes through Sonarr's API rather than `rm`, so history, episode state,
and disk statistics stay consistent, and your recycle bin is honoured.

## Requirements

- Docker host with `python3` 3.8 or newer for the poller
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
6. creates the Custom Script connection over the API

There is nothing to click in the Sonarr UI afterwards. It never overwrites an
existing config, so re-running it is how you upgrade.

By default everything lands in `$HOME/sonarr-guard`. Override with
`SONARR_GUARD_DIR=/opt/sonarr-guard ./install.sh`. If a container already mounts
`/guard`, that host path is reused automatically.

### There is no Arguments field

Sonarr 4.0.19 removed argument support from custom scripts, rejecting any value
with *"Arguments are no longer supported for custom scripts"*. The hook therefore
identifies itself: it reads its own key and port from `/config/config.xml` and
matches them against the shared config. Test the binding at any time:

```bash
docker exec -e sonarr_eventtype=Test sonarr /guard/sonarr-grab-hook.sh
```

```
Test OK. Matched [main] by api key, on http://127.0.0.1:8989 (Sonarr 4.0.19.2979),
grace 3d, mode strict, delete_early_files=true
```

## Before you let it loose

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py --dry-run
```

Reports every queue item it would remove, every file it would delete, and every
episode it would unmonitor. Changes nothing. Read it, confirm the air dates look
right for the shows involved, then drop the flag.

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

# guard the queue but never touch files or monitoring
python3 sonarr-airdate-guard.py --queue-only

# leave one show alone
python3 sonarr-airdate-guard.py --exclude-series "Doctor Who,Taskmaster"

# give back every unmonitor this tool ever made
python3 sonarr-airdate-guard.py --restore-monitoring

# check config and reachability of every instance
python3 sonarr_guard_common.py

# re-create or remove the Custom Script connections
python3 lib/setup-connections.py
python3 lib/setup-connections.py --remove
```

| Flag | Effect |
| --- | --- |
| `-i`, `--instance` | Limit to named instances. Repeatable. |
| `--config` | Path to `sonarr-guard.conf`. |
| `--dry-run` | Report only, change nothing. |
| `--scope` | Override `delete_scope` for this run. |
| `--grace-days` | Override the queue grace period for this run. |
| `--queue-only` | Skip the file and monitoring passes. |
| `--files-only` | Skip the queue and monitoring passes. |
| `--no-delete` | Never delete files, whatever the config says. |
| `--no-unmonitor` | Skip the monitoring pass. |
| `--restore-monitoring` | Re-monitor everything this tool unmonitored, and forget it. |
| `--no-blocklist` | Remove without blocklisting. |
| `--keep-files` | Queue pass only: leave the download in the torrent or usenet client. |
| `--exclude-series` | Comma-separated series titles to skip everywhere. |

## Schedule

The installer sets up two cron entries:

```cron
0 * * * *   python3 ~/sonarr-guard/sonarr-airdate-guard.py
30 4 * * 0  python3 ~/sonarr-guard/sonarr-airdate-guard.py --files-only --scope imported_early
@reboot     sleep 180 && python3 ~/sonarr-guard/sonarr-airdate-guard.py
```

Hourly is the queue guard, the cheap unaired file check, and the monitoring
pass. The weekly one is the deep library sweep, on its own schedule because it
costs an API call per series. The `@reboot` entry is a catch-up run, because the
hourly slot can be up to an hour after a restart; the sleep gives Docker and
Sonarr time to finish starting.

### What survives a host reboot

All of it, and the protection does not depend on anything running.

| Thing | Where it lives | Survives |
| --- | --- | --- |
| Unmonitored flags | Sonarr's own database | yes |
| Custom Script connection | Sonarr's own database | yes |
| `/guard` bind mount | the compose file | yes |
| Scripts, config, state file | the guard directory on disk | yes |
| Cron entries | the user crontab | yes |

The important one is the first. What actually stops Sonarr grabbing an unaired
episode is a `monitored = false` flag in its database, not a running process. If
this tool never ran again, the episodes already unmonitored would stay protected.
The cost of it not running is the opposite: episodes that have aired never get
their monitoring back.

Your containers need `restart: unless-stopped` (or `always`) in their compose
files, and `docker` and `cron` need to be enabled at boot, which is the default
on most distributions. Check with:

```bash
systemctl is-enabled docker cron
docker inspect sonarr --format '{{.HostConfig.RestartPolicy.Name}}'
```

One thing worth checking that is nothing to do with this tool: if your media lives
on a network mount, make sure its `/etc/fstab` entry has **`nofail`**. Without it a
NAS that is slow or unreachable at boot can stall the boot into emergency mode.
And if the mount is missing while Sonarr starts, Sonarr sees an empty library and
may try to re-download a great deal of it. The guard will not make that worse, it
keys off Sonarr's `hasFile`, which is false for everything in that state, so it
deletes nothing.

Skip cron entirely with `SONARR_GUARD_CRON=0 ./install.sh` if you prefer systemd
timers.

## Logs

Everything, from every instance and both layers, goes to one file at
`<guard dir>/sonarr-guard.log`:

```
[2026-08-22 09:39:01][poll][main] Monitoring: unmonitored 10 unaired episode(s), Sonarr will stop searching for them until they air
[2026-08-22 09:40:43][hook][animation]   -> removed and blocklisted in 4s
[2026-08-22 09:40:44][hook][animation] EARLY IMPORT Futurama S11E5 | air date 2026-08-24 | 1d 20h before air | ...
[2026-08-22 09:40:44][hook][animation]   -> deleted from disk
```

`[hook]` lines come from inside a container, `[poll]` from the host cron job, and
the second tag is the instance.

```bash
tail -f ~/sonarr-guard/sonarr-guard.log | grep '\[anime\]'
```

There is no log rotation built in. Add a `logrotate` snippet if yours grows.

## Knowing when v5 makes half of this redundant

Sonarr does not announce release dates, so `tools/sonarr-v5-watch.py` watches for
the artefact instead: a v5 tag or release on GitHub, or a v5 image on
linuxserver's Docker Hub. It fires once when a prerelease appears and once when a
stable build does, and says nothing in between.

```bash
python3 -c "import secrets,string; a=string.ascii_lowercase+string.digits; print('sonarr-v5-'+''.join(secrets.choice(a) for _ in range(16)))"
export SONARR_V5_NTFY_TOPIC=sonarr-v5-...        # the topic it just printed
python3 tools/sonarr-v5-watch.py --test          # confirm it reaches you
python3 tools/sonarr-v5-watch.py --status        # what it can see right now
```

Then subscribe at `https://ntfy.sh/<your topic>` in a browser or the app, and run
it from cron:

```cron
15 9 * * *  SONARR_V5_NTFY_TOPIC=... /usr/bin/python3 /path/to/sonarr-v5-watch.py --quiet
```

ntfy needs no account, so the random topic name is the only thing keeping it
private. Point `SONARR_V5_NOTIFIARR_CONF` at a `notifiarr.conf` if you would
rather it went to Discord, though that needs the passthrough integration enabled
on notifiarr.com first.

## Documentation

- [Configuration reference](docs/configuration.md), every option and what it does
- [How it works](docs/how-it-works.md), the decision logic and API calls in detail
- [Troubleshooting](docs/troubleshooting.md), when something is not firing

## Uninstall

Give the monitoring back first, or every unaired episode stays unmonitored
forever:

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py --restore-monitoring
python3 ~/sonarr-guard/lib/setup-connections.py --remove
crontab -l | grep -v sonarr-airdate-guard.py | crontab -
```

Then remove the `- ...:/guard` line from each compose file, `docker compose up -d`
each project, and delete the guard directory. The installer leaves a timestamped
`.bak` of every compose file it touched.

## Licence

MIT. See [LICENSE](LICENSE).

This software deletes media files and changes what Sonarr monitors. Run it with
`--dry-run` first.
