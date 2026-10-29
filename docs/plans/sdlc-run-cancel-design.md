# Run cancellation: findings and design

## Answer

- Phase/reservation released, parked or failed: **No** today; need an exact phase-instance stop linked to each run.
- Tree no longer a live tip: **No** today; need provenance linked to all live consumers before this can prove obsolescence.
- Unit recorded stop or park: **No** today; need the ruling linked to exact phase instances or rounds.
- Owner session gone: **No** today; need an ended ownership record with no successor, not just session absence.
- Newer same-step run queued: **No** today; need an explicit replacement link to the older command.

One proposed command, `scripts/direct/run_stop.py`, has three forms:

```text
run_stop.py command --control-dir DIR --command-id ID
run_stop.py round --session-dir DIR --phase-instance UUID --round N
run_stop.py phase --session-dir DIR --phase-instance UUID
```

Each form records its stop, blocks further starts in that scope, and cancels its
verify, mesh and queue work. Exit 77 means cancelled; receipts show the stopped
scope and unfinished cleanup. The first version uses explicit stops only.

## Findings

Research date: 2026-10-29. Code citations name this worktree. Live evidence below
comes from existing natedev records, not from executing their producers. No test,
gate, build, cancellation, session start or signal was used. The full type design
contract at `~/.claude/docs/type_design.md` was read.

“Reliable today” means sufficient to select and cancel the right work without a
new human decision. It does not mean that every field in the source is wrong.

| Criterion | Source a tool reads; checked code | Reliable today | Gap and proposed check |
| --- | --- | --- | --- |
| The run's phase or reservation is released, parked or failed. | `progress_history_state.json` and its `history_file`: phase instances start at `scripts/direct/progress_history.py:1466`; explicit finish status is stored at `scripts/direct/progress_history.py:5639`. Reservation state and release marker names are at `scripts/direct/phase_reservation.py:63`; release proof is written at `scripts/direct/phase_reservation.py:475`. | No, as a combined cancel rule. An explicit `phase_finished` with `stopped` or `error` is usable once the run has an exact phase link. | `parked` also means an older **open** phase whose passes continue (`scripts/direct/progress_history.py:1064`). A released reservation can be replaced during the same phase (`scripts/direct/phase_reservation.py:37`). Release proves a checkpoint action, not that all validation is useless. Require an exact phase-instance stop record. Keep reservation release as evidence, not an independent trigger. Observation L1 shows the incident phase still active. |
| Its tree or clone is no longer the tip of any live branch or phase. | Mesh record `cwd` and `tree_key` (`scripts/agents/mesh_runs.py:95`, `scripts/agents/mesh_runs.py:318`); Git refs and the phase's source/copy records. Verify's cache key can cover a package and its dependencies (`scripts/direct/verify.sh:608`). | No. | A tree key is not a commit id. Dirty trees, detached copies, bisects and retained checkpoints may remain useful after a branch advances. No complete index says which trees all live phases still need. A future automatic rule needs copy provenance and a complete set of live consumers; first-version cancellation uses an explicit command stop. A path identifies the owner; it does not prove obsolescence. L2 shows a phase clone still at a branch tip after the reported park. |
| Its unit recorded a stop or park. | Production state rules (`docs/production_format.md:229`), showrunner cancellation rule (`commands/showrunner/produce.md:286`) and stop/move instructions (`commands/showrunner/produce.md:1117`), phase finish events (`scripts/direct/progress_history.py:5639`). | No, for an arbitrary unit ruling. | Production deliberately has no separate unit run-state field. A stopped run can lack a finish record. The Phase 9 text now requires the ruling to name runs, but it does not add a required structured run/phase/round join. The stop command must record the exact scope when the ruling is made. L1 has no matching phase-finish event despite the reported park. Do not parse prose into a cancellation order. |
| The session that owns it is gone. | Run `director`, active markers and mesh roster; `scripts/lib/unit_runs.py:108`, `scripts/lib/unit_runs.py:151`; process-aware session results at `scripts/message/sessions.py:39` and `scripts/message/sessions.py:270`; mesh supervisor checks at `scripts/agents/mesh_runs.py:209`. | No, as a cancel trigger. Local process absence can be established, but ownership can survive it. | A detached command is meant to outlive a worker turn. Delivery follows role transfer and can reach the director (`scripts/agents/mesh_runs.py:433`). A move can temporarily remove a session (`commands/showrunner/produce.md:1128`). Missing marker, socket or launcher is not abandonment. A future automatic rule needs explicit ended ownership with no successor; treat unreadable or remote evidence as unknown. L3 records ownership apart from the current marker. |
| A newer run for the same phase and step is queued. | Mesh records and live-twin matching (`scripts/agents/mesh_runs.py:277`); board wait files and matching key (`scripts/direct/verify.sh:2296`, `scripts/direct/verify.sh:2304`); memory wait rows (`scripts/lint/memory_admit.py:167`). | No, for phase-and-step replacement. A narrower board-wait replacement exists. | Existing matching uses worker/slot, cwd and command, not phase instance, round and required result. Mesh suppresses a duplicate on the same tree; it does not cancel an older live run. Orphan delivery suppression is different (`scripts/agents/mesh_runs.py:259`). Use an explicit command stop naming the older command; queue age alone does not authorize replacement. A newer timestamp alone is insufficient. L4 shows queue fields without phase or round. |

Each check returns a tagged `CriterionResult`: `Satisfied(evidence)`,
`NotSatisfied(evidence)` or `Unknown(reason, sources_checked)`. “Unknown” never
means “cancel”. An explicit stop is the primary signal. This design does not add
a watchdog that makes new park decisions from these five observations.

### Live observations

L1–L4 retain the round 1 sample; they are not a new liveness check.

- **L1 — phase and reservation.** Read
  `/tmp/claude/delegate/fc206247-76c0-4607-b268-025299592a62/progress_history_state.json`.
  Phase `54b`, instance `730d3a67-bffe-4b96-9741-8efcc42e07ac`, remained `active`
  in `open_phases`, with a `parked` container. Its reservation file under
  `phases/54b/` said `enrolled_awaiting_first_touch`; `reservation_released`
  was absent. Its history, named by `history_file`, had no `phase_finished`
  for that instance. This is evidence of a recording gap, not proof that the
  reported ruling did not occur. This design session also had phases 8 and 10
  active under `open_phases` while phase 9 was current. Cancelling all “parked”
  entries would cancel valid parallel work.
- **L2 — the incident command and clone.** The incident folder's
  `mesh_runs/20261029T101655-767382-9ebf.json` records a verify test call in
  `phases/54b/code`, start `2026-10-29T14:16:55Z`, finish
  `2026-10-29T17:04:38Z`, exit 143. Buildlog record
  `v20261029T141656-768591-16177` in
  `~/.local/state/buildlog/natedev/2026-10.jsonl` records the same call as
  `interrupted`, ending at `17:04:36Z`. These are 10:16 and 13:04 EDT. They do
  not prove the supplied 11:39 child-step start or exact 12:50 park time.
  Read-only Git queries found the clone's current HEAD at
  `f031d53fa215f328fec136a0e466c64104fd2fa2`, on `refs/heads/p54b-code`.
  That cannot establish where the original dirty tree is still needed.
- **L3 — owner and record shape.** The incident run's `director` file names
  `450ac99b-caed-49ba-8c6f-29016df421ab`. Mesh records store that run folder,
  worker thread, cwd, supervisor pid and tree key, but no phase instance or
  round. Current and completed records under both `/tmp/claude/unit/` and
  `/tmp/claude/delegate/` have this shape. No remote liveness probe was made;
  neither this observation nor marker absence establishes owner death.
- **L4 — queue shape.** Direct reads found 23 `.waiting` and 7 `.reservation`
  files in `~/.local/state/buildlog/admission/` at the sample time. For example,
  `1475041-249256452.waiting` names `phases/54c/work`, a session id, package,
  filters, pid and start time, but no phase instance, round or command-run id.
  `1391335-249020479-1793291902630127370.reservation` has a verify call id,
  but no phase or round. Their `/proc/<pid>/stat` files existed when read.
  These changing counts are observations, not a queue-wide consistency proof.

No saved run output or temporary research copy was created. Existing records
and logs belong to their runs and were left in place.

## Run kinds today

| Kind | Start and record | Cancellation now | Phase link today |
| --- | --- | --- | --- |
| Detached verify call | A mesh worker's `verify.sh` calls `mesh_runs.py start`, then re-enters verify with `UNIT_DETACHED_RUN=1` (`scripts/direct/verify.sh:977`). The supervisor writes `<session>/mesh_runs/<id>.json` and `.log` (`scripts/agents/mesh_runs.py:325`). | The current low-level route is TERM to the recorded supervisor process group, after checking that the pid still supervises that record. This is not a phase-aware API. The supervisor forwards signals to its immediate child (`scripts/agents/mesh_runs.py:494`); verify has its own child-group cleanup (`scripts/direct/verify.sh:2779`). Separate groups and remote copies need their own handling. Exit is commonly 143, not 77. | Session directory, worker/thread and cwd; phase may appear in cwd. Wait history can resolve the worker's pass, but the detached record has no direct phase/round link. |
| Attached verify call | Non-mesh calls stay attached. Verify makes `BUILDLOG_CALL_ID` (`scripts/direct/verify.sh:1057`), a live record in `~/.local/state/verify-steps/` (`scripts/direct/step_record.py:72`) and buildlog call/step records. The live record is removed at cleanup (`scripts/direct/verify.sh:2774`). | TERM to the verified call process invokes signal cleanup. Cleanup stops the copy watcher and releases board ownership (`scripts/direct/verify.sh:2749`). It has no public stop-by-phase command or uniform cancelled result. | Live step record has pid/start, repo, kind, package and routing state (`scripts/direct/step_record.py:39`). No exact phase or round. `--session-dir` and worker-thread wait attribution help, but are not a complete run index (`scripts/direct/verify.sh:1071`). |
| Other mesh command | `mesh_runs.py start --label ... -- <command>` starts a supervisor in a new session and stores command, cwd, worker/thread, tree key and pid (`scripts/agents/mesh_runs.py:328`). | Same process-group TERM route. There is no cancel verb. `reap` detects lost supervisors and can report failure without an exit status (`scripts/agents/mesh_runs.py:232`). A process group alone cannot cover a child that starts another session. | Run folder and worker only. Any phase in a label or path is an inference. |
| Memory waiter or admitted step | `memory_gate.sh` calls `memory_admit.py check` using its shell pid (`scripts/lint/memory_gate.sh:192`, `scripts/lint/memory_gate.sh:218`). The admission lock protects `.waiting` and `.reservation` records (`scripts/lint/memory_admit.py:977`). | No stop-by-owner verb. `release <reservation> <status>` removes a reservation; it does not stop work or remove a live waiter (`scripts/lint/memory_admit.py:1241`, `scripts/lint/memory_admit.py:1294`). Normal cleanup drops a charge when its shell pid disappears, even if a child survives (`scripts/lint/memory_admit.py:737`). Deleting a live wait file alone lets its next check recreate it. | Waiter has pid/start and some session/folder fields. Reservation can carry `call_id`. Neither carries a required phase, round or mesh id. |
| Board waiter and verify child/copy | Board wait files identify slot, command key, timestamp and pid. A local step starts a separate session and records its group and start identity (`scripts/direct/verify.sh:2515`). Verify can also route work to the Mac. | Existing board replacement returns 76 (`scripts/direct/verify.sh:2284`, `scripts/direct/verify.sh:2387`). Verify cleanup forwards signals and stops its copy watcher. This does not establish a durable stop across all remote children. | Board and verify call are indirect owners. Children need a common command-run link, including copies and retries. |

A build hold is separate. Only removal of its holder file ends it
(`scripts/build_hold/build_hold.py:732`). A hold can make verify wait or route
again (`scripts/direct/verify.sh:2172`). It must not be translated into a phase
stop, and a phase stop must not release a user's hold.

The existing low-level mesh command is `kill -TERM -- -<supervisor-pid>`.
This is documentation of the current route; it was not executed. It requires
the identity check above and does not by itself prove complete cleanup.

## Design

### Scope and ownership

All three forms require `--cause`, `--actor ADDRESS`, `--evidence REF` and
`--reason TEXT`. Causes are `tree-changed`, `replaced`, `failed-round`, `parked`,
`failed-phase` or `stop-rule`; validate them against the selected scope. `REF`
names the ruling or result. No caller infers a phase or round ruling from an exit code.
`N` is the immutable dispatch ordinal, not the repair interval
(`scripts/direct/findings.py:1154`, `scripts/direct/findings.py:1171`).

Use tagged records with required fields per variant, never null, omitted fields
or empty strings that encode state. `StopScope` is `Command(command_id)`,
`Round(phase_instance, dispatch_ordinal)` or `Phase(phase_instance)`.
`Ownership` is `Managed(unit_run_id, session_dir, phase_id, work_scope, origin)`
or `Standalone(control_dir)`. Managed `work_scope` is `PhaseWork(instance)` or
`RoundWork(instance, dispatch_ordinal)`. Capture origin as `WorkerPass`,
`Director` or `Checkpoint`, retaining their existing identity fields from
`scripts/direct/progress_history.py:4964` and
`scripts/direct/progress_history.py:4808`. Resolve ownership before detaching.

Each command gets one immutable `command_id`, shared by mesh, verify, its steps,
queues and remote children. Keep their existing ids as adapter handles. A managed
launch requires the full owner link and initialized control state; refuse either
missing part. It must not fall back to standalone. A call without any unit context
uses explicit `Standalone` mode internally; a proposed `--standalone` flag also
selects that mode. Existing non-unit verify calls remain usable
(`scripts/direct/verify.sh:968`). Standalone commands get command-only cancellation.
They have no invented phase link. Unit context and `--standalone` together are invalid.

A phase stop covers all its rounds and commands; a round stop covers only its
ordinal. A command stop leaves its phase and other commands open. A resumed phase
gets a new instance; a new repair gets a new ordinal. Stop does not end a session.

### One commit record and recovery

The single authoritative stop record is `CONTROL/stop_state.json`.
For managed work, `CONTROL` is `<session>/run_control`; standalone uses
`~/.local/state/run-control/standalone/<command-id>`. The record contains version,
ordered stop decisions, their exact scopes and evidence, each command's first
cancel id, cleanup targets and receipt state. Command registrations and handles
live in `CONTROL/commands/<command-id>.json`. Derived receipts live in
`CONTROL/receipts/<stop-id>.json`; they are not a second authority.

Registration, child-start permission, terminal-result decisions and stops share
one control lock. Under it, select nonterminal commands and publish the full new
stop record with **one atomic replace**: write a same-directory temporary file,
fsync it, rename it over `stop_state.json`, then fsync the directory. Signal only
after this succeeds. The rename commits the stop. Command state, history,
findings and receipt files are recoverable projections of that decision.

| Interrupted write | Reader and recovery action |
| --- | --- |
| Temporary file only | Read the previous authoritative record; ignore the temporary file. No stop was committed. |
| Rename done, directory flush uncertain | Treat a visible stop as effective; retry durability before signals. Report persistence uncertainty, never undo it. After restart, use the surviving complete record. |
| Stop committed, command/history/findings files old | Derive cancellation and launch refusal from the stop record; replay each projection by stop id. Never trust a cached `Running` or `Open` over it. |
| Cleanup happened, acknowledgement not committed | Keep `Pending`; recheck exact handles and retry idempotently. Do not infer completion from a missing wrapper. |
| Completion committed, receipt/outbox file old | Rebuild it from the authoritative record; delivery deduplicates by command id. |
| Initialized control state missing, damaged or unreadable | Return `ControlUnavailable(reason)`; refuse managed launch/admission and report cleanup pending. Do not recreate an empty open state. |

Registration atomically saves handles before granting execution. Stop-first denies
that grant with 77; start-first leaves a registered cleanup target. Check the
record again before admission, retries, routing changes and cached-result delivery.
Remote launch must register its handle before execution; a lost remote connection
leaves a pending target. Hold no control lock during host calls. Admission releases
its queue lock before requesting launch permission; a rejected grant is returned.

`scripts/lint/tmp_sweep.py:46` protects current owners, not pending stops. Extend
its run-folder guards and deletion recheck to retain any run with pending cleanup,
unapplied projections or unreadable control state, plus referenced handles and
provenance. Recheck under the control lock before removal, including hand cleanup;
retention must not depend on a live director or age. Archive completed records
before deleting a finished run. Standalone control records are outside scratch.

### Worked round stop

The director records failed dispatch 3 of phase instance `P`, then calls:

```text
run_stop.py round --session-dir /tmp/claude/unit/U --phase-instance P --round 3 --cause failed-round --actor director:U --evidence gate:C1 --reason 'Round 3 cannot meet its gate'
```

1. The control lock resolves `Round(P,3)` to command `C1` (mesh plus verify) and
   `C2` (memory waiter). Atomic publication records stop `S1`, marks that round
   stopped and assigns each command its first cancel id `S1`. Other rounds stay open.
2. The findings projection marks dispatch 3 `Stopped(S1)`. Keep fix-attempt and
   budget counts, accepted findings, prior failures and all landed-edit facts.
   Reopen unresolved findings covered by dispatch 3, including `repair_in_flight`
   and pending-review entries. Keep landed evidence on those reopened entries.
   Never use the refund behavior of `abandon` (`scripts/direct/findings.py:1235`).
   Append `round_stopped` once. A phase stop applies this to its affected rounds
   and records `phase_finished: stopped` for the exact instance. This is a new
   projection, not a call to today's finish-phase path that closes waits at once.
3. Today dispatch blocks on in-flight or pending findings
   (`scripts/direct/findings.py:1123`), while `landed` selects the latest round
   (`scripts/direct/findings.py:1209`). Change every `landed` and `abandon` call,
   including `scripts/direct/implement.sh:636`, to require phase instance and
   dispatch ordinal. Under the control lock, check those against the ledger and
   committed stop before updating it. A late result for round 3 records a stale
   result diagnostic; it cannot update round 4. Replayed projections change only
   entries still owned by round 3. New dispatch waits for that round's cleanup
   and ledger projection to complete, then uses a new ordinal.
4. Remove C2's queue place and stop C1's owned execution as specified below.
   The final S1 receipt lists both command ids, `Round(P,3)`, release evidence and
   `Complete`. Each command has one result, `Cancelled(S1,77)`. If a target cannot
   be reached, S1 stays `Pending(targets)` and the stop command returns 1.

### Adapter contract and cleanup order

| Contract | Required content |
| --- | --- |
| Request | Stop id; command's first cancel id; tagged stop scope and ownership; command id; recorded adapter handles; attempt deadline. Process handles include host, boot id, pid/start identity and owned group or containment. |
| Response | Command id and cancel id; `Pending(targets)` or `Complete(evidence)`. Pending reasons are tagged `HostUnreachable`, `ProcessAlive`, `IdentityUnknown`, `LegacyUnlinked` or `WriteFailed`, each with its affected handle and reason. |
| Records | Control files above; existing mesh JSON, verify live/buildlog records, board wait/ownership records and memory `.waiting`/`.reservation` files gain the common command id and tagged ownership. |
| Completion | All owned execution confirmed empty, queued places removed, admitted charges and execution slots released, exact waits closed and projections saved. Preserve raw termination status and resource-release times as evidence. |

Use one overall deadline per attempt, proposed as ten seconds including locks,
local cleanup and host calls. No per-host reset. After committing the stop:

1. Block admissions and retries. Remove matching queued entries under their
   queue locks; an already granted entry becomes a cleanup target. Never let a
   live waiter recreate a stopped entry.
2. Send **all local TERM requests before any remote call**. Use registered
   handles, deduplicate mesh-wrapped verify, and signal outside resource locks.
   Cover child groups, hold waiters, watchers and registered remote work.
3. Request remote cleanup within the remaining deadline. Allow a proposed five
   seconds from each TERM before force. Do not let a remote request consume time
   reserved for local force/recheck. Recheck identity before force. If the overall
   deadline is too close, carry that target and its grace start into the next attempt.
4. Confirm owned execution is empty before releasing its admitted memory charge
   or execution slot. A dead wrapper is insufficient. Unknown or unreachable
   execution retains its charge. Never release another command's resource or a
   user's build hold.
5. Commit cleanup evidence, close waits for the exact commands, and publish the
   receipt and cancellation result. Unknown legacy links or escaped children
   remain pending; they cannot produce a false complete receipt.

Repeating the command resumes remaining targets. Supervisors and the next start
or reap may reconcile the same record. There is no new daemon: without another
executor, the director must retry. No attempt waits beyond its deadline.

### Exit codes and overlapping stops

Stop CLI: **0** means complete, **1** means committed but pending, **2** means
invalid scope or no confirmed durable commit. Persistence uncertainty includes
the record path and must be retried before claiming cancellation succeeded.

Run status **77 always means cancelled**, never failed. The receipt names
`Command`, `Round` or `Phase`; 77 alone does not mean the phase stopped. Tree-change
cancellation planned at `docs/plans/natedev-verify.md:101` and explicit replacement
both call the command form. Neither reruns work or closes its phase. A raw 77
without a receipt remains cancelled with `PendingReceipt(command_id, reason)`;
reconcile a command
stop and cleanup evidence, never fabricate a round/phase ruling. Forced execution
may exit by signal; its recorded result is 77 with the raw status retained.
Keep existing 76 supersession compatible. Cancelled work writes no pass/fail cache
or gate verdict and triggers no repair.

Repeated scope stops reuse their first stop id. **A command never changes its
first cancel id.** If round stop S1 is pending when phase stop S2 arrives, S2
closes the phase and includes S1's pending targets and completed receipts as
dependencies. S2 drives remaining S1 cleanup; it does not issue a second result
for those commands. Only newly selected commands get S2. Completion-first keeps
its terminal result; stop-first suppresses later success/failure. The durable
result outbox is keyed by command id, with the immutable first cancel id inside;
receivers deduplicate that key when a send is retried.

Managed commands use recorded handles without broad process discovery. At rollout,
legacy candidates require a unique recorded phase-instance/round join. Cwd and
labels alone are insufficient; unresolved candidates stay pending and prevent a
claim that all tied runs were cancelled.

## Callers

- **Director/showrunner:** stop each exact phase at a park, failure or stop-rule
  ruling. Report pending cleanup. Starting another open phase is not a park.
- **Repair controller:** stop the failed dispatch before its replacement, preserve
  counts, and pass exact ownership to every later ledger update.
- **Worker/verify watcher:** stop one recorded command on explicit replacement or
  the agreed input-tree change rule. Report phase/round decisions to the director.

Finish, abandon and session-end paths must consult the stop record. A session
move preserves ownership. No caller infers stop authority from session absence.

## builds' part

**Owner: builds production, verify-unit. Every change below needs builds'
agreement. This is a self-contained hand-off, not permission for sdlc to edit it.**

The proposed `scripts/direct/run_stop.py` stops a command, exact phase instance
or dispatch ordinal. It atomically replaces one authoritative
`CONTROL/stop_state.json` before cleanup; `CONTROL/receipts/<stop-id>.json` is a
projection. Managed CONTROL is `<session>/run_control`; standalone CONTROL is
`~/.local/state/run-control/standalone/<command-id>`. Builds must agree these parts:

1. **Ownership and adapter.** All verify wrappers, steps, retries, remote children
   and queues share one command id. Ownership is tagged `Managed` with unit run,
   directory, phase instance, phase/round scope and launch origin, or `Standalone`.
   Managed calls missing that link are refused; non-unit verify stays usable and
   gets command-only stops. Register exact host/boot/start/group handles before
   execution. Adapter requests carry stop scope, first cancel id, command id,
   ownership, handles and one attempt deadline; responses are `Pending(targets)`
   or `Complete(evidence)`. Store them in existing verify, board and memory records.
2. **Admission and memory.** Add idempotent cancel-by-command to memory admission,
   board and hold waits. Remove queued places promptly and block recreation.
   Change **normal** cleanup at `scripts/lint/memory_admit.py:737`, not just the
   cancel path: bind each admitted charge to its owned execution group or
   containment, retain it after wrapper death, and release only on confirmed
   emptiness. Unknown or unreachable groups retain charges. Admission returns
   unused grants when the control barrier rejects a start.
3. **TERM and release order.** `scripts/direct/verify.sh:2785` currently calls
   `release_token` immediately on TERM. `scripts/direct/board.sh:285` sends SIGKILL;
   release invokes it under its guard (`scripts/direct/board.sh:453`). Replace
   these routes with: commit stop, deny new execution, request TERM outside locks,
   allow grace, force only verified targets, confirm emptiness, then reacquire the
   resource guard and release the exact owner's slot/charge. Normal release and
   stale-owner reclaim must follow the same rule when execution remains. No
   signals or waits under the resource guard. One ten-second attempt deadline
   includes every host; send all local TERM requests before remote calls, reserve
   time for local rechecks, and persist unfinished targets. Proposed grace is five
   seconds; an unfinished grace continues on retry.
4. **Cancellation result.** Agree command-only cancellation for explicit replacement
   and `docs/plans/natedev-verify.md:101` tree changes. Both use the same stop and
   receipt protocol; 77 always means cancelled. Receipts identify command, round
   or phase scope and pending cleanup. Preserve raw signal status; do not cache
   cancellation as a verdict or trigger repair. Keep 76 compatible. A command keeps
   its first cancel id even when a later phase stop takes over pending round cleanup;
   publish one command result. Mesh and final-pass consumers must honor this too.
5. **Copy path: please confirm.** The external copy plan is unavailable here.
   Request this shape, or an equivalent with the same identity components:

   ```text
   /tmp/claude/unit/<unit-run-id>/phases/<phase-id>/instances/<phase-instance-id>/
     build-copies/<command-id>/<copy-id>/tree/
     build-copies/<command-id>/<copy-id>/provenance.json
   ```

   Existing delegate runs may retain `/tmp/claude/delegate/`. Publish provenance
   before execution: command id, tagged phase/round ownership, copy id, host and
   source repo plus `CommitTree(commit,tree)` or `WorkingTreeDigest(algorithm,digest)`.
   Never reuse copy ids. This proves ownership, not obsolescence. Retain provenance
   through cleanup; remove bulky copies only after execution is confirmed empty.
6. **Future acceptance cases.** Cover registration/admission/stop races; wrapper
   death with a live child during normal reservation cleanup; local TERM before
   an unreachable host; deadline expiry and retry; pid reuse; command-only tree
   change; standalone calls; late round-N landed/abandon after N+1; partial writes
   and scratch sweep; completed-result races; two callers; **round stop pending,
   then phase stop**; and sibling phases. These are implementation checks, not
   checks run for this document.

sdlc owns the command, authoritative record, findings/history projections, callers
and the pending-record protection in `scripts/lint/tmp_sweep.py`. Mesh must adopt
shared ownership and cancellation receipts. Unsupported adapters remain pending.

## Later

- **Ownership leases:** needed only for a rule that automatically stops work after
  its owner ends with no successor. Session absence is insufficient today.
- **Copy-consumer tracking:** needed only to prove no live phase still needs a tree.
  A phase-bearing path is enough for explicit ownership, not for that rule.
- **Replacement generations:** needed only for automatic same-step queue replacement.
  First-version replacement names the exact old command and records the decision.

## Open questions

Builds must confirm the copy path, adapter contract, remote containment and proposed
five-second grace/ten-second attempt. No cross-host completeness was established.
L1–L4 do not verify the exact child start or park time, retained memory, or affected
peers. Legacy records lack complete joins. Automatic stops from owner absence,
reservation release or tree obsolescence need a separate process ruling.

## Ruled out

- Cancel every `parked` history entry. It would stop parallel open phases.
- Cancel on reservation release or branch movement alone. Checkpoints and
  retained validation can still need that work.
- Cancel on missing marker, socket, launcher or supervisor alone. These have
  different meanings, including moves and failed delivery.
- Match only a pid, cwd, command string, package or newest timestamp. These do
  not identify a unique owner or replacement.
- Delete a queue row or memory reservation while its process can continue.
  The row can return, and the memory charge can become false.
- Signal the whole shared build slice or all work in a repository. Sibling
  phases and other units must continue.
- Convert every signal exit to 77, or call 77 a failed gate. Record explicit
  cancellation with its receipt; an unrelated signal remains a separate outcome.
- Wait for useless work to finish a report. Stop it when the decision is made;
  report cancellation and any incomplete cleanup instead.
