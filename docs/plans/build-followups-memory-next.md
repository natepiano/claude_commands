# Build follow-ups: builds that fit in memory — Next

## Items to consider

- [ ] **Record whether a cargo-token wait got the token or gave up**
  - Target: `scripts/delegate/verify.sh` and the build log (`scripts/buildlog/record.py`, `index.py`, `report.py`)
  - Why needed: `calls.token_wait_s` stores only the seconds waited, so a 30-minute wait that got the token and one that gave up and built without it ("waited for the cargo token and did not get it; running anyway") look the same, and the report cannot show builds that overlapped
  - Completion condition: each call records how its token wait ended (got it, gave up, or no token used), and the day's report counts the builds that ran without the token
  - Revealed by: Phase 9

- [ ] **Skipped launch results stay findable**
  - Target: `scripts/buildlog/launches.py` and the day's report
  - Why needed: a `brp_launch` result skipped for missing launch facts or a missing location is counted only in the pass that met it; once the transcript offset moves on, nobody can find which result was lost
  - Completion condition: each skipped result is kept with its transcript and byte position, and a fixture test finds it after a second collection
  - Revealed by: Phase 12

- [ ] **Say why a launch has no repository or commit**
  - Target: `scripts/buildlog/record.py` (`GitFacts`) and `scripts/buildlog/launches.py`
  - Why needed: four nullable fields stand for four different conditions (no repository, worktree gone, commit read at collection time, launch-time commit no longer available), so an empty field cannot say which one happened
  - Completion condition: the collector turns the lookup into named resolved and unresolved repository states and tells a commit read at collection time from a launch-time one that is gone, with a test for each
  - Revealed by: Phase 12

- [ ] **Count launches that run inside subagents**
  - Target: `scripts/buildlog/launches.py` (`collect()` and its one-level `glob("*/*.jsonl")`)
  - Why needed: on the measured day 11 of 37 BRP launches ran in subagent transcripts and never reached `steps`, so every later day undercounts launches and their memory
  - Completion condition: the collector reads nested subagent transcripts, records each launch once when its result line appears twice, and a fixture test with a nested transcript and a duplicate result finds exactly the launches present
  - Revealed by: Phase 18

- [ ] **Measure the first one-at-a-time release**
  - Target: the build log and `buildlog memory`, over a natural hold and release after one-session-at-a-time release ships
  - Why needed: that release's effect is unmeasured until a real release runs; the one measured release before it did no harm, so the change has to show it costs no more than it saves
  - Completion condition: for one natural release, each held session's delivery, marks, first build start, the builds-slice pressure and any kill are reported, with the time to release every session
  - Revealed by: Phase 18
