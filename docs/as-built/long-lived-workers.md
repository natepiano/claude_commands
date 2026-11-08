# Long-lived workers and held Astra roles

## Phase 30, round 1

Live consumers read staff membership and backend evidence.
`StaffSnapshot.backends` derives backend evidence from each current native session.
`session_staff.worker_backend` returns recorded evidence or a reason for unavailable evidence.
Worker displays and cleanup read `worker_panes.observe_parents`.
They show stopped, closed, unknown and incomplete states without reading a mesh roster.
`inventory.worker_resumes` keeps Claude child commands in `ClaudeChildResume`.
`refusal_watch.py` owns `QUOTA_REFUSAL`.

Round 2 removes the held launcher, shutdown and sandbox dependencies.
It deletes `codex_mesh.py`, the worker ledger and the remaining allocation and adoption helpers.
It removes `PausedSeat`, `unit_runs.worker_transport`, `parent_transport`, run pins and the transport config keys.
It moves sandbox constants into `worker_panes.py`.
It renames `mesh_runs.py`, `mesh_run_process.py`, their config and the detached record folder.
The current detached commands keep their existing names until that round.
Completed run folders keep their roster files.
`progress_history.py` and `usage/agents_log.py` keep their historical reads.

## Staff membership and dispatch

A worker has a fixed staff id, a parent and a current native session.
A family change preserves the staff id and changes the current session.
Staff storage uses schema 2.
Backend readers derive the family and account from current session evidence.
They store no separate backend record.

The launcher records dispatch identity in `last_dispatch_<slot>.json`.
`worker_state.worker_record_path` resolves the staff member before it selects the slot file.
`worker_admission.parent` reads the run owner from the state file.
It does not recover ownership from a mesh roster.
The held launcher still uses allocation and publication helpers in this round.
Round 2 removes those helpers after the other units release their files.

## Displays and messages

The roster, board and account display read staff members and pane evidence.
`worker_panes.observe_parents` shares one pane read across the selected parents.
Each display preserves the reason for incomplete evidence.
A missing pane record does not prove that a worker has ended.

Staff worker addresses use `worker:<parent>/<worker>`.
Message selection resolves the fixed staff id and current session.
Pane delivery uses the observed pane and current holder.
Daemon sessions retain their `codex:<thread>` route.
The message command has no mesh recipient branch.

## Retirement and resume

Unit retirement stops workers through the pane worker path.
Run cleanup preserves worker threads.
`remove_workers.py` resolves the selected staff member before it calls the worker command.
Shutdown keeps saved Claude child launcher commands in `ClaudeChildResume`.
Pane worker resume messages keep the fixed staff id and dispatch id.
Account move messages preserve the move source, destination and task context.

The sweep uses staff ownership and pane states to retain live run folders.
Stopped or closed workers can supply end evidence.
Unknown or incomplete evidence retains the folder and names the cause.
Detached command records retain their own process and working directory checks.

## Held interfaces

`assign.py` still imports mesh launch helpers in this round.
`production/worker.py` and shutdown stop and restart code await their owning units.
`workers.py` keeps the ledger methods that those launchers call.
`worker_adoption.py` keeps the adoption helpers that `codex_mesh.py` calls.
`session_staff` keeps roster publication for those launchers.
These interfaces do not provide live display state.

Watchers and watcher events move to `worker_lifecycle.py`.
`finished_run_status` has the same owner.
`loaded_threads` moves to `herdr.py`.
Shared launcher fixtures move to `codex_mesh_test_support.py` without changing their names.
