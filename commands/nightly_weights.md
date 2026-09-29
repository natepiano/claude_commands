---
description: Recompute the nightly review's rust target weights from the last 90 days of commits, and apply them on the user's yes.
argument-hint: "[days, default 90]"
---

1. Run `python3 ~/.claude/scripts/nightly_review/weights.py --days N`, N being `$ARGUMENTS` or 90 when empty, and show the user its table. Hana's share is fixed by the user; the other targets split the rest by the square root of their commits, at least 1 each (`weights.py` docstring).
2. If it prints `unchanged`, stop there. Otherwise ask whether to apply the proposal.
3. On yes: run it again with `--apply`, then commit only `commands/nightly_review.md` in `~/.claude` and push.
