---
description: Show the build budget and waiting steps, or one step's place and why it waits. Args - `[<position|session|folder>|close]`.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show the output as is inside a `text` code block.

The budget bar shows free (`░`) and taken (`█`) GiB. Each waiting step has one row with its showrunner, unit, step, minutes waited and a size bar on the same scale. `▸` marks your session's steps. On row 1, `↑ ≤N.N` lets younger steps use that many GiB in a strict window, `⊘` holds them when no room remains, and `↑` allows backfill; the countdown ends at the next window change. `low memory`, `pressure` or `no reading` names a host hold.

`/build:queue <position|session|folder>` shows matching step cards: whether each is next, who is ahead and why it waits. `build-why` keeps the head's full recorded history and release advice.

With `unit-board@nate-local`, the pane refreshes every 10 seconds and redraws the countdown every second without running the script. `close` closes it. Run `/reload-plugins` in sessions started before the mod change.

`bump <position|session|folder>` moves nothing and exits 3 with one stdout line and no stderr: `build queue: bump is off; nothing can move a waiting step`. Waiting steps that fit keep room in queue order; a younger step held by that room shows `behind older step <label>`. Use `/build:status` for holds and target state.
