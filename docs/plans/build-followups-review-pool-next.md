# review-pool — Next

## Items to consider

- [ ] **The showrunner gets a unit's review history location from one command at each merge.**
  - Target: `scripts/production/` — a verb that prints a unit's newest run id and session directory for its Worktree cell
  - Why needed: at every merged checkpoint the showrunner reads run records by hand to fill the `review merge:` line for the review unit; a script can do that lookup with no tokens spent
  - Completion condition: the showrunner fills the merge line's session directory from one command's output, with a test, and `commands/showrunner/produce.md` names that command where it now describes the lookup
  - Revealed by: Phase 5
