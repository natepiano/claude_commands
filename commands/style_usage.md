# Style Usage Summary

Show style guide usage analytics from nightly style fix runs.

**Arguments**: `$ARGUMENTS` — optional flags passed to summary.py

## Instructions

Run the summary script and display its output:

```bash
python3 ~/.claude/scripts/nightly/style_usage_summary.py $ARGUMENTS
```

Available flags:
- `--since 30d` — filter by time window (e.g. 30d, 2w, 6m)
- `--project foo` — filter by project name
- `--local` — include repo-local styles
- `--skips` — show skip/partial reasons
- `--style foo.md` — detail view for a single style
- `--generate` — write Obsidian reports (style_report_usage.md + style_report.md)

If `$ARGUMENTS` is empty, run with no flags (all shared styles, all time).
