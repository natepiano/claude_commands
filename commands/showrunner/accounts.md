---
description: Show account groups, move a group or ad hoc session, or swap Codex workers.
argument-hint: '[--detail | pick claude|codex [--exclude <label>] [--keep <label>] [--explain] | move <session-id|name> "claude N"|default [--why <text>] | move units|showrunners "claude N"|default [--dry-run] [--why <text>] | setup "claude N" | setup "codex N" | check "codex N" | role seats | set-role seats "codex N"|default | swap seats "codex N"|default [--dry-run]]'
---

# Accounts

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- No arguments: the units and showrunners accounts, then one Markdown table, a row for `~/.claude` and each `~/.claudeN`,
  logged in or not, and for each Codex account, then a line for anything wrong in a folder. Nothing else.
- `--detail`: the same, then each live session's account, the Codex seats line with its refusal destination
  and held worker continuations, and the last ten move entries with their outcomes.
- An argument it does not recognise: run the no-argument form instead and print only its output, with no explanation.
- Claude sessions refused by the provider move automatically on the notes timer;
  the shutdown progress line keeps their target and reason visible until safe to stop.
- `setup "claude N"`: builds the folder and prints how to log in.
- `setup "codex N"`: links the home, finishes its first startup (up to 20 minutes: run it in the background), then prints the login command.
  Keep `CODEX_HOME` on it: without it, `codex login` signs `~/.codex` out and in, stopping workers on the default role.
- `pick claude|codex [--explain]`: picks room with the soonest weekly reset; `--keep <label>` keeps a working account, and repeated `--exclude <label>` skips accounts.
- `placement --explain`: room and each group's destination on refusal. Low or 0% is room until refused; healthy groups stay put.
- Configuration decides every new and resumed launch: `claude-unit` and `role unit` use units; `role showrunner` uses showrunners. Set it with `accounts.py move <group> "claude N"`; an unconfigured group refuses with that command.
- A showrunner moves sessions and groups with `/showrunner:move`, which proves where each came back; `move` below starts a move and does not wait for it.
- `move <session-id|name> "claude N"|default`: moves one local ad hoc session; grouped sessions must use the group command; `--why <text>` explains the move on resume; an unknown host prints the resume line to run after exit.
- `move units|showrunners "claude N"|default [--dry-run]`: selects the group account and requests shutdown’s pause-for-move protocol; members record `ready --where` before stopping, except shutdown’s idle usage-held owners and requester rules. The timer retries members still off target without duplicating open shutdowns. A preview prints the target and each off-target name/id, or `no live session moves`, and writes nothing; missing showrunner registrations print their repair command. An empty group still selects its account.
- Codex numbers name logins, including a login held in `~/.codex`; setup still uses `~/.codexN`. Each Codex note has `use_credits: on|off` (absent means on); off removes paid-credit room at an exhausted window. Weekly 0% sends one phone/showrunner notice; on stays with hourly forecasts, off prefers weekly room, then another account’s paid credits; no destination or a failed move sends Codex work to Claude until the earliest refill.
- `role seats` shows the selection; `set-role seats "codex N"|default` selects future turns.
- `swap seats "codex N"|default [--dry-run]` checks usage and moves every run at its next turn;
  working workers finish first. `default` selects `~/.codex`.

Switching the default account remains `/login` in a `~/.claude` session.
User's guide: `~/.claude/docs/accounts.md`.
