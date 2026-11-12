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

#### Work Order

**Goal:** `/showrunner:units` prints tables whose rows are at most `units_table_columns - 4` columns wide, so Claude Code draws them as tables.

**Spec:**

1. Add `units_table_columns=170` to `config/production.conf`, with the comment "Terminal width in columns that the /showrunner:units tables fit." and the range line `# whole: 40..`. Read it at use, as the other keys in that file are read.
2. Add one shared function in `display_cells.py`: `fitted(headers, rows, *, cuttable, columns) -> tuple[headers, rows]`.
   - Leave out each column whose cells are all `NONE`. Keep a column that has no rows.
   - The row width is `1 + Σ(width + 3)`, where a column's width is the widest of its header and its cells, and at least 3. The limit is `columns - 4`.
   - `cuttable` is an ordered tuple of header names. While the width is over the limit, take the first cuttable column whose width is above its header's width, and make it one column narrower: each of its cells wider than the new width keeps its text to `new width - 1` columns, strips trailing spaces, then adds `…`. Recompute and repeat.
   - A column is cut only when every column before it in `cuttable` is at its header's width. A cuttable column never goes below its header's width. When no cuttable column can shrink, return the table as it is.
   - Cut on display columns: a wide character is never split.
3. `unit_phases.table` calls `fitted` with `cuttable` in this order: Queued, Non-Phase Work, Workers (the `worker_heading()` text), then Notes last, so Notes keeps its text while another text cell can still shrink. Then it escapes and joins the rows. The optional ETA Plan column keeps its current rule.
4. `style_units.table` calls `fitted` with `cuttable` = Step, then Ticket. Keep its right-aligned number columns aligned after a column is left out.
5. Update `commands/showrunner/units.md`: a column that is `—` in every row is left out; the tables fit `units_table_columns`; long text cells end with `…`.
6. Update the "Unit phases" item in `docs/as-built/showrunner-automation.md` in the same commit.
7. Progress legend (the user via sdlc, 2026-11-12: "i need a legend for the progress table to show up underneath"). When the fitted phase table keeps its Progress column, `unit_phases.table` adds one blank line and then `Progress: 🟩 done · 🟨 open · ⬜ not started` under the table. The squares and words come from the same constants `progress_cell` uses. No legend when Progress is left out or the table has no rows. The style table has no legend. Update `commands/showrunner/units.md` and the as-built item.

**Files:**
- `config/production.conf` — the new key.
- `scripts/production/display_cells.py` — `fitted`.
- `scripts/production/unit_phases.py` — call `fitted`.
- `scripts/production/style_units.py` — call `fitted`.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the text.
- `scripts/production/test_unit_phases.py`, `scripts/production/test_style_units.py` — rows.

**Workers:** `1 writer + 1 tester`
- `impl` — the config key, `display_cells.py`, `unit_phases.py`, `style_units.py`, the command text and the as-built doc.
- `test` — rows in the existing test classes: an all-`—` column is left out; a wide table at 170 columns fits `166`, cut in the order Queued, Non-Phase Work, Workers, then Notes, with Notes whole while another text column is above its header width; Progress and ETA cells stay whole; a wide character is not split; a cuttable column stops at its header width; a table that already fits does not change; the style table keeps its number alignment. Update existing expectations that change.

**State:** none

**Acceptance gate:**
1. `basedpyright scripts/production scripts/lib`: 0 errors, 0 warnings, 0 notes.
2. The Test command passes.
3. Live: `scripts/lib/py scripts/production/unit_phases.py --production sdlc` prints a table whose every row is at most 166 display columns, with Progress and ETAs whole.
