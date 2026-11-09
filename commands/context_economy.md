---
description: Report context costs and compare saved results.
---

Use `report` when `$ARGUMENTS` is empty or starts with `--`.
Otherwise use `$ARGUMENTS` unchanged.
Run `~/.claude/scripts/lib/py ~/.claude/scripts/usage/context_economy.py <arguments>` and print its output as it arrives.

1. `report [--since TIME] [--until TIME] [--role ROLE] [--by settings|week]` shows each quantity.
2. `save NAME [--since TIME] [--until TIME]` saves a result and refuses an existing name.
3. `save --last-week` saves the previous complete ISO week and keeps an existing result.
4. `compare NAME [--since TIME] [--until TIME]` shows both sample sizes and the confounds.
5. `trend [--role ROLE]` shows saved results in time order.

Use `--family claude` for every precompute experiment save, threshold read, and comparison.
Use `--where KEY=JSON` on `report`, `save` or `compare`; repeat it for more settings.
