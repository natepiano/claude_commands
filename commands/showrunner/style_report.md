---
description: Show style coverage, held changes and pending hand-offs
argument-hint: "[rule|crate]"
---

Run in the style showrunner or any showrunner above it. Find your production
with `showrunners.py` (`~/.claude/scripts/production/`); inspect its on-disk doc
for `**Kind:** style`. Otherwise walk `showrunners.py children <slug>` recursively
and inspect each child's doc from `showrunners.py list`. Report every style
production found, naming each when there is more than one. With none, say so.

Read `**Showrunner checkout:**` from each style production doc and run:

```sh
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/style/coverage_report.py" --repo <style checkout> [--rule <rule id or stem> | --crate <crate directory or package name>]
```

Without an argument, show the full report. Resolve an argument against the
production's rule ids/stems and crate directories/package names; use the matching
flag. If both match, ask which view the user wants. Print the script's output
unchanged; on failure, report its error. It reads coverage and saves only the
last-report comparison beside the ledger. It never starts style work or presents
proposal bodies; use `/showrunner:proposals` for those.
