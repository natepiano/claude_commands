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
  - `scripts/production/merge_gate.py` — line numbers on builds 772ef96a9: `MODULE_HISTORY_RUNS = 5` (103); `SUITE_BOOTSTRAP` (108, execs its argv); `GateJobResult` (159, has `cpu_seconds`); `gate_state_dir()` (510); `start_gate_job` (638); `test_module_environment` (655, gives each module a private `TMPDIR`, `BUILDLOG_DIR` and tool tripwires); `finish_gate_job` (678, finishes every job, the type job included); `PassedTreeLedger` (736, `CleanTestTree.tree` and `.commit`); `discover_test_modules` (864); `run_suites` (878): the module argv at 1003 is `[*scope_prefix, python, "-m", "unittest", "discover", "-s", str(path.parent), "-p", path.name]`, the type job argv is at 1000-1001, the rerun-alone pass keeps first results in `first_runs`, and the `finally` block (1074-1088) saves `module_seconds.json`, `module_cpu_seconds.json`, `module_peak_mib.json`, `types_peak_mib.json` and `module_history.json`; the summary lines (1093-1127) print each folder suite's time from `suite_started` and `suite_finished`
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — the gate's fixture and tests
  - `scripts/buildlog/store.py` — `root()` (honors `BUILDLOG_DIR`), `host_name()`, `utc_iso(epoch)`, `host_file(host, epoch)`, `append_line(path, record)`: the JSON-lines store, one file per host per month
  - `scripts/buildlog/record.py` — `RECORD_VERSION`, `new_id(host, epoch)` (513), `ssh_attempt` (1055): the pattern for a one-line record writer
  - `scripts/buildlog/index.py` — `SCHEMA_VERSION = 29` (42); column lists such as `TEST_COLUMNS` (141) and `CALL_COLUMNS`; `TABLES` (452, with the gate tables); `create_schema` (574, adds a `<table>_src` index for each table); `insert`, `pick`; `add_step` (763, the pattern for a nested list that becomes child rows); `ADDERS` (950, `gate_run` maps to `add_gate_run`); `forget` (954, loops `TABLES`); `update()` (1012); `read_only()` (1051)
  - `scripts/test_times/gate_record.py` — the `gate_run` record types (`ModuleTimes`, `ModuleRun`, `SuiteRun`, `GateRun`, `NoModuleTimes`), `TYPE_CHECK_JOB` (38), `read_module_times` (131), `kept_tests` (153), `slow_test_s` (162), `write_gate_run` (166)
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

### Phase 2 — The merge gate records every run  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** each merge gate run appends one `gate_run` line with each folder suite, each module run and each kept test, and the gate's output and verdicts do not change.

**Spec:**

1. In `scripts/production/merge_gate.py`, run each test module with the gate's own copy of the runner: `RUNNER = Path(__file__).resolve().parents[1] / "test_times" / "module_run.py"`, argv `[*scope_prefix, python, str(RUNNER), str(path.parent), path.name]` in place of the `-m unittest discover` argv. A tree that does not have the runner still gates, because the file comes from the gate's checkout.
2. `test_module_environment` adds `TEST_TIMES_OUT=<module root>/test-times.json`.
3. `finish_gate_job` reads a test module's file with `gate_record.read_module_times` and puts the result on a new `GateJobResult.times: ModuleTimes | NoModuleTimes | TimingNotApplicable` field. `TimingNotApplicable` is a new frozen dataclass in `merge_gate.py`: the type-check job runs no tests and writes no file, so `finish_gate_job` reads no file for a `GateJobKind.TYPE_CHECK` job, and the reused types result (957) also carries `TimingNotApplicable`. A missing file is never the type job's state. `finish_gate_job` copies `job.peak_memory_mib` to a new `GateJobResult.peak_mib`.
4. Keep an `executions: list[GateJobResult]` in `run_suites`. Append each job's result when the job finishes, before any requeue, so a job stopped by a natedev hold (requeued at `merge_gate.py:1034`) also gets its entry, marked as a hold restart. The record step turns each entry into one `ModuleRun`: `attempt` counts the module's entries from 1; `restart` is `hold` or empty; `tests` is `gate_record.kept_tests(times, gate_record.slow_test_s())` for `ModuleTimes`, else empty; `times_missing` is `NoModuleTimes.reason`, empty for `ModuleTimes` and `TimingNotApplicable`. The type job is suite `types`, module `basedpyright` (`gate_record.TYPE_CHECK_JOB`), with no tests and an empty `times_missing`; a reused run keeps it when basedpyright ran.
5. One function, `record_gate_run`, builds the `GateRun` and calls `gate_record.write_gate_run`. Its build step sits in a `try`/`except Exception` that prints `gate run not written: <error>` to stderr, the line `write_gate_run` prints for a failed write. So a bad `SLOW_TEST_S` or a failed write never changes the gate's output, exception or verdict. Put no catch around `write_gate_run`: it catches every error itself.
   - Completed run: call it after `run` is built (`merge_gate.py:1128`), before `run_suites` returns, with `aborted: false`. `status` is 0 when `run.passed`, else 1 (a red from the memory cap counts).
   - Aborted run: an exception that leaves `run_suites` after the first job starts calls it from an `except BaseException:` handler that then re-raises the same exception, with `aborted: true` and `status: 1`. The record holds the executions that finished. Jobs that the cleanup kills get no entry; `aborted: true` marks the record as partial.
   - `selection`: `reused`, `affected` or `full`; `reason` is the full-suite reason or empty.
   - `suites`: one `SuiteRun` for each folder suite that ran, with `suite_finished - suite_started`, and status 1 when the last attempt of one of its modules failed.
   - `wall_s` is the printed `Gate tests` time (hold wait excluded); for an aborted run, the time from the start to the abort, hold wait excluded. `hold_wait_s` is the hold wait of this run.
   - `checkout` is the repository path; `branch` comes from `git rev-parse --abbrev-ref HEAD`; `sha` is the HEAD commit; `tree` is `CleanTestTree.tree`, or empty with `dirty: true` for an `UnrecordableTestTree`; `source` is the ledger's value.
6. The gate fixture already sets `BUILDLOG_DIR` to a private folder in every gate run (`merge_gate_test_support.py:367`), so no gate test writes the live build log. Do not change the support file.
7. Keep `module_seconds.json`, `module_cpu_seconds.json`, `module_history.json` and the peak files as they are: the gate's start order and memory admission read them. Their retirement, if any, goes to devtools-unit as a proposal.

**Files:**
- `scripts/production/merge_gate.py` — runner argv, `TEST_TIMES_OUT`, `GateJobResult.times`, `record_gate_run`
- `scripts/production/test_merge_gate.py` — its argv checks (154 reads `-s`; 420 asserts `discover`) follow the runner argv
- `scripts/production/test_merge_gate_test_times.py` — new: the gate's record tests

**Workers:** 1 writer + 1 tester — the gate code and the gate tests split by file
- `impl` — `scripts/production/merge_gate.py`
- `test` — `scripts/production/test_merge_gate_test_times.py`: with the existing fixture, a gate run with one passing, one failing and one slow-test module writes one `gate_run` line whose suites, modules (attempt 1 and the rerun attempt 2) and kept tests match; a module that dies before the runner writes gives a module row with no tests and its `times_missing` reason; the types row has no tests and an empty `times_missing`, in a full run and in a reused run; an unwritable `BUILDLOG_DIR` prints `gate run not written:` and the gate's output and verdict are unchanged; an invalid `SLOW_TEST_S` in a `CONF_DIR` fixture prints `gate run not written:` and the verdict is unchanged; an exception after the first job starts writes `aborted: true` with status 1, and the same exception leaves the gate; a reused run writes `selection: reused` with only the types row; a job stopped by a hold gets its own entry with `restart: hold`, and its requeued run the next attempt; a red from the memory cap with every module green writes status 1; `scripts/production/test_merge_gate.py` (154, 420) checks the runner argv in place of `discover`

**State:** one appended `gate_run` line in the build log for each gate run.

**Constraints from prior phases:**
- Phase 1: `scripts/test_times/module_run.py` keeps `python -m unittest discover` output, exit codes and imports. Its `main()` reads `TEST_TIMES_OUT` once and removes it from the environment before discovery imports a test module. With the wrong argument count it prints one usage line and exits 2. `gate_record.py` gives `read_module_times`, `kept_tests`, `slow_test_s` and `write_gate_run`. `write_gate_run` catches every error and prints `gate run not written: <error>` to stderr. `NoModuleTimes.reason` names the file and the error. `TestTime.status` is the `TestStatus` Literal.
- The owner of `merge_gate.py` is sdlc devtools-unit (builds showrunner, 2026-11-03). Before the edit lands, send the showrunner the diff; the showrunner gets devtools' agreement through sdlc. devtools fixes a basedpyright exit-3 defect in `merge_gate.py` (due 15:00 EST, 2026-11-03): merge its tip first and test against it. The checkpoint notice says `also touches scripts/production/merge_gate.py (owner devtools-unit), tested against <devtools tip>, clean`.
- Until the admission rebuild, a tree's own gate waits forever (builds showrunner, 2026-11-03). Gate this phase with main's `merge_gate.py --checkout <tree> --tests-only`.
- Phase 3 (done before this phase): `scripts/test_times/gate_record.py` holds `TYPE_CHECK_JOB`, the (suite, module) pair `("types", "basedpyright")` that the index leaves out of `modules_run` and `tests_run`. The type job's `GateJobRequest` and `GateJobResult` in `merge_gate.py` (955, 957) use it, so the writer and the reader name it in one place.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'` green
- `python3 -m unittest discover -s scripts/lib` green
- `basedpyright scripts/production/merge_gate.py scripts/production/test_merge_gate.py scripts/production/test_merge_gate_test_times.py` prints `0 errors, 0 warnings, 0 notes`
- The checkpoint gate, main's `merge_gate.py --checkout <tree> --tests-only`, is green. The fixture tests prove the `gate_run` line; this tree's own gate runs live only after the admission rebuild

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

### Phase 4 — `/test_times` shows suite, module and test history  · status: todo
**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** `/test_times` prints in under one second the descending sort of test modules, the history of gate runs and folder suites, and the history of one module or one test.

**Spec:**

1. `scripts/test_times/test_times.py`, with `argparse` subcommands. It reaches the build log as `scripts/buildlog/cli.py` does: `index.py` imports `store` and `parse` by bare name, so `scripts/buildlog` must be on `sys.path`. Only rows of the current host (`store.host_name()`) count. Each subcommand first runs `index.update()` and then queries `index.read_only()`:
   - `top [--limit N] [--repo PATH]` (the default with no subcommand; `N` defaults to 20; `PATH` to `~/.claude`): the descending sort. Rank each live module by the median `wall_s` of its ranking rows from its last `TOP_RUNS` gate runs in `gate_modules` (`conf.read("test_times").whole("TOP_RUNS")`). A run's ranking row for a module is its first completed execution: the lowest `attempt` whose `restart` is empty. A hold restart is not a measurement, and a rerun alone runs on a quiet machine. A module is live when `discover_test_modules(PATH)` (imported from `scripts/production/merge_gate.py`) lists it; the `types/basedpyright` row is always live. Columns: rank, `suite/module`, median s, last s, median CPU s, tests run, slowest kept test of the last run with its seconds, runs counted. When the last ranking row has `times_missing`, the tests-run and slowest-test cells show `times missing`, never zero. The `types/basedpyright` row runs no tests: its tests-run and slowest-test cells are empty, never `times missing` or zero. Last line: `<count> modules; sum of medians <s> s`.
   - `suites [--days N]` (default 7): gate runs, newest first: local start time, host, selection, modules run, `wall_s`, `PASS` or `RED`, the slowest module of the run.
   - `suite <folder> [--days N]`: that folder suite's `wall_s` in each run, newest first.
   - `module <folder>/<file> [--runs N]` (default 20): each execution of the module: local time, attempt (with `hold` for a hold restart), `wall_s`, `cpu_s`, `peak_mib`, status, `tests_s`, `wall_s - tests_s` (import and fixture time), the 3 slowest kept tests. A row with `times_missing` shows `times missing: <reason>` in place of `tests_s`, `wall_s - tests_s` and the slowest tests, never zeros.
   - `test <text> [--runs N]` (default 20): kept rows whose test name contains `<text>`: local time, `suite/module`, test, `duration_s`, status. A later run of the same module with no kept row for the test shows `not kept`: the test passed under that run's `SLOW_TEST_S`, or did not run (removed, skipped or filtered). A run whose module row has `times_missing` shows `times missing`. The index keeps no full test list, so an absent row never shows a duration.
   - Times show in the user zone (`astimezone(user_zone())`), in the form `11-03 09:30`. Column widths come from `terminal_cells.py`. An empty result prints `no gate runs recorded yet` (or the subcommand's subject) and exits 0.
2. `commands/test_times.md`, in the form of `commands/build/status.md`: run `python3 ~/.claude/scripts/test_times/test_times.py $ARGUMENTS` and show its lines unchanged; list the subcommands in one line each.

**Files:**
- `scripts/test_times/test_times.py` — new: the query tool
- `commands/test_times.md` — new: the `/test_times` command
- `scripts/test_times/test_test_times.py` — new: query tool tests

**Workers:** 1 writer + 1 tester — the tool and its tests split by file
- `impl` — `scripts/test_times/test_times.py`, `commands/test_times.md`
- `test` — `scripts/test_times/test_test_times.py`: write `gate_run` lines with `write_gate_run` into a `BUILDLOG_DIR` fixture (a `CONF_DIR` fixture sets `TOP_RUNS`), run each subcommand in-process, and check the sort order by median, the host filter, the live-module filter against a fixture repository, the ranking-row rule (a hold restart then a completed run; a failed first run then a rerun alone), the `not kept` and `times missing` cells (a module with missing times, a removed test, a `SLOW_TEST_S` change between runs), the empty test cells of the `types/basedpyright` row, the user-zone time form, and the empty outputs; one test runs the CLI in a fresh subprocess

**State:** none (reads only; `index.update()` appends to the index as every build log reader does).

**Constraints from prior phases:**
- Phase 1: `config/test_times.conf` holds `TOP_RUNS`; `gate_record.write_gate_run` writes fixture lines.
- Phase 2: a hold restart is its own module row with `restart: hold`; a failed module's rerun alone is the next attempt. A row with `times_missing` has no measured tests, and its `tests_s` is not a measurement. The `types/basedpyright` row has no tests and an empty `times_missing`: timing does not apply to it.
- Phase 3: tables `gate_runs`, `gate_suites`, `gate_modules` (`attempt` counts from 1; the type job is `types`/`basedpyright`) and `gate_tests`, with indexes on `(suite, module, started_at)` and `(suite, module, test, started_at)`; a test-name substring match scans `gate_tests` (19 ms over 250,000 rows).

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
