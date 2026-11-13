# AWS per-run gates and trials: runbook

AWS runs temporary Spot boxes for merge gates, paired gate trials, and the GPU size trial.
Normal build jobs use `mac` and `natedev`.
AWS has no permanent builder or build offload route.
Resource changes in this runbook belong to the user at a terminal.
Local fake tests do not prove live quota, capacity, or deployment state.

## Removed builder: 2026-11-04

`builder-removal.json` records the removal on 2026-11-04.
The persistent `aws-build` instance, Tailscale device, and data volumes were removed.
`routing.json` records `enabled: false`.
The registry contains `targets=mac,natedev`.
The builder configuration and offload route have been removed.
Builder slots, warm mirrors, idle stop, and controller activation are historical features.
Only historical cost, ownership, and teardown compatibility remain.
Use `up --workload merge-gates` for gate deployment.
The default `up` command now selects gate deployment.

## Configuration and state

Read account limits and billing settings from `config/aws_target.conf`.
Read launch and trial settings from `config/cloud_gate.conf`.
The configuration files remain the source for cap values.
Historical builder costs still count in account reports.
The ledger preserves those costs after builder removal.
The current resource inventory excludes the removed builder's ongoing storage costs.

The state folder is `~/.local/state/build-targets/aws`.
`gate-deployment.json` records the bucket and regional launch resources.
`deployment.json` records foundation ownership.
The configured region list alone does not prove that all regional stacks exist.
Preserve the ledger, deployment records, launch records, and cleanup records during recovery.

## Credentials

Create or update the encrypted administrator file at your terminal:

```sh
python3 scripts/aws_target/deploy.py secrets
```

The default encrypted path is `~/.config/aws-build/admin.gpg`.
The helper reads hidden terminal input and uses symmetric AES256 encryption.
It removes a plain administrator profile after encryption succeeds.
It preserves the scaler profile and unrelated profiles.
Enter retains a stored value during replacement.
A process without a terminal cannot decrypt through this helper.

Deployment uses the encrypted administrator key for resource changes and quota requests.
Gate services use the private scaler profile from `credentials_profile` in `aws_target.conf`.
`credential_file` in `cloud_gate.conf` supplies the default private file path.
A nonempty `AWS_SHARED_CREDENTIALS_FILE` overrides that path.
Deployment commands and transient services use the same resolved path.
The resolver expands `~`.
Boxes receive signed URLs and no scaler credential file.

## Regions, prices, and quotas

The configured regions are `us-east-2`, `us-west-2`, `eu-north-1`, and `eu-south-2`.
`us-east-2` is the home region for shared artifacts.
Before European deployment:

1. Enable `eu-south-2` on the AWS Account page.
2. Confirm that its status is `ENABLED`.
3. Add both European regions to the Organizations `RegionFloor` policy.
4. Preserve the home and US region permissions.

Use these commands at your terminal:

```sh
python3 scripts/aws_target/deploy.py prices
python3 scripts/aws_target/deploy.py quota-request
python3 scripts/aws_target/deploy.py quota-status
```

Read the local hourly snapshot without an AWS call or a passphrase:

```sh
python3 scripts/aws_target/deploy.py spot-table
```

The rows show configured Standard and GPU machine types.
The columns show candidate regions and the lowest zone price in USD/h.
A `-` means the snapshot has no offer for that type and region.
Wide tables use separate groups of region columns.
Each region shows its snapshot age, both applied quotas, and the number of boxes each quota fits.
GPU types use the G and VT quota; other configured types use the Standard quota.
Old snapshots show a stale mark after `price_max_age_seconds`.
A regional read failure keeps that region's previous values and age.

The existing enabled `buildlog.timer` runs `buildlog hourly` on natedev every hour.
The hourly job calls `aws_target.snapshot` after the existing jobs.
A job failure does not stop the other jobs.
No new timer, systemd change, or NixOS rebuild is required.
The old `aws-target.service` stays disabled.
The reader uses `credentials_profile` from `aws_target.conf` and `credential_file` from `cloud_gate.conf`.
It uses no administrator passphrase.
It reads every candidate region and all configured Standard and GPU types.
It atomically stores prices, quotas, and regional read times together in the AWS state folder's `spot-prices.json`.
The existing price rows still support the gate ranking fallback.
`price_refresh_seconds` limits repeated attempts; its value remains unchanged.

After this change reaches the installed scripts:

1. Run `python3 scripts/aws_target/aws_target.py snapshot` to create the first snapshot.
2. Run `python3 scripts/aws_target/deploy.py spot-table`, or `/aws_prices` in a session, to inspect the snapshot.

Later reads run through the existing timer.
`GateGpuTrialScalerPolicy` already allows `ec2:DescribeSpotPriceHistory` and `service-quotas:GetServiceQuota` in every candidate region.
No `deploy.py up` command is required for this change.
The snapshot makes only these two read calls.
It does not use Cost Explorer or create resources.

`prices` reads deployed-region configuration and candidate-region configuration without duplicates.
It prints current Spot prices and applied Standard and G and VT quotas.
It includes the GPU types from `gpu_instance_types`.
It prints no resource identities.

`quota-request` requests Standard Spot quota in each configured gate region.
It uses `spot_quota_request_vcpus` from `cloud_gate.conf`.
It requests G and VT Spot quota in each candidate region.
It uses `gpu_spot_quota_request_vcpus` from the same file.
It skips sufficient quotas and existing open requests.
A regional failure prints its error code and permits other regions to continue.
Any regional failure makes the command exit with status 1.
A requested quota does not prove an applied quota.

`quota-status` reports the applied quotas and their request history without creating a request or writing local state.
It uses the same regions, quota codes, and encrypted administrator credentials as `quota-request`.
Each request row shows the region, quota name, asked value, current applied value, status, case ID, and date.
The date uses the latest update time, or the creation time when no update time exists.
Dates use the zone from `config/user_zone.conf`, with `USER_ZONE` as an override.
A quota with no request history has one row with status `none`.
The command prints progress for each region and a final completion time.
A regional failure prints only its error code and makes the command exit with status 1 after the remaining regions.
The report includes case IDs and omits account IDs, ARNs, and request IDs.
It calls only `GetServiceQuota` and `ListRequestedServiceQuotaChangeHistoryByQuota` through the AWS CLI.
The AWS CLI reads every page of request history.
[AWS provides Service Quotas at no additional charge](https://aws.amazon.com/about-aws/whats-new/2023/03/service-quotas-additional-regions/).

The administrator IAM policy needs these actions:

1. `servicequotas:GetServiceQuota`.
2. `servicequotas:ListRequestedServiceQuotaChangeHistoryByQuota`.
3. `servicequotas:RequestServiceQuotaIncrease`.

`quota-status` needs only the first two actions, which this list already includes.

## Deploy gate resources

Update the existing gate foundation at your terminal:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
```

This path updates gate resources and preserves the foundation's existing builder parameters.
It requires the saved foundation ownership record.
It does not activate the removed builder route.
The home stack supplies the bucket, role, instance profile, and home launch resources.
Each additional region has a separate gate stack and expiry function.
Deployment records each completed regional stack in `gate-deployment.json`.
The gate path uses the official NixOS image.
Saved regional image copies remain subject to normal teardown.

## Foundation maintenance

`prepare` validates the retained deployment inputs and records the foundation plan.
`apply --approve-resources` creates or updates the Budget and scaler foundation with compute disabled.
It also confirms the scaler credential file.
These commands do not create a permanent builder.
Gate deployment requires the saved foundation ownership record.
The removed `price`, `bootstrap`, and `setup-start` commands are unavailable.
The removed builder service commands `start`, `stop`, and `serve` are unavailable.
Use `aws_target.py tick` for account bill and regional price refresh.

## Merge gates and ranking

The production merge gate selects the cloud path through `cloud_gate.py`.
Queue admission reserves cloud capacity before launch.
The spending ledger reserves cost before each compute request.
The guest obtains the source and tools for that run.
The queue shows the current setup stage, region, and zone.
The normal module counter replaces the setup stage when tests start.

Before ranking, the worker reads live Spot prices from all deployed regions in parallel.
`spot_price_live_read_timeout_s` bounds the complete read.
Failed regional reads retain applicable hourly prices from `spot-prices.json`.
`buildlog hourly` refreshes cached prices and quotas through the scaler profile.
`aws_target.py tick` reads account bills only.
A gate's live read does not change the hourly file.

The worker places variants under a capacity cooldown last.
It prefers larger vCPU counts within each group.
Within one size, it prefers the lowest expected cost across regions and zones.
Expected cost includes compute, remote transfer, and the configured regional delay cost.
Equal costs preserve configured order.
The worker saves its ranking and price evidence before launch.
A resumed launch keeps that ranking and its client tokens.
Capacity refusals select the next variant.
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

## Paired gate trials

Run a paired trial from the scripts repository:

```sh
python3 -m scripts.aws_target.gate_trial --base BASE --changed CHANGED --modules scripts/PATH/test_MODULE.py --rounds 3
```

The command pushes temporary branches through the invoking terminal's Git credentials.
It uses a fresh gate box for each selected module.
It reserves all required cloud slots and costs before launch.
Each box runs both commits once before measurement.
Measured rounds alternate their execution order.
The command reports samples, median CPU savings, estimated cost, and cleanup status.
Trial cleanup deletes its temporary branches and terminates its boxes.
The command uses the scaler service and reads no administrator secret.

## Results, spending, and cleanup

The build report reads AWS costs without registering an AWS build target.
It includes account bills, daily gate spending, and historical builder comparisons.
Bill rates use the recorded region and UTC day.
Open reservations remain in the spending calculation until reconciliation confirms their result.
Daily and monthly caps apply to gates and trials.

Worker cleanup requests termination after results or failures.
`ExecStopPost` repeats cleanup after the worker stops.
Cleanup requests termination for known boxes before it searches uncertain launches.
A failed AWS confirmation leaves the reservation open for later reconciliation.
Guest shutdown and regional expiry functions supply separate lifetime bounds.
AWS failures can delay termination beyond the configured lifetime.

Remove all remaining gate resources only after reviewing the dry run:

```sh
python3 scripts/aws_target/deploy.py down
python3 scripts/aws_target/deploy.py down --approve-destroy
```

The removal path works without restoring the former AWS target registration.
It checks saved ownership and live claims before deletion.
`down --approve-destroy` removes the complete AWS deployment, not only the gates:

1. It terminates regional boxes, then deletes the regional gate stacks, copied images, and snapshots.
2. It deletes the tagged Tailscale device and keeps the `tag:build-aws` policy rules.
3. It deletes all scaler access keys and removes only the scaler profile from the plain credentials file.
4. It deletes the home foundation stack, which holds the scaler user and its policies.
5. It deletes the retained Budget.
6. It removes `deployment.json`, `up.json`, the setup proofs, and the prepared bundle.

It keeps the encrypted administrator file, the ledger, and the charge history.
After complete removal, run `deploy.py prepare` and `deploy.py apply --approve-resources` to create the foundation again.
Then run `deploy.py up --approve-resources` to create gate resources.
Correct a reported removal failure before repeating the approved command.
Preserve `down.json` until removal completes.

## GPU size trial

The trial compares one prepared Hana archive on natedev and the configured GPU types.
The complete trial has a USD6 reservation limit.
The existing daily and monthly limits also apply.

The session runs these local commands:

```sh
python3 -m scripts.aws_target.gpu_size_trial prepare --commit SHA
python3 -m scripts.aws_target.gpu_size_trial compare --trial-folder DIR
```

`prepare` prints the trial folder.
It waits for build admission and a natedev hold release.
It builds the archive and NixOS closure, then runs the archive twice on natedev.
Use that same folder for the online run and each restart after upload.
Host trial folders must be canonical direct children of `~/.local/state/gpu-size-trial`.
The commands refuse nested paths, outside paths, and paths through directory links before deletion.
A new preparation removes earlier folders under `~/.local/state/gpu-size-trial` when no live run holds their locks.
It refuses to create a second folder while an earlier folder is held.
The active preparation and online service hold those locks.
Preparation deletes the build tree after it creates the archive and closure.
It keeps a clean source copy only until baseline measurement ends.
Each measurement deletes its extracted archive, source copy, and Cargo target directory on success or failure.
The reports, samples, and logs remain.

The prepared folder keeps these files:

1. `hana-tests.tar.zst`, `closure.export`, and `source.tar`.
2. `cargo-home/`, `runtime.env`, `gpu-trial.nix`, and `gpu_trial_wrapper.py`.
3. `closure.json`, `request.json`, `manifest.json`, and `inventory.json`.
4. `machines/` with baseline results and `prepare.log`.

With the measured inputs, preparation peaks near 32 GiB and keeps about 12 GiB when ready.
Local measurement uses about 19 GiB with one extracted archive.
These estimates cover the trial folder; Nix store use is separate.

The Rust tools come from Nix.
Old preparations require a new `prepare` run before the service can use them.
The trial uses the Hana flake's toolchain when that output exists.
Otherwise, the pinned host nixpkgs and Rust overlay supply the toolchain.
A `rust-toolchain.toml` selects its version when the file exists.
The trial fetches dependencies into a private Cargo cache.
It does not copy `~/.rustup` or `~/.cargo`.
Preparation checks Cargo with an empty `HOME`.

The user runs these commands at their terminal after review:

```sh
python3 scripts/aws_target/deploy.py up --workload merge-gates --approve-resources
python3 scripts/aws_target/deploy.py gpu-size-trial run --trial-folder DIR --approve-resources
```

The first command adds the configured GPU types to the scaler policy.
The GPU permission requires the existing gate tags and a `GpuSizeTrial` tag.
The existing gate policy remains unchanged.
The separate GPU policy permits quota and offering reads.

The second command checks the private scaler credential file and starts a transient service.
The service checks candidate regions in their configured order.
It selects the first region whose applied G and VT Spot quota fits the largest type the trial launches (`gpu_size_trial_spot_max_usd_per_hour`).
An unknown type-size vCPU count stops selection before AWS reads.
If no region fits, the refusal shows the required vCPUs and each region's applied value.
`gpu_spot_quota_request_vcpus` still controls the separate quota request size.
It reports `not offered` for missing types.
It reports `price above max` when no offered zone fits the configured maximum.
It never substitutes another type or region.

The service uploads the archive, closure, manifest, and runtime files to the home-region bucket.
It removes its temporary upload payload after upload, including an upload failure.
When the online run ends, it saves the result or error and removes the archive, closure export, source tar, and Cargo cache.
Metadata, reports, samples, and logs remain for comparison.
A restart uses the uploaded inputs and saved hashes.
A failure before upload requires a new preparation.
Boxes receive signed URLs.
They receive no AWS credential file.
Each box imports the prepared NixOS closure and reboots once.
The system unit then starts the runner.
The setup record includes boot, import, reboot, and total setup seconds.

The service runs one box at a time.
It starts termination five minutes before the configured lifetime ends.
The expiry tag triggers two minutes before the lifetime ends.
An unconfirmed termination blocks the next box.
The service's `ExecStopPost` command repeats cleanup after a failure.
It saves an error and removes large inputs when an interrupted worker leaves no final result.

The ledger reserves the type maximum multiplied by the lifetime hours, plus the configured box extra.
It counts completed costs and open reservations against the same trial ID.
It stops before the next reservation would exceed USD6.
A restart cannot increase the trial's saved budget.
The ledger uses the configured extra estimate until bill data establishes the completed cost.

`gpu_size_trial_root_gib=100` estimates space for the driver, runtime, archive extraction, and two runs.
This size is an estimate, rather than a measured closure requirement.
The box extra remains `gpu_size_trial_box_extra_usd=0.05`.

Each machine runs every selected test twice with zero retries.
The report retains both statuses, wall times, and largest-process peak RSS values.
Different statuses on one machine mean `flaky`.
A flaky natedev test makes the comparison `inconclusive`.
Missing measurements mean `incomplete`.
