# hierarchies — Next

## Items to consider

- [ ] **An answer replay within 48 hours of a production or unit rename uses the current names**
  - Target: `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `scripts/production/rename_state.py`, `scripts/production/update_registration.py` and their tests
  - Why needed: a production rename moves answered history but keeps its old slug, so `answer` skips the replay; a unit rename keeps the old `asked_by`, so the replay succeeds with the former unit name in its relay line.
  - Completion condition: separate tests answer an item, rename its production or asking unit, then replay the same answer within 48 hours; each replay exits 0, prints "already answered <time>" with the original answer time, and names the current relay destination.
  - Revealed by: Phase 14
