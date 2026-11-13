# Units view in two tables

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/showrunner:units` prints the phase production view as two tables, so the text columns are readable in a 170-column terminal.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words on C1475, from the sdlc showrunner on 2026-11-12:

"i think it should split into two tables if this is how it is going to be because i can't see what's in Notes, workers, non-phase work or queued"

The showrunner's ruling: "Split /showrunner:units into two tables. Table 1: Unit, Status, Machine (when shown), Progress, Phase, ETA Next, ETA Plan. Table 2: Unit, Notes, Workers, Non-Phase Work, Queued. The goal: the user can read Notes, Workers, Non-Phase Work and Queued in a 170-column terminal. Table 2 gets the full width. Remove text that repeats: for example, Non-Phase Work now repeats the Notes phase when the open phase is outside the plan. Show it only when it differs. Cut text only when a cell still does not fit."

## Delegation Context

- **Project:** the `~/.claude` scripts — the `/showrunner:units` view (`scripts/production/unit_phases.py`).
- **Project started:** 2026-11-12T19:30:00+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`.
- **Layout:**
  - `scripts/production/unit_phases.py` — `table()` (about line 439) builds one row per unit: `Unit | Status | [Machine] | Progress | Phase | Notes | ETA Next | ETA Plan | Workers | Non-Phase Work | Queued`. It drops ETA Plan when no row runs a plan, then calls `display_cells.fitted(..., cuttable=(Queued, Non-Phase Work, Workers, Notes), columns=units_table_columns)`, writes the Markdown rows, and adds the Progress legend when a row has a Progress bar.
  - `notes_cell()` (about line 241) shows `phase <id> — <task>` and fix rounds. `table()` passes it the run record when the plan has no phase rows, so it can name a phase outside the plan.
  - `non_phase_cell()` (about line 377) shows `<id>: <label>: <text>` for each open run phase outside the plan, the newest first, with `+N more`.
  - `queued_cell()`, `workers_cell()`, `worker_heading()` — the other text cells.
  - `scripts/production/display_cells.py` — `fitted(headers, rows, *, cuttable, columns)` removes all-`—` columns and cuts text in `cuttable` order until `1 + sum(width + 3) <= columns - 4`. `style_units.py` also calls it; its table does not change.
  - `config/production.conf` — `units_table_columns=170`.
- **Live sample (2026-11-12, sdlc, 170 columns):** Notes, Non-Phase Work and Queued are cut to a few characters (`copyimport: f…`, `copyi…`). The hierarchies row shows `phase sdlc-merge — …` in Notes and `sdlc-merge: —` in Non-Phase Work.
- **Test lane:** `scripts.production.test_unit_phases`, `scripts.production.test_style_units`, `scripts.production.test_display_cells` when it exists.
- **Build:** `basedpyright scripts/production` must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - The style production table (`style_units.py`) does not change.
  - Cell content rules (Notes text, ETA cells, Progress bar, legend text) do not change, except the repeat removal below.
  - Add no guard, refusal or check that this plan does not ask for.

## Phases

### Phase 1 — The units view prints two tables  · status: done

#### As-built

- `unit_phases.table()` prints a status table, `Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan]`, then the Progress legend when a row has a bar, a blank line, and a work table, `Unit | Notes | <Workers heading> | Non-Phase Work | Queued`. Both tables use the same unit order. With no rows, both tables keep their headers.
- Each table calls `display_cells.fitted()` on its own with `units_table_columns`; the all-`—` column removal applies per table. The status table has no cuttable column.
- `display_cells.CutOrder` selects the cut order: `LISTED` (the default, used by the style table) or `WIDEST_FIRST` (the work table). `WIDEST_FIRST` cuts the widest cuttable column one character per step; on equal widths it takes the first in `cuttable` (Queued, Non-Phase Work, Workers, Notes).
- `non_phase_cell(record, run, *, notes_record=...)` leaves out the open phase that Notes shows. `queued_cell(staff_id, record, *, notes=...)` leaves out each queue item equal to the Notes text; `+N more` counts only the items that stay.

**Files:**
- `scripts/production/unit_phases.py` — the two tables and both repeat rules.
- `scripts/production/display_cells.py` — `CutOrder` and `fitted(..., cut_order=)`.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the two-table layout, fitting and repeat rules.
- `scripts/production/test_unit_phases.py`, `scripts/agents/test_shutdown_cli.py` — table rows.

**Gotchas:** `OpenUnitRun` holds at least one open phase; apply a phase exclusion when the cell is made, never by building a run with no phases.

**Ruled out:** the listed cut order for the work table — it cut Queued to a few characters while Notes kept 80.

### Phase 2 — The units view prints in a few seconds  · status: done

#### Work Order

**Goal:** `/showrunner:units` prints in a few seconds. Today `scripts/lib/py scripts/production/unit_phases.py --production sdlc` takes 77 to 105 s wall time on natedev, on main and on this branch.

The sdlc showrunner's ruling on 2026-11-12: "make /showrunner:units fast. 77 to 105 s is far too slow for a status command. Measure where the time goes (paired runs, not cProfile alone), fix the slowest parts, and give before/after wall times. Target: a few seconds."

**Spec:**

1. **Measure.** Find where the wall time goes for `--production sdlc`. Use paired wall-time runs (the same command before and after one change, run alternately, at least 3 pairs). A profiler (`cProfile`, `py-spy`) may point at candidates, but a gain is named only from paired wall times. Look at subprocess calls (git, herdr, ssh, `ps`), repeated file reads (run histories, staff, plan docs, queue files) and work repeated per unit or per row. Run each heavy probe under `systemd-run --user --scope -p MemoryMax=2G`.
2. **Fix the slowest parts.** Remove repeated work: read each file and run each subprocess once per command, and skip work whose result no cell shows. Keep every cell's content the same. Fix a cause in the module where it lives, also when that module is outside `scripts/production/`.
3. **Report.** Give the before and after wall times (median of the pairs) and the share of each fixed cause, in the summary.

**Files:**
- `scripts/production/unit_phases.py` — the units view.
- `scripts/production/display_cells.py` — shared cells.
- `scripts/production/test_unit_phases.py` — rows.

Other modules on the slow path are in scope when the measurement names them; name each one in the summary.

**Workers:** `1 writer + 1 tester`
- `impl` — measures, then fixes the slowest parts in the production modules.
- `test` — measures the baseline independently at the phase baseline commit with paired runs, keeps the test lane green, and adds rows only where a fix changes a function's contract. Reports its own before/after wall times when `impl` posts `done`.

**State:** none.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes for every changed Python file.
2. The Test command passes for the test lane and for the tests of each changed module.
3. Live: `scripts/lib/py scripts/production/unit_phases.py --production sdlc` prints the same two tables as before the change (time-dependent cells aside), and its median wall time over 3 runs is a few seconds.
