---
description: Send one message to every other top-level session, on this machine and on others over Remote Control; each showrunner passes it to its unit directors. Args - `[--here] <message>`; `--here` keeps it to this machine.
---

1. Recipients: `python3 ~/.claude/scripts/message/top_level.py --self <your name>`, your name being the one ListAgents gives this session. Unit directors are left out. Without `--here`, add every ListAgents peer on another machine.
2. SendMessage each recipient the message, then the line `Showrunners: pass this to your unit directors where it applies to them.` To a peer on another machine add `Reply "ack" to <you>.`; nothing reports that delivery back.
3. Name any recipient whose delivery was held or refused. Until every other-machine peer has acked, name the ones missing whenever you next report to the user.
