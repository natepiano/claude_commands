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
**Depends on:** Phase 3, Phase 9 (the sweep change this phase extends), and the case D verdict of verify-unit's Phase 1 trial (Spec 1–2, frozen). Global order: 21 → 16 → 25 → 24 → 26 → 20 → **4** → 11 → 17 → 18 → 19 → 8. The Change-Review uses `copies.py` (verify-copies Phase 3, verify-unit).

#### Work Order

**Goal:** a new temp clone of a Rust repository reuses the build output of earlier clones of the same repository through one shared build folder per repository. Approved by the showrunner 2026-10-29 (W2); case D adopted 2026-10-30. Measurement: mktemp folders (91.2 compile-h in 7 days, 4.6 steps per clone, 61.6 cold-h on first builds) and style-sync folders (10.9 h) rebuild everything on their first build.

**Spec:**

1. **Trial record (done, not work).** Case D of verify-copies Phase 1 ran on hana under verify's build hold on 2026-10-30: two clones at one commit with one shared `CARGO_BUILD_BUILD_DIR`. The second clone's build took 0.7 s; two builds at once waited 68.7 s on Cargo's build-folder lock; the shared folder held 14.62 GiB.
2. **Verdict (done, not work).** The builds showrunner adopted case D on 2026-10-30 (verify-copies decision rule 1). This phase runs no trial and makes no adopt decision; it builds the support.
3. **The support.** This phase builds the shared-target support only: identity, lifetime, locking and sweep attribution. It adds no copy-making command and no guard steer: `scripts/copies/copies.py` (verify-unit) is the one entry point that makes copies, and its shared target mode calls this support.
   - `scripts/lib/scratch.py`: one function that returns the shared build folder for a repository, for `copies.py` to set as `CARGO_BUILD_BUILD_DIR`. Name it in the as-built.
   - `scripts/style/style_sync.py` already shares one `style-sync-target` per repository (:332) and serializes checkout creation through cleanup. Change only what the shared-folder lifetime needs, and keep its lock.
   - **Shared-target identity.** One mapping from repository to its shared folder. The folder path is a function of the repository identity alone, so the path names its repository when no copy record exists. The folder has one use stamp for all its clones, a lifetime that does not end with one scratch task, and Cargo's build-folder lock. `rebuild_causes()` (`sweep.py` :2291) finds a workspace by the target's parent path and keys the earlier build by `step.workspace`, so a build in a new clone has no earlier build and gets cause `none`. Give it the mapping: a step whose target is a shared folder keys its earlier build by that shared folder, and a removal of the shared folder names the repository.
   - **Attribution after the last copy record.** Copy records end when their copies end; the shared folder stays. So attribution never needs a live copy record. A removal of a shared folder writes the repository identity into its removal row (`record_sweep_removal`, `sweep.py` :2130) at removal time, from the mapping. `parse_sweep_removals` (:2111) reads the field. The retention rewrite in `record_sweep_removal` rewrites rows with only `at`, `path` and `cause` today; it keeps the repository field. A row from before this change has no repository field and keeps today's path attribution.
   - The disk sweeps treat the shared build folder as one target with its own last use. `lock_trees()` (sweep.py :493) returns `TargetLocksAcquired | TargetLockBusy`, not `tuple[list[int], str | None]`.
   - Deletions of the shared folder go through `reclaim()` and count only `ConfirmedDeletion` bytes (Phase 9). Artifacts that Cargo hard-links into a clone's own target folder free nothing while that clone is present; a test in `test_sweep.py` covers a shared folder whose artifacts are still linked from a clone.
4. **Tests** for each changed module, in `scripts/lib/test_scratch.py`, `scripts/style/test_style_sync.py` and `scripts/lint/test_sweep.py`: two clones on one target, a busy shared target, and rebuild attribution after its removal. The sequence test: build in copy A; remove A, so its record ends; sweep the shared target; build in a new copy B. B's rebuild names that repository and that removal's cause. A retention rewrite keeps the repository field, and an old row with no field keeps path attribution. The existing busy-skip and exhaustion tests stay as gates.
5. **Docs.** `docs/as-built/build-targets.md` (Copy records and lifetime; Build output retained by the sweep): the shared build folder, the function `copies.py` calls, and the removal row's repository field.

**Constraints from prior phases:** Phase 21 built the copies root (`/tmp/claude/copies/<pair>/<copy>`), its record and its lifetime (`docs/as-built/build-targets.md`). A clone that uses the shared build folder is registered with `scratch.register_copy` (:189), and the record names its shared folder. While a copy lives, sweep attribution reads its record, never the folder name; after its record ends, the mapping and the removal row name the repository (Spec 3). Every record change goes through `scratch.update_record` (:364) under `copy_record_lock` (:236), and task fields through `fields_for_task` (:346); add no second writer. `.claude-busy` is per copy: the shared build folder's own lifetime and Cargo's build-folder lock are separate, and a sweeper never takes a copy's busy mark to judge the shared folder. The caller: `scripts/copies/copies.py` lands with verify-copies Phase 3, "The helper" (branch `origin/verify-copies`; its plan is `docs/plans/verify-copies.md` there). Its form is `copies.py make --source <worktree> --commit <sha> --phase <id> --purpose <word> [--task <task>]`, with the Python API `make_copy(...)`; in shared mode it gets the folder from this phase's `scratch.py` function and sets `CARGO_BUILD_BUILD_DIR` to it in the copy's `.cargo/config.toml`. Its as-built is `docs/as-built/frozen-copies.md`, from the same phase. At `origin/verify-copies` `dafd6118` neither file exists yet.

**Files:**
- `scripts/lib/scratch.py` — Spec 3.
- `scripts/lib/test_scratch.py` — Spec 4.
- `scripts/style/style_sync.py` — Spec 3 (owner natedev style-unit; trial-merge against `origin/natedev`).
- `scripts/lint/sweep.py` — Spec 3, the shared folder as one target and the removal row's repository field.
- `scripts/lint/test_sweep.py`, `scripts/style/test_style_sync.py` — Spec 4.
- `docs/as-built/build-targets.md` — Spec 5.

**Workers:** `1 writer + 1 tester`
- `impl` — shared-target support (`scratch.py`, `style_sync.py`), sweep integration (`sweep.py`) and docs (`build-targets.md`).
- `test` — `test_scratch.py`, `test_style_sync.py`, `test_sweep.py`.

**Acceptance gate:**
- The tests of each changed module pass; `basedpyright` over each changed file reports `0 errors, 0 warnings, 0 notes`. Post-merge check, 24 h after the merge reaches main (as in Phase 12's ruling): the first-build cold-h of mktemp and scratch.py folders, next to the 7-day figures. `rebuilds` does not separate first builds (a workspace's first step has cause `none`), so compute it from the build-log records: the first recorded step of each new workspace folder. The removal records in `rebuilds` name the shared folder's repository. The phase report records when the check is due; the result goes in a follow-up notice.
- The notice has an `also touches scripts/style/style_sync.py (owner natedev style-unit)` line, tested against `origin/natedev`.
- Change-Review line: a new temp clone of hana builds only the crates that changed | in any Claude session on natedev, make two copies of hana at one commit with `copies.py make` in its shared target mode (its form is in `docs/as-built/frozen-copies.md`) and build each with `verify.sh build`; while `copies.py` is not on `origin/builds`, make the two clones with `scratch.py new` and build each with `CARGO_BUILD_BUILD_DIR` set to the Spec 3 function's output | the second clone's build log step compiles under half the crates of the first.

### Phase 5 — Clone builds turn incremental mode off, and mend uses the compile cache  · status: dropped
**Dropped:** builds showrunner 2026-10-30, on case E of verify-unit's copies trial (hana `c421a1a6`, verify's build hold 2026-10-30). Clone builds with `CARGO_INCREMENTAL=0` got 0 sccache hits on workspace crates, so incremental off buys nothing. mend with sccache ran 132.4 s against 115.4 s without, so mend keeps `RUSTC_WRAPPER=`. Neither adopt rule held; `verify.sh` and `invoke.sh` keep their current lines. Log: `/tmp/claude/unit/bf5b8d4c-77ff-4f7b-9318-afb6114ad8f2/copies/results/case_e.log`.

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
**Depends on:** Phase 2; Phases 10, 12 and 11 (`memory_admit.py`, its tests and doc); report-unit's bare-step writer in `waiter_identity` (merged at HEAD 64a94048)

**Approved:** showrunner 2026-10-29, after Phase 2. report-unit keeps its bare-step writer (it fixes the cause in main) and drops only its read-side strip; this phase builds the repository field on report-unit's merge. The edit to `builds-production.md:14` is approved.

#### Work Order

**Goal:** the showrunner's ruling (2026-10-29 13:15, on the user's word that the current state is a bad design): shared coordination code (memory gate, queue rows, holds) writes shared state from one installed copy, `~/.claude` main. Every caller runs that copy, never its own checkout's. Tests keep their fakes. Queue rows carry the repository and the step as separate fields.

Measured 2026-10-29 13:20 EDT (read-only survey of `/proc` and `~/.local/state/buildlog/admission`):
- All 19 live `.waiting` rows carry the combined label ("hana clippy"). Main's own `memory_admit.py` writes it (`waiter_identity`, `"step": f"{repo} {step}"`, rewritten by the held-poll write in `check`). The 13 `.reservation` rows already keep `repo` and `step` apart. So the old format is main's writer, not an old copy.
- The read-side prefix strip is not in main. It is an uncommitted change in report-unit's worktree (`scripts/lint/memory_admit.py` :1126–1128 strip, :1068–1071 bare-step writer).
- Checkout copies that run today: `verify.sh`'s relative lookups in the `override_guard.sh` source line, `LOCAL_RELEASE_WAIT_SCRIPT` (`local_release_wait.py`, loads `scripts.build_hold` from its own tree), `MESH_RUNS_SCRIPT` (`mesh_runs.py`), `MAC_IDLE_SCRIPT` (`mac_idle.py`, writes Mac-idle markers) and the `BUILD_TARGETS_SCRIPT` fallback; `memory_gate.sh:26` names `memory_admit.py` beside itself with no override; `merge_gate.py:52,59` imports `build_hold` from the copy that runs (it reads holds only); `scripts/message/top_level.py:80` and `scripts/production/retire_unit.py:75-79` write holds through the copy that runs. One live checkout `verify.sh` (claude-build-followups-verify, real `HOME`, no fake dirs) held a queue row at 13:20.
- Already main by registration: the BRP launch hook, the build-hold hook, the `cargo` wrapper, `/build:queue`.

**Spec:**

1. **The writers refuse a checkout copy.** `memory_admit.py` and `build_hold.py` refuse to write the real state directories (`~/.local/state/buildlog/admission`, the real hold dir `~/.local/state/build-hold`, the real release dir `~/.local/state/build-hold-release`) when their own file is not the installed copy under `~/.claude/scripts/`. With a fake `BUILDLOG_DIR` / `BUILD_HOLD_DIR` / `BUILD_HOLD_RELEASE_DIR` (every test), they write as now. The refusal names the installed path to run instead. This one guard is the fix at the cause; items 2–4 make every caller pass it.
   - **One place.** Extend the existing guard, `scripts/build_hold/override_guard.py` (`refusal()` :42, `refuse_overrides()` :71, the account-database home `user_home()` :28). Add one function there that takes the calling script, its installed relative path and the directory about to change, and returns allowed or a refusal line. Write no second home lookup or installed-path check anywhere.
   - **Refuse before any change, imported calls included.** A CLI-only check misses two boundaries: Phase 16's merge gate calls `memory_admit.check` in its own process, and `recorded_releases()` (`build_hold.py` :1105) moves old receipts during a lookup, before `release_cycle` runs. So call the guard where every change passes: `memory_admit.admission_dir()` (:1020; `check`, `bump_waiter`, `release` and `set_width` all get their directory from it), and `build_hold.release_lock()` (:919; every holder-file write, release-record write and receipt move runs under it, `write_hold` and `release_hold` included), which checks the holder dir and the release dir. Read-only paths (`queue_view`, which reads `store.root() / "admission"` directly, and `status`) change nothing and stay allowed. The guard raises one typed error; the CLIs turn it into exit 2 with the named reason, and an imported caller gets the error (the merge gate stops on it, Phase 16 Spec 4).
   - Each directory is judged on its own: a fake `BUILD_HOLD_DIR` with the real release dir refuses at the release dir, and a real `BUILD_HOLD_DIR` with a fake release dir refuses at the holder dir.
2. **Queue rows carry separate fields.** report-unit's merged writer already gives `.waiting` rows a bare `step`; this phase adds the `repository` field beside it, as `.reservation` rows carry `repo`. The queue view's reader takes the fields; no prefix strip exists anywhere. `build_queue.py` shows the repository beside the step for waiting and running rows alike (report-unit's file; its one display change only).
3. **Callers run the installed copy.**
   - `memory_gate.sh`: `BUILDLOG_MEM_ADMIT_SCRIPT="${BUILDLOG_MEM_ADMIT_SCRIPT:-$HOME/.claude/scripts/lint/memory_admit.py}"`. `invoke.sh:89` sources `$HOME/.claude/scripts/lint/memory_gate.sh`.
   - `verify.sh`: one root, `SCRIPTS="$HOME/.claude/scripts"`, for `override_guard.sh`, `local_release_wait.py`, `mesh_runs.py`, `mac_idle.py` and the `build_targets` fallback. Tests fake `HOME` with a copied tree already; the `VERIFY_*_SCRIPT` overrides stay.
   - `merge_gate.py` reads holds only and already runs from `~/.claude` main (Delegation Context, Test). No change. The notice records this.
   - `top_level.py`, `retire_unit.py`: the `build_hold.py` path from `Path.home() / ".claude"`.
4. **The plan lines that teach a checkout run** (`docs/plans/builds-production.md:14`, `docs/plans/build-followups-worker-seats.md:48`) name `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout <tree>`.
5. **Tests:** `scripts/build_hold/override_guard.py` already finds the real home through the account database and refuses fake state variables when the installed script runs. Keep that refusal. Routing tests copy scripts under a fake home; the checkout-write refusal is tested against an injected canonical-home and state fixture, never real coordination state. A checkout-copy `memory_admit.py` against the real-dir path refuses (exit 2, named reason) and against a fake dir writes; same for `build_hold.py`; a `.waiting` row has `repository` and a bare `step`; the queue view shows both; `verify.sh` and `memory_gate.sh` resolve to `$HOME/.claude/scripts` under a fake `HOME`. Guard tests in `scripts/build_hold/test_override_guard.py`: fake holder dir with the real release dir, and the real holder dir with a fake release dir, each refuse and leave every file in the real dir unchanged; an imported `memory_admit.check` call from a checkout copy against the real admission dir raises before it writes a file; `recorded_releases()` from a checkout copy with a real release dir that holds an old receipt refuses before it moves the receipt; the installed script's existing override refusal tests in that module pass unchanged. Fake memory readings throughout (Phase 1 helper).

**Constraints from prior phases:** Phase 22 added hold release state under `BUILD_HOLD_RELEASE_DIR`: `pending-release.json`, `releases/<id>/` and `receipts/<holder-key>/completed-release.json` (`docs/as-built/build-memory-admission.md`, Build hold). It is shared build state, so the installed copy is its only writer, as for the holder files and the cycle. Phase 16 makes the merge gate call `memory_admit.check` and `release` in its own process; the guard covers that call through `admission_dir()`. Phase 23's `status` reader of pending release records is read-only and takes no `release_lock()`, so the guard does not refuse it.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/lint/test_memory_admit.py` — items 1–2, 3, 5.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — items 1, 5.
- `scripts/build_hold/override_guard.py`, `scripts/build_hold/test_override_guard.py` — item 1, the one guard function; item 5, the guard tests.
- `scripts/build_queue/build_queue.py`, `scripts/build_queue/test_build_queue.py` — item 2 (owner report-unit).
- `scripts/direct/verify.sh`, `scripts/lint/invoke.sh` — item 3 (owner verify-unit). The tester owns routing coverage in `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_nix_shell.py`, `scripts/lint/test_invoke_scope.py`, `scripts/message/test_top_level.py` and `scripts/production/test_retire_unit.py`, plus the admission, hold and queue tests named above. Cover the installed paths and retained overrides under a fake HOME and private coordination state.
- `scripts/production/merge_gate.py` — no change (item 3); the notice records why.
- `scripts/message/top_level.py`, `scripts/production/retire_unit.py` — item 3 (owners per natedev's Owners table, named in the notice).
- `docs/plans/builds-production.md`, `docs/plans/build-followups-worker-seats.md` — item 4.
- `docs/as-built/build-memory-admission.md` — the one-copy rule and the row fields.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–4 (`override_guard.py` included) and the doc.
- `test` — item 5 (`test_override_guard.py` included).

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
5. Tests in `scripts/lint/test_memory_admit.py`: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; a compile run's promise uses only compile runs; an anon row with no `call_id` and step match counts as neither; a fresh run keeps the `fresh_test_peak` promise; a `merge-gate` row with no index match stays in the gate's history; each test fakes both memory readings.
6. Docs: `docs/as-built/build-memory-admission.md` gets the replay table before and after, and the new rules. The replay script lives in the session folder and is not committed.

**Constraints from prior phases:**
- Phase 12: Phase 11 changes estimates (`need`) only. It keeps the running charge as Phase 12 built it: current memory, the ramp and unknown-peak promises, the observed peak with its `.wave` decay after 1500 s, the missing-`.wave` fallback to the `.max` mtime, the recovery start spacing, and the estimate-expiry checks after the lock.
- Phase 16: merge gates write `anon_peaks.jsonl` rows under step `merge-gate`, and those rows have no Cargo index step to join. Spec 4's "every step" classifier does not drop them as "neither": `merge-gate` rows stay a class of their own, and a gate's estimate (Phase 16's `gate_estimate`) keeps reading every `merge-gate` row. The compile and no-compile split applies to Cargo steps only (Spec 5 tests it).

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
**Depends on:** Phase 14 (same file, `memory_admit.py`; its row 3 charges a step by the step cgroup that the `invoke.sh` sampler reads, and adds cleanup paths that must remove this phase's new sidecar). Merge `origin/builds` first. report-unit's build-why change (Phase 20's prerequisite: `explain_queue`, `ChargeBasis`, waiter `need_source`) may land first; if it is on `origin/builds`, merge it before dispatch. **Parked** (builds showrunner 2026-10-30): both replay arms (history max, max + 10%) failed the landing rule, so the same-width charge stays as today. report-unit records `test_phase_peak_bytes` (the per-step test-phase peak) in a new phase; this phase resumes only after that phase merges on `origin/builds` and a replay on its data passes. Global order: 21 → 16 → 25 → 24 → 26 → 20 → 4 → 11 → 17 → 18 → 19 → 8; this phase merges on top when it resumes.

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
   - Save the replay script in the run folder (`phases/15/`), not in the repository. Its first run (`phases/15/replay_report.md`) failed the model check (639.56 times the recorded wait); Phase 20 Spec 1 owns its calibration, so rerun this replay only on Phase 20's calibrated model.
2. **Record the test phase.** The sampler writes a new sidecar beside `.max`, `<reservation>.tests` = `"<first test start epoch> <test-phase anon peak>"`, atomically as `.max` is written (temporary file, then `mv -f`). `release` adds `test_phase_started_at` and `test_phase_peak_bytes` to the `anon_peaks.jsonl` row when the sidecar exists, and the stored `EstimatedTestPhaseCharge` (bytes, history cutoff, width form, recorded widths) when the reservation has one, so the 24-hour check compares each test phase with the charge it really had. Every path that deletes a reservation removes `.tests` with `.max` and `.wave`: `release`, `live_reservations` (:986-988), the `check` cleanup (:1233-1236), and Phase 14's cgroup-liveness paths.
3. **The charge after compile.** `check` computes an `EstimatedTestPhaseCharge` for a nextest step at admission, outside the admission lock, as it computes `expected_peak`. It holds the bytes, the history cutoff, the recorded widths it used and the width form (Spec 4), or `NoTestPhaseHistory`. It is stored in the reservation and lives until the reservation is released; a later width change (`set_width`) computes it again outside the lock. When a reservation has a `.tests` mark, `reservation_memory` drops the compile peak and charges `max(current memory, the stored test-phase charge)`. The test-phase charge is the p90 of earlier test-phase peaks of the same family (repository + test filter), from `anon_peaks.jsonl` rows that carry `test_phase_peak_bytes`, over `HISTORY_DAYS`; Spec 4 picks which recorded widths supply it. It is the whole test phase with all its parallel test processes, never one test's allowance. The single-test p90 of the repository (filtered, no-compile runs) is a floor only. With no test-phase history for the key, the step keeps its compile charge until it ends: no decay. The 1500 s decay stays only for steps with no mark: steps other than nextest, and agent, alias or unknown cargo calls (`steps.caller`).
4. **Width rule.** The run's width is the reservation's `test_threads` (the share after `set_width`); it is not part of the family key. The form is a closed set, picked in this order: `same_width` — the family has rows at the run's width: their p90; `fitted` — the family has rows at two or more other widths: a fixed part plus a per-thread part, fitted from them; `wider_unscaled` — rows only at wider widths: the p90 of the nearest wider width, no scale-down; `scaled_up` — rows only at narrower widths: the p90 of the widest narrower width × width / recorded width. The replay reports the charge with and without `fitted`, so the showrunner sees what the fit changes.
5. **Build-why names.** Phase 20 owns the handoff with report-unit's build-why change (`explain_queue() -> QueueExplanation`, `ChargeBasis`, waiter `need_source`, `build_why.py`) and confirms its names. This phase adds the basis variant `TestPhasePeak(bytes, runs, width, recorded_widths, form)` with `form` one of `same_width | fitted | wider_unscaled | scaled_up`, and the need source `"test_phase_peak"`. If build-why is on `origin/builds`, add the variant and its words in `build_why.py`; if not, add only the typed value that `reservation_memory` returns, and report-unit adds the variant.
6. **Tests** in `scripts/lint/test_memory_admit.py`: the compile charge holds until the mark; after the mark the charge is the stored test-phase charge with history and the compile peak without it (no decay); a step with no mark still decays at 1500 s; each width form: `same_width`, `fitted` with two other widths, `wider_unscaled` with no scale-down, `scaled_up`; the estimate is computed outside the lock and stored in the reservation; the floor; `.tests` is removed on every delete path; the `release` row carries the two new fields. Sampler tests in `scripts/lint/test_invoke_scope.py` (owner verify-unit): the mark appears when a fake test binary starts in a fake scope, and a sampler from before this change (no `.tests`) keeps today's charge. Every test fakes both memory readings (`fake_memory_readings`).
7. **Docs.** `docs/as-built/build-memory-admission.md`: the mark, the sidecar, the charge after compile, the width rule, the replay table. Update the running-charge constraints of Phases 11, 17 and 20 in this plan: the running charge is as Phases 12 and 15 built it.

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
- The notice has `also touches` lines for `invoke.sh` and `test_invoke_scope.py` (verify-unit, against `origin/natedev-verify` and `origin/natedev`) and, if changed, `build_why.py` (report-unit, against `origin/builds-report`).
- Change-Review line: a nextest step's charge falls to its test-phase peak once its tests start | on natedev, start `~/.claude/scripts/direct/verify.sh test hana_diegetic` in `~/rust/hana`; after its compile, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` (before build-why lands: `cat ~/.local/state/buildlog/admission/*.tests`) | the step's charge reads as the test-phase peak with its width form, below its compile peak.

### Phase 16 — Python test gates take a place in the shared admission queue  · status: done
**Depends on:** Phase 21 (same file, `merge_gate.py`: Phase 21's checkout lock lands first). Runs before Phase 20. Merge `origin/builds` first. Global order: 21 → **16** → 25 → 24 → 26 → 20 → 4 → 11 → 17 → 18 → 19 → 8.

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

1. **Measure first.** From `/proc` while gates run, and from the last 7 days of `samples` (`builds_anon_bytes`, `stall_full_us`, `cpu_stall_some_us`) and `steps`: how many gates ran at once, each gate's peak memory (the gate prints `Largest module peak` at :927; its whole tree is what matters), and the wait minutes and pressure holds of cargo steps while gates ran against while none ran. The run with no queue place is `merge_gate.py --tests-only`, the `~/.claude` merge gate: its parallel suites (about 10 min) and its basedpyright run sit outside the memory and CPU queue, and on 2026-10-29 it pushed natedev load to 67 (showrunner, 23:3x EDT). That run gets a queue place with a CPU charge. `verify.sh test` already waits in the memory queue; leave it as it is. List any other Python run found outside the queue (a unit's own pytest through `verify.sh lint`, say) in the phase report and give it the same rule. **Correction (resources-unit 2026-10-30, at review):** the split of cargo waits and pressure holds with and without gates cannot come from old data: no gate recorded its start, and `passed-trees.jsonl` keeps only passing runs' end times. The phase records each gate's queue place and `merge-gate` history row, so the 24 h post-merge check makes that split.
2. **Memory: the gate's admission and history contract.** A gate takes one reservation in the admission queue before it starts its first module, through `memory_admit.check` (:1242) in the installed `~/.claude` copy (the gate already runs from `~/.claude` main), called in the gate process. It waits in the queue as a cargo step waits, and `/build:queue` and the blocker lookup show it as step `merge-gate`.
   - **Identity and estimate.** `check` takes `profile: StepProfile | None` today. A supplied profile older than `HEAD_SECONDS` (600 s, :1251) becomes `None`, and `check` then infers repository, step and estimate from cargo arguments, which a gate has none of. Replace the optional with a closed union: `InferFromArgv` (today's `None`), `StepProfile` (the shell's cached profile, :1749) and `GateProfile` (repository `~/.claude`, step `merge-gate`, worktree = the checkout, and a `GateEstimate`). On the 600 s refresh a `GateProfile` is estimated again by one function, `gate_estimate` in `memory_admit.py`, and keeps its identity; it never goes through argv inference.
   - **No history is a named state.** `GateEstimate = MeasuredGatePeak(bytes, runs) | NoGateHistory(cap_bytes)`. `MeasuredGatePeak` is the p90 of the gate's `merge-gate` rows in `anon_peaks.jsonl` over `HISTORY_DAYS`. `NoGateHistory` asks for the gate's own memory cap (`--memory-cap-mib`, default `DEFAULT_MEMORY_CAP_MIB` 4096), because `SampledRSSAdmission` keeps the gate's sampled tree under that cap. The waiter row's need source is `measured` or `fallback` to match.
   - **Running observations.** The gate's reservation is charged from what the gate writes, as `invoke.sh`'s sampler writes for cargo. Each memory sample in `run_suites` (`admission.observe`, about :804) that raises the whole-tree peak (`admission.peak_mib`, the `peak memory:` figure at :928) writes `<reservation>.max` and `.wave` in the sampler's form: a temporary file, then `mv`/`os.replace`. So the running charge and its 1500 s decay work for the gate unchanged.
   - **History.** `release` (:1654) adds an `anon_peaks.jsonl` row only when `.max` is positive and the reservation's `sidecar` is a file. The gate passes a sidecar file in its private run folder with an empty scope name (it has no single step scope; its owner pid's life decides, `reservation_lifetime` :780), keeps it until release, and releases on every exit path. The `merge-gate` rows in `anon_peaks.jsonl` are the gate's one history: no second record in `gate_state_dir()`.
3. **CPU.** Each gate module, `types` included, takes one `/dev/steve` slot before it starts and gives it back when it ends (read one byte from the fifo, write it back), so modules and rustc share one pool. The token read never blocks the runner: the launch loop (`run_suites`, :818) tries a non-blocking read and, with no token, goes on reaping finished jobs, sampling memory, and handling holds and cancellation, then tries again. A token is owned by the job that took it and is written back on every exit path (pass, failure, kill, stop, an exception in the runner). With no `/dev/steve` (the Mac, a test), the gate runs as today. The module count limit stays as a per-gate cap.
4. **Fail closed.** An admission error stops the gate with the checker's last line, as `memory_gate.sh` refuses a step (Invariants: only an explicit admission answer starts a step).
5. **Tests** in the gate's suite modules by lane. The fakes the parent gate's admission call needs (a private `BUILDLOG_DIR` ledger, both memory readings from `fake_memory_readings` in `scripts/lint/memory_test_support.py`, and a fake jobserver fifo) go once into `MergeGateFixture.bind_fixture` (`scripts/production/merge_gate_test_support.py` :297, environment at :307), never into each suite module. `test_merge_gate_suites_admission.py`: a gate waits while the fake queue is full and starts when it frees; the reservation is released after a pass, a failure, a module kill and a stop; the first gate (no `merge-gate` rows) asks for its cap as `NoGateHistory`; a gate that waits over 600 s keeps its identity and its gate estimate after the refresh; after release a `merge-gate` row with the gate's peak is in `anon_peaks.jsonl`, and the next gate's estimate is `MeasuredGatePeak`. `test_merge_gate_suites_resources.py`: a slot is taken and returned per module with a fake fifo; two jobs share a one-slot fake pool and both finish, the second starting after the first returns its token; no `/dev/steve` runs as today; while the gate waits for admission and for a token, it still holds Phase 21's copy lock (`scratch.copy_lock`, `merge_gate.py` :1377), and a sweeper's non-blocking exclusive try on the checkout's `.claude-busy` fails, so the sweeper reports the checkout busy. `test_merge_gate_suites_signals.py`: a stop while a job waits for a token returns every owned token. `test_merge_gate_suites_types.py`: `types` takes a slot. `scripts/lint/test_memory_admit.py`: the `check` profile union, `gate_estimate` for both variants, and the refresh of a `GateProfile`. Fake both memory readings; never touch the real `/dev/steve`.
6. **Docs.** `docs/as-built/merge-gate-and-test-speed.md` (the gate's place and slots) and `docs/as-built/build-memory-admission.md` (the `merge-gate` step).

**Constraints from prior phases:**
- Phase 2: every gate module runs under the live-memory tripwire, and the `types` suite has none. The gate's own admission call runs in the gate process, outside every module's environment.
- Phase 14 rows 1–2 change `promote` and the checkpoint push in the same file; build on them.
- Phase 8 (later) makes the admission writers refuse a checkout copy; the gate calls the installed copy, so it passes.
- `BuildHoldWait` (:226) stays first: a gate waits out a build hold before it asks for memory.
- Phase 21: `main` runs every mode inside `scratch.copy_lock(<checkout>)` (:1377), which holds a shared `flock` on `.claude-busy` for a checkout under `/tmp/claude/copies`. The admission wait and the token waits run inside `run_suites`, so inside that lock; keep them there.
- Phase 11 (later) keeps `merge-gate` rows in their own class; `gate_estimate` is their one reader.

**Files:**
- `scripts/production/merge_gate.py` — Specs 2–4 (owner natedev showrunner; also-touches at checkpoint, trial-merge against `origin/natedev`).
- `scripts/production/merge_gate_test_support.py` — Spec 5, the shared admission fakes in `MergeGateFixture` (owner natedev showrunner).
- `scripts/production/test_merge_gate_suites_admission.py`, `test_merge_gate_suites_resources.py`, `test_merge_gate_suites_signals.py`, `test_merge_gate_suites_types.py` — Spec 5 (owner natedev showrunner).
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py` — Spec 2, the `check` profile union, `GateProfile` and `gate_estimate`; Spec 5.
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — Spec 6.

**Workers:** `1 writer + 1 tester`
- `impl` — gate runner (`merge_gate.py`), admission boundary (`memory_admit.py`) and docs (the two as-built docs).
- `test` — shared gate fixture (`merge_gate_test_support.py`), the four suite modules and the affected admission tests (`test_memory_admit.py`).

**Acceptance gate:**
- The changed test modules pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The Spec 1 numbers are in the phase report: gates at once, gate peak memory, and cargo-step wait minutes and pressure holds with and without gates.
- Post-merge check, 24 h after the merge reaches main: cargo-step wait minutes, gate wait minutes, `full avg10` holds and earlyoom kills against the 24 h before; how often a gate's tree went above its reservation's `need`. The phase report records when the check is due.
- The notice has `also touches scripts/production/merge_gate.py, merge_gate_test_support.py and the changed test_merge_gate_suites_*.py modules (owner natedev showrunner)`, tested against `origin/natedev`.
- Change-Review line: a merge gate waits in the memory queue and takes build slots | in a terminal on natedev, `cd ~/.claude && env HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout ~/.claude --tests-only`, then type `/build:queue` in a Claude session | the gate shows as a `merge-gate` row, running or waiting, with its need.

### Phase 17 — The nextest promise splits by compile size  · status: todo
**Depends on:** Phase 11 (same `expected_peak`; Phase 11 splits compile runs from no-compile runs). Merge `origin/builds` first. Global order: 21 → 16 → 25 → 24 → 26 → 20 → 4 → 11 → **17** → 18 → 19 → 8.

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
- Running charge: the band changes `need` and so the ramp promise only; it leaves the running charge as built now (`build_charge` :478, `reservation_memory` :828; `docs/as-built/build-memory-admission.md`): a running step charges the largest of its current resident and swap bytes, its observed `.max` peak, its promise during `RAMP_SECONDS` (120 s), and its promise when the peak is unknown. The `.max` peak expires `PEAK_DECAY_SECONDS` (1500 s) after the last near-peak time in `.wave`, or after the `.max` mtime when `.wave` is absent. Phase 15 is parked; its test-phase charge after the mark applies only if Phase 15 lands first, and the band leaves that charge unchanged too.

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
**Depends on:** Phase 17 (same `expected_peak`). Merge `origin/builds` first. Global order: 21 → 16 → 25 → 24 → 26 → 20 → 4 → 11 → 17 → **18** → 19 → 8.

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
**Depends on:** Phase 18 (same `expected_peak`). Runs before Phase 8. Merge `origin/builds` first. Global order: 21 → 16 → 25 → 24 → 26 → 20 → 4 → 11 → 17 → 18 → **19** → 8.

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
**Depends on:** Phase 16 (same file, `memory_admit.py`); report-unit's build-why change on `origin/builds` (`explain_queue`, Spec 2). Runs before Phase 4. Merge `origin/builds` first. Global order: 21 → 16 → 25 → 24 → 26 → **20** → 4 → 11 → 17 → 18 → 19 → 8.

**Approved:** showrunner 2026-10-29 (ETA 08:30 EDT 2026-10-30). The showrunner decides adoption from the replay table.

#### Work Order

**Goal:** when the head of the memory queue cannot fit by its own measure, younger steps that fit start, if the replay shows the head is not held back by more than the showrunner's bound.

**Measured:**
- Today's rule (`memory_admit.py` at `a650d945`): `HEAD_SECONDS` 600 (:90), `STRICT_SECONDS` 600 (:91), `BACKFILL_SECONDS` 120 (:92). `head_window` (:307): before 600 s younger steps backfill; after that, 600 s strict then 120 s backfill, in a cycle. `head_priority` (:1025) gives the head priority when it fits now or `MemAvailable + measured >= head need + reserve`. `queue_decision` (:1040) holds every younger step in a strict window (`replace(decision, state="hold")`, :1047-1048), even one that fits.
- The hana showrunner saw 25 steps wait behind one 11.8 GiB head while many 3 GiB clippy steps fit (reported 2026-10-29; not checked against the log in this Work Order).

**Spec:**

1. **Calibrate the queue model first.** The Phase 15 replay (`/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/phases/15/replay.py`; report `replay_report.md` in the same folder) is the failed baseline: its replay of today's rule gave 113,965,165.46 wait minutes against 178,192.68 recorded `mem_wait_s` minutes, 639.56 times the recorded wait. Its report names the admission inputs it lacks: `MemAvailable` and host totals at each admission poll, PSI avg10, exact sccache anon, request retries, queue bumps, and `.max`/`.wave` growth. Over the last 7 days of the natedev build log, the replay of today's rule must give total wait minutes within 25% of the recorded `mem_wait_s` sum (all steps, and hana alone). Inputs: step arrival = `steps.started_at - mem_wait_s`, `need` from `expected_peak` with the history before the step, run time `duration_s`, charge from `anon_peaks.jsonl`, `MemAvailable` from `samples.mem_used_bytes`, bumps from `queue_bumps`. Print both totals and the ratio, and name each missing input the replay uses a stand-in for. If the replay cannot reach the bound, the phase records the totals, the ratio and the missing inputs in the as-built doc and the phase report, names the record that each missing input needs as a follow-up item, changes no admission code, and stops.
2. **One head policy, before the comparison.** Today four places decide the head's window on their own: `queue_decision` (:1092)/`head_priority` (:1077), `head_wait_reason` (:302; it recomputes the timed window through `head_window` :307), `read_queue` (:1544; it sets `BehindHead`) and `check` (it sets head fields). Make one function return the head's policy for a queue snapshot, with a closed window state and a closed rule argument: rule (a) today's timed windows, rule (b) the capacity rule of Spec 3. Production passes rule (a). Admission, the blocker lookup, `read_queue`, `explain_queue` (in the pass it shares with `read_queue`), `head_wait_text` (:320) and `check`'s head fields all read that result; none recomputes the window. This step changes no admission behavior: the existing tests pass unchanged. The replay imports this function from the checkout; it holds no copy of the rule. It lands whether or not Spec 5 adopts rule (b).
3. **Comparative replay.** On the calibrated model of Spec 1, replay the queue through the Spec 2 function under two rules: (a) today's; (b) in a strict window, when `decide` for the head alone returns hold, a younger step is checked as in a backfill window, on its own budget and memory checks with no `head_need`. Report per day and per repository: wait minutes under each rule, minutes saved, and for each head, how much later it starts under (b) (count, p50, p90, max). Find the hana showrunner's case in the log and show it as one row.
4. **Risk.** For rule (b), count how often an admitted step went above its charge, and the replayed `full avg10` holds, against rule (a).
5. **Adopt rule.** The showrunner sets the bound on the head's added delay from the replay table. Rule (b) lands only if the head's added delay stays under that bound. Otherwise the phase records the table in the as-built doc and lands only the Spec 2 function.
6. **If adopted:** production passes rule (b). The window state adds `CapacityBackfill` (the head cannot fit, so younger steps that fit start) apart from today's timed backfill. `verify.sh`'s wait lines and `/build:queue` share the text. A bump keeps today's meaning: a bumped head holds younger steps as now.
7. **Tests** in `scripts/lint/test_memory_admit.py`: for one snapshot under rule (a), admission, `blocker`, the queue view's Reason, `explain_queue` and the shell wait text show the same window state (Spec 2). If adopted: a younger step that fits starts while a head that cannot fit is strict; a younger step waits while a head that fits is strict; a bumped head still holds; the same five readers show `CapacityBackfill`. In `scripts/build_queue/test_build_why.py` (owner report-unit), only if `build_why.py` changes: its head text for each window state. Fake both memory readings.
8. **Docs.** `docs/as-built/build-memory-admission.md`: the calibration totals, the replay table and, if adopted, the new head rule (Rule and Waiters paragraphs).

**Constraints from prior phases:**
- Phase 10: the blocker lookup reuses `decide` and `queue_decision`; no second rule.
- Running charge: the replay uses the running charge as built now (`build_charge` :478, `reservation_memory` :828; `docs/as-built/build-memory-admission.md`): a running step charges the largest of its current resident and swap bytes, its observed `.max` peak, its promise during `RAMP_SECONDS` (120 s), and its promise when the peak is unknown. The `.max` peak expires `PEAK_DECAY_SECONDS` (1500 s) after the last near-peak time in `.wave`, or after the `.max` mtime when `.wave` is absent. Phase 15 is parked; its test-phase charge after the mark applies only if Phase 15 lands before this phase dispatches; then the replay uses it for steps with a `.tests` mark.
- Phase 16: merge gates are queue steps; the replay counts them where records exist.
- report-unit's build-why change (on `origin/builds-report`, not yet on `origin/builds`): `explain_queue() -> QueueExplanation` in `memory_admit.py`, `ChargeBasis` and the waiter `need_source` as closed unions, and `scripts/build_queue/build_why.py`. `read_queue` and `explain_queue` share one internal pass; the policy result goes in that pass.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 2 and 6.
- `scripts/lint/test_memory_admit.py` — Spec 7.
- `docs/as-built/build-memory-admission.md` — Specs 1, 3 and 8.
- `scripts/build_queue/build_queue.py` — only if its head Reason text must change (owner report-unit; trial-merge against `origin/builds-report`).
- `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py` — only if the head text in build-why must change (owner report-unit; trial-merge against `origin/builds-report`).

**Workers:** `1 writer`

**Acceptance gate:**
- The calibration totals (replayed and recorded wait minutes, the ratio, the missing inputs) are in the phase report and the checkpoint notice. If Spec 1 stops the phase: Change-Review: none — measurement only.
- The replay table is in the phase report and the checkpoint notice with the showrunner's bound and the adopt decision.
- The Spec 2 function: the memory admission tests pass unchanged and with the new same-state test; `basedpyright` over the changed Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- If adopted: the memory admission tests pass; `basedpyright` over the changed Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green. Post-merge check, 24 h after the merge reaches main: wait minutes and the heads' start delay against the 24 h before, and how often a step went above its charge. The phase report records when the check is due.
- Change-Review line: small steps start while a large head cannot fit | on natedev while a large hana nextest head waits, type `/build:queue` in a Claude session | younger clippy rows that fit are running, and the head's Reason names the new window state (text in the as-built).

### Phase 21 — Copies get a root, a busy mark and a lifetime that ends when their work ends  · status: done

#### As-built

- `/tmp/claude/copies/<pair>/<copy>` is a kept root in `tmp_sweep.py`, in `folder_audit.py` `KEPT_NAMES` and in the scratch guard hook. `scratch.copies_root()` resolves it once with `realpath` (`SCRATCH_COPIES_ROOT` sets it in tests). Each copy is one entry; an empty `<pair>/` goes.
- Each copy has one `.scratch.json` record: source path and commit, copy commit, target mode, seed source, `borrows_from`, owner unit, task, land target and copy identity; a `phase:` record adds its run dir and phase id. `scratch.register_copy` writes it. Only the record and `.claude-busy` go in the copy's `.git/info/exclude`.
- One record writer: `update_record` under `copy_record_lock`; task fields come only from `fields_for_task`. `directory_record_lock(path, *, wait)` checks after the `flock` that the locked fd still names the path (`st_dev`, `st_ino`), tries at most three times, then raises `ScratchError`.
- Closed results: `read_record` returns `Registered | NotRegistered | UnreadableRecord(reason)`; `lifetime` returns `Live(why) | Ended(evidence) | Unknown(reason)`. A sweep removes only `Ended`; the dry run names each `Unknown` with its repair reason.
- Lifetime rules: an open phase keeps a copy at any age; the recorder's completed, stopped and error states end it. `landed:<unit>/<branch>` ends when `origin/<branch>` is an ancestor of the land target in the refs of the record's `source_path` (no fetch) and the copy has no unsaved work; the 7-day no-change rule then applies. A parent stays while a live record borrows from it.
- Busy mark: holders take a shared `flock` on `.claude-busy` while they run; `tmp_sweep.py`, scratch end and the `sweep.py` floor sweep try a non-blocking exclusive `flock` and skip the copy when it fails. `removal_lock` creates the mark; `judgment_lock` (dry runs, scans) does not. `merge_gate.py --checkout <copy>` holds the shared lock through `scratch.copy_lock`.
- Removal writes a certificate `.<copy>.scratch-removal.json` in the pair folder with the copy identity, so a stopped removal finishes on a rerun and never removes a new copy at the same path.
- `scratch.py hand-over PATH --to UNIT --task TASK [--run-dir RUN_DIR] [--land-target LAND_TARGET]` replaces owner and task fields in one record write and keeps the path. A phase record handed to a `landed:` task drops its phase fields.

**Files:**
- `scripts/lib/scratch.py` — copies root, record, lifetime, locks, removal certificates, `hand-over`.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/folder_audit.py` — kept root and a judgment per copy.
- `scripts/lint/sweep.py` — busy lock in the floor sweep.
- `scripts/hooks/pre-tool-use-scratch-guard.py` — copies root allowed.
- `scripts/production/merge_gate.py` — `--checkout` shared busy lock.
- `docs/as-built/build-targets.md` — copies root, record and lifetime.

**Binds later work:** every clone under the copies root registers with `scratch.register_copy`, and every record change goes through `update_record`. The busy mark is per copy: a lock on a shared build folder does not take `.claude-busy` from inside a copy. Admission and token waits stay inside the copy lock (Python test gates take a place in the shared admission queue).

**Gotchas:** a `flock` on a directory fd does not prove the path still names that directory; every record lock rechecks dev/ino. A second record writer reopens the record races.

**Ruled out:** a rename on hand-over (it breaks child `objects/info/alternates` and the merge gate's path-keyed push-recovery records); trust in a locked fd without a path identity check.

### Phase 22 — A stopped hold release finishes when it runs again  · status: done

#### As-built

- `release_cycle` saves the last holder's release intent atomically in `BUILD_HOLD_RELEASE_DIR/pending-release.json` (`save_release_intent`) before it removes the holder file. When a later release saves its intent, `retain_release_record` first moves an earlier pending record to `releases/<release-id>/`, so its notices stay.
- `release` and `release --resume` with no hold in force drain each pending record on its own under `release.lock` (`finish_release_intent`). A record whose notice or broadcast fails stays pending, and the drain goes on to the next.
- A notice outcome is saved after its sender returns, so a stopped send is retried: notices are at-least-once. A release without a usable cycle keeps its broadcast pending until the broadcast returns.
- A drained record becomes the one completed receipt for its holder key, `receipts/<holder-key>/completed-release.json`, replaced in place (`complete_release_record`). `recorded_releases()` moves older receipts there as it reads.
- Live holder files take precedence over receipts. A finished release prints `hold is already released.` even after a new cycle opens, then the details of any record it drained, `Not reached, tell them yourself` lines included.

**Files:**
- `scripts/build_hold/build_hold.py` — release intent, drain, receipts.
- `scripts/build_hold/test_build_hold.py` — fault-injection rerun tests.
- `docs/as-built/build-memory-admission.md` — the release intent paragraph and notice outcome timing in its build-hold part.

**Binds later work:** `BUILD_HOLD_RELEASE_DIR` (`pending-release.json`, `releases/`, `receipts/`) is shared build state that only the installed copy writes (Shared build state is written only by the installed copy); receipt migration in `recorded_releases()` is a write. `status` does not show a pending release record yet; Hold status names unsent release notices adds it.

**Gotchas:** a session can receive a release notice twice. `recorded_releases()` migrates receipts as it reads, so a read-only reader must not call it.

**Ruled out:** at-most-once notices (a lost notice is worse than a repeated one); one receipt per release (the count grows without bound).

### Phase 23 — Hold status names unsent release notices  · status: todo
**Depends on:** Phase 22 (same file, `scripts/build_hold/build_hold.py`). Runs beside Phase 16 (no shared files). Merge `origin/builds` first.

**Approved:** resources-unit 2026-10-30, follow-up of Phase 22 (architect review: status hides an unfinished release); builds showrunner told.

#### Work Order

**Goal:** `/build:hold status` (`build_hold.py status`) names each pending release record, its unsent recipients and the command that finishes it, also when no holder file is present.

**Spec:**

1. **Today.** The `status` branch of `main` (`build_hold.py` :1643-1659) prints `no build hold` when `read_holders` returns `NoHolders`, and reads no release record. A release that stopped after its last holder file went (Phase 22) leaves `BUILD_HOLD_RELEASE_DIR/pending-release.json` or `releases/<id>/pending-release.json`, and status hides it.
2. **One reader, read-only.** Add `pending_release_paths()` in `build_hold.py`: the list `release_directory() / "pending-release.json"` plus `sorted((release_directory() / "releases").glob("*/pending-release.json"))`, which `recorded_releases()` (:1105) builds inline today. `recorded_releases()` and the new status reader both call it, and both parse each file with `read_release_intent()` (:1039): no second parser and no second path list. The status reader never calls `recorded_releases()`, because that function moves old receipts (`complete_release_record`) during lookup. It takes no `release_lock()` (that creates the folder and the lock file); it reads as status reads the cycle today. It writes, moves and deletes nothing.
3. **Unsent recipients.** For a `cycle` record, read its cycle with `read_hold_cycle()` (:949; it validates and writes nothing) at `release_directory() / <cycle_id> / "cycle.json"`. A recipient in the record's `recipients` is unsent when its cycle entry is missing, `NotAttempted` or `DeliveryFailed` (the ones a rerun sends). For a `broadcast` record, the recipients are every session.
4. **Output.** After the holder lines, or after `no build hold`, one line per pending record: `release pending: <holder name>, released <HH:MM zone> - unsent: <name>, <name>; run /build:hold release to finish it` (`unsent: every session (broadcast)` for a broadcast; zone from `user_zone()`, as the holder lines). A record or its cycle that cannot be read is named, never hidden: `release pending: <path> could not be read: <error>; /build:hold release sets it aside` for the record, and the same form naming the cycle path for a cycle. No pending record prints no line.
5. **Tests** in `scripts/build_hold/test_build_hold.py`, with private `BUILD_HOLD_DIR` and `BUILD_HOLD_RELEASE_DIR` fixtures (`build_hold_test_support.py`): a current `pending-release.json` with one unsent and one delivered recipient, and no holder file, prints `no build hold` and one pending line naming only the unsent recipient and `/build:hold release`; a retained `releases/<id>/pending-release.json` beside a live holder prints its own line; a broadcast record; an unreadable record and an unreadable cycle each print their path and error; a completed receipt prints nothing; status changes no file (a snapshot of every path, size and mtime under both dirs before and after is equal, old receipts under `releases/*/completed-release.json` included); `recorded_releases()` still reads the same records through `pending_release_paths()`.
6. **Docs.** `docs/as-built/build-memory-admission.md`, Build hold: the `status` table row and the Release intent paragraph name the pending lines.

**Constraints from prior phases:**
- Phase 22: a pending record is saved before the last holder file goes; a new hold moves an earlier one to `releases/<id>/`; a drained record becomes `receipts/<holder-key>/completed-release.json`. A send's outcome is saved after the sender returns, so a rerun retries `NotAttempted` and `DeliveryFailed` entries.
- Phase 8 (later) guards writes at `release_lock()`; this reader takes no lock, so the guard does not refuse it from a checkout copy.

**Files:**
- `scripts/build_hold/build_hold.py` — Specs 2–4.
- `scripts/build_hold/test_build_hold.py` — Spec 5.
- `docs/as-built/build-memory-admission.md` — Spec 6, the Build hold section.

**Workers:** `1 writer`

**Acceptance gate:**
- The build-hold tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- Change-Review line: hold status names a stopped release and how to finish it | on natedev, in a Claude session with no build hold, type `/build:hold status` | it prints `no build hold`, and after a stopped release a `release pending:` line names the unsent sessions and `/build:hold release`.

### Phase 24 — A build's promise comes from builds of the same shape and holds until the build ends  · status: todo
**Depends on:** runs beside Phase 16 now (builds showrunner 2026-10-30: a wrong-result fix starts at once). Part A (new files only: the shape module, its tests, the replay) runs now. Part B (`scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`, the as-built doc) waits until Phase 16 checkpoints, then builds on 16's tree. Merge `origin/builds` first. Global order: 21 → 16 → 25 → **24** → 26 → 20 → 4 → 11 → 17 → 18 → 19 → 8.

**Approved:** builds showrunner 2026-10-30 05:2x EDT, design accepted as written (wrong-result defect, fixed at the cause in the gate).

#### Work Order

**Goal:** an admitted build step has its whole peak free for its whole life. Its promise comes only from earlier builds of the same shape, and the gate keeps that promise free until the step ends.

**Measured (2026-10-30, natedev, live code `~/.claude` at `bc4ebeb1`):**
- The incident: `verify.sh build hana --bin hana --release --features bevy/trace_chrome` (mesh run `20261030T031418`, worker `tool-based-ui-frame-time-frame-cost-test`) got a 10.9 GiB promise. Its `hana` bin rustc reached 14,720 MiB RSS, and earlyoom stopped it at 05:09:52 EDT after 86 min.
- Cause 1, the key: `expected_peak` keys history by host, repository, step and test scope only. `anon_peaks.jsonl` rows carry no argv, profile or features. The 58 hana `build` rows in 14 days are mostly dev builds near 3 GiB; their p90 is 10.86 GiB, set by the two release rows (10.86 and 12.35 GiB). So a release + `trace_chrome` build is promised from a pool of lighter builds.
- Cause 2, thin history: the index (`steps.argv`) has 8 hana release builds since 2026-10-16, peaks 10.88–13.01 GiB (2 with `trace_chrome`: 12.02 and 13.01). The failed run went 13% above the heaviest of them, so no history rule alone would have covered it.
- Cause 3, the running charge: `build_charge` charges a running step `max(ramp_promise, bytes, observed_peak, unknown_peak_promise)`, and the ramp promise ends after `RAMP_SECONDS` (120 s). After that, a build that sits low for most of its life and peaks in its last crate is charged its current memory, and the gate admits other steps into the room its peak needs.

**Spec:**

1. **Replay first** (no queue model: it compares each promise with the step's own peak, so it does not need Phase 20's calibration). Over 14 days of natedev `build` steps (index `steps`, with argv): for each step, the promise today's `expected_peak` gives from the history before it, and the promise of Spec 3; count steps above each promise, and the excess (p50, max, GiB), by shape. Also the cost, per shape and in total: the room the new rule holds beyond today's, in GiB-hours, and the most extra room it holds at one time (from the steps' `started_at`/`ended_at` overlap). Give two figures for each: the promise increase (new promise − today's promise, held for the step's life), and the upper bound (the new promise held for the step's life − today's 120 s ramp hold). Save the script in the run folder (`phases/24/`), the table in the as-built doc in Part B. If the extra room would make other steps wait much longer, the numbers go to the showrunner before Part B lands.
2. **The shape** (Part A, new module `scripts/lint/build_shape.py`). `build_shape(argv) -> BuildShape`, a frozen value with a stable text key: the profile (`release` for `--release`, the name for `--profile <name>`, else `dev`), the sorted feature set (`--features a,b`, `--features=a,b`, repeated flags; `--all-features` as its own value; `--no-default-features` as its own value), and the target selector (`--bin <name>`, `--example <name>`, `--lib`, else `all`). The module also holds the index join: `index_shapes(index_path, host, repository, step, cutoff, before) -> dict[(call_id, step), BuildShape]`, read-only, from `steps.argv`, `steps.call_id` and `steps.step` (Phase 11 Spec 4's join). In Part B, `check` writes the shape in the reservation row and `release` copies it to the `anon_peaks.jsonl` row; old rows with no shape take it from the join; a row with no match has no shape and counts for no shape.
3. **The promise for a `build` step** (option C, builds showrunner 2026-10-30, after the options replay in the run folder `phases/24/options/`). Only rows of the same repository and the same shape count. With 10 or more rows over `HISTORY_DAYS`: their p90; with 1 to 9 rows: their largest. With no same-shape rows: the larger of `FALLBACK` and the heaviest same-repository `build` row of the same profile. A `release`-profile shape's promise is that value times `RELEASE_HEADROOM` = 1.314, the smallest factor under which no replayed release step with earlier same-shape rows went above its promise (comment the constant with that measurement); dev shapes take no factor. No promise goes above `BUILD_BUDGET_BYTES` (21 GiB): cap it there; a build that needs more is a separate question for the showrunner. Rows of another shape never fill a tier. **Killed or over-promise builds:** a `build` step that is killed (earlyoom, a signal) or whose observed peak passes its promise enters the same-shape history at the peak it reached, so the next build of that shape is promised at least that peak (the 05:09 incident shape: 15.76 GiB × 1.314 ≈ 20.7 GiB, under the cap). Replay of option C over 126 builds, capped at 21 GiB: +78.2 GiB-hours upper bound and +22.5 GiB-hours promise increase; the most extra room at one time 17.4 GiB (upper bound) and 14.9 GiB (increase); no promise above 21 GiB; the incident build gets 15.76 GiB (it needed 14.4). The first design (largest same-shape row × 4.20 for every build) cost +146.7 GiB-hours and 27.8 GiB at one time, with 77 promises above 21 GiB before the cap.
4. **The running charge for a release `build` step.** The promise stays charged until the step ends: `max(need, current memory, observed peak)`. The 120 s ramp no longer ends it. Dev-profile `build` steps and all other steps keep today's charge (nextest stays Phase 15's; `check`, `clippy` and `doc` as today).
5. **Tests.** Part A, in `scripts/lint/test_build_shape.py`: the shape from each argv form; the index join on a temporary SQLite index (a row with no argv or no `call_id` gives no shape). Part B, in `scripts/lint/test_memory_admit.py`: a release build is promised only from release rows of the same features and target; p90 with 10 or more rows and the largest with fewer; no same-shape rows gives the profile fallback, never the dev pool; the 1.314 factor applies to release shapes only; no promise passes 21 GiB; an old row takes its shape from the index join, and a row with no join counts for no shape; a release `build` step past 120 s stays charged its need while its memory is lower; a dev `build` step and a nextest step past 120 s keep today's charge; a killed `build` step, and one whose peak passed its promise, add a same-shape row at the peak reached, and the next same-shape promise is at least that peak times the factor. Every test fakes both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the shape, the promise rule, the running charge, and the replay table.

**Phase 15 check (builds asked):** Phase 15's two failed arms (history max, max + 10%) lowered a nextest step's charge after its compile to its test-phase history, and failed because test phases went above that lower charge. This phase lowers no charge and does not touch nextest or the after-compile charge. It raises the promise of `build` steps only, from same-shape history, and Spec 1 measures its cost before it lands.

**Constraints from prior phases:**
- Phase 12: estimates run outside the admission lock; the observed peak with its `.wave` decay stays for steps other than `build`.
- Phase 11 (later) splits compile and no-compile runs and keyed and unkeyed rows inside each shape; Phase 18 (later) splits on build changes inside each shape.
- Phase 16: `merge-gate` rows stay their own class.

**Files:**
- `scripts/lint/build_shape.py` — new — Spec 2 (Part A).
- `scripts/lint/test_build_shape.py` — new — Spec 5 (Part A).
- `scripts/lint/memory_admit.py` — Specs 2–4 (Part B).
- `scripts/lint/test_memory_admit.py` — Spec 5 (Part B).
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6 (Part B).

The replay script (Spec 1) lives in the run folder, not in the repository.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The replay: no `build` step with same-shape history goes above its new promise; the incident's argv gets a promise at or above 14.4 GiB (14,720 MiB). The table, the GiB-hours cost and the most extra room held at one time go in the checkpoint notice.
- Change-Review line: a release build is promised from release builds and keeps its promise until it ends | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh build hana --bin hana --release --features bevy/trace_chrome` and read its reservation in `~/.local/state/buildlog/admission` | its `need` is at or above 14.4 GiB with the shape in the row, and the build-why charge stays at the need while its memory is lower.

### Phase 25 — Build-why names when a recovery spacing hold ends  · status: todo
**Depends on:** Phase 16 (same file, `memory_admit.py`); report-unit's build-why change (`explain_queue`, `head_after_steps_end`, `build_why.py`), on `origin/builds` since `228b45ae`. Merge `origin/builds` first. Global order: 21 → 16 → **25** → 24 → 26 → 20 → 4 → 11 → 17 → 18 → 19 → 8.

**Approved:** builds showrunner 2026-10-30, a text fix with its test, after Phase 16 (the 158 min head wait question: the hold was right, the text was wrong).

#### Work Order

**Goal:** when the only thing that holds the first waiting step is the recovery spacing (one start per 30 s for 120 s after a pressure hold), build-why says the time it starts, not that every running step must end.

**Measured (2026-10-30, `origin/builds` `228b45ae`):**
- `head_after_steps_end` (`memory_admit.py` :2025) applies `queue_decision` with the snapshot's `host.pressure`. A recovery spacing hold is a `MemoryPressure` with `next_start_at` set: a timed hold that no step's end lifts. So with every running step ended the result is still `HeadStillHeld(causes=("pressure",))`.
- `release_text` (`scripts/build_queue/build_why.py` :131-145) then prints `all running steps ending still leaves pressure holding the head`. On 2026-10-30 06:28 EDT the head started 154 s later on the timer, with 25.9 GiB free.

**Spec:**

1. **A closed result for the timed hold.** Add `HeadStartsAt(at: float)` to the `HeadAfterEnd` union. `head_after_steps_end` returns it when the decision holds only for a `MemoryPressure` with `next_start_at` (budget and free checks pass, so `causes` would be `("pressure",)` alone); `at` is `next_start_at`. A pressure hold with no `next_start_at` (a real `full avg10` or swap hold) stays `HeadStillHeld` with `pressure` in its causes. `smallest_head_release` returns `HeadStartsAt` when the snapshot's own result is `HeadStartsAt`: no step needs to end.
2. **Text.** `release_text` prints `no running step needs to end; recovery spacing starts it at <HH:MM:SS zone>` for `HeadStartsAt` (zone as the header). All other texts stay as they are.
3. **Tests.** In `scripts/lint/test_memory_admit.py`: a snapshot held only by recovery spacing gives `HeadStartsAt(next_start_at)` from `head_after_steps_end` and from `smallest_head_release`; a real pressure hold (no `next_start_at`) still gives `HeadStillHeld` with `pressure`; recovery spacing plus a failed budget check gives `HeadStillHeld` with `build budget` and `pressure`. In `scripts/build_queue/test_build_why.py`: the recovery spacing case prints the start time and not `still leaves pressure`; the existing `test_release_keeps_the_snapshot_limits_and_reports_a_pressure_hold` passes unchanged. Fake both memory readings.
4. **Docs.** `docs/as-built/build-memory-admission.md`: the build-why paragraph names the recovery spacing text.

**Constraints from prior phases:**
- Phase 14 and report-unit's Phase 3: `head_after_steps_end` reads no files and uses admission's own `queue_decision`; this phase adds no second rule.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 1.
- `scripts/lint/test_memory_admit.py` — Spec 3.
- `scripts/build_queue/build_why.py` — Spec 2 (owner report-unit; trial-merge against `origin/builds-report`).
- `scripts/build_queue/test_build_why.py` — Spec 3 (owner report-unit).
- `docs/as-built/build-memory-admission.md` — Spec 4.

**Workers:** `1 writer`

**Acceptance gate:**
- The memory admission and build-why tests pass; `basedpyright` over the four Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- Change-Review line: build-why says when recovery spacing starts the first waiting step | on natedev, during the 120 s after a pressure hold ends, type `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` | the release line says no running step needs to end and names the start time.

### Phase 26 — The memory queue starts steps in queue order and records when a step became first  · status: todo
**Depends on:** Phase 24 Part B (same file, `memory_admit.py`). Runs before Phase 20, whose single head policy (Spec 2) then reads these fields. Merge `origin/builds` first. Global order: 21 → 16 → 25 → 24 → **26** → 20 → 4 → 11 → 17 → 18 → 19 → 8.

**Approved:** builds showrunner 2026-10-30 (two wrong results from the 158 min head wait; `head_since` and the token record for report-unit's Phase 4e). Who may bump is a user question the showrunner takes; this phase does not change it.

#### Work Order

**Goal:** in a backfill window the steps that fit start in queue order; a bump of a step near the front changes nothing; each waiting row says when it became first; a step that misses the CPU token records when it gave back its place in memory and when it got the token.

**Measured (2026-10-30, 03:50–06:28 EDT, natedev; `S/pressure_followup_report.md`):**
- The head waited 158 min. Causes: 88 queue bumps, each one restarting a bumped head's 600 s strict window (`bump_waiter` sets `priority_since = now`, `memory_admit.py` :2144), and backfill windows where younger steps started in the order they polled, not their queue order.
- `queue_decision` admits a younger step in a backfill window on its own budget and memory checks; nothing checks whether an older waiter that also fits is waiting.
- `verify.sh` `buildlog_wait_for_memory` (:2613-2648): when `try_token_now` fails, the step releases its memory reservation (`buildlog_release_memory 0`), waits in `acquire_token`, then waits for memory again with `BUILDLOG_MEM_WAIT_SINCE` from its first wait. Only the token wait seconds go to `TOKEN_WAIT_FILE`; no record holds the two times.

**Spec:**

1. **Backfill in queue order.** In a backfill window, a younger step starts only if no older waiting step (head excluded, as today) fits by the same budget and memory checks at the same snapshot. Each poll decides from the queue snapshot under the lock, so the first fitting step in queue order is the one that starts, whichever process polls first. A step that fits but has an older fitting step ahead holds with a reason that names it (`BehindOlderFit`, a closed variant beside `BehindHead`; the queue view, `explain_queue` and the shell wait text show it).
2. **Bump near the front.** `bump_waiter` on a step at positions 1–3 writes nothing (no row change, no `queue_bump` record) and returns a closed result `AlreadyNearFront(step, position)` beside `Bumped(step)`; `/build:queue bump` prints `already at position <N>; nothing changed`. The head's `priority_since` and window stay as they were.
3. **`head_since` on the waiter row.** The admission pass that finds a row first in queue order writes `head_since` (epoch seconds) into that row under the queue lock when the row has none; a row that is no longer first loses it (a bump of another step ahead moves it back). A step that gives back its reservation for the token (Spec 4) and returns to the queue with its first wait time keeps its `head_since` if it is first again at once. The queue view and `explain_queue` expose it as a typed field; `BehindHead.head_since` reads it instead of the head's queue time.
4. **Token record.** When a step misses the token and gives back its memory reservation, `verify.sh` records `token_left_at` (when it released the reservation) and `token_back_at` (when `acquire_token` returned). It writes them through one new `memory_admit.py` command (no second writer of admission files) as one JSON line in `${BUILDLOG_DIR}/admission/token_gaps.jsonl`: `session_id`, `step`, `pid`, `start_time`, `token_left_at`, `token_back_at`. A step that leaves the queue during the second wait still writes its line.
5. **Tests.** In `scripts/lint/test_memory_admit.py`: with three waiters where the second and third fit in a backfill window, a poll by the third holds it and a poll by the second starts it (Spec 1); a bump of positions 1, 2 and 3 leaves every row and the head window as they were and returns `AlreadyNearFront`, a bump of position 4 moves it as today (Spec 2); `head_since` is set when a row becomes first, cleared when a bump moves it back, set again when it is first again, and kept across a token give-back (Spec 3); the token command writes one line with both times (Spec 4). In the verify.sh tests: a step that misses the token writes one `token_gaps.jsonl` line with `token_left_at <= token_back_at`. Fake both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the Waiters paragraph (queue order in backfill, `head_since`), the bump paragraph and the token record.

**Constraints from prior phases:**
- Phase 10: the blocker lookup reuses `decide` and `queue_decision`; no second rule.
- Phase 20 (later) makes one head policy function; this phase's backfill rule and `head_since` go in `queue_decision` and the queue pass, so Phase 20 moves them with the rest.
- Report-unit's Phase 4e reads `head_since`, `token_left_at` and `token_back_at` by these names; tell report-unit at the checkpoint.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 1–4.
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `scripts/direct/verify.sh` — Spec 4 (owner verify-unit, natedev production; trial-merge against `origin/natedev-verify`).
- `scripts/direct/test_verify_memory_wait.py` — Spec 5, the verify.sh test (new file).
- `scripts/build_queue/build_queue.py` — Spec 2's bump text (owner report-unit; trial-merge against `origin/builds-report`).
- `scripts/build_queue/test_build_queue.py` — the bump text test (owner report-unit).
- `docs/as-built/build-memory-admission.md` — Spec 6.

**Workers:** `1 writer, 1 tester`

**Acceptance gate:**
- The memory admission, build-queue and new verify.sh tests pass; `basedpyright` over the changed Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- Change-Review line: steps that fit start in queue order and a bump near the front changes nothing | on natedev, type `/build:queue` while steps wait, then `/build:queue bump 2` | rows start top-down in a backfill window, the bump prints `already at position 2; nothing changed`, and each first row shows when it became first.
