---
description: Stop, resume, or restore a session.
argument-hint: 'stop SESSION | resume NAME [--why TEXT] [--to claudeN] | status | restart [--all] [--dry-run]'
---

Run `agents stop SESSION` to end its Claude process. Codex workers keep their threads. Stop checks the process identity before each signal, sends TERM, then KILL if needed, and confirms exit. It creates no stopping record and waits for no ready mark.

Run `resume NAME [--why "reason"] [--to claudeN]` from any terminal and directory.
Use a short name, a staff id, or a session id.
The default reason is `resumed by name`.
The command restores the saved account, environment, and folder.
A closed tab uses the existing restore path.
A herdr server restore resumes the saved conversation without a generated prompt.
An empty target pane replaces the foreground process with the saved launch.
A session already running on the target account prints an already-running line and returns status 0.
A session running on another account refuses resume without stopping.
An unknown name returns status 1 with close local unit and showrunner names when available.
An ambiguous name returns status 1 with its matching members.
A remote member returns status 1 with its machine name.
`agents resume` accepts the same arguments.

For one or many account moves, use `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`. The shared batch stops all selected sessions before any resumes. Each resumes in its own pane. One failure does not prevent the others. A self-move hands off to a detached batch and prints `moving now`; each final result goes to the shutdown logs directory.

`agents status [account]` shows the live sessions.
`shutdown.py restart --all` saves the local live Claude row IDs, stops each session, then starts each session.
`shutdown.py restart` resumes saved rows that do not run.
A running row prints `unchanged`. A failed row prints one anomaly line. Other rows continue.
`--dry-run` prints the rows and changes nothing.
Staff supplies each row's current session and worktree. Herdr keeps the saved tab order by staff row ID.
The launch sends no work prompt.
Shutdown records contain only row IDs. They contain no progress or account copies.
`forget`, `snapshot`, `down`, `now`, `ready`, `cancel`, `conduct`, and `follow` are removed.
