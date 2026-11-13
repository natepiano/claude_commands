Status: design only; requires agreement from AWS, resources, queue, verify, report, and natedev owners.

# One kept AWS machine for Hana

Use one Ohio `m7a.2xlarge` Spot instance with 8 vCPUs, 32 GiB RAM, and retained build storage.
Route only Hana clippy judgments and check to AWS.
Keep mend, build, and clippy repairs on natedev.
Keep nextest on natedev with its GPU and local test binaries.
The expected daily cost is about USD 2.89–3.26, subject to the trial.

## Placement and lifecycle

Require queue Phase 44 before Hana Phases E, F1, and G.
Require queue Phase 44c before activation.
Extend Phase 44 placement from `PlacementRequest` constraints through `PlacementAssignment`, assigned-info, and `ExecutionReport`.
Add one provider candidate within that path; create no selector or separate placement path.
Reuse build holds, exact-source ownership, the Linux runner, and buildlog result contracts.
Use the existing gate S3 transport instead of adding SSH credentials or an instance IAM role.
Read the candidate target row only through `target_config.read_targets`.
Put the target name `hana-aws` in configuration, not in code.
Disable the candidate when `config/hana_aws.conf` has `enabled=off`.
Extend the existing `build_targets` capability path; add no second placement rule.
Register the box as `kind=ssh` and `platform=linux`.
Put Hana capability keys in its settings file.
The AWS provider owns box start and stop.
An enabled Hana route tries this candidate before natedev.
Preserve explicit target pins and all other routing tests unchanged.
Match Hana by its existing repository identity, not a directory name.
`placement.BusyProviderDiagnostic` and `placement.ProviderRetryNotice` land with queue Phase 44.
Use those notices for busy, starting, and held states.

| State | Queue action |
| --- | --- |
| No provisioned machine | Use natedev; provision only through a separate approved setup command. |
| Machine stopped after idle, capacity and spend allowed | Hold one start claim and start immediately. |
| Machine stopped by interruption | Use natedev after any prior attempt settles, until AWS restores capacity. |
| Starting machine | Bound the claimant wait at 120 seconds; keep pending starts blocked until settlement. |
| Ready and idle | Claim the machine and a `slots.aws.hana` slot, then execute remotely. |
| Busy, held, unhealthy, or start deadline exceeded | Use natedev only after any remote attempt settles. |
| Interrupted, refused, or remote result lost | Settle the remote attempt before the normal natedev path uses the live worktree. |

Enforce the Phase 44 settlement barrier before local fallback.
A settled `PrelaunchRefused` or `ExecutionNotStarted` closes the attempt immediately.
Keep that provider excluded until cleanup completes.
A pending or unacknowledged start remains blocked until settlement.
After loss or watchdog termination, let offload record the attempt lost and fence its generation before settlement permits fallback.
Do not transfer the remote snapshot to local fallback.
Keep `scripts/direct/verify.sh` unchanged.

Begin with one active step, limited to 4 vCPUs and 24 GiB RAM.
The supplied steps use 0.7–2.1 cores; their median peak memory reaches 9.1 GiB.
Run only one command on each leased slot (D2, D5).
Release local memory admission and the cargo token through the existing Mac offload release path.
Retain the caller's hold and ownership records.
Define the local remote-step waiter class in `build_hold.audit_builds` and `hold_enforcer.decide_builds`.
A natedev drain hold leaves this waiter running because remote work uses no natedev memory.
A normal compiler failure returns that failure instead of silently running again elsewhere.

Use a persistent Spot request with interruption behavior `stop`, rather than the gates' termination behavior.
Stop after 600 seconds without an active or committed incoming command.
This interval retains short bursts without paying for a quiet evening.
Serialize idle-stop and new claims under the lifecycle lock selected in Phase D.
AWS alone can restart an instance stopped by interruption; capacity recovery has no promised deadline.
An automatic restart without a fresh work permit stops again within 60 seconds.
Implement the stop guard as a systemd unit without dependencies on the data disk, network, or tool setup.
Order tool setup `After=` the guard.
A failed tool setup must still let the guard stop the machine.
Phase D selects a replacement for the removed `RemoteAgent.tick`.
Add an independent guest watchdog beside that lifecycle service and the [Linux runner](../../scripts/mac_test/linux_runner.py).
An idle-stop check cannot stop a command that never exits.
Run the watchdog independently of the controller and the command.
At execution permit expiry, kill the command's cgroup and stop the machine, even when the controller is absent.
Install this watchdog before accepting commands.
Refresh the bootstrap read URL before a demand start.
Keep the restart service separate from the disposable gates' 20-minute expiry path.
These constraints follow the [AWS stop rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/interruption-behavior.html).

## Exact source and warm storage

Reuse `copies.py` source capture through `staged_tree`, `source_commit`, `apply_changes`, and the existing `SourcePolicy`.
Keep the caller's real index unchanged; create no second snapshot path.
Reuse the Mac/Linux runner's input identity checks.
Follow the [build folders contract](build-folders.md), D1–D9.
Allocate repository folders for source capture and guest materialization through bf1 `create_build_folder()` (D1).
Inherit its process lock into the working process and its children (D5).
Keep the lock beside the slot, outside reset and deletion boundaries.
Use the Phase 3 `slots.aws.hana` pool for all guest repository work (D2).
The Hana box has no separate cache scheme or cache lifetime.
Different Hana worktrees share this fixed pool with task affinity and stable source and Cargo target paths.
Prefer the task's previous free slot, then another free slot (D2).
Use existing admission behavior when the pool is full; create no overflow slot.
Publish showrunner, unit, purpose, and machine through the Phase 3 queue fields (D3).
Require Phase 2 rescue before source reset, target removal, or repository deletion (D6).
Preserve commits, refs, index changes, working files, untracked and ignored source, conflicts, and nested repository work in rescue.
Publish rescue objects under a unique `refs/rescue/<name>` in the source repository.
Verify the rescue ref and objects; retain the source if rescue fails.
Use the maker for repository allocation; permit no separate clone or worktree creation path (D7).
Deliver the AWS slot integration with build folders Phase 4.
Use a separate S3 module behind one typed transport boundary for digest upload, manifests, and materialization.
Implement the S3 module in `scripts/aws_target/hana_transport.py`.
Keep `copies.py` free of AWS imports and the sole source of truth for source definitions.

1. Capture the exact tracked and permitted untracked files through the existing copies source path.
2. Include staged and unstaged changes without changing the caller's real index.
3. Upload missing content by digest and send a manifest with the tree, command, features, and allowed environment.
4. Acquire a pool slot and rescue its prior source before reset and manifest materialization.
5. Verify hashes, deletions, permissions, symlinks, and the resulting source identity before execution.

Reuse the initial 318 MB source snapshot and send changed content on later requests.
Never copy ignored files, credentials, the caller's HOME, or its `target/` directory.
Refuse remote execution if an excluded input affects the build.
Keep the source and Cargo target paths stable for each slot (D2).
Reset source at acquisition and retain the Cargo target unless D4 requires its removal.
Retain toolchain, feature, profile, and shell identities with each slot.
Use the existing Linux shell cache and invalidate incompatible tool state.

Keep a 40 GiB root volume and a 300 GiB encrypted gp3 data volume in the same zone.
Retain the data volume through stop and instance replacement.
Mount the data disk with `nofail` and a device timeout.
A missing disk refuses work without delaying boot indefinitely.
Keep Rust tools and Cargo caches on the data volume.
Retain the 30 GiB free floor as a capacity constraint.
Measure the complete slot size at acquisition against `slots.aws.hana.max_bytes` (D4).
After rescue, remove an oversized slot's Cargo target once, then run one cold build.
Report source-only excess without a repeated deletion loop.
Use no separate age, worktree lifetime, or free-space eviction rule.
The build folders Phase 8 disk sweep raises alarms without deleting slots (D8).
The shared stall watch stops hung owners; only lock release proves that a slot is free (D5, D8).
Do not reset or delete a slot while another process holds its lock.
Serialize acquisition, rescue, and source reset under the acquiring process's own lock (D5, D6).
A replacement must fence the old owner before attaching the single-writer volume.
Do not create trial snapshots; measure cold rebuilds after cache loss.

## Tools and returned results

Use the Hana toolchain source that natedev's NixOS configuration uses, including clippy and the pinned Nix shell.
Add no second Rust version pin in `aws-build.nix`.

Validate `rustc -vV` against the requested compiler identity.
Disable the target when the requested toolchain or shell cannot be reproduced.

Return stdout, stderr, exit status, timings, peak memory, source identity, and a buildlog row for the remote attempt.
Keep interrupted attempts linked to the caller; accept only one final result per execution generation.
Reuse diagnostics path mapping and result synchronization.
Use the same captured source identity as the `verify.sh` verify cache key.
Refuse a remote result when its source identity differs.
Reject results when the command no longer matches.
Reject late results from fenced generations before any buildlog row or verify cache entry is written.
Record the remote attempt and its natedev fallback as two step rows with one shared link key.
Count the step once by its final result.
Show lost remote time separately from natedev step totals.
Give each remote row its own host.
Keep natedev step tables, memory pressure, and Rebuilds limited to natedev rows.
Bump `SCHEMA_VERSION` for new buildlog columns.
Read missing columns from older records as `NULL`; preserve every existing column's meaning.

Preserve the existing warning policy for clippy judgments.
Return no patch or binary.
Preserve the existing lint sequence and recapture source after local repairs before another remote judgment.
The queue and verify owners must confirm that the provider integration preserves them.

## Cost and configuration

The eligible workload on 2026-11-11 was 170 steps: 65 clippy judgments and 105 check runs.
The other 30 clippy runs used `--fix`; they are repairs and stay on natedev.
A replay of that day's arrival times against one machine gives these estimates:
- 12–19% of eligible steps arrive while the machine is busy and run on natedev.
- Steps spread over the day, so the 600-second idle tails add up to 15.9–16.3 running hours and 34–37 starts.
The replay assumes one to 1.5 times the natedev step time and a 60-second start.
The trial must measure actual running hours, starts and the busy share.
The full-day case covers longer idle periods, slower execution, and failed work.

| Daily component | Estimate, USD |
| --- | ---: |
| Compute, 16 hours at USD 0.11–0.13/hour | 1.75–2.12 |
| 340 GiB gp3 at USD 0.08/GiB-month, using 30 days | 0.907 |
| Public IPv4, 16 hours at USD 0.005/hour | 0.08 |
| Diagnostic transfer, object calls, and query calls, allowance | 0.15 |
| Total | About 2.89–3.26 |
| Full day at the proposed USD 0.15/hour bid ceiling, including USD 0.12 IPv4 | About 4.78 total |

Prices are planning inputs from the supplied cost study, not guaranteed paid rates.
Avoid NAT gateways, reserved addresses, extra gp3 performance, cross-region transfers, and another compiler-cache service.
Disk remains chargeable while stopped; source transfer, failed starts, warmup, and interrupted work remain visible.
Use [EBS billing](https://aws.amazon.com/ebs/pricing/) and account bill records in the report.

Extend the existing ledger with kept-machine running intervals, persistent storage, transfer, queries, and worktree-linked execution attempts.
Reserve remaining exposure before starts and renew work permits only while spending remains allowed.
Include automatic Spot restarts in exposure and reconcile them even without an active caller.
Reserve compute exposure through the permit deadline and retain that reservation until the machine's stop is confirmed.
Keep storage reserved after the machine stops.
Keep the effective account daily limit at USD 35, including gates and every other AWS workload.
Include the Hana budget within that limit.
Read kept-machine running time, disk cost, and transfer cost from the AWS ledger in `aws_report.py`.
Use bills when available and label estimates.
Compute no cost in `record.py`.
Add a kept-machine compute column beside 32-CPU and 16-CPU in the daily spend table.

Add the workload keys below in a new `config/hana_aws.conf`.
Add only the new target row keys shown below in `config/build_targets.conf`.
The new workload and target row keys need no user approval while inactive.
The slot capacity values below require the user's go before activation.
Keep the row outside the active `targets=` list, as the existing Manual Linux proof configuration does.
Change no existing configuration value during implementation.
At enable time, add `hana-aws` to `targets=` only through the user's own `/tune` approval.
Obtain that approval after the user approves the USD 6 first-day trial bound.
Read these workload keys through `scripts/lib/conf.py`; retain `target_config` as the sole target-row reader.
Keep `start_timeout_seconds=120`, `idle_stop_seconds=600`, and `max_steps=1` as configuration keys, as listed below.

| New target row key in `config/build_targets.conf` | Value |
| --- | --- |
| `kind.hana-aws` | `ssh` |
| `platform.hana-aws` | `linux` |
| `settings.hana-aws` | `hana_aws.conf` |

| New key | Value | Reason |
| --- | --- | --- |
| `enabled` | `off` | Require an explicit decision before routing Hana work. |
| `region` | `us-east-2` | Use the supplied Ohio prices and one storage zone. |
| `instance_type` | `m7a.2xlarge` | Provide 32 GiB RAM at the smaller supplied price. |
| `spot_max_usd_per_hour` | `0.15` | Bound compute exposure; refuse higher prices. |
| `idle_stop_seconds` | `600` | Retain short bursts and stop through quiet periods. |
| `start_timeout_seconds` | `120` | Bound a caller's startup wait. |
| `max_steps` | `1` | Establish memory and slot behavior before concurrent execution. |
| `step_cpu_limit` | `4` | Allow the observed work without occupying all host CPUs. |
| `step_memory_gib` | `24` | Leave 8 GiB for the operating system and control processes. |
| `root_gib` | `40` | Retain the system and Nix store without a smaller untested root. |
| `data_gib` | `300` | Retain the shared slot pool, Rust tools, and Cargo caches. |
| `free_floor_gib` | `30` | Prevent disk exhaustion during compilation. |
| `daily_budget_usd` | `6` | Bound this workload within the unchanged USD 35 account limit. |

Keep the step allowlist fixed to Hana clippy judgments and check.
Reject tree-writing flags such as `--fix` before remote execution.
Derive the compiler revision from the requested source instead of adding a duplicate version setting.

### Values for the user's go

1. Set `slots.aws.hana.count=1` only after the user's go.
2. Set `slots.aws.hana.max_bytes=137438953472` (128 GiB) only after the user's go.

These values live in `config/build_folders.conf`, not code.
Missing approved capacity values stop admission with a named configuration error.
The sizing basis is the supplied Mac target measurements: 20.8, 85.8, and 110 GiB.
Targets alone do not measure complete slots.
The proposal also considers the 300 GiB data disk and the 30 GiB free floor.
No AWS slot measurement exists yet.
Phase H measures complete AWS slot size and cold-build behavior before any later capacity proposal.

## Security, ownership, and implementation

Keep the same secret boundary as disposable gates.
Only natedev holds AWS credentials; manual setup uses the encrypted admin session.
Deliver the permit and short-lived, object-scoped signed URLs at run time through the authenticated transport.
Store no secrets in the Nix store or bootstrap template.
Do not install repository tokens, SSH keys, Tailscale credentials, or a broad instance role.
Refresh URLs through the trusted controller and reject stale boot or execution generations.
Run Cargo and build scripts unprivileged, outside the root control service.
Keep controller files inaccessible to the runner and reject archive paths outside the leased source root.
Hold, cancellation, interruption, and cap refusal stop the process group before local fallback.
Keep uncertain cancellation blocked until the attempt settles under the Phase 44 barrier.
Print safe aliases and error codes, without cloud identifiers, addresses, secrets, or signed URLs.

1. Send queue the `build_queue` and copies diffs before checkpoint and before merge.
2. Send verify the `offload.py` and `linux_runner.py` diffs before checkpoint.
3. Send report the `record.py` and `aws_report.py` diffs before checkpoint.

Send the `/etc/nixos` change to natedev as a branch on the `nixos` origin with its file list.
Natedev reviews, merges, and pushes that branch.
Start no build or spend before all owners agree and the user approves the USD 6 first-day bound.
Keep existing `scripts/mac_test`, `test_verify_mac*`, and `ssh_targets` tests unchanged; add new rows beside them.
Place new copies tests beside `scripts/copies/test_copies.py` tests.
Create independent test repositories through maker slots with private configuration (D1, D2, D7).
Use no live units.
Remove review row folders after their verdict and verified rescue, as D9 requires.
Coordinate the pending `ConnectTimeout` removal in `offload.py` and `mac_test.py` before editing those files.

| Files or area | Estimated change | Owner and required agreement |
| --- | --- | --- |
| `scripts/aws_target/hana_box.py` (new) | 250–350 lines for lifecycle, gate transport, execution and results | AWS |
| `scripts/aws_target/{deploy,ledger}.py`, `docs/plans/builds-aws/cloudformation.json` | 180–280 lines for setup, the kept workload, stop recovery and charges | AWS |
| `/etc/nixos/hosts/aws-build.nix`, `/etc/nixos/hosts/aws-bootstrap.sh.in` | 40–70 lines for shared toolchain inputs, guard and disk mount | Natedev: required go |
| `scripts/build_targets/{target_config,build_targets}.py` | 50–90 lines for the configured target row and existing capability path | Resources: required go |
| `scripts/build_hold/{build_hold,hold_enforcer}.py` | 20–40 lines for the remote waiter class and drain hold | Resources: required go |
| `scripts/build_queue` | 50–90 lines within Phase 44 placement and settlement | Queue: required go |
| `scripts/copies/{copies,test_copies}.py` | 30–50 implementation lines for existing source capture and shared slot integration; tests below | Queue: GO, under the conditions below |
| `scripts/lib/build_folders.py`, `config/build_folders.conf` | Shared maker, process locks, rescue, pool limits, and affinity; no Hana cache allocator | Queue owns; devtools coordinates build folders phases |
| `scripts/aws_target/hana_transport.py` (new) | 80–120 lines for S3 behind the typed transport boundary | AWS; queue reviews the copies boundary |
| `scripts/mac_test/{offload,linux_runner}.py` | 70–120 lines for source identity, admission release, fencing and cgroups | Verify: required go |
| `scripts/buildlog/{record,aws_report}.py` | 80–120 lines for linked rows, schema and ledger reporting | Report: required go |
| `config/hana_aws.conf`, new row keys in `config/build_targets.conf`, AWS operation documents | Under 70 lines; later `targets=` edit requires user `/tune` | AWS; resources reviews target registration |
| Tests beside these modules | 220–350 lines | Each code owner |

### Implementation phases

Follow this phase order.
Queue dependencies gate the named phases, rather than unrelated guest safety and spending work.

| Phase | Deliverable | Dependencies |
| --- | --- | --- |
| 0 | Align this design with D1–D9 and record proposed capacity values. | Build folders contract. |
| A | Guest safety: independent guard, watchdog, cgroups, and bounded disk mount. | 0. |
| C | Spending permits: reserve exposure, renew permits, and confirm stops. | A. |
| D | Provisioning and lifecycle: setup command, start, stop, replacement, and recovery. | A, C. |
| E | Provider placement through the configured SSH target and existing capabilities. | D; queue 44; queue 44c before activation. |
| F1 | Offload fencing, settlement, and local admission release. | E; queue 44; queue 44c before activation. |
| G | Results and costs: linked rows, source checks, schema, and ledger reporting. | C, F1; queue 44; queue 44c before activation. |
| B | Exact source into shared slots through the maker and S3 transport. | Build folders 3; deliver with build folders 4. |
| F2 | Execution on the leased slot with inherited process locks. | B, D, F1; build folders 4. |
| H | Acceptance: equivalent results, complete slot sizes, safety, latency, and first-day costs. | G, F2; build folders 4; owner agreements and the user's go. |

Phase D includes two decisions before implementation:

1. Select a lifecycle service to replace the removed `RemoteAgent.tick`.
2. Define a new `deploy.py` Hana setup command that the user runs.

The lifecycle service owns start and stop serialization through the AWS provider.
The setup command provisions the workload separately from placement and ordinary gate deployment.

### Owner conditions

| Owner | Condition | Where the design meets it |
| --- | --- | --- |
| Resources | One target reader; new inactive row keys; later `targets=` change through user `/tune`; existing capabilities; drain-safe waiter | Placement; configuration; files; drain test |
| Queue | Queue 44 before E, F1, G; 44c before activation; settled fallback and configured limits | Placement; implementation phases; settlement test |
| Queue (notices) | `placement.BusyProviderDiagnostic` and `placement.ProviderRetryNotice` land with queue 44. | Placement; queue dependency |
| Verify | Shared source identity, one release path, fenced results, unchanged tests | Placement; results; delivery rules; fencing test |
| Report | Linked rows, host separation, ledger costs, compatible schema | Results; cost; report tests |
| Natedev | Shared toolchain, independent guard, bounded mount, runtime secrets, branch review | Lifecycle; storage; tools; security; delivery rules |
| Queue (copies): GO | Reuse source functions and `SourcePolicy`; preserve the real index | Exact source; capture test |
| Queue (copies): GO | Use the shared maker and `slots.aws.hana`; rescue before reset and inherit process locks (D1–D7). | Exact source and warm storage; slot tests |
| Queue (copies): GO | Separate typed S3 boundary; no AWS imports in `copies.py` | Exact source; transport file |
| Queue (copies): GO | Copies tests use private maker slots and no live units. | Implementation; copies tests |
| Queue (copies): GO | Review the copies diff before checkpoint and merge | Delivery step 1 |

| Required test rows | Expected behavior |
| --- | --- |
| Wrong repository, mend, build, nextest, local pin, disabled mode | Preserve existing placement. |
| Clippy repair flags and local repair followed by judgment | Keep repairs local and capture the updated source for judgment. |
| Simultaneous requests, idle-stop race, busy machine | Start one instance and admit one step; use existing provider notices. |
| Natedev drain hold during remote execution | Keep the classified local waiter running without local memory admission or a cargo token. |
| Pending start, unacknowledged start, settled refusal, cleanup | Enforce the Phase 44 barrier and provider exclusion until cleanup. |
| Two dirty worktrees and deleted or untracked files | Preserve exact source identities through existing source functions and `SourcePolicy`. Share the pool with task affinity (D2). |
| Staged source capture and change application | Preserve the real index in independent private maker slots without live units (D1, D7). |
| Slot acquisition, full pool, and process death | Preserve task affinity and target paths. Use existing admission. Inherit locks and observe release (D2, D3, D5). |
| Oversized complete slot | Rescue, remove the target once, and run one cold build; report source-only excess (D4, D6). |
| Source reset or deletion; failed rescue | Verify rescue first; retain source on failure; protect held slots from collectors (D5, D6). |
| Edited source during execution or result transfer | Reject stale remote judgments. |
| Remote refusal or loss after a local edit | Settle and fence the attempt before the normal natedev path uses the live worktree. |
| Compiler mismatch or incompatible shell | Refuse remote execution before Cargo starts. |
| Failed tool setup or missing data disk | Stop through the independent guard; refuse work without hanging boot. |
| Interruption, automatic restart, lost controller, cancellation | Fence stale work, stop safely, and return one final result. |
| Controller loss during a command that never exits | At permit expiry, kill the command cgroup and stop without the controller; retain exposure until stop confirmation. |
| Memory, disk, workload budget, or account cap reached | Record the reason and use the permitted local path. |
| Late result after fencing | Write no buildlog row or verify cache entry. |
| Linked remote loss and natedev fallback | Count one final step; separate remote time, host, memory, and Rebuilds. |
| Older buildlog records and new ledger columns | Read missing fields as NULL; preserve old meanings and label estimated costs. |
| Stop intervals and delayed or revised bills | Charge physical compute once and retain storage charges. |

### Later acceptance

Activation does not wait for these checks.

1. After build folders Phase 6, verify review-verdict cleanup after a verified rescue (D9).
2. After build folders Phase 8, verify alarm-only disk sweeping without slot deletion (D8).
3. After build folders Phase 8, verify that the shared stall watch stops hung owners and observes lock release (D5, D8).

## First-day trial and decisions

1. Obtain all owner agreements and the user's go for slot capacity and the USD 6 first-day bound.
2. Confirm USD 6 of headroom below the account cap, including existing exposure and reserves.
3. Provision one instance and record all setup time and cost inside the first-day budget.
4. Measure cold, warm, and one-edit runs for clippy and check on identical snapshots on AWS and natedev.
5. Confirm equivalent exit status and diagnostics before routing live requests.
6. Enable only clippy judgments and check for one day with the stated local fallback.
7. Measure complete AWS slot size against the proposed 128 GiB limit in Phase H.
8. Compare queue wait, end-to-end latency, execution time, memory, source bytes, slot reuse, interruptions, and local nextest time.
9. Compare natedev queue wait with the supplied 34–83-minute baseline and matched before/after activity windows.
10. Report the full day's compute, storage, transfer, setup and failed-attempt costs from the bill when available.
11. Disable new remote work at USD 6 projected exposure or on incorrect results, and stop after active cleanup.

The USD 6 bound includes a retained disk reservation and remaining compute exposure, rather than only completed step costs.
It is a subset of the USD 35 account limit.
A separate storage decision remains necessary after the trial, because stopped volumes continue to cost about USD 0.91/day.

| Open question or risk | Decision owner |
| --- | --- |
| Cold slots and D4 target removal can exceed the execution forecast. | Phase H: measure before promising lower queue wait. |
| Spot interruption can prevent restarting in the fixed storage zone. | User: accept local fallback; no automatic larger or On-Demand replacement. |
| Median memory does not prove every Hana step fits. | Trial: measure peaks and keep the one-step limit. |
| A second active step could improve throughput. | Later proposal after memory and queue measurements; do not enable it in this trial. |
| The old AWS build target was removed. | User: approve this separate CPU-only workload and its USD 6 first-day bound. |
| Storage remains after the machine stops. | User: retain the warm disk or remove it after the trial. |

## Later, if the trial earns it

Mend requires a patch contract with exact-source validation before local application.
Build requires selected binary downloads, verified hashes, compatible runtime files, and real local paths.
Measure compressed outputs and agree a transfer allowance before either route expands.
These changes require a separate design and the verify owner's agreement.
