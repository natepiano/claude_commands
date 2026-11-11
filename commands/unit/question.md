---
description: Queue a unit's user question for review.
argument-hint: "<the question in full>"
---

# Question

A unit uses this command when it needs the user's answer.
This includes an action that cannot be undone or a choice only the user can make.
See `~/.claude/docs/production_format.md`, `<ProductionUnit/>` item 2.
The command adds the question to the production's open-questions list.
The status line count and `/next question` show the question.
Do not send a separate reply to the user.

Send design choices and proposed additions to the showrunner.

## Steps

1. Read your unit's staff row and its parent showrunner row.
   Use your unit's fixed staff `id` for `--unit`.
   The command reads the production document from the parent showrunner's `doc.path`.
   Do not pass a production document path or resolve one against your current directory.
   Without a parent showrunner, end the turn with `— decision:` as `/unit:direct` requires.
2. Write the full question with enough context for the user to read it days later.
   Include the unit, phase, file or screen, and the reason for the question.
   State which work needs the answer.
   Explain each choice and its result.
   Mark the recommended choice with `(recommended — <reason>)`.
   Add one line of text for the lists, such as `alpha: keep the old key binding?`.
3. Run the command:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --unit <your unit staff id> --text "<one line>" --question "<the full question>" [--urgency blocks]
   ```
   Pass `--urgency blocks` when the phase needs the answer to continue.
   The command reviews the question before it adds the question to the list.
   `ask: ok` and `filed question:` mean that the command added the question.
   The user sees the filed text.
   Do not send the filed text to the showrunner or repeat it in a reply to the user.
   For `question held for review:` (exit 2), rewrite the question with the missing facts and repeat this step.
   For `already open`, do not file the same text again.
4. Continue work that does not need the answer.
   When no work can continue, end the turn with `— decision: filed for the user: <one line>`.
5. Apply the answer that the showrunner sends with `From the user (via the showrunner):`.
   Treat the answer as the user's instruction.
