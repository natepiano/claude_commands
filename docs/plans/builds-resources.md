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

### Phase 2 — The merge gate sets the tripwire for every test module, and every caller's tests fake both readings  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** every test module in the merge gate runs with the tripwire set; a module that reads live memory fails the gate with the reading named, and no module in the suite does.

**Spec:**

1. **The gate sets the tripwire.** `test_module_environment` adds `BUILDLOG_LIVE_MEMORY_TRIPWIRE=<root>/live-memory-reads.jsonl` (`root` is the module's temporary folder). The `types` suite does not get it.
2. **The gate fails a tripped module.** `finish_gate_job` reads `live-memory-reads.jsonl` like `build-tool-calls.jsonl`: for each line it appends `<module>: a test read live memory <reading>: <shlex-joined argv>` to the output, and any line sets status 1. Each line is a JSON object `{"reading": <path>, "argv": [...], "pid": <pid>}`, not the argv list that `build-tool-calls.jsonl` holds. Rename `GateJobResult.build_tool_started` to `tripwire_tripped` (true for a build-tool call or a live memory reading); :859 keeps a tripped module out of the alone rerun, as it does now for build tools. Update every reader of the old field name.
3. **Find the set.** With items 1–2 in place, run the full merge gate with the worktree's own runner, since `~/.claude` main's copy has neither item: `env HERDR_BIN=/nonexistent/herdr python3 <worktree>/scripts/production/merge_gate.py --checkout <worktree> --tests-only --full` (`--checkout` selects the test tree only; the runner code is the file that runs). Every module that fails with `a test read live memory` goes on the list in the phase report, with its owner (Delegation Context survey).
4. **Fix each module on the list.** Each module takes its memory variables from `fake_memory_readings` (Phase 1). Where a module already writes its own meminfo for a test value (for example `test_record.py` :249 compares `MemAvailable: 12582912 kB`), it keeps that value and writes it to the helper's file, or keeps its own file and adds the helper's `BUILDLOG_MEMORY_PRESSURE` entry. Assertions stay as they are. A module whose fix needs more than adding the helper goes to the unit director, who routes it to its owner with the gate output; it does not block the rest.
5. **The pressure-only leaks the survey names** get the helper's `BUILDLOG_MEMORY_PRESSURE` entry even when the gate run in item 3 does not trip them: `scripts/buildlog/test_record.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/direct/test_verify_doc.py`, `test_verify_release_ack.py`, `scripts/production/test_dailies_render_holds.py`. The tripwire trips on the read itself, not on a wait, so item 3 lists them on any machine; this item is the check that it did. Where a module takes its environment from a shared fixture module (`verify_*_test_support.py`, or another test module it imports), the fix goes in that fixture module, not in each consumer. The modules the survey lists under fake both need no change.
6. **A test of the gate** in `scripts/production/test_merge_gate_suites.py`, beside `test_module_tool_calls_fail_even_when_its_tests_pass` :132: a module folder whose `live-memory-reads.jsonl` has one line gives status 1, the `a test read live memory` line and `tripwire_tripped` true; an empty or absent file leaves status as the module returned it. And an end-to-end case through `run_gate`, like the build-tool test: a fixture module whose tests pass but which writes one record line to the `BUILDLOG_LIVE_MEMORY_TRIPWIRE` path it receives makes the gate exit 1, prints `<module>: a test read live memory <reading>: <args>`, runs the module once (no `Rerun alone:`), and gives two modules distinct record paths; the `types` job gets no `BUILDLOG_LIVE_MEMORY_TRIPWIRE`. The build-tool tests stay as they are.
7. **Docs.** `docs/as-built/merge-gate-and-test-speed.md`: the tripwire line names the live memory tripwire next to the build-tool tripwires. `docs/as-built/build-memory-admission.md`: the tests invariant says that the merge gate sets the tripwire for every module, and names its two limits: `test_memory_admit.py` `test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty` reads live pressure with the tripwire removed, by design; and a reading whose record write fails still refuses but leaves no record, so the gate sees only recorded readings.

**Files:**
- `scripts/production/merge_gate.py` — items 1–2 (owner: natedev showrunner).
- `scripts/production/test_merge_gate_suites.py` — item 6 (owner: natedev showrunner).
- `scripts/direct/` — verify-unit test modules item 3 lists, and the `test_verify_*.py` modules of item 5 (owner verify-unit).
- `scripts/buildlog/test_mac_idle.py` — item 5 (owner verify-unit).
- `scripts/buildlog/test_record.py` — item 5 (owner report-unit).
- `scripts/build_queue/` — any module item 3 lists (owner report-unit).
- `scripts/production/test_dailies_render_holds.py` — item 5 (owner natedev showrunner).
- `scripts/hooks/` — any module item 3 lists (this unit, or the owner the survey names).
- `scripts/build_hold/` — any module item 3 lists (this unit).
- `scripts/build_targets/` — any module item 3 lists (this unit).
- `scripts/lint/` — any module item 3 lists (this unit, or verify-unit for `test_invoke_scope.py`, `test_nextest_*.py`).
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md` — item 7.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–5 and 7: the gate change, the full gate run, and each module's environment change. The writer reports the item 3 list with each module's owner and the failing test names.
- `test` — item 6, written from this Spec alone.

**Showrunner approval (2026-10-29):** edits to other owners' test files only add the helper to each test; nothing else in those files changes. verify-unit is a natedev unit until natedev transfers it: trial-merge its files against `origin/natedev-verify` and `origin/natedev`, and name each owner in the notice's also-touches line.

**Constraints from prior phases:**
- Phase 1's `fake_memory_readings` signature (`scripts/lint/memory_test_support.py`). An inherited `BUILDLOG_LIVE_MEMORY_TRIPWIRE` stays in use, an inherited empty value stays empty, and the default `root/"live-memory-reads.jsonl"` applies only when the variable is absent. So under item 1 a module's helper calls write to the gate's record file.
- The refusal text: `live memory reading <live path> in a test; fake BUILDLOG_MEMINFO and BUILDLOG_MEMORY_PRESSURE (scripts/lint/memory_test_support.py)`. `memory_admit.py` `check_memory_reading` writes the record and raises `LiveMemoryReading`; a record write that fails still raises but leaves no line. The gate's record folder exists for the whole module run, so only a test that removes it can lose a record; the gate detects recorded readings only.
- A shell-side read (`memory_gate.sh`) records through `memory_admit.py tripwire <path>`, so its `argv` names the checker, not the test. The gate's output line names the module from the job, not from `argv`.
- Tests that trip on purpose use a private record path and never the inherited one: `test_memory_admit.py` :1128 and :1215, `test_brp_launch_gate.py` :136. Keep that rule in every fix, or the gate fails the module.
- `test_memory_admit.py` `test_default_pressure_still_returns_pressure_when_tripwire_is_unset_or_empty` :1228 reads live `/proc/pressure/memory` with the tripwire removed, by design (it proves live behavior is unchanged). It writes no record; do not change it.
- The worktree carries natedev 367da4f1 and c495a0b2 (director merge 73a8d903 of `origin/builds` ff44142b). Anchors in this Work Order are that tree's `merge_gate.py` and split tests.

**Acceptance gate:**
- A `--full` merge gate run with the worktree's runner (item 3's command) green with no `a test read live memory` line; the summary gives the item 3 list before the fixes and its count after (0).
- `basedpyright` over the Lint directories and the directory of every changed module reports `0 errors, 0 warnings, 0 notes`.
- The checkpoint notice has one `also touches` line per owner (verify-unit, report-unit, natedev showrunner), each with `git merge-tree --write-tree HEAD origin/<owner branch>` clean, and a trial merge against `origin/natedev`.
- Module times for `test_brp_launch_gate.py`, `test_record.py` and `test_verify_token_wait.py` from this gate run next to their medians from `/build:report`.
- Change-Review line: the merge gate fails a test module that reads live machine memory | in a scratch copy of `~/.claude` (`git clone --shared ~/.claude <scratch>`), remove the `BUILDLOG_MEMORY_PRESSURE` entry from `test_brp_launch_gate.py` `setUp`, then run `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <scratch> --tests-only` | the module fails with `a test read live memory /proc/pressure/memory: …`.

### Phase 3 — The floor sweep keeps build output that is in use  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the disk-floor and budget sweeps stop deleting build output a temp clone is still using, and a command names the cause of each cold rebuild. Approved by the showrunner 2026-10-29 (W1); measurement 2026-10-29: floor takes followed by a cold rebuild cost about 12.8 compile-hours a day, mostly gate and merge copies (4.9) and phase clones (2.7).

**Spec:**

1. **One order for every idle target.** `hold_floor` in `scripts/lint/sweep.py` (sort key :1659-1665) no longer takes any idle `/tmp` target first. Every idle target is ordered by last use (:1102, the newer of the `.lint-sweep-used` stamp and the fingerprint atimes), oldest first.
2. **Recent output is the last choice.** A target whose last use is newer than `recent_minutes` (new key in the `[sweep]` section of `config/lint.conf`, default 60) is taken only when the older targets cannot meet the floor. The per-target budget sweep follows the same rule. The floor itself does not change.
3. **`sweep.py rebuilds --days N`.** For each build step with `crates_compiled >= 50` in the build log, it finds the same folder's previous step and names the cause of the cold rebuild: `floor take`, `budget take`, `tmp sweep`, `none`. It groups the result by clone kind (gate and merge copies, phase clones, mktemp folders, scratchpad clones, scratch.py folders, other /tmp, ~/rust worktrees), and prints compile-hours per cause. It reads the journal lines `the floor took` and `lint sweep: removed` and the build log. It reads only; it writes nothing.
4. **Tests** in `scripts/lint/test_sweep.py`:
   - an idle `/tmp` target used 10 minutes ago is kept while an older `~/rust` target meets the floor;
   - a recent target is taken when nothing older can meet the floor;
   - the budget sweep obeys the same order;
   - `rebuilds` names each cause on a fixture journal and build log.
5. **Docs.** The sweep as-built (`docs/as-built/build-targets.md`, or the doc that describes `hold_floor` now) states the order, `recent_minutes`, and the `rebuilds` command.

**Files:**
- `scripts/lint/sweep.py` — items 1–3.
- `scripts/lint/test_sweep.py` — item 4.
- `config/lint.conf` — the `[sweep]` section only (this unit's section; file owner verify-unit).
- `docs/as-built/build-targets.md` — item 5.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–3 and 5.
- `test` — item 4, written from this Spec.

**Parallel:** runs beside Phase 2 (showrunner, 2026-10-29). If Phase 2's full gate lists `test_sweep.py`, its helper line is made here, not in Phase 2.

**Acceptance gate:**
- `test_sweep.py` passes; `basedpyright scripts/lint/sweep.py scripts/lint/test_sweep.py` reports `0 errors, 0 warnings, 0 notes`.
- `sweep.py rebuilds --days 7` on the live journal and build log reproduces the measured split within 10% (floor-take rebuilds about 12.8 cold-h a day; gate copies 4.9, phase clones 2.7).
- A replay of the last 7 days of floor takes with the new order: takes from targets used in the last 60 minutes fall to 0 on the days the older targets met the floor.
- Live, after merge: `sweep.py rebuilds --days 1`, 24 h after the merge, shows `/tmp` floor-take rebuilds under 3 cold-h a day. The unit reports it in a later notice; it does not hold the checkpoint.
- Change-Review line: the disk-floor sweep keeps build output used in the last hour, and a command names why each clone rebuilt cold | in a terminal on natedev, `cd ~/.claude && python3 scripts/lint/sweep.py rebuilds --days 1` | a table of compile-hours per clone kind and cause (floor take, budget take, tmp sweep, none).

### Phase 4 — Short-lived clones share one build folder per repository  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** a new temp clone of a Rust repository reuses the build output of earlier clones of the same repository, if a measured trial shows that this saves work without making builds wait. Approved by the showrunner 2026-10-29 (W2). Measurement: mktemp folders (91.2 compile-h in 7 days, 4.6 steps per clone, 61.6 cold-h on first builds) and style-sync folders (10.9 h) rebuild everything on their first build.

**Spec:**

1. **Trial first.** On hana, make two clones at one commit with `scratch.py new`. Build the first, then the second with a shared `CARGO_BUILD_BUILD_DIR` per repository. Record crates compiled and wall time for each. Then run two builds at once on the shared build folder and record the lock wait. Use `verify.sh build` for every build.
2. **Adopt rule.** Adopt only when the second build compiles under half the crates of the first, and the parallel lock wait is under 10% of build time. Otherwise the phase records the numbers in the plan, changes no code, and stops.
3. **If adopted:**
   - `scripts/lib/scratch.py` `new()` (:182) sets the shared build folder for the repository it clones.
   - `scripts/hooks/pre-tool-use-scratch-guard.py` steers a hand clone of a Rust repository into `/tmp` or a scratchpad to `scratch.py new`.
   - `scripts/style/style_sync.py` (:356, mkdtemp) makes its clone with `scratch.py new`.
   - The disk sweeps treat the shared build folder as one target with its own last use.
4. **Tests** for each changed module, in `scripts/lib/test_scratch.py`, `scripts/hooks/test_scratch_guard.py` and the style_sync test module.
5. **Docs.** The scratch as-built names the shared build folder and the guard's steer.

**Files:**
- `scripts/lib/scratch.py` — item 3.
- `scripts/lib/test_scratch.py` — item 4.
- `scripts/hooks/pre-tool-use-scratch-guard.py` — item 3.
- `scripts/hooks/test_scratch_guard.py` — item 4.
- `scripts/style/style_sync.py` — item 3 (owner natedev style-unit; trial-merge against `origin/natedev`).
- `scripts/lint/sweep.py` — item 3, the shared folder as one target.

**Workers:** `1 writer + 1 tester`
- `impl` — the trial (item 1), the adopt decision record, items 3 and 5.
- `test` — item 4, after the adopt decision.

**Acceptance gate:**
- The trial numbers (crates compiled, wall time, lock wait) are in the phase report with the adopt decision.
- If adopted: the tests of each changed module pass; `basedpyright` over each changed file reports `0 errors, 0 warnings, 0 notes`; `sweep.py rebuilds --days 1` 24 h after merge shows the first-build cold-h of mktemp and scratch.py folders, next to the 7-day figures.
- The notice has an `also touches scripts/style/style_sync.py (owner natedev style-unit)` line, tested against `origin/natedev`.
- Change-Review line: a new temp clone of hana builds only the crates that changed | in any Claude session on natedev, make a clone with `python3 ~/.claude/scripts/lib/scratch.py new hana` and build it twice in two clones | the second clone's build log step compiles under half the crates of the first.

### Phase 5 — Clone builds turn incremental mode off, and mend uses the compile cache  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** builds in temp clones store their workspace crates in sccache, and mend uses sccache, if a measured trial shows hits. Approved by the showrunner 2026-10-29 (W3). Measurement: workspace crates build with `-C incremental`, so sccache never caches them; mend runs with `RUSTC_WRAPPER=` (`scripts/lint/invoke.sh:582`), 31.6 cold-h in `/tmp` this week; sccache is full at 64 of 64 GiB.

**Spec:**

1. **Trial first.** Two hana clones at one commit. Build both with `CARGO_INCREMENTAL=0` and record sccache hits for workspace crates on the second (stats before and after). Find why mend clears `RUSTC_WRAPPER` (does `cargo mend` drive rustc itself?), and test mend with sccache in a clone.
2. **Adopt rule.** Adopt each change only on a measured gain: workspace-crate hits on the second clone, or a shorter mend run with the same result.
3. **If adopted:** `scripts/direct/verify.sh` sets `CARGO_INCREMENTAL=0` for builds in a temp clone (not in `~/rust` worktrees, where incremental helps); `scripts/lint/invoke.sh` runs mend with sccache.
4. **sccache size and disk room.** Report the full cache and the disk numbers to natedev, which decides the cache size and the disk room as root.
5. **Tests** in the verify-unit test modules that cover the changed lines.

**Files:**
- `scripts/direct/verify.sh` — item 3 (owner verify-unit; trial-merge against `origin/natedev-verify` and `origin/natedev`).
- `scripts/lint/invoke.sh` — item 3 (owner verify-unit; same trial merges).
- `scripts/direct/` — the verify-unit test module for the changed `verify.sh` lines (owner verify-unit).
- `scripts/lint/` — the test module for the changed `invoke.sh` lines (owner verify-unit).

**Workers:** `1 writer + 1 tester`
- `impl` — the trial, the adopt decision record, item 3, the item 4 message text for the director.
- `test` — item 5, after the adopt decision.

**Acceptance gate:**
- The trial numbers are in the phase report with each adopt decision.
- If adopted: the changed modules' tests pass; `basedpyright` over every changed Python file reports `0 errors, 0 warnings, 0 notes`; the notice has `also touches` lines for verify.sh and invoke.sh (owner verify-unit), tested against `origin/natedev-verify` and `origin/natedev`.
- Change-Review line: temp-clone builds and mend runs reuse the compile cache | in a terminal on natedev, `sccache --show-stats` before and after a second clone's build | the Rust hit count rises by the workspace crates of the second build.

### Phase 6 — A session can launch an app that is already built without waiting at the BRP launch gate  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the user's words (2026-10-29): "add it to the queue to make it so that a session can bypass the hook if it knows the app is already built". The BRP launch gate (`scripts/hooks/pre-tool-use-brp-launch-gate.sh`) treats every launch as a build and waits for memory admission. A launch whose binary is already built and up to date skips the wait.

**Spec:**

1. **The hook decides from facts, not a claim.** For a `brp_launch_bevy_app` or `brp_launch_bevy_example` call, the hook finds the target's binary and asks cargo whether it is fresh (`cargo build --message-format=json` with `--offline`, no compile: use the `fresh` field of a dry check, or compare the binary's mtime with the newest input the fingerprint names). A fresh binary skips memory admission; a stale or missing one keeps the current wait.
2. **A session can also say so.** A launch call whose session set `BRP_LAUNCH_PREBUILT=1` (or the tool's own flag, if the BRP tool has one) skips admission only when item 1 confirms the binary is fresh; a stale binary still waits, and the hook prints why.
3. **Tests** in `scripts/hooks/test_brp_launch_gate.py`: fresh binary skips the wait; stale binary waits; the session flag with a stale binary waits; fake memory readings throughout (Phase 1 helper).

**Files:**
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — items 1–2.
- `scripts/hooks/test_brp_launch_gate.py` — item 3.
- `docs/as-built/build-memory-admission.md` — the hook section.

**Workers:** `1 writer + 1 tester`
- `impl` — items 1–2 and the doc.
- `test` — item 3.

**Acceptance gate:**
- `test_brp_launch_gate.py` passes; the hook's added checks take under 1 s on a fresh hana binary.
- Change-Review line: launching an already-built Bevy app through BRP starts at once | in a Claude session on natedev with a built hana, launch it with the BRP launch tool | it starts without a `waiting for memory` line.

### Phase 7 — A one-line command summarizes the machine's working set, and the disk-floor sweep uses less memory  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** the user's words (2026-10-29): "investigate why we use 12 GiB of running python helpers - we have to reduce our working set - more generally you should provide a quick way for me to summarize our working set by categorize that i can run from a simple shell command anytime". The survey (2026-10-29 10:50 EDT) found the causes; the style_plan cache, loss_census90 and codex_mesh follow findings went to the showrunner to route to their owners. This phase adds the summary command and cuts this unit's own share: the disk-floor sweep's 1.3–3.9 GB peak (`scripts/lint/tmp_sweep.py` :527–543, :1157).

**Spec:**

1. **`working-set` command.** A script in this unit's files (`scripts/build_targets/working_set.py`, with a shell name on PATH through the existing command wrappers) that prints, in under 2 s: memory by category (cargo/rustc, Claude sessions, Codex, Python helpers by script, other), resident PSS and swap for each, the top processes per category, and the machine totals (available, swap used). It reads `/proc` only and writes nothing. Its categories come from one table in the script.
2. **Python helpers grouped by script and owner.** The Python row breaks down by script path and names the owning unit from the production docs' Owns cells.
3. **The sweep's peak.** `tmp_sweep.py` stops holding the whole scan in memory at :527–543 and :1157 (stream the entries, keep only what the decision needs). The peak during a sweep falls under 300 MB, measured with `/usr/bin/time -v` on a sweep run.
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
- `working-set` prints the table in under 2 s on natedev, and its totals agree with `free -m` within 5%.
- A sweep run's max resident size is under 300 MB.
- Change-Review line: one command prints the machine's memory by category | in a terminal on natedev, type `working-set` | a table of cargo, Claude, Codex, Python by script, and other, with PSS and swap per row.
