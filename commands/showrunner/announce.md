---
description: Send every showrunner on this machine a message at once. Args - `<message>`.
argument-hint: "<message>"
---

`$ARGUMENTS` is the user's message. Send it as written, never rephrased, and show the output unchanged:

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --showrunners "From the user (via <your session name>, <HH:MM zone>): <message>"`

Each showrunner is sent it at the same moment and told that no unit director or other agent was. `/unit:announce` reaches unit directors, `/announce` everyone.
