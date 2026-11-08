# builds-report — next items

## Admission reads the builds slice's memory from the one shared reader
- **Starts when:** resources' hash with its Phase 66 is on builds (with Phase 27), and queue's Phase 10x is on builds. From queue-unit (not the user), 2026-11-05: 10x changes `sccache_memory` (`slice_working_set(directory / 'sccache.service')`, `member_programs(..., skip_unreadable=True)`); build on that form. 10k edits `memory_admit.py` but not `sccache_memory`, so this item does not wait for 10k. The shared cgroup reader is on main. From the builds showrunner (not the user), 2026-11-05: report-unit owns this item and holds `scripts/lint/memory_admit.py` for it. Showrunner disposition 2026-10-30: next.
- **Target:** `scripts/lint/memory_admit.py` (`sccache_memory`).
- **Need:** `sccache_memory` constructs the builds slice path and separately parses `sccache.service/memory.stat`. The shared cgroup reader already exists.
- **Completion:** `sccache_memory` reads the shared typed slice capture; no second parse of the slice's `memory.stat` remains.
- **Source phase:** 6 (phase review).

## Other script folders import from the repo root, one phase per folder
- **Starts when:** Phase 26 is done (Phase 27 is done). From the builds showrunner (not the user), 2026-11-06: "F020: next (one phase per folder, after Phases 26/27; each phase names its folder owner in also-touches)."
- **Target:** the `sys.path` lines that sdlc's devtools 3z converter left in these folders (grep, 2026-11-08): `scripts/build_hold/hold_applies.py:13`, `test_hold_applies.py:17`, `test_target_hold.py:15`; `scripts/build_targets/targets.py:23`, `working_set.py:25`, `test_ssh_eligibility.py:26`; `scripts/mac_test/audit.py:20-23`, `loopback_proof.py:20,27,40-41`, `test_audit.py:17`, `test_target_state.py:25`. Lines inside code strings that a test or the Linux runner sends to another process are not targets. Check the lines first.
- **Need:** these modules still change `sys.path`, so a module can load under two names. `scripts/notify` and `scripts/shutdown` have no such line now, and no file imports `sessions`, `unit_runs`, `user_zone` or `cargo_manifest` by a bare name (grep, 2026-11-08). From the builds showrunner (not the user), 2026-11-06: "add-on 1: next. Fold it into the F020 per-folder phases (one owner switches all callers of each module; name each folder owner in also-touches). After Phases 26 and 27."
- **Completion:** each folder uses `from scripts.x import y` and keeps a repo-root path line only in a module that runs by path. Each phase's notice names the folder owner in also-touches. No module loads under two names.
- **Source phase:** 33 (simplify F019, F020, F021).

## bevy_brp launches report their target folder
- **Starts when:** a session owns bevy_brp and has a worktree for it. Showrunner disposition 2026-11-04: next.
- **Target:** `~/rust/bevy_brp/mcp/src/app_tools/launch` (the launch result; the binary path comes from the targets `get_binary_path`).
- **Need:** add `target_directory` (absolute) to the launch result. The buildlog side is done: `record.transcript_target_folder` (`scripts/buildlog/record.py:120`) uses an absolute `target_directory` first, else `binary_path` with `profile`. Launches without either record `unresolved`.
- **Completion:** A bevy_brp launch result contains an absolute `target_directory` in its metadata, and the indexed launch records that exact value.
- **Source phase:** 11 (simplify item 24).
