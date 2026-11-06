# natest

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Record each `~/.claude` test run, test module and slow test in the build log, show their history at once with `/test_times`, and make the slowest test modules faster, from the top of a descending sort down.

> **Production: builds** — unit `natest-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-11-03 09:30 EST

From the user (via natedev): Mission: find the longest-running ~/.claude tests and make them faster. Keep a descending sort of test times; when it makes the longest one shorter, it takes the next one. Build tools that instantly show the history of ~/.claude test suite times and of individual test times. Use parallelism, removal of trivial tests, shared code and good engineering practice. It must also send architectural change proposals back. It organizes its work in phases so /unit:direct works, and keeps its own phase backlog: the user wants the standard process engaged.

## Standing work

1. At each phase start, read the merge gate's slowdown warnings; natest-unit owns them. A warned module goes to `builds-natest-next.md`.
2. At each phase end, re-sort with `show.py top`. Before the last todo phase starts, add a speed phase for the top live module that no phase holds.
3. `builds-natest-next.md` lists each module with a reported floor. A floored module is a target again only when its gate median rises 20% above its floor.
4. When no live module has a median over 60 s, the plan holds and tells the showrunner; it does not close.

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
  - `scripts/production/full_test.py` — a unit's run: it selects modules through `scripts/production/affected_modules.py`. `merge_gate.SuiteMode` names the two modes; `run_suites(..., mode=SuiteMode.MERGE_GATE)` runs all modules (Phase 36). The line numbers above are history: find the current lines with `git grep`.
  - `scripts/lib/build_steps.py`, `config/build_steps.conf` — `StepNames.shown` and `StepNames.prose`: the step names that the user reads (Phase 36)
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py`, `scripts/production/test_full_test.py` — the gate's fixture and tests
  - `scripts/buildlog/store.py` — `root()` (honors `BUILDLOG_DIR`), `host_name()`, `utc_iso(epoch)`, `host_file(host, epoch)`, `append_line(path, record)`: the JSON-lines store, one file per host per month
  - `scripts/buildlog/record.py` — `RECORD_VERSION`, `new_id(host, epoch)` (513), `ssh_attempt` (1055): the pattern for a one-line record writer
  - `scripts/buildlog/index.py` — `SCHEMA_VERSION = 34`, with `step`, `dirty_paths` and `dirty_path_count` (was 42); column lists such as `TEST_COLUMNS` (141) and `CALL_COLUMNS`; `TABLES` (452, with the gate tables); `create_schema` (574, adds a `<table>_src` index for each table); `insert`, `pick`; `add_step` (763, the pattern for a nested list that becomes child rows); `ADDERS` (950, `gate_run` maps to `add_gate_run`); `forget` (954, loops `TABLES`); `update()` (1012); `read_only()` (1051)
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
- **Test:** for each named test file, `python3 -m unittest discover -s scripts/<folder> -p '<file>'` from the worktree root; always also `python3 -m unittest discover -s scripts/lib`. A worker's verification lists every test module that calls a changed function, unchanged callers and subprocess fixtures included (Phase 18: a caller's adjacency test was missed). Checkpoint gate: merge origin's merge branch first, then the full test: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/full_test.py --checkout <tree>` (main's copy). Until Phase 36 is on main, main has only `merge_gate.py --checkout <tree> --tests-only`, which runs all modules.
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
  - Commit every new file in the disposable gate clone before the gate starts. The aws_target box replica copies only `git ls-files` (Phase 22: an untracked new module turned `test_control.py` red).
  - Each dispatch names one worker that runs the changed modules and their callers after the last edit. With a writer and a tester, the writer runs the chain (Phase 22: each left it to the other).

- **Speed method** (every speed phase):
  0. Targets: at dispatch, the director takes the top live rows of `test_times top` that no phase holds. When they differ from the modules the phase names, the director rewrites its Goal, Files and acceptance first. `types/basedpyright` is never a target; it goes to its architecture proposal.
  1. Baseline: before the edit, run the module three times with `TEST_TIMES_OUT=<run directory>/<module>-before-<n>-<sha>.json python3 scripts/test_times/module_run.py scripts/<folder> <file>` and keep the median `wall_s` and `cpu_s`. These runs find the cost (step 2); they are not the before number of the gain (step 5). Read `python3 scripts/test_times/show.py module <folder>/<file>` for the gate history.
  2. Find the cost from the JSON: the slowest tests, and `wall_s - tests_s` (import and module or class fixtures). Name the expensive operations before any edit; do not apply a cut the module already has. Estimate each command's removable cost, not the whole script's: Phase 41 estimated 54-62 CPU s and measured 1.8, because 246 of 366 starts already had the light path and two commands kept the heavy chain.
  3. Cut it, in this order:
     - Find each fixed wait first: a sleep, a poll count, or a wait that no published state ends. Wait on the event, file, pipe or ready signal the code already gives. A fixed wait often also causes flakes, so a flaky module is a speed target too (fastest-unit, Phases 5i and 6: 64 to 35 s, 58 to 24 s). A server fixture that runs `serve_forever` pays its 0.5 s poll interval on each stop; stop it on an event (Phase 20: 62 to 21 s).
     - Remove trivial tests (Invariants above) only when step 2 shows tests that read as repeats: tests with the same setup and the same assertion target. Then run `python3 scripts/test_times/coverage_overlap.py scripts/<folder> <file>` and compare each candidate's assertions with its covering tests; a candidate whose result a covering test also checks is a trivial test. Skip the sweep in a module whose time is in process starts: Phase 13 found no repeat in five such modules.
     - Share expensive setup: build a seed once in `setUpModule` or `setUpClass` and copy it for each test, as `scripts/build_hold/test_build_hold.py` does.
     - Call code in-process in place of a subprocess when the test does not check the process boundary; keep one process test for each boundary.
     - Split a module whose tests are independent into two or more files, so the gate runs them at the same time. The original file and its new files are one group: compare the group's summed CPU, its test count, and its wall time when its files run at the same time. Every file of the group is in acceptance. Time the group with one `module_run.py` process per file, all started at once (as `pairs.sh` does), from the first start to the last end. The gate runs at most half the CPU count of modules at once (`DEFAULT_MAX_RUNNING_JOBS` in `merge_gate.py`); a group with more files than that gains less in the gate.
  4. A change to a shared support file (for example `scripts/lint/memory_test_support.py`) is allowed only when every module that imports it stays green; name those importers in the final gate run.
  5. After the edit, measure the gain in same-load pairs: run the module at the phase base (a `copies.py run --commit <base>` copy) and in the worktree at the same time, three rounds, into `<module>-pair-<before|after>-<n>-<sha>.json`, with `/proc/loadavg` beside each round. Each side sets `TMPDIR` to its own folder under `/dev/shm` and removes it after the round, because the gate runs there and pays no `fsync` cost (Phase 47). A number on the normal disk is a separate result. The host load changes all day, so a before batch and a later after batch do not compare. The director runs the pairs after the last repair lands (example: `phases/7/pairs.sh` in the run directory). Half the wall time is the target. When the allowed edits cannot reach it, report the measured floor and send the rest as an architecture proposal. A floor names its cost breakdown and the one operation that limits it. A proposal names its owner, its revisit condition, and whether its gain is measured or estimated. The pair tool prints the before and after medians as a table when it ends. Report the before and after medians, each removed test with its reason or covering test, and each architecture proposal with its measured gain.

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

- The SSH target tests are five `test_verify_ssh_targets*.py` files that share `verify_ssh_targets_test_support.py`. Each file runs in its own `module_run.py` process, all at the same time (the runner itself is serial). Group wall time is 62-71 s (median), from 327.1 s; CPU is 181-188 s, from 205.5 s.
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

### Phase 6 — The simplify-lens and assign-launcher modules run faster  · status: done
**Depends on:** Phase 5

#### As-built

- `test_simplify_lens.py` seeds one git repository per class and copies it for each test. `setUp()` still records `start-run` and `start-phase simplify` for each test, except the ad hoc test, where the assignment makes the phase. Same-load pairs: 204 s to 118 s wall, CPU 117 s to 94 s.
- `assign_launcher_test_support.py` defines `AssignmentExecution = InProcessAssignment | LauncherScript`; `AssignLauncherFixture.launch()` and `successful()` take `execution`. The process tests stay.
- The assign launcher group records its session for each test, as before.

**Files:**
- `scripts/direct/test_simplify_lens.py` — per-class git seed
- `scripts/direct/assign_launcher_test_support.py` — named execution choices
- `scripts/direct/test_assign_launcher.py`, `_identity`, `_reuse`, `_review` — use the execution choices
- `scripts/agents/test_heartbeat.py` — checks the wait helper

**Gotchas:**
- The simplify-lens floor is one real review-launcher run per test (3-6 s); `test_report_validation` takes 25 s for 8 launches.
- On natedev, before and after batches at different times compare host load, not code; measure with same-load pairs.

**Ruled out:**
- A per-class recorder seed for the assign launcher tests (F025): it gained about 4%, so simple wins (showrunner ruling).

### Phase 7 — The fast-track module and the next live slow module run in half their time  · status: done
**Depends on:** none

#### As-built

- `test_fast_track.py` calls the CLI in-process where a test does not check the process boundary, and starts Git directly where no interruption fixture is needed. Same-load pairs (load 40-73): 304 s to 193 s wall, 202 s to 110 s CPU. Half was not reached; the floor is 55 tests at about 3.5 s each of real Git work. The architecture proposal (one injected Git runner in `fast_track.py`) went to the showrunner.
- The Git hang guard (`GIT_HANG_GUARD`) runs Git under `timeout -s KILL`, which moves Git to its own process group and kills that group at the deadline. Since Phase 33 only `test_git_wrappers_stop_a_blocked_hook` installs it; the other wrappers start Git directly in the module's process group.
- `cli_fixture.py` adds the `@process_boundary` mark and `process_boundary_for_case()`. The fixtures that kept one named test as a process now read the mark from the test method.

**Files:**
- `scripts/production/test_fast_track.py` — speed edits and the hang guard
- `scripts/production/cli_fixture.py` — the process-boundary mark
- `scripts/production/merge_checkpoint_test_support.py`, `rename_test_support.py`, `scripts/direct/findings_test_support.py`, `scripts/bevy_upgrade/cli_test_support.py`, `scripts/style/style_plan_test_support.py` — read the mark; one importer test of each marks its process case

**Gotchas:**
- `lib/test_process_group.py` also scans script text held in strings: a group signal there goes through `send_process_group`, from a process outside the target group.

**Ruled out:**
- A per-class seed for `test_crate_run.py`: no gain under the same load (128 s to 126 s).

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

### Phase 11 — Shared test fixtures skip repeated setup  · status: done
**Depends on:** Phase 6

#### As-built

- No change shipped. Same-load pairs (3 rounds) showed no gain: `test_simplify_lens` 93.2 to 92.7 s wall, `test_nix_env` 3.0 to 2.8 s, `test_nix_env_launches` 4.7 to 4.7 s, `test_review_launcher` 32.2 to 32.5 s.

**Gotchas:**
- The `lib/py` interpreter probe is not a measurable share of these modules. Their cost is git work and subprocess starts.

**Ruled out:**
- Fixture micro-cuts (an interpreter cache seed, a per-class config copy): the gain is noise, and a trimmed config copy needs a hand-kept file list that breaks a future caller.

### Phase 12 — A tool lists the tests that only repeat other tests  · status: done
**Depends on:** none

**Result:** `coverage_overlap.py` on `test_fast_track.py`: 25 of 55 tests known, 9 candidates, 191 s with the collector on (189 s module time). The 30 unknown tests name their cause (a git wrapper script on the test PATH, or Python children with no set). Thread attribution, setup-child liveness and a failed exec moved to Phase 15; Phase 13 waits for it.

#### As-built

- `python3 scripts/test_times/coverage_overlap.py scripts/<folder> <file> [<file> ...]` runs the modules through `unittest` with `sys.monitoring` `LINE` events under its own tool id, one `(path, line)` set for each test. `COVERAGE_OUT=<path>` writes the JSON (tree sha, interpreter, and for each test: id, status, seconds, line count, `child`, covering ids). The table prints through `terminal_cells.print_table` and `seconds`.
- Candidates are decided slowest first against the kept set; two tests that cover each other give one candidate. Unknown tests are never candidates and cover nothing.
- `line_collector.py` holds the `LINE` collector, the path filter and the launch record (audit hook), used by the parent and by each child. `start_child()` owns the child setup. `ActiveTest.finish_processes()` drops a `Popen` with no pid, records exited and pending launches, and releases the `Popen` objects; `stop_test` and `write_child_report` both call it. A thread-local argument identity skips the duplicate `posix_spawn` event.
- `coverage_child/sitecustomize.py` loads `line_collector.start_child()` when `NATEST_COVERAGE_TEST` is set, then always loads the next `sitecustomize` (the Nix `NIX_PYTHONPATH` one).
- The child rule is a count: `unknown: <n> python children, <m> reported` when a Python child wrote no set (this covers `-I`, `-S`, `-E`), `unknown: unverified launch <program>` for a shell, `env` or a non-Python `#!` script. `COVERAGE_LEAF_PROGRAMS` (`git`) launches are verified. A launch that fails to start is no launch.
- The path filter drops test code (`scripts.lib.code_kind.is_test_code`), the runner classes (the files of `CoverageResult.__mro__`) and the collector.
- `module_run.timed_outcomes(result)` is the one outcome rule for `test_times` and the collector. `print_table` and `seconds` moved from `show.py` to `scripts/lib/terminal_cells.py`.
- On `test_fast_track.py`: 25 of 55 known, 9 candidates, 191 s with the collector on (189 s module time). 28 of the unknown tests cite `unverified launch git`: the module puts a non-Python `#!` git wrapper on PATH.

**Files:**
- `scripts/test_times/coverage_overlap.py`, `line_collector.py`, `coverage_child/sitecustomize.py` — the tool
- `scripts/test_times/module_run.py`, `show.py`, `scripts/lib/terminal_cells.py` — the shared outcome rule and table
- `scripts/test_times/test_coverage_overlap.py` — 22 tests, 4 s

**Gotchas:**
- A commit during a simplify review changes git status, and the review fails. Commit nothing while a reviewer runs.
- Each review round found new attribution edges. Thread attribution, setup-child liveness and a failed exec moved to Phase 15 at repair round 3 (hard landing, after an Astra review).

**Ruled out:**
- coverage.py: the Nix Python does not have it (showrunner ruling 2026-11-04).
- Parsing `-I`/`-S`/`-E` flags: the count rule covers them with no parser.

### Phase 13 — The slowest modules lose the tests that only repeat other tests  · status: done
**Depends on:** Phase 12, Phase 7, Phase 14, Phase 15

#### As-built

- No test was removed. Two workers read each candidate's assertions against those of its covering tests. None checked the same result.
- `scripts/direct/test_verify_ssh_targets_routing.py`, `_results.py`, `_retry.py`: 25 tests, 24 unknown (unverified launches of bash, git, rustc, ssh, rsync, ps; forks; one shared thread), 0 candidates.
- `scripts/aws_target/test_deploy.py`: 97 tests, 39 candidates kept, 4 unknown (bash, env launches).
- `scripts/hana_shot/test_hana_shot.py`: 113 tests, 42 candidates kept, 22 unknown (magick launches, shared threads, a thread that outlived its test, an unreported Python child).
- The five modules held back by the showrunner (other units' unlanded branches) were not read.

**Files:**
- none changed; `scripts/test_times/coverage_overlap.py` was run on the five modules above

**Binds later work:**
- Line overlap does not prove a repeat in a process-heavy module. Speed work on the slowest modules targets process starts and fixed waits.
- Speed proposals sent to the showrunner: the hana_shot fixture server stop (0.5 s `serve_forever` poll, upper bound 47.5 s of 74 s); the hana_shot busy-capture deadline (2.7 s); fake AWS process starts in `test_deploy.py` (three tests near 13 s).

**Gotchas:**
- `test_held_local_target_does_not_block_linux_check` fails under the collector: its fixture mocks `subprocess.Popen`, and `line_collector.py` checks `isinstance` against it. Phase 19 owns the fix.

### Phase 14 — `test_times top` ranks only valid runs  · status: done
**Depends on:** none

#### As-built

- `show.py top` counts a module row only when its status is 0, its run is not aborted, its run's checkout is `TOP_CHECKOUT`, and it started within `TOP_MAX_AGE_DAYS`. It keeps the host and `restart = ''` filters. One line under the table names each live module (`discover_test_modules`) that has no counted row. `top` prints in 0.2 s on the live index.
- Each gate run record stores `slow_test_s` and `tests_run`. `gate_record.module_runs(executions, slow_test_s)` takes the threshold; `merge_gate.record_gate_run` reads `gate_record.slow_test_s()` once, inside its `try`, and passes it in. The build log index has both columns in `gate_runs`; an old record reads them as null.
- `show.py suites` prints `tests run`. A test row that is not kept reads `not kept (faster than <slow_test_s> s or did not run)`: a record does not list the tests that ran.

**Files:**
- `scripts/test_times/show.py`, `config/test_times.conf` — ranking filters, the unmeasured line, `TOP_CHECKOUT`, `TOP_MAX_AGE_DAYS`, and `COVERAGE_LEAF_PROGRAMS` for Phase 12
- `scripts/test_times/gate_record.py`, `scripts/buildlog/index.py`, `scripts/production/merge_gate.py` (owner: sdlc devtools-unit) — the two run fields
- `scripts/test_times/test_show.py`, `test_gate_record.py`, `gate_record_test_support.py`, `scripts/buildlog/test_gate_index.py`, `test_index.py` — tests

**Gotchas:**
- A file in the `files` list of two open phases blocks the edit hook. Give each file to one phase when the plan is written.

### Phase 15 — The coverage tool checks which children and threads are alive at each test boundary  · status: done
**Depends on:** Phase 12

#### As-built

- `line_collector.py` keeps each launch's `Popen`, launch kind, owner test and launching thread, and records constructor success explicitly. At each test start and stop it polls the children it holds.
- `coverage_overlap.py` marks a test unknown and names the cause:
  - `child outlived its test`: a test's child is alive at its stop; each later test that starts while it lives is also unknown.
  - `shared child`: a setup Python child is alive at a test's start.
  - `shared thread`: a thread alive at a test's start runs a measured line, or a child is launched from a thread other than the runner thread.
  - `thread outlived its test`: a thread the test started is alive at its stop.
  - `unverified launch git`: a launch with no `Popen` and no set.
- LINE events stay on while thread attribution is needed. A shared thread's line never goes into a test's set.
- An exec writes the set apart from the collector stop. A successful exec keeps the pre-exec set as its own report; a failed exec keeps collection on.
- Acceptance on `test_fast_track.py`: 25 of 55 tests known, 9 candidates. Collector run 229.3 s against 215.2 s plain (ratio 1.07).

**Files:**
- `scripts/test_times/line_collector.py`, `scripts/test_times/coverage_overlap.py` — liveness, threads, exec report
- `scripts/test_times/test_coverage_overlap.py` — tests

**Binds later work:**
- An unknown test is never a removal candidate.

**Gotchas:**
- 28 of 55 `test_fast_track.py` tests stay unknown through `unverified launch git`.
- The checkpoint also carries the Phase 17 fixture repair: `test_target_report.py` and `test_show.py` write a private `buildlog.conf`, which `update()` reads.

### Phase 16 — `/test_times top` ranks the slow tests of the calling session's repository  · status: done
**Depends on:** none

#### As-built

- `show.py top` with no `--repo` takes the root from `treekey.worktree`, then the identity from `repository.repository_key`. The ~/.claude repository keeps the gate module view. Any other repository ranks its nextest tests for one host (`--host`, default `store.host_name()`).
- Rank: one row per `binary` and `test`, keyed `(not passed, -median, binary, test)`. A test with no recorded duration shows `DurationNotRecorded`. The heading names the repository, the host and the threshold.
- `parse.py` reads `NEXTEST_SLOW_TEST_S` (1.0) from `config/test_times.conf` through `nextest_slow_test_s()`.
- `index.py`: the step owns the identity. `tests.repo_identity`, `tests_step` and `tests_repository_history` are gone. `steps.identity_derived` marks a derived identity. `resolve_mac_repositories(connection, touched_calls)` runs one `UPDATE ... INDEXED BY steps_call` for the calls that the update read: it takes the identity of a recorded step of the same call and `repo_path`, else of the call's `ssh_attempts` row. `open_for_update` recreates missing indexes; `update_in(root)` and `update_lock_in(root)` serve a scratch root.
- `width_trial.report` builds a missing index through `update_in`.
- Measured: a 1,939-test hana log keeps 606 rows; `test_results` 11.0 ms plus 7.2 ms insert per step. One Mac step update 33 ms, resolver 0.30 ms. Warm hana top 0.25 s on 1.8 M rows (was 1.6 s). Importing `record` costs 49.7 ms cold in `show.py`.

**Files:**
- `scripts/test_times/show.py`, `scripts/test_times/test_show.py` — scope, host and ranking
- `scripts/buildlog/index.py`, `scripts/buildlog/test_index.py` — derived identity, query indexes
- `scripts/buildlog/parse.py`, `scripts/buildlog/test_parse.py` — the threshold read
- `scripts/buildlog/width_trial.py`, `scripts/buildlog/test_width_trial.py` — missing index build
- `config/test_times.conf`, `config/README.md`, `commands/test_times.md` — key and docs

**Binds later work:**
- Keep the derived-identity rule for old rows. A step with a recorded identity has `identity_derived = 0`, and the resolver never changes it.
- A query that looks up steps by call uses `INDEXED BY steps_call`.

**Gotchas:**
- A correlated identity lookup per call planned through `steps_host_started` and held the live `index.lock` for 5 s and more. The resolver now reads only touched calls.

### Phase 17 — The build log keeps only the index files that a checkout used in the last day  · status: done

#### As-built

- `cleanup_unused_indexes(root, current, settings)` runs at the end of each `update()`, under `update_lock()`. It deletes the legacy `index.sqlite` (`LEGACY_INDEX_NAME`) at any age, and each other `index-<hash>.sqlite` whose `max(st_atime, st_mtime)` is older than `INDEX_UNUSED_DAYS` (2). The current file always stays.
- `update()` reads `config/buildlog.conf` once and sets the current file's mtime when it is older than `INDEX_TOUCH_MINUTES` (60).
- `remove_index(path)` deletes the `INDEX_SIDECAR_SUFFIXES` files (`-journal`, `-wal`, `-shm`) first and the main file last. Cleanup, `reindex()` and `open_for_update()` use it. Cleanup also deletes orphan sidecars, so the next `update()` finishes an interrupted removal.
- A removed file costs its next reader one rebuild. Report commands rebuild through `update()`; memory admission uses its fallback while the index is absent.

**Files:**
- `scripts/buildlog/index.py` — `remove_index`, `INDEX_SIDECAR_SUFFIXES`, `LEGACY_INDEX_NAME`, cleanup, the hourly stamp
- `config/buildlog.conf` — `INDEX_UNUSED_DAYS=2`, `INDEX_TOUCH_MINUTES=60`
- `scripts/buildlog/test_index.py` — cleanup, sidecar, legacy, age and stamp tests

**Binds later work:**
- A change to `index.py` keeps `remove_index()`, `LEGACY_INDEX_NAME`, the `settings` argument of `cleanup_unused_indexes`, and the hourly stamp in `update()`.

**Gotchas:**
- Read-only paths (`index.read_only()`, the four `read_only_uri()` connects in `scripts/lint/memory_admit.py`) never stamp mtime, and relatime moves atime at most once a day. Keep `INDEX_UNUSED_DAYS` at 2 or more and `INDEX_TOUCH_MINUTES` well below it.
- One full rebuild costs 61 s wall, 43 s CPU, 1.0 GB peak memory and a 1.5 GB file (natedev, 2026-11-04).
- A live `show.py top` can hold `index.lock` for a long time; a test or acceptance run uses a scratch `BUILDLOG_DIR` and a timeout.

**Ruled out:**
- A 1-day window: a checkout that only reads its index could lose a file it uses each hour.

### Phase 18 — Mac and Linux mirror steps record the repository identity that natedev sends  · status: done
**Depends on:** Phase 16

#### As-built

- natedev sends `KnownRepository.key` (from `repository_identity`) to each mirror step as `BUILDLOG_REPO_IDENTITY`. An empty key is not sent. Phase 22 stores the value in the step row; until then nothing reads it.
- Mac: `remote_test_command` builds a `repo_identity` local in the shape of `sent_tree`, and puts it before `BUILDLOG_CALL_ID`, so `BUILDLOG_SENT_TREE_KEY` stays right after the call id.
- Linux: `linux_runner.command_script` takes a required `repo_identity` and adds `--repo-identity` only when it is not empty. `run` and `execute_slot` take it with a `''` default and add it to the step `environment` beside `BUILDLOG_SENT_TREE_KEY`.
- The record side (items 2-7 of the first Work Order) moved to Phase 22.

**Files:**
- `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py` — send the key (verify-unit's files)
- `scripts/mac_test/test_offload.py`, `scripts/mac_test/test_linux_runner.py` — the Mac command and Linux command and environment tests, with and without a key

**Gotchas:**
- `test_offload_lifecycle.py` checks that `BUILDLOG_SENT_TREE_KEY` is the word right after `BUILDLOG_CALL_ID`. Put a new Mac assignment before the call id.
- verify-unit's unlanded `verify-remake-1-24` also changes `remote_test_command`. This unit does the one-line merge resolve when it lands.

### Phase 19 — The coverage tool runs under a mocked `subprocess.Popen` and its git child test passes at high load  · status: done
**Depends on:** none

#### As-built

- `line_collector.py` keeps `POPEN_CLASS = subprocess.Popen` at import, beside `POPEN_INIT`. `stack_popen` and the audit hook check `isinstance` against it, so a fixture that replaces `subprocess.Popen` with a mock collects with no error. `test_verify_ssh_targets_routing.py` runs all 13 tests under the collector.
- The flaky git test was not a collector defect. Both tests stay known; candidate selection takes the slower of two tests with equal coverage, so the candidate reverses when git is slower. `test_git_child_keeps_coverage_known` asserts the slower row as the candidate, covered by the faster one. A deterministic `select_candidates` test checks both duration orders. The git test passed 25 of 25 repeats at load about 66.
- `coverage_overlap.py`: `load_suite` reads the new `TestLoader.errors` after each selected module's discovery and raises `ModuleImportsFailed`. `main` prints `coverage overlap: <file> does not import: <last error line>` for each failed module to stderr, prints no table and no summary, runs no test, and returns 2.

**Files:**
- `scripts/test_times/line_collector.py` — the Popen class kept at import
- `scripts/test_times/coverage_overlap.py` — the import-failure line and exit 2
- `scripts/test_times/test_coverage_overlap.py` — the mocked Popen, both-order and import-failure tests

**Binds later work:**
- Read a process class at import, never at call time: a fixture can replace it.

### Phase 20 — The hana_shot test module stops its fixture servers at once  · status: done
**Depends on:** none

#### As-built

- `FixtureServer` (a `ThreadingHTTPServer`) serves until a `stopping` event is set. `shutdown` sets the event, opens one wake connection, and waits for the loop to finish, with `TEST_HANG_GUARD_SECS` as the limit. `HanaShotTest.setUp` and `restart_fake_listener` use `FixtureServer.serve` in place of `serve_forever`, so no stop waits for the 0.5 s poll interval.
- `test_screenshot_busy_past_the_resend_limit_fails` gets `resend_seconds=0.05` through the `BuildLogIsolatedTest` fixture config. `hana_shot.py` is unchanged.
- All 113 tests and their assertions stay. Same-load pair medians (3 rounds, load 28-37): wall 62.3 s to 20.9 s, CPU 17.3 s to 19.3 s. In single runs the event stop (30.5 s) beat a short poll interval (32.8 s).

**Files:**
- `scripts/hana_shot/test_hana_shot.py` — the fixture stop and the private resend deadline (the hana_shot owner's file; fixtures only)

**Gotchas:**
- `serve_forever` pays its poll interval on each `shutdown()`. A fixture that starts a server for each test pays it for each test.

### Phase 21 — The merge gate fails a test module that starts the real `send.py`  · status: done
**Depends on:** none

#### As-built

- Phase 26 replaced the audit hook with a guard at the `send.py` entry. The hook bullets below are history.
- `scripts/message/session_send_test_support.py` holds the audit hook `_forbid_sender`, `RealSenderForbidden(AssertionError)`, the state `sender_violation: NoSenderViolation | RealSenderRefused(test_id)` and `requesting_test()`. `forbid_real_sender()` installs the hook once.
- The hook reads the process-start audit events `subprocess.Popen`, `os.posix_spawn`, `os.exec`, `os.spawn` and `os.system`. It checks each value whole and after `shlex.split`, resolves a relative path against the child `cwd`, and compares it with the real `send.py`. It records the first refusal, with the test that asked, before it raises. A test that catches the exception does not clear the state.
- The only installs are the per-module `setUpModule` installs, which the scan test in `test_session_send.py` enforces.
- `scripts/test_times/module_run.py` `main()` installs no hook. After `unittest.main`, when `scripts.message.session_send_test_support` is loaded and its state is `RealSenderRefused`, it prints `<pattern>: started the real scripts/message/send.py` to stderr and exits 1.
- `test_sweep.py` same-load pair medians: 29.5 s before, 29.8 s after (noise at load 48).

**Files:**
- `scripts/message/session_send_test_support.py` — the guard, its exception and its state
- `scripts/test_times/module_run.py` — reads the guard state after a module
- `scripts/message/test_session_send.py` — child probes for each process-start form and the scan test
- `scripts/test_times/test_module_run.py` — uncaught, caught, expected-failure and fake-sender fixture modules

**Binds later work:**
- Test-only code lives in a `*_test_support.py` or `test_*.py` file. The line collector counts every other `scripts/` file as production code.

**Gotchas:**
- On Linux, `subprocess.Popen` emits a nested `os.posix_spawn` event. A probe's own safety hook must allow the fake sender on both events.

**Ruled out:**
- A check inside `session_send_argv`: about 20 modules build their own `send.py` path.
- A production-named guard module: it adds guard lines to coverage records.

### Phase 22 — Mirror step rows store the sent repository identity, and `show.py` loads a light repository module  · status: done
**Depends on:** Phase 18, Phase 23

#### As-built

- `record.step()` records the `BUILDLOG_REPO_IDENTITY` value for a `verify-mac` or `verify-linux` step whose local key is `store.UNKNOWN_IDENTITY`. `sent_repository_identity()` reads it stripped and returns `SentRepositoryIdentity(value) | NoSentRepositoryIdentity`. A known local key wins, and an ordinary caller ignores a sent value. A recorded identity has `identity_derived = 0`; old `unknown` rows keep the Phase 16 index rule.
- `scripts/buildlog/repository.py` imports only the standard library and `store`. It holds `GIT_TIMEOUT_S`, `output(args, cwd, timeout)`, `remote_identity(remote, directory)` and `repository_key(directory)`. `record.py` imports these names, so `record.remote_identity` still works for `memory_admit.py`. `recorded_repository_key` is deleted; `step()` checks for an empty argv and calls `repository_key`.
- `scripts/test_times/show.py` imports `repository` in place of `record`. The cold start of `show.py top` fell from 184.8 ms to 126.9 ms (median of 5 same-load pairs, a prepared index). The index build is 15.5 ms.
- `parse.py` and `record.sync_config` import `scripts.lib.conf`, so one step process loads `conf.py` once.
- `scripts/build_queue/admission_code.py` lists `buildlog/repository.py` after `buildlog/record.py` (showrunner, 2026-11-05).
- The Mac records the sent identity only after the normal main promotion.

**Files:**
- `scripts/buildlog/repository.py` — the light repository key module
- `scripts/buildlog/record.py` — the sent identity reader and the mirror rule (report-unit's file)
- `scripts/buildlog/parse.py` — one `conf` import name (report-unit's file)
- `scripts/test_times/show.py` — imports the light module
- `scripts/build_queue/admission_code.py` — lists the light module (queue-unit's file)
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_parse.py`, `scripts/buildlog/test_step_row.py`, `scripts/test_times/test_show.py` — the tests

**Binds later work:**
- A caller of the repository key uses `repository.repository_key(directory)`. report Phase 24's `offload.py` and `memory_admit.git_identity` callers switch to it when they land.
- A test that stops the git call patches `repository.output`; patching `record.output` no longer reaches the key lookup.

**Gotchas:**
- A gate clone must commit a new file before the gate: the aws_target box replica copies only `git ls-files`.

### Phase 23 — `test_show.py` passes in a checkout with no origin  · status: done
**Depends on:** none

#### As-built

- `test_show.py` read the real checkout's origin at import (`TOOL_IDENTITY`), so a clone with no origin gave 10 failures: `show.py` `main()` picks the module view when `--repo` has the identity of `show.TOOL_REPOSITORY`.
- `setUp` patches `show.TOOL_REPOSITORY` to the fixture repository `self.repo`, and computes `self.tool_identity` from it. `TOOL_IDENTITY` and the `remote set-url` call are gone. No production file changed; every test and assertion stays.
- 24 tests green in a checkout with an origin and in a `copies.py` clone with its origin removed.

**Files:**
- `scripts/test_times/test_show.py` — the fixture tool repository

**Binds later work:**
- A test reads no identity from the real checkout. Patch `show.TOOL_REPOSITORY` to a fixture repository.

### Phase 24 — `test_verify_ssh_targets_routing.py` runs in half its time  · status: done

#### As-built

- `build_targets.py platform` answers from `target_config` before any coordination import (`targets`, `build_hold`, the admission service, `roster`, `memory_admit`, `offload`). One start takes 0.094 s in place of 1.05 s, and `verify.sh` `target_platform` pays the lower cost on each call. The other commands import the coordination modules as before, and every module-level name that tests import from `build_targets` stays.
- `target_config.require_target_name(target)` holds the SSH target name check. `ssh_target_state_directory` and `configured_ssh_target` call it, and `offload.configured_ssh_target` delegates to `target_config`. The "unknown SSH target" message and its precedence are unchanged.
- Same-load pairs: the module drops from 117.6 s to 81.6 s (-31%). The floor is the Python start-up of `eligible` (24 runs) and `reserve` (11 runs). Both import `build_hold`, which imports `admission_service` and `memory_admit` at once (`build_hold.py:40-46`); `build_targets.py` and `targets.py:21` import `build_hold` directly.

**Files:**
- `scripts/build_targets/build_targets.py` — the light `platform` route before the coordination imports
- `scripts/build_targets/target_config.py` — `require_target_name`, `configured_ssh_target`
- `scripts/mac_test/offload.py` — `configured_ssh_target` delegates to `target_config`

**Gotchas:**
- A cut at `sweep.py` alone does not remove `memory_admit` from `eligible`, `reserve`, `register-local`, `wait-local` or `check-local`; the cut is the module-level `admission_service` import in `build_hold.py` (owner resources; queue-unit takes it after the `build_hold.py` freeze, measured 9.6 s here).

**Ruled out:**
- Import tests for `platform`, `eligible` and `reserve` (user rule 2026-11-05: a new check goes to the user first, with a reason).
- A warm command server in the fixture: it tests a different process boundary than production uses.
- A git identity cache in the fixture and changes to the shared cleanup scans: each costs less than 6 s.

### Phase 25 — The merge gate runs the `module_run.py` of the tree it tests  · status: done
**Depends on:** none

#### As-built

- `scripts/production/merge_gate.py` `module_runner(repo)` returns `<checkout>/scripts/test_times/module_run.py`. `run_suites` calls it once after discovery, and every test job starts that runner. The unit gate, `--tests-only` and `--nightly` (its prepared clone) all pass the tested checkout.
- A checkout with no runner refuses the gate with `REFUSED: test runner not found: <path>`, before any ledger row, admission or temporary folder. There is no fallback to the installed copy.
- `module_run.py` puts the cwd first on `sys.path`, and the gate starts each job in the checkout, so a job imports only the tested tree. This ended the nightly red at main 72539dc10, where the installed runner read guard state that the older tree lacked.
- Checks: the older tree's `test_session_send.py` and `test_disk.py` pass through the new job form; the branch's own gate on this phase plus builds 56ca7bc7a was green (1131 s, full suite).

**Files:**
- `scripts/production/merge_gate.py` — `module_runner` and the refusal
- `scripts/production/merge_gate_test_support.py` — `MergeGateFixture.build_fixture` commits the real runner into each fixture checkout
- `scripts/production/test_merge_gate_suites.py` — the checkout-runner test and the refusal test

**Binds later work:**
- A change to `module_run.py` takes effect for the tree that carries it; the installed copy no longer runs another tree's tests.

### Phase 26 — The sender guard also stops `send.py` started from its own folder or as a module  · status: done
**Depends on:** Phase 25

#### As-built

- `scripts/message/send.py` `main()` starts with the guard: when `TEST_SENDER_GUARD` is set, it writes the marker file that the variable names, prints `REFUSED: real scripts/message/send.py is forbidden in tests` and returns 1. This covers every start path: a direct path, `cd scripts/message && python send.py`, `python -m scripts.message.send`, a shell, `os.system`, `os.posix_spawn` and grandchildren.
- `scripts/message/session_send_test_support.py` `forbid_real_sender()` sets the variable in `os.environ` when it is not set (children inherit it), with a temporary marker path that `atexit` removes. It keeps the path once in `refusal_marker: SenderGuardNotInstalled | SenderRefusalMarker(path)`, so a test that clears `os.environ` does not lose it. The Phase 21 audit hook, `RealSenderForbidden` and `sender_violation` are deleted.
- `scripts/test_times/module_run.py` reads `refusal_marker` after `unittest.main`. When the marker file exists, it prints `<pattern>: started the real scripts/message/send.py` to stderr and exits 1.
- Known limit (showrunner, 2026-11-05): a wrapper that strips the variable on purpose (`env -i`, `env -u`, an explicit `env=`) is not guarded. The guard stops test accidents only.
- Checks: the probes for each start path and the cleared-environment test are green; the gate on this phase plus builds was green (1395 s, full suite).

**Files:**
- `scripts/message/send.py` — the guard at the top of `main()`
- `scripts/message/session_send_test_support.py` — sets the variable and keeps the marker path
- `scripts/message/test_session_send.py` — probes for each start path, the caught-refusal test and the install scan test
- `scripts/test_times/module_run.py` — reads the marker after a module
- `scripts/test_times/test_module_run.py` — fixture modules, the cleared-environment test

**Binds later work:**
- Only the modules that call `forbid_real_sender()` are guarded. The next-items backlog holds the proposal that the runner guards every module.
- A change to `send.py` keeps the guard as the first statement of `main()`.

### Phase 27 — `test_merge_gate_ledger.py` runs in half its time  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `scripts/production/test_merge_gate_ledger.py` (gate median 121.3 s, 14.8 CPU s; the top live module that no phase holds and that the verify-unit remake does not touch) runs in half its time, or a measured floor shows why it cannot, with every checked behavior kept.

**Spec:** Follow the Speed method. This phase runs while Phase 24 waits on the verify-unit remake (Phase 22 architect review, 2026-11-05). sdlc devtools-unit owns `merge_gate.py` and its test files; natest-unit changes the test files through cargo-berth's flow.

1. Profile before any edit: for each of the 17 tests, find where the wall time goes. The module spends about 106 s of 121 s with no CPU, so look first for waits: poll intervals, sleeps, timeouts, lock or admission waits, and child jobs that wait on a clock. Name each wait, the code line that holds it, and the published state that could end it. Count the gate runs, module jobs and type jobs that each test starts. Write the result into this Spec and name the files and changes before the code dispatch.
2. Cut in the Speed method's order: fixed waits first, then repeated setup, then process starts.
3. A cut in `merge_gate.py` or another production file goes to the showrunner as an architecture proposal with the measured gain, unless the showrunner approves it for this phase.
4. A change to `merge_gate_test_support.py` keeps every `test_merge_gate*.py` module green; name each one in the final run.
5. The two files belong to the sdlc production. sdlc devtools-unit holds them on an unmerged branch while it repairs a `merge_gate.py` guard (showrunner, 2026-11-05). sdlc gave natest the `run_gate` clock fix. The code step starts only after the builds showrunner sends the go with the main hash of devtools' repaired combined merge (due about 07:10 EST). That merge changes `merge_gate_test_support.py`: merge the hash first and build on it. The profile in step 1 is done.
6. Profile result (2026-11-05, baseline median 118.2 s wall, 11.1 CPU s, 17 tests): 110.0 s is 22 admission start waits of about 5 s each. After each admission, `scripts/lint/memory_admit.py:3716` publishes `NextStartAt(now + recovery_start_seconds)` (`build_admission.conf`: 5 s). The private server keeps that deadline between the gates of one test, and `admission_test_support.py:357-365` moves its fake clock with real time. So each later gate in a test waits 5 real seconds. Seed builds (0.3 s), type environment builds (0.9 s) and scheduler polls (3.7 s, `merge_gate.py:1144`) are small.
7. Code step (showrunner, 2026-11-05): the fixture does not advance the admission clock by hand. A manual `allow_next_start()` advance in each module is a workaround. queue-unit fixes the start spacing at its cause in the shared admission fixture (`scripts/build_queue/admission_test_support.py`), as its first item after 10x. The code step starts when that fix is on builds and the go in step 5 has come. Merge builds, then measure the module again in same-load pairs. If the module is at half time, record the result. If not, profile the rest and cut it in the Speed method's order. Keep hold, pressure, queued-head and timing behavior. Leave the mocked nightly calls. Keep every test name, assertion, gate order, module job and type job in all 12 `test_merge_gate*.py` modules. Do not set the spacing to zero or mock the admission result. Add no guard, refusal or check that this Work Order does not name. Projected ledger result after the queue fix: 8-12 s.
8. Later proposal, not this phase: `merge_gate.py` could wake on child exit in place of 0.05 s polls (at most 3.7 s here; owner sdlc devtools-unit).

**Files:**
- `scripts/production/merge_gate_test_support.py` — only for a cut the new profile names
- `scripts/production/test_merge_gate_ledger.py` — only for a cut the new profile names

**Workers:** 1 writer (one module and its fixture; the files do not split)
- `impl` — all files above

**Constraints from prior phases:**
- Phase 25: `MergeGateFixture.build_fixture` commits the real `module_run.py` into each fixture checkout, and the gate refuses a checkout with no runner.
- Phase 26: fixture modules that start the real `send.py` fail through the `TEST_SENDER_GUARD` marker.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.
- Architect review after Phase 41: the module now has 18 tests, and `test_full_test.py` also imports the fixture. Profile the current module after Phase 36, and add `test_full_test.py` to the final run when the fixture changes.
- Architect review after Phase 34: `merge_gate_test_support.py:273` casts `None` to `AdmissionFixture`, and `admission_started` tracks its lifetime apart. A change to that lifetime uses named states (for example `AdmissionNotStarted | RunningAdmission`). A change that only measures needs no refactor.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_merge_gate_ledger.py'` green, with the same test names and assertions
- all 12 `scripts/production/test_merge_gate*.py` modules and `scripts/production/test_full_test.py` green, with the same test names and assertions; each module's before and after time in same-load pairs
- same-load pair medians (`pairs.sh`) before and after, in the checkpoint notice
- `basedpyright` on each changed file prints `0 errors, 0 warnings, 0 notes`

### Phase 28 — The build log isolation test checks every module in parallel  · status: done
**Depends on:** none

#### As-built

- `scripts/buildlog/test_index.py` `LogIsolationTests.test_each_module_uses_a_temporary_log` submits one future for each `scripts/buildlog/test_*.py` module to a `ThreadPoolExecutor` with `ISOLATION_IMPORT_WORKERS = 8`. It reads each result inside that module's `subTest`, so a start error fails only that module, with its name.
- Each module keeps its own Python process, the same command, the environment without `BUILDLOG_DIR`, the same cwd and both checks. One process for all modules is not correct: the first module's `BUILDLOG_DIR` and `store` state stay set.
- Same-load pairs (median of 3): `test_index.py` 26.0 s -> 5.8 s wall, 24.3 -> 26.8 CPU s. The gate on this phase plus builds was green.

**Files:**
- `scripts/buildlog/test_index.py` — the parallel starts in `LogIsolationTests` (report-unit's file)

**Binds later work:**
- A test loop that runs work in parallel reads each result inside its own `subTest`.

### Phase 29 — `test_mac_run.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- `scripts/production/test_mac_run.py` command stubs are zsh scripts in place of a new Python interpreter for each command. They write NUL-separated fields (arguments, labels, stdin), and `events()` reads them as bytes with no newline conversion, into the same `CommandEvent` rows. The held build keeps a Python process with `signal.pause()`.
- The private production config holds two examples (one with an optional feature, one without) in place of nine; both argument forms stay covered.
- The readiness wait uses the shared `wait_for_file` on the published `build-started` file in place of a 0.01 s poll.
- All 17 tests and their bodies are the same. Same-load pairs at load 40 (median of 3): 55.5 s -> 40.5 s wall, 49.8 -> 37.2 CPU s. The gate on this phase plus builds was green.
- Floor: `mac_run.sh` `config_value()` starts `python3 conf.py get` once for each key (339 starts, 23.5 s of a 49.5 s traced run); claim and release in `mac_test.py` take 19.3 s in 35 starts. The proposal is in the next-items file.

**Files:**
- `scripts/production/test_mac_run.py` — zsh stubs, two config examples, the readiness wait

**Binds later work:**
- A fixture log that holds raw arguments is read as bytes.

### Phase 30 — `mac_run.sh` reads its settings with no process for each key  · status: done
**Depends on:** Phase 29

#### As-built

- `scripts/production/mac_run.sh` sources `scripts/lib/conf.sh` once and reads its 18 keys (17 `production`, one `mac_test`) with `conf_value` in place of a `python3 conf.py get` start for each key. Keys, read order and `|| exit 2` paths are the same.
- `scripts/lib/conf.sh` runs under bash 3.2 and zsh. Under zsh it finds its folder with `${(%):-%x}`. It reads the kernel host name once when sourced (`/proc/sys/kernel/hostname`, else one `uname -n`) and ignores inherited `HOSTNAME` and `HOST`. It removes one final CR from a line and gives any line with another CR to `conf.py`, which splits lines with `str.splitlines()`. A key with a `# range:` limit (`mac_test.connect_timeout_s`) still goes to Python.
- `scripts/lib/test_conf_sh.py` runs every reader check under bash and under zsh (`ZshShellConfTests`), plus tests for an inherited host name, CR-only lines and CRLF lines.
- Per real mac_run: config reads 0.712 s -> 0.077 s (median), Python starts 18 -> 1. Same-load pairs of `test_mac_run.py` at load 20-26 (median of 3): 34.6 s -> 20.5 s wall, 33.6 -> 19.9 CPU s. The gate on this phase plus builds was green.
- The `mac_test.py` claim and release import profile went to the next-items file; the code change there is split between natest (after Phase 24) and resources.

**Files:**
- `scripts/production/mac_run.sh` — one `conf.sh` source; `conf_value` for each read
- `scripts/lib/conf.sh` — zsh support, host name at source time, CR delegation
- `scripts/lib/test_conf_sh.py` — bash and zsh runs of the reader checks

**Binds later work:**
- A shell reader of config gives the same value and exit as `conf.py get`, or gives the read to `conf.py`.
- `review.sh`, `implement.sh` and `mac_run.sh` source `conf.sh`; a change to it runs their tests.

### Phase 31 — `test_verify_lint_judgment.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- `test_compile_errors_and_unknown_101_repair_and_return_final_judgment` runs each of its 5 compile diagnostics once, with the final status alternating 0, 101, 0, 101, 0, so each diagnostic and each status stays checked. Test names and assertions are the same.
- `test_verify_build_key.py` uses the fixture's one admission service lookup; the second lookup and its import are gone.
- Same-load pairs (median of 3): 237.4 s -> 198.6 s wall, 180.9 -> 148.5 CPU s; `test_verify_build_key.py` 4.0 -> 3.9 s. The gate on this phase plus builds was green.
- Floor: the production route takes 3.1 s per `verify.sh lint` run with no repair and 5.2 s with repair: Python starts for step-record writes (`verify.sh`) and admission clients (`scripts/lint/memory_gate.sh`), plus about 2.0 s of admission start spacing for each later run in one test. Half time (107.5 s) is under that floor. The repair decision has no callable home (`verify.sh:3711-3714`), so the status tables stay end-to-end runs.

**Files:**
- `scripts/direct/test_verify_lint_judgment.py` — the compile-error diagonal
- `scripts/direct/test_verify_build_key.py` — one admission lookup

**Binds later work:**
- A verify or merge-gate fixture does not advance the admission clock by hand; queue-unit fixes the start spacing in `scripts/build_queue/admission_test_support.py`.

**Ruled out:**
- A manual `allow_next_start()` advance in the verify fixtures: a workaround for the spacing (showrunner ruling).

### Phase 32 — `smoke_launch.sh` reads its settings with no process for each key  · status: done
**Depends on:** none

#### As-built

- `scripts/production/smoke_launch.sh` sources `scripts/lib/conf.sh` and reads its 9 settings with no Python start for each key. Each read keeps its key, order, variable name and exit. `user_zone.py` and `process_group.py` still start as before.
- `conf.sh` gives a read it cannot answer to `conf.py` through the `py` launcher in the same folder, held in one global `_CONF_LIB` (`.` for a bare file name). `py` honors `CLAUDE_PYTHON` and finds Python 3.12 or newer.
- `py_floor_test_support.seed_interpreter_cache(cache_home, interpreter=sys.executable)` writes the interpreter cache that `py` reads. `test_conf_sh.py` and 8 `scripts/direct` fixtures use it.
- Settings read of one real smoke launch (median of 3): 0.770 s -> 0.076 s. `test_smoke_launch.py` same-load pairs: 1.1 s -> 1.1 s. The full gate on this phase plus builds was green.

**Files:**
- `scripts/production/smoke_launch.sh` — one `conf.sh` source for its 9 settings
- `scripts/lib/conf.sh` — shell settings reader; fallback through `py`
- `scripts/lib/py_floor_test_support.py` — `seed_interpreter_cache`
- `scripts/lib/test_conf_sh.py` — fixture with a `bash` link and a seeded cache

**Gotchas:**
- A fixture whose PATH holds only a stub folder needs a `bash` link, because `py` starts with `#!/usr/bin/env bash`. Without a seeded cache, `py` runs the stub once more to check its version.

**Ruled out:**
- Starting `scripts/build_hold/cargo`, the BRP launch hook and `scripts/lint/lint` through `py` in this phase: outside its scope; a next item after the PYTHONPATH item.

### Phase 33 — `test_fast_track.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- `scripts/production/test_fast_track.py` starts no Python watchdog for each Git call. Only `test_git_wrappers_stop_a_blocked_hook` installs `GIT_HANG_GUARD`, which runs Git under `timeout -s KILL`; the other wrappers start Git directly.
- Same-load pairs (3 runs): 168.5 s -> 97.9 s wall, 106.9 s -> 52.9 s CPU. Half time (84.25 s) is not reached. The full gate on this phase plus builds was green.
- Floor at that load (about 101 s): production Git 42.9 s, fast-track CLI children 44.0 s, blocked-hook deadlines 10.0 s, promotion retries 4.0 s. Test Git takes 2.7 s.
- Architecture proposals sent to the showrunner: one injected Git runner in `fast_track.py` (2,937 Git calls, 27.6 s); the repeated `rev-parse` (898), `update-ref` (777) and `cat-file` (283) calls around `fast_track.reconcile`; 403 fetches through the `copies.py` remote refresh.

**Files:**
- `scripts/production/test_fast_track.py` — Git wrappers and the blocked-hook guard

**Gotchas:**
- A bash job-control guard (`set -m`) fails three ways: a hook child that keeps stdout open survives Git, Git cannot read terminal input (SIGTTIN), and a signal exit becomes an ordinary exit code. `timeout -s KILL` has none of these faults.

**Ruled out:**
- Deleting `GIT_HANG_GUARD` and the blocked-hook test (simplify F009): the Work Order keeps every test name, and the test proves the guard stops a blocked hook.

### Phase 34 — Every test module imports from the repo root with no PYTHONPATH  · status: done
**Depends on:** none

#### As-built

- `docs/as-built/herdr-adoption.md:343` runs Mac suites from the clone root as `~/.claude/scripts/lib/py -m unittest discover -s scripts/<suite> -p 'test_*.py'`, with no PYTHONPATH. Proof: the herdr suite in that form with PYTHONPATH unset, 408 tests OK in 89.5 s in a `copies.py` clone.
- Survey (708 modules on 58dbae565): the path form `python3 -m unittest scripts/<folder>/<module>.py` fails on import for 143 modules in 17 folders, because they import siblings by bare name. The gate's form (`discover -s <folder>`) passes for every module. No code runs the path form: `module_run.py`, `fast_track.py` and `coverage_overlap.py` use discover.
- The report unit owns the `PYTHONPATH=scripts/buildlog` Test line at `docs/plans/builds-report.md:76`.
- Next item filed: three `scripts/lib` helpers named `test_*.py` hold no tests (rename proposal, held).

**Files:**
- `docs/as-built/herdr-adoption.md` — the Mac suite command at line 343

**Ruled out:**
- An `__init__.py` in each of the 17 folders that appends its folder to `sys.path` (sdlc, 2026-11-05): the folders then share one bare-name space, and `inventory` (`feature_log`, `shutdown`) and `report` (`buildlog`, `model_study`) clash.
- Qualified imports in every folder: a runtime change in 17 folders of many units.

### Phase 35 — A checkpoint merge writes its unit as the Producer of each change-review row  · status: done
**Depends on:** none

#### As-built

- `merge_checkpoint.Unit` carries a required `staff_id` from `unit_from_row`. `record_changes` and the pre-push read pass it to `change_review.publication_changes(known_producer=...)`, and every row of the merge gets it. Producer no longer depends on the checkpoint subject slug.
- `merge_gate.py` passes `change_review.unit_producer(args.unit)`. It calls `unit_lookup.unit_row`, which reads the whole staff record, for a bare or a `-unit` name. With no row, more than one row or an unreadable staff record, it returns `UNKNOWN_PRODUCER`, and the branch lookup runs, so the gate gains no refusal.
- `publication_changes` reads `_publication_branches`: the merging production's units first, then every other unit on this machine, with live rows before retired rows. A checkpoint on any unit's branch writes that unit's id and its own Production, also when the production document path differs from the staff path. `unit_branches` and its other readers stay scoped.
- The `change_review.py add` command line takes Producer from `--repo`. natedev's rows came out empty because its merge script ran `add` from `/etc/nixos` with no `--repo`; natedev fixed its script. Rows already on the list stay as they are.
- New test `test_unit_merge_with_an_unmatched_slug_writes_the_units_staff_id` fails on the base and passes on the phase. `test_a_successful_publication_notes_an_unregistered_unit_once` became `test_a_merge_keeps_its_units_producer_after_the_staff_row_goes`, because it expected the defect.
- Six more tests pin the wider lookup: an unrelated unit, a different production path, bare and canonical names, duplicate and absent names, a scoped alias, and a live unit over a retired unit on one branch.
- The rows with Production builds and an empty Producer went to the showrunner; the list is unchanged.

**Files:**
- `scripts/production/merge_checkpoint.py`, `scripts/production/change_review.py`, `scripts/production/merge_gate.py` — the fix
- `scripts/production/test_change_review.py`, `scripts/production/test_merge_checkpoint_rerun.py`, `scripts/production/test_waiting.py` — the tests and one fixture

**Gotchas:**
- `merge_gate.py` has no test of its own for an unmatched slug; the showrunner asked for one test, on the merge path.

**Ruled out:**
- A staff snapshot passed from the gate lookup through `publication_changes` in this phase. The merge of builds at 3145e7df3 brought that `record` keyword. `publication_changes` now takes both `record` and `known_producer`, and `merge_checkpoint.py` passes both.

### Phase 36 — A unit's run is a full test, and only a merge is a merge gate  · status: done
**Depends on:** none

#### As-built

- `scripts/production/full_test.py --checkout <tree> [--full]` runs `merge_gate.run_tests_only(args, SuiteMode.FULL_TEST)` and selects affected modules. `merge_gate.py --tests-only` runs all modules under `SuiteMode.MERGE_GATE`; an unswitched caller gets that correct run.
- `run_suites(..., mode=)` carries the mode into the build-queue step, the build-folder prefix, `BUILDLOG_CALLER` and the gate record.
- `config/build_steps.conf` holds the shown names. `scripts/lib/build_steps.py` has `read_step_names` and `StepNames.shown`, `.prose`, `.memory_cap_text`. The step id `full-test` shows as `full test`; `merge-gate` and `build` stay as ids.
- `config/build_folders.conf` has `prefixes.full-test` and `prefixes.suite-module`. `config/build_admission.conf` has `peak_hold_seconds.full-test=180`.
- `gate_record` has required `step`, and `dirty_paths`, `dirty_path_count` for a dirty checkout. `buildlog/index.py` is schema 34 with `step`.
- The queue files, the build-hold enforcer and hook, as-built docs, `docs/direct/write_prompt_contract.md` and the unit plans use the new names.
- Live check: the queue page showed `7▸builds  natest  full test  2 pid 1674416 ██████ 6.0`.
- Phase 48 owns the raw step id in the running and release sentences, the three copies of the suite-runner names, and `build_hold.py:493`.

### Phase 37 — A unit's full test runs under 40% of the test modules  · status: todo
**Depends on:** none

#### Work Order

**Goal:** a unit's full test selects the modules its change touches directly, and natedev spends less CPU on gates. The user asked for this through the queue unit, 2026-11-05.

**Spec:** a missed module now costs one red merge, not a red branch, because Phase 36 makes the merge gate run all modules. Replay of 104 unit runs (estimates, run folder, `phases/37/replay/report.md`): today's selection has a mean of 6285 CPU s per run, a clean-run median of 650 modules and 4 runs at most 40%. This selection has a mean of about 3600 CPU s, a clean-run median of about 146 modules, 44 runs at most 40%, 25 module misses and 8 red merges. The next-narrower variant has 157 misses and 19 red merges, and it leads only while a red merge costs under about 8000 CPU s; a red merge also stalls the chain. Showrunner, 2026-11-05: replay first, this set, Phase 36 stands.
1. `select_affected` selects a test module when any of these holds:
   - the module itself changed;
   - its source imports a changed Python file directly, one import step, with no further search;
   - its source names a changed repository-relative path as a complete token, with a leading `./` allowed;
   - it sits in the folder of a changed non-test Python file.
2. A changed non-Python file reaches a test only through the "names it" rule.
3. A deleted or renamed Python file uses the import facts at the base for the same one step. Its surviving direct importers are selected.
4. The changed set is the diff from the base to `HEAD` plus the dirty paths of the tree. A dirty tree no longer means a full run.
5. Delete the full-run triggers: `tree not clean`, renamed Python file, deleted Python file, `full_suite_trigger`, the 40% limit, the orphan-folder rule and the tree-scanning always-selected set. Delete the filename-mention edges, the transitive search and the code they leave unused. Bump `EXTRACTOR_VERSION` if source facts change.
6. A full run comes only from `--full`, `no certified base` or a graph error. The nightly-red marker no longer widens a full test; it stays for the merge and the nightly run.
7. Do not target `scripts/agents/test_codex_mesh.py`: the user removes it within 48 hours.
8. Add no guard, refusal or check (user rule, 2026-11-05). The nightly full run stays.
9. Land after Phase 36: until the merge gate runs all modules, this selection must not reach a merge. The worker first changes `affected_modules.py` and its tests. After the Phase 36 checkpoint, a second step changes `merge_gate.py` and `full_test.py` for the removed `tree_scanners` field and the nightly-red marker.
10. If selections fall as the replay says, send the showrunner a pass cache proposal with a new estimate. It saved 13% of CPU at the old closures.

**Files:**
- `scripts/production/affected_modules.py` — the selection
- `scripts/production/test_affected_modules.py` — tests
- `scripts/production/merge_gate.py`, `scripts/production/full_test.py` — after the Phase 36 checkpoint: the removed field and the nightly-red marker

**Workers:** 1 writer on slot `impl` (one selection function and its tests)
- `impl` — all files above

**Constraints from prior phases:**
- Phase 36: `full_test.py` selects and `merge_gate.py` runs all modules. Its checkpoint comes first.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- Replay of the same 104 unit runs on the final code gives the K row of the report, within its ranges: full runs, median modules, misses, red merges. Quote before and after in the checkpoint notice, and mark them as estimates.
- Measured CPU per unit run, before and after, in the checkpoint notice. Before: the mean CPU of the unit runs in the merge-gate records of the 24 hours to the Phase 37 start. After: the final `full_test.py` on 5 recent unit commits, in clones, with the sample size stated. These numbers go to the user.
- Each of the four selection rules has a test with a real scratch repository. A deleted Python file selects its surviving direct importers, and a dirty path selects as a changed path.
- A clean tree with no reach selects no module and passes.
- `test_affected_modules.py`, `test_full_test.py` and each `test_merge_gate*.py` green in the gate form, and the full test green on the phase commit merged with `origin/builds`.
- After landing, the checkpoint's Change-Review line: the user reads `full test` runs on the queue page and sees fewer modules.

### Phase 38 — Three scripts start Python through `scripts/lib/py`  · status: done
**Depends on:** none

#### As-built

- `scripts/build_hold/cargo`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh` and `scripts/lint/lint` start Python through `scripts/lib/py`, with the same arguments, stdin and exit status. Filename-only and symlink starts of these scripts resolve the launcher.
- `scripts/mac_test/test_offload.py` runs the two lint routes with a cargo stub. `test_cargo_wrapper.py` and `test_brp_launch_gate.py` seed the interpreter cache with `seed_interpreter_cache`.
- `pyrightconfig.json` has `"."` in the `scripts/mac_test` extraPaths.

| Route | Machine | Before | After |
| --- | --- | --- | --- |
| BRP launch hook, labelled launch | Mac | `rc=0`, Python Traceback | `rc=0`, one `memory_gate.sh:160` warning |
| BRP launch hook, filename start | Mac | not run | `rc=0`, deny JSON that names `HANA_TEST_PURPOSE` |
| lint_scope | Mac | `rc=0`, `--workspace` | `rc=0`, `--workspace` |
| cargo hold | Linux clone | `rc=1`, the hold message | `rc=1`, the hold message |

### Phase 39 — `test_verify_mac_only.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- No repository change. The phase ended at a measured floor, with every test name and assertion kept.
- Three clean rounds at base f43735e60: median 156.5 s wall, 144.4 CPU s, 17 tests. Load changed during the rounds, so they find the cost and do not measure a gain.
- The 84 `verify.sh` calls take 131.7 s of a 134.7 s traced run (97.8%). The parent spends 3.0 s outside those calls. The module has no large fixed wait: the four wait tests use a stub or a signal.
- Floor: process starts. `build_targets.py` has 366 starts and 51.5 CPU s. `mac_idle.py` has 82 starts and 25.9 CPU s. Both import the coordination stack at start (`build_targets.py:137-153`, `mac_idle.py:25-53`).
- Proposal, sent to the showrunner 2026-11-05: lighter imports for each command in those two files, and fewer starts where read-only queries can share one. Estimate: 54-62 CPU s in this module at a 70-80% cut. The same cause limits Phases 24 and 31.

**Ruled out:**
- Another seed in `verify_mac_offload_test_support.py`: the fixture already builds one seed per class and copies it for each test, and the cost is not there.

### Phase 40 — `style/test_crate_run.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- No repository change. The phase ended at a measured floor, with every test name and assertion kept.
- Three clean rounds at base 58b84efc2: median 257.2 s wall, 67.9 CPU s, 97 tests, at load 45-75. Load changed during the rounds, so they find the cost and do not measure a gain.
- The module has no fixed wait, and repeated setup is at most 31.8 s (9.8%). One diagnostic run records 24,316 process starts and 211 s in `subprocess.run`.
- Floor: production Git work. `copies.refresh_remotes` has 803 calls and 99.7 s (1,606 fetches, 3,212 config writes, 1,606 update-refs). `style/bulk_run.py` and `style/git_process.py` have 10,444 Git calls and 60.9 s. Durable publications have 5,777 fsync calls and 44.4 s.
- Proposal, sent to the showrunner 2026-11-05. It assigned the `copies.py` part as Phase 44. The other parts are estimates and have no owner phase.

**Ruled out:**
- A per-class seed: an earlier phase gained nothing (128 s to 126 s).

### Phase 41 — `build_targets.py` loads only what its command needs  · status: done
**Depends on:** none

#### As-built

- No repository change. The phase ended at a measured floor, and the trial did not land.
- The trial moved about 60 imports inside functions in `build_targets.py`. Its 430 caller tests passed and `basedpyright` was clean.
- Same-load pairs of `test_verify_mac_only.py`, 17 tests, load 60-75: median 212.0 to 207.7 s wall (2%) and 182.9 to 181.1 CPU s (1%). The Phase 39 estimate of 54-62 CPU s was not met. The pair ended the trial, so the real `verify.sh` measure did not run.
- Floor: `platform` already had its early path (246 of 366 starts). `eligible` and `reserve` still load `targets` and `build_hold`, at 345-384 ms per start. `build_hold.py` is frozen, and queue-unit owns its lazy import.

**Ruled out:**
- Function-local imports in `build_targets.py` alone: 1% CPU does not pay for about 60 imports inside functions.
- One start for adjacent `eligible` and `platform` queries in `verify.sh`: about 1.6 CPU s.

### Phase 42 — `mac_idle.py` loads only what its command needs  · status: todo
**Depends on:** Phase 41
**Blocked by:** the `send.py` freeze end; the showrunner says when.

#### Work Order

**Goal:** a `verify.sh` run spends less CPU in `mac_idle.py` starts, with the same behavior. Phase 39 measured 82 starts and 25.9 CPU s in `test_verify_mac_only.py`.

**Spec:**
1. Measure each command of `scripts/buildlog/mac_idle.py`, then make a trial where each command imports only what it uses (`mac_idle.py:25-53`).
2. Pair the trial before any review. The whole cost is 25.9 of 182.9 CPU s. Land only when the pair median falls by at least 5% CPU; otherwise end at a floor with no repository change (Phase 41 gained 1% with this method).
3. Behavior does not change. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/buildlog/mac_idle.py` — imports for each command

**Workers:** 1 writer on slot `review3` (one script; the file does not split)
- `review3` — all files above, role impl

**Constraints from prior phases:**
- Phase 41: the same method for `build_targets.py` gained 1% CPU and did not land. Function-local imports pay only when a whole heavy chain leaves the command.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- Measured CPU before and after in the checkpoint notice: `test_verify_mac_only.py` in same-load pairs (`pairs.sh`), and one real `verify.sh` run.
- Each caller module of `mac_idle.py` green, with the same test names and assertions.
- the full test green on the phase commit merged with `origin/builds`.
- `basedpyright` on the changed file prints `0 errors, 0 warnings, 0 notes`.

### Phase 43 — `direct/test_verify_token_renew.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/test_verify_token_renew.py` runs in half its time, with every test name and assertion kept. Same-load pair medians: 145.84 s to 73.76 s wall, 57.93 to 53.42 CPU s.
- The module waits for the first expired second, not for fixed seconds after it. A short `TOKEN_GUARD_ATTEMPTS` and `TOKEN_GUARD_DELAY_MS` config makes `test_busy_board_guard_does_not_stop_memory_wait_renewal` reach status 2 in milliseconds.
- The remaining CPU is 11 real `invoke_verify` calls. Step-record starts are 225 and 19.96 process s; admission and release commands are 44 and 25.17 process s. The showrunner routed these starts to the verify unit and the queue unit.
- Phase 50 owns the write watch and the one inotify binding (Phase 43 F005, F006).

### Phase 44 — `copies.py` does less Git work  · status: todo
**Depends on:** none
**Blocked by:** verify P35 on GitHub builds — it moves every copy maker onto `copies.py`; the showrunner says when.

#### Work Order

**Goal:** each `copies.py` use does less Git work, with the same copies. Phase 40 measured 803 `refresh_remotes` calls, 1,606 fetches, 3,212 config writes and 1,606 update-refs, about 100 s of a 325 s profiled `style/test_crate_run.py` run. The showrunner assigned this on 2026-11-05 and moved queue's item to it.

**Spec:**
1. Measure before any edit: `style/test_crate_run.py` (same-load pairs) and one real `copies.py run`.
2. `refresh_remotes` (`scripts/copies/copies.py:372`) does only the work a changed source needs.
3. Remove the full object scan from `copies.py` initialization (0.39 s per `verify.sh doc` run; item from queue-unit).
4. Queue-unit keeps the `copies.py` list labels item. On `copies.py`, the second lander resolves.
5. Behavior does not change: same trees, same remote refs, same exit status. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/copies/copies.py` — the cuts
- `scripts/copies/test_copies.py` — tests for the changed paths

**Workers:** 1 writer on slot `impl` (one module and its tests; the file does not split)
- `impl` — all files above, role impl

**Constraints from prior phases:**
- Phase 40 measured the cost; the module itself is at a floor.
- Verify Phase 35 replaces the `None` source and merge policies of `copies.standing` with named types. Build on those types.
- Recovery from missing borrowed objects stays (`copies.py:349`, `test_copies.py:980`). If the refresh error path changes, replace `PostUseError.caller_error: BaseException | None` (`copies.py:110`) with named caller outcomes.

**Acceptance gate:**
- caller suites for `copies.py` green
- measured before and after for `style/test_crate_run.py` and one `copies.py run`, in the checkpoint notice
- the full test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`

### Phase 45 — `direct/test_verify_lint_cache_versions.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- No repository change. The phase ended at a measured floor, with every test name and assertion kept.
- Three clean rounds at base 5d459b8bb with the helper trace on: median 135.9 s wall, 94.0 CPU s, 7 tests, load 41-75. Load changed during the rounds, so they find the cost and do not measure a gain.
- The module has no fixed wait, and the fixture already builds one seed per class. Five tests make 29 full `verify.sh lint` calls at 22-34 s per test; two tests call the code in-process and take under 0.2 s.
- Floor: production process starts. `step_record.py` has 486 starts and 23.1 CPU s (about 17 starts per `verify.sh` call), `tree_watch.py` has 155 starts and 7.8 CPU s, and `build_folders.py` has 50 starts and 3.2 CPU s. The trace attributes 40.0 of 94.0 CPU s.
- Proposal, sent to the showrunner 2026-11-05: this module measures verify-unit's step-record reuse item at up to 21.7 CPU s (estimate, 457 of 486 starts).

**Ruled out:**
- Another seed or a removed test: the four named-tool tests each check a different tool, and the fixture has no repeated setup.

### Phase 46 — style test runs do less Git work outside `copies.py`  · status: done
**Depends on:** none

#### As-built

- No repository change. One traced run at 54e560258, load 80-93: 97 tests, 319.0 s wall, 79.5 CPU s; `bulk_run.py` and `git_process.py` make 10,582 Git calls in 69.7 s.
- Most of those calls read mutable state or write. Those stay: `status`, `diff`, `ls-files`, `symbolic-ref`, branch `rev-parse`, and all writes.
- Reads that a cache could cut, as upper bounds: `rev-parse` on object ids 9.4 s, `hash-object` 5.0 s, `ls-tree` 4.6 s, `cat-file --batch` 2.8 s, `check-ref-format` 2.0 s, `require_base` 1.3 s. The sum is 25.2 s (7.9%), and each distinct input still needs its first read, so the real gain is under the 5% bar. A cache for that gain is more code than the gods accept.
- The largest removable lead is outside this phase: `bulk_run.py:1048`, `test_tree`, delegates 1,652 Git calls (24.1 s) to `copies.standing`. Phase 44 owns it.
- Root and common-directory reads (5.6 s upper bound) come from `style_ledger.py`, outside this phase.

### Phase 47 — test runs spend less time forcing writes to disk  · status: done
**Depends on:** none

#### As-built

- No repository change. The merge gate and the full test already run every module with `TMPDIR` under `/dev/shm` (`merge_gate.py:958`, `merge_gate.py:1048`, `roots.gate` in `config/build_folders.conf`), so these runs have no gain left.
- Two rounds of `style/test_crate_run.py` through `module_run.py` at f031f0464, with load 77-89: normal 196.2 and 176.0 s wall, 68.0 and 67.5 CPU s; `TMPDIR` on tmpfs 136.8 and 132.6 s wall, 62.6 and 63.0 CPU s. The tmpfs gain is real (about 29% wall), and the gate has it now.
- Peak tmpfs use of that module: 1508 MiB. The other two planned modules did not run, because the gate fact decides the phase.
- Method correction: `module_run.py` runs on the normal disk, so a paired number includes `fsync` cost that the gate does not pay. The 44.4 s `fsync` cost from Phase 40 does not apply to the gate.

**Ruled out:**
- Moving `module_run.py` to tmpfs as a saving: it times one module for a director, and it is not part of a suite run.

### Phase 48 — The queue's sentences say `full test`, and one file lists the suite runners  · status: done
**Depends on:** Phase 36

#### Work Order

**Goal:** every queue sentence shows the step name that the user reads, and one file holds the suite-runner names. Phase 36 left both, under the landing rule.

**Spec:**
1. `queue_evidence.running_text` (`queue_evidence.py:299`) and `queue_evidence.release_text` (`queue_evidence.py:224`) print the raw step id, so a card can say `full-test`. Print `names.prose(step)` with a required `names: StepNames` keyword, as `need_text` does. `queue_card.py` passes `model.names`.
2. `hold_enforcer.py`, `scope_activity.py` and `pre-tool-use-build-hold-guard.py` each define `SUITE_RUNNERS` and `SUITE_MODULES`. Add `scripts/lib/suite_runners.py` with these two sets and one `starts_suite_runner(target)`, standard library only. `scope_activity.py` keeps its own scan of every argument and uses the two sets.
3. If the admission service imports the new file, add it to `admission_code.ADMISSION_CODE_PATHS`.
4. `build_hold.py:493` stays on hold until the showrunner sends natedev messaging's main hash. When the hash comes, that line uses `SUITE_RUNNERS` in this phase or in a small follow-up.
5. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/build_queue/queue_evidence.py`, `scripts/build_queue/queue_card.py`, `scripts/build_queue/test_queue_evidence.py`, `scripts/build_queue/test_queue_card.py`, `scripts/build_queue/test_admission_protocol.py`, `scripts/lint/test_memory_admit.py` — item 1 (owner: queue unit)
- `scripts/lib/suite_runners.py` — new file
- `scripts/build_hold/hold_enforcer.py`, `scripts/build_queue/scope_activity.py`, `scripts/build_queue/admission_code.py`, `scripts/hooks/pre-tool-use-build-hold-guard.py` — item 2
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — item 4, only after the hold ends

**Workers:** 1 writer on slot `impl` (few lines; slot `test` holds Phase 37)
- `impl` — all files above

**Constraints from prior phases:**
- Phase 36: `StepNames` reads config once per queue model, and renderers read no file.
- The hook starts on every Bash call, so `scripts/lib/suite_runners.py` imports only the standard library.

**Acceptance gate:**
- A test with a running `full-test` step and a running `merge-gate` step: `running_text` and `release_text` print `full test` and `merge gate`, and never the raw step id.
- `python3 -m unittest discover -s scripts/build_queue -p 'test_*.py'` green, with `scripts/hooks/test_build_hold_guard.py` and the `scripts/build_hold` modules
- `git grep -n 'SUITE_RUNNERS = '` finds one line
- the full test green on the phase commit merged with `origin/builds`

### Phase 49 — Every shell script starts Python through `scripts/lib/py`  · status: todo
**Depends on:** Phase 36

#### Work Order

**Goal:** no shell script starts a bare `python3`, so a Mac with Apple's Python 3.9 first on PATH starts the right interpreter. The showrunner chose this sweep, 2026-11-05 (Phase 38 finding F006).

**Spec:**
1. The 17 starts: `berth_fix/capture.sh:44`; `berth_fix/launch.sh:44,69,82,101,116`; `bevy_migration_plan/bevy_migration_count_pattern.sh:48`; `direct/board.sh:173,230,287`; `lint/memory_gate.sh:21,124,148`; `production/mac_run.sh:74,96`; `statusline/statusline.sh:66,76`. Find the current line numbers with `git grep`. Also: `agents/test_agent_exec.sh` (7 starts), `agents/test_launcher_tasks.sh:28`, `lib/test_models_registry.sh:7`. `direct/verify.sh:259` and `rust_style/load-rust-style.sh:8` default `CLAUDE_PYTHON` to `python3`: change the default only if the readers can start it. Before the first edit, run the survey `git grep -nE '\bpython3\b' -- '*.sh'` again.
2. Keep each start's arguments, stdin and exit status. Use the Phase 38 form, so a filename-only start and a symlink start still resolve the launcher.
3. `statusline.sh` and `board.sh` start often. Measure the time of one start before and after, and put both numbers in the notice.
4. Workers run `board.sh` in a sandbox, so run one `board.sh post` in a sandboxed worker before the checkpoint.
5. A script on hold when this phase starts waits for the end of its hold.
6. Add no ban check, guard or refusal (showrunner, 2026-11-05; user rule, 2026-11-05).

**Files:**
- `scripts/berth_fix/capture.sh`, `scripts/berth_fix/launch.sh`
- `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh`
- `scripts/direct/board.sh`
- `scripts/lint/memory_gate.sh` — tell the queue unit before the edit; it owns line 160
- `scripts/production/mac_run.sh`
- `scripts/statusline/statusline.sh`
- `scripts/agents/test_agent_exec.sh`, `scripts/agents/test_launcher_tasks.sh`, `scripts/lib/test_models_registry.sh` — shell tests with starts
- `scripts/direct/verify.sh`, `scripts/rust_style/load-rust-style.sh` — the `CLAUDE_PYTHON` default; tell the verify unit before the `verify.sh` edit
- `scripts/mac_test/test_offload.py` — with any other test module that copies these scripts into a fixture

**Workers:** 1 writer on slot `impl3` (one rule across small files)
- `impl3` — all files above

**Constraints from prior phases:**
- Phase 38: a test fixture that copies a script also needs `scripts/lib/py` and `seed_interpreter_cache`.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- `git grep -nE '^[^#]*\bpython3\b'` on the listed files finds no start outside `scripts/lib/py`
- each caller module of these scripts green in the gate form
- the routes of each script on natedev and on the Mac, before and after, with the same exit status, in the notice
- the full test green on the phase commit merged with `origin/builds`

### Phase 50 — One shared wait watches writes, and one file binds inotify  · status: todo
**Depends on:** Phase 43

#### Work Order

**Goal:** a test wakes on a line that a live writer adds to an open log, through one shared wait with no raw masks. The Phase 43 simplify review found the copies.

**Spec:**
1. `scripts/lib/inotify.py` gains `IN_MODIFY`.
2. `LocalReleaseEvents` and `local_release_waiter` take a `mask` parameter. The default stays `0x00000FC8`, written with the named constants, so production callers keep the same events.
3. `wait_until` takes a keyword `watch_writes: bool = False`.
4. `VerifyTokenWaitFixture.wait_for_output` becomes one `wait_until(..., watch_writes=True)` call, then the `assertIn`. A timeout raises `TimeoutError`, not `AssertionError`.
5. Remove the raw-mask patches in `verify_hold_wait_test_support.py` and `verify_mac_offload_test_support.py`.
6. `LocalReleaseEvents` calls `inotify.open_watch`, `inotify.add_watch` and `inotify.events`. `arm()` still skips a path that is not a directory.
7. `scripts/agents/codex_mesh.py` keeps its own copy: it goes within 48 hours (user, 2026-11-05).
8. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/lib/inotify.py`
- `scripts/direct/local_release_wait.py`
- `scripts/direct/wait_for_test_state.py`
- `scripts/direct/verify_token_wait_test_support.py`
- `scripts/direct/verify_hold_wait_test_support.py`
- `scripts/direct/verify_mac_offload_test_support.py`
- `scripts/direct/test_verify_hold_events.py` — the open-writer test

**Workers:** 1 writer on slot `impl4` (one wait, used by every file; slots `impl`, `impl3` and `test` hold other phases)
- `impl4` — all files above

**Constraints from prior phases:**
- Phase 43: the shared mask has no `IN_MODIFY`, so a file that a live writer keeps open sends no event until its last close.
- Turned on for every caller, a write watch wakes on each write in a busy directory, and predicates can read half-written files. Keep it off by default.
- Architect review after Phase 36: keep the production mask equal to `0xFC8`. `inotify.DIRECTORY_CHANGES` also holds `IN_ATTRIB`, so it is not that mask. Keep the recursive subscriptions and the unrestricted event reads of the two fixtures when their patches go.

**Acceptance gate:**
- A test: a live writer adds a line to a file it keeps open, and `wait_until(..., watch_writes=True)` returns before the file closes.
- `git grep -n '0x00000FC'` finds one definition at most, outside `codex_mesh.py`.
- Each caller module of the changed files green in the gate form; `merge_gate.py` waits have the same events.
- Same-load pairs for `direct/test_verify_hold_*` and `direct/test_verify_mac_only.py`, before and after, in the notice.
- The full test green on the phase commit merged with `origin/builds`.

### Phase 51 — `direct/test_simplify_lens.py` runs in less time  · status: todo
**Depends on:** none
**Blocked by:** devtools 7u on main, about 23:30 EST; the showrunner sends the word. Build on main after it lands. 7s rebases on this phase.

#### Work Order

**Goal:** `scripts/direct/test_simplify_lens.py` (179 s wall, 135 CPU s; its Phase 6 floor is 118 s) falls under its floor again, or a new measured floor shows why it cannot, with every checked behavior kept. The Phase 36 architect review picked it: it is the top module over its floor that no phase holds, outside the routes that other units own.

**Spec:** Follow the Speed method.
1. The module had 17 tests at Phase 6, and it has 30 now. Profile before any edit. Name what the added tests cost and what each review-launcher run costs.
2. `test_report_validation_trims_whitespace_and_requires_pass_or_a_numbered_item` runs the review launcher 8 times, and each run checks the report result, the saved status and the record's `status` and `raised` fields. Keep each check and each process boundary that a check needs.
3. Cut in the Speed method's order. A cut in a production file or a support file goes to the director first, with the measured gain.
4. Add no guard, refusal or check (user rule, 2026-11-05). Make any project that a test needs in a tempdir at run time.

**Files:**
- `scripts/direct/test_simplify_lens.py` — the module (recent owner: sdlc-devtools); tell the showrunner at start

**Workers:** 1 writer on slot `impl4` (one module; the file does not split)
- `impl4` — all files above, role impl

**Constraints from prior phases:**
- Phase 6: one real review-launcher run per test takes 3-6 s, and a parse-only call cannot replace the recorder checks.
- Phase 47: pairs run with `TMPDIR` under `/dev/shm`.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/direct -p 'test_simplify_lens.py'` green, with the same test names and assertions
- same-load pair medians (`pairs.sh`) before and after, in the checkpoint notice
- the full test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed file prints `0 errors, 0 warnings, 0 notes`

### Phase 52 — One shared reader for Git blobs, trees and status  · status: todo
**Depends on:** Phase 37

#### Work Order

**Goal:** one reader for each Git batch format. The Phase 37 simplify review found the copies.

**Spec:**
1. Move `style_ledger.blob_contents` (`style_ledger.py:1893`) and `style_ledger.tree_entries` (`style_ledger.py:1862`) into `scripts/style/git_process.py`, standard library only.
2. Remove the copies: `affected_modules.blob_texts`, `affected_modules.revision_files`, `phase_process.py:1046`, `feature_log/inventory.py:285`, `bulk_run.py:794`. Check that each copy has the same behavior before you replace it.
3. `scripts/style/unit_files.py:145-154` uses one `status --porcelain -z` parser with `affected_modules.dirty_paths`. Choose the home with the fewest imports.
4. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/style/git_process.py`, `scripts/style/style_ledger.py`, `scripts/style/bulk_run.py`, `scripts/style/unit_files.py` — owner: style
- `scripts/production/affected_modules.py`
- `scripts/direct/phase_process.py`
- `scripts/feature_log/inventory.py`

**Workers:** 1 writer on slot `impl` (one move, used by every file)
- `impl` — all files above

**Constraints from prior phases:**
- Phase 37: `affected_modules.blob_texts` raises `GraphError` on a failed read, and the selection turns that into a full run.

**Acceptance gate:**
- `git grep -n 'cat-file.*--batch'` finds one framing reader outside a test.
- Each caller module of the changed files green in the gate form.
- The full test green on the phase commit merged with `origin/builds`.
