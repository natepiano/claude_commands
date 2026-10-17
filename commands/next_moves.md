---
description: Show or change what happens next when an account's weekly usage runs low.
---

Translate $ARGUMENTS into `python3 ~/.claude/scripts/accounts/accounts.py next`:

- “when units reach 1% switch to claude2” → `add units 1 claude2`.
- “when workers reach 1% switch to codex2” → `add workers 1 codex2`; `claude` is also a workers target.
- For units or showrunners, a requested hold adds `--hold-minutes N`.
- “what's set up” → `list`.
- “drop units 2” → `remove units 2`.
- “swap showrunners 1 and 2” → `move showrunners 1 2`; `move GROUP N M` moves position N to M.
- Insert at a position with `add GROUP PERCENT TARGET --at N`; omission appends.
- Positions start at 1 within each group; never accept or show internal ids.

Workers entries cannot have a minute hold. A workers move to Claude holds there until the source account’s weekly reset.

Print the list after every change. For units, showrunners and workers, only the first entry per group can run; queued entries block fallback moves, and a recorded move removes its entry so the next becomes eligible on a later pass (starting its hold for Claude groups). A due head whose target is already current or blocked by login, reading, allowance, refusal or preparation stays queued and tells showrunners once per changed reason; shutdown waits silently, and completing members onto the selected account still runs. Queued Claude groups wait for any known refusal reset and continue on the current account afterward (unknown resets wait for the head); queued workers keep their brief reset wait. Reads and changes on the Mac go to natedev. See `docs/accounts.md`.
