---
description: Show the user the next change to check, proposal to review, efficiency proposal or open question, settle that one item, then stop.
argument-hint: "change | proposal | efficiency | question"
---

# Next

**Usage:** `/next change` · `/next proposal` · `/next efficiency` · `/next question`

The words "next change", "next proposal", "next efficiency proposal" and "next
question" mean this command too.

The user asks for one item and works on that one. Show it in full, wait for
their answer, record the answer, then stop. Never show the item after it, also
when the answer settles it or more items wait: the user asks for the next one
when they want it (user, 2026-10-20). Any other argument, or none: print the
usage line and stop.

Change, proposal and question run in the showrunner session. Efficiency runs
in any session.

Order: every queue shows the most useful item first, by
`~/.claude/scripts/lib/urgency.py`. First what the user called important (the
newest mark first), then what blocks work, then what is due soon (soonest
first), then normal, then later. Inside a level: the higher value first, then
the oldest. An item with a prerequisite comes after it. The item shown stays
current until it has its answer, also when a more urgent item arrives.

When the user calls an item important, mark it: `mark-important` on its
queue's script (below).

## change

The next change for the user to check, from the top-level showrunner's change
review list. Run `/change_review` → show; it prints the current row.
Mark: `change_review.py mark-important --row C<n>`. Say what changed, how to try it and what the user sees, in plain words.
Record the user's verdict through `/change_review` → verdict.

## proposal

The next proposal waiting for the user's review. Run `"$HOME/.claude/scripts/lib/py"
"$HOME/.claude/scripts/production/proposals_table.py" --next`; it prints the
proposal and its file, or `no proposal is waiting for your review`. Read the
proposal in full: an issue is that file, and a plan backlog line in the
ledger names its file. Mark: `proposal_issue.py mark-important <issue>`. Lead with its P number and
title, then say what it changes and why in plain words, then its proposed
ranking values in one plain-text line. Record
the user's answer through `/showrunner:proposal`.

## efficiency

The open nightly-review proposal with the largest measured gain. Run
`/efficiency_next`; it presents one candidate and records its outcome.

## question

The current open question. Run `python3
~/.claude/scripts/production/update_registration.py next --production
PRODUCTION_DOC`; it prints one item, or `no question is open`. Mark:
`update_registration.py mark-important --production PRODUCTION_DOC --item
"<its text>"`. Ask it in full: what is asked, why, and the answer choices. Act on the
user's answer as `/showrunner:produce` → Throughout says, and remove the item
through its Open-questions list.
