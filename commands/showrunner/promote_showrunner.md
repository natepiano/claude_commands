---
description: Promote a unit to a child showrunner.
argument-hint: "<unit>"
---

**Usage:** `/showrunner:promote_showrunner <unit>`

Run this command from the parent showrunner:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/promote_showrunner.py" <unit> --model <your model id> --effort <your effort level>
```

The command uses the recorded plan when it is a production document.
Otherwise, it reads Markdown files throughout the unit's checkout, except under `.git` and `target`.
It uses the single valid production document.
An ambiguous default reports every candidate path.
It uses the unit name for the child showrunner.
Optional `--production` and `--doc` values override those defaults.
Pass your own model id and effort level.
Both options are required.
Use `--check` to inspect the request without changes.
The command preserves the conversation and retained workers.
Promotion removes the unit type from the showrunner row.
The recovery command includes the saved unit type.
It stops the unit before it starts the child on the showrunner account.
The child's document supplies its merge branch.
A failed restart prints a recovery command.
Show the script's output and any refusal.
