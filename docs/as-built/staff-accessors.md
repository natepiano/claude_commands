# Staff file accessors

This is the list of every place that reads or writes the staff file, `~/.local/state/staff/staff.json`.
Use it to find each place to change in a staff migration. The spec is `docs/plans/staff-lifecycle.md`.

The check in `scripts/production/test_staff_accessors.py` fails when this table omits an accessor. Before a migration, run the check and add each file it names.

The table excludes Python test modules.
`staff.py` owns reads and writes.
On natedev, each Python accessor reads `staff.json` in its own process.
On the Mac, `staff.py` asks natedev over SSH.

## Staff identity and kinds

`StaffRow` holds `id`, `kind`, `name`, `parent`, `current_session`, `worktree`, `doc`, `role`, `home`, `run`, `paused`, `exited`, and `unknown_fields`.
The fixed `id` identifies a member across session changes.
`parent` is `ReportsTo(id)` or `StaffRoot`.
`current_session` holds the native Claude session ID or Codex thread ID.
`run` holds `OpenRun(path)` or `NoRun`.
`set_run` stores the open run path.
`clear_run` clears pointers that match the supplied run path.

`Kind` is `Literal["showrunner", "unit", "worker", "producer"]`.
A producer has `StaffRoot` as its parent.
It can hold both a worktree and a document.
It can start a staff path, such as `morty`.
`staff_sources` includes producers when it reads active root members.
Restart, shutdown selection, and message routing recognize the same kind.

`StaffRow.home` is `SessionHome(path)` or `HomeUnknown()`.
It records the account configuration folder.
A move can write the target home before start confirmation.
The home alone does not prove a successful start.
`member_account.repair_home` repairs it from a proved live process.

## Producer rows and commands

Each producer runs its own named job.
The producer kind supplies shared management.
Each producer keeps its own role contract.

| Staff name | Kind | Command | Job contract |
|---|---|---|---|
| `morty` | `producer` | `/producer:morty` | Tracks the user's items and coordinates their showrunners. |
| `brain` | `producer` | `/producer:brain` | Its command defines management only. |
| `dwight` | `producer` | `/producer:dwight` | Strategist; its command defines management only. |

The commands live in `commands/producer/`.
Morty's operations document is `docs/producer/morty-operations.md`.
The Codex command sync names Morty's skill `producer-morty`.
`PRODUCER_FOLDER_ROOT` in `config/staff.conf` selects the document root, with default `~/rust/hanadocs`.
Each producer uses `<root>/<name>/README.md`.
The [shared resume path](shutdown-and-restart.md#producer-launch-and-admission) admits a first conversation through `staff.ensure`.

## Backend and hold evidence

`StaffSnapshot` derives backend evidence from session records, transcripts, and live processes.
`ClaudeBackend.family` is `claude`.
`CodexBackend.family` is `codex`; its `codex_home` names the serving account.
The worker's recorded `SessionHome` supplies `codex_home` when that home contains its transcript.
A Codex transcript without a matching recorded home gives `BACKEND_UNAVAILABLE`, including when the row has no recorded home.
Shared session folders do not select an account, and these rows never become `ClaudeBackend`.
`BackendUnavailable` names unavailable backend evidence.
A live Claude session can supply backend evidence before its transcript exists.
A caller that already read the session registry sets `StaffSnapshot.running_sessions`, and `backends` then reads no registry.

`StaffRow.paused` is `Paused(since, by, why)` or `Active()`.
`StaffRow.exited` is `Exited(since, by, why)` or `Active()`.
JSON stores each hold field only when that hold is active.
`pause(reference, why)` and `exit(reference, why)` record the time, caller, and reason through `set_hold`.
`resume(reference)` removes both hold fields through `set_hold`.
These staff functions write the record. The `agents` program owns process control and continuation delivery.

`agents pause NAME --why TEXT` stops the workers and holds the director at its prompt.
`agents exit NAME --why TEXT` stops the director and workers.
`--finish-step` waits for the workers' current tasks first.
`agents resume NAME` sends a paused director one continuation or starts an exited director through the shared resume path.
Failed continuation delivery restores the pause. A failed start retains the exit record.
The production forms use `--production SHOWRUNNER` and accept repeated `--except UNIT` values for pause and exit.
An unmatched exclusion refuses the production command before any write.
Repeated holds retain the original metadata. Rename, move, retire, and restore retain the hold fields.

Rows, locations, and hold records keep fields that the reader does not know in `unknown_fields`.
Writes preserve these fields unchanged. Row changes also preserve them.
The owner keeps an existing location's unknown fields when its path or state changes, including retirement and restore.
Changes from the Mac use the same owner setters.
A new hold record starts with no unknown fields.
Staff storage uses schema 2.
A change that old code cannot read safely requires a new schema number.
`staff_sources.live` emits observations with schema 3.
`WorkerObservation.identity` is `ClaudeSession`, `CodexThread`, or `BackendUnavailable`.

Run records identify workers through fixed staff references.
The [worker lifecycle](long-lived-workers.md#staff-membership-and-dispatch) defines their stored fields.
Its [admission boundary](long-lived-workers.md#staff-identity-and-admission) publishes the current runtime.

`set_worktree` compares an expected `PresentWorktree`, `RemovedWorktree`, or `WorktreeUnfilled` under the staff lock.
The comparison includes the machine and path when present.
A row lock protects one member during a worker change.
`staff.child_environment` removes inherited row-lock proofs and the staff ID before an agent starts.
It also removes the [operation configuration](agent-registry.md#agent-environment-boundary).
Staff lock and remote waits read `OPERATION_LOCK_SECONDS` and `REMOTE_SECONDS` from `config/staff.conf` at use.
Each wait uses the smaller configured limit or remaining operation deadline.

## Production document lookup

`StaffSnapshot.by_doc(machine, path)` prefers an exact document path.
Otherwise, it compares the Git common directory and repository-relative document path.
`_document_checkout` reads `.git`, `gitdir:`, and `commondir` without a subprocess.
It returns `CheckoutDocument`, `OutsideGitCheckout`, or `UnreadableGitCheckout`.
A document outside Git matches only an exact path.
Unreadable metadata for the requested document raises `StaffRefused`.
Unreadable metadata for another candidate causes the lookup to skip that candidate.
Several repository matches raise `StaffRefused` with the matching staff ids.
Question writers resolve relative folders from the selected owner's document.
Each checkout therefore uses the same question store.

## Accessor table

The write functions are `ensure`, `ensure_worker`, `move`, `rename`, `retire`, and `retire_pending`.
They also include `set_doc`, `set_worktree`, `update_current_session`, `set_hold`, `pause`, `exit`, and `resume`.
`set_worker_role` writes the last dispatch role.
`set_home` writes the selected or proved account home.
`progress_history.begin_dispatch` writes every dispatch role, including Astra policies.
The shared pane launch builder uses `set_home`.
`add_unit.ensure_staff_unit`, worker pane admission, and `staff_sources.ensure_staff_showrunner` complete earlier pane publications.

| File | Access | `staff` calls |
|---|---|---|
| `commands/plan/shrink.md` | read | runtime_id; unit_runs.open_run reads the run pointer |
| `commands/showrunner/promote_unit.md` | read | staff.py get (shell) |
| `commands/showrunner/units.md` | write | agents pause, exit, resume (shell; staff writes through shutdown.py) |
| `docs/producer/morty-operations.md` | read | staff.json (shell) |
| `docs/as-built/showrunner-automation.md` | write | agents pause, exit, resume (shell; staff writes through shutdown.py) |
| `docs/as-built/session-notifier.md` | write | staff.py clear-run (documented shell command) |
| `docs/production_format.md` | read | staff.py get (shell) |
| `scripts/accounts/accounts.py` | read | local_snapshot |
| `scripts/accounts/pane_moves.py` | read | local_snapshot, WorkerName.parse |
| `scripts/accounts/refusal_watch.py` | read | initial_snapshot |
| `scripts/accounts/session_state.py` | read | StaffRow, Exited, Paused |
| `scripts/accounts/unit_watchdog.py` | read | active, initial_snapshot, snapshot |
| `scripts/agents/codex_test_support.py` | read | CodexBackend (fixture) |
| `scripts/agents/detached_runs.py` | read | local_snapshot |
| `scripts/agents/test_agent_exec.sh` | read | staff.json (shell) |
| `scripts/astra/astra_review.py` | read | runtime_id |
| `scripts/astra/astra_roles.py` | read | local_snapshot |
| `scripts/astra/design_role_test_support.py` | read | CodexBackend |
| `scripts/astra/design_roles.py` | read | runtime_id |
| `scripts/bevy_upgrade/bevy_upgrade.py` | read | snapshot |
| `scripts/bevy_upgrade/cli_test_support.py` | write | native_id, update_current_session |
| `scripts/bevy_upgrade/upgrade_schedule.py` | read | snapshot |
| `scripts/build_targets/working_set.py` | read | local_snapshot |
| `scripts/build_hold/build_hold.py` | read | local_snapshot, OpenRun; finds the run owner's current_session |
| `scripts/buildlog/mac_idle_test_support.py` | read | snapshot |
| `scripts/copies/copies.py` | read | local_snapshot, caller_identity; current owner names by row id |
| `scripts/build_queue/build_queue.py` | read | caller_identity |
| `scripts/build_queue/admission_records.py` | read | caller_identity |
| `scripts/direct/assign.py` | read | runtime_id |
| `scripts/direct/assign_claude.py` | read | local_snapshot |
| `scripts/direct/assign_claude_test_support.py` | read | staff.json path |
| `scripts/direct/assign_launcher_test_support.py` | write | by_id, local_snapshot, rename, set_worktree |
| `scripts/direct/worker_runs.py` | read | find; pane worker rows |
| `scripts/direct/claude_worker.py` | read | runtime_id |
| `scripts/direct/clear_phase_review.sh` | read | local_snapshot |
| `scripts/direct/end_session.sh` | write | clear-run (shell); reads the current pointer through unit_runs.open_run |
| `scripts/direct/phase_reservation.py` | read | snapshot, WorktreeUnfilled |
| `scripts/direct/phase_table.py` | read | active, snapshot |
| `scripts/direct/plan_history.py` | read | snapshot |
| `scripts/direct/prepare_session.sh` | write | set-run (shell), line 24; stores the prepared run path |
| `scripts/direct/progress_history.py` | write | active, initial_snapshot, snapshot, set_worker_role, set_run, clear_run |
| `scripts/direct/recorder_fixture.py` | read | PresentWorktree |
| `scripts/direct/remove_workers.py` | read | local_snapshot, runtime_id |
| `scripts/direct/review.sh` | read | local_snapshot, StaffRow |
| `scripts/direct/review_launcher_test_support.py` | write | local_snapshot, native_id, update_current_session |
| `scripts/direct/verify.sh` | read | snapshot, by_id, StaffId, StaffRow |
| `scripts/direct/worker_runs.py` | read | StaffId, StaffSnapshot |
| `scripts/direct/worker_state.py` | read | StaffRow |
| `scripts/direct/workers.py` | read | initial_snapshot, local_snapshot |
| `scripts/hana_shot/hana_shot.py` | read | local_snapshot, runtime_id |
| `scripts/herdr/herdr.py` | read | active, initial_snapshot, native_id, runtime_id |
| `scripts/herdr/herdr_sessions.py` | read | initial_snapshot |
| `scripts/herdr/pane_launch.py` | write | initial_snapshot, local_snapshot, by_id, set_home |
| `scripts/herdr/side_pane.py` | read | runtime_id |
| `scripts/herdr/turn_end.py` | write | snapshot, backend, native_id, runtime_id, retire; worker_admission.parent, find_worker, transcript_source, transcript_entries |
| `scripts/herdr/worker_panes.py` | write | snapshot, runtime_id, row_operation, slot_operation; worker_admission.publish |
| `scripts/hooks/conversation_pause.py` | read | local_by_current_session; StaffSnapshot, LiveDoc, WrappedDoc |
| `scripts/hooks/conversation_pause_test_support.py` | write | LiveDoc; fixture_member calls ensure |
| `scripts/hooks/herdr_tab_title.py` | read | snapshot |
| `scripts/hooks/post-tool-use-sendmessage-miss.py` | read | snapshot |
| `scripts/hooks/session-start-pane-record.py` | read | runtime_id |
| `scripts/hooks/showrunner_footer.py` | read | snapshot |
| `scripts/hooks/staff_session_identity.py` | write | initial_snapshot, native_id, runtime_id, update_current_session |
| `scripts/hooks/user-prompt-submit-conversation-pause.py` | read | StaffSnapshot (type); conversation_pause.session_staff reads the row |
| `scripts/lib/relaunch.py` | read | snapshot, by_current_session |
| `scripts/lib/resume_message.py` | read | StaffId |
| `scripts/lib/scratch.py` | read | by_current_session, runtime_id, snapshot |
| `scripts/lib/unit_runs.py` | read | local_snapshot, local_by_current_session, record_path, OpenRun |
| `scripts/lint/tmp_sweep.py` | read | local_snapshot |
| `scripts/maintenance/agent_notes_job.py` | read | snapshot |
| `scripts/message/escalate.py` | read | active, snapshot |
| `scripts/message/failed_lookup.py` | read | snapshot |
| `scripts/message/morty_identity.py` | read | native_id |
| `scripts/message/rca.py` | read | active, snapshot |
| `scripts/message/recipient_test_support.py` | read | staff.json path |
| `scripts/message/roster.py` | read | initial_snapshot, runtime_id |
| `scripts/message/send.py` | read | snapshot |
| `scripts/message/sessions.py` | read | current_session_target |
| `scripts/message/unit_labels.py` | read | active, snapshot, local_snapshot, OpenRun |
| `scripts/nightly_review/nightly_review.py` | read | active, snapshot |
| `scripts/nix_env/heal.py` | read | initial_snapshot |
| `scripts/production/add_showrunner.py` | write | by_id, cli_result, initial_snapshot, native_id, operation, update_current_session |
| `scripts/production/add_showrunner_test_support.py` | read | staff.json path |
| `scripts/production/add_unit.py` | write | cli_result, ensure, initial_snapshot, native_id, operation, row_operation, set_worktree |
| `scripts/production/add_unit_test_support.py` | write | find, native_id, record_production, set_worktree, update_current_session |
| `scripts/production/change_review.py` | read | snapshot |
| `scripts/production/change_routing.py` | read | staff.json path |
| `scripts/production/codex_winddown.py` | read | snapshot |
| `scripts/production/completion_receipt.py` | read | native_id |
| `scripts/production/delivery_state.py` | read | staff.json path |
| `scripts/production/demote_showrunner.py` | write | by_id, change_director, cli_result, move, operation, snapshot |
| `scripts/production/fake_showrunner.py` | write | native_id, update_current_session |
| `scripts/production/fake_unit_host.py` | read | by_path |
| `scripts/production/footer_counts.py` | read | snapshot |
| `scripts/production/live_units.py` | read | snapshot |
| `scripts/production/log_backup.py` | read | none (StaffRefused, StaffUnreadable exceptions) |
| `scripts/production/checkpoint_gate.py` | read | snapshot |
| `scripts/production/merge_checkpoint.py` | read | snapshot |
| `scripts/production/merge_checkpoint_test_support.py` | write | native_id, update_current_session |
| `scripts/production/bump_gate.py` | read | read_production.owner.id |
| `scripts/production/ci_points.py` | read | Production.owner.id |
| `scripts/production/fast_track.py` | read | copies.source_owner |
| `scripts/production/waiting.py` | read | Production.owner.id |
| `scripts/production/merge_gate.py` | read | snapshot, initial_snapshot, doc_owner |
| `scripts/production/merge_gate_test_support.py` | write | LiveDoc; fixture_member calls ensure |
| `scripts/production/next_item.py` | read | snapshot |
| `scripts/production/production_doc.py` | read | StaffRow, StaffSnapshot, LiveDoc, WrappedDoc |
| `scripts/production/production_lifecycle.py` | write | defer_retire, operation, retire_pending, set_doc, set_worktree, snapshot |
| `scripts/production/production_lifecycle_test_support.py` | read | find, pane, register_production, session_transcript |
| `scripts/production/promote_showrunner.py` | read | cli_result, find, native_id, operation, promote_unit, runtime_id, snapshot |
| `scripts/production/proposal_issue.py` | read | snapshot, StaffId |
| `scripts/production/proposals_folder.py` | read | initial_snapshot |
| `scripts/production/proposals_table.py` | read | none (StaffSnapshot type) |
| `scripts/production/prune_phase_notes.py` | read | snapshot |
| `scripts/production/rename_test_support.py` | write | find, native_id, operation, set_worktree, update_current_session |
| `scripts/production/rename_unit_test_support.py` | read | snapshot |
| `scripts/agents/shutdown.py` | write | by_id, cli_result, initial_snapshot, operation, operation_fds, rename, restore_unit, row_operation, set_doc, set_worktree, snapshot |
| `scripts/production/session_labels.py` | read | snapshot |
| `scripts/production/session_staff.py` | read | active, snapshot |
| `scripts/production/showrunner_parent.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/showrunner_tree.py` | read | active, snapshot |
| `scripts/production/showrunners.py` | read | initial_snapshot, runtime_id |
| `scripts/production/staff.py` | owner | staff.json (owner) |
| `scripts/production/staff_move.py` | write | cli_result, main, move, operation, snapshot |
| `scripts/production/staff_sources.py` | write | active, apply_seed, cli_result, ensure, initial_snapshot, native_id, operation, set_doc, snapshot |
| `scripts/production/staff_test_support.py` | write | ensure, ensure_worker, initial_snapshot, local_snapshot, move, native_id, operation, set_doc, set_run, set_worktree, snapshot, update_current_session |
| `scripts/production/stall_watch.py` | read | snapshot |
| `scripts/production/store_owner.py` | read | snapshot |
| `scripts/production/style_units.py` | read | snapshot |
| `scripts/production/tidy_wrapped.py` | read | initial_snapshot |
| `scripts/production/transfer_unit.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/unit_command.py` | read | snapshot |
| `scripts/production/unit_config.py` | read | snapshot |
| `scripts/production/unit_config_test_support.py` | read | staff.json path |
| `scripts/production/unit_lookup.py` | read | snapshot |
| `scripts/production/unit_move.py` | write | native_id, row_operation, set_worktree, snapshot |
| `scripts/production/unit_phases.py` | read | snapshot |
| `scripts/production/unit_queue.py` | read | local_snapshot, by_current_session, caller_identity, StaffId |
| `scripts/production/unit_run.py` | read | snapshot |
| `scripts/production/unit_status.sh` | read | snapshot |
| `scripts/production/unit_status_test_support.py` | write | set_doc, set_worktree |
| `scripts/production/update_registration.py` | read | snapshot |
| `scripts/production/update_registration_test_support.py` | write | by_id, initial_snapshot, set_doc |
| `scripts/production/waiting.py` | read | snapshot |
| `scripts/production/worker.py` | write | backend, snapshot, runtime_id, row_operation, retire |
| `scripts/production/worker_admission.py` | write | by_current_session, ensure_worker, native_id, runtime_id, snapshot, update_current_session |
| `scripts/review_links/branch_page.py` | read | snapshot, worktree_machine, by_id, by_current_session, caller_identity; session_staff.RowResolver reads branches and production docs |
| `scripts/review_links/change_browser.py` | read | none (ReportsTo type) |
| `scripts/review_links/change_page.py` | read | snapshot |
| `scripts/agents/inventory.py` | read | local_snapshot |
| `scripts/agents/launch_names.py` | read | initial_snapshot |
| `scripts/agents/member_account.py` | write | StaffRow, SessionHome, set_home; repairs the home from live process evidence |
| `scripts/agents/pane_record.py` | read | initial_snapshot |
| `scripts/agents/pane_restore.py` | read | row_operation |
| `scripts/agents/remote_control.py` | read | initial_snapshot |
| `scripts/agents/restart.py` | write | initial_snapshot, row_operation, set_home |
| `scripts/agents/session_types.py` | read | snapshot |
| `scripts/agents/shutdown.py` | write | initial_snapshot, pause, exit, resume, set_hold |
| `scripts/agents/shutdown_record.py` | read | snapshot |
| `scripts/agents/stop_results.py` | read | StaffRow and backend types (type check only) |
| `scripts/agents/stop_resume.py` | write | initial_snapshot, row_operation, slot_operation, ensure, native_id, set_home, resume, set_hold, child_environment |
| `scripts/agents/unit_staff_test_support.py` | read | staff.json path |
| `scripts/statusline/assignment.py` | read | StaffRow.from_json, StaffSnapshot |
| `scripts/statusline/counts_row.py` | read | none (StaffRefused exception) |
| `scripts/statusline/statusline.sh` | read | staff.json (shell) |
| `scripts/style/add_style.py` | read | snapshot, LiveDoc, WrappedDoc |
| `scripts/style/coverage_report.py` | read | snapshot |
| `scripts/style/ledger_test_support.py` | write | apply_seed, initial_snapshot, set_doc |
| `scripts/style/style_config.py` | read | StaffRow, StaffSnapshot.find, StaffSnapshot.parent_of, LiveDoc, WrappedDoc |
| `scripts/style/style_ledger.py` | read | snapshot |
| `scripts/style/style_plan.py` | read | snapshot |
| `scripts/style/style_plan_test_support.py` | read | staff.json path |
| `scripts/style/style_proposals.py` | read | snapshot |
| `scripts/style/style_sync.py` | read | local_snapshot, caller_identity, doc_owner |
| `scripts/style/unit_files.py` | read | snapshot |
| `scripts/usage/agents_log.py` | read | local_snapshot, OpenRun |
| `scripts/usage/outcomes.py` | read | snapshot |
| `scripts/usage/transcript_retention.py` | read | local_snapshot |
| `scripts/usage/usage_report.py` | read | snapshot |
| `scripts/usage/waste_watch.py` | read | snapshot |
| `scripts/worktree/worktrees.py` | read | snapshot, StaffSnapshot, StaffAbsent, StaffRefused |
| `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/hierarchy.rs` | read | StaffFileReader reads staff.json (Rust, direct) |

182 accessor rows: 39 write, 142 read, 1 owner.

## Approved copies

| Copy | Writer | Reader | Approved |
|---|---|---|---|
| `~/.local/state/staff/staff.json` on the Mac, read-only | `staff.py` on natedev, after each write (atomic rename) | cargo-handler on the Mac (`census/hierarchy.rs`) | the user, 2026-11-04 |

No other copy of the staff facts is allowed without the user's approval. Remake this list before any staff migration.

`Herdr.codex_client_session` reads the current thread for a bare Codex pane from its tab's staff row.
Both `Herdr._codex_launch` and `pane_record.seed` use this method.
`pane_record.py` still reads staff for saved launch references and resume operations.

A removed worktree retains its branch in `rows[].worktree.branch`.
`staff.RemovedWorktree.branch` uses `ParkedBranch` or `BranchUnrecorded`.
The lifecycle writers retain the branch before they remove its last Git evidence.
Parked configuration files retain only the retirement date and session.
Folder moves use the staff row lock.
Git stores the move lock at `<git-common-dir>/worktrees/<worktree-id>/locked`.
`unit_move._lock_move` writes `staff:<unit-id>` through `git worktree lock`.
`unit_move.move_locked` and `_recoverable_folder` read this fixed identity.
The file contains no folder, branch, destination, or requested name.
`_unlock_move` removes the lock after staff records the destination.
Recovery also checks the owner's Git repository before it adopts a destination.
After an interruption, staff can still name the absent source.
Several worktrees can share one Git repository.
Staff, disk, and the ordinary Git registration cannot then prove which unit owns the destination.
The native lock supplies this identity without a move journal.
`staff.folder_move_path` and the folder move journal are removed.
