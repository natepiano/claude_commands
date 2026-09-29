---
description: Give the showrunner a measured ETA for this unit's current phase, built from the durations this run has already recorded, when the unit has not stated one.
---

# ETA

The showrunner asks for this when a unit's phase ETA reads "none measured" or
it has stated none. Answer in this turn with a time the records back. This is a
read of what was already measured; start no new runs, and do not pause the
phase's work.

## Steps

1. **The recorder's band.** Run the recorder's `progress` for the current phase
   (`~/.claude/scripts/delegate/progress_history.py`). If it gives an ETA band
   calibrated from measured runs, that is the answer.
2. **Otherwise, build one.** List the steps left in this phase: repair rounds,
   lint, the full tests, reviews, the live smoke, screenshots, the checkpoint.
   Time each from the durations the recorder holds for the same step, earlier
   in this run or in this project (`timeline`, `aggregate`). Sum them, and give
   a range from the shortest and longest matching records.
3. **Name the basis.** Say which recorded steps the time comes from, e.g. "hana
   full tests took 14–19 min in phases 10–11".
4. **Only when no remaining step has any record,** say `none measured` and name
   the step that lacks one, and when its first run will give a time.

## Answer

Send the showrunner one message, first line self-contained, with the time in
the machine's zone:

`From <unit>: Phase <N> ETA: <HH:MM zone> (range <HH:MM>–<HH:MM>), from <basis>.`

Print the same line as this turn's update, then carry on with the phase.
