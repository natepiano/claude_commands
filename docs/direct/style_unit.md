# Style unit contracts

Read these contracts at their named steps in `/unit:style`.
`docs/direct/phase_reservation.md` remains authoritative for reservation states and ticket release.

<StandingPlanRefill>
Apply at startup, after restart or compaction, and at each ticket boundary, including an empty plan.

1. Read every `${SESSION_DIR}/phases/*/delegated_phase_reservation_state.json`.
2. Recover each pending publication or release under <StyleCheckpoint/> before running `next-phase`.
3. Stop refill if recovery fails.
4. Run the refill command:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/<plan> --repo <unit-worktree> --session-dir "${SESSION_DIR}"
   ```

Read `<plan>` from `.unit.toml`, relative to the unit worktree.
`add_unit.py` copies the showrunner template only at first launch.
Exit 0 continues with ready phases; a todo repair runs first.
The production's `Style units` table supplies families and `Open tickets`, default 1.
Refill refuses a missing family owner.
It selects tier D first, then crates that block the most cells within those families.
The ledger resumes assigned work and discards merged or dropped ticket phases.
Refill assigns up to N disjoint tickets.
With N above 1, `--session-dir` restores each ticket's phase and its own Codex worker through `assign.sh`.
The footprint proof runs at assignment, gate, and merge.

| Exit | Status and action |
| --- | --- |
| 3 | Report `waiting for sync` or `waiting for structure: <crates>` once to the showrunner. End with `— blocked: waiting on the showrunner: sync`. Resume on sync or `ticket <id> merged; refill.`. |
| 4 | Report `board unavailable: <reason>` once. End with `— blocked: waiting on the showrunner: board`. Resume on sync or a showrunner message. |
| 5 | Report `waiting for merge: <id>` once. End with `— blocked: waiting on the showrunner: ticket <id> merge`. Resume on `ticket <id> merged; refill.`. |
| 6 | Follow the Sync routes in `docs/style_production.md` for `merge the style branch first: <n> cells judged at <unit tip> wait for <style branch>`. |
| Other nonzero | Report the error, retain the ticket, and repair before refill. |

A pending sync stops new assignments while assigned workers finish.
Use the Style showrunner route or Direct route in `docs/style_production.md`, section Sync.
</StandingPlanRefill>

<StyleTicketGate>
Run the ticket gate:

```sh
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py gate --repo <unit-worktree> --ticket <id>
```

The gate and `ticket checkpoint` refuse unjudged owned cells and name each cell.
Coverage tickets use the mechanical filter at their recorded base.
An approved proposal permits its approved finding paths and recorded widened paths.
Exit 1 prints `review:` and returns to repair with the reservation retained.
Exit 2 prints `error:` when the gate cannot run.
Retain the reservation and repeat the gate after repair.
Stop and report a repeated error.
</StyleTicketGate>

<StyleCheckpoint>
Read `commands/unit/checkpoint.md` for shared checkpoint requirements.
Apply <CheckpointCommit/> steps 1, 3, and 5 before publication.
These style exceptions replace its plan staging, commit, and ticket release steps.
Do not enter the ordinary code commit path.

1. Confirm that status contains only ticket changes and permitted concurrent sibling work.
2. Exclude the plan, `docs/style-production/`, and pinned rule files from staging.
3. Keep completed ticket Work Orders until refill discards them.
4. Defer `status: done` until publication and release confirmation succeed.
5. Write the checkpoint message from <CheckpointCommit/> step 6 to a file.
6. Run the publication coordinator:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py checkpoint --repo <unit-worktree> --ticket <id> --session-dir "${SESSION_DIR}" --phase <phase id> --message-file <message file>
   ```

7. Require publication and reservation release confirmation before marking the phase done.
8. Apply <CheckpointCommit/> steps 8 and 9 after confirmation.
9. Send the ticket checkpoint through the production's configured merge route under <ProductionUnit/>.

The coordinator gates the candidate and publishes it with an expected-HEAD update.
It updates published paths in the index and records the checkpoint.
It releases the reservation while HEAD names that checkpoint.
The next publication waits for release confirmation.
Other workers continue to edit their assigned paths.
With N=1 and one assigned ticket, the gate and verification use the editing tree.
Otherwise, they use the reused candidate copy.
The coordinator runs `doc`; do not run it separately for a ticket.
Keep ledger and approval documents in the original repository.

On failure, retain the reservation and repeat the same coordinator command after repair.
A retained publication record completes the same checkpoint without another commit.
For `CheckpointCommittedAwaitingReleaseConfirmation` with `held_for_ticket`, repeat that coordinator command before marking the phase done.
For a pending ordinary release without `held_for_ticket`, apply <CheckpointCommit/> step 7 before push and the merge request.
A merged no-change ticket needs no repeated transition.
Before refill, restore every ticket's phase and worker through `next-phase --session-dir "${SESSION_DIR}"`.
Apply <StyleNoChangeTicket/> instead of publication for an eligible N=1 no-change ticket.
</StyleCheckpoint>

<StyleNoChangeTicket>
Use this path only with N=1, one assigned ticket, and no tracked diff.
Every owned cell must be recorded `clean`, `exception`, or `held` at the unit's HEAD.

1. Apply <CheckpointCommit/> steps 1, 3, and 5.
2. Capture `git rev-parse HEAD` once.
3. Require `git merge-base --is-ancestor <captured HEAD> refs/heads/<receiving merge branch>`.
4. On ancestry failure, report to the showrunner and leave the ticket assigned until it answers.
5. From `Active`, write the captured commit through the reservation API:

   ```sh
   phase_reservation.py committed --session-dir "${SESSION_DIR}" --phase <phase id> --commit <captured HEAD> --ticket <ticket>
   ```

6. Read the durable record and require that same full commit id.
7. Run the ticket checkpoint:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/style/style_ledger.py ticket checkpoint --repo <unit-worktree> --id <ticket> --commit <captured HEAD> --session-dir "${SESSION_DIR}" --phase <phase id>
   ```

8. Run the ticket merge:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/style/style_ledger.py ticket merge --repo <unit-worktree> --id <ticket>
   ```

9. Apply the printed `held built` command only after checking that its change exists in the code.
10. Apply <CheckpointCommit/> step 7 to confirm release; a cleared record or inactive state releases nothing.
11. Mark the phase done after confirmation.
12. Send `From <unit>: ticket <id> closed with no code change; <n> held.`.
13. Continue with <StyleTicketEnd/>.

The fast-forward exception in `docs/production_format.md` item 5 preserves the required receiving-branch ancestry after sync.
This is the sole path that records a checkpoint commit which the phase did not create.
`--commit` accepts a commit name; the record keeps its full id.
A failed transition or read prevents ticket merge and retains the valid durable state.
Ticket merge releases the held reservation and clears its record under <PhaseReservationContract/>.
Exit 2 ending with `rerun ticket merge` retains the reservation.
Report that line and repeat the merge after repair.
A finding whose change is absent needs a person to implement it or run `style_ledger.py held waive --person <name>`.
Create no commit, push, or merge request for this path.
</StyleNoChangeTicket>

<StyleTicketEnd>
Require checkpoint and release confirmation before completing a ticket.
Apply <PhaseCleanup/> and <RecordPhaseCompletion/> after release confirmation.
Retain worker rows and native conversations under <LongLivedWorkers/>.
Apply <StandingPlanRefill/> at the next boundary, even without a todo phase.
</StyleTicketEnd>

<StyleStandingLifetime>
Keep the standing run open when no ticket is available.
Skip <FinalGate/>, <RunAsBuilt/>, final <PeriodicCI/>, <CICleanup/>, and <RunSummary/>.
Skip shrink, its structural check, its commit, and its notice.
Never stage, commit, or shrink the excluded plan, `docs/style-production/`, or pinned rule files.
Refill discards merged or dropped ticket phases.
Workers judge only the pinned `docs/style-production/rules/<blob>.md` files named in the ticket Work Order.
`record --ticket` retains the pinned blob after a live rule edit.
The edited rule's cell remains stale until a later ticket judges it.
Never put a checkpoint hash in the plan.
</StyleStandingLifetime>
