---
description: Ask every other top-level session on this machine to stop its cargo builds while you run one test, then release them. Args - `hold <test and why>` or `release`.
---

`$ARGUMENTS` is `hold <test and why>` or `release`.

1. On hold, create `~/.local/state/build-hold/` with
   `mkdir -p ~/.local/state/build-hold`. Name your own file for the holder,
   replacing every character outside `A-Za-z0-9._-` with `-`. Write one line
   containing the holder, the current ISO time, and the test and reason. Then send with
   `/notify_top_level --here`: `/build_hold from <you>: stop any cargo or
   verify.sh you are running and start none until I release. No release after
   2 h: ask me. For: <test and why>`
2. After a hold, start the test once `pgrep -u "$USER" '^(cargo|rustc|cargo-nextest)$'` prints nothing. Wait with Monitor, never a sleep loop. CI runners are not asked.
3. Release as soon as the test ends, pass or fail: remove only your holder file
   from `~/.local/state/build-hold/`, then send with `/notify_top_level
   --here`: `/build_hold from <you>: released, builds may resume.`
