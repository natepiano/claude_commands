# Unit and showrunner rename

`agents rename OLD NEW` renames a unit, showrunner, or worker.
The staff record supplies the current name, parent, document, worktree, and native session.
The command keeps the staff ID.
It repairs displays through the row's current native session.
It keeps panes that run another session.

The command accepts a staff ID, `showrunner/name`, or a bare name.
A bare name first selects the calling showrunner or its own units.
If none matches, the command searches all showrunner, unit, and worker rows.
Several matches require a staff ID or `showrunner/name`.
A same-name request repairs differing displays.
A completed rerun prints `unchanged`.
An unavailable display produces an anomaly line and a successful exit.

## Optional document and branch move

Showrunners require the paired `--to-doc <path>` and `--to-branch <name>` options.
The target document must remain inside the current Git checkout.
The command reads the checkout and current branch from Git.
It reads the document path from staff.
It creates no rename journal.
It does not read or change legacy `rename.json` or `rename_unit.json` files.

1. Validate the requested name, document destination, and branch against current staff, files, and Git.
2. Change the staff name.
3. Move the document without changing its text.
4. Repair the Git index from its current source or destination entry.
5. Publish the new document path through `staff.set_doc`.
6. Rename the current branch with `git branch -m`.
7. Repair the session and display names.

Run the same command again after an interruption.
Staff supplies the current document path and name on each invocation.
When the source file moved before publication, the explicit destination and Git index identify the remaining work.
The index supplies the staged blob and file mode.
Recovery preserves staged and unstaged edits separately.
The command adds the destination index entry before it removes the source entry.
A missing source file does not require a saved checksum or step number.
The command reads the branch again before it changes the branch.
A completed document or branch step needs no repeated write.

A missing source does not permit an unrelated destination.
Recovery checks the destination against the Git tree and the current index.
Matching source and destination index entries identify an interrupted index update.
A staged Git rename identifies a completed move after source index removal.
A destination collision preserves the files and staff.
A target branch collision preserves the existing branch.
A wrapped document stays wrapped.
A completed retry changes no document, staff, or Git bytes or timestamps.
The showrunner path creates no commit and makes no push.
Legacy state removal remains a separate action after recovery.

## Unit branch move

Units accept `--to-branch <name>` without `--to-doc`.
`agents rename handler handler --to-branch cargo-liner-handler` changes only the branch.
A changed unit name also changes the matching folder suffix.
The explicit branch replaces the branch suffix change.
The rename keeps directors, workers, and their commands running.
A rename marker gives the tab its new sort position.
A label repair or tab restore keeps its saved position.

1. Read the current worktree from staff and the branch from Git.
2. Prepare the folder move through the shared unit move API.
3. Change the staff name and repair the displays.
4. Send the director and each worker one rename notice.
5. Move the folder and branch when current process evidence shows an idle worktree.
6. Push the target branch with an explicit refspec and `--set-upstream`.

A busy worktree keeps its folder and branch.
A later idle rerun finishes the pending movement.
The shared folder move state retains its existing recovery responsibilities.
The notice file contains a delivery key and pending recipient IDs.
It does not supply names or file paths.
Messages use the current staff name at delivery.
A second name change prepares all members with a new delivery key.
An interrupted rename can retry with either earlier notice file format.
A rerun prefers the caller's former name over a suffix inferred from the parent and branch.
The folder suffix and branch suffix change independently.
A custom branch without the former suffix stays unchanged.
A message timeout keeps that recipient pending and does not prevent later messages or movement.
The command removes completed notice state.

An explicit `unit_move.py move-folder` command keeps its stop, move, and resume behavior.
The command refuses `--to-doc` for a unit.
See `unit-rename.md` for the live rename behavior.

## Workers

A worker rename retains its staff ID, current conversation, parent, and backend.
The existing `staff.rename` writer calls `Herdr.retitle_row` for workers.
It renames proved worker panes and keeps their shared tab and workspace names.
An unavailable display produces one anomaly line after the staff rename.

## Scope and tests

Timers and stores retain their staff IDs during a rename.
The command edits no document fields, plan headers, prose, marks, or history.
Membership comes from staff parent relationships.
Folder-only moves use `unit_move.py move-folder --unit <staff id> --to <path>`.

The tests use private staff records, temporary repositories, and fake sessions.
They cover display repair, document moves, retained edits, index recovery, branch collisions, and an interrupted staff publication.
A legacy journal cannot change the result of a new rename.
