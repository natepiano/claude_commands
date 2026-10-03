---
description: Turn banned-word hook enforcement on or off
---

# banned_word_hook

`$ARGUMENTS` — required: `on` or `off`. `status` reports without changing anything.

**If `$ARGUMENTS` is empty, ask for `on` or `off` and stop.** Do not default to
either one and do not report status instead — this command exists to change a
setting, and picking a direction for the user is not a decision to make on their
behalf.

Run:

```bash
~/.claude/scripts/hooks/banned_word_hook.py $ARGUMENTS
```

If the write fails with `Operation not permitted`, re-run it with
`dangerouslyDisableSandbox: true`; the sandbox denies writes under
`~/.claude/config` in some configurations.

Relay the script's output as-is. It prints the transition, the switch file, and
the two hooks the setting governs.

## What the switch does

The two hooks stay registered in `~/.claude/settings.json` permanently:

| Event | Hook | Role |
|---|---|---|
| `PostToolUse` | `post-tool-use-banned-words.py` | blocks until the violation is fixed, messages it and bumps the counter |
| `Stop` | `stop-assistant-prose-banned-words.py` | scans the turn just emitted and blocks until it is rewritten |

Each one calls `hooks_enabled()` in `scripts/hooks/banned_words_lib.py` before
scanning anything, and that function reads `config/banned_words.conf`. Setting
it to `off` makes both exit without a scan.

Registration and enforcement are deliberately separate. `settings.json` carries
a git clean filter, so flipping enforcement by adding and removing hook entries
would mean JSON surgery through that filter every time — and an entry deleted by
an unrelated edit is how enforcement went missing for three months without
anyone noticing. A config line can go wrong in exactly one way, and
`/banned_word_hook status` reads it back.

## Related

- `/add_banned_word` — add an entry to the style guide. The hooks re-read the
  guide on every invocation, so a new entry takes effect with no restart.
- `/banned_word_analysis` — the local counter report. Its counters are written
  by the two hooks above, so they stand still while enforcement is off.
- `/revert_banned_count` — undo the most recent counter bump.
