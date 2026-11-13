# Phase 10g: the queue owns placement

Settled design for Phases 10g, 44, 45 and 46 of `docs/plans/builds-queue.md`. Baseline `e6a76d2fc`. Reviewed by Astra on 2026-11-09. Offload references use function names.

## 1. Placement contract: durable assignments and single-use dispatch

The queue chooses the destination for every attempt.
The adapter claims that exact destination through the existing provider lock.
The provider owns physical claims, adoption, cleanup and uncertain departure.
An assignment never overrides a provider lock.

Persist each assignment before the adapter receives it.
Each assignment holds the episode, attempt id, destination, source key, submitting owner and original queue position.
The destination includes the provider's exact scope and selected slot, where a slot exists.
An assignment expresses a decision; it does not prove that capacity is reserved.
A `ReservedHost` requires a matching provider claim.

### Fixed constraints and changing evidence

| Fixed for one submitted step | Refreshed while the step waits |
| --- | --- |
| Episode, source key, command, repository and package | Reachability, disk floor and sweep result |
| Submitting process and session identity | Holds, block exemptions and overlapping claims |
| Pins, platform requirements and source writes | Free provider slots and host generation |
| Artifacts, fetch request and forwarded environment | Spending permission and deployment availability |
| Normal step or existing speculative copy | Memory, pressure, estimates and retry deadlines |

The request carries all configured candidates and fixed exclusions.
Dynamic exclusions belong to evidence, not the saved request constraints.
Configuration changes invalidate the affected policy evidence.
A changed source uses the existing cancellation and resubmission path with a new source key.

Ordinary movable steps follow `read_targets()` order: Mac, natedev, then AWS SSH capacity.
The queue chooses the first eligible destination with room.
A local destination still requires ordinary memory admission and hold checks.
A pin permits only its selected target.
Tree writes stay local, including the existing behavior under remote routing flags.
Keep `step_routing_rule` rules for local artifacts and `final` steps.

Keep current retry policy from `remember_mac_retry` and `ssh_retry_hook`.
Busy, blocked and disk declines remain retryable at the current poll cadence.
Keep the existing 180-second delay for other temporary Mac outcomes.
Keep permanent declines, pinned startup behavior and permitted fallback.
Preserve the episode, original `since`, run place and priority through each reassignment.
Each permitted retry receives a new attempt id.
Host preference does not create another queue priority order.

Scaler gates keep their cloud-first policy and permitted local fallback.
They do not acquire the ordinary Mac-first policy.
AWS SSH slots and scaler gates remain distinct destinations and capacity providers.
A gate's local type job remains a separate local step after remote suites pass.

### Assignment, claim and dispatch

1. Evaluate candidates from current evidence in existing queue order.
2. Persist one assignment with a new attempt id.
3. Send the assignment to its adapter.
4. Claim only the assigned provider scope through the existing target lock.
5. Report a failed claim to the queue before requesting another destination.
6. Adopt the exact successful claim as remote-bound under that lock.
7. Request launch permission for that assignment and adopted claim.
8. Validate the current assignment, source, owner, claim identity and adoption evidence.
9. Persist permission consumption through the existing durable replay ledger.
10. Send one launch authorization to that adapter.
11. Execute the assigned command once after receiving that authorization.
12. Report execution evidence and outcomes through the existing adapter path.

These steps describe remote dispatch.
Local execution still uses the existing memory reservation and process control.
Scaler dispatch uses its existing durable attempt reservation instead of an SSH claim.
Its unresolved launch remains charged by that provider.

The claim binds a unique claim id, episode, attempt, target, scope and local holder identity.
Adoption preserves that identity while binding the remote host and generation.
A provider release compares that exact claim under the target lock.
A PID alone cannot identify a release.
Cleanup takeover preserves the original claim identity and grants no execution permission.

Claim failure cannot cause the adapter to select another slot or host.
The queue closes the failed assignment and reevaluates the same episode.
The queue rejects permission consumption for an obsolete assignment.
A late successful claim goes through provider cleanup before it can be reused.
An assignment expiry does not release that claim.
An uncertain claim response requires provider reconciliation; it is not a failed claim.
Repeated provider inspection uses the same claim identity and grants no execution permission.

### One permission consumption exchange

The first valid consumption request durably changes `PermissionAvailable` to `PermissionConsumed`.
Only its first response authorizes execution.
Every repeated consumption request returns `DispatchStatus`, never another authorization.
This rule also applies when the repeated request uses another transport request id.
The durable key is the assignment's episode and attempt, not the socket or client request id.

The adapter executes at most once from that first authorization.
It never resends an uncertain remote start command.
A lost permission response can therefore leave a consumed attempt that never started.
That case accepts slower recovery.
A later status response cannot restore the lost authorization.

The generic replay handler currently returns the saved reply at `admission_service.py:2002`.
Permission consumption must use its own ledger semantics before that generic reply path.
Reuse the durable ledger and `persist`; do not replay an authorization byte string.
Preserve consumed status while the assignment or its claim remains unresolved.
Ledger pruning must not turn an old attempt into a new permission request.
An unknown old attempt receives status without execution authority.

No new remote attempt marker exists.
The existing runner process markers remain cleanup evidence.
They do not provide completed-command replay and need no placement rewrite.
The source-copy marker concern in review finding 6 falls away because this design adds no remote replay marker.

### Cancellation, absence and uncertain departure

Owner death cancels unused permission in the queue loop.
If consumption wins that race, the attempt remains potentially launched.
Before accepting absence, stop the adapter and resolve its pending claim and launch operations.
This barrier also applies after an assignment expires or the service restarts.
A remote scan that finds no process cannot precede that barrier.
Otherwise a paused adapter could launch after the scan.

Provider cleanup keeps the exact remote-bound claim until departure is confirmed.
A disconnected SSH process, local owner death, timeout or service restart proves no remote departure.
A lost launch response also proves no absence.
The queue reads provider evidence; it does not perform a second cleanup protocol.
The existing provider watcher can recover cleanup after the original adapter ends.

A definite prelaunch refusal closes the attempt after its claim is released.
The queue can then assign a new attempt with the same queue position.
A local retry passes ordinary local memory admission again.
If existing policy permits another attempt while cleanup remains uncertain, retain both provider references and charges separately.
Uncertainty never authorizes reuse of the affected scope or another launch of the consumed attempt.

The two existing defects in section 10 require repair before their affected placement paths switch.

The existing speculative copy remains a separate attempt while local compilation continues.
Keep newest-copy selection, copy yielding and the first-result rule.
The queue chooses its destination; the watcher retains result collection and loser stopping.

## 2. Map of today's routing

All paths below refer to the baseline checkout.
Offload references use function names because that file changes in another phase.

| File and function | Current responsibility |
| --- | --- |
| `scripts/direct/verify.sh::initialize_step_record` | Calls `eligible`, saves target rows, and converts eligibility into local or remote routing state. |
| `verify.sh::route_test` | Loops over targets, checks local holds, reserves remote capacity, and chooses fallback or retry. |
| `verify.sh::route_compiling_step` | Translates compiling commands and chooses local execution or the remote routing loop. |
| `verify.sh::run_local_test`, `buildlog_wait_for_memory` | Install queue-leave hooks that can select remote work during local waits. |
| `verify.sh::start_copy_watch` | Starts the speculative Mac copy watcher during local compilation. |
| `scripts/build_targets/build_targets.py::step_routing_rule` | Applies pins, tree-write constraints and local artifact rules. |
| `build_targets.py::command_kind_eligible`, `command_pinned_eligible` | Combine policy, target order, probes and live capacity into CLI answers. |
| `build_targets.py::ssh_target_eligibility`, `mac_target_eligibility` | Read claims, backoff, command policy, reachability and disk evidence. |
| `build_targets.py::command_reserve`, `command_release_wait` | Take process-bound target claims and release claims not adopted by a remote runner. |
| `build_targets.py::ssh_target_config`, `aws_spending_exclusion`, `aws_target_spending_exclusion` | Apply AWS settings and the spending guard before target use. |
| `build_targets.py::command_wait_local`, `command_check_local` | Wait for local holds; a leave hook can reserve remote capacity. |
| `scripts/build_queue/admission_service.py::run_pass`, `_cloud_pass` | Admit local memory work and cloud slots through separate passes. |
| `admission_service.py::_admit`, `_watch_owner` | Persist grants and watch a local submitting PID with a local pidfd. |
| `admission_service.py::OperatingSystemReader` | Reads system evidence outside the state-changing loop. |
| `scripts/lint/memory_admit.py::run_memories`, `inputs_room`, `head_policy`, `admission_walk`, `admission_pass` | Calculate local charges, continuation slots, pressure, spacing and queue protection. |
| `scripts/mac_test/offload.py::run_offload`, `leased_offload`, `claim_for_run` | Check eligibility, lease mirrors, choose a slot and obtain or adopt a claim. |
| `offload.py::claimed_run`, `release_claim`, `check_current_leftovers`, `observe_cleanup` | Copy and execute remotely; retain claims when departure is uncertain. |
| `scripts/mac_test/copy_watch.py::Watch.try_copy`, `newest_candidate` | Choose whether a speculative copy can start and which candidate wins. |
| `scripts/production/merge_gate.py::run_gate_suites` | Tries the cloud gate route and selects permitted local fallback. |
| `merge_gate.py::gate_reservation`, `BuildHoldWait` | Admit local gates and enforce local holds before execution. |
| `scripts/aws_target/cloud_gate.py::cloud_reservation`, `run_cloud`, `GateWorker.launch` | Reserve a cloud queue slot, launch through the scaler, and retain spending evidence. |

The path is `scripts/build_targets/build_targets.py`, not `scripts/build_targets.py`.
`memory_admit` owns local admission arithmetic; it does not currently choose a remote host.
`LocalPlace` and `CloudPlace` describe a client's prior choice.
Their current reply does not instruct a client to use a different host.

## 3. Semantic types and authority

Use closed variants that name each guarantee.
Convert missing transport fields into these variants at the boundary.
Do not encode unstarted execution and unavailable evidence with the same optional PID.

| Type | Meaning |
| --- | --- |
| `SubmittingOwner` | Coordinator host, local PID and start time; only this identity reaches a local pidfd. |
| `SubmittingSession` | The caller's existing hold identity: `OwningSession` or `NoOwningSession`. |
| `PlacementConstraints` | Fixed source, command, session and permitted destination constraints. |
| `HostSelection` | `ConfiguredOrder`, `PinnedHost` or the existing `ScalerGateOrder`. |
| `PlacementAssignment` | Durable episode, attempt, destination, source key, submitting owner and original queue position. |
| `AssignmentState` | `CurrentAssignment` or `ClosedAssignment(reason)`; neither state owns physical capacity. |
| `LaunchPermission` | `PermissionAvailable` or `PermissionConsumed`; consumption never resets for the same attempt. |
| `ProviderClaimReference` | Exact provider, target, scope, claim id, episode and attempt. It references the existing provider record. |
| `ReservedHost` | The current assignment and evidence of its matching provider claim. An assignment alone cannot create this value. |
| `ExecutionIdentity` | `ProcessExecution(host, generation, pid, start)` or `CloudExecution(target, generation, instance, attempt)`. |
| `ExecutionEvidence` | `ExecutionNotStarted`, `ObservedExecution(identity)` or `ExecutionEvidenceUnavailable(reason, observed_at)`. |
| `HostEvidence` | `FreshHostEvidence`, `StaleHostEvidence` or `HostEvidenceUnavailable`, with observation time and scope. |
| `HostEligibility` | `AllowedHost`, `FixedExclusion`, `TemporaryExclusion` or `EvidenceNeeded`. |
| `UncertainCleanup` | A view of the provider's retained claim, reason and last evidence time. It creates no second lifecycle record. |

Keep existing local memory reservations, SSH claim records and scaler reservations separate.
Add no `HostCharge` ledger or `HostCapacityState` machine.
Running, stopping and released rows derive from existing execution and provider evidence.
Persist only assignment identity, permission consumption, retry deadlines and provider references beyond existing queue records.
Do not copy provider cleanup state into another durable service state machine.

### Actual provider scope

`claim_state.target_claim_scope` at `scripts/mac_test/claim_state.py:364` defines the Mac scope.
A known Mac repository uses `RepositoryScope(repository.legacy_mirror_name)`.
An unknown repository uses `WholeMacScope`.
`mac_test.scopes_overlap` at `scripts/mac_test/mac_test.py:633` permits distinct repository scopes to coexist.
`slot_capacity` gives one claim per legacy Mac repository scope.
The Mac does not have one global slot for every known repository.

Linux SSH claims retain their repository key and configured slot.
The assignment names the selected slot; claim execution cannot substitute another free slot.
The provider lock still decides whether overlapping scopes can coexist.
Scaler gates retain their configured `cloud_gate_slots` limit and spending reservation.

### Process and session identities

Construct `SubmittingOwner` from the local coordinator's checked process identity.
Do not construct a local owner watch from a caller-supplied remote host or execution PID.
Only that submitting identity reaches `_watch_owner` and `Proc.pidfd`.
Sampling, grouping and release use the host-scoped `ExecutionIdentity`.
Equal PID and start time on different hosts remain different executions.
Keep a logical verify run id distinct from each physical attempt.

Pass `SubmittingSession` explicitly into local hold policy, remote block policy, claim creation and adoption.
The service's environment cannot substitute its own session identity.
Preserve existing `OwningSession` semantics and the caller's hold exemption.
Claim records retain the submitting session for existing block and cleanup behavior.
Cached eligibility keys include the session identity, target, provider scope, command policy and configuration generation.
Evidence for one session cannot exempt another session from a hold.

Remote execution releases any local continuation claim associated with that waiting step.
Remote memory cannot reuse that local slot or debit natedev memory room.
Unknown remote memory stays unknown; a provider slot is not a byte estimate.

## 4. Evidence and the service loop

Keep remote probes outside `run_pass`, as `OperatingSystemReader` keeps system reads outside the state-changing loop.
Background readers publish immutable target evidence through the existing wake path.
Only the loop changes assignments, permission consumption, queue records and replay entries.
Adapters perform physical claims, adoption, launch and cleanup outside that loop.
Do not add service-owned claim, adoption or cleanup workers.

Evidence carries target, provider scope, submitting-session policy key, observation time and configuration generation.
Bound freshness with the existing poll interval and target probe timeout.
Expired evidence becomes stale even when another target continues to update.
Keep retry deadlines separate from evidence age.
A stale observation prevents a new decision that needs that observation.
It never releases an existing provider claim.

Each pass applies incoming reports and owner deaths, then reevaluates unassigned waiting episodes in existing queue order.
An outstanding assignment remains attached to its attempt until it closes.
Evidence changes do not silently replace an assignment that can still consume permission.
A claim refusal closes that assignment before the queue selects another destination.
An adapter report names its exact assignment; a late report cannot authorize a different attempt.

Known provider claims constrain choices through the existing overlap and copy-yield rules.
A retained uncertain claim never yields its capacity.
Assignments themselves are not reservations and need no second capacity store.
If observations race, the provider lock serializes the actual claims.
Only the successful adopted claim can support launch permission.
The losing episode keeps its queue position and returns to queue evaluation.

Reuse `memory_admit.admission_pass` and its existing local accounting.
Do not repeat budget, starts-alone, continuation-slot, spacing or head-protection arithmetic in placement code.
Exclude remote-selected requests before committing local admission effects.
Commit local spacing only for an actual local memory reservation.
Fold cloud route choice into placement while retaining the provider's cloud capacity limit.

No network call, sweep, cloud API call or waiting provider lock occurs in the service loop.
View, release and eligible local admission must answer within one second during delayed remote evidence.
Claim and cleanup adapters can block without blocking those socket requests.
This contract gives no one-second limit for completing a remote placement.

## 5. Wire, replay and restart

The baseline wire version is 2.
Add version 3 placement messages while preserving version 1 and 2 decoding at the boundary.

| Message | Content and effect |
| --- | --- |
| Placement request | Episode, profile, constraints, submitting process and explicit session identity. |
| Placement wait | Original queue position and current eligibility for every candidate. |
| Assignment | Persisted destination, source key, attempt and owner; it grants no launch authority. |
| Claim failure | Exact assignment and reason; the queue closes it before another choice. |
| Consume permission | Exact assignment and adopted provider claim reference. |
| First authorization | One successful consumption response; only its receiving adapter can execute once. |
| Dispatch status | Current or obsolete assignment and consumed status; it authorizes no execution. |
| Execution or outcome report | Existing execution identity, provider reference, result or cleanup evidence for that attempt. |
| Running row | Execution host and identity, separate from submitting owner and session. |

Use one consumption exchange, followed by ordinary execution reports.
There is no launch-begin, launch-confirmation persistence handshake.
Execution reports provide evidence; they do not create another durable running transition.
The ledger atomically records the assignment's consumption before the first authorization response.
Repeated consumption returns status even when the first response disappeared.

Version 1 and 2 local requests remain fixed local requests.
Their cloud requests retain the existing fixed scaler route.
They receive their original reply shapes and retain their episodes and queue positions.
They do not receive version 3 assignments.
New clients do not restore client-side routing after a version disagreement.

Restore assignments, consumption records, provider references and existing retry deadlines after restart.
Reattach watches only to local submitting owners.
Read physical claims and execution evidence from existing providers asynchronously.
Do not infer provider release from a missing adapter connection or an expired assignment.
A consumed attempt returns status after restart and can never authorize another launch.
An unconsumed current assignment can continue only after its exact claim and owner pass validation.
Stop old dispatch activity before accepting absence and assigning a replacement attempt.

Keep unresolved consumption records live under the existing replay-retention mechanism.
After closure, an absent assignment cannot become a new attempt through a repeated consumption request.
Only a new queue decision can create that new attempt.
Provider retention still protects uncertain capacity after the queue restarts.

Schema changes need the existing external stop, convert, install and start boundary.
Convert only the new assignment and exact claim fields that implementation actually introduces.
Existing local and cloud requests retain their fixed destinations and existing ids.
Do not introduce migration for duplicate running, stopping or cleanup records; this design introduces none.
Only the user stops and starts the live service.
Drain older routing shells through the existing caller-drain path before declaring a caller family fully converted.
Do not terminate their work to meet a checkpoint.

## 6. Adapter handoff

The director gives verify-unit this function list before a writer edits its files.
The director agrees the target and lifecycle lists with aws-unit and sdlc.
These tables name changed functions only.
Unchanged transport, runner, copy, cleanup and lifecycle functions remain outside the edit list.

### verify-unit: verify shell and offload

| Function | Required change |
| --- | --- |
| `verify.sh::initialize_step_record` | Submit constraints and explicit session identity instead of saving permanent dynamic exclusions. |
| `verify.sh::route_test`, `route_compiling_step` | Consume queue assignments and dispatch the exact destination; remove independent target selection. |
| `verify.sh::try_reserved_target`, `try_mac_test` | Pass assignment identity and provider scope into the adapter. |
| `verify.sh::remember_mac_retry`, `ssh_retry_hook`, `mac_retry_hook`, `ssh_reserve_hook`, `register_mac_wait` | Move retry policy and target choices into the queue; delete unused hooks after conversion. |
| `verify.sh::step_can_retry` | Remove client eligibility-table retry decisions after queue policy replaces them. |
| `verify.sh::target_platform`, `target_for_settings` | Use assigned destination metadata instead of scanning the saved eligibility table. |
| `verify.sh::check_mac_only_result` | Consume the queue outcome instead of deciding whether another assignment is permitted. |
| `verify.sh::release_wait_claim` | Release only the exact provider claim or cancel the assignment. |
| `verify.sh::run_local_test`, `buildlog_wait_for_memory` | Await placement without a host-selecting leave hook; preserve token renewal and local execution prerequisites. |
| `verify.sh::start_copy_watch` | Supply copy constraints and source facts without selecting a host. |
| `offload.parser`, `request_from_arguments` | Accept assignment, claim and submitting-session identity; direct callers request their existing fixed destination. |
| `offload.run_offload` | Execute the assigned target and report declines without selecting another destination. |
| `offload.leased_offload` | Lease only the assigned scope and slot; report refusal instead of trying another slot. |
| `offload.claim_arguments`, `read_claim_answer`, `claim_for_run` | Bind acquisition and remote-bound adoption to the exact assignment and claim identity. |
| `offload.claimed_run` | Consume permission after adoption and before remote execution; report outcomes through its existing cleanup path. |
| `offload.release_claim` | Send the exact claim identity instead of releasing by PID alone. |
| `copy_watch.Watch.try_copy`, `newest_candidate` | Submit candidates to queue copy policy instead of choosing available remote capacity. |
| `copy_watch.start_copy` | Dispatch only the assigned speculative attempt. |

Keep the existing `with_token_renewal` wrapper around the converted routing wait.
Local token acquisition remains an execution prerequisite, not a destination decision.
`remote_test_command`, `linux_runner.slot_run`, `command_script` and `execute_slot` need no new replay marker or protocol.
`observe_cleanup`, `check_current_leftovers` and watcher stop operations keep their provider cleanup roles.
An adapter must resolve its existing pending launch operation before those checks can establish absence.
Keep existing first-result, loser-stop, fetch, log and source-lock behavior.
Keep source transfers through `copies.remote_rsync` and `RSYNC_RECEIVER_WRITE_TIME_FLAGS`.
`offload.copy_tree`, `copy_tree_with_key` and `copy_tracked_scripts` retain their current transfer semantics.
No placement adapter restores sender timestamps for changed content.

### aws-unit and provider integration

| Function | Required change |
| --- | --- |
| `build_targets.py::ssh_target_eligibility`, `mac_target_eligibility` | Supply refreshed evidence with explicit submitting-session policy. |
| `build_targets.py::command_kind_eligible`, `command_pinned_eligible`, `command_eligible` | Return queue eligibility for converted execution callers; retain diagnostic use. |
| `build_targets.py::command_reserve` | Claim only the assignment's scope; remove destination and slot fallback. |
| `build_targets.py::owns_wait_claim`, `command_release_wait` | Match exact claims and the explicit submitting session. |
| `build_targets.py::caller_holds_only_own`, `command_check_local`, `command_wait_local` | Accept caller identity at the policy boundary; remove host selection from converted hold waits. |
| `mac_test.blocking_claim_block`, `claim_once`, `adopt_wait_claim`, `command_claim_wait`, `command_claim` | Use explicit session identity and exact scope; persist exact attempt identity before dispatch. |
| `mac_test.command_release` and its argument boundary | Release only the matching claim under the existing target lock. |
| `cloud_gate.cloud_reservation`, `run`, `route` | Request placement and return route outcomes instead of selecting local fallback. |
| `cloud_gate.run_cloud`, `GateWorker.execute` | Consume one assignment's dispatch permission at the launch boundary; report provider outcomes. |
| `cloud_gate.GateWorker.launch`, `reconcile` | Retain uncertain launch reservations and reconcile with existing attempt and client-token evidence. |

Verify-unit owns the generic Mac claim API edits with queue-unit's adapter worker.
aws-unit owns the cloud launch correction.
The director coordinates that shared `mac_test.py` edit before implementation.
The existing spending ledger stays authoritative at the actual spending boundary.
No change to `ledger.py` pricing, limits or reservation arithmetic is proposed.

### sdlc and shared queue adapters

| Function | Required change |
| --- | --- |
| `merge_gate.run_gate_suites` | Submit existing cloud/local constraints and execute the queue's selected route. |
| `merge_gate.gate_reservation` | Consume the selected local reservation instead of making a second request after fallback. |
| `merge_gate.BuildHoldWait.admits`, `wait_until_free` | Use explicit submitting identity for hold policy. |
| `lint/memory_gate.sh::buildlog_wait_for_memory` | Forward placement constraints and session identity; await the queue's assignment. |
| `lint/memory_gate.sh::build_hold_wait_local` | Remove host-selecting leave hooks for converted requests. |
| `lint/invoke.sh::run_step_attempt` | Use the selected local reservation without a second admission request. |

`merge_gate --tests-only` takes the same queue route as a full gate and keeps its exit codes (sdlc's condition, 2026-11-09).
`checkpoint_gate`, `bump_gate`, `ci_points`, `waiting` and `fast_track` need no new host selector.
Their existing shared gate or verify path receives placement; their result and checkout policies stay unchanged.
Standalone lint, cargo and `queue_place` retain fixed local requests.
Extend the queue protocol, client and service for assignments, consumption and host-scoped rows.
Extend grouping and readers where execution identity reaches queue views, cards and the memory feed.
Delete obsolete client leave-hook selection only after its final caller switches.

## 7. Acceptance tests

Use private roots, fixture loop turns, fake transports and existing provider locks.
No test contacts a live service, session, Mac or AWS account.
Test the contracts rather than copying implementation arithmetic.

| Plan item or review | Required behavior |
| --- | --- |
| 2, 13 | A disk decline blocks Mac placement while local memory blocks execution. Refreshed disk evidence assigns the same waiter to the Mac. |
| 13 | Busy, reachability and spending evidence can change during a wait. Fixed platform and source-write exclusions remain fixed. |
| Review 4 | Two episodes request the same Mac `RepositoryScope`. The provider lock admits one claim; the loser returns to the queue. |
| Review 4 | Distinct known Mac repository scopes can coexist. A `WholeMacScope` request overlaps both under existing rules. |
| 13 | Two episodes request one Linux repository slot. At most one adopted claim supports launch permission. |
| 13 | An external claimant wins after free evidence. The adapter reports refusal; the queue selects again with the original position. |
| Review 1 | Pause an adapter after assignment. Expire and replace the assignment, then allow its claim. Obsolete permission is refused; cleanup retains exact identity. |
| Review 2 | Death before adoption permits only unlaunched cleanup. Death after adoption retains remote-bound capacity before any permission exists. |
| Review 2 | A late release from attempt A cannot remove attempt B held by the same local process. |
| 7 | Remote placement releases the waiting step's local continuation claim. Remote memory never consumes that local slot. |
| 7, 13 | A definite prelaunch refusal releases its exact claim. A later local attempt passes normal local memory admission. |
| 12, 13 | Owner death before consumption cancels permission. A consumption race retains potential execution until pending launch work resolves. |
| Review 1, 8 | Lose the first authorization response. Repeated consumption returns status; the command executes zero times. Recovery uses a new attempt. |
| Review 8 | Deliver the first authorization and repeat consumption requests across restart. The command executes once, never twice. |
| Review 8 | Pause an authorized adapter before startup. Absence cannot release its claim until the adapter and pending start operation stop. |
| 13 | Unreachable cleanup retains the provider claim across restart. Another episode cannot reuse its overlapping capacity. |
| Review 3 | Cloud launch succeeds but its response disappears. The reservation stays pending; reconciliation finds the existing instance without another dispatch. |
| Review 5 | A submitting holder keeps its exemption inside service policy. Another session cannot use that cached result or claim exemption. |
| 9, 12 | Equal PID and start time on different hosts remain separate executions. Only the local submitting owner reaches pidfd after restart. |
| 8 | View, card and memory feed show the Mac for a Mac execution. Mixed local and cloud labels remain correct. |
| 11 | Block remote evidence, claim and cleanup operations separately. View, release and eligible local admission answer within one second. |
| 11, 13 | Evidence expires during a blocked probe. New placement sees stale evidence; existing claims remain held. |
| 13 | Pins, target order, holds, priority, retry deadlines and Mac-only refusals retain existing suite behavior. |
| 13 | A speculative copy yields under current policy. Its uncertain loser retains the provider claim until departure is confirmed. |
| Wire | Restore old local/cloud waiters, an unconsumed assignment and a consumed uncertain attempt. Preserve positions, identities and permission status. |
| 14 | Shared transfers give changed content receiver write time. Unchanged content retains its earlier time. |

Claims may race before the provider lock resolves them.
The competing-episodes test does not treat assignments as exclusive capacity grants.
It proves exclusive overlapping claims and single-use dispatch instead.
Use a real fixture socket for the one-second limit; planner duration alone is insufficient.
Keep existing routing, offload, copy, spending and target-slot assertions.

## 8. Checkpoints

Each checkpoint leaves one host-choice owner for each converted caller family.
No episode runs both client-side selection and queue placement.

1. **Assignment and provider foundations.** Add asynchronous evidence, explicit session identity, exact claim identity and durable assignment/consumption protocol.
   Repair PID-only release before its placement path switches.
   Preserve old fixed-destination decoding and provider retention during restart.
2. **Complete verify conversion.** Switch verify, offload, copy-watch and shared lint adapters together.
   Adapters claim and adopt exact assignments, then consume permission once.
   This checkpoint first fixes the 14:04 disk-decline defect.
   Include host identity, correct local accounting and the relevant Machine labels in this checkpoint.
3. **Scaler and lifecycle conversion.** After aws-unit repairs uncertain cloud launches, switch cloud/local gate route decisions to the queue.
   Preserve spending limits, scaler slots, cloud-first policy and lifecycle results.
4. **Reader completion and deletion.** Complete every host label and remove unused selection hooks and target loops.
   Verify restart, delayed evidence, lost authorization and caller coverage across the accepted suites.

Send the accepted verify function list before checkpoint 2 writers edit those files.
Checkpoint 1 introduces no remote command marker or second provider lifecycle.
Checkpoint 4 does not defer correct host accounting from an earlier converted path.
Old routing shells drain through the current caller-drain mechanism.

## 9. Scope and review disposition

| Review finding | Design response |
| --- | --- |
| 1 | Durable attempt identity and current-assignment validation reject obsolete execution. Expiry never releases a provider claim. |
| 2 | Exact claim identity replaces PID release. Remote-bound adoption precedes permission consumption. Provider retention remains authoritative. |
| 3 | Section 10 keeps the uncertain cloud launch repair. aws-unit removes the older builder's release code, so its two generation defects need no repair here. |
| 4 | Section 3 uses actual Mac repository scopes. Section 7 tests overlapping and distinct scopes separately. |
| 5 | Explicit submitting-session identity reaches hold and claim policy and every relevant eligibility cache key. |
| 6 | No new remote replay marker exists, so source copying cannot delete such a marker. |
| 7 | Acceptance cases retain the required guarantees through durable assignments, one consumption exchange and existing provider cleanup. |
| 8 | This document adopts the smaller design and removes the separate alternative. |

Round 1's service claim workers, service adoption and service cleanup procedures are removed.
Separate durable running, stopping and released records are removed.
The eight-state capacity machine, additional charge ledger and remote replay markers are removed.
The three-message launch sequence becomes one consumption exchange plus execution reports.
Migration for duplicated lifecycle records is removed.
Durable attempt identity, exact release, consumption, uncertain retention and restart reconstruction remain.
Unstarted execution remains distinct from unavailable execution evidence.

| Plan items | Minimum work |
| --- | --- |
| 1–5 | Queue policy and refreshed evidence; adapters claim the exact assignment without choosing another target. |
| 6 | Parallel scheduling with 10f can wait; it adds no runtime behavior. |
| 7–9 | Keep separate providers and carry the actual execution host through accounting and readers. |
| 10 | Divide implementation, adapter and test work after this contract is accepted. |
| 11–12 | Asynchronous evidence and distinct submitting process, session and execution identities. |
| 13 | Durable assignment, exact provider claim, adoption before single-use dispatch and provider-owned cleanup. |
| 14 | Retain shared transfer flags and timestamp tests without a transfer rewrite. |

Per-host admission daemons, new transports, distributed consensus and cross-host memory arithmetic remain outside this phase.
Live process migration and adaptive host scoring remain outside this phase.
A replacement for existing local, SSH and cloud capacity stores is unnecessary.

## 10. Existing defects

These verdicts follow direct source reads at the baseline.
They are not results from executed tests.
Both defects affect required placement guarantees.
The review also named two defects in the older AWS builder's release code (`stopping` as departure and any generation mismatch).
aws-unit removes that builder outside this work, so no placement path builds on that code.

| Defect and verdict | Source and shortest failure | Fix owner and phase requirement |
| --- | --- | --- |
| PID-only release: real. | `scripts/mac_test/mac_test.py:1943`, especially `:1952–1956`, deletes every claim with `args.pid`. Attempt A releases after attempt B claims under the same process; B disappears. | Verify-unit's claim adapter, coordinated by queue-unit. Bind release to exact claim and attempt before checkpoint 2. |
| Uncertain cloud launch recorded absent: real. | `scripts/aws_target/cloud_gate.py:305–312` resolves a non-capacity error as `GateAbsent`. `:384–387` repeats this when no instance id was saved. An accepted launch loses its reply; the instance runs after its reservation becomes absent. | aws-unit's cloud gate integration. Retain pending accounting and reconcile before checkpoint 3. |


The cloud path already saves launch arguments at `cloud_gate.py:274–275`.
Its client token uses the attempt and variant at `:265`.
Use that existing evidence for reconciliation; do not infer absence from a missing saved instance id.
The proof of a pending launch is `describe-instances` with the saved client token, as the expiry path at `cloud_gate.py:280` does.
One row proves `GateRunning`. Zero rows before the request expires prove nothing, and the reservation stays.
At expiry the reservation can resolve absent. A definite AWS refusal stays absent at once.
`GateWorker.reconcile` at `:198–215` must also respect the pending-launch barrier before accepting an absence observation.
Provider API retries with the same client token do not grant another queue dispatch or authorize another command execution.

The existing retention at `mac_test.py:601–606` is correct and remains.
The lock-protected adoption at `:1842–1885` remains the mechanism, with exact identity and explicit session arguments.
Offload's `release_allowed` cleanup decision at `offload.py:2291–2297` remains the release authority after its evidence barrier.
No separate cleanup implementation is needed.

## 11. Open doubts

Verify-unit must map the handoff functions onto its incoming offload revision before editing shared files.
The adapter implementation must show how it settles a pending remote startup before cleanup accepts absence.
The ledger implementation must retain consumed status without replaying authorization or extending unrelated replay history.
The local planner must avoid committing spacing or continuation effects for a remote-selected waiter.
Existing routing suites decide detailed retry behavior where this design adds no policy.
The blocked-operation socket tests still must prove the one-second response limit.
No test, benchmark, live probe or deployment ran during this design task.
