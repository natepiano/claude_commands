---
description: Send every unit director on both machines a message at once. Args - `<message>`.
argument-hint: "<message>"
---

`$ARGUMENTS` is the user's message. Send it as written, never rephrased, and show the output unchanged:

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --units "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every unit director on both machines; each is told that unit directors received it directly. `/showrunner:announce` reaches showrunners, `/announce` everyone.
