---
description: Bump urgent work so it is delivered now and validated later - top priority, a short checkpoint, first in the merge queue, a demo build, then the full suite on the pushed tip.
argument-hint: "<unit> <what the user wants now>"
---

# Bump

Run in the showrunner session when the user calls a piece of work urgent: they
want to use it now, and the full validation can come after. User, 2026-10-22,
after trunk P140 (the 3D group box) waited days behind a flake fix and the
merge queue.

`<unit>` owns the work. If no unit owns it yet, give it to the unit whose files
it changes, and say so in one line.

## Steps

1. **Top priority at once.** Make the work the whole production's top priority.
   Log it in `LOG` as `bump: <unit> <what>`. Tell every other unit director in
   one line: the bumped unit's merge goes first, and a build hold may come. Do
   not wait for the next update tick.
2. **Checkpoint now.** Tell the owning unit director: "Bump: checkpoint and
   push this work now. Run lint and the tests directly related to the change
   only, not the full suite. The full suite runs on the pushed tip after the
   merge, and its fixes come as follow-up checkpoints." The unit sends its
   normal checkpoint notice, with the line `bump: lint and related tests only`.
3. **First in the merge queue.** Take the bumped notice before every other
   notice that waits.
   - When the production has a merge unit (hana: `merge-gate`), tell it in one
     line to merge this checkpoint next, before its queue.
   - Otherwise run <MergeCheckpoint/> (`/showrunner:produce`) with `--bump`
     for it yourself now.

   After the merge and push, start the demo build: the Mac run of
   <CIPoint/> on the pushed sha, or the build the production's rules name. If
   natedev's free memory is too short for it, first take `/build:hold
   bump: demo build of <unit>`, and release it when the build ends.
4. **Full suite after.** `--bump` starts the checkpoint gate on the pushed
   tip in the background, in a detached clone. Red goes to the owning unit
   and the showrunner with the failed step, tip and log; green goes to the
   showrunner. With a merge unit, use its full run. Keep the work top priority
   until the tip is green; red fixes come as follow-up checkpoints.
5. **Tell the user.** When the demo build is installed, tell the user what to
   run and where, in one line, and send a `note` (<Notify/>). Then log
   `bump done: <unit>` once the tip is green, and return to the normal order.

## Rules

- A bump never skips the Change-Review line, the overlap check with other
  units, or the design check of <MergeCheckpoint/>. It skips only the full
  suite before the merge.
- One bump at a time. A second urgent request waits for the first one's demo
  build. Tell the user its place in one line.
