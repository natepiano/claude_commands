---
description: Show or change what happens next when an account's weekly usage runs low.
---

Translate $ARGUMENTS into `python3 ~/.claude/scripts/accounts/accounts.py next`:

- “when units reach 1% switch to claude2” → `add units 1 claude2`.
- “when workers reach 1% switch to codex2” → `add workers 1 codex2`; `claudeN` and bare `claude` are also workers targets. A numbered Claude target sets the account for worker launches; bare `claude` keeps the caller or showrunners account. Subagents inside a Claude session use that session’s account.
- For units or showrunners, “hold for half a point” adds `--hold half-point`; a fixed duration adds `--hold-minutes N`.
- “what's set up” → `list`.
- “drop units 2” → `remove units 2`.
- “swap showrunners 1 and 2” → `move showrunners 1 2`; `move GROUP N M` moves position N to M.
- Insert at a position with `add GROUP PERCENT TARGET --at N`; omission appends.
- Change a hold with `hold GROUP N half-point|MINUTES --expect PERCENT TARGET`, copying the listed percent and target immediately after `list`. A different mode clears the started hold; the same mode keeps it, including a retry after a lost reply. A refusal means re-list; never recreate a consumed entry or undo an update with another `hold`.
- Positions start at 1 within each group; never accept or show internal ids.

Workers entries cannot have either hold mode. A workers move to Claude holds there until the source account’s weekly reset. A half-point hold fixes its minutes at its first low reading using the selected account’s pace history, or 40 minutes without usable pace. Both machines show natedev’s stored minutes, source and rounded-up time left once started (or `hold over`); unstarted entries show `hold half a point (≈30 min)`, `hold half a point (40 min default: no pace history)` or `hold 30 min`. Started rows read `hold half a point (34 min at 0.9 points/hour, 12 min left)`, `hold half a point (40 min default: no pace history, 12 min left)` or `hold 30 min (12 min left)`.

Print the list after every change. For units, showrunners and workers, only the first entry per group can run; queued entries block fallback moves, and a recorded move removes its entry so the next becomes eligible on a later pass (starting its hold for Claude groups). A due head whose target is already current or blocked by login, reading, allowance, refusal or preparation stays queued and tells showrunners once per changed reason; shutdown waits silently, and completing members onto the selected account still runs. Queued Claude groups wait for any known refusal reset and continue on the current account afterward (unknown resets wait for the head); queued workers keep their brief reset wait. Reads and changes on the Mac go to natedev. See `docs/accounts.md`.
