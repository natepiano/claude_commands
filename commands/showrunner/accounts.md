---
description: Show account groups, move a group or ad hoc session, or swap Codex workers.
argument-hint: '[--detail | exec claudeN|codexN|units|showrunners|workers [args…] | pick claude|codex [--exclude <label>] [--keep <label>] [--explain] | move <session-id|name> claudeN [--dry-run] [--why <text>] | move units|showrunners claudeN [--dry-run] [--why <text>] | setup claudeN | setup codexN | check codexN | role workers | set-role workers codexN | swap workers codexN [--dry-run]]'
---

# Accounts

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/accounts.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.

- No arguments: the units and showrunners accounts, then one Markdown table, a row for `~/.claude` and each `~/.claudeN`,
  logged in or not, and for each Codex account, then a line for anything wrong in a folder. Nothing else.
- `--detail`: the same, then each live session's account, the Codex workers line with its refusal destination
  and held worker continuations, the last ten account moves with their cause and outcomes, and `Codex work routing:` with open, recovered or failed assignment fallbacks.
- An argument it does not recognise: run the no-argument form instead and print only its output, with no explanation.
- A proof refusal affects only its ungrouped probe; it changes no account refusal or other session’s pick. Hand moves record `by hand`; group retries keep their transition ID and cause.
- Claude sessions refused by the provider move automatically on the notes timer;
  the shutdown progress line keeps their target and reason visible until safe to stop.
- `setup claudeN`: builds the folder and prints how to log in.
- `exec claudeN|codexN|units|showrunners|workers [args…]`: starts Claude or Codex on that account or group's selection, passing arguments unchanged; `--probe` before the label is Claude-only.
- `setup codexN`: links the home, finishes its first startup (up to 20 minutes: run it in the background), then prints the login command.
  Keep `CODEX_HOME` on it: without it, `codex login` signs `~/.codex` out and in, stopping workers using that home.
- `pick claude|codex [--explain]`: picks room with the soonest weekly reset; `--keep <label>` keeps a working account, and repeated `--exclude <label>` skips accounts.
- `placement --explain`: room and each group's destination on refusal. Low or 0% is room until refused; healthy groups stay put.
- Configuration decides every new and resumed launch: `claude-unit` and `role unit` use units; `role showrunner` uses showrunners. Set it with `accounts.py move <group> claudeN`; an unconfigured group refuses with that command.
- In this terminal, a showrunner uses `/showrunner:move` to prove where each session came back. A terminal `accounts.py move` follows each source request live; session and redirected callers receive scoped follow commands.
- `move <session-id|name> claudeN [--dry-run]`: moves or previews one local ad hoc session; grouped sessions use the group command. `--why <text>` explains the move on resume. A manual restart outcome gives its command and place.
- `move units|showrunners claudeN [--dry-run]`: selects the group account and moves off-target members. A preview writes nothing and shows each name/id with `pause, then move` or `reap and restart now`, its source account and reason. A fresh exhausted target with no credits refuses before writes; a missing or stale reading is named and permits the move. A missing showrunner registration names the repair command to run inside that showrunner's own session. An empty group still selects its account.
- Prompt-taking members pause and mark ready; refused or exhausted members reap and restart immediately, with process identity checks. A terminal run follows every source request, even if one needs help; Ctrl-C detaches without cancelling. The group move-back command names its previous stored account, or its common source when no account was stored; mixed sources without a stored account get no command. A single-session move-back command names its source. Supervised alias-started sessions keep their window and scroll history across restarts.
- Codex numbers name logins, including a login held in `~/.codex`; setup still uses `~/.codexN`. Each Codex note has `use_credits: on|off` (absent means on); off removes paid-credit room at an exhausted window. Weekly 0% sends one phone/showrunner notice; on stays with hourly forecasts, off prefers weekly room, then another account’s paid credits; no destination or a failed move sends Codex work to Claude until the earliest refill.
- `role workers` shows the selection; `set-role workers codexN` selects future turns.
- `swap workers codexN [--dry-run]` checks usage, interrupts running workers and resumes their threads on the target immediately; stopped commands must be rerun. Prints `Workers moving <source> → <target>:` (`Workers would move` for previews), then one showrunner · unit · worker → target line per worker, adding `, interrupted and resumed` for mid-turn workers. A failed interrupt or resume names the worker and its manual resume command. Previews interrupt nothing. A number resolves to the home holding its note’s login, including `~/.codex`.
- Swaps and sign-in changes skip ended sessions' workers; a surviving launcher counts only when the run has no holder. Ended runs print `N leftover servers from ended sessions, M stopped`; busy servers get `left running:` lines with reasons. Previews stop nothing and print `N leftover servers from ended sessions: M can be stopped, K would stay`, plus `would stay:` reasons. Rows fold by holder and slot with ` ×N`, followed by `, K interrupted and resumed` when K entries were mid-turn; failures keep separate rows and resume commands.

Changing the login in `~/.claude` remains `/login` in a `~/.claude` session.
User's guide: `~/.claude/docs/accounts.md`.

Account names are numbers only. `default` refuses with each home’s current number. An unnoted signed-in home is shown as not selectable; add its numbered note first.
