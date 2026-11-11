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
4. When no live module has a median CPU over 30 s, the plan holds and tells the showrunner; it does not close.

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
- When the jobserver is readable, `acquire` takes the token through `try_acquire` inside `build_folders.defer_signals()`. Another gate can take the token first; the loop then waits again. `try_acquire` keeps the empty-read `GateRefused` and slot ownership.
- `test_merge_gate_suites_resources.py` patches `select.select` and waits on the real `pool.blocking`. `test_stop_during_a_blocking_read_returns_every_owned_slot` has rows `before_wait`, `process` and `sender_thread` (`signal.pthread_kill` to the sender thread). `test_stop_after_blocking_acquisition_returns_the_new_slot_too` has rows `read` and `return`. Every row requires every token to return.
- Measured on copies with a 3 s guard: guard waits 11/40 alone and 1/40 under CPU load before; 0/40 and 0/40 after.

**Files:**
- `scripts/production/merge_gate.py` — `SharedCPUPool.acquire` (owner sdlc devtools-unit)
- `scripts/production/test_merge_gate_suites_resources.py` — the stop and acquire rows

**Gotchas:**
- The wakeup descriptor is set and restored inside `build_folders.defer_signals()`. `ExitStack` restores the previous wakeup descriptor before it closes the pipe.

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
- A fixture checkout that copies `conf.py` must also copy `conf_reads.py`. The Mac context-economy payload ships both.
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

### Phase 142 — The wait holder and style sync test modules use less CPU  · status: todo
**Depends on:** none

#### Work Order

**Goal:** `direct/test_verify_wait_holder.py` and `style/test_style_sync.py` use less CPU per gate, with the same tests and assertions.

**Decision:** User, 2026-11-11: "there is no minimum speed change required - anything is helpful - but i want you to always work on the worst case scenarios". Any measured saving ships. This phase takes the measured test-only candidates of Phases 137 and 140 for the first and third slowest modules.

**Spec:**
1. `scripts/direct/verify_hold_wait_test_support.py` (Phase 137 candidate B, 2.2 CPU s in one pair): the fixture environment at line 281 stops setting `CLAUDE_PYTHON` to `sys.executable`, so the real launcher uses the seeded interpreter cache. The two observer cases near line 543 keep their explicit overrides.
2. `scripts/style/test_style_sync.py` (Phase 140, 19.5 CPU s together in one pair):
   1. Run the fixture's private `address.py`, `send.py` and `roster.py` sources in process through `invoke_cli` (`scripts/production/cli_fixture.py:91`). Keep their file writes, argument reads, output and exit result. Return `CompletedProcess` results with the same check behavior. Keep the observer mocks.
   2. Route the supported style commands through `run_style_command` (`scripts/style/style_plan_test_support.py:73`). Unknown commands and Python probes keep their subprocesses.
   3. Run the fixture's synchronous scratch `cargo-save` command in process through `invoke_cli` with `scratch.main`. Keep `CARGO_BUILD_BUILD_DIR`. Driver-process scratch calls stay real.
   4. Build the initial Git repository (init, two identity settings, add, first commit) once in a class fixture seed, and copy only its `.git` into each case. Every later Git call stays real.
3. Keep every test and assertion. Signal, lock, verifier, lint driver and workspace integration processes stay real.

**Files:**
- `scripts/direct/verify_hold_wait_test_support.py`
- `scripts/style/test_style_sync.py`

**Workers:** 1 writer; the two files are small, separate changes.
- impl: both files.

**Constraints from prior phases:**
- Phase 134: `run_style_command` runs style plan commands in process; reuse it without change.
- Phase 137: the recorder and staff import change belongs to Phase 138. Do not change `progress_history.py`, `plan_history.py` or `session_staff.py` here.
- Phase 140: the research harness used a frame check to limit the Git seed to `setUp`. Use an explicit class fixture seed instead.

**State:** none.

**Acceptance gate:**
- basedpyright reports 0 errors and 0 warnings on both files.
- Both modules pass all tests.
- An aws paired trial (3 rounds, both modules) shows a summed CPU saving greater than zero.

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

#### Work Order

**Goal:** The memory admission, sweep and tmp sweep test modules lose every rare-case test that cannot show the user a wrong result, damage saved state or lose work.

**Decision:** User, 2026-11-11: "i really don't feel that all of our tests are paying their way". The user approved the Rule 2 sweep with no CPU or money bar: fewer tests is the goal. Rule 2 (user, 2026-11-07): keep a rare-case test only when its defect would show the user a wrong result, damage saved state or lose work. Every main-path test stays. Folders go by test count, then their largest modules. Owners gave the go through builds on 2026-11-11.

**Spec:**
1. For each test in the listed modules, decide main path or rare case. A main path is the behavior the tool exists for, as its users call it. A rare case is an edge, refusal, malformed input, failure path, message wording, defensive branch or internal detail.
2. Keep each main-path test. Keep a rare-case test when its defect would show the user a wrong result, damage saved state (staff rows, ledgers, plan docs, Git refs, config) or lose work. Remove each other rare-case test.
3. Typical removals: exact wording of an error or refusal in an agent-only tool; argument errors that `argparse` already rejects; a second test of a branch that another test already reaches; tests of private helpers that a main-path test already covers; guards against states that no caller can make.
4. When a removal leaves a fixture, helper, constant or import with no user, remove it too. A helper in a listed support file may go only when `grep -rn <name> scripts` finds no other user. Do not change production code.
5. Do not merge tests into table rows in this phase. Remove no test that a held speed phase changes.
6. Write `phases/148/removed_tests.md` in the unit session: one row per removed test, with module, test name, kind of rare case and the reason no user harm follows. Give the counts before and after for each module.
7. Resources rule (owner, 2026-11-11, through builds): in `test_sweep.py` keep the rows for nested, held, symlinked and overlapping Mac targets. Without them the Mac floor alert picks a wrong or held folder.

**Files:**
- `scripts/lint/test_memory_admit.py` — 375 tests.
- `scripts/lint/test_sweep.py` — 363 tests.
- `scripts/lint/test_tmp_sweep.py` — 99 tests.
- `scripts/lint/memory_test_support.py` — support; remove a helper only when nothing else uses it.

**Workers:** 1 writer; one reader keeps one standard across the modules.
- impl: the three modules and the removal table.

**Constraints from prior phases:**
- Phase 86: no test only repeats another test; line overlap does not prove a repeat. Judge by the behavior each test guards.
- Phase 87: Rule 2 removal saved no CPU in Git-heavy modules. CPU is not this phase's measure.
- Phases 145-147: blind reviews returned tests whose code a live tool still uses, though the module name pointed elsewhere. Check each removal against every caller of the code it tests. A removal can leave a helper in a support file with no user.

**State:** none.

**Acceptance gate:**
- Each listed test module passes; basedpyright reports 0 errors and 0 warnings on each edited file.
- `phases/148/removed_tests.md` lists every removed test with its reason, and the counts.
- Two blind reviews check each removal against Rule 2. A removal a reviewer shows to guard user harm goes back.

### Phase 149 — Four buildlog test modules keep only the tests that pay their way  · status: done
**Depends on:** none

#### Work Order

**Goal:** The build report, index, record and Mac idle test modules lose every rare-case test that cannot show the user a wrong result, damage saved state or lose work.

**Decision:** User, 2026-11-11: "i really don't feel that all of our tests are paying their way". The user approved the Rule 2 sweep with no CPU or money bar: fewer tests is the goal. Rule 2 (user, 2026-11-07): keep a rare-case test only when its defect would show the user a wrong result, damage saved state or lose work. Every main-path test stays. Folders go by test count, then their largest modules. Owners gave the go through builds on 2026-11-11.

**Spec:**
1. For each test in the listed modules, decide main path or rare case. A main path is the behavior the tool exists for, as its users call it. A rare case is an edge, refusal, malformed input, failure path, message wording, defensive branch or internal detail.
2. Keep each main-path test. Keep a rare-case test when its defect would show the user a wrong result, damage saved state (staff rows, ledgers, plan docs, Git refs, config) or lose work. Remove each other rare-case test.
3. Typical removals: exact wording of an error or refusal in an agent-only tool; argument errors that `argparse` already rejects; a second test of a branch that another test already reaches; tests of private helpers that a main-path test already covers; guards against states that no caller can make.
4. When a removal leaves a fixture, helper, constant or import with no user, remove it too. A helper in a listed support file may go only when `grep -rn <name> scripts` finds no other user. Do not change production code.
5. Do not merge tests into table rows in this phase. Remove no test that a held speed phase changes.
6. Write `phases/149/removed_tests.md` in the unit session: one row per removed test, with module, test name, kind of rare case and the reason no user harm follows. Give the counts before and after for each module.

**Files:**
- `scripts/buildlog/test_report.py` — 171 tests.
- `scripts/buildlog/test_index.py` — 88 tests.
- `scripts/buildlog/test_record.py` — 86 tests.
- `scripts/buildlog/test_mac_idle.py` — 69 tests.
- `scripts/buildlog/cgroup_test_support.py` — support; remove a helper only when nothing else uses it.
- `scripts/buildlog/cli_probe_test_support.py` — support; remove a helper only when nothing else uses it.
- `scripts/buildlog/mac_natedev_test_support.py` — support; remove a helper only when nothing else uses it.
- `scripts/buildlog/mac_idle_test_support.py` — support; remove a helper only when nothing else uses it.

**Workers:** 1 writer; one reader keeps one standard across the modules.
- impl: the four modules and the removal table.

**Constraints from prior phases:**
- Phase 86: no test only repeats another test; line overlap does not prove a repeat. Judge by the behavior each test guards.
- Phase 87: Rule 2 removal saved no CPU in Git-heavy modules. CPU is not this phase's measure.
- Phases 145-147: blind reviews returned tests whose code a live tool still uses, though the module name pointed elsewhere. Check each removal against every caller of the code it tests. A removal can leave a helper in a support file with no user.

**State:** none.

**Acceptance gate:**
- Each listed test module passes; basedpyright reports 0 errors and 0 warnings on each edited file.
- `phases/149/removed_tests.md` lists every removed test with its reason, and the counts.
- Two blind reviews check each removal against Rule 2. A removal a reviewer shows to guard user harm goes back.
