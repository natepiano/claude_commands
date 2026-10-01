---
description: Report the time verify.sh pass records saved, for any span, workspace, worktree, day or week, on both machines.
---

Run `~/.claude/scripts/delegate/verify_saved.py $ARGUMENTS` with `dangerouslyDisableSandbox: true` (it reads the Mac's ledger over ssh) and paste its output verbatim, unfenced. Options: `--from DATE --to DATE` or `--days N` (default 7), `--workspace NAME`, `--by workspace|worktree|day|week`, `--no-mac`.
