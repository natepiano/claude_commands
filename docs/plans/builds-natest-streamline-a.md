# Phase 86: test streamlining, report A

Director check, 2026-11-07: ten Table groups, sampled evenly. All ten test names exist. Nine groups have one body shape after the literals change. G062 has two body shapes; check it again before a table merge.

This report covers seven script folders on `builds-natest`.
It proposes later test changes.
It changes no test or production code.

The scan covers 433 modules and 8,320 declared test methods.
The safe deletion list is empty.
There are 309 Table candidates in 127 groups.
There are three Wording candidates.
There are 736 conditional Retired candidates.

The available tables can reduce 175 method entries after fixture checks.
They keep every input case and assertion.
They do not establish a CPU gain.
Seven more method reductions belong to held work.

## Count and CPU basis

“Tests” means declared `test_*` methods in classes.
It does not mean input cases.
The count includes support modules with zero methods.
The count excludes imported and inherited methods from other files.
No test discovery import supplied these counts.

The scan reads every module as Python syntax.
It compares statement trees within each class.
It ignores method names, docstrings, and literal values for this comparison.
All 128 reported groups pass this comparison.
This comparison supports table proposals.
It does not prove equal behavior.

The CPU column uses `show.py top --limit 800`.
The saved snapshot reports rows as of 14:52 EST on 2026-11-07.
It selects up to five passing, non-aborted gate runs per module.
It excludes restarted attempts and repeated records from one run.
The configured checkout is `~/worktrees/claude-natedev-builds`.
The configured age limit is seven days.

Each folder total adds module medians.
It is a gate budget estimate, not one measured gate.
The test count comes from this worktree.
The CPU history comes from the configured checkout.
Their revisions and counts can differ.

Five modules have no selected CPU history.
The affected totals show measured lower bounds.
An unknown cost is never zero.

“After” estimates the method count after available Table actions.
It excludes held actions, retirement, and Wording removal.
Every Table action still needs a check of row isolation.
No proposed table has been implemented.

“Saved” gives the CPU gain credited by this report.
Zero credited gain means unmeasured gain.
It does not mean that the action cannot save CPU.

## Folder summary

| Folder | Modules | Tests now | Tests after available tables, estimate | CPU s per gate now, estimate | CPU s saved, credited | Behavior owner |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `production` | 116 | 2791 | 2737 | ≥ 914.999 | 0 | devtools-unit; verify-unit; owner unknown |
| `direct` | 206 | 1737 | 1701 | ≥ 2804.355 | 0 | verify-unit; resources-unit; owner unknown |
| `agents` | 8 | 875 | 862 | 269.888 | 0 | owner unknown |
| `lint` | 13 | 968 | 947 | 60.469 | 0 | resources-unit; verify-unit; owner unknown |
| `message` | 18 | 764 | 751 | 155.539 | 0 | owner unknown |
| `hooks` | 29 | 676 | 647 | 154.282 | 0 | resources-unit; verify-unit; owner unknown |
| `lib` | 43 | 509 | 500 | 112.135 | 0 | resources-unit; queue-unit; owner unknown |
| **Total** | **433** | **8,320** | **8,145** | **≥ 4,471.667** | **0** | Mixed ownership |

The shared test change owner is `natest-unit`.
The behavior owner appears beside each action.
[builds-production.md](builds-production.md) names the known builds owners.
[builds-natest.md](builds-natest.md) names `devtools-unit` for the merge gate.
No folder has one proven owner.
“Owner unknown” asks the director to assign the behavior owner.

## Class totals

These classes are disjoint.
Candidate counts refer to original methods.
The ledger gives each module's class counts.

| Class | Candidate methods | Groups | Method reduction, estimate | Current decision |
| --- | ---: | ---: | ---: | --- |
| Retired | 736 | 1 module | 736 conditional | Keep until retirement completes |
| Trivial | 0 | 0 | 0 | No supported deletion |
| Repeat | 0 | 0 | 0 | No supported deletion |
| Cross-module repeat | 0 | 0 | 0 | No supported deletion |
| Table | 309 | 127 | 182 conditional | 175 available; 7 held |
| Wording | 3 | 1 | 0 credited | The user decides |
| Retained without a candidate | 7,272 | — | 0 | Keep |
| **Total** | **8,320** | — | — | — |

The Table total includes 12 methods in five held groups.
The available groups contain 297 methods.
They become 122 table methods.
The held groups become five methods after their owners release them.

Available tables give 8,145 methods.
All tables give 8,138 methods.
Available tables plus full retirement give 7,409 methods.
All tables plus full retirement give 7,402 methods.
These are estimates.
None of these totals removes Wording tests.
None of the Table totals removes input cases.

## Ranked actions

Rank 1 has a conditional CPU opportunity.
Every later action has zero credited CPU savings.
Those actions rank by method reduction.
Current module CPU breaks ties.
Held actions appear after available actions.

The table cost is the whole module's historical median.
It is not the saving from the named group.
No proportional CPU estimate uses a method count.

Each module section names every candidate.
Each group names its proposed replacement.
The replacement does not exist yet.
It retains every original assertion and literal input.

| Rank | Action | Module | Candidate methods → methods after | Class | Covering test or reason | CPU s saved per gate | Module CPU s | Behavior owner |
| ---: | --- | --- | ---: | --- | --- | --- | ---: | --- |
| 1 | Finish retirement, then remove obsolete tests | `agents/test_codex_mesh.py` | 736 → 0, conditional | Retired | R1; keep live behavior until migration completes | 0 credited; estimate 0–194.296 conditional upper bound | 194.296 | owner unknown |
| 2 | Combine literal cases with `subTest` | `agents/test_models_conf_publish.py` | 19 → 6 | Table | G082, G083, G084, G085, G086, G087; retain all rows and assertions | 0 credited; gain unknown | 30.472 | owner unknown |
| 3 | Combine literal cases with `subTest` | `lint/test_package_imports.py` | 14 → 2 | Table | G092, G093; retain all rows and assertions | 0 credited; gain unknown | 0.387 | owner unknown |
| 4 | Combine literal cases with `subTest` | `hooks/test_post_tool_use_banned_words.py` | 15 → 5 | Table | G113, G114, G115, G116, G117; retain all rows and assertions | 0 credited; gain unknown | 1.137 | owner unknown |
| 5 | Combine literal cases with `subTest` | `production/test_merge_checkpoint_promotion.py` | 14 → 6 | Table | G016, G017, G018, G019, G020, G021; retain all rows and assertions | 0 credited; gain unknown | 19.300 | owner unknown |
| 6 | Combine literal cases with `subTest` | `message/test_session_send.py` | 9 → 1 | Table | G099; retain all rows and assertions | 0 credited; gain unknown | 12.828 | owner unknown |
| 7 | Combine literal cases with `subTest` | `hooks/test_fn_length.py` | 15 → 7 | Table | G104, G105, G106, G107, G108, G109, G110; retain all rows and assertions | 0 credited; gain unknown | 0.614 | owner unknown |
| 8 | Combine literal cases with `subTest` | `production/test_unit_move.py` | 9 → 3 | Table | G036, G037, G038; retain all rows and assertions | 0 credited; gain unknown | 5.746 | owner unknown |
| 9 | Combine literal cases with `subTest` | `lint/test_sweep.py` | 5 → 1 | Table | G094; retain all rows and assertions | 0 credited; gain unknown | 16.051 | resources-unit |
| 10 | Combine literal cases with `subTest` | `production/test_dailies_render.py` | 7 → 3 | Table | G007, G008, G009; retain all rows and assertions | 0 credited; gain unknown | 4.126 | owner unknown |
| 11 | Combine literal cases with `subTest` | `direct/test_progress_history.py` | 6 → 3 | Table | G057, G058, G059; retain all rows and assertions | 0 credited; gain unknown | 38.603 | owner unknown |
| 12 | Combine literal cases with `subTest` | `production/test_stall_watch.py` | 6 → 3 | Table | G032, G033, G034; retain all rows and assertions | 0 credited; gain unknown | 28.481 | owner unknown |
| 13 | Combine literal cases with `subTest` | `production/test_dailies_input.py` | 6 → 3 | Table | G004, G005, G006; retain all rows and assertions | 0 credited; gain unknown | 23.060 | owner unknown |
| 14 | Combine literal cases with `subTest` | `direct/test_verify_ssh_targets.py` | 5 → 2 | Table | G073, G074; retain all rows and assertions | 0 credited; gain unknown | 22.936 | verify-unit |
| 15 | Combine literal cases with `subTest` | `production/test_add_showrunner.py` | 6 → 3 | Table | G001, G002, G003; retain all rows and assertions | 0 credited; gain unknown | 21.819 | owner unknown |
| 16 | Combine literal cases with `subTest` | `production/test_merge_gate_suites_signals.py` | 4 → 1 | Table | G025; retain all rows and assertions | 0 credited; gain unknown | 13.883 | devtools-unit |
| 17 | Combine literal cases with `subTest` | `lib/test_py_floor.py` | 4 → 1 | Table | G126; retain all rows and assertions | 0 credited; gain unknown | 9.232 | owner unknown |
| 18 | Combine literal cases with `subTest` | `direct/test_verify_memory_kill.py` | 5 → 2 | Table | G071, G072; retain all rows and assertions | 0 credited; gain unknown | 2.885 | verify-unit |
| 19 | Combine literal cases with `subTest` | `direct/test_findings.py` | 5 → 2 | Table | G054, G055; retain all rows and assertions | 0 credited; gain unknown | 2.827 | owner unknown |
| 20 | Combine literal cases with `subTest` | `production/test_update_registration_answers.py` | 5 → 2 | Table | G043, G044; retain all rows and assertions | 0 credited; gain unknown | 1.504 | owner unknown |
| 21 | Combine literal cases with `subTest` | `hooks/test_heredoc_guard.py` | 5 → 2 | Table | G111, G112; retain all rows and assertions | 0 credited; gain unknown | 0.793 | owner unknown |
| 22 | Combine literal cases with `subTest` | `direct/test_verify_target_platform.py` | 5 → 2 | Table | G076, G077; retain all rows and assertions | 0 credited; gain unknown | 0.101 | verify-unit |
| 23 | Combine literal cases with `subTest` | `production/test_fast_track.py` | 4 → 2 | Table | G011, G012; retain all rows and assertions | 0 credited; gain unknown | 52.485 | owner unknown |
| 24 | Combine literal cases with `subTest` | `lint/test_memory_admit.py` | 4 → 2 | Table | G089, G090; retain all rows and assertions | 0 credited; gain unknown | 32.737 | resources-unit |
| 25 | Combine literal cases with `subTest` | `production/test_production_lifecycle.py` | 4 → 2 | Table | G026, G027; retain all rows and assertions | 0 credited; gain unknown | 28.859 | owner unknown |
| 26 | Combine literal cases with `subTest` | `production/test_merge_checkpoint_bump.py` | 3 → 1 | Table | G014; retain all rows and assertions | 0 credited; gain unknown | 24.825 | owner unknown |
| 27 | Combine literal cases with `subTest` | `message/test_roster.py` | 4 → 2 | Table | G095, G096; retain all rows and assertions | 0 credited; gain unknown | 23.243 | owner unknown |
| 28 | Combine literal cases with `subTest` | `direct/test_verify_mac_signal_boundaries.py` | 4 → 2 | Table | G069, G070; retain all rows and assertions | 0 credited; gain unknown | 13.339 | verify-unit |
| 29 | Combine literal cases with `subTest` | `lib/test_claude_launch_scan.py` | 4 → 2 | Table | G122, G123; retain all rows and assertions | 0 credited; gain unknown | 10.330 | owner unknown |
| 30 | Combine literal cases with `subTest` | `hooks/test_stop_direct_continue.py` | 4 → 2 | Table | G120, G121; retain all rows and assertions | 0 credited; gain unknown | 5.970 | owner unknown |
| 31 | Combine literal cases with `subTest` | `hooks/test_stop_context_deferral.py` | 4 → 2 | Table | G118, G119; retain all rows and assertions | 0 credited; gain unknown | 5.365 | owner unknown |
| 32 | Combine literal cases with `subTest` | `production/test_merge_checkpoint_rerun.py` | 4 → 2 | Table | G022, G023; retain all rows and assertions | 0 credited; gain unknown | 4.458 | owner unknown |
| 33 | Combine literal cases with `subTest` | `lint/test_invoke_scope.py` | 3 → 1 | Table | G088; retain all rows and assertions | 0 credited; gain unknown | 3.238 | verify-unit |
| 34 | Combine literal cases with `subTest` | `message/test_send_delivery.py` | 3 → 1 | Table | G098; retain all rows and assertions | 0 credited; gain unknown | 1.031 | owner unknown |
| 35 | Combine literal cases with `subTest` | `lib/test_py_shim.py` | 4 → 2 | Table | G127, G128; retain all rows and assertions | 0 credited; gain unknown | 0.672 | owner unknown |
| 36 | Combine literal cases with `subTest` | `direct/test_board_start.py` | 3 → 1 | Table | G052; retain all rows and assertions | 0 credited; gain unknown | unknown | owner unknown |
| 37 | Combine literal cases with `subTest` | `direct/test_verify_ssh_targets_routing.py` | 2 → 1 | Table | G075; retain all rows and assertions | 0 credited; gain unknown | 67.086 | verify-unit |
| 38 | Combine literal cases with `subTest` | `production/test_staff.py` | 2 → 1 | Table | G030; retain all rows and assertions | 0 credited; gain unknown | 56.787 | owner unknown |
| 39 | Combine literal cases with `subTest` | `production/test_mac_run.py` | 2 → 1 | Table | G013; retain all rows and assertions | 0 credited; gain unknown | 33.849 | verify-unit |
| 40 | Combine literal cases with `subTest` | `direct/test_verify_token_mac_step.py` | 2 → 1 | Table | G078; retain all rows and assertions | 0 credited; gain unknown | 29.103 | verify-unit |
| 41 | Combine literal cases with `subTest` | `message/test_send.py` | 2 → 1 | Table | G097; retain all rows and assertions | 0 credited; gain unknown | 28.688 | owner unknown |
| 42 | Combine literal cases with `subTest` | `hooks/test_direct_turn_wait_hooks.py` | 2 → 1 | Table | G103; retain all rows and assertions | 0 credited; gain unknown | 22.528 | owner unknown |
| 43 | Combine literal cases with `subTest` | `direct/test_verify_mac_retry_clock.py` | 2 → 1 | Table | G068; retain all rows and assertions | 0 credited; gain unknown | 18.410 | verify-unit |
| 44 | Combine literal cases with `subTest` | `direct/test_assign_gate.py` | 2 → 1 | Table | G047; retain all rows and assertions | 0 credited; gain unknown | 16.716 | owner unknown |
| 45 | Combine literal cases with `subTest` | `production/test_merge_gate_promotion.py` | 2 → 1 | Table | G024; retain all rows and assertions | 0 credited; gain unknown | 15.465 | devtools-unit |
| 46 | Combine literal cases with `subTest` | `production/test_worker.py` | 2 → 1 | Table | G045; retain all rows and assertions | 0 credited; gain unknown | 15.063 | owner unknown |
| 47 | Combine literal cases with `subTest` | `hooks/test_brp_launch_gate.py` | 2 → 1 | Table | G100; retain all rows and assertions | 0 credited; gain unknown | 13.209 | resources-unit |
| 48 | Combine literal cases with `subTest` | `direct/test_verify_hold_workers.py` | 2 → 1 | Table | G065; retain all rows and assertions | 0 credited; gain unknown | 11.756 | verify-unit |
| 49 | Combine literal cases with `subTest` | `direct/test_remove_workers.py` | 2 → 1 | Table | G060; retain all rows and assertions | 0 credited; gain unknown | 11.698 | owner unknown |
| 50 | Combine literal cases with `subTest` | `production/test_staff_test_support.py` | 2 → 1 | Table | G031; retain all rows and assertions | 0 credited; gain unknown | 9.308 | owner unknown |
| 51 | Combine literal cases with `subTest` | `direct/test_demo_refusals.py` | 2 → 1 | Table | G053; retain all rows and assertions | 0 credited; gain unknown | 9.107 | owner unknown |
| 52 | Combine literal cases with `subTest` | `direct/test_verify_token_release.py` | 2 → 1 | Table | G079; retain all rows and assertions | 0 credited; gain unknown | 9.076 | verify-unit |
| 53 | Combine literal cases with `subTest` | `direct/test_verify_lint_local_writes.py` | 2 → 1 | Table | G067; retain all rows and assertions | 0 credited; gain unknown | 8.915 | verify-unit |
| 54 | Combine literal cases with `subTest` | `production/test_roles_audit.py` | 2 → 1 | Table | G028; retain all rows and assertions | 0 credited; gain unknown | 8.312 | owner unknown |
| 55 | Combine literal cases with `subTest` | `direct/test_assign_claude_door.py` | 2 → 1 | Table | G046; retain all rows and assertions | 0 credited; gain unknown | 8.289 | owner unknown |
| 56 | Combine literal cases with `subTest` | `production/test_unit_status_decisions.py` | 2 → 1 | Table | G041; retain all rows and assertions | 0 credited; gain unknown | 6.983 | owner unknown |
| 57 | Combine literal cases with `subTest` | `production/test_unit_status.py` | 2 → 1 | Table | G040; retain all rows and assertions | 0 credited; gain unknown | 6.983 | owner unknown |
| 58 | Combine literal cases with `subTest` | `hooks/test_conversation_pause.py` | 2 → 1 | Table | G102; retain all rows and assertions | 0 credited; gain unknown | 5.414 | owner unknown |
| 59 | Combine literal cases with `subTest` | `lib/test_process_failure.py` | 2 → 1 | Table | G125; retain all rows and assertions | 0 credited; gain unknown | 5.087 | owner unknown |
| 60 | Combine literal cases with `subTest` | `production/test_merge_checkpoint_handoff.py` | 2 → 1 | Table | G015; retain all rows and assertions | 0 credited; gain unknown | 4.630 | owner unknown |
| 61 | Combine literal cases with `subTest` | `lib/test_nix_env.py` | 2 → 1 | Table | G124; retain all rows and assertions | 0 credited; gain unknown | 2.684 | owner unknown |
| 62 | Combine literal cases with `subTest` | `direct/test_review_launcher_ux.py` | 2 → 1 | Table | G062; retain all rows and assertions | 0 credited; gain unknown | 2.170 | owner unknown |
| 63 | Combine literal cases with `subTest` | `direct/test_rerun_lens.py` | 2 → 1 | Table | G061; retain all rows and assertions | 0 credited; gain unknown | 2.003 | owner unknown |
| 64 | Combine literal cases with `subTest` | `direct/test_verify_example_markers.py` | 2 → 1 | Table | G064; retain all rows and assertions | 0 credited; gain unknown | 2.002 | verify-unit |
| 65 | Combine literal cases with `subTest` | `production/test_smoke_launch.py` | 2 → 1 | Table | G029; retain all rows and assertions | 0 credited; gain unknown | 1.499 | owner unknown |
| 66 | Combine literal cases with `subTest` | `hooks/test_change_review_gate.py` | 2 → 1 | Table | G101; retain all rows and assertions | 0 credited; gain unknown | 1.221 | owner unknown |
| 67 | Combine literal cases with `subTest` | `production/test_dailies_render_agents.py` | 2 → 1 | Table | G010; retain all rows and assertions | 0 credited; gain unknown | 1.005 | owner unknown |
| 68 | Combine literal cases with `subTest` | `production/test_unit_phases.py` | 2 → 1 | Table | G039; retain all rows and assertions | 0 credited; gain unknown | 0.786 | owner unknown |
| 69 | Combine literal cases with `subTest` | `direct/test_worker_state.py` | 2 → 1 | Table | G081; retain all rows and assertions | 0 credited; gain unknown | 0.534 | owner unknown |
| 70 | Combine literal cases with `subTest` | `production/test_unrouted_alert.py` | 2 → 1 | Table | G042; retain all rows and assertions | 0 credited; gain unknown | 0.516 | owner unknown |
| 71 | Combine literal cases with `subTest` | `lint/test_nextest_width.py` | 2 → 1 | Table | G091; retain all rows and assertions | 0 credited; gain unknown | 0.390 | verify-unit |
| 72 | Combine literal cases with `subTest` | `direct/test_phase_artifacts.py` | 2 → 1 | Table | G056; retain all rows and assertions | 0 credited; gain unknown | 0.126 | owner unknown |
| 73 | Combine literal cases with `subTest` | `direct/test_tag_inventory.py` | 2 → 1 | Table | G063; retain all rows and assertions | 0 credited; gain unknown | 0.087 | owner unknown |
| 74 | Combine literal cases with `subTest` | `direct/test_verify_wait_holder.py` | 2 → 1 | Table | G080; retain all rows and assertions | 0 credited; gain unknown | unknown | verify-unit |
| 75 | Held: Combine literal cases with `subTest` | `direct/test_verify_lint_cache_versions.py` | 4 → 1 | Table | G066; retain all rows and assertions | 0 credited; gain unknown | 58.376 | verify-unit |
| 76 | Held: Combine literal cases with `subTest` | `direct/test_assign_launcher.py` | 4 → 2 | Table | G048, G049; retain all rows and assertions | 0 credited; gain unknown | 180.120 | owner unknown |
| 77 | Held: Combine literal cases with `subTest` | `direct/test_assign_launcher_reuse.py` | 2 → 1 | Table | G050; retain all rows and assertions | 0 credited; gain unknown | 74.558 | owner unknown |
| 78 | Held: Combine literal cases with `subTest` | `direct/test_assign_launcher_review.py` | 2 → 1 | Table | G051; retain all rows and assertions | 0 credited; gain unknown | 44.473 | owner unknown |

Wording is outside this action ranking.
Its removal requires the user's choice.

## Table implementation conditions

1. Keep every original input and expected result.
2. Name each row with the original method name.
3. Keep every assertion expression and assertion helper.
4. Build an expensive immutable seed once.
5. Give each mutating row its own copy of the seed.
6. Reset each row's files, database, environment, clock, and patches.
7. Keep each child launch that proves a separate entry point.
8. Keep existing nested tables and their full case products.
9. Run each changed module in normal and reverse case order.
10. Measure CPU before crediting a saving.

A table shares only setup that has no row state.
Some helpers already create private state per call.
Those helpers must keep that state.
A table does not remove their child launches.

`RegistryPublishingTests.setUp` creates and clones Git repositories for each method.
Its tables need one immutable repository seed and private copies.
A table must not share dirty files, indexes, refs, or a remote between rows.
The CPU saving remains unknown.

`ReclamationTests` already tests both callers in each method.
Its five scenarios must retain all ten scenario and caller pairs.
`SessionSendTests.probe` needs each launch mode.
Its nine rows still need nine child probes.

## Candidate modules

The paths below are relative to `scripts/`.
The module section supplies the reason for each listed candidate.
A proposed replacement covers only the rows assigned to that group.

### agents/test_codex_mesh.py

Class: **Retired, conditional**.
Reason **R1** applies to every test below.

The speed method marks this module as retiring.
The current code still has live callers.
`mesh_runs.py` still imports `codex_mesh`.
It uses its roster, detached run, held message, and worker APIs.
Retirement has not been proved complete.

`direct/test_launcher_waits.py` imports three fixture helpers from this test module.
`production/test_session_staff.py` imports `StubAppServer` and `select_fixture_workers`.
These helpers must move before this test module can disappear.
`production/test_affected_modules.py` uses the module name in synthetic fixture source.
That source is not a live dependency.

No other test is named as a current substitute.
The reason for later removal is the end of the supported feature.
Keep each behavior test while its feature remains supported.
The retirement owner must map any surviving behavior to its new test.
Do not remove this entire module now.

The 194.296 CPU s value is a conditional gross upper bound estimate.
Replacement tests, retained helpers, and moved behavior can reduce the net gain.
The credited current gain is zero.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FixtureModelDefaultsTests.test_only_unchanged_baseline_results_are_reused` | Retired, conditional | R1; feature retirement must complete first |
| `FixtureModelDefaultsTests.test_custom_registry_and_subprocess_faults_bypass_baseline_reuse` | Retired, conditional | R1; feature retirement must complete first |
| `StubServerShutdownTests.test_close_wakes_a_blocking_listener_and_reaps_its_thread` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerResumeRecoveryTests.test_resume_after_dead_writer_keeps_original_thread_and_history` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerResumeRecoveryTests.test_resume_refusal_with_saved_history_never_starts_a_replacement` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_family_switch_starts_codex_and_keeps_the_existing_staff_slot` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_claude_to_codex_dispatch_reads_the_published_slot_and_extract_first` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_same_family_retry_delivers_the_saved_extract_without_creating_a_second_thread` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_interrupted_dispatch_uses_delivery_evidence_before_submission` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_the_worker_row_is_available_while_its_codex_turn_runs` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_no_pane_codex_switch_ends_only_its_loaded_thread_before_claude_dispatch` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_reports_invalid_records_and_continues` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_ignores_an_unrelated_live_pid_without_a_live_server` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_reports_connection_failures_and_checks_the_next_server` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_reports_a_failed_loaded_thread_read` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_reports_a_damaged_run_and_checks_the_next_run` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_refuses_when_a_loaded_thread_cannot_be_stopped` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_skips_a_changed_source_identity` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_only_active_records_belong_to_the_matching_launcher` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_source_release_anomaly_does_not_block_family_switch` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_switch_without_a_source_server_record_publishes_and_dispatches_the_destination` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_idle_stop_preserves_a_live_retained_server_for_source_release` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_start_uses_staff_worktree_instead_of_caller_or_saved_shell_folder` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_completed_worker_relaunch_publishes_a_distinct_thread_before_work_and_keeps_fixed_owner` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_returned_codex_thread_is_the_staff_id_before_the_first_turn` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_open_publishes_membership_before_the_server_sees_a_thread` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_failed_resume_replaces_the_native_worker_without_losing_queued_work` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_replacement_start_observes_durable_membership` | Retired, conditional | R1; feature retirement must complete first |
| `StaffAdmissionTests.test_held_follow_uses_the_design_holder_without_ambient_membership` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerShownNameTests.test_replacement_thread_and_its_followup_notice_use_the_current_unit_context` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerShownNameTests.test_a_refused_unit_reviewer_follow_keeps_the_existing_thread_title` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerShownNameTests.test_an_accepted_unit_reviewer_follow_uses_the_configured_shown_name` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerShownNameTests.test_help_for_addressable_commands_uses_the_worker_vocabulary` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_strict_roster_readers_refuse_damaged_state_without_rewriting_it` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_cleanup_keeps_the_server_and_reports_an_unreadable_roster` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_presentation_reads_or_marks_unavailable_without_waiting_for_the_roster_lock` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_presentation_marks_unreadable_worker_identity_unavailable` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_repeated_inbox_publications_read_the_current_path` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_inbox_readers_and_writers_refuse_damaged_messages_without_clearing_them` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_resolve_refuses_nonempty_corrupt_pending_without_quarantining_it` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_missing_inbox_stays_absent_until_its_first_complete_publication` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_roster_failure_keeps_existing_workers_and_pending_work_whole` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_concurrent_inbox_appends_keep_every_message_across_replacements` | Retired, conditional | R1; feature retirement must complete first |
| `AtomicWorkerStateTests.test_two_roster_writers_open_current_data_only_after_the_stable_lock` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_interrupted_stop_respects_a_held_staff_lock_and_keeps_the_worker_row` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_server_lock_uses_the_current_operation_deadline_when_omitted` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_connection_and_handshake_share_the_remaining_operation_second` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_initialize_and_two_fragmented_rpcs_spend_one_operation_budget` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_frame_timeout_classification_retains_the_deadline_that_bounded_the_wait` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_legacy_wall_limits_survive_clock_jumps_and_tighter_scopes_bound_existing_clients` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_constructor_closes_its_socket_when_the_handshake_fails` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_constructor_closes_its_socket_when_initialization_cannot_be_sent` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_unscoped_rpc_limits_remain_local_and_a_slow_send_counts_toward_its_call` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_ordinary_rpc_timeouts_keep_each_callers_structured_recovery` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_operation_expiry_escapes_release_and_idle_recovery_fallbacks` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_pause_rpc_timeouts_keep_the_server_and_all_unconfirmed_work` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_sweep_and_idle_stop_keep_servers_after_an_ordinary_socket_timeout` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_account_move_socket_timeout_after_marker_keeps_work_and_publishes_matching_failure` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_lock_acquisitions_use_the_shared_deadline_primitive` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_nonblocking_inbox_refusal_does_not_wait_or_consume_the_shutdown_budget` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_sequential_lock_waits_share_one_monotonic_budget` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_nested_deadlines_restore_after_failure_without_extending_the_operation` | Retired, conditional | R1; feature retirement must complete first |
| `ShutdownDeadlineTests.test_concurrent_shutdown_deadlines_do_not_leak_between_threads` | Retired, conditional | R1; feature retirement must complete first |
| `LifecycleClockTests.test_hang_guard_uses_one_wall_clock_when_lifecycle_time_is_patched` | Retired, conditional | R1; feature retirement must complete first |
| `LifecycleClockTests.test_registry_time_is_excluded_during_resolution_and_after_failure` | Retired, conditional | R1; feature retirement must complete first |
| `LifecycleClockTests.test_readiness_excludes_registry_time_but_still_expires_without_an_event` | Retired, conditional | R1; feature retirement must complete first |
| `LifecycleClockTests.test_result_wait_excludes_registry_time_and_preserves_task_errors` | Retired, conditional | R1; feature retirement must complete first |
| `LifecycleClockTests.test_callback_budget_uses_the_lifecycle_clock_and_retains_its_limits` | Retired, conditional | R1; feature retirement must complete first |
| `FixtureHomeSafetyTests.test_worker_selection_refuses_real_home_descendants_and_symlinks` | Retired, conditional | R1; feature retirement must complete first |
| `FixtureHomeSafetyTests.test_base_setup_rejects_unsafe_environment_before_creating_files` | Retired, conditional | R1; feature retirement must complete first |
| `FixtureHomeSafetyTests.test_metadata_guard_catches_a_bypassed_refusal_in_an_absent_or_existing_tree` | Retired, conditional | R1; feature retirement must complete first |
| `RecordedTurnSettingsTests.test_accepted_turn_settings_take_precedence_over_legacy_roster_fields` | Retired, conditional | R1; feature retirement must complete first |
| `RecordedTurnSettingsTests.test_an_accepted_model_without_effort_does_not_borrow_the_legacy_effort` | Retired, conditional | R1; feature retirement must complete first |
| `RecordedTurnSettingsTests.test_a_missing_or_empty_accepted_model_uses_the_legacy_pair` | Retired, conditional | R1; feature retirement must complete first |
| `RecordedTurnSettingsTests.test_unrecorded_settings_are_empty_and_legacy_fields_remain_supported` | Retired, conditional | R1; feature retirement must complete first |
| `StaffFolderTurnTests.test_follow_up_turn_keeps_the_thread_folder_without_a_cwd_argument` | Retired, conditional | R1; feature retirement must complete first |
| `QueueGraceSettingsTests.test_unset_or_empty_grace_keeps_the_production_window` | Retired, conditional | R1; feature retirement must complete first |
| `QueueGraceSettingsTests.test_grace_reads_each_environment_change` | Retired, conditional | R1; feature retirement must complete first |
| `QueueGraceSettingsTests.test_invalid_grace_is_refused_with_its_configuration_name` | Retired, conditional | R1; feature retirement must complete first |
| `MeshParserTests.test_start_parser_accepts_cwd_and_defaults_to_empty` | Retired, conditional | R1; feature retirement must complete first |
| `MeshParserTests.test_prime_parser_accepts_cwd_and_defaults_to_empty` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fixture_guard_rejects_live_servers_and_watchers_in_its_temporary_folder` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_darwin_fixture_guard_finds_servers_and_watchers_by_their_working_folder` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_darwin_fixture_guard_finds_a_server_home_with_a_checkout_working_folder` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_darwin_fixture_guard_finds_a_watcher_by_its_run_argument_outside_the_fixture_cwd` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_darwin_fixture_guard_distinguishes_an_exited_process_from_unreadable_evidence` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_complete_stub_seed_reuses_its_checkout_server` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_child_inherits_zero_queue_grace_without_waiting_for_a_frame` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_accelerated_reader_preserves_wire_order_around_an_rpc_reply` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_held_turn_with_current_binding_reuses_the_server_without_blocking` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sweep_or_idle_stop_of_a_held_design_server_resumes_same_thread_with_pending_text` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_recovery_saves_the_held_reply_at_its_assignment_path` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_invalid_account_move_header_preserves_the_roster_for_a_later_follow` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_held_instruction_and_turn_claim_has_one_winner_and_uses_current_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_stale_held_assignment_cannot_claim_a_turn_or_replace_instructions` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_held_thread_resume_failure_does_not_start_a_replacement` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_explicit_designer_access_stays_writable_with_standing_instructions` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_from_recovery_marks_lost_thread_ended_before_failed_server_cleanup` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_move_without_a_prior_marker_parks_queued_text_before_interrupting` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_idle_server_release_parks_queue_before_unsubscribe_and_preserves_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_nested_server_selection_releases_held_server_without_reacquiring_its_locks` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_idle_durable_account_move_without_run_marker_parks_queue_before_server_stop` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_durable_account_moves_report_missing_parents_and_continue_all_runs` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_old_launcher_normal_end_preserves_the_replacement_roster` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_old_launcher_exception_preserves_the_replacement_roster` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_old_thread_start_and_token_notices_preserve_every_replacement_field` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compaction_reply_after_replacement_preserves_its_context_and_identity` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compaction_checks_ownership_after_acquiring_the_roster_lock` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_steer_on_the_same_thread_keeps_fields_and_records_the_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_steer_reply_after_replacement_preserves_its_context_and_identity` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_interrupt_selects_a_server_when_its_launcher_dies_between_liveness_checks` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_taken_during_request_releases_only_its_new_thread_and_refuses` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_taken_during_request_reports_unconfirmed_release_without_touching_replacement` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_priming_a_worker_publishes_its_backend_identity_without_dispatching` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_forking_publishes_the_new_identity_without_changing_the_template` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_creates_and_names_a_read_only_review_template_without_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_accepts_omitted_model_and_service_tier` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_reports_request_failures_and_closes_every_client` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_reports_an_unreadable_instructions_file_as_a_named_failure` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_resumes_the_source_before_creating_and_naming_a_read_only_review_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_refuses_a_destination_with_any_nonended_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_replaces_an_ended_destination_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_records_the_absolute_instructions_path` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_uses_the_numbered_worker_home_despite_the_process_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_moved_worker_resume_restores_its_standing_instructions` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_replacement_worker_keeps_standing_instructions_and_read_only_policy` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_and_fork_refuse_a_thread_held_by_a_retired_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_and_fork_stop_unused_retired_writer_before_reopening` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_and_fork_confirm_retired_thread_release_before_reopening` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_held_send_reopens_on_the_worker_home_with_standing_instructions_before_delivery` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_and_fork_refuse_an_unconfirmed_retired_writer` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_each_resume_uses_current_instructions_and_refuses_unreadable_content` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_set_instructions_repoints_an_idle_reviewer_and_its_next_resume_sends_the_new_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_set_instructions_refuses_a_busy_reviewer_and_a_missing_file` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_and_a_follow_claim_between_idle_read_and_revert_have_one_winner` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_refuses_a_worker_with_a_follow_claim_and_preserves_the_launcher` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_releases_its_claim_after_success_with_or_without_history` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_releases_its_claim_after_each_failed_request` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_namespace_uses_the_staff_cwd_for_thread_and_turn_start` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fresh_and_resumed_implementation_turns_disable_approval_prompts` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_cwd_is_parsed_into_the_turn_cwd_destination` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_fork_reports_request_failures_and_closes_every_client` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_reverts_before_the_earliest_turn_and_clears_context_for_followable_states` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_of_a_thread_without_turns_clears_context_without_reverting` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_refuses_nonfollowable_roster_states_without_reverting` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_clears_a_thread_with_no_active_turn_after_a_refusal_or_unload` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_refuses_active_or_unknown_thread_states` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_refuses_a_live_turn_without_erasing_its_context` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_reports_request_failures_without_clearing_context_and_closes_clients` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_cli_sends_worktree_read_only_policy_and_images_after_text_in_order` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_cli_sends_each_policy_and_override_with_the_complete_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_finished_run_wake_uses_the_recorded_task_and_not_the_launch_tier` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_resolves_registry_and_paced_tiers_instead_of_the_launch_tier` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_unknown_task_uses_the_function_tier_and_registry_errors_do_not_stop_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_after_a_read_only_follow_restores_the_write_policy` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_without_read_only_keeps_a_standing_reviewer_read_only` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_held_record_without_policy_names_recovery_and_stays_unchanged` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_worker_without_instructions_keeps_each_launch_sandbox` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_pool_verbs_report_server_failures_with_the_destination_name` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_records_its_server_before_it_reopens_the_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_paused_seat_can_be_claimed_and_followed_on_its_original_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_recovers_an_ended_thread_and_follow_resumes_it` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_on_a_finished_run_names_reactivation_and_a_new_run_as_remedies` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_refuses_missing_thread_or_live_launcher_without_mutation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_recovers_orphaned_active_seats_only_after_the_server_is_gone` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_accepts_ended_seat_without_launcher_and_refuses_live_orphans` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_checks_the_seats_server_port_when_another_server_is_alive` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reopen_refuses_a_live_retired_server_on_the_workers_port` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_reuses_a_done_thread_and_records_its_new_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_overrides_the_model_for_the_reassigned_role` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_accepted_turn_records_display_settings_without_replacing_legacy_identity_fields` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_chooses_current_registry_settings_instead_of_either_roster_model_pair` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_name_taken_during_resume_releases_fallback_without_touching_new_owner` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_name_taken_after_fallback_is_named_releases_it_without_starting_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_name_taken_reports_unconfirmed_fallback_release_and_keeps_new_owner` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_refused_resume_without_replacement_owner_runs_on_the_fallback_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_carries_held_messages_to_a_replacement_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_records_the_assigned_role` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_thread_titles_follow_assignment_at_each_turn_without_renaming_the_worker` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_display_rename_reaches_the_next_thread_title_without_changing_record_identity` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_and_fork_send_shown_titles_and_keep_mesh_addresses` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_prime_held_titles_use_the_design_holder_before_the_mesh_record_exists` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resume_fallback_sets_shown_title_and_retains_assignment` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_appends_usage_without_a_lens` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_cli_records_role_and_lens_only_in_the_usage_log` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_records_each_role_and_lens_assignment_on_the_same_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_cli_accepts_a_review_lens` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reserved_follow_records_one_assignment_with_its_lens` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_probe_without_a_claim_does_not_record_an_assignment` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_usage_write_failure_does_not_stop_a_new_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_reports_unexpected_usage_write_failure_and_continues` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_usage_write_failure_does_not_stop_a_follow_up` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_preclaimed_follow_survives_a_deeply_nested_usage_line` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_preclaimed_follow_reports_unexpected_usage_reader_failure_and_continues` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_claim_replaces_an_interactive_assignment_with_the_claim_role` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_start_without_task_role_keeps_the_staff_role` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_keeps_the_staff_role_and_context_when_the_task_role_changes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_turn_final_usage_records_the_context_size_in_the_roster` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_refuses_a_running_worker` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_zero_compaction_reports_an_unmeasured_size_and_list_keeps_three_fields` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_next_turn_usage_replaces_the_unmeasured_compaction_size` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_late_zero_compaction_keeps_the_newer_turns_complete_record` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_late_measured_compaction_keeps_the_newer_turns_complete_record` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_late_compaction_keeps_a_finished_turn_with_the_same_status_and_size` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_refuses_an_unmeasured_size_with_its_specific_reason` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_without_a_recorded_size_keeps_the_existing_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_each_roster_write_advances_revision_even_when_ended_fields_are_kept` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compaction_reads_a_legacy_entry_as_revision_zero` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_up_without_usage_keeps_the_unmeasured_compaction_size` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_roster_writes_without_size_preserve_the_unmeasured_compaction_state` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_replaces_unmeasured_compaction_state_with_measured_zero` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_compaction_keeps_the_complete_recorded_size` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_roster_writes_without_size_keep_a_measured_size` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_waits_for_compaction_and_records_the_smaller_context` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_accepts_the_current_context_compaction_item` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_waits_for_turn_completion_after_the_compaction_event` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_a_compaction_that_times_out_is_interrupted_so_the_worker_can_follow` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_a_compaction_that_cannot_be_interrupted_exits_with_the_still_live_status` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_waits_for_a_live_turn_on_a_finished_worker_to_end` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compact_refuses_a_finished_worker_whose_turn_stays_live` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_context_overflow_starts_one_continue_turn_and_posts_once` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_thread_read_failure_names_the_serving_account_and_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_reset_turn_list_failure_names_the_serving_account_and_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_client_without_a_binding_uses_a_named_server_state` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_controller_wait_uses_explicit_context_with_absent_or_wrong_environment_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_two_controller_workers_keep_distinct_passes_in_one_phase` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_provider_refusal_wait_ends_when_the_worker_runs_again` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_room_wait_closes_on_dispatch_without_recording_a_quota_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_probe_account_room_wait_writes_no_phase_wait` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_finished_quota_continuation_then_disconnect_sends_no_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_finished_quota_continuation_then_timeout_sends_no_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_finished_quota_continuation_then_capacity_exhaustion_sends_no_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_idle_completed_quota_continuation_without_notification_then_timeout_sends_no_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_submission_retry_continuation_still_live_at_timeout_sends_one_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_quota_continuation_then_completed_held_message_still_sends_one_rerun` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_stream_refusal_waits_for_room_without_handing_off_or_clearing_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_replacement_thread_refusal_waits_for_room_and_keeps_its_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_markerless_room_wait_keeps_its_continuation_without_a_director_handoff` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_old_command_exit_leaves_a_replacement_workers_thread_and_continuation_untouched` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_attaching_to_a_fresh_server_keeps_start_tracking_unchanged` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_started_refusal_keeps_the_account_after_its_server_record_disappears` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_late_refusal_keeps_the_outgoing_account_when_a_port_record_is_reused` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_followed_turn_reports_its_refusing_account_once` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_parked_message_refusal_routes_then_hands_the_exited_command_back` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_thread_start_refusal_reports_the_exited_command_context` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_submission_refusal_reports_its_account_and_keeps_the_continuation_for_room` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_fork_preserves_the_account_without_reporting_a_turn_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_unproven_fast_refusal_does_not_report_or_open_fallback` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_unreadable_serving_sign_in_reports_an_explicit_unknown_account` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_replacement_conversations_first_refusal_names_the_target_account` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_release_refusal_does_not_report_a_provider_turn_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_between_marker_read_and_idle_check_resumes_same_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_change_resumes_live_holder_but_leaves_dead_holders_worker_alone` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_quiet_poll_resumes_a_marked_account_move_after_a_system_error` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_change_finds_ended_retired_only_busy_server_and_names_why_it_stays` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_without_holders_requires_a_live_launcher_and_any_running_holder_keeps_a_run_live` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_change_stops_ended_retired_only_server_without_resuming_its_workers` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_change_interrupts_all_runs_before_a_stalled_account_move_times_out` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_sign_in_change_interrupts_and_resumes_a_waiting_launcher_on_a_fresh_server` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_resume_prefers_the_dispatch_role_and_lens_to_the_role_log` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_account_move_interrupts_the_recorded_server_that_holds_the_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_account_move_refuses_an_unreadable_server_record_without_a_marker` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_account_move_resumes_the_same_thread_after_its_server_dies` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_with_a_nonworker_dispatch_role_refuses_without_a_worker_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_without_staff_id_or_dispatch_refuses_without_a_worker_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_command_keeps_the_dispatch_role_after_a_session_clear` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_without_dispatch_reports_a_named_resume_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_director_account_move_keeps_the_review_lens` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_resumes_running_worker_with_dead_launcher_using_director_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_resumes_running_worker_without_launcher_pid_using_director_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_account_move_command_uses_the_recorded_role_and_quotes_paths` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_reports_interrupt_rejection_without_resuming_worker` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_director_resume_rejection_keeps_assignment_for_the_manual_retry` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_interrupted_turn_without_account_move_intent_keeps_failure_handling` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_automatic_continuation_after_a_role_change_starts_once_on_the_new_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_worker_move_reads_old_request_and_deduplicates_late_old_confirmation` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_destination_submission_refusal_continues_once_after_the_following_move` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_initial_submission_refusal_and_destination_refusal_continue_once_on_final_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_new_worker_on_requested_destination_does_not_confirm_another_workers_move` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resumed_worker_confirms_only_a_request_from_its_actual_source_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resumed_worker_confirms_a_pending_spaced_label_request` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resumed_worker_confirms_a_pending_account_move_among_work_routing_entries` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resumed_worker_confirms_a_legacy_default_source_under_its_numbered_label` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resumed_worker_confirms_a_legacy_default_destination_under_its_numbered_label` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_worker_resume_failure_records_the_current_move_request_and_reason` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_start_on_a_closed_ephemeral_port_fails_before_creating_a_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_refused_worker_without_queued_messages_continues_once_on_new_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_refused_worker_continues_before_held_messages_in_order` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_refused_worker_without_messages_continues_once_after_wait_reset_passes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_with_an_owed_continuation_and_no_room_returns_without_watching_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_unstored_refusal_ends_one_turn_launch_and_keeps_its_continuation_and_messages` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_waits_for_room_without_repeating_its_log_or_ending_its_seat` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_room_wait_after_a_destination_refusal_keeps_and_closes_the_new_connection` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_capacity_retry_hands_its_debt_to_one_quota_continuation_after_room_returns` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_owed_continuation_without_a_refusal_starts_on_its_serving_account_with_room` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_owed_continuation_without_a_refusal_waits_then_starts_when_a_reading_shows_room` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_owed_continuation_on_an_unknown_account_stays_unknown` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_retry_after_a_role_change_starts_once_on_the_new_home` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_checked_initial_turn_waits_for_a_live_thread_then_starts_once` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_controlled_initial_wait_requires_clock_advancement` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_controlled_initial_wait_never_submits_without_the_scripted_transition` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_initial_turn_waits_for_an_event_before_retrying_live_state` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_direct_mesh_sends_hold_the_third_repeat_and_allow_a_human_relay` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_py_and_direct_mesh_share_one_recipient_counter` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_human_interrupt_messages_bypass_an_active_repeat_hold` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_message_for_retired_live_server_is_held_and_steer_stays_on_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_continuation_user_message_does_not_clear_overflow_guard` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_later_overflow_after_progress_gets_one_more_continue_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_refuses_a_running_worker_before_sending` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_refuses_failed_worker_with_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_accepts_failed_worker_only_after_idle_read` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_earlier_completion_cannot_finish_follow_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_earlier_completion_alone_does_not_finish_follow_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_sees_a_peer_turn_end_when_the_thread_read_still_names_the_followed_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_a_launcher_finishes_when_the_thread_is_idle_and_no_completion_notice_came` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_idle_read_that_does_not_list_the_turn_as_ended_does_not_end_it` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_an_idle_thread_with_no_notice_and_no_answer_finishes_as_failed` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_one_turn_launcher_delivers_a_finished_run_result_after_a_system_error` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_delivers_a_finished_run_result_after_a_system_error` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_one_turn_launcher_keeps_a_finished_run_result_while_the_refused_account_has_no_room` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_keeps_a_finished_run_result_while_the_refused_account_has_no_room` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_a_thread_with_a_queued_message_or_a_live_turn_has_not_ended` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_failure_without_turn_id_finishes_as_failed` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_completion_without_turn_id_finishes_as_done` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_watches_peer_turn_queued_during_its_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_followed_turn_remains_failed_after_peer_completes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_claimed_follow_refuses_another_launcher_before_start` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_released_follow_claim_restores_the_prior_role_in_roster_and_usage` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_released_follow_claim_restores_an_absent_role` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_claim_restore_records_prior_role_while_a_peer_turn_remains_running` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_live_turn_after_resume_restores_preclaim_roster_when_peer_finishes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_claim_changed_after_dispatch_fails_without_overwriting_peer` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_recovers_dead_launcher_only_after_thread_is_idle` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_recovers_a_capacity_wait_whose_launcher_died` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_running_roster_without_turn_id_has_no_active_state` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_refuses_running_roster_without_turn_id` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_resumes_its_thread_without_repeating_the_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_structured_capacity_error_resumes_the_same_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_on_completed_notification_resumes_the_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_other_turn_failure_does_not_use_capacity_backoff` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_budget_resets_after_a_completed_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_budget_resets_after_a_peer_turn_completes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_turn_does_not_abandon_a_peers_queued_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_peer_turn_finishes_before_task_resume` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_backoff_waits_for_peer_notices_from_an_earlier_readback` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_peer_idle_readback_recovers_a_missing_completion_notice` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_turn_readback_preserves_a_finished_peers_queued_completion` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_disconnect_after_turn_start_never_repeats_the_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_waits_stop_at_twenty_minutes_and_name_the_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_waiting_send_joins_resume_input_without_opening_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_exhausted_send_refuses_without_opening_a_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_exhaustion_drains_the_server_queue` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_live_turn_found_before_resume_is_streamed_then_task_resumes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_refuses_a_live_thread_without_starting_another` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_interrupts_orphaned_turn_after_launcher_dies` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_continues_after_orphan_interrupt_is_refused` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_reports_orphaned_turn_without_a_recorded_launcher` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_refuses_a_waiting_worker_with_a_live_launcher` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_refuses_live_waiting_launcher_without_server_record` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_dead_waiting_launcher_keeps_held_messages_on_relaunch` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_replaces_waiting_worker_whose_launcher_died` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_relaunches_when_old_thread_has_unknown_status` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_relaunches_when_old_thread_has_system_error` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_interrupts_old_turn_before_starting_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_refuses_relaunch_when_interrupt_errors_and_turn_stays_live` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_relaunches_when_turn_ends_after_interrupt_error` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_refuses_relaunch_when_active_turn_has_no_id` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_worker_without_server_record_starts_on_new_server` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_replaced_waiting_launcher_starts_no_resume_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_refuses_when_thread_read_errors` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_refuses_when_thread_read_times_out` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_continues_when_dead_launchers_thread_read_errors` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_continues_when_dead_launchers_thread_read_times_out` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_interrupts_when_unknown_thread_becomes_readable_during_cleanup` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_continues_after_cleanup_connection_and_log_write_failures` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_relaunch_reports_no_interrupt_when_active_thread_becomes_idle_during_cleanup` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_stop_pause_interrupts_live_work_preserves_queued_messages_and_threads` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_launcher_completion_after_pause_cannot_overwrite_resume_metadata` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_interrupts_real_id_when_read_has_no_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_on_system_error_thread_has_no_turn_to_interrupt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_marks_a_finished_worker_ended_and_keeps_its_metadata` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_a_second_end_retries_cleanup_on_the_ended_workers_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_stops_the_last_workers_server_after_a_thread_read_refusal` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_stops_the_last_workers_server_and_keeps_the_staff_row` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_keeps_an_unrelated_process_and_forgets_its_server_record` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_reports_a_failed_process_scan_without_signaling_the_server` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_keeps_the_server_for_another_open_worker` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_capacity_exhaustion_has_a_distinct_run_outcome` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_turn_outcome_cases_carry_only_their_own_data` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_end_reaches_a_thread_during_capacity_wait` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_after_end_refuses_waiting_worker_and_keeps_pending_empty` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_to_ended_status_without_end_marker_refuses_and_keeps_pending_empty` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_to_worker_missing_from_roster_names_the_known_workers_and_holds_nothing` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_unreadable_thread_after_completion_ends_and_cleans_up` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compaction_after_home_change_releases_old_writer_before_target_resume` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_swap_stops_unused_old_writer_and_next_follow_resumes_on_target` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_compaction_with_unconfirmed_old_writer_posts_one_refusal_and_never_resumes` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_between_last_pending_check_and_exit_starts_one_message_turn` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_new_task_is_the_last_input_after_held_messages` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_after_launcher_closes_inbox_is_held_until_follow_after_its_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_to_paused_worker_is_held_until_follow_after_its_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_to_done_worker_is_held_until_follow_after_its_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_send_with_dead_launcher_is_held_until_follow_after_its_prompt` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_failed_turn_delivers_one_reply_and_ends_without_repeating_failure` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_idle_ticks_skip_move_requests_when_home_and_port_match` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_controlled_resident_ticks_require_clock_advancement` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_lost_new_server_turn_reply_cleans_up_that_server_and_thread` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_resume_failure_retries_on_a_later_counted_tick` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_completed_follow_holds_one_message_and_finishes_done_after_home_change` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_hold_closes_inbox_and_keeps_matching_messages_under_one_lock` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_parent_claim_closes_inbox_and_preserves_matching_messages` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_inbox_closing_helpers_refuse_another_launchers_record` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_completed_start_holds_two_messages_with_plural_wording_and_finishes_done` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_message_arriving_at_inbox_close_is_held_after_unconfirmed_move` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_ended_unseen_answer_holds_parked_message_and_finishes_done` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_ended_unseen_without_answer_keeps_its_failure_while_holding_message` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_failed_follow_holds_messages_and_exits_with_its_own_failure_detail` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_follow_after_hold_submits_prompt_then_held_messages_once_and_empties_inbox` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_accepted_target_turn_pending_write_failure_fails_and_cleans_distinct_target` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_accepted_target_turn_roster_write_failure_fails_and_cleans_distinct_target` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_completed_turn_with_worker_ended_before_move_leaves_pending_untouched_and_reports_no_hold` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_resident_delivered_reply_waits_for_old_writer_and_moves_on_later_tick` | Retired, conditional | R1; feature retirement must complete first |
| `MeshCommandTests.test_move_failure_after_thread_replacement_retains_target_cleanup` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_compaction_reports_a_concurrent_same_thread_roster_change_without_overwriting_it` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_reopen_recovers_an_idle_stuck_worker_on_its_original_thread` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_reopen_leaves_a_stuck_worker_unchanged_while_its_thread_has_a_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_reopen_leaves_an_idle_stuck_worker_unchanged_while_its_launcher_lives` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_reopen_keeps_the_roster_when_the_live_server_cannot_confirm_an_idle_thread` | Retired, conditional | R1; feature retirement must complete first |
| `CompactionRecoveryTests.test_a_finished_follow_subscriber_cannot_turn_compaction_into_running_work` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_follow_waits_for_run_delivery_and_finishes_its_result_turn` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_parent_claim_keeps_the_last_run_result_attached_with_a_child_process` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_roster_lock_guard_rejects_a_nested_read_and_releases_on_close` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_settling_an_owed_quota_continuation_does_not_relock_the_roster` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_compaction_completion_cannot_settle_or_clean_up_a_new_launcher_claim` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_compaction_completion_cannot_settle_or_clean_up_a_paused_worker` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_own_turn_lifecycle_notices_do_not_read_roster_but_foreign_compaction_does` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_submission_wait_preserves_buffered_and_new_peer_notices_without_self_waking` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_idle_readback_rechecks_after_buffered_rate_notice_without_another_timeout` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_lifecycle_lock_order_guard_names_the_inbox_or_roster_and_acquisition_sites` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_recursive_lifecycle_acquisition_names_both_call_sites` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_membership_and_server_lock_order_guard_names_both_acquisition_sites` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_staff_transaction_never_precedes_membership_or_follows_a_backend_lock` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_server_replacement_waits_for_reaping_and_keeps_its_record` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_bulk_pause_revalidates_an_insertion_before_touching_its_work` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_bulk_shutdown_reports_lock_timeout_without_claiming_work_stopped` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_shutdown_shared_roster_read_expires_without_losing_worker_state` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_shutdown_read_timeouts_escape_json_fallbacks_and_preserve_records` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_shutdown_inbox_contention_keeps_work_and_releases_its_other_locks` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_refused_server_kill_keeps_records_and_reports_incomplete_shutdown` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_settlement_owns_cleanup_connection_until_pause_and_reclaim_can_publish` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_claim_before_settlement_preserves_the_new_owners_queue_inbox_and_turn` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_replacement_submission_failure_cleans_only_the_owned_replacement` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_replacement_publication_refusal_closes_only_the_new_client_and_keeps_queued_work` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_reopen_probe_allows_roster_read_and_rejects_a_changed_revision` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_compaction_keeps_its_lifecycle_fence_until_pause_preserves_the_turn_and_queue` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_cleanup_timeout_releases_lifecycle_without_claiming_the_live_turn_stopped` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_dead_launcher_recovery_requires_idle_evidence_on_current_or_resumed_threads` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_invalid_server_metadata_cannot_recover_a_dead_launcher_with_a_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_compact_dead_launcher_names_follow_or_reopen_without_mutating_work` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_due_run_scan_drains_raw_buffered_completion_before_its_next_wait` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_resident_room_retry_does_not_wake_on_its_own_server_utime` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_quota_owed_retry_does_not_wake_on_an_identical_closed_inbox_write` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_trailing_status_notices_do_not_delay_one_turn_exit` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_failed_turn_with_answer_and_trailing_status_is_recorded_failed` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_last_detached_run_result_turn_is_watched_not_interrupted` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_implement_and_assign_parent_claims_keep_the_last_run_result_attached` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_a_result_held_past_the_wall_clock_deadline_opens_the_next_turn` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_missing_completion_keeps_the_inbox_open_for_a_new_detached_run` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_terminal_resident_keeps_the_inbox_open_for_a_new_detached_run` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_follow_blocks_without_periodic_wakes_and_a_marker_wakes_it` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_file_wake_keeps_completion_frames_received_before_the_idle_readback_reply` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_follow_consumes_ten_thousand_deltas_with_bounded_work_and_no_sleeps` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_idle_follow_exits_when_the_worker_roster_becomes_terminal` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_idle_follow_exits_when_the_run_is_finished_without_a_turn_notice` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_resident_follow_exits_when_the_worker_roster_becomes_terminal` | Retired, conditional | R1; feature retirement must complete first |
| `FollowEventRegressionTests.test_resident_follow_exits_when_the_run_is_finished_without_a_turn_notice` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorLifetimeTests.test_run_end_keeps_workers_and_server_when_the_director_stays_on_its_account` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorLifetimeTests.test_run_end_keeps_workers_and_server_after_the_director_changes_accounts` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorLifetimeTests.test_run_end_does_not_reclaim_an_empty_server_before_the_next_directed_dispatch` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_guarded_end_refuses_a_replacement_thread_without_mutating_its_work` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_guarded_end_refuses_a_running_roster_without_mutating_its_work` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_guarded_end_refuses_an_idle_roster_with_a_live_server_turn` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_guarded_end_cancels_the_matching_idle_workers_work` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_retrying_end_finishes_queue_cleanup_after_its_budget_expires` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_idle_end_refuses_a_worker_reserved_for_the_next_dispatch` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_idle_end_refuses_a_worker_with_a_live_detached_command` | Retired, conditional | R1; feature retirement must complete first |
| `GuardedWorkerEndTests.test_end_and_a_launcher_holding_the_inbox_finish_without_deadlock` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorServerReclamationTests.test_idle_stop_reclaims_an_empty_server_even_while_its_director_lives` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorServerReclamationTests.test_retired_server_reclamation_is_not_vetoed_by_a_live_director` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorServerReclamationTests.test_reclamation_keeps_a_loaded_threads_queued_submission` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorServerReclamationTests.test_reclamation_keeps_a_server_when_its_loaded_queue_is_unreadable` | Retired, conditional | R1; feature retirement must complete first |
| `DirectorServerReclamationTests.test_the_second_reclamation_check_keeps_work_queued_after_the_first_check` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_pause_does_not_query_a_server_that_never_loaded_the_worker` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_partial_pause_failure_still_protects_every_detached_commands_checkout` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_failed_ledger_pause_still_protects_a_held_workers_command_checkout` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_pause_parks_server_messages_before_deleting_them_and_preserves_local_work` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_pause_preserves_queues_on_current_and_retired_servers_before_reaping_either` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerPauseTests.test_pause_reaps_only_its_recorded_servers_and_keeps_another_runs_server` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_adopting_a_paused_worker_carries_queued_work_before_the_new_assignment_once` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_a_paused_worker_keeps_its_live_detached_record_in_the_giving_run` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_opening_a_unit_worker_passes_the_configured_service_tier_to_the_server` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_new_seat_uses_the_requested_cwd_instead_of_the_staff_folder` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_bulk_stop_waits_for_an_unpublished_creator_before_reaping_its_server` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_new_review_thread_has_its_configured_shown_name_before_any_turn` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_admission_after_bulk_validation_waits_until_empty_run_cleanup_finishes` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_adoption_into_a_run_waits_until_its_bulk_cleanup_finishes` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_a_new_thread_is_released_if_naming_or_recording_its_worker_fails` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_adoption_publishes_the_existing_native_thread_before_destination_success` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_idle_adoption_keeps_the_thread_and_refuses_follow_from_the_old_run` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_held_worker_adoption_refuses_live_activity_without_losing_work` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_adopted_thread_resumes_through_a_symlink_and_records_its_current_account_home` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_completed_adoption_checks_live_turns_and_can_replace_a_later_lost_rollout` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_two_racing_adoptions_deliver_queued_work_to_exactly_one_owner` | Retired, conditional | R1; feature retirement must complete first |
| `WorkerAdoptionTests.test_two_racing_adoptions_leave_exactly_one_destination_owner` | Retired, conditional | R1; feature retirement must complete first |
| `DetachedRunWatchTests.test_three_thousand_finished_records_are_read_once_and_live_runs_refresh_at_sixty_seconds` | Retired, conditional | R1; feature retirement must complete first |
| `DetachedRunWatchTests.test_lifecycle_decisions_discover_a_run_created_after_the_last_scan` | Retired, conditional | R1; feature retirement must complete first |
| `DetachedRunWatchTests.test_a_supervisor_that_dies_is_reaped_and_its_result_is_held_by_the_next_sixty_second_scan` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_fast_failure_on_an_inherited_server_is_retried` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_failure_on_a_server_this_call_started_is_final` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_failure_with_a_thread_is_never_retried` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_sign_in_the_inherited_server_no_longer_holds_is_retried_with_a_thread` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_stale_sign_in_on_a_fresh_server_or_after_work_is_final` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_start_retries_a_stale_sign_in_on_a_new_server` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_slow_failure_reached_the_provider` | Retired, conditional | R1; feature retirement must complete first |
| `RetryDecisionTests.test_a_resident_worker_is_never_retried` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_a_refusal_from_a_server_this_launcher_started_is_the_providers` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_a_fast_refusal_from_an_inherited_server_proves_nothing` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_a_refusal_after_real_work_reached_the_provider` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_other_failures_and_capacity_are_not_quota` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_refusal_handoff_preserves_named_window_or_unknown_and_observation_time` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_exited_refusal_handoff_prefers_marker_then_launcher_and_clears_only_its_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_a_stale_quota_report_preserves_a_new_claim_or_released_worker` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_before_thread_exit_uses_its_director_without_rewriting_a_roster` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_waiting_refusal_handoff_does_not_resolve_a_director_or_clear_continuation` | Retired, conditional | R1; feature retirement must complete first |
| `QuotaRefusalTests.test_the_report_runs_the_blocked_verb_and_survives_its_failure` | Retired, conditional | R1; feature retirement must complete first |
| `ThreadStartTests.test_a_registry_tier_is_sent_in_the_config_spelling` | Retired, conditional | R1; feature retirement must complete first |
| `ThreadStartTests.test_no_tier_leaves_the_codex_config_in_charge` | Retired, conditional | R1; feature retirement must complete first |
| `ReplyDeliveryTests.test_without_a_reply_file_the_summary_receives_the_answer` | Retired, conditional | R1; feature retirement must complete first |
| `ReplyDeliveryTests.test_the_workers_own_summary_survives_its_last_reply` | Retired, conditional | R1; feature retirement must complete first |
| `ReplyDeliveryTests.test_a_summary_the_worker_never_wrote_is_filled_from_the_reply` | Retired, conditional | R1; feature retirement must complete first |
| `ReplyDeliveryTests.test_resident_replies_accumulate_in_order` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_server_listing_parses_all_elapsed_forms_and_excludes_non_websocket_targets` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_bundled_codex_executable_cannot_hide_behind_plain_argument_zero_on_either_platform` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_unread_executable_path_keeps_a_websocket_server_instead_of_judging_it_unused` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_connections_count_only_the_local_endpoint_for_ipv4_and_ipv6` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_stderr_distinguishes_a_mesh_log_a_non_mesh_log_and_an_unread_handle` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_empty_mac_stderr_selection_requires_positive_evidence_that_the_pid_is_ours` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_empty_lsof_selection_means_zero_clients_but_missing_denied_and_other_exits_refuse` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_failed_mac_ps_or_connection_listing_keeps_sweep_and_idle_stop_from_signalling` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_sweep_and_idle_stop_apply_the_same_idle_and_connected_server_verdicts` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_unread_mac_stderr_keeps_and_names_the_server_in_sweep_and_idle_stop` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_signin_change_retires_an_unread_server_and_moves_live_workers_before_keeping_it` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_signin_change_stops_an_idle_ended_server_through_the_same_second_look` | Retired, conditional | R1; feature retirement must complete first |
| `MacCleanupTests.test_mac_signin_change_keeps_the_record_and_names_a_failed_start_time_command` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_sweep_lists_and_stops_only_signed_out_shared_home_daemons_and_their_descendants` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_daemon_is_kept_when_shared_home_auth_exists_or_its_absence_cannot_be_confirmed` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_unread_codex_invocation_is_reported_without_being_counted_as_an_app_server` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_daemon_report_names_its_own_numbered_home` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_sweep_discovers_reports_and_resumes_a_stopped_shared_home_non_daemon` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_linux_daemon_discovery_reads_cmdline_and_uses_the_same_home_and_login_verdict` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_daemon_discovery_excludes_exec_prompts_and_accepts_real_subcommands_after_config_flags` | Retired, conditional | R1; feature retirement must complete first |
| `CodexDaemonCleanupTests.test_readable_codex_exec_record_is_unclassified_even_when_its_prompt_names_a_daemon` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonProcessStateTests.test_session_reader_uses_the_kernel_id_and_tags_unreadable_sessions_on_both_platforms` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonProcessStateTests.test_own_user_listing_preserves_the_complete_command_path_and_arguments_with_spaces` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonProcessStateTests.test_mac_process_state_distinguishes_stopped_running_zombie_and_gone` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonProcessStateTests.test_linux_process_state_reads_state_after_the_complete_parenthesized_command` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonProcessStateTests.test_denied_process_state_refuses_instead_of_claiming_a_process_is_gone` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_daemon_freeze_and_exit_confirmation_back_off_in_the_monotonic_clock` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_signed_out_family_is_frozen_parent_first_and_killed_children_first` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_sweep_reports_a_kept_daemons_stopped_shell_child_and_resumes_only_on_stop` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_sweep_keeps_and_names_stopped_shell_members_with_unsafe_recovery_evidence` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_sweep_preserves_job_control_stops_and_recovers_only_detached_members` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_session_candidate_exit_is_skipped_but_unreadable_session_refuses_and_resumes_the_family` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_kept_family_recovery_names_the_cleanup_holding_its_lock` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_session_orphan_uses_its_table_session_without_a_second_racy_read` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_reparented_member_remains_tracked_until_frozen_and_killed` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_reparented_member_must_be_gone_before_shutdown_reports_success` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_stop_signal_is_confirmed_by_process_state_before_any_member_is_killed` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_member_exiting_before_freeze_resumes_the_family_and_reports_unconfirmed_absence` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_child_born_while_freezing_is_discovered_on_the_next_pass_before_any_kill` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_unseen_intermediates_orphan_is_found_in_a_frozen_leaders_session_before_any_kill` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_survivor_in_a_swept_session_refuses_success_and_names_its_pid` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_excluded_session_orphan_is_never_stopped_and_the_frozen_family_is_resumed` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_a_session_with_an_untracked_leader_is_never_swept` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_overlapping_daemon_stops_hold_one_lock_until_the_first_family_is_released` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_session_sweep_never_reads_or_signals_another_users_process` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_snapshot_reads_full_identities_only_for_family_and_swept_session_candidates` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_kept_family_resumes_and_reports_a_shared_home_member_found_already_stopped` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_excluded_member_found_after_freeze_resumes_every_stopped_member_without_killing` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_identity_unread_after_stop_still_resumes_the_member_by_pid_and_start` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_unconfirmed_start_after_stop_keeps_the_member_stopped_and_names_it` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_exclusion_after_freezing_keeps_a_pre_stopped_member_on_another_home_stopped` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_failed_child_stop_resumes_the_parent_without_killing_any_member` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_failed_frozen_child_kill_resumes_every_surviving_member` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_unrelated_snapshot_processes_do_not_prevent_verified_family_shutdown` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_bundled_descendant_with_plain_argument_zero_is_never_signalled` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_changed_child_identity_is_never_signalled_and_parent_is_resumed` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_pid_reused_between_ancestry_listing_and_identity_read_is_not_a_trusted_child` | Retired, conditional | R1; feature retirement must complete first |
| `DaemonFamilySafetyTests.test_changed_parent_or_returned_login_never_kills_the_family` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_held_design_server_waits_for_the_quiet_window_before_sweep_stops_it` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_held_design_server_keeps_its_startup_quiet_window_with_old_activity` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_held_design_server_with_a_live_turn_or_client_survives_sweep` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_design_mesh_without_a_held_owner_keeps_unowned_sweep_behavior` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_run_server_keeps_its_quiet_window_with_durable_design_meshes_present` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_only_a_server_cleared_by_every_check_is_unused` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_retired_server_is_judged_only_on_its_clients_and_conversations` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_an_old_unowned_server_is_unused_only_when_its_conversations_are_idle` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_dev_shell_startup_budget_does_not_extend_the_unowned_sweep_grace` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_current_server_without_a_marker_must_clear_every_live_and_idle_signal` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_server_discovery_accepts_the_actual_zero_unload_delay_launch_arguments` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_servers_and_connections_are_read_from_ps_and_ss` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_process_text_that_only_mentions_the_server_command_is_not_a_target` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_unix_daemon_and_app_server_helper_are_never_sweep_targets` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_the_run_folder_comes_from_the_servers_own_log_handle` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_judgment_preserves_the_explicit_unowned_without_log_state` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_run_folder_reports_its_marker_its_quiet_time_and_its_live_launchers` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_the_server_is_asked_for_every_conversation_it_holds` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_server_that_cannot_be_asked_in_full_reads_as_busy` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_the_sweep_fails_closed_when_it_cannot_look` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_the_report_lists_each_server_and_stops_nothing` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_stop_reaches_each_old_unowned_server` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_unowned_server_is_reaped_only_after_the_second_look` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_sweep_preserves_paused_threads_but_plain_stop_ends_them` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_recorded_server_stops_when_ended_and_stays_for_each_live_signal` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_server_is_stopped_only_when_the_second_look_agrees` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_server_stops_itself_only_when_nothing_uses_it` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_server_judging_itself_ignores_the_run_active_marker` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_server_the_run_retired_is_read_as_retired` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_any_live_current_record_protects_a_server_from_stale_duplicate_owners` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_a_retired_owner_wins_over_an_ended_stale_current_record` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_stopping_a_retired_server_drops_it_from_the_retired_list` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_live_launcher_holds_send_locally_and_steer_reaches_outgoing_server` | Retired, conditional | R1; feature retirement must complete first |
| `SweepTests.test_message_without_live_launcher_is_held_without_contacting_a_server` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_the_time_a_server_has_run_is_read_from_ps` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_the_sign_in_file_is_only_ever_looked_at_never_read` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_the_holder_is_told_by_its_session_id` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_each_run_on_an_older_server_moves_and_its_holder_is_told` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_a_holder_that_ends_while_earlier_runs_move_leaves_its_run_ended` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_an_idle_run_has_its_old_server_stopped_at_once` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_live_run_with_only_retired_old_sign_in_is_not_moved_or_notified` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_a_resident_worker_moves_to_the_new_server_between_turns` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_a_resident_worker_mid_turn_stays_until_its_turn_ends` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_unreadable_thread_state_never_starts_a_replacement_turn` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_refused_cross_home_resume_creates_a_new_thread_and_says_so` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_home_migration_confirms_old_writer_release_before_resuming` | Retired, conditional | R1; feature retirement must complete first |
| `SignInChangeTests.test_unconfirmed_old_writer_release_starts_no_thread_or_turn_on_target` | Retired, conditional | R1; feature retirement must complete first |
| `LauncherSessionSwapTests.test_sign_in_holderless_launcher_finds_nearest_session_with_matching_process_start` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_mesh_server_child_and_record_follow_role_with_conflicting_caller_home` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_server_uses_the_configured_agent_scope` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_server_starts_without_a_scope_when_the_probe_fails_or_is_absent` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_app_server_environment_excludes_the_launch_and_editor_configuration` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_a_run_folders_server_names_its_run_whatever_the_caller_carries` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_a_server_outside_the_run_roots_keeps_the_callers_environment` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_home_change_retires_server_even_when_sign_in_timestamps_are_unchanged` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_legacy_server_is_retired_when_role_moves_to_numbered_home_without_stopping_live_turn` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_legacy_server_is_kept_when_role_stays_default_and_sign_in_is_current` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_server_started_without_the_compaction_limit_is_retired_without_stopping_live_turns` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_send_keeps_legacy_default_server_and_holds_message_for_live_launcher` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_swap_reads_legacy_source_as_default_home` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_unsubscribe_not_loaded_proves_release_without_another_state_read` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_unsubscribe_not_subscribed_does_not_prove_a_loaded_thread_released` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_matching_home_sign_in_check_uses_recorded_home_instead_of_caller` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_sign_in_notice_retires_only_servers_on_the_changed_home` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_swap_retires_all_temp_runs_and_notifies_each_director_once` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_swap_stops_unused_old_server_immediately_without_starting_a_watcher` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_ensure_server_stops_an_unused_retired_server_before_starting_target` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_dead_retired_writer_counts_as_released_even_when_the_connection_fails` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_dead_record_on_reused_port_does_not_confirm_live_server_is_gone` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_live_writer_on_reused_port_is_probed_despite_an_earlier_dead_record` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_live_record_on_reused_port_keeps_failed_probe_unconfirmed` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_loaded_idle_thread_on_reused_port_waits_for_live_writer_release` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_live_peer_on_retired_server_names_run_and_worker_until_turn_ends` | Retired, conditional | R1; feature retirement must complete first |
| `WorkersHomeTests.test_sign_in_notices_use_the_shared_user_zone` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_loaded_worktree_launches_codex_directly_and_records_its_pid` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_run_owned_values_are_reapplied_after_direnv_without_overwriting_dev_shell_flags` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_no_envrc_keeps_the_existing_launch` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_shared_mesh_does_not_load_a_callers_worktree` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_caller_without_cwd_starts_the_existing_plain_server` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_legacy_and_other_worktree_servers_are_retired_before_the_new_shell_is_started` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_the_same_dev_shell_reuses_the_server` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_no_cwd_never_retires_a_dev_shell_server` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_dev_shell_server_can_start_after_thirty_seconds` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_plain_start_still_times_out_after_thirty_seconds` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_mac_sweep_keeps_a_slow_dev_shell_server_before_its_current_record_is_written` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_failed_dev_shell_server_falls_back_and_logs_the_failure` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_codex_missing_from_loaded_path_falls_back` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_cwd_caller_reuses_the_plain_fallback_for_an_unchanged_failed_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_editing_a_nix_input_retries_the_failed_dev_shell_without_editing_envrc` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_creating_a_previously_absent_nix_input_retries_the_failed_dev_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_removing_a_nix_input_retries_the_failed_dev_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_failure_older_than_thirty_minutes_allows_an_unchanged_dev_shell_to_retry` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_failed_shell_is_not_retried_after_the_live_server_record_is_removed` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_editing_the_failed_envrc_allows_a_new_dev_shell_attempt` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_another_worktree_can_load_even_when_its_envrc_has_the_failed_files_mtime` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_failed_plain_fallback_is_reported_without_retrying_forever` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_definite_absence_of_an_envrc_retires_a_dev_shell_server` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_a_subdirectory_starts_and_reuses_its_nearest_ancestor_dev_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_failed_start_terminates_the_server_process_group_and_escalates` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_held_design_identity_is_also_applied_after_the_dev_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_start_forwards_the_worktree_on_the_initial_launch_and_recovery` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_no_envrc_result_uses_the_caller_environment_and_records_no_shell` | Retired, conditional | R1; feature retirement must complete first |
| `DevShellServerTests.test_refused_shell_logs_its_line_once_and_reuses_the_fallback` | Retired, conditional | R1; feature retirement must complete first |
| `LegacyRosterTests.test_follow_and_write_upgrade_the_worker_reference` | Retired, conditional | R1; feature retirement must complete first |
| `LegacyRosterTests.test_an_unmatched_worker_keeps_the_missing_thread_result` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_pid_alive_rejects_a_zombie_stat` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_pid_alive_rejects_nonpositive_pids_without_an_os_probe` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_retired_child_app_server_is_reaped` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_reap_collects_a_child_when_zombie_state_precedes_waitable_exit` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_loaded_threads_reads_current_and_retired_pages_under_one_deadline` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_loaded_threads_never_returns_partial_results_or_connects_after_deadline` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_failed_start_signals_only_real_process_ids_above_one` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_reap_uses_configured_probes_and_escalates_only_for_a_live_server` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_live_record_is_attached_to_rather_than_replaced` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_using_a_live_server_marks_its_record_as_used` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_replacing_a_server_record_retires_the_old_live_server` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_start_record_survives_a_folder_fsync_failure_before_current_server_publication` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_confirmed_retained_start_is_adopted_without_starting_another_child` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_server_that_never_accepts_is_stopped_before_start_fails` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_server_is_stopped_when_recording_its_start_fails` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_failed_start_after_publication_preserves_the_retained_server_for_source_release` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_repeated_retirement_keeps_one_watcher_until_that_watcher_exits` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_the_watcher_asks_only_about_a_record_gone_untouched_and_then_leaves` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_the_watcher_judges_a_retired_server_at_every_wake` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_server_holding_an_old_sign_in_is_replaced_at_the_next_launch` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_server_started_after_the_sign_in_is_kept` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_the_watcher_leaves_with_its_server` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_worker_keeps_its_server_port_until_its_launcher_lets_go` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_worker_with_no_recorded_port_is_reached_on_the_retired_server_holding_it` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_worker_no_running_server_holds_fails_without_starting_one` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_retiring_drops_the_record_and_keeps_the_pid_for_stop` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_a_peer_that_already_replaced_the_server_is_left_alone` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_stop_reaps_the_retired_server_as_well_as_the_live_one` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_stop_without_live_server_still_ends_roster_and_cleans_retired_records` | Retired, conditional | R1; feature retirement must complete first |
| `ServerRecordTests.test_pause_refuses_promptly_and_keeps_a_server_whose_queue_cannot_be_preserved` | Retired, conditional | R1; feature retirement must complete first |
| `RunFolderNameSweepTests.test_naming_one_real_root_sweeps_both` | Retired, conditional | R1; feature retirement must complete first |
| `RunFolderNameSweepTests.test_a_marked_run_under_the_former_name_owns_its_server` | Retired, conditional | R1; feature retirement must complete first |

### production/test_add_showrunner.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G001: Table

Proposed replacement: `AddShowrunnerTests.test_dead_saved_launch_resumes_its_transcript_with_the_resume_prompt_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AddShowrunnerTests.test_dead_saved_launch_resumes_its_transcript_with_the_resume_prompt` | Table | G001 proposed table retains this complete case |
| `AddShowrunnerTests.test_dead_saved_launch_without_a_transcript_starts_its_reserved_session` | Table | G001 proposed table retains this complete case |

#### G002: Table

Proposed replacement: `AddShowrunnerTests.test_open_showrunner_recovers_before_its_timer_registers_without_second_launch_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AddShowrunnerTests.test_open_showrunner_recovers_before_its_timer_registers_without_second_launch` | Table | G002 proposed table retains this complete case |
| `AddShowrunnerTests.test_open_showrunner_submits_its_owed_prompt_without_second_launch` | Table | G002 proposed table retains this complete case |

#### G003: Table

Proposed replacement: `AddShowrunnerTests.test_observed_native_launch_recovers_before_staff_publication_without_a_second_backend_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AddShowrunnerTests.test_observed_native_launch_recovers_before_staff_publication_without_a_second_backend` | Table | G003 proposed table retains this complete case |
| `AddShowrunnerTests.test_observed_native_launch_recovers_after_staff_publication_before_its_opening_work` | Table | G003 proposed table retains this complete case |

### production/test_dailies_input.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G004: Table

Proposed replacement: `DailiesInputTests.test_a_finished_unit_that_shows_a_form_keeps_its_place_and_needs_its_entry_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DailiesInputTests.test_a_finished_unit_that_shows_a_form_keeps_its_place_and_needs_its_entry` | Table | G004 proposed table retains this complete case |
| `DailiesInputTests.test_a_finished_unit_whose_question_was_shown_before_keeps_its_place` | Table | G004 proposed table retains this complete case |

#### G005: Table

Proposed replacement: `DailiesInputTests.test_a_supplied_holder_mark_is_refused_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DailiesInputTests.test_a_supplied_holder_mark_is_refused` | Table | G005 proposed table retains this complete case |
| `DailiesInputTests.test_build_hold_marker_without_holder_file_is_refused` | Table | G005 proposed table retains this complete case |

#### G006: Table

Proposed replacement: `DailiesInputTests.test_unsent_handoff_age_in_the_first_fold_of_the_repeated_hour_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DailiesInputTests.test_unsent_handoff_age_in_the_first_fold_of_the_repeated_hour` | Table | G006 proposed table retains this complete case |
| `DailiesInputTests.test_unsent_handoff_age_in_the_second_fold_of_the_repeated_hour` | Table | G006 proposed table retains this complete case |

### production/test_dailies_render.py

Behavior owner: owner unknown.

The cases check different time boundaries and upcoming report states. Each row keeps its time, refusal result, and expected report fields.

The past, present, and future footer cases are behavior boundaries.
Their words do not make them Wording candidates.

#### G007: Table

Proposed replacement: `ReportFooterTests.test_one_minute_is_singular_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ReportFooterTests.test_one_minute_is_singular` | Table | G007 proposed table retains this complete case |
| `ReportFooterTests.test_at_the_time_counts_zero` | Table | G007 proposed table retains this complete case |
| `ReportFooterTests.test_one_minute_overdue_is_singular` | Table | G007 proposed table retains this complete case |

#### G008: Table

Proposed replacement: `UpcomingWorkTests.test_item_that_mentions_another_phase_without_a_head_is_allowed_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UpcomingWorkTests.test_item_that_mentions_another_phase_without_a_head_is_allowed` | Table | G008 proposed table retains this complete case |
| `UpcomingWorkTests.test_then_without_another_phase_is_allowed_in_an_item` | Table | G008 proposed table retains this complete case |

#### G009: Table

Proposed replacement: `UpcomingWorkTests.test_each_item_checks_phase_order_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UpcomingWorkTests.test_each_item_checks_phase_order` | Table | G009 proposed table retains this complete case |
| `UpcomingWorkTests.test_each_item_checks_user_facing_words` | Table | G009 proposed table retains this complete case |

### production/test_dailies_render_agents.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G010: Table

Proposed replacement: `DailiesAgentsTests.test_repeated_hour_run_out_shows_pst_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DailiesAgentsTests.test_repeated_hour_run_out_shows_pst` | Table | G010 proposed table retains this complete case |
| `DailiesAgentsTests.test_run_out_rounds_across_fall_back` | Table | G010 proposed table retains this complete case |

### production/test_fast_track.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G011: Table

Proposed replacement: `FastTrackTests.test_unrelated_stuck_restore_does_not_block_a_new_fix_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FastTrackTests.test_unrelated_stuck_restore_does_not_block_a_new_fix` | Table | G011 proposed table retains this complete case |
| `FastTrackTests.test_unrelated_missing_production_does_not_block_a_new_fix` | Table | G011 proposed table retains this complete case |

#### G012: Table

Proposed replacement: `FastTrackTests.test_rerun_finishes_restore_after_the_review_record_was_saved_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FastTrackTests.test_rerun_finishes_restore_after_the_review_record_was_saved` | Table | G012 proposed table retains this complete case |
| `FastTrackTests.test_rerun_finishes_only_the_unrestored_paths_after_a_partial_restore` | Table | G012 proposed table retains this complete case |

### production/test_mac_run.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G013: Table

Proposed replacement: `MacRunTests.test_successful_clone_cleanup_removes_both_registry_records_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MacRunTests.test_successful_clone_cleanup_removes_both_registry_records` | Table | G013 proposed table retains this complete case |
| `MacRunTests.test_failed_clone_cleanup_keeps_both_registry_records` | Table | G013 proposed table retains this complete case |

### production/test_merge_checkpoint_bump.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G014: Table

Proposed replacement: `CheckpointBumpTests.test_a_resumed_red_gate_notifies_current_sessions_after_a_production_rename_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `CheckpointBumpTests.test_a_resumed_red_gate_notifies_current_sessions_after_a_production_rename` | Table | G014 proposed table retains this complete case |
| `CheckpointBumpTests.test_a_resumed_red_gate_notifies_current_sessions_after_a_unit_rename` | Table | G014 proposed table retains this complete case |
| `CheckpointBumpTests.test_a_resumed_green_gate_notifies_the_showrunner_after_a_unit_rename` | Table | G014 proposed table retains this complete case |

### production/test_merge_checkpoint_handoff.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G015: Table

Proposed replacement: `MergeCheckpointTests.test_fresh_child_push_conflict_reports_held_without_completion_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_fresh_child_push_conflict_reports_held_without_completion` | Table | G015 proposed table retains this complete case |
| `MergeCheckpointTests.test_pushed_child_awaiting_promotion_reports_main_conflict_as_held` | Table | G015 proposed table retains this complete case |

### production/test_merge_checkpoint_promotion.py

Behavior owner: owner unknown.

The cases vary promotion failures and recovery states. Each row keeps its result, state, and publication assertions.

#### G016: Table

Proposed replacement: `MergeCheckpointTests.test_completed_handoff_without_review_keeps_its_tip_when_main_advances_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_completed_handoff_without_review_keeps_its_tip_when_main_advances` | Table | G016 proposed table retains this complete case |
| `MergeCheckpointTests.test_completed_handoff_with_review_keeps_its_tip_when_main_advances` | Table | G016 proposed table retains this complete case |

#### G017: Table

Proposed replacement: `MergeCheckpointTests.test_local_promotion_failure_keeps_publication_records_without_duplicates_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_local_promotion_failure_keeps_publication_records_without_duplicates` | Table | G017 proposed table retains this complete case |
| `MergeCheckpointTests.test_mac_pull_failure_keeps_publication_records_without_duplicates` | Table | G017 proposed table retains this complete case |
| `MergeCheckpointTests.test_handoff_local_promotion_failure_keeps_publication_records_without_duplicates` | Table | G017 proposed table retains this complete case |
| `MergeCheckpointTests.test_handoff_mac_pull_failure_keeps_publication_records_without_duplicates` | Table | G017 proposed table retains this complete case |

#### G018: Table

Proposed replacement: `MergeCheckpointTests.test_launcher_failure_notices_wait_for_promotion_and_push_lock_release_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_launcher_failure_notices_wait_for_promotion_and_push_lock_release` | Table | G018 proposed table retains this complete case |
| `MergeCheckpointTests.test_completed_gate_retry_notices_wait_for_promotion_and_push_lock_release` | Table | G018 proposed table retains this complete case |

#### G019: Table

Proposed replacement: `MergeCheckpointTests.test_local_promotion_failure_still_runs_and_reports_the_bump_gate_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_local_promotion_failure_still_runs_and_reports_the_bump_gate` | Table | G019 proposed table retains this complete case |
| `MergeCheckpointTests.test_mac_promotion_failure_still_runs_and_reports_the_bump_gate` | Table | G019 proposed table retains this complete case |

#### G020: Table

Proposed replacement: `MergeCheckpointTests.test_checkpoint_releases_main_push_lock_before_notices_and_mac_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_checkpoint_releases_main_push_lock_before_notices_and_mac` | Table | G020 proposed table retains this complete case |
| `MergeCheckpointTests.test_handoff_releases_main_push_lock_before_notices_and_mac` | Table | G020 proposed table retains this complete case |

#### G021: Table

Proposed replacement: `MergeCheckpointTests.test_checkpoint_retry_integrates_main_fix_before_finishing_promotion_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeCheckpointTests.test_checkpoint_retry_integrates_main_fix_before_finishing_promotion` | Table | G021 proposed table retains this complete case |
| `MergeCheckpointTests.test_handoff_retry_integrates_main_fix_before_finishing_promotion` | Table | G021 proposed table retains this complete case |

### production/test_merge_checkpoint_rerun.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G022: Table

Proposed replacement: `CheckpointRerunTests.test_review_rows_recover_after_merge_before_tests_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `CheckpointRerunTests.test_review_rows_recover_after_merge_before_tests` | Table | G022 proposed table retains this complete case |
| `CheckpointRerunTests.test_review_rows_recover_after_push_before_recording` | Table | G022 proposed table retains this complete case |

#### G023: Table

Proposed replacement: `CheckpointRerunTests.test_handoff_killed_after_push_retries_promotion_before_acknowledgment_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `CheckpointRerunTests.test_handoff_killed_after_push_retries_promotion_before_acknowledgment` | Table | G023 proposed table retains this complete case |
| `CheckpointRerunTests.test_handoff_failed_promotion_retries_before_acknowledgment` | Table | G023 proposed table retains this complete case |

### production/test_merge_gate_promotion.py

Behavior owner: devtools-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G024: Table

Proposed replacement: `MergeGatePromotionTests.test_mac_missing_ff_rc_is_failure_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeGatePromotionTests.test_mac_missing_ff_rc_is_failure` | Table | G024 proposed table retains this complete case |
| `MergeGatePromotionTests.test_mac_conflicting_ff_rc_lines_are_failure` | Table | G024 proposed table retains this complete case |

### production/test_merge_gate_suites_signals.py

Behavior owner: devtools-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G025: Table

Proposed replacement: `MergeGateSignalsTests.test_failed_gate_removes_its_folder_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MergeGateSignalsTests.test_failed_gate_removes_its_folder` | Table | G025 proposed table retains this complete case |
| `MergeGateSignalsTests.test_sigterm_during_folder_removal_leaves_no_folder` | Table | G025 proposed table retains this complete case |
| `MergeGateSignalsTests.test_sighup_during_folder_removal_leaves_no_folder` | Table | G025 proposed table retains this complete case |
| `MergeGateSignalsTests.test_sigint_during_folder_removal_leaves_no_folder` | Table | G025 proposed table retains this complete case |

### production/test_production_lifecycle.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G026: Table

Proposed replacement: `LifecycleTests.test_wrap_rerun_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `LifecycleTests.test_wrap_rerun` | Table | G026 proposed table retains this complete case |
| `LifecycleTests.test_wrap_rerun_after_showrunner_retirement_keeps_the_release_capture` | Table | G026 proposed table retains this complete case |

#### G027: Table

Proposed replacement: `LifecycleTests.test_wrap_release_retry_does_not_repeat_a_persisted_notice_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `LifecycleTests.test_wrap_release_retry_does_not_repeat_a_persisted_notice` | Table | G027 proposed table retains this complete case |
| `LifecycleTests.test_wrap_release_keeps_failed_delivery_out_of_the_capture` | Table | G027 proposed table retains this complete case |

### production/test_roles_audit.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G028: Table

Proposed replacement: `NameTests.test_retired_shell_output_excludes_comments_and_assignments_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `NameTests.test_retired_shell_output_excludes_comments_and_assignments` | Table | G028 proposed table retains this complete case |
| `NameTests.test_retired_shell_output_heredocs_exclude_input_body_commands` | Table | G028 proposed table retains this complete case |

### production/test_smoke_launch.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G029: Table

Proposed replacement: `SmokeLaunchTests.test_both_cargo_calls_use_the_checkout_from_an_external_caller_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `SmokeLaunchTests.test_both_cargo_calls_use_the_checkout_from_an_external_caller` | Table | G029 proposed table retains this complete case |
| `SmokeLaunchTests.test_relative_checkout_uses_absolute_paths_for_both_cargo_calls` | Table | G029 proposed table retains this complete case |

### production/test_staff.py

Behavior owner: owner unknown.

The cases check different inherited descriptor states. Each row keeps its descriptor condition and its ownership assertions.

#### G030: Table

Proposed replacement: `StaffTests.test_operation_rejects_an_inherited_descriptor_for_another_file_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `StaffTests.test_operation_rejects_an_inherited_descriptor_for_another_file` | Table | G030 proposed table retains this complete case |
| `StaffTests.test_operation_rejects_an_unlocked_inherited_descriptor` | Table | G030 proposed table retains this complete case |

### production/test_staff_test_support.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G031: Table

Proposed replacement: `FixtureAccountTests.test_late_environment_and_explicit_codex_home_redirect_before_worker_publication_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FixtureAccountTests.test_late_environment_and_explicit_codex_home_redirect_before_worker_publication` | Table | G031 proposed table retains this complete case |
| `FixtureAccountTests.test_readers_and_child_process_find_the_parent_worker_in_the_private_folder` | Table | G031 proposed table retains this complete case |

### production/test_stall_watch.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G032: Table

Proposed replacement: `StallWatchTests.test_gate_turn_end_waits_until_a_later_holding_status_stalls_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `StallWatchTests.test_gate_turn_end_waits_until_a_later_holding_status_stalls` | Table | G032 proposed table retains this complete case |
| `StallWatchTests.test_decision_turn_end_waits_until_a_later_holding_status_stalls` | Table | G032 proposed table retains this complete case |

#### G033: Table

Proposed replacement: `StallWatchTests.test_a_multiword_own_label_does_not_hold_the_unit_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `StallWatchTests.test_a_multiword_own_label_does_not_hold_the_unit` | Table | G033 proposed table retains this complete case |
| `StallWatchTests.test_a_sentence_period_ends_the_units_own_shown_label` | Table | G033 proposed table retains this complete case |

#### G034: Table

Proposed replacement: `StallWatchTests.test_waiting_on_user_interface_tests_does_not_defer_a_bump_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `StallWatchTests.test_waiting_on_user_interface_tests_does_not_defer_a_bump` | Table | G034 proposed table retains this complete case |
| `StallWatchTests.test_hyphenated_own_work_words_do_not_name_an_outside_party` | Table | G034 proposed table retains this complete case |

### production/test_style_units.py

Behavior owner: owner unknown.

These tests pin the displayed words for three steps. Keep them until the user decides whether exact labels remain part of the contract.

#### G035: Wording

Keep these tests until the user chooses the message contract.
No existing test has been proved to cover these exact labels.
A table can keep all three labels if the user keeps the contract.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `StyleUnitsTests.test_testing_step_says_testing` | Wording | Exact displayed label only; the user decides |
| `StyleUnitsTests.test_reviewing_step_says_reviewing` | Wording | Exact displayed label only; the user decides |
| `StyleUnitsTests.test_fixing_step_says_fixing_review_findings` | Wording | Exact displayed label only; the user decides |

### production/test_unit_move.py

Behavior owner: owner unknown.

The recovery cases stop at different steps. Each row keeps its stop point and the helper's full recovery assertions.

#### G036: Table

Proposed replacement: `UnitFolderMoveTests.test_running_unit_stops_pauses_moves_and_resumes_with_its_worker_command_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitFolderMoveTests.test_running_unit_stops_pauses_moves_and_resumes_with_its_worker_command` | Table | G036 proposed table retains this complete case |
| `UnitFolderMoveTests.test_folder_move_resumes_the_same_worker_thread_with_the_staff_folder` | Table | G036 proposed table retains this complete case |

#### G037: Table

Proposed replacement: `UnitFolderMoveTests.test_stopped_unit_with_a_closed_pane_moves_records_without_starting_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitFolderMoveTests.test_stopped_unit_with_a_closed_pane_moves_records_without_starting` | Table | G037 proposed table retains this complete case |
| `UnitFolderMoveTests.test_stopped_move_starts_the_unit_without_renaming` | Table | G037 proposed table retains this complete case |

#### G038: Table

Proposed replacement: `FolderBranchRecoveryTests.test_rerun_finishes_after_stop_cases`.
All 5 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FolderBranchRecoveryTests.test_rerun_finishes_after_stop` | Table | G038 proposed table retains this complete case |
| `FolderBranchRecoveryTests.test_rerun_finishes_after_move` | Table | G038 proposed table retains this complete case |
| `FolderBranchRecoveryTests.test_rerun_finishes_after_publish` | Table | G038 proposed table retains this complete case |
| `FolderBranchRecoveryTests.test_rerun_finishes_after_branch` | Table | G038 proposed table retains this complete case |
| `FolderBranchRecoveryTests.test_rerun_finishes_after_start` | Table | G038 proposed table retains this complete case |

### production/test_unit_phases.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G039: Table

Proposed replacement: `UnitPhasesTests.test_read_failures_print_below_the_table_and_send_one_configured_message_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitPhasesTests.test_read_failures_print_below_the_table_and_send_one_configured_message` | Table | G039 proposed table retains this complete case |
| `UnitPhasesTests.test_failed_anomaly_send_keeps_the_table_and_exit_code_and_adds_one_line` | Table | G039 proposed table retains this complete case |

### production/test_unit_status.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G040: Table

Proposed replacement: `UnitStatusTests.test_healthy_run_has_no_tick_warning_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitStatusTests.test_healthy_run_has_no_tick_warning` | Table | G040 proposed table retains this complete case |
| `UnitStatusTests.test_claude_session_id_comes_from_lookup_even_with_a_different_name` | Table | G040 proposed table retains this complete case |

### production/test_unit_status_decisions.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G041: Table

Proposed replacement: `UnitStatusTests.test_holding_after_gate_prints_no_user_wait_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitStatusTests.test_holding_after_gate_prints_no_user_wait` | Table | G041 proposed table retains this complete case |
| `UnitStatusTests.test_holding_text_that_mentions_gate_clears_user_wait` | Table | G041 proposed table retains this complete case |

### production/test_unrouted_alert.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G042: Table

Proposed replacement: `UnroutedAlertTests.test_a_missing_retry_setting_reports_one_error_without_sending_or_marking_sent_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnroutedAlertTests.test_a_missing_retry_setting_reports_one_error_without_sending_or_marking_sent` | Table | G042 proposed table retains this complete case |
| `UnroutedAlertTests.test_an_unusable_recipient_reports_one_error_and_keeps_the_alert_pending` | Table | G042 proposed table retains this complete case |

### production/test_update_registration_answers.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G043: Table

Proposed replacement: `RegistrationTests.test_answer_rerun_recovers_relay_after_list_replacement_completed_then_stopped_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistrationTests.test_answer_rerun_recovers_relay_after_list_replacement_completed_then_stopped` | Table | G043 proposed table retains this complete case |
| `RegistrationTests.test_answer_rerun_removes_open_item_after_answer_record_was_saved` | Table | G043 proposed table retains this complete case |

#### G044: Table

Proposed replacement: `RegistrationTests.test_answer_replay_names_the_renamed_asking_unit_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistrationTests.test_answer_replay_names_the_renamed_asking_unit` | Table | G044 proposed table retains this complete case |
| `RegistrationTests.test_answer_replay_names_the_renamed_production_owner` | Table | G044 proposed table retains this complete case |
| `RegistrationTests.test_answer_replay_names_the_asking_units_new_production` | Table | G044 proposed table retains this complete case |

### production/test_worker.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G045: Table

Proposed replacement: `WorkerCommandTests.test_done_with_a_live_run_waits_for_delivery_and_a_later_done_reply_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `WorkerCommandTests.test_done_with_a_live_run_waits_for_delivery_and_a_later_done_reply` | Table | G045 proposed table retains this complete case |
| `WorkerCommandTests.test_a_reply_without_done_with_a_live_run_does_not_return_no_done` | Table | G045 proposed table retains this complete case |

### direct/test_assign_claude_door.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G046: Table

Proposed replacement: `AssignClaudeDoorTests.test_claude_ancestor_reaches_the_configured_claude_stub_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AssignClaudeDoorTests.test_claude_ancestor_reaches_the_configured_claude_stub` | Table | G046 proposed table retains this complete case |
| `AssignClaudeDoorTests.test_codex_ancestor_reaches_the_configured_claude_stub` | Table | G046 proposed table retains this complete case |

### direct/test_assign_gate.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G047: Table

Proposed replacement: `AssignGateTests.test_assign_parent_passes_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AssignGateTests.test_assign_parent_passes` | Table | G047 proposed table retains this complete case |
| `AssignGateTests.test_apostrophe_in_launch_argument_passes_the_gate` | Table | G047 proposed table retains this complete case |

### direct/test_assign_launcher.py

Behavior owner: owner unknown.

This module is held by Phase 68.
The proposed change must wait for that phase.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G048: Table

Proposed replacement: `FailedLaunchRunTests.test_an_open_review_without_a_worker_name_keeps_the_run_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FailedLaunchRunTests.test_an_open_review_without_a_worker_name_keeps_the_run` | Table, held | G048 proposed table retains this complete case |
| `FailedLaunchRunTests.test_a_finished_review_without_a_worker_name_keeps_the_run` | Table, held | G048 proposed table retains this complete case |

#### G049: Table

Proposed replacement: `AssignLauncherTests.test_manual_resolution_lands_a_clean_fix_without_another_worker_turn_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AssignLauncherTests.test_manual_resolution_lands_a_clean_fix_without_another_worker_turn` | Table, held | G049 proposed table retains this complete case |
| `AssignLauncherTests.test_manual_resolution_abandons_a_failed_fix_without_another_worker_turn` | Table, held | G049 proposed table retains this complete case |

### direct/test_assign_launcher_reuse.py

Behavior owner: owner unknown.

This module is held by Phase 68.
The proposed change must wait for that phase.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G050: Table

Proposed replacement: `AssignReuseTests.test_a_dead_launchers_reopened_dispatch_refuses_a_missing_thread_without_replacement_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `AssignReuseTests.test_a_dead_launchers_reopened_dispatch_refuses_a_missing_thread_without_replacement` | Table, held | G050 proposed table retains this complete case |
| `AssignReuseTests.test_an_ended_workers_reopened_dispatch_refuses_a_missing_thread_without_replacement` | Table, held | G050 proposed table retains this complete case |

### direct/test_assign_launcher_review.py

Behavior owner: owner unknown.

This module is held by Phase 68.
The proposed change must wait for that phase.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G051: Table

Proposed replacement: `PaneAstraReviewTests.test_pane_reviewer_writes_the_reply_and_ends_its_row_and_hold_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PaneAstraReviewTests.test_pane_reviewer_writes_the_reply_and_ends_its_row_and_hold` | Table, held | G051 proposed table retains this complete case |
| `PaneAstraReviewTests.test_failed_pane_review_ends_its_row_and_hold` | Table, held | G051 proposed table retains this complete case |

### direct/test_board_start.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G052: Table

Proposed replacement: `BoardStartTests.test_path_with_a_slash_posts_without_readlink_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `BoardStartTests.test_path_with_a_slash_posts_without_readlink` | Table | G052 proposed table retains this complete case |
| `BoardStartTests.test_filename_only_start_posts_to_the_board` | Table | G052 proposed table retains this complete case |
| `BoardStartTests.test_symlink_start_posts_to_the_board` | Table | G052 proposed table retains this complete case |

### direct/test_demo_refusals.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G053: Table

Proposed replacement: `RefusalDemoTests.test_five_refusals_run_in_order_and_remove_their_scratch_folders_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RefusalDemoTests.test_five_refusals_run_in_order_and_remove_their_scratch_folders` | Table | G053 proposed table retains this complete case |
| `RefusalDemoTests.test_demo_skips_python310_without_datetime_utc` | Table | G053 proposed table retains this complete case |

### direct/test_findings.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G054: Table

Proposed replacement: `FindingsLedgerTests.test_dispatch_refuses_unknown_ids_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FindingsLedgerTests.test_dispatch_refuses_unknown_ids` | Table | G054 proposed table retains this complete case |
| `FindingsLedgerTests.test_dispatch_refuses_when_the_gate_says_converged` | Table | G054 proposed table retains this complete case |
| `FindingsLedgerTests.test_abandon_refuses_when_no_repair_is_in_flight` | Table | G054 proposed table retains this complete case |

#### G055: Table

Proposed replacement: `FindingsLedgerTests.test_reopening_an_accepted_finding_requires_evidence_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FindingsLedgerTests.test_reopening_an_accepted_finding_requires_evidence` | Table | G055 proposed table retains this complete case |
| `FindingsLedgerTests.test_still_open_is_refused_for_an_accepted_finding` | Table | G055 proposed table retains this complete case |

### direct/test_phase_artifacts.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G056: Table

Proposed replacement: `PhaseArtifactTests.test_a_phase_reviewing_counts_as_running_work_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PhaseArtifactTests.test_a_phase_reviewing_counts_as_running_work` | Table | G056 proposed table retains this complete case |
| `PhaseArtifactTests.test_a_phase_implementing_counts_as_running_work` | Table | G056 proposed table retains this complete case |

### direct/test_progress_history.py

Behavior owner: owner unknown.

The cases check terminal holder states, legacy pass state, and plan titles. Each row keeps its state and expected fields.

#### G057: Table

Proposed replacement: `ProgressHistoryTests.test_finished_wait_keeps_its_malformed_holder_with_or_without_a_start_row_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ProgressHistoryTests.test_finished_wait_keeps_its_malformed_holder_with_or_without_a_start_row` | Table | G057 proposed table retains this complete case |
| `ProgressHistoryTests.test_unclosed_wait_keeps_its_malformed_holder_with_or_without_a_start_row` | Table | G057 proposed table retains this complete case |

#### G058: Table

Proposed replacement: `ProgressHistoryTests.test_a_legacy_single_pass_object_still_finishes_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ProgressHistoryTests.test_a_legacy_single_pass_object_still_finishes` | Table | G058 proposed table retains this complete case |
| `ProgressHistoryTests.test_a_legacy_pass_with_no_slot_reads_as_the_empty_slot` | Table | G058 proposed table retains this complete case |

#### G059: Table

Proposed replacement: `PublicPhaseHelperTests.test_plan_phases_preserves_descriptive_parentheticals_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PublicPhaseHelperTests.test_plan_phases_preserves_descriptive_parentheticals` | Table | G059 proposed table retains this complete case |
| `PublicPhaseHelperTests.test_plan_phases_strips_only_commit_annotations` | Table | G059 proposed table retains this complete case |

### direct/test_remove_workers.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G060: Table

Proposed replacement: `RemoveWorkersTest.test_stale_down_record_does_not_keep_a_dead_directors_run_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RemoveWorkersTest.test_stale_down_record_does_not_keep_a_dead_directors_run` | Table | G060 proposed table retains this complete case |
| `RemoveWorkersTest.test_stale_restarting_record_does_not_keep_a_dead_directors_run` | Table | G060 proposed table retains this complete case |

### direct/test_rerun_lens.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G061: Table

Proposed replacement: `RerunLensTests.test_string_replacement_and_console_output_do_not_arm_the_lens_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RerunLensTests.test_string_replacement_and_console_output_do_not_arm_the_lens` | Table | G061 proposed table retains this complete case |
| `RerunLensTests.test_binary_console_output_does_not_arm_the_lens` | Table | G061 proposed table retains this complete case |

### direct/test_review_launcher_ux.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G062: Table

Proposed replacement: `ReviewLauncherPassTests.test_plain_ux_defect_report_does_not_write_a_turn_record_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ReviewLauncherPassTests.test_plain_ux_defect_report_does_not_write_a_turn_record` | Table | G062 proposed table retains this complete case |
| `ReviewLauncherPassTests.test_plain_ux_pass_does_not_write_a_turn_record` | Table | G062 proposed table retains this complete case |

### direct/test_tag_inventory.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G063: Table

Proposed replacement: `TagInventoryTests.test_two_bodies_are_named_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `TagInventoryTests.test_two_bodies_are_named` | Table | G063 proposed table retains this complete case |
| `TagInventoryTests.test_table_cannot_hide_a_different_stale_stub` | Table | G063 proposed table retains this complete case |

### direct/test_verify_example_markers.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G064: Table

Proposed replacement: `ExampleMarkerTests.test_plain_test_attribute_is_refused_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ExampleMarkerTests.test_plain_test_attribute_is_refused` | Table | G064 proposed table retains this complete case |
| `ExampleMarkerTests.test_namespaced_test_attribute_is_refused` | Table | G064 proposed table retains this complete case |

### direct/test_verify_hold_workers.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G065: Table

Proposed replacement: `VerifyHoldWaitTests.test_verification_thread_overrides_the_servers_impl_slot_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyHoldWaitTests.test_verification_thread_overrides_the_servers_impl_slot` | Table | G065 proposed table retains this complete case |
| `VerifyHoldWaitTests.test_fix_named_worker_uses_its_recorded_test_slot` | Table | G065 proposed table retains this complete case |

### direct/test_verify_lint_cache_versions.py

Behavior owner: verify-unit.

This module is held by Phase 84.
The proposed change must wait for that phase.

Each case changes a different tool version. Keep every invalidation case. Phase 84 holds this module.

#### G066: Table

Proposed replacement: `VerifyLintToolVersionTests.test_mend_version_invalidates_passes_and_failures_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyLintToolVersionTests.test_mend_version_invalidates_passes_and_failures` | Table, held | G066 proposed table retains this complete case |
| `VerifyLintToolVersionTests.test_rustfmt_version_invalidates_passes_and_failures` | Table, held | G066 proposed table retains this complete case |
| `VerifyLintToolVersionTests.test_clippy_version_invalidates_passes_and_failures` | Table, held | G066 proposed table retains this complete case |
| `VerifyLintToolVersionTests.test_taplo_version_invalidates_passes_and_failures` | Table, held | G066 proposed table retains this complete case |

### direct/test_verify_lint_local_writes.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G067: Table

Proposed replacement: `VerifyLintLocalWritesTests.test_mend_fix_waits_for_natedev_and_changes_the_local_tree_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyLintLocalWritesTests.test_mend_fix_waits_for_natedev_and_changes_the_local_tree` | Table | G067 proposed table retains this complete case |
| `VerifyLintLocalWritesTests.test_clippy_fix_waits_for_natedev_and_changes_the_local_tree` | Table | G067 proposed table retains this complete case |

### direct/test_verify_mac_retry_clock.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G068: Table

Proposed replacement: `VerifyMacRetryClockTests.test_idle_marker_bypasses_memory_retry_spacing_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyMacRetryClockTests.test_idle_marker_bypasses_memory_retry_spacing` | Table | G068 proposed table retains this complete case |
| `VerifyMacRetryClockTests.test_idle_marker_bypasses_token_retry_spacing` | Table | G068 proposed table retains this complete case |

### direct/test_verify_mac_signal_boundaries.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G069: Table

Proposed replacement: `VerifyMacSignalBoundariesTests.test_signal_before_runner_start_removes_result_without_local_work_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyMacSignalBoundariesTests.test_signal_before_runner_start_removes_result_without_local_work` | Table | G069 proposed table retains this complete case |
| `VerifyMacSignalBoundariesTests.test_signal_before_delayed_tee_opens_leaves_no_temp_log` | Table | G069 proposed table retains this complete case |

#### G070: Table

Proposed replacement: `VerifyMacSignalBoundariesTests.test_check_stop_before_delayed_tee_opens_leaves_no_temp_log_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyMacSignalBoundariesTests.test_check_stop_before_delayed_tee_opens_leaves_no_temp_log` | Table | G070 proposed table retains this complete case |
| `VerifyMacSignalBoundariesTests.test_build_stop_before_delayed_tee_opens_leaves_no_temp_log` | Table | G070 proposed table retains this complete case |

### direct/test_verify_memory_kill.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G071: Table

Proposed replacement: `MemoryKillTests.test_sigkill_earlyoom_message_is_memory_kill_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MemoryKillTests.test_sigkill_earlyoom_message_is_memory_kill` | Table | G071 proposed table retains this complete case |
| `MemoryKillTests.test_plain_sigterm_without_journal_kill_is_not_memory_kill` | Table | G071 proposed table retains this complete case |
| `MemoryKillTests.test_genuine_lint_failure_is_tree_failure` | Table | G071 proposed table retains this complete case |

#### G072: Table

Proposed replacement: `MemoryKillTests.test_nextest_test_binary_kill_retries_once_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `MemoryKillTests.test_nextest_test_binary_kill_retries_once` | Table | G072 proposed table retains this complete case |
| `MemoryKillTests.test_linker_kill_retries_once` | Table | G072 proposed table retains this complete case |

### direct/test_verify_ssh_targets.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G073: Table

Proposed replacement: `VerifySshTargetsTests.test_probe_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifySshTargetsTests.test_probe_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target` | Table | G073 proposed table retains this complete case |
| `VerifySshTargetsTests.test_run_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target` | Table | G073 proposed table retains this complete case |
| `VerifySshTargetsTests.test_after_probe_refusal_keeps_queue_age_and_continues_to_the_next_ssh_target` | Table | G073 proposed table retains this complete case |

#### G074: Table

Proposed replacement: `VerifyLinuxStagedDocTests.test_linux_doc_judges_the_staged_tree_without_a_local_hold_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyLinuxStagedDocTests.test_linux_doc_judges_the_staged_tree_without_a_local_hold` | Table | G074 proposed table retains this complete case |
| `VerifyLinuxStagedDocTests.test_pinned_linux_doc_keeps_the_staged_tree_and_skips_local_admission` | Table | G074 proposed table retains this complete case |

### direct/test_verify_ssh_targets_routing.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G075: Table

Proposed replacement: `VerifySshRoutingTests.test_pinned_wait_keeps_its_live_record_until_aws_becomes_available_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifySshRoutingTests.test_pinned_wait_keeps_its_live_record_until_aws_becomes_available` | Table | G075 proposed table retains this complete case |
| `VerifySshRoutingTests.test_pinned_check_waits_until_aws_becomes_available` | Table | G075 proposed table retains this complete case |

### direct/test_verify_target_platform.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G076: Table

Proposed replacement: `VerifyTargetPlatformTests.test_macos_row_supplies_the_platform_without_an_interpreter_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyTargetPlatformTests.test_macos_row_supplies_the_platform_without_an_interpreter` | Table | G076 proposed table retains this complete case |
| `VerifyTargetPlatformTests.test_linux_row_supplies_the_platform_without_an_interpreter` | Table | G076 proposed table retains this complete case |

#### G077: Table

Proposed replacement: `VerifyTargetPlatformTests.test_local_row_supplies_no_remote_platform_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyTargetPlatformTests.test_local_row_supplies_no_remote_platform` | Table | G077 proposed table retains this complete case |
| `VerifyTargetPlatformTests.test_missing_mac_row_has_no_platform` | Table | G077 proposed table retains this complete case |
| `VerifyTargetPlatformTests.test_missing_linux_row_has_no_platform` | Table | G077 proposed table retains this complete case |

### direct/test_verify_token_mac_step.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G078: Table

Proposed replacement: `VerifyTokenMacStepTests.test_initial_mac_reservation_ends_before_local_mend_waits_for_the_token_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyTokenMacStepTests.test_initial_mac_reservation_ends_before_local_mend_waits_for_the_token` | Table | G078 proposed table retains this complete case |
| `VerifyTokenMacStepTests.test_mac_stage_releases_its_claim_before_local_fmt_waits_for_the_token` | Table | G078 proposed table retains this complete case |

### direct/test_verify_token_release.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G079: Table

Proposed replacement: `VerifyTokenReleaseTests.test_step_group_is_recorded_before_cargo_starts_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyTokenReleaseTests.test_step_group_is_recorded_before_cargo_starts` | Table | G079 proposed table retains this complete case |
| `VerifyTokenReleaseTests.test_a_seat_with_only_the_names_before_the_rename_takes_its_run_token` | Table | G079 proposed table retains this complete case |

### direct/test_verify_wait_holder.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G080: Table

Proposed replacement: `VerifyRecordedTokenIdentityTests.test_dispatch_pass_names_the_worker_session_without_worker_files_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `VerifyRecordedTokenIdentityTests.test_dispatch_pass_names_the_worker_session_without_worker_files` | Table | G080 proposed table retains this complete case |
| `VerifyRecordedTokenIdentityTests.test_dispatch_worker_name_resolves_when_the_dispatch_slot_differs` | Table | G080 proposed table retains this complete case |

### direct/test_worker_state.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G081: Table

Proposed replacement: `WorkerClaimTests.test_malformed_json_has_no_worker_claim_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `WorkerClaimTests.test_malformed_json_has_no_worker_claim` | Table | G081 proposed table retains this complete case |
| `WorkerClaimTests.test_an_empty_record_has_no_worker_claim` | Table | G081 proposed table retains this complete case |

### agents/test_models_conf_publish.py

Behavior owner: owner unknown.

The writer and recovery cases use the same assertion helpers. Each row keeps its writer, obstruction, merge state, and push result.

#### G082: Table

Proposed replacement: `RegistryPublishingTests.test_a_row_writer_publishes_only_the_registry_and_preserves_dirty_and_staged_files_cases`.
All 5 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_a_row_writer_publishes_only_the_registry_and_preserves_dirty_and_staged_files` | Table | G082 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_assignment_writer_publishes_only_the_registry` | Table | G082 proposed table retains this complete case |
| `RegistryPublishingTests.test_the_all_assignments_writer_publishes_only_the_registry` | Table | G082 proposed table retains this complete case |
| `RegistryPublishingTests.test_a_model_sweep_publishes_only_the_registry` | Table | G082 proposed table retains this complete case |
| `RegistryPublishingTests.test_a_service_tier_writer_publishes_only_the_registry` | Table | G082 proposed table retains this complete case |

#### G083: Table

Proposed replacement: `RegistryPublishingTests.test_recovery_refuses_a_dirty_conflicting_file_without_changing_the_registry_index_or_head_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_recovery_refuses_a_dirty_conflicting_file_without_changing_the_registry_index_or_head` | Table | G083 proposed table retains this complete case |
| `RegistryPublishingTests.test_recovery_refuses_an_untracked_obstruction_without_changing_the_registry_index_or_head` | Table | G083 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_ordinary_fast_forward_refuses_a_dirty_conflicting_file_without_changing_the_index` | Table | G083 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_ordinary_fast_forward_refuses_an_untracked_obstruction_without_changing_the_index` | Table | G083 proposed table retains this complete case |

#### G084: Table

Proposed replacement: `RegistryPublishingTests.test_recovery_refuses_an_untracked_directory_in_the_way_of_a_remote_file_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_recovery_refuses_an_untracked_directory_in_the_way_of_a_remote_file` | Table | G084 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_ordinary_fast_forward_refuses_an_untracked_directory_in_the_way_of_a_remote_file` | Table | G084 proposed table retains this complete case |

#### G085: Table

Proposed replacement: `RegistryPublishingTests.test_an_equal_remote_refuses_a_fully_staged_unfinished_merge_before_publication_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_an_equal_remote_refuses_a_fully_staged_unfinished_merge_before_publication` | Table | G085 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_equal_remote_refuses_an_unresolved_merge_before_publication` | Table | G085 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_ahead_checkout_refuses_a_fully_staged_unfinished_merge_before_publication` | Table | G085 proposed table retains this complete case |
| `RegistryPublishingTests.test_an_ahead_checkout_refuses_an_unresolved_merge_before_publication` | Table | G085 proposed table retains this complete case |

#### G086: Table

Proposed replacement: `RegistryPublishingTests.test_a_successful_push_preserves_a_merge_started_before_local_reconciliation_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_a_successful_push_preserves_a_merge_started_before_local_reconciliation` | Table | G086 proposed table retains this complete case |
| `RegistryPublishingTests.test_a_successful_push_preserves_unmerged_entries_without_merge_head_before_local_reconciliation` | Table | G086 proposed table retains this complete case |

#### G087: Table

Proposed replacement: `RegistryPublishingTests.test_a_rejected_push_refuses_a_dirty_peer_path_before_another_write_or_push_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RegistryPublishingTests.test_a_rejected_push_refuses_a_dirty_peer_path_before_another_write_or_push` | Table | G087 proposed table retains this complete case |
| `RegistryPublishingTests.test_a_rejected_push_refuses_an_untracked_peer_path_before_another_write_or_push` | Table | G087 proposed table retains this complete case |

### lint/test_invoke_scope.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G088: Table

Proposed replacement: `SweepStartupTests.test_offload_switch_starts_no_detached_sweep_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `SweepStartupTests.test_offload_switch_starts_no_detached_sweep` | Table | G088 proposed table retains this complete case |
| `SweepStartupTests.test_without_offload_switch_the_detached_sweep_still_starts` | Table | G088 proposed table retains this complete case |
| `SweepStartupTests.test_switch_set_to_zero_keeps_detached_sweep_enabled` | Table | G088 proposed table retains this complete case |

### lint/test_memory_admit.py

Behavior owner: resources-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G089: Table

Proposed replacement: `FreshTestPromiseTests.test_failed_tests_still_prove_the_test_targets_were_built_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FreshTestPromiseTests.test_failed_tests_still_prove_the_test_targets_were_built` | Table | G089 proposed table retains this complete case |
| `FreshTestPromiseTests.test_successful_empty_selection_still_proves_the_test_targets_were_built` | Table | G089 proposed table retains this complete case |

#### G090: Table

Proposed replacement: `RepositoryHistoryTests.test_same_repository_shares_peaks_across_folders_and_remote_forms_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RepositoryHistoryTests.test_same_repository_shares_peaks_across_folders_and_remote_forms` | Table | G090 proposed table retains this complete case |
| `RepositoryHistoryTests.test_clone_uses_other_repositories_pooled_samples_without_a_basename_key` | Table | G090 proposed table retains this complete case |

### lint/test_nextest_width.py

Behavior owner: verify-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G091: Table

Proposed replacement: `NextestWidthTests.test_trial_arm_uses_100_minute_block_parity_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `NextestWidthTests.test_trial_arm_uses_100_minute_block_parity` | Table | G091 proposed table retains this complete case |
| `NextestWidthTests.test_trial_mode_uses_logical_width_in_an_odd_block` | Table | G091 proposed table retains this complete case |

### lint/test_package_imports.py

Behavior owner: owner unknown.

Each helper checks unchanged source bytes and the refusal result. Different unsafe source forms remain separate rows.

#### G092: Table

Proposed replacement: `PackageImportTests.test_conversion_refuses_a_path_assignment_from_the_current_directory_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PackageImportTests.test_conversion_refuses_a_path_assignment_from_the_current_directory` | Table | G092 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_an_unsupported_file_directory_call` | Table | G092 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_path_slice_with_a_vendor_directory` | Table | G092 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_to_select_a_repository_import_after_a_plugin_path` | Table | G092 proposed table retains this complete case |

#### G093: Table

Proposed replacement: `PackageImportTests.test_conversion_refuses_to_delete_a_function_that_has_a_caller_cases`.
All 10 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PackageImportTests.test_conversion_refuses_to_delete_a_function_that_has_a_caller` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_to_leave_a_decorator_without_its_class` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_path_change_on_a_line_with_a_kept_return` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_binding_cleanup_that_would_empty_a_function` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_to_delete_a_loop_binding_read_after_the_loop` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_keeps_a_refused_entry_unchanged_without_a_bootstrap` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_path_guard_that_calls_a_root_function` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_path_guard_for_a_function_parameter` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_path_guard_when_str_is_rebound` | Table | G093 proposed table retains this complete case |
| `PackageImportTests.test_conversion_refuses_a_while_header_call_around_a_path_change` | Table | G093 proposed table retains this complete case |

### lint/test_sweep.py

Behavior owner: resources-unit.

The reclamation cases check different link and removal states. Each row keeps both callers and each space credit assertion.

#### G094: Table

Proposed replacement: `ReclamationTests.test_link_added_outside_target_after_scan_does_not_cover_shortfall_cases`.
All 5 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ReclamationTests.test_link_added_outside_target_after_scan_does_not_cover_shortfall` | Table | G094 proposed table retains this complete case |
| `ReclamationTests.test_earlier_unlink_in_same_pass_allows_last_link_to_free_blocks` | Table | G094 proposed table retains this complete case |
| `ReclamationTests.test_failed_unlink_does_not_cover_shortfall` | Table | G094 proposed table retains this complete case |
| `ReclamationTests.test_dry_run_uses_scan_estimate_without_unlinking_or_recording` | Table | G094 proposed table retains this complete case |
| `ReclamationTests.test_partial_removal_credits_only_successful_unlinks` | Table | G094 proposed table retains this complete case |

### message/test_roster.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G095: Table

Proposed replacement: `RosterTests.test_codex_review_lens_uses_role_and_lens_fields_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RosterTests.test_codex_review_lens_uses_role_and_lens_fields` | Table | G095 proposed table retains this complete case |
| `RosterTests.test_codex_review_without_lens_uses_its_role` | Table | G095 proposed table retains this complete case |

#### G096: Table

Proposed replacement: `UnitLabelsTests.test_folder_name_joins_showrunner_and_unit_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `UnitLabelsTests.test_folder_name_joins_showrunner_and_unit` | Table | G096 proposed table retains this complete case |
| `UnitLabelsTests.test_folder_name_drops_a_missing_relationship` | Table | G096 proposed table retains this complete case |

### message/test_send.py

Behavior owner: owner unknown.

The cases check different orderings of older and newer sends. Each row keeps its order and the queued body assertions.

#### G097: Table

Proposed replacement: `SendTests.test_older_delivery_keeps_the_newer_sends_queued_body_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `SendTests.test_older_delivery_keeps_the_newer_sends_queued_body` | Table | G097 proposed table retains this complete case |
| `SendTests.test_older_enqueue_keeps_the_newer_sends_queued_body_after_a_restart` | Table | G097 proposed table retains this complete case |

### message/test_send_delivery.py

Behavior owner: owner unknown.

Each stop point has a different retry or duplicate result. Each row keeps its stop point and delivery count.

#### G098: Table

Proposed replacement: `KeyedDeliveryStopTests.test_stop_after_lock_before_acceptance_retries_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `KeyedDeliveryStopTests.test_stop_after_lock_before_acceptance_retries` | Table | G098 proposed table retains this complete case |
| `KeyedDeliveryStopTests.test_stop_after_acceptance_before_record_allows_one_duplicate` | Table | G098 proposed table retains this complete case |
| `KeyedDeliveryStopTests.test_stop_after_record_before_watcher_mark_does_not_redeliver` | Table | G098 proposed table retains this complete case |

### message/test_session_send.py

Behavior owner: owner unknown.

Each probe mode checks a different sender launch path. The child makes the behavior assertions. A zero exit status is not trivial.

#### G099: Table

Proposed replacement: `SessionSendTests.test_real_sender_is_refused_under_unittest_cases`.
All 9 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `SessionSendTests.test_real_sender_is_refused_under_unittest` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_posix_spawn_refuses_the_real_sender` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_shell_command_refuses_the_real_sender` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_system_command_refuses_the_real_sender` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_sender_from_its_own_folder_is_refused` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_sender_as_a_module_is_refused` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_grandchild_sender_is_refused` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_fake_sender_runs_under_unittest` | Table | G099 proposed table retains this complete case |
| `SessionSendTests.test_caught_refusal_keeps_the_marker_after_a_fake_send` | Table | G099 proposed table retains this complete case |

### hooks/test_brp_launch_gate.py

Behavior owner: resources-unit.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G100: Table

Proposed replacement: `BrpLaunchGateTests.test_failed_memory_wait_denies_launch_with_its_last_reason_line_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `BrpLaunchGateTests.test_failed_memory_wait_denies_launch_with_its_last_reason_line` | Table | G100 proposed table retains this complete case |
| `BrpLaunchGateTests.test_failed_memory_wait_without_output_denies_launch_with_exit_status` | Table | G100 proposed table retains this complete case |

### hooks/test_change_review_gate.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G101: Table

Proposed replacement: `RefusalTest.test_a_malformed_line_is_refused_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `RefusalTest.test_a_malformed_line_is_refused` | Table | G101 proposed table retains this complete case |
| `RefusalTest.test_a_checkpoint_without_any_line_names_the_three_part_form` | Table | G101 proposed table retains this complete case |

### hooks/test_conversation_pause.py

Behavior owner: owner unknown.

The cases check different user input and time states. Each row keeps its input, clock, and pause result.

#### G102: Table

Proposed replacement: `ConversationPauseTests.test_returned_other_starts_a_new_pause_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ConversationPauseTests.test_returned_other_starts_a_new_pause` | Table | G102 proposed table retains this complete case |
| `ConversationPauseTests.test_bare_yes_after_the_late_window_is_plain_typing` | Table | G102 proposed table retains this complete case |

### hooks/test_direct_turn_wait_hooks.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G103: Table

Proposed replacement: `DirectorTurnWaitHookTests.test_missing_postmaster_record_leaves_supplier_wait_open_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DirectorTurnWaitHookTests.test_missing_postmaster_record_leaves_supplier_wait_open` | Table | G103 proposed table retains this complete case |
| `DirectorTurnWaitHookTests.test_two_matching_postmaster_records_leave_supplier_wait_open` | Table | G103 proposed table retains this complete case |

### hooks/test_fn_length.py

Behavior owner: owner unknown.

The cases vary source text or manifest text. Each row keeps the expected function names, line counts, exemptions, or scope result.

#### G104: Table

Proposed replacement: `FunctionCountingTests.test_blank_and_comment_only_lines_do_not_count_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionCountingTests.test_blank_and_comment_only_lines_do_not_count` | Table | G104 proposed table retains this complete case |
| `FunctionCountingTests.test_multiline_block_comment_and_code_after_it` | Table | G104 proposed table retains this complete case |

#### G105: Table

Proposed replacement: `FunctionCountingTests.test_string_with_slashes_is_counted_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionCountingTests.test_string_with_slashes_is_counted` | Table | G105 proposed table retains this complete case |
| `FunctionCountingTests.test_one_line_body_counts_one_line` | Table | G105 proposed table retains this complete case |

#### G106: Table

Proposed replacement: `FunctionDiscoveryTests.test_uninvoked_macro_definition_and_foreign_invocation_are_skipped_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionDiscoveryTests.test_uninvoked_macro_definition_and_foreign_invocation_are_skipped` | Table | G106 proposed table retains this complete case |
| `FunctionDiscoveryTests.test_unclosed_function_is_dropped_without_losing_prior_function` | Table | G106 proposed table retains this complete case |
| `FunctionDiscoveryTests.test_spaced_and_commented_macro_calls_hide_generated_functions` | Table | G106 proposed table retains this complete case |

#### G107: Table

Proposed replacement: `FunctionDiscoveryTests.test_braces_in_literals_and_nested_comments_do_not_close_body_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionDiscoveryTests.test_braces_in_literals_and_nested_comments_do_not_close_body` | Table | G107 proposed table retains this complete case |
| `FunctionDiscoveryTests.test_unicode_and_raw_function_names_are_preserved` | Table | G107 proposed table retains this complete case |

#### G108: Table

Proposed replacement: `FunctionDiscoveryTests.test_inner_attribute_exempts_only_its_module_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionDiscoveryTests.test_inner_attribute_exempts_only_its_module` | Table | G108 proposed table retains this complete case |
| `FunctionDiscoveryTests.test_impl_return_type_keeps_outer_exemption_on_nested_function` | Table | G108 proposed table retains this complete case |

#### G109: Table

Proposed replacement: `FunctionDiscoveryTests.test_not_equal_operator_does_not_hide_nested_or_later_functions_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionDiscoveryTests.test_not_equal_operator_does_not_hide_nested_or_later_functions` | Table | G109 proposed table retains this complete case |
| `FunctionDiscoveryTests.test_string_slashes_before_prefix_bang_do_not_hide_functions` | Table | G109 proposed table retains this complete case |

#### G110: Table

Proposed replacement: `FunctionScopeTests.test_missing_lints_disables_scope_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `FunctionScopeTests.test_missing_lints_disables_scope` | Table | G110 proposed table retains this complete case |
| `FunctionScopeTests.test_malformed_manifest_disables_scope` | Table | G110 proposed table retains this complete case |

### hooks/test_heredoc_guard.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G111: Table

Proposed replacement: `HeredocGuardTests.test_dollar_paren_in_unquoted_body_is_refused_cases`.
All 3 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `HeredocGuardTests.test_dollar_paren_in_unquoted_body_is_refused` | Table | G111 proposed table retains this complete case |
| `HeredocGuardTests.test_heredoc_inside_command_substitution_is_refused` | Table | G111 proposed table retains this complete case |
| `HeredocGuardTests.test_unterminated_unquoted_body_is_refused` | Table | G111 proposed table retains this complete case |

#### G112: Table

Proposed replacement: `HeredocGuardTests.test_plain_variable_expansion_is_allowed_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `HeredocGuardTests.test_plain_variable_expansion_is_allowed` | Table | G112 proposed table retains this complete case |
| `HeredocGuardTests.test_here_string_is_allowed` | Table | G112 proposed table retains this complete case |

### hooks/test_post_tool_use_banned_words.py

Behavior owner: owner unknown.

The cases vary tool input and authored text. Each row keeps its block result, exemption result, and state assertions.

#### G113: Table

Proposed replacement: `PostToolUseBannedWordsTests.test_a_shell_command_writing_the_word_blocks_cases`.
All 7 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PostToolUseBannedWordsTests.test_a_shell_command_writing_the_word_blocks` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_replacement_text_blocks` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_a_reader_does_not_exempt_a_writer_in_the_same_command` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_notebook_inserted_source_blocks` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_scripted_replacement_scans_the_inserted_text` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_a_shell_substitution_is_not_exempted_by_its_reader` | Table | G113 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_inspection_tools_with_authored_format_strings_are_scanned` | Table | G113 proposed table retains this complete case |

#### G114: Table

Proposed replacement: `PostToolUseBannedWordsTests.test_written_file_content_blocks_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PostToolUseBannedWordsTests.test_written_file_content_blocks` | Table | G114 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_inspection_commands_do_not_exempt_written_descriptions` | Table | G114 proposed table retains this complete case |

#### G115: Table

Proposed replacement: `PostToolUseBannedWordsTests.test_a_commit_message_is_scanned_beside_a_log_command_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PostToolUseBannedWordsTests.test_a_commit_message_is_scanned_beside_a_log_command` | Table | G115 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_search_masking_does_not_exempt_a_word_written_later` | Table | G115 proposed table retains this complete case |

#### G116: Table

Proposed replacement: `PostToolUseBannedWordsTests.test_inspection_paths_do_not_block_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PostToolUseBannedWordsTests.test_inspection_paths_do_not_block` | Table | G116 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_scripted_removal_does_not_scan_the_removed_text` | Table | G116 proposed table retains this complete case |

#### G117: Table

Proposed replacement: `PostToolUseBannedWordsTests.test_notebook_deletion_does_not_scan_removed_source_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PostToolUseBannedWordsTests.test_notebook_deletion_does_not_scan_removed_source` | Table | G117 proposed table retains this complete case |
| `PostToolUseBannedWordsTests.test_guide_reproduction_remains_exempt` | Table | G117 proposed table retains this complete case |

### hooks/test_stop_context_deferral.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G118: Table

Proposed replacement: `ContextDeferralTests.test_other_contexts_allow_the_stop_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ContextDeferralTests.test_other_contexts_allow_the_stop` | Table | G118 proposed table retains this complete case |
| `ContextDeferralTests.test_negative_plans_allow_the_stop` | Table | G118 proposed table retains this complete case |

#### G119: Table

Proposed replacement: `ContextDeferralTests.test_a_block_quote_ends_at_the_next_blank_line_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ContextDeferralTests.test_a_block_quote_ends_at_the_next_blank_line` | Table | G119 proposed table retains this complete case |
| `ContextDeferralTests.test_an_exclusion_outside_the_region_keeps_the_block` | Table | G119 proposed table retains this complete case |

### hooks/test_stop_direct_continue.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G120: Table

Proposed replacement: `DirectorStopTests.test_declared_user_wait_is_recorded_and_allows_stop_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DirectorStopTests.test_declared_user_wait_is_recorded_and_allows_stop` | Table | G120 proposed table retains this complete case |
| `DirectorStopTests.test_declared_supplier_wait_is_recorded_and_allows_stop` | Table | G120 proposed table retains this complete case |

#### G121: Table

Proposed replacement: `DirectorStopTests.test_holding_status_without_pause_above_the_threshold_wakes_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `DirectorStopTests.test_holding_status_without_pause_above_the_threshold_wakes` | Table | G121 proposed table retains this complete case |
| `DirectorStopTests.test_blocked_within_a_sentence_above_the_threshold_wakes` | Table | G121 proposed table retains this complete case |

### lib/test_claude_launch_scan.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G122: Table

Proposed replacement: `ClaudeLaunchScanTests.test_same_module_ensure_guard_covers_its_caller_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ClaudeLaunchScanTests.test_same_module_ensure_guard_covers_its_caller` | Table | G122 proposed table retains this complete case |
| `ClaudeLaunchScanTests.test_dynamic_import_guard_and_indirect_caller_cover_starts` | Table | G122 proposed table retains this complete case |

#### G123: Table

Proposed replacement: `ClaudeLaunchScanTests.test_other_module_attribute_call_does_not_establish_a_guard_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ClaudeLaunchScanTests.test_other_module_attribute_call_does_not_establish_a_guard` | Table | G123 proposed table retains this complete case |
| `ClaudeLaunchScanTests.test_guard_cycle_without_ensure_does_not_cover_a_start` | Table | G123 proposed table retains this complete case |

### lib/test_nix_env.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G124: Table

Proposed replacement: `EnsureTests.test_missing_nix_proof_refuses_with_contract_line_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `EnsureTests.test_missing_nix_proof_refuses_with_contract_line` | Table | G124 proposed table retains this complete case |
| `EnsureTests.test_direnv_failure_uses_last_stderr_line` | Table | G124 proposed table retains this complete case |

### lib/test_process_failure.py

Behavior owner: owner unknown.

The cases vary process output. Each row keeps its stream order, labels, exit status, and expected result.

#### G125: Table

Proposed replacement: `ProcessFailureTests.test_both_streams_are_labelled_with_stderr_before_stdout_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `ProcessFailureTests.test_both_streams_are_labelled_with_stderr_before_stdout` | Table | G125 proposed table retains this complete case |
| `ProcessFailureTests.test_no_output_reports_the_failed_exit_code` | Table | G125 proposed table retains this complete case |

### lib/test_py_floor.py

Behavior owner: owner unknown.

Each case supplies a different malformed minor assignment. Each row keeps its source and the ValueError assertion.

#### G126: Table

Proposed replacement: `PythonFloorTests.test_a_missing_minor_assignment_has_a_clear_error_cases`.
All 4 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PythonFloorTests.test_a_missing_minor_assignment_has_a_clear_error` | Table | G126 proposed table retains this complete case |
| `PythonFloorTests.test_a_duplicate_minor_assignment_with_a_comment_has_a_clear_error` | Table | G126 proposed table retains this complete case |
| `PythonFloorTests.test_a_noninteger_minor_assignment_has_a_clear_error` | Table | G126 proposed table retains this complete case |
| `PythonFloorTests.test_a_comment_glued_to_the_minor_digits_has_a_clear_error` | Table | G126 proposed table retains this complete case |

### lib/test_py_shim.py

Behavior owner: owner unknown.

The candidate methods have the same statement tree after literal values and docstrings are removed. Each row must keep its distinct inputs and expected results.

#### G127: Table

Proposed replacement: `PythonShimTests.test_stdin_ignores_a_local_scripts_folder_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PythonShimTests.test_stdin_ignores_a_local_scripts_folder` | Table | G127 proposed table retains this complete case |
| `PythonShimTests.test_stdin_ignores_an_old_local_library` | Table | G127 proposed table retains this complete case |

#### G128: Table

Proposed replacement: `PythonShimTests.test_command_ignores_a_local_scripts_folder_cases`.
All 2 original methods become named rows.
The replacement keeps every input, expected value, helper call, and assertion.
No existing first row substitutes for another row.

| Candidate test | Class | Covering test or reason |
| --- | --- | --- |
| `PythonShimTests.test_command_ignores_a_local_scripts_folder` | Table | G128 proposed table retains this complete case |
| `PythonShimTests.test_command_ignores_an_old_local_library` | Table | G128 proposed table retains this complete case |

## Checks that stay

Line overlap does not prove assertion overlap.
The production overlap run passed 107 tests in 4.583 wall seconds.
It verified child coverage for 105 tests.
Two tests had unverified `py` launches.

Those tests are `ReportFooterTests.test_cold_imported_production_paths_return_to_the_callers_home_after_each_case`.
They also include `TimelineWindowTests.test_same_hour_latest_keeps_eta_marker_in_both_chart_styles`.
Both stay.

The direct overlap run passed 284 tests in 72.319 wall seconds.
Some child launches remained unverified.
They included `bash`, `taplo`, `herdr`, `py`, and `sync_codex_catalog.sh`.
No deletion relies on those coverage results.
The exact direct known-test count was not retained.

The overlap report proposes many line subsets.
These examples keep distinct behavior:

1. Keep `ProgressHistoryTests.test_phase_table_failure_does_not_change_recorder_command_results`.
2. Keep `ProgressHistoryTests.test_header_and_calibration_use_completed_history`.
3. Keep `ProgressHistoryTests.test_review_trial_counts_each_code_finding_once`.

The first test checks failure isolation.
The second test checks report values from history.
The third test checks unique finding counts.
Other tests can cover their lines without checking those results.

The run records remain under the phase's `mesh_runs` directory.
The production run ID is `20261107T151002-3333085-df17`.
The direct run ID is `20261107T151027-3404462-7358`.
Both runs exited zero.
Both raw logs were read and removed.
The first raw log was removed before the second run.
Collector wall time is not a CPU saving.

No overlap run was needed to delete a Repeat candidate.
There are no approved Repeat deletions.
No overlap run covered another folder.

### Cross-module repeats

No cross-module deletion has sufficient assertion proof.
The statement comparison does not establish proof across modules.
Similar names do not establish equal contracts.

`production/test_dailies_input.py` checks accepted report data.
`production/test_dailies_render.py` checks rendered report results.
Keep both entry points.

`message/test_send.py` checks send ordering and queue state.
`message/test_send_delivery.py` checks stop boundaries and delivery counts.
Keep both entry points.

The split-crossing retirement dependency includes `scripts/agents/stop_resume.py`.
The session staff release test calls it with the shared mesh fixture.
Keep that test during fixture migration.
This is a dependency, not a proven cross-module repeat.
No candidate in the other writer's folders is approved for deletion here.

### Trivial tests

No candidate has enough evidence for a Trivial deletion.
An exit code assertion can represent real child assertions.
An existence assertion can prove a produced file or acquired state.
A mock assertion can prove a required command and its arguments.
These forms alone do not make a test trivial.

## Held work

| Phase | Modules in this report | Decision |
| --- | --- | --- |
| 67 | `direct/test_verify_mac_only.py`; `direct/test_verify_lint_judgment.py` | Keep; no supported removal candidate |
| 68 | `direct/test_assign_launcher*.py` and the launcher fixture | Keep the hold; four Table groups remain conditional |
| 84 | `direct/test_verify_lint_cache_versions.py`; `direct/test_verify_step_state.py` | Keep the hold; one Table group remains conditional |
| 85 | No module in these seven folders | No overlapping action |

The Phase 67 and Phase 68 test counts use `git show HEAD:<path>`.
Other test counts use the worktree snapshot.
All 433 module counts were checked again.
Their counts did not change during this report.

The held costs remain in the folder totals.
No table proposal claims a held phase's expected CPU gain.
The cost of `test_verify_step_state.py` is not a new streamlining action.

## Growth patterns and proposed rules

These rules are proposals.
They create no new check or gate.

| Pattern | Proposed rule |
| --- | --- |
| One method for each literal input | Add a named row to the existing table when the assertion path stays the same. |
| One method for each tool version key | Keep each invalidation key as a table row. |
| One method for each displayed label | Keep exact text only when the user or a consumer requires it. |
| One repository build for each publishing case | Build one immutable seed and copy it for each mutating case. |
| One helper wrapper for each failure stage | Keep all failure stages in one table with separate expected results. |
| One process for each direct function decision | Call the real function directly when the process adds no distinct behavior proof. |
| Repeated process entry paths | Keep each path that proves a distinct launch or import boundary. |
| New regression tests beside similar old tests | Extend the old test when the new assertion uses the same setup and result. |
| Feature retirement without test retirement | Remove tests only after the supported behavior and its fixture dependencies end. |
| Coverage subsets treated as repeats | Require the same setup, result, and assertion target before removing a repeat. |
| Tables judged by their lower method count | Report input cases and measured CPU beside method counts. |
| New support modules imported as tests | Move reusable helpers to a support module when their test module retires. |
| Separate modules for one fixture family | Compare the full family's CPU before adding another module. |

The direct-call proposal follows the existing speed method.
It has no measured gain in this report.
It needs a later behavior map and a broken-function probe.
It must keep each real end-to-end path that proves a unique boundary.

## Full module ledger

This ledger includes every `scripts/<folder>/test_*.py` file.
The shell glob counts match the ledger.
The folder counts are 116, 206, 8, 13, 18, 29, and 43.

R means conditional Retired.
T means Table.
W means Wording.
K means retained without a candidate.
Trivial, Repeat, and Cross-module repeat counts are zero in every row.
R, T, W, and K add to the current method count.

The after column excludes held actions, retirement, and Wording removal.
The CPU column is a historical estimate.
An asterisk marks a held module.
The test speed change owner remains `natest-unit` for every row.

| Module | Now | R | T | W | K | After, estimate | CPU s per gate | Behavior owner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `production/test_add_showrunner.py` | 60 | 0 | 6 | 0 | 54 | 57 | 21.819 | owner unknown |
| `production/test_add_unit.py` | 64 | 0 | 0 | 0 | 64 | 64 | 60.921 | owner unknown |
| `production/test_add_unit_accounts.py` | 34 | 0 | 0 | 0 | 34 | 34 | 24.338 | owner unknown |
| `production/test_add_unit_rerun.py` | 13 | 0 | 0 | 0 | 13 | 13 | 13.366 | owner unknown |
| `production/test_add_unit_restore.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.951 | owner unknown |
| `production/test_add_unit_resume.py` | 30 | 0 | 0 | 0 | 30 | 30 | 15.758 | owner unknown |
| `production/test_add_unit_staff_row.py` | 3 | 0 | 0 | 0 | 3 | 3 | 1.490 | owner unknown |
| `production/test_affected_modules.py` | 23 | 0 | 0 | 0 | 23 | 23 | 0.907 | owner unknown |
| `production/test_autocompact.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.114 | owner unknown |
| `production/test_broadcast.py` | 49 | 0 | 0 | 0 | 49 | 49 | 6.491 | owner unknown |
| `production/test_change_review.py` | 135 | 0 | 0 | 0 | 135 | 135 | 5.843 | owner unknown |
| `production/test_change_routing.py` | 36 | 0 | 0 | 0 | 36 | 36 | 1.473 | owner unknown |
| `production/test_ci_points.py` | 34 | 0 | 0 | 0 | 34 | 34 | 13.492 | owner unknown |
| `production/test_codex_winddown.py` | 38 | 0 | 0 | 0 | 38 | 38 | 12.853 | owner unknown |
| `production/test_completion_receipt.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.426 | owner unknown |
| `production/test_dailies_counts.py` | 7 | 0 | 0 | 0 | 7 | 7 | 0.426 | owner unknown |
| `production/test_dailies_input.py` | 130 | 0 | 6 | 0 | 124 | 127 | 23.060 | owner unknown |
| `production/test_dailies_render.py` | 107 | 0 | 7 | 0 | 100 | 103 | 4.126 | owner unknown |
| `production/test_dailies_render_agents.py` | 16 | 0 | 2 | 0 | 14 | 15 | 1.005 | owner unknown |
| `production/test_dailies_render_holds.py` | 20 | 0 | 0 | 0 | 20 | 20 | 33.906 | owner unknown |
| `production/test_delivery_state.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.099 | owner unknown |
| `production/test_fake_showrunner.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.363 | owner unknown |
| `production/test_fast_track.py` | 56 | 0 | 4 | 0 | 52 | 54 | 52.485 | owner unknown |
| `production/test_footer_counts.py` | 28 | 0 | 0 | 0 | 28 | 28 | 1.200 | owner unknown |
| `production/test_live_units.py` | 15 | 0 | 0 | 0 | 15 | 15 | 4.319 | owner unknown |
| `production/test_log_backup.py` | 9 | 0 | 0 | 0 | 9 | 9 | 1.582 | owner unknown |
| `production/test_mac_run.py` | 20 | 0 | 2 | 0 | 18 | 19 | 33.849 | verify-unit |
| `production/test_merge_checkpoint.py` | 36 | 0 | 0 | 0 | 36 | 36 | 5.863 | owner unknown |
| `production/test_merge_checkpoint_bump.py` | 23 | 0 | 3 | 0 | 20 | 21 | 24.825 | owner unknown |
| `production/test_merge_checkpoint_handoff.py` | 16 | 0 | 2 | 0 | 14 | 15 | 4.630 | owner unknown |
| `production/test_merge_checkpoint_history.py` | 12 | 0 | 0 | 0 | 12 | 12 | 3.471 | owner unknown |
| `production/test_merge_checkpoint_ledger.py` | 3 | 0 | 0 | 0 | 3 | 3 | 5.974 | owner unknown |
| `production/test_merge_checkpoint_promotion.py` | 35 | 0 | 14 | 0 | 21 | 27 | 19.300 | owner unknown |
| `production/test_merge_checkpoint_receipts.py` | 10 | 0 | 0 | 0 | 10 | 10 | 10.084 | owner unknown |
| `production/test_merge_checkpoint_rerun.py` | 12 | 0 | 4 | 0 | 8 | 10 | 4.458 | owner unknown |
| `production/test_merge_checkpoint_tree_cancel.py` | 5 | 0 | 0 | 0 | 5 | 5 | 2.181 | owner unknown |
| `production/test_merge_checkpoint_validation.py` | 22 | 0 | 0 | 0 | 22 | 22 | 4.736 | owner unknown |
| `production/test_merge_gate.py` | 35 | 0 | 0 | 0 | 35 | 35 | 13.713 | devtools-unit |
| `production/test_merge_gate_holds.py` | 7 | 0 | 0 | 0 | 7 | 7 | 5.383 | devtools-unit |
| `production/test_merge_gate_ledger.py` | 19 | 0 | 0 | 0 | 19 | 19 | 16.759 | devtools-unit |
| `production/test_merge_gate_promotion.py` | 25 | 0 | 2 | 0 | 23 | 24 | 15.465 | devtools-unit |
| `production/test_merge_gate_suites.py` | 20 | 0 | 0 | 0 | 20 | 20 | 6.408 | devtools-unit |
| `production/test_merge_gate_suites_admission.py` | 31 | 0 | 0 | 0 | 31 | 31 | 6.896 | devtools-unit |
| `production/test_merge_gate_suites_reruns.py` | 6 | 0 | 0 | 0 | 6 | 6 | 5.282 | devtools-unit |
| `production/test_merge_gate_suites_resources.py` | 17 | 0 | 0 | 0 | 17 | 17 | 7.978 | devtools-unit |
| `production/test_merge_gate_suites_signals.py` | 11 | 0 | 4 | 0 | 7 | 8 | 13.883 | devtools-unit |
| `production/test_merge_gate_suites_tripwires.py` | 7 | 0 | 0 | 0 | 7 | 7 | 2.960 | devtools-unit |
| `production/test_merge_gate_suites_types.py` | 23 | 0 | 0 | 0 | 23 | 23 | 7.631 | devtools-unit |
| `production/test_merge_gate_test_times.py` | 10 | 0 | 0 | 0 | 10 | 10 | 5.406 | devtools-unit |
| `production/test_next_item.py` | 21 | 0 | 0 | 0 | 21 | 21 | 1.380 | owner unknown |
| `production/test_production_doc.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.405 | owner unknown |
| `production/test_production_lifecycle.py` | 68 | 0 | 4 | 0 | 64 | 66 | 28.859 | owner unknown |
| `production/test_promote_showrunner.py` | 34 | 0 | 0 | 0 | 34 | 34 | 4.554 | owner unknown |
| `production/test_proposal_issue.py` | 51 | 0 | 0 | 0 | 51 | 51 | 0.705 | owner unknown |
| `production/test_proposals_folder.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.222 | owner unknown |
| `production/test_proposals_table.py` | 19 | 0 | 0 | 0 | 19 | 19 | 0.602 | owner unknown |
| `production/test_prune_phase_notes.py` | 5 | 0 | 0 | 0 | 5 | 5 | 1.350 | owner unknown |
| `production/test_rename.py` | 26 | 0 | 0 | 0 | 26 | 26 | 2.192 | owner unknown |
| `production/test_rename_finish.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.980 | owner unknown |
| `production/test_rename_rerun.py` | 1 | 0 | 0 | 0 | 1 | 1 | 1.033 | owner unknown |
| `production/test_rename_state.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.129 | owner unknown |
| `production/test_rename_unit.py` | 20 | 0 | 0 | 0 | 20 | 20 | 3.247 | owner unknown |
| `production/test_rename_unit_processes.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.809 | owner unknown |
| `production/test_rename_unit_sessions.py` | 1 | 0 | 0 | 0 | 1 | 1 | 1.508 | owner unknown |
| `production/test_rename_unit_style.py` | 1 | 0 | 0 | 0 | 1 | 1 | 1.273 | owner unknown |
| `production/test_resume_unit.py` | 30 | 0 | 0 | 0 | 30 | 30 | 3.629 | owner unknown |
| `production/test_retire_unit.py` | 34 | 0 | 0 | 0 | 34 | 34 | 6.870 | owner unknown |
| `production/test_review_pause.py` | 16 | 0 | 0 | 0 | 16 | 16 | 34.397 | owner unknown |
| `production/test_review_regime.py` | 8 | 0 | 0 | 0 | 8 | 8 | 0.141 | owner unknown |
| `production/test_roles_audit.py` | 52 | 0 | 2 | 0 | 50 | 51 | 8.312 | owner unknown |
| `production/test_session_labels.py` | 19 | 0 | 0 | 0 | 19 | 19 | 0.921 | owner unknown |
| `production/test_session_pane.py` | 12 | 0 | 0 | 0 | 12 | 12 | 3.046 | owner unknown |
| `production/test_session_staff.py` | 16 | 0 | 0 | 0 | 16 | 16 | 1.020 | owner unknown |
| `production/test_showrunner_move.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.267 | owner unknown |
| `production/test_showrunner_parent.py` | 8 | 0 | 0 | 0 | 8 | 8 | 2.337 | owner unknown |
| `production/test_showrunner_tree.py` | 18 | 0 | 0 | 0 | 18 | 18 | 9.615 | owner unknown |
| `production/test_showrunners.py` | 38 | 0 | 0 | 0 | 38 | 38 | 4.680 | owner unknown |
| `production/test_smoke_launch.py` | 2 | 0 | 2 | 0 | 0 | 1 | 1.499 | owner unknown |
| `production/test_staff.py` | 129 | 0 | 2 | 0 | 127 | 128 | 56.787 | owner unknown |
| `production/test_staff_accessors.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.219 | owner unknown |
| `production/test_staff_mac_copy.py` | 9 | 0 | 0 | 0 | 9 | 9 | 0.441 | owner unknown |
| `production/test_staff_move.py` | 10 | 0 | 0 | 0 | 10 | 10 | 3.086 | owner unknown |
| `production/test_staff_operation.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.644 | owner unknown |
| `production/test_staff_sources.py` | 51 | 0 | 0 | 0 | 51 | 51 | 12.527 | owner unknown |
| `production/test_staff_test_support.py` | 5 | 0 | 2 | 0 | 3 | 4 | 9.308 | owner unknown |
| `production/test_stall_watch.py` | 130 | 0 | 6 | 0 | 124 | 127 | 28.481 | owner unknown |
| `production/test_style_units.py` | 45 | 0 | 0 | 3 | 42 | 45 | 0.954 | owner unknown |
| `production/test_targeted_test.py` | 6 | 0 | 0 | 0 | 6 | 6 | unknown | owner unknown |
| `production/test_tidy_wrapped.py` | 18 | 0 | 0 | 0 | 18 | 18 | 19.417 | owner unknown |
| `production/test_transfer_unit.py` | 8 | 0 | 0 | 0 | 8 | 8 | 1.885 | owner unknown |
| `production/test_unit_command.py` | 24 | 0 | 0 | 0 | 24 | 24 | 1.152 | owner unknown |
| `production/test_unit_config.py` | 43 | 0 | 0 | 0 | 43 | 43 | 2.590 | owner unknown |
| `production/test_unit_effort.py` | 13 | 0 | 0 | 0 | 13 | 13 | 3.062 | owner unknown |
| `production/test_unit_lookup.py` | 67 | 0 | 0 | 0 | 67 | 67 | 8.848 | owner unknown |
| `production/test_unit_metadata.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.465 | owner unknown |
| `production/test_unit_move.py` | 62 | 0 | 9 | 0 | 53 | 56 | 5.746 | owner unknown |
| `production/test_unit_names.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.078 | owner unknown |
| `production/test_unit_operation_panes.py` | 6 | 0 | 0 | 0 | 6 | 6 | 1.138 | owner unknown |
| `production/test_unit_phases.py` | 19 | 0 | 2 | 0 | 17 | 18 | 0.786 | owner unknown |
| `production/test_unit_run.py` | 18 | 0 | 0 | 0 | 18 | 18 | 4.522 | owner unknown |
| `production/test_unit_status.py` | 9 | 0 | 2 | 0 | 7 | 8 | 6.983 | owner unknown |
| `production/test_unit_status_dailies.py` | 4 | 0 | 0 | 0 | 4 | 4 | 6.677 | owner unknown |
| `production/test_unit_status_decisions.py` | 5 | 0 | 2 | 0 | 3 | 4 | 6.983 | owner unknown |
| `production/test_unit_status_routing.py` | 5 | 0 | 0 | 0 | 5 | 5 | 5.912 | owner unknown |
| `production/test_unrouted_alert.py` | 19 | 0 | 2 | 0 | 17 | 18 | 0.516 | owner unknown |
| `production/test_update_registration.py` | 32 | 0 | 0 | 0 | 32 | 32 | 3.597 | owner unknown |
| `production/test_update_registration_answers.py` | 23 | 0 | 5 | 0 | 18 | 20 | 1.504 | owner unknown |
| `production/test_update_registration_commands.py` | 3 | 0 | 0 | 0 | 3 | 3 | 6.626 | owner unknown |
| `production/test_update_registration_log.py` | 6 | 0 | 0 | 0 | 6 | 6 | 1.055 | owner unknown |
| `production/test_update_registration_open.py` | 5 | 0 | 0 | 0 | 5 | 5 | 1.184 | owner unknown |
| `production/test_update_registration_questions.py` | 20 | 0 | 0 | 0 | 20 | 20 | 1.379 | owner unknown |
| `production/test_update_registration_review.py` | 7 | 0 | 0 | 0 | 7 | 7 | 1.032 | owner unknown |
| `production/test_waiting.py` | 35 | 0 | 0 | 0 | 35 | 35 | 34.223 | owner unknown |
| `production/test_worker.py` | 74 | 0 | 2 | 0 | 72 | 73 | 15.063 | owner unknown |
| `production/test_worker_admission.py` | 24 | 0 | 0 | 0 | 24 | 24 | 2.241 | owner unknown |
| `production/test_worker_fill.py` | 5 | 0 | 0 | 0 | 5 | 5 | 1.958 | owner unknown |
| `direct/test_assign_claude.py` | 19 | 0 | 0 | 0 | 19 | 19 | 46.231 | owner unknown |
| `direct/test_assign_claude_accounts.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.476 | owner unknown |
| `direct/test_assign_claude_claim_follow.py` | 2 | 0 | 0 | 0 | 2 | 2 | 5.808 | owner unknown |
| `direct/test_assign_claude_claims.py` | 6 | 0 | 0 | 0 | 6 | 6 | 14.131 | owner unknown |
| `direct/test_assign_claude_compaction.py` | 4 | 0 | 0 | 0 | 4 | 4 | 30.834 | owner unknown |
| `direct/test_assign_claude_door.py` | 3 | 0 | 2 | 0 | 1 | 2 | 8.289 | owner unknown |
| `direct/test_assign_claude_effort.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.190 | owner unknown |
| `direct/test_assign_claude_follow.py` | 8 | 0 | 0 | 0 | 8 | 8 | 30.220 | owner unknown |
| `direct/test_assign_claude_panes.py` | 4 | 0 | 0 | 0 | 4 | 4 | 9.942 | owner unknown |
| `direct/test_assign_claude_probes.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.258 | owner unknown |
| `direct/test_assign_claude_records.py` | 10 | 0 | 0 | 0 | 10 | 10 | 48.234 | owner unknown |
| `direct/test_assign_claude_workers.py` | 5 | 0 | 0 | 0 | 5 | 5 | 18.075 | owner unknown |
| `direct/test_assign_gate.py` | 11 | 0 | 2 | 0 | 9 | 10 | 16.716 | owner unknown |
| `direct/test_assign_launcher.py` * | 61 | 0 | 4 | 0 | 57 | 61 | 180.120 | owner unknown |
| `direct/test_assign_launcher_identity.py` * | 9 | 0 | 0 | 0 | 9 | 9 | 63.836 | owner unknown |
| `direct/test_assign_launcher_reuse.py` * | 13 | 0 | 2 | 0 | 11 | 13 | 74.558 | owner unknown |
| `direct/test_assign_launcher_review.py` * | 13 | 0 | 2 | 0 | 11 | 13 | 44.473 | owner unknown |
| `direct/test_board_json.py` | 88 | 0 | 0 | 0 | 88 | 88 | 3.486 | owner unknown |
| `direct/test_board_process_start.py` | 4 | 0 | 0 | 0 | 4 | 4 | 1.824 | owner unknown |
| `direct/test_board_reclaim.py` | 16 | 0 | 0 | 0 | 16 | 16 | 2.021 | owner unknown |
| `direct/test_board_start.py` | 3 | 0 | 3 | 0 | 0 | 1 | unknown | owner unknown |
| `direct/test_board_text.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.159 | owner unknown |
| `direct/test_claude_worker.py` | 9 | 0 | 0 | 0 | 9 | 9 | 21.053 | owner unknown |
| `direct/test_demo_refusals.py` | 2 | 0 | 2 | 0 | 0 | 1 | 9.107 | owner unknown |
| `direct/test_end_session.py` | 7 | 0 | 0 | 0 | 7 | 7 | 2.519 | owner unknown |
| `direct/test_findings.py` | 82 | 0 | 5 | 0 | 77 | 79 | 2.827 | owner unknown |
| `direct/test_findings_simplify.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.724 | owner unknown |
| `direct/test_launcher_modes.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.095 | owner unknown |
| `direct/test_launcher_waits.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.884 | owner unknown |
| `direct/test_memory_kill.py` | 9 | 0 | 0 | 0 | 9 | 9 | 0.122 | owner unknown |
| `direct/test_mesh_tree_cancel.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.906 | owner unknown |
| `direct/test_one_launch_path.py` | 8 | 0 | 0 | 0 | 8 | 8 | 8.748 | owner unknown |
| `direct/test_phase_artifacts.py` | 3 | 0 | 2 | 0 | 1 | 2 | 0.126 | owner unknown |
| `direct/test_phase_process.py` | 73 | 0 | 0 | 0 | 73 | 73 | 0.661 | owner unknown |
| `direct/test_phase_reservation.py` | 56 | 0 | 0 | 0 | 56 | 56 | 3.913 | owner unknown |
| `direct/test_phase_table.py` | 104 | 0 | 0 | 0 | 104 | 104 | 15.835 | owner unknown |
| `direct/test_plan_history.py` | 9 | 0 | 0 | 0 | 9 | 9 | 0.540 | owner unknown |
| `direct/test_progress_history.py` | 284 | 0 | 6 | 0 | 278 | 281 | 38.603 | owner unknown |
| `direct/test_recorder_fixture.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.472 | owner unknown |
| `direct/test_remove_run_scratch.py` | 10 | 0 | 0 | 0 | 10 | 10 | 2.850 | resources-unit |
| `direct/test_remove_workers.py` | 26 | 0 | 2 | 0 | 24 | 25 | 11.698 | owner unknown |
| `direct/test_rerun_lens.py` | 15 | 0 | 2 | 0 | 13 | 14 | 2.003 | owner unknown |
| `direct/test_review_launcher.py` | 18 | 0 | 0 | 0 | 18 | 18 | 22.311 | owner unknown |
| `direct/test_review_launcher_delivery.py` | 15 | 0 | 0 | 0 | 15 | 15 | 17.034 | owner unknown |
| `direct/test_review_launcher_records.py` | 10 | 0 | 0 | 0 | 10 | 10 | 17.723 | owner unknown |
| `direct/test_review_launcher_style.py` | 8 | 0 | 0 | 0 | 8 | 8 | 22.849 | owner unknown |
| `direct/test_review_launcher_trees.py` | 9 | 0 | 0 | 0 | 9 | 9 | 19.173 | owner unknown |
| `direct/test_review_launcher_ux.py` | 2 | 0 | 2 | 0 | 0 | 1 | 2.170 | owner unknown |
| `direct/test_simplify_lens.py` | 15 | 0 | 0 | 0 | 15 | 15 | 20.584 | owner unknown |
| `direct/test_simplify_result.py` | 9 | 0 | 0 | 0 | 9 | 9 | 0.549 | owner unknown |
| `direct/test_step_record.py` | 16 | 0 | 0 | 0 | 16 | 16 | 0.467 | verify-unit |
| `direct/test_step_record_cli.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.731 | owner unknown |
| `direct/test_tag_inventory.py` | 12 | 0 | 2 | 0 | 10 | 11 | 0.087 | owner unknown |
| `direct/test_tree_watch.py` | 13 | 0 | 0 | 0 | 13 | 13 | 1.443 | owner unknown |
| `direct/test_verify_admission_signals.py` | 1 | 0 | 0 | 0 | 1 | 1 | 2.924 | verify-unit |
| `direct/test_verify_argument_parser.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.107 | verify-unit |
| `direct/test_verify_build_key.py` | 7 | 0 | 0 | 0 | 7 | 7 | 3.212 | verify-unit |
| `direct/test_verify_check_no_library.py` | 2 | 0 | 0 | 0 | 2 | 2 | 2.688 | verify-unit |
| `direct/test_verify_checks_fail_closed.py` | 15 | 0 | 0 | 0 | 15 | 15 | 20.410 | verify-unit |
| `direct/test_verify_dev_feature_commands.py` | 3 | 0 | 0 | 0 | 3 | 3 | 12.497 | verify-unit |
| `direct/test_verify_dev_features.py` | 3 | 0 | 0 | 0 | 3 | 3 | 4.915 | verify-unit |
| `direct/test_verify_dev_features_defaults.py` | 1 | 0 | 0 | 0 | 1 | 1 | 9.015 | verify-unit |
| `direct/test_verify_dev_features_execution.py` | 2 | 0 | 0 | 0 | 2 | 2 | 8.555 | verify-unit |
| `direct/test_verify_doc.py` | 3 | 0 | 0 | 0 | 3 | 3 | 4.190 | verify-unit |
| `direct/test_verify_doc_checkout.py` | 2 | 0 | 0 | 0 | 2 | 2 | 5.534 | verify-unit |
| `direct/test_verify_doc_mac.py` | 2 | 0 | 0 | 0 | 2 | 2 | 7.654 | verify-unit |
| `direct/test_verify_doc_records.py` | 3 | 0 | 0 | 0 | 3 | 3 | 5.840 | verify-unit |
| `direct/test_verify_doc_recovery.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.404 | verify-unit |
| `direct/test_verify_example_acceptance.py` | 2 | 0 | 0 | 0 | 2 | 2 | 5.877 | verify-unit |
| `direct/test_verify_example_cache.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.502 | verify-unit |
| `direct/test_verify_example_commands.py` | 2 | 0 | 0 | 0 | 2 | 2 | 5.894 | verify-unit |
| `direct/test_verify_example_dependencies.py` | 2 | 0 | 0 | 0 | 2 | 2 | 7.051 | verify-unit |
| `direct/test_verify_example_edits.py` | 3 | 0 | 0 | 0 | 3 | 3 | 8.419 | verify-unit |
| `direct/test_verify_example_final.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.354 | verify-unit |
| `direct/test_verify_example_manifest.py` | 5 | 0 | 0 | 0 | 5 | 5 | 2.231 | verify-unit |
| `direct/test_verify_example_markers.py` | 5 | 0 | 2 | 0 | 3 | 4 | 2.002 | verify-unit |
| `direct/test_verify_final_pass.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.475 | verify-unit |
| `direct/test_verify_final_pass_commits.py` | 1 | 0 | 0 | 0 | 1 | 1 | 2.994 | verify-unit |
| `direct/test_verify_final_pass_compiler.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.051 | verify-unit |
| `direct/test_verify_final_pass_edits.py` | 1 | 0 | 0 | 0 | 1 | 1 | 2.952 | verify-unit |
| `direct/test_verify_final_pass_failure.py` | 1 | 0 | 0 | 0 | 1 | 1 | 2.504 | verify-unit |
| `direct/test_verify_final_pass_records.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.120 | verify-unit |
| `direct/test_verify_final_pass_script.py` | 3 | 0 | 0 | 0 | 3 | 3 | 5.732 | verify-unit |
| `direct/test_verify_final_pass_staged.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.085 | verify-unit |
| `direct/test_verify_final_pass_unstaged.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.054 | verify-unit |
| `direct/test_verify_final_pass_untracked.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.010 | verify-unit |
| `direct/test_verify_final_targets.py` | 7 | 0 | 0 | 0 | 7 | 7 | 12.471 | verify-unit |
| `direct/test_verify_hold_director.py` | 2 | 0 | 0 | 0 | 2 | 2 | 6.539 | verify-unit |
| `direct/test_verify_hold_events.py` | 8 | 0 | 0 | 0 | 8 | 8 | 2.104 | verify-unit |
| `direct/test_verify_hold_history.py` | 3 | 0 | 0 | 0 | 3 | 3 | 10.418 | verify-unit |
| `direct/test_verify_hold_lint.py` | 5 | 0 | 0 | 0 | 5 | 5 | 9.237 | verify-unit |
| `direct/test_verify_hold_mac.py` | 3 | 0 | 0 | 0 | 3 | 3 | 11.527 | verify-unit |
| `direct/test_verify_hold_memory.py` | 2 | 0 | 0 | 0 | 2 | 2 | 6.935 | verify-unit |
| `direct/test_verify_hold_signals.py` | 5 | 0 | 0 | 0 | 5 | 5 | 20.338 | verify-unit |
| `direct/test_verify_hold_steps.py` | 3 | 0 | 0 | 0 | 3 | 3 | 11.895 | verify-unit |
| `direct/test_verify_hold_wait.py` | 6 | 0 | 0 | 0 | 6 | 6 | 16.209 | verify-unit |
| `direct/test_verify_hold_workers.py` | 2 | 0 | 2 | 0 | 0 | 1 | 11.756 | verify-unit |
| `direct/test_verify_lint_cache.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.795 | verify-unit |
| `direct/test_verify_lint_cache_fix_limit.py` | 1 | 0 | 0 | 0 | 1 | 1 | 6.472 | verify-unit |
| `direct/test_verify_lint_cache_mend.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.710 | verify-unit |
| `direct/test_verify_lint_cache_token_loss.py` | 1 | 0 | 0 | 0 | 1 | 1 | 4.818 | verify-unit |
| `direct/test_verify_lint_cache_versions.py` * | 7 | 0 | 4 | 0 | 3 | 7 | 58.376 | verify-unit |
| `direct/test_verify_lint_edits.py` | 2 | 0 | 0 | 0 | 2 | 2 | 11.808 | verify-unit |
| `direct/test_verify_lint_fix_failures.py` | 1 | 0 | 0 | 0 | 1 | 1 | 7.879 | verify-unit |
| `direct/test_verify_lint_fix_limit.py` | 1 | 0 | 0 | 0 | 1 | 1 | 11.130 | verify-unit |
| `direct/test_verify_lint_fixes.py` | 1 | 0 | 0 | 0 | 1 | 1 | 10.976 | verify-unit |
| `direct/test_verify_lint_free_mac.py` | 4 | 0 | 0 | 0 | 4 | 4 | 31.630 | verify-unit |
| `direct/test_verify_lint_judgment.py` * | 16 | 0 | 0 | 0 | 16 | 16 | 63.368 | verify-unit |
| `direct/test_verify_lint_local_writes.py` | 2 | 0 | 2 | 0 | 0 | 1 | 8.915 | verify-unit |
| `direct/test_verify_lint_output.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.611 | verify-unit |
| `direct/test_verify_lint_scope.py` | 5 | 0 | 0 | 0 | 5 | 5 | 17.511 | verify-unit |
| `direct/test_verify_lint_selection.py` | 3 | 0 | 0 | 0 | 3 | 3 | 5.389 | verify-unit |
| `direct/test_verify_lint_taplo.py` | 11 | 0 | 0 | 0 | 11 | 11 | 11.785 | verify-unit |
| `direct/test_verify_lint_tool_failures.py` | 3 | 0 | 0 | 0 | 3 | 3 | 15.364 | verify-unit |
| `direct/test_verify_mac_arguments.py` | 4 | 0 | 0 | 0 | 4 | 4 | 20.597 | verify-unit |
| `direct/test_verify_mac_configuration.py` | 2 | 0 | 0 | 0 | 2 | 2 | 8.986 | verify-unit |
| `direct/test_verify_mac_configuration_errors.py` | 2 | 0 | 0 | 0 | 2 | 2 | 8.197 | verify-unit |
| `direct/test_verify_mac_copy_ownership.py` | 3 | 0 | 0 | 0 | 3 | 3 | 19.991 | verify-unit |
| `direct/test_verify_mac_copy_results.py` | 3 | 0 | 0 | 0 | 3 | 3 | 18.600 | verify-unit |
| `direct/test_verify_mac_disk_leave.py` | 4 | 0 | 0 | 0 | 4 | 4 | 24.374 | verify-unit |
| `direct/test_verify_mac_failed_status.py` | 1 | 0 | 0 | 0 | 1 | 1 | 7.338 | verify-unit |
| `direct/test_verify_mac_fallback.py` | 1 | 0 | 0 | 0 | 1 | 1 | 6.063 | verify-unit |
| `direct/test_verify_mac_fallback_announcements.py` | 1 | 0 | 0 | 0 | 1 | 1 | 20.083 | verify-unit |
| `direct/test_verify_mac_fallback_diagnostics.py` | 1 | 0 | 0 | 0 | 1 | 1 | 20.062 | verify-unit |
| `direct/test_verify_mac_fixture.py` | 5 | 0 | 0 | 0 | 5 | 5 | 1.984 | verify-unit |
| `direct/test_verify_mac_holder_history.py` | 1 | 0 | 0 | 0 | 1 | 1 | 22.039 | verify-unit |
| `direct/test_verify_mac_lifecycle.py` | 3 | 0 | 0 | 0 | 3 | 3 | 27.339 | verify-unit |
| `direct/test_verify_mac_lint_token.py` | 1 | 0 | 0 | 0 | 1 | 1 | 1.454 | verify-unit |
| `direct/test_verify_mac_local.py` | 1 | 0 | 0 | 0 | 1 | 1 | 6.858 | verify-unit |
| `direct/test_verify_mac_local_arguments.py` | 2 | 0 | 0 | 0 | 2 | 2 | 11.694 | verify-unit |
| `direct/test_verify_mac_local_release.py` | 1 | 0 | 0 | 0 | 1 | 1 | 18.855 | verify-unit |
| `direct/test_verify_mac_malformed.py` | 1 | 0 | 0 | 0 | 1 | 1 | 13.636 | verify-unit |
| `direct/test_verify_mac_memory_decline.py` | 1 | 0 | 0 | 0 | 1 | 1 | 6.264 | verify-unit |
| `direct/test_verify_mac_memory_excluded.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.602 | verify-unit |
| `direct/test_verify_mac_memory_handoff.py` | 1 | 0 | 0 | 0 | 1 | 1 | 19.929 | verify-unit |
| `direct/test_verify_mac_memory_retry.py` | 2 | 0 | 0 | 0 | 2 | 2 | 11.693 | verify-unit |
| `direct/test_verify_mac_memory_retry_clock.py` | 1 | 0 | 0 | 0 | 1 | 1 | 12.406 | verify-unit |
| `direct/test_verify_mac_offload.py` | 3 | 0 | 0 | 0 | 3 | 3 | 6.137 | verify-unit |
| `direct/test_verify_mac_only.py` * | 17 | 0 | 0 | 0 | 17 | 17 | 159.643 | verify-unit |
| `direct/test_verify_mac_pacing.py` | 2 | 0 | 0 | 0 | 2 | 2 | 15.716 | verify-unit |
| `direct/test_verify_mac_pressure.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.932 | verify-unit |
| `direct/test_verify_mac_release.py` | 2 | 0 | 0 | 0 | 2 | 2 | 17.898 | verify-unit |
| `direct/test_verify_mac_release_events.py` | 3 | 0 | 0 | 0 | 3 | 3 | 9.075 | verify-unit |
| `direct/test_verify_mac_result_paths.py` | 3 | 0 | 0 | 0 | 3 | 3 | 15.331 | verify-unit |
| `direct/test_verify_mac_result_shape.py` | 1 | 0 | 0 | 0 | 1 | 1 | 13.542 | verify-unit |
| `direct/test_verify_mac_result_status.py` | 1 | 0 | 0 | 0 | 1 | 1 | 13.579 | verify-unit |
| `direct/test_verify_mac_results.py` | 4 | 0 | 0 | 0 | 4 | 4 | 22.688 | verify-unit |
| `direct/test_verify_mac_retry_clock.py` | 3 | 0 | 2 | 0 | 1 | 2 | 18.410 | verify-unit |
| `direct/test_verify_mac_retry_hold.py` | 3 | 0 | 0 | 0 | 3 | 3 | 31.923 | verify-unit |
| `direct/test_verify_mac_routing.py` | 1 | 0 | 0 | 0 | 1 | 1 | 16.671 | verify-unit |
| `direct/test_verify_mac_routing_changes.py` | 1 | 0 | 0 | 0 | 1 | 1 | 19.163 | verify-unit |
| `direct/test_verify_mac_signal_boundaries.py` | 5 | 0 | 4 | 0 | 1 | 3 | 13.339 | verify-unit |
| `direct/test_verify_mac_signals.py` | 1 | 0 | 0 | 0 | 1 | 1 | 10.269 | verify-unit |
| `direct/test_verify_mac_wait_records.py` | 2 | 0 | 0 | 0 | 2 | 2 | 20.269 | verify-unit |
| `direct/test_verify_mac_withdrawal.py` | 2 | 0 | 0 | 0 | 2 | 2 | 15.374 | verify-unit |
| `direct/test_verify_memory_kill.py` | 16 | 0 | 5 | 0 | 11 | 13 | 2.885 | verify-unit |
| `direct/test_verify_metadata.py` | 6 | 0 | 0 | 0 | 6 | 6 | 1.969 | verify-unit |
| `direct/test_verify_nix_shell.py` | 7 | 0 | 0 | 0 | 7 | 7 | 0.962 | verify-unit |
| `direct/test_verify_python_starts.py` | 5 | 0 | 0 | 0 | 5 | 5 | 6.170 | verify-unit |
| `direct/test_verify_release_ack.py` | 7 | 0 | 0 | 0 | 7 | 7 | 16.269 | verify-unit |
| `direct/test_verify_result_environment.py` | 7 | 0 | 0 | 0 | 7 | 7 | unknown | verify-unit |
| `direct/test_verify_routing_rule.py` | 13 | 0 | 0 | 0 | 13 | 13 | unknown | verify-unit |
| `direct/test_verify_run_place.py` | 3 | 0 | 0 | 0 | 3 | 3 | 1.030 | verify-unit |
| `direct/test_verify_ssh_targets.py` | 13 | 0 | 5 | 0 | 8 | 10 | 22.936 | verify-unit |
| `direct/test_verify_ssh_targets_results.py` | 8 | 0 | 0 | 0 | 8 | 8 | 38.816 | verify-unit |
| `direct/test_verify_ssh_targets_retained.py` | 7 | 0 | 0 | 0 | 7 | 7 | 27.083 | verify-unit |
| `direct/test_verify_ssh_targets_retry.py` | 4 | 0 | 0 | 0 | 4 | 4 | 30.538 | verify-unit |
| `direct/test_verify_ssh_targets_routing.py` | 14 | 0 | 2 | 0 | 12 | 13 | 67.086 | verify-unit |
| `direct/test_verify_step_owner.py` | 5 | 0 | 0 | 0 | 5 | 5 | 3.833 | verify-unit |
| `direct/test_verify_step_refusal.py` | 1 | 0 | 0 | 0 | 1 | 1 | 1.535 | verify-unit |
| `direct/test_verify_step_routes.py` | 3 | 0 | 0 | 0 | 3 | 3 | 7.075 | verify-unit |
| `direct/test_verify_step_routes_helpers.py` | 2 | 0 | 0 | 0 | 2 | 2 | 11.527 | verify-unit |
| `direct/test_verify_step_routes_lint.py` | 2 | 0 | 0 | 0 | 2 | 2 | 11.308 | verify-unit |
| `direct/test_verify_step_routes_restart.py` | 2 | 0 | 0 | 0 | 2 | 2 | 8.220 | verify-unit |
| `direct/test_verify_step_routes_results.py` | 1 | 0 | 0 | 0 | 1 | 1 | 16.391 | verify-unit |
| `direct/test_verify_step_state.py` * | 6 | 0 | 0 | 0 | 6 | 6 | 53.020 | verify-unit |
| `direct/test_verify_target_platform.py` | 5 | 0 | 5 | 0 | 0 | 2 | 0.101 | verify-unit |
| `direct/test_verify_token_build.py` | 3 | 0 | 0 | 0 | 3 | 3 | 8.288 | verify-unit |
| `direct/test_verify_token_cancellation.py` | 2 | 0 | 0 | 0 | 2 | 2 | 3.877 | verify-unit |
| `direct/test_verify_token_check.py` | 1 | 0 | 0 | 0 | 1 | 1 | 8.878 | verify-unit |
| `direct/test_verify_token_detach.py` | 4 | 0 | 0 | 0 | 4 | 4 | 7.385 | verify-unit |
| `direct/test_verify_token_fifo.py` | 2 | 0 | 0 | 0 | 2 | 2 | 7.772 | verify-unit |
| `direct/test_verify_token_handoff.py` | 4 | 0 | 0 | 0 | 4 | 4 | 13.210 | verify-unit |
| `direct/test_verify_token_history.py` | 2 | 0 | 0 | 0 | 2 | 2 | 10.010 | verify-unit |
| `direct/test_verify_token_mac_step.py` | 5 | 0 | 2 | 0 | 3 | 4 | 29.103 | verify-unit |
| `direct/test_verify_token_memory_history.py` | 8 | 0 | 0 | 0 | 8 | 8 | 26.185 | verify-unit |
| `direct/test_verify_token_ownership.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.180 | verify-unit |
| `direct/test_verify_token_queue.py` | 2 | 0 | 0 | 0 | 2 | 2 | 13.273 | verify-unit |
| `direct/test_verify_token_reclaim.py` | 3 | 0 | 0 | 0 | 3 | 3 | 7.623 | verify-unit |
| `direct/test_verify_token_release.py` | 4 | 0 | 2 | 0 | 2 | 3 | 9.076 | verify-unit |
| `direct/test_verify_token_renew.py` | 14 | 0 | 0 | 0 | 14 | 14 | 37.787 | verify-unit |
| `direct/test_verify_token_reservation.py` | 4 | 0 | 0 | 0 | 4 | 4 | 14.361 | verify-unit |
| `direct/test_verify_token_same_slot.py` | 1 | 0 | 0 | 0 | 1 | 1 | 5.020 | verify-unit |
| `direct/test_verify_token_self_hold.py` | 2 | 0 | 0 | 0 | 2 | 2 | 1.641 | verify-unit |
| `direct/test_verify_token_signals.py` | 2 | 0 | 0 | 0 | 2 | 2 | 8.418 | verify-unit |
| `direct/test_verify_token_wait.py` | 6 | 0 | 0 | 0 | 6 | 6 | 17.768 | verify-unit |
| `direct/test_verify_tree_cancel.py` | 2 | 0 | 0 | 0 | 2 | 2 | 6.370 | verify-unit |
| `direct/test_verify_tree_cancel_history.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.826 | verify-unit |
| `direct/test_verify_tree_cancel_lint.py` | 6 | 0 | 0 | 0 | 6 | 6 | 24.791 | verify-unit |
| `direct/test_verify_tree_cancel_mac.py` | 2 | 0 | 0 | 0 | 2 | 2 | 9.252 | verify-unit |
| `direct/test_verify_tree_cancel_reacquire.py` | 2 | 0 | 0 | 0 | 2 | 2 | 5.821 | verify-unit |
| `direct/test_verify_untested_examples.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.251 | verify-unit |
| `direct/test_verify_verbs.py` | 10 | 0 | 0 | 0 | 10 | 10 | 0.942 | verify-unit |
| `direct/test_verify_wait_holder.py` | 29 | 0 | 2 | 0 | 27 | 28 | unknown | verify-unit |
| `direct/test_worker_state.py` | 19 | 0 | 2 | 0 | 17 | 18 | 0.534 | owner unknown |
| `direct/test_workers.py` | 43 | 0 | 0 | 0 | 43 | 43 | 27.050 | owner unknown |
| `agents/test_agent_exec.py` | 10 | 0 | 0 | 0 | 10 | 10 | 28.148 | owner unknown |
| `agents/test_claude_result.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.204 | owner unknown |
| `agents/test_codex_mesh.py` | 736 | 736 | 0 | 0 | 0 | 736 | 194.296 | owner unknown |
| `agents/test_heartbeat.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.207 | owner unknown |
| `agents/test_mesh_runs.py` | 66 | 0 | 0 | 0 | 66 | 66 | 13.235 | owner unknown |
| `agents/test_models_cache.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.956 | owner unknown |
| `agents/test_models_conf_publish.py` | 50 | 0 | 19 | 0 | 31 | 37 | 30.472 | owner unknown |
| `agents/test_no_old_model_names.py` | 2 | 0 | 0 | 0 | 2 | 2 | 2.370 | owner unknown |
| `lint/test_build_shape.py` | 25 | 0 | 0 | 0 | 25 | 25 | 0.122 | owner unknown |
| `lint/test_folder_audit.py` | 40 | 0 | 0 | 0 | 40 | 40 | 2.497 | resources-unit |
| `lint/test_invoke_progress_frames.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.106 | owner unknown |
| `lint/test_invoke_scope.py` | 28 | 0 | 3 | 0 | 25 | 26 | 3.238 | verify-unit |
| `lint/test_memory_admit.py` | 367 | 0 | 4 | 0 | 363 | 365 | 32.737 | resources-unit |
| `lint/test_memory_gate_wait.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.311 | owner unknown |
| `lint/test_memory_test_support.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.253 | owner unknown |
| `lint/test_nextest_budget.py` | 8 | 0 | 0 | 0 | 8 | 8 | 0.259 | verify-unit |
| `lint/test_nextest_width.py` | 11 | 0 | 2 | 0 | 9 | 10 | 0.390 | verify-unit |
| `lint/test_package_imports.py` | 31 | 0 | 14 | 0 | 17 | 19 | 0.387 | owner unknown |
| `lint/test_style_review.py` | 17 | 0 | 0 | 0 | 17 | 17 | 1.191 | owner unknown |
| `lint/test_sweep.py` | 330 | 0 | 5 | 0 | 325 | 326 | 16.051 | resources-unit |
| `lint/test_tmp_sweep.py` | 103 | 0 | 0 | 0 | 103 | 103 | 2.927 | resources-unit |
| `message/test_codex_daemon.py` | 17 | 0 | 0 | 0 | 17 | 17 | 0.511 | owner unknown |
| `message/test_escalate.py` | 31 | 0 | 0 | 0 | 31 | 31 | 13.898 | owner unknown |
| `message/test_install_codex_agents.py` | 11 | 0 | 0 | 0 | 11 | 11 | 0.926 | owner unknown |
| `message/test_notifier.py` | 34 | 0 | 0 | 0 | 34 | 34 | 31.939 | owner unknown |
| `message/test_postmaster.py` | 26 | 0 | 0 | 0 | 26 | 26 | 0.242 | owner unknown |
| `message/test_process_table.py` | 7 | 0 | 0 | 0 | 7 | 7 | 0.117 | owner unknown |
| `message/test_process_test_support.py` | 15 | 0 | 0 | 0 | 15 | 15 | 3.001 | owner unknown |
| `message/test_rca.py` | 24 | 0 | 0 | 0 | 24 | 24 | 6.993 | owner unknown |
| `message/test_repeat_guard.py` | 54 | 0 | 0 | 0 | 54 | 54 | 26.729 | owner unknown |
| `message/test_roster.py` | 130 | 0 | 4 | 0 | 126 | 128 | 23.243 | owner unknown |
| `message/test_send.py` | 313 | 0 | 2 | 0 | 311 | 312 | 28.688 | owner unknown |
| `message/test_send_budget.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.128 | owner unknown |
| `message/test_send_delivery.py` | 5 | 0 | 3 | 0 | 2 | 3 | 1.031 | owner unknown |
| `message/test_send_later.py` | 7 | 0 | 0 | 0 | 7 | 7 | 0.129 | owner unknown |
| `message/test_session_send.py` | 13 | 0 | 9 | 0 | 4 | 5 | 12.828 | owner unknown |
| `message/test_sessions.py` | 41 | 0 | 0 | 0 | 41 | 41 | 3.274 | owner unknown |
| `message/test_top_level.py` | 24 | 0 | 0 | 0 | 24 | 24 | 1.267 | owner unknown |
| `message/test_unit_labels.py` | 8 | 0 | 0 | 0 | 8 | 8 | 0.595 | owner unknown |
| `hooks/test_berth_hook.py` | 52 | 0 | 0 | 0 | 52 | 52 | 8.271 | owner unknown |
| `hooks/test_brp_launch_gate.py` | 20 | 0 | 2 | 0 | 18 | 19 | 13.209 | resources-unit |
| `hooks/test_build_hold_guard.py` | 29 | 0 | 0 | 0 | 29 | 29 | 3.154 | resources-unit |
| `hooks/test_change_review_gate.py` | 21 | 0 | 2 | 0 | 19 | 20 | 1.221 | owner unknown |
| `hooks/test_checkpoint_doc_gate.py` | 76 | 0 | 0 | 0 | 76 | 76 | 3.126 | verify-unit |
| `hooks/test_codex_hooks.py` | 32 | 0 | 0 | 0 | 32 | 32 | 25.342 | owner unknown |
| `hooks/test_conversation_pause.py` | 100 | 0 | 2 | 0 | 98 | 99 | 5.414 | owner unknown |
| `hooks/test_conversation_pause_processes.py` | 9 | 0 | 0 | 0 | 9 | 9 | 6.395 | owner unknown |
| `hooks/test_direct_command.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.079 | owner unknown |
| `hooks/test_direct_turn_wait_hooks.py` | 25 | 0 | 2 | 0 | 23 | 24 | 22.528 | owner unknown |
| `hooks/test_fn_length.py` | 51 | 0 | 15 | 0 | 36 | 43 | 0.614 | owner unknown |
| `hooks/test_herdr_tab_title.py` | 19 | 0 | 0 | 0 | 19 | 19 | 0.767 | owner unknown |
| `hooks/test_heredoc_guard.py` | 12 | 0 | 5 | 0 | 7 | 9 | 0.793 | owner unknown |
| `hooks/test_hook_dispatch.py` | 39 | 0 | 0 | 0 | 39 | 39 | 34.383 | owner unknown |
| `hooks/test_post_tool_use_banned_words.py` | 21 | 0 | 15 | 0 | 6 | 11 | 1.137 | owner unknown |
| `hooks/test_push_gate.py` | 23 | 0 | 0 | 0 | 23 | 23 | 0.276 | owner unknown |
| `hooks/test_scratch_guard.py` | 29 | 0 | 0 | 0 | 29 | 29 | 5.489 | resources-unit |
| `hooks/test_sendmessage_miss.py` | 13 | 0 | 0 | 0 | 13 | 13 | 4.766 | owner unknown |
| `hooks/test_session_start_pane_record.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.453 | owner unknown |
| `hooks/test_session_start_ste.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.600 | owner unknown |
| `hooks/test_session_temp_root.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.321 | resources-unit |
| `hooks/test_showrunner_compaction.py` | 8 | 0 | 0 | 0 | 8 | 8 | 0.866 | owner unknown |
| `hooks/test_showrunner_footer.py` | 7 | 0 | 0 | 0 | 7 | 7 | 0.937 | owner unknown |
| `hooks/test_showrunner_move_guard.py` | 8 | 0 | 0 | 0 | 8 | 8 | 0.741 | owner unknown |
| `hooks/test_staff_session_identity.py` | 16 | 0 | 0 | 0 | 16 | 16 | 0.599 | owner unknown |
| `hooks/test_stop_banned_words.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.404 | owner unknown |
| `hooks/test_stop_context_deferral.py` | 18 | 0 | 4 | 0 | 14 | 16 | 5.365 | owner unknown |
| `hooks/test_stop_direct_continue.py` | 21 | 0 | 4 | 0 | 17 | 19 | 5.970 | owner unknown |
| `hooks/test_tool_output_read.py` | 7 | 0 | 0 | 0 | 7 | 7 | 1.062 | owner unknown |
| `lib/test_agent_scope.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.117 | owner unknown |
| `lib/test_atomic_file.py` | 16 | 0 | 0 | 0 | 16 | 16 | 0.122 | owner unknown |
| `lib/test_berth_board.py` | 9 | 0 | 0 | 0 | 9 | 9 | 0.111 | owner unknown |
| `lib/test_build_folders.py` | 37 | 0 | 0 | 0 | 37 | 37 | 1.857 | owner unknown |
| `lib/test_cargo_output.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.120 | owner unknown |
| `lib/test_claude_launch_scan.py` | 11 | 0 | 4 | 0 | 7 | 9 | 10.330 | owner unknown |
| `lib/test_code_kind.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.078 | owner unknown |
| `lib/test_conf.py` | 44 | 0 | 0 | 0 | 44 | 44 | 0.707 | owner unknown |
| `lib/test_conf_sh.py` | 13 | 0 | 0 | 0 | 13 | 13 | 1.437 | owner unknown |
| `lib/test_detached.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.231 | owner unknown |
| `lib/test_file_lock.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.089 | owner unknown |
| `lib/test_file_module.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.086 | owner unknown |
| `lib/test_git_exclude.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.502 | owner unknown |
| `lib/test_hang_guard.py` | 0 | 0 | 0 | 0 | 0 | 0 | 0.086 | owner unknown |
| `lib/test_hang_guard_behavior.py` | 5 | 0 | 0 | 0 | 5 | 5 | 0.391 | owner unknown |
| `lib/test_main_publish.py` | 21 | 0 | 0 | 0 | 21 | 21 | 2.020 | owner unknown |
| `lib/test_main_sync.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.642 | owner unknown |
| `lib/test_models_registry.py` | 3 | 0 | 0 | 0 | 3 | 3 | 8.737 | owner unknown |
| `lib/test_models_registry_fixture.py` | 13 | 0 | 0 | 0 | 13 | 13 | 0.835 | owner unknown |
| `lib/test_names.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.122 | owner unknown |
| `lib/test_nix_env.py` | 43 | 0 | 2 | 0 | 41 | 42 | 2.684 | owner unknown |
| `lib/test_nix_env_integration.py` | 0 | 0 | 0 | 0 | 0 | 0 | 10.353 | owner unknown |
| `lib/test_nix_env_launches.py` | 18 | 0 | 0 | 0 | 18 | 18 | 8.143 | owner unknown |
| `lib/test_one_hang_guard.py` | 3 | 0 | 0 | 0 | 3 | 3 | 5.849 | owner unknown |
| `lib/test_package_root.py` | 5 | 0 | 0 | 0 | 5 | 5 | 3.365 | owner unknown |
| `lib/test_phase.py` | 0 | 0 | 0 | 0 | 0 | 0 | 0.078 | owner unknown |
| `lib/test_process_failure.py` | 7 | 0 | 2 | 0 | 5 | 6 | 5.087 | owner unknown |
| `lib/test_process_group.py` | 14 | 0 | 0 | 0 | 14 | 14 | 22.267 | owner unknown |
| `lib/test_process_start.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.161 | owner unknown |
| `lib/test_py_floor.py` | 7 | 0 | 4 | 0 | 3 | 4 | 9.232 | owner unknown |
| `lib/test_py_root.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.592 | owner unknown |
| `lib/test_py_shim.py` | 10 | 0 | 4 | 0 | 6 | 8 | 0.672 | owner unknown |
| `lib/test_relaunch.py` | 18 | 0 | 0 | 0 | 18 | 18 | 6.427 | owner unknown |
| `lib/test_scratch.py` | 66 | 0 | 0 | 0 | 66 | 66 | 1.484 | resources-unit |
| `lib/test_script_prompts.py` | 6 | 0 | 0 | 0 | 6 | 6 | 0.146 | owner unknown |
| `lib/test_session_paths.py` | 1 | 0 | 0 | 0 | 1 | 1 | 0.148 | owner unknown |
| `lib/test_socket_test_support.py` | 2 | 0 | 0 | 0 | 2 | 2 | 0.283 | owner unknown |
| `lib/test_terminal_cells.py` | 3 | 0 | 0 | 0 | 3 | 3 | 0.074 | queue-unit |
| `lib/test_tune.py` | 16 | 0 | 0 | 0 | 16 | 16 | 3.140 | owner unknown |
| `lib/test_unit_runs.py` | 30 | 0 | 0 | 0 | 30 | 30 | 0.636 | owner unknown |
| `lib/test_urgency.py` | 17 | 0 | 0 | 0 | 17 | 17 | 0.129 | owner unknown |
| `lib/test_user_zone.py` | 10 | 0 | 0 | 0 | 10 | 10 | 0.506 | owner unknown |
| `lib/test_zone.py` | 14 | 0 | 0 | 0 | 14 | 14 | 2.059 | owner unknown |

## Limits and next decisions

The ledger is complete for module names and declared method counts.
The statement comparison reads all modules.
It does not prove that every unlisted test is necessary.
Tests with different statement forms can still repeat behavior.
No such deletion has enough evidence here.

The five unmeasured modules are:

1. `direct/test_board_start.py`.
2. `direct/test_verify_result_environment.py`.
3. `direct/test_verify_routing_rule.py`.
4. `direct/test_verify_wait_holder.py`.
5. `production/test_targeted_test.py`.

Historical test counts differ from the current source in some modules.
For example, the staff history reports 127 methods.
The source has 129 methods.
The roster history reports 129 methods.
The source has 130 methods.
The package imports history has one selected run.
Its source also has more tests than that run.

The report reads per-test history for the two overlap modules and both agents candidates.
The history keeps only slow passing tests and non-passing results.
It supplies wall times, not CPU per candidate.
It cannot assign a module's CPU to the proposed table rows.

No CPU pair, process count comparison, or broken-function probe ran.
No test code changed, so no such gain is claimed.
A smaller method count must not be sold as a smaller test workload.

The director still needs ten candidate checks against the code.
The user still decides the Wording contract.
The director still assigns unknown behavior owners.
The retirement owner still proves that no supported behavior is lost.

The report supports 175 available method reductions with all cases retained.
It supports no immediate behavior deletion.
The conditional retirement has the largest gross CPU opportunity.
Its net saving remains unverified.
