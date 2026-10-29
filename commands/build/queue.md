---
description: Show waiting and running build steps, or move a waiting step to the head. Args - `[bump <position|session|folder>]`.
---

Run `python3 ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS` and show its lines unchanged. With no arguments, it reads the memory queue once: waiting steps in order with session, step, folder, local wait time and one reason; then running steps with promised and measured memory. A new step has no use reading yet; its promise counts until the first reading. A ready step waits for the next admission check. `memory queue: empty` means no step waits.

`bump <position|session|folder>` moves one waiting step to the head for the next admission. Any session or the user may run it. Memory limits still apply. An unknown or ambiguous match changes nothing; use its position, full session id, session name, folder path or folder name. The result and build log name who moved which step. Use `/build:status` for holds and target state.
