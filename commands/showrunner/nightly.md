---
description: List the nightly review's open improvements in one table - largest measured gain per week first, with the one to do next marked recommended.
---

# Nightly

Run this in the showrunner session. Run
`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/nightly_review/candidates.py"`
and print its output exactly as it comes, with nothing added. It prints one
Markdown table, `| Pick | Night | Area | Candidate | Gain per week | State |`,
of every improvement the nightly review found that is still open: each line of
`~/.local/state/nightly-review/ledger.md` ending `proposed` or `deferred`.

- **Order**: measured gain per week, user-visible time (waiting, red CI,
  failed runs) first, then share of tokens. A candidate with no measured gain
  comes after every measured one, in the ledger's order.
- **Pick**: the first row reads `recommended` when its gain is measured. It is
  the candidate `/efficiency_next` presents first.
- **Area**: `config` or `rust/<target>`. `, also found` and `, noted` mark a
  smaller finding of that night.

With nothing open it prints `no open candidates`. The script rewrites
`~/rust/hanadocs/nightly improvement list.md` (`NIGHTLY_REVIEW_NOTE` replaces
the path) and changes nothing else. The ledger is kept on natedev, so run it
there. To act on a row, use
`/efficiency_next`.
