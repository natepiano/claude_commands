---
description: Show CI job durations for recent runs, optionally filtered by branch
---

Run `~/.claude/scripts/ci_timings.sh $ARGUMENTS` and display the output as a markdown table.

- If no arguments are provided, shows the latest run across all branches (with branch name in output).
- First argument: branch name (filters to that branch only)
- Second argument: number of recent runs to show

## Output format
- Display results as a markdown table with columns for each CI job found in the output.
- Include ALL jobs from the output — never drop columns.
- The Run column should show the short run ID as plain text (no markdown links — they don't render in the terminal).
- After the table, output the GitHub Actions URL for the repo so the user can navigate there manually.
- Jobs that didn't run in a given row should show `—`.
- Show the conclusion icon (✓/✗/⊘) alongside the duration in each job cell.
