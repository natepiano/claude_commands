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

## change

The next change for the user to check, from the top-level showrunner's change
review list. Run `/change_review` → show; it prints the first row still to
review. Say what changed, how to try it and what the user sees, in plain words.
Record the user's verdict through `/change_review` → verdict.

## proposal

The next proposal waiting for the user's review. Run `/showrunner:proposals`
and take the first row whose State is `waiting for your review`; with none,
say `no proposal is waiting for your review`. Read the proposal in full: an
issue is `<title>.md` in the folder the production doc's `Proposals` line
names, and a plan backlog line names its file. Say what it changes and why in
plain words, then its proposed ranking values in one plain-text line. Record
the user's answer through `/showrunner:proposal`.

## efficiency

The open nightly-review proposal with the largest measured gain. Run
`/efficiency_next`; it presents one candidate and records its outcome.

## question

The oldest open question that is due. Run `python3
~/.claude/scripts/production/update_registration.py open --production
PRODUCTION_DOC` and take its item 1 only; with none, say `no question is
open`. Ask it in full: what is asked, why, and the answer choices. Act on the
user's answer as `/showrunner:produce` → Throughout says, and remove the item
through its Open-questions list.
