# Stall watch and pane workers

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Stall watch does not bump a unit director while one of its pane workers is busy or a detached run of one of its workers is live.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md` (stall watch)

## Source

Phase 2: sdlc [cd056195] 2026-11-09. Hana reported that stall-watch bumped fastest-unit every 8 to 10 minutes from 03:54 EST. Its Phase 53 Codex worker had ended its turn and waited on its own live detached test run. `worker_panes.worker_state` reads only the herdr agent status, so that worker reads idle.

Phase 1: sdlc [63b1adde] 2026-11-09. Since panes 34, a dispatch returns at once and no director shell waits on the worker. Stall watch then bumped unit directors while their pane workers ran: at 03:39 and 04:05 EST it sent "idle since 04:05 EST, nothing running; bumped" while panes waited on workers hook34 (34b) and impl7 (41).

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

### Phase 2 — Stall watch counts a live detached run of a worker as running work  · status: done

**Depends on:** Phase 1.

#### Work Order

**Goal:** stall watch reports a unit director as `running`, and sends no idle bump, while a detached run of one of that director's workers is live.

**Spec:**
1. Cause: a worker that starts a long command through `detached_runs.py start` ends its turn and waits for the result. Its pane then reads idle, so `worker_panes.worker_state` is not `busy`. The run's supervisor is not a descendant of the director and does not carry the director's session id, so `work_running` does not find it.
2. Fix: in the per-unit decision, `running_work` is also true when a worker whose staff parent is the unit director's staff row has a live detached run. Live means `detached_runs.live_runs(<run folder>/detached_runs, <worker staff name>)` returns a record. This is the call `worker_panes._idle_checks` uses to keep a worker pane open. Add no second liveness rule.
3. The run folder is the director's unfinished held run folder, which the per-unit decision already finds through `runs.unfinished_held_run(row.current_session)`. `unit_lookup.HeldRun` carries that folder as `folder: Path`, because a held folder can be a verified marker target outside `unit_runs.run_roots()`; a folder rebuilt from the history name misses it. Do not scan the run folders again for each unit. Do not change `unit_runs`, `worker_panes` or `detached_runs`.
4. Check the detached runs only when the unit has no process work and no busy pane worker. A unit with no staff workers causes no read.
5. A failed read (an unreadable records folder or record) counts as no detached work. It prints one `stall-watch: detached runs:` stderr line with the unit name, and the cycle continues.
6. The rest of the decision stays: a `running` result resets the idle stretch as process work and pane work do.

**Files:**
- `scripts/production/stall_watch.py` — the detached run check and its use in the per-unit decision.
- `scripts/production/unit_lookup.py` — `HeldRun.folder`.
- `scripts/production/test_stall_watch.py` — table rows for the new cases.

**Workers:** `1 writer + 1 tester`.
- `impl` — `scripts/production/stall_watch.py`, `scripts/production/unit_lookup.py`.
- `test` — `scripts/production/test_stall_watch.py`: add rows to the existing case table in `test_running_shell_or_worker_launcher_prevents_a_bump`, from the Spec alone. Rows: a director whose idle pane worker has a live detached run gives `running` and no bump; a director whose worker has only a finished detached run record gets the idle bump as today. Fake the detached run read at the `detached_runs.live_runs` boundary; read no live state.

**State:** none.

**Constraints from prior phases:** Phase 1 reads the panes once per cycle through `read_busy_pane_worker_parents` and skips the read when no watched director has staff workers. An existing test counts shared-session pane reads per cycle; keep that count. Run tests as `scripts/lib/py -m unittest scripts.production.test_stall_watch` from the worktree root. Imports are package-qualified.

**Acceptance gate:**
1. `scripts/lib/py -m unittest scripts.production.test_stall_watch` passes, with the new rows.
2. `basedpyright scripts/production/stall_watch.py scripts/production/test_stall_watch.py` gives `0 errors, 0 warnings, 0 notes`.
3. The director's merge gate (`merge_gate.py --tests-only`) and `basedpyright scripts/direct scripts/production` pass before the hash.
