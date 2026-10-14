---
description: Add a Claude or Codex account and wire it into every system that names accounts.
argument-hint: 'claude claudeN [login] | codex codexN <login>'
---

# New account

Run `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/accounts/new_account.py" $ARGUMENTS`
and print its output exactly as it comes, with nothing added.
For `codex`, run it in the background and wait for it: the first startup takes up to 20 minutes.

- Each step reads `done:` or `for you:`. After the user finishes the `for you:` steps, run the same command again.
- `claude claudeN`: builds `~/.claudeN` and prints the login command; the run after login writes the account note from that login.
- `codex codexN <login>`: the login is needed first, because setup refuses a second home on one login.
- Never edit `~/.claude/settings.json` or the nixos shell module; the script prints the lines for the user.

User's guide: `~/.claude/docs/accounts.md`.
