---
description: Add a unit to the running production, create its worktree, and start its unit director.
argument-hint: '<name> (--plan <path> | --brief "<goal>" | --standby) [--port <n>] [--owns "<text>"]'
---

# Add unit

Run this in the showrunner session for a running production. Give exactly one
of `--plan`, `--brief`, and `--standby`. `--brief` is the user's goal in their own words; pass
it without rewriting it.

Run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <PRODUCTION_DOC> <arguments>
```

Show the script's output. If it exits nonzero, show its message and stop.
An existing Units row is adopted; its Port and Owns values stand when omitted.
Add `--check` to run preflight without launching or writing anything.
The unit director launch takes its model and effort from `/agent production.director`.
For a standby unit, send its work when assigned, set the Units row's Plan cell
to the plan path, commit and push that doc, then run
`$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/production/showrunners.py ready <showrunner session> --unit <name>`.
