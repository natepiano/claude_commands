---
description: Undo the most recent banned-word counter bump from its backup
---

Roll the local banned-word counter file back to its backup — the snapshot taken
just before the most recent write. Use this when a hit was recorded that should
not count (e.g. a message that quoted a banned stem as an example rather than
using it).

## Step 1 — revert

```bash
python3 ~/.claude/scripts/hooks/banned_words_lib.py --revert
```

The script copies `forbidden-word-counts.json.bak` back over the live counter
file under the counter lock, then prints the restored totals. The script path is
intentionally `banned_words_lib.py`; the hook introspection bypass recognizes
it, so neither the command nor its output is scanned for banned words.

Outcomes:

- **Reverted** — exit 0. Confirm the revert succeeded and that the intended
  bump was undone. **Do NOT paste, quote, or summarize the restored-totals line
  the script printed** — it lists every banned stem as a key, and the Stop hook
  scans the prose *you* emit, so reproducing those keys would re-bump the
  counters (including the one you just reverted). The script's own stdout is
  exempt via the introspection bypass; your reply about it is not. Describe the
  outcome without naming any banned word — e.g. "the entry you flagged no longer
  appears in the restored totals" or "its count is back to N".
- **No backup found** — exit 2. Report that there was nothing to restore. This
  happens before the counter file's second-ever write (the first write has no
  prior state to snapshot), or if the backup was deleted.

## Step 2 — flag the one-write-behind window

The backup is refreshed on every counter write, so a revert only rewinds a
single step. If another flagged message landed after the one being undone, the
backup now holds that intermediate state and this revert will not reach the
earlier total. If the restored totals do not match what the user expected, say
so plainly rather than reverting again — a second revert is a no-op, since the
live file already equals the backup.

Do not edit the style guide or hand-edit the counter state.
