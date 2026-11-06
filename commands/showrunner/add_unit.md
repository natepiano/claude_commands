---
description: Add and start a production unit.
argument-hint: '<name> (--plan <path> | --brief "<goal>" | --standby | --permanent) [--port <n>] [--responsibility <name>]'
---

# Add unit

Run this in the showrunner session for a running production. Give exactly one
of `--plan`, `--brief`, `--standby`, and `--permanent`. `--brief` is the user's goal in their own words; pass
it without rewriting it. `--permanent` is a standby unit that wrap never retires (the
nightly review).

Run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <showrunner name> <arguments>
```

Show the script's output. If it exits nonzero, show its message and stop.
New launches print `<unit> started: herdr session attach agents, workspace <space>, tab <name>`;
restores print `<unit> restore launched: ` and the same place. Every unit is a tab of the one
herdr session, `agents`.
The unit uses its staff name without an added suffix.
The launcher adopts an existing live staff row. It keeps the saved `.unit.toml` responsibilities and the port when omitted.
A retired name stays reserved: resume the unit or remove it for good before reusing the name.
Repeat `--responsibility <name>` for short duty names in a new configuration.
Admission writes the worktree to the staff row.
It writes the plan, port and responsibilities to untracked `.unit.toml`.
`--resume` keeps that file.
Add `--check` to run preflight without launching or writing anything.
An untrusted session folder refuses before any stub, staff row, or worktree is
written. Show the refusal's `Run:` command; the user runs it on the launch
account (in the main checkout for a new worktree), accepts the trust prompt,
types `/exit`, then retries this command.
The unit director launch takes its model and effort from `/default_models production.director`.
For a standby unit, send its work when assigned and set `plan` in its `.unit.toml`
to the plan path. Nothing else marks the unit as working: it
reads as running from the moment it starts its `/unit:direct` run, and the
stall watch watches it from then.
