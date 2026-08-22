# Feature request for Sonarr: cleaning up early imports

Filed as [Sonarr#8896](https://github.com/Sonarr/Sonarr/issues/8896) and closed as
`not_planned`. Kept here as a record of what was asked and what was decided.

**The decision**, from markus101:

> This is better as an external script that uses the API to determine which
> episodes should be cleaned up, instead of a feature built into Sonarr itself.
>
> No plans to add or support this functionality directly in Sonarr.

That settles it: cleaning up early imports is out of scope for Sonarr, and this
tool is the intended shape of the solution. The grab side still becomes native in
v5 via `AirDateSpecification`; the cleanup and monitoring sides stay here.

---

## What was filed

---

### Is there an existing issue for this?

- [x] I have searched the existing open and closed issues

Closest existing issues, none of which cover this:

- #969 *Prevent Grabbing if Episode Hasn't aired*, closed as completed. Implemented
  on `v5-develop` as `AirDateSpecification`, using the new `AirDateRestriction` and
  `AirDateGracePeriod` release profile fields. That handles the **grab** decision.
  This request is about files that are **already in the library**.
- #5312 *Option to exclude episodes before/(or without) their release date from RSS*,
  closed as not planned, superseded by the work in #969.
- #6108 *Blocklist does not block GrabID*, related but a different mechanism.

### Is your feature request related to a problem? Please describe

`AirDateSpecification` in v5 stops new early grabs, which is excellent. It does
nothing about the files an instance already imported before that protection
existed, and there is no way to ask Sonarr to reassess its library against air
dates.

Those files are actively harmful rather than merely junk. Sonarr sees the episode
as present, so it never searches for the real release when it airs. The user does
not find out until they sit down to watch and the file is a fake, a mislabelled
foreign release, or a `.exe` in a video container.

A concrete example from a library of mine, one series, one season:

```
S11E01  air=2026-08-03  imported=2026-08-19   legitimate
S11E02  air=2026-08-03  imported=2026-08-13   legitimate
S11E03  air=2026-08-10  imported=2026-08-13   legitimate
S11E04  air=2026-08-17  imported=2026-08-17   legitimate
S11E05  air=2026-08-24  imported=2026-07-21   imported a month before air
S11E06  air=2026-08-31  imported=2026-07-21   imported a month before air
S11E07  air=2026-09-07  imported=2026-07-21   imported 7 weeks before air
S11E08  air=2026-09-14  imported=2026-07-21   imported 8 weeks before air
S11E09  air=2026-09-21  imported=2026-07-21   imported 9 weeks before air
```

Episodes 5 to 9 arrived in a single batch on one afternoon, before episode 1 had
even aired. The information needed to identify every one of them is already in
Sonarr's database: `EpisodeFile.DateAdded` against `Episode.AirDateUtc`.

Enabling the v5 setting on an instance in this state protects it going forward but
leaves the existing damage in place, and the user has no way to find it short of
querying the API themselves.

### Describe the solution you'd like

A housekeeping task that flags, and optionally removes, episode files whose
`DateAdded` precedes their episode's `AirDateUtc`.

Two settings would cover it, ideally alongside the existing air date fields:

1. **Report early imports** (default on). Surface them, most likely as a Health
   Check or a filter in Wanted, listing episode, air date, import date, and the
   release that produced it. Reporting alone would be a large improvement, because
   the user currently has no way to see this at all.
2. **Delete early imports** (default off). Delete the file, honouring the recycle
   bin, and mark the episode missing so it is grabbed properly once it airs.

Reusing `AirDateGracePeriod` from the release profile would keep the semantics
consistent, so a file imported within the grace window is not flagged. Web
releases legitimately land a few hours ahead of the recorded air time in some
regions, so the tolerance matters.

Scoping it to a manual command, similar to an existing rescan, rather than
something automatic would be entirely reasonable for a first pass.

### Describe alternatives you've considered

**Doing it outside Sonarr.** This is what I currently do, walking
`/api/v3/episode?seriesId=N&includeEpisodeFile=true` per series and comparing
`episodeFile.dateAdded` to `airDateUtc`, then calling
`DELETE /api/v3/episodefile/{id}`. It works, but it costs one API call per series
for information Sonarr already has indexed, and every user who hits this has to
rediscover the same query.

**Deleting and re-searching by hand.** Only viable if you already know which
episodes are affected, which is the part that is hard.

**Treating it as a blocklist problem.** It is not. Blocklists match release names,
and the same content reappears moments later under a slightly different one, so
the file comes back even when the original release is blocked.

**Waiting for the episode to air and letting an upgrade replace it.** Does not
happen, because Sonarr considers the episode complete and stops looking.

### Anything else?

The relevant fields are already present and indexed, so the detection is a
comparison rather than new bookkeeping:

- `Episode.AirDateUtc`
- `EpisodeFile.DateAdded`

One related observation while implementing the external version, in case it is
useful. Marking a release failed to blocklist it triggers a redownload when
**Redownload Failed** is enabled, which is the default. For an unaired episode the
replacement is simply the next fake, so cleanup tooling that blocklists without
passing `skipRedownload=true` produces a grab, block, search, grab loop. I
measured a new fake roughly every 30 to 40 seconds on one instance until I passed
that flag. `POST /api/v3/history/failed/{id}` has no equivalent parameter, which
is worth knowing for anyone building on this.
