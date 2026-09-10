---
description: Load before editing any command, skill, or style rule file.
---

Load before editing anything that enters a session as instructions: `~/.claude/commands/`, skills, and style rules under `~/rust/nate_style/` or a repo-local `docs/style/`.

## Context cost is real

Style rules bulk-load into every session via `load-rust-style.sh`; a command loads whole on every invocation. Every sentence pays a token cost on every future turn. Accuracy first, then ruthless terseness — as short as it goes without losing what an agent needs to finish the work.

## Defaults

- One-line scope notes beat paragraphs. A reader reads the rule, not your explanation of it.
- Point at other files by name (`see foo.md`). In style rules, do **not** add to `see_also` unless the target is tiny or carries the rule itself — the loader inlines it, so additions duplicate instead of redirecting.
- No meta-commentary. Do not explain the mistake that prompted the edit, do not narrate what you cut.
- Do not restate a rule elsewhere in the same file for emphasis.
- Style rules: bump `date_modified` in frontmatter.

## Cut list

Before submitting an edit, delete:

- "This is why…" sentences → the rule is the why.
- "Note that…" prefaces → just say the thing.
- Full example blocks when a one-line example makes the same point.
- Any sentence whose removal would not confuse a first-time reader.
- Motivation, reassurance, and context the step itself already carries.

## Escalation

A style edit needing more than ~3 lines of body is probably a new rule file, not an amendment. Propose splitting it.
