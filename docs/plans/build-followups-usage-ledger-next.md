# Usage ledger: token use by role and model, kept over time — Next


## Items to consider

- [ ] **A custom metric that looks at every turn separately prints as fast as the built-in ones**
  - Target: `scripts/usage/usage_report.py`, the general per-turn path
  - Why needed: such a metric takes 8 to 15 s on natedev's 1.2 million turns, against 2.3 to 2.8 s for every built-in metric and a 10 s limit; none of the ten built-in metrics uses that path today
  - Completion condition: a per-turn metric on the full store prints well inside the 10 s limit
  - Revealed by: Phase 7

2026-11-11: Dailies retirement removed the proposed dailies usage section.
