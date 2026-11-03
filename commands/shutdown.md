---
description: Stop, resume, or restore a session.
argument-hint: 'stop SESSION | resume NAME [--why TEXT] [--to claudeN] | status | restart [--all] [--dry-run]'
---

Run `agents stop SESSION` to end its Claude process. Codex workers keep their threads. Stop confirms the target session before each signal. It sends TERM, then KILL if needed, and confirms exit. It creates no stopping record and waits for no ready mark.

Run `resume NAME [--why "reason"] [--to claudeN]` from another pane and any directory.
Use a short name, a staff id, or a session id.
The default reason is `resumed by name`.
The command restores the saved account, environment, and folder.
A closed tab uses the existing restore path.
A herdr server restore resumes the saved conversation without a generated prompt.
Resume from its own empty pane executes the saved launch in that pane.
A running session prints `unchanged` with its current account and returns status 0.
Resume does not stop or move a running session.
An unknown name returns status 1 with close local unit and showrunner names when available.
An ambiguous name returns status 1 with its matching members.
A remote member returns status 1 with its machine name.
`agents resume` accepts the same arguments.
Resume makes one attempt.

For one or many account moves, use `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`. The shared batch stops all selected sessions before any resumes. Each resumes in its own pane. One failure does not prevent the others. A self-move hands off to a detached batch and prints `moving now`; each final result goes to the shutdown logs directory.

`agents status [account]` shows the live sessions.
`shutdown.py restart --all` stops each local live Claude or Codex row, then starts each row.
`shutdown.py restart` resumes saved rows that do not run.
A running row prints `unchanged`. A failed row prints one anomaly line. Other rows continue.
`restart.json` holds only the row IDs whose starts failed.
A started or running row leaves the record.
A refused row lock changes no saved row.
`--only` leaves other saved rows alone.
`--dry-run` prints each name, row ID, kind, and saved tab position.
A row without a saved tab says so on the same line.
The command changes nothing.
Staff supplies each row's current session and worktree. Herdr keeps the saved tab order by staff row ID.
The launch sends no work prompt.
Shutdown records contain only row IDs. They contain no progress or account copies.
`forget`, `snapshot`, `down`, `now`, `ready`, `cancel`, `conduct`, and `follow` are removed.
