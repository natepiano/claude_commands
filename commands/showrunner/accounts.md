---
description: Show Claude and Codex homes, roles, quotas and credits; set up a home or swap the unit account.
argument-hint: '[setup "claude N" | setup "codex N" | check "codex N" | swap unit "claude N" | swap unit default]'
---

# Accounts

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- No arguments: one Markdown table, a row for `~/.claude` and each `~/.claudeN`,
  logged in or not, and for each Codex account, then a line for anything wrong in a folder.
- `setup "claude N"`: builds the folder and prints how to log in.
- `swap unit "claude N"`: moves the unit directors to that account;
  `swap unit default` returns them to `~/.claude`.

Switching the default account remains `/login` in a `~/.claude` session.
User's guide: `~/.claude/docs/accounts.md`.
