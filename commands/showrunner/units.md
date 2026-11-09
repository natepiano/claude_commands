---
description: List unit phases and style tickets.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/unit_phases.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer. Print its total line too, when present.
Both tables show only the production's staff units.
For the phase table, **Status** is `busy`, `idle`, `paused`, `exited`, or `on <machine>`.
The accounts board uses the same status rule. An exit record gives `exited`.
A pause record gives `paused`, even with no live session.
An active unit on another machine shows `on <machine>`. Exit and pause records still win.
A local live pane process without a Claude registry record gives `idle`.
With no live session or process, the status is `exited`. Otherwise the live session gives `busy` or `idle`.
The style table keeps `active`, `paused` and `exited` from staff records.
**Unit** is the unit's staff name, including stopped units and units without a session.

For a production without `Kind: style`, the table is `| Unit | Status | Phase | Non-Phase Work | Queued |`:

- **Phase**: `phase <n> of <total> — <step>: <what it does>` for the open phase.
  The task is the open step of the newest run. With no open step, use the phase title.
  Cut the cell to 80 characters with `…`. With no open phase, show
  `between phases, <done> of <total> done`, `not started`, `plan done`,
  `no phases` or `no plan`.
- **Non-Phase Work**: the newest open run whose phase is outside the plan,
  as `<phase id>: <open step>`, cut to 80 characters with `…`.
  Add ` +<k> more` for other open runs outside the plan. Show `—` when none is open.
- **Queued**: the first item from the director's `/unit:queue` list,
  cut to 80 characters with `…`. Add ` +<n> more` for later items.
  Show `—` when the list is empty.

For `Kind: style`, the table is
`| Unit | Status | Ticket | Step | ETA | Findings built | Last 24 h | Findings left |`:

- **Ticket**: `#<n> · <rule> · <crate> · <work>`. The number drops the unit
  prefix. The rule is its file stem; an empty rule uses the first stem from
  the rules list, plus `+<k>` for the rest. Work is `<n> findings` for a
  proposal, `<n> sites` for listed work, or `<n> files` for read or structure
  work; 1 uses the singular. The oldest assigned or checkpointed ticket wins.
  With no ticket: `idle — <outcome> (<HH:MM>)` from the last refill, else `idle`.
- **Step**: in words, what the unit does now. A specific activity text from
  the newest run shows as it is (`testing on the Mac`). Otherwise `impl` →
  `writing <work>`, `fix` → `fixing review findings`, `review` → `reviewing`,
  `test` → `testing`, `checkpoint sent` → `waiting for the showrunner to merge`.
  With no open step: `starting <work>` for an assigned ticket, `waiting for the
  showrunner to merge` for a checkpointed one, else `idle — <outcome>` or `idle`.
- **ETA**: the current phase ETA as `<HH:MM>`, else `—`.
- **Findings built**: the all-time built count for the unit's family.
- **Last 24 h**: findings first built in the last 24 hours.
- **Findings left**: the family's filed findings with an approved decision:
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
Staff name conflicts also produce lines. The command does not scan other processes.
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
