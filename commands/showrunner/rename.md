---
description: Rename a unit or showrunner and its current displays.
argument-hint: "plan|apply <old> <new> [--to-branch <name>] [--to-doc <path>]"
---

# Rename a unit or showrunner

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/rename.py <arguments>`.

1. Run `plan <old> <new>` to inspect the changes.
2. Run `apply <old> <new>` to make the changes.
3. Repeat the command after an interruption.

Use a staff id, `slug/name`, or a unique name.
A bare name first selects the calling showrunner or its units, then all productions.
A unit rename changes matching folder and branch suffixes.
Every unit branch change pushes the target to origin before live sessions resume.
A showrunner rename repairs its session, tab, and workspace names.
Use `--to-branch <name>` for a unit's explicit target branch.
Keep `<old>` and `<new>` equal to change only its branch.
Showrunners require `--to-doc <path>` with `--to-branch <name>`.
Show each anomaly and the final output.
The folder-only command stays: `unit_move.py move-folder --unit <staff id> --to <path>`.
See `docs/as-built/production-rename.md` and `docs/as-built/unit-rename.md`.
