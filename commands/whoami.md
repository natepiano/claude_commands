---
description: Show Claude and Codex accounts, plans, and weekly quota reset times
---

Run `python3 ~/.claude/scripts/whoami/whoami.py` and relay its output. Times use the machine's local timezone. Weekly reset means quota renewal, not login expiry. Report unavailable information as unavailable; never inspect or print raw credentials.

The status line's second line shows the Claude account and its remaining 5-hour and weekly percentages, and `~/.claude/scripts/whoami/account.py` prints both Claude and Codex accounts.
