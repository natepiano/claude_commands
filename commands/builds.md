---
description: Answer a question about build, lint, test and CI timings, failures and flaky tests from the build log, on both machines.
---

`$ARGUMENTS` is the question.

Empty, or only a date → run `~/.claude/scripts/buildlog/buildlog report [YYYY-MM-DD]` (default today) and show its markdown unchanged. Add at most one line after it, only for something the tables do not show.

Otherwise:
1. Run `~/.claude/scripts/buildlog/buildlog schema` once: tables, columns, views, example queries.
2. Write SQL and run `~/.claude/scripts/buildlog/buildlog query "<SQL>"` (read-only; single quotes inside). Add `--json` only when you need exact values to compute from.
3. Answer the question in a few lines, with the numbers. Per kind of step, split by caller unless asked otherwise. Mac rows arrive with natedev's hourly sync.
