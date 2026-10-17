# Style showrunner — Next

## Items to consider

- [ ] **A production two levels up also shows its grandchild style production's line in an expanded dailies view**
  - Target: scripts/production/dailies_input.py, scripts/production/dailies_render.py (expand of a middle production)
  - Why needed: a middle production's saved report does not carry its style child's summary, so the grandparent's `expand` shows no style line under the middle production's style child.
  - Completion condition: the grandparent's expand shows the same style line under the middle production's style child that the middle production's own dailies shows.
  - Revealed by: Phase 13
