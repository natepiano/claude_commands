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

Some contracts are defined in their own file and stubbed here. A stub names the
file and the moment it applies; **read that file at the moment and act from what
it says, never from memory of an earlier read or from the stub alone.** Each one
is also a command the user can invoke directly when this workflow fails to run
it:

| Contract | File | Command |
| --- | --- | --- |
| <ProgressReport/> | `commands/plan/delegate_report.md` | `/plan:delegate_report` |
| <VerbosePostPhaseReport/>, <CombinedWindowReport/>, <RemainingWorkOutlook/> | `commands/plan/delegate_phase_report.md` | `/plan:delegate_phase_report` |
| <CheckpointCommit/> | `commands/plan/delegate_checkpoint.md` | `/plan:delegate_checkpoint` |
| <ConsiderNextItems/> | `commands/plan/delegate_next.md` | `/plan:delegate_next` |
| <ResolveStyleDiffBase/>, <RunProjectStyleReview/> | `commands/plan/delegate_style.md` | `/plan:delegate_style` |
| <ComposeWorkOrder/> | `docs/delegate/compose_work_order.md` | — |

Paths are under `~/.claude/`.
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
For `verify.sh final`, launch under <ToolingContract/>, export
`PLAN_DELEGATE_SESSION_DIR="${SESSION_DIR}"` so `verify.sh` opens its own
progress window, and tell the user what is running. Nothing else reports on it,
so the timer matters more here, not less: Claude arms one under
<ProgressContract/> exactly as a dispatch does and ends the turn, resuming from
the task notification; Codex applies <CodexDispatchWait/> with progress disabled.
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
   slot this prompt is for and the role it opens in, taken from the Work
   Order's **Seats** field per <PhaseTeam/>.
2. Boundaries: do not commit, branch, or touch unrelated files; summarize files,
   reasons, and deviations when done, and **write that summary to this slot's
   `impl_summary_<slot>.txt` as the last act before finishing** — a background
   session has no output redirect, so a summary left only in the reply is a
   summary the orchestrator never sees. State this slot's file set and the
   peers' file sets per <TeamFilePartition/>, and that a peer's file is blocked
   rather than merged.
3. Narration: before each activity, run
   `bash ~/.claude/scripts/delegate/board.sh post <concrete SESSION_DIR> <slot> status "<activity>"`.
   Use short present-tense text and never read the heartbeat file. Role
   changes: **before the first tool call in a new role** — recruited into
   writing, converging on review, standing down — run
   `bash ~/.claude/scripts/delegate/board.sh role <concrete SESSION_DIR> <slot> <impl|fix|test|review> "<why>"`,
   written out with the real path and this slot's name. A `status` sentence
   saying the same thing does not count: the table reads the `role=` field,
   and a move that is not posted is a row the run never shows.
4. `## Team` — state the opening from Seats (`2 writers + 1 tester`, and the
   role each slot opens in), then name the three concurrent slots and who holds
   which files, each hub file with its one owner. Copy
   the board commands from <CoordinationBoard/>, and say a `verify.sh` run may
   pause while a peer finishes its own. Copy <BuildTokenContract/>'s
   delegate-facing prohibition: never mention, request, or acquire the cargo
   token. State the one rule plainly: **a question to a peer is a message, a
   decision is a board post**, and the board has no `ask` kind to fall back on.
   Give this slot its own mesh name, both peers' names, the call that reaches
   each of them, and — on the claude path — the orchestrator's name from
   `ListAgents`, per <PhaseMesh/>. An address a member has to go looking for is
   one it will not use, and a codex peer needs the literal `codex_mesh.py`
   command line with the concrete `--session-dir` already filled in, not a
   description of it. A slot whose register line says `mesh=none` has no peer
   channel at all: say so, and tell it to read the board rather than wait on a
   reply.
5. `## Project Context`.
6. `## Work Specification`.
7. `## Type Design Contract` per <TypeDesignContract/>.
8. `## Verification` per <VerificationContract/>, exactly as listed and with
   nothing added around it.

The Verification section carries the applicable command lines and every
delegate-facing rule from <VerificationContract/>, with nothing added around
them. It must also say: run only its listed commands, never raw Cargo; run each
with the sandbox disabled; do not report until every command has exited and its
output has been read. If an edited package has no listed `test` line, add that
package's scoped `verify.sh test` and report it. Omit plan **Style** metadata
and never load the style guide; <RunProjectStyleReview/> owns the run's one
style audit.
</WritePromptContract>

<PhaseTeam>
Every implementation and fix dispatch runs **three delegates at
once**, never one. They share `${SESSION_DIR}` and `${WORKING_DIR}`, and each
occupies a fixed **slot** that names its artifacts and its board identity. The
**default opening**:

| Slot | Opens as | Owns |
| --- | --- | --- |
| `impl` | the phase's implementation or repair | the Work Order's production files |
| `test` | tests for the same specification | test targets under `tests/` and new test files |
| `review` | reading the spec and the tree cold | nothing; it is the team's reserve |

**The Work Order's `Seats:` field sets the opening and overrides this table.**
Its first line names the opening — `1 writer + 1 tester + reserve` is the table
above; `2 writers + 1 tester`, `1 writer + 2 testers`, and `3 writers` are the
others — and a line per slot names that slot's files and, where it differs
from the table, what it opens as. `impl` always opens as `impl` (`fix` in a
repair). `test` opens as `test` wherever the phase has a **test lane** — a
`tests/` directory in a touched crate and a Spec concrete enough to test before
the implementation exists — and as a writer where it has none. `review` is the
**flex seat**: it opens as whatever third role the opening needs — a second
writer, a second tester, or the cold read. A plan compiled without the field
opens as the table says, with the partition decided at launch per
<TeamFilePartition/>.

A slot is an identity and never changes. What a slot is *doing* is its **role**,
and roles move during a phase per <RoleReassignment/>. Everything downstream —
the board, the progress table, the review split — reads the slot for identity
and the role for activity, so keep the two distinct: `review` doing
implementation work is still slot `review`.

`test` opens against the **specification, not the implementation**. The Work
Order defines the behavior, so tests can be written before any of it exists;
a tester that waits for `impl` has converted a parallel team back into a queue.

**Every seat carries its own pass kind, which is its opening role**, so a team
phase records three passes. The recorder keys them by slot and closes only that slot's stale pass;
<LaunchImplementation/> step 5 owns the argument positions.

Launch all three in **one message** so they run concurrently, each with its own
prompt file and its slot as the ninth argument to `implement.sh`, then apply
<DispatchContract/> once for the team: the progress timer covers the phase, not
each member. <LaunchImplementation/> owns the rest of the procedure, and repairs
run the same team under <FixDispatch/>.

The phase is complete only when every slot has a terminal `impl_status_<slot>`,
not when the first one lands. Reading one slot's `implemented` as the phase's
result is the same defect as reading a completion notification as a finished
assignment.
</PhaseTeam>

<CoordinationBoard>
The team coordinates through `${SESSION_DIR}/board.log`, written only with
`bash ~/.claude/scripts/delegate/board.sh`.

**Why a file even when messages work.** Every member is reachable by name on
both paths — see <PhaseMesh/> — but the board is the durable broadcast record
and the token owner, where messages are addressed and transient. One post
reaches both peers and the wrapper at once; a member resumed hours later reads
the whole history rather than what arrived while it listened; and only the
token, taken with `mkdir`, makes anything mutually exclusive. With
`[delegate.options] codex_mesh=0` a codex member is unaddressable and the board
is its only channel, since the orchestrator is asleep between progress ticks and
cannot relay. Each `register` line says which case holds, in its `mesh=` field.

- `board.sh post <session_dir> <slot> <kind> <message>` — one broadcast line.
  Kinds are a closed set: `register`, `claim`, `release`, `status`, `blocked`,
  `handoff`, `done`. There is no `ask` and no `answer`, and the command rejects
  both: a question to a peer is a message, per <PhaseMesh/>.
- `board.sh read <session_dir> --since <cursor>` — everything new. Each line is
  numbered; keep the last number as the cursor. Read after acquiring a token,
  and whenever you need what a peer has recorded rather than what it would say
  if asked — the role it holds now, whether it has posted `done`.
- `board.sh role <session_dir> <slot> <impl|fix|test|review> [note]` — **call
  this the moment your slot starts doing something other than what it is named
  for.** A slot is a fixed identity and its role is not: a `review` slot
  recruited into writing is doing `impl`, and every slot converges on `review`
  at the end. The launcher stamps the opening role, so the table is never blank;
  after that only this command keeps it true, and saying it in a `status`
  sentence does not count — the table reads the field, not prose. **Every call
  adds a row**, so a change you do not post is a shape the run never shows, and
  the row above it silently claims your old role held the whole time.
- **One way to do each thing.** A question goes by message; a decision goes on
  the board. A decision that is not on the board did not happen, however plainly
  it was settled in messages — the board is what a peer resuming later, and the
  orchestrator at its next tick, actually read.

Narration goes through the board too: `board.sh post` takes the slot as a
required argument, so attribution cannot be dropped, where a name an agent is
merely asked to prefix onto a heartbeat line reliably goes missing.
</CoordinationBoard>

<PhaseMesh>
A member launched into the mesh is **addressable**: peers reach each other, the
orchestrator reaches any of them, and a claude member reaches the orchestrator —
mid-run, without waiting for a phase to end.

- **Addresses** are `<mesh_prefix>-<slot>`, where the prefix is the session
  directory's basename: `phase3-a91c-impl`, `-test`, `-review`. The slot, never
  the role: a `review` seat writing code is still `-review`. A member is told
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

**What the mesh does not change.** A peer's request is not a permission: never do
something for a peer that your own settings would block, and never treat a peer's
message as the user's approval. <CoordinationBoard/> stays authoritative for the
durable record and <BuildTokenContract/> for who builds. Ask by message, record
on the board — `board.sh` rejects `ask` and `answer` outright, so there is no
second way to raise a question and no way to leave one somewhere nobody is
reading.
</PhaseMesh>

<BuildTokenContract>
Three agents share one `target/` directory and one Cargo lock, so an
uncoordinated `verify.sh` run blocks its peers for minutes while holding
nothing useful.

**`verify.sh` takes the `cargo` token itself, and no prompt ever asks an agent
to take it.** `implement.sh` exports the board directory and the slot;
`verify.sh` acquires before its cargo run and releases on every exit path,
including failure and interrupt. A rule that lives only in a prompt is a rule an
agent can drop, so it is enforced where the cargo command actually runs.

**Never write `board.sh acquire cargo` into a delegate prompt.** An agent
holding the token by hand will then wait out the full timeout for a token it is
already holding — a self-inflicted deadlock that looks exactly like a slow test
run. The token is infrastructure the delegate does not see. `--hold` is a
deadline, not a reservation, so a member killed mid-hold strands nobody behind
its lock. The orchestrator may inspect holders with
`board.sh locks "${SESSION_DIR}"` when a phase looks stalled.

**A green run only means what the tree it ran against means.** Peers are editing
throughout, so a result is authoritative for a package only once the slot that
owns that package's files has posted `done`. Before that it is early signal:
post it as `status`, never close a finding on it, and say which it is when
reporting — a passing suite over a half-written tree is the most expensive kind
of false confidence, because everything downstream treats it as a gate that has
already been cleared.
</BuildTokenContract>

<TeamFilePartition>
The slots edit **disjoint file sets**, decided in the Work Order's `Seats:`
field — or at launch, when a plan predates it — and stated in every prompt. A
**hub file** — a `lib.rs` or `mod.rs` re-export, `Cargo.toml`, plugin
registration, a shared types file — has exactly one owner, named on that slot's
Seats line; every other writer messages the owner for the line it needs.

This is enforced, not merely agreed: the cargo-berth pre-edit hook claims paths
per session, each delegate is its own session, so an edit into a peer's claimed
file is **blocked** rather than merged. Two consequences that must reach the
prompts:

- The tester writes **integration tests under `tests/`**. A `#[cfg(test)]`
  module added inside a production file that `impl` has claimed is a blocked
  edit, not a merge conflict, and the tester will simply fail to write it.
- Any change that reaches outside one slot's file set — a signature both slots
  need, a shared helper, a new type two slots want — belongs to the slot that
  owns the file it lives in. Message the owner and let it write it. Never
  weaken a fix to avoid the dependency, and never define the same type twice to
  route around a claim.

Where the Work Order's own files cannot be split — everything lands in one or
two files — the Seats opening line says so, `impl` gets the whole set, and
`test` and `review` open on work that does not touch it. A partition that does
not exist is not worth inventing; a partition that is wrong costs the phase.
</TeamFilePartition>

<RoleReassignment>
Roles move; slots do not. Every move is a board `handoff` post naming the slot,
the role it is leaving, and the role it is taking, because that post is what the
progress table reads to say what each agent is doing now.

- **A writer may recruit the reserve, when the opening left one.** When the
  implementation is wider than one writer, `impl` messages `review` naming the
  disjoint file subset it wants taken; `review` replies, then posts `handoff`
  so the table shows the move. The team is then two writers and a tester.
  `review` is the reserve precisely because it holds no files and can leave its
  lane without stranding anything. A phase whose Seats opened all three as
  writers or testers has no reserve, and that was the field's decision, not a
  gap to recruit around.
- **`test` is never recruited away** while tests for the phase are unwritten.
  It is the only slot whose absence cannot be recovered later in the phase, and
  a phase that ships untested is not cheaper, only later. A `test` seat that
  Seats opened as a writer has no tests to protect and moves like any writer.
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
adversary for its own file set. With three writers no seat is a non-author of
everything, so each seat takes the adversarial brief over a file set it did not
write, assigned cross-wise, and the two aspects below are dealt out across the
same three seats on the same rule.

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

- Serialization and result authority follow <BuildTokenContract/>: every line
  here takes the `cargo` token on its own, so a run may wait for a peer, and a
  result is a gate only once the slot owning that package's files has posted
  `done`.
- `check` is optional feedback, not a gate. Every modified package gets `test`
  and `lint`; trace changed public APIs, traits, registration, and plugin wiring
  to modified callers. Name an integration target explicitly only to re-run it
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
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <delegate\|main\|both> [--detail <d>]` | create an id |
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
the worker errors. The main agent owns it only when the launcher is gone — the
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
for completion and for error alike. The main agent never calls either by hand —
a hand-written call forges a pass that never ran, and `findings.py gate` counts
passes when it decides whether a phase is converging. The recorder rejects an
unowned call.

The one exception is a launcher the main agent killed — the <DualReview/>
preemption. Close that slot's open pass with `finish-pass --status canceled
--orphaned-launcher`, which the recorder accepts only for `canceled` and only
while a pass is open. A killed fix dispatch leaves its findings
`repair_in_flight` for the same reason, so `findings.py abandon` per
<FindingsLedger/> belongs beside this call.

Phase records are the main agent's, and both belong at the real boundary:
`finish-phase` for the outgoing phase and `start-phase` for the incoming one run
before that phase's first dispatch. Recording them late attributes the new
phase's work to the finished one — its title, its elapsed clock, and its pass
counts all describe the wrong phase.
</PassOwnership>

<ProgressContract>
Before every progress-enabled wait, set `${PROGRESS_INTERVAL_SECONDS}` from
`PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` in `~/.claude/config/delegate.conf`.
It is the Claude timer delay and the Codex poll timeout. There is no default:
if it is missing or is not a positive integer, stop and tell the user to set it.

Claude keeps exactly one one-shot timer in a managed background terminal while
work is running and progress is enabled. Launch:

`bash ~/.claude/scripts/delegate/progress_timer.sh "${SESSION_DIR}" "${PROGRESS_INTERVAL_SECONDS}"`

Save its handle and end the turn normally. The timer contains no loop and runs
no agent. Use the script rather than a bare `sleep`: it records the armed
deadline in `${SESSION_DIR}/progress_timer` and clears it on exit, which lets
the Stop hook tell an armed timer from none. Codex never launches it; a
<CodexDispatchWait/> timeout is its tick.

**Never end a turn that leaves work running without an armed timer.** Running
work is a live launcher, a background `verify.sh final`, or any main-agent run
that opened a progress window. A registered Stop hook enforces this and blocks
once; treat that block as a dropped timer, not as a prompt to argue.

Launcher work is a **pass**; main-agent work -- verification, smoke, style -- is
an **activity**. `verify.sh` opens and closes its own whenever
`PLAN_DELEGATE_SESSION_DIR` is set; open one by hand for other main-agent work
with
`progress_history.py start-activity --session-dir "${SESSION_DIR}" --label <label> --activity <what>`
and close it with
`finish-activity --session-dir "${SESSION_DIR}" --status <status> --result <outcome>`.
Keep `--label` to one or two words -- it names the row -- and make `--result` the
short outcome that row should show: `pass`, `clean`, `no change`. Without one
the row can only say `done`, which reports that the window closed rather than
what it found. Activities sit beside passes and are invisible to `findings.py`,
which is why <PassOwnership/> forbids faking a pass for the same purpose.

On a Claude timer notification or Codex poll timeout, compose the update per
<ProgressReport/>, which
`~/.claude/commands/plan/delegate_report.md` defines in full. Read that file and
follow it; a report written from memory of an earlier read drops the
byte-for-byte copy rule first. It also owns this tick's <EarlyReviewArm/>
trigger point and the query that answers questions about work already finished.
The user can invoke the same file as `/plan:delegate_report`.

Afterwards, if the dispatch remains active, Claude reads the interval again,
launches a fresh one-shot timer, replaces the handle, and ends the turn. Codex
returns immediately to <CodexDispatchWait/> on the same session and reads the
interval again before polling.

**An armed timer is never a substitute for the report.** Every turn that arms or
re-arms one emits the full report first — both tables and the wall-clock line —
and a bare "timer re-armed" line is a dropped report, not a short one. This
matters most where it is easiest to skip: a Stop-hook block reads as a mechanical
complaint about a missing file, so the reflex is to relaunch the script and end
the turn. But the hook fires on the turn the user was owed an update and did not
get one, and the timer file is only how it noticed. Re-arming without reporting
answers the hook and leaves the user exactly where they were.

A user-requested status check emits <ProgressReport/> immediately. If the user
stops updates, stop and clear any Claude timer and set
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
Read `~/.claude/commands/plan/delegate_style.md` in full and apply it once per
run, before the first dispatch. Loop and verbose only; `single` skips it and
never sets a base. That file defines this contract and <RunProjectStyleReview/>.
Never resolve the base from memory of an earlier read — the
`purpose_built=false` question is asked exactly once and dispatch waits on it.
The user can invoke the same file as `/plan:delegate_style`.
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
| `Active` | The edit hook, from a validated clear-check result whose acquisition is `appended` or `already_held` | Copies the returned reservation and coordination-run ids, phase, and protected phase-start HEAD; supplies a release argument. |
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
1. When no state exists yet, invoke the shared `/sync board` entry point once:

   ```sh
   PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.claim_state board \
     --cwd "${WORKING_DIR}"
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

### Opening
[the Seats opening line verbatim, or "default — no Seats field"]
```

Rows include only load-bearing types explicitly named by the Work Order. Status
is `New`, `Existing - Changes`, or `Existing - No Changes`, inferred from that
Work Order without code research. Mark genuine uncertainty instead of guessing;
say explicitly when no load-bearing type is specified. Write every cell under
<TypeTableCells/>.
</PhaseBriefing>

<TypeTableCells>
Governs the `Planned role` and `System relationship` cells in every types table
— phase briefing, window briefing, and completion report alike. The
`Type / trait / API` cell is the only place a code identifier belongs; the other
two are written for someone who has never opened the file and never will. Name
the thing in ordinary words — a tag, a list, a rule, the box around the members,
the step that copies it — and say what it does, or what breaks without it.

| Test every cell must pass | Rejected | Written |
| --- | --- | --- |
| **Say it out loud** to someone watching the running application | "Durable back-reference from an instance shell to the registered Look definition it was built from" | "A tag on each Look saying which of the seven it came from" |
| **Name the consequence**, not just the mechanism | "Copied onto duplicates by the new integration point" — copied by what, and what happens if it is not? | "Duplicating has to copy it deliberately, because the duplicator copies wiring but no tags" |

Three specific failures, each fluent English that informs nobody: a code
identifier used as a noun where an ordinary word exists — *the shell*, *the
definition*, *the publication*; a noun phrase compounded from three or more plan
terms, such as naming a transaction by the three steps it performs; and
plan-internal vocabulary the user has never been shown — gate ids, phase numbers
as adjectives, *promotion*, *staging*, *additive*, *erased*.

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
3. `~/.claude/config/agents.conf` owns delegate family/model/effort, one row
   per kind. Each seat's kind is its opening role from the Work Order's
   `Seats:` field: `impl` for the `impl` slot, and for `test` and `review`
   whatever their Seats lines open them as — `test` and `review` under the
   default opening. State the opening in the dispatch update in ordinary
   words: "opening 2 writers + 1 tester: impl on the hana side, review writing
   the catalyst side, test on the catalyst tests".
4. Take the partition and the opening from Seats and write one prompt per slot
   under <WritePromptContract/>: `${SESSION_DIR}/implementation_prompt.md` for
   `impl`, `test_prompt.md` for `test`, and `review_prompt_team.md` for
   `review`. Only when the field is absent, partition per <TeamFilePartition/>
   yourself and say so in the dispatch update.
5. Launch all three in one message, `impl` first, each under <ToolingContract/>.
   The fourth and sixth arguments are the seat's opening role, the same word in
   both places; the default opening is:

   ```sh
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/implementation_prompt.md" impl \
     "<responsibility>" impl "<activity>" 0 impl
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/test_prompt.md" test \
     "<responsibility>" test "<activity>" 0 test
   implement.sh "${SESSION_DIR}" "${WORKING_DIR}" \
     "${SESSION_DIR}/review_prompt_team.md" review \
     "<responsibility>" review "<activity>" 0 review
   ```

   A seat Seats opens in another role swaps both words and nothing else — the
   `review` seat opening as a writer is
   `implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/review_prompt_team.md" impl "<responsibility>" impl "<activity>" 0 review`.
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
contained diff; that judgment is the orchestrator's own reading of the batch, as
no task name carries it. When the two estimates disagree, or either is a guess,
do nothing; the synchronous path still exists.

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
4. Write the applicable early-form prompt — <BroadReviewPrompt/> for pass 1,
   <ClosureReview/> for a fix — including the completion estimate, the partial
   diff, and the exact final-diff and ready-sentinel paths below.
5. Launch `review.sh` exactly as <DualReview/> step 3 does, appending one extra
   final argument: `${SESSION_DIR}/final_diff_${REVIEW_PASS}.ready`. Save the
   handle as `${REVIEW_DISPATCH_HANDLE}` and set `EARLY_REVIEW=launched`. Leave
   `${DISPATCH_HANDLE}` and the tick's timer re-arm untouched.
6. Before the recorder call in <ProgressContract/> step 5, put the reviewer in
   the round table:

   `python3 ~/.claude/scripts/delegate/progress_history.py arm-review --session-dir "${SESSION_DIR}" --activity "<what this reviewer is checking>" --called-task delegate.review`

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
| Primary errors, or the run stops | Kill the early reviewer. If the sentinel exists, close its pass with `finish-pass --status canceled --orphaned-launcher` per <PassOwnership/>; before the sentinel no pass was recorded, so record nothing — a pre-sentinel kill counts toward no advisory, including the blind-review cancellation one. |
| Reviewer errors before delivery | Report it in one line, clear `${REVIEW_DISPATCH_HANDLE}`, set `EARLY_REVIEW=none`, and leave the numbered artifacts. The primary continues and is reviewed synchronously at completion under the next `${REVIEW_PASS}` index. |
| A verdict arrives before delivery | **It is void.** The reviewer cannot have read a diff that does not exist yet, so discard its findings entirely rather than reading them as evidence: open nothing in the ledger, preempt nothing, route no blocker into a fix dispatch. Say in one line that it is discarded and why, then follow the reviewer-error row. A void verdict is often fluent and specific — a stale diff supports confident claims about missing work — so the check is the timing, never how convincing the text reads. |

Every path that ends an early launch before its real pass starts also drops the
row it was given:

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

**Close every delegation result with the current progress header** — both tables
and the wall-clock line, produced by <ProgressContract/> steps 3 and 5 with the
current pass or activity. This is unconditional: the numbered items say what
happened, and the tables say how far into the phase and the plan it happened,
which is the half the user cannot reconstruct. Emit it after any launch and
after the timer is armed, printed below the sections above exactly as the
recorder emits it. Should the recorder answer that no window is open, the
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

A repair runs the same three slots as a phase, per <PhaseTeam/>. The default
opening is `fix` / `test` / `review`: `impl` makes the repair, `test` writes
the regression test that would have caught each finding, and `review` opens
adversarially on the repair under <TeamReview/> — the reading most likely to
catch a fix that closes a finding by weakening what detects it. Partition the
findings' files per <TeamFilePartition/> and write one prompt per slot; `test`
covers the same batch ids from the outside. When the batch's files partition
into disjoint sets, `review` opens as a second `fix` seat — task and kind both
`fix` — and the dispatch update says so.

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

Each seat records the work it was assigned: a repairing seat `fix`, the test
seat `test`, the review seat `review`.

**`PLAN_DELEGATE_RESOLVES_ROUND=1` goes on exactly one seat**, whichever
opening is in play. Only the launcher
watches the worker exit, so only a launcher can say a repair landed; two seats
carrying the signal would resolve one round twice over. The signal is separate
from the pass kind precisely so a second repairing seat can record `fix`
honestly without performing the resolution.

Apply <DispatchContract/>; set `EARLY_REVIEW=none` at dispatch, and close the
turn with the progress header per <DelegationResultFormat/>. While a fix runs
that will receive a delegate closure review, <EarlyReviewArm/> may arm that
reviewer early.

**A contained repair closes without a delegate review.** Apply
<ReviewDiffContract/> and read the repair diff yourself. When every hunk sits in
a path the batch's own findings named — or in a new file one of those paths
creates — record each verdict directly and continue to <Synthesize/>.

Dispatch the normal <DualReview/> closure review whenever the repair leaves that
boundary or the diff cannot answer the question: an edited path no finding
named, a caller or consumer pulled in, a changed signature, registration, or
invariant reaching past the batch, an id whose verdict reads unclear, or a new
defect the repair introduced. Uncertainty routes to the reviewer. Judge the
paths the diff touched, never how confident the reading felt.

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
Read `~/.claude/commands/plan/delegate_style.md` in full and apply it. This is
the run's single style audit, over everything the project built rather than one
phase: `single` runs it after first smoke, loop and verbose from <FinalGate/>.
Phases never run it — they carry no style gate, and a phase checkpoint never
waits on one. Never run it from memory of an earlier read; the after-cleanup
reverification and the smoke reset are what get dropped. The user can invoke the
same file as `/plan:delegate_style`.
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
Read `~/.claude/commands/plan/delegate_next.md` in full and apply it after
shrink, at each phase boundary. Phased plans only; the main agent performs the
assessment and never launches another agent for it. Never work from memory of an
earlier read — `Class` obedience and the single-line reporting rule are what
drift. The user can invoke the same file as `/plan:delegate_next`.
</ConsiderNextItems>

<CheckpointCommit>
Read `~/.claude/commands/plan/delegate_checkpoint.md` in full and apply it once
per completed phase. Loop and verbose only; `single` never commits.

This is durable state with no cheap undo, so read the whole contract before
acting on any part of it, and read the reservation record from disk. A value
remembered from conversation, taken from the harness session mapping, or
re-derived from current `HEAD` is not proof and will silently accept the wrong
checkpoint. The user can invoke the same file as `/plan:delegate_checkpoint`.

Never push here. The phase does not complete until the reservation release is
confirmed, so a failed or busy release applies
<RetainDelegatedPhaseReservation/> rather than a retry.
</CheckpointCommit>

<DiscardPhaseReviewText>
After <RunPhaseShrink/> and a successful checkpoint when one applies, run:

`bash ~/.claude/scripts/delegate/clear_phase_review.sh "${SESSION_DIR}" <phase-id>`

The script removes only this phase's review prose; structured progress history
remains. Do not clear before shrink succeeds or while a checkpoint can still
fail.
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
Read `~/.claude/commands/plan/delegate_phase_report.md` in full and apply it
after a completed verbose phase outside an auto window. Inside an active window,
emit no per-phase report; when the window's last phase completes, emit one
combined report instead. That file defines this contract,
<CombinedWindowReport/>, and <RemainingWorkOutlook/>. Never compose the report
from memory of an earlier read — the phase count and the closing control line
are what go missing. The user can invoke the same file as
`/plan:delegate_phase_report`.
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
**Why the run stopped:** [complete, user stop, pending decision, or error]
```

Apply <UserFacingText/> and <RetainDelegatedPhaseReservation/> for every ending
that did not complete <CheckpointCommit/>. Then run `progress_history.py finish-run` with
`completed`, `stopped`, or `error`; it closes active pass/phase as incomplete
when needed. Finally run `bash ~/.claude/scripts/delegate/end_session.sh` on
every exit so the Stop hook cannot revive a finished run.
</RunSummary>
