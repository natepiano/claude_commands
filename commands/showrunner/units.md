---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's staff row. Print its total line too, when present.
Both tables show only the production's staff units, sorted by unit name without case distinctions.
For the phase table, **Status** is `busy`, `idle`, `paused` or `exited`.
The accounts board uses the same status rule. An exit record gives `exited`.
A pause record gives `paused`, even with no live session.
Exit and pause records take priority.
A local live pane process without a Claude registry record gives `idle`.
With no live session or process, the status is `exited`. Otherwise the live session gives `busy` or `idle`.
The style table uses the same status words as the phase table.
**Unit** is the unit's staff name, including stopped units and units without a session.

Both tables add a **Machine** column after **Status** only when a row runs on another machine.
The home machine is the `kind.<machine>=local` entry in `config/build_targets.conf`.

For units outside the `Style units` table in a production without `Kind: style`, the table is:
`| Unit | Status | Progress | Phase | Notes | ETA Next | ETA Plan | Workers | Non-Phase Work | Queued |`.
ETA Plan appears only when at least one row has unfinished plan phases.

1. **Progress** shows only a bar: `🟩` done, `🟨` open, and `⬜` not started.
   It uses one cell per phase, or 20 equal groups above 20 phases.
   An open phase takes priority over not started, then done.
   All three characters use the same display width.
   With no recorded phases, Progress gives `—`.
2. **Phase** shows `<done>/<total>` from the same phases as Progress, or `—` with no phases.
   When the plan has no phases, an open run supplies Progress, Phase, and both ETA cells from its recorded phases.
   A finished newest run supplies no fallback.
3. **Notes** shows `phase <id> — <step>: <task>` for the open phase.
   The newest run supplies the open step; no step uses the phase title.
   Other states are `between phases`, `not started`, `plan done`, `no phases`, and `no plan`.
   Repair rounds append ` · fix rounds <n>[ + 1 running]` when the count exceeds zero.
4. **ETA Next** shows the latest promise for the open plan phase first.
   Without an open plan phase, it uses the newest open phase outside the plan.
   Without open work, it uses the next not-started plan phase with a promise.
   A passed promise adds ` · late <elapsed>`.
   Open work without a promise shows `no ETA`; no open work or promised phase gives `—`.
   Times use the production zone: `HH:MM` today, `Sat 18:41` within six days, otherwise `24 Oct`.
5. **ETA Plan** shows the predicted plan finish in the same time form, without `ends`.
   It never precedes the latest promise of any open or not-started plan phase.
   A row without an unfinished plan shows `—`.
6. **Workers** lists workers with runs before workers that are working.
   Run states are `waiting on run: <label> <elapsed>`, `result queued: <label>`, and `run gone: <label>`.
   Idle workers append ` +<n> idle` to a nonempty cell.
   Closed workers and workers on another machine add nothing.
7. **Non-Phase Work** shows the newest open phase outside the plan, with ` +<k> more` for other open phases.
8. **Queued** combines the first `/unit:queue` item and `plan: phase <id>: <title>`.
   Each part adds ` +<n> more` for later items.
   Plan work includes every phase that is neither done nor open.
   It uses the configured plan when that plan has phases; otherwise, it uses the newest run's plan.
   A missing plan adds nothing; a failed queue read keeps `queue unreadable`.

Notes, ETA Next, ETA Plan, and Workers use at most 80 characters per cell, with `…` for omitted text.
The Progress bar has no text cut.
Queued limits each item or title to 80 characters and keeps its count suffix after the cut.
Empty cells show `—`.

For `Kind: style` or units named in the production's `Style units` table, the table is
`| Unit | Status | Ticket | Step | ETA | Findings built | Last 24 h | Findings left |`:

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
