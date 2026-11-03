# Write a worker prompt

Use from `/unit:direct` for implementation, fix, test, and review prompts.

<WritePromptContract>
Include sections 1–10 in every implementation, test, and repair prompt.
Make each prompt complete after compaction. Keep sections 9 and 10 inline.
A follow-up may use the section 7 pointer.
Review prompts include sections 7 and 9 and the <ReviewPromptContract/> opening.
Codex review prompts also include section 3's command block.

1. Role: instruct the worker to write code directly without questions.
   Name its slot and Work Order role under <PhaseTeam/>.
   Continue an existing thread with: `You are slot <slot>, continuing into phase <N> as <role>. Your earlier work is committed.`
   Use opening wording for a replacement thread.
2. Boundaries: prohibit commits, branches, and unrelated edits.
   State both file sets under <TeamFilePartition/>. Block changes to a peer's files.
   Require the summary as the last act at the full `${PHASE_DIR}/impl_summary_<slot>.txt` path.
   Include files, reasons, deviations, doubts, unverified checks, and edits outside the file set.
   Include this instruction verbatim in implementation, fix, and test prompts:
   "Do not open the `rust_style` skill or run `load-rust-style.sh`; the reviewer worker owns the phase style review."
   Use <ReviewPromptContract/> for the reviewer's style assignment.
3. Narration: require this command when the activity changes, with the concrete path and assigned slot:
   `bash ~/.claude/scripts/direct/board.sh post <concrete SESSION_DIR> <slot> status "<activity>"`.
   Use short present-tense text. Do not read the heartbeat file or post unchanged activity.
   Include this command block verbatim after section 2 in every Codex prompt:

   ```
   ## Builds and tests

   Run only commands that finish within one call. Never make an empty
   write_stdin poll, for any command, build or not. Anything longer runs
   detached: each verify.sh line as listed, which detaches itself, or any
   other command as
   `python3 ~/.claude/scripts/agents/mesh_runs.py start --label "<what>" -- <command>`.
   A successful start returns at once with the run, its pid and its log.
   A live twin takes priority over config/mesh_runs.conf LIVE_RUN_LIMIT.
   Exit 4 refuses a new run at that limit and names the live runs.
   Wait for a run's result, or put more files in one run. Never run attached after exit 4.
   Exit 3 means the caller is not a mesh worker; run attached only for that exit.
   Keep working if you have other work; otherwise end your turn. The result
   (pass or fail, exit code, log path, last lines) arrives as your next
   message, also after an account move or a stop. Never poll, sleep, call
   `wait`, loop on a status file, or post a waiting status.
   Status 77 means cancelled because the tree changed: resubmit after your
   edits are done; it is not a test failure.
   When a later edit or result makes a run of yours useless, cancel it in
   the same turn: take `pid` from the run's record (the `.json` beside its
   log), check that `ps -p <pid> -o args=` still shows
   `mesh_runs.py supervise` with that record's path, then run
   `kill -TERM -- -<pid>`.
   ```

   Claude workers use `run_in_background: true` and end the turn until the result arrives.
   Before a role change, require `bash ~/.claude/scripts/direct/board.sh role <concrete SESSION_DIR> <slot> <impl|fix|test|review> "<why>"`.
   Use the concrete path and the prompt's slot. Check that different prompts name different slots.
4. `## Team`: copy the Workers assignment and file ownership, including one owner for each hub file.
   Copy the <CoordinationBoard/> commands. State that `verify.sh` may pause for a peer.
   Prohibit cargo token requests, acquisition, and discussion under <BuildTokenContract/>.
   Send questions by message. Record decisions on the board. State each source when passing information.
   Label the user's words only when the unit director relays them.
   Include each mesh name and its concrete contact command under <PhaseMesh/>.
   Include the unit director's `ListAgents` name for Claude workers.
   For `mesh=none`, state that no peer channel exists and require the worker to read the board.
   For repairs, list the file set, other workers' files as read-only, and the token prohibition under <FixDispatch/>.
5. `## Project Context`: include the current context. Omit plan **Style** metadata.
6. `## Work Specification`: include the following instructions verbatim.
   "Do not weaken, delete or turn off an existing assertion or test to make a test pass. If one must change, post blocked with the test, the old assertion, the new assertion and why."
   The next Work Order rejects the change or records `Assertion-Change: <path>::<test> — <why>` for `assertion_guard.py`.
   For tests, prohibit constant comparisons, implementation copies, library tests, configured mock tests, existence checks, and duplicate behavior tests.
   Include these repair rules:
   - Remove dead code and unused bindings. Do not suppress warnings or prefix unused names with `_`.
   - Before a rename or signature change, use LSP `findReferences`. Update all references and reread the changed body.
   - Before a clippy repair, run `grep -l "^lint:.*\b<lint>\b" ~/rust/nate_style/rust/*.md docs/style/*.md`.
   - Read matching rules, apply them, and cite them in the summary.
   - If no rule matches, report that fact and use the finding and nearby code.
   - After a `cargo mend --fix` rollback, stop and keep the tree for inspection.
7. `## Type Design Contract`: copy <TypeDesignContract/> verbatim in opening prompts.
   A follow-up may say: `Read ~/.claude/docs/type_design.md in full and apply its Type Design Contract to this work.`
8. `## Verification`: copy <VerificationContract/> commands and worker rules without extra requirements.
   Do not require lint before tests. Permit only listed commands, with the sandbox disabled, and prohibit raw Cargo.
   Require every result and its output before the final report. For Codex, require results from section 3's runs.
   Read the named log for `PASS (recorded)`. Add `--no-cache` only for an intentional repeat, such as a flake investigation.
   Add the package's scoped `verify.sh test` when an edited package has no listed test command. Report the addition.
9. Include this section verbatim:

   ```
   ## Three Gods

   We believe in three gods, Simple, Fast and Beautiful, with correctness as the
   floor beneath them.
   ```
10. Include this section inline in every implementation, test, and repair prompt:

    ```
    ## Build rules

    1. Fix every visible failure before you rerun. Finish the edit round. Then run once.
    2. After a pass, do not run again until your edits are done.
    3. While editing, use `verify.sh check`. Lint runs once, at the phase gate.
    ```

    Run the phase and merge gates in full. Require each worker's listed tests before its checkpoint.
</WritePromptContract>

<PhaseTeam>
Every phase's implementation dispatch assigns **two workers at once**; a
repair assigns one per disjoint file set under <FixDispatch/>. They share
`${SESSION_DIR}` and `${WORKING_DIR}`, and each occupies a fixed **slot** that
names its artifacts and board identity. Under <LongLivedWorkers/>, the first
phase opens the Codex threads and later phases continue each thread that remains
open; a replacement starts fresh with an opening prompt. The **default
assignment**:

| Slot | Opens as | Owns |
| --- | --- | --- |
| `impl` | the phase's implementation | the Work Order's production files |
| `test` | tests for the same specification | test targets under `tests/` and new test files |

**The Work Order's `Workers:` field sets the assignment and overrides this table.**
Its first line names the assignment — `1 writer + 1 tester` is the table above,
`2 writers` the other — and a line per slot names that slot's files and, where
it differs from the table, its role. `impl` is `impl` by default. `test` is
`test` wherever the phase has a **test lane** — a `tests/`
directory in a touched crate and a Spec concrete enough to test before the
implementation exists — and as a writer where it has none. A plan compiled
without the field assigns the table's roles, with the partition decided at launch
per <TeamFilePartition/>. **A legacy three-worker field** maps down: drop its
`review` line and fold that line's files into the surviving slot holding the
same role (`impl` when both do), so `3 writers` becomes `2 writers` and either
three-worker mix becomes `1 writer + 1 tester`.

A slot is an identity and never changes. What a slot is *doing* is its **role**,
and roles move during a phase per <RoleReassignment/>. Everything downstream —
the board, the progress table, every artifact name — reads the slot for identity
and the role for activity, so keep the two distinct: `test` doing
implementation work is still slot `test`. Its Codex thread stays open across
phase boundaries. Keep its role when the next Work Order fits, otherwise move
it under <RoleReassignment/>. Open another thread only when no open worker can
take the assignment, including when every open worker is busy; name its slot for
its work (`review`, `fix2`), keep it for reuse, and end it only with
`end_session.sh`.

`test` opens against the **specification, not the implementation**. The Work
Order defines the behavior, so tests can be written before any of it exists;
a tester that waits for `impl` has converted a parallel team back into a queue.

**Every worker carries its own pass kind, which is its assigned role**, so a team
phase records two passes. The recorder keys them by slot and closes only that slot's stale pass;
<LaunchImplementation/> step 5 owns the argument positions.

Launch both in **one message** so they run concurrently, each with its own
prompt file and its slot as the ninth argument to `implement.sh`; continue an
open Codex worker with `--to <full-worker-name>`, then apply
<DispatchContract/> once for the team: the run's one notifier instance covers
the Claude phase; the Codex poll covers its phase. <LaunchImplementation/> owns
the rest of the procedure.

The phase is complete only when every slot has a terminal `${PHASE_DIR}/impl_status_<slot>`,
not when the first one lands. Reading one slot's `implemented` as the phase's
result is the same defect as reading a completion notification as a finished
assignment. Completion ends the turns, not their Codex threads.
</PhaseTeam>

<CoordinationBoard>
The team coordinates through `${SESSION_DIR}/board.log`, written only with
`bash ~/.claude/scripts/direct/board.sh`.

**Why a file even when messages work.** Every member is reachable by name on
both paths — see <PhaseMesh/> — but the board is the durable broadcast record
and the token owner, where messages are addressed and transient. One post
reaches the peer and the wrapper at once; a member resumed hours later reads
the whole history rather than what arrived while it listened; and only the
token, taken with `mkdir`, makes anything mutually exclusive. With
`[direct.options] codex_mesh=0` a codex member is unaddressable and the board
is its only channel, since the unit director is asleep between progress ticks and
cannot relay. Each `register` line says which case holds, in its `mesh=` field.

- `board.sh post <session_dir> <slot> <kind> <message>` — one broadcast line.
  Kinds are a closed set: `register`, `claim`, `release`, `status`, `blocked`,
  `handoff`, `done`. There is no `ask` and no `answer`, and the command rejects
  both: a question to a peer is a message, per <PhaseMesh/>.
- `board.sh read <session_dir> --since <cursor>` — everything new. Each line is
  numbered; keep the last number as the cursor. Read after acquiring a token,
  and whenever you need what a peer has recorded rather than what it would say
  if asked — the role it holds now, whether it has posted `done`.
- `board.sh role <session_dir> <slot> <impl|fix|test|review> [note]` — **call this
  the moment your slot starts doing something other than what it is named
  for.** A slot is a fixed identity and its role is not: a writer that takes a
  slice of the remaining test work is doing `test`. The launcher stamps the opening role, so
  the table is never blank;
  after that only this command keeps it true, and saying it in a `status`
  sentence does not count — the table reads the field, not prose. **Every call
  adds a row**, so a change you do not post is a shape the run never shows, and
  the row above it silently claims your old role held the whole time.
- **One way to do each thing.** A question goes by message; a decision goes on
  the board. A decision that is not on the board did not happen, however plainly
  it was settled in messages — the board is what a peer resuming later, and the
  unit director at its next tick, actually read.

Narration goes through the board too: `board.sh post` takes the slot as a
required argument, so attribution cannot be dropped, where a name an agent is
merely asked to prefix onto a heartbeat line reliably goes missing.
</CoordinationBoard>

<PhaseMesh>
A member launched into the mesh is **addressable**: peers reach each other, the
unit director reaches any of them, and a claude member reaches the unit director —
mid-run, without waiting for a phase to end.

- **Addresses** are `<project>-<slot>`, the project being the working tree's
  directory name: `hana_catalyst-impl`, `-test`; a repair worker is `-fix`. The
  slot, never the role: a `test` worker writing code is still `-test`. Get each
  name from `bash ~/.claude/scripts/direct/worker_name.sh "${WORKING_DIR}"
  <slot> <kind>`, the launcher's own rule; never compose one. A member is told
  its peer's name in its prompt, and every `register` line on the board repeats
  the names in its `mesh=` field, so a member that missed the launch can still
  look one up.
- **How you reach a name depends on its family**, and the register line says
  which in its `reach=` field. Using the wrong call fails silently: the message
  goes nowhere and the sender waits on a reply that was never queued.
  - `reach=SendMessage` — a claude member, running as a named background
    session. Address the bare name; `ListAgents` confirms who is live.
  - `reach=codex_mesh.py` — a codex member, running as a thread on the run's
    `codex app-server`. Two calls, both with
    `--session-dir <concrete SESSION_DIR> --to <name>`:
    `python3 ~/.claude/scripts/agents/codex_mesh.py send --message "<text>"`
    queues the message and it lands at the start of that worker's next turn;
    `… steer --message "<text>"` interrupts the turn it is running right now.
    Send by default. Steer only when the work in flight is work you need
    stopped — it costs the worker whatever it was mid-way through.
    `… list --session-dir <dir>` prints the roster; each entry carries `role`,
    `status` (including terminal `"ended"`), and `context_tokens`.
  - `mesh=none` — that member has no address. Do not wait on a reply from it;
    read its board posts instead.
- **A finished claude peer is still reachable.** Its session stays alive after
  its turn ends, until <PhaseCleanup/>, and a message resumes it from its
  transcript. So the tester may ask the implementer a question after the
  implementer has reported done, and get an answer rather than silence. **A
  finished Codex turn holds `send` for its next turn.** Its thread remains open.
  Only the unit director starts its next turn — a later phase, repair, or
  follow-up — through `implement.sh --to <worker>`. That turn receives the held
  messages first and its prompt last. The launcher attempts compaction above
  `UNIT_COMPACT_ABOVE_TOKENS`, continues after a failed attempt, and
  presses on after overflow per <LongLivedWorkers/>.
- **A codex member has no route to the unit director.** It reaches its peer with
  the calls above and reaches the unit director only through the board, which the
  unit director reads at every progress tick. Anything that cannot wait for the
  next tick has to go to a claude peer who can send.

**What to send, and to whom.** Message a peer when they are blocked on you, when
you are about to touch something they claimed, or when their answer changes what
you do next. Message the unit director when the *user* needs to know something now
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
Both agents share one `target/` directory and one Cargo lock, so an
uncoordinated `verify.sh` run blocks its peer for minutes while holding
nothing useful.

**`verify.sh` takes the `cargo` token itself, and no prompt ever asks an agent
to take it.** `implement.sh` exports the board directory and the slot;
`verify.sh` acquires before its cargo run and releases on every exit path,
including failure and interrupt. A rule that lives only in a prompt is a rule an
agent can drop, so it is enforced where the cargo command actually runs.

**Never write `board.sh acquire cargo` into a worker prompt.** An agent
holding the token by hand will then wait out the full timeout for a token it is
already holding — a self-inflicted deadlock that looks exactly like a slow test
run. The token is infrastructure the worker does not see. `--hold` is a
deadline, not a reservation, so a member killed mid-hold strands nobody behind
its lock. The unit director may inspect holders with
`board.sh locks "${SESSION_DIR}"` when a phase looks stalled.

**A green run only means what the tree it ran against means.** The peer is editing
throughout, so a result is authoritative for a package only once the slot that
owns that package's files has posted `done`. Before that it is early signal:
post it as `status`, never close a finding on it, and say which it is when
reporting — a passing suite over a half-written tree is the most expensive kind
of false confidence, because everything downstream treats it as a gate that has
already been cleared.
</BuildTokenContract>

<TeamFilePartition>
The slots edit **disjoint file sets**, decided in the Work Order's `Workers:`
field — or at launch, when a plan predates it — and stated in every prompt. A
**hub file** — a `lib.rs` or `mod.rs` re-export, `Cargo.toml`, plugin
registration, a shared types file — has exactly one owner, named on that slot's
Workers line; every other writer messages the owner for the line it needs.

This is enforced, not merely agreed: the cargo-berth pre-edit hook claims paths
per session, each worker is its own session, so an edit into a peer's claimed
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
two files — the Workers opening line says so, `impl` gets the whole set, and
`test` opens on work that does not touch it. A partition that does
not exist is not worth inventing; a partition that is wrong costs the phase.
</TeamFilePartition>

<RoleReassignment>
Roles move; slots do not. Any open worker may take any role, mid-phase included,
because every worker in a unit works in the same area. Before dispatch, post a
board `handoff` naming the slot, the role it leaves, and the role it takes; the
new prompt has that worker run `board.sh role` before its first tool call.

A busy turn does not reserve its worker's next role. Reassign it when that turn
ends; when the new work must run concurrently and every open worker is busy, open
an extra worker named for its work (`review`, `fix2`) and keep it for reuse. Never
abandon unwritten tests: assign them to an open worker or open one.

**Standing down means ending the turn, not waiting.** A slot with nothing left
posts `done` with what it completed and finishes its turn; it never runs an idle
poll loop. Its Codex thread stays open for the unit director's next
launcher `--to` assignment and ends at `end_session.sh`. This is why a finished
worker moves toward work that exists now rather than work a peer might hand over
later in the same turn.
</RoleReassignment>
