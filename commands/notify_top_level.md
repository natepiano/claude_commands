---
description: Send a message to top-level sessions.
---

Use `$ARGUMENTS` as `[--here] <message>`. With `--here`, send only on this machine.

1. Recipients: `~/.claude/scripts/lib/py ~/.claude/scripts/message/top_level.py`. Each output line is a session name, a tab, and its `uds:` address. Unit directors under a showrunner are left out, since their showrunner passes the message on; a unit director with no showrunner is included. Without `--here`, add every ListAgents peer on another machine by its ListAgents name.
2. SendMessage to each local `uds:` address the message, then the line `Showrunners: pass this to your unit directors where it applies to them.` Report that session by its name. For a peer on another machine, send the same text with `send.py --machine <other machine> --to <name> --from "<you>" --text "<text>"` (/message) and report its outcome line.
3. Name any held or refused delivery by session name and address. Report each `not reachable` line from step 1 as a session that was not told. Name every other-machine peer whose `send.py` outcome is not `SENT`.
