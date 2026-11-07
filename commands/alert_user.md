---
description: Notify the user when work needs them.
---

Load `/message_style` before the first message to the user. Use the matching registered text below. Fill values with facts; keep agent-written title, action and body short and to the point.

On the Mac add `--machine natedev`; never read `~/.config/pushover/env`.

Mac locked, screenshots black (replace `<clear-command>` with a command that exits 0 when unlocked):
`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id mac.locked --value "sender=<your session>" --value "waiting=One unit's phase waits for 2 shots" --clears-when "<clear-command>"`

GitHub keys cold (one push per machine until `github-warm-status` passes; the template sets its key and clear check):
`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id github.keys_cold --value "sender=<your session>" --value "machine=<machine with cold keys>"`

A finished build hold still blocks work:
`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id build_hold.blocking --value "sender=<your session>" --value "machine=natedev" --value "holder=builds" --value "reason=a compiler check" --value "finished=09:10 EST" --clears-when "<command that exits 0 after release>"`

Other work stopped on the user:
`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id alert.blocked --value "sender=hana" --value "title=Hana crashes at start on the Mac" --value "action=On the Mac, tell the hana tab to roll back or keep the Bevy update" --value "body=Hana stops at start with a shader error since the Bevy update at 09:10. Mac screenshots wait until it starts again." --clears-when "<command that exits 0 when fixed>"`

A choice only the user can make:
`~/.claude/scripts/lib/py ~/.claude/scripts/message/send.py --to user --text-id alert.decision --value "sender=builds" --value "title=Keep the old build cache?" --value "action=Say keep or delete in the builds tab" --value "body=The old build cache uses 120 GiB. Deleting it frees the space; the next build of each repo takes about 10 minutes longer."`

One alert per block. Never for progress, finished work, or a choice a session can make. `blocked` is only for an action the user must take now; its clear command stops repeats when fixed. The review refuses agent-written fields when a fixed template fits. On `FAILED` (exit 3), report the failure; no alternate push path.
