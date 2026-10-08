---
description: Give the showrunner a measured ETA for this unit's current phase, built from the durations this run has already recorded, when the unit director has not stated one.
---

# ETA

The showrunner asks for this when a unit's phase ETA reads "none measured" or
its unit director has stated none. Answer in this turn with a time the records
back. This is a read of what was already measured; start no new runs, and do
not pause the phase's work.

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
4. **When the answer has an ETA, record it before answering.** Run
   `TZ=<User zone> python3 ~/.claude/scripts/delegate/progress_history.py eta --session-dir "${SESSION_DIR}" --time "<YYYY-MM-DDTHH:MM>" --earliest "<YYYY-MM-DDTHH:MM>" --latest "<YYYY-MM-DDTHH:MM>" --basis "<basis>"`
   with the ETA, range and basis in the answer. The recorder reads each time
   in the zone it runs under, so `TZ` is the production doc's **User zone**,
   and the `ETA recorded:` line it prints names that zone. If the answer has
   no range, omit both range options.
5. **Only when no remaining step has any record,** say `none measured` and name
   the step that lacks one, and when its first run will give a time.

## Answer

Send the showrunner one message, first line self-contained, with every time
in the production doc's **User zone**, converted from the recorder's machine
time (user, 2026-10-04):

`From <unit>: Phase <N> ETA: <HH:MM zone>, <P>% done (range <HH:MM>–<HH:MM>), from <basis>.`

P is the phase row's `%` in the recorder's summary.

Print the same line as this turn's update, then carry on with the phase.
