# builds-report — next items

## Admission reads the builds slice's memory from the one shared reader
- **Starts when:** resources' hash with its Phase 66 is on builds (with Phase 27), and queue's Phase 10x is on builds. From queue-unit (not the user), 2026-11-05: 10x changes `sccache_memory` (`slice_working_set(directory / 'sccache.service')`, `member_programs(..., skip_unreadable=True)`); build on that form. 10k edits `memory_admit.py` but not `sccache_memory`, so this item does not wait for 10k. The shared cgroup reader is on main. From the builds showrunner (not the user), 2026-11-05: report-unit owns this item and holds `scripts/lint/memory_admit.py` for it. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/lint/memory_admit.py` (`sccache_memory`).
- **Need:** `sccache_memory` constructs the builds slice path and separately parses `sccache.service/memory.stat`. The shared cgroup reader already exists.
- **Completion:** `sccache_memory` reads the shared typed slice capture; no second parse of the slice's `memory.stat` remains.
- **Source phase:** 6 (phase review).

## bevy_brp launches report their target folder
- **Starts when:** a session owns bevy_brp and has a worktree for it. Showrunner disposition 2026-11-04: next.
- **Target:** `~/rust/bevy_brp/mcp/src/app_tools/launch` (the launch result; the binary path comes from the targets `get_binary_path`).
- **Need:** add `target_directory` (absolute) to the launch result. The buildlog side is done: `record.transcript_target_folder` (`scripts/buildlog/record.py:120`) uses an absolute `target_directory` first, else `binary_path` with `profile`. Launches without either record `unresolved`.
- **Completion:** A bevy_brp launch result contains an absolute `target_directory` in its metadata, and the indexed launch records that exact value.
- **Source phase:** 11 (simplify item 24).
