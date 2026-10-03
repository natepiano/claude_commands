---
description: Ask every other top-level session on this machine to stop its cargo builds while you run one test, then release them. Args - `hold <test and why>` or `release`.
---

`$ARGUMENTS` is `hold <test and why>` or `release`.

1. Recipients: `python3 ~/.claude/scripts/message/top_level.py --self <your name>`, your name being the one ListAgents gives this session. Unit directors are left out; their showrunner holds them.
2. SendMessage each recipient one line:
   - hold: `/build_hold from <you>: stop any cargo or verify.sh you are running and start none until I release. Showrunners: hold your unit directors. No release after 2 h: ask me. For: <test and why>`
   - release: `/build_hold from <you>: released, builds may resume. Showrunners: release your unit directors.`
3. After a hold, start the test once `pgrep -u "$USER" '^(cargo|rustc|cargo-nextest)$'` prints nothing. Wait with Monitor, never a sleep loop. CI runners are not asked.
4. Release as soon as the test ends, pass or fail.
