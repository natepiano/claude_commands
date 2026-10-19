---
description: Rename one unit of this production. It stops the unit, moves every place that names it, relaunches it and verifies the result.
argument-hint: "<old> <new>"
---

# Rename unit

Run from the showrunner session, at any time, also in the middle of a phase. `<old>` is the unit's id or its stem.
`<new>` can be an id or a stem, and it becomes `<stem>-unit`. The script lives at
`~/.claude/scripts/production/rename_unit.py`. It finds the showrunner state in `STATE_DIR`:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename_unit.py" <plan|apply|verify> <slug> <old> <new>
```

1. Run `plan`. It writes nothing, and it either refuses or prints every step with its old and new values.
   It refuses when:
   - the new name is taken, or a session already runs under it;
   - the unit is a reserved one;
   - the unit is on the Mac.

   A running unit is not a refusal: apply stops it itself, at step 1. The stop pauses its Codex
   seats, and the unit's commits that are not merged move with the branch.
2. Run `apply` once, in the background: one run does the whole rename, with no rerun. It waits for the
   unit to stop (up to an hour), for a merge gate that is running, and for the relaunched unit to come
   up. It journals each step, so a rerun after a refusal resumes from the step that stopped. The steps are:
   1. stop the unit through the shutdown path;
   2. merge the merge branch into the unit, except while its work is in progress (an open phase or
      uncommitted work): then the branch keeps its tip, steps 2 and 6 merge nothing, and the unit
      takes the merge branch and the rename commit at its own next merge;
   3. move the branch and the worktree;
   4. move the Claude transcripts and trust;
   5. commit the doc change `chore(<slug>): rename unit <old> to <new>`;
   6. merge that commit into the unit;
   7. move the unit's state;
   8. retire the old branch;
   9. point the unit's shutdown record at the new names;
   10. relaunch the unit;
   11. verify.
   - **Exit 2** means the unit was still stopping after an hour. Find out why its stop holds, then rerun `apply`.
   - **Exit 1** names the step that refused. Fix the cause and rerun `apply`.
3. Verify fails when a live place still holds an old name, or when a file nothing classifies holds one.
   A failure leaves the journal open. A stale live place is a bug in the rename. A file nothing
   classifies is a place to add to `unit_rename_sweep.py`: history or live. Then run `verify` alone.
4. When the rename is done, the unit restarts. Its first prompt gives its new id, worktree, branch and plan.
   Tell the user the new name in one line.
