---
description: Review one pending item with the user.
argument-hint: "[change | proposal | efficiency | question] [oldest | newest] [all-below]"
---

# Next

**Usage:** `/next` · `/next <kind>` · `/next [<kind>] oldest` · `/next [<kind>] newest` ·
`/next [<kind>] [oldest|newest] all-below` · `/next to the end`

Kinds: `change`, `proposal`, `efficiency`, `question`. The words "next", "next change", "next
proposal", "next efficiency proposal", "next question", "next oldest" and "next newest" mean this
command too.

- No kind: the most urgent item of all four kinds.
- A kind: the most urgent item of that kind.
- `oldest` or `newest`, alone or with a kind: the oldest or newest item instead (user,
  2026-10-22).
- `all-below`, with any of the above: also the items of every production below this one.

Any other argument: print the usage line and stop.

The user asks for one item and works on that one. Show it in full, wait for
their answer, record the answer, then stop. Never show the item after it, also
when the answer settles it or more items wait: the user asks for the next one
when they want it (user, 2026-10-20). The user can also move the shown item to
the end of its list (user, 2026-10-22).

Every kind is scoped to one showrunner: the items of its own production
only, from its own unit directors. It never shows the items of the showrunner
above it, and by default not those of the showrunners below it, which show
their own. So each showrunner's review is complete for its level (user,
2026-10-27). `all-below` adds every production below it, at any depth. The
user runs `/next` in the showrunner whose work they want. Run it in the
showrunner session, or pass the showrunner name.

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

1. Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/next_item.py" show [--kind <kind>] [--order oldest|newest] [--all-below]`.
2. Add `--production <showrunner name>` outside the showrunner session, for every kind.
3. Judge the item against current events.
4. Print its verdict line only for `obsolete` or `in conflict`.
5. Present the item under its kind's section below.

The command prints the item's kind, then the item or a message that no item waits.

## Current events

Judge every item of every kind before you show it (user, 2026-11-05). The item
may be days old. The work around it may have changed. Use what you already know
first, read git only for the files the item names, and start no worker.

Check, and stop when the answer is clear:

1. The open plans, and the current phase of the owning unit (`/showrunner:units`).
2. Merges since the item's date: `git log --since=<date> -- <files it names>`.
3. The user's recent rulings: your memory and this session.

Classify the item:

- `current`: nothing since its date changes it. Print no verdict line.
- `obsolete`: the work is done, removed or replaced.
- `in conflict`: it clashes with work in progress or a ruling, such as a tuning
  proposal for code the user is redoing. Name that work.

For `obsolete` or `in conflict`, print one verdict line with its reason and evidence (commit, plan, ruling or phase).
Recommend the kind's own answer: for `current`, the answer the item deserves; for `obsolete`, close it.
For `in conflict`, defer it, decline it or reshape it.
Show an obsolete item too: the user answers it.
The one exception is an efficiency item already fixed, which its section records as done.

## To the end

When the user says to move the shown item to the end of its list, run
`next_item.py move-to-end --kind <its kind>`, with the same `--production` and `--all-below`. Say it moved, and stop: do not
show the next item.

## Important

When the user calls an item important, mark it: `mark-important` on its
kind's script (below).

## change

The next change for the user to check, from its current and earlier lists.
A row follows the staff id in its Producer cell. With no Producer and no verdict,
it follows a staff id in its Production cell. A unit id gives that unit's current
production, and a showrunner id gives that showrunner's production. A Producer id
with no staff row is unrouted. Any other row belongs to its list's production.
Mark: `change_review.py mark-important --row <id>`.
Pass the ID as `/next` shows it, such as `C434` or `sdlc/C9`. Say
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

The open nightly-review proposal: its line in the ledger that the `Efficiency
ledger` line names, of the production doc (with `all-below`, also of each production below it). A
production with no such line has no efficiency items. Each night's reports are
in `<night>/config.md`, `<night>/rust.md` and `<night>/messages.md` beside the
ledger. Where a cheap check exists (its measurement script, the line it cites),
confirm the candidate still holds; record one already fixed as done.

Present only this item; never list the backlog.
After the current-events check, give the current and proposed lines (file:line), then the measured gain and the cost.
End with one choice line, based on that check:
`approve (recommended — reason) / decline / defer / elaborate`.
For a fix that belongs to another session, offer `relay` in place of `approve`.

Record the outcome on the candidate's own ledger line as soon as it is decided:
its `— proposed` becomes `— accepted <date>, applied (<repo> <sha>)`,
`— declined`, `— deferred`, `— done <date>` or `— relayed <date>`. Apply an
approved change; commit and push only when the user says so.

## question

The current open question. Mark: `update_registration.py mark-important
--production <showrunner name> --item "<its text>"`. Ask it in full: what is asked,
why, and the answer choices. Act on the user's answer as `/showrunner:produce` →
Throughout says, and remove the item through its Open-questions list.

An item shown with `asked by <unit>` was filed with `/unit:question` or
`/showrunner:question`. When the user answers it, run `update_registration.py
answer --production <showrunner name> --item "<its text>"`. It removes the item and
prints whom the answer goes to:

- `relay: to <unit>`: send the unit the answer, beginning
  `From the user (via the showrunner): `, with the question's one-line text.
- `relay: to the showrunner of <showrunner name>, for <unit>`: the item is a production
  below this one (`all-below`). Send the answer to that production's
  showrunner, beginning `From the user (via <your name>): `, naming the unit
  and the item. That showrunner relays it to its unit.
- `relay: none`: act on the answer here.
