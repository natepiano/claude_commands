# Units view: Workers reads the same run as Notes

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** In `/showrunner:units`, the Workers cell reads the same run as Notes, so a finished side run never hides the workers of the open run.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user asked through the cargo-liner showrunner. The sdlc showrunner sent the fault on 2026-11-13 at 11:30 EST:

1. On main, `unit_phases.py` observes worker rows against `newest.get(member.id)`, the unit's newest run by start time. Notes uses `open_unit_runs`.
2. Port: the newest run `20261112T225040Z-351564` is a finished astra-review run. The open run `386d8d90-e9fc-4c2c-bc14-c3db0779daf3` has `port-test` and `port-impl` waiting on runs. Workers shows `—`.
3. Tile: the newest run `20261111T094446Z-1541089` is a finished astra-review run. The open run `02717738-2a97-4a2f-b362-e4add2f2dd8e` has 3 workers waiting and 1 working. Workers shows `—`.
4. Handler is correct: its newest run is its open run.

The director's live check at 11:31 EST: the `natedev-unit-board` branch, with the units-table-wrap phase 2 checkpoint `2c60d4da1`, already shows the port and tile workers. That phase makes `newest_unit_runs` prefer the staff row's current run. It has not landed on main.

The director's run scan at 11:35 EST: 9 units have open runs that are not the staff current run. Most are old astra-review runs, proof runs and stopped runs (for example `panes` has 5, `fastest` has 2). So only the staff current run supplies rows and workers.

## Delegation Context

- **Project:** the `~/.claude` scripts — the `/showrunner:units` view (`scripts/production/unit_phases.py`).
- **Project started:** 2026-11-13T16:33:00.602+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`.
- **Layout:**
  - `scripts/production/unit_phases.py` — `newest_unit_runs()` selects one run per unit and prefers the staff current run. `open_unit_runs()` reads the open phases of that run. `table()` builds the rows. Open phase rows call `worker_runs.phase_workers(session_directory(newest[member.id]), …)`. Rows without an open phase call `worker_runs.observe_pane_rows(session_directory(run), …)`.
  - `scripts/direct/worker_runs.py` — `phase_workers`, `observe_pane_rows`.
  - `scripts/production/test_unit_phases.py` — table tests with fixture staff rows and run files.
  - `commands/showrunner/units.md` and the "Unit phases" entry in `docs/as-built/showrunner-automation.md` — the row and cell rules.
- **Test lane:** `scripts.production.test_unit_phases`, `scripts.direct.test_worker_runs`.
- **Build:** `basedpyright` on each changed Python file must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - The style production table (`style_units.py`) does not change.
  - Cell content rules other than the Workers source do not change.
  - Add no guard, refusal or check that this plan does not ask for.

## Phases

### Phase 1 — Workers reads the same run as Notes  · status: done

#### As-built

- `table()` in `unit_phases.py` selects one run per unit with `newest_unit_runs`: the staff current run, otherwise the newest run. Notes, open phase rows, ETA Next, ETA Plan and Workers read that run.
- Other open runs of the unit supply no rows, Notes, workers or promises. A newer finished side run does not change the Workers cell.
- `phase_table.with_run_promises(record, events)` rebuilds each row's `eta_trail` and the `plan_finish` from one run's events. Progress, Phase and duration predictions keep the plan record across all runs.
- `phase_note_label()` shows `phase <id> — <title>` once when the title already starts with `Phase <id>` and a dash, in any case.

**Files:**
- `scripts/production/unit_phases.py` — Units table rows and cells.
- `scripts/direct/phase_table.py` — phase records; `with_run_promises`.
- `scripts/production/test_unit_phases.py` — table rows for side runs, other open runs, the label rule and run-scoped promises.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the selected-run rule and the label rule.

**Gotchas:** a hand-built `PhaseRecord` fixture needs a matching `eta_stated` event in its run, and a `PredictedPhaseTiming` for a predicted plan finish; `with_run_promises` recomputes both cells from events.

**Ruled out:** combining the workers of several open runs; only the staff current run counts.
