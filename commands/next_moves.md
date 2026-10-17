---
description: Show or change what happens next when a Claude group's weekly usage runs low.
---

Translate $ARGUMENTS into `python3 ~/.claude/scripts/accounts/accounts.py next`:

- “when units reach 1% switch to claude2” → `add units 1 claude2`.
- A requested hold adds `--hold-minutes N`.
- “what's set up” → `list`.
- “drop the units one” → list, identify the matching entry, then `remove ID`; clarify only if several match.

Print the list after every change. Entries run once, in list order; a recorded move removes its entry. Reads and changes on the Mac go to natedev. See `docs/accounts.md`.
