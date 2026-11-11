---
description: Show suite, module and test times.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/test_times/show.py $ARGUMENTS` and show its lines unchanged.

- `top [--limit N] [--repo PATH] [--host NAME]`: rank the slowest tests of the working repository, or of PATH. This is the default command.
- `trend [--days N]`: show daily gate CPU, wall time and cost. The default is `TREND_DAYS`.
- `suites [--days N]`: show recent gate runs.
- `suite <folder> [--days N]`: show one folder suite's history.
- `module <folder>/<file> [--runs N]`: show each module execution and its slowest kept tests.
- `test <text> [--runs N]`: show kept test names that contain the text and later missing measurements.

In the ~/.claude repository, `top` ranks live gate modules by median CPU time.
The nightly type check has a footnote with its latest passed CPU and wall time. It does not count toward the limit.
In another repository, `top` ranks recorded nextest tests on the current host within `TOP_MAX_AGE_DAYS`.
The repository key follows its origin, so worktrees share the same test history.
Each list holds the records from the last complete index update.
The `rows as of` time names the last update that changed rows.
An unchanged refresh keeps that time.
Stderr shows `rows as of HH:MM ZONE` for today in the user's zone.
For another day, it shows `rows as of MM-DD HH:MM ZONE`.
The report refreshes the index after it prints.
A newer gate record appears on a later run after that refresh ends.
The first list after a gate can omit that gate.
When no complete compatible index exists, the report builds an index before it prints.
Use `--host NAME` to select another host. The default is the current host. The heading names the selected host.

The nextest table shows the median of passed runs, including successful retries.
It also shows retained run counts, failed and timed-out run counts, and the latest duration.
Tests with only failed runs follow passed tests, ordered by their latest duration.
The heading names the repository, host and recording threshold.
The recorder retains failures, retries, SLOW results and tests above `NEXTEST_SLOW_TEST_S`.

Run `python3 ~/.claude/scripts/test_times/module_cost.py --tree <path> <folder>/<file> [<folder>/<file> ...]` for module costs.
It reports CPU seconds, process starts, successful execs, wall seconds and test status.
It runs each module once for time and once under `strace` for counts.

Use `--tests N` to show the N tests or fixtures with the most CPU in each module.
These rows come from the time run.
Test CPU includes the runner's CPU and the CPU of children reaped during that test.
A separate fixture row measures each module or class fixture and its cleanups.
Fixture measures do not change `testsRun`, `tests_s`, or the existing outcome rows.
The supervisor reaps orphans after the tests end.
Their CPU does not belong to a test row.
Both runs use `module_run.py`, so each process count includes one extra supervisor process.
This changes the process baseline by one process and no execs.

Use `--commands N` to add a run that measures CPU at kernel task exit events on Linux.
The command table groups tasks by the last program.
For Python and shells, the group also names the script or Python module.
A task without an exec keeps its parent's command.
The table counts process starts and includes each thread's CPU.
`unassigned` shows the run's child CPU less the CPU of all command rows, including rows below the limit.
The number keeps its sign.
These command measures come from a separate run, so they can differ from the time run.

`trend` groups passing, non-aborted runs by place, selection and day in the user's zone.
It prints one table for each place and selection.
Columns: day, runs, CPU mean, CPU p95, wall mean, wall p95, cost mean, cost p95, cost total.
Targeted (`affected`) tables add median modules run.
CPU includes all module attempts. Wall time comes from the gate run.
The p95 uses nearest rank. Seconds have no decimals. USD has three decimals.
AWS cost joins `gate_runs.id = "gate-" + GateReservation.attempt` in the local cost ledger.
Cost includes the reservation lifetime at its applied hourly rate, plus its allowance.
A matching reservation identifies an AWS run even when its host has a custom name.
Cost statistics use only runs with a confirmed lifetime and an applied rate.
An unpriced reservation has no cost record.
AWS tables report missing cost counts below the table. A group with no cost records shows `—`.
Other hosts show `—`. Missing cost records do not delay caching.
`gate-trend.json` beside the gate index caches finished days.
A day finishes after a complete refresh starts `TREND_SETTLE_MINUTES` after its end (default 60).
The cache records that start after the refresh completes. An exclusive lock protects each cache update.
Later runs compute only open days. A missing, unreadable or incompatible cache is rebuilt.
