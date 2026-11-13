---
description: Show AWS Spot prices and quotas.
---

Run `python3 ~/.claude/scripts/aws_target/deploy.py spot-table` and show its lines unchanged.

The command reads only the local hourly snapshot. It makes no AWS call.
The table shows the lowest zone Spot price in USD/h for each type and region, and the snapshot age.
Under the table, each region shows its Standard and G and VT Spot quotas in vCPUs and the box count of each type that fits.
`buildlog hourly` refreshes the snapshot with `aws_target.py snapshot`.
A `-` cell means no price or quota is in the snapshot.
