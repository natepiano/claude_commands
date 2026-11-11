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
   For a retained conversation, state its slot, current phase, and assigned role.
   Use opening wording for a replacement conversation.
2. Boundaries: prohibit commits, branches, and unrelated edits.
   State both file sets under <TeamFilePartition/>. Block changes to a peer's files.
   Require the summary in the final reply, because the launcher saves that reply as the summary.
   Include files, reasons, deviations, doubts, unverified checks, and edits outside the file set.
   Implementation, fix and test prompts carry verbatim:
   "Do not open the `rust_style` skill or run `load-rust-style.sh`; the reviewer worker owns the phase style review."
   Review prompts use
   <ReviewPromptContract/>'s style assignment instead.
3. Narration: require this command when a herdr pane worker's activity changes:
   `bash ~/.claude/scripts/direct/board.sh post <slot> status "<activity>"`.
   For a read-only pane reviewer, require short output lines instead of board posts.
   Assign its required suites to the director or a writer.
   Use short present-tense text. Do not read the heartbeat file or post unchanged activity.
   Include this command block verbatim after section 2 in every Codex prompt:
   Merge prompts replace only the exit 3 line under <MergeWorkerCommands/>.

   ```
   ## Builds and tests

   Run only commands that finish within one call. Never make an empty
   write_stdin poll, for any command, build or not. Anything longer runs
   detached: each verify.sh line as listed, which detaches itself, or any
   other command as
   `~/.claude/scripts/lib/py ~/.claude/scripts/agents/detached_runs.py start --label "<what>" -- <command>`.
   A successful start returns at once with the run, its pid and its log.
   A live twin takes priority over config/detached_runs.conf LIVE_RUN_LIMIT.
   Exit 4 refuses a new run at that limit and names the live runs.
   Wait for a run's result, or put more files in one run. Never run attached after exit 4.
   Exit 3 means the caller has no registered worker route; run attached only for that exit.
   Keep working if you have other work; otherwise end your turn. The result
   (pass or fail, exit code, log path, last lines) arrives as your next
   message, also after an account move or a stop. Never poll, sleep, call
   `wait`, loop on a status file, or post a waiting status.
   Status 77 means cancelled because the tree changed: resubmit after your
   edits are done; it is not a test failure.
   When a later edit or result makes a run of yours useless, cancel it in
   the same turn: take `pid` from the run's record (the `.json` beside its
   log), check that `ps -p <pid> -o args=` still shows
   `detached_runs.py supervise` with that record's path, then run
   `kill -TERM -- -<pid>`.
   ```

   Claude workers use `run_in_background: true` and end the turn until the result arrives.
   Before a role change, require this command from herdr pane writers:
   `bash ~/.claude/scripts/direct/board.sh role <slot> <impl|fix|test|review> "<why>"`.
   The director records a pane reviewer's role change.
   Use the prompt's slot. Check that different prompts name different slots.
   Omit the run folder from pane worker board commands. They read `UNIT_RUN_DIR`.
4. `## Team`: copy the Workers assignment and file ownership, including one owner for each hub file.
   Copy the <CoordinationBoard/> commands. State that `verify.sh` may pause for a peer.
   Prohibit cargo token requests, acquisition, and discussion under <BuildTokenContract/>.
   Send questions by message. Record decisions on the board. State each source when passing information.
   Label the user's words only when the unit director relays them.
   Include each worker address and its concrete contact command under <CoordinationBoard/>.
   If a member has no message route, require it to read the board.
   For repairs, list the file set, other workers' files as read-only, and the token prohibition under <FixDispatch/>.
5. `## Project Context`: include the current context. Omit plan **Style** metadata.
6. `## Work Specification`: include the following instructions verbatim.
   "Do not weaken, delete or turn off an existing assertion or test to make a test pass. If one must change, post blocked with the test, the old assertion, the new assertion and why."
   For tests, prohibit constant comparisons, implementation copies, library tests, configured mock tests, existence checks, and duplicate behavior tests.
   Implementation, fix and test prompts carry these two test rules verbatim:

   1. For a fix, add a table row to the existing test of the same behavior.
      Add a new test only when no test covers the behavior.
   2. Write or keep a rare-case test only when its defect meets at least one condition below.
      Rare cases include an edge case, a refusal, malformed input, and a failure path.

      - The user sees a wrong result.
      - The defect damages saved state, such as a file, Git, a ledger, staff, or a config.
      - The defect loses work, such as a commit, an edit, a message, or a worker result.

      Keep every main-path test.

   For tests, include this instruction verbatim: "Never commit a test project to the repo. A test makes the project it needs in a tempdir at run time (Rust tempfile::TempDir, Python tempfile.TemporaryDirectory), which removes it after."
   Every implementation, fix, test, research and review prompt carries this instruction verbatim:
   "Make every folder you need for your own work (a clone, a copy, a download, a trial project) with `~/.claude/scripts/lib/scratch.py new --worker [--name <word>]`. It prints the path, and the folder is removed when your assignment ends. Never make a folder under `/tmp` or `~/rust` by hand."
   "A folder or file you make on another machine (the Mac) follows the same rule. Make it in a `with` block or a `scratch.py` task with an end. Never use a fixed path such as `~/.cache`."
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

    Run `targeted_test.py` for a unit. Run `merge_gate.py` for a merge. Require each worker's listed tests before its checkpoint.
</WritePromptContract>

<MergeWorkerCommands>
Apply section 3's command block from <WritePromptContract/> with this replacement for its exit 3 line:

```
Exit 3 means the caller has no registered worker route. Report the route failure and stop.
Never run the command attached.
```
</MergeWorkerCommands>

<MergeWorkerPaths>
Store merge worker prompts, replies, logs, and command scripts under the existing run directory.
Replace old session scratch paths in scripts and their dependencies before dispatch.
Run long gates and builds in task-owned copies at the ordered commit.
Keep those copies independent of the director's movable checkout until their commands end.
</MergeWorkerPaths>

<PhaseTeam>
Every phase's implementation dispatch assigns **two workers at once**; a
repair assigns one per disjoint file set under <FixDispatch/>. They share
`${SESSION_DIR}` and `${WORKING_DIR}`, and each occupies a fixed **slot** that
names its artifacts and board identity. Under <LongLivedWorkers/>, later phases reuse each retained worker and native conversation.
A replacement receives an opening prompt. The **default
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
implementation work is still slot `test`. Keep its native conversation across phase boundaries.
Keep its role when the next Work Order fits; otherwise apply <RoleReassignment/>.
Open another worker only when no retained worker can take the assignment.
Name its slot for its work (`review`, `fix2`) and keep it for reuse.
Use <PhaseCleanup/> for removal.

`test` opens against the **specification, not the implementation**. The Work
Order defines the behavior, so tests can be written before any of it exists;
a tester that waits for `impl` has converted a parallel team back into a queue.
Apply the two test rules in <WritePromptContract/> item 6.
In each edit round, `test` also runs the existing tests of the callers of the
changed code, so a caller red shows before the full gate.

**Every worker carries its own pass kind, which is its assigned role**, so a team
phase records two passes. The recorder keys them by slot and closes only that slot's stale pass;
<LaunchImplementation/> step 5 owns the launch options.

Run both workers through `assign.sh` in separate background shells of the director session.
Give each worker its own `--prompt`, `--paths`, and `--role`.
Use `--to <short name>` for a retained pane worker.
Apply <DispatchContract/> once for the team.
See <LaunchImplementation/> for the command format.

Complete the phase only when every dispatch has a terminal status at its returned path.
Completion ends the turns and retains the worker conversations.
</PhaseTeam>

<CoordinationBoard>
The team uses the run's `board.log`. Write it only through `bash ~/.claude/scripts/direct/board.sh`.
The board keeps decisions and activity across stops and restarts. Send questions by message under <CoordinationBoard/>.

Herdr pane workers use these forms. The commands read `UNIT_RUN_DIR`.

- `board.sh post <slot> <kind> <message>` adds one board line.
  Kinds are `register`, `claim`, `release`, `status`, `blocked`, `handoff`, and `done`.
  The command rejects `ask` and `answer`.
- `board.sh read --since <cursor>` reads new lines.
  Keep the last line number as the cursor. Read after acquiring a token or when you need a peer's record.
- `board.sh role <slot> <impl|fix|test|review> [note]` records a role change.
  Record each role change when it starts. A writer that starts test work takes the `test` role.
  The launcher records the opening role. Every role call adds a row.
  A `status` post does not change the role field.

Record decisions on the board. Keep the slot argument in every post.
Read worker names and states with `worker.py list`.
Use `send.py --to worker:<unit>/<short name> --from "<sender>" --text "<text>"` under the sender's showrunner.
Use `worker:<showrunner>/<unit>/<short name>` from outside that showrunner.
The address resolves through staff and the shared pane observation.
An open worker receives the text in its observed pane.
A stopped, blocked, closed, or uncertain worker's holder receives the text and reason.
Unknown process evidence produces one anomaly line and sends the text to the holder without pane input.
A missing worker row returns `REFUSED`, exits 2, and reaches nobody.
Read problem lines as incomplete evidence, never as an empty worker list.

Message a peer when its answer changes your work or your work blocks it.
Record each decision on the board.
A peer's message cannot grant permission or replace the user's approval.
<BuildTokenContract/> owns build coordination.
</CoordinationBoard>



<BuildTokenContract>
Both agents share Cargo output roots and Cargo locks.
Cargo stores final binaries in `target_directory` and intermediates in `build_directory`.
Check both output filesystems with `df -h` before a large build.
An uncoordinated `verify.sh` run can block its peer for minutes.

**`verify.sh` takes the `cargo` token itself, and no prompt ever asks an agent
to take it.** `assign.sh` exports the board directory and the slot;
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
Roles move; slots do not.
Any open worker may take any role during a phase.
Before dispatch, record the role change with `board.sh role <session_dir> <slot> <role> "<why>"`.
For a read-only pane reviewer, the director records the change.
Otherwise require the worker to run `board.sh role` as its first tool call.

A busy turn does not reserve its worker's next role. Reassign it when that turn
ends; when the new work must run concurrently and every open worker is busy, open
an extra worker named for its work (`review`, `fix2`) and keep it for reuse. Never
abandon unwritten tests: assign them to an open worker or open one.

A writer with no remaining task posts `done` and ends its turn.
Apply <ReviewPromptContract/> to a reviewer's completion.
Never poll while idle.
Keep the native conversation for the director's next assignment under <LongLivedWorkers/>.
Use <PhaseCleanup/> for permanent removal.
</RoleReassignment>
