---
description: List every proposal still open for the user in one table - the production's open proposal issues, the plan backlog and the add-ons waiting for a decision.
---

# Proposals

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/proposals_table.py"`
and print its output exactly as it comes, with nothing added. It finds the
production from this session's update timer and prints one Markdown table,
`| Proposed by | Proposal | Date | State |`, from three sources in this order:

- **Issues**: each issue directly in the folder the production doc's
  `Proposals` line names, while its frontmatter's `status` is `open` and its
  `tags` hold `proposal`, and its `proposed_by` names this production, one of
  its units, a production below it, or a unit of one (`unit (production)`
  names the production). Productions share one issues folder, so the others'
  proposals are left out. The row is the issue's P number and title
  under its `proposed_by`, dated by its `date_created`. A production with no
  `Proposals` line has no issue rows.
  Each style decision is one row for one rule and crate under one rule text.
  Rule candidates are not proposals: `style_proposals.py candidates` lists them,
  and `style_proposals.py drop-candidate --idea "<text>"` drops one.
- **The plan backlog**: each line of `~/.local/state/plan-backlog/ledger.md`
  ending `proposed` or `deferred`, under the proposer its file names.
- **Add-ons**: each entry a unit's run holds until it has an answer, under the
  unit's name. Its date is the day the unit last changed its list.

State says what the user can do: `waiting for your review`, `in progress by
<unit>` (or `in progress`) or `deferred until you ask for it`. Waiting issue rows
come before in-progress ones. An add-on is not the user's to decide, so its State says who
does: `the showrunner decides: build, save for later, or drop`. The unit sends
its add-ons to the showrunner, whose answer settles each one
(`/showrunner:produce` → Throughout → Designs and add-ons).

The table prints what the units and the issues say. When a row cannot be
understood as it reads, ask its unit to reword the entry's title as one plain
sentence; do not reword it here.

With nothing open it prints `no open proposals`. It reads only and writes
nothing. When it exits non-zero, report its error in one line.
