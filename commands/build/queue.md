---
description: Show running and waiting build steps, or move a waiting step to the head. Args - `[bump <position|session|folder>]`.
---

Run `python3 ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show its output as is: Running table first, Waiting table second, including `empty` rows and the totals line. Do not replace the tables with a summary. Sessions show names, with short folder names when unknown. Delta is promised minus using; starting steps show `—` until a reading exists. Each waiting row states its reason; times use the user's zone.

`bump <position|session|folder>` moves one waiting step to the head for the next admission. Any session or the user may run it. Memory limits still apply. An unknown or ambiguous match changes nothing; use its position, full session id, session name, folder path or folder name. The result and build log name who moved which step. Use `/build:status` for holds and target state.
