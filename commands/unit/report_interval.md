---
description: Show or change this Claude unit's progress update interval.
argument-hint: "[minutes]"
---

# Unit report interval

**Usage:** `/unit:report_interval [minutes]`

## Steps

1. Read `CLAUDE_CODE_SESSION_ID`. A Codex unit has no notifier instance; say
   this command is unavailable there. If the ID or its marker is missing, say
   no Claude unit instance is available.
2. Read the path in `/tmp/claude/unit/active/$CLAUDE_CODE_SESSION_ID`, or in
   `/tmp/claude/delegate/active/$CLAUDE_CODE_SESSION_ID` for a run started
   before the folder rename; its basename is the run id. The instance is
   `unit-<run id>`, or `delegate-<run id>` when the path is under
   `/tmp/claude/delegate/`.
3. With no argument, run
   `zsh ~/.claude/scripts/message/notifier.sh status <instance>`.
   With one positive integer argument, run
   `zsh ~/.claude/scripts/message/notifier.sh interval <instance> <minutes>`.
   Exit 2 means the change was refused; report the error without claiming a
   new interval. Reject other argument shapes.

## Answer

Tell the user the current interval, or the changed interval and the next tick
from the printed `next_due` line. Its time is in the user's zone.
