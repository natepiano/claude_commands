---
description: Show forbidden-word hit counts and last-triggered timestamps
---

Show the local banned-word counter report.

## Step 1 — interpret the user's intent into a sort mode

Read `$ARGUMENTS` (free-form words) and pick ONE sort mode. Do not forward
`$ARGUMENTS` to the script verbatim — the script only recognizes specific
tokens, so raw phrases like "latest trigger" silently fall back to count-sort.

- **By recency** — if the words gesture at time/recency in any phrasing:
  "last triggered", "latest", "latest trigger", "recent", "most recent",
  "newest", "by time", "when", etc. → run:

  ```bash
  python3 ~/.claude/scripts/hooks/banned_words_lib.py --analysis last-triggered
  ```

  This sorts newest-first; never-triggered words sort last.

- **By count** — the default. If the words gesture at frequency ("count",
  "most", "top", "frequency", "worst offenders") or are empty/ambiguous → run:

  ```bash
  python3 ~/.claude/scripts/hooks/banned_words_lib.py --analysis
  ```

  This sorts by count, descending.

The script path is intentionally `banned_words_lib.py`; the hook introspection
bypass recognizes it, so the report and its output are not scanned again.

## Step 2 — format the output as a table

The script prints a fixed-width text report that truncates in the terminal.
Do NOT paste it raw. Parse the `word / count / last_triggered_at` rows and
re-render them as a GitHub-flavored markdown table, preserving the sort order
the script produced:

```
| Word | Count | Last triggered |
|------|------:|----------------|
| <word> | 560 | 2026-06-01 22:45 |
| ...    | ... | ...              |
```

Then add one or two lines of commentary on what stands out (the top offender,
any recent cluster, words that have never fired).

## Step 3 — render and send the color gradient image

Always also produce the white→red gradient image (color maps to count: red =
max, white = min; log scale). Pass the SAME sort mode chosen in Step 1
(`--sort recency` for the by-recency case; omit `--sort` for count). Terminals
here don't render ANSI color, so the PNG is the only way to show the gradient.

```bash
uv run ~/.claude/scripts/banned_word_analysis/banned-word-gradient.py --sort recency   # recency case
uv run ~/.claude/scripts/banned_word_analysis/banned-word-gradient.py                  # count case
```

`uv` auto-installs Pillow from the script's inline metadata, but its cache
write is blocked by the sandbox — run this command with the sandbox disabled.
The script prints the output PNG path. Then:

1. Send that file to the user with SendUserFile (the clickable attachment).
2. Also open it in Preview so it surfaces without a click — terminals don't
   render images inline, so the macOS viewer is the most direct view:

   ```bash
   open "$PNG_PATH"   # the path the script printed; run with sandbox disabled
   ```

Do not edit the style guide or counter state.
