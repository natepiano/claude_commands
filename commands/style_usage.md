---
description: Show style guide history and reporting derived from fix pipeline style runs.
---

# Style Report Summary

Show style guide history and reporting derived from fix pipeline style runs.

**Arguments**: `$ARGUMENTS` — optional flags passed to `style_report.py`

## Instructions

Run the summary script and display its output:

```bash
python3 ~/.claude/scripts/fix/style_report.py $ARGUMENTS
```

Available flags:
- `--since 30d` — filter by time window (e.g. 30d, 2w, 6m)
- `--project foo` — filter by project name
- `--latest-run` — show the latest recorded run with exact reviewed guideline outcomes

If `$ARGUMENTS` is empty, run with no flags (all projects, all time).
