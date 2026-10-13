# review-pool — Next

## Items to consider

- [ ] **The trial report counts a merge hold only for the plan it belongs to.**
  - Target: `scripts/delegate/review_pool_report.py` — the join of `review_regime.py` ledger rows to review records, with its tests
  - Why needed: each ledger row gains `plan`, the unit's plan path as the production doc names it, so a unit's second plan records its own phase 1; a join on unit and phase alone then sums the other plan's holds into that phase
  - Completion condition: the code-review quality table derives each review's plan from the `run_started` event's `plan_doc` and `working_dir` in its recorded `history_file`, and matches plan-bearing ledger rows on unit, phase and that plan, resolving relative and absolute paths consistently. Ledger rows without a plan retain the unit-and-phase match. When a plan-bearing row cannot be matched because the review's plan is unavailable, omit its holds and print a source warning. Tests cover two plans with the same unit and phase, an older ledger row, equivalent relative and absolute paths, and unavailable review-plan metadata
  - Where the plan comes from: neither the launcher's turn record nor `ReviewRecord` carries a plan; the run history the report already loads does (`progress_history.py` writes `plan_doc` and `working_dir` on `run_started`). The report reads the ledger's raw JSON rows, not `review_regime.read_rows()`, so a row from before the ledger change is one with no `plan` key
  - Depends on: showrunner-fixer's ledger change `b2c983f0` on the merge branch; before starting, `git merge-base --is-ancestor b2c983f0 build-followups` must succeed
  - Ledger row form (the showrunner, 2026-10-12): `plan` is a string, the first token of the unit's Plan cell as the production doc writes it, for example `docs/plans/alpha.md`; a row written before 2026-10-12 has no `plan` key and `review_regime.read_rows()` gives `None` for it; a row is named by unit, plan and phase
  - Revealed by: the showrunner, 2026-10-12 (first in order, ahead of the item below)

- [ ] **The showrunner gets a unit's review history location from one command at each merge.**
  - Target: `scripts/production/` — a verb that prints a unit's newest run id and session directory for its Worktree cell
  - Why needed: at every merged checkpoint the showrunner reads run records by hand to fill the `review merge:` line for the review unit; a script can do that lookup with no tokens spent
  - Completion condition: the showrunner fills the merge line's session directory from one command's output, with a test, and `commands/showrunner/produce.md` names that command where it now describes the lookup
  - Revealed by: Phase 5

- [ ] **A pooled design check stops re-reading the design rules on every turn.**
  - Target: `commands/unit/direct.md` <UXReview/> step 3, the prompt sent to the pooled UI reviewer, and `config/review_pool/ui.md`
  - Why needed: every UI turn starts from a reset thread whose standing instructions already hold the checklist and the report format, yet the shared design-check prompt opens with "Read `~/.claude/commands/ux_eval.md` and follow it" and names each rule file by path, so the reviewer reads them all again on its turn; the saving the pool exists for is lost on each check
  - Completion condition: the prompt sent to a pooled UI reviewer names no file to read and says the standing instructions hold the format; the Claude helper's prompt is unchanged; one live shadow check in a scratch pool shows no read of `ux_eval.md` or a rule file on the pool's turn and the same defect rows as the helper
  - Revealed by: Phase 6, the live shadow check; placed by the showrunner, 2026-10-12, after the two items above

- [ ] **The side-by-side report of design checks names the machine whose checks it counted.**
  - Target: `scripts/delegate/review_pool_report.py --shadow`, with its tests
  - Why needed: the report reads verdict pairs under the local history root only; every unit director runs on natedev today, so the count is complete, and one line naming the machine keeps the count from reading as complete if a unit on the Mac starts running design checks
  - Completion condition: the `--shadow` report prints one line naming the machine whose pairs it counted, with a test; reading a second machine's pairs stays out until a unit on the Mac runs design checks
  - Revealed by: Phase 6; the showrunner, 2026-10-12, in place of a two-machine reader
