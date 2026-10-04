---
description: Show or change this Claude unit's progress update interval.
argument-hint: "[minutes]"
---

# Unit interval

**Usage:** `/unit:interval [minutes]`

## Steps

1. Read `CLAUDE_CODE_SESSION_ID`. A Codex unit has no notifier instance; say
   this command is unavailable there. If the ID or its marker is missing, say
   no Claude unit instance is available.
2. Read the path in
   `/tmp/claude/delegate/active/$CLAUDE_CODE_SESSION_ID`; its basename is the
   run id. The instance is `delegate-<run id>`.
3. With no argument, run
   `zsh ~/.claude/scripts/message/notifier.sh status delegate-<run id>`.
   With one positive integer argument, run
   `zsh ~/.claude/scripts/message/notifier.sh interval delegate-<run id> <minutes>`.
   Exit 2 means the change was refused; report the error without claiming a
   new interval. Reject other argument shapes.

## Answer

Tell the user the current interval, or the changed interval and the next tick
from the printed `next_due` line. Keep the displayed time in the machine's
local zone.
