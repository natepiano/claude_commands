---
description: Reach the user when work has stopped on something only they can do, or a decision only they can make is overdue. Any session, natedev or Mac.
---

Send one alert:

`python3 ~/.claude/scripts/message/send.py --to user --need decision|blocked --summary "<session name>: <topic>" --text "<the one action or decision, under 200 characters>"`

On the Mac, add `--machine natedev`; the keys exist only on natedev. Never read `~/.config/pushover/env`.

| Need | When |
|---|---|
| `blocked` (repeats until acknowledged) | Work has stopped and only the user can restart it: a login, a passphrase (`github-warmup`), a sudo step, a physical action. |
| `decision` | A decision only the user can make, while other work continues. |

- One alert per block. If it is still blocked an hour after the user acknowledged, send `decision` saying what changed, never the same text.
- Never for progress, finished work, or anything a session can decide or retry itself. Sessions with their own alert policy (`/showrunner:produce`) follow it.
- `FAILED` (exit 3): fall back to PushNotification.
