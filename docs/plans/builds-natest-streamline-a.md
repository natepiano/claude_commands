# Phase 86: test streamlining, report A

Director note, 2026-11-09: a hierarchies phase deleted the retired module. Its per-test section is removed. The totals below still count its 736 tests.

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
| 1 | Done: a hierarchies phase deleted the retired module | — | 736 → 0 | Retired | — | 194.296 removed | 194.296 | hierarchies |
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

The run records were kept under that phase's run folder.
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
The session staff release test calls it with the shared fixture.
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
| `direct/test_detached_tree_cancel.py` | 1 | 0 | 0 | 0 | 1 | 1 | 3.906 | owner unknown |
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
| `agents/test_heartbeat.py` | 4 | 0 | 0 | 0 | 4 | 4 | 0.207 | owner unknown |
| `agents/test_detached_runs.py` | 66 | 0 | 0 | 0 | 66 | 66 | 13.235 | owner unknown |
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
