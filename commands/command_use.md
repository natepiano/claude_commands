---
description: List unused commands and commands by oldest last use.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/usage/command_use.py` on natedev and print its output as it arrives.
Count automation metadata as agent use.
Match launcher input to the nearest entry within the configured time window.
Count unmarked input as user use.
Skill calls count as agent uses; `skillUsage` adds dates without counts.
