# builds-natest — Next

The phase backlog of natest-unit. At each phase end, rank modules by median CPU with `python3 ~/.claude/scripts/test_times/show.py top --repo ~/.claude`. The next speed phase takes the highest ranked live module that no phase holds, subject to the floor rule.

## Order

Median CPU per gate, from `show.py top --limit 400` on 2026-11-06. The speed phases confirm their targets at dispatch.

1. Phases 65, 64, 66, 55, 63, 70 and 71 are complete, and they form one tip.
2. Phases 69 and 44 are complete. Their paired after measurements are 120.2 CPU s for `copies/test_review_remote_copies.py` and 63.80 CPU s for `style/test_crate_run.py`. These are not refreshed gate medians. Confirm the merged gate rank before another dispatch.
3. Phase 67 follows Phase 82: lint judgment and the parser cases, previously 231 CPU s. Its required functions already exist on origin/builds. The four SSH target files, previously 146.1 CPU s, still wait for verify's remaining import and function changes (Phase 66 floor).
4. Phase 63 is complete. Phase 70 reports the token order hang; its cause stays open. Read the call output and queue files at its next recurrence.
5. Phase 72 complete; the lifecycle writer and the first admission stack stay unproved. Phase 73 makes no code change: verify-unit owns the surviving token-renewal delay, and the Mac case needs no edit because queue lacked Phase 63's marker wait.
6. Further `test_verify*` batches follow CPU rank. Measure with `module_cost.py --tests 10 --commands 10` before direct moves. Hold cases dominated by real `build_targets.py` commands for verify's import change. Hold routing decisions without a narrow function for verify's function change. These include Mac retry, fallback, queue-age and handoff cases. Profile doc, lint-scope and existing token-renewal function cases now. Their fixtures and function boundaries differ from the SSH fixtures. Include admission, setup and cleanup CPU. Give each shared fixture one owner. Each batch has one phase and one bumped checkpoint. Use `docs/plans/natedev-verify-test-cost.md` for the file list.
   - The next batch names these files, with ownership by support file (`verify_mac_offload_test_support.py`, `verify_token_wait_test_support.py`, `verify_hold_wait_test_support.py`), not by importer: `test_verify_mac_lifecycle`, `mac_retry_hold`, `mac_retry_clock` (memory and token cases; its routing-hook case already uses a direct call), `mac_results`, `mac_holder_history`, `mac_fallback_announcements`, `mac_fallback_diagnostics`, `mac_wait_records`, `mac_local_release`, `mac_release`, `mac_routing`, `mac_routing_changes`, `mac_memory_handoff`, `mac_pacing`, `mac_withdrawal`, `token_memory_history`, `token_wait`, `token_reservation`, `token_handoff`, `token_queue`, `hold_wait`.
7. Phases 67 and 68 run in parallel from 2026-11-07. Phase 84 (`test_verify_lint_cache_versions`, `test_verify_step_state`) follows Phase 67. Phase 85 (`style_ledger_tickets`, `style_ledger_findings`) is next after them (showrunner, 2026-11-07).
8. Phase 59 complete: `style/test_style_proposals.py`, 53.75 to 28.51 CPU s in pairs; the rule-loading proposal is with the style owner.
9. Phase 74 complete: `copies/test_copy_guard.py`, 64.15 to 31.36 CPU s in pairs. Phase 75 complete: `production/test_add_unit.py`, 59.76 to 20.34 CPU s in pairs. Phase 76 complete: the verify hold and token wait fixtures make no live `/tmp/claude/unit` read. Phases 77, 78 and 79 are complete. Phase 77 records 53.25 CPU s for `style/test_crate_run.py`; Phase 78 records 28.9 CPU s for `production/test_staff.py`; Phase 79 reports 27.7 CPU s for `style/test_add_style.py`. `production/test_fast_track.py` (53.3) stays at its measured floor until its architecture proposal lands. `agents/test_agent_bg.py` (50.4) waits for the `agent_bg.sh` changes in `herdr-worker-panes.md`.

10. hierarchies-unit Phase 30 deletes the codex_mesh code and its tests (sdlc, 2026-11-07; checkpoint ETA 23:30 EST). sdlc sends the main hash. Then count its effect in the speed record: `agents/test_codex_mesh.py` was 736 tests and 194.3 CPU s median per gate (`show.py top`, 2026-11-07 14:50 EST). Compare the gate's total CPU and test count before and after that hash (the user, 2026-11-07).

11. Rule 2 cleanup (the user, 2026-11-07: "do rules 1 and 2"). After Phase 87 measures the yield, the cleanup takes the next modules by CPU s spent on rare-case tests: `production/test_dailies_render_holds.py`, `agents/test_models_conf_publish.py`, `direct/test_progress_history.py`, `lint/test_memory_admit.py`, then the rest. Skip modules that a speed phase holds and `agents/test_codex_mesh.py`, which hierarchies deletes.

Held: `style/test_style_sync.py`, 66.6 CPU s; devtools holds it. Never: `types/basedpyright`, 238.4 CPU s; `agents/test_codex_mesh.py`, 232.6 CPU s, retires.

`types/basedpyright` takes 2-4 min in a gate (report-unit, 2026-11-03). It is not in the gate's module files; Phase 2 records it, and it goes to the proposal below, never to a speed phase.

## Items to consider

- [ ] **No test fails at cleanup because Git tidies a test repository in the background.**
  - Target: `scripts/test_times/module_run.py` and the gate test environment: set `maintenance.auto false` for every test process (for example through `GIT_CONFIG_COUNT`), not fixture by fixture.
  - Why needed: Phase 81 proved that `git commit` starts a detached `git maintenance run --auto`, and its repack writes into `.git` during `TemporaryDirectory` cleanup. `ImmutableGitSeed.create`, `CargoProjectTests`, `LedgerTestCase`, `LifecycleFixture` and `MergeGateFixture` still commit in temporary repositories without stopping it.
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
  - Target: `scripts/direct/assign.py` (owner sdlc; models and herdr change it now)
  - Why needed: Phase 54 profile: one dispatch takes 2.6-3.2 s before the worker starts its turn. The children are check-launch 0.164 s, worker resolve 0.365 s, can-follow 0.292 s, start-pass 0.158 s, finish-pass 0.177 s, release-follow 0.294 s and board 0.04 s. Each real dispatch pays this, and so do the assign launcher tests.
  - Accepted by sdlc on 2026-11-06: devtools builds it after herdr Phase 15 and models reach main. It runs the recorder and resolver calls in the coordinator process. Estimated gain: about 1.4 s per dispatch, about 15 s in `test_assign_launcher_reuse.py` and 20 s in `test_assign_launcher_identity.py`.
  - Completion condition: same-load pairs of both modules when devtools' change is on builds; the Floors rows move to the new medians.
  - Revealed by: Phase 54 profile, 2026-11-06.
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

These are wall floors from before 2026-11-06, not CPU floors. A wall floor does not hold a module back when its CPU rank calls it and direct calls were not tried. The `direct/test_verify*` rows go to the batches.

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `production/test_fast_track.py` | 97.9 | production Git, CLI children, blocked-hook deadlines and promotion retries | 33 |
| `direct/test_verify_ssh_targets_routing.py` | 81.6 | Python start-up of `eligible` and `reserve` (`build_hold` -> `admission_service` -> `memory_admit`) | 24 |
| `style/test_crate_run.py` | Phase 77 CPU floor: 53.25 s | Production Git wrappers cost 40.8 CPU s; natedev-style owns reuse of the validated checkout | 77 |
| `direct/test_verify_mac_only.py` | 185.8 (load 52-70) | `build_targets.py` process starts through the `build_hold` chain; lazy imports in `build_targets.py` alone gain 1% CPU | 39, 41, 42 |
| `direct/test_verify_lint_cache_versions.py` | 135.9 (clean rounds, no pairs) | `verify.sh lint` route: `step_record.py` 486 starts and 23.1 of 94.0 CPU s | 45 |
| `direct/test_verify_lint_judgment.py` | 198.6 | production `verify.sh lint` route: 3.1 s per run with no repair, 5.2 s with repair (step-record and admission client Python starts) plus admission start spacing | 31 |
| `direct/test_assign_launcher_reuse.py` | 57.4 (load 55-70) | one real launcher process for each later dispatch: 2.6-3.2 s, of which 1.80 s is 12 `subprocess.run` calls in the coordinator | 54 |
| `direct/test_assign_launcher_identity.py` | 55.8 (load 55-70) | the same launcher process, 14 dispatches in 9 tests | 54 |
| `direct/test_verify_ssh_targets_retained.py` | 35.5 (load 52-65) | child process starts and the coordination imports through `build_hold.py`; no fixed waits remain | 56 |
| `direct/test_verify_ssh_targets_retry.py` | 42.1 (load 52-65) | the same starts and imports; `recovery_start_seconds` stays 5 s | 56 |
