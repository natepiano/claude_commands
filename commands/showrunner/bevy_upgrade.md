---
description: Track a production's Bevy upgrade, current dependency pins, and the user's answers.
argument-hint: "[status | start <version> | approve <crate> <words> | decline <crate> <words> | forget <crate> | note <crate> [<text>]]"
---

# Bevy upgrade

Run in the showrunner session:
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/bevy_upgrade/bevy_upgrade.py" $ARGUMENTS`
Print its output exactly as it comes, with nothing added. If it exits non-zero,
report its error in one line.

- Every statement about this upgrade in a report or reply comes from this
  command's output in the same turn, never from the log or memory.
- Run `approve` and `decline` only on the user's own words about that crate,
  passed verbatim as one quoted argument. An answer for one crate never
  extends to another.
- Use `note` for what a script cannot look up, such as why a pull request's CI
  fails. With no text, it clears the note.
