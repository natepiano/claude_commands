---
description: Add a unit to the running production, create its worktree, and start its unit director.
argument-hint: '<name> (--plan <path> | --brief "<goal>") [--port <n>] [--owns "<text>"]'
---

# Add unit

Run this in the showrunner session for a running production. Give exactly one
of `--plan` and `--brief`. `--brief` is the user's goal in their own words; pass
it without rewriting it.

Run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <PRODUCTION_DOC> <arguments>
```

Show the script's output. If it exits nonzero, show its message and stop.
