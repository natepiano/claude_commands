---
description: Stop a local session and resume it on another Claude account, or move the units or showrunners group.
argument-hint: 'session <session-id|name> <claudeN> [--why <text>] | units|showrunners <claudeN> [--dry-run] [--why <text>]'
---

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunner_move.py" $ARGUMENTS` and show its output unchanged.

It delegates the account move to `accounts.py move SESSION_OR_GROUP claudeN --why "reason"`.

For stop or resume alone, use `agents stop SESSION` or `agents resume SESSION --why "reason"`.

A move stops Claude at once, then resumes the same conversation in its own local herdr tab on the target account. One resume message explains why and how to resume its Codex workers; workers keep their threads. Each member prints one result line. There is no ready, follow or cancel step.

`units` or `showrunners` selects the group's account and moves every live off-target member. `--dry-run` previews without changes. `--why` supplies the resume reason. A session on the other machine moves from there. See `docs/accounts.md`, “Move a session”, for target checks and refusals.
