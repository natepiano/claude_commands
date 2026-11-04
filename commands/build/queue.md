---
description: Show build queue positions and budget.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`. Show its output in a `text` block.

The admission service supplies the queue through `memory_admit.queue_read()`. The view and cards use the service's stored decision.
The budget bar shows free (`░`) and taken (`█`) GiB. Each waiter shows its showrunner, unit, step, wait, and need.
`▸` marks your steps. The head shows its need and queue window. The selected page shows the full admission arithmetic.
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

`bump <position|session|folder>` selects a waiting step with the card's selector resolver.
It sets priority for that step's unit with the reason `the user's bump`. `clear` clears priority.
An unmatched selector exits 3 with `build queue: no waiting step matches <selector>`.
`admit-head <why>` permits the current head to exceed the budget when free memory permits its start.
These commands work from any session or a plain shell. Priority preserves order among steps with the same rank.
Use `/build:status` for holds and target state.
