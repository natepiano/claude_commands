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
The installed guest supplies the environment and memory sampler.
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
A normal gate uses natedev after every regional variant refuses.
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

`aws_target.py tick` refreshes gate Spot prices.
`price_refresh_seconds=3600` limits the refresh frequency.
The command no longer starts or stops a builder.
It takes `spot-prices.lock` without blocking and skips the refresh when that lock is held.
The price lock stores the last refresh attempt time.
Slow price reads leave start and stop commands free to acquire the controller lock.
It reads each region in `spot_candidate_regions` and the gate deployment once per refresh.
Each read requests all configured gate `instance_types` for Linux/UNIX.
The reader keeps the latest price for each zone and type.
It atomically replaces `spot-prices.json` in the AWS state folder only after every region has complete prices.
Each row contains `region`, `zone`, `instance_type`, `usd_per_hour`, and `read_at`.
An error, missing type, missing deployed zone, or incomplete page preserves the previous file.
The price refresh does not change `controller.json`.
Failed attempts use the same refresh interval.
The price read remains active when the former builder route is disabled or removed.

A new gate ranks only variants from its deployment.
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
A successful preparation keeps all trial files.

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

The service confirms termination before another launch.
The guest stops five minutes before the lifetime ends.
The expiry tag triggers two minutes before the lifetime ends.
`ExecStopPost` cleans known boxes and uncertain launches.
The service uses no new signal handler.

`gpu_size_trial_root_gib=100` is an estimated disk requirement.
It includes the GPU driver, runtime, archive extraction, and two test runs.
The service reserves the configured maximum compute cost plus `gpu_size_trial_box_extra_usd` for each box.
The default extra is USD0.05.
