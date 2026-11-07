# Staff file accessors

This is the list of every place that reads or writes the staff file, `~/.local/state/staff/staff.json`.
Use it to find each place to change in a staff migration. The spec is `docs/plans/staff-lifecycle.md`.

The check in `scripts/production/test_staff_accessors.py` fails when this table omits an accessor. Before a migration, run the check and add each file it names.

The table excludes Python test modules.
`staff.py` owns reads and writes.
On natedev, each Python accessor reads `staff.json` in its own process.
On the Mac, `staff.py` asks natedev over SSH.

## Staff identity and backends

`StaffRow` holds `id`, `kind`, `name`, `parent`, `current_session`, `worktree`, `doc`, `paused`, and `unknown_fields`.
The fixed `id` identifies a member across session changes.
`parent` is `ReportsTo(id)` or `StaffRoot`.
`current_session` holds the native Claude session ID or Codex thread ID.

`StaffSnapshot` derives backend evidence from session records, transcripts, and live processes.
`ClaudeBackend.family` is `claude`.
`CodexBackend.family` is `codex`; its `codex_home` names the serving account.
`BackendUnavailable` names unavailable backend evidence.
A live Claude session can supply backend evidence before its transcript exists.
A caller that already read the session registry sets `StaffSnapshot.running_sessions`, and `backends` then reads no registry.

`StaffRow.paused` is `Paused(since, by, why)` or `Active()`.
JSON stores `paused: {since, by, why}` only for a paused row.
`pause(reference, why)` records the time in the user zone and the caller's staff name.
`resume(reference)` removes the field.
The commands accept a unit or `--production <showrunner name>`.
Production pause accepts repeated `--except <unit>` options with a unit's staff name, its `-unit` form, or its staff id.
An unmatched `--except` value refuses the whole command before any write.
The refusal names the value and the production. A unit of another production does not match.
Both commands are safe to repeat. They stop no process.
Rename, move, retire and restore retain the pause field.

Rows, locations, and pause records keep fields that the reader does not know in `unknown_fields`.
Writes preserve these fields unchanged. Row changes also preserve them.
The owner keeps an existing location's unknown fields when its path or state changes, including retirement and restore.
Changes from the Mac use the same owner setters.
A new pause record starts with no unknown fields because it replaces the old record.
Staff storage uses schema 2.
A change that old code cannot read safely requires a new schema number.
`staff_sources.live` emits observations with schema 3.
`WorkerObservation.identity` is `ClaudeSession`, `CodexThread`, or `BackendUnavailable`.

Run records identify workers through fixed staff references.
The [worker lifecycle](long-lived-workers.md#roster-fields) defines their stored fields.
Its [admission boundary](long-lived-workers.md#staff-identity-and-admission) publishes the current runtime.

`set_worktree` compares an expected `PresentWorktree`, `RemovedWorktree`, or `WorktreeUnfilled` under the staff lock.
The comparison includes the machine and path when present.
A row lock protects one member during a worker change.
`staff.child_environment` removes inherited row-lock proofs and the staff ID before an agent starts.
It also removes the [operation configuration](agent-registry.md#agent-environment-boundary).
Staff lock and remote waits read `OPERATION_LOCK_SECONDS` and `REMOTE_SECONDS` from `config/staff.conf` at use.
Each wait uses the smaller configured limit or remaining operation deadline.

## Accessor table

The write functions are `ensure`, `ensure_worker`, `move`, `rename`, `retire`, and `retire_pending`.
They also include `set_doc`, `set_worktree`, `update_current_session`, `pause`, and `resume`.

| File | Access | `staff` calls |
|---|---|---|
| `commands/showrunner/promote_unit.md` | read | staff.py get (shell) |
| `commands/showrunner/units.md` | write | staff.py pause, resume (shell) |
| `docs/associate-producer/operations.md` | read | staff.json (shell) |
| `docs/as-built/showrunner-automation.md` | write | staff.py pause, resume (shell) |
| `docs/production_format.md` | read | staff.py get (shell) |
| `scripts/accounts/accounts.py` | read | local_snapshot |
| `scripts/accounts/pane_moves.py` | read | local_snapshot, WorkerName.parse |
| `scripts/accounts/refusal_watch.py` | read | initial_snapshot |
| `scripts/accounts/unit_watchdog.py` | read | active, initial_snapshot, snapshot |
| `scripts/agents/codex_mesh.py` | read | native_id, runtime_id, snapshot |
| `scripts/agents/mesh_runs.py` | read | local_snapshot |
| `scripts/agents/test_agent_exec.sh` | read | staff.json (shell) |
| `scripts/astra/astra_review.py` | read | runtime_id |
| `scripts/astra/astra_roles.py` | read | local_snapshot |
| `scripts/astra/design_role_test_support.py` | read | CodexBackend |
| `scripts/astra/design_roles.py` | read | runtime_id |
| `scripts/bevy_upgrade/bevy_upgrade.py` | read | snapshot |
| `scripts/bevy_upgrade/cli_test_support.py` | write | native_id, update_current_session |
| `scripts/bevy_upgrade/upgrade_schedule.py` | read | snapshot |
| `scripts/build_targets/working_set.py` | read | local_snapshot |
| `scripts/buildlog/mac_idle_test_support.py` | read | snapshot |
| `scripts/copies/copies.py` | read | local_snapshot |
| `scripts/direct/assign.py` | read | runtime_id |
| `scripts/direct/assign_claude.py` | read | local_snapshot |
| `scripts/direct/assign_claude_test_support.py` | read | staff.json path |
| `scripts/direct/assign_launcher_test_support.py` | write | by_id, local_snapshot, rename, set_worktree |
| `scripts/direct/board_json.py` | read | active, snapshot |
| `scripts/direct/claude_worker.py` | read | runtime_id |
| `scripts/direct/phase_reservation.py` | read | snapshot, WorktreeUnfilled |
| `scripts/direct/phase_table.py` | read | active, snapshot |
| `scripts/direct/plan_history.py` | read | snapshot |
| `scripts/direct/progress_history.py` | read | active, initial_snapshot, snapshot |
| `scripts/direct/recorder_fixture.py` | read | PresentWorktree |
| `scripts/direct/remove_workers.py` | write | local_snapshot, retire |
| `scripts/direct/review.sh` | read | local_snapshot, StaffRow |
| `scripts/direct/review_launcher_test_support.py` | write | local_snapshot, native_id, update_current_session |
| `scripts/direct/workers.py` | read | initial_snapshot |
| `scripts/hana_shot/hana_shot.py` | read | local_snapshot, runtime_id |
| `scripts/herdr/herdr.py` | read | active, initial_snapshot, native_id, runtime_id |
| `scripts/herdr/herdr_sessions.py` | read | initial_snapshot |
| `scripts/herdr/pane_launch.py` | read | initial_snapshot |
| `scripts/herdr/side_pane.py` | read | runtime_id |
| `scripts/herdr/worker_panes.py` | write | snapshot, runtime_id, row_operation, slot_operation; worker_admission.publish |
| `scripts/hierarchies/worker_adoption.py` | read | local_snapshot, row_operation, ReportsTo, StaffId |
| `scripts/hooks/conversation_pause.py` | read | LiveDoc, WrappedDoc (types only) |
| `scripts/hooks/conversation_pause_test_support.py` | write | LiveDoc; fixture_member calls ensure |
| `scripts/hooks/herdr_tab_title.py` | read | snapshot |
| `scripts/hooks/post-tool-use-sendmessage-miss.py` | read | snapshot |
| `scripts/hooks/session-start-pane-record.py` | read | runtime_id |
| `scripts/hooks/showrunner_footer.py` | read | snapshot |
| `scripts/hooks/staff_session_identity.py` | write | initial_snapshot, native_id, runtime_id, update_current_session |
| `scripts/lib/relaunch.py` | read | snapshot, by_current_session |
| `scripts/lib/resume_message.py` | read | StaffId |
| `scripts/lib/scratch.py` | read | by_current_session, runtime_id, snapshot |
| `scripts/lib/unit_runs.py` | read | local_snapshot, record_path |
| `scripts/lint/tmp_sweep.py` | read | local_snapshot |
| `scripts/maintenance/agent_notes_job.py` | read | snapshot |
| `scripts/message/escalate.py` | read | active, snapshot |
| `scripts/message/morty_identity.py` | read | native_id |
| `scripts/message/rca.py` | read | active, snapshot |
| `scripts/message/recipient_test_support.py` | read | staff.json path |
| `scripts/message/roster.py` | read | initial_snapshot, runtime_id |
| `scripts/message/send.py` | read | snapshot |
| `scripts/message/sessions.py` | read | current_session_target |
| `scripts/message/unit_labels.py` | read | active, snapshot |
| `scripts/nightly_review/nightly_review.py` | read | active, snapshot |
| `scripts/production/add_showrunner.py` | write | by_id, cli_result, initial_snapshot, native_id, operation, update_current_session |
| `scripts/production/add_showrunner_test_support.py` | read | staff.json path |
| `scripts/production/add_unit.py` | write | cli_result, ensure, initial_snapshot, native_id, operation, row_operation, set_worktree |
| `scripts/production/add_unit_test_support.py` | write | find, native_id, record_production, set_worktree, update_current_session |
| `scripts/production/change_review.py` | read | snapshot |
| `scripts/production/change_routing.py` | read | staff.json path |
| `scripts/production/codex_winddown.py` | read | snapshot |
| `scripts/production/completion_receipt.py` | read | native_id |
| `scripts/production/dailies_input.py` | read | snapshot |
| `scripts/production/dailies_input_test_support.py` | write | rename, snapshot |
| `scripts/production/delivery_state.py` | read | staff.json path |
| `scripts/production/fake_showrunner.py` | write | native_id, update_current_session |
| `scripts/production/fake_unit_host.py` | read | by_path |
| `scripts/production/footer_counts.py` | read | snapshot |
| `scripts/production/live_units.py` | read | snapshot |
| `scripts/production/log_backup.py` | read | none (StaffRefused, StaffUnreadable exceptions) |
| `scripts/production/merge_checkpoint.py` | read | snapshot |
| `scripts/production/merge_checkpoint_test_support.py` | write | native_id, update_current_session |
| `scripts/production/merge_gate.py` | read | staff.json path |
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
| `scripts/production/rename.py` | write | by_id, cli_result, operation, rename, row_operation, set_doc, snapshot |
| `scripts/production/rename_state.py` | read | snapshot |
| `scripts/production/rename_test_support.py` | write | find, native_id, operation, set_worktree, update_current_session |
| `scripts/production/rename_unit_test_support.py` | read | snapshot |
| `scripts/production/resume_unit.py` | read | cli_result, initial_snapshot, operation, restore_unit |
| `scripts/production/retire_unit.py` | write | cli_result, operation, operation_fds, set_worktree, snapshot |
| `scripts/production/session_labels.py` | read | snapshot |
| `scripts/production/session_staff.py` | read | active, snapshot |
| `scripts/production/showrunner_parent.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/showrunner_tree.py` | read | active, snapshot |
| `scripts/production/showrunners.py` | read | initial_snapshot, runtime_id |
| `scripts/production/staff.py` | owner | staff.json (owner) |
| `scripts/production/staff_move.py` | write | cli_result, main, move, operation, snapshot |
| `scripts/production/staff_sources.py` | write | active, apply_seed, cli_result, ensure, initial_snapshot, native_id, operation, set_doc, snapshot |
| `scripts/production/staff_test_support.py` | write | ensure, ensure_worker, initial_snapshot, move, native_id, operation, set_doc, set_worktree, snapshot, update_current_session |
| `scripts/production/stall_watch.py` | read | snapshot |
| `scripts/production/store_owner.py` | read | snapshot |
| `scripts/production/style_units.py` | read | snapshot |
| `scripts/production/tidy_wrapped.py` | read | initial_snapshot |
| `scripts/production/transfer_unit.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/unit_command.py` | read | snapshot |
| `scripts/production/unit_config.py` | read | snapshot |
| `scripts/production/unit_config_test_support.py` | read | staff.json path |
| `scripts/production/unit_lookup.py` | read | snapshot |
| `scripts/production/unit_move.py` | write | folder_move_path, native_id, set_worktree, snapshot |
| `scripts/production/unit_phases.py` | read | snapshot |
| `scripts/production/unit_run.py` | read | snapshot |
| `scripts/production/unit_status.sh` | read | snapshot |
| `scripts/production/unit_status_test_support.py` | write | set_doc, set_worktree |
| `scripts/production/update_registration.py` | read | snapshot |
| `scripts/production/update_registration_test_support.py` | write | by_id, initial_snapshot, set_doc |
| `scripts/production/waiting.py` | read | snapshot |
| `scripts/production/worker.py` | write | backend, snapshot, runtime_id, row_operation, retire |
| `scripts/production/worker_admission.py` | write | by_current_session, ensure_worker, native_id, runtime_id, snapshot, update_current_session |
| `scripts/production/worker_fill.py` | write | ensure_worker, native_id, snapshot |
| `scripts/review_links/change_browser.py` | read | none (ReportsTo type) |
| `scripts/review_links/change_page.py` | read | snapshot |
| `scripts/shutdown/inventory.py` | read | local_snapshot |
| `scripts/shutdown/launch_names.py` | read | initial_snapshot |
| `scripts/shutdown/pane_record.py` | read | initial_snapshot |
| `scripts/shutdown/pane_restore.py` | read | row_operation |
| `scripts/shutdown/remote_control.py` | read | initial_snapshot |
| `scripts/shutdown/restart.py` | read | initial_snapshot, row_operation |
| `scripts/shutdown/session_types.py` | read | snapshot |
| `scripts/shutdown/shutdown.py` | read | initial_snapshot |
| `scripts/shutdown/shutdown_record.py` | read | snapshot |
| `scripts/shutdown/stop_results.py` | read | StaffRow and backend types (type check only) |
| `scripts/shutdown/stop_resume.py` | read | initial_snapshot, row_operation |
| `scripts/shutdown/unit_staff_test_support.py` | read | staff.json path |
| `scripts/statusline/assignment.py` | read | StaffRow.from_json, StaffSnapshot |
| `scripts/statusline/counts_row.py` | read | none (StaffRefused exception) |
| `scripts/statusline/statusline.sh` | read | staff.json (shell) |
| `scripts/style/add_style.py` | read | snapshot, LiveDoc, WrappedDoc |
| `scripts/style/coverage_report.py` | read | snapshot |
| `scripts/style/ledger_test_support.py` | write | apply_seed, initial_snapshot, set_doc |
| `scripts/style/style_ledger.py` | read | snapshot |
| `scripts/style/style_plan.py` | read | snapshot |
| `scripts/style/style_plan_test_support.py` | read | staff.json path |
| `scripts/style/style_proposals.py` | read | snapshot |
| `scripts/style/unit_files.py` | read | snapshot |
| `scripts/usage/agents_log.py` | read | local_snapshot, record_path |
| `scripts/usage/outcomes.py` | read | snapshot |
| `scripts/usage/usage_report.py` | read | snapshot |
| `scripts/usage/waste_watch.py` | read | snapshot |
| `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/hierarchy.rs` | read | StaffFileReader reads staff.json (Rust, direct) |

155 accessors: 31 write, 123 read, 1 owner.

## Approved copies

| Copy | Writer | Reader | Approved |
|---|---|---|---|
| `~/.local/state/staff/staff.json` on the Mac, read-only | `staff.py` on natedev, after each write (atomic rename) | cargo-handler on the Mac (`census/hierarchy.rs`) | the user, 2026-11-04 |

No other copy of the staff facts is allowed without the user's approval. Remake this list before any staff migration.

`Herdr.codex_client_session` reads the current thread for a bare Codex pane from its tab's staff row.
Both `Herdr._codex_launch` and `pane_record.seed` use this method.
`pane_record.py` still reads staff for saved launch references and resume operations.
