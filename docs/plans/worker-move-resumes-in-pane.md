# A done worker's pane closes at once, and an account move keeps the pane

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Requested on 2026-11-09 by the sdlc showrunner, with the user's choices.

Written by panes-unit on 2026-11-09 for the sdlc showrunner.

## The request

1. The user, 2026-11-09 (via sdlc), on the pane-close question: "if we have a reason to close - why do we not close without checking?" sdlc's reading, under the user's rule that a broken guard is deleted: add no second read. Delete the "other work" process check from the worker pane close. A close happens only when a worker is done (the Stop hook after DONE). That close closes the pane at once. Any check that stays is named to sdlc with its reason.
2. The user, 2026-11-09 (via sdlc): "yes - i like the recommended mechanism - so we close workers only when they're done". An account move stops the worker and resumes it on the new account in its same pane. The move closes no pane and runs no close check. Add no new guard or check.

## Cause of the change

- `scripts/herdr/worker_panes.py` `close`: before it closes, it runs `can_close` / `_idle_checks` (agent state, transcript DONE line, pass state, dispatch lock, live detached run), `_stop_row` (a graceful agent quit), a terminal-busy read, and `herdr.Herdr.close_tab_confirmed`, whose `check_target` refuses with "pane runs other work" while a shell setup step (direnv) runs.
- `scripts/herdr/worker_panes.py` `_parent_evidence`: a labelled pane that is not at the shell gives `UncertainWorker("labelled recovery pane runs other work")`.
- `scripts/accounts/pane_moves.py` `_move_locked`: a worker with no active dispatch is closed through `worker_panes.close`, not moved.
- `scripts/herdr/worker_panes.py` `_start_locked`: a `StoppedWorker` that does not switch family has its pane closed, and the resume opens a new split pane.

## Delegation Context

- **Project:** `~/.claude` scripts. Python 3.11+ through `scripts/lib/py`, `unittest`, basedpyright.
- **Project started:** 2026-11-09T14:30:00+00:00
- **Files:** `scripts/herdr/worker_panes.py`, `scripts/herdr/herdr.py` (`close_tab_confirmed`, `_close_pane`, `_pane_closed`), `scripts/accounts/pane_moves.py`, `scripts/production/worker.py` (`end`), `scripts/herdr/turn_end.py` (`_finish_completion`), `scripts/herdr/test_worker_panes.py`.

## Phases

### Phase 1 — A done worker's pane closes at once, and an account move keeps the pane  · status: done

#### Work Order

**Goal:** `worker_panes.close` closes the worker's pane at once, with no state or process check. An account move stops the worker and resumes it on the new account in the same pane.

**Spec:**
1. `worker_panes.close(parent, name)`: find the worker's pane (the pane that runs the row's session, else the labelled pane in the parent's tab that no other staff session owns). Close that pane through `_close_pane`, confirm with `_pane_closed`, call `_retire_launch`, and return `Closed()`. A row with no pane returns `Closed()` as now.
2. Delete these from the close path: `can_close`, `_idle_checks`, `_completed_turn`, `_stop_row`, the "terminal became busy before close" read, the "worker pane is outside its parent's tabs" refusal, and the `check_target` process read (do not call `close_tab_confirmed`). Delete functions that have no caller after this.
3. Keep these, and name them in the as-built: (a) the pane's terminal id matches the terminal the evidence read, so a reused pane id never closes another pane; (b) a labelled pane that runs another staff row's session is not this worker's pane; (c) the worker lock, taken blocking; (d) `_pane_closed` confirms the close, and a pane that stays reports "pane close is incomplete".
4. Remove the `interrupt` argument: from `close`, the `close --interrupt` CLI flag, `worker.end`, and `turn_end`. Every close is now the same close.
5. `_parent_evidence`: a labelled pane in the parent's tab that no other staff session owns is a `StoppedWorker`, whatever its foreground process. Delete the "labelled recovery pane runs other work" state and the matching branch in `_close_target`.
6. `_start_locked`: for a `StoppedWorker` that does not switch family, do not close its pane. Keep `_retire_launch`. Launch into that pane as `herdr.ExistingPane(current.pane, current.terminal)`, with no `pane_activity` read for this pane.
7. `scripts/accounts/pane_moves.py` `_move_locked`: remove the branch that closes an `OpenWorker` or `StoppedWorker` with no active dispatch. Such a worker moves through `_resume_destination`. A `ClosedWorker` with no active dispatch still returns "nothing to move".
8. Add no new read, wait, retry, refusal or check.

**Files:**
- `scripts/herdr/worker_panes.py` — `close`, `_parent_evidence`, `_close_target`, `_start_locked`, the CLI.
- `scripts/accounts/pane_moves.py` — `_move_locked`.
- `scripts/production/worker.py` — `end` without `interrupt`.
- `scripts/herdr/turn_end.py` — the close and end calls without `interrupt`.
- `scripts/herdr/test_worker_panes.py`, `scripts/herdr/test_turn_end.py`, `scripts/production/test_worker.py` — delete the rows for deleted refusals; change the rows of the existing close, move and resume tests: a busy or setup-step pane closes at once; a move resumes in the same pane; an idle open worker moves and keeps its pane.
- `docs/as-built/herdr-adoption.md` — correct the close and move text; list the kept items of Spec 3.
- Each doc or command file that tells a caller to use `close --interrupt`.

**Workers:** `1 writer` — one module's close and start paths, with their callers.
- `impl` — all files above.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** basedpyright 0/0/0 on the changed files; `scripts.herdr.test_worker_panes`, `scripts.herdr.test_turn_end` and `scripts.production.test_worker` green; the targeted test green.
