# Unit rename

`agents rename OLD NEW` renames one staff unit.
The command reads the unit from its staff row.
Use a staff id, `showrunner/name`, or a unique name; see `production-rename.md`.
It keeps the staff id, current session, parent, and recorded role.
A rename sets the staff name and the display labels before it moves the folder.
A same-name request sets the session, tab, and pane names to the staff name.
A finished rename prints `unchanged`.

A new-name rename changes the staff name and sets the displays.
It reads the folder from staff and the branch from Git.
It uses `unit_names.swap_stem` for each name independently.
It replaces the trailing `-<old name>` with `-<new name>`.
The folder keeps its parent folder.
A name without the suffix stays unchanged and prints one `anomaly:` line.
The other name still changes when its suffix matches.

A rename never stops or restarts the director, its workers, or their commands.
It sends the director and each worker one message with the new director name.
The command uses the shared send path and stable staff IDs.
The sender is `agents rename`.
A separate notice journal retains incomplete messages and the original name during a deferred folder move.
The command removes this journal when no message or folder move remains.
Document moves use their own journal.
One message timeout prints one anomaly line and keeps that recipient pending.
Other recipients and the folder move still run.
The send path can queue a message for a stopped member.
A message key prevents duplicate delivery if the command stops before it saves the delivery result.

The renamed tab loses its saved position and sorts as a new tab among its siblings.
Other tabs keep their saved order.
The layout retains the tab's staff ID and terminal binding.
The rename path saves a marker before it changes the staff name.
A label repair, tab restore, or title hook keeps the saved position.
A saved marker lets a rerun finish the sort after an interruption or a layout save failure.
The command removes the marker only after it saves the new order.

The folder and branch move only when the worktree is idle.
The command reads live process working directories, including child folders.
It also checks detached runs and verifies that each supervisor still owns its run record.
A saved run record alone does not defer the move.
When work runs, the command keeps the folder and branch and prints one line that explains the delay.
The pending move remains saved.
A rerun after the work ends moves the folder and branch and writes the staff location.
A stopped session stays stopped.
A retry reports stopped rows and keeps them stopped.
Git command limits come from `config/production.conf` at use.
A finished rerun prints one `unchanged` line.

Use `unit_move.py move-folder --unit <staff id> --to <path>` to move only the folder.
An explicit folder move retains its stop, move, and resume behavior, including when a rename remains pending.
The command refuses an existing destination or a concurrent row operation.
An open phase does not prevent a folder move.
Staff remains the folder source for runs, panes, recovery, and resume.
Claude transcripts and Codex threads retain their native ids and existing transcript locations.
The shared restart path can resume a previously live unit without an older launch record.

## Tests

`scripts/agents/test_shutdown_cli.py` covers row names, display repair, live messages, deferred moves, and idle recovery.
The `test_unit_move.py` module covers interruption recovery across the folder and branch operations.
The `test_herdr.py` module covers the new tab position and the saved order of other tabs.
Tests use private staff records, private processes, and fake sessions and terminals.
