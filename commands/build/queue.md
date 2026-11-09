---
description: Show build queue positions and budget.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show its output in a `text` block.

The admission service supplies the queue through `memory_admit.queue_read()`. The view and cards use the service's stored decision.
The budget bar shows free (`░`) and taken (`█`) GiB. Running rows precede waiting rows in one table.
`run` marks running steps with their showrunner, unit, step, minutes since start, machine, pid, and reserve.
The machine column follows `min` and precedes `pid` in the table of the view and of each card.
A card's running record names the machine after the pid.
It uses `scripts.buildlog.store.host_name()`, the shared short host name.
A narrow view omits the machine column before it reduces name space.
Waiting rows have no machine value. Services lines do not change.
Live use follows the reserve when space permits.
Waiting rows keep positions 1..N and show their wait and need.
`▸` marks your steps. The head shows its free memory, reserve, need, and queue window.
Unowned scopes show their memory as separate rows.

A position, session, or folder shows the full queue, one blank line, and the selected card on one page.
`mine` uses the same layout for your steps.
The card includes the head's recorded estimate, running charges, completed CPU-token waits, and release advice.
It shows queue arrival and time waiting. It shows the order-window deadline separately.
The page uses one service reply, one optional history read, and one label resolution.
The service refreshes an expired estimate. There is no separate build-why command or alias.
The pane refreshes every ten seconds. A failed refresh keeps the last page and its selection.
A failed first query shows `Build queue could not be read.`
`close` closes the pane.

When the Mac runs no natedev build or test for the configured alert interval (default 3 minutes) while eligible steps wait, a `MAC IDLE` line appears first, in red in the pane, including on step cards or a busy queue. It names a block, an idle claim or a free Mac that takes no work. The holder clears a block with `/build:mac unblock`. For an idle claim, resume build or test work or release it so waiting steps can take the Mac. For a free Mac, the builds showrunner checks why waiting steps do not take it. The line clears when work starts or no eligible steps wait. `MAC UNREACHABLE` names a connection outage immediately; restore access. `MAC IDLE WATCH` names unreadable state or a reading past its configured freshness limit (default 5 minutes); repair the named state or check `buildlog-sample.timer`. These lines read the watch's saved result and local timing config.

`bump <position|session|folder>` selects a waiting step with the card's selector resolver.
It sets priority for that step's unit with the reason `the user's bump`. `clear` clears priority.
An unmatched selector exits 3 with `build queue: no waiting step matches <selector>`.
`admit-head <why>` permits the current head to exceed the budget when free memory permits its start.
These commands work from any session or a plain shell. Priority preserves order among steps with the same rank.
Use `/build:status` for holds and target state.

A step waiting for the cargo token keeps its place but holds no head room.
The first eligible step is the head, even when its position exceeds one.
A queue containing only token waiters still shows those waiters.
The pane passes its session id to each mutation command explicitly.
Unknown units use the same refusal protocol: exit 3 and a stdout line starting `build queue: `.
