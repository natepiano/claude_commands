# Review regime trial — results (closed 2026-10-05)

**Question:** do extra review seats pay for themselves? From 2026-10-01 (user decision), every phase that changes the screen got a UX reviewer, and every phase got a code-quality reviewer (the `craft` lens). Both judged by the three gods (`docs/decision_criteria.md`). On 2026-10-04 the user dropped the code-quality reviewer and kept the UX reviewer. A 12-phase watch then measured the result. The user acknowledged it on 2026-10-05.

**Data:** `review-regime-trial.jsonl` beside this file is the full ledger as of the acknowledgment: 122 rows from the production logs, one per merged phase. The live ledger is `~/.claude/data/review_regime.jsonl`, which is not in git. `scripts/production/review_regime.py report` recomputes the table from either file.

**Method:**
- `before` = phases before 2026-10-01; `trial` = both extra reviewers; `after` = UX reviewer only.
- `holds` = checkpoints the showrunner held.
- `merge-defects` = defect rows that the merge design check found in the phase's own work.
- Phases the comparison cannot use carry an `excluded` reason. These were marked from the logs on 2026-10-05, after build-tool phases skewed the first count: 46 changed nothing on screen, 17 had no merge design check, 3 had no check in the log, and 1 had no defect count. The table counts only the rest.

| | before | trial | after |
| --- | --- | --- | --- |
| phases merged | 9 | 29 | 12 |
| holds per phase (mean) | 1.3 | 0.2 | 0.1 |
| merge design-check defects per phase (mean) | 8.0 | 3.2 | 2.8 |
| hours, start to merge (median) | 13.9 | 8.1 | 6.3 |
| review-seat minutes per phase (mean) | - | 62.0 | 48.7 |
| screenshot check minutes per phase (mean) | - | 38.0 | 25.1 |
| repair minutes after its findings per phase (mean) | - | 211.3 | 111.6 |
| UX reviewer findings per phase (mean) | - | 5.7 | 2.5 |
| code reviewer findings per phase (mean) | - | 4.1 | 0.3 |

**Learning:**
- **The extra review seats paid for themselves.** Against `before`, review seats cut holds by about 6x and design-check defects by 2.5–3x, and phases merged in roughly half the time.
- **The code-quality reviewer added nothing measurable on top of the UX reviewer.** Without it, every quality figure held or improved, and phases merged about 2 h faster.
- **Keep the UX reviewer; leave the code-quality seat off.**

**Limits:**
- The samples are small (9 / 29 / 12).
- `after` phases came later in mature plans.
- Exclusions were judged from logs, not re-measured.

**To re-run the comparison:** append rows with `review_regime.py add` and read `report`. The watch is acknowledged, so `watch` no longer alerts.
