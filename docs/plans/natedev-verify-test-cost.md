# Verify test cost

This doc holds the cost of the `scripts/direct/test_verify*.py` files. It also sets the order in which they move to direct calls.

**Source:** builds showrunner, 2026-11-06. These files hold about 2% of all tests. They used 2,113 of 5,610 CPU s in the 5 fastest full gates of 2026-11-05.

## Method

**One file.** `measure_cost.py` runs `python3 -m unittest <module>` in a `copies.py` copy of the tree, with `HERDR_BIN=/nonexistent/herdr` and `PYTHONDONTWRITEBYTECODE=1`.

- **CPU s and wall s:** the `wait4` resource use and the clock of that one run, with all its waited children.
- **Processes:** a separate run under `strace -f`. It counts the `fork`, `vfork` and `clone` calls that succeed without `CLONE_THREAD`.
- **Execs:** the `execve` calls of that same run that succeed.

**Whole set.** `python3 ~/.claude/scripts/test_times/show.py top --limit 1000`, read on 2026-11-06: the median CPU s of each `direct/test_verify*` module in the gate records.

## Before and after by file

| File | Tests | CPU s before | CPU s after | Processes before | Processes after | Execs before | Execs after | Wall s before | Wall s after |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `test_verify_mac_arguments.py` | 4 | 56.0 | 30.2 | 1,925 | 982 | 1,384 | 728 | 62.1 | 34.0 |

`test_verify_mac_arguments.py` made 6 full `verify.sh` calls before and makes 3 now. Each kept run proves a path that a direct call cannot:

1. A Mac decline runs locally with the same arguments, the token passes between the machines, and the call record holds the result.
2. The real offload's busy answer reaches the call record.
3. A plain-package `no_tests` answer reaches `route_test`, and the local run follows, with `--no-tests=warn` at both machines.

## Batch of Phase 41a

Each number is one `measure_set.py` run on a `copies.py` copy: the `wait4` CPU s and the clock of the file's `python3 -m unittest` run. This batch has no process count. Before is `138dcf10e`. After is the batch on the `verify-plain` history. Wall s depends on the machine load of that minute.

| File | Tests | CPU s before | CPU s after | Wall s before | Wall s after | Test code changed |
| --- | --- | --- | --- | --- | --- | --- |
| `test_verify_mac_retry_hold.py` | 3 | 48.8 | 50.8 | 61.1 | 79.6 | yes |
| `test_verify_token_renew.py` | 14 | 45.6 | 42.9 | 81.8 | 82.2 | yes |
| `test_verify_mac_lifecycle.py` | 3 | 39.5 | 40.6 | 65.7 | 69.9 | no |
| `test_verify_token_memory_history.py` | 8 | 36.0 | 34.4 | 59.3 | 60.5 | no |
| `test_verify_mac_retry_clock.py` | 3 | 35.0 | 27.3 | 57.3 | 50.6 | yes |
| `test_verify_mac_results.py` | 4 | 34.4 | 32.4 | 38.5 | 39.5 | yes |
| `test_verify_hold_wait.py` | 6 | 30.0 | 21.5 | 37.0 | 26.9 | yes |
| `test_verify_tree_cancel_lint.py` | 6 | 29.4 | 29.8 | 42.8 | 46.0 | no |
| `test_verify_lint_taplo.py` | 11 | 23.2 | 14.2 | 30.6 | 29.9 | yes |
| `test_verify_lint_scope.py` | 5 | 18.8 | 19.6 | 23.5 | 31.7 | no |
| **Total** | 63 | 340.7 | 313.5 | 497.6 | 516.9 | |

CPU s fell by 27.2, or 8%. Wall s did not fall in these runs.

The four files with no test code change moved by at most 1.6 CPU s. Read that as the noise of one run.

`test_verify_mac_retry_hold.py` saved nothing. Review showed that a direct call of its deadline check passed with the deadline check removed from `verify.sh`, so that test keeps its full `verify.sh` run. The memory and token idle-marker tests in `test_verify_mac_retry_clock.py` keep their full runs for the same reason. The routing idle-marker test calls the hook directly.

natest takes the other files, and measures the whole set. The builds showrunner relayed this ruling from the user on 2026-11-06.

## Baseline for the whole set

The total is 1,618.6 median CPU s over 122 files, largest first.

| # | File | Median CPU s | Tests |
| --- | --- | --- | --- |
| 1 | `test_verify_token_renew.py` | 56.9 | 14 |
| 2 | `test_verify_mac_arguments.py` | 46.1 | 4 |
| 3 | `test_verify_token_memory_history.py` | 35.7 | 8 |
| 4 | `test_verify_mac_retry_hold.py` | 34.3 | 3 |
| 5 | `test_verify_mac_lifecycle.py` | 31.2 | 3 |
| 6 | `test_verify_tree_cancel_lint.py` | 29.8 | 5 |
| 7 | `test_verify_lint_taplo.py` | 28.6 | 11 |
| 8 | `test_verify_mac_results.py` | 27.4 | 4 |
| 9 | `test_verify_hold_wait.py` | 26.6 | 6 |
| 10 | `test_verify_mac_retry_clock.py` | 25.6 | 3 |
| 11 | `test_verify_mac_holder_history.py` | 25.3 | 1 |
| 12 | `test_verify_lint_scope.py` | 24.6 | 5 |
| 13 | `test_verify_mac_fallback_announcements.py` | 24.1 | 1 |
| 14 | `test_verify_mac_fallback_diagnostics.py` | 24.0 | 1 |
| 15 | `test_verify_token_wait.py` | 23.3 | 6 |
| 16 | `test_verify_checks_fail_closed.py` | 22.8 | 14 |
| 17 | `test_verify_mac_copy_ownership.py` | 22.5 | 3 |
| 18 | `test_verify_hold_signals.py` | 22.1 | 5 |
| 19 | `test_verify_mac_wait_records.py` | 22.0 | 2 |
| 20 | `test_verify_mac_copy_results.py` | 21.8 | 3 |
| 21 | `test_verify_lint_tool_failures.py` | 21.7 | 3 |
| 22 | `test_verify_mac_routing_changes.py` | 21.5 | 1 |
| 23 | `test_verify_mac_local_release.py` | 21.3 | 1 |
| 24 | `test_verify_release_ack.py` | 21.0 | 7 |
| 25 | `test_verify_mac_release.py` | 20.0 | 2 |
| 26 | `test_verify_lint_fix_limit.py` | 19.7 | 1 |
| 27 | `test_verify_mac_routing.py` | 18.9 | 1 |
| 28 | `test_verify_token_reservation.py` | 18.9 | 4 |
| 29 | `test_verify_mac_memory_handoff.py` | 18.8 | 1 |
| 30 | `test_verify_lint_fixes.py` | 18.2 | 1 |
| 31 | `test_verify_mac_result_paths.py` | 17.9 | 3 |
| 32 | `test_verify_lint_edits.py` | 17.8 | 2 |
| 33 | `test_verify_token_handoff.py` | 17.8 | 4 |
| 34 | `test_verify_mac_pacing.py` | 17.6 | 2 |
| 35 | `test_verify_mac_withdrawal.py` | 17.2 | 2 |
| 36 | `test_verify_dev_feature_commands.py` | 17.0 | 3 |
| 37 | `test_verify_token_queue.py` | 17.0 | 2 |
| 38 | `test_verify_mac_malformed.py` | 16.3 | 1 |
| 39 | `test_verify_mac_result_shape.py` | 16.1 | 1 |
| 40 | `test_verify_final_targets.py` | 16.0 | 7 |
| 41 | `test_verify_mac_result_status.py` | 16.0 | 1 |
| 42 | `test_verify_step_routes_results.py` | 15.9 | 1 |
| 43 | `test_verify_mac_signal_boundaries.py` | 15.8 | 2 |
| 44 | `test_verify_hold_steps.py` | 15.6 | 3 |
| 45 | `test_verify_mac_local_arguments.py` | 14.1 | 2 |
| 46 | `test_verify_mac_memory_retry_clock.py` | 13.9 | 1 |
| 47 | `test_verify_mac_memory_retry.py` | 13.5 | 2 |
| 48 | `test_verify_hold_mac.py` | 13.5 | 3 |
| 49 | `test_verify_lint_fix_failures.py` | 13.5 | 1 |
| 50 | `test_verify_token_release.py` | 13.1 | 4 |
| 51 | `test_verify_token_history.py` | 12.8 | 2 |
| 52 | `test_verify_dev_features_defaults.py` | 12.7 | 1 |
| 53 | `test_verify_mac_signals.py` | 12.6 | 1 |
| 54 | `test_verify_token_check.py` | 12.5 | 1 |
| 55 | `test_verify_hold_workers.py` | 12.4 | 2 |
| 56 | `test_verify_token_signals.py` | 12.3 | 2 |
| 57 | `test_verify_hold_history.py` | 12.2 | 3 |
| 58 | `test_verify_dev_features_execution.py` | 11.7 | 2 |
| 59 | `test_verify_example_edits.py` | 11.5 | 2 |
| 60 | `test_verify_hold_lint.py` | 11.2 | 5 |
| 61 | `test_verify_tree_cancel_mac.py` | 11.1 | 2 |
| 62 | `test_verify_lint_cache_fix_limit.py` | 10.9 | 1 |
| 63 | `test_verify_mac_configuration.py` | 10.5 | 2 |
| 64 | `test_verify_token_reclaim.py` | 10.4 | 2 |
| 65 | `test_verify_step_routes_lint.py` | 10.2 | 2 |
| 66 | `test_verify_step_routes_helpers.py` | 10.2 | 2 |
| 67 | `test_verify_mac_release_events.py` | 10.2 | 3 |
| 68 | `test_verify_token_detach.py` | 9.8 | 4 |
| 69 | `test_verify_doc_mac.py` | 9.8 | 2 |
| 70 | `test_verify_token_build.py` | 9.7 | 3 |
| 71 | `test_verify_lint_cache.py` | 9.5 | 1 |
| 72 | `test_verify_token_fifo.py` | 9.5 | 2 |
| 73 | `test_verify_example_dependencies.py` | 9.3 | 2 |
| 74 | `test_verify_mac_configuration_errors.py` | 9.2 | 2 |
| 75 | `test_verify_step_routes_restart.py` | 8.9 | 2 |
| 76 | `test_verify_mac_failed_status.py` | 8.7 | 1 |
| 77 | `test_verify_doc_records.py` | 8.5 | 3 |
| 78 | `test_verify_tree_cancel.py` | 8.5 | 2 |
| 79 | `test_verify_doc_checkout.py` | 8.4 | 2 |
| 80 | `test_verify_step_routes.py` | 8.3 | 3 |
| 81 | `test_verify_hold_memory.py` | 8.3 | 2 |
| 82 | `test_verify_lint_selection.py` | 8.1 | 3 |
| 83 | `test_verify_example_commands.py` | 8.0 | 2 |
| 84 | `test_verify_example_acceptance.py` | 7.9 | 2 |
| 85 | `test_verify_final_pass_script.py` | 7.9 | 3 |
| 86 | `test_verify_mac_local.py` | 7.9 | 1 |
| 87 | `test_verify_tree_cancel_reacquire.py` | 7.8 | 2 |
| 88 | `test_verify_doc_recovery.py` | 7.8 | 1 |
| 89 | `test_verify_final_pass.py` | 7.7 | 1 |
| 90 | `test_verify_hold_director.py` | 7.7 | 2 |
| 91 | `test_verify_mac_fallback.py` | 7.5 | 1 |
| 92 | `test_verify_mac_memory_decline.py` | 7.2 | 1 |
| 93 | `test_verify_token_same_slot.py` | 7.1 | 1 |
| 94 | `test_verify_mac_offload.py` | 7.0 | 3 |
| 95 | `test_verify_dev_features.py` | 7.0 | 3 |
| 96 | `test_verify_doc.py` | 6.0 | 3 |
| 97 | `test_verify_lint_cache_mend.py` | 5.2 | 1 |
| 98 | `test_verify_tree_cancel_history.py` | 5.1 | 1 |
| 99 | `test_verify_token_cancellation.py` | 5.0 | 2 |
| 100 | `test_verify_mac_pressure.py` | 4.7 | 1 |
| 101 | `test_verify_example_cache.py` | 4.6 | 1 |
| 102 | `test_verify_example_final.py` | 4.4 | 1 |
| 103 | `test_verify_untested_examples.py` | 4.3 | 1 |
| 104 | `test_verify_token_ownership.py` | 4.3 | 1 |
| 105 | `test_verify_final_pass_records.py` | 4.2 | 1 |
| 106 | `test_verify_final_pass_staged.py` | 4.2 | 1 |
| 107 | `test_verify_final_pass_untracked.py` | 4.2 | 1 |
| 108 | `test_verify_mac_memory_excluded.py` | 4.2 | 1 |
| 109 | `test_verify_final_pass_compiler.py` | 4.2 | 1 |
| 110 | `test_verify_step_owner.py` | 4.1 | 5 |
| 111 | `test_verify_final_pass_unstaged.py` | 4.1 | 1 |
| 112 | `test_verify_final_pass_edits.py` | 4.0 | 1 |
| 113 | `test_verify_final_pass_commits.py` | 4.0 | 1 |
| 114 | `test_verify_final_pass_failure.py` | 3.1 | 1 |
| 115 | `test_verify_memory_kill.py` | 3.1 | 16 |
| 116 | `test_verify_example_manifest.py` | 2.4 | 5 |
| 117 | `test_verify_hold_events.py` | 2.3 | 5 |
| 118 | `test_verify_example_markers.py` | 2.2 | 5 |
| 119 | `test_verify_mac_fixture.py` | 2.1 | 5 |
| 120 | `test_verify_metadata.py` | 2.1 | 5 |
| 121 | `test_verify_nix_shell.py` | 0.9 | 7 |
| 122 | `test_verify_lint_output.py` | 0.6 | 4 |

22 files have no gate record yet, so they have no place in the order:

- `test_verify_admission_signals.py`
- `test_verify_build_key.py`
- `test_verify_lint_cache_token_loss.py`
- `test_verify_lint_cache_versions.py`
- `test_verify_lint_judgment.py`
- `test_verify_lint_local_writes.py`
- `test_verify_mac_disk_leave.py`
- `test_verify_mac_lint_token.py`
- `test_verify_mac_only.py`
- `test_verify_python_starts.py`
- `test_verify_run_place.py`
- `test_verify_ssh_targets.py`
- `test_verify_ssh_targets_results.py`
- `test_verify_ssh_targets_retained.py`
- `test_verify_ssh_targets_retry.py`
- `test_verify_ssh_targets_routing.py`
- `test_verify_step_refusal.py`
- `test_verify_step_state.py`
- `test_verify_target_platform.py`
- `test_verify_token_mac_step.py`
- `test_verify_token_self_hold.py`
- `test_verify_verbs.py`

## Whole set after

natest writes this section after its last batch.
