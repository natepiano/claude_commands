# Phase 86: test streamlining, report B

Director check, 2026-11-07: ten Table groups, sampled evenly. All ten test names exist. Five groups have one body shape after the literals change. B049, B097, B113, B129 and B145 differ in more than literals; check each group body again before a table merge.

Date: 2026-11-07. Scope: every `scripts/` folder except `production`, `direct`, `agents`, `lint`, `message`, `hooks`, and `lib`.

The census contains 300 modules and 10,453 declared test methods.
The proposed active actions reduce that count to 10,146 methods, an estimate of 307 fewer methods.
Every table retains every input case and every assertion. A method reduction does not mean a behavior reduction.

## Counts and evidence

`Tests now` counts declared `test_` methods in top-level test classes. It does not count `subTest` rows.
`Tests after` is an estimate for these declarations. It excludes held actions and the wording decision.
Imported tests, inherited methods, base classes, and changes since a gate can change the runtime count.
The last selected gate rows record 10,408 tests in these modules. They do not describe the current source exactly.

`CPU now` sums module medians from `show.py top --limit 800`. It covers 299 measured modules and totals 2052.486 CPU s.
`test_times/test_module_cost.py` has no selected gate measurement. Its CPU is unknown.
The sums combine module histories. They are not a measurement of one complete gate.

`CPU saved` is a conservative planning estimate of 0 CPU s for each proposed action.
No command profile or before/after pair proves a CPU gain here. Actual gains remain unknown and can be negative.
The rank uses these CPU estimates first, then estimated method reductions. All CPU estimates currently tie.
This order ranks count reductions. It cannot establish the best CPU investment until the owners measure removable commands.
Do not multiply a module median by its removed-method fraction. That calculation does not identify removable CPU.

| Class | Candidate methods | Active method reduction, estimate | Decision |
| --- | ---: | ---: | --- |
| Retired | 0 | 0 | No supported retirement in this scope |
| Trivial | 0 | 0 | No deletion without an assertion keeper |
| Repeat | 0 | 0 | Different inputs remain separate cases |
| Cross-module repeat | 1 | 1 | Keep the named budget tests |
| Table | 473 | 306 | Keep every case; eight candidate methods are held |
| Wording | 1 | 0 | The user decides |
| Retained, outside these classes | 9978 | 0 | Keep the current tests |
| Total | 10453 | 307 | No suite change in this phase |

There are 473 Table candidates in 162 groups. Eight candidates in three groups belong to Phase 85.
The active tables preserve all cases from 465 original methods in 159 table methods. They remove an estimated 306 method declarations.
The cross-module action removes one further method. The wording test remains in the after count.

## Folder summary

Zero-module rows include folders with no discovered test module. Nested modules count under their first folder.
The `berth` row includes `berth/tests`. The `prioritize` row includes `prioritize/tests`.

| Folder | Modules | Tests now | Tests after, estimate | CPU now, s/gate | CPU saved, estimate s/gate | Owner |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| accounts | 9 | 849 | 827 | 39.179 | 0 | owner unknown |
| ask_a_friend | 1 | 5 | 5 | 0.772 | 0 | owner unknown |
| astra | 4 | 150 | 143 | 43.034 | 0 | owner unknown |
| aws_target | 8 | 406 | 402 | 54.560 | 0 | aws-unit |
| bake_textures | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| banned_word_analysis | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| berth | 1 | 7 | 5 | 0.081 | 0 | owner unknown |
| berth_fix | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| bevy_migration_plan | 3 | 49 | 48 | 1.278 | 0 | owner unknown |
| bevy_upgrade | 8 | 213 | 212 | 4.232 | 0 | owner unknown; former bevy-upgrade-unit |
| build_hold | 8 | 252 | 251 | 50.122 | 0 | resources-unit |
| build_queue | 15 | 658 | 652 | 53.209 | 0 | queue-unit |
| build_targets | 5 | 197 | 189 | 8.774 | 0 | resources-unit |
| buildlog | 38 | 1060 | 1035 | 225.883 | 0 | report-unit; verify-unit exceptions |
| changelog | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| claude_to_codex | 1 | 2 | 2 | 0.318 | 0 | owner unknown |
| cli_agent | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| commit_prep | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| copies | 4 | 209 | 194 | 293.506 | 0 | queue-unit; verify-unit; owner unknown |
| exclude | 1 | 10 | 10 | 2.297 | 0 | owner unknown |
| feature_log | 2 | 163 | 162 | 5.909 | 0 | owner unknown; former feature-log-unit |
| hana_instances | 1 | 21 | 21 | 0.868 | 0 | owner unknown |
| hana_shot | 1 | 115 | 113 | 23.062 | 0 | screenshot-unit |
| herdr | 8 | 583 | 575 | 67.142 | 0 | owner unknown; former herdr-trial-unit |
| hierarchies | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| history | 2 | 61 | 61 | 0.638 | 0 | owner unknown |
| issue_score | 11 | 189 | 189 | 15.848 | 0 | typesafe-unit |
| mac_test | 16 | 468 | 458 | 219.626 | 0 | verify-unit |
| maintenance | 4 | 20 | 20 | 1.699 | 0 | owner unknown |
| make_a_worktree | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| merge_branch | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| model_study | 6 | 67 | 67 | 7.617 | 0 | owner unknown |
| monitor | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| new_rust_project | 1 | 9 | 9 | 5.840 | 0 | owner unknown |
| nightly_review | 2 | 66 | 66 | 1.370 | 0 | owner unknown |
| nix_env | 1 | 28 | 28 | 0.700 | 0 | owner unknown |
| notify | 7 | 300 | 297 | 15.112 | 0 | messaging-unit |
| orphans | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| prioritize | 7 | 49 | 49 | 5.016 | 0 | owner unknown |
| release | 1 | 4 | 4 | 2.641 | 0 | owner unknown |
| review_links | 3 | 206 | 206 | 47.672 | 0 | efficiency-unit |
| rust_style | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| sccache | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| screen_record | 1 | 29 | 28 | 8.828 | 0 | owner unknown; former stalls-unit |
| settings | 4 | 30 | 30 | 6.022 | 0 | owner unknown |
| shot_report | 2 | 141 | 122 | 6.543 | 0 | screenshot-unit |
| shutdown | 18 | 423 | 421 | 37.718 | 0 | shutdown-unit |
| statusline | 1 | 75 | 74 | 28.383 | 0 | owner unknown |
| style | 49 | 2304 | 2149 | 640.364 | 0 | owner unknown |
| test_times | 5 | 111 | 108 | 11.740 + unknown | 0 | natest-unit |
| usage | 14 | 322 | 317 | 56.301 | 0 | owner unknown |
| ux_style | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| validate_and_push | 2 | 11 | 11 | 12.136 | 0 | owner unknown; former notifier-unit |
| whoami | 24 | 589 | 584 | 45.470 | 0 | owner unknown |
| worktree | 0 | 0 | 0 | 0.000 | 0 | owner unknown |
| worktree_delete | 1 | 2 | 2 | 0.976 | 0 | owner unknown |
| **Total** | **300** | **10453** | **10146** | **2052.486 + unknown** | **0** | See owners above |

### Owner evidence

Current ownership follows [builds production](builds-production.md), [natedev production](natedev-production.md), and the [natest plan](builds-natest.md).
`builds-production.md` assigns `buildlog/test_mac_idle.py` and `buildlog/test_port_lint.py` to verify-unit.
It assigns `copies/test_copy_guard.py` to verify-unit and `copies/test_copies.py` to queue-unit.
It does not assign the two other copy test modules by name. Their owner remains unknown.
The same document names shutdown-unit under sdlc for production rename and worker identity.
Retired unit names describe former owners. A retired unit does not imply retired production code.
The accounts-unit row owns `docs/as-built/accounts.md`. It does not establish ownership of the accounts scripts.

## Ranked actions

Each Table action uses the proposed keeper in its module section. Each named original method becomes a labelled case.
CPU values below are planning estimates. The report credits no measured saving.
Held actions and wording decisions follow the active list. Their reductions are absent from the after counts.

| Rank | Action | Module under scripts/ | Candidate methods → methods after, estimate | Class | Covering test or reason | CPU saved, estimate s/gate | Owner |
| ---: | --- | --- | ---: | --- | --- | ---: | --- |
| 1 | Merge cases | style/test_style_footprint.py | 71 → 22 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 2 | Merge cases | style/test_finding_sites.py | 49 → 6 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 3 | Merge cases | style/test_style_ledger_sites.py | 32 → 10 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 4 | Merge cases | accounts/test_accounts.py | 26 → 7 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 5 | Merge cases | shot_report/test_transcripts.py | 25 → 6 | Table | Named table keepers below retain all inputs and assertions | 0 | screenshot-unit |
| 6 | Merge cases | copies/test_copy_guard.py | 17 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 7 | Merge cases | style/test_style_plan_module_owners.py | 11 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 8 | Merge cases | herdr/test_herdr.py | 13 → 5 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown; former herdr-trial-unit |
| 9 | Merge cases | buildlog/test_step_row.py | 13 → 6 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 10 | Merge cases | style/test_bulk_run.py | 12 → 6 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 11 | Merge cases | astra/test_design_roles.py | 10 → 5 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 12 | Merge cases | style/test_style_plan_gate.py | 10 → 5 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 13 | Merge cases | style/test_style_plan_ownership.py | 6 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 14 | Merge cases | build_targets/test_build_targets.py | 7 → 3 | Table | Named table keepers below retain all inputs and assertions | 0 | resources-unit |
| 15 | Merge cases | build_targets/test_targets.py | 6 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | resources-unit |
| 16 | Merge cases | mac_test/test_offload_lifecycle.py | 6 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 17 | Merge cases | build_queue/test_queue_place.py | 6 → 3 | Table | Named table keepers below retain all inputs and assertions | 0 | queue-unit |
| 18 | Merge cases | buildlog/test_record_rerun.py | 4 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 19 | Merge cases | buildlog/test_store_identity.py | 4 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 20 | Merge cases | style/test_approved_renames.py | 6 → 3 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 21 | Merge cases | style/test_style_ledger.py | 5 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 22 | Merge cases | usage/test_compaction_replay.py | 5 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 23 | Merge cases | accounts/test_launch_accounts.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 24 | Merge cases | astra/test_astra_review.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 25 | Merge cases | aws_target/test_deploy.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | aws-unit |
| 26 | Merge cases | berth/tests/test_work_order.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 27 | Merge cases | buildlog/test_charts.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 28 | Merge cases | buildlog/test_sync.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 29 | Merge cases | buildlog/test_target_report.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 30 | Merge cases | hana_shot/test_hana_shot.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | screenshot-unit |
| 31 | Merge cases | mac_test/test_linux_runner.py | 3 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 32 | Merge cases | mac_test/test_offload.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 33 | Merge cases | notify/test_pushover.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | messaging-unit |
| 34 | Merge cases | style/test_bulk_rename.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 35 | Merge cases | style/test_rules.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 36 | Merge cases | style/test_style_sync.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 37 | Merge cases | test_times/test_coverage_overlap.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | natest-unit |
| 38 | Merge cases | usage/test_usage_report.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 39 | Merge cases | whoami/test_worker_usage.py | 4 → 2 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 40 | Merge cases | accounts/test_unit_watchdog.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 41 | Merge cases | aws_target/test_box.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | aws-unit |
| 42 | Merge cases | aws_target/test_control.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | aws-unit |
| 43 | Merge cases | bevy_migration_plan/test_bevy_migration_dependency_check.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 44 | Merge cases | bevy_upgrade/test_upstream.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown; former bevy-upgrade-unit |
| 45 | Merge cases | build_hold/test_build_hold.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | resources-unit |
| 46 | Merge cases | build_queue/test_admission_service.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | queue-unit |
| 47 | Merge cases | build_queue/test_progress_profile.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | queue-unit |
| 48 | Merge cases | build_queue/test_queue_lines.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | queue-unit |
| 49 | Merge cases | buildlog/test_mac_idle.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 50 | Merge cases | buildlog/test_mac_report.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 51 | Merge cases | buildlog/test_record.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 52 | Merge cases | buildlog/test_recorder_health.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 53 | Merge cases | buildlog/test_report.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 54 | Merge cases | buildlog/test_test_phase_peak.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | report-unit |
| 55 | Merge cases | feature_log/test_feature_log.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown; former feature-log-unit |
| 56 | Merge cases | mac_test/test_copy_watch.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 57 | Merge cases | mac_test/test_offload_forward.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | verify-unit |
| 58 | Merge cases | notify/test_push_texts.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | messaging-unit |
| 59 | Merge cases | screen_record/test_screen_record.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown; former stalls-unit |
| 60 | Merge cases | agents/test_pane_record.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | shutdown-unit |
| 61 | Merge cases | agents/test_stop_resume.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | shutdown-unit |
| 62 | Merge cases | statusline/test_statusline.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 63 | Merge cases | style/test_bulk_constants.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 64 | Merge cases | style/test_standing_checkpoint.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 65 | Merge cases | style/test_style_plan_renames.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 66 | Remove the repeated budget assertion | style/test_style_sync.py | 1 → 0 | Cross-module repeat | `SendBudgetTests.test_default_budget_tracks_delivery_and_cleanup_changes_without_reload`; retain caller budget tests | 0 | owner unknown |
| 67 | Merge cases | test_times/test_module_run.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | natest-unit |
| 68 | Merge cases | whoami/test_account.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 69 | Merge cases | whoami/test_codex_alert_accounts.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| 70 | Merge cases | whoami/test_quota_zero_episode.py | 2 → 1 | Table | Named table keepers below retain all inputs and assertions | 0 | owner unknown |
| Held | Merge cases after Phase 85 | style/test_style_ledger_findings.py | 8 → 3 | Table | Named table keepers below; no current action | 0 | owner unknown |
| User decision | Consider the documentation wording test | copies/test_review_remote_copies.py | 1 → 1 | Wording | Documentation terms only; no runtime result | 0 | owner unknown |

### CPU priorities that need profiles

The current CPU costs below are measurements from gate history. They are not estimates of savings.
These profile priorities do not change the count ranking above.

| Module | Median CPU s/gate | Existing proof to retain | Next measurement | Owner |
| --- | ---: | --- | --- | --- |
| copies/test_review_remote_copies.py | 210.927 | Receive interruption recovery, repeated receive, owner removal, and real claim locks | Count each child command in each recovery case | owner unknown |
| style/test_crate_run.py | 70.687 | Admission refusal after planning and validation aborts | Separate repository setup from each actual pipeline command | owner unknown |
| style/test_style_sync.py | 64.626 | Real lint process signals and handoff outcomes | Price repository setup paid by the budget-only methods | owner unknown |
| copies/test_copy_guard.py | 62.180 | Real scanner decisions and route behavior | Separate scanner CPU from hook entry CPU | verify-unit |
| mac_test/test_offload.py | 42.598 | Local preflight, scope, and remote command behavior | Price fixture setup for the two proposed tables | verify-unit |
| mac_test/test_offload_forward.py | 41.825 | Environment forwarding, quoting, fetch, and refusal side effects | Price the CLI starts in the refusal table | verify-unit |

`test_argument_refusals_start_no_work` already has a table. It still starts the CLI for its cases.
Compare direct parser calls with a few real CLI refusal cases. Keep the no-work and stale-result checks.
This is a profiling proposal. It has no credited CPU gain or method reduction.
`test_review_remote_copies.py` already shares an immutable Git seed. Do not propose that change again.
The recovery tables test different interruption points. Their similar commands do not prove repeated behavior.
Keep the live-claim and cancellation lock tests. Mocks cannot prove their process and lock behavior.

### Implementation requirements for later phases

1. Measure each selected module with `module_cost.py --tests 10 --commands 10` before an edit.
2. Map each existing assertion to its proposed table row or retained test.
3. Keep each production decision in the tested function.
4. Preserve a fresh mutable fixture for each case that requires one.
5. Keep a fresh interpreter for each cold-import case.
6. Run an eligible coverage sweep before a proposed deletion.
7. Break each moved decision in a temporary copy and confirm that its new check fails.
8. Measure three before/after pairs with separate temporary roots under `/dev/shm`.
9. Record CPU, processes, execs, wall time, and host load.
10. Reorder the actions when measured CPU savings replace the planning estimates.

A table can share immutable setup. It must reset mutable state before the next row.
Move only setup that the cases can share safely. Do not replace a real decision with a configured mock.
Keep one typed case record for each semantic role. Use named outcomes for distinct states.
Do not use `Any`, anonymous tuple positions, or optional fields that hide different case roles.
These are implementation proposals. This report adds no gate, check, or production rule.

## Module census

Every discovered module appears below. `T` means Table, `X` means Cross-module repeat, and `W` means Wording.
`R` means Retired, `Tr` means Trivial, and `Rep` means Repeat. `Keep` counts the other declared methods.
The class columns count original methods. They include the held candidates.
`Gate tests` shows the last selected historical count. A dash means no selected measurement.
`After` remains an estimate. The held findings module retains all 139 methods.

| Module under scripts/ | Now | Keep | R | Tr | Rep | X | T | W | After | Gate tests | CPU now, s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| accounts/test_accounts.py | 468 | 442 | 0 | 0 | 0 | 0 | 26 | 0 | 449 | 468 | 10.403 |
| accounts/test_launch_accounts.py | 20 | 17 | 0 | 0 | 0 | 0 | 3 | 0 | 18 | 20 | 11.225 |
| accounts/test_moves.py | 36 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 36 | 36 | 1.289 |
| accounts/test_new_account.py | 29 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 29 | 0.433 |
| accounts/test_next_moves.py | 30 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 30 | 30 | 1.132 |
| accounts/test_refusal_watch.py | 162 | 162 | 0 | 0 | 0 | 0 | 0 | 0 | 162 | 162 | 5.152 |
| accounts/test_refusals.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 0.346 |
| accounts/test_role_map.py | 34 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 34 | 1.210 |
| accounts/test_unit_watchdog.py | 52 | 50 | 0 | 0 | 0 | 0 | 2 | 0 | 51 | 52 | 7.989 |
| ask_a_friend/test_end_friend.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.772 |
| astra/test_astra_review.py | 22 | 18 | 0 | 0 | 0 | 0 | 4 | 0 | 20 | 95 | 11.813 |
| astra/test_astra_roles.py | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 15 | 10.097 |
| astra/test_astra_work.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 0.401 |
| astra/test_design_roles.py | 104 | 94 | 0 | 0 | 0 | 0 | 10 | 0 | 99 | 104 | 20.723 |
| aws_target/test_admin_secrets.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.904 |
| aws_target/test_box.py | 61 | 59 | 0 | 0 | 0 | 0 | 2 | 0 | 60 | 61 | 18.447 |
| aws_target/test_control.py | 48 | 46 | 0 | 0 | 0 | 0 | 2 | 0 | 47 | 45 | 5.303 |
| aws_target/test_deploy.py | 127 | 124 | 0 | 0 | 0 | 0 | 3 | 0 | 125 | 127 | 22.326 |
| aws_target/test_gate_trial.py | 62 | 62 | 0 | 0 | 0 | 0 | 0 | 0 | 62 | 60 | 2.407 |
| aws_target/test_ledger.py | 39 | 39 | 0 | 0 | 0 | 0 | 0 | 0 | 39 | 39 | 1.017 |
| aws_target/test_measurement.py | 24 | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 24 | 24 | 1.071 |
| aws_target/test_remote.py | 37 | 37 | 0 | 0 | 0 | 0 | 0 | 0 | 37 | 37 | 1.085 |
| berth/tests/test_work_order.py | 7 | 4 | 0 | 0 | 0 | 0 | 3 | 0 | 5 | 7 | 0.081 |
| bevy_migration_plan/test_bevy_migration_count_pattern.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 0.676 |
| bevy_migration_plan/test_bevy_migration_dependency_check.py | 36 | 34 | 0 | 0 | 0 | 0 | 2 | 0 | 35 | 36 | 0.157 |
| bevy_migration_plan/test_bevy_migration_rerun.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.445 |
| bevy_upgrade/test_cli.py | 43 | 43 | 0 | 0 | 0 | 0 | 0 | 0 | 43 | 43 | 2.260 |
| bevy_upgrade/test_crates_io_pacer.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.080 |
| bevy_upgrade/test_pins.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 0.091 |
| bevy_upgrade/test_upgrade_check.py | 33 | 33 | 0 | 0 | 0 | 0 | 0 | 0 | 33 | 33 | 0.125 |
| bevy_upgrade/test_upgrade_record.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 0.692 |
| bevy_upgrade/test_upgrade_report.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 0.119 |
| bevy_upgrade/test_upgrade_schedule.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 0.725 |
| bevy_upgrade/test_upstream.py | 39 | 37 | 0 | 0 | 0 | 0 | 2 | 0 | 38 | 39 | 0.140 |
| build_hold/test_aws_hold_cli.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 2.382 |
| build_hold/test_build_hold.py | 146 | 144 | 0 | 0 | 0 | 0 | 2 | 0 | 145 | 146 | 28.525 |
| build_hold/test_cargo_wrapper.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 2.106 |
| build_hold/test_hold_applies.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 6.289 |
| build_hold/test_hold_enforcer.py | 24 | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 24 | 24 | 0.777 |
| build_hold/test_hold_shortcut.py | 11 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 11 | 2.181 |
| build_hold/test_override_guard.py | 29 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 29 | 6.921 |
| build_hold/test_target_hold.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 0.941 |
| build_queue/test_admission_client.py | 65 | 65 | 0 | 0 | 0 | 0 | 0 | 0 | 65 | 64 | 5.025 |
| build_queue/test_admission_client_wait.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 4.231 |
| build_queue/test_admission_protocol.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 17 | 1.182 |
| build_queue/test_admission_service.py | 209 | 207 | 0 | 0 | 0 | 0 | 2 | 0 | 208 | 192 | 13.345 |
| build_queue/test_build_queue.py | 65 | 65 | 0 | 0 | 0 | 0 | 0 | 0 | 65 | 65 | 5.044 |
| build_queue/test_caller_admission_clock.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 1.047 |
| build_queue/test_ci_cargo_front.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 4.961 |
| build_queue/test_editor_scope.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.635 |
| build_queue/test_head_facts.py | 54 | 54 | 0 | 0 | 0 | 0 | 0 | 0 | 54 | 53 | 1.500 |
| build_queue/test_progress_profile.py | 26 | 24 | 0 | 0 | 0 | 0 | 2 | 0 | 25 | 26 | 0.145 |
| build_queue/test_queue_card.py | 29 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 25 | 1.223 |
| build_queue/test_queue_evidence.py | 49 | 49 | 0 | 0 | 0 | 0 | 0 | 0 | 49 | 48 | 1.592 |
| build_queue/test_queue_lines.py | 19 | 17 | 0 | 0 | 0 | 0 | 2 | 0 | 18 | 19 | 0.079 |
| build_queue/test_queue_place.py | 33 | 27 | 0 | 0 | 0 | 0 | 6 | 0 | 30 | 33 | 10.125 |
| build_queue/test_queue_view.py | 63 | 63 | 0 | 0 | 0 | 0 | 0 | 0 | 63 | 56 | 3.075 |
| build_targets/test_aws_status.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 1.318 |
| build_targets/test_build_targets.py | 78 | 71 | 0 | 0 | 0 | 0 | 7 | 0 | 74 | 78 | 4.873 |
| build_targets/test_ssh_eligibility.py | 33 | 33 | 0 | 0 | 0 | 0 | 0 | 0 | 33 | 32 | 1.451 |
| build_targets/test_targets.py | 50 | 44 | 0 | 0 | 0 | 0 | 6 | 0 | 46 | 50 | 0.902 |
| build_targets/test_working_set.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 0.230 |
| buildlog/test_aws_report.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 1.289 |
| buildlog/test_charts.py | 54 | 50 | 0 | 0 | 0 | 0 | 4 | 0 | 52 | 54 | 14.006 |
| buildlog/test_ci.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.735 |
| buildlog/test_costs.py | 26 | 26 | 0 | 0 | 0 | 0 | 0 | 0 | 26 | 26 | 1.549 |
| buildlog/test_disk.py | 23 | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 23 | 23 | 2.442 |
| buildlog/test_gate_index.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 0.903 |
| buildlog/test_holds.py | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 20 | 20 | 0.887 |
| buildlog/test_index.py | 82 | 82 | 0 | 0 | 0 | 0 | 0 | 0 | 82 | 78 | 32.754 |
| buildlog/test_index_rerun.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.756 |
| buildlog/test_launch_recording.py | 16 | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 16 | 16 | 2.734 |
| buildlog/test_launches.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 4.164 |
| buildlog/test_log_isolation.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 21.153 |
| buildlog/test_mac_idle.py | 63 | 61 | 0 | 0 | 0 | 0 | 2 | 0 | 62 | 56 | 4.995 |
| buildlog/test_mac_idle_alert.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.831 |
| buildlog/test_mac_outcome.py | 12 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 12 | 12 | 0.707 |
| buildlog/test_mac_report.py | 41 | 39 | 0 | 0 | 0 | 0 | 2 | 0 | 40 | 41 | 20.565 |
| buildlog/test_memory_records.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 6.231 |
| buildlog/test_memory_window.py | 38 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 38 | 38 | 9.273 |
| buildlog/test_moments.py | 29 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 29 | 3.255 |
| buildlog/test_parse.py | 31 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 31 | 2.729 |
| buildlog/test_port_lint.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 1.545 |
| buildlog/test_record.py | 86 | 84 | 0 | 0 | 0 | 0 | 2 | 0 | 85 | 86 | 23.142 |
| buildlog/test_record_rerun.py | 30 | 26 | 0 | 0 | 0 | 0 | 4 | 0 | 27 | 30 | 6.129 |
| buildlog/test_recorder_health.py | 12 | 10 | 0 | 0 | 0 | 0 | 2 | 0 | 11 | 12 | 1.723 |
| buildlog/test_report.py | 171 | 169 | 0 | 0 | 0 | 0 | 2 | 0 | 170 | 171 | 27.953 |
| buildlog/test_report_tree_cancel.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.750 |
| buildlog/test_rust_release.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 3.654 |
| buildlog/test_sample.py | 30 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 30 | 30 | 2.023 |
| buildlog/test_step_row.py | 49 | 36 | 0 | 0 | 0 | 0 | 13 | 0 | 42 | 49 | 2.595 |
| buildlog/test_store.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 0.728 |
| buildlog/test_store_identity.py | 6 | 2 | 0 | 0 | 0 | 0 | 4 | 0 | 3 | 6 | 1.453 |
| buildlog/test_sync.py | 19 | 16 | 0 | 0 | 0 | 0 | 3 | 0 | 17 | 19 | 0.906 |
| buildlog/test_target_report.py | 9 | 6 | 0 | 0 | 0 | 0 | 3 | 0 | 7 | 9 | 0.813 |
| buildlog/test_target_sync.py | 21 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 21 | 21 | 0.842 |
| buildlog/test_test_phase_peak.py | 14 | 12 | 0 | 0 | 0 | 0 | 2 | 0 | 13 | 14 | 9.462 |
| buildlog/test_treekey.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 0.954 |
| buildlog/test_vault_pull.py | 32 | 32 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 32 | 1.371 |
| buildlog/test_width_trial.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 7.882 |
| claude_to_codex/test_sync.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.318 |
| copies/test_copies.py | 105 | 105 | 0 | 0 | 0 | 0 | 0 | 0 | 105 | 69 | 14.206 |
| copies/test_copy_guard.py | 51 | 34 | 0 | 0 | 0 | 0 | 17 | 0 | 36 | 51 | 62.180 |
| copies/test_remote_copies.py | 24 | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 24 | 22 | 6.193 |
| copies/test_review_remote_copies.py | 29 | 28 | 0 | 0 | 0 | 0 | 0 | 1 | 29 | 28 | 210.927 |
| exclude/test_exclude.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 2.297 |
| feature_log/test_feature_log.py | 129 | 127 | 0 | 0 | 0 | 0 | 2 | 0 | 128 | 129 | 2.509 |
| feature_log/test_inventory.py | 34 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 34 | 3.400 |
| hana_instances/test_hana_instances.py | 21 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 21 | 21 | 0.868 |
| hana_shot/test_hana_shot.py | 115 | 112 | 0 | 0 | 0 | 0 | 3 | 0 | 113 | 115 | 23.062 |
| herdr/test_conversation_reader.py | 31 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 31 | 0.834 |
| herdr/test_detection_rules.py | 14 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 14 | 14 | 0.402 |
| herdr/test_herdr.py | 363 | 350 | 0 | 0 | 0 | 0 | 13 | 0 | 355 | 358 | 40.501 |
| herdr/test_herdr_sessions.py | 38 | 38 | 0 | 0 | 0 | 0 | 0 | 0 | 38 | 38 | 9.211 |
| herdr/test_pane_launch.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.198 |
| herdr/test_recorded_marks.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.195 |
| herdr/test_side_pane.py | 31 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 31 | 4.253 |
| herdr/test_worker_panes.py | 101 | 101 | 0 | 0 | 0 | 0 | 0 | 0 | 101 | 100 | 11.548 |
| history/test_rounds.py | 57 | 57 | 0 | 0 | 0 | 0 | 0 | 0 | 57 | 57 | 0.166 |
| history/test_routed.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.472 |
| issue_score/test_apply_labels.py | 16 | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 16 | 16 | 0.826 |
| issue_score/test_blind.py | 7 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 7 | 1.553 |
| issue_score/test_client.py | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 15 | 0.390 |
| issue_score/test_config.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.455 |
| issue_score/test_decide.py | 30 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 30 | 30 | 0.457 |
| issue_score/test_evaluate.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 4.975 |
| issue_score/test_experiment.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 0.637 |
| issue_score/test_questions.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 0.340 |
| issue_score/test_reassess.py | 12 | 12 | 0 | 0 | 0 | 0 | 0 | 0 | 12 | 12 | 4.479 |
| issue_score/test_score_issues.py | 34 | 34 | 0 | 0 | 0 | 0 | 0 | 0 | 34 | 34 | 1.377 |
| issue_score/test_vault.py | 21 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 21 | 21 | 0.359 |
| mac_test/test_audit.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 3.221 |
| mac_test/test_cargo_manifest.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.083 |
| mac_test/test_copy_watch.py | 27 | 25 | 0 | 0 | 0 | 0 | 2 | 0 | 26 | 27 | 9.214 |
| mac_test/test_floor_sweep.py | 14 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 14 | 14 | 6.905 |
| mac_test/test_linux_runner.py | 93 | 90 | 0 | 0 | 0 | 0 | 3 | 0 | 91 | 90 | 8.406 |
| mac_test/test_mac_test.py | 124 | 124 | 0 | 0 | 0 | 0 | 0 | 0 | 124 | 124 | 17.215 |
| mac_test/test_offload.py | 70 | 66 | 0 | 0 | 0 | 0 | 4 | 0 | 68 | 61 | 42.598 |
| mac_test/test_offload_cleanup.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 1.513 |
| mac_test/test_offload_cleanup_leftovers.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 25.338 |
| mac_test/test_offload_cleanup_results.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 19.021 |
| mac_test/test_offload_cleanup_takeover.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 9.719 |
| mac_test/test_offload_forward.py | 18 | 16 | 0 | 0 | 0 | 0 | 2 | 0 | 17 | 17 | 41.825 |
| mac_test/test_offload_lifecycle.py | 21 | 15 | 0 | 0 | 0 | 0 | 6 | 0 | 17 | 21 | 32.032 |
| mac_test/test_process_start_claims.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.578 |
| mac_test/test_target_slots.py | 32 | 32 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 32 | 1.000 |
| mac_test/test_target_state.py | 22 | 22 | 0 | 0 | 0 | 0 | 0 | 0 | 22 | 22 | 0.958 |
| maintenance/test_agent_notes_job.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 1.108 |
| maintenance/test_herdr_upkeep.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.291 |
| maintenance/test_schedule.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.079 |
| maintenance/test_vault_commit.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 0.221 |
| model_study/test_compare.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 6.079 |
| model_study/test_phases.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 0.815 |
| model_study/test_report.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 0.154 |
| model_study/test_rerun.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 0.266 |
| model_study/test_stats.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 0.110 |
| model_study/test_turns.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 0.193 |
| new_rust_project/test_rust_generate.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 5.840 |
| nightly_review/test_candidates.py | 40 | 40 | 0 | 0 | 0 | 0 | 0 | 0 | 40 | 40 | 0.229 |
| nightly_review/test_nightly_review.py | 26 | 26 | 0 | 0 | 0 | 0 | 0 | 0 | 26 | 50 | 1.141 |
| nix_env/test_heal.py | 28 | 28 | 0 | 0 | 0 | 0 | 0 | 0 | 28 | 28 | 0.700 |
| notify/test_auto_silence.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 0.434 |
| notify/test_decision_priority.py | 7 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 7 | 0.189 |
| notify/test_push_texts.py | 50 | 48 | 0 | 0 | 0 | 0 | 2 | 0 | 49 | 50 | 9.562 |
| notify/test_pushover.py | 82 | 78 | 0 | 0 | 0 | 0 | 4 | 0 | 80 | 82 | 1.316 |
| notify/test_review.py | 110 | 110 | 0 | 0 | 0 | 0 | 0 | 0 | 110 | 110 | 1.922 |
| notify/test_silence.py | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 27 | 27 | 1.579 |
| notify/test_user_action.py | 7 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 7 | 7 | 0.110 |
| prioritize/tests/test_install_watcher.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.078 |
| prioritize/tests/test_renumber.py | 30 | 30 | 0 | 0 | 0 | 0 | 0 | 0 | 30 | 30 | 0.139 |
| prioritize/tests/test_run_watcher.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 4.304 |
| prioritize/tests/test_runner_lock.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.139 |
| prioritize/tests/test_snapshot.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.127 |
| prioritize/tests/test_watch_signature.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.081 |
| prioritize/tests/test_writer_lock.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.148 |
| release/test_release.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 2.641 |
| review_links/test_change_page.py | 139 | 139 | 0 | 0 | 0 | 0 | 0 | 0 | 139 | 139 | 39.809 |
| review_links/test_review_link.py | 31 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 31 | 5.978 |
| review_links/test_serve.py | 36 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 36 | 36 | 1.885 |
| screen_record/test_screen_record.py | 29 | 27 | 0 | 0 | 0 | 0 | 2 | 0 | 28 | 29 | 8.828 |
| settings/test_ensure_git_filters.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.588 |
| settings/test_git_filters_retire.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.242 |
| settings/test_local_key_notices.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 0.147 |
| settings/test_settings_local_keys.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 5.045 |
| shot_report/test_shot_report.py | 47 | 47 | 0 | 0 | 0 | 0 | 0 | 0 | 47 | 47 | 2.578 |
| shot_report/test_transcripts.py | 94 | 69 | 0 | 0 | 0 | 0 | 25 | 0 | 75 | 94 | 3.965 |
| agents/test_account_move.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 1.663 |
| agents/test_deadline.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 0.471 |
| agents/test_inventory.py | 57 | 57 | 0 | 0 | 0 | 0 | 0 | 0 | 57 | 57 | 8.645 |
| agents/test_native_label_filtering.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 0.441 |
| agents/test_pane_record.py | 32 | 30 | 0 | 0 | 0 | 0 | 2 | 0 | 31 | 32 | 4.212 |
| agents/test_pane_restore.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 6.081 |
| agents/test_probe_inventory.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 1.692 |
| agents/test_remote.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 0.988 |
| agents/test_remote_control.py | 18 | 18 | 0 | 0 | 0 | 0 | 0 | 0 | 18 | 18 | 1.225 |
| agents/test_restart.py | 69 | 69 | 0 | 0 | 0 | 0 | 0 | 0 | 69 | 68 | 2.513 |
| agents/test_restart_support.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.258 |
| agents/test_resume_command.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.654 |
| agents/test_send_guard.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.851 |
| agents/test_shutdown_record.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.887 |
| agents/test_shutdown_status.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 0.941 |
| agents/test_stop_resume.py | 163 | 161 | 0 | 0 | 0 | 0 | 2 | 0 | 162 | 162 | 4.617 |
| agents/test_support.py | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0.828 |
| agents/test_time_limit.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.751 |
| statusline/test_statusline.py | 75 | 73 | 0 | 0 | 0 | 0 | 2 | 0 | 74 | 75 | 28.383 |
| style/test_add_style.py | 31 | 31 | 0 | 0 | 0 | 0 | 0 | 0 | 31 | 31 | 49.221 |
| style/test_approved_renames.py | 89 | 83 | 0 | 0 | 0 | 0 | 6 | 0 | 86 | 89 | 0.517 |
| style/test_bulk_constants.py | 44 | 42 | 0 | 0 | 0 | 0 | 2 | 0 | 43 | 44 | 0.252 |
| style/test_bulk_rename.py | 118 | 114 | 0 | 0 | 0 | 0 | 4 | 0 | 116 | 118 | 12.669 |
| style/test_bulk_run.py | 108 | 96 | 0 | 0 | 0 | 0 | 12 | 0 | 102 | 108 | 41.313 |
| style/test_candidate_generators.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | 9 | 0.123 |
| style/test_coverage_report.py | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 20 | 20 | 14.137 |
| style/test_crate_run.py | 97 | 97 | 0 | 0 | 0 | 0 | 0 | 0 | 97 | 97 | 70.687 |
| style/test_declarations.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 0.147 |
| style/test_finding_sites.py | 68 | 19 | 0 | 0 | 0 | 0 | 49 | 0 | 25 | 68 | 6.659 |
| style/test_git_process.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 4 | 0.111 |
| style/test_mechanical_classes.py | 22 | 22 | 0 | 0 | 0 | 0 | 0 | 0 | 22 | 22 | 2.093 |
| style/test_order_waste.py | 14 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 14 | 14 | 1.964 |
| style/test_refill_speed.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.229 |
| style/test_rules.py | 50 | 46 | 0 | 0 | 0 | 0 | 4 | 0 | 48 | 50 | 1.296 |
| style/test_standing_checkpoint.py | 6 | 4 | 0 | 0 | 0 | 0 | 2 | 0 | 5 | 6 | 28.695 |
| style/test_style_admin.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 0.115 |
| style/test_style_footprint.py | 271 | 200 | 0 | 0 | 0 | 0 | 71 | 0 | 222 | 271 | 16.731 |
| style/test_style_ledger.py | 100 | 95 | 0 | 0 | 0 | 0 | 5 | 0 | 97 | 100 | 23.942 |
| style/test_style_ledger_corrections.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.215 |
| style/test_style_ledger_findings.py | 139 | 131 | 0 | 0 | 0 | 0 | 8 | 0 | 139 | 139 | 46.378 |
| style/test_style_ledger_sites.py | 169 | 137 | 0 | 0 | 0 | 0 | 32 | 0 | 147 | 169 | 38.830 |
| style/test_style_ledger_tickets.py | 195 | 195 | 0 | 0 | 0 | 0 | 0 | 0 | 195 | 195 | 46.481 |
| style/test_style_plan.py | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 27 | 27 | 6.056 |
| style/test_style_plan_ancestry.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 4.278 |
| style/test_style_plan_batches.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 9.174 |
| style/test_style_plan_borrowing.py | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 15 | 6.789 |
| style/test_style_plan_checkpoint_refill.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.237 |
| style/test_style_plan_decision_completion.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.797 |
| style/test_style_plan_decisions.py | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 20 | 20 | 4.811 |
| style/test_style_plan_declarations.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.994 |
| style/test_style_plan_file_renames.py | 27 | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 27 | 27 | 5.496 |
| style/test_style_plan_gate.py | 64 | 54 | 0 | 0 | 0 | 0 | 10 | 0 | 59 | 64 | 13.973 |
| style/test_style_plan_module_owners.py | 13 | 2 | 0 | 0 | 0 | 0 | 11 | 0 | 4 | 13 | 3.415 |
| style/test_style_plan_ownership.py | 18 | 12 | 0 | 0 | 0 | 0 | 6 | 0 | 13 | 18 | 4.361 |
| style/test_style_plan_phases.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 5.116 |
| style/test_style_plan_pipeline_growth.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 1.795 |
| style/test_style_plan_recovery.py | 26 | 26 | 0 | 0 | 0 | 0 | 0 | 0 | 26 | 26 | 5.801 |
| style/test_style_plan_refill.py | 36 | 36 | 0 | 0 | 0 | 0 | 0 | 0 | 36 | 36 | 4.519 |
| style/test_style_plan_renames.py | 32 | 30 | 0 | 0 | 0 | 0 | 2 | 0 | 31 | 32 | 4.967 |
| style/test_style_plan_selection.py | 29 | 29 | 0 | 0 | 0 | 0 | 0 | 0 | 29 | 29 | 8.820 |
| style/test_style_plan_sites.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 4.172 |
| style/test_style_plan_split.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 1.940 |
| style/test_style_plan_structure.py | 17 | 17 | 0 | 0 | 0 | 0 | 0 | 0 | 17 | 17 | 4.029 |
| style/test_style_plan_type_batches.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 1.823 |
| style/test_style_proposals.py | 186 | 186 | 0 | 0 | 0 | 0 | 0 | 0 | 186 | 186 | 58.515 |
| style/test_style_rules.py | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6 | 6 | 0.115 |
| style/test_style_sync.py | 62 | 57 | 0 | 0 | 0 | 1 | 4 | 0 | 59 | 62 | 64.626 |
| style/test_unit_files.py | 11 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 11 | 0.940 |
| test_times/test_coverage_overlap.py | 38 | 34 | 0 | 0 | 0 | 0 | 4 | 0 | 36 | 38 | 5.344 |
| test_times/test_gate_record.py | 11 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 11 | 0.294 |
| test_times/test_module_cost.py | 9 | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 9 | — | unknown |
| test_times/test_module_run.py | 20 | 18 | 0 | 0 | 0 | 0 | 2 | 0 | 19 | 12 | 4.550 |
| test_times/test_show.py | 33 | 33 | 0 | 0 | 0 | 0 | 0 | 0 | 33 | 24 | 1.552 |
| usage/test_activity.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 0.090 |
| usage/test_agents_log.py | 41 | 41 | 0 | 0 | 0 | 0 | 0 | 0 | 41 | 41 | 4.125 |
| usage/test_collect.py | 65 | 65 | 0 | 0 | 0 | 0 | 0 | 0 | 65 | 65 | 2.952 |
| usage/test_collect_incremental.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.249 |
| usage/test_collect_mac.py | 11 | 11 | 0 | 0 | 0 | 0 | 0 | 0 | 11 | 11 | 4.009 |
| usage/test_collect_mac_recovery.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 2.519 |
| usage/test_collect_mac_roles.py | 8 | 8 | 0 | 0 | 0 | 0 | 0 | 0 | 8 | 8 | 3.293 |
| usage/test_collect_processes.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 1.178 |
| usage/test_compaction_replay.py | 17 | 12 | 0 | 0 | 0 | 0 | 5 | 0 | 14 | 17 | 4.741 |
| usage/test_outcomes.py | 53 | 53 | 0 | 0 | 0 | 0 | 0 | 0 | 53 | 53 | 7.575 |
| usage/test_rollouts.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 0.136 |
| usage/test_session_start_hook.py | 20 | 20 | 0 | 0 | 0 | 0 | 0 | 0 | 20 | 20 | 2.021 |
| usage/test_usage_report.py | 49 | 45 | 0 | 0 | 0 | 0 | 4 | 0 | 47 | 49 | 21.898 |
| usage/test_waste_watch.py | 19 | 19 | 0 | 0 | 0 | 0 | 0 | 0 | 19 | 19 | 1.515 |
| validate_and_push/test_ci_tick.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.229 |
| validate_and_push/test_rerun.py | 10 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 10 | 11.907 |
| whoami/test_account.py | 46 | 44 | 0 | 0 | 0 | 0 | 2 | 0 | 45 | 46 | 0.306 |
| whoami/test_agent_accounts.py | 24 | 24 | 0 | 0 | 0 | 0 | 0 | 0 | 24 | 24 | 0.531 |
| whoami/test_agent_notes.py | 106 | 106 | 0 | 0 | 0 | 0 | 0 | 0 | 106 | 106 | 1.458 |
| whoami/test_codex_alert_accounts.py | 77 | 75 | 0 | 0 | 0 | 0 | 2 | 0 | 76 | 77 | 7.169 |
| whoami/test_codex_homes.py | 35 | 35 | 0 | 0 | 0 | 0 | 0 | 0 | 35 | 35 | 0.419 |
| whoami/test_codex_pacer.py | 14 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 14 | 14 | 0.140 |
| whoami/test_five_hour.py | 23 | 23 | 0 | 0 | 0 | 0 | 0 | 0 | 23 | 23 | 0.810 |
| whoami/test_note_file.py | 14 | 14 | 0 | 0 | 0 | 0 | 0 | 0 | 14 | 14 | 0.308 |
| whoami/test_profile_notes_pass.py | 21 | 21 | 0 | 0 | 0 | 0 | 0 | 0 | 21 | 21 | 0.989 |
| whoami/test_quota_alert.py | 32 | 32 | 0 | 0 | 0 | 0 | 0 | 0 | 32 | 32 | 1.060 |
| whoami/test_quota_alert_cancellation.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.459 |
| whoami/test_quota_alert_claude.py | 16 | 16 | 0 | 0 | 0 | 0 | 0 | 0 | 16 | 16 | 0.988 |
| whoami/test_quota_alert_delivery.py | 13 | 13 | 0 | 0 | 0 | 0 | 0 | 0 | 13 | 13 | 1.088 |
| whoami/test_quota_alert_recovery.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 1.254 |
| whoami/test_quota_alert_recovery_failure.py | 4 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 | 4 | 1.301 |
| whoami/test_quota_alert_refusals.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 1.192 |
| whoami/test_quota_alert_restore.py | 3 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 3 | 3 | 1.746 |
| whoami/test_quota_alert_routing.py | 5 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 5 | 5 | 1.248 |
| whoami/test_quota_alert_routing_repeat.py | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 1 | 0.981 |
| whoami/test_quota_zero_episode.py | 65 | 63 | 0 | 0 | 0 | 0 | 2 | 0 | 64 | 65 | 6.691 |
| whoami/test_run_out.py | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 15 | 0.100 |
| whoami/test_session_table.py | 15 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 15 | 15 | 0.766 |
| whoami/test_usage_store.py | 25 | 25 | 0 | 0 | 0 | 0 | 0 | 0 | 25 | 25 | 0.194 |
| whoami/test_worker_usage.py | 27 | 23 | 0 | 0 | 0 | 0 | 4 | 0 | 25 | 27 | 14.272 |
| worktree_delete/test_perform_deletion_rerun.py | 2 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 2 | 2 | 0.976 |
| **Total** | **10453** | **9978** | **0** | **0** | **0** | **1** | **473** | **1** | **10146** | **10408 measured** | **2052.486 + unknown** |

## Candidate modules

Each group names its proposed keeper. Keep the first method name for the table, or use an equally clear replacement.
Each listed method supplies one labelled case. The table retains that method's complete input and assertions.
The group reason applies to each candidate row. Different expected outcomes remain separate table data.
Some helpers already have tables. Merge their callers only when the helper preserves each case's independent state.

### accounts/test_accounts.py

Owner: owner unknown. Declared methods: 468. After active actions: 449, estimate.

#### B001: 10 methods become one table

Proposed keeper: `AccountsTests.test_stored_json_counts_local_members_per_account_and_role_without_discovery`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_stored_role_counts`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AccountsTests.test_stored_json_counts_local_members_per_account_and_role_without_discovery` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_follow_retained_unit_references_after_clear` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_follow_retained_unit_references_after_clear_without_marks` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_resolve_a_unit_with_only_its_staff_identity` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_skip_damaged_launch_history_and_keep_loading` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_keep_valid_siblings_of_an_empty_launch_entry` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_skip_a_newer_launch_with_a_non_string_config_dir` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_keep_a_unit_without_legacy_launch_flags` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_treat_unknown_claude_activity_as_idle` | Table | B001: the proposed keeper retains this complete case |
| `AccountsTests.test_stored_role_counts_use_the_native_current_session_without_a_launch_record` | Table | B001: the proposed keeper retains this complete case |

#### B002: 3 methods become one table

Proposed keeper: `AccountsTests.test_piped_status_keeps_mcp_problem_in_table_and_json`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_mcp_status_streams`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AccountsTests.test_piped_status_keeps_mcp_problem_in_table_and_json` | Table | B002: the proposed keeper retains this complete case |
| `AccountsTests.test_piped_status_suppresses_nested_account_notes` | Table | B002: the proposed keeper retains this complete case |
| `AccountsTests.test_terminal_status_keeps_nested_account_notes` | Table | B002: the proposed keeper retains this complete case |

#### B003: 2 methods become one table

Proposed keeper: `AccountsTests.test_piped_status_keeps_cache_warning_when_a_later_read_fails`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `role_map.role_map_cache_file`, `cache.parent.mkdir`, `cache.write_text`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AccountsTests.test_piped_status_keeps_cache_warning_when_a_later_read_fails` | Table | B003: the proposed keeper retains this complete case |
| `AccountsTests.test_piped_status_keeps_read_diagnostics_when_rendering_fails` | Table | B003: the proposed keeper retains this complete case |

#### B004: 2 methods become one table

Proposed keeper: `PickerTests.test_picker_breaks_equal_weekly_resets_by_most_weekly_left`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_picked`, `self.reading`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PickerTests.test_picker_breaks_equal_weekly_resets_by_most_weekly_left` | Table | B004: the proposed keeper retains this complete case |
| `PickerTests.test_picker_breaks_equal_reset_and_room_by_label` | Table | B004: the proposed keeper retains this complete case |

#### B005: 2 methods become one table

Proposed keeper: `PickerTests.test_picker_codex_credits_allow_work_after_both_windows_are_exhausted`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_picked`, `self.codex`, `self.reading`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PickerTests.test_picker_codex_credits_allow_work_after_both_windows_are_exhausted` | Table | B005: the proposed keeper retains this complete case |
| `PickerTests.test_picker_codex_unlimited_credits_allow_work` | Table | B005: the proposed keeper retains this complete case |

#### B006: 2 methods become one table

Proposed keeper: `StatusProblemCauseTests.test_state_summary_stays_when_its_observed_diagnostic_is_absent`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_inventory_state_problems`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StatusProblemCauseTests.test_state_summary_stays_when_its_observed_diagnostic_is_absent` | Table | B006: the proposed keeper retains this complete case |
| `StatusProblemCauseTests.test_logout_summary_without_a_source_stays_visible` | Table | B006: the proposed keeper retains this complete case |

#### B007: 5 methods become one table

Proposed keeper: `StatusProblemCauseTests.test_malformed_identity_has_one_cause_and_keeps_private_file_fault`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_inventory_state_problems`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StatusProblemCauseTests.test_malformed_identity_has_one_cause_and_keeps_private_file_fault` | Table | B007: the proposed keeper retains this complete case |
| `StatusProblemCauseTests.test_invalid_identity_has_one_cause_and_keeps_private_file_fault` | Table | B007: the proposed keeper retains this complete case |
| `StatusProblemCauseTests.test_linked_identity_has_one_cause_and_keeps_private_file_fault` | Table | B007: the proposed keeper retains this complete case |
| `StatusProblemCauseTests.test_inaccessible_folder_has_one_cause` | Table | B007: the proposed keeper retains this complete case |
| `StatusProblemCauseTests.test_ordinary_logout_keeps_its_problem_and_private_file_fault` | Table | B007: the proposed keeper retains this complete case |

### accounts/test_launch_accounts.py

Owner: owner unknown. Declared methods: 20. After active actions: 18, estimate.

#### B008: 3 methods become one table

Proposed keeper: `LaunchAccountTests.test_preparing_an_arbitrary_supplied_label_does_not_count_as_resolving_a_launch_account`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertTrue`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `LaunchAccountTests.test_preparing_an_arbitrary_supplied_label_does_not_count_as_resolving_a_launch_account` | Table | B008: the proposed keeper retains this complete case |
| `LaunchAccountTests.test_selection_at_module_scope_is_checked_as_a_launch_decision` | Table | B008: the proposed keeper retains this complete case |
| `LaunchAccountTests.test_recorded_resume_exception_does_not_allow_selecting_a_groups_launch_account` | Table | B008: the proposed keeper retains this complete case |

### accounts/test_unit_watchdog.py

Owner: owner unknown. Declared methods: 52. After active actions: 51, estimate.

#### B009: 2 methods become one table

Proposed keeper: `UnitWatchdogTests.test_grace_keeps_the_launch_then_recovery_resumes_its_conversation`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.grace_launch_recovery`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `UnitWatchdogTests.test_grace_keeps_the_launch_then_recovery_resumes_its_conversation` | Table | B009: the proposed keeper retains this complete case |
| `UnitWatchdogTests.test_unreadable_grace_probe_keeps_recovery_until_a_later_readable_probe` | Table | B009: the proposed keeper retains this complete case |

### astra/test_astra_review.py

Owner: owner unknown. Declared methods: 22. After active actions: 20, estimate.

#### B010: 2 methods become one table

Proposed keeper: `ManagedAstraReviewTests.test_mesh_pinned_caller_keeps_mesh_for_astra_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_review_transport`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ManagedAstraReviewTests.test_mesh_pinned_caller_keeps_mesh_for_astra_review` | Table | B010: the proposed keeper retains this complete case |
| `ManagedAstraReviewTests.test_herdr_pinned_caller_keeps_herdr_for_astra_review` | Table | B010: the proposed keeper retains this complete case |

#### B011: 2 methods become one table

Proposed keeper: `ManagedAstraReviewTests.test_the_reviewer_role_exists_during_success_and_is_released_afterward`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_review_lifetime`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ManagedAstraReviewTests.test_the_reviewer_role_exists_during_success_and_is_released_afterward` | Table | B011: the proposed keeper retains this complete case |
| `ManagedAstraReviewTests.test_the_reviewer_role_exists_during_failure_and_is_released_afterward` | Table | B011: the proposed keeper retains this complete case |

### astra/test_design_roles.py

Owner: owner unknown. Declared methods: 104. After active actions: 99, estimate.

#### B012: 2 methods become one table

Proposed keeper: `HeldRolePaneTests.test_codex_held_roles_use_their_selected_models_and_sandboxes`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_held_role_family`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `HeldRolePaneTests.test_codex_held_roles_use_their_selected_models_and_sandboxes` | Table | B012: the proposed keeper retains this complete case |
| `HeldRolePaneTests.test_claude_held_roles_use_the_same_worker_admission_path` | Table | B012: the proposed keeper retains this complete case |

#### B013: 2 methods become one table

Proposed keeper: `DurableRolesTests.test_astra_role_publishes_the_returned_thread_as_its_staff_identity`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_native_role`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `DurableRolesTests.test_astra_role_publishes_the_returned_thread_as_its_staff_identity` | Table | B013: the proposed keeper retains this complete case |
| `DurableRolesTests.test_showrunner_held_astra_role_keeps_its_worker_row_and_backend` | Table | B013: the proposed keeper retains this complete case |

#### B014: 2 methods become one table

Proposed keeper: `DurableRolesTests.test_repair_retry_finishes_publication_after_the_design_save`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_interrupted_repair_finishes_publication`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `DurableRolesTests.test_repair_retry_finishes_publication_after_the_design_save` | Table | B014: the proposed keeper retains this complete case |
| `DurableRolesTests.test_repair_retry_finishes_a_failed_launch_context_publication` | Table | B014: the proposed keeper retains this complete case |

#### B015: 2 methods become one table

Proposed keeper: `DurableRolesTests.test_a_unit_transfer_moves_the_holder_to_the_receiving_production`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.open_designer`, `design_roles.design_directory`, `design_roles.worker_parent`, `staff.move`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `DurableRolesTests.test_a_unit_transfer_moves_the_holder_to_the_receiving_production` | Table | B015: the proposed keeper retains this complete case |
| `DurableRolesTests.test_unit_transfer_skips_another_units_damaged_record` | Table | B015: the proposed keeper retains this complete case |

#### B016: 2 methods become one table

Proposed keeper: `DurableRolesTests.test_unit_rename_skips_another_units_damaged_record`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.open_designer`, `design_roles.design_directory`, `design_roles.worker_parent`, `staff.rename`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `DurableRolesTests.test_unit_rename_skips_another_units_damaged_record` | Table | B016: the proposed keeper retains this complete case |
| `DurableRolesTests.test_a_production_rename_moves_the_showrunner_holder` | Table | B016: the proposed keeper retains this complete case |

### aws_target/test_box.py

Owner: aws-unit. Declared methods: 61. After active actions: 60, estimate.

#### B017: 2 methods become one table

Proposed keeper: `BoxTests.test_image_cleanup_resumes_after_deregister_failure_without_box_changes`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_image_cleanup_resumes`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `BoxTests.test_image_cleanup_resumes_after_deregister_failure_without_box_changes` | Table | B017: the proposed keeper retains this complete case |
| `BoxTests.test_image_cleanup_resumes_after_snapshot_failure_without_box_changes` | Table | B017: the proposed keeper retains this complete case |

### aws_target/test_control.py

Owner: aws-unit. Declared methods: 48. After active actions: 47, estimate.

#### B018: 2 methods become one table

Proposed keeper: `ControlTests.test_publication_timeout_cannot_delay_a_required_stop`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.publication_timeout_stops`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ControlTests.test_publication_timeout_cannot_delay_a_required_stop` | Table | B018: the proposed keeper retains this complete case |
| `ControlTests.test_publication_timeout_cannot_delay_an_unexpected_generation_stop` | Table | B018: the proposed keeper retains this complete case |

### aws_target/test_deploy.py

Owner: aws-unit. Declared methods: 127. After active actions: 125, estimate.

#### B019: 3 methods become one table

Proposed keeper: `DeploymentTests.test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_compute_failure_accounting`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `DeploymentTests.test_compute_timeout_keeps_exact_instance_costs_until_preparation_recovers` | Table | B019: the proposed keeper retains this complete case |
| `DeploymentTests.test_stack_lookup_failure_keeps_exact_instance_costs_until_apply_recovers` | Table | B019: the proposed keeper retains this complete case |
| `DeploymentTests.test_instance_lookup_failure_saves_identity_and_costs_until_setup_recovers` | Table | B019: the proposed keeper retains this complete case |

### berth/tests/test_work_order.py

Owner: owner unknown. Declared methods: 7. After active actions: 5, estimate.

#### B020: 3 methods become one table

Proposed keeper: `WorkersFieldTest.test_absent_workers_validates`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self._validate`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkersFieldTest.test_absent_workers_validates` | Table | B020: the proposed keeper retains this complete case |
| `WorkersFieldTest.test_in_flight_plan_with_the_old_field_still_validates` | Table | B020: the proposed keeper retains this complete case |
| `WorkersFieldTest.test_old_field_is_not_validated_as_a_worker_assignment` | Table | B020: the proposed keeper retains this complete case |

### bevy_migration_plan/test_bevy_migration_dependency_check.py

Owner: owner unknown. Declared methods: 36. After active actions: 35, estimate.

#### B021: 2 methods become one table

Proposed keeper: `MigrationDependencyTests.test_bevy_requirement_takes_precedence_over_subcrates`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.release`, `checker.get_bevy_dependency_requirement`, `checker.BevyRequirement`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `MigrationDependencyTests.test_bevy_requirement_takes_precedence_over_subcrates` | Table | B021: the proposed keeper retains this complete case |
| `MigrationDependencyTests.test_first_subcrate_is_selected_by_sorted_name` | Table | B021: the proposed keeper retains this complete case |

### bevy_upgrade/test_upstream.py

Owner: owner unknown; former bevy-upgrade-unit. Declared methods: 39. After active actions: 38, estimate.

#### B022: 2 methods become one table

Proposed keeper: `UpstreamTests.test_requirement_prefers_non_dev_bevy_outside_known_names`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `upstream.release_requirement`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `UpstreamTests.test_requirement_prefers_non_dev_bevy_outside_known_names` | Table | B022: the proposed keeper retains this complete case |
| `UpstreamTests.test_unknown_and_dev_only_dependencies_have_no_bevy_requirement` | Table | B022: the proposed keeper retains this complete case |

### build_hold/test_build_hold.py

Owner: resources-unit. Declared methods: 146. After active actions: 145, estimate.

#### B023: 2 methods become one table

Proposed keeper: `BuildAuditTests.test_merge_gate_stops_the_job_root_and_descendants_without_stopping_the_gate`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_gate_job_stopped`, `self.gate_job`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `BuildAuditTests.test_merge_gate_stops_the_job_root_and_descendants_without_stopping_the_gate` | Table | B023: the proposed keeper retains this complete case |
| `BuildAuditTests.test_verify_stops_the_job_root_and_descendants_without_stopping_the_gate` | Table | B023: the proposed keeper retains this complete case |

### build_queue/test_admission_service.py

Owner: queue-unit. Declared methods: 209. After active actions: 208, estimate.

#### B024: 2 methods become one table

Proposed keeper: `CutoverServiceTests.test_service_drain_other_folder_counts_admission_client`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `self.service_drain_with_process`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CutoverServiceTests.test_service_drain_other_folder_counts_admission_client` | Table | B024: the proposed keeper retains this complete case |
| `CutoverServiceTests.test_service_drain_default_folder_counts_verify_process` | Table | B024: the proposed keeper retains this complete case |

### build_queue/test_progress_profile.py

Owner: queue-unit. Declared methods: 26. After active actions: 25, estimate.

#### B025: 2 methods become one table

Proposed keeper: `ProgressCounterTests.test_output_without_a_counter_has_a_named_missing_state`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `progress.parse_progress`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ProgressCounterTests.test_output_without_a_counter_has_a_named_missing_state` | Table | B025: the proposed keeper retains this complete case |
| `ProgressCounterTests.test_finished_phase_does_not_keep_a_stale_counter` | Table | B025: the proposed keeper retains this complete case |

### build_queue/test_queue_lines.py

Owner: queue-unit. Declared methods: 19. After active actions: 18, estimate.

#### B026: 2 methods become one table

Proposed keeper: `WrappingTests.test_long_words_split_to_fit_instead_of_exceeding_the_columns`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WrappingTests.test_long_words_split_to_fit_instead_of_exceeding_the_columns` | Table | B026: the proposed keeper retains this complete case |
| `WrappingTests.test_one_cell_lines_make_progress_even_when_indent_cannot_fit` | Table | B026: the proposed keeper retains this complete case |

### build_queue/test_queue_place.py

Owner: queue-unit. Declared methods: 33. After active actions: 30, estimate.

#### B027: 2 methods become one table

Proposed keeper: `QueuePlaceTests.test_nested_scope_stops_an_unsampled_setsid_descendant_before_release`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_nested_scope_cleanup`, `unittest.skipUnless`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `QueuePlaceTests.test_nested_scope_stops_an_unsampled_setsid_descendant_before_release` | Table | B027: the proposed keeper retains this complete case |
| `QueuePlaceTests.test_scope_empties_before_release_when_a_parent_forks_during_cleanup` | Table | B027: the proposed keeper retains this complete case |

#### B028: 2 methods become one table

Proposed keeper: `QueuePlaceTests.test_descendant_of_an_exited_leader_is_stopped_before_release`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_descendant_stopped_before_release`, `unittest.skipUnless`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `QueuePlaceTests.test_descendant_of_an_exited_leader_is_stopped_before_release` | Table | B028: the proposed keeper retains this complete case |
| `QueuePlaceTests.test_descendant_of_a_term_stopped_leader_is_stopped_before_release` | Table | B028: the proposed keeper retains this complete case |

#### B029: 2 methods become one table

Proposed keeper: `QueuePlaceTests.test_scope_is_published_at_popen_return_and_survives_holder_death`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_scope_survives_holder_death`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `QueuePlaceTests.test_scope_is_published_at_popen_return_and_survives_holder_death` | Table | B029: the proposed keeper retains this complete case |
| `QueuePlaceTests.test_scope_survives_holder_death_after_its_leader_exits_before_popen_returns` | Table | B029: the proposed keeper retains this complete case |

### build_targets/test_build_targets.py

Owner: resources-unit. Declared methods: 78. After active actions: 74, estimate.

#### B030: 2 methods become one table

Proposed keeper: `StatusTests.test_status_fields_remove_control_characters`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `build_targets.format_state`, `targets.Free`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StatusTests.test_status_fields_remove_control_characters` | Table | B030: the proposed keeper retains this complete case |
| `StatusTests.test_free_line` | Table | B030: the proposed keeper retains this complete case |

#### B031: 3 methods become one table

Proposed keeper: `StatusTests.test_hold_without_end_line`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `build_targets.format_state`, `targets.Held`, `targets.UntilReleased`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StatusTests.test_hold_without_end_line` | Table | B031: the proposed keeper retains this complete case |
| `StatusTests.test_hold_reason_whitespace_collapses_to_one_line` | Table | B031: the proposed keeper retains this complete case |
| `StatusTests.test_holder_whitespace_collapses_to_one_line` | Table | B031: the proposed keeper retains this complete case |

#### B032: 2 methods become one table

Proposed keeper: `StatusTests.test_unreachable_line`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `build_targets.format_state`, `targets.Unreachable`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StatusTests.test_unreachable_line` | Table | B032: the proposed keeper retains this complete case |
| `StatusTests.test_unreachable_reason_whitespace_collapses_to_one_line` | Table | B032: the proposed keeper retains this complete case |

### build_targets/test_targets.py

Owner: resources-unit. Declared methods: 50. After active actions: 46, estimate.

#### B033: 4 methods become one table

Proposed keeper: `ConfigurationTests.test_unknown_kind_names_its_key`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertRaisesRegex`, `self.write_config`, `targets.read_targets`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ConfigurationTests.test_unknown_kind_names_its_key` | Table | B033: the proposed keeper retains this complete case |
| `ConfigurationTests.test_missing_platform_names_its_key` | Table | B033: the proposed keeper retains this complete case |
| `ConfigurationTests.test_missing_settings_key_names_its_key` | Table | B033: the proposed keeper retains this complete case |
| `ConfigurationTests.test_missing_settings_file_names_its_key` | Table | B033: the proposed keeper retains this complete case |

#### B034: 2 methods become one table

Proposed keeper: `TargetStateTests.test_invalid_json_block_is_a_state_read_failure`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `(self.mac_state / 'block.json').write_text`, `targets.target_state`, `self.mac`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TargetStateTests.test_invalid_json_block_is_a_state_read_failure` | Table | B034: the proposed keeper retains this complete case |
| `TargetStateTests.test_invalid_json_claim_is_a_state_read_failure` | Table | B034: the proposed keeper retains this complete case |

### buildlog/test_charts.py

Owner: report-unit. Declared methods: 54. After active actions: 52, estimate.

#### B035: 2 methods become one table

Proposed keeper: `ChartTests.test_rendered_moment_numbers_have_a_band_clear_of_every_panel_title`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.label_page`, `self.subTest`, `self.rendered_layout`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ChartTests.test_rendered_moment_numbers_have_a_band_clear_of_every_panel_title` | Table | B035: the proposed keeper retains this complete case |
| `ChartTests.test_rendered_end_labels_stay_nearest_their_series_and_group_equal_endpoints` | Table | B035: the proposed keeper retains this complete case |

#### B036: 2 methods become one table

Proposed keeper: `ChartTests.test_machine_without_a_log_has_one_message_and_a_named_selector_option`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.configured_machine_series`, `fixtures.build`, `self.rendered_layout`, `charts.render_html`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ChartTests.test_machine_without_a_log_has_one_message_and_a_named_selector_option` | Table | B036: the proposed keeper retains this complete case |
| `ChartTests.test_no_data_cells_agree_in_the_table_tooltip_and_detail` | Table | B036: the proposed keeper retains this complete case |

### buildlog/test_mac_idle.py

Owner: verify-unit. Declared methods: 63. After active actions: 62, estimate.

#### B037: 2 methods become one table

Proposed keeper: `MacIdleTests.test_notification_marks_survive_stop_after_delivery_before_return`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_interrupted_delivery`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `MacIdleTests.test_notification_marks_survive_stop_after_delivery_before_return` | Table | B037: the proposed keeper retains this complete case |
| `MacIdleTests.test_stop_before_transport_retries_and_delivers_once` | Table | B037: the proposed keeper retains this complete case |

### buildlog/test_mac_report.py

Owner: report-unit. Declared methods: 41. After active actions: 40, estimate.

#### B038: 2 methods become one table

Proposed keeper: `MacReportTests.test_sent_key_on_recorded_mac_step_counts_a_natedev_pass_of_the_same_selection`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assertNotIn`, `self.write`, `self.record_sent_mac_step`, `self.report`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `MacReportTests.test_sent_key_on_recorded_mac_step_counts_a_natedev_pass_of_the_same_selection` | Table | B038: the proposed keeper retains this complete case |
| `MacReportTests.test_sent_key_on_recorded_mac_step_does_not_count_a_different_natedev_command` | Table | B038: the proposed keeper retains this complete case |

### buildlog/test_record.py

Owner: report-unit. Declared methods: 86. After active actions: 85, estimate.

#### B039: 2 methods become one table

Proposed keeper: `RecordTests.test_local_step_with_changed_tree_does_not_use_the_sent_tree_key`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertNotEqual`, `assertEqual`, `assert_unverified_mac_step_uses_sent_key`, `self.key`, `self.step`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RecordTests.test_local_step_with_changed_tree_does_not_use_the_sent_tree_key` | Table | B039: the proposed keeper retains this complete case |
| `RecordTests.test_verify_mac_step_with_changed_tree_does_not_use_the_sent_tree_key` | Table | B039: the proposed keeper retains this complete case |

### buildlog/test_record_rerun.py

Owner: report-unit. Declared methods: 30. After active actions: 27, estimate.

#### B040: 4 methods become one table

Proposed keeper: `RecordRerunTests.test_fragment_then_normal_step_and_call_recovers_one_physical_row_per_id`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_short_append_recovers`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RecordRerunTests.test_fragment_then_normal_step_and_call_recovers_one_physical_row_per_id` | Table | B040: the proposed keeper retains this complete case |
| `RecordRerunTests.test_complete_object_without_newline_then_other_writers_recovers_without_duplicate` | Table | B040: the proposed keeper retains this complete case |
| `RecordRerunTests.test_stop_after_fragment_tail_cut_recovers_without_losing_later_rows` | Table | B040: the proposed keeper retains this complete case |
| `RecordRerunTests.test_stop_after_complete_object_tail_cut_recovers_without_duplicate` | Table | B040: the proposed keeper retains this complete case |

### buildlog/test_recorder_health.py

Owner: report-unit. Declared methods: 12. After active actions: 11, estimate.

#### B041: 2 methods become one table

Proposed keeper: `RecorderHealthTests.test_mac_publish_failure_reaches_natedev_without_a_mac_report_and_clears_on_recovery`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_peer_failure_recovers`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RecorderHealthTests.test_mac_publish_failure_reaches_natedev_without_a_mac_report_and_clears_on_recovery` | Table | B041: the proposed keeper retains this complete case |
| `RecorderHealthTests.test_published_row_with_failed_cleanup_still_counts_as_unfinished` | Table | B041: the proposed keeper retains this complete case |

### buildlog/test_report.py

Owner: report-unit. Declared methods: 171. After active actions: 170, estimate.

#### B042: 2 methods become one table

Proposed keeper: `ReportTests.test_build_folder_wait_fully_behind_own_call_from_previous_day`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.write`, `self.waiting_rows`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ReportTests.test_build_folder_wait_fully_behind_own_call_from_previous_day` | Table | B042: the proposed keeper retains this complete case |
| `ReportTests.test_build_folder_wait_fully_behind_another_worker` | Table | B042: the proposed keeper retains this complete case |

### buildlog/test_step_row.py

Owner: report-unit. Declared methods: 49. After active actions: 42, estimate.
Keep separate interpreters for the cold-import rows. Combining method names cannot remove those interpreter starts.

#### B043: 3 methods become one table

Proposed keeper: `RecorderCompatibilityTests.test_scp_remote_identity_does_not_load_url_parsing`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `subprocess.run`, `os.environ.items`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RecorderCompatibilityTests.test_scp_remote_identity_does_not_load_url_parsing` | Table | B043: the proposed keeper retains this complete case |
| `RecorderCompatibilityTests.test_row_ids_do_not_load_random_or_temporary_file_modules` | Table | B043: the proposed keeper retains this complete case |
| `RecorderCompatibilityTests.test_empty_metadata_does_not_load_json_or_store` | Table | B043: the proposed keeper retains this complete case |

#### B044: 2 methods become one table

Proposed keeper: `RecorderCompatibilityTests.test_terminal_row_copies_its_completed_admission_charge`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_completed_charge`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RecorderCompatibilityTests.test_terminal_row_copies_its_completed_admission_charge` | Table | B044: the proposed keeper retains this complete case |
| `RecorderCompatibilityTests.test_tee_row_copies_its_completed_admission_charge` | Table | B044: the proposed keeper retains this complete case |

#### B045: 2 methods become one table

Proposed keeper: `StepOutputFolderTests.test_env_assignment_selects_target_output`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_output_pair`, `self.write_step`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StepOutputFolderTests.test_env_assignment_selects_target_output` | Table | B045: the proposed keeper retains this complete case |
| `StepOutputFolderTests.test_first_target_argument_selects_start_and_end_output` | Table | B045: the proposed keeper retains this complete case |

#### B046: 2 methods become one table

Proposed keeper: `VerifyOutputEvidenceTests.test_target_environment_wrapper_reads_its_own_output_folders`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_recorded_roots`, `self.capture_export`, `self.set_metadata_roots`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyOutputEvidenceTests.test_target_environment_wrapper_reads_its_own_output_folders` | Table | B046: the proposed keeper retains this complete case |
| `VerifyOutputEvidenceTests.test_inline_config_reads_its_selected_output_folders` | Table | B046: the proposed keeper retains this complete case |

#### B047: 2 methods become one table

Proposed keeper: `VerifyOutputEvidenceTests.test_changed_config_reads_the_new_output_folders`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_recorded_roots`, `self.capture_export`, `config.parent.mkdir`, `config.write_text`, `self.set_metadata_roots`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyOutputEvidenceTests.test_changed_config_reads_the_new_output_folders` | Table | B047: the proposed keeper retains this complete case |
| `VerifyOutputEvidenceTests.test_legacy_config_change_reads_the_new_output_folders` | Table | B047: the proposed keeper retains this complete case |

#### B048: 2 methods become one table

Proposed keeper: `VerifyOutputEvidenceTests.test_changed_manifest_rejects_the_prior_metadata_evidence`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_recorded_roots`, `self.capture_export`, `(self.workspace / 'Cargo.toml').write_text`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyOutputEvidenceTests.test_changed_manifest_rejects_the_prior_metadata_evidence` | Table | B048: the proposed keeper retains this complete case |
| `VerifyOutputEvidenceTests.test_toolchain_file_change_rejects_prior_metadata_evidence` | Table | B048: the proposed keeper retains this complete case |

### buildlog/test_store_identity.py

Owner: report-unit. Declared methods: 6. After active actions: 3, estimate.
Keep separate interpreters for import-order rows. Preserve the import-order boundary.

#### B049: 4 methods become one table

Proposed keeper: `StoreIdentityTests.test_bare_import_then_package_import_share_one_store`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_imports`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StoreIdentityTests.test_bare_import_then_package_import_share_one_store` | Table | B049: the proposed keeper retains this complete case |
| `StoreIdentityTests.test_bare_import_then_dotted_import_sets_the_package_store_attribute` | Table | B049: the proposed keeper retains this complete case |
| `StoreIdentityTests.test_package_import_then_bare_import_share_one_store` | Table | B049: the proposed keeper retains this complete case |
| `StoreIdentityTests.test_sweep_import_uses_one_store_across_its_callers` | Table | B049: the proposed keeper retains this complete case |

### buildlog/test_sync.py

Owner: report-unit. Declared methods: 19. After active actions: 17, estimate.

#### B050: 3 methods become one table

Proposed keeper: `SyncTests.test_mkdir_printed_failure_overrides_successful_ssh_exit`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIs`, `subprocess.CompletedProcess`, `sync.sync`, `self.status`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `SyncTests.test_mkdir_printed_failure_overrides_successful_ssh_exit` | Table | B050: the proposed keeper retains this complete case |
| `SyncTests.test_mkdir_without_printed_status_fails` | Table | B050: the proposed keeper retains this complete case |
| `SyncTests.test_ssh_255_is_peer_unavailable` | Table | B050: the proposed keeper retains this complete case |

### buildlog/test_target_report.py

Owner: report-unit. Declared methods: 9. After active actions: 7, estimate.

#### B051: 3 methods become one table

Proposed keeper: `TargetReportTests.test_target_identity_selects_mac_on_a_new_host`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `self.write`, `self.mac_section`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TargetReportTests.test_target_identity_selects_mac_on_a_new_host` | Table | B051: the proposed keeper retains this complete case |
| `TargetReportTests.test_explicit_aws_target_overrides_a_legacy_mac_host` | Table | B051: the proposed keeper retains this complete case |
| `TargetReportTests.test_empty_target_uses_the_legacy_mac_host_alias` | Table | B051: the proposed keeper retains this complete case |

### buildlog/test_test_phase_peak.py

Owner: report-unit. Declared methods: 14. After active actions: 13, estimate.

#### B052: 2 methods become one table

Proposed keeper: `TestPhasePeakTests.test_native_test_in_a_custom_target_directory_opens_the_window`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_test_directory_opens_the_window`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TestPhasePeakTests.test_native_test_in_a_custom_target_directory_opens_the_window` | Table | B052: the proposed keeper retains this complete case |
| `TestPhasePeakTests.test_cross_target_test_in_a_custom_target_directory_opens_the_window` | Table | B052: the proposed keeper retains this complete case |

### copies/test_copy_guard.py

Owner: verify-unit. Declared methods: 51. After active actions: 36, estimate.
Keep each Python binding scenario. Shared scanner assertions do not make different binding rules redundant.

#### B053: 11 methods become one table

Proposed keeper: `CopyGuardTests.test_scan_resolves_global_binding_after_later_module_import`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `copy_guard.PythonPolicy(self.root / 'maker.py').scan`, `ast.parse`, `copy_guard.PythonPolicy`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CopyGuardTests.test_scan_resolves_global_binding_after_later_module_import` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_resolves_nonlocal_binding_after_later_enclosing_import` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_global_clone_called_before_module_rebinding` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_nonlocal_clone_called_before_enclosing_rebinding` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_uses_each_module_call_binding_without_duplicate_findings` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_global_clone_at_module_call_in_branch` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_uses_final_global_binding_for_outside_callers` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_indirect_global_clone_before_module_rebinding` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_indirect_nonlocal_clone_before_enclosing_rebinding` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_finds_global_clone_through_two_level_call_chain` | Table | B053: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_ends_nested_recursion_and_reports_indirect_nonlocal_clone` | Table | B053: the proposed keeper retains this complete case |

#### B054: 6 methods become one table

Proposed keeper: `CopyGuardTests.test_scan_keeps_local_shadowing_clean_after_later_imports`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `copy_guard.PythonPolicy(self.root / 'maker.py').scan`, `ast.parse`, `copy_guard.PythonPolicy`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CopyGuardTests.test_scan_keeps_local_shadowing_clean_after_later_imports` | Table | B054: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_keeps_global_rebinding_before_definition_clean` | Table | B054: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_keeps_global_rebinding_before_only_call_clean` | Table | B054: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_keeps_nonlocal_rebinding_before_only_call_clean` | Table | B054: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_permits_imported_git_reads_before_module_rebinding` | Table | B054: the proposed keeper retains this complete case |
| `CopyGuardTests.test_scan_keeps_indirect_nonlocal_rebinding_before_call_clean` | Table | B054: the proposed keeper retains this complete case |

### copies/test_review_remote_copies.py

Owner: owner unknown. Declared methods: 29. After active actions: 29, estimate.

#### Documentation wording

Candidate: `RemoteEntryTests.test_doc_description_names_the_standing_checkout_and_lease`. Class: Wording.
Reason: it checks four documentation terms. It does not execute the documented commands.
It requires `copies.py hold` and `StagedSource`. It excludes two old command or lock-path strings.
The user decides whether this documentation wording belongs in the gate. No runtime keeper is required for those literal checks.
Keep this method until that decision. Its removal is absent from the after count.
Keep `test_doc_checkout_uses_only_the_copy_owner_reply`. That source check targets a specific unsafe checkout path.
CPU saved: 0 s, planning estimate. This wording decision does not explain the expensive recovery cases.

### feature_log/test_feature_log.py

Owner: owner unknown; former feature-log-unit. Declared methods: 129. After active actions: 128, estimate.

#### B055: 2 methods become one table

Proposed keeper: `FeatureLogTests.test_release_rejects_unsorted_titles_without_stamps_or_pending_cleanup`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `assert_no_temporary_files`, `self.add_crate_note`, `self.supersede_crate_note`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FeatureLogTests.test_release_rejects_unsorted_titles_without_stamps_or_pending_cleanup` | Table | B055: the proposed keeper retains this complete case |
| `FeatureLogTests.test_release_rejects_missing_reviewed_note_without_stamps_or_pending_cleanup` | Table | B055: the proposed keeper retains this complete case |

### hana_shot/test_hana_shot.py

Owner: screenshot-unit. Declared methods: 115. After active actions: 113, estimate.

#### B056: 3 methods become one table

Proposed keeper: `RemoteTests.test_remote_mac_shot_restores_an_always_on_bottom_window`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `self.fake_remote`, `self.shot`, `self.ssh_calls`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RemoteTests.test_remote_mac_shot_restores_an_always_on_bottom_window` | Table | B056: the proposed keeper retains this complete case |
| `RemoteTests.test_remote_mac_shot_repairs_an_already_always_on_top_window_to_normal` | Table | B056: the proposed keeper retains this complete case |
| `RemoteTests.test_failed_remote_mac_shot_restores_the_window_and_ends_keep_awake` | Table | B056: the proposed keeper retains this complete case |

### herdr/test_herdr.py

Owner: owner unknown; former herdr-trial-unit. Declared methods: 363. After active actions: 355, estimate.

#### B057: 4 methods become one table

Proposed keeper: `NativePaneSessionTests.test_target_proof_survives_a_descriptor_closing_during_listing`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_target_survives_closed_descriptor`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `NativePaneSessionTests.test_target_proof_survives_a_descriptor_closing_during_listing` | Table | B057: the proposed keeper retains this complete case |
| `NativePaneSessionTests.test_target_proof_survives_a_descriptor_closing_during_read` | Table | B057: the proposed keeper retains this complete case |
| `NativePaneSessionTests.test_target_proof_survives_a_descriptor_closing_before_the_final_listing` | Table | B057: the proposed keeper retains this complete case |
| `NativePaneSessionTests.test_target_proof_refuses_a_new_descriptor_before_the_final_listing` | Table | B057: the proposed keeper retains this complete case |

#### B058: 3 methods become one table

Proposed keeper: `CodexAnomalyNameTests.test_unknown_codex_note_uses_the_tab_label_before_the_registry_name`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_unknown_codex_note`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CodexAnomalyNameTests.test_unknown_codex_note_uses_the_tab_label_before_the_registry_name` | Table | B058: the proposed keeper retains this complete case |
| `CodexAnomalyNameTests.test_unknown_codex_note_uses_the_registry_name_when_placement_is_unavailable` | Table | B058: the proposed keeper retains this complete case |
| `CodexAnomalyNameTests.test_unknown_codex_note_uses_the_pid_when_both_name_readers_fail` | Table | B058: the proposed keeper retains this complete case |

#### B059: 2 methods become one table

Proposed keeper: `PromptSubmissionTests.test_ready_session_still_requires_a_visible_empty_composer`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `assertGreaterEqual`, `self.events.append`, `self.submit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PromptSubmissionTests.test_ready_session_still_requires_a_visible_empty_composer` | Table | B059: the proposed keeper retains this complete case |
| `PromptSubmissionTests.test_idle_pane_waits_for_a_visible_empty_composer` | Table | B059: the proposed keeper retains this complete case |

#### B060: 2 methods become one table

Proposed keeper: `PromptSubmissionTests.test_an_old_bordered_prompt_above_a_new_unbordered_input_is_scrollback`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `assertEqual`, `self.submit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PromptSubmissionTests.test_an_old_bordered_prompt_above_a_new_unbordered_input_is_scrollback` | Table | B060: the proposed keeper retains this complete case |
| `PromptSubmissionTests.test_old_bordered_prompt_above_assistant_output_without_a_new_marker_gets_no_enter` | Table | B060: the proposed keeper retains this complete case |

#### B061: 2 methods become one table

Proposed keeper: `PromptSubmissionTests.test_named_composer_with_status_mode_and_agent_footer_gets_one_enter_retry`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_real_composer_retries_once`, `self.real_composer_screen`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PromptSubmissionTests.test_named_composer_with_status_mode_and_agent_footer_gets_one_enter_retry` | Table | B061: the proposed keeper retains this complete case |
| `PromptSubmissionTests.test_named_composer_with_rule_character_in_label_gets_one_enter_retry` | Table | B061: the proposed keeper retains this complete case |

### mac_test/test_copy_watch.py

Owner: verify-unit. Declared methods: 27. After active actions: 26, estimate.

#### B062: 2 methods become one table

Proposed keeper: `CopyWatchProcessTests.test_parent_killed_ends_the_watcher_at_once`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_parent_loss_stops_watcher`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CopyWatchProcessTests.test_parent_killed_ends_the_watcher_at_once` | Table | B062: the proposed keeper retains this complete case |
| `CopyWatchProcessTests.test_parent_killed_stops_a_running_copy` | Table | B062: the proposed keeper retains this complete case |

### mac_test/test_linux_runner.py

Owner: verify-unit. Declared methods: 93. After active actions: 91, estimate.

#### B063: 3 methods become one table

Proposed keeper: `LinuxProcessTests.test_unresolved_run_startup_keeps_the_claim_without_work_or_backoff`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_unresolved_startup_stays_busy`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `LinuxProcessTests.test_unresolved_run_startup_keeps_the_claim_without_work_or_backoff` | Table | B063: the proposed keeper retains this complete case |
| `LinuxProcessTests.test_unresolved_probe_startup_keeps_the_claim_without_work_or_backoff` | Table | B063: the proposed keeper retains this complete case |
| `LinuxProcessTests.test_successful_probe_with_unresolved_cleanup_blocks_run_startup` | Table | B063: the proposed keeper retains this complete case |

### mac_test/test_offload.py

Owner: verify-unit. Declared methods: 70. After active actions: 68, estimate.

#### B064: 2 methods become one table

Proposed keeper: `LintVerbTests.test_lint_symlink_in_another_folder_keeps_workspace_scope`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_scope_start`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `LintVerbTests.test_lint_symlink_in_another_folder_keeps_workspace_scope` | Table | B064: the proposed keeper retains this complete case |
| `LintVerbTests.test_lint_filename_in_its_folder_keeps_workspace_scope` | Table | B064: the proposed keeper retains this complete case |

#### B065: 2 methods become one table

Proposed keeper: `LocalVersionPreflightTests.test_matching_versions_continue_to_claim`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_record`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `LocalVersionPreflightTests.test_matching_versions_continue_to_claim` | Table | B065: the proposed keeper retains this complete case |
| `LocalVersionPreflightTests.test_only_recorded_keys_are_compared` | Table | B065: the proposed keeper retains this complete case |

### mac_test/test_offload_forward.py

Owner: verify-unit. Declared methods: 18. After active actions: 17, estimate.
Reset event logs, result paths, and call identifiers between the two real CLI rows.

#### B066: 2 methods become one table

Proposed keeper: `OffloadForwardTests.test_named_environment_reaches_the_remote_test_command`. Class: Table.
Reason: both CLI cases check environment forwarding. The `--copy` argument selects the second route.
Keep the return code and the remote command assertion for both routes.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `OffloadForwardTests.test_named_environment_reaches_the_remote_test_command` | Table | B066: the proposed keeper retains this complete case |
| `OffloadForwardTests.test_named_environment_reaches_the_remote_copy_command` | Table | B066: the proposed keeper retains this complete case |

### mac_test/test_offload_lifecycle.py

Owner: verify-unit. Declared methods: 21. After active actions: 17, estimate.

#### B067: 3 methods become one table

Proposed keeper: `OffloadCommandTests.test_signal_during_fresh_claim_releases_without_ssh_when_check_would_time_out`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_fresh_claim_signal_releases_without_ssh`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `OffloadCommandTests.test_signal_during_fresh_claim_releases_without_ssh_when_check_would_time_out` | Table | B067: the proposed keeper retains this complete case |
| `OffloadCommandTests.test_signal_during_fresh_claim_releases_without_ssh_when_check_would_fail` | Table | B067: the proposed keeper retains this complete case |
| `OffloadCommandTests.test_signal_during_fresh_claim_releases_without_ssh_when_check_would_omit_status` | Table | B067: the proposed keeper retains this complete case |

#### B068: 3 methods become one table

Proposed keeper: `OffloadCommandTests.test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_check_fails`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_takeover_signal_checks_reply_host`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `OffloadCommandTests.test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_check_fails` | Table | B068: the proposed keeper retains this complete case |
| `OffloadCommandTests.test_signal_during_takeover_checks_reply_host_and_keeps_claim_when_leftovers_remain` | Table | B068: the proposed keeper retains this complete case |
| `OffloadCommandTests.test_signal_during_takeover_checks_reply_host_and_releases_claim_when_check_is_clean` | Table | B068: the proposed keeper retains this complete case |

### notify/test_push_texts.py

Owner: messaging-unit. Declared methods: 50. After active actions: 49, estimate.
Keep the action metadata and the user-approved text together. These cases check more than wording.

#### B069: 2 methods become one table

Proposed keeper: `PushTextTests.test_paid_credits_keeps_the_user_approved_lines`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `push_texts.render_entry`, `push_texts.catalog`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PushTextTests.test_paid_credits_keeps_the_user_approved_lines` | Table | B069: the proposed keeper retains this complete case |
| `PushTextTests.test_needs_go_keeps_the_user_approved_lines` | Table | B069: the proposed keeper retains this complete case |

### notify/test_pushover.py

Owner: messaging-unit. Declared methods: 82. After active actions: 80, estimate.

#### B070: 2 methods become one table

Proposed keeper: `PushoverTests.test_array_reply_logs_http_refusal`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assert_log`, `self.run_send`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PushoverTests.test_array_reply_logs_http_refusal` | Table | B070: the proposed keeper retains this complete case |
| `PushoverTests.test_null_reply_logs_http_refusal` | Table | B070: the proposed keeper retains this complete case |

#### B071: 2 methods become one table

Proposed keeper: `PushoverTests.test_action_free_emergency_is_refused`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assert_log`, `self.run_send`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PushoverTests.test_action_free_emergency_is_refused` | Table | B071: the proposed keeper retains this complete case |
| `PushoverTests.test_normal_priority_decision_still_requires_an_action` | Table | B071: the proposed keeper retains this complete case |

### screen_record/test_screen_record.py

Owner: owner unknown; former stalls-unit. Declared methods: 29. After active actions: 28, estimate.

#### B072: 2 methods become one table

Proposed keeper: `ScreenRecordTests.test_missing_client_list_records_the_matching_tree_window`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_tree_window_records_with_required_limits`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ScreenRecordTests.test_missing_client_list_records_the_matching_tree_window` | Table | B072: the proposed keeper retains this complete case |
| `ScreenRecordTests.test_empty_client_list_records_the_matching_tree_window` | Table | B072: the proposed keeper retains this complete case |

### shot_report/test_transcripts.py

Owner: screenshot-unit. Declared methods: 94. After active actions: 75, estimate.

#### B073: 4 methods become one table

Proposed keeper: `TranscriptTest.test_cat_heredoc_then_later_direct_run_counts`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self._script_calls`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_cat_heredoc_then_later_direct_run_counts` | Table | B073: the proposed keeper retains this complete case |
| `TranscriptTest.test_script_sourcing_remembered_screenshot_script_counts` | Table | B073: the proposed keeper retains this complete case |
| `TranscriptTest.test_cat_append_adds_screenshot_call_to_later_run` | Table | B073: the proposed keeper retains this complete case |
| `TranscriptTest.test_timeout_wrapper_runs_remembered_script` | Table | B073: the proposed keeper retains this complete case |

#### B074: 4 methods become one table

Proposed keeper: `TranscriptTest.test_written_script_without_run_does_not_count`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self._script_calls`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_written_script_without_run_does_not_count` | Table | B074: the proposed keeper retains this complete case |
| `TranscriptTest.test_edit_removing_screenshot_call_prevents_later_run` | Table | B074: the proposed keeper retains this complete case |
| `TranscriptTest.test_write_replacement_removes_screenshot_call` | Table | B074: the proposed keeper retains this complete case |
| `TranscriptTest.test_patch_deletion_removes_screenshot_call` | Table | B074: the proposed keeper retains this complete case |

#### B075: 4 methods become one table

Proposed keeper: `TranscriptTest.test_loop_with_three_screenshot_calls_counts_three_images`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self._script_calls`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_loop_with_three_screenshot_calls_counts_three_images` | Table | B075: the proposed keeper retains this complete case |
| `TranscriptTest.test_script_result_with_two_distinct_png_paths_counts_two_images` | Table | B075: the proposed keeper retains this complete case |
| `TranscriptTest.test_script_with_two_screenshot_calls_and_no_paths_counts_two_images` | Table | B075: the proposed keeper retains this complete case |
| `TranscriptTest.test_second_pending_id_on_multi_result_line_is_read` | Table | B075: the proposed keeper retains this complete case |

#### B076: 9 methods become one table

Proposed keeper: `TranscriptTest.test_positional_method_arguments_count`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self._assert_shell_cases`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_positional_method_arguments_count` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_python_call_literal_counts` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_interpreter_heredoc_and_written_script_run_count` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_compound_shell_commands_count` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_other_positional_and_python_methods_are_related_calls` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_variable_program_can_pass_positional_method` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_compound_heredoc_and_written_script_run_count` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_assigned_request_sent_by_curl_counts` | Table | B076: the proposed keeper retains this complete case |
| `TranscriptTest.test_interpreter_options_import_and_parameter_expansion_count` | Table | B076: the proposed keeper retains this complete case |

#### B077: 2 methods become one table

Proposed keeper: `TranscriptTest.test_wrapped_and_nested_commands_count`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsNotNone`, `assertEqual`, `self.subTest`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_wrapped_and_nested_commands_count` | Table | B077: the proposed keeper retains this complete case |
| `TranscriptTest.test_later_screenshot_takes_priority_over_related_call` | Table | B077: the proposed keeper retains this complete case |

#### B078: 2 methods become one table

Proposed keeper: `TranscriptTest.test_piped_hana_shot_counts_with_distinct_result_images`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `tempfile.TemporaryDirectory`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `TranscriptTest.test_piped_hana_shot_counts_with_distinct_result_images` | Table | B078: the proposed keeper retains this complete case |
| `TranscriptTest.test_piped_curl_shot_counts_one_image` | Table | B078: the proposed keeper retains this complete case |

### agents/test_pane_record.py

Owner: shutdown-unit. Declared methods: 32. After active actions: 31, estimate.

#### B079: 2 methods become one table

Proposed keeper: `PaneRecordTests.test_repair_recovers_history_and_staff_identity_without_changing_conversations`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_repair_recovers_history`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `PaneRecordTests.test_repair_recovers_history_and_staff_identity_without_changing_conversations` | Table | B079: the proposed keeper retains this complete case |
| `PaneRecordTests.test_repair_recovers_identity_from_a_three_field_launch_without_name_or_cwd` | Table | B079: the proposed keeper retains this complete case |

### agents/test_stop_resume.py

Owner: shutdown-unit. Declared methods: 163. After active actions: 162, estimate.

#### B080: 2 methods become one table

Proposed keeper: `StopResumeTests.test_proof_workspace_unset_refuses_staff_file_signals`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `assertIn`, `self.signal_guard.stop`, `sessions.LiveSessionProcess`, `self.kill.assert_not_called`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StopResumeTests.test_proof_workspace_unset_refuses_staff_file_signals` | Table | B080: the proposed keeper retains this complete case |
| `StopResumeTests.test_proof_workspace_unset_refuses_shutdown_state_signals` | Table | B080: the proposed keeper retains this complete case |

### statusline/test_statusline.py

Owner: owner unknown. Declared methods: 75. After active actions: 74, estimate.

#### B081: 2 methods become one table

Proposed keeper: `RebuildRowTests.test_both_machines_need_a_rebuild`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.answer`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RebuildRowTests.test_both_machines_need_a_rebuild` | Table | B081: the proposed keeper retains this complete case |
| `RebuildRowTests.test_unreachable_mac_does_not_hide_the_local_item` | Table | B081: the proposed keeper retains this complete case |

### style/test_approved_renames.py

Owner: owner unknown. Declared methods: 89. After active actions: 86, estimate.

#### B082: 2 methods become one table

Proposed keeper: `AppliedCheckTests.test_unparsed_enclosing_condition_is_unproved_instead_of_clear`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `assertEqual`, `approved_renames.rename_admission`, `source.splitlines`, `approved_renames.binding_admission_check`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AppliedCheckTests.test_unparsed_enclosing_condition_is_unproved_instead_of_clear` | Table | B082: the proposed keeper retains this complete case |
| `AppliedCheckTests.test_preceding_enclosing_macro_cannot_hide_a_binding` | Table | B082: the proposed keeper retains this complete case |

#### B083: 2 methods become one table

Proposed keeper: `AppliedCheckTests.test_an_enum_variant_rename_is_applied_only_when_the_old_variant_is_gone`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AppliedCheckTests.test_an_enum_variant_rename_is_applied_only_when_the_old_variant_is_gone` | Table | B083: the proposed keeper retains this complete case |
| `AppliedCheckTests.test_a_top_level_item_of_a_crate_root_is_under_crate` | Table | B083: the proposed keeper retains this complete case |

#### B084: 2 methods become one table

Proposed keeper: `BindingRenameTests.test_a_raw_use_of_the_old_name_cannot_be_missed`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertFalse`, `self.prove`, `source.replace`, `proof.checks.values`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `BindingRenameTests.test_a_raw_use_of_the_old_name_cannot_be_missed` | Table | B084: the proposed keeper retains this complete case |
| `BindingRenameTests.test_encoded_format_braces_cannot_hide_a_missed_capture` | Table | B084: the proposed keeper retains this complete case |

### style/test_bulk_constants.py

Owner: owner unknown. Declared methods: 44. After active actions: 43, estimate.

#### B085: 2 methods become one table

Proposed keeper: `ConstantTests.test_refuses_macro_arguments`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.refused`, `self.finding`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ConstantTests.test_refuses_macro_arguments` | Table | B085: the proposed keeper retains this complete case |
| `ConstantTests.test_tuple_field_indices_are_not_literal_expressions` | Table | B085: the proposed keeper retains this complete case |

### style/test_bulk_rename.py

Owner: owner unknown. Declared methods: 118. After active actions: 116, estimate.
Keep a fresh Rust analyzer process where the current case requires it.

#### B086: 2 methods become one table

Proposed keeper: `RenameTests.test_same_line_outer_bindings_refuse_before_analyzer_rename`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_outer_block_before_analyzer`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RenameTests.test_same_line_outer_bindings_refuse_before_analyzer_rename` | Table | B086: the proposed keeper retains this complete case |
| `RenameTests.test_raw_outer_binding_refuses_before_analyzer_rename` | Table | B086: the proposed keeper retains this complete case |

#### B087: 2 methods become one table

Proposed keeper: `RustAnalyzerTests.test_real_f001_duplicate_function_parameter_refuses_before_rename`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `(self.repo / 'src/lib.rs').write_text`, `bulk.RustAnalyzer`, `bulk.rename_file`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RustAnalyzerTests.test_real_f001_duplicate_function_parameter_refuses_before_rename` | Table | B087: the proposed keeper retains this complete case |
| `RustAnalyzerTests.test_real_f001_duplicate_closure_parameter_refuses_before_rename` | Table | B087: the proposed keeper retains this complete case |

### style/test_bulk_run.py

Owner: owner unknown. Declared methods: 108. After active actions: 102, estimate.

#### B088: 2 methods become one table

Proposed keeper: `WorkspaceRunTests.test_workspace_applies_ready_findings_and_releases_a_blocked_writers_claim`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_refused_writer_releases_its_finding`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkspaceRunTests.test_workspace_applies_ready_findings_and_releases_a_blocked_writers_claim` | Table | B088: the proposed keeper retains this complete case |
| `WorkspaceRunTests.test_workspace_checkpoints_a_shared_ticket_after_releasing_its_blocked_member` | Table | B088: the proposed keeper retains this complete case |

#### B089: 2 methods become one table

Proposed keeper: `WorkspaceRunTests.test_workspace_repeated_refusal_reuses_completed_claims`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_repeated_queue_refusal`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkspaceRunTests.test_workspace_repeated_refusal_reuses_completed_claims` | Table | B089: the proposed keeper retains this complete case |
| `WorkspaceRunTests.test_workspace_repeated_refusal_restores_formatted_creations` | Table | B089: the proposed keeper retains this complete case |

#### B090: 2 methods become one table

Proposed keeper: `WorkspaceRunTests.test_later_rename_is_proved_without_a_held_traits_overlay`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_trait_overlay_rollback`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkspaceRunTests.test_later_rename_is_proved_without_a_held_traits_overlay` | Table | B090: the proposed keeper retains this complete case |
| `WorkspaceRunTests.test_formatter_rollback_reproves_and_refuses_a_new_capture` | Table | B090: the proposed keeper retains this complete case |

#### B091: 2 methods become one table

Proposed keeper: `WorkspaceRunTests.test_formatter_retry_restores_paths_excluded_by_survivor_reproof`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.check_formatter_retry_after_reproof_excludes_another_file`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkspaceRunTests.test_formatter_retry_restores_paths_excluded_by_survivor_reproof` | Table | B091: the proposed keeper retains this complete case |
| `WorkspaceRunTests.test_recovery_restores_a_formatter_retry_on_an_excluded_path_and_releases_tickets` | Table | B091: the proposed keeper retains this complete case |

#### B092: 2 methods become one table

Proposed keeper: `WorkspaceRunTests.test_missing_pass_record_reverts_and_releases_the_batch`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_pass_record_refuses_batch`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `WorkspaceRunTests.test_missing_pass_record_reverts_and_releases_the_batch` | Table | B092: the proposed keeper retains this complete case |
| `WorkspaceRunTests.test_other_tree_pass_record_reverts_and_releases_the_batch` | Table | B092: the proposed keeper retains this complete case |

#### B093: 2 methods become one table

Proposed keeper: `FreshMachineJudgmentTests.test_changed_declaration_that_no_longer_declares_the_old_name_is_refused`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `assertIn`, `self.fresh_case`, `self.prove`, `MACHINE_RENAME_AFTER.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FreshMachineJudgmentTests.test_changed_declaration_that_no_longer_declares_the_old_name_is_refused` | Table | B093: the proposed keeper retains this complete case |
| `FreshMachineJudgmentTests.test_current_parameter_type_must_still_justify_the_requested_name` | Table | B093: the proposed keeper retains this complete case |

### style/test_finding_sites.py

Owner: owner unknown. Declared methods: 68. After active actions: 25, estimate.
Keep every Rust construct as a named row. Equal refusal results do not make the constructs redundant.

#### B094: 13 methods become one table

Proposed keeper: `FindingSitesTests.test_refuses_fn_behind_qualifier`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_hidden_item_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_refuses_fn_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_struct_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_enum_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_trait_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_impl_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_mod_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_static_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_type_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_use_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_extern_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_const_item_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_union_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_macro_rules_behind_qualifier` | Table | B094: the proposed keeper retains this complete case |

#### B095: 5 methods become one table

Proposed keeper: `FindingSitesTests.test_refuses_dollar_visibility_prefix`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_macro_prefix_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_refuses_dollar_visibility_prefix` | Table | B095: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_unknown_qualifier_chain` | Table | B095: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_two_identifier_prefix` | Table | B095: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_macro_fragment_prefix` | Table | B095: the proposed keeper retains this complete case |
| `FindingSitesTests.test_refuses_path_prefix` | Table | B095: the proposed keeper retains this complete case |

#### B096: 6 methods become one table

Proposed keeper: `FindingSitesTests.test_generic_prefix_cannot_hide_owner_move`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_owner_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_generic_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |
| `FindingSitesTests.test_async_move_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |
| `FindingSitesTests.test_parenthesized_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |
| `FindingSitesTests.test_macro_call_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |
| `FindingSitesTests.test_turbofish_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |
| `FindingSitesTests.test_bracket_prefix_cannot_hide_owner_move` | Table | B096: the proposed keeper retains this complete case |

#### B097: 20 methods become one table

Proposed keeper: `FindingSitesTests.test_enum_variant_brace_comma_continues`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_continuation_carries`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_enum_variant_brace_comma_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_match_arm_brace_comma_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_destructuring_brace_arrow_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_destructuring_brace_equals_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_destructuring_let_else_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_block_expression_else_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_unsafe_block_postfix_question_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_block_postfix_dot_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_attribute_tests_let_after_metadata` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_attribute_tests_if_after_metadata` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_attribute_tests_block_after_metadata` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_attribute_tests_call_after_metadata` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_attribute_tests_field_after_metadata` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_qualified_self_path_starts_expression` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_qualified_reference_path_starts_expression` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_binding_at_struct_pattern_continues` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_struct_fields_have_field_boundaries` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_struct_literal_shorthand_fields_have_field_boundaries` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_variant_fields_and_shorthand_patterns_have_field_boundaries` | Table | B097: the proposed keeper retains this complete case |
| `FindingSitesTests.test_generic_and_closure_commas_do_not_start_statements` | Table | B097: the proposed keeper retains this complete case |

#### B098: 3 methods become one table

Proposed keeper: `FindingSitesTests.test_function_pointer_let_reads_owner`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_function_pointer_owner`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_function_pointer_let_reads_owner` | Table | B098: the proposed keeper retains this complete case |
| `FindingSitesTests.test_function_pointer_parameter_reads_owner` | Table | B098: the proposed keeper retains this complete case |
| `FindingSitesTests.test_function_pointer_return_reads_owner` | Table | B098: the proposed keeper retains this complete case |

#### B099: 2 methods become one table

Proposed keeper: `FindingSitesTests.test_function_pointer_field_reads_owner`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_function_pointer_owner`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `FindingSitesTests.test_function_pointer_field_reads_owner` | Table | B099: the proposed keeper retains this complete case |
| `FindingSitesTests.test_function_pointer_extern_field_reads_owner` | Table | B099: the proposed keeper retains this complete case |

### style/test_rules.py

Owner: owner unknown. Declared methods: 50. After active actions: 48, estimate.

#### B100: 2 methods become one table

Proposed keeper: `RuleTests.test_local_tier_d_rule_rejects_the_types_family`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertRaises`, `self.member_repo`, `local_dir.mkdir`, `path.write_text`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RuleTests.test_local_tier_d_rule_rejects_the_types_family` | Table | B100: the proposed keeper retains this complete case |
| `RuleTests.test_local_tier_c_rule_requires_a_family` | Table | B100: the proposed keeper retains this complete case |

#### B101: 2 methods become one table

Proposed keeper: `RuleTests.test_member_rule_without_a_matching_workspace_member_names_the_error`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assertRaises`, `self.member_repo`, `local_dir.mkdir`, `path.write_text`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `RuleTests.test_member_rule_without_a_matching_workspace_member_names_the_error` | Table | B101: the proposed keeper retains this complete case |
| `RuleTests.test_local_candidate_rules_require_a_declared_surface` | Table | B101: the proposed keeper retains this complete case |

### style/test_standing_checkpoint.py

Owner: owner unknown. Declared methods: 6. After active actions: 5, estimate.
Keep separate clean and held ticket state. Each row must prove the merge and refill without a commit.

#### B102: 2 methods become one table

Proposed keeper: `StandingCheckpointTests.test_clean_only_ticket_merges_and_refills_without_a_commit`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.close_without_code`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StandingCheckpointTests.test_clean_only_ticket_merges_and_refills_without_a_commit` | Table | B102: the proposed keeper retains this complete case |
| `StandingCheckpointTests.test_held_only_ticket_merges_and_refills_without_a_commit` | Table | B102: the proposed keeper retains this complete case |

### style/test_style_footprint.py

Owner: owner unknown. Declared methods: 271. After active actions: 222, estimate.
This module already copies a class seed. Keep a fresh repository copy for each diff case.

#### B103: 3 methods become one table

Proposed keeper: `StyleFootprintTests.test_whitespace_only_function_formatting_is_mechanical`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_mechanical`, `self.write`, `self.commit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_whitespace_only_function_formatting_is_mechanical` | Table | B103: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_erasing_a_bodyless_trait_method_parameter_is_mechanical` | Table | B103: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_erasing_an_unused_externally_reachable_function_parameter_is_mechanical` | Table | B103: the proposed keeper retains this complete case |

#### B104: 4 methods become one table

Proposed keeper: `StyleFootprintTests.test_erasing_an_unused_required_trait_implementation_parameter_is_mechanical`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_mechanical`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_erasing_an_unused_required_trait_implementation_parameter_is_mechanical` | Table | B104: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_plain_local_rename_with_uses_after_its_statement_is_mechanical` | Table | B104: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_plain_unqualified_enum_variant_rename_is_mechanical` | Table | B104: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_enum_variant_rename_with_default_derive_is_mechanical` | Table | B104: the proposed keeper retains this complete case |

#### B105: 8 methods become one table

Proposed keeper: `StyleFootprintTests.test_erasing_a_parameter_named_inside_a_macro_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_erasing_a_parameter_named_inside_a_macro_requires_review` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_erasing_a_parameter_read_through_a_raw_identifier_requires_review` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_erasing_a_parameter_with_unavailable_token_spans_requires_review` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_refuses_a_name_already_used_in_the_function` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_refuses_an_old_name_left_in_the_function` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_cannot_certify_a_changed_parameter_in_another_function` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_inside_a_macro_requires_review` | Table | B105: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_reflected_struct_rename_cannot_be_proven_mechanical` | Table | B105: the proposed keeper retains this complete case |

#### B106: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_erasing_an_unused_parameter_with_a_type_change_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.write`, `self.commit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_erasing_an_unused_parameter_with_a_type_change_requires_review` | Table | B106: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_added_derive_reports_a_trait_implementation_shape_change` | Table | B106: the proposed keeper retains this complete case |

#### B107: 9 methods become one table

Proposed keeper: `StyleFootprintTests.test_complete_rename_used_by_stringify_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_complete_rename_used_by_stringify_requires_review` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_multiline_stringify_does_not_prove_a_definition_rename_safe` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_free_function_rename_cannot_justify_an_unrelated_receiver_method_swap` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_method_target_swap_without_a_definition_rename_requires_review` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_refuses_a_different_call_in_its_initializer` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_refuses_a_different_call_before_its_declaration` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_cannot_move_a_value_to_a_different_struct_field` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_definition_rename_cannot_certify_a_foreign_qualified_call` | Table | B107: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_glob_reexport_makes_a_private_inline_modules_public_function_reachable` | Table | B107: the proposed keeper retains this complete case |

#### B108: 3 methods become one table

Proposed keeper: `StyleFootprintTests.test_spacing_change_inside_a_macro_invocation_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assert_review`, `self.write`, `self.commit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_spacing_change_inside_a_macro_invocation_requires_review` | Table | B108: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_added_derive_on_a_union_reports_a_trait_implementation_shape_change` | Table | B108: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_derive_on_an_unrecognized_item_is_not_erased_from_the_diff` | Table | B108: the proposed keeper retains this complete case |

#### B109: 3 methods become one table

Proposed keeper: `StyleFootprintTests.test_module_declaration_change_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.write`, `self.commit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_module_declaration_change_requires_review` | Table | B109: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_public_item_inside_public_module_requires_review` | Table | B109: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_enum_variant_rename_refuses_remaining_self_use_in_a_generic_impl` | Table | B109: the proposed keeper retains this complete case |

#### B110: 5 methods become one table

Proposed keeper: `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_name_and_a_glob_requires_review`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.shorten_module_constants`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_name_and_a_glob_requires_review` | Table | B110: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_name_as_a_macro_requires_review` | Table | B110: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_path_left_requires_review` | Table | B110: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_name_in_a_macro_argument_requires_review` | Table | B110: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_constant_import_shortening_with_a_removed_module_name_in_a_macro_rules_body_requires_review` | Table | B110: the proposed keeper retains this complete case |

#### B111: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_import_scope_allows_a_std_constant_with_a_child_module_glob`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_mechanical`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_import_scope_allows_a_std_constant_with_a_child_module_glob` | Table | B111: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_import_cfg_accepts_the_same_guard_on_the_import_and_function` | Table | B111: the proposed keeper retains this complete case |

#### B112: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_import_scope_refuses_an_import_inside_a_function`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_review`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_import_scope_refuses_an_import_inside_a_function` | Table | B112: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_enum_variant_rename_refuses_an_attribute_on_the_renamed_variant` | Table | B112: the proposed keeper retains this complete case |

#### B113: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_import_cfg_accepts_matching_guards_with_a_known_std_root`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_mechanical`, `self.write`, `self.commit`, `before.replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_import_cfg_accepts_matching_guards_with_a_known_std_root` | Table | B113: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_import_external_root_accepts_a_word_used_only_as_a_path_root` | Table | B113: the proposed keeper retains this complete case |

#### B114: 4 methods become one table

Proposed keeper: `StyleFootprintTests.test_enum_variant_rename_cannot_certify_another_enums_qualified_use`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `assert_review`, `self.write`, `self.commit`, `before.replace('Mode { Old', 'Mode { New').replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_enum_variant_rename_cannot_certify_another_enums_qualified_use` | Table | B114: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_local_rename_refuses_a_shorthand_field_inside_a_condition` | Table | B114: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_same_line_rename_and_literal_edit_requires_review` | Table | B114: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_same_line_rename_and_condition_edit_requires_review` | Table | B114: the proposed keeper retains this complete case |

#### B115: 4 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_the_item_changes`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`, `self.LIMIT_MOVED['src/constants.rs'].replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_the_item_changes` | Table | B115: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_the_reexport_widens_its_visibility` | Table | B115: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_its_doc_comment_changes` | Table | B115: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_with_another_edit` | Table | B115: the proposed keeper retains this complete case |

#### B116: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_a_touched_file_has_a_glob_use`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_touched_file_has_a_glob_use` | Table | B116: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_the_source_still_names_it_without_a_use` | Table | B116: the proposed keeper retains this complete case |

#### B117: 4 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_a_raw_cfg_guards_the_destination`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_raw_cfg_guards_the_destination` | Table | B117: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_commented_path_attribute_guards_the_destination` | Table | B117: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_attribute_macro_guards_the_destination` | Table | B117: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_attribute_on_the_destination_path_holds_a_comment` | Table | B117: the proposed keeper retains this complete case |

#### B118: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_a_generic_parameter_shadows_a_repointed_path`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`, `before['src/lib.rs'].replace`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_generic_parameter_shadows_a_repointed_path` | Table | B118: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_touched_file_has_a_raw_identifier` | Table | B118: the proposed keeper retains this complete case |

#### B119: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_the_manifest_cannot_be_read`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_the_manifest_cannot_be_read` | Table | B119: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_main_declares_a_module_beside_the_library` | Table | B119: the proposed keeper retains this complete case |

#### B120: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_a_derive_can_read_the_module_path`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`, `text.replace`, `self.SHAPE_FILES.items`, `self.SHAPE_MOVED.items`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_derive_can_read_the_module_path` | Table | B120: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_derive_path_leaves_the_standard_library` | Table | B120: the proposed keeper retains this complete case |

#### B121: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_an_associated_path_finds_another_trait`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_associated_path_finds_another_trait` | Table | B121: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_associated_path_finds_a_lowercase_trait` | Table | B121: the proposed keeper retains this complete case |

#### B122: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_an_extern_crate_brings_its_macros`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_extern_crate_brings_its_macros` | Table | B122: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_macro_use_list_names_a_standard_derive` | Table | B122: the proposed keeper retains this complete case |

#### B123: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_an_attribute_macro_on_another_item_can_define_a_name`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_an_attribute_macro_on_another_item_can_define_a_name` | Table | B123: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_the_source_drops_the_prelude_after_the_move` | Table | B123: the proposed keeper retains this complete case |

#### B124: 2 methods become one table

Proposed keeper: `StyleFootprintTests.test_an_item_move_is_refused_when_a_macro_binds_its_arguments`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_macro_binds_its_arguments` | Table | B124: the proposed keeper retains this complete case |
| `StyleFootprintTests.test_an_item_move_is_refused_when_a_macro_takes_a_keyword_as_an_argument` | Table | B124: the proposed keeper retains this complete case |

### style/test_style_ledger.py

Owner: owner unknown. Declared methods: 100. After active actions: 97, estimate.

#### B125: 3 methods become one table

Proposed keeper: `StyleLedgerRecordsTests.test_generation_backfill_timestamp_tie_uses_higher_id_blob`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_generation_backfill`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerRecordsTests.test_generation_backfill_timestamp_tie_uses_higher_id_blob` | Table | B125: the proposed keeper retains this complete case |
| `StyleLedgerRecordsTests.test_generation_backfill_newer_timestamp_wins_over_higher_id` | Table | B125: the proposed keeper retains this complete case |
| `StyleLedgerRecordsTests.test_generation_backfill_partitions_uncovered_component_with_its_base_rule` | Table | B125: the proposed keeper retains this complete case |

#### B126: 2 methods become one table

Proposed keeper: `StyleLedgerRecordsTests.test_rule_reads_becomes_stale_when_own_file_changes`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.rule`, `self.set_tip`, `self.record_reads`, `self.write`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerRecordsTests.test_rule_reads_becomes_stale_when_own_file_changes` | Table | B126: the proposed keeper retains this complete case |
| `StyleLedgerRecordsTests.test_rule_reads_becomes_stale_when_read_file_changes` | Table | B126: the proposed keeper retains this complete case |

### style/test_style_ledger_findings.py

Owner: owner unknown. Declared methods: 139. After active actions: 139, estimate.
**Held by Phase 85.** Its eight Table candidates remain unchanged in the after count.

#### B127: 2 methods become one table

Proposed keeper: `StyleLedgerFindingsTests.test_a_later_outer_binding_cannot_build_using_an_anchor_without_a_saved_base`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_outer_scope_refuses_built`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerFindingsTests.test_a_later_outer_binding_cannot_build_using_an_anchor_without_a_saved_base` | Table | B127: the proposed keeper retains this complete case |
| `StyleLedgerFindingsTests.test_an_applied_rename_without_a_saved_base_keeps_later_current_bindings_visible` | Table | B127: the proposed keeper retains this complete case |

#### B128: 4 methods become one table

Proposed keeper: `StyleLedgerFindingsTests.test_an_inherited_inner_attribute_never_allows_a_finding_to_build`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_outer_scope_refuses_built`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerFindingsTests.test_an_inherited_inner_attribute_never_allows_a_finding_to_build` | Table | B128: the proposed keeper retains this complete case |
| `StyleLedgerFindingsTests.test_comparison_before_an_unread_binding_cannot_be_marked_built` | Table | B128: the proposed keeper retains this complete case |
| `StyleLedgerFindingsTests.test_preceding_enclosing_macro_cannot_be_marked_built` | Table | B128: the proposed keeper retains this complete case |
| `StyleLedgerFindingsTests.test_undecidable_outer_binding_visibility_cannot_be_overridden_as_built` | Table | B128: the proposed keeper retains this complete case |

#### B129: 2 methods become one table

Proposed keeper: `StyleLedgerFindingsTests.test_same_line_outer_bindings_cannot_be_marked_built`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_outer_scope_refuses_built`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerFindingsTests.test_same_line_outer_bindings_cannot_be_marked_built` | Table | B129: the proposed keeper retains this complete case |
| `StyleLedgerFindingsTests.test_raw_outer_binding_cannot_be_marked_built` | Table | B129: the proposed keeper retains this complete case |

### style/test_style_ledger_sites.py

Owner: owner unknown. Declared methods: 169. After active actions: 147, estimate.
Keep the ledger entry path. A lower-level parser test does not prove ledger ownership or follow behavior.

#### B130: 5 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_site_refuses_dollar_visibility_owner_move`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_hidden_enclosing_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_site_refuses_dollar_visibility_owner_move` | Table | B130: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_refuses_unknown_qualifier_chain_owner_move` | Table | B130: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_refuses_two_identifier_owner_move` | Table | B130: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_refuses_macro_fragment_owner_move` | Table | B130: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_refuses_path_prefix_owner_move` | Table | B130: the proposed keeper retains this complete case |

#### B131: 7 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_site_carries_none_match_arm_boundary`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_expression_boundary_carries`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_site_carries_none_match_arm_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_wildcard_match_arm_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_struct_literal_field_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_compound_assignment_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_cast_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_range_boundary` | Table | B131: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_carries_comma_boundary` | Table | B131: the proposed keeper retains this complete case |

#### B132: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_site_owner_refuses_work_moved_between_nested_extern_functions`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_extern_owner_move_refused`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_site_owner_refuses_work_moved_between_nested_extern_functions` | Table | B132: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_site_owner_refuses_qualified_work_moved_between_nested_extern_functions` | Table | B132: the proposed keeper retains this complete case |

#### B133: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_a_statement_whose_text_changed_in_the_moved_function_is_left`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.follow_statement`, `style_ledger.StatementNotFollowed`, `declarations.Declaration`, `declarations.ModuleItem`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_a_statement_whose_text_changed_in_the_moved_function_is_left` | Table | B133: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_a_statement_whose_text_is_only_in_a_function_nested_in_the_moved_function_is_left` | Table | B133: the proposed keeper retains this complete case |

#### B134: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_inherent_source_keeps_trait_follow_identity`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_trait_source_collision`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_inherent_source_keeps_trait_follow_identity` | Table | B134: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_other_trait_source_keeps_trait_follow_identity` | Table | B134: the proposed keeper retains this complete case |

#### B135: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_inherent_destination_keeps_trait_follow_identity`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_trait_destination_collision`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_inherent_destination_keeps_trait_follow_identity` | Table | B135: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_other_trait_destination_keeps_trait_follow_identity` | Table | B135: the proposed keeper retains this complete case |

#### B136: 3 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_preexisting_destination_keeps_trait_follow_identity`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_trait_destination_limit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_preexisting_destination_keeps_trait_follow_identity` | Table | B136: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_two_destination_files_keeps_trait_follow_identity` | Table | B136: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_two_destination_items_keeps_trait_follow_identity` | Table | B136: the proposed keeper retains this complete case |

#### B137: 5 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_unapproved_trait_rename_keeps_trait_follow_identity`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_trait_follow_limit`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_unapproved_trait_rename_keeps_trait_follow_identity` | Table | B137: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_trait_rename_from_another_crate_keeps_trait_follow_identity` | Table | B137: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_forked_trait_rename_keeps_trait_follow_identity` | Table | B137: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_open_trait_build_ticket_keeps_trait_follow_identity` | Table | B137: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_trait_rename_losing_approval_before_lock_keeps_trait_follow_identity` | Table | B137: the proposed keeper retains this complete case |

#### B138: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_an_unreadable_implemented_trait_grants_no_method_follow`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_changed_trait_membership_is_left`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_an_unreadable_implemented_trait_grants_no_method_follow` | Table | B138: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_matching_trait_leaf_names_do_not_hide_different_qualified_trait_paths` | Table | B138: the proposed keeper retains this complete case |

#### B139: 2 methods become one table

Proposed keeper: `StyleLedgerSitesTests.test_a_finding_whose_item_two_built_renames_renamed_in_turn_follows_the_last_name`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.ledger`, `self.restructured_parent`, `self.built_finding`, `self.commit_tip`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleLedgerSitesTests.test_a_finding_whose_item_two_built_renames_renamed_in_turn_follows_the_last_name` | Table | B139: the proposed keeper retains this complete case |
| `StyleLedgerSitesTests.test_a_finding_whose_built_renames_come_back_to_its_name_follows_the_name_before_the_return` | Table | B139: the proposed keeper retains this complete case |

### style/test_style_plan_gate.py

Owner: owner unknown. Declared methods: 64. After active actions: 59, estimate.

#### B140: 2 methods become one table

Proposed keeper: `StylePlanTests.test_gate_and_checkpoint_refuse_an_impl_inner_attribute`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_inner_attribute_refuses_gate_and_checkpoint`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_gate_and_checkpoint_refuse_an_impl_inner_attribute` | Table | B140: the proposed keeper retains this complete case |
| `StylePlanTests.test_gate_and_checkpoint_refuse_a_trait_inner_attribute` | Table | B140: the proposed keeper retains this complete case |

#### B141: 2 methods become one table

Proposed keeper: `StylePlanTests.test_approved_doc_replacement_does_not_exempt_new_documentation_in_the_same_hunk`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `self.approved_documentation_ticket`, `target.write_text`, `self.reference_gate`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_approved_doc_replacement_does_not_exempt_new_documentation_in_the_same_hunk` | Table | B141: the proposed keeper retains this complete case |
| `StylePlanTests.test_approval_does_not_move_from_module_comment_to_function_documentation` | Table | B141: the proposed keeper retains this complete case |

#### B142: 2 methods become one table

Proposed keeper: `StylePlanTests.test_approved_doc_edit_allows_an_ordinary_line_added_above_its_site`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.approved_documentation_ticket`, `target.write_text`, `self.reference_gate`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_approved_doc_edit_allows_an_ordinary_line_added_above_its_site` | Table | B142: the proposed keeper retains this complete case |
| `StylePlanTests.test_approved_doc_edit_allows_an_ordinary_line_removed_above_its_site` | Table | B142: the proposed keeper retains this complete case |

#### B143: 2 methods become one table

Proposed keeper: `StylePlanTests.test_approved_rename_refuses_an_approval_without_a_parent`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `self.write`, `self.commit`, `self.approved_reference_ticket`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_approved_rename_refuses_an_approval_without_a_parent` | Table | B143: the proposed keeper retains this complete case |
| `StylePlanTests.test_approved_rename_refuses_a_pair_that_changes_the_parent_path` | Table | B143: the proposed keeper retains this complete case |

#### B144: 2 methods become one table

Proposed keeper: `StylePlanTests.test_type_approval_keeps_reference_files_outside_its_scope`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertIn`, `self.write`, `self.commit`, `self.approved_reference_ticket`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_type_approval_keeps_reference_files_outside_its_scope` | Table | B144: the proposed keeper retains this complete case |
| `StylePlanTests.test_restructure_approval_keeps_reference_files_outside_its_scope` | Table | B144: the proposed keeper retains this complete case |

### style/test_style_plan_module_owners.py

Owner: owner unknown. Declared methods: 13. After active actions: 4, estimate.

#### B145: 9 methods become one table

Proposed keeper: `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_path_attribute`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIn`, `self.owned_module_decision`, `self.refill_with_busy`, `self.ledger().named_held`, `self.ledger`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_path_attribute` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_has_a_path_attribute_in_an_inline_module` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_has_a_cfg_attr_path` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_mounts_its_folder_on_an_inline_module` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_mounts_its_folder_by_a_raw_path_attribute` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_raw_path_attribute` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_a_raw_module_name` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_whose_owner_declares_it_by_an_escaped_path_string` | Table | B145: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_restructure_of_a_mod_rs_file_whose_declaring_root_is_busy` | Table | B145: the proposed keeper retains this complete case |

#### B146: 2 methods become one table

Proposed keeper: `StylePlanTests.test_decision_refill_builds_a_restructure_whose_busy_file_names_its_module_without_declaring_it`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertNotIn`, `assertEqual`, `self.owned_module_decision`, `self.write`, `self.commit_to_every_checkout`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_decision_refill_builds_a_restructure_whose_busy_file_names_its_module_without_declaring_it` | Table | B146: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_builds_a_restructure_whose_busy_file_mounts_another_file_by_path` | Table | B146: the proposed keeper retains this complete case |

### style/test_style_plan_ownership.py

Owner: owner unknown. Declared methods: 18. After active actions: 13, estimate.

#### B147: 6 methods become one table

Proposed keeper: `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_by_a_path_attribute_in_an_inline_module`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_rename_waits_for_an_uncertain_path`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_by_a_path_attribute_in_an_inline_module` | Table | B147: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_by_a_cfg_attr_path` | Table | B147: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_in_a_folder_an_inline_module_mounts` | Table | B147: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_by_an_escaped_path_string` | Table | B147: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_declared_by_an_escaped_path_string_outside_the_crate` | Table | B147: the proposed keeper retains this complete case |
| `StylePlanTests.test_decision_refill_holds_a_file_rename_whose_own_file_has_an_uncertain_path` | Table | B147: the proposed keeper retains this complete case |

### style/test_style_plan_renames.py

Owner: owner unknown. Declared methods: 32. After active actions: 31, estimate.

#### B148: 2 methods become one table

Proposed keeper: `StylePlanTests.test_bare_rename_mention_allows_no_matching_rust_files`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.write`, `self.commit`, `self.approved_reference_ticket`, `self.reference_gate`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StylePlanTests.test_bare_rename_mention_allows_no_matching_rust_files` | Table | B148: the proposed keeper retains this complete case |
| `StylePlanTests.test_approved_rename_allows_bare_markdown_mentions_of_a_unique_item` | Table | B148: the proposed keeper retains this complete case |

### style/test_style_sync.py

Owner: owner unknown. Declared methods: 62. After active actions: 59, estimate.
Move the budget cases into a small fixture. They do not need the current Git repository and staff setup.

#### B149: 2 methods become one table

Proposed keeper: `StyleSyncTests.test_queued_send_handoff_is_sent_to_the_parent`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.handoff_checkpoint`, `self.handoff_sender`, `self.send_handoff`, `self.handoff_notices`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleSyncTests.test_queued_send_handoff_is_sent_to_the_parent` | Table | B149: the proposed keeper retains this complete case |
| `StyleSyncTests.test_kept_handoff_notice_records_acceptance_and_reports_queued` | Table | B149: the proposed keeper retains this complete case |

#### B150: 2 methods become one table

Proposed keeper: `StyleSyncTests.test_handoff_budget_tracks_a_raised_cleanup_grace_without_reload`. Class: Table.
Reason: both cases write configuration, call `handoff_notice`, and check the selected timeout.
Keep both configuration families and their exact timeout formulas.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StyleSyncTests.test_handoff_budget_tracks_a_raised_cleanup_grace_without_reload` | Table | B150: the proposed keeper retains this complete case |
| `StyleSyncTests.test_handoff_budget_tracks_raised_postmaster_wait_and_claim_grace_without_reload` | Table | B150: the proposed keeper retains this complete case |

#### Repeated send-budget assertion

Candidate: `StyleSyncTests.test_a_send_is_given_send_pys_whole_run_before_it_counts_as_timed_out`. Class: Cross-module repeat.
Keeper in the other split: `message/test_send_budget.py::SendBudgetTests.test_default_budget_tracks_delivery_and_cleanup_changes_without_reload`.
Caller keeper: `StyleSyncTests.test_handoff_budget_tracks_a_raised_cleanup_grace_without_reload`.
Also keep `StyleSyncTests.test_handoff_budget_tracks_raised_postmaster_wait_and_claim_grace_without_reload` as a separate case in its proposed table.
The candidate calls `send_budget.seconds` directly. It does not call `style_sync` behavior.
The message keeper checks the budget change after delivery and cleanup configuration changes.
The caller keeper checks `[150, 290]` through `handoff_notice` and checks the raised cleanup limit.
These caller cases retain the real timeout selection. They mock only the sending process.
This keeper crosses into `message`, which belongs to report A. This report credits the one removal in `style` only.
CPU saved: 0 s, planning estimate. Assertion coverage remains a static claim until a coverage sweep and mutation check.

### test_times/test_coverage_overlap.py

Owner: natest-unit. Declared methods: 38. After active actions: 36, estimate.

#### B151: 2 methods become one table

Proposed keeper: `CoverageOverlapTests.test_failed_launch_keeps_its_test_and_the_next_test_known`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `assertFalse`, `self.run_fixture`, `self.subTest`, `fixture.row`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CoverageOverlapTests.test_failed_launch_keeps_its_test_and_the_next_test_known` | Table | B151: the proposed keeper retains this complete case |
| `CoverageOverlapTests.test_runner_failed_exec_writes_no_child_report_or_cause` | Table | B151: the proposed keeper retains this complete case |

#### B152: 2 methods become one table

Proposed keeper: `CoverageOverlapTests.test_child_chains_the_next_sitecustomize`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `self.run_fixture`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CoverageOverlapTests.test_child_chains_the_next_sitecustomize` | Table | B152: the proposed keeper retains this complete case |
| `CoverageOverlapTests.test_child_preserves_nix_pythonpath` | Table | B152: the proposed keeper retains this complete case |

### test_times/test_module_run.py

Owner: natest-unit. Declared methods: 20. After active actions: 19, estimate.

#### B153: 2 methods become one table

Proposed keeper: `ModuleRunnerTests.test_none_fixtures_keep_the_standard_runner_result`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `tempfile.TemporaryDirectory`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `ModuleRunnerTests.test_none_fixtures_keep_the_standard_runner_result` | Table | B153: the proposed keeper retains this complete case |
| `ModuleRunnerTests.test_a_class_without_an_imported_module_keeps_the_standard_result` | Table | B153: the proposed keeper retains this complete case |

### usage/test_compaction_replay.py

Owner: owner unknown. Declared methods: 17. After active actions: 14, estimate.

#### B154: 2 methods become one table

Proposed keeper: `CompactionReplayTests.test_equal_bounds_are_refused_without_changing_the_store`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_store_preserved_on_refusal`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CompactionReplayTests.test_equal_bounds_are_refused_without_changing_the_store` | Table | B154: the proposed keeper retains this complete case |
| `CompactionReplayTests.test_reversed_bounds_are_refused_without_changing_the_store` | Table | B154: the proposed keeper retains this complete case |

#### B155: 3 methods become one table

Proposed keeper: `CompactionReplayTests.test_negative_follow_up_prompt_is_refused_without_changing_the_store`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_store_preserved_on_refusal`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CompactionReplayTests.test_negative_follow_up_prompt_is_refused_without_changing_the_store` | Table | B155: the proposed keeper retains this complete case |
| `CompactionReplayTests.test_nan_follow_up_prompt_is_refused_without_changing_the_store` | Table | B155: the proposed keeper retains this complete case |
| `CompactionReplayTests.test_infinite_follow_up_prompt_is_refused_without_changing_the_store` | Table | B155: the proposed keeper retains this complete case |

### usage/test_usage_report.py

Owner: owner unknown. Declared methods: 49. After active actions: 47, estimate.

#### B156: 2 methods become one table

Proposed keeper: `UsageReportTests.test_datetime_without_offset_is_read_in_the_requested_zone`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_report`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `UsageReportTests.test_datetime_without_offset_is_read_in_the_requested_zone` | Table | B156: the proposed keeper retains this complete case |
| `UsageReportTests.test_account_switch_uses_previous_role_label_before_the_window` | Table | B156: the proposed keeper retains this complete case |

#### B157: 2 methods become one table

Proposed keeper: `UsageReportTests.test_rate_format_prints_two_decimals_without_a_denominator`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.report_script`, `self.expected_report`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `UsageReportTests.test_rate_format_prints_two_decimals_without_a_denominator` | Table | B157: the proposed keeper retains this complete case |
| `UsageReportTests.test_number_format_rounds_a_ratio_without_decimals` | Table | B157: the proposed keeper retains this complete case |

### whoami/test_account.py

Owner: owner unknown. Declared methods: 46. After active actions: 45, estimate.

#### B158: 2 methods become one table

Proposed keeper: `AccountTests.test_darwin_process_owned_by_another_uid_is_unreadable`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertIsInstance`, `account.parse_darwin_config_dir`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `AccountTests.test_darwin_process_owned_by_another_uid_is_unreadable` | Table | B158: the proposed keeper retains this complete case |
| `AccountTests.test_darwin_process_without_visible_home_environment_is_unreadable` | Table | B158: the proposed keeper retains this complete case |

### whoami/test_codex_alert_accounts.py

Owner: owner unknown. Declared methods: 77. After active actions: 76, estimate.

#### B159: 2 methods become one table

Proposed keeper: `CodexAccountAlertTests.test_queued_workers_head_waits_for_a_five_hour_reset_three_hours_away`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_queued_workers_wait_for_known_reset`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CodexAccountAlertTests.test_queued_workers_head_waits_for_a_five_hour_reset_three_hours_away` | Table | B159: the proposed keeper retains this complete case |
| `CodexAccountAlertTests.test_queued_workers_head_waits_for_a_known_weekly_reset` | Table | B159: the proposed keeper retains this complete case |

### whoami/test_quota_zero_episode.py

Owner: owner unknown. Declared methods: 65. After active actions: 64, estimate.

#### B160: 2 methods become one table

Proposed keeper: `CodexZeroEpisodeTests.test_paid_credits_with_a_self_targeting_workers_head_promise_only_the_queue`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assert_queued_credits_wait_without_a_fallback_promise`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `CodexZeroEpisodeTests.test_paid_credits_with_a_self_targeting_workers_head_promise_only_the_queue` | Table | B160: the proposed keeper retains this complete case |
| `CodexZeroEpisodeTests.test_paid_credits_with_a_blocked_other_workers_head_promise_only_the_queue` | Table | B160: the proposed keeper retains this complete case |

### whoami/test_worker_usage.py

Owner: owner unknown. Declared methods: 27. After active actions: 25, estimate.

#### B161: 2 methods become one table

Proposed keeper: `StoredSwitchTests.test_simultaneous_accounts_after_history_without_workers_changes_have_zero_switches`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.stored_row`, `self.dual_readings`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StoredSwitchTests.test_simultaneous_accounts_after_history_without_workers_changes_have_zero_switches` | Table | B161: the proposed keeper retains this complete case |
| `StoredSwitchTests.test_simultaneous_account_credits_sum_each_accounts_own_decrease` | Table | B161: the proposed keeper retains this complete case |

#### B162: 2 methods become one table

Proposed keeper: `StoredSwitchTests.test_role_changes_without_positive_reading_coverage_still_have_an_unavailable_rate`. Class: Table.
Reason: the method bodies have the same structure after literal inputs change.
Retain these calls and assertions for each row: `assertEqual`, `self.stored_row`.

| Candidate | Class | Covering test or reason |
| --- | --- | --- |
| `StoredSwitchTests.test_role_changes_without_positive_reading_coverage_still_have_an_unavailable_rate` | Table | B162: the proposed keeper retains this complete case |
| `StoredSwitchTests.test_role_changes_without_any_reading_in_the_piece_have_an_unavailable_rate` | Table | B162: the proposed keeper retains this complete case |

## Repeats across the split that need further proof

The budget assertion above is the only counted cross-module candidate.
The shell policy corpus also runs through the scanner and two hook entry paths.
Those paths prove different routing and output behavior. They do not justify a current deletion.

| Path | Test | Behavior that stays |
| --- | --- | --- |
| copies/test_copy_guard.py | `CopyGuardTests.test_shell_script_scan_retains_the_same_literal_and_executable_distinction` | Shell scan and literal/executable decisions |
| copies/test_copy_guard.py | `CopyGuardTests.test_semantic_cases_reach_shell_and_python_repository_scanners` | Shell and Python repository entry paths |
| hooks/test_hook_dispatch.py | `CopyGuardDispatchTests.test_registered_dispatch_distinguishes_literal_and_executable_substitutions` | Registered dispatch, return code, and output |
| hooks/test_codex_hooks.py | `CodexHookInstallerTests.test_registered_policy_distinguishes_literal_and_executable_substitutions` | Installed policy route and diagnostic prefix |

Later work can retain the full corpus at the scanner and select route witnesses at each hook.
First prove that the smaller hook set detects a broken registration, wrong result mapping, and missing diagnostic.
The hook changes belong to report A. Their method counts and possible savings are absent from this report.

## Growth patterns and proposed rules

These rules are proposals for the user. They create no automatic check.

| Growth pattern | Example | One proposed rule |
| --- | --- | --- |
| One method for each syntax variant | Finding qualifiers and Rust item moves | Add a named row to the existing decision table for each new input variant. |
| One method for each input spelling | Transcript shell and interpreter forms | Use a typed input table when the execution and assertion structure stays the same. |
| One method for each invalid state | Account identity and role summaries | Keep each invalid state as a labelled row with its expected diagnostic and retained siblings. |
| A parser corpus repeats through every entry path | Scanner, registered dispatcher, and installed hook | Keep the full corpus at the decision and a small, justified witness set at each entry path. |
| A budget formula repeats in callers | Send cleanup and postmaster wait | Test the formula once and test each caller's selected timeout through its real decision. |
| A small check pays a full repository fixture | Style sync budget cases | Place a direct function check in the smallest fixture that preserves its decision. |
| An import check pays several cold starts | Build log compatibility and store identity | Keep required cold starts and combine only their method declarations. |
| A recovery loop resembles a repeat | Remote receive interruptions | Name the changed state at each interruption before proposing any removal. |
| A text check grows with documentation edits | Standing checkout and lease terms | Ask the user whether exact wording is a gate contract before adding another wording check. |
| Shared helpers gain separate wrapper methods | Copy scanner and style ownership helpers | Extend the existing typed case table before adding a wrapper with the same assertion structure. |
| A retired unit leaves live code | Herdr, feature log, and Bevy upgrade | Retire a test only when its tested behavior also retires. |
| Test counts imply a speed gain | Large tables in style | Report retained case count and measured CPU beside every proposed method reduction. |

## Verification, doubts, and unverified claims

1. The gate discovery function supplied the module list.
2. The independent `ls scripts/<folder>/test_*.py | wc -l` check covered every folder.
3. Separate checks included the nested berth and prioritize modules.
4. An AST census counted declared methods in all 300 modules.
5. Candidate grouping compared method structures with literal values removed.
6. Targeted source reads checked candidate assertions, helper paths, and expensive integration behavior.
7. `show.py top --limit 800` supplied the current CPU ranking.
8. Module queries read three gate rows for all 71 candidate modules.
9. Each detached query passed. Its raw log was read and removed before the next run.
10. Only this report changes from this worker.

The inventory is a static review. It does not prove that every retained assertion is necessary.
Structural matches identify table proposals. They do not prove repeated behavior or safe shared mutable setup.
The proposed after counts assume that the director accepts each active table after checking its fixture isolation.
The manual style budget table also requires a shared helper for its two configuration families.
Runtime discovery remains unverified for the current tree. Imported test classes can change counts without changing declared methods.
At least 27 modules differ between declared counts and the last selected gate counts.
`astra/test_astra_review.py` has 22 declarations but 95 historical tests. Imported classes explain why these units need separate labels.
`nightly_review/test_nightly_review.py` has 26 declarations but 50 historical tests.
`copies/test_copies.py` has 105 declarations but 69 historical tests. Tree changes also affect the comparison.
The history for remote copy recovery changes sharply between rows. Do not combine those rows into a causal speed claim.
Per-test history reports wall time. It does not supply each command's removable CPU.
No coverage sweep, command profile, mutation test, or before/after pair ran for this report.
Thus, the cross-module removal remains a proposal with named assertion keepers. It is not an approved deletion.
No test or production file changed. No test run was needed to validate this report-only edit.
Phase 85 also holds `style/test_style_ledger_tickets.py`. The census keeps all 195 methods there.
No supported retirement, trivial deletion, or same-module repeated-result deletion emerged from this review.
Unknown owners need a director assignment before another phase changes their files.
The director must still check ten candidates against the code for the Phase 86 acceptance gate.

