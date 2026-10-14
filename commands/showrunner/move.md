---
description: Move a session, or the units or showrunners group, to another Claude account, pausing each session first and proving where it came back.
argument-hint: 'session <session-id|name> "claude N"|default [--why <text>] | units|showrunners "claude N"|default [--dry-run] [--why <text>]'
---

# Move

The one way a showrunner moves a Claude session to another account, or stops
one to change its account. Run it in the background (it waits up to 20 minutes
for each move to end):

`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunner_move.py" $ARGUMENTS`

When it finishes, print its output exactly as it comes, with nothing added.

- `session <id|name> <target>`: one local session.
- `units <target>` and `showrunners <target>`: units and showrunners are each
  one group on one account. This sets the group's account to the target and
  moves every live member not already on it, across all productions; for
  `showrunners`, this one included. A showrunner with no session under the id
  its timer names is left where it is and named.
- `--dry-run`: for a group, list the sessions it would move and change nothing.
- `--why <text>`: the reason each session reads when it is back.

Each session is asked to pause (`Pause for move: …`), stopped at its ready mark
and resumed on the target; a unit's showrunner gets one line before it stops and
one when it is back. The script then reads the resumed process itself: one line a
session, `on <target>, proven (pid N runs on <folder>)` or `not on <target>:
<reason>`. Exit 0 means every session is proven (or, for a group, none needed to move); nothing is left to check by hand.
A move of this session ends this turn; the restart proves its account when it is
back and tells the user if it is not.

Never stop or move a session by hand: no `/exit` sent into its pane, no
`shutdown.py now`, no killing a Claude process and no `claude --resume` with a
`CLAUDE_CONFIG_DIR` of your own. A hook refuses each in a showrunner session.
