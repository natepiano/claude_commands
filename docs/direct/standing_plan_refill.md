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
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/<plan> --repo <unit-worktree> --session-dir "${SESSION_DIR}"
```

Read `<plan>` from `plan` in the unit's `.unit.toml`, relative to its worktree; a rename
keeps that path. `add_unit.py` copies the showrunner template
only at first launch. Exit 0 continues with the ready phases; a todo repair always runs first.
The production document's `Style units` table supplies the unit's families and `Open tickets` count, default 1.
Refill selects only from those families, with tier D first and then crates that block the most cells.
Refill refuses a missing family owner.
The ledger resumes assigned work and discards merged or dropped ticket phases.
With N above 1, `--session-dir` restores each ticket's phase and its own Codex worker through `assign.sh`.
Refill assigns up to N disjoint tickets. The footprint proof runs at assignment, gate and merge.

| Exit | Unit status and next action |
| --- | --- |
| 3 | `waiting for sync` or `waiting for structure: <crates>`; report the printed line once to the showrunner and end the turn with `— blocked: waiting on the showrunner: sync`. Refill on its sync message or any `ticket <id> merged; refill.`. |
| 4 | `waiting for board`; report `board unavailable: <reason>` once and end the turn with `— blocked: waiting on the showrunner: board`. Refill on the next sync or showrunner message. |
| 5 | `waiting for merge: <id>`; report once and end the turn with `— blocked: waiting on the showrunner: ticket <id> merge`. Refill on `ticket <id> merged; refill.` |
| 6 | `merge the style branch first: <n> cells judged at <unit tip> wait for <style branch>`. Run `style_plan.py sync-unit --repo <unit-worktree> --unit <unit>`, then refill. A pending sync stops new assignments while assigned workers finish. Report a refused merge once to the showrunner. |
| Other nonzero | Report the error and retain the ticket; repair it before refill. |

A standing plan never closes its run: skip FinalGate, RunAsBuilt, the final
PeriodicCI, CICleanup and RunSummary. Never stage or shrink the excluded plan,
`docs/style-production/` or its pinned rule files. Workers judge only the pinned
`docs/style-production/rules/<blob>.md` files named in the ticket Work Order;
`record --ticket` keeps that pinned blob even after a live rule edit, which
leaves the new rule's cell stale until a later ticket.

The ticket gate is `style_plan.py gate --repo <unit-worktree> --ticket <id>`.
The gate and `ticket checkpoint` refuse unjudged owned cells and name each one.
Coverage tickets use the mechanical filter at their recorded base.
An approved proposal permits its approved finding paths and recorded widened paths.
Exit 1 prints `review:` and returns to repair with the reservation retained.
Exit 2 prints `error:` when the gate cannot run.
Retain the reservation and repeat the gate; stop and report a repeated error.

At <CheckpointCommit/> step 6, use the publication coordinator:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py checkpoint --repo <unit-worktree> --ticket <id> --session-dir "${SESSION_DIR}" --phase <phase id> --message-file <message file>
```

The coordinator gates and publishes the candidate, records the checkpoint and confirms reservation release before the next publication.
With N=1 and one assigned ticket, verification uses the editing tree.
Otherwise, verification uses the reused candidate copy while other workers edit their assigned paths.
Repeat the same coordinator command to recover a retained publication record.
For an N=1 no-change ticket, follow the no-change procedure in `commands/unit/checkpoint.md`.
</StandingPlanRefill>
