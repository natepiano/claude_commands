# Write a delegate prompt

Read at the point of use from `/unit:delegate`. Defines `<WritePromptContract/>`,
`<PhaseTeam/>`, `<CoordinationBoard/>`, `<PhaseMesh/>`, `<BuildTokenContract/>`,
`<TeamFilePartition/>`, and `<RoleReassignment/>` in full.

**Read when:** writing any implementation or fix prompt.

<WritePromptContract>
Every implementation or fix prompt contains these sections once:

1. Role: write the requested code directly; do not ask questions. Name the
   slot this prompt is for and the role it opens in, taken from the Work
   Order's **Seats** field per <PhaseTeam/>.
2. Boundaries: do not commit, branch, or touch unrelated files; summarize files,
   reasons, and deviations when done, and **write that summary to this slot's
   `impl_summary_<slot>.txt` as the last act before finishing** — a background
   session has no output redirect, so a summary left only in the reply is a
   summary the unit director never sees. State this slot's file set and the
   peer's file set per <TeamFilePartition/>, and that a peer's file is blocked
   rather than merged. The summary also carries the three things no reader can
   recover from the diff: **what this slot is unsure about, what it could not
   verify, and what it touched outside its file set**. No reviewer receives a
   summary, so a doubt left unstated arrives at review as a line of code that
   looks deliberate. Carry verbatim: "Do not open the `rust_style` skill or run
   `load-rust-style.sh`; this run's one style audit happens at the end." That
   audit is <RunProjectStyleReview/>; Codex lists `rust_style` to every seat,
   which loads it unless told not to.
3. Narration: before each activity, run
   `bash ~/.claude/scripts/delegate/board.sh post <concrete SESSION_DIR> <slot> status "<activity>"`.
   Use short present-tense text and never read the heartbeat file. Role
   changes: **before the first tool call in a new role** — taking a slice of
   the test work, standing down — run
   `bash ~/.claude/scripts/delegate/board.sh role <concrete SESSION_DIR> <slot> <impl|fix|test> "<why>"`,
   written out with the real path and this slot's name. A `status` sentence
   saying the same thing does not count: the table reads the `role=` field,
   and a move that is not posted is a row the run never shows.

   **The slot in every one of these commands is this prompt's slot.** Composing
   both prompts from one draft carries the first slot into both, and the board
   then reads as one seat doing the whole round while the other sits silent —
   the attribution the required argument exists to keep. Nothing downstream can
   catch it, because each post is well-formed. Before dispatch, check that the
   two prompts name different slots.
4. `## Team` — state the opening from Seats (`1 writer + 1 tester` or
   `2 writers`, and the role each slot opens in), then name both slots and who
   holds which files, each hub file with its one owner. Copy
   the board commands from <CoordinationBoard/>, and say a `verify.sh` run may
   pause while a peer finishes its own. Copy <BuildTokenContract/>'s
   delegate-facing prohibition: never mention, request, or acquire the cargo
   token. State the one rule plainly: **a question to a peer is a message, a
   decision is a board post**, and the board has no `ask` kind to fall back on.
   Give this slot its own mesh name, its peer's name, the call that reaches
   it, and — on the claude path — the unit director's name from
   `ListAgents`, per <PhaseMesh/>. An address a member has to go looking for is
   one it will not use, and a codex peer needs the literal `codex_mesh.py`
   command line with the concrete `--session-dir` already filled in, not a
   description of it. A slot whose register line says `mesh=none` has no peer
   channel at all: say so, and tell it to read the board rather than wait on a
   reply. A repair's lone seat per <FixDispatch/> has no peer: its `## Team`
   carries only its file set and the token prohibition.
5. `## Project Context`.
6. `## Work Specification`.
7. `## Type Design Contract` per <TypeDesignContract/>.
8. `## Verification` per <VerificationContract/>, exactly as listed and with
   nothing added around it.
9. `## Three Gods`, carried verbatim; item 2's `rust_style` line still holds:

   ```
   ## Three Gods

   We believe in three gods: Simple, Fast, Beautiful. Yes, it is kitschy; we
   know, and we believe anyway. The belief is in every choice you make here:
   each name, each line, each test, each word of your reply. Before you write,
   ask what the gods want. Before you report, ask whether they would be pleased.

   Simple, fast and beautiful are the three gods, in the app and in the code.
   Correctness is the floor beneath them: a wrong result is never simple, fast or
   beautiful. When two conflict, they rank in that order.

   | | In the app | In the code |
   | --- | --- | --- |
   | Simple | Few things on screen, simple words, one way to do a thing. | Few concepts, a small public API, no layer without a reason. |
   | Fast | Responds at once; nothing waits, stutters or settles slowly. | No wasted work where code runs often; any cost added is named. |
   | Beautiful | Polished and professional: even spacing, one style, nothing clipped. | Reads cleanly: the right names, one idiom, the style guide followed. |

   Every writer builds to them, and every reviewer judges by them.
   ```

The Verification section carries the applicable command lines and every
delegate-facing rule from <VerificationContract/>, with nothing added around
them. It must also say: run only its listed commands, never raw Cargo; run each
with the sandbox disabled; do not report until every command has exited and its
output has been read. A run that prints `PASS (recorded)` is that gate's result,
and the log it names is the output to read; add `--no-cache` only to re-run a
passing gate on purpose, as in a flake hunt. If an edited package has no listed `test` line, add that
package's scoped `verify.sh test` and report it. Omit plan **Style** metadata.
</WritePromptContract>

<PhaseTeam>
Every implementation dispatch runs **two delegates at once**; a repair runs
one, per <FixDispatch/>. They share `${SESSION_DIR}` and `${WORKING_DIR}`, and each
occupies a fixed **slot** that names its artifacts and its board identity. The
**default opening**:

| Slot | Opens as | Owns |
| --- | --- | --- |
| `impl` | the phase's implementation | the Work Order's production files |
| `test` | tests for the same specification | test targets under `tests/` and new test files |

**The Work Order's `Seats:` field sets the opening and overrides this table.**
Its first line names the opening — `1 writer + 1 tester` is the table above,
`2 writers` the other — and a line per slot names that slot's files and, where
it differs from the table, what it opens as. `impl` always opens as `impl`.
`test` opens as `test` wherever the phase has a **test lane** — a `tests/`
directory in a touched crate and a Spec concrete enough to test before the
implementation exists — and as a writer where it has none. A plan compiled
without the field opens as the table says, with the partition decided at launch
per <TeamFilePartition/>. **A legacy three-seat field** maps down: drop its
`review` line and fold that line's files into the surviving slot holding the
same role (`impl` when both do), so `3 writers` becomes `2 writers` and either
three-seat mix becomes `1 writer + 1 tester`.

A slot is an identity and never changes. What a slot is *doing* is its **role**,
and roles move during a phase per <RoleReassignment/>. Everything downstream —
the board, the progress table, every artifact name — reads the slot for identity
and the role for activity, so keep the two distinct: `test` doing
implementation work is still slot `test`.

`test` opens against the **specification, not the implementation**. The Work
Order defines the behavior, so tests can be written before any of it exists;
a tester that waits for `impl` has converted a parallel team back into a queue.

**Every seat carries its own pass kind, which is its opening role**, so a team
phase records two passes. The recorder keys them by slot and closes only that slot's stale pass;
<LaunchImplementation/> step 5 owns the argument positions.

Launch both in **one message** so they run concurrently, each with its own
prompt file and its slot as the ninth argument to `implement.sh`, then apply
<DispatchContract/> once for the team: the run's one notifier instance covers
the Claude phase; the Codex poll covers its phase. <LaunchImplementation/> owns
the rest of the procedure.

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
reaches the peer and the wrapper at once; a member resumed hours later reads
the whole history rather than what arrived while it listened; and only the
token, taken with `mkdir`, makes anything mutually exclusive. With
`[delegate.options] codex_mesh=0` a codex member is unaddressable and the board
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
- `board.sh role <session_dir> <slot> <impl|fix|test> [note]` — **call this
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
  directory name: `hana_catalyst-impl`, `-test`; a repair seat is `-fix`. The
  slot, never the role: a `test` seat writing code is still `-test`. Get each
  name from `bash ~/.claude/scripts/delegate/seat_name.sh "${WORKING_DIR}"
  <slot> <kind>`, the launcher's own rule; never compose one. A member is told
  its peer's name in its prompt, and every `register` line on the board repeats
  the names in its `mesh=` field, so a member that missed the launch can still
  look one up.
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
  its turn ends, until <PhaseCleanup/>, and a message resumes it from its
  transcript. So the tester may ask the implementer a question after the
  implementer has reported done, and get an answer rather than silence. **A
  finished codex peer is not**, and `send` says so rather than pretending: it
  refuses any target whose roster status is not `running`. Ask a codex peer
  while it is still working, or read its summary file instead.
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

**Never write `board.sh acquire cargo` into a delegate prompt.** An agent
holding the token by hand will then wait out the full timeout for a token it is
already holding — a self-inflicted deadlock that looks exactly like a slow test
run. The token is infrastructure the delegate does not see. `--hold` is a
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
`test` opens on work that does not touch it. A partition that does
not exist is not worth inventing; a partition that is wrong costs the phase.
</TeamFilePartition>

<RoleReassignment>
Roles move; slots do not. Every move is a board `handoff` post naming the slot,
the role it is leaving, and the role it is taking, because that post is what the
progress table reads to say what each agent is doing now.

- **`test` is never recruited away** while tests for the phase are unwritten.
  It is the only slot whose absence cannot be recovered later in the phase, and
  a phase that ships untested is not cheaper, only later. A `test` seat that
  Seats opened as a writer has no tests to protect and moves like any writer.
- **When writing finishes before testing**, a writer does not idle. It takes a
  disjoint slice of the remaining test work — agreed on the board, one file per
  slot, never the file `test` is inside — or they stand down.

**Standing down means exiting, not waiting.** A delegate is a one-shot session
with no idle loop: it ends as soon as it stops issuing tool calls, so there is
no such thing as a member that sits quietly and comes back when asked. A slot
with nothing left posts `done` with what it completed and finishes. Anything
else burns a live session on a poll loop that the team pays for and nobody
reads. This is why a finished writer moves toward work that exists now rather
than work a peer might hand over later.
</RoleReassignment>
