---
description: List every proposal still open for the user in one table - the plan backlog, the units' proposal docs and the add-ons waiting for next or drop.
---

# Proposals

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposals_table.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer and prints one Markdown table,
`| Proposed by | Proposal | Date | State |`, from three sources in this order:

- **The plan backlog**: each line of `~/.local/state/plan-backlog/ledger.md`
  ending `proposed` or `deferred`, under the proposer its file names.
- **Proposal docs**: each `docs/plans/proposal-*.md` in the trunk and unit
  worktrees, while its `> **Status:` line says PROPOSAL. A doc with no status
  line counts as open.
- **Add-ons**: each entry a unit's run holds for the user's `next` or `drop`,
  under the unit's name, with state `add-on: next or drop`. Its date is the
  day the unit last changed its list.

With nothing open it prints `no open proposals`. It reads only and writes
nothing. When it exits non-zero, report its error in one line.
