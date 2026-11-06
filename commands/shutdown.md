---
description: Stop, resume, or restore a session.
argument-hint: 'stop SESSION | resume NAME [--why TEXT] [--to ACCOUNT] | status | restart [--all] [--dry-run]'
---

Run `agents stop SESSION` to stop the session and its worker agents.
Stop checks the current session before each signal and confirms exit.
Workers keep their rows, native conversations, and panes at the shell.
Unknown workers retain their reasons and remain not proved stopped.
Unproved targets stay unchanged; unrelated proved work continues.
Stop uses the limits from `config/shutdown.conf`.

Run `resume NAME [--why "reason"] [--to ACCOUNT]` from another pane and any directory.
Use a short name, a staff id, or a session id.
An exact Codex thread name resolves through the shared `session_index.jsonl`.
A raw session id takes priority over a thread name.
The default reason is `resumed by name`.
The command restores the saved environment and folder.
A Codex resume uses the configured `workers` account.
A Claude unit or showrunner uses its configured role account.
Other Claude sessions keep their saved account.
`--to ACCOUNT` overrides the account for either family.
A closed tab uses the existing restore path.
A herdr server restore keeps the saved account for both families and sends no generated prompt.
Resume from its own empty pane executes the saved launch in that pane.
A running session prints `unchanged` with its current account and returns status 0.
Restore resumes the director and its retained workers with their native conversations.
A stopped worker reuses its pane and terminal; a proved closed worker can use a new split.
Restore reads each worker's role and busy dispatch from the saved pass.
An unavailable role prints its reason and starts no replacement worker.
A busy worker continues that dispatch; the launcher checks the transcript before it submits work.
A completed rerun prints one `unchanged` line, exits 0, and sends no restart notice.
Resume does not stop or move a running session.
An unknown name returns status 1 with close local unit and showrunner names when available.
An ambiguous name returns status 1 with its matching members.
A remote member returns status 1 with its machine name.
`agents resume` accepts the same arguments.

Move sessions with `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`.
The batch stops selected sessions before it resumes them in their panes.
One failure does not prevent unrelated moves.
A self-move uses a detached batch and prints `moving now`; final results enter the shutdown logs directory.
Worker account moves keep the conversation, pane, and terminal; see `docs/accounts.md`.

`agents status [account]` shows the live sessions.
`shutdown.py restart --all` stops each local live Claude or Codex row, then starts each row.
`shutdown.py restart` resumes saved rows that do not run and restores their retained workers.
A failed row prints one anomaly line; other rows continue.
`restart.json` holds only the row IDs whose starts failed.
A started or running row leaves the record.
A refused row lock changes no saved row.
`--only` leaves other saved rows alone.
`--dry-run` prints each name, row ID, kind, and saved tab position without changes.
A row without a saved tab says so on the same line.
Staff supplies current sessions and worktrees; herdr keeps tab order by staff row ID.
Shutdown records contain only row IDs; the existing pass supplies progress and launcher arguments.
An interrupted tab rebuild can lose the split ratio and leave panes in the source workspace.
Inspect the tab and panes named by its anomaly; a retry does not rebuild the split.

Only an existing run pinned to `mesh` uses retained server inventory, pause, and paused-worker resume messages.
Only worker end or permanent unit removal deletes worker rows.
