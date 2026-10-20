---
description: Move a session, or the units or showrunners group, to another Claude account, pausing each session first and proving where it came back.
argument-hint: 'session <session-id|name> <claudeN> [--why <text>] | units|showrunners <claudeN> [--dry-run] [--why <text>]'
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
- `--dry-run`: for a group, show each off-target name/id, source account and `will move once idle` or `will stop and move now` with its reason; change nothing. In this terminal, `~/.claude/scripts/lib/py ~/.claude/scripts/accounts/accounts.py move <session-id|name> <target> --dry-run` previews one ad hoc session.
- `--why <text>`: the reason each session reads when it is back.

A fresh target at zero usage with no credits refuses before writes and gives the picker command. A missing or stale target reading is named and permits the move. Idle members stop with no pause; busy members get `Pause for move: …` and stop after marking ready, or once idle after the pause. A transcript refusal or fresh exhausted source reading reaps and restarts immediately. Supervised windows, including sessions started by numbered account aliases, retain their window and scroll history.

A terminal `accounts.py move` follows the shutdown trail live to its summary; session or redirected calls print the exact follow command and place. `all N back on <target>` means done; `taken out` means cancelled members stayed out; an action line names what to do and where, then following continues through other source requests. Ctrl-C detaches while the shutdown continues. One group move-back command names its previous stored account, or its common source when no account was stored; mixed sources without a stored account get no command. Single-session guidance names its source.

This showrunner wrapper keeps missing-reading notices and move-back guidance, and reads each resumed process: `on <target>, proven (pid N runs on <folder>)` or `not on <target>: <reason>`. Exit 0 means every session is proven, or no member needed to move. A move of this session ends this turn; restart proves its account when it is back and tells the user if it is not.

Never stop or move a session by hand: no `/exit` sent into its pane, no
`shutdown.py now`, no killing a Claude process and no `claude --resume` with a
`CLAUDE_CONFIG_DIR` of your own. A hook refuses each in a showrunner session.
