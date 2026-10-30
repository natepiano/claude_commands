---
description: Show running and waiting build steps, or one step's place and why it waits. Args - `[<position|session|folder>|close]`.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show the output as is inside a `text` code block.

Each running or waiting step takes one line, even in a narrow pane. `↑` marks a step moved up; the move lines name who moved it, when, and from which place. The `#1 waits on` block shows admission's checks once, with pass/fail marks and their numbers. `starts` is an estimate at the last hour's completion pace, not a promise.

`/build:queue <position|session|folder>` shows the matching step's card: whether it is next, who is ahead, why it waits and its likely start. A session with several waiting steps gets several cards. `build-why` keeps the head's full recorded history and release advice.

With `unit-board@nate-local`, the command opens the queue or card in a pane and refreshes it every 10 seconds. `close` closes it. Run `/reload-plugins` in sessions started before the mod change.

Moving a step up is the user's, through `/showrunner:bump`. Never run `bump` for a session. Use `/build:status` for holds and target state.
