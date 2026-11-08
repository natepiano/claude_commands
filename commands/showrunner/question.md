---
description: Queue a user question for later review.
argument-hint: "[<unit staff id>] <the question in full>"
---

# Question

Use this command for a unit's question or your own question. Do not file questions you can decide under <DecisionEconomy/>.

1. Write the full question with its context, choices, recommendation and reason.
2. Write one short title for the lists.
3. Resolve the asker through the staff record.
4. Run the command below.
5. Tell the asking unit that you filed its question.
6. Add the question to your next `open_for_user` state.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py ask --production <showrunner name> [--unit <unit staff id>] --text "<title>" --question "<full question>" [--urgency blocks]
```

Omit `--unit` for your own question. A current unit label is also accepted. The stored `asked_by` always holds the fixed staff id.

`ask: ok` means the question is filed. Add nothing about it to your reply to the user.

`question held for review:` means the question needs correction. Correct the named defects before you file it again.

Questions are separate records beside proposals in the production's `Proposals` folder. `/showrunner:questions` lists them. Reminders remain separate.

The status count and `/next question` retain their behavior. The queue keeps a filed question until `answer` removes it.

After the user answers, follow the `answer` relay line. It resolves the asker's current staff row after a rename or transfer.
