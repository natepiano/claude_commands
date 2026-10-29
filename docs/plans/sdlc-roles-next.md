# sdlc-roles — Next

## Items to consider

- [ ] **Cover stderr heredocs before the fixed-display check** — taken into Phase 6 (Names that can change live in one config file); remove this item at that phase's checkpoint
  - Target: `scripts/production/roles_audit.py`, `scripts/production/test_roles_audit.py`
  - Why needed: `cat >&2 <<EOF` output escapes extraction, so Phase 6's fixed-display check could pass while a script prints a fixed display name there
  - Completion condition: fixtures prove stderr `cat` heredocs count in Names and report retired or fixed display names at physical source lines, while non-output heredocs stay excluded; record the extraction change apart from the frozen Phase 3 lexical measure
  - Revealed by: Phase 1; required by Phase 6

- [ ] **A push to the user's phone shows who sent it as production · unit · job**
  - Owner: messaging-unit (owns `scripts/message/user_channel.py` and its push text review; showrunner, 2026-10-28)
  - Target: `scripts/message/user_channel.py`, `scripts/message/send.py`
  - Why needed: `send.py` passes the address as `user_channel.py --source`, which keys silence and dedupe, so a user push shows the address, not the R5b shown name
  - Completion condition: a user push shows the sender's shown name (`production · unit · job`) in a display field, while silence and dedupe still key on the address; the push texts pass their text review
  - Revealed by: Phase 5
