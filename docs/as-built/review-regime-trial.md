# Review regime records

## What it is

The review records connect each merged phase to its findings, review time, merge holds, and merge defects.
`scripts/production/review_regime.py` writes `~/.claude/data/review_regime.jsonl` and reports means by regime.

## How it works

`progress_history.py review-trial --session-dir <run> [--phase-id <id>]` reports one phase instance.
A named phase selects its open instance or its latest completed instance.
Without `--phase-id`, the command selects the current phase, including a completed phase.

The phase's `finding_opened` events supply the finding counts.
`findings.py open` writes one event for each ledger entry with its phase instance, finding id, and lens.
The recorder counts each code finding id once, including routed and closed findings.
Review records and closure replies do not change the counts.

| Field | Source |
| --- | --- |
| UX findings | `finding_opened` events that name `ux`. |
| Code findings | `finding_opened` events with no lens or any lens other than `ux`. |
| Review-worker minutes | Finished review passes and UX review activities. |
| UX check minutes | Finished `UX review` activities. |
| UX repair minutes | Finished fix passes whose dispatched batch includes a UX finding. |

The recorder rounds each time to the nearest whole minute.
A ledger finding with several lenses contributes to each applicable count.

The line has this form:

```text
review trial: ux <N> findings, code <N> findings, review-worker minutes <M>, ux check minutes <U>, ux repair minutes <R>
```

`scripts/production/merge_checkpoint.py` parses it with `review_numbers(line) -> ReviewTrial`.
The checkpoint script passes the fields to `review_regime.py add`.
The reader ignores unknown keys in old rows, including former simplify fields.

`review_regime.Row` holds the unit, plan, phase, regime, times, holds, merge defects, findings, review minutes, and optional notes.
The regime is `before`, `trial`, or `after`.
`add` requires times with UTC offsets and refuses a merge time before the start time.
It keeps an existing row for the same unit, plan, and phase.

`report [--since <ISO date>]` excludes rows with an `excluded` reason.
It reports means for findings, holds, defects, and review times.
It reports the median elapsed hours from start to merge.
A phase with zero UX check minutes receives `excluded: no visible change` unless a reason is supplied.

## Calibration / gotchas

The tracked `review-regime-trial.jsonl` contains a fixed data sample.
The live ledger remains outside Git.
Rows without a plan field do not identify the same key as rows that supply a plan.
Missing metric fields do not mean zero.
Older code counts can describe one reviewer or totals from review records; current counts use ledger opening events.
Compare records with the same count definition.

`watch` waits for 12 eligible `after` phases, then returns 3 until `ack` records the user's acknowledgment.
An acknowledged watch returns 0.

## Why

Phase-instance selection keeps reopened phases separate.
Ledger opening events preserve findings after routing and closure.
The row key lets one unit run several plans without merging their phase numbers.
