---
description: Show running and waiting build steps, or move a waiting step to the head. Args - `[bump <position|session|folder>]`.
---

Run `python3 ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show its output as is: Running table first, Waiting table second, including `empty` rows, totals and any roster problem. Both tables start with `# | Showrunner | Unit | Session | Step | Folder`. Showrunner, Unit and Session come from the local session roster; unmatched steps show `—` for both roles and the recorded session name or folder basename for Session. Step is the bare step name. Running adds `Promised | Using | Delta | State`; Delta is promised minus using, and starting steps show `—` until measured. Waiting adds `Waiting since | Needs | Free | Reason`; Free is available memory, and a memory-hold Reason names the required free memory and its parts. Times use the user's zone.

`bump <position|session|folder>` moves one waiting step to the head for the next admission. Any session or the user may run it. Memory limits still apply. An unknown or ambiguous match changes nothing; use its position, shown Session name, full session id, recorded session name, folder path or folder name. The result and build log name who moved which step. Use `/build:status` for holds and target state.
