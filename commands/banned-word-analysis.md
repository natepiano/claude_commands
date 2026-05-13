---
description: Show forbidden-word hit counts and last-triggered timestamps
---

Run the local counter report:

```bash
python3 ~/.claude/scripts/hooks/banned_words_lib.py --analysis
```

The script path is intentionally `banned_words_lib.py`; the hook introspection
bypass recognizes it, so the report and its output are not scanned again.

Report the output directly. Do not edit the style guide or counter state.
