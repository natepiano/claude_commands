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

### Phase 2b — A user's bump merges first and gates the pushed tip  · status: done
**Depends on:** none

#### As-built

- `merge_checkpoint.py --bump` is for a user's bump only, and its help text says so. It applies only to a code checkpoint. `user_bump` refuses it with `--shrink`, `--as-built` or `--from-child` before any merge.
- With `--bump`, `run` keeps scope, conflicts, other units, merge, push, records and promotion in their order, but runs no tests before the push. The `test` step reports that the gate runs on the pushed tip in the background, and the merge log line says `gate after push`. A rerun after a stop between merge and push also runs no tests. Without `--bump`, nothing changes: a red test stops the merge.
- After the push and before `promote`, `start_gate` writes a `BumpGateRecord` to `checkpoint_step_record(checkout, merge_head, "bump-gate")`, with `bump-gate.log` beside it. It starts `scripts/production/bump_gate.py --record <path>` in its own session, with stdin closed and output in the log, and does not wait. Promotion runs as for any merge. A start failure is a red result and does not stop promotion.
- The record holds the tip, the gate plan (paths, Merge tests, flakes, flaky tests), the production and unit names, the runner `pid` and `proc_start`, `result` (`pending`, `green`, `red`), `detail`, `log` and `delivered_to`. A rerun for the same tip starts no second gate while the runner lives, reports `held` when liveness is unknown, starts the gate again when the runner died with no result, and sends a completed result only to recipients not in `delivered_to`.
- `bump_gate.py` claims the record under `record_lock`, and stops with no tests when the result is done or a live other runner holds it. It makes a `git clone --shared` of the production checkout in a temporary folder beside the record, detached at the tip, and runs `run_checkpoint_test_steps` there. Flake notes go to the real checkout's production log. The clone goes at the end; the log stays. Any failure, also before the first step, is red.
- `run_checkpoint_test_steps(production, paths, merge_tests, flakes, flaky_tests, log) -> str` is the one copy of the test steps, pass rule, flake rule and rerun of named failures. `checkpoint_tests` and the bump gate both call it. It appends to the log, and the caller owns merge rollback. A red step names its failing tests when known.
- A red result goes to the unit and the showrunner, a green result to the showrunner only, through `scripts/message/send.py` (`MERGE_CHECKPOINT_SEND` overrides it). The text is `Bump gate <red|green>: <unit>; tip <tip>; <detail>; log <log>`. A failed send is written to the log and retried once. Each sent recipient is saved in `delivered_to`.

**Files:**
- `scripts/production/merge_checkpoint.py` — the flag, the path with no tests, `start_gate`, the record and its lock, `run_checkpoint_test_steps`, `complete_gate`, `send_result`, `bump_gate_addresses`.
- `scripts/production/bump_gate.py` — the background gate on the pushed tip.
- `scripts/production/test_merge_checkpoint_bump.py`, `scripts/production/merge_checkpoint_test_support.py` — tests through the real `run` path on a fixture repo with a real origin, and their fixture.
- `commands/showrunner/bump.md` — step 3 runs <MergeCheckpoint/> with `--bump`; step 4 says what the gate does; `bump done` is logged once the tip is green. `commands/showrunner/produce.md` and `docs/as-built/showrunner-automation.md` name the flag.

**Gotchas:**
- `complete_gate` and `send_result` read the record again under `record_lock` and write only when it still names this runner (`same_gate_runner`: tip, pid, process start). An old runner cannot replace a new owner's result or send one.
- Addresses come at send time from `bump_gate_addresses(slug, unit)` (`unit_lookup.marked_unit`, `showrunners.current_address`). The record keeps only slug and unit, so a notice follows a recipient that moved during the run.

**Ruled out:** the gate in the production checkout (a merge may start there), in a copied `.git` pointer (it moves the real branch) or in an export with no git (it fails a shutdown test); a second copy of the test-step, pass or flake code.

### Phase 2c — The assertion guard pairs a moved Rust string continuation  · status: todo
**Depends on:** none

#### Work Order

**Goal:** a Rust assertion that moves and changes indent, with a `\` line continuation inside a string, pairs with its old copy and is no finding.

**Spec:**
- Source: the sdlc showrunner, 2026-10-30, change-review row C437: in `~/rust/cargo-liner`, `assertion_guard.py --base e21d5b93^ --head e21d5b93` prints 74 blocking where the row says 73. "If the guard got better, correct the row; if wrong, fix the guard." The guard is wrong: the 74th item is `crates/cargo-tile/src/render.rs:14864 a_short_tree_asks_for_fewer_rows_than_a_long_one removed`. The commit moved that assertion to `render/tests/mod.rs:15961` with one less indent. Its tokens are the same; only the spaces after a `\` line continuation inside the message string changed (13 to 9).
- **Cause.** `normalized()` (`scripts/direct/assertion_guard.py:140-148`, since db889600, phase 6b) keeps each literal span exactly as written, so whitespace inside a Rust string continuation counts. Rust drops a `\` + newline and all leading whitespace on the next line, so both strings have the same value.
- **Fix at the cause.** Inside a non-raw Rust string literal (`"…"`, `b"…"`, `c"…"`), the comparison key removes each `\` + newline + the leading whitespace after it, which is Rust's own rule. An escaped backslash (`\\`) before a newline is no continuation. Raw strings (`r"…"`, `r#"…"#`) keep their text as written. Python literals keep their text as written. Every other part of the 6b literal-exact pairing stays as it is.
- After the fix, the C437 command prints 73 blocking again, and the row stays as written.

**Files:**
- `scripts/direct/assertion_guard.py` — the key rule
- `scripts/direct/test_assertion_guard_strings.py` — regression tests
- `scripts/direct/assertion_guard_test_support.py` — fixture additions, if needed

**Workers:** `1 writer + 1 tester`.
- `impl` — `scripts/direct/assertion_guard.py`
- `test` — `scripts/direct/test_assertion_guard_strings.py`, `scripts/direct/assertion_guard_test_support.py`

**State:** none.

**Constraints from prior phases:** every hang limit comes from `scripts/lib/test_hang_guard.py` (Phase 2).

**Acceptance gate:**
- A test through the real guard on a fixture repo: an assertion with a `\` continuation in its message moves to a new file with a new indent and less space after the `\`; the guard reports no finding for it. Without the fix, the test fails.
- A test: the same move where the string's value changes (a word after the `\` differs) is still a finding.
- A test: an escaped `\\` before a newline, and a raw string with `\` and a newline, keep their whitespace in the key.
- `python3 -m unittest discover -s scripts/direct -p 'test_assertion_guard*.py'`: green.
- In `~/rust/cargo-liner`, `python3 scripts/direct/assertion_guard.py --repo ~/rust/cargo-liner --base e21d5b93^ --head e21d5b93` from the worktree prints `assertion guard: 73 blocking`, and its other 73 items are the ones the current guard prints. The director runs this check.
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
