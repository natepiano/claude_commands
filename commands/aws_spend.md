---
description: Show AWS spend per day with analysis.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/buildlog/aws_spend.py` and show its lines unchanged.

The command reads only the local cost ledger. It makes no AWS call and takes a few seconds.
Each gate run updates the ledger, so the estimate for today is current to the last run.
Completed days show the AWS bill. Today shows an estimate until AWS bills it the next day.
The output shows the month to date, the month forecast against the monthly limit, and the cost per run for each machine type.
