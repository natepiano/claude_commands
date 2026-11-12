---
description: Direct standing style tickets and their publication.
argument-hint: "[plan-doc-path] [resume]"
---

# Style

**Usage:** `/unit:style [plan-doc-path] [resume]`

## Identity

<StyleIdentity>
Read your staff row and require `kind=unit` and `unit_type=style`.
Read the owning showrunner's production document through staff.
Set `WORKING_DIR` from your staff worktree.
Read `PLAN_DOC` from `plan` in that worktree's `.unit.toml`.
Resolve a relative plan path against `WORKING_DIR`.
Keep that plan path after a rename.
The showrunner's plan is only the first-launch template.
Read `docs/style_production.md` for ticket scope and merge routes.
Set `MODE=loop` for the shared ticket contracts.

Apply only the cited shared contracts from `commands/unit/direct.md`; do not enter its phase loop.
`<Tag/>` means apply the complete contract from its source at the named step.
Read the source at that step.

| Contract | Source |
| --- | --- |
| <LongLivedWorkers/>, <DispatchContract/>, <WorkerRuns/>, <ToolingContract/> | `commands/unit/direct.md` |
| <RestartTakeover/> | `docs/direct/restart_takeover.md` |
| <ThreeGods/>, <DecisionEconomy/> | `~/.claude/docs/decision_criteria.md` |
| <AuthorizationContract/> | `docs/direct/authorization.md` |
| <StandingPlanRefill/>, <StyleTicketGate/>, <StyleCheckpoint/>, <StyleNoChangeTicket/>, <StyleTicketEnd/>, <StyleStandingLifetime/> | `docs/direct/style_unit.md` |
| <WritePromptContract/>, <PhaseTeam/>, <RoleReassignment/> | `docs/direct/write_prompt_contract.md` |
| <LaunchImplementation/> | `docs/direct/launch_implementation.md` |
| <DualReview/>, <ReviewPromptContract/>, <ClosureReview/> | `docs/direct/dual_review.md` |
| <FindingsLedger/>, <Synthesize/>, <FixDispatch/> | `docs/direct/repair_rounds.md` |
| <PhaseChecks/> | `docs/direct/phase_checks.md` |
| <PhaseReservationContract/>, <CoordinatePhaseReservation/>, <RetainPhaseReservation/> | `docs/direct/phase_reservation.md` |
| <BerthDecisions/> | `docs/direct/berth_decisions.md` |
| <WorkerLaunchFailure/> | `docs/direct/launch_failure.md` |
| <HeldAstraRoles/> | `docs/direct/held_astra_roles.md` |
| <CheckpointCommit/>, <PushCheckpoint/> | `commands/unit/checkpoint.md` |
| <PhaseCleanup/>, <RecordPhaseCompletion/> | `commands/unit/direct.md` |
| <ProductionUnit/> | `docs/production_format.md` |

Paths are under `~/.claude/`.
</StyleIdentity>

## Ownership

<StyleOwnership>
Read your row in the owning production's `Style units` table.
Take families and `Open tickets` from that row; an empty count means 1.
Do not infer ownership from your name or plan header.
Do not assign work from another unit's families.
The table defines ownership; the ledger defines assigned work.
Product work takes priority over style work.
</StyleOwnership>

## Startup and recovery

<StyleStartup>
1. Recover the existing run directory through staff and dispatch records.
2. For a new run, run `bash ~/.claude/scripts/direct/prepare_session.sh` under <ToolingContract/>.
3. Set `SESSION_DIR` to the recovered or prepared run path.
4. Apply <RestartTakeover/> before new work.
5. Initialize a missing recorder with the configured plan:

   ```sh
   ~/.claude/scripts/lib/py ~/.claude/scripts/direct/progress_history.py start-run --session-dir "$SESSION_DIR" --working-dir "$WORKING_DIR" --plan-doc "$PLAN_DOC"
   ```

6. Recover pending publication under <StyleCheckpoint/> before refill.
7. Apply <StandingPlanRefill/>.

Repeat this order after restart, compaction, move, or rename.
Keep each restored worker and dispatch identity.
</StyleStartup>

## Refill

Apply <StandingPlanRefill/> at startup, after recovery, and at each ticket boundary.
Refill resumes assigned tickets before selecting new work.
An empty plan remains open under <StyleStandingLifetime/>.

## Ticket execution

<StyleTicketExecution>
1. Read the ticket's generated Work Order and its recorded base.
2. Use only the pinned rule blobs that the Work Order names.
3. Apply <CoordinatePhaseReservation/> before dispatch.
4. Apply <WritePromptContract/> and <LaunchImplementation/> with the ticket's phase and paths.
5. Give each concurrent ticket its own worker through `assign.sh`.
6. Read its completed reply under <DispatchContract/>.
7. Apply <DualReview/> and <PhaseChecks/> to the ticket's changes.
8. Apply <Synthesize/> to record findings and select repairs.
9. Apply <FixDispatch/> and <ClosureReview/> for each required repair, then return to <Synthesize/> until findings converge.
10. Apply <StyleTicketGate/>.
11. Apply <StyleCheckpoint/>, then <StyleTicketEnd/>.

Retain workers under <LongLivedWorkers/> between tickets.
Use restored dispatches when refill already opened a worker.
Do not submit that worker's prompt again.
Keep footprint proofs at assignment, gate, and merge.
Record every owned cell through the ledger at its judged commit.
Hold changes outside the mechanical criteria for a decision under `docs/style_production.md`.
Preserve approved finding paths and recorded widened paths.
Apply <HeldAstraRoles/> when a ticket holds an Astra role.
</StyleTicketExecution>

## Ticket gate

Apply <StyleTicketGate/> before publication.
Keep the reservation during repair or a gate error.

## Publication

Apply <StyleCheckpoint/> for changed tickets and <StyleNoChangeTicket/> for eligible unchanged tickets.
The shared <PhaseReservationContract/> owns reservation states and ticket release.
Resume a retained publication before another ticket can publish or refill.

## Waiting states

Use the exit table in <StandingPlanRefill/>.
Report each wait once through the owning showrunner.
Resume on the matching sync, board, or ticket merge message.
Keep assigned workers running while a pending sync blocks new assignments.

## Standing lifetime

Apply <StyleStandingLifetime/>.
The unit waits for more tickets when no work is available.
