# Delete Style

Delete a shared style file deterministically with the shared admin script.

**Arguments**: `$ARGUMENTS` — `style_name` (`.md` optional)

## Instructions

If `$ARGUMENTS` is empty or does not contain exactly one argument, show usage and stop:
```text
Usage: /delete_style style_name
(`.md` extension is optional — it will be appended when needed)
```

The admin script:
- auto-appends `.md` when the file exists with that suffix,
- strips `see_also` references to the deleted file from every other style file's frontmatter (handles both single-value and list forms),
- rewrites remaining body wikilinks pointing at the deleted file to plain text, and
- cleans history and EVALUATION.md entries.

Run:
```bash
python3 ~/.claude/scripts/nightly/style_admin.py delete $ARGUMENTS
```
