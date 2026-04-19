# Rename Style

Rename a shared style file deterministically with the shared admin script.

**Arguments**: `$ARGUMENTS` — `old_name.md new_name.md`

## Instructions

If `$ARGUMENTS` is empty or does not contain exactly two arguments, show usage and stop:
```text
Usage: /rename_style old_name.md new_name.md
```

Run:
```bash
python3 ~/.claude/scripts/nightly/style_admin.py rename $ARGUMENTS
```
