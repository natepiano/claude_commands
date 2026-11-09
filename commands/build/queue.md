---
description: Change build queue priority or admit its head.
---

Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_queue/build_queue.py $ARGUMENTS`.
Show its output in a `text` block.
Use `/build:page` to see running steps, waiting steps, and memory use.
A view request without `--json` prints `build queue: the queue view is on the build page; run /build:page` and exits 3.

`bump <position|session|folder>` selects a waiting step with the pane card's selector resolver.
It sets priority for that step's unit with the reason `the user's bump`.
`clear` clears priority.
An unmatched selector exits 3 with `build queue: no waiting step matches <selector>`.
`admit-head <why>` permits the current head to exceed the budget when free memory permits its start.
These controls work from any session or a plain shell.
Priority preserves order among steps with the same rank.
The pane passes its session id to each control command.
Unknown units use the same refusal protocol: exit 3 and a stdout line starting `build queue: `.
Use `/build:status` for holds and target state.

The unit-board queue pane keeps its view and selected step cards through `--json`.
A position, session, folder, or `mine` selects cards in the pane.
`close` closes the pane.
The pane refreshes every ten seconds.
A failed refresh keeps the last successful view and its selection.
A failed first query shows `Build queue could not be read.`
There is no separate build-why command or alias.

The pane and the build page show the saved Mac idle alert first, in red.
`MAC IDLE` means eligible steps wait while no natedev build or test runs on the Mac past the configured interval.
The default interval is three minutes.
The line names a block, an idle claim, or a free Mac that takes no work.
The holder clears a block with `/build:mac unblock`.
For an idle claim, resume build or test work or release the claim.
For a free Mac, the builds showrunner checks why waiting steps do not take it.
The line clears when work starts or no eligible steps wait.
`MAC UNREACHABLE` names a connection outage immediately.
Restore access before Mac work continues.
`MAC IDLE WATCH` names unreadable state or a reading past the configured freshness limit.
The default freshness limit is five minutes.
Repair the named state or check `buildlog-sample.timer`.
These lines use the same saved reading and timing configuration on the pane and the page.

A step waiting for the cargo token keeps its place but holds no head room.
The first eligible step is the head, even when its position exceeds one.
A queue containing only token waiters still shows those waiters.
