---
description: Direct phased work with phase checks, review, repair, smoke gates, as-built shrink, an as-built doc pass at the end of the run, approved follow-up capture, and one checkpoint per phase. Supports automatic loop, verbose gating, bounded auto windows, and single no-commit mode.
---

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
For LongLivedWorkers and LaunchImplementation, the director briefs and accepts;
Astra authors the assigned design. Use one `design_roles.py` designer and at most one
reviewer per design, with models resolved from `design_roles` in `agents.conf`. Open
once; resume the same instance across rounds, phases and runs. `design_roles.py` saves
each headed prompt and reply; do not also use `astra_work.py brief` for that role.
For a designer outside `design_roles.py`, include `config/design_roles/designer.md` in its brief
and save it with `astra_work.py brief --run-directory <SESSION_DIR> --phase <id>
--member <mesh name> --document <path> --round <N> --text-file <brief>`.
Save its reply as `<SESSION_DIR>/design/phase<id>/round<N>_reply.md` without
overwrite. Pass both `--run-directory` and `--phase` to phase `astra_review.py` calls.

For DelegatedPhaseReservationContract, prepare exact phase files, baseline and
reservation before dispatch. Bind the designer to those output paths with explicit
`PhaseBoundDesigner` access; reviews use `ReadOnlyReviewer`. Repository edits use
patches. Finish a turn and rebind while idle before adding a path or changing scope.
A stale binding, recovery state, adoption fence or unavailable holder blocks dispatch.

For DualReview and FixDispatch, keep the same reviewer and writer. Save findings,
responses and short rulings that cite files; update the Work Order for a scope
change. Busy or unavailable review is not a pass and permits no replacement.

For PhaseCleanup, FinalGate and RunSummary, finish turns, release the reservation,
then unbind the design. Keep role threads, pending messages and evidence until
explicit end. Run cleanup must not stop durable meshes. Account moves and holder
restarts recover the same threads through the role lifecycle; never raw pause or
silently replace a lost thread. Adoption uses its recorded transfer and destination.
The existing checkpoint, visual check and gate order still applies.
For rollback, set `[roles] new_open=false` in `config/design_roles.conf`. Finish active
turns and release servers through the holder lifecycle. Keep placements, fences,
pending input, prompts, replies and unfinished reservations. Keep adopted ownership
at its destination unless a reverse transfer proves no destination writer. Restore
old command text only when it preserves these fences; never replace a thread.
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
  (`board.log`, `heartbeat.log`, `mesh_server.json`, `mesh_roster.json`, the
  findings ledger and recorder state) stay in `${SESSION_DIR}`.
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
| <FinalGateCommit/>, <RunAsBuilt/>, <AsBuiltCommit/> | `docs/direct/final_gate_commit.md` | — |
| <ProductionUnit/> | `docs/production_format.md` | — |

Paths are under `~/.claude/`.

The `plan:` commands a run uses are loaded the same way: Read the file, never
the Skill tool. Claude Code puts every skill a session invoked back
after each compaction for the rest of the session; a file loaded by Read is not.
A call site's arguments are what that file calls `$ARGUMENTS`.
</TagReferenceContract>

<LongLivedWorkers>
Unit workers persist across phases and plans. Dispatch with `assign.sh --role
implement|test|fix|review --prompt <path> --cwd <worktree>`. The unit owns its
read-only `review` worker; a concurrent review uses `review2`. The first use
adopts a leased reviewer on the same thread. A busy leased reviewer finishes
before adoption; new work uses `review2` meanwhile.

Keep each worker's thread. Pause, move and end it with the unit ledger as
`docs/as-built/long-lived-workers.md` describes. Never open a fresh reviewer
while its thread exists. Every review prompt names `config/review/code.md`
under <ReviewPromptContract/>. Other prompts carry the Three Gods and Type
Design Contract under <WritePromptContract/>.

The launcher compacts a worker above `UNIT_COMPACT_ABOVE_TOKENS`, then resumes
it. A failed compaction leaves the thread available for its own compaction.
</LongLivedWorkers>

<CoreContract>
- Never create a worktree or modify unrelated files. The only branch the run may
  create is the remote branch <PushCheckpoint/> pushes; never switch to an
  existing branch.
- The unit director does not write implementation code unless the user explicitly
  asks. Exceptions: agreed doc-only/trivial post-review fixes.
- `single` never commits. Loop and verbose modes create one
  <CheckpointCommit/> and one <ShrinkCommit/> per completed phase, plus the one
  <FinalGateCommit/> that closes verification and the one <AsBuiltCommit/> that
  carries the run's documentation. <PeriodicCI/> may add `ci(<plan-slug>): …`
  commits for validation fixes and CI repairs. No other commit is allowed.
  Only <ShrinkCommit/> runs while workers edit, and it commits only the plan doc
  and an approved `${NEXT_ITEMS_PATH}` change.
- Every commit the run makes is pushed by <PushCheckpoint/>, fast-forward only,
  to the working branch's own name on origin, never to the default branch.
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
When the plan header carries a `> **Production:**` line, this run is a unit of
that production. Read `~/.claude/docs/production_format.md` → <ProductionUnit/>
in full at <PrepareSession/> and after every compaction, and apply it
throughout. It changes six things in this command:
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
  refusal with `send.py --to user --text-id auto_mode.refused` and its `--value`s (`send.py --help`),
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
6. A new-worker or follow-up launcher killed at its time limit leaves its Codex
   worker running with no one watching. Arm a Monitor on
   `${SESSION_DIR}/board.log` until that worker
   posts its own `done`, not a `launcher:` line. Once it has, and its last lint
   and test passed after its last edit, leave its thread open under
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
A message from a worker's run saying no
launcher is attached carries the result to you: dispatch that worker
(`implement.sh --to <full worker name>`) when it should act on it.

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
A dispatch that dies in seconds never reached the provider, and a provider
message it prints is a message some earlier call received. Workers launched
together that fail within seconds of each other carrying **byte-identical text,
the same retry-at timestamp included, are a local fault**: separate API calls do
not produce identical text. Confirm it against
`${SESSION_DIR}/mesh_server.log` — a launch that got out of this machine wrote
to it, and one that added no line was answered by the session's own
`codex app-server` from what it had cached. Never report workers as
unavailable, or a usage limit as reached, on the error text alone.

`codex exec` cannot test this and must not be used to. It opens its own process
against the API, where a dispatch attaches a thread to the long-lived server
recorded in `${SESSION_DIR}/mesh_server.json` — a different path that can fail
while `codex exec` answers. That disagreement is the wedged server's signature,
not evidence workers are back. The only valid probe is the dispatch itself.

The launcher already tried. On a fast failure with no work done, or a stale Codex
sign-in (`access token could not be refreshed`) even after the thread exists, `codex_mesh.py`
abandons the inherited server, starts one of its own and runs the worker again,
printing `retrying on a new app-server` to the worker's log. So an error that
reaches you has usually already been tested against a clean server and is real.
Read the log for that line before doing anything by hand: present, the retry
happened and the failure survived it; absent, the launcher held back — the worker
had already done work, or the failure took too long to be local — and the
paragraphs above are yours to apply.

A new Codex sign-in moves the run by itself. A message from `codex-sign-in` names
the server it retired; the next dispatch starts on a new one, a worker mid-turn
finishes first, and nothing is owed in reply.

Recovering by hand, in that case only. Check that no peer run claims the
recorded pid or port before signalling anything —
`grep -l '<pid>\|<port>' /tmp/claude/unit/*/mesh_server.json /tmp/claude/delegate/*/mesh_server.json` should name
only this session's file, and any second file means leave the process alone.
Then kill that pid, move `mesh_server.json` aside, and relaunch verbatim; the
next launcher starts a fresh server. Leave `mesh_roster.json` alone — its other
entries may name threads still live on a server that is fine. If the log **is**
growing and the error arrives new on each launch, the limit is real and none of
this applies.
</WorkerLaunchFailure>

<CodexDispatchWait>
Codex only; no timer process:

1. Empty-poll `${DISPATCH_HANDLE}` with `write_stdin`. Set `yield_time_ms` to the
   configured interval from <ProgressContract/>, capped by
   `background_terminal_max_timeout`. Do not use shell `wait`, `sleep`, a status-file loop, or a second
   terminal.
2. A result with the same `session_id` and no `exit_code` means the interval
   elapsed and the launcher remains active. Apply <EarlyReviewArm/>, then poll
   the same session again. A poll timeout composes no report.
3. An initial launch or poll result with `exit_code` means the launcher finished.
   Clear `${DISPATCH_HANDLE}`, read the call site's status/result files, and
   route to review, synthesis, repair, smoke, or the next stage immediately in
   this turn. Do not end the turn between completion and routing.
4. A user message may interrupt the poll. Answer it, retain the session handle,
   and resume this contract unless the user cancels or redirects the run. An
   instruction that steers the run is an interactive point: act on it, then
   run <ReviewPendingAddOns/>.
5. An early-launched reviewer occupies its own managed terminal under
   `REVIEW_DISPATCH_HANDLE`. Keep polling the primary dispatch session; each
   timeout is also the <EarlyReviewArm/> evaluation point. After the primary
   completes and the ready sentinel is written, poll the reviewer session under
   this same contract.
</CodexDispatchWait>

<BackgroundVerificationContract>
Run every `verify.sh` call as one plain command,
`bash ~/.claude/scripts/direct/verify.sh --session-dir "${SESSION_DIR}" <verb> …`,
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
- After compaction, re-read this command in full, then the handoff. Restore live
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
Rust workers run only exact prompt lines using
`~/.claude/scripts/direct/verify.sh`:

| Intent | Command |
| --- | --- |
| compile feedback | `bash ~/.claude/scripts/direct/verify.sh check <package> [--keep-going]` |
| package tests | `bash ~/.claude/scripts/direct/verify.sh test <package>` |
| one integration target alone | `bash ~/.claude/scripts/direct/verify.sh test <package> <test>` |
| only the tests whose name contains `<name>`, while iterating | `bash ~/.claude/scripts/direct/verify.sh test <package> --filter <name> [--filter <name> …]` |
| a test that cannot pass on macOS, run on natedev (before CI either machine answers a test, merge gates included; user, 2026-10-16) | `bash ~/.claude/scripts/direct/verify.sh test <package> --local "<why it cannot pass on macOS>"`; never to keep tests off the Mac: a reason that names a reservation is refused, and a Mac hold (`/build:hold hold <why> target mac`) does that |
| phase lint checks under <PhaseChecks/> | `bash ~/.claude/scripts/direct/verify.sh lint <package>` |
| changed example | `bash ~/.claude/scripts/direct/verify.sh example <package> <name>` |
| final workspace gate | `bash ~/.claude/scripts/direct/verify.sh final` |

Rules:

- Serialization and result authority follow <BuildTokenContract/>: every line
  here takes the `cargo` token on its own, so a run may wait for a peer, and a
  result is a gate only once the slot owning that package's files has posted
  `done`.
- `check` and `test --filter` are feedback while iterating, never a gate.
  Run `check` after each batch of edits, and `test --filter` once per finished
  change, naming all its tests in one call (repeat `--filter`). Never re-run
  either on a tree that has not changed. User, 2026-10-04: workers ran a
  `--filter` after every edit, 619 test runs in six hours.
- Start `test` for every modified package as soon as the code is ready. Never
  wait for lint to finish or for a natedev build hold to end before starting
  tests that can run on the Mac. Run `lint` once after the worker's last edit,
  naming any one modified package, in either order with `test`. Both must pass
  before the phase is done. `lint` rewrites the tree (mend fixes, then format),
  so a `test` run counts only when no later lint changed a file. Read
  `git diff --stat` before and after lint; when lint changed a file, run `test`
  once more on the resulting tree. Never re-run lint or test on an unchanged
  tree. Showrunner, 2026-10-28: tests that waited for lint never reached the
  free Mac while a natedev build hold held the lint.
  Lint is never per crate: one run covers the whole workspace (mend
  and clippy across it, format for every changed member; the unit director
  runs workspace rustdoc with `verify.sh doc` at the checkpoint). A failed
  lint means fix the error it names, wherever it is in the workspace, then lint
  once more. User, 2026-10-01: a Work Order that said "for each crate, lint
  first" cost 13 lint runs of one workspace, all failing on one error. Trace
  changed public APIs,
  traits, registration, and plugin wiring to modified callers. Name an integration target explicitly only to re-run it
  alone. Add example lines only when the phase owns them.
- Tests are the only testing: a passing `test` run proves the package builds.
  Never run `check` or any build alongside a `test` that is going to run anyway;
  `check` exists solely for mid-edit compile feedback.
- A launch is not a time to build or test: `verify.sh example` and any app or
  binary launch compile their own target. Never precede or follow a launch with
  a build, `check`, or `test` pass just to prove it builds.
- Phase prompts never use `final`, raw Cargo, or compose their own Cargo flags.
  Workspace lint coverage belongs to <PhaseChecks/>; <FinalGate/> owns the
  workspace all-targets check and full tests.
- Non-Rust prompts list the project's exact scoped commands under the same
  run-only-what-is-listed rule.
- Type checks are per slot. In a Python project, each slot's prompt gets its
  own line, `basedpyright <the Python files on its Workers line>`, which it
  runs after its last edit and before it posts `done`. Only the footer counts:
  it must read `0 errors, 0 warnings, 0 notes` (the exit status says nothing).
  A plan's folder-wide basedpyright line becomes these per-slot lines; never
  give one slot a run over files a peer owns. User, 2026-10-21: the impl
  worker got the phase-wide run, its 13 warnings were in the test worker's
  files, and the test worker had to be launched again.
- Prove any claimed pre-existing failure on the pre-phase tree before mentioning
  it. Do not stash; use the existing clean tree or a scratch checkout.
- Re-run suspected environment failures unsandboxed before classifying them.
  Nested Swift `sandbox-exec` failures and missing GPU adapters are environment
  failures, not dependency or code defects.
- A printed `SKIPPED` is skipped, not passed; do not bypass a disabled check
  manually.
- `LINT_OP_STYLE_REVIEW=off` makes the phase style review report `Off`.
</VerificationContract>

<VerificationNarration>
Whenever choosing how a change gets verified — running tests, launching a
binary or example, or dispatching a worker that will — state the choice and
its reason to the user in one line: `Running tests only because …`,
`Launching <binary|example> because …`, or `Worker will run tests only
because …`. The line makes the verification economy visible; it never replaces
recording the result.
</VerificationNarration>

<FindingsLedger>
Use `python3 ~/.claude/scripts/direct/findings.py <command> --session-dir
"${SESSION_DIR}" --phase "<phase id>"` for confirmed style violations in the
main review, or after <Synthesize/> confirms another issue. Each open phase has
its own ledger, and `--phase` names which one: with two phases open, a call
naming none is refused. Every launch names its phase (<PassOwnership/>), so the
launcher's own `landed` and `abandon` reach that phase's ledger.

| Command | Purpose |
| --- | --- |
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <worker\|main\|both> [--lens <adversary\|contract\|craft\|ux\|rerun>[,…]] [--detail <d>]` | create an id; `--lens` names every lens that raised it, comma-separated; legacy `both` means `adversary,contract` |
| `status` | read the ledger for closure review |
| `gate` | get `converged` or `dispatch`, plus batch and any advisory |
| `dispatch --covers F001,F002,... --accepted-checks "<checks or none: reason>" [--keep F001=<regression\|broken\|gate\|ruling>:<evidence>]` | record the complete kept batch; repeat `--keep` per id from repair round 4 (the user, 2026-10-28) |
| `route --id F003 --reason <spec-gap\|outside-kept> --owner <unit> --work-order "<plan path> — Phase <N> — <title>" --due "<checkpoint>" [--evidence <why>]` | leave work open in an owned follow-up; refuses in-flight, pending or accepted findings and a missing due Astra record (the user, 2026-10-28) |
| `close-route --from <run id>:<phase instance id>:<finding id> --evidence "<commit, test result, review>"` | the receiving director records closure at its checkpoint (the user, 2026-10-28) |
| `astra --id F003 --note <path> --approach "<one line>"` or `--unavailable "<reason>"` | record the approach or busy/unavailable result at the current fix-attempt count; note must exist (the user, 2026-10-28) |
| `exposure --proposal <P1\|P2> --basis "<text>" [--decision "<text>"]` | record trial exposure before the decision (the user, 2026-10-28) |
| `abandon --reason <r> [--edits-landed]` | a dispatched repair died; reopen its batch |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | record closure evidence |

`findings.py` owns batching and gating: the first round gates blockers and
minors, later rounds gate blockers, nits never gate. It rejects a partial batch,
so a round covers every gating open finding; `start-phase` resets it.
A `routed` finding remains unresolved in `status` under `routed`, outside the
batch and open counts. `history.py routed --open` shows its owner, Work Order
and due checkpoint until the receiving director records `close-route`
(the user, 2026-10-28).

**The gate never stops the run.** It answers `converged` or `dispatch` and
nothing else. Where it once stopped, it now returns that sentence in `advisory`
beside a `dispatch` verdict and the round runs. An advisory is not a gate and
never becomes one: **report it in one line** — the pattern in ordinary words,
beside the repair being dispatched — then continue. Never stop, ask permission,
re-open a decision the user has already made about this run, or edit
`~/.claude/config/direct.conf` mid-run to change what gets said.

**Dispatching a repair fixes nothing.** `dispatch` leaves its batch
`repair_in_flight`, and `gate` and `verdict` both refuse a finding still in
flight, so a repair that never finished cannot reach a reviewer pre-labelled as
fixed and be confirmed on that label alone. `implement.sh` resolves that state
itself: `landed` when its worker exits cleanly, `abandon --edits-landed` when
the worker errors. The unit director owns it only when the launcher is gone — the
user stopped it, the process was killed, the session was interrupted. Then run
`abandon --reason "<how it ended>"` before any other workflow step, and say in
one line what died and that the findings are open again. Pass `--edits-landed`
only when repair edits are actually in the tree; without it the attempt is
refunded, because a repair that never ran must not spend the budget that decides
when this phase stops.
</FindingsLedger>

<PassOwnership>
Every pass is recorded by the launcher that runs it: `implement.sh` and
`review.sh` call `start-pass` and `finish-pass` around the worker they wait on,
for completion and for error alike. The unit director never calls either by hand —
a hand-written call forges a pass that never ran, and `findings.py gate` counts
passes when it decides whether a phase is converging. The recorder rejects an
unowned call.

The one exception is a launcher the unit director killed — the <DualReview/>
preemption. Close that slot's open pass with `finish-pass --status canceled
--orphaned-launcher`, which the recorder accepts only for `canceled` and only
while a pass is open. The slot comes from `UNIT_TEAM_ROLE` on the call
and never from the record — a launcher closes its own pass and nothing else, so
the unit director standing in for one names the worker it is standing in for, once
per killed launcher. A killed fix dispatch leaves its findings
`repair_in_flight` for the same reason, so `findings.py abandon` per
<FindingsLedger/> belongs beside this call.

Phase records are the unit director's, and both belong at the real boundary:
`finish-phase` for the outgoing phase and `start-phase` for the incoming one run
before that phase's first dispatch. Recording them late attributes the new
phase's work to the finished one — its title, its elapsed clock, and its pass
counts all describe the wrong phase. A phase opened beside one still running
leaves that one open: `start-phase` parks it, and `finish-phase --phase-id <id>`
closes each at its own end. Name the phase on every launch,
`implement.sh --phase <id>` and `review.sh --phase <id>`: the pass is stamped
with it, and the launcher writes its files to that phase's `${PHASE_DIR}`.
A launch without a phase is refused (the user, 2026-10-28).
When a park makes a run useless, apply <WorkerRuns/>.

Before each launch, check the prompt's phase against its active instance and title. Run start-phase and finish-phase at the real boundaries and keep parallel phases separately identified. Do not launch against a stale or absent instance. Classify planned implementation/deployment as implementation and command-only verification as an activity. A fix launch names its defect, failed gate or ledger batch. Correct active state only through supported recorder operations, preserving old evidence; never fabricate starts/finishes or rewrite historic records. Missing historic boundaries stay uncertain in analysis. (the user, 2026-10-28)

For the 30-phase accuracy trial, launchers check before any worker starts.
`review.sh` runs `check-launch` in every mode, including UX and early reviews;
`implement.sh` runs `start-pass` first. Both commands share the launch check:
they refuse a missing or closed phase, a first identity line
`Phase <id> — <title>` that differs from the open phase, and a fix without an
authorized ledger round unless `implement.sh --fix-cause "<failed gate or defect>"`
names its cause. The newest round authorizes its workers while in flight and,
after its resolver lands, while covered findings await closure verdicts; its
companion can still launch. A prompt with no identity line passes; later phase
lines are content. Refusals record `launch_refused`; each `pass_started` holds
`launch_check`, and UX records `launch_checked` without opening a pass. Early
reviews check again at deferred `start-pass`; a refusal reports error. Stop on
the recorder message and correct state through supported operations
(the user, 2026-10-28).

For the 30-exposed-phase P2 trial, after `start-phase` and before the first
dispatch, record `findings.py exposure --proposal P2 --basis "<Work Order behavior>"`
when the Work Order changes visible layout, lifecycle or integration behavior,
or rule or acceptance criteria (the user, 2026-10-28).
</PassOwnership>

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

Launcher work is a **pass**; unit-director work -- verification, smoke, style -- is
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
Mode behavior:

| Mode | Before phase | After phase | Commit |
| --- | --- | --- | --- |
| `single` | no gate | end | none |
| `loop` | automatic | next phase | one checkpoint |
| `verbose` | <VerbosePrePhaseGate/> unless an approved auto window is active | report, then `continue` gate; a window defers both into one combined report at its close | one checkpoint |

Loop invocation authorizes its phase checkpoints. Verbose authorization occurs
only after the required briefing:

- `proceed` or `approved`: current phase only; trailing text amends its prompt.
- `auto next N phases`: positive N including the current phase.
- `auto through phase X`: current through a current-or-later todo phase X.
- `stop`: end without the described phase.
- Post-phase `continue`: show the next briefing only; it never authorizes work.
  An auto control at that gate is a normal window control and routes through
  <BriefingFreshness/> like any other.

An auto window removes intermediate stops, not explanations. Apply
<BriefingFreshness/> before its first dispatch. An auto control on initial
invocation still requires <AutoWindowBatchBriefing/> because no phase has been
briefed.

Before the first loop/verbose dispatch, stop on a dirty tree unless the selected
plan doc is the only dirty path; then include it in the first checkpoint.

Loop stops only for that dirty-tree guard, an unresolved current Pending
decision, a real design choice, reviews conflicting on intended behavior, a
required gate that cannot run, or worker/environment error. It never stops
for add-ons: <ConsiderNextItems/> accumulates them, and <ReviewPendingAddOns/>
walks them only at an interactive point — one of those stops once its decision
is resolved, a verbose gate outside a window, a user instruction that is more
than an authorization word, <RunSummary/>, or `/unit:add_ons`. The
findings ledger is not on this list and never joins it: a
convergence advisory is reported and the round runs. Everything else
auto-routes, resequences, or defers. Verbose adds only its authorization gates.
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
<ProductionUnit/> when the plan header names a production.

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
Coordination applies to phased Work Orders in every mode. Ad hoc work skips it.

Persist exactly one tagged `PhaseReservationState` for each open
phase at `${PHASE_DIR}/delegated_phase_reservation_state.json`. A script verb
makes every state change, atomically; never write or delete this file by hand.
Read it back before the next lifecycle action; the initial state must be
durable before dispatch. The verbs:

| Change | Verb |
| --- | --- |
| Initial state from `/sync board` | `phase_reservation.py enroll --session-dir "${SESSION_DIR}" --phase <id> --board <board_ready\|unconfigured>` |
| First touch to `Active` | The Codex worker hook, or `phase_reservation.py record-active` at <CheckpointCommit/> step 5 |
| Lost open phase record | `phase_reservation.py repair --session-dir "${SESSION_DIR}" --phase <id> --repo <repository>` requires a recorder-open phase, validates berth's mapped reservation on its live board and restores `Active` from the phase baseline; no live reservation exits 2 with enroll/claim commands. A finished phase exits 2 with `progress_history.py start-phase` to reopen it; a checkpoint-released phase refuses repair. |
| `Active` to pending release | `phase_reservation.py committed --session-dir "${SESSION_DIR}" --phase <id> --commit <checkpoint commit>`; a style ticket adds `--ticket <ticket>`, and the record then also names the ticket and the worktree, so the reservation stays held until that ticket's merge |
| Held reservation released at ticket merge | `style_ledger.py ticket merge` runs `phase_reservation.py release-held --session-dir <run> --phase <id> --ticket <ticket> --merged <style branch tip>`: it releases an `active` reservation from the recorded worktree at the checkpoint, confirms on the board a release whose protected tip is the checkpoint, or a later commit that descends from it and that the merged commit contains, and clears the record as `clear --released` does; a record not held for that ticket is a no-op, and a moved worktree or an unconfirmed release exits 2 and keeps it |
| Pending release confirmed | `phase_reservation.py clear --session-dir "${SESSION_DIR}" --phase <id> --released <checkpoint commit>` leaves `reservation_released` with the commit; the phase leaves edit attribution while its plan review finishes. |
| Inactive state at phase end | `progress_history.py finish-phase --status completed` |
| A finished phase reopened | `progress_history.py start-phase` on that phase id restores its enrolled state from the board answer that `enroll` kept |

Exit 2 means the verb refused the change; it names the state it found and
leaves that state as it is. The domain states and on-disk shapes are:

```json
{"kind":"repository_not_enrolled","phase":"<phase>"}
{"kind":"enrolled_awaiting_first_touch","phase":"<phase>"}
{"kind":"active","active_phase_reservation":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>"}}
{"kind":"checkpoint_committed_awaiting_release_confirmation","checkpoint_release_confirmation_pending":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>","checkpoint_commit":"<full object id>"}}
{"kind":"checkpoint_committed_awaiting_release_confirmation","checkpoint_release_confirmation_pending":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>","checkpoint_commit":"<full object id>","held_for_ticket":"<ticket>","worktree":"<unit worktree>"}}
```

| State | Created only from | Lifecycle |
| --- | --- | --- |
| `RepositoryNotEnrolled` | `/sync board` returning `unconfigured` | Owns no reservation; supplies no release argument. |
| `EnrolledAwaitingFirstTouch` | `/sync board` returning `board_ready` | The registered edit hook still owns first-touch acquisition; supplies no release argument. |
| `Active` | A clear check that acquired or already held the scopes: the Codex worker hook, or the checkpoint backstop for a first touch no hook recorded. A later clear check that names another reservation of the same phase replaces it, as after berth released the recorded one. `repair` may restore it from berth's session map only after the board confirms it is active | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
| `CheckpointCommittedAwaitingReleaseConfirmation` | <CheckpointCommit/> step 6, atomically replacing `Active` once that step's commit succeeds | Copies the active fields plus the full object id captured immediately from that commit; supplies a release argument, and resumes only at release confirmation. A style ticket's record also names the ticket and the worktree; it supplies no release argument, and only `release-held` at that ticket's merge releases it. |

These are
`PhaseReservationState::{RepositoryNotEnrolled,
EnrolledAwaitingFirstTouch, Active(ActivePhaseReservation),
CheckpointCommittedAwaitingReleaseConfirmation(
CheckpointReleaseConfirmationPending)}`. Do not represent any of them as an
absent record or a bare optional value.

`ActivePhaseReservation` is orchestration memory: never reconstruct it from a
marker, rendered prose, or conversation. Only a recorder-open phase can have a lost
record; finished or checkpoint-released phases are not candidates. Reopening a finished
phase removes its release marker with the restored enrolled state. To restore a lost record, use `repair`:
it reads berth's phase-session mapping (the director's bare session only when no
other phase is open), confirms `active` on the board, and logs and posts the repair.
Never supply reservation or run ids by hand; an existing live record stays unchanged.
`CheckpointReleaseConfirmationPending` is durable proof that the
checkpoint commit succeeded and only release confirmation remains: never create
or reconstruct it from conversation or from `git rev-parse HEAD` read at a later
time, and delete it only after <CheckpointCommit/> validates a successful
release.

An unsuccessful ending applies <RetainPhaseReservation/>. This includes
a cancelled no-diff phase, dispatch or run error, failed checkpoint commit,
every `single` run including a dirty one, user stop, drift refusal, busy ledger,
and failed release. Never invoke release from an error handler, cancellation
path, single-mode completion, run summary, or session cleanup.
</PhaseReservationContract>

<RetainPhaseReservation>
For `Active` or `CheckpointCommittedAwaitingReleaseConfirmation`, leave both the
engine reservation and `${PHASE_DIR}/delegated_phase_reservation_state.json`
intact. Report that the phase stopped before its checkpoint release could be
confirmed and name the recovery action that stopped; retain the reservation id
in durable/internal recovery output even when ordinary user-facing prose omits
tooling ids. Retention is still required when `release` may have died after
appending its checkpoint: only <CheckpointCommit/> step 7 may later confirm that
journalled success from matching outstanding evidence and delete the record.

After the coordination boundary has produced a state, a state file that is
absent, malformed, names another unfinished phase, or disagrees with a validated
claim is lifecycle loss, not non-enrollment: stop and report it. Absence before
that boundary is ordinary and must not be misreported as loss.
`RepositoryNotEnrolled` and `EnrolledAwaitingFirstTouch` require no engine
release.
</RetainPhaseReservation>

<CoordinatePhaseReservation>
Run after phase authorization and task selection, immediately before
<LaunchImplementation/>. It is the only pre-dispatch coordination boundary.
Do not dispatch until this contract reaches one of its four persisted states.

0. On resume, read this phase's state file first. A valid state for this phase
   is authoritative:

   | State | Resume action |
   | --- | --- |
   | `Active`, this phase | Resume without another claim. |
   | Either inactive state, this phase | Resume without an engine mutation; when enrolled, acquisition remains the registered edit hook's. |
   | `CheckpointCommittedAwaitingReleaseConfirmation`, this phase | The checkpoint already committed and only its release confirmation remains: resume at <CheckpointCommit/> step 7 without re-committing, re-claiming, or dispatching. |
   | Either reservation-bearing state, another unfinished phase | A stranded reservation; stop the new dispatch. |

   A completed phase removes its inactive state under <RecordPhaseCompletion/>,
   so no old inactive tag is silently reused for a new phase.
1. When no state exists yet, create `${PHASE_DIR}` and write its `files` list
   from the Work Order's Files field: one repository-relative path per line, a
   trailing `/` covering the tree below it. A phase alone needs no list; with
   two open, it is how a worker's edit finds its phase, so keep each path in
   exactly one phase's list and add a path a worker's refusal names. Then invoke
   the shared `/sync board` entry point once, under <BerthDecisions/>:

   ```sh
   set -o pipefail; cargo-berth board --json | jq -r .status
   ```

   Only the status is read, so the board's long JSON is never saved as tool
   output, and nothing needs deleting. Empty output or a non-zero exit is
   `ledger_unreadable`.

   | Result | Action |
   | --- | --- |
   | `unconfigured` | Persist `RepositoryNotEnrolled` with `phase_reservation.py enroll --board unconfigured` and proceed silently. |
   | `board_ready` | Persist `EnrolledAwaitingFirstTouch` with `phase_reservation.py enroll --board board_ready` and proceed without claiming predicted paths. |
   | `ledger_unreadable` | Stop the run; it is never opt-out. |
   | `busy` | Stop with “the ledger is busy, try again” and the exact board command to rerun; do not retry. |
2. The registered PreToolUse edit hooks own the next transition. A clear check
   atomically acquires exact `file:` scopes before the edit. A Codex worker's
   `apply_patch` runs `scripts/hooks/pre-tool-use-berth.py`, which gives each
   path to its open phase (the only one, else the one whose `files` list covers
   it, refusing a path no single list owns), checks as that phase's own berth
   session, `<this session's id>.phase-<phase>`, and replaces that phase's
   `EnrolledAwaitingFirstTouch` with `Active` from the returned facts. A path a
   sibling phase already holds is allowed and records nothing: the engine
   answers `left_unmapped` with the sibling's reservation. A Claude edit's hook
   protects the paths but writes no state, so a first touch of your own leaves
   the state for the checkpoint backstop (<CheckpointCommit/> step 5). That
   edit checks under your bare session id, and cargo-berth maps its reservation
   there, so the phase's own check later answers `left_unmapped` with it. With
   one open phase, the hook and the backstop record that reservation as the
   phase's `Active`. With two
   phases holding reservations, your own edit is refused as ambiguous until you
   name its phase: `cargo-berth check <path> --reservation <that phase's
   reservation id> --json` acquires the path for it, and the edit then clears. A blocked check refuses the edit and
   leaves the pending state intact; a worker reports the refusal to you, and you
   answer the overlap under <BerthDecisions/>. An unreadable ledger stops
   without facts. Beyond those answers, do not call `cargo-berth claim` on
   behalf of a Work Order.
</CoordinatePhaseReservation>

<BerthDecisions>
Overlap answers, incursion resolves, orphan retirement and releases never reach
the user. Under a production, send the showrunner the overlap — holder ids,
paths, answer choices — and record the answer it picks; that answer is a
decision, not a relayed user approval. With no showrunner, pick the answer
yourself. Either way, report what was decided in one line.

An answer is `cargo-berth claim <paths> --reservation <id> --<choice> <holder>
--overlap-why "<why>"`, `<choice>` one of `before`, `after`, `defer`,
`override`, and `<id>` the asking phase's recorded reservation; it records in
one step and exits 0. `before` and `after` widen only that phase's reservation,
with the ordering edge; `defer` records the answer and takes no paths, so the
phase holds its dispatch on them. An id the ledger does not know is refused.
A claim behind a holder you already sequenced against reuses that edge and
adds the paths; only `cargo-berth sequence` still refuses a duplicate edge
(exit 2). When several holders
conflict, name every one in the same claim, one flag each: `--before` and
`--after` repeat once per holder and mix, as in `cargo-berth claim <paths>
--reservation <id> --before <A> --after <B> --overlap-why "<why>"`. `--defer`
repeats once per holder but excludes `--before`, `--after` and `--override`:
a defer takes no paths, so it cannot share a claim that widens. To defer to
one holder and order behind another, claim with `--defer` first, then claim
the paths with `--before`/`--after` when the deferred holder ends.

Every cargo-berth call is one plain command run in `${WORKING_DIR}`: no pipe,
loop, `;`, `&&`, `$(...)`, redirect or env-var prefix. cargo-berth reads
`CLAUDE_CODE_SESSION_ID` itself. Read the exit status and output from the tool
result.
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
Launch the blind reviewer while the writer is still running, so both finish
together instead of back to back. Evaluate only at a Codex poll timeout or a
requested <ProgressReport/>, and arm only when all of these hold: an implementation or fix dispatch is
active; `EARLY_REVIEW=none`; the completed dispatch would receive a worker
review — <DualReview/> pass 1 or a closure review; **pass-internal** completion
of that dispatch is at least 75%; and at least ten minutes of writer time still
remain.

Read completion from that report's or poll's evidence — the heartbeat shows the worker
running its listed verification commands, or the diff already covers essentially
all Work Order (or fix batch) files — and the remainder from how long the pass
has already run against that estimate. Ten minutes left at 75% means a pass of
roughly forty minutes or longer, so most implementations and nearly every repair
arm nothing and review synchronously; that is the intended outcome, not a missed
opportunity. A behavior-preserving repair never arms — documentation,
formatting, lint guidance, an agreed trivial rename — and neither does one whose
batch sits in paths narrow enough that <FixDispatch/> will close it on a
contained diff; that judgment is the unit director's own reading of the batch, as
no task name carries it. When the two estimates disagree, or either rests on no
evidence, do nothing; the synchronous path still exists.

At an eligible point, in that same turn:

1. Increment `${REVIEW_PASS}` now; <DualReview/> will not increment it again.
2. **Delete every stale delivery artifact and prove they are gone.**
   A resumed or repeated phase reuses `${PHASE_DIR}`, so an earlier delivery's
   artifacts can already sit on disk under the exact names this launch will
   poll.

   `rm -f "${PHASE_DIR}"/final_diff_*.diff "${PHASE_DIR}"/final_diff_*.ready`

   Clear the whole glob, not just the current index — later passes in this phase
   collide the same way — then confirm no sentinel remains before continuing. A
   stale sentinel releases the reviewer onto a previous phase's diff, which
   returns a confident and entirely false blocker saying the phase implemented
   nothing; and because the sentinel is what releases `assign.sh` to call
   `start-pass`, it opens the review pass while the writer is still running, so
   the recorder closes the live implementation pass as `interrupted` and refuses
   every later `progress` call in that phase. Both failures are silent until the
   false verdict or the refused call.
3. Apply <ReviewDiffContract/> to the current partial tree. The worker has
   named no created files yet, so that check is vacuous; the snapshot is
   expected to be incomplete.
4. Use the unit's own reviewer under <TeamReview/>. Save its slot as
   `${EARLY_REVIEW_SLOT}` from the dispatch result.
   Write the applicable early-form prompt — the `adversary`
   lens of
   <BroadReviewPrompt/> for pass 1, <ClosureReview/> for a fix — including the
   completion estimate, the partial diff, and the exact final-diff and
   ready-sentinel paths below. **Only the adversary arms early.** It is the lens
   that gains most from the extra time, and arming both against a partial
   tree would double the exposure to the void verdict below; `contract`
   launches at completion under <DualReview/> step 3.
5. Launch `assign.sh --role review --lens adversary --pass "${REVIEW_PASS}"
   --phase "<phase id>" --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}"
   --prompt <early prompt> --early-ready "${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready"`
   under <DispatchContract/>. Save the handle as `${REVIEW_DISPATCH_HANDLE}`
   and set `EARLY_REVIEW=launched`. Leave `${DISPATCH_HANDLE}` untouched.
6. Before the recorder call in <ProgressReport/> step 5, put the reviewer in
   the round table:

   `python3 ~/.claude/scripts/direct/progress_history.py arm-review --session-dir "${SESSION_DIR}" --phase-id "<phase id>" --activity "<what this reviewer is checking>" --called-task direct.review [--lens adversary]`

   Pass the same lens and phase the launch carries, and no lens where the
   launch carries none: the marker names the status and pid files it will watch for, and a
   marker watching the unsuffixed pair while the launcher writes the suffixed
   one retires the row on its first tick, reporting a live reviewer as gone.
   It is a presentation marker, not a pass event, so it cannot forge anything
   convergence counts. It resolves the same reviewer `assign.sh` will, retires
   its row when `assign.sh` errors or its process is gone, and is superseded
   when the sentinel lets `assign.sh` open the real pass. The two running rows
   are the announcement, and they carry what a sentence cannot: which agent each
   one is, when it started, and how long it has been going.

The extra argument defers `assign.sh`'s `start-pass` until the sentinel appears,
so the implementation pass and the review pass never overlap in the recorder. At
most one early launch per dispatch; otherwise review runs synchronously, as it
always may — early launch is opportunistic, never required.

| Event | Required action |
| --- | --- |
| Primary completes | After <LaunchImplementation/> step 7, write the final diff to `${PHASE_DIR}/final_diff_${REVIEW_PASS}.diff` — a closure review stays limited to its paths per <ClosureReview/> — then create `${PHASE_DIR}/final_diff_${REVIEW_PASS}.ready`. **Never create the sentinel before the diff is fully written.** `assign.sh`'s run then sends the reviewer its Stage 2. |
| Primary errors, or the run stops | Kill the early reviewer. If the sentinel exists, close its pass with `UNIT_TEAM_ROLE=${EARLY_REVIEW_SLOT} … finish-pass --status canceled --orphaned-launcher` per <PassOwnership/>; skip this call if the launcher opened no pass. Before the sentinel no pass was recorded, so record nothing — a pre-sentinel kill counts toward no advisory, including the blind-review cancellation one. |
| Reviewer errors before delivery | Report it in one line, clear `${REVIEW_DISPATCH_HANDLE}` and `${EARLY_REVIEW_SLOT}`, set `EARLY_REVIEW=none`, and leave the numbered artifacts. The primary continues and is reviewed synchronously at completion under the next `${REVIEW_PASS}` index. |
| A verdict arrives before delivery | **It is void.** The reviewer cannot have read a diff that does not exist yet, so discard its findings entirely rather than reading them as evidence: open nothing in the ledger, preempt nothing, route no blocker into a fix dispatch. Say in one line that it is discarded and why, then follow the reviewer-error row. A void verdict is often fluent and specific — a stale diff supports confident claims about missing work — so the check is the timing, never how convincing the text reads. |

Every path that ends an early launch before its real pass starts also drops the
row it was given:

`python3 ~/.claude/scripts/direct/progress_history.py disarm-review --session-dir "${SESSION_DIR}" --reason "<canceled|reviewer error|void verdict>"`

Run it alongside clearing `${REVIEW_DISPATCH_HANDLE}`, `${EARLY_REVIEW_SLOT}`,
and `EARLY_REVIEW`. The
row would retire itself at the next report anyway; this is how the reason
reaches the record, and it is what lets that report state the reason rather
than infer one.
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

<FixDispatch>
Apply <LongLivedWorkers/>.
When a repair round fails or its next edit replaces a run's tree, apply <WorkerRuns/>.

Find a failing test's cause before you scope its repair: read the failure text
and the fixture it runs in. A repair aimed at a cause no one has checked costs
a full round.

For a `dispatch` batch, set `${FIX_ROUND}` from the gate's `round` and create
`${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md` under
<WritePromptContract/>. Work Specification contains every batch id with concrete
file/line findings and intended behavior. Verification contains only implicated
`verify.sh` lines—usually check and test, plus lint after the worker's last
edit when the phase has a lint run, per <PhaseChecks/>.

Put known fixture/setup updates, lint, tests and merge follow-through in the original repair prompt. After handoff, inspect the remaining action. If it only runs unchanged commands and no edit or judgment is pending, the director runs the existing detached verification workflow; do not dispatch a repair worker. Preserve the commands, output, tree identity and review result. A failing command reopens or retains its finding and goes to the owning worker if an edit is needed. Fixture expectations, source edits and conflict choices remain owner work; a green command does not replace closure review. (the user, 2026-10-28)

At each repair handoff whose Verification list holds lint or test commands,
record `findings.py exposure --proposal P1 --basis "<handoff and commands>"`
before choosing the runner. Give `--decision "director ran the commands"` or
`--decision "worker: <the edit or judgment still pending>"` (the user, 2026-10-28).

A repair runs **one worker per file set no other worker touches**: when the failing
tests or findings split that way, always run as many workers as you can reasonably
manage; otherwise one. User, 2026-10-04. Use the open `impl` and `test` slots
first, preferring the worker that owns the files and reassigning another open worker
under <RoleReassignment/> when needed. Only then open `fix2`, `fix3`… when no
open worker can take another file set. Each worker's task and kind are `fix`, its
ninth argument remains its slot, and it gets its own
`fix_prompt_${FIX_ROUND}[_<slot>].md`. Each worker makes its repair and writes, for each testable finding, the regression test that would
have caught it — one that fails without the repair. A finding about what is
drawn gets a test that renders and reads the pixels back, or a live pixel check
the prompt names; a test of components, constants or hand-written pointer hits
does not count. It names each test for the behavior it pins and puts the
finding id only in its summary; an id in code outlives the review that defined
it. <ClosureReview/> is the cold read, so no worker is spent
on one here. A worker's file set is its findings' files plus their test targets;
its prompt names every other worker's files as read only.

Before editing, list accepted behaviors and checks reached by the repair's planned paths, using earlier verdicts, real callers and the current suite. Carry their commands/live views into Verification beside the new regression check. Preserve assertions unless a cited approved goal change or evidence shows an expectation is wrong. Run the new and affected accepted checks on the same tree. Compare a suspected old defect against the prior accepted tree. Reopen lost accepted behavior with failing evidence. Keep cold closure review for public contracts, registration and cross-batch invariants; route unrelated old defects under item 7. (the user, 2026-10-28)

For the 30-phase P5 trial on all repairs, carry those checks in
`dispatch --accepted-checks "<accepted checks the edits can break>"`; use
`--accepted-checks "none: <why no accepted behavior is reached>"` only with that
evidence. Dispatch refuses an empty value; its event records the list. A value
without `none:` records P5 exposure (the user, 2026-10-28).

Dispatch every open worker with `implement.sh --to <full-worker-name>` before the
usual positional arguments; the prompt file is the follow-up message. Omit
`--to` only for a new worker allowed by <LongLivedWorkers/>. Messages without the
launcher are for questions only.

Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. From repair round 4 onward, do not dispatch a missing target, rule interpretation or acceptance case in this phase unless evidence establishes a phase-caused regression, broken working behavior or test/gate failure. A broad original permission to fix pilot defects does not override this check. A later explicit user or showrunner ruling may keep named work; cite it. Route every other missing criterion to a follow-up Work Order in the owning unit, with its target decision, implementation, tests and acceptance evidence. Do not dispatch a batch until every finding is either retained with evidence or assigned to that follow-up. Split mixed batches; keep their eligible defects here. Never mark an unmet pilot acceptance bar passed because its work was routed.

Work Orders state the acceptance observation, unit or comparison, and named real-path test or live check for changed behavior. Reuse established targets. Resolve choices through the showrunner before dependent work; do not invent a number to fill the form. A later changed goal updates the order explicitly. (the user, 2026-10-28)

Count repair rounds as `history.py rounds` numbers its repair intervals, not as ledger batches. Before any round 4-or-later dispatch on a repeated finding, require evidence that it is a phase-caused regression, broken working behavior or test/gate failure. Earlier attempts and an open verdict alone are not that evidence. Broad original permission for pilot fixes does not override landing. Cite any later explicit user/showrunner ruling that keeps the named work. Route a repeated finding outside these categories to its owning unit's follow-up Work Order before another repair starts. Keep the finding open and link its checks, previous attempts, due checkpoint and final acceptance result. Split mixed batches and retain all eligible work. If eligibility is unknown, diagnose it rather than silently route or count it as a certain saving.

For retained work after two completed failed fixes, record the failure, approaches tried, cause diagnosis and next distinguishing test before another approach. Interrupted workers and account moves remain continuations, not completed rejected patches. (the user, 2026-10-28)

At each fix limit, try something different. When a phase reaches repair round 4 with a finding still open, or a kept finding has failed two fixes, the director writes a short note: the finding, the tries so far and why each failed. It runs `/astra_review` on the note. Astra's approach goes into the next try, or into the follow-up Work Order when the finding moves out (the user, 2026-10-28).

Before forming the batch, use the gate's `repair_round`, which counts
`history.py rounds` repair intervals, for the round-4 checks; keep `round` for
`${FIX_ROUND}` and ledger dispatches. P2 is a 30-exposed-phase trial from
round 4; P3 is a 30-phase trial. For every `astra_review_due` id, write the note
and run `/astra_review --run-directory "${SESSION_DIR}" --phase <id> <note>`.
Record `findings.py astra --id <id> --note <note> --approach "<one line>"`,
or `--unavailable "<reason>"` when the review reports busy or unavailable.
Put its approach into the next prompt or receiving Work Order. A new failed
fix makes the review due again (the user, 2026-10-28).

Route each outgoing id with `findings.py route`, naming its owner, receiving
Work Order and due checkpoint, before dispatch. Routing is allowed in any
round, including item 7's landing rule. From repair round 4, give one
`--keep F003=<regression|broken|gate|ruling>:<evidence>` per covered id.
The tools refuse a missing due Astra record, kept evidence or accepted checks.
Every gating open id still belongs in `--covers`; a routed id leaves that
batch but stays unresolved until its receiving checkpoint records `close-route`
(the user, 2026-10-28).

Apply <PassOwnership/>'s phase and cause check before each launch
(the user, 2026-10-28). Run
`findings.py dispatch --covers <all kept batch ids> --accepted-checks "<checks or none: reason>" [--keep <id>=<category>:<evidence> ...]`
before launching, then:

```sh
UNIT_RESOLVES_ROUND=1 implement.sh --to "<full-worker-name>" --phase "<phase id>" \
  "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" fix \
  "<responsibility>" fix "<activity>" "${FIX_ROUND}" <slot>
```

**`UNIT_RESOLVES_ROUND=1` is what lets the launcher resolve the round**:
only the launcher watches the worker exit, so only it can say a repair landed.
Each further worker runs the same call with `UNIT_RESOLVES_ROUND=0`, its
own prompt and its slot; the round is done only once every worker has exited.

Apply <DispatchContract/>; set `EARLY_REVIEW=none` and clear
`${EARLY_REVIEW_SLOT}` at dispatch, and close the
turn with the progress header per <DelegationResultFormat/>. While a fix runs
that will receive a worker closure review, <EarlyReviewArm/> may arm that
reviewer early.

**A repair the main read can close, closes without a worker review.** Apply
<ReviewDiffContract/> and read every repair hunk yourself. Close it directly —
check each testable id has its regression test, record each verdict, and
continue to <Synthesize/> — when the diff answers every id and each hunk is one
of:

- a path the batch's own findings named, a new file one of those paths
  creates, or a test file holding the batch's regression tests;
- a doc comment, user-facing copy, or comment change in any path, with no
  code change beside it;
- a new private or crate-private type, function, or accessor whose every
  caller is inside the repair diff.

**A repair to what is drawn is proved live before it closes.** Before closing
the round or dispatching its closure review, launch the build and probe each
repaired visible finding in the running app (BRP for a Bevy app): hover it,
crop the shot, read the drawn pixels or layout. In Hana, shoot with
`/hana_shot` (a stored view or `--target`), every view in one call, never a
camera worked out by hand. A visible finding that fails a second time gets a live
diagnosis of its cause before another repair turn, and the next prompt carries
the measured cause.

Dispatch the normal <DualReview/> closure review only when the diff cannot
answer the question: a public API or signature change with a caller outside
the diff, a registration, transition, or invariant reaching past the batch, an
id whose verdict reads unclear, a claim the worker says it could not verify and
the main read cannot verify either, or a new defect the repair introduced.
Uncertainty routes to the reviewer; a hunk fully read and fully understood
does not. A closure review costs a stage of the phase, so it is spent on a
question, never on a path. Use the unit's own reviewer under <TeamReview/> and dispatch with
`assign.sh --role review --prompt <closure prompt> --cwd <worktree>`.

On completion, `implemented` continues as above; `error` applies
<WorkerLaunchFailure/>, and then, if the error survives it,
<RetainPhaseReservation/>, reports the fix log, records an error
outcome, clears the session marker, and stops. Both outcomes resolve the round
in the ledger through the launcher. Any third outcome — a new-worker or follow-up dispatch stopped,
killed, or gone without `impl_status` reaching either — is the unit director's to
resolve with `findings.py abandon` per <FindingsLedger/>, then apply
<RetainPhaseReservation/> before reviewing, re-dispatching, or
reporting anything about the round.
</FixDispatch>

<Synthesize>
1. Merge every lens file's findings — `adversary`, `contract` — and
   any <UXReview/> rows with the main review's, dedupe real issues, tag the
   lens or reader that caught each — `--lens` on `open` — and discard refuted
   findings with a concrete explanation. Several readers landing on one hunk is
   one finding with several witnesses, not several findings; readers
   disagreeing about that hunk is a reviewer disagreement, which
   <DelegationResultFormat/> reports rather than resolves by majority.
2. Present <DelegationResultFormat/>. If the user is confused, apply
   <ExplainOnDemand/> before any choice.
3. If every remaining issue is trivial — doc-only, an agreed rename, or a
   small mechanical change whose one correct edit is evident from the finding
   itself, with no design judgment and no reviewer disputing the reading —
   the unit director applies them all directly,
   runs the implicated `verify.sh` lines, reports the edits and why they
   qualified, and records `verdict --state accepted` for each fixed issue
   with a ledger id, citing the edit and verification evidence. Then continue.
   Otherwise the whole batch follows steps 4–5.
4. Apply <DecisionRouting/> before opening findings.
5. Open every confirmed remaining issue that has no ledger id yet, then obey
   `findings.py gate`. Use its `repair_round` for the round-4 routing and kept
   evidence checks; use `round` for the ledger batch. For each `astra_review_due`
   id, run and record the Astra step in <FixDispatch/> before dispatch or routing.
   The gate records P3 exposure when a round-4-or-later decision has a gating
   open finding with a previous repair (the user, 2026-10-28):
   - `converged`: retain nits for retrospective; continue to smoke, then
     <UXReview/>.
   - `dispatch`: apply <FixDispatch/> to the complete batch, then return here.
     When the payload carries an `advisory`, say it in one line and dispatch
     anyway per <FindingsLedger/>.
6. Stop only when the plan leaves a real design choice or reviews conflict on
   intended behavior:

```
Your choice:
1. One more worker fix pass — [work and cost; recommendation with reason].
2. Stop here — preserve remaining items as written todos.
3. Talk through an item first.
```

Choice 1 applies <FixDispatch/>; choice 2 continues to smoke, then <UXReview/>;
choice 3 preserves the gate. With no gating issues, continue to
<RunApplicationSmokeTest/>, then <UXReview/>.
</Synthesize>

<RunApplicationSmokeTest>
Read the diff. If no repository binary reaches its changes, record
`not applicable — <reason>` and continue to <UXReview/>. Otherwise select the
target from Delegation Context Run/Smoke, Acceptance gate, repository
instructions, then manifest. A build, test binary, static example build, or
worker report is not a smoke test.

Launch the real product from `${WORKING_DIR}` with useful logging/backtraces,
exercise the changed runtime behavior, observe stability, close cleanly, and
record command/action/result. Never run a `check` or `test` pass first to prove
it builds. Startup alone suffices only when no changed
behavior can be invoked. A pass continues to <UXReview/>.

A launch that needs a built binary — a smoke or shot helper's, or a launcher
that takes a path — builds it with this one line, then starts the
`binary: <path>` it prints:

`bash ~/.claude/scripts/direct/verify.sh --session-dir "${SESSION_DIR}" build <package> [--bin <name> | --example <name>]`

It waits for a build hold, takes the cargo token and memory admission, and
records the build, and its allow rule needs no approval. You and every helper
never run raw cargo (`cargo build`, `cargo run`): a helper's raw `cargo build`
needed the user's approval by hand (2026-10-23). Copy the line into every smoke
or shot helper prompt.

A panic, fatal log, unexpected exit, or wrong behavior is a blocker: route it
through <Synthesize/>, then repeat review, synthesis, and smoke before later
gates. If this environment cannot perform the interaction or locate an
applicable executable, close the process, record `deferred — <exact human action
and limitation>`, and continue to <UXReview/> without waiting. Deferred smoke
allows the checkpoint but is batched at <FinalGate/> and reported by
<RunSummary/>.
</RunApplicationSmokeTest>

<UserBuildGuard>
Before a build goes to the user's machine (an install, a binary path given to
the user, a deploy), run in that repository:

`python3 ~/.claude/scripts/direct/assertion_guard.py --base main`

Exit 1 (a removed, weaker or turned-off assertion or test with no
`Assertion-Change:` ruling) or exit 2 (it could not run) stops the hand-off:
route the finding through <FixDispatch/>, or record its ruling, then run it
again. The user got a build that does not work from cargo-liner-tile
checkpoint e21d5b93, where a worker weakened an `assert_eq!` (user,
2026-10-28).
</UserBuildGuard>

<UXReview>
Runs when the phase changes what users see, in the app or in any example; a
phase that changes nothing on screen skips it and says so in one line. Guide:
the UX guide the production doc names, else the one the repository's
instructions name; with none, skip and say so in one line.

1. After smoke has a build, take a shot of each changed view and state on the
   run's port at normal size — the same shots the checkpoint notice sends
   (<ProductionUnit/> item 3). Never view them yourself. In Hana, shoot with
   `/hana_shot`: a stored view (`crates/hana/brp_views.toml`) or an ad hoc
   `--target`, every view in one call, never a camera worked out by hand. A changed view the file
   lacks gets `views add`; when the scene changed, run `views check` and commit
   the file with the phase.
2. Open an activity; the
   label is exact, because `review-trial` counts it:
   `progress_history.py start-activity --session-dir "${SESSION_DIR}" --label "UX review" --activity "<what the shots show>"`
3. Run one design check, so the shots never enter your context. Use this
   prompt for the Claude helper:

   > Read `~/.claude/commands/ux_eval.md` and follow it for these shots:
   > <paths>. Guide: <path>. Scale: <shot pixels per logical pixel>. Context:
   > <unit> phase <N> — <what changed, every state the shots must show, and
   > each shot's window size in logical pixels>. Also judge by the three gods
   > (`<ThreeGods/>` in `~/.claude/docs/decision_criteria.md`) and these
   > production rules: <the production doc's rules that concern looks>. You
   > only review: edit nothing. Return only the verdict and the table.

   Spawn a fresh Claude Agent helper in the background with that prompt.
   Read its verdict once and stop it with TaskStop. This helper is the one
   design check.

   Save each canonical
   candidate it lists, without viewing it, into a slot still empty:
   `mkdir -p <guide>/examples/<stem> && magick <shot> -crop <crop> +repage <guide>/examples/<stem>/<slot>.png`.
   Saving it approves it for use now. Add one line to
   `<guide>/examples/pending.md`, which holds it for the user's final approval:
   `- <stem>/<slot>.png — <why it is clear> — <unit>, <date>`. Close the activity with
   `finish-activity --session-dir "${SESSION_DIR}" --result <pass|N defects>`.
4. Open each defect row as a finding — blocker, because the showrunner's merge
   check holds a checkpoint on any visible defect. A `no rule` row is still a
   finding:
   `findings.py open --severity blocker --lens ux --caught-by worker --title <defect> --file <shot path> --detail "<rule>: <fix>"`
   Then obey `findings.py gate` exactly as <Synthesize/> step 5 does, the same
   repair round under <FixDispatch/>, except that `converged` continues to
   <PhaseEnd/> in loop or verbose, or <RunPhaseReview/> in `single`.
5. Re-entered after a `ux` repair that changed the screen, this step replaces
   1–4: re-shoot and re-judge only the rows raised, under its own `UX review`
   activity, given just those rows and their shots. Run step 3's design-check
   procedure with the fresh Claude helper;
   then close the activity and record
   each with `findings.py verdict`. A `still_open` row returns to step 4's
   gate. Then continue to <PhaseEnd/> in loop or verbose, or
   <RunPhaseReview/> in `single`.
6. **The notice's verdict.** In a production unit, the checkpoint notice's
   `design check:` line (<ProductionUnit/> item 3) uses step 3's selected
   verdict on exactly the shots the notice sends, from a build of the
   checkpoint's code. Step 5's partial re-judge does not count, nor does a
   verdict from before any later change to what users see (a repair, a merge
   of the merge branch). In those cases, before the notice, re-shoot the whole
   set and judge it once more as in steps 1–3. Send on a `pass`, or with each
   defect moved to a follow-up phase (item 7's landing rule); a defect left in
   the phase goes back to step 4. The showrunner merges on that line and runs
   no check of its own (user rule 2026-10-01).
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
In `single`, run both halves under <ExecutionSteps/> step 9.
At <PhaseEnd/>'s worker cleanup point, run
`python3 ~/.claude/scripts/direct/remove_workers.py --session-dir "${SESSION_DIR}"`.
It removes the completed phase's Claude workers and any worker a dead run left
alive; a failure is one line in the report, never a stop. Under
<LongLivedWorkers/>, do not end this run's Codex workers: their turns finish, their
threads stay open, and `end_session.sh` ends them with the run. TaskStop each
Claude Agent helper, end finished launchers, and shut down every Hana or
example app this phase launched. Never touch what another session started.
Mid-phase, stop each helper once its result is read and each app once no step
uses it.

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
~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <unit-worktree>/<Plan cell> --repo <unit-worktree>
```

Read the Plan cell from the unit's Units row, relative to its worktree; a rename
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

<FinalGate>
Loop/verbose only after plan exhaustion:

1. Launch `verify.sh final` once under <BackgroundVerificationContract/>. It
   owns the workspace all-targets check and full tests.
2. On failure, create a synthetic phase `final` / `Final verification` once:
   capture a new baseline, run `start-phase`, reset review and smoke state, open
   the concrete failures in the ledger, then use its gate plus <FixDispatch/>.
   Later repairs do not repeat those resets. After closure convergence, run
   applicable smoke and return here; do not run phase review or a phase
   checkpoint for the synthetic phase. Rerun this gate after each repair.
3. Batch all deferred smoke actions after the gate is green. Ask the user once;
   route discovered defects through the synthetic fix path. If declined, carry
   them as outstanding rather than blocking run completion.
4. Finish the synthetic phase when applicable, run <FinalGateCommit/>, and
   record the final result.

Single mode and early endings skip this gate and state why in <RunSummary/>.
</FinalGate>

<FinalGateCommit>
Read `~/.claude/docs/direct/final_gate_commit.md` in full once <FinalGate/> is green.
It also defines <RunAsBuilt/> and <AsBuiltCommit/>, which follow it.
</FinalGateCommit>

<RunAsBuilt>
Defined in `~/.claude/docs/direct/final_gate_commit.md`; read it in full at a complete
run's end, including a finished `single` task, then apply <AsBuiltCommit/>.
</RunAsBuilt>

<RunSummary>
Emit on every multi-phase ending:

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Final gate:** [result or skipped reason]
**As-built:** [docs created or updated, and the commit that carried them; in
`single`, that they are uncommitted; or the reason the run never ran it]
**Style review:** [each phase's reviewed range and result, including Off or a skipped reason]
**Smoke checks still unperformed:** [phase + exact action, or none]
**Deferred decisions still open:** [phase + decision, or none]
**Add-ons awaiting review:** [count, or none]
**CI points:** [each point from `ci_points.log` with its result, the CI branch
merged or kept, or not applicable — fewer than five phases or `single`]
**Reservation disposition:** [checkpointed and outstanding, retained with the
reason this run stopped, or coordination not active]
**Why the run stopped:** [complete, user stop, pending decision, or error]
```

Apply <UserFacingText/> and <RetainPhaseReservation/> for every ending
that did not complete <CheckpointCommit/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Finally run `bash ~/.claude/scripts/direct/end_session.sh` on
every exit so the Stop hook cannot revive a finished run. Then run
<ReviewPendingAddOns/>; the summary and its commits never wait on it.
</RunSummary>
