# builds-natest — Next

The phase backlog of natest-unit. Phases 117–119 are complete. Apply item 6a to compare removable CPU across modules and shared commands. Keep the current owner holds. No remaining import meets Phase 117's five-second threshold. Its final estimates include `simplify_daily` at 4.13 CPU s and `phase_reservation` at 3.99 CPU s per gate. Continue module and shared-command work without a general CPU stop rule.

## Order

Median CPU per gate, from `show.py top --limit 400` on 2026-11-06. The speed phases confirm their targets at dispatch.

1. Phases 65, 64, 66, 55, 63, 70 and 71 are complete, and they form one tip.
2. Phases 69, 44, 97 and 99 are complete. Phase 97: `copies/test_review_remote_copies.py`, 45.8 to 21.7 CPU s in pairs. Phase 99 reports `direct/test_assign_claude.py` at 49.3 to 14.9 CPU s in module_cost. Its pairs report 43.4 to 22.4 CPU s and 59.7 to 33.0 wall s. Phases 102-108 and 114-116 are complete. Phases 117–119 are complete; item 6a selects the next speed phase. Phase 108: `hooks/test_hook_dispatch.py`, 26.9 to 10.3 CPU s in warm pairs (62% less) and 36.7 to 12.2 with no bytecode writes. Apply item 6a to refresh the ranking and profile the highest eligible module. Keep the current production limits and follow item 6a without a CPU stop rule. Phase 100 is complete: `hooks/test_direct_turn_wait_hooks.py`, 26.1 to 3.9 CPU s in pairs. Phase 101 is complete: `herdr/test_herdr.py`, 26.0 to 5.7 CPU s in pairs (78% less).
3. Phase 67 follows Phase 82: lint judgment and the parser cases, previously 231 CPU s. Its required functions already exist on origin/builds. The four SSH target files, previously 146.1 CPU s, waited for verify's import and function changes (Phase 66 floor). Remeasured 2026-11-09 with `module_cost.py`, sequential, revision `5ff151123`: the four files total 92.0 CPU s (146.1 before), and routing is 47.9. Their `build_targets.py` starts run through `verify.sh`, so the import hold stays.
4. Phase 63 is complete. Phase 70 reports the token order hang; its cause stays open. Read the call output and queue files at its next recurrence.
5. Phase 72 complete; the lifecycle writer and the first admission stack stay unproved. Phase 73 makes no code change: verify-unit owns the surviving token-renewal delay, and the Mac case needs no edit because queue lacked Phase 63's marker wait.
6. Further `test_verify*` batches follow CPU rank. Measure with `module_cost.py --tests 10 --commands 10` before direct moves. Queue Phases 30-31 and verify Phase 41w are merged. The 2026-11-09 remeasure shows that the real `build_targets.py` commands in the SSH and wait-holder modules run through `verify.sh`, so their import hold stays. Hold routing decisions without a narrow function for verify's function change. These include Mac retry, fallback, queue-age and handoff cases. Profile doc, lint-scope and existing token-renewal function cases now. Their fixtures and function boundaries differ from the SSH fixtures. Include admission, setup and cleanup CPU. Give each shared fixture one owner. Each batch has one phase and one bumped checkpoint. Use `docs/plans/natedev-verify-test-cost.md` for the file list.
6a. Pick each speed phase by the CPU a test change can remove (showrunner, 2026-11-09). Refresh the CPU rank. Compare the highest eligible module with shared test command chains before selecting a phase. Attribute the measured 268 CPU s of fixture and stub `python -c` calls to their callers. Profile the leading candidates with `module_cost.py --tests 10 --commands 10`, and identify removable CPU while retaining required process boundaries. Select the largest measured removable cost. Open a phase only for a measured removable cost. Link modules with the same production limit to one owner proposal, and send each production-limit proposal to the showrunner for routing. Keep each module's behavior map and verification separate. Revisit a floor after its owner change lands. No CPU limit holds the plan. The mission is to take the next longest test each time. A module at its floor gets a Floors row or an owner proposal, and the next module follows. When neither modules nor shared test commands have removable CPU, take the item 11 rare-case cleanup.
   - Phase 95 complete: `direct/test_verify_wait_holder.py`, 94.1 to 81.5 CPU s in pairs, at its floor. Real target, reservation and recorder commands limit it. Remeasured 2026-11-09 at 74.0 CPU s; the import hold stays (Floors). verify-unit still owns the routing boundary proposal. Phase 102 already includes 41w. Keep the other verify modules as candidates under `docs/plans/natedev-verify-test-cost.md`, subject to refreshed CPU rank and their existing holds.
7. Phases 67, 68, 84, 85, 86, 88, 90, 91 and 92 are complete. Phase 86 reports no measured CPU gain from the table proposals in the two streamline reports. The codex_mesh deletion is merged; item 10 owns the remaining measurements. Phase 90: the six `LedgerTestCase` modules, 109.0 to 78.3 CPU s in pairs. Phase 91: the SSH result module, 59.9 to 49.9 CPU s. Phase 88: the staff modules 49.2 to 26.2 CPU s, add-unit 23.7 to 14.6, fast-track 66.6 to 58.4. Phase 92 reports a 27 GiB gate tmpfs reading from a suspected runaway verify module. Verify Phase 41r owns the fix. Phase 89 is complete at `d23c0bf81`: `direct/test_assign_launcher.py`, 47.2 to 26.0 CPU s in pairs, an accepted floor (Floors). Models-launch-settings phase 1 is merged at `6cde34726` and `b381eb355`. Keep 26.0 CPU s as the historical floor. Measure all four launcher modules across that change with fixed fixtures, `/tmp` roots and matching bytecode conditions.
8. Phase 59 complete: `style/test_style_proposals.py`, 53.75 to 28.51 CPU s in pairs; the rule-loading proposal is with the style owner.
9. Phase 74 complete: `copies/test_copy_guard.py`, 64.15 to 31.36 CPU s in pairs. Phase 75 complete: `production/test_add_unit.py`, 59.76 to 20.34 CPU s in pairs. Phase 76 complete: the verify hold and token wait fixtures make no live `/tmp/claude/unit` read. Phases 77, 78 and 79 are complete. Phase 77 records 53.25 CPU s for `style/test_crate_run.py`; Phase 78 records 28.9 CPU s for `production/test_staff.py`; Phase 79 reports 27.7 CPU s for `style/test_add_style.py`. `production/test_fast_track.py` (53.3) stays at its measured floor until its architecture proposal lands. `agents/test_agent_bg.py` (50.4) is removed with `agent_bg.sh`.

10. Locate the builds merge for hierarchies-unit Phase 30 deletion commit `ce529466f`. Builds revision `a87536252` and worktree merge `d7234eaac` contain the deletion. Compare the gate CPU and test count before and after that builds merge. Historical baseline: `agents/test_codex_mesh.py`, 736 tests and 194.3 CPU s median per gate (`show.py top`, 2026-11-07 14:50 EST). Also compare the `config/postmaster.conf` selection count and the cold-cache and warm-cache selection times. Phase 96 baseline: 574 of 738 modules selected; 572 reach `send_budget.py` and 570 reach `codex_mesh.py`. Keep the Phase 96 counts as history. Phase 127 applies the user's answer B (2026-11-10): select recorded readers, unrecorded modules, and modules whose own text names the config. Use the newest full, non-aborted gate with at least one config record.

11. Rule 2 cleanup (the user, 2026-11-07: "do rules 1 and 2"). After Phase 87 measures the yield, the cleanup takes the next modules by CPU s spent on rare-case tests: `production/test_dailies_render_holds.py`, `agents/test_models_conf_publish.py`, `direct/test_progress_history.py`, `lint/test_memory_admit.py`, then the rest. Skip modules that a speed phase holds.

`style/test_style_sync.py`, 66.6 CPU s, is free: the style unit's multi-unit hash reached main on 2026-11-09. Never: `types/basedpyright`, 238.4 CPU s.

`types/basedpyright` takes 2-4 min in a gate (report-unit, 2026-11-03). It is not in the gate's module files; Phase 2 records it, and it goes to the proposal below, never to a speed phase.

## Items to consider

- [x] **Shared test setup costs are measured across a whole gate before the next speed phase is picked.** Done 2026-11-09, 739 modules, natedev at low load, total 3,056 CPU s. Admission setup 28.0, admission loop 25.9, admission cleanup 3.1, process scans 24.8, process cleanup 24.0, seed copies 13.3. The highest eligible module is 50.3. Phase 113 takes the admission costs. The process scans stay: 77% is the kernel read of each environment.
  - Target: `scripts/build_queue/admission_test_support.py` and the shared verify fixture modules.
  - Why needed: `caller_admission()` starts a private server for each fixture, and shared verify cleanup reads process environments across `/proc`. A one-module order misses a cost split over many callers.
  - Completion condition: admission setup, admission cleanup, seed copies and process cleanup are measured apart from production commands, and each cost is summed across its callers and compared with the highest eligible module.
  - Revealed by: Phase 110 architect review. Showrunner answer: next (2026-11-09).
- [ ] **No test fails at cleanup because Git tidies a test repository in the background.**
  - Target: `scripts/test_times/module_run.py` and the gate test environment: set `maintenance.auto false` for every test process (for example through `GIT_CONFIG_COUNT`), not fixture by fixture.
  - Why needed: Phase 81 proved that `git commit` starts a detached `git maintenance run --auto`, and its repack writes into `.git` during `TemporaryDirectory` cleanup. `ImmutableGitSeed.create`, `CargoProjectTests`, `LifecycleFixture` and `MergeGateFixture` still commit in temporary repositories without stopping it.
  - Completion condition: a gate run sets the value for each test process, and the Phase 81 proof driver passes 100 repetitions with the fixture line removed.
  - Revealed by: Phase 81, 2026-11-07. Showrunner answer: next (2026-11-07).
- [x] **One path to `send.py`.** 29 production modules build their own `send.py` path in place of `session_send`. Sent to the showrunner on 2026-11-05 with the owner of each file; recommended owner: natedev-messaging. Phase 26 refuses the sender when `TEST_SENDER_GUARD` is set; the runner item below owns the guard for every module. No natest work remains on this caller cleanup.
- [ ] **The test runner guards every module from the real `send.py`.**
  - Target: `scripts/test_times/module_run.py`, `scripts/message/session_send_test_support.py`
  - Why needed: after Phase 26, only the four modules that call `forbid_real_sender()` are guarded, and the runner finds the marker through that module's stored record. A module that never installs the guard, or a marker variable inherited from a parent, is not checked (Phase 26 adversary round 2). No module in the gate takes either path today.
  - Completion condition: the runner sets one marker path before `unittest.main` for every module and checks that path after it; the four installs reuse it; the gate shows which modules, if any, start the real `send.py`.
  - Revealed by: Phase 26 adversary review round 2, 2026-11-05.
- [ ] **Report missed failures and excessive selection under the current targeted selector.**
  - Target: natest owns the selector audit; the showrunner owns its notice.
  - Record the selector revision, chosen base, changed paths, selected modules and measured CPU for each tested tree. Keep the id, host, revision, time and module records of the gate that supplied the config reads. Separate NULL records, empty lists and stems. Record newer gates without records that the selector skipped, and index errors that gave a full suite.
  - Compare omitted modules with full-gate results for that same tree.
  - Separate same-commit passes, nearby-commit failures and unresolved single-commit failures.
  - Separate full-suite passes, affected passes, zero-module affected passes and reused full passes. Compare coverage with a full run of the same tree. Report dirty runs without a tree hash as unresolved for same-tree comparison.
  - Audit both 107-module increases and the 264-module selection for `scripts/direct/verify.sh` and `scripts/direct/board.sh` (147 and 117 before the Phase 123 script-call closure).
  - Classify matches against repository files separately from names used only by fake commands.
  - Preserve selection of `direct/test_verify_lint_output.py`.
  - Audit the 26 direct repository scanners and actual scan use through the four identified support modules. The 493 importers do not prove 493 executed scans.
  - Known miss (sdlc, 2026-11-10): hierarchies added `scripts/message/failed_lookup.py`, which imports staff. The targeted test did not select `production/test_staff_accessors.py`, which checks that `docs/as-built/staff-accessors.md` lists every staff importer. The full merge gate caught it. A new or changed file whose text matches a scanner's pattern must select that scanner.
  - Document `targeted_test.py --full` as the available full-coverage check for scan-dependent changes.
  - Phase 129 made the nine raw config readers record their reads (`scripts/lib/conf_record.sh` for shell). Raw reads that still record nothing: `production/dailies_counts.py` `read_specs`, generic file hashing in `direct/tree_watch.py` and `direct/progress_history.py`, and fixture readers in five `*_test_support.py` files.
  - Classify the three failures that Phase 127's targeted run missed and its full gate found: `aws_target/test_admin_secrets.py`, `whoami/test_profile_notes_pass.py`, `usage/test_context_economy_read.py`. Record why each was not selected.
  - Completion condition: after a day on builds, report missed failures, confirmed unnecessary selections and their measured CPU.
  - Submit any selector change with its coverage cases for separate approval.
  - Keep the Phase 37 estimates and Phase 112 replay counts as history. Phase 127's replay (17,861 to 10,042 CPU s over 19 merges) is an estimate from one current graph, one set of config records and saved module CPU. Compare it with measured selection time and executed test CPU. The merge gate continues to run all modules.
  - Revealed by: Phases 111-116.
- [ ] **A folder scan in a child process selects its test.**
  - Target: `scripts/test_times/module_run.py` and the selector in `scripts/production/affected_modules.py`.
  - Why needed: Phase 130 records folder listings only in the test process. A test that starts a tool which lists repository folders records nothing, so a change in those folders does not select it. Git listing commands other than `ls-files` are not recorded either.
  - Completion condition: name the tests whose child processes list repository folders, with their CPU, and record those listings or report why each cannot be recorded.
  - Revealed by: Phase 130 design. Builds showrunner, 2026-11-11: keep it as a named item.
- [ ] **Architecture proposal: the assign launcher starts fewer child processes**
  - Target: `scripts/direct/assign.py` for recorder coordination, owned by sdlc. Target: `scripts/lib/models_registry.py:launch_settings` and `scripts/agents/models_config.sh` for resolver cost, owned by the models unit. Retain the comparable-build measurements across all four launcher modules. The Phase 89 prototype gain of 14.9% is one pair, measured before the port onto builds.
  - Why needed: the current coordinator starts recorder commands and a shell resolver. The codex_mesh deletion removes the follow commands from the Phase 54 profile. Phase 99's adapter changes test execution only. Profile the current production commands before using the historical gain estimates.
  - Accepted by sdlc on 2026-11-06: devtools runs the recorder and resolver calls in the coordinator process after the agreed dependencies reach main.
  - Completion condition: measure all four assign launcher modules before and after devtools' production change on comparable builds revisions. Record the fixture implementation for each measurement. Keep Phase 68 figures as history and report production gains separately from fixture gains. Update the Floors rows with measured CPU and the remaining limiting operation.
  - Revealed by: Phase 54 profile, 2026-11-06. Updated after Phase 99 and the codex_mesh deletion.
- [x] **The default merge gate omits the type check.**
  - `scripts/production/merge_gate.py` checks types nightly or when the caller supplies `--type-dirs`.
  - Phase 110 excludes the type check from module ranks and reports its latest passed time in a footnote.
  - No ordinary merge-gate type cost remains to reduce under this item.
- [ ] **Architecture proposal: stale module keys in the gate state**
  - Target: `scripts/production/merge_gate.py` (owner sdlc devtools-unit)
  - Why needed: `module_seconds.json` keeps 728 keys for 664 test files; old `delegate/*` keys remain after the folder moved. Memory admission reads the peak file the same way.
  - Completion condition: send the showrunner a proposal to drop keys whose module file is gone, with the key counts.
  - Revealed by: plan research, 2026-11-03.
- [x] **Index isolation shares production imports and keeps separate test imports.**
  - Target: `scripts/buildlog/test_index.py` — `LogIsolationTests.test_each_module_uses_a_temporary_log`.
  - Completed by Phase 114: one parent imports the production modules and forks one child per test module. Each child imports its test module and keeps both existing assertions.
  - Same-load paired module CPU falls from 30.7 to 12.0 seconds. Module wall time rises from 7.1 to 16.0 seconds because the children run serially.
  - Keep the eight-worker measurements of Phase 28 as history.
  - Revealed by: Phase 3; updated after Phase 114.
- [x] **The simplify-lens report validation test starts fewer processes and keeps its recorder checks.** The daily simplify work removed this test's cost (Phase 51 as-built).
  - Target: `scripts/direct/test_simplify_lens.py` — `test_report_validation_trims_whitespace_and_requires_pass_or_a_numbered_item` (25 s, 8 review-launcher runs)
  - Why needed: Phase 6 left this test as the module's largest cost. Each of the 8 runs checks the report result, the saved status and the review record's `status` and `raised` fields, so a parse-only call cannot replace them.
  - Completion condition: every existing check stays; the needed process boundaries stay; same-load pairs show the gain.
  - Revealed by: Phase 6; showrunner ruling 2026-11-04; the Phase 11 architect review.
- [ ] **Each improved module keeps a reference time.**
  - Target: `scripts/test_times/show.py`, `config/test_times.conf`
  - Why needed: the gate's slowdown warning compares with a rolling median, which slowly accepts a slowdown as normal (Astra plan review, 2026-11-04).
  - Completion condition: store each improved module's after median, CPU time, revision, load, filesystem roots and bytecode cache conditions. Compare gate readings only under comparable conditions. Report a confirmed increase above 20% at phase start, and investigate it before another speed phase is selected.
  - Revealed by: Astra plan review; starts after Phase 14 lands (showrunner ruling 2026-11-04).
- [x] **`mac_run.sh` uses the sourced configuration reader.**
  - Target: `scripts/production/mac_run.sh` (recent owner: builds-resources)
  - Why needed: `config_value()` starts `python3 conf.py get` once for each key. `test_mac_run.py` makes 339 such starts (23.5 s traced of a 49.5 s traced run); they set the Phase 29 floor.
  - Completed by Phase 30 (2026-11-05): `mac_run.sh` sources `conf.sh`. Python starts per run 18 -> 1; paired module medians 34.6 s -> 20.5 s.
- [ ] **Measure the remaining Mac claim and release cost**
  - Target: `scripts/mac_test/mac_test.py` and `scripts/mac_test/offload.py`; preserve their current owner holds.
  - Completed operations: repository identity uses the shared repository module; admission and sweep imports load inside their consuming functions. Phase 117 delays the `offload` and `buildlog.sync` imports in `mac_test.py`.
  - History: the Phase 30 profile at load 10 records claim 334.5 ms and release 276.6 ms, with an estimated gain of 200-260 ms for each pair.
  - Measure current claim and release command CPU before proposing another change. Route any remaining production change through its owner. Give `offload.py` one writer.
  - Completion condition: report the measured remaining cost and any owner proposal, and update affected caller measurements under matching conditions.
  - Revealed by: Phase 29 trace and Phase 30 profile, 2026-11-05. Updated after Phase 117.
- [ ] **Architecture proposal: one injected Git runner in fast_track.py**
  - Target: `scripts/production/fast_track.py` (owner: production) and `scripts/production/test_fast_track.py`
  - Why needed: Phase 33 measured 2,937 Git calls that take 27.6 s. The module's paired median is 97.9 s.
  - Completion condition: decision tests run against the runner; a few process tests keep real Git; same-load pairs show the real gain.
  - Risk: a recorded Git transcript is a second model of Git that can drift from real Git (showrunner, 2026-11-04).
  - Revealed by: Phase 7; showrunner ruling 2026-11-04 (next, not current).
- [ ] **Measure `test_verify_lint_judgment.py` again when its owners' changes land.**
  - Target: `direct/test_verify_lint_judgment.py` (198.6 s after Phase 31)
  - Queue Phase 13 completed the admission start-spacing fix. Measure both lint modules on the merged code. Owners (showrunner, 2026-11-05): queue-unit keeps the lighter admission client start (`scripts/lint/memory_gate.sh:17-22`) and the `build_hold.py` import work. verify-unit keeps step-record state reuse (`verify.sh:356-358`, `verify.sh:1925-1934`), then a callable repair decision (`verify.sh:3711-3714`).
  - Phase 45 recorded 486 step-record starts and 23.1 of 94.0 CPU s in `direct/test_verify_lint_cache_versions.py`. Natest pairs both lint modules after verify-unit lands the step-record reuse.
  - Completion condition: same-load pairs on builds after each change lands; the Floors row moves to the new median.
  - Revealed by: Phase 31, 2026-11-05.
- [x] **Document the gate's discover form without PYTHONPATH.** Phase 34 corrected the Mac instructions. Every code runner already uses discover. The report unit owns the `docs/plans/builds-report.md:76` command. No package or import rewrite remains.
- [ ] **Three `scripts/lib` helpers named like tests hold no tests.**
  - Target: `scripts/lib/test_hang_guard.py` (286 importing files), `scripts/lib/test_models_registry.py` (30), `scripts/lib/test_phase.py` (5)
  - Why needed: each name matches `test_*.py`, so a run of the module alone reports "NO TESTS RAN" and exits 5. It reads as a failed test module (Phase 34 survey).
  - Proposal: rename each to a `*_test_support.py` name and switch every importer in one change. Do not rename until the showrunner places it (showrunner, 2026-11-05).
  - Completion condition: no `test_*.py` file under `scripts/lib` is without tests; every importer passes. Add no new check.
  - Revealed by: Phase 34 survey, 2026-11-05.
- [ ] **Route and measure the verify helper-chain proposals.**
  - Target: queue-unit for `admission_client.py`; verify-unit for `build_targets.py` and `verify.sh`; route direct and buildlog helper changes through the showrunner.
  - Evidence: the helper chain costs about 540 CPU s per measured gate. This total includes its component commands.
  - Admission client: 129 CPU s across 910 starts. Queue-unit assesses the wire-format-only client path.
  - Build targets: 117 CPU s across 786 starts. Each start averages 31 ms of imports and 118 ms of work.
  - Profile repeated build-target work before proposing another import change.
  - Verify-unit assesses one helper process for the direct, buildlog and build-folder operations that `verify.sh` starts separately.
  - Queue Phases 30–31 removed the old admission imports. Keep the earlier measurements as history.
  - Per-helper trial (Phase 114): with `record.py` or `progress_history.py` as a no-op the tests stay green, 171 CPU s per gate. `tree_watch` fails four lint scope tests, 66 CPU s. `step_record` is required.
  - Builds decided against a test-only path in `verify.sh`. Verify measures after its Phase 42e and moves `progress_history.py` into the helper process if it still costs real CPU.
  - Completion condition: record each owner proposal and landing revision. Measure affected callers before and after each landing under matching conditions.
  - Report CPU and process counts, then update the affected Floors rows. Do not add overlapping command costs.
  - Revealed by: Phase 24, 2026-11-05. Updated from the whole-gate measurement, 2026-11-09.

- [x] **Phase notes stopped updating.** Text: `phase table not written: phase note target is not owned by builds/natest-unit: ~/rust/hanadocs/showrunners/builds/natest.md`. Cause: `_note_ownership` in `scripts/direct/phase_table.py` looks up the note's `unit:` field as a staff id, and the notes still hold the older `unit: natest-unit` header (queue, report and fastest also). So each write refuses. Fix: run the one-off `phase_table.py migrate-notes`, which reads `*/*.md` below `--vault-root` and rewrites the header to the staff id. Done 2026-11-05: the showrunner assigned the builds notes to natest; `phase_table.py migrate-notes` rewrote natest, queue, report, resources and verify. natedev has the fastest note.

## Floors

Each floor row states its measurement conditions: revision, filesystem roots and bytecode cache (showrunner, 2026-11-09). Compare a gate reading with a floor only under the same conditions. The older rows are wall floors from before 2026-11-06, not CPU floors. A wall floor does not hold a module back when its CPU rank calls it and direct calls were not tried. The `direct/test_verify*` rows go to the batches.

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `production/test_fast_track.py` | Historical wall median: 97.9 s in Phase 33; Phase 88 reports 58.4 CPU s | Real Git and remaining child processes. Phase 87 removed the blocked-hook guard. Profile current fixture, Git and child-process CPU before confirming a floor. | 33, 87, 88 |
| `direct/test_verify_ssh_targets_routing.py` | 81.6 | `eligible` and `reserve` process startup after queue Phases 30–31 removed the admission imports. Remeasured 2026-11-09 with `module_cost.py`, sequential, revision `5ff151123`: 47.9 CPU s; `build_targets.py` makes 49 starts for 15.9 s through `verify.sh` | 24 |
| `style/test_crate_run.py` | Phase 77 CPU floor: 53.25 s | Production Git wrappers cost 40.8 CPU s; natedev-style owns reuse of the validated checkout | 77 |
| `direct/test_verify_mac_only.py` | 185.8 (load 52-70) | Real `build_targets.py` processes through `verify.sh`. Queue Phases 30–31 removed the old admission imports. Profile current startup and fixture CPU before confirming this floor. | 39, 41, 42 |
| `direct/test_verify_ssh_targets_retained.py` | 35.5 (load 52-65) | child process starts and the coordination imports through `build_hold.py`; no fixed waits remain. Remeasured 2026-11-09: 14.1 CPU s | 56 |
| `direct/test_verify_ssh_targets_retry.py` | 42.1 (load 52-65) | the same starts and imports; `recovery_start_seconds` stays 5 s. Remeasured 2026-11-09: 17.9 CPU s | 56 |
| `direct/test_verify_wait_holder.py` | Phase 95 CPU floor: 81.5 s, revision 409c83c62 | Real target, reservation and recorder commands. The import changes are merged (queue Phases 30-31, verify Phase 41w). Remeasured 2026-11-09 with `module_cost.py`, sequential, revision `5ff151123`: 74.0 CPU s. The profile records 88 `progress_history.py` starts costing 25.2 s and 53 `build_targets.py` starts costing 18.0 s. Target calls run through `verify.sh`. Profile the extracted memory and token recorder cases separately before confirming their floor. verify-unit owns the routing boundary | 95 |
| `direct/test_assign_launcher.py` | Phase 89 CPU floor: 26.0 s pair median, revision d23c0bf81, /tmp roots, default bytecode cache | `launch_settings` started a shell and a Python registry command for each launch. The models resolver change is merged at `6cde34726` and `b381eb355`. Remeasured 2026-11-09 with `module_cost.py`, sequential, revision `5ff151123`: 24.0 CPU s; profile remaining test costs under item 6a; reuse 3.0, identity 2.5, review 2.1 | 89 |
| `direct/test_verify_result_environment.py` | Phase 102 CPU floor: 37.7 s pair median, revision 60c5a1b9d, /tmp roots, default bytecode cache, 16 real `verify.sh` calls. Worker pairs: 70.5 s on /dev/shm roots with an empty bytecode cache | Real `verify.sh` routing, admission, key and record processes; verify-unit owns the Phase 95 target-selection, recorder import and routing boundary proposals. Revisit after those changes land | 102 |
| `review_links/test_change_page.py` | Phase 107 CPU floor: 10.388 s; base `ea13a357b`, /dev/shm roots, empty bytecode prefix, writes disabled. Warm measurement: 7.004 CPU s; `/tmp` ext4 roots; private bytecode prefix; one full warmup; writes enabled. Both retain 879 processes. | Six `change_page.py` CLI starts prove exit status, saved state across restarts, and publication from startup paths. The sdlc in-process verdict change removed all `change_review.py` and `feature_log.py` starts. Revisit after sdlc reduces CLI startup cost. | 107 |
| `lint/test_memory_admit.py` | Phase 106 CPU floor: 10.2 s pair median, base `8cd6189b8`, /tmp roots, warm bytecode cache (19.5 before, 48% less). With no bytecode writes on /dev/shm roots: 26.7 to 10.1 s (62% less). Worker pairs with an empty bytecode prefix: 40.5 to 24.2 s (40% less) | Real admission client startup at shell boundaries. Queue-unit owns the lighter client and source-stamp proposal. Revisit after those changes reach builds. | 106 |
