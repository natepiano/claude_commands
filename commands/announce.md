---
description: Send every showrunner, unit director and other running agent on this machine a message at once. Args - `<message>`.
argument-hint: "<message>"
---

`$ARGUMENTS` is the user's message. Send it as written, never rephrased, and show the output unchanged:

`python3 ~/.claude/scripts/production/broadcast.py --from <your session name> --all "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every showrunner, every unit director, and every other agent: other Claude sessions, Claude seats and running Codex seats. Each is told everyone has it, so nobody passes it on.

When part of the message does not concern one role, give that role its own version after `--all`, leaving out only that part: `--showrunners "<text>"`, `--units "<text>"` or `--agents "<text>"`.

One session: `send.py --to <name>` (/message). The user: /alert_user.
