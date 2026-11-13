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

### Phase 2 — Each open phase shows its work, its wait and its latest ETA  · status: todo

The user, relayed by the cargo-liner showrunner at 22:20 EST on 2026-11-12: "i can't actually tell what any of them are working on - which should be the intent of this command". The sdlc showrunner sent this evidence from the plan-delegate run records:

1. A run with several open phases shows only one. Port run 386d8d90 has Phase 8 open (repair round 1, fix worker since 20:17, stated ETA 19:15). The table shows only Phase 10, and lists Phase 8 under Queued as `plan: phase 8`. The tile run (branch `remove-running-section-tile`) has 62, 58d, 32j, 58f, 60 and 43j open; the table shows only 62.
2. ETA Next shows an old promise. Handler run 54fb1c7e: the last `eta_stated` for Phase 3 is 21:30 (stated at 18:25 EST). The table shows `15:30 · late 6h 49m`.
3. `no ETA` does not say why. Port Phase 8 has a passed ETA and shows `no ETA`.
4. Status `idle` hides waits. Three directors show `idle` while their workers wait on the build queue (`wait_started`, `HolderNotNamed`). The table shows neither the wait nor its length. Handler round 2 fix took 18:42–22:05; about 2 h of that were waits.
5. Workers shows `—` for port and tile, but port's fix worker and tile's impl workers have open passes.

#### Work Order

**Goal:** In `/showrunner:units` for the cargo-liner production, every open phase of each unit's newest run shows what it does, what it waits on with the wait length, its latest stated ETA, and its workers. The run records are the source.

**Spec:**

1. **Find each cause first.** For each of the five evidence items, read the named run records and the code path that builds the cell (`scripts/direct/phase_table.py`, `scripts/direct/worker_runs.py`, `scripts/production/unit_phases.py`). Post the cause of each item on the board with the record lines and code lines. Fix each cause where it lives.
2. **One row per open phase.** Each open phase of a unit's newest run gets its own row in both tables, in phase order. The first row of a unit carries the unit-wide cells (Status, Machine, Progress, ETA Plan). Later rows of the same unit repeat the Unit name and show `—` in the unit-wide cells. A unit with no open phase keeps one row as now. An open phase never appears under Queued.
3. **Latest ETA.** ETA Next for a phase row is the latest `eta_stated` for that phase instance. A passed ETA shows its time and `late <length>`. `no ETA` appears only when the run states no ETA for that phase; it reads `no ETA stated`.
4. **Waits.** Notes for a phase row ends with `waiting on <what> <length>` while a worker or the director of that phase waits (for example a build queue wait from `wait_started`). The wait text names the holder when the record names one. Status keeps the session classifier; the wait shows in Notes.
5. **Workers.** The Workers cell of a phase row lists the workers with open passes in that phase, from the run records, with the existing worker text rules.
6. **Tests.** Add rows to the existing tests in `scripts/production/test_unit_phases.py`, and in `scripts/direct/test_phase_table.py` or `scripts/direct/test_worker_runs.py` when a cause lives there. Build each fixture from the shape of the evidence records, in a temporary folder. Use no live unit.
7. **Docs.** Update `commands/showrunner/units.md` and the "Unit phases" entry in `docs/as-built/showrunner-automation.md` in the same commit as the code.
8. **Proof.** Screenshot `/showrunner:units` for the cargo-liner production in a Claude Code terminal 170 columns wide, before the change and after. Save the PNG files under the session folder, never in the repository.

**Constraints from prior phases:**
- Phase 1 put Workers last in the status table (`table()` in `scripts/production/unit_phases.py`) and left the work table as `Unit | Notes | Non-Phase Work | Queued`.
- Both phase tables pass through `display_cells.wrapped(headers, rows)`: full cell text, all-`—` columns removed. Claude Code wraps each cell at the terminal width; never cut text with `…`, insert `<br>` or add continuation rows. `units_table_columns` sets only the style table.
- `single_spaced(text)` in `unit_phases.py` is the one whitespace rule for cell text. `shortened()` (80 characters) now applies only to the Workers cell and ETA Next.
- The test lane includes `scripts.production.test_display_cells`.

**Files:**
- `scripts/production/unit_phases.py` — row layout, Notes, ETA Next, Workers.
- `scripts/direct/phase_table.py` — open phase instances and their ETA, when a cause lives there.
- `scripts/direct/worker_runs.py` — open passes and waits, when a cause lives there.
- `scripts/production/test_unit_phases.py`, `scripts/direct/test_phase_table.py`, `scripts/direct/test_worker_runs.py` — test rows.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — docs.

**Workers:** `1 writer + 1 tester`
- `impl` — runs step 1, then writes steps 2–5 and 7.
- `test` — makes the before screenshot at once, writes the step 6 rows, keeps the test lane green, and makes the after screenshot when `impl` posts `done`.

**State:** none.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes for every changed Python file.
2. The Test command passes for the test lane, `scripts.direct.test_phase_table` and `scripts.direct.test_worker_runs`.
3. Live: `scripts/lib/py scripts/production/unit_phases.py --production cargo-liner` shows each open phase of the port, tile and handler runs on its own row, with its latest stated ETA, its waits and its workers.
4. The after screenshot shows each evidence item fixed; the before screenshot shows the old view.
