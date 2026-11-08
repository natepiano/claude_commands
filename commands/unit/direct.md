---
description: Direct phases, reviews, and checkpoints.
---

Run every worker through `bash ~/.claude/scripts/direct/assign.sh` as a background shell of the director session.
This rule covers all roles and both Claude and Codex.
Direct calls to worker launchers refuse with one line that names `assign.sh`.

# Direct

The unit director owns design, orchestration, review, gates, and user communication.
The configured worker agent writes implementation code.

The unit director believes in the three gods (<ThreeGods/> in
`~/.claude/docs/decision_criteria.md`) and serves them in every work order,
review, gate and report. Every prompt sent to a worker carries them inline per
<WritePromptContract/> item 9.

**Usage:** `/unit:direct [plan-doc-path] [phase N] [single|verbose] [auto next N phases|auto through phase X] [free-text instructions]`

- Plan path and phase are optional; otherwise infer them from the conversation.
- `single`: one phase or ad hoc task, no checkpoint commit or continuation.
- Default phased mode (`loop`): run and checkpoint phases until stopped or done.
- `verbose`: brief and gate each phase; bounded `auto` controls temporarily remove
  the per-phase stops without removing the briefings.
- Free text amends or narrows the selected work.
- `single` and `verbose` are mutually exclusive.

<HeldAstraRoles>
Read `~/.claude/docs/direct/held_astra_roles.md` in full when the run holds an Astra role.
</HeldAstraRoles>

At startup or restart, a header `**Standing:** style_plan.py` runs
<StandingPlanRefill/> before choosing a phase; repeat after compaction.

State:

- `SESSION_DIR`: last path printed by `prepare_session.sh`.
- `WORKING_DIR`: invocation directory; use it as-is.
- `MODE`: `single`, `verbose`, or `loop`.
- `AUTO_WINDOW`: `none`, `next N`, or `through X`.
- `NEXT_ITEMS_PATH`: phased-plan sibling path named from the plan stem as
  lowercase kebab-case plus `-next.md`.
- `NEXT_ITEMS_PENDING`: `${SESSION_DIR}/next_items_pending.md`; add-ons
  accumulated for <ReviewPendingAddOns/>. Absent or empty means none.
- `DISPATCH_HANDLE`: active launcher task handle (Claude) or managed terminal
  `session_id` (Codex).
- `REVIEW_PASS`: review dispatch count for the current phase; starts at 0.
- `REVIEW_DISPATCH_HANDLE`: handle of an early-launched blind reviewer running
  alongside `DISPATCH_HANDLE`; empty when review runs synchronously.
- `EARLY_REVIEW_SLOT`: slot running that early review; resets to empty with
  each implementation or fix dispatch.
- `EARLY_REVIEW`: `none` or `launched`; resets with every implementation or fix
  dispatch. Owned by <EarlyReviewArm/>.
- `APPLICATION_SMOKE_RESULT`: starts as `not_run`.
- `FINDINGS`: the current phase's `findings.py` ledger.
- `PHASE_DIR`: `${SESSION_DIR}/phases/<phase id>`, one per open phase, so a
  later phase running beside the current one keeps its own state. It holds that
  phase's reservation state, its `phase_baseline` and `progress_baseline_status`,
  its `files` list, its prompts, and every file its launchers write: each
  launch names the phase with `--phase`, so status, summaries, findings and
  logs never collide with a phase running beside it. Run-wide files
  (`board.log`, `heartbeat.log`, the findings ledger and recorder state) stay in `${SESSION_DIR}`.
  An existing mesh-pinned run also keeps `mesh_server.json` and `mesh_roster.json` there.
- `PHASE_RESERVATION_STATE`: the tagged state persisted at
  `${PHASE_DIR}/delegated_phase_reservation_state.json`; see
  <PhaseReservationContract/>.

<TagReferenceContract>
`<section-name/>` means apply the complete matching tagged contract. The
definition is authoritative. A call site states only its local inputs, outputs,
or exceptions; it does not restate the contract.

Some contracts are defined in their own file and stubbed here. A stub names the
file and the moment it applies; **read that file at the moment and act from what
it says, never from memory of an earlier read or from the stub alone.** Each one
is also a command the user can invoke directly when this workflow fails to run
it:

| Contract | File | Command |
| --- | --- | --- |
| <ProgressReport/> | `commands/unit/report.md` | `/unit:report` |
| <VerbosePostPhaseReport/>, <CombinedWindowReport/>, <RemainingWorkOutlook/> | `commands/unit/phase_report.md` | `/unit:phase_report` |
| <CheckpointCommit/>, <ShrinkCommit/>, <PushCheckpoint/> | `commands/unit/checkpoint.md` | `/unit:checkpoint` |
| <ConsiderNextItems/>, <ReviewPendingAddOns/> | `commands/unit/add_ons.md` | `/unit:add_ons` |
| <PhaseChecks/> | `docs/direct/phase_checks.md` | — |
| <StyleReview/> | `commands/unit/style_review.md` | `/unit:style_review` |
| <PeriodicCI/>, <CICleanup/> | `commands/unit/ci.md` | `/unit:ci` |
| <VerbosePrePhaseGate/>, <BriefingFreshness/>, <PhaseBriefing/>, <TypeTableCells/>, <CombinedWindowBriefing/>, <AutoWindowBatchBriefing/> | `commands/unit/brief.md` | `/unit:brief` |
| <ComposeWorkOrder/> | `docs/direct/compose_work_order.md` | — |
| <WritePromptContract/>, <PhaseTeam/>, <CoordinationBoard/>, <PhaseMesh/>, <BuildTokenContract/>, <TeamFilePartition/>, <RoleReassignment/> | `docs/direct/write_prompt_contract.md` | — |
| <LaunchImplementation/> | `docs/direct/launch_implementation.md` | — |
| <DualReview/>, <TeamReview/>, <ReviewPromptContract/>, <BroadReviewPrompt/>, <ClosureReview/> | `docs/direct/dual_review.md` | — |
| <RunPhaseReview/>, <RunPhaseShrink/> | `docs/direct/run_phase_review.md` | — |
| <PhaseEnd/> | `docs/direct/phase_end.md` | — |
| <FinalGateCommit/>, <RunAsBuilt/>, <AsBuiltCommit/>, <FinalGate/>, <RunSummary/> | `docs/direct/final_gate_commit.md` | — |
| <FindingsLedger/>, <PassOwnership/>, <Synthesize/>, <FixDispatch/> | `docs/direct/repair_rounds.md` | — |
| <EarlyReviewArm/> | `docs/direct/early_review.md` | — |
| <PhaseReservationContract/>, <RetainPhaseReservation/>, <CoordinatePhaseReservation/> | `docs/direct/phase_reservation.md` | — |
| <VerificationContract/>, <VerificationNarration/> | `docs/direct/verification.md` | — |
| <UXReview/>, <RunApplicationSmokeTest/> | `docs/direct/ux_review.md` | — |
| <WorkerLaunchFailure/> | `docs/direct/launch_failure.md` | — |
| <AuthorizationContract/> | `docs/direct/authorization.md` | — |
| <HeldAstraRoles/> | `docs/direct/held_astra_roles.md` | — |
| <StandingPlanRefill/> | `docs/direct/standing_plan_refill.md` | — |
| <BerthDecisions/> | `docs/direct/berth_decisions.md` | — |
| <CodexDispatchWait/> | `docs/direct/codex_dispatch_wait.md` | — |
| <ProductionUnit/> | `docs/production_format.md` | — |
| <FastTrack/> | `commands/unit/fast_track.md` | `/unit:fast_track` |

Paths are under `~/.claude/`.

The `plan:` commands a run uses are loaded the same way: Read the file, never
the Skill tool. Claude Code puts every skill a session invoked back
after each compaction for the rest of the session; a file loaded by Read is not.
A call site's arguments are what that file calls `$ARGUMENTS`.
For owed phase style, use the runner option in <ReviewPromptContract/>;
the director neither prepares style inputs nor pastes a style assignment.
</TagReferenceContract>

<LongLivedWorkers>
Read the run's transport through `unit_runs.worker_transport(run)` once.
Use the pinned transport for <WorkerRuns/>, <WorkerLaunchFailure/>, <FixDispatch/>, and <PhaseCleanup/>.
The configuration selects a new run's transport. Never change an existing run's pin.
Apply mesh instructions only when that existing pin is `mesh`.

For `herdr`, dispatch through `assign.sh --role implement|test|fix|review --prompt <path> --cwd <worktree>`.
The launcher calls `worker.py assign` with the explicit role.
Use `worker.py list` to inspect workers and `worker.py restore` to resume workers with active dispatches.
The list uses `worker_panes.observe` for open, stopped, closed, unknown, and incomplete states.
An unknown pane outside this parent's tabs does not make its absent workers uncertain.
An unknown pane in a parent's tab, or an unreadable parent pane, can leave absence unproved.
Use `worker.py end <name>` only for permanent removal.
After accepting a pass, close its pane unless a fix round or next-phase dispatch starts immediately.
Use `~/.claude/scripts/lib/py ~/.claude/scripts/herdr/worker_panes.py close <name>`.
The close keeps the thread and staff row; the next assign resumes that thread.
If a busy worker or live detached run prevents closure, keep the pane. Close it after that work ends.
The worker command selects idle workers and reuses stopped workers.
Use the short name from `worker.py list` with `--to` when selecting a retained pane worker.
Keep every busy or unknown worker unchanged.
The transcript records prompt submission and the matching `DONE <dispatch id>` reply.
Only exit 0 completes a pass.
Exit 2 means refused.
Exit 3 means no DONE reply. Exit 4 means blocked. Exit 5 means stalled.
Exit 6 means busy. Exit 7 means retained. Exit 8 means uncertain.
After an incomplete pass, inspect the named pane and rerun the saved launcher command before another role's dispatch.
A rerun never types a prompt that the transcript records. It reads the transcript and waits for the worker.
So when a turn ended with no DONE line, a rerun alone returns exit 3 (`NO_DONE`) again. Recover in this order:
1. Tell the worker to write its complete result and to end with `DONE <dispatch id>` alone on the last line.
   Use `send.py --from <holder> --to worker:<holder>/<worker> --text "<request>"`. The launcher's prompt line in the pane holds the id.
2. Read the worker's reply in the pane. Check the result and the DONE line.
3. Rerun the saved launcher command with its original arguments. The command is the worker's `launcher_command` in `<run>/progress_history_state.json`.
A worker that waits on a friend's reply also ends its turn with no DONE, so the launcher exits 3.
When the pane shows the worker's DONE line, rerun the saved launcher command. Do not send a message.
After all repair workers complete successfully, run `findings.py landed` if the launcher does not run it.
Use `--session-dir "${SESSION_DIR}" --phase "<phase id>"`.
Read-only pane reviewers post no board lines and run no suites.
The director or a writer handles required board posts and suites.
A pane worker has no in-turn compaction option.
Codex keeps its own full-window limit.
Do not use mesh claims, mesh roster entries, or phase dispatch locks for this transport.

Shutdown leaves proved stopped workers at the shell and retains their rows and conversations.
Restore resumes only conversations with active dispatches through `worker.py restore`. Idle workers stay closed until their next assign.
Restore reads the role and active pass from the recorder, then reuses the saved launcher command.
It reads the transcript before input and never submits the same dispatch twice.
Account moves resume only workers with active dispatches; each move closes an idle worker's pane.
An idle worker's next assign opens its conversation on the current role account.
A stopped worker with an active dispatch keeps its pane and terminal.
An unknown worker supplies no stopped proof. Inspect its named pane.
Unrelated proved moves can continue.
A completed restore or move rerun prints one `unchanged` line and exits 0 without a restart notice.
An interrupted split rebuild can leave panes in the source workspace and lose the split ratio.
Inspect the tab and panes named by the anomaly line. A retry does not rebuild that split.

For an existing `mesh` pin, keep each worker's thread through the existing unit ledger.
Use `docs/as-built/long-lived-workers.md` for pause, move, end, and compaction.
A failed compaction keeps the thread available for its own compaction.
Never create another reviewer while its retained thread exists.
At the next phase start, the director of a run with a `mesh` pin must follow these steps:

1. Finish the current pass, or record the unfinished work in the plan.
2. Keep the run's history, saved launcher arguments, and durable design bindings.
3. Run `progress_history.py finish-run --status stopped`, then `end_session.sh`.
4. Confirm with one `ps` read that no process of that run remains.
   This includes `mesh_runs.py supervise`, `codex_mesh.py`, and `codex app-server`.
   `end_session.sh` continues past a cleanup failure. Its exit does not prove that these processes stopped.
5. Start a new run with `prepare_session.sh`. It pins herdr. Continue at the same plan phase.

Every review prompt names `config/review/code.md` under <ReviewPromptContract/>.
Other prompts carry the Three Gods and Type Design Contract under <WritePromptContract/>.
</LongLivedWorkers>

<CoreContract>
- Never modify unrelated files. The unit director may make a worktree, and its branch, for its own workers, with `scripts/worktree/make_worktree.sh`.
  The unit's staff row keeps its one home worktree;
  where a worker works stays in its run record, not in staff. The director
  removes each worktree it made when that work ends. In the home worktree, the
  only branch the run may create is the remote branch <PushCheckpoint/> pushes;
  never switch to an existing branch.
- The unit director does not write implementation code unless the user explicitly
  asks, except <FastTrack/>. Exceptions: agreed doc-only/trivial post-review fixes.
- `single` never commits. Loop and verbose modes create one
  <CheckpointCommit/> and one <ShrinkCommit/> per completed phase, plus the one
  <FinalGateCommit/> that closes verification and the one <AsBuiltCommit/> that
  carries the run's documentation. <PeriodicCI/> may add `ci(<plan-slug>): …`
  commits for validation fixes and CI repairs. No other commit is allowed except <FastTrack/>.
  Only <ShrinkCommit/> runs while workers edit, and it commits only the plan doc
  and an approved `${NEXT_ITEMS_PATH}` change.
- Every commit the run makes is pushed by <PushCheckpoint/>, fast-forward only,
  to the working branch's own name on origin, never to the default branch except <FastTrack/>.
  <PeriodicCI/> runs CI on it every fifth checkpoint of a plan with five or more
  phases.
- A phase reservation is released only by the successful-checkpoint path in
  <CheckpointCommit/>. Cancellation, error, failed commit, `single`, user stop,
  and failed release never release it or delete its durable record.
- Verify a claimed blocker against the live tree. Follow a decision already
  settled by the plan. Do not expose sandbox flags, scripts, status files,
  ledger ids, or other tooling mechanics in user-facing reports.
</CoreContract>

<ProductionUnit>
Your staff row identifies your unit and its parent showrunner.
The parent row's `name` is your production name. Its `doc.path` gives the document.
Read `~/.claude/docs/production_format.md` → <ProductionUnit/> at <PrepareSession/> and after every compaction.
Apply it throughout the run when your staff row has a parent showrunner. It changes six things in this command:
- it adds one commit kind to <CoreContract/>: merging the production's merge
  branch into this branch, at every phase start and when asked;
- it replaces <PeriodicCI/> and <CICleanup/>, because the showrunner runs CI;
- it sends checkpoint and shrink notices at <PhaseEnd/>'s points;
- it runs independent phases in parallel, mapped at plan start and at each
  <PhaseEnd/> (item 3);
- it adds a landing rule to the repair rounds in <FixDispatch/>;
- it sends design choices, Pending decisions about how something is built and
  add-ons to the showrunner, whose answer settles them (item 2). Only an
  action that cannot be undone, or a choice only the user knows the answer
  to, still reaches the user.
</ProductionUnit>

<TurnEndGate>
Ending a turn is an action this command authorizes, never a default it falls
back to. Every turn ends in exactly one of the conditions below, and **the
turn's final line names which one**:

`— holding: waiting on <what is running>` · `gate: <name>` · `decision: <question>` ·
`blocked: <cause>` · `done: run summary emitted`

The closed list:

- **waiting** — a background dispatch, verification, smoke run, or style pass is
  live and nothing synchronous remains. Name each thing by what it is and what
  it is doing, in plain words: `— holding: waiting on the Codex workers writing
  phase 2 (implementation and tests)`, `— holding: waiting on the blind code
  reviewer for phase 3`, `— holding: waiting on the final test suites`. Never
  a task id, agent id, session id or shell handle: a reader cannot tell what
  `b7emgpk3r` is. Handles are the unit director's own bookkeeping, kept for
  <CompactionContract/>. User, 2026-10-06. <DispatchContract/> step 4 and
  <BackgroundVerificationContract/> are this case.
- **gate** — a gate this command defines reaches the user by design:
  <VerbosePrePhaseGate/>, <VerbosePostPhaseGate/>, <ReviewPendingAddOns/>,
  <CICleanup/>, or the authorization round trip in <AuthorizationContract/>.
- **decision** — a genuine user decision under <DecisionRouting/>. State the
  question being asked.
- **blocked** — a hard stop this command defines: a structural check that blocks
  checkpoint, a refused tool call, a launch failure that survived
  <WorkerLaunchFailure/>'s retry. With no showrunner, alert the user to an auto-mode
  refusal with `send.py --to user --text-id auto_mode.refused` and its `--value`s (read the send rule in `send.py --help`),
  and to a wait for the user to type a go with `send.py --to user --text-id session.needs_go`.
  An update to an earlier user push on the same subject reuses that push's `--key`,
  so silence keeps only the newest. A `--need blocked` push with a live earlier one
  on its key is skipped: first `send.py retire --to user --from <sender> --key <key>`.
  A `--need blocked` push needs `--clears-when "<command that exits 0 when fixed>"`
  (`--clears-when github-warm-status`); send.py cancels it when the command exits 0,
  and refuses a blocked push without it. With no such command, send it at its own need.
- **done** — <RunSummary/> has been emitted.

Nothing else authorizes a turn end, however complete it feels:

- a finished workflow step, or a step boundary in <ExecutionSteps/>;
- a report, table, briefing, or summary of work just completed;
- a sentence naming what comes next;
- context pressure or an approaching compaction — <CompactionContract/> already
  forbids stopping for those.

Two rules hold the gate shut rather than restating it:

**Report after action.** In a turn that performs a step and reports it, the tool
call comes first and the report follows. A report is never the last thing in a
turn that still holds runnable work. <DispatchContract/> step 2 is the shape:
launch, then say what is running.

**Naming is committing.** Writing the next action in user-facing text obliges
performing it in that same turn. A closing line of the form "Next: <X>" is this
workflow's most frequent defect, and it diagnoses itself — an action specific
enough to name is specific enough to call. Name it in the past tense, after the
call, or not at all.

A turn ending with no `— holding:` line is a defect in the run, and the user may
treat it as one. When it is unclear which condition applies, none does: continue
working.
</TurnEndGate>

<ToolingContract>
Run every command under `~/.claude/scripts/direct/` with
`dangerouslyDisableSandbox: true`; do not try sandboxed first. Ledger and history
calls run in the foreground. Unqualified worker script names below resolve
under that directory. This avoids half-applied durable-state writes.

- Claude: launchers and `verify.sh final` use `run_in_background: true`; retain
  the returned task handle.
- Codex: launch the same command in a managed unified-exec terminal with
  `tty: true` and a short initial yield; retain its returned `session_id`. Do not
  shell-background the launcher: it waits for its worker and remains attached.
- Saved run output (traces, captures, logs) stays under a few GB: read each run
  and delete it before the next. A disk-floor sweeper keeps 300 GiB free on `/`
  by deleting only the shortfall, least recently used build output first, never
  from a target a build holds; build trial merges in your own worktree as usual.
  Ask natedev for room before a run that
  must keep more. Put this rule in every worker and helper prompt that saves
  output. User, 2026-10-04: 300 GB of traces cost 235 GiB of caches.
</ToolingContract>

<DispatchContract>
Applies to every implementation, test, fix, and review launcher.

1. Launch under <ToolingContract/> and save `${DISPATCH_HANDLE}`.
2. Tell the user in one line what is running and what happens on completion.
3. Perform only synchronous work assigned by the call site: the main half of
   <DualReview/> or the shrink assigned by <PhaseEnd/>. Do not inspect launcher
   output as a substitute for that review.
4. Claude: after assigned synchronous work, end the turn under <TurnEndGate/>,
   naming what is running in the `— holding: waiting on <what is running>`
   line, never its handle. A task message resumes the workflow.
5. Codex: apply <CodexDispatchWait/>. Never end the turn while the launcher is
   active; its terminal result drives the next workflow step.
6. For `herdr`, recover an incomplete pass under <LongLivedWorkers/> before another role's dispatch.
   For an existing `mesh` pin, a launcher killed at its time limit leaves its Codex
   worker running with no one watching. Arm a Monitor on
   `${SESSION_DIR}/board.log` until that worker
   posts its own `done`, not a `launcher:` line. Once it has, and its last lint
   and test passed after its last edit, retain its native conversation under
   <LongLivedWorkers/> and record the outcome the launcher would have; for a
   repair, that is <FixDispatch/>'s third outcome.
</DispatchContract>

<WorkerRuns>
A Codex worker runs only commands that finish within one call and never makes
an empty `write_stdin` poll. A longer command, a `verify.sh` run or any other
started with `mesh_runs.py start`, detaches and pushes its own result to the
worker, per <WritePromptContract/> item 3. A live run keeps the worker's
launcher attached, so the dispatch finishes only when the worker is done.
Records are in `${SESSION_DIR}/mesh_runs/`, each log beside its record.
`start` checks for a live twin before it applies `config/mesh_runs.conf` `LIVE_RUN_LIMIT` to that worker.
At the limit, exit 4 starts nothing and names the live runs on stderr.
Wait for a run's result as the next message, or put more files in one run.
Exit 3 means the caller has no registered worker route; only that exit permits attached execution.

For `herdr`, keep the pane and native session during detached work.
Results use the worker's native identity and observed pane.
Delivery reads the transcript and does not repeat a recorded result.
After a stop, a rerun can type a detached result twice without loss; add no check for the duplicate.
Stopped, blocked, closed, or uncertain workers' results go to their holder with the reason.
Unknown process evidence produces one anomaly line and permits no pane input.
Use the result without sending another dispatch prompt.

For an existing `mesh` pin, a result can report that no launcher is attached.
Dispatch that worker through `assign.sh --role implement --to <full worker name>` when it must act on the result.

When a decision makes a running or queued test, lint or build run useless,
cancel it in the same turn to free its memory and its place in the queue.
This applies when a phase is parked, a round fails, a newer tree replaces the
run's tree, or a result already in hand answers the question. No run finishes
only to complete a report.

- For a `mesh_runs.py` run, read `pid` from its record in
  `${SESSION_DIR}/mesh_runs/` (the `.json` beside its log). Signal only while
  its `status` is `running` and `ps -p <pid> -o args=` shows
  `mesh_runs.py supervise` with that record's path, so a reused pid is never
  hit; then run `kill -TERM -- -<pid>`. A start that found the same run live
  prints no pid: read it from that run's record.
- A detached `verify.sh` run uses the same record and command: `verify.sh`
  detaches through `mesh_runs.py` and handles TERM to stop its step.
- For a run in the memory queue, cancel its owning run as above. The queue
  drops dead callers on its next read. It has no separate cancel command;
  `memory_admit.py release` only releases a reservation. A clean queue cancel
  and one command for all run kinds are not built yet
  (`docs/plans/sdlc-run-cancel-design.md`); report any run you cannot cancel.
</WorkerRuns>

<WorkerLaunchFailure>
Read `~/.claude/docs/direct/launch_failure.md` in full when a dispatch fails.
</WorkerLaunchFailure>

<CodexDispatchWait>
Read `~/.claude/docs/direct/codex_dispatch_wait.md` in full when the director is Codex.
</CodexDispatchWait>

<BackgroundVerificationContract>
Run every `verify.sh` call as one plain command,
`bash ~/.claude/scripts/direct/verify.sh --session-dir "${SESSION_DIR}" <command> [<package>] [flags]`,
with no env prefix, redirect or trailing command: the settings allow rule
matches only that shape, and the task notification already carries its exit
code. For `verify.sh final`, launch under <ToolingContract/> so `verify.sh`
opens its own progress window, and tell the user what is running. Claude ends
the turn and resumes from a task message; Codex applies
<CodexDispatchWait/>.
</BackgroundVerificationContract>


<CompactionContract>
- Apply <LongLivedWorkers/> to Codex worker compaction and overflow continuation;
  the rules below govern the unit director's own context.
- Do not maintain a handoff before the context hook requests one.
- When requested, write it in the repository and include the hook's fields plus
  `MODE`, `AUTO_WINDOW`, the last authorization, whether the user stopped
  progress updates, any live `DISPATCH_HANDLE`, any live
  `REVIEW_DISPATCH_HANDLE` with `EARLY_REVIEW` and `REVIEW_PASS`,
  `NEXT_ITEMS_PATH`, whichever tagged
  `PhaseReservationState` is live, any pending <PhaseEnd/> work, and any
  unresolved next-item approval, and any overlap sent to the showrunner and not
  yet answered: its holder ids, paths and answer choices. Exclude the
  handoff from review intent-to-add and commits.
- Never stop or delay work for compaction. Claude resumes from a live-dispatch
  notification; Codex remains in <CodexDispatchWait/>.
- A wait you cannot fill is the exception. With nothing left to do but wait on
  background work, end the turn; the Stop hook stands down there, and the
  wake-up request compacts on its own.
- After compaction, read this core in full. Read each stub file whose moment is live, and then the handoff. Restore live
  dispatches and state. Independently read and validate
  each open phase's `${PHASE_DIR}/delegated_phase_reservation_state.json` once coordination has
  reached a persisted state, including its durable `checkpoint_commit` when
  release confirmation is pending; the handoff, conversation, and the session
  mapping are never reservation-lifecycle memory. Delete the handoff only after
  checkpoint and any active release both succeed. A
  dispatch the handoff records as live but that is gone did not survive: resolve
  it per <FixDispatch/> before trusting any state it was supposed to have written.
- A real user decision may still end the turn after the Stop hook's one retry.
</CompactionContract>

<UserFacingText>
For every briefing, decision, progress update, review result, and report, read
and follow `~/.claude/docs/user_facing_explanation.md`. Reconstruct context for
the user; do not pass through internal review or tooling vocabulary. Never
measure work in lines of code, insertions, or file counts anywhere in that text
— describe what the code does, not how much of it there is.
</UserFacingText>

<ExplainOnDemand>
If the user is confused or asks for a reframe at any gate, preserve the gate and
read `~/.claude/docs/explain_on_demand.md`. Explain from concrete behavior and
real signatures, with problem/fix code examples where required. Explanation is
not authorization; restate the pending question afterwards.
</ExplainOnDemand>

<TypeDesignContract>
Read `~/.claude/docs/type_design.md`. Apply it in the main review and put it
under `## Type Design Contract` in every review, implementation, and fix
prompt. Copy it verbatim into opening prompts; any follow-up may instead point
to that file per <WritePromptContract/>.
</TypeDesignContract>

<WritePromptContract>
Read `~/.claude/docs/direct/write_prompt_contract.md` in full before writing any
implementation, fix, or review prompt. It also defines <PhaseTeam/>,
<CoordinationBoard/>, <PhaseMesh/>, <BuildTokenContract/>,
<TeamFilePartition/>, and <RoleReassignment/>.
</WritePromptContract>

<VerificationContract>
Read `~/.claude/docs/direct/verification.md` in full before verification.
It also defines <VerificationNarration/>.
</VerificationContract>

<FindingsLedger>
Read `~/.claude/docs/direct/repair_rounds.md` in full when a review, gate, or verification result returns.
Read it before each repair dispatch.
It also defines <PassOwnership/>, <Synthesize/>, <FixDispatch/>.
</FindingsLedger>

<ProgressContract>
Before stating any ETA in a message, reply or notice, record it with
`progress_history.py eta --session-dir "${SESSION_DIR}" --time <HH:MM> --basis stated`
(`--basis checkpoint` for checkpoint notices, `--basis eta-command` for
`/unit:eta` and `/unit:eta_breakdown`). Add `--phase-id <id>` when it names a
phase other than the active one. `<HH:MM>` means today in the user's zone; an
ETA on a later day takes `<YYYY-MM-DDTHH:MM>`. State the time the recorder
printed.

Before every Codex poll, set `${PROGRESS_INTERVAL_SECONDS}` from
`UNIT_PROGRESS_INTERVAL_SECONDS` in `~/.claude/config/direct.conf`.
It sets the Codex poll timeout. A missing or non-positive integer is an error;
tell the user to set it. No timer sends reports: a report comes only when
someone asks (2026-10-19).

Launcher work is a **pass**; unit-director work -- verification and smoke -- is
an **activity**. `verify.sh` opens and closes its own whenever it runs with
`--session-dir`; open one by hand for other unit-director work
with
`progress_history.py start-activity --session-dir "${SESSION_DIR}" --label <label> --activity <what>`
and close it with
`finish-activity --session-dir "${SESSION_DIR}" --status <status> --result <outcome>`.
Keep `--label` to one or two words -- it names the row -- and make `--result` the
short outcome that row should show: `pass`, `clean`, `no change`. Without one
the row can only say `done`, which reports that the window closed rather than
what it found. Activities sit beside passes and are invisible to `findings.py`,
which is why <PassOwnership/> forbids faking a pass for the same purpose.

A requested status check — `/unit:report`, or a user or showrunner asking —
emits <ProgressReport/> immediately, which `~/.claude/commands/unit/report.md`
defines in full. Read that file and follow it; a report written from memory of an
earlier read drops the byte-for-byte copy rule first. It also owns that report's
<EarlyReviewArm/> trigger point and the query that answers questions about work
already finished. A report never replaces the completion report: when work
finishes, report its result and current progress header.

A `conversation-pause:` message asks you to put its question to the user word for
word. Never answer it for them.
</ProgressContract>

<AuthorizationContract>
Read `~/.claude/docs/direct/authorization.md` in full when an action needs user authorization.
</AuthorizationContract>

`<DecisionEconomy/>` is defined by this import, shared with every session:

@~/.claude/docs/decision_criteria.md

<DecisionRouting>
For every decision raised by review, repair, or phase review:

- If the plan already defines behavior and only ordering is wrong, resequence,
  split, merge, or renumber phases; preserve scope, API, invariants, tests, and
  ownership. Update affected Work Orders and continue automatically.
- If current-phase correctness has at least two buildable unresolved answers,
  apply <DecisionEconomy/>; stop and ask only when the tradeoff survives it.
- If only a later phase is affected, write a `**Pending decision:**` block using
  `~/.claude/docs/direct_plan_format.md`, report the deferral, and continue.
  That phase's pre-dispatch check will stop on it.
- If an alleged option cannot be implemented by the phase's actual structure,
  correct the plan instead of presenting it as a choice.
</DecisionRouting>

<ExecutionSteps>
Execute in order:

Apply <CoreContract/>, <TurnEndGate/>, <CompactionContract/>,
<UserFacingText/>, and <VerificationNarration/> throughout, and
<ProductionUnit/> when your staff parent is a showrunner.

The numbered steps below carry no turn boundaries. Consecutive steps run in one
turn unless <TurnEndGate/> authorizes a stop between them. Steps 1–8 apply to
all modes. Step 9 ends `single`; step 10 applies to loop and verbose.

1. <PrepareSession/>
2. <ComposeWorkOrder/>
3. <VerbosePrePhaseGate/> when required
4. <CoordinatePhaseReservation/>
5. <LaunchImplementation/>
6. <PhaseChecks/> at <DualReview/>. Beside the implementation pass's review, run
   <RunApplicationSmokeTest/> and <UXReview/> steps 1–3; open their rows and let
   the first <Synthesize/> gate them with the code findings.
7. <Synthesize/>
8. <RunApplicationSmokeTest/>, then <UXReview/>, unless step 6 ran them and no
   later change reached the app or the screen
9. `single`: <RunPhaseReview/>, <RunPhaseShrink/>, <ConsiderNextItems/>,
    <PhaseCleanup/> in full, then <RecordPhaseCompletion/> and end. It never
    commits or enters the remaining steps.
10. Loop and verbose: <PhaseEnd/>.
</ExecutionSteps>

<PhaseEnd>
Read `~/.claude/docs/direct/phase_end.md` in full when a loop or verbose
phase's gates pass. Follow its order through the next turn boundary.
</PhaseEnd>

<PrepareSession>
Run `bash ~/.claude/scripts/direct/prepare_session.sh` under
<ToolingContract/>. Capture `SESSION_DIR` and set `WORKING_DIR`. The script also
creates the run-active marker; every exit must
eventually run `end_session.sh`
through <RunSummary/> or single-mode completion.

When the plan header carries a `> **Issues:**` line, mark each issue it names
as being built:
`~/.claude/scripts/lib/py ~/.claude/scripts/production/proposal_issue.py start <issue>`.
</PrepareSession>

<ComposeWorkOrder>
Read `~/.claude/docs/direct/compose_work_order.md` in full and apply it at the
start of every phase, before any prompt is written. It owns Work Order
validation, the `**Pending decision:**` re-test, mode parsing, the run start,
`${NEXT_ITEMS_PATH}` derivation, and both the direct-ready fast path and the
research fallback. Read it every time: a work order composed from memory is the
most common cause of a phase that builds the wrong thing.

If an initial verbose invocation contains a bounded-auto control, resolve
`AUTO_WINDOW` and run <AutoWindowBatchBriefing/> before
<CoordinatePhaseReservation/>. Otherwise
follow <AuthorizationContract/>.
</ComposeWorkOrder>

<PhaseReservationContract>
Read `~/.claude/docs/direct/phase_reservation.md` in full at phase start and checkpoint release.
Read it after compaction with a live reservation.
It also defines <RetainPhaseReservation/>, <CoordinatePhaseReservation/>.
</PhaseReservationContract>

<BerthDecisions>
Read `~/.claude/docs/direct/berth_decisions.md` in full when a cargo-berth decision applies.
</BerthDecisions>

<VerbosePrePhaseGate>
Read `~/.claude/commands/unit/brief.md` in full at every pre-phase gate,
briefing, or auto control, and before a phase report's types table. It also defines
<BriefingFreshness/>, <PhaseBriefing/>, <TypeTableCells/>, <CombinedWindowBriefing/>,
and <AutoWindowBatchBriefing/>. The user can invoke it as `/unit:brief`.
</VerbosePrePhaseGate>

<LaunchImplementation>
Read `~/.claude/docs/direct/launch_implementation.md` in full before the phase's
first dispatch, and again when that dispatch completes: steps 7-8 route its result.
</LaunchImplementation>

<ReviewDiffContract>
Before every broad or closure review, run `git status --short` and apply
`git add -N` to each new phase-created file so `git diff` contains it. Exclude
pre-existing untracked and unit-director-owned handoffs. Verify every created
file named by the worker is visible; stop if not. Capture the diff and status.
</ReviewDiffContract>

<EarlyReviewArm>
Read `~/.claude/docs/direct/early_review.md` in full before each implementation or fix dispatch.
</EarlyReviewArm>

<DualReview>
Read `~/.claude/docs/direct/dual_review.md` in full before writing any review prompt:
after an implementation or fix completes, and when <EarlyReviewArm/> arms. It also
defines <TeamReview/>, <ReviewPromptContract/>, <BroadReviewPrompt/>, and <ClosureReview/>.
</DualReview>

<DelegationResultFormat>
Use <UserFacingText/> and emit:

```
## Delegation Result

### Where things stand
[what now works and what verification established]

### What's left
[numbered plain-language issues: behavior/risk, frequency, fix cost]

### Reference
| # | Severity | File:line | Technical problem | Caught by |

### Reviewer disagreements
[only when present]
```

Summary and reference numbers must match. A reader should not need the plan,
diff, reviews, or finding ids.

**Close every delegation result and launch with the short progress report**
from <ProgressReport/>: phase and stage, ETA line, workers, summary, board link,
and wall clock. Copy the recorder's phase, stage, ETA and clock exactly. Include
the summary table in every report; include the round table only for
`/unit:report tables`.

Between windows, keep the recorder's `as of` and no-open-window lines. An
activity still open after its phase closes remains the stage. If the recorder
reports `No active phase to report`, continue without the progress report.
</DelegationResultFormat>

<UXReview>
Read `~/.claude/docs/direct/ux_review.md` in full before phase UX review or an application smoke test.
It also defines <RunApplicationSmokeTest/>.
</UXReview>

<RunPhaseReview>
Read `~/.claude/docs/direct/run_phase_review.md` in full once per phase.
In loop and verbose, apply it at <PhaseEnd/>'s point. In `single`, use
<ExecutionSteps/> step 9. The file also defines <RunPhaseShrink/>.
</RunPhaseReview>

<ConsiderNextItems>
Read `~/.claude/commands/unit/add_ons.md` in full and apply it at
<PhaseEnd/>'s point in loop or verbose; in `single`, use <ExecutionSteps/>
step 9. Phased plans only; the unit director performs the assessment and never
launches another agent for it. It writes `apply`
corrections, accumulates every add-on in `${NEXT_ITEMS_PENDING}`, and asks
nothing. Never work from memory of an earlier read — `Class` obedience and the
single-line reporting rule are what drift.
</ConsiderNextItems>

<ReviewPendingAddOns>
Defined in `~/.claude/commands/unit/add_ons.md`; read it in full before
each run. It walks accumulated add-ons through `/adhoc_review` with
`current / next / drop`, only at an interactive point and only when
`${NEXT_ITEMS_PENDING}` is non-empty. It is the one route by which an add-on
reaches the plan or `${NEXT_ITEMS_PATH}`. The user can invoke the same file as
`/unit:add_ons`.
</ReviewPendingAddOns>

<CheckpointCommit>
Read `~/.claude/commands/unit/checkpoint.md` in full and apply it once
per completed loop or verbose phase at <PhaseEnd/>'s point. `single` never
commits.

This is durable state with no cheap undo, so read the whole contract before
acting on any part of it, and read the reservation record from disk. A value
remembered from conversation, taken from the harness session mapping, or
re-derived from current `HEAD` is not proof and will silently accept the wrong
checkpoint. The user can invoke the same file as `/unit:checkpoint`.

The phase does not complete until the reservation release is confirmed, so a
failed or busy release applies <RetainPhaseReservation/> rather than a
retry. <PushCheckpoint/> runs only after that release; a failed push never
fails the checkpoint.
</CheckpointCommit>

<ShrinkCommit>
Standing plans skip this tag entirely: their excluded plan and pinned files
are never staged, and no shrink commit or notice is sent.

Read `~/.claude/commands/unit/checkpoint.md` in full and apply it at
<PhaseEnd/>'s point in loop and verbose. It commits the plan doc and an approved
`${NEXT_ITEMS_PATH}` change, then runs <PushCheckpoint/>. It owns no
reservation and calls no release. When a next phase is running, its first
plan-doc edit acquires that phase's reservation in an enrolled repository.
</ShrinkCommit>

<PhaseCleanup>
For `herdr`, apply <LongLivedWorkers/> to close idle panes and retain worker rows for the next phase.

For an existing `mesh` pin, run this command at <PhaseEnd/>'s worker cleanup point:
`~/.claude/scripts/lib/py ~/.claude/scripts/direct/remove_workers.py --session-dir "${SESSION_DIR}"`.
It removes the completed phase's Claude workers and any worker a dead run left
alive; a failure is one line in the report, never a stop. Under
<LongLivedWorkers/>, do not end this run's Codex workers: their turns finish, their
threads stay open, and `end_session.sh` ends them with the run.

For both transports, stop each Claude Agent helper and end finished launchers.
Shut down every Hana or example app this phase launched. Never touch what another session started.
Mid-phase, stop each helper once its result is read and each app once no step
uses it.

In `single`, run both cleanup halves under <ExecutionSteps/> step 9.
At <PhaseEnd/>'s review-prose cleanup point, run
`bash ~/.claude/scripts/direct/clear_phase_review.sh "${SESSION_DIR}" <phase-id>`.
It removes only the completed phase's review prose; structured progress
history remains.
User, 2026-10-03.
</PhaseCleanup>

<RecordPhaseCompletion>
In loop and verbose, run at <PhaseEnd/>'s point; in `single`, follow
<ExecutionSteps/> step 9. Call `progress_history.py finish-phase
--session-dir "${SESSION_DIR}" --status completed`, adding `--phase-id <id>`
while another phase is open.

`finish-phase --status completed` clears a `RepositoryNotEnrolled` or
`EnrolledAwaitingFirstTouch` state file by itself; delete nothing by hand.
`Active` must already have been replaced by
`CheckpointCommittedAwaitingReleaseConfirmation`, and that pending state must
already have been cleared by the validated successful release. If either state
survives, the phase is not complete and this section must not run.

In `single`, also run `finish-run --status completed`, then run <RunAsBuilt/>,
apply <RetainPhaseReservation/>, report that no checkpoint release was
attempted, run `bash ~/.claude/scripts/direct/end_session.sh`, and end. Other
modes continue.
</RecordPhaseCompletion>

<VerbosePostPhaseReport>
Read `~/.claude/commands/unit/phase_report.md` in full and apply it
after a completed verbose phase outside an auto window. Inside an active window,
emit no per-phase report; when the window's last phase completes, emit one
combined report instead. That file defines this contract,
<CombinedWindowReport/>, and <RemainingWorkOutlook/>. Never compose the report
from memory of an earlier read — the phase count and the closing control line
are what go missing. The user can invoke the same file as
`/unit:phase_report`.
</VerbosePostPhaseReport>

<VerbosePostPhaseGate>
Skip when no todo phase remains or an auto window continues. Otherwise run
<ReviewPendingAddOns/>, then ask:

`Reply \`continue\` when you are ready to review the next phase's pre-phase briefing, \`auto next N phases\` or \`auto through phase X\` to open a window, or \`stop\` to end the run.`

`continue` authorizes only composing that briefing. An auto control accepts the
outlook in <RemainingWorkOutlook/> and applies <BriefingFreshness/>: the covered
phases are unbriefed here, so it routes to <AutoWindowBatchBriefing/>, which
briefs each one and gates once before any dispatch. A window the user sizes
differently from the recommendation is authoritative. `stop` ends through
<RunSummary/>. `proceed`, `approved`, questions, or discussion do not advance;
answer from the completed report and preserve the gate.
</VerbosePostPhaseGate>

<NextPhase>
At <PhaseEnd/>'s next boundary, a plan whose header carries
`**Standing:** style_plan.py` runs <StandingPlanRefill/> first. Otherwise choose
the next phase or close the plan. If no
todo phase remains, run <FinalGate/>, <RunAsBuilt/>, the final <PeriodicCI/>
point and <CICleanup/>, then <RunSummary/>.

For `next N`, decrement after completion and clear at zero. For `through X`,
clear after X. In verbose mode outside an active window, use <PhaseEnd/>'s
report and gate.

For loop or an active auto window, reset `REVIEW_PASS=0` and smoke to
`not_run`. Run <PeriodicCI/> when a CI point is due, after the shrink commit
per <PhaseEnd/>, and repair a red result before
dispatching. Announce the next phase, run
<ComposeWorkOrder/>, <VerbosePrePhaseGate/> when required,
<CoordinatePhaseReservation/>, and <LaunchImplementation/>. Its
`start-phase` runs before dispatch.
Every return rechecks Pending decisions.
</NextPhase>

<StandingPlanRefill>
Read `~/.claude/docs/direct/standing_plan_refill.md` in full when the plan header has `**Standing:**`.
</StandingPlanRefill>

<FinalGateCommit>
Read `~/.claude/docs/direct/final_gate_commit.md` in full at the final gate or when a `single` task finishes.
It also defines <FinalGate/>, <RunSummary/>, <RunAsBuilt/>, and <AsBuiltCommit/>.
</FinalGateCommit>

<RunAsBuilt>
Defined in `~/.claude/docs/direct/final_gate_commit.md`; read it in full at a complete
run's end, including a finished `single` task, then apply <AsBuiltCommit/>.
</RunAsBuilt>
