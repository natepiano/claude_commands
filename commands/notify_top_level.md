---
description: Send one message to every other top-level session on this machine; each showrunner passes it to its unit directors. Args - the message.
---

1. Recipients: `python3 ~/.claude/scripts/message/top_level.py --self <your name>`, your name being the one ListAgents gives this session. Unit directors are left out.
2. SendMessage each recipient `$ARGUMENTS`, then the line `Showrunners: pass this to your unit directors where it applies to them.`
3. Name any recipient whose delivery was held or refused.
