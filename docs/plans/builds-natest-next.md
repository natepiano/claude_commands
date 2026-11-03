# builds-natest — Next

The phase backlog of natest-unit. At each phase end, re-sort with `python3 ~/.claude/scripts/test_times/test_times.py top` (after Phase 4), and the next speed phase takes the top live module that no phase holds.

## Descending sort

Median wall time of the last 5 gate runs, from `~/.local/state/merge-gate/module_history.json` on 2026-11-03. Phases 5-7 name the first six rows; each speed phase confirms its targets at dispatch.

| Rank | Module | Median s | Last s | Last CPU s | Phase |
| --- | --- | --- | --- | --- | --- |
| 1 | `direct/test_assign_launcher.py` | 180 | 132 | 127 | 5 |
| 2 | `direct/test_verify_ssh_targets.py` | 157 | 116 | 117 | 5 |
| 3 | `agents/test_codex_mesh.py` | 137 | 947 | 79 | 6 |
| 4 | `direct/test_simplify_lens.py` | 132 | 122 | 61 | 6 |
| 5 | `production/test_fast_track.py` | 127 | 97 | 85 | 7 |
| 6 | `direct/test_verify_lint_guard_key.py` | 107 | 63 | 63 | 7 |
| 7 | `shutdown/test_restart.py` | 71 | 59 | 58 | — |
| 8 | `hana_shot/test_hana_shot.py` | 61 | 56 | 14 | — |
| 9 | `direct/test_workers.py` | 51 | 57 | 26 | — |
| 10 | `direct/test_review_launcher_pool.py` | 50 | 37 | 23 | — |

`types/basedpyright` takes 2-4 min in a gate (report-unit, 2026-11-03). It is not in the gate's module files; Phase 2 records it, and it goes to the proposal below, never to a speed phase.

## Items to consider

- [ ] **Speed phases for ranks 7-10**
  - Target: `scripts/shutdown/test_restart.py`, `scripts/hana_shot/test_hana_shot.py`, `scripts/direct/test_workers.py`, `scripts/direct/test_review_launcher_pool.py`
  - Why needed: the user's mission takes the next module when the longest one is shorter (2026-11-03).
  - Completion condition: each module's median wall time is at most half its baseline, with the speed method in the plan's Delegation Context.
  - Revealed by: the 2026-11-03 sort.
- [ ] **Architecture proposal: the type-check job**
  - Target: `scripts/production/merge_gate.py` (owner sdlc devtools-unit)
  - Why needed: report-unit names `types/basedpyright` among the three highest-CPU gate jobs (2-4 min). One basedpyright job checks every listed folder in each gate.
  - Completion condition: after Phase 2 records 5 or more gate runs, send the showrunner a proposal with the measured type-job share of gate wall time and CPU, and one design (for example, check only the folders whose files changed, or one job for each folder group).
  - Revealed by: the builds showrunner's welcome, 2026-11-03.
- [ ] **Architecture proposal: stale module keys in the gate state**
  - Target: `scripts/production/merge_gate.py` (owner sdlc devtools-unit)
  - Why needed: `module_seconds.json` keeps 728 keys for 664 test files; old `delegate/*` keys remain after the folder moved. Memory admission reads the peak file the same way.
  - Completion condition: send the showrunner a proposal to drop keys whose module file is gone, with the key counts.
  - Revealed by: plan research, 2026-11-03.
