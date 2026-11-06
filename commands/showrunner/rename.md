---
description: Rename a showrunner row and its session and display names.
argument-hint: "plan|apply <old> <new> [--to-doc <path> --to-branch <name>]"
---

# Rename showrunner

Run from the showrunner session:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename.py" <arguments>
```

1. Run `plan <old> <new>` to inspect the row and display changes.
2. Run `apply <old> <new>` to change the row, session, tab, and workspace names.
3. Run `apply <new> <new>` to repair names after an interruption.

Use a staff id or a current staff name for `<old>`.
A same-name rename prints `unchanged` when all names agree.
Show each anomaly line and the final output.

Add both `--to-doc <path>` and `--to-branch <name>` only for an explicit document and branch move.
The command saves the move journal first and sets the staff document after the file moves.
Repeat the same command after an interruption.
The command creates no commit and makes no push.

See `docs/as-built/production-rename.md`.
