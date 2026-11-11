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
6. `hourly` shows active units and cost per unit per hour from the ledger, against the normal and busy hours.

Use `--family claude` for every precompute experiment save, threshold read, and comparison.
Use `--where KEY=JSON` on `report`, `save` or `compare`; repeat it for more settings.

Both machines cache parsed transcript events under `~/.cache/context_economy/`.
Warm reads reuse unchanged files; appended files resume at the last complete line with their saved parser state.
Append checks hash only the first 4 KiB and the last 4 KiB before the saved offset.
A failed anchor, size, device, or inode check causes a full parse.
The anchors assume append-only files and do not detect edits between the blocks.
The report applies its time window after the cache read.
Summaries survive file deletion and share one entry with the `.jsonl.zst` form.
Compressed files without a summary use the shared zstd reader once.
`cache_max_megabytes` in `config/context_economy.conf` bounds reproducible entries by least recent access.
Entries whose plain source files are compressed or deleted remain protected from eviction.
Long reads show file progress on stderr, including the Mac read.
`progress_seconds` in the same config sets the update interval.
