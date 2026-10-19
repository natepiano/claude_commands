# Unit rename (as built)

`/showrunner:rename_unit <old> <new>` (`commands/showrunner/rename_unit.md`) renames one natedev unit
of a production. `/unit:rename` forwards to it. The script is `scripts/production/rename_unit.py`,
with three actions: `plan`, `apply` and `verify`.

## Names

- The unit id (`<stem>-unit`) is the key. The branch `<slug>-<stem>` and the worktree are built from
  the stem.
- A session name is a label: the rename changes it only while it is still add_unit's default.
- Rule: store only what cannot be looked up, and never rewrite history.
  - Merge subjects, logs, transcripts and plan prose keep the old id.
  - `merge_checkpoint.merge_branch_history` follows the commit `chore(<slug>): rename unit <old> to
    <new>`, so merges made before the rename still count under the new id.
- Work Orders read the worktree and branch from git, so the rename does not edit plan prose.

## apply

apply works under the production lock in `unit_move`, which `rename.py` also takes. It saves the
journal `rename_unit.json` before changing anything, and checks the showrunner checkout before
each step. The preflight, each step's checks, and every step except stop, relaunch and verify
(`Step.locked`) run under the merge lock, `merge_checkpoint.merge_lock(checkout, slug)`: an flock
on `<git common dir>/showrunner-merge-<slug>.lock`, which `merge_checkpoint.py` holds for a
whole gate run, unit checkpoints and `--from-child` hand-offs alike. A rename that finds a gate
running prints `merge lock: waiting — …` and waits, and a gate waits for the step. The steps that
wait do not hold the lock, so no gate waits behind a stop. Every step except the first and the last also checks that the unit is stopped. The
steps:

1. **Stop.** A running unit is not a refusal: plan lists this step, and apply stops the unit
   through `shutdown.py down --only <session>`. The unit's id is `SHOWRUNNER_UNIT_ID` in the
   environment of its Claude process in its herdr pane, set at launch, and a live process cannot
   change it, so every rename is a relaunch. apply then waits for the stop (`_await_stopped`):
   it reads the unit's shutdown record every `POLL_SECONDS` (1 s) and prints `stop: waiting —
   <unit> is <progress>` on each change. It goes on when the record reads stopped, and refuses when
   the record asks for a restart, reads something else, or was never written (`RECORD_WAIT_SECONDS`,
   120 s). It exits 2 only past `STOP_WAIT_SECONDS` (`RENAME_UNIT_STOP_WAIT`, default 3600), and a
   rerun resumes there. Before 2026-10-18 apply always exited 2 here and nothing reran it, so a
   rename stopped its unit and stayed at step 0 until someone ran it again (user: "it is
   unacceptable that it require my intervention"). Refused up front: the new id already running, a Claude that cannot be read, and
   processes inside the worktree of a unit with no Claude to stop.
2. **Merge.** `origin/<merge branch>` is merged into the unit in the old worktree. While the unit's
   work is in progress (`_in_progress`: an open phase in its run record, or `git status --porcelain`
   output), nothing is merged: a failed merge's `--abort` can lose uncommitted work. The journal's
   `kept` holds the reason, `merged` is the old tip, and step 6 takes that tip as `unit_tip`. The
   unit takes both merges at its own next merge of the merge branch (user, 2026-10-18: a rename
   works at any time, mid-phase too; the plan's refusals of an open phase and of a checkpoint not
   merged are gone).
3. **Git.** The new branch is pushed and the worktree moved, which repairs it and copies
   `cargoBerthTarget`.
4. **Claude.** Transcript folders move, and the trust entry is copied in every config dir.
5. **Doc commit.** The commit edits:
   - the Unit, Worktree and Branch cells;
   - the Hub Owner and Other cells;
   - the Waiting and Waits-on cells of open Gates;
   - the `> **Production:**` header of the unit's live plans.

   The journal records the commit's hash, so a retry only pushes it.
6. **Merge rename.** The unit branch takes the rename commit.
7. **State.** These move to the new id:
   - the six showrunner state files (`rename_state.SCRATCH_FILES`) in `showrunners.session_state_dir(<slug>)`, or in the `--scratch` directory when one is given;
   - the stall-watch stretch;
   - the reviewer leases and memory;
   - the newest run record and the recorder state;
   - this production's open proposal issues and phase notes (`phase_table.owned_notes`);
   - `fix.conf` redirects.
8. **Retire.** The old branch is deleted once origin holds the new tip.
9. **Restart record.** The fields that move:
   - `host.unit`;
   - the cwd, plan path and worker cwds under the worktree;
   - the Claude and herdr names, while they are still the defaults;
   - `renamed`, which `restart.restart_note` puts first in the resumed prompt. With `kept` set it
     also says the branch took no merge and the paused Codex seats now work in the new worktree.
     `implement.sh` passes `--cwd <working dir>` on every follow-up, so a resumed seat's turns run
     there and not in the path its thread started in.

   `restart._unit_argv` still reads the plan from the Units row.
10. **Relaunch.** `shutdown.py restart --only <session> --here` brings the unit back. apply then
    waits for the new id to carry a live Claude mark (`_await_live`, `LIVE_WAIT_SECONDS`,
    `RENAME_UNIT_LIVE_WAIT`, default 180), so verify does not run before the unit is up. The rename
    claim (`rename_state.rename_claim`) refuses add_unit for both ids until step 9 is done.
11. **Verify** (below).

A unit that was not running has no record, so steps 9 and 10 do nothing for it.

## verify

- **Live places.** These must hold the new names:
  - the Units row;
  - the doc cells and plan headers (apply's own edit would change nothing);
  - the branches and worktree;
  - the transcript folders, the stall stretch and the newest run;
  - proposals, phase notes and `fix.conf`;
  - the shutdown record and the live session's mark.
- **Control.** The old id must still be in the merge-branch subjects. If the matcher finds nothing
  in history, it is broken.
- **Sweep.** `unit_rename_sweep.sweep` runs `rg -F` for the old id, branch and worktree path, then
  checks each hit as a whole token. It covers:
  - the state root;
  - every `~/.claude*` config dir;
  - the showrunner checkout and the new worktree, as git sees them;
  - the six showrunner state files.

  A hit outside the history classes fails and names its file. Shutdown paths print as `(masked)`.
  On natedev a sweep takes about 80 s.
