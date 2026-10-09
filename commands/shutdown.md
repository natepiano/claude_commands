---
description: Shut down every Claude session of one account on natedev and the Mac safely and restart them later; show what runs on an account.
argument-hint: "status [account]"
---

`$ARGUMENTS` is `status [account]`. Run the matching line and show its output unchanged. This phase only reports; it changes no session, timer, or Codex server.

```
/shutdown status             show what runs for this session's Claude account on both machines
/shutdown status <account>   show what runs for a note label or login on both machines
```

Run `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py status` with the optional account argument exactly as the user gave it. Do not add `--here`: status covers natedev and the Mac, and reports an unreachable machine without failing the local report.
