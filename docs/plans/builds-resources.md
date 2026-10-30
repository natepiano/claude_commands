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
  - `scripts/lint/memory_admit.py` — the admission checker. `check_memory_reading(path)` is the tripwire check: with `BUILDLOG_LIVE_MEMORY_TRIPWIRE` set, a path that resolves to `/proc/meminfo` or `/proc/pressure/memory` appends a record and raises `LiveMemoryReading`. `read_meminfo(path)` returns `HostMemory` (`MemAvailable`, `MemTotal`, `SwapTotal`, `SwapFree`); its path comes from the shell (`check <meminfo> …`) or from `BUILDLOG_MEMINFO` (`read_queue`). `read_pressure(host)` reads `BUILDLOG_MEMORY_PRESSURE`, default `/proc/pressure/memory`, and holds every step when `full avg10` > 5, or when swap use is over 85% and `some avg10` is over 25% (Phase 12). The tripwire also protects live cgroup reads; fixtures use `fake_memory_readings`, which includes a private `BUILDLOG_BUILDS_CGROUP`. It swallows `OSError`/`ValueError`. Both call `check_memory_reading` before they open the file. Callers: `check` (in two places) and `read_queue` (the `/build:queue` view). `run_action` (actions include `tripwire <path>`), `main` (a `LiveMemoryReading` exits 2 with its text as the last stderr line).
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory`. With the tripwire set it resolves `$meminfo` and refuses a live path through `memory_admit.py tripwire <path>`. Then it reads `MemAvailable` itself from `${BUILDLOG_MEMINFO:-/proc/meminfo}` and starts `memory_admit.py check`. A checker that exits non-zero is refused with its last output line (`buildlog_memory_refuse`). On darwin with `BUILDLOG_MEMINFO` unset it grants at once and reads nothing.
  - `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — sources `memory_gate.sh` and calls `buildlog_wait_for_memory` with no arguments (a launch: no reservation).
  - `scripts/hooks/test_brp_launch_gate.py` — `setUp` :53 takes `BUILDLOG_MEMINFO`, `BUILDLOG_MEMORY_PRESSURE` and `BUILDLOG_LIVE_MEMORY_TRIPWIRE` from `fake_memory_readings(self.root)` (:74). `test_low_memory_wait_leaves_old_release_record_unchanged` :159 writes its low value to the returned `meminfo` file.
  - `scripts/production/merge_gate.py` — `test_module_environment` :514 gives each test module a private `BUILDLOG_DIR` and the build-tool tripwires (`cargo`, `rustc`, `rustdoc`, `rustup`, `cargo-nextest` first on `PATH`, each appends its argv to `<root>/build-tool-calls.jsonl` and exits 99). `finish_gate_job` reads that file, appends `a test started real <tool>: <args>` to the output, and sets status 1 and `GateJobResult.tripwire_tripped`. The gate never reruns a tripped module alone. Not in any unit's Owns cell: natedev showrunner's file.
  - `scripts/production/affected_modules.py` — affected-only module selection follows import edges (natedev c495a0b2): a change to a test helper selects every module that imports it. There is no `SHARED_TEST_HELPERS` list. natedev showrunner's file.
  - `scripts/production/test_merge_gate_suites_tripwires.py` — tests of the gate's build-tool tripwire: `test_module_tool_calls_fail_even_when_its_tests_pass` :22 (asserts `a test started real <tool>: <args>` and no `Rerun alone:`), `test_module_can_put_its_own_build_tool_stub_first` :35. Resource, admission, signal and `types` coverage live in `test_merge_gate_suites_resources.py`, `_admission.py`, `_signals.py` and `_types.py`. natedev 367da4f1 split `test_merge_gate.py` by subject into this module, `test_merge_gate_holds.py` and `test_merge_gate_ledger.py`. natedev showrunner's file.
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
**Depends on:** Phase 3, Phase 9 (the sweep change this phase extends), and the case D numbers from verify-unit's Phase 1 trial (the user approved verify-unit's copy plan on 2026-10-29; case D runs under verify's build hold after 00:00 EDT). The adopt rule picks this phase or drops it. The Change-Review uses `copies.py` (verify-copies Phase 2, verify-unit).

#### Work Order

**Goal:** a new temp clone of a Rust repository reuses the build output of earlier clones of the same repository, if a measured trial shows that this saves work without making builds wait. Approved by the showrunner 2026-10-29 (W2). Measurement: mktemp folders (91.2 compile-h in 7 days, 4.6 steps per clone, 61.6 cold-h on first builds) and style-sync folders (10.9 h) rebuild everything on their first build.

**Spec:**

1. **Trial first.** (Showrunner, 2026-10-29: if the user approves verify-unit's copies plan, this trial runs as case D of verify's Phase 1, in the same build-hold session, so its numbers compare with the seeding cases. The build work in items 3-5 waits on the pick that plan's rule makes from those numbers.) On hana, make two clones at one commit with `scratch.py new`. Build the first, then the second with a shared `CARGO_BUILD_BUILD_DIR` per repository. Record crates compiled and wall time for each. Then run two builds at once on the shared build folder and record the lock wait. Use `verify.sh build` for every build. `verify.sh` serializes calls that share a unit board, so start the two parallel builds from two separate session folders, and record each Cargo start and end time: the phase report shows that the two Cargo runs overlap. Report the build-folder lock wait (Cargo's `Blocking waiting for file lock` time) apart from the admission wait and the board wait.
2. **Adopt rule.** This phase does not pick on its own: it uses the case D verdict of verify-copies Phase 1. Adopt only when the second build compiles under half the crates of the first, the parallel lock wait is under 10% of build time, and clone 1's rebuild after clone 2 compiles at most 5 crates (verify-copies decision rule 1). Otherwise the phase records the numbers in the plan, changes no code, and stops.
3. **If adopted:** this phase builds the shared-target support only: identity, lifetime, locking and sweep attribution. It adds no copy-making command and no guard steer: `scripts/copies/copies.py` (verify-copies Phase 2, verify-unit) is the one entry point that makes copies, and its shared target mode calls this support.
   - `scripts/lib/scratch.py`: one function that returns the shared build folder for a repository, for `copies.py` to set as `CARGO_BUILD_BUILD_DIR`. Name it in the as-built.
   - `scripts/style/style_sync.py` already shares one `style-sync-target` per repository (:332) and serializes checkout creation through cleanup. Change only what the shared-folder lifetime needs, and keep its lock.
   - **Shared-target identity.** One mapping from repository to its shared folder. The folder has one use stamp for all its clones, a lifetime that does not end with one scratch task, and Cargo's build-folder lock. `rebuild_causes()` today finds a workspace by the target's parent path; give it the mapping, so a removal of the shared folder names the repository.
   - The disk sweeps treat the shared build folder as one target with its own last use. `lock_trees()` (sweep.py :491) returns `TargetLocksAcquired | TargetLockBusy`, not `tuple[list[int], str | None]`.
   - Deletions of the shared folder go through `reclaim()` and count only `ConfirmedDeletion` bytes (Phase 9). Artifacts that Cargo hard-links into a clone's own target folder free nothing while that clone is present; a test in `test_sweep.py` covers a shared folder whose artifacts are still linked from a clone.
4. **Tests** for each changed module, in `scripts/lib/test_scratch.py`, `scripts/style/test_style_sync.py` and `scripts/lint/test_sweep.py`: two clones on one target, a busy shared target, and rebuild attribution after its removal. The existing busy-skip and exhaustion tests stay as gates.
5. **Docs.** The scratch as-built names the shared build folder and the function `copies.py` calls.

**Files:**
- `scripts/lib/scratch.py` — item 3.
- `scripts/lib/test_scratch.py` — item 4.
- `scripts/style/style_sync.py` — item 3 (owner natedev style-unit; trial-merge against `origin/natedev`).
- `scripts/lint/sweep.py` — item 3, the shared folder as one target.
- `scripts/lint/test_sweep.py`, `scripts/style/test_style_sync.py` — item 4.

**Workers:** `1 writer + 1 tester`
- `impl` — the trial (item 1), the adopt decision record, items 3 and 5.
- `test` — item 4, after the adopt decision: `test_scratch.py`, `test_style_sync.py`, `test_sweep.py`.

**Acceptance gate:**
- The trial numbers (crates compiled, wall time, lock wait) are in the phase report with the adopt decision.
- If adopted: the tests of each changed module pass; `basedpyright` over each changed file reports `0 errors, 0 warnings, 0 notes`. Post-merge check, 24 h after the merge reaches main (as in Phase 12's ruling): the first-build cold-h of mktemp and scratch.py folders, next to the 7-day figures. `rebuilds` does not separate first builds (a workspace's first step has cause `none`), so compute it from the build-log records: the first recorded step of each new workspace folder. The removal records in `rebuilds` name the shared folder's repository. The phase report records when the check is due; the result goes in a follow-up notice.
- The notice has an `also touches scripts/style/style_sync.py (owner natedev style-unit)` line, tested against `origin/natedev`.
- Change-Review line: a new temp clone of hana builds only the crates that changed | in any Claude session on natedev, make two copies of hana at one commit with `copies.py make` in its shared target mode (its exact form is in the frozen-copies as-built) and build each with `verify.sh build` | the second clone's build log step compiles under half the crates of the first.

### Phase 5 — Clone builds turn incremental mode off, and mend uses the compile cache  · status: todo
**Depends on:** Phase 3; Phase 12 (`invoke.sh`). Runs before Phase 8, which depends on this phase's `verify.sh` and `invoke.sh` changes.

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
- Owner rules come from `scripts/build_targets/working_set_owners.json`, generated from the production docs' Owns cells (73 rules, 13 units at checkpoint; 69 rules, 12 units since the retired enh-showrunner rows dropped out). `load_owners` validates the file; a bad file exits 1 and names the file. The rules are not in the script, because the push-text gate read an owner tuple that named `pushover.py` as a phone send.
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
- `scripts/direct/verify.sh`, `scripts/lint/invoke.sh` — item 3 (owner verify-unit). The tester owns routing coverage in `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_nix_shell.py`, `scripts/lint/test_invoke_scope.py`, `scripts/message/test_top_level.py` and `scripts/production/test_retire_unit.py`, plus the admission, hold and queue tests named above. Cover the installed paths and retained overrides under a fake HOME and private coordination state.
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

**Constraints from prior phases:** Phase 11 changes estimates (`need`) only. It keeps the running charge as Phase 12 built it: current memory, the ramp and unknown-peak promises, the observed peak with its `.wave` decay after 1500 s, the missing-`.wave` fallback to the `.max` mtime, the recovery start spacing, and the estimate-expiry checks after the lock.

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

#### As-built

- `build_charge` charges a running step the largest of its ramp promise (inside `RAMP_SECONDS`, 120 s), its current memory (`tree_memory`), its observed peak, and its unknown-peak promise. `reservation_memory` reads the peak from `<reservation>.max`; an unreadable `.max` charges the step its `need`. In the blocker lookup a release frees only the step's current resident bytes.
- The peak decays: the `invoke.sh` sampler (`buildlog_sample_anon`) publishes `.max` first, then `<reservation>.wave` = `"<committed peak> <epoch seconds>"` on each new peak and at most once per 60 s while samples stay at or above half the peak. `reservation_memory` drops the peak when the `.wave` peak equals `.max` and its time is `PEAK_DECAY_SECONDS` (1500 s) or more old. With no `.wave` file (a sampler from before the decay), the `.max` mtime is the last near-peak time under the same rule. An empty, malformed, negative, time-only, mismatched or unreadable `.wave` keeps the peak. Release and stale-reservation cleanup remove `.wave` with `.max`.
- The anon of `builds.slice/sccache.service` is added to the build charge once. It is not a reservation and never a blocker; a missing or unreadable file adds nothing.
- `read_meminfo` returns `HostMemory` with `SwapTotal`/`SwapFree`; `read_pressure(host)` holds when `full avg10` > 5, or when swap use is over 85% and `some avg10` is over 25%. The hold text names the reading that caused it. After a pressure hold ends, admission starts at most one step every 30 s; a spacing hold gives `blocker` `{}`.
- `working_set.py` prints each owner by its short unit name (`[resources]`, `[verify]`); `working_set_owners.json` keeps unit ids and holds 69 rules over 12 units, with no retired unit.

**Files:**
- `scripts/lint/memory_admit.py` — the peak, decay and sccache charge, swap and "some" holds, start spacing.
- `scripts/lint/invoke.sh` — `buildlog_sample_anon` writes `.max` and `.wave` atomically (owner verify-unit).
- `scripts/lint/test_memory_admit.py`, `scripts/lint/memory_test_support.py`, `scripts/lint/test_invoke_scope.py`, `scripts/build_queue/test_build_queue.py` — faked readings for every new path.
- `scripts/build_targets/working_set.py`, `scripts/build_targets/test_working_set.py`, `scripts/build_targets/working_set_owners.json` — short owner names and the regenerated snapshot.
- `docs/as-built/build-memory-admission.md` — the charge, decay, holds and spacing.

**Binds later work:** a change to estimates (`need`) leaves the running charge unchanged: current memory, ramp and unknown-peak promises, observed peak with its `.wave` decay and `.max` mtime fallback, and recovery spacing. Any path that deletes a reservation removes `.max` and `.wave` with it.

**Gotchas:** a step keeps the sampler it started with for its whole run, so a change to the sidecar format needs a fallback for steps already running. `PEAK_DECAY_SECONDS` comes from the hana nextest wave gaps (longest 954 s between runs of samples at or above half the peak). Admission tests use a fixed fake clock (2026-10-06), so every `.max` they write sets its mtime with `os.utime`.

**Ruled out:** `RAMP_SECONDS = 600` (kept at 120 s while herdr fixed the swapped herdr server); a time-only `.wave` record (a failed `.wave` write after a new peak would let the old time decay the new peak).

### Phase 13 — A session can launch an app that is already built without waiting at the BRP launch gate  · status: dropped
**Dropped:** showrunner 2026-10-29. There is no bevy_brp prebuilt option and no hook bypass, so every launch keeps waiting, as Phase 6 ruled. The user doubted its value. report-unit first restores the brp-launch rows in the build log (none since 2026-10-15); after that, launch waits get measured, and a new phase starts only if they show a cost.

### Phase 14 — Memory admission, the sweeps and the merge gate finish when they run again after a stop  · status: done

#### As-built

- `merge_gate.py` `promote` records the commit it promotes, with its tested merge, before the local fast-forward of main. A rerun pushes local main only when local main is that recorded commit and origin/main is its ancestor; any other difference refuses.
- The merge gate records each checkpoint merge (checkpoint commit, merge commit, base) before the push, in a record apart from the pass ledger. `check_local_doc_commits` accepts a local commit only when it is that recorded merge, and the rerun pushes it.
- `memory_admit.py` applies one liveness rule, `reservation_lifetime` (`PopulatedStepScope | LiveOwnerProcess | EndedStep`), in `live_reservations`, `step_memory`, `check` cleanup and `queue_rows`. A scoped step's reservation stays live while its step cgroup holds a process: it is charged that cgroup's memory and keeps `.max` and `.wave`. An unscoped step keeps the pid-and-start-time rule. `set_width` writes through a temporary file and rename.
- `sweep.py` retention writes the kept rows to a temporary file and renames it over the removal log. Append, retention and readers take a separate stable lock file before they open the log.
- `tmp_sweep.py` writes the plan to a temporary file and links it into place; it never replaces an existing plan. `--plan` over an incomplete leftover names the file and how to clear it.
- `folder_audit.py` `remove` records the worktree sources next to the removal log before the delete, prunes them, then removes the record. A rerun prunes every source still in a record.

**Files:**
- `scripts/production/merge_gate.py` — promote and checkpoint-push recovery records.
- `scripts/lint/memory_admit.py` — step-cgroup liveness, atomic `set_width`.
- `scripts/lint/sweep.py` — removal-log lock file, atomic retention.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/folder_audit.py` — atomic plan link and leftover message; worktree-prune record.
- `docs/as-built/build-memory-admission.md`, `docs/as-built/build-targets.md`, `docs/as-built/merge-gate-and-test-speed.md` — the rerun rule for each command.

**Binds later work:** a new reservation sidecar must be removed on every delete path, the cgroup-liveness paths included. Any writer or reader of the `sweep.py` removal log takes its separate stable lock file first.

**Gotchas:** a reused pid can lend its `/proc` identity to an orphaned scope row. Origin can be rewound below the recorded base, so a checkpoint retry must refuse it. A scope can vanish between the `memory.current` read and the swap read. The push-recovery records are never pruned.

**Ruled out:** tree equality as proof for a promote rerun (it does not show the commit is the gate's own).

### Phase 15 — After its compile, a nextest step is charged its test phase's measured peak  · status: todo
**Depends on:** Phase 14 (same file, `memory_admit.py`; its row 3 charges a step by the step cgroup that the `invoke.sh` sampler reads, and adds cleanup paths that must remove this phase's new sidecar). Merge `origin/builds` first. report-unit's build-why change (report Phase 3: `explain_queue`, `ChargeBasis`, waiter `need_source`) may land first; if it is on `origin/builds`, merge it before dispatch. **Parked** (builds showrunner 2026-10-30): both replay arms (history max, max + 10%) failed the landing rule, so the same-width charge stays as today. report-unit records `test_phase_peak_bytes` (the per-step test-phase peak) in a new phase; this phase resumes only after that phase merges on `origin/builds` and a replay on its data passes. Global order: 21 → 16 → 20 → 4 → 11 → 17 → 18 → 19 → 5 → 8; this phase merges on top when it resumes.

**Approved:** showrunner 2026-10-29 (ETA 04:30 EDT 2026-10-30).

#### Work Order

**Goal:** a nextest step that has finished its compile is charged what its test phase uses, measured with all its parallel test processes, not its compile peak. Waiters behind it start sooner, and a test phase does not go above its charge more often than today.

**Measured (code, at `a650d945`; line numbers move with Phase 14, so find each by its function):**
- `build_charge` (`memory_admit.py` :478) charges a running step `max(ramp_promise, bytes, observed_peak, unknown_peak_promise)`. `reservation_memory` (:778) keeps the `.max` peak until the `.wave` time is `PEAK_DECAY_SECONDS` (:78, 1500 s) old (:805). So a hana nextest step keeps its compile peak for up to 25 min into its test phase.
- `buildlog_sample_anon` (`invoke.sh` :172) reads the scope's `anon` every second and writes only `.max` (the peak) and `.wave` (`"<peak> <epoch>"`). It overwrites both. `release` (:1596) writes one `anon_peaks.jsonl` row per step with the whole-step peak (:1609-1615). No record holds a per-step memory series or a compile-end time.
- The build log has the compile time of each nextest step: `steps.finished_s` is cargo's own `Finished … in` time (`index.py` STEP_COLUMNS :56), and `started_at` is set after the memory wait (`run_once`, `invoke.sh` :290-293). Index read 2026-10-29 22:59 EDT: `finished_s` is NULL on 109 of 1067 hana nextest rows since 2026-10-22 (all hosts). Machine samples (`samples.builds_anon_bytes`, every 60 s) cover the whole `builds.slice`, not one step.
- Test width: `run_nextest` (`invoke.sh` :496) runs at the logical core count (`LINT_TEST_WIDTH=logical`, :390; 32 on natedev). `nextest_share_claim` (:474) asks `nextest_budget.py claim` (:51) for `shared_width(full, runs)` = `min(full, max(FLOOR, full // runs))`, `FLOOR = 4` (:28). A narrower share goes into the reservation through `set_width` (`memory_admit.py` :1626). `check` writes `test_threads(argv)` (:499) at admission.

**Spec:**

1. **Signal and replay first.**
   - Pick the compile-end mark: the time the first test binary starts in the step's scope. Candidates: (a) the sampler reads the scope's `cgroup.procs` and finds a process whose executable is in the cargo target folder's `deps/` (nextest's list pass runs each test binary, so this mark falls after the last compile); (b) nextest's `Starting N tests across M binaries` line in `run_once`'s tee log (:312-317), which a terminal run (`-t 1`, :300) does not have. Choose the one that works for terminal and non-terminal runs, and measure its added sampler cost per second. Record the choice and the cost in the phase report.
   - Replay on the last 14 days of the natedev build log (`~/.local/state/buildlog/index.sqlite`, tables `steps`, `samples`; `admission/anon_peaks.jsonl`). A past test-phase peak exists only as a proxy: for a nextest step that ran alone in `builds.slice`, the largest `builds_anon_bytes` sample in `[started_at + finished_s, ended_at]`. Count such steps first; leave out rows with no `finished_s`. Key each by its family (repository + test filter: `test_scope`, `package`, `filters`) and record its test width apart from the key.
   - For each nextest step, compute the charge after compile under Spec 3 from the history before it, with and without the `fitted` form (Spec 4). Compare today's charge and the new charge on the same eligible steps (nextest steps that ran alone and have `finished_s`). Report per repository: how often the test phase went above today's charge and above its new charge, by how much (p50 and max, GiB), and the wait minutes saved by waiters that the lower charge admits sooner. State the queue model, and check it first: the replay of today's rule must give total wait minutes close to the recorded `steps.mem_wait_s` sum; print both.
   - Save the replay script in the run folder (`phases/15/`), not in the repository; Phases 16 and 20 reuse it.
2. **Record the test phase.** The sampler writes a new sidecar beside `.max`, `<reservation>.tests` = `"<first test start epoch> <test-phase anon peak>"`, atomically as `.max` is written (temporary file, then `mv -f`). `release` adds `test_phase_started_at` and `test_phase_peak_bytes` to the `anon_peaks.jsonl` row when the sidecar exists, and the stored `EstimatedTestPhaseCharge` (bytes, history cutoff, width form, recorded widths) when the reservation has one, so the 24-hour check compares each test phase with the charge it really had. Every path that deletes a reservation removes `.tests` with `.max` and `.wave`: `release`, `live_reservations` (:986-988), the `check` cleanup (:1233-1236), and Phase 14's cgroup-liveness paths.
3. **The charge after compile.** `check` computes an `EstimatedTestPhaseCharge` for a nextest step at admission, outside the admission lock, as it computes `expected_peak`. It holds the bytes, the history cutoff, the recorded widths it used and the width form (Spec 4), or `NoTestPhaseHistory`. It is stored in the reservation and lives until the reservation is released; a later width change (`set_width`) computes it again outside the lock. When a reservation has a `.tests` mark, `reservation_memory` drops the compile peak and charges `max(current memory, the stored test-phase charge)`. The test-phase charge is the p90 of earlier test-phase peaks of the same family (repository + test filter), from `anon_peaks.jsonl` rows that carry `test_phase_peak_bytes`, over `HISTORY_DAYS`; Spec 4 picks which recorded widths supply it. It is the whole test phase with all its parallel test processes, never one test's allowance. The single-test p90 of the repository (filtered, no-compile runs) is a floor only. With no test-phase history for the key, the step keeps its compile charge until it ends: no decay. The 1500 s decay stays only for steps with no mark: steps other than nextest, and agent, alias or unknown cargo calls (`steps.caller`).
4. **Width rule.** The run's width is the reservation's `test_threads` (the share after `set_width`); it is not part of the family key. The form is a closed set, picked in this order: `same_width` — the family has rows at the run's width: their p90; `fitted` — the family has rows at two or more other widths: a fixed part plus a per-thread part, fitted from them; `wider_unscaled` — rows only at wider widths: the p90 of the nearest wider width, no scale-down; `scaled_up` — rows only at narrower widths: the p90 of the widest narrower width × width / recorded width. The replay reports the charge with and without `fitted`, so the showrunner sees what the fit changes.
5. **Shared names with report-unit (build-why).** Before dispatch, confirm these names with report-unit and record the reply in the phase report: report adds `explain_queue() -> QueueExplanation` to `memory_admit.py`; `check` writes waiter fields `need_source`, `need_runs`, `test_threads`; `read_queue` and `explain_queue` share one internal pass; `ChargeBasis` and the need source are closed unions. This phase adds the basis variant `TestPhasePeak(bytes, runs, width, recorded_widths, form)` with `form` one of `same_width | fitted | wider_unscaled | scaled_up`, and the need source `"test_phase_peak"`. Whoever lands second adds the variant. If build-why is on `origin/builds`, add the variant and its words in `build_why.py`; if not, add only the typed value that `reservation_memory` returns, and report adds the variant.
6. **Tests** in `scripts/lint/test_memory_admit.py`: the compile charge holds until the mark; after the mark the charge is the stored test-phase charge with history and the compile peak without it (no decay); a step with no mark still decays at 1500 s; each width form: `same_width`, `fitted` with two other widths, `wider_unscaled` with no scale-down, `scaled_up`; the estimate is computed outside the lock and stored in the reservation; the floor; `.tests` is removed on every delete path; the `release` row carries the two new fields. Sampler tests in `scripts/lint/test_invoke_scope.py` (owner verify-unit): the mark appears when a fake test binary starts in a fake scope, and a sampler from before this change (no `.tests`) keeps today's charge. Every test fakes both memory readings (`fake_memory_readings`).
7. **Docs.** `docs/as-built/build-memory-admission.md`: the mark, the sidecar, the charge after compile, the width rule, the replay table. Update Phase 11's "Constraints from prior phases" line in this plan: the running charge is as Phases 12 and 15 built it.

**Constraints from prior phases:**
- Phase 12: a step keeps the sampler it started with, so a step already running with no `.tests` file keeps today's charge and decay. Any path that deletes a reservation removes every sidecar.
- Phase 12's "a change to estimates leaves the running charge unchanged" stays true; this phase changes the running charge on purpose, only for steps with a mark.
- Phase 10: the blocker lookup reuses `decide` and `reservation_memory`, so it follows the new charge with no copy of the rule.
- Phases 1–2: tests never read live `/proc/meminfo`, `/proc/pressure/memory` or the cgroup tree; the merge gate trips a module that does.

**Files:**
- `scripts/lint/invoke.sh` — Spec 2, the sampler mark (owner verify-unit; trial-merge against `origin/natedev-verify` and `origin/natedev`).
- `scripts/lint/memory_admit.py` — Specs 2–5.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/memory_test_support.py` — Spec 6 (the support file only for a fake test binary or scope).
- `scripts/lint/test_invoke_scope.py` — Spec 6, sampler tests (owner verify-unit).
- `scripts/build_queue/build_why.py` — Spec 5, only if build-why landed first (owner report-unit; trial-merge against `origin/builds-report`).
- `docs/as-built/build-memory-admission.md`, `docs/plans/builds-resources.md` — Spec 7.

**Workers:** `1 writer`

**Acceptance gate:**
- The changed test modules pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; `bash -n scripts/lint/invoke.sh`; the merge gate is green.
- The replay table (with and without `fitted`) is in the phase report and the checkpoint notice: per repository, on one eligible cohort, test phases above today's charge and above the new charge (count, p50 and max excess) and wait minutes saved, next to the model check against recorded `mem_wait_s`. The change lands only when the share above the new charge is not higher than the share above today's charge, for every repository.
- Post-merge check, 24 h after the merge reaches main: from `anon_peaks.jsonl` rows with `test_phase_peak_bytes` and the stored charge, how often a test phase went above the charge it had; from `steps.mem_wait_s`, wait minutes against the 24 h before the merge; earlyoom kills and `calls.mem_kills` in both windows. The phase report records when the check is due; the result goes in a follow-up notice.
- The notice has `also touches` lines for `invoke.sh` and `test_invoke_scope.py` (verify-unit, against `origin/natedev-verify` and `origin/natedev`) and, if changed, `build_why.py` (report-unit, against `origin/builds-report`). It names the report-unit reply on the shared names.
- Change-Review line: a nextest step's charge falls to its test-phase peak once its tests start | on natedev, start `~/.claude/scripts/direct/verify.sh test hana_diegetic` in `~/rust/hana`; after its compile, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` (before build-why lands: `cat ~/.local/state/buildlog/admission/*.tests`) | the step's charge reads as the test-phase peak with its width form, below its compile peak.

### Phase 16 — Python test gates take a place in the shared admission queue  · status: todo
**Depends on:** Phase 21 (same file, `merge_gate.py`: Phase 21's checkout lock lands first). Runs before Phase 20. Merge `origin/builds` first. Global order: 21 → **16** → 20 → 4 → 11 → 17 → 18 → 19 → 5 → 8.

**Approved:** showrunner 2026-10-29 (ETA 06:30 EDT 2026-10-30).

#### Work Order

**Goal:** merge-gate test runs take a place in the shared admission queue for memory and for CPU, as cargo steps do, so several gates and the cargo builds no longer start together into more than the machine holds.

**Measured (code, at `a650d945`):**
- `merge_gate.py` `run_suites` (:721) runs its modules in a private `/dev/shm/merge-gate-*` folder (:741, :793). One gate caps itself only: at most `DEFAULT_MAX_RUNNING_JOBS` = `cpu_count // 2` modules at once (:71; 16 on natedev's 32 logical cores), and `SampledRSSAdmission` (:175) with `DEFAULT_MEMORY_CAP_MIB` 4096 (:70). Nothing caps several gates together, and no gate is in the memory queue or takes a `/dev/steve` slot.
- Each module runs `python -m unittest discover` (:833) behind `suite_scope_prefix` (:478): `systemd-run --user --scope … --slice=builds.slice`, so its memory is in `builds.slice` and in `samples.builds_anon_bytes`, but no reservation charges it.
- The CPU pool for session builds is the `/dev/steve` jobserver (31 slots, `docs/as-built/build-memory-admission.md` machine table); `invoke.sh` :44 exports `CARGO_MAKEFLAGS=--jobserver-auth=fifo:/dev/steve`. Only cargo's compiles take its slots.
- `verify.sh test` runs `cargo nextest` through `run_once` and its own `buildlog_wait_for_memory` (`verify.sh` :2613), so it has a memory place today. Its test processes take no `/dev/steve` slot; `nextest_budget.py` shares test threads only among nextest runs.
- The gate keeps no record of when a run started or how many ran at once. `~/.local/state/merge-gate/passed-trees.jsonl` records only passes (`at`); `module_history.json` and `module_seconds.json` hold per-module times. The "often six at once" figure is the showrunner's observation.

**Spec:**

1. **Measure first.** From `/proc` while gates run, and from the last 7 days of `samples` (`builds_anon_bytes`, `stall_full_us`, `cpu_stall_some_us`) and `steps`: how many gates ran at once, each gate's peak memory (the gate prints `Largest module peak` at :927; its whole tree is what matters), and the wait minutes and pressure holds of cargo steps while gates ran against while none ran. The run with no queue place is `merge_gate.py --tests-only`, the `~/.claude` merge gate: its parallel suites (about 10 min) and its basedpyright run sit outside the memory and CPU queue, and on 2026-10-29 it pushed natedev load to 67 (showrunner, 23:3x EDT). That run gets a queue place with a CPU charge. `verify.sh test` already waits in the memory queue; leave it as it is. List any other Python run found outside the queue (a unit's own pytest through `verify.sh lint`, say) in the phase report and give it the same rule.
2. **Memory.** A gate takes one reservation in the admission queue before it starts its first module, through `memory_admit.check` in the installed `~/.claude` copy (the gate already runs from `~/.claude` main). Its `need` is the gate's measured peak from its own history; a new record in `gate_state_dir()` keeps each run's whole-tree peak. The gate releases the reservation on every exit path, and its record goes to `anon_peaks.jsonl` under step `merge-gate`. The gate waits in the queue as a cargo step waits, and `/build:queue` and the blocker lookup show it.
3. **CPU.** Each gate module, `types` included, takes one `/dev/steve` slot before it starts and gives it back when it ends (read one byte from the fifo, write it back), so modules and rustc share one pool. The token read never blocks the runner: the launch loop (`run_suites`, :818) tries a non-blocking read and, with no token, goes on reaping finished jobs, sampling memory, and handling holds and cancellation, then tries again. A token is owned by the job that took it and is written back on every exit path (pass, failure, kill, stop, an exception in the runner). With no `/dev/steve` (the Mac, a test), the gate runs as today. The module count limit stays as a per-gate cap.
4. **Fail closed.** An admission error stops the gate with the checker's last line, as `memory_gate.sh` refuses a step (Invariants: only an explicit admission answer starts a step).
5. **Tests** in the gate's suite modules by lane: `test_merge_gate_suites_admission.py` (a gate waits while the fake queue is full and starts when it frees; the reservation is released after a pass, a failure, a module kill and a stop), `test_merge_gate_suites_resources.py` (a slot is taken and returned per module with a fake fifo; two jobs share a one-slot fake pool and both finish, the second starting after the first returns its token; no `/dev/steve` runs as today), `test_merge_gate_suites_signals.py` (a stop while a job waits for a token returns every owned token) and `test_merge_gate_suites_types.py` (`types` takes a slot). `scripts/lint/test_memory_admit.py` only if `memory_admit.py` changes. Fake both memory readings; never touch the real `/dev/steve`.
6. **Docs.** `docs/as-built/merge-gate-and-test-speed.md` (the gate's place and slots) and `docs/as-built/build-memory-admission.md` (the `merge-gate` step).

**Constraints from prior phases:**
- Phase 2: every gate module runs under the live-memory tripwire, and the `types` suite has none. The gate's own admission call runs in the gate process, outside every module's environment.
- Phase 14 rows 1–2 change `promote` and the checkpoint push in the same file; build on them.
- Phase 8 (later) makes the admission writers refuse a checkout copy; the gate calls the installed copy, so it passes.
- `BuildHoldWait` (:224) stays first: a gate waits out a build hold before it asks for memory.

**Files:**
- `scripts/production/merge_gate.py` — Specs 2–4 (owner natedev showrunner; also-touches at checkpoint, trial-merge against `origin/natedev`).
- `scripts/production/test_merge_gate_suites_admission.py`, `test_merge_gate_suites_resources.py`, `test_merge_gate_suites_signals.py`, `test_merge_gate_suites_types.py` — Spec 5 (owner natedev showrunner).
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py` — only if the gate needs a new entry point.
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — Spec 6.

**Workers:** `1 writer + 1 tester`
- `impl` — `merge_gate.py`, any admission entry point in `memory_admit.py`, and the two as-built docs.
- `test` — the four suite modules of Spec 5, and `test_memory_admit.py` if `memory_admit.py` changes.

**Acceptance gate:**
- The changed test modules pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The Spec 1 numbers are in the phase report: gates at once, gate peak memory, and cargo-step wait minutes and pressure holds with and without gates.
- Post-merge check, 24 h after the merge reaches main: cargo-step wait minutes, gate wait minutes, `full avg10` holds and earlyoom kills against the 24 h before; how often a gate's tree went above its reservation's `need`. The phase report records when the check is due.
- The notice has `also touches scripts/production/merge_gate.py and the changed test_merge_gate_suites_*.py modules (owner natedev showrunner)`, tested against `origin/natedev`.
- Change-Review line: a merge gate waits in the memory queue and takes build slots | in a terminal on natedev, `cd ~/.claude && env HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout ~/.claude --tests-only`, then type `/build:queue` in a Claude session | the gate shows as a `merge-gate` row, running or waiting, with its need.

### Phase 17 — The nextest promise splits by compile size  · status: todo
**Depends on:** Phase 11 (same `expected_peak`; Phase 11 splits compile runs from no-compile runs). Merge `origin/builds` first. Global order: 21 → 16 → 20 → 4 → 11 → **17** → 18 → 19 → 5 → 8.

**Approved:** showrunner 2026-10-29 (no ETA given).

#### Work Order

**Goal:** a nextest step's promise comes from earlier runs that compiled about as much as this run will, so a one-test run on a warm tree is not promised a full compile, and a full compile is not promised a one-test run.

**Measured:**
- The showrunner's figures: the p90 over the "nextest" class mixes 0.5 GiB one-test runs with 29 GiB full compiles (not checked against the log in this Work Order).
- `steps.crates_compiled` counts `Compiling/Checking/Documenting` lines (`index.py` :56); anon rows join to steps by `call_id` and step name (Phase 11 Spec 4; `width_trial.py` `anon_by_call` :171 and `anon_by_step` :197 pair them). The size of a step's compile is known only after it runs.

**Spec:**

1. **Measure first.** Over 14 days of natedev nextest steps, per repository: the distribution of `crates_compiled` and the anon peak by `crates_compiled`. Choose size bands where the peak changes (the edges come from the data; show the table). Then measure predictors of the band at admission time, using only data present before the step: (a) the last recorded `crates_compiled` of the same worktree and step; (b) whether the `tree_key` changed since the last step in that worktree; (c) a target folder with no earlier step (first build). Report for each predictor how often it picks the right band, and choose the best. Replay the promise per band against today's (after Phase 11): per repository, promises under half and over 3x the measured peak, wait minutes saved, and how often a step went above its promise.
2. **Adopt rule.** Adopt only when the replay shows fewer promises under half the peak in total and none rises for any repository. Otherwise record the table and stop.
3. **If adopted:** `expected_peak` and `filtered_peak` (`memory_admit.py`) take the predicted band and use only history rows of that band, in each tier. The need's basis names the band, so the wait line and build-why show it. Phase 11's compile/no-compile split stays the first split; bands split compile runs only. With fewer than 5 rows in the band, use the next larger band, never a smaller one. Comment each band edge with the measurement that set it.
4. **Tests** in `scripts/lint/test_memory_admit.py`: each predictor input; band selection; a thin band takes the next larger band; a no-compile run keeps Phase 11's rule; fake both memory readings.
5. **Docs.** `docs/as-built/build-memory-admission.md`: the bands, the predictor and the replay table.

**Constraints from prior phases:**
- Phase 11: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; the compile-run promise wins over `min(filtered, full)`; fresh runs keep `fresh_test_peak`.
- Phase 15: the band sets the compile charge only; the test-phase charge after the mark is unchanged.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 3.
- `scripts/lint/test_memory_admit.py` — Spec 4.
- `docs/as-built/build-memory-admission.md` — Specs 1 and 5.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The band table, predictor accuracy and before/after replay are in the checkpoint notice, with wait minutes saved and steps above their promise.
- Post-merge check, 24 h after the merge reaches main: per band, steps above their promise and wait minutes against the 24 h before.
- Change-Review line: a small-compile test run on a warm tree gets a small promise | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic --filter <one test>` once, then add a comment line to one source file of that crate (a small compile that is not zero; an unchanged tree takes the freshness path instead), run it again and read that run's memory wait line | its need is near the small band's p90 that the replay gives, and its basis names the compile band it picked.

### Phase 18 — History splits on the dylib feature and each step's commit, not on a time  · status: todo
**Depends on:** Phase 17 (same `expected_peak`). Merge `origin/builds` first. Global order: 21 → 16 → 20 → 4 → 11 → 17 → **18** → 19 → 5 → 8.

**Approved:** showrunner 2026-10-29 (no ETA given).

#### Work Order

**Goal:** a promise uses only history built the same way as the step it admits. The 14-day window crosses two hana build changes that cut memory, and a time cutoff mixes steps from both sides.

**Measured:**
- Bevy dylib: hana `11c5edbd6f` (2026-10-29 05:33 EDT); `9b60fcd76e` is only its merge. Every dylib step's argv carries `--features` with `hana/dynamic_linking` in a comma list, for example `"--features", "hana/dynamic_linking,hana/hotpatching"`. Index read 2026-10-29 22:59 EDT: 68 step rows carry it; 42 of them are `verify-mac` rows with no repo and no sha. The showrunner's count: a time or sha-ancestry cutoff misses 13 of 17 dylib runs.
- opt-level 1: hana `ad25f045ff` (2026-10-28 23:54 EDT), merged by `2bbd4c1fba` (2026-10-29 00:59 EDT), changes only `Cargo.toml` `[profile.dev.package.<crate>]` for dev and test builds. Argv does not show it, and the build log has no profile or `Cargo.toml` field. Steps record `sha` (short commit) and `tree_key`, so a step's side is whether `ad25f045ff` is an ancestor of its own `sha`.
- Anon rows carry no sha; they join to `steps` by `call_id` and step name (Phase 11 Spec 4).

**Spec:**

1. **Measure first.** Over 14 days of natedev hana steps: the count on each side of each change, by step; the anon peak p90 on each side; how many rows have no sha or no `call_id` match (they count on neither side). Confirm that the opt-level side from sha ancestry separates the peaks; if it does not, record that and leave the opt-level split out. Replay today's promise against the split promise: per step, promises under half and over 3x the measured peak, wait minutes saved, steps above their promise.
2. **One list of build changes.** A new `config/build_changes.json` holds each change: repository identity, kind (`argv_feature` with the feature name, or `commit` with the full sha), and a note. The two entries above are its first rows.
3. **The split.** `expected_peak` and `filtered_peak` keep only history rows on the same side of every listed change as the admitted step. `argv_feature`: the step's own argv and the row's argv (parse `--features a,b` and `--features=a,b`). `commit`: whether the change is an ancestor of the step's `HEAD` and of the row's `sha`. Run git once per distinct sha and keep the answers in `admission/build_marks.json`; measure the added check time and report it. A release build (`--release`) ignores the opt-level entry (dev and test only).
4. **Fallback.** With fewer than 5 rows on the step's side, the tiers below apply as today, still on that side. Rows of the other side never fill a tier.
5. **Tests** in `scripts/lint/test_memory_admit.py`: the comma feature list; `--features=`; a sha after and before a commit change; a row with no sha counts on neither side; the cache file; fake both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the list, the split rule and the replay table.

**Constraints from prior phases:**
- Phase 17's bands and Phase 11's compile split apply inside each side.
- The estimate runs outside the admission lock (`check`), so git calls never run under the lock.

**Files:**
- `config/build_changes.json` — new — Spec 2.
- `scripts/lint/memory_admit.py` — Spec 3–4.
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The side counts, the peak per side, the added check time and the before/after replay are in the checkpoint notice.
- Post-merge check, 24 h after the merge reaches main: hana steps above their promise and wait minutes against the 24 h before.
- Change-Review line: a dylib hana test is promised only from dylib history | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic` and read its first memory wait line | the need and its source match the dylib-side p90 that the replay gives.

### Phase 19 — Measure where admission estimates come from, then make the 0.65 fallback rare  · status: todo
**Depends on:** Phase 18 (same `expected_peak`). Runs before Phase 5. Merge `origin/builds` first. Global order: 21 → 16 → 20 → 4 → 11 → 17 → 18 → **19** → 5 → 8.

**Approved:** showrunner 2026-10-29, rescoped (no ETA given).

#### Work Order

**Goal:** most admissions use measured anon peaks. The build-log peak times `ANON_SHARE` is a fallback that few admissions take.

**Measured (code, at `a650d945`):**
- `ANON_SHARE = 0.65` (`memory_admit.py` :71) scales a build-log `peak_mem_bytes` (page cache included) to process memory: in `expected_peak` (:597) when a repository has fewer than 5 measured anon peaks, and in `filtered_peak` (:703, :708) for the index tiers. `ExpectedPeak.source` (:141) is `measured`, `buildlog`, `pooled` or `fallback`.
- No record keeps the source of an admitted step's estimate: reservations have no source field, and waiter rows (report Phase 3 adds `need_source`) are deleted at admission.

**Spec:**

1. **Measure first.** For every admitted natedev step in the last 7 days (steps that pass the memory gate: `width_trial.py` `_uses_memory_gate` :355), replay `expected_peak` with the history before the step and record its tier. Report the share of admissions per tier, by step kind (nextest full, nextest filtered, clippy, check, build, doc, other). Report the share that took a `× ANON_SHARE` tier, and for those steps, the estimate against the measured anon peak.
2. **Stop rule.** If the `× ANON_SHARE` share is already small, the phase is only this measurement: the table goes in the as-built doc and no code changes. The showrunner sets "small" from the table.
3. **Otherwise, widen anon matching.** When the exact filter set has no anon history, use the same repository + step anon rows (same scope and Phase 11/17/18 splits) before any `× ANON_SHARE` tier. Replay again: the `× ANON_SHARE` share, promises under half and over 3x the measured peak, wait minutes, steps above their promise.
4. **Drop 0.65 once rare.** When the replay shows the `× ANON_SHARE` share below the showrunner's bound, remove the `ANON_SHARE` tiers; a step with no anon history goes to the pooled tier, then the fallback. Keep `ANON_SHARE` if the replay shows more promises under half the peak without it.
5. **Tests** in `scripts/lint/test_memory_admit.py`: repo + step anon rows win over the index tier when the exact filter has none; with no anon history the next tier applies; fake both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the tier share table; if changed, the `need` paragraph.

**Constraints from prior phases:**
- Phases 11, 17 and 18 set which rows match; the widened match keeps every split.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 3–4 (none if Spec 2 stops the phase).
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6.

**Workers:** `1 writer`

**Acceptance gate:**
- The tier share table is in the phase report and the checkpoint notice, with the stop decision.
- If code changed: the memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green; the before/after replay is in the notice. Post-merge check, 24 h after the merge reaches main: steps above their promise and wait minutes against the 24 h before.
- Change-Review line: few admissions use the 0.65 fallback | on natedev while builds wait, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` | the waiting rows name source `measured` and few name `buildlog`. If Spec 2 stops the phase: Change-Review: none — measurement only.

### Phase 20 — Replay a looser head rule, and adopt it only on the replay  · status: todo
**Depends on:** Phase 16 (same file, `memory_admit.py`). Runs before Phase 4. Merge `origin/builds` first. Global order: 21 → 16 → **20** → 4 → 11 → 17 → 18 → 19 → 5 → 8.

**Approved:** showrunner 2026-10-29 (ETA 08:30 EDT 2026-10-30). The showrunner decides adoption from the replay table.

#### Work Order

**Goal:** when the head of the memory queue cannot fit by its own measure, younger steps that fit start, if the replay shows the head is not held back by more than the showrunner's bound.

**Measured:**
- Today's rule (`memory_admit.py` at `a650d945`): `HEAD_SECONDS` 600 (:90), `STRICT_SECONDS` 600 (:91), `BACKFILL_SECONDS` 120 (:92). `head_window` (:307): before 600 s younger steps backfill; after that, 600 s strict then 120 s backfill, in a cycle. `head_priority` (:1025) gives the head priority when it fits now or `MemAvailable + measured >= head need + reserve`. `queue_decision` (:1040) holds every younger step in a strict window (`replace(decision, state="hold")`, :1047-1048), even one that fits.
- The hana showrunner saw 25 steps wait behind one 11.8 GiB head while many 3 GiB clippy steps fit (reported 2026-10-29; not checked against the log in this Work Order).

**Spec:**

1. **Replay first.** Reuse the Phase 15 replay script (`phases/15/` in the run folder). Over the last 7 days of the natedev build log, replay the queue under two rules: (a) today's; (b) in a strict window, when `decide` for the head alone returns hold, a younger step is checked as in a backfill window, on its own budget and memory checks with no `head_need`. Inputs: step arrival = `steps.started_at - mem_wait_s`, `need` from `expected_peak` with the history before the step, run time `duration_s`, charge from `anon_peaks.jsonl`, `MemAvailable` from `samples.mem_used_bytes`, bumps from `queue_bumps`. Check the model first: rule (a) must give total wait minutes close to the recorded `mem_wait_s` sum; print both. Report per day and per repository: wait minutes under each rule, minutes saved, and for each head, how much later it starts under (b) (count, p50, p90, max). Find the hana showrunner's case in the log and show it as one row.
2. **Risk.** For rule (b), count how often an admitted step went above its charge, and the replayed `full avg10` holds, against rule (a).
3. **Adopt rule.** The showrunner sets the bound on the head's added delay from the replay table. The change lands only if the head's added delay stays under that bound. Otherwise the phase records the table in the as-built doc, changes no code, and stops.
4. **If adopted:** one shared policy result. Today four places decide the head's window on their own: `queue_decision`/`head_priority`, `head_wait_reason` (it recomputes the timed window), `read_queue` (it sets `BehindHead`) and `check` (it sets head fields). Make one function return the head's policy for a queue snapshot, with a closed window state that adds `CapacityBackfill` (the head cannot fit, so younger steps that fit start) apart from today's timed backfill. Admission, the blocker lookup, `read_queue`, report-unit's `explain_queue`, `head_wait_text` (:320) and `check`'s head fields all read that result; none recomputes the window. `verify.sh`'s wait lines and `/build:queue` share the text. A bump keeps today's meaning: a bumped head holds younger steps as now.
5. **Tests** in `scripts/lint/test_memory_admit.py`: a younger step that fits starts while a head that cannot fit is strict; a younger step waits while a head that fits is strict; a bumped head still holds; for one snapshot, admission, `blocker`, the queue view's Reason and the shell wait text all show the same window state, `CapacityBackfill` included. Fake both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the replay table and, if adopted, the new head rule (Rule and Waiters paragraphs).

**Constraints from prior phases:**
- Phase 10: the blocker lookup reuses `decide` and `queue_decision`; no second rule.
- Phase 15: the charge after compile is the test-phase charge; the replay uses it.
- Phase 16: merge gates are queue steps; the replay counts them where records exist.
- report Phase 3: `read_queue` and `explain_queue` share one pass; the change goes in that pass.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 4.
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6.
- `scripts/build_queue/build_queue.py` — only if its head Reason text must change (owner report-unit; trial-merge against `origin/builds-report`).

**Workers:** `1 writer`

**Acceptance gate:**
- The replay table is in the phase report and the checkpoint notice with the showrunner's bound and the adopt decision.
- If adopted: the memory admission tests pass; `basedpyright` over the changed Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green. Post-merge check, 24 h after the merge reaches main: wait minutes and the heads' start delay against the 24 h before, and how often a step went above its charge. The phase report records when the check is due.
- Change-Review line: small steps start while a large head cannot fit | on natedev while a large hana nextest head waits, type `/build:queue` in a Claude session | younger clippy rows that fit are running, and the head's Reason names the new window state (text in the as-built).

### Phase 21 — Copies get a root, a busy mark and a lifetime that ends when their work ends  · status: todo
**Depends on:** Phase 14 (`tmp_sweep.py`, `folder_audit.py`). Runs beside Phase 15 (no shared files). Phase 4 follows it on `sweep.py`. Merge `origin/builds` first. It is the resources part of verify-copies Phase 2; verify-unit builds none of it.

**Approved:** showrunner 2026-10-29 23:3x EDT (ETA 06:00 EDT 2026-10-30), from resources' answer to verify (23:05) and verify's four points (verify-copies `9d36907d`).

#### Work Order

**Goal:** a copy under `/tmp/claude/copies` stays while its work runs or waits, and goes when its work has ended; no sweep removes a busy copy, a copy of an open phase, or a parent another copy borrows from.

**Spec:**
1. **Root.** `/tmp/claude/copies` is a kept root in `tmp_sweep.py`, like `scratch/` and `unit/`, and a kept name in `folder_audit.py` (`KEPT_NAMES`), so the daily audit of `/tmp/claude` never removes a copy. Each `<pair>/<copy>` is one entry; its `scratch.py` record is its end evidence; an empty `<pair>/` goes. The scratch guard hook allows the root.
2. **Record** (`scripts/lib/scratch.py`): fields source path, source commit, copy commit, target mode, seed source, `borrows_from` (the parent copy's git dir, from `clone --shared`), owner unit, task, land target; a `phase:` record also names its run dir and phase id. Written atomically. The record and `.claude-busy` stay out of Git status (the copy's `.git/info/exclude`) without hiding any other file: a clean ended copy is removable, and an ordinary untracked file still keeps it.
   **Lifetime as types.** Today `scratch.py`'s record lookup (:206) returns `str | None` (missing and unreadable are one value) and `task_end()` returns `None` both for a task that ended outside and for a bad timestamp. Replace both with closed variants: a record is `Registered | NotRegistered | UnreadableRecord(reason)`; a lifetime is `Live(why) | Ended(evidence) | Unknown(reason)`, where `why` is busy, open phase, borrowed or not landed, and `Unknown` covers an unreadable record, progress file or land target. A sweep removes only `Ended`; `Unknown` is kept and named with its repair reason in the dry run.
3. **Busy mark.** A file `.claude-busy` in a copy (only under `/tmp/claude/copies`). Holders take a shared `flock` for as long as they run; sweepers (`tmp_sweep.py`, scratch end, the floor sweep in `sweep.py`) try a non-blocking exclusive `flock` and skip the folder when it fails. A lock dies with its process. `merge_gate.py --checkout <copy>` takes the shared lock when the checkout is under `/tmp/claude/copies` (verify.sh takes it before its memory wait; verify-unit builds that). A hana target can carry the same file, set with `flock`, without `~/.claude`.
4. **Task `landed:<unit>/<branch>`:** ended when `origin/<branch>` is an ancestor of the record's land target (the production's Merge branch, else `origin/main`) and the copy has no uncommitted or unpushed work. The 7-day no-change rule still applies after that.
5. **Open phase:** a `phase:` copy is not idle while the progress recorder in its run dir shows that phase open, whatever its age.
6. **Child before parent:** scratch end and `tmp_sweep.py` skip a parent while any live record borrows from it, and remove it after the last child.
7. **Hand-over:** `scratch.py hand-over <path> --to <unit>` changes owner and task in one atomic record replace, and keeps the path. A rename would break child copies' `objects/info/alternates` (absolute paths into the parent's git dir), Phase 14's push-recovery records (keyed by the checkout's resolved path) and sibling records, and a rename plus a record write cannot be one atomic step. The folder name names the unit that made the copy; the record names its owner.
8. Tests (private roots and fake `/proc` only): each rule above keeps or removes as stated; an open-phase copy older than 7 days survives `tmp_sweep.py` and the folder audit; an unreadable record, progress file or land target gives `Unknown` and keeps the copy; hand-over keeps the path and a child's alternates still resolve; a shared lock held by a fake run keeps the copy for every sweeper; two shared holders do not wait on each other; a crashed holder's lock does not keep the copy.

**Files:**
- `scripts/lib/scratch.py`, `scripts/lib/test_scratch.py` — Specs 2, 4-8.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py` — Specs 1, 3-6, 8.
- `scripts/lint/folder_audit.py`, `scripts/lint/test_folder_audit.py` — Spec 1, the kept name.
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py` — Spec 3, the busy lock only.
- `scripts/hooks/pre-tool-use-scratch-guard.py`, `scripts/hooks/test_scratch_guard.py` — Spec 1.
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — Spec 3, the `--checkout` lock and its tests (owner natedev showrunner; also-touches line).
- `docs/as-built/build-targets.md` — the copies root, record and lifetime.

**Workers:** `1 writer`

**Acceptance gate:**
- Every changed test module passes; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- Change-Review line: a busy copy survives every sweep | on natedev in a terminal, make a folder under `/tmp/claude/copies/try/a`, hold `flock -s /tmp/claude/copies/try/a/.claude-busy sleep 600`, then run `~/.claude/scripts/lib/py ~/.claude/scripts/lint/tmp_sweep.py --dry-run` | the copy is listed as kept, busy.

### Phase 22 — A stopped hold release finishes when it runs again  · status: todo
**Depends on:** none in this plan (own file, `scripts/build_hold/build_hold.py`). Runs beside Phases 15 and 21. Code may be written during the 02:00–05:00 EDT hold; no test runs and nothing merges before the hold ends (verify releases that hold at 05:00 with the installed copy). Merge `origin/builds` first.

**Approved:** builds showrunner 2026-10-30 01:0x EDT, a row of the rerun sweep the user approved 2026-10-29 (row `scripts/build_hold/build_hold.py:1173`, `~/.claude/docs/plans/sdlc-rerun-sweep.md`).

#### Work Order

**Goal:** a hold release that stops after the last holder file is removed and before its release notices are sent finishes when `release` or `release --resume` runs again.

**Spec:**
1. **Record first.** `release_cycle` (`build_hold.py`, sweep :1173 at sdlc `097b1a0d`) writes a release record (holder identity, the notices still to send) atomically before it removes the last holder file, and removes the record only after every notice is sent.
2. **Rerun.** `release` and `release --resume` with no holder file and a release record finish the recorded notices and remove the record; they no longer refuse (sweep :1365, :1376) in that state. With neither a holder file nor a record, they refuse as today.
3. **Idempotent.** A second run after a finished release changes nothing and exits 0 with one line saying the hold is already released. Every notice is sent at most once per release.
4. **Tests** in `scripts/build_hold/test_build_hold.py`: a fault injected after the last holder file is removed and before the notices (never a sleep), then `release` again, and `release --resume` again, each finishes the notices once; run twice after a finished release; no record and no holder still refuses.
5. **Docs.** `docs/as-built/build-memory-admission.md` (its build-hold part) names the release record and the rerun rule.

**Files:**
- `scripts/build_hold/build_hold.py` — Specs 1–3.
- `scripts/build_hold/test_build_hold.py` — Spec 4.
- `docs/as-built/build-memory-admission.md` — Spec 5, the build-hold part.

**Workers:** `1 writer`

**Acceptance gate:**
- The build-hold tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green. No test runs between 01:50 and the end of the hold.
- Change-Review line: a stopped hold release finishes on a rerun | in a terminal on natedev, `~/.claude/scripts/lib/py -m unittest discover -s ~/.claude/scripts/build_hold -p test_build_hold.py -k release` | the stop-and-rerun tests pass and the notices are sent once.
