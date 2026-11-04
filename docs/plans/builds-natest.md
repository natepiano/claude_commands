# natest

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Record each `~/.claude` test run, test module and slow test in the build log, show their history at once with `/test_times`, and make the slowest test modules faster, from the top of a descending sort down.

> **Production: builds** — unit `natest-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-11-03 09:30 EST

From the user (via natedev): Mission: find the longest-running ~/.claude tests and make them faster. Keep a descending sort of test times; when it makes the longest one shorter, it takes the next one. Build tools that instantly show the history of ~/.claude test suite times and of individual test times. Use parallelism, removal of trivial tests, shared code and good engineering practice. It must also send architectural change proposals back. It organizes its work in phases so /unit:direct works, and keeps its own phase backlog: the user wants the standard process engaged.

## Delegation Context

- **Project:** `~/.claude` — the scripts, commands and config that run the user's productions. This plan records test times and makes the slowest test modules faster.
- **Project started:** 2026-11-03T14:50:07.684+00:00
- **Stack:** Python 3.13, standard library only (`unittest`, `sqlite3`, `json`); Markdown commands; `.conf` files read through `scripts/lib/conf.py`.
- **Layout:**
  - `scripts/test_times/` — new, natest-unit owns it: the module runner, the gate record writer, the `test_times` query tool and their tests
  - `scripts/production/merge_gate.py` — the merge gate (owner: sdlc devtools-unit)
  - `scripts/buildlog/` — the build log store and its SQLite index (owner: report-unit)
  - `config/test_times.conf` — new: the tunables of this plan
  - `commands/test_times.md` — new: the `/test_times` command
  - `scripts/<folder>/test_*.py` — the test modules that the speed phases change (natest-unit changes shared test files through cargo-berth's flow; production doc, Units → natest-unit)
- **Key files:**
  - `scripts/production/merge_gate.py` — line numbers on builds 772ef96a9: `MODULE_HISTORY_RUNS = 5` (103); `SUITE_BOOTSTRAP` (108, execs its argv); `GateJobResult` (159, has `cpu_seconds`); `gate_state_dir()` (510); `start_gate_job` (638); `test_module_environment` (655, gives each module a private `TMPDIR`, `BUILDLOG_DIR` and tool tripwires); `finish_gate_job` (678, finishes every job, the type job included); `PassedTreeLedger` (736, `CleanTestTree.tree` and `.commit`); `discover_test_modules` (864); `run_suites` (878): the module argv at 1003 is `[*scope_prefix, python, "-m", "unittest", "discover", "-s", str(path.parent), "-p", path.name]`, the type job argv is at 1000-1001, the rerun-alone pass keeps first results in `first_runs`, and the `finally` block (1074-1088) saves `module_seconds.json`, `module_cpu_seconds.json`, `module_peak_mib.json`, `types_peak_mib.json` and `module_history.json`; the summary lines (1093-1127) print each folder suite's time from `suite_started` and `suite_finished` Since Phase 2 (8bc0d5c88): each module job runs `RUNNER` (`scripts/test_times/module_run.py`), not `unittest discover`; `discover_test_modules` (877); `record_gate_run` writes the run record.
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — the gate's fixture and tests
  - `scripts/buildlog/store.py` — `root()` (honors `BUILDLOG_DIR`), `host_name()`, `utc_iso(epoch)`, `host_file(host, epoch)`, `append_line(path, record)`: the JSON-lines store, one file per host per month
  - `scripts/buildlog/record.py` — `RECORD_VERSION`, `new_id(host, epoch)` (513), `ssh_attempt` (1055): the pattern for a one-line record writer
  - `scripts/buildlog/index.py` — `SCHEMA_VERSION = 29` (42); column lists such as `TEST_COLUMNS` (141) and `CALL_COLUMNS`; `TABLES` (452, with the gate tables); `create_schema` (574, adds a `<table>_src` index for each table); `insert`, `pick`; `add_step` (763, the pattern for a nested list that becomes child rows); `ADDERS` (950, `gate_run` maps to `add_gate_run`); `forget` (954, loops `TABLES`); `update()` (1012); `read_only()` (1051)
  - `scripts/test_times/gate_record.py` — the `gate_run` record types (`ModuleTimes`, `ModuleRun`, `SuiteRun`, `GateRun`, `NoModuleTimes`), `TYPE_CHECK_JOB` (38), `read_module_times` (131), `kept_tests` (153), `slow_test_s` (162), `write_gate_run` (166) Since Phase 2: `Execution` (106), `module_runs` (185), `report_not_written` (213), `write_gate_run` (219).
  - `scripts/buildlog/cli.py` — runs `index.update()` before each report (140)
  - `scripts/lib/conf.py` — `read(file).number(key)`; a config file puts a `# range: 0..` or `# whole: 1..` line directly above each `KEY=value` (see `config/mesh_runs.conf`)
  - `config/README.md` — one entry for each config file
  - `scripts/lib/user_zone.py` — `user_zone()`; `scripts/lib/test_user_zone.py` scans every script and fails an `astimezone()` with no zone
  - `scripts/lib/terminal_cells.py` — terminal cell widths for aligned tables
  - `commands/build/status.md` — the form of a command that runs one script and shows its lines unchanged
  - `docs/as-built/merge-gate-and-test-speed.md` — the gate's as-built and its test rules: no live state, no wall-clock thresholds, no fixed waits in the code under test, few process launches, no trivial tests
  - `~/.local/state/merge-gate/module_history.json` — the gate's last 5 wall times for each module, with no times of day; it seeds the first sort only
- **Test lanes:** none as `tests/` folders. Each module's tests sit beside it as `scripts/<folder>/test_<name>.py`; the `test` worker writes those files.
- **Build:** none (Python).
- **Test:** for each named test file, `python3 -m unittest discover -s scripts/<folder> -p '<file>'` from the worktree root; always also `python3 -m unittest discover -s scripts/lib`. Checkpoint gate: merge origin's merge branch first, then `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/merge_gate.py --checkout <tree> --tests-only` (main's copy of the gate).
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures. They never read or write live state: `~/.claude` main, `~/.local/state/buildlog`, `~/.local/state/merge-gate`, `/tmp/claude` (the gate's as-built).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - All text uses Simplified Technical English (user, 2026-10-18).
  - One place for each thing (user, 2026-10-30): reuse the build log store and index, `conf.py`, `user_zone.py`, `terminal_cells.py` and the gate's `discover_test_modules`; never copy a reader, a parser or a module list.
  - A tunable lives in config and is read at use (user, 2026-10-31).
  - Speed work keeps coverage (user mission, 2026-11-03). Every behavior a module checked before stays checked. A trivial test is removed only with its covering test or its reason named in the phase report. A trivial test asserts a constant, the standard library, or only what another test in the same module already asserts.
  - A speed gain that needs a change outside a phase's Files goes to the showrunner as an architecture proposal with the measured gain; the phase does not make the change (user mission, 2026-11-03). Examples: gate scheduling, the type-check job, the design of production code, a new shared fixture library.
  - A record write never turns a gate red: a failed write prints one line and the gate goes on (this plan's choice).
  - A worker starts at most one detached run at a time, with at most 4 named test files; the wide suites go in one final gate run (sdlc, 2026-11-03). Fix every visible failure before a rerun; after a pass, do not run again until the edits are done (user, 2026-10-31).
  - No test run starts while a natedev build hold is in force (builds showrunner, 2026-11-03).
  - A change to another unit's file names that owner in the checkpoint notice (production doc, Production rules).

- **Speed method** (every speed phase):
  0. Targets: at dispatch, the director takes the top live rows of `test_times top` that no phase holds. When they differ from the modules the phase names, the director rewrites its Goal, Files and acceptance first. `types/basedpyright` is never a target; it goes to its architecture proposal.
  1. Baseline: before the edit, run the module alone three times with `TEST_TIMES_OUT=<run directory>/<module>-before-<n>-<sha>.json python3 scripts/test_times/module_run.py scripts/<folder> <file>` and keep the median `wall_s` and `cpu_s`. Read `python3 scripts/test_times/show.py module <folder>/<file>` for the gate history.
  2. Find the cost from the JSON: the slowest tests, and `wall_s - tests_s` (import and module or class fixtures). Name the expensive operations before any edit; do not apply a cut the module already has.
  3. Cut it, in this order:
     - Remove trivial tests (Invariants above).
     - Share expensive setup: build a seed once in `setUpModule` or `setUpClass` and copy it for each test, as `scripts/build_hold/test_build_hold.py` does.
     - Call code in-process in place of a subprocess when the test does not check the process boundary; keep one process test for each boundary.
     - Remove fixed waits and polling loops in the tests; wait on an event, a file or a pipe instead.
     - Split a module whose tests are independent into two or more files, so the gate runs them at the same time. The original file and its new files are one group: compare the group's summed CPU, its test count, and its wall time when its files run at the same time. Every file of the group is in acceptance.
  4. A change to a shared support file (for example `scripts/lint/memory_test_support.py`) is allowed only when every module that imports it stays green; name those importers in the final gate run.
  5. After the edit, measure three runs again, the same way, into `<module>-after-<n>-<sha>.json`. Half the wall time is the target. When the allowed edits cannot reach it, report the measured floor and send the rest as an architecture proposal. Report the before and after medians, each removed test with its reason or covering test, and each architecture proposal with its measured gain.

## Phases

### Phase 1 — The module runner writes each test's time  · status: done
**Depends on:** none

#### As-built

- `scripts/test_times/module_run.py <start_dir> <pattern>` runs one module through `unittest.main` with `TimedRunner`, so its output and exit status match `python -m unittest discover` (1 on a failure, 5 when no test ran, else 0). It uses the standard library only and puts `os.getcwd()` in `sys.path[0]`, so main's copy works by its absolute path on another tree. A wrong argument count prints `usage: module_run.py <start_dir> <pattern>` and exits 2.
- `TimedResult` records each outcome itself: `startTest` and `stopTest` open and close the parent test, `addSubTest` maps a subtest outcome to its parent, and `addDuration` keeps the time. Each parent id has one entry, by precedence `error` > `fail` > `unexpected_success` > `expected_failure` > `skip` > `pass`. A skip has `s` 0.0. A class or module fixture failure has its own `error` entry with its description as `test`.
- `main()` pops `TEST_TIMES_OUT` from the environment before discovery and keeps it in `TimedRunner.destination` (`TimesOutput | NoTimesOutput`). After the run, `write_times` writes `{wall_s, cpu_s, tests_s, tests_run, failures, errors, skipped, tests}` to that path with `os.replace`. `cpu_s` is the process plus its reaped children. A failed write prints `test times not written: <error>` to stderr and keeps the exit status.
- `scripts/test_times/gate_record.py` holds `TestStatus` (a `Literal` of the six statuses), the TypedDicts `TestTime`, `ModuleTimes`, `ModuleRun`, `SuiteRun`, `GateRun` and `GateRunRecord`, and the frozen dataclass `NoModuleTimes(reason)`.
  - `read_module_times(path: Path) -> ModuleTimes | NoModuleTimes` validates the whole file. A missing or invalid file gives `NoModuleTimes("<path>: <error>")`.
  - `kept_tests(times, slow_test_s) -> list[TestTime]` keeps every `fail`, `error` and `unexpected_success` test and every other test with `s >= slow_test_s`, slowest first.
  - `slow_test_s() -> float` reads `SLOW_TEST_S` at each call.
  - `write_gate_run(run: GateRun) -> None` appends `{"kind": "gate_run", "v", "id", "host", **run}` through `scripts/buildlog/store.py` and `scripts/buildlog/record.py`. It catches every error and prints `gate run not written: <error>`.
- `config/test_times.conf` holds `SLOW_TEST_S=0.5` and `TOP_RUNS=5`. `config/README.md` has its entry.

**Files:**
- `scripts/test_times/module_run.py` — the timing module runner
- `scripts/test_times/gate_record.py` — the record types, the times reader, `kept_tests`, `slow_test_s` and `write_gate_run`
- `config/test_times.conf` — `SLOW_TEST_S`, `TOP_RUNS`
- `scripts/test_times/test_module_run.py` — subprocess tests on fixture modules, compared with `python -m unittest discover`
- `scripts/test_times/test_gate_record.py` — reader, threshold and writer tests with `BUILDLOG_DIR` and `CONF_DIR` fixtures

**Binds later work:** The merge gate passes `TEST_TIMES_OUT` in each module job's environment, and the runner reads it once in `main()`. `write_gate_run` catches every error, so its caller adds no catch around it. `NoModuleTimes.reason` is the text the gate records as `times_missing`. `ModuleRun.restart` is empty or `hold`, and `ModuleRun.attempt` counts from 1.

**Gotchas:** Discovery imports the test modules before any test runs, so `TEST_TIMES_OUT` leaves the environment before `unittest.main`, not in `TimedRunner.run`.

**Ruled out:** a `Protocol` `isinstance` check on a subtest's `test_case` attribute — `addSubTest` already receives the parent test.

### Phase 2 — The merge gate records every run  · status: done
**Depends on:** Phase 1

#### As-built

- Each merge gate module job runs `RUNNER` (`scripts/test_times/module_run.py`, the gate checkout's copy) in place of `unittest discover`. `test_module_environment` sets `TEST_TIMES_OUT` to the module root's `test-times.json`, and `finish_gate_job` reads it with `gate_record.read_module_times`. The type-check job reads no file: its times are `TimingNotApplicable`.
- `run_suites` keeps one `gate_record.Execution` per finished job, a hold restart included. `record_gate_run` builds the `GateRun` with `gate_record.module_runs` and calls `write_gate_run` once per gate run.
- An exception after the first job starts writes the record with `aborted: true` (`record_on_abort`, a context manager first in the `run_suites` `with`) and raises again. An aborted record holds only the finished jobs.
- A failed record build prints `gate run not written: <error>` (`report_not_written`). The gate's output, exception and verdict never change.

**Files:**
- `scripts/production/merge_gate.py` — the runner argv, the times read, `executions`, `record_gate_run`, `record_on_abort`
- `scripts/test_times/gate_record.py` — `Execution`, `module_runs`, `report_not_written`
- `scripts/production/test_merge_gate_test_times.py` — the record tests

**Binds later work:** module rows: `attempt` counts from 1 per module, `restart` is `hold` or empty, `times_missing` is the `NoModuleTimes` reason and is empty for `ModuleTimes` and the type job. The first live `gate_run` record appears when this code reaches the `~/.claude` main checkout that runs the gates.

**Gotchas:** the record clock uses `datetime.now(UTC)`, because `test_merge_gate_holds.py` replaces `merge_gate.time` with a monotonic-only stub. Any edit to `merge_gate.py` runs every `test_merge_gate*.py` file, `test_merge_gate_holds.py` included.

### Phase 3 — The build log indexes gate runs  · status: done
**Depends on:** Phase 1

#### As-built

- `index.update()` indexes each `gate_run` log line into four tables: `gate_runs` (one row for each run), `gate_suites`, `gate_modules` and `gate_tests`. `add_gate_run` is in `ADDERS`; `forget` removes a source's rows from all four tables.
- Indexes: `gate_modules (suite, module, started_at)` and `gate_tests (suite, module, test, started_at)`. A suite, module or test history query uses them (0.1-0.6 ms over 500 runs). A nested entry that is not a dict is skipped.
- `SCHEMA_VERSION` is 29. The first `update()` after the version change rebuilds the index from the source logs (22.6 s on a copy of the live log).
- `gate_record.TYPE_CHECK_JOB = ("types", "basedpyright")` names the type-check job's (suite, module) pair; the index leaves that row out of `modules_run` and `tests_run`.
- `test_index.py` has a module-level `rows(sql, *parameters)` helper; `test_index_rerun.py` imports it.

**Files:**
- `scripts/buildlog/index.py` — the gate tables, their indexes, `add_gate_run`, `SCHEMA_VERSION = 29`
- `scripts/test_times/gate_record.py` — `TYPE_CHECK_JOB`
- `scripts/buildlog/test_gate_index.py` — gate table rows, rerun without new rows, `forget`, query plans over 500 fixture runs
- `scripts/buildlog/test_index.py` — the schema check follows `index.SCHEMA_VERSION`; the shared `rows` helper
- `scripts/buildlog/test_index_rerun.py` (owner report-unit) — imports the shared `rows` helper

**Binds later work:** the merge gate's type-check job uses `gate_record.TYPE_CHECK_JOB`. `/test_times` reads the four gate tables; a test-name substring match scans `gate_tests` (19 ms over 250,000 rows).

**Gotchas:** `write_gate_run` imports `record` and `store` inside its body: a module-level import makes the cycle index -> gate_record -> record -> memory_admit -> index.

**Ruled out:** a `gate_tests (test)` index — no B-tree serves a substring match; `executemany` for `gate_tests` inserts — saves 2 ms per rebuild and adds a second insert path.

### Phase 4 — `/test_times` shows suite, module and test history  · status: done
**Depends on:** Phase 2, Phase 3

#### As-built

- `scripts/test_times/show.py` has the subcommands `top` (the default), `suites`, `suite <folder>`, `module <folder>/<file>` and `test <text>`. Each subcommand runs `index.update()`, then queries `index.read_only()` for rows of the current host only.
- `top` ranks each live module by the median `wall_s` of its ranking rows from its last `TOP_RUNS` gate runs (`config/test_times.conf`). A ranking row is the lowest `attempt` with an empty `restart`. A module is live when `merge_gate.discover_test_modules(repo)` lists it; `types/basedpyright` is always live and shows empty test cells.
- A row with `times_missing` shows `times missing`, never zero. A test with no kept row in a later run shows `not kept`. A row of an aborted run carries the mark `aborted`; its completed module rows still count in `top`.
- Times show in the user zone as `11-03 09:30`. An empty result prints `no modules recorded yet` (`top`) or `no gate runs recorded yet` and exits 0. `top` answers in 0.73 s with a warm index.
- `store.locked_rows(path, start=0) -> Iterator[dict[str, object]]` opens a build log file, takes a shared flock and yields `whole_rows(handle, start)`; an absent file yields no rows. `record.read_attempt_results` reads through it.
- `terminal_cells.pad_left(text, cells)` and `pad_right(text, cells)` pad by terminal cell width.

**Files:**
- `scripts/test_times/show.py` — the history tool
- `scripts/test_times/test_show.py` — its tests, with one test that loads each gate-discovered module in `scripts/test_times`
- `scripts/test_times/gate_record_test_support.py` — the `gate_run()` and `module_times()` fixtures, shared by `test_gate_record.py`, `scripts/buildlog/test_gate_index.py` and `test_show.py`
- `commands/test_times.md` — `/test_times`: runs `show.py $ARGUMENTS` and shows its lines unchanged
- `scripts/buildlog/store.py` (owner report-unit) — `locked_rows`
- `scripts/buildlog/record.py` (owner report-unit) — `read_attempt_results` calls `locked_rows`
- `scripts/lib/terminal_cells.py` (owner queue-unit) — `pad_left`, `pad_right`

**Binds later work:** a speed phase reads before and after medians with `show.py module <folder>/<file>`. Gate records exist only after the record writer in `merge_gate.py` reaches the main checkout that runs the gates; until then, a speed phase measures its baseline with `module_run.py`. `show.py` imports `discover_test_modules` and `test_module_name` from `merge_gate.py`; "The test history tool starts without the merge gate module" moves them to a light module. `memory_admit.py` and `sweep.py` still keep their own copy of the locked row loop.

**Gotchas:**
- A file named `test_*.py` in a gate suite folder is a test module to the gate; a tool in that folder must not use that name.
- `test_show.py` imports `scripts.lib.test_hang_guard` absolutely, above its `TYPE_CHECKING` block: `scripts/lib/test_one_hang_guard.py` reads that import.
- A tree that changes `scripts/lib/terminal_cells.py` makes the gate run the full suite (36 min at load 50).

**Ruled out:** a `scripts/production/test_modules.py` light module — the name matches the gate's test pattern.

### Phase 5 — The two slowest direct modules run in half their time  · status: done

#### As-built

- The SSH target tests are five `test_verify_ssh_targets*.py` files that share `verify_ssh_targets_test_support.py`. The module runner runs them in parallel. Group wall time is 62-71 s (median), from 327.1 s; CPU is 181-188 s, from 205.5 s.
- The assign launcher tests are four `test_assign_launcher*.py` files (`test_assign_launcher.py`, `_identity`, `_reuse`, `_review`) that share `assign_launcher_test_support.py`. Most tests call the launcher in-process; `test_unreadable_staff_starts_no_worker` runs it as a separate process, because the progress recorder writes its refusal to the process stderr.
- The merge gate test-times fixture writes `build_admission.conf` and `build_folders.conf` into its private config folder.
- The build log index is at `SCHEMA_VERSION = 31`. A database at an older schema rebuilds, and the rebuild reads gate history from the start of each log, also before the saved offset.

**Files:**
- `scripts/direct/test_verify_ssh_targets*.py`, `scripts/direct/verify_ssh_targets_test_support.py` — the SSH target group
- `scripts/direct/test_assign_launcher*.py`, `scripts/direct/assign_launcher_test_support.py` — the assign launcher group
- `scripts/buildlog/index.py`, `scripts/buildlog/test_index.py` — schema 31 and the legacy-schema recovery test

**Gotchas:**
- A test fixture that sets its own `CONF_DIR` breaks each time a unit moves a setting into a new config file.
- CPU comparisons are valid only when the before and after runs go one after the other on an idle host.
- A split of a file that other units edit makes later upstream merges need hand ports: merge origin/builds before each split and keep the split window short.
- The index schema number must stay above the builds branch's number; a clash makes one side skip its rebuild.

### Phase 6 — The simplify-lens and assign-launcher modules run faster  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** `direct/test_simplify_lens.py` (median 132 s, 61 s CPU) runs in at most half its baseline wall time, and the assign launcher group records its session once per class (F025), with no CPU increase and no lost coverage.

**Spec:** apply the speed method (Delegation Context). Each writer owns one module group.
- `agents/test_codex_mesh.py` left this phase: codex-mesh retires, so the builds showrunner stopped new mesh test work (2026-11-04). Do not edit it.
- `direct/test_simplify_lens.py`: `scripts/direct/wait_for_test_state.py` already waits on folder events and process descriptors (37). Find the cost from the timings.
- F025: each assign launcher test runs the recorder setup from the start (about 150 git starts a run). Record the session once per class and copy it per test. Do it in `assign_launcher_test_support.py`; do not edit `test_codex_mesh.py`.
- F025 isolation: the parent fixture makes a new root and server for each test, and the recorder state holds absolute history and project paths (`progress_history.py:1358`). Restore the session, history and project together for each test, with private paths and a fresh server identity. Add a test that proves a recorder write in one test cannot reach the class seed or another test.
- Type: `AssignLauncherFixture.launch()` and `successful()` take `script: Path | None` (`assign_launcher_test_support.py:119`, `:139`); absence means in-process and presence means a separate process. Replace it with named execution choices (for example `InProcessAssignment | LauncherScript`). Keep the existing process tests.
- Measure CPU with the sequential method from phase 5: run the before and after trees one after the other on an idle host, never side by side.

**Files:**
- `scripts/direct/test_simplify_lens.py` — speed edits
- `scripts/direct/wait_for_test_state.py` — read; edit only under speed method step 4
- `scripts/direct/assign_launcher_test_support.py` — F025 per-class recording
- `scripts/direct/test_assign_launcher*.py` — fixture use only

**Workers:** 2 writers — one module group each; there is no new test to write before the code
- `impl` — `scripts/direct/assign_launcher_test_support.py`, `scripts/direct/test_assign_launcher*.py`
- `test` opens as impl — `scripts/direct/test_simplify_lens.py`; hub: `scripts/direct/wait_for_test_state.py`

**State:** none

**Constraints from prior phases:**
- Phase 5: the assign launcher tests are four files (`test_assign_launcher.py`, `_identity`, `_reuse`, `_review`) with one support module, `assign_launcher_test_support.py`. Each still imports `scripts.agents.test_codex_mesh` as a fixture, so the gate run includes it.
- Phase 5: the SSH target tests are five files with `verify_ssh_targets_test_support.py`; they are not in this phase.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/direct -p 'test_simplify_lens.py'` and `-p 'test_assign_launcher*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- Each module's (or group's) after median `wall_s` is at most half its before median, or the phase report names the measured floor and the proposal sent; its `cpu_s` median is not higher
- The checkpoint gate is green

### Phase 7 — The fast-track module and the next live slow module run in half their time  · status: todo
**Depends on:** none (its two modules share no file with Phase 6; each baseline is its own gate median and an idle-host run, so Phase 6 need not land first)

#### Work Order

**Goal:** `production/test_fast_track.py` (median 296 s of its last 5 gate runs, 188 s CPU) and `style/test_crate_run.py` (median 178 s, 63 s CPU; the slowest live module of `test_times top` that no phase owns, 2026-11-04) each run in at most half their baseline wall time, with no CPU increase and no lost coverage.

**Spec:** apply the speed method (Delegation Context) to each module. Each writer owns one module. `MergeCheckpointFixture.setUpClass` (`scripts/production/merge_checkpoint_test_support.py:262`) already builds one seed for each class and copies it for each test. Find the cost from the timings.

**Files:**
- `scripts/production/test_fast_track.py` — speed edits
- `scripts/style/test_crate_run.py` — speed edits (the second module)
- `scripts/production/merge_checkpoint_test_support.py` — read; edit only under speed method step 4

**Workers:** 2 writers — one module each; there is no new test to write before the code
- `impl` — `scripts/production/test_fast_track.py`; hub: `scripts/production/merge_checkpoint_test_support.py`
- `test` opens as impl — `scripts/style/test_crate_run.py`

**State:** none

**Constraints from prior phases:**
- Phase 5: the speed phases run one after another, so each baseline is measured on the tree the last one left.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_fast_track.py'` and the second module green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- Each module's (or group's) after median `wall_s` is at most half its before median, or the phase report names the measured floor and the proposal sent; its `cpu_s` median is not higher
- The checkpoint gate is green, with the importers of each changed support file in it

### Phase 8 — The test history tool starts without the merge gate module  · status: done

#### As-built

- `scripts/production/gate_modules.py` holds `test_module_name`, `discover_test_modules` and the gate refusal error `GateRefused`. Discovery uses `os.walk`, skips `__pycache__`, and sorts on the path parts, so `merge_gate` keeps its module order. `merge_gate.py`, `show.py` and every `test_merge_gate*.py` module import these names from `gate_modules`.
- `show.py` does not import `merge_gate`, so each `/test_times` command starts about 0.4 s faster.
- `index.GATE_TABLES` is the tuple of gate tables, taken from the `TABLES` keys. `show.py` probes them after `index.update()` and `read_only()`. A missing gate table gives one line that ends "run `buildlog reindex`", exit 1. After the probe, a report query error keeps its traceback.
- `index.update()` makes no `meta` root write when the stored root is the same.

**Files:**
- `scripts/production/gate_modules.py`, `scripts/production/merge_gate.py` — discovery and `GateRefused`
- `scripts/test_times/show.py`, `scripts/test_times/test_show.py` — the light import and the missing-table line
- `scripts/buildlog/index.py`, `scripts/buildlog/test_gate_index.py` — `GATE_TABLES` and the no-write update
- `scripts/production/test_merge_gate*.py` — imports from `gate_modules`

**Gotchas:**
- basedpyright reports a name reached through another module's import as private (`reportPrivateLocalImportUsage`). A moved name moves in every importer in the same phase.

### Phase 9 — Memory admission and the disk sweeper read the build log through the locked reader  · status: done

#### As-built

- `memory_admit.anonymous_history_rows(path)` returns `store.locked_rows(path)`. `sweep.build_log_rows`, `sweep.trim_row_recorded` and `sweep.read_sweep_removals` read through `store.locked_rows`. A month file that the rebuild enumerated and then cannot open still fails the rebuild report.
- `memory_admit.py` imports the buildlog modules under `TYPE_CHECKING`, like `sweep.py`; the hand-copied store protocols are gone. `admission_service.py`, `admission_client.py` and their tests import `store` the same way.
- `memory_admit.current_worktree_steps` keeps its own locked read: it needs the handle under the same lock for `os.fstat` and the file marks, and it raises on a partial tail.
- `queue_lines.py` and `queue_view.py` pad through `scripts/lib/terminal_cells.py`; the padding cases are in `test_terminal_cells.py`.

**Files:**
- `scripts/lint/memory_admit.py`, `scripts/lint/sweep.py` and their tests — the locked reads
- `scripts/build_queue/admission_service.py`, `admission_client.py`, `admission_test_support.py`, `queue_lines.py`, `queue_view.py` and their tests
- `scripts/lib/test_terminal_cells.py` — the padding cases

**Gotchas:**
- Reading `cgroup.procs` of a cgroup the kernel is removing gives ENODEV, not `FileNotFoundError`. `reservation_lifetime` catches only `FileNotFoundError`; resources-unit owns the fix.

### Phase 10 — Each schema keeps its own build log index file  · status: done

#### As-built

- The index file is `index-<sha256>.sqlite` in the store root. The hash covers `SCHEMA_VERSION` and every schema statement (tables, columns, indexes), so a tree with another schema opens its own file and rebuilds nothing of a peer's.
- `index.index_path()` names the file in `store.root()`; `index.index_path_in(root)` names it in a given root; `index.read_only_uri(root)` gives the quoted read-only URI. Every reader calls one of these. `store.INDEX_NAME` is gone.
- `index.update()` rebuilds when any `TABLES` name is missing, and sets the current file's mtime when it is more than one day old. `cleanup_unused_indexes` deletes another schema's file only when `max(st_atime, st_mtime)` is older than `INDEX_UNUSED_DAYS` from `config/buildlog.conf` (30).
- `store.locked_rows` reads the bytes under the shared lock, releases it, then decodes and yields the rows.
- A dropped gate table is rebuilt by `update()`. `show.py` keeps the one-line missing-table diagnostic for a table still missing after `update()`.

**Files:**
- `scripts/buildlog/index.py`, `scripts/buildlog/store.py`, `config/buildlog.conf` — file name, rebuild, cleanup, lock span
- `scripts/lint/memory_admit.py`, `scripts/buildlog/width_trial.py`, `scripts/hierarchies/measure.py`, `scripts/test_times/show.py` — readers
- `scripts/buildlog/test_index.py`, `test_gate_index.py`, `test_store.py`, `scripts/test_times/test_show.py` and the tests that build the index path

**Binds later work:**
- No code names `index.sqlite`; a new reader calls `index.index_path()` or `index.index_path_in(root)`.

**Gotchas:**
- `hashlib` and the schema statements add about 2 ms to each CLI start that imports `index`.

**Ruled out:**
- Cost indexes outside the hash: a changed cost index is a schema change and gets its own file.

### Phase 11 — Shared test fixtures skip repeated setup  · status: todo
**Depends on:** Phase 6

#### Work Order

**Goal:** a test with a fresh HOME skips the `lib/py` interpreter probe, and `NixFixture` stops copying the whole `config` folder for each test.

**Spec:**

1. Add one shared helper in `scripts/lib/py_floor_test_support.py` that sets `XDG_CACHE_HOME` to a folder for the test and writes the interpreter cache file there with `cache_file_name()` (`:26`). `recorder_fixture.launcher_environment` (`scripts/direct/recorder_fixture.py:198-208`) and `test_simplify_lens.py` use it; the copy in `recorder_fixture.py` goes. Estimate: 20-40 ms for each test that makes a fresh HOME.
2. `NixFixture.setUp` (`scripts/lib/nix_env_test_support.py:50`) copies the `config` folder (55 files, 288 KB) once per class, or copies only the files the tests read. Each test keeps its own writable copy of any file it changes.
3. Measure each changed importer module before and after with `scripts/test_times/module_run.py`, three runs each.

**Files:**
- `scripts/lib/py_floor_test_support.py`, `scripts/lib/nix_env_test_support.py` — helpers
- `scripts/direct/recorder_fixture.py`, `scripts/direct/test_simplify_lens.py` — callers
- every module that imports `nix_env_test_support.py` — run in the gate

**Workers:** 2 writers — one helper each
- `impl` — `py_floor_test_support.py`, `recorder_fixture.py`, `test_simplify_lens.py`
- `test` opens as impl — `nix_env_test_support.py`

**State:** none

**Constraints from prior phases:**
- Phase 6: `test_simplify_lens.py` seeds one git repository per class. `setUp()` still records `start-run` and `start-phase simplify` for each test, except the ad hoc test, where the assignment makes the phase.
- Ledger routes: Phase 6 F018, F019. Phase 6 F017 (the `assign.py` exec call) belongs to sdlc devtools, not this phase.

**Acceptance gate:**
- every importer of the two helpers green in the checkpoint gate run
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- each changed importer's after median `cpu_s` is not higher than its before median
