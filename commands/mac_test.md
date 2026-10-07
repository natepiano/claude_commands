---
description: Block new Mac tests and builds, remove your block, or report the Mac's current test state. Args - `block <why>`, `unblock`, or `status`.
---

`$ARGUMENTS` is `block <why>`, `unblock`, or `status`.

1. `block <why>` runs `python3 ~/.claude/scripts/mac_test/mac_test.py block --holder <your session name> --for '<why>'`. A block stops new tests and builds on the Mac. A test already running finishes, and the "Mac is free" message arrives when it does.
2. `unblock` runs `python3 ~/.claude/scripts/mac_test/mac_test.py unblock --holder <your session name>`.
3. `status` runs `python3 ~/.claude/scripts/mac_test/mac_test.py status`.
