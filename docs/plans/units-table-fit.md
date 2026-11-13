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

#### Work Order

**Goal:** In `/showrunner:units`, Claude Code 2.1.319 shows both phase tables as tables at every terminal width from `units_table_floor_columns` upward. No cell needs more than the renderer's line cap at that width. Long text continues on extra rows of the same unit.

**Spec:**

1. **Config.** Add two keys to `config/production.conf`, each with a comment and a range line:
   - `units_table_floor_columns` — the narrowest terminal width, in terminal columns, where both phase tables keep table form. Set it to the measured value from step 6.
   - `units_table_cell_lines=4` — the most lines Claude Code 2.1.319 lets one table cell wrap to before it stacks the table.
   Read both at use, the same way `style_units.py` reads `units_table_columns`.
2. **Renderer width rule, one module.** Put the Claude Code width rule, continuation rows and id breaks in `scripts/production/claude_table_layout.py`. The width rule is the four diagnosis items above. Give the module one public entry point for the fit. It takes headers, rows and a render width. It returns each column's width and whether wrapping is hard. Name the Claude Code version and the binary location in the module docstring. Measure display width with the rule `display_cells` already uses; each Progress character is 2 columns. Find the render width from the terminal width: measure the difference (the reply indent) in step 7, and keep it as one named value.
3. **Continuation rows.** At the floor width, a cell that needs more lines than the cap splits into continuation rows of the same unit. Split at item marks first (` · `, then `; `), then at word boundaries. Each piece fills the cap without going over it. A continuation row repeats the Unit name and leaves every other cell empty. Splitting changes full widths, so compute the layout again until no cell needs more lines than the cap. Header cells also count.
4. **Long ids.** While the sum of column floors at the floor width exceeds the available width, break the longest word of the widest breakable column. Unit, Status, Machine and Progress are not breakable. Break after the last `-`, `/`, `:` or `_` before the column's share; with none, break at the share. A break is one space, so Claude Code can wrap there.
5. **Monotonic.** Above the floor, every column width only grows, so table form holds. Keep that true; add no width input other than the floor.
6. **Measure the floor.** With the module, find the narrowest terminal width where table 1 of sdlc, builds and cargo-liner keeps table form after steps 3 and 4, and the Progress bar does not wrap. Set `units_table_floor_columns` to the largest of the three. Put the three measured values in your summary.
7. **Proof in the renderer.** On natedev, run Claude Code 2.1.319 in a terminal window sized to an exact column count, have it print the `unit_phases.py --production sdlc` output as Markdown (as `/showrunner:units` does), and save a PNG. Take shots at about 200 columns, at 140 and at the floor, once with origin/sdlc's `unit_phases.py` (before) and once with this tree (after). Use one `scratch.py new --worker --name shots` folder; name files `before-<cols>.png` and `after-<cols>.png`. Put the folder path in your summary; the director holds the folder for the checkpoint notice. Use the shots also to confirm the reply indent from step 2: find the narrowest width where the before output still shows table form, and compare it with the module's prediction.
8. **Tests.** Build rows from the shape of the evidence in `test_claude_table_layout.py` and `test_unit_phases.py`. Use no live unit.
   - A Queued cell of 400 characters at the floor width becomes continuation rows; each piece fits the cap at the floor width.
   - A Notes cell with ` · ` marks splits at a mark before a word boundary.
   - A Phase id of 22 characters and a Workers label of 25 characters in a table whose floors do not fit: each breaks at a hyphen, and the module then predicts no hard wrap.
   - Above the floor, the module predicts table form for the same rows at a wider width.
9. **Docs.** Update `commands/showrunner/units.md` (replace "Keep each phase on one Markdown row"; name continuation rows, the floor key and the line cap) and the "Unit phases" entry in `docs/as-built/showrunner-automation.md` in the same commit.

**Files:**
- `scripts/production/claude_table_layout.py` — new: the renderer width rule, continuation rows and id breaks.
- `scripts/production/display_cells.py` — `wrapped()` uses the new module.
- `scripts/production/unit_phases.py` — both tables go through the new fit.
- `scripts/production/test_claude_table_layout.py`, `scripts/production/test_display_cells.py`, `scripts/production/test_unit_phases.py`, `scripts/agents/test_shutdown_cli.py` — the rows and the config fixture.
- `config/production.conf`, `commands/showrunner/units.md`, `docs/as-built/showrunner-automation.md` — keys and docs.

**Workers:** `1 writer`
- `impl` — writes steps 1–9 and keeps the test lane green.

**State:** none.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes for every changed Python file.
2. The Test command passes for the test lane.
3. Live: `env PYTHONPATH="$PWD" scripts/lib/py -m scripts.production.unit_phases --production sdlc` (also builds and cargo-liner): the module predicts table form for both tables at the floor width.
4. Renderer: the after shots show both tables as tables at all three widths. The before shot at the floor shows the stacked form.
