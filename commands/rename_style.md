# Rename Style

Rename a shared style file deterministically with the shared admin script.

**Arguments**: `$ARGUMENTS` — `old_name new_name` (`.md` optional)

## Instructions

If `$ARGUMENTS` is empty or does not contain exactly two arguments, show usage and stop:
```text
Usage: /rename_style old_name new_name
(`.md` extension is optional — it will be appended when needed)
```

The admin script auto-appends `.md` to either argument when the source file exists with that suffix, and rewrites `see_also` wikilinks across all style files so references to the old name continue to resolve.

Run:
```bash
python3 ~/.claude/scripts/nightly/style_admin.py rename $ARGUMENTS
```
