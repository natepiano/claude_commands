# Style showrunner — Next

## Items to consider

- [ ] **A production two levels up also shows its grandchild style production's line in an expanded dailies view**
  - Target: scripts/production/dailies_input.py, scripts/production/dailies_render.py (expand of a middle production)
  - Why needed: a middle production's saved report does not carry its style child's summary, so the grandparent's `expand` shows no style line under the middle production's style child.
  - Completion condition: the grandparent's expand shows the same style line under the middle production's style child that the middle production's own dailies shows.
  - Revealed by: Phase 13

- [ ] **Changing which files a rule checks no longer makes all of that rule's earlier results out of date**
  - Target: scripts/style/style_ledger.py (rule version), scripts/style/rules.py (rule blob)
  - Why needed: the rule version is a hash of the whole rule file, so an edit to only the `input_scope:` frontmatter line turns every verdict of that rule stale and costs a full recheck.
  - Completion condition: an edit to only a rule's `input_scope:` line leaves that rule's current cells current; an edit to its body or to judging frontmatter still turns them stale.
  - Revealed by: Phase 17 (ad hoc review of the pilot results, showrunner disposition next, 2026-10-18)
