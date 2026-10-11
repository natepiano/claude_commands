---
description: Turn a showrunner's reply footers on or off, or show their current state.
argument-hint: "[on|off]"
---

# Footer

Run this in the showrunner session during `/showrunner:produce`.

**Usage:** `/showrunner:footer [on|off]`. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/showrunner_footer.py" <action>`
and report its output. Use `status` when no argument was given; it changes nothing.
With any argument other than `on` or `off`, say `Use /showrunner:footer on or /showrunner:footer off.` and stop.

`off` pauses reply footers and the Waiting on block for every production
targeting this session; `on` resumes both. The switch persists across
compaction and session resume.
