---
description: Show suite, module and test times.
---

Run `python3 ~/.claude/scripts/test_times/show.py $ARGUMENTS` and show its lines unchanged.

- `top [--limit N] [--repo PATH]`: rank live modules by median wall time. This is the default command.
- `suites [--days N]`: show recent gate runs.
- `suite <folder> [--days N]`: show one folder suite's history.
- `module <folder>/<file> [--runs N]`: show each module execution and its slowest kept tests.
- `test <text> [--runs N]`: show kept test names that contain the text and later missing measurements.
