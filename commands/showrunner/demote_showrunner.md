---
description: Demote a child showrunner to a unit.
argument-hint: "<child> --plan <path> [--port <n>] [--check]"
---

Run this command from the parent showrunner:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/demote_showrunner.py" <child> --plan <path> [--port <n>] [--check]
```

The child keeps its conversation and resumes as a unit on the units account.
Relative `--plan` paths start at the child's checkout.

1. Use a plan committed on the child's merge branch.
2. Use `--port` to set the unit port.
3. Use `--check` to check the request without changes.
4. Show the script's output.
5. If the command fails, show its message and stop.
6. Use the same child, plan, and port for a rerun.
