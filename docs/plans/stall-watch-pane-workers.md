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

#### As-built

- In the per-unit decision of `stall_watch.watch_pass`, a unit director with no process work is `running`, and gets no idle bump, when a pane worker whose staff parent is the director is busy. Busy is `worker_panes.worker_state(observed) == "busy"`; there is no second busy rule. A `running` result resets the idle stretch, as process work does.
- `read_busy_pane_worker_parents(parents: tuple[staff.StaffRow, ...], record: staff.StaffSnapshot) -> BusyPaneWorkerParents` makes one `worker_panes.observe_parents` read for all director rows of the cycle. The cycle holds `PaneWorkersUnread` until the first unit without process work needs the answer, then reuses it. It skips the read when no watched director has staff workers.
- A failed pane read counts as no pane work. `worker_panes` reports it with its own `anomaly:` lines and unknown workers; the stall watch prints one `stall-watch: pane workers:` line only for an error that reaches it, such as an unreadable shared-session file.

**Files:**
- `scripts/production/stall_watch.py` — the pane worker check and its use in the per-unit decision
- `scripts/production/test_stall_watch.py` — busy and idle pane rows in `test_running_shell_or_worker_launcher_prevents_a_bump`

**Gotchas:** an existing test counts shared-session pane reads per cycle; a read for directors with no workers breaks it.

**Ruled out:** changing `worker_panes` to raise a failed read to the caller — the fix is a `worker_panes` call only.
