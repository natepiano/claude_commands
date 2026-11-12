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

#### Work Order

**Goal:** `/showrunner:units` for a phase production prints a status table and a work table. In a 170-column terminal, the user reads Notes, Workers, Non-Phase Work and Queued in full whenever they fit.

**Spec:**

1. **Two tables.** `table()` prints table 1, `Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan]`, then the Progress legend when shown, then a blank line, then table 2, `Unit | Notes | <Workers heading> | Non-Phase Work | Queued`. The Machine and ETA Plan rules stay as they are. Both tables use the same unit order.
2. **Fitting.** Each table calls `display_cells.fitted()` on its own columns with `units_table_columns`. Table 1 has no cuttable column. Table 2 cuts only when table 2 alone still exceeds the width. It cuts the widest cuttable column first, one character per step, so cut text spreads across the text columns; on equal widths it cuts in the order Queued, Non-Phase Work, Workers, Notes. The style table keeps its listed cut order. (Changed at repair round 1: the listed order cut Queued to a few characters while Notes kept 80.) The all-`—` column removal applies to each table.
3. **No repeated text.** Non-Phase Work leaves out each open phase whose id is the phase Notes shows for that row. When nothing is left, the cell is `—`. Queued leaves out each queue item whose text equals the row's Notes text. Find any other cell text in table 2 that repeats another cell of the same row, and remove the repeat by the same rule; name each one in your summary.
4. **Docs.** Update the phase productions item in `docs/as-built/showrunner-automation.md` (about line 670): the two tables, their columns, the per-table fitting and the repeat rule. Update the same commit.

**Files:**
- `scripts/production/unit_phases.py` — the two tables and the repeat rule.
- `docs/as-built/showrunner-automation.md` — the phase productions item.
- `scripts/production/test_unit_phases.py` — rows.

**Workers:** `1 writer + 1 tester`
- `impl` — `unit_phases.py` and the as-built doc.
- `test` — rows in the existing table tests: the output has two tables with the columns above; table 2 text is not cut when table 2 fits; a Non-Phase Work phase equal to the Notes phase shows `—`; a different non-plan phase still shows.

**State:** none.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes.
2. The Test command passes for the test lane.
3. Live: `scripts/lib/py scripts/production/unit_phases.py --production sdlc` from this worktree prints two tables, and table 2 rows show Notes, Non-Phase Work and Queued without `…` when they fit in 170 columns.
