---
description: Send a message to all agents.
argument-hint: "<message>"
---

## Message text

Treat `$ARGUMENTS` as the user's message. Send it without changes. Show the command output without changes.

## Delivery

`~/.claude/scripts/lib/py ~/.claude/scripts/production/broadcast.py --from <your session name> --all "From the user (via <your session name>, <HH:MM zone>): <message>"`

It reaches every showrunner, unit director, Claude or Codex worker, and freestanding agent on both machines. Each is told everyone has it, so nobody passes it on.

When part of the message does not concern one role, give that role its own version after `--all`, leaving out only that part: `--showrunners "<text>"`, `--units "<text>"` or `--agents "<text>"`.

One session: `SendMessage` on this machine, `send.py --machine <other machine> --to <name>` on the other machine (/message). The user: /alert_user.
