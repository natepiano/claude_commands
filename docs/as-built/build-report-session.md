# Build cost analysis

The user retired `/build:report` on 2026-11-03. The command file is removed.
Run `buildlog costs [--days N]` on demand to rank build costs and show phase time.
The default window is seven days. The command reads the index and progress history without changing them.

The round record is [builds-report-rounds.md](../plans/builds-report-rounds.md).

Use the `buildlog` CLI for detail. `buildlog report`, the index, and all collectors remain available.
No notifier schedules a build report.
