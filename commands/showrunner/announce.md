---
description: Send every showrunner on both machines a message at once. Args - `<message>`.
argument-hint: "<message>"
---

`$ARGUMENTS` is the user's message. Send it as written, never rephrased, and show the output unchanged:

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --showrunners "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every showrunner on both machines; each is told that showrunners received it directly. `/unit:announce` reaches unit directors, `/announce` everyone.
