---
description: Report the time verify.sh pass records saved, for any span, workspace, worktree, day or week, on both machines.
---

Run `~/.claude/scripts/delegate/verify_saved.py $ARGUMENTS` and paste its output verbatim, unfenced. It reads the build log (`buildlog query`), which natedev's hourly sync fills with the Mac's calls; the note line says how fresh they are. Options: `--from DATE --to DATE` or `--days N` (default 7), `--workspace NAME`, `--by workspace|worktree|day|week`, `--no-mac` (this machine only).
