---
description: Send every unit director on this machine a message at once. Args - `<message>`.
argument-hint: "<message>"
---

`$ARGUMENTS` is the user's message. Send it as written, never rephrased, and show the output unchanged:

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --units "From the user (via <your session name>, <HH:MM zone>): <message>"`

Each unit director is sent it at the same moment and told that no showrunner or other agent was. `/showrunner:announce` reaches showrunners, `/announce` everyone.
