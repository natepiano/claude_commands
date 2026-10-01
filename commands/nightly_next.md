---
description: Present the one nightly-review proposal with the largest measured impact that is not yet implemented, decide it with the user, and record the outcome in the ledger.
---

LEDGER is `~/.local/state/nightly-review/ledger.md`; each night's reports are in `<night>/config.md` and `<night>/rust.md` beside it.

1. **Candidates.** Every LEDGER line ending `— proposed` or `— deferred`, plus every **Also found** and **Noted** item in every night's reports that LEDGER does not record as done, declined or relayed. Where a cheap check exists (its measurement script, the line it cites), confirm the candidate still holds; one already fixed is recorded done and dropped.
2. **Rank** by measured gain per week, from the report's own numbers: user-visible time (waiting, red CI, failed runs) first, then share of tokens. An unmeasured item ranks below every measured one.
3. **Present exactly one**, the top candidate; never list the backlog. Open with the line as it reads today next to the line as proposed (file:line), then the measured gain, then the cost. End with one choice line: `approve (recommended — reason) / decline / defer / elaborate`; a fix that belongs to another session offers `relay` in place of `approve`.
4. **Record** the outcome in LEDGER as soon as it is decided. A main proposal's `— proposed` becomes `— accepted <date>, applied (<repo> <sha>)`, `— declined` or `— deferred`. An Also-found item is appended to its night's line as `; from Also found: <title> — <outcome>`. Apply an approved change; commit and push only when the user says so. Present the next candidate only when asked.
