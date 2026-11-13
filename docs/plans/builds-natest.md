# natest

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Record each `~/.claude` test run, test module and slow test in the build log, show their history at once with `/test_times`, and make the slowest test modules faster, from the top of a descending sort down.

## Source

2026-11-03 09:30 EST

From the user (via natedev): Mission: find the longest-running ~/.claude tests and make them faster. Keep a descending sort of test times; when it makes the longest one shorter, it takes the next one. Build tools that instantly show the history of ~/.claude test suite times and of individual test times. Use parallelism, removal of trivial tests, shared code and good engineering practice. It must also send architectural change proposals back. It organizes its work in phases so /unit:direct works, and keeps its own phase backlog: the user wants the standard process engaged.

2026-11-06 12:15 EST

From the user (via the showrunner): the verify is a good improvement. i think that it needs to recalibrate its approach to test improvement based on this feedback

From the user (via the showrunner, 12:19 EST): natest should be the one doing the test work, of course

The feedback: verify's Phase 41 cut `test_verify_mac_arguments.py` from 56.0 to 30.2 CPU s and from 1,925 to 982 processes. It tests logic by direct calls and keeps a few real runs. Gate throughput depends on CPU, because many gates share the machine.

## Standing work

1. At each phase start, read the merge gate's slowdown warnings; natest-unit owns them. A warned module goes to `builds-natest-next.md`.
2. At each phase end, re-sort with `show.py top`, which ranks by median CPU. Before the last todo phase starts, add a speed phase for the top live module that no phase holds.
3. `builds-natest-next.md` lists each module with a reported floor. A gate median CPU 20% above a floor starts an investigation. A floor from before 2026-11-06 is a wall floor. It does not hold a module back when the module's CPU rank calls it and direct calls were not tried.
4. No CPU stop rule holds the plan. Select each phase by measured removable CPU, and keep owner holds.

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
  - `scripts/production/targeted_test.py` — a unit's run: it selects modules through `scripts/production/affected_modules.py`. `merge_gate.SuiteMode` names the two modes; `run_suites(..., mode=SuiteMode.MERGE_GATE)` runs all modules (Phase 36). The line numbers above are history: find the current lines with `git grep`.
  - `scripts/lib/build_steps.py`, `config/build_steps.conf` — `StepNames.shown` and `StepNames.prose`: the step names that the user reads (Phase 36)
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py`, `scripts/production/test_targeted_test.py` — the gate's fixture and tests
  - `scripts/buildlog/store.py` — `root()` (honors `BUILDLOG_DIR`), `host_name()`, `utc_iso(epoch)`, `host_file(host, epoch)`, `append_line(path, record)`: the JSON-lines store, one file per host per month
  - `scripts/buildlog/record.py` — `RECORD_VERSION`, `new_id(host, epoch)` (513), `ssh_attempt` (1055): the pattern for a one-line record writer
  - `scripts/buildlog/index.py` — `SCHEMA_VERSION = 34`, with `step`, `dirty_paths` and `dirty_path_count` (was 42); column lists such as `TEST_COLUMNS` (141) and `CALL_COLUMNS`; `TABLES` (452, with the gate tables); `create_schema` (574, adds a `<table>_src` index for each table); `insert`, `pick`; `add_step` (763, the pattern for a nested list that becomes child rows); `ADDERS` (950, `gate_run` maps to `add_gate_run`); `forget` (954, loops `TABLES`); `update()` (1012); `read_only()` (1051)
  - `scripts/test_times/gate_record.py` — the `gate_run` record types (`ModuleTimes`, `ModuleRun`, `SuiteRun`, `GateRun`, `NoModuleTimes`), `TYPE_CHECK_JOB` (38), `read_module_times` (131), `kept_tests` (153), `slow_test_s` (162), `write_gate_run` (166) Since Phase 2: `Execution` (106), `module_runs` (185), `report_not_written` (213), `write_gate_run` (219).
  - `scripts/buildlog/cli.py` — runs `index.update()` before each report (140)
  - `scripts/lib/conf.py` — `read(file).number(key)`; a config file puts a `# range: 0..` or `# whole: 1..` line directly above each `KEY=value` (see `config/detached_runs.conf`)
  - `config/README.md` — one entry for each config file
  - `scripts/lib/user_zone.py` — `user_zone()`; `scripts/lib/test_user_zone.py` scans every script and fails an `astimezone()` with no zone
  - `scripts/lib/terminal_cells.py` — terminal cell widths for aligned tables
  - `commands/build/status.md` — the form of a command that runs one script and shows its lines unchanged
  - `docs/as-built/merge-gate-and-test-speed.md` — the gate's as-built and its test rules: no live state, no wall-clock thresholds, no fixed waits in the code under test, few process launches, no trivial tests
  - `~/.local/state/merge-gate/module_history.json` — the gate's last 5 wall times for each module, with no times of day; it seeds the first sort only
- **Test lanes:** none as `tests/` folders. Each module's tests sit beside it as `scripts/<folder>/test_<name>.py`; the `test` worker writes those files.
- **Build:** none (Python).
- **Test:** for each named test file, `python3 -m unittest discover -s scripts/<folder> -p '<file>'` from the worktree root; always also `python3 -m unittest discover -s scripts/lib`. A worker's verification lists every test module that calls a changed function, unchanged callers and subprocess fixtures included (Phase 18: a caller's adjacency test was missed). Checkpoint gate: merge origin's merge branch first, then the targeted test: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/targeted_test.py --checkout <tree>` (main's copy). Until Phase 36 is on main, main has only `merge_gate.py --checkout <tree> --tests-only`, which runs all modules.
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

- **Speed method** (every speed phase; CPU first, 2026-11-06):
  0. Targets: at dispatch, the director takes the top live rows of `show.py top`, ranked by median CPU per gate, that no phase holds. When they differ from the modules the phase names, the director rewrites its Goal, Files and acceptance first. `types/basedpyright` is never a target; it goes to its architecture proposal.
  1. Baseline: before the edit, run `python3 scripts/test_times/module_cost.py --tree <tree> <folder>/<file>` for CPU s, processes, execs and wall s. Run the module with `TEST_TIMES_OUT=<run directory>/<module>-before-<n>-<sha>.json python3 scripts/test_times/module_run.py scripts/<folder> <file>` for each test's time. Read `python3 scripts/test_times/show.py module <folder>/<file>` for the gate history.
  2. Find the cost: the process starts per test, what starts them, and the CPU per test (`module_cost.py --tests 10`). A process count does not rank CPU: Phase 61's SSH files lost 52% of their processes and no CPU, because a Python start and a live server cost far more than a Bash start. Add up CPU by command (`module_cost.py --commands 10`) before a move. CPU in a production command that the tests need goes to its owner: no test edit removes it (Phase 66). Name each real end-to-end run and what only it proves. Do not apply a cut the module already has. Estimate each command's removable cost, not the whole script's: Phase 41 estimated 54-62 CPU s and measured 1.8, because 246 of 366 starts already had the light path and two commands kept the heavy chain.
  3. Cut it, in this order:
     - Test logic by direct calls of the functions under test. Use `scripts/lib/shell_test_support.py` for Bash functions, and run many cases in one shell where the cases allow it. Keep a few real end-to-end runs: each one proves a path that no direct call can prove. Write a behavior map with one row per assertion: the test that checks it after the phase, and whether that test is a direct call or an end-to-end run. For each moved check, show on a tempdir copy that breaking the function fails the new test (verify Phase 41). A direct call keeps the decision that the test proves. A fixture that plays that decision itself cuts CPU and removes the proof (Phase 66).
     - Share expensive setup: build a seed once in `setUpModule` or `setUpClass` and copy it for each test, as `scripts/build_hold/test_build_hold.py` does. When files share a fake tool or a fixture, change the fixture once. Make no copy of a fixture.
     - Remove trivial tests (Invariants above) only when step 2 shows tests that read as repeats: tests with the same setup and the same assertion target. Then run `python3 scripts/test_times/coverage_overlap.py scripts/<folder> <file>` and compare each candidate's assertions with its covering tests; a candidate whose result a covering test also checks is a trivial test. Skip the sweep in a module whose time is in process starts: Phase 13 found no repeat in five such modules.
     - Remove a fixed wait: a sleep, a poll count, or a wait that no published state ends. Wait on the event, file, pipe or ready signal the code already gives. This cut gains wall time and no CPU (Phase 56: 30.5 to 31.1 CPU s), so it does not reach the target alone. A fixed wait often causes flakes, so a flaky module is a target too.
     - Split a module only to gain wall time. A split adds CPU, so it never counts as a CPU gain. The original file and its new files are one group: compare the group's summed CPU, its test count, and its wall time when its files run at the same time.
     - Cuts add up. A cut that cannot reach half alone still counts, so state a floor only after the available cuts. Look first for repeated first launches that are setup (Phase 55).
  4. A change to a shared support file (for example `scripts/lint/memory_test_support.py`) is allowed only when every module that imports it stays green; name those importers in the final run. A cut in `verify.sh` or another production file goes to its owner: verify for `verify.sh` (showrunner, 2026-11-06).
  5. After the edit, measure the gain. Run `module_cost.py` on the phase base and on the result, one after the other, for processes and execs. Same-load pairs give CPU s and wall s: run the module at the phase base (a `copies.py run --commit <base>` copy) and in the worktree at the same time, three rounds, into `<module>-pair-<before|after>-<n>-<sha>.json`, with `/proc/loadavg` beside each round. Each side sets `TMPDIR` to its own folder under `/dev/shm` and removes it after the round, because the gate runs there and pays no `fsync` cost (Phase 47). The host load changes all day, so a before batch and a later after batch do not compare. The director runs the pairs after the last repair lands (example: `phases/7/pairs.sh` in the run directory). Half the CPU is the target. When the allowed edits cannot reach it, report the measured floor and send the rest as an architecture proposal. A floor names its cost breakdown and the one operation that limits it. A proposal names its owner, its revisit condition, and whether its gain is measured or estimated. Report CPU s and process count first, then wall s, each removed test with its reason or covering test, and each architecture proposal with its measured gain.

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

### Phase 27 — `test_merge_gate_ledger.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- Closed with no code on 2026-11-06. Queue's Phase 13 start-spacing fix removed the module's 110 s of admission waits. The gate median fell from 121.3 s to 23.2 s, at 16.9 CPU s, rank 130 by wall time. The CPU rank does not call it.

**Binds later work:** `merge_gate.py` could wake on child exit in place of 0.05 s polls: at most 3.7 s here; owner sdlc devtools-unit. Nothing starts it.

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

### Phase 37 — A unit's full test runs under 40% of the test modules  · status: done
**Depends on:** none

#### As-built

- `select_affected` in `scripts/production/affected_modules.py` selects a test module by four rules: the test module changed; the test directly imports a changed Python file; the test names a changed path as a complete token; the test shares a folder with a changed non-test Python source.
- A full run comes only from `--full`, no certified base or a graph error. The nightly-red marker widens only a merge gate. A dirty tree, a rename or a deletion does not mean a full run.
- A unit run prints `affected: N of M modules (base <commit>, <user-zone time>), K changed files` once.
- Measured CPU per unit run: before, 124 runs, mean 6512.54 s, median 6710.86 s; after, 5 runs, mean 2238.07 s, median 2155.40 s. Replay of 104 records at the final code: median 120 modules, 58 runs at or under 40%, mean 2660 to 2716 CPU s, 102 module misses, 15 estimated red merges.
- `CACHE_FORMAT_VERSION` is 2 and `EXTRACTOR_VERSION` is 4. The public names are `extract_facts`, `SourceFacts(imports, tokens)`, `module_dependencies`, `graph_for_files`, `dependency_graph`, `select_affected`, `dirty_paths`, `blob_texts`, `revision_files`, `CertifiedBase`, `NoCertifiedBase`, `AffectedSelection`, `FullSuite` and `GraphError`.

**Files:**
- `scripts/production/affected_modules.py` — selection
- `scripts/production/merge_gate.py`, `scripts/production/gate_modules.py`, `scripts/production/merge_gate_test_support.py` — the unit run and the merge run
- `scripts/production/test_affected_modules.py`, `scripts/production/test_full_test.py`, `scripts/production/test_merge_gate_ledger.py` — tests
- `docs/as-built/merge-gate-and-test-speed.md` — selection rules

**Binds later work:** After one day on builds, count the red merges that a unit full test missed. Report that count against the 104-run estimate of 15 (8 for the wider selection), with both target orders: time, and CPU saved each day. A missed module turns red at the merge, because the merge gate runs all modules.

**Gotchas:** A `git merge` selection on a wide change stays wide: one measured run selected 490 of 716 modules.

**Ruled out:** A pass cache on import closures: upper limit 23.7% CPU, median 69 CPU s per run. The wider selection with filename mentions and a transitive search costs 3572 to 3628 CPU s per run and saves 7 red merges per 104 runs.

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

### Phase 42 — `mac_idle.py` loads only what its command needs  · status: done
**Depends on:** Phase 41

#### As-built

- `register`, `remove`, `exclude` and `cleared` import only what they use. The heavy coordination chain loads inside the watch functions, and `watch` still needs it. `unit_labels_for` is a module-level function that loads `unit_labels` and keeps the test patch name.
- CPU s per start, 15 paired starts of each command: `register` 0.54 to 0.09, `remove` 0.56 to 0.08, `exclude` 0.56 to 0.08, `cleared` 0.56 to 0.08, `watch` 0.56 to 0.54.
- Same-load pairs of `test_verify_mac_only.py`, 17 tests each: 218.79 s to 185.78 s wall, 183.67 to 150.76 CPU s, 17.9% less.
- One real `verify.sh test` from each tree, cargo-liner `census-test-support`, routed to the Mac: two `mac_idle.py` starts, 1.004 to 0.152 CPU s; all helpers 4.200 to 3.399 CPU s.

**Files:**
- `scripts/buildlog/mac_idle.py` — imports for each command

**Binds later work:** `build_targets.py` starts and the `build_hold` chain in `eligible` and `reserve` now set the `test_verify_mac_only.py` floor (Floors in `builds-natest-next.md`).

**Gotchas:** A run that the Mac cannot take starts no `mac_idle.py` command. A real measure needs a repository that `linux_only.<name>` in `config/mac_test.conf` knows; a clone of one needs `direnv allow`.

**Ruled out:** Plain local imports in place of the `TYPE_CHECKING` branches: they have no measured gain.

### Phase 43 — `direct/test_verify_token_renew.py` runs in half its time  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/test_verify_token_renew.py` runs in half its time, with every test name and assertion kept. Same-load pair medians: 145.84 s to 73.76 s wall, 57.93 to 53.42 CPU s.
- The module waits for the first expired second, not for fixed seconds after it. A short `TOKEN_GUARD_ATTEMPTS` and `TOKEN_GUARD_DELAY_MS` config makes `test_busy_board_guard_does_not_stop_memory_wait_renewal` reach status 2 in milliseconds.
- The remaining CPU is 11 real `invoke_verify` calls. Step-record starts are 225 and 19.96 process s; admission and release commands are 44 and 25.17 process s. The showrunner routed these starts to the verify unit and the queue unit.
- Phase 50 owns the write watch and the one inotify binding (Phase 43 F005, F006).

### Phase 44 — `copies.py` does less Git work  · status: done
**Depends on:** none

#### As-built

Each `copies.py` use does less Git work, with the same trees, remote refs and exit status. Three same-time pairs: warm `copies.py run` CPU 0.503 to 0.361 s (28.3%), processes 60 to 30, execs 67 to 33, wall 0.515 to 0.359 s. `style/test_crate_run.py` CPU 71.98 to 63.80 s (11.4%), processes 34,460 to 29,883, execs 36,289 to 31,010, wall 170.56 to 148.40 s. The Git work is a small part of that module now, so it misses the half target; no floor is established.

- `initialize` checks ref tips with `git show-ref --head` in place of `rev-list --objects --all --quiet`: 0.0012 CPU s against 0.307 s. A missing ref tip still exits 128 and sets `replace_git` through `CopyError`.
- `refresh_remotes` reads `git config --local --null --list` and `for-each-ref`, in the source and the tree. It writes config only if the settings differ, and runs `update-ref -d HEAD` and `fetch --prune` only if the refs differ.
- Recovery from missing borrowed objects stays (`test_deleted_borrowed_objects_are_recovered_from_replacement_source`).

**Files:**
- `scripts/copies/copies.py` — the cuts
- `scripts/copies/test_copies.py` — `test_warm_copies_refresh_moved_and_deleted_refs_without_a_new_head`, `test_warm_copies_remove_tracking_refs_after_source_objects_are_pruned`

**Gotchas:** damage below a valid ref tip is no longer scanned at `initialize`. A later Git step reports it through `CopyError`. The showrunner accepted this limit.

**Ruled out:** a half-CPU claim for `style/test_crate_run.py` from this cut alone.

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
- The largest removable lead is outside this phase: `bulk_run.py:1043`, `test_tree`, delegates 1,652 Git calls (24.1 s) to `copies.standing`. Phase 44 owns it.
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

#### As-built

- The running, release, bump and Cargo token sentences on `/build:queue` print `names.prose(step)`. `queue_evidence.running_text`, `queue_evidence.release_text` and the `head_facts` line builders take a required `names: StepNames` keyword, so every caller shows at type check.
- `scripts/lib/suite_runners.py` holds `SUITE_RUNNERS`, `SUITE_MODULES` and `starts_suite_runner(target)`, standard library only. `hold_enforcer.py`, `scope_activity.py` and `pre-tool-use-build-hold-guard.py` use it. `scope_activity.py` keeps its own scan of every argument. `admission_code.ADMISSION_CODE_PATHS` lists the file.

**Files:**
- `scripts/build_queue/queue_evidence.py`, `scripts/build_queue/head_facts.py`, `scripts/build_queue/queue_card.py`, `scripts/build_queue/queue_fixture.py` — sentences take names
- `scripts/lib/suite_runners.py` — the one list of suite runners
- `scripts/build_hold/hold_enforcer.py`, `scripts/build_queue/scope_activity.py`, `scripts/hooks/pre-tool-use-build-hold-guard.py`, `scripts/build_queue/admission_code.py` — use the list
- `scripts/build_queue/test_queue_evidence.py`, `test_queue_card.py`, `test_head_facts.py`, `test_build_queue.py`, `test_admission_protocol.py`, `scripts/lint/test_memory_admit.py` (owner resources) — tests

**Binds later work:** `build_hold.py:493` uses `SUITE_RUNNERS` in a small follow-up when natedev messaging's hold ends. On the tip after queue lands, `memory_cap_text` has no article and no step set, and `config/build_steps.conf` has no `names.full-test`.

**Gotchas:** The hook starts on every Bash call, so `suite_runners.py` imports only the standard library.

**Ruled out:** Moving the hook's import saves 0.25 ms next to 20 ms of start. The admission client wait notice keeps the raw step, because no user reads it.

### Phase 49 — Every shell script starts Python through `scripts/lib/py`  · status: done
**Depends on:** Phase 36

#### As-built

- Every shell script and shell test starts Python through the adjacent `scripts/lib/py`, so a Mac with Apple's Python 3.9 first on PATH starts the right interpreter. Arguments, stdin and exit status of each start stay the same.
- `board.sh` resolves its folder with `readlink -f`, so a filename-only start and a symlink start find the launcher. `memory_gate.sh` holds the launcher path in `BUILDLOG_MEM_PY`, so a relative source survives a directory change, and it has no bare-Python fallback.
- `direct/verify.sh` and `rust_style/load-rust-style.sh` keep `export CLAUDE_PYTHON="${CLAUDE_PYTHON:-python3}"`. That line names a default value and starts nothing.
- The launcher adds about 4 ms to each start (200 random-order pairs of `statusline.sh`, load 54-59: median pair difference 4.09 ms). The two `readlink` and `dirname` starts in `board.sh` add about 4.7 ms to each call.

**Files:**
- `scripts/berth_fix/capture.sh`, `scripts/berth_fix/launch.sh`, `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh`, `scripts/direct/board.sh`, `scripts/lint/memory_gate.sh`, `scripts/production/mac_run.sh`, `scripts/statusline/statusline.sh` — start Python through the launcher
- `scripts/agents/test_agent_exec.sh`, `scripts/agents/test_launcher_tasks.sh`, `scripts/lib/test_models_registry.sh` — shell tests start Python through the launcher
- `scripts/production/test_mac_run.py`, `scripts/statusline/test_statusline.py`, `scripts/lint/test_memory_gate_wait.py`, `scripts/build_hold/test_hold_shortcut.py`, `scripts/direct/verify_lint_scope_test_support.py` — fixtures carry the launcher, a seeded interpreter cache or the tools it needs

**Binds later work:** A fixture that copies a script also needs `scripts/lib/py` and `seed_interpreter_cache`. A narrow-PATH fixture that starts `board.sh` needs `readlink`.

**Gotchas:** Without `readlink`, `BOARD_DIR` becomes `.`, and the board request fails and retries for 60 s.

**Ruled out:** Starting the `CLAUDE_PYTHON` default through the launcher: the launcher must never receive its own path as `CLAUDE_PYTHON`. A ban check, by the showrunner's word and the user's rule against unasked checks.

### Phase 50 — One shared wait watches writes, and one file binds inotify  · status: done
**Depends on:** Phase 43

#### As-built

- `scripts/lib/inotify.py` is the one inotify binding. It holds `IN_MODIFY` and `read_events(descriptor)`, which does one read; `events` loops on it.
- `LocalReleaseEvents` and `local_release_waiter` take `mask`. The default `RELEASE_CHANGES` equals `0xFC8`, written with named constants. `LocalReleaseEvents.wait` uses `read_events` and returns when something changed or the deadline passes.
- `wait_until(..., watch_writes=False)` is the one wait that can watch writes. `VerifyTokenWaitFixture.wait_for_output` is one `wait_until(..., watch_writes=True)` call, then the `assertIn`. The raw-mask patches are gone.
- `verify_hold_wait_test_support.py` catches only `FileNotFoundError` around `add_watch`.
- Tests: `test_fixture_releases_a_call_when_a_scanned_directory_disappears`, `ReleaseDeadlineTests.test_wait_returns_by_its_deadline_when_irrelevant_events_never_stop`, and the live-writer test.
- Same-load pairs show no speed change, as expected.

**Files:**
- `scripts/lib/inotify.py`, `scripts/direct/local_release_wait.py` (owner verify), `scripts/direct/wait_for_test_state.py` — the binding and the wait
- `scripts/direct/verify_token_wait_test_support.py`, `verify_hold_wait_test_support.py`, `verify_mac_offload_test_support.py` — fixtures
- `scripts/direct/test_verify_hold_events.py` (owner verify), `scripts/direct/test_review_launcher.py` — tests

**Binds later work:** `test_verify_mac_signal_boundaries.py` `test_signal_before_runner_start_removes_result_without_local_work` cleared on one rerun and 32 stress runs. It belongs to verify if it returns.

**Gotchas:** `add_watch` raises `FileNotFoundError` for a directory that goes away. The deadline test allows `deadline + 5` s, so load gives no false red.

**Ruled out:** A write watch on by default wakes on each write in a busy directory, and predicates can read half-written files.

### Phase 51 — `direct/test_simplify_lens.py` runs in less time  · status: done

#### As-built

- Other work met the goal, and this phase changed no file. devtools 7u and the daily simplify work (`240d8fa4d`) replaced the lens in each phase review, and the module went from 30 tests to 15.
- Six runs of the new module on 2026-11-06 at load 10-70 took 16.1-38.7 s wall and 15.6-20.3 CPU s, under the Phase 6 floor of 118 s.

**Ruled out:**
- Same-load pairs: no edit exists to compare.

### Phase 52 — One shared reader for Git blobs, trees and status  · status: done
**Depends on:** Phase 37

#### As-built

- `scripts/style/git_process.py`, standard library only, holds `read_batch_blob(replies)`, which returns `GitBlob`, `MissingBlob`, `MalformedBlobReply` or `TruncatedBlobReply`. It also holds `TreeEntry`, `parse_tree_listing`, `blob_contents` and `parse_status_z`. A blob length that Python cannot read returns `MalformedBlobReply`.
- `tree_entries` stays in `style_ledger.py` and calls `parse_tree_listing`, because `test_the_split_blob_cache_reads_each_tree_once_and_keeps_versions_distinct` patches `style_ledger._git`. `style_ledger.py` imports `TreeEntry` and `blob_contents`, so `ledger_api` names still resolve.
- Each caller keeps its transport and its failure policy: `rules.py` (persistent process), `affected_modules.py`, `phase_process.py` and `feature_log/inventory.py` use `read_batch_blob`. `bulk_run.plan_constants` uses `ledger_api.tree_entries`. `unit_files.changed_files` and `affected_modules.dirty_paths` use `parse_status_z`.
- The full test on the phase commit is green: 715 modules, 6818 CPU s, memory peak 4.556 GiB. A trial merge with `origin/sdlc` 40e200dd0 is green.

**Files:**
- `scripts/style/git_process.py`, `scripts/style/test_git_process.py` — the parsers and their tests
- `scripts/style/style_ledger.py`, `scripts/style/bulk_run.py`, `scripts/style/unit_files.py`, `scripts/style/rules.py` — style callers
- `scripts/production/affected_modules.py`, `scripts/direct/phase_process.py`, `scripts/feature_log/inventory.py` — other callers

**Binds later work:** A new reader of a `cat-file --batch` reply, an `ls-tree -rz` listing or a `status --porcelain -z` output uses these parsers. Phase 44 finds `copies.standing` at `bulk_run.py:1043`.

**Gotchas:** `phase_process.py` and `inventory.py` start as plain scripts, so each adds the repository root to `sys.path` before its import.

**Ruled out:** `direct/review_tree.py`, `buildlog/treekey.py`, `direct/tree_watch.py` and `hana_shot/hana_shot.py` keep their own reads. `style/style_plan.py:404` reads each status code, so it is no copy of the parser.

### Phase 53 — `board.sh` starts no process to find its folder  · status: done
**Depends on:** Phase 49

#### As-built

- `scripts/direct/board.sh` finds its folder by parameter expansion when it starts from a path with a slash that is no symlink. A filename start or a symlink start still calls `readlink -f`.
- A `board.sh read` takes 6.01 ms median, from 9.23 ms, in 200 random-order pairs at a load of about 26. The median pair difference is 3.09 ms.
- A fixture PATH needs `readlink` only for a filename or symlink start, so `isolate_path_without_taplo` does not list it.
- Each start form returns rc=0 on natedev and on the Mac (Darwin 27.0.0, bash 5.3.9).

**Files:**
- `scripts/direct/board.sh` — the start form
- `scripts/direct/verify_lint_scope_test_support.py` (owner verify) — the fixture tool list
- `scripts/direct/test_board_start.py` — one test per start form, each posts one board line

### Phase 54 — The assign launcher reuse and identity tests run in half their time  · status: done
**Depends on:** none

#### As-built

- `FirstDispatchFixture.first_assignment` in `assign_launcher_test_support.py` runs one real first dispatch per role and option set, saves its files, and returns `CompletedFirstDispatch` with `FirstDispatchArtifact` and `FirstDispatchObservations`. A later test restores that state. `test_assign_launcher_reuse.py` uses it in 9 of 9 tests and `test_assign_launcher_identity.py` in 5 of 9. Each later dispatch stays a real launcher process. Every test name and assertion stays.
- Same-load pair medians at load 55-70: reuse 91.7 s to 57.4 s (0.63), CPU 70.5 s to 42.5 s; identity 67.8 s to 55.8 s (0.82), CPU 57.3 s to 47.2 s. Unpaired runs at lower load gave 0.40 and 0.64.
- Floor: each later dispatch is a real launcher process because `require_live_director` needs a live Claude or Codex ancestor. One process takes 2.6-3.2 s. The coordinator takes 1.80 s of that in 12 `subprocess.run` calls: follow 0.73 s, compact 0.39 s, recorder children 0.60 s, can-follow plus release-follow 0.66 s, model resolver 0.06 s, board and heartbeat 0.09 s.

**Files:**
- `scripts/direct/assign_launcher_test_support.py` — `FirstDispatchFixture` and its record types
- `scripts/direct/test_assign_launcher_reuse.py`, `scripts/direct/test_assign_launcher_identity.py` — the modules that use it

**Binds later work:** `FirstDispatchFixture` serves `test_assign_launcher.py` with no support file change. Its first-dispatch boundary tests and later dispatches stay real.

**Gotchas:** A restored file replaces the first test's root with the current root. The restore starts the stub server with the current pid and port. `from scripts.whoami import agent_accounts` stays after `scripts.production.staff_test_support` in the support file.

**Ruled out:** In-process launcher calls: the identity check needs the real process chain.

### Phase 55 — The Claude launcher records test uses half its CPU  · status: done
**Depends on:** Phase 54

#### As-built

`FindingsFixture` shares one real first launch per setup kind. `setUpClass` clears the class's `completed_setups`, and `first_worker_launch(*, repair=False)` copies a finished launch's files for each later test. Each decision stays on a real launch.

Same-load medians of three pairs: CPU 62.325 to 33.024 s (47.0%), processes 1,687 to 1,116, execs 2,217 to 1,478, wall 101.245 to 59.589 s. Real launches 16 to 11. Findings setup commands 12 to 4. 51 assertions and nine test names stay. The tests pass in reverse order, and each passes alone.

The targeted test on the gate had three red modules, and none came from this phase. Phase 65 caused two. The third, `production/test_merge_gate_suites_admission.py`, hung at load 140 and passes alone on the base.

**Files:**
- `scripts/direct/test_implement_launcher_records.py` — the tests call `first_worker_launch`
- `scripts/direct/implement_launcher_test_support.py` — the shared first launch

**Binds later work:** the floor is 1.861 CPU s above half. The rest is repeated interpreter starts at inline Python sites in `implement.sh`, `agent_bg.sh`, `claude_account.sh` and `heartbeat_watch.sh`, a proposal the showrunner holds.

**Gotchas:** the snapshot lines repeat `FirstDispatchFixture.first_assignment` in `assign_launcher_test_support.py`. Phase 68 settles that copy.

**Ruled out:** a floor claimed from each cut alone. The first worker's result missed the repeated first launches.

### Phase 56 — The SSH retained and retry tests run in half their time  · status: done
**Depends on:** none

#### As-built

- The shared fixture waits on events, not on fixed seconds. Each module loses about 28 s of fixed waits; CPU does not change.
- Same-load pair medians, load 52-65: `test_verify_ssh_targets_retained.py` 62.67 s to 35.46 s, ratio 0.566, 4 to 7 tests. `test_verify_ssh_targets_retry.py` 64.50 s to 42.11 s, ratio 0.653, 4 tests. Half is not reached.
- `verify` holds `memory-hook-ack` open with `os.O_RDWR` from before the start until its end. The hook reads exactly the acknowledgment length, so a hook that skips its read blocks nothing.
- `test_verify_returns_when_settled_hook_skips_acknowledgment` and two more regression tests pin the settled hook, early exit and output drain paths.

**Files:**
- `scripts/direct/verify_ssh_targets_test_support.py` — the shared fixture
- `scripts/direct/test_verify_ssh_targets_retained.py` — the module and the regression tests
- `scripts/direct/test_verify_ssh_targets_retry.py` — unchanged; it gains through the fixture

**Binds later work:** The floor is the child process starts and the coordination imports through `build_hold.py`, as in Phase 24. `recovery_start_seconds` stays 5 s (Phase 27 constraint). Measure both modules in pairs when queue-unit's `build_hold.py` cut is on builds.

**Gotchas:** A FIFO open for write blocks until a reader opens it, so a test holds one read-write descriptor. A retry interval near 0.01 s starves ready admission replies. A short retry repair has no regression test, because it needs a scheduling delay injected in a production file.

**Ruled out:** A warm command server in the fixture: it tests a different process boundary.

### Phase 57 — The merge tip holds the queue stack, and the unit's narrow run is called targeted test  · status: done
**Depends on:** none

#### As-built

- The branch holds builds 2cd281ee3 and this unit's Phases 34 and 36-54.
- The unit's narrow run is `scripts/production/targeted_test.py`, with tests in `test_targeted_test.py`. `merge_gate.SuiteMode.TARGETED_TEST` is `"targeted-test"`.
- Config keys: `names.targeted-test=targeted test` (`build_steps.conf`), `prefixes.targeted-test=targeted-test-` (`build_folders.conf`), `peak_hold_seconds.targeted-test=180` and `program_first_peak_gib.targeted-test=4` (`build_admission.conf`).
- `scripts/lib/suite_runners.py` holds `SUITE_RUNNERS = frozenset({"merge_gate.py", "targeted_test.py"})`. `admission_code.py`, `admission_service.py`, `buildlog/index.py` and `hooks/pre-tool-use-build-hold-guard.sh` use the new step. `StepNames.memory_cap_text` is gone, because the queue stack removed its only caller.
- `scripts/direct/board.sh:289` starts its Python block through `"$BOARD_DIR/../lib/py"`. `docs/plans/builds-queue.md` Phase 10v items 6 and 7 stand as builds wrote them.
- `test_change_review.py` pins `unit_producer` on the exact staff name across productions, and `UnknownProducer` for duplicate and absent names.
- Merge gate on the merged tree: one red module, `production/test_change_review.py`, later repaired. Its 112 tests pass on the tip merged with a8e9b1c42. Gate tests took 6220.74 s at load 55-135, with a peak of 2.006 GiB.

**Files:**
- `scripts/production/targeted_test.py`, `scripts/production/test_targeted_test.py` — the unit's narrow run and its tests
- `scripts/lib/suite_runners.py` — the runners the guards recognize
- `config/build_steps.conf`, `config/build_folders.conf`, `config/build_admission.conf` — the step's name, folder prefix and limits
- `scripts/production/test_change_review.py` — exact-name producer tests
- `scripts/direct/board.sh` — the board's Python start

**Binds later work:** Until the script is on main, a targeted run uses the tree's script with `CONF_DIR='{tree}/config'`, or the merge gate. Stored `full-test` rows in `anon_peaks.jsonl` and `queue_times.jsonl` stay as history. `build_hold.py:493` keeps the old word until the send freeze ends.

**Gotchas:** Before the tip lands, a plain targeted run refuses with `REFUSED: no first peak for targeted-test`. Before the tip is on builds, the selection covers about 564 of 716 modules, because the base is the merge base with builds. `publication_changes` can print "Producer stays empty" and keep `UnknownProducer` (`change_review.py:975-982`).

**Ruled out:** A test for a different relative production path: `read_production` and `production_doc` take `row.doc.path` from the staff row, so no caller can pass that path.

### Phase 58 — The hana_shot launch test reads no live build log  · status: done
**Depends on:** Phase 57

#### As-built

- `test_launch_and_cleanup_leave_the_default_buildlog_absent` runs with `HOME` under the test's scratch folder, so it reads no real log and another build cannot fail it. It asserts that the isolated log root is inside the scratch folder and that the default log root stays absent after launch and cleanup. The other assertions on exit code, state file, scratch folder and isolated log rows stand.
- The module has 115 tests and takes 52 s. A `store.root` that ignores `BUILDLOG_DIR` fails the test at its `assertFalse(default_root.exists())` line.

**Files:**
- `scripts/hana_shot/test_hana_shot.py` — the launch and cleanup test

**Gotchas:** The test reads the default root with `BUILDLOG_DIR` removed, under the patched `HOME`.

**Ruled out:** A proof that appends to the real shared log: it puts false rows in a log other tools read.

### Phase 59 — `style/test_style_proposals.py` uses half its CPU  · status: done
**Depends on:** Phase 61, Phase 64

#### As-built

`style/test_style_proposals.py` uses 47.0% less CPU, with all 186 test names and 838 assertion calls kept. Three same-load pairs: CPU 53.75 to 28.51 s, processes 19,147 to 14,152, execs 18,841 to 14,031, wall 83.86 to 63.54 s.

- `setUpClass` runs `fixture.prepare_fixture()` once and copies `fixture.root` to `cls.fixture_seed`.
- `setUp` copies the seed, rewrites the seed root in `staff.json` only, and binds the patches through `use_fixture()`. It starts no Git command.
- `approved_decision` and `decision_closed_as_built` keep their real writes and assertions.

**Files:**
- `scripts/style/test_style_proposals.py` — the module

**Gotchas:** the remaining cost is rule loading: `_load_rule` in `scripts/style/rules.py` starts `git hash-object` for each rule read, 6,556 starts and 9.85 CPU s across 2,080 calls. The proposal for one immutable rule result per content version went to the style owner, with an estimated gain of 4.9 CPU s.

**Ruled out:** more seeds for the decision helpers, because their findings and later mutations differ.

### Phase 60 — `show.py top` ranks by CPU, and `module_cost.py` counts process starts  · status: done
**Depends on:** none

#### As-built

`show.py top` for `~/.claude` ranks gate modules by median CPU. The columns are rank, suite/module, median CPU s, median s, last s, tests run, slowest kept test, and the sum line sums median CPU. The nextest view for another repository is unchanged.

`module_cost.py --tree <path> <folder>/<file> ...` prints one row for each module and a total row: CPU s, processes, execs, wall s, test status. One run gives CPU s and wall s from `os.wait4` and the clock. A separate `strace -f` run gives the counts: successful `fork`, `vfork`, `clone` and `clone3` calls without `CLONE_THREAD`, and successful `execve` calls. A red module or failed trace start prints `RED (test=<n>, trace=<n>)` in its row, later modules still run, and the command exits nonzero. No `strace` on PATH prints one error line and exits 2.

Measure: `direct/test_verify_mac_arguments.py` gives 40.353 CPU s, 1,936 processes, 1,972 execs and 62.311 wall s; verify's method counts 1,925 processes on the same tree, a 0.6% difference. A warm `show.py top` takes 0.455 s.

**Files:**
- `scripts/test_times/show.py` — the CPU rank
- `scripts/test_times/test_show.py` — the rank's tests
- `scripts/test_times/module_cost.py` — module costs
- `scripts/test_times/test_module_cost.py` — count tests from captured lines, one real two-child run, and the failed trace start
- `commands/test_times.md`, `config/README.md` — the text

**Binds later work:** the verify batches measure with `module_cost.py`. Its count matches only a trailing `<unfinished ...>` marker, so a quoted marker in an argument does not hide an exec.

**Gotchas:** the first `show.py top` after a gap took 1m58 at load 70, with no explained cause.

**Ruled out:** rewriting done Phase 4's wall-time text; done phases stay byte-identical.

### Phase 61 — Two large verify test files test logic by direct calls  · status: done
**Depends on:** none

#### As-built

`test_verify_mac_only.py` and `test_verify_lint_cache_versions.py` check their argument, routing, selection and result logic through direct calls of the real functions. They keep the real `verify.sh` runs that prove what no direct call can. Each moved check was proved by a broken step on a tempdir copy.

| File | CPU s before, after | Processes before, after | Wall s before, after |
| --- | --- | --- | --- |
| `test_verify_mac_only.py` | 180.0, 107.4 | 6,665, 4,820 | 214.3, 127.6 |
| `test_verify_lint_cache_versions.py` | 87.7, 44.6 | 6,079, 3,379 | 141.0, 72.9 |

**Files:**
- `scripts/direct/test_verify_mac_only.py` — `mac_result()` holds the direct call form for result functions
- `scripts/direct/test_verify_lint_cache_versions.py` — the harness form for lint functions

**Binds later work:** a process count does not rank CPU. Add up CPU by command before a move. The four SSH files lost 52% of their processes and no CPU, because they keep real Python target commands and one live admission server per test (Phase 66). The 38 parser cases of `test_verify_mac_only.py` and the 28 real starts of `test_verify_lint_judgment.py` wait for verify's two function moves (Phase 67).

**Gotchas:** a measure run started with `&` has SIGINT ignored, so a SIGINT test goes red there. Measure with `env --default-signal=INT`.

**Ruled out:** moving the SSH cases in this phase with no CPU gain. The worktree keeps that first step for Phase 66, which keeps a move only if it cuts CPU or another proof needs it.

### Phase 62 — A test runner ends each process its tests leave  · status: done
**Depends on:** none

#### As-built

On Linux, `module_run.py` marks itself a child subreaper and forks before it imports test code. The child runs the tests. The parent reaps each adopted orphan at once. When the test process ends, or the parent receives SIGTERM, SIGINT or SIGHUP, the parent sends SIGKILL to each direct child and reaps until no child stays. It then ends with the test process's status. A SIGKILL of the parent stops this end step. Where `prctl` does not exist, the runner does not fork.

The merge gate's module time limit now calls `stop_gate_jobs([job])`, so SIGTERM comes first and the end step runs.

Measure: runner CPU per module 0.063 to 0.088 s, in 30 same-load pairs at load about 80. That is one more process per module, and about +18 CPU s per gate over 717 modules. `process_memory_mib` sums the root and its descendants, so a module's sampled memory now includes the parent's RSS and any orphan that stays in the tree.

**Files:**
- `scripts/test_times/module_run.py` — the fork and the end step
- `scripts/test_times/test_module_run.py` — the cases
- `scripts/lib/descendants.py` — the one subreaper function
- `scripts/lib/build_folders.py`, `scripts/build_queue/ci_cargo_front.py`, `scripts/message/process_test_support.py` — its callers
- `scripts/production/merge_gate.py` — the time limit path
- `scripts/production/merge_gate_suites_test_support.py`, `merge_gate_test_support.py`, `test_merge_gate_suites.py`, `test_merge_gate_suites_resources.py` — the time limit tests
- `docs/as-built/merge-gate-and-test-speed.md` — the rule

**Binds later work:** a process count from before this phase is one lower per module. A test process's own CPU counts do not hold an orphan's CPU, because the parent reaps it.

**Gotchas:** a test run with `&` in a non-interactive shell has SIGINT ignored, so a SIGINT test fails falsely. Run these tests in the foreground. The three old live fixture processes stayed alive: queue-unit asked the user about them.

**Ruled out:** a subreaper in the test process itself, which leaves each orphan as an unreaped zombie.

### Phase 63 — Three verify test files stop failing at random  · status: done
**Depends on:** none

#### As-built

Each random failure form is reproduced with a forced delay, and each fix passes it.

- `test_a_foreign_mac_block_keeps_lint_in_the_memory_wait_until_natedev_is_ready`: the read found `''` because the file exists before its content. The probe writes `<name>.pending`, then renames it.
- `test_a_stopped_call_leaves_no_step_or_copy_running`: the read found an empty `cargo-compiling`. The test now uses `wait_for_pid`, sends SIGTERM to the verify process only, and waits with `wait_for_marker_exit` before the unchanged empty-set assertion. Removing the body of `stop_copy` in `scripts/mac_test/copy_watch.py` fails it.
- `test_edit_during_the_test_run_records_no_pass` got rc 77 when the edit came before the watcher's first read. Two tests now fix each order: the cancel test uses the real watcher at a 0.01 s interval and a held Cargo stub. The recording test keeps a first-read hook. Changing the `tree_watch.py` signal fails the cancel test.

**Files:**
- `scripts/direct/test_verify_token_mac_step.py` — the atomic probe
- `scripts/direct/test_verify_mac_copy_ownership.py` — the cleanup wait
- `scripts/direct/test_verify_example_edits.py` — two fixed orders

**Gotchas:** the historical gates kept no logs, so their exact causes stay unproved. The origin of a marked process seen in one copy ownership run stays unproved. If it recurs, capture its command and parent at the failing scan.

### Phase 64 — `module_cost.py` names the tests with the most CPU  · status: done
**Depends on:** Phase 60, Phase 62

#### As-built

`module_cost.py --tests N` prints the N tests with the most CPU, with each test's CPU s and wall s. `--commands N` prints the N commands with the most CPU, with starts and CPU s, then an `unassigned` line with its sign: the run's `RUSAGE_CHILDREN` CPU less the sum of all rows.

- `module_run.py` adds `cpu_s` to each test row and writes a row for each class and module fixture, with its own `cpu_s`. It wraps a fixture only when the value is not `None`. It skips the module fixtures of a class whose module is not in `sys.modules`, as unittest does. `coverage_overlap.py` keeps its three-value tuples.
- `gate_record._test_time` keeps only test, s and status, so gate records do not grow. `show.py` and the build log store do not change.
- `module_cost.py` starts both of its runs through `module_run.py`, so each process count holds the supervisor, and the end step of Phase 62 runs. It removes `TEST_TIMES_OUT` from the trace and command runs. `--commands` adds one run; without it, the other runs do not change.
- `task_cpu.measure` forks a child that blocks on a pipe read, then seizes it with `PTRACE_SEIZE`. The options trace fork, vfork, clone, exec and exit, and add `PTRACE_O_EXITKILL`, so the tracees end with the tracer. It stops at no system call.
- A task's first stop assigns its command from `/proc/<pid>/status`: a thread takes its `Tgid`'s row, a process takes its `PPid`'s row. An exec stop reads `/proc/<pid>/cmdline`. An exit stop reads the first field of `/proc/<pid>/schedstat`.
- A group stop (`SIGSTOP`, `SIGTSTP`, `SIGTTIN`, `SIGTTOU`) calls `PTRACE_LISTEN`, so the task stays stopped and its real parent sees the stop. A signal-delivery stop passes its signal on, `SIGSTOP` included.
- `command_name` knows two families. Python (`python*`, `pypy*`) takes values for `-W` and `-X`. Shells (`bash`, `sh`, `zsh`) take values for `-o` and `-O`. A short cluster ending `c` gives `<program> -c`. A Python cluster ending `m` gives `<program> -m <name>`. `--` ends the options. Other programs group by program name.

Measure: `unassigned` was +0.0056 s of 2.599 CPU s on `message/test_process_test_support.py`, and +0.39 s of 36.7 CPU s on `direct/test_verify_ssh_targets_retained.py`.

**Files:**
- `scripts/test_times/task_cpu.py` — the kernel task counters by command
- `scripts/test_times/module_run.py` — `cpu_s` and the fixture rows
- `scripts/test_times/test_module_run.py` — the runner cases
- `scripts/test_times/module_cost.py` — `--tests`, `--commands`
- `scripts/test_times/test_module_cost.py` — the real-module cases
- `commands/test_times.md` — the options and limits

**Binds later work:** an orphan is reaped by the parent process, so its CPU is not in the test process's count. A speed phase reads `--commands` for orphan CPU.

**Gotchas:** a failed `PTRACE_SEIZE` leaves the blocked child reading end of file when the tracer exits, and it then runs untraced. A forked task with no exec takes its parent's current row. A recycled pid can take an old row. A long option with a value, such as `--rcfile`, reads its value as the script. A task ended by `SIGKILL` reports no exit stop, so its CPU goes to `unassigned`.

**Ruled out:** `wait4` usage, because most waits in a module request none. Script groups for perl, ruby and node. A cleanup path other than `PTRACE_O_EXITKILL`.

### Phase 65 — The first test time list after a gap appears in under a second  · status: done
**Depends on:** Phase 60

#### As-built

`show.py` reads the index first and refreshes after the print. A list holds every gate record that the last complete update read. A newer record appears on a later run.

- `update_in()` writes the `meta` key `updated_at` inside its transaction, when rows changed or the key has no number. `open_for_update()` sets `cache_spill`, so a reader does not wait for an ordinary refresh.
- `open_report_index()` opens the current schema's index when it has `updated_at`. Otherwise `index.py` finds the newest other index with the key and the same `CREATE TABLE` SQL for all four `GATE_TABLES`. When no index qualifies, it updates first. This path can take minutes.
- One read transaction covers the report rows and the time, so both describe one update.
- After the output, `show.py` closes its connection, flushes and forks one child, which calls `os.setsid()` and `index.update()`. The parent exits at once.
- Stderr prints `rows as of <HH:MM zone>`, with `MM-DD` for another day. Stdout does not change.

Measure: a first list on a scratch history took 0.116 CPU s and 0.166 s wall at load 66. A second list during a refresh took 0.120 and 0.147. An older compatible index took 0.117 and 0.127. Before the phase, a 30-minute gap took 2.77 CPU s and 14.3 s wall under `cProfile`, and a new index took 222.0 CPU s and 351.6 s wall.

**Files:**
- `scripts/test_times/show.py` — read first, the refresh, the time line
- `scripts/test_times/test_show.py` — the real-process cases
- `scripts/buildlog/index.py` — the key, `cache_spill`, the compatible index search; owner report-unit
- `scripts/buildlog/test_index.py` — the index cases
- `commands/test_times.md` — the time line and the rule

**Binds later work:** the first list after a gate can omit that gate. A reader that needs each record reads the records, not this list.

**Gotchas:** a refresh child that fails ends with no message, and the time line shows the old time. A burst of lists queues refresh children on `update_lock_in`; each child after the first is cheap. An index with no `updated_at` gets one synchronous update. The original 1m58 is not classified, because no matching lock trace exists.

**Ruled out:** an update before the print, which cannot reach 1 s after a gap.

### Phase 66 — The four SSH target test files use half their CPU  · status: done
**Depends on:** none

#### As-built

No test file changes. The four files keep their real `verify.sh` runs. The measured floor is 146.1 CPU s at same load, against the 102 CPU s target.

| File | CPU s, same-load median | Wall s | Processes |
| --- | ---: | ---: | ---: |
| `test_verify_ssh_targets_routing.py` | 66.56 | 82.7 | 2,672 |
| `test_verify_ssh_targets_results.py` | 41.64 | 47.6 | 3,297 |
| `test_verify_ssh_targets_retry.py` | 16.02 | 17.4 | 1,302 |
| `test_verify_ssh_targets_retained.py` | 21.87 | 22.9 | 1,268 |
| Total | 146.10 | | 8,539 |

Cost breakdown, from one traced measure of 249.6 CPU s: child commands use 228.2. `build_targets.py` uses 128.4 of that, over 270 starts, and a cold import costs about 0.83 CPU s per start. Admission threads use 8.0, cleanup scans 6.0, and inventory 0.2.

The one operation that limits the floor is a cold start of a `build_targets.py` command. The cut belongs to verify-unit. It joins verify's item "Each verify run starts fewer helper processes", after its Phase 8. Requests sent to verify-unit: an eligibility consumer from `initialize_step_record`, a result consumer from `try_mac_test` with the next step of `route_test`, and reservation and retry orchestration around `command_reserve`. Revisit when verify's hash arrives: same-load pairs of the four files. The gain is an estimate until then, with an upper limit of the 128.4 CPU s share.

SIGINT at its default did not prove the cause of any failure in Spec step 5. `event, = self.ssh_events()` received several events once on the base and once in the measure; a full-context rerun did not reproduce it, so the cause stays uncertain.

**Files:** none changed.

**Binds later work:** a direct call keeps the decision that the test proves. `route_test` and the queue-age handoff are Bash decisions with no narrow function, so their tests stay real until verify's functions land.

**Ruled out:**
- The `route()` form of Phase 61: 52% fewer process starts and no CPU gain, with command-line paths lost.
- The `offload_result` fixture: 41.64 to 14.39 CPU s in the results file, but it calls each target itself and precomputes each attempt. A stop after a busy result and a lost `BUILDLOG_MEM_WAIT_SINCE` pass unseen. The two tests that could stay save about 8 CPU s, against a 90-line fixture that patches global state beside the admission thread.

### Phase 67 — The lint judgment and parser cases move onto verify's new functions  · status: done
**Depends on:** Phase 61

#### As-built

- `test_verify_lint_judgment.py` calls `run_lint_judgment`, `run_lint_held_sequence` and `run_lint_sequence` through `shell_functions`. It keeps a real `verify.sh` start only for a path that no direct call proves. Same-load CPU: 68.3 → 19.1 s.
- `test_verify_mac_only.py` checks its parser cases through `parse_routing_arguments`. `test_policy_refusals_start_nothing` keeps one real `verify test sample --env SHOTS` run: exit 2, the `--env and --fetch need --mac` line, and no runner, cargo or call-record event. Same-load CPU: 101.5 → 96.5 s.

**Files:**
- `scripts/direct/test_verify_lint_judgment.py` — lint judgment cases; direct calls plus the real starts only a real run proves.
- `scripts/direct/test_verify_mac_only.py` — Mac-only policy and parser cases; one real refusal run.

**Gotchas:**
- `test_verify_mac_only.py` stays at its floor until `verify.sh` makes one `build_targets.py` call per start that returns one JSON (verify owns this work). Count the file at this floor until then.

### Phase 68 — The assign launcher tests use half their CPU  · status: done
**Depends on:** Phase 54

#### As-built

The four assign launcher test files use 37% less CPU, with every checked behavior kept. Three same-time pairs on `/tmp`: 482.4 to 303.6 CPU s. `test_assign_launcher.py` 286.6 to 214.8, `_reuse` 89.8 to 36.1, `_identity` 59.1 to 21.2, `_review` 46.9 to 31.5.

- `invoke_assignment` in `assign_launcher_test_support.py` runs the real coordinator in process. It patches `require_live_director`. Forty tests use it.
- `snapshot_files` and `changed_files` hold the first-dispatch snapshot. `FirstDispatchFixture.first_assignment` and `AssignClaudeLauncherFixture.first_worker_launch` both use them, and each keeps its own restoration.
- The process tests that stay real cover the ancestor check, launcher ownership, worker failure and concurrent review stages.

**Files:**
- `scripts/direct/assign_launcher_test_support.py` — the assign fixture, `invoke_assignment` and the snapshot helpers.
- `scripts/direct/assign_claude_test_support.py` — the Claude launcher fixture.
- `scripts/direct/test_assign_launcher.py`, `test_assign_launcher_reuse.py`, `test_assign_launcher_identity.py`, `test_assign_launcher_review.py` — the four test files.

**Gotchas:**
- `AssignLauncherFixture` inherits `unittest.TestCase` and does not set the child `TMPDIR`.
- The remaining cost is the process tests in `test_assign_launcher.py` and the recorder and resolver children (58.6 CPU s in the command profile).

### Phase 69 — The remote copies review test uses half its CPU  · status: done
**Depends on:** Phase 64

#### As-built

`copies/test_review_remote_copies.py` uses 65.7% less CPU, with all 28 tests and 152 assertion statements kept. Three same-time pairs on `/dev/shm`: CPU 350.0 to 120.2 s, wall 616.2 to 205.5 s, process starts 1,684 to 813, execs 2,308 to 842.

- Each test class copies one immutable Git seed with `ImmutableGitSeed.copy_to`.
- `warm_claim_servers()` runs a warm fork `ClaimServer` over the real `floor_claims` source and entry points.
- `seed_interpreter_cache` replaces repeated interpreter probes, with `CLAUDE_PYTHON` empty and `XDG_CACHE_HOME` set.
- The threaded cancellation test keeps fresh subprocesses, and the real slot shell stays.

**Files:**
- `scripts/copies/test_review_remote_copies.py` — the module
- `scripts/copies/remote_test_support.py` — seed and warm transport; also imported by `test_retire_unit.py` and `test_tidy_wrapped.py`

**Gotchas:** warm fork children keep the module_run command name until an exec, so a command table credits their CPU to it. Two probes stay green on base and new: a claim reply with no mirrors, and a server with no admission lock. These cases have no test.

### Phase 70 — The token order test stops hanging  · status: done
**Depends on:** none

#### As-built

The goal was not reached: the hang is not fixed and its cause stays open. The hang did not reproduce in ten runs and five forced stops.

`test_the_token_goes_to_the_call_that_waited_longest` now reports the call output and the queue file names when its guard ends the wait. A forced hang on a tempdir copy shows both.

**Files:**
- `scripts/direct/test_verify_token_fifo.py` — the hang report

**Gotchas:** one unproved condition is PID reuse at `verify.sh:2953-2956`. The gate deletes its temporary log, so the report stays readable only in a log that the gate's caller keeps. The next recurrence names the wait.

### Phase 71 — Index refreshes leave reads and unchanged indexes alone  · status: done
**Depends on:** Phase 65

#### As-built

An unchanged index update writes nothing, a read leaves no side file, and a list reads during an ordinary refresh.

- `open_for_update()` uses the rollback journal and sets `PRAGMA cache_spill` from `INDEX_SPILL_PAGES=65536` in `config/buildlog.conf`. SQLite reads that value through 8 bits (65536 & 255 = 0), so cache spill is off: no index update spills, and every update takes the exclusive lock only at commit (builds report Phase 35, 2026-11-08).
- `update_in()` writes the `meta` key `updated_at` only when the transaction changed rows or `has_numeric_update_time()` is false. `rows as of` names the last update that changed rows.
- `complete_gate_index_in()` waits up to `INDEX_READ_WAIT_SECONDS=1` while it selects an index. The `show.py` report connection waits `BUSY_TIMEOUT_MS`, 30 s, so a writer that takes the exclusive lock after selection does not stop the report.
- A list during a refresh on a scratch index of 100 gates takes 0.111 CPU s, 0.187 wall s and 5 processes. That refresh peaks at 66.9 MiB RSS.

**Files:**
- `scripts/buildlog/index.py` — journal mode, spill size, the key write, the selection wait; owner report-unit
- `scripts/buildlog/test_index.py` — the index cases
- `scripts/test_times/show.py` — the report connection
- `scripts/test_times/test_show.py` — the real-process case
- `commands/test_times.md` — the time line
- `config/buildlog.conf` — the two keys

**Gotchas:** with cache spill off, an update keeps all its changed pages in memory until commit; no size limit applies. A value that keeps spill on must not reduce to 0 in 8 bits. Memory for a full build at live scale is not measured; report measures the value, and any value change goes to the user first.

**Ruled out:** WAL, because an unchanged update and a read then change the index files; a 1 s wait on the report connection, because a commit after selection then stops the report.

### Phase 72 — Three gate tests stop failing from load and live staff  · status: done
**Depends on:** none

#### As-built

- `direct/test_findings_simplify.py` runs its recorder sequence inside `isolated_staff_file`. An unreadable `STAFF_FILE` gave `StaffUnreadable` before and passes now.
- `LifecycleFixture.remove_fixture` renames the fixture root to a new `finished-` folder before `shutil.rmtree`. One failed removal stays one error, and the next test gets its path.
- `AdmissionFixture.loop_turn` and the fixture shutdown wait `TEST_HANG_GUARD_SECS` (600 s) in place of 3 s. A forced 4 s delay fails on the old form and passes now.

**Files:**
- `scripts/direct/test_findings_simplify.py` — staff isolation
- `scripts/production/production_lifecycle_test_support.py` — fixture cleanup
- `scripts/build_queue/admission_test_support.py` — fixture waits; queue-unit owns this file

**Gotchas:** two causes stay unproved. The process that wrote to `origin.git/objects` did not show in 66 delayed cleanups under strace, with zero live writers. No stack exists from the first admission hang at load 140. A real hang can report at up to 1200 s, below the 1800 s module limit.

**Ruled out:** a retry in `rmtree`, because it hides the writer; one shared deadline for the two waits, because it could fail a slow passing test.

### Phase 73 — Two stopped-call tests stop failing at random  · status: done
**Depends on:** none

#### As-built

No code change. Both causes are named from evidence, and neither cause is in a test.

- Mac case: `test_a_stopped_call_leaves_no_step_or_copy_running` failed in queue's tree, which lacks Phase 63's `wait_for_marker_exit`. Phase 63 (c6deef7b5) fixes it.
- Token case: 3 of 60 paired runs (5%) failed at `test_verify_token_memory_history.py:110`. Each survivor was `sleep 60` from `verify.sh:3259` (`sleep "$TOKEN_RENEW_S" &`). It stayed alive at least 10 s in the verifier's process group, with no pending signal. `stop_token_renewer` (`verify.sh:3271-3272`) signals and waits only for the renewer pid.
- verify-unit owns the token defect. Its next tip carries the `jobs -p` fix and a forced regression test. The line 110 assertion stays as the acceptance test for that fix.

**Files:**
- `scripts/direct/test_verify_token_memory_history.py` — unchanged
- `scripts/direct/verify_token_wait_test_support.py` — unchanged
- `scripts/direct/test_verify_mac_copy_ownership.py` — unchanged
- `scripts/direct/verify_mac_offload_test_support.py` — unchanged

**Gotchas:** the forced-pass form in the goal is not reached for the token case, because a longer wait would hide the production defect. The exact signal timing inside the renewer stays unproved.

**Ruled out:** a longer wait or a retry in the test, because it hides a process that `verify.sh` leaves running.

### Phase 74 — `copies/test_copy_guard.py` uses half its CPU  · status: done
**Depends on:** none

#### As-built

`copies/test_copy_guard.py` uses 51.1% less CPU, with all 51 tests and 76 assertion calls kept. Three same-load pairs: CPU 64.15 to 31.36 s, processes 1,308 to 446, execs 1,261 to 449, wall 83.90 to 43.04 s.

- An `ImmutableGitSeed` class seed is made once in `setUpClass`. Each test copies it in place of a new Git repository.
- `repository_findings()` caches one strict repository scan. Each repository test filters it by its own `settings.allow_list`.
- The mend front test keeps 1 real `copy_guard.py` process. Its other 12 cases call `copy_guard.main` through `invoke_cli`.

**Files:**
- `scripts/copies/test_copy_guard.py` — the module

**Gotchas:** the cached scan is valid only while no test writes into the scanned tree. A test that edits repository files must scan again.

### Phase 75 — `production/test_add_unit.py` uses half its CPU  · status: done
**Depends on:** none

#### As-built

`production/test_add_unit.py` uses 66.0% less CPU, with all 64 tests kept. Three same-load pairs: CPU 59.76 to 20.34 s, processes 2,575 to 2,483, execs 2,414 to 2,234, wall 68.56 to 26.73 s.

- `setUpModule` patches `herdr.Herdr.start_command` with `unittest.enterModuleContext`. The patch runs the argv through `herdr.run_program`, which the existing fake transport answers.
- `test_real_brief_admission_can_switch_to_its_approved_plan` calls the brief in process for its two admissions.
- The standby test and the cross-production refusal test keep their real CLI processes.

**Files:**
- `scripts/production/test_add_unit.py` — the module

**Gotchas:** the Herdr tests own the waiting `start_command` behavior. They cover CLI completion, observation and deadline expiry, but not a `Popen` failure or `AgentExited`; the old add-unit tests did not force those cases either.

### Phase 76 — Verify hold fixtures read their own run root  · status: done
**Depends on:** none

#### As-built

The verify hold and token wait fixtures keep every run root inside the test root. Live `/tmp/claude/unit` opens per module go from 289-867 to zero. All 36 importer modules (123 tests) pass, and CPU does not change (59.19 to 58.99 s for the four hold modules).

- `VerifyHoldTools.setUpClass` sets `ROSTER_RUN_ROOT` and `UNIT_RUN_ROOT` to `<root>/unit` and `SCRATCH_ROOT` to `<root>/scratch`.
- The token wait fixture makes a `unit` folder and sets the same three keys under each test root.
- The seven other `verify_*_test_support.py` families make no live read and do not change.

**Files:**
- `scripts/direct/verify_hold_wait_test_support.py` — hold fixture class seed and environment
- `scripts/direct/verify_token_wait_test_support.py` — token wait fixture environment

**Binds later work:** a fixture that starts `verify.sh` sets `ROSTER_RUN_ROOT`, `UNIT_RUN_ROOT` and `SCRATCH_ROOT` inside its test root.

**Gotchas:** `verify.sh:1272` reaches `progress_history.last_dispatch` (`ROSTER_RUN_ROOT`), and `unit_runs.held` and `progress_history._supplier_address` (`UNIT_RUN_ROOT`). `SCRATCH_ROOT` sets the `unit_runs` defaults at import, and `copy_record.py` reads it.

### Phase 77 — `style/test_crate_run.py` uses half its CPU  · status: done

#### As-built

`style/test_crate_run.py` uses 21% less CPU, with all 97 tests and their assertions kept. Three same-load pairs: CPU 67.62 to 53.25 s, processes 29,883 to 28,158, execs 31,009 to 29,476, wall 206.3 to 180.3 s (pair 1). The half-CPU target is not met; the rest is the measured floor of the production Git wrappers.

- `setUpClass` builds the full fixture once into a class seed. Each test copies the seed back to the original root path, then opens a fresh queue and ledger connection. Setup cost falls from 15.9 to 1.1 CPU s.
- The production Git wrappers cost 40.8 CPU s: `bulk_run.py` and `git_process.py` make 10,224 Git calls (14.8 s), and `copies.py` makes 10,799 (26.0 s). Fsync costs 0.02 CPU s under `/dev/shm`.

**Files:**
- `scripts/style/test_crate_run.py` — the module

**Gotchas:** the seed copy keeps symlinks, and only `staff.json` holds the original root path, so each test restores the seed at that same path.

**Ruled out:** more test-side cuts; the test bodies already call production functions directly. A production cut, reuse of the held validated checkout in `CrateRun.integrate` (upper bound 5.0 CPU s), belongs to natedev-style.

### Phase 78 — `production/test_staff.py` uses half its CPU  · status: done
**Depends on:** none

#### As-built

`StaffCommandTestCase` runs 72 staff CLI calls in process through `invoke_cli` (`scripts/production/cli_fixture.py`). Gate CPU goes from 91.8 to 28.9 s, and process starts from 393 to 177, in three same-load pairs. All 122 tests pass.

- `StaffCommandTestCase.setUp` sets `ROSTER_RUN_ROOT`, `UNIT_RUN_ROOT` and `SCRATCH_ROOT` inside the test root before any CLI call.
- The `pause` calls, the Mac transport test, the fake SSH processes and the lock, concurrency and killed-writer tests keep real processes.

**Files:**
- `scripts/production/test_staff.py` — staff CLI tests; command tests call the CLI in process

**Gotchas:** `staff.py pause` finds its caller through `runtime_id()`, which walks the parent processes. An in-process call has no such parent, so `pause` keeps `self.script(STAFF_SCRIPT, "pause", ...)`.

### Phase 79 — `style/test_add_style.py` uses half its CPU  · status: done

#### As-built

`style/test_add_style.py` uses half its CPU, with all 31 tests and their assertions kept. Three same-load pairs: CPU 57.09 to 27.73 s (down 51.4%); 22 mutations still fail their tests. The module passes at 27.4 CPU s on the branch.

- `AddStyleTests.setUpClass` builds one repository seed through `prepare_repository`. Each `setUp` copies the seed with `shutil.copytree`.
- The bootstrap runs in process through `invoke_cli(add_style, lambda: add_style.main(argv), argv, self.env, self.repo)` from `scripts/production/cli_fixture.py`.
- Two `run_bootstrap_process` tests and the rules-listing CLI keep one real process each.

**Files:**
- `scripts/style/test_add_style.py` — the module

**Gotchas:** the local seed adds an origin, an upstream branch and its own commit message, so `initial_tip` depends on it.

**Ruled out:** a shared `ImmutableGitSeed`; it changes the repository state the tests assert on.

### Phase 80 — Claim tests catch a reply with no mirrors and a server with no admission lock  · status: done

#### As-built

`scripts/copies/test_review_remote_copies.py` fails when a claim reply leaves out the mirrors, and when the claim server takes no admission lock. All eight claim mutations fail the 29-test module.

- A test reads the mirrors that the claim reply carries.
- `test_claim_server_keeps_a_second_admission_waiting_until_release` shows that a second admission waits while the lock is held.
- `RemoteEntryTests.setUp` sets a private `STAFF_FILE` inside the test root before `warm_claim_servers()`, so admission reads no live run pointer.

**Files:**
- `scripts/copies/test_review_remote_copies.py` — the module

**Gotchas:** without a private `STAFF_FILE`, admission reads the live staff record and its run pointers.

**Ruled out:** a CPU claim; the before and after samples ran under different loads.

### Phase 81 — Affected-module tests remove their Git repository without a race  · status: done

#### As-built

`production/test_affected_modules.py` sets `maintenance.auto false` in each test repository before its first commit. No Git process writes into `.git` after a test body ends, so `TemporaryDirectory` cleanup never fails with "Directory not empty". The module passes 20 runs in a row under load; the proof driver fails on the old fixture and passes 100 repetitions on the new one.

**Files:**
- `scripts/production/test_affected_modules.py` — the module

**Gotchas:** `git commit` starts a detached `git maintenance run --auto`. Its repack writes `.git/objects/info/packs` after the commit exits, so any fixture that commits in a tempdir repository can race its own cleanup.

**Ruled out:** a retry, a sleep or `ignore_cleanup_errors` in cleanup.

### Phase 82 — The branch takes builds with its package imports  · status: done

#### As-built

Merge 82e4ed73a puts builds 7ab78b74e into this branch with phases 35 to 83. Its parents are the Phase 83 checkpoint and the builds tip. Entry files run through `scripts/lib/py`, and imports use the package form (`from scripts.<package>.<module> import ...`).

- `queue_lines.step_text(step, names)` and `queue_lines.step_prose(step, names)` show the project with the step's shown or sentence name from `StepNames`. `queue_card`, `queue_evidence` and `queue_view` pass the model's names.
- `build_steps.RECORDED_STEP_NAMES` shows each step as the queue records it. `admission_client` uses it and reads no configuration.
- `merge_gate.py` keeps the suite mode (`SuiteMode`) and the aws machine (`SharedMachine | WholeMachine`). `required: FullSuite | NoFullRunRequired` is bound to `NoFullRunRequired()` before `if shared:`.
- `aws_target/test_gate_trial.py` expects the stuck fixture to end on TERM: exit `-signal.SIGTERM` and the log text `exit=-15`.
- `direct/test_verify_mac_only.py` gives the target table its fifth field, the platform (`macos`).

**Files:**
- `scripts/build_queue/queue_lines.py`, `queue_card.py`, `queue_evidence.py`, `queue_view.py` — project and step names
- `scripts/build_queue/admission_client.py`, `scripts/lib/build_steps.py` — recorded step names
- `scripts/production/merge_gate.py` — mode and machine
- `scripts/aws_target/test_gate_trial.py`, `scripts/direct/test_verify_mac_only.py`, `scripts/build_queue/test_queue_card.py` — test expectations

**Gotchas:**
- The package-import converter refuses `scripts/test_times/module_run.py`; that file keeps `sys.path[0] = os.getcwd()`.
- `scripts/build_hold/build_hold.py` is frozen and equals builds' copy.
- `direct/test_claude_worker.py` can fail once with `BrokenPipeError` at `delivery.sendall` and pass alone.

### Phase 83 — The gate tests need no real type checker  · status: done

#### As-built

`scripts/production/test_merge_gate_suites_types.py` holds 23 tests. None runs the real `basedpyright`, and the module does not import `venv`. A gate host needs no type checker install to run the gate tests.

**Files:**
- `scripts/production/test_merge_gate_suites_types.py` — merge gate type-check tests with stubs only

### Phase 84 — The lint cache version and step state tests use half their CPU  · status: done
**Depends on:** Phase 67

#### As-built

The lint cache version and step state tests use 66% less CPU, with all 13 tests kept. Same-time pairs on `/dev/shm`: 50.9 to 17.1 CPU s. `test_verify_lint_cache_versions.py` 22.6 to 9.7, `test_verify_step_state.py` 28.3 to 7.4.

- `test_verify_lint_cache_versions.py` checks the Mend, Rustfmt, Clippy and Taplo version keys by direct calls to the cache helper, with fresh version files per call. Two real `verify.sh` runs stay: startup with pass publication, and empty-version handling through startup.
- `test_verify_step_state.py` shares one recorded lint sequence per class. The tree-step test calls the production `run_lint_sequence`, `fmt_cargo` and `invoke_mend` with the real `run`.

**Files:**
- `scripts/direct/test_verify_lint_cache_versions.py` — version-key tests by direct call, plus two real runs.
- `scripts/direct/test_verify_step_state.py` — step-state tests on one recorded sequence per class.

**Gotchas:**
- A direct-call test goes through the production function that holds the checked branch. A test that calls a lower helper passes while that protection is bypassed; only a mutation of the branch shows it.

### Phase 85 — The style ledger ticket and finding tests use half their CPU  · status: done
**Depends on:** none

#### As-built

The style ledger ticket and finding tests use 49% less CPU, with all tests kept. Same-time pairs on `/dev/shm`: 89.4 to 45.2 CPU s. `test_style_ledger_tickets.py` 45.4 to 22.7, `test_style_ledger_findings.py` 44.0 to 22.5.

- Each module runs `style_staff_fixture` once per class and copies the staff record to each test with its own paths.
- Each module stops Git maintenance in its fixture repository, so no background repack writes during cleanup.

**Files:**
- `scripts/style/test_style_ledger_tickets.py` — class staff seed, copied per test.
- `scripts/style/test_style_ledger_findings.py` — class staff seed, copied per test.

**Gotchas:**
- The per-test cost is in `LedgerTestCase.setUp`, which six modules share. Phase 90 moves the seed there and removes the two module copies.
- A text replace of paths misses a JSON-escaped path. Phase 90 decodes the JSON to rewrite them.

### Phase 86 — We know which tests to remove, merge or move to streamline the suite  · status: done

#### As-built

- Two reports sort every test module under `scripts/` into Retired, Trivial, Repeat, Cross-module repeat, Table and Wording candidates. They change no test.
- Report A covers `production`, `direct`, `agents`, `lint`, `message`, `hooks` and `lib`: 433 modules and 8,320 test methods. It has no safe deletion, 309 Table candidates in 127 groups, 3 Wording candidates and 736 conditional Retired candidates in a module that a later hierarchies phase deleted.
- Report B covers every other folder: 300 modules and 10,453 test methods. Its Table merges remove an estimated 307 methods and keep every input case.
- Both reports credit CPU savings only to the deletion of that retired module. Every Table merge credits 0 CPU s per gate.
- Each report ends with growth patterns and one proposed rule for each. The user approved rules 1 and 2 on 2026-11-07.

**Files:**
- `docs/plans/builds-natest-streamline-a.md` — report A
- `docs/plans/builds-natest-streamline-b.md` — report B

**Gotchas:** a Table candidate is not proof of one body shape. A sample of ten groups found 9 of 10 in report A and 5 of 10 in report B with one shape. Check each group body before a merge.

**Ruled out:** a Table-merge cleanup phase, because the merges save no CPU.

### Phase 87 — The three costliest production modules keep only the rare-case tests that matter  · status: done
**Depends on:** none

#### As-built

- Rule 2 (keep a rare-case test only when its defect shows the user a wrong result, damages saved state or loses work) applies to three production modules.
- `test_fast_track.py` drops four fixture-only rare-case tests and `GIT_HANG_GUARD`: 56 → 52 tests.
- `test_staff.py` drops the staff-row scan-count test, `CountedStaffRows` and its `Iterator` import: 129 → 128 tests. `DocumentLookupTests` sets `maintenance.auto false` in its main repository.
- `test_add_unit.py` keeps every test: each is a main path or guards saved state.

**Files:**
- `scripts/production/test_fast_track.py` — fast-track main paths and the kept rare cases.
- `scripts/production/test_staff.py` — staff record tests.

**Gotchas:**
- Same-load CPU did not change (fast_track 57.56 → 57.48 s, staff 28.65 → 28.60 s). The cost of these modules is the temporary git repository each test builds, not the rare-case tests.

**Ruled out:**
- Rule 2 removal as a CPU tool for these modules: no measured gain.

### Phase 88 — The fast-track, staff and add-unit tests build their temporary Git repository once per module  · status: done
**Depends on:** none

#### As-built

The staff, add-unit and fast-track test modules use less CPU, with all 14,774 assertion sites kept. The Git seeds were already built once per class; the real costs were 42 fake SSH process starts and Git maintenance. `staff_test_support.py` now runs the fake SSH calls inside one owner process per module, and the other fixtures turn off Git maintenance in their test repositories. Same-load CPU: the staff modules 49.2 to 26.2 s, add-unit 23.7 to 14.6 s, fast-track 66.6 to 58.4 s.

**Files:**
- `scripts/production/staff_test_support.py` — the staff fixture and its fake SSH owner process
- `scripts/production/add_unit_test_support.py` — the add-unit fixture; serializes fake process publication
- `scripts/production/merge_checkpoint_test_support.py` — the merge-checkpoint fixture
- `scripts/production/test_fast_track.py` — the fast-track tests

**Gotchas:**
- Cleanup kills the owner's process group after a timeout, then reaps the owner. A hung fake SSH call otherwise leaves a forked child, and cleanup waits with no limit.
- A group signal in `scripts/` goes through `process_group.send_process_group`; `lib/test_process_group.py` fails on a direct `os.killpg`.
- The fake SSH script loads the package with `scripts/lib/package_root.py`, not `sys.path`.

**Ruled out:**
- One Git seed per module: the seeds were already built once per class.

### Phase 89 — The main assign launcher test file uses half its CPU  · status: done

#### As-built

- `assignment_helpers()` in `assign_launcher_test_support.py` seeds the interpreter cache once and patches `subprocess.run`: `phase_table.py` runs through `invoke_cli(phase_table, phase_table.main, ...)` and `findings.py` through `invoke_findings(...)` in the test process. The patch prints captured output when the caller does not capture it and honors `check`.
- `AssignLauncherFixture.setUp` and `PaneReviewLauncherTests.setUp` enter the context. `HookAssignmentFixture` and the native completion step stay unchanged.
- 63 tests stay, and no assertion is removed. `module_cost.py` CPU 95.0 → 61.1 s, processes 4444 → 3785, execs 3939 → 3449, wall 96.9 → 71.8 s. Same-load pairs (/tmp roots, default bytecode cache): CPU 47.2 → 26.0 s (44.8% less), wall 51.3 → 29.0 s.
- The module is at a CPU floor: `models_registry.launch_settings` starts a shell and a Python registry command for each launch. The models unit owns that resolver change.

**Files:**
- `scripts/direct/assign_launcher_test_support.py` — shared assign launcher fixture; `assignment_helpers()` runs helper commands in the test process
- `scripts/direct/test_assign_launcher.py` — main assign launcher tests

**Gotchas:**
- `assign_launcher_test_support.py` is the hub every assign launcher test module imports. One writer holds it.

**Ruled out:**
- An `assign.resolve_model` change: it leaves the process count unchanged.

### Phase 90 — The style ledger tests seed staff once per class and close every database  · status: done
**Depends on:** Phase 85

#### As-built

The six `LedgerTestCase` modules use 28% less CPU, with all tests kept. Same-load pairs: 109.0 to 78.3 CPU s. `test_style_ledger_sites.py` 37.8 to 19.0, `test_style_ledger.py` 22.1 to 11.3, `test_style_ledger_corrections.py` 2.11 to 1.33, `test_refill_speed.py` 2.15 to 1.63. The tickets and findings modules keep their Phase 85 times (22.2 and 22.9).

- `LedgerTestCase.setUpClass` runs `style_staff_fixture` once against the template repository and keeps the staff record text in `staff_seed`.
- `LedgerTestCase.setUp` decodes that record, rewrites each `doc.path` and `worktree.path` under the test root, writes it, and enters `isolated_staff_file`.
- The template repository sets `maintenance.auto false` before its first commit.
- `judgment_rows` and the three database reads in `test_style_ledger_sites.py` use `closing(sqlite3.connect(...))`, so each connection commits and closes.
- `test_style_ledger_tickets.py` and `test_style_ledger_findings.py` hold no seed code of their own.

**Files:**
- `scripts/style/ledger_test_support.py` — `LedgerTestCase` per-class staff seed and per-test copy.
- `scripts/style/test_style_ledger_tickets.py`, `scripts/style/test_style_ledger_findings.py` — use the shared seed.
- `scripts/style/test_style_ledger_sites.py` — closes its SQLite connections.

**Gotchas:**
- Path rewrites use `relative_to(template_repo.parent)` on the decoded JSON, never a text replace: a non-ASCII `TMPDIR` is escaped in the JSON text.

### Phase 91 — The SSH result tests pass under load, whatever time an admission pass takes  · status: done
**Depends on:** none

#### As-built

The eight SSH result tests no longer depend on how long an admission pass takes. `VerifySshTargetsFixture.control_admission_clock()` runs the admission client with a frozen monotonic clock (`ADMISSION_BEFORE_RETRY`), so no leave pass can fire before local admission. `test_verify_ssh_targets_results.py` calls it in `setUp`. No interval changed, and all 47 assertions and exact attempt counts stay. Under load: 20 of 20 passes. Module CPU 59.9 to 49.9 s.

**Files:**
- `scripts/direct/verify_ssh_targets_test_support.py` — the fixture; `install_admission_client(script)` routes `lib/py .../admission_client.py` to a test script (the frozen clock or the memory-hook observer)
- `scripts/direct/test_verify_ssh_targets_results.py` — the result tests

**Gotchas:**
- While a local step waits for admission, the leave hook retries a free non-Mac target at each running pass. This is the intended production policy (verify-unit, 2026-11-08): fix such flakes in the tests, not in `verify.sh`.
- The frozen clock also freezes the client's failure, silence and outage timers. An unreachable admission service in a result test ends at the test hang guard.

**Ruled out:**
- A longer `running_pass_seconds`: the frozen clock removes the dependence without a slower interval.
- Live failure deadlines under the frozen clock: only a broken admission service reaches them, and the test still fails.

### Phase 92 — A merge gate's test files in tmpfs end with their module  · status: done
**Depends on:** none

#### As-built

A find with no code change; the builds showrunner closed it so on 2026-11-08. At 11:19 UTC the builds slice held 27.48 GiB of shmem. The gate tmpfs is `/dev/shm` (30.2 GiB). In the same window the herdr gate's `test_verify_mac_memory_handoff.py` recorded a 27,000.8 MiB peak and `test_verify_mac_local_release.py` 9,383.4 MiB; their reruns stayed under 254 MiB. The sizes match, so the runaway verify module is the likely writer. The evidence went to builds for verify Phase 41r.

**Files:** none.

**Gotchas:**
- The files stay only while the module runs. At herdr 7c5e53574, `finish_gate_job` kills the module's process group and removes its `TMPDIR` when the module ends, so `merge_gate.py` has no leak.
- The gate clone was deleted, so no file list or file sizes remain. The match is by size and time only.

**Ruled out:**
- A gate cap on a module's `TMPDIR` size: it is a new check that needs the user's approval, and verify Phase 41r fixes the cause.

### Phase 93 — The build queue shows a merge gate as "merge gate", with no hyphen  · status: done
**Depends on:** none

#### As-built

The build queue shows each step by its configured name. `config/build_steps.conf` `[names]` maps `merge-gate` to "merge gate" and `targeted-test` to "targeted test". The queue table, the cards and the cloud rows show names through `StepNames.shown`; recorded step ids and keys stay raw.

**Files:**
- `config/build_steps.conf` — `[names]`: the shown name of each step id
- `scripts/build_queue/queue_view.py` — cloud row text shows `model.names.shown(identity.step)`
- `scripts/build_queue/test_queue_view.py`, `test_queue_card.py`, `test_build_queue.py` — read names through `read_step_names()`, never a mapping in the test
- `scripts/build_queue/golden/` — cloud and unit-override card and view files show the configured names

**Gotchas:**
- A shown name can hold a space. Row tests read table cells by header column, not by a whitespace split.

### Phase 94 — The merge gate stops a module that goes above its memory limit  · status: done
**Depends on:** none

#### As-built

On a shared machine, a merge gate stops a test module whose own memory sample (resident plus swapped memory of the module and its descendants) goes above `merge_gate_governor_gib` (4 GiB). The stop uses the time-limit path: `killed_for` names the peak and the limit, and `stop_gate_jobs` ends the module. The module runs again alone up to `memory_stop_reruns` times (`config/merge_gate.conf`, 1). These reruns do not count toward `RERUN_LIMIT`, and a further overrun fails the gate. A stopped run records no peak in `module_peak_mib.json`.

Each stop sends sdlc one message through `session_send.send_argv`, after the result is known. It names the gate mode, branch and checkpoint, the module, its peak, the limit, and "runs again alone" or "the gate fails". A failed send prints one line and does not change the gate result.

**Files:**
- `scripts/production/merge_gate.py` — the stop in the sampling loop, `tell_memory_stop`, `can_retry_memory_stop`, the memory rerun pass
- `config/merge_gate.conf` — `memory_stop_reruns`
- `config/README.md`, `docs/as-built/merge-gate-and-test-speed.md` — the stop, the rerun and the message
- `scripts/production/merge_gate_test_support.py` — fake `send.py` (a send-fails file makes it exit 3) and the `sampling_module_memory` hook
- `scripts/production/test_merge_gate_suites_reruns.py`, `test_merge_gate_suites_resources.py` — stop, rerun, fail and message rows

**Gotchas:**
- `WholeMachine` mode has no admission, so it has no memory stop.
- A tripwire result never reruns; its memory-stop message says "the gate fails".

### Phase 95 — The verify wait-holder test file uses half its CPU  · status: done
**Depends on:** none

#### As-built

`scripts/direct/test_verify_wait_holder.py` keeps its 29 tests and 125 assertions at a measured floor of 81.5 CPU s (same-load pairs: 94.1 to 81.5 CPU s, wall 127.8 to 114.4 s).

- `RecordedWaitFixture` seeds `isolate_run_sources` and `record_phase` once for each class. `VerifyMemoryWaitHolderTests` and `VerifyRecordedTokenIdentityTests` use it.
- `VerifyTargetWaitHolderTests.setUpClass` seeds the recorder once, and `setUp` applies the recorded environment again.
- `isolate_run_sources` sets `UNIT_DETACHED_RUN=1`, so `verify.sh` skips its detach attempt.
- The memory observer is a shell wrapper. It sends only `admission_client.py admit` calls to the Python observer.
- Target tests call the real `build_targets.py`. Only the pinned starting-target case uses `capacity-tool.py`.
- The floor is held by real target, reservation and recorder commands in production code.

**Files:**
- `scripts/direct/test_verify_wait_holder.py` — verify wait-holder tests with class seeds

**Gotchas:**
- A class seed keeps absolute paths valid only because the class root is the same as each test root.

**Ruled out:**
- Direct calls for routing cases: no narrow production function exists, and a fixture must not copy the routing decision.
- The remaining cost is verify-unit's: `step_selection.py` imports (2.4 CPU s measured), a lazy `cell_value` import (1.6 CPU s measured), and a routing boundary (up to 15.0 CPU s estimated).

### Phase 96 — A changed config file selects the tests that read it  · status: done
**Depends on:** none

#### As-built

A changed `config/<name>.conf` selects every test module whose transitive import closure reaches a source that reads `<name>`. There is no hand map. Full-tree selection takes 22.98 s with an empty fact cache and 0.39 s with a warm cache.

- A source reads `<name>` through an AST call `conf.<fn>("<name>")` on the conf module (`import conf`, `from scripts.lib import conf`, `import scripts.lib.conf [as x]`), through a call to a function imported from `scripts.lib.conf` with a string-literal first argument, or through a `<name>.conf` path token. The read is the literal's file name with `.conf` removed.
- `SourceFacts` carries `config_reads` and `syntax` (`ValidPython` | `InvalidPython(message)`). `SourceGraph(imports, tokens, config_reads)` is the parsed graph; `transitive_config_reads` carries reads back through importers. `EXTRACTOR_VERSION` is 6.
- Every tracked source is parsed and its facts are cached by blob (`source_blobs`), so a warm cache parses only changed blobs.
- A changed `.py` file still selects only through direct imports, tokens and the same folder.

**Files:**
- `scripts/production/affected_modules.py` — selection, fact extraction and the blob fact cache
- `scripts/production/test_affected_modules.py` — selection tests, with rows for a two-hop reader, a function import, a path token, an unrelated config and a warm cache

**Gotchas:**
- A syntax error in a non-test source does not raise. Only an invalid test module raises `SyntaxError`.
- At Phase 96, `config/postmaster.conf` selected 574 of 738 modules: 572 reached `send_budget.py`, most through a module that a later hierarchies phase deleted. Only the targeted test widens; merge gates run all modules.

**Ruled out:**
- A narrower closure: the wide selection follows the approved design.

### Phase 97 — The remote copies review test uses half its CPU again  · status: done

#### As-built

`copies/test_review_remote_copies.py` uses 52.6% less CPU, with all 29 tests and 167 assertions kept. Three same-time pairs on `/dev/shm`: median CPU 45.8 to 21.7 s, wall 51.8 to 24.8 s. `module_cost.py`: 60.9 to 23.6 CPU s, 771 to 568 processes, 848 to 609 execs. No floor and no architecture proposal.

- `RemoteEntryTests` saves each interrupted mirror once per `(moment, stage)` key: `save_interrupted_mirror` copies the tree and its removal authority into a class seed under `interruption_root`. Later cases call `restore_interrupted_mirror`, which copies them back and registers the mirror again.
- `remove_trees_through_cli` runs the real `copies.main` remote CLI in process through `invoke_cli` (`scripts/production/cli_fixture.py`). It patches `copies.subprocess` only for that call, so no SSH interpreter starts. The two recovery-interruption matrices use it.

**Files:**
- `scripts/copies/test_review_remote_copies.py` — remote copy entry, interruption and recovery tests.

**Gotchas:**
- A restored seed must call `copies.register_remote` again, because the registry lives outside the copied tree.
- Other SSH paths in the module stay real.

### Phase 98 — The merge gate resources test never waits out its hang guard  · status: done

#### As-built

- `SharedCPUPool.acquire` in `scripts/production/merge_gate.py` waits in `select` on the jobserver descriptor and a private signal wakeup pipe (`os.pipe2(O_NONBLOCK | O_CLOEXEC)`, `signal.set_wakeup_fd`). A stop signal wakes the wait when the kernel delivers it to another thread, or when it arrives just before the wait starts. Either case used to leave the gate blocked in `read()` until the 600 s hang guard.
- When the jobserver is readable, `acquire` takes the token through `try_acquire` inside `cancellation.defer_cleanup_signals()`. Another gate can take the token first. The loop then waits again. `try_acquire` keeps the empty-read `GateRefused` and slot ownership.
- `test_merge_gate_suites_resources.py` patches `select.select` and waits on the real `pool.blocking`. `test_stop_during_a_blocking_read_returns_every_owned_slot` has rows `before_wait`, `process` and `sender_thread` (`signal.pthread_kill` to the sender thread). `test_stop_after_blocking_acquisition_returns_the_new_slot_too` has rows `read` and `return`. Every row requires every token to return.
- Measured on copies with a 3 s guard: guard waits 11/40 alone and 1/40 under CPU load before; 0/40 and 0/40 after.

**Files:**
- `scripts/production/merge_gate.py` — `SharedCPUPool.acquire` (owner sdlc devtools-unit)
- `scripts/production/test_merge_gate_suites_resources.py` — the stop and acquire rows

**Gotchas:**
- The wakeup descriptor is set and restored inside `cancellation.defer_cleanup_signals()`. `ExitStack` restores the previous wakeup descriptor before it closes the pipe.

**Ruled out:**
- A signal pipe in the test fixture only: it hides the production race.

### Phase 99 — The Claude assign launcher test file uses half its CPU  · status: done

#### As-built

- `direct_launcher_helpers()` in `scripts/direct/assign_claude_test_support.py` patches `subprocess.run`. It runs the `progress_history`, `phase_reservation`, `worker_state` and `build_folders` CLIs in the test process through runpy and `invoke_cli`.
- Merged output (`stdout=PIPE, stderr=STDOUT`) uses `invoke_cli(..., combined_output=True)` and returns `CompletedProcess(args, code, stdout, None)`.
- The fixture flag `direct_cli_calls: ClassVar[bool] = False` selects the adapter. When it is set, the child source runs inside `with direct_launcher_helpers():` (`child_bootstrap` + `child_source`).
- `run_assignment` runs the child in process, with patched `scripts.direct.__path__` and the assign and assign_claude `SCRIPTS` and `PYTHON` values, unless `process_boundary_for_case` names the case.
- 19 tests and 76 assertions stay; 6 tests keep a real process boundary. `module_cost.py` CPU 49.3 → 14.9 s. Same-load pairs on `/tmp`: CPU 43.4 → 22.4 s (0.517), wall 59.7 → 33.0 s.

**Files:**
- `scripts/direct/assign_claude_test_support.py` — the adapter and the in-process child
- `scripts/direct/test_assign_claude.py` — the launcher tests
- `scripts/direct/test_assign_claude_test_support.py` — the adapter against a real run, 3 capture modes × 2 arguments

**Binds later work:** `test_assign_launcher.py` imports `assign_claude_test_support.py`. A reuse of `direct_launcher_helpers()` from `assign_launcher_test_support.py` imports it inside the function that uses it, to avoid an import cycle. `direct_cli_calls` on the Claude fixture does not change `AssignLauncherFixture`.

**Gotchas:**
- A merged-capture caller must get `stderr=None`; `invoke_cli` merges the streams itself.

### Phase 100 — The director turn-wait hook tests use half their CPU  · status: done

#### As-built

- `DirectorWaitFixture` takes `execution: Literal["direct", "process"] = "direct"`; `director_wait_fixture(*, execution=...)` passes it on.
- The direct path builds a `ConversationPauseFixture`, sets its `root`, and runs the real hook through `execute_script(script, [str(HOOKS / hook)], environment, payload)`. A `subprocess.Popen` patch keeps each child; the fixture reaps each one with `wait(timeout=TEST_HANG_GUARD_SECS)` and restores the SIGALRM handler and ITIMER_REAL.
- Two tests keep `execution="process"`: `test_final_decision_and_gate_lines_start_one_user_wait_and_allow_the_stop` and `test_typed_user_prompt_finishes_the_user_wait_before_conversation_pause`.
- 25 tests and 48 assertions stay. `module_cost.py` CPU 33.3 → 4.0 s, processes 315 → 225, execs 283 → 193, wall 36.1 → 5.7 s. Same-load pairs (TMPDIR on tmpfs, sockets under `/tmp`): CPU 26.1 → 3.9 s (85% less), wall 27.3 → 4.9 s.

**Files:**
- `scripts/hooks/test_direct_turn_wait_hooks.py` — director turn-wait hook tests; the hook runs in the test process by default

**Gotchas:**
- A test that asserts on Stop output or a typed prompt across the process boundary keeps `execution="process"`.

### Phase 101 — The herdr tests use half their CPU  · status: done

#### As-built

- `HerdrTests.setUp` patches `subprocess.run`: a synchronous fake herdr command runs through `run_fake` in the test process. Tests marked `@process_boundary` (from `scripts.production.cli_fixture`) keep a real process.
- `run_fake` replaces the environment when the caller gives `env` and inherits `os.environ` otherwise, as `subprocess` does.
- `StartupClock.advance(seconds)` drives a test clock. The two `LaunchDestinationTests` refusal tests patch `herdr.time` with `SimpleNamespace(monotonic=..., sleep=clock.advance)`, so no ten-second production deadline elapses.
- 370 tests stay, and no assertion is removed. `module_cost.py` CPU 44.1 → 5.6 s, processes 1092 → 122, execs 1069 → 99, wall 72.5 → 6.2 s. Same-load pairs: CPU 26.0 → 5.7 s (78% less), wall 46.6 → 6.2 s.

**Files:**
- `scripts/herdr/test_herdr.py` — herdr library tests; fake herdr commands run in the test process by default

**Gotchas:**
- Three tests keep a real process through `process_boundary`: pane labels, Darwin process identity and an unreadable observation.

### Phase 102 — The verify result environment tests use half their CPU  · status: done

#### As-built

- `test_result_values_distinguish_unset_empty_and_changed_values_in_saved_passes` runs one shared first pass and repeat with `NO_COLOR` and `CLICOLOR` set, under `--filter result_values`. Then, for each name, it sets the value to empty and then unsets it, and each step runs again.
- Real `verify.sh` calls drop from 18 to 16. 7 tests and 29 assertions stay, and no assertion is removed.
- Same-load pairs: /tmp roots, default bytecode cache, CPU 41.2 → 37.7 s (8.3% less); /dev/shm roots, empty bytecode cache, CPU 77.6 → 70.5 s (9.1% less).
- The module is at a CPU floor: real `verify.sh` routing, admission, key and record work (`build_targets.py`, `tree_watch.py`, `admission_client.py` starts). verify-unit owns the target-selection, recorder import and routing boundary proposals for that cost.

**Files:**
- `scripts/direct/test_verify_result_environment.py` — result environment behavior through real `verify.sh` runs

**Gotchas:**
- Bytecode cache state changes the measured CPU by about two times. Compare readings only under the same roots and cache state.

**Ruled out:**
- Direct policy calls for the remaining cases: they cannot prove Cargo input, cache reuse or remote command input.

### Phase 103 — The progress history tests use half their CPU  · status: done

#### As-built

- `test_progress_history.py` runs `phase_table.py` in process: a `subprocess.run` side effect in setUp sends only `phase_table.py` commands to `invoke_cli(phase_table, phase_table.main, ...)`; every other command runs as a real process.
- Findings calls use `invoke_findings`, and recorder calls such as `phase-count` use `invoke_recorder`. setUp seeds the interpreter cache with `seed_interpreter_cache`.
- setUpClass builds one seed Git repository (`git init -b feature/rubric`). setUp copies it with `shutil.copytree`.
- Real processes stay for concurrent writers, cold imports and the recorder refusal. All 286 tests, 1,020 assertion rows and 6 embedded assertions are kept.
- CPU under module_cost (no bytecode writes): 61.5 -> 26.1 s sequential, 41.8 -> 17.7 s pair median (57.6% less); processes 2124 -> 1757; execs 2231 -> 1871. Pairs on `/tmp` with a warm bytecode cache: 24.8 -> 17.1 s (31% less).

**Files:**
- `scripts/direct/test_progress_history.py` — progress history recorder tests

**Gotchas:**
- `invoke_cli` ignores the `timeout` argument of the routed `phase_table.py` calls.
- Fewer child starts save more CPU with no bytecode writes than with a warm cache. State the bytecode condition with each measurement.

### Phase 104 — The AWS box tests use half their CPU  · status: done

#### As-built

- `test_box.py` runs the fake aws and ssh tools in process. setUp replaces `aws_target.subprocess` with a namespace whose `run` is `run_tool`, except for cases marked `@process_boundary`.
- `run_tool` resolves the tool with `shutil.which`. A tool in the test's own `bin` folder runs the compiled fake source (`FAKE_CODE`) through `invoke_cli`; any other tool runs through the real `subprocess.run`. It sets `result.args` and calls `check_returncode` when `check` is set.
- `@process_boundary` keeps a real process for stdin and exit-status cases, such as the SSM stdin key test and the refusal stderr test.
- All 61 test bodies and 337 assertions are kept. 46 moved cases fail when the production function under test is broken. `test_deploy.py` passes.
- CPU in worker pairs on tmpfs with no bytecode writes: 136.1 -> 9.2 s pair median (93.2% less); 98.4 -> 8.7 s sequential; processes 1296 -> 150; execs 1297 -> 151. Pairs on `/tmp` with a warm bytecode cache: 24.3 -> 3.2 s CPU (86.8% less), 24.5 -> 3.4 s wall.

**Files:**
- `scripts/aws_target/test_box.py` — box command tests; fake tools run in process

**Gotchas:**
- `invoke_cli` restores signal handlers and changes the process cwd, environment and streams. Call it only for sequential calls on the main thread.

### Phase 105 — The change page tests use half their CPU  · status: done

#### As-built

- `test_change_page.py` builds one seed Git repository and copies it for each test.
- setUp patches `subprocess.run` with a `run` side effect. A main-thread call of a test-owned CLI entry runs in process through `invoke_cli`. A call from any other thread, from a `@process_boundary` case, or for another command runs through the real process.
- `serve.py` answers HTTP calls on worker threads, so CLI calls made inside an HTTP request keep their real `change_page.py`, `change_review.py` and `feature_log.py` processes.
- 7 `@process_boundary` tests keep a real process. All 139 tests and 829 assertions are kept.
- `module_cost` on a copy of `befc1a04a`: 30.7 -> 25.7 CPU s; processes 3040 -> 1001. `change_review.py` keeps 55 starts and 13.4 CPU s from HTTP-thread calls.
- Same-load pairs against `befc1a04a`: `/tmp` with a warm bytecode cache 23.5 -> 17.7 CPU s (24.6% less); `/dev/shm` with no bytecode writes 36.9 -> 18.0 CPU s (51.2% less).

**Files:**
- `scripts/review_links/test_change_page.py` — change page tests; seed repository copy and main-thread in-process CLI calls
- `docs/plans/builds-natest-next.md` — Floors row for the change page tests

**Gotchas:**
- `invoke_cli` changes process state. Use it only on the main thread; a call from an HTTP worker thread keeps a real process.
- The warm-cache floor is `change_page.py` starting `change_review.py` and `feature_log.py` as child processes. An in-process call in production removes about 10 CPU s; sdlc owns that change.

**Ruled out:**
- Queueing HTTP worker-thread CLI calls onto the main thread: it removes the process boundary that the HTTP path needs.

### Phase 106 — The memory admission tests use half their CPU  · status: done

#### As-built

- The config fault tests write a complete config with `write_memory_config` and change only the selected key; a missing key passes `None`. The real reader still recovers the committed value of that key through Git.
- `RepositoryHistoryTests.setUpClass` builds one initialized seed repository (`repository_seed`); `checkout()` copies it with `shutil.copytree` for each test.
- The queue client runs from a generated `queue-client.py` file (`TEST_QUEUE_CLIENT`), built from the source of `queue_leave_client_main`. The real client, shell and socket boundaries stay.
- `ShellTests.test_invoke_twice_with_temporary_home` calls `allow_next_start()` before each shell run.
- All 375 tests and 1194 assertions are kept. Processes: 11100 -> 1614.
- Same-load pairs against `8cd6189b8`: `/tmp` with a warm bytecode cache 19.5 -> 10.2 CPU s (47.8% less); `/dev/shm` with no bytecode writes 26.7 -> 10.1 CPU s (62.1% less).

**Files:**
- `scripts/lint/test_memory_admit.py` — memory admission tests; direct config writes, seed repository copy, generated queue client
- `docs/plans/builds-natest-next.md` — Floors row for the memory admission tests

**Gotchas:**
- `write_config_fault` stays in `memory_test_support.py`: `mac_test/test_offload.py` and `buildlog/test_parse.py` use it.
- The warm-cache floor is the real admission client start at each shell boundary.

**Ruled out:**
- A source-stamp path change in the queue client: about 0.5 CPU s (3%) of gain, with a path-equivalence risk across platforms (showrunner, 2026-11-09).

### Phase 107 — The change page tests drop the processes kept at the floor  · status: done

#### As-built

- `test_change_page.py` no longer patches `subprocess.run` with an adapter. devtools `52256c239` makes `change_page.py` call `change_review` and `feature_log` in its own process, so the adapter had no route left.
- Five tests lost obsolete `@process_boundary` marks; none of them reaches `self.command`. Two marked tests keep six real `change_page.py` starts: exit status, saved state across restarts, and publication from startup paths.
- All 139 tests and 832 assertions are kept. Processes: 879 -> 879.
- The phase base `ea13a357b` was already at the floor. Worker pairs: `/dev/shm` with no bytecode writes 10.384 -> 10.388 CPU s; `/tmp` with a warm bytecode cache 7.022 -> 7.004 CPU s. No gain is claimed.

**Files:**
- `scripts/review_links/test_change_page.py` — change page tests; adapter and five marks removed
- `docs/plans/builds-natest-next.md` — Floors row for the change page tests

**Gotchas:**
- A floor that waits on another unit's change needs a remeasure when that change reaches builds; the gain can already be taken.

**Ruled out:**
- A deferred `feature_log` import in `change_page.py`: no CPU gain in three pairs per condition.
- The architect's reword of the Phase 106 Floors row: the showrunner said to keep that row as it is.

### Phase 108 — The hook dispatch tests use half their CPU  · status: done

#### As-built

- `CopyGuardDispatchTests.dispatch_cases` sends a list of Bash payloads through the real `hook_dispatch.main` and the registered hooks in one isolated main-thread driver process. `invoke_cli` captures each case's return code, stdout and stderr. A driver crash fails the test with the driver stderr.
- Three matrix tests use it: registered dispatch, interpreter vectors with unresolved programs, and recorded commands. Real dispatcher process starts stay for the refusal, pass and first unresolved case.
- All 39 tests, 136 matrix cases and 95 assertions are kept. Sequential `module_cost`: 47.1 -> 13.4 CPU s, processes 411 -> 280.
- Director pairs on base `5db3e42fd`: `/tmp` with a warm bytecode cache 26.9 -> 10.3 CPU s (62% less); `/dev/shm` with no bytecode writes 36.7 -> 12.2 CPU s (67% less).

**Files:**
- `scripts/hooks/test_hook_dispatch.py` — hook dispatcher tests; `dispatch_cases` groups matrix cases in one driver

**Gotchas:**
- Cases in one driver share `sys.modules`. `hook_dispatch._execute` loads each hook as a fresh module, so hook globals do not carry; keep HOME, PATH and cwd the same for all cases of one driver.

### Phase 109 — The sender guard folder goes when its test module ends  · status: done

#### As-built

- `forbid_real_sender()` makes the `sender-guard-` folder with `unittest.enterModuleContext(tempfile.TemporaryDirectory(prefix="sender-guard-"))`, so the folder goes when its module ends. No `atexit` registration and no signal handler exist.
- A module cleanup records the result in the in-memory `real_sender_started` value, restores `TEST_SENDER_GUARD` to its value before the install and resets `refusal_marker`. A later module in the same process makes its own folder. The record is never a file.
- `real_sender_was_started()` returns `real_sender_started` or the existence of the installed marker file. `module_run.py` calls it, and a module that started the real sender exits 1 with `<pattern>: started the real scripts/message/send.py`.
- A `TEST_SENDER_GUARD` value from a parent process makes no folder, and the module cleanup keeps that value.

**Files:**
- `scripts/message/session_send_test_support.py` — guard install, module cleanup, outcome record
- `scripts/test_times/module_run.py` — reads the outcome through `real_sender_was_started()`
- `scripts/test_times/test_module_run.py` — `setup_exit` row; cleanup and no-folder assertions in the runner fixtures
- `scripts/message/test_session_send.py` — `GUARD_PROBE` asserts the marker reset and the kept inherited value

**Gotchas:**
- Module cleanups run in reverse order. The record step registers after the folder context, so it runs before the folder goes.
- A `SystemExit` in `setUpModule` ends the module before its cleanups run. The marker file still exists at that time, so `real_sender_was_started()` reads both sources.

**Ruled out:**
- A guard for a test that runs a second in-process `unittest` runner: no test does this, and module cleanups are process-wide in `unittest`.

### Phase 110 — The nightly type check is a footnote under the slowest modules list  · status: done

#### As-built

- `show.py top` in the `~/.claude` repository ranks test modules only. `types/basedpyright` takes no rank, no `--limit` slot and no share of the `sum of median CPU` line.
- After the sum line, one footnote gives the type check's latest passed run: `types/basedpyright runs nightly, so it is not ranked: last run <cpu> CPU s, <wall> s`. With no qualifying run, no footnote prints.
- The footnote reads the same history query as a module: host, configured checkout, age limit, status 0, no restart, not aborted.
- The `module`, `suites` and nextest views are unchanged, and the gate still records the type check.

**Files:**
- `scripts/test_times/show.py` — ranked rows and the footnote
- `scripts/test_times/test_show.py` — the limit, the sum, the footnote values and the history filters, in three existing tests
- `commands/test_times.md` — the `top` description

### Phase 111 — A targeted test runs only what the unit's own change can reach  · status: done

#### As-built

- `select_affected` in `scripts/production/affected_modules.py` builds its base candidates from the certified base, when one exists, and `git merge-base HEAD refs/remotes/origin/<branch>` for each branch of `landing_branches` in `config/merge_gate.conf`. A missing ref or a branch with no common ancestor gives no candidate.
- It compares each distinct commit once and takes the candidate with the fewest changed paths. On a tie the certified base comes first, then the config order. With no candidate the result is `FullSuite("no certified base")`.
- A landing candidate is a `CertifiedBase` with its committer time, so `report_selection` prints it unchanged. Each landing passed a merge gate that runs all modules, so landed work is not this unit's change.
- `SuiteMode.MERGE_GATE` still runs all modules. Dirty paths, removed-file facts, the module rules and the fact cache are unchanged, and `merge_gate.py` and `cloud_gate.py` take no edit.
- Replay of 19 landings on `builds`: the old rule selects a median 692 of 743 modules and 5193 CPU s, and the new rule a median 6 modules and 78 CPU s (mean 72 modules, 642 CPU s).

**Files:**
- `scripts/production/affected_modules.py` — the base choice
- `config/merge_gate.conf` — `landing_branches`
- `docs/as-built/merge-gate-and-test-speed.md` — the base rule, its reason and the fallback
- `scripts/production/test_affected_modules.py` — landing bases, a merge with a landing branch, a dirty file at the tip, and candidate priority
- `scripts/production/test_targeted_test.py` — the fixture sets a private branch list

**Gotchas:**
- An edit of `config/merge_gate.conf` alone selects 429 modules through the approved transitive config rule.
- A missing `landing_branches` key gives a full suite through the graph-error result.

**Ruled out:**
- The import closure in place of the same-folder rule: replay selects more modules (596 against 426 on one 39-file run).

### Phase 112 — A targeted test skips folder neighbors that do not name the changed file  · status: done

#### As-built

- In `select_affected` in `scripts/production/affected_modules.py`, a changed non-test Python file selects a test module of its folder only when that module's text holds the file's stem as a whole word. Imports, strings and comments each count. Removed files count.
- The selector reads the text only for a module in such a folder that no earlier rule selected. A read or decode error gives the existing graph-error result.
- The other module rules, the base choice and the fact cache are unchanged.
- Replay of 19 landings on `builds`: mean 91 to 68 modules, mean 768 to 590 CPU s, median 77 to 64 CPU s. With no folder rule at all the mean is 67 modules and 581 CPU s.

**Files:**
- `scripts/production/affected_modules.py` — the folder rule
- `scripts/production/test_affected_modules.py` — the changed rows and the new rows
- `scripts/production/test_targeted_test.py` — the fixture's folder neighbor names the changed file
- `docs/as-built/merge-gate-and-test-speed.md` — the fourth rule

**Gotchas:**
- A test that reaches a changed sibling only through a helper, and does not name it, is not selected. The merge gate still runs it.
- The transitive config rule now holds most of the targeted cost: two `config/lint.conf` edits ran 580 modules each, 86% of the replay's CPU. A question to the user about it is open.

**Ruled out:**
- A measured per-module file map: the name check keeps nearly the same gain with much less work.
- Config selection by changed key: replay gain is small (590 to 569 CPU s), because `scripts/lint/sweep.py` alone reaches 477 test modules by imports.

### Phase 113 — A private admission server costs less to start and to keep running  · status: done

#### As-built

- `ObservedAdmissionServer` in `scripts/build_queue/admission_test_support.py` keeps one class-level record of source paths, keyed by the count of `sys.modules`. A fixture start with an unchanged count stamps the kept paths and resolves none.
- `SERVING_POLL_SECONDS` is 0.05. `serve()` marks its thread with `serves_loop`, and `serving()` wakes the loop at stop through `wake_loop`.
- `loop_turn` takes `turn_gate` at depth 0, wakes the loop when the caller is not the loop thread, then takes `loop_access`. A nested call skips `turn_gate`.
- `wake_loop` and `close` both hold `wake_lock`, so a wake cannot write a descriptor that a restart closed.
- Same-load measurement, three rounds: setup plus close 13.4-13.7 ms to 6.0-6.3 ms, idle loop 1.8-3.3 ms/s to 1.1-1.7 ms/s, median turn wait 7.04 ms to 0.02-0.03 ms. Gate estimate: about 27 of 3,056 CPU s.

**Files:**
- `scripts/build_queue/admission_test_support.py` — the poll value, the path record, the turn gate and the wakes

**Gotchas:**
- Shared fixtures are a small part of a gate. Python child processes are 57% of gate CPU, and the helper chain per `verify.sh` call is about 18%.

**Ruled out:**
- Rewriting the process scans with `os` calls: it saves 23%, because the kernel read of each process environment is 77% of a scan.

### Phase 114 — A test that starts Python for one function stops loading what the function does not use  · status: done

#### As-built

- `test_each_module_uses_a_temporary_log` in `scripts/buildlog/test_index.py` runs one `lib/py -c` parent. The parent imports each production module named on its command line, with no `try`, then forks one child per test module. Each child imports its test module and prints `store.root()`. The production list comes from the folder listing, without `test_*.py`, `*_test_support.py` and `__init__.py`. Children run one at a time, so wall time rises from 7.1 s to 16.0 s and CPU falls.
- `CliWork`, `MeasuredCliRun`, `probe_cli` and `measure_cli_work` live in `scripts/buildlog/cli_probe_test_support.py`. `test_report.py` and `test_mac_report.py` import them. The host is read in the parent under `mock.patch.dict(os.environ, environment, clear=True)`.
- `configured_host_cli` in `test_memory_window.py` receives `configured_local_host()!r` from the parent, and its bootstrap imports only `store`.
- The fake child in `scripts/mac_test/test_linux_runner.py` matches `argv[1] == "-c"` with `sweep.sweep_workspace` in `argv[2]`, and yields to `use_real_sweep`. Four tests keep `use_real_sweep = True`.
- Same-load pairs, three rounds, median CPU s: `test_index` 30.7 to 12.0, `test_linux_runner` 27.3 to 13.3, `test_report` 24.8 to 23.1, `test_mac_report` 15.7 to 12.8, `test_memory_window` 7.6 to 7.5; total 106.1 to 68.7. The test names and assertions are the same.

**Files:**
- `scripts/buildlog/test_index.py` — the preloaded parent and its forked children
- `scripts/buildlog/cli_probe_test_support.py` — the shared CLI probe helpers
- `scripts/buildlog/test_report.py`, `scripts/buildlog/test_mac_report.py` — use the shared helpers
- `scripts/buildlog/test_memory_window.py` — the small CLI bootstrap
- `scripts/mac_test/test_linux_runner.py` — the fake child match

**Gotchas:**
- Index isolation must keep a separate test-module import in each child. Sharing that import removes what the test proves.
- Floors per site, by the largest import: `scripts.buildlog.report` 0.41 s, `scripts.buildlog.memory` 0.19 s, style `queue_place` 0.146 s, statusline `update_registration` 0.30 s, `scripts.accounts.accounts` 0.12 s. Each lower cost needs a production import change.
- Blanket no-op helpers in `verify.sh` make every test red. Per helper, `record.py` and `progress_history.py` stay green at 171 CPU s per gate, `tree_watch` fails four lint scope tests, and `step_record` is required.

**Ruled out:**
- A style CLI helper split: 206-249 ms per start against 225-247 ms, because the fake must import `queue_place`.
- Bytecode work: compiling the whole tree costs 3.4 CPU s, and main-script compile across all starts costs 67.3 CPU s.
- A preloaded Python start server: tests inspect real process trees, signals and process groups.
- A test-only path in `verify.sh`: it breaks the one-way rule.

### Phase 115 — Measurements show how much CPU each cheaper Python start saves, and which tests each breaks  · status: done

#### As-built

- This phase is research. No repository file changed for it, and no prototype landed.
- Each start variant has an estimate: the paired per-start CPU difference of the top 20 commands, multiplied by the start counts of the baseline survey. The estimates cover startup, imports and compile for those 20 commands only.

| Variant | CPU s saved per gate | At cloud factor 1.62 | New failures |
| --- | ---: | ---: | --- |
| Preloaded parent | 474.5 | 768.7 | 36 entries, 7 modules missing |
| `-S` | not established | not established | 49, and two 1,800 s timeouts |
| `-I` | 7.5 | 12.1 | 130 entries, 16 modules missing |
| `-I -S` | 26.4 | 42.7 | 139 entries, 15 modules missing |
| Warm bytecode | 19.1 (14.8 net) | 31.0 | 0 |
| Thin entry scripts | 80.3 | 130.1 | 136 entries, 2 modules missing |
| Delayed shared imports | 186.7 | 302.4 | 0, and 1 module incomplete |

- A native survey of 740 of 744 modules records 4,975 CPU s. The interpreter alone costs 13.6 ms per start, 8.6 with `-S`, 12.6 with `-I` and 7.95 with both.
- The routes that start Python: `lib/py` 22,281 execs, an absolute path, `sys.executable` or a shebang 11,885, Python on `PATH` 2,791, and other absolute paths 1,160.
- One `progress_history.py` start costs 40 ms for standard-library imports, 140 ms for repository imports and 50.6 ms for main-script compile. Its large dependencies are `plan_history`, `session_staff` and `phase_reservation`.
- The delayed-imports pilot used the standard-library `LazyLoader` over 309 production modules. It saved 100.5 CPU s across 58 matched passing modules, 1,610.1 to 1,509.6.

**Files:**
- `docs/plans/builds-natest.md` — this record

**Binds later work:**
- Delayed shared imports is the only variant that keeps tested behavior and has a large saving. It lands in the small form, with one start path for tests and real use.
- `scripts/direct/test_review_launcher.py` timed out under the pilot, and its cause is not known.
- The typing and thread behavior of the pilot's `LazyLoader` are not verified.

**Gotchas:**
- One whole-gate pair takes 45 minutes and about 10,000 CPU s.
- The whole-gate confirmation of the preloaded parent stopped with SIGTERM twice, at 94 of 744 modules. The sender is not known.
- `-S` runs need the startup hook. Without it, a pair shows hook failures, so its raw difference of 131 CPU s is not a saving.
- One failure entry can be a module import failure and not one test.
- A process name can hold invalid UTF-8 bytes, so a measurement tool must decode with replacement.

**Ruled out:**
- A preloaded parent: it breaks process ancestry, PID ownership, signals, process groups, startup hooks and pipe ownership.
- `-S`, `-I`, `-I -S` and thin entry scripts: 49 to 139 new failures for 7 to 80 CPU s.
- Warm bytecode: 14.8 CPU s net, which is too small to justify a preparation step.

### Phase 116 — A targeted test run selects every test that reads a changed file as text  · status: done

#### As-built

`names_changed_file(graph, path, changed, folder_names)` in `scripts/production/affected_modules.py` decides whether a test names a changed file as text. A source names the file when one of its tokens equals the whole path, or when a token equals the base name and the source sits in the file's folder or holds a token equal to the folder name. The sources of a test are the test module and each `*_test_support.py` module it reaches through test-support imports; production modules are not followed. `select_affected` builds `folder_names` from the changed paths, so a file at the repository root matches by whole path only. The rule uses the existing tokens and imports: the extractor, `CACHE_FORMAT_VERSION` and `EXTRACTOR_VERSION` are unchanged, and the selector adds no parse and no Git process.

Replay over the last 20 first-parent merges on `origin/builds`, one graph from the current tree: a mean of 16.45 added modules per landing and a maximum of 107 (73 to 180, and 20 to 127). No merge loses a module. The changed set `{scripts/direct/verify.sh}` selects 147 modules, 4 before, and includes `direct/test_verify_lint_output.py`.

**Files:**
- `scripts/production/affected_modules.py` — `names_changed_file` and its call in `select_affected`
- `scripts/production/test_affected_modules.py` — rows in `test_complete_path_tokens_select_only_the_test_that_names_the_path`
- `docs/as-built/merge-gate-and-test-speed.md` — selection rule 3 and the summary

**Gotchas:** A name used only by a fake command also selects the test; the largest additions come from `build_targets.py` and `targets.py` names in verify test-support modules. A folder scan does not name a file: 26 direct test modules scan repository folders, and `targeted_test.py --full` is the full-coverage check for those changes.

**Ruled out:** following production modules for names, because `test_indirect_path.py` must stay unselected; a new source fact or cache version.

### Phase 117 — Scripts and tests start faster because the heaviest shared imports load only when used  · status: done

#### As-built

Four shared imports load at the function that uses them: `unit_lookup` in `read_production` of `scripts/direct/phase_table.py`, `findings` in `_review_trial` of `scripts/direct/progress_history.py`, and `offload` and `buildlog.sync` in each function of `scripts/mac_test/mac_test.py` that uses them. `mac_test.py` keeps `offload` under `TYPE_CHECKING` for its annotations. A whole-gate pair at the same load, phase base against the change, measured 5161.3 to 5122.7 CPU s: 38.7 CPU s saved per gate, above a modeled noise of 26.6 s. Both sides ran 746 modules and 18,556 tests with no missing result and no signal stop.

**Files:**
- `scripts/direct/phase_table.py` — delayed `unit_lookup` import
- `scripts/direct/progress_history.py` — delayed `findings` import
- `scripts/mac_test/mac_test.py` — delayed `offload` and `buildlog.sync` imports
- `scripts/copies/test_review_remote_copies.py` — patches `scripts.mac_test.offload`, the package import target

**Gotchas:** A test that patches a delayed import patches the package attribute, not the old module-level name. Shared import costs overlap, so a ranking by single-import CPU overstates the saving by about half. Readiness-deadline tests can error under whole-gate load on either side of a pair. The SIGTERM that stopped earlier whole-gate runs came from the user systemd manager, through the preloaded design's scope cleanup.

**Ruled out:** a repair for the `test_review_launcher.py` timeout, because it did not reproduce; deferral of imports below 5 CPU s per gate.

### Phase 118 — The test_times report tests pass on every date  · status: done

#### As-built

`REPORT_PROCESS` in `scripts/test_times/test_show.py` replaces the report module's `datetime` with a subclass whose `now` returns the time in `TEST_REPORT_EPOCH`. `report_process` sets `TEST_REPORT_EPOCH` to `EPOCH + 300`, the time the in-process `ReportTime` returns. The report child and the in-process tests use the same fixed clock, so the module passes 33 of 33 tests on every date. The first-report test expects the same-day stamp `rows as of 09:30 EST`; the module-report stamp test keeps the other-day stamp.

**Files:**
- `scripts/test_times/test_show.py` — the report child's fixed clock

**Gotchas:** A patch of `show.datetime` in the parent does not reach a child process; every child report needs `TEST_REPORT_EPOCH`. A fixed fixture date with the seven-day `TOP_MAX_AGE_DAYS` window fails seven days after that date when the clock is real.

**Ruled out:** fast track for this file, because it refuses Python files that import `scripts/production/staff.py`.

### Phase 119 — A targeted test with no selected module ends at once and starts no cloud box  · status: done

#### As-built

A targeted test with no selected module ends at once and starts no cloud box. With requested type checks, it runs them locally.

- `select_targeted_modules(repo, tested_tree, modules, *, source="tests-only")` in `merge_gate.py` is the one targeted selection. It returns the selection and its `PassedTreeLedger`.
- `TreeSelection(paths, tree)` holds the selected paths and the Git tree that the selection read. `AllModules` asks for every discovered module.
- `run_gate_suites` selects before `cloud.route`. An empty selection reuses or records its ledger pass, records the gate run, and returns a certified `GateRun` with no modules.
- `cloud_gate.prepare` takes `selected: AllModules | TreeSelection`, and raises `GateRefused` before any request or bundle when its snapshot differs from `selected.tree`. `run_cloud` includes the modules and the selection tree in its identity.
- A local fallback calls `run_suites` without `selected`, so it selects again against its own tree.

**Files:**
- `scripts/production/merge_gate.py` — selection, `TreeSelection`, `CloudGateRoute`, the empty return.
- `scripts/aws_target/cloud_gate.py` — the tree check and the selected paths.
- `scripts/production/test_merge_gate_suites.py`, `scripts/aws_target/test_cloud_gate.py` — empty selection, tree change during reservation, and fallback rows.

**Binds later work:** a queue merge-gate step merges builds first, keeps selection before `cloud.route`, and keeps the empty return, requested local type checks and fallback selection.

**Gotchas:** the cloud reservation can wait, so a fact computed before it must carry its tree. The targeted test runs main's gate code, so a real cloud run of the empty path is unproved until this code is on main.

**Ruled out:** passing the old selected paths to the local fallback.

### Phase 120 — Test fixtures stop starting Python to check the version  · status: done

#### As-built

A whole gate starts 997 interpreter version checks instead of 3,351. At a paired cost of 20.83 ms per check, this removes about 49 CPU s per gate on natedev and about 78 on a cloud box.

- `scripts/lib/py` runs its version check (`new_enough`) when the interpreter cache file is absent or `CLAUDE_PYTHON` is set. A set `CLAUDE_PYTHON` skips the cache.
- `interpreter_cache_environment(cache_home)` in `scripts/lib/py_floor_test_support.py` seeds the cache with `seed_interpreter_cache` and returns `{"XDG_CACHE_HOME": ...}` for a fixture's environment.
- Fixtures that start scripts through the launcher in a temporary home seed the cache. Fixtures that set `CLAUDE_PYTHON` only to find Python drop it, and pop an inherited value inside their `patch.dict`.
- These fixtures keep their interpreter setting because their tests check it: the hold family (its shell reads `PY=$CLAUDE_PYTHON`), observer and stub interpreters, the floor, root and shim fixtures, cold-cache and too-old tests, and the explicit interpreters of the override-guard, settings and merge-checkpoint fixtures.

**Files:**
- `scripts/lib/py_floor_test_support.py` — `seed_interpreter_cache`, `interpreter_cache_environment`.
- Test modules and support files in `scripts/` agents, build_hold, build_queue, build_targets, direct, hana_shot, hooks, lib, lint, mac_test, message, production, review_links, settings, style, test_times, usage, whoami and worktree — seed or drop per fixture.

**Binds later work:** the approved `scripts/style/style_sync.py` ledger change (one `style_ledger.open_ledger` around the `lint` record loop, `RuleValidationError` and `sqlite3.Error` in the `except`, no `LEDGER` constant or `diagnostic_file` write) is not in this phase.

**Gotchas:** an inherited `CLAUDE_PYTHON` makes a seeded cache useless; pop it inside the fixture's `patch.dict` so cleanup restores it.

**Ruled out:** a test-only start path in `scripts/lib/py`; a whole-gate paired timing for this gain, because its noise is about 27 CPU s.

### Phase 121 — Board helpers and the style lint record stop starting Python  · status: done

#### As-built

`flatten()`, `lock_guard()` and the `style_sync.py lint` record loop start no Python process. The estimated saving is about 42 CPU s per gate on natedev: 28.4 from the two board helpers (paired per-call cost times the Phase 115 whole-gate call counts) and 13.5 from `style/test_style_sync.py` (median 44.0 to 29.5 CPU s in `module_cost.py` pairs).

- `flatten()` in `scripts/direct/board.sh` uses bash parameter expansion. It replaces newline, CR and tab with a space, removes each character outside `[[:print:]]`, and cuts to `MAX_MESSAGE_CHARS` characters. It pins a UTF-8 locale in a `local LC_ALL` (C.UTF-8, then en_US.UTF-8, then UTF-8), so text keeps its non-ASCII characters in a C or empty caller locale.
- `lock_guard()` opens descriptor 9 in the shell and takes `flock(LOCK_EX|LOCK_NB)` with one perl call; perl exits 75 when the lock is busy. The shell keeps the lock after perl exits. The return codes do not change. It finds perl on PATH, then at `/run/current-system/sw/bin/perl` and `/usr/bin/perl`, as `scripts/lib/py` does for python3, so a caller with a cut PATH (`test_verify_lint_tool_failures`) still gets the guard.
- `style_sync.py lint` opens `style_ledger.open_ledger(repo)` once, only when records exist, and calls `ledger.lint_record` per record. `main` also catches `rule_catalog.RuleValidationError` and `sqlite3.Error`. The `LEDGER` constant and the diagnostic file are gone; `style_ledger.py lint-record` stays.

**Files:**
- `scripts/direct/board.sh` — `flatten`, `lock_guard` (owner sdlc).
- `scripts/direct/test_board_text.py` — Unicode and limit rows in the inherited, C and no-locale cases.
- `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py` — in-process lint records (owner style unit).
- `docs/as-built/build-memory-admission.md` — the perl guard line.

**Gotchas:** bash `[[:print:]]` and `${#text}` follow the locale; a launchd job or an `env -i` child has none. perl resolves from the `verify.sh` PATH: `/run/current-system/sw/bin/perl` on natedev, `/usr/bin/perl` on the Mac. The locale rows did not run on the Mac (SSH timed out).

**Ruled out:** a whole-gate paired timing for this gain, because its noise is about 27 CPU s.

### Phase 122 — Progress records start faster because the staff and plan imports load only when used  · status: done (declined)

#### As-built

Declined by the builds showrunner on 2026-11-10: the gain is under the 30 CPU s per gate bar. Checkpoint `8c9858589` was reverted by `25ec7c5ef`; the code is unchanged.

- Change measured: `progress_history.py` and `plan_history.py` imported the staff, plan, reservation and worker modules inside the functions that use them, with type-only imports under `TYPE_CHECKING`.
- `import scripts.direct.progress_history` went from about 87 ms to about 45 ms.
- Paired CPU per start, minimum of 30 pairs on natedev under load: `start-wait` 132.5 to 106.2 ms, `finish-wait` 136.1 to 108.3, `start-activity` 132.7 to 108.3, `finish-activity` 133.7 to 105.0, `review-trial` 137.0 to 115.6. The saving is 21-29 ms per start, about 20 CPU s per gate at 772 starts.
- The probe estimate (about 47 ms per start) was too high: every state read still loads `staff` (about 17 ms) for the run owner, and `models_registry` (about 14 ms) loads for a module constant.

**Ruled out:** a recorder import phase while every state read needs `staff`; a larger gain needs a recorder that does not resolve the owner through staff on each start.

### Phase 123 — A changed shell script selects the tests of the scripts that call it  · status: done

#### As-built

A changed shell script now selects the tests of every script that calls it, directly or through other scripts. A `board.sh` change selects `test_verify_lint_tool_failures`, the test the Phase 121 repair missed.

- `script_callers` in `scripts/production/affected_modules.py` seeds from the changed scripts (non-Python files under `scripts/` that end in `.sh` or have an execute bit, removed `.sh` files included). It adds each script that calls a member until no new caller is found.
- `calls_script` counts a call by the test rule (`names_path`: the full path, or the name with its folder), by a token that ends in `/` and the full path, or by a token that ends in `/` and the name in the caller's own folder (`"$SCRIPT_DIR/board.sh"`).
- Tests select by the unchanged test rule against the changed files plus the closure. Python files are not callers. Script text is read only when a changed script exists; the fact cache does not change. The closure costs 40-50 ms per selection.
- Selection, before and after, of 745 modules (recorded median CPU, full suite 5,075 s): `board.sh` 117 to 264 modules and 1,358 to 2,905 CPU s; `lint` 152 to 264; `build_hold/cargo` 115 to 267; a Python-only change 5 to 5. About 260 tests run `verify.sh`, which sources `board.sh`.

**Files:**
- `scripts/production/affected_modules.py` — `path_tokens`, `folder_names_of`, `names_path`, `is_script`, `calls_script`, `script_callers`.
- `scripts/production/test_affected_modules.py` — the call-chain test, with sibling, full-path, executable, fragment, other-folder and Python rows.
- `docs/as-built/merge-gate-and-test-speed.md` — the targeted selection lines.

**Gotchas:** `~/.claude/scripts/lib/py -m` imports main's code from a worktree; run local tests with the worktree's `scripts/lib/py`.

**Ruled out:** Python files as callers (a `board.sh` change would select 521-575 modules). A changed Python file that a shell script runs still selects only its importers and the tests that name it.

### Phase 124 — Test modules start faster because three test-support files load their heavy imports only when used  · status: done

#### As-built

Three test-support files load their heavy imports at the function that uses them, in the phase 117 form. `memory_test_support.py` imports `read_memory_config` in `admission_configuration_fixture`. `queue_fixture.py` imports `roster` and `UnitLabels` in `QueueFixture.__init__`, and `head_facts` and `queue_view` in `model`; it keeps `queue_view` and `UnitLabels` under `TYPE_CHECKING`. `admission_test_support.py` imports `step_estimator` in `history`. A paired import scan of 745 gate test modules, phase base against the change, measured 149.8 to 117.0 and 143.2 to 115.2 CPU s: about 30 CPU s saved per gate. 440 modules load less, and the classes built fell from 428,232 to 322,648. The targeted gate passed 109 of 109 modules.

**Files:**
- `scripts/lint/memory_test_support.py` — delayed `admission_records` import
- `scripts/build_queue/queue_fixture.py` — delayed `roster`, `unit_labels`, `head_facts` and `queue_view` imports
- `scripts/build_queue/admission_test_support.py` — delayed `step_estimator` import

**Gotchas:** Dataclass creation is about two thirds of test-module import CPU, so a cut is worth the classes it skips. `-X importtime` names only the first loader of a module; use paired CPU scans to rank a cut.

**Ruled out:** a dataclass code cache in the module runner, because it replaces a private stdlib name; a delayed `pane_record` import in `staff_test_support.py`, because its isolation patches `RECORD_DIR` only when that module is already loaded.

### Phase 125 — Message sends and test modules start faster because roster, pane records and unit lookup load the herdr client only when they call it  · status: done

#### As-built

New `scripts/herdr/herdr_types.py` holds the one definition and the one import path of `HerdrFailure`, `HerdrMissing` and `NATIVE_ID_MARKS`. `herdr.py` imports them for its own use and does not re-export them; all 53 caller files, production code and tests, read them from `herdr_types`. `roster.py`, `pane_record.py` and `unit_lookup.py` take `HerdrPlace` from `place.py`, keep annotation-only names under `TYPE_CHECKING`, and import `herdr` in each function that calls the client. Importing any of the three loads no `scripts.herdr.herdr`. A paired import scan of 745 gate test modules found 201 modules that load less, and the classes built fell from 322,685 to 304,696. Those modules saved 8.3 and 5.8 CPU s, about 7 CPU s per gate. A roster read, which each `send.py` roster lookup pays, saved 31-41 ms of a 258-302 ms median at host load above 40.

**Files:**
- `scripts/herdr/herdr_types.py` — new; failure types and native ID marks
- `scripts/herdr/herdr.py` — imports the moved names; no re-export
- 53 caller files across `production`, `herdr`, `agents`, `message`, `accounts`, `hooks`, `direct`, `lib` and `hana_instances` — read the moved names from `herdr_types`
- `scripts/message/roster.py`, `scripts/agents/pane_record.py`, `scripts/production/unit_lookup.py` — delayed `herdr` imports
- `scripts/herdr/test_herdr.py`, `scripts/production/unit_status_test_support.py` — isolated-copy lists add `herdr_types.py`
- `scripts/production/test_unit_lookup.py` — reads `unit_lookup.HerdrPlace`

**Gotchas:** One owner switches all callers: a re-export kept for old callers is a forbidden shim (builds, 2026-11-10). `staff.py` loads `herdr_types` through its lazy `import_module` form. A test that copies `herdr.py` to an isolated folder must also copy `herdr_types.py`. `send.py` loads `roster` lazily, so its bare start does not change. The `-X importtime` self cost overstated the gain by about half, because other paths still load part of the same closure.

**Ruled out:** a split of `admission_records.py` now, because alone it is under the bar and the queue unit has phases there; it is in the next items.

### Phase 126 — A targeted test reads the same working tree each time, so its cloud snapshot check no longer fails at random  · status: done

#### As-built

A private copy of the git index now keeps the mtime of the bytes it copies, so git keeps its racy-entry check. Before, `cloud_gate.working_tree` used `shutil.copyfile`. The copy got a new mtime, and git skipped a same-size edit made in the clock tick of the last index write. This gave the flaky `test_merge_gate_suites` tree mismatch. In 3,300 local repeats, `copyfile` gave 6 stale trees, and a copy that keeps the mtime gave none. New `scripts/lib/git_index.private_copy(source, destination)` opens the index once, takes `fstat` of that file, copies its bytes, and sets the copy's mtime from the same `fstat`. All six Python private index copies call it. `validate_ci.sh` `worktree_tree` copies from one file descriptor and takes the time with `touch -r /dev/fd/3`.

**Files:**
- `scripts/lib/git_index.py` — new; `private_copy`
- `scripts/aws_target/cloud_gate.py`, `scripts/hooks/checkpoint_gate.py` (two copies), `scripts/lib/models_registry.py`, `scripts/copies/copies.py` (`staged_tree`), `scripts/style/style_footprint.py` (`worktree_commit`) — call `private_copy`
- `scripts/validate_and_push/validate_ci.sh` — descriptor copy; ShellCheck removed two unused bindings
- `scripts/aws_target/test_cloud_gate.py`, `scripts/lib/test_git_index.py`, `scripts/validate_and_push/test_rerun.py` — one regression each; each fails with the old copy

**Gotchas:** `shutil.copy2` is not enough. It stats the path again after the read, and git replaces the index by rename, so a rewrite between the two gives old bytes with a newer mtime. Do not copy a git index with `cp` or `shutil.copyfile`; use `git_index.private_copy`. The macOS run of the `validate_ci.sh` regression is not verified.

### Phase 127 — A config edit selects only the tests that really read that config file  · status: done

#### As-built

Config reads are recorded at run time. When the environment has `CONF_READ_LOG`, each read through `conf.py` or `conf.sh` appends the config stem to that file with one append write; without the variable, the readers do no added work. `module_run.py` sets the log per module, so child processes inherit it, and writes the sorted stems to the module's times file as `config_reads`. The gate record and the build log index (schema 37, `gate_modules.config_reads`) carry them. A module with no times file has no record (NULL), which is different from an empty list.

`affected_modules.select_affected` selects, for each changed `config/<name>.conf`:
- the modules that recorded `<name>` in the newest full, non-aborted gate with at least one config record (`latest_config_reads`); a newer gate without records does not replace it;
- the modules with no record in that gate;
- the modules whose own text names the file (direct `SourceFacts.config_reads`).

The transitive config closure is deleted. An index read error gives `FullSuite` with a reason. On 19 builds merges, the estimate is targeted CPU 17,861 to 10,042 CPU s (median 280 to 126 per merge); selection takes under 3 s.

**Files:**
- `scripts/lib/conf_reads.py` — new; the recorder (`record`, `read_text`), standard library only
- `scripts/lib/conf.py`, `scripts/lib/conf.sh` — record each read
- `scripts/test_times/module_run.py`, `scripts/test_times/gate_record.py`, `scripts/buildlog/index.py` — carry the records
- `scripts/production/affected_modules.py` — `latest_config_reads`, `select_affected`
- Nine direct config readers read through `conf_reads.read_text`

**Gotchas:**
- `conf_reads.py` binds `os.open`, `os.write` and `os.close` at import, so a test that patches them does not catch the record.
- A fixture checkout that copies `conf.py` must also copy `conf_reads.py`.
- Nine raw readers do not record: `statusline.sh`, `lint_config.sh`, `progress_history.py`, `verify.sh`, `new_account.py`, `zone.py`, `tune.py`, the `models_registry.py` edit snapshots and `deploy.py`.
- A new reader is missed until the next full gate records it.

**Ruled out:** the transitive closure rule of Phase 96 — one `merge_gate.conf` edit selected 429 of 743 modules.

### Phase 128 — Test runs start no background Git upkeep  · status: done
**Depends on:** Phase 127

#### As-built

- No repository change. The trial gained 2.8 CPU s on the 21 most Git-heavy modules, far under the 30 CPU s bar, and did not land.
- The trial: `module_run.py` appended `maintenance.auto=false` and `gc.auto=0` through `GIT_CONFIG_COUNT`, one fixture helper kept those settings, and nine per-fixture copies were removed. Review closed four findings, and the full gate passed (748 modules).
- Measure: `module_cost.py --commands` on 21 Git-heavy modules, base and change side by side. Command CPU 451.3 to 448.5 CPU s, Git CPU 92.1 to 90.2 CPU s, Git starts 82,028 to 79,432 (3%). No measured row was a detached `gc` or `maintenance` task.
- Floor: the per-fixture copies already turned off most upkeep. The Work Order estimate (about 50 CPU s on 12 modules) did not hold on this base.

**Ruled out:** a runner-wide Git upkeep setting for speed. Keep the per-fixture copies; several of them are race guards for fixture removal.

### Phase 129 — Every config reader records its read, so a config edit selects its tests  · status: done
**Depends on:** Phase 128

#### As-built

- Nine config readers record each config file they read in the `config_reads` record that `module_run.py` writes per module. The targeted selector uses that record to pick the tests that read an edited config file.
- Shell readers call `conf_record <stem>` from `scripts/lib/conf_record.sh`. It is POSIX, appends to `CONF_READ_LOG` with builtins only, and starts no process. `conf.sh` sources it, and `conf_value` calls it.
- `statusline.sh` sources the recorder only when `CONF_READ_LOG` is set. It records `statusline` and the stem of the zone file it reads.
- `lint_config.sh` and `verify.sh` `lint_code_key` record the stem of the lint file they read. `verify.sh` sources only `conf_record.sh` before `lint_code_key`; its `conf.sh` source stays before `TOKEN_HOLD_S`.
- Python readers: `progress_history.py` (`direct`), `new_account.py` (`statusline`), `tune.py` (the source stem) and `models_registry.py` (`models`, once before its snapshot reads) call `conf_reads.record`. `zone.py` and `deploy.py` read through `conf_reads.read_text`.
- Test rows prove the record in `test_conf_sh.py`, `test_statusline.py`, `test_new_account.py`, `test_progress_history.py`, `test_deploy.py`, `test_models_registry.py`, `test_invoke_scope.py` and `test_verify_lint_cache_versions.py`. The lint-cache row routes the log only around `lint_code_key` in its own harness script, so the Clippy reader cannot satisfy it.
- Raw reads that still record nothing: `production/dailies_counts.py` `read_specs`, generic file hashing in `direct/tree_watch.py` and `direct/progress_history.py`, and fixture readers in `findings_test_support.py`, `assign_launcher_test_support.py`, `rename_unit_test_support.py`, `memory_test_support.py` and `offload_test_support.py`.

**Files:**
- `scripts/lib/conf_record.sh` — the shell recorder.

**Gotchas:**
- Seven fixtures copy shell helpers one by one, and each must also copy `conf_record.sh`: `assign_claude_test_support.py`, `review_launcher_test_support.py`, `test_board_start.py`, `test_verify_nix_shell.py`, `test_verify_run_place.py`, `test_process_group.py` and `test_settings_local_keys.py`.
- `verify.sh` runs top-level code before the nix re-exec, and some fixtures copy `verify.sh` alone. Source only `conf_record.sh` there, never `conf.sh`.

**Ruled out:** rows for `tune.py` and `zone.py`, because the parsed read in the same call records the same stem; a test-only read-log branch in `verify.sh`.

### Phase 130 — A test that scans repository folders runs when a file in those folders changes  · status: done
**Depends on:** Phase 129

#### As-built

- `module_run.py` installs the `RepositoryFolderScans` audit hook when a times file is requested. It records `os.scandir` and `os.listdir` listings inside the running checkout as repository-relative folders (`.` for the root), and skips listings made by the import system. A `git ls-files` child process (with or without `-C`) records each pathspec as a recursive prefix with a trailing `/`; no pathspec records `/`, the whole repository. The sorted set is `folder_scans` in the times JSON; `[]` means no scan.
- `gate_record.py`: `ModuleReadRecord` holds `config_reads` (`RecordedConfigReads | NoConfigReadRecord`) and `folder_scans` (`RecordedFolderScans | NoFolderScanRecord`). `module_read_record` is the one reader of both stored fields.
- `buildlog/index.py` schema 38 adds `gate_modules.folder_scans` (sorted JSON; NULL means no record).
- `affected_modules.py`: `latest_module_reads` reads each field from its newest full, non-aborted gate that has it. A listed folder selects its lister only when an added or removed path has that folder as its parent (`changed_path_parents`; a path in a new folder uses its nearest ancestor that exists at the base). A recursive prefix or `/` selects its module for any changed path under it (`scans_changed_paths`). A module with a NULL folder record is selected; with no gate that has folder records the rule selects nothing.
- Measured on the first cloud full gate: 327 of 748 modules scan folders (about 3,100 of 4,584 CPU s), 288 list `config/` through `scripts/lint/memory_test_support.py`, and only `agents/test_no_old_model_names.py` records `/`. Over the last 10 builds merges the selection grew from 1,321 to 1,380 modules (11,344 to 11,683 CPU s); each change under `scripts/`, `docs/` or `commands/` now selects `production/test_staff_accessors.py` and `production/test_roles_audit.py`.

**Files:**
- `scripts/test_times/module_run.py` — the folder audit hook.
- `scripts/test_times/gate_record.py` — `ModuleReadRecord` and its reader.
- `scripts/buildlog/index.py` — the `folder_scans` column.
- `scripts/production/affected_modules.py` — the combined reader and the folder rule.

**Binds later work:** the stored folder strings (`.` root, `/` whole repository, trailing `/` recursive) are the inputs that "A test that reads the files it finds in a folder runs when one of those files changes" decodes into named states.

**Gotchas:**
- Each targeted selection reads the module records (1.2-2.6 CPU s), and the first index update on a host after the schema change rebuilds the index (99 CPU s on natedev).
- Listings in child processes and Git listing commands other than `ls-files` record nothing.
- A test that patches `os.path.realpath` counted the hook's own calls until the hook bound its functions at install time.

**Ruled out:** selecting a lister on a content change in its listed folder, because 288 modules list `config/` and the rule selected all of them for each config edit.

### Phase 131 — A test that reads the files it finds in a folder runs when one of those files changes  · status: done
**Depends on:** Phase 130

#### As-built

- `module_run.py`'s `RepositoryReadAudit` records each read-mode `open` of an existing regular file inside the checkout as a repository-relative path. It skips `config/`, `__pycache__/` and opens made by the import system. The sorted set is `content_reads` in the times JSON; `[]` means no read.
- `gate_record.py`: `ModuleReadRecord` adds `content_reads` (`RecordedContentReads | NoContentReadRecord`). `module_read_record(configs, folders, contents)` decodes the stored folder strings into `ListedDirectory(path)`, `RecursiveDirectory(prefix)` and `WholeRepository()`; `folder_scan_strings` encodes them back to the same JSON.
- `buildlog/index.py` schema 39 adds `gate_modules.content_reads` (sorted JSON; NULL means no record).
- `affected_modules.py`: `latest_module_reads` reads `content_reads` from the newest full, non-aborted gate that has it. A module is selected when a changed non-config path is in its content reads, or when its content record is NULL. With no gate that has content records, the rule selects nothing. `scans_changed_paths` matches on the three folder states.
- `module_cost.py`: `runner_command(tree, module)` builds the `module_run.py` command from the requested tree for the time, trace and `--commands` runs.
- Measured on the first cloud full gate: all 748 modules have a content record, 487 read repository files, and the records add 907 KB of JSON per gate. Over the last 10 builds merges the targeted selection grew from 978 to 1,072 modules (8,368 to 9,214 CPU s). A warm selection costs 0.14 CPU s more.

**Files:**
- `scripts/test_times/module_run.py` — the read audit.
- `scripts/test_times/gate_record.py` — the content record and the three folder states.
- `scripts/buildlog/index.py` — the `content_reads` column.
- `scripts/production/affected_modules.py` — the content rule.
- `scripts/test_times/module_cost.py` — `runner_command`.

**Gotchas:**
- The audit `open` event carries no `dir_fd`, so a read through `os.open(name, dir_fd=fd)` records nothing. No repository test reads repository files that way.
- Reads in child processes record nothing.
- 19 modules read over 500 repository files (the largest about 1,700), so almost every change under `scripts/` selects them.

**Ruled out:** a `dir_fd` fix, because no current reader needs it.

### Phase 132 — A script change runs the scan checks that read it, not the slower tests beside them  · status: done
**Depends on:** Phase 131

#### As-built

- Nine modules keep their repository scan tests in `test_<stem>_sources.py` in the same folder. Each original module now reads 0 to 9 repository files (before: 609 to 1,701), so a script change selects only the scan module.
- A scan module imports its helpers by function name from the original module. Each helper is defined there, not re-exported. Each moved test body and assertion is unchanged.
- `scripts/lib/test_nix_env_integration.py` is deleted. The gate runs the `lib/test_claude_launch_scan.py` tests once.
- Measured: over the last 10 builds merges, the targeted selection went from 1,072 modules (9,210 CPU s) to 1,081 (9,008 CPU s). Seven merges are about 34 CPU s lower each. The full gate went from 4,636 to 4,618 CPU s. The scan modules keep most of the old cost (3 to 19 CPU s each).

**Files:**
- `scripts/copies/test_copy_guard_sources.py` — both repository copy-policy scans (shared cached scan).
- `scripts/astra/test_design_roles_sources.py` — retired design role name scan.
- `scripts/lib/test_process_group_sources.py` — group signal scan of every script.
- `scripts/message/test_session_send_sources.py` — sender guard scan of test modules.
- `scripts/direct/test_one_launch_path_sources.py` — worker launch command scan.
- `scripts/accounts/test_launch_accounts_sources.py` — Python and shell launcher account scans.
- `scripts/lib/test_claude_launch_scan_sources.py` — repository launch guard scan; the scanner stays in `test_claude_launch_scan.py`.
- `scripts/lib/test_py_floor_sources.py` — interpreter-floor parse of every script.
- `scripts/notify/test_push_texts_sources.py` — literal phone send and Pushover host scans.

**Gotchas:**
- A scan module must never import a `TestCase` class or the original module object. The loader then runs those tests twice.

**Ruled out:**
- Splitting `lib/test_one_hang_guard.py`, `lib/test_process_failure.py` and `agents/test_no_old_model_names.py`, because each is almost only its scan.
- Splitting `production/test_staff_accessors.py` and `lib/test_user_zone.py`, because each costs under 1 CPU s.
- Changing the four tree-copy modules (`production/test_update_registration_commands.py`, `build_queue/test_memory_feed.py`, `aws_target/test_control.py`, `mac_test/test_floor_sweep.py`), because their read records are a correct over-approximation of the child's imports.

### Phase 133 — Find where the two slowest source scans spend their CPU  · status: done
**Depends on:** Phase 132

#### As-built

- No code changed. The phase is a stage profile of the two largest source scan modules, `scripts/copies/test_copy_guard_sources.py` and `scripts/lib/test_process_group_sources.py`. Each stage ran as a separate process over 3 alternating paired rounds on natedev, 2026-11-11.
- Median CPU s of each added stage:

| Added work | copy guard sources | process group sources |
| --- | ---: | ---: |
| start and imports | 0.13 | 0.12 |
| list and read | 0.10 | 0.38 |
| parse | 2.55 | 8.43 |
| one walk | 1.27 | 6.03 |
| check (full minus one walk) | 22.49 | 5.62 |
| full module | 26.6 | 21.5 |

- Removing all of the parse and one walk in both modules saves 18.3 CPU s at most. No shared-parse or shared-walk design reaches the 30 CPU s bar, so builds dropped the source-scan speed work.
- The copy guard check is 22.5 CPU s of non-parse work. It parses each file twice (1,012 parse calls for 506 files). This item is in the next-items file for verify-unit.

**Files:**
- `docs/plans/builds-natest-next.md` — the copy guard check item for verify-unit.

**Gotchas:**
- Each scan module runs in its own gate process. A parse that a shared helper caches does not cross modules.
- Name a cost inside a check only from a measured stage difference, never from cProfile totals.

**Ruled out:**
- A shared helper that parses each file and builds a full parent and scope node index: paired runs showed no saving (process group sources 10.6 -> 14.4 CPU s) and about 20 MiB more peak memory in each scan module.
- A one-process run that keeps every tree in memory: 31 to 36 CPU s slower, with a 3.5 GB peak.
- Any shared-parse or shared-walk design across the scan modules: at most 18.3 CPU s.

### Phase 134 — The next slowest test module uses at least 30 CPU s less  · status: done
**Depends on:** Phase 133

#### As-built

- `run_style_command(root, case, command, ...)` in `scripts/style/style_plan_test_support.py` runs ledger, planner and nested `style_footprint.py` commands in process with real parsers, Git, ledger files, stdout, stderr and exit codes. `StylePlanFixture.run_command` delegates to it; `StandingCheckpointTests.cli` uses `partial(run_style_command, self.root, self)`. A case that `process_boundary_for_case` marks keeps its real child process.
- `invoke_checkpoint` (`scripts/production/merge_checkpoint_test_support.py`) takes `scripts/lib/py` as the interpreter, so `progress_history.py` calls reach `invoke_recorder`.
- aws paired trial, 3 rounds: `style/test_standing_checkpoint.py` 51.3 to 11.9 CPU s (15 tests); `production/test_merge_checkpoint.py` 37.8 to 16.5 CPU s (39 tests); 60.7 CPU s saved per gate.

**Files:**
- `scripts/style/style_plan_test_support.py` — `run_style_command`.
- `scripts/style/test_standing_checkpoint.py` — `cli` through `run_style_command`.
- `scripts/production/merge_checkpoint_test_support.py` — `invoke_checkpoint` interpreter rule.

**Ruled out:** progress history parser reuse (6.9 CPU s, 1 pair); style sync Git seed (2.6 CPU s); bulk run Git seed (-1.5 CPU s).

### Phase 135 — One cached command shows the daily trend of gate CPU, wall time and cost  · status: done
**Depends on:** none

#### As-built

- `show.py trend [--days N]` (default `TREND_DAYS`) prints one table for each place and selection (`full`, `affected`). Columns: day, runs, CPU mean, CPU p95, wall mean, wall p95, cost mean, cost p95, cost total. An `affected` table adds median modules run.
- Rows use passing, non-aborted runs with at least one module, grouped by day in the user's zone. Run CPU is the sum of `gate_modules.cpu_s` over all attempts; run wall is `gate_runs.wall_s`. p95 is nearest-rank (`nearest_rank`: index ceil(0.95n)-1). Seconds print as whole numbers; USD has 3 decimals.
- `gate_costs()` joins `gate_runs.id` to `"gate-" + GateReservation.attempt` from `CostLedger.read_snapshot()`. Cost is `rate.usd_per_hour` x the charged lifetime (reservation start to `GateTerminated.at`) + `allowance_usd`. A `not priced` rate or a reservation that is not terminated gives `CostNotRecorded`.
- Place is `aws` for a run that joins a reservation or an EC2 host name; any other host is its own place and shows `—` for cost. Cost statistics use only costed runs; a note line `<N> of <M> runs have no cost record.` follows the table.
- `gate-trend.json` beside the gate index (`TREND_CACHE_VERSION = 2`) holds the rows of done days. A day is done when the completed index refresh started at least `TREND_SETTLE_MINUTES` (default 60) after the day ended. Every read-change-write holds `file_lock.exclusive`; the refresh start keeps its maximum. Writes are atomic; a missing, unreadable or other-version cache is rebuilt.
- A first run reads the whole gate index (about 9 s); a run from the cache takes about 0.4 s.

**Files:**
- `scripts/test_times/show.py` — `trend`, `trend_day`, `gate_costs`, `nearest_rank`, the cache readers and writers, `refresh_trend_index`.
- `scripts/test_times/test_show.py` — trend grouping, p95, place, cost join, missing cost, settle and cache cases.
- `config/test_times.conf` — `TREND_DAYS=8`, `TREND_SETTLE_MINUTES=60`.
- `commands/test_times.md` — the `trend` usage and rules.

**Gotchas:** The cost ledger re-prices every stored reservation with its newest AWS bill at each reserve and resolve, so a cost cached with a day goes stale.

### Phase 136 — Trend costs always match the cost ledger  · status: done
**Depends on:** Phase 135

#### As-built

- The trend cache (`TREND_CACHE_VERSION = 3`) keeps, for each done day and each place and selection group, a `TrendDay` with the cost-free cells (day, runs, CPU mean and p95, wall mean and p95, median modules run) and `run_ids`.
- Each `trend` run reads the ledger once through `gate_costs()` and computes cost mean, p95, total and the missing-cost count over the run ids, for cached and open days alike. The gate index is queried only for open days.
- A cached day keeps the place it was cached with.

**Files:**
- `scripts/test_times/show.py` — `TrendRun.run_id`, `TrendDay.run_ids`, cost cells at print time in `trend`.
- `scripts/test_times/test_show.py` — the settled-day case re-prices the ledger and checks the cached day's new cost and note.
- `commands/test_times.md` — the ledger and cache rules.

### Phase 137 — Find where the slowest test module spends its CPU  · status: done

#### As-built

- Research only; no repository file changed. `direct/test_verify_wait_holder.py` measures 76.0 CPU s, 5,404 processes and 27 tests at `4900e5d38`.
- Recorder starts cost 19.6 CPU s in 88 starts; `build_targets.py` target selection costs 8.9 CPU s in 50 starts. Most CPU is in real Python children, not fixtures.
- One local pair each: deferred recorder and staff imports save 16.4 CPU s; the seeded interpreter cache (`verify_hold_wait_test_support.py`) saves 2.2 CPU s; routing synchronous holder calls through the existing verify helper saves 7.3 CPU s; all three together save 9.0 CPU s. The isolated savings overlap.
- Importing `progress_history` costs about 133 ms; 57 test modules and `verify.sh` start the recorder.

**Binds later work:** The recorder import change ("Recorder commands import only what they use") is sdlc's code; sdlc gave the go after its owner lands on the same files.

**Ruled out:** another class seed or a shorter fixture sleep (the dominant cost is child starts); a preloaded fork server (process ownership).

### Phase 138 — Recorder commands import only what they use  · status: todo
**Depends on:** Phase 134
**Blocked by:** three lands on the same files — sdlc phase 22 (`plan_history.py`, progress_history tests), hierarchies staff-copies section 9 (`session_staff` readers), and the builds verify worker wait fix (`progress_history.py`). sdlc gave the go on 2026-11-11 and names when both reach main; start after builds carries them.

#### Work Order

**Goal:** A recorder start costs less CPU, and the gate modules that start the recorder use less CPU in sum, with the same tests. User, 2026-11-11: any measured saving ships.

**Decision:** Director, 2026-11-11, from the Phase 137 research: deferred imports saved 16.4 CPU s in `direct/test_verify_wait_holder.py` alone. Savings may join across modules.

**Spec:**
1. In `scripts/direct/progress_history.py`, `scripts/direct/plan_history.py` and `scripts/production/session_staff.py`, move each top-level import that only a function uses into that function.
2. Keep each import that a module expression, class base or argument default uses at the top level.
3. Keep every supported module attribute at the top level: explicit exports (`OPEN_PHASES_KEY`, `STATE_FILENAME`), names that tests patch (`progress_history.subprocess`, patched at `scripts/direct/test_progress_history.py:491` and near line 7557), decorator and module-expression names, and `session_staff`'s runtime aliases for staff types, configuration types and `HerdrPlace` (`session_staff.py:113` and near line 668). Import names used only in annotations under `TYPE_CHECKING`.
4. Change no recorder, staff or holder decision. Keep every process boundary.
5. Change no test body or assertion.

**Files:**
- `scripts/direct/progress_history.py`
- `scripts/direct/plan_history.py`
- `scripts/production/session_staff.py`

**Workers:** 1 writer; three small edits in one import chain.
- impl: all three files.

**Constraints from prior phases:**
- Phase 122 rejected a smaller recorder-only import change because real commands still loaded `staff`. That cause remains: `_read_state` calls `staff.initial_snapshot()` (`progress_history.py:485`), and the module constant near line 217 needs `models_registry`. Measure real commands, not an import probe.
- Phase 137 research report: `phases/137/research_report.md` in the unit session. Its 16.4 CPU s is one local pair before Phase 134; Phase 134 already removes recorder starts in `merge_checkpoint_test_support.py`, so that number is not acceptance evidence.
- Phase 134 gives the aws paired trial command for acceptance.

**State:** none.

**Acceptance gate:**
- basedpyright reports zero errors and zero warnings on the three files and their callers.
- The direct and production suites pass.
- An aws paired trial, 3 rounds, over the five modules with the most `progress_history.py` starts gives a summed paired median saving greater than zero, with the same test counts. Both trial revisions contain Phase 134 and the three owner lands.
  1. Find the five modules: `python3 scripts/test_times/module_cost.py --tree . --commands 15 <module> ...` over `direct/test_verify_wait_holder.py` and the direct and production modules that start the recorder. Count `progress_history.py` rows.
  2. Run `python3 -m scripts.aws_target.gate_trial --base <base full sha> --changed <changed full sha> --modules <the five modules> --rounds 3`.

### Phase 139 — Find where the fast-track test module spends its CPU  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. `production/test_fast_track.py` measures 45.7 CPU s, 8,618 processes and 52 tests at `bcda46a2e`. Git commands use 13.9 CPU s in 7,703 starts; repeated setup uses 1.0 CPU s.
- No absolute CPU floor is established. Git and required children dominate. devtools-unit owns the Git read path. Phase 143 asks which of these Git calls the tests need.

**Gotchas:** One local pair on natedev can vary by more than 10 CPU s. Check process and exec counts before trusting a local saving.

**Ruled out:** direct fixture calls (-2.6 CPU s alone); retained immutable Git reads (-4.9 CPU s alone); both together remove only 16 of 8,618 processes.

### Phase 140 — Find where the style sync test module spends its CPU  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. `style/test_style_sync.py` measures 46.3 CPU s, 4,218 processes and 70 tests. Git uses 3.6 CPU s in 3,263 starts.
- Three test-only candidates, one pair each: private sender CLIs in process (11.6 CPU s), the scratch `cargo-save` CLI in process (7.6 CPU s), and a seeded initial Git repository (7.5 CPU s). Together they save 19.5 CPU s. Phase 142 implements them.

**Gotchas:** A harness that patches the unqualified test module counts nothing; the runner loads the qualified module. Check each candidate counter before trusting a pair.

**Kept:** signal, lock, verifier, lint driver and workspace integration processes stay real.

### Phase 141 — Find where the crate run test module spends its CPU  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. `style/test_crate_run.py` measures 61.7 CPU s, 27,662 processes and 97 tests. Git uses about 30.8 CPU s in 26,573 starts; `rev-parse` runs 8,182 times and `fetch` 764 times.
- No candidate gave a stable saving. Phase 143 asks which of these Git calls the tests need.

**Ruled out:** batched checkout metadata (+8.7 CPU s in one pair, queue-unit); batched ledger location (-2.7 CPU s); both together (-3.7 CPU s).

### Phase 142 — The wait holder and style sync test modules use less CPU  · status: done
**Depends on:** none

#### As-built

- `verify_hold_wait_test_support.py`: the fixture environment no longer sets `CLAUDE_PYTHON` to `sys.executable`, so the real launcher uses the seeded interpreter cache. The two observer cases keep their explicit overrides.
- `test_style_sync.py`: the private `address.py`, `send.py`, `roster.py` and scratch `cargo-save` run in process through `invoke_cli`; supported style commands go through `run_style_command`; a class seed builds the initial Git repository once and each case copies its `.git`. Signal, lock, verifier, lint driver and workspace processes stay real.
- aws pairs, 3 rounds: style sync 42.35 → 26.40 CPU s; wait holder 65.16 → 62.60 CPU s. Same tests pass.

**Gotchas:** two aws trials failed for Spot capacity in us-east-2; the us-west-2 fallback (`4fde8afa6`) made the third run.

### Phase 143 — Find which Git calls the slowest test modules do not need  · status: done
**Depends on:** none

#### As-built

- User, 2026-11-11: "why do we need all of those git calls in our tests - maybe they're not necessary". Research only; no repository file changed.
- Git starts by module: crate_run 26,573 (20.7 CPU s), bulk_run 13,019 (10.0), fast_track 7,703 (16.7), merge_checkpoint 4,789 (5.5), standing_checkpoint 3,219 (2.7). Most calls test real Git behavior: commits, refs, index state, ancestry, recovery and publication. Those keep real Git.
- Two candidates remain: a shared `CargoProjectTests` repository seed (550 calls, natest-unit, reaches `test_bulk_rename.py`, `test_bulk_run.py`, `test_crate_run.py`) and batched manifest reads in `coverage_report.workspace` (377 calls, style owner, 42 modules). Isolated local pairs save 27.9 and 7.1 CPU s; together only 1.3 CPU s. Host noise is larger than the saving. Both wait in the next-items review; an aws pair decides.
- Report: `phases/143/research_report.md` in the unit session.

**Gotchas:** isolated local savings do not add. Measure candidates together, on aws, before naming a gain.

### Phase 144 — /test_times trend says targeted tests and merge gates, and every table right-aligns its numbers  · status: done
**Depends on:** none

#### As-built

- User, 2026-11-11, through builds: `/test_times trend` names its tables `<place> merge gates` (selection `full`) and `<place> targeted tests` (selection `affected`), merge gates first. The database keeps `full` and `affected`.
- `scripts/lib/terminal_cells.py` `is_number_cell` decides number cells (sign, `$`, `%` or `s` suffix, or `—`). `print_table` right-aligns each column whose cells are all number cells, header included; only test_times tools call it.

### Phase 145 — The dailies tests are gone and the stall-watch tests keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- User, 2026-11-11: the dailies retire; `/showrunner:units` replaces them. The accounts unit retires the dailies code, config, timers, commands and docs.
- Deleted `test_dailies_counts.py`, `test_dailies_input.py`, `test_dailies_render.py`, `test_dailies_render_agents.py`, `test_dailies_render_holds.py`, `test_unit_status_dailies.py`, `test_review_pause.py` and `dailies_input_test_support.py`. Removed dailies-only cases from eight other test files.
- Seven tests cover code that live tools still use, so they moved to live modules:
  - `parse_outstanding_entries` and `due` (used by `showrunner_footer.open_questions`): `scripts/hooks/test_showrunner_footer.py`.
  - `update_registration.read_open_items`: `test_update_registration.py` and `test_update_registration_answers.py`.
  - `unit_status.sh` → `live_units.py` session lookup (used by `/showrunner:units`): `test_unit_status_decisions.py`.
  - `waiting.eta_moment` (used by `waiting.phase_eta`): `test_waiting.py`, with a literal log line.
- `test_stall_watch.py` lost its rare-case tests under Rule 2. It keeps the damaged-history and scan-failure recovery tests.
- `unit_status_test_support.py` lost four helpers that had no user.
- Held (sdlc): `test_change_review.py`, `test_worker.py` and `test_production_lifecycle.py` wait for owner branches to reach main.

**Gotchas:** a retired feature's tests can cover code that a live tool also calls. Before a delete, list every caller of each function the tests reach.

### Phase 146 — Four style test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 30 rare-case tests: `test_style_ledger_tickets.py` 196 → 176, `test_style_proposals.py` 187 → 182, `test_style_ledger_sites.py` 169 → 167, `test_bulk_rename.py` 118 → 115.
- Removals were documentation wording, refusal order, lock timing, call counts, failure context and exception types. None of them changes saved state.
- `ledger_test_support.py` lost `record_while_stalled`, which had no user after the removals.
- Held (sdlc): `test_style_footprint.py` and `test_style_ledger_findings.py` wait for owner branches to reach main. `test_style_sync.py` stays with Phase 142.

**Gotchas:** an argument refusal is not always a rare case. Both reviews restored two `--by` refusals in `test_style_proposals.py`: without them, a blank name reaches the ledger write and erases who acted. Check where a refused value lands before removing its test.

### Phase 147 — Four direct test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- User, 2026-11-11: the Rule 2 sweep has no CPU or money bar; fewer tests is the goal. Rule 2 keeps a rare-case test only when its defect would show the user a wrong result, damage saved state or lose work.
- Removed 14 rare-case tests: `test_findings.py` 82 → 76, `test_assign_launcher.py` 65 → 60, `test_phase_reservation.py` 59 → 56. `test_phase_process.py` keeps all 73: each test guards a main path or saved state.
- Removals were parser refusals, configuration errors that already fail visibly, cleanup of unused panes or backends, and diagnostics that change no state.
- `findings_test_support.py` lost `write_partial_config`, which had no user after the removals.
- Held (sdlc): `test_progress_history.py` and `test_phase_table.py` wait for owner branches to reach main.

**Gotchas:** a removal can leave a helper in a shared `*_test_support.py` file with no user. List each module's support files under Files in a sweep phase.

### Phase 148 — Three lint test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 25 rare-case tests: `test_memory_admit.py` 375 → 367, `test_sweep.py` 363 → 348, `test_tmp_sweep.py` 99 → 97.
- Removals checked internal details, repeated reads, import cost and diagnostics.
- Kept by review: the sweep refusals for an empty `--target-dir` and a bad floor value, and the memory refusal for a missing size basis. The resources rule keeps the `test_sweep.py` rows for nested, held, symlinked and overlapping Mac targets.

**Gotchas:** in a tool that deletes files or admits work, a refusal test guards saved state. A hand-written parser needs its refusal tests; `argparse` does not cover it.

### Phase 149 — Four buildlog test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 4 rare-case tests: `test_index.py` 88 → 87, `test_record.py` 86 → 84, `test_mac_idle.py` 69 → 68. `test_report.py` keeps all 171.
- Kept by review: the unknown Mac outcome test (a broader classifier would count it as completed and give a false usable-Mac share) and the bad idle configuration warning test.
- Removed by review: a timestamp refusal test. Both callers validate timestamps first, so no caller can make that state.

**Gotchas:** reviewers can disagree on a kept test. Check the callers: a guard no caller can reach goes; a guard on a value a report counts stays.

### Phase 150 — Three production test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 7 rare-case tests: `test_add_showrunner.py` 60 → 56, `test_fast_track.py` 52 → 50, `test_roles_audit.py` 52 → 51.
- Fixed by review: `advance()` in `add_showrunner_test_support.py` no longer writes `polls.jsonl`; the removed interval test was its only reader.

**Gotchas:** a removed test can leave fixture writes with no reader. Check each fixture output the removed test read.

### Phase 151 — Four more style test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 5 tests: `test_style_ledger.py` 100 → 98, `test_crate_run.py` 97 → 96, `test_approved_renames.py` 89 → 87. `test_bulk_run.py` keeps all 108. The removed tests checked internal call counts, query-plan shape or an import cycle.
- Kept guard for the removed nested-`if` refusal test: `test_bulk_rename.py` `test_unread_enclosing_scopes_refuse_before_analyzer_rename` (no edit) and `test_style_ledger_findings.py` `test_unparsed_enclosing_condition_cannot_be_marked_built` (no built mark), with the same source.

**Gotchas:** a removal table must cite a guard that uses the same input. A wrong citation made one reviewer ask to restore a covered test.

### Phase 152 — Two more production test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Rule 2 sweep, as in Phase 147. Removed 1 test: `test_style_units.py` 46 → 45 (it only proved Python requires `ledger_paths`). `test_broadcast.py` keeps all 49: each guards a wrong delivery, a lost message or a main path.

**Gotchas:** production modules near 50 tests now yield one to four tests each.

### Phase 153 — The bulk rename tests copy one seeded project instead of building it for each test  · status: done
**Depends on:** none

#### As-built

- Checkpoint `cc41ea577`. `CargoProjectTests` (`scripts/style/test_bulk_rename.py`) builds one committed project seed per class; each test copies it with `shutil.copytree(..., symlinks=True)`. `CrateRunTests` keeps its complete class seed and builds no base seed. 157 tests keep their own commits or workspace setup.
- About 1,700 fewer Git starts (bulk rename 817 to 250). aws pairs, 3 rounds: bulk rename 14.48 to 13.80 CPU s, bulk run 46.01 to 45.25, crate run unchanged at 59.45. All 319 tests stay.

**Gotchas:** a worker report in a scratch folder is gone at review time; a report the review needs goes in the phase folder.

### Phase 154 — Find what the Mac-only verify tests spend their CPU on  · status: done
**Depends on:** none

#### As-built

- Research only; report in the unit session at `phases/154/research_report.md`. `direct/test_verify_mac_only.py`: 33.7 CPU s locally, 4,691 processes. Real `build_targets.py` (15.2 CPU s) and `verify_helper.py` (5.0 CPU s) runs lead; fixture setup costs 0.13 CPU s.
- Candidates: two result cases can use the existing `mac_result` helper (256 fewer process starts, this module only); `assert_nothing_outlives` in `verify_mac_offload_test_support.py` can skip one `/proc` scan on its clean path (68 modules use the fixture). Shell version probes cost more; rejected.

**Gotchas:** local pairs on the shared host varied from 33.7 to 52.8 CPU s for the same tree. Only aws pairs decide a saving.

### Phase 155 — The worker, style footprint and ledger findings test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `4903c6be2`. `production/test_worker.py` 121 to 115 tests, `style/test_style_footprint.py` 272 to 268, `style/test_style_ledger_findings.py` 143 to 141.
- Returned by review: `test_list_all_reads_each_record_once_without_live_calls`. It is the only guard of the documented `list all --json --all-parents` no-live-reads path (`commands/unit/worker.md:25`).

### Phase 156 — The progress history test module keeps only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `0741ab882`. `direct/test_progress_history.py` 284 to 279 tests. Removed: a header line check, phase table timeout wording, two cases that start no phase table process, and private helper names.

**Gotchas:** a merge of origin/builds landed between the work and the review, so the worker's checks covered an older tree. Run the checks on the final merged tree and record its revision.

### Phase 157 — The Mac-only verify tests start fewer processes and the offload fixture scans `/proc` once on a clean exit  · status: done
**Depends on:** none

#### As-built

- Checkpoint `79f2f1727`. Two tests in `direct/test_verify_mac_only.py` use the `mac_result` helper, so they start fewer verify processes. `assert_nothing_outlives` (`verify_mac_offload_test_support.py`) calls `reap()` only when the `/proc` scan finds a marked process; a process that exits before the reap still fails the test (`exited before reap`). One new leak test proves a marked survivor fails. aws pairs: 38.31 to 36.18 CPU s median (6%), with one more test. 810 importer tests pass.
- Minor F002 (no row for the scan/reap race) is closed: verify-unit ruled that the race fails the test, so it hides no leak.

**Gotchas:** a change that skips work on the clean path needs a row for the race between the check and the skipped work. Pass the full base sha to `gate_trial`. `direct/test_verify_mac_fixture.py` fails `test_hold_fixture_reaps_its_recorders_without_waiting_for_an_existing_recorder` on aws in the base tree too; verify owns it.

### Phase 158 — The standing checkpoint tests build their sample repository once for each class  · status: done
**Depends on:** none

#### As-built

- Checkpoint `f5024b9f3`. `style/test_standing_checkpoint.py` builds its catalog, Git repository, two worktrees and ledger tip once in `setUpClass`. Each of the 15 tests restores a private copy at the same absolute paths, so the worktree links stay valid. Git starts 3,244 to 2,907. aws pairs: 11.85 to 10.37 CPU s median (12%).

**Gotchas:** copied Git indexes carry old stat data. Run `git update-index --refresh` in each checkout after the restore. Local CPU showed 38% less; aws showed 12%.

### Phase 159 — Eight merge checkpoint test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `0d79ca025`. Eight merge checkpoint test modules go from 127 to 124 tests. Removed: the cancel-prior refusal on the Git push path, the style_sync exit 1 notice case (`test_merge_checkpoint_promotion.py` keeps the same check) and the ledger error after a child handoff. The unused `add_handoff=False` mode of `prepare_handoff_ledger` goes too.
- `test_merge_checkpoint.py`, `test_merge_checkpoint_batch.py` and `test_merge_checkpoint_history.py` stay held by sdlc until fix-rounds batch-held-items lands.

**Gotchas:** a notice runs before the completion writers (`merge_checkpoint.py` `run`), so a test of notice error handling guards saved records. Review returned the notice ValueError test. Check where a tested step sits before state writes, not only what the test asserts.

### Phase 160 — The waiting tests build their Git trees once for each class  · status: done
**Depends on:** none

#### As-built

- Checkpoint `61579526f`. `production/test_waiting.py` builds the bare origin, production checkout and two worktrees once in `setUpClass`. Each test restores them at the same paths and refreshes the three indexes. Live hosts, sockets, staff records, board files, unit files and logs stay per test. aws pairs: 28.91 to 27.76 CPU s (1.15 s, 4%); 34 tests pass.
- The two parallel ETA tests (about 14.4 s each) are now the main cost. Next item 6b takes them.

**Gotchas:** a class seed must copy every environment isolation that the old per-test setup made. Review found the seed builder kept the user's `XDG_CONFIG_HOME`; the seed now sets it to `root/config`, the folder `isolated_staff_file` uses.

### Phase 161 — Three style test modules build their sample repository once for each class  · status: done
**Depends on:** none

#### As-built

- Checkpoint `36044a56d`. `style/test_coverage_report.py`, `style/test_mechanical_classes.py` and `style/test_order_waste.py` each build one sample Git repository in a class `tempfile.TemporaryDirectory`. Each test copies it. aws pairs: 13.76 to 13.58, 3.11 to 2.97 and 2.16 to 2.08 CPU s; 20, 22 and 14 tests pass. Every saving ships (style-unit: any measured saving ships).
- Git setup is no longer the main cost in these modules. Next item 6b names the costs in the test bodies.

### Phase 162 — The last three merge checkpoint test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `f8b27fda5`. Rule 2 removes `test_example_without_a_manifest_has_no_step` from `test_merge_checkpoint.py`: a missing crate manifest gives a visible failure before publication. The three modules go from 53 to 52 tests. The history and batch modules keep all their tests; each guards saved completion records or the checkpoints that lifecycle and CI decisions use.
- The merge checkpoint family review is complete (Phases 159 and 162).

### Phase 163 — The parallel ETA tests start fewer Python interpreters  · status: done
**Depends on:** none

#### As-built

- Checkpoint `a9b62f887`. The two parallel ETA tests in `production/test_waiting.py` start 12 forked children behind a barrier (`eta_request_processes`, line 653) in place of 12 fresh `waiting.py` interpreters. Each child runs the real handler with its own output capture and leads its own process group; cleanup kills each group. aws pairs: 27.59 to 14.61 CPU s (12.98 s, 47%); 34 tests pass.
- Profile of one fresh child: imports 0.49 CPU s, live unit lookup 0.15 s, state work under 0.001 s.

**Gotchas:** `process.kill()` on a forked child leaves its Git children alive; give each child its own process group and kill the group through `send_process_group` (`scripts/lib/process_group.py`). `scripts/lib/test_process_group_sources.py` refuses a direct `os.killpg`; fix `04a1f121b` after chain372 went red.

### Phase 164 — Measure the shared style Git work in three style modules  · status: done
**Depends on:** none

#### As-built

- Checkpoint `4f1e3e7f2`. A read-only measurement at `fe38cb50e`: style_sync 70 tests 21.9 CPU s, bulk_run 108 tests 38.7, crate_run 96 tests 40.8. Three changes can remove at most 2.97 CPU s across the three modules. style-unit takes the `ledger_location` single query and a narrow immutable-read change (no cache across ledger handles).
- Report: `phases/164/style_git_report.md` in the unit session. Next item 6b holds the costs and owners.

### Phase 165 — The style-plan recovery tests keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `7d9c833bb`. No test removed from `test_style_plan_recovery.py` (26) or `test_style_plan_checkpoint_refill.py` (16). Review returned `test_edited_files_of_a_read_ticket_reads_no_synced_tip`: its full file-list assertion stops a refill that assigns a two-file ticket while its second file is busy.

**Gotchas:** judge each assertion, not the test name; a full returned value can guard harm.

### Phase 166 — The bulk run tests build their workspace once for each class and the admission window test costs less  · status: done
**Depends on:** none

#### As-built

- Checkpoint `ce820d5f5`. `WorkspaceRunTests` in `style/test_bulk_run.py` builds its two-crate workspace commit once in `setUpClass` on the class seed; each test copies the seed and runs `git update-index --refresh`. The admission window test copies the prepared root once and restores it before each of its four cases. All 108 tests and 600 assertions stay. aws pairs: 45.12 to 43.85 CPU s (1.27 s).

**Gotchas:** the local measure (44.3 to 30.6 CPU s) overstated the saving tenfold; only aws pairs decide. A spot instance termination can end an aws trial after one round; run it again.

### Phase 167 — The style-plan selection tests keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `8d1005406`. Rule 2 on `test_style_plan_selection.py`, `test_style_plan_batches.py` and `test_style_plan_structure.py`: 73 to 72 tests. Removed `test_modules_reads_blocking_structure_before_scanning_decision_footprints` (read order only); `test_modules_reads_blocking_structure_before_approved_decisions` keeps the saved-ticket check. Both reviews approved the 32 kept rare cases. The family is at its floor.

### Phase 168 — Measure the forked-process candidates for repeated Python starts  · status: done
**Depends on:** none

#### As-built

- Checkpoint `fe4c134c2`. Read-only. Forked children can remove at most 4.68 CPU s across the six fork candidates, all test-only; the two `test_change_review.py` candidates (2.50 s) are under the sdlc hold. Entry imports: ci_points 0.50 s, change_review 0.22 s, detached_runs 0.14 s, agents_log 0.02 s. Lock waits cost almost no CPU. Results are in next item 6b.

### Phase 169 — Measure removable crate-copy preparation in the crate run tests  · status: done
**Depends on:** none

#### As-built

- Checkpoint `b73561f05`. Read-only. `test_crate_run.py`: 96 tests, 55.3 CPU s; 13,638 Git starts inside `copies.py` cost 26.3 child CPU s. A larger project seed removes no checkout work: `copies.fresh_tree()` makes a checkout for each standing request, and `freeze()` makes none. Test-only step: one default frozen-state seed for 27 tests (about 1.3 CPU s); the report named 28, but one test changes its base before it freezes. copies-unit proposals: one fetch for refs and the exact base (2.2 s ceiling), one metadata query (0.8 s ceiling). Results are in next item 6b.

### Phase 170 — The four unheld lock-contention tests use forked children  · status: done
**Depends on:** none

#### As-built

- Checkpoint `195ea9cda`. Four lock-contention tests start their contenders as forked children: the simultaneous watch alerts test (`test_ci_points.py`), the parallel admission starts test (`test_detached_runs.py`), and the concurrent registration and JSON append tests (`test_agents_log.py`). `scripts/lib/fork_test_support.py` (`ForkChildren`: `pipe`, `start`; `ForkChild`: `pid`, `is_alive`, `result`) owns start, pipes, output capture and cleanup; cleanup signals the child directly when its process group does not exist yet. Its tests use pipes, not multiprocessing events: a first check run hung 2h10m in an event `set()` after a killed waiter. aws pairs: 2.06 CPU s saved per gate (1.00, 0.65, 0.40); the helper tests cost 0.13 CPU s locally.

### Phase 171 — The crate run tests share one default frozen state  · status: done
**Depends on:** none

#### As-built

- Checkpoint `e870b2059`. `test_crate_run.py` builds one default frozen state for each class in a `tempfile.TemporaryDirectory`; 27 prerequisite tests copy it and rebuild ownership and locks. `test_moved_destination_clean_overlap_is_merged_and_reproved` keeps the real freeze: it changes the base first. All 96 tests and 587 assertions stay. aws CPU: 59.32 to 58.44 s (0.89 s). The three judgment continuations (`test_crate_run.py:970`) stay unseeded.

### Phase 172 — The Pushover notice tests keep only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- Checkpoint `fc3900f9b`. Rule 2 on `notify/test_pushover.py`: 86 to 86 tests. Each rare case guards delivery, saved send records, status or private data. The family is at its floor.

### Phase 173 — The test-time trend shows the booked cost of each aws gate  · status: done
**Depends on:** none

#### As-built

- Checkpoint `8c7e63ab5`. `show.py` `gate_costs()` adds `ledger.gate_allowance(row).usd` to the box-hour cost. `read_snapshot()` already decodes `booked_allowance`, so no `priced_snapshot` call is needed. `TREND_CACHE_VERSION` is 4, so each cached day is priced again once. The trend cost tests carry booked allowances that differ from `allowance_usd`.

### Phase 174 — Rank the test work by gate seconds saved per work hour  · status: done
**Depends on:** none

#### As-built

- Checkpoint `f1a98808e`. Read-only ranking; report at `phases/174/gate_value_ranking.md` in the unit session. Gate wall time follows CPU over 32 slots (r 0.994); the fixed cost per module is about 133 of 4,664 CPU s, so test bodies hold 97%. Scheduling is already longest-first. Gate records then had no module start times (report phase 50 adds them). Ranking by estimated gate seconds per work hour: admission fixture imports (36 CPU s, 6 h), coverage CLI in process (7.9 s, 2 h; Phase 178), unused ledger imports (1.87 s, 0.5 h; Phase 178).

### Phase 175 — Check whether affected-only merge gates would have caught the recorded failures  · status: done
**Depends on:** none

#### As-built

- Checkpoint `8f30bce69`. Read-only replay of the affected-test selection on 62 failed cloud merge gates (42 trees) since 11-08. Of 35 trees with a commit, the selection includes every failing module on 25, some on 4 and none on 6; 7 trees have no commit object. Most misses are whole-repository audits and fixtures that read a config file (Phase 176).

### Phase 176 — A targeted test run selects the whole-repository checks and the tests whose fixtures read a changed config  · status: done
**Depends on:** Phase 175

#### As-built

- Checkpoint `c6a39ab84`. `affected_modules.py` adds rooted source scans, fixture config reads and named paths to targeted-run selection. Scan evidence stays inside the called function and its root, and keeps the scan folder and recursion mode. An unresolved root or pattern selects at any depth under the repository root. An absolute literal under the repository root is a repository path. 55 replays keep every old selection. Of 12 replayable historical misses, 8 are caught and 4 remain; 3 more sit on trees whose commits are gone and stay unverified; builds accepted each (general import closure ruled out; `test_scratch_guard` and `test_quota_alert_delivery` not caused by the change; `test_merge_checkpoint_batch` closes in Phase 189). Added modules per targeted run: median 290, largest 478 (source scan 241). Four repair rounds; the last two fixed regressions only. Full merge gates unchanged.

### Phase 177 — Find the shared costs inside test bodies across the whole gate  · status: done
**Depends on:** none

#### As-built

- Checkpoint `3360d8b15`. Read-only census of the commands that test bodies start, in 137 high-cost and 30 random small modules (60.78% of median gate CPU); report at `phases/177/body_command_census.md`. Measured leaders: module_run task family 598.5 CPU s, `python3 -c` 166.8 (2,081 starts), `progress_history.py` 98.9 (390), `build_targets.py` 90.9 (541), `admission_client.py` 79.9 (467), `git rev-parse` 56.8 (56,622 calls). Ranked gross ceilings per work hour: recorder reuse 19.17 (Phase 182), target metadata reads 18.69, Git metadata 15.58, model resolution 13.86, admission client reuse 10.95. The census replaced `task_cpu.task_ids` in memory after a task name with byte `0xff` broke UTF-8 decoding.

### Phase 178 — The coverage report tests run the report in process, and the ledger fixture drops two unused imports  · status: done
**Depends on:** none

#### As-built

- Checkpoint `35eaebdf1`. `test_coverage_report.py` `report()` calls `coverage_report.main` in process through `invoke_cli`; one test keeps the real CLI through `lib/py`. `ledger_test_support.py` imports `style_plan` and `declarations` inside the two fixture methods that use them. aws pairs: `test_coverage_report.py` 13.56 to 3.10 CPU s; four ledger importers saved 0.23–0.35 s, `test_crate_run.py` -0.03 (noise).

### Phase 179 — Answer three questions on slow verify tests, coverage overlap and a fast-only gate  · status: done
**Depends on:** none

#### As-built

- Checkpoint `b69fac85d`. Read-only answers to item 80 A, B and C; report at `phases/179/gate_size_questions.md`. A: 338 of 414 slow verify tests start the full `verify.sh` (496 launches); at least 32 tests need the real process path. B: no full-folder `coverage_overlap.py` run exists; `scripts/direct` would cost about 2,545 CPU s and 52 serial minutes (estimate). C: a gate with only tests below 0.5 s keeps 604 of 757 modules, about 1,408 CPU s and 60 s wall (estimate; measured full gate 4,827 CPU s, 204 s). dwight: 5 of 43 failed trees (about 12%, a low estimate) fail only on slow tests.

### Phase 180 — Collect line coverage overlap across scripts/direct  · status: done
**Depends on:** none

#### As-built

- Checkpoint `68da1fa10` (plan only). One read-only `coverage_overlap.py` run over 207 `scripts/direct` modules: 1,558 tests, all pass; collector 1,711 CPU s, 43.5 min wall. 0 known, 0 candidates: every test has `child outlived its test`, from a collector defect (a launch with no seen exit counts as alive for the rest of the run). 618 tests have only that cause. Phase 184 repairs the collector and repeats the run.

### Phase 181 — The wait-holder tests call the real wait functions, and keep one full verify.sh run per wait kind  · status: done
**Depends on:** none

#### As-built

- Checkpoint `68da1fa10`. In `test_verify_wait_holder.py` three tests call the real hold and route functions (`start_hold_step`, `start_pinned_route`) in place of a full `verify.sh` entry. 27 tests; 126 assertions kept and 7 added. Fixtures unchanged. The verify keeps stay: a full entry for each wait kind, holder changes, exact NotRecorded lines, both owner paths, the memory-close retry. aws pairs: 63.99 to 62.61 CPU s (1.37 s). Local separate runs claimed 25 s: noise. The full-entry keeps hold the rest of the cost.

### Phase 182 — Review launches start the recorder fewer times, with the same pass records  · status: done
**Depends on:** none

#### As-built

- Dropped; nothing landed. A trial kept one `progress_history.py` server per review launch (bash coproc) with light relay clients. aws pairs: the three review-launcher modules saved 1.57-1.79 CPU s each; `test_progress_history.py` +0.54, `test_assign_claude_follow.py` +0.11; net about 4.45 CPU s per gate. Peak memory rose up to 111 MiB. Blind reviews found two blockers: SIGPIPE in cleanup replaced the exit status, and `coproc` needs Bash 4 (the repository keeps Bash 3.2). Simple ranks above Fast. sdlc accepted the drop and gave a go for a lighter recorder import (Phase 185).

### Phase 183 — The Mac-only tests call the real verify.sh functions where the decision is in one function, and keep the full entries verify named  · status: done
**Depends on:** none

#### As-built

- Checkpoint `8fe4448ec`. In `test_verify_mac_only.py` three tests call the real policy, eligibility and result functions in place of a full `verify.sh` entry; full entries 23 to 20. 18 tests and 115 assertions kept. Fixture unchanged. The verify keeps stay at their base boundary (signals, the Mac-only guarantee, environment wiring, the cached-pass refusal, the leak check, exact text, runner status 42). aws pairs saved 0.71 CPU s. Round 0 added 14 full entries (local CPU 33.8 to 50.9) because "keep" did not name the base boundary; Spec 3 now names it. The full entries verify named hold the rest of the cost; the next gain is a shared fixture change (next item 0).

### Phase 184 — The coverage collector stops one child with no seen exit from marking every later test unknown  · status: done
**Depends on:** Phase 180

#### As-built

- Checkpoint `0a6f179eb`. Cause: the collector gave a launch with no seen exit an end of infinity, so it counted as alive at every later test start. `line_collector.py` now wraps spawn and `waitpid` to see child exits, with a monotonic clock; a launch with no exit evidence still counts as alive (the reviewer blocker F001 kept that rule). New rows in `test_coverage_overlap.py` fail on the old code. Repeat run at `0a6f179eb` over 207 `scripts/direct` modules: 1,558 tests, all pass; 617 known (was 0), 941 unknown, 335 candidates; collector 1,665 CPU s, 45.8 min wall. Top candidate modules: `test_progress_history` 124, `test_phase_table` 49, `test_findings` 41, `test_phase_process` 40, `test_phase_reservation` 35. Largest unknown causes: unverified Bash launches 729, shared thread 602, child outlived its test 589 (most from `test_tree_watch` Git and the `test_verify_hold_*` Python children). Candidates are not removal approvals.

### Phase 185 — The recorder commands that review launches call import less  · status: done
**Depends on:** none

#### As-built

- Checkpoint `cbe16dd36`. `progress_history.py` imports `phase_reservation`, `statistics`, `agents_log` and `tag_inventory` inside the functions that use them; function and class logic unchanged; no new module. aws pairs saved 0.43–1.00 CPU s in six caller modules (review launcher, trees, style, `test_assign_claude_follow.py`, `test_verify_wait_holder.py`), about 2.7 s in all; `test_progress_history.py` 0.03 s and `test_verify_mac_only.py` −0.19 s. Local separate runs showed no gain: only pairs decide. Replaces the dropped Phase 182 server design.

### Phase 186 — The verify test fixtures answer fixed Cargo calls from a shell script, not a Python start  · status: done
**Depends on:** none

#### As-built

- Checkpoint `a8054dadf`. `install_fixed_cargo` in `verify_mac_offload_test_support.py` writes a POSIX shell `cargo` that answers the fixed replies (`--version`, metadata, `nextest run` status) and appends the same `TEST_CARGO_EVENTS` line as the Python stub; it exits 1 when the append fails. Other calls (`TEST_CARGO_HOLD`, signals, PID files, observers) still go to Python. `verify_hold_wait_test_support.py` and `verify_ssh_targets_test_support.py` use it; `verify_token_wait_test_support.py` is unchanged. `test_fixed_cargo_replies_match_python_events` in `test_verify_mac_only.py` compares both stubs byte for byte, including a `/dev/full` row. 145 importers, 2,355 tests pass. Three aws pairs saved about 1.2 CPU s per run: `ssh_targets_routing` 0.63, `wait_holder` 0.39, `token_renew` 0.22, `hold_events` 0.06, `mac_only` −0.01 (one new test), `result_environment` −0.06. The local 25 s figure was load noise: Python stub starts are not the main cost of these modules.

### Phase 187 — The task CPU measure reads a task whose name holds bytes that are not UTF-8  · status: done
**Depends on:** none

#### As-built

- Checkpoint `58aaa15cc`. `task_ids` in `task_cpu.py` reads `/proc/<pid>/status` as bytes and parses only `Tgid:` and `PPid:`. Two rows in `test_module_cost.py`: a `Name:` with byte `0xff` (fails on the old code) and an ordinary status. Nothing else changed.

### Phase 188 — The coverage collector verifies the Python descendants of a Bash launch  · status: done
**Depends on:** Phase 184

#### As-built

- Dropped; nothing landed. A trial traced each Bash launch with Linux `strace` to prove every descendant (+364 lines, an `strace` dependency). Sample of the 10 modules that hold the 59 tests with only `unverified launch bash`: 2 recovered. With real system binaries (`date`, `mkdir`, `dirname`, `cat`, `rm`, `mv` and others) added as leaves, the projection is 46 of 59, and the folder goes from 617 to 663 known of 1,558. The other 13 stay unknown (the `py` wrapper, fixture executables). The folder records cannot project more. Simple ranks above a 3% coverage gain, and no removal work is approved to use it. Patch: `phases/188/bash_descendant_evidence.patch` in the session folder.

### Phase 189 — A targeted test run uses the declared scan folders of the tests that scan the tree  · status: done
**Depends on:** Phase 176

#### As-built

- Checkpoint `60e5efdf6`. `config/merge_gate.conf` `scan_roots` declares, per tree-scanning test module, the folders and files it reads; `affected_modules.py` selects a declared module only when a change falls under one of them, in place of the whole-tree source scan. An invalid or unknown declaration fails loudly to the full suite; a declared module absent from the tree gives `FullSuite` with a graph error. The declarations for `agents/test_models_conf_publish.py` and `lint/test_style_review.py` were dropped (reads unverified). Two repair rounds: loud failure on a malformed word, five data-path declarations added; the test rows sit in the existing scan test. Evidence over 55 replay rows, bound to the final hashes: 769 selections removed in 21 modules; added execution CPU median 2572.249 to 2519.426 s (largest 3503.011 to 3440.183); selector CPU median 9.77 to 9.96 s. Every row keeps the Phase 175 selections and the 13 recovered Phase 176 misses; 36 unverified pairs stay selected.

### Phase 190 — Measure the repeated target-command work in three verify test modules  · status: done
**Depends on:** none

#### As-built

- Research; no commit. Target commands cost 28.462 CPU s in `test_verify_wait_holder.py` (7.420), `test_verify_mac_only.py` (11.064) and `test_verify_ssh_targets_routing.py` (9.978), all required eligibility, probe, reserve and release work. Repeats that read unchanged data: `target_platform` (`verify.sh:2628`), `target_for_settings` (`verify.sh:2639`), and the reserve command that `route_test` builds twice per attempt (`verify.sh:2848,2865`). Estimate, not measured: 0–0.22 CPU s for 446 excess reads. Owner proposal sent to verify-unit via builds: build `ssh_reserve_hook` once per `route_test` attempt; natest recommends no action at this gain. Report: `phases/190/target_command_report.md` in the session folder. Architect after Phase 190: the tested verify harness changes are at a practical floor; the next verify step is a startup-reuse owner trial (next items).

### Phase 191 — Profile the three slowest test modules outside verify and name their removable work  · status: done
**Depends on:** none

#### As-built

- Research; no commit. Local profiles (161 tests pass): `test_fast_track.py` 46.1 CPU s (Git 21.3 s in 7,596 starts, `fast_track.py` CLI 11.0 s), `test_crate_run.py` 55.5 s (26,619 processes), `test_standing_checkpoint.py` 6.9 s (the 39.9 s rolling median is not a paired baseline). All three already use class fixtures; most Git work supplies race and recovery proofs. Bounded estimates, not measured: zone handler called directly in function cases (`test_fast_track.py:316`, natest) 0–2.06 s; skip frozen checkout preparation for terminal resumes (`style/crate_run.py:979`, style-unit with copies-unit review) 0–2.86 s; single-file frozen seed (`test_crate_run.py:135`) 0–1.09 s; command adapter for direct publication helpers (`test_standing_checkpoint.py:237`) 0–0.78 s; extended frozen seed (`test_crate_run.py:102`) 0–0.57 s; fixed seed Git metadata (`test_fast_track.py:275,298`) 0–0.26 s. First aws trial: the zone handler change, after the test owner's go. Report: `phases/191/module_profile_report.md` in the session folder.

### Phase 192 — The fast-track tests run the zone drain through its real handler in process  · status: done
**Depends on:** none

#### As-built

- Checkpoint `46b4a770b`. In synchronous function cases, `FastTrackTests.command` runs the known `zone.py drain` through the real `zone.main` with `invoke_cli` and restores the environment, `user_zone.CONF`, cwd, stdout, stderr and exit status on success, nonzero exit and exception. The class seed keeps its common Git directory and first main commit. The full CLI case and every interruption, concurrent-process and deadline case stay real processes. 50 tests and 308 assertions kept. aws pairs: 53.77 to 51.19 CPU s (2.63 s per run); `lib/test_zone.py` unchanged. Local unpaired runs showed no gain.

### Phase 193 — The crate-run and standing-checkpoint tests reuse class seeds and the command adapter  · status: done
**Depends on:** none

#### As-built

- Checkpoint `c2bc18282`. `test_crate_run.py` adds one single-file frozen class seed (`paths=("src/lib.rs",)`) for thirteen setup calls and moves seven setup-only cases onto the default `frozen_tests` seed; each test restores its private files and opens its own ledger. `test_standing_checkpoint.py` routes the direct publish and gate helpers through `run_style_command` for `style_plan` calls only; Git calls keep their lock descriptors, and one publication-to-footprint child path and both notification CLI calls stay. The manifest, custom-source, changed-manifest, shared-proposal, dry-run, loader and pre-freeze cases keep their live paths. 111 tests and 687 assertion nodes kept. aws pairs: `test_crate_run.py` 58.29 to 57.86 CPU s (0.43 s), `test_standing_checkpoint.py` 10.25 to 9.27 (0.98 s).

### Phase 194 — A terminal crate-run resume skips frozen checkout preparation  · status: done
**Depends on:** Phase 193

#### As-built

- Checkpoint `7a5781915`. `CrateRun.advance` (`crate_run.py:977`) looks up the package and its saved stage before frozen checkout acquisition and returns for `ABANDONED` and `INTEGRATED`; the `serial` run lock and the `complete_run` context stay around the return, and the nine other stages still acquire the checkout. The proof (in the checkpoint message) shows that the terminal body, `complete_run`, `copies.workspace`, `copies.end_workspaces` and the `scratch` removal path read the canonical manifest, worker records, holds and the copies registry, never the frozen checkout; the skipped work is the unused `copies.standing` lifetime. One row in an existing terminal cleanup test (`test_crate_run.py:511`) fails on the old code. 96 tests and 587 assertions kept. aws pairs: `test_crate_run.py` 58.06 to 56.15 CPU s (1.91 s); `test_bulk_run.py` unchanged.

### Phase 195 — Profile the copy-guard source scan and name its removable work  · status: done
**Depends on:** none

#### As-built

- Research; no commit. Local runs: `copies/test_copy_guard_sources.py` 34.1 and 20.2 CPU s (two runs, both pass). Step split: Python policy evaluation 15.8–16.2 s, shell checks 2.8–3.0 s, AST parses 2.0 s, file reads 0.03 s, no child processes. Both tests already share one scan; each file is read and parsed once. Candidates (bounds, not measured; all queue-owned in `scripts/copies/copy_guard.py`): allocate fallbacks only for missing keys (`:797`) 0–9.8 s; combine scope traversals (`:900,1124`) 0–1.8 s; smaller items below 0.4 s. First aws trial: the `:797` change alone, three pairs, all 50 policy tests kept, after queue's go. Report: `phases/195/copy_guard_report.md` in the session folder.

### Phase 196 — Profile the bulk-run interruption tests and name their repeated preparation  · status: done
**Depends on:** none

#### As-built

- Research; no commit. Local runs: `style/test_bulk_run.py` 34.6 and 35.6 CPU s, 108 tests pass, 11,303 process starts per run. The four interruption subtests at `test_bulk_run.py:713` (one method) use 2.4–2.6 CPU s: cleanup 0.59–0.62, recovery 0.61–0.64, tested-stage 0.53–0.54, startup 0.25–0.26, shared fixtures 0.40–0.43. They already share one repository and staff seed. Candidates (bounds, not measured; natest-owned): prepare findings once before the seed copy (`:716,734`) 0–0.09 s; install fixed tools before the seed copy (`:716,736`) below 0.001 s. Both are below what three aws pairs can detect, so no trial runs; the interruption cases are at their practical floor. The rest of the module's CPU is in the other 104 tests. Report: `phases/196/bulk_run_report.md` in the session folder.

### Phase 197 — The copy-guard policy allocates fallbacks only for missing keys  · status: done
**Depends on:** none

#### As-built

- Checkpoint `90cd75ec3`. `_join_bindings` (`copy_guard.py:797`) builds the `ScalarValue(UnknownArgument(key))` fallback only when a branch lacks the key, in place of the eager `dict.get` default; the values given to `_union` are unchanged (reviewers compared 4,805 and 312 cases with the old code). No test changed; 275 importer tests and all 50 policy tests pass with unchanged counts. aws pairs: `copies/test_copy_guard_sources.py` 28.82 to 19.25 CPU s (9.63 s, 3/3 rounds); `copies/test_copy_guard.py` unchanged. queue reviewed and approved the change; its follow-up suggestion (`_union` linear scan) is Phase 198.

### Phase 198 — The copy-guard policy finds repeated union values by hash, not by a list scan  · status: done
**Depends on:** Phase 197

#### As-built

- No gain; nothing landed. Two queue-approved `_union` versions (`copy_guard.py:786`) were measured in aws pairs against `96e8e395f`: a dict used as an ordered set made `copies/test_copy_guard_sources.py` 3.69 CPU s slower (19.21 to 22.90, 3/3 rounds), because frozen dataclass hashes are not cached and walk every nested tuple on each insert; an identity fast path in front of the old list scan made it 0.61 s slower (19.17 to 19.78, 3/3 rounds). `copy_guard.py` is unchanged. Do not retry hashed membership for these value types without a cached hash.

### Phase 199 — Measure repeated target metadata reads within one build-target request  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. Report: `phases/199/target_reads_report.md` in the run folder. One target command costs 0.11 to 0.35 local CPU s (`eligible` 0.30–0.35, `reserve` 0.28–0.33, `register-local` 0.14–0.17, `release-wait` 0.11–0.13), so the Phase 177 group cost (541 starts, 90.862 local CPU s) is mostly process start, not repeated reads. Combinable reads are small; bounds overlap and are not savings: parsed settings outside AWS 0–0.378 s (`scripts/mac_test/offload.py:1053`), repository metadata 0–0.328 s (`scripts/buildlog/repository.py:46`), target registry 0–0.309 s (`scripts/build_targets/target_config.py:134`, already cached for `eligible` and `release-wait`), validated maps 0–0.144 s and scalar settings 0–0.050 s (`scripts/lib/conf.py:174,117`), Cargo manifests 0–0.014 s. resources and verify own every candidate; the smallest first trial is the adjacent reads at `offload.py:1057–1058` (0–0.131 s). Two SSH routing trace passes failed one unchanged assertion (an unexpected Mac run seen only under tracing); the CPU pass succeeds.

### Phase 200 — Measure repeated Git metadata queries within one caller  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. Report: `phases/200/git_metadata_report.md` in the run folder. Measured inclusive caller CPU (do not add): `copies.git` 4,479 calls 4.450 s; `copies.commit_at` 1,321 calls 1.448 s; `copies.main_checkout` and `copies.repository_name` 2,397 calls 2.248 s each; `style.run_git` 8,510 calls 5.873 s; fixture Git wrapper 650 calls 0.371 s. Candidate bounds (not savings): combine commit and tree reads 0–2.039 s (`style_ledger.py:1938–1940`, style); combine publication metadata 0–0.847 s (`models_registry.py:362,536`, `main_push_lock.py:22`, sdlc and devtools); combine the bare-flag query 0–0.755 s, 799 starts (`copies.py:476,478`, queue); reuse a verified commit 0–0.459 s (`style_ledger.py:2709,1939`, style); reuse workspace identity 0–0.216 s and common directory 0–0.147 s (`copies.py:965,982,981`, queue); fixture: reuse stable ledger locations 0–0.157 s (`ledger_test_support.py:355`, natest). Fresh refs and working-tree checks stay live. The first trial is the copies bare-flag query after linked-layout coverage.

### Phase 201 — Record-content tests call the recorder in process  · status: done
**Depends on:** none

#### As-built

- Checkpoint `1674a8375`. `scripts/buildlog/test_record.py` runs its record-content cases through `recorder.main` and `step_context.main` in process with one `invoke_cli` helper (`test_record.py:169`). The helper sets the full environment, working folder, argv, stdin and captured output per call, maps `SystemExit` to the status, patches `recorder.detach` (`setsid` and descriptor closure), and restores all of them after the call. Real CLI starts stay for `test_step_record_fields`, the metadata stdin case, the entry-point case (now with an argument-error status) and the shell integration cases. All 84 test methods and every assertion stay; no production file changed. report approved the file at SHA-256 `feb6fcda`. aws pairs: `buildlog/test_record.py` 22.90 to 11.50 CPU s (11.40 s, 3/3 rounds); `test_record_rerun.py` 0.25 s; `test_step_row.py` unchanged.

### Phase 202 — Hold-event tests run the recorder in process  · status: done
**Depends on:** none

#### As-built

- Checkpoint `9e14aa8fe`. `scripts/build_hold/build_hold_test_support.py` runs the hold-event recorder through the real `record.main` in a thread named `hold-event-recorder`, in place of one detached Python process per hold event. The row and its log path resolve under the caller environment and cwd before the caller continues; the real `store.append_line` write completes in the thread; `reap_recorders` joins every writer before a read and before teardown. The thread calls `record.main` directly, so the `record` module bindings stay stable through an append error (review fix F001; an earlier `invoke_cli` wrap restored them while the thread ran). Four cases keep real recorder processes: detached launch and reaping, two-holder record order, a locked log, and the launch-failure path. All 141 test bodies and assertions stay; no production file changed. report and resources gave the go; report checked the final support file at SHA-256 `9fd1b6cb`. aws pairs on the final file: `build_hold/test_build_hold.py` 30.72 to 9.39 CPU s (21.38 s, 3/3 rounds); wall 18.4 to 13.0 s; peak 616 to 158 MiB; `test_override_guard.py` unchanged.

### Phase 203 — Phase-table tests call the phase-table command in process  · status: done
**Depends on:** none

#### As-built

- Checkpoint `52d1e5952`. `scripts/direct/test_phase_table.py` runs its show, prune, archive and refresh content cases through `phase_table.main` in process with the shared `invoke_cli` helper (`scripts/production/cli_fixture.py:84`). The test points `showrunners.NOTIFIER_STATE_DIR` at the fixture folder and restores it after each call. `test_show_help_loads_outside_repository` keeps the real CLI entry path. All 97 test methods and 531 assertions stay; no production file changed. sdlc gave the go; file SHA-256 `98530a20`. aws pairs: 12.88 to 3.03 CPU s (9.85 s, 3/3 rounds); wall 15.7 to 5.8 s.

### Phase 204 — Screenshot tool tests call the tool in process for argument and validation cases  · status: done
**Depends on:** none

#### As-built

- Checkpoint `d3269a529`. `scripts/hana_shot/test_hana_shot.py` runs its argument and validation content cases through `hana_shot.main` in process with the shared `invoke_cli` helper. A test helper gives each direct call a private `worker_pool` and `EXIT_SIGNAL_LATCH` and closes the pool after the call. Eighteen cases keep real CLI starts: entry and cold imports, timeout limits, traceback output, background cleanup at exit, keep-awake child lifetime, and signal delivery. All 117 test methods and 575 assertions stay; no production file changed. natedev gave the go; file SHA-256 `fecae85b`. aws pairs: 24.95 to 6.88 CPU s (18.07 s, 3/3 rounds); wall 25.9 to 7.7 s.

### Phase 205 — Measure build-target command starts that tests could run in process  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. Report: `phases/205/content_call_report.md` in the run folder (160 modules, with starts, CPU, source lines, owners and isolation needs). Measured: 771 direct target-command starts (136.859 local CPU s), 211 target-wrapper starts (30.136 s) and 11 cold entries (1.092 s). Removable CPU under the verify conditions is zero in every module: direct content tests already run in process; the remaining starts check shell integration, imports, guards, waits, signals or ownership. No aws trial qualifies. All 160 modules pass after one repeat caused by a build hold.

### Phase 206 — Copies asks Git for the bare flag in one query  · status: done
**Depends on:** none

#### As-built

- `scripts/copies/copies.py` reads the common dir, the git dir and the bare flag with one `git rev-parse --path-format=absolute --git-common-dir --git-dir --is-bare-repository` call; it used two. Table rows in `scripts/copies/test_copies.py` cover normal, bare, separate-metadata and linked-worktree repositories on the old and new code; all 120 test methods remain. 25 importer modules pass (1,469 tests). aws pairs, 3 rounds: `copies/test_copies.py` 30.82 → 30.43, `style/test_crate_run.py` 56.07 → 55.00, `style/test_bulk_run.py` 43.31 → 43.06 CPU s (1.70 s saved). queue reviewed the diff. Checkpoint `5502f4ed1`.

### Phase 207 — Measure where model resolution spends CPU in tests  · status: done
**Depends on:** none

#### As-built

- Research only; no repository file changed. Report: `phases/207/model_resolution_report.md` in the run folder. `production/test_add_unit.py` costs 29.98 and 28.45 CPU s in two runs; 110 registry-reader starts cost 7.07 s. Per caller (reads / publication / catalog): add_unit 110 starts 5.585 s / 0 / 0; models_conf_publish 50 starts 2.532 s / 1,703 tasks 17.674 s / 720 tasks 3.285 s; agent_exec 48 starts 2.624 s; assign_claude_follow 14 starts 2.923 s; add_showrunner 44 starts 2.723 s. Snapshot reuse alone removes no start; the cost is Python starts. Ranked candidates pass a parsed table into an existing shell resolver: `models_registry.py:135` (models; 50 starts; 0–3.392 s), `add_unit.py:459` (devtools and sdlc; 64 starts; 0–3.219 s), `assign.py:418` (sdlc and devtools; 12 starts; 0–2.002 s). Rejected: a gate-wide snapshot, edit-base reuse, skipped catalog synchronization, reuse of the missing add-showrunner registry, nested launch reuse.

### Phase 208 — The Claude model resolver takes the parsed launch table  · status: done
**Depends on:** none

#### As-built

- `claude_model` in `scripts/lib/models_registry.py` builds one `LaunchModelConfiguration` per call and sends `configuration_shell(configuration)` through stdin to the existing Claude model shell, so the shell starts no registry-reader Python child. No resolved model, catalog or snapshot is reused. The `scripts/notify/test_review.py` process fixture accepts `input` (default `None`) and forwards it (sdlc, 2026-11-12). 44 importer modules pass (2,634 tests). aws pairs, 3 rounds: `notify/test_review.py` 10.38 → 3.38, `production/test_add_unit.py` 23.72 → 21.53, `direct/test_assign_claude_follow.py` 33.49 → 32.78, `agents/test_agent_exec.py` 19.69 → 19.60 CPU s (10.04 s saved). sdlc approved the design and saw the numbers. Checkpoint `ce9febf6d`.

### Phase 209 — The style ledger reads the commit and its tree in one Git query  · status: done
**Depends on:** none

#### As-built

- `_tip` in `scripts/style/style_ledger.py` reads the commit and its tree with one `git rev-parse --end-of-options <c>^{commit} <c>^{tree}` call; it used two. A reply that is not the echoed marker and two object ids (40 or 64 hex) raises `GitReadFailed` with status 0, so a revision range keeps a typed failure; missing, non-commit and ambiguous names keep status 128. `test_tree_entries_share_commit_and_bare_tree_inventory` in `scripts/style/test_style_ledger.py` gets four rows (missing, tree id, ambiguous prefix with SHA-1 and SHA-256 input pairs, range). By builds' order, `scripts/production/test_add_unit.py` sets `HERDR_BIN=/nonexistent/herdr` in `test_resume_uses_the_renamed_native_owner_for_arguments_and_tab`; that test raised `HerdrMissing` on a host without herdr (the aws box) since it came in (`c8dec492b`). aws pairs, 3 rounds: `style/test_style_ledger.py` 13.32 → 12.53, `style/test_style_ledger_findings.py` 27.20 → 26.20, `style/test_style_ledger_tickets.py` 23.22 → 22.37 CPU s (2.68 s saved); `test_add_unit` passes on the aws box. style approved through sdlc. Checkpoint `72252ade3`.

### Phase 210 — Usage report tests call the report in process  · status: done
**Depends on:** none

#### As-built

- `report()` in `scripts/usage/test_usage_report.py` calls the usage report `main` in process through `invoke_cli` (`scripts/production/cli_fixture.py:84`); four metric cases use scoped `METRICS` patches. Four tests keep a real process: the CLI entry with cold imports, the SQLite snapshot during a writer commit, and two `-c` scripts with their own assertions; each keeps its timeout. One embedded `assert main(...) == 0` became an `assertEqual` on the in-process status (Work Order authorized). Report script starts fell from 74 to 1. `scripts/agents/test_restart.py` binds a private `HERDR_BIN` in `RestartTestCase.setUp`; the fake-CLI override stays. On the aws box the module had 19 failures and 105 errors per run (`HerdrMissing`); now all 107 pass. aws pairs, 3 rounds: `usage/test_usage_report.py` 21.93 → 6.98 CPU s (14.92 s saved); `agents/test_restart.py` 23.15 (stops early on errors) → 24.08 CPU s, all pass. sdlc gave the owner go. Checkpoint `db1ec4e5e`.

### Phase 211 — Escalation tests call the escalation command in process  · status: done
**Depends on:** none

#### As-built

- `scripts/message/test_escalate.py` calls the escalation command in process; escalate process starts fell from 57 to 3. Tests that need a real process keep one. All tests and assertions stay. aws pairs, 3 rounds, 31 tests: `message/test_escalate.py` 13.13 → 3.93 CPU s (9.19 s saved). sdlc gave the owner go. Checkpoint `944dad03d`.

### Phase 212 — Showrunners tests call the showrunners command in process  · status: done
**Depends on:** none

#### As-built

- `ShowrunnerFixture.cli` in `scripts/production/test_showrunners.py` calls `showrunners.main` in process through `invoke_cli(production_state=True)`. Calls with pane or caller arguments keep the real CLI process. Showrunners Python starts fell from 28 to 4. All 38 tests and 154 assertions stay. aws pairs, 3 rounds: `production/test_showrunners.py` 5.03 → 2.47 CPU s (2.56 s saved). sdlc gave the owner go. Checkpoint `6ad7a4767`.

### Phase 213 — The unit director resolver takes the parsed launch table  · status: done
**Depends on:** none

#### As-built

- `director_agent` in `scripts/production/add_unit.py` reads the launch configuration in process through `models_registry.launch_configuration(os.environ, registry=models_registry.registry_path(os.environ))` in the script's checkout. The resolver shell gets the parsed table through stdin: `{ IFS= read -r -d "" table; _models_table_load "$table"; }`. Read errors become a Refusal. `models_registry.registry_path(environment)` is the one rule for `MODELS_CONFIG_FILE` or HOME. Registry-reader starts fell from 64 to 0. All tests stay. aws pairs, 3 rounds: `production/test_add_unit.py` 20.07 → 16.80 CPU s (3.28 s saved); `notify/test_review.py` no change. sdlc approved the diff. Checkpoint `719978efd`.

### Phase 214 — The assignment resolver takes the parsed launch table  · status: done
**Depends on:** none

#### As-built

- `resolve_model` in `scripts/direct/assign.py` reads the launch configuration in process with `models_registry.registry_path(env)` and sends the parsed table to the resolver shell through stdin; `call()` takes `input=`. The read form is errexit-safe: `source "$1" && { IFS= read -r -d "" table; _models_table_load "$table"; } || exit 2`. `_sync_catalog(path, environment)` reads HOME, the tier flag, the state file and both Codex sources from the call environment and runs the sync with it. `unit_effort.py` keeps its fresh editor read with `edit_base({**os.environ, OPERATION_ENVIRONMENT[1]: ""})`. Four test calls that passed `{}` pass the fixture environment, so no catalog sync starts in those tests. A direct `resolve_model` check under `SHELLOPTS=errexit` replaced a whole extra launcher row (1.80 s). aws pairs, 3 rounds: `direct/test_assign_claude_follow.py` 42.84 → 41.13 CPU s (1.55 s saved); net 1.45 s. sdlc approved the final diff. Checkpoint `b4906cdb2`.

### Phase 215 — Worker usage tests call the readout in process  · status: done
**Depends on:** none

#### As-built

- `readout` in `scripts/whoami/test_worker_usage.py` calls the readout `main` in process through `invoke_cli`. The script entry and cold-import checks keep real processes. All 27 tests and 73 assertions stay. aws pairs, 3 rounds: `whoami/test_worker_usage.py` 13.36 → 9.91 CPU s (3.44 s saved). sdlc gave the owner go. Checkpoint `745d1a5d8`.

### Phase 216 — Codex hook installer tests call the installer in process  · status: done
**Depends on:** none

#### As-built

- `run_installer` in `scripts/hooks/test_codex_hooks.py` calls the installer `main` in process through `invoke_cli`. Fresh install, dead server and closure cases keep real processes. Installer starts fell from 67 to 2. All 36 tests and 215 assertions stay. aws pairs, 3 rounds: `hooks/test_codex_hooks.py` 26.44 → 16.76 CPU s (9.68 s saved). sdlc gave the owner go. Checkpoint `b3b211e07`.

### Phase 217 — Codex wind-down tests call the wind-down command in process  · status: done
**Depends on:** none

#### As-built

- `run_script` in `scripts/production/test_codex_winddown.py` calls the wind-down `main` in process through `invoke_cli`. `start`, `triage`, `clear` and broadcast commands keep real processes; the status test keeps its CLI entry. Wind-down starts fell from 23 to 11. All 37 tests stay. aws pairs, 3 rounds: `production/test_codex_winddown.py` 16.91 → 12.16 CPU s (4.75 s saved); peak memory 128 → 143 MiB. sdlc gave the owner go. Checkpoint `ae26327bf`.

### Phase 218 — The unit lookup test module keeps only the tests that pay their way  · status: done
**Depends on:** none

#### As-built

- `scripts/production/test_unit_lookup.py` went from 67 to 60 tests under Rule 2. The removed tests checked a constructor type, timer and process-scan call counts, and a retired history tail reader. The `live_hosts` helper and unused `renamed_production` rows went with them. All 60 retained tests and 205 assertions are unchanged. Both blind reviews approved each removal. Checkpoint `76007751d`.

### Phase 219 — Gates and targeted runs stop at a time limit  · status: done
**Depends on:** none

#### As-built

- `config/merge_gate.conf` adds `GATE_TIME_LIMIT_FACTOR=1.5`. `scripts/test_times/gate_record.py` computes both limits from the newest `TOP_RUNS` passing records on the same vCPU count: a full gate uses the median gate wall time; a targeted run uses max(longest selected module median, summed module medians ÷ jobs). Any selected module with no passing history, or no history for the vCPU count, gives `NoGateTimeLimit` and one printed line. The index records `vcpus`; old records stay readable.
- `merge_gate.py` reaps finished jobs first, then applies the monotonic deadline and stops the rest through `stop_gate_jobs`. `cloud_gate.py` reads the result once more, then ends the box through the existing `terminate` call; `gate_box.py` enforces the limit on the guest. A result that finishes before the deadline passes.
- The red report reads `time limit: limit <s>; elapsed <s>; running modules: …`, then each failed module with its traceback tail. Owner notices go through `session_send.send_argv` and `send.py`, which reach unit names and staff IDs. The gate adds no signal handlers.
- One fix round closed three review findings (finished cloud result, finished local job, owner reach) and two sdlc requests. 994 tests pass in 30 modules; basedpyright clean. Live AWS teardown is not yet seen. Checkpoint `bd55aa60b`.

### Phase 220 — Pane summary tests call the summary function directly  · status: done
**Depends on:** none

#### As-built

- Three pane summary cases in `scripts/direct/test_assign_claude_panes.py` call `claude_worker._summary` directly through one `summary_turn` fixture method and the `WRITE_SUMMARY` constant (sdlc asked for both). The cleanup-before-cut case keeps the whole launcher. Launcher starts fell from 4 to 1. Three launcher exit checks became checks of the unchanged source log bytes, because `_summary` returns None; the launcher case still checks the exit. All 4 tests and 12 assertions stay. aws pairs, 3 rounds: `direct/test_assign_claude_panes.py` 8.26 → 2.46 CPU s (5.80 s saved); peak memory 157.7 → 164.0 MiB. sdlc approved the diff. Checkpoint `dcaafbc0c`.

### Phase 221 — Follow and records tests make content calls in process  · status: done
**Depends on:** none

#### As-built

- Each setUp in `scripts/direct/test_assign_claude_follow.py` and `test_assign_claude_records.py` turns on the existing `run_assignment` adapter. Nine cases keep real processes for startup hooks, child observations, recorder failures and status order. Both fixtures set `MODELS_CONFIG_TIER_ONLY=1`, so in-process calls start no catalog sync. All 18 tests and 136 assertions stay. aws pairs, 3 rounds: records 31.52 → 25.96 CPU s (5.56 s saved), peak 167.0 → 173.2 MiB; follow 32.16 → 30.92 CPU s (0.96 s saved), peak 220.2 → 225.1 MiB. sdlc approved the diff; move the repeated setUp into the shared launcher fixture when a third module adopts it. Checkpoint `a5bce07e7`.

### Phase 222 — The merge gate installs no signal handlers  · status: done
**Depends on:** Phase 219

#### As-built

- `scripts/production/merge_gate.py` and `scripts/test_times/module_run.py` install no signal handlers. Cleanup uses `cancellation.defer_cleanup_signals()`, which blocks signals through `pthread_sigmask`. The rollback handler and its `gate_box.py` caller are gone. Each gate writes one run record by atomic rename. The record contains process identities, groups, temporary roots, CPU tokens, and rollback intent. A clean end removes it. The next gate recovers a dead gate’s record. Recovery sends TERM, then KILL, to its groups even if the leader ended. Recovery restores tokens and removes folders. It checks HEAD under the checkout lock before rollback. Failed recovery keeps the record. Module children receive KILL when their parent dies. Signal tests require exact statuses: INT gives 1; other signals give `-signal`. Queue cleanup reads the previous mask before it blocks signals inside `try`. The queue test checks this order. Validation passed 323 tests in 17 modules. Checkpoint: `b17e9b84e`.

### Phase 223 — Simplify lens tests call the daily command in process  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/test_simplify_lens.py` calls `simplify_daily.main` in process through `invoke_cli`. `assign_gate.called_by_assign` refuses an in-process reviewer (unittest is the ancestor), and the guard stays. `simplify_daily.py` names its launcher path in one constant, `AGENT_EXEC`; the in-process cases bind it to a private launcher with an admission stub (the `review_launcher_test_support.py:184` pattern). The full `assign.sh` case and the status-write failure case keep the real guard. All 15 tests and 80 assertions stay; assignment starts 18 → 2. aws pairs, 3 rounds: 19.23 → 9.67 CPU s (9.56 s saved), peak 143.7 → 148.1 MiB. sdlc approved the diff. Checkpoint `8ee605e7f`.

### Phase 224 — The module runner supervisor restores its signal mask on every path  · status: done
**Depends on:** Phase 222

#### As-built

- Audit only; no code change. `run_with_descendant_cleanup()` blocks SIGTERM, SIGINT, SIGHUP and SIGCHLD at `module_run.py:580` by design, so `signalfd` can read them. Its sole caller (`:664`) has no catch, so a `signalfd`, `pidfd_open` or `prctl` error ends the process. The test child restores SIGCHLD and the previous mask at `:607-608` before `main()`; parent loss kills it with SIGKILL. The supervisor restores the mask at `:659` before `sys.exit`; signal termination exits through `:653-658`. No path keeps a process running with the mask blocked. `test_module_run.py` (24), `test_module_cost.py` (10) and `test_merge_gate_suites_signals.py` (12) pass; basedpyright clean.

### Phase 225 — Change review tests prove contention without fresh interpreters  · status: done
**Depends on:** none

#### As-built

- `scripts/production/test_change_review.py`: the lock readiness case (`:1393`) uses two forked children, not fresh interpreters. The parent keeps the list lock until both children report a failed nonblocking lock attempt. The eight-add numbering case keeps one fresh CLI process (`:1724`) and seven forked processes. Each fork closes inherited lock descriptors and keeps process-group cleanup. All 136 tests and 545 assertions are unchanged.
- AWS paired trial (3 rounds): CPU 16.32 → 14.38 s; median peak 156.3 → 147.2 MiB. Local 13.73 → 9.50 s. Checkpoint `2cc7b7d02`.

### Phase 226 — Workers and claims tests share one in-process launch fixture  · status: done
**Depends on:** none

#### As-built

- `scripts/direct/assign_claude_test_support.py:633` `InProcessAssignClaudeLauncherFixture` holds one switch: its setUp calls the inherited fixture, sets `MODELS_CONFIG_TIER_ONLY=1` and turns on in-process content calls through `run_assignment` (`:445`). A `process_boundary` marker keeps 14 real-process cases (workers 2, claims 3, follow 5, records 4). Follow and records lose their own setUp copies. The adapter captures `check-launch` output so the staff-refusal assertions see stderr. All 29 test bodies and 200 assertions are unchanged.
- AWS paired trial (3 rounds, c7a.8xlarge), CPU s: claims 11.08 → 7.92, follow 30.87 → 28.28, records 26.66 → 17.28, workers 13.08 → 9.36 (18.84 s in all). Median peak rises 26–36 MiB per module (for example workers 216.4 → 249.5). Checkpoint `ff5068f46`.

### Phase 227 — Gate recovery frees CPU tokens across checkouts and rolls back only an idle checkout  · status: done
**Depends on:** Phase 222

#### As-built

- `scripts/production/merge_gate.py:799` recovery reads every checkout directory under `active-runs`, each under its own `LOCK_EX` directory lock (`:815`). A dead gate's FIFO CPU tokens return for a gate in any checkout.
- Each checkout run directory has an `operation.lock`. A live gate takes it `LOCK_SH` after its own recovery (`:859,864`) and keeps it until its clean record removal (`:875`). Rollback takes it `LOCK_EX | LOCK_NB` (`:766`) and checks HEAD and rolls back inside it. When busy, recovery frees groups, tokens and folders, keeps only the rollback intent, prints one line and leaves HEAD unchanged; a later start finishes the rollback. `scratch.copy_lock()` is unchanged; no signal handler is added.
- `test_merge_gate_suites_signals.py:81` gains two rows (other-checkout token return; deferred rollback under a live holder); both fail on the old code. 230 merge gate tests pass. Checkpoint `2c59524fa`.
- Open (routed by builds to sdlc devtools): `scripts/lib/build_folders.py:47,262,286` still installs signal handlers on the gate path through `temporary_build_folder()`.

### Phase 228 — The whole-machine cloud guest keeps no shared gate state  · status: done
**Depends on:** Phase 227

#### As-built

- Phase 222 made every gate run shared recovery, so the whole-machine guest reached `gate_state_dir()` and `test_gate_trial.py` `GuestTests.test_guest_uses_whole_machine_without_types_with_timeout_and_counts` failed. Now a `WholeMachine` run skips shared recovery (`merge_gate.py:1408`) and keeps its `run.json` and `operation.lock` in a private `merge-gate-recovery-*` directory under tmpfs (`:879`), removed at normal or failed exit. Nested runs keep their existing recovery owner. Shared recovery is unchanged; the guest test is unchanged. 179 aws target tests and 230 merge gate tests pass. Checkpoint `ef465ee86`.

### Phase 229 — Unit move and shutdown tests find and remove their in-process CPU  · status: done
**Depends on:** none

#### As-built

- `unit_move.processes_inside` scanned the host `/proc` and ignored `HERDR_PROC`, so one move mixed host processes with private test records. It now reads `herdr.proc_root()`; `herdr._proc_root` is renamed with no alias. The fixtures (`rename_unit_test_support.py`, `rename_test_support.py`) publish the private process records the host scan never needed; `UnitRenameFixture` links only its own direct children for one call and skips that bridge on Darwin. A direct test in `test_unit_move.py` covers exact, descendant, unrelated and deleted cwd paths. No assertion changes. aws pairs: unit move 18.22 to 17.47 CPU s, shutdown CLI 36.95 to 36.54 CPU s, lower in every pair; peak 148.0 to 147.4 and 226.7 to 225.7 MiB. natedev medians fell 32–35%, because natedev has many host processes. On the aws trial guest, 18 shutdown CLI resume tests fail on base and changed alike; impl4 diagnoses. Checkpoint `46ab6fa60`.

### Phase 230 — Find the tests that scan every host process  · status: done
**Depends on:** Phase 229

#### As-built

- Research only; no repository change. Report: unit session `phases/230/report.txt` (baseline `8d8ef549c`). 45 static scan sites; 209 of 210 paired comparisons pass. Host scan CPU on natedev, scan against a small private process view: `message/process_test_support.py:105` `OwnedTestProcesses.pin_descendants` 53.9 s (7 modules); `direct/verify_mac_offload_test_support.py:93` `processes_carrying` 35.0 s (70 modules); `lint/tmp_sweep.py:523` via `copies.scan_mirror_processes` 24.4 s (6); `production/merge_gate.py:618` `process_memory_mib` 11.6 s (15); `lint/memory_admit.py:873` 6.4 s (2); `direct/board.sh:311` `live_group` 5.0 s (79); `verify_token_wait_test_support.py:279` 2.4 s; `message/process_table.py:23` 1.5 s; `verify_token_ownership_test_support.py:36` 1.0 s; `lint/sweep.py:1230` 0.7 s. Each fix keeps real owned processes and reads only the owned PIDs or the selected root. Slow modules now: verify_wait_holder 90.9 CPU s (progress_history.py 89 starts 24.6 s; build_targets.py 50 starts 11.1 s), crate_run 56.2 (Git 24,108 starts 25.8 s), fast_track 37.5 (fast_track.py 22 starts 19.9 s; Git 7,446 starts 19.5 s).

### Phase 231 — Every test module run sees no host herdr  · status: done
**Depends on:** Phase 229

#### As-built

- The paired aws guest (`gate_box.py` paired environment) did not set `HERDR_BIN`, so 18 `agents/test_shutdown_cli.py` tests raised `HerdrMissing` on base and changed alike. `merge_gate.TEST_HERDR_BIN` is now the one definition; `test_module_environment` sets it after the inherited environment. The normal guest, both `module_cost.py` paths and `fast_track.fast_check` drop their copies; the paired supervisor embeds the constant beside the copied builder. `module_cost.py` now gives a module the gate's full environment. All 136 shutdown CLI tests pass through `module_cost.py` with `HERDR_BIN` unset. Checkpoint `0db93aadd`.

### Phase 232 — The chain415 gate failures are fixed or owned  · status: done
**Depends on:** Phase 231

#### As-built

- Two of the five chain415 failures came from this branch. `buildlog/test_gate_index.py` now expects the nullable `vcpus` column from Phase 219.
- Phase 222 masked cancellation signals only in the calling thread. A process-directed SIGINT taken by another thread broke `reap_gate_job`: it lost status and CPU, then raised `ChildProcessError`. `scripts/lib/cancellation.py` now owns `CANCELLATION_SIGNALS`, `defer_cleanup_signals` and `start_masked_thread`, which starts an existing thread under the mask so it inherits it. The gate_box guest memory monitor, the queue_place sampler, the `copies.available_tree` waiter and the admission test fixture loop start through it. QueuePlace records its child before the sampler starts. No signal handlers.
- Base failures with owners, sent to builds: `direct/test_verify_admission_signals.py` (builds-queue `730e73dc1`, build-followups-test-speed `f085ddb08a`), `herdr/test_worker_panes.py` (staff-copies `7676e0473`, quota-refusal `b711e365a`), `review_links/test_change_page.py` (sdlc-fix-rounds `8786bfbef3`).
- The nosignals phase removes the unused signal recorder from `build_folders.py`. Callers keep `cancellation.defer_cleanup_signals()`, which uses a signal mask. The earlier deletion order was recorded at checkpoint `4f09f6f20`.

### Phase 233 — The lifecycle tests reuse one running-state repository  · status: done
**Depends on:** Phase 232

#### As-built

- `LifecycleFixture.setUpClass` (`production_lifecycle_test_support.py`) saves a second snapshot, `running_snapshot`, after `build_running_repository()`. That method holds the Git part of the old `running()`: branch switch, running document add and commit, two pushes, two worktree adds.
- `running()` replaces `origin.git`, `checkout`, `alpha` and `beta` with copies from that snapshot. The fixed paths keep the Git worktree links valid. Then it writes the per-test staff rows, panes, transcripts, unit config, exclude entry and log.
- The test module did not change: 70 tests, all assertions and `run_lifecycle_process` stay. `running()` Git commands went from 448 to 56 in each module run.
- aws paired trial, 3 rounds on c7a.8xlarge: CPU median 32.730 s to 30.856 s (saving 1.876 s); peak 155.2 to 159.2 MiB. natedev module_cost CPU 34.848 to 26.544 s. Checkpoint `1b63e07bf`.

### Phase 234 — The unit config tests copy one seed repository  · status: done
**Depends on:** Phase 233

#### As-built

- `UnitConfigFixture.setUpClass` (`unit_config_test_support.py`) builds one seed repository under a private home: `git init` on `main` and one empty commit. `setUp` copies it into each test's new root. Each test keeps its own home, environment, staff file and staff rows.
- The three fixture classes inherit the seed. `test_unit_config.py` (45 tests) and `lib/test_git_exclude.py` (5 tests) did not change.
- aws paired trial, 3 rounds on m7a.8xlarge: `test_unit_config.py` CPU 3.099 to 2.947 s; `test_git_exclude.py` 0.295 to 0.285 s; peak unchanged. natedev module_cost 3.915 to 1.744 s for both. Taplo (158 starts, 1.16 natedev CPU s) is now the top command in `test_unit_config.py`. Checkpoint `63acaa3e0`.

### Phase 235 — The copies tests copy one source seed  · status: done
**Depends on:** Phase 234

#### As-built

- `StandingTests.setUpClass` (`copies/test_copies.py`) stores the published and unpushed source states once. `setUp` copies them with `ImmutableGitSeed.copy_to`, which rewrites the checkout origin URL. `ImmutableGitSeed.create` falls from 122 calls to 3. Each test gets its own Git config file with no path into the seed.
- `ImmutableGitSeed.copy_to` (`production/cli_fixture.py`) raises `ValueError` when the config does not hold the seed origin text, so an escaped path never leaves a copy that points at the seed. Its 11 caller modules (470 tests) pass. All 120 test bodies and 602 assertion expressions did not change.
- aws paired trial, 3 rounds on m7a.8xlarge: CPU 30.596 to 26.526 s; peak 119.8 to 119.2 MiB. Checkpoint `3ff00e6ad`.

### Phase 236 — The shutdown CLI tests copy retired and resume snapshots  · status: done
**Depends on:** Phase 234

#### As-built

- `RetireUnitTests` and `ResumeUnitTests` (`agents/test_shutdown_cli.py`) build the retired and resume repository states once per class at fixed roots. Each test copies its snapshot back. Nested retirement fixtures keep separate roots.
- `restore_repository` registers the removal of the destination before the copy, so a failed copy leaves no partial folder (closure of review finding F001). All 136 test bodies and 943 assertion sites did not change.
- aws paired trial, 3 rounds on m7a.8xlarge: CPU 37.478 to 34.466 s; peak 226.1 to 225.4 MiB. FolderRename pending move (16.6 natedev CPU s) and UnitRename identity (9.9 s) are the next costs. Checkpoint `f65598a5a`.

### Phase 237 — The build record tests copy one seed repository  · status: done
**Depends on:** Phase 234

#### As-built

- `RecordTests.setUpClass` (`buildlog/test_record.py`) builds one seed repository under a private home: `git init` on `main` and one empty commit. `setUp` copies it into each test's `self.repo`. Admission state, build records, tools, environment and patches stay private per test.
- Callers in other modules that build the fixture without class setup register the seed cleanup before the seed build, so a failed build leaves no seed. All 84 test bodies and assertions did not change.
- aws paired trial, 3 rounds on m7a.8xlarge: CPU 11.874 to 11.493 s; peak 136.6 to 135.5 MiB. Admission (`admission_client.py`, 2.9 natedev CPU s) and `module_run.py` stay the top costs. Checkpoint `4f9a19c62`.

### Phase 238 — The models publication tests copy one publication fixture  · status: done
**Depends on:** Phase 234

#### As-built

- `RegistryPublishingTests.setUpClass` (`agents/test_models_conf_publish.py`) builds the checkout, bare remote, competitor clone, registry and the dirty, staged and unstaged states once. Each test copies the whole seed back to the same fixed class root, so both absolute origin URLs stay valid, and refreshes both copied indexes. Each test gets its own environment dictionary; `initial` is a class value.
- Real writers, locks, rejected pushes and publication checks did not change. All 51 test bodies and 214 assertions did not change.
- aws paired trial, 3 rounds on m7a.8xlarge: CPU 32.828 to 31.491 s; peak 104.2 to 104.4 MiB. Git (17.3 natedev CPU s) and `models_registry.py` (6.8 s) stay the top commands. Checkpoint `0a5f1f853`.

### Phase 239 — The unit config fixtures write formatted configs without Taplo  · status: done
**Depends on:** Phase 234

#### As-built

- `production_lifecycle_test_support.py` writes the alpha and beta unit configs once before `running_snapshot`; `running()` copies them with the repository. Lifecycle fixture Taplo starts fell from 112 to 2. All 70 lifecycle test bodies did not change.
- The repair (sdlc review) removed the `ParkedConfigBaseline` cache from `unit_config_test_support.py`: it copied the side effects of park by hand, so a change to park would leave the fixture wrong without an error. `park_config` runs real park again.
- aws paired trial of the lifecycle part alone, 3 rounds: `test_production_lifecycle` CPU 29.450 to 28.069 s, peak 160.9 to 156.9 MiB; `test_unit_config` unchanged. Checkpoint `dc08e6574` and the repair checkpoint.

### Phase 240 — The message process fixture finds descendants without a host scan  · status: done
**Depends on:** none

#### As-built

- `OwnedTestProcesses.pin_descendants` (`message/process_test_support.py`) walks the descendant task trees (`/proc/<pid>/task/<tid>/children`) under the subreaper on Linux and revisits each parent after its descendants, so a child forked during the walk is found. It no longer scans every process on the host. Owner markers, pidfd pinning, the stop, kill and reap order and the macOS branch did not change.
- All 198 test bodies and 829 assertion calls in the 7 importers did not change. natedev attribution: `pin_descendants` 47.8 to 0.05 CPU s; the old scan cost grows with the host's process count.
- aws paired trial, 3 rounds, sum of 7 modules: CPU 57.11 to 55.19 s (notifier 11.67 to 9.94; `repeat_guard` 15.55 to 15.90). Peak per module changed by -13.5 to +1.9 MiB. Checkpoint `f16184193`.

### Phase 241 — The CI points tests copy one seed repository  · status: done
**Depends on:** none

#### As-built

- `production/test_ci_points.py` builds the full fixture once per class (`build_fixture` keeps the old setup body) and copies it per test to the same fixed root. Origin, document, staff and environment paths select the restored root. Each test gets its own environment dictionary and a refreshed checkout index; cleanup registrations precede each build or copy.
- setUp ran 11 Git commands in each of 34 tests; it now copies. Tested fetches, commits, locks, races and recovery stay real. All 34 test bodies and 277 assertion expressions did not change.
- aws paired trial, 3 rounds: CPU 13.399 to 11.942 s; peak 165.2 to 152.2 MiB. Checkpoint `07a15ce4c`.

### Phase 242 — The notifier tests start fewer shells  · status: done
**Depends on:** none

#### As-built

- `run_cli` in `message/test_notifier.py` computes the shown zone in process (`invoke_cli`, `user_zone.CONF` bound per call) and passes `SHOWN_ZONE` to zsh; two zone-read cases keep the real shell (`@process_boundary`).
- aws pairs: median CPU 11.749 -> 9.014 s; peak 112.8 -> 109.2 MiB. 35 tests and 172 assertions unchanged.
- Review F001 (config binding, then relative paths) and F002 (helper failure keeps the shell start) closed in two repair rounds. Checkpoint 4afd7c4f5.

### Phase 243 — The rename tests reuse their restored fixture state  · status: done
**Depends on:** none

#### As-built

- Folded into Phase 252; no separate checkpoint. The change binds `RUN_ROOT` and `FORMER_RUN_ROOT` to each rename fixture root in `agents/test_shutdown_cli.py`.
- natedev module CPU 81.5 -> 37.8 s: the rename fixtures read 422,583 live run JSON files on the host. aws pairs 34.319 -> 34.423 CPU s, peak 224.5 -> 227.1 MiB: the aws host has no live run folders, so no gain.
- The finding is a test-isolation defect; Phase 252 owns the fix for every module.

### Phase 244 — The verify wait holder tests start fewer recorder and target helpers  · status: done
**Depends on:** none

#### As-built

- `direct/test_verify_wait_holder.py`: the recorder and admission-client observers run the real code in the child interpreter that already started (runpy), not in a second exec; `RecordedHoldWaitFixture` shares the class target seed with the hold and memory-route classes.
- aws pairs: 71.700 -> 71.039 CPU s (paired median saving 0.664 s); peak 274.4 -> 272.3 MiB. natedev showed 82.4 -> 67.3 s, mostly host state.
- 27 tests and 133 assertions unchanged; two blind reviews approve with no findings.

### Phase 245 — The crate run tests copy class seed repositories  · status: done
**Depends on:** none

#### As-built

- Profile only; no source change. `setUpClass` already builds the initial, frozen and single-file frozen seeds; each test restores one seed at the fixed root. Class setup 0.28 CPU s and all 96 `setUp` calls 0.85 s: 2% of the suite.
- Module 56.2 natedev CPU s: Git 24,125 starts (26.3 s) run inside tested crate run behavior; `module_run.py` 17.6 s. A test-only change has no budget left.
- Found: `setUp` registers its cleanup after `copytree`, the Phase 236 F001 defect class. Architecture proposal to sdlc: the crate run code starts about 250 Git commands per test.

### Phase 246 — The fast track tests copy class seed repositories  · status: done
**Depends on:** none

#### As-built

- Profile only; no source change. `MergeCheckpointFixture.setUpClass` already builds the repositories once and saves a class snapshot; per-test setup starts no Git command (`FastTrackTests.setUp` 2.6 CPU s over 50 tests).
- Module 49.1 natedev CPU s: Git 7,446 starts (17.5 s) and `fast_track.py` 22 starts (11.9 s) run inside tested `fast_track.py` behavior (restores, patches, retries, publication, recovery). A test-only change has no budget left.
- Architecture proposal to sdlc: profile `fast_track.py` itself (0.54 CPU s and about 340 Git starts per run).

### Phase 247 — The offload cleanup tests do less repeated work  · status: done
**Depends on:** none

#### As-built

- No source change lands. The candidate built one bytecode cache per class for the `python3 -c` children of `mac_test/test_offload_cleanup.py`.
- aws pairs: 26.793 -> 27.657 CPU s (paired median saving -0.866 s); peak 98.3 -> 98.8 MiB. No gain, so the change is dropped; the diff is in the phase folder as `rejected_change.diff`.
- natedev shows 46.5 CPU s and 96 `python3 -c` starts at 0.40 CPU s each; aws shows 26.8 s for the module. The extra cost is natedev Python start-up, not the test.

### Phase 248 — The verify SSH routing tests do less repeated work  · status: done
**Depends on:** none

#### As-built

- Profile only; no source change. The fixture already copies a class seed at fixed paths; class setup 0.09 CPU s and all per-test setup 0.29 s: 0.56% of the suite.
- Module 50.3 natedev CPU s, 4,355 processes: real `verify.sh` routing runs. `build_targets.py` 49 starts 7.8 s, `python3 -c` 40 starts 3.0 s, `verify_helper.py` 17 starts 2.6 s, `build_folders.py` 46 starts 2.5 s, `detached_runs.py` 17 starts 2.0 s.
- Top test `pinned_target_refusal_keeps_the_reason_and_never_runs_locally` 10.1 s runs four real refusals, about 2.5 CPU s each. Architecture proposal to sdlc: a verify refusal costs about 2.5 CPU s in helper starts.

### Phase 249 — The bulk run tests do less repeated work  · status: done
**Depends on:** none

#### Work Order

**Goal:** `style/test_bulk_run.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** Gate median CPU 47.2 s. sdlc owner go 2026-11-13 15:35, profile first.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD style/test_bulk_run.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated setup or process starts that no test checks. Build shared state once per class and copy it per test where setup is a top cost. Keep each real start that a test checks for exit status, signals, waits, routing or recovery. Never start anything on the Mac.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/style/test_bulk_run.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phases 235–243: class seed copies; register cleanup before a copy; bind import-time roots to the fixture root (Phase 243).
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 250 — The verify Mac-only tests do less repeated work  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `direct/test_verify_mac_only.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** Gate median CPU 46.1 s. sdlc owner go 2026-11-13 15:35, profile first.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD direct/test_verify_mac_only.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated setup or process starts that no test checks. Build shared state once per class and copy it per test where setup is a top cost. Keep each real start that a test checks for exit status, signals, waits, routing or recovery. Never start anything on the Mac.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/direct/test_verify_mac_only.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phases 235–243: class seed copies; register cleanup before a copy; bind import-time roots to the fixture root (Phase 243).
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 251 — The crate run setup removes a partial seed copy  · status: done
**Depends on:** none

#### As-built

- `setUp` in `style/test_crate_run.py` registers `shutil.rmtree(self.root, ignore_errors=True)` before `copytree`; a failed copy leaves no partial root.
- Correctness fix with no aws trial. Probes inject a failure before and after root creation; each gives one setup error, and the next test passes.
- 96 tests and 589 assertions unchanged; two blind reviews approve with no findings. Checkpoint 536f8eb74.

### Phase 252 — Tests never reach the real unit run roots  · status: todo
**Depends on:** none

#### Work Order

**Goal:** No test reads or writes the real unit run roots (`/tmp/claude/unit`, `/tmp/claude/delegate`, `/tmp/claude/astra`). One shared helper binds them; a test-support default makes them unreachable.

**Decision:** sdlc owner assignment 2026-11-13 15:14, from the Phase 243 finding. `test_shutdown_cli.py` rename fixtures read 422,583 live run JSON files. `unit_runs.RUN_ROOT`, `FORMER_RUN_ROOT` and `ASTRA_RUN_ROOT` (`scripts/lib/unit_runs.py:31-33`) are set at import from `SCRATCH_ROOT` and `ASTRA_REVIEW_DIR`. Test fixtures patch some roots and leave others real.

**Spec:**
1. Add one shared helper, `bind_private_run_roots(case, root)`, in a test-support module beside `unit_runs` (for example `scripts/lib/unit_runs_test_support.py`). It patches `RUN_ROOT`, `FORMER_RUN_ROOT` and `ASTRA_RUN_ROOT` to folders under `root`. It registers `patch.stop` before `patch.start`. It makes no folder and holds no snapshot path.
2. Replace `bind_rename_run_roots` (`scripts/agents/test_shutdown_cli.py:2824`) with the shared helper.
3. Use the helper in the fixtures that leave a root real:
   - `scripts/production/rename_unit_test_support.py` `FolderRenameFixture` (used by four classes in `scripts/production/test_unit_move.py`: `UnitFolderMoveTests`, `FolderBranchRecoveryTests`, `FolderRenameConfigTests`, `FolderMoveReviewTests`).
   - `scripts/production/rename_test_support.py:238` `FolderMoveFixture` (patches `RUN_ROOT` only; `CodexPaneRestoreTests` in `scripts/agents/test_pane_restore.py` reaches `FORMER_RUN_ROOT`).
   - `scripts/production/test_rename_unit_processes.py:171` `RunRecordLookupTests` (patches `RUN_ROOT` only; `run_is_open` reaches `run_roots`).
   Keep any root a test sets on purpose; the helper only replaces a real root.
4. Add the default: find the one entry that every Python test process passes through (the gate runner, `scripts/test_times/module_run.py`, the `verify.sh` Python lane). Set `SCRATCH_ROOT` and `ASTRA_REVIEW_DIR` there to a private folder for the run, removed at the end. Then a fixture without the helper resolves to a private root. If no single entry exists, report the entries to the director before you change one.
5. Search for other modules that reach the three roots or `run_roots()` without a private binding. Fix each in a test file, or list it with its evidence.
6. Prove it: an observation probe (as in `phases/243/impl4/empty_roots.py`) records zero reads of the real roots for each changed module.
7. Run each changed test module three times and basedpyright on each changed file. Report `module_cost.py` CPU before and after for each changed module.

**Files:**
- `scripts/lib/unit_runs_test_support.py` (new) — hub
- `scripts/agents/test_shutdown_cli.py`
- `scripts/production/rename_unit_test_support.py`
- `scripts/production/rename_test_support.py`
- `scripts/production/test_rename_unit_processes.py`
- the test runner entry from step 4
- other modules from step 5

**Workers:** 1 writer; the helper is a hub every other file needs.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phase 243 is folded into this phase. `scripts/agents/test_shutdown_cli.py` holds its uncommitted change (SHA-256 06d351b53e4bb9afa1c3e9ca4590921bd965fcae517433da7154f93243ce9f6c): `bind_rename_run_roots` binds `RUN_ROOT` and `FORMER_RUN_ROOT` for three rename classes. Build on it; replace the helper with the shared one. Evidence: `phases/243/impl4/`. Its aws pairs show no gain (34.319 -> 34.423 CPU s): the aws host has no live run folders. The 81.5 -> 37.8 natedev gain is the host's live run folders. So this phase is a correctness fix.
- Phases 236, 237, 251: register each cleanup before the step that can leave partial state.
- sdlc terms: test and test-support files only; every test and assertion stays. A correctness fix needs no aws gain; report the aws pairs for changed modules.

**Acceptance gate:**
- The same test count and assertions in every changed module; each passes three times.
- The probe shows zero reads of the real roots for each changed module.
- A fixture with no helper, run under the default, resolves to a private root.
- basedpyright 0 errors and 0 warnings on each changed file.

### Phase 253 — The shutdown CLI tests copy class-built Git repositories  · status: todo
**Depends on:** Phase 252

#### Work Order

**Goal:** `agents/test_shutdown_cli.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** sdlc go 2026-11-13 15:25, option (a) after Phase 252. After Phase 243 the module costs 37.8 natedev CPU s for 136 tests: Git 7,380 starts (8.3 s), git-upload-pack and git-receive-pack 976 starts (2.8 s), taplo 231 starts (1.1 s). Top tests: RetireUnitTests unowned_or_live_sessions 3.7 s, retirement_without_saved_work 3.3 s, remote_retirement_retries_failed_steps 3.2 s; UnitRename preserve identity 1.9 s; WorkerRename owner_rename 1.8 s.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD agents/test_shutdown_cli.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Build each Git repository, bare remote and checkout that `ShutdownRepositoryFixture` (`test_shutdown_cli.py:214`), `UnitRenameFixture`, `FolderRenameFixture` and `WorkerRenameTests` make in `setUp` once per class, and copy it per test, as Phases 235-238 did. Absolute origin or worktree paths need a fixed per-class root or a rewrite after the copy. A copy must hold no path into the seed. Keep tested fetches, pushes, commits, retries and recovery real; copy only the state a test reads before its tested step.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/agents/test_shutdown_cli.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phase 252: every fixture binds private run roots through `bind_private_run_roots`; keep it. Phases 235-238 and 251: class seed copies; register cleanup before a copy.
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 254 — The roster tests do less repeated work  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `message/test_roster.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** next untouched module in the descending CPU list (show.py top 15:18: rank 10, 38.5 median CPU s, 125 tests; ranks 1-9 and 11 are done or planned). Slowest kept test `RosterTests.test_an_unreadable_herdr_session_keeps_codex_rows_from_other_sessions` 9.1 s.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD message/test_roster.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated work a test does not check: per-test setup that can be built once per class and copied, process starts that a content-only test does not check (use the `invoke_cli` pattern), and repeated parsing. Keep real process starts where a test checks process integration, exit status, signals or waits.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/message/test_roster.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phases 235-238 and 251: class seed copies; register cleanup before a copy. Phase 252: bind private run roots with `bind_private_run_roots` if the module reaches a unit run root. Phase 242: in-process calls must bind module state that the real process reads at start (for example a config path) for each call.
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 255 — The send tests do less repeated work  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `message/test_send.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** next untouched module in the descending CPU list (show.py top 15:18: rank 12, 38.0 median CPU s, 316 tests). Slowest kept test `AccountDaemonRoutingTests.test_names_that_differ_only_in_case_are_ambiguous_even_with_an_exact_match` 5.5 s.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD message/test_send.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated work a test does not check: per-test setup that can be built once per class and copied, process starts that a content-only test does not check (use the `invoke_cli` pattern), and repeated parsing. Keep real process starts where a test checks process integration, exit status, signals or waits. Also look for test-isolation defects: inherited `UNIT_*` variables, real run roots, real staff or home paths. Report each one.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/message/test_send.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phases 235-238 and 251: class seed copies; register cleanup before a copy. Phase 252: bind private run roots with `bind_private_run_roots`. Phase 242: in-process calls bind per call the module state that the real process reads at start. Phases 240-247: natedev gains can come from host state; only aws pairs decide a land.
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 256 — The stall watch tests do less repeated work  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `production/test_stall_watch.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** next untouched module in the descending CPU list (show.py top 15:18: rank 17, 35.9 median CPU s, 122 tests; ranks 13-16 are done in earlier phases or in review). Slowest kept test `StallWatchTests.test_a_resume_during_a_running_tick_retires_the_observation_then_observes_the_new_prompt` 11.5 s.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD production/test_stall_watch.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated work a test does not check: per-test setup that can be built once per class and copied, process starts that a content-only test does not check (use the `invoke_cli` pattern), and repeated parsing. Keep real process starts where a test checks process integration, exit status, signals or waits. Also look for test-isolation defects: inherited `UNIT_*` variables, real run roots, real staff or home paths. Report each one.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/production/test_stall_watch.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phases 235-238 and 251: class seed copies; register cleanup before a copy. Phase 252: bind private run roots with `bind_private_run_roots`. Phase 242: in-process calls bind per call the module state that the real process reads at start. Phases 240-247: natedev gains can come from host state; only aws pairs decide a land.
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.

### Phase 257 — The verify result environment tests do less repeated work  · status: todo
**Depends on:** Phase 250

#### Work Order

**Goal:** `direct/test_verify_result_environment.py` uses less CPU in aws pairs, with the same tests and assertions.

**Decision:** next untouched module in the descending CPU list (show.py top 15:51: rank 16, 35.9 median CPU s, 7 tests; ranks 1-15 and 17 are done, planned or running). Slowest kept test `VerifyResultEnvironmentTests.test_result_values_distinguish_unset_empty_and_changed_values_in_saved_passes` 26.6 s.

**Spec:**
1. Profile first: `python3 ~/.claude/scripts/test_times/module_cost.py --tree $PWD direct/test_verify_result_environment.py --tests 10 --commands 10`, under `systemd-run --user --scope -q -p MemoryMax=2G --unit=<a new name>`. Name the top costs by test, fixture and command.
2. Remove repeated work a test does not check: repeated real `verify.sh` runs whose results no assertion reads, per-test setup that can be built once per class and copied, and repeated parsing. Keep the real `verify.sh` runs that the assertions read. Do not edit `scripts/direct/verify_mac_offload_test_support.py`: Phase 250 changes it and is in review. Report any cost in that shared fixture to the director.
3. If the profile names a larger cost, report it to the director before you change the plan.
4. Keep every test and assertion. Map each changed fixture step to the assertions that read its result. Register each cleanup before the step that can leave a partial folder.
5. Run the module three times and basedpyright on each changed file. Give the before and after `module_cost.py` CPU.
6. The director runs the aws pairs.

**Files:**
- `scripts/direct/test_verify_result_environment.py`

**Workers:** 1 writer; one test module.
- impl: all files.

**Constraints from prior phases:**
- Phase 232: no signal handlers anywhere.
- Phase 250: the shared verify fixture removes inherited `UNIT_*` values before it sets private values. Phase 248: a verify routing refusal costs about 2.5 CPU s of helper starts; that is a production cost with an architecture proposal, not test work. Phases 240-247: natedev gains can come from host state; only aws pairs decide a land.
- sdlc terms: test files only; every test and assertion stays; land only on a measured aws gain; report paired peak memory.

**Acceptance gate:**
- The same test count and assertions; the module passes three times.
- `module_cost.py` CPU before and after, with the top costs named.
- aws pairs (3 rounds) show a gain; peak memory reported.
