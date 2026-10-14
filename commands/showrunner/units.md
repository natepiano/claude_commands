---
description: List the production's unit directors in one table, each with its session, current phase and task.
---

# Units

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/units_table.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer and prints one Markdown table,
`| Unit | Session | Phase | Task |`, one row per live unit:

- **Session**: the unit's session name now, or `gone` (no tmux session carries
  its mark), `not running` (Claude has stopped in it) or `unknown`.
- **Phase**: `<n> of <total> · <title>` for the open phase; otherwise
  `between phases, <done> of <total> done`, `not started`, `plan done`,
  `no phases` or `no plan`.
- **Task**: the step the unit's newest run has open in that phase, as
  `<step>: <what it does>`, or `—`.

It reads only run records and tmux marks, never a pane. When it exits non-zero,
report its error in one line.
