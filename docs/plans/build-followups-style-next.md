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
  - Completion condition: an edit to only a rule's `input_scope:` line, with the `date_modified:` line that every rule edit changes, leaves that rule's current cells current; an edit to its body or to judging frontmatter, `pre_filter:` included (it holds forbidden-words' word list), still turns them stale.
  - Revealed by: Phase 17 (ad hoc review of the pilot results, showrunner disposition next, 2026-10-18)

- [ ] **A declined style decision can be answered again with a command, not a hand edit**
  - Target: scripts/style/style_proposals.py (decision answers)
  - Why needed: `style_proposals.py unwaive` refuses a finding whose decision is declined, and the only way to change a declined answer is a hand edit of the decision's frontmatter.
  - Completion condition: one command changes a declined decision's answer to approved or to no answer, and removes `style_answer` and `style_answer_words`; an answer of none also reopens the decision, because a closed decision with no answer and the reason `declined: <words>` refuses later findings (phase 31). After it, unwaive takes that decision's findings and the next file puts them in the decision.
  - Revealed by: Phase 26 (showrunner disposition next, 2026-10-20)

- [ ] **A trait method finding inside a test module that reads its parent with `use super::*` follows its method instead of waiting for a person**
  - Target: scripts/style/style_ledger.py (trait import reader, `_trait_import_path`)
  - Why needed: three cargo-liner rows (`impl Subscriber for GitStatusTrace` inside checkout.rs `mod tests`, which has `use super::*`) stay "trait identity uncertain" after phase 34, because a block glob can bind the trait name. Resolving the glob from the parent module's proved items and imports would let them follow.
  - Completion condition: a dry-run follow on a copy of cargo-liner's ledger shows those three rows in place; a glob whose parent cannot be fully read still keeps the row.
  - Revealed by: Phase 34 (showrunner disposition next, 2026-10-20)
