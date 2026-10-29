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
  - `scripts/lint/memory_admit.py` — the admission checker. `check_memory_reading(path)` is the tripwire check: with `BUILDLOG_LIVE_MEMORY_TRIPWIRE` set, a path that resolves to `/proc/meminfo` or `/proc/pressure/memory` appends a record and raises `LiveMemoryReading`. `read_meminfo(path)` reads `MemAvailable`/`MemTotal`; its path comes from the shell (`check <meminfo> …`) or from `BUILDLOG_MEMINFO` (`read_queue`). `read_pressure()` reads `BUILDLOG_MEMORY_PRESSURE`, default `/proc/pressure/memory`, and holds every step when `full avg10` > 10.0. It swallows `OSError`/`ValueError`. Both call `check_memory_reading` before they open the file. Callers: `check` (in two places) and `read_queue` (the `/build:queue` view). `run_action` (actions include `tripwire <path>`), `main` (a `LiveMemoryReading` exits 2 with its text as the last stderr line).
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory`. With the tripwire set it resolves `$meminfo` and refuses a live path through `memory_admit.py tripwire <path>`. Then it reads `MemAvailable` itself from `${BUILDLOG_MEMINFO:-/proc/meminfo}` and starts `memory_admit.py check`. A checker that exits non-zero is refused with its last output line (`buildlog_memory_refuse`). On darwin with `BUILDLOG_MEMINFO` unset it grants at once and reads nothing.
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — sources `memory_gate.sh` and calls `buildlog_wait_for_memory` with no arguments (a launch: no reservation).
  - `scripts/hooks/test_brp_launch_gate.py` — `setUp` :53 takes `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE` and `BUILDLOG_LIVE_MEMORY_TRIPWIRE` from `fake_memory_readings(self.root)` (:74). `test_low_memory_wait_leaves_old_release_record_unchanged` :159 writes its low value to the returned `meminfo` file.
  - `scripts/production/merge_gate.py` — `test_module_environment` :514 gives each test module a private `BUILDLOG_DIR` and the build-tool tripwires (`cargo`, `rustc`, `rustdoc`, `rustup`, `cargo-nextest` first on `PATH`, each appends its argv to `<root>/build-tool-calls.jsonl` and exits 99). `finish_gate_job` reads that file, appends `a test started real <tool>: <args>` to the output, and sets status 1 and `GateJobResult.tripwire_tripped`. The gate never reruns a tripped module alone. Not in any unit's Owns cell: natedev showrunner's file.
  - `scripts/production/affected_modules.py` — affected-only module selection follows import edges (natedev c495a0b2): a change to a test helper selects every module that imports it. There is no `SHARED_TEST_HELPERS` list. natedev showrunner's file.
  - `scripts/production/test_merge_gate_suites.py` — tests of the gate's build-tool tripwire: `test_module_tool_calls_fail_even_when_its_tests_pass` :132 (asserts `a test started real <tool>: <args>` and no `Rerun alone:`), `test_module_can_put_its_own_build_tool_stub_first` :145. natedev 367da4f1 split `test_merge_gate.py` by subject into this module, `test_merge_gate_holds.py` and `test_merge_gate_ledger.py`. natedev showrunner's file.
  - Import idiom for a test helper: `from scripts.lib.nix_env_test_support import NixFixture` (`scripts/direct/test_verify_nix_shell.py` :13); the gate runs each module with the repo root as working directory.
- **Survey (2026-10-29, `grep -c` per test module that names the gate, `invoke.sh`, `verify.sh` or the launch hook):**
  - Fake both readings: `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`, `scripts/hooks/test_brp_launch_gate.py`, `scripts/build_hold/test_build_hold.py` (this unit, through `fake_memory_readings`); `scripts/direct/test_verify_mac_offload.py` and `test_verify_checks_fail_closed.py` (through `verify_mac_offload_test_support.py` :682), `test_verify_lint_scope.py` (`verify_lint_scope_test_support.py` :244), `test_verify_token_wait.py` (`verify_token_wait_test_support.py` :156), `test_verify_untested_examples.py` (:190) (verify-unit).
  - Fake `BUILDLOG_MEMINFO` but not `BUILDLOG_MEMORY_PRESSURE` (pressure leaks), checked on the worktree tree after merge 73a8d903: `scripts/buildlog/test_record.py` (report-unit); `scripts/buildlog/test_mac_idle.py` :333, `scripts/direct/test_verify_doc.py` :137, `test_verify_release_ack.py` :83 (verify-unit); `scripts/production/test_dailies_render_holds.py` :124, :255 (natedev showrunner).
  - Fake `BUILDLOG_MEMORY_PRESSURE` but not `BUILDLOG_MEMINFO`: `scripts/direct/test_verify_hold_wait.py` (verify-unit) — the meminfo read may be stubbed; the tripwire decides.
  - Fake neither but run `verify.sh`, `invoke.sh` or the gate: about 30 more modules (`scripts/direct/test_verify_memory_kill.py`, `test_verify_final_pass.py`, `test_verify_nix_shell.py`, `scripts/hooks/test_build_hold_guard.py`, `scripts/build_hold/test_hold_enforcer.py`, `scripts/lint/test_invoke_scope.py` and others). Many stub the gate or stop before it; a static count cannot tell, so Phase 2 uses the tripwire to find the set.
- **Out of scope (unit director's call, 2026-10-29; a one-line change to the tripwire if wrong):** the process table (`BUILDLOG_PROC`, `process_start_time`). The gate reads it only for the pids that the test's private ledger names, so machine load cannot change an admission through it. The build budget is a constant unless `BUILDLOG_BUILD_BUDGET_GIB` is set: not live.
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
- The typed reclamation split shipped in Phase 9 (`ReclamationEstimate`, `ConfirmedDeletion`, `reclaim`); a hard link outside the target now frees nothing.
- The tmp_sweep memory phase: the `sweep.py` floor scan now runs 4 threads, so a working-set reading of `sweep.py` must come from this code.
- The clone cache trial uses `sweep.py rebuilds` for its before/after cold compile-hours per clone kind.

**Gotchas:**
- Unlinking one hard-linked copy changes the shared inode's ctime. Output identity is (dev, ino, size, mtime), never ctime.
- `freed_by()` runs only on a dry run, through `reclaim()`; a real run credits the `ConfirmedDeletion` that `remove()` returns (Phase 9).

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
   - `scripts/style/style_sync.py` already shares one `style-sync-target` per repository (:332) and serializes checkout creation through cleanup. Change only what the shared-folder lifetime needs, and keep its lock.
   - **Shared-target identity.** One mapping from repository to its shared folder. The folder has one use stamp for all its clones, a lifetime that does not end with one scratch task, and Cargo's build-folder lock. `rebuild_causes()` today finds a workspace by the target's parent path; give it the mapping, so a removal of the shared folder names the repository.
   - The disk sweeps treat the shared build folder as one target with its own last use. `lock_trees()` (sweep.py :491) returns `TargetLocksAcquired | TargetLockBusy`, not `tuple[list[int], str | None]`.
   - Deletions of the shared folder go through `reclaim()` and count only `ConfirmedDeletion` bytes (Phase 9). Artifacts that Cargo hard-links into a clone's own target folder free nothing while that clone is present; a test in `test_sweep.py` covers a shared folder whose artifacts are still linked from a clone.
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
- If adopted: the tests of each changed module pass; `basedpyright` over each changed file reports `0 errors, 0 warnings, 0 notes`. Post-merge check, 24 h after the merge reaches main (as in Phase 12's ruling): the first-build cold-h of mktemp and scratch.py folders, next to the 7-day figures. `rebuilds` does not separate first builds (a workspace's first step has cause `none`), so compute it from the build-log records: the first recorded step of each new workspace folder. The removal records in `rebuilds` name the shared folder's repository. The phase report records when the check is due; the result goes in a follow-up notice.
- The notice has an `also touches scripts/style/style_sync.py (owner natedev style-unit)` line, tested against `origin/natedev`.
- Change-Review line: a new temp clone of hana builds only the crates that changed | in any Claude session on natedev, make two clones of hana at one commit with the scratch.py clone command that Spec 3 adds (its exact form is in the scratch as-built) and build each with `verify.sh build` | the second clone's build log step compiles under half the crates of the first.

### Phase 5 — Clone builds turn incremental mode off, and mend uses the compile cache  · status: todo
**Depends on:** Phase 3; Phase 12 (`invoke.sh`).

#### Work Order

**Goal:** builds in temp clones store their workspace crates in sccache, and mend uses sccache, if a measured trial shows hits. Approved by the showrunner 2026-10-29 (W3). Measurement: workspace crates build with `-C incremental`, so sccache never caches them; mend runs with `RUSTC_WRAPPER=` (`scripts/lint/invoke.sh:582`), 31.6 cold-h in `/tmp` this week; sccache is full at 64 of 64 GiB.

**Spec:**

1. **Trial first.** Two hana clones at one commit. Build both with `CARGO_INCREMENTAL=0` and record sccache hits for workspace crates on the second (stats before and after). invoke.sh :578-579 says a cache hit suppresses the rustc diagnostics that mend reads. Test that claim: run mend twice with sccache in a clone, and compare the findings of the cached second run with a run that has `RUSTC_WRAPPER=`.
2. **Adopt rule.** Adopt each change only on a measured gain: workspace-crate hits on the second clone, or a shorter mend run with the same mend findings. The baseline is dated after Phase 3 (`e3b8e391`), so a sweep gain is not counted as a cache gain. The trial is case E in verify-unit's build hold (showrunner, 2026-10-29).
3. **If adopted:** `scripts/direct/verify.sh` sets `CARGO_INCREMENTAL=0` for builds in a temp clone (not in `~/rust` worktrees, where incremental helps); `scripts/lint/invoke.sh` runs mend with sccache.
4. **sccache size and disk room.** Report the full cache and the disk numbers to natedev, which decides the cache size and the disk room as root.
5. **Tests** in the new `scripts/direct/test_verify_clone_incremental.py` for the `verify.sh` lines, and in `scripts/lint/test_invoke_scope.py` (owner verify-unit) for the `invoke.sh` lines.

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

### Phase 6 — The BRP launch gate keeps waiting for every launch; the bypass moves to Phase 13  · status: done

#### As-built

- Showrunner ruling B, 2026-10-29 18:13 EDT: ship "every launch waits". There is no bypass and no build record. `scripts/direct/verify.sh` does not change.
- Rounds 1–3 built a freshness check in the hook: a build record from `verify.sh`'s build step, dep-info times, flags and features. Pass 3 found four more edges: feature-name normalization, `env!` and env-dep variables, build-script rerun-if inputs, and a race when the record is published. An Astra design review found that a freshness copy in the hook can never be certain: the hook's environment is not the environment of the MCP server whose cargo BRP runs.
- Result: no code change. The hook stays as Phase 1 left it: every `mcp__brp__brp_launch` call runs the build-hold check (`check-local`) and then `buildlog_wait_for_memory`. The Phase 1 fake memory readings and tripwire tests stay. The bypass diff stays in the run folder (`phases/6/bypass_round3.patch`) for Phase 13.

**Files:** none changed.

**Binds later work:** Phase 13 adds the bypass only through a bevy_brp prebuilt-only launch mode. A freshness check in the hook and a build record are ruled out.

### Phase 7 — A one-line command summarizes the machine's working set, and the disk-floor sweep uses less memory  · status: done
**Depends on:** Phase 3

#### As-built

- `scripts/build_targets/working_set.py` prints memory by category from `/proc`: cargo/rustc, Claude sessions, Codex, Python helpers and other. The Python row breaks down by script path and names the owning unit. Each row gives PSS, swap and its top three processes. The machine totals are total, available and swap used. The script writes nothing. A process that is unreadable or that exited is counted on its own line.
- Owner rules come from `scripts/build_targets/working_set_owners.json`, generated from the production docs' Owns cells (73 rules, 13 units at checkpoint). `load_owners` validates the file; a bad file exits 1 and names the file. The rules are not in the script, because the push-text gate read an owner tuple that named `pushover.py` as a phone send.
- Argv parsing finds the entry point past interpreter options, Node runtime options included. `node --redirect-warnings` is a known gap (follow-up add-on).
- `scripts/lint/tmp_sweep.py` streams its scan. The process-use boundary returns `ProcessUsesKnown | ProcessUsesUnavailable`. `scripts/lint/folder_audit.py` uses the same types.
- Measured on natedev 2026-10-29: `working_set.py` ran in 1.25 s, and its available and swap totals matched `free -m`. `tmp_sweep.py --dry-run` over the live `/tmp` peaked at 36 MB (was 1.3–3.9 GB).
- The `working-set` PATH wrapper is natedev's change in `/etc/nixos/modules/common/agents.nix`, after this reaches main.

**Files:**
- `scripts/build_targets/working_set.py` — new — the category table, the owner lookup and the argv parsing.
- `scripts/build_targets/test_working_set.py` — new — tests on a fixture `/proc` tree, owner file checks and argv forms.
- `scripts/build_targets/working_set_owners.json` — new — the owner rules generated from the production docs' Owns cells.
- `scripts/lint/tmp_sweep.py` — the streamed scan and the typed process-use result.
- `scripts/lint/test_tmp_sweep.py` — tests for the streamed scan and both process-use results.
- `scripts/lint/folder_audit.py` — the same process-use types; an unavailable process scan keeps a due folder.
- `scripts/lint/test_folder_audit.py` — tests for a known empty and an unavailable process-use result, a kept due folder and the lsof fallback.

**Binds later work:** A new script whose owner must show in the table needs an Owns entry in its production doc. Regenerate `working_set_owners.json` from the production docs after an Owns cell changes.

### Phase 8 — Shared build state is written only by the installed copy  · status: todo
**Depends on:** Phase 2; Phase 5 (`verify.sh`, `invoke.sh`); Phases 10, 12 and 11 (`memory_admit.py`, its tests and doc); report-unit's bare-step writer in `waiter_identity` (merged at HEAD 64a94048)

**Approved:** showrunner 2026-10-29, after Phase 2. report-unit keeps its bare-step writer (it fixes the cause in main) and drops only its read-side strip; this phase builds the repository field on report-unit's merge. The edit to `builds-production.md:14` is approved.

#### Work Order

**Goal:** the showrunner's ruling (2026-10-29 13:15, on the user's word that the current state is a bad design): shared coordination code (memory gate, queue rows, holds) writes shared state from one installed copy, `~/.claude` main. Every caller runs that copy, never its own checkout's. Tests keep their fakes. Queue rows carry the repository and the step as separate fields.

Measured 2026-10-29 13:20 EDT (read-only survey of `/proc` and `~/.local/state/buildlog/admission`):
- All 19 live `.waiting` rows carry the combined label ("hana clippy"). Main's own `memory_admit.py` writes it (`waiter_identity`, `"step": f"{repo} {step}"`, rewritten by the held-poll write in `check`). The 13 `.reservation` rows already keep `repo` and `step` apart. So the old format is main's writer, not an old copy.
- The read-side prefix strip is not in main. It is an uncommitted change in report-unit's worktree (`scripts/lint/memory_admit.py` :1126–1128 strip, :1068–1071 bare-step writer).
- Checkout copies that run today: `verify.sh`'s relative lookups in the `override_guard.sh` source line, `LOCAL_RELEASE_WAIT_SCRIPT` (`local_release_wait.py`, loads `scripts.build_hold` from its own tree), `MESH_RUNS_SCRIPT` (`mesh_runs.py`), `MAC_IDLE_SCRIPT` (`mac_idle.py`, writes Mac-idle markers) and the `BUILD_TARGETS_SCRIPT` fallback; `memory_gate.sh:26` names `memory_admit.py` beside itself with no override; `merge_gate.py:52,59` imports `build_hold` from the copy that runs (it reads holds only); `scripts/message/top_level.py:80` and `scripts/production/retire_unit.py:75-79` write holds through the copy that runs. One live checkout `verify.sh` (claude-build-followups-verify, real `HOME`, no fake dirs) held a queue row at 13:20.
- Already main by registration: the BRP launch hook, the build-hold hook, the `cargo` wrapper, `/build:queue`.

**Spec:**

1. **The writers refuse a checkout copy.** `memory_admit.py` and `build_hold.py` refuse to write the real state directories (`~/.local/state/buildlog/admission`, the real hold dir) when their own file is not the installed copy under `~/.claude/scripts/`. With a fake `BUILDLOG_DIR` / `BUILD_HOLD_DIR` (every test), they write as now. The refusal names the installed path to run instead. This one guard is the fix at the cause; items 2–4 make every caller pass it.
2. **Queue rows carry separate fields.** report-unit's merged writer already gives `.waiting` rows a bare `step`; this phase adds the `repository` field beside it, as `.reservation` rows carry `repo`. The queue view's reader takes the fields; no prefix strip exists anywhere. `build_queue.py` shows the repository beside the step for waiting and running rows alike (report-unit's file; its one display change only).
3. **Callers run the installed copy.**
   - `memory_gate.sh`: `BUILDLOG_MEM_ADMIT_SCRIPT="${BUILDLOG_MEM_ADMIT_SCRIPT:-$HOME/.claude/scripts/lint/memory_admit.py}"`. `invoke.sh:89` sources `$HOME/.claude/scripts/lint/memory_gate.sh`.
   - `verify.sh`: one root, `SCRIPTS="$HOME/.claude/scripts"`, for `override_guard.sh`, `local_release_wait.py`, `mesh_runs.py`, `mac_idle.py` and the `build_targets` fallback. Tests fake `HOME` with a copied tree already; the `VERIFY_*_SCRIPT` overrides stay.
   - `merge_gate.py` reads holds only and already runs from `~/.claude` main (Delegation Context, Test). No change. The notice records this.
   - `top_level.py`, `retire_unit.py`: the `build_hold.py` path from `Path.home() / ".claude"`.
4. **The plan lines that teach a checkout run** (`docs/plans/builds-production.md:14`, `docs/plans/build-followups-worker-seats.md:48`) name `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout <tree>`.
5. **Tests:** `scripts/build_hold/override_guard.py` already finds the real home through the account database and refuses fake state variables when the installed script runs. Keep that refusal. Routing tests copy scripts under a fake home; the checkout-write refusal is tested against an injected canonical-home and state fixture, never real coordination state. A checkout-copy `memory_admit.py` against the real-dir path refuses (exit 2, named reason) and against a fake dir writes; same for `build_hold.py`; a `.waiting` row has `repository` and a bare `step`; the queue view shows both; `verify.sh` and `memory_gate.sh` resolve to `$HOME/.claude/scripts` under a fake `HOME`. Fake memory readings throughout (Phase 1 helper).

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/lint/test_memory_admit.py` — items 1–2, 3, 5.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — items 1, 5.
- `scripts/build_queue/build_queue.py`, `scripts/build_queue/test_build_queue.py` — item 2 (owner report-unit).
- `scripts/direct/verify.sh`, `scripts/lint/invoke.sh` — item 3 (owner verify-unit). The tests that cover the changed lines are named here at phase start, from a grep of each path.
- `scripts/production/merge_gate.py` — no change (item 3); the notice records why.
- `scripts/message/top_level.py`, `scripts/production/retire_unit.py` — item 3 (owners per natedev's Owners table, named in the notice).
- `docs/plans/builds-production.md`, `docs/plans/build-followups-worker-seats.md` — item 4.
- `docs/as-built/build-memory-admission.md` — the one-copy rule and the row fields.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–4 and the doc.
- `test` — item 5.

**Acceptance gate:**
- The changed modules' tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`.
- Live, after install: every `.waiting` row in `~/.local/state/buildlog/admission` has `repository` and a bare `step` within one held poll; a run of a checkout copy's `memory_admit.py` against the real dir exits 2 with the named refusal.
- The notice has `also touches` lines for verify.sh and invoke.sh (verify-unit, against `origin/natedev-verify` and `origin/natedev`), the hold callers (their owners, against `origin/natedev`), and build_queue.py (report-unit, against `origin/builds-report`); it confirms no read-side prefix strip remains in `memory_admit.py` or `build_queue.py`.
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

1. Replay first: for every step in the 14-day history, compute the promise that today's `expected_peak` gives from the history before that step, and compare it to the step's measured anon peak. Report, per repository and step, the count of promises under half the measured peak and over 3x the measured peak. Fresh runs and compile runs (Spec 4's rule) are separate rows. Save the table in the as-built doc. The replay runs again on the result: neither count rises for any repository, and each count falls in total.
2. `expected_peak` (`scripts/lint/memory_admit.py`): when a repository has 5 or more keyed rows, use only keyed rows; old unkeyed rows count only when keyed rows are fewer than 5.
3. A repository with 1 to 4 own runs does not take the all-repository pool p90. Use the largest of its own runs, or the pool when that is lower. A repository with no own runs keeps today's rule.
4. A promise does not mix compile runs and no-compile runs (showrunner 2026-10-29 17:11 EDT, from the user's rule). Spec 4 applies to every step. A history row is a compile run when its index step (anon rows: joined by `call_id` and step name to `steps.call_id` and `steps.step`; index rows: their own column) has `crates_compiled > 0`. A row with no match counts as neither. The step being admitted is a compile run unless verify Phase 4's `is_fresh_test` proves it fresh. A compile run's promise comes only from earlier compile runs; for a filtered run the order is the same as `filtered_peak` today: the exact filter set, then the package's filtered runs, then the check/build peaks. The 1 GiB floor stays only for fresh runs and for a repository with no compile history; do not raise the fresh floor. Fresh runs keep verify Phase 4's `fresh_test_peak` unchanged. `expected_peak()` returns `min(filtered, full)` today, so a compile-run promise above the full-run estimate would not hold: the compile-run promise wins over that minimum, and a test covers a promise above the full estimate; the replay includes that case (Phase 3 architect review, F11). Comment each new constant with the measurement that set it, as the current constants are.
5. Tests in `scripts/lint/test_memory_admit.py`: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; a compile run's promise uses only compile runs; an anon row with no `call_id` and step match counts as neither; a fresh run keeps the `fresh_test_peak` promise; each test fakes both memory readings.
6. Docs: `docs/as-built/build-memory-admission.md` gets the replay table before and after, and the new rules. The replay script lives in the session folder and is not committed.

**Files:**
- `scripts/lint/memory_admit.py` — items 2–4.
- `scripts/lint/test_memory_admit.py` — item 5.
- `docs/as-built/build-memory-admission.md` — items 1 and 6.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The replay after the change: neither count rises for any repository, and each count falls in total. The before and after table goes in the checkpoint notice.
- Change-Review line: memory promises match each repository's real use | on natedev run `cargo clippy` in `~/rust/cargo-liner` and read its reservation in `~/.local/state/buildlog/admission` | its `need` is near the compile-only keyed p90 that the replay measures (the phase report gives the figure), not 3.0 GiB.

### Phase 12 — Memory admission counts host swap, sccache compiles and each step's peak  · status: done
**Depends on:** Phase 10 (same file, `memory_admit.py`). Runs before Phase 11.

**Approved:** showrunner 2026-10-29 14:58 EDT, as proposed after the 14:48 and 14:51 EDT freezes; ETA 19:00 EDT, right after the Phase 10 checkpoint. The showrunner kept `RAMP_SECONDS` at 120 for now: herdr is fixing the swapped herdr server, which was the main cause. If a freeze happens after herdr reports its fix, the director makes `RAMP_SECONDS = 600` as a small fix checkpoint on this branch and sends the notice.

**Ruling:** showrunner 2026-10-29 17:56 EDT: ship the peak charge as built. Check 3 h after the merge reaches main (before 00:05 EDT). If the median admission wait is more than 2x the 10:00–14:00 EDT baseline, or any step waits over 30 min while measured build anon is under budget, send the peak-decay patch as a small checkpoint: the peak decays after N = 25 min below it (N from the measured hana nextest wave gaps, max 954 s).

#### Work Order

**Goal:** memory admission does not start new build steps when the machine is already short of memory, because it counts what the builds really use and how full swap is.

**Measured 2026-10-29 (freeze study, evidence in the session folder `freeze/`):**
- 14:47:12–54 EDT: admission started 4 steps with 10.3 GiB of promises in 42 s. Builds anon had reached 25.5–26.9 GiB at 14:31–14:39 against the 21 GiB budget. Swap was 80–87% full, PSI "some" avg10 29–37%, "full" avg10 under 10. About 30 s of thrash followed.
- After `RAMP_SECONDS` (120 s) a step is charged only its RSS at that moment. The 32-thread hana nextest (anon peak 10.6 GiB) was charged 1.96 GiB between test waves.
- Every rustc runs as a child of `sccache.service` (5.95 GiB RSS at 14:50), outside each step's process tree, so no step is charged its compiles. A cargo-liner mend was charged 0.27 GiB against a 3.14 GiB promise.
- `read_meminfo` reads only `MemAvailable` and `MemTotal`; `read_pressure` reads only the "full" line and holds when avg10 > 10.
- The Bevy dylib change (9b60fcd76e) did not raise hana's use: every hana run after 13:50 EDT was inside its earlier range.

**Spec:**

1. **Charge the peak.** A running step is charged the largest of: its ramp promise while inside `RAMP_SECONDS`, its current memory (`tree_memory`), and its observed peak (the step's `.max` file). Never only its current RSS. In the blocker lookup, a release still frees only the step's current resident bytes, not its peak charge.
2. **Charge sccache.** Read the anon of `builds.slice/sccache.service` (`memory.stat`) and add it to the build charge once, so compile memory counts once. A missing or unreadable file adds nothing and does not fail the check. The sccache charge is not a reservation: it is not added to `running` beside `reservations`, it is never a blocker candidate, and `blocker` never names it. A test covers `blocker` with a sccache charge present.
3. **Read host swap and "some" pressure.** `read_meminfo` also reads `SwapTotal` and `SwapFree`; `read_pressure` also reads the "some" line. Hold when swap used is over 85% and "some" avg10 is over 25%. Lower the "full" trigger from avg10 > 10 to > 5. Each new constant has a comment with the measurement that set it, as the current constants do. The hold result names the reading that caused it.
4. **Space the starts after a hold.** After a pressure hold ends, admit at most one step every 30 s. A spacing hold gives `blocker` `{}`, as a pressure hold does.
5. **Tests** in `scripts/lint/test_memory_admit.py`, each with faked readings (`memory_test_support.py`, with swap lines and a "some" line added to the fake files if the helper does not write them): a step past the ramp whose RSS dropped is charged its peak; sccache anon raises the charge, and a missing sccache file does not; swap over 85% with "some" over 25% holds and either alone does not; "full" avg10 6 holds; two waiters after a hold ends start 30 s apart. A replay of 14:47:12–54 EDT from the freeze study's readings holds all four steps.
6. **Docs:** `docs/as-built/build-memory-admission.md` names the new charge, the swap and "some" holds, and the start spacing. The writer puts the section in the phase folder (`as_built_section.md`) while another phase has uncommitted edits to the doc; the director places it.

**Files:**
- `scripts/lint/memory_admit.py` — items 1–4.
- `scripts/lint/test_memory_admit.py` — item 5.
- `scripts/lint/memory_test_support.py` — item 5, only if the fake files need swap and "some" lines.
- `docs/as-built/build-memory-admission.md` — item 6.
- `scripts/build_queue/test_build_queue.py` — a private `BUILDLOG_BUILDS_CGROUP` fixture only (owner report-unit).
- `scripts/lint/invoke.sh` — the sampler's atomic `.max` write only (owner verify-unit).

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The 14:47 replay holds all four steps. The notice gives the wait cost: how much longer a step would have waited on 2026-10-29 10:00–14:00 EDT, from the same replay over that window.
- Assertion-Change lines: these two lines go in the checkpoint commit message, word for word:
  - `Assertion-Change: scripts/lint/test_memory_admit.py::test_full_memory_stall_holds_only_above_ten — Spec 3 lowers the full avg10 limit from 10 to 5; the test is renamed test_full_memory_stall_holds_only_above_five and its boundary cases move with it (4, 5 admit; 6 holds)`
  - `Assertion-Change: scripts/lint/test_memory_admit.py::test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty — the default path's read is faked, because tests never read the live /proc/pressure/memory (as-built Invariants); the assertion is unchanged`
- The notice has `also touches` lines for scripts/lint/invoke.sh (verify-unit, tested against origin/natedev-verify and origin/natedev) and scripts/build_queue/test_build_queue.py (report-unit, tested against origin/builds-report). `bash -n scripts/lint/invoke.sh` passes.
- Change-Review line: memory admission waits when swap is nearly full | in a terminal on natedev, run `cargo clippy` in `~/rust/cargo-liner` with `BUILDLOG_MEMINFO` and `BUILDLOG_MEMORY_PRESSURE` pointing at files with SwapFree under 15% of SwapTotal and `some avg10=30.00` | it waits for memory, and its wait line names swap.

### Phase 13 — A session can launch an app that is already built without waiting at the BRP launch gate  · status: todo
**Depends on:** Phase 6, and a bevy_brp prebuilt-only launch mode (repository `~/rust/bevy_brp`, outside this production; the showrunner takes it to the user). This phase does not start until that mode is on bevy_brp's main and installed as `~/.cargo/bin/bevy_brp_mcp`.

#### Work Order

**Goal:** the user's words (2026-10-29): "add it to the queue to make it so that a session can bypass the hook if it knows the app is already built". The BRP launch gate (`scripts/hooks/pre-tool-use-brp-launch-gate.sh`) treats every launch as a build and waits for memory admission. A launch that asks bevy_brp to run only a binary that is already built skips the wait.

**The launch tool today (read 2026-10-29):**
- One tool, `brp_launch` (`mcp/src/tool/name.rs:452`), launches apps and examples. `settings.json` registers the hook on matcher `mcp__brp__brp_launch`, and `test_brp_launch_gate.py` sends that `tool_name`. `tool_input` has `target_name`, `profile`, `path`, `package_name`, `port`, `instance_count`, `env`, `search_order` and `args`.
- An app already skips cargo when bevy_brp's own freshness check says Fresh. An example always builds. Cargo is called at `launch/build.rs:177`.
- The smallest prebuilt-only mode is a `prebuilt: bool` parameter that skips `ensure_built` at `orchestration.rs:215`. In that mode, a missing binary should return `tool_call_failed_with_details`.

**Spec:**

1. **The parameter decides.** The hook skips memory admission only for a `mcp__brp__brp_launch` call whose `tool_input` sets the prebuilt-only parameter (use the name bevy_brp ships). Every other launch waits as today. The build-hold check stays first for every launch. The hook reads only `tool_input`: a session cannot set an environment variable for the hook, because the hook inherits Claude Code's environment from session start. Read stdin once. Parse `session_id` and `tool_input.<param>` from that one read. Skip admission only when the value is JSON `true`. A missing, non-boolean or unparseable value waits, as today. A parse failure still leaves `session_id` empty.
2. **No freshness check in the hook and no build record.** Phase 6 showed that they cannot be certain.
3. **Tests** in `scripts/hooks/test_brp_launch_gate.py`: a prebuilt call skips admission; a call without the parameter waits; a build hold still refuses a prebuilt call; fake memory readings throughout (Phase 1 helper). The suite stays at 55 s or less.
4. **Docs.** `docs/as-built/build-memory-admission.md` gets the as-built text in its hook section.

**Files:**
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — items 1–2.
- `scripts/hooks/test_brp_launch_gate.py` — item 3.
- `docs/as-built/build-memory-admission.md` — item 4. The writer puts the section in the phase folder (`as_built_section.md`) while another phase has uncommitted edits to the doc; the director places it.

**Workers:** `1 writer`
- `impl` — items 1–4.

**Acceptance gate:**
- `test_brp_launch_gate.py` passes in 55 s or less; `basedpyright` over it reports `0 errors, 0 warnings, 0 notes`; `bash -n` passes on the hook.
- Live on natedev with a built hana: pipe the hook JSON of a prebuilt launch into the installed hook with `BUILDLOG_MEMINFO` pointing at a file with MemAvailable under the threshold. It exits 0 at once with no deny and no `waiting for memory` line. The same JSON without the parameter prints `waiting for memory` (run it under `timeout 10`).
- Change-Review line: launching an already-built Bevy app through BRP with the prebuilt option starts at once | in a Claude session on natedev with a built hana, launch it with the BRP launch tool and its prebuilt option | the launch returns at once while another build holds memory.
