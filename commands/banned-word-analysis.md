---
description: Show forbidden-word hit counts and last-triggered timestamps
---

Run the local counter report. Default sort is by count, descending. If
`$ARGUMENTS` mentions "last triggered", pass `last-triggered` to sort oldest
first instead:

```bash
python3 ~/.claude/scripts/hooks/banned_words_lib.py --analysis $ARGUMENTS
```

The script path is intentionally `banned_words_lib.py`; the hook introspection
bypass recognizes it, so the report and its output are not scanned again.

Report the output directly. Do not edit the style guide or counter state.
