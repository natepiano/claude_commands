---
description: Demote a child showrunner to a unit.
argument-hint: "<child> --plan <path> [--port <n>] [--check]"
---

Run this command from the parent showrunner:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/demote_showrunner.py" <child> --plan <path> [--port <n>] [--check]
```

The child keeps its conversation and resumes as a unit on the units account.
Use `--port` to set the unit port.
Use `--check` to check the request without changes.
Show the script's output.
If the command fails, show its message and stop.
Use the same child, plan, and port for a rerun.
