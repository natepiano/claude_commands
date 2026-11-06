---
description: Transfer a unit between productions.
argument-hint: "<unit> <receiving showrunner name>"
---

# Transfer unit

Run from the giving showrunner session, when the receiving showrunner asks for the unit. `<unit>` is
the unit's id or its stem. `<receiving showrunner name>` is the receiving showrunner row's `name`. The script lives
at `~/.claude/scripts/production/transfer_unit.py`:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/transfer_unit.py" <plan|apply|verify> <showrunner name> <unit> <receiving showrunner name>
```

1. Run `plan` to check the unit and the receiving showrunner against their staff rows.
2. Run `apply` to change the unit's staff parent and move its tab to the receiving production.
3. Run `verify` to complete the transfer after a failed tab move.
4. Send the receiving showrunner the unit name, worktree and branch.
5. Tell the user that the unit now reports to the receiving showrunner.
