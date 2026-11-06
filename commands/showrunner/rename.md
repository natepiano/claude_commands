---
description: Rename a unit or showrunner and its current displays.
argument-hint: "plan|apply <old> <new> [--to-doc <path> --to-branch <name>]"
---

# Rename a unit or showrunner

Run `~/.claude/scripts/lib/py ~/.claude/scripts/production/rename.py <arguments>`.

1. Run `plan <old> <new>` to inspect the changes.
2. Run `apply <old> <new>` to make the changes.
3. Repeat the command after an interruption.

Use a staff id, `slug/name`, or a unique name.
A bare name first selects the calling showrunner or its units, then all productions.
A unit rename changes matching folder and branch suffixes and resumes its live sessions.
A showrunner rename repairs its session, tab, and workspace names.
Only showrunners accept the paired `--to-doc` and `--to-branch` options.
Show each anomaly and the final output.
The folder-only command stays: `unit_move.py move-folder --unit <staff id> --to <path>`.
See `docs/as-built/production-rename.md` and `docs/as-built/unit-rename.md`.
