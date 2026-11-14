# AWS cloud gates and trials

## Current model

AWS runs temporary Spot boxes for merge gates, paired gate trials, and the GPU size trial.
Normal build jobs use `mac` and `natedev`, in that order.
AWS has no permanent builder or build offload route.
Cloud gates use `~/.local/state/build-targets/aws` without an `aws` target registration.
The configured regions are `us-east-2`, `us-west-2`, `eu-north-1`, and `eu-south-2`.
The saved deployment record identifies the regions available for launch.
Boxes boot the official NixOS image and fetch their tools at boot.
GPU trial boxes then import the prepared GPU system and reboot.

`config/aws_target.conf` supplies the account spending caps and billing settings.
`config/cloud_gate.conf` supplies launch, expiry, trial, and artifact settings.
The ledger reserves spending before launch and records confirmed termination.
The configuration files remain the source for cap values.

`report.py` reads the AWS state folder without target registration.
Its AWS section retains the account bill, daily gate spend, and historical builder comparisons.
`sync.hosts.aws=aws-build` identifies historical steps.
The report shows no synchronization status for the removed builder.

One resolver selects the scaler credential file for deployment, services, key creation, and teardown.
It uses a nonempty `AWS_SHARED_CREDENTIALS_FILE` override before `credential_file` from `cloud_gate.conf`.
It expands `~` in either path.
Deployment uses encrypted administrator credentials for resource changes.
Gate services use the private scaler profile.
Boxes receive signed URLs and no scaler credential file.

## Hana guest safety files

`scripts/aws_target/hana_guest.py` renders safety files for the planned kept Hana box.
These files do not provision a machine or add a deployment command.
Phase D must install and enable the units before the box accepts work.
`config/hana_aws.conf` keeps placement disabled and supplies all safety timing and resource values.

The boot guard waits for a fresh permit within `guard_grace_seconds` from boot.
It has no ordering or requirement dependency on the data disk, network, or tool setup.
Tool setup runs after the guard and requires the bounded data mount.
A failed setup or missing disk cannot prevent the guard from requesting poweroff.
The mount bounds both device discovery and mount execution through `mount_timeout_seconds`.

Phase D must deliver `/run/hana-aws/work-permit.json` and `/run/hana-aws/generation` as private, root-owned files.
The permit fields are `expires_at` (UTC epoch seconds), `generation`, and `boot_id`.
The boot ID must match `/proc/sys/kernel/random/boot_id`.
The guard and watchdog only read the permit and identity files.
Missing, unreadable, malformed, expired, or mismatched permits grant no permission.
`permit_max_seconds=1800` bounds one renewal window.
A permit beyond that window grants no permission.
Phase C reserves permission for longer steps.
Phase D must deliver renewals through `write_permit()`.
The writer uses a private temporary file and atomic rename.

On the first read, safety records the permit's boot-clock deadline in `/run/hana-aws/permit-deadline.json`.
An unchanged permit retains that deadline across later reads and service restarts.
A renewal with a changed expiry records a new bounded deadline when safety next reads it.
The watchdog stops at the earlier of wall-clock expiry and the boot-clock deadline.
A backward wall-clock change cannot extend the boot-clock deadline.

Safety, permit renewal, and command admission share `/run/hana-aws/permit.lock` through `fcntl.flock`.
Safety holds the lock from the permit read through the shutdown decision.
Before killing work, safety writes `/run/hana-aws/shutdown.json` under the lock.
This fence refuses later renewals and command admission for that boot.

The independent watchdog starts after the guard and reads no controller or command state.
At expiry, it sends SIGKILL to all command cgroups under `hana-work.slice`, then requests poweroff.
A failed kill request still triggers the poweroff request.
The watchdog checks at `watchdog_check_seconds` intervals and shortens its next interval to the permit deadline.
Systemd also requests poweroff if a safety service fails.

`admit_command()` requires enabled configuration, a fresh bounded permit, and a mounted data disk.
It checks the shutdown fence and starts the command under the shared lock.
Its start callback spawns the process and returns immediately.
The caller waits for command completion outside the lock.
It prepares an unprivileged command with a separate slice under `hana-work.slice`.
Both the parent slice and each command enforce `step_cpu_limit` and `step_memory_gib` from configuration.
The initial limits are 4 vCPUs and 24 GiB RAM.
Phase F2 must use this command path after checking the actual data mount.
`test_hana_guest.py` tests the decisions with injected clocks and verifies rendered units when `systemd-analyze` exists.
No safety test needs AWS or a running systemd manager.

## Hana spending permits

`scripts/aws_target/hana_spend.py` reserves spending before it returns a guest permit.
`HanaSpending` uses the account ledger lock for starts, renewals, lifecycle observations, and storage deletion.
It makes no AWS call and delivers no permit to a guest.
Phase D must deliver permits and schedule lifecycle reconciliation without an active caller.
Placement remains disabled.

`reserve_storage()` reserves the root and data volumes before provisioning.
It checks current account caps and the Hana budget before returning a saved reservation.
`issue()` reserves compute through the deadline at the configured bid ceiling.
It includes retained storage when no storage reservation exists.
Each issued permit has an increasing generation.
A renewal must name the current generation and extend an unexpired deadline.
A refused renewal leaves the saved deadline unchanged.
The guest watchdog then stops the box when its existing permit expires.

The shared ledger checks account policy, account evidence, gate exposure, bills, and the existing daily and monthly caps.
The account daily limit remains USD 35.
The Hana daily budget includes Hana compute and storage within that account limit.
Open compute exposure remains reserved after permit expiry until a lifecycle observation confirms `stopped` or `terminated`.
Storage remains reserved after stop and termination until volume deletion is confirmed.
Storage estimates use a 30-day month.
The ledger reserves the remaining storage cost for every day that a permit reaches.
`reconcile()` records an automatic restart even when Hana placement is disabled.
It reserves the boot guard interval and retains exposure until a confirmed stop.
Unknown lifecycle state releases no exposure.
Each lifecycle observation names the generation that the observer read before its request.
An observation for an older generation changes no exposure or observation time.
The ledger saves `hana_lifecycle_fence` with the latest accepted observation or permit issue time.
An older observation cannot close a newer permit or replace a later running observation.
The fence survives controller restarts.
Observations at the same time as the fence leave exposure unchanged.

The existing `cost-ledger.json` version remains 1.
The optional `hana_compute` and `hana_storage` collections hold Hana exposure records.
Older records have no Hana exposure when these collections are absent.
A missing `hana_lifecycle_fence` remains absent until a fresh observation arrives.
The first stop confirmation saves the missing fence and retains the compute exposure.
A newer confirmation for the same generation can then close that exposure without a running observation.
Missing fields within a Hana exposure record make that record unusable.
The reader never invents a zero price or deadline.
Account guards and gate reservations include Hana exposure.
Ledger estimates include accrued Hana compute and storage.

| New setting | Value | Reason |
| --- | --- | --- |
| `region` | `us-east-2` | Use Ohio for the box and retained storage. |
| `instance_type` | `m7a.2xlarge` | Provide 32 GiB of memory. |
| `spot_max_usd_per_hour` | `0.15` | Bound reserved compute exposure. |
| `root_gib` | `40` | Reserve the retained root volume. |
| `data_gib` | `300` | Reserve the retained data volume. |
| `daily_budget_usd` | `6` | Bound Hana spending within the account limit. |
| `gp3_usd_per_gib_month` | `0.08` | Price standard gp3 storage without extra performance. |

The account configuration has storage totals, but no shared gp3 price per GiB.
The new rate therefore belongs to the Hana configuration.
`test_hana_spend.py` uses private files and fake lifecycle observations.

## Hana lifecycle controller

`aws_target.py serve` supervises bounded `tick` child processes for the existing `aws-target.service`.
Each child attempts Hana lifecycle recovery before account charge refresh.
Either operation can fail without suppressing the other operation.
The supervisor continues after a failed or timed-out child.
It releases all child-owned locks when the child exits.

`hana_box.HanaController` stores its journal in `hana/lifecycle.json` beneath the account directory.
`controller_lock.py` holds the existing `controller.lock` implementation.
`aws_target.control_lock` remains available through the same import.
Claims, lifecycle decisions, and operation intents take that lock before any ledger lock.
The controller releases both locks before each provider call.
It checks the saved deployment identity and operation token before applying the response.
Each tick performs at most one provider step.

The journal stores these lifecycle states:

| State | Meaning |
| --- | --- |
| Unprovisioned | No setup has registered a deployment. |
| Idle-stopped | A deliberate idle stop permits a later demand start. |
| Interrupted | Only EC2 can restart the interrupted Spot instance. |
| Stopped-unknown | An unknown stop cause forbids a manual restart. |
| Starting | A committed claim owns the unresolved demand start. |
| Ready | A verified boot can accept a permit for its current claim. |
| Stopping | A saved stop intent blocks new claims. |
| Replacement-pending | A terminated instance needs the later replacement path. |

The journal records deployment, instance, Spot request, operation, boot, generation, and execution identities.
A claim records whether work is committed incoming work or active work.
The initial controller allows one claim.
A caller timeout retains an unresolved start claim.
The controller refuses claim release while a start or provider operation remains unresolved.
Idle stop and claim creation use the same lock.
A committed claim prevents idle stop.
A saved idle-stop intent refuses a later claim until stop confirmation.
A confirmed interruption takes priority over a pending idle stop.
The controller records `INTERRUPTED` and refuses a manual restart.

Phase C reserves compute before the next boot identity exists through `reserve_start()`.
`check_start()` rechecks current caps before bootstrap, start, and permit publication.
A cap refusal preserves the pending mutation and its token until the provider confirms the outcome.
The controller calls the read-only `HanaProvider.recover()` when spending refuses another mutation attempt.
Recovery continues after a cap refusal or reservation expiry.
`MutationUnresolved` means the original request can still complete.
Its stopped observation cannot close the compute exposure or replace the pending mutation.
`MutationSettled` confirms that the original request cannot produce further changes.
A settled stopped observation can close exposure and permit claim release.
An unknown stop cause still forbids a manual restart.
Recovery records exposure for a running box before it schedules a stop.
Recovery never starts the box or publishes a permit.
Each bootstrap, start, or publication attempt still needs accepted spending.
`bind_start()` checks spending after the provider verifies the boot identity.
A publication includes the expected prior generation and the current execution identity.
Command permission requires a current active claim, verified boot, acknowledged permit, and accepted spending.
Renewal retains the execution identity and advances the permit generation.
A refused or lost publication does not extend the guest deadline.
The controller reserves exposure until stop confirmation through the Phase C ledger.
It reconciles automatic restarts without a caller, including while placement is disabled.
An automatic restart cannot reuse an interrupted execution's boot challenge.

`HanaProvider.step()` defines the typed provider boundary.
A provider must bound its calls and use the saved operation tokens.
Its `recover()` method reads status without repeating a mutation.
It must distinguish a completed request from an unresolved request before reporting `MutationSettled`.
The provider refreshes bootstrap access before a demand start.
It must verify root receiver responses against deployment and demand-start identities.
It must install a renewed generation and permit together before acknowledging publication.
Phase D2 supplies the concrete AWS and root receiver transport with approved setup.
The D1 default refuses provider calls for a registered deployment until that transport exists.
An unprovisioned tick makes no provider call.
D1 adds no setup command, resource, target registration, or placement activation.

| New setting | Value | Reason |
| --- | --- | --- |
| `idle_stop_seconds` | `600` | Retain short bursts before a deliberate idle stop. |
| `start_timeout_seconds` | `120` | Bound caller waiting while retaining unresolved claims. |
| `tick_interval_s` | `5` | Reconcile before the independent boot guard expires. |
| `controller_tick_timeout_s` | `45` | Bound each supervisor child and failed provider step. |

The controller validates these intervals against the guest guard and permit windows.
Renewal starts with enough margin for two bounded steps and three tick intervals.
Tests use private journals, injected clocks, and a fake provider.
They verify that neither the controller lock nor the ledger lock remains held during provider work.
Regression tests cover expired bootstrap, start, and publication recovery.
They also cover stale recovery responses, concurrent cap refusals, and interruptions during idle stops.

## Removed builder: 2026-11-04

`builder-removal.json` records the removal on 2026-11-04.
The persistent `aws-build` instance, Tailscale device, and data volumes were removed.
`routing.json` records `enabled: false`.
The target registry contains `mac,natedev`.
The builder configuration and offload route have been removed.
Builder slots, warm mirrors, idle shutdown, and queue-based starts are historical features.
Only historical cost, ownership, and teardown compatibility remain.

`deploy.py down --approve-destroy` can remove the remaining gate resources without registering the former target.
Teardown resolves former builder claim and lock paths directly from the AWS state folder.
It preserves the existing lock and live claim checks.
The builder controller and remote agent have been removed.
Teardown still checks retained claims before deletion.

## Paired speed trials

Run `python3 -m scripts.aws_target.gate_trial --base <ref> --changed <ref> --modules <folder>/<file.py> --rounds 3`.
Both refs can name local commits in `~/.claude`.
The trial pushes each commit to a fresh `gate-trial-<expires>-<id>-base|changed` branch with a plain push.
The expires field records the trial's fixed expiry epoch in seconds.
The trial folder's UUID supplies the id; `trial_branch_prefix` configures the prefix.
At trial end, cleanup lists the trial's remote branch namespace and deletes each branch, including after errors and Ctrl-C.
The trial installs no signal handler.
Ctrl-C and terminal command exceptions stop the paired service.
SIGTERM or SIGHUP to the terminal can skip branch cleanup.
The separate trial service then runs to its normal end and cleans its boxes.
The paired unit uses `ExecStopPost` with `gate_trial --paired-cleanup <folder>`.
After a service stop, systemd runs this cleanup after killing the coordinator and its children.
Cleanup requests termination for all recorded boxes before searching uncertain launches.
Cleanup requests termination for each discovered box before the next search.
Cleanup checks final states after these requests.
Cleanup uses one deadline from `termination_timeout_s` for all boxes.
Each API call uses the time that remains before this deadline.
The service stop timeout adds twice `api_timeout_s` to this deadline duration.
Cleanup records confirmed termination in the cost ledger.
Confirmed termination is final for every later ledger outcome.
Later running, absent, or terminated updates cannot change its end time or cost.
A worker rejects an ended booking before checking for a launch file.
The ledger rejects reservations for closed attempt IDs.
A retry uses a new attempt ID.
Cleanup resolves reservations without saved launch arguments as `GateAbsent`.
The module and coordinator also call this cleanup from `finally` blocks.
Repeated cleanup does not change resolved reservations.
AWS failures leave reservations unresolved for later recovery.
Before reservation, the coordinator cleans expired earlier trials with unresolved reservations.
The sweep checks each attempt's owner and passes only due attempts to cleanup.
The sweep preserves boxes with live owners, even when another owner in the same expired trial has ended.
The sweep preserves trial folders.
Guest expiry and the AWS expiry function remain independent termination controls.
AWS failures can delay termination beyond the configured expiry and confirmation timeout.
Merge gate units also use `ExecStopPost` with `cloud_gate --cleanup`.
Trials and merge gates use the same attempt cleanup code in `cloud_gate.py`.
The worker installs no signal handler.
The ledger records each worker's PID, process start identity, and attempt folder.
Reconciliation preserves live workers and workers whose identity cannot be read.
Reconciliation terminates expired attempts and attempts whose recorded worker has ended.
Each worker reconciles before reusing a saved launch.
A hard kill without a later start can leave the booking open.
The ledger continues to estimate its cost for the spending cap.
The AWS expiry function ends the box.
The next gate or trial start closes the booking after confirmation.
Before new pushes, a start sweep deletes parsed trial branches whose expiry is earlier than the current time.
The sweep reads no current lifetime setting.
A lower lifetime setting leaves existing trials' fixed expiries unchanged.
The sweep preserves unexpired branches, malformed names, and unrelated branches.
A sweep failure prints one stderr line and the trial continues.
This cleanup covers a push accepted remotely before the client reports an error.
Each network Git call uses `git_network_timeout_s`, which defaults to 60 seconds.
A timeout or cancellation kills the Git process group, including SSH children.
A cleanup failure prints one stderr line for the trial or branch and preserves the trial's exit code.
The pushes use the invoking terminal's Git credentials.
Each module uses one disposable gate box.
All boxes launch after the full trial obtains its cloud slots and cost reservations.
The trial refuses costs above the daily or monthly room.
It does not change either limit.
It uses the scaler service, the existing launch template, and presigned transfers.
It reads no administrator secret.

Each box checks both trees and runs a separate `module_run.py` process for every sample.
Each box first runs base and changed once in separate processes without measurements.
These warm-up runs use the same process isolation as measured runs.
The result rows and medians exclude the warm-up runs.
The default remains three measured rounds.
The reserved box lifetime includes both warm-up runs and all measured rounds.
The guest divides the available time across warm-up runs and measured rounds.
Odd rounds run base first; even rounds run changed first.
The changed candidate supplies the supervisor, environment, and memory sampler.
Each tested tree supplies `module_run.py` and the test module.
Periodic log transfers stop before measurement.
The guest transfers each sample between measurements.
Each paired run writes a UTC `stage=` line through the shared stage writer.
Warm-up stages are `warm-up base` and `warm-up changed`.
Measured stages are `round <r> of <N> <variant>`.
The guest writes `finish` after the last run.
It publishes the current stage before each run through the existing per-run log transfer.
The final transfer includes `finish` and the last result rows.
These transfers include `boot.log`, `gate.log`, and `timeline.log`.
The upload count and measured result fields remain unchanged.

`cloud_gate.log_cloud_progress` reads stages for both paired trials and merge gates.
The progress record adds `stage`, `region`, and `zone` beside the existing fields.
Before the first box log line, the state is `requesting` and the stage is `launch`.
The last UTC `stage=` line supplies the stage text.
A `round` stage sets the state to `running`.
Once the merge runner writes `Runner:`, the stage text becomes empty and the existing module counter applies.
`Cloud gate: uploading` sets the stage text to `uploading`.
Each log download publishes the progress beside the reservation sidecar in `.cloud.json`.
After launch, every progress publication includes the launched region and availability zone from the saved variant.
Before launch, the region and zone are empty.
Older records default all three new fields to empty strings.
Older readers ignore these extra fields.

Stdout contains run JSON lines, module JSON lines, and one final cost JSON line.
The module summary gives the median CPU saving across complete pairs.
A failed or missing sample makes `all_passed` false.
The final line contains `total_cost_usd`, `cost_basis: "estimate"`, and `cleanup_complete`.
The estimate counts only this trial's boxes, allowances, and new bill-query fees.
Unconfirmed termination uses the reserved lifetime and returns a nonzero status.
The command terminates boxes after results, errors, or cancellation.
Uncertain launches retain their requests and reservations for reconciliation and expiry cleanup.

The paired trial replaces the size/start trials.
The twelve `gate_trial_*` settings are removed.
The paired trial uses the existing `cloud_gate.conf` settings.

## Cloud gate region fallback

The deployed region list comes from `regions` in `cloud_gate.conf`.
The list must start with the home region and must not repeat a region.
The list contains `us-east-2`, `us-west-2`, `eu-north-1`, and `eu-south-2`.
`us-east-2` remains the home region for shared artifacts.
The list can include other regions that the account policy permits.
The gate ranks deployed types and public zones by size and expected cost before launch.
Equal costs retain the configured region, type, and zone order.
Capacity refusals and unsupported types select the next variant.
A normal gate uses natedev after all capacity choices refuse.
A confirmed Spot interruption before a final result starts another cloud attempt.
`spot_interruption_retries=2` permits two retries, which can reach another zone and then another region.
The retry uses the next choice in the saved launch order.
The route keeps its cloud slot across retries.
The route saves the interruption count and the next choice in `current.json` before each retry.
Resume restores both values, so a crash cannot reset the retry budget.
Each retry gets a new attempt ID and passes the existing spend and attempt checks.
The worker confirms cleanup of the interrupted box and reservation before the retry.
The final natedev fallback row includes the interruption count when retries or capacity choices end.
A complete gate failure keeps its failed result and gets no retry.
An unconfirmed termination or another controller error retains the existing fallback behavior.
The launch path uses Spot only; it has no on-demand choice.
Paired trials use the same regional launch and cleanup path.

The home stack owns the artifact bucket and the global gate role and instance profile.
Each fallback region has a separate gate stack in its default VPC.
The deployment generates each regional template from the home template.
The regional template contains network, launch, scaler policy, and expiry resources.
The regional scaler policy keeps the home tag, type, Spot, and launch-template restrictions.
The regional expiry function terminates expired boxes in that region.

The deployment resolves the pinned image by Name and owner or copies it when no matching image exists.
It records copies before it waits for image readiness.
The deployment record stores ordered regional launch resources and keeps artifact resources at the top level.
The reader migrates an old single-region record once.
Launch state, launch logs, and reservations retain the box region.
Recovery replays saved launch arguments in the saved region before it uses the current variants.
Status, termination, and expired-launch searches use that region.
Reconciliation searches every configured region for an expired reservation without a recorded region.
After a valid final result, the controller books the box end at the result receipt time.
It still requests termination and requires AWS to confirm termination before it books that time.
A box without a valid final result uses the termination confirmation time.
Reconciliation and the expiry sweep retain their existing end times.
Results include `kernel_boot_at` from `/proc/stat` and `user_data_started_at` from the first user data command.
Each timestamp is a UTC epoch time from the box.
Results include `instance_running_at` when a cached instance identity document supplies `pendingTime`.
The live `gate_box.GUEST` program contains the bootstrap code.
The bootstrap downloads or clones the candidate and verifies its requested tree.
It prepares the environment and calls the candidate's `scripts.aws_target.gate_box.run_guest` entry point.
The entry point accepts the box directory, links, and mutable result dictionary.
The candidate supplies its own suite, timing, and record code.
The guest installs no signal handler.
The module time limit and process groups stop the gate.
The bootstrap names no `merge_gate` attribute and copies no host function source.
A paired trial uses the changed candidate's entry point to supervise both verified trees.
A candidate without the entry point publishes `candidate predates guest entry point` through the existing boot error link.
The controller then uses the normal natedev fallback.
The result fields and transfer links keep their existing format.

The guest reads the NixOS metadata cache or the cloud-init metadata cache without network requests.
It omits `instance_running_at` when the cache is absent or invalid.
The current source checkout occurs after `nix_ready_at`, so results omit `source_ready_at`.
All regions use one spend ledger and the existing caps.
Gate costs use bill rates for the recorded region and UTC day.
A day with its own completed bill keeps that day's rate when newer bills arrive.
An unbilled day uses the newest earlier bill until its own bill arrives.
A day before every bill uses the oldest available bill.
Reservations store the selected rates and bill days for each UTC day.
Each pricing pass builds one bill index for each type and region.
Reservations that start after the current time retain their full reserved compute cost.
Intervals that cross UTC midnight use each day's rate for that part of the lifetime.
The minimum charge applies once per lifetime.
The older builder and paired trials use the same daily rate rule.
Reservations without a region use the highest configured regional rate.
A region without a usable bill rate uses the configured rate ceiling.
Completed gates also use bill rates for non-compute costs.
Public IPv4 and gp3 root storage use regional rates over the gate's actual box hours.
The root charge uses `root_gib` and the number of hours in the bill's month.
S3 requests, storage, and outbound transfer use the bill day's totals divided by its gate launch count.
The ledger charges the S3 share once per gate.
The hourly charges use each UTC day's rate across midnight.
These rates use the same bill day rule as compute rates.
The ledger stores the selected cost and its bill source.
Paired trials use this booked cost for completed gates.
Open reservations retain `attempt_allowance_usd`.
Admission includes these allowances when bills exist.
An own-day bill corrects saved estimates that still include configured allowances.
The bill remains the minimum cost for its reconciliation period.
Missing cost rows contribute zero.
The configured allowance supplies the cost when no needed bill rows exist or the S3 share has no launch count.
The spending limits do not change.

European deployment needs account and policy changes before the regional stacks can start.
Enable `eu-south-2` on the AWS Account page and wait until its status is `ENABLED`.
See [AWS Region activation](https://docs.aws.amazon.com/accounts/latest/reference/manage-acct-regions.html).
Add `eu-north-1` and `eu-south-2` to the allowed regions in `RegionFloor` before deployment.
Keep the existing home and US region permissions.
The runbook gives the preparation steps.

`buildlog.timer` runs the hourly Spot snapshot through `buildlog hourly` on natedev.
The existing timer uses `OnUnitInactiveSec=3600s` and calls the installed scripts.
The hourly job calls `aws_target.snapshot`; `python3 scripts/aws_target/aws_target.py snapshot` runs the same reader manually.
No systemd unit change or NixOS rebuild is required.
The old `aws-target.service` remains disabled.
`aws_target.py tick` reads account charges only.
`price_refresh_seconds` limits snapshot attempts to one per configured interval.
The snapshot takes `spot-prices.lock` without blocking.
It reads each distinct `spot_candidate_regions` region once.
Each regional read requests all `instance_types` and `gpu_instance_types` prices for Linux/UNIX.
The AWS CLI reads all pages, and the reader selects the latest price for each type and zone.
The reader also reads applied Standard Spot and G and VT Spot quotas.
It uses the scaler `credentials_profile` and the private `credential_file` without an administrator passphrase.
It removes inherited administrator keys and profiles from the AWS process environment.
The only API actions are `ec2:DescribeSpotPriceHistory` and `service-quotas:GetServiceQuota`.
`GateGpuTrialScalerPolicy` already permits both actions on all resources without regional conditions.
This change requires no IAM deployment.
The snapshot atomically writes prices and quota values together in `spot-prices.json`.
The existing `prices` array keeps its ranking format: `region`, `zone`, `instance_type`, `usd_per_hour`, and `read_at`.
The new `regions` array stores `region`, `read_at`, `standard_vcpus`, and `gpu_vcpus`.
A regional failure preserves that region's previous prices, quotas, and read time.
Other regions still refresh, and the command returns a failure status.
Each failure logs the region, the failed read, the error class, and a redacted reason on one line.
The read name is `price`, `Standard quota`, or `G and VT quota`.
The shared `admin_secrets.redact` helper removes AWS account ids, ARNs, access key ids, and instance, subnet, AMI, volume, and snapshot ids.
The helper first removes credential field values and all following text, including wrapped values.
It preserves credential fields that already contain `[redacted]`, so repeated redaction keeps appended deployment reports.
It then removes known credentials and AWS identifiers, including wrapped account ids, access key ids, and ARNs.
The snapshot first removes request dumps, response dumps, headers, structured dump lines, and all lines after each dump marker.
It redacts the remaining raw text and selects the first AWS or CLI error line that matches `SNAPSHOT_ERROR_PATTERNS`.
Every pattern matches only at the start of a line.
The patterns accept both CLI error formats, `aws: error:` and `aws: [ERROR]:`, with any letter case for `error`.
They also accept standalone `Invalid choice` and `Parameter validation failed` lines.
If no line matches, it selects the first non-empty line.
It discards all other lines, including usage text and request dumps.
It normalizes that line's whitespace and redacts it again.
`snapshot_error_reason_max_chars=240` limits the reason after both redaction passes.
A successful regional read removes previous prices for types or zones absent from the new reply.
The snapshot does not change `controller.json`.

`python3 scripts/aws_target/deploy.py spot-table` reads only the local snapshot and configuration.
It prints machine types as rows and candidate regions as columns.
Each cell shows the lowest zone price in USD/h, or `-` when the snapshot has no offer.
It splits wide tables into groups of region columns to fit the terminal.
Each region shows its snapshot age and a stale mark after `price_max_age_seconds`.
The `/aws_prices` command runs `spot-table` and shows its lines unchanged.
Below the prices, it shows both quotas in vCPUs and the number of boxes each type fits.
Standard types use the Standard quota; GPU types use the G and VT quota.
The box counts divide the quota by the existing type-size vCPU calculation and round downward.
An absent snapshot shows unknown quotas and box counts without creating files.

A new gate ranks only variants from its deployment.
`deploy.py up` saves the deployed `instance_types` in `gate-deployment.json`.
A gate skips a configured type that this list does not name, and prints `Run deploy.py up`.
When the list names no configured type, the gate falls back to natedev before any launch.
A manifest without the list keeps every configured type.
An `UnauthorizedOperation` refusal skips that type for the rest of the launch while another type remains.
`launch.json` saves the skipped types in `unauthorized_types`.
Before its first ranking, it reads live Spot prices in parallel from each deployed region.
Each region receives one Linux/UNIX request for all configured `instance_types`.
`spot_price_live_read_timeout_s=3` bounds the whole parallel read in seconds.
The reader selects the latest price for each deployed zone and type.
Live rows replace matching hourly rows for this ranking only.
Failed or slow regions retain their hourly rows.
The live read never writes `spot-prices.json`.
`launch.json` saves `spot_price_reads`, with `region`, `seconds`, and `used` for each regional call.
Launch and refusal rows copy this evidence into `cloud-gate-launches.jsonl`.
Resumed launches keep the saved ranking and evidence without another live read.
Available variants precede variants under a capacity cooldown.
Within each group, larger vCPU sizes precede smaller sizes.
Types with equal vCPU counts compete on expected gate cost.
That cost includes the hourly Spot price times expected gate hours and the remote region transfer charge.
It also includes regional extra seconds times `gate_wait_usd_per_minute`, divided by 60.
`region_extra_gate_seconds={}` holds a JSON map from region names to measured extra seconds.
Missing regions contribute zero extra seconds.
`gate_wait_usd_per_minute=0` disables the wait cost until the delay trial and builds review set it.
Expected duration uses the median finished `gate_s` for the same mode within `price_max_age_seconds=7200`.
Without recent durations, it uses `lifetime_minutes`.
`remote_region_transfer_usd=0.00015` applies when the region differs from the home region.
Local measurements cover 692 gates with a source bundle, request, and result.
Their median sizes are 10,157, 32,926, and 7,051,583 bytes, respectively.
The median combined size is 7,107,672 bytes.
At $0.02 per decimal GB, this costs $0.00014215344; the configured value rounds upward.
Equal costs keep configured order.
Missing or stale prices follow fresh prices for the same vCPU count.
Without applicable fresh prices, the gate preserves configured order except for capacity cooldowns.

`capacity_cooldown_seconds=1800` moves recently refused capacity to the end without removing it.
`InsufficientInstanceCapacity`, `InsufficientSpotCapacity`, and `SpotMaxPriceTooLow` affect one region, type, and zone.
`MaxSpotInstanceCountExceeded` and `VcpuLimitExceeded` affect every variant of the region.
Ranking reads the end of `cloud-gate-launches.jsonl` in 64 KiB blocks.
It stops at the first row before the longer of the price-age and cooldown windows.
The current two-hour window reads 65,536 bytes from a 750,584-byte local history file.
`launch.json` saves the ranked region, type, subnet, and zone list before the first launch step.
Resumed launches keep that list and their offset-based ClientToken values.
Older files with only `variant` retain configured order.
Refusal handling, regional cost bookings, and the $35 daily and $400 monthly limits remain unchanged.

The prices command prints regional Spot prices without resource identities.
It reads gate regions, then `spot_candidate_regions`, without duplicates.
It preserves the configured order within each list.
`spot_candidate_regions=us-east-2 us-west-2 eu-north-1 eu-south-2` supplies the candidate regions for Spot work and price reads.
`gpu_instance_types=g6.xlarge g4dn.xlarge g4ad.xlarge g5.xlarge` adds GPU types to the existing price rows.
The table header follows the administrator passphrase prompt.
Whole quota values have no decimal suffix.
`deploy.py prices` shows the Standard Spot vCPU quota (`L-34B43A08`) for every region in its combined list.
`deploy.py prices` also shows the G and VT Spot vCPU quota (`L-3819A6DF`) for each region in its list.
The reads use the encrypted administrator key through the admin executor.
A region whose quota is below the largest configured type prints a request for a quota increase.
The vCPU count comes from the type size: `large` is 2, `xlarge` is 4, and `<N>xlarge` is 4 × N.
A type with another size has no known count, and the check skips it.
A failed read prints the quota as unknown with the redacted reason, and the command continues.
The administrator IAM policy is outside the repository and needs `servicequotas:GetServiceQuota`.
`deploy.py quota-request` requests Standard Spot quota (`L-34B43A08`) in each gate region from `regions`.
Credential refusals print their message once and stop the command with a nonzero exit status.
AWS failures still print the AWS error code and allow the next region's request.
It uses `spot_quota_request_vcpus` from `cloud_gate.conf`.
The default request is 136 vCPUs for four 32-vCPU gates and one 8-vCPU Hana box.
The same command requests G and VT Spot quota (`L-3819A6DF`) in each region from `spot_candidate_regions`.
It uses `gpu_spot_quota_request_vcpus=32` for one GPU box up to 8xlarge.
All three new keys are in `cloud_gate.conf`.
Both quota codes use one helper for regional requests.
The command uses the encrypted administrator key through the admin executor.
It skips regions with sufficient quota and prints existing open requests instead of creating duplicates.
It prints the region, desired value, and request status without request, case, or account identifiers.
A failed call prints only the AWS error code or Python error class and does not stop other regions.
Any regional failure makes the command exit with status 1.
The external administrator IAM policy also needs `servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota` and `servicequotas:RequestServiceQuotaIncrease`.
Both quota codes use these same three IAM actions.
`python3 scripts/aws_target/deploy.py quota-status` reads both quotas and their request history through the same administrator executor.
It shares the quota codes and region lists with `quota-request`.
Each region starts one terminal block with its name as the heading.
Each quota shows its applied vCPUs once, above an aligned request table.
The table shows asked vCPUs, status, case ID, and update time, with the newest requests first.
Dates use the user's zone and the last update time, with the creation time as a fallback.
A quota with no requests prints `No requests.`
The final summary lists regions whose applied GPU quota fits the trial and requests that remain open.
The summary uses the trial's shared vCPU calculation over `gpu_size_trial_spot_max_usd_per_hour`.
It does not use the requested quota as proof of an applied quota.
When every applied quota is too small, the summary says `no region has enough applied quota`.
An unread applied GPU quota adds `unknown for <regions>` after the regions that fit.
An unread request history makes the summary say `open requests: unknown` and name the unavailable region and quota.
The report omits account IDs, ARNs, and request IDs.
Regional failures print the AWS error code or Python error class and allow the remaining regions to continue.
Any regional failure makes the command exit with status 1.
The command uses only `GetServiceQuota` and `ListRequestedServiceQuotaChangeHistoryByQuota`, including every history page.
It makes no quota request, paid API call, or local state write.
The administrator IAM action list in the runbook already includes both read actions.
`down` searches configured regions and regions in the saved manifest and stack records.
It terminates boxes before it deletes their regional stacks.
Then it deletes copied images and their snapshots.
`remove-images` changes the home image and keeps regional stacks and copied images.
Copied image records permit cleanup after an interrupted deployment.

## GPU size trial

`gpu_size_trial.py` provides local preparation, a transient scaler service, cleanup, and local comparison.
The session prepares and compares the trial without AWS calls:

```sh
python3 -m scripts.aws_target.gpu_size_trial prepare --commit SHA
python3 -m scripts.aws_target.gpu_size_trial compare --trial-folder DIR
```

The user installs the policy and starts the service:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
python3 scripts/aws_target/deploy.py gpu-size-trial run --trial-folder DIR --approve-resources
```

Preparation uses build admission and natedev holds.
It builds one archive and one NixOS GPU system closure.
The Rust tools and nextest come from Nix store paths.
The Hana toolchain output takes precedence when it exists.
Otherwise, the pinned host nixpkgs supplies Rust through its locked overlay.
A Hana `rust-toolchain.toml` selects the Rust version when that file exists.
Preparation uses a private Cargo cache.
It never copies the user's Rust or Cargo directories.
It checks toolchain wrappers for home paths and tests Cargo with an empty `HOME`.
Preparation builds all features and discovers shared libraries in the Cargo target directory.
A nextest archive profile inherits `ci` and includes each shared library through `archive.include`.
The archive retains the workspace's existing archive includes.
Nextest lists the extracted tests before preparation builds the NixOS closure.
An archive validation error includes nextest's diagnostic, which names a missing shared library.
Each measurement run creates its extraction directory before passing it to nextest through `--extract-to`.
Nextest 0.9.143 returns setup exit 96 when that directory does not exist.
A failed preparation copies up to 200 final lines of each failed `nextest.log` into `prepare.log`.
The reader seeks backward in 64 KiB chunks and reads at most 1 MiB per log.
It retains diagnostics when a run has no valid status record.
It then keeps only `prepare.log` and removes the other files and GC root links.
Host trial folders must be canonical direct children of `~/.local/state/gpu-size-trial`.
The commands refuse nested paths, outside paths, and paths through directory links before deletion.
A new preparation removes earlier trial folders under `~/.local/state/gpu-size-trial` when no live run holds their locks.
It refuses to create another folder while a live run holds an earlier folder.
Preparation, its worker, the service launcher, and the online worker hold folder locks.
The locks remain outside the trial folders.
A successful preparation keeps only these inputs and records:

1. `hana-tests.tar.zst`, `closure.export`, and `source.tar`.
2. `cargo-home/`, `runtime.env`, `gpu-trial.nix`, and `gpu_trial_wrapper.py`.
3. `closure.json`, `request.json`, `manifest.json`, and `inventory.json`.
4. `machines/` with baseline reports, samples, and logs, plus `prepare.log`.

After the archive and closure exist, preparation replaces the build tree with the source tar's clean contents.
It removes that source copy and the temporary Nix profile links after baseline measurement.
Each measurement removes its extraction directory, source copy, and Cargo target directory when the run ends, including failures.
Reports, samples, and logs remain.
The measured inputs imply an estimated peak near 32 GiB during preparation and about 12 GiB after preparation.
Local measurement needs about 19 GiB because only one extracted archive remains at a time.
These estimates cover the trial folder; Nix store use is separate.

The service checks candidate regions in their configured order.
It selects the first region whose applied G and VT Spot quota fits the largest type the trial launches (`gpu_size_trial_spot_max_usd_per_hour`).
It uses the existing type-size vCPU calculation and refuses a type whose vCPU count is unknown.
If no region fits, the refusal reports the required vCPUs and each region's applied value.
The quota request size remains separate from this selection rule.
It checks regional offerings and current Spot prices.
It uses the regional gate template and its NixOS image.
A separate policy statement permits the configured GPU types with the existing tags and a `GpuSizeTrial` tag.
The CPU gate statement remains unchanged.

Each box imports the signed closure download and reboots once.
The prepared system enables NVIDIA Vulkan and starts the trial runner.
The runner uploads each run's report and memory rows separately.
The wrapper records `ru_maxrss` for the largest single process of each test.
The report excludes GPU memory.

The ledger stores the mode, trial budget, and root size with each reservation.
The USD6 check includes completed costs and full open reservations under the accounting lock.
The daily and monthly limits still apply.
A restart retains the trial ID, limit, region, and type order.
A saved budget stop ends the trial.
After an online run ends, the worker saves its result or error before removing the archive, closure export, source tar, and Cargo cache.
It removes the temporary upload payload after upload, including an upload failure.
Metadata, results, and logs remain for comparison.
A restart uses saved hashes and uploaded inputs when upload completed.
A failure before upload requires a new preparation.

The service confirms termination before another launch.
The guest stops five minutes before the lifetime ends.
The expiry tag triggers two minutes before the lifetime ends.
`ExecStopPost` cleans known boxes and uncertain launches.
It saves an error and removes the large inputs if an interrupted worker leaves no final result.
The service uses no new signal handler.

`gpu_size_trial_root_gib=100` is an estimated disk requirement.
It includes the GPU driver, runtime, archive extraction, and two test runs.
The service reserves the configured maximum compute cost plus `gpu_size_trial_box_extra_usd` for each box.
The default extra is USD0.05.
