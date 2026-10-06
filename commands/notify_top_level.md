---
description: Send one message to every other top-level session, on this machine and on others over Remote Control; each showrunner passes it to its unit directors. Args - `[--here] <message>`; `--here` keeps it to this machine.
---

1. Recipients: `python3 ~/.claude/scripts/message/top_level.py`. Each output line is a session name, a tab, and its `uds:` address. Unit directors are left out. Without `--here`, add every ListAgents peer on another machine by its ListAgents name.
2. SendMessage to each local `uds:` address the message, then the line `Showrunners: pass this to your unit directors where it applies to them.` Report that session by its name. To a peer on another machine add `Reply "ack" to <you>.`; nothing reports that delivery back.
3. Name any held or refused delivery by session name and address. Report each `not reachable` line from step 1 as a session that was not told. Until every other-machine peer has acked, name the ones missing whenever you next report to the user.
