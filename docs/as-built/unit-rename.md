# Unit rename

`agents rename OLD NEW` renames one staff unit.
The command reads the unit from its staff row.
Use a staff id, `showrunner/name`, or a unique name; see `production-rename.md`.
It keeps the staff id, current session, parent, and recorded role.
A pending rename finishes before the command sets the displays.
A same-name request sets the session, tab, and pane names to the staff name.
A finished rename prints `unchanged`.

A new-name rename changes the staff name and sets the displays.
It reads the folder from staff and the branch from Git.
It uses `unit_names.swap_stem` for each name independently.
It replaces the trailing `-<old name>` with `-<new name>`.
The folder keeps its parent folder.
A name without the suffix stays unchanged and prints one `anomaly:` line.
The other name still changes when its suffix matches.

The move stops the live unit and its live workers.
It moves the Git worktree and changes the branch while the sessions are stopped.
It writes the staff location and resumes the same current sessions in their panes.
A stopped session stays stopped.
A restart failure prints `agents restart --only <staff-id>`.
A retry reports stopped rows and keeps them stopped.
The move limits come from `config/production.conf` at use.

Use `unit_move.py move-folder --unit <staff id> --to <path>` to move only the folder.
The command refuses an existing destination or a concurrent row operation.
An open phase does not prevent a folder move.
Staff remains the folder source for runs, panes, recovery, and resume.
Claude transcripts and Codex threads retain their native ids and existing transcript locations.
The shared restart path can resume a previously live unit without an older launch record.

## Tests

`scripts/agents/test_shutdown_cli.py` covers row names, display repair, moves, and stopped-session recovery.
The `test_unit_move.py` module covers interruption recovery across the folder and branch operations.
Tests use private staff records and fake process and terminal evidence.
