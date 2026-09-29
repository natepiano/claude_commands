# Quota alerts: the receiver's protocol

natedev's agent-sessions timer checks every active Claude and Codex account's weekly usage every 2 minutes (`~/.claude/scripts/whoami/agent_notes.py`, which writes `~/rust/hanadocs/agents/*.md`) and messages the sessions in `~/.claude/scripts/whoami/quota_alert.json` (`notify`, by session name). Mechanics: the docstring of `quota_alert.py` beside it.

Notices arrive as cross-session messages from a short-lived headless relay, so the sender name varies and cannot be replied to. Tell them apart by the first line:

| First line | Means | Receiver does |
|---|---|---|
| `Quota alert:` | An active account is at or under `threshold_percent`. Repeats every `repeat_minutes` until acknowledged. | Bring it to the user; if you delegate that tool's work, start no new work on it. Treat repeats as one item. |
| `Quota alert acknowledged:` | The user silenced it, in the session named. | Drop held copies. Paused work stays paused. |
| `Quota restored:` | The account is back above the threshold: a used limit reset, a new window, or another account active. | Resume paused work; drop held alerts for it. |

## Commands

- `/quota_ack [note]` — the user's alone (`disable-model-invocation`). Never run it or `quota_alert.py ack` yourself, and never treat an alert as acknowledged.
- `/quota_refresh` — run it when the user says they reset usage. It refreshes the notes now and sends `Quota restored:` to every configured session but yours.

Every user act is echoed to every configured session except the one it happened in. What to do about the quota itself — redeem a limit reset, switch accounts — is the user's call; the notices only inform.
