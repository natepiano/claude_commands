# Phase 17 and Phase 20 port decisions

Comparison reference: `origin/main` at `b7fe55a67`.
Destination starts at `261787810`. Source commits remain read only.

Each row names one behavior. Test and fixture paths belong to that behavior.
`kept` means the destination receives the missing behavior and its tests.
`dropped` means main already has the behavior. `obsolete` means no port applies.

The judgment-first statement in the Work Order differs from the specified reference.
Commit `945969453` exists, but it is not an ancestor of `origin/main`.
The table therefore keeps the source judgment-first change.

| Source commit | Change | Files | Decision | Main commit or port note |
| --- | --- | --- | --- | --- |
| 81e41ae50 | Read process identity and state once for Linux and macOS | scripts/lib/process_start.py; process_start_test_support.py; test_process_start.py | kept | Main has no shared reader. Port the typed reader and its tests. |
| 81e41ae50 | Reject stale launcher claims after PID reuse | scripts/direct/assign.py; scripts/agents/codex_mesh.py; launcher and mesh tests | kept | Record the process start and release only the matching stale claim. |
| 81e41ae50 | Restore a missing batch-landed event on retry | scripts/direct/findings.py; test_findings.py | kept | Main saves landed state before the event without retry recovery. |
| 81e41ae50 | Remove an old lint pass before a forced failure | scripts/direct/verify.sh; test_verify_hold_lint.py | kept | Main retains the earlier pass when forced lint fails. |
| 81e41ae50 | Keep failed wait closures and recover stale wait owners | scripts/direct/verify.sh; test_verify_hold_wait.py | kept | Main deletes the folder after a failed closure. |
| 81e41ae50 | Retry session cleanup separately for each run | scripts/direct/end_session.sh; test_end_session.py | kept | Main stops at an unbind error and ignores later cleanup failures. |
| 81e41ae50 | Check board owners and owned groups through the shared reader | scripts/direct/board.sh; test_board_reclaim.py; test_verify_step_owner.py | kept | Preserve unknown owners. Signal only a confirmed group. |
| 81e41ae50 | Read step owners through the shared reader | scripts/direct/step_record.py; test_step_record.py; verify.sh | kept | Distinguish an unreadable owner from a stale owner. |
| 81e41ae50 | Stop the tree watcher when its parent PID changes | scripts/direct/tree_watch.py; test_tree_watch.py | kept | Main checks only the parent PID and zombie state. |
| 81e41ae50 | Read implementation group identity through the shared reader | scripts/direct/implement.sh; implement_launcher_test_support.py; review_launcher_test_support.py | kept | Preserve the current launcher flow and adapt its identity read. |
| 81e41ae50 | Keep build holds and signal only confirmed owners | scripts/build_hold/; scripts/hooks/test_build_hold_guard.py | kept | Port owner checks and complete process-stat fixtures. |
| 81e41ae50 | Check local wait owners through the shared reader | scripts/build_targets/; scripts/build_queue/test_build_queue.py | kept | Port waiter identity and the corresponding fixtures. |
| 81e41ae50 | Check idle-Mac evidence through the shared reader | scripts/buildlog/mac_idle.py; test_mac_idle.py | kept | Keep current queue behavior and replace identity reads. |
| 81e41ae50 | Retain memory promises when owner identity is unreadable | scripts/lint/memory_admit.py; test_memory_admit.py; test_invoke_scope.py | kept | Main treats unreadable identity as absent. |
| 81e41ae50 | Keep scratch folders when ownership cannot be read | scripts/lint/tmp_sweep.py; test_tmp_sweep.py | kept | Replace the local start and elapsed readers. |
| 81e41ae50 | Check Mac claims and copy group owners through the shared reader | scripts/mac_test/mac_test.py; copy_watch.py; offload_test_support.py; tests | kept | Keep unknown claims. Never signal an unconfirmed group. |
| 81e41ae50 | Use typed process identity for session liveness | scripts/message/sessions.py; test_sessions.py; test_top_level.py; test_rca.py; test_roster.py | kept | Keep the public session contract and port the shared reader boundary. |
| 81e41ae50 | Check herdr agent startup and process ancestry through the shared reader | scripts/herdr/herdr.py; test_herdr.py; test_herdr_sessions.py | kept | Adapt the port to current native pane readers. |
| 81e41ae50 | Keep Nix healing pending for unreadable owners | scripts/nix_env/heal.py; test_heal.py | kept | Replace the old empty-string identity result. |
| 81e41ae50 | Use shared identity for production cleanup and memory reads | scripts/production/merge_gate.py; retire_unit.py; rename_unit.py; tests | kept | Preserve later staff and merge-gate changes. |
| 81e41ae50 | Give production fixtures complete shared process identities | scripts/production/*test_support.py; fake_unit_host.py; affected production tests | kept | Adapt fixtures without weakening their assertions. |
| 81e41ae50 | Use shared identity for live shutdown inventory and remote control | scripts/shutdown/inventory.py; pane_record.py; remote_control.py; relevant tests | kept | Port only current live paths. |
| 81e41ae50 | Migrate the removed shutdown snapshot reader | scripts/shutdown/inventory.py; snapshot tests | obsolete | 3f3312f95 removed the agent session snapshot. |
| 81e41ae50 | Migrate the removed shutdown stop conductor | scripts/shutdown/stop.py; test_stop.py; test_settle.py | obsolete | 4c635ed46 removed the old stop conductor. |
| 81e41ae50 | Give shutdown restart fixtures typed identities | scripts/shutdown/test_restart.py; test_inventory.py; test_remote_control.py | kept | Adapt only tests for code that still exists. |
| be1478486 | Preserve library modules when replacing the fixture launcher | scripts/direct/verify_mac_offload_test_support.py; verify_hold_wait_test_support.py | kept | Main replaces the linked library with a launcher-only folder. |
| f83e5d1a5 | Read wait history with explicit complete and unavailable results | scripts/direct/progress_history.py; test_progress_history.py | kept | Never infer absence from unreadable or malformed history. |
| f83e5d1a5 | Publish wait identity before its start event | scripts/direct/progress_history.py; verify.sh; wait tests | kept | Use one publication operation for resource and turn waits. |
| f83e5d1a5 | Check phase and pass attribution before appending a wait start | scripts/direct/progress_history.py; test_progress_history.py | kept | Record abandoned starts with status 5 when attribution changes. |
| f83e5d1a5 | Return status 4 for a wait that never started | scripts/direct/progress_history.py; verify.sh; wait tests | kept | Keep unavailable history separate from a proven absent start. |
| f83e5d1a5 | Close and retire a recovered wait folder under one lock | scripts/direct/progress_history.py; verify.sh; test_verify_hold_wait.py | kept | Rename closed folders before deleting them. Retry deletion leftovers. |
| f83e5d1a5 | Do not recreate a retired result folder | scripts/mac_test/copy_watch.py; test_copy_watch.py | kept | Use the shared atomic writer and report publication failures. |
| b8afa5695 | Let atomic callers require an existing parent | scripts/lib/atomic_file.py; test_atomic_file.py; progress_history.py; copy_watch.py | kept | Add create_parent without changing existing callers or durability guarantees. |
| b8afa5695 | Use complete stat fixtures for build and roster tests | scripts/build_queue/test_build_why.py; scripts/message/test_roster.py | kept | Update only the shared-reader fixture boundaries. |
| 6af2af4c9 | Shrink Phase 20 history | docs/plans/sdlc-phases.md | obsolete | History-only plan text is excluded by the Work Order. |
| 73646aaf8 | Name the shared process reader in the roster document | docs/as-built/session-roster.md | kept | Replace the removed sessions.process_start reference. |
| 73646aaf8 | Update historical account plan text | docs/plans/build-followups-two-accounts.md | obsolete | History-only plan text is excluded by the Work Order. |
| 5bdd53aa4 | Record the held Mac check review row | commit message only | obsolete | The commit changes no file. No code exists to port. |
| 81e41ae50 / f83e5d1a5 | Keep held Mac routes safe before initial acquisition | scripts/direct/verify.sh; test_verify_token_wait.py | dropped | e11dca0e6 already covers held Mac routing on main. |
| f5645c552 | Judge clippy before running repairs | scripts/direct/verify.sh; lint tests and fixtures | kept | origin/main still fixes first. 945969453 has judgment-first behavior only on origin/natedev-verify-cancel, outside origin/main. |
| f5645c552 | Put build rules in every worker prompt | docs/direct/compose_work_order.md; write_prompt_contract.md | kept | Main lacks the build-rule section. Preserve current prompt requirements. |
| 70d081f5a | Check detached results before releasing a resident worker | scripts/agents/codex_mesh.py; test_codex_mesh.py | kept | Main checks release before the detached result on this path. |
| 70d081f5a | Restore staff and host fixture state on the same stack | scripts/agents/test_codex_mesh.py | kept | Main still splits environment cleanup across two stacks. |
| 70d081f5a | Consume roster-update return values in the new identity tests | scripts/agents/test_codex_mesh.py | kept | Apply to the tests ported with launcher identity. |
| 81e41ae50 / f83e5d1a5 | Update phase status in the source plan | docs/plans/sdlc-phases.md | obsolete | History-only plan hunks are excluded by the Work Order. |
