---
description: Show account homes, roles, quotas and credits; set up a home or swap unit directors or Codex seats.
argument-hint: '[setup "claude N" | setup "codex N" | check "codex N" | role seats | set-role seats "codex N"|default | swap seats "codex N"|default [--dry-run] | swap unit "claude N"|default]'
---

# Accounts

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- No arguments: one Markdown table, a row for `~/.claude` and each `~/.claudeN`,
  logged in or not, and for each Codex account, then a line for anything wrong in a folder.
- `setup "claude N"`: builds the folder and prints how to log in.
- `setup "codex N"`: links the home, finishes its first startup (up to 20 minutes: run it in the background), then prints the login command.
  Keep `CODEX_HOME` on it: without it, `codex login` signs `~/.codex` out and in, stopping seats on the default role.
- `swap unit "claude N"`: moves the unit directors to that account;
  `swap unit default` returns them to `~/.claude`.
- `role seats` shows the selection; `set-role seats "codex N"|default` selects future turns.
- `swap seats "codex N"|default [--dry-run]` checks usage and moves every run at its next turn;
  working seats finish first. `default` selects `~/.codex`.

Switching the default account remains `/login` in a `~/.claude` session.
User's guide: `~/.claude/docs/accounts.md`.
