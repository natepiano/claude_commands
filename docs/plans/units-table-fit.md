# Units view: both tables keep table form down to a set floor width

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/showrunner:units` splits long cells into continuation rows and breaks long ids, so Claude Code keeps both phase tables in table form at every terminal width from a configured floor upward.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words on C1514, returned through the sdlc showrunner on 2026-11-13:

"the word wrap is not quite working right - i did see a wrap in notes and in queued but that's for a still very wide window - past a certain point it switched the table to be all rows instead of just continuing the word wrap - i don't know which column was driving this but word wrap needs to work beter"

The director's diagnosis at 15:21 EST, from the Claude Code 2.1.319 binary (`~/.local/share/claude/versions/2.1.319`, the table function near the string `kind:"vertical"`):

1. Width rule. `p` is the render width. `available = max(p - (1 + 3 × columns) - 4, 3 × columns)`. Each column's floor is its longest word (split on whitespace), at least 3. Each column's full width is its longest cell, at least 3.
2. When the full widths fit, each column gets its full width. When only the floors fit, each column gets its floor plus `floor((full - floor) / Σ(full - floor) × spare)`. Otherwise every column gets `max(floor(floor × available / Σfloors), 3)`, and every cell wraps hard, inside words.
3. Cells wrap at word boundaries (hard inside words in the last case). Header cells count.
4. The renderer stacks the table into "Header: value" lines when any cell needs more than 4 lines, or when the drawn table is wider than `p - 4`.

Measured on live data with that rule at 15:21 EST (render columns):

| Production | Table 1 stacks below | Table 2 stacks below |
| --- | --- | --- |
| sdlc | 156 | 177 |
| builds | 134 | 175 |
| cargo-liner | 167 | 136 |

Table 2 drivers: Queued and Notes cells that need 5 or more lines. Table 1 driver: the sum of column floors (bar 40, Phase id `restart-record-replace` 22, Workers `item137-bf9-final-callers` 25, ETA Plan 16) exceeds the available width. That forces hard wrap, and ETA Plan then needs 5 lines.

The sdlc showrunner's ruling (2026-11-13, 15:40 EST):

1. Keep the two-table layout. Workers stays in table 1 (the user's C1499 request).
2. Port the renderer width rule. Split cells over 4 lines into continuation rows. Widths only grow above the floor.
3. The floor is the narrowest width where the current table 1 keeps table form, about 125. Break long ids inside the word where that lowers the floor.
4. Report the measured floor for sdlc, builds and cargo-liner in the checkpoint notice.
5. Proof: Claude Code screenshots at a wide width, about 140, and the floor, before and after.

## Delegation Context

- **Project:** the `~/.claude` scripts — the `/showrunner:units` view (`scripts/production/unit_phases.py`).
- **Project started:** 2026-11-12T11:14:06-05:00
- **Stack:** Python 3.13, basedpyright, `unittest`. Claude Code 2.1.319 renders the showrunner's reply.
- **Layout:**
  - `scripts/production/unit_phases.py` — `table()` builds table 1 (`Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan] | Workers`) and table 2 (`Unit | Notes | Non-Phase Work | Queued`). Both go through `display_cells.wrapped()`. `single_spaced()` normalizes cell text.
  - `scripts/production/display_cells.py` — `wrapped(headers, rows)` keeps full cells and removes all-`—` columns. `fitted()` serves only the style table. `escaped()` escapes pipes.
  - `config/production.conf` — `units_table_columns=170` sets only the style table width.
  - `commands/showrunner/units.md` — the command text the showrunner follows. It says "Keep each phase on one Markdown row".
  - `docs/as-built/showrunner-automation.md` — the "Unit phases" entry.
- **Test lane:** `scripts.production.test_claude_table_layout`, `scripts.production.test_unit_phases`, `scripts.production.test_display_cells`, `scripts.production.test_style_units`, `scripts.agents.test_shutdown_cli`.
- **Build:** `basedpyright` on each changed Python file must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - The style production table (`style_units.py`) does not change.
  - The column sets, column order and cell texts do not change, except for continuation rows and the spaces that break long ids.
  - Add no guard, refusal or check that this plan does not ask for.

## Phases

### Phase 1 — Both tables keep table form from the floor width upward  · status: done

#### As-built

- `scripts/production/claude_table_layout.py` models the Claude Code 2.1.319 table width rule. `fit(headers, rows, render_width)` returns `ColumnFit(widths, hard_wrap)`; `ColumnFit.keeps_table` predicts table form against the 4-line cap and the `p - 4` width limit.
- `continued(headers, rows, render_width, line_cap, break_width)` splits a cell over the cap into continuation rows of the same unit, at ` · `, then `; `, then a word boundary, and repeats until no cell exceeds the cap. Unit, Status, Machine and Progress never split.
- While the column floors exceed the available width, the longest word of the widest breakable column gets one space: after the last `-`, `/` or `_` between two letters or digits within the column share, else at the share for a word of at least `break_width` columns. When no such word is left, the longest word over 3 columns breaks, because one break costs less than the renderer's hard wrap of every column.
- `unit_phases.py`, run as a script, reads its pane width once through `herdr.own_pane_columns`: the socket method `pane.layout` for `HERDR_PANE_ID`, limited by `units_table_width_read_milliseconds` (50). Median 0.7 ms, p90 7.7 ms; about 6% of reads meet a herdr wait of about 100 ms.
- `display_cells.wrapped(headers, rows, columns)` shapes both phase tables for that width minus `REPLY_INDENT` (0). Without a width, it uses `units_table_floor_columns` (120) and prints no anomaly. Calls from tests never read a live pane.
- sdlc shots: no id breaks at 200; ids break at 140 and 120; both tables keep table form at all three widths.

**Files:**
- `scripts/production/claude_table_layout.py` — the renderer model, continuation rows and word breaks.
- `scripts/herdr/herdr.py` — `own_pane_columns(timeout)`.
- `scripts/production/display_cells.py` — `pane_columns()` and `wrapped()`.
- `scripts/production/unit_phases.py` — `main(arguments, columns)` and `table(..., columns)`; the script entry point reads the width.
- `config/production.conf` — `units_table_floor_columns=120`, `units_table_cell_lines=4`, `units_table_break_columns=30`, `units_table_width_read_milliseconds=50`.
- `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — the rules above.

**Gotchas:** The floor depends on live data: a new unit name `sdlc-merge-gate` put the sdlc minimums 1 column over 120. A read that times out shapes for the floor, so a wide window can show break spaces. The model matches Claude Code 2.1.319; a renderer change needs a new measure.

**Ruled out:**
- `:` as a break mark: it split times such as `17:24`.
- A zero-width space as the break: Claude Code splits words only at plain spaces.
- Shaping only for the floor: it showed break spaces at every width.
