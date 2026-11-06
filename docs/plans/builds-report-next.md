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
