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

#### As-built

`worker_panes.close` takes the blocking worker lock, finds the pane, closes it through `herdr.Herdr.close_terminal_pane`, retires the launch and returns `Closed()`. It reads no process and runs no state check. A stopped worker that does not change family resumes in its same pane. `pane_moves._move_locked` moves open and stopped idle workers and closes no pane. `start_or_resume` records the launch role, so a worker with no dispatch can move. The `interrupt` argument is gone from close, the command line, `worker.end` and `turn_end`. Every pane keeps its shell when its agent ends.

Four items stay on the close path: the terminal id match, a labelled pane that runs another staff session is not this worker's, the blocking worker lock, and the confirm read that reports "pane close is incomplete".

**Files:**
- `scripts/herdr/worker_panes.py` — close, evidence and start paths.
- `scripts/herdr/herdr.py` — `close_terminal_pane`.
- `scripts/accounts/pane_moves.py` — account moves in the same pane.

**Gotchas:** A pane started before this change keeps the old shell-exit hook and closes when its agent stops. A move then opens a new split.

**Ruled out:** A second process read after a one-second wait before close.
