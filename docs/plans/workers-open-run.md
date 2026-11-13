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

#### Work Order

**Goal:** In `/showrunner:units`, Notes and Workers of each unit row read one run: the staff row's current run when it has one, otherwise the newest run. A newer finished side run or another open run of the same unit never changes the Workers cell.

**Spec:**

1. **One run value.** In `table()`, select the unit's run once. Notes, the open phase rows, ETA cells and Workers all read that value. Remove any second lookup that can select a different run. Keep `newest_unit_runs` as the one selection rule.
2. **Other open runs.** An open run that is not the selected run supplies no rows, no Notes and no workers.
3. **Tests.** Add rows to the existing table tests in `scripts/production/test_unit_phases.py`. Build each fixture in a temporary folder from the shape of the evidence. Use no live unit.
   - A staff current run with an open phase and two open worker passes, and a newer finished astra-review run of the same owner. Workers shows the two workers of the current run on the open phase row.
   - The same newer finished run, and a current run between phases with a working pane worker. Workers shows that worker.
   - The same current run and a newer open run of the same owner that is not the staff current run, with its own open phase and worker pass. Only the current run's phase rows and workers show.
4. **Docs.** Update `commands/showrunner/units.md` and the "Unit phases" entry in `docs/as-built/showrunner-automation.md` in the same commit when their words change. State that other open runs of the unit do not show.
5. **No repeated phase label.** When a phase title already starts with `Phase <id> —` (any case, any dash), Notes shows `phase <id> — <rest of the title>` once. Live example: port reads `phase 9 — Phase 9 — cargo-mend CLI-boundary tests run under 1 s`; it must read `phase 9 — cargo-mend CLI-boundary tests run under 1 s`. Add a row to the existing Notes test. The sdlc showrunner made this part of this phase.

**Files:**
- `scripts/production/unit_phases.py` — the one run value in `table()` and the phase label in Notes.
- `scripts/production/test_unit_phases.py` — the test rows.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — docs.

**Workers:** `1 writer`
- `impl` — writes steps 1–5 and keeps the test lane green.

**State:** none.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes for every changed Python file.
2. The Test command passes for the test lane.
3. Live: `env PYTHONPATH="$PWD" scripts/lib/py -m scripts.production.unit_phases --production cargo-liner` shows the port and tile workers on their open phase rows. Port phase 9 Notes reads `phase 9 — cargo-mend CLI-boundary tests run under 1 s`.
