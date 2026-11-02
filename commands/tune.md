---
description: Publish an approved config value.
---

**Usage:** `/tune <file> <key> <value>` (file is the config stem)

**Arguments:** $ARGUMENTS

Only the user approves a value change. A session that wants a change asks the
user in one line, then runs this command only on the user's go. Moving a value
from code to config with the same value needs no go and does not use `/tune`.

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/tune.py" <file> <key> <value> --why "<one line>" --user-words "<the user's own approval words>"`.
Pass arguments as separate, quoted shell arguments. On the Mac the script
forwards them to natedev. It checks the key and limits, commits only that config
file on main, pushes, and pulls the Mac at once. Git fetch, Git push, and the Mac
pull have time limits. A stalled or asleep Mac keeps a queued pull. The
15-minute sync retries the pull. Run no test or gate. Add no approval step after
the user's go.

If `<key>.<host>` also exists, the script refuses `<key>` and names both lines.
Name the exact key, such as `sweep_free_floor_gib.mac`, to change that line.

Report the script's result. Exit 2 means refusal; exit 1 means publication or
forwarding failed. `already <key>=<value>` means the value needs no edit.
The script still completes publication and retries the Mac pull.
Exit 0 requires main on origin and a completed or queued Mac pull.
