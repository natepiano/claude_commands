---
description: Reach the user when work has stopped on something only they can do, or a decision only they can make is overdue. Any session, natedev or Mac.
---

Send one alert:

`python3 ~/.claude/scripts/message/send.py --to user --need decision|blocked --action "<what the user does>" --summary "<session name>: <topic>" --text "<the one action or decision, under 200 characters>"`

On the Mac, add `--machine natedev`; the keys exist only on natedev. Never read `~/.config/pushover/env`.

News a terminal already shows, sent only when the user types in no terminal for N minutes (natedev): `python3 ~/.claude/scripts/message/escalate.py hold <key> --need note --no-action --summary "<title>" --text "<news>" [--minutes 15]`, then `close <key>` once it stops being true.

Every message says what the user does with `--action`, or says nothing is needed with `--no-action`. Emergency (`--need blocked`) is only for an action the user must take now.

| Need | When |
|---|---|
| `blocked` (repeats until acknowledged) | Work has stopped and only the user can restart it: a login, a passphrase (`github-warmup`), a sudo step, a physical action. |
| `decision` | A decision only the user can make, while other work continues. |

- One alert per block. If it is still blocked an hour after the user acknowledged, send `decision` saying what changed, never the same text.
- Never for progress, finished work, or anything a session can decide or retry itself. Sessions with their own alert policy (`/showrunner:produce`) follow it.
- `FAILED` (exit 3): fall back to PushNotification, beginning its message with the same `Action: <what the user does>` or `No action needed.` line.
