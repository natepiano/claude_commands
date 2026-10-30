# sdlc-devtools-tests

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Make the `~/.claude` repo tests run in every gate, faster, and with less CPU. Phase 1 fixes the gate's test list. Phases 4-6 come from the measured inventory of full gate 3 (2026-10-30).

> **Production: sdlc** — unit `devtools-unit`; production doc `docs/plans/sdlc-production.md`

## Source

The user, through the sdlc showrunner, 2026-10-30: "it can also be where we look at making repo tests go faster and take less cpu". Phase 1: herdr through natedev and the showrunner, 2026-10-30. natedev agreed the `merge_gate.py` edit ("take it"; no open natedev branch touches it).

Known input for the inventory: `scripts/agents/test_codex_mesh.py` was killed at the 1800 s module limit under load (gate_merge77.log). It takes about 1251 s unloaded and 741 s in a later gate, and it grows with each mesh test. Check first if herdr's plan retires the mesh soon.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-10-30T11:40:41.136+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Key files:**
  - `scripts/production/merge_gate.py` — the merge gate: finds test modules, runs them under a memory cap, keeps the passed-tree ledger (`PassedTreeLedger`)
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — its fixture and tests
  - `docs/as-built/merge-gate-and-test-speed.md` — the gate's as-built
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only --full`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, `~/.cargo/bin`).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - All text uses Simplified Technical English (user, 2026-10-18).
  - One place for each thing: no second list of suites anywhere (user, 2026-10-30).

## Phases

### Phase 1 — The merge gate finds every test module itself  · status: done
**Depends on:** none

#### As-built

- `scripts/production/merge_gate.py` has no suite list. `discover_test_modules` finds every `scripts/**/test_*.py` outside `__pycache__`, sorted by path. A module's suite is its first folder under `scripts/`, so a module in a nested `tests/` folder runs under its top folder's suite. Each module runs as `unittest discover -s <its folder> -p <its file>`, with the repo as cwd.
- A test file directly in `scripts/` makes the gate refuse before any job starts, and the message names the path. An empty test list is refused too. The passed-tree ledger `suites` field holds the sorted found suite names.
- `GateJobKind` (`TEST_MODULE`, `TYPE_CHECK`) selects job behavior, so a folder named `types` runs its tests. `GateJobRequest` keeps each job's real path, so holds and retries run that path.
- Each job reserves its last recorded peak × `MEMORY_PEAK_MARGIN` (1.2). One margin covers modules and the types job. A module with no record reserves the largest recorded module peak × 1.2, or `UNRECORDED_MODULE_RESERVATION_MIB` (640) when no module has a record. A types job with no record reserves `TYPES_MEMORY_MIB` (3072).
- A job starts only when the latest aggregate sample, the remaining reservations of the running jobs and its own reservation sum to the cap or less. With nothing running, one job always starts.
- `module_peak_mib.json` in the gate state dir holds each module's last sampled peak under its `<suite>/<path within the suite>` key (`test_module_name`), beside `types_peak_mib.json`. The gate writes it at run end. A job killed for a limit records nothing.
- `DEFAULT_MEMORY_CAP_MIB` is 6144. It holds the types job's reservation (full-tree basedpyright peak 4065 MiB × 1.2 = 4878 MiB) with modules beside it. A job over the cap is killed, and a sampled peak over the cap makes the run red.

**Files:**
- `scripts/production/merge_gate.py` — discovery, job kinds, recorded-peak admission, peak records, the cap.
- `scripts/production/merge_gate_test_support.py` — the fixture; the folder names it writes are fixture data only.
- `scripts/production/test_merge_gate*.py` — tests for discovery, nested modules, the `types` folder, the root-file refusal, the ledger key and admission.
- `scripts/berth/tests/check_installed_hooks.py` — checks the installed front end, not the checkout, so it is not a gate test. `scripts/berth/install/README.md` tells how to run it after an install: `cd ~/.claude/scripts && python3 -m unittest berth.tests.check_installed_hooks`.
- `docs/as-built/merge-gate-and-test-speed.md` — discovery and recorded-peak admission. `long-lived-workers.md`, `style-showrunner.md`, `bevy-upgrade.md` and `showrunner-automation.md` in `docs/as-built/` describe discovery, not a list.

**Binds later work:** a test module in any `scripts/<folder>/` runs in every gate with no list edit. The copy check in "Every test uses the one hang guard" uses this.

**Gotchas:**
- A new test folder changes the ledger `suites` key, so the next gate runs in full one time.
- `shot_report/test_transcripts.py` fails only when TMPDIR is inside a Claude scratchpad path. Under the gate's `/dev/shm` TMPDIR it passes.

**Ruled out:** a `"types"` suite name to select the type check; a retry path built again from suite and module names.

### Phase 2 — Every test uses the one hang guard  · status: done
**Depends on:** Phase 1; models' dc80ee4c (`scripts/lib/test_hang_guard.py`, `scripts/agents` on it), on origin/sdlc and merged here at d721ac43

#### As-built

- Every test module and test support module imports its hang limit from `scripts/lib/test_hang_guard.py` (`TEST_HANG_GUARD_SECS = 600`, `LauncherHangGuard`). No local copy or alias stays, `HANG_GUARD_S` included. The change spans every `scripts/` folder except `scripts/agents/`, which already used the guard.
- `scripts/lib/test_one_hang_guard.py` parses every `scripts/**/*.py`. It refuses an assignment to `TEST_HANG_GUARD_SECS` outside the guard module, a module-level alias, and an import of either name from any other module. Each refusal names `path:line`. A second test runs the check on a temporary tree with one copy and expects the refusal.
- A literal timeout under 120 s on a wait for an expected event now uses the guard, because short real-time limits fail under load. A wait that proves an event does not occur in a window stays. A sleep that is the behavior under test stays. A timeout given to product code as its own setting stays.
- The fixture script text in `scripts/production/merge_gate_test_support.py` keeps its `__TEST_HANG_GUARD_SECS__` placeholder, because the fixture repo has no `scripts.lib`. The value put in comes from the imported name. Text in a string is not code, so the check passes it.

**Files:**
- `scripts/lib/test_one_hang_guard.py` — the copy check and its temporary-tree test
- `scripts/**/test_*.py`, `scripts/**/*_test_support.py` — take the guard from the one place
- `docs/as-built/merge-gate-and-test-speed.md` — names the one 600-second guard

**Binds later work:** Every test hang limit comes from `scripts/lib/test_hang_guard.py` and the check refuses a copy. The merge gate finds the check by itself. `scripts/production/test_merge_gate_suites_resources.py` keeps its real CPU workloads and their assertions, so `os.wait4` can tell the workloads apart.

**Gotchas:**
- Assertions on elapsed wall time keep their literal limits. No shared test clock exists.
- Code from a merge can add a copy. The check then goes red in the gate, and the merge must move the copy to the guard.
- A commit message with `Assertion-Change` lines must come from a file (`git commit -F`), because the commit hook reads `-m` text badly.

**Ruled out:** A support module that passes the guard name on: each file imports it directly. `LifecycleTestClock` as the shared clock: it lives only in `scripts/agents/test_codex_mesh.py` and retires with the mesh.

### Phase 2b — A user's bump merges first and gates the pushed tip  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `merge_checkpoint.py --bump` merges and pushes a code checkpoint first, then runs the gate on the pushed tip in the background and sends a red result to the unit. `commands/showrunner/bump.md` matches the tool.

**Spec:**
- Source: the sdlc showrunner, 2026-10-30: "commands/showrunner/bump.md:21-25 says a bumped checkpoint merges on lint and related tests, and the full suite runs on the pushed tip after the merge. merge_checkpoint.py has no such path: every merge runs the full gate. Natedev merged the instant move by hand today for that reason. Add one flag to merge_checkpoint.py for the bump path: merge and push first, then run the gate on the pushed tip, in the background, and send a red result to the unit. Only a user's bump uses it. Keep bump.md matching the tool."
- **The flag.** `--bump` is valid only for a code checkpoint. With `--shrink`, `--as-built` or `--from-child`, `request_from` refuses it, as it refuses `--cancel-prior` today (`merge_checkpoint.py:318`). The help text says only a user's bump uses it.
- **Before the push.** `run` (`merge_checkpoint.py:1134`) keeps scope, conflicts, other units, merge, push, records and promotion in their order. With `--bump`, `tests()` does not run, and `report("test", "ok", ...)` says the gate runs on the pushed tip. The merge record names that the gate comes after the push. Without `--bump`, nothing changes. A rerun after a stop between merge and push takes the same path with no tests.
- **After the push.** `merge_checkpoint.py` starts a new script, `scripts/production/bump_gate.py`, as a detached process (its own session, stdin closed, output in a log file), and does not wait for it. It starts the process after `push` and before `promote`; promotion runs as for any merge, because the user wants the work now.
- **The gate run.** `bump_gate.py` makes a `git clone --shared` of the production checkout at the pushed tip, detached (never the production checkout: a merge may start there; a copied `.git` pointer moves the real branch, and an export with no git fails a shutdown test). It runs in the clone the same steps a normal merge runs: `test_steps` over the checkpoint's paths and the production's Merge tests. It uses the same pass rule (`test_passed`), flake rule and rerun of named failures as `checkpoint_tests`. Share that code; a second copy of any of them is a defect. It never resets or changes the merge branch, and it removes its clone at the end. The log stays after the clone goes.
- **The result.** A red result goes through `scripts/message/send.py` to the unit and to the showrunner: the step that failed, the failing test names when known, the tip, and the log path. A green result goes to the showrunner only, as one line. The showrunner needs it because bump.md keeps the work top priority until the tip is green. A run that cannot start or that fails before its first step counts as red. A failed send is written to the log and retried once; it never stays silent. The unit's address comes from the lookup production code already uses; do not add a second one. (Decision: the showrunner also gets the result, which is one extra send, so bump.md's "until the tip is green" has a source. Revert: one line.)
- **Rerun.** A rerun of the command for the same tip starts no second gate while the first lives. When the first is gone with no result, the rerun starts it again. Keep this record through `checkpoint_step_record`, and use the repo's existing process identity check; add no new one.
- **bump.md.** Step 3's <MergeCheckpoint/> line names `--bump`. Step 4 says what the flag does: the gate on the pushed tip, in the background, red to the owning unit and the showrunner. The merge unit's full run stays for a production with a merge unit. The Rules line "It skips only the full suite before the merge" stays true. `commands/showrunner/produce.md` and `docs/as-built/showrunner-automation.md` name the flag where they list `merge_checkpoint.py`'s options.

**Files:**
- `scripts/production/merge_checkpoint.py` — the flag, the path with no tests, the start of the background gate; any shared test-step code
- `scripts/production/bump_gate.py` — new: the background gate on the pushed tip
- `scripts/production/test_merge_checkpoint_bump.py` — new: tests through the real merge path
- `scripts/production/merge_checkpoint_test_support.py` — fixture additions, if needed
- `commands/showrunner/bump.md` — matches the tool
- `commands/showrunner/produce.md` — names the flag
- `docs/as-built/showrunner-automation.md` — names the flag

**Workers:** `1 writer + 1 tester` — `merge_checkpoint.py` is the hub, owned by `impl`.
- `impl` — `scripts/production/merge_checkpoint.py`, `scripts/production/bump_gate.py`, the three docs
- `test` — `scripts/production/test_merge_checkpoint_bump.py`, `scripts/production/merge_checkpoint_test_support.py`

**State:** `bump_gate.py` writes one per-merge step record (started, process identity, result) through `checkpoint_step_record`, and one log. Both stay until the normal cleanup of those records.

**Constraints from prior phases:** every hang limit comes from `scripts/lib/test_hang_guard.py` (Phase 2). `test_merge_checkpoint_rerun.py` (3 tests) and `test_merge_checkpoint_validation.py` `test_a_merge_adds_a_row_per_visible_change_to_the_named_list` fail on origin/sdlc 7a18d704 before this phase, and they pass on main 6c319087, which changes `merge_checkpoint_test_support.py`. They are not this phase's to fix. Do not port main's change here; the sdlc merge of main brings it.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint*.py'`, from the worktree root: green, except the four tests named above.
- `scripts/production/test_merge_checkpoint_bump.py`, through the real `run` path on a fixture repo with a real origin, shows each of these:
  - A merge test that fails: with `--bump`, the merge is on origin and the command exits 0. A fake `send.py` (`MERGE_CHECKPOINT_SEND`) receives the failed step, the tip and the log path, for the unit and the showrunner.
  - A merge test that passes: the showrunner gets one line, and the unit gets nothing.
  - The gate ran in a clone at the pushed tip: the fixture's test command writes `git rev-parse HEAD` and its cwd to a file, the HEAD equals the tip, and the cwd is not the checkout. The production checkout's HEAD and status do not change during the gate, and the clone is gone at the end.
  - `--bump` with `--shrink`, `--as-built` or `--from-child` is refused, with no merge.
  - A rerun for the same tip while the gate lives starts no second gate; a rerun after the gate died with no result starts one.
  - Without `--bump`, the same failing test stops the merge, as today.
- basedpyright prints `0 errors, 0 warnings, 0 notes` on each slot's changed files.
- The director runs the full merge gate (`--tests-only --full`) on the phase tree.

### Phase 3 — Close-route closes a route made before its phase record  · status: done
**Depends on:** none

#### As-built

- `scripts/history/routed.py` keys each route `<run id>:<phase instance id>:<finding id>` from its `finding_routed` event. A route made before its run's phase record has an empty phase instance id, so the routed-work list prints its key as `<run id>::<finding id>`.
- `findings.py close-route --from` takes the exact key that the routed-work list prints, and it accepts an empty phase instance id. It refuses a key without exactly three parts, and a key with an empty or blank run id or finding id. The refusal message names both forms: `<run id>:<phase instance id>:<finding id>` and `<run id>::<finding id>`.
- The `finding_route_closed` event keeps its shape. No reader of it changed.

**Files:**
- `scripts/direct/findings.py` — `_close_route` checks the `--from` key.
- `scripts/direct/test_findings.py` — one test closes a route from before a phase record through the real routed-work join (`routed_work`); one test shows that nine malformed keys refuse and record no event.

**Ruled out:** a second `--from` form, or a translation from one form to the other — `--from` is always the exact listed key.

### Phase 4 — The gate records each job's CPU time  · status: done

#### As-built

- `reap_gate_job` in `merge_gate.py` reaps a job leader with `os.wait4`. It stores the exit status and `ru_utime + ru_stime` on the job, and `GateJobResult.cpu_seconds` carries the value.
- `reap_gate_job` blocks SIGTERM and SIGHUP (`ROLLBACK_SIGNALS`) with `signal.pthread_sigmask` across `os.wait4` and the stores. An interrupt arrives only after the status and CPU are stored, so it cannot lose a reaped status or stop the cleanup of later jobs.
- At run end, `save_measures` writes `module_cpu_seconds.json` in the gate state dir, under the `<suite>/<module>` keys of `module_seconds.json`; the type-check job uses `types/basedpyright`. A job killed for a limit records nothing.
- After `Largest module peak:`, the report prints `Most CPU: <s> s <suite/module>, …; total <s> s`: the three keys with the most CPU, largest first. The run total adds every attempt, killed and requeued attempts too.

**Files:**
- `scripts/production/merge_gate.py` — `reap_gate_job`, the CPU record, the `Most CPU:` line
- `scripts/production/test_merge_gate_suites_resources.py` — spin and sleep CPU, killed job, report order, interrupt after a reap
- `docs/as-built/merge-gate-and-test-speed.md` — names the record and the report line

**Binds later work:** "The slowest modules lose their real-time waits" ranks modules by wall seconds minus CPU seconds from `module_seconds.json` and `module_cpu_seconds.json`. "The gate runs a large module's test classes as separate jobs" builds on two facts: `reap_gate_job` is the one path that reaps a job and reads its CPU, and one CPU key names one job.

**Gotchas:**
- `systemd-run --scope` execs the test in place, so the leader's rusage holds the test and the children it waited for. A child that `kill_job_group` kills after its leader exits is not counted.
- The signal mask covers the calling thread only. The gate starts no other thread.
- `module_seconds.json` holds the first attempt's wall time, and `module_cpu_seconds.json` holds the last finished attempt's CPU. Compare the two only from one attempt.

### Phase 5 — The gate runs a large module's test classes as separate jobs  · status: todo
**Depends on:** Phase 4; resources-unit's be4d46f7 on main (it changes the same `merge_gate.py` hunks)

Source: devtools-unit from the full gate 3 measure, 2026-10-30; natedev's go, 2026-10-30, with one condition (below). The gate already starts the slowest recorded module first (`module_order`), so the wall time is the largest single module: `agents/test_codex_mesh.py` took 1057 s of the gate's 1062 s (32 classes, 593 tests). The next largest are `direct/test_assign_launcher.py` 734 s (1 class), `shutdown/test_bounded_stop.py` 442 s (2 classes) and `direct/test_verify_hold_wait.py` 311 s (1 class). herdr deletes `test_codex_mesh.py` only in its Phase 18. Goal: a module whose recorded time is over a limit runs as one job per test class, each with its own recorded time, peak and CPU time, so no test file changes and the wall time drops to about 734 s. natedev's condition: a module with `setUpModule`, `tearDownModule` or module-level state that its classes share stays one job; split only modules whose classes run alone, and a test proves that a split module gives the same pass and fail result as one job. The Work Order is written when be4d46f7 is on main.

Precondition (the sdlc showrunner, 2026-10-30): `production/test_dailies_input.py` `test_style_summary_is_saved_only_for_a_style_production_and_keeps_every_ledger_key` fails only in a full `scripts/production` discover run (`rules_total` 58, expected 1) and passes alone. The likely cause: `style_rules` reads `NATE_STYLE_DIR` at import, and `dailies_input_test_support.py:276` runs `style_ledger` in the same process, so an earlier test's rule catalog stays. Fix it at the cause (read the variable when called, not at import). A class or module split must not hide it.

From Phase 4 (architect review, 2026-10-30): `agents/test_codex_mesh.py` has `setUpModule`, so natedev's condition keeps it one job, and the other named modules have one class each. Before the Work Order, list the modules that the condition allows and that hold more than one class, with their recorded seconds. If no allowed module would cut the gate's wall time, tell the sdlc showrunner and drop this phase. A Work Order must: give each job one explicit target (whole module or `module:Class`) carried through the job lifecycle with one identity rule, so class records do not overwrite each other in `module_seconds.json`, `module_peak_mib.json` and `module_cpu_seconds.json`; say how `module_order` and Phase 6 read class records and ignore stale module records; keep `reap_gate_job` as the one reaping path (be4d46f7 still uses `poll()`/`wait()`); keep the interrupt-cleanup, waited-child, retry-total and killed-record tests; accept on distinct class records, nested paths and a rerun of only the failed class.

### Phase 6 — The slowest modules lose their real-time waits  · status: todo
**Depends on:** Phase 4; Phase 2

Source: devtools-unit, 2026-10-30. From Phase 4's CPU measure, each module over 60 s whose time is mostly waiting (low CPU) gets its cause found and fixed in its own test or fixture. First candidates: `direct/test_verify_hold_wait.py` (5 tests, 311 s) and `direct/test_assign_launcher.py` (41 tests, 734 s). Fence (the sdlc showrunner, 2026-10-30): models owns the real-time waits in `scripts/agents/test_codex_mesh.py` and `scripts/agents/test_heartbeat.py` (its hang guard checkpoint). This phase leaves those two files out, unless Phase 4's measure still shows them on top after models' checkpoint merges; then agree the split with models first. The Work Order is written from Phase 4's measure.

From Phase 4: the input is `module_cpu_seconds.json` beside `module_seconds.json`. The two can come from different attempts (wall time from the first attempt, CPU time from the last attempt that finished; a limit kill updates wall time only), so the ranking uses wall and CPU seconds from the same attempt (the paired `GateJobResult` values), with tests for a retry and a limit kill.
