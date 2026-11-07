# One EC2 instance for each merge gate

Recommend a fresh Spot instance from a ready image as the first trial candidate.
This is a provisional recommendation, not a measured efficiency result.
Run the small size trial first. Then measure cold launch and stopped-instance start without a gate.
Keep one gate alone on each instance.
The gate target remains seven minutes, or 420 seconds, as a design requirement.
Step A costs an estimated $1.49, with a $4 reserve.
Step B costs an estimated $1.25, including optional Batch, with a $3 reserve.
The combined estimate is $2.74. The combined reserve remains $7 within the shared caps.
Natedev takes about 1,030 seconds alone and 2,660 seconds under the supplied morning load.
Keep the Rust box separate.
Keep the shared $20 daily and $400 monthly caps.
Keep queue ownership and publishing on natedev.

## Status and evidence

This document is a design. The commands and record changes marked **proposed** do not exist yet.
The source references describe this checkout. The build phases identify the required new work.
No deployment, host connection, credential read, or state-directory read formed part of this design.
The launch method and size remain unselected.
Later EC2-specific sections describe the fresh-Spot candidate.
Production integration waits for the small trial and the later concurrency trial.
Exact-tree testing, equal coverage, local publishing, and the shared spending caps remain requirements.

The user supplied these measurements from 191 full passing gates since 2026-11-01:

| Gates running | Median wall seconds, measured | Cores per gate, measured | Gates per hour, measured |
| --- | ---: | ---: | ---: |
| 0–2 | 673 | 9.1 | 8.9 |
| 2–4 | 1,164 | 5.7 | 9.1 |
| 4–6 | 1,809 | 3.8 | 9.2 |
| 6–8 | 2,377 | 2.8 | 10.5 |
| 8–10 | 2,860 | 2.3 | 11.0 |
| 10–13 | 2,926 | 2.1 | 13.8 |

The separate 1,030-second and 2,660-second comparisons describe the supplied current workload.
They do not replace the historical bins above.
The supplied CPU total is about 6,600 seconds per full gate.
The gate's sampled memory cap is 6 GiB.
Full-tree basedpyright reached 4,065 MiB.
Four concurrent gates showed 3–4.5 GiB combined memory in 75-second maximum samples.
Those samples do not establish a safe memory size for a fresh instance.

## Candidate sizes and unmeasured predictions

The target is seven minutes, or 420 seconds, for one gate alone on its instance.
The trial measures that target. It does not treat it as a prediction.
The supplied CPU seconds came from gates with contention.
CPU work, scheduling, and memory waits can differ on an idle instance.
Therefore, dividing 6,600 CPU seconds by the vCPU count does not establish an idle-instance time bound.

The c7a rows below are estimates for a later `--family c7a` run.
The command defaults to the family in `instance_type`, which is currently m7a.
These c7a prices do not price the default m7a trial.
The predictions below only choose trial sizes.
They use `250 + 6350 / vCPU` seconds, rounded upward.
The 250-second serial allowance is an estimate, not a measured stage duration.
Each price calculation adds 120 estimated seconds for startup and result delivery.
The calculation treats that whole allowance as billed time, which overstates any unbilled allocation wait.

| Instance | vCPU, sourced | GiB, sourced | On-Demand $/hour, estimated | Spot $/hour, estimated | Tests seconds, estimated | On-demand compute $/gate, estimated | Spot compute $/gate, estimated | Spot total $/gate, estimated |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `c7a.2xlarge` | 8 | 16 | 0.4106 | 0.1256 | 1,100 | 0.139 | 0.043 | 0.052 |
| `c7a.4xlarge` | 16 | 32 | 0.8211 | 0.2427 | 700 | 0.187 | 0.055 | 0.065 |
| `c7a.8xlarge` | 32 | 64 | 1.6420 | 0.3234 | 500 | 0.283 | 0.056 | 0.065 |
| `c7a.16xlarge` | 64 | 128 | 3.2840 | 0.7717 | 350 | 0.429 | 0.101 | 0.110 |

The total includes the variable charges described under Money.
It excludes the shared monthly allowance and retries.
Eight vCPUs provide the low-cost comparison. Sixteen and thirty-two test the middle sizes.
Sixty-four tests whether more dedicated capacity can reach the target.
The predictions cannot establish which size wins, or whether any size meets 420 seconds.

The vCPU and memory specifications are sourced from [AWS compute instance specifications](https://docs.aws.amazon.com/ec2/latest/instancetypes/co.html), accessed 2026-11-06.
The hourly rates remain estimates from the earlier design, not verified AWS Ohio price quotes.
Record actual Ohio prices and capacity during the user-run trial.
On-Demand rates price the warm-pool candidate; Spot rates price the other EC2 candidates.
Step A selects the size. Step B compares startup methods without running gates.

### Work that does not scale

Each test module is one job. Tests within that module remain serial.
Basedpyright is one full-tree job.
Memory reservations can prevent other jobs from starting beside it.
Failed modules can run again, one at a time.
Tree preparation, process startup, and result delivery also take time.
More instances remove competition between gates. They do not remove these limits within a gate.

The runner defines these rules at `scripts/production/merge_gate.py:959`, `:1018`, and `:1134`.
The measured type peak requires a 4,878-MiB reservation with the existing margin.
See `docs/as-built/merge-gate-and-test-speed.md:76` and `:81`.
The default job limit is half the CPU count, so sixteen vCPUs give eight jobs.
See `docs/as-built/merge-gate-and-test-speed.md:82`.
Keep that default for every trial run. Keep the sampled memory cap at 6 GiB.

Measure launch request, first test, type completion, last module, result receipt, and confirmed termination separately.
Record CPU seconds, sampled memory, failed modules, reruns, and the actual price.
Compare the same full tree with a natedev gate.
The first build phase defines the two small trial steps and their selection rules.

## Split and owners

The planned production boundary is `merge_gate.run_suites`, at `scripts/production/merge_gate.py:918`.
The trial calls the same runner with `WholeMachine`.
`SharedMachine` keeps the existing admission and scheduling behavior.
The smallest gate change replaces its checkpoint call with a backend call returning the same `GateRun` meaning.
That call currently occurs at `scripts/production/merge_gate.py:1464`.

| Owner | Work |
| --- | --- |
| **sdlc devtools** | Own the gate backend call, test selection, result format, and pass certificates. |
| **queue** | Choose EC2 or natedev before local admission. Keep queue order, retries, and the durable request on natedev. |
| **aws-unit** | Own the image, launch template, disposable instances, transport, cleanup, and shared cost accounting. |
| **sdlc devtools** | Keep merge, rollback, push, promotion, and change-review writes on natedev. |

The merge happens before tests. Publishing happens after tests.
See `scripts/production/merge_gate.py:1451`, `:1471`, and `:1480`.
Recheck the local merge tip before publishing the remote result.
Preserve the existing ancestry test and rollback behavior.
The instance receives no Git push credential.

Queue dispatch must precede `gate_reservation`, at `scripts/production/merge_gate.py:337`.
An EC2 gate must not reserve 6 GiB on natedev while its tests run elsewhere.
Its small local control process still follows the queue's existing accounting rules.
The worker uses the existing admission code against its own machine.
This design does not change the held admission rule for concurrent local gates.

Standing copies have two slots per owner and repository.
They retain their busy lock throughout a command.
See `docs/as-built/gate-checkouts.md:25` and `:77`.
Prepare the bundle within the copy lock, then release that copy before the remote gate runs.
The result names the immutable commit and tree, so it does not depend on that copy remaining unchanged.
Otherwise, twenty remote gates could still wait behind two local copies.

## Launch methods and the first decision

All AWS links below have access date **2026-11-06**.
Times and costs remain **estimated** or **to measure**, unless marked **sourced**.
Phase 1 compares sizes and basic startup methods. Phase 2 contains the full method table and the concurrency trial.

### Cold capacity still requires EC2

Batch launches EC2 instances through its managed compute environment.
EKS Cluster Autoscaler requests nodes through EC2 Auto Scaling when pods cannot fit.
See [Batch managed capacity](https://docs.aws.amazon.com/batch/latest/userguide/managed_compute_environments.html) and [EKS Cluster Autoscaler](https://docs.aws.amazon.com/eks/latest/best-practices/cas.html), accessed 2026-11-06.

**Inference:** with the same image, type, and capacity, both cold paths require the same EC2 launch work as a direct launch.
They add scheduling and worker registration before a job can start.
They cannot remove EC2 launch time; they offer no cold-start speed advantage from their scheduler alone.
Actual request times can vary with capacity and configuration. Step B measures the Batch difference instead of assuming its duration.

A faster capacity path needs existing running or prepared capacity, which has idle or storage cost.
A stopped warm pool makes the same trade: preparation before the request, plus retained storage.
AWS recommends warm pools when initial boot delay matters and warns about unnecessary pool cost.
See [warm pools](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-warm-pools.html), accessed 2026-11-06.
The value of Batch or EKS here is less lifecycle code for us to own, rather than faster cold EC2 launches.

### Decision from the small trial

Recommend fresh Spot from our image unless the startup measurements show a material delay beside gate time.
It preserves the current systemd and cgroup behavior. It retains no idle worker or worker volume.
If cold overhead is small beside gate time, fresh Spot wins because it has no worker idle cost.
If cold overhead is large and stopped starts remove it, a stopped warm pool wins the startup comparison.
Include retained-volume and On-Demand costs before production use; Phase 2 measures the complete pool.
AWS excludes Spot from mixed-instance warm pools. The later pool candidate uses On-Demand instances.
See [warm-pool limits](https://docs.aws.amazon.com/autoscaling/ec2/userguide/ec2-auto-scaling-warm-pools.html), accessed 2026-11-06.

Report the cold overhead as a fraction of the measured gate time and show the seconds saved by stopped starts.
Agree whether that delay matters from the measured values; do not invent a threshold before the first answer.
Choose Batch over our launch code only if Step B shows a start time close to direct cold launch.
Its reason for selection is less code. A close timing result permits further qualification; it does not prove gate compatibility.

Keep EKS outside the trial unless a later requirement needs Kubernetes.
Its sourced standard control-plane rate is $0.10/hour; an estimated 730-hour month costs $73 before workers and storage.
Extended support costs a sourced $0.60/hour, or an estimated $438 for the same month.
See [EKS pricing](https://aws.amazon.com/eks/pricing/), accessed 2026-11-06.

### Fargate waits for compatibility evidence

Keep Fargate outside the trial until the runner's `/proc`, cgroup v2, and process group use is proven there.
`merge_gate.py:492` samples descendants. `queue_place.py:284` requires its process group; `:291` refuses a missing memory scope.
The current gate also needs a user systemd manager and delegated cgroup scopes.
Fargate's task limits do not prove equivalent module accounting or cancellation.

ECS Fargate Spot supports sourced task sizes from 0.25 to 32 vCPUs and 0.5–244 GiB through permitted pairs.
The sourced 32-vCPU memory choices are 60, 120, and 244 GiB.
Fargate supports a shared task PID namespace but does not offer privileged tasks or configurable tmpfs/shared-memory mounts.
See [task limits](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html), [Spot support](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-ecs-fargate-32vcpu/), and [PID namespace settings](https://docs.aws.amazon.com/AmazonECS/latest/APIReference/API_TaskDefinition.html), accessed 2026-11-06.
Do not remove coverage or disable the memory scope to qualify Fargate.

## Startup details waiting for the trial

The remaining EC2-specific sections describe the fresh-Spot candidate, not a selected production method.
Their production launch method waits for Steps A and B, then the concurrency trial.
Shared requirements remain: exact tree, equal coverage, dedicated gate capacity, bounded cost, result integrity, and confirmed cleanup.
The ready-image candidate contains NixOS, required store paths, Python, Node, basedpyright, uv, Git, and shells.
It contains no job tree, host credential, session state, or Rust build output.
A launch template stores settings; it does not contain installed tools.
Containers require the same tool manifest with separately qualified host integration.
No candidate receives a production startup deadline from the estimates above.

## Launch, scale, and termination

This section describes production with fresh Spot. Phase 1 uses the smaller cleanup arrangement below.
Use one launch template for all gates.
Use one ordinary `RunInstances` request per attempt, with one instance and an idempotency token.
Use one-time Spot requests with termination on interruption.
Set guest shutdown behavior to terminate.
Set the root EBS volume to delete on termination.
Create no retained home volume for a gate.

Twenty ready requests can issue twenty launches concurrently.
This candidate has no autoscaling group or AWS work queue between requests and instances.
The existing queue remains the only work queue.
The twenty-instance limit is a proposed configuration value, not a new local admission rule.
AWS refusal returns the affected request to natedev.

The trial reads the regional Standard Spot quota before it creates resources.
It subtracts the configured Rust box vCPUs to set the trial limit.
`--vcpus N` overrides the limit and skips the quota read.
One attempt at each size needs 120 vCPUs when all four attempts overlap.
Three attempts at every size need 360 vCPUs if all twelve overlap.
The existing sixteen-vCPU Rust box raises those regional Standard Spot totals to 136 and 376.
An eighty-vCPU quota leaves 64 vCPUs for this trial when the Rust box runs.
The automatic limit is 64 for that quota. The attempts then run in smaller groups.
Twenty concurrent gates need `20 × winning vCPU count + 16` when the Rust box also runs.
That is 336 vCPUs for sixteen-vCPU workers, or 1,296 for sixty-four-vCPU workers.
Image construction can require additional capacity.
AWS lists a default Standard Spot quota of five vCPUs.
See [Spot quotas](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-spot-limits.html), accessed 2026-11-06.
The current account quota is unknown. Regional quota does not guarantee capacity in zone 2c.

Each launch record contains its absolute expiry time before the launch request.
Set the absolute lifetime from measured gate time and its cost reservation.
It includes startup, tests, upload, and termination delay.
All lifetimes and intervals belong in configuration read through `conf.py`.

| Event | End action | Queue result |
| --- | --- | --- |
| Gate completes | Upload the final result, then shut down. Natedev also requests termination. | Use the complete result. |
| Gate process dies | The worker service records failure and shuts down. | Red attempt. |
| Natedev dies | The worker finishes or reaches its absolute expiry, then shuts down. | Recover the stored result, or mark the attempt red. |
| Guest or worker hangs | An AWS expiry function terminates the expired instance. | Red attempt. |
| Spot interrupts | Upload available evidence, then terminate. | Red interrupted attempt. |
| Launch reply disappears | Retry the same idempotent request and reconcile its instance. | Pending until identified, then normal completion or expiry. |

One EventBridge schedule invokes one small Lambda function each minute.
The function terminates only expired instances tagged for merge gates.
This is the independent cleanup owner when both natedev and the guest fail.
The expiry never moves when a service restarts.
The guest also has an absolute systemd deadline, rather than an inactivity timer.
Normal completion does not wait for the expiry function.
An AWS control-plane outage can delay termination; no design can promise zero charges during that outage.

The Lambda function and schedule need separate production permission.
Steps A and B install neither resource. They use guest shutdown deadlines and command cleanup.

## Exact tree delivery

Use one private S3 bucket in Ohio for source bundles, request manifests, results, and logs.
Use HTTPS and instance-role credentials. Use no SSH or Tailscale connection for gate execution.
Use a public subnet with an automatically assigned public IPv4 address and no inbound rules.
This avoids a NAT gateway and private endpoint services.

1. Create the merged commit on natedev.
2. Export a Git bundle containing that commit and the history needed for its references.
3. Upload the bundle under its content digest.
4. Write an attempt manifest with the commit, tree, suites, type paths, image identity, and expiry.
5. Launch the instance with the manifest location and attempt identity.
6. Import the bundle into a real, private Git checkout on the instance.
7. Run the tested checkout's runner and module launcher.

Include the required `origin/main` and merge-branch references.
Include a certified ancestor when affected selection uses one.
Do not send a shallow archive without Git history.
Do not copy local Git configuration, hooks, credential helpers, private refs, or untracked files.
The checkout records the real origin URL without a credential.
No private credential enters the image or source bundle.

The module launcher must come from the tested tree.
The current runner enforces this at `scripts/production/merge_gate.py:910`.
The source copy contract requires independent Git metadata at `docs/as-built/gate-checkouts.md:43`.
Git preserves the commit and tree identities across transfer.
Natedev compares those identities with the returned result before it publishes anything.

Estimate 5–15 seconds for a 100-MB bundle over the two transfers and checkout.
Measure the real bundle size and transfer rate in the trial.
Step A measures this allowance within running-to-gate-start time.

## Test environment and equal meaning

Use a non-root Linux user with a real passwd home, a user systemd manager, and cgroup v2.
Give it an empty private home and the public configuration from the tested tree.
Keep the same Python and basedpyright versions as natedev.
Use the same pinned Nix packages for the remaining tools.
Do not copy natedev's private home configuration.

| Requirement found in code | File and line | Instance treatment or local work |
| --- | --- | --- |
| User scopes and `builds.slice` | `scripts/build_queue/queue_place.py:105`, `:122`, `:290` | Provide the user manager and slice. Keep real scope accounting. Do not set `BUILDLOG_SCOPE=0` for the gate. |
| Fixed gate memory reservation | `scripts/production/merge_gate.py:337` | The trial uses no admission service or memory reservation on the instance. |
| Linux process memory and process groups | `scripts/production/merge_gate.py:484`, `:590`, `:1146` | Provide `/proc`, cgroup v2, and normal process permissions. Keep sampling and group cleanup. |
| Writable tmpfs | `scripts/production/merge_gate.py:1013` | Provide writable `/dev/shm` with space for the gate's temporary files. |
| Optional shared CPU device | `scripts/production/merge_gate.py:312` | Use the existing absent-device path. One instance has one gate, so it needs no shared `/dev/steve` pool. |
| Pinned type packages and uv | `scripts/production/merge_gate.py:98`, `:526` | Include the type environment in the image. Keep its existing pin validation. |
| A real basedpyright executable | `scripts/production/test_merge_gate_suites_types.py:36` | Install the same version as natedev. A fake executable cannot satisfy this test. |
| Nix launch environment and trusted home paths | `scripts/lib/nix_env.py:76`; `scripts/lib/test_nix_env_launches.py:54` | Install Nix and the public trusted-root configuration. Existing tests supply their fake launch programs. |
| bash, zsh, Git, jq, and core tools | `scripts/production/test_smoke_launch.py:35`; `scripts/statusline/test_statusline.py:30`; `scripts/hana_instances/test_hana_instances.py:445` | Install them. Missing zsh must not remove tests from the remote gate. |
| Fixture homes and public target settings | `scripts/direct/verify_mac_offload_test_support.py:675`, `:722` | Keep fixture creation unchanged. Copy the complete public configuration tree. |
| Fake Mac and network commands | `scripts/direct/verify_mac_offload_test_support.py:683`, `:690` | Run these tests remotely with their existing fakes. They need no real Mac. |
| Fake herdr sessions | `scripts/herdr/test_herdr_sessions.py:59`, `:84` | Keep the fixture's herdr executable, process tree, and private XDG paths. No live daemon is required. |
| Live herdr matcher and downloaded rules | `scripts/herdr/test_detection_rules.py:220` | Keep this complete module on natedev. Its installed rules are outside the transferred tree. |
| Installed home-script override behavior | `scripts/build_hold/test_override_guard.py:439` | Keep this complete module on natedev. Its passwd-home script is part of the host behavior. |
| Non-root and OOM score behavior | `scripts/build_hold/test_hold_shortcut.py:124`; `scripts/lint/test_invoke_scope.py:110` | Run without root and without an inherited elevated OOM score. Preserve the existing test conditions. |
| User time zone | `scripts/lib/user_zone.py:16`, `:31`; `scripts/production/test_merge_gate_ledger.py:63` | Transfer the public zone setting. Record event times in UTC. Keep test-specific zone overrides. |
| Private logs and tool tripwires | `scripts/production/merge_gate.py:657` | Preserve every per-module environment, fake-tool rule, memory tripwire, and private log directory. |

The two local modules run from the same merged tree during every full gate.
The remote module list excludes those modules explicitly.
The final verdict combines the remote jobs, the local modules, and the full-tree type job.
The local jobs keep their real host identities in reports.
An affected gate includes the local modules when selection requires them.
New modules still enter through the existing automatic discovery rule.

The trial runs one full natedev baseline and the combined remote gates on the same tree.
It compares module identities, test counts, skip identities, verdicts, and type output.
A new skip cannot substitute for a previously executed test.
Any additional test that requires natedev remains local until its environment has an equivalent fixture.
This is the proof of equal coverage, not a claim that Linux hosts behave identically.
No `scripts/production/full_test.py` exists in this checkout.

## Result and queue record

The existing asynchronous record is `BumpGateRecord` in `scripts/production/merge_checkpoint.py:1256`.
Its location uses Git's `checkpoint-completed/<merge-head>/bump-gate` path.
See `scripts/production/merge_checkpoint.py:522` and `:1365`.
Its current result is `pending`, `green`, or `red`.
It also records the local PID, process start, tip, log, commands, and notice recipients.
`bump_gate.py` completes this record after checkpoint commands return.
See `scripts/production/bump_gate.py:45`.

Keep this record on natedev. Keep its existing result values and notice delivery.
Add one execution field with distinct local and EC2 variants.
The EC2 variant holds the attempt identity, instance identity, image identity, expiry, artifact path, and failed modules.
Keep the local supervisor's PID and process start for existing liveness handling.
Do not create another merge queue or replace the recorded commands.
Ordinary synchronous gates return the combined `GateRun` through the same backend call.

The worker uploads module logs as jobs finish.
It uploads one final result after process cleanup.
That result includes every final module status, first failure, isolated rerun, killed reason, type output, and skip list.
It also includes wall time, CPU seconds, memory peaks, source identity, and the gate buildlog record.
The final object is the completion signal. Partial logs cannot complete a gate.

The local supervisor accepts a pass only from the current attempt and exact tested tree.
It requires the existing `GateRun.passed` meaning and successful required artifact delivery.
It then combines the local module results and records green or red.
Existing completion already rejects a superseded local runner at `scripts/production/merge_checkpoint.py:1351`.
Extend that comparison to the EC2 attempt identity.
A late result from an old attempt cannot overwrite a retry.

A dead instance without a complete result is red, never green.
After supervisor restart, reconcile the saved attempt before creating another instance.
A retry gets a new attempt identity and preserves the failed attempt's evidence.
The queue can start the same tree again.

Reuse `scripts/test_times/gate_record.py:66` for the gate measurements.
Import its `gate_run` record on natedev once by record identity.
Keep the real remote host in the record, as written at `scripts/test_times/gate_record.py:216`.
The current writer only warns when a record write fails.
Remote completion must therefore return the record explicitly, rather than infer success from that warning path.
Do not add short-lived hosts to the build-target registry or the regular peer sync service.
These jobs are merge gates, not Rust offload attempts.

## Reuse and the cache claim

The user's claim is correct for Rust build output in this gate.
The runner places tripwires before real Rust tools at `scripts/production/merge_gate.py:671`.
A Python merge gate should not compile Rust through those tools.
Retaining `target/` does not accelerate this workload's intended execution.

The claim does not apply to all reuse.
The runner reuses a full pass for the same tree, host, and suite set.
It selects affected modules from a certified full ancestor on the same host.
See `scripts/production/merge_gate.py:788` and `:804`.
It also saves module times, CPU times, memory peaks, and history at `scripts/production/merge_gate.py:959`.
These records improve job ordering, reservations, and reports.
The image also avoids repeated tool and type-package installation.

Every Step A gate uses `--full` and no pass from another host or earlier attempt.
Fresh instance names otherwise prevent current pass reuse and ancestor selection.
Do not pretend that a disposable instance is natedev.

Later, **sdlc devtools** can key remote certificates by the image and environment identity, alongside the existing tree and suite set.
Keep actual host identity as evidence.
Keep the certificate store and the `full-required` marker on natedev.
Send a selected ancestor and the current scheduling records with each request.
Import updated records after the gate.
Keep natedev certificates separate from remote certificates.
The combined certificate also includes successful local host-dependent modules.

An image or tool change starts a new certificate environment.
An affected pass must not become a full certificate.
Keep the current final clean-tree comparison at `scripts/production/merge_gate.py:820`.
Measure a cold full gate, a repeated full gate, and an affected gate to quantify each form of reuse.

## Spot behavior

Fresh EC2, Batch, and EKS gate candidates use Spot.
The warm-pool comparison uses On-Demand capacity; production purchase mode waits for the trial.
Short jobs reduce the amount of work lost per interruption, but they do not eliminate interruption risk.
No measured interruption rate for this type and zone was supplied.

AWS provides a best-effort two-minute interruption notice.
See [interruption notices](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-instance-termination-notices.html), accessed 2026-11-06.
The worker stops launching modules, stops its owned process groups, and uploads available evidence.
It reports an interrupted red attempt.
The queue retries the exact tree or sends it to natedev.
It does not resume a partial test pass or certify the interrupted tree.
No persistent Spot request replaces the terminated instance automatically.

At a hypothetical 5% interruption probability, full-cost retries multiply expected cost by `1 / 0.95`, or about 1.053.
That is a sensitivity estimate, not a measured probability.
Local fallback avoids repeated demand on an unavailable Spot pool.

## Money and shared limits

The limits remain $20 per accounting day and $400 per month for the combined AWS workload.
The Rust box already consumes part of both limits.
There is no separate $400 allowance for merge gates.
The examples below price fresh Spot only. The later method comparison includes idle and fixed charges.

The sixteen-vCPU example below uses 820 billed seconds and a 40-GiB gp3 root volume.
This is a costing example, not a size recommendation.
The image size, disk use, runtime, and artifact size remain estimates until the trial measures them.
No extra IOPS or throughput is planned.

| Charge | Estimated amount for one successful gate | Basis |
| --- | ---: | --- |
| Spot compute | $0.05528 | $0.2427/hour for 820 seconds. |
| Root EBS | $0.00101 | 40 GiB at $0.08/GiB-month, using a 30-day month. |
| Public IPv4 | $0.00114 | $0.005/hour for 820 seconds. |
| Result transfer | $0.00500 | Planning allowance for 50 MB at an estimated $0.10/GB. |
| Object requests | $0.00050 | Planning allowance, including result checks during the run. |
| Rounded variable total | **$0.065** | Includes a small rounding allowance. |

The EBS rate is the public pricing-page example, not a verified Ohio quote.
The page also prices standard snapshots at $0.05/GB-month and states a sixty-second minimum for gp3 storage.
See [EBS pricing](https://aws.amazon.com/ebs/pricing/), accessed 2026-11-06.
Public IPv4 costs $0.005/hour, with a sixty-second minimum.
See [VPC pricing](https://aws.amazon.com/vpc/pricing/), accessed 2026-11-06.

Allow about $3 per month for shared resources at the example demand.
This estimate includes two 20-GB image snapshots at $2 per month total.
The remaining $1 covers estimated object storage, expiry-function execution, schedule calls, and their small logs.
Object request costs remain in the per-gate allowance.
Retain gate logs for seven days, as a proposed configuration value.
Retain the current image and its predecessor. Delete older images and their snapshots through the user-run update command.
Image-construction compute, volumes, transfer, and temporary addresses also count against the caps.
They are additional setup costs, not hidden inside the successful-gate estimate.

Inbound source delivery uses no budgeted Internet egress from natedev.
Keep EC2 and object storage in the same region.
Budget result downloads without assuming an unused free transfer allowance.
AWS's 100-GB monthly free transfer allowance is account-wide.
See [EC2 on-demand pricing](https://aws.amazon.com/ec2/pricing/on-demand/), accessed 2026-11-06.
The fresh-Spot candidate uses no NAT gateway, load balancer, retained gate volume, or paid snapshot acceleration.
Steps A and B give the first startup answer. Phase 2 measures the complete pool cost if needed.

| Limit example | Estimated capacity before Rust costs, retries, and image builds |
| --- | ---: |
| $20/day, less $0.10 shared daily allowance | 306 gates/day |
| $400/month, less $3 shared monthly allowance | 6,107 gates/month |
| The same monthly limit across 30 days | About 203 gates/day |
| One burst of 20 successful gates | $1.30 variable cost |
| 191 successful gates in one day | $12.415 variable cost |

Today's actual gate count was not supplied.
The 191-gate total spans roughly five to six days as of 2026-11-06.
That implies about 32–38 gates/day, not a measured count for today.
At that rate, estimated gate spend is $2.08–$2.47/day, plus about $0.10/day shared costs.
For today's count `N`, the report must show `0.065 × N + shared daily cost + retry cost`.
These daily estimates use the sixteen-vCPU example. The trial replaces its $0.065 cost with the measured winner's cost.
The **queue** owner supplies `N` for the later production cost report. Steps A and B use no build queue.
No private state was read to invent that number.

### Ledger change

The current ledger describes one instance observation stream.
See `scripts/aws_target/ledger.py:33` and `:66`.
It also permits only one unresolved compute request at `scripts/aws_target/ledger.py:669`.
Appending twenty workers to that stream would misprice overlapping intervals.
This code cannot account for the proposed fleet unchanged.

**aws-unit** adds per-attempt launch reservations and per-instance charged intervals to the existing cost authority.
Keep these separate from the Rust box's observations and control state.
Sum both workloads before the existing live-limit decision at `scripts/aws_target/ledger.py:797`.
Take one cost lock across the decision and reservation, before each launch.
Concurrent queue requests must not spend the same remaining dollars.

Reserve the configured maximum Spot price through the absolute expiry, plus storage, transfer, and termination allowance.
Release unused reservation only after confirmed termination or confirmed absence of resources.
Retain an uncertain launch reservation across a lost API reply or controller restart.
Split reservations across accounting days and months when a job crosses their boundary.
The caps therefore constrain twenty launches as well as one launch.

For twenty workers, reserve `20 × winning type's configured maximum hourly price × lifetime hours`, plus other charges.
At a hypothetical $0.30/hour maximum, twenty one-hour compute reservations total $6 before other charges.
That example does not price every trial size or select a winner.
The user-run price stage records the configured maximum before routing starts.

Use the existing cost-scope tag for both workloads and a separate workload tag for their reports.
Tag instances, volumes, snapshots, and the shared resources.
Reuse one UTC Cost Explorer query and its saved-reply recovery at `scripts/aws_target/ledger.py:743`.
Apply each actual charge once across the combined estimate.
Keep the configured query fee in the total.
Do not query Cost Explorer from each worker.
Refresh combined charges when either workload needs them, even when Rust offload is disabled.
The gate switch must not enable Rust offload or start the Rust box.
Accounting-day costs still use the configured accounting zone. Performance comparisons use the report's local day.

## Access and permission questions

Sessions send queue requests. They make no AWS calls.
The queue daemon on natedev makes the required scoped machine calls.
The user alone runs deployment administration through `deploy.py`.
Reuse the existing scaler credential-file design and profile on natedev.
Keep the credential in the existing private file with mode 0600.
The service sets `AWS_SHARED_CREDENTIALS_FILE=~/.config/aws-build/credentials` and uses profile `build-scaler`.
Read its location from service configuration. Never copy it to an instance or the repository.

The current scaler policy permits start and stop on the Rust instance.
It also permits describe calls and Cost Explorer reads.
See `docs/plans/builds-aws/cloudformation.json:703`.
It does not permit disposable instance launches.
The following questions describe additional permissions for production with fresh Spot.
They are not the permission list for Steps A and B.
They are proposed grants, not permissions this design assumes already exist.

| Permission question for the user | Scope and reason |
| --- | --- |
| May the queue daemon receive `ec2:RunInstances`? | Restrict it to the gate image, launch-template settings, subnet, security group, instance profile, and approved type in zone 2c. |
| May it receive `ec2:CreateTags` during `RunInstances` only? | Set gate ownership, cost scope, attempt, and absolute expiry on each instance and volume. |
| May it receive `iam:PassRole` for the gate instance role only? | Attach the object-transfer role. Limit the recipient service to EC2. |
| May it receive `ec2:TerminateInstances` for gate-tagged instances only? | End completed or cancelled gates. Exclude the Rust box. |
| May it receive `s3:PutObject` within the gate bucket? | Publish source bundles and attempt manifests. |
| May it receive `s3:GetObject` within the gate bucket? | Read results and logs. |
| May the worker role receive `s3:GetObject`? | Read public source bundles and its assigned manifest. |
| May the worker role receive `s3:PutObject`? | Write results only under its own instance's artifact prefix. |
| May the expiry role receive `ec2:DescribeInstances`? | Find expired gate instances. The action requires regional describe scope. |
| May the expiry role receive `ec2:TerminateInstances`? | Terminate only gate-tagged instances after their recorded expiry. |
| May `deploy.py up` install the expiry function and its schedule? | Cleanup must remain available when natedev and the guest both fail. |

Reuse the scaler's existing `ec2:DescribeInstances` and `ce:GetCostAndUsage` grants.
Pin the launch-template version and image in the deployed local manifest, so runtime needs no image-management grants.
Use instance-role temporary credentials for object transfer.
Workers receive no EC2 launch permission, Git credential, or scaler key.
Use bucket lifecycle rules for log removal. Runtime needs no bucket administration or object deletion grant.

The trial reads the Standard Spot quota and reserves capacity for the configured Rust box.
Can the quota support 120 trial vCPUs for one concurrent attempt at every size?
Can its quota support `20 × winning vCPU count + 16` before fleet routing starts?
Any required quota increase remains part of the user-run deployment path.
No new notification, manual approval per gate, or resource check unrelated to this request is proposed.

## One-command setup and image updates

Extend the existing staged `deploy.py up` command with a separate merge-gate workload.
The current stage runner appears at `scripts/aws_target/deploy.py:916`.
The **proposed** user command is:

`python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources`

This command uses the user's existing deployment inputs and administrator access.
It creates or reconciles the image, bucket, worker role, launch template, expiry service, and scoped queue policy.
It records priced inventory before the first charged resource.
It installs queue integration after the small trial and the later concurrency trial.
Rerunning it reconciles existing resources rather than creates another fleet.
It requires no per-instance Tailscale login, SSH setup, console action, or package installation.
An absent AWS account or unavailable administrator access remains outside a command's ability to create its own authority.

Build the image from the selected Nix system and a public tool manifest.
Include the gate's Python, basedpyright, Node, shell tools, and pinned type packages in that manifest.
A changed manifest causes `up` to build and qualify a new image.
Publish a new template version after qualification. Let existing attempts finish on their recorded image.
The queue sends work to natedev when no image matches the required tool manifest.
The user reruns the same command after a tool change.

The Rust box already records its desired system with its saved image.
See `scripts/aws_target/box.py:594`.
Reuse that comparison pattern, not the box's image or Tailscale state.
Keep a separate gate image record and image tags.
Gate image updates must not delete the Rust image or its snapshots.
Put gate settings in a proposed `config/merge_gate_instances.conf`, read through `conf.py`.
Read the existing caps and placement from `config/aws_target.conf`; do not copy their values into another setting.

## Fallback and separation from Rust

AWS capacity refusal, missing quota, a reached cap, or an unavailable image sends the gate to natedev.
Use the current local gate and its existing queue admission.
Do not silently switch to on-demand, another zone, or a larger spending cap.
A failed remote test remains a red result. Capacity fallback must not erase a reproducible test failure.
Interrupted infrastructure attempts retain their red evidence before a retry.

The two workloads share deployment administration, scoped credentials, public configuration conventions, accounting limits, and cost reconciliation.
They do not share instance identity, home volume, claims, slot state, image state, lifecycle service, or artifact directories.
The Rust box keeps its persistent Spot request, retained home volume, and existing routing.
The gate template uses one-time requests and disposable root volumes.
The existing template sets persistent requests and shutdown-to-stop at `docs/plans/builds-aws/cloudformation.json:501` and `:515`.
Do not change those settings for Rust.

Gate termination does not call the Rust `teardown.py` path.
That path removes the box's compute resources at `scripts/aws_target/teardown.py:290`.
Gate cleanup names only the recorded gate instance and its disposable resources.
No new gate host enters `config/build_targets.conf`.

## Phased build order

### Phase 1: two small trial steps

The user runs each command from the `builds-aws` worktree.
Neither command uses the natedev build queue or a natedev baseline.
`deploy.py` dispatches to `scripts/aws_target/gate_trial.py`.
Only the user runs these commands against AWS.

#### Step A: size

```sh
~/.claude/scripts/lib/py scripts/aws_target/deploy.py gate-trial --step size --repo . --approve-resources
```

The config defaults are `--sizes 8,16,32,64 --runs 3`.
`--vcpus N` is optional for both steps. It overrides the automatic concurrent vCPU limit.
The command reads `GetServiceQuota` for EC2 quota `L-34B43A08` in the trial region.
It subtracts the vCPUs of the configured `instance_type` and prints the quota and limit.
A failed read prints the AWS message and requests `--vcpus`. It creates no resources first.
The command removes duplicate sizes and preserves their order.
`--family NAME` selects another family. Its default is m7a from `instance_type`.
Attempts start as soon as both the vCPU limit and the reserve allow them.
An attempt waits for active attempts to end when the reserve cannot cover it.
It reports `not run` when no attempt remains and the reserve still cannot cover it.
An attempt larger than N reports `not run`.
Start with one small run:

```sh
~/.claude/scripts/lib/py scripts/aws_target/deploy.py gate-trial --step size --repo . --approve-resources --sizes 16 --runs 1
```

1. Read the ledger totals without writing them.
2. Print and bundle the exact commit at `--repo` HEAD.
3. Upload the bundle to a temporary private bucket.
4. Launch one Spot instance from the configured official NixOS image.
5. Prepare tools through `nix shell`.
6. Run all discovered test modules and the script type check through `run_suites` with `WholeMachine`.
7. Terminate each instance when its attempt ends.

The host reads the root `nixpkgs` input revision from `/etc/nixos/flake.lock` before it creates trial resources.
The guest uses that revision for Python, SQLite, Chromium, and the other Nix packages.
The guest links `google-chrome` to Chromium for headless chart tests.
The host reads and prints its basedpyright version at trial start.
The guest installs that version through `uv tool install basedpyright==<version>` and adds the installation directory to PATH.
The guest replaces the wheel's Node executable with a link to Nix Node from the tool shell.
The tool shell also includes `taplo`, `zsh`, `bash`, `gh`, and Git.
Root prepares `/bin/bash`, `/bin/zsh`, and Python at `/run/current-system/sw/bin/python3` through a temporary bind mount.
Before the bind mount, `cp -a` copies the system profile links and preserves their targets in `/nix/store`.
The guest runs as the unprivileged `gate-trial` user with its own home and runtime directory.
Its shell reads `/etc/profile` before tests start, then restores the tool PATH.
The exported NixOS environment marker prevents child zsh shells from replacing fixture paths.
Tool preparation time includes the Nix packages, the basedpyright installation, and the environment setup.
The change in preparation time remains to be measured.

The bundle excludes uncommitted changes.
The image has no box Tailscale identity or box idle service.
The instance uses no admission service, hold check, queue reservation, memory governor, CPU pool, systemd scope, or test ledger.
The `WholeMachine` mode uses the instance CPU count as its maximum parallel job count.
The host sends its recorded module times from `~/.local/state/merge-gate/module_seconds.json` as a separate S3 object.
`WholeMachine` uses `module_order` with these times, which places unknown modules first, then measured modules from slowest to fastest.
A missing or unreadable host file keeps the discovery order. The guest writes no module times back.
It records this value as `max_running_jobs` in JSON and text.
It limits each module to `gate_trial_module_limit_s` seconds, with a default of 600, and disables reruns.
The JSON report retains each module's status, time, and stopped flag.
The log prints the module name, exit status, and time as each job ends.
Both machine modes use one runner loop with the same module command, test environment, and process cleanup.
It runs basedpyright on `scripts` as one more job.
A failed suite remains a measured result.

The command calls `DescribeSubnets` for the configured VPC.
It tries the configured subnet first, then one public-address subnet in each other zone.
Each zone uses its current Spot price and the same reserve rule.
`InsufficientInstanceCapacity` advances to the next zone.
The report records each capacity refusal with its zone and AWS message.
Other AWS refusals end that attempt.
An attempt reports `no capacity` when every zone refuses for capacity.

The table shows the zone, five time intervals, request-to-ready time, dollars, counts, skips, verdict, failed modules, and log path.
The report gives medians and the cheapest size whose gates all pass within the configured target.
Each instance runs one gate, with no other trial gate on that instance.
An unfinished attempt prevents that size from qualifying.
Each gate result records peak machine memory in `memory_peak_bytes`.
The guest samples `MemTotal - MemAvailable` from `/proc/meminfo` every two seconds during the gate.
This value includes tmpfs memory and test processes in user scopes. A missing file gives no value.
Use this measurement before a later run selects a family with less memory.
Batch remains **not measured**.

#### Step B: start method, without a gate

```sh
~/.claude/scripts/lib/py scripts/aws_target/deploy.py gate-trial --step start --repo . --approve-resources --size 16
```

Replace `16` with the selected size. The config default is `--runs 5`.

1. Launch an On-Demand instance and measure tool readiness.
2. Stop that instance through the EC2 API.
3. Start that instance and measure tool readiness again.
4. Terminate that instance when its attempt ends.

Ready means the tools exist and the guest writes its marker to S3.
The guest creates its local `prepared` marker after the readiness upload succeeds.
Each boot runs user data directly through `amazon-init`.
The stopped start reuses the Nix store.
The cold launch and its stopped start hold one shared vCPU and cost reservation.
Other start attempts can run at the same time within the limits.
This step runs no gate, type check, warm pool, or Batch job.

#### Permissions and cleanup

1. Allow `GetServiceQuota` for the regional EC2 Standard Spot quota.
2. Allow instance launch, gate tags, state reads, stop, start, and termination.
3. Allow `DescribeSubnets` in the configured VPC.
4. Allow temporary private bucket creation, configuration, object access, and deletion.
5. Allow an outbound-only security group, `DescribeSecurityGroups`, and group deletion.
6. Allow current Spot price and public price reads.

The command uses the existing encrypted administrator credentials and `AwsCli`.
Only presigned object URLs reach the instance.
No instance role, `iam:PassRole`, inbound rule, or Tailscale credential is required.
The bucket has public access blocked and a one-day object lifetime.

User data schedules `shutdown -h +30` before other commands by default.
It runs the tool shell directly and creates no systemd unit.
The guest uploads `boot.log`, `gate.log`, `timeline.log`, and the current UTC time every `gate_trial_log_push_s` seconds.
Each timeline line records the UTC time, load, available memory, process count, and CPU busy percent since the previous sample.
CPU busy time excludes idle and iowait from the aggregate counters in `/proc/stat`.
The result records busy CPU seconds, gate seconds, vCPU count, and CPU use percent during the gate.
CPU use percent equals 100 times busy CPU seconds divided by vCPU count times gate seconds.
The text table shows CPU use percent for each size and run.
Each upload includes the current process snapshot, with at most 60 lines and 200 characters per line.
The snapshot excludes lines that contain `curl` and is not stored between uploads.
The default interval is 30 seconds. A failed upload does not stop later uploads.
The final log contains the same files and time. The reserve and reported S3 cost include periodic uploads.
A shell failure uploads the combined log and an error result through the image's curl.
The controller downloads logs for failed attempts in both steps.
A missing log object is harmless.

The [NixOS 26.05 module](https://github.com/NixOS/nixpkgs/blob/nixos-26.05/nixos/modules/virtualisation/amazon-init.nix)
defines a oneshot service under `multi-user.target`, without a first-boot condition.
Its PATH includes Nix, systemd, and `/run/current-system/sw`.
It executes shell user data through the runtime shell on each boot.
Source access date: 2026-11-06.

Guest shutdown terminates the instance and deletes its root volume.
Cleanup waits through `shutting-down` until the instance reaches `terminated`.
The ledger accepts `shutting-down` and adds no compute charge for that state.
The command first terminates stopping and stopped instances with the `GateTrial` tag key.
Cleanup finds active instances through the current run's tag.
A lost launch reply causes up to three calls with the same `ClientToken`.
If all replies remain lost, the command starts no more attempts.
Attempts already in progress continue to their end.
An incomplete cleanup also stops new attempts.
An attempt sweep uses its own tag and cannot terminate another active attempt.
The report names the lost reply and the guest shutdown timer as the remaining protection.
Ctrl-C, SIGTERM, and SIGHUP stop new attempts and cancel active waits.
The main thread handles these signals. Each worker terminates its instance.
The command joins all workers before final cleanup and the final report.
The command blocks these signals during launch calls, final cleanup, and the final report.
Cleanup finds the security group by its fixed name through `DescribeSecurityGroups`.
Cleanup removes a bucket or group after a lost creation reply.
Cleanup retries security group dependencies for a bounded time.
The final text and JSON name remaining resources, or report `Remaining: none`.
No expiry Lambda is required for this trial.

The command saves JSON, text, and failed-attempt logs under the existing AWS state directory.
The [SigV4 query specification](https://docs.aws.amazon.com/AmazonS3/latest/developerguide/sigv4-query-string-auth.html)
defines the object URL signatures. Source access date: 2026-11-06.

#### Cost and reserve

| Step | Config reserve | Cost before a live trial |
| --- | ---: | --- |
| A: size | $4 | **To measure**, including boot tools and failed attempts. |
| B: start | $3 | **To measure**, including cold and stopped starts. |
| Combined | $7 | Maximum step reserves; no Batch or concurrent burst. |

The shared caps remain $20 daily and $400 monthly.
The command stops if either cap has less room than the step reserve.
It reads prices for compute, volumes, addresses, and S3.
Each started instance accrues estimated cost from its request time.
The reserve includes spent money and the full projected lifetimes of all attempts in progress.
Each new attempt must fit within that reserve.
One lock protects the reservations, costs, request counts, remaining instances, and report rows.
Step B reserves two lifetimes.
The report names attempts that the reserve prevents.
AWS billing must confirm these estimates.

`config/aws_target.conf` holds all trial tunables, which the command reads at use.
Defaults are 30 minutes per lifetime, 7200 seconds per URL, and 420 seconds for the target gate.
The poll pause is 2 seconds. The security group delete wait is 120 seconds.
Apply the comparison rules after both reports exist.
Keep the larger comparison and twenty-gate burst in Phase 2.

### Phase 2: later method and concurrency comparison

Start this phase only after Steps A and B produce their reports.
Use their recommendation to limit implementation work. Obtain a separate trial budget before paid work.
Do not build five launch harnesses as a condition for the first answer.

1. Measure pool filling, stopped volume retention, replenishment, and depleted starts if stopped capacity wins Step B.
2. Qualify full gate coverage on Batch only if approved Step B timing justifies its smaller lifecycle code.
3. Measure twenty simultaneous full gates on the selected method and size, with one gate per instance.
4. Compare another method in a twenty-gate burst only if its measured benefit justifies that separate work and cost.
5. Record first, median, and last start and result, plus teardown and all charges.

The complete comparison table belongs to this later phase.
Its empty cells remain **to measure**. They do not block the small trial or show that an unmeasured method lost.

| Method | One gate: request to start | Twenty gates: request to start | Dollars per gate, including idle | Fixed monthly cost | Lost worker removal | Work and permissions |
| --- | --- | --- | --- | --- | --- | --- |
| Fresh Spot | Estimated 60–120 seconds; replace with Step A results. | To measure in the later burst. | Estimated $0.065 at 16 vCPUs; include failed attempts and fixed share. | Estimated $3 for shared images, artifacts, and cleanup; no retained worker. | Guest deadline, command finalizer, and production expiry Lambda. | Image, transfer, launch/result code, accounting; scoped EC2, S3, role attachment, production expiry setup. |
| Auto Scaling stopped pool, On-Demand | To measure; Step B supplies stopped-start evidence only. | To measure with twenty prepared instances and a depleted pool. | To measure On-Demand runtime, pool preparation, idle storage, and replenishment. | Estimated $3.20 per retained 40-GiB volume plus $3 shared: $6.20 for one, $67 for twenty. | Auto Scaling replaces unhealthy workers; gate deadlines and lifecycle hooks handle job completion. | Pool assignment, clean reset, hooks; group capacity, lifecycle, template, role, and S3 permissions. |
| Batch EC2 Spot to zero | To measure; optional Step B measures container start only. | To measure after gate compatibility and dedicated-instance placement. | To measure Spot runtime, image pull, failures, and scale-down idle. | Sourced $0 Batch service fee; estimated $3 shared after compute reaches zero. | Batch owns compute and job state; runtime timeout stops jobs; scaling removes idle workers later. | Full gate container and result adapter; Batch submit/read/terminate and setup roles/resources. |
| Fargate Spot | To measure only after process and memory compatibility. | To measure only after compatibility. | To measure vCPU, memory, image pull, network, and storage. | No running task charge without tasks; artifacts and logs remain. | ECS owns task infrastructure; application deadline stops hung tasks. | Supported scope adapter and coverage proof; ECS task, role, image, log, and S3 permissions. |
| EKS with node autoscaler | To measure only after a later Kubernetes requirement. | To measure with one gate per node. | To measure nodes, controller, idle, storage, and fixed allocation. | Sourced $0.10/hour; estimated $73/month plus controller and workers. | Auto Scaling replaces unhealthy nodes; autoscaler removes idle nodes; Job deadlines stop hung work. | Cluster, controller, Job adapter, node separation; EKS, Kubernetes, autoscaler, role, image, and S3 permissions. |

The pool storage estimate uses 40 GiB at an example $0.08/GiB-month, not a verified Ohio quote.
Auto Scaling and Batch have no additional service fee; their underlying resources remain billable.
See [EBS pricing](https://aws.amazon.com/ebs/pricing/), [Auto Scaling pricing](https://aws.amazon.com/autoscaling/pricing/), and [Batch pricing](https://aws.amazon.com/batch/pricing/), accessed 2026-11-06.
Fargate bills allocated resources through task termination, including image download.
See [Fargate pricing](https://aws.amazon.com/fargate/pricing/), accessed 2026-11-06.
Keep equal coverage, the seven-minute gate target, and the shared caps in every later comparison.
Keep EKS and Fargate outside implementation until their stated conditions change.

### Phase 3: queue delivery and production cleanup

Owners: **queue**, **sdlc devtools**, and **aws-unit**.

1. Add the backend call and the execution variant to the current queue record.
2. Import module logs, gate measurements, and buildlog records.
3. Install the production expiry service with separate permission.
4. Extend reservations to concurrent attempts.
5. Prove worker death, supervisor death, lost launch replies, interruption, and cap fallback.
6. Enable remote queue requests within the measured concurrency.

Retain the selected size, method, measurements, and image identity in the deployment record.
The user supplies any required quota authorization. The shared caps do not change.

### Phase 4: retain useful test reuse

Owner: **sdlc devtools**. **queue** carries the selected environment identity.

1. Add image-based remote pass certificates with real host evidence.
2. Preserve the local host-dependent module results in each combined certificate.
3. Share scheduling records and certified ancestors across disposable workers.
4. Compare cold full, repeated full, and affected gates.

This phase changes result reuse. Worker retention follows the selected method.
