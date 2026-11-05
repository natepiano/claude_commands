# builds-report — next items

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

## The recorder takes the build folders from verify.sh
- **Starts when:** verify-unit's remake of `verify.sh` merges (ETA 2026-11-05 01:30 EST). Tell verify-unit at the start. Showrunner disposition 2026-11-04: next, report-unit owns both sides.
- **Target:** `scripts/buildlog/record.py` (`record.step`), `scripts/direct/verify.sh` (`GATE_METADATA`).
- **Need:** `record.step` runs `cargo metadata` (30-120 ms CPU) and `rustc -V` (10-17 ms) on every step. `verify.sh` already knows `target_directory` and `build_directory`.
- **Completion:** verify.sh passes the folders with evidence of the Cargo inputs that produced them. The recorder checks that evidence through `step_context` before it uses the folders. Missing or stale evidence uses the Phase 19 reader (`step_context.step_output_roots`). Keep the mismatch diagnostic and the saved pairs of pending retries. Test env wrappers, manifests, CLI config, target overrides, and a config change between steps. The phase notice gives the CPU per step from an interleaved A/B run against the prior checkpoint.
- **Target also:** `scripts/buildlog/step_context.py` (Phase 19 moved the output-folder read there).
- **Source phase:** 16 (simplify item 11).

