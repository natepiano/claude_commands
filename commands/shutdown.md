---
description: Safely stop or restart an account or selected sessions on natedev and the Mac.
argument-hint: "[account; omitted = this session, or all accounts from a terminal] [--here] [--only NAME[,NAME...]] [--restart] | status | follow ACCOUNT --request ID | now | cancel | restart"
---

In this terminal, run `agents <verb> [arguments]` for `down`, `restart`, `status`, `cancel`, `now` or `follow` (equivalent to `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py <verb> [arguments]`). `/shutdown` remains the in-Claude form. Lead with the account label and show its output unchanged.

| Request | CLI verb |
| --- | --- |
| Stop safely | `down [account]` |
| Show progress | `status [account]` |
| Follow one local request | `follow ACCOUNT --request ID` |
| Stop without readiness | `now [account]` |
| Take unstopped sessions out | `cancel [account]` |
| Restore stopped sessions | `restart [account]` |

Pass flags as given: `--here` limits work to this machine; `--only NAME[,NAME...]` selects exact ids or names and their owned workers on down, status, now, cancel and restart; `down --restart` restores each stopped owner; `down --to ACCOUNT --restart` moves it; `restart --dry-run` previews; `--no-alert` suppresses phone alerts. Never infer now from urgency. Use the given account. With none, down, restart and status use this session's account when `CLAUDE_CODE_SESSION_ID` is set; from a terminal they cover every configured Claude account (restart covers accounts with shutdown records). `--only` and `--here` apply in either form. An unreadable session account refuses attribution; an in-session restart can infer one from stored records or ask for an explicit account.

An idle session stops with no pause; a busy one gets a pause message and stops after ready, or once idle after the pause. A newest transcript refusal or a fresh exhausted account reading starts reaping immediately, without a pause or readiness wait. Identity checks precede every signal. A refusal after the pause also reaps, on every host. If this session has a record after down, in this terminal run `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py ready --where "<work in progress and next step>"` and end the turn. The requester goes last; a showrunner never waits for its units.

A single-account terminal down with tty output follows automatically, including the original requests when repeated. A terminal down covering several accounts starts each account and prints its follow command. Follow prints each timed trail step once, including archived outcomes; stopped is intermediate when a restart is pending. Cancelled sessions print taken out once and count as done in the summary. It exits 0 when done, 1 when the user must act, or 130 on Ctrl-C. Detaching leaves the shutdown running and prints the exact follow command to run in this terminal. Session and redirected requests print commands naming the account, request, selected ids and machine instead.

Every next-step command must say `in this terminal`, `in a terminal on <machine>`, or `in any Claude session on <machine>`. Use full script paths and the explicit account and --only ids. A move's cancel takes out only sessions not yet stopped; it does not undo completed moves. Each machine conducts independently; follow reads the local machine's request records. Follow the peer separately in a terminal on that machine using its printed command.

Status shows progress, message delivery, hold details, cleanup issues and the timed trail. Its actions contain runnable scoped commands. Cancel restores timers and footers, retires pending pause messages and lets unstopped sessions continue. Stopped records remain available to restart. Now also accepts reaping records and retries incomplete cleanup. Restart refuses waiting, ready, reaping and stopping records; a relaunch already in progress is never duplicated. A selected request without a record reports no shutdown in progress.

After four minutes without a ready reply, a waiting owner sends one push naming its window or pane and the exact now command and place; unreadable Ghostty panes are included. It explains the safe-point pause, continued waiting, and that now stops Claude mid-step, keeps uncommitted work, and resumes it when restart was requested. A reaping owner gets no holdout push. The requester receives the same guidance, and follow displays it.

Restart alerts follow the selected records' saved policy on both machines; --here reads no peer. If the peer is unavailable, account-wide restart uses all local records: no local record or any alerts=user allows the alert. A selected restart uses only chosen records. The Remote Control fixer runs quietly for each restarted local session still missing it; outcomes stay in the service log. Probe records always use alerts=none, preserved through restart and move. Partial stops and incomplete restores name what needs attention.

A selected shutdown holds only its sessions; an account-wide shutdown also blocks new launches and scheduled work. Other accounts remain isolated. Legacy record migration conflicts write nothing: report the diagnostic and preserve the files for repair.
