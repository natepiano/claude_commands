---
description: Give a running unit session a new name and update every production store that names it.
argument-hint: <old> <new>
---

# Rename unit

**Usage:** /showrunner:rename_unit <old> <new>

Use `PRODUCTION_DOC` and `SCRATCH` from `/showrunner:produce`. Run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename_unit.py" --production <PRODUCTION_DOC> --scratch <SCRATCH> <old> <new>
```

Show the script's output. If it exits nonzero, show its message and stop.
