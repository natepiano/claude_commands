---
description: Show suite, module and test times.
---

Run `python3 ~/.claude/scripts/test_times/show.py $ARGUMENTS` and show its lines unchanged.

- `top [--limit N] [--repo PATH] [--host NAME]`: rank the slowest tests of the working repository, or of PATH. This is the default command.
- `suites [--days N]`: show recent gate runs.
- `suite <folder> [--days N]`: show one folder suite's history.
- `module <folder>/<file> [--runs N]`: show each module execution and its slowest kept tests.
- `test <text> [--runs N]`: show kept test names that contain the text and later missing measurements.

In the ~/.claude repository, `top` ranks live gate modules by median CPU time.
In another repository, `top` ranks recorded nextest tests on the current host within `TOP_MAX_AGE_DAYS`.
The repository key follows its origin, so worktrees share the same test history.
Each list holds the records from the last complete index update.
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
