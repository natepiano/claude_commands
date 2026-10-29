# builds-resources

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Tests fake every memory reading the memory gate makes, and a tripwire fails a test that reads live machine memory.

> **Production: builds** — unit `resources-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 10:07 EDT

Own what a build may use and when it starts: memory admission and the memory queue, build holds, build targets and slots, the cargo wrapper, and the /tmp and disk sweeps. First work: test_brp_launch_gate.py waits on the live machine's memory under load (natedev, 2026-10-29: module took 169 s, median 6 s; two tests printed 'waiting for memory'). Its setUp fakes BUILDLOG_MEMINFO but not BUILDLOG_MEMORY_PRESSURE. Fake both readings in every test, add a guard that fails a test when the memory gate would read live machine state, and check the other memory-gate callers' tests for the same leak.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, docs and Python and shell scripts that run every session. This unit owns what a build may use and when it starts (production doc, Units → Owns).
- **Project started:** 2026-10-29T14:07:00+00:00
- **Stack:** Python 3.13, standard library only; basedpyright strict (`pyrightconfig.json`); unittest. Bash for the memory gate and hooks.
- **Layout:**
  - `scripts/lint/memory_admit.py` — the admission checker. `check_memory_reading(path)` :702 is the tripwire check: with `BUILDLOG_LIVE_MEMORY_TRIPWIRE` set, a path that resolves to `/proc/meminfo` or `/proc/pressure/memory` appends a record and raises `LiveMemoryReading`. `read_meminfo(path)` :722 reads `MemAvailable`/`MemTotal`; its path comes from the shell (`check <meminfo> …`) or from `BUILDLOG_MEMINFO` (`read_queue` :1182). `read_pressure()` :738 reads `BUILDLOG_MEMORY_PRESSURE`, default `/proc/pressure/memory`, and holds every step when `full avg10` > 10.0. It swallows `OSError`/`ValueError`. Both call `check_memory_reading` before they open the file. Callers: `check` :1000–:1001 and :1043–:1044, `read_queue` :1182 and :1185 (the `/build:queue` view). `run_action` :1320 (actions include `tripwire <path>`), `main` :1375 (a `LiveMemoryReading` exits 2 with its text as the last stderr line).
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory` :179. With the tripwire set it resolves `$meminfo` and refuses a live path through `memory_admit.py tripwire <path>` (:194–:205). Then it reads `MemAvailable` itself from `${BUILDLOG_MEMINFO:-/proc/meminfo}` (:206–:211) and starts `memory_admit.py check`. A checker that exits non-zero is refused with its last output line (:226–:229, `buildlog_memory_refuse` :153). On darwin with `BUILDLOG_MEMINFO` unset it grants at once and reads nothing (:182).
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — sources `memory_gate.sh` and calls `buildlog_wait_for_memory` with no arguments (a launch: no reservation).
  - `scripts/hooks/test_brp_launch_gate.py` — `setUp` :53 takes `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE` and `BUILDLOG_LIVE_MEMORY_TRIPWIRE` from `fake_memory_readings(self.root)` (:74). `test_low_memory_wait_leaves_old_release_record_unchanged` :159 writes its low value to the returned `meminfo` file.
  - `scripts/production/merge_gate.py` — `test_module_environment` :514 gives each test module a private `BUILDLOG_DIR` and the build-tool tripwires (`cargo`, `rustc`, `rustdoc`, `rustup`, `cargo-nextest` first on `PATH`, each appends its argv to `<root>/build-tool-calls.jsonl` and exits 99). `finish_gate_job` :536 reads that file, appends `a test started real <tool>: <args>` to the output, and sets status 1 and `GateJobResult.build_tool_started` :143. :859 never reruns a tripped module alone. Not in any unit's Owns cell: natedev showrunner's file.
  - `scripts/production/affected_modules.py` — affected-only module selection follows import edges (natedev c495a0b2): a change to a test helper selects every module that imports it. There is no `SHARED_TEST_HELPERS` list. natedev showrunner's file.
  - `scripts/production/test_merge_gate_suites.py` — tests of the gate's build-tool tripwire: `test_module_tool_calls_fail_even_when_its_tests_pass` :132 (asserts `a test started real <tool>: <args>` and no `Rerun alone:`), `test_module_can_put_its_own_build_tool_stub_first` :145. natedev 367da4f1 split `test_merge_gate.py` by subject into this module, `test_merge_gate_holds.py` and `test_merge_gate_ledger.py`. natedev showrunner's file.
  - Import idiom for a test helper: `from scripts.lib.nix_env_test_support import NixFixture` (`scripts/direct/test_verify_nix_shell.py` :13); the gate runs each module with the repo root as working directory.
- **Survey (2026-10-29, `grep -c` per test module that names the gate, `invoke.sh`, `verify.sh` or the launch hook):**
  - Fake both readings: `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`, `scripts/hooks/test_brp_launch_gate.py`, `scripts/build_hold/test_build_hold.py` (this unit, through `fake_memory_readings`); `scripts/direct/test_verify_mac_offload.py` and `test_verify_checks_fail_closed.py` (through `verify_mac_offload_test_support.py` :682), `test_verify_lint_scope.py` (`verify_lint_scope_test_support.py` :244), `test_verify_token_wait.py` (`verify_token_wait_test_support.py` :156), `test_verify_untested_examples.py` (:190) (verify-unit).
  - Fake `BUILDLOG_MEMINFO` but not `BUILDLOG_MEMORY_PRESSURE` (pressure leaks), checked on the worktree tree after merge 73a8d903: `scripts/buildlog/test_record.py` (report-unit); `scripts/buildlog/test_mac_idle.py` :333, `scripts/direct/test_verify_doc.py` :137, `test_verify_release_ack.py` :83 (verify-unit); `scripts/production/test_dailies_render_holds.py` :124, :255 (natedev showrunner).
  - Fake `BUILDLOG_MEMORY_PRESSURE` but not `BUILDLOG_MEMINFO`: `scripts/direct/test_verify_hold_wait.py` (verify-unit) — the meminfo read may be stubbed; the tripwire decides.
  - Fake neither but run `verify.sh`, `invoke.sh` or the gate: about 30 more modules (`scripts/direct/test_verify_memory_kill.py`, `test_verify_final_pass.py`, `test_verify_nix_shell.py`, `scripts/hooks/test_build_hold_guard.py`, `scripts/build_hold/test_hold_enforcer.py`, `scripts/lint/test_invoke_scope.py` and others). Many stub the gate or stop before it; a static count cannot tell, so Phase 2 uses the tripwire to find the set.
- **Out of scope (unit director's call, 2026-10-29; a one-line change to the tripwire if wrong):** the process table (`BUILDLOG_PROC`, `process_start_time` :636). The gate reads it only for the pids that the test's private ledger names, so machine load cannot change an admission through it. The build budget is a constant unless `BUILDLOG_BUILD_BUDGET_GIB` is set: not live.
- **Test lanes:** `test_*.py` beside each module.
- **Build:** none (Python, shell and markdown).
- **Test:** each changed module with `python3 -m unittest discover -s <dir> -p '<module>'` from the worktree root. Then the merge gate: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <worktree> --tests-only`, run from `~/.claude` main after a merge of `origin/builds` (memory: merge origin before every gate). Check `build_hold.py status` first; no tests during a natedev build hold that this unit does not hold.
- **Lint:** `basedpyright scripts/lint scripts/hooks scripts/build_hold scripts/production` from the worktree root, plus the directory of every other changed module; pass is its `0 errors, 0 warnings, 0 notes` line (exit status says nothing). `bash -n scripts/lint/memory_gate.sh`.
- **Invariants:**
  - Admission and hold check errors stop the step; only an explicit admission answer starts it (`docs/as-built/build-memory-admission.md` → Invariants).
  - Outside a test the tripwire does nothing: it acts only when `BUILDLOG_LIVE_MEMORY_TRIPWIRE` is set, and no installed caller sets it. A live session's gate behaves as before, byte for byte in its output.
  - Tests never read the real cgroup tree, `/proc/meminfo` or `/proc/pressure/memory` (as-built Invariants; this plan adds the pressure file).
  - Files of other owners go through cargo-berth's normal flow, and the checkpoint notice names each owner: `also touches <paths> (owner <unit>), tested against <owner origin tip>, clean` (production rule, Owners in natedev).
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — A tripwire for live memory readings, and the BRP launch tests fake both  · status: done
**Depends on:** none

#### As-built

- **Tripwire.** `BUILDLOG_LIVE_MEMORY_TRIPWIRE` holds a record file path. When it is set and not empty, a memory reading whose path resolves (`os.path.realpath`; bash `realpath -m`) to `/proc/meminfo` or `/proc/pressure/memory` is not read: it appends one JSON line `{"reading": "<live path>", "argv": [...], "pid": <pid>}` under `fcntl.flock` and stops the caller with `live memory reading <live path> in a test; fake BUILDLOG_MEMINFO and BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)`. A failed record write still refuses. Unset or empty, every reading is unchanged.
- **Python.** `memory_admit.py` `check_memory_reading(path)` runs before `read_meminfo` and `read_pressure` open their file and raises `LiveMemoryReading` (a `RuntimeError`, so `read_pressure`'s `OSError`/`ValueError` handler does not swallow it). The CLI action `tripwire <path>` runs the same check. `main` wraps `run_action` and turns `LiveMemoryReading` into exit 2 with the text as its last stderr line; in-process callers (`read_queue`, tests) get the exception.
- **Shell.** `memory_gate.sh` `buildlog_wait_for_memory` resolves `$meminfo` before its `MemAvailable` read; a live path goes to `memory_admit.py tripwire`, which writes the record, and the gate refuses with its last line and returns 2. The darwin early grant stays first and reads nothing.
- **Fake helper.** `scripts/lint/memory_test_support.py` `fake_memory_readings(root: Path, *, available_kib: int = 67108864, total_kib: int = 67108864, full_avg10: float = 0.0) -> dict[str, str]` writes `root/"meminfo"` and `root/"pressure"` and returns `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE` and `BUILDLOG_LIVE_MEMORY_TRIPWIRE`. An inherited tripwire value stays in use (an empty value stays empty); `root/"live-memory-reads.jsonl"` applies only when the variable is absent. A test that changes a reading rewrites the returned files.
- **Tests.** `test_brp_launch_gate.py` and `test_build_hold.py` take their memory variables from the helper; a launch with the tripwire set and pressure unfaked is denied with the `/proc/pressure/memory` text. `test_memory_admit.py` sets the tripwire module-wide and tests the Python, symlink, queue, unwritable-record and shell refusals, fake readings that admit with no record, and live behavior with the tripwire unset.

**Files:**
- `scripts/lint/memory_admit.py` — `LiveMemoryReading`, `check_memory_reading`, the `tripwire` action, `main`.
- `scripts/lint/memory_gate.sh` — the live-path check before the `MemAvailable` read.
- `scripts/lint/memory_test_support.py` — `fake_memory_readings`.
- `scripts/lint/test_memory_admit.py`, `scripts/hooks/test_brp_launch_gate.py`, `scripts/build_hold/test_build_hold.py` — the helper and the tripwire tests.
- `docs/as-built/build-memory-admission.md` — the tests invariant names both readings and the helper; the tripwire; the unfaked-pressure gotcha.

**Binds later work:** the `fake_memory_readings` signature and its inherited-tripwire rule; the refusal text; the record line shape (a JSON object, not an argv list); the gate that fails a tripped module reads only recorded lines.

**Gotchas:**
- A shell-side record's `argv` names the checker (`memory_admit.py tripwire <path>`), not the test process.
- Tests that trip on purpose use a private record path, never the inherited one.
- `test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty` reads live `/proc/pressure/memory` by design.
- Module selection follows import edges (`affected_modules.py`), so a change to `memory_test_support.py` selects every module that imports it; there is no shared-helper list.

**Ruled out:** a second JSON/flock writer in bash (the shell delegates to `memory_admit.py tripwire`); the process table in the tripwire (the gate reads only the pids the test's ledger names).

### Phase 2 — The merge gate sets the tripwire for every test module, and every caller's tests fake both readings  · status: done

#### As-built

- `test_module_environment` sets `BUILDLOG_LIVE_MEMORY_TRIPWIRE=<module tmp>/live-memory-reads.jsonl` for every test module. The `types` suite gets no tripwire.
- `finish_gate_job` reads that file. Each line is `{"reading": <path>, "argv": [...], "pid": <pid>}` and adds `<module>: a test read live memory <reading>: <args>` to the output and sets status 1. A line that does not parse also trips the module, with its raw text in the output.
- `GateJobResult.tripwire_tripped` (was `build_tool_started`) is true for a build-tool call or a live memory reading. A tripped module is not rerun alone, and the hold retry keeps the result.
- The five pressure-only leaks (`test_record.py`, `test_mac_idle.py`, `test_verify_doc.py`, `test_verify_release_ack.py`, `test_dailies_render_holds.py`) fake both readings with `fake_memory_readings`. No other module in the suite reads live memory.

**Files:**
- `scripts/production/merge_gate.py` — tripwire environment, record read, tripped result.
- `scripts/production/test_merge_gate_suites.py` — unit and end-to-end tripwire tests.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/direct/test_verify_doc.py`, `scripts/direct/test_verify_release_ack.py`, `scripts/production/test_dailies_render_holds.py` — fake both readings.
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — the tripwire, its two limits, and the shipped floor sweep order.

**Binds later work:** every test module runs under the tripwire in the merge gate, so a new test fakes both readings with `fake_memory_readings` or fails the gate. A test that trips on purpose uses a private record path, never the inherited one.

**Gotchas:**
- `test_memory_admit.py` `test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty` reads live pressure with the tripwire removed, by design.
- A reading whose record write fails still refuses but leaves no record. The gate sees only recorded readings.
- A shell-side read records through `memory_admit.py tripwire <path>`, so `argv` names the checker, not the test. The output line takes the module name from the job.

**Ruled out:** module medians from `/build:report`, which has no module rows; the merge gate's own timing history is the source.

### Phase 3 — The floor sweep keeps build output that is in use  · status: done

#### As-built

- `hold_floor` in `scripts/lint/sweep.py` orders every idle target by last use, oldest first. Last use is the newer of the `.lint-sweep-used` stamp and the fingerprint atimes. No `/tmp` target goes first. A target used within `recent_minutes` (`[sweep]` in `config/lint.conf`, default 60) is taken only when older targets cannot meet the floor. The per-target budget sweep uses the same order.
- The floor pass plans without a lock. It then takes each target's cargo lock without blocking, one target at a time, skips a busy target, and stops when the shortfall is met. The floor-root scan runs on 4 threads (202 s to 123 s).
- Each floor and budget take appends a line to `~/.local/state/lint-sweep/removals.jsonl`. Past 1 MiB, the file is cut to the newest 512 KiB. `sweep.py rebuilds --days N` reads this record and the build log. For each step with `crates_compiled >= 50`, it names the cause of the cold rebuild (`floor take`, `budget take`, `tmp sweep`, `none`) and prints compile-hours per cause and clone kind. It writes nothing.

**Files:**
- `scripts/lint/sweep.py` — floor and budget sweeps, per-target locks, removal record, `rebuilds` report.
- `scripts/lint/test_sweep.py` — order, lock, removal record and rebuild tests.
- `config/lint.conf` — `[sweep]` keep window and scan thread settings.
- `docs/as-built/build-targets.md` — the sweep order, `recent_minutes` and `rebuilds`.

**Binds later work:**
- The shared-folder sweep extension: `sweep.py` keys use by workspace and finds clone kind from the path (gate or merge, `/tmp/claude/scratch`, phase). A folder shared by many clones is one target with many users; the keep window and the per-target lock (the cargo build-dir lock) cover all of them, and `rebuilds` attributes by clone path.
- The typed reclamation split builds on `freed_by()` and the lstat path, and owns the over-count when a hard link outside the target shares the inode.
- The tmp_sweep memory phase: the `sweep.py` floor scan now runs 4 threads, so a working-set reading of `sweep.py` must come from this code.
- The clone cache trial uses `sweep.py rebuilds` for its before/after cold compile-hours per clone kind.

**Gotchas:**
- Unlinking one hard-linked copy changes the shared inode's ctime. Output identity is (dev, ino, size, mtime), never ctime.
- `freed_by()` runs after `remove()` in `shrink_floor()`, but before deletion in `choose()`.

**Ruled out:** a first-builds column in `rebuilds`; the shared-folder sweep extension computes first builds from build-log records.

### Phase 4 — Short-lived clones share one build folder per repository  · status: todo
**Depends on:** Phase 3, Phase 9 (the sweep change this phase extends), and the case D numbers from verify-unit's Phase 1 trial (the user approved verify-unit's copy plan on 2026-10-29; case D runs under verify's build hold after 00:00 EDT). The adopt rule picks this phase or drops it.

#### Work Order

**Goal:** a new temp clone of a Rust repository reuses the build output of earlier clones of the same repository, if a measured trial shows that this saves work without making builds wait. Approved by the showrunner 2026-10-29 (W2). Measurement: mktemp folders (91.2 compile-h in 7 days, 4.6 steps per clone, 61.6 cold-h on first builds) and style-sync folders (10.9 h) rebuild everything on their first build.

**Spec:**

1. **Trial first.** (Showrunner, 2026-10-29: if the user approves verify-unit's copies plan, this trial runs as case D of verify's Phase 1, in the same build-hold session, so its numbers compare with the seeding cases. The build work in items 3-5 waits on the pick that plan's rule makes from those numbers.) On hana, make two clones at one commit with `scratch.py new`. Build the first, then the second with a shared `CARGO_BUILD_BUILD_DIR` per repository. Record crates compiled and wall time for each. Then run two builds at once on the shared build folder and record the lock wait. Use `verify.sh build` for every build.
2. **Adopt rule.** Adopt only when the second build compiles under half the crates of the first, and the parallel lock wait is under 10% of build time. Otherwise the phase records the numbers in the plan, changes no code, and stops.
3. **If adopted:**
   - `scripts/lib/scratch.py`: `new()` (:182) makes a task-owned folder today; it does not clone and takes no repository. Add an explicit clone operation (repository, commit, destination) that sets the shared build folder for that repository. Name the exact command in the as-built.
   - `scripts/hooks/pre-tool-use-scratch-guard.py` steers a hand clone of a Rust repository into `/tmp` or a scratchpad to that clone operation.
   - `scripts/style/style_sync.py` already shares one `style-sync-target` per repository (:330) and serializes checkout creation through cleanup. Change only what the shared-folder lifetime needs, and keep its lock.
   - **Shared-target identity.** One mapping from repository to its shared folder. The folder has one use stamp for all its clones, a lifetime that does not end with one scratch task, and Cargo's build-folder lock. `rebuild_causes()` today finds a workspace by the target's parent path; give it the mapping, so a removal of the shared folder names the repository.
   - The disk sweeps treat the shared build folder as one target with its own last use. The lock boundary returns a typed result (`TargetLocksAcquired | TargetLockBusy`), not `tuple[list[int], str | None]`.
4. **Tests** for each changed module, in `scripts/lib/test_scratch.py`, `scripts/hooks/test_scratch_guard.py`, `scripts/style/test_style_sync.py` and `scripts/lint/test_sweep.py`: two clones on one target, a busy shared target, and rebuild attribution after its removal. The existing busy-skip and exhaustion tests stay as gates.
5. **Docs.** The scratch as-built names the shared build folder and the guard's steer.

**Files:**
- `scripts/lib/scratch.py` — item 3.
- `scripts/lib/test_scratch.py` — item 4.
- `scripts/hooks/pre-tool-use-scratch-guard.py` — item 3.
- `scripts/hooks/test_scratch_guard.py` — item 4.
- `scripts/style/style_sync.py` — item 3 (owner natedev style-unit; trial-merge against `origin/natedev`).
- `scripts/lint/sweep.py` — item 3, the shared folder as one target.
- `scripts/lint/test_sweep.py`, `scripts/style/test_style_sync.py` — item 4.

**Workers:** `1 writer + 1 tester`
- `impl` — the trial (item 1), the adopt decision record, items 3 and 5.
- `test` — item 4, after the adopt decision: `test_scratch.py`, `test_scratch_guard.py`, `test_style_sync.py`, `test_sweep.py`.

**Acceptance gate:**
- The trial numbers (crates compiled, wall time, lock wait) are in the phase report with the adopt decision.
- If adopted: the tests of each changed module pass; `basedpyright` over each changed file reports `0 errors, 0 warnings, 0 notes`; 24 h after merge, the first-build cold-h of mktemp and scratch.py folders, next to the 7-day figures. `rebuilds` does not separate first builds (a workspace's first step has cause `none`), so compute it from the build-log records: the first recorded step of each new workspace folder. The removal records in `rebuilds` name the shared folder's repository.
- The notice has an `also touches scripts/style/style_sync.py (owner natedev style-unit)` line, tested against `origin/natedev`.
- Change-Review line: a new temp clone of hana builds only the crates that changed | in any Claude session on natedev, make a clone with `python3 ~/.claude/scripts/lib/scratch.py new hana` and build it twice in two clones | the second clone's build log step compiles under half the crates of the first.

### Phase 5 — Clone builds turn incremental mode off, and mend uses the compile cache  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** builds in temp clones store their workspace crates in sccache, and mend uses sccache, if a measured trial shows hits. Approved by the showrunner 2026-10-29 (W3). Measurement: workspace crates build with `-C incremental`, so sccache never caches them; mend runs with `RUSTC_WRAPPER=` (`scripts/lint/invoke.sh:582`), 31.6 cold-h in `/tmp` this week; sccache is full at 64 of 64 GiB.

**Spec:**

1. **Trial first.** Two hana clones at one commit. Build both with `CARGO_INCREMENTAL=0` and record sccache hits for workspace crates on the second (stats before and after). Find why mend clears `RUSTC_WRAPPER` (does `cargo mend` drive rustc itself?), and test mend with sccache in a clone.
2. **Adopt rule.** Adopt each change only on a measured gain: workspace-crate hits on the second clone, or a shorter mend run with the same result. The baseline is dated after Phase 3 (`e3b8e391`), so a sweep gain is not counted as a cache gain. The trial is case E in verify-unit's build hold (showrunner, 2026-10-29).
3. **If adopted:** `scripts/direct/verify.sh` sets `CARGO_INCREMENTAL=0` for builds in a temp clone (not in `~/rust` worktrees, where incremental helps); `scripts/lint/invoke.sh` runs mend with sccache.
4. **sccache size and disk room.** Report the full cache and the disk numbers to natedev, which decides the cache size and the disk room as root.
5. **Tests** in the verify-unit test modules that cover the changed lines.

**Files:**
- `scripts/direct/verify.sh` — item 3 (owner verify-unit; trial-merge against `origin/natedev-verify` and `origin/natedev`).
- `scripts/lint/invoke.sh` — item 3 (owner verify-unit; same trial merges).
- `scripts/direct/test_verify_clone_incremental.py` — new — item 5, the tests for the changed `verify.sh` lines (a new module, so verify-unit's own test modules stay untouched).
- `scripts/lint/test_invoke_scope.py` — item 5, the tests for the changed `invoke.sh` lines (owner verify-unit).

**Workers:** `1 writer + 1 tester`
- `impl` — the trial, the adopt decision record, item 3, the item 4 message text for the director.
- `test` — item 5, after the adopt decision.

**Acceptance gate:**
- The trial numbers are in the phase report with each adopt decision.
- If adopted: the changed modules' tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the notice has `also touches` lines for verify.sh and invoke.sh (owner verify-unit), tested against `origin/natedev-verify` and `origin/natedev`.
- Change-Review line: temp-clone builds and mend runs reuse the compile cache | in a terminal on natedev, `sccache --show-stats` before and after a second clone's build | the Rust hit count rises by the workspace crates of the second build.

### Phase 6 — A session can launch an app that is already built without waiting at the BRP launch gate  · status: todo
**Depends on:** Phase 1. verify-unit's cancel-on-change and memory phases also change `verify.sh` and merge first. Merge `origin/builds` after they land. Before the checkpoint notice, trial-merge against `origin/natedev-verify-cancel`, `origin/natedev-verify-memory`, `origin/natedev-verify` and `origin/natedev-verify-token`. The notice names verify-unit in its also-touches line.

**Ruling:** showrunner 2026-10-29 17:00 EDT: the hook decides from a build record that `verify.sh`'s build step writes.

#### Work Order

**Goal:** the user's words (2026-10-29): "add it to the queue to make it so that a session can bypass the hook if it knows the app is already built". The BRP launch gate (`scripts/hooks/pre-tool-use-brp-launch-gate.sh`) treats every launch as a build and waits for memory admission. A launch whose binary is already built and up to date skips the wait.

**Spec:**

1. **The hook decides from a build record that the build wrote.** `scripts/direct/verify.sh`'s build step (only the build step) writes a record beside each binary it builds: the `rustc -vV` hash, profile, target, rustflags, features and the build start time. For a `brp_launch_bevy_app` or `brp_launch_bevy_example` call, the hook skips the memory wait only when the launch's inputs match the record AND every dep-info input time is older than the build start. The launch's path comes from `tool_input.path`. No path means the launch waits, because BRP searches from the MCP server's working directory. A missing record, a missing path or any difference means the launch waits. No compiler starts; `cargo build --offline` is still a build and is not allowed (Phase 3 architect review, F5). The hook's build-hold check stays as it is.
2. **A session can also say so.** A launch call whose session set `BRP_LAUNCH_PREBUILT=1` (or the tool's own flag, if the BRP tool has one) skips admission only when item 1 confirms the binary is fresh; a stale binary still waits, and the hook prints why.
3. **Tests** in `scripts/hooks/test_brp_launch_gate.py`: fresh binary skips the wait; stale, missing and uncertain binaries wait and start no compiler; the session flag with a stale binary waits; fake memory readings throughout (Phase 1 helper). Add a test of the record write in `verify.sh`'s build step with a fake cargo. Keep the suite fast: it grew to 100 s; the target is the old 55 s or less. The hook's probe stays under 1 s.

**Files:**
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — items 1–2.
- `scripts/hooks/test_brp_launch_gate.py` — item 3.
- `scripts/direct/verify.sh` — the build-step record only (item 1; owner verify-unit).
- `scripts/direct/test_verify_token_wait.py` — the record-write test (item 3); it holds the tests of `verify.sh`'s build step.
- `docs/as-built/build-memory-admission.md` — the hook section. Phases 2 and 10 also edit this doc; the writer puts the section in the phase folder (`as_built_section.md`) and the director places it.

**Workers:** `1 writer`
- `test` — items 1–3 and the doc section, this round (it holds the thread).

**Acceptance gate:**
- `test_brp_launch_gate.py` passes; the hook's added checks take under 1 s on a fresh hana binary.
- Change-Review line: launching an already-built Bevy app through BRP starts at once | in a Claude session on natedev with a built hana, launch it with the BRP launch tool | it starts without a `waiting for memory` line.

### Phase 7 — A one-line command summarizes the machine's working set, and the disk-floor sweep uses less memory  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** the user's words (2026-10-29): "investigate why we use 12 GiB of running python helpers - we have to reduce our working set - more generally you should provide a quick way for me to summarize our working set by categorize that i can run from a simple shell command anytime". The survey (2026-10-29 10:50 EDT) found the causes; the style_plan cache, loss_census90 and codex_mesh follow findings went to the showrunner to route to their owners. This phase adds the summary command and cuts this unit's own share: the disk-floor sweep's 1.3–3.9 GB peak (`scripts/lint/tmp_sweep.py` :527–543, :1157).

**Spec:**

1. **`working-set` command.** A script in this unit's files (`scripts/build_targets/working_set.py`, with a shell name on PATH through the existing command wrappers) that prints, in under 2 s: memory by category (cargo/rustc, Claude sessions, Codex, Python helpers by script, other), resident PSS and swap for each, the top processes per category, and the machine totals (available, swap used). It reads `/proc` and one owner table kept in the script, and writes nothing. Its categories come from one table in the script. A process whose readings are gone or refused is counted as unavailable, not dropped.
2. **Python helpers grouped by script and owner.** The Python row breaks down by script path and names the owning unit from the production docs' Owns cells.
3. **The sweep's peak.** `tmp_sweep.py` stops holding the whole scan in memory at :527–543 and :1157 (stream the entries, keep only what the decision needs). The peak during a sweep falls under 300 MB, measured with `/usr/bin/time -v` on a dry run (`tmp_sweep.py --dry-run`) over the live `/tmp`, with a baseline taken after `e3b8e391`. This is `tmp_sweep.py`, not `sweep.py`, whose four scans Phase 3 added. Hard-link accounting, retention and keep-on-unavailable-evidence stay. The process-use boundary returns `ProcessUsesKnown | ProcessUsesUnavailable`, not `list[...] | None`.
4. **Tests** for the categories table on a fixture `/proc` tree, and the sweep's existing tests still pass.

**Files:**
- `scripts/build_targets/working_set.py` — new — items 1–2.
- `scripts/build_targets/test_working_set.py` — new — item 4.
- `scripts/lint/tmp_sweep.py` — item 3.
- `scripts/lint/test_tmp_sweep.py` — item 4.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–3.
- `test` — item 4.

**Acceptance gate:**
- `working-set` prints the table in under 2 s on natedev, and its machine totals (available, swap used) agree with `free -m` within 5%. Summed process PSS is a different quantity and is not compared.
- The PATH wrapper is in `/etc/nixos/modules/common/agents.nix` (natedev's file; the director routes it).
- A sweep run's max resident size is under 300 MB.
- Change-Review line: one command prints the machine's memory by category | in a terminal on natedev, type `working-set` | a table of cargo, Claude, Codex, Python by script, and other, with PSS and swap per row.

### Phase 8 — Shared build state is written only by the installed copy  · status: todo
**Depends on:** Phase 2; Phase 5 (`verify.sh`, `invoke.sh`); Phases 10, 12 and 11 (`memory_admit.py`, its tests and doc); report-unit's bare-step writer in `waiter_identity` merged into builds

**Approved:** showrunner 2026-10-29, after Phase 2. report-unit keeps its bare-step writer (it fixes the cause in main) and drops only its read-side strip; this phase builds the repository field on report-unit's merge. The edit to `builds-production.md:14` is approved.

#### Work Order

**Goal:** the showrunner's ruling (2026-10-29 13:15, on the user's word that the current state is a bad design): shared coordination code (memory gate, queue rows, holds) writes shared state from one installed copy, `~/.claude` main. Every caller runs that copy, never its own checkout's. Tests keep their fakes. Queue rows carry the repository and the step as separate fields.

Measured 2026-10-29 13:20 EDT (read-only survey of `/proc` and `~/.local/state/buildlog/admission`):
- All 19 live `.waiting` rows carry the combined label ("hana clippy"). Main's own `memory_admit.py` writes it (`:1066` `waiter_identity`, `"step": f"{repo} {step}"`, rewritten on each held poll at `:1037`). The 13 `.reservation` rows already keep `repo` and `step` apart. So the old format is main's writer, not an old copy.
- The read-side prefix strip is not in main. It is an uncommitted change in report-unit's worktree (`scripts/lint/memory_admit.py` :1126–1128 strip, :1068–1071 bare-step writer).
- Checkout copies that run today: `verify.sh`'s relative lookups of `override_guard.sh` (:212), `local_release_wait.py` (:230, loads `scripts.build_hold` from its own tree), `mesh_runs.py` (:983), `mac_idle.py` (:1324, writes Mac-idle markers) and the `build_targets` fallback (:228); `memory_gate.sh:26` names `memory_admit.py` beside itself with no override; `merge_gate.py:52,59` imports `build_hold` from the copy that runs (it reads holds only); `scripts/message/top_level.py:80` and `scripts/production/retire_unit.py:75-79` write holds through the copy that runs. One live checkout `verify.sh` (claude-build-followups-verify, real `HOME`, no fake dirs) held a queue row at 13:20.
- Already main by registration: the BRP launch hook, the build-hold hook, the `cargo` wrapper, `/build:queue`.

**Spec:**

1. **The writers refuse a checkout copy.** `memory_admit.py` and `build_hold.py` refuse to write the real state directories (`~/.local/state/buildlog/admission`, the real hold dir) when their own file is not the installed copy under `~/.claude/scripts/`. With a fake `BUILDLOG_DIR` / `BUILD_HOLD_DIR` (every test), they write as now. The refusal names the installed path to run instead. This one guard is the fix at the cause; items 2–4 make every caller pass it.
2. **Queue rows carry separate fields.** report-unit's merged writer already gives `.waiting` rows a bare `step`; this phase adds the `repository` field beside it, as `.reservation` rows carry `repo`. The queue view's reader takes the fields; no prefix strip exists anywhere. `build_queue.py` shows the repository beside the step for waiting and running rows alike (report-unit's file; its one display change only).
3. **Callers run the installed copy.**
   - `memory_gate.sh`: `BUILDLOG_MEM_ADMIT_SCRIPT="${BUILDLOG_MEM_ADMIT_SCRIPT:-$HOME/.claude/scripts/lint/memory_admit.py}"`. `invoke.sh:89` sources `$HOME/.claude/scripts/lint/memory_gate.sh`.
   - `verify.sh`: one root, `SCRIPTS="$HOME/.claude/scripts"`, for `override_guard.sh`, `local_release_wait.py`, `mesh_runs.py`, `mac_idle.py` and the `build_targets` fallback. Tests fake `HOME` with a copied tree already; the `VERIFY_*_SCRIPT` overrides stay.
   - `merge_gate.py`: imports `build_hold` from `Path.home() / ".claude"`.
   - `top_level.py`, `retire_unit.py`: the `build_hold.py` path from `Path.home() / ".claude"`.
4. **The plan lines that teach a checkout run** (`docs/plans/builds-production.md:14`, `docs/plans/build-followups-worker-seats.md:48`) name `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout <tree>`.
5. **Tests:** `scripts/build_hold/override_guard.py` already finds the real home through the account database and refuses fake state variables when the installed script runs. Keep that refusal. Routing tests copy scripts under a fake home; the checkout-write refusal is tested against an injected canonical-home and state fixture, never real coordination state. A checkout-copy `memory_admit.py` against the real-dir path refuses (exit 2, named reason) and against a fake dir writes; same for `build_hold.py`; a `.waiting` row has `repository` and a bare `step`; the queue view shows both; `verify.sh` and `memory_gate.sh` resolve to `$HOME/.claude/scripts` under a fake `HOME`. Fake memory readings throughout (Phase 1 helper).

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/lint/test_memory_admit.py` — items 1–2, 3, 5.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — items 1, 5.
- `scripts/build_queue/build_queue.py`, `scripts/build_queue/test_build_queue.py` — item 2 (owner report-unit).
- `scripts/direct/verify.sh`, `scripts/lint/invoke.sh` — item 3 (owner verify-unit). The tests that cover the changed lines are named here at phase start, from a grep of each path.
- `scripts/production/merge_gate.py` — item 3 (owner natedev showrunner).
- `scripts/message/top_level.py`, `scripts/production/retire_unit.py` — item 3 (owners per natedev's Owners table, named in the notice).
- `docs/plans/builds-production.md`, `docs/plans/build-followups-worker-seats.md` — item 4.
- `docs/as-built/build-memory-admission.md` — the one-copy rule and the row fields.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–4 and the doc.
- `test` — item 5.

**Acceptance gate:**
- The changed modules' tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`.
- Live, after install: every `.waiting` row in `~/.local/state/buildlog/admission` has `repository` and a bare `step` within one held poll; a run of a checkout copy's `memory_admit.py` against the real dir exits 2 with the named refusal.
- The notice has `also touches` lines for verify.sh and invoke.sh (verify-unit, against `origin/natedev-verify` and `origin/natedev`), merge_gate.py and the hold callers (their owners, against `origin/natedev`), and build_queue.py (report-unit, against `origin/builds-report`); it confirms no read-side prefix strip remains in `memory_admit.py` or `build_queue.py`.
- Change-Review line: `/build:queue` shows the repository and the step in separate columns | in a Claude session on natedev while a build waits, type `/build:queue` | each row shows e.g. `hana` and `clippy` apart, and no row reads "hana clippy".

### Phase 9 — The floor sweep counts what a deletion really frees  · status: done

#### As-built

- `scripts/lint/sweep.py` keeps two typed counts apart. `ReclamationEstimate(bytes)` comes from the scan's link counts: `freed_by()` returns it, before any deletion. `ConfirmedDeletion(bytes, failures)` comes from the deletion: `remove()` returns it, summed from each unlink.
- `remove_entry_at()` reads `os.lstat` just before each unlink. A file's blocks (`st_blocks * 512`) count as freed only when `st_nlink` is 1, so a file with a link outside the target frees nothing. A link that the same pass removed earlier has already lowered the count, so the last link still counts as freed. A failed or partial removal credits only the files it removed.
- `reclaim(group, scan, remaining, dry_run)` returns the estimate on a dry run and the confirmed account on a real run. Both callers, `shrink()` (orphans and the budget pass) and `shrink_floor()`, use it and the same two types. A dry run deletes nothing and records nothing.
- `choose()` is gone. `shrink()` and `shrink_floor()` take the next candidate in removal order and subtract the confirmed bytes, until the account covers the shortfall. When no candidate covers it, the exhaustion alert still goes out.
- `remove_entry()` opens the parent with `O_DIRECTORY | O_NOFOLLOW`. The recursive delete walks with directory fds and checks each directory's identity with `os.path.samestat` before it opens it and again before `rmdir`. A path replaced by a symlink during the pass is not followed and not credited.

**Files:**
- `scripts/lint/sweep.py` — the two types, `reclaim`, `remove_entry`, `remove_entry_at`, and the loops in `shrink` and `shrink_floor`.
- `scripts/lint/test_sweep.py` — tests for an outside link added after the scan, an earlier unlink in the same pass, a failed and a partial removal, the dry run, a directory swapped for a symlink, and the exhaustion alert when an outside link leaves the floor short.

**Binds later work:** Phase 4 extends this sweep; its deletions go through `reclaim` and credit only `ConfirmedDeletion` bytes. A new caller of the sweep uses the same two types and does not count from the scan on a real run.

### Phase 10 — Memory admission names the reservation a waiting step waits on  · status: done

#### As-built

- `memory_admit.py` has `current_blocker(waiter_path)`. It reads the admission ledger under a shared lock. It does not create the lock, prune stale records or change state files. It uses the same queue view and `decide` rule as `check`; there is no second admission rule.
- The lookup names a reservation only when removing it alone admits the waiter and no other single reservation does. It removes that reservation's full build charge and growth promise, then applies the admission rule. It never assigns a pressure hold to a reservation.
- The verb `memory_admit.py blocker --waiter <waiter file>` prints one JSON line and exits 0: `{"session_id":…,"session_name":…,"pool":"memory"}` when the session is known; `{"unit":…}` when only the cgroup sidecar names a systemd scope or service; `{}` for a ready step, a pressure hold, queue priority, several possible blockers or unknown ownership. A gone waiter file gives exit 2. A stale waiter still on disk gives `{}` and exit 0.
- `CheckResult.blocker` carries the same typed result from the snapshot that admission uses. With `BUILDLOG_MEM_BLOCKER=1`, `check` appends one JSON blocker field to its pipe-separated output: field 16, or field 19 with `BUILDLOG_MEM_DETAIL=1`. JSON escapes a pipe in a name as `\u007c`. Without the opt-in, the 15-field and 18-field forms do not change.

**Files:**
- `scripts/lint/memory_admit.py` — `current_blocker`, the `blocker` verb and the `check` blocker field.
- `scripts/lint/test_memory_admit.py` — tests for each result shape, the exit codes, the `check` field, and that the verb writes nothing.
- `docs/as-built/build-memory-admission.md` — the "Name the reservation that blocks a waiter" section.

**Binds later work:** A change to the admission charge or holds flows into the blocker lookup, because the lookup reuses `decide`; the lookup must follow each new charge and hold. The verify memory wait reads the blocker as `check` field 16 (19 with detail) under `BUILDLOG_MEM_BLOCKER=1`, so it sees a holder change on the poll it already makes, with no separate Python start.

**Gotchas:** Releasing a measured reservation frees only its resident bytes. Its swap bytes stay part of the build charge but do not count as freed RAM.

### Phase 11 — Memory admission promises what each repository's steps really use  · status: todo
**Depends on:** Phase 10 and Phase 12 (same file, `memory_admit.py`); verify-unit's Phase 4 (branch `natedev-verify-memory`). Start after that merge reaches `origin/builds`, and merge `origin/builds` first.

**Approved:** showrunner 2026-10-29 (ETA 22:00 EDT, after Phase 12), from the widget wait study (07:09–11:43 EDT): a measured floor per repository for filtered test runs, and the promises that are 3x or more too high.

#### Work Order

**Goal:** each step's memory promise is close to what that repository's steps use: not so low that a filtered run starts small and then compiles, and not so high that a small repository holds back other builds.

**Measured 2026-10-29 (natedev, 14 days, anon peaks):**
- cargo-liner clippy: keyed rows (repository URL) p90 1.94 GiB over 56 runs; old rows with no key (folder name `cargo-liner`) p90 3.04 GiB over 2061 runs. `expected_peak` mixes both, so the promise is about 3.0 GiB.
- bevy_brp nextest: 5 keyed runs, p90 1.78 GiB. With fewer than 5 own runs, `expected_peak` uses the all-repository pool, p90 10.83 GiB (mostly hana). That gave the 10.9 GiB promise.
- `FILTERED_FLOOR` is one value (1 GiB) for all repositories.

**Spec:**

1. Replay first: for every step in the 14-day history, compute the promise that today's `expected_peak` gives from the history before that step, and compare it to the step's measured anon peak. Report, per repository and step, the count of promises under half the measured peak and over 3x the measured peak. Fresh runs (`crates_compiled` 0) and compile runs are separate rows. Save the table in the as-built doc. The replay runs again on the result: neither count rises for any repository, and each count falls in total.
2. `expected_peak` (`scripts/lint/memory_admit.py`): when a repository has 5 or more keyed rows, use only keyed rows; old unkeyed rows count only when keyed rows are fewer than 5.
3. A repository with 1 to 4 own runs does not take the all-repository pool p90. Use the largest of its own runs, or the pool when that is lower. A repository with no own runs keeps today's rule.
4. A promise does not mix compile runs and no-compile runs (showrunner 2026-10-29 17:11 EDT, from the user's rule). A compile run's promise comes only from earlier runs with `crates_compiled > 0`, in the same order as `filtered_peak` today: the exact filter set, then the package's filtered runs, then the check/build peaks. The 1 GiB floor stays only for fresh runs (`crates_compiled` 0) and for a repository with no compile history; do not raise the fresh floor. Fresh runs are verify Phase 4's: they get the p90 of earlier fresh runs of the same package and scope, at least `FILTERED_FLOOR`. `expected_peak()` returns `min(filtered, full)` today, so a compile-run promise above the full-run estimate would not hold: the compile-run promise wins over that minimum, and a test covers a promise above the full estimate; the replay includes that case (Phase 3 architect review, F11). Comment each new constant with the measurement that set it, as the current constants are.
5. Tests in `scripts/lint/test_memory_admit.py`: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; a compile run's promise uses only compile runs; a fresh run gets the fresh-run p90, at least `FILTERED_FLOOR`; each test fakes both memory readings.
6. Docs: `docs/as-built/build-memory-admission.md` gets the replay table before and after, and the new rules. The replay script lives in the session folder and is not committed.

**Files:**
- `scripts/lint/memory_admit.py` — items 2–4.
- `scripts/lint/test_memory_admit.py` — item 5.
- `docs/as-built/build-memory-admission.md` — items 1 and 6.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The replay after the change: neither count rises for any repository, and each count falls in total. The before and after table goes in the checkpoint notice.
- Change-Review line: memory promises match each repository's real use | on natedev run `cargo clippy` in `~/rust/cargo-liner` and read its reservation in `~/.local/state/buildlog/admission` | its `need` is near 1.9 GiB, not 3.0 GiB.

### Phase 12 — Memory admission counts host swap, sccache compiles and each step's peak  · status: todo
**Depends on:** Phase 10 (same file, `memory_admit.py`). Runs before Phase 11.

**Approved:** showrunner 2026-10-29 14:58 EDT, as proposed after the 14:48 and 14:51 EDT freezes; ETA 19:00 EDT, right after the Phase 10 checkpoint. The showrunner kept `RAMP_SECONDS` at 120 for now: herdr is fixing the swapped herdr server, which was the main cause. If a freeze happens after herdr reports its fix, the director makes `RAMP_SECONDS = 600` as a small fix checkpoint on this branch and sends the notice.

#### Work Order

**Goal:** memory admission does not start new build steps when the machine is already short of memory, because it counts what the builds really use and how full swap is.

**Measured 2026-10-29 (freeze study, evidence in the session folder `freeze/`):**
- 14:47:12–54 EDT: admission started 4 steps with 10.3 GiB of promises in 42 s. Builds anon had reached 25.5–26.9 GiB at 14:31–14:39 against the 21 GiB budget. Swap was 80–87% full, PSI "some" avg10 29–37%, "full" avg10 under 10. About 30 s of thrash followed.
- After `RAMP_SECONDS` (120 s) a step is charged only its RSS at that moment. The 32-thread hana nextest (anon peak 10.6 GiB) was charged 1.96 GiB between test waves.
- Every rustc runs as a child of `sccache.service` (5.95 GiB RSS at 14:50), outside each step's process tree, so no step is charged its compiles. A cargo-liner mend was charged 0.27 GiB against a 3.14 GiB promise.
- `read_meminfo` reads only `MemAvailable` and `MemTotal`; `read_pressure` reads only the "full" line and holds when avg10 > 10.
- The Bevy dylib change (9b60fcd76e) did not raise hana's use: every hana run after 13:50 EDT was inside its earlier range.

**Spec:**

1. **Charge the peak.** A running step is charged the largest of: its ramp promise while inside `RAMP_SECONDS`, its current memory (`tree_memory`), and its observed peak (the step's `.max` file). Never only its current RSS.
2. **Charge sccache.** Read the anon of `builds.slice/sccache.service` (`memory.stat`) and add it to the build charge as its own running entry, so compile memory counts once. A missing or unreadable file adds nothing and does not fail the check.
3. **Read host swap and "some" pressure.** `read_meminfo` also reads `SwapTotal` and `SwapFree`; `read_pressure` also reads the "some" line. Hold when swap used is over 85% and "some" avg10 is over 25%. Lower the "full" trigger from avg10 > 10 to > 5. Each new constant has a comment with the measurement that set it, as the current constants do. The hold result names the reading that caused it.
4. **Space the starts after a hold.** After a pressure hold ends, admit at most one step every 30 s.
5. **Tests** in `scripts/lint/test_memory_admit.py`, each with faked readings (`memory_test_support.py`, with swap lines and a "some" line added to the fake files if the helper does not write them): a step past the ramp whose RSS dropped is charged its peak; sccache anon raises the charge, and a missing sccache file does not; swap over 85% with "some" over 25% holds and either alone does not; "full" avg10 6 holds; two waiters after a hold ends start 30 s apart. A replay of 14:47:12–54 EDT from the freeze study's readings holds all four steps.
6. **Docs:** `docs/as-built/build-memory-admission.md` names the new charge, the swap and "some" holds, and the start spacing. The writer puts the section in the phase folder (`as_built_section.md`) if Phase 2 is not yet committed; the director places it.

**Files:**
- `scripts/lint/memory_admit.py` — items 1–4.
- `scripts/lint/test_memory_admit.py` — item 5.
- `scripts/lint/memory_test_support.py` — item 5, only if the fake files need swap and "some" lines.
- `docs/as-built/build-memory-admission.md` — item 6.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The 14:47 replay holds all four steps. The notice gives the wait cost: how much longer a step would have waited on 2026-10-29 10:00–14:00 EDT, from the same replay over that window.
- Change-Review line: memory admission waits when swap is nearly full | on natedev while swap is over 85% full and PSI "some" avg10 is over 25%, run `cargo clippy` in `~/rust/cargo-liner` | it waits for memory, and its wait line names swap.
