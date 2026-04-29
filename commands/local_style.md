---
description: Create a local style guide doc for the current repo (or a workspace member)
---

**Arguments**: `$ARGUMENTS` — `[<scope>] <filename>`

- `<filename>` is a kebab-case filename (without `.md`), e.g. `diagnostic-lifecycle`.
- `<scope>` is optional. In a Cargo workspace, pass a workspace-member name (e.g. `bevy_diegetic`) to scope the rule to that crate. Omit to write a workspace-wide rule.

## Context

The `/rust_style` loader (`~/.claude/scripts/load-rust-style.sh`) automatically picks up:

- `docs/style/*.md` — workspace-wide (or repo-wide) rules
- `docs/<member>/style/*.md` — rules scoped to a workspace member crate

All workspace-member style dirs are loaded together; the loader prints a disclaimer that some rules may not apply to the file being edited, and the agent judges from context which rules are relevant.

## Format rules

Local style docs must:

1. **No YAML frontmatter** — the global guides use Obsidian frontmatter; local docs do not
2. **Start with an `##` heading** — the rule name, matching the filename in title case
3. **Lead with 1-2 sentences** stating the rule directly
4. **Include a code or structure example** if the rule involves code patterns
5. **Stay under 40 lines** — these get concatenated into context on every `/rust_style` load, so brevity matters. If a rule needs extensive explanation, it belongs in the README or a dedicated doc, not in the style overlay.
6. **One rule per file** — don't combine unrelated conventions

## Steps

1. Determine the target directory:
   - If `<scope>` was provided: `docs/<scope>/style/`
   - Otherwise: `docs/style/`
   - Create the directory if it does not exist.
2. Check if a file already exists at `<dir>/<filename>.md` — if so, read it and update rather than overwrite
3. Ask the user what the rule should say (unless they already described it)
4. Write `<dir>/<filename>.md` following the format above
5. Verify the style loader picks it up:

```bash
zsh ~/.claude/scripts/load-rust-style.sh --list-files
```

The new file should appear in the output.
