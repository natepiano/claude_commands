---
description: Stop or resume a local session, or restore recovery entries.
argument-hint: 'stop SESSION | resume SESSION --why TEXT [--to claudeN] | status | restart | snapshot | forget NAME_OR_ID'
---

Run `agents stop SESSION` to end its Claude process. Codex workers keep their threads. Stop checks the process identity before each signal, sends TERM, then KILL if needed, and confirms exit. It creates no stopping record and waits for no ready mark.

Run `agents resume SESSION --why "reason" [--to claudeN]` to resume that conversation in its own herdr pane. The account exec command receives one opening message: why it stopped and resumed, continue its work, and resume its existing worker threads. No pane typing or second message is used. A session id or exact tab label selects a local session; run the command on the machine that owns its tab.

For one or many account moves, use `accounts.py move <session-id|exact-name|units|showrunners> <claudeN> --why "reason"`. The shared batch stops all selected sessions before any resumes. Each resumes in its own pane. One failure does not prevent the others. A self-move hands off to a detached batch and prints `moving now`; each final result goes to the shutdown logs directory.

`agents status [account]`, `restart [account] [--only ID[,ID...]] [--here]`, `snapshot` and `forget NAME_OR_ID [--account ACCOUNT]` retain recovery support. `down`, `now`, `ready`, `cancel`, `conduct` and `follow` are removed.

After a crash or reboot, run `agents restart`. Each session names its source: `from shutdown record` or `from snapshot taken <time> (<age>)`, using the entry’s last-seen time in the user’s zone. A record takes precedence for its id; other sessions with a crash signal still restore from snapshots. A snapshot session is restorable when its boot id changed or its herdr server or terminal process is gone. A session that exits while its host stays alive leaves the snapshot; old shutdown records remain restorable until Phase 8. Running sessions are skipped. Conflicting account evidence refuses a restore and names the accounts. An incomplete shutdown restores only after the owner is positively dead and cleanup is confirmed; unreadable liveness refuses it. A successful restore removes its snapshot entry; a failure keeps it for retry.

`agents snapshot` changes no session. An incomplete observation keeps every previous recovery file and names its failed sources. Status lists unresolved sessions with their last-seen times in the user's zone. `agents forget NAME_OR_ID [--account ACCOUNT]` retires local recovery entries and stopped or failed shutdown-record entries without stopping a process; the account login or label selects between cross-account copies. Snapshot-only names participate in restart and forget, while status selections use membership and shutdown records. Codex restores in herdr with its recorded home and session id; without an id it opens Codex's resume picker. Snapshots stay on the machine that observed them.
