# hierarchies — Next

## Items to consider

- [ ] **A second run of an answer within 48 hours of a production or unit rename repeats the first answer instead of failing**
  - Target: `scripts/production/rename.py`, `scripts/production/rename_state.py`, `scripts/production/update_registration.py` and their tests
  - Why needed: a production rename moves `outstanding/<slug>.answered.jsonl` but keeps the old slug in its records, and a unit rename keeps the old `asked_by`, so `answer` refuses those records (update_registration.py answer path) and the rerun fails instead of repeating its relay line.
  - Completion condition: tests run answer, then a production rename and a unit rename, then the same answer again within 48 hours; each rerun exits 0 and prints "already answered <time>" with the relay line for the current name.
  - Revealed by: Phase 14
