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
The ready-image candidate contains NixOS, required store paths, Python, uv, Git, and shells.
It contains no job tree, host credential, session state, or Rust build output.
A launch template stores settings; it does not contain installed tools.
Containers require the same tool manifest with separately qualified host integration.
No candidate receives a production startup deadline from the estimates above.

## Launch, scale, and termination

The production route uses one fresh Spot instance for each merge gate or targeted test.
`config/cloud_gate.conf` sets the order to `m7a.8xlarge`, then `m7a.4xlarge`.
The route tries every deployed public zone at 32 vCPU before it tries 16 vCPU.
It never requests On-Demand capacity.
The build queue enforces nine cloud slots through `cloud_gate_slots` in `build_admission.conf`.
The route adds no AWS work queue or autoscaling group.

Each attempt uses one `RunInstances` request and a saved idempotency token.
An uncertain reply uses the same saved arguments and token after restart.
Each launch uses the recorded launch-template version, image, subnet, security group, role, and expiry.
The request uses one-time Spot, termination on interruption, termination on shutdown, and root deletion on termination.
No home volume survives the attempt.

The default lifetime is 20 minutes, including preparation and result delivery.
The guest sets an absolute systemd shutdown deadline.
One EventBridge schedule invokes an expiry Lambda each minute.
The function terminates only expired instances with this deployment's gate tags.
Normal completion requests termination and waits for confirmed termination.
A launch timeout permits local fallback.
An unconfirmed termination keeps charging until the expiry cleanup confirms termination.
An AWS launch refusal releases its cost reservation before local fallback.
An AWS outage can delay termination and its final charge.

| Event | Result |
| --- | --- |
| Complete result and final log | Validate the result and import its measurements. |
| Guest death, deadline, or Spot interruption without a final result | Use natedev. |
| Upload or launch timeout | Release the cloud slot and use natedev. |
| AWS launch refusal | Record the refusal and use natedev after capacity retries. |
| Natedev death | Recover the saved attempt; the guest and Lambda retain their deadlines. |
| No capacity at either size | Use the existing local runner. |

Nine 32-vCPU gates need 288 Standard Spot vCPUs before the Rust box or image work.
The user must request any required quota increase.
A quota does not guarantee free capacity in any zone.

## Exact tree delivery

The gate bucket holds source bundles, guest programs, request manifests, results, and logs.
The boot script downloads the guest program before Python starts.
Production also downloads the request that contains the module list.
User data carries neither the program nor the module paths.
Production downloads through S3 inside the tools shell, where `aws` is available.
The trial downloads its program and source bundle through signed HTTPS links.
Instances use HTTPS and temporary role credentials. They need no SSH or Tailscale connection.
Each public subnet has an internet gateway route. The gate security group has no inbound rule.

1. Record the clean checkout's commit and tree on natedev.
2. Select targeted modules on natedev with the existing affected-module rules.
3. Bundle HEAD and the branch ref with `--not refs/remotes/origin/main`.
4. Upload that bundle only when HEAD has commits outside `origin/main`.
5. Save the selected paths, tested commit, tree, natedev `origin/main` hash, clone URL, and expiry in the request.
6. Upload the guest program beside the request.
7. Launch the instance with download links and the recorded deployment settings.
8. Download the program and request before Python starts.
9. Clone the public HTTPS repository from `clone_url` in `cloud_gate.conf`.
10. Fetch the optional bundle and check out the requested commit.
11. Set `origin/main` to natedev's recorded hash and verify the exact tree.

The guest uses natedev's `origin/main` hash even when GitHub main has advanced.
The incremental bundle contains only commits outside that reference.
The guest sets the recorded origin URL after the clone.
The bundle contains no local Git configuration, hooks, credential helper, or untracked file.
The guest runs the module launcher from the tested tree.
The local route compares the returned attempt, commit, tree, module list, verdict, and host before completion.

## Test environment and equal meaning

The trial and production route import one guest implementation from `scripts/aws_target/gate_box.py`.
The guest uses the deployment host's locked nixpkgs revision and the configured public package list.
It runs as the unprivileged `gate-trial` user with a private home and user systemd manager.
Its temporary system profile exposes Python, Chromium, and the shell tools expected by existing fixtures.
The guest links only the tested tree's public configuration into its home.

The cloud gate runs tests only. It installs no basedpyright package and performs no type check.
The two tests that required the real checker have left this branch.
Nightly type checks remain local. An explicit `--type-dirs` request also runs its checker locally.

Production sends `SelectedModules`, including a certified empty selection.
The shared runner uses one job per CPU and the existing module launcher, timeouts, fake tools, and process cleanup.
Failed production modules receive the existing isolated rerun.
The trial retains its full-module comparison mode without reruns.
New modules enter through automatic discovery.
No module requires a permanent natedev exception in this route.
The earlier guest proof covered all 18,630 tests at each trial size.

## Result and queue record

Natedev retains each attempt under the checkout's Git `cloud-gates/<attempt>` directory.
The directory holds the bundle, request, deployment settings, launch arguments, instance identity, result, log, and import marker.
`current.json` identifies the recoverable attempt for the same commit, tree, mode, and timeout.
A completed retry uses a new identity and retains the previous evidence.

The guest pushes its log every five seconds by default.
The final log must arrive before a result can complete the gate.
The result contains final jobs, first failures, isolated reruns, timeouts, CPU use, memory peaks, and an explicit `gate_run` record.
The route validates the attempt, commit, tree, modules, host, and verdict.
Partial logs cannot complete a gate.
A dead instance without a final result permits local fallback.
A remote test failure remains red and never starts a local retry.

The route imports the explicit buildlog record once by its identity under the real remote host.
It uses `scripts/test_times/gate_record.py` for module measurements.
It adds no gate host to the build-target registry or peer sync service.
The synchronous caller receives the existing `GateRun` shape and exit behavior.

The queue phase will add cloud execution details and progress to `BumpGateRecord` and its displays.
This phase preserves that record and its existing notices.
The queue still owns admission, slot enforcement, cancellation display, and later progress display.

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

The route tries every configured zone at the preferred size before the smaller size.
Both sizes use Spot. The route never requests On-Demand capacity.
An interruption without a valid final result permits local fallback.
The saved log and cost evidence remain available for a later retry.
Only capacity refusal at both sizes permits capacity fallback to natedev.

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

The shared ledger keeps gate reservations separate from the Rust instance observation stream.
One cost lock covers each admission decision and reservation.
Both workloads use the existing $20 daily cap and $400 monthly cap from `aws_target.conf`.
Gate reservations also constrain Rust admission.

Each gate reserves the configured maximum hourly Spot price through its absolute expiry, plus the configured attempt allowance.
The defaults reserve `$0.8 × 20/60 + $0.02` before launch.
Reservations split across accounting days and months.
Confirmed termination or confirmed absence releases the unused portion.
An uncertain launch or termination keeps charging until reconciliation proves its end.
The existing safety reserve remains in every cap decision.

Both workloads use the existing Cost Explorer scope tag and one combined charge query.
The refresh uses the existing saved-reply recovery and query charge.
It remains active for gates when Rust offload is disabled.
Deployment records the shared gate resource price before it creates resources.
The gate route never starts the Rust box or enables Rust offload.

## Access and permission questions

Sessions make no AWS call.
The route starts a user systemd service with the scaler credential file and profile `build-scaler`.
The service removes inherited AWS key, profile, and config overrides.
The credential file stays on natedev. The guest receives only temporary instance-role credentials.
The user alone runs `deploy.py` with administrator access.

| Added scaler grant | Scope |
| --- | --- |
| `ec2:RunInstances` | Deployed image, launch template, security group, public subnets, instance profile, configured types, and Spot market. |
| `ec2:CreateTags` | Instances and volumes during `RunInstances`, with gate ownership and workload tags. |
| `iam:PassRole` | The gate role only, passed to EC2 only. |
| `ec2:TerminateInstances` | Instances with this deployment's gate ownership and workload tags. |
| `ec2:DescribeInstances` | Regional describe scope, required by EC2. |
| `ce:GetCostAndUsage` | The combined cost query; AWS requires wildcard resource scope. |
| `s3:PutObject` | The gate bucket's `source/*` and `requests/*` prefixes. |
| `s3:GetObject` | The gate bucket's `artifacts/*` prefix. |

Launch requests require attempt and expiry tags on instances and volumes.
The route pins the deployed launch-template version.
The guest role reads only source and request objects.
It writes only `artifacts/${aws:userid}/*`, where an instance session uses `<role-id>:<instance-id>`.
See [IAM policy variables](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_variables.html).
Workers receive no launch permission, scaler key, or Git credential.

The expiry role can describe instances and terminate only this deployment's gate instances.
It writes only its function's log streams.
The bucket lifecycle removes old artifacts. Runtime receives no bucket administration or object deletion grant.
Rust retains its existing resource policies and lifecycle settings.

## One-command setup and image updates

The user first deploys the existing Rust foundation stack with its configured inputs.
The user then runs:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
```

This command uses the existing administrator credential path.
It creates an UPDATE change set in that stack and preserves every Rust parameter.
It rejects a change set that changes a Rust resource.
It adds the gate bucket, guest role, instance profile, security group, launch template, scaler grants, expiry Lambda, schedule, and log group.
It records the pinned image, template version, public subnets, nixpkgs revision, and artifact role prefix locally.
It records the shared resource price before creation.
A repeated command reconciles the same resources.

This phase uses the configured NixOS base image and prepares tools at boot.
The prepared image belongs to plan phase 7.
That phase will cache only the package list and nixpkgs revision, independent of code changes.
It will use the machine name `cloud-claude-gate`.
No image build or queue display ships in this route phase.

`config/cloud_gate.conf` contains the new settings and their limits.
`config/aws_target.conf` remains the source for placement, credentials profile, accounting settings, and shared caps.

## Fallback and separation from Rust

The local runner handles cloud setup and infrastructure failures:

1. AWS is down.
2. The image or deployed stack is absent.
3. The shared daily or monthly cap prevents launch.
4. Both configured sizes lack Spot capacity in every deployed zone.

A remote test failure never permits local fallback.
Access denial, invalid results, lost logs, and dead instances permit local fallback.
A launch timeout releases its reservation when no instance identity is known.
The route never increases a cap or requests On-Demand capacity.

The workloads share deployment administration, scoped credentials, configuration, and accounting.
They keep separate instance identities, volumes, lifecycle state, and artifact paths.
The Rust box retains its persistent Spot request, retained home volume, and shutdown-to-stop behavior.
Gate cleanup terminates only the recorded disposable gate instance.
It never calls the Rust teardown path or registers a new build target.

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
6. Run all discovered test modules through `run_suites` with `WholeMachine` and no `type_dirs`.
7. Terminate each instance when its attempt ends.

The host reads the root `nixpkgs` input revision from `/etc/nixos/flake.lock` before it creates trial resources.
The guest uses that revision for Python, SQLite, Chromium, and the other Nix packages.
The guest links `google-chrome` to Chromium for headless chart tests.
The guest installs no basedpyright package. The two real-tool tests have left this branch.
The cloud gate matches natedev's `--tests-only` run and prepares no full type environment.
The tool shell also includes `taplo`, `zsh`, `bash`, `gh`, and Git.
Root prepares `/bin/bash`, `/bin/zsh`, and Python at `/run/current-system/sw/bin/python3` through a temporary bind mount.
Before the bind mount, `cp -a` copies the system profile links and preserves their targets in `/nix/store`.
The guest runs as the unprivileged `gate-trial` user with its own home and runtime directory.
Its shell reads `/etc/profile` before tests start, then restores the tool PATH.
The exported NixOS environment marker prevents child zsh shells from replacing fixture paths.
Tool preparation time includes the Nix packages and the environment setup.
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
The cloud gate runs tests only. The nightly run does the full type check.
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

The route phase supplies launch recovery, result import, shared reservations, and the independent expiry service.
The queue phase remains separate.

1. Add the cloud execution fields to the existing queue record.
2. Enforce the configured nine cloud slots.
3. Show module progress from the five-second log snapshots.
4. Connect queue cancellation to the recorded attempt.
5. Measure concurrency, capacity refusals, interruptions, and fallback use.

The user supplies any required quota increase. The shared spending caps stay unchanged.

### Phase 4: retain useful test reuse

Owner: **sdlc devtools**. **queue** carries the selected environment identity.

1. Add image-based remote pass certificates with real host evidence.
2. Preserve the local host-dependent module results in each combined certificate.
3. Share scheduling records and certified ancestors across disposable workers.
4. Compare cold full, repeated full, and affected gates.

This phase changes result reuse. Worker retention follows the selected method.

## Cloud gate resource names

The feature uses `scripts/aws_target/cloud_gate.py` and `config/cloud_gate.conf`.
The machines use the name `cloud-claude-gate`.
The instance `Name` tag and the resource `Workload` tags use `cloud-claude-gate`.
The instance profile has no CloudFormation tag property.

| Resource | Name |
| --- | --- |
| Bucket, role, profile, security group, launch template | `cloud-claude-gate-${DeploymentId}` |
| Expiry role, expiry function, schedule | `cloud-claude-gate-${DeploymentId}-expiry` |
| Log group | `/aws/lambda/cloud-claude-gate-${DeploymentId}-expiry` |

The box sends logs every `log_push_s=5` seconds.
The route reads module counters from each log download.
It writes the existing queue progress format through `progress_profile.publish_counter`.

The route admits with `CloudPlace("aws")` before it requests a box.
The route uses its own pid as the run identity.
The build queue releases the slot when the route ends.
The route passes the reservation sidecar path to the scaler service.
The service writes module progress and `CloudProgress` beside that path.
The cloud state records the phase, vCPU count, failed module count, and update time.
The phases are `requesting`, `setup`, `running`, and `uploading`.
The route releases cloud admission before local fallback requests memory.
The route requests no local memory for a cloud gate.

## Launch refusal measurements

The AWS state folder retains `cloud-gate-launches.jsonl`.
Each launch call appends its time, attempt, instance type, vCPUs, zone, result, error code, and reason.
Capacity refusals first try the remaining zones and sizes.
Every cloud setup or infrastructure failure permits local fallback.
Only a real remote test failure remains red.
The route releases its cost reservation and cloud slot before local admission.
A refusal prints `Cloud gate refused: <type> in <zone>: <code>`.
Fallback prints `Cloud gate uses natedev: <reason>`.
These lines contain no account, instance, or subnet identifier.
Upload errors, timeouts, missing results, and invalid results also permit local fallback.
