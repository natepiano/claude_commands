---
description: Add a new banned word to the global style guide
argument-hint: <stem> [substitute1, substitute2, ...]
---

<!-- allow-banned: this command authors entries in the banned-words list and must name banned stems by example -->

Append a new banned-word section to `~/rust/nate_style/rust/forbidden-words.md`.

Arguments: $ARGUMENTS

If $ARGUMENTS is empty, ask the user for:
1. The stem or phrase (e.g. `honest`, `shape`, `pressure test`).
2. The forms list (e.g. honest/honestly/honesty). For a phrase, list the conjugations you want flagged (e.g. test/tested/testing).
3. A one-sentence reason it's banned.
4. The substitute set (precise replacements).
5. Near-miss words that should also be rejected (the "Not …" list).

Then:

1. Read `~/rust/nate_style/rust/forbidden-words.md` to confirm the entry isn't already present.

2. **Single-word stem vs. multi-word phrase — they wire up differently.** `find_violations` in `~/.claude/scripts/hooks/banned_words_lib.py` routes on whether the `### "<heading>"` contains whitespace:
   - **Whitespace in the heading → literal phrase matcher** (`\s+` between tokens) and the `regex:` line is *ignored*. This catches only the exact spaced spelling — not the hyphenated form, not conjugations.
   - **No whitespace in the heading → stem matcher**, which *does* honor the `regex:` override.

   So a multi-word phrase must use a **hyphen-joined heading** for its regex to fire.

3. **Single-word stem.** The default matcher strips a trailing silent `e` and matches `\b\w*<root>\w*\b` (case-insensitive) — fine for most stems. Add a custom `regex:` line only when the default root would collide with unrelated common English words (e.g. a 3-letter root that appears inside ordinary words); propose a regex matching only the listed forms with `\b` boundaries and confirm with the user.

4. **Multi-word phrase — always block both the spaced and the hyphenated spelling. Do this by default; do not ask.**
   - Heading: hyphen-join the tokens — `### "pressure-test"` (NOT `### "pressure test"`), so the regex is honored.
   - `regex:` line (required): join the tokens with `[\s-]+` so a space *or* a hyphen between words both match, add `\b` boundaries, and include suffix alternations for the conjugations the user wants — e.g. `\bpressure[\s-]+test(s|ed|ing)?\b`.
   - Forms line: list both spellings (e.g. `pressure test, pressure-test, pressure tested, pressure testing`).

5. Append the new section before the `### Review pass` section, in this format:

```
### "<heading>"

regex: <pattern>          # required for phrases; for single stems only when the default collides

Forms: <forms>. <one-sentence reason>.

Substitute: <substitutes> — or delete. **Not** <near-misses>.
```

The `regex:` line is read by `load_overrides` in `banned_words_lib.py`.

6. **Register the pattern in the `pre_filter:` frontmatter** (pipe-separated, appended after the last alternative). This is the gate the clean-fix LLM style-eval pass uses to decide whether to evaluate a project — without it the new word is invisible to that subsystem. Use the same alternation as the `regex:` line minus the `\b` anchors, e.g. append `|pressure[\s-]+test(s|ed|ing)?`.

   **Never put a literal `'` (apostrophe) in the pre_filter alternation.** The `pre_filter:` value is a single-quoted YAML scalar; a literal apostrophe breaks Obsidian's frontmatter parser (a lone `'` is read as the closing quote). If a form needs to match an apostrophe (e.g. `you're`, `one's`), encode it as `\x27` in the pre_filter line — ripgrep reads `\x27` as an apostrophe, so matching is unchanged. The `regex:` line in the body section is *not* YAML and may keep the literal `'`.

7. If the new stem has a global exemption phrase (e.g. a domain term that should always be allowed), also add it to the `exceptions:` frontmatter line.

8. Bump `date_modified` in the frontmatter to today.

9. Confirm to the user: heading added, both spellings blocked, local counter starts at 0 on first hook hit, hooks pick it up immediately on next run (no restart — the lib re-reads the file each invocation).

10. Do **not** commit the change. Per the user's global rule, never commit unless explicitly asked.

If the stem already exists, tell the user and ask whether they want to amend the existing entry instead.
