---
description: Show, pause, exit, resume, stop, or restart sessions.
argument-hint: 'pause NAME --why TEXT | exit NAME --why TEXT | help | status [ACCOUNT] [--json] [--here] | resume NAME [--why TEXT] [--to ACCOUNT] | restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here] | stop SESSION [--dry-run] | stop --all [--dry-run]'
---

Use the `agents` program.

- `agents help`: show the usage. `agents` without arguments prints the same text.
- `agents status [ACCOUNT] [--json] [--here]`: show live sessions. `--here` selects this machine.
- `agents pause NAME --why TEXT`: stop the workers now. Let the director finish its turn and hold at its prompt.
- `agents exit NAME --why TEXT`: stop the director and its workers. Keep their staff rows, tabs, and shells.
- Add `--finish-step` to pause or exit after the workers finish their current tasks.
- Use `agents pause --production SHOWRUNNER [--except UNIT ...] --why TEXT` to select a production.
- Use `agents resume --production SHOWRUNNER` to resume its units.
- `agents resume NAME [--why TEXT] [--to ACCOUNT]`: resume a name, staff id, session id, or exact Codex thread name.
- `agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]`: restore saved rows or restart local live rows with `--all`.
- `agents stop SESSION [--dry-run]`: stop the session and its workers. Keep their rows, conversations, and panes at the shell.
- `agents stop --all [--dry-run]`: save the live local staff rows, then stop them without a restart.

Pause and exit record the time, caller, and reason. The board shows `paused` or `exited`.
Resume removes the record. A paused director receives one continuation message.
Failed continuation delivery restores the pause and returns failure.
A stop dry run prints the selected session and workers without stopping them.
An exited director starts through the shared resume path.
Stop does not save or restore composer drafts.
Resume restores the saved folder and environment.
Codex uses the configured `workers` account; Claude units and showrunners use their configured role accounts.
Other Claude sessions use the units account; `--to ACCOUNT` overrides the account.
The default reason is `resumed by name`.
A running session prints `unchanged` and returns status 0.
A closed tab uses the existing restore path.
An empty current pane can resume its own saved session.
Resume restores retained workers with their native conversations.
The short form `resume NAME [--why TEXT] [--to ACCOUNT]` stays available from any directory.

Before a power-off, run `agents stop --all`.
After login, run `agents restart`.
Stop and restart select the same live local staff rows, including freestanding agents.
Stop saves each row ID before its stop. It starts nothing.
A live session without a staff row prints one anomaly line. Stop does not save it.
Restart keeps only row IDs whose starts fail.
`--only` selects row IDs and leaves other saved rows unchanged.
`stop --all --dry-run` prints each selected row and its position with `would stop and save`.
`restart --dry-run` prints saved rows and identifies live rows outside the saved list.
Both commands change nothing.
A row without a saved tab says so on the same line.
One anomaly does not prevent unrelated work.
Stop uses the limits in `config/shutdown.conf`.
See `docs/as-built/shutdown-and-restart.md` and `docs/accounts.md`.
