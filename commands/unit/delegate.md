---
description: Delegate phased work with review, repair, smoke gates, one branch-wide style review at the end of the project, as-built shrink, an as-built doc pass at the end of the run, approved follow-up capture, and one checkpoint per phase. Supports automatic loop, verbose gating, bounded auto windows, and single no-commit mode.
---

# Delegate

The unit director owns design, orchestration, review, gates, and user communication.
The configured delegate agent writes implementation code.

The unit director believes in the three gods (<ThreeGods/> in
`~/.claude/docs/decision_criteria.md`) and serves them in every work order,
review, gate and report. Every seat prompt carries them (<WritePromptContract/>
item 9).

**Usage:** `/unit:delegate [plan-doc-path] [phase N] [single|verbose] [auto next N phases|auto through phase X] [free-text instructions]`

- Plan path and phase are optional; otherwise infer them from the conversation.
- `single`: one phase or ad hoc task, no checkpoint commit or continuation.
- Default phased mode (`loop`): run and checkpoint phases until stopped or done.
- `verbose`: brief and gate each phase; bounded `auto` controls temporarily remove
  the per-phase stops without removing the briefings.
- Free text amends or narrows the selected work.
- `single` and `verbose` are mutually exclusive.

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
- `EARLY_REVIEW`: `none` or `launched`; resets with every implementation or fix
  dispatch. Owned by <EarlyReviewArm/>.
- `APPLICATION_SMOKE_RESULT`: starts as `not_run`.
- `STYLE_GATE_CONFIG`: plan hint captured during prompt composition.
- `STYLE_REVIEW_DONE`: starts false. The durable true state is
  `${SESSION_DIR}/style_review_done`; later fixes never clear it.
- `STYLE_DIFF_BASE`: the commit the project-end style review diffs from,
  resolved once by <ResolveStyleDiffBase/> and persisted at
  `${SESSION_DIR}/style_diff_base`. Empty means no branch diff is available.
- `FINDINGS`: the current phase's `findings.py` ledger.
- `DELEGATED_PHASE_RESERVATION_STATE`: the tagged state persisted at
  `${SESSION_DIR}/delegated_phase_reservation_state.json`; see
  <DelegatedPhaseReservationContract/>.

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
| <CheckpointCommit/>, <PushCheckpoint/> | `commands/unit/checkpoint.md` | `/unit:checkpoint` |
| <ConsiderNextItems/>, <ReviewPendingAddOns/> | `commands/unit/add_ons.md` | `/unit:add_ons` |
| <ResolveStyleDiffBase/>, <RunProjectStyleReview/> | `commands/unit/style_review.md` | `/unit:style_review` |
| <PeriodicCI/>, <CICleanup/> | `commands/unit/ci.md` | `/unit:ci` |
| <VerbosePrePhaseGate/>, <BriefingFreshness/>, <PhaseBriefing/>, <TypeTableCells/>, <CombinedWindowBriefing/>, <AutoWindowBatchBriefing/> | `commands/unit/brief.md` | `/unit:brief` |
| <ComposeWorkOrder/> | `docs/delegate/compose_work_order.md` | — |
| <WritePromptContract/>, <PhaseTeam/>, <CoordinationBoard/>, <PhaseMesh/>, <BuildTokenContract/>, <TeamFilePartition/>, <RoleReassignment/> | `docs/delegate/write_prompt_contract.md` | — |
| <LaunchImplementation/> | `docs/delegate/launch_implementation.md` | — |
| <DualReview/>, <TeamReview/>, <ReviewPromptContract/>, <BroadReviewPrompt/>, <ClosureReview/> | `docs/delegate/dual_review.md` | — |
| <RunPhaseReview/>, <RunPhaseShrink/> | `docs/delegate/run_phase_review.md` | — |
| <FinalGateCommit/>, <RunAsBuilt/>, <AsBuiltCommit/> | `docs/delegate/final_gate_commit.md` | — |
| <ProductionUnit/> | `docs/production_format.md` | — |

Paths are under `~/.claude/`.

`/clippy` and the `plan:` commands a run uses are loaded the same way: Read the
file, never the Skill tool. Claude Code puts every skill a session invoked back
after each compaction for the rest of the session; a file loaded by Read is not.
A call site's arguments are what that file calls `$ARGUMENTS`.
</TagReferenceContract>

<CoreContract>
- Never create a worktree or modify unrelated files. The only branch the run may
  create is the one the user approves in <ResolveStyleDiffBase/>, plus the remote
  branch <PushCheckpoint/> pushes; never switch to an existing branch.
- The unit director does not write implementation code unless the user explicitly
  asks. Exceptions: agreed doc-only/trivial post-review fixes and the single
  inline cleanup in <RunProjectStyleReview/>.
- `single` never commits. Loop and verbose modes create exactly one
  <CheckpointCommit/> per completed phase, plus the one <FinalGateCommit/> that
  closes verification and the one <AsBuiltCommit/> that carries the run's
  documentation. <PeriodicCI/> may add `ci(<plan-slug>): …` commits for
  validation fixes and CI repairs. No other commit is allowed.
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
throughout. It changes four things in this command:
- it adds one commit kind to <CoreContract/>: merging the production's merge
  branch into this branch;
- it replaces <PeriodicCI/> and <CICleanup/>, because the showrunner runs CI;
- it sends a checkpoint notice after <RecordPhaseCompletion/>;
- it adds a landing rule to the repair rounds in <FixDispatch/>.
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
  it is doing, in plain words: `— holding: waiting on the Codex seats writing
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
  <DelegateLaunchFailure/>'s retry.
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
Run every command under `~/.claude/scripts/delegate/` with
`dangerouslyDisableSandbox: true`; do not try sandboxed first. Ledger and history
calls run in the foreground. Unqualified delegate script names below resolve
under that directory. This avoids half-applied durable-state writes.

- Claude: launchers and `verify.sh final` use `run_in_background: true`; retain
  the returned task handle.
- Codex: launch the same command in a managed unified-exec terminal with
  `tty: true` and a short initial yield; retain its returned `session_id`. Do not
  shell-background the launcher: it waits for its worker and remains attached.
- Saved run output (traces, captures, logs) stays under a few GB: read each run
  and delete it before the next. A disk-floor sweeper keeps 500 GiB free on `/`
  by deleting every unit's build caches. Ask natedev for room before a run that
  must keep more. Put this rule in every seat and helper prompt that saves
  output. User, 2026-10-04: 300 GB of traces cost 235 GiB of caches.
</ToolingContract>

<DispatchContract>
Applies to every implementation, test, fix, and review launcher.

1. Launch under <ToolingContract/> and save `${DISPATCH_HANDLE}`.
2. Tell the user in one line what is running and what happens on completion.
3. Perform only synchronous work assigned by the call site: the main half of
   <DualReview/>. Do not inspect launcher output as a substitute for that review.
4. Claude: end the turn under <TurnEndGate/>, naming what is running in the
   `— holding: waiting on <what is running>` line, never its handle. Task and notifier messages resume
   the workflow independently; process the first without waiting for the other.
5. Codex: apply <CodexDispatchWait/>. Never end the turn while the launcher is
   active; its terminal result drives the next workflow step.
6. A launcher killed at its time limit leaves its Codex seat running with no
   one watching. Arm a Monitor on `${SESSION_DIR}/board.log` until that seat
   posts its own `done`, not a `launcher:` line. Once it has, and its last lint
   and test passed after its last edit, end it with `codex_mesh.py end
   --session-dir "${SESSION_DIR}" --to <seat>` (never `stop`, which ends every
   seat on the server). Then record the outcome the launcher would have; for a
   repair, that is <FixDispatch/>'s third outcome.
</DispatchContract>

<DelegateLaunchFailure>
A dispatch that dies in seconds never reached the provider, and a provider
message it prints is a message some earlier call received. Seats launched
together that fail within seconds of each other carrying **byte-identical text,
the same retry-at timestamp included, are a local fault**: separate API calls do
not produce identical text. Confirm it against
`${SESSION_DIR}/mesh_server.log` — a launch that got out of this machine wrote
to it, and one that added no line was answered by the session's own
`codex app-server` from what it had cached. Never report delegates as
unavailable, or a usage limit as reached, on the error text alone.

`codex exec` cannot test this and must not be used to. It opens its own process
against the API, where a dispatch attaches a thread to the long-lived server
recorded in `${SESSION_DIR}/mesh_server.json` — a different path that can fail
while `codex exec` answers. That disagreement is the wedged server's signature,
not evidence delegates are back. The only valid probe is the dispatch itself.

The launcher already tried. On a fast failure with no work done, `codex_mesh.py`
abandons the inherited server, starts one of its own and runs the seat again,
printing `retrying on a new app-server` to the seat's log. So an error that
reaches you has usually already been tested against a clean server and is real.
Read the log for that line before doing anything by hand: present, the retry
happened and the failure survived it; absent, the launcher held back — the seat
had already done work, or the failure took too long to be local — and the
paragraphs above are yours to apply.

Recovering by hand, in that case only. Check that no peer run claims the
recorded pid or port before signalling anything —
`grep -l '<pid>\|<port>' /tmp/claude/delegate/*/mesh_server.json` should name
only this session's file, and any second file means leave the process alone.
Then kill that pid, move `mesh_server.json` aside, and relaunch verbatim; the
next launcher starts a fresh server. Leave `mesh_roster.json` alone — its other
entries may name threads still live on a server that is fine. If the log **is**
growing and the error arrives new on each launch, the limit is real and none of
this applies.
</DelegateLaunchFailure>

<CodexDispatchWait>
Codex only; no timer process:

1. Empty-poll `${DISPATCH_HANDLE}` with `write_stdin`. Set `yield_time_ms` to the
   configured interval from <ProgressContract/>, capped by
   `background_terminal_max_timeout`; when the user has stopped updates, use
   the maximum. Do not use shell `wait`, `sleep`, a status-file loop, or a second
   terminal.
2. A result with the same `session_id` and no `exit_code` means the interval
   elapsed and the launcher remains active. Unless the user stopped updates,
   apply <ProgressContract/>, then poll the same session again.
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
`bash ~/.claude/scripts/delegate/verify.sh --session-dir "${SESSION_DIR}" <verb> …`,
with no env prefix, redirect or trailing command: the settings allow rule
matches only that shape, and the task notification already carries its exit
code. For `verify.sh final`, launch under <ToolingContract/> so `verify.sh`
opens its own progress window, and tell the user what is running. Claude ends
the turn and resumes from a task or notifier message; Codex applies
<CodexDispatchWait/> with progress disabled.
</BackgroundVerificationContract>

<CompactionContract>
- Do not maintain a handoff before the context hook requests one.
- When requested, write it in the repository and include the hook's fields plus
  `MODE`, `AUTO_WINDOW`, the last authorization, whether the user stopped
  progress updates, any live `DISPATCH_HANDLE`, any live
  `REVIEW_DISPATCH_HANDLE` with `EARLY_REVIEW` and `REVIEW_PASS`,
  `STYLE_REVIEW_DONE`, `STYLE_DIFF_BASE`, `NEXT_ITEMS_PATH`, whichever tagged
  `DelegatedPhaseReservationState` is live, and any
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
  `${SESSION_DIR}/delegated_phase_reservation_state.json` once coordination has
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
Read `~/.claude/docs/type_design.md`. Apply it in the main review and copy it
verbatim under `## Type Design Contract` into every implementation, fix, and
broad-review prompt. Fresh delegates inherit nothing from prior
calls. Closure reviews omit it to remain scoped to the repair.
</TypeDesignContract>

<WritePromptContract>
Read `~/.claude/docs/delegate/write_prompt_contract.md` in full before writing any
implementation or fix prompt. It also defines <PhaseTeam/>, <CoordinationBoard/>,
<PhaseMesh/>, <BuildTokenContract/>, <TeamFilePartition/>, and <RoleReassignment/>.
</WritePromptContract>

<VerificationContract>
Rust delegates run only exact prompt lines using
`~/.claude/scripts/delegate/verify.sh`:

| Intent | Command |
| --- | --- |
| compile feedback | `bash ~/.claude/scripts/delegate/verify.sh check <package>` |
| package tests | `bash ~/.claude/scripts/delegate/verify.sh test <package>` |
| one integration target alone | `bash ~/.claude/scripts/delegate/verify.sh test <package> <test>` |
| only the tests whose name contains `<name>`, while iterating | `bash ~/.claude/scripts/delegate/verify.sh test <package> --filter <name> [--filter <name> …]` |
| mend fix, format, scoped clippy, rustdoc | `bash ~/.claude/scripts/delegate/verify.sh lint <package>` |
| checkpoint format | `bash ~/.claude/scripts/delegate/verify.sh fmt <package>` |
| changed example | `bash ~/.claude/scripts/delegate/verify.sh example <package> <name>` |
| final workspace gate | `bash ~/.claude/scripts/delegate/verify.sh final` |

Rules:

- Serialization and result authority follow <BuildTokenContract/>: every line
  here takes the `cargo` token on its own, so a run may wait for a peer, and a
  result is a gate only once the slot owning that package's files has posted
  `done`.
- `check` and `test --filter` are feedback while iterating, never a gate.
  Run `check` after each batch of edits, and `test --filter` once per finished
  change, naming all its tests in one call (repeat `--filter`). Never re-run
  either on a tree that has not changed. User, 2026-10-04: seats ran a
  `--filter` after every edit, 619 test runs in six hours. After the seat's last edit, run
  `lint` once, naming any one modified package, then `test` for every modified
  package. Lint is never per crate: one run covers the whole workspace (mend
  and clippy across it, format and rustdoc for every changed member). A failed
  lint means fix the error it names, wherever it is in the workspace, then lint
  once more; never re-run lint on a tree that has not changed. User,
  2026-10-01: a Work Order that said "for each crate, lint first" cost 13 lint
  runs of one workspace, all failing on one error. Lint comes before test
  because `lint` rewrites the tree (mend fixes, then format), so a `test` run
  before it proves nothing about the tree that gets committed, and a `test`
  run after it is the one that counts. Never run the suites twice around a
  lint. Trace changed public APIs,
  traits, registration, and plugin wiring to modified callers. Name an integration target explicitly only to re-run it
  alone. Add example lines only when the phase owns them.
- Tests are the only testing: a passing `test` run proves the package builds.
  Never run `check` or any build alongside a `test` that is going to run anyway;
  `check` exists solely for mid-edit compile feedback.
- A launch is not a time to build or test: `verify.sh example` and any app or
  binary launch compile their own target. Never precede or follow a launch with
  a build, `check`, or `test` pass just to prove it builds.
- Phase prompts never use `final`, raw Cargo, `--all-targets`, or the full
  `clippy` skill. Workspace breadth belongs to <FinalGate/>.
- Non-Rust prompts list the project's exact scoped commands under the same
  run-only-what-is-listed rule.
- Prove any claimed pre-existing failure on the pre-phase tree before mentioning
  it. Do not stash; use the existing clean tree or a scratch checkout.
- Re-run suspected environment failures unsandboxed before classifying them.
  Nested Swift `sandbox-exec` failures and missing GPU adapters are environment
  failures, not dependency or code defects.
- A printed `SKIPPED` is skipped, not passed; do not bypass a disabled check
  manually.
- `style_review=off` does not waive <RunProjectStyleReview/>; it blocks the run
  from completing <FinalGate/>.
</VerificationContract>

<VerificationNarration>
Whenever choosing how a change gets verified — running tests, launching a
binary or example, or dispatching a delegate that will — state the choice and
its reason to the user in one line: `Running tests only because …`,
`Launching <binary|example> because …`, or `Delegate will run tests only
because …`. The line makes the verification economy visible; it never replaces
recording the result.
</VerificationNarration>

<FindingsLedger>
Use `python3 ~/.claude/scripts/delegate/findings.py <command> --session-dir
"${SESSION_DIR}"` only after <Synthesize/> confirms an issue.

| Command | Purpose |
| --- | --- |
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <delegate\|main\|both> [--lens <adversary\|contract\|craft\|ux>[,…]] [--detail <d>]` | create an id; `--lens` names every lens that raised it, comma-separated; legacy `both` means `adversary,contract` |
| `status` | read the ledger for closure review |
| `gate` | get `converged` or `dispatch`, plus batch and any advisory |
| `dispatch --covers F001,F002,...` | record one complete repair batch |
| `abandon --reason <r> [--edits-landed]` | a dispatched repair died; reopen its batch |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | record closure evidence |

`findings.py` owns batching and gating: the first round gates blockers and
minors, later rounds gate blockers, nits never gate. It rejects a partial batch,
so a round closes everything on the ledger; `start-phase` resets it.

**The gate never stops the run.** It answers `converged` or `dispatch` and
nothing else. Where it once stopped, it now returns that sentence in `advisory`
beside a `dispatch` verdict and the round runs. An advisory is not a gate and
never becomes one: **report it in one line** — the pattern in ordinary words,
beside the repair being dispatched — then continue. Never stop, ask permission,
re-open a decision the user has already made about this run, or edit
`~/.claude/config/delegate.conf` mid-run to change what gets said.

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
while a pass is open. The slot comes from `PLAN_DELEGATE_TEAM_ROLE` on the call
and never from the record — a launcher closes its own pass and nothing else, so
the unit director standing in for one names the seat it is standing in for, once
per killed launcher. A killed fix dispatch leaves its findings
`repair_in_flight` for the same reason, so `findings.py abandon` per
<FindingsLedger/> belongs beside this call.

Phase records are the unit director's, and both belong at the real boundary:
`finish-phase` for the outgoing phase and `start-phase` for the incoming one run
before that phase's first dispatch. Recording them late attributes the new
phase's work to the finished one — its title, its elapsed clock, and its pass
counts all describe the wrong phase.
</PassOwnership>

<ProgressContract>
Before every Codex poll, set `${PROGRESS_INTERVAL_SECONDS}` from
`PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` in `~/.claude/config/delegate.conf`.
The key sets the Codex poll timeout and a Claude unit's notifier interval.
For Codex, a missing or non-positive integer is an error; tell the user to set
it. <PrepareSession/> creates Claude's `delegate-<run id>` instance, where
the run id is the basename of `${SESSION_DIR}`; `end_session.sh` removes it.
The agent arms nothing. When `prepare_session.sh` prints `notifier instance not
created`, say so in one line: this run gets no ticks until
`zsh ~/.claude/scripts/delegate/unit_notifier.sh "$CLAUDE_CODE_SESSION_ID"`
succeeds. While work runs, the notifier sends
`/unit:report` from `delegate-<run id>` every interval. That message
is the Claude tick. Each `progress_history.py progress` call restarts its
clock; `--hold` keeps at most one tick waiting.

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

On a Claude notifier tick or Codex poll timeout, compose the update per
<ProgressReport/>, which
`~/.claude/commands/unit/report.md` defines in full. Read that file and
follow it; a report written from memory of an earlier read drops the
byte-for-byte copy rule first. It also owns this tick's <EarlyReviewArm/>
trigger point and the query that answers questions about work already finished.
The user can invoke the same file as `/unit:report`.

After a Codex report, return to <CodexDispatchWait/> on the same session and
read the interval again before polling. A tick never replaces the completion
report: when work finishes, report its result and current progress header even
if a tick arrived recently.

A user-requested status check emits <ProgressReport/> immediately. If the user
stops updates, run
`zsh ~/.claude/scripts/message/notifier.sh stop delegate-<run id>` for Claude;
Codex keeps polling without reports. Resume Claude updates with `start` on the
same instance. Change one unit's interval with `/unit:interval <min>`.
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
required gate that cannot run, or delegate/environment error. It never stops
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
  `~/.claude/docs/delegate_plan_format.md`, report the deferral, and continue.
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
turn unless <TurnEndGate/> authorizes a stop between them. Steps 11 through 16
in particular are one continuous sequence from phase review through completion
recording, with no report emitted between them.

1. <PrepareSession/>
2. <ComposeWorkOrder/>
3. <ResolveStyleDiffBase/>
4. <VerbosePrePhaseGate/> when required
5. <CoordinateDelegatedPhaseReservation/>
6. <LaunchImplementation/>
7. <DualReview/>. Beside the implementation pass's review, run
   <RunApplicationSmokeTest/> and <UXReview/> steps 1–3; open their rows and let
   the first <Synthesize/> gate them with the code findings.
8. <Synthesize/>
9. <RunApplicationSmokeTest/>, then <UXReview/>, unless step 7 ran them and no
   later change reached the app or the screen
10. <RunProjectStyleReview/> — `single` only; loop and verbose run the run's one
    style review from <FinalGate/> after the whole plan is green
11. <RunPhaseReview/>
12. <RunPhaseShrink/>
13. <ConsiderNextItems/>
14. <CheckpointCommit/>
15. <PhaseCleanup/>
16. <RecordPhaseCompletion/>
17. <VerbosePostPhaseReport/> and applicable <VerbosePostPhaseGate/>
18. <NextPhase/> or <RunSummary/>
</ExecutionSteps>

<PrepareSession>
Run `bash ~/.claude/scripts/delegate/prepare_session.sh` under
<ToolingContract/>. Capture `SESSION_DIR` and set `WORKING_DIR`. The script also
creates the run-active marker and Claude's notifier instance; every exit must
eventually run `end_session.sh`
through <RunSummary/> or single-mode completion.
</PrepareSession>

<ComposeWorkOrder>
Read `~/.claude/docs/delegate/compose_work_order.md` in full and apply it at the
start of every phase, before any prompt is written. It owns Work Order
validation, the `**Pending decision:**` re-test, mode parsing, the run start,
`${NEXT_ITEMS_PATH}` derivation, and both the delegate-ready fast path and the
research fallback. Read it every time: a work order composed from memory is the
most common cause of a phase that builds the wrong thing.

If an initial verbose invocation contains a bounded-auto control, resolve
`AUTO_WINDOW` and run <AutoWindowBatchBriefing/> before
<CoordinateDelegatedPhaseReservation/>. Otherwise
follow <AuthorizationContract/>.
</ComposeWorkOrder>

<ResolveStyleDiffBase>
Read `~/.claude/commands/unit/style_review.md` in full and apply it once per
run, before the first dispatch. Loop and verbose only; `single` skips it and
never sets a base. That file defines this contract and <RunProjectStyleReview/>.
Never resolve the base from memory of an earlier read. On the default branch
with no other local branches and no branch named by the user, the run continues
on the default branch without asking; only a detached HEAD or other existing
branches raise the branch question, asked exactly once while dispatch waits.
The user can invoke the same file as `/unit:style_review`.
</ResolveStyleDiffBase>

<DelegatedPhaseReservationContract>
Coordination applies to phased Work Orders in every mode. Ad hoc work skips it.

Persist exactly one tagged `DelegatedPhaseReservationState` for the current
phase at `${SESSION_DIR}/delegated_phase_reservation_state.json`. Write every
state transition atomically and read it back before the next lifecycle action;
the initial state must be durable before dispatch. The domain states and
on-disk shapes are:

```json
{"kind":"repository_not_enrolled","phase":"<phase>"}
{"kind":"enrolled_awaiting_first_touch","phase":"<phase>"}
{"kind":"active","active_phase_reservation":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>"}}
{"kind":"checkpoint_committed_awaiting_release_confirmation","checkpoint_release_confirmation_pending":{"reservation_id":"<id>","coordination_run_id":"<uuid-v7>","phase":"<phase>","phase_start_head":"<full object id>","checkpoint_commit":"<full object id>"}}
```

| State | Created only from | Lifecycle |
| --- | --- | --- |
| `RepositoryNotEnrolled` | `/sync board` returning `unconfigured` | Owns no reservation; supplies no release argument. |
| `EnrolledAwaitingFirstTouch` | `/sync board` returning `board_ready` | The registered edit hook still owns first-touch acquisition; supplies no release argument. |
| `Active` | The edit hook, from a clear check that acquired or already held the scopes | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
| `CheckpointCommittedAwaitingReleaseConfirmation` | <CheckpointCommit/> step 6, atomically replacing `Active` once that step's commit succeeds | Copies the active fields plus the full object id captured immediately from that commit; supplies a release argument, and resumes only at release confirmation. |

These are
`DelegatedPhaseReservationState::{RepositoryNotEnrolled,
EnrolledAwaitingFirstTouch, Active(ActivePhaseReservation),
CheckpointCommittedAwaitingReleaseConfirmation(
CheckpointReleaseConfirmationPending)}`. Do not represent any of them as an
absent record or a bare optional value.

`ActivePhaseReservation` is orchestration memory: never reconstruct it from a
marker, rendered prose, conversation, or the engine's harness-session mapping —
that mapping is a disposable edit-authorization projection, replaced by another
claim in the same harness session, and no command reads a reservation id back
from it. `CheckpointReleaseConfirmationPending` is durable proof that the
checkpoint commit succeeded and only release confirmation remains: never create
or reconstruct it from conversation or from `git rev-parse HEAD` read at a later
time, and delete it only after <CheckpointCommit/> validates a successful
release.

An unsuccessful ending applies <RetainDelegatedPhaseReservation/>. This includes
a cancelled no-diff phase, dispatch or run error, failed checkpoint commit,
every `single` run including a dirty one, user stop, drift refusal, busy ledger,
and failed release. Never invoke release from an error handler, cancellation
path, single-mode completion, run summary, or session cleanup.
</DelegatedPhaseReservationContract>

<RetainDelegatedPhaseReservation>
For `Active` or `CheckpointCommittedAwaitingReleaseConfirmation`, leave both the
engine reservation and `${SESSION_DIR}/delegated_phase_reservation_state.json`
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
</RetainDelegatedPhaseReservation>

<CoordinateDelegatedPhaseReservation>
Run after phase authorization and task selection, immediately before
<LaunchImplementation/>. It is the only pre-dispatch coordination boundary.
Do not dispatch until this contract reaches one of its four persisted states.

0. On resume, read the state file first. A valid state for the current phase is
   authoritative:

   | State | Resume action |
   | --- | --- |
   | `Active`, this phase | Resume without another claim. |
   | Either inactive state, this phase | Resume without an engine mutation; when enrolled, acquisition remains the registered edit hook's. |
   | `CheckpointCommittedAwaitingReleaseConfirmation`, this phase | The checkpoint already committed and only its release confirmation remains: resume at <CheckpointCommit/> step 7 without re-committing, re-claiming, or dispatching. |
   | Either reservation-bearing state, another unfinished phase | A stranded reservation; stop the new dispatch. |

   A completed phase removes its inactive state under <RecordPhaseCompletion/>,
   so no old inactive tag is silently reused for a new phase.
1. When no state exists yet, invoke the shared `/sync board` entry point once,
   under <BerthDecisions/>:

   ```sh
   cargo-berth board --json
   ```

   | Result | Action |
   | --- | --- |
   | `unconfigured` | Persist `RepositoryNotEnrolled` and proceed silently. |
   | `board_ready` | Persist `EnrolledAwaitingFirstTouch` and proceed without claiming predicted paths. |
   | `ledger_unreadable` | Stop the run; it is never opt-out. |
   | `busy` | Stop with “the ledger is busy, try again” and the exact board command to rerun; do not retry. |
2. The registered PreToolUse edit hook owns the next transition. A clear check
   atomically acquires exact `file:` scopes before the edit, then replaces
   `EnrolledAwaitingFirstTouch` with `Active` from the returned facts. A blocked
   check refuses the edit and leaves the pending state intact; answer an overlap
   under <BerthDecisions/>. An unreadable ledger stops without facts. Beyond
   those answers, do not call `cargo-berth claim` on behalf of a Work Order.
</CoordinateDelegatedPhaseReservation>

<BerthDecisions>
Overlap answers, incursion resolves, orphan retirement and releases never reach
the user. Under a production, send the showrunner the overlap — holder ids,
paths, answer choices — and record the answer it picks; that answer is a
decision, not a relayed user approval. With no showrunner, pick the answer
yourself. Either way, report what was decided in one line.

An answer is `cargo-berth claim <paths> --<choice> <holder> --overlap-why
"<why>"`, `<choice>` one of `before`, `after`, `defer`, `override`; it records
in one step and exits 0. If another holder also conflicts, it is refused and
records nothing: split the claim so each call meets one holder.

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
Read `~/.claude/docs/delegate/launch_implementation.md` in full before the phase's
first dispatch, and again when that dispatch completes: steps 7-8 route its result.
</LaunchImplementation>

<ReviewDiffContract>
Before every broad or closure review, run `git status --short` and apply
`git add -N` to each new phase-created file so `git diff` contains it. Exclude
pre-existing untracked and unit-director-owned handoffs. Verify every created
file named by the delegate is visible; stop if not. Capture the diff and status.
</ReviewDiffContract>

<EarlyReviewArm>
Launch the blind reviewer while the writer is still running, so both finish
together instead of back to back. Evaluate only on a <ProgressContract/> tick,
and arm only when all of these hold: an implementation or fix dispatch is
active; `EARLY_REVIEW=none`; the completed dispatch would receive a delegate
review — <DualReview/> pass 1 or a closure review; **pass-internal** completion
of that dispatch is at least 75%; and at least ten minutes of writer time still
remain.

Read completion from the tick's evidence — the heartbeat shows the delegate
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

At an eligible tick, in that same tick:

1. Increment `${REVIEW_PASS}` now; <DualReview/> will not increment it again.
2. **Delete every stale delivery artifact and prove they are gone.**
   `${SESSION_DIR}` spans the whole run but `${REVIEW_PASS}` resets with every
   phase, so earlier phases' artifacts already sit on disk under the exact names
   this phase's launches will poll.

   `rm -f "${SESSION_DIR}"/final_diff_*.diff "${SESSION_DIR}"/final_diff_*.ready`

   Clear the whole glob, not just the current index — later passes in this phase
   collide the same way — then confirm no sentinel remains before continuing. A
   stale sentinel releases the reviewer onto a previous phase's diff, which
   returns a confident and entirely false blocker saying the phase implemented
   nothing; and because the sentinel is what releases `review.sh` to call
   `start-pass`, it opens the review pass while the writer is still running, so
   the recorder closes the live implementation pass as `interrupted` and refuses
   every later `progress` call in that phase. Both failures are silent until the
   false verdict or the refused call.
3. Apply <ReviewDiffContract/> to the current partial tree. The delegate has
   named no created files yet, so that check is vacuous; the snapshot is
   expected to be incomplete.
4. Write the applicable early-form prompt — the `adversary` lens of
   <BroadReviewPrompt/> for pass 1, <ClosureReview/> for a fix — including the
   completion estimate, the partial diff, and the exact final-diff and
   ready-sentinel paths below. **Only the adversary arms early.** It is the lens
   that gains most from the extra time, and arming both against a partial
   tree would double the exposure to the void verdict below; `contract`
   launches at completion under <DualReview/> step 3.
5. Launch `review.sh` exactly as <DualReview/> step 3 does — with `adversary`
   as its lens for pass 1 — appending one extra final argument:
   `${SESSION_DIR}/final_diff_${REVIEW_PASS}.ready`. Save the
   handle as `${REVIEW_DISPATCH_HANDLE}` and set `EARLY_REVIEW=launched`. Leave
   `${DISPATCH_HANDLE}` untouched.
6. Before the recorder call in <ProgressReport/> step 5, put the reviewer in
   the round table:

   `python3 ~/.claude/scripts/delegate/progress_history.py arm-review --session-dir "${SESSION_DIR}" --activity "<what this reviewer is checking>" --called-task delegate.review [--lens adversary]`

   Pass the same lens the launch carries, and none where the launch carries
   none: the marker names the status and pid files it will watch for, and a
   marker watching the unsuffixed pair while the launcher writes the suffixed
   one retires the row on its first tick, reporting a live reviewer as gone.
   It is a presentation marker, not a pass event, so it cannot forge anything
   convergence counts. It resolves the same reviewer `review.sh` will, retires
   its row when `review.sh` errors or its process is gone, and is superseded
   when the sentinel lets `review.sh` open the real pass. The two running rows
   are the announcement, and they carry what a sentence cannot: which agent each
   one is, when it started, and how long it has been going.

The extra argument defers `review.sh`'s `start-pass` until the sentinel appears,
so the implementation pass and the review pass never overlap in the recorder. At
most one early launch per dispatch; otherwise review runs synchronously, as it
always may — early launch is opportunistic, never required.

| Event | Required action |
| --- | --- |
| Primary completes | After <LaunchImplementation/> step 7, write the final diff to `${SESSION_DIR}/final_diff_${REVIEW_PASS}.diff` — a closure review stays limited to its paths per <ClosureReview/> — then create `${SESSION_DIR}/final_diff_${REVIEW_PASS}.ready`. **Never create the sentinel before the diff is fully written.** |
| Primary errors, or the run stops | Kill the early reviewer. If the sentinel exists, close its pass with `PLAN_DELEGATE_TEAM_ROLE=test … finish-pass --status canceled --orphaned-launcher` per <PassOwnership/> — the seat the `adversary` lens sits in; before the sentinel no pass was recorded, so record nothing — a pre-sentinel kill counts toward no advisory, including the blind-review cancellation one. |
| Reviewer errors before delivery | Report it in one line, clear `${REVIEW_DISPATCH_HANDLE}`, set `EARLY_REVIEW=none`, and leave the numbered artifacts. The primary continues and is reviewed synchronously at completion under the next `${REVIEW_PASS}` index. |
| A verdict arrives before delivery | **It is void.** The reviewer cannot have read a diff that does not exist yet, so discard its findings entirely rather than reading them as evidence: open nothing in the ledger, preempt nothing, route no blocker into a fix dispatch. Say in one line that it is discarded and why, then follow the reviewer-error row. A void verdict is often fluent and specific — a stale diff supports confident claims about missing work — so the check is the timing, never how convincing the text reads. |

Every path that ends an early launch before its real pass starts also drops the
row it was given:

`python3 ~/.claude/scripts/delegate/progress_history.py disarm-review --session-dir "${SESSION_DIR}" --reason "<canceled|reviewer error|void verdict>"`

Run it alongside clearing `${REVIEW_DISPATCH_HANDLE}` and `EARLY_REVIEW`. The
row would retire itself at the next report anyway; this is how the reason
reaches the record, and it is what lets that report state the reason rather
than infer one.
</EarlyReviewArm>

<DualReview>
Read `~/.claude/docs/delegate/dual_review.md` in full before writing any review prompt:
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

**Close every delegation result with the current progress header** — both tables
and the wall-clock line, produced by <ProgressReport/> steps 3 and 5 with the
current pass or activity. This is unconditional: the numbered items say what
happened, and the tables say how far into the phase and the plan it happened,
which is the half the user cannot reconstruct. Emit it after any launch,
printed below the sections above exactly as the recorder emits it. Should the
recorder answer that no window is open, the
launcher has not recorded its pass yet: try once more, then continue without the
tables rather than stalling the turn.
</DelegationResultFormat>

<FixDispatch>
For a `dispatch` batch, set `${FIX_ROUND}` from the gate's `round` and create
`${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md` under
<WritePromptContract/>. Work Specification contains every batch id with concrete
file/line findings and intended behavior. Verification contains only implicated
`verify.sh` lines—usually check and test, adding lint only for lint-related
repairs.

A repair runs **one seat per file set no other seat touches**: when the failing
tests or findings split that way, always run as many seats as you can reasonably
manage; otherwise one. User, 2026-10-04. The first is slot `impl`, the rest
`fix2`, `fix3`…, each task and kind `fix`, each with its own
`fix_prompt_${FIX_ROUND}[_<slot>].md`. Each seat makes its repair and writes, for each testable finding, the regression test that would
have caught it — one that fails without the repair. A finding about what is
drawn gets a test that renders and reads the pixels back, or a live pixel check
the prompt names; a test of components, constants or hand-written pointer hits
does not count. It names each test for the behavior it pins and puts the
finding id only in its summary; an id in code outlives the review that defined
it. <ClosureReview/> is the cold read, so no seat is spent
on one here. A seat's file set is its findings' files plus their test targets;
its prompt names every other seat's files as read only.

Run `findings.py dispatch --covers <all batch ids>` before launching, then:

```sh
PLAN_DELEGATE_RESOLVES_ROUND=1 implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" fix \
  "<responsibility>" fix "<activity>" "${FIX_ROUND}" impl
```

**`PLAN_DELEGATE_RESOLVES_ROUND=1` is what lets the launcher resolve the round**:
only the launcher watches the worker exit, so only it can say a repair landed.
Each further seat runs the same call with `PLAN_DELEGATE_RESOLVES_ROUND=0`, its
own prompt and its slot; the round is done only once every seat has exited.

Apply <DispatchContract/>; set `EARLY_REVIEW=none` at dispatch, and close the
turn with the progress header per <DelegationResultFormat/>. While a fix runs
that will receive a delegate closure review, <EarlyReviewArm/> may arm that
reviewer early.

**A repair the main read can close, closes without a delegate review.** Apply
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
diagnosis of its cause before any new seat, and the next prompt carries the
measured cause.

Dispatch the normal <DualReview/> closure review only when the diff cannot
answer the question: a public API or signature change with a caller outside
the diff, a registration, transition, or invariant reaching past the batch, an
id whose verdict reads unclear, a claim the seat says it could not verify and
the main read cannot verify either, or a new defect the repair introduced.
Uncertainty routes to the reviewer; a hunk fully read and fully understood
does not. A closure review costs a stage of the phase, so it is spent on a
question, never on a path.

On completion, `implemented` continues as above; `error` applies
<DelegateLaunchFailure/>, and then, if the error survives it,
<RetainDelegatedPhaseReservation/>, reports the fix log, records an error
outcome, clears the session marker, and stops. Both outcomes resolve the round
in the ledger through the launcher. Any third outcome — the dispatch stopped,
killed, or gone without `impl_status` reaching either — is the unit director's to
resolve with `findings.py abandon` per <FindingsLedger/>, then apply
<RetainDelegatedPhaseReservation/> before reviewing, re-dispatching, or
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
   itself, with no design judgment and no reviewer disputing the reading — the
   unit director applies them all directly, runs the implicated `verify.sh` lines,
   reports the edits and why they qualified, and continues. A fix round is
   never dispatched for a batch that is trivial throughout; a single
   non-trivial finding sends the whole batch through steps 4–5 instead.
4. Apply <DecisionRouting/> before opening findings.
5. Open every confirmed remaining issue, then obey `findings.py gate`:
   - `converged`: retain nits for retrospective; continue to smoke, then
     <UXReview/>.
   - `dispatch`: apply <FixDispatch/> to the complete batch, then return here.
     When the payload carries an `advisory`, say it in one line and dispatch
     anyway per <FindingsLedger/>.
6. Stop only when the plan leaves a real design choice or reviews conflict on
   intended behavior:

```
Your choice:
1. One more delegate fix pass — [work and cost; recommendation with reason].
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
delegate report is not a smoke test.

Launch the real product from `${WORKING_DIR}` with useful logging/backtraces,
exercise the changed runtime behavior, observe stability, close cleanly, and
record command/action/result. The launch compiles its own target — never run a
build, `check`, or `test` pass first to prove it builds. Startup alone suffices only when no changed
behavior can be invoked. A pass continues to <UXReview/>.

A panic, fatal log, unexpected exit, or wrong behavior is a blocker: route it
through <Synthesize/>, then repeat review, synthesis, and smoke before later
gates. If this environment cannot perform the interaction or locate an
applicable executable, close the process, record `deferred — <exact human action
and limitation>`, and continue to <UXReview/> without waiting. Deferred smoke
allows the checkpoint but is batched at <FinalGate/> and reported by
<RunSummary/>.
</RunApplicationSmokeTest>

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
3. Spawn a fresh Claude Agent helper in the background with the showrunner's
   design-check prompt, so the shots never enter your context:

   > Read `~/.claude/commands/ux_eval.md` and follow it for these shots:
   > <paths>. Guide: <path>. Scale: <shot pixels per logical pixel>. Context:
   > <unit> phase <N> — <what changed, every state the shots must show, and
   > each shot's window size in logical pixels>. Also judge by the three gods
   > (`<ThreeGods/>` in `~/.claude/docs/decision_criteria.md`) and these
   > production rules: <the production doc's rules that concern looks>. You
   > only review: edit nothing. Return only the verdict and the table.

   Read its verdict once, then stop it with TaskStop. Save each canonical
   candidate it lists, without viewing it, into a slot still empty:
   `mkdir -p <guide>/examples/<stem> && magick <shot> -crop <crop> +repage <guide>/examples/<stem>/<slot>.png`.
   Saving it approves it for use now. Add one line to
   `<guide>/examples/pending.md`, which holds it for the user's final approval:
   `- <stem>/<slot>.png — <why it is clear> — <unit>, <date>`. Close the activity with
   `finish-activity --session-dir "${SESSION_DIR}" --result <pass|N defects>`.
4. Open each defect row as a finding — blocker, because the showrunner's merge
   check holds a checkpoint on any visible defect. A `no rule` row is still a
   finding:
   `findings.py open --severity blocker --lens ux --caught-by delegate --title <defect> --file <shot path> --detail "<rule>: <fix>"`
   Then obey `findings.py gate` exactly as <Synthesize/> step 5 does, the same
   repair round under <FixDispatch/>, except that `converged` continues to
   <RunPhaseReview/>.
5. Re-entered after a `ux` repair that changed the screen, this step replaces
   1–4: re-shoot and re-judge only the rows raised — a fresh helper, under its
   own `UX review` activity, given just those rows and their shots — and record
   each with `findings.py verdict`. A `still_open` row returns to step 4's
   gate. Then continue to <RunPhaseReview/>.
6. **The notice's verdict.** In a production unit, the checkpoint notice's
   `design check:` line (<ProductionUnit/> item 3) is one fresh helper's
   verdict on exactly the shots the notice sends, from a build of the
   checkpoint's code. Step 5's partial re-judge does not count, nor does a
   verdict from before any later change to what users see (a repair, a merge
   of the merge branch). In those cases, before the notice, re-shoot the whole
   set and judge it once more as in steps 1–3. Send on a `pass`, or with each
   defect moved to a follow-up phase (item 7's landing rule); a defect left in
   the phase goes back to step 4. The showrunner merges on that line and runs
   no check of its own (user rule 2026-10-01).
</UXReview>

<RunProjectStyleReview>
Read `~/.claude/commands/unit/style_review.md` in full and apply it. This is
the run's single style audit, over everything the project built rather than one
phase: `single` runs it after first smoke, loop and verbose from <FinalGate/>.
Phases never run it — they carry no style gate, and a phase checkpoint never
waits on one. Never run it from memory of an earlier read; the after-cleanup
reverification and the smoke reset are what get dropped. The user can invoke the
same file as `/unit:style_review`.
</RunProjectStyleReview>

<RunPhaseReview>
Read `~/.claude/docs/delegate/run_phase_review.md` in full after smoke and
<UXReview/>, once per phase. It also defines <RunPhaseShrink/>, which follows
it before the checkpoint.
</RunPhaseReview>

<ConsiderNextItems>
Read `~/.claude/commands/unit/add_ons.md` in full and apply it after
shrink, at each phase boundary. Phased plans only; the unit director performs the
assessment and never launches another agent for it. It writes `apply`
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
per completed phase. Loop and verbose only; `single` never commits.

This is durable state with no cheap undo, so read the whole contract before
acting on any part of it, and read the reservation record from disk. A value
remembered from conversation, taken from the harness session mapping, or
re-derived from current `HEAD` is not proof and will silently accept the wrong
checkpoint. The user can invoke the same file as `/unit:checkpoint`.

The phase does not complete until the reservation release is confirmed, so a
failed or busy release applies <RetainDelegatedPhaseReservation/> rather than a
retry. <PushCheckpoint/> runs only after that release; a failed push never
fails the checkpoint.
</CheckpointCommit>

<PhaseCleanup>
After <RunPhaseShrink/> and a successful checkpoint when one applies, run:

`bash ~/.claude/scripts/delegate/clear_phase_review.sh "${SESSION_DIR}" <phase-id>`
`python3 ~/.claude/scripts/delegate/remove_seats.py --session-dir "${SESSION_DIR}"`

The first removes only this phase's review prose; structured progress history
remains. Do not clear before shrink succeeds or while a checkpoint can still
fail. The second removes this run's claude seats, which nothing messages after
the phase, and any seat a dead run left alive; a failure there is one line in
the report, never a stop.

Then end everything else this phase started: TaskStop each Claude Agent
helper, end finished launchers, and shut down every Hana or
example app it launched. Never touch what another session started. Mid-phase,
stop each helper once its result is read and each app once no step uses it.
User, 2026-10-03.
</PhaseCleanup>

<RecordPhaseCompletion>
After smoke, UX review, phase review, shrink, next-item consideration, cleanup,
and checkpoint when applicable, run `progress_history.py finish-phase
--session-dir "${SESSION_DIR}" --status completed`.

After a loop/verbose phase with `RepositoryNotEnrolled` or
`EnrolledAwaitingFirstTouch`, delete its completed-phase state
file. `Active` must already have been replaced by
`CheckpointCommittedAwaitingReleaseConfirmation`, and that pending state must
already have been deleted by the validated successful release. If either state
survives, the phase is not complete and this section must not run.

In `single`, also run `finish-run --status completed`, then run <RunAsBuilt/>,
apply <RetainDelegatedPhaseReservation/>, report that no checkpoint release was
attempted, run `bash ~/.claude/scripts/delegate/end_session.sh`, and end. Other
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
If no todo phase remains, run <FinalGate/>, then <RunAsBuilt/>, then the final
<PeriodicCI/> point and <CICleanup/>, then <RunSummary/>. Otherwise reset `REVIEW_PASS=0` and smoke to `not_run`.
Style state is per-run, not per-phase: never reset it or delete its marker
here, and never re-resolve `STYLE_DIFF_BASE`.

- Every mode but `single`: first run <PeriodicCI/> when a CI point is due, and
  repair a red CI result before dispatching.
- Loop: announce next phase and return to <ComposeWorkOrder/>.
- Verbose/no window: announce its briefing and return to <ComposeWorkOrder/>.
- `next N`: decrement after completion; clear at zero, otherwise continue.
- `through X`: clear after X, otherwise continue.

Every return rechecks Pending decisions. When a window closes, prepare the next
briefing but do not dispatch it.
</NextPhase>

<FinalGate>
Loop/verbose only after plan exhaustion:

1. Launch `verify.sh final` under <BackgroundVerificationContract/>. It owns workspace
   fmt-check, all-targets check, and full tests.
2. For Rust, read `~/.claude/commands/clippy.md` in full and apply it inline
   with arguments `auto-proceed no-style`. Style stays out of this step; step 4
   owns it.
3. On failure, create a synthetic phase `final` / `Final verification` once:
   capture a new baseline, run `start-phase`, reset review and smoke state, open
   the concrete failures in the ledger, then use its gate plus <FixDispatch/>.
   Later repairs do not repeat those resets. After closure convergence, run
   applicable smoke and return here; do not run phase review or a phase
   checkpoint for the synthetic phase. Rerun this gate after each repair.
4. Once full verification is green, run <RunProjectStyleReview/> exactly once —
   this is the run's only style pass, and it covers every phase the project
   checkpointed, not just synthetic final fixes. Then rerun steps 1-2 so its
   cleanup receives full breadth.
5. Batch all deferred smoke actions after the gate is green. Ask the user once;
   route discovered defects through the synthetic fix path. If declined, carry
   them as outstanding rather than blocking run completion.
6. Finish the synthetic phase when applicable, run <FinalGateCommit/>, and
   record the final result.

Single mode and early endings skip this gate and state why in <RunSummary/>.
An ending that never reaches this gate never runs the style review;
<RunSummary/> reports that, and the next run over the same plan resolves the
same `STYLE_DIFF_BASE` and picks the whole project up again.
</FinalGate>

<FinalGateCommit>
Read `~/.claude/docs/delegate/final_gate_commit.md` in full once <FinalGate/> is green.
It also defines <RunAsBuilt/> and <AsBuiltCommit/>, which follow it.
</FinalGateCommit>

<RunAsBuilt>
Defined in `~/.claude/docs/delegate/final_gate_commit.md`; read it in full at a complete
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
**Style review:** [range reviewed and result, or the reason the run never ran it]
**Smoke checks still unperformed:** [phase + exact action, or none]
**Deferred decisions still open:** [phase + decision, or none]
**Add-ons awaiting review:** [count, or none]
**CI points:** [each point from `ci_points.log` with its result, the CI branch
merged or kept, or not applicable — fewer than five phases or `single`]
**Reservation disposition:** [checkpointed and outstanding, retained with the
reason this run stopped, or coordination not active]
**Why the run stopped:** [complete, user stop, pending decision, or error]
```

Apply <UserFacingText/> and <RetainDelegatedPhaseReservation/> for every ending
that did not complete <CheckpointCommit/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Finally run `bash ~/.claude/scripts/delegate/end_session.sh` on
every exit so the Stop hook cannot revive a finished run. Then run
<ReviewPendingAddOns/>; the summary and its commits never wait on it.
</RunSummary>
