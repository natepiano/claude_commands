---
description: Add and start a production unit.
argument-hint: "<name>"
---

**Usage:** `/showrunner:add_unit <name>`

Run this command from the parent showrunner:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/add_unit.py" --production <parent> <name>
```

Pass the current production name with `--production`.
The plan comes from the unit's saved configuration or its row in the production document.
A missing default reports its source paths.
Optional `--plan`, `--brief`, `--standby`, or `--permanent` selects the launch mode.
Pass brief text without rewriting it.
A permanent unit remains after wrap.
Hana units receive the lowest free BRP port in the configured unit range when no port is recorded.
Optional `--port` overrides the default for a new configuration.
Repeat `--responsibility <name>` for short duty names.
The launcher preserves an existing configuration and refuses conflicting values.
Every unit uses a tab in the shared `agents` session.
Admission writes the worktree to the staff row.
It writes the plan, port, and responsibilities to untracked `.unit.toml`.
The director profile comes from `/default_models production.director`.
Use `--resume` with `--cwd` to restore a stopped conversation.
Use `--check` to inspect the request without writes or launch.
For a trust refusal, show its `Run:` command.
The user runs that command on the launch account before a retry.
Show the script's output and any refusal.
