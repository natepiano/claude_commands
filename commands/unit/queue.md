---
description: Set the unit's queued work outside its plan.
argument-hint: "add <text> | done | clear | show"
---

# Queue

Use this in the unit director session. The script finds your unit from your staff row.
The list holds work that comes after the current work outside the plan.
The showrunner sees its first item and the count of later items in `/showrunner:units`.
The queue only stores this list and wakes nobody; for a timed action, use a scheduled wakeup.
Queued also shows unfinished plan phases, excluding done and open phases.

## Steps

1. Run the script for the requested action:
   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/unit_queue.py add --text "<item>"
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/unit_queue.py done
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/unit_queue.py clear
   ~/.claude/scripts/lib/py ~/.claude/scripts/production/unit_queue.py show
   ```
   `add` appends an item. `done` removes the first item. `clear` removes all items.
   `show` reads the list. Each action prints the remaining list in order.
2. Print the result. Report a non-zero exit as one error line.

The script stores the list at `~/.local/state/unit-queue/<staff id>.json`.
`UNIT_QUEUE_STATE_DIR` sets a different folder.
