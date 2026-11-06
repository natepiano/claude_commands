# builds-natest — Next

The phase backlog of natest-unit. At each phase end, rank modules with `python3 ~/.claude/scripts/test_times/show.py top --repo ~/.claude`. The next speed phase takes the highest ranked live module that no phase holds, subject to the floor rule.

## Descending sort

Median wall time of the last 5 gate runs, from `~/.local/state/merge-gate/module_history.json` on 2026-11-03. Phases 5-7 name the first six rows; each speed phase confirms its targets at dispatch.

| Rank | Module | Median s | Last s | Last CPU s | Phase |
| --- | --- | --- | --- | --- | --- |
| 1 | `direct/test_assign_launcher.py` | 180 | 132 | 127 | 5 |
| 2 | `direct/test_verify_ssh_targets.py` | 157 | 116 | 117 | 5 |
| 3 | `agents/test_codex_mesh.py` | 137 | 947 | 79 | excluded: codex-mesh retires |
| 4 | `direct/test_simplify_lens.py` | 132 | 122 | 61 | 6 |
| 5 | `production/test_fast_track.py` | 127 | 97 | 85 | 7 |
| 7 | `shutdown/test_restart.py` | 71 | 59 | 58 | — |
| 8 | `hana_shot/test_hana_shot.py` | 61 | 56 | 14 | — |
| 9 | `direct/test_workers.py` | 51 | 57 | 26 | — |
| 10 | `direct/test_review_launcher_pool.py` | 50 | 37 | 23 | — |

`types/basedpyright` takes 2-4 min in a gate (report-unit, 2026-11-03). It is not in the gate's module files; Phase 2 records it, and it goes to the proposal below, never to a speed phase.

## Items to consider

- [x] **One path to `send.py`.** 29 production modules build their own `send.py` path in place of `session_send`. Sent to the showrunner on 2026-11-05 with the owner of each file; recommended owner: natedev-messaging. Phase 26 refuses the sender when `TEST_SENDER_GUARD` is set; the runner item below owns the guard for every module. No natest work remains on this caller cleanup.
- [ ] **The test runner guards every module from the real `send.py`.**
  - Target: `scripts/test_times/module_run.py`, `scripts/message/session_send_test_support.py`
  - Why needed: after Phase 26, only the four modules that call `forbid_real_sender()` are guarded, and the runner finds the marker through that module's stored record. A module that never installs the guard, or a marker variable inherited from a parent, is not checked (Phase 26 adversary round 2). No module in the gate takes either path today.
  - Completion condition: the runner sets one marker path before `unittest.main` for every module and checks that path after it; the four installs reuse it; the gate shows which modules, if any, start the real `send.py`.
  - Revealed by: Phase 26 adversary review round 2, 2026-11-05.
- [ ] **Select the next speed target after Phases 24 and 27.** Phase 51 closed with no edit: daily simplify work cut `direct/test_simplify_lens.py` to 15 tests and about 20 s. Phase 54 took `_reuse` and `_identity` of the assign launcher group (floors below). Phase 55 takes `test_assign_launcher.py` after models and herdr reach main. Phase 56 takes `test_verify_ssh_targets_retained.py` and `_retry.py` (2026-11-06).
  - Target: the highest ranked live module in `test_times top --repo ~/.claude` that no phase owns, subject to the plan's floor rule.
  - Completion condition: the module goes through the Speed method with its current path and a measured baseline.
  - Candidate after Phase 56: `herdr/test_herdr.py`, median 68.3 s wall and 38.3 s CPU, latest 65.9 s (2026-11-06). Confirm owner availability after herdr lands, then measure the current revision. `direct/test_verify_mac_only.py` is at its floor (211.8 s against 212.0 s).
  - Taken: `direct/test_verify_lint_judgment.py`, rank 3 at 210.4 s (CPU 172.2 s), was Phase 31 (floor below). `production/test_fast_track.py`, rank 4 at 216.9 s on 2026-11-05, is Phase 33 (showrunner, 2026-11-05). Rank 2 is `agents/test_codex_mesh.py`, still excluded.
  - Held: `style/test_style_sync.py`; devtools holds it and `style_sync.py` until its phase is on main, and the showrunner sends the word (2026-11-06).
  - Revealed by: the user's mission (2026-11-03); Phase 24 took `test_verify_ssh_targets_routing.py` and Phase 27 took `production/test_merge_gate_ledger.py` (2026-11-05).
- [ ] **The next test to speed up is chosen by the CPU it saves each day, not by its time in one run.** The showrunner placed it here on 2026-11-06; if the two orders differ, the showrunner takes the rule question to the user.
  - Target: the Introduction of this file and "Select the next speed target"; proposed text: "Choose eligible modules by expected daily CPU saving: observed daily executions × estimated removable CPU per execution. Include merge executions, selected unit executions, and retries; exclude reused results."
  - Why needed: 5 unit runs ran `direct/test_simplify_lens.py` twice (258 CPU s) and `direct/test_verify_mac_only.py` once (194 CPU s). The sample is small.
  - Completion condition: after a day on builds, report both orders with the count of red merges that a unit targeted test missed, against the 104-run estimate of 15 (Phase 37).
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
  - Owners (showrunner, 2026-11-05): queue-unit fixes the admission start spacing in `scripts/build_queue/admission_test_support.py` (first after 10x) and takes the lighter admission client start (`scripts/lint/memory_gate.sh:17-22`), with the `build_hold.py` import proposal. verify-unit takes step-record state reuse (`verify.sh:356-358`, `verify.sh:1925-1934`), next after its Phase 35, then a callable repair decision (`verify.sh:3711-3714`).
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
  - Owners (showrunner, 2026-11-05): the lazy `admission_service` import in `build_hold.py` goes to queue-unit with the admission client start, after the `build_hold.py` freeze (measured 9.6 s on this module). A platform column in the `eligible` answer goes to verify-unit as its next item 3.
  - Phase 41 result, 2026-11-05: lazy imports in `build_targets.py` alone gain 1% CPU in `test_verify_mac_only.py`, so they did not land. The gain needs `build_hold`'s chain to leave `eligible` and `reserve`.
  - Completion condition: natest measures `test_verify_ssh_targets_routing.py` again in same-load pairs when each change is on builds.
  - Revealed by: Phase 24, 2026-11-05.

- [x] **Phase notes stopped updating.** Text: `phase table not written: phase note target is not owned by builds/natest-unit: ~/rust/hanadocs/showrunners/builds/natest.md`. Cause: `_note_ownership` in `scripts/direct/phase_table.py` looks up the note's `unit:` field as a staff id, and the notes still hold the older `unit: natest-unit` header (queue, report and fastest also). So each write refuses. Fix: run the one-off `phase_table.py migrate-notes`, which reads `*/*.md` below `--vault-root` and rewrites the header to the staff id. Done 2026-11-05: the showrunner assigned the builds notes to natest; `phase_table.py migrate-notes` rewrote natest, queue, report, resources and verify. natedev has the fastest note.

## Floors

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `production/test_fast_track.py` | 97.9 | production Git, CLI children, blocked-hook deadlines and promotion retries | 33 |
| `direct/test_verify_ssh_targets_routing.py` | 81.6 | Python start-up of `eligible` and `reserve` (`build_hold` -> `admission_service` -> `memory_admit`) | 24 |
| `style/test_crate_run.py` | Current floor not measured; historical baseline 257.2 | current gate median 88.6 s; Phase 44 measures current copy costs before it states a gain | 40, 44 |
| `direct/test_verify_mac_only.py` | 212.0 (load 60-75; 156.5 in clean rounds) | `build_targets.py` and `mac_idle.py` process starts; lazy imports in `build_targets.py` alone gain 1% CPU | 39, 41 |
| `direct/test_verify_lint_cache_versions.py` | 135.9 (clean rounds, no pairs) | `verify.sh lint` route: `step_record.py` 486 starts and 23.1 of 94.0 CPU s | 45 |
| `direct/test_verify_lint_judgment.py` | 198.6 | production `verify.sh lint` route: 3.1 s per run with no repair, 5.2 s with repair (step-record and admission client Python starts) plus admission start spacing | 31 |
| `direct/test_assign_launcher_reuse.py` | 57.4 (load 55-70) | one real launcher process for each later dispatch: 2.6-3.2 s, of which 1.80 s is 12 `subprocess.run` calls in the coordinator | 54 |
| `direct/test_assign_launcher_identity.py` | 55.8 (load 55-70) | the same launcher process, 14 dispatches in 9 tests | 54 |
