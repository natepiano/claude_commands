# builds-natest — Next

The phase backlog of natest-unit. At each phase end, rank modules by median CPU with `python3 ~/.claude/scripts/test_times/show.py top --repo ~/.claude`. The next speed phase takes the highest ranked live module that no phase holds, subject to the floor rule.

## Order

Median CPU per gate, from `show.py top --limit 400` on 2026-11-06. The speed phases confirm their targets at dispatch.

1. Phases 65, 64, 66, 55, 63, 70 and 71 are complete, and they form one tip.
2. Phases 69, 44, 97 and 99 are complete. Phase 97: `copies/test_review_remote_copies.py`, 45.8 to 21.7 CPU s in pairs. Phase 99 reports `direct/test_assign_claude.py` at 49.3 to 14.9 CPU s in module_cost. Its pairs report 43.4 to 22.4 CPU s and 59.7 to 33.0 wall s. Refresh the CPU ranking after Phase 102 closes. Exclude modules with an accepted floor until their stated revisit condition occurs. Add the next eligible speed phase before the last todo phase starts. Phase 100 is complete: `hooks/test_direct_turn_wait_hooks.py`, 26.1 to 3.9 CPU s in pairs. Phase 101 is complete: `herdr/test_herdr.py`, 26.0 to 5.7 CPU s in pairs (78% less).
3. Phase 67 follows Phase 82: lint judgment and the parser cases, previously 231 CPU s. Its required functions already exist on origin/builds. The four SSH target files, previously 146.1 CPU s, still wait for verify's remaining import and function changes (Phase 66 floor).
4. Phase 63 is complete. Phase 70 reports the token order hang; its cause stays open. Read the call output and queue files at its next recurrence.
5. Phase 72 complete; the lifecycle writer and the first admission stack stay unproved. Phase 73 makes no code change: verify-unit owns the surviving token-renewal delay, and the Mac case needs no edit because queue lacked Phase 63's marker wait.
6. Further `test_verify*` batches follow CPU rank. Measure with `module_cost.py --tests 10 --commands 10` before direct moves. Hold cases dominated by real `build_targets.py` commands for verify's import change. Hold routing decisions without a narrow function for verify's function change. These include Mac retry, fallback, queue-age and handoff cases. Profile doc, lint-scope and existing token-renewal function cases now. Their fixtures and function boundaries differ from the SSH fixtures. Include admission, setup and cleanup CPU. Give each shared fixture one owner. Each batch has one phase and one bumped checkpoint. Use `docs/plans/natedev-verify-test-cost.md` for the file list.
6a. Pick each speed phase by the CPU a test change can remove (showrunner, 2026-11-09). Refresh the CPU rank. Profile the highest eligible module with `module_cost.py --tests 10 --commands 10` and separate fixture cost from required production cost. Open a phase only for a measured removable cost. Link modules with the same production limit to one owner proposal, and send each production-limit proposal to the showrunner for routing. Keep each module's behavior map and verification separate. Revisit a floor after its owner change lands. If no eligible module is above 30 CPU s, hold the plan and tell the showrunner. A script that reads this stop line takes it from config.
   - Phase 95 complete: `direct/test_verify_wait_holder.py`, 94.1 to 81.5 CPU s in pairs, at its floor. Real target, reservation and recorder commands limit it. verify-unit owns the target-selection and recorder import proposals (one phase after its 41u) and the routing boundary proposal (its next file). Keep the other verify modules as candidates under `docs/plans/natedev-verify-test-cost.md`, subject to refreshed CPU rank and their existing holds.
7. Phases 67, 68, 84, 85, 86, 88, 90, 91 and 92 are complete. Phase 86 reports no measured CPU gain from the table proposals in the two streamline reports. The codex_mesh deletion is merged; item 10 owns the remaining measurements. Phase 90: the six `LedgerTestCase` modules, 109.0 to 78.3 CPU s in pairs. Phase 91: the SSH result module, 59.9 to 49.9 CPU s. Phase 88: the staff modules 49.2 to 26.2 CPU s, add-unit 23.7 to 14.6, fast-track 66.6 to 58.4. Phase 92 reports a 27 GiB gate tmpfs reading from a suspected runaway verify module. Verify Phase 41r owns the fix. Phase 89 is complete at `d23c0bf81`: `direct/test_assign_launcher.py`, 47.2 to 26.0 CPU s in pairs, an accepted floor (Floors). The remaining resolver cost belongs to the models unit: models-launch-settings phase 1 took the proposal (showrunner, 2026-11-09). Revisit after that phase lands.
8. Phase 59 complete: `style/test_style_proposals.py`, 53.75 to 28.51 CPU s in pairs; the rule-loading proposal is with the style owner.
9. Phase 74 complete: `copies/test_copy_guard.py`, 64.15 to 31.36 CPU s in pairs. Phase 75 complete: `production/test_add_unit.py`, 59.76 to 20.34 CPU s in pairs. Phase 76 complete: the verify hold and token wait fixtures make no live `/tmp/claude/unit` read. Phases 77, 78 and 79 are complete. Phase 77 records 53.25 CPU s for `style/test_crate_run.py`; Phase 78 records 28.9 CPU s for `production/test_staff.py`; Phase 79 reports 27.7 CPU s for `style/test_add_style.py`. `production/test_fast_track.py` (53.3) stays at its measured floor until its architecture proposal lands. `agents/test_agent_bg.py` (50.4) is removed with `agent_bg.sh`.

10. Locate the builds merge for hierarchies-unit Phase 30 deletion commit `ce529466f`. Builds revision `a87536252` and worktree merge `d7234eaac` contain the deletion. Compare the gate CPU and test count before and after that builds merge. Historical baseline: `agents/test_codex_mesh.py`, 736 tests and 194.3 CPU s median per gate (`show.py top`, 2026-11-07 14:50 EST). Also compare the `config/postmaster.conf` selection count and the cold-cache and warm-cache selection times. Phase 96 baseline: 574 of 738 modules selected; 572 reach `send_budget.py` and 570 reach `codex_mesh.py`. Keep the approved transitive closure (the user, 2026-11-07).

11. Rule 2 cleanup (the user, 2026-11-07: "do rules 1 and 2"). After Phase 87 measures the yield, the cleanup takes the next modules by CPU s spent on rare-case tests: `production/test_dailies_render_holds.py`, `agents/test_models_conf_publish.py`, `direct/test_progress_history.py`, `lint/test_memory_admit.py`, then the rest. Skip modules that a speed phase holds and `agents/test_codex_mesh.py`, which hierarchies deletes.

Held: `style/test_style_sync.py`, 66.6 CPU s; devtools holds it. Never: `types/basedpyright`, 238.4 CPU s; `agents/test_codex_mesh.py`, 232.6 CPU s, retires.

`types/basedpyright` takes 2-4 min in a gate (report-unit, 2026-11-03). It is not in the gate's module files; Phase 2 records it, and it goes to the proposal below, never to a speed phase.

## Items to consider

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
- [ ] **Report the red merges that a unit targeted test missed.**
  - Target: the showrunner's notice, from the merge gate records.
  - Why needed: Phase 37's selection is outside the K row, because the merge gate runs all modules. The estimate is about 7 more red merges per 104 runs. Rank by CPU per gate settled the target order (the user, 2026-11-06).
  - Completion condition: after a day on builds, report the count against the 104-run estimate of 15.
  - Revealed by: Phase 37 architect review, 2026-11-06.
- [ ] **Architecture proposal: the assign launcher starts fewer child processes**
  - Target: `scripts/direct/assign.py` for recorder coordination, owned by sdlc. Target: `scripts/lib/models_registry.py:launch_settings` and `scripts/agents/models_config.sh` for resolver cost, owned by the models unit. Retain the comparable-build measurements across all four launcher modules. The Phase 89 prototype gain of 14.9% is one pair, measured before the port onto builds.
  - Why needed: the current coordinator starts recorder commands and a shell resolver. The codex_mesh deletion removes the follow commands from the Phase 54 profile. Phase 99's adapter changes test execution only. Profile the current production commands before using the historical gain estimates.
  - Accepted by sdlc on 2026-11-06: devtools runs the recorder and resolver calls in the coordinator process after the agreed dependencies reach main.
  - Completion condition: measure all four assign launcher modules before and after devtools' production change on comparable builds revisions. Record the fixture implementation for each measurement. Keep Phase 68 figures as history and report production gains separately from fixture gains. Update the Floors rows with measured CPU and the remaining limiting operation.
  - Revealed by: Phase 54 profile, 2026-11-06. Updated after Phase 99 and the codex_mesh deletion.
- [ ] **Architecture proposal: the type-check job**
  - Target: `scripts/production/merge_gate.py` (owner sdlc devtools-unit)
  - Why needed: report-unit names `types/basedpyright` among the three highest-CPU gate jobs (2-4 min). One basedpyright job checks every listed folder in each gate.
  - Completion condition: after 5 or more completed gate runs each record a measured `types/basedpyright` execution (not aborted, not a bootstrap failure with zero duration), send the showrunner a proposal with the measured type-job share of gate wall time and CPU, and one design (for example, check only the folders whose files changed, or one job for each folder group).
  - Revealed by: the builds showrunner's welcome, 2026-11-03.
- [ ] **Architecture proposal: stale module keys in the gate state**
  - Target: `scripts/production/merge_gate.py` (owner sdlc devtools-unit)
  - Why needed: `module_seconds.json` keeps 728 keys for 664 test files; old `delegate/*` keys remain after the folder moved. Memory admission reads the peak file the same way.
  - Completion condition: send the showrunner a proposal to drop keys whose module file is gone, with the key counts.
  - Revealed by: plan research, 2026-11-03.
- [x] **The build log test suite finishes about 20 seconds sooner.** Taken by Phase 28 (2026-11-05): parallel starts, one process for each module.
  - Target: `scripts/buildlog/test_index.py` — `LogIsolationTests.test_each_module_uses_a_temporary_log`
  - Why needed: the test starts one Python process for each of the 33 `test_*.py` modules in `scripts/buildlog`: 24.5 s of the 34.8 s suite run at load 70 (11:24 EST, 2026-11-03). It is the slowest test in the index suite, and each new module adds one more process.
  - Completed by Phase 28: eight workers run one process for each discovered module, and each module keeps both checks in its own subTest. Paired module medians: 26.0 s -> 5.8 s.
  - Revealed by: Phase 3
- [x] **The simplify-lens report validation test starts fewer processes and keeps its recorder checks.** The daily simplify work removed this test's cost (Phase 51 as-built).
  - Target: `scripts/direct/test_simplify_lens.py` — `test_report_validation_trims_whitespace_and_requires_pass_or_a_numbered_item` (25 s, 8 review-launcher runs)
  - Why needed: Phase 6 left this test as the module's largest cost. Each of the 8 runs checks the report result, the saved status and the review record's `status` and `raised` fields, so a parse-only call cannot replace them.
  - Completion condition: every existing check stays; the needed process boundaries stay; same-load pairs show the gain.
  - Revealed by: Phase 6; showrunner ruling 2026-11-04; the Phase 11 architect review.
- [ ] **Each improved module keeps a reference time.**
  - Target: `scripts/test_times/show.py`, `config/test_times.conf`
  - Why needed: the gate's slowdown warning compares with a rolling median, which slowly accepts a slowdown as normal (Astra plan review, 2026-11-04).
  - Completion condition: each module a speed phase improved has its after median, CPU time, revision and load stored as a reference. A gate median 20% over it is reported at phase start and starts an investigation. A measure at comparable load confirms a slowdown before another speed phase is selected.
  - Revealed by: Astra plan review; starts after Phase 14 lands (showrunner ruling 2026-11-04).
- [x] **`mac_run.sh` uses the sourced configuration reader.**
  - Target: `scripts/production/mac_run.sh` (recent owner: builds-resources)
  - Why needed: `config_value()` starts `python3 conf.py get` once for each key. `test_mac_run.py` makes 339 such starts (23.5 s traced of a 49.5 s traced run); they set the Phase 29 floor.
  - Completed by Phase 30 (2026-11-05): `mac_run.sh` sources `conf.sh`. Python starts per run 18 -> 1; paired module medians 34.6 s -> 20.5 s.
- [ ] **Architecture proposal: the Mac claim and release commands import less**
  - Target: `scripts/mac_test/offload.py`, `scripts/mac_test/mac_test.py` (owner verify-unit), `scripts/build_hold/build_hold.py` (held until natedev messaging's hash)
  - Why needed: Phase 30 profile at load 10: claim 334.5 ms, release 276.6 ms; imports are about 230 ms of each. `build_hold.py:43` imports `admission_service` at once (126-132 ms cumulative); `offload.py:910` imports `memory_admit` a second time under another name (42 ms on claim); `offload` and `sweep` load at module start (about 41-53 ms, overlapping).
  - Split (showrunner, 2026-11-05): natest takes items 1 and 3 and resources' add-on F006 (offload reads its git helpers from a small module, not all of `sweep`) in one phase after Phase 24 lands; one unit changes `offload.py`. queue-unit takes item 2 after the `build_hold.py` freeze, with the admission client start (it was resources' add-on F005); natest sent the measure and the light `session_owner` module design, and measures `test_verify_ssh_targets_routing.py` and `test_verify_mac_only.py` when it is on builds (2026-11-05).
  - Completion condition (natest part): `offload.repository_identity` (`offload.py:959`) uses `repository.repository_key` from Phase 22 and imports no `memory_admit`; report-unit's Phase 24 checkpoint 811bc2ba0 does this, so confirm it on builds and measure it. Then a light release path and F006, under one owner of `offload.py`, after Phase 24. Measure each change separately. Originally: send the showrunner the three proposals in order (one `memory_admit` name; admission imports out of the claim and release path through one light shared module, not a copy; a light release path), each with its measured gain after the one before it lands. Estimated 200-260 ms for each claim and release pair.
  - Revealed by: Phase 29 trace and Phase 30 profile, 2026-11-05.
  - Revealed by: Phase 29, 2026-11-05.
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
- [ ] **Architecture proposal: the build target commands import less**
  - Target: `scripts/build_hold/build_hold.py` (owner resources; queue-unit takes the cut), `scripts/build_targets/build_targets.py` `eligible` answer (owner verify-unit)
  - Why needed: after Phase 24, `eligible` and `reserve` set the floor of `test_verify_ssh_targets_routing.py`: they import `build_hold`, which imports `admission_service` and `memory_admit` at once (`build_hold.py:40-46`). `eligible` runs 24 times (23.4 traced s).
  - Owners: queue-unit owns the admission import change after the `build_hold.py` freeze (measured 9.6 s on this module). The `eligible` platform column is complete. Verify-unit owns the remaining eligibility, result and reservation consumer changes named by Phase 66.
  - Phase 41 result, 2026-11-05: lazy imports in `build_targets.py` alone gain 1% CPU in `test_verify_mac_only.py`, so they did not land. The gain needs `build_hold`'s chain to leave `eligible` and `reserve`.
  - Phase 42 result, 2026-11-06: the same method in `mac_idle.py` gains 17.9% CPU in `test_verify_mac_only.py`, because the whole chain leaves four of five commands.
  - Phase 56 result, 2026-11-06: `test_verify_ssh_targets_retained.py` and `_retry.py` reach this same floor after all fixed waits left. Measure them in the same pairs.
  - Completion condition: after each owner change reaches builds, measure routing, retained, retry, and Mac-only modules in same-load pairs. Report CPU s and process counts first. Use the test forms that Phase 61 leaves.
  - Revealed by: Phase 24, 2026-11-05.

- [x] **Phase notes stopped updating.** Text: `phase table not written: phase note target is not owned by builds/natest-unit: ~/rust/hanadocs/showrunners/builds/natest.md`. Cause: `_note_ownership` in `scripts/direct/phase_table.py` looks up the note's `unit:` field as a staff id, and the notes still hold the older `unit: natest-unit` header (queue, report and fastest also). So each write refuses. Fix: run the one-off `phase_table.py migrate-notes`, which reads `*/*.md` below `--vault-root` and rewrites the header to the staff id. Done 2026-11-05: the showrunner assigned the builds notes to natest; `phase_table.py migrate-notes` rewrote natest, queue, report, resources and verify. natedev has the fastest note.

## Floors

Each floor row states its measurement conditions: revision, filesystem roots and bytecode cache (showrunner, 2026-11-09). Compare a gate reading with a floor only under the same conditions. The older rows are wall floors from before 2026-11-06, not CPU floors. A wall floor does not hold a module back when its CPU rank calls it and direct calls were not tried. The `direct/test_verify*` rows go to the batches.

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `production/test_fast_track.py` | 97.9 | production Git, CLI children, blocked-hook deadlines and promotion retries | 33 |
| `direct/test_verify_ssh_targets_routing.py` | 81.6 | Python start-up of `eligible` and `reserve` (`build_hold` -> `admission_service` -> `memory_admit`) | 24 |
| `style/test_crate_run.py` | Phase 77 CPU floor: 53.25 s | Production Git wrappers cost 40.8 CPU s; natedev-style owns reuse of the validated checkout | 77 |
| `direct/test_verify_mac_only.py` | 185.8 (load 52-70) | `build_targets.py` process starts through the `build_hold` chain; lazy imports in `build_targets.py` alone gain 1% CPU | 39, 41, 42 |
| `direct/test_verify_ssh_targets_retained.py` | 35.5 (load 52-65) | child process starts and the coordination imports through `build_hold.py`; no fixed waits remain | 56 |
| `direct/test_verify_ssh_targets_retry.py` | 42.1 (load 52-65) | the same starts and imports; `recovery_start_seconds` stays 5 s | 56 |
| `direct/test_verify_wait_holder.py` | Phase 95 CPU floor: 81.5 s, revision 409c83c62 | Real target, reservation and recorder commands; verify-unit owns the remaining imports and the routing boundary | 95 |
| `direct/test_assign_launcher.py` | Phase 89 CPU floor: 26.0 s pair median, revision d23c0bf81, /tmp roots, default bytecode cache | `launch_settings` starts a shell and a Python registry command for each launch; the models unit owns the resolver change | 89 |
| `direct/test_verify_result_environment.py` | Phase 102 CPU floor: 37.7 s pair median, revision 60c5a1b9d, /tmp roots, default bytecode cache, 16 real `verify.sh` calls. Worker pairs: 70.5 s on /dev/shm roots with an empty bytecode cache | Real `verify.sh` routing, admission, key and record processes; verify-unit owns the Phase 95 target-selection, recorder import and routing boundary proposals. Revisit after those changes land | 102 |
