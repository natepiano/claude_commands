# Delete Style

Delete a shared style file deterministically with the shared admin script.

**Arguments**: `$ARGUMENTS` — `style_name.md`

## Instructions

If `$ARGUMENTS` is empty or does not contain exactly one argument, show usage and stop:
```text
Usage: /delete_style style_name.md
```

Run:
```bash
python3 ~/.claude/scripts/nightly/style_admin.py delete $ARGUMENTS
```
