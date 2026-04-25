Load this before editing any file under `~/rust/nate_style/` or a repo-local `docs/style/`.

## Context cost is real

Style rules bulk-load into every session via `load-rust-style.sh`. 60+ files. Every sentence you add pays a token cost on every future agent turn. Accuracy first, then ruthless terseness.

## Defaults

- One-line scope notes beat paragraphs. A future reader reads the rule, not your explanation of it.
- Finger-point to other rules by filename (`see foo.md`). Do **not** add to `see_also` unless the target is tiny or strongly load-bearing — the loader inlines see_also'd content, so additions duplicate, not redirect.
- No meta-commentary. Do not explain the mistake that prompted the edit, do not say "reasoning by analogy fails," do not narrate what you cut.
- Do not restate the rule elsewhere in the same file for emphasis.
- Bump `date_modified` in frontmatter.

## Cut list

Before submitting an edit, delete:

- "This is why…" sentences → the rule is the why.
- "Note that…" prefaces → just say the thing.
- Full example blocks when a one-line example makes the same point.
- Any sentence whose removal would not confuse a first-time reader.

## Escalation

If the edit needs more than ~3 lines of body text, it is probably a new rule file, not an amendment. Propose splitting it.
