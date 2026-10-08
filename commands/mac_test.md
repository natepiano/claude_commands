---
description: Block new Mac and CI work, remove your block, or report the Mac's current test state. Args - `block <why> [hours N]`, `unblock`, or `status`.
---

`$ARGUMENTS` is `block <why> [hours N]`, `unblock`, or `status`.

1. `block <why> [hours N]` runs `python3 ~/.claude/scripts/mac_test/mac_test.py block --holder <your session name> --for '<why>' [--hours N]`. A unit director also passes `--showrunner <its showrunner's session name>`, which `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py name <production slug>` prints. A block stops new tests and builds on the Mac and turns off CI's Mac switch. New CI runs skip the Mac job and stay green; runs started under the block receive no macOS check. A local test or CI Mac job already running finishes first, and the "Mac is free" message arrives when it does. The block lifts by itself at the reported time. Run `block` again to renew it.
2. `unblock` runs `python3 ~/.claude/scripts/mac_test/mac_test.py unblock --holder <your session name>`.
   It turns CI's Mac switch back on only when this block turned it off, then lists each branch whose CI runs skipped the macOS job and the commit to run again.
3. `status` runs `python3 ~/.claude/scripts/mac_test/mac_test.py status`.

Run these commands with the sandbox off. They write under
`~/.local/state/mac-test`, call `gh`, and start a user service that watches the
block until it becomes active or expires.
