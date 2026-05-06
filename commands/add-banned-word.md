---
description: Add a new banned word to the global style guide
argument-hint: <stem> [substitute1, substitute2, ...]
---

<!-- allow-banned: this command authors entries in the banned-words list and must name banned stems by example -->

Append a new banned-word section to `~/rust/nate_style/rust/forbidden-words.md`.

Arguments: $ARGUMENTS

If $ARGUMENTS is empty, ask the user for:
1. The stem or phrase (e.g. `honest`, `shape`, `plain English`). A value with whitespace is treated as a phrase — literal case-insensitive match, no form-folding.
2. The forms list (e.g. honest/honestly/honesty). For a phrase, list the variants you actually want flagged.
3. A one-sentence reason it's banned.
4. The substitute set (precise replacements).
5. Near-miss words that should also be rejected (the "Not …" list).

Then:

1. Read `~/rust/nate_style/rust/forbidden-words.md` to confirm the entry isn't already present.
2. **Decide if a custom regex is needed.** For a single stem the default matcher strips a trailing silent `e` and matches `\b\w*<root>\w*\b` (case-insensitive). That is fine for most stems. A custom `regex:` line is required when the default root would collide with unrelated common English words — for example, a stem that strips to a 3-letter root which appears as a substring inside many ordinary words. If in doubt, ask the user; otherwise propose a regex that only matches the listed forms (with `\b` word boundaries) and have the user confirm. For a phrase entry the default phrase matcher (literal, case-insensitive, `\s+` between tokens, `\b` on word-character edges) is almost always correct — a `regex:` line should be rare.
3. If absent, append a new section before the `### Review pass` section, in this format:

```
### "<stem>" — counter: 0

regex: <pattern>          # optional — include only if a custom matcher is needed (see step 2)

Forms: <forms>. <one-sentence reason>.

Substitute: <substitutes> — or delete. **Not** <near-misses>.
```

The `regex:` line is read by `~/.claude/scripts/hooks/banned_words_lib.py` (`load_overrides`). Omit it entirely when the default matcher suffices — there is no placeholder.

4. If the new stem has a global exemption phrase (e.g. domain term that should always be allowed), also add the phrase to the `exceptions:` frontmatter line.
5. Confirm to the user: stem added, counter starts at 0, hooks will pick it up immediately on next run (no restart needed — the lib re-reads the file each invocation).
6. Do **not** commit the change. Per the user's global rule, never commit unless explicitly asked.

If the stem already exists, tell the user and ask whether they want to amend the existing entry instead.
