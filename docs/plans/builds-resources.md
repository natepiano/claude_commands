# builds-resources

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Tests fake every memory reading the memory gate makes, and a tripwire fails a test that reads live machine memory.


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
- **Test:** each changed module with `python3 -m unittest discover -s <dir> -p '<module>'` from the worktree root.
  Then run the targeted test: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/targeted_test.py --checkout <worktree>`.
  Run it from `~/.claude` main after a merge of `origin/builds`.
  Check `build_hold.py status` first.
  Run no tests during a natedev build hold that this unit does not hold.
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
**Depends on:** Phases 2, 10, 12 and 19. Merge `origin/builds` first. Global order: 31 → 33 → 26 → 15/27 → 39 → 20 → 56 → 11 → 60 → 17 → 18 → 19 → 8.

**Held (2026-11-05):** the send.py freeze covers `build_hold.py`, so this phase starts after messaging's main hash.

**User decision (2026-11-05, via the builds showrunner):** "A. Build Phase 8 with the refusals." The question was whether to add the installed-copy write guards; the user's 2026-11-05 rule sends a new check to the user first.

**Approved:** showrunner 2026-10-29. The edit to `docs/plans/builds-production.md` is approved.

#### Work Order

**Goal:** only the installed copy under `~/.claude/scripts` writes shared build state. Tests use private state.

**Measured:**
1. `AdmissionServer` owns admission transitions. `persist()` saves the service state and drains the lifecycle outbox.
2. `StateIntents` saves finalization intent. `export_priority()` mutates state without constructing `AdmissionServer`.
3. `admission_client.step_request()` calls `step_estimator.step_estimate()` before it connects to the service.
4. Hold recovery can change release records during an imported call. A CLI guard alone does not cover that call.

**Spec:**
1. Extend `scripts/build_hold/override_guard.py` with one shared installed-copy guard.
   Reuse `user_home()` and `is_installed_file()`. Add no second home lookup or installed-path check.
   Check the actual directory before a write. Return a named refusal that identifies the installed entry point.
   Use a named allowed state and a named refusal state. Keep private test directories usable.
   Raise a typed refusal for imported mutation attempts. The CLI reports the reason and exits with status 2.
   Keep the installed copy's existing environment-override refusal.
2. Guard `AdmissionServer` before construction can restore or change shared state.
   Guard `StateIntents` and the lifecycle outbox through the same validated service boundary.
   Cover state replacement, reservation finalization, cancellation, priority changes, token returns and restart recovery.
   Guard `export_priority()` separately because it does not construct `AdmissionServer`.
   Validate each actual mutation path against the guarded directory. A private root does not authorize a real-state reservation path.
   Test direct imported calls to each mutation entry point. Refuse before any file changes.
3. Keep initial estimation in `admission_client` through `step_estimate()`. Phase 68 removes the estimate cache (`admission/build_marks.json`) and its writer; add no guard for it.
4. Guard hold and release mutations through `build_hold.release_lock()`.
   Validate both the holder directory and the release directory.
   Introduce a named `ReleaseLockHeld` capability for helpers that change release state.
   Bind the capability to the validated directories. Require it for receipt migration and damaged-intent recovery.
   Cover `release_records_for_change()`, `complete_release_record()` and `set_aside_damaged_record()` through that capability.
   Do not take the same flock again inside a helper. Keep hold status read-only.
   Refuse mixed private and real directories before either real directory changes.
   `read_cycle()` (`build_hold.py:978`) returns `HoldCycle | None`, and `cycle_path()` (`build_hold.py:967`) returns `Path | None`, for the same no-current-cycle state.
   Return the existing `NoCycle` (`build_hold.py:84`) from both in place of `None`. Add no second type for this state.
   Update every caller: `read_cycle_for_change()`, and `scripts/production/dailies_render.py` (`read_cycle()` near line 1570, and `hold_line()`'s `cycle: HoldCycle | None = None` at line 1408).
   Update the test callers too. Six test files narrow the result with `assert cycle is not None`, a check that `NoCycle` passes.
   Replace each one with `assert isinstance(cycle, build_hold.HoldCycle)`. This change keeps the assertion's intent.
5. Route callers through the installed copy.
   `memory_gate.sh` and `invoke.sh` use the installed admission client.
   `verify.sh` uses the installed helpers for holds, local release waits, detached runs, Mac idle state and build targets.
   Keep documented script overrides for tests. Test routing under a private `HOME`.
   `top_level.py` and unit retirement in `scripts/shutdown/shutdown.py` (its `build_hold.py release --holder` call near line 895) use the installed hold writer.
   `merge_gate.py` already runs from the installed copy. Record that fact without changing its hold reader.
   Update the two caller-plan examples to name the installed merge gate with `--checkout <tree>`.
6. Keep repository and step fields separate in service snapshots and queue queries.
   Reuse `repository_name` on waiters and `repo` on reservations. Add no parallel repository field or prefix parser.
   Show repository and step separately in the queue overview and cards. Show a missing repository as `—`.
7. Test refusal and allowed private writes at every mutation entry point, including `export_priority()` and restart recovery.
   Test interrupted cancellation publication and imported-place migration through the service.
   Test queue queries and service state after admission. Do not require a live `.waiting` file.
   Fake both memory readings. Never change real coordination state in a test.

**Constraints from prior phases:**
- builds-queue Phase 10z replaced the Phase 20 active and inactive windows. One pass (`memory_admit.admission_walk`) uses the captured room.
  It protects room for the head, lets a `ContinuationSlot` reuse its run's slot, and lets an oversized head start alone.
  Displays, the client and the wait text use that pass's frozen result. Add no separate admission rule in a reader.
- Phase 20: `PendingLeft` saves durable cancellation facts. Restart publishes one cancellation with its original time.
  Keep the one lifecycle outbox and the saved finalization intent. Add no second lifecycle writer.
- Phase 20: an old `NoWaiter` place identity is migration input, not proof that a step ended.
  `WaiterPlace.queue_identity` carries the logical identity. Preserve it through restart and owner exit.
  builds-queue Phase 10x (89ba4f087) removed the legacy import and its test `test_imported_and_restored_places_record_one_owner_exit_after_restarts`.
  Add no user control or display for either migration or pending cancellation.
- Phase 41: token waiters keep their arrival and priority times. They hold neither the head position nor head room.
  Preserve the saved offset and transition identity for priority publication retries.
- Phases 22 and 23: hold recovery can migrate receipts and recover damaged release records.
  Guard those writes without changing the read-only status path.
- Phase 68 removes Phase 18's estimate-cache writer and `admission/build_marks.json`. Nothing remains to guard there.

**Files:**
- `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_client.py` — service boundaries and installed routing.
- `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py` — refusal, recovery and routing tests.
- `scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, `scripts/lint/invoke.sh` — caller routing.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_invoke_scope.py` — caller tests.
- `scripts/build_hold/override_guard.py`, `scripts/build_hold/build_hold.py` — shared guard and hold mutation boundary.
- `scripts/build_hold/test_override_guard.py`, `scripts/build_hold/test_build_hold.py` — guard and hold recovery tests.
- `scripts/build_hold/test_hold_enforcer.py`, `scripts/build_targets/test_build_targets.py`, `scripts/direct/test_verify_mac_lifecycle.py`, `scripts/direct/verify_mac_offload_test_support.py`, `scripts/direct/verify_hold_wait_test_support.py` — `read_cycle()` callers that narrow to `HoldCycle`.
- `scripts/build_queue/queue_view.py`, `scripts/build_queue/queue_card.py` — separate repository and step displays.
- `scripts/build_queue/test_queue_view.py`, `scripts/build_queue/test_queue_card.py`, `scripts/build_queue/golden/` — display tests and goldens.
- `scripts/direct/verify.sh`, `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_nix_shell.py` — installed helper routing.
- `scripts/message/top_level.py`, `scripts/shutdown/shutdown.py` — installed hold writer routing.
- `scripts/message/test_top_level.py`, `scripts/shutdown/test_shutdown_cli.py` — caller tests.
- `scripts/production/dailies_render.py` — `NoCycle` from `read_cycle()` in the hold lines.
- `scripts/production/test_dailies_render_holds.py`, `scripts/production/test_dailies_render.py` — hold line tests.
- `docs/plans/builds-production.md`, `docs/plans/build-followups-worker-seats.md` — installed merge-gate examples.
- `docs/as-built/build-memory-admission.md` — installed-copy rule and separate row fields.

**Workers:** `1 writer + 1 tester`
- `impl` owns the implementation files and documentation listed above.
- `test` owns the listed test files, routing fixtures and display goldens.

**Acceptance gate:**
- Preserve these existing tests unchanged:
  1. `test_live_priority_marks_and_cards_use_the_snapshot_without_bump_history` in `scripts/build_queue/test_queue_view.py`.
  2. `test_token_wait_keeps_place_one_and_head_status_moves_to_the_eligible_row` in `scripts/build_queue/test_queue_view.py`.
  3. `test_priority_override_and_frozen_release_keep_the_resolved_head` in `scripts/build_queue/test_admission_service.py`.
  4. `test_each_pass_matches_the_shared_rule_and_view_uses_its_frozen_limits` in `scripts/build_queue/test_admission_service.py`.
  5. `test_view_release_and_admit_pass_capture_the_same_continuation_slot` in `scripts/build_queue/test_admission_service.py`.
  6. `test_a_younger_run_cannot_take_the_room_protected_for_the_head` in `scripts/build_queue/test_admission_service.py`.
  7. `test_an_oversized_head_starts_alone_but_unreadable_host_or_pressure_holds_it` in `scripts/build_queue/test_admission_service.py`.
- Preserve `test_restart_retries_an_interrupted_cancellation_with_its_original_time` unchanged in `scripts/build_queue/test_admission_service.py`.
- Preserve `test_full_queue_holds_gate_until_the_running_charge_is_released` and `test_admission_error_refuses_before_any_module_starts` unchanged.
  Both tests are in `scripts/production/test_merge_gate_suites_admission.py`.
- Preserve these hold tests unchanged in `scripts/build_hold/test_build_hold.py`:
  1. `test_status_reports_recovery_without_changing_damaged_record`.
  2. `test_status_names_unsent_release_after_last_holder_is_gone`.
  3. `test_release_sets_aside_malformed_current_intent_with_no_holder`.
  4. `test_release_recovers_recipient_notices_from_a_damaged_referenced_cycle`.
  5. `test_status_names_unreadable_pending_records_and_cycles_without_hiding_them`.
- Every changed module and caller passes its tests. Basedpyright reports `0 errors, 0 warnings, 0 notes` for those modules.
- No `read_cycle()` caller checks its result with `is not None` or `is None`, in source or in tests.
- After installation, query the service and inspect its saved state. Repository and step fields remain separate.
  A checkout mutation attempt refuses before state changes. A private-state attempt still works.
- Name queue-unit files as `also touches`, tested against `origin/builds-queue`.
  Name verify-unit and hold-caller files with their owners in the notice.
- Change-Review: `/build:queue` shows repository and step separately. A row shows `hana` and `clippy` in separate cells.

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

### Phase 11 — Memory admission promises what each repository's steps really use  · status: done

#### As-built

- `build_shape.py` holds the one anon-to-index join, `index_step_evidence(index_path, host, step, cutoff, before)` (formerly `index_shapes`). It has no repository argument; repository filtering stays at the history-row boundary. Each `(call_id, step)` key maps to a frozen `StepEvidence(shape, compilation)`, and shape and compile count have independent known and unknown states.
- The compile state is `CompiledCrates(count) | NoCratesCompiled | CompileCountUnknown`. `combine_compile_counts()` merges repeated attempts in any order: a positive count means compiled (counts summed), zero only when every attempt reports zero, otherwise unknown. A matching row with a missing or bad `started_at` is an unknown attempt. A key with no matching row is absent and counts as neither.
- `expected_peak()`: with 5 or more keyed rows (repository URL), only keyed rows count. A repository with 1 to 4 own runs takes its largest own run, or the pool p90 when that is lower; a repository with none keeps the pool. `build` steps keep the same-shape rule.
- Compile runs and no-compile runs never mix, for every Cargo step. The admitted step is a compile run unless `is_fresh_test` proves it fresh. A compile run's promise comes only from earlier compile runs, in the order exact filter set, package filtered runs, check/build peaks, and it is not capped by the full-run estimate. Fresh runs keep `fresh_test_peak` and its 1 GiB floor. `merge-gate` rows stay their own class.
- An index without a `crates_compiled` column keeps every legacy peak in `expected_peak()` and `filtered_peak()`.
- `DevHeadroom` is `LegacyDevMultiplierHeadroom`; its wire tag stays `"dev"`.
- Replay over 15,053 runs (2026-10-21 to 2026-11-04): under-half 491 → 430, over-three 2,605 → 2,620. The bar was missed; the showrunner accepted the miss on 2026-11-04. The largest rise is cargo-liner nextest compile runs at 21 GiB: under-half 27 → 7, over-three 101 → 166. The replay script and its full grouped table stay in the run folder (`phases/11/replay.py`, `replay_groups.md`, `replay_inputs/`).

**Files:**
- `scripts/lint/build_shape.py` — `index_step_evidence`, `StepEvidence`, the compile states and `combine_compile_counts`.
- `scripts/lint/memory_admit.py` — keyed rows, the own-run rule, the compile/no-compile split, `compiling_history()` and the rename.
- `scripts/build_queue/queue_evidence.py` — the rename.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py`, `scripts/build_queue/test_queue_evidence.py` — tests for each rule.
- `docs/as-built/build-memory-admission.md` — the rules and the replay summary.

**Binds later work:** History readers use `index_step_evidence`; add no second `steps` join and parse no argv outside `build_shape`. An unknown compile count keeps the peak and never reads as zero. `CompiledCrates` beside an unknown attempt is a lower bound, not an exact size. `compiling_history()` keeps a peak with no index row and selects by completion time. These tests stay unchanged: `test_repeated_attempts_combine_compile_states_in_either_order`, `test_undated_attempt_prevents_a_zero_compile_result`, `test_unknown_attempt_between_zero_attempts_keeps_the_count_unknown`, `test_unknown_attempt_between_zero_attempts_keeps_its_compile_peak` and `test_failed_peak_refresh_precedes_indexing_and_zero_compile_evidence_removes_it`.

**Gotchas:** A release publishes its failed peak before `invoke.sh` writes the index row, so a peak with no index row is a normal transient state, not a run with no compile.

**Ruled out:** capping a compile promise at the full-run estimate — a filtered run that compiles can use more than the full suite's no-compile runs.

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
5. **Queue evidence words.** `queue_evidence.charge_text` (:31) ends in `assert_never`, so the new variant needs its text: `tests since <HH:MM>: peak so far <GiB>, need <GiB> (<form>, p90 of <runs> runs at width <width>), × 1.2`.
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
   - In `scripts/build_queue/test_queue_evidence.py`: `charge_text` for `TestPhaseCharge`.

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
- `scripts/build_queue/queue_evidence.py` — Spec 5 (owner queue-unit; trial-merge against `origin/builds-queue`).
- `scripts/build_queue/test_queue_evidence.py` — Spec 6, the charge text test (owner queue-unit).
- `docs/as-built/build-memory-admission.md` — Spec 7.

The replay and 24 h check scripts live in the run folder (`phases/15/`), not in the repository; both call Spec 2's rule.

**Workers:** `1 writer + 1 tester`
- `impl` — `memory_admit.py` (including the finalization-intent extension, Spec 4, and the one codec, Spec 2), `queue_evidence.py`, docs, the replay and the follow-up measurement (the 24 h check).
- `test` — `test_memory_admit.py` with its shared fixture (`MemoryQueueFixture`) and the one module-level history row writer (Spec 6), the retry regressions (failed and interrupted appends, owner-gone cleanup), and `test_queue_evidence.py`.

**Acceptance gate:**
- The memory admission and queue evidence tests pass; `basedpyright` over `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`, `scripts/build_queue/queue_evidence.py` and `scripts/build_queue/test_queue_evidence.py` reports `0 errors, 0 warnings, 0 notes`; the merge gate is green.
- The finalization retry tests (Spec 6) pass: a failed or interrupted append, the removal of the test-phase file, then a retry or owner-gone cleanup, leaves exactly one history row with the saved test-phase evidence (Phase 33's one-row guarantee, extended by Spec 4).
- A test phase is above its charge when its `test_phase_peak_bytes` is above N × 1.2, its charge at the mark. The replay on 4c data uses natedev build-log rows with `test_phase_peak_bytes`, each with N from earlier rows by Spec 2's rule. For every repository, at most 2.3 % of test phases with an N are above their charge. The table (per repository: test phases, test phases with an N, test phases above the charge, worst excess in GiB) goes in the checkpoint notice. If a repository is above 2.3 %, the numbers go to the builds showrunner before the checkpoint.
- The notice has an `also touches scripts/build_queue/queue_evidence.py, test_queue_evidence.py (owner queue-unit)` line, tested against `origin/builds-queue`.
- The 24 h check, 24 h after the merge reaches main: from `anon_peaks.jsonl` rows with `test_phase_peak_bytes` and `test_phase_need`, per repository, the nextest steps that got a `TestPhaseCharge` and the test phases above their charge; the `steps.mem_wait_s` total against the 24 h before the merge; earlyoom kills (journal) and `calls.mem_kills` in both windows. The phase report records when the check is due; the result goes in a follow-up notice.
- Change-Review line: a nextest step's charge falls to its test-phase need once its tests start | on natedev in `~/rust/hana`, run `~/.claude/scripts/direct/verify.sh test hana_diegetic` two times; during the second run, after its compile, run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py` | the step's charge reads `tests since …` with its need and width form, below its compile peak.

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

### Phase 17 — The nextest promise splits by compile size  · status: done

#### As-built

- No code changed. Nextest promises do not split by compile count; the estimate is the Phase 60 rule.
- `docs/as-built/build-memory-admission.md` → "Phase 17 compile-band measurement (not adopted)" records the replay against checkpoint `e88fb841d`: bands 1–5, 6–20, 21–50 and more than 50 crates; under-half 395 → 423 with ten repositories worse; over-three 2,525 → 2,485. The adopt rule needs fewer under-half promises in total and no rise in any repository.
- Predictor accuracy for the band of a compile request: (a) last compile count of the same worktree and step 54%, (b) changed `tree_key` 27%, (c) recorded Cargo output folders 0.4%.

**Files:**
- `docs/as-built/build-memory-admission.md` — the compile-band measurement paragraph.

**Binds later work:** `StepEvidence` (`scripts/lint/build_shape.py`) still carries only shape, compilation and arguments. A reader fact (worktree, start time, `tree_key`, `sha`, recorded Cargo roots, count certainty) lands only with the code that first reads it (builds showrunner 2026-11-04). The withdrawn reader patch with 26 tests is `phases/17/reader_facts_withdrawn.patch` in the resources run folder; it carries no `sha`.

**Gotchas:**
- Only 46 of 18,564 frozen nextest rows record Cargo roots (roots exist since Phase 51), so folder history cannot predict a band yet.
- Compile count is not a monotonic memory predictor across repositories: the Hana p90 falls above 50 crates.

**Ruled out:**
- Compile-count bands for nextest promises: they raise under-half promises in ten repositories.
- Landing reader facts with no caller: each fact lands with its first reader.

### Phase 18 — History splits on the dylib feature and each step's commit, not on a time  · status: done
#### As-built
Phase 68 removed this split; this record is history.
A memory promise keeps only history rows from the same side of each listed build change as the admitted step.
- `config/build_changes.json` lists the hana changes: the `hana/dynamic_linking` feature (`argv_feature`) and the opt-level 1 commit `ad25f045ff` (`commit`). `read_build_changes()` decodes it; `UnusableBuildChanges` names a bad file.
- `HistorySplit` (or `NO_HISTORY_SPLIT`) goes through `expected_peak`, `filtered_peak`, `build_expected_peak`, `build_peaks`, `fresh_test_peak` and `compiling_history`. `prepare_history_split()` builds it per estimate.
- Feature sides come from `build_shape(argv).features`. Commit sides come from git ancestry of each row's sha (`ancestry_outcomes()`, one git call per sha), cached in `admission/build_marks.json` by `memory_admit.step_estimate()`.
- `build_shape.step_commit()` gives `KnownStepCommit` or `UNKNOWN_STEP_COMMIT`; `AncestryOutcome` is after, before, ambiguous or unavailable. Only before and after place a row; other rows count on neither side. A failed peak with no index row keeps the admitted step's side.
- `admission_client.build_history_generation` has two named configuration states (current, supplied).

**Files:**
- `config/build_changes.json`, `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py`, `scripts/build_queue/admission_client.py`, `scripts/buildlog/width_trial.py` — the split and its callers.
- `scripts/lint/build_change_test_support.py`, `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py`, `scripts/build_queue/test_admission_client.py` — tests.
- `docs/as-built/build-memory-admission.md` — the list, the split rule and the replay table.

**Binds later work:** the replay shows no gain. With the split, nextest steps above their promise went from 106 to 108 and below half their peak from 6 to 9; the feature split alone gave the same result. "Memory estimates use all same-shape history again" deletes both splits, the change list, the build-marks cache and the commit states.
**Gotchas:** after the dylib change the feature side has only 676 rows. Queue 10z removed the build budget cap; promises are uncapped.

### Phase 19 — Measure where admission estimates come from, then make the 0.65 fallback rare  · status: done
#### As-built
The phase stopped at Spec 2. The builds showrunner ruled the `ANON_SHARE` share small on 2026-11-05: 10/4,267 (0.23%) of the reconstructed estimates, caps excluded, and 0/220 recorded admissions. Small means under 5% of estimates. Specs 3-5 were not built, and `ANON_SHARE` stays.
- `memory_admit.count_queue_sources(lifetimes: Sequence[QueueStepLifetime]) -> QueueSourceCounts` counts each reservation interval's `need_source` as one admission and each logical step's memory wait once. `QueueSourceCounts` keeps `MemoryWaitIncomplete` (`incomplete_memory_waits`), `NoPositiveSample` (`unsampled_intervals`), open intervals and completed steps apart. A token return ends an interval, not the step.
- `docs/as-built/build-memory-admission.md` `#### Phase 19 source shares` has the 7-day table by step kind, before and after Phase 60. The recorded admissions and the replayed estimates are separate populations, and `shape` and `cap` are their own columns. Recorded admissions covered only 16.30 hours, and the recovered legacy rows were still in the window.

**Files:**
- `scripts/lint/memory_admit.py` — `QueueSourceCounts` and `count_queue_sources`.
- `scripts/lint/test_memory_admit.py` — `test_source_counts_separate_admissions_logical_waits_and_missing_evidence`: two intervals, two sources, one logical step, a token return, an incomplete wait and an unsampled peak.
- `docs/as-built/build-memory-admission.md` — the source-share table.

**Next:** the remeasure on or after 2026-11-12, when the legacy rows leave the window, is in `docs/plans/builds-resources-next.md`. Specs 3-4 come back as a phase only if the share then reaches 5%.

### Phase 20 — Replay a looser head rule, and adopt it only on the replay  · status: done

#### As-built

- Every queue step's life is in `admission/queue_times.jsonl`: `admitted`, `left` and `ended` events from the admission service (`scripts/build_queue/admission_service.py`), written through a lifecycle outbox. `PendingLeft` is the durable cancellation intent; a restart replays it with its original time.
- `finalize_reservation(..., end_kind)` takes `StepEnd | TokenReturn`. A `TokenReturn` `ended` event ends one reservation interval, never the step. One `ended` event is written per reservation id, also on a retry.
- `read_queue_lifetimes` (`scripts/lint/memory_admit.py`) rebuilds each logical step from its reservation intervals: one arrival at the original `since`, the token gaps, and one end.
- `WaiterPlace.queue_identity` carries each waiting place's identity. One constructor builds live places and both import paths; a migration fills a missing identity from the saved waiting row.
- One head policy: `head_policy(...) -> HeadPolicy`, with `HeadPolicy.queued_head: QueuedHead | NoQueuedHead`, in `PassResult(rows, policy)`. Admission, the blocker lookup, `read_queue`, `explain_queue`, the release search, the queue card, the overview (`queue_view.py`) and the shell wait text read that one resolved state. `HeadAdmissionStatus` carries the override result.
- Calibration found insufficient evidence (less than 7 days after Phase 39 reached main), so the strict head rule stays deployed. `CapacityBackfill` does not exist. The as-built doc `docs/as-built/build-memory-admission.md` records the result.

**Files:**
- `scripts/lint/memory_admit.py` — head policy, lifetime reader, `finalize_reservation`
- `scripts/build_queue/admission_service.py` — lifecycle outbox, `PendingLeft`, `WaiterPlace` identity and migration
- `scripts/build_queue/queue_view.py`, `scripts/build_queue/golden/card_1.txt`, `card_4.txt`, `unit_override_view.txt` — displays read the resolved head
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_admission_service.py`, `test_admission_client_wait.py`, `test_build_queue.py`, `test_queue_view.py` — tests
- `docs/as-built/build-memory-admission.md` — lifecycle record and calibration result

**Binds later work:** later phases use `PassResult(rows, policy)` and `HeadPolicy.queued_head`, and displays consume the resolved head state. Keep `test_restart_retries_an_interrupted_cancellation_with_its_original_time`, `test_imported_and_restored_places_record_one_owner_exit_after_restarts` and `test_first_priority_mark_agrees_in_the_card_overview_and_shell`.

**Gotchas:** `NoQueuedHead` and a first priority mark are different states; a first-mark row shows its allowance and deadline. The 8 MiB cap on `queue_times.jsonl` can keep less than 14 days. A reservation is an interval, not a step: count memory waits per logical step.

**Ruled out:** `head_wait_reason`, `recorded_window` and a separate `admission_event` writer (one resolved head state instead); `CapacityHeadRule` and `DeployedHeadRule` rule classes; adopting a looser head rule without a calibrated replay; a separate user control for `PendingLeft`.

### Phase 21 — Copies get a root, a busy mark and a lifetime that ends when their work ends  · status: done

#### As-built

- `/tmp/claude/copies/<pair>/<copy>` is a kept root in `tmp_sweep.py`, in `folder_audit.py` `KEPT_NAMES` and in the scratch guard hook. `copy_record.copies_root()` resolves it once with `realpath` (`SCRATCH_COPIES_ROOT` sets it in tests). Each copy is one entry; an empty `<pair>/` goes.
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

**Binds later work:** a new head result joins `HeadRelease` or `HeadAfterEnd`, and release-line text goes only in `queue_evidence.release_text` (The memory queue starts steps in queue order and records when a step became first; Replay a looser head rule, and adopt it only on the replay). `HeadStartsAt` is per snapshot and not saved; build-why and `/build:queue`'s head explanation show it from the same `QueueRead`.

**Gotchas:** the type check that counts is the merge gate's form, `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python`; a bare run reports bpy and PIL errors. The merge gate's type job can go above its 4.000 GiB sampled cap; it passes when it runs alone.

### Phase 26 — The memory queue starts steps in queue order, and the user's bump puts one unit first  · status: done

#### As-built

- One resolved queue snapshot exists. `resolve_queue_order` gives each waiter a derived `priority_rank`. Admission (`check`), `admit-head`, the queue view and card, `build_why`, the blocker lookup and the release search all read that one snapshot; none reads the roster or `queue_priority.json` on its own. The snapshot keeps the head-admission override as it was.
- Queue priority is the user's bump. `memory_admit.py priority set --unit <unit> --why <why>` and `priority clear` work only for a showrunner session. State is `admission/queue_priority.json`. It has no expiry: a second `set` replaces the live record, and `clear` restores plain `(since, pid)` order. Scope is the unit's roster `unit_labels` (its Codex workers and scratch-copy steps), read at each pass. Each set, replace and clear writes one build-log row with a transition id, deduped by `record.append_row_once`.
- `/build:queue bump <unit>` (unit-board pane and `build_queue.py bump`) runs `priority set --why "the user's bump"`. It prints `queue priority set for <unit>: first step now #N` or `…: no waiting step yet`. A session that is not a showrunner gets exit 3 and `build queue: type /build:queue bump <unit> in a showrunner pane`. A wrong word count gets `build queue: use /build:queue bump <unit>`.
- Token wait: when `try_token_now` fails, `memory_admit.py token-wait <reservation>` (shell `buildlog_token_wait` in `memory_gate.sh`, called from `verify.sh`) ends the reservation through the finalizer and, under the same lock, puts back the waiter row with its original `since` and `priority_since` plus `token_left_at`. The row is an ordinary waiter for every reader. When `acquire_token` returns, the next `check` of that pid and start time (with `BUILDLOG_MEM_TOKEN_BACK_AT`) appends one `token_gap` line to `queue_times.jsonl` through the one locked append, drops `token_left_at`, and admits under the usual rules. A step runs only when it holds both memory and the token.
- No `head_since` exists. Views show total wait from the first queue time. The row-1 mark is the head window mark only (`↑ ≤N`, `⊘`, `↑` backfill). An active priority shows on its own line, `queue priority: <unit> (the user's bump)`, in the view and the card. `HEAD_SECONDS`, `STRICT_SECONDS`, `BACKFILL_SECONDS` and the strict/backfill cycle are unchanged.

**Files:**
- `scripts/lint/memory_admit.py` — resolved snapshot, `priority set|clear`, `token-wait`, the exit-3 pane refusal.
- `scripts/lint/memory_gate.sh`, `scripts/direct/verify.sh` — the `buildlog_token_wait` route.
- `scripts/build_queue/queue_view.py`, `queue_card.py`, `build_why.py`, `head_facts.py`, `build_queue.py` — snapshot readers, the priority line (written once in `head_facts`), the bump route.
- `scripts/buildlog/record.py` — priority transition rows, `append_row_once`.
- `mods/unit-board/hooks/register.tsx` — the pane bump route.
- `commands/build/queue.md`, `commands/showrunner/bump.md`, `docs/as-built/build-memory-admission.md` — priority and token-wait rules.

**Binds later work:** Queue priority rows are set, replace and clear only; the single head policy work and its lifecycle events read them so. `head_facts.read_head_facts` gets no `head_since`; `_token_gap` reads `token_left_at` and `token_back_at` by those names. Old `queue_bump` rows stay readable as history; nothing writes new ones. Approved follow-up: a typed `/build:queue bump` from any pane, with no session check on the plugin route.

**Gotchas:**
- `priority_rank` is derived: strip it before any durable write (waiter rows, reservation copies).
- The row-1 mark is never a priority mark; a test that expects an up arrow for a priority is wrong.
- An agent session cannot set a priority; only a showrunner session or the user's typed bump in a showrunner pane can.
- A stop between the finalizer and the row write in `token-wait` is finished by the next `token-wait` or the dead-owner sweep; there is no second path.

**Ruled out:**
- Priority expiry (`--until`) — the user: a priority lasts until replaced or cleared.
- `head_since` as a separate measure — the user: total wait is what matters.
- A give-back that sends a token-waiting step behind others — it keeps its place under the normal windows.
- Holding the reservation through a token wait — it promises memory no build uses.
- A moved mark on row 1 — priority has its own line.
- `bump_order`, `bump_waiter`, `BumpOff`, `priority refuse` — removed.

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

### Phase 38 — The floor_scan row shows takeable bytes and the reason for the rest  · status: done

#### As-built

- `IdleFloorTarget` retains `folder`, `last_use` and total `bytes` (`scan.target_bytes`), with optional `takeable_bytes` and `reason` when they differ; `record.floor_scan` persists them. `floor_scan_targets` uses `freed_by` with target-local candidate inode links and no shortfall limit, independently of take order; cross-target shared inodes free nothing from either target alone, and reasons distinguish `links outside the target`, `links outside the take candidates` within the target, and `output outside take candidates`. Busy and held targets are omitted.
- `record_before_delete` saves `target-deletes.json` before normal or doc-budget deletion; a failed write keeps the folder, prints its cause, counts a failure and continues to later targets.
- `DiscoveredTargetRoots` and `Scan` share one discovery inventory, including nested roots, without rediscovery in `walk`; `target_last_use` excludes entries containing a nested target from its no-stamp fallback.
- `process_use` rechecks one target per take through `TargetOwnerIndex`, comparing `exe`/`cwd` prefixes and filtering raw `maps` bytes before decoding matching processes; `nextest_targets` and deepest-owner attribution protect nextest runs and nested targets. The initial full `process_uses` scan remains the inventory source.
- Real `floor.lock` holders record pid and `started_at`; `floor_lock_holder` returns `KnownFloorLockHolder`, `UnreadableFloorLockHolder` or `NoFloorLockHolder`. A real sweep unable to acquire the lock alerts natedev only for a known overdue hold below the floor with pid, command and free space, using `[sweep]` `floor_lock_alert_minutes` from `config/lint.conf` at use (configured as 15); invalid configuration prints its cause and sends nothing.
- `FloorRecord` represents receipts with `DeliveredFloorLockAlert(pid, started_at)` and `NoFloorLockAlert`; `locked_floor_record` holds the separate stable `floor-alert.lock` across receipt read/check/send/save and every measurement write, rereading inside the lock. Receipts persist only after `send_floor_alert` succeeds, so concurrent waiters deliver once per hold, failed sends retry, and another hold by the same pid can alert again.

**Files:** `scripts/lint/sweep.py` — estimates, discovery, deletion, ownership and alert state; `scripts/lint/test_sweep.py` — filesystem, process and concurrency contracts; `scripts/buildlog/record.py` and `scripts/buildlog/test_record.py` — optional row fields and persistence; `config/lint.conf` — alert delay; `docs/as-built/build-targets.md` — behavior contracts.

**Gotchas:** Target-local observed link counts distinguish internal links outside candidates from external links; the alert lock keeps a stable inode separate from the atomically replaced record, and both receipt and measurement writers reread under it to preserve newer state.

**Ruled out:** Treating all scanned bytes as independently takeable; copying all `scan.links` entries per target; using mtime or pid alone as hold identity; recording invalidation after deletion; saving delivery before a successful send.

### Phase 39 — A build's memory promise fits the build's size, so a big build does not wait for a promise it never uses  · status: done

#### As-built

- A dev `build` promise is `DevSizeHeadroom(factor, margin)`: ceil(base × `dev_size_factor` + `dev_size_margin_gib`), then the cap at the build budget. A release promise is `ReleaseHeadroom(release_headroom)`. Other profiles get `NoHeadroom`. Release is found through `BuildShape.profile`, so `--release` and `--profile release` match.
- `memory_config()` / `read_memory_config()` (`scripts/lint/memory_admit.py`) read `dev_size_factor` (1.1), `dev_size_margin_gib` (1.1) and `release_headroom` (1.314) from `config/build_admission.conf` `[memory]` at use, each with its range check. The config comments name the replay cohorts and the builds that limit each value.
- Replay of the fit: the 126-build cohort at a 21 GiB budget has 0 of 91 dev builds over the new promise and 1 of 35 release builds over (3.76 GiB). The last 3 days, 64 builds at 24 GiB, have 0 of 22 dev and 2 of 42 release over (largest 0.19 GiB). Each release excess is a profile-fallback row with no same-shape history. The S2 case (6.3 GiB earlier peak) gets an `UnderBudget` need; the 0.34 GiB cargo-berth build is still covered at 1.41 GiB.
- `CappedAtBudget(uncapped, cap: int | CapNotRecorded)`: the cap is required and saved with the basis. A row saved before the cap field decodes to `CapNotRecorded`.
- `HEADROOM_FACTOR_CLASSES` maps the wire tags `release`, `dev` and `dev_size` to `ReleaseHeadroom`, `DevHeadroom` and `DevSizeHeadroom`; the encoder and the decoder share it. The decoder reads each saved factor back as saved: not a bool, finite, at least 1, with `OverflowError` caught; a `dev_size` row also needs an int margin of at least 0. `DevHeadroom` and `DEV_HEADROOM` (4.2034275337646125) stay only to decode old saved rows.
- `need_text(source, *, zone)` (`scripts/build_queue/queue_evidence.py`) takes no budget. It prints the saved cap, or "the build budget of that time" for `CapNotRecorded`, and renders each headroom class. The memory-kill retry is unchanged.

**Files:**
- `config/build_admission.conf`, `scripts/lint/fixtures/build_admission.conf` — the three headroom keys.
- `scripts/lint/memory_admit.py` — the headroom classes, the shared tag map, the codec and `build_expected_peak`.
- `scripts/build_queue/queue_evidence.py`, `scripts/build_queue/queue_card.py` — `need_text` without a budget.
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_queue_evidence.py`, `scripts/build_queue/test_admission_service.py` — rule, codec, change-at-use and text tests.
- `docs/as-built/build-memory-admission.md` — the rule, its source and the replay summary.

**Binds later work:** the deployment of both checkpoints of this phase on main is a calibration boundary for the head-rule replay; headroom values come only from `memory_config()`, never constants.

**Gotchas:** a saved JSON int factor such as 10**400 overflows `float()`; the decoder must catch it. The replay script and its inputs live only in the run folder.

**Ruled out:** committing the replay script (1.6 MB of inputs and a fixed tree path); an exact match of a saved dev factor against `DEV_HEADROOM` (it ties the decode to a constant).

### Phase 40 — A scratch folder's task always has an end, and only a running worker keeps a folder  · status: done

#### As-built

`scratch.registered_plan(unit)` is the single resolver, using `unit_lookup.unit_parent` with `staff.snapshot`, `phase_table.show_production` and `proposals_table.unit_plan_paths` to locate the registered plan.
`target` and `hand_over` validate new phase tasks through `plan_history.plan_phases` before creation, adoption or record replacement; missing phases and unreadable plans raise cause-bearing refusals naming a real phase, `gate:<id>` or `--until <when>`.
`checked_task` stays shape-only for legacy updates, holds and end, so existing invalid phase tasks remain cleanable.
`tmp_sweep.worker_use` keeps a detached run record's cwd only while its status is `running`; other keeps and the 7-day fallback still apply.

**Files:**
- `scripts/lib/scratch.py`, `scripts/lib/test_scratch.py` — resolver, assignment guard and private-plan assignment/legacy-cleanup cases.
- `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py` — running-only cwd retention and lifecycle cases.
- `scripts/production/test_merge_checkpoint.py` — private `registered_plan` fixture for phase-end cleanup.
- `docs/as-built/build-memory-admission.md` — scratch task and retention rules.

**Binds later work:** “Test Hana clones go through scratch, and the Mac keeps no idle clone below its floor” uses the single resolver, real registered phase or gate/deadline tasks, shape-only legacy cleanup, private-plan tests and running-only detached run cwd keeps.

**Gotchas:** Staff records carry no plan; resolution uses the registered production rather than the caller's checkout. Tests patch `registered_plan` and keep scratch/copy roots private. A paused roster worker or active unit run does not make a finished detached run record's cwd live.

**Ruled out:** A second unit-plan registry; semantic validation on legacy update/end paths.

### Phase 41 — A queue priority change reads only the new end of the build log, and a step waiting for the cargo token holds no place at the head  · status: done

#### As-built

`begin_priority_transition` persists `PriorityTransition.id` and `month_row_end`; `finish_priority_transition`, including recovery through `check()`, passes the saved boundary to `record.queue_priority(..., start=...)` for an idempotent suffix-only append. `waiter_state` returns `QueuedWaiter | TokenWaitingWaiter`: `token_wait` publishes `token_left_at` until `check()` consumes the token return, retaining order and clocks while excluding token waiters from head candidacy and reserved memory. `first_queued_waiter` selects the eligible head for the shared admission pass, override, queue readers, head history, and frozen blocker/release calculations without another stored head clock. `queue_read_from` produces `TokenWaitersOnly` in `QueueExplanation.head` for a nonempty queue with no eligible head; views and explanations report token waiting, and release search cannot claim a head admission. The recorder loads `AdmissionMetadata` lazily so both modules remain independently importable.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/buildlog/record.py` — transition recovery, named waiter states, admission, and bounded priority recording.
- `scripts/build_queue/{queue_view,queue_card,head_facts,build_why}.py` — eligible-head display, history, and explanation.
- Corresponding tests and `docs/as-built/build-memory-admission.md` — behavioral regressions and the token-wait rule.

**Gotchas:** The eligible head need not occupy row 1; an all-token queue differs from an empty queue. Saved row identity uses `pid` and `since`, because display labels can change. Legacy transition intents without `month_row_end` use `0`; new retries reuse the saved boundary and fixed id.

**Ruled out:** Full-log duplicate scans for new priority transitions; head-room reservations for token waiters.

### Phase 42 — Bump and token-wait edges: one bad intent, the pane's session, unit names, card text and type shapes  · status: done
**Depends on:** Phase 41

#### As-built

- One bad token-wait intent stops nothing else: each intent finishes in its own guard; an error is logged with its path and the intent stays for the next poll.
- The unit-board pane's bump passes the session id from `$.session.id()` to the process; it does not inherit `CLAUDE_CODE_SESSION_ID`.
- `priority set <unit>` refuses a unit that is not in the roster, and keeps the old priority. The unit goes to argparse as `--unit=<unit>`.
- The card names a unit-priority step `N of <unit> (queue priority)`, shows the priority line once, and renders the stored `why`.
- `priority_clear` removes an unreadable `queue_priority.json` and says so.
- Types: the resolved order is `RankedWaiter` with `PriorityUnit | NormalOrder`; a transition is `SettledPriority | PendingPriorityTransition`; these live in `scripts/build_queue/admission_service.py` and `scripts/lint/memory_admit.py`. "No waiting step" is `NoWaitingStep` in `scripts/build_queue/build_queue.py`.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/build_queue/admission_service.py` — intent guard, unit check, types
- `mods/unit-board/hooks/register.tsx` — bump session id
- `scripts/build_queue/queue_card.py`, `head_facts.py`, `build_queue.py`, `golden/card_*.txt` — card text, priority clear
- `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_queue_card.py`, `test_head_facts.py`, `test_build_queue.py`, `mods/unit-board/tests/queue.test.ts` — tests

### Phase 43 — Every agent Hana carries its owner and purpose, and `hanas` lists, stops and sweeps test Hanas on both machines  · status: done

#### As-built

- Hana-shot launch and capture, Mac-run and smoke launch carry `HANA_TEST_PURPOSE` and `HANA_TEST_OWNER`; owner defaults to `SHOWRUNNER_UNIT_ID`. Missing or blank purpose refuses before launch and before Mac SSH; the BRP Hana hook checks purpose before hold and memory checks.
- `config_values` reads `config/hanas.conf` each invocation: exempt ports 15702/15761, labelled lifetime 4 hours, unlabelled lifetime 30 minutes and stop grace 5 seconds. `HANAS_CONFIG` selects another file; Mac host and SSH settings come from `mac_test.conf`.
- `hanas list`, `stop <machine>:<pid|port>` and `sweep [--dry-run]` share one inventory, classification and stop path. Matching `target/<profile>/hana` yields machine, pid, port, owner, purpose, age and checkout without session lookup; natedev collects local `/proc` plus Mac SSH, while Darwin collects local `ps`.
- `HanaCheckout | NotHana`, `HanaProcess | NotHana`, `ProcessIdentity | ProcessGone` and `EnvironmentKnown | EnvironmentUnknown` distinguish match, lifetime and environment states. A label, known non-exempt port, or agent marker (`CLAUDECODE`, `SSH_CONNECTION`, `SHOWRUNNER_UNIT_ID`) marks a test Hana; unknown port alone does not. Stop protects user Hanas on exempt ports without labels or markers and refuses unknown environments.
- Each stop checks saved start identity before TERM and KILL, waits the configured grace and confirms the original process exited before printing `stopped`; reused pids receive no signal. Linux uses start ticks and Darwin UTC `lstart`; absent targets report `already gone` and succeed, while failed signals or missing exit confirmation fail. Reruns collect fresh identities.
- Sweep stops only overdue test Hanas; dry-run sends no signals. Unknown environments remain visible with their cause; an unreachable Mac leaves natedev rows visible. Sweep handles other known rows after a Mac inventory problem or a failed stop and returns nonzero; a repeated completed sweep succeeds with no further stops.

**Files:**
- `scripts/hanas/hanas.py`, `scripts/hanas/test_hanas.py` — inventory, classification, stop/sweep and private-process tests.
- `config/hanas.conf`, `config/README.md` — runtime settings and guide.
- `scripts/hana_shot/hana_shot.py`, `scripts/hana_shot/test_hana_shot.py` — purpose/owner flags, labels and refusal.
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/hooks/test_brp_launch_gate.py` — Hana purpose gate.
- `scripts/production/mac_run.sh`, `scripts/production/smoke_launch.sh`, `scripts/production/test_mac_run.py`, `scripts/buildlog/test_launch_recording.py` — labelled shell launches and caller/recovery tests.
- `commands/hana_shot.md`, `commands/showrunner/produce.md`, `docs/as-built/hana-test-windows.md` — launch guidance and tool behavior.

**Binds later work:** “Test Hana clones go through scratch, and the Mac keeps no idle clone below its floor” preserves required purpose, owner fallback, literal label quoting, refusal before SSH and launch recovery; uses runtime settings without changing Hana lifetimes or exemptions; keeps live or unreadable-environment checkouts busy; and reuses the inventory/process-use guard and stop path.

**Gotchas:** Darwin uses herdr's existing argv/environment boundary; assignment-like arguments are not labels. The remote stop program supports Bash and Zsh, uses `identity_status` because Zsh reserves `status`, and checks the full process table before declaring exit; remote success status alone proves nothing. The CLI exists in the repository; wrapper and both-machine sweep timer installation remain a director-only `/etc/nixos` branch-and-hash handoff. Hana's own refusal, title and idle-exit change waits for this installation.

**Ruled out:** A second Darwin environment lexer; roster-dependent discovery; unknown ports alone proving test ownership; failed environment reads proving absence or safety; failed signals proving exit; signalling reused pids; stopping sweep at its first failure.

### Phase 44 — Test Hana clones go through scratch, and the Mac keeps no idle clone below its floor  · status: done

#### As-built

- `scratch.clone(source, task, word, now)` returns the task parent's `checkout` child; the parent records `test_hana_clone` and `source_path` before Git starts, so task end also removes partial clones.
- Runtime `ClonePolicy` uses `clone_max_hours` (4 hours) for clone expiry and Mac `free_floor_gib` for oldest-first idle-clone removal until space recovers; younger clones qualify only under the Mac floor, which removes only `target` from dirty clones; other folder lifetimes stay unchanged.
- Discovery canonicalizes paths before hold, live-process, process-evidence, socket and busy-lock guards. Certified pending removal resumes through the same guards after partial Git metadata deletion; unavailable process evidence keeps the clone.
- `scratch.git_directory` and `busy_path` resolve normal and linked-worktree Git metadata for shared busy locks; legacy checkout-root locks remain guards.
- `hanas stop-checkout` uses identity-safe `hanas.stop_process`, inventories twice and checks other process use. Matching or nested unknown environments and global inventory failures refuse cleanup; only unrelated per-checkout environment diagnostics are ignored.
- `mac_run.sh` requires successful stop-checkout and the exact `hanas: checkout clear` line before ending its gate task; failed or unconfirmed cleanup keeps the task and checkout, prints the cause and returns 4 on normal cleanup failure. Missing or blank `HANA_TEST_PURPOSE` refuses before SSH; generated Terminal commands preserve purpose and `HANA_TEST_OWNER` (default `SHOWRUNNER_UNIT_ID`) with Zsh `(q)` quoting.

**Files:**
- `scripts/lib/scratch.py`, `scripts/lint/folder_audit.py` — owned clones, shared locks, runtime policy and guarded recovery.
- `scripts/production/mac_run.sh`, `scripts/hanas/hanas.py` — task-owned launch and confirmed scoped checkout exit.
- `config/hanas.conf`, `config/mac_test.conf`, `config/rust_roots.txt` — runtime limits and removal of the standing-clone exemption.
- Corresponding scratch, audit, Mac-run and Hana tests plus two production lock probes — lifecycle, refusal, recovery and lock contracts.
- `commands/showrunner/produce.md`, `docs/as-built/hana-test-windows.md` — scratch clone and launch guidance.

**Gotchas:** `/tmp` and `/private/tmp` must reach guards as the same canonical path; Git locks belong inside resolved metadata to keep clean checkouts clean. Successful SSH status or a sent signal alone cannot confirm checkout exit.

**Ruled out:** Raw TERM as exit proof; root locks that dirty Git; standing-clone exemptions; bypassing live-use checks for pending removal; unrelated per-row environment failures blocking cleanup.

### Phase 45 — Hanas and the folder audit read their settings through the one config reader  · status: done

#### As-built

- Hard-coded settings in this unit's files (108) are config keys read through `scripts/lib/conf.py` at use, with their old values: `hanas.conf`, `lint.conf`, `production.conf`, new `hana_shot.conf` and `scratch.conf`. `config/README.md` lists each key.
- One key per value: `sweep.py` takes state and build-log folders from `tmp_sweep.state_dir()` and `buildlog/store.root()`; `tmp_sweep.wide_idle_seconds()` and the folder audit share `folder_remove_after_seconds`; the audit walks with `scratch.conf` `repo_skip` and keeps the configured `visible_root`; `smoke_launch.sh` reads `hana_shot.conf` `refused_port` and `host`; `mac_run.sh` reads `mac_test.conf` `connect_timeout_s`, rounded up for ssh; holder messages use the first `sweep_message_path_widths` entry; `hold_floor(floor, dry_run)` reads roots and lock from config.
- Hot paths read each setting once per call (scratch repository scan, `Brp` instance, Hana polls, folder scans, dep-info scan).
- Standalone `hana_shot.py` loads its libraries without `PYTHONPATH`. A config read failure in `scratch.holds()` keeps an active hold. `mac_run.sh` gives `scp` the raw remote path and `ssh` the quoted one.
- Tests use `CONF_DIR`; `memory_test_support.write_config_fixture` keeps existing keys and adds the given ones.

**Files:**
- `scripts/hanas/hanas.py`, `scripts/lint/folder_audit.py`, `scripts/lint/sweep.py`, `scripts/lint/tmp_sweep.py`, `scripts/lib/scratch.py`, `scripts/hana_shot/hana_shot.py`, `scripts/production/mac_run.sh`, `scripts/production/smoke_launch.sh` — config reads.
- `config/*.conf`, `config/README.md`, `docs/as-built/build-memory-admission.md`, `build-targets.md`, `hana-test-windows.md` — keys and docs.
- Their tests, plus fixture fixes in `test_verify_mac_fallback.py`, `test_offload.py`, `test_linux_runner.py` and merge repairs in `test_build_why.py`, `test_queue_card.py`, `test_merge_gate_suites_resources.py`.

**Gotchas:** a test fixture that writes a partial `.conf` must start from the full template; `write_config_fixture` merges. A shell-quoted path is wrong for `scp`'s SFTP destination. `mac_test` allows fractional seconds but ssh `ConnectTimeout` needs a whole number.

**Ruled out:** value changes (none without the user); `conf.read` (devtools 4r not landed); edits to `memory_admit.py` (queue-unit's; it keeps its own `LINT_SWEEP_STATE_DIR` default).

### Phase 46 — Each shared config key has a test that proves its reader uses it  · status: done

#### As-built

- Six regression tests guard the Phase 45 shared-key reads, each with a private `CONF_DIR` value that differs from the default: `tmp_sweep` folder age, folder-audit configured visible root and `repo_skip`, `smoke_launch.sh` refused port and host (curl stubbed), `mac_run.sh` connect timeout rounded up for ssh, and the first sweep message width.
- Each test fails when its read goes back to the old fixed value (8 mutation checks).

**Files:** `scripts/lint/test_tmp_sweep.py`, `scripts/lint/test_folder_audit.py`, `scripts/production/test_mac_run.py`, `scripts/lint/test_sweep.py`.

**Gotchas:** a test that copies the repo `lint.conf` must set every value its expected text depends on; the `mac_run` command stub links `curl`, so no test reaches the network.

### Phase 47 — This unit's settings reads use conf.read  · status: done

#### As-built

- Hanas, Hana shots, scratch, the folder audit, the disk sweep and the tmp sweep call `conf.read(file)` once for each pass and give the `ConfigRead` to their helpers. Every key, value and reader kind is the same.
- A helper that takes the read defaults to `UseConfiguredValue.CURRENT`. `sweep_workspace(target_dir, budget_config: conf.ConfigRead, dry_run, recent_minutes, *, lint_settings=UseConfiguredValue.CURRENT)`: the Linux runner passes its own budget file; other sweep settings still come from the repo `lint.conf`.
- `UseConfiguredValue` has one definition, in `scripts/lib/conf.py` (devtools 4q item 13). No lib, lint or production module imports `scripts.issue_score`.
- `smoke_launch.sh` checks the binary before its purpose check and reads config through `scripts/lib/py`.

**Files:** `scripts/hanas/hanas.py`, `scripts/hana_shot/hana_shot.py`, `scripts/lib/scratch.py`, `scripts/lib/conf.py`, `scripts/issue_score/selection.py`, `scripts/lint/folder_audit.py`, `scripts/lint/sweep.py`, `scripts/lint/tmp_sweep.py`, `scripts/mac_test/linux_runner.py`, `scripts/production/smoke_launch.sh`, the `UseConfiguredValue` importers (`scripts/issue_score/`, `scripts/accounts/next_moves.py`, `scripts/direct/simplify_result.py`, `scripts/production/retire_unit.py`), and their tests.

**Gotchas:** a lib module that imports a package outside `scripts/lib` fails in the merge gate's command copy, though the worktree suites pass. The tests `test_environment_budget_does_not_need_a_config_file` and `test_workspace_recovers_doc_budget_separately_from_a_private_budget_file` guard the two sweep budget reads.

### Phase 48 — The test Hana tool gets a clear name: `hanas` becomes `hana_instances`  · status: done

#### As-built

- The test Hana tool is `scripts/hana_instances/hana_instances.py` (tests in `test_hana_instances.py`), with settings in `config/hana_instances.conf` read by `conf.read("hana_instances")`. Keys, values and behavior are unchanged from `hanas`.
- Its output prefix is `hana-instances:`, the shell command is `hana-instances`, and the sweep job is `hana-instances-sweep` (nixos `modules/common/agents.nix`, branch `resources-hanas-sweep`).
- Callers use the new names: `scripts/lint/folder_audit.py`, `scripts/production/mac_run.sh`, `config/README.md`, `docs/as-built/hana-test-windows.md` and `docs/as-built/build-memory-admission.md`. The Mac runs `mac_run.sh stop-checkout` from its own `~/.claude`, so it uses the new name after it takes main.

**Files:**
- `scripts/hana_instances/` — the tool and its tests
- `config/hana_instances.conf` — its settings

### Phase 49 — A hold that starts during a merge gate stops whole test modules, so the gate runs them again after the release  · status: done

#### As-built

- When a process to stop has a waiting-gate ancestor (`is_waiting_gate`: `merge_gate.py` or `verify.sh`), `scripts/build_hold/hold_enforcer.py` stops that gate's child job that contains it, with all its descendants. The gate stays protected, sees its job end on SIGTERM during the hold, and queues the module again ("stopped during a natedev hold; queued again").
- A process is kept when its step record's controller is kept or has a kept ancestor, as well as by live ancestry.
- `verify.sh` `step_stopped_by_hold` reads a hold stop only when the step ended on a signal; a step that fails on its own with the refusal text in its log is a failure.
- Kept holders, the sole holder, sccache, user terminals, the refusal line and the stop record are unchanged.

**Files:**
- `scripts/build_hold/hold_enforcer.py` — gate-job stop and step-record keep
- `scripts/direct/verify.sh` — signal-only hold stop
- `scripts/build_hold/test_build_hold.py`, `scripts/direct/test_verify_hold_wait.py` — the tests

### Phase 50 — The Mac runs the floor sweep, and it never deletes a mirror target that a Mac test claim holds  · status: done

#### As-built

- `sweep.py --floor-only --host mac` runs the floor sweep on the Mac. verify-unit calls it over ssh when Mac test admission declines for disk, and a nix-darwin job (branch `resources-mac-floor`) calls it on the interval of `disk-floor.nix`.
- The Mac floor is `config/mac_test.conf` `sweep_floor_gib`, read at use through `conf.read`. A missing value, or one below `free_floor_gib`, refuses with one line.
- The sweep never deletes a mirror target that a live Mac test claim holds. `scripts/mac_test/floor_claims.py` reads the claims that `scripts/mac_test/offload.py` writes, with no `/proc`; an unreadable claim counts as busy.
- It takes only the shortfall, least recently used idle target first, from the Mac roots only: builds and mirrors this tooling created in tmp and `~/rust` (user rule 2026-11-03). It prints one line with the bytes removed, free space before and after, and busy targets skipped.

**Files:**
- `scripts/lint/sweep.py` — the Mac floor entry point
- `scripts/mac_test/floor_claims.py` — the live claim check
- `config/mac_test.conf` — `sweep_floor_gib`

### Phase 51 — Build output moves to the /build drive, and every tool that reads build folders follows it  · status: done

#### As-built

- `scripts/lib/cargo_output.py` is the one mapping from a checkout to its Cargo output roots: `CargoOutputRoots(target_directory, build_directory)` or `CargoOutputUnavailable(reason)`. On natedev, `build.build-dir` puts intermediates under `/build/cargo/<workspace-path-hash>`; final binaries stay in `target/`.
- The step recorder and the index record both roots (`scripts/buildlog/record.py`, `scripts/buildlog/index.py`; index schema 29). `verify.sh` prints a three-line build key.
- `tree_watch.py` resolves the roots from the metadata workspace root, so a nested workspace uses its own roots.
- `memory_admit.is_fresh_test` takes `CargoOutputRoots | CargoOutputUnavailable` and is never fresh when a root is unavailable, missing or swept.
- The floor sweep takes and keeps both roots as one pair: a CI exclusion keeps the whole pair, recovery checks saved descendants before a recursive delete, and a changed pair clears its obsolete intent. `sweep.saved_cargo_output_roots()` exposes the saved index. The `/build` floor is 20% free.

**Files:**
- `scripts/lib/cargo_output.py` — the root mapping
- `scripts/lint/sweep.py` — two-root takes, pair recovery, CI exclusion
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py` — both roots per step
- `scripts/direct/tree_watch.py` — roots from the metadata workspace root
- `scripts/lint/memory_admit.py` — two-root freshness
- `scripts/direct/verify.sh` — three-line build key

**Binds later work:** freshness proves that both roots survived; trim attribution and the nextest compile-band predictor read the recorded roots.

### Phase 52 — The build hold audit classifies a reused pid and an admitted step correctly under load  · status: done

#### As-built

- The build hold audit reads admitted owners from durable grants: `admitted_processes()` (`scripts/build_hold/build_hold.py`) reads `admission_service.read_state(store.root() / "admission")`, which the service writes before it replies and before its next decision pass. An unreadable state raises `OSError`.
- A pid matches an admission only with the same start time as its grant, so a reused pid is never `admitted`, and an admitted step stays `admitted` when the system is slow.

**Files:**
- `scripts/build_hold/build_hold.py` — admitted owners from durable grants
- `scripts/build_hold/test_build_hold.py` — reused-pid and slow-read tests

### Phase 53 — After the /build switch, cleanup removes a checkout's outside build cache, and the disk reports show each drive  · status: done

#### As-built

- Removing a checkout also removes its outside Cargo build cache. `scratch.remove_with_cargo_output` writes a per-removal mark (`.scratch-cargo-<name>.json`) and keeps it until cleanup finishes, so a rerun completes a stopped removal. A bad mark raises `OSError`, keeps its source, and does not stop other folder removals.
- `folder_audit.py --clean-cargo-output` is the one guarded delete, run as a subprocess. It keeps an output that is live, locked, used after the attempt started, or inside another cache (both roots, both directions). An empty or unreadable index raises `OSError` and keeps the source and the mark.
- The disk reports (`disk.py`, `sample.py`, `report.py`) show the drive that holds each saved output root, and report unavailable when the mapping fails.
- The Linux Mac-test probe reuses `output_free_bytes()`: it measures the nearest existing parent of each output, retries when an output is removed during measurement, and reports "disk capacity unavailable" when the mapping fails.

**Files:**
- `scripts/lib/scratch.py` — removal mark and outside-cache cleanup
- `scripts/lint/folder_audit.py` — guarded delete of a checkout's outside cache
- `scripts/buildlog/disk.py`, `scripts/buildlog/sample.py`, `scripts/buildlog/report.py` — per-drive reports
- `scripts/mac_test/linux_runner.py` — the probe's free-space read

### Phase 54 — The floor dry run prints the free size above the floor  · status: done
**Depends on:** Phase 53

#### As-built

- A floor dry run at or above the floor prints `lint sweep: <path>: <N> GiB free, at or above the <F> GiB floor; nothing to remove` (`_hold_floor_pass`, `scripts/lint/sweep.py`). A real run prints nothing new.

**Files:**
- `scripts/lint/sweep.py` — the dry-run line
- `scripts/lint/test_sweep.py` — `test_floor_dry_run_above_the_floor_prints_the_free_size`

### Phase 55 — The merge gate of phases 41-54 is green: build kills, example metadata reads and the ssh stop event  · status: done
**Depends on:** Phase 54

#### As-built

- A program under a Cargo target folder is a build kill (`EarlyoomKill.is_build`, `outside_build_gate`, `scripts/buildlog/memory.py`). Each kill checks the program's resolved parent folders with `sweep.is_cargo_target`; no roots are cached and no `cargo metadata` probe runs.
- Each verify run's example tests count only the example scan's `cargo metadata` reads; `tree_watch`'s tree-key read is named apart.
- A SIGTERM from the ssh runner writes one `InterruptedTargetRun` event with its generation and remote output; a step that a hold stopped still goes the hold path (`scripts/direct/verify.sh`).
- The branch's admission reader accepts the snapshot shapes that the installed service writes (`installed_snapshot_record`, `scripts/lint/memory_admit.py`). It refuses a `BehindHead` record that carries both `head_since` and `joined_at` (ValueError).
- Test Hana clones come from `copies.py clone <source> --task gate:<id>` (`scripts/copies/copies.py`). The task folder is claimed first, the checkout is built with `initialize_git`, a failed clone leaves the partial folder owned, and the checkout keeps no `BUSY` mark. `scratch.clone` and `scratch.py clone` are gone; `scripts/production/mac_run.sh` calls `copies.py clone`.
- `scratch.py` does not import `copies.py` (copies imports scratch; basedpyright `reportImportCycles`). A raw `git clone` in a script fails `scripts/copies/test_copy_guard.py`.
- Tests that read Cargo output roots use one helper: `fake_cargo_output_roots` (`scripts/lib/cargo_output_test_support.py`); `children=True` puts the same mapping into Python child processes.
- The buildlog index (schema 30) keeps `target_directory` and `build_directory` (resolved Cargo roots) beside `build_history_group` and the step-context target folder.
- Ruled out: adding `scratch.py` to the copy-guard allow-list; a function-local import of `copies` in `scratch.py`.

**Files:**
- `scripts/buildlog/memory.py`, `scripts/buildlog/test_memory_window.py`, `scripts/buildlog/test_moments.py` — build-kill roots
- `scripts/direct/tree_watch.py`, `scripts/direct/verify_examples_test_support.py`, `scripts/direct/verify.sh`, `scripts/direct/test_verify_ssh_targets.py` — metadata reads, ssh stop event
- `scripts/lint/memory_admit.py`, `scripts/build_queue/test_admission_service.py` — installed snapshot reader
- `scripts/copies/copies.py`, `scripts/copies/test_copies.py`, `scripts/lib/scratch.py`, `scripts/production/mac_run.sh` — test clone owner
- `scripts/lib/cargo_output_test_support.py` — shared Cargo output fixture
- `scripts/buildlog/index.py`, `scripts/buildlog/record.py` — index schema 30
### Phase 56 — Build holds and memory admission catch store interface errors before they run  · status: done

#### As-built

- `hold_enforcer.py` imports `store` from `..buildlog` under `TYPE_CHECKING` and as `import store` at run time. The `BuildLogWriter` protocol, the `importlib` load and the cast are gone.
- `memory_admit.py` re-exports `store` with `from ..buildlog import store as store`, so callers read `gate.store` and basedpyright checks it. It imports `parse`, `record` and `index` under `TYPE_CHECKING`; the `GitFacts`, `StepParser`, `GitIdentityReader`, `BuildLogFileMarks` and `BuildLogStore` protocol copies and their casts are gone.
- `test_memory_admit.py` `BuildStepRecorder` uses the real `record` module.

**Files:**
- `scripts/build_hold/hold_enforcer.py` — `store` import for the type checker and at run time.
- `scripts/lint/memory_admit.py` — the `store` re-export and the type-checker imports of `parse`, `record` and `index`.
- `scripts/lint/test_memory_admit.py` — `BuildStepRecorder` over the real `record` module.

**Gotchas:** a plain import without `as store` is private to basedpyright, so callers that read `gate.store` fail the type check. `build_hold.py` and `head_facts.py` still load `store` as a second module object (`scripts.buildlog.store`); the gate comparison phase fixes this.

**Ruled out:** caller-side `store` imports in six files, replaced by the one re-export.

### Phase 57 — The gate comparison shows Cargo waits with and without running gates  · status: done

#### As-built

A measurement script in the unit run folder splits Cargo memory waits by recorded merge-gate overlap for the first 24 hours after Phase 20 was installed. It changes no repository file.

- `compare_lifetimes(lifetimes, *, window_start, window_end) -> WindowComparison` reads only through `read_queue_lifetimes()`. It unites overlapping gate intervals and counts each logical wait once. It keeps token gaps, incomplete waits, open intervals, cancelled gates and missing peak samples separate.
- Phase 20 reached the installed natedev copy at 2026-11-04 15:45:21 EST and origin/main at 15:45:22 EST, in `7e5c326df`. The comparison was due at 2026-11-05 15:45:21 EST.
- The report has an After window only: 23.996 usable hours, with gates running 23.298 of them. It shows 241 Cargo steps, 240 complete waits and 1 incomplete wait. Cargo waits are 4313.325 minutes with gates, 12.495 minutes without gates and 2.230 minutes unknown.
- 229 gates ran and 8 were cancelled. Two gate endings have no admission record. Token gaps are 0 minutes, because no token return is recorded. Four peak samples are missing.
- Pressure comparison is unavailable, because the memory PSI avg10 series is not recorded.
- The result does not show that gates cause Cargo waits.

**Files:**
- `phases/57/gate_comparison.py`, in the unit run folder — the measurement script.
- `phases/57/test_gate_comparison.py`, in the unit run folder — private fixtures and tests.
- `phases/57/gate_comparison_report.md` and `phases/57/evidence/`, in the unit run folder — the report and the copied evidence.

**Gotchas:**
- A commit time does not prove arrival on main. `4298a9aaf` at 16:30:03 EST was never an origin/main tip; the origin/main reflog gives the arrival.
- Lifecycle recording began 13 seconds after the install, at 15:45:34 EST. `read_queue_lifetimes()` drops 110 older `token_gap` rows that have no `since`.
- `read_queue_lifetimes()` discards an unmatched ending that is a `TokenReturn` or that has other intervals in its step. This evidence has neither case.
- The no-gate rate rests on about 0.67 hours. A window 45 minutes later changes it from 19 to 362 wait minutes per hour.

**Ruled out:**
- A before and after comparison: recording began at the deployment.
- Comparing the with-gate and no-gate rates: the no-gate period is too short.
- A second lifecycle parser or gate intervals rebuilt from Cargo step rows.

### Phase 58 — The disk floor alert stays silent during a live sweep, and the floor pass finishes quickly  · status: done

#### As-built

- `alert_floor_lock()` (`scripts/lint/sweep.py`) sends nothing while the `floor.lock` holder is a live `sweep.py` process. An unreadable holder gets the alert after `floor_lock_alert_minutes`; `ObservedUnreadableFloorHold` keeps the first-observed time and resets when the holder pid changes or the lock is free.
- One notification finalizer, `notify_floor_problem`, runs inside the floor lock for every pass outcome. It sends first, then writes the floor record, so a failed send is sent again on the next pass. A contended pass (`SkippedFloorAssessment`) changes no state.
- Crossing states `AbovePhoneFloor`, `UnderPhoneFloorPending`, `UnderPhoneFloorPushed` (line `sweep_phone_floor_gib`, 150 GiB) and failure states per scope `NoFailure`, `FailurePending`, `FailureReported` (scopes: root, discovery) give one alert per episode.
- Every floor alert goes to `FloorAlertChannels.NATEDEV` only. The phone gets no disk push; `sweep.disk_floor` and `sweep.disk_cleanup_failed` are not in the push catalog.
- Output discovery runs Cargo metadata in parallel, the floor scan shares one listing per folder, and `newest_times(entry, observations)` gives fingerprint ages that include the directory mtime. A floor-only dry run on natedev takes 23 to 46 s, by load and by whether a sweep ran in the last few minutes.

**Files:**
- `scripts/lint/sweep.py` — the lock alert, the notification finalizer, the crossing and failure states, parallel discovery and shared listings.
- `scripts/lint/test_sweep.py` — each state transition, the retry after a failed send, live-sweep silence.
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — no disk push ids.
- `config/lint.conf`, `config/README.md` — `sweep_phone_floor_gib=150`; `sweep_push_text_limit` and `sweep_page_paths` have no reader.

**Binds later work:** the finalizer order (send, then write, inside the floor lock) and the crossing and failure-episode states stay; workspace and temporary-sweep failures join them with their own scopes. The low-space phone text, the two keys with no reader and the `Phone` state names belong to the phone push work. The floor's folder mapping moves to recorded step rows, with no Cargo metadata.

**Gotchas:**
- `UnderPhoneFloorPushed` records a natedev delivery only; it does not prove a phone delivery.
- Floor dry-run time varies by 2x under build load on 32 cores. A timing gate states the load and whether a sweep ran in the last few minutes.
- The push text review rejects a text that asks nothing of the user. Only `review.py texts --approve` on a passing text writes an approval record.

**Ruled out:** phone pushes for low space and sweep failure in this phase (text review failed; builds ruling 2026-11-04); an override of the text review with `--user-spec`; a persistent Cargo metadata cache.

### Phase 59 — Idle hana runners hold no swap: the runner home moves from tmpfs to disk  · status: done

#### As-built

- In the nixos repository, `modules/linux/hana-runners.nix` gives each hana runner `homeDir = "${cacheDir}/home"` and `workDir = "${cacheDir}/work"` under `cacheRoot = "/var/lib/hana-ci"`. The service `HOME` is `homeDir` (`.nv` shader cache, `.config`, `.dbus`); its working directory is `workDir` (`_actions`, `_diag`). tmpfiles rules create both with mode 0700 and the runner account as owner.
- Registration state stays on the tmpfs at `%S/github-runner/hana-linux-N`. The passwd home stays at `cacheDir`, so `cargo-tile install --all-accounts` finds `.rustup` as before. The `ACTIONS_RUNNER_HOOK_JOB_COMPLETED` sweep hook is unchanged.
- Branch `resources-runner-home` (7eaeba1) is on nixos main as cb366fc. After the rebuild, `memory.swap.current` of `hana-ci.slice` and both `github-runner-hana-linux-N.service` units read 0 while CI was idle (2026-11-04 16:33 EST).

**Files:**
- `modules/linux/hana-runners.nix` (nixos repository) — the runner home and work directory on disk.

**Gotchas:** the shader cache starts cold once after the move, and the first job refills `_actions`.

### Phase 60 — A filtered compile run's promise comes near what that run uses  · status: done

#### As-built

- `StepEvidence.arguments` (`scripts/lint/build_shape.py`) holds the decoded argv of test steps (`TEST_STEPS` = nextest, test, t). `index_step_evidence` keeps the argv and parses no test selection.
- `history_selection(item, evidence)` (`memory_admit.py`) is the one resolver of package, scope and filters for a history row. A recorded field wins; only a missing field is parsed from `StepEvidence.arguments` through `test_selection`, once per row.
- `expected_peak`: a full test request with a package keeps only rows whose package and scope match (test steps only).
- `filtered_peak`: the larger of the exact-filter p90 and the package p90 from anonymous rows, then the indexed exact and package peaks × `ANON_SHARE`, then check/build peaks, the compile index and `FILTERED_FLOOR`.
- The rebuilt replay (`phases/60/replay.py` in the run folder) classifies calls through `index_step_evidence` and `combine_compile_counts`. At this checkpoint: 395 under-half and 2,525 over-three over 15,053 runs; cargo-liner nextest compile runs at 21 GiB: over-three 166 → 65, under-half 7 → 0.

**Files:**
- `scripts/lint/memory_admit.py` — the selection parsers (`test_selection`, `test_package`, `test_scope`, `test_filters`), `history_selection` and the estimate tiers.
- `scripts/lint/build_shape.py` — `TEST_STEPS` and `StepEvidence.arguments`.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py` — legacy selection recovery, package isolation, the max rule and the argument tests.
- `docs/as-built/build-memory-admission.md` — the selection text and the Phase 60 replay comparison.

**Binds later work:** compile bands apply inside these selections, never around them. `history_selection` stays the one place rows get package, scope and filters; a later reader of argv selection uses `test_selection` or `history_selection`, not a new parser. The replay at this checkpoint is the baseline for the compile-band adopt rule.

**Gotchas:**
- Anonymous rows before 2026-10-29 15:05Z have no package, scope or filter fields. Recovery reads the indexed argv. The effect ends about 2026-11-12 (`HISTORY_DAYS` = 14).
- Parsing argv for every index row makes `index_step_evidence(nextest)` 3× slower. Parse only rows with a missing field.
- Each `compiling_history` call decodes the whole `anon_peaks.jsonl`; `filtered_peak` calls it three times.

**Ruled out:** named availability states for `StepEvidence.arguments` — `history_selection` reads an empty argv and a missing one the same way.

### Phase 61 — Workspace and temporary-sweep failures reach natedev once, and the Phase 58 floor code has one copy of each rule  · status: done

#### As-built

- Workspace and temporary-sweep failures go through the floor failure-episode finalizer, and each failure episode sends one natedev alert. A sweep failure sends no phone push. A clean pass ends the episode.
- Each scope has its own record beside `floor.json`: `workspace-<sha256 of the sorted output roots>.json` for each workspace, and `temporary-sweep.json`. A run from the workspace root and a run from a member folder share one record. A clean workspace sweep with no saved record writes no state file.
- `tmp_sweep.SweepAssessment = CompletedSweepAssessment | SkippedSweepAssessment`. `run_if_due` returns a skip when the sweep is not due. A workspace pass with lock contention or with no output root is also a skip. A skip changes no state, so it keeps an open episode. `report_assessment(run, scope, ...)` runs a sweep and reports its assessment.
- `FloorOutcome.assessment` holds a `tmp_sweep.SweepAssessment`. `notify_floor_problem` accepts `FloorOutcome | tmp_sweep.SweepAssessment`.
- `send_floor_alert` returns `FloorAlertDelivery(natedev, phone)`, with one `FloorChannelOutcome` per channel (not attempted, delivered, queued or failed). `send_holder_message` is the natedev leg; it returns `FloorChannelOutcome` and logs "message to natedev ...".
- The lock alert has one pure transition, `next_lock_alert(previous, holder, process, now)`, also used at lock acquisition. The record is written only when the state changes. An unreadable hold is `ObservedUnreadableFloorHold` or `ReportedUnreadableFloorHold`; an old `alert_delivered` true decodes as reported, false as observed. A pass that sees no holder keeps `DeliveredFloorLockAlert`, so the same holder does not alert again.
- One copy of each floor rule: `class PhoneCrossing(Enum)`, `floor_record_or_new(previous, free)`, one pid check, `_command_text(bytes)`, and one `roots_for` lookup per distinct root. `target_dirs` is one loop over a named 3-level depth.
- `config/lint.conf` sets `sweep_scan_workers.natedev=1` and `sweep_discovery_workers=4` (the directory-search pool). The code no longer forces one scan worker.
- A real floor pass walks each idle target once, and walks again only the targets that had deletions to compute `after_caches`.

**Files:**
- `scripts/lint/sweep.py` — failure scopes, `report_assessment`, the lock-alert transition, the channel outcomes and the floor code.
- `scripts/lint/tmp_sweep.py` — the `SweepAssessment` types used by both sweeps.
- `config/lint.conf` — the scan and directory-search worker counts.
- `scripts/lint/test_sweep.py`, `scripts/lint/test_tmp_sweep.py` — tests.
- `docs/as-built/build-targets.md` — the failure records and the lock-alert rules.

**Binds later work:** The phone low-space push keeps the natedev leg `send_holder_message` and the phone leg of `send_floor_alert` apart, and keeps `notify_floor_problem` accepting `FloorOutcome | tmp_sweep.SweepAssessment`. A no-holder pass keeps `DeliveredFloorLockAlert`; `test_missing_lock_observation_preserves_the_delivered_holder_receipt` stays unchanged. Workspace and temporary-sweep failures keep their own records and never send a phone push.

**Gotchas:**
- A test that calls `sweep_workspace` with `dry_run` false writes the real `~/.local/state/lint-sweep` folder unless it sets `tmp_sweep.STATE_ENV` to a temporary folder.
- `after_caches` is never computed from blocks minus freed inodes: hard links make that number wrong.

**Ruled out:** a scope keyed by the calling folder (a member folder and its workspace root must share one episode); the stand-in arguments to `next_lock_alert` at acquisition and the phone branch of `send_floor_alert` stay, because the phone push uses that branch.

### Phase 62 — The floor sweep takes its target folders from recorded step rows  · status: done

#### As-built

- `build_shape.recorded_output_roots(index_path, host)` (`scripts/lint/build_shape.py`) reads the distinct step rows of every step kind on this host (worktree, cwd, target, build, argv, target_folder). It returns `RecordedOutputRoots`, the attempt pairs per workspace keyed by `RecordedRoot` (`AbsoluteRecordedRoot | UnknownRecordedRoot | RelativeRecordedRoot`), or `RecordedRootsUnavailable(reason)`. `_step_index` opens the index read-only for this reader and for `index_step_evidence`, whose results are unchanged.
- A row whose argv sets its own target folder (`--target-dir`, `--config …target-dir`), or whose absolute `target_folder` differs from `target_directory`, gives no evidence.
- `sweep.discover_cargo_outputs` maps targets from the recorded pairs, then from the saved cache (workspaces and history). It runs no Cargo metadata and has no worker pool. A target with neither is `NoRecordedEvidence`: it is protected, counts in the left-alone total and is never a floor holder. `target_dirs` also searches the recorded and cached output roots outside the configured roots, so a nested unrecorded target there is found and protected.
- A real pass saves the merged pairs to `cargo-output-roots.json`; history keeps extra pairs. `folder_audit` and `disk.py` read that cache.
- With the index unavailable, the saved cache alone applies, and each pass prints `lint sweep: recorded output roots unavailable: <reason>` to stderr.
- The natedev low-space alert lists the largest protected folders with sizes (`protected_folder_lines`, limited by `sweep_measurement_paths`, sized once per pass, without the recorded targets inside them).
- A floor-only dry run on natedev takes 6.8-8.5 s at load 64-66.

**Files:**
- `scripts/lint/build_shape.py` — the shared recorded-root reader beside `index_step_evidence`.
- `scripts/lint/sweep.py` — floor discovery from recorded pairs and the cache, `NoRecordedEvidence`, the protected folder measurement and alert lines.
- `docs/as-built/build-targets.md` — the floor rule: a target folder from before the step rows recorded it is not deleted until a new step builds it.
- `scripts/lint/test_sweep.py`, `scripts/lint/test_build_shape.py` — tests.

**Binds later work:** `build_shape.py` holds two `_step_index` readers, `recorded_output_roots` and `index_step_evidence`; keep them apart and keep one join. The floor keeps its recorded-root mapping, its speed and the `NoRecordedEvidence` protection. A phone low-space text uses the same protected-folder measurement and never makes a protected folder a floor holder. Trim matching for rebuild reports reuses the shared root decoding.

**Gotchas:**
- `scripts/buildlog/record.py:925` reads Cargo metadata without a step's target-folder override, so such rows give no evidence until the recorder is fixed.
- `scripts/buildlog/memory.py` `build_output_roots()` still runs Cargo metadata per checkout.
- A cold-run timing cannot be measured on natedev: `disk-floor.timer` runs the floor every 120 s.

**Ruled out:** `target_folder` as the authoritative output root (builds ruling 2026-11-04).

### Phase 63 — The phone gets one low-disk push under the alert line, with a text that passes the review  · status: done

#### As-built

- When free space falls under `sweep_phone_floor_gib`, the floor finalizer sends one natedev message (`send_floor_alert`, key `natedev:<crossing identity>`) and, with a current text approval, one phone push (`send_phone_push`, key `phone:<crossing identity>`). Each channel keeps its own receipt in the floor record and retries alone. Both return `FloorChannelOutcome`.
- The crossing identity is saved in the record before the first send. A crossing whose identity cannot be saved uses the key `natedev:unsaved-crossing` (`UNSAVED_CROSSING_KEY`) with the `sweep_alert_interval_seconds` window and sends no phone push. A failure message carries no crossing key; its acceptance stores `FailureReported`.
- An old record with `PhoneCrossing.UNDER_PUSHED` gets its first phone push once, with no second natedev alert.
- The `sweep.disk_floor` catalog text (`scripts/notify/push_catalog.py`) has four forms: available (recommends one choice), insufficient (says what the largest choice frees), protected (folders the floor never deletes, labelled as protected, with their sizes) and unavailable (names no folder). Each form ends with a do-nothing choice. Folder labels are full home-relative paths. `fit_disk_floor_push` trims the smallest choice to `sweep_push_text_limit` with the real values, then falls back to the unavailable form.
- `disk_floor_channels()` keeps the phone off until `push_texts.approval_date("sweep.disk_floor")` exists. Until the user approves the text, `scripts/notify/test_push_texts.py` fails only on that missing approval.

**Files:**
- `scripts/lint/sweep.py` — crossing receipts, `send_floor_alert` (natedev only), `send_phone_push`, `run_send`, the folder choices and `fit_disk_floor_push`.
- `scripts/notify/push_catalog.py` — the `sweep.disk_floor` template and its four examples.
- `config/lint.conf`, `config/README.md` — `sweep_crossing_repeat_minutes`, `sweep_push_text_limit`, `sweep_page_paths`.
- `scripts/lint/test_sweep.py` — crossing, receipt, key and text tests.

**Gotchas:**
- `send.py` exits 0 for a skipped repeat key. Never key a message whose acceptance records new state.
- Only the user records the text approval (`review.py texts --id sweep.disk_floor --approve`).

**Ruled out:** Cargo labels for the measured folders (the measurement excludes Cargo targets); a director-recorded text approval (the approval is the user's).

### Phase 64 — Recorded step outputs have one owner, distinct rows and row facts  · status: done

#### As-built

- `scripts/buildlog/step_outputs.py` owns the recorded-output reader. `recorded_output_roots(index_path, host)` opens the steps index read-only and returns `RecordedStepOutputs(rows)` or `RecordedRootsUnavailable(reason)`. It never creates an index.
- Each `RecordedStepOutput` holds facts, not rules: worktree, cwd, target folder, build folder and recorded target folder (`AbsoluteRecordedRoot | UnknownRecordedRoot | RelativeRecordedRoot`), plus `TargetFolderOverride` (`ABSENT`/`PRESENT`).
- The SQL `DISTINCT` covers the row facts only. A `target-dir` prefilter sets the override marker, so argv stays out of `DISTINCT`. `indexed_argv` decodes a stored argv.
- `scripts/lint/sweep.py` `_recorded_mapping_rows` applies sweep's own rule: skip `PRESENT` rows and rows whose absolute target folder differs; the workspace is the worktree, else the cwd; all three roots must be absolute. One dedupe follows the resolve.
- `scripts/lib/cargo_target_config.py` has `cargo_config_values(argv)`. `scripts/buildlog/width_trial.py` uses `indexed_argv`. `scripts/lint/build_shape.py` keeps only `index_step_evidence`.
- Reader CPU on natedev is 117.65 ms (was 400.22 ms) for 1,120 distinct workspace pairs. A floor-only dry run takes 7.57 s.

**Files:**
- `scripts/buildlog/step_outputs.py` — the reader and its row-fact types.
- `scripts/buildlog/step_outputs_test_support.py` — `create_step_index` and `record_root_attempt` for every steps-index test.
- `scripts/buildlog/test_step_outputs.py` — reader tests.
- `scripts/lint/sweep.py` — sweep's deletion rule over the facts.
- `scripts/lib/cargo_target_config.py`, `scripts/buildlog/width_trial.py` — argv decoding through the shared helpers.

**Binds later work:** Memory kill classification reads these rows and applies its own rule; `scripts/buildlog/memory.py` still calls `build_shape.recorded_output_roots`, and `scripts/buildlog/test_memory_window.py` still imports from `test_build_shape`, until the report-unit memory work merges. The floor's override-row evidence adds a row-time fact to `RecordedStepOutput`; the distinct row count and the reader CPU are measured again.

**Gotchas:**
- Keep one copy of each module in a process. At run time, import a `scripts/buildlog` module in the form its other importers use; `step_outputs` is bare today (`scripts/lint/sweep.py`, `scripts/buildlog/width_trial.py`). Type checking uses `if TYPE_CHECKING: from .x import …`.
- `create_step_index` keeps each column's declared type word. TEXT for every column breaks the build_shape tests.
- `scripts/build_queue/head_facts.py:17` loads a second `store` module copy through a bare import. Its owner fixes it.
- The Phase 62 protections hold: no recorded evidence is `NoRecordedEvidence`, an override row gives sweep no evidence, and an unreadable index prints `lint sweep: recorded output roots unavailable: <reason>`.

**Ruled out:** One reader that applies sweep's deletion rules for every caller (memory classification needs its own rule); argv inside `DISTINCT` (15,838 rows for 1,120 pairs).

### Phase 65 — Memory kill classification reads the shared step output rows and applies its own rule  · status: done
#### As-built
`memory.build_output_roots` reads the recorded step rows through `step_outputs.recorded_output_roots` and applies memory's own rule to the row facts (`memory.py` :97-108). It imports nothing from `build_shape` for this.
- `cargo_target_config.cargo_folder_settings` reads config files, then `CARGO_BUILD_TARGET_DIR`/`CARGO_BUILD_BUILD_DIR`, then the `--config` values through `cargo_config_values(argv)`, then `CARGO_TARGET_DIR`; report's strict configuration branch stays.
- `memory.py` imports `step_outputs` in its bare runtime form, the same as `sweep.py` and `width_trial.py`, so a process holds one copy.

**Files:**
- `scripts/buildlog/memory.py`, `scripts/lib/cargo_target_config.py` — the caller rule and the config reader.
- `scripts/buildlog/test_memory_window.py` — classification over override, differing target folder, usable and relative-worktree rows; its index comes from `step_outputs_test_support.py`.
- `scripts/buildlog/test_step_row.py` — `CargoFolderPrecedenceTests`, under both `CargoConfigPolicy` values.

**Binds later work:** report-unit Phase 25 deletes `memory.build_output_roots` and classifies a kill by `sweep.is_cargo_target` over the program's parent folders; after it lands, sweep is the only reader that applies a rule to the rows.
**Gotchas:** a package-form import (`from scripts.buildlog import step_outputs`) at run time loads a second module copy beside the bare one.

### Phase 66 — The floor takes evidence from target-folder override rows that the fixed recorder wrote  · status: done
#### As-built
A step row whose argv sets its own target folder gives the floor evidence when the fixed recorder wrote it.
- `scripts/buildlog/step_outputs.py`: `RecordedStepOutput.newest_start: StepStart`, where `StepStart = KnownStepStart(instant) | UnknownStepStart`. The reader groups the distinct facts and the override flag and returns `MAX(started_at_us)` for each group, so the row count stays the distinct count (1,194 rows on natedev, 0.14 s of CPU). `started_at_us` is integer UTC microseconds; text `MAX(started_at)` ordered `…36Z` above `…36.001Z`.
- `scripts/lint/sweep.py`: `TARGET_OVERRIDE_RECORDER_CUT = datetime(2026, 11, 5, 8, 31, 36, tzinfo=UTC)`, the time the fixed recorder reached natedev's installed `~/.claude` (from that checkout's reflog). `_recorded_mapping_rows()` has two guards: a target-folder mismatch is never evidence, and an override row is evidence only when its newest start is strictly after the cut. An unknown start keeps the override skip.
- A zone-free stamp reads as UTC, as in the index.

**Files:**
- `scripts/buildlog/step_outputs.py` — the newest start as a row fact.
- `scripts/lint/sweep.py` — the cut and the two guards.
- `scripts/buildlog/step_outputs_test_support.py` (`record_root_attempt(..., started_at=None)` writes `started_at_us`), `scripts/buildlog/test_step_outputs.py`, `scripts/lint/test_sweep.py` — cut, unknown-start, mismatch and fractional-second tests.

**Binds later work:** keep both guards in `_recorded_mapping_rows()` and the cut tests unchanged.

**Gotchas:** `index.add_step` fills a missing output pair from the captured target folder, also a pair the fixed recorder chose not to write; the index fix belongs to report-unit.

### Phase 67 — Importing offload or sweep loads no build coordination modules  · status: done
#### As-built
Importing `scripts/mac_test/offload.py` or `scripts/lint/sweep.py` loads no `build_hold`, `scripts.build_queue*` or `memory_admit` module.
- `sweep.py` imports `build_hold` only inside `build_hold_targets()`. The import returns the module object in `sys.modules`, so `test_sweep.py`'s patch of `build_hold.live_sessions` holds. The `TYPE_CHECKING` import stays.
- `holder_sessions` adds no path to `sys.path` on each call.
- Offload import median: 218 ms before, 130 ms after.

**Files:**
- `scripts/lint/sweep.py` — the import cut.
- `scripts/lint/test_sweep.py` — a fresh-interpreter import test for `offload` and `sweep`, and a test that repeated holder queries leave `sys.path` unchanged.
- `scripts/build_queue/test_store_identity.py` — imports `sweep`, then `scripts.buildlog.store`, and checks one store module object.

**Binds later work:** keep build coordination imports inside functions in `sweep.py`; the fresh-interpreter import test guards it.

### Phase 68 — Memory estimates use all same-shape history again; the Phase 18 history splits go  · status: done
#### As-built
`expected_peak()` and `filtered_peak()` select history as before Phase 18: no build-change list, side filter, step-commit ancestry or estimate cache exists.
- `config/build_changes.json`, `scripts/lint/build_change_test_support.py`, `HistorySplit`, the side filters, ancestry outcomes and the `admission/build_marks.json` writer are deleted. No estimate function takes `history_split`.
- `compiling_history` returns `(item, evidence)` pairs again. `build_shape.step_index` is public; `expected_peak` opens the index only when the repository has a legacy name. `fresh_test_peak` uses `history_matches_repository`.
- `admission_client.build_history_generation` keeps its two named configuration states.
- The Phase 18 replay (1,064 runs) found no gain from the feature split: 106 steps above their promise without it, 108 with it.
- The estimate median for the hana dev nextest request rose from 0.436 s to 0.536 s, because all same-shape history passes through three history decodes per filtered estimate.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py`, `scripts/build_queue/admission_client.py`, `scripts/buildlog/width_trial.py` — the selection without splits.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py`, `scripts/build_queue/test_admission_client.py` — the pre-Phase-18 tests.
- `docs/as-built/build-memory-admission.md` — one line: the replay found no gain, so no estimate splits its history.

**Gotchas:** natedev keeps an orphan `admission/build_marks.json`; nothing reads it.

**Ruled out:** splitting history by build feature or commit without a replay that shows fewer steps above their promise.

### Phase 69 — A memory estimate reads its history file and its index rows once  · status: done
#### As-built
Each estimate decodes `admission/anon_peaks.jsonl` once and asks the index once, only for the window's call ids. The estimates are the same as before.
- `read_estimate_history(root, host, steps, now, *, include_current_end=False) -> EstimateHistory` applies one parsed window to every tier: host, steps, `cutoff <= ended_at <= now` (`< now` unless `include_current_end`) and a positive int peak. `EstimateHistory` carries `root`, `host`, `now`, `steps`, `include_current_end` and `rows` by step.
- `compiling_history(history)` takes only the history and joins its rows through `index_step_evidence`.
- `expected_peak` reads once: a filtered request with a package reads `filtered_steps(step)` (`step`, `check`, `build` without repeats), others read `(step,)`. It tries `fresh_test_peak` (`ExpectedPeak | NoFreshHistory`), then `filtered_history_peak` or `full_history_peak`. No `filtered_peak` wrapper exists. `build_peaks` makes one read with `include_current_end=True`.
- `index_step_evidence(index_path, host, steps: tuple[str, ...], call_ids: tuple[str, ...], before)` is one query over `json_each` call ids and steps with `+host=?`, so SQLite uses `steps_call`. It skips rows dated at or after `before`, keeps test arguments only for `TEST_STEPS`, and gives a `BuildShape` only to `build` rows (`NoBuildShape()` otherwise).
- The hana dev nextest estimate median fell from 0.462 s to 0.276 s.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py` — the single read and the bounded query.
- `scripts/lint/test_memory_admit.py`, `scripts/lint/test_build_shape.py` — the decode-once helper `assert_history_decoded_once`, window-edge, query-plan and shared-call tests.

**Binds later work:** an estimate reuses the one `EstimateHistory` read and the one evidence join; add no second history read, evidence join or tier window. The indexed fallback queries in `full_history_peak`, `filtered_history_peak` and `fresh_test_peak` are separate index reads by host, repository and step. Only `build_peaks` reads `StepEvidence.shape`.

**Gotchas:** without `+host`, SQLite picks `steps_host_started` and reads every row for the host. `INDEXED BY steps_call` raises when the index lacks it (the frozen replay index does).

**Ruled out:** a byte-offset start of the history read (the file is only close to append order); moving `program_peak_estimate` onto the shared read (it raises on an unreadable file); the fresh zero-compile rule inside the shared join (it changes fresh estimates).

### Phase 70 — Memory and sweep find the enclosing Cargo target with one function  · status: done
#### As-built
One function finds the Cargo target folder that holds a path, and both parent walks use it.
- `sweep.enclosing_cargo_target(path: str, *, lint_settings: conf.ConfigRead | UseConfiguredValue = UseConfiguredValue.CURRENT) -> EnclosingCargoTarget | NoEnclosingCargoTarget` resolves the path and returns the nearest folder, the path itself included, that `is_cargo_target` accepts. `EnclosingCargoTarget.folder` is a `str`, as the other sweep target paths are.
- `record_target_deletes` skips a path whose real path is a known target and records the enclosing target of each other path; a path outside every target adds nothing.
- `memory.build_output_program` passes the resolved program's parent folder, so a target folder itself is not a build program. It reads lint settings only through `is_cargo_target`, so only a cache-tag folder reads `lint.conf`.

**Files:**
- `scripts/lint/sweep.py` — the function, its two result types and `record_target_deletes`.
- `scripts/buildlog/memory.py` — `build_output_program` calls the function.
- `scripts/lint/test_sweep.py` — the cache-tagged target, the path itself, the nearest delete record and the outside delete.
- `scripts/buildlog/test_memory_window.py` — a target folder is not a build program.

**Gotchas:** the function includes the path itself; a caller that classifies a file passes its parent folder.

**Ruled out:** moving the memory cache-tag and symlink tests to `test_sweep.py` (they pin memory's classification).

### Phase 71 — The phone low-disk push carries only the free space, the alert line and one folder  · status: done
#### As-built
The `sweep.disk_floor` push body is `natedev has {free} free, below the {floor} alert line, so builds can fail. {recommendation}`. Its title and action lines did not change. The natedev session message carries the cleanup line, every measured folder and the choices. The phone stays off until the user approves the text (Phase 63).
- `sweep.folder_recommendation(choices: FolderChoices, shortfall: int) -> str` writes the one folder fact both channels use. Trash: `Delete <folder> (recommended): this frees <size>, which covers the <shortfall> shortfall.` Protected, scratch or other: `Review <folder> first (recommended): deleting it frees …`. Insufficient: `The largest folder is <folder> at <size>. Deleting it frees only part of the <shortfall> shortfall.` Unavailable: `Folder sizes are unknown.`
- `sweep.disk_floor_push(free: int, floor: int, choices: FolderChoices) -> RenderedPush` adds the push-only words: the measure and delete-nothing choices for the unavailable state, and `; the natedev session lists every folder.` for the insufficient state. The unavailable action is `In the natedev Claude session, reply measure or delete nothing.`
- `sweep.fit_disk_floor_push(free, floor, choices, *, lint_settings) -> RenderedPush | NoPhoneAlert` renders the measured choices, then the unavailable form, and returns `NO_PHONE_ALERT` with a stderr line when both reach `sweep_push_text_limit`.
- `sweep.folder_choice_lines(choices, shortfall, *, path_limit) -> list[str]` writes the session lines: one line per folder with its size and effect, the folder fact, `Delete nothing: builds can fail when the disk is full.` and `natedev checks your choice before it deletes anything.` The unavailable state has the fact, `Measure first (recommended): …` and `Delete nothing: the disk stays low and builds can fail.` `notify_floor_problem` computes the shortfall once and runs `folder_choices` once per crossing for both channels.
- `folder_choices` returns every measured choice; the `sweep_page_paths` cut is gone. The measured list comes from the folder audit rows at depth 1-2 of at least `MIN_OUTSIDE_DIRECTORY_BYTES`.
- `page_paths` and `classified_folder_choices` are deleted. The out-of-reach floor sends no phone page; holder sessions and natedev get their messages.
- Catalog examples (longest message 285 characters): available 248, unavailable 283, insufficient 285, protected 253. Purpose: `A low-disk alert recommends one folder for natedev review.`

**Files:**
- `scripts/lint/sweep.py` — the folder fact, the push and session renderers, the fit fallback.
- `scripts/notify/push_catalog.py` — the `sweep.disk_floor` template, its four examples and purpose.
- `scripts/lint/test_sweep.py` — exact session lines, every folder listed, the action lines, the protected session label and `test_disk_floor_push_fit_falls_back_at_each_limit`.
- `config/lint.conf`, `config/README.md` — `sweep_page_paths` removed.
- `docs/as-built/build-memory-admission.md` — the out-of-reach paragraph.

**Gotchas:** `test_push_texts.test_every_catalog_example_has_current_approval` stays red until the user approves `sweep.disk_floor`; only the user records that approval.

**Ruled out:** shortening the push folder label before the unavailable fallback (Spec 6 sets the fallback; a label over about 220 characters is rare).

### Phase 72 — One bad run marker keeps only its own run, and the sweep reads rosters only in folders  · status: done
#### As-built
One bad entry in a run marker folder keeps only its own session's runs; every other run is judged as usual.
- `marked_runs(roots) -> RunMarkers` reads each marker on its own. `RunMarkers` holds `paths: dict[str, str]` (real run path to marker name) and `unreadable_sessions: frozenset[str]`. A marker is bad when it is not a regular file, cannot be read, or has no absolute path on its first line.
- `run_folder_guards(roots, live) -> dict[str, RunKeepReason]` reads the markers and the session record in one `try`. A good marker keeps its run with `ACTIVE_MARKER`. A bad marker keeps only the run folders whose `unit_runs.director` is its name, with `UNREADABLE_RECORD`. When the marker folder or the session record cannot be read, every run folder (folders only, never files) gets `UNREADABLE_RECORD`.
- `judge_entries` has no fallback of its own; `owner_reason` reads no markers. `cleanup_end_reason` skips a run-root entry that is not a folder, so a roster is read only inside a folder and a file never gets a raw error text as its keep reason.

**Files:**
- `scripts/lint/tmp_sweep.py` — `RunMarkers`, `marked_runs`, `run_folder_guards`, `owner_reason`, `cleanup_end_reason`.
- `scripts/lint/test_tmp_sweep.py` — empty roster lock, bad marker cases (empty, relative, invalid UTF-8, NUL byte, directory, broken link), broken marker folder, and a good marker beside a bad one.

**Gotchas:**
- A NUL byte in a marker path makes `os.path.realpath` raise `ValueError`; the per-marker handler catches `OSError`, `UnicodeError` and `ValueError`.
- The guard reads the director only for a folder not already kept, so a good marker keeps `ACTIVE_MARKER` when another marker is bad.

**Ruled out:** a test that counts director reads (it needs a configured mock).

### Phase 73 — Measure what drops the warm hana_rigging prelude build, and propose one fix  · status: done
#### As-built
A measurement only; no repository code changed. The report is in the unit run folder (`phases/73/prelude_cold_report.md`).
- The test is `production_prelude_compiles_without_test_support` (hana `crates/hana_rigging/tests/external_client.rs:1931`); lines 1946-1948 put its build in `<target>/external-client`. A warm folder is 1.3 GB.
- 2026-11-02 to 11-05 13:24 EST: the floor sweep removed one `external-client` folder (11-02 01:01, `hana_catalyst-hana-migration`, 1.5 GB). Three CI points in fresh clones ran the test cold on 11-02 (101, 117 and 99 s). One Mac run on 11-05 12:00 took 162 s with no matched cause.
- The fresh-clone CI cause is gone: `ci_start` now uses standing copies (`scripts/production/ci_points.py`).

**Ruled out:** a floor rule that keeps hana `target/external-client` — the builds showrunner dropped it on 2026-11-05: the fresh-clone cause is gone, and 1 eviction in 3 days does not pay for 1.3 GB per checkout. The Mac cold run is a next item.

### Phase 74 — Find what made the 11-05 12:00 Mac prelude compile run cold  · status: done
#### As-built
A measurement only; no repository code changed. The report is in the unit run folder (`phases/74/mac_cold_report.md`).
- The 162 s run (step `Nates-MacBook-Pro-2023-20261105T170021-b2e1fa`) used the Mac mirror `~/.local/state/mac-test/mirror/hana`, made on 2026-10-09; it was not a new mirror.
- Its `target` folder was made again at 11:17:11 EST, after the same-path step that ended at 11:14:00. No record names what removed it.
- The suspect is the post-run cleanup in `scripts/mac_test/offload.py:1640-1647`: it removes the whole target when free space is under the Mac floor (100 GiB). `report_cleanup` (`offload.py:905-912`) prints no line for a removal.
- It is the only cold Mac run of the test from 11-02 to 11-05 13:47 EST (later runs: 33.9 s and 5.9 s).

**Ruled out:** a fix — the cause does not repeat.

### Phase 75 — The Mac post-run cleanup prints one line when it removes a target  · status: done

#### As-built

When free space on the Mac is under the floor, the post-run cleanup measures the mirror's `target` with `du -sk` before `rm`, then sends `mac_cleanup=0 <kib|-> <free GiB>`. The free-space test, the floor and the `rm` are unchanged.

- `parse_cleanup` returns `CleanupDone(size, free_gib)` for that marker. `size` is `MeasuredCleanupSize(kib)` or `CleanupSizeNotMeasured`, and a failed `du` still removes the target.
- A bare `mac_cleanup=0` from `linux_runner.py` is `CleanupRemovedWithoutFacts`: a confirmed removal that prints no line.
- `report_cleanup` prints one line for `CleanupDone`: `Mac build output removed: ~/.local/state/mac-test/mirror/hana/target, 31.2 GiB; Mac free 59 GiB under the 60 GiB floor`. The line starts with the target label.
- `mirror_target(repository, target, slot)` builds both the removed path and the printed path.

**Files:**
- `scripts/mac_test/offload.py` — the cleanup command, the outcome types, `parse_cleanup`, `report_cleanup`, `mirror_target`.
- `scripts/mac_test/test_offload_cleanup.py`, `scripts/mac_test/test_offload_cleanup_results.py`, `scripts/mac_test/test_offload_forward.py`, `scripts/mac_test/offload_test_support.py` — the marker, the printed line, a failed `du`, and the command text.

**Gotchas:** `claimed_run` and `parse_cleanup` serve the Mac and the Linux targets. `scripts/direct/verify_ssh_targets_test_support.py` keeps the bare marker because its fixture also stands for the Linux targets; the Mac marker there hides a break.

**Ruled out:** `linux_runner.py` printing the new marker: the line's text says "Mac free", and that runner needs no line.

### Phase 76 — Each --until folder has its own scratch task  · status: done

#### As-built

Each `--until` or default scratch folder now has the task `until:<stamp>/<id>`, with a new random id for each call. So `scratch.py end --task` removes only the folder that the task names. The cause was the 2026-11-05 19:54 EST incident: `end --task until:2026-11-07T0500Z` removed four clones of other units and one gate checkout.

- `until_task(moment, *, scratch_settings)` adds the id. `random_suffix(scratch_settings)` makes the random text for `until_task` and `target`. `until_stamp(name)` reads the stamp for `task_end` and `target`.
- Folder names do not change. `copies.py` and `end` do not change.
- `end --task until:<stamp>` with no id removes no folder that has an id. `scratch.py list` shows a folder's task.
- The fix reached builds on its own branch, `builds-resources-scratch-task`.

**Files:**
- `scripts/lib/scratch.py`, `docs/as-built/build-memory-admission.md`.
- `scripts/lib/test_scratch.py`, `scripts/copies/test_copies.py` — two real `copies.py clone --until` calls with one value.

**Gotchas:**
- Fast track refuses `scratch.py`: `maker` imports `scripts/production/staff.py`, and the import walk reaches imports inside functions.
- A task with no id still parses, and a hand-written `--task until:<stamp>` still passes `checked_task`. No script or doc writes that form.

**Ruled out:**
- `end` refusing a folder with uncommitted work, or a folder a gate holds: the user answered no on 2026-11-05, because no two folders share a task now.

### Phase 77 — Random text uses the standard library  · status: done

#### As-built

The scratch suffix and the review link id come from `secrets.token_hex`, so two copies of a one-line random text loop are gone and no helper exists.

- `random_suffix(scratch_settings)` returns `secrets.token_hex(length)[:length]`, with `length` from the scratch.conf `suffix_length`. `target` passes `suffix_length=len(suffix)` to `BuildFolderSpec`.
- `review_link.py` makes each snapshot id with `secrets.token_hex(3)`.
- Each suffix and id has 24 random bits, not about 31. Readers and tests of `[a-z0-9]{6}` accept the new form.

**Files:**
- `scripts/lib/scratch.py`, `scripts/review_links/review_link.py`.
- `scripts/review_links/test_review_link.py` — the two collision tests patch `secrets.token_hex`.
- `docs/as-built/review-links.md` — the id form and the `snapshot_allocation_tries` setting.

**Gotchas:**
- The collision tests patch the random call, so a change of that call changes them. The ten-try test counts ten calls, one for each id.

**Ruled out:**
- `token_hex` in `scripts/lib/build_folders.py`: `names.suffix_characters` in config sets its characters.
- `secrets.token_hex` for `os.urandom(n).hex()` and `uuid4().hex[:8]`: each stays one call and no copy goes away.

### Phase 78 — A target that goes away during a floor pass does not fail the pass  · status: done

#### As-built

A target that goes away during a floor pass counts as done. The pass finishes with status 0 and saves its floor record. Any other `OSError` still fails the pass.

- `on_floor_filesystem` returns False when `output_device` raises `FileNotFoundError`: a missing target is on no filesystem. The `sweep_ci_targets` measure uses it.
- `filesystem_floors` keeps its `isdir` check and skips a path that goes away after it.
- Private `stamp_newer(root, moment)` returns False when `USE_STAMP` is missing. The paired trim uses it.
- `removed` leaves out the blocks of an idle target that went away and that the pass did not take, so the alert states only what the pass removed. A block that a surviving target still links stays in.

**Files:**
- `scripts/lint/sweep.py`.
- `scripts/lint/test_sweep.py` — a real private floor pass with a target removed during `shrink_floor`, the removal alert test, the `filesystem_floors` and `stamp_newer` tests, and a fixed alert channel in `test_protected_report_limits_paths_and_measures_each_folder_once_per_pass`.
- `docs/as-built/build-memory-admission.md` — one bullet under the disk floor section.

**Gotchas:**
- The base floor fixture replaces `output_device`, so a test of a missing target restores the real call.
- `before_caches` keeps the bytes of a target that went away, because the growth check compares it with `scanned_free`, and both are from before the take.

**Ruled out:**
- A probe of a secondary filesystem: on natedev it is always the home folder.

### Phase 79 — The folder audit names a maker only on proof, and it skips the Codex daemon folder  · status: done

#### As-built

The folder audit names a session as a folder's maker only when one of that session's own commands makes that exact folder. A folder with no proven maker prints `maker unknown` in its `loose:` line and gets no message. The audit keeps `/tmp/codex-daemon-<uid>`.

- The guard (`scripts/hooks/pre-tool-use-scratch-guard.py`) owns `creation_targets(text, cwd)`. It yields frozen `CreationTarget(path, repository_copy)` records. It skips environment words, follows `cd` (a bare `cd` goes to `~`), stops at a failed `cd`, and yields nothing for `make_a_worktree`. `loose_folders()` and the audit both use it.
- `folder_audit.makers()` selects transcript lines with ripgrep on each folder name, parses each line as JSON, and needs an absolute `cwd`. It reads commands from `input`, `tool_input` and `tool_use` content. A target proves a folder only on exact path equality. The earliest-started transcript wins.
- `KEPT_PREFIXES` adds `codex-daemon-` to the system temp prefixes.

**Files:**
- `scripts/lint/folder_audit.py` — the maker proof and the kept daemon folder.
- `scripts/hooks/pre-tool-use-scratch-guard.py` — the shared creation walk.
- `scripts/lint/test_folder_audit.py` — the maker proof test (child targets and an implicit clone destination), the `maker unknown` test, and the kept daemon folder test.
- `docs/as-built/build-memory-admission.md` — the folder audit paragraph and the Mac socket rule.

**Gotchas:**
- A folder's name in message text (a `send.py --text` report, a cleanup message) is not proof. Each of the 6 real folders with a wrong maker came from such text.
- `mktemp` targets resolve to the template path with its `X`s, so a folder made by `mktemp -d` has no proven maker.
- Never run `folder_audit.py` or `audit()` against the real `/tmp` or `~/rust` in a test: an armed run removes folders.

**Ruled out:**
- Exit-status reasoning (`false && mkdir x`, `(cd x); mkdir y`): the static parser cannot know an exit status, and the earliest-session rule limits the risk.

### Phase 80 — Every Mac test run states its threads and the Mac's load  · status: done

#### As-built

Every nextest run that lint starts prints one line after admission: `nextest: <granted> of <requested> test threads; load <one-minute load> on <cpu count> CPUs (<host>)`. A run narrowed by the share claim adds `; shared with the runs in flight`. A missing load prints `load unknown`. The line covers the share claim, a caller width (`--test-threads`, `--jobs`, `-j`), trial mode, and the fallback paths (no marker folder, a failed claim).

- `scripts/lint/nextest_budget.py` owns the one formatter, `thread_line(granted, requested)`. Widths are `TestWidth = SpecifiedTestWidth | UnknownTestWidth`. `requested_threads(argv)` reads `--test-threads`, `--jobs`, `-j` (all spellings) before `--`, then `NEXTEST_TEST_THREADS`, then the CPU count. The `report` action prints the line for an unshared run; `claim ... --show-load` prints the width, then the line.
- `scripts/lint/invoke.sh`: `run_step_attempt` calls `nextest_thread_report` for a `cargo nextest run` with no share marker. `nextest_share_claim` prints the claim's line, or the report line when the marker or claim fails. `run_nextest` treats `--jobs` and `--jobs=N` as a caller width, so it adds no second width option.
- The cause report for the slow hana Mac runs (three runs that fastest-unit timed, 2026-11-07) proved overlapping load: another session's hana demo (about 220–250% CPU), cargo-liner checks, clippy and 6-thread nextest runs, Chrome and battery power. A full-width grant is not rebalanced when later runs start. Spotlight, Time Machine, thermal limits, swap and a cold cache stay unproven.

**Files:**
- `scripts/lint/nextest_budget.py` — the width types, the request reader and the formatter.
- `scripts/lint/invoke.sh` — the report call on every nextest path.
- `scripts/lint/test_nextest_budget.py` — the line on shared, full, caller-width (`--jobs` rows), trial, marker-failure, claim-failure and unreadable-load runs, through the real dispatch.
- `scripts/lint/test_memory_admit.py` — the line on a shared admission.
- `docs/as-built/nextest-width-and-lint-scope.md` — the line and its paths.

**Gotchas:**
- The test fixture keeps the real `run`, `run_once` and `run_step_attempt`. It replaces only admission and `buildlog_exec`, and it drops the command banner line.
- Each run starts one more short Python process to read the load.

**Ruled out:**
- A timing comparison on a shared Mac: timing runs need a Mac with no interactive demo and the same power source. That is outside this code.

### Phase 81 — A Mac test build never links stale objects from an old build  · status: todo
**Depends on:** none. Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-08 ("a Mac build fault from hana, as your next phase now").

#### Work Order

**Goal:** find why Mac test builds of hana failed to link with undefined ThinLTO-promoted symbols, and fix the cause in the build tooling. No `incremental = false`, and no other workaround.

**Measured (demo via hana, 2026-11-07 23:59 to 2026-11-08 01:18 EST):** three Mac runs failed the same way. Log on natedev: `/tmp/claude/unit/11db7b37-3b59-4a18-a50c-f71ae8c8b561/evidence/mac-link-undefined-llvm-symbols.log`.
1. The command was `verify.sh test hana_catalyst`. verify.sh ran on natedev and sent a copy to the Mac mirror `/Users/natemccoy/.local/state/mac-test/mirror/hana` (`mac_test: running a copy on the Mac while natedev compiles`).
2. The linker printed `Undefined symbols for architecture arm64`. The missing names have `.llvm.<hash>` suffixes: `World::get_resource::<CascadeCombine<hana_diegetic::render::HairlineFade>>`, referred from `hana_diegetic::panel::lifecycle::prepare_cascade_seed::<HairlineFade>` in `libhana_diegetic-46693889639531b7.rlib`, and `_anon.07f7ce924af2bea6e273f39da1024b91.146` to `.150`.
3. `incremental = false` in `[profile.dev]` made the run pass. demo reverted that change.

**Spec:**
1. Find the cause first, with read-only commands only. Write the report to `phases/81/mac_link_report.md` in the run folder. Change no Mac setting and start no Mac test for measurement.
   - Read the log. Find which target folder, profile and toolchain the Mac build used, and what the offload copies or removes in it.
   - Check each candidate and give its evidence: stale incremental objects in the Mac target; a partial target removal by the Mac post-run cleanup (Phase 75) or the floor sweep (Phase 78) while a build used the target; two toolchains or two builds that share one target folder at the same time; a copy of build output from natedev into the Mac target.
   - Give one line for each proven cause with its evidence. Say which candidates you ruled out and how.
2. Fix the proven cause where it lives in the build tooling, so a Mac build never links objects that a different or interrupted build left. If the cause lives in verify.sh or the offload and another unit owns that code, name the owner in the report and post blocked.
3. Add a test that fails without the fix and passes with it.
4. Update the as-built doc that describes the changed tooling.

**Constraints from prior phases:**
- Rounds 1-9 are in `phases/81/mac_link_report.md` in the run folder `/tmp/claude/unit/20523639-3e08-48d6-9277-73e052bca297`. Read it before any new step.
- builds approved one controlled reproduction on 2026-11-08. Its first script no longer exists, because its scratch folder ended. Before the first case, write a new script at `phases/81/reproduce.sh` in the run folder.
- The script takes the normal Mac claim before its first remote command. At every exit it removes its private folder and releases the claim. `ssh mac` exits 0 on a failure, so the script prints one result line for each step.
- The script runs this procedure on the Mac, in the private folder `~/.local/state/mac-test/repro81`:
  1. Use Rust 1.99.0 on arm64 macOS. Keep the measured opt-level, codegen-units, incremental and shared-generics settings.
  2. Use one small library and one integration-test crate. Start from the `HairlineFade`/`CascadeCombine` resource path.
  3. Build in series, with no offload, no cleanup, no source copy during a build and no second compiler.
  4. Sequential case: build one revision, change only the generic body or the caller, and rebuild with the completed incremental history. Its control builds the same changed revision in a new empty target.
  5. Interrupted case: interrupt an intermediate compile, and then finish the build. Its control runs the same intermediate compile with no interrupt.
  6. Reduce the source while the same promoted-symbol failure stays.
- builds decided on 2026-11-08 that the reproduction uses one private target at a time. One case needs about 89.06 GiB (source and one debug target). Before the source copy, the script reads the deployed `sweep_floor_gib` and requires `free - (source_size + debug_size) >= floor`. Record each build's result, then remove that private target before the next case or control starts.
- The 150 GiB Mac floor stays. So a case needs about 239 GiB free before it starts. The Mac had 238.71 GiB free after the 2026-11-08 core cleanup. Start a case only when the room is there and builds approves the start.
- Keep sccache and the hana test mirror. Run no gc command on the Mac.
- New evidence, 2026-11-09 (Phase 92 retrospective): a Mac hana link failed at 11:56 UTC with the same signature, undefined `.llvm.<hash>` symbols in `libhana_conduit`.
  - The Mac build log shows the builds in series under the mirror lease, with no overlap.
  - The log shows no floor take between the last good link (11:53 UTC) and the failure. So the floor sweep and a two-build overlap are ruled out as causes.
  - hana sets `[profile.dev.package.hana_conduit] opt-level = 3` and `[profile.dev.package."*"] opt-level = 3`. The earlier failures (hana_diegetic, hana_catalyst) were also workspace members above opt-level 0. Incremental compilation plus local ThinLTO promotes `.llvm.<hash>` names in such members.
  - Separate the package profile settings in hana's `Cargo.toml` from the effective profile of the generated instance the Mac builds. Read the instance's real profile before you name a cause.
  - builds approved one `cargo clean -p hana_conduit` in the Mac mirror. It finished at 12:08 UTC on 2026-11-09. That approval is used; it permits no other cleanup.
- The reproduction hold and the start approval stay: start no reproduction case until builds approves its start.

**Files:**
- `scripts/mac_test/offload.py` — the Mac copy, target and cleanup, when the cause is there.
- `scripts/mac_test/floor_claims.py` — the floor claims, when the cause is there.
- `scripts/mac_test/test_offload_cleanup.py`, `scripts/mac_test/test_floor_sweep.py`, `scripts/mac_test/test_offload.py` — the regression test.
- `docs/as-built/build-memory-admission.md` — the changed rule.

**Workers:** `1 writer + 1 tester`
- `impl` owns the report, the reproduction script, the source files and the as-built doc, and runs the final suites.
- `test` owns the test files.

**Acceptance gate:**
- The report names each proven cause with its evidence, or states that no cause could be proven and what was ruled out.
- The regression test fails before the fix and passes after it.
- The changed test modules pass. Basedpyright reports `0 errors, 0 warnings, 0 notes` for every changed Python file.

### Phase 82 — The Mac keeps its disk floor without waiting for a test run  · status: done
**Approved:** builds showrunner, 2026-11-08. Checkpoint `ab051b13a`.

#### As-built
- nixos branch `resources-mac-floor` (`efdf8bfc1`, on nixos main `064af4a3c`) adds `modules/darwin/disk-floor.nix`: `nate.jobs.disk-floor` runs `sweep.py --floor-only --host mac` on the Mac every 120 s. It is the same command as the on-demand sweep in `offload.ensure_mac_disk_floor()`, so it takes the same mirror leases, both claim locks and the floor lock, and trims only the shortfall below `sweep_floor_gib` (150 GiB). The 100 GiB on-demand trigger is unchanged.
- The same branch puts `/usr/sbin` on the Mac `jobPath` (`modules/darwin/nate-backend.nix`), after `/usr/bin:/bin`. Before it, launchd jobs found no `lsof`, so tmp-sweep and folder-audit kept every entry.
- `docs/as-built/build-memory-admission.md` describes the Mac floor job.
- The job runs only after the showrunner pushes the branch to nixos main and the Mac runs `darwin-rebuild`.
- No Python change and no new test: the job calls an existing entry point.

### Phase 83 — Memory admission holds a gate's tests back before the memory kill  · status: done
**Approved:** builds showrunner, 2026-11-08. Closed as research with no code (builds, 2026-11-08: "Close Phase 83 as research with no code. Its report stays the evidence.").

#### As-built
- Report: `/tmp/claude/unit/20523639-3e08-48d6-9277-73e052bca297/phases/83/memory_kill_report.md`.
- earlyoom stopped hana `b2f5eb9641`'s gate nextest at 11:19:14, 11:19:28 and 11:19:51 UTC on 2026-11-08, with about 1.2 GiB available and no free swap. hana's nextest stayed inside its claim (8.6 GiB anonymous peak against 12.1 GiB).
- A herdr merge gate that ran at the same time had a 4 GiB claim and a 26.4 GiB anonymous peak. Its first attempts of `test_verify_mac_memory_handoff.py` (27.0 GiB) and `test_verify_mac_local_release.py` (9.4 GiB) ran away; their retries passed at about 250 MiB. The builds slice also held up to 27 GiB of shmem that no reservation counts.
- Routed by builds: the runaway modules to verify; the fixed 4 GiB merge-gate claim (`scripts/build_queue/step_estimator.py:244`) and the shmem question to queue; the merge_gate governor that lets one oversized module run alone (`scripts/production/merge_gate.py:291`) to sdlc.

### Phase 84 — Admission counts tmpfs memory in the builds slice  · status: done
**Approved:** builds showrunner, 2026-11-08 ("Phase 84: admission counts tmpfs memory ... Add the slice's memory.stat shmem to the used memory that admission reads, with a test row that fails without it.").

#### As-built
- Memory admission counts swap-backed memory as used memory. `slice_working_set()` in `scripts/lint/memory_admit.py` reads `memory.stat` `anon + shmem`; `process_memory` reads `/proc/<pid>/status` `RssAnon + RssShmem`. A missing or negative field makes the reading unavailable ("swap-backed memory unavailable").
- `buildlog_sample_anon` in `scripts/lint/invoke.sh` records `anon + shmem` for the peak, the phase peak and the near-peak, so a step's measured need has the same basis.
- A registered CI scope's reading enters the cap once: `unowned_slice_memory` and the service's unowned path subtract each registered scope's whole reading and leave its pids out of the process subtraction (`owned_scope_roots`).
- `AnonymousRunUse` and `ProcessAnonymousUse` (`scripts/build_queue/admission_records.py`) keep their names: `snapshot_codec` uses the class name as the record kind, so a rename breaks saved snapshots and protocol replies. `sum_anonymous_use` is now `sum_swap_backed_use`.
- Rows: 1 GiB anon + 27 GiB shmem reads 28 GiB; CI shmem counted once; a process with RssShmem. `docs/as-built/build-memory-admission.md` states the basis.

### Phase 85 — The tmp sweep judges plain files in the scratch root by age  · status: done
**Depends on:** none. Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-08 (change review C1267 returned: "`tmp_sweep.py --plan <file>` still keeps 535 entries with "Not a directory". Each one is a plain file in /tmp/claude/scratch, such as msg136.txt or wo.json, that the sweep reads as a folder for .scratch.json. Fix it where it lives, and add a test row.").

#### As-built
- `scratch.task_of` (`scripts/lib/scratch.py`) reads a plain file in the scratch root as `NotRegistered`. It tests with `os.path.isfile`, which returns False on a permission error; `Path.is_file` raises `PermissionError` on Python 3.13. A folder whose record cannot be read still reads as `UnreadableRecord` and stays.
- The automatic tmp sweep keeps a recent plain file (`RECENT`) and removes an old one by the existing idle limit. A hand cleanup plan (`tmp_sweep.py --plan`) keeps a plain file with reason `NO_END` ("no recorded end permits hand cleanup"). No row says "Not a directory".
- Rows: `test_scratch` (plain file and folder read `NotRegistered`; a parent without search permission reads `UnreadableRecord`); `test_tmp_sweep` `ScratchRootTests` and `HandCleanupTests --plan`. `docs/as-built/build-memory-admission.md` states the plain-file rule.

### Phase 86 — Mac rebuild works over ssh through a local sshd hop  · status: done
**Depends on:** none. Repository: nixos (worktree `/home/natepiano/worktrees/nixos-mac-ssh-rebuild`, branch `resources-mac-ssh-rebuild`, from nixos main `590edd7`). The showrunner pushes nixos main; this unit never does.

**Approved:** builds showrunner, 2026-11-08, for the user: "i want to be able to rebuild over ssh - i made the configuration changes to settings that it suggested but it still does not work". Design: natedev.

#### As-built
- nixos `0da1eefd4` on branch `resources-mac-ssh-rebuild`; on nixos main as `7e9331f` (showrunner merge).
- `modules/common/rebuild.nix`: on Darwin, when `SSH_CONNECTION` shows a non-loopback server address, `rebuild` runs itself again with `/usr/bin/ssh -t` to `natemccoy@127.0.0.1`, with the same arguments, and returns that exit status. The hop uses the hop key only (`IdentityAgent=none`, `IdentitiesOnly=yes`, no password), `UserKnownHostsFile=/etc/ssh/rebuild_known_hosts` and `StrictHostKeyChecking=yes`. `UNAME` is set on Linux only.
- `modules/darwin/ssh.nix`: the `users` activation makes `~/.ssh/id_ed25519_rebuild_localhost` when missing (no passphrase) and writes `/etc/ssh/rebuild_authorized_keys.d/natemccoy` (root-owned) with `from="127.0.0.1,::1"`. nix-darwin's key options read keys at evaluation, so sshd reads this runtime file. The drop-in `/etc/ssh/sshd_config.d/000-rebuild-localhost.conf` sets `AllowUsers natemccoy@127.0.0.1 natemccoy@::1`, no password or keyboard-interactive login, and that `AuthorizedKeysFile`. `postActivation` writes the trust file from `/etc/ssh/ssh_host_*_key.pub`, after nix-darwin's openssh stage makes host keys. With no host key, activation prints one line: run `rebuild` once in the Mac's own Terminal after Remote Login is on.
- `modules/darwin/tailscale.nix`, `modules/linux/ssh.nix`: Remote Login is on for the loopback hop only.
- User steps: Remote Login on with "Allow full disk access for remote users"; first `rebuild` in the Mac's Terminal; then `ssh -t mac rebuild`. The live hop result comes from natedev through the showrunner.

### Phase 87 — The Mac floor alert reaches the user's phone  · status: done
**Depends on:** none. Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-08: "Did the Mac floor job send that push? If not, find why and fix it in Phase 85 or a new phase. If it did, tell me its time. Find what besides the 110 GiB hana target holds the other Mac space, from a read-only size list, and send me the top five folders."

**Approved:** builds showrunner, 2026-11-08: "Phase 87 go, after the Phase 85 checkpoint: the Mac floor uses the same crossing path, with a natedev message."

**User answer (via builds, 2026-11-08), to "approve the Mac low-disk phone push text?":** "1." builds: "Change the existing sweep.disk_floor text so that the machine name is a value ({machine}), and use it for natedev and the Mac. Add no sweep.disk_floor_mac. Title: "{machine} disk is low; {machine} tests can fail". The body and action are your proposed words, with {machine} in place of "Mac". The natedev push keeps its meaning. Your Phase 87 sends the phone push for the Mac. Put the changed catalog entry in the checkpoint notice."

#### As-built
- The Mac floor pass (`sweep.py --floor-only --host mac`, launchd `org.nixos.disk-floor`) and natedev use one phone-floor crossing path. A non-dry pass under `MacFloor` calls `notify_floor_problem` with `FloorMachine.MAC`; before, only `NatedevFloor` reached it. Each machine keeps its crossing state and receipts in its own `floor.json`.
- `FloorMachine` (`NATEDEV = "natedev"`, `MAC = "Mac"`) fills `{machine}` in the one `sweep.disk_floor` text: title "{machine} disk is low; {machine} tests can fail". `disk_floor_push` and `fit_disk_floor_push` take `machine`. The natedev session message adds "This is the Mac disk." for the Mac.
- `send_floor_alert()` sends the session message (exit 0 or 1 accepted); `send_phone_push()` sends the phone push (only exit 0 accepted). Each channel keeps its own receipt. A reading above the alert line rearms both; equality does not.
- natedev cache size, growth and removal reports stay outside the Mac path.
- Rows: `test_sweep.py` Mac crossing row. `push_texts_reviewed.json` holds the user's approval of the changed text. `docs/as-built/build-memory-admission.md` states the shared path.

**Gotchas:** a changed push text needs a new approval record before a phone push sends. While approval is stale, the natedev message still sends and the phone receipt stays pending.

### Phase 88 — The hana CI margin key goes after the runner activation  · status: todo

**Depends on:** queue's 10d, natedev's confirmation that the runners are active behind the cargo front, and the user's approval of a config key deletion. Source: builds-queue Phase 22, moved to resources by builds on 2026-11-08 (queue had too much work).

#### Work Order

**Goal:** A registered CI job is charged only through its reservation. The fixed CI margin is gone.

**Spec:**

1. **The wait.** natedev confirms the runner activation. resources then files the key deletion with /unit:question, because a config key deletion needs the user's approval. Do not start before both answers are on the plan.
2. **The deletion.** `ci_memory_margin_gib` leaves `config/build_admission.conf` and `AdmissionPolicy` (`ci_memory_margin_bytes`, `admission_records.py:68`). `ci_keep_aside` (`scripts/lint/memory_admit.py`) and the mixed rule go with it.
   The readers of the room's `ci_keep_aside` lose their CI keep-aside text: `scripts/build_queue/queue_evidence.py` (lines 118 and 223-224) and `scripts/build_queue/memory_feed.py` (lines 499 and 764).
   The key also leaves the test fixture `scripts/lint/fixtures/build_admission.conf` (line 17).
3. **The limit.** A CI job that the front cannot cover has no keep-aside. `MemoryMax=34G` is its limit. The notice says so.
4. **Proof.** Tests: registered jobs still have no keep-aside; an unregistered job counts once in the cap and now takes no margin, so the room rises only in that case. Remove or change the tests of the mixed rule only with an approved assertion change. Three modules hold such tests:
   - `scripts/lint/test_memory_admit.py`.
   - `scripts/build_queue/test_queue_evidence.py`: `test_release_advice_retains_the_running_ci_keep_aside` (line 481) expects the running CI margin to keep the head held and the room to fall from 15 GiB to 8 GiB.
   - `scripts/build_queue/test_queue_view.py`: `test_running_ci_names_its_keep_aside_in_the_view_and_card` (line 608) expects `CI keep-aside 7.0 GiB` in the view and the card.

**Files:**
- `config/build_admission.conf`
- `scripts/build_queue/admission_records.py`
- `scripts/lint/memory_admit.py`
- `scripts/lint/test_memory_admit.py`
- `scripts/build_queue/test_queue_evidence.py`
- `scripts/build_queue/queue_evidence.py`
- `scripts/build_queue/memory_feed.py`
- `scripts/build_queue/test_queue_view.py`
- `scripts/lint/fixtures/build_admission.conf`
- `docs/as-built/build-memory-admission.md`

**Workers:** `1 writer + 1 tester`
- `impl` owns `config/build_admission.conf`, `admission_records.py`, `memory_admit.py`, `queue_evidence.py`, `memory_feed.py` and the as-built doc.
- `test` owns `test_memory_admit.py`, `test_queue_evidence.py`, `test_queue_view.py` and `scripts/lint/fixtures/build_admission.conf`.

**Gate:** the targeted test; `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_queue_evidence.py` and `scripts/build_queue/test_queue_view.py` pass; and `basedpyright` 0/0/0 on each changed file and each caller module.

**Change-Review:** The queue view, the queue card and the build memory page show no CI keep-aside while a hana CI job runs | Open the queue view while a hana CI job runs | No `CI keep-aside` line shows, and the room counts the CI job once.

Registered jobs already take no margin: `test_registered_and_unregistered_jobs_each_enter_the_cap_once` (`test_memory_admit.py:6010`).

### Phase 89 — The Mac folder audit reads process use and acts  · status: done

#### As-built

- The Mac folder audit (`org.nixos.folder-audit`) kept every due folder as `maker unknown: no /proc or lsof to show which processes use it` because the old launchd job PATH had no `/usr/sbin`. The job could not start `lsof`, so `folder_audit.process_uses()` returned `ProcessUsesUnavailable`.
- The Python fallback is correct: `process_uses()` (`scripts/lint/folder_audit.py:325`) calls `tmp_sweep.lsof_paths()` when `/proc` is missing, and `test_lsof_fallback_keeps_used_folders_and_judges_unused_folders` covers it. No Python or test changed.
- Nixos `efdf8bf` adds `/usr/sbin` after `/usr/bin:/bin` to `jobPath` (`modules/darwin/nate-backend.nix:40`). The loaded job has that PATH. Its plist change time is 2026-11-08 11:04:48 EST. The job has `RunAtLoad=false` and an 86400-second interval, and had zero runs at inspection.
- The as-built records the cause, the loaded PATH and the closed Phase 44 gap: the old standing `~/rust/hana_catalyst_mac` clone had no lifetime after the runner moved to scratch clones; its 85.8 GiB target was removed on 2026-11-08 with builds' approval. Folder creators register lifetimes; `~/.cache` stays outside every cleanup root.
- The first audit run after the PATH change goes to builds as a note (removed count, NO_PROC rows). A run that still removes nothing becomes a new phase.

**Files:**
- `docs/as-built/build-memory-admission.md` — the Mac audit PATH cause, the loaded job PATH and the Phase 44 gap.

**Gotchas:**
- An ssh shell finds `/usr/sbin/lsof`; a launchd job uses its own explicit PATH. Test a command lookup in the job environment, not over ssh.
- The plist file time is not the launchd load time; `launchctl print` shows no load time.

**Ruled out:** a Python change or a new test row for the Mac case — the existing fallback test covers the `lsof` path.

### Phase 90 — Find why the Mac drops off the network under test load, and propose the fix  · status: done

#### As-built

- The Mac drops of 11-08 13:35 EST and 11-09 02:23 EST are clamshell sleep: the MacBook lid was closed with no external display, so macOS slept. It did not restart (uptime 21 days). Tailscale and ssh stop because the Mac sleeps.
- `pmset -g log` shows `Entering Sleep state due to 'Clamshell Sleep'` at both times. On 11-08 the charger came off at the same moment (`Using Batt`); the Mac woke only at 22:01:45 on `USB-C_plug` and `DarkWake to FullWake`.
- Short dark wakes (2 to 45 s) explain the brief returns that natedev saw. A 14-minute dark wake (02:48 to 03:02 on 11-09) ran tests with the lid closed and ended in `Dark Wake Thermal Emergency`.
- At 03:19 on 11-09: `AppleClamshellState = Yes`, only the internal display, AC power, `pmset` `sleep 0`. `sleep 0` does not stop a clamshell sleep.
- builds accepted the cause and asked the user to open the lid. No config or concurrency change. No lid-state check (builds: no new check without the user's ask).

**Files:** none changed. Records are in the run folder under `phases/90/`.

**Gotchas:**
- The Mac is a MacBook. A closed lid with no external display sleeps it whatever `pmset sleep` says.
- `pmset disablesleep 1` with a closed lid is not a fix: the 11-09 thermal emergency shows a closed Mac overheats under test load.
- Tailscale control can list the Mac as online for minutes after it sleeps; an ssh timeout with `online=yes` is the sign.

**Ruled out:** a kernel panic and restart (uptime 21 days); memory pressure as the cause (the pmset log names clamshell sleep for each drop); a nextest concurrency change.

### Phase 91 — The floor alert sends once for each crossing of the line  · status: done
**Depends on:** none. Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-09 ("top priority, a Mac disk defect from natedev. Fix it at the source as a phase."). Order change, builds, 2026-11-09: "Fix the repeat first and send that checkpoint alone: one alert for each crossing of the line, with a margin before it alerts again. Put the margin in config as a new key."

#### As-built
- `notify_floor_problem` (`scripts/lint/sweep.py`) rearms the phone-floor crossing (`AbovePhoneFloor`) only when free space is above the alert line plus `sweep_phone_rearm_gib`; a reading at that line or inside the margin does not rearm. The rule is the same for natedev and the Mac. Readings 200,140,130,151,140 send one alert; 200,140,160,140 send two.
- Crossing deliveries (natedev message and phone push) go only while free space is below the alert line. A pending receipt above the line stays pending and sends at the next reading below it.
- `save_delivery` accepts the natedev crossing receipt only when the delivered message held crossing text; a failure-only message leaves the receipt pending.
- `config/lint.conf` holds `sweep_phone_rearm_gib=5` (range `0..`). `docs/as-built/build-memory-admission.md` states the rearm rule.
- Rows in `scripts/lint/test_sweep.py`: `test_crossing_sends_both_channels_once_and_rearms_only_above_the_line`, `test_phone_floor_crossing_sends_once_until_free_space_rises`, `test_crossing_retries_only_the_channel_without_a_receipt`, `test_failure_after_an_interrupted_crossing_receipt_uses_no_crossing_key`.

**Gotchas:** a failure-only notification must not consume the crossing receipt, or the next real crossing sends nothing.

### Phase 92 — The Mac floor sweep trims the mirror targets, and its alert names folder sizes  · status: done
**Depends on:** Phase 91 (same file, `scripts/lint/sweep.py`). Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-09 (the Mac disk defect, items 1 and 2; order change: after the repeat fix).

#### As-built
- The floor sweep trims the Mac mirror targets by design: it removes only the shortfall, least recently used first, and never a target a live build holds. On 2026-11-09 both mirror targets held live nextest runs at each scan; later trims took them.
- `ClaimSnapshot.holds` (`scripts/mac_test/floor_claims.py`) skips missing claimed mirror paths. A missing unrelated mirror does not protect a marked target without a copy record. Other claim read errors still protect the target. Remote mirror removal keeps its own lease exemption and checks sibling leases.
- While a crossing delivery is pending, the Mac measures each direct folder child of its floor roots (`mac_folder_measurement`, `CurrentMacFolders` in `scripts/lint/sweep.py`). `mac_folder_reclaimable_bytes` counts the blocks that deleting that folder alone frees: an inode counts once only when all its hard links are inside the folder; symlinks are not followed. The alert lists the folders by size and recommends the largest; protected folders keep their labels. No natedev disk snapshot is needed.
- `notify_floor_problem` takes the machine name from its floor policy; it has no `machine` parameter.
- `docs/as-built/build-memory-admission.md` states the claim and folder-measurement rules.

**Files:**
- `scripts/lint/sweep.py` — the Mac folder measurement and the folder choices.
- `scripts/mac_test/floor_claims.py` — the missing-mirror rule in `ClaimSnapshot.holds`.
- `scripts/lint/test_sweep.py`, `scripts/mac_test/test_floor_sweep.py` — rows in `test_crossing_recommends_one_measured_choice_that_frees_the_shortfall` (Mac rows) and `test_no_proc_mac_sweep_takes_only_the_oldest_shortfall` (mirror row).
- `docs/as-built/build-memory-admission.md` — the changed rules.

**Gotchas:** a recommended Mac folder can contain a held target; the folder measurement does not yet know the held paths.

**Ruled out:** a trim defect in the floor sweep for the mirror targets (the targets were busy at each scan); raw block counts for folder sizes (hard links outside the folder free nothing).

### Phase 93 — The floor alert never recommends a folder that holds output a build is using  · status: done
**Depends on:** Phase 92 (same file, `scripts/lint/sweep.py`). Merge `origin/builds` first.

**Approved:** builds showrunner, 2026-11-09 ("Your open nit is a wrong result: the alert can recommend a folder that holds output a build holds. Fix it now as phase 93. The recommendation must leave out held output, or name a different folder.").

#### As-built
- A non-dry floor pass computes the held paths when it notifies, in the `finally` of `_hold_floor_pass` (`scripts/lint/sweep.py`), from the final `busy` map. A held path is a target with a `busy` reason other than `NoRecordedEvidence`, a no-evidence target with an entry in `uses` or `hold_targets`, a `.claude-busy` judgment lock or a held cargo lock, and each paired root (`OUTPUT_ROOTS.get().roots_for`) of these. A live use that `shrink_floor` finds after the first scan is held.
- `notify_floor_problem` receives them as `FloorOutcome.held_targets`. No caller reads held paths from the outcome that `_hold_floor` returns.
- `folder_choices(..., held_targets=...)` marks a choice that is, contains or is inside a held path as `FolderChoiceKind.HELD_OUTPUT`. It compares resolved paths (`Path(label).expanduser().resolve()`, `is_relative_to` both ways). Held wins over `PROTECTED`. This applies to the Mac measurement and the natedev disk measurement.
- A held folder line reads `<label>: holds <size>. A build is using output inside it; deleting it can break that build.`
- Classification starts at the largest non-held choice: `AvailableFolderChoices` when it covers the shortfall, else `InsufficientFolderChoices`. When every choice is held, `HeldOnlyFolderChoices` gives the recommendation `Every measured folder holds output a build is using. The sweep frees it when the build ends.` and the phone action `In the natedev Claude session, reply delete nothing.`
- `docs/as-built/build-memory-admission.md` states the held-output rule and the late-use rule.

**Files:**
- `scripts/lint/sweep.py` — `FolderChoiceKind.HELD_OUTPUT`, `HeldOnlyFolderChoices`, `FloorOutcome.held_targets`, held paths in `_hold_floor_pass`.
- `scripts/lint/test_sweep.py` — held-next (Mac and natedev), held-insufficient and held-only rows in `test_crossing_recommends_one_measured_choice_that_frees_the_shortfall`; `test_protected_folder_is_not_a_floor_holder_in_a_low_space_pass` and `test_floor_rechecks_busy_mark_after_its_scan` read the `FloorOutcome` that `notify_floor_problem` receives.

**Gotchas:** held paths taken at the first scan miss a live use that `shrink_floor` finds later; compute them from the final `busy` map. The no-evidence branch walks `_build_trees` only when a cargo lock is held.

### Phase 94 — Each fall below the floor line gives one phone push, and the return gives one more  · status: done

#### As-built

- Each fall of free space below `sweep_phone_floor_gib` (150 GiB) gives one fall push for that machine, and the return gives one `sweep.disk_floor_recovered` push. No push goes between them. The rule is the same for natedev and the Mac.
- `notify_floor_problem` rearms a crossing only when every pass during `sweep_phone_rearm_minutes` (60) reads strictly above the line plus `sweep_phone_rearm_gib` (155 GiB). `AboveRearmLine.since` keeps the first above-line time under the record lock. A reading at or below 155 GiB restarts the interval.
- `RecoveringPhoneCrossing` holds the return receipt, the retirement result and `ReturnPushPolicy`. The fall key is `phone:<identity>` and the return key is `phone-recovered:<identity>`. A failed return delivery retries above the rearm line and a repeat is stopped by the receipt. A later fall opens a new episode even when the prior return receipt is pending.
- At recovery, `retire_floor_fall` retires the crossing's fall key on natedev, which owns the silence queue. The Mac reaches natedev through `silence.SSH`. A dropped kept push means no return push and the crossing closes, even when the retire exits with an error. A retire with no count suppresses the return push in saved state, and the next pass retries. Only an accepted fall and a first retire result of zero dropped permit a return push.
- The alert folder list shows only the largest folders that cover the shortfall, at most `sweep_alert_folder_count` (3, range 3..5). It never shows 0-byte folders and shows each path in full (`~` allowed). At the text limit it shows fewer folders. `notify_floor_problem` falls back to `UNAVAILABLE_FOLDER_CHOICES`.
- Both Mac floor paths, the launchd job and the ssh call, read and write the same `floor.json` under one `floor-alert.lock`.

**Files:**
- `scripts/lint/sweep.py` — crossing states, timed rearm, return push, fall-key retirement, folder list.
- `scripts/lint/test_sweep.py` — return, retirement, lost-receipt, timed-rearm and folder-list rows.
- `scripts/notify/push_catalog.py` — `sweep.disk_floor_recovered`.
- `scripts/notify/push_texts_reviewed.json` — approval records for the natedev and Mac return examples.
- `config/lint.conf` — `sweep_phone_rearm_minutes`, `sweep_alert_folder_count`.
- `docs/as-built/build-memory-admission.md` — the two-push rule and the folder list.

**Gotchas:**
- The approval record for the return text carries the reviewer's objection to a push that asks for no action. The user approved the words on 2026-11-09.
- A pending fall phone receipt gets no return push.

**Ruled out:**
- A deploy repair for the crossing record: the older and current formats read the same crossing, and the 147.7 GiB alert followed a real rise to 155.23 GiB.

### Phase 95 — Each test folder goes when its test ends  · status: done

#### As-built

`RecordedOutputRootsTests` and `IndexStepEvidenceTests` have no `__init__`. Each `setUp()` makes its folder with `self.enterContext(tempfile.TemporaryDirectory())` and sets the attributes that depend on that folder. A test that loads and does not run makes no folder, and each folder goes when its test ends. `before` and `cutoff` in `IndexStepEvidenceTests` are class constants.

**Files:**
- `scripts/buildlog/test_step_outputs.py` — `RecordedOutputRootsTests` fixture.
- `scripts/lint/test_build_shape.py` — `IndexStepEvidenceTests` fixture.

**Ruled out:** a check, a scan or a signal handler for leftover folders.

### Phase 96 — The hand cleanup command runs when typed by its path  · status: done

#### As-built

`scripts/lint/tmp_sweep.py` has git mode 100755, so `~/.claude/scripts/lint/tmp_sweep.py --plan <new-plan.json>` runs when typed by its path.

**Files:**
- `scripts/lint/tmp_sweep.py` — mode only.

**Gotchas:** the small-fix path refuses this file because it imports `scripts/production/staff.py`.
