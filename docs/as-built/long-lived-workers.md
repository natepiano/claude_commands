# Long-lived workers and held Astra roles

## Native workers and detached commands

Live consumers read staff membership and backend evidence.
`StaffSnapshot.backends` derives backend evidence from each current native session.
`session_staff.worker_backend` returns recorded evidence or a reason for unavailable evidence.
Worker displays and cleanup read `worker_panes.observe_parents`.
They show stopped, closed, unknown and incomplete states.
`inventory.worker_resumes` keeps Claude child commands in `ClaudeChildResume`.
`refusal_watch.py` owns `QUOTA_REFUSAL`.

The assignment launcher uses native worker panes.
Detached commands use `detached_runs.py` and the `detached_runs/` record folder.
Completed run folders keep their roster files.
`progress_history.py` and `usage/agents_log.py` keep their historical reads.

## What it is

Unit workers retain their native conversations across assignments.
Staff records identify each worker independently of its current session, run or account.
A later assignment resumes the same conversation when the worker remains available.
Codex code review uses read-only review workers.
The [daily simplify run](simplify-lens.md) uses a fresh Claude review.
Workers compact between assignments when their recorded context exceeds the configured limit.
Unit pause, move, and end use staff membership.
Usage reports compare checkpointed phases through the usage store.

## Pane submission and completion

`assign.sh` returns after successful prompt delivery. Exit 0 means submitted.
The transcript can record the prompt later.
A failed delivery command returns an error.
The transcript prevents replay.
A matching final transcript reply proves completion.
A matching dispatch prompt also permits completion from the hook's own DONE reply before the final transcript record appears.
The native hook sends its payload to a separate handler through stdin and returns.
The child reads stdin before it applies the completion rule.
The payload stays outside the child argument list.
The handler runs once outside the terminal session.
The pass retains its id, launcher arguments, reply path, and completion actions.
The director ends its turn after the assigned synchronous review.
The turn hook sends the director the worker, phase, outcome, and pass id.
The notice includes the reply path when the reply has text.
The director reads that reply when the message resumes it.

`scripts/herdr/turn_end.py` serves both families.
Codex uses the global `Stop` handler installed through `scripts/hooks/codex_hooks.py`.
Codex worker launch lines contain no `-c notify` argument.
Claude receives an asynchronous `Stop` command through launch-line `--settings` JSON.
The launcher changes no account settings file.
The hook finds the worker through its native conversation id.
It follows a per-worker link to the dispatch's existing state file.
The link lives in `dispatches/<staff id>` beside the staff record.
This lookup includes dispatches outside the run root.
Older workers without a link use the held-run lookup.
A session without a worker pass changes nothing.
A later event retries unfinished completion steps for a completed pass.

An identical rerun of a completed dispatch sends one completion message through the hook's notice code.
It saves the reply without typing another prompt.
This rule also applies to a completed managed Astra review.
The saved completion includes the reason and pane for a close anomaly.
A successful completed rerun sets its status file to `done` after it sends the notice.
Native duplicate hook events still send no second message.
Each completion notice ends its first line with `; pass <dispatch instance id>`.
An explicit rerun repeats the saved completion text with the same pass id.
Each notice uses the worker's native identity and name, including a notice from the director's rerun.
The saved completion supplies the sender identity for a retired managed Astra reviewer.
The sender identity remains the same after a session change and retirement.
Turn and run events send no notice when the outcome, reply, and run ids match the last notice.
A changed outcome, reply, or run id sends one notice.
Waiting notices add `; runs <run ids>` to the first line.
The run ids include received results that still await a worker reply.
An empty turn reply writes no reply file and adds no `; reply <path>` text.

A new Codex worker has no resume argument.
Its pane session report supplies the first native identity.
The identity reader excludes only `scripts/herdr/turn_end.py` from the foreground process count.
Every other foreground process prevents the pane report from proving a sole Codex process.

`worker.dispatch_receipt` reads the transcript and detached run evidence.
A live detached run holds DONE. A finished run permits completion with an anomaly for an absent transcript result.
A pending run reply or friend reply keeps the pass active without another prompt.
Run events preserve the last worker reply while a result awaits a reply.
A live run reports `waiting on runs`, including when an earlier result awaits a reply.
A worker puts its question in its reply and ends the turn.
The turn hook sends that reply to the director.
An unexpected screen prompt ends no turn, and the director finds it only when it reads the pane.
A turn without DONE reports `no DONE` when no pending work explains it.
The notice includes the worker's last reply and leaves the pass active.
The completion hook saves the reply, records the exact pass, runs completion actions, and closes the pane.
The close accepts the saved DONE hook completion for the same dispatch.
It retains its status, live-run, and idle checks.
`progress_history.complete_dispatch` records both completed passes and failed managed Astra review passes.
It matches the slot and dispatch instance id before it records an outcome.
A refused launcher retry retains the existing pass and its run, including a run with one worker.
A later DONE reply corrects a launcher error for the same pass and permits pane closure.
The review trial uses the latest completion duration for each pass.
Named adversary and contract reviews use the same completion hook.
Their DONE reply records `done` and sends one notice.
The hook also saves an assigned style review result.
A close anomaly retains the completed outcome and reaches the director in the hook message.
A held reviewer role remains held after submission. Completion ends that reviewer and releases the role.
Native process exit alone does not prove that a pane can close.
The close retains a pane until it observes its shell or confirms pane closure.
The close adds no wait for foreground cleanup.
A failed stop reports the pane id and the program name of each foreground process from the proof read.

Event producers call `handle(WorkerEvent(thread_id, EventKind.RUN_FINISHED))` after a run record reaches `finished`.
The final diff event uses `EventKind.STAGE_READY` for a review with two stages.
`detached_runs.py` calls the handler after supervisor completion and orphan completion.
The director sends the final-diff event with `turn_end.py stage-ready <worker>`.

### Hook input sources

The [Codex Stop reference](https://learn.chatgpt.com/docs/hooks#stop) defines JSON on stdin.
Its fields include `session_id`, `turn_id`, `cwd`, `hook_event_name`, `stop_hook_active`, `transcript_path`, `model`, and `last_assistant_message`.
A null `last_assistant_message` becomes an empty reply.
Saved launch records can retain legacy `notify` arguments.
The handler ignores their `agent-turn-complete` payloads.
The global Stop hook supplies completion for those workers.
The [Claude Stop reference](https://code.claude.com/docs/en/hooks#stop) defines JSON on stdin.
Its fields include `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `stop_hook_active`, and `last_assistant_message`.
The hook uses the native id to select the worker.
The hook applies the completion rule described above.

### Stop hook deployment

The director installs the hook after deployment.
Each registered Codex home must trust the shared `hooks.json` handlers.

1. Set `CODEX_HOME` to the account home and run `scripts/lib/py scripts/hooks/codex_hooks.py install`.
2. Run `scripts/lib/py scripts/hooks/codex_hooks.py check` with the same `CODEX_HOME`.

The installer reports the `turn-end` handler as trusted.
A worker needs no new launch-line setting to use the installed Stop hook.
Every Codex session runs the handler at each turn end. A non-worker turn exits as ignored and adds one line to `~/.local/state/herdr/turn-end-ignored.jsonl`.
A Codex process that started before the Stop hook was installed (2026-11-09 08:27 EST) runs no Stop hook. Measured: such a process finished turns after that time and added no line.
Close that worker's pane. Its next assign starts a new process that has the hook.
A pane launched with `-c notify` sends a payload that the handler saves as an ignored line with an empty thread.
Why: legacy notify passed the payload as one argument, and Linux refuses one argument over 128 KiB. Long-lived threads lost turn ends that way (2026-11-09).

## Unit code reviewers

Unit reviewers use read-only access.
Writers retain their writing roles.
Concurrent review assignments use separate worker identities.
Titles use the current staff placement and assignment role.
`workers.shown_name` numbers unit workers in their parent's staff order.

## How it works

### Staff identity and admission

A worker has one fixed staff id, name, parent and worktree.
Its `role` records its last dispatch. Its `home` records the absolute account folder.
`KnownWorkerRole | WorkerRoleUnknown` and `SessionHome | HomeUnknown` represent these values.
Missing keys mean unknown. Serialization omits missing values.
An invalid value remains unknown and retains its raw JSON value.
`progress_history.begin_dispatch` uses `staff.set_worker_role`.
Pane publication uses `staff.set_home`.
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
A failed pane admission closes only the new pane that this call created.
For an existing pane, the launcher quits its own agent and keeps the shell.
Both paths compare the current terminal and process with the identities saved after launch.
A replacement process remains open. Cleanup prints one anomaly with the pane id.

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
The launcher retains its original prompt identity and its dispatch record.
`review.sh` has no pending dispatch check and does not restore a saved extract.

### Claude coordinator and friends

`assign.py.execute_herdr` sends normal assignments through `worker.py` and native worker panes.
The retained `scripts/direct/claude_worker.py` API runs Claude coordinator steps within its caller's process.
`assign_claude._run` calls its functions directly.
The module has no command line or shell protocol.
It reads `CLAUDE_WORKER_REGISTER_WAIT_SECS` before it stops a source.
It verifies the destination against the running Claude registry before staff publication.
It records the role before delivery.
Delivery starts one Python child through `scripts/lib/py`.
The child calls `send.py --to session:<id> --from launcher --file <prompt>`.
`CLAUDE_WORKER_SEND_SCRIPT` selects the delivery script.

`assign_claude.run(ClaudeLaunch)` provides the retained coordinator API and its integration fixture entry.
`ClaudeLaunch.worker` selects `NewClaudeWorker` or `ClaudeFollowUp`.
`claude_worker.open_worker` and `follow_up` take a `ClaudeTurn`.
The launch returns `Delivered(native_id, turns_before, launch_environment)`, `Unchanged`, or `NotDelivered(code)`.
`TurnsNotCounted` represents a new session without an earlier turn count.
`wait_for_turn` reads Claude agent listings and logs in the same process.
`CLAUDE_WORKER_POLL_SECS` sets its interval; `UNIT_WORKER_POLL_SECS` overrides it.
One `nix_env.ensure` supplies the settings and start environment.
The account step uses `accounts.account_for_unscoped_launch`.
The banner's `backgrounded` line supplies the native id.
The registry lookup resolves that id within `CLAUDE_WORKER_REGISTER_WAIT_SECS`.
`assign_claude._run` records published workers in `finally` before it starts the heartbeat watch.
The watch follows the coordinator process during the wait.
A signal after the banner stops an unpublished session through `claude stop`.
An unreadable log supplies a fallback summary.
A transcript record without a message object adds no turn.
The launch uses exit 127 for a missing binary and exit 2 for refusal or failed delivery.
Other launch failures use exit 1; TERM, INT, and HUP use 143, 130, and 129.

A retry that finds its original user prompt in the transcript records acceptance without another send.
A complete transcript without that prompt permits delivery.
A damaged transcript without that prompt refuses uncertain delivery.
The refusal says `Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused`.
A missing transcript keeps the published session and places the saved handoff before the assignment.

### Launcher claims

`worker_state.publish_worker_claim` writes `impl_claim_<slot>.json` atomically.
The record holds `pid`, `worker`, and the process start in `started` when available.
An unreadable start prints an anomaly and leaves `started` absent.
`read_worker_claim` returns `HeldWorkerClaim(pid)` when the start matches or the current start cannot be read.
It returns `EndedWorkerClaim` for a gone process, zombie, changed start, or missing recorded start.
An absent or malformed record, or a record without a positive pid, gives `NoWorkerClaim`.
Claim release compares the recorded pid with the launcher pid.

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
A busy recipient gets only its `own_pass` continuation.
An idle worker row receives no prompt.
A director can receive the list of busy and idle worker children.
Restore reopens only workers with active dispatches.
It reads each worker's dispatch record and transcript once to select the resume step.
An accepted dispatch gets a continuation prompt only when the worker is idle.
Restore holds the dispatch lock across the record read, receipt read, and prompt delivery.
An unsubmitted dispatch keeps the saved assign command as its step.
Restore, named resume, and director notes use the same receipt rule.
Director notes keep the saved prompt path and reuse shared dispatch history reads.
A dispatch with a DONE line or a finished record gets no resume note or step.
Later chat does not reopen a completed dispatch.
The restart shares its run index and snapshots.
Idle workers stay closed until their next assign.
Account moves must preserve closed idle workers.

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

Staff supplies worker names, parents, worktrees, and backend identities.
A rename or transfer preserves the fixed staff references.
`rename.py` changes the unit row and repairs its session and display names.
A changed unit name changes matching folder and branch suffixes.
`--to-branch <name>` sets an explicit branch.
Every unit branch change pushes the target to origin before sessions resume.
Equal old and new unit names keep the folder and staff name.
See [production rename](production-rename.md) for unit branch moves.

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

`DamagedDesignRecord` identifies the record path and damaged field.
Holder-aware scans decide whether matching damage prevents the requested operation.
Writes validate the complete design before publication.
An omitted binding repository uses the authority checkout; an empty repository refuses.
`rebind --unbind` or a complete replacement binding can repair an empty repository under the generation lock.
Unrelated damaged records produce one warning per operation and do not stop other holders.
`unbind-run` refuses matching damage, including damage without a live role.
Plain `list` keeps diagnostics on stderr; JSON retains `problems`.
An identical `rebind` retry completes only after launch context and census publication agree.

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

A design role uses `worker.py` operations.
`worker.py list --json` gives each worker row `name`, `id`, `current_session`, `state`, `role` and `activity`.
The staff row supplies `role` and `home`. The `home` folder name supplies the account label.
`work` is `idle`, `busy` or `unknown`, from `worker_panes.worker_state`.
An unknown role uses `WorkerRoleUnavailable`. An unknown home uses `AccountUnavailable` and omits `home`.
`worker.py list --json --all` reads workers for every manager and adds each parent's name as `manager`.
It needs no caller row and makes no process, pane, or thread read.
Its `state` uses the saved dispatch status or staff hold.
Its `activity` uses the saved pass activity, then its status.
History lookup resolves fixed manager ids through the staff record, even after a session change.
History lookup reads the last 64 KiB of each file, plus one byte to skip a leading partial line.
History-only dispatches outside this window use the same unknown fields as missing dispatches.
It skips invalid lines and decodes only wanted worker events.
Listing reads no role map or account history. Missing dispatch history does not change the staff role or home.
Per-manager listing keeps its live observations.

History lookup resolves fixed manager ids through staff, even after a session change.
It merges events from newest to oldest across files.
A file search stops when newer dispatches supply each worker's state or all remaining events precede each worker's creation.
A UUIDv7 staff id supplies its first conversation's creation time.
Older id formats have no creation bound.
The reader skips invalid lines and keeps history events absent from the current pass slots.
Stored account history resolves only the unit group and reads its history once.

The authority host comes from `[ownership]` in `config/design_roles.conf`.
The configured authority host owns placement.
Both roles remain on the design's recorded host.
Host transfer is unsupported.
Repository registration requires each host's absolute checkout mapping.
Supply every host mapping on the first registration.
Use `register_repository` for multiple mappings.
Repeated registration must keep the same mappings.
Placement uses a hash of the repository id and relative document path.
The authority supports registration, reservation, lookup, generation advance, checkout changes, and aliases.
An authority error stops the local lifecycle operation without changing placement.
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
An unbound design opens `astra-review` only when that phase is closed.
Each `dispatch_turn` records `finish-phase` after assign returns, with `completed` or `error`.
A failed recording after refusal preserves the dispatch refusal text.
Ending an active unbound design records `finish-run --status completed` under the lock.
A phase-bound design uses the unit run and records no separate design phase.
The one-shot managed review closes its phase and run in `assign.py` after worker cleanup.
`worker.dispatch` owns successful pass completion; `assign.py` records pass failure only.
Detached command results use `detached_runs.deliver` and `send.deliver_worker_text`.
The result carries `Detached run: <run_id>` as its stable marker.
Under a per-run delivery lock, `detached_runs.deliver` checks the worker transcript for that marker.
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

`scripts/herdr/worker_panes.py` owns `worker_lock` and `WorkerLockHold`.
`worker_lock` holds the parent's named creation lock until the new worker has a fixed staff id.
`WorkerLockHold.bind` locks that fixed id before creation serialization ends.
A competing dispatch returns `Kept` when the lock is held.
`worker.py dispatch` holds this lock from pass creation through prompt submission.
It releases the lock before the completion hook ends the pass.
`worker_dispatch_state` keeps an active pass with a saved submission held during this gap.
A transcript that ends with the dispatch DONE line proves completion before the hook records the pass end.
An active pass without a saved submission, a held worker lock, or a live detached run is stale.
`scripts/production/staff.py` owns `slot_operation` and `row_operation`.
These locks protect native identity publication and family changes.
Inherited child processes share the staff lock through its file descriptor.

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

## Displays and messages

The roster, board and account display read staff members and pane evidence.
`worker_panes.observe_parents` shares one pane read across the selected parents.
Each display preserves the reason for incomplete evidence.
A missing pane record does not prove that a worker has ended.

Staff worker addresses use `worker:<parent>/<worker>`.
Message selection resolves the fixed staff id and current session.
Pane delivery uses the observed pane and current holder.
Daemon sessions retain their `codex:<thread>` route.

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

## Shared helpers

`worker_lifecycle.py` owns watchers and watcher events.
`finished_run_status` has the same owner.
`herdr.py` owns `loaded_threads`.
`codex_test_support.py` owns the shared launcher fixtures with their existing names.

## Pane closure and restore

The completion hook closes a worker pane after DONE.
An immediate repair or phase dispatch can retain the pane.
A live detached run keeps the pass active, so the hook closes no pane until that run ends.
The close reads no process. It closes the pane at once.
Closure keeps the staff row and thread.
The next assign resumes that thread in a new pane.
An account move resumes an open or stopped worker in its same pane and closes no pane.
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

`worker.py list` text rows read `<n> <name> <role> <account>[ <machine>] <pane> <state>[ <reason>]`.
`_worker_pane_state` is the one mapping from an observed worker to its pane and state words.

| Observed worker | Pane | State |
| --- | --- | --- |
| `OpenWorker` | `open` | `paused` or `exited` from the staff row, else `busy`, `idle` or `unknown` |
| `StoppedWorker` | `open` | `exited` |
| `ClosedWorker` | `closed` | `-` |
| Any other class | `unknown` | `-`, and the row keeps its reason |

A row whose pane is not open prints `-` for the account.
`list --all` prints `-` for the pane and the account, and a state from the dispatch status.
`display_cells.machine_column` decides the machine field.
The field shows only when a row uses a machine other than the home machine in `config/build_targets.conf`.
A worker uses the machine of its parent's worktree.
`list_workers(as_json=False)` replaces the `state` key with the column word, so the key holds the observed class name only in JSON output.
`list --json` and `list --json --all` have no pane column and no machine field.

## Assignment settings and command files

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
