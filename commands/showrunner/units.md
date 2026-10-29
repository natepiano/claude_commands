---
description: List the production's units with their phases or style tickets.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/units_table.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer. Print its total line too, when present.
Both tables have one row per live unit. **Unit** is the session name now, or
`gone` (no herdr session carries its mark), `not running` (Claude has stopped)
or `unknown`.

For a production without `Kind: style`, the table is `| Unit | Phase | Task |`:

- **Phase**: `<n> of <total> · <title>` for the open phase; otherwise
  `between phases, <done> of <total> done`, `not started`, `plan done`,
  `no phases` or `no plan`.
- **Task**: the step the unit's newest run has open in that phase, as
  `<step>: <what it does>`, cut to 80 characters with `…`, or `—`.

For `Kind: style`, the table is
`| Unit | Ticket | Step | ETA | Findings built (24 h) | Findings left |`:

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
- **Findings built (24 h)**: `<all time> (<n>)` for the unit's family; `<n>`
  counts findings first built in the last 24 h.
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

It reads session marks, run records, decision files and read-only style ledgers.
It never reads a pane. Missing ledgers or tables supply no data. When it exits
non-zero, report its error in one line.
