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
| 1 | Cold rebuilds: 1,517 natedev steps and 260.28 wall hours over seven days ending 2026-11-04 01:21 UTC. Temporary clones and gate copies hold 829 of them (167.35 h, 64%): each new copy builds every step kind cold in its own target folder. | Move gate copies onto standing warm checkouts (verify-unit P18). Then report the cold copies that remain. Predict up to half of the 167.35 h/week saved; the 116 same-kind steps with unchanged build_base (26.70 h) are a second, separate fix. Measure phase time after ten hana phases. | verify-unit phase 18 (copies.py: standing warm checkouts per owner and repository); set by builds 2026-11-03 | open | Prior event of each cold step: command change 896 steps/157.39 h (776 of them a different step kind in the same folder); branch/SHA change 209/35.38 h; new folder 211/36.10 h; rustc change 107/23.83 h (102 after nightly formatting, not a cache loss); sweep_trim 77/5.05 h; none found 17/2.52 h. 1,399 of the 1,517 records lack target_dir, so folders are inferred. The costs command's 0.32 h is per finished phase of all kinds; per hana phase it is 3.8 h. |
