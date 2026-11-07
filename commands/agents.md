---
description: Show, stop, resume, or restart sessions.
argument-hint: 'help | status [ACCOUNT] [--json] [--here] | resume NAME [--why TEXT] [--to ACCOUNT] | restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here] | stop SESSION'
---

Use the `agents` program.

- `agents help`: show the usage. `agents` without arguments prints the same text.
- `agents status [ACCOUNT] [--json] [--here]`: show live sessions. `--here` selects this machine.
- `agents resume NAME [--why TEXT] [--to ACCOUNT]`: resume a name, staff id, session id, or exact Codex thread name.
- `agents restart [ACCOUNT] [--all] [--dry-run] [--only ID[,ID...]] [--here]`: restore saved rows or restart local live rows with `--all`.
- `agents stop SESSION`: stop the session and its workers. Keep their rows, conversations, and panes at the shell.

Resume restores the saved folder and environment.
Codex uses the configured `workers` account; Claude units and showrunners use their configured role accounts.
Other Claude sessions keep their saved accounts; `--to ACCOUNT` overrides the account.
The default reason is `resumed by name`.
A running session prints `unchanged` and returns status 0.
A closed tab uses the existing restore path.
An empty current pane can resume its own saved session.
Resume restores retained workers with their native conversations.
The short form `resume NAME [--why TEXT] [--to ACCOUNT]` stays available from any directory.

Restart saves only row IDs whose starts fail.
`--only` selects row IDs and leaves other saved rows unchanged.
`--dry-run` prints each name, row ID, kind, and saved tab position without changes.
A row without a saved tab says so on the same line.
One anomaly does not prevent unrelated work.
Stop uses the limits in `config/shutdown.conf`.
See `docs/as-built/shutdown-and-restart.md` and `docs/accounts.md`.
