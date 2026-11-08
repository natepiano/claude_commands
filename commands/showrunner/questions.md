---
description: List the production's open questions.
argument-hint: "[<production doc>]"
---

# Questions

1. Run the command below in the showrunner session.
2. Print its output exactly.
3. Report an error in one line when the command fails.

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py questions-table [--production <showrunner name>]
```

Without `--production`, the command finds the production through the session's update timer.

The table columns are `Asked by`, `Question`, `Date` and `State`. Only open question records appear. Proposals and reminders remain outside this table.

The asker label comes from the fixed staff id. A rename or transfer does not change the stored asker.

State shows whether the question waits for an answer or a deferred time. It also identifies an unavailable review.

With no open questions, the command prints `no open questions`. Use `/next question` to answer the current question.

For an existing JSON question list, run the store migration once:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/production/update_registration.py migrate-questions --production <showrunner name>
```

Migration writes question records and removes questions from the JSON list under one lock. Reminders remain in JSON.

A repeated migration changes nothing. An interrupted migration completes when repeated. Queue keys, times, reviews and deferrals remain unchanged.
