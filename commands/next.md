---
description: Show the user the one most urgent item waiting for them, or the most urgent of one kind, or the oldest or newest; settle that one item, then stop.
argument-hint: "[change | proposal | efficiency | question] [oldest | newest]"
---

# Next

**Usage:** `/next` · `/next <kind>` · `/next [<kind>] oldest` · `/next [<kind>] newest` ·
`/next to the end`

Kinds: `change`, `proposal`, `efficiency`, `question`. The words "next", "next change", "next
proposal", "next efficiency proposal", "next question", "next oldest" and "next newest" mean this
command too.

- No kind: the most urgent item of all four kinds.
- A kind: the most urgent item of that kind.
- `oldest` or `newest`, alone or with a kind: the oldest or newest item instead (user,
  2026-10-22).

Any other argument: print the usage line and stop.

The user asks for one item and works on that one. Show it in full, wait for
their answer, record the answer, then stop. Never show the item after it, also
when the answer settles it or more items wait: the user asks for the next one
when they want it (user, 2026-10-20). The user can also move the shown item to
the end of its list (user, 2026-10-22).

No kind, change, proposal and question run in the showrunner session. Efficiency
runs in any session.

## Order

One order for all four kinds, from `~/.claude/scripts/lib/urgency.py`:

1. What the user called important, the newest mark first.
2. What blocks work.
3. What is due soon, the soonest due first.
4. Normal.
5. Later.

Inside a level the higher value goes first where a kind has one (a proposal's
score, an efficiency gain), then the oldest. Across kinds a value never
compares: the level decides, then the oldest. `oldest` and `newest` order by
age, and the level breaks a tie. An item with a prerequisite comes after it.

An item the user moved to the end goes after every other item of its kind, in
each order, until the user calls it important again. The item shown stays
current until it has its answer, also when a more urgent item arrives.

## Show

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/next_item.py" show
[--kind <kind>] [--order oldest|newest]`. Outside the showrunner session add
`--production <doc>`. It prints the item's kind on the first line, then the
item, or a line that nothing waits. Present the item by its kind's section
below.

## To the end

When the user says to move the shown item to the end of its list, run
`next_item.py move-to-end --kind <its kind>`. Say it moved, and stop: do not
show the next item.

## Important

When the user calls an item important, mark it: `mark-important` on its
kind's script (below).

## change

The next change for the user to check, from the top-level showrunner's change
review list: its row. Mark: `change_review.py mark-important --row C<n>`. Say
what changed, how to try it and what the user sees, in plain words. Record the
user's verdict through `/change_review` → verdict.

## proposal

The next proposal waiting for the user's review: its title, then its file. Read
the proposal in full: an issue is that file, and a plan backlog line in the
ledger names its file. Mark: `proposal_issue.py mark-important <issue>`. Lead
with its P number and title, then say what it changes and why in plain words,
then its proposed ranking values in one plain-text line. Record the user's
answer through `/showrunner:proposal`.

## efficiency

The open nightly-review proposal: its ledger line. Present and record it by
`/efficiency_next` steps 3 and 4.

## question

The current open question. Mark: `update_registration.py mark-important
--production PRODUCTION_DOC --item "<its text>"`. Ask it in full: what is asked,
why, and the answer choices. Act on the user's answer as `/showrunner:produce` →
Throughout says, and remove the item through its Open-questions list.
