---
description: Turn the review of pushes to the user's phone on or off, or show its state.
---

Load `/message_style` before the first message to the user in this session.

`$ARGUMENTS`: `on`, `off`, or `status`. If empty, ask for `on` or `off` and stop.

Run `~/.claude/scripts/lib/py ~/.claude/scripts/notify/review.py switch $ARGUMENTS` and relay its output.
