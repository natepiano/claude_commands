---
description: Send a message to all showrunners.
argument-hint: "<message>"
---

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --showrunners "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every showrunner on both machines; each is told that showrunners received it directly. `/unit:announce` reaches unit directors, `/announce` everyone.
