# Long-lived workers and held Astra roles

## Phase 30, rounds 1 and 2a

Live consumers read staff membership and backend evidence.
`StaffSnapshot.backends` derives backend evidence from each current native session.
`session_staff.worker_backend` returns recorded evidence or a reason for unavailable evidence.
Worker displays and cleanup read `worker_panes.observe_parents`.
They show stopped, closed, unknown and incomplete states without reading a mesh roster.
`inventory.worker_resumes` keeps Claude child commands in `ClaudeChildResume`.
`refusal_watch.py` owns `QUOTA_REFUSAL`.

Round 2a removes mesh selection from the worker command, stop, restart and pane restore.
The pane owner holds the sandbox constants.
Round 2b removes the held assignment launcher dependencies.
It deletes `codex_mesh.py`, the worker ledger and the remaining allocation and adoption helpers.
It removes `unit_runs.worker_transport`, `parent_transport`, run pins and the transport config keys.
It renames `mesh_runs.py`, `mesh_run_process.py`, their config and the detached record folder.
The current detached commands keep their existing names until that round.
Completed run folders keep their roster files.
`progress_history.py` and `usage/agents_log.py` keep their historical reads.

## What it is

Unit workers retain their native conversations across assignments.
Staff records identify each worker independently of its current session, run or account.
A later assignment resumes the same conversation when the worker remains available.

## Unit code reviewers

Unit reviewers use read-only access.
Writers retain their writing roles.
Concurrent review assignments use separate worker identities.
Titles use the current staff placement and assignment role.
`workers.shown_name` numbers unit workers in their parent's staff order.

## How it works

### Staff identity and admission

A worker has one fixed staff id, name, parent and worktree.
`current_session` selects its Claude session or Codex thread.
A family change preserves the fixed staff id.
The [staff design](staff-accessors.md#staff-identity-and-backends) defines the row fields and backend evidence.

`worker_admission.admit(name, run, *, reference="")` returns `WorkerStartAllowed` before a backend starts.
Its slot is `NewWorkerSlot` or `ExistingWorkerSlot(row)`.
`PublishedWorker(row, handoff)` means that staff publication succeeds.
Only successful publication permits successful dispatch.
`assign.py` completes admission before it records the implementation pass.
A publication failure runs the caller's cleanup callback and reports the native id.
The Claude coordinator tries to stop an unpublished session.
A failed stop reports `cleanup unproved`.
Native pane startup retains an unpublished pane for recovery.

A new slot takes the parent/name lock before the row lock.
An existing slot takes the row lock.
`worker_operation` locks the admitted row.
For a new slot, it first checks membership under the parent/name lock.
Admission and publication use the same fixed staff reference.
The held `workers.resolve` loop continues candidate selection after `AdmissionRefused`.

### Family handoff and retry

`switch_worker` extracts source context, releases the source, creates the destination and publishes its native identity.
Both families use this sequence.
`FamilyHandoff` has `NoFamilyHandoff`, `TranscriptHandoff(extract, source)` and `TranscriptUnavailable(reason)` variants.
The extract contains assignments, final replies and changed paths.
It excludes tool request bodies and tool output.
A missing transcript prints an anomaly and permits dispatch without a handoff.
`TornTranscriptRecord` permits extraction from the valid lines of a damaged transcript.

The source result is `SourceReleased`, `SourceUnresolved(reason)` or `SourceReleaseUnknown(reason)`.
`stop_resume.release_source(row, backend)` preserves each stop reason and supplies the next action.
Native evidence must prove that the source stops or is absent.
Unknown evidence preserves the source and prevents the family change.
`StopUnproved` proves neither absence nor continued execution.
The operation checks the fixed staff id and current session before it stops a source.

An existing worker's default switch request is `sha256(f"{staff id}:{destination family}")`.
`assignment_id(prompt)` hashes the resolved prompt path, modification time and contents.
Retries keep the original prompt file as the assignment identity.
`DispatchRecord(assignment_id, state)` uses these states:

| State | Stored value | Meaning |
| --- | --- | --- |
| `NO_RECORD` | empty text | No known dispatch state. |
| `CREATION_INTENT_RECORDED` | `prepared` | The request precedes backend creation. |
| `DISPATCH_IN_FLIGHT` | `sending` | Delivery starts, but acceptance is uncertain. |
| `ASSIGNMENT_ACCEPTED` | `accepted` | The destination accepts the assignment. |

`DispatchOwner.TURN_SUBMISSION` delegates acceptance recording to turn submission.
`record_dispatch` checks its expected request under the exclusive record lock.
It preserves a request that another operation replaces before lock acquisition.
`dispatch_record(run, name, *, report_anomaly=True)` reports an unknown stored state and uses `NO_RECORD`.
The diagnostic is `anomaly: <name>: unknown dispatch_state <value>; using NO_RECORD`.
This anomaly does not refuse admission.
`report_anomaly=False` suppresses the diagnostic on repeated reads.

`dispatch_pending(run, name, prompt)` matches the assignment id and a state other than `ASSIGNMENT_ACCEPTED`.
An unreadable record prints one anomaly and returns false.
A family change still reaches the switch operation.
A retry reuses the published destination and places the saved extract before the assignment.
An accepted retry prints `unchanged`.
The held launcher retains its original prompt identity and its dispatch record until round 2b.
`review.sh` has no pending dispatch check and does not restore a saved extract.

### Claude coordinator and friends

`scripts/direct/claude_worker.py` runs the Claude launch steps in the assign process.
`assign_claude._run` calls its functions directly.
The module has no command line or shell protocol.
It reads `CLAUDE_WORKER_REGISTER_WAIT_SECS` before it stops a source.
It verifies the destination against the running Claude registry before staff publication.
It records the role before delivery.
Delivery starts one Python child through `scripts/lib/py`.
The child calls `send.py --to session:<id> --from launcher --file <prompt>`.
`CLAUDE_WORKER_SEND_SCRIPT` selects the delivery script.
`assign_claude.py` records the worker after publication.
The heartbeat watch follows the assign process during the wait.

A retry that finds its original user prompt in the transcript records acceptance without another send.
A complete transcript without that prompt permits delivery.
A damaged transcript without that prompt refuses uncertain delivery.
The refusal says `Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused`.
A missing transcript keeps the published session and places the saved handoff before the assignment.

### Account selection

The [account design](accounts.md) defines the selected workers account and its authority on natedev.
Both machines resolve this selection through `role_map.py`.
The selected account is separate from a worker's thread and role.
Account moves preserve the thread while the native worker operation changes its home.
Claude assignment fallback does not replace the selected Codex account.

### Launchers

The launcher records the fixed worker id before it reports successful dispatch.
Role and lens records describe the current assignment.
A later assignment retains the worker identity and selects its current native session.
A prompt names its expected HEAD and asks the worker to read the current tree.
Review prompts retain the Three Gods sentence and the Type Design Contract.

`assign.py.end_failed_launch` ends a failed launch's run only when these conditions hold:

1. No recorded pass has status `completed`.
2. Every recorded pass belongs to the failed recorder slot.
3. No other worker appears in the roster, launch records or native staff children.
4. The director holds only that run.

The native worker command must return exit 2.
Exits 3 through 8 retain the run.
A Claude launch must add its slot's `pass_started` event before a failed return qualifies.
The held Codex path requires an error from its worker process.
A signal that interrupts the launcher does not reach this cleanup call.
Each recorder slot counts separately, including a pass without a worker name.
Cleanup records `finish-run --status error`, then calls `end_session.sh`.
Both commands use `call(check=False)`.
Cleanup errors preserve the launch exit code.
The phase reservation stays until a checkpoint releases it.
`prepare_session.sh` and `end_session.sh` select `CODEX_THREAD_ID` before `CLAUDE_CODE_SESSION_ID`, as `staff.runtime_id()` does.
A retry without `--session-dir` uses a current marker or run environment when available.
Otherwise `prepare_session.sh` creates a new run.
An explicit run folder retains its existing retry behavior.

### Choosing a worker for a review

Each review lens uses the unit's read-only reviewer through `assign.sh --role review`.
Concurrent reviews use distinct workers.
Only the adversary lens starts early.
The director retains its dispatch handle for Stage 2 and cancellation.
Closure review uses the review role without a lens.
`progress_history.py` gives each extra worker its own `Agent N` number after `impl` and `test`.
The first recorded pass or board handoff establishes the order.
The original `review` worker appears only when a displayed stage assigns it.

### Resume messages

`resume_message` builds one prompt for resumed conversations.
`ResumeWorkers(results, cwd, panes)` combines child resume results with pane worker states.
`inventory.worker_resumes` reads direct Claude worker children and preserves their saved launcher arguments.
Pane observations supply native worker resume state.
`AccountMove` names the source and destination accounts.
A busy worker receives only its own pass continuation.
An idle worker receives no prompt.
A director can receive the list of its busy and idle worker children.
Restore shares dispatch reads across pane workers.
It reopens only workers with active dispatches.

### Cleanup

Run end acts on every run that the ending director holds.
It leaves another director's run unchanged.
The marker's run precedes the other runs under the run roots.
`run_folders` excludes each root's `active` directory.
A failed scan still retains the marker's run.

`end_session.sh` first removes held design bindings without ending their workers.
A failed unbind prevents the later steps.
It then removes phase workers and tracked run scratch.
Scratch removal keeps a checkout that contains a live detached command's working directory.
It does not follow directory links or remove run logs, histories or the run directory.
The later steps continue after a failure.
Run end does not start a sweep.
It removes only the ending session's active marker.
Another marker's live or unknown owner prevents removal of its former run folder.

A rename or transfer preserves fixed worker references.
`rename.py` repairs the unit's session and display names.
An explicit worktree move also changes the worktree path and branch.
See [unit rename](unit-rename.md) for worktree moves.

### Measurement: `scripts/whoami/worker_usage.py`

`worker_usage.py` reads the usage store through `usage_report.open_read_store` without changing it.
It reads no rollout, SSH, git or account readings file.
The collector stores requests, compactions, edits, checkpoints, account readings and role changes from both machines.
The [usage design](usage-ledger.md) defines the worker report, dated model weights and source states.
The report compares before and after windows, or daily windows around `DEFAULT_T_LIVE`.
It reports usage per checkpointed phase and names incomplete source evidence.
Missing readings, credits or checkpoints produce explicit unavailable results.

## Lasting roles

A design has one designer slot and one reviewer slot.
Open roles retain fixed staff ids across rounds, bindings, restarts and account moves.
The worker row's parent supplies its holder.
`worker_parent` resolves that parent from the supplied staff snapshot.
A rename never selects a replacement through the worker's former name.

### Store and types

`design_roles.py` stores each design under `DESIGN_ROLES_STATE`.
The default is `~/.local/state/astra/design-roles/<design id>/`.
`design.json` stores placement, repository, document, area, binding, role slots and assignment claims.
`prompts/`, `replies/` and `pending/<role>/` retain work and queued messages.
`transition.json` retains an incomplete lifecycle operation.
The binding is `DesignUnbound` or `DesignBoundToPhase(run_directory, phase, repository, output_paths)`.
The lifecycle is `RoleNotOpened`, `RoleOpen`, `RolePausedForMove`, `RoleNeedsRecovery` or `RoleEnded`.
Open, paused and recovery states retain the fixed staff id.
Ending an opened role retains its staff id.
Ending an uncertain creation can record an empty id when no worker exists.
`HeldWorker.thread_id` derives the current thread from staff.
`ReadOnlyReviewer` and `PhaseBoundDesigner` define the access policy.
`_read_designs` reports each skipped damaged record once per operation.
It refuses a matching active design that needs recovery.
`list` returns damaged record problems alongside readable roles.

### Operations

`open` reserves placement, checks the binding and records creation intent before launch.
An uncertain start retains its claim.
`resume` requires an open or paused role, a valid binding and no active assignment claim.
It saves the headed prompt, checks the current lifecycle and generation, then records the claim before dispatch.
`recover` proves the recorded worker idle and clears its uncertain claim without creating a replacement.
`rebind` requires a reason and the expected generation.
It records a transition and advances the authority generation before it publishes a new binding, checkout or document.
It then refreshes the census and clears the transition.
`end` confirms release before it records `RoleEnded`.
A failed end retains its transition record for retry.
`list` shows an active role with an incomplete transition as `RoleNeedsRecovery`.
`unbind-run` removes the binding without ending the role.
`list` reports roles and problems without a network request for a local read.
An ended identity cannot reopen.
`[roles] new_open=false` in `config/design_roles.conf` preserves existing roles and refuses new ones.

The configured authority host owns placement.
Both roles remain on the design's recorded host.
Host transfer is unsupported.
Repository registration requires each host's absolute checkout mapping.
Repeated registration must keep the same mappings.
An authority error stops the local lifecycle operation.
Renames and transfers retain worker staff ids without rewriting holder records.

### Dispatch and messages

`assign.pane_policy` selects designer and reviewer settings through `worker_panes.launch_policy` and the shared registry.
A designer requires a phase binding and authorized output paths.
A reviewer uses read-only access.
`resume` saves one assignment claim and includes pending message ids in the prompt.
Before recording the claim, `resume` rechecks the binding generation and fixed staff lifecycle.
A pending binding transition prevents dispatch.
The claim permits one `dispatch_turn` call through `assign.sh --target durable-design-role`.
Success removes only the included pending messages.
Failure retains pending input and records recovery state.
A role's `send` queues text for its next authorized assignment without waking the role.
Detached command results use `mesh_runs.deliver` and `send.deliver_worker_text`.
The result carries `Detached run: <run_id>` as its stable marker.
Under a per-run delivery lock, `mesh_runs.deliver` checks the worker transcript for that marker.
It skips delivery when a user entry already contains the marker.
The sender submits to a proved worker pane or sends the result and cause to the current holder.
`check_patch` checks patch paths against the binding generation and phase reservation.
It rejects patches outside the bound paths or repository.
The check does not cover arbitrary shell or Python writes.

### Astra work header

`scripts/astra/astra_work.py` records assignment identity before dispatch in `<!-- astra_work {JSON} -->`.
`AstraWork` schema 1 identifies the role, member, document, assignment time, reply path and work.
`PhaseWork` records the run, phase, phase instance and history file.
`StandaloneWork` identifies work without a phase.
`HeaderMissing` and `HeaderInvalid` preserve failed read reasons.
`phase_work` requires exactly one matching active or open phase in the run state.
`read_header` checks exact keys, schema, role and the UTC assignment time.
The brief command creates the prompt exclusively and refuses an existing reply.
An identical brief retry keeps the original file and assignment time.
`astra_review.py` requires both phase flags or neither.
An unresolved phase returns exit 2.

### Census for cargo-handler

`astra_roles.py` stores `staff_id`, `held_at`, `session_dir`, `worker` and `pid` under `ASTRA_ROLES_STATE`.
Durable holds also record `design_id` and `role_id`.
The record copies no role, thread, account home or manager.
Staff and assignment logs supply those values.
`hold`, `release` and `live_roles` manage the census.
A missing runtime does not erase a durable design identity.
Cargo-handler reads staff names and parents from schema 2.
The [session roster](session-roster.md) shows open roles through their current staff identity.

## Worker operation fencing

`scripts/agents/worker_lifecycle.py` owns worker lifecycle locks.
`lock_worker` locks one identity.
`lock_workers` sorts identities before a transfer.
Recursive acquisition by the same thread fails and names both call sites.
Competing threads and processes serialize through the file lock.
Staff slot and row locks protect native identity publication.

Native stop paths use the caller's absolute monotonic deadline through row locks and worker stop calls.
`operation_deadline` carries that limit through callbacks and nested helpers.
An omitted deadline inherits the active limit.
An explicit limit can only reduce it.
Nested scopes restore their parent on every exit.
New threads and processes receive the absolute deadline explicitly.
`acquire_flock` handles shared, exclusive and immediate lock requests.
Lock expiry propagates as `OperationDeadlineExpired` to the incomplete shutdown report.
A released lock does not prove that a turn stops.
Native idle or stopped evidence must prove the source state.

## Staff membership and dispatch

A worker has a fixed staff id, a parent and a current native session.
A family change preserves the staff id and changes the current session.
Staff storage uses schema 2.
Backend readers derive the family and account from current session evidence.
They store no separate backend record.

Native dispatch identity comes from the recorder's pass and history records.
The held Claude launcher stores the fixed staff reference and slot in `impl_worker_<slot>.json`.
`worker_state.worker_record_path` resolves the staff member before it selects the slot file.
`worker_admission.parent` reads the run owner from the state file.
It does not recover ownership from a mesh roster.
The held launcher still uses allocation and publication helpers in this round.
Round 2b removes those helpers after the other units release their files.

## Displays and messages

The roster, board and account display read staff members and pane evidence.
`worker_panes.observe_parents` shares one pane read across the selected parents.
Each display preserves the reason for incomplete evidence.
A missing pane record does not prove that a worker has ended.

Staff worker addresses use `worker:<parent>/<worker>`.
Message selection resolves the fixed staff id and current session.
Pane delivery uses the observed pane and current holder.
Daemon sessions retain their `codex:<thread>` route.
The message command has no mesh recipient branch.

## Retirement and resume

Unit retirement stops workers through the pane worker path.
Run cleanup preserves worker threads.
`remove_workers.py` resolves the selected staff member before it calls the worker command.
Shutdown keeps saved Claude child launcher commands in `ClaudeChildResume`.
Pane worker resume messages keep the fixed staff id and dispatch id.
Account move messages preserve the move source, destination and task context.
Native source release requires a confirmed stop or absence.
Unknown source evidence preserves the source and prevents the family switch.

The sweep uses staff ownership and pane states to retain live run folders.
Stopped or closed workers can supply end evidence.
Unknown or incomplete evidence retains the folder and names the cause.
Detached command records retain their own process and working directory checks.

## Held interfaces

`assign.py` still imports mesh launch helpers in this round.
`production/worker.py`, shutdown and pane restore use staff membership and native evidence.
`workers.py` keeps the ledger methods that the held assignment launcher calls.
`worker_adoption.py` keeps the adoption helpers that `codex_mesh.py` calls.
`session_staff` keeps roster publication for those launchers.
These interfaces do not provide live display state.

`worker_lifecycle.py` owns watchers and watcher events.
`finished_run_status` has the same owner.
`herdr.py` owns `loaded_threads`.
`codex_mesh_test_support.py` owns the shared launcher fixtures with their existing names.

## Pane closure and restore

The director closes a worker pane after it accepts a pass.
An immediate repair or phase dispatch can retain the pane.
A busy worker or live detached run prevents closure.
Closure keeps the staff row and thread.
The next assign resumes that thread in a new pane.
Restore reopens only workers with active dispatches.
Idle workers stay closed until their next assign.
Account moves preserve closed idle workers.

## Worker account display

`worker.py list --json` gives each worker its name, fixed id, current session, state, role and activity.
It computes `work`, `account` and `home` when it reads each worker.
It stores none of these three fields.
`worker_panes.worker_state` supplies `work` as `idle`, `busy` or `unknown`.
`member_account.resume_account` supplies the account label and absolute home path.
Unavailable backend evidence gives `account` the `AccountUnavailable` state and its cause.
That row has no `home` field.

## Assignment settings and command files

`UNIT_COMPACT_ABOVE_TOKENS` controls idle mesh compaction until round 2b removes that path.
Pane workers have no option for compaction during a turn.
Codex keeps its own full-window limit.
`commands/unit/direct.md` defines `<LongLivedWorkers/>`.
`docs/direct/repair_rounds.md` references it from `<FixDispatch/>`.
`docs/direct/launch_implementation.md` references it from `<LaunchImplementation/>`.
`docs/direct/early_review.md` defines `<EarlyReviewArm/>` and the early review worker.

### Key files

| File | Purpose |
| --- | --- |
| `scripts/production/worker_admission.py` | Admission, publication and family changes. |
| `scripts/direct/claude_worker.py` | Claude launch and retry. |
| `scripts/herdr/worker_panes.py` | Native worker observation and pane operations. |
| `scripts/astra/design_roles.py` | Held design identities and assignments. |
| `scripts/agents/worker_lifecycle.py` | Lifecycle locks, watchers and event records. |
| `scripts/whoami/worker_usage.py` | Usage store report. |

## Invariants

Staff publication precedes successful assignment delivery.
A family change preserves the fixed staff id, parent, worktree and queued messages.
An uncertain stop does not authorize a replacement source.
A retry preserves the original assignment identity and the saved handoff.
A worker identity file names the fixed staff id.
Saved identity records do not select the next model.
Native policy checks retain a compatible running worker and restart it in place when its policy must change.
Review artifacts identify the pass and lens.
Every worker prompt repeats the Three Gods sentence and the Type Design Contract, directly or through a reference.
Usage reports name unavailable values and incomplete sources without changing the usage store.

## Calibration / gotchas

Worker reuse depends on fixed staff identity, assignment role and availability.
A rename does not authorize name-based replacement of an existing worker.
A missing completion message does not prove that a native turn stops.
Usage weights have dates and can differ between report windows.
`DEFAULT_T_LIVE` is `2026-10-10T18:17:00Z`.
The before window covers the preceding seven days.
Codex edit calls can contain `tools.apply_patch(` inside JavaScript.
Edit detection excludes quoted text.

## Design rules

Persistent conversations retain context between assignments.
Read-only reviewers and writers have separate access policies.
Checkpointed phases provide the comparison unit for usage reports.
Staff owns worker identity and parent relationships.
Native evidence owns current execution state.
