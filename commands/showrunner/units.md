---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's staff row. Print its total line too, when present.
Print the tables as Markdown in the reply, without code fences.
Preserve continuation rows exactly as the script prints them.
Each continuation row repeats Unit and fills one work cell.
The script reads this pane's width once from herdr and shapes the phase tables for that width.
When it cannot read the width within `units_table_width_read_milliseconds`, it uses `units_table_floor_columns`.
This fallback is normal and prints no anomaly.
`units_table_cell_lines` sets the cell line cap for Claude Code 2.1.319.
Each table shows only the production's staff units, sorted by unit name without case distinctions.
The phase tables read one `cargo-handler census --json` for **Status** and **Workers**.
The census lists the unit directors and workers that the handler tiles show.
It runs beside the other reads, within `units_census_seconds` in `config/production.conf`.
For the phase status table, **Status** is the tile's status word for the unit's staff ID.
The words are `busy`, `starting`, `waiting`, `idle`, `paused` and `exited`.
A tile without an accounts entry shows `—`.
A unit that the census does not list shows `exited` when the census scanned the unit's machine.
A unit on a machine that the census did not scan shows `—`.
When the census fails, Status and Workers show `—` and the output ends with one anomaly line.
The style table keeps the shared session classifier, which the accounts board also uses.
**Unit** is the unit's staff name, including stopped units and units without a session.

The phase status table and the style table add **Machine** after **Status** only when a row runs on another machine.
The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.

Each table omits a column when every row has `—` in that column.
A table without rows keeps its columns.
The phase tables preserve full work text and use Claude Code's native Markdown wrapping.
Claude Code sets their column widths from the terminal width.
When the column minimums exceed that width, the script adds a space inside the longest ID.
The space follows a `-`, `/` or `_` between two letters or digits.
A word without such a mark breaks only at `units_table_break_columns` or more columns; shorter words stay whole.
When no such word is left and the minimums still exceed the width, the longest word over 3 columns breaks.
One break costs less than Claude Code's hard wrap of every column.
The style table uses `units_table_columns` in `config/production.conf`, which defaults to 170 terminal columns.
It allows four columns for the terminal margin and measures text before Markdown escaping.
Long style text cells end with `…`; a cut never splits a wide character.
Style column headers set the minimum text widths.
Fixed style columns and minimum text widths can exceed a smaller configured width.

For units outside the `Style units` table in a production without `Kind: style`, the command prints two tables.
Table 1 shows `| Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan] | Workers |`.
Table 2 shows `| Unit | Notes | Non-Phase Work | Queued |`.
Both tables use the same unit order.
Each open phase of the selected run gets one row in each table.
The staff current run selects the run when present; otherwise, the newest run supplies the rows.
Notes, ETA cells, and Workers use that same selected run.
Other open runs of the unit supply no rows, Notes, or workers.
Rows follow the configured plan order, then the start order for phases outside that plan.
Each row repeats the Unit name.
Only the first row shows Status, Machine, Progress, ETA Plan, and Queued.
Later rows use `—` in those cells.
A unit without open phases keeps one row.
A run remains open after a `reopened` event until a later `run_finished` event.
Workers uses the configured display name.
ETA Plan appears when a unit has unfinished plan phases or a promise for an unfinished target without an open row.

1. **Progress** shows only a bar: `🟩` done, `🟨` open, and `⬜` not started.
   It uses one cell per phase, or 20 equal groups above 20 phases.
   An open phase takes priority over not started, then done.
   All three characters use the same display width.
   With no recorded phases, Progress gives `—`.
   When a row shows a Progress bar, one blank line separates table 1 from this legend:
   `Progress: 🟩 done · 🟨 open · ⬜ not started`.
   A blank line separates table 2 from the legend or table 1.
   The style table has no Progress legend.
2. **Phase** shows the phase id for an open phase row.
   Without an open phase, it shows `<done>/<total>` from Progress, or `—` with no phases.
   When the plan has no phases, an open run supplies Progress, Phase, and both ETA cells from its recorded phases.
   A finished selected run supplies no fallback.
3. **Notes** starts with `phase <id> — <phase title>` for the open phase.
   A matching phase label at the start of the title appears once, regardless of case or dash.
   The phase instance in the selected run supplies the open step.
   A useful step appends ` · <label>: <text>`, or ` · <text>` without a label.
   Empty text, role-only text, and `<label> dispatch` add nothing.
   Text equal to the title adds nothing; this comparison ignores case and whitespace.
   Other states are `between phases`, `not started`, `plan done`, `no phases`, and `no plan`.
   Repair rounds append ` · fix rounds <n>[ + 1 running]` when the count exceeds zero.
   Each open worker or director wait appends ` · waiting on <reason> <elapsed>`.
   A recorded holder adds `(held by <holder>)` before the elapsed time.
   A matching staff ID or current session ID uses the staff name.
   An unknown holder keeps its recorded name or ID.
   A reason that starts with `waiting on`, `waiting for`, or either phrase after `blocked:` keeps that wording.
   A finished wait or pass removes its wait text.
   Status keeps the census word even while Notes shows a wait.
4. **ETA Next** shows the latest stated promise for that row's phase instance.
   `eta_phase_id` names the target; the active instance on an ETA event does not override that target.
   Without open work, it uses the next not-started plan phase with a promise.
   A passed promise adds ` · late <elapsed>`.
   Open work without a promise shows `no ETA stated`; no open work or promised phase gives `—`.
   Times use the production zone: `HH:MM` today, `Sat 18:41` within six days, otherwise `24 Oct`.
5. **ETA Plan** shows the predicted plan finish in the same time form, without `ends`.
   Duration predictions still use the plan's recorded history.
   It never precedes the latest promise of any open or not-started plan phase.
   The first row also names promises for unfinished targets without open rows, as `phase <id>: <time>[ · late <elapsed>]`.
   The latest `phase_finished` event for each target decides whether that target is done.
   Only status `completed` marks a target as done.
   A later `error` or `stopped` finish overrides earlier completions and plan-record done rows.
   That target's promise remains visible.
   A row without an unfinished plan or such a promise shows `—`.
6. **Workers** lists the census workers of the unit.
   An open phase row lists only the workers whose open pass is in that phase instance.
   A row without an open phase lists every census worker of the unit.
   Each detached run shows first, as `<name> waiting on run: <label> <elapsed>`, `<name> result queued: <label>`, or `<name> run gone: <label>`.
   A running run without a start time omits the elapsed time.
   A worker without a run shows `<name> working` when busy or starting, and `<name> waiting` when waiting.
   Idle workers add `+<n> idle` after other workers.
7. **Non-Phase Work** shows `—` for an open phase row because Notes already describes that phase.
   A row without a recorded phase instance keeps the existing summary for work outside the plan.
8. **Queued** combines the first `/unit:queue` item and `plan: phase <id>: <title>`.
   It excludes each queue item whose text equals Notes in the same row.
   Each part adds ` +<n> more` for later items.
   The queue count includes only remaining items.
   With no remaining queue item, it shows the plan part or `—`.
   Plan work includes every phase that is neither done nor open.
   Every open phase of the selected run stays outside Queued, including phases absent from the configured plan.
   It uses the configured plan when that plan has phases; otherwise, it uses the selected run's plan.
   A missing plan adds nothing; a failed queue read keeps `queue unreadable`.

Workers first limits each cell to 80 characters.
Notes, Non-Phase Work, and Queued retain every word of each displayed item or title.
Each phase table calls `display_cells.wrapped()` to remove columns whose cells are all `—`.
At the pane width, or the floor without one, a cell that needs more lines than the cap continues on extra rows.
A split uses ` · ` first, then `; `, then a word boundary.
Unit, Status, Machine, and Progress never split.
Do not insert `<br>`; Claude Code displays it as text.
Empty cells show `—`.

For `Kind: style` or units named in the production's `Style units` table, the table is
`| Unit | Status | Ticket | Step | ETA | Findings built | Last 24 h | Findings left |`.

The style table cuts Step before Ticket to fit the width.
The number columns keep their right alignment when the table omits a column.

- **Ticket**: `#<n> · <rule> · <crate> · <work>`. The number drops the unit
  prefix. The rule is its file stem; an empty rule uses the first stem from
  the rules list, plus `+<k>` for the rest. Work is `<n> findings` for a
  proposal, `<n> sites` for listed work, or `<n> files` for read or structure
  work; 1 uses the singular. The oldest assigned or checkpointed ticket wins.
  With no ticket: `idle (<HH:MM>)` from the last refill, else `idle`.
- **Step**: in words, what the unit does now. A specific activity text from
  the newest run shows as it is (`testing on the Mac`). Otherwise `impl` →
  `writing <work>`, `fix` → `fixing review findings`, `review` → `reviewing`,
  `test` → `testing`, `checkpoint sent` → `waiting for the showrunner to merge`.
  With no open step: `starting <work>` for an assigned ticket, `waiting for the
  showrunner to merge` for a checkpointed one, else `idle — <outcome>` or `idle`.
- **ETA**: the current phase ETA as `<HH:MM>`, else `—`.
- **Findings built**: the all-time built count across the unit's configured families.
- **Last 24 h**: findings first built in the last 24 hours across those families.
- **Findings left**: filed findings with an approved decision across those families:
  `<approved>`, or `<approved> approved of <filed> filed` when not all filed
  findings are approved. A recorded split adds `: ready <a> · changed <b> ·
  structure <c> · other <d> (<age>)`. Age is whole minutes below 60 (`<n> min`),
  whole hours below 24 (`<n> h`), else whole days (`<n> d`), since the scan
  started. A split alone leaves the ticket cell as `idle`.

The total line is `All families: <built> findings built · <approved> approved
findings left[ of <filed> filed] · <r> built in the last 24 h · ETA at that rate
<YYYY-MM-DD>`. `<filed>` counts every filed finding, also one with no family,
and shows only when it differs. The date is now plus `approved / r` days.
When `r` is zero, it ends with `no ETA: nothing built in 24 h`. Numbers use
thousands commas. Times and the date use the production zone.

Read failures that make the table wrong or incomplete appear as `anomaly:` lines below the table.
Staff name conflicts also produce lines. Worker run checks share one process snapshot when needed.
A process with an identity but no staff row produces no line and cannot use a unit's pane placement.
A process without an identity reports an unreadable thread when its pane holds staff.
One staff candidate retains its pane placement with unknown status.
Morty produces no anomaly when its native ID is on the producer staff row named `morty`.
The command sends one message with all anomaly lines to `UNIT_ANOMALIES_TO` in `config/production.conf` (`sdlc`).
A failed send adds one anomaly line. It changes neither the table nor the exit code.

Use `agents` to pause, exit, or resume units:

1. Run `agents pause <unit> --why "<why>"` to stop its workers and hold its director at the prompt.
2. Run `agents exit <unit> --why "<why>"` to stop the director and its workers.
3. Add `--finish-step` to wait for the workers' current tasks before either action.
4. Run `agents pause --production <showrunner> [--except <unit> ...] --why "<why>"` to pause selected units.
5. Run `agents resume <unit>` or `agents resume --production <showrunner>` to resume units.

Each `--except` accepts a staff name, its `-unit` form, or a staff id within that production.
An unmatched value refuses the command before any row changes.
The refusal names the value and the production.
Pause and exit record the time, caller, and reason in the staff row.
A pause lets the director finish its current turn. It starts no new work.
An exit preserves the staff rows, tabs, and shells.
Resume removes the record. A paused director receives one continuation message.
An exited director starts on its configured role account.
Stop does not save or restore composer drafts.

The command reads process identities, terminal metadata, run records, decision files and read-only style ledgers.
It never reads pane contents. Missing ledgers or tables supply no data. Report a non-zero exit as one error line.
