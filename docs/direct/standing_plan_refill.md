# Standing plan refill

<StandingPlanRefill>
Run at standing-plan startup, after compaction or restart, and at every
<NextPhase/> boundary, before deciding that the plan is exhausted.
Before `next-phase`, read every
`${SESSION_DIR}/phases/*/delegated_phase_reservation_state.json`. For any record
in `checkpoint_committed_awaiting_release_confirmation`, resume
<CheckpointCommit/> at step 7 through release confirmation, then push and merge
request as that procedure requires, before refilling; a new ticket must not
overtake an unconfirmed release. An unsuccessful recovery stops refill.

Then run:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/<plan> --repo <unit-worktree>
```

Read `<plan>` from `plan` in the unit's `.unit.toml`, relative to its worktree; a rename
keeps that path. `add_unit.py` copies the showrunner template
only at first launch. Exit 0 continues with the todo phase; a todo repair
always runs first. The ledger resumes assigned work, rebuilds a missing phase,
recovers interrupted assignment, and discards merged or dropped ticket phases;
never take a second ticket while one is open.

| Exit | Unit status and next action |
| --- | --- |
| 3 | `waiting for sync` or `waiting for structure: <crates>`; report the printed line once to the showrunner and end the turn with `— blocked: waiting on the showrunner: sync`. Refill on its sync message or any `ticket <id> merged; refill.`. |
| 4 | `waiting for board`; report `board unavailable: <reason>` once and end the turn with `— blocked: waiting on the showrunner: board`. Refill on the next sync or showrunner message. |
| 5 | `waiting for merge: <id>`; report once and end the turn with `— blocked: waiting on the showrunner: ticket <id> merge`. Refill on `ticket <id> merged; refill.` |
| 6 | `merge the style branch first: <n> cells judged at <unit tip> wait for <style branch>`; each cell was judged at a commit in the unit tip with the inputs the tip has, and the pushed style merge branch changes those inputs. In the unit worktree, run the standing style unit merge (`~/.claude/docs/production_format.md` <ProductionUnit/> item 5): `git -C <unit-worktree> fetch origin <style merge branch> && git -C <unit-worktree> merge --ff-only origin/<style merge branch>`, then refill at once. If the fast-forward is refused or leaves HEAD unchanged, do not refill: report it once to the showrunner and end the turn with `— blocked: waiting on the showrunner: style merge`. |
| Other nonzero | Report the error and retain the ticket; repair it before refill. |

A standing plan never closes its run: skip FinalGate, RunAsBuilt, the final
PeriodicCI, CICleanup and RunSummary. Never stage or shrink the excluded plan,
`docs/style-production/` or its pinned rule files. Workers judge only the pinned
`docs/style-production/rules/<blob>.md` files named in the ticket Work Order;
`record --ticket` keeps that pinned blob even after a live rule edit, which
leaves the new rule's cell stale until a later ticket.

The ticket gate is `style_plan.py gate --repo . --ticket <id>`, from the unit
worktree. Run it again at <CheckpointCommit/> step 6 before staging. The gate and
`ticket checkpoint` refuse unjudged owned cells, naming each one. Coverage
selection runs the mechanical filter at the ticket's recorded base; an approved
proposal permits only files of its approved findings. Exit 1 prints `review:`,
retains the reservation and returns to repair. Exit 2 prints `error:` when the
gate could not run, such as a locked ledger: retain the reservation and rerun
it; if the same error comes back, stop and report it. Exit 0 permits committing exactly that tree,
then `style_ledger.py ticket checkpoint --id <ticket> --commit <code commit> --session-dir "${SESSION_DIR}" --phase <phase id>`
before marking the phase done. Both gates run this one command, so they cannot drift.
</StandingPlanRefill>
