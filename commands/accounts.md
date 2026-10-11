---
description: Inspect, prepare, or swap the account used by unit directors
argument-hint: '[status | setup "claude N" | swap unit "claude N" | swap unit default]'
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py $ARGUMENTS` (no arguments means `status`) and show its output unchanged. `swap unit default` returns units to the default account. Switching the default account remains `/login` in a `~/.claude` session.
