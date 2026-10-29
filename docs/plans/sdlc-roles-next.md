# sdlc-roles — Next

## Items to consider

- [ ] **A push to the user's phone shows who sent it as production · unit · job**
  - Owner: messaging-unit (owns `scripts/message/user_channel.py` and its push text review; showrunner, 2026-10-28)
  - Target: `scripts/message/user_channel.py`, `scripts/message/send.py`
  - Why needed: `send.py` passes the address as `user_channel.py --source`, which keys silence and dedupe, so a user push shows the address, not the R5b shown name
  - Completion condition: a user push shows the sender's shown name (`production · unit · job`) in a display field, while silence and dedupe still key on the address; the push texts pass their text review
  - Revealed by: Phase 5

- [ ] **The unit board and cargo-liner show each session's job from the shared names file, and an empty job cell gives its reason**
  - Owner: the unit-board owner and cargo-liner (showrunner, 2026-10-29: it sends them the contract and done-test when phase 6 merges)
  - Target: the unit board's job column, cargo-liner's job column
  - Why needed: both read session names, not the roster's `job.display`, so a configured display rename does not reach them
  - Completion condition: each owner has a fixture test that a configured display rename changes the job column without changing session identity, and that unrecorded and unreadable assignments show `job.reason`
  - Revealed by: Phase 6
