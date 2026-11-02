---
description: Send a message to all unit directors.
argument-hint: "<message>"
---

Read `~/.claude/commands/announce.md` → Message text.

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --units "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every unit director on both machines; each is told that unit directors received it directly. `/showrunner:announce` reaches showrunners, `/announce` everyone.
