---
description: Rename one unit of this production. It stops the unit, moves every place that names it, relaunches it and verifies the result.
argument-hint: "<old> <new>"
---

# Rename unit

Run from the showrunner session, at any time, also in the middle of a phase. `<old>` is the unit's id, its stem, or its Claude session's name. When the id already is `<new>`, only the session takes the new name: the plan shows the other steps as already matching.
`<new>` can be an id or a stem, and it becomes `<stem>-unit`. A stem starting with `<slug>-`
keeps that prefix once. If it equals the old branch or worktree folder name, that path stays:
`names-unit` → `cargo-liner-style-names-unit` keeps `cargo-liner-style-names` for both.
The script lives at
`~/.claude/scripts/production/rename_unit.py`. It finds the showrunner state in `STATE_DIR`:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename_unit.py" <plan|apply|verify> <slug> <old> <new>
```

1. Run `plan`. It writes nothing, and it either refuses or prints every step with its old and new values.
   It refuses when:
   - the new name is taken, or a session already runs under it;
   - the unit is a reserved one;
   - the unit is on the Mac.

   A running unit is not a refusal: apply stops it itself, at step 1. The stop is at once: it does
   not wait for the director or its Codex workers to go idle, and a worker in the middle of a turn
   pauses there. The unit's commits that are not merged move with the branch.
2. Run `apply` once, in the background: one run does the whole rename, with no rerun. It waits for the
   unit to stop (up to an hour), for a merge gate that is running, and for the relaunched unit to come
   up. It journals each step, so a rerun after a refusal resumes from the step that stopped. The steps are:
   1. stop the unit through the shutdown path (`down`, then `now`), then pause its runs' workers
      (`codex_mesh.py stop --pause`) and SIGTERM the processes left in the worktree, except a paused
      worker's detached run. While such a run works inside the worktree, apply refuses: `detached run
      <id> of <worker> works inside <worktree>; rerun apply once it ends`;
   2. merge the merge branch into the unit, except while its work is in progress (an open phase or
      uncommitted work): then the branch keeps its tip, steps 2 and 6 merge nothing, and the unit
      takes the merge branch and the rename commit at its own next merge;
   3. move the branch and the worktree where their names change;
   4. move the Claude transcripts and trust;
   5. update the Units row and plan Production header, then commit
      `chore(<slug>): rename unit <old> to <new>`; style plans keep their Plan cell filenames,
      and git-excluded style docs use `--allow-empty`;
   6. merge that commit into the unit;
   7. move the unit's state; style productions also move current ledger tickets, sync waits and
      open proposal issues to the new id, keeping ticket ids and history;
   8. retire the old branch when its name changed;
   9. point the unit's shutdown record at the new names, also each paused Codex worker's launcher
      folder, and add the new worktree path to each worker's resume prompt;
   10. relaunch the unit; it resumes each Codex worker's own thread in the new worktree;
   11. verify.
   - **Exit 2** means the unit was still stopping after an hour. Find out why its stop holds, then rerun `apply`.
   - **Exit 1** names the step that refused. Fix the cause and rerun `apply`.
3. Verify fails when a live place still holds an old name, when a process still works in a moved
   worktree path, or when a file nothing classifies holds an old name.
   A failure leaves the journal open. A stale live place is a bug in the rename. A file nothing
   classifies is a place to add to `unit_rename_sweep.py`: history or live. Then run `verify` alone.
4. When the rename is done, the unit restarts. Its first prompt gives its new id, worktree, branch and plan,
   and each Codex worker's resume prompt gives the new worktree. A moved worktree leaves no link or copy at the old path.
   Tell the user the new name in one line.
