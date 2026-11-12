# Units table fit

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The `/showrunner:units` tables fit the user's terminal, so Claude Code draws them as tables.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words, from the sdlc showrunner on 2026-11-12:

"my terminal window is prety wide - yet the markdown didn't fit and it turns it into rows - can you make sure we fit?"

The showrunner's design: leave out a column that is `—` in every row; fit each row to one width, set by a new key in `config/production.conf`; when a row is too wide, cut text cells with `…` in the order Queued, Non-Phase Work, Workers, then Notes last; the Progress bar and the ETAs stay whole.

## Delegation Context

- **Project:** the `~/.claude` scripts — the production Units table (`scripts/production/`).
- **Project started:** 2026-11-12T15:35:00+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`.
- **Layout:**
  - `scripts/production/unit_phases.py` — `table(...)` builds the phase table: headers, then one Markdown row per unit; `shortened` cuts some cells at `TASK_WIDTH = 80`.
  - `scripts/production/style_units.py` — `table(...)` builds the style table for a `Kind: style` production (about line 304).
  - `scripts/production/display_cells.py` — cell helpers both tables share: `NONE = "—"`, `escaped`, `machine_column`.
  - `scripts/lib/terminal_cells.py` — `terminal_width(text)`: display columns (wide characters such as `🟩` count 2).
  - `config/production.conf` — production keys, each with a comment and a range line.
  - `commands/showrunner/units.md` — the command text for both tables.
  - `docs/as-built/showrunner-automation.md` — "Unit phases" item (about line 604).
- **Claude Code's table rule (version 2.1.319, read from its program):** for terminal width `C` and `n` columns, a table row draws as `1 + Σ(width + 3)` columns. Claude Code draws stacked `Header: value` blocks when that width is more than `C - 4`, or when a wrapped cell needs more than 4 lines. A row with every cell on one line at most `C - 4` wide always draws as a table.
- **The user's width:** the screenshot is 1366 pixels at 8 pixels per column (the 20-square Progress bar spans 320 pixels), so the terminal is about 170 columns. 170 is the default; a wider terminal also fits.
- **Today's sdlc table at natural width:** Unit 15, Status 6, Progress 40, Phase 5, Notes 80, ETA Next 15, ETA Plan 9, Workers 65, Non-Phase Work 47, Queued 58. Sum 340 plus 31 border columns; the limit at 170 is 166.
- **Test lanes:** `scripts/production/test_unit_phases.py`, `scripts/production/test_style_units.py`.
- **Build:** `basedpyright scripts/production scripts/lib` must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest scripts.production.test_unit_phases scripts.production.test_style_units`.
- **Invariants:**
  - Cells are measured with `terminal_cells.terminal_width` on the text before `escaped`.
  - The Progress bar, the ETA cells, Unit, Status, Machine and Phase are never cut.
  - Column order does not change.

## Phases

### Phase 1 — Tables fit the terminal width  · status: done

#### As-built

- `config/production.conf` holds `units_table_columns=170` (`# whole: 40..`). Both tables read it with `conf.read_or_repository("production")`.
- `display_cells.fitted(headers, rows, *, cuttable, columns)` leaves out each column whose cells are all `—` and keeps a column with no rows. It measures a row as `1 + Σ(width + 3)` against the limit `columns - 4`.
- While a row is over the limit, `fitted` makes the first `cuttable` column above its header width one column narrower. A cut cell ends with `…` and never splits a wide character. When no cuttable column can shrink, the table stays as it is.
- `unit_phases.table` cuts Queued, Non-Phase Work, Workers, then Notes. The Progress bar, the ETAs, Unit, Status, Machine and Phase are never cut.
- When a row has a Progress bar, `unit_phases.table` adds a blank line and `Progress: 🟩 done · 🟨 open · ⬜ not started`. `progress_cell` and `progress_legend()` share the `ProgressState` constants. A table with no rows, and the style table, have no legend.
- `style_units.table` cuts Step, then Ticket, and keeps its number columns right-aligned after a column is left out.

**Files:**
- `config/production.conf` — `units_table_columns`.
- `scripts/production/display_cells.py` — `fitted`.
- `scripts/production/unit_phases.py` — `ProgressState`, `progress_legend`, the fitted phase table.
- `scripts/production/style_units.py` — the fitted style table.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the command text and the as-built item.
- `scripts/production/test_unit_phases.py`, `scripts/production/test_style_units.py` — the rows.

**Gotchas:**
- `cuttable` holds original column indexes, not header names, because two columns can share a header name.
- A CONF_DIR test fixture can omit `production.conf`; `read_or_repository` then reads the repository file.
- Other suites call `unit_phases.table`: `test_shutdown_cli`, `test_accounts` and `test_style_plan_split`.

**Ruled out:** cut selection by header name.
