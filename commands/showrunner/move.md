---
description: Move a session, this showrunner's units, or an account's showrunners to another Claude account, pausing each first and proving where it came back.
argument-hint: 'session <session-id|name> "claude N"|default [--why <text>] | units "claude N"|default [--from "claude N"] [--why <text>] | showrunners --from "claude N" "claude N"|default [--why <text>]'
---

# Move

The one way a showrunner moves a Claude session to another account, or stops
one to change its account. Run it in the background (it waits up to 20 minutes
for each move to end):

`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunner_move.py" $ARGUMENTS`

When it finishes, print its output exactly as it comes, with nothing added.

- `session <id|name> <target>`: one local session.
- `units <target> [--from "claude N"]`: every live unit of this showrunner's
  production, or only those on `--from`.
- `showrunners --from "claude N" <target>`: every local showrunner on that
  account, this one included.
- `--why <text>`: the reason each session reads when it is back.

Each session is asked to pause (`Pause for move: …`), stopped at its ready mark
and resumed on the target; a unit's showrunner gets one line before it stops and
one when it is back. The script then reads the resumed process itself: one line a
session, `on <target>, proven (pid N runs on <folder>)` or `not on <target>:
<reason>`. Exit 0 means every session is proven; nothing is left to check by hand.
A move of this session ends this turn; the restart proves its account when it is
back and tells the user if it is not.

Never stop or move a session by hand: no `/exit` sent into its pane, no
`shutdown.py now`, no killing a Claude process and no `claude --resume` with a
`CLAUDE_CONFIG_DIR` of your own. A hook refuses each in a showrunner session.
