---
description: Send a message to all unit directors.
argument-hint: "<message>"
---

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --units "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every unit director on both machines. For one production, put `--production <showrunner>` before `--units`.
`/showrunner:announce` reaches showrunners. `/announce` reaches everyone.
