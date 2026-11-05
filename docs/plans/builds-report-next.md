# builds-report — next items

## Each test step's row records the memory charge admission gave its test phase
- **Starts when:** resources-unit's test-phase entry charge lands in admission's reservation (its parked Phase 15 follow-up). Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py` (`test_phase_context`).
- **Need:** every step row records `test_phase_charge` null today, because admission has no test-phase entry-charge field.
- **Completion:** `record.test_phase_context` copies the charge (bytes, history cutoff, form, contributing widths) from the reservation into the row's `test_phase_charge` and computes nothing. Test: a reservation with the field records it for terminal and tee steps; a reservation without it records null.
- **Source phase:** 4c.

## Admission reads the builds slice's memory from the one shared reader
- **Starts when:** the shared cgroup reader is deployed on main. The showrunner assigns `memory_admit.py`'s owner. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/lint/memory_admit.py` (`sccache_memory`).
- **Need:** `sccache_memory` constructs the builds slice path and separately parses `sccache.service/memory.stat`. The shared cgroup reader already exists.
- **Completion:** `sccache_memory` reads the shared typed slice capture; no second parse of the slice's `memory.stat` remains.
- **Source phase:** 6 (phase review).

## A real build step's row shows measured CPU and IO
- **Starts when:** the installed recorder includes scope-counter capture for CPU. IO verification follows the user's natedev rebuild with scope IO accounting. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/buildlog/record.py`, `scripts/lint/invoke.sh`.
- **Need:** `invoke.sh` runs the installed recorder, so tests and the copy-based live check could not show a real step's counters.
- **Completion:** one real step on natedev has measured CPU in `scope_resources`; IO is measured after the rebuild.
- **Source phase:** 7.

## bevy_brp launches report their target folder
- **Starts when:** a session owns bevy_brp and has a worktree for it. Showrunner disposition 2026-11-04: next.
- **Target:** `~/rust/bevy_brp/mcp/src/app_tools/launch` (the launch result; the binary path comes from the targets `get_binary_path`).
- **Need:** add `target_directory` (absolute) to the launch result. The buildlog side is done: `record.transcript_target_folder` (`scripts/buildlog/record.py:120`) uses an absolute `target_directory` first, else `binary_path` with `profile`. Launches without either record `unresolved`.
- **Completion:** a bevy_brp launch on natedev shows its absolute target folder in the build log index.
- **Source phase:** 11 (simplify item 24).

## Recorder sampling and Mac offload no longer load memory_admit
- **Starts when:** queue 10z merges to builds, after natest P10 is on builds. Four units edit `memory_admit.py` now. Showrunner disposition 2026-11-04: next, report-unit owns it as one phase.
- **Target:** `record.test_phase_context`, `record.admission_metadata`, `memory_admit.sampling_context`, and `offload.repository_identity` (`scripts/mac_test/offload.py`).
- **Need:** Phase 16 moved width parsing (`parse.test_threads`) and the repository key (`record.remote_identity`) out of memory_admit. Tagged sampling calls still load memory_admit (`record.py` test-phase-context). `offload.repository_identity` imports all of memory_admit and runs 2-3 git processes to read one key.
- **Constraints:** reuse `parse.test_threads` and `record.remote_identity`. Keep repository keys and the sampling inputs unchanged.
- **Completion:** neither recorder sampling nor Mac repository identity loads memory_admit. Tests cover sampling output, old tagged calls, missing calls, remote keys and root keys. The recorded rows are unchanged. The phase notice gives the CPU per step before and after.
- **Source phase:** 16 (simplify items 10 and 13).

## The recorder takes the build folders from verify.sh
- **Starts when:** verify-unit's remake of `verify.sh` merges (ETA 2026-11-04 20:30). Tell verify-unit at the start. Showrunner disposition 2026-11-04: next, report-unit owns both sides.
- **Target:** `scripts/buildlog/record.py` (`record.step`), `scripts/direct/verify.sh` (`GATE_METADATA`).
- **Need:** `record.step` runs `cargo metadata` (30-120 ms CPU) and `rustc -V` (10-17 ms) on every step. `verify.sh` already knows `target_directory` and `build_directory`.
- **Completion:** verify.sh passes the folders; the recorder runs `cargo metadata` only when they are missing. The phase notice gives the CPU per step before and after.
- **Source phase:** 16 (simplify item 11).

## Each memory tick writes one row
- **Starts when:** natest P10 is on builds (the showrunner says when). Showrunner disposition 2026-11-04: next.
- **Target:** `scripts/buildlog/index.py` (a `zram` column in `SAMPLE_COLUMNS`, schema version bump), `scripts/buildlog/sample.py`, `scripts/buildlog/memory.py`, `scripts/buildlog/cli.py`.
- **Need:** each tick writes kind, at and host twice through two writers; the tick snapshot row holds only zram.
- **Completion:** the sample row carries zram; the tick `memory_snapshot` row, `write_snapshot`, `tick_snapshot` and `TickMemorySnapshot` are gone; `memory_snapshot` stays only for `buildlog snapshot`. Old logs give the same report. About 105 KB/day less.
- **Source phase:** 17 (simplify item 1).
