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
  - `scripts/production/merge_gate.py` — `MODULE_HISTORY_RUNS = 5` (101); `SUITE_BOOTSTRAP` (106, execs its argv); `GateJobResult` (157, has `cpu_seconds`); `gate_state_dir()` (500); `start_gate_job` (615); `test_module_environment` (632, gives each module a private `TMPDIR`, `BUILDLOG_DIR` and tool tripwires); `finish_gate_job` (655); `PassedTreeLedger` (713, `CleanTestTree.tree` and `.commit`); `discover_test_modules` (841); `run_suites` (855): the module argv at 975-978 is `[*scope_prefix, python, "-m", "unittest", "discover", "-s", str(path.parent), "-p", path.name]`, the type job argv is at 975, the rerun-alone pass keeps first results in `first_runs`, and the `finally` block (1045-1062) saves `module_seconds.json`, `module_cpu_seconds.json`, `module_peak_mib.json`, `types_peak_mib.json` and `module_history.json`; the summary lines (1065-1100) print each folder suite's time from `suite_started` and `suite_finished`
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — the gate's fixture and tests
  - `scripts/buildlog/store.py` — `root()` (honors `BUILDLOG_DIR`), `host_name()`, `utc_iso(epoch)`, `host_file(host, epoch)`, `append_line(path, record)`: the JSON-lines store, one file per host per month
  - `scripts/buildlog/record.py` — `RECORD_VERSION`, `new_id(host, epoch)` (513), `ssh_attempt` (1051): the pattern for a one-line record writer
  - `scripts/buildlog/index.py` — `SCHEMA_VERSION = 28` (35); column lists such as `TEST_COLUMNS` (121) and `CALL_COLUMNS`; `TABLES` (363); `create_schema` (473, adds a `<table>_src` index for each table); `insert`, `pick`; `add_step` (650, the pattern for a nested list that becomes child rows); `ADDERS` (792); `forget` (796, loops `TABLES`); `update()` (854); `read_only()` (893)
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
  1. Baseline: before the edit, run the module alone three times with `TEST_TIMES_OUT=<run directory>/<module>-before-<n>-<sha>.json python3 scripts/test_times/module_run.py scripts/<folder> <file>` and keep the median `wall_s` and `cpu_s`. Read `python3 scripts/test_times/test_times.py module <folder>/<file>` for the gate history.
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

#### Work Order

**Goal:** `scripts/test_times/module_run.py` runs one test module exactly like `python -m unittest discover` and writes each test's time, and `scripts/test_times/gate_record.py` turns a gate run into one `gate_run` line in the build log.

**Spec:**

1. `scripts/test_times/module_run.py <start_dir> <pattern>`:
   - Use the standard library only. Replace `sys.path[0]` (the script's own folder) with `os.getcwd()` before any import of test code, so imports resolve as under `python -m unittest` from the same folder. The gate runs main's copy of this file by its absolute path (Phase 2), so the file must work from outside the tree under test.
   - Run `unittest.main(module=None, argv=["python -m unittest", "discover", "-s", start_dir, "-p", pattern], testRunner=TimedRunner)`. `TestProgram` keeps its own warning setup and exit codes (Python 3.13: 1 on a failure, then 5 when no test ran, else 0), so the output and the status match `python -m unittest discover` apart from the time line.
   - `TimedRunner(unittest.TextTestRunner)` sets `resultclass = TimedResult` and writes the `TEST_TIMES_OUT` file in `run()` after `super().run()` returns.
   - `TimedResult(unittest.TextTestResult)` overrides `addDuration(test, elapsed)` (called for each test that runs, with setUp, tearDown and cleanups included), keeps `(test.id(), elapsed)` and calls `super().addDuration`.
   - Keep one outcome for each parent test id. A subtest failure or error maps to `subtest.test_case.id()`. Precedence: `error` > `fail` > `unexpected_success` > `expected_failure` > `skip` > `pass`. A skipped test has no duration: its `s` is `0.0`. A class or module fixture failure (an `_ErrorHolder`, such as `setUpClass (module.Class)`) gets its own entry with its description as `test`, status `error` and `s` `0.0`.
   - When the environment has `TEST_TIMES_OUT`, write that path once, after the run, with `os.replace` from a temporary file in the same folder:
     ```json
     {"wall_s": 12.3, "cpu_s": 10.1, "tests_s": 11.0, "tests_run": 51, "failures": 0, "errors": 0, "skipped": 2,
      "tests": [{"test": "AssignLauncherTests.test_x", "s": 1.234, "status": "pass"}]}
     ```
     `wall_s` is the time from the script's start to the end of the run. `cpu_s` is user plus system time of the process and its reaped children (`resource.getrusage` with `RUSAGE_SELF` and `RUSAGE_CHILDREN`). `tests_s` is the sum of all test times. `test` is `test.id()` without its first dotted part (the module name). `status` is one of `pass`, `fail`, `error`, `skip`, `expected_failure`, `unexpected_success`, taken from the result's lists. A test that has subtests gets one entry. A failure to write the file prints `test times not written: <error>` to stderr and does not change the exit status.
   - Remove `TEST_TIMES_OUT` from `os.environ` before the tests run, so a test that starts this runner again does not overwrite the file.
2. `scripts/test_times/gate_record.py`:
   - TypedDicts: `TestTime {test: str, s: float, status: str}`; `ModuleTimes {wall_s, cpu_s, tests_s: float; tests_run, failures, errors, skipped: int; tests: list[TestTime]}`; `ModuleRun {suite, module: str; attempt: int; wall_s, cpu_s, peak_mib: float; status: int; tests_run, failures, errors, skipped: int; tests_s: float; tests: list[TestTime]; restart: str (empty, or `hold`); times_missing: str (empty, or the `NoModuleTimes` reason)}`; `SuiteRun {suite: str, wall_s: float, status: int}`; `GateRun {started_at, ended_at: str; wall_s, hold_wait_s: float; selection: str; reason: str; status: int; max_jobs: int; peak_mib, cap_mib: float; checkout, branch, sha, tree, source: str; aborted, dirty: bool; suites: list[SuiteRun]; modules: list[ModuleRun]}`. `selection` is `full`, `affected` or `reused`. `source` is `tests-only` or `checkpoint`.
   - `read_module_times(path: Path) -> ModuleTimes | NoModuleTimes`: parse and type-check the runner's file. `NoModuleTimes` (a frozen dataclass with a `reason: str`) when the file is missing or invalid, for example when the module died before the runner wrote it.
   - `kept_tests(times: ModuleTimes, slow_test_s: float) -> list[TestTime]`: every test whose status is `fail`, `error` or `unexpected_success`, and every other test with `s >= slow_test_s`, slowest first.
   - `slow_test_s() -> float`: `conf.read("test_times").number("SLOW_TEST_S")`, read at each call.
   - `write_gate_run(run: GateRun) -> None`: append `{"kind": "gate_run", "v": record.RECORD_VERSION, "id": record.new_id(host, epoch), "host": store.host_name(), **run}` with `store.append_line(store.host_file(host, epoch), ...)`, where `epoch` is now. Reuse `scripts/buildlog/store.py` and `scripts/buildlog/record.py`; add nothing to them.
3. `config/test_times.conf`, new:
   ```
   # Keep a passing test's time in the build log when the test takes at least this many seconds.
   # Basis: 17,423 tests in 664 modules on 2026-11-03; the gate's module times sum to about 5,000 s, a mean of 0.3 s for each test.
   # range: 0..
   SLOW_TEST_S=0.5
   # Rank a module by the median wall time of its last this many recorded gate runs.
   # whole: 1..
   TOP_RUNS=5
   ```
   Add one `config/README.md` entry for it, in the form of the entries there.

**Files:**
- `scripts/test_times/module_run.py` — new: the runner
- `scripts/test_times/gate_record.py` — new: the record types, the readers and the writer
- `config/test_times.conf` — new: `SLOW_TEST_S`, `TOP_RUNS`
- `config/README.md` — one entry for `test_times.conf`
- `scripts/test_times/test_module_run.py` — new: runner tests
- `scripts/test_times/test_gate_record.py` — new: record tests

**Workers:** 1 writer + 1 tester — the code and the tests split by file
- `impl` — `scripts/test_times/module_run.py`, `scripts/test_times/gate_record.py`, `config/test_times.conf`; hub: `config/README.md` (the one config index)
- `test` — `scripts/test_times/test_module_run.py`: run the runner as a subprocess on fixture test modules in a temporary folder (pass, fail, error, skip, subtests with a failure, no tests, a `setUpClass` failure, a test that emits a `DeprecationWarning`, a module that imports from the repository root) and check the exit codes, the stderr text against `python -m unittest discover` on the same fixture, and the JSON file; `scripts/test_times/test_gate_record.py`: `read_module_times` on valid, missing and invalid files, `kept_tests` at the threshold, and `write_gate_run` into a `BUILDLOG_DIR` fixture with a `CONF_DIR` fixture for `SLOW_TEST_S`

**State:** one appended JSON line for each `write_gate_run` call, in the build log month file of the host.

**Constraints from prior phases:** none

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/test_times -p 'test_*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright scripts/test_times` prints `0 errors, 0 warnings, 0 notes`
- On one real module, `python3 scripts/test_times/module_run.py scripts/lib test_conf.py` and `python3 -m unittest discover -s scripts/lib -p test_conf.py` give the same exit status and the same test count

### Phase 2 — The merge gate records every run  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** each merge gate run appends one `gate_run` line with each folder suite, each module run and each kept test, and the gate's output and verdicts do not change.

**Spec:**

1. In `scripts/production/merge_gate.py`, run each test module with the gate's own copy of the runner: `RUNNER = Path(__file__).resolve().parents[1] / "test_times" / "module_run.py"`, argv `[*scope_prefix, python, str(RUNNER), str(path.parent), path.name]` in place of the `-m unittest discover` argv. A tree that does not have the runner still gates, because the file comes from the gate's checkout.
2. `test_module_environment` adds `TEST_TIMES_OUT=<module root>/test-times.json`.
3. `finish_gate_job` reads the file with `gate_record.read_module_times` and puts the result on a new `GateJobResult.times: ModuleTimes | NoModuleTimes` field. It copies `job.peak_memory_mib` to a new `GateJobResult.peak_mib`.
4. Keep an `executions: list[ModuleRun]` in `run_suites`. Append one entry each time a job finishes, before any requeue, so a job stopped by a natedev hold (`merge_gate.py:1008`) also gets its entry, with `restart: "hold"`. `attempt` counts the module's entries from 1. A test module entry takes `tests` from `gate_record.kept_tests(times, gate_record.slow_test_s())`, and `times_missing` from `NoModuleTimes.reason` (empty when the times were read). The type job is suite `types`, module `basedpyright`, with no tests; a reused run keeps it when basedpyright ran.
5. Write the record in the `finally` block, after the measure files are saved, with `gate_record.write_gate_run`. Catch `OSError` and `ValueError`, print `Test times not recorded: <error>` and go on. Never change the exception or the verdict. An exception that leaves the block writes `aborted: true`.
   - `status`: 0 when `run.passed`, else 1 (a red from the memory cap counts); an aborted run writes 1.
   - `selection`: `reused`, `affected` or `full`; `reason` is the full-suite reason or empty.
   - `suites`: one `SuiteRun` for each folder suite that ran, with `suite_finished - suite_started`, and status 1 when the last attempt of one of its modules failed.
   - `wall_s` is the printed `Gate tests` time (hold wait excluded); `hold_wait_s` is the hold wait of this run.
   - `checkout` is the repository path; `branch` comes from `git rev-parse --abbrev-ref HEAD`; `sha` is the HEAD commit; `tree` is `CleanTestTree.tree`, or empty with `dirty: true` for an `UnrecordableTestTree`; `source` is the ledger's value.
6. `merge_gate_test_support.py` sets `BUILDLOG_DIR` to the fixture's temporary folder in every gate run it starts, so no test writes the live build log.
7. Keep `module_seconds.json`, `module_cpu_seconds.json`, `module_history.json` and the peak files as they are: the gate's start order and memory admission read them. Their retirement, if any, goes to devtools-unit as a proposal.

**Files:**
- `scripts/production/merge_gate.py` — runner argv, `TEST_TIMES_OUT`, `GateJobResult.times`, the `gate_run` write
- `scripts/production/merge_gate_test_support.py` — `BUILDLOG_DIR` in every fixture run
- `scripts/production/test_merge_gate.py` — its argv checks (154 reads `-s`; 420 asserts `discover`) follow the runner argv
- `scripts/production/test_merge_gate_test_times.py` — new: the gate's record tests

**Workers:** 1 writer + 1 tester — the gate code and the new test file split by file
- `impl` — `scripts/production/merge_gate.py`; hub: `scripts/production/merge_gate_test_support.py` (every gate test reads it)
- `test` — `scripts/production/test_merge_gate_test_times.py`: with the existing fixture, a gate run with one passing, one failing and one slow-test module writes one `gate_run` line whose suites, modules (attempt 1 and the rerun attempt 2) and kept tests match; a module that dies before the runner writes gives a module row with no tests; an unwritable `BUILDLOG_DIR` prints `Test times not recorded:` and the gate's verdict is unchanged; a reused run writes `selection: reused` with only the types row; a job stopped by a hold gets its own entry and its requeued run the next attempt; a red from the memory cap with every module green writes status 1; `scripts/production/test_merge_gate.py` (154, 420) checks the runner argv in place of `discover`

**State:** one appended `gate_run` line in the build log for each gate run.

**Constraints from prior phases:**
- Phase 1: `scripts/test_times/module_run.py` keeps `python -m unittest discover` output, exit codes and imports; it reads `TEST_TIMES_OUT` and removes it from the environment of the tests. `gate_record.py` gives `read_module_times`, `kept_tests`, `slow_test_s` and `write_gate_run`.
- The owner of `merge_gate.py` is sdlc devtools-unit (builds showrunner, 2026-11-03). Before the edit lands, send the showrunner the diff; the showrunner gets devtools' agreement through sdlc. devtools fixes a basedpyright exit-3 defect in `merge_gate.py` (due 15:00 EST, 2026-11-03): merge its tip first and test against it. The checkpoint notice says `also touches scripts/production/merge_gate.py (owner devtools-unit), tested against <devtools tip>, clean`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright scripts/production/merge_gate.py scripts/production/merge_gate_test_support.py scripts/production/test_merge_gate_test_times.py` prints `0 errors, 0 warnings, 0 notes`
- The checkpoint gate, run with this tree's `merge_gate.py`, prints the same suite lines as main's copy and appends one `gate_run` line to the build log

### Phase 3 — The build log indexes gate runs  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** `buildlog`'s index turns each `gate_run` line into rows of four new tables that answer suite, module and test history queries from their indexes.

**Spec:**

1. In `scripts/buildlog/index.py`, add four column lists in the form of `TEST_COLUMNS`, each with a `src` column, and add them to `TABLES`:
   - `gate_runs` — `id TEXT PRIMARY KEY`, `src`, `host`, `started_at`, `ended_at`, `wall_s REAL`, `hold_wait_s REAL`, `selection`, `reason`, `status INTEGER`, `max_jobs INTEGER`, `peak_mib REAL`, `cap_mib REAL`, `checkout`, `branch`, `sha`, `tree`, `source`, `aborted INTEGER`, `dirty INTEGER`, `modules_run INTEGER` (distinct test modules; the type job excluded), `tests_run INTEGER` (sum over the last attempt of each test module). Description: `one row per merge-gate test run (scripts/production/merge_gate.py)`.
   - `gate_suites` — `run_id`, `src`, `host`, `started_at`, `suite`, `wall_s REAL`, `status INTEGER`; `PRIMARY KEY (run_id, suite)`.
   - `gate_modules` — `run_id`, `src`, `host`, `started_at`, `suite`, `module`, `attempt INTEGER` (counts from 1: a hold restart and the rerun alone each add one), `restart`, `times_missing`, `wall_s REAL`, `cpu_s REAL`, `peak_mib REAL`, `status INTEGER`, `tests_run INTEGER`, `failures INTEGER`, `errors INTEGER`, `skipped INTEGER`, `tests_s REAL`; `PRIMARY KEY (run_id, suite, module, attempt)`. The type job is the row with suite `types` and module `basedpyright`.
   - `gate_tests` — `run_id`, `src`, `host`, `started_at`, `suite`, `module`, `attempt INTEGER`, `test`, `duration_s REAL`, `status`; `PRIMARY KEY (run_id, suite, module, attempt, test)`. Description: `per-test rows of a gate module, kept only for tests that failed or took at least SLOW_TEST_S (config/test_times.conf)`.
2. Indexes in `create_schema`: `gate_runs_started ON gate_runs (started_at)`, `gate_suites_suite ON gate_suites (suite, started_at)`, `gate_modules_key ON gate_modules (suite, module, started_at)`, `gate_tests_key ON gate_tests (suite, module, test, started_at)`, `gate_tests_test ON gate_tests (test, started_at)`.
3. `add_gate_run(connection, record, src)`: delete the child rows of the run id, then insert the `gate_runs` row and one child row for each suite, module and kept test, in the form of `add_step`. A malformed nested entry (not a dict) is skipped, as `add_step` skips one. Register it as `ADDERS["gate_run"]`.
4. Raise `SCHEMA_VERSION` by one. `open_for_update` (501) deletes the whole index on a version change, and the next `update()` rebuilds it from the JSON lines (1.29 GB and 436 files on 2026-11-03). `index.py` belongs to report-unit: claim it through berth before the edit. Agree the landing time of the bump through the showrunner (report-unit Phase 5 is in flight, 2026-11-03), so an older checkout does not rebuild it again. Measure one rebuild on a copy of the live build log (`BUILDLOG_DIR=<copy>`) and report its time.

**Files:**
- `scripts/buildlog/index.py` — the four tables, their indexes, `add_gate_run`, `ADDERS`, `SCHEMA_VERSION`
- `scripts/buildlog/test_gate_index.py` — new: index tests
- `scripts/buildlog/test_index.py` — its schema version check (635) follows the new version

**Workers:** 1 writer + 1 tester — the index code and its tests split by file
- `impl` — `scripts/buildlog/index.py`
- `test` — `scripts/buildlog/test_gate_index.py`: write `gate_run` lines with `gate_record.write_gate_run` into a `BUILDLOG_DIR` fixture, run `index.update()`, and check the rows of each table; a second `update()` adds no rows; `forget` of the source removes them; a nested entry that is not a dict is skipped; a suite, module and test query over 500 fixture gate runs returns in under 1 s; `scripts/buildlog/test_index.py` (635) expects the new version

**State:** the build log index (`index.sqlite`) is rebuilt at its first update after the version change.

**Constraints from prior phases:**
- Phase 1: `gate_run` lines carry `kind`, `v`, `id`, `host` and the `GateRun` fields of `scripts/test_times/gate_record.py`; `modules[].tests` holds only the kept tests, each `{test, s, status}`.
- Phase 2: the gate writes one `ModuleRun` for each job execution; `attempt` counts from 1; the type job is suite `types`, module `basedpyright`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/buildlog -p 'test_gate_index.py'` and `-p 'test_index*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright scripts/buildlog/index.py scripts/buildlog/test_gate_index.py` prints `0 errors, 0 warnings, 0 notes`

### Phase 4 — `/test_times` shows suite, module and test history  · status: todo
**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** `/test_times` prints in under one second the descending sort of test modules, the history of gate runs and folder suites, and the history of one module or one test.

**Spec:**

1. `scripts/test_times/test_times.py`, with `argparse` subcommands. It reaches the build log as `scripts/buildlog/cli.py` does: `index.py` imports `store` and `parse` by bare name, so `scripts/buildlog` must be on `sys.path`. Only rows of the current host (`store.host_name()`) count. Each subcommand first runs `index.update()` and then queries `index.read_only()`:
   - `top [--limit N] [--repo PATH]` (the default with no subcommand; `N` defaults to 20; `PATH` to `~/.claude`): the descending sort. Rank each live module by the median `wall_s` of its last `TOP_RUNS` attempt-1 rows in `gate_modules` (`conf.read("test_times").whole("TOP_RUNS")`). A module is live when `discover_test_modules(PATH)` (imported from `scripts/production/merge_gate.py`) lists it; the `types/basedpyright` row is always live. Columns: rank, `suite/module`, median s, last s, median CPU s, tests run, slowest kept test of the last run with its seconds, runs counted. Last line: `<count> modules; sum of medians <s> s`.
   - `suites [--days N]` (default 7): gate runs, newest first: local start time, host, selection, modules run, `wall_s`, `PASS` or `RED`, the slowest module of the run.
   - `suite <folder> [--days N]`: that folder suite's `wall_s` in each run, newest first.
   - `module <folder>/<file> [--runs N]` (default 20): each run of the module: local time, attempt, `wall_s`, `cpu_s`, `peak_mib`, status, `tests_s`, `wall_s - tests_s` (import and fixture time), the 3 slowest kept tests.
   - `test <text> [--runs N]` (default 20): kept rows whose test name contains `<text>`: local time, `suite/module`, test, `duration_s`, status. A later run of the same module with no kept row for the test shows `< <SLOW_TEST_S> s`: the index keeps a passing test only at or above `SLOW_TEST_S`.
   - Times show in the user zone (`astimezone(user_zone())`), in the form `11-03 09:30`. Column widths come from `terminal_cells.py`. An empty result prints `no gate runs recorded yet` (or the subcommand's subject) and exits 0.
2. `commands/test_times.md`, in the form of `commands/build/status.md`: run `python3 ~/.claude/scripts/test_times/test_times.py $ARGUMENTS` and show its lines unchanged; list the subcommands in one line each.

**Files:**
- `scripts/test_times/test_times.py` — new: the query tool
- `commands/test_times.md` — new: the `/test_times` command
- `scripts/test_times/test_test_times.py` — new: query tool tests

**Workers:** 1 writer + 1 tester — the tool and its tests split by file
- `impl` — `scripts/test_times/test_times.py`, `commands/test_times.md`
- `test` — `scripts/test_times/test_test_times.py`: write `gate_run` lines with `write_gate_run` into a `BUILDLOG_DIR` fixture (a `CONF_DIR` fixture sets `TOP_RUNS`), run each subcommand in-process, and check the sort order by median, the host filter, the live-module filter against a fixture repository, the attempt-1 rule, the below-threshold line, the user-zone time form, and the empty outputs; one test runs the CLI in a fresh subprocess

**State:** none (reads only; `index.update()` appends to the index as every build log reader does).

**Constraints from prior phases:**
- Phase 1: `config/test_times.conf` holds `TOP_RUNS`; `gate_record.write_gate_run` writes fixture lines.
- Phase 3: tables `gate_runs`, `gate_suites`, `gate_modules` (`attempt` counts from 1; the type job is `types`/`basedpyright`) and `gate_tests`, with indexes on `(suite, module, started_at)`, `(suite, module, test, started_at)` and `(test, started_at)`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/test_times -p 'test_*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright scripts/test_times` prints `0 errors, 0 warnings, 0 notes`
- On natedev, with a warm index, `time python3 scripts/test_times/test_times.py top` finishes in under 1 s; the first query after the schema change rebuilds the index and is not timed

### Phase 5 — The two slowest direct modules run in half their time  · status: todo
**Depends on:** Phase 2, Phase 4

#### Work Order

**Goal:** `direct/test_assign_launcher.py` (median 180 s of its last 5 gate runs on 2026-11-03, 127 s CPU in the last) and `direct/test_verify_ssh_targets.py` (median 157 s, 117 s CPU) each run in at most half their baseline wall time, with no CPU increase and no lost coverage.

**Spec:** apply the speed method (Delegation Context) to each module. Each writer owns one module.

**Files:**
- `scripts/direct/test_assign_launcher.py` — speed edits
- `scripts/direct/test_verify_ssh_targets.py` — speed edits; a split adds `scripts/direct/test_verify_ssh_targets_<part>.py` files
- `scripts/direct/recorder_fixture.py` — read; edit only under speed method step 4
- `scripts/direct/verify_mac_offload_test_support.py` — read; edit only under speed method step 4

**Workers:** 2 writers — one module each; there is no new test to write before the code
- `impl` — `scripts/direct/test_assign_launcher.py`; hub: `scripts/direct/recorder_fixture.py`
- `test` opens as impl — `scripts/direct/test_verify_ssh_targets.py` and its split files; hub: `scripts/direct/verify_mac_offload_test_support.py`

**State:** none

**Constraints from prior phases:**
- Phase 1: `scripts/test_times/module_run.py` writes `wall_s`, `cpu_s`, `tests_s` and each test's `s` to `TEST_TIMES_OUT`.
- Phase 4: `test_times.py module <folder>/<file>` shows the module's gate history.
- `test_assign_launcher.py` imports `scripts.agents.test_codex_mesh` as a fixture; Phase 6 changes that module, so keep this import's use unchanged.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/direct -p 'test_assign_launcher.py'` and `-p 'test_verify_ssh_targets*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- Each module's (or group's) after median `wall_s` is at most half its before median, or the phase report names the measured floor and the proposal sent; its `cpu_s` median is not higher
- The checkpoint gate is green, with the importers of each changed support file in it

### Phase 6 — The mesh and simplify-lens modules run in half their time  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** `agents/test_codex_mesh.py` (median 137 s of its last 5 gate runs, one run at 947 s, 78 s CPU in the last) and `direct/test_simplify_lens.py` (median 132 s, 61 s CPU) each run in at most half their baseline wall time, with no CPU increase and no lost coverage.

**Spec:** apply the speed method (Delegation Context) to each module. Each writer owns one module.
- `agents/test_codex_mesh.py` is 18,835 lines and 720 tests, and herdr-unit's plan (`docs/plans/herdr-worker-panes.md`) deletes `scripts/agents/codex_mesh.py` in its cleanup phase. Its wall time is near twice its CPU time, so cut waits only: fixed sleeps, polling loops and timeouts that the 947 s run shows grow with load. Do not restructure the file. When herdr-unit's deletion of `codex_mesh.py` lands before this phase starts, this module leaves the phase and the next live module of `test_times top` takes its place.
- `direct/test_simplify_lens.py`: `scripts/direct/wait_for_test_state.py` already waits on folder events and process descriptors (37). Find the cost from the timings.

**Files:**
- `scripts/agents/test_codex_mesh.py` — wait cuts
- `scripts/direct/test_simplify_lens.py` — speed edits
- `scripts/direct/wait_for_test_state.py` — read; edit only under speed method step 4

**Workers:** 2 writers — one module each; there is no new test to write before the code
- `impl` — `scripts/agents/test_codex_mesh.py`
- `test` opens as impl — `scripts/direct/test_simplify_lens.py`; hub: `scripts/direct/wait_for_test_state.py`

**State:** none

**Constraints from prior phases:**
- Phase 5: `scripts/direct/test_assign_launcher.py` imports `scripts.agents.test_codex_mesh` as a fixture, so the final gate run includes it.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/agents -p 'test_codex_mesh.py'`, `python3 -m unittest discover -s scripts/direct -p 'test_simplify_lens.py'` and `-p 'test_assign_launcher.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- Each module's (or group's) after median `wall_s` is at most half its before median, or the phase report names the measured floor and the proposal sent; its `cpu_s` median is not higher
- The checkpoint gate is green

### Phase 7 — The fast-track and lint-guard-key modules run in half their time  · status: todo
**Depends on:** Phase 6

#### Work Order

**Goal:** `production/test_fast_track.py` (median 127 s of its last 5 gate runs, 85 s CPU in the last) and `direct/test_verify_lint_guard_key.py` (median 107 s, 63 s CPU) each run in at most half their baseline wall time, with no CPU increase and no lost coverage.

**Spec:** apply the speed method (Delegation Context) to each module. Each writer owns one module. `MergeCheckpointFixture.setUpClass` (`scripts/production/merge_checkpoint_test_support.py:262`) already builds one seed for each class and copies it for each test. Find the cost from the timings.

**Files:**
- `scripts/production/test_fast_track.py` — speed edits
- `scripts/direct/test_verify_lint_guard_key.py` — speed edits
- `scripts/production/merge_checkpoint_test_support.py` — read; edit only under speed method step 4

**Workers:** 2 writers — one module each; there is no new test to write before the code
- `impl` — `scripts/production/test_fast_track.py`; hub: `scripts/production/merge_checkpoint_test_support.py`
- `test` opens as impl — `scripts/direct/test_verify_lint_guard_key.py`

**State:** none

**Constraints from prior phases:**
- Phase 5: the speed phases run one after another, so each baseline is measured on the tree the last one left.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_fast_track.py'` and `python3 -m unittest discover -s scripts/direct -p 'test_verify_lint_guard_key.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`
- Each module's (or group's) after median `wall_s` is at most half its before median, or the phase report names the measured floor and the proposal sent; its `cpu_s` median is not higher
- The checkpoint gate is green, with the importers of each changed support file in it
