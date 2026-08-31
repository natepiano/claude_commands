---
description: Delegate phased work with review, repair, smoke gates, one branch-wide style review at the end of the project, as-built shrink, approved follow-up capture, and one checkpoint per phase. Supports automatic loop, verbose gating, bounded auto windows, and single no-commit mode.
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
</TagReferenceContract>

<CoreContract>
- Never create a worktree or modify unrelated files. The only branch the run may
  create is the one the user approves in <ResolveStyleDiffBase/>; never switch to
  an existing branch.
- The main agent does not write implementation code unless the user explicitly
  asks. Exceptions: agreed doc-only/trivial post-review fixes and the single
  inline cleanup in <RunProjectStyleReview/>.
- `single` never commits. Loop and verbose modes create exactly one
  <CheckpointCommit/> per completed phase, plus the one <FinalGateCommit/> that
  closes the run. No other commit is allowed.
- A checkpoint never pushes. If a phase explicitly needs a remote commit for a
  dependency pin, consumer, or CI run, pushing that working branch is mechanical
  phase work, not a user decision or prerequisite.
- A phase reservation is released only by the successful-checkpoint path in
  <CheckpointCommit/>. Cancellation, error, failed commit, `single`, user stop,
  and failed release never release it or delete its durable record.
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
Applies to every implementation, test, fix, and review launcher.

1. Launch under <ToolingContract/> and save `${DISPATCH_HANDLE}`.
2. Tell the user in one line what is running and what happens on completion.
3. Perform only synchronous work assigned by the call site: the main half of
   <DualReview/>. Do not inspect launcher output as a substitute for that review.
4. Claude: if progress is enabled, arm <ProgressContract/>; then end the turn.
   Task and timer notifications resume the workflow independently. Process the
   first notification without waiting for the other. Re-arm before every
   subsequent turn that leaves work running -- a completed dispatch that hands
   straight off to verification, a smoke run, or a style pass is still running
   work, and its timer is the one most often dropped.
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
5. An early-launched reviewer occupies its own managed terminal under
   `REVIEW_DISPATCH_HANDLE`. Keep polling the primary dispatch session; each
   timeout is also the <EarlyReviewArm/> evaluation point. After the primary
   completes and the ready sentinel is written, poll the reviewer session under
   this same contract.
</CodexDispatchWait>

<BackgroundVerificationContract>
For `verify.sh final`, launch under <ToolingContract/> and tell the user what is
running. Claude ends the turn and resumes from the task notification. Codex
applies <CodexDispatchWait/> with progress disabled.

It is not an agent dispatch and has no heartbeat monitor, so nothing else
reports on it. That makes a timer more necessary here, not less: export
`PLAN_DELEGATE_SESSION_DIR="${SESSION_DIR}"` so `verify.sh` opens its own
progress window, and arm a timer under <ProgressContract/> exactly as a dispatch
does.
</BackgroundVerificationContract>

<CompactionContract>
- Do not maintain a handoff before the context hook requests one.
- When requested, write it in the repository and include the hook's fields plus
  `MODE`, `AUTO_WINDOW`, the last authorization, `PROGRESS_UPDATES_ENABLED`, any
  live `DISPATCH_HANDLE`, any live `REVIEW_DISPATCH_HANDLE` with `EARLY_REVIEW`
  and `REVIEW_PASS`, any Claude `PROGRESS_TIMER_HANDLE`,
  `STYLE_REVIEW_DONE`, `STYLE_DIFF_BASE`, `NEXT_ITEMS_PATH`, whichever tagged
  `DelegatedPhaseReservationState` is live, and any
  unresolved next-item approval. When a claim is still in its authorization
  round trip, also include its UUID-v7 coordination run, captured phase-start
  HEAD, answer, blocker, reason, and transient proposal token. Exclude the
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
Every implementation or fix prompt contains these sections once:

1. Role: write the requested code directly; do not ask questions. Name the
   slot this prompt is for and the role it opens in, per <PhaseTeam/>.
2. Boundaries: do not commit, branch, or touch unrelated files; summarize files,
   reasons, and deviations when done, and **write that summary to this slot's
   `impl_summary_<role>.txt` as the last act before finishing** — a background
   session has no output redirect, so a summary left only in the reply is a
   summary the orchestrator never sees. State this slot's file set and the
   peers' file sets per <TeamFilePartition/>, and that a peer's file is blocked
   rather than merged.
3. Narration: before each activity, run
   `bash ~/.claude/scripts/delegate/board.sh post <concrete SESSION_DIR> <slot> status "<activity>"`.
   Use short present-tense text and never read the heartbeat file.
4. `## Team` — the three slots, who holds which files, and the board commands
   from <CoordinationBoard/>. Say that peers are working concurrently, that the
   board carries the record every member can read, and that a `verify.sh` run
   may pause while a peer finishes its own. Never mention the cargo token or
   ask for it: <BuildTokenContract/> takes it inside `verify.sh`, and an agent
   holding it by hand deadlocks against its own verification.
   Whenever this slot has a mesh address, also give it its own mesh name, both
   peers' names, the call that reaches each of them, and — on the claude path —
   the orchestrator's name from `ListAgents`, per <PhaseMesh/>. An address a
   member has to go looking for is one it will not use, and a codex peer needs
   the literal `codex_mesh.py` command line with the concrete `--session-dir`
   already filled in, not a description of it.
5. `## Project Context`.
6. `## Work Specification`.
7. `## Type Design Contract` per <TypeDesignContract/>.
8. `## Verification` per <VerificationContract/>, exactly as listed and with
   nothing added around it.

The Verification section must say: run only its listed commands, never raw
Cargo; run each listed command with the sandbox disabled; do not report until
every command has exited and its output has been read. If an edited package has
no listed `test` line, add that package's scoped
`verify.sh test` and report it. Omit plan **Style** metadata and never load the
style guide; <RunProjectStyleReview/> owns the run's one style audit.

It must also instruct the delegate: tests are the only testing — a passing
`test` run proves the build, so never add a `check` or build pass around a
listed `test`; and a listed example or app launch compiles its own target — do
not build or test first just to prove it builds.
</WritePromptContract>

<PhaseTeam>
Every implementation and fix dispatch runs **three delegates at
once**, never one. They share `${SESSION_DIR}` and `${WORKING_DIR}`, and each
occupies a fixed **slot** that names its artifacts and its board identity:

| Slot | Opens as | Owns |
| --- | --- | --- |
| `impl` | the phase's implementation or repair | the Work Order's production files |
| `test` | tests for the same specification | test targets under `tests/` and new test files |
| `review` | reading the spec and the tree cold | nothing; it is the team's reserve |

A slot is an identity and never changes. What a slot is *doing* is its **role**,
and roles move during a phase per <RoleReassignment/>. Everything downstream —
the board, the progress table, the review split — reads the slot for identity
and the role for activity, so keep the two distinct: `review` doing
implementation work is still slot `review`.

`test` opens against the **specification, not the implementation**. The Work
Order defines the behavior, so tests can be written before any of it exists;
a tester that waits for `impl` has converted a parallel team back into a queue.

**Only `impl` is given a `${PASS_KIND}`.** The recorder closes any open pass
when a new one starts, so three recorded passes would leave the ledger
describing whichever finished last and would corrupt the pass counts
`findings.py gate` uses to judge convergence. One dispatch, one recorded pass,
three agents inside it. This is the same reason <EarlyReviewArm/> defers its
reviewer's `start-pass`, applied to a wider team.

Launch all three in **one message** so they run concurrently, each with its own
prompt file and its slot as the ninth argument to `implement.sh`. Announce the
board path with the prompt and heartbeat paths, then apply <DispatchContract/>
once for the team: the progress timer covers the phase, not each member.

A member that finishes writes `impl_status_<slot>`; the phase is complete when
every slot has a terminal status, not when the first one lands. Reading one
slot's `implemented` as the phase's result is the same defect as reading a
completion notification as a finished assignment.
</PhaseTeam>

<CoordinationBoard>
The team coordinates through `${SESSION_DIR}/board.log`, written only with
`bash ~/.claude/scripts/delegate/board.sh`.

**Why a file even when messages work.** On the claude path every member is also
reachable by name — see <PhaseMesh/> — but the board still carries the record,
for three reasons. A post is a single broadcast that reaches both peers and the
wrapper at once, where addressed sends are N-1 separate deliveries that can each
fail and leave the team holding different pictures of one decision. The board
outlives a turn, so a member that starts late, or is resumed hours later, reads
the whole history rather than the messages that happened to arrive while it was
listening. And a message cannot make anything mutually exclusive: only the
token, taken with `mkdir`, decides who builds.

When `agents.conf` resolves the delegate family to codex there is no mesh at all
— a codex process has no reachable address, and the orchestrator is asleep
between progress ticks and cannot relay — so the board is then the only channel
that exists. Each `register` line says which case holds, in its `mesh=` field.

- `board.sh post <session_dir> <slot> <kind> <message>` — one broadcast line.
  Kinds are a closed set: `register`, `claim`, `release`, `ask`, `answer`,
  `status`, `blocked`, `handoff`, `done`.
- `board.sh read <session_dir> --since <cursor>` — everything new. Each line is
  numbered; keep the last number as the cursor. Read before every decision that
  depends on a peer, and always after acquiring a token.
- `board.sh role <session_dir> <slot> <impl|fix|test|review> [note]` — **call
  this the moment your slot starts doing something other than what it is named
  for.** A slot is a fixed identity and its role is not: a `review` slot
  recruited into writing is doing `impl`, and every slot converges on `review`
  at the end. The launcher stamps the role each slot opens in, so the progress
  table is never blank; after that, only this command keeps it true. Saying it
  in a `status` sentence does not count — the table reads the field, not prose.
  **Every call adds a row.** The progress table starts a new row for the round
  each time any slot changes role, so the reader watches `impl / test / test`
  become `impl / impl / test` and then `review / review / review`. A change you
  do not post is a shape the run never shows, and the row above it silently
  claims your old role held the whole time.
- A decision that is not on the board did not happen. Say it on the board first,
  then message a peer if it needs attention now.

Narration goes through the board too, not through prompt-formatted heartbeat
text: `board.sh post` takes the slot as a required argument, so attribution
cannot be dropped, where a name an agent is merely asked to prefix onto a
heartbeat line reliably goes missing.
</CoordinationBoard>

<PhaseMesh>
A member launched into the mesh is **addressable**: peers reach each other, the
orchestrator reaches any of them, and a claude member reaches the orchestrator —
mid-run, without waiting for a phase to end.

- **Addresses** are `<mesh_prefix>-<role>`, where the prefix is the session
  directory's basename: `phase3-a91c-impl`, `-test`, `-review`. A member is told
  its peers' names in its prompt, and every `register` line on the board repeats
  them in its `mesh=` field, so a member that missed the launch can still look
  one up.
- **How you reach a name depends on its family**, and the register line says
  which in its `reach=` field. Using the wrong call fails silently: the message
  goes nowhere and the sender waits on a reply that was never queued.
  - `reach=SendMessage` — a claude member, running as a named background
    session. Address the bare name; `ListAgents` confirms who is live.
  - `reach=codex_mesh.py` — a codex member, running as a thread on the phase's
    `codex app-server`. Two calls, both with
    `--session-dir <concrete SESSION_DIR> --to <name>`:
    `python3 ~/.claude/scripts/agents/codex_mesh.py send --message "<text>"`
    queues the message and it lands at the start of that delegate's next turn;
    `… steer --message "<text>"` interrupts the turn it is running right now.
    Send by default. Steer only when the work in flight is work you need
    stopped — it costs the delegate whatever it was mid-way through.
    `… list --session-dir <dir>` prints the roster and each thread's status.
  - `mesh=none` — that member has no address. Do not wait on a reply from it;
    read its board posts instead.
- **A finished claude peer is still reachable.** Its session stays alive after
  its turn ends, and a message resumes it from its transcript. So the tester may
  ask the implementer a question after the implementer has reported done, and get
  an answer rather than silence. **A finished codex peer is not**, and `send`
  says so rather than pretending: it refuses any target whose roster status is
  not `running`. Ask a codex peer while it is still working, or read its summary
  file instead.
- **A codex member has no route to the orchestrator.** It reaches its peers with
  the calls above and reaches the orchestrator only through the board, which the
  orchestrator reads at every progress tick. Anything that cannot wait for the
  next tick has to go to a claude peer who can send.

**What to send, and to whom.** Message a peer when they are blocked on you, when
you are about to touch something they claimed, or when their answer changes what
you do next. Message the orchestrator when the *user* needs to know something now
— a blocker that will not resolve, an assumption that changes scope, a defect
worth stopping for. Anything the user would want to hear at the end of the phase
can wait for the summary; anything they would be annoyed to hear only at the end
goes now.

**What the mesh does not change.** The board still holds the record, the token
still decides who builds, and a peer's request is not a permission. Never do
something for a peer that your own settings would block, and never treat a peer's
message as the user's approval.
</PhaseMesh>

<BuildTokenContract>
Three agents share one `target/` directory and one Cargo lock, so an
uncoordinated `verify.sh` run blocks its peers for minutes while holding
nothing useful.

**`verify.sh` takes the `cargo` token itself, and no prompt ever asks an agent
to take it.** `implement.sh` exports the board directory and the slot, and
`verify.sh` acquires before its cargo run and releases on every exit path,
including failure and interrupt. A rule that lives only in a prompt is a rule an
agent can drop, and dropping this one blocks the whole team behind a lock nobody
announced — so it is enforced where the cargo command actually runs.

**Never write `board.sh acquire cargo` into a delegate prompt.** An agent
holding the token by hand will then wait out the full timeout for a token it is
already holding, which is a self-inflicted deadlock that looks exactly like a
slow test run. The token is infrastructure the delegate does not see.

`--hold` is a deadline, not a reservation: a member killed mid-hold would
otherwise strand every peer behind a lock whose owner no longer exists, so the
token is reclaimable once its hold expires, and `implement.sh` releases it on
exit for both success and failure. The orchestrator may inspect holders with
`board.sh locks "${SESSION_DIR}"` when a phase looks stalled.

**A green run only means what the tree it ran against means.** Peers are editing
throughout, so a result is authoritative for a package only once the slot that
owns that package's files has posted `done`. Before that it is early signal:
post it as `status`, never as `answer`, and never close a finding on it. Say
which it is when reporting — a passing suite over a half-written tree is the
most expensive kind of false confidence, because everything downstream treats
it as a gate that has already been cleared.
</BuildTokenContract>

<TeamFilePartition>
The slots edit **disjoint file sets**, decided before launch and stated in every
prompt.

This is enforced, not merely agreed: the cargo-berth pre-edit hook claims paths
per session, each delegate is its own session, so an edit into a peer's claimed
file is **blocked** rather than merged. Two consequences that must reach the
prompts:

- The tester writes **integration tests under `tests/`**. A `#[cfg(test)]`
  module added inside a production file that `impl` has claimed is a blocked
  edit, not a merge conflict, and the tester will simply fail to write it.
- Any change that reaches outside one slot's file set — a signature both slots
  need, a shared helper, a new type two slots want — belongs to the slot that
  owns the file it lives in. Ask on the board and let the owner write it. Never
  weaken a fix to avoid the dependency, and never define the same type twice to
  route around a claim.

Where the Work Order's own files cannot be split — everything lands in one or
two files — say so in one line, give `impl` the whole set, and open `test` and
`review` on work that does not touch it. A partition that does not exist is not
worth inventing; a partition that is wrong costs the phase.
</TeamFilePartition>

<RoleReassignment>
Roles move; slots do not. Every move is a board `handoff` post naming the slot,
the role it is leaving, and the role it is taking, because that post is what the
progress table reads to say what each agent is doing now.

- **`impl` may recruit `review`.** When the implementation is wider than one
  writer, `impl` posts `ask` naming the disjoint file subset it wants taken;
  `review` answers and posts `handoff` to implementation work. The team is then
  two writers and a tester. `review` is the reserve precisely because it holds
  no files and can leave its lane without stranding anything.
- **`test` is never recruited away** while tests for the phase are unwritten.
  It is the only slot whose absence cannot be recovered later in the phase, and
  a phase that ships untested is not cheaper, only later.
- **When writing finishes before testing**, the writers do not idle. They take a
  disjoint slice of the remaining test work — agreed on the board, one file per
  slot, never the file `test` is inside — or they stand down.

**Standing down means exiting, not waiting.** A delegate is a one-shot session
with no idle loop: it ends as soon as it stops issuing tool calls, so there is
no such thing as a member that sits quietly and comes back when asked. A slot
with nothing left posts `done` with what it completed and finishes. Anything
else burns a live session on a poll loop that the team pays for and nobody
reads. This is why recruitment flows toward work that exists now rather than
work a peer might hand over later.
</RoleReassignment>

<TeamReview>
When a phase reaches review, all three slots review — but never as three
readings of the same question, which buys one opinion three times.

**One slot is adversarial, and it is a slot that did not write the code under
review.** Its brief is to break the change, not to check it: find the input that
violates a stated invariant, the caller that was not updated, the state the new
code cannot reach, the test that passes for the wrong reason. It reports the
concrete failing case, or reports plainly that it could not construct one.
Normally this is `review`; where `review` was recruited into implementation, it
is whichever slot wrote least of the code under review. An author is never the
adversary for its own file set.

**The other two take different aspects**, disjoint and named in their prompts:

- **Specification conformance** — does the change do what the Work Order says,
  including the parts no test covers, and does `${IMPL_SUMMARY}` describe what
  the diff actually contains.
- **Blast radius** — callers, consumers, public API, traits, registration,
  plugin wiring, invariants and transitions the change reaches without naming.

Each posts findings to the board under its own slot, so <Synthesize/> can tag
who caught what and can tell three independent findings from one finding found
three times. The adversarial verdict is reported even when it is empty: "no
failing case found" from a reader who was trying to build one is evidence, and
it is the only reading here that produces evidence by failing to find anything.
</TeamReview>

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
| one integration target alone | `bash ~/.claude/scripts/delegate/verify.sh test <package> <test>` |
| format + scoped lint | `bash ~/.claude/scripts/delegate/verify.sh lint <package>` |
| checkpoint format | `bash ~/.claude/scripts/delegate/verify.sh fmt <package>` |
| changed example | `bash ~/.claude/scripts/delegate/verify.sh example <package> <name>` |
| final workspace gate | `bash ~/.claude/scripts/delegate/verify.sh final` |

Rules:

- **Every line in this table serializes against the phase's peers on its own**,
  per <BuildTokenContract/>: `verify.sh` takes the `cargo` token before running
  and releases it after, so a prompt neither mentions the token nor takes it. A
  run may therefore wait for a peer before starting. A result is authoritative
  for a package only once the slot owning that package's files has posted
  `done`; before that it is early signal, not a gate.
- `check` is optional feedback, not a gate. Every modified package gets `test`
  and `lint`; trace changed public APIs, traits, registration, and plugin wiring
  to modified callers. `test <package>` already runs that package's integration
  targets, so name one explicitly only to re-run it alone. Add example lines
  only when the phase owns them.
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
- `verify.sh lint` and `fmt` honor `~/.claude/config/lint.conf`. A printed `SKIPPED` is
  skipped, not passed; do not bypass a disabled check manually.
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
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <delegate\|main\|both> [--detail <d>]` | create an id |
| `status` | read the ledger for closure review |
| `gate` | get `converged` or `dispatch`, plus batch and any advisory |
| `dispatch --covers F001,F002,...` | record one complete repair batch |
| `abandon --reason <r> [--edits-landed]` | a dispatched repair died; reopen its batch |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | record closure evidence |

The script owns batching, not permission: first round gates blockers and minors;
later rounds gate blockers; nits never gate. It rejects partial batches, so a
round always closes everything currently on the ledger. `start-phase` resets it.

**The gate never stops the run.** It answers `converged` or `dispatch` and
nothing else. Where it once stopped — a finding that failed to close repeatedly,
a gating count that will not come down, a spent repair budget, a repeated pass
shape, repeated blind-review cancellations, the runaway backstop — it now returns
that sentence in `advisory` beside a `dispatch` verdict, and the round runs.
There is no override to record, because there is nothing to override.

An advisory is not a gate and never becomes one: do not treat it as a reason to
stop, to ask permission, or to re-open a decision the user has already made about
this run. It is a fact worth passing along, so **report it in one line whenever
one is present** — say the pattern in ordinary words alongside the repair being
dispatched, and continue. The user watches the shape of the phase and stops it
themselves if it needs stopping; that judgment is theirs, and the whole point of
reporting is to let them make it early. Every limit that decides when an advisory
is worth printing lives in `~/.claude/config/delegate.conf`; never edit that file
mid-run to change what gets said.

**Dispatching a repair fixes nothing.** `dispatch` leaves its batch
`repair_in_flight`, and exactly two things resolve that state: `implement.sh`
records `landed` when its worker exits cleanly, and `abandon --reason "<how it
ended>"` reopens the batch when the repair died instead. Both `gate` and `verdict`
refuse a finding still in flight, so a repair that never finished cannot reach a
reviewer pre-labelled as fixed and be confirmed on that label alone.

The main agent owns `abandon` and nothing else here — `landed` belongs to the
launcher, the only party that watches the worker exit. Whenever a fix dispatch
ends without `impl_status` reaching `implemented` — the user stopped it, the
process was killed, the session was interrupted — run `abandon` before any other
workflow step, and say in one line what died and that the findings are open again.
Pass `--edits-landed` only when repair edits are actually in the tree; without it
the attempt is refunded, because a repair that never ran must not spend the budget
that decides when this phase stops.
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

A dead launcher usually leaves two records open, not one. If it was a fix
dispatch, its findings are still `repair_in_flight` for the same reason its pass
is still open — the process died before anything observed how it ended — so
`findings.py abandon` per <FindingsLedger/> belongs beside this call.

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
in `~/.claude/config/delegate.conf`. This is the Claude timer delay and Codex
poll timeout. There is no default: if the key is missing or is not a positive
integer, stop and tell the user to set it rather than picking a value.

Claude: while any work is running and progress is enabled, keep exactly one
one-shot timer in a managed background terminal. Launch:

`bash ~/.claude/scripts/delegate/progress_timer.sh "${SESSION_DIR}" "${PROGRESS_INTERVAL_SECONDS}"`

Save its handle and end the turn normally. The timer contains no loop and runs
no agent. Use the script rather than a bare `sleep`: it records the armed
deadline in `${SESSION_DIR}/progress_timer` and clears it on exit, which is what
lets the Stop hook tell an armed timer from none at all. Codex never launches
this timer; a <CodexDispatchWait/> timeout is its progress tick.

**Never end a turn that leaves work running without an armed timer.** Running
work is a live launcher, a background `verify.sh final`, or any main-agent run
that opened a progress window. A registered Stop hook enforces this and blocks
once; treat that block as a dropped timer, not as a prompt to argue.

The reported window is a pass when a launcher owns the work and an **activity**
when the main agent runs it itself -- verification, smoke, style. `verify.sh`
opens and closes its own activity whenever `PLAN_DELEGATE_SESSION_DIR` is set;
open one by hand for other main-agent work with
`progress_history.py start-activity --session-dir "${SESSION_DIR}" --label <label> --activity <what>`
and close it with
`finish-activity --session-dir "${SESSION_DIR}" --status <status> --result <outcome>`.
Keep `--label` to one or two words -- it is the row's name in the round table --
and make `--result` the short outcome that row should show: `pass`, `clean`,
`no change`. Without one the row can only say `done`, which reports that the
window closed rather than what it found. Activities sit in that table beside
passes and are invisible to `findings.py`, so they never touch convergence
counting -- which is exactly why <PassOwnership/> forbids faking a pass for the
same purpose.

On a Claude timer notification or Codex poll timeout:

1. Check launcher state first. For Codex, `exit_code` alone marks terminal
   completion; a returned `session_id` without it remains active. If no dispatch
   remains active, emit no stale report and process completion. On Claude, also
   stop and clear any timer when its dispatch completes first.
2. Read the current Work Order and verification list, the latest relevant
   heartbeat lines, `board.sh read "${SESSION_DIR}" --since <cursor>` for what
   the team settled since the last tick, `git status --short`, and
   `git diff --stat` in `${WORKING_DIR}`. Keep the board cursor across ticks.
   Compare status with the phase baseline; include untracked paths without
   changing the index. The board is where a `handoff` appears, so it is what
   tells the report which slot is doing which role right now.
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
4. Apply <EarlyReviewArm/>: the evidence steps 2-3 just gathered is its input,
   and this tick is its only trigger point. It runs **before** the recorder,
   never after, so that when it does launch a reviewer the round table this
   tick is about to print already shows it working.
5. Run:

   `python3 ~/.claude/scripts/delegate/progress_history.py calibrate --session-dir "${SESSION_DIR}" --candidate-percent "${PHASE_RAW_PERCENT}"`

   Use its phase suggestion when applicable; otherwise keep the raw value. Then
   run:

   `python3 ~/.claude/scripts/delegate/progress_history.py progress --session-dir "${SESSION_DIR}" --project-raw-percent "${PROJECT_RAW_PERCENT}" --project-percent "${PROJECT_RAW_PERCENT}" --phase-raw-percent "${PHASE_RAW_PERCENT}" --phase-percent "${PHASE_REPORTED_PERCENT}" --cap-stage "<stage>" --activity "<current activity>" [--phase-override-reason "<specific evidence>"]`

   Include the override reason only when rejecting an applicable calibrated
   value. The recorder refreshes any legacy run whose project clock was not
   script-resolved. Copy the resulting Markdown header exactly, both tables and
   the delegates line included: the first table carries the project and phase
   clocks, the second one row per round — implementation, then each fix — with a
   column per team slot naming the role that delegate is filling and how long it
   has been at it, a further row each time the seats change role, and a row
   apiece for each main-agent activity, in the order they ran. The line under it
   names the delegate sitting in each seat. The second table is the one a reader
   scans to see who is on what; dropping it leaves them the timings with no way
   to tell the agents apart. Durations below one day are
   always `HH:MM:SS`; longer durations are `<days> day(s) HH:MM:SS`, and the
   `ETA`, `ETA low`, and `ETA high` columns are arrival times rather than
   durations — the two band columns each carry their own distance from the ETA
   in parentheses as `(-HH:MM)` and `(+HH:MM)`. The line above the first table
   names the worktree, the branch, and the phase's position in the plan. Its
   last line is the wall clock — `now` and the next report time, both computed
   by the recorder from the same interval the timer uses. Never write, adjust,
   or drop that line, reorder a table, or edit a cell by hand.

   Two stage rows read `running` at once only after step 4 armed an early
   reviewer, and that is the table's whole point there: the writer and the
   reviewer are working at the same time, and the reviewer's row says
   `running (early)` because it started on a diff that was not finished yet.
   Say so in the prose below rather than leaving the reader to infer it from
   two rows that both look live.
6. Add two or three ordinary-English sentences covering current activity,
   material work now present, and what remains.

   **Open by saying what this phase gives the person using the tool, then report
   the movement on it.** Under <UserFacingText/> this report is read cold: the
   user is doing something else, reads one update out of a long scroll, and
   reads it the morning after. It re-orients every time, and it never spends the
   design's own vocabulary — `edge`, `ancestry`, `stale marker`, a type name, the
   plan's name for a subsystem — on a reader who has not opened the plan doc.
   Those words look like English from inside the work, which is why they slip
   through; that file's banned list covers them.

   One topic per sentence, no sentence carrying more than two clauses. When a
   topic holds more than two items, give the count and what they have in common
   rather than chaining them; the same ceiling applies to lists of tests, files,
   or checks. An accurate sentence the user has to read twice has failed at its
   one job.

   Not this:

   ```text
   The repair pass has landed its production changes: an edge whose successor
   has already ended now reports as ended rather than still waiting on its
   predecessor, ancestry questions are narrowed to the reservations that
   actually have something ordered after them, and an unrelated missing commit
   no longer poisons a whole batch of ancestry answers.
   ```

   This:

   ```text
   This phase lets one worktree's work be ordered behind another's — start here
   only once that branch is done. The repair pass fixed four cases where the
   tool gave the wrong answer about whether the waiting side was still blocked.
   Tests for that ordering come next, then verification.
   ```

   Do not paste logs or filenames. Never quantify the work in lines of code,
   insertions, or file counts: the user can already see the diff, so a line
   total displaces the one thing only the reporter knows — what the code now
   does.
7. If the dispatch remains active, Claude reads the interval again, launches a
   fresh one-shot timer, replaces the handle, and ends the turn. Codex returns
   immediately to <CodexDispatchWait/> on the same session and reads the
   interval again before polling.

**An armed timer is never a substitute for the report.** Every turn that arms or
re-arms a timer emits steps 1-6 first — both tables and the wall-clock line —
and a bare "timer re-armed" line is a dropped report, not a short one. This
matters most where it is easiest to skip: a Stop-hook block reads as a
mechanical complaint about a missing file, so the reflex is to relaunch the
script and end the turn. But the hook fires on the turn the user was owed an
update and did not get one, and the timer file is only how it noticed. Re-arming
without reporting answers the hook and leaves the user exactly where they were.
Steps 1-6 are cheap: the recorder emits both tables, and the prose is three
sentences.

A user-requested status check performs steps 1-6 immediately. A question about
work already finished — how many fix passes there have been, how long a review
took, what an earlier phase ran — is answered by

`python3 ~/.claude/scripts/delegate/progress_history.py timeline --session-dir "${SESSION_DIR}" [--phase <id>]`

which renders one row per pass -- with the agent that ran each -- for one phase
or for every phase of the run.
Read the answer from it rather than counting passes from memory or grepping the
event stream by hand. If the user stops
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
required gate that cannot run, or delegate/environment error. It may also stop
at a phase or auto-window boundary for <ConsiderNextItems/> approval, and only
for that step's `gate` proposals — its `apply` ones are written and reported,
never asked. The findings ledger is not on this list and never joins it: a
convergence advisory is reported and the round runs. Everything else
auto-routes, resequences, or defers. Verbose adds only its authorization gates.
</AuthorizationContract>

<BriefingFreshness>
A phase is freshly briefed when the user received either its complete
<PhaseBriefing/> or the complete <CombinedWindowBriefing/> for an auto range
containing it in the current uninterrupted pre-phase review sequence, every
pending decision and user amendment was surfaced and resolved, and no later
edit changed its behavior, scope, files, or verification. Sequential individual
briefings count; they need not appear in one batch message. A follow-up question
or explanation does not stale a briefing. Recording an accepted decision that
the briefing and discussion already described does not stale it.

When an auto control arrives and every covered phase is freshly briefed, that
control is the batch approval: set the approved `AUTO_WINDOW` and proceed to
<CoordinateDelegatedPhaseReservation/> without repeating briefings or asking for
`proceed`. If any
covered phase is unbriefed, stale, or has an unresolved decision, route to
<AutoWindowBatchBriefing/>. Compressed rows and phase titles are not briefings.
</BriefingFreshness>

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

Apply <CoreContract/>, <CompactionContract/>, <UserFacingText/>, and
<VerificationNarration/> throughout.

1. <PrepareSession/>
2. <ComposeWorkOrder/>
3. <ResolveStyleDiffBase/>
4. <VerbosePrePhaseGate/> when required
5. <CoordinateDelegatedPhaseReservation/>
6. <LaunchImplementation/>
7. <DualReview/>
8. <Synthesize/>
9. <RunApplicationSmokeTest/>
10. <RunProjectStyleReview/> — `single` only; loop and verbose run the run's one
    style review from <FinalGate/> after the whole plan is green
11. <RunPhaseReview/>
12. <RunPhaseShrink/>
13. <ConsiderNextItems/>
14. <CheckpointCommit/>
15. <DiscardPhaseReviewText/>
16. <RecordPhaseCompletion/>
17. <VerbosePostPhaseReport/> and applicable <VerbosePostPhaseGate/>
18. <NextPhase/> or <RunSummary/>
</ExecutionSteps>

<PrepareSession>
Run `bash ~/.claude/scripts/delegate/prepare_session.sh` under
<ToolingContract/>. Capture `SESSION_DIR` and set `WORKING_DIR`. The script also
creates the run-active marker; every exit must eventually run `end_session.sh`
through <RunSummary/> or single-mode completion.
</PrepareSession>

<ComposeWorkOrder>
1. For a phased plan, validate the target Work Order before reading any of its
   fields:

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.work_order \
     --repository-root "${WORKING_DIR}" validate --document "${PLAN_DOC}" \
     --phase <target-phase>
   ```

   The tagged output is authoritative for complete Goal/Spec/Files structure
   and lexical paths. Work Orders do not declare reservations; the edit hook
   claims exact paths on first touch.

   Then scan the target Work Order for `**Pending decision:**`.
   Verify cited code still matches the block. **Re-test the block against
   <DecisionRouting/> before presenting it** — a block is a claim that a decision
   is the user's, not proof of it, and the pass that wrote it may have been wrong
   or may have been overtaken by later phases. If the two options differ only in
   how committed work is packaged — phase count, boundaries, ordering, numbering,
   ownership of a task — resolve it yourself under <DecisionEconomy/>, edit the
   resolution in, delete the block, state the call in one line, and do not stop.
   If it is genuinely the user's, present it, apply
   <ExplainOnDemand/> when needed, edit the resolution into Spec/Files/gate, and
   remove the block before continuing. A resolution introduces behavior nothing
   else audits — `/plan:phase_review`'s `<StateAndConsequenceAudit/>` inspects
   only what a phase already shipped — so run that audit against the resolution
   here and state its destination and owner alongside it. An in-repository
   destination is the Spec/Files/gate edit already being made. A destination in
   another repository goes to the next-items file derived in step 5, and only
   with the user's approval; never append to it automatically.
   After editing a resolution into Spec, Files, or the acceptance gate, rerun
   the shared validation above before continuing. A validation failure blocks
   dispatch.
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
  <RunProjectStyleReview/>.
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
`AUTO_WINDOW` and run <AutoWindowBatchBriefing/> before
<CoordinateDelegatedPhaseReservation/>. Otherwise
follow <AuthorizationContract/>.
</ComposeWorkOrder>

<ResolveStyleDiffBase>
Loop and verbose only, once per run, before the first dispatch. `single` skips
it and never sets a base: it commits nothing, so <RunProjectStyleReview/> reads
the working tree directly.

1. If `${SESSION_DIR}/style_diff_base` exists, restore `STYLE_DIFF_BASE` from it
   and return. Later phases never re-resolve the base.
2. Take `<plan-slug>` as the normalized stem derived in <ComposeWorkOrder/>
   step 5 without its `-next.md` suffix — the same slug <CheckpointCommit/>
   writes into every checkpoint subject. Run:

   ```sh
   bash ~/.claude/scripts/delegate/style_branch.sh resolve "${WORKING_DIR}" <plan-slug>
   ```

   Its `project_base` is the parent of this plan's first checkpoint commit, or
   current HEAD when the plan has not checkpointed yet. That base spans a
   project resumed across several runs and still excludes commits the branch
   already carried. Any status other than `ok` records no base: report the
   reason in one line, leave `STYLE_DIFF_BASE` empty, and continue.
3. `purpose_built=true` needs no user decision. Persist `project_base` to
   `${SESSION_DIR}/style_diff_base`, name the branch and how many commits the
   end-of-run style review will therefore cover in one line, and continue.
4. `purpose_built=false` means HEAD is detached or sits on the default branch,
   so the run would checkpoint onto a branch it does not own. Ask exactly once
   and dispatch nothing until it is answered:

   ```
   Each phase checkpoints, and the project-end style review diffs the branch to reach that committed work. Currently <reason>, so this project has no branch of its own to diff. Reply \`branch\` to create \`<suggested_branch>\` here and run on it, \`branch <name>\` to choose the name, or \`stay\` to keep this position and accept that anything else committed here lands in the same style diff.
   ```

   A `branch` answer runs

   ```sh
   bash ~/.claude/scripts/delegate/style_branch.sh create "${WORKING_DIR}" <name>
   ```

   and persists the `project_base` it returns. A non-`ok` status reports its
   reason and re-asks; never create a differently named branch on its own
   initiative, and never move onto an existing one. `stay` persists the
   `project_base` already resolved and says in one line where the style diff
   will start and that unrelated commits landing here join it.
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

These are
`DelegatedPhaseReservationState::{RepositoryNotEnrolled,
EnrolledAwaitingFirstTouch, Active(ActivePhaseReservation),
CheckpointCommittedAwaitingReleaseConfirmation(
CheckpointReleaseConfirmationPending)}`. Do not represent any of them as an
absent record or a bare optional value. Only `Active` and
`CheckpointCommittedAwaitingReleaseConfirmation` supply a release argument.
The engine's harness-session mapping is a disposable edit-authorization
projection: another claim in the same harness session replaces it, and no
command reads a reservation id back from it.

`ActivePhaseReservation` is orchestration memory. The registered edit hook
creates it only from a validated clear-check result whose acquisition is
`appended` or `already_held`, copying the returned reservation and
coordination-run ids, phase, and protected phase-start HEAD. Never reconstruct
it from a marker, rendered prose, or conversation.

`CheckpointReleaseConfirmationPending` is durable proof that the checkpoint
commit succeeded and only release confirmation remains. Create it only in
<CheckpointCommit/> step 6 by atomically replacing `Active` after that step's
commit succeeds, copying the active reservation fields and the full object id
captured immediately from that successful commit. Never create or reconstruct
it from conversation or from `git rev-parse HEAD` read at a later time. Delete
this pending record only after <CheckpointCommit/> validates a successful
release.

An unsuccessful ending applies <RetainDelegatedPhaseReservation/>. This includes
a cancelled no-diff phase, dispatch or run error, failed checkpoint commit,
every `single` run including a dirty one, user stop, drift refusal, busy ledger,
and failed release. Never invoke release from an error handler, cancellation
path, single-mode completion, run summary, or session cleanup.
</DelegatedPhaseReservationContract>

<RetainDelegatedPhaseReservation>
If the durable state is `Active` or
`CheckpointCommittedAwaitingReleaseConfirmation`, leave both the engine
reservation and `${SESSION_DIR}/delegated_phase_reservation_state.json` intact.
Report that the phase stopped before its checkpoint release could be confirmed
and name the recovery action that stopped; retain the reservation id in
durable/internal recovery output even when ordinary user-facing prose omits
tooling ids. This retention is still required when `release` may have died after
appending its checkpoint: that post-append death leaves
`CheckpointCommittedAwaitingReleaseConfirmation`, and only
<CheckpointCommit/> step 7 may later confirm that journalled success from
matching outstanding evidence and delete the record. A state file that is
absent after a validated claim, malformed, names another unfinished phase, or
disagrees with a validated claim is lifecycle loss, not non-enrollment: stop and
report it. Absence before the coordination boundary has produced a state is
ordinary and must not be misreported as loss. `RepositoryNotEnrolled` and
`EnrolledAwaitingFirstTouch` require no engine release.
</RetainDelegatedPhaseReservation>

<CoordinateDelegatedPhaseReservation>
Run after phase authorization and task selection, immediately before
<LaunchImplementation/>. It is the only pre-dispatch coordination boundary.
Do not dispatch until this contract reaches one of its four persisted states.

0. On resume, read the state file first. A valid state for the current phase is
   authoritative: `Active` resumes without another claim, and either inactive
   state resumes without an engine mutation. `EnrolledAwaitingFirstTouch`
   resumes with the registered edit hook still responsible for acquisition.
   `CheckpointCommittedAwaitingReleaseConfirmation` for the current phase means
   the checkpoint already committed and only its release confirmation remains:
   resume at <CheckpointCommit/> step 7 without re-committing, re-claiming, or
   dispatching. `Active` or
   `CheckpointCommittedAwaitingReleaseConfirmation` for another unfinished
   phase is a stranded reservation and stops the new dispatch. A completed phase
   removes its inactive state under <RecordPhaseCompletion/>, so no old inactive
   tag is silently reused for a new phase.
1. When no state exists yet, invoke the shared `/sync board` entry point once:

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state board \
     --cwd "${WORKING_DIR}"
   ```

   `unconfigured` persists `RepositoryNotEnrolled` and proceeds silently.
   `ledger_unreadable` stops the run; it is never
   opt-out. `busy` stops with “the ledger is busy, try again” and the exact board
   command to rerun; do not retry. `board_ready` persists
   `EnrolledAwaitingFirstTouch` and proceeds without claiming predicted paths.
2. The registered PreToolUse edit hook owns the next transition. A clear check
   atomically acquires exact `file:` scopes before the edit, then replaces
   `EnrolledAwaitingFirstTouch` with `Active` from the returned facts. A blocked
   check refuses the edit and leaves the pending state intact. An unreadable
   ledger stops without facts. Do not call `cargo-berth claim` on behalf of a
   Work Order.
</CoordinateDelegatedPhaseReservation>

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
say explicitly when no load-bearing type is specified. Write every cell under
<TypeTableCells/>.
</PhaseBriefing>

<TypeTableCells>
Governs the `Planned role` and `System relationship` cells in every types table
— phase briefing, window briefing, and completion report alike.

The `Type / trait / API` cell is the only place a code identifier belongs. The
other two are written for someone who has never opened the file and never will:
name the thing in ordinary words — a tag, a list, a rule, the box around the
members, the step that copies it — and say what it does, or what breaks without
it.

Two tests every cell must pass:

- **Say it out loud.** Could you read the cell to someone watching the running
  application and have them follow it? "Durable back-reference from an instance
  shell to the registered Look definition it was built from" fails. "A tag on
  each Look saying which of the seven it came from" passes.
- **Name the consequence.** A cell that states a mechanism and stops makes the
  reader work out why it matters. "Copied onto duplicates by the new integration
  point" fails — copied by what, and what happens if it is not? "Duplicating has
  to copy it deliberately, because the duplicator copies wiring but no tags"
  passes.

Three specific failures, each fluent English that informs nobody:

- A code identifier used as a noun where an ordinary word exists — *the shell*,
  *the definition*, *the publication*.
- A noun phrase compounded from three or more plan terms, such as naming a
  transaction by the three steps it performs.
- Plan-internal vocabulary the user has never been shown: gate ids, phase
  numbers as adjectives, *promotion*, *staging*, *additive*, *erased*.

One sentence per cell is the target and two the ceiling, but length is not the
constraint — a cell needing a clause of context gets it. Terseness bought by
compressing plan terms into a noun phrase is the failure this contract exists to
stop.
</TypeTableCells>

<CombinedWindowBriefing>
For an auto window, build one high-level preview from Delegation Context, every
covered Work Order, and command-line amendments:

```
## Phases N–M ready — <one outcome-oriented title>

### Overview
[succinct human-readable explanation of what the window accomplishes as one
piece of work, why these phases belong together, and the deliberate boundary]

### Phase summaries
- **Phase N — <title>:** [succinct purpose, dependency, behavior, and deliberate
  exclusion]
- **Phase N+1 — <title>:** [...]

### Important types and APIs
| Phase | Type / trait / API | Status | Planned role | System relationship |
| --- | --- | --- | --- | --- |

### Files and verification
[one combined module/package and acceptance summary; name per-phase differences
only where they matter]

### Wrap-up
[succinct statement of the resulting capability, what remains outside this
window, and why the window can run without an intermediate stop]
```

Keep the overview and phase summaries behavioral and high-level; do not replay
each Work Order. The complete preview must still preserve the load-bearing
state transitions, ownership, visible effects, dependencies, and exclusions
the user needs to authorize the range.

Table rows include only load-bearing types explicitly named by the applicable
Work Order. Use status `New`, `Existing - Changes`, or
`Existing - No Changes`, inferred from that Work Order without code research.
Order rows by editing sequence: covered phase order first, then the order each
type is first introduced or changed within that phase. Never alphabetize the
table or regroup it by crate. Mark genuine uncertainty instead of guessing;
say explicitly when the window specifies no load-bearing type. Write every cell
under <TypeTableCells/>.

Keep `### Wrap-up` short. It synthesizes the authorization boundary; it does not
repeat the overview, phase summaries, table, or verification section.
</CombinedWindowBriefing>

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
<CoordinateDelegatedPhaseReservation/> without another briefing or gate.
Otherwise read every Work Order
now and emit one complete <CombinedWindowBriefing/> for the covered range;
surface any pending decision. Ask:

`Run phases <list> without stopping? Reply \`proceed\` to authorize all of them, \`proceed phase N\` to authorize only phase N and re-gate after it, or \`stop\`.`

Approval runs the resolved range without intermediate gates. Narrowing updates
`AUTO_WINDOW`; questions preserve the batch gate. The full combined preview,
not a phase-title list or type table alone, owns batch authorization.
</AutoWindowBatchBriefing>

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
3. Set `${PASS_KIND}=impl`. Every implementation pass is `impl`, however
   ambiguous the architecture or hard the mathematics: there is no escalation
   task and no escalated kind. `~/.claude/config/agents.conf` owns delegate
   family/model/effort, one row per kind. State the kind in the dispatch update.
4. Partition the Work Order's files per <TeamFilePartition/> and write one
   prompt per slot under <WritePromptContract/>:
   `${SESSION_DIR}/implementation_prompt.md` for `impl`, `test_prompt.md` for
   `test`, and `review_prompt_team.md` for `review`.
5. Launch all three in one message, `impl` first, each under <ToolingContract/>:

   ```sh
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/implementation_prompt.md" impl \
     "<responsibility>" "${PASS_KIND}" "<activity>" 0 impl
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/test_prompt.md" test \
     "<responsibility>" test "<activity>" 0 test
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/review_prompt_team.md" review \
     "<responsibility>" review "<activity>" 0 review
   ```

   Responsibility follows <ProgressContract/>. **All three seats carry a pass
   kind**, so a team phase records three passes and stops being attributed to one
   agent. The kind is the work the seat was assigned and nothing more — it names,
   it never triggers, so a seat never misreports its work to avoid a side effect.
   **Task and kind are the same word** on every seat: the fourth argument selects
   the agent and the sixth records the pass, and both say what this seat is
   doing. The vocabulary is `impl`, `test`, `fix`, `review` — nothing else
   resolves, in `agents.conf` or in the ledger.
6. Announce prompt, board, and heartbeat paths, set `EARLY_REVIEW=none`, then
   apply <DispatchContract/> once for the whole team.
7. On completion, read `impl_status_impl`, `impl_status_test`, and
   `impl_status_review`; the phase is done only when all three are terminal.
   `implemented` on `impl` loads `impl_summary_impl.txt` into
   `${IMPL_SUMMARY}`; read the other two summaries for what they completed and
   for findings they posted. If `impl` errors, cancel any early-launched
   reviewer per <EarlyReviewArm/>, apply <RetainDelegatedPhaseReservation/>,
   report `impl_agent_impl.log`, record
   `finish-run --status error`, run `end_session.sh`, and stop; multi-phase runs
   also emit <RunSummary/>.
8. `implemented` is the delegate's claim, not a passed gate. Read
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

<EarlyReviewArm>
Launch the blind reviewer while the writer is still running, so both finish
together instead of back to back. Evaluated only on a <ProgressContract/> tick,
and only when all of these hold: an implementation or fix dispatch is active;
`EARLY_REVIEW=none`; and the completed dispatch would receive a delegate
review — <DualReview/> pass 1 or a closure review. A behavior-preserving repair
never arms — documentation, formatting, lint guidance, an agreed trivial rename —
and neither does a repair whose batch sits in paths narrow enough that
<FixDispatch/> will close it on a contained diff. That judgment is the
orchestrator's own reading of the batch; no task name carries it.

Estimate the **pass-internal** completion of the running dispatch — not the
capped phase percentage — from the tick's evidence. It is ≥75% when the
heartbeat shows the delegate running its listed verification commands, or the
diff already covers essentially all Work Order (or fix batch) files. Below
75%, or when genuinely unsure, do nothing; the synchronous path still exists.

**Also require at least ten minutes of writer time still to run.** The whole
saving here is the reviewer's reading, and it can only read while the writer is
still writing — a reviewer armed four minutes before delivery has time to open
the specification and nothing else, while still carrying every failure mode this
section exists to guard. Estimate the remainder from the same tick evidence: how
long this pass has already run against the completion estimate. Ten minutes
remaining at 75% means a pass of roughly forty minutes or longer, so most
implementations and nearly every repair arm nothing and review synchronously.
That is the intended outcome, not a missed opportunity. When the two conditions
disagree, or the remainder is a guess, do nothing.

At ≥75% with ten or more minutes left, in the same tick:

1. Increment `${REVIEW_PASS}` now; <DualReview/> will not increment it again.
2. **Delete every stale delivery artifact and prove they are gone.**
   `${SESSION_DIR}` spans the whole run but `${REVIEW_PASS}` resets with every
   phase, so earlier phases' `final_diff_*.diff` and `final_diff_*.ready` are
   already on disk under the exact names this phase's launches will poll.
   `rm -f "${SESSION_DIR}"/final_diff_*.diff "${SESSION_DIR}"/final_diff_*.ready`
   — the whole glob, not just the current index, because later passes in this
   phase collide the same way — then confirm no sentinel remains before
   continuing. Skipping this does not fail loudly, and it does two separate
   kinds of damage. The reviewer's poll succeeds instantly, so it reads a
   previous phase's diff as if it were this phase's finished code and returns a
   confident, entirely false blocker saying the phase implemented nothing. And
   because the sentinel is what releases `review.sh` to call `start-pass`, the
   review pass opens while the implementation is still running: the recorder
   closes the live implementation pass as `interrupted`, and every later
   `progress` call in that phase is refused for having no active window. The
   delivery step below then overwrites the diff a reviewer has already read.
3. Apply <ReviewDiffContract/> to the current partial tree. The delegate has
   named no created files yet, so that check is vacuous; the snapshot is
   expected to be incomplete.
4. Write the applicable early-form prompt — <BroadReviewPrompt/> for pass 1,
   <ClosureReview/> for a fix — including the completion estimate, the partial
   diff, and the exact final-diff and ready-sentinel paths below.
5. Launch `review.sh` exactly as <DualReview/> step 3 does, appending one extra
   final argument: `${SESSION_DIR}/final_diff_${REVIEW_PASS}.ready`. Save the
   handle as `${REVIEW_DISPATCH_HANDLE}` and set `EARLY_REVIEW=launched`. Do not
   disturb `${DISPATCH_HANDLE}` or the tick's timer re-arm.
6. Put the reviewer in the round table, so the tick's report shows two agents
   working rather than one:

   `python3 ~/.claude/scripts/delegate/progress_history.py arm-review --session-dir "${SESSION_DIR}" --activity "<what this reviewer is checking>" --called-task delegate.review`

   Run it before the recorder call in <ProgressContract/> step 5, which is what
   prints the table. The command opens no pass and writes no pass event, so it
   cannot forge anything convergence counts — it is a presentation marker and
   nothing else. It resolves the reviewer's agent from the same registry
   `review.sh` uses, and retires its own row when `review.sh` reports an error
   or its process is gone, so the table never shows a reviewer that stopped
   working. The real review pass, recorded by `review.sh` when the ready
   sentinel releases it, supersedes the marker automatically.

   A one-line announcement is no longer the delivery here. The two running rows
   are, and they carry what a sentence cannot: which agent each one is, when it
   started, and how long it has been going.

The extra argument makes `review.sh` defer its `start-pass` until the sentinel
appears, so the implementation pass and review pass never overlap in the
recorder. At most one early launch per dispatch. When the primary dispatch
completes first — before any tick reaches 75% — review runs synchronously as
before; early launch is opportunistic, never required.

**Delivery.** When the primary dispatch completes, after
<LaunchImplementation/> step 7, write the final diff to
`${SESSION_DIR}/final_diff_${REVIEW_PASS}.diff` (for a closure review, limited
to its paths per <ClosureReview/>), then create
`${SESSION_DIR}/final_diff_${REVIEW_PASS}.ready`. Never create the sentinel
before the diff is fully written.

**Cancellation.** If the primary dispatch errors or the run stops before
delivery, kill the early reviewer. If the ready sentinel exists, close its
pass with `finish-pass --status canceled --orphaned-launcher` per
<PassOwnership/>; before the sentinel no pass was recorded, so record nothing —
a pre-sentinel kill counts toward no advisory, including the blind-review
cancellation one.

**Reviewer error before delivery.** If the early reviewer itself errors while
the primary dispatch is still running, report it in one line, clear
`${REVIEW_DISPATCH_HANDLE}`, and set `EARLY_REVIEW=none`; the primary dispatch
continues and its review runs synchronously at completion under the next
`${REVIEW_PASS}` index. The errored pass's numbered artifacts remain.

**Verdict before delivery is void.** An early reviewer that returns findings
while the primary dispatch is still running reviewed something other than this
phase's finished code — it cannot have read a diff that does not exist yet.
Discard its findings entirely rather than reading them as evidence: open nothing
in the ledger, preempt nothing, and never route its blockers into a fix
dispatch. Treat it exactly as a reviewer error before delivery, and say in one
line that the review is being discarded and why. A void verdict is often
fluent and specific — a stale diff supports confident claims about missing work
— so the check is the timing, never how convincing the text reads.

**Every path above that ends an early launch before its real pass starts also
drops the row it was given** — cancellation, a reviewer error before delivery,
and a void verdict alike:

`python3 ~/.claude/scripts/delegate/progress_history.py disarm-review --session-dir "${SESSION_DIR}" --reason "<canceled|reviewer error|void verdict>"`

Run it alongside clearing `${REVIEW_DISPATCH_HANDLE}` and `EARLY_REVIEW`. The
row would retire itself at the next report anyway; this is how the reason
reaches the record, and it is what keeps that report from having to guess.
</EarlyReviewArm>

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

**Early form** (an <EarlyReviewArm/> launch only): the `## Diff` section holds
the partial diff at launch, and an `## Implementation status` section is
inserted before it:

```
## Implementation status

Implementation is estimated ~N% complete and still running; the diff below is
a partial snapshot. Work in two stages.

Stage 1 — arm, now: read the specification, the partial diff, and the
surrounding code, callers, and consumers it touches. Draft provisional
findings. Emit no verdict.

Stage 2 — fire: poll for <ready sentinel path> (e.g. `test -e`, sleeping ~15s
between checks), narrating each wait as one short output line. When it
appears, read <final diff path> — it supersedes the partial snapshot — and
review it in full, concentrating on hunks that changed since the snapshot.
Reconcile your provisional findings against the final diff; drop any the final
code resolves. Then answer the Review Questions and emit the normal numbered
findings and verdict from the final diff only. If the sentinel has not
appeared after 30 minutes, report that timeout and exit with an error instead
of reviewing the partial diff.
```
</BroadReviewPrompt>

<DualReview>
1. If `EARLY_REVIEW=launched`, the reviewer is already armed and
   `${REVIEW_PASS}` already incremented: apply <ReviewDiffContract/> to the
   completed tree, deliver the final diff and ready sentinel per
   <EarlyReviewArm/>, reset `EARLY_REVIEW=none`, and skip to step 4 with
   `${REVIEW_DISPATCH_HANDLE}` as the blind-review handle. Otherwise increment
   `${REVIEW_PASS}`. Pass 1 uses <BroadReviewPrompt/> over the whole
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

**Early form** (an <EarlyReviewArm/> launch only): the diff section holds the
partial repair diff at launch, and the prompt opens with the same
`## Implementation status` staging block as <BroadReviewPrompt/>'s early form —
arm on the open ids, their surrounding code, and the partial diff; fire on the
final path-limited diff when the ready sentinel appears; answer the two
questions from the final diff only.

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

**Close every delegation result with the current progress header** — all three
tables and the wall-clock line, produced by <ProgressContract/> steps 3 and 5 with the
current pass or activity. This is not conditional on what the result led to. A
review that came back clean, a fix that landed, a verification that passed, and
a pass that ends the phase all close the same way as one that launches a repair;
so does a turn that only re-arms the timer. The numbered items say what
happened, and the tables say how far into the phase and the plan it happened —
and the tables are the half the user cannot reconstruct for themselves. A result
that ends in a dispatch is where they are worth the most, so emit the header
after the launch and after the timer is armed. Print it below the sections
above, exactly as the recorder emits it. Should the recorder answer that no
window is open, the launcher has not recorded its pass yet: try once more, then
continue without the tables rather than stalling the turn.
</DelegationResultFormat>

<FixDispatch>
For a `dispatch` batch, set `${FIX_ROUND}` from the gate's `round` and create
`${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md` under
<WritePromptContract/>. Work Specification contains every batch id with concrete
file/line findings and intended behavior. Verification contains only implicated
`verify.sh` lines—usually check and test, adding lint only for lint-related
repairs.

A repair runs the same three slots as a phase, per <PhaseTeam/>: `impl` makes
the repair, `test` writes the regression test that would have caught each
finding, and `review` opens adversarially on the repair under <TeamReview/> —
the reading most likely to catch a fix that closes a finding by weakening what
detects it. Partition the findings' files per <TeamFilePartition/> and write one
prompt per slot; `test` covers the same batch ids from the outside.

Run `findings.py dispatch --covers <all batch ids>` before launching, then
launch all three in one message:

```sh
PLAN_DELEGATE_RESOLVES_ROUND=1 implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" fix \
  "<responsibility>" fix "<activity>" "${FIX_ROUND}" impl
implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/fix_test_prompt_${FIX_ROUND}.md" test \
  "<responsibility>" test "<activity>" "${FIX_ROUND}" test
implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
  "${SESSION_DIR}/fix_review_prompt_${FIX_ROUND}.md" review \
  "<responsibility>" review "<activity>" "${FIX_ROUND}" review
```

Each seat records the work it was assigned: the repairing seat `fix`, the test
seat `test`, the review seat `review`.

**`PLAN_DELEGATE_RESOLVES_ROUND=1` goes on exactly one seat**, and it is what
marks the repair round landed. Only the launcher watches the worker exit, so only
a launcher can say a repair landed; asking the orchestrator to record it later
leaves a gap it can be killed or compacted inside, and that gap used to resolve
as "fixed". Two seats carrying the signal would resolve one round twice over.
The signal is separate from the kind precisely so a second repairing seat can
record `fix` honestly without performing the resolution.

Apply <DispatchContract/>; set `EARLY_REVIEW=none` at dispatch, and close the
turn with the progress header per <DelegationResultFormat/>. While a fix runs
that will receive a delegate closure review, <EarlyReviewArm/> may arm that
reviewer early.

**A contained repair closes without a delegate review.** Apply
<ReviewDiffContract/> and read the repair diff yourself. When every hunk sits in
a path the batch's own findings named — or in a new file one of those paths
creates — record each verdict directly and continue to <Synthesize/>. A second
reader buys nothing there: the closure question is only whether the named line
changed as the finding intended, and every line of the diff is in a file the
main pass just read for that finding. Containment is the whole test, which is
why no separate classification of the repair decides it: a behavior-preserving
repair simply always satisfies the same containment test anyway.

Dispatch the normal <DualReview/> closure review whenever the repair leaves that
boundary or the diff cannot answer the question: an edited path no finding
named, a caller or consumer pulled in, a changed signature, registration, or
invariant reaching past the batch, an id whose verdict reads unclear, or a new
defect the repair introduced. Uncertainty routes to the reviewer. The test is
what the diff touched, never how confident the reading felt.

On completion, `implemented` continues as above; `error` applies
<RetainDelegatedPhaseReservation/>, reports the fix log, records an error
outcome, clears the session marker, and stops. Both outcomes resolve the round
in the ledger through the launcher. Any third outcome — the dispatch stopped,
killed, or gone without `impl_status` reaching either — is the main agent's to
resolve with `findings.py abandon` per <FindingsLedger/>, then apply
<RetainDelegatedPhaseReservation/> before reviewing, re-dispatching, or
reporting anything about the round.
</FixDispatch>

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

Choice 1 applies <FixDispatch/>; choice 2 continues to smoke; choice 3 preserves
the gate. With no gating issues, continue to <RunApplicationSmokeTest/>.
</Synthesize>

<RunApplicationSmokeTest>
Read the diff. If no repository binary reaches its changes, record
`not applicable — <reason>` and continue. Otherwise select the target from
Delegation Context Run/Smoke, Acceptance gate, repository instructions, then
manifest. A build, test binary, static example build, or delegate report is not
a smoke test.

Launch the real product from `${WORKING_DIR}` with useful logging/backtraces,
exercise the changed runtime behavior, observe stability, close cleanly, and
record command/action/result. The launch compiles its own target — never run a
build, `check`, or `test` pass first to prove it builds. Startup alone suffices only when no changed
behavior can be invoked.

A panic, fatal log, unexpected exit, or wrong behavior is a blocker: route it
through <Synthesize/>, then repeat review, synthesis, and smoke before later
gates. If this environment cannot perform the interaction or locate an
applicable executable, close the process, record `deferred — <exact human action
and limitation>`, and continue without waiting. Deferred smoke allows the
checkpoint but is batched at <FinalGate/> and reported by <RunSummary/>.
</RunApplicationSmokeTest>

<RunProjectStyleReview>
The run's single style audit, over everything the project built rather than one
phase. Required exactly once when the reviewed diff contains `.rs`,
`Cargo.toml`, or `Cargo.lock`. The actual diff, not `${STYLE_GATE_CONFIG}`,
decides applicability. Phases never run it: they carry no style gate, and a
phase checkpoint never waits on one.

- `single`: after behavioral convergence and first smoke, before phase review.
  The reviewed range is the working tree, tracked and untracked.
- Loop and verbose: from <FinalGate/>, once the whole plan is verified green.
  The reviewed range is `${STYLE_DIFF_BASE}..` — every commit the project
  landed on this branch plus the current working tree.

1. If `STYLE_REVIEW_DONE=true` or the marker exists, restore true and continue.
2. Loop and verbose with an empty `STYLE_DIFF_BASE` have no branch to diff:
   set true, write `not applicable — no diff base` to the marker, and report
   that the run ends without a style review.
3. Build the reviewed diff, including untracked paths. With no Rust/Cargo
   changes in it, set true and write `not applicable` to the marker. Stop if
   the style pass would reach Rust/Cargo work the project did not write.
4. Save combined diff/status to `${SESSION_DIR}/style_review_before.diff` and
   `${SESSION_DIR}/style_review_before.status`, announce the single cleanup and
   the range it covers, and invoke the `clippy` skill inline as
   `style-only auto-proceed` — for loop and verbose, as
   `style-only auto-proceed since ${STYLE_DIFF_BASE}`. `Off`, error, or
   unresolved choice blocks completion.
5. On successful review, set true and write the result to the marker before any
   cleanup verification. Never clear it during later fixes.
6. Save `${SESSION_DIR}/style_review_after.diff` and
   `${SESSION_DIR}/style_review_after.status`, compare them with the before
   snapshots, and read every style-induced hunk. If Rust/Cargo changed, rerun
   `verify.sh test` and `lint` for every affected package. Failures use normal
   finding/fix routing.
7. If cleanup reached runnable code, reset smoke to `not_run` and rerun
   <RunApplicationSmokeTest/>. The guard skips this section on return.
8. Continue to <RunPhaseReview/> for `single`, or back to <FinalGate/>.
</RunProjectStyleReview>

<RunPhaseReview>
For phased plans, invoke `plan:phase_review` with this run's `SESSION_DIR` and
`WORKING_DIR`; pass `auto` in loop/verbose and make `${NEXT_ITEMS_PATH}` available.
Its retrospective, review outcomes, and proposed next-item amendments are
temporary session files, never plan sections. It may edit only remaining `todo`
Work Orders; earlier `done` phases remain byte-identical. Later user choices
become Pending decision blocks.

Dispatch its architect review only when any trigger holds. Every trigger below
asks one question: **does something still ahead of the run now read wrong?** It
never asks whether this phase did something notable. A phase that shipped
exactly what its Work Order described, leaving every remaining Work Order and
next item still accurate, needs no architect review however much it built.

- implementation deviated from the Work Order;
- phases or remaining Work Orders were changed;
- a later Pending decision was added;
- the phase changed a semantic state, transition, failure, availability,
  recovery condition, diagnostic, or externally observable lifecycle that a
  remaining Work Order or `${NEXT_ITEMS_PATH}` item now describes wrongly.
  Introducing one that nothing ahead depends on is not a trigger, and neither is
  changing one that every remaining item still describes correctly;
- a changed type/API/registration/path is named by a remaining Work Order or
  `${NEXT_ITEMS_PATH}` **and** the change invalidates what that item says about
  it — a rename, a changed signature or ownership, a moved responsibility, a
  removed affordance. Merely touching a file or type a later item also names is
  not a trigger;
- the ledger returned a convergence advisory; or
- three phases completed since the prior architect review.

The last trigger is the floor, and it is meant to carry most runs: a plan whose
phases land as written reaches the architect every third phase and no more
often. Name the trigger that fired in the one-line report, so a run that
dispatches it every phase is visible as the drift it represents.

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
that matters happens later, when an item is scheduled into a phase. Rewriting a
backlog record so it describes the code that now exists is the same operation
`/plan:shrink` and `/plan:to_as_built` perform on completed phases — an as-built
correction, never a decision. What earns a gate here is a judgment about what is
worth doing, not the maintenance of a record.

A proposal is **`apply`** — do it now, do not ask — when the shipped code decides
it:

- Any `add`. A new backlog item records that something may be worth doing. It
  changes no phase, no schedule, and no commitment.
- Any `amend`. A drifted file or line reference, a re-key onto a type a phase
  introduced, a re-target at the crate that now owns the work, a capability
  moved out because a phase absorbed it, a restatement of what would satisfy the
  item now that the surrounding code has changed — all of it is as-built
  maintenance of a record nobody has committed to building. Cite the evidence in
  the one-line report and move on. An `amend` is never gated for being large,
  for changing what the item asks for, or for changing what would satisfy it.
- A `remove` the shipped code has already satisfied. The item asked for
  something that now exists; deleting it records that fact.

A proposal is **`gate`** — ask the user — only when it rests on judgment rather
than evidence: a `remove` proposed because the work looks not worth doing, out
of scope, or superseded by a direction nobody has taken yet. That is a product
call and nothing here may make it.

When the split is genuinely unclear, apply it — a wrong backlog edit is one line
to revert, and every item is re-read before it is ever scheduled. Never write a
`gate` proposal to the repository without approval.

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

1. Require smoke pass, `not applicable`, or `deferred`. Style is not a phase
   gate: <RunProjectStyleReview/> runs once at <FinalGate/>, over every
   checkpoint this one joins.
2. Confirm status contains only this phase, its plan doc, and an approved change
   to `${NEXT_ITEMS_PATH}` when present.
3. Run `verify.sh fmt <package>` for every touched package; include resulting
   formatting changes.
4. Mark the phase `status: done`. Never put its commit hash in the plan.
5. Read and validate
   `${SESSION_DIR}/delegated_phase_reservation_state.json`; do not use a value
   remembered from conversation or the harness-session mapping. For `Active`,
   require its phase to be current and run `/sync check` as drift's full
   phase-start comparison exactly once:

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state invoke \
     --cwd "${WORKING_DIR}" --expected-verb drift -- drift --full --json
   ```

   Require a validated exit-`0` drift result with `payload.kind = drift`,
   `payload.data.comparison = full_phase_start`, and exactly one
   `payload.data.results` entry. That entry's `reservation_id` must equal the
   `reservation_id` in the durable `ActivePhaseReservation`; bind the result to
   that record's `phase_start_head`, because `full_phase_start` means the engine
   compared the selected reservation against its protected phase-start
   baseline. The response does not echo that object id, so do not invent an OID
   field or re-derive acting identity. An exit-`5` `invalid_input` response whose
   diagnostic says drift requires a live session mapping, active coordination-run
   marker, or `CARGO_BERTH_RUN` is the `Unidentified` identity case. That case,
   no result, more than one result, a different reservation id, any comparison
   other than `full_phase_start`, an incursion, collision, attribution
   requirement, absent/corrupt ledger, or malformed response is identity
   ambiguity or unsafe drift and blocks the checkpoint. Apply
   <RetainDelegatedPhaseReservation/> and report the exact full-drift command
   above so a person can run it to see the conflict. The cheap drift delta is
   forbidden here. Exit `6` has already spent the engine's single ten-second
   deadline: invoke no retry, retain the reservation, and name that same exact
   full-drift command. The two inactive states skip drift because they own no
   reservation. `CheckpointCommittedAwaitingReleaseConfirmation` is valid here
   only as a resumed state that routes directly to step 7; it does not re-enter
   drift or checkpoint creation.
6. Stage the phase, its plan doc, and `${NEXT_ITEMS_PATH}` when approved and
   changed; commit exactly once:

   ```
   checkpoint(<plan-slug>): phase N — <title>

   <what the phase built>

   Claude-Session: <session url>
   ```

   A failed commit is an unsuccessful ending: do not invoke release, leave
   `Active` untouched, and apply <RetainDelegatedPhaseReservation/>. After a
   successful commit, for `Active`, immediately capture the full output of
   `git rev-parse HEAD`, atomically replace the `Active` record with
   `CheckpointCommittedAwaitingReleaseConfirmation` carrying that value as
   `checkpoint_commit`, and read the new record back. This durable transition
   must succeed before invoking release. The new state comes only from the
   commit that just succeeded, never from conversation or from a later reading
   of `HEAD`. If capture, atomic replacement, or read-back fails, do not invoke
   release and apply <RetainDelegatedPhaseReservation/> to whichever valid
   durable state remains.
7. Read and validate the durable record. For
   `CheckpointCommittedAwaitingReleaseConfirmation`, read its reservation id
   and `checkpoint_commit` from the read-back
   `CheckpointReleaseConfirmationPending` and invoke `/sync release` exactly
   once. An `Active` record at this step means step 6's durable transition did
   not complete: do not invoke release and apply
   <RetainDelegatedPhaseReservation/>.

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state invoke \
     --cwd "${WORKING_DIR}" --expected-verb release -- \
     release <recorded-reservation-id> --json
   ```

   Do not pass a commit: `release` snapshots the invoking worktree's current
   HEAD. Require exit `0`, envelope status `outstanding`, release payload status
   `checkpointed`, the requested reservation id, and
   `payload.data.protected_tip` equal to the durable record's read-back
   `checkpoint_commit`. Also require its
   `payload.data.session_mapping_publication`; report its `unavailable`
   diagnostic without treating the journalled checkpoint as absent. Those are
   the first-attempt assertions and are not weakened by recovery.

   A resumed session obtains `checkpoint_commit` by reading the durable record,
   never by re-deriving it from current `HEAD`. `HEAD` is not proof: a later
   commit in the same worktree would silently supply the wrong answer and make
   the comparison accept the wrong checkpoint.

   A process kill, crash, or power loss can retain the durable record after the
   `Checkpoint` operation was appended but before this workflow observed the
   reply. On a later recovery from that retained record, invoke the same release
   once. If it returns exit `0`, names the requested reservation, and has release
   payload status `resnapshotted`, `evidence_revalidated`, or `released` instead
   of `checkpointed`, invoke the named reservation lifecycle query below.
   `resnapshotted` has envelope status `outstanding`; `evidence_revalidated` can
   legitimately have `outstanding`, `integrated`, `trunk_rewritten`, or
   `object_unknown` because its envelope status reports current integration
   evidence, not reservation lifecycle. Evidence replay preserves the
   `outstanding` lifecycle and its original protected tip. Do not gate recovery
   on envelope status: the named reservation lifecycle query must establish the
   current lifecycle and protected-tip equality with the journalled checkpoint.

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state reservation \
     --cwd "${WORKING_DIR}" --reservation <recorded-reservation-id>
   ```

   Inspect the validated coordinator `state`, which is derived from
   `envelope.payload.data` without reading `message`. Exit `0` requires
   `kind = reservation_lifecycle`, the requested `reservation_id`, and exactly
   one lifecycle alternative: `active`; `outstanding` with `protected_tip`;
   `released_after_checkpoint` with `protected_tip` and `disposition`; or
   `released_without_checkpoint` with `disposition`. Exit `5` is valid only for
   `kind = unknown_reservation` carrying the requested `reservation_id`.

   `outstanding` with `protected_tip` equal to the durable record's read-back
   `checkpoint_commit` confirms that the same checkpoint release is already
   journalled. `released_after_checkpoint` with the same protected tip also
   confirms that release, followed by the reported terminal disposition; report
   it as recovered and subsequently released. A different protected tip is
   another release and must retain the record. `active`,
   `released_without_checkpoint`, `unknown_reservation`, a mismatched echoed id,
   or a busy, unreadable, malformed, or otherwise invalid response retains the
   record with that distinct reason. Do not consult the retention ref: it proves
   commit reachability but not whether the selected reservation is outstanding
   or released.

   Report a matching checkpoint as recovered rather than re-made.
   Only after the first-attempt structured assertions or these recovery
   assertions pass, delete
   `${SESSION_DIR}/delegated_phase_reservation_state.json`.

   An ordinary failed release did not append anything because transaction
   validation precedes the journal write; it is not recovery and the original
   first-attempt assertions still apply when it is run again. At the moment of
   any busy or failed release, invoke no retry and apply
   <RetainDelegatedPhaseReservation/>. The checkpoint commit exists, but the
   phase does not complete until a later invocation confirms either the normal
   first-attempt reply or the matching already-journalled checkpoint above. The
   two inactive states invoke no release.
8. Report `Checkpoint <short hash> — phase N: <title>.` Never push here. This
   report follows successful release when the phase was active.
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
After smoke, phase review, shrink, next-item consideration, cleanup, and
checkpoint when applicable, run `progress_history.py finish-phase --session-dir
"${SESSION_DIR}" --status completed`.

After a loop/verbose phase with `RepositoryNotEnrolled` or
`EnrolledAwaitingFirstTouch`, delete its completed-phase state
file. `Active` must already have been replaced by
`CheckpointCommittedAwaitingReleaseConfirmation`, and that pending state must
already have been deleted by the validated successful release. If either state
survives, the phase is not complete and this section must not run.

In `single`, also run `finish-run --status completed`, then
apply <RetainDelegatedPhaseReservation/>, report that no checkpoint release was
attempted, run `bash ~/.claude/scripts/delegate/end_session.sh`, and end. Other
modes continue.
</RecordPhaseCompletion>

<VerbosePostPhaseReport>
After a completed verbose phase outside an auto window, report only that phase
from its reviewed diff, accepted fixes, `As-built` block, and checkpoint. Inside
an active window, emit no per-phase report; when the window's last phase
completes, emit one <CombinedWindowReport/> instead of per-phase reports.

Single-phase report:

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
[gate, meaningful tests/lint, review/fixes, smoke]

**Checkpoint:** `<short hash>`

### What remains
[phases remaining out of the plan total, then the auto-together recommendation]
```

Use the diff over planned claims. Include only load-bearing new/materially
changed types; use the same statuses as <PhaseBriefing/>, write every cell under
<TypeTableCells/>, and say when none exist.
Everything above `### What remains` describes only the completed phase.

<CombinedWindowReport>
One report covering every phase the window ran, built from the same sources as
the single-phase report. Keep it succinct and plain-spoken — ordinary sentences
a reader away from the details can follow — while still naming types, modules,
and crates exactly. Describe what the window built as one piece of work, not a
per-phase replay:

```
## Phases N–M complete — <window summary title>

### What now works
[combined reviewed behavior across the window]

### Important types and APIs
| Phase | Type / trait / API | Status | Role | System relationship |
| --- | --- | --- | --- | --- |

### Verification and review
[one combined summary; call out only per-phase results that differed]

**Checkpoints:** phase N `<short hash>`, …, phase M `<short hash>`

### What remains
[same content as the single-phase report]
```

Unify the types into that single table with its `Phase` column — never one
table per phase. Include only load-bearing new/materially changed types across
the whole window, using the <PhaseBriefing/> statuses and <TypeTableCells/>;
say when none exist.
<RemainingWorkOutlook/> applies to `### What remains` unchanged.
</CombinedWindowReport>

<RemainingWorkOutlook>
`### What remains` is the one place the report looks forward. It states what is
left and whether the next phases are safe to run without stopping between them.
It never briefs a phase and never asks for authorization; <VerbosePostPhaseGate/>
owns the ask.

**Count.** Read it from

`python3 ~/.claude/scripts/delegate/progress_history.py phase-count --plan-doc "<plan>"`

and report its `todo` and `total`. Never count phases by hand or by grep: a
heading takes three forms over its life and any pattern keyed on `status:`
silently ignores every shrunk phase, which is the mistake <ProgressContract/>
records. When `todo` is zero, say the plan is out of phases and that final
workspace verification runs next; skip the recommendation.

**Recommendation.** Read the Work Orders of the next todo phases in order, up to
three. A consecutive run is auto-together only while every phase in it:

- carries no unresolved `**Pending decision:**` block — that phase stops the run
  at its own pre-dispatch check whatever the window says;
- extends a subsystem shipped by a `done` phase rather than opening a new one;
- has an acceptance gate that runs unattended, with no smoke action needing the
  user;
- and does not depend on an outcome only knowable once an earlier phase in the
  run has actually landed.

The run ends at the first phase failing any of these. Name that phase and the
one reason it stops there. When the very next phase fails a test, recommend that
single phase; never round the window up to a phase you would then have to
interrupt. Cap the recommendation at three phases even when more would qualify,
so a batch briefing stays readable.

When the only criterion stopping a longer window is an unresolved
`**Pending decision:**` block on a covered phase, and the phases otherwise
cohere enough that running them together is clearly desirable, say so and offer
to surface those decision questions now: answering them here resolves the
blocks and lets the next auto control cover the full run. Before bringing a
decision forward, apply <DecisionEconomy/>: decide any with an obviously better
answer yourself and bring only true, substantive tradeoffs. Ask only the
blocking questions; the batch briefing still owns briefing the phases.

Write it as two or three sentences of ordinary English under <UserFacingText/> —
what the next phases do, why they group or do not — and always end with the
concrete control to type: `auto through phase X` with the actual phase number,
or `proceed` when only the single next phase qualifies. Never leave the number
for the user to work out. This closing control line is required in every
single-phase and combined-window report while todo phases remain. Do not emit a
table, a per-phase criteria checklist, or the criteria vocabulary above.
</RemainingWorkOutlook>
</VerbosePostPhaseReport>

<VerbosePostPhaseGate>
Skip when no todo phase remains or an auto window continues. Otherwise ask:

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
If no todo phase remains, run <FinalGate/> then <RunSummary/>. Otherwise reset
`REVIEW_PASS=0` and smoke to `not_run`.
Style state is per-run, not per-phase: never reset it or delete its marker
here, and never re-resolve `STYLE_DIFF_BASE`.

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
2. For Rust, invoke `clippy auto-proceed no-style` inline. Style stays out of
   this step; step 4 owns it.
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
Loop and verbose only, at most once per run, after <FinalGate/> is green. It
commits what the gate itself produced — the style cleanup from step 4 and any
synthetic-final repairs — so the run leaves no uncommitted work behind.

1. With no changes in `git status --short`, skip it silently.
2. Confirm the changed paths are only the ones the gate touched: the
   before/after snapshots in <RunProjectStyleReview/> plus the synthetic phase's
   own baseline name them. Anything else stays uncommitted and is reported
   instead; never sweep an unrelated path into this commit.
3. Run `verify.sh fmt <package>` for every touched package and include the
   result.
4. Stage those paths and commit exactly once:

   ```
   checkpoint(<plan-slug>): final gate

   <what the style pass and any final repairs changed>

   Claude-Session: <session url>
   ```

   Keep the `checkpoint(<plan-slug>)` subject: <ResolveStyleDiffBase/> reads it
   to place a later run's diff base.
5. Never push. This commit holds no phase reservation, so it invokes no drift
   check and no release. Report `Final gate <short hash> — style review and
   closing repairs.`
</FinalGateCommit>

<RunSummary>
Emit on every multi-phase ending:

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Final gate:** [result or skipped reason]
**Style review:** [range reviewed and result, or the reason the run never ran it]
**Smoke checks still unperformed:** [phase + exact action, or none]
**Deferred decisions still open:** [phase + decision, or none]
**Reservation disposition:** [checkpointed and outstanding, retained with the
reason this run stopped, or coordination not active]
**Why the run stopped:** [complete, user stop, pending decision, convergence reason, or error]
```

Apply <UserFacingText/> and <RetainDelegatedPhaseReservation/> for every ending
that did not complete <CheckpointCommit/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Finally run `bash ~/.claude/scripts/delegate/end_session.sh` on
every exit so the Stop hook cannot revive a finished run.
</RunSummary>
