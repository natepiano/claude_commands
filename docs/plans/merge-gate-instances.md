# One EC2 instance for each merge gate

Recommend a size trial before selecting a Spot instance type in `us-east-2c`.
Use a ready NixOS image and a launch template. Terminate the instance after its gate.
Estimate 60–120 seconds from launch request to tests, when Spot capacity exists.
Target 420 seconds per gate, alone on its instance. Do not select a size from CPU arithmetic.
Test eight, sixteen, thirty-two, and sixty-four vCPUs three times each, with no overlapping trial gates.
Estimate $1.49 for the trial day, including image preparation; reserve at most $7 within the existing caps.
Select the cheapest measured cost per gate with a median wall time of 420 seconds or less.
Natedev takes about 1,030 seconds alone and 2,660 seconds under the supplied morning load.
Keep the Rust box separate. Share its $20 daily and $400 monthly caps; keep queue and publishing work on natedev.

## Status and evidence

This document is a design. The commands and record changes marked **proposed** do not exist yet.
The source references describe this checkout. The build phases identify the required new work.
No deployment, host connection, credential read, or state-directory read formed part of this design.

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

## Size, speed, and price

The target is seven minutes, or 420 seconds, for one gate alone on its instance.
The trial measures that target. It does not treat it as a prediction.
The supplied CPU seconds came from gates with contention.
CPU work, scheduling, and memory waits can differ on an idle instance.
Therefore, dividing 6,600 CPU seconds by the vCPU count does not establish an idle-instance time bound.

The predictions below only choose trial sizes.
They use `250 + 6350 / vCPU` seconds, rounded upward.
The 250-second serial allowance is an estimate, not a measured stage duration.
Each price calculation adds 120 estimated seconds for startup and result delivery.
The calculation treats that whole allowance as billed time, which overstates any unbilled allocation wait.

| Instance | vCPU, sourced | GiB, sourced | On-demand $/hour, sourced | Spot $/hour, sourced | Tests seconds, estimated | On-demand compute $/gate, estimated | Spot compute $/gate, estimated | Spot total $/gate, estimated |
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

Prices and specifications come from the [Ohio price table](https://instances.usage.ai/aws/ec2/region/us-east-2), accessed 2026-11-06.
This secondary source gives indicative regional Spot prices. It does not establish the current `us-east-2c` price or capacity.
The trial records the actual zone price through the user-run deployment command.
On-demand prices provide a comparison only. The design uses Spot.

Choice: measure four sizes before selecting one; risk: the gate's serial stages can prevent every size from meeting the target.

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
The first build phase defines the repeated size trial and its selection rule.

## Split and owners

The remote boundary is `merge_gate.run_suites`, at `scripts/production/merge_gate.py:918`.
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

Choice: move only test execution; risk: host-dependent tests still require a short local stage.

## Startup

The selected path uses a ready image **and** a launch template.
A template stores launch settings. It does not contain installed tools.
The image contains NixOS, the required store paths, Python, Node, basedpyright, uv, Git, shells, and the pinned type environment.
It contains no job tree, host credential, session state, or Rust build output.

All times in this table are estimates. No EC2 startup measurement was available.

| Path | Request and boot | Tools, tree, and service | First test | Decision and risk |
| --- | ---: | ---: | ---: | --- |
| Ready image plus launch template | 35–80 s | 25–40 s | 60–120 s | Selected; Spot allocation and snapshot reads can take longer. |
| Stopped ready instances | 20–50 s | 20–40 s | 40–90 s | Rejected; idle volumes cost money, and stopped Spot reuse complicates ownership. |
| Launch template with an unprepared image | 35–80 s | 180–900 s | 215–980 s | Rejected; package installation dominates startup. |

These estimates assume available capacity and a small source bundle.
They are not a launch deadline or an AWS promise.
The warm-pool estimate describes a comparison, not an alternative Spot policy.
Use neither a warm pool nor snapshot acceleration in the first implementation.

Choice: install tools before launch; risk: an image update takes longer than a gate launch.

## Launch, scale, and termination

Use one launch template for all gates.
Use one ordinary `RunInstances` request per attempt, with one instance and an idempotency token.
Use one-time Spot requests with termination on interruption.
Set guest shutdown behavior to terminate.
Set the root EBS volume to delete on termination.
Create no retained home volume for a gate.

Twenty ready requests can issue twenty launches concurrently.
No shared worker, autoscaling group, or AWS work queue sits between them.
The existing queue remains the only work queue.
The twenty-instance limit is a proposed configuration value, not a new local admission rule.
AWS refusal returns the affected request to natedev.

The sequential trial needs up to sixty-four Spot vCPUs for its gate.
The existing sixteen-vCPU Rust box raises that need to at least eighty in the same regional Standard quota.
Twenty concurrent gates need `20 × winning vCPU count + 16` when the Rust box also runs.
That is 336 vCPUs for sixteen-vCPU workers, or 1,296 for sixty-four-vCPU workers.
Image construction can require additional capacity.
AWS lists a default Standard Spot quota of five vCPUs.
See [Spot quotas](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-spot-limits.html), accessed 2026-11-06.
The current account quota is unknown. Regional quota does not guarantee capacity in zone 2c.

Each launch record contains its absolute expiry time before the launch request.
Use a sixty-minute lifetime for each trial attempt, as an estimated operational limit.
It includes startup, tests, upload, and termination delay.
An unusually slow valid gate can exceed this limit and require a local retry.
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

The Lambda function and schedule require the user's permission listed below.
The one-command trial includes independent cleanup after that permission.
This dependency is necessary to satisfy the requested cleanup after natedev failure.

Choice: terminate each gate instance; risk: an interrupted attempt loses all local progress.

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
The startup table includes this allowance.

Choice: use object storage for both directions; risk: large logs or history increase transfer time.

## Test environment and equal meaning

Use a non-root Linux user with a real passwd home, a user systemd manager, and cgroup v2.
Give it an empty private home and the public configuration from the tested tree.
Keep the same Python and basedpyright versions as natedev.
Use the same pinned Nix packages for the remaining tools.
Do not copy natedev's private home configuration.

| Requirement found in code | File and line | Instance treatment or local work |
| --- | --- | --- |
| User scopes and `builds.slice` | `scripts/build_queue/queue_place.py:105`, `:122`, `:290` | Provide the user manager and slice. Keep real scope accounting. Do not set `BUILDLOG_SCOPE=0` for the gate. |
| Fixed gate memory reservation | `scripts/production/merge_gate.py:337` | Run the existing admission service and reservation against the instance. Keep the 6-GiB sampled cap. |
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

Choice: retain host-dependent coverage locally; risk: the local stage limits the best possible gate time.

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

Choice: use the current result record; risk: its recovery logic needs an EC2 attempt identity as well as a local PID.

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

Every size-trial run uses `--full` and no pass from another host or earlier attempt.
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

Choice: preserve result and scheduling reuse, not build folders; risk: the certificate-key change requires careful gate-owner review.

## Spot behavior

Spot is the required purchase mode.
The on-demand column only shows the cost comparison.
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
No NAT gateway, load balancer, retained gate volume, or paid snapshot acceleration is part of this design.

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
The **queue** owner supplies `N` from its existing records during the trial.
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

Choice: extend one cost authority; risk: this is a required ledger change, not an existing multi-instance feature.

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
The following questions request each additional runtime permission.
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

Does Ohio have eighty Standard Spot vCPUs available for the sequential trial while the Rust box runs?
Can its quota support `20 × winning vCPU count + 16` before fleet routing starts?
Only the user can authorize an increase if the current quota is lower.
No new notification, manual approval per gate, or resource check unrelated to this request is proposed.

Choice: extend the existing machine credential with narrow runtime permissions; risk: the queue daemon can launch paid resources within those permissions.

## One-command setup and image updates

Extend the existing staged `deploy.py up` command with a separate merge-gate workload.
The current stage runner appears at `scripts/aws_target/deploy.py:916`.
The **proposed** user command is:

`python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources`

This command uses the user's existing deployment inputs and administrator access.
It creates or reconciles the image, bucket, worker role, launch template, expiry service, and scoped queue policy.
It records priced inventory before the first charged resource.
It installs the local queue integration after the size trial selects a winner.
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

Choice: use the existing user-run setup workflow; risk: a tool change needs an image build before remote routing resumes.

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

Choice: preserve local fallback and the Rust lifecycle; risk: fallback returns the gate to natedev's measured load delays.

## Phased build order

### Phase 1: one command, a sequential size trial

Owners: **aws-unit** and **sdlc devtools**. The **queue** owner does not enable fleet routing yet.

The **proposed** command is:

`python3 scripts/aws_target/deploy.py gate-trial --repo . --approve-resources`

The same command prepares the image and trial resources when they do not exist.
It records one exact commit, tree, image, tool manifest, suite set, and memory cap for the whole trial.
It uses the four instance types listed in the prediction table.
It launches three fresh instances per type, for twelve attempts in total.
Only one trial instance runs at a time.
Each instance runs one full gate and then terminates.

Three runs give a median and an observed range without a large trial bill.
They reduce the effect of one unusually fast or slow run.
They do not establish a reliable tail-latency estimate.
Rotate through all four sizes once per round, for three rounds.
This reduces the effect of testing one size only during one period of Spot conditions.
Use the same runner defaults and 6-GiB cap throughout.
Start the next trial instance only after the previous instance reaches confirmed termination.

1. Reserve the trial cost within the existing shared daily and monthly caps.
2. Prepare the image, approved expiry service, and priced launch resources through the user-run deployment path.
3. Run one full natedev baseline for coverage and timing comparison.
4. Run the twelve sequential full-gate attempts on the recorded tree.
5. Import every result and its logs.
6. Confirm termination for every launched instance.
7. Print the trial table, total cost, and winner or unmet target.

Keep every attempt's wall time, failure, rerun, and interruption in the saved report.
Do not replace an interrupted attempt automatically during this trial.
Report unavailable capacity as missing trial data, not a zero-second gate.
A size needs three full passing runs to enter the winner selection.
The existing coverage comparison includes the required local modules before a run counts as passing.

The trial prints one table with these columns:

| Instance type | vCPU | Memory GiB | Wall seconds | Seconds to start | Peak CPU | Peak memory GiB | Spot $/hour | Dollars per gate | Gates/day under $20 cap |
| --- | ---: | ---: | --- | --- | --- | --- | --- | --- | --- |
| `c7a.2xlarge` | 8 | 16 | Three times; median and range | Median and range | Maximum used vCPUs | Maximum RSS plus swap | Observed range | Mean priced cost | Calculated remaining capacity |
| `c7a.4xlarge` | 16 | 32 | Three times; median and range | Median and range | Maximum used vCPUs | Maximum RSS plus swap | Observed range | Mean priced cost | Calculated remaining capacity |
| `c7a.8xlarge` | 32 | 64 | Three times; median and range | Median and range | Maximum used vCPUs | Maximum RSS plus swap | Observed range | Mean priced cost | Calculated remaining capacity |
| `c7a.16xlarge` | 64 | 128 | Three times; median and range | Median and range | Maximum used vCPUs | Maximum RSS plus swap | Observed range | Mean priced cost | Calculated remaining capacity |

The table above defines the output format. It contains no measured trial results.
Wall time starts when the worker enters `run_suites` and ends when it returns its final verdict.
It includes types, module waits, and isolated reruns. It excludes instance startup and artifact delivery.
The 420-second target applies to that wall time.
The required local modules must also pass. Report their time and the complete queue wait separately.
Startup time runs from the launch request to the first test or type job.
Peak CPU is the largest one-second CPU delta for the gate's cgroup, expressed as used vCPUs.
One used vCPU means one CPU second in that interval.
The trial adds this peak measurement. Existing CPU-second totals do not measure a peak.
The **sdlc devtools** owner adds the sample to the trial result without replacing the existing CPU totals.
Peak memory uses the gate's existing RSS-plus-swap sampling.
CPU peaks and elapsed times measure actual execution; dollars use measured resources and observed prices until billing reconciliation arrives.

For each size, calculate mean variable cost across its three attempts, including failed-attempt charges.
Include compute, EBS, IPv4, requests, and transfer in dollars per gate.
Report image preparation and shared fixed costs in the trial total.
Calculate gates/day as `floor((20 - other committed daily spend - shared daily fixed cost) / mean variable cost per gate)`.
Other committed spend includes the Rust box and outstanding reservations.
The monthly cap can reduce that daily capacity further.

Select the lowest mean cost per gate among sizes with three passing runs and a median wall time at most 420 seconds.
Report the full range beside the median. Three runs do not prove that every future gate meets the target.
Use the smaller vCPU count when measured costs tie.
Do not select a size from the prediction table.

If no size meets 420 seconds, report **no winner** and leave queue routing on natedev.
Do not raise the time target, select the fastest failure, or silently add another size.
Report the type-job duration, longest module, isolated-rerun time, and memory-admission wait for every size.
The longest serial module, full-tree basedpyright, or memory reservation can limit the wall time despite idle CPUs.
The measured critical path identifies which limit applies; the CPU arithmetic cannot identify it.
The **sdlc devtools** owner uses that profile for any later gate change.

#### Trial cost and cleanup

The planning estimate for twelve gate attempts is `3 × (0.052 + 0.065 + 0.065 + 0.110)`, or $0.876.
Allow $0.50 for one bounded image-preparation run and $0.10 for the trial day's shared resources.
Allow $0.01 for a reconciliation query if the existing schedule needs one.
The estimated total is **$1.486, rounded to $1.49**, before any applicable taxes.
This estimate includes the first image preparation. An existing matching image removes that preparation charge.
Later snapshot and artifact retention remain part of the shared monthly costs described above.

Reserve **at most $7 for the trial**, including setup, within the existing $20 daily and $400 monthly limits.
For the cost envelope, assume twelve one-hour attempts at up to 125% of the listed Spot rates.
That gives $5.49 compute, about $0.18 variable extras, $0.50 preparation, $0.10 shared costs, and $0.01 query cost.
The resulting $6.28 envelope leaves about $0.72 for termination delay and price rounding.
The 125% factor describes this estimate. It does not change a configured maximum price.
The user-run preparation prices the actual configured maximum and all reserved resources before launch.
If that reservation exceeds $7 or the remaining shared caps, report an incomplete trial without starting another instance.
Keep completed measurements so the same command can resume unfinished attempts within a later accounting day's remaining budget.
Do not raise either cap or repeat completed attempts automatically.

Use the same cleanup for pass, red, interruption, cancellation, or controller failure.
The guest deadline and approved expiry function cover every trial instance and the image-preparation instance.
The command terminates its current instance in finalization and retains its reservation until termination is confirmed.
Its report names any termination that remains unconfirmed; it must not report complete cleanup for that instance.
Report image preparation separately from request-to-test startup.
Do not describe a fake API test as an EC2 timing measurement.

Choice: select from measured time and cost; risk: a small trial cannot predict all future gates or Spot interruptions.

### Phase 2: queue delivery and independent cleanup

Owners: **queue**, **sdlc devtools**, and **aws-unit** within the split above.

1. Add the backend call and the execution variant to the current queue record.
2. Import module logs, gate measurements, and buildlog records.
3. Extend the existing trial cleanup and launch reservations to concurrent attempts.
4. Prove worker death, supervisor death, lost launch replies, interruption, and cap fallback.
5. Enable one remote queue request at a time.

### Phase 3: twenty independent gates

Owners: **queue** for dispatch and **aws-unit** for instances and accounting.

1. Use the type selected by the completed size trial.
2. Retain its measured time, cost, and image identity in the deployment record.
3. Raise the configured remote concurrency through two, five, ten, and twenty gates.
4. Confirm separate results and termination for every attempt.
5. Report today's actual gate count and combined AWS spend.

The user supplies any required quota authorization. The caps do not change.

### Phase 4: retain useful test reuse

Owner: **sdlc devtools**. **queue** carries the selected environment identity.

1. Add image-based remote pass certificates with real host evidence.
2. Preserve the local host-dependent module results in each combined certificate.
3. Share scheduling records and certified ancestors across disposable workers.
4. Compare cold full, repeated full, and affected gates.

This phase changes test-result reuse only. It introduces no retained build cache or stopped worker pool.
