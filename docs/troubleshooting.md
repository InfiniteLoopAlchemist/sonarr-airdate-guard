# Troubleshooting

Everything lands in `<guard dir>/sonarr-guard.log`. Start there.

```bash
tail -f ~/sonarr-guard/sonarr-guard.log
```

`[hook]` lines come from inside a container, `[poll]` from the host cron job,
`[connect]` from the connection installer. The second tag is the instance.

## Check the basics first

```bash
python3 ~/sonarr-guard/sonarr_guard_common.py
```

Pings every instance and prints its Sonarr version:

```
Config OK: /home/you/sonarr-guard/sonarr-guard.conf
  main: http://127.0.0.1:8989 grace=3.0d mode=strict delete=unaired unmonitor [enabled]
    reachable, Sonarr 4.0.19.2979
  anime: http://127.0.0.1:9779 grace=5.0d mode=lenient delete=unaired unmonitor [enabled]
    reachable, Sonarr 4.0.19.2979
```

| Output | Meaning |
| --- | --- |
| `UNREACHABLE: 401` | wrong `api_key` for that section |
| `UNREACHABLE: Connection refused` | wrong port, or the container is down |
| `UNREACHABLE: Connection reset by peer` | Sonarr is still starting, wait and retry |
| `no such file` | wrong `--config` path, or the guard directory was not created |

## Testing the hook without waiting for a real grab

The hook is an ordinary script, so you can drive it with a synthetic event.
Replace `sonarr` with your container name. You do not pass a section name; the
hook works out which instance it is.

**Does it load, and which section did it match?**

```bash
docker exec -e sonarr_eventtype=Test sonarr /guard/sonarr-grab-hook.sh
```

```
Test OK. Matched [main] by api key, on http://127.0.0.1:8989 (Sonarr 4.0.19.2979),
grace 3d, mode strict, delete_early_files=true
```

`by api key` is the healthy answer. `by port` means the key in your config no
longer matches the container's, which will also be breaking the poller.

**Does it reject an unaired release?** This one has no download id and a title
that is not in the queue, so it reports its decision, spends its 45 second budget
looking, and stops without touching anything.

```bash
docker exec \
  -e sonarr_eventtype=Grab \
  -e sonarr_series_title="Fake Show" \
  -e sonarr_release_seasonnumber=21 \
  -e sonarr_release_episodenumbers=12 \
  -e sonarr_release_title="Fake.Show.S21E12.1080p.WEB-DL" \
  -e sonarr_release_episodeairdatesutc="$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)" \
  sonarr /guard/sonarr-grab-hook.sh
```

```
EARLY Fake Show S21E12 | air date 2026-09-21 | clears in 32d 23h | Fake.Show.S21E12.1080p.WEB-DL
  -> not in the queue within 45s, most likely already imported. The import hook covers it.
```

**Does it allow an aired one?** Swap the date for `'-30 days'` and you get
`ALLOW ... grace cleared`, immediately.

**Does `strict` versus `lenient` behave?** Pass two dates, one past and one
future. A `strict` instance reports `EARLY`, a `lenient` one reports `ALLOW`.

```bash
-e sonarr_release_episodeairdatesutc="$(date -u -d '-30 days' +%Y-%m-%dT%H:%M:%SZ),$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)"
```

**Does the import path work?** Again with no file id, so nothing is deleted.

```bash
docker exec \
  -e sonarr_eventtype=Download \
  -e sonarr_series_title="Fake Show" \
  -e sonarr_episodefile_seasonnumber=21 \
  -e sonarr_episodefile_episodenumbers=12 \
  -e sonarr_episodefile_relativepath="Season 21/Fake.Show.S21E12.mkv" \
  -e sonarr_episodefile_episodeairdatesutc="$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)" \
  sonarr /guard/sonarr-grab-hook.sh
```

Never put a real `sonarr_episodefile_id` in a test. That is the id the script
deletes.

## The hook is not firing

**Nothing at all in the log for `[hook]`.**

Check the connection exists and is bound to the right events:

```bash
python3 ~/sonarr-guard/lib/setup-connections.py
```

That is idempotent. It reports `already correct`, or repairs the connection.

Then check the script is visible inside the container. The path in the connection
is `/guard/sonarr-grab-hook.sh`, which is the path **inside** the container, not
the host path.

```bash
docker exec sonarr test -x /guard/sonarr-grab-hook.sh && echo ok
docker inspect sonarr --format '{{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}'
```

If `/guard` is not in that list, the compose patch did not apply or the container
was never recreated. Re-run `./install.sh`.

**`ERROR: cannot tell which instance this is`**

No config section has this container's key or port. Either the config was written
for a different set of containers, or you regenerated the key in the Sonarr UI.
Fix the `api_key` in the config, or delete the config and re-run `./install.sh`.

**`ERROR: cannot read /guard/sonarr-guard.conf`**

The directory is mounted but the file is not readable by the container's user.
The container runs as `PUID`, which needs to match the owner of the guard
directory.

```bash
ls -la ~/sonarr-guard/
docker exec sonarr id
```

**`ERROR: could not read the key and port`**

Sonarr's `/config/config.xml` is not where the hook expects. If your image uses a
different config path, set `SONARR_CONFIG_XML` in that container's environment.

**"Arguments are no longer supported for custom scripts"**

Sonarr 4.0.19 and later refuse any value in the Arguments field. That is expected
and nothing needs to go there. If you added one by hand in the UI, clear it.

## Sonarr keeps grabbing the same fake over and over

This is the failure mode that matters, and it has three separate causes.

**1. The monitoring pass is off.** Check:

```bash
python3 ~/sonarr-guard/sonarr_guard_common.py
```

If an instance shows `no-unmonitor`, set `unmonitor_unaired = true` for it.
Without that pass, Sonarr keeps searching for unaired episodes and you are only
ever cleaning up afterwards.

**2. The episodes are still monitored.** Confirm directly:

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py -i animation --dry-run
```

`Monitoring: would unmonitor N unaired episode(s)` means the pass has not run yet
for them. Drop `--dry-run`.

If it says `nothing unaired left to unmonitor` but Sonarr is still grabbing, the
episode may be outside `future_window_days`, or its series carries one of your
`ignore_tags`.

**3. Something is triggering redownloads.** Sonarr's **Redownload Failed**
setting, on by default under Settings > Indexers, makes it search for a
replacement every time a release is marked failed. This tool always passes
`skipRedownload=true` on queue removals for exactly that reason, so if you see a
loop, check that you are not running an old copy:

```bash
grep -c 'skipRedownload=true' ~/sonarr-guard/sonarr-grab-hook.sh   # expect 4
```

If that returns 0, re-run `./install.sh` to update the deployed scripts.

## Something got deleted that should not have

Check whether Sonarr's recycle bin has it, under **Settings > Media Management >
Recycle Bin**. If that was configured, the file is still there, because deletion
goes through Sonarr's API rather than `rm`.

Then work out why. The log line names the air date the decision was based on:

```
EARLY FILE Futurama S11E05 | air date 2026-08-24 | has not aired yet | 781.4MB | ...
```

If that air date is wrong, the problem is metadata, not the guard. Refresh the
series in Sonarr and check it on TVDB. Shows that drop a whole season at once but
are listed with weekly air dates are the usual culprit.

To stop it happening again while you sort the metadata out, tag the series in
Sonarr and add that tag to `ignore_tags`, or:

```bash
python3 sonarr-airdate-guard.py --exclude-series "That Show"
```

Always `--dry-run` first when changing anything about deletion.

## An episode never came back after it aired

The re-monitor happens on the first poller run after the air time passes, so with
the hourly cron it can take up to an hour.

Check what the tool is tracking:

```bash
python3 -c "
import json; d = json.load(open('$HOME/sonarr-guard/sonarr-guard-state.json'))
for k, v in d['instances'].items(): print(k, len(v), 'tracked')
"
```

If the state file was lost, the episodes it recorded stay unmonitored and nothing
will restore them automatically, because the tool deliberately will not re-monitor
episodes it has no record of touching. Re-monitor those by hand in the Sonarr UI.

To hand everything back at once:

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py --restore-monitoring
```

## The deep sweep takes forever

`imported_early` makes one API call per series. On a large library that is
minutes, which is why it is a weekly cron entry. Narrow it:

```bash
python3 sonarr-airdate-guard.py --files-only --scope imported_early -i anime
```

## Nothing is running on schedule

```bash
crontab -l | grep sonarr-airdate-guard
```

Cron runs with a minimal environment and no shell profile, which is why the
installed entries use absolute paths for both `python3` and the script. If you
hand-edited them, put the absolute paths back. Test the exact command from the
crontab by hand before assuming cron is at fault.

## Starting completely over

Give the monitoring back first, or every unaired episode stays unmonitored:

```bash
python3 ~/sonarr-guard/sonarr-airdate-guard.py --restore-monitoring
python3 ~/sonarr-guard/lib/setup-connections.py --remove
crontab -l | grep -v sonarr-airdate-guard.py | crontab -
rm -rf ~/sonarr-guard
```

Then remove the `- ...:/guard` line from each compose file and
`docker compose up -d` each project. Every compose file the installer touched has
a timestamped `.bak` beside it.
