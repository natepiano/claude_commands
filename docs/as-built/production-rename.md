# Unit and showrunner rename

`scripts/production/rename.py plan|apply <old> <new>` renames a unit or showrunner.
`plan <old> <new>` reads the request without writing.
`apply <old> <new>` calls `staff.rename` before the display repair.
The shared `rename.repair_displays` selects the row's current native session.
It keeps panes that run another session.

The command accepts a staff id, `showrunner/name`, or a bare name.
A bare name first selects the calling showrunner or its own units.
If none matches, the command searches all showrunner and unit rows.
Several matches require a staff id or `showrunner/name`.
A same-name request repairs differing names.
It prints `unchanged` only when the row and all available names agree.
An unavailable display produces one anomaly line and a successful exit.

## Optional document and branch move

Only showrunners accept the paired `--to-doc <path>` and `--to-branch <name>` options.
The target document must remain inside the current Git checkout.
The command reads the checkout and current branch from Git.
It reads the document path from staff.

1. Save the request in `<SHOWRUNNER_STATE_DIR>/<staff-id>/rename.json` before changes.
2. Change the row name.
3. Move the document without changing its text.
4. Call `staff.set_doc` with the new document path.
5. Rename the branch with `git branch -m`.
6. Mark the journal complete.
7. Repair the session and display names.

The journal records the paths, branch names, staff id, document checksum, and Git tracking state.
A retry uses this saved request after the source file moves.
It also repairs an interrupted Git index update.
The checksum prevents recovery from claiming a different target file.
A target file or target branch collision preserves the existing data.
A wrapped document stays wrapped.
A completed retry changes no journal bytes or timestamps.
The command creates no commit and makes no push.

## Scope and tests

Timers and stores keep their staff ids during a rename.
The command edits no document fields, plan headers, prose, marks, or history.
Unit renames change matching folder and branch suffixes; see `unit-rename.md`.
Folder-only moves use `unit_move.py move-folder --unit <staff id> --to <path>`.

`test_rename*.py` uses private staff records, temporary files, and a fake herdr.
The tests cover row rename, display repair, optional moves, and recovery after an interruption.
