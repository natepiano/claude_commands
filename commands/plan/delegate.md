---
description: Delegate phased work with review, repair, smoke/style gates, as-built shrink, approved follow-up capture, and one checkpoint per phase. Supports automatic loop, verbose gating, bounded auto windows, and single no-commit mode.
---

# Delegate

The main agent owns design, orchestration, review, gates, and user communication.
The configured delegate agent writes implementation code.

**Usage:** `/plan:delegate [plan-doc-path] [phase N] [single|verbose] [auto next N phases|auto through phase X] [free-text instructions]`

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
- `NEXT_ITEMS_PENDING`: `${SESSION_DIR}/next_items_pending.md`; absent or empty
  means no unreviewed additions or amendments.
- `PROGRESS_UPDATES_ENABLED`: starts true; user cancellation sets it false for
  the rest of the run.
- `DISPATCH_HANDLE`: active launcher task handle (Claude) or managed terminal
  `session_id` (Codex).
- `PROGRESS_TIMER_HANDLE`: Claude only; starts empty and identifies its current
  one-shot managed background timer.
- `REVIEW_PASS`: review dispatch count for the current phase; starts at 0.
- `IMPLEMENTATION_TASK`: starts as `implementation`.
- `APPLICATION_SMOKE_RESULT`: starts as `not_run`.
- `STYLE_GATE_CONFIG`: plan hint captured during prompt composition.
- `STYLE_REVIEW_DONE`: starts false. The durable true state is
  `${SESSION_DIR}/style_review_done`; later fixes never clear it.
- `MECHANICAL_GATE_CLEANUP_USED`: starts false. Its durable marker is
  `${SESSION_DIR}/mechanical_gate_cleanup_used` and resets with the phase.
- `FINDINGS`: the current phase's `findings.py` ledger.

<TagReferenceContract>
`<section-name/>` means apply the complete matching tagged contract. The
definition is authoritative. A call site states only its local inputs, outputs,
or exceptions; it does not restate the contract.
</TagReferenceContract>

<CoreContract>
- Never create a worktree, switch branches, or modify unrelated files.
- The main agent does not write implementation code unless the user explicitly
  asks. Exceptions: agreed doc-only/trivial post-review fixes and the single
  inline cleanup in <RunPhaseStyleReview/>.
- `single` never commits. Loop and verbose modes create exactly one
  <CheckpointCommit/> per completed phase. No other commit is allowed.
- A checkpoint never pushes. If a phase explicitly needs a remote commit for a
  dependency pin, consumer, or CI run, pushing that working branch is mechanical
  phase work, not a user decision or prerequisite.
- Verify a claimed blocker against the live tree. Follow a decision already
  settled by the plan. Do not expose sandbox flags, scripts, status files,
  ledger ids, or other tooling mechanics in user-facing reports.
</CoreContract>

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
</ToolingContract>

<DispatchContract>
Applies to implementation, review, fix, and architect launchers.

1. Launch under <ToolingContract/> and save `${DISPATCH_HANDLE}`.
2. Tell the user in one line what is running and what happens on completion.
3. Perform only synchronous work assigned by the call site: the main half of
   <DualReview/>. Do not inspect launcher output as a substitute for that review.
4. Claude: if progress is enabled, arm <ProgressContract/>; then end the turn.
   Task and timer notifications resume the workflow independently. Process the
   first notification without waiting for the other.
5. Codex: apply <CodexDispatchWait/>. Never end the turn while the launcher is
   active; its terminal result drives the next workflow step.
</DispatchContract>

<CodexDispatchWait>
Codex only; no timer process:

1. Empty-poll `${DISPATCH_HANDLE}` with `write_stdin`. Set `yield_time_ms` to the
   configured interval from <ProgressContract/>, capped by
   `background_terminal_max_timeout`; when progress is disabled, use the
   maximum. Do not use shell `wait`, `sleep`, a status-file loop, or a second
   terminal.
2. A result with the same `session_id` and no `exit_code` means the interval
   elapsed and the launcher remains active. If progress is enabled, apply
   <ProgressContract/>, then poll the same session again.
3. An initial launch or poll result with `exit_code` means the launcher finished.
   Clear `${DISPATCH_HANDLE}`, read the call site's status/result files, and
   route to review, synthesis, repair, smoke, or the next stage immediately in
   this turn. Do not end the turn between completion and routing.
4. A user message may interrupt the poll. Answer it, retain the session handle,
   and resume this contract unless the user cancels or redirects the run.
</CodexDispatchWait>

<BackgroundVerificationContract>
For `verify.sh final`, launch under <ToolingContract/> and tell the user what is
running. Claude ends the turn and resumes from the task notification. Codex
applies <CodexDispatchWait/> with progress disabled. It is not an agent dispatch
and has no heartbeat monitor.
</BackgroundVerificationContract>

<CompactionContract>
- Do not maintain a handoff before the context hook requests one.
- When requested, write it in the repository and include the hook's fields plus
  `MODE`, `AUTO_WINDOW`, the last authorization, `PROGRESS_UPDATES_ENABLED`, any
  live `DISPATCH_HANDLE`, any Claude `PROGRESS_TIMER_HANDLE`,
  `STYLE_REVIEW_DONE`, `MECHANICAL_GATE_CLEANUP_USED`, `NEXT_ITEMS_PATH`, and any
  unresolved next-item approval. Exclude it from review intent-to-add and
  commits.
- Never stop or delay work for compaction. Claude resumes from a live-dispatch
  notification; Codex remains in <CodexDispatchWait/>.
- After compaction, re-read this command in full, then the handoff. Restore live
  dispatches and state; delete the handoff after its phase is committed.
- A real user decision may still end the turn after the Stop hook's one retry.
</CompactionContract>

<UserFacingText>
For every briefing, decision, progress update, review result, and report, read
and follow `~/.claude/docs/user_facing_explanation.md`. Reconstruct context for
the user; do not pass through internal review or tooling vocabulary.
</UserFacingText>

<ExplainOnDemand>
If the user is confused or asks for a reframe at any gate, preserve the gate and
read `~/.claude/docs/explain_on_demand.md`. Explain from concrete behavior and
real signatures, with problem/fix code examples where required. Explanation is
not authorization; restate the pending question afterwards.
</ExplainOnDemand>

<TypeDesignContract>
Read `~/.claude/docs/type_design.md`. Apply it in the main review and copy it
verbatim under `## Type Design Contract` into every implementation, fix,
escalation, and broad-review prompt. Fresh delegates inherit nothing from prior
calls. Closure reviews omit it to remain scoped to the repair.
</TypeDesignContract>

<WritePromptContract>
Every implementation or fix prompt contains these sections once:

1. Role: write the requested code directly; do not ask questions.
2. Boundaries: do not commit, branch, or touch unrelated files; summarize files,
   reasons, and deviations when done.
3. Heartbeat: before each activity, run
   `bash ~/.claude/scripts/agents/heartbeat.sh <concrete SESSION_DIR>/heartbeat.log agent "<activity>"`.
   Use short present-tense text and never read the heartbeat file.
4. `## Project Context`.
5. `## Work Specification`.
6. `## Type Design Contract` per <TypeDesignContract/>.
7. `## Verification` per <VerificationContract/>.

The Verification section must say: run only its listed commands, never raw
Cargo; run each listed command with the sandbox disabled; do not report until
every command has exited and its output has been read. If an edited package has
no listed `test` line, add that package's scoped
`verify.sh test` and report it. Omit plan **Style** metadata and never load the
style guide; <RunPhaseStyleReview/> owns the one style audit.
</WritePromptContract>

<ReviewPromptContract>
Every reviewer is a fresh read-only session. It narrates each activity as a
short output line so the wrapper heartbeat remains live. It does not receive a
heartbeat path and never receives `${IMPL_SUMMARY}`.

Do not repeat the implementer's listed verification or audit style. A reviewer
may run one specific omitted check only when a plausible regression lies outside
the listed gate, and must name that command. Broad reviews apply
<TypeDesignContract/>; closure reviews apply only <ClosureReview/>.
</ReviewPromptContract>

<VerificationContract>
Rust delegates run only exact prompt lines using
`~/.claude/scripts/delegate/verify.sh`:

| Intent | Command |
| --- | --- |
| compile feedback | `bash ~/.claude/scripts/delegate/verify.sh check <package>` |
| package tests | `bash ~/.claude/scripts/delegate/verify.sh test <package>` |
| integration target | `bash ~/.claude/scripts/delegate/verify.sh test <package> <test>` |
| format + scoped lint | `bash ~/.claude/scripts/delegate/verify.sh lint <package>` |
| checkpoint format | `bash ~/.claude/scripts/delegate/verify.sh fmt <package>` |
| changed example | `bash ~/.claude/scripts/delegate/verify.sh example <package> <name>` |
| final workspace gate | `bash ~/.claude/scripts/delegate/verify.sh final` |

Rules:

- `check` is optional feedback, not a gate. Every modified package gets `test`
  and `lint`; trace changed public APIs, traits, registration, and plugin wiring
  to modified callers. Add example or integration lines only when the phase owns
  them.
- Phase prompts never use `final`, raw Cargo, `--all-targets`, or the full
  `clippy` skill. Workspace breadth belongs to <FinalGate/>.
- Non-Rust prompts list the project's exact scoped commands under the same
  run-only-what-is-listed rule.
- Prove any claimed pre-existing failure on the pre-phase tree before mentioning
  it. Do not stash; use the existing clean tree or a scratch checkout.
- Re-run suspected environment failures unsandboxed before classifying them.
  Nested Swift `sandbox-exec` failures and missing GPU adapters are environment
  failures, not dependency or code defects.
- `verify.sh lint` and `fmt` honor `~/.claude/config/lint.conf`. A printed `SKIPPED` is
  skipped, not passed; do not bypass a disabled check manually.
- `style_review=off` does not waive <RunPhaseStyleReview/>; it blocks the
  checkpoint.
</VerificationContract>

<FindingsLedger>
Use `python3 ~/.claude/scripts/delegate/findings.py <command> --session-dir
"${SESSION_DIR}"` only after <Synthesize/> confirms an issue.

| Command | Purpose |
| --- | --- |
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <delegate\|main\|both> [--detail <d>]` | create an id |
| `status` | read the ledger for closure review |
| `gate` | get `converged`, `dispatch`, or `stop`, plus batch and reason |
| `dispatch --covers F001,F002,...` | record one complete repair batch |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | record closure evidence |
| `override --reason <the user's own words>` | clear one wrong `stop` |

The script, not the main agent, owns convergence: first round gates blockers and
minors; later rounds gate blockers; nits never gate. It rejects partial batches
and stops on repeated failed closure, reopening, stalled counts, repair budget,
repeated pass shape, a second blind-review cancellation, or the backstop. The one
<MechanicalGateCleanup/> exception bypasses only a repair-budget stop; it never
calls `findings.py dispatch`. `start-phase` resets the ledger.

A `stop` can be wrong about the world when its inputs were: an aborted launcher,
a pass recorded outside <PassOwnership/>, a count carried across a mislabeled
phase boundary. Never edit the run history to clear one — that history is the
audit trail, and rewriting it destroys the evidence that the stop was wrong.
Report the stop and its evidence, get the user's explicit decision, then record
`override --reason "<their words>"`. The override names the one stop reason it
clears, is spent by the round it authorizes, and is appended beside the stop it
corrects. A stop the evidence supports is never overridden; find the real defect.
</FindingsLedger>

<PassOwnership>
Every pass is recorded by the launcher that runs it. `implement.sh` and
`review.sh` call `progress_history.py start-pass` and `finish-pass` themselves,
around the worker they wait on, for completion and for error alike. The main
agent never calls either by hand: the launcher's own records already exist, so a
hand-written call forges a pass that never ran, and `findings.py gate` counts
passes when it decides whether a phase is converging. The recorder enforces this
and rejects an unowned call.

The one exception is a launcher the main agent killed — the <DualReview/>
preemption. Its pass stays open because the process died before recording, so
close it with `finish-pass --status canceled --orphaned-launcher`, which the
recorder accepts only for `canceled` and only while a pass is actually open.

Phase records are the main agent's, and both belong at the real boundary:
`finish-phase` for the outgoing phase and `start-phase` for the incoming one run
before that phase's first dispatch. Recording them late attributes the new
phase's work to the finished one — its title, its elapsed clock, and its pass
counts all describe the wrong phase.
</PassOwnership>

<ProgressContract>
The main agent produces progress reports from the plan, launcher state,
heartbeat, and live diff. Before every progress-enabled wait, set
`${PROGRESS_INTERVAL_SECONDS}` from `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS`
in `~/.claude/config/timings.conf`; use 240 when it is missing or not a positive
integer. This is the Claude timer delay and Codex poll timeout.

Claude: while a dispatch is active and progress is enabled, keep exactly one
one-shot timer in a managed background terminal. Launch:

`sleep "${PROGRESS_INTERVAL_SECONDS}"; printf 'PLAN_DELEGATE_PROGRESS_TICK\n'`

Save its handle and end the turn normally. The timer contains no loop and runs
no agent. Codex never launches this timer; a <CodexDispatchWait/> timeout is its
progress tick.

On a Claude timer notification or Codex poll timeout:

1. Check launcher state first. For Codex, `exit_code` alone marks terminal
   completion; a returned `session_id` without it remains active. If no dispatch
   remains active, emit no stale report and process completion. On Claude, also
   stop and clear any timer when its dispatch completes first.
2. Read the current Work Order and verification list, the latest relevant
   heartbeat lines, `git status --short`, and `git diff --stat` in
   `${WORKING_DIR}`. Compare status with the phase baseline; include untracked
   paths without changing the index.
3. Derive the **current-phase** percentage from completed and remaining work,
   changed areas, current activity, and passed verification—not elapsed time.
   Round hard: stay below 20 only until implementation appears; editing is the
   middle; completed verification lines form the final stretch; reviews advance
   by inspected scope. Use the last factually passed cap stage:
   `implementation` 75, `initial_review` 85, `open_findings` 90, `closure` 95,
   `checkpoint` 98, or `complete` 100.

   **Do not derive the whole-plan percentage.** `progress_history.py` computes it
   from the plan's phase headings and overwrites whatever `--project-raw-percent`
   and `--project-percent` carry, so pass the phase percentage there and treat
   the project value as advisory. Never count phases by hand or by grep: a
   heading takes three forms over its life—`· status: todo`, `· status: done`,
   and the shrunk as-built form that drops the status marker and carries a commit
   annotation instead—and any pattern keyed on `status:` silently ignores every
   archived phase. That mistake reported a 68%-complete plan as 36%. To read the
   count directly, without an active phase and pass:

   `python3 ~/.claude/scripts/delegate/progress_history.py phase-count --plan-doc "<plan>" [--phase-percent N]`
4. Run:

   `python3 ~/.claude/scripts/delegate/progress_history.py calibrate --session-dir "${SESSION_DIR}" --candidate-percent "${PHASE_RAW_PERCENT}"`

   Use its phase suggestion when applicable; otherwise keep the raw value. Then
   run:

   `python3 ~/.claude/scripts/delegate/progress_history.py progress --session-dir "${SESSION_DIR}" --project-raw-percent "${PROJECT_RAW_PERCENT}" --project-percent "${PROJECT_RAW_PERCENT}" --phase-raw-percent "${PHASE_RAW_PERCENT}" --phase-percent "${PHASE_REPORTED_PERCENT}" --cap-stage "<stage>" --activity "<current activity>" [--phase-override-reason "<specific evidence>"]`

   Include the override reason only when rejecting an applicable calibrated
   value. The recorder refreshes any legacy run whose project clock was not
   script-resolved. Copy the resulting Markdown header exactly. Durations below
   one day are always `HH:MM:SS`; longer durations are
   `<days> day(s) HH:MM:SS`.
5. Add one or two ordinary-English sentences covering current activity,
   material work now present, and what remains. Do not paste logs or filenames.
6. If the dispatch remains active, Claude reads the interval again, launches a
   fresh one-shot timer, replaces the handle, and ends the turn. Codex returns
   immediately to <CodexDispatchWait/> on the same session and reads the
   interval again before polling.

A user-requested status check performs steps 1-5 immediately. If the user stops
updates, stop and clear any Claude timer and set
`PROGRESS_UPDATES_ENABLED=false` for the rest of the run; Codex keeps polling
without reports.
</ProgressContract>

<AuthorizationContract>
Mode behavior:

| Mode | Before phase | After phase | Commit |
| --- | --- | --- | --- |
| `single` | no gate | end | none |
| `loop` | automatic | next phase | one checkpoint |
| `verbose` | <VerbosePrePhaseGate/> unless an approved auto window is active | report, then `continue` gate unless windowed | one checkpoint |

Loop invocation authorizes its phase checkpoints. Verbose authorization occurs
only after the required briefing:

- `proceed` or `approved`: current phase only; trailing text amends its prompt.
- `auto next N phases`: positive N including the current phase.
- `auto through phase X`: current through a current-or-later todo phase X.
- `stop`: end without the described phase.
- Post-phase `continue`: show the next briefing only; it never authorizes work.

An auto window removes intermediate stops, not explanations. Apply
<BriefingFreshness/> before its first dispatch. An auto control on initial
invocation still requires <AutoWindowBatchBriefing/> because no phase has been
briefed.

Before the first loop/verbose dispatch, stop on a dirty tree unless the selected
plan doc is the only dirty path; then include it in the first checkpoint.

Loop stops only for that dirty-tree guard, an unresolved current Pending
decision, a real design choice, reviews conflicting on intended behavior, a
ledger `stop`, a required gate that cannot run, or delegate/environment error.
It may also stop at a phase or auto-window boundary for
<ConsiderNextItems/> approval, and only for that step's `gate` proposals — its
`apply` ones are written and reported, never asked. Apply
<MechanicalGateCleanup/> before treating an
eligible repair-budget verdict as a stop. Everything else auto-routes,
resequences, or defers. Verbose adds only its authorization gates.
</AuthorizationContract>

<BriefingFreshness>
A phase is freshly briefed when the user received its complete
<PhaseBriefing/> in the current uninterrupted pre-phase review sequence, every
pending decision and user amendment was surfaced and resolved, and no later
edit changed its behavior, scope, files, or verification. Sequential individual
briefings count; they need not appear in one batch message. A follow-up question
or explanation does not stale a briefing. Recording an accepted decision that
the briefing and discussion already described does not stale it.

When an auto control arrives and every covered phase is freshly briefed, that
control is the batch approval: set the approved `AUTO_WINDOW` and proceed to
<SelectTask/> without repeating briefings or asking for `proceed`. If any
covered phase is unbriefed, stale, or has an unresolved decision, route to
<AutoWindowBatchBriefing/>. Compressed rows and phase titles are not briefings.
</BriefingFreshness>

<DecisionRouting>
For every decision raised by review, repair, or phase review:

- If the plan already defines behavior and only ordering is wrong, resequence,
  split, merge, or renumber phases; preserve scope, API, invariants, tests, and
  ownership. Update affected Work Orders and continue automatically.
- If current-phase correctness has at least two buildable unresolved answers,
  stop and ask.
- If only a later phase is affected, write a `**Pending decision:**` block using
  `~/.claude/docs/delegate_plan_format.md`, report the deferral, and continue.
  That phase's pre-dispatch check will stop on it.
- If an alleged option cannot be implemented by the phase's actual structure,
  correct the plan instead of presenting it as a choice.
</DecisionRouting>

<ExecutionSteps>
Execute in order:

Apply <CoreContract/>, <CompactionContract/>, and <UserFacingText/> throughout.

1. <PrepareSession/>
2. <ComposeWorkOrder/>
3. <VerbosePrePhaseGate/> when required
4. <SelectTask/>
5. <LaunchImplementation/>
6. <DualReview/>
7. <Synthesize/>
8. <RunApplicationSmokeTest/>
9. <RunPhaseStyleReview/>
10. <RunPhaseReview/>
11. <RunPhaseShrink/>
12. <ConsiderNextItems/>
13. <CheckpointCommit/>
14. <DiscardPhaseReviewText/>
15. <RecordPhaseCompletion/>
16. <VerbosePostPhaseReport/> and applicable <VerbosePostPhaseGate/>
17. <NextPhase/> or <RunSummary/>
</ExecutionSteps>

<PrepareSession>
Run `bash ~/.claude/scripts/delegate/prepare_session.sh` under
<ToolingContract/>. Capture `SESSION_DIR` and set `WORKING_DIR`. The script also
creates the run-active marker; every exit must eventually run `end_session.sh`
through <RunSummary/> or single-mode completion.
</PrepareSession>

<ComposeWorkOrder>
1. For a phased plan, scan the target Work Order for `**Pending decision:**`.
   Verify cited code still matches the block. If unresolved, present it, apply
   <ExplainOnDemand/> when needed, edit the resolution into Spec/Files/gate, and
   remove the block before continuing. A resolution introduces behavior nothing
   else audits — `/plan:phase_review`'s `<StateAndConsequenceAudit/>` inspects
   only what a phase already shipped — so run that audit against the resolution
   here and state its destination and owner alongside it. An in-repository
   destination is the Spec/Files/gate edit already being made. A destination in
   another repository goes to the next-items file derived in step 5, and only
   with the user's approval; never append to it automatically.
2. Parse the complete bounded-auto phrase before a standalone phase selector.
   Reject `single` plus `verbose`, auto without `verbose`, non-positive N, or an
   invalid range. Set `MODE=single` for `single` or non-phased work,
   `MODE=verbose` for a phased verbose invocation, otherwise `MODE=loop`.
   Infer absent work from the conversation.
3. Run `progress_history.py start-run --session-dir "${SESSION_DIR}"
   --working-dir "${WORKING_DIR}" [--plan-doc <path>]`. It is idempotent; stop
   if exact main-agent identity cannot be detected. The recorder alone owns the
   project clock: for a supplied plan it validates and uses `Project started`,
   or derives and persists it from the plan's oldest Git commit or run start;
   for ad hoc work it reuses the latest plan-backed clock for the exact working
   directory and branch, or starts at this run when none exists. Never
   calculate, pass, edit, or correct a project timestamp in the agent.
4. A delegate-ready plan has `## Delegation Context` and a target
   `#### Work Order` per `~/.claude/docs/delegate_plan_format.md`. `verbose`
   requires one.
5. For a phased plan, derive `${NEXT_ITEMS_PATH}` beside `${PLAN_DOC}`. Lowercase
   its filename stem, replace each run of non-alphanumeric characters with one
   hyphen, trim leading/trailing hyphens, and append `-next.md`. Stop if the
   normalized stem is empty. The file need not exist.

**Delegate-ready fast path:** do not research the codebase. Assemble
`${SESSION_DIR}/implementation_prompt.md` under <WritePromptContract/>:

- Project Context: Delegation Context except **Style**, plus Constraints from
  prior phases.
- Work Specification: Goal, Spec, Files verbatim, plus command-line amendments.
- Capture **Style** only as `${STYLE_GATE_CONFIG}` for
  <RunPhaseStyleReview/>.
- Verification: translate Build/Test/Lint/Run/Smoke and Acceptance gate into
  <VerificationContract/> lines. Convert old raw Cargo/full-clippy entries to
  scoped `verify.sh`; the main agent retains live smoke ownership.

Do not open code to fill a plan gap. Name the gap and let review catch its
effect. Mark the dispatch as assembled from the Work Order without research.

**Fallback:** research only enough to write the same prompt structure. Quote an
applicable plan section verbatim, or compose a complete spec from the
conversation with files, behavior, APIs, edges, and constraints. Point to files
instead of copying their contents. Set `${STYLE_GATE_CONFIG}` to `rust` for Rust
work, otherwise `none`; do not load style. Derive scoped verification per
<VerificationContract/>.

If an initial verbose invocation contains a bounded-auto control, resolve
`AUTO_WINDOW` and run <AutoWindowBatchBriefing/> before <SelectTask/>. Otherwise
follow <AuthorizationContract/>.
</ComposeWorkOrder>

<PhaseBriefing>
Build only from Delegation Context, the Work Order, and command-line amendments:

```
## Phase N ready — <title>

### Why this phase exists
[purpose, dependencies, deliberate exclusions]

### Work to be done
[behavior, state transitions, ownership, visible effect]

### Important types and APIs this phase will introduce or change
| Type / trait / API | Status | Planned role | System relationship |
| --- | --- | --- | --- |

### Files and verification
[modules, acceptance gate, meaningful checks]
```

Rows include only load-bearing types explicitly named by the Work Order. Status
is `New`, `Existing - Changes`, or `Existing - No Changes`, inferred from that
Work Order without code research. Mark genuine uncertainty instead of guessing;
say explicitly when no load-bearing type is specified.
</PhaseBriefing>

<VerbosePrePhaseGate>
When `MODE=verbose` and no approved auto window is active, emit
<PhaseBriefing/> and ask exactly:

`Start Phase N? Reply \`proceed\` to run only this phase, \`auto next N phases\`, \`auto through phase X\`, or \`stop\`.`

Apply <AuthorizationContract/>. Questions preserve the gate; confusion invokes
<ExplainOnDemand/>. Opening an auto window applies <BriefingFreshness/>: a fully
fresh range is authorized by the auto control itself; otherwise route to
<AutoWindowBatchBriefing/> before dispatch.
</VerbosePrePhaseGate>

<AutoWindowBatchBriefing>
Resolve the covered todo phases and apply <BriefingFreshness/> first. If every
covered phase is fresh, set the approved `AUTO_WINDOW` and continue directly to
<SelectTask/> without another briefing or gate. Otherwise read every Work Order
now and emit one complete <PhaseBriefing/> per phase in order; surface any
pending decision. Ask:

`Run phases <list> without stopping? Reply \`proceed\` to authorize all of them, \`proceed phase N\` to authorize only phase N and re-gate after it, or \`stop\`.`

Approval runs the resolved range without intermediate gates. Narrowing updates
`AUTO_WINDOW`; questions preserve the batch gate. Never accept a compressed row
or phase title as batch authorization.
</AutoWindowBatchBriefing>

<SelectTask>
Choose from the Work Order, never keyword matching:

- `implementation`: ordinary feature work.
- `escalation`: ambiguous architecture, numerical/transform mathematics, or a
  failed behavioral attempt.

`~/.claude/config/agents.conf` owns delegate family/model/effort. State the task
in the dispatch update.
</SelectTask>

<LaunchImplementation>
1. Once per phase, save `git status --short` to
   `${SESSION_DIR}/progress_baseline_status`; fixes retain it.
2. Close the outgoing phase before opening this one, per <PassOwnership/>: when a
   phase record is still active, run `progress_history.py finish-phase
   --session-dir "${SESSION_DIR}" --status completed` first. Then run
   `progress_history.py start-phase --session-dir "${SESSION_DIR}"
   --phase-id <id> --phase-title <title> --work-order-file
   "${SESSION_DIR}/implementation_prompt.md"`. Use `ad hoc` plus scope without a
   phased plan; pass the original prompt only. Both run before the dispatch in
   step 4, never after it.
3. Set `${PASS_KIND}=arch` for escalation, otherwise `${PASS_KIND}=impl`.
4. Launch
   `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}"
   "${WORKING_DIR}" "${SESSION_DIR}/implementation_prompt.md"
   "${IMPLEMENTATION_TASK}" "<responsibility>" "${PASS_KIND}" "<activity>"`.
   Responsibility follows <ProgressContract/>.
5. Announce prompt and heartbeat paths, then apply <DispatchContract/>.
6. On completion, read `impl_status`: `implemented` loads `impl_summary.txt`
   into `${IMPL_SUMMARY}`. On `error`, report `impl_agent.log`, record
   `finish-run --status error`, run `end_session.sh`, and stop; multi-phase runs
   also emit <RunSummary/>.
7. `implemented` is the delegate's claim, not a passed gate. Read
   `${IMPL_SUMMARY}` for a verification line it left running, unread, or
   unmentioned — "still running", "I'll report once it completes", a listed
   command with no stated result. When one is there, run that command yourself
   under <VerificationContract/> before <DualReview/> and review against its real
   output. A failing gate the delegate never read is a finding like any other,
   not a reason to reject the phase.
</LaunchImplementation>

<ReviewDiffContract>
Before every broad or closure review, run `git status --short` and apply
`git add -N` to each new phase-created file so `git diff` contains it. Exclude
pre-existing untracked and orchestrator-owned handoffs. Verify every created
file named by the delegate is visible; stop if not. Capture the diff and status.
</ReviewDiffContract>

<BroadReviewPrompt>
Write `${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md` under
<ReviewPromptContract/> with:

```
You are independently reviewing a change you did not write. Read surrounding
code as needed. Report a numbered list; each item has a title, file:line,
1-3 sentence explanation, and severity:
- blocker: wrong behavior, spec violation, or missing required work
- minor: real edge, error-handling, or quality defect
- nit: non-behavioral quality; style-guide conformance is out of scope
End with APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES. Do not invent findings.

## Specification
[implementer's Work Specification verbatim]

## Type Design Contract
[verbatim contract]

## Diff
[complete diff]

## Review Questions
1. Complete and correct against the specification?
2. Bugs, missed edges, or broken error handling?
3. Unrequested implementation?
4. Consistent with surrounding code?
5. Are domain types clear, and are owned bare Option<T> values replaced or
   justified at an external boundary?
```
</BroadReviewPrompt>

<DualReview>
1. Increment `${REVIEW_PASS}`. Pass 1 uses <BroadReviewPrompt/> over the whole
   phase; later passes use <ClosureReview/> over one repair.
2. Apply <ReviewDiffContract/> and create the applicable prompt.
3. Launch
   `bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}"
   "${WORKING_DIR}" "${SESSION_DIR}/review_prompt_${REVIEW_PASS}.md" review
   "<responsibility>" "<activity>" "${REVIEW_PASS}"` under
   <DispatchContract/>. Keep the blind-review handle.
4. While it runs, perform the main review. Pass 1 reads changed code in risk
   order: Work Order paths, public API/traits/registration/plugin wiring, then
   remaining hunks. Verify spec, extras, codebase fit, <TypeDesignContract/>, and
   `${IMPL_SUMMARY}` claims without loading or auditing the style guide. State
   any honest coverage limit. Later passes read only repair paths and affected
   callers, consumers, transitions, or invariants.
5. If this main pass confirms a substantial, unambiguous spec-defined defect
   while the blind review remains active, read its log once, cancel it, record
   `finish-pass --status canceled --orphaned-launcher` per <PassOwnership/>,
   open and gate the finding, and apply
   <FixDispatch/> with useful partial-review evidence. Preempt at most once per
   phase; never edit while an old-diff review remains active. A canceled review
   supplies no verdict or direct-fix agreement.
6. Otherwise finish the main pass and let <DispatchContract/> await the blind
   reviewer by its host-specific path. On completion, `reviewed` loads
   `review_findings.txt` into `${AGENT_REVIEW}`. On `error`, report it and
   continue explicitly with the main review alone. Numbered artifacts remain
   available through the unnumbered symlinks.
</DualReview>

<ClosureReview>
Run `findings.py status` and write a prompt under <ReviewPromptContract/> that
contains only:

- Each open id, severity, file:line, and title verbatim.
- Paths named by the fix plus new post-fix paths.
- Diff limited to those paths, including new files.
- Two questions: for each id, `FIXED`, `NOT FIXED`, or `UNCLEAR` with file/line
  evidence; and whether this repair breaks any caller, consumer, transition, or
  invariant of its changed symbols.

Forbid whole-phase, style, polish, and already-reviewed design findings. An
outside-path problem is valid only when a quoted repair hunk causes it. Omit the
broad questions and Type Design Contract.

After the main pass agrees, record `accepted` for fixed, `still_open` for not
fixed/unclear, or `reopened` with the invalidating hunk. Open any new defect
introduced by the repair, then return to <Synthesize/>.
</ClosureReview>

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
</DelegationResultFormat>

<FixDispatch>
For a `dispatch` batch, set `${FIX_ROUND}` from the gate's `round` and create
`${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md` under
<WritePromptContract/>. Work Specification contains every batch id with concrete
file/line findings and intended behavior. Verification contains only implicated
`verify.sh` lines—usually check and test, adding lint only for lint-related
repairs.

Set `${FIX_TASK}=mechanical` only when every item is documentation, formatting, lint
guidance, an agreed trivial rename, or another behavior-preserving edit. Choose
`${FIX_TASK}=escalation` for wrong behavior, math, unresolved architecture, or a
failed repair; otherwise set `${FIX_TASK}=implementation`.

Run `findings.py dispatch --covers <all batch ids>` before launching:

`bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}"
"${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" "${FIX_TASK}"
"<responsibility>" fix "<activity>" "${FIX_ROUND}"`

Apply <DispatchContract/>. After a non-mechanical repair, run <DualReview/>.
For a mechanical repair, apply <ReviewDiffContract/>, read it yourself, and
record each verdict without a delegate review. Force normal closure review if
the repair touched another path or any id remains unclear.

On completion, `implemented` continues as above; `error` reports the fix log,
records an error outcome, clears the session marker, and stops.
</FixDispatch>

<MechanicalGateCleanup>
One automatic cleanup is allowed after behavioral convergence when all of these
hold:

- `findings.py gate` stopped solely on `repair_budget`;
- every gating finding came from required lint, format, or style verification,
  has `fix_attempts=0`, and has one behavior-preserving mechanical repair;
- smoke and behavioral review passed; and
- `${MECHANICAL_GATE_CLEANUP_USED}=false` and its marker is absent.

Set the state true and write the marker before dispatch. Compose one mechanical
fix prompt containing the complete gate batch, exact diagnostics, and affected
test and lint lines. Do not call `findings.py dispatch`; the earlier behavioral
rounds remain authoritative. Launch with <DispatchContract/> using the gate's
next round number.

On completion, apply <ReviewDiffContract/>, read every cleanup hunk, and rerun
the exact failing verification plus affected tests. Record each existing id
directly as `accepted` or `still_open`. If all pass, continue from the blocked
gate without another style review or blind review. If any remains, the cleanup
changed behavior, or it touched an unrelated path, return to <Synthesize/> and
honor its normal gate; never use this exception twice in one phase.
</MechanicalGateCleanup>

<Synthesize>
1. Merge delegate and main findings, dedupe real issues, tag who caught each,
   and discard refuted findings with a concrete explanation.
2. Present <DelegationResultFormat/>. If the user is confused, apply
   <ExplainOnDemand/> before any choice.
3. If all remaining issues are doc-only or one/two-line mechanical changes and
   both reviews agree, the main agent applies them directly, reports why they
   qualify, and continues. This exception exists only after <DualReview/>.
4. Apply <DecisionRouting/> before opening findings.
5. Open every confirmed remaining issue, then obey `findings.py gate`:
   - `converged`: retain nits for retrospective; continue to smoke.
   - `dispatch`: apply <FixDispatch/> to the complete batch, then return here.
   - `stop`: apply <MechanicalGateCleanup/> when eligible; otherwise present the
     result plus the choices below and wait.
6. Also stop when the plan leaves a real design choice or reviews conflict on
   intended behavior:

```
Your choice:
1. One more delegate fix pass — [work and cost; recommendation with reason].
2. Stop here — preserve remaining items as written todos.
3. Talk through an item first.
```

Explain a convergence stop in ordinary language. Choice 1 overrides the ledger
and applies <FixDispatch/>; choice 2 continues to smoke; choice 3 preserves the
gate. With no gating issues, continue to <RunApplicationSmokeTest/>.
</Synthesize>

<RunApplicationSmokeTest>
Read the diff. If no repository binary reaches its changes, record
`not applicable — <reason>` and continue. Otherwise select the target from
Delegation Context Run/Smoke, Acceptance gate, repository instructions, then
manifest. A build, test binary, static example build, or delegate report is not
a smoke test.

Launch the real product from `${WORKING_DIR}` with useful logging/backtraces,
exercise the changed runtime behavior, observe stability, close cleanly, and
record command/action/result. Startup alone suffices only when no changed
behavior can be invoked.

A panic, fatal log, unexpected exit, or wrong behavior is a blocker: route it
through <Synthesize/>, then repeat review, synthesis, and smoke before later
gates. If this environment cannot perform the interaction or locate an
applicable executable, close the process, record `deferred — <exact human action
and limitation>`, and continue without waiting. Deferred smoke allows the
checkpoint but is batched at <FinalGate/> and reported by <RunSummary/>.
</RunApplicationSmokeTest>

<RunPhaseStyleReview>
Required exactly once when the current phase diff contains `.rs`, `Cargo.toml`,
or `Cargo.lock`, after behavioral convergence and first smoke, before phase
review. The actual diff, not `${STYLE_GATE_CONFIG}`, decides applicability.

1. If `STYLE_REVIEW_DONE=true` or the marker exists, restore true and continue.
2. Compare current status/diff, including untracked paths, with the phase
   baseline. With no Rust/Cargo changes, set true and write `not applicable` to
   the marker. Stop if the style pass would edit pre-existing Rust/Cargo work.
3. Save combined diff/status to `${SESSION_DIR}/style_review_before.diff` and
   `${SESSION_DIR}/style_review_before.status`, announce the single cleanup,
   and invoke the `clippy` skill inline as `style-only auto-proceed`. `Off`,
   error, or unresolved choice blocks completion.
4. On successful review, set true and write the result to the marker before any
   cleanup verification. Never clear it during later fixes.
5. Save `${SESSION_DIR}/style_review_after.diff` and
   `${SESSION_DIR}/style_review_after.status`, compare them with the before
   snapshots, and read every style-induced hunk. If Rust/Cargo changed, rerun
   exact phase `test` and `lint` lines for affected packages. Failures use normal
   finding/fix routing; an eligible repair-budget stop uses
   <MechanicalGateCleanup/> without another style pass.
6. If cleanup reached runnable code, reset smoke to `not_run` and rerun
   <RunApplicationSmokeTest/>. The guard skips this section on return.
7. Continue to <RunPhaseReview/>, or back to <FinalGate/> for synthetic final.
</RunPhaseStyleReview>

<RunPhaseReview>
For phased plans, invoke `plan:phase_review` with this run's `SESSION_DIR` and
`WORKING_DIR`; pass `auto` in loop/verbose and make `${NEXT_ITEMS_PATH}` available.
Its retrospective, review outcomes, and proposed next-item amendments are
temporary session files, never plan sections. It may edit only remaining `todo`
Work Orders; earlier `done` phases remain byte-identical. Later user choices
become Pending decision blocks.

Dispatch its architect review only when any trigger holds:

- implementation deviated from the Work Order;
- phases or remaining Work Orders were changed;
- a later Pending decision was added;
- the phase introduced or changed a semantic state, transition, failure,
  availability, recovery condition, diagnostic, or externally observable
  lifecycle;
- a changed type/API/registration/path is named by a remaining Work Order or
  `${NEXT_ITEMS_PATH}`;
- the ledger stopped convergence; or
- three phases completed since the prior architect review.

Otherwise pass `skip-architect`. When dispatched, focus real-code checking on
affected phases and next items, then give the rest a consistency pass. It uses
`review.sh`, this session, and the next review index. Ad hoc work skips this
section.
</RunPhaseReview>

<RunPhaseShrink>
For a phased plan, invoke `plan:shrink "${PLAN_DOC}" --phases <current-id>
--closeout "${SESSION_DIR}"` after phase review and before checkpoint. This is
the final plan mutation for the phase. It replaces only the current phase's
`Work Order` with `As-built`; prior `done` phases must remain byte-identical and
remaining `todo` phases must retain the forward edits from phase review.

Require a successful structural check and a current phase containing no Work
Order, Retrospective, or Phase Review heading. Failure blocks checkpoint. Ad hoc
work skips this section.
</RunPhaseShrink>

<ConsiderNextItems>
Phased plans only. The main agent performs this assessment; do not launch another
agent. After shrink, read the current `As-built` block, phase diff,
`${SESSION_DIR}/phase_review_outcomes_<phase>.md`, remaining `todo` Work Orders,
`${SESSION_DIR}/next_item_amendments_<phase>.md` when present, and
`${NEXT_ITEMS_PATH}` when it exists. Inspect targeted in-scope consumer or crate
code only when those sources cannot confirm a candidate.

A candidate must be needed for the plan's broader outcome and target an
in-scope consumer named by Delegation Context, the crate's API/implementation,
or a crate example. Exclude a current-phase defect, work already owned by a
remaining Work Order, and optional polish or an idea that is not required.
Current-phase defects return to <Synthesize/>.

Each new candidate is an `add` proposal with title, target, need, completion
condition, and source phase. Import each architect proposal as `amend` or
`remove` with its exact current item, replacement, reason, and source phase, then
delete its amendment artifact. Deduplicate all proposals by action, target, and
observable outcome against `${NEXT_ITEMS_PATH}` and `${NEXT_ITEMS_PENDING}`.

**Then split the proposals into `apply` and `gate`.** The gate costs the user a
turn, so it is spent only where there is something to decide. This file is a
backlog: writing an item into it commits nobody to building it, and the decision
that matters happens later, when an item is scheduled into a phase. What earns a
gate here is destroying or rewriting a record the user may have already read —
not creating one.

A proposal is **`apply`** — do it now, do not ask — when it is purely additive or
purely factual:

- Any `add`. A new backlog item records that something may be worth doing. It
  changes no phase, no schedule, and no commitment.
- An `amend` that only makes an existing item agree with code that has already
  shipped, leaving it asking for exactly the work it asked for before: a drifted
  file or line reference, a stated dependency the completed phase satisfied, a
  named consequence the completed phase created, a claim the completed phase
  falsified. Cite the evidence in the one-line report and move on.

A proposal is **`gate`** — ask the user — when it removes or rewrites what is
already recorded: any `remove`, and any `amend` that changes what the item asks
for, what would satisfy it, or what it targets. Deleting a candidate and
redefining one are the user's calls; nothing here may make them.

When the split is genuinely unclear, gate it. Never write a `gate` proposal to
the repository without approval.

**A defect in what this phase just shipped is never an `add`.** Filing it as
future work converts a fix into a backlog entry the user must later approve, read,
and schedule — three costs where there was one. Route it to <Synthesize/> and fix
it in this phase, even when no remaining Work Order names the files it touches:
Work Order **Files** lists scope the plan predicted, not permission to edit.

Apply every `apply` proposal to `${NEXT_ITEMS_PATH}` now, in one edit, and report
them as a single line naming the count and the file — not as a list, and never as
a question. They never enter `${NEXT_ITEMS_PENDING}`, never appear in the block
below, and never hold an auto window open.

Only `gate` proposals reach the gate. If a bounded auto window continues after
this phase, return immediately without reporting or asking. `next N` continues
when N is greater than 1; `through X` continues until the current phase is X. At
an ordinary phase boundary or the last phase of an auto window, continue
silently when `${NEXT_ITEMS_PENDING}` is absent or empty; otherwise present all
pending candidates once:

```
## Items to consider

1. **<Amend | Remove>: <title>**
   **Target:** <in-scope consumer | crate | crate example>
   **Current:** <exact existing item>
   **Proposed:** <replacement, or removal>
   **Why:** <missing capability or changed evidence>
   **Completion condition:** <observable result; amend only>

Reply `approve <numbers>`, `revise <number>: ...`, or `reject <numbers>`.
```

Every `gate` proposal requires a disposition. Feedback revises it and requires
re-presentation; questions preserve the gate. Apply approved actions only:
replace the exact quoted item for `amend`, and delete the exact quoted item for
`remove`. If a target no longer matches, refresh and re-present it. Preserve
existing content and create this structure only when an `add` lands in a file
that does not exist:

```
# <plan title> — Next

## Items to consider

- [ ] **<title>**
  - Target: <in-scope consumer | crate | crate example>
  - Why needed: <missing capability>
  - Completion condition: <observable result>
  - Revealed by: Phase <id>
```

Remove resolved entries from `${NEXT_ITEMS_PENDING}` and delete it when empty.
A rejected add does not enter the repository; a rejected amendment leaves the
existing item unchanged. In loop/verbose, an approved file change joins the
current phase checkpoint; in `single`, it remains uncommitted. The boundary gate
occurs before the last auto phase's checkpoint, never between auto phases.
</ConsiderNextItems>

<CheckpointCommit>
Loop/verbose only:

1. Require smoke pass, `not applicable`, or `deferred`; require
   `STYLE_REVIEW_DONE=true`.
2. Confirm status contains only this phase, its plan doc, and an approved change
   to `${NEXT_ITEMS_PATH}` when present.
3. Run `verify.sh fmt <package>` for every touched package; include resulting
   formatting changes.
4. Mark the phase `status: done`. Never put its commit hash in the plan.
5. Stage the phase, its plan doc, and `${NEXT_ITEMS_PATH}` when approved and
   changed; commit exactly once:

   ```
   checkpoint(<plan-slug>): phase N — <title>

   <what the phase built>

   Claude-Session: <session url>
   ```

6. Report `Checkpoint <short hash> — phase N: <title>.` Never push here.
</CheckpointCommit>

<DiscardPhaseReviewText>
After <RunPhaseShrink/> and a successful checkpoint when one applies, run:

`bash ~/.claude/scripts/delegate/clear_phase_review.sh "${SESSION_DIR}" <phase-id>`

This removes raw review prompts, findings, logs, and the temporary retrospective
and outcome files. Do not clear them before shrink succeeds or while a checkpoint
can still fail. Structured progress history remains; no review prose remains in
the plan or phase scratch files.
</DiscardPhaseReviewText>

<RecordPhaseCompletion>
After smoke, style, phase review, shrink, next-item consideration, cleanup, and
checkpoint when applicable, run `progress_history.py finish-phase --session-dir
"${SESSION_DIR}" --status completed`.

In `single`, also run `finish-run --status completed`, then
`bash ~/.claude/scripts/delegate/end_session.sh`, and end. Other modes continue.
</RecordPhaseCompletion>

<VerbosePostPhaseReport>
After every completed verbose phase, including an auto window, report only that
phase from its reviewed diff, accepted fixes, `As-built` block, and checkpoint:

```
## Phase N complete — <title>

### Why this phase exists
[purpose and deliberate boundary]

### What now works
[reviewed behavior]

### Important types and APIs
| Type / trait / API | Status | Role | System relationship |
| --- | --- | --- | --- |

### Verification and review
[gate, meaningful tests/lint, review/fixes, one style result, smoke]

**Checkpoint:** `<short hash>`
```

Use the diff over planned claims. Include only load-bearing new/materially
changed types; use the same statuses as <PhaseBriefing/> and say when none exist.
Never include or ask about the next phase here.
</VerbosePostPhaseReport>

<VerbosePostPhaseGate>
Skip when no todo phase remains or an auto window continues. Otherwise ask:

`Reply \`continue\` when you are ready to review the next phase's pre-phase briefing, or \`stop\` to end the run.`

`continue` authorizes only composing that briefing. `stop` ends through
<RunSummary/>. `proceed`, `approved`, questions, or discussion do not advance;
answer from the completed report and preserve the gate.
</VerbosePostPhaseGate>

<NextPhase>
If no todo phase remains, run <FinalGate/> then <RunSummary/>. Otherwise reset
`REVIEW_PASS=0`, `IMPLEMENTATION_TASK=implementation`, smoke to `not_run`, style
to false, mechanical cleanup to false, and delete both markers.

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
2. For Rust, invoke `clippy auto-proceed no-style` inline. Per-phase style is
   already complete.
3. On failure, create a synthetic phase `final` / `Final verification` once:
   capture a new baseline, run `start-phase`, reset review/smoke/style state and
   marker, reset mechanical cleanup state and marker, open the concrete failures
   in the ledger, then use its gate plus <FixDispatch/>. Later repairs do not
   repeat those resets. After closure
   convergence, run applicable smoke and return here; do not run phase review or
   checkpoint for the synthetic phase. Rerun this gate after each repair.
4. Once full verification is green, run <RunPhaseStyleReview/> exactly once on
   synthetic final fixes, then rerun steps 1-2 so cleanup receives full breadth.
5. Batch all deferred smoke actions after the gate is green. Ask the user once;
   route discovered defects through the synthetic fix path. If declined, carry
   them as outstanding rather than blocking run completion.
6. Finish the synthetic phase when applicable and record the final result.

Single mode and early endings skip this gate and state why in <RunSummary/>.
</FinalGate>

<RunSummary>
Emit on every multi-phase ending:

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Final gate:** [result or skipped reason]
**Smoke checks still unperformed:** [phase + exact action, or none]
**Deferred decisions still open:** [phase + decision, or none]
**Why the run stopped:** [complete, user stop, pending decision, convergence reason, or error]
```

Apply <UserFacingText/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Finally run `bash ~/.claude/scripts/delegate/end_session.sh` on
every exit so the Stop hook cannot revive a finished run.
</RunSummary>
