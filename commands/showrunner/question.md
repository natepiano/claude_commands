---
description: Put a question for the user on this production's open-questions list without showing it in a reply, for a unit that asked or for the showrunner itself; the answer goes back through /next question.
argument-hint: "[<unit>] <the question in full>"
---

# Question

The showrunner uses this when a question for the user should wait on the
open-questions list and not come up in a reply. Use it:

- when a unit director sends you a question for the user: file it for that
  unit (most units file their own with `/unit:question`);
- when the user asked you to add a question to the list, not to ask it now.

The status line's count and `/next question` show the question. User,
2026-10-28: "i asked it to add it to your question list not to bring it
directly to my attention". A question you settle yourself under
<DecisionEconomy/> is never filed.

## Steps

1. **The question in full**, so it stands alone when the user reads it later:
   what is asked and where, why, each choice and what it does, and the
   recommended choice with its reason. Keep the unit's words where they are
   complete. Write a one-line text for the lists, led by the unit when a unit
   asked (`alpha-unit: keep the old key binding?`).
2. **File it.** Run
   ```sh
   python3 ~/.claude/scripts/production/update_registration.py ask --production PRODUCTION_DOC [--unit <unit>] --text "<one line>" --question "<the full question>" [--urgency blocks]
   ```
   `--unit` names the asking unit as the Units table writes it; leave it out
   for your own question. The question review runs as for `questions`.
   - `ask: ok`: it is on this production's list. Put nothing about it in your
     reply to the user. Tell the unit in one line that it is filed, if the unit
     sent it to you.
   - `question held for review:` (exit 2): rewrite it with what the review
     names, or send that back to the unit, and file it again.
3. **Your state.** Add the item to `open_for_user` in `SHOWRUNNER_STATE` the
   next time you rewrite the list. `questions` keeps a filed item even when
   your list leaves it out, and says `kept`; it leaves the list only through
   `answer` (`/next` → question).

The answer: when the user answers it through `/next question`, `answer` names
whom to send it to (`/next` → question).
