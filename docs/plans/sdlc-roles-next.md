# sdlc-roles — Next

## Items to consider

- [ ] **A push to the user's phone shows who sent it** (sender in the `production · unit · job` form, for example `sdlc · roles · code reviewer 6`; the user, 2026-10-29)
  - Owner: messaging-unit (showrunner, 2026-10-28)
  - Target: `scripts/notify/pushover.py`, `scripts/message/send.py`
  - Why needed: user sends skip sender-roster resolution, and `send.py` passes the raw sender as `pushover.py --source`, which also supplies the displayed sender
  - Completion condition: resolve the sender's configured roster shown name on the sending machine, carry it through delivery and silence replay, and show it on the phone, while the address still keys identity, silence and dedupe; fixture tests cover a display rename in config and a replay after the config changes; the push texts pass their text review
  - Revealed by: Phase 5

- [ ] **The unit board and cargo-liner show each session's job from the shared names file, and an empty job cell gives its reason**
  - Owner: the unit-board owner and cargo-liner (showrunner, 2026-10-29: it sends them the contract and done-test when phase 6 merges)
  - Target: the unit board's job column, cargo-liner's job column
  - Why needed: both read session names, not the roster's `job.display`, so a configured display rename does not reach them
  - Completion condition: each owner has a fixture test that a configured display rename changes the job column without changing session identity, and that unrecorded and unreadable assignments show `job.reason`
  - Revealed by: Phase 6
