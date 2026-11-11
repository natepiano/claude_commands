---
description: Demote a child showrunner to a unit.
argument-hint: "<child>"
---

Run this command from the parent showrunner:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/demote_showrunner.py" <child>
```

The child keeps its conversation and resumes on the units account.
The plan comes from its saved configuration or production document.
Hana units receive the lowest free BRP port in the configured unit range when no port is recorded.
Optional `--plan` and `--port` values override the defaults.
Relative plan paths start at the child's checkout.
Use `--check` to inspect the request without changes.
Repeat the child name to continue a pending demotion.
Show the script's output and any refusal.

A pending demotion keeps the stopped showrunner notifier for restoration.
A completed demotion removes that notifier, its generated prompt and its counts cache.
The command keeps reminders and history records.
It marks the old production document ended and commits only that document on the unit branch.
