# Troubleshooting

Everything lands in `<guard dir>/sonarr-guard.log`. Start there.

```bash
tail -f ~/sonarr-guard/sonarr-guard.log
```

`[hook]` lines come from inside a container, `[poll]` from the host cron job. The
second tag is the instance section name.

## Check the basics first

```bash
python3 ~/sonarr-guard/sonarr_guard_common.py
```

Pings every instance and prints its Sonarr version:

```
Config OK: /home/you/sonarr-guard/sonarr-guard.conf
  main: http://127.0.0.1:8989 grace=3.0d mode=strict delete=unaired [enabled]
    reachable, Sonarr 4.0.19.2979
  anime: http://127.0.0.1:9779 grace=5.0d mode=lenient delete=unaired [enabled]
    reachable, Sonarr 4.0.19.2979
```

| Output | Meaning |
| --- | --- |
| `UNREACHABLE: 401` | wrong `api_key` for that section |
| `UNREACHABLE: Connection refused` | wrong port, or the container is down |
| `UNREACHABLE: Connection reset by peer` | Sonarr is still starting, wait and retry |
| `no such file` | wrong `--config` path, or the guard directory was not created |

## Testing the hook without waiting for a real grab

The hook is an ordinary script, so you can drive it directly with a synthetic
event. Replace `sonarr` with your container name and `main` with your section.

**Does it load at all?**

```bash
docker exec -e sonarr_eventtype=Test sonarr /guard/sonarr-grab-hook.sh main
```

```
Test OK. Bound to [main] on http://127.0.0.1:8989 (Sonarr 4.0.19.2979),
grace 3d, mode strict, delete_early_files=true
```

**Does it reject an unaired release?** This one has no download id, so it reports
the decision and stops before touching anything.

```bash
docker exec \
  -e sonarr_eventtype=Grab \
  -e sonarr_series_title="Fake Show" \
  -e sonarr_release_seasonnumber=21 \
  -e sonarr_release_episodenumbers=12 \
  -e sonarr_release_title="Fake.Show.S21E12.1080p.WEB-DL" \
  -e sonarr_release_episodeairdatesutc="$(date -u -d '+30 days' +%Y-%m-%dT%H:%M:%SZ)" \
  sonarr /guard/sonarr-grab-hook.sh main
```

```
EARLY Fake Show S21E12 | air date 2026-09-21 | clears in 33d 0h | Fake.Show.S21E12.1080p.WEB-DL
  -> no download id in payload, leaving it for the poller
```

**Does it allow an aired one?** Swap the date for `'-30 days'` and you should get
`ALLOW ... grace cleared`.

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
  sonarr /guard/sonarr-grab-hook.sh main
```

Never put a real `sonarr_episodefile_id` in a test. That is the id the script
deletes.

## The hook is not firing

**Nothing at all in the log for `[hook]`.**

Check the connection actually saved with On Grab and On Import ticked. Then check
the path is `/guard/sonarr-grab-hook.sh`, which is the path **inside** the
container, not the host path.

```bash
docker exec sonarr test -x /guard/sonarr-grab-hook.sh && echo ok
```

If that fails, the mount is missing. Confirm it:

```bash
docker inspect sonarr --format '{{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}'
```

If `/guard` is not there, the compose patch did not apply or the container was
never recreated. Re-run `./install.sh`.

**`ERROR: no section [xyz] in /guard/sonarr-guard.conf`**

The Arguments field does not match a section name. The message lists the valid
ones. Matching is case insensitive but nothing else is normalised, so a trailing
space will do it.

**`ERROR: cannot read /guard/sonarr-guard.conf`**

The directory is mounted but the file is not readable by the container's user.
The container runs as `PUID`, which needs to match the owner of the guard
directory.

```bash
ls -la ~/sonarr-guard/
docker exec sonarr id
```

**`ERROR: could not read the key and port`**

Sonarr's own `/config/config.xml` is not where the hook expects. If your image
uses a different config path, set `SONARR_CONFIG_XML` in that container's
environment.

## Something got deleted that should not have

Check whether Sonarr's recycle bin has it, under
**Settings > Media Management > Recycle Bin**. If that was configured, the file is
still there, because deletion goes through Sonarr's API rather than `rm`.

Then work out why. The log line names the air date the decision was based on:

```
EARLY FILE Futurama S11E05 | air date 2026-08-24 | has not aired yet | 781.4MB | ...
```

If that air date is wrong, the problem is metadata, not the guard. Refresh the
series in Sonarr, and check the show on TVDB. Shows that drop a whole season at
once but are listed with weekly air dates are the usual culprit.

To stop it happening again while you sort the metadata out:

```bash
python3 sonarr-airdate-guard.py --exclude-series "That Show"
```

Or set `delete_early_files = false` in that instance's section, which leaves the
queue guard running but stops it touching the library.

Always `--dry-run` first when changing anything about deletion.

## Sonarr keeps re-grabbing the same fake

Blocklisting is not working. The log will say:

```
  -> not blocklisted (no grab found in history), Sonarr may grab it again
```

That happens when the file was manually imported, so there is no grab record to
mark as failed. Add the release to the blocklist by hand from
**Activity > History**, or from the series' interactive search.

If you passed `--no-blocklist`, that is the cause. Do not use that flag routinely;
without it you get a grab, remove, grab, remove loop that hammers your indexer.

## The deep sweep takes forever

`imported_early` makes one API call per series. On a large library that is minutes,
which is why it is a weekly cron entry and not the hourly one. Narrow it:

```bash
python3 sonarr-airdate-guard.py --files-only --scope imported_early -i anime
```

## Nothing is running on schedule

```bash
crontab -l | grep sonarr-airdate-guard
```

Cron runs with a minimal environment and no shell profile, which is why the
installed entries use absolute paths for both `python3` and the script. If you
hand-edited them, put the absolute paths back.

Test the exact command from the crontab by hand before assuming cron is at fault.

## Starting completely over

```bash
crontab -l | grep -v sonarr-airdate-guard.py | crontab -
rm -rf ~/sonarr-guard
```

Then remove the `- ...:/guard` line from each compose file, `docker compose up -d`
each project, and delete the Custom Script connections in each Sonarr UI. Every
compose file the installer touched has a timestamped `.bak` beside it.
