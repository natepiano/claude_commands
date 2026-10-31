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
- **Lint:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <files>` from the worktree root. `<files>` is what the acceptance gate names; with no list, `scripts/lint scripts/hooks scripts/build_hold scripts/production` plus the directory of every other changed module. The interpreter is the merge gate's type-check venv (`merge_gate.py` `typecheck_python()` :574, pinned pillow and fake-bpy-module), so `bpy` and `PIL` resolve; a bare `basedpyright` reports false import errors. If the venv is absent, a merge gate run builds it. Every `basedpyright` line in an acceptance gate below means this form. Pass is its `0 errors, 0 warnings, 0 notes` line (exit status says nothing). `bash -n scripts/lint/memory_gate.sh`.
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

### Phase 4 — Short-lived clones share one build folder per repository  · status: done

#### As-built

- The per-repository shared Cargo build folder (`scratch.shared_build_folder`) was built here and removed by Phase 34: the builds showrunner reversed its 02:42 word on 2026-10-30, because the verify-copies measurement (case D) spent 68.7 of 69.0 s on the shared folder's build lock, so `copies.py` seeds each copy's own build folder. A copy record no longer writes `shared_target`; old records still load.
- `scratch.mark_cargo_cache(target)` writes `CACHEDIR.TAG` and refreshes the one use stamp. `scratch.USE_STAMP`, `CACHE_TAG`, `STYLE_TARGET_NAME` and `STYLE_LOCK_NAME` are the only spellings of these names.
- `style_sync.py` keeps its one `style-sync-target` per repository past task end and keeps its `sync.lock`.
- The sweeps treat each target as one unit with its own last use. `sweep.lock_trees(trees) -> TargetLocksAcquired(held) | TargetLockBusy(path)` takes Cargo's locks and, for a style target, `sync.lock`, without a wait. The busy check expands Cargo `build.build-dir` templates (`{workspace-root}`, `{cargo-cache-home}`, `{workspace-path-hash}`); an unknown template keeps the whole Cargo cache busy.
- `record.sweep_trim(folder: str, bytes_freed: int, extent: Literal["whole", "partial"], cause: Literal["budget take", "floor take"], at: float) -> None` appends one `kind` `"sweep_trim"` build-log row (`host`, `at`, `folder`, `bytes_freed`, `extent`, `cause`). `sweep.record_sweep_removal` is its one call site; the budget take and the floor take call it once per target take. `bytes_freed` counts only `ConfirmedDeletion` bytes. `extent` is `whole` when the take leaves no build unit or incremental dir of the target. A dry run writes no row. A failed write prints to stderr and the sweep goes on.
- Each take is write-ahead: a `TrimTake` intent under the floor record's `trims/` folder lasts until its row is in the build log, and `recover_pending_trims` publishes a stopped take on the next run.
- `rebuilds` reads trims from `sweep_trim` rows through the same build-log read as its steps, and merges them with the journal and legacy `removals.jsonl` rows. Nothing writes `removals.jsonl`.
- Rebuild attribution still keys steps by workspace. The shared-folder attribution through the build step row's `target_dir` and `repo_identity`, and `repo_identity` on the trim row, do not exist yet.

**Files:**
- `scripts/lib/scratch.py` — `mark_cargo_cache` and the shared cache names (the shared folder mapping was removed by Phase 34).
- `scripts/style/style_sync.py` — the retained shared style target.
- `scripts/lint/sweep.py` — one-target sweep, `lock_trees`, Cargo build-dir templates, `TrimTake`, `recover_pending_trims`, `record_sweep_removal`, the `rebuilds` trim reader.
- `scripts/buildlog/record.py` — `sweep_trim`.
- `docs/as-built/build-targets.md` — the trim row (the shared folder text was removed by Phase 34).
- `scripts/lib/test_scratch.py`, `scripts/style/test_style_sync.py`, `scripts/lint/test_sweep.py` — tests.

**Binds later work:** the `sweep_trim` row has no `repo_identity` until the step-row attribution reader lands. Trim recovery accounting extends `TrimTake.log_offset`, `recover_pending_trims` and `store.read_last_whole_row_end` / `store.last_whole_row_end`; a stop between `reclaim` and the row append, and a cut-short row, are not yet covered.

**Gotchas:**
- `ConfirmedDeletion.removed` counts removals apart from bytes: a delete of a hard-linked file frees 0 bytes but is still a trim.
- `{workspace-path-hash}` follows Cargo's own hash; a golden test pins it.
- `target_dirs` must include the retained style target under `.git`, and the floor must not take it while `sync.lock` is held.

**Ruled out:** a second build-log appender for trims; a journal change for failed groups.

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
- Argv parsing finds the entry point past interpreter options, Node runtime options included. Phase 28 closed the `node --redirect-warnings` gap: space-form value options skip their value.
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
   - **One place.** Extend the existing guard, `scripts/build_hold/override_guard.py` (`refusal()` :63, `refuse_overrides()` :89, the account-database home `user_home()` :28, and Phase 36's `is_installed_file()` :42, which the new function reuses for its installed-copy test). Add one function there that takes the calling script, its installed relative path and the directory about to change, and returns allowed or a refusal line. Write no second home lookup or installed-path check anywhere.
   - **Refuse before any change, imported calls included.** A CLI-only check misses two boundaries: Phase 16's merge gate calls `memory_admit.check` in its own process, and `recorded_releases()` (`build_hold.py` :1184) changes state during a lookup, before `release_cycle` runs. So call the guard where every change passes: `memory_admit.admission_dir()` (:2045; `check`, `admit_head`, `release`, `set_width` and `cancel_waiter` all get their directory from it; `bump_waiter` refuses every caller and writes nothing since Phase 30), and `build_hold.release_lock()` (:939; every holder-file write, release-record write and receipt move runs under it, `write_hold` and `release_hold` included), which checks the holder dir and the release dir. Read-only paths (`queue_view`, which reads `store.root() / "admission"` directly, and `status`) change nothing and stay allowed. The guard raises one typed error; the CLIs turn it into exit 2 with the named reason, and an imported caller gets the error (the merge gate stops on it, Phase 16 Spec 4).
   - **Imported recovery goes through the lock.** `recorded_releases()` (:1184) only calls `release_records_for_change()` (:1188), and that function does not take `release_lock()` itself. It moves old receipts (`complete_release_record` :1112) and sets damaged pending intents aside (`set_aside_damaged_record` :1027). Today its callers hold the lock (`recorded_release_holder` :1220, `release_cycle` :1496), but an imported call does not have to, so a lock-only guard misses it. Make the lock the one mutation boundary: `release_lock()` runs the guard, then yields a typed `ReleaseLockHeld` value. Every helper that changes release state takes that value as a parameter: `release_records_for_change`, `complete_release_record`, `set_aside_damaged_record`, `read_cycle_for_change` (:1039), `save_cycle` (:1057) and `finish_recorded_notices` (:1274). `recorded_releases()` takes it too, or goes, because its callers already hold the lock. No helper takes the flock again: a second `flock` on `release.lock` from the same process blocks.
   - **The admission lock binds the directory it changes.** `release` (:3017) takes its lock directory from `root` but reads and finalizes the given `path`, and `finalize_reservation` (:2953) writes history under `path.parent`, so a guard on `root` alone does not prove the write is private. One guarded admission-lock context validates the actual mutation directory and yields an `AdmissionLockHeld` value bound to it. Reservation updates, finalization, recovery, override and queue-event writers require that value and validate their paths against it; they never take the lock again.
   - Each directory is judged on its own: a fake `BUILD_HOLD_DIR` with the real release dir refuses at the release dir, and a real `BUILD_HOLD_DIR` with a fake release dir refuses at the holder dir.
2. **Queue rows carry separate fields.** Waiters already carry `repository_name` (`Waiter` :435, written by `check` :2416) beside `repo_identity` and a bare `step`. Reuse the waiter's existing `repository_name` and bare `step`, and expose the reservation's existing `repo` through the same queue-reader model. Do not add a parallel `repository` field or reconstruct a combined label; no prefix strip exists anywhere. `build_queue.py` shows separate Repository and Step cells for waiting and running rows alike (queue-unit's file; its one display change only).
3. **Callers run the installed copy.**
   - `memory_gate.sh`: `BUILDLOG_MEM_ADMIT_SCRIPT="${BUILDLOG_MEM_ADMIT_SCRIPT:-$HOME/.claude/scripts/lint/memory_admit.py}"`. `invoke.sh:89` sources `$HOME/.claude/scripts/lint/memory_gate.sh`.
   - `verify.sh`: one root, `SCRIPTS="$HOME/.claude/scripts"`, for `override_guard.sh`, `local_release_wait.py`, `mesh_runs.py`, `mac_idle.py` and the `build_targets` fallback. Tests fake `HOME` with a copied tree already; the `VERIFY_*_SCRIPT` overrides stay.
   - `merge_gate.py` reads holds only and already runs from `~/.claude` main (Delegation Context, Test). No change. The notice records this.
   - `top_level.py`, `retire_unit.py`: the `build_hold.py` path from `Path.home() / ".claude"`.
4. **The plan lines that teach a checkout run** (`docs/plans/builds-production.md:14`, `docs/plans/build-followups-worker-seats.md:48`) name `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --checkout <tree>`.
5. **Tests:** `scripts/build_hold/override_guard.py` already finds the real home through the account database and refuses fake state variables when the installed script runs. Keep that refusal. Routing tests copy scripts under a fake home; the checkout-write refusal is tested against an injected canonical-home and state fixture, never real coordination state. A checkout-copy `memory_admit.py` against the real-dir path refuses (exit 2, named reason) and against a fake dir writes; same for `build_hold.py`; a `.waiting` row has `repository_name` and a bare `step`, and no `repository` field; the queue view shows separate Repository and Step cells for waiting and running rows; a private root paired with a real-state reservation path, and direct imported mutation calls, refuse before any write; `verify.sh` and `memory_gate.sh` resolve to `$HOME/.claude/scripts` under a fake `HOME`. Guard tests in `scripts/build_hold/test_override_guard.py`: fake holder dir with the real release dir, and the real holder dir with a fake release dir, each refuse and leave every file in the real dir unchanged; an imported `memory_admit.check` call from a checkout copy against the real admission dir raises before it writes a file; from a checkout copy with a real release dir, the recovery read refuses before it moves an old receipt and before it sets a damaged pending intent aside (every file in the release dir unchanged); the installed copy still migrates the receipt and sets the damaged intent aside; `status` stays read-only and prints the same lines with and without the change; the installed script's existing override refusal tests in that module pass unchanged. Fake memory readings throughout (Phase 1 helper).

**Constraints from prior phases:** Phase 22 added hold release state under `BUILD_HOLD_RELEASE_DIR`: `pending-release.json`, `releases/<id>/` and `receipts/<holder-key>/completed-release.json` (`docs/as-built/build-memory-admission.md`, Build hold). It is shared build state, so the installed copy is its only writer, as for the holder files and the cycle. Phase 16 makes the merge gate call `memory_admit.check` and `release` in its own process; the guard covers that call through `admission_dir()`. Phase 23's `status` reader of pending release records is read-only and takes no `release_lock()`, so the guard does not refuse it. Phase 23 also made release set damaged pending records aside and rebuild a damaged or missing cycle from the intent; those writes are the recovery that item 1 routes through `ReleaseLockHeld`. Phase 32 added `admit_head` (:525), which writes `admit-head.json` under `admission_dir()`. Phases 24, 26 and 20 add admission writers (the reservation finalizer, Phase 26's queue-place records and `queue_times.jsonl`; the user's 2026-10-31 amendment dropped the token give-back handoff); each gets its directory from `admission_dir()`, and Phase 18's estimate cache (`admission/build_marks.json`, written while `check` estimates, before it reaches `admission_dir()`) is one more guarded write: an imported checkout refuses before the cache changes, and git work stays outside the admission lock. So the one guard covers them.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/lint/test_memory_admit.py` — items 1–2, 3, 5.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — items 1, 5.
- `scripts/build_hold/override_guard.py`, `scripts/build_hold/test_override_guard.py` — item 1, the one guard function; item 5, the guard tests.
- `scripts/build_queue/build_queue.py`, `scripts/build_queue/test_build_queue.py` — item 2 (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/direct/verify.sh`, `scripts/lint/invoke.sh` — item 3 (owner verify-unit). The tester owns routing coverage in `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_nix_shell.py`, `scripts/lint/test_invoke_scope.py`, `scripts/message/test_top_level.py` and `scripts/production/test_retire_unit.py`, plus the admission, hold and queue tests named above. Cover the installed paths and retained overrides under a fake HOME and private coordination state.
- `scripts/production/merge_gate.py` — no change (item 3); the notice records why.
- `scripts/message/top_level.py`, `scripts/production/retire_unit.py` — item 3 (owners per natedev's Owners table, named in the notice).
- `docs/plans/builds-production.md`, `docs/plans/build-followups-worker-seats.md` — item 4.
- `docs/as-built/build-memory-admission.md` — the one-copy rule and the row fields.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–4 (`override_guard.py` and the guarded admission-lock context included) and the doc.
- `test` — item 5 (`test_override_guard.py` included).

**Acceptance gate:**
- The changed modules' tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`.
- These existing tests stay green unchanged, because each one guards an outcome a user sees: `test_full_queue_holds_gate_until_the_running_charge_is_released` (:155) and `test_admission_error_refuses_before_any_module_starts` (:224) in `scripts/production/test_merge_gate_suites_admission.py`; in `scripts/build_hold/test_build_hold.py`, `test_status_reports_recovery_without_changing_damaged_record` (:941), `test_status_names_unsent_release_after_last_holder_is_gone` (:1479), `test_release_sets_aside_malformed_current_intent_with_no_holder` (:1537), `test_release_recovers_recipient_notices_from_a_damaged_referenced_cycle` (:1575) and `test_status_names_unreadable_pending_records_and_cycles_without_hiding_them` (:1656).
- Live, after install: every `.waiting` row in `~/.local/state/buildlog/admission` has `repository_name` and a bare `step` within one held poll; a run of a checkout copy's `memory_admit.py` against the real dir exits 2 with the named refusal.
- The notice has `also touches` lines for verify.sh and invoke.sh (verify-unit, against `origin/natedev-verify` and `origin/natedev`), the hold callers (their owners, against `origin/natedev`), and build_queue.py (queue-unit, against `origin/builds-queue`); it confirms no read-side prefix strip remains in `memory_admit.py` or `build_queue.py`.
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
**Depends on:** Phase 10 and Phase 12 (same file, `memory_admit.py`); verify-unit's Phase 4 (branch `natedev-verify-memory`). Start after that merge reaches `origin/builds`, and merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/27 (queue-unit) → 39 → 20 → **11** → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11. Phases 15 and 27 moved to queue-unit: start after queue-unit's checkpoints for them reach `origin/builds`, and merge it first. Phase 27's gate cap source and the removal of `gate_estimate` come from those checkpoints.

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
3. A repository with 1 to 4 own runs does not take the all-repository pool p90. Use the largest of its own runs, or the pool when that is lower. A repository with no own runs keeps today's rule. This rule does not apply to `build` steps: Phase 24 Spec 3 owns their promise.
4. A promise does not mix compile runs and no-compile runs (showrunner 2026-10-29 17:11 EDT, from the user's rule). Spec 4 applies to every step. A history row is a compile run when its index step has `crates_compiled > 0` (index rows: their own column). Anon rows get it through the one index join in `scripts/lint/build_shape.py`: `index_shapes(index_path, host, step, cutoff, before)` (:89, Phase 24) joins anon rows to `steps` by host, `call_id` and step name, with no repository argument, because `steps.repo` is the checkout folder; it drops rows without usable argv, which fits a shape lookup only. Widen the existing host/call-id/step join into one frozen step-evidence value whose shape and compile count have independent known/unknown states, and rename it to say what it returns. Missing argv must not erase a recorded compile count, and a stored shape must not bypass compile classification; keep the no-repository-argument contract and repository filtering at the history-row boundary. Open no second `steps` join and parse no argv outside `build_shape` (:59). A row with no match counts as neither. The step being admitted is a compile run unless verify Phase 4's `is_fresh_test` proves it fresh. A compile run's promise comes only from earlier compile runs; for a filtered run the order is the same as `filtered_peak` today: the exact filter set, then the package's filtered runs, then the check/build peaks. The 1 GiB floor stays only for fresh runs and for a repository with no compile history; do not raise the fresh floor. Fresh runs keep verify Phase 4's `fresh_test_peak` unchanged. `expected_peak()` returns `min(filtered, full)` today, so a compile-run promise above the full-run estimate would not hold: the compile-run promise wins over that minimum, and a test covers a promise above the full estimate; the replay includes that case (Phase 3 architect review, F11). Comment each new constant with the measurement that set it, as the current constants are.
5. Tests in `scripts/lint/test_memory_admit.py`: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; a `build` step with 1–4 same-shape rows keeps Phase 24's same-shape promise, not Spec 3's lower pool; a compile run's promise uses only compile runs; an anon row with no `call_id` and step match counts as neither; a fresh run keeps the `fresh_test_peak` promise; a `merge-gate` row with no index match stays in `anon_peaks.jsonl` and goes into no Cargo step's compile or no-compile history; each test fakes both memory readings. In `scripts/lint/test_build_shape.py`: the widened join returns known shape with known count, known count with unknown shape (no usable argv), known shape with unknown count, and the unmatched-row state. The existing `BuildShapePromiseTests` (same-shape promise and failed-peak floor) and the `need_basis` codec and rendering tests pass unchanged.
6. Docs: `docs/as-built/build-memory-admission.md` gets the replay table before and after, and the new rules. The replay script lives in the session folder and is not committed.

**Constraints from prior phases:**
- Phase 33 (`5c9b01fd`): history rows are read only through `store.whole_rows(handle, start)` (`scripts/buildlog/store.py`); `anonymous_history_rows` (`memory_admit.py`) delegates its decoding to it. A row counts only when it ends in a newline; a cut-short, non-UTF-8 or non-object row is skipped. Add no second decode loop; keep the unfinished-JSON and split-UTF-8 consumer tests after any classification change.
- Phase 12: Phase 11 changes estimates (`need`) only. It keeps the running charge as Phase 12 built it: current memory, the ramp and unknown-peak promises, the observed peak with its `.wave` decay after 1500 s, the missing-`.wave` fallback to the `.max` mtime, the recovery start spacing, and the estimate-expiry checks after the lock.
- Phase 16: merge gates write `anon_peaks.jsonl` rows under step `merge-gate`, and those rows have no Cargo index step to join. Spec 4's "every step" classifier does not drop them as "neither": `merge-gate` rows stay a class of their own, and report-unit's merge-gate history rows stay written. Phase 27 sizes the gate row by its cap and removes `gate_estimate`, so no admission estimate reads `merge-gate` rows. The compile and no-compile split applies to Cargo steps only (Spec 5 tests it).
- Phase 24: `scripts/lint/build_shape.py` holds the one argv parser (`build_shape`) and the one anon-to-index join (`index_shapes`); Spec 4 widens that join and adds no other. For `build` steps, `build_expected_peak` (`memory_admit.py` :1312) keeps Phase 24's same-shape tiers (p90 with 10 or more rows, the largest with 1 to 9), failed-peak floor, release factor 1.314 (`RELEASE_HEADROOM` :89), the dev rule Phase 39 adopted (it replaced `DEV_HEADROOM`'s single 4.2034275337646125 factor; keep the adopted rule, never restore the factor) and 21 GiB cap. `ShapeFallback` (the profile fallback) has neither floor nor factor; feature-side filtering is already satisfied inside same-shape build tiers and remains necessary for profile fallback and other step histories. Determine release profile through `BuildShape.profile`, including `--profile release`, and test both release spellings. Specs 2 and 4 split rows inside each shape; they change none of those tiers, and Spec 3's lower pool never replaces a same-shape `build` promise.

**Files:**
- `scripts/lint/memory_admit.py` — items 2–4.
- `scripts/lint/build_shape.py` — item 4, the widened join.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py` — item 5.
- `docs/as-built/build-memory-admission.md` — items 1 and 6.

**Workers:** `1 writer + 1 tester`
- `impl` — `memory_admit.py`, `build_shape.py`, the as-built doc and the replay (run folder).
- `test` — `test_memory_admit.py` and `test_build_shape.py` (Spec 5).

**Acceptance gate:**
- The memory admission and build-shape tests pass; `basedpyright` over `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py`, `scripts/lint/test_memory_admit.py` and `scripts/lint/test_build_shape.py` reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
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

### Phase 15 — A nextest step's charge falls to its test-phase need when its tests start  · status: moved
**Moved:** to queue-unit (builds showrunner 2026-10-31): one owner for queue Phase 5's hub file and a shorter path to the cutover. No new work on it here.
**Depends on:** Phase 33 (its one anonymous-history reader feeds Spec 2, and its finalizer retry carries Spec 4's saved evidence; the reader moved from this phase to Phase 33) and Phase 26 (same file, `memory_admit.py`; 33 → 26 → 15 land one after another); report-unit's Phase 4c (the test-phase file `$BUILDLOG_PEAK.test-phase.json`, `test_phase_peak_bytes`, `record.read_test_phase`), merged on `origin/builds` (`f88d8012`) and in this branch at `31c3ee3e`. Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → **15**/27 (queue-unit) → 39 → 20 → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

**Approved:** builds showrunner 2026-10-30, option A: the narrow key (repository + scope + package + filters), margin × 1.2, a test for each case, acceptance at most 2.3 % of test phases above their charge per repository on 4c data, and a 24 h check after the merge (steps above their charge, `mem_wait_s` against the 24 h before, earlyoom kills).

#### Work Order

**Goal:** when a nextest step's tests start, its charge falls from its compile peak to `max(test-phase peak so far, N) × 1.2`, where N is the p90 test-phase peak of earlier runs of the same family. A step with no N keeps today's charge.

**Measured (code at `bc36e49f`; Phases 24 and 26 move the line numbers, so find each by its name):**
- Need: `check` (`memory_admit.py` :1582) stores the p90 whole-step anon peak from `expected_peak` (:845) or `filtered_peak` (:943) as the reservation's `need`.
- Running charge: `charge_basis` (:797) takes the largest of `UsingNow`, `RampPromise`, `PeakSoFar` and `PeakFileUnreadable`, and `decide` (:771) sums it. `reservation_memory` (:1155) reads `.max` and `.wave` (`"<peak> <epoch>"`), which the sampler `buildlog_sample_anon` (`invoke.sh` :172) writes. The peak decays `PEAK_DECAY_SECONDS` (:78, 1500 s) after the last sample at or above half the peak. So a nextest step keeps its compile peak far into its test phase. Live, 2026-10-30 07:51 EDT: a widget nextest step had a charge of 11.2 GiB while its test binary used 0.38 GiB.
- Admission has no compile-end signal. Report-unit's Phase 4c adds one. The same sampler writes `$BUILDLOG_PEAK.test-phase.json`: `test_phase_started_at` (the first tick a test binary in a `deps` folder runs in the step's scope), the live `test_phase_peak_bytes` (scope anon, all parallel tests together), and the context (`repo_identity`, `test_scope`, `package`, `filters`, `call_id`, `test_threads`). Its path is the reservation's `sidecar` (`$BUILDLOG_PEAK.cgroup`) with `.cgroup` replaced by `.test-phase.json`. Its one parser is `record.read_test_phase` (`scripts/buildlog/record.py` :603 at `f88d8012`). It returns `TestPhaseRecord` (window `ObservedTestPhase(peak_bytes, started_at)` or `UnobservedTestPhase`), `NoTestPhaseRecord` or `UnreadableTestPhaseRecord`. `buildlog_finish_memory` releases the reservation before `buildlog_end` runs `record.py`, and `record.py` deletes the file after it reads it, so the file still exists at release. The build-log row gets `test_phase_peak_bytes`, and `test_phase_charge` stays null (admission records no charge yet). On 2026-10-30 08:00 EDT, 0 natedev build-log rows had `test_phase_peak_bytes`.
- Replay (helper, 2026-10-30, data to 08:00 EDT; `/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/nextest_charge/report.md`). Rule at the mark: charge = max(test peak so far, N) × m, no decay. N stand-in: the p90 of the family's no-compile nextest runs. At m = 1.2 it frees 795 GiB·min on 10-29 and 320 GiB·min on 10-30, at most 11.6–11.7 GiB at one time. The queue model fails its check (it gives 90 of 592 waited admissions on 10-29 and 97 of 146 on 10-30), so the wait minutes saved (0–140 and 31–199) show sensitivity only.
- Risk, no-compile runs from 2026-10-16 to 10-30 against N from earlier runs: on the narrow key (repository + scope + package + filters), 20 of 869 runs (2.3 %) went above N × 1.2. With rows widened to the package or the repository, 60 of 1,399 (4.3 %). Worst at 1.2: a hana filtered run at width 16, 6.04 GiB against 1.65 GiB. A margin of 1.314 removes only 2–13 more and frees 15 % less.
- The parked arms (same-width history max, max + 10 %) failed: test phases went above that lower charge more often than above today's charge.

**Spec:**

1. **Read the mark.** One helper, `test_phase_path(row) -> Path | NoTestPhaseFile`, gives the file path from the reservation's `sidecar`. An empty `sidecar`, or one that does not end in `.cgroup`, gives `NoTestPhaseFile`. Read the file only with `record.read_test_phase`, through the `record` module that `memory_admit.py` already loads (`record` :72); add the function to that module's Protocol (`GitIdentityReader` :49). Do not write a second parser. Call it at run time only: `record.py` imports `memory_admit` when it loads, so `memory_admit` must not use `record` attributes while it loads. Admission never writes or deletes the file; `record.py` owns it.
   - In `reservation_memory` (:1847), an `ObservedTestPhase` window and a stored `TestPhaseNeed` (Spec 2) give a new `PeakHistory` variant, `TestPhaseStarted(started_at, peak_so_far, need)`, with `peak_so_far` = the window's `peak_bytes`. Every other case keeps today's history (`PeakSoFar`, `PeakDecayed`): no file, no window yet, an unreadable file, or no stored need. The `.max` read stays first, so an unreadable `.max` keeps today's `PeakFileUnreadable`.
2. **N, the test-phase need.**
   - **Rows.** This host's `anon_peaks.jsonl` rows over `HISTORY_DAYS` with `step` `nextest`, an integer `test_phase_peak_bytes` above 0, and the same `repo_identity`, `test_scope`, `package` and sorted `filters` as the step. Use no rows of other filters, other packages or other repositories (no widening: 4.3 % against 2.3 % in the replay).
   - **Width.** The run's width is the reservation's `test_threads` (the share after `set_width`); it is not part of the key. The form is a closed set, picked in this order: `same_width`: rows at the run's width, their p90; `wider_unscaled`: rows only at wider widths, the p90 of the nearest wider width, no scale-down; `scaled_up`: rows only at narrower widths, the p90 of the widest narrower width × width / recorded width, rounded up. No `fitted` form (the replay ran without it). The p90 is `percentile` (:1128). The floor is `FILTERED_FLOOR` (:80, the existing single-test floor, 1 GiB): N = max(form value, `FILTERED_FLOOR`). No second floor rule.
   - **Types.** `TestPhaseNeed(bytes, runs, width, recorded_width, form)`, or `NoTestPhaseNeed(reason)` with a closed reason: `no_width`, `no_history`, or `not_recorded` (a reservation from before this phase).
   - **One rule, one reader.** Split N into a pure function over rows (the rule) and the reader that feeds it, so the replay and the 24 h check call the same rule. The reader is Phase 33's one anonymous-history reader (Phase 33 Spec 3), which feeds `gate_estimate`, `expected_peak`, `build_peaks`, `filtered_peak`, `fresh_test_peak` and the finalizer's reservation-id lookup from store's whole-row boundary. This phase consumes that reader and adds no parser.
   - **When.** Outside the admission lock (Phase 12), once for each admitted nextest step, never on each poll. After `check` admits a nextest step and leaves the lock, it computes N from the row's key and `test_threads` and writes it into the reservation. `set_width` (:3030) computes N for the new width before it takes the lock, and writes it with the width. One locked update helper writes reservation fields by atomic replace (today's read, change and `os.replace` in `set_width`); both callers use it, and it skips a reservation that is gone. The field `test_phase_need` is tagged JSON through one codec: generalize and rename the existing tagged estimate codec, `need_basis_record` (:1352) and `read_need_basis` (:1370), so it has explicit typed entry points for admission need bases (`NeedBasis`) and test-phase needs (`TestPhaseNeed | NoTestPhaseNeed`), while those stay separate domain types. Both entry points use one codec implementation and the existing atomic reservation writer; each field's reader returns that field's named unknown state (`BasisNotRecorded` for `need_basis`, `NoTestPhaseNeed(not_recorded)` for `test_phase_need`). No second encoder. A row with no field reads as `NoTestPhaseNeed(not_recorded)`.
3. **The charge after the mark.** In `charge_basis` (:1100), for `TestPhaseStarted` the observed-peak term becomes `TestPhaseCharge(bytes, peak_so_far, need)`, with `bytes = ceil(max(peak_so_far, need.bytes) × TEST_PHASE_MARGIN)` and `TEST_PHASE_MARGIN = 1.2`. Comment the constant with the replay: 2.3 % of 869 no-compile runs above N × 1.2 on the narrow key; 1.314 removed only 2–13 more and freed 15 % less. The other terms stay: `UsingNow`, the promise (a nextest step keeps `RampPromise` for 120 s, then `NoPromise`, as Phase 24 left it) and `PeakFileUnreadable`. After the mark, the compile `.max` peak and its `.wave` decay no longer count, and the test-phase charge does not decay. The sampler still writes `.max` for the history row. `growth_headroom` (:1119) does not change: the test-phase charge is a budget charge, as the observed peak is today. With no mark or no N, the step keeps today's charge and its 1500 s decay.
   - **Where the types sit.** `TestPhaseCharge` is one more `ChargeBasis` variant, beside `RampPromise` and Phase 24's `LifetimePromise`. It is not a promise: Phase 24's `RampPromise | LifetimePromise | NoPromise` on `MeasuredMemory` does not change. `TestPhaseNeed` is not a `NeedBasis` variant: `NeedBasis` explains the admission `need`, and this phase does not change `need`. So the charge after the mark has one type, and N has one type.
4. **The history row.** Phase 24's one finalizer, `finalize_reservation` (`memory_admit.py` :2953), adds three fields to the `anon_peaks.jsonl` row, from the same `test_phase_path` read: `test_phase_peak_bytes` and `test_phase_started_at` when the window is observed, and `test_phase_need` (tagged JSON) when the reservation has one. With no file or no window, the row has no window fields. N's history starts at this merge: coverage grows over about a day, and until then a family has no N and keeps today's charge.
   - **Saved before the first append.** `record.py` deletes the test-phase file after it reads it (`_delete_step_sources`, `scripts/buildlog/record.py` :729), so a retry cannot read it again. Snapshot the observed test-phase fields and the stored test-phase need into the existing `FinalizationIntent` (`memory_admit.py` :241, Phase 24) before the first history append. Every retry publishes that saved evidence through the same finalizer, also after `record.py` has removed the file.
5. **Build-why words.** `build_why.charge_text` (:51) ends in `assert_never`, so the new variant needs its text: `tests since <HH:MM>: peak so far <GiB>, need <GiB> (<form>, p90 of <runs> runs at width <width>), × 1.2`.
6. **Tests.** In `scripts/lint/test_memory_admit.py`, on `MemoryQueueFixture` (:1286) with `fake_memory_readings`. Extend `MemoryQueueFixture.running` (:1315) to take a sidecar, a `.max` peak and a test-phase file; write no second reservation writer. Write history rows through one module-level row writer, and move the three writers that the classes have today (`HistoryTests.add_measured` :158, `FilteredPromiseTests.samples` :263, `RepositoryHistoryTests.samples` :934) onto it. One test for each case:
   - `test_nextest_charge_falls_to_test_phase_need_at_mark`: `.max` 11 GiB, a mark with peak 0.4 GiB, N 2 GiB: the charge is 2.4 GiB, and a held 12 GiB waiter is admitted.
   - `test_test_phase_charge_follows_peak_so_far`: peak so far 5 GiB, N 2 GiB: the charge is 6 GiB.
   - `test_test_phase_charge_does_not_decay`: 1500 s after the last near-peak time, the charge stays.
   - `test_no_history_keeps_compile_peak`: a mark and no N: today's `PeakSoFar`, with its 1500 s decay.
   - `test_no_mark_keeps_today`: no file, a file with no window, and an unreadable file each keep today's charge.
   - The need: `same_width`, `wider_unscaled` (no scale-down) and `scaled_up` (rounded up); the `FILTERED_FLOOR` floor; rows of other filters, another package or another repository give `no_history`; no width gives `no_width`; a reservation with no field gives `not_recorded`.
   - `test_need_stored_outside_lock_and_on_width_change`: N is computed while the lock is free and stored in the reservation; `set_width` computes it again for the new width.
   - The history row: the finalizer row carries `test_phase_peak_bytes`, `test_phase_started_at` and `test_phase_need`; with no test-phase file (a step that started with the old sampler) and a stored need, the row keeps `test_phase_need` (Spec 4) and has no window fields (`test_phase_peak_bytes`, `test_phase_started_at`); admission leaves the test-phase file in place.
   - The one reader: N's rows come through Phase 33's reader; the existing estimate-consumer tests pass unchanged.
   - The saved evidence (Spec 4): a failed append and an interrupted append, each followed by the removal of the test-phase file and then a retry, and owner-gone cleanup after the file is gone: each ends with exactly one history row that carries the saved `test_phase_peak_bytes`, `test_phase_started_at` and `test_phase_need`.
   - The codec (Spec 2): every `NeedBasis` variant, `TestPhaseNeed` and each `NoTestPhaseNeed` reason round-trip through the one codec; a missing field reads as its field's named unknown state.
   - In `scripts/build_queue/test_build_why.py`: `charge_text` for `TestPhaseCharge`.

   Every test fakes both memory readings.
7. **Docs.** `docs/as-built/build-memory-admission.md`: the mark read, N with its key, width forms and floor, the charge after the mark and its margin, the history row fields, and the replay table.

**State:** the reservation's `test_phase_need` field (written after admission and by `set_width`, under the lock, by atomic replace) and three new fields on the finalizer's `anon_peaks.jsonl` row. No new files; the test-phase file stays `record.py`'s.

**Constraints from prior phases:**
- Phase 12: estimates run outside the admission lock. A step keeps the sampler it started with, so a step that started before 4c's sampler (no test-phase file) keeps today's charge and decay.
- Phase 10: the blocker lookup reuses `decide` and `reservation_memory`, so it follows the new charge with no copy of the rule.
- Phase 24: one finalizer writes every history row and ends every reservation; `ChargeBasis` has `LifetimePromise`; `MeasuredMemory` carries `RampPromise | LifetimePromise | NoPromise`; a nextest step keeps `RampPromise` for 120 s; `need_basis` is tagged JSON.
- Phase 33: one anonymous-history reader feeds every estimate consumer and the finalizer from store's whole-row boundary; a retry of a failed or interrupted append ends with exactly one row with its original outcome. Spec 4 extends that saved intent; it adds no second retry path.
- Phase 26: same file; it changes the ordered snapshot, removes `bump_waiter` and changes the waiter row, and this phase does not.
- Phases 1–2: tests never read live `/proc/meminfo`, `/proc/pressure/memory` or the cgroup tree; the merge gate trips a module that does.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 1–4.
- `scripts/lint/test_memory_admit.py` — Spec 6.
- `scripts/build_queue/build_why.py` — Spec 5 (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/test_build_why.py` — Spec 6, the charge text test (owner queue-unit).
- `docs/as-built/build-memory-admission.md` — Spec 7.

The replay and 24 h check scripts live in the run folder (`phases/15/`), not in the repository; both call Spec 2's rule.

**Workers:** `1 writer + 1 tester`
- `impl` — `memory_admit.py` (including the finalization-intent extension, Spec 4, and the one codec, Spec 2), `build_why.py`, docs, the replay and the follow-up measurement (the 24 h check).
- `test` — `test_memory_admit.py` with its shared fixture (`MemoryQueueFixture`) and the one module-level history row writer (Spec 6), the retry regressions (failed and interrupted appends, owner-gone cleanup), and `test_build_why.py`.

**Acceptance gate:**
- The memory admission and build-why tests pass; `basedpyright` over `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`, `scripts/build_queue/build_why.py` and `scripts/build_queue/test_build_why.py` reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The finalization retry tests (Spec 6) pass: a failed or interrupted append, the removal of the test-phase file, then a retry or owner-gone cleanup, leaves exactly one history row with the saved test-phase evidence (Phase 33's one-row guarantee, extended by Spec 4).
- A test phase is above its charge when its `test_phase_peak_bytes` is above N × 1.2, its charge at the mark. The replay on 4c data uses natedev build-log rows with `test_phase_peak_bytes`, each with N from earlier rows by Spec 2's rule. For every repository, at most 2.3 % of test phases with an N are above their charge. The table (per repository: test phases, test phases with an N, test phases above the charge, worst excess in GiB) goes in the checkpoint notice. If a repository is above 2.3 %, the numbers go to the builds showrunner before the checkpoint.
- The notice has an `also touches scripts/build_queue/build_why.py, test_build_why.py (owner queue-unit)` line, tested against `origin/builds-queue`.
- The 24 h check, 24 h after the merge reaches main: from `anon_peaks.jsonl` rows with `test_phase_peak_bytes` and `test_phase_need`, per repository, the nextest steps that got a `TestPhaseCharge` and the test phases above their charge; the `steps.mem_wait_s` total against the 24 h before the merge; earlyoom kills (journal) and `calls.mem_kills` in both windows. The phase report records when the check is due; the result goes in a follow-up notice.
- Change-Review line: a nextest step's charge falls to its test-phase need once its tests start | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic` two times; during the second run, after its compile, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` | the step's charge reads `tests since …` with its need and width form, below its compile peak.

### Phase 16 — Python test gates take a place in the shared admission queue  · status: done

#### As-built

- `merge_gate.py` `gate_reservation` takes one memory reservation through `memory_admit.check` after `BuildHoldWait` and before the first module. The gate waits in the shared queue, inside `scratch.copy_lock`, as a cargo step waits; `/build:queue` shows it as step `merge-gate` with its checkout as the folder. It releases on every exit path, and `cancel_waiter` removes its queue place when a wait stops. An admission error stops the gate with the checker's last line.
- `check` takes `profile: InferFromArgv | StepProfile | GateProfile`. `GateProfile` (repository `~/.claude`, step `merge-gate`, the checkout, the cap) carries `GateEstimate = MeasuredGatePeak(bytes, runs) | NoGateHistory(cap_bytes)`. `gate_estimate` gives the p90 of this host's `merge-gate` rows in `anon_peaks.jsonl` over `HISTORY_DAYS`, else the cap (`--memory-cap-mib`, default 4096). At the 600 s refresh a `GateProfile` keeps its identity and gets a new estimate, never an argv one.
- `GateReservation.observe` writes `<reservation>.max` and `.wave` atomically when the tree peak goes up, so the charge and its decay work as for cargo. The sidecar is an empty `gate.scope` in the private run folder; release adds the gate's one history row to `anon_peaks.jsonl`. Records go to the caller's `BUILDLOG_DIR/admission`.
- Each module, `types` included, takes one slot from `/dev/steve` (or `BUILDLOG_JOBSERVER_FIFO`) and writes it back on every exit path. `shared_cpu_pool` accepts a FIFO or a character device and reads after a zero-timeout `select`; with no token the launch loop goes on reaping, sampling and handling stops. With no pool file the gate takes no slots.
- Each launch and its cleanup registration run in one `defer_cleanup_signals` block, and the child unblocks `SIGTERM`, `SIGHUP` and `SIGINT` before `exec`, so a stop never gives back capacity under a live module.
- `Gate memory wait: <GiB> needed; <reasons>` (`gate_wait_reasons`) prints on a change, else once a minute.

**Files:**
- `scripts/production/merge_gate.py` — reservation, CPU pool, launch, wait line.
- `scripts/lint/memory_admit.py` — profile union, `GateProfile`, `gate_estimate`, `cancel_waiter`; `queue_rows` keeps every reservation row with an int pid (readers skip `GoneMemory`).
- `scripts/production/merge_gate_test_support.py` — `MergeGateFixture`: private `BUILDLOG_DIR`, fake memory readings, fake jobserver FIFO.
- `scripts/production/test_merge_gate_suites_{admission,resources,signals,types}.py`, `scripts/lint/test_memory_admit.py` — tests.
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — the `merge-gate` step and slots.

**Binds later work:** the gate calls `check`, `release` and `cancel_waiter` in its own process, so a write guard in `admission_dir()` covers it (Shared build state is written only by the installed copy). `merge-gate` rows have no Cargo index step: they stay their own class, and `gate_estimate` is their one reader (Memory admission promises what each repository's steps really use; A build's promise comes from builds of the same shape and holds until the build ends). Gates are queue steps: `gate_wait_reasons` reads their `CheckResult` (The memory queue starts steps in queue order and records when a step became first), and the replay counts them only from rebuilt queue intervals (Replay a looser head rule, and adopt it only on the replay).

**Gotchas:** natedev's `/dev/steve` is a CUSE character device, not a FIFO, and need not honor `O_NONBLOCK`; `select` comes before every read. A launch outside `defer_cleanup_signals` can give back capacity under a live module. `Waiter.need_source` is report-unit's typed `NeedSource`; add no second field.

**Ruled out:** a split of cargo waits by gate presence from old logs (no gate recorded its start); the 24 h check after `queue_times.jsonl` reaches main makes it.

### Phase 17 — The nextest promise splits by compile size  · status: todo
**Depends on:** Phase 11 (same `expected_peak`; Phase 11 splits compile runs from no-compile runs). Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/27 (queue-unit) → 39 → 20 → 11 → **17** → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11. Phases 15 and 27 moved to queue-unit: start after queue-unit's checkpoints for them reach `origin/builds`, and merge it first. `TestPhaseCharge` (Phase 15) comes from those checkpoints.

**Approved:** showrunner 2026-10-29 (no ETA given).

#### Work Order

**Goal:** a nextest step's promise comes from earlier runs that compiled about as much as this run will, so a one-test run on a warm tree is not promised a full compile, and a full compile is not promised a one-test run.

**Measured:**
- The showrunner's figures: the p90 over the "nextest" class mixes 0.5 GiB one-test runs with 29 GiB full compiles (not checked against the log in this Work Order).
- `steps.crates_compiled` counts `Compiling/Checking/Documenting` lines (`scripts/buildlog/index.py` :100); anon rows join to steps by host, `call_id` and step name in Phase 11's one step-evidence reader (the widened `index_shapes` join, `scripts/lint/build_shape.py` :89; Phase 11 Spec 4). That join returns no worktree or step start time today, and a new copy seeds its own build folder from a source tree's cache (the copies rule picks seed; builds reversed the shared build folder on 2026-10-30 and Phase 34 removes `scratch.shared_build_folder`), so a worktree with no earlier step is not a cold cache, and the history it inherited from its seed is not recorded. `width_trial.py` `anon_by_call` (:171) and `anon_by_step` (:197) pair anon rows with steps. The size of a step's compile is known only after it runs.

**Spec:**

1. **Measure first.** Over 14 days of natedev nextest steps, per repository: the distribution of `crates_compiled` and the anon peak by `crates_compiled`. Choose size bands where the peak changes (the edges come from the data; show the table). Then measure predictors of the band at admission time, using only data present before the step: (a) the last recorded `crates_compiled` of the same worktree and step; (b) whether the `tree_key` changed since the last step in that worktree; (c) the build history of the actual Cargo cache folder the step builds into, including reuse across copies. Predictors read the one step-evidence reader (Phase 11 Spec 4), extended with worktree, start time and `tree_key`; add no second index join. Predictor (c) reads lasting attribution to the actual cache folder; an unknown folder history is a named unknown predictor state (`CacheHistoryUnknown`), never proof of a cold cache. Report for each predictor how often it picks the right band, and choose the best. Replay the promise per band against today's (after Phase 11): per repository, promises under half and over 3x the measured peak, wait minutes saved, and how often a step went above its promise.
2. **Adopt rule.** Adopt only when the replay shows fewer promises under half the peak in total and none rises for any repository. Otherwise record the table and stop.
3. **If adopted:** `expected_peak` and `filtered_peak` (`memory_admit.py`) take the predicted band and use only history rows of that band, in each tier. Phase 11's compile/no-compile split stays the first split; bands split compile runs only. With fewer than 5 rows in the band, use the next larger band, never a smaller one. Comment each band edge with the measurement that set it. Predictors (a) and (b) read `crates_compiled`, worktree, start time and `tree_key` from the one step-evidence reader (Phase 11 Spec 4; add worktree, start time and `tree_key` to its value), not from a second join. Predictor (c) reads cache-folder attribution as Spec 1 says.
   - **The band in the need's basis.** Add one variant, `CompileBand(low, high, runs, predictor)`, to Phase 24's `NeedBasis` union; write no second basis type. Producer: `expected_peak`/`filtered_peak`. It travels as Phase 24 Spec 3 sets out: the waiter row's `need_basis` field, read into `RecordedNeed` (`memory_admit.py` :791) by the queue pass, through the one basis codec (`need_basis_record` :1352, `read_need_basis` :1370), and the source token the shell checks. Readers: `build_why.need_text` (`scripts/build_queue/build_why.py` :81) names the band and its predictor and no longer calls the need a plain p90; `memory_gate.sh` names the band in the first wait line (`buildlog_memory_line_inner` :158). An unknown source token fails closed in the shell (:252-254), so the shell and the producer land in one change.
4. **Tests** in `scripts/lint/test_memory_admit.py`: each predictor input; band selection; a thin band takes the next larger band; a no-compile run keeps Phase 11's rule; the waiter row carries `CompileBand`, which round-trips through the basis codec, and the shell's first wait line names the band (`ShellTests` :4042); an unknown cache-folder history gives `CacheHistoryUnknown` in every reader, not a cold-cache band; two worktrees that share one cache folder, and a copy whose record has ended, read that folder's history; fake both memory readings. In `scripts/lint/test_build_shape.py`: the step-evidence reader returns worktree, start time and `tree_key` with the existing shape and compile-count states. In `scripts/build_queue/test_build_why.py`: `need_text` for `CompileBand`.
5. **Docs.** `docs/as-built/build-memory-admission.md`: the bands, the predictor and the replay table.

**Decided (builds showrunner 2026-10-30 18:1x EDT):** predictor (c) stays unavailable until step records keep the actual Cargo cache folder each build used (the next item `Record the Cargo cache folder each build used so trims can be matched to affected builds`); a wrong prediction is worse than none. A seeded copy whose inherited history is not recorded reads as `CacheHistoryUnknown`.

**Constraints from prior phases:**
- Phase 33 (`5c9b01fd`): history rows are read only through `store.whole_rows(handle, start)` (`scripts/buildlog/store.py`); `anonymous_history_rows` (`memory_admit.py`) delegates its decoding to it. A row counts only when it ends in a newline; a cut-short, non-UTF-8 or non-object row is skipped. Add no second decode loop; keep the unfinished-JSON and split-UTF-8 consumer tests after any classification change.
- Phase 11: keyed rows win over unkeyed rows; 1–4 own runs do not take the pool; the compile-run promise wins over `min(filtered, full)`; fresh runs keep `fresh_test_peak`.
- Running charge: the band changes `need` and so the ramp promise only; it leaves the running charge as built (`build_charge` :1096, `charge_basis` :1100, `growth_headroom` :1119, `reservation_memory` :1847 at `31c3ee3e`; `docs/as-built/build-memory-admission.md`): a running step charges the largest of its current resident and swap bytes, its observed `.max` peak, its promise during `RAMP_SECONDS` (120 s), and its promise when the peak is unknown. After Phase 24, a release-profile `build` step holds its promise for its whole life (`LifetimePromise`, in the budget charge and the free-memory growth term); nextest steps keep the ramp. The `.max` peak expires `PEAK_DECAY_SECONDS` (1500 s) after the last near-peak time in `.wave`, or after the `.max` mtime when `.wave` is absent. Phase 15 lands before this phase: after report 4c's test-phase mark the running charge is `TestPhaseCharge`, and the band leaves that charge unchanged too.
- Phase 24: `NeedBasis` and its path to the waiter row, build-why and the shell are Phase 24's; this phase adds one variant on that path, and the existing `need_basis` codec and rendering tests and the `LifetimePromise` tests stay gates. `build` steps keep Phase 24's same-shape rule; bands apply to nextest steps only.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 3.
- `scripts/lint/build_shape.py` — Spec 3, worktree, start time and `tree_key` in the one step-evidence reader's value.
- `scripts/lint/test_build_shape.py` — Spec 4, the reader's new fields.
- `scripts/lint/memory_gate.sh` — Spec 3, the band in the wait line.
- `scripts/lint/test_memory_admit.py` — Spec 4.
- `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py` — Specs 3–4, the band text (owner queue-unit; trial-merge against `origin/builds-queue`).
- `docs/as-built/build-memory-admission.md` — Specs 1 and 5.

**Workers:** `1 writer + 1 tester`
- `impl` — admission (`memory_admit.py`), the step-evidence reader (`build_shape.py`), the shell text (`memory_gate.sh`), `build_why.py` and docs.
- `test` — `test_memory_admit.py`, `test_build_shape.py` and `test_build_why.py`; the predictor replay (Spec 1).

**Acceptance gate:**
- The memory admission and build-why tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; `bash -n scripts/lint/memory_gate.sh`; the merge gate is green. If Spec 2 stops the phase, no code changes and this item does not apply.
- The notice has an `also touches scripts/build_queue/build_why.py, test_build_why.py (owner queue-unit)` line, tested against `origin/builds-queue`.
- The `need_basis` codec and rendering tests (Phase 24's, plus `CompileBand`) and the `LifetimePromise` tests pass; an unknown cache-folder history reads as `CacheHistoryUnknown` in admission, build-why and the shell.
- The band table, predictor accuracy and before/after replay are in the checkpoint notice, with wait minutes saved and steps above their promise.
- Post-merge check, 24 h after the merge reaches main: per band, steps above their promise and wait minutes against the 24 h before.
- Change-Review line: a small-compile test run on a warm tree gets a small promise | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic --filter <one test>` once, then add a comment line to one source file of that crate (a small compile that is not zero; an unchanged tree takes the freshness path instead), run it again and read that run's memory wait line | its need is near the small band's p90 that the replay gives, and its basis names the compile band it picked.

### Phase 18 — History splits on the dylib feature and each step's commit, not on a time  · status: todo
**Depends on:** Phase 17 (same `expected_peak`). Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/27 (queue-unit) → 39 → 20 → 11 → 17 → **18** → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

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
3. **The split.** `expected_peak` and `filtered_peak` keep only history rows on the same side of every listed change as the admitted step. `argv_feature`: the feature set of the step's own argv and of the row's argv, both from `build_shape(argv).features` (`scripts/lint/build_shape.py` :59), which already reads comma lists, `--features=`, `-F` and repeated flags; write no second parser. `commit`: whether the change is an ancestor of the step's `HEAD` and of the row's `sha`. An anon row's argv and `sha` come from the one index join (Phase 11 Spec 4); add `sha` to its value and open no second join. Run git once per distinct sha and keep the answers in `admission/build_marks.json`; measure the added check time and report it. A release build (`BuildShape.profile` is `release`: `--release` or `--profile release`) ignores the opt-level entry (dev and test only).
4. **Fallback.** With fewer than 5 rows on the step's side, the tiers below apply as today, still on that side. Rows of the other side never fill a tier.
5. **Tests** in `scripts/lint/test_memory_admit.py`: a step with the dylib feature in a comma list, and one with `--features=`, lands on the dylib side; a sha after and before a commit change; a row with no sha counts on neither side; the cache file; a release `build` step keeps Phase 24's tiers, floor, factor and cap on its side; fake both memory readings. In `scripts/lint/test_build_shape.py`: the join's value carries `sha`. The existing `BuildShapePromiseTests` (same-shape promise and failed-peak floor) and the `need_basis` codec and rendering tests pass unchanged.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the list, the split rule and the replay table.

**Constraints from prior phases:**
- Phase 33 (`5c9b01fd`): history rows are read only through `store.whole_rows(handle, start)` (`scripts/buildlog/store.py`); `anonymous_history_rows` (`memory_admit.py`) delegates its decoding to it. A row counts only when it ends in a newline; a cut-short, non-UTF-8 or non-object row is skipped. Add no second decode loop; keep the unfinished-JSON and split-UTF-8 consumer tests after any classification change.
- Phase 17's bands and Phase 11's compile split apply inside each side.
- Build history inside each side: `build_expected_peak` (`memory_admit.py` :1312) keeps Phase 24's same-shape tiers (p90 with 10 or more rows, the largest with 1 to 9), failed-peak floor, release factor 1.314 (`RELEASE_HEADROOM` :89), the dev rule Phase 39 adopted (it replaced `DEV_HEADROOM`'s single 4.2034275337646125 factor; keep the adopted rule, never restore the factor) and 21 GiB cap. `ShapeFallback` (the profile fallback) has neither floor nor factor; feature-side filtering is already satisfied inside same-shape build tiers and remains necessary for profile fallback and other step histories. Determine release profile through `BuildShape.profile`, including `--profile release`, and test both release spellings. The split removes rows; it changes none of those values, and Phase 11's lower pool never replaces a same-shape `build` promise.
- `scripts/lint/build_shape.py` holds the one argv parser and the one anon-to-index join (Phase 24 Part A; Phase 11 widens the join).
- The estimate runs outside the admission lock (`check`), so git calls never run under the lock.

**Files:**
- `config/build_changes.json` — new — Spec 2.
- `scripts/lint/memory_admit.py` — Spec 3–4.
- `scripts/lint/build_shape.py` — Spec 3, `sha` in the join's value.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py` — Spec 5.
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6.

**Workers:** `1 writer + 1 tester`
- `impl` — `config/build_changes.json`, `memory_admit.py` (history filtering), `build_shape.py` (step evidence), the as-built doc and the side-split replay.
- `test` — `test_memory_admit.py` and `test_build_shape.py` (Spec 5).

**Acceptance gate:**
- The memory admission and build-shape tests pass; `basedpyright` over `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py`, `scripts/lint/test_memory_admit.py` and `scripts/lint/test_build_shape.py` reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The side counts, the peak per side, the added check time and the before/after replay are in the checkpoint notice.
- Post-merge check, 24 h after the merge reaches main: hana steps above their promise and wait minutes against the 24 h before.
- Change-Review line: a dylib hana test is promised only from dylib history | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic` and read its first memory wait line | the need and its source match the dylib-side p90 that the replay gives.

### Phase 19 — Measure where admission estimates come from, then make the 0.65 fallback rare  · status: todo
**Depends on:** Phase 18 (same `expected_peak`); Phase 20 Spec 0 (the `admitted` events and their one reader, Spec 1). Runs before Phase 8. Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/27 (queue-unit) → 39 → 20 → 11 → 17 → 18 → **19** → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11. Phases 15 and 27 moved to queue-unit: start after queue-unit's checkpoints for them reach `origin/builds`, and merge it first. Phase 27's gate cap source and the removal of `gate_estimate` come from those checkpoints.

**Approved:** showrunner 2026-10-29, rescoped (no ETA given).

#### Work Order

**Goal:** most admissions use measured anon peaks. The build-log peak times `ANON_SHARE` is a fallback that few admissions take.

**Measured (code, at `a650d945`):**
- `ANON_SHARE = 0.65` (`memory_admit.py` :71) scales a build-log `peak_mem_bytes` (page cache included) to process memory: in `expected_peak` (:597) when a repository has fewer than 5 measured anon peaks, and in `filtered_peak` (:703, :708) for the index tiers. `ExpectedPeak.source` (:141) is `measured`, `buildlog`, `pooled` or `fallback`.
- Before Phase 20 Spec 0, no record keeps the source of an admitted step's estimate: reservations have no source field, and waiter rows (report Phase 3 adds `need_source`) are deleted at admission. From Spec 0's merge, each `admitted` event in `admission/queue_times.jsonl` records the need source, and Phase 20's one interval reader in `memory_admit.py` rebuilds each step from those events.

**Spec:**

1. **Measure first.** For every admitted natedev step in the last 7 days (steps that pass the memory gate: `width_trial.py` `_uses_memory_gate` :355), find its tier. For a step in the period that Phase 20 Spec 0 covers, read the recorded source of its `admitted` event through Phase 20's one interval reader; write no second parser of `queue_times.jsonl`. For an earlier step, replay `expected_peak` with the history before the step. Keep the two apart: recorded sources and reconstructed historical estimates are separate columns, never one sum. Phase 27's `cap` source (the gate row sized by its cap) is its own row; it is a chosen size, not a history fallback, and it counts in neither the `fallback` share nor the `× ANON_SHARE` share. Recorded source tables include `shape` and `cap` as distinct sources. `expected_peak` (:1152) routes a build to `build_expected_peak` with the default shape `NO_BUILD_SHAPE`, so a replay through the old call signature silently gives fallback estimates: reconstruct earlier build estimates with their recorded argv through `build_shape` and the shared step-evidence reader (Phase 11), preserving the historical shape and profile rules; report missing shape evidence separately, never as an ordinary fallback admission. Report the share of admissions per tier, by step kind (nextest full, nextest filtered, clippy, check, build, doc, other). Report the share that took a `× ANON_SHARE` tier, and for those steps, the estimate against the measured anon peak.
2. **Stop rule.** If the `× ANON_SHARE` share is already small, the phase is only this measurement: the table goes in the as-built doc and no code changes. The showrunner sets "small" from the table.
3. **Otherwise, widen anon matching.** When the exact filter set has no anon history, use the same repository + step anon rows (same scope and Phase 11/17/18 splits) before any `× ANON_SHARE` tier. Replay again: the `× ANON_SHARE` share, promises under half and over 3x the measured peak, wait minutes, steps above their promise.
4. **Drop 0.65 once rare.** When the replay shows the `× ANON_SHARE` share below the showrunner's bound, remove the `ANON_SHARE` tiers; a step with no anon history goes to the pooled tier, then the fallback. Keep `ANON_SHARE` if the replay shows more promises under half the peak without it. Only the producer stops: old records stay readable. `NeedSource` (`memory_admit.py` :130) keeps `buildlog` as a value that only old rows carry; `build_why.need_text` (:81) and the shell's `buildlog)` description (`memory_gate.sh` :163) still render it; the cached-profile read in `run_action` still accepts it.
5. **Tests** in `scripts/lint/test_memory_admit.py`: repo + step anon rows win over the index tier when the exact filter has none; with no anon history the next tier applies; a `build` step keeps Phase 24's same-shape rule when the widened match has rows of another shape; fake both memory readings. If Spec 4 removes the tier: an old waiter row with `need_source` `buildlog` still renders in build-why (`scripts/build_queue/test_build_why.py`) and in the shell's wait line.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the tier share table; if changed, the `need` paragraph.

**Constraints from prior phases:**
- Phase 33 (`5c9b01fd`): history rows are read only through `store.whole_rows(handle, start)` (`scripts/buildlog/store.py`); `anonymous_history_rows` (`memory_admit.py`) delegates its decoding to it. A row counts only when it ends in a newline; a cut-short, non-UTF-8 or non-object row is skipped. Add no second decode loop; keep the unfinished-JSON and split-UTF-8 consumer tests after any classification change.
- Phases 11, 17 and 18 set which rows match; the widened match keeps every split, and reads rows through the one index join in `scripts/lint/build_shape.py` (no second join or argv parser).
- Phase 24 owns the `build` promise: `build_expected_peak` (`memory_admit.py` :1312) keeps Phase 24's same-shape tiers (p90 with 10 or more rows, the largest with 1 to 9), failed-peak floor, release factor 1.314 (`RELEASE_HEADROOM` :89), the dev rule Phase 39 adopted (it replaced `DEV_HEADROOM`'s single 4.2034275337646125 factor; keep the adopted rule, never restore the factor) and 21 GiB cap. `ShapeFallback` (the profile fallback) has neither floor nor factor; feature-side filtering is already satisfied inside same-shape build tiers and remains necessary for profile fallback and other step histories. Determine release profile through `BuildShape.profile`, including `--profile release`, and test both release spellings. The widened match and the drop of 0.65 change other steps' tiers only.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 3–4 (none if Spec 2 stops the phase).
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `scripts/build_queue/test_build_why.py` — Spec 5, the old `buildlog` row test, only if Spec 4 removes the tier (owner queue-unit; trial-merge against `origin/builds-queue`).
- `docs/as-built/build-memory-admission.md` — Specs 1 and 6.

**Workers:** `1 writer + 1 tester`
- `impl` — the measurement, any adopted tier change in `memory_admit.py`, and the as-built doc.
- `test` — `test_memory_admit.py` and the old-source rendering tests (Spec 5).

**Acceptance gate:**
- The tier share table is in the phase report and the checkpoint notice, with the stop decision.
- If code changed: the memory admission tests pass; `basedpyright` over both Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green; the before/after replay is in the notice. Post-merge check, 24 h after the merge reaches main: steps above their promise and wait minutes against the 24 h before.
- Change-Review line: few admissions use the 0.65 fallback | on natedev while builds wait, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` | the waiting rows name source `measured` and few name `buildlog`. If Spec 2 stops the phase: Change-Review: none — measurement only.

### Phase 20 — Replay a looser head rule, and adopt it only on the replay  · status: todo
**Depends on:** Phases 31 (Rule A; the head-admission override in the one snapshot), 33 (the finalizer's retry guarantees, Spec 0) and 26 (`queue_times.jsonl`, the priority-aware ordered snapshot and the total queue wait; the user's 2026-10-31 amendment dropped `head_since`), all on the same file `memory_admit.py`; checkpointed Phases 16, 24, 30 and 32 (gate rows, the one reservation finalizer, the ordered walk, the override); report-unit's build-why change on `origin/builds` (`explain_queue`, Spec 2). Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/27 (queue-unit) → 39 → **20** → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11. Phases 15 and 27 moved to queue-unit: start after queue-unit's checkpoints for them reach `origin/builds`, and merge it first. Phase 39 (the dev need rule) lands first: its deployment time is a calibration boundary for Spec 1, like each other deployed rule change.

**Approved:** showrunner 2026-10-29 (ETA 08:30 EDT 2026-10-30). The showrunner decides adoption from the replay table. Spec 0 (every step's queue life in the shared record): builds showrunner 2026-10-30.

#### Work Order

**Goal:** when the head of the memory queue cannot fit by its own measure, younger steps that fit start, if the replay shows the head is not held back by more than the showrunner's bound.

**Measured:**
- Today's rule (`memory_admit.py` at `a650d945`): `HEAD_SECONDS` 600 (:90), `STRICT_SECONDS` 600 (:91), `BACKFILL_SECONDS` 120 (:92). `head_window` (:307): before 600 s younger steps backfill; after that, 600 s strict then 120 s backfill, in a cycle. `head_priority` (:1025) gives the head priority when it fits now or `MemAvailable + measured >= head need + reserve`. `queue_decision` (:1040) holds every younger step in a strict window (`replace(decision, state="hold")`, :1047-1048), even one that fits.
- At `31c3ee3e`: `queue_decision` (:2181) returns its row's entry from `admission_walk` (:2151; Phase 30) and takes `ordered`, `recovery` and `now`; `check` applies the Phase 32 head-admission override after the walk (:2460-2469).
- The hana showrunner saw 25 steps wait behind one 11.8 GiB head while many 3 GiB clippy steps fit (reported 2026-10-29; not checked against the log in this Work Order).

**Spec:**

0. **Record every step's queue life first.** Phase 16 deferred its comparison of cargo waits with and without a gate running, and no record can make it: `release` (:3017) keeps an end time and a positive peak, but no arrival, admission time, wait or need, and a gate has no Cargo `steps` row. `memory_admit.py` appends lifecycle events to Phase 26's `admission/queue_times.jsonl` through Phase 26's one append function (`at`, lock, 8 MiB / 14-day trim), with Phase 26's identity fields (one record holds every queue time; no second gate history): `admitted` from `check` (arrival = the waiter's `since`, `admitted_at`, wait seconds, `need`, the need source, the head state), `left` from `cancel_waiter`, and `ended` from Phase 24's finalizer, `finalize_reservation` (:2953) (`ended_at`, `outcome`, and `PeakSampled` or `NoPositiveSample` for an exit before the first positive sample). Every step writes them, `merge-gate` included. One reader in `memory_admit.py` rebuilds each step's interval from these events. Nothing claims the Phase 16 comparison or the replay's gate coverage until that reader rebuilds overlapping gate intervals, a cancelled gate and a gate that exits before a positive sample. The Phase 16 comparison runs 24 h after Spec 0 reaches main, not 24 h after Phase 16. Lifecycle publication preserves Phase 33's retry guarantees and reservation identity: the `ended` event comes from the saved finalization intent, once per reservation id, also on a retry. Spec 0 lands in every case, also when Spec 1 stops the phase.
1. **Calibrate the queue model first.** Calibrate on one cohort whose real policy is known: one span with one rule in force, bounded by the time every admission-policy checkpoint reached main (`git log origin/main`): Phases 24, 30, 31, 32, 26, 15 and 27. Each changes the policy: 24 the build promise and its lifetime charge, 30 the ordered walk, its recovery slot and the bump stop, 31 Rule A, 32 the head-admission override, 26 queue priority, 15 the test-phase charge, 27 the gate's cap. The replay uses the cohort's actual charge and override rules. Use the newest span that holds 7 days, and say which. Then Spec 3 compares the deployed rule with the capacity rule on that model, so a change from any of those phases does not read as model error. The Phase 15 replay (`/tmp/claude/unit/abea5ffb-4b99-499e-ae41-7ebb04630f9f/phases/15/replay.py`; report `replay_report.md` in the same folder) is the failed baseline: its replay of today's rule gave 113,965,165.46 wait minutes against 178,192.68 recorded `mem_wait_s` minutes, 639.56 times the recorded wait. Its report names the admission inputs it lacks: `MemAvailable` and host totals at each admission poll, PSI avg10, exact sccache anon, request retries, queue bumps, and `.max`/`.wave` growth. Over the cohort's 7 days of the natedev build log, the replay of its rule must give total wait minutes within 25% of the recorded `mem_wait_s` sum (all steps, and hana alone). Inputs: step arrival = `steps.started_at - mem_wait_s`, `need` from `expected_peak` with the history before the step, run time `duration_s`, charge from `anon_peaks.jsonl`, `MemAvailable` from `samples.mem_used_bytes`, bumps from `queue_bumps` only before Phase 30 (after it no bump moves a step), queue priority from Phase 26's build-log rows, and override admissions from `head_admission` rows. Print both totals and the ratio, and name each missing input the replay uses a stand-in for. If the replay cannot reach the bound, the phase records the totals, the ratio and the missing inputs in the as-built doc and the phase report, names the record that each missing input needs as a follow-up item, and stops the comparative replay and any behavior change (Specs 3–6). Spec 0 lands in every case; whether Spec 2 lands is the pending decision below.
2. **One head policy, before the comparison.** Since Phase 31 (`04e4cee2`) each row's admission decision, its `BehindHead`/`BehindOlderFit` reason and `check`'s head fields already come from the one walk through `admission_for_waiter`, `queue_wait_reason` and the shared override decision (`HeadRoom | NoHeadRoom`). What still recomputes the window on its own is `head_wait_reason` (:672, through `head_window` :677) and the window state in `read_queue_pass`. Keep that admission decision path and the `window_clause` writer; add no second implementation of Rule A. Make one function return the head's policy for a queue snapshot, with a closed window state and a closed rule argument: rule (a) the deployed rule of Spec 3, rule (b) the capacity rule of Spec 3. It takes the walk and its recovery slot, Phase 31's head room and Phase 26's priority-aware order, and keeps the head-admission override that Phase 31 puts in the one snapshot (never transferred to another waiter). Production passes rule (a). Admission, the blocker lookup, `read_queue`, `explain_queue` (in the pass it shares with `read_queue`), `head_wait_text` (:690) and `check`'s head fields all read that result; none recomputes the window. The release search reads it too: `head_after_steps_end` (`memory_admit.py` :2829) rebuilds the head waiter from the frozen `QueueExplanation` and calls `head_priority` and `queue_decision`, and `smallest_head_release` (:2872) calls it for each set of endings. Both call the one policy function on the snapshot with those rows ended. They stay frozen and read-only (no file read, no lock, no state change), and they return the closed `HeadRelease = HeadAdmitsAfterEnd | HeadStartsAt` (:909) or `HeadStillHeld`, with no new variant. The release line text stays only in `build_why.release_text` (:161). This step changes no admission behavior: the existing tests pass unchanged. The replay imports this function from the checkout; it holds no copy of the rule. It lands whether or not Spec 5 adopts rule (b).
3. **Comparative replay.** On the calibrated model of Spec 1, replay the queue through the Spec 2 function under two rules: (a) the deployed ordered walk after Phases 30, 31 and 26: Rule A, recovery spacing and the recovery slot, queue priority (a user bump stays until the user moves it; no expiry), the total queue wait, the Phase 32 override and the lifetime promise of release builds; (b) the same walk with only the stated capacity condition changed: in a strict window, when `decide` for the head alone returns hold, a younger step is checked as in a backfill window, on its own budget and memory checks, with no head-room check (Rule A's check (2)). Both rules keep the cohort's own test-phase charge and gate-cap rules: a nextest step gets Phase 15's `TestPhaseCharge` after its mark only in a cohort after Phase 15 landed; in a cohort after Phase 27 landed, each gate row holds its own cap, read from its `admitted` event's `need`, so a gate run with a nondefault `--memory-cap-mib` keeps that cap, not the default; in an earlier cohort, a gate row holds the estimate it had. Report per day and per repository: wait minutes under each rule, minutes saved, and for each head, how much later it starts under (b) (count, p50, p90, max). Find the hana showrunner's case in the log and show it as one row.
4. **Risk.** For rule (b), count how often an admitted step went above its charge, and the replayed `full avg10` holds, against rule (a).
5. **Adopt rule.** The showrunner sets the bound on the head's added delay from the replay table. Rule (b) lands only if the head's added delay stays under that bound. Otherwise the phase records the table in the as-built doc and lands only the Spec 2 function.
6. **If adopted:** production passes rule (b). The window state adds `CapacityBackfill` (the head cannot fit, so younger steps that fit start) apart from today's timed backfill. `verify.sh`'s wait lines and `/build:queue` share the text: the window text keeps one writer, `head_facts.order_text` (`scripts/build_queue/head_facts.py` :388) through Phase 31's one clause function. Queue priority (Phase 26) keeps its meaning: the priority unit's steps walk first under both rules, and the Phase 32 override admits only its own head.
7. **Tests** in `scripts/lint/test_memory_admit.py`: for one snapshot under rule (a), admission, `blocker`, the queue view's Reason, `explain_queue` and the shell wait text show the same window state (Spec 2); `BehindOlderFit`, the recovery-slot reservation and an override head read the same in every reader, and the Phase 30 and 31 cross-reader tests, `AdmitHeadTests`, `test_fitting_strict_head_keeps_its_place_when_younger_polls_first` and `test_build_why.py` `test_override_free_memory_terms_add_up_to_its_printed_threshold` pass unchanged. During recovery spacing, with a head behind running charge that only endings free: `smallest_head_release` returns `HeadStartsAt(next_start_at)` with the required steps in `ended`, from the same window state that admission reads, and reads no file (rule (a), and rule (b) if adopted). If adopted: a younger step that fits starts while a head that cannot fit is strict; while a head that fits is strict, a younger step that fits beside the marked head still admits (budget 21 GiB, running charge 10, head 8, younger 2), and a younger step the marked head's room leaves no space for holds with `BehindOlderFit` (same snapshot, younger 4); queue priority still walks the priority unit first, and an override head still admits only itself; the same five readers show `CapacityBackfill`. In `scripts/build_queue/test_build_why.py` (owner queue-unit), only if `build_why.py` changes: its head text for each window state. Fake both memory readings.
8. **Docs.** `docs/as-built/build-memory-admission.md`: the calibration totals, the replay table and, if adopted, the new head rule (Rule and Waiters paragraphs).

**Decided (builds showrunner 2026-10-30 18:1x EDT):** the one head-policy function (Spec 2) lands even when calibration (Spec 1) fails; it changes no behavior and removes four copies of the window rule.

**Constraints from prior phases:**
- Phase 10: the blocker lookup reuses `decide` and `queue_decision`; no second rule.
- Running charge: the replay uses the running charge as built after Phase 24 (`build_charge` :1096, `growth_headroom` :1119, `reservation_memory` :1847 at `31c3ee3e`; `docs/as-built/build-memory-admission.md`): a running step charges the largest of its current resident and swap bytes, its observed `.max` peak, its promise during `RAMP_SECONDS` (120 s), and its promise when the peak is unknown; a release-profile `build` step holds its promise for its whole life, in the budget charge and the free-memory growth term (`LifetimePromise`). The `.max` peak expires `PEAK_DECAY_SECONDS` (1500 s) after the last near-peak time in `.wave`, or after the `.max` mtime when `.wave` is absent. Phase 15 lands before this phase: in a cohort after Phase 15 landed, the replay charges a nextest step `TestPhaseCharge` after report 4c's test-phase mark, as Phase 15 builds it (Spec 3 keeps the cohort's rule).
- Phase 16: merge gates are queue steps; the replay counts them only from Spec 0's rebuilt intervals. Phase 27: after it lands, a gate row holds its own `--memory-cap-mib` from start to end, with need source `cap`.
- Phase 26: one priority-aware ordered snapshot, resolved once at the snapshot boundary, feeds admission, `admit_head`, the queue readers, the blocker lookup and the release search; the walk does not sort it again, and `head_since` lives in the queue pass. Spec 2's one function takes that order as it is.
- Phase 31: Rule A runs inside the walk and keeps its recovery slot; the head-admission override (`HeadAdmissionOverride | NoHeadAdmissionOverride`) is part of the one admission snapshot, so check, the queue view, build-why and the release search agree when a head is let in. This phase keeps it: the one policy function reads the override from the snapshot.
- Phase 32: `admit-head` admits the waiting head once; its `head_admission` set, admitted and lapsed rows stay readable (`AdmitHeadTests`, `test_memory_admit.py` :2122).
- Phase 25: `head_after_steps_end` and `smallest_head_release` return `HeadStartsAt` for a recovery spacing hold, with the endings the timer needs first; `build_why.release_text` is the one writer of the release line.
- report-unit's build-why change (on `origin/builds` since `228b45ae`): `explain_queue() -> QueueExplanation` in `memory_admit.py`, `ChargeBasis` and the waiter `need_source` as closed unions, and `scripts/build_queue/build_why.py`. `read_queue` and `explain_queue` share one internal pass (`read_queue_pass` :2694); the policy result goes in that pass. Keep its reader contract: `queue_rows` (:2599) keeps every reservation row, readers skip `GoneMemory` rows (:2668, :2705), and `Waiter.need_source` is the typed `NeedSource` (:440; `NeedSource` :130).

**Files:**
- `scripts/lint/memory_admit.py` — Specs 0, 2 and 6.
- `scripts/lint/test_memory_admit.py` — Spec 7, and Spec 0's events and interval reader (overlapping gates, a cancelled gate, an exit before a positive sample).
- `docs/as-built/build-memory-admission.md` — Specs 0, 1, 3 and 8.
- `scripts/build_queue/build_queue.py` — only if its head Reason text must change (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/head_facts.py`, `scripts/build_queue/test_head_facts.py` — only if adopted: the `CapacityBackfill` window text in `order_text`, the one window-text writer (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py` — only if the head text in build-why must change (owner queue-unit; trial-merge against `origin/builds-queue`).

**Workers:** `1 writer + 1 tester`
- `impl` — the lifecycle writer and reader (Spec 0) and the head policy function (Specs 2 and 6) in `memory_admit.py`, any queue text change, and docs.
- `test` — lifecycle, frozen-policy and explanation tests (Spec 7) and the comparative replay (Specs 1, 3 and 4).

**Acceptance gate:**
- Spec 0: its tests pass, including a retried `ended` event published once per reservation id; post-merge check, 24 h after Spec 0 reaches main: the Phase 16 comparison (cargo-step wait minutes and pressure holds with and without a gate running) from the rebuilt intervals; the phase report records when it is due; the result goes in a follow-up notice.
- The calibration totals (replayed and recorded wait minutes, the ratio, the missing inputs, the cohort) are in the phase report and the checkpoint notice. If Spec 1 stops the phase: Change-Review line: each step's queue life is recorded | on natedev after a merge gate runs, read the last lines of `~/.local/state/buildlog/admission/queue_times.jsonl` | the gate has `admitted` and `ended` lines with its arrival, admission and end times.
- The replay table is in the phase report and the checkpoint notice with the showrunner's bound and the adopt decision.
- The Spec 2 function: the memory admission tests pass unchanged and with the new same-state test; the Phase 30 and 31 cross-reader tests (`BehindOlderFit`, the recovery-slot reservation, an override head) and `AdmitHeadTests` pass unchanged; `basedpyright` over the changed Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
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
- Removal writes a certificate `.scratch-removing-<copy>.json` in the pair folder with the copy identity, so a stopped removal finishes on a rerun and never removes a new copy at the same path. It is the same removal mark `scratch.remove_marked` writes beside every scratch folder (one name, one writer, one reader `pending_removal`); only a copy's mark carries and checks the identity.
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

### Phase 23 — Hold status names unsent release notices  · status: done

#### As-built

- `build_hold.py status` prints one line per pending release record, after the holder lines or `no build hold`: `release pending: <holder name>, released <HH:MM zone> - unsent: <name>, <name>; run /build:hold release to finish it` (`every session (broadcast)` for a broadcast). An unreadable record or cycle prints `release pending: <path> could not be read: <error>; /build:hold release sets it aside`.
- Status and release share `pending_release_paths()` and `read_release_intent()`. Status takes no `release_lock()`, never calls `recorded_releases()`, and changes no file.
- `release_notices(intent)` is the one unsent reader: `CycleNoticesToSend(cycle, entries)` or the broadcast notice. The first cycle entry per session counts; a recipient is unsent when its entry is missing, `NotAttempted` or `DeliveryFailed`. A cycle id that is not the record's `cycle_id` raises `ReleaseNoticeReadError(path, reason)`.
- Release sets a damaged pending record or cycle aside (`set_aside_damaged_record`, `<name>.damaged-<UTC time>[-n]`) and goes on. It rebuilds a damaged or missing cycle from the intent's recipients, so the printed command works.

**Files:**
- `scripts/build_hold/build_hold.py` — path list, unsent reader, status lines, set-aside and cycle rebuild.
- `scripts/build_hold/test_build_hold.py` — status and release agreement, damaged and duplicate cases, status writes nothing.
- `docs/as-built/build-memory-admission.md` — Build hold: `status` row, release intent paragraph.

**Binds later work:** the status reader takes no lock, so a write guard at `release_lock()` does not refuse it. The set-aside and cycle rebuild are writes: recovery that the guard routes through `ReleaseLockHeld`, with no second `flock` (Shared build state is written only by the installed copy).

**Gotchas:** a rebuilt cycle has no delivery evidence, so a session can get a notice twice (at-least-once).

### Phase 24 — A build's promise comes from builds of the same shape and holds until the build ends  · status: done

#### As-built

- **Shape.** `build_shape(argv) -> BuildShape` is a frozen value (profile, sorted features, targets) with a stable JSON `key`; `--all-features` and `--no-default-features` are their own feature values. `recorded_build_shape(raw) -> BuildShape | NoBuildShape` decodes a stored key and never infers `dev`. `index_shapes(index_path, host, step, cutoff, before) -> dict[(call_id, step), BuildShape]` reads `steps.argv` read-only; an old history row takes its shape from this join, and a row with no match has no shape.
- **Promise.** `build_expected_peak(repository, shape, host, now, root) -> ExpectedPeak` counts only same-repository, same-shape rows: p90 with 10 or more rows (`ShapeP90`), the largest with 1 to 9 (`ShapeLargest`). With no same-shape rows, `ShapeFallback` is `max(FALLBACK, heaviest same-profile build)`, with no floor and no factor.
- **Failed-build floor.** On the two tiers, the newest same-shape row with outcome `killed` or `owner_gone`, or with a peak above its `need`, sets `FailedPeakFloor(peak, ended_at)`; the base is `max(tier, floor)`. A later success below the floor does not end it; a newer failed row replaces it; it ends when its row leaves `HISTORY_DAYS`. A row with `OutcomeNotRecorded` never sets a floor.
- **Factor and cap.** The tiers multiply by `RELEASE_HEADROOM` (1.314) for release shapes and `DEV_HEADROOM` (about 4.203) for dev shapes; other profiles get `NoHeadroom`. Every promise caps at `BUILD_BUDGET_BYTES` (21 GiB) as `CappedAtBudget(uncapped)` or `UnderBudget`.
- **Need basis.** `NeedBasis = LegacyNeedSource | ShapeP90 | ShapeLargest | ShapeFallback`, each shape tier with its floor, factor and cap. `need_basis_record` writes it to the waiter row's `need_basis` beside the `shape` source token; `read_need_basis` reads it back (`BasisNotRecorded` for an old row). `read_queue_pass`, `build_why.need_text` and `memory_gate.sh` read it; the wait line says "a same-shape promise", never a p90. An unknown token fails closed in the shell.
- **Lifetime promise.** `MeasuredMemory.promise` is `RampPromise | LifetimePromise | NoPromise`, set in `step_memory`. A release `build` step holds `LifetimePromise(need)` until it ends: `charge_basis` charges `max(need, current memory, observed peak)` and `growth_headroom` keeps `need − current` as growth in the free-memory check. `build_why.charge_text` says the promise holds until the step ends. Dev builds and all other steps keep the 120 s `RampPromise`.
- **One finalizer.** `finalize_reservation(path, row, outcome, now, status) -> HistoryRecorded | NoHistoryRecorded` runs under the caller's lock, from `release` and from `check` for each ended row. It stores a `finalization` intent with a stable `reservation_id` in the reservation, appends one `anon_peaks.jsonl` row with `need`, `shape` and `outcome` (`completed` status 0, `failed` 1–127, `killed` 128+, `owner_gone`) unless that id is already there, then removes the sidecars and the reservation last. `check` uses the evidence it records in the same admission.

**Files:**
- `scripts/lint/build_shape.py` — shape value, stored-key decoder, index join.
- `scripts/lint/memory_admit.py` — same-shape promise, floor, need basis codec, lifetime promise, `finalize_reservation`.
- `scripts/lint/memory_gate.sh` — the `shape` source token and its wait line.
- `scripts/build_queue/build_why.py` — need and charge texts (report-unit).
- `docs/as-built/build-memory-admission.md` — the rule and the replay table (126 natedev builds, 14 days to 2026-10-30).
- Tests: `scripts/lint/test_build_shape.py`, `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_why.py`, `scripts/buildlog/test_test_phase_peak.py`.

**Binds later work:** `finalize_reservation` is the only path that ends a reservation and writes history; the anonymous-history reader and the test-phase charge build on it. `need_basis_record`/`read_need_basis` is the one codec for need bases; new bases (a band, a gate cap) join the `NeedBasis` union through it. `LifetimePromise` is the state to reuse for any promise held for a step's life. `index_shapes` takes no repository.

**Gotchas:**
- The index join cannot match on repository: `steps.repo` is the checkout folder, not the origin name.
- `live_reservations` deletes nothing: `merge_gate.py` calls it outside the lock. Neither caller of the finalizer takes the lock twice.
- A `.max` read error keeps every file for the next cleanup.
- `DEV_HEADROOM` is set by one small cargo-berth build (0.336 GiB history, 1.413 GiB peak); the constant's comment holds the measurement.
- All rows written before this change lack `outcome`, so no floor exists until a new failed row lands.

**Ruled out:**
- A headroom factor on the fallback — it gave dev builds 21 GiB.
- The largest same-shape row × 4.20 for every build — +146.7 GiB-hours, 77 promises above 21 GiB before the cap.
- A p90 with no floor — nine 10 GiB rows and one 15.76 GiB killed row give 13.14 GiB, not 20.7.

### Phase 25 — Build-why names when a recovery spacing hold ends  · status: done

#### As-built

- `memory_admit.py` has `HeadStartsAt(at, ended, charge_drop, freed, growth_drop)` and the alias `HeadRelease = HeadAdmitsAfterEnd | HeadStartsAt`; `HeadAfterEnd = HeadRelease | HeadStillHeld | NoWaiter | ReadingUnavailable`.
- `head_after_steps_end` returns `HeadStartsAt` with `at = next_start_at` when the decision holds only for a `MemoryPressure` with `next_start_at` (recovery spacing: one start per 30 s for 120 s after a pressure hold). A pressure hold with no `next_start_at` (`full avg10` or swap) stays `HeadStillHeld` with `pressure` in its causes, and so does recovery spacing with a failed budget or free check. It reads no files and uses admission's own `queue_decision`.
- `smallest_head_release` takes `HeadStartsAt` as a release found, as it takes `HeadAdmitsAfterEnd`. It returns `HeadStartsAt` with empty `ended` only when the snapshot with no endings gives it; otherwise the result keeps the endings the timer needs first.
- `build_why.release_text` writes the whole release line. For `HeadStartsAt` it prints `no running step needs to end; recovery spacing starts it at <HH:MM:SS zone>`, or `<steps> end (<drops>); then recovery spacing starts it at <HH:MM:SS zone>` when steps must end first. It adds `; the pressure hold or recovery spacing must also clear` for every other result under pressure, not for `HeadStartsAt`.

**Files:**
- `scripts/lint/memory_admit.py` — `HeadStartsAt`, `HeadRelease`, the release search.
- `scripts/build_queue/build_why.py` — `release_text` (owner report-unit).
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_why.py` — recovery spacing, mixed and pressure cases.
- `docs/as-built/build-memory-admission.md` — build-why paragraph: the `HeadStartsAt` text.

**Binds later work:** a new head result joins `HeadRelease` or `HeadAfterEnd`, and release-line text goes only in `build_why.release_text` (The memory queue starts steps in queue order and records when a step became first; Replay a looser head rule, and adopt it only on the replay). `HeadStartsAt` is per snapshot and not saved; build-why and `/build:queue`'s head explanation show it from the same `QueueRead`.

**Gotchas:** the type check that counts is the merge gate's form, `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python`; a bare run reports bpy and PIL errors. The merge gate's type job can go above its 4.000 GiB sampled cap; it passes when it runs alone.

### Phase 26 — The memory queue starts steps in queue order and records when a step became first  · status: todo
**Depends on:** Phases 31 and 33 (same file, `memory_admit.py`; 31 → 33 → 26 land one after another). Phase 24 (`31c3ee3e`), Phase 30 (`7106f93d`; the walk and the bump stop, Spec 2) and Phase 32 (`f8601d3c`; `require_showrunner` at `memory_admit.py` :479) are checkpointed. Runs before Phase 20, whose single head policy (Spec 2) then reads these fields. Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → **26** → 15/27 (queue-unit) → 39 → 20 → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

**Approved:** builds showrunner 2026-10-30 (two wrong results from the 158 min head wait; `head_since` and the token record for report-unit's Phase 4e).

#### Work Order

**Goal:** the user's bump is one queue priority that walks one unit's steps first until a set time; each waiting row says when it became first; a step that misses the CPU token records when it gave back its place in memory and when it got the token.

**Measured (2026-10-30, 03:50–06:28 EDT, natedev; `S/pressure_followup_report.md`):**
- The head waited 158 min. Causes: 88 queue bumps, each one restarting a bumped head's 600 s strict window (`bump_waiter` then set `priority_since = now`, `memory_admit.py` :2144 at that time; Phase 30 made it refuse every caller), and backfill windows where younger steps started in the order they polled, not their queue order.
- `queue_decision` admits a younger step in a backfill window on its own budget and memory checks; nothing checks whether an older waiter that also fits is waiting.
- `verify.sh` `buildlog_wait_for_memory` (:2630-2651 at `31c3ee3e`): when `try_token_now` fails, the step releases its memory reservation (`buildlog_release_memory 0`), waits in `acquire_token`, then waits for memory again with `BUILDLOG_MEM_WAIT_SINCE` from its first wait. Only the token wait seconds go to `TOKEN_WAIT_FILE`; no record holds the two times.

**User amendment (2026-10-31, the user's words via the builds showrunner; it overrides the Specs below where they differ):**
- Bump end time: "i never asked for a feature to have a bump until a period of time - it is non-sencial feature - if i want something else in front, i will bump it". So queue priority has no `until` and no expiry. It lasts until `priority clear`, or until a new `priority set` replaces it (a second `set` replaces the live record, and is not refused). Remove `--until`, the expiry pass and the expiry build-log row. Show `queue priority: <unit> (the user's bump)`.
- head_since: "if it sat first for 158 minutes and it had already been in the queue for 200 does it matter? it is still too long - i don't understand this one". So drop `head_since` as a separate measure: no `HeadSince`/`NotHead`/`HeadTimeNotRecorded` state, no `head_state`/`head_since` row fields, and no "became first" text. The total wait (from the first queue time) is what every reader shows. Tell queue-unit at the checkpoint that `head_facts.read_head_facts` no longer gets `head_since`.
- Give-back: "once it has both memory and tokens it just goes - if it doesn't have it, it stays in place until it has both- it should follow essentially the same rules - if it has been waiting for a short period, it lets things go through - otherwise it blocks until it gets what it needs". So a step that misses the CPU token keeps its queue place. It starts only when it holds both its memory and the token. While it waits, the same admission rules apply as for any waiter: inside the short (backfill) window, younger steps that fit may go; after it, the step blocks until it has what it needs (strict). No give-back that sends a step behind others. The token record (Spec 4) stays: it records `token_left_at` and `token_back_at`.
- Also: merge `origin/builds` (`50c45192` or later: queue_view, queue Phases 1, 2, 3, 7) and port to it. Queue Phase 5 lands without these fields; whoever lands second carries this phase's state into the queue service, one copy of each field. `memory_gate.sh`'s wait loop is now queue-unit's.

**Spec:**

1. **Backfill in queue order: moved to Phase 30** (builds showrunner 2026-10-30). Phase 30 builds one admission walk for every window, with `BehindOlderFit`, its readers and the release search. This phase adds queue priority, `head_since` and the token record (Specs 2–6).
2. **Queue priority: the user's bump** (builds showrunner 2026-10-30). **Depends on:** Phase 30 (the walk, the bump stop and its refusal) and Phase 32 (`require_showrunner` :479, the one showrunner caller check), both checkpointed.
   - `queue_priority.json` in the admission folder holds `{unit, why, set_at, until, set_by}`. Only one may be live; a second `set` while one is live is refused.
   - `memory_admit.py priority set --unit <unit> --until <HH:MM|ISO> --why "<what>"` and `memory_admit.py priority clear`. Only a showrunner-role session may run them, through that caller check; any other caller gets a plain refusal and a non-zero exit. `--until` is required; `HH:MM` is in the user's zone.
   - Scope: the roster's `unit_labels` mapping (`scripts/message/roster.py` :418; `unit_labels_for` :1037 resolves a Codex worker to its unit director), which includes the unit's Codex workers and its scratch-copy steps. It grants nothing else. The record stores only the unit name. The walk reads the unit's session and thread ids from that one mapping when a priority is live, and matches a waiter's `session_id` against them; no second copy of the roster's ids is kept (user rule 2026-10-30: no duplication). A Codex worker opened after `set` is covered, because the walk reads the current roster.
   - The walk sorts the priority unit's steps first, then `(since, pid)`. At `until` the record stops counting, and the next locked pass deletes it.
   - **One ordered snapshot.** Resolve the current roster and the priority record once at the snapshot boundary and produce one priority-aware ordered snapshot, used by admission (`check` :2326), `admit_head` (:525), the queue readers (`read_queue_pass` :2694, `explain_queue` :2824), the blocker lookup (`reservation_blocker` :2613, `blocker_snapshot` :2648) and the release search (`head_after_steps_end` :2829, `smallest_head_release` :2872). `admission_walk` (:2151) consumes that order and does not sort it again with `waiter_order` (:2125). Every order comparison reads the resolved order too, not only the sort: `snapshot_head_override` picks its head as the minimum by `waiter_order`, and `admission_for_waiter` and `admission_walk` compare `waiter_order` to tell which rows are younger (`memory_admit.py` :2189-2251 at `5c9b01fd`). Change all three to the resolved order, so a priority-first waiter is never judged against the old time order for override choice, younger-row tests or room accounting. The frozen `QueueExplanation` keeps the resolved order (each waiter's priority rank and session identity), so a hypothetical ending reads neither the roster nor `queue_priority.json` and reorders nothing. The snapshot keeps Phase 31's head-admission override (`HeadAdmissionOverride | NoHeadAdmissionOverride`) as it is, so check, the queue view, build-why and the release search still agree when a head is let in.
   - Delete `bump_order`, `holds_younger` set by position, `bump_waiter`, `BumpOff` (:1036) and `build_queue`'s bump subcommand. Today `build_queue.py bump` prints the `BumpOff` refusal on stdout and exits 3, and unit-board's pane sends `/build:queue bump …` to it and reads that stdout/exit-3 protocol (`mods/unit-board/hooks/register.tsx` :369-380). In the same checkpoint, remove or replace that pane dispatch and update `commands/build/queue.md` (its description, :2, and the `bump` paragraph, :13), so `/build:queue bump …` gives one line that names `/showrunner:bump` and never the generic `Build queue could not be read.` reply. The replacement reuses the priority command and its refusal writer and adds no second policy. Old `queue_bump` build-log rows stay readable (`head_facts._read_bumps` :187 and the index); nothing writes new ones.
   - One build-log row on each set, clear and expiry (`unit`, `until`, `why`, `set_by`), from one writer in `scripts/buildlog/record.py` beside `head_admission` (:971), in its style (Phase 30 removed `record.queue_bump`).
   - `build_why` and `/build:queue` show `queue priority: <unit> until HH:MM (the user's bump)`. The line is written once, in `scripts/build_queue/head_facts.py` (report 4e), the one writer of the head order text (`order_text` :388, whose bump suffix today reads `QueueBump` from old `queue_bump` rows); the build-why Order rule line (`build_why.py` :234) and `build_queue.head_reason_text` (:109) call it, and neither keeps its own copy.
   - `commands/showrunner/bump.md` (owner sdlc): step 1 runs `priority set`; step 5 runs `priority clear`.
3. **`head_since` on the waiter row.** The head state is closed: `HeadSince(at)`, `NotHead`, or `HeadTimeNotRecorded` for a row from before this phase. The row stores `head_state` (`head` or `not_head`) and, for `head`, `head_since` (epoch seconds); a row with no `head_state` reads as `HeadTimeNotRecorded`. No reader uses a bare optional time. The admission pass that finds a row first in queue order writes `HeadSince(now)` under the queue lock when the row is not already `HeadSince`; a row that is no longer first becomes `NotHead` (a queue priority step ahead moves it back). The queue view and `explain_queue` expose the typed value; `BehindHead.head_since` reads it instead of the head's queue time.
   - **Handoff across the token give-back.** Today the time is lost: admission deletes the waiter (:2488), release deletes the reservation, and `verify.sh` (:2651) carries only the first queue time into its second wait. So `check` copies the head state into the reservation row at admission. The give-back goes through one new command, `memory_admit.py give-back <reservation>` (shell function `buildlog_give_back_memory` in `memory_gate.sh`; `verify.sh` :2647 calls it in place of `buildlog_release_memory 0`). Under the lock it first writes `<pid>-<start>.handoff` (with `write_text_atomic`) holding the head state and `token_left_at`, bound to that owner's pid and start time, and only then ends the reservation through Phase 24's finalizer, `finalize_reservation`, which deletes the reservation before it returns. A stop between the two leaves a handoff and a live reservation: the next give-back or the dead-owner sweep finishes through the same finalizer and keeps the one handoff; no second finalize path. The next `check` of the same pid and start time takes the handoff under the lock: if its row is first in queue order at that poll, it keeps the handoff's `HeadSince`; if another step is first, it is `NotHead`. Then the handoff goes. A handoff whose owner is gone goes with the dead waiters (`live_waiters` :2096).
4. **Token record.** The `check` that takes the handoff (Spec 3) appends one JSON line to `${BUILDLOG_DIR}/admission/queue_times.jsonl`: `at` (epoch seconds, float, the write time), `kind` `token_gap`, `session_id`, `step`, `pid`, `start_time`, `token_left_at` (from the handoff) and `token_back_at` (`verify.sh` sets `BUILDLOG_MEM_TOKEN_BACK_AT` when `acquire_token` returns). `memory_admit.py` is the one writer of admission files. One append function writes every `queue_times.jsonl` line of every kind under the admission lock and stamps `at`, so the file is in `at` order; report 4e's reader, `head_facts._read_token_waits` (`scripts/build_queue/head_facts.py` :270; `_token_gap` :241), reads it backward and stops at the first line older than its oldest waiting step. When the file passes 8 MiB, the same append replaces it atomically with only the lines whose `at` is at or after now − 14 days (the need-history window), in order. `queue_times.jsonl` is the one record of queue times: Phase 20 Spec 0 adds every step's lifecycle events to it, with the head state at admission. A step that leaves the queue during its second wait still has its line, because the first poll of that wait writes it.
5. **Tests.** In `scripts/lint/test_memory_admit.py`: Spec 1's tests are in Phase 30; queue priority (Spec 2): a showrunner `set` works; another caller is refused; the priority unit's waiters walk first; a Codex worker's step and a scratch-copy step of the unit walk first; plain order returns at `until`; a second `set` is refused; `clear` works; the set, clear and expiry build-log rows are written; one ordered snapshot (Spec 2) gives the same order to admission, `admit_head`, the queue readers, the blocker lookup and the release search for a priority set, its expiry, a newly opened Codex worker and hypothetical endings with conflicting original wait times, and the release search reads neither the roster nor the priority file; an override head (Phase 31) still agrees across those readers; the `BehindOlderFit` cross-reader tests, `test_open_recovery_slot_reserves_first_fitting_waiter_in_each_window` and `AdmitHeadTests` pass unchanged; `head_since` is set when a row becomes first, cleared when a queue priority step moves ahead of it, set again when it is first again, a legacy row reads as `HeadTimeNotRecorded`; the full path check (admitted as head) → give-back → check keeps the earlier `HeadSince` when the step is first at once, and gives `NotHead` when it returns behind another head (Spec 3); the take of the handoff writes one `token_gap` line with both times and `at` (Spec 4); every line kind carries `at` in write order; a file past 8 MiB is trimmed to the last 14 days by `at` with order kept, and a line older than 14 days is gone (Spec 4). In `scripts/build_queue/test_head_facts.py`: the queue priority line; old `queue_bump` rows still render in the bump history. In `mods/unit-board/tests/queue.test.ts`: `/build:queue bump …` through the pane routing gives its one line and never `Build queue could not be read.`; the CLI test in `scripts/build_queue/test_build_queue.py` covers the same reply. In `scripts/direct/test_verify_token_wait.py`, on `VerifyTokenWaitFixture` (`verify_token_wait_test_support.py` :39): a step that misses the token runs check → give-back → token → check and writes one `token_gap` line with `token_left_at <= token_back_at`. Fake both memory readings.
6. **Docs.** `docs/as-built/build-memory-admission.md`: the Waiters paragraph (queue order in backfill, `head_since`), the Queue order paragraph (queue priority replaces bump) and the token record.

**Constraints from prior phases:**
- Queue-unit's /build:queue view (builds showrunner 2026-10-30: this phase merges after it): row marks come from `queue_view.py` `is_moved(model, position)` and `move_mark(model, position)` over `QueueModel` (`origin/builds-queue`). "Became first" (`HeadSince`) reaches the view's up arrow and countdown through that one function and its model; write no second mark or countdown writer. Merge `origin/builds` before the checkpoint and resolve the queue order text there. Only the user owns queue order: priority replaces bump as Spec 2 says, and no command lets a session move a step.
- Phase 10: the blocker lookup reuses `decide` and `queue_decision`; no second rule.
- Phase 20 (later) makes one head policy function; this phase's backfill rule and `head_since` go in `queue_decision` and the queue pass, so Phase 20 moves them with the rest.
- Report 4e (`be391b7a`) landed `scripts/build_queue/head_facts.py`: `read_head_facts` (:326) reads `head_since` from the first waiter row (:334), and `_token_gap` (:241) reads `token_left_at` and `token_back_at` from `queue_times.jsonl`, by these names; keep them. Tell queue-unit at the checkpoint.
- Phase 31: Rule A runs inside the walk and keeps its recovery slot, and the head-admission override is part of the one admission snapshot; this phase keeps both, and its order feeds that walk.
- Phase 24: a reservation ends only through its finalizer; the give-back uses it.
- Phase 16: the merge gate is a queue step; `gate_wait_reasons` reads its `CheckResult`.
- Phase 25: `head_after_steps_end` and `smallest_head_release` return `HeadStartsAt` for a recovery spacing hold, with the endings the timer needs first; `build_why.release_text` is the one writer of the release line.

**Files:**
- `scripts/lint/memory_admit.py` — Specs 2–4.
- `scripts/lint/test_memory_admit.py` — Spec 5.
- `scripts/lint/memory_gate.sh` — Spec 3, `buildlog_give_back_memory`.
- `scripts/direct/verify.sh` — Specs 3–4 (owner verify-unit, natedev production; trial-merge against `origin/natedev-verify`).
- `scripts/direct/test_verify_token_wait.py` — Spec 5, the verify.sh test on the existing `VerifyTokenWaitFixture`; no new verify test module (owner verify-unit).
- `scripts/build_queue/build_queue.py` — Spec 2: delete the bump subcommand; the queue priority line through `head_facts` (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/head_facts.py`, `scripts/build_queue/test_head_facts.py` — Spec 2, the queue priority line in the one order-text writer; old `queue_bump` rows stay readable (owner queue-unit; trial-merge against `origin/builds-queue`).
- `commands/build/queue.md` — Spec 2, the description and the `bump` paragraph (owner queue-unit; trial-merge against `origin/builds-queue`).
- `mods/unit-board/hooks/register.tsx`, `mods/unit-board/tests/queue.test.ts` — Spec 2, the `/build:queue bump` pane dispatch (the `/build:queue` routing came with builds-report phase 1, `54d54d58`; owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/test_build_queue.py` — the reason and queue priority tests (owner queue-unit).
- `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py` — Spec 2, the queue priority line (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py` — Spec 2, the queue priority writer (owner report-unit; trial-merge against `origin/builds-report`; Phase 4 committed its `sweep_trim` writer, `e9f7cd0a`).
- `commands/showrunner/bump.md` — Spec 2, steps 1 and 5 (owner sdlc).
- `docs/as-built/build-memory-admission.md` — Spec 6.

**Workers:** `1 writer + 1 tester`
- `impl` — admission policy and state (`memory_admit.py`), the verify handoff (`memory_gate.sh`, `verify.sh`), the queue readers (`build_queue.py`, `build_why.py`, `head_facts.py`), the unit-board pane dispatch and `commands/build/queue.md`, the build-log writer (`record.py`), `bump.md` and docs.
- `test` — `test_memory_admit.py`, `test_build_queue.py`, `test_build_why.py`, `test_head_facts.py`, `test_record.py`, the pane routing test `queue.test.ts` and `test_verify_token_wait.py`, on the existing shared fixture `VerifyTokenWaitFixture`.

**Acceptance gate:**
- The memory admission, build-queue and verify token-wait tests pass; `basedpyright` over `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`, `scripts/build_queue/build_queue.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py`, `scripts/build_queue/head_facts.py`, `scripts/build_queue/test_head_facts.py`, `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py` and `scripts/direct/test_verify_token_wait.py` reports `0 errors, 0 warnings, 0 notes`; `bash -n scripts/lint/memory_gate.sh scripts/direct/verify.sh`; the merge gate is green.
- The notice has `also touches` lines for `verify.sh` and `test_verify_token_wait.py` (verify-unit, against `origin/natedev-verify`), `build_queue.py`, `build_why.py`, `head_facts.py`, `commands/build/queue.md`, `mods/unit-board/hooks/register.tsx` and their tests (queue-unit, against `origin/builds-queue`), `record.py` and its test (report-unit, against `origin/builds-report`), and `commands/showrunner/bump.md` (sdlc).
- The unit-board pane test passes: `/build:queue bump …` gives its one line, never the generic failure; old `queue_bump` rows still render; the `BehindOlderFit` cross-reader tests, the recovery-slot test, `AdmitHeadTests`, `test_fitting_strict_head_keeps_its_place_when_younger_polls_first`, `test_frozen_release_preserves_negative_queue_order_for_an_override` and `test_build_why.py` `test_override_free_memory_terms_add_up_to_its_printed_threshold` pass unchanged, and an override head agrees across every reader.
- Give-back interruption tests: a stop after the handoff is written and before finalization, and a stop after finalization and before the next check takes the handoff; each ends with one finalized reservation and the handoff's `HeadSince` and `token_left_at` kept.
- Change-Review line: the user's bump walks one unit first until a set time | on natedev, from the builds showrunner session, run `~/.claude/scripts/lib/py ~/.claude/scripts/lint/memory_admit.py priority set --unit <a waiting unit> --until <HH:MM> --why "<what>"`, then type `/build:queue` | that unit's waiting steps are first, the queue shows `queue priority: <unit> until HH:MM (the user's bump)`, and each first row shows when it became first

### Phase 27 — The merge gate holds its whole memory cap in the admission queue  · status: moved
**Moved:** to queue-unit (builds showrunner 2026-10-31): one owner for queue Phase 5's hub file and a shorter path to the cutover. No new work on it here.
**Depends on:** devtools-unit's merge-gate checkpoint that raises the gate memory cap to 6 GiB (`a70f99e6`), which is on `main`, `origin/builds` and this branch: `merge_gate.py` :72 has `DEFAULT_MEMORY_CAP_MIB = 6144`. Phase 15 (same files, `memory_admit.py` and `build_why.py`; 26 → 15 → 27 land one after another). Merge `origin/builds` first. Global order: Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15/**27** (queue-unit) → 39 → 20 → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

**Approved:** builds showrunner 2026-10-30 (natedev item: after devtools' 6 GiB cap lands on `main`, each gate holds its cap as one queue row from start to end, with a test).

#### Work Order

**Goal:** a merge gate reserves its whole memory cap as one row in the admission queue from its start to its end, so a build that starts beside it never counts on room the gate can still use.

**Measured (2026-10-30, `builds-resources` `01441cc0`):**
- `merge_gate.py` `gate_reservation` (:359-372) opens one `GateProfile` row with `NoGateHistory(cap)`. `memory_admit.py` `check` (:2336-2337 at `31c3ee3e`) replaces that estimate every `HEAD_SECONDS` with `gate_estimate` (:989): the p90 of the host's `merge-gate` rows in `admission/anon_peaks.jsonl` over 14 days, else the cap. So the row promises the p90 peak, not the cap.
- Inside the gate, its own admission lets its modules use up to the cap. The Phase 25 gate on 2026-10-30 sampled 4.050 GiB against its 4.000 GiB cap. The gate can use its whole cap; its queue row promises less.
- `build_why.py` `need_text` (:79-92) prints `fallback: fewer than 5 runs` for a `fallback` source, which is the gate row's source with no history today. That text is not true for a row sized by the cap.
- The running charge: `step_memory` (:1812) sets the promise only during `RAMP_SECONDS` (120 s). After that, `charge_basis` charges current memory or the decaying `.max` peak, and `growth_headroom` holds no unused cap. So a 6 GiB gate that uses 1 GiB lets a 20 GiB request start under the 21 GiB budget.

**Spec:**

1. **The row holds the cap.** `GateProfile.peak` is `ExpectedPeak(cap_bytes, "cap", 0)` for every check, from start to end. `"cap"` is a new closed value of the need source (`NeedSource` :130), used only by the gate. Define a semantic gate-cap basis and carry it through `ExpectedPeak` (:282), the waiter record, the existing basis codec (`need_basis_record` :1352, `read_need_basis` :1370; today it accepts only the four legacy sources) and `RecordedNeed` (:791); add no second codec. Remove `gate_estimate`, `MeasuredGatePeak`, `NoGateHistory`, `GateEstimate`, the `estimate` and `estimated_at` fields and their refresh in `check`, after a grep of the tree shows no other reader. The `merge-gate` rows in `anon_peaks.jsonl` are still written for report-unit; only the read for the estimate goes.
   - **The cap stays charged until the gate ends.** A gate row gets Phase 24's `LifetimePromise(need)`: Phase 24's one promise decision in `step_memory` (from the reservation row's shape) gains one case, a row with step `merge-gate`. Reuse `LifetimePromise` (:321) for charging; write no second promise rule. Both checks read it: `charge_basis` for the budget charge (`max(cap, current memory, observed peak)`) and `growth_headroom` for the free-memory `promised` term (cap − current memory). The 120 s ramp and the `.wave` peak decay no longer lower a gate's charge.
2. **One cap value.** `gate_reservation` sizes the row from the gate's own cap (`--memory-cap-mib`, default `DEFAULT_MEMORY_CAP_MIB`, 6 GiB after devtools' change) and nothing else.
3. **Text.** `need_text` prints `the merge gate's memory cap` for the `"cap"` source; every other source keeps its text. `build_why.charge_text` renders `LifetimePromise` as `release build promise: holds until the step ends`; make that one rendering name the step's kind (a release build or the merge gate) from the row it already reads, with no second promise type or charging rule, and test both texts in `scripts/build_queue/test_build_why.py`.
4. **Tests.** In `scripts/lint/test_memory_admit.py`: a gate row holds `cap_bytes` with ten `merge-gate` history rows at 2 GiB; the row keeps the cap on a check after `HEAD_SECONDS`. A 6 GiB gate that uses 1 GiB, past `RAMP_SECONDS`: its charge is `LifetimePromise` 6 GiB and its growth term 5 GiB, a 20 GiB waiter under a 21 GiB budget is held, and admission (`check`), the queue view (`read_queue`, `explain_queue`) and the `blocker` line agree that the full cap stays reserved. The same after a peak falls: `.max` at 5 GiB with its last near-peak time older than `PEAK_DECAY_SECONDS`, the charge stays 6 GiB in all three. In `scripts/production/test_merge_gate_suites_admission.py`: the gate's reservation equals `--memory-cap-mib` from start to its last module. In `scripts/build_queue/test_build_why.py`: a gate row's need line reads `the merge gate's memory cap`. The gate-cap basis round-trips through the basis codec, and its queue and build-why text is the same before and after the former `HEAD_SECONDS` refresh interval. This Work Order approves replacing the tests that assert `MeasuredGatePeak` or `NoGateHistory` (`test_memory_admit.py` `GateProfileTests` :2027-2120, `test_merge_gate_suites_admission.py` :176-209); put one `Assertion-Change:` line per replaced test in the summary. Fake both memory readings.
5. **Docs.** `docs/as-built/build-memory-admission.md`: the gate paragraph says the row holds the cap, and its charge holds it until the gate ends in both checks. `docs/as-built/merge-gate-and-test-speed.md`, the Shared admission bullet (:66): replace the estimate text (`With no gate history it asks for --memory-cap-mib; otherwise it uses the gate peaks' p90 ...` and `A wait that reaches 600 seconds refreshes that estimate ...`) with the cap contract: the reservation asks for the gate's `--memory-cap-mib` (default `DEFAULT_MEMORY_CAP_MIB`) and holds it, in the budget and free-memory checks, from start to end, with no history estimate and no refresh; a wait keeps its gate identity and queue place; release still writes the gate's `merge-gate` history row, which admission no longer reads.

**Constraints from prior phases:**
- Phase 16: a gate holds one queue row from start to end through `gate_reservation`; keep that one row.
- Phase 14: a gate run again after a stop finishes, and its row is released on every exit path.
- Phase 24: `MeasuredMemory` carries `RampPromise | LifetimePromise | NoPromise`, set in `step_memory` from the reservation row; `LifetimePromise` counts in `charge_basis` and `growth_headroom`; `build_why.charge_text` says the promise holds until the step ends.

**Files:**
- `scripts/lint/memory_admit.py` — Spec 1.
- `scripts/lint/test_memory_admit.py` — Spec 4.
- `scripts/production/merge_gate.py` — Spec 2 (owner natedev showrunner; trial-merge against `origin/natedev`).
- `scripts/production/test_merge_gate_suites_admission.py` — Spec 4 (owner natedev showrunner).
- `scripts/build_queue/build_why.py` — Spec 3 (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/test_build_why.py` — Spec 4 (owner queue-unit).
- `docs/as-built/build-memory-admission.md` — Spec 5.
- `docs/as-built/merge-gate-and-test-speed.md` — Spec 5, the Shared admission bullet (owner natedev showrunner; trial-merge against `origin/natedev`).

**Workers:** `1 writer + 1 tester`
- `impl` — gate sizing (`merge_gate.py`), the admission charge and the gate-cap basis (`memory_admit.py`), the explanation (`build_why.py`) and both docs.
- `test` — `test_memory_admit.py`, `test_merge_gate_suites_admission.py` and `test_build_why.py`.

**Acceptance gate:**
- The memory admission, gate admission and build-why tests pass; `basedpyright` over the six Python files reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The `need_basis` codec and rendering tests pass with the gate-cap basis, and the existing `LifetimePromise` tests pass unchanged; the gate extends that one rule.
- The notice has `also touches` lines for `merge_gate.py`, its test and `merge-gate-and-test-speed.md` (natedev showrunner, against `origin/natedev`) and for `build_why.py` and its test (queue-unit, against `origin/builds-queue`).
- Change-Review line: a merge gate holds its whole memory cap in the queue | on natedev, while a merge gate runs, type `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_why.py` | the gate's row shows a 6.0 GiB need from `the merge gate's memory cap`, and its charge stays 6.0 GiB after its first 2 min while it uses less, from its start to its end.

### Phase 28 — The working-set command files a Node helper under its own script  · status: done

#### As-built

- `NODE_VALUE_OPTIONS` (`working_set.py`) is the one list of space-form value options, read from Node v24.19.0 `--help`. `node_entry(arguments) -> str` skips a second argument only for an option in that list; an equals form skips nothing. `node --redirect-warnings <file> <script>` names `<script>`.
- `--eval`, `--print`, `-e`, `-p`, `--run` and `--run=` return `NO_NODE_ENTRY` (`""`), so a `node --run <task>` process has no entry script and shows as `other`.
- `classify_process(arguments) -> ProcessClassification` (frozen: `category`, `node_script`) feeds both classification and report grouping; `classify` returns its `category`. A Node helper gets script and owner detail rows like a Python helper; detail groups key on category and script.

**Files:**
- `scripts/build_targets/working_set.py` — option list, `node_entry`, `classify_process`, report grouping.
- `scripts/build_targets/test_working_set.py` — one table-driven case per value option, `--run` cases, rendered Node rows.

**Gotchas:**
- The option list is a snapshot of Node v24.19.0; a newer Node can add value options and misfile its helpers.
- A parsed entry script is not proof it reaches the table; only a rendered-output test shows the Node rows.

**Ruled out:**
- Skip one argument per option: it files `--redirect-warnings <file>` under the file.

### Phase 29 — The floor sweep keeps targets used in the last hours  · status: done

#### As-built

- `hold_floor` (`scripts/lint/sweep.py`) takes idle targets least recently used first: the sort key is (target last use, then `unit_age`). The floor has no recency window and no `floor_recent_hours` key.
- Each real scan under the floor writes one `floor_scan` build-log row through `record.floor_scan`: the free space after the scan, the floor, the shortfall, and the idle targets oldest first (folder, last use, bytes). A `--dry-run` writes no row.
- `recent_minutes` in `[sweep]` orders only the units inside one target at its budget.

**Files:**
- `scripts/lint/sweep.py` — the floor order and the `floor_scan` write.
- `scripts/lint/test_sweep.py` — floor order, `floor_scan` and dry-run tests.
- `config/lint.conf` — the `[sweep]` comment: `recent_minutes` is the budget's unit order only.
- `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py` — the `floor_scan` writer beside `sweep_trim`, and its test.
- `docs/as-built/build-targets.md` — Build output retained by the sweep holds the floor rule; `docs/as-built/build-memory-admission.md` points there.

**Gotchas:** a dry run during a live sweep is refused by the floor lock; run it after the live sweep ends.

**Ruled out:** a floor recency window (`floor_recent_hours`) — a target used inside any window is always newer than every target outside it, so the window never changes least-recently-used order.

### Phase 30 — Waiting steps start in queue order  · status: done

#### As-built

- `admission_walk(available, total, ordered, running, head, pressure, *, recovery, now, room_used=0, override=..., limits=...) -> list[WaiterAdmission]` (`memory_admit.py`) sorts the waiting snapshot by `waiter_order` and walks it once, in every window. It marks a row when the row fits after the running charge plus the needs of the rows marked before it (memory budget, free-memory check, pressure, run-alone rule). Only a marked row admits, so a younger row that polls first cannot take an older fitting row's room.
- A row that fits alone but loses its room to an older marked row holds with `BehindOlderFit(older_name, older_since, older_session_id)`, beside `BehindHead`. In a strict window a row behind the head still holds with `BehindHead`; the head's drained start and the user's admit-head override do not change.
- While a recovery start slot is open (`RecoveryStarts`, `now < ends_at`), the walk marks only the first fitting row.
- `queue_decision` takes the walk's arguments plus `row` (after `total`) and returns that row's entry; it adds the row to the snapshot when the row is not in it. `read_queue_pass` walks once for all rows. `head_after_steps_end` and `smallest_head_release` pass the snapshot order and stay read-only.
- Every reader shows the one reason as `behind older step <label>` (`older_fit_text`): `check`/`CheckResult`, `reservation_blocker`, `blocker_snapshot`, `read_queue_pass` (`WaitReason`), `explain_queue`, `build_queue.reason_text`, `merge_gate.gate_wait_reasons` and the shell summary, which puts the text first so the field count stays.
- `bump_waiter(_selector, _actor) -> NoReturn` raises `BumpOff(ValueError)` with `only the user can bump; bump is off until /showrunner:bump sets queue priority`. It takes no lock, reads nothing and writes nothing. `build_queue.py bump <selector>` calls it before any queue or roster read, prints `build queue: <text>` as one stdout line and exits 3. `bump_order` in existing rows still orders them.
- `WaiterRowSelector`, `UnmatchedWaiter`, `bump_selector`, `refusal_text` and `record.queue_bump` are gone; the index still loads old `queue_bump` rows.

**Files:**
- `scripts/lint/memory_admit.py` — `admission_walk`, `queue_decision`, `BehindOlderFit`, `BumpOff`, `bump_waiter`.
- `scripts/build_queue/build_queue.py` — older-fit text in `reason_text(…, facts)`; bump refusal before any read.
- `scripts/production/merge_gate.py` — `gate_wait_reasons` names the older step.
- `scripts/buildlog/record.py` — no `queue_bump` writer.
- `docs/as-built/build-memory-admission.md`, `commands/build/queue.md` — the walk and the bump refusal.
- Tests: `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_queue.py`, `scripts/build_queue/test_build_why.py`, `scripts/production/test_merge_gate_suites_admission.py`, `scripts/buildlog/test_record.py`.

**Binds later work:** Rule A's room check is one more check inside `admission_walk` and keeps the recovery slot. The one head policy function takes `admission_walk` with its `recovery` argument. Queue priority reaches the walk through one priority-aware order; the walk keeps no second order. No bump writes `priority_since` until queue priority lands; queue priority deletes `bump_waiter`, `BumpOff`, the bump subcommand, the unit-board pane's bump dispatch and the `commands/build/queue.md` bump text together, and its writer goes beside `head_admission` in `record.py`.

**Gotchas:**
- Every `queue_decision` caller passes the ordered snapshot plus `recovery` and `now`; a caller that passes one row alone loses the queue order.
- The unit-board pane (`mods/unit-board/hooks/register.tsx`) reads a bump refusal only as exit 3 with one stdout line.

**Ruled out:**
- Bump refusal on stderr with exit 2 — it breaks the unit-board pane protocol.
- A second `BehindHead` text in the shell summary.

### Phase 31 — The strict window lets younger steps use the budget minus the head's need  · status: done

#### As-built

- Rule A: in a strict window, a row behind the head admits when (1) it fits now with no head term (budget or run-alone rule, free memory, pressure) and (2) the steps admitted since the head took priority, plus marked older rows, plus its own need total at most budget − head need. The head passes the budget check when the steps running at its priority time end.
- "Admitted since priority" is a live reservation with `admitted_at` (ms) at or after `priority_since`, so a step admitted in the same `check` that set it counts, as does a backfill-slot step; each counts at the larger of its `need` and `build_charge`. No new head time or row field.
- `decide` reports a strict head's room as one closed result, `HeadRoom(head_need, used, room, passes) | NoHeadRoom`, carried on `AdmissionDecision`; `check`, `queue_view`, `explain_queue`, the blocker lookup and the shell wait line read it. `CheckResult.head_need` is set only when the head's room alone holds the step.
- `admission_walk` checks an admitting later row against running memory plus every marked older promise, so a fitting strict head keeps its room when a younger step polls first (the younger holds with `BehindOlderFit`). Younger rows that fail check (1) get `NeedsBuildBudget`, `NeedsMemory` or `MemoryPressure`, not `BehindHead`.
- The head-admission override is captured in the one queue snapshot (`snapshot_head_override`) and applied through the shared decision path; its free-memory check sets `promised=0`, so build-why's printed terms add up to its threshold. The override never passes to another waiter, and pressure still holds it.
- One function in `memory_admit.py` writes the window clause: backfill `younger steps may start until HH:MM`; strict `younger steps may use <budget − head need> GiB of the budget until HH:MM`; strict with head need at or above budget `younger steps wait until HH:MM`. `head_wait_text` and `head_facts.order_text` call it.

**Files:**
- `scripts/lint/memory_admit.py` — `decide`, `admission_walk`, `queue_decision`, `snapshot_head_override`, `head_after_steps_end`, the window clause.
- `scripts/build_queue/build_queue.py`, `head_facts.py`, `build_why.py` — readers of `HeadRoom`, the override terms and the window clause.
- `docs/as-built/build-memory-admission.md` — Rule and Waiters paragraphs.
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_build_why.py` — Rule A, room sum, override and cross-reader tests.

**Binds later work:** Admission results are the closed `HeadRoom | NoHeadRoom`. `admission_walk` counts every marked older promise; a waiting head is never running memory. The override lives in the one snapshot with `promised=0`. `head_after_steps_end` keeps captured queue order fields and rebuilds legacy head fields only when no ordered snapshot exists. The head rule replay and queue order work keep `test_fitting_strict_head_keeps_its_place_when_younger_polls_first`, `test_override_free_memory_terms_add_up_to_its_printed_threshold`, the recovery-slot test and the override cross-reader tests as gates.

**Gotchas:** A waiting head's room counts once, as a marked older promise; dropping it from later rows' reservations lets a younger step take its place.

### Phase 32 — The user can admit the waiting head  · status: done

#### As-built

- `memory_admit.py admit-head [--why "<text>"]` runs `admit_head(why) -> str`. `require_showrunner() -> str` is the one caller check: it refuses with `AdmitHeadRefused` unless `CLAUDE_CODE_SESSION_ID` is a registered showrunner and `CODEX_THREAD_ID` is unset.
- It replaces one `admission/admit-head.json` record that names the first waiter by file name, pid and start time. Nothing waiting gives a refusal and no record.
- The next locked `check` admits that same head above the build budget when `MemAvailable >= need + reserve`. Pressure, recovery spacing and estimate expiry still hold it. The record stays while memory is short, and is deleted when the head starts or on the next locked check after it leaves. It never admits another step or changes order.
- `record.head_admission(action, unit, step, need, free, reserve, why, set_by, at)` writes `set`, `admitted` or `lapsed` rows.

**Files:**
- `scripts/lint/memory_admit.py` — caller check, record, override in `admission_for_waiter`.
- `scripts/buildlog/record.py` — `head_admission`.
- `scripts/lint/test_memory_admit.py` — `AdmitHeadTests`.
- `docs/as-built/build-memory-admission.md` — "Admit the waiting head".

**Binds later work:** queue priority (`priority set`) reuses `require_showrunner`; new build-log writers follow `head_admission`. The override is part of the one admission snapshot that check, queue view, build-why, blocker lookup and release search share; `AdmitHeadTests` gates admission changes.

**Gotchas:** `check` reads the override once per pass; `snapshot_head_override` matches it to the head and passes it on as a value, so no reader in the pass reopens the file.

**Ruled out:** a unit label rule in `admit_head` apart from the queue view's `waiter_unit_name`.

### Phase 33 — A history row counts only when it is whole  · status: done

#### As-built

- `store.whole_rows(handle, start)` is the one reader of whole JSONL rows from an offset: it starts at the next row boundary, skips cut-short, non-UTF-8 and non-object rows, and yields objects. A row counts as recorded only when it ends in a newline.
- `anonymous_history_rows` (all estimate reads of `anon_peaks.jsonl`), the reservation-id check in `finalize_reservation` and the sweep's trim recovery all read through `whole_rows`.
- The finalizer repairs a cut-short tail under the store lock before it checks for its reservation id, and appends through that locked handle when the id is absent.
- A sweep trim keeps a write-ahead intent until its `sweep_trim` row is in the build log. An intent whose time cannot be published is set aside as `DamagedTrimIntentSetAside(path)` (names the kept `.damaged-` file), and a later take of the target proceeds. Recovery counts deleted bytes by the same rule as a live take.
- Admission records are written with `write_text_atomic`; `write_admission_record` no longer exists.

**Files:**
- `scripts/buildlog/store.py`, `scripts/buildlog/test_store.py` — `whole_rows` (report-unit files).
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py` — trim intent, recovery, `DamagedTrimIntentSetAside`.
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py` — history reads and finalizer through `whole_rows`; `write_text_atomic`.

**Binds later work:** history rows are read only through `store.whole_rows(handle, start)`; no second decode loop; the unfinished-JSON and split-UTF-8 tests stay. The trim write-ahead intent and `DamagedTrimIntentSetAside(path)` stay, with `test_damaged_trim_outcome_names_the_preserved_intent` as a gate.

**Gotchas:** an intent time is checked with `store.utc_iso` at read time; a millisecond epoch passes a range check and fails only at publication.

### Phase 34 — The unused shared build folder goes  · status: done

#### As-built

- No code, test or as-built text names a shared Cargo build folder per repository. The verify-copies measurement spent 68.7 of 69.0 s waiting on the shared folder's build lock, so the copies rule picks seed and `copies.py` has no shared mode.
- `scratch.py` has no `shared_build_folder`, `SharedBuildFolder`, `NoSharedBuildFolder` or `SCRATCH_SHARED_BUILD_ROOT`. `scratch.register_copy` takes no `shared_target` and new copy records carry no such field.
- Readers load old copy records that still carry `shared_target` and ignore the field.
- `style_sync.py`'s own `style-sync-target` and the sweep's general Cargo `build.build-dir` template reading are unchanged.

**Files:**
- `scripts/lib/scratch.py` — copy records and cache markers; no shared folder.
- `scripts/lib/test_scratch.py` — `test_copy_record_with_a_legacy_shared_target_still_loads` pins the legacy-field read.
- `scripts/lint/test_sweep.py` — no shared-folder discovery test.
- `docs/as-built/build-targets.md` — "Copy records and lifetime" says each copy has its own build folder.

**Gotchas:** a grep for `shared_target` in `scripts` also finds unrelated account-link (`test_accounts.py`) and style-sync names; only the legacy-record test in `test_scratch.py` refers to copy records.

**Ruled out:** a shared per-repository build folder for verify copies — its build lock serializes the copies.

### Phase 35 — The hold wait line names the hold the session waits on  · status: done

#### As-built

- `build_targets.hold_waited_on` picks the hold a session waits on: the oldest hold in force whose key is not the caller's own. It returns `NoBlockingHold` when no hold blocks the caller. A caller with no owning session gets the oldest hold in force.
- The wait line, the check and launch lines and the 2 h line name the holder and reason of that hold. The words of each line stay the same.
- `holds_only_own` and `caller_holds_only_own` use the same choice over one `natedev_holds` read, so the wait decision and the named hold cannot disagree.
- When the caller owns the older of two holds, the line names the other session and its reason. The caller's own hold alone gives no wait.

**Files:**
- `scripts/build_targets/build_targets.py` — `hold_waited_on`, `NoBlockingHold`, and the wait, check and launch lines.
- `scripts/build_targets/test_build_targets.py` — scratch-hold cases for owner, peer and later sessions and for callers, among them `test_wait_and_launch_lines_name_the_oldest_hold_the_caller_does_not_own`.
- `docs/as-built/build-targets.md` — the named-hold bullet.

**Gotchas:** `scripts/direct/verify.sh` prints no `is held by` line of its own; the line comes only from `build_targets.py`.

**Ruled out:** a change in `verify.sh` — it has no held-by line to fix.

### Phase 36 — The floor's dry-run lines show the takes in take order, and a suite run from the installed copy stops with one line  · status: done

#### As-built

- `hold_floor` (`scripts/lint/sweep.py`) prints one line per target in the order of the first take from that target (`taken` insertion order, built from `chosen`), on a dry run and a real run. `shrink_floor` takes orphan batches of every target before unit batches, so a target with orphans prints before an older target with units only.
- `override_guard.is_installed_file(script, relative, home) -> bool` is the one installed-file test (same file, links followed). `refusal()` and `require_checkout(script: Path) -> None` both use it, with the home from `user_home()`.
- `require_checkout` raises one `RuntimeError` at import when the file is under `<home>/.claude/scripts`: `<suite file>: this suite runs from a checkout, not from the installed scripts under <home>/.claude/scripts; run it from a worktree.` A checkout path passes with no file read beyond the path check. It never skips: a skipped suite reads as passed in a gate.
- Eight modules call `require_checkout(Path(__file__))` at import, after their own path setup: `memory_test_support.py`, `build_hold_test_support.py`, `test_hold_applies.py`, `test_cargo_wrapper.py`, `test_hold_enforcer.py`, `test_targets.py`, `test_nextest_budget.py`, `test_nextest_width.py`.

**Files:**
- `scripts/lint/sweep.py` — the floor line order.
- `scripts/lint/test_sweep.py` — `test_floor_report_lists_targets_in_take_order_in_both_modes`.
- `scripts/build_hold/override_guard.py` — `is_installed_file`, `require_checkout`.
- `scripts/build_hold/test_override_guard.py` — `test_installed_suite_stops_with_one_checkout_instruction`, `test_checkout_suite_passes_without_reading_file_contents`, `test_wired_modules_keep_their_checkout_import_forms` (19 start forms across the eight modules).
- `scripts/lint/memory_test_support.py`, `scripts/build_hold/build_hold_test_support.py`, `scripts/build_hold/test_hold_applies.py`, `scripts/build_hold/test_cargo_wrapper.py`, `scripts/build_hold/test_hold_enforcer.py`, `scripts/build_targets/test_targets.py`, `scripts/lint/test_nextest_budget.py`, `scripts/lint/test_nextest_width.py` — the import-time call.
- `docs/as-built/build-targets.md` — the Tests bullet names the check and its line; `docs/as-built/build-memory-admission.md` — floor lines in take order, oldest use first.

**Binds later work:** "The floor takes an idle target that holds no build unit whole, in last-use order" keeps the first-take line order for whole takes. "Shared build state is written only by the installed copy" reuses `user_home` and `is_installed_file` for its writer guard, keeps `require_checkout`, and keeps the installed-stop, checkout-path and wired-import-form tests green; `require_checkout` guards test entry points only, not production writers.

**Gotchas:**
- A module that calls `require_checkout` at import must set its import path first. Package import (`scripts.<dir>.<file>`), `unittest discover` and a direct `python3 <file>` run must all load; only a subprocess run from the checkout with no `PYTHONPATH` shows a break.
- The installed copy is main's: a run from `~/.claude` shows the new line only after this reaches main.
- `scripts/lint/test_invoke_scope.py` and the suites under `scripts/direct/`, `scripts/production/`, `scripts/mac_test/` and `scripts/hooks/` do not call the check; their owners add the same import.

**Ruled out:** removing or loosening the installed-script guard (builds 2026-10-31: the guard stays); a skip in place of the error; wiring `test_invoke_scope.py` here (verify-unit owns it).

### Phase 37 — The floor takes an idle target that holds no build unit whole, in last-use order  · status: done

#### As-built

- `shrink_floor` (`scripts/lint/sweep.py`) takes `FloorTake = Group | WholeTargetTake`. An idle target with no build-unit group is one `WholeTargetTake`: the target root and the walk's observed entries, inodes, output versions and retained paths. Orphan batches of every target go first. Then unit batches and whole takes go together in `target_last_use` order, least recently used first. A target with no unit and no use stamp uses the newest mtime of its top-level entries as its last use. The floor takes only the shortfall and never a held target.
- A whole take uses the one deletion path: `recoverable_trim` (write-ahead intent, `sweep_trim` row) → `reclaim` → `remove`. It runs under the same target lock and rechecks as a group batch (busy, build hold, process use, cargo lock, a newer use stamp). `record_target_deletes(paths, *, known_targets=())` writes the target root to `target-deletes.json` before the markers go, so `memory_admit.py` refuses a warm promise for that target.
- A whole take keeps the target folder and its lock files: `.claude-busy`, `LOCK_NAMES` in each build tree, and `doc/.lock`. It takes `doc/.lock` with `try_lock`. When rustdoc holds it, the target is busy for this pass.
- A cargo target inside another target's folder, outside that target's build trees, is its own target with its own last use, busy check and batches. `TargetOwnerIndex` maps each path to its deepest target once per pass (prefix check first; `/proc` paths skip `realpath`). `build_trees`, `process_uses` and `walk` use that one owner. `busy_bytes` and `after_caches` count a nested target's bytes once.
- `unit_hash(name) -> UnitHash | NoUnitHash` gives a linker temp `.<name>-<hash>.<pid>` in `deps/` to its unit. `copied_name`, `copied_up` and `group_roots` keep their result for every other name.
- A floor dry run takes no `floor.lock`, so a real pass does not stop beside a dry run.
- The report line stays `lint sweep: the floor took <gib> from <target>, last used <when>` (`would take` on a dry run), in take order.

**Files:**
- `scripts/lint/sweep.py` — floor candidates, `WholeTargetTake`, `TargetOwnerIndex`, `unit_hash` states, the dry-run lock rule.
- `scripts/lint/test_sweep.py` — whole-take order, nested targets, locks, recovery, byte totals, linker temps, a dry run beside a held `floor.lock`.
- `docs/as-built/build-targets.md` — Build output retained by the sweep: "a target folder is disposable; keep nothing you need in it", the whole take, nested targets, linker temps.
- `docs/as-built/build-memory-admission.md` — Disk floor: one pointer line to `build-targets.md`.

**Binds later work:** "The floor_scan row shows takeable bytes and the reason for the rest" computes a whole take's `takeable_bytes` from that target's candidate inodes, fixes the nested-mtime last use, adds a cheap one-target recheck that keeps the nextest and deepest-owner checks, and adds the `floor.lock` hold alert. `TargetOwnerIndex` stays the one path-to-target map.

**Gotchas:**
- One process recheck costs about 0.21 s with 1830 pids. The natedev dry run makes 221 takes, most of them small whole takes (0.0 GiB lines), and runs 292 s (44 s with 4 takes before).
- Unit tests at 20-40 targets do not show a square term: an owner check per (pid, target) is O(P×T²). A floor change needs a real-scale dry run (floor 400, the same config) in its acceptance.
- Never run a real floor on natedev's roots from a test or by hand; tests use scratch roots. Run `test_sweep.py` from the worktree, never from `~/.claude`.

**Ruled out:** pruning `doc/` or `mend-wrapper/` inside a target that still has units; a path rule for session scratchpad clones (the order is by last use only); whole takes ahead of orphans (orphans also cost no rebuild).

### Phase 38 — The floor_scan row shows takeable bytes and the reason for the rest  · status: todo
**Depends on:** Phase 26 (`scripts/buildlog/record.py`, `scripts/buildlog/test_record.py`); Phase 37 (`scripts/lint/sweep.py`)

**Approved:** builds showrunner 2026-10-31, part 4 of the floor pass-over work; it lands after Phase 26 because Phase 26 holds `record.py`.

#### Work Order

**Goal:** A `floor_scan` row tells what the floor could take from each idle target, so a reader does not take all bytes for takeable bytes (on 2026-10-31 the row showed 63.7 GiB "older" that the floor could not take).

**Spec:**

1. **Row fields.** Each target in the `floor_scan` row keeps `bytes` (all bytes the walk saw, `scan.target_bytes`) and adds `takeable_bytes` and `reason` when the two differ (`links outside the target`, or another short reason the code already knows for an idle target). `sweep.py` computes them; `record.floor_scan` writes them.
   - **What `takeable_bytes` means.** A scan-time estimate of what taking only this target's candidates (its orphans and groups, or Phase 37's whole take) would free, with no shortfall limit: the take lines already show the shortfall-limited take. Compute it with `freed_by` against link counts restricted to that target's candidate inodes (never a copy of all of `scan.links` per target: that is targets × inodes), so it does not depend on take order: a file with a link outside the target frees nothing from it (two targets that share one inode each show 0 for it).
   - **Scope.** The row lists idle targets only, as now (`hold_floor` writes `idle`; a busy target prints a `left alone` line, and a held target is not walked). Busy and held targets stay out of the row.
2. **Docs.** `docs/as-built/build-targets.md` names the new fields.
3. **Three defects from Phase 37's review (routed here).**
   - **The doc-budget sweep records its delete like `remove` does.** Phase 37 made `record_target_deletes` raise `OSError`. `remove` catches it, but the doc-budget path calls it in a `finally` with no handler, so a state-write failure stops the doc sweep and every later target. That path also records only after `rmtree`. Use one record-before-delete rule for both paths, in one place: the target root is in `target-deletes.json` before the doc folder goes. A record failure keeps that doc folder, prints one line, counts one failure, and the sweep goes on.
   - **One target discovery per pass.** `walk` calls `target_dirs([directory])` for each target it scans, which walks each target's folders outside its build trees a second time, to depth 8. The scan takes the nested roots from the discovery result its caller already holds, so each folder is walked once per pass.
   - **A whole take's last use ignores a nested target.** `target_last_use`'s no-stamp fallback reads the mtime of every top-level entry except the locks, so a nested target's folder (and its new output) counts as the outer target's use. Skip an entry that holds a nested target root; the nested target has its own last use.

4. **natedev hears when the floor cannot run.** On 2026-10-31 a slow dry run held `floor.lock` from 02:55 to 06:46 EDT. Every real pass printed one "another sweep is holding it" line and stopped, and `/` fell from 297 GiB to 0.9 GiB free with no alert. Intended: when a real pass finds `floor.lock` held, free space is under the floor, and the lock has been held more than 15 minutes, it tells natedev once per hold (one message, through the existing send path), naming the holder's pid and command and the free space. A later pass under the same hold sends nothing more.
   - **Hold identity.** A real pass that takes the lock writes its pid and its start time into the lock file. The hold's age comes from that start time, never from the lock file's mtime, and a hold is the pair (pid, start time), so two holds by one pid are two holds. The waiting pass reads the holder as named states: a known holder (pid, start time, command), a holder `lock_holder` sees with no readable record, or no holder (the lock is free again or could not be opened); only a known holder past 15 minutes sends. The pass records the hold it told about in the floor record only after `send_floor_alert` returns true, so a failed send is tried again on the next pass. (builds showrunner, 2026-10-31.)

5. **A whole take's process recheck is cheap.** Phase 37's real-scale dry run (natedev, floor 400, 2026-10-31) made 221 takes, most of them small whole takes, and each take's recheck read every process's `maps` and parsed every line (`process_uses`, 25.6M lines, about 0.21 s a pass with 1830 pids). The dry run took 292 s; Phase 36's code took 44 s for 4 takes. The recheck before one take asks only whether a process uses that one target: compare `exe` and `cwd` by prefix, and search the raw `maps` bytes for the target's path prefix before any line is decoded; parse lines only in a process whose bytes match. The recheck keeps the cargo-nextest use (`nextest_targets`: a nextest run holds no path in the target between two test binaries) and deepest-owner attribution through `TargetOwnerIndex` (a process inside a nested target keeps only the nested one busy). Keep the first full `process_uses` pass as it is, and keep one recheck per take. Report the real-scale dry run's time before and after (floor 400, the same config file).

Tests: `scripts/buildlog/test_record.py` (the row shape) and `scripts/lint/test_sweep.py` (the values for a whole-take target, a target with units, a file with a link outside the target, one inode shared by two targets, and the same values for a small and a large shortfall; a failed doc-budget record keeps the doc folder and the sweep continues; a successful doc removal records before `rmtree`; a scan of a target with a nested target does not call `target_dirs` again; a lock held past 15 minutes under the floor sends one message, and a second pass under the same hold sends none; a new hold by the same pid sends again; an old lock file with a new hold does not send early; a failed send is tried on the next pass; the one-target recheck still finds a nextest run whose workspace owns the target and keeps an outer target idle for a process inside its nested target; a no-stamp outer target's last use ignores its nested target's folder; a one-target recheck decodes no `maps` line of a process whose bytes do not name the target, and still finds a process that maps a file under the target, runs from it or works in it).

**Acceptance:** the tests above; `test_record.py`, `test_sweep.py` and Phase 36's take-order regression pass from the worktree; `basedpyright` on the changed Python files.

**Files:**
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`, `scripts/buildlog/record.py`, `scripts/buildlog/test_record.py`, `docs/as-built/build-targets.md` — items 1-5.

**Workers:** `1 writer + 1 tester`
- `impl` — `sweep.py`, `record.py` and the doc: the values and the row contract.
- `test` — `test_sweep.py` and `test_record.py`: the values and the row shape.

### Phase 39 — A build's memory promise fits the build's size, so a big build does not wait for a promise it never uses  · status: todo
**Depends on:** Phase 26 (`scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`). Runs before Phase 20 (same file; director's order 2026-10-31, a one-line revert), so Phase 20's replay calibrates against the adopted dev rule.

**Approved:** builds showrunner GO 2026-10-31, after the head-memory-wait answer; the director's measurement is quoted below.

#### Work Order

**Goal:** A dev build with few earlier runs gets a promise near its real peak. On 2026-10-31 the S2 hana build (`verify.sh build hana`) waited 63 min at the head of the queue with 26 to 44 steps behind it. It was promised 21.0 GiB and peaked at 7.8 GiB.

**Cause (measured by the director, 2026-10-31):** the `shape_largest` basis multiplies the largest earlier same-shape peak by `DEV_HEADROOM` (4.2). That one factor was fit so that the smallest dev build in the 126-build replay was covered: a cargo-berth build that used 1.41 GiB after a 0.34 GiB same-shape peak. Applied to a 6.3 GiB hana peak, it gives 26.4 GiB, which the 21 GiB budget then caps. A need equal to the whole budget fits only when no build runs. A cap on the result would only hide this; the cause is a multiplier fit to small builds and applied to large ones. In the 3 days to 2026-10-31 02:05, the 50 hana `build` steps on natedev peaked at p50 10.8, p90 13.4 and at most 15.3 GiB. Phase 31 does not cover this case: it changes only what younger steps may do while the head has priority.

**Spec:**

1. **The headroom fits the build's size.** Replace the single dev multiplier with a headroom rule that covers small builds without scaling their ratio onto large ones (for example a fixed margin plus a smaller factor, or a factor that falls with the peak). Fit the rule by replay on the same 126-build cohort that fit `DEV_HEADROOM`, plus every build in the last 3 days. Keep `RELEASE_HEADROOM` unless the replay shows the same defect for release builds; say so in the summary either way.
2. **One source.** The rule and its fitted numbers live in one place in `memory_admit.py`, with a comment that names the replay (cohort, date, the build that limits each number). The need basis row records the rule so `need_basis` still reads back (`read_need_basis` (written by `need_basis_record`); a basis saved before this phase still reads back). No second copy of the shape logic anywhere, tests included: tests import the constants.
3. **The kill retry stays.** The existing retry after a memory kill stays the safety for a build that goes over its promise. Do not change it.
4. **Replay report.** The phase ships a replay over the last 3 days of natedev step rows: for each build, the old promise, the new promise and the measured peak. The report gives how many builds would have gone over their new promise, and by how much (each one, and the largest).
5. **Docs.** `docs/as-built/build-memory-admission.md` states the new rule and its source.

Tests: `scripts/lint/test_memory_admit.py`: a large dev build with 1 to 9 earlier runs gets a need within the fitted margin of its earlier peak (the S2 case: 6.3 GiB earlier peak gives a need well under 21 GiB); a small build (0.34 GiB earlier peak) is still covered at 1.41 GiB; the basis row round-trips; a release build keeps its current need unless Spec 1 changes it.

**Acceptance:** the tests; the replay report (Spec 4) in the checkpoint notice; `test_memory_admit.py`, the build queue tests and `basedpyright` on the changed Python files pass from the worktree. After the phase lands on main, the director sends builds the count of memory kills on natedev for one day, against the rate before the phase.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`, `docs/as-built/build-memory-admission.md` — items 1-5.
- `scripts/build_queue/build_why.py`, `scripts/build_queue/test_build_why.py` — `need_text` renders `DevHeadroom`'s multiplier today; the new rule is a named basis variant with its own text, and an old saved basis keeps its text.

**Workers:** `1 writer + 1 tester`
- `impl` — `memory_admit.py`, `build_why.py`, the replay and the doc.
- `test` — `test_memory_admit.py` and `test_build_why.py`.

### Phase 40 — A scratch folder's task always has an end, and only a running worker keeps a folder  · status: todo
**Depends on:** Phase 37 (order set by the builds showrunner: after the dry-run lock and owner-index fixes)

**Approved:** builds showrunner 2026-10-31: scratch.py and tmp_sweep.py are this unit's files; both fixes take a test each.

#### Work Order

**Goal:** A scratch folder made for a task goes away when that task ends. On 2026-10-31 lfo's `phase-lfo-unit-ci-icon-e0z61v` (58 GB) held a task `phase:lfo-unit/ci-icon` that nothing can end: `merge_checkpoint.py:1043` ends `phase:<unit>/<phase id>`, and `ci-icon` is no phase id. tmp_sweep also kept it as "a live worker uses it" from 31 finished `mesh_runs` records. Report: the director's lfo scratch diagnosis, 2026-10-31.

**Spec:**

1. **A `phase:` task names a real phase.** `scripts/lib/scratch.py` refuses a `phase:<unit>/<id>` task when a folder is made for it (`target`, :489) or handed to it (`hand_over`, :356), when `<id>` is not a phase id in that unit's plan. `checked_task` (:475) stays the shape check only: it also runs from `end`, `hold` and `update_record`, and an existing task with a bad id must still end and clean up. Read the unit's plan from its registry record (`proposals_table.unit_plan_paths`, the record's `plan`), never from the caller's checkout, and list its phases with `plan_history.plan_phases`. With no readable plan the make refuses and names the cause. The refusal names the right forms: a phase id, `gate:<id>` or `--until <when>`. Find the flow that made `phase:lfo-unit/ci-icon` (a CI-fix flow); if it needs a scratch folder outside a phase, it uses `gate:` or `--until`, changed in that flow.
2. **Only a running record keeps a folder.** `scripts/lint/tmp_sweep.py` `worker_use` (:753-769) counts the cwd of a `mesh_runs` record only while that record's status is `running`. A finished record no longer keeps a folder; the 7-day fallback and the other keeps stay.
3. **Docs.** `docs/as-built/build-memory-admission.md`, where it describes scratch tasks and tmp_sweep's keeps, states both rules.

Tests: `scripts/lib/test_scratch.py`: a `phase:` task with an unknown phase id is refused with the named forms; a real phase id is accepted; an existing folder whose task has an unknown id still ends (`end`) and its record still updates; a make for a unit with no readable plan refuses and names the cause. `scripts/lint/test_tmp_sweep.py`: a folder whose only `mesh_runs` records are finished is not kept for a worker; one with a running record is.

**Acceptance:** the tests; `test_scratch.py`, `test_tmp_sweep.py`, `test_merge_checkpoint.py` and `basedpyright` on the changed Python files pass from the worktree.

**Files:**
- `scripts/lib/scratch.py`, `scripts/lib/test_scratch.py`, `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`, `docs/as-built/build-memory-admission.md` — items 1-3. The director adds the CI-fix flow's file here before dispatch once item 1 finds it.

**Workers:** `1 writer + 1 tester`
- `impl` — `scratch.py`, `tmp_sweep.py`, the flow and the doc.
- `test` — `test_scratch.py` and `test_tmp_sweep.py`.
