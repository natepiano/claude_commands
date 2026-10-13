---
description: List every proposal still open for the user in one table - the production's proposals index, the plan backlog, the units' proposal docs and the add-ons waiting for a decision.
---

# Proposals

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposals_table.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer and prints one Markdown table,
`| Proposed by | Proposal | Date | State |`, from four sources in this order:

- **The proposals index**: each row of `index.md` in the folder the production
  doc's `Proposals` line names, unless its Status begins with a closed word
  (done, built, declined, dropped, superseded and the like). The row reads
  `<title> — <the summary up to its first semicolon>`, and its State is the
  Status as written. A production with no `Proposals` line has no index.
- **The plan backlog**: each line of `~/.local/state/plan-backlog/ledger.md`
  ending `proposed` or `deferred`, under the proposer its file names.
- **Proposal docs**: each `docs/plans/proposal-*.md` in the trunk and unit
  worktrees, while its `> **Status:` line says PROPOSAL. A doc with no status
  line counts as open.
- **Add-ons**: each entry a unit's run holds until it has an answer, under the
  unit's name. Its date is the day the unit last changed its list.

State says what the user can do: `waiting for your review` or `deferred until
you ask for it`. An add-on is not the user's to decide, so its State says who
does: `the showrunner decides: build, save for later, or drop`. The unit sends
its add-ons to the showrunner, whose answer settles each one
(`/showrunner:produce` → Throughout → Designs and add-ons).

The table prints what the units and the index say. When a row cannot be
understood as it reads, ask its unit to reword the entry's title as one plain
sentence; do not reword it here.

With nothing open it prints `no open proposals`. It reads only and writes
nothing. When it exits non-zero, report its error in one line.
