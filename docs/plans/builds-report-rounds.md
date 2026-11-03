# Build improvement rounds

A round ranks one cost, states a prediction, fixes its cause, and measures the result.
The score is phase time: the wall time of a finished phase. Build hours per phase give a second measure.
Measure after at least 10 finished hana phases. Add one day when the result is not clear.
Keep one or two rounds active. Record whether each measured change stays or is undone.

| Round | Cost | Prediction | Owner | Status | Result |
| --- | --- | --- | --- | --- | --- |
| 1 | Memory wait | Gate grant sizing reduces memory wait and phase time. Phase 10e is this round's fix. | queue-unit 10e | open | In flight; measurement pending. |
| 1 | Mac worktree switches: hana test median 283 s after a different worktree, 84 s after the same | Placement and slot affinity reduce switch costs and phase time. | queue-unit 10g (placement), verify-unit (slot affinity) | open | Pending; the observed medians do not prove a cold cache. |
| 1 | Reruns after a pass: 3 h/day | Reuse of a valid pass reduces rerun hours and phase time. | verify-unit | open | Pending. |
| 1 | Cold rebuilds: 8.4 h of nextest time on 11-03 | Find the cause before predicting a fix. | Unnamed; report-unit finds the cause first | open | Pending. |
