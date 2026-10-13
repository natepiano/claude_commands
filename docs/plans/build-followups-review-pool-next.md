# review-pool — Next

## Items to consider

- [ ] **The trial report counts a merge hold only for the plan it belongs to.**
  - Target: `scripts/delegate/review_pool_report.py` — the join of `review_regime.py` ledger rows to review records, with its tests
  - Why needed: each ledger row gains `plan`, the unit's plan path as the production doc names it, so a unit's second plan records its own phase 1; a join on unit and phase alone then sums the other plan's holds into that phase
  - Completion condition: where a ledger row and a record both carry a plan, the join also matches on it; a row from before the change carries none and keeps matching on unit and phase; tests cover a unit with two plans and an older row
  - Depends on: showrunner-fixer's ledger change `b2c983f0` on the merge branch; before starting, `git merge-base --is-ancestor b2c983f0 build-followups` must succeed
  - Ledger row form (the showrunner, 2026-10-12): `plan` is a string, the first token of the unit's Plan cell as the production doc writes it, for example `docs/plans/alpha.md`; a row written before 2026-10-12 has no `plan` key and `review_regime.read_rows()` gives `None` for it; a row is named by unit, plan and phase
  - Revealed by: the showrunner, 2026-10-12 (first in order, ahead of the item below)

- [ ] **The showrunner gets a unit's review history location from one command at each merge.**
  - Target: `scripts/production/` — a verb that prints a unit's newest run id and session directory for its Worktree cell
  - Why needed: at every merged checkpoint the showrunner reads run records by hand to fill the `review merge:` line for the review unit; a script can do that lookup with no tokens spent
  - Completion condition: the showrunner fills the merge line's session directory from one command's output, with a test, and `commands/showrunner/produce.md` names that command where it now describes the lookup
  - Revealed by: Phase 5
