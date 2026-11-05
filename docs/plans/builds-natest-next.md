# builds-natest — Next

The phase backlog of natest-unit. At each phase end, re-sort with `python3 ~/.claude/scripts/test_times/show.py top`, and the next speed phase takes the top live module that no phase holds.

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

- [ ] **Select the next speed targets after Phase 14.**
  - Target: the highest ranked live modules in `test_times top` that no phase owns, subject to the plan's floor rule.
  - Why needed: the user's mission takes the next module when the longest one is shorter (2026-11-03). The 2026-11-03 list named `scripts/direct/test_review_launcher_pool.py`, which no longer exists.
  - Completion condition: each selected module goes through the Speed method with its current path and a measured baseline.
  - Revealed by: the 2026-11-03 sort; the Phase 11 architect review.
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
- [ ] **The build log test suite finishes about 20 seconds sooner.**
  - Target: `scripts/buildlog/test_index.py` — `LogIsolationTests.test_each_module_uses_a_temporary_log`
  - Why needed: the test starts one Python process for each of the 33 `test_*.py` modules in `scripts/buildlog`: 24.5 s of the 34.8 s suite run at load 70 (11:24 EST, 2026-11-03). It is the slowest test in the index suite, and each new module adds one more process.
  - Completion condition: one process imports every module and reports each module's log root; the test keeps one assertion per module and takes under 3 s on an idle natedev.
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

## Floors

| Module | Floor (pair median wall s) | Limiting operation | Phase |
| --- | --- | --- | --- |
| `direct/test_simplify_lens.py` | 118 | one real review-launcher run per test (3-6 s) | 6 |
