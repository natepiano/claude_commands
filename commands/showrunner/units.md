---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's staff row. Print its total line too, when present.
Print the tables as Markdown in the reply, without code fences.
Keep each phase on one Markdown row so Claude Code wraps its cells within the terminal width.
Each table shows only the production's staff units, sorted by unit name without case distinctions.
For the phase status table, **Status** is `busy`, `starting`, `waiting`, `idle`, `paused` or `exited`.
The accounts board uses the same status rule.
An exit record gives `exited` before a pause record gives `paused`.
A busy pane gives `busy`. A starting pane gives `starting` before a live detached run gives `waiting`.
Without these activity states, a live session or pane process gives `idle`.
No live session or process gives `exited`.
The style table uses the same status words as the phase table.
**Unit** is the unit's staff name, including stopped units and units without a session.

The phase status table and the style table add **Machine** after **Status** only when a row runs on another machine.
The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.

Each table omits a column when every row has `—` in that column.
A table without rows keeps its columns.
The phase tables preserve full work text and use Claude Code's native Markdown wrapping.
Claude Code sets their column widths from the terminal width.
The style table uses `units_table_columns` in `config/production.conf`, which defaults to 170 terminal columns.
It allows four columns for the terminal margin and measures text before Markdown escaping.
Long style text cells end with `…`; a cut never splits a wide character.
Style column headers set the minimum text widths.
Fixed style columns and minimum text widths can exceed a smaller configured width.

For units outside the `Style units` table in a production without `Kind: style`, the command prints two tables.
Table 1 shows `| Unit | Status | [Machine] | Progress | Phase | ETA Next | [ETA Plan] | Workers |`.
Table 2 shows `| Unit | Notes | Non-Phase Work | Queued |`.
Both tables use the same unit order.
Each open phase of a unit's newest run gets one row in each table.
The current staff run pointer selects the unit run before newer completed ad hoc records.
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
   A finished newest run supplies no fallback.
3. **Notes** starts with `phase <id> — <phase title>` for the open phase.
   The phase instance in the newest run supplies the open step.
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
   Status keeps the session classifier even while Notes shows a wait.
4. **ETA Next** shows the latest stated promise for that row's phase instance.
   `eta_phase_id` names the target; the active instance on an ETA event does not override that target.
   Without open work, it uses the next not-started plan phase with a promise.
   A passed promise adds ` · late <elapsed>`.
   Open work without a promise shows `no ETA stated`; no open work or promised phase gives `—`.
   Times use the production zone: `HH:MM` today, `Sat 18:41` within six days, otherwise `24 Oct`.
5. **ETA Plan** shows the predicted plan finish in the same time form, without `ends`.
   It never precedes the latest promise of any open or not-started plan phase.
   The first row also names promises for unfinished targets without open rows, as `phase <id>: <time>[ · late <elapsed>]`.
   The latest `phase_finished` event for each target decides whether that target is done.
   Only status `completed` marks a target as done.
   A later `error` or `stopped` finish overrides earlier completions and plan-record done rows.
   That target's promise remains visible.
   A row without an unfinished plan or such a promise shows `—`.
6. **Workers** lists workers with open passes in that row's phase instance, from the run records.
   It lists workers with detached runs before workers that are working.
   Run states are `waiting on run: <label> <elapsed>`, `result queued: <label>`, and `run gone: <label>`.
   A finished pass removes its worker.
   Without an open phase row, the cell keeps the existing pane observation rules.
7. **Non-Phase Work** shows `—` for an open phase row because Notes already describes that phase.
   A row without a recorded phase instance keeps the existing summary for work outside the plan.
8. **Queued** combines the first `/unit:queue` item and `plan: phase <id>: <title>`.
   It excludes each queue item whose text equals Notes in the same row.
   Each part adds ` +<n> more` for later items.
   The queue count includes only remaining items.
   With no remaining queue item, it shows the plan part or `—`.
   Plan work includes every phase that is neither done nor open.
   Every open phase of the newest run stays outside Queued, including phases absent from the configured plan.
   It uses the configured plan when that plan has phases; otherwise, it uses the newest run's plan.
   A missing plan adds nothing; a failed queue read keeps `queue unreadable`.

Workers first limits each cell to 80 characters.
Notes, Non-Phase Work, and Queued retain every word of each displayed item or title.
Each phase table calls `display_cells.wrapped()` to remove columns whose cells are all `—`.
The helper preserves full cells for native Markdown wrapping.
Workers uses the same wrapping form when the status table exceeds the terminal width.
Do not insert `<br>` or continuation rows.
Claude Code displays `<br>` as text and adds row borders around continuation rows.
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
Morty produces no anomaly when its thread matches `MORTY_NATIVE_ID` in `config/send.conf`.
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
