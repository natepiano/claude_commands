---
description: Stop, resume, or restore a session.
argument-hint: 'stop SESSION | resume NAME [--why TEXT] [--to claudeN] | status | restart | forget NAME_OR_ID'
---

Run `agents stop SESSION` to end its Claude process. Codex workers keep their threads. Stop confirms the target session before each signal. It sends TERM, then KILL if needed, and confirms exit. It creates no stopping record and waits for no ready mark.

Run `resume NAME [--why "reason"] [--to claudeN]` from another pane and any directory.
Use a short name, a staff id, or a session id.
The default reason is `resumed by name`.
The command restores the saved account, environment, and folder.
A closed tab uses the existing restore path.
A herdr server restore resumes the saved conversation without a generated prompt.
The target pane refuses resume: type `/exit`, then run `resume <name>` from another pane.
A session already running on the target account prints an already-running line and returns status 0.
A session running on another account refuses resume without stopping.
An unknown name returns status 1 with close local unit and showrunner names when available.
An ambiguous name returns status 1 with its matching members.
A remote member returns status 1 with its machine name.
`agents resume` accepts the same arguments.
Resume makes one attempt.

For one or many account moves, use `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`. The shared batch stops all selected sessions before any resumes. Each resumes in its own pane. One failure does not prevent the others. A self-move hands off to a detached batch and prints `moving now`; each final result goes to the shutdown logs directory.

`agents status [account]`, `restart [account] [--only ID[,ID...]] [--here]` and `forget NAME_OR_ID [--account ACCOUNT]` retain shutdown-record support. `snapshot`, `down`, `now`, `ready`, `cancel`, `conduct` and `follow` are removed.

`agents restart` restores explicit shutdown records. Running sessions are skipped; conflicting account evidence names the accounts and refuses the restore. An incomplete shutdown restores only after the owner is positively dead and cleanup is confirmed; unreadable liveness refuses it. Failed records remain available for retry. Unit crash recovery uses retained pane launches and `staff.json`; an existing intentional stop keeps the session down until an explicit resume.

`agents forget NAME_OR_ID [--account ACCOUNT]` retires stopped or failed shutdown-record entries without stopping a process. The account login or label selects between cross-account copies. Recovery snapshots, their restore candidates and desktop notes are deleted.
