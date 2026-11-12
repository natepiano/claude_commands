Status: design only; requires agreement from AWS, NixOS, Linux runner, queue, copies, remote-runner, and buildlog owners.

# One kept AWS machine for Hana

Use one Ohio `m7a.2xlarge` Spot instance with 8 vCPUs, 32 GiB RAM, and retained build storage.
Route only Hana clippy judgments and check to AWS.
Keep mend, build, and clippy repairs on natedev.
Keep nextest on natedev with its GPU and local test binaries.
The expected daily cost is about USD 2.00–2.81, subject to the trial.

## Placement and lifecycle

Reuse `PlacementAssignment`, build holds, exact-source ownership, the Linux runner, and buildlog result contracts.
Use the existing gate S3 transport instead of adding SSH credentials or an instance IAM role.
Add the target `hana-aws` as an optional Hana placement candidate, rather than changing the global `targets` list.
An enabled Hana route tries this candidate before natedev.
Preserve explicit target pins and all other routing.
Match Hana by its existing repository identity, not a directory name.

| State | Queue action |
| --- | --- |
| No provisioned machine | Use natedev; provision only through a separate approved setup command. |
| Machine stopped after idle, capacity and spend allowed | Hold one start claim and start immediately. |
| Machine stopped by interruption | Use natedev until AWS restores capacity. |
| Starting machine | Let that claimant wait at most 120 seconds; send other claims to natedev. |
| Ready and idle | Claim the machine and the source worktree, then execute remotely. |
| Busy, held, unhealthy, or start deadline exceeded | Use the normal natedev queue without another remote wait. |
| Interrupted, refused, or remote result lost | Reconcile or cancel any remote attempt, then use the normal natedev path on the live worktree. |

Do not transfer the remote snapshot to local fallback.
Keep `scripts/direct/verify.sh` unchanged.

Begin with one active step, limited to 4 vCPUs and 24 GiB RAM.
The supplied steps use 0.7–2.1 cores; their median peak memory reaches 9.1 GiB.
Do not run two commands against the same worktree cache.
Release local compute admission while AWS executes; retain the caller's hold and ownership records.
A normal compiler failure returns that failure instead of silently running again elsewhere.

Use a persistent Spot request with interruption behavior `stop`, rather than the gates' termination behavior.
Stop after 600 seconds without an active or committed incoming command.
This interval retains short bursts without paying for a quiet evening.
Serialize idle-stop and new claims under the controller's existing lifecycle lock.
AWS alone can restart an instance stopped by interruption; capacity recovery has no promised deadline.
An automatic restart without a fresh work permit stops again within 60 seconds.
Install the stop guard before tool setup, so failed preparation cannot leave an unattended running machine.
Add a guest watchdog beside [RemoteAgent.tick()](../../scripts/aws_target/remote.py) and the [Linux runner](../../scripts/mac_test/linux_runner.py).
The existing idle-stop check cannot stop a command that never exits.
Run the watchdog independently of the controller and the command.
At execution permit expiry, kill the command's cgroup and stop the machine, even when the controller is absent.
Install this watchdog before accepting commands.
Refresh the bootstrap read URL before a demand start.
Keep the restart service separate from the disposable gates' 20-minute expiry path.
These constraints follow the [AWS stop rules](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/interruption-behavior.html).

## Exact source and warm storage

Reuse the copies service's source selection and leases, plus the Mac/Linux runner's input identity checks.
Extend their transport boundary for S3; do not create an independent checkout service.
Key each cache by repository and persistent worktree identity.

1. Capture the exact tracked and permitted untracked files through the existing snapshot service.
2. Include staged and unstaged changes without changing the caller's real index.
3. Upload missing content by digest and send a manifest with the tree, command, features, and allowed environment.
4. Acquire the worktree cache lease before materializing that manifest.
5. Verify hashes, deletions, permissions, symlinks, and the resulting source identity before execution.

Reuse the initial 318 MB source snapshot and send changed content on later requests.
Never copy ignored files, credentials, the caller's HOME, or its `target/` directory.
Refuse remote execution if an excluded input affects the build.
Keep a stable remote source path and a separate `target/` for each worktree.
Retain toolchain, feature, profile, and shell identities with each cache.
Use the existing Linux shell cache and invalidate incompatible tool state.

Keep a 40 GiB root volume and a 300 GiB encrypted gp3 data volume in the same zone.
Retain the data volume through stop and instance replacement.
Keep Rust tools and Cargo caches on the data volume.
Reserve 30 GiB free and initially budget 56 GiB per worktree cache, following the existing Hana Mac allowance.
This fits approximately four warm caches after shared tools and sources.
Remove only unleased build outputs, oldest first, when space is low.
Never remove an active cache or unsaved caller source.
A replacement must fence the old owner before attaching the single-writer volume.
Do not create trial snapshots; measure cold rebuilds after cache loss.

## Tools and returned results

Use Hana's pinned Nix shell and Rust 1.99.1 with its declared components, including clippy.
Validate `rustc -vV` against the requested compiler identity.
Disable the target when the requested toolchain or shell cannot be reproduced.

Return stdout, stderr, exit status, timings, peak memory, source identity, and one buildlog execution row.
Keep interrupted attempts linked to the caller; accept only one final result per execution generation.
Reuse diagnostics path mapping and result synchronization.
Accept a remote result only when its source identity matches the caller's capture.
Reject results when the command no longer matches.

Preserve the existing warning policy for clippy judgments.
Return no patch or binary.
Preserve the existing lint sequence and recapture source after local repairs before another remote judgment.
The queue and remote-runner owners must confirm that the provider integration preserves them.

## Cost and configuration

The trial workload totals 197 steps and 5.7 execution hours per day.
This covers 67% of the supplied 8.5 daily execution hours.
Model AWS execution at one to two times that duration, plus idle time.
Assume 1.3–1.6 idle hours for planning, giving 7–13 running hours.
The trial must measure actual idle time.
The full-day case covers longer idle periods, slower execution, and failed work.

| Daily component | Estimate, USD |
| --- | ---: |
| Compute, 7–13 hours at the supplied USD 0.13/hour | 0.91–1.69 |
| 340 GiB gp3 at USD 0.08/GiB-month, using 30 days | 0.907 |
| Public IPv4, 7–13 hours at USD 0.005/hour | 0.035–0.065 |
| Diagnostic transfer, object calls, and query calls, allowance | 0.15 |
| Total | About 2.00–2.81 |
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
Use bill totals and usage quantities for daily cost; label recent estimates and pending bills explicitly.

Add only these keys in a new `config/hana_aws.conf`; change no existing configuration value.
Read them through `scripts/lib/conf.py`.

| New key | Value | Reason |
| --- | --- | --- |
| `enabled` | `off` | Require an explicit decision before routing Hana work. |
| `region` | `us-east-2` | Use the supplied Ohio prices and one storage zone. |
| `instance_type` | `m7a.2xlarge` | Provide 32 GiB RAM at the smaller supplied price. |
| `spot_max_usd_per_hour` | `0.15` | Bound compute exposure; refuse higher prices. |
| `idle_stop_seconds` | `600` | Retain short bursts and stop through quiet periods. |
| `start_timeout_seconds` | `120` | Bound a caller's startup wait. |
| `max_steps` | `1` | Establish memory and cache behavior before concurrent execution. |
| `step_cpu_limit` | `4` | Allow the observed work without occupying all host CPUs. |
| `step_memory_gib` | `24` | Leave 8 GiB for the operating system and control processes. |
| `root_gib` | `40` | Retain the system and Nix store without a smaller untested root. |
| `data_gib` | `300` | Retain several independent worktree caches. |
| `cache_budget_gib` | `56` | Start with the existing Hana Mac cache allowance. |
| `free_floor_gib` | `30` | Prevent disk exhaustion during compilation. |
| `daily_budget_usd` | `6` | Bound this workload within the unchanged USD 35 account limit. |

Keep the step allowlist fixed to Hana clippy judgments and check.
Reject tree-writing flags such as `--fix` before remote execution.
Derive the compiler revision from the requested source instead of adding a duplicate version setting.

## Security, ownership, and implementation

Keep the same secret boundary as disposable gates.
Only natedev holds AWS credentials; manual setup uses the encrypted admin session.
The machine receives only short-lived, object-scoped URLs and an expiring execution permit through that authenticated transport.
Do not install repository tokens, SSH keys, Tailscale credentials, or a broad instance role.
Refresh URLs through the trusted controller and reject stale boot or execution generations.
Run Cargo and build scripts unprivileged, outside the root control service.
Keep controller files inaccessible to the runner and reject archive paths outside the leased source root.
Hold, cancellation, interruption, and cap refusal stop the process group before local fallback.
If cancellation is uncertain, mark the attempt lost and fence its later result before local execution.
Print safe aliases and error codes, without cloud identifiers, addresses, secrets, or signed URLs.

| Files or area | Estimated change | Owner and required agreement |
| --- | --- | --- |
| `scripts/aws_target/hana_box.py` (new) | 250–350 lines for lifecycle, gate transport, execution and result handling | AWS unit |
| `scripts/aws_target/{deploy,remote,ledger}.py`, `docs/plans/builds-aws/cloudformation.json` | 180–280 lines for a separate kept workload, permit expiry watchdog, stop recovery and physical charges | AWS unit |
| `/etc/nixos/hosts/aws-build.nix`, `/etc/nixos/hosts/aws-bootstrap.sh.in`, `scripts/mac_test/linux_runner.py` | 60–100 lines for warm Rust tools, compiler checks and the command cgroup boundary | NixOS and Linux runner owners: required go |
| `scripts/build_targets/{target_config,build_targets}.py`, `scripts/build_queue` | 100–180 lines for Hana placement, capability checks and single-machine admission | Queue unit: required go |
| `scripts/copies` and `scripts/mac_test/offload.py` | 80–140 lines for existing source and lease contracts over gate transport | Copies and remote-runner owners: required go |
| `scripts/buildlog/{record,aws_report}.py` | 60–100 lines for linked attempts, host timing and visible kept storage costs | Buildlog unit: required go |
| New configuration and AWS operation documents | Under 70 lines | AWS unit |
| Tests beside these modules | 160–280 lines | Each code owner |

| Required test rows | Expected behavior |
| --- | --- |
| Wrong repository, mend, build, nextest, local pin, disabled mode | Preserve existing placement. |
| Clippy repair flags and local repair followed by judgment | Keep repairs local and capture the updated source for judgment. |
| Simultaneous requests, idle-stop race, busy machine | Start one instance and admit one step; retain local fallback. |
| Two dirty worktrees and deleted or untracked files | Preserve exact independent source and cache identities. |
| Edited source during execution or result transfer | Reject stale remote judgments. |
| Remote refusal or loss after a local edit | Use the live worktree through the normal natedev path; do not transfer the snapshot. |
| Compiler mismatch or incompatible shell | Refuse remote execution before Cargo starts. |
| Interruption, automatic restart, lost controller, cancellation | Fence stale work, stop safely, and return one final result. |
| Controller loss during a command that never exits | At permit expiry, kill the command cgroup and stop without the controller; retain exposure until stop confirmation. |
| Memory, disk, workload budget, or account cap reached | Record the reason and use the permitted local path. |
| Stop intervals and delayed or revised bills | Charge physical compute once and retain storage charges. |

## First-day trial and decisions

1. Obtain agreement from all owners listed in the status line.
2. Confirm USD 6 of headroom below the account cap, including existing exposure and reserves.
3. Provision one instance and record all setup time and cost inside the first-day budget.
4. Measure cold, warm, and one-edit runs for clippy and check on identical snapshots on AWS and natedev.
5. Confirm equivalent exit status and diagnostics before routing live requests.
6. Enable only clippy judgments and check for one day with the stated local fallback.
7. Compare queue wait, end-to-end latency, execution time, memory, source bytes, cache hits, interruptions, and local nextest time.
8. Compare natedev queue wait with the supplied 34–83-minute baseline and matched before/after activity windows.
9. Report the full day's compute, storage, transfer, setup and failed-attempt costs from the bill when available.
10. Disable new remote work at USD 6 projected exposure or on incorrect results, and stop after active cleanup.

The USD 6 bound includes a retained disk reservation and remaining compute exposure, rather than only completed step costs.
It is a subset of the USD 35 account limit.
A separate storage decision remains necessary after the trial, because stopped volumes continue to cost about USD 0.91/day.

| Open question or risk | Decision owner |
| --- | --- |
| Cold caches and cache eviction can exceed the execution forecast. | Trial: measure before promising lower queue wait. |
| Spot interruption can prevent restarting in the fixed storage zone. | User: accept local fallback; no automatic larger or On-Demand replacement. |
| Median memory does not prove every Hana step fits. | Trial: measure peaks and keep the one-step limit. |
| A second active step could improve throughput. | Later proposal after memory and queue measurements; do not enable it in this trial. |
| The old AWS build target was removed. | User: approve this separate CPU-only workload and its USD 6 first-day bound. |
| Storage remains after the machine stops. | User: retain the warm disk or remove it after the trial. |

## Later, if the trial earns it

Mend requires a patch contract with exact-source validation before local application.
Build requires selected binary downloads, verified hashes, compatible runtime files, and real local paths.
Measure compressed outputs and agree a transfer allowance before either route expands.
These changes require a separate design and the verification owner's agreement.
