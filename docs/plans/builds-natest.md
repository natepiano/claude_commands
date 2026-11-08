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
  - `scripts/lib/conf.py` — `read(file).number(key)`; a config file puts a `# range: 0..` or `# whole: 1..` line directly above each `KEY=value` (see `config/mesh_runs.conf`)
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
  0. Targets: at dispatch, the director takes the top live rows of `show.py top`, ranked by median CPU per gate, that no phase holds. When they differ from the modules the phase names, the director rewrites its Goal, Files and acceptance first. `types/basedpyright` is never a target; it goes to its architecture proposal. `agents/test_codex_mesh.py` is excluded: it retires.
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

- `scripts/lib/inotify.py` is the one binding outside `codex_mesh.py`. It holds `IN_MODIFY` and `read_events(descriptor)`, which does one read; `events` loops on it.
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

**Ruled out:** A write watch on by default wakes on each write in a busy directory, and predicates can read half-written files. `codex_mesh.py` keeps its own copy, because it goes.

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

- `invoke_assignment` in `assign_launcher_test_support.py` runs the real coordinator in process. It calls the recorder, `workers.py` and `codex_mesh` in process, and patches `require_live_director` and `Dispatch.follow`. Forty tests use it.
- `snapshot_files` and `changed_files` hold the first-dispatch snapshot. `FirstDispatchFixture.first_assignment` and `AssignClaudeLauncherFixture.first_worker_launch` both use them, and each keeps its own restoration.
- The process tests that stay real cover the ancestor check, launcher ownership, worker failure and concurrent review stages.

**Files:**
- `scripts/direct/assign_launcher_test_support.py` — the assign fixture, `invoke_assignment` and the snapshot helpers.
- `scripts/direct/assign_claude_test_support.py` — the Claude launcher fixture.
- `scripts/direct/test_assign_launcher.py`, `test_assign_launcher_reuse.py`, `test_assign_launcher_identity.py`, `test_assign_launcher_review.py` — the four test files.

**Gotchas:**
- `AssignLauncherFixture` inherits `MeshCommandFixture` and sets the child `TMPDIR` to `/tmp`, so `/dev/shm` pairs do not reach it. Pairs run on `/tmp` at the same time.
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

- `open_for_update()` uses the rollback journal and sets `PRAGMA cache_spill` from `INDEX_SPILL_PAGES=65536` in `config/buildlog.conf`. A transaction up to about 256 MiB of changed pages takes the exclusive lock only at commit.
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

**Gotchas:** `cache_spill` is not a hard memory limit. A refresh above the spill size blocks readers from the first spill until commit, up to the 30 s wait. Memory for a full build at live scale is not measured.

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
- `RemoteEntryTests.setUp` sets `UNIT_ACTIVE_DIR` inside the test root before `warm_claim_servers()`, so admission reads no live active marker.

**Files:**
- `scripts/copies/test_review_remote_copies.py` — the module

**Gotchas:** an inherited `UNIT_RUN_DIR` moves the active folder outside the test root unless `UNIT_ACTIVE_DIR` is set.

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

#### Work Order

**Source:** the Phase 79 architect review add-on, settled "current, after 67 and 68" by the showrunner on 2026-11-07.

**Goal:** `style/test_style_ledger_tickets.py` (44.9 CPU s per gate on 2026-11-07, 195 tests) and `style/test_style_ledger_findings.py` (43.8 CPU s, 139 tests) use half their CPU, or a measured floor shows why they cannot, with every checked behavior kept.

**Spec:**
1. Follow the Speed method. Measure CPU with `module_cost.py --tests 10 --commands 10` first. Both modules spend their time in tests, not in imports: about 0.3 s for each test.
2. Find what each test pays for: the `LedgerTestCase` setup, a Git repository, or a process start. Share an expensive seed per class and copy it for each test where the test changes it.
3. These modules have many tests. Run the trivial-test sweep from the Speed method: tests with the same setup and the same assertion target, then `coverage_overlap.py`.
4. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/style/test_style_ledger_tickets.py`
- `scripts/style/test_style_ledger_findings.py`

**Workers:** 2 writers; each module is its own file set
- `impl` — `test_style_ledger_tickets.py`, role impl
- `test` — `test_style_ledger_findings.py`, opens as impl

**Constraints from prior phases:**
- `scripts/style/ledger_test_support.py` and `scripts/production/staff_test_support.py` are not natest's to edit (standing rule). A gain that needs a change there goes to the director as an architecture proposal with its measured gain.
- Phase 81: a fixture that commits in a tempdir repository sets `maintenance.auto false` before its first commit.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- The behavior map has a row for each assertion. Each removed test names its covering test or its reason.
- CPU s, processes, execs and wall s before and after, from `module_cost.py` and same-load pairs, in the checkpoint notice.
- Each changed file passes.
- the targeted test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`

### Phase 86 — We know which tests to remove, merge or move to streamline the suite  · status: todo
**Depends on:** none

#### Work Order

**Source:** the user, 2026-11-07: "18,800 seems excessive for the amount of actual code we have", then "yes i want you to evalutae how we can streamline our testing". The gate runs 733 modules and 18,777 test methods. The test code is about 1.5 times the size of the production code.

**Goal:** a ranked report says which tests to remove, merge or move, with the test count and the CPU each action saves. The suite changes in later phases, not in this one.

**Spec:**
1. Change no test and no production file. Write only the report files below.
2. For each module in your folders, read the tests and sort each one that can go into one class:
   - **Retired:** it tests code that is gone or retiring (for example `agents/codex_mesh.py`).
   - **Trivial:** it asserts a constant, the standard library, a configured mock, or only that a thing exists (Invariants).
   - **Repeat:** another test in the same module checks the same result from the same setup.
   - **Cross-module repeat:** another module checks the same behavior through another entry point. Name the test that stays.
   - **Table:** several tests have the same body and differ only in input. They become one table test with `subTest`, which keeps every case and pays the setup once.
   - **Wording:** it pins message text and no behavior. List these separately; the user decides on them.
3. Use `python3 scripts/test_times/coverage_overlap.py scripts/<folder> <file>` only for modules with 100 or more tests or 20 or more CPU s per gate. Run it as one detached run per folder, one at a time: natedev is shared.
4. Read CPU per gate with `python3 scripts/test_times/show.py top --limit 800` and test times with `show.py module <folder>/<file>`.
5. For each candidate, name the test that keeps the behavior, or the reason no test needs to keep it. A test with no covering test and a real behavior stays.
6. Rank the actions by CPU s saved per gate, then by tests removed. For each folder, give the test count before and after, and the owner unit from the production doc.
7. Name the patterns that make the suite grow (for example one test per error message, one test per config key), and one rule for each that stops the growth. A rule is a proposal for the user, not a new check.

**Files:**
- `docs/plans/builds-natest-streamline-a.md` — the report for `production`, `direct`, `agents`, `lint`, `message`, `hooks`, `lib`
- `docs/plans/builds-natest-streamline-b.md` — the report for every other folder under `scripts/`

**Workers:** 2 writers; split by folder
- `eval1` — `docs/plans/builds-natest-streamline-a.md`, role impl
- `eval2` — `docs/plans/builds-natest-streamline-b.md`, role impl

**Constraints from prior phases:**
- Phase 13: five modules whose time is in process starts held no repeat. Spend the reading on modules with many tests.
- Phase 89 holds `direct/test_assign_launcher.py` and `direct/assign_launcher_test_support.py`. Phase 88 holds `production/test_fast_track.py`, `production/test_staff.py` and `production/test_add_unit.py`. Phase 90 holds the `style/` modules that import `ledger_test_support.py`. Report candidates in them, and mark them as held.
- Phase 87 removed rare-case tests from `production/test_fast_track.py` and `production/test_staff.py`. Reconcile each candidate in those modules against that removal.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- Each report covers every module in its folders, with a count by class.
- Each candidate names its covering test or its reason.
- The director checks ten candidates from each report against the code.

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

### Phase 88 — The fast-track, staff and add-unit tests build their temporary Git repository once per module  · status: todo
**Depends on:** none

#### Work Order

**Source:** the showrunner, 2026-11-07, on the Phase 87 result: "Add a phase after 85 that makes that setup once per module (one shared fixture repo, copied per test where a test writes), for test_fast_track, test_staff and add_unit first. Measure CPU before and after."

**Goal:** `production/test_fast_track.py` (57.5 CPU s same-load, 52 tests), `production/test_staff.py` (28.6 CPU s, 128 tests) and `production/test_add_unit.py` (64 tests) do their Git setup once per class, and use half their CPU, or a measured floor shows why they cannot, with every checked behavior kept.

**Spec:**
1. Follow the Speed method. Measure CPU with `module_cost.py --tests 10 --commands 10` first. Name the share of each module's CPU that Git setup, per-test copies and process starts take.
2. Two modules already have a class seed. `test_fast_track.py` uses `MergeCheckpointFixture` (`merge_checkpoint_test_support.py:255`), which builds a seed and copies it. `test_add_unit.py` uses `AddUnitTestCase` with `ImmutableGitSeed` (`add_unit_test_support.py:218`). Start from the measured remaining cost in these fixtures, not from a new seed.
3. `test_staff.py` builds repositories in `DocumentLookupTests.checkouts` (`test_staff.py:409`). Build them once per class where the tests only read them.
4. A test that writes to a shared repository gets its own copy, made by a file copy, not by a new `git init` and commits.
5. A shared fixture sets `maintenance.auto false` before its first commit (Phase 81).
6. Read the importers of a shared fixture before you change it. Every importer stays green.
7. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/production/test_fast_track.py`
- `scripts/production/test_staff.py`
- `scripts/production/test_add_unit.py`
- `scripts/production/merge_checkpoint_test_support.py`
- `scripts/production/add_unit_test_support.py`

**Workers:** 2 writers; split by module and its fixture
- `impl` — `test_fast_track.py` and `test_add_unit.py`, hub: `merge_checkpoint_test_support.py`, `add_unit_test_support.py`
- `test` — `test_staff.py`, opens as impl

**Constraints from prior phases:**
- Phase 87: rule 2 removal gave no CPU gain in these modules. The cost is the temporary Git repository that each test builds.
- `scripts/production/staff_test_support.py` is not natest's to edit (standing rule). A gain that needs a change there goes to the director as an architecture proposal with its measured gain.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- The behavior map has a row for each assertion. No assertion is removed.
- CPU s, processes, execs and wall s before and after, from `module_cost.py` and same-load pairs, in the checkpoint notice.
- Each changed file and every importer of a changed fixture pass.
- the targeted test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`

### Phase 89 — The main assign launcher test file uses half its CPU  · status: todo
**Depends on:** Phase 68
**Blocked by:** hierarchies-unit Phase 30 on `origin/builds` — it deletes `agents/codex_mesh.py` and `agents/test_codex_mesh.py`, which this phase's fixture imports. Merge `origin/builds` at phase start.

#### Work Order

**Source:** Phase 68 contract review (F001). Phase 68 same-load pairs cut the four assign launcher files from 482.4 to 303.6 CPU s (37%), short of half. The director split the rest of the work into this phase, so the Phase 68 gain lands now.

**Goal:** `scripts/direct/test_assign_launcher.py` (61 tests, 214.8 CPU s same-load median after Phase 68, 286.6 before) uses half its Phase 68 CPU, or a measured floor shows why it cannot, with every checked behavior kept.

**Spec:**
1. Follow the Speed method. Measure CPU with `module_cost.py --tests 10 --commands 10` first. Name each test's CPU, and each real launcher process that it starts.
2. Move each test that does not test the process boundary to `InProcessAssignment`, as Phase 68 did. Keep the process tests that the Phase 68 behavior map names: ancestor check, launcher ownership, worker failure and concurrent review stages.
3. Measure the recorder and resolver children (58.6 CPU s in the Phase 68 command profile). If only a production API change can cut them, write the floor and send the director an architecture proposal with its measured gain.
4. Read the importers of a changed fixture before you change it. Every importer stays green.
5. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/direct/test_assign_launcher.py`
- `scripts/direct/assign_launcher_test_support.py`

**Workers:** 1 writer; one test file and its fixture do not split
- `impl` — `test_assign_launcher.py`, hub: `assign_launcher_test_support.py`

**Constraints from prior phases:**
- Phase 68: `invoke_assignment` in `assign_launcher_test_support.py` runs the real coordinator in process and calls the recorder, `workers.py` and `codex_mesh` in process. `snapshot_files` and `changed_files` share the first-dispatch snapshot.
- Phase 68 behavior map: `/tmp/claude/unit/2113de4f-9913-485a-9a51-61558bfd81db/phases/68/profile/behavior_map.md`.
- Before hierarchies Phase 30, `AssignLauncherFixture` inherits `MeshCommandFixture` from `agents/test_codex_mesh.py`, and `invoke_assignment` routes `codex_mesh` calls in process (`assign_launcher_test_support.py:15`). After Phase 30, the fixture and that route go. Measure the new baseline with `module_cost.py` on the merged tree before any change. The 214.8 CPU s figure is the pre-deletion baseline.
- Phase 68: `MeshCommandFixture.setUp` uses `dir="/tmp"` and `AssignLauncherFixture.setUp` sets the child `TMPDIR` to `/tmp`, so `/dev/shm` pairs do not reach the fixture. Run each pair on `/tmp` at the same time, and state that in the result. If the deletion moves the fixture root, state the new filesystem basis.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- The behavior map has a row for each assertion. No assertion is removed.
- CPU s, processes, execs and wall s before and after, from `module_cost.py` and same-load pairs, in the checkpoint notice.
- The changed file and every importer of a changed fixture pass.
- the targeted test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`

### Phase 90 — The style ledger tests seed staff once per class and close every database  · status: todo
**Depends on:** Phase 85

#### Work Order

**Source:** the showrunner, 2026-11-07: "The open SQLite connection in LedgerTestCase.judgment_rows (ledger_test_support) is test work, so it is yours: add it as a small item and fix it at the source." The Phase 85 reviews (F001-F003) route the class-wide staff seed here, because its cause is in the same file.

**Goal:** `LedgerTestCase` seeds its staff record once per class and closes every SQLite connection it opens, and the two Phase 85 modules drop their own copies of the seed.

**Spec:**
1. `LedgerTestCase.judgment_rows` (`ledger_test_support.py:323`) uses `with sqlite3.connect(database) as connection`, which commits but does not close. Use `closing(...)` like the other helpers in the file.
2. `LedgerTestCase.setUp` (`ledger_test_support.py:240`) runs `style_staff_fixture` for each test. Run it once in `setUpClass` against the template repository, and give each test a copy with its own paths.
3. Rewrite paths in the copied staff record by decoding the JSON, changing the path fields, and serializing it again. A literal text replace misses an escaped path, for example a non-ASCII `TMPDIR` (Phase 85 F001).
4. Set `maintenance.auto false` in the template repository before its first commit, in one place (Phase 81). Remove the two idioms in the Phase 85 modules (F003).
5. Remove the `setUpClass`, `copy_staff_seed` and `copied_staff_fixture` overrides and the `git` override from `test_style_ledger_tickets.py` and `test_style_ledger_findings.py` (F002).
6. Every importer of `ledger_test_support.py` stays green: `test_style_ledger.py`, `test_style_ledger_tickets.py`, `test_style_ledger_findings.py`, `test_style_ledger_corrections.py`, `test_style_ledger_sites.py`, `test_refill_speed.py`.
7. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/style/ledger_test_support.py`
- `scripts/style/test_style_ledger_tickets.py`
- `scripts/style/test_style_ledger_findings.py`

**Workers:** 1 writer; the shared fixture is the hub and the two module edits are removals
- `impl` — `test_style_ledger_tickets.py`, `test_style_ledger_findings.py`, hub: `ledger_test_support.py`

**Constraints from prior phases:**
- The showrunner gave natest `ledger_test_support.py` for this phase on 2026-11-07. `scripts/production/staff_test_support.py` stays outside natest.
- Phase 85: the per-class seed cut the tickets module from 45.4 to 22.7 CPU s and the findings module from 44.0 to 22.5 CPU s (same-load pairs on `/dev/shm`). The shared version must keep those gains.
- Phase 85: the seed run writes `.git/style-production.md` into the template repository before each per-test `copytree`, so each copy holds the document that the staff `LiveDoc` row names.
- Tests never read or write live state: `~/.claude` main, `~/.local/state/merge-gate`, `/tmp/claude`.

**Acceptance gate:**
- No assertion is removed.
- CPU s before and after for all six importers, from `module_cost.py` and same-load pairs, in the checkpoint notice.
- Every importer of `ledger_test_support.py` passes, and runs with no `ResourceWarning` for an unclosed database.
- the targeted test green on the phase commit merged with `origin/builds`
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`
