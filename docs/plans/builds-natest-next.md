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
- [ ] **Select the next speed target after Phases 24 and 27.**
  - Target: the highest ranked live module in `test_times top --repo ~/.claude` that no phase owns, subject to the plan's floor rule.
  - Completion condition: the module goes through the Speed method with its current path and a measured baseline.
  - Candidate: `direct/test_verify_mac_only.py`, rank 1 at 358.7 s (CPU 339.3 s, 17 tests) in `top --repo ~/.claude` on 2026-11-05 08:15 EST. It waits until verify's load hang fix in `verify_mac_offload_test_support.py` is on builds; the showrunner sends word (2026-11-05).
  - Taken: `direct/test_verify_lint_judgment.py`, rank 3 at 210.4 s (CPU 172.2 s), was Phase 31 (floor below). `production/test_fast_track.py`, rank 4 at 216.9 s on 2026-11-05, is Phase 33 (showrunner, 2026-11-05). Rank 2 is `agents/test_codex_mesh.py`, still excluded.
  - Candidate: `direct/test_verify_lint_cache_versions.py`, rank 9 at 110.2 s. The verify remake is on builds (5493dfa93). Confirm ownership and rank before dispatch.
  - Candidate: `style/test_style_sync.py`, gate median 52.6 s; free since the verify remake landed (showrunner, 2026-11-05).
  - Revealed by: the user's mission (2026-11-03); Phase 24 took `test_verify_ssh_targets_routing.py` and Phase 27 took `production/test_merge_gate_ledger.py` (2026-11-05).
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
- [ ] **The simplify-lens report validation test starts fewer processes and keeps its recorder checks.**
  - Target: `scripts/direct/test_simplify_lens.py` — `test_report_validation_trims_whitespace_and_requires_pass_or_a_numbered_item` (25 s, 8 review-launcher runs)
  - Why needed: Phase 6 left this test as the module's largest cost. Each of the 8 runs checks the report result, the saved status and the review record's `status` and `raised` fields, so a parse-only call cannot replace them.
  - Completion condition: every existing check stays; the needed process boundaries stay; same-load pairs show the gain.
  - Revealed by: Phase 6; showrunner ruling 2026-11-04; the Phase 11 architect review.
- [ ] **Each improved module keeps a reference time.**
  - Target: `scripts/test_times/show.py`, `config/test_times.conf`
  - Why needed: the gate's slowdown warning compares with a rolling median, which slowly accepts a slowdown as normal (Astra plan review, 2026-11-04).
  - Completion condition: each module a speed phase improved has its after median stored as a reference; a gate median 20% over it is reported at phase start.
  - Revealed by: Astra plan review; starts after Phase 14 lands (showrunner ruling 2026-11-04).
- [x] **`mac_run.sh` uses the sourced configuration reader.**
  - Target: `scripts/production/mac_run.sh` (recent owner: builds-resources)
  - Why needed: `config_value()` starts `python3 conf.py get` once for each key. `test_mac_run.py` makes 339 such starts (23.5 s traced of a 49.5 s traced run); they set the Phase 29 floor.
  - Completed by Phase 30 (2026-11-05): `mac_run.sh` sources `conf.sh`. Python starts per run 18 -> 1; paired module medians 34.6 s -> 20.5 s.
- [ ] **Architecture proposal: the Mac claim and release commands import less**
  - Target: `scripts/mac_test/offload.py`, `scripts/mac_test/mac_test.py` (owner verify-unit), `scripts/build_hold/build_hold.py` (held until natedev messaging's hash)
  - Why needed: Phase 30 profile at load 10: claim 334.5 ms, release 276.6 ms; imports are about 230 ms of each. `build_hold.py:43` imports `admission_service` at once (126-132 ms cumulative); `offload.py:910` imports `memory_admit` a second time under another name (42 ms on claim); `offload` and `sweep` load at module start (about 41-53 ms, overlapping).
  - Split (showrunner, 2026-11-05): natest takes items 1 and 3 and resources' add-on F006 (offload reads its git helpers from a small module, not all of `sweep`) in one phase after Phase 24 lands; one unit changes `offload.py`. resources takes item 2 as its add-on F005 after the `build_hold.py` freeze; natest sent the measure and the light `session_owner` module design (2026-11-05).
  - Completion condition (natest part): `offload.repository_identity` (`offload.py:959`) uses `repository.repository_key` from Phase 22 and imports no `memory_admit`; report-unit's Phase 24 checkpoint 811bc2ba0 does this, so confirm it on builds and measure it. Then a light release path and F006, under one owner of `offload.py`, after Phase 24. Measure each change separately. Originally: send the showrunner the three proposals in order (one `memory_admit` name; admission imports out of the claim and release path through one light shared module, not a copy; a light release path), each with its measured gain after the one before it lands. Estimated 200-260 ms for each claim and release pair.
  - Revealed by: Phase 29 trace and Phase 30 profile, 2026-11-05.
  - Revealed by: Phase 29, 2026-11-05.
- [ ] **Architecture proposal: one injected Git runner in fast_track.py**
  - Target: `scripts/production/fast_track.py` (owner: production) and `scripts/production/test_fast_track.py`
  - Why needed: the module's floor is 55 tests at about 3.5 s each of real Git work (Phase 7); the test file alone cannot go lower.
  - Completion condition: decision tests run against the runner; a few process tests keep real Git; same-load pairs show the gain (about 3 minutes on one module).
  - Risk: a recorded Git transcript is a second model of Git that can drift from real Git (showrunner, 2026-11-04).
  - Revealed by: Phase 7; showrunner ruling 2026-11-04 (next, not current).
- [ ] **Measure `test_verify_lint_judgment.py` again when its owners' changes land.**
  - Target: `direct/test_verify_lint_judgment.py` (198.6 s after Phase 31)
  - Owners (showrunner, 2026-11-05): queue-unit fixes the admission start spacing in `scripts/build_queue/admission_test_support.py` (first after 10x) and takes the lighter admission client start (`scripts/lint/memory_gate.sh:17-22`), with the `build_hold.py` import proposal. verify-unit takes step-record state reuse (`verify.sh:356-358`, `verify.sh:1925-1934`), next after its Phase 35, then a callable repair decision (`verify.sh:3711-3714`).
  - Completion condition: same-load pairs on builds after each change lands; the Floors row moves to the new median.
  - Revealed by: Phase 31, 2026-11-05.
- [ ] **Every test runs from the repo root with no PYTHONPATH.**
  - Target: `scripts/production/test_next_item.py`, two `scripts/notify` test modules, `scripts/lib` (no package file; needs a module list), `scripts/production/test_production_lifecycle.py` (cannot import `fake_unit_host`), `scripts/mac_test/test_offload.py` (cannot import `offload`), `scripts/build_hold`, `scripts/buildlog` (`test_width_trial`: no module `store`), `scripts/notify` (`pushover`) and `scripts/shutdown` (`test_support`)
  - Why needed: these modules fail with an import error unless PYTHONPATH names their folder (natedev, via the showrunner, 2026-11-05).
  - Priority: the next phase after Phase 33 (showrunner, 2026-11-05). Send the showrunner an ETA when the phase is planned.
  - Completion condition: each module passes from the repo root with no PYTHONPATH. Fix the test layout once for every folder, not folder by folder (natedev). Fix the imports at the cause; add no new check.
  - Revealed by: natedev, 2026-11-05.
- [ ] **Three scripts start Python through `scripts/lib/py`, not bare `python3`.**
  - Target: `scripts/build_hold/cargo:59`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh:35`, `scripts/lint/lint:75`
  - Why needed: on a Mac with Apple's Python 3.9 first on PATH, a bare `python3` can start the wrong interpreter. Phase 32 fixed the same fault in `conf.sh` (simplify lens, Phase 32).
  - Priority: after the PYTHONPATH layout phase (showrunner, 2026-11-05).
  - Steps: 1. Run each of the three scripts on the Mac to learn whether it fails today. 2. Switch all three to `scripts/lib/py`, as one owner of the interpreter rule.
  - Ownership: `scripts/build_hold/cargo` is in builds; natest takes it. Before an edit to the hook or `scripts/lint/lint`, send the file to the showrunner, who checks the holder.
  - Completion condition: the three scripts start Python through `scripts/lib/py`; the notice carries the Mac result. Add no new check.
  - Revealed by: Phase 32 simplify review, 2026-11-05.
- [ ] **Architecture proposal: the build target commands import less**
  - Target: `scripts/build_hold/build_hold.py` (owner resources; queue-unit takes the cut), `scripts/build_targets/build_targets.py` `eligible` answer (owner verify-unit)
  - Why needed: after Phase 24, `eligible` and `reserve` set the floor of `test_verify_ssh_targets_routing.py`: they import `build_hold`, which imports `admission_service` and `memory_admit` at once (`build_hold.py:40-46`). `eligible` runs 24 times (23.4 traced s).
  - Owners (showrunner, 2026-11-05): the lazy `admission_service` import in `build_hold.py` goes to queue-unit with the admission client start, after the `build_hold.py` freeze (measured 9.6 s on this module). A platform column in the `eligible` answer goes to verify-unit as its next item 3.
  - Completion condition: natest measures `test_verify_ssh_targets_routing.py` again in same-load pairs when each change is on builds.
  - Revealed by: Phase 24, 2026-11-05.

## Floors

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `direct/test_simplify_lens.py` | 118 | one real review-launcher run per test (3-6 s) | 6 |
| `production/test_fast_track.py` | 193 | real Git work, about 3.5 s per test | 7 |
| `direct/test_verify_ssh_targets_routing.py` | 81.6 | Python start-up of `eligible` and `reserve` (`build_hold` -> `admission_service` -> `memory_admit`) | 24 |
| `direct/test_verify_lint_judgment.py` | 198.6 | production `verify.sh lint` route: 3.1 s per run with no repair, 5.2 s with repair (step-record and admission client Python starts) plus admission start spacing | 31 |
