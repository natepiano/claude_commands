---
description: Safely stop or restart an account or selected sessions on natedev and the Mac.
argument-hint: "[account] [--here] [--only NAME[,NAME...]] [--restart] | status | now | cancel | restart"
---

Use `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py`. Run the matching command, lead with the account label, and show its output unchanged:

| Request | CLI |
| --- | --- |
| `/shutdown [account]` | `down [account]` |
| `/shutdown status [account]` | `status [account]` |
| `/shutdown now [account]` | `now [account]` |
| `/shutdown cancel [account]` | `cancel [account]` |
| `/shutdown restart [account]` | `restart [account]` |

Pass flags as given: `--here` limits the command to this machine; `--only NAME[,NAME...]` selects exact session names or ids on `down`, `cancel`, `now`, and `restart`; `down --restart` brings each session back after its stop; `restart --dry-run` previews without changing state; `--no-alert` on `down` or `restart` suppresses phone alerts. Never infer `now` from urgency. Use a given account exactly; otherwise use this session's own account. A bare `restart` with an unreadable own account can infer one from shutdown records and account holds on both machines; if it cannot, show its candidates and request an account.

After starting `down`, run `ready --where "<what this session was doing before /shutdown>"` and end the turn if this session has a record. The requester goes last among its request's sessions on this machine. A selected `restart` can bring that requester back while siblings still wait. A showrunner never waits for its units. Each machine conducts its own sessions; an unreachable peer prints `<machine>: not reached; run /shutdown there` and leaves the local shutdown running.

A selected shutdown holds only its sessions. An account-wide shutdown also blocks new launches and scheduled work on that account. The launch check prints `<label> is held by a shutdown (since <time>)` for an account hold or `<session name> is held by a shutdown (since <time>)` for a restore whose session has not stopped yet; unreadable shutdown state refuses launches.

`status` shows each session's progress, hold details, message delivery and leftover cleanup. Each machine ends with applicable grouped actions:

```text
  take out: /shutdown cancel --only <name> · hana, nixos
  stop now: /shutdown now --only <name> · hana, nixos
  bring back: /shutdown restart --only <name> · bevy
```

Replace literal `<name>` with a listed name or id. `no conductor running; /shutdown --only <name> resumes one, /shutdown cancel --only <name> takes it out` means repeat the selected shutdown or take it out; `conductor ownership unavailable: <reason>` means ownership could not be observed.

- `cancel --only` restores timers and footers, retires queued shutdown messages, tells the session to carry on, archives its record and prints `<name>: taken out`. A still-live failed stop prints `<name>: taken out, still running`. A retained record prints `<name> was not cancelled: <progress>; /shutdown restart --only <name> brings it back` and exits 1; account-wide cancellation uses `/shutdown restart brings it back`. Selected cancellation keeps an account hold while any record of that request remains.
- `now --only` prints `<name>: stopping now` and asks the conductor to stop it, including busy sessions. A partial stop (`stop failed`, or a stopped session with a Codex server or tmux session left running) keeps its issues; `now --only` retries its cleanup. An inapplicable request prints `<name> is <progress>; nothing to stop` and exits 1.
- `restart --only` never relaunches a waiting, ready or stopping session: `<name> is <progress>, not stopped; nothing to restart`, exit 1. A current launch prints `<name> is already restarting`, exit 0. Refusals alone send no alert. A selected session without a record prints `<name>: no shutdown in progress`, exit 1, for `cancel`, `now` or `restart`.

After 5 minutes from a sent settle message, or from the request when it was queued or not sent, a waiting or held ready session alerts by name with `/shutdown cancel --only <name>` (take it out; it carries on) and `/shutdown now --only <name>` (stop it anyway), at most once per 60 minutes. The requester gets the same text and the final result. A relaunched session without Remote Control prints `<name>: running, no Remote Control`; turn it on at that session's terminal with `/remote-control`. It counts as restarted; do not restart it again for this diagnostic.

Restart alerts follow the incomplete sessions' saved policy on both machines; `--only` uses only chosen sessions and `--here` reads no peer. If the peer is unavailable, including an answer without the result object, account-wide restart uses all local records: no local record or any local `alerts: "user"` allows the alert; selected restart uses only chosen records, with no second peer call. `alerts: "none"` also suppresses the Remote Control alert after archive. A partial stop or incomplete restore names what needs attention.

If old-record migration reports conflicting session ids or file names, it writes no session records and moves no source files. Report the diagnostic; keep the files for repair.
