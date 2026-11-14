---
description: Send a message to all showrunners.
argument-hint: "<message>"
---

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --showrunners "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every showrunner on both machines. Use `broadcast.py --production <your name> --units` for your production's unit directors.
`/unit:announce` reaches all unit directors. `/announce` reaches everyone.
