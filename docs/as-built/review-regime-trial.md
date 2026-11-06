# Review regime records

## What it is

The review records connect each merged phase to its findings, review time, merge holds, and merge defects.
`scripts/production/review_regime.py` writes `~/.claude/data/review_regime.jsonl` and reports means by regime.
The `simplify` lens has separate findings and minutes fields.
[Simplify review lens](simplify-lens.md) defines its launch and decision rules.

## How it works

`progress_history.py review-trial --session-dir <run> [--phase-id <id>]` reports one phase instance.
A named phase selects its open instance or its latest completed instance.
Without `--phase-id`, the command selects the current phase, including a completed phase.

Completed review records supply code and simplify finding counts through `_raised_findings`.
The records live under `history_root()/review_turns/<run>/`.
`CodeReviewEvidence` is `NoRecordedCodeReview | RecordedCodeFindings`.
`RecordedCodeFindings(0)` records a completed review with no findings.
`NoRecordedCodeReview` makes the count use `finding_opened` events instead.

| Field | Source |
| --- | --- |
| UX findings | `finding_opened` events that name `ux`. |
| Code findings | Completed review records, excluding `ux` and `simplify`; otherwise matching ledger events. |
| Review-worker minutes | Finished review passes except `direct.simplify`, plus UX review activities. |
| UX check minutes | Finished `UX review` activities. |
| UX repair minutes | Finished fix passes whose dispatched batch includes a UX finding. |
| Simplify findings | Completed simplify review records; otherwise ledger events that name `simplify`. |
| Simplify minutes | Nonnegative elapsed time from all simplify review records, including failed runs. |

The recorder rounds each time to the nearest whole minute.
A ledger finding with several lenses contributes to each applicable count.
A simplify-only finding does not contribute to the code count.

The line has this form:

```text
review trial: ux <N> findings, code <N> findings, review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>[, simplify <S> findings, simplify minutes <T>]
```

The simplify suffix appears when the phase has simplify records or findings.
`scripts/production/merge_checkpoint.py` parses it with `review_numbers(line) -> ReviewTrial`.
`ReviewTrial.simplify` is `SimplifyReviewMetrics(findings, minutes) | NoSimplifyReview`.
The parser accepts lines without the suffix.
The checkpoint script passes the present fields to `review_regime.py add`.

`review_regime.Row` holds the unit, plan, phase, regime, times, holds, merge defects, findings, review minutes, and optional notes.
The regime is `before`, `trial`, or `after`.
`add` requires times with UTC offsets and refuses a merge time before the start time.
It keeps an existing row for the same unit, plan, and phase.
Missing simplify fields remain absent when it writes the row.

`report [--since <ISO date>]` excludes rows with an `excluded` reason.
It reports means for findings, holds, defects, and review times.
It reports the median elapsed hours from start to merge.
Simplify report rows appear only when the selected records contain those fields.
A phase with zero UX check minutes receives `excluded: no visible change` unless a reason is supplied.

## Calibration / gotchas

The tracked `review-regime-trial.jsonl` contains a fixed data sample.
The live ledger remains outside Git.
Rows without a plan field do not identify the same key as rows that supply a plan.
Missing metric fields do not mean zero.
Older code counts can describe only the `craft` reviewer; current counts cover code reviews excluding UX and simplify.
Compare records with the same count definition.

`watch` waits for 12 eligible `after` phases, then returns 3 until `ack` records the user's acknowledgment.
An acknowledged watch returns 0.

## Why

Separate simplify fields keep its findings and cost distinct from code review.
Phase-instance selection keeps reopened phases separate.
A recorded zero preserves evidence that a review completed.
The row key lets one unit run several plans without merging their phase numbers.
