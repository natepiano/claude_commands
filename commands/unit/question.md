---
description: Put a question for the user on the production's open-questions list without showing it to them now; the answer comes back through the showrunner when the user asks for it.
argument-hint: "<the question in full>"
---

# Question

A unit of a production uses this when it needs the user's answer: an action
that cannot be undone, or a choice only the user knows the answer to
(`~/.claude/docs/production_format.md` → <ProductionUnit/> item 2). The
question goes on the production's open-questions list. The status line's count
and `/next question` show it; no reply puts it in front of the user. User,
2026-10-28: "i asked it to add it to your question list not to bring it
directly to my attention".

A design choice or an add-on is the showrunner's, never this command's: send
those to the showrunner as before.

## Steps

1. **The production.** Read the `> **Production:**` line of your plan header:
   it names the production doc. Your unit name is your row's `Unit` cell in that
   doc's Units table, as written (for example `alpha-unit`). No production line
   means no production: end the turn on `— decision:` as `/unit:direct` says.
2. **The question in full.** Write it so that it stands alone when the user
   reads it days later, with nothing else in front of them:
   - what is asked, and where (the unit, the phase, the file or the screen);
   - why it is asked now, and what waits on it;
   - each choice, and what it does;
   - the recommended choice, and why: `(recommended — <reason>)`.
   Also write a one-line text for it, the name the lists use (for example
   `alpha-unit: keep the old key binding?`).
3. **File it.** Run
   ```sh
   python3 ~/.claude/scripts/production/update_registration.py ask --production <production doc> --unit <your unit> --text "<one line>" --question "<the full question>" [--urgency blocks]
   ```
   Pass `--urgency blocks` when the phase cannot go on without the answer. The
   question review runs as it does for a showrunner's question.
   - `ask: ok` and a `filed question:` line: it is on the list. The filed
     text is what the user sees. Do not send it to the showrunner, and do not
     put it in a reply to the user.
   - `question held for review:` (exit 2): the review needs more. Rewrite the
     question with what it names and run step 3 again.
   - `already open`: the same text is on the list; do not file it again.
4. **Go on.** Do the work that does not wait on the answer. When nothing else
   can run, end the turn on `— decision: filed for the user: <one line>`.
5. **The answer** arrives by message from the showrunner, beginning
   `From the user (via the showrunner):`. Act on it as on any of the user's
   words.
