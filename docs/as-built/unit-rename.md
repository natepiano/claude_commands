# Unit rename

`agents rename OLD NEW` renames one staff unit.
The command reads the unit from its staff row.
Use a staff id, `showrunner/name`, or a unique name; see `production-rename.md`.
It keeps the staff id, current session, parent, and recorded role.
A rename sets the staff name and the display labels before it moves the folder.
A same-name request sets the session, tab, and pane names to the staff name.
A finished rename prints `unchanged`.

## Worker name migration

Run `agents rename OWNER OWNER` once per owner at deploy.
Use the existing rename command without a new program or an all-owner option.
The command checks direct worker rows of that unit or showrunner.
A dashed role from staff.WORKER_ROLES keeps its full name and optional trailing digits.
For example, the bare names can be astra-designer or astra-reviewer1481574.
Other workers use the last name component as the bare name.
The preceding prefix must equal the owner name or end with `-<owner name>`.
For example, `cargo-liner-handler-review4` under `handler` becomes `review4`.

Without a bare twin, the command sets an open agent's name and its pane label before the staff name.
It also sets a retained shell pane's label before the staff name.
It then writes the bare name through the existing staff rename path.
It keeps the worker's fixed ID, native conversation, and parent.
The pane label still comes from `agent_name(owner, worker)`.
A rerun after either label step completes the staff rename.
The bare row can then find its retained shell pane.

Only OpenWorker, StoppedWorker, or ClosedWorker evidence permits the staff rename without a bare twin.
Other observations retain the qualified staff name and the existing pane label.
One kept line names the row and gives the evidence reason.
A later run with recovered evidence completes the label and staff rename.
The shared pane readers retain their existing evidence diagnostics.

When a bare twin exists, the old qualified row must have a closed pane and no active dispatch.
The command then uses the existing worker end path to remove the old row.
An open pane, a retained shell pane, or an active dispatch keeps both rows.
One line names both rows and the reason.
Unavailable evidence also keeps both rows.
A StaffRefused exception from worker.end keeps both rows.
One line names both rows and includes the refusal text.
The command continues with later workers and owner displays.

A prefix such as `widget-enhancements` does not end with the current owner name `widget`.
The command keeps `widget-enhancements-impl` and prints `/agents rename <id> impl`.
A repeated run changes no completed row.
It repeats the same explanation for an unresolved row and prints `unchanged` when nothing changes.

## Director rename

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
A separate notice file keeps a delivery key and pending recipient IDs.
Messages use the current staff name at delivery.
A second name change prepares all members again with a new delivery key.
Each member receives the final name, including members that received an earlier name.
The command removes this file when no message or folder move remains.
Document moves use current staff, files, and Git evidence.
The command ignores legacy rename journals.
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
