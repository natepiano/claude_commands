# Stall watch and pane workers

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Stall watch does not bump a unit director while one of its pane workers is busy.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md` (stall watch)

## Source

sdlc [63b1adde] 2026-11-09. Since panes 34, a dispatch returns at once and no director shell waits on the worker. Stall watch then bumped unit directors while their pane workers ran: at 03:39 and 04:05 EST it sent "idle since 04:05 EST, nothing running; bumped" while panes waited on workers hook34 (34b) and impl7 (41).

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T09:13:26.939+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Layout:** `scripts/production/` (stall watch), `scripts/herdr/` (pane worker state).
- **Key files:**
  - `scripts/production/stall_watch.py` — the stall watch: one cycle reads processes, staff and panes, and decides for each unit director `waiting`, `running`, `fresh`, `holding` or an idle bump
  - `scripts/production/test_stall_watch.py` — its tests
  - `scripts/herdr/worker_panes.py` — pane worker state: `observe_parents(parents, record)` and `worker_state(observed)`
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_stall_watch`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, live herdr panes).
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.
  - One place for each thing: the worker busy rule stays in `worker_panes.worker_state`; no copy of it.

## Phases

### Phase 1 — Stall watch counts a busy pane worker as running work  · status: done

#### Work Order

**Goal:** stall watch reports a unit director as `running`, and sends no idle bump, while a pane worker of that director is busy.

**Spec:**
1. Cause: `work_running(pid, rows, session_id)` (`scripts/production/stall_watch.py:313`) finds work only in the director's process descendants and in processes with the director's native session id. A pane worker runs in its own herdr pane with its own session, so it is neither.
2. Fix: in the per-unit decision (`stall_watch.py`, the `running_work = work_running(pid, rows, session_id)` line near 1364), `running_work` is also true when a pane worker whose staff parent is the unit director's staff row is busy. Busy means `worker_panes.worker_state(observed) == "busy"`. Use `worker_panes.observe_parents(parents, record)` with the cycle's `staff.StaffSnapshot` (`record`). Add no second busy rule.
3. One pane read per cycle, and only when needed: read the panes at most once in a cycle, the first time a unit has no process work. Units that need no read cause no read.
4. A failed pane read counts as no pane work, so the cycle keeps its current behavior. `worker_panes` reports a failed read itself: one `anomaly:` line per worker, and each worker reads as unknown, which is not busy. Only an error that reaches the stall watch (such as an unreadable shared-session file) prints one `stall-watch:` stderr line.
5. The rest of the decision stays: a `running` result resets the idle stretch as it does for process work today.

**Files:**
- `scripts/production/stall_watch.py` — the pane worker check and its use in the per-unit decision.
- `scripts/production/test_stall_watch.py` — table rows for the new cases.

**Workers:** `1 writer + 1 tester`.
- `impl` — `scripts/production/stall_watch.py`.
- `test` — `scripts/production/test_stall_watch.py`: add rows to the existing test of the per-unit decision (running, fresh, idle bump), from the Spec alone. Rows: a director with a busy pane worker and no process work gives `running` and no bump; a director with only an idle pane worker gets the idle bump as today. Fake the pane read at the `worker_panes` boundary; read no live pane.

**State:** none.

**Constraints from prior phases:** none. Run test modules alone; tests run as `scripts/lib/py -m unittest scripts.production.test_stall_watch` from the worktree root (the worktree launcher, not `~/.claude/scripts/lib/py`, which imports the installed modules). Each entry file loads `scripts/lib/package_root.py` through its 4-line `try` block; imports are package-qualified (`from scripts.herdr import worker_panes`).

**Acceptance gate:**
1. `scripts/lib/py -m unittest scripts.production.test_stall_watch` passes, with the new rows.
2. `basedpyright scripts/production/stall_watch.py scripts/production/test_stall_watch.py` gives `0 errors, 0 warnings, 0 notes`.
3. The director's merge gate (`merge_gate.py --tests-only`) and `basedpyright scripts/direct scripts/production` pass before the hash.
