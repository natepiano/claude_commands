# Units view: Workers in the status table, wrapped text in the work table

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/showrunner:units` shows Workers in the status table and wraps the work table's text inside its cells, so the user can read every word in a 170-column Claude Code terminal.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words on C1499, from the sdlc showrunner on 2026-11-12:

"you should put the workers column in the first table after ETA Plan and the text in the second table should word wrap within their cells so we still fit on screen but can also see the text - it's nearly impossible for me to figure out what is going on"

The showrunner's ruling:

1. Table 1: `| Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan] | Workers |`.
2. Table 2: Notes, Non-Phase Work and Queued wrap inside their cells to fit `units_table_columns`. No text is cut with an ellipsis.
3. The showrunner prints the output as Markdown in the Claude Code terminal. Prove the wrap in that renderer with a screenshot of `/showrunner:units`, before and after.
4. Update `commands/showrunner/units.md` and its as-built in the same commit.

## Delegation Context

- **Project:** the `~/.claude` scripts — the `/showrunner:units` view (`scripts/production/unit_phases.py`).
- **Project started:** 2026-11-13T03:00:00+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`. Claude Code 2.1.319 renders the showrunner's reply.
- **Layout:**
  - `scripts/production/unit_phases.py` — `table()` (line 446) builds two tables. `status_headers` (line 531) is `Unit | Status | [Machine] | Progress | Phase | ETA Next | ETA Plan`. `work_headers` (line 532) is `Unit | Notes | worker_heading() | Non-Phase Work | Queued`. The loop at line 540 calls `display_cells.fitted(...)` per table: the status table has no cuttable column; the work table cuts columns `(4, 3, 2, 1)` with `CutOrder.WIDEST_FIRST`. `workers_cell()` (line 207) and `worker_heading()` (line 440) make the Workers cell and heading.
  - `scripts/production/display_cells.py` — `CutOrder` (line 16) and `fitted(headers, rows, *, cuttable, columns, cut_order)` (line 23): removes all-`—` columns and cuts text until `1 + sum(width + 3) <= columns - 4`. `escaped()` (line 125) escapes pipes. `style_units.py` also calls `fitted`; its table does not change.
  - `config/production.conf` — `units_table_columns=170`.
  - `commands/showrunner/units.md` — the command text that the showrunner follows to print the tables.
  - `docs/as-built/showrunner-automation.md` — the "Unit phases" entry (about line 618) describes the tables, fitting and cut order.
- **Test lane:** `scripts.production.test_unit_phases`, `scripts.production.test_style_units`, `scripts.production.test_display_cells` when it exists, `scripts.agents.test_shutdown_cli`.
- **Build:** `basedpyright` on each changed Python file must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - The style production table (`style_units.py`) does not change.
  - Cell content rules (Notes text, ETA cells, Progress bar, legend text, repeat removal) do not change.
  - Add no guard, refusal or check that this plan does not ask for.

## Phases

### Phase 1 — Workers move to the status table and work text wraps  · status: done

#### As-built

- The status table headers are `Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan] | <Workers heading>`. Workers is the last column. The all-`—` column removal still applies.
- The work table headers are `Unit | Notes | Non-Phase Work | Queued`. Each cell holds the full text, with no `…` cut.
- The command does not wrap text. Claude Code 2.1.319 wraps each cell at the terminal width and keeps one visible row per unit.
- `display_cells.wrapped(headers, rows)` keeps the full cells and removes columns whose cells are all `—`. Both tables go through it.
- `unit_phases.single_spaced(text)` is the one whitespace rule for cells. `shortened()` (`TASK_WIDTH` 80) applies only to the Workers and ETA cells.
- `units_table_columns` sets only the width of the style table.

**Files:**
- `scripts/production/unit_phases.py` — the two table layouts and the cell text.
- `scripts/production/display_cells.py` — `wrapped()` beside `fitted()`.
- `scripts/production/test_display_cells.py`, `scripts/production/test_unit_phases.py`, `scripts/agents/test_shutdown_cli.py` — the table rows.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md`, `config/production.conf` — the layout and the width key.

**Binds later work:** each open phase row builds on `display_cells.wrapped()` and the full cell text; Workers stays the last status-table column; `single_spaced()` normalizes cell text.

**Gotchas:** `<br>` renders as literal text, and continuation rows get borders between lines. Neither form keeps one visible row per unit.

**Ruled out:** command-side wrapping to `units_table_columns` — the renderer sets the column widths, so the command cannot hold them.

### Phase 2 — Each open phase shows its work, its wait and its latest ETA  · status: done

#### As-built

- Each open phase of a unit's current run has its own row in both tables, in plan order. The first row carries Status, Machine, Progress, ETA Plan and Queued; later rows of the unit show `—` there. Phases outside the plan get their own rows.
- Notes read `phase <id> — <title>`, then an informative current step, fix rounds, and each open wait with its holder (the staff name when known) and its length. A wait reason that already states its wait keeps its own words.
- ETA Next is the latest `eta_stated` for that phase: `late <length>` when passed, `no ETA stated` when none. ETA Plan also names promises for unfinished target phases with no open row.
- Workers lists the open recorded passes of that phase instance, with their detached run states.
- `newest_unit_runs` prefers the staff row's current run over newer ad hoc runs. `run_is_finished(events)` lets the latest `run_finished` or `reopened` event decide.
- Only the latest `phase_finished` of a phase id decides done; `error` and `stopped` finishes are not done. `build_run` keeps each finish status.

**Files:**
- `scripts/production/unit_phases.py` — phase rows, `phase_notes_cell`, `phase_eta_cell`, `pending_eta_cell`, `run_is_finished`.
- `scripts/direct/phase_table.py` — `_open_instances` returns every open instance of the newest run.
- `scripts/direct/worker_runs.py` — `phase_workers`, `phase_waits`, `PhaseWait`, `NamedWaitHolder`/`UnnamedWaitHolder`.
- `scripts/production/test_unit_phases.py`, `scripts/direct/test_phase_table.py`, `scripts/direct/test_worker_runs.py` — the row and record cases.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the row and cell rules.

**Gotchas:** an `eta_stated` record can target a phase other than the active one (`eta_phase_id`). A reopened run keeps its earlier `run_finished` event.

**Ruled out:** moving an explicit ETA target to the active phase — the record names the promised phase; hiding an errored phase from Queued — it is waiting work.
