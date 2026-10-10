---
description: Safely stop or restart every Claude session of one account on natedev and the Mac, watch it, force it, or cancel it.
argument-hint: "[account] | --here [account] | status [account] | now [account] | cancel [account] | restart [account]"
---

Account: act on the account named in `$ARGUMENTS`, or otherwise this session's own account as `/whoami` reports it.

Run the matching command and lead the reply with that account's label:

- `/shutdown [account]`: run `shutdown.py down [account]`. When it starts, run `shutdown.py ready --where "<what this session was doing before /shutdown>"`, report its output, and end the turn; this requesting session is stopped last.
- `/shutdown --here [account]`: run `shutdown.py down [account] --here`. This is the way to shut down only this machine while the other is unreachable; then run `ready` and end the turn as above.
- `/shutdown status [account]`: run `shutdown.py status [account]` and show its output unchanged.
- `/shutdown now [account]`: run `shutdown.py now [account]` and show its output unchanged. This stops busy sessions too; never infer `now` from urgency.
- `/shutdown cancel [account]`: run `shutdown.py cancel [account]` and show its output unchanged.
- `/shutdown restart [account]`: run `shutdown.py restart [account]` and show its output unchanged. It resumes every recorded session on both machines, restores its timers, and leaves manual commands in the output when a host cannot be launched automatically.

Use the optional account exactly as given. A session on another account is never touched.

`down` means every attributable session is resumably stopped: units also lose their exact tmux session, each non-seat owner's Codex server is stopped, and Ghostty windows close when Ghostty still owns them. Showrunners and top-level sessions resume through `/shutdown restart`. Seats keep their job and transcript but no process; their next message respawns them. `stop partial` keeps what was left running and why on the record, and `/shutdown status` shows it under each machine. A stop runs to its end; a second stop on the same machine waits for it. Unknown-account and process-identity-mismatch sessions are listed as left running.

From a terminal, the same restart is:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py restart [account]
```
