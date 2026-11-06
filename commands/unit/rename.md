---
description: Rename a unit through its showrunner.
---

# Rename a unit

Read `~/.claude/commands/showrunner/rename_unit.md` in full.
Use the supplied arguments: `$ARGUMENTS`.
Run the command from the owning showrunner session.
A same-name rename only sets the displays.
A new-name rename changes matching folder and branch suffixes.
The move stops and resumes the live unit and its live workers.
A restart failure prints `shutdown.py restart --only <staff-id>` for manual recovery.
A rerun reports stopped rows and keeps them stopped.
