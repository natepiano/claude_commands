---
description: Safely stop or restart an account or selected sessions on natedev and the Mac.
argument-hint: "[account; omitted = this session, or all accounts from a terminal] [--here | --also MACHINE] [--only NAME[,NAME...]] [--restart] | status | follow ACCOUNT --request ID | now | cancel | restart | snapshot | forget NAME_OR_ID [--account ACCOUNT]"
---

In this terminal, run `agents <verb> [arguments]` for `down`, `restart`, `status`, `cancel`, `now`, `follow`, `snapshot` or `forget` (equivalent to `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> [arguments]`). `/shutdown` remains the in-Claude form. Lead with the account label and show its output unchanged.

| Request | CLI verb |
| --- | --- |
| Stop safely | `down [account]` |
| Show progress | `status [account]` |
| Follow one local request | `follow ACCOUNT --request ID` |
| Stop without readiness | `now [account]` |
| Take unstopped sessions out | `cancel [account]` |
| Restore stopped or crashed sessions | `restart [account]` |
| Save a read-only recovery observation | `snapshot` |
| Retire one recovery entry | `forget NAME_OR_ID [--account ACCOUNT]` |

Pass flags as given. `down` and `now` stop sessions only on this machine; `--also <other machine>` (`mac` or `natedev`) adds the other machine, and a session named with `--only` is stopped where it runs. `--here` limits the other verbs to this machine; `--only NAME[,NAME...]` selects exact ids or names and their owned workers on down, status, now, cancel and restart; `down --restart` restores each stopped owner; `restart --dry-run` previews; `--no-alert` suppresses phone alerts. Never infer now from urgency. Use the given account. With none, down, restart and status use this session's account when `CLAUDE_CODE_SESSION_ID` is set; from a terminal they cover every configured Claude account (restart covers accounts with shutdown records, holds or recovery snapshots, including hand-run Codex sessions). A named Claude account restores only its Claude sessions. `--only` and `--here` apply in either form. An unreadable session account refuses attribution; an in-session restart can infer one from stored records or ask for an explicit account.

After a crash or reboot, run `agents restart`. Each session names its source: `from shutdown record` or `from snapshot taken <time> (<age>)`, using the entry’s last-seen time in the user’s zone. A record takes precedence for its id; other sessions with a crash signal still restore from snapshots. A snapshot session is restorable when its boot id changed or its herdr server or terminal process is gone. A session that exits while its host stays alive leaves the snapshot; sessions stopped by agents down restore through their shutdown records. Running sessions are skipped. Conflicting account evidence refuses a restore and names the accounts. An incomplete shutdown restores only after the owner is positively dead and cleanup is confirmed; unreadable liveness refuses it. A successful restore removes its snapshot entry; a failure keeps it for retry.

`agents snapshot` changes no session. An incomplete observation keeps every previous recovery file and names its failed sources. Status lists unresolved sessions with their last-seen times in the user's zone. `agents forget NAME_OR_ID [--account ACCOUNT]` retires local recovery entries and stopped or failed shutdown-record entries without stopping a process; the account login or label selects between cross-account copies. Snapshot-only names participate in restart and forget, while down, now, cancel and status selections use membership and shutdown records. Codex restores in herdr with its recorded home and session id; without an id it opens Codex's resume picker. Snapshots stay on the machine that observed them.

An idle session stops with no pause; a busy one gets a pause message and stops after ready, or once idle after the pause. A newest transcript refusal or a fresh exhausted account reading starts reaping immediately, without a pause or readiness wait. Identity checks precede every signal. A refusal after the pause also reaps, on every host. If this session has a record after down, in this terminal run `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py ready --where "<work in progress and next step>"` and end the turn. The requester goes last; a showrunner never waits for its units.

A single-account terminal down with tty output follows automatically, including the original requests when repeated. A terminal down covering several accounts starts each account and prints its follow command. Follow prints each timed trail step once, including archived outcomes; stopped is intermediate when a restart is pending. Cancelled sessions print taken out once and count as done in the summary. It exits 0 when done, 1 when the user must act, or 130 on Ctrl-C. Detaching leaves the shutdown running and prints the exact follow command to run in this terminal. Session and redirected requests print commands naming the account, request, selected ids and machine instead.

Every next-step command must say `in this terminal`, `in a terminal on <machine>`, or `in any Claude session on <machine>`. Use full script paths and the explicit account and --only ids. Each machine conducts independently; follow reads the local machine's request records. Follow the peer separately in a terminal on that machine using its printed command.

Status shows progress, message delivery, hold details, cleanup issues and the timed trail. Its actions contain runnable scoped commands. Cancel restores timers, retires pending pause messages and lets unstopped sessions continue. Stopped records remain available to restart. Now also accepts reaping records and retries incomplete cleanup. Restart refuses incomplete records whose owner or cleanup cannot be confirmed gone; a relaunch already in progress is never duplicated. A selected request without a record or recovery entry reports no shutdown in progress.

After four minutes without a ready reply, a waiting owner sends one push naming its window or pane and the exact now command and place; unreadable Ghostty panes are included. It explains the safe-point pause, continued waiting, and that now stops Claude mid-step, keeps uncommitted work, and resumes it when restart was requested. A reaping owner gets no holdout push. The requester receives the same guidance, and follow displays it.

Restart alerts follow the selected records' saved policy on both machines; --here reads no peer. If the peer is unavailable, account-wide restart uses all local records: no local record or any alerts=user allows the alert. A selected restart uses only chosen records. The Remote Control fixer runs quietly for each restarted local session still missing it; outcomes stay in the service log. Probe records always use alerts=none, preserved through restart. Partial stops and incomplete restores name what needs attention.

A selected shutdown holds only its sessions; an account-wide shutdown also blocks new launches and scheduled work until it is finished, then the account opens again. Other accounts remain isolated. Legacy record migration conflicts write nothing: report the diagnostic and preserve the files for repair.

A session with no recorded conversation starts fresh under its recorded id and prints a plain line explaining that. On the Mac, a `terminal` host restarts into herdr in an attached Ghostty window.

`herdr_sessions.py start` or `resume` in an untrusted folder refuses with one line to paste: the account’s trust command, then the same invocation with `--cwd` written out. Run that line, accept trust, exit Claude, and retry.

For an account move, run `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id|exact-name|units|showrunners> <claudeN> [--why "<text>"]`. It stops Claude at once, then resumes the same session in its own local herdr tab on the target account. It sends one message with the reason and Codex worker resume instructions, and prints one result line per member. Workers keep their threads. Moves have no ready, follow or cancel step; a session on the other machine moves from there.
