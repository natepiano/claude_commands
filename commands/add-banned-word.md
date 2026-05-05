---
description: Add a new banned word to the global style guide
argument-hint: <stem> [substitute1, substitute2, ...]
---

<!-- allow-banned: this command authors entries in the banned-words list and must name banned stems by example -->

Append a new banned-word section to `~/rust/nate_style/rust/forbidden-words.md`.

Arguments: $ARGUMENTS

If $ARGUMENTS is empty, ask the user for:
1. The stem (e.g. `honest`, `shape`).
2. The forms list (e.g. honest/honestly/honesty).
3. A one-sentence reason it's banned.
4. The substitute set (precise replacements).
5. Near-miss words that should also be rejected (the "Not …" list).

Then:

1. Read `~/rust/nate_style/rust/forbidden-words.md` to confirm the stem isn't already present.
2. If absent, append a new section before the `### Review pass` section, in this format:

```
### "<stem>" — counter: 0

Forms: <forms>. <one-sentence reason>.

Substitute: <substitutes> — or delete. **Not** <near-misses>.
```

3. If the new stem has a global exemption phrase (e.g. domain term that should always be allowed), also add the phrase to the `exceptions:` frontmatter line.
4. Confirm to the user: stem added, counter starts at 0, hooks will pick it up immediately on next run (no restart needed — the lib re-reads the file each invocation).
5. Do **not** commit the change. Per the user's global rule, never commit unless explicitly asked.

If the stem already exists, tell the user and ask whether they want to amend the existing entry instead.
