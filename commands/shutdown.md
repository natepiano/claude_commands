---
description: Stop, resume, or restore a session.
argument-hint: 'stop SESSION | resume SESSION --why TEXT [--to claudeN] | status | restart | forget NAME_OR_ID'
---

Run `agents stop SESSION` to end its Claude process. Codex workers keep their threads. Stop checks the process identity before each signal, sends TERM, then KILL if needed, and confirms exit. It creates no stopping record and waits for no ready mark.

Run `agents resume SESSION --why "reason" [--to claudeN]` to resume that conversation in its own herdr pane. The account exec command receives one opening message: why it stopped and resumed, continue its work, and resume its existing worker threads. No pane typing or second message is used. A session id or exact tab label selects a local session; run the command on the machine that owns its tab.

For one or many account moves, use `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`. The shared batch stops all selected sessions before any resumes. Each resumes in its own pane. One failure does not prevent the others. A self-move hands off to a detached batch and prints `moving now`; each final result goes to the shutdown logs directory.

`agents status [account]`, `restart [account] [--only ID[,ID...]] [--here]` and `forget NAME_OR_ID [--account ACCOUNT]` retain shutdown-record support. `snapshot`, `down`, `now`, `ready`, `cancel`, `conduct` and `follow` are removed.

`agents restart` restores explicit shutdown records. Running sessions are skipped; conflicting account evidence names the accounts and refuses the restore. An incomplete shutdown restores only after the owner is positively dead and cleanup is confirmed; unreadable liveness refuses it. Failed records remain available for retry. Unit crash recovery uses retained pane launches and `staff.json`; an existing intentional stop keeps the session down until an explicit resume.

`agents forget NAME_OR_ID [--account ACCOUNT]` retires stopped or failed shutdown-record entries without stopping a process. The account login or label selects between cross-account copies. Recovery snapshots, their restore candidates and desktop notes are deleted.
