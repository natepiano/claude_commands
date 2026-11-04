# Staff file accessors

This is the list of every place that reads or writes the staff file, `~/.local/state/staff/staff.json`.
Use it to find each place to change in a staff migration. The spec is `docs/plans/staff-lifecycle.md`.

The staff accessor test fails when a file that reads or writes the staff file is not listed here. Before a migration, run that test and add each file it names.

Made from the code on 2026-11-04. Tests are left out. `staff.py` is the one owner of reads and writes;
on natedev each Python accessor reads `staff.json` in its own process, and on the Mac `staff.py` asks natedev over ssh.

Write functions of `staff.py`: ensure, ensure_worker, move, rename, retire, retire_pending, set_doc, set_worktree, update_current_session.

| File | Access | `staff` calls |
|---|---|---|
| `scripts/accounts/accounts.py` | read | local_snapshot |
| `scripts/accounts/refusal_watch.py` | read | initial_snapshot |
| `scripts/accounts/unit_watchdog.py` | read | active, initial_snapshot, snapshot |
| `scripts/agents/codex_mesh.py` | read | native_id, runtime_id, snapshot |
| `scripts/astra/design_roles.py` | read | runtime_id |
| `scripts/direct/assign.py` | read | runtime_id |
| `scripts/direct/board_json.py` | read | active, snapshot |
| `scripts/direct/implement_launcher_test_support.py` | read | staff.json path |
| `scripts/direct/phase_table.py` | read | active, snapshot |
| `scripts/direct/plan_history.py` | read | snapshot |
| `scripts/direct/progress_history.py` | read | active, initial_snapshot, snapshot |
| `scripts/direct/workers.py` | read | initial_snapshot |
| `scripts/herdr/herdr.py` | read | active, initial_snapshot, native_id, runtime_id |
| `scripts/herdr/herdr_sessions.py` | read | initial_snapshot |
| `scripts/herdr/pane_launch.py` | read | initial_snapshot |
| `scripts/hierarchies/measure.py` | read | active, snapshot |
| `scripts/hierarchies/push_audit.py` | read | active, snapshot |
| `scripts/hooks/herdr_tab_title.py` | read | snapshot |
| `scripts/hooks/post-tool-use-sendmessage-miss.py` | read | snapshot |
| `scripts/hooks/session-start-pane-record.py` | read | runtime_id |
| `scripts/hooks/showrunner_footer.py` | read | snapshot |
| `scripts/hooks/staff_session_identity.py` | write | initial_snapshot, native_id, runtime_id, update_current_session |
| `scripts/lib/scratch.py` | read | by_current_session, runtime_id, snapshot |
| `scripts/message/escalate.py` | read | active, snapshot |
| `scripts/message/rca.py` | read | active, snapshot |
| `scripts/message/recipient_test_support.py` | read | staff.json path |
| `scripts/message/roster.py` | read | initial_snapshot, runtime_id |
| `scripts/message/send.py` | read | native_id, snapshot |
| `scripts/message/unit_labels.py` | read | active, snapshot |
| `scripts/nightly_review/nightly_review.py` | read | active, snapshot |
| `scripts/production/add_showrunner.py` | write | by_id, cli_result, initial_snapshot, native_id, operation, update_current_session |
| `scripts/production/add_showrunner_test_support.py` | read | staff.json path |
| `scripts/production/add_unit.py` | write | cli_result, ensure, initial_snapshot, native_id, operation, row_operation, set_worktree |
| `scripts/production/add_unit_test_support.py` | write | find, native_id, record_production, set_worktree, update_current_session |
| `scripts/production/change_review.py` | read | snapshot |
| `scripts/production/change_routing.py` | read | staff.json path |
| `scripts/production/completion_receipt.py` | read | native_id |
| `scripts/production/dailies_input.py` | read | snapshot |
| `scripts/production/dailies_input_test_support.py` | write | rename, snapshot |
| `scripts/production/delivery_state.py` | read | staff.json path |
| `scripts/production/fake_unit_host.py` | read | by_path |
| `scripts/production/footer_counts.py` | read | snapshot |
| `scripts/production/live_units.py` | read | snapshot |
| `scripts/production/merge_checkpoint.py` | read | snapshot |
| `scripts/production/merge_checkpoint_test_support.py` | write | native_id, update_current_session |
| `scripts/production/merge_gate.py` | read | staff.json path |
| `scripts/production/migrate_staff_ids.py` | read | _file, is_owner, native_id, operation, publish_native_ids, runtime_id, snapshot |
| `scripts/production/production_lifecycle.py` | write | defer_retire, operation, retire_pending, set_doc, set_worktree, snapshot |
| `scripts/production/production_lifecycle_test_support.py` | read | find, pane, register_production, session_transcript |
| `scripts/production/production_tree.py` | read | initial_snapshot, snapshot |
| `scripts/production/promote_showrunner.py` | read | cli_result, find, native_id, operation, promote_unit, runtime_id, snapshot |
| `scripts/production/proposals_folder.py` | read | initial_snapshot |
| `scripts/production/rename.py` | write | by_id, cli_result, operation, rename, set_doc, snapshot |
| `scripts/production/rename_state.py` | read | snapshot |
| `scripts/production/rename_test_support.py` | write | find, native_id, operation, set_worktree, update_current_session |
| `scripts/production/rename_unit.py` | write | by_id, cli_result, operation, rename, set_worktree, snapshot |
| `scripts/production/rename_unit_test_support.py` | read | snapshot |
| `scripts/production/resume_unit.py` | read | cli_result, initial_snapshot, operation, restore_unit |
| `scripts/production/retire_unit.py` | write | cli_result, operation, operation_fds, set_worktree, snapshot |
| `scripts/production/roles_audit.py` | read | snapshot |
| `scripts/production/session_labels.py` | read | snapshot |
| `scripts/production/session_staff.py` | read | active, snapshot |
| `scripts/production/showrunner_parent.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/showrunner_tree.py` | read | active, snapshot |
| `scripts/production/showrunners.py` | read | initial_snapshot, runtime_id |
| `scripts/production/staff_move.py` | write | cli_result, main, move, operation, snapshot |
| `scripts/production/staff_sources.py` | write | active, apply_seed, cli_result, ensure, initial_snapshot, native_id, operation, set_doc, snapshot |
| `scripts/production/staff_test_support.py` | write | ensure, ensure_worker, initial_snapshot, move, native_id, operation, set_doc, set_worktree, snapshot, update_current_session |
| `scripts/production/stall_watch.py` | read | snapshot |
| `scripts/production/tidy_wrapped.py` | read | initial_snapshot |
| `scripts/production/transfer_unit.py` | write | cli_result, move, operation, snapshot |
| `scripts/production/unit_command.py` | read | snapshot |
| `scripts/production/unit_config.py` | read | snapshot |
| `scripts/production/unit_config_test_support.py` | read | staff.json path |
| `scripts/production/unit_lookup.py` | read | snapshot |
| `scripts/production/unit_move.py` | write | folder_move_path, native_id, set_worktree, snapshot |
| `scripts/production/unit_phases.py` | read | snapshot |
| `scripts/production/unit_run.py` | read | snapshot |
| `scripts/production/unit_status_test_support.py` | write | set_doc, set_worktree |
| `scripts/production/update_registration.py` | read | snapshot |
| `scripts/production/update_registration_test_support.py` | write | by_id, initial_snapshot, set_doc |
| `scripts/production/waiting.py` | read | snapshot |
| `scripts/production/worker_admission.py` | write | by_current_session, ensure_worker, native_id, runtime_id, snapshot, update_current_session |
| `scripts/production/worker_fill.py` | write | ensure_worker, native_id, snapshot |
| `scripts/review_links/change_page.py` | read | snapshot |
| `scripts/shutdown/launch_names.py` | read | initial_snapshot |
| `scripts/shutdown/pane_record.py` | read | initial_snapshot |
| `scripts/shutdown/pane_restore.py` | read | row_operation |
| `scripts/shutdown/restart.py` | read | initial_snapshot, row_operation |
| `scripts/shutdown/session_types.py` | read | snapshot |
| `scripts/shutdown/shutdown.py` | read | initial_snapshot |
| `scripts/shutdown/stop_resume.py` | read | initial_snapshot, row_operation |
| `scripts/shutdown/unit_staff_test_support.py` | read | staff.json path |
| `scripts/style/coverage_report.py` | read | snapshot |
| `scripts/style/style_plan_test_support.py` | read | staff.json path |
| `scripts/style/style_proposals.py` | read | snapshot |
| `scripts/style/unit_files.py` | read | snapshot |
| `scripts/usage/outcomes.py` | read | snapshot |
| `scripts/usage/usage_report.py` | read | snapshot |
| `scripts/usage/waste_watch.py` | read | snapshot |
| `scripts/agents/test_agent_exec.sh` | read | staff.json (shell) |
| `scripts/statusline/statusline.sh` | read | staff.json (shell) |
| `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/command_inputs.rs` | read | staff.json (Rust, direct) |

103 accessors: 21 write, the rest read.

## Approved copies

| Copy | Writer | Reader | Approved |
|---|---|---|---|
| `~/.local/state/staff/staff.json` on the Mac, read-only | `staff.py` on natedev, after each write (atomic rename) | cargo-handler on the Mac (`census/command_inputs.rs`) | the user, 2026-11-04 |

No other copy of the staff facts is allowed without the user's approval. Remake this list before any staff migration.
