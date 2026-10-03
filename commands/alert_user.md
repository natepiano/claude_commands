---
description: Reach the user's phone when work has stopped on something only they can do, or a decision only they can make is overdue. Any session, natedev or Mac.
---

Send one Pushover alert:

`~/.claude/scripts/notify/pushover.py [--priority 1|2] "<session name>: <topic>" "<the one action or decision, under 200 characters>"`

On the Mac, prefix `ssh natedev`; the keys exist only on natedev. Never read `~/.config/pushover/env`.

| Priority | When |
|---|---|
| 2 (repeats until acknowledged) | Work has stopped and only the user can restart it: a login, a passphrase (`github-warmup`), a sudo step, a physical action. |
| 1 | A decision only the user can make, while other work continues. |

- One alert per block. If it is still blocked an hour after the user acknowledged, send priority 1 saying what changed, never the same text.
- Never for progress, finished work, or anything a session can decide or retry itself. Sessions with their own alert policy (`/showrunner:produce`) follow it.
- Exit 1 or 2: fall back to PushNotification.
