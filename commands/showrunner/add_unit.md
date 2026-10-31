---
description: Add a unit to the running production, create its worktree, and start its unit director.
argument-hint: '<name> (--plan <path> | --brief "<goal>" | --standby | --permanent) [--port <n>] [--owns "<text>"]'
---

# Add unit

Run this in the showrunner session for a running production. Give exactly one
of `--plan`, `--brief`, `--standby`, and `--permanent`. `--brief` is the user's goal in their own words; pass
it without rewriting it. `--permanent` is a standby unit that wrap never retires (the
nightly review).

Run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <PRODUCTION_DOC> <arguments>
```

Show the script's output. If it exits nonzero, show its message and stop.
New launches print `<unit> started: herdr session attach agents, workspace <space>, tab <name>`;
restores print `<unit> restore launched: ` and the same place. Every unit is a tab of the one
herdr session, `agents`.
A name may be given with or without its `-unit` ending; `feature-log` and
`feature-log-unit` both make unit `feature-log-unit`.
Unit short names are unique across all productions; `feature-log` uses
`~/worktrees/feature-log` and branch `feature-log`.
An existing Units row is adopted; its Port and Owns values stand when omitted.
Add `--check` to run preflight without launching or writing anything.
An untrusted session folder refuses before any stub, Units row, or worktree is
written. Show the refusal's `Run:` command; the user runs it on the launch
account (in the main checkout for a new worktree), accepts the trust prompt,
types `/exit`, then retries this command.
The unit director launch takes its model and effort from `/agent production.director`.
For a standby unit, send its work when assigned, set the Units row's Plan cell
to the plan path, and commit that doc on the merge branch without pushing it (it
goes out with your next merge push). Nothing else marks the unit as working: it
reads as running from the moment it starts its `/unit:direct` run, and the
stall watch watches it from then.
