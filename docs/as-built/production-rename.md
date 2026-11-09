# Unit and showrunner rename

`agents rename OLD NEW` renames a unit, showrunner, or worker.
The command calls `staff.rename` before display repair.
`shutdown.rename_repair_displays` selects the row's current native session.
It keeps panes that run another session.

The command accepts a staff id, `showrunner/name`, or a bare name.
A bare name first selects the calling showrunner or its own units.
If none matches, the command searches all showrunner, unit, and worker rows.
Several matches require a staff id or `showrunner/name`.
A same-name request repairs differing names.
It prints `unchanged` only when the row and all available names agree.
An unavailable display produces one anomaly line and a successful exit.

## Optional document and branch move

Showrunners require the paired `--to-doc <path>` and `--to-branch <name>` options.
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
The showrunner path creates no commit and makes no push.

## Unit branch move

Units accept `--to-branch <name>` without `--to-doc`.
`agents rename handler handler --to-branch cargo-liner-handler` changes only the branch.
A changed unit name also changes the matching folder suffix.
The explicit branch replaces the branch suffix change.
Every unit branch change pushes the target with `--set-upstream` before live sessions resume.
The explicit refspec `<branch>:refs/heads/<branch>` prevents push configuration from changing the target.
Units and showrunners use the same branch name and collision checks.

1. Save the unit move through `unit_move.prepare_move`.
2. Change the staff name if the request changes it.
3. Repair the session and display names.
4. Stop live sessions through `unit_move.move_folder`.
5. Move the folder if the request changes its suffix.
6. Rename the branch to the explicit target.
7. Push the target branch to origin.
8. Resume the stopped sessions.

The unit move journal retains the target branch.
A retry completes an interrupted folder move, branch move, or push.
The command refuses a different requested branch while a move remains pending.
The refusal names the pending branch.
The command refuses `--to-doc` for a unit.

## Workers

A worker rename retains its staff id, current conversation, parent, and backend.
The existing `staff.rename` writer calls `Herdr.retitle_row` for workers.
It renames proved worker panes and keeps their shared tab and workspace names.
An unavailable display produces one anomaly line after the staff rename.

## Scope and tests

Timers and stores keep their staff ids during a rename.
The command edits no document fields, plan headers, prose, marks, or history.
Unit renames change matching folder and branch suffixes unless `--to-branch` supplies the branch; see `unit-rename.md`.
Folder-only moves use `unit_move.py move-folder --unit <staff id> --to <path>`.

`scripts/shutdown/test_shutdown_cli.py` uses private staff records, temporary files, and a fake herdr.
The tests cover row rename, display repair, optional moves, and recovery after an interruption.
