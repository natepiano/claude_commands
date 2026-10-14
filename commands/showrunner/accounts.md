---
description: Show accounts, move a session, pick an account with room, or swap Codex seats.
argument-hint: '[pick claude|codex [--exclude <label>] [--keep <label>] [--explain] | move <session-id|name> "claude N"|default [--why <text>] | move showrunners --from "claude N" --to "claude N"|default [--dry-run] [--why <text>] | setup "claude N" | setup "codex N" | check "codex N" | role seats | set-role seats "codex N"|default | swap seats "codex N"|default [--dry-run]]'
---

# Accounts

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- No arguments: one Markdown table, a row for `~/.claude` and each `~/.claudeN`,
  logged in or not, and for each Codex account, then a line for anything wrong in a folder.
  Status also lists each live session's account, Codex's destination on a refusal,
  held seat continuations and the last ten move entries with their outcomes.
  Claude sessions refused by the provider move automatically on the notes timer;
  the shutdown progress line keeps their target and reason visible until safe to stop.
- `setup "claude N"`: builds the folder and prints how to log in.
- `setup "codex N"`: links the home, finishes its first startup (up to 20 minutes: run it in the background), then prints the login command.
  Keep `CODEX_HOME` on it: without it, `codex login` signs `~/.codex` out and in, stopping seats on the default role.
- `pick claude|codex [--explain]`: picks room with the soonest weekly reset; `--keep <label>` keeps a working account, and repeated `--exclude <label>` skips accounts.
- `claude-unit` and `role unit` use `pick claude`; new sessions have no unit role.
- A showrunner moves sessions with `/showrunner:move`, which proves where each came back; `move` below starts a move and does not wait for it.
- `move <session-id|name> "claude N"|default`: moves one local session, keeping its conversation and name; `--why <text>` explains the move on resume; an unknown host prints the resume line to run after exit.
- `move showrunners --from "claude N" --to "claude N"|default [--dry-run]`: moves that account's local showrunners on their original desktops; skips sessions already in shutdown.
- `role seats` shows the selection; `set-role seats "codex N"|default` selects future turns.
- `swap seats "codex N"|default [--dry-run]` checks usage and moves every run at its next turn;
  working seats finish first. `default` selects `~/.codex`.

Switching the default account remains `/login` in a `~/.claude` session.
User's guide: `~/.claude/docs/accounts.md`.
