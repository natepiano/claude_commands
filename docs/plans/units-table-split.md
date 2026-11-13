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

#### As-built

- `unit_phases.main` takes one `staff.snapshot()` and opens `plan_history.history_snapshot(snapshot)` inside `herdr.observation_snapshot()`. The reader thread runs `live_units.read_units(name, record=snapshot, ...)` under `copy_context()`, so it shares the same snapshot.
- `plan_history.HistorySnapshot` holds the staff record, the run headers (`starts`) and the run events per path for one command. `staff_record()`, `run_starts()` and `read_run_events(path)` use it when it is set, and read directly when it is not.
- `plan_history.read_run_events(path)` is the one run-event reader: a shared flock, invalid JSON lines skipped, `OSError` or `UnicodeError` gives no events. `phase_table.build_plan` and the work table cells use it.
- `plan_history._staff_paths(fields, row)` is the one path rule for `staff_directory` and `plan_runs`.
- `live_units.read_units` takes an optional `record: staff.StaffSnapshot`.
- On natedev, `--production sdlc` prints in a median of 1.7 s (52.4 s before). Run headers are read 398 times (4776 before) and staff snapshots once (9 before).

**Files:**
- `scripts/direct/plan_history.py` — the command snapshot, the shared readers and the staff path rule.
- `scripts/direct/phase_table.py` — the shared event reader and staff record.
- `scripts/production/live_units.py` — `read_units(..., record=)`.
- `scripts/production/unit_phases.py` — one snapshot per command, shared with the reader thread.
- `scripts/production/test_unit_phases.py` — `test_main_reads_shared_history_once_for_multiple_unit_plans` counts real header, staff and file reads.

**Gotchas:** a thread started inside the snapshot sees it only through `copy_context().run`; a bare `submit` reads everything again.

**Ruled out:** a per-path cache that ignores which reader filled it — the two old readers handled lock errors differently.
