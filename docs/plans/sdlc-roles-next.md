# sdlc-roles — Next

## Items to consider

- [ ] **A push to the user's phone shows who sent it as production · unit · job**
  - Owner: messaging-unit (owns `scripts/message/user_channel.py` and its push text review; showrunner, 2026-10-28)
  - Target: `scripts/message/user_channel.py`, `scripts/message/send.py`
  - Why needed: `send.py` passes the address as `user_channel.py --source`, which keys silence and dedupe, so a user push shows the address, not the R5b shown name
  - Completion condition: a user push shows the sender's shown name (`production · unit · job`) in a display field, while silence and dedupe still key on the address; the push texts pass their text review
  - Revealed by: Phase 5
