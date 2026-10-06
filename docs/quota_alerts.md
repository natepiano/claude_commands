# Quota alerts: the receiver's protocol

natedev's agent-sessions timer checks every active Claude and Codex account's weekly usage every 2 minutes (`~/.claude/scripts/whoami/agent_notes.py`, which writes `~/rust/hanadocs/agents/*.md`) and messages `always` and each showrunner in `~/.claude/config/showrunners.json` by session name. `/showrunner:produce` adds each showrunner. Mechanics: the docstring of `quota_alert.py` beside it.

Notices arrive through `send.py` (/message) from the relay `quota_alert`, which cannot be answered; a session not running when one is sent has it queued. Tell them apart by the first line:

| First line | Means | Receiver does |
|---|---|---|
| `Quota alert:` | An active account is at or under `threshold_percent`. Repeats every `repeat_minutes` until acknowledged. | Bring it to the user; if you delegate that tool's work, start no new work on it. Treat repeats as one item. |
| `Quota alert:` saying every function on Codex was moved to Claude | Codex ran out, and the registry was switched to Claude. | Bring it to the user and keep delegating, now on Claude; re-run Codex work that was in flight. |
| `Quota alert acknowledged:` | The user silenced it, in the session named. | Drop held copies. Paused work stays paused. |
| `Quota restored:` | The account is back above the threshold: a used limit reset, a new window, or another account active. A Codex one lists what moved back to Codex. | Resume paused work; drop held alerts for it. |

## Commands

- `/quota_ack [note]` — the user's alone (`disable-model-invocation`). Never run it or `quota_alert.py ack` yourself, and never treat an alert as acknowledged.
- `/quota_refresh` — run it when the user says they reset usage. It refreshes the notes now and sends `Quota restored:` to every configured session but yours.

Every user act is echoed to every configured session except the one it happened in. What to do about the quota itself — redeem a limit reset, switch accounts — is the user's call. The one automatic act is the Codex switch and its undoing; the natedev session shows the user each on their next prompt.
