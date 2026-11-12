# Shared Spot instances for cloud gates

This document proposes a design for morty item 79.
It changes no code and authorizes no experiment or deployment.

## Fixed behavior

| Outstanding runs | Placement while earlier runs remain outstanding |
| --- | --- |
| First | Start instance A immediately. |
| Second | Reserve the waiting slot on A. |
| Third | Start instance B immediately. |
| Fourth | Reserve the waiting slot on B. |
| Fifth | Start instance C immediately. |

Each instance has one active run and at most one waiting run.
Normal admission, capacity, and spending checks still apply.
An active run includes preparation, tests, and durable result delivery.
If no waiting run exists at completion, the owner closes the instance immediately and requests termination.
There is no idle timer or period for collecting another request.
The close decision and a waiting-slot claim use the same lock.
A request that loses that race starts another instance.

The rule limits outstanding assignments.
After promotion, the instance can receive another waiting run before its fixed expiry.
The experiment submits exactly two runs to each shared instance.
Neither a 120-second nor a 300-second idle period forms part of this design.

## Evidence and expected tradeoff

The user supplied these measurements on 2026-11-11.
The evening correction in `followup_numbers.md` replaces the earlier saving claim.

| Measurement | Supplied value |
| --- | ---: |
| Launch to kernel | 7 s |
| Kernel to user data | 14 s |
| User data to Nix shell | 23 s |
| Preparation | 4 s |
| Tests | 203 s |
| Result copy | 15 s |
| Termination | 15 s |
| Mean ledger interval | 278 s |
| Corrected fixed overhead | About 74 s per run |
| Fresh request to first test | About 50 s |
| Replay wait on an existing instance | Median 114 s; p90 below 183 s; maximum 329 s |
| Replay launches | 168 for 313 runs |
| Maximum saving per reused run | USD 0.0062 |
| Maximum saving for 145 reused runs | About USD 0.90 per day |

The rounded stage observations do not reconcile exactly with the mean ledger interval.
The replay launch counts remain valid.
The claimed one-third cost saving is withdrawn.
The replay median wait exceeds the supplied fresh startup time by 64 seconds.
That arithmetic is not a measured paired difference.
Sharing can reduce launch cost while increasing the waiting run's latency.
The experiment must measure both effects.

Today, `GateWorker.execute()` terminates after each attempt in [cloud_gate.py](../../scripts/aws_target/cloud_gate.py).
The production shell also shuts down after the guest exits in [gate_box.py](../../scripts/aws_target/gate_box.py).
Both decisions must move to the instance owner in shared mode.

## Local ownership and admission

Use one existing-style systemd worker per physical instance on natedev.
Do not add a network service, remote queue, or controller database.
Extend `CostLedger` with instance rows and run assignments in its existing atomic state file.
These rows form the shared registry and use the existing accounting lock.
Keep legacy reservations for mode `off` and historical records.

An instance row holds its local alias, deployment compatibility, launch generation, expiry, owner unit, lifecycle state, and assignments.
Its private resource mapping remains local and never appears in reports.
The lifecycle states are `launching`, `running`, `closing`, `lost`, and `terminated`.
Each assignment has a logical run key, execution generation, request path, timestamps, and state.
Assignment states are `waiting`, `active`, `complete`, and `retry`.

Claim procedure:

1. Save the exact request and source bundle durably on natedev.
2. Acquire the accounting lock.
3. Select the oldest compatible instance with a free waiting slot and no close decision.
4. Check the spending cap for the assignment and its transfer allowance.
5. Save the waiting assignment, or save a new instance reservation with its first assignment active.
6. Release the lock.
7. Start the uniquely named instance worker when the reservation needs a launch.

Compatibility includes the deployment, guest protocol, Nix revision, package set, architecture, and experiment membership.
A launching instance can receive its waiting assignment.
A third claim sees both assignments and creates the next instance reservation.
No AWS call or object transfer occurs under the accounting lock.
Persist launch arguments and the idempotency token before the launch call.
Keep the existing regional and instance-type fallback order.

The instance owner holds a separate kernel file lock for its whole lifetime.
Systemd restarts a dead owner with the same unit and durable reservation.
A replacement owner acquires that lock before it reconciles or sends commands.
Do not replace a live owner based only on heartbeat age.
Kill the old service group before replacing a hung owner.
The fixed guest deadline and AWS expiry function remain independent cleanup safeguards.

Each logical run keeps its existing cloud admission reservation, including while waiting.
Instance ownership is separate from the caller's lifetime.
A completed caller releases its admission reservation without terminating another caller's active run.
Do not reinterpret or increase the existing slot limits.

## Delivery and the completion race

Retain the existing S3 source bundles, requests, logs, results, and presigned URLs.
Add one instance command object that only the owner writes.
The guest receives a presigned read URL for that object at boot.
Commands contain `start` or `stop`, a sequence number, and the expected launch generation.
A `start` command contains the immutable request digest and that run's bounded object URLs.
The guest needs no administrator key, SSH connection, or broad bucket permission.
The fixed instance expiry also bounds every URL.

Second-run delivery:

1. Hold the waiting assignment before beginning its object transfers.
2. Upload its bundle and request to immutable run-specific keys while the first run executes.
3. Verify upload completion before publishing its `start` command.
4. Retain the assignment during a transfer retry until its request deadline.

After a run, the guest uploads its final log and result before accepting another `start` command.
It does not shut down unconditionally in shared mode.
It rejects duplicate, stale, or incompatible command generations.
It retains the accepted sequence outside the runner's writable directories.
It publishes a cleanup acknowledgement before the owner can promote another run.

Completion procedure:

1. Validate and save the result, final log, and cleanup acknowledgement on natedev.
2. Acquire the accounting lock.
3. Commit the run's single final result.
4. Promote the waiting assignment, or mark the instance `closing` if none exists.
5. Save the state and release the lock.
6. Publish `start` for the promoted run, or publish `stop` and request termination.

The ledger commit defines the scheduling completion boundary.
Tests can finish earlier while their result still needs delivery.
A waiting claim before that commit wins the slot; a later claim cannot reopen a closing instance.
Protocol delivery can consume time, but the owner never waits for a future run to arrive.
If a reserved request lacks its bundle, the owner waits only for that assigned transfer or its deadline.
The guest exits on `stop`, hard expiry, or an unrecoverable protocol failure.
The owner confirms termination and records its actual observation time.
It must not substitute the earlier result receipt time for termination in shared mode.

## State between runs

Use a fresh runner process and a fresh source clone for each run.
Reuse the current guest source preparation path with a run-specific root.
Do not depend on `git reset` or `git clean` to remove all prior state.
The runner receives new HOME, TMPDIR, XDG directories, runtime files, test records, and output paths.
Use private temporary directories and shared-memory mounts for each runner.
Retain only the pinned tools and verified immutable Nix store paths.
Do not reuse Git state, Python imports, writable caches, pass records, or module timing files from the previous run.

Run preparation and tests inside a dedicated systemd control group.
Use the same runner isolation for the fresh comparison arm.
Permit writes only in the run's directories and required private mounts.
Use a distinct unprivileged user for each run.
Stop that user's services during cleanup, including services outside the runner's control group.
After delivery, kill remaining descendants and remove the run directories.
Require a successful cleanup acknowledgement before promotion.
A cleanup failure closes the instance and sends its waiting run through recovery.
A valid test result remains final even if subsequent cleanup fails.
The new runner verifies its requested commit, tree, module selection, tool versions, and normal umask.
OS page caches and the kernel remain shared; report this limit instead of claiming identical hardware state.

## Failure and recovery

The logical run key survives retries.
Each execution generation has separate object keys and result validation.
Only one ledger transaction can accept the final result for a logical run.
Use that logical key for the existing buildlog import's duplicate check.
Stale results remain diagnostic evidence and cannot create another final result.

| Failure | Recovery |
| --- | --- |
| Spot interruption affects active and waiting runs | Fence the lost generation and launch one replacement instance with both unfinished runs in their original order. |
| Valid first result already committed before interruption | Keep that result and recover only the unfinished run. |
| Guest dies or fails cleanup | Close the instance and recover every unfinished assignment. |
| Owner dies during execution | Restart its unit, reconcile saved launch arguments and objects, and resume ownership without another launch. |
| Launch reply is uncertain | Reuse the saved launch token and retain the full reservation until reconciliation. |
| Result arrives twice or after a retry | Accept only the expected generation before the logical result becomes final. |
| Caller cancels a waiting run | Remove its waiting assignment under the lock and save its cancellation result. |
| Caller cancels an active run | Stop that run, clean its process group, and preserve any other waiting assignment. |
| AWS, transport, or capacity remains unavailable | Use local fallback separately for each unfinished logical run. |

Permit one replacement cloud execution per logical run in the initial shared implementation.
This fixed bound requires no new configuration key.
After that bound, require local execution or an explicit terminal infrastructure failure.
A terminal infrastructure failure is a fail result, never a fabricated test verdict.
Do not retry a normal test failure to obtain a pass.
If both assigned runs lose the instance, both must receive independent final results.
The report records every discarded attempt and its cost.

## Expiry

Keep `lifetime_minutes=20` unchanged.
The instance deadline comes from the first assignment's existing request expiry.
Never extend that deadline after promotion or another waiting claim.
Keep the guest shutdown deadline and the existing AWS `ExpiresAt` cleanup behavior.
Each run also retains its original absolute request deadline.
Limit tests by the smaller remaining run and instance budgets, including required result delivery.
Use existing request delivery limits; add no new idle or lifetime setting.
If expiry prevents promotion, treat it as an instance failure and recover the waiting run.
Do not silently extend a run deadline to conceal queue delay.
A replacement gets a new physical reservation bounded by the remaining logical deadline and the unchanged maximum lifetime.
If no cloud budget remains, use local fallback and record the expired attempt.

## Accounting and bill evidence

Create one compute reservation per physical instance, including an uncertain launch.
Reserve its full remaining lifetime once through the existing accounting lock.
A waiting assignment adds its transfer allowance, rather than another full instance reservation.
Reserve any replacement instance separately before launch.
Release compute exposure only after confirmed absence or termination.
Keep the effective daily limit at USD 35 and retain the existing monthly limit and reserve.
This design changes no existing configuration value.

The instance row records launch request, confirmed running, test intervals, result receipt, termination request, and confirmed termination.
Use guest monotonic intervals for local durations and calibrated UTC timestamps for measurements between hosts.
Record clock uncertainty and controller observation delay with each cross-host measurement.
Reject inconsistent timelines instead of converting negative durations to zero.

The existing `ChargeRead` records contain daily usage-type quantities and money, rather than resource-level lifetimes.
They can supply bill rates and reconcile account totals.
They cannot establish one instance's actual billed life by themselves.
Do not label rate-times-observed-duration estimates as actual instance bills.

Import resource-level billing usage into a private local bill record when such evidence is available.
Match resource, region, usage interval, usage quantity, and compute cost to the private instance row.
Sum billed hours times 3600 for billed life, including all applicable daily rows.
Keep billing adjustments and corrections traceable to their source records.
Never infer this duration from CPU time or the guest test interval.
If the billing source lacks resource-level usage, mark `BillAttributionPending` and block the experiment's cost conclusion.
Current aggregate bills remain the cap's spending floor during that delay.

Allocate each matched instance bill across its assigned runs with a fixed wall-time rule.
For each billing interval, give run i its exclusive preparation, execution, cleanup, and delivery duration as weight d_i.
Split the remaining observed instance duration F equally among the n assigned runs.
The run's share is `C * (d_i + F/n) / (sum(d_i) + F)` for interval cost C.
Use equal shares when every duration is zero.
Apply this rule separately to every failed and replacement instance, then sum shares by logical run.
Weights allocate money; they do not replace the bill's duration or amount.
Price the measured startup and termination phases from matched bill rows by timestamp overlap.
Report their allocated USD amounts separately from their measured seconds.
Assign the final rounding remainder deterministically so all shares sum exactly to the physical bill.
Waiting has its own latency field and creates no second compute charge.
Keep storage, network, object, and query charges separate from compute.
Label any allocation of aggregate non-compute charges explicitly.
Count physical launches, rather than logical runs, when allocating existing per-launch bill costs.

## Measurement record and report

Store `measurement.json` under each existing logical run folder, with execution-generation entries.
The ledger remains authoritative for assignment state, accepted results, reservations, and bill references.
Recreate a missing measurement file from those durable entries after a crash.

| Field group | Required contents |
| --- | --- |
| Identity | Logical run, trial pair, commit, tree, mode, selected modules, protocol, execution generation |
| Placement | Local instance alias, region, type, `first` or `reused`, assignment order |
| Result | Test pass/fail, failed modules, infrastructure outcome, final result acceptance |
| Time | Request, slot claim, active preparation, actual first test, last test, result receipt, cleanup, termination |
| Delay | Admission delay, assigned waiting duration, request-to-first-test duration, retry delay, clock uncertainty |
| Fixed cost | Startup and termination intervals per physical instance, with allocated USD amounts |
| Handoff | Inter-run cleanup, source preparation, control delivery, and result transfer intervals |
| Bill | Evidence state, source reference, billed instance seconds, physical cost, allocated run share, corrections |
| Recovery | Interruption reason code, displaced assignments, replacement generation, local fallback, discarded results |

Keep preparation-to-first-test separate from waiting for an existing instance.
Sum waiting durations across retries without counting the same interval twice.
Retain failed attempts when computing total request latency and total run cost.
Use local aliases to identify shared instances in output.
Print no cloud resource identifiers, account identifiers, addresses, or signed URLs.

Proposed commands extend `gate_trial` with one subcommand:

```sh
python3 -m scripts.aws_target.gate_trial shared --repo <path> --commit <ref> --pairs 8
python3 -m scripts.aws_target.gate_trial shared --report <trial-folder>
```

The report command reads saved records and imports no live AWS state.
Its optional `--bill-file <path>` imports a private local resource-billing file for matching and allocation.
It prints per-run rows, pair verdict differences, latency distributions, bill status, physical totals, and allocated shares.
It separates interrupted pairs from normal pairs and pending bill evidence from settled costs.
Normal buildlog output also counts logical runs and physical launches separately.

## Paired experiment and spending bound

Use eight matched pairs, with one primer run before each reused run.
Each pair therefore has a primer, a reused run, and a fresh control run.
Submit the reused request and its fresh control at the same comparison point while the primer remains active.
Pin the same commit, tree, module selection, tool revision, type, region, and runner isolation for the matched runs.
Match actual placement; a capacity fallback to another type or region makes the pair unmatched.
Keep unmatched results and costs visible.
Do not publish merges or pass certificates from the experimental comparison arms.

Use six normal pairs and two recovery pairs.
Interrupt one shared instance while its first run is active and its second run is waiting.
Use the other recovery pair for loss during handoff before the second run starts.
The recovery checks require final results for both logical runs and exactly one accepted result per run.
Inject these failures only during an explicitly approved live experiment.
Unit tests simulate them without AWS.

The paired check compares reused and fresh test verdicts and failed-module sets.
Two matching test failures are equivalent results; an infrastructure failure does not establish equivalence.
Report every mismatch and stop automatic adoption until its cause is known.
Report request latency and assigned waiting separately from test duration.
Wait for the required bill records before stating a measured cost saving.

| Pilot budget basis | Reservation |
| --- | ---: |
| Eight pairs with primers | 24 logical runs; 16 planned instances |
| One replacement execution per logical run | At most 32 physical instances for this controlled pilot |
| Current bid ceiling and maximum lifetime | USD 0.8/hour and 20 minutes |
| Current non-compute allowance | USD 0.02 per run execution; at most 48 executions including retries |
| Conservative reservation bound | `32 * 0.8 * 20/60 + 48 * 0.02` = USD 9.494, rounded upward |
| Pilot envelope, including billing-query headroom | USD 10 within the USD 35 daily limit |

The allowance is a reservation estimate.
The ledger must use larger established exposure if current bill evidence requires it.
Start the pilot only when existing spend, open reservations, the USD 10 envelope, and the existing reserve fit below USD 35.
Treat the envelope as a subset of account exposure, not another charge added to individual reservations.
Check the account cap and pilot envelope before every launch or retry.
Stop new pilot launches when either bound fails, and finish cleanup and local result recovery.
Production activity can consume the remaining headroom; the pilot does not raise the cap or exclude that spend.

## Configuration

| New key in `cloud_gate.conf` | Committed value | Reason |
| --- | --- | --- |
| `shared_instances` | `off` | Enable the shared placement and owner path only when set to `on`. |

This is the only new configuration key.
Mode `off` retains today's launch, guest shutdown, ledger, and fallback paths.
Mode changes affect new assignments only.
An owner finishes existing assignments before closing its instance.
The shared experiment requires mode `on`; its fresh arm uses internal fresh-only placement.
The pilot size and USD 10 envelope are fixed experiment rules, not new production settings.

## Implementation scope and tests

These are estimated changed lines, including relevant tests.
They are planning ranges, rather than implementation commitments.

| File | Change and estimated size |
| --- | --- |
| `scripts/aws_target/shared_gate.py` (new) | Instance owner, handoff, reconciliation, result acceptance; 300–450 lines |
| `scripts/aws_target/cloud_gate.py` | Shared-mode dispatch, caller attachment, private launch reuse; 120–180 lines |
| `scripts/aws_target/gate_box.py` | Repeated isolated runners, command validation, conditional shutdown; 150–220 lines |
| `scripts/aws_target/ledger.py` | Atomic assignments, physical reservations, bill references and allocation; 220–320 lines |
| `scripts/aws_target/measurement.py` | Named evidence states and private resource-bill import; 80–130 lines |
| `scripts/aws_target/gate_trial.py` | Matched shared experiment and report subcommand; 140–220 lines |
| `scripts/production/merge_gate.py` | Actual first-test event callback; 20–40 lines |
| `scripts/buildlog/aws_report.py` | Distinct run and launch totals; shared physical cost allocation; 50–90 lines |
| `scripts/aws_target/test_shared_gate.py` (new) | Owner, concurrent claims, close races and recovery; 250–400 lines |
| Existing AWS, guest, ledger, trial, and buildlog tests | Isolation, bill allocation and compatibility rows; 220–350 lines |
| `config/cloud_gate.conf`, `config/README.md` | One mode key and its reason; under 15 lines |
| AWS as-built and runbook documents | Operation, limits and measurement caveats; 40–70 lines |

Reuse the existing launch expiry function and template permissions where their object prefix rules permit the command object.
Check that prefix coverage during implementation; propose a permission change separately if needed.
Add no instance lease extension or new cloud service.

| Test row | Required result |
| --- | --- |
| Mode off | Existing path and result behavior remain unchanged. |
| Five concurrent claims | Exactly three instance reservations with assignment counts 2, 2, 1. |
| Competing waiting claims | Exactly one caller holds each waiting slot. |
| Claim races completion | A saved waiter promotes, or a closed instance rejects the claim; no orphan assignment. |
| No waiter at completion | Termination begins without an idle timer. |
| Caller exits after first result | The waiting run and owner survive. |
| Slow second upload | The reserved transfer completes or fails by deadline; no unassigned idle hold. |
| Owner crashes at each state write | Restart neither duplicates a launch nor loses a run. |
| Dirty first run and escaped descendants | The second runner has fresh files, environment, processes and source. |
| Stale command, result, or launch generation | The guest and owner reject it. |
| Interruption with two assigned runs | Both unfinished runs retry and receive one final result each. |
| Result committed before interruption | The completed result remains final. |
| Expiry before promotion | The waiter receives recovery without an expiry extension. |
| Daily cap or pilot envelope reached | No additional launch occurs; cleanup and result recovery continue. |
| Shared and retry costs | Each physical interval appears once; run shares sum to its bill. |
| Midnight, minimum charge, and bill correction | The bill remains authoritative and allocation remains exact. |
| Aggregate bill without resource usage | The report shows pending attribution and makes no actual-cost claim. |
| Same commit, fresh and reused | The report preserves pass/fail differences, placement mismatches and infrastructure errors. |

Run the AWS suite, affected buildlog and runner tests, the four existing guard suites, and basedpyright after implementation.
Use simulated clocks and fake transports for concurrency, interruption, and expiry tests.
Do not use a live AWS instance for those checks.

## Risks and decisions

| Risk or question | Decision owner |
| --- | --- |
| Waiting costs more latency than fresh startup for small monetary savings. | User: review paired latency and settled cost before enabling production sharing. |
| Existing bills lack resource-level instance usage. | User: approve access to suitable billing records or a billing export before the cost experiment can complete. |
| The live pilot needs up to USD 10 of available daily headroom. | User: authorize the pilot without changing the USD 35 cap. |
| Deliberate interruption affects assigned work. | User: authorize interruption of experiment instances only. |
| Kernel and page-cache state can change reused-run results or timing. | Implementation: isolate mutable state and stop adoption on unexplained pair differences. |
| An owner failure delays handoff or termination. | Implementation: restart the owner and retain the independent hard expiry. |
| Billing publication delays the final comparison. | Implementation: retain pending evidence and issue no saving claim before attribution. |
| A long first run leaves insufficient time for its waiter. | Implementation: preserve fixed expiry and record recovery rather than extending the box life. |

No user decision is necessary to write this design.
Implementation and live operation remain separate steps.
