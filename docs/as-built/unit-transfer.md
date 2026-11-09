# Unit transfer

`/showrunner:transfer_unit <unit> <receiving showrunner name>` changes a unit's staff parent.
`scripts/production/transfer_unit.py` implements `plan`, `apply`, and `verify`.
The giving showrunner supplies the source production name.

`build` reads one staff snapshot and resolves both productions through their showrunner rows.
It searches the giving parent first, then the receiving parent for a repeated transfer.
A missing unit or destination row refuses the command.
`plan` prints the selected destination without changing membership.

`apply` holds the unit's `staff.row_operation` lock.
It changes the parent through `staff.move` when needed.
It then sends the unit a notice with the receiving production's current name.
`staff_move.move_unit` reconciles the unit's tab with the receiving workspace.
It pins the unit and parent through their fixed staff ids.
The unit retains its fixed id, current session, worktree, branch, plan, and unmerged work.
Production documents contain no unit rows to move.

A retry accepts membership already changed to the receiving parent and repeats tab reconciliation.
It prints `done` when membership changes and `unchanged` otherwise.
`verify` uses this same apply path and can complete an interrupted transfer.
No transfer journal or production-document commit exists.

`staff_move.py` owns tab placement.
The [unit rename design](unit-rename.md) owns folder and branch changes.
The [worker design](long-lived-workers.md#staff-membership-and-dispatch) owns worker identity during parent changes.
