---
description: Show build queue positions and budget.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show its output in a `text` block.

The admission service supplies the queue through `memory_admit.queue_read()`. The view and cards use the service's stored decision.
The budget bar shows free (`░`) and taken (`█`) GiB. Each waiter shows its showrunner, unit, step, wait, and need.
`▸` marks your steps. The head shows its free memory, reserve, need, and queue window.
Unowned scopes show their memory as separate rows.

A position, session, or folder shows the full queue, one blank line, and the selected card on one page.
`mine` uses the same layout for your steps. `build-why` shows the head's recorded evidence and release advice.
The pane refreshes every ten seconds. A failed refresh keeps the last page and its selection.
A failed first query shows `Build queue could not be read.`
`close` closes the pane.

`bump <position|session|folder>` selects a waiting step with the card's selector resolver.
It sets priority for that step's unit with the reason `the user's bump`. `clear` clears priority.
An unmatched selector exits 3 with `build queue: no waiting step matches <selector>`.
`admit-head <why>` permits the current head to exceed the budget when free memory permits its start.
These commands require a showrunner session. Priority preserves order among steps with the same rank.
The permission refusal says: `build queue: type /build:queue bump <position|session|folder> in a showrunner pane`.
Use `/build:status` for holds and target state.

A step waiting for the cargo token keeps its place but holds no head room.
The first eligible step is the head, even when its position exceeds one.
A queue containing only token waiters still shows those waiters.
The pane passes its session id to each mutation command explicitly.
Unknown units use the same refusal protocol: exit 3 and a stdout line starting `build queue: `.
