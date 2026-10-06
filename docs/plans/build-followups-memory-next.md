# Build follow-ups: builds that fit in memory — Next

## Items to consider

- [ ] **Record whether a cargo-token wait got the token or gave up**
  - Target: `scripts/delegate/verify.sh` and the build log (`scripts/buildlog/record.py`, `index.py`, `report.py`)
  - Why needed: `calls.token_wait_s` stores only the seconds waited, so a 30-minute wait that got the token and one that gave up and built without it ("waited for the cargo token and did not get it; running anyway") look the same, and the report cannot show builds that overlapped
  - Completion condition: each call records how its token wait ended (got it, gave up, or no token used), and the day's report counts the builds that ran without the token
  - Revealed by: Phase 9
