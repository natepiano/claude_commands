---
description: Create a local style guide doc in docs/style/ for the current repo
---

**Arguments**: `$ARGUMENTS` — a short kebab-case filename (without `.md`) describing the rule, e.g. `diagnostic-lifecycle`

## Context

The `/rust_style` loader (`~/.claude/scripts/load-rust-style.sh`) automatically picks up `docs/style/*.md` from the repo root. Local style docs enforce project-specific conventions that don't belong in the shared global guide.

## Format rules

Local style docs must:

1. **No YAML frontmatter** — the global guides use Obsidian frontmatter; local docs do not
2. **Start with an `##` heading** — the rule name, matching the filename in title case
3. **Lead with 1-2 sentences** stating the rule directly
4. **Include a code or structure example** if the rule involves code patterns
5. **Stay under 40 lines** — these get concatenated into context on every `/rust_style` load, so brevity matters. If a rule needs extensive explanation, it belongs in the README or a dedicated doc, not in the style overlay.
6. **One rule per file** — don't combine unrelated conventions

## Steps

1. Confirm the repo root has (or create) a `docs/style/` directory
2. Check if a file already exists at `docs/style/$ARGUMENTS.md` — if so, read it and update rather than overwrite
3. Ask the user what the rule should say (unless they already described it)
4. Write `docs/style/$ARGUMENTS.md` following the format above
5. Verify the style loader picks it up:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files
```

The new file should appear in the output.
