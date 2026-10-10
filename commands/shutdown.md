---
description: Safely settle and stop every Claude session of one account on natedev and the Mac, watch it, force it, or cancel it.
argument-hint: "[account] | --here [account] | status [account] | now [account] | cancel [account]"
---

Account: act on the account named in `$ARGUMENTS`, or otherwise this session's own account as `/whoami` reports it.

Run the matching command and lead the reply with that account's label:

- `/shutdown [account]`: run `shutdown.py down [account]`. When it starts, run `shutdown.py ready --where "<what this session was doing before /shutdown>"`, report its output, and end the turn; this requesting session is stopped last.
- `/shutdown --here [account]`: run `shutdown.py down [account] --here`. This is the way to shut down only this machine while the other is unreachable; then run `ready` and end the turn as above.
- `/shutdown status [account]`: run `shutdown.py status [account]` and show its output unchanged.
- `/shutdown now [account]`: run `shutdown.py now [account]` and show its output unchanged. This stops busy sessions too; never infer `now` from urgency.
- `/shutdown cancel [account]`: run `shutdown.py cancel [account]` and show its output unchanged.

Use the optional account exactly as given. A session on another account is never touched.

`down` means every attributable session is resumably stopped: units also lose their exact tmux session, each non-seat owner's Codex server is stopped, and Ghostty windows close when Ghostty still owns them. Showrunners and top-level sessions resume through `/shutdown restart`. Seats keep their job and transcript but no process; their next message respawns them. `stop partial` names anything that could not be confirmed stopped. Unknown-account and process-identity-mismatch sessions are listed as left running.
