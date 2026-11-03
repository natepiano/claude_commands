# AWS build target: guarded deployment

This procedure belongs to the user at a terminal.
Agents prepare files and evaluate NixOS configurations.
Agents do not create resources, configure credentials, activate systems, or enable services.
Local fake tests do not prove real bootstrap, volume retention, or persistent Spot behavior.
The user checks those properties below.

## Prepared branch and fixed choices

The separate NixOS checkout is `/home/natepiano/worktrees/nixos-aws-build` on branch `aws-build`.
Its base is `3a2d311`.
The unit director commits its prepared changes.
The natedev showrunner receives that commit before activation.
The review patch is [nixos.patch](builds-aws/nixos.patch).
The patch includes the dedicated host and natedev's disabled controller service.
The AWS host imports no desktop configuration or natedev jobs.

Keep the configured `instance_type`, x86_64, persistent Spot, interruption behavior `stop`, and hostname `aws-build`.
Keep the encrypted retained `/home` volume with the configured `data_volume_gib`, `data_volume_iops`, and `data_volume_throughput`.
Keep `monthly_limit_usd=400` and `ci_repo=`.
Approve `daily_limit_usd=20` (derived: the monthly cap / 20). Do not raise it.
Keep AWS lint disabled.
AWS accepts only test, check, and doc work.
Build, example, and final work remain on natedev.

All tuning values belong in `config/aws_target.conf`.
Read configuration through `scripts/lib/conf.py`.
The controller uses `config.offload_enabled` for the switch.
The scheduled controller also owns charge reads through `CostLedger.refresh_charges`.
There is no separate charge timer.

## Account, placement, and public prices

1. Create the AWS account and administrator login in the AWS console.
2. Set the billing notification email and confirm its subscription.
3. Prepare the administrator profile named by `deployment_profile` in the same private credential file at your terminal.
4. Read the free placement scores for `m7a.4xlarge` in `us-east-2`.
5. Read current Spot, EBS, gp3 performance, root storage, and public IPv4 prices.
6. Select one available zone in `us-east-2` from those reads.
7. Read the standard Spot vCPU quota. Request an increase if it is below 16.
8. Record the Spot price and its read time just before `deploy.py prepare`. Deployment refuses a price older than `price_max_age_seconds`.
9. Confirm that the fixed components sum to `fixed_usd_per_month`.
10. Confirm that `limit_reserve_usd` covers the configured drain and idle intervals.

A placement score for one instance type is always low. AWS needs at least three types for a useful score. On 2026-11-03 every `us-east-2` zone scored 1, so the zone choice uses price. The proposed zone, waiting for the user's approval, is `us-east-2c` (`use2-az3`) at $0.2250/h.

A new account's "All Standard Spot Instance Requests" quota (`L-34B43A08`) defaults to 5 vCPU. `m7a.4xlarge` needs 16. A start below the quota fails with "Max spot instance count exceeded".

The account (created 2026-11-03) is a project account. The user activated advanced features on 2026-11-03. Its service control policies now set these limits:

- `RegionFloor` allows only `us-east-1`, `us-east-2` and `us-west-2`. The policy denies most actions in `us-east-1`, the Pricing API included. Use `us-east-2`, and read prices from the public AWS price pages.
- No policy denies Spot. Activation removed the Paid Plan policy. A `run-instances --dry-run` with `MarketType=spot` in `us-east-2` succeeded on 2026-11-03.
- The AWS spend limit is gone. The stack's $400 Budget with its stop action is the AWS-side backstop.
- The four `BudgetsSpendLimit*` policies must have no target. In the Organizations console, open each policy's Targets tab, and detach the project account if it is listed.

The following commands are user-run examples.
Replace the uppercase values with public account inputs from the configuration.
Do not send credentials or command output that contains credentials to an agent.
Set the private credential-file environment before these reads.
Use the configured `deployment_profile` if it differs from `build-admin`.

```sh
install -d -m 0700 "$HOME/.config/aws-build"
export AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials"
(umask 077; aws configure --profile build-admin)
chmod 0600 "$AWS_SHARED_CREDENTIALS_FILE"
aws --profile build-admin ec2 get-spot-placement-scores \
  --region us-east-2 --instance-types m7a.4xlarge --target-capacity 1 \
  --single-availability-zone --region-names us-east-2
aws --profile build-admin ec2 describe-spot-price-history \
  --region us-east-2 --instance-types m7a.4xlarge \
  --product-descriptions Linux/UNIX --max-results 20
aws --profile build-admin service-quotas get-service-quota \
  --region us-east-2 --service-code ec2 --quota-code L-34B43A08 \
  --query Quota.Value
aws --profile build-admin service-quotas request-service-quota-increase \
  --region us-east-2 --service-code ec2 --quota-code L-34B43A08 \
  --desired-value 32
```

Run the increase request only when the quota is below 16. The value 32 also covers a later move to `m7a.8xlarge`. A quota request has no charge.

Read gp3, root storage and public IPv4 prices for `us-east-2` from the public [EC2](https://aws.amazon.com/ec2/pricing/on-demand/) and [EBS](https://aws.amazon.com/ebs/pricing/) price pages.
Price-list and placement reads do not launch capacity.
Cost Explorer reads have the configured query fee.

## Official AMI and required inputs

1. Open the [official NixOS AMI list](https://nixos.github.io/amis/).
2. Select the configured NixOS release, region, and x86_64 architecture.
3. Record the AMI ID and its official owner ID in configuration.
4. Verify the AMI at your terminal.

```sh
aws --profile build-admin ec2 describe-images --region REGION \
  --image-ids AMI_ID --owners OFFICIAL_OWNER_ID \
  --query 'Images[].{Id:ImageId,Owner:OwnerId,Name:Name,Arch:Architecture,State:State,Root:RootDeviceName}'
```

The result must show the selected release, `x86_64`, and `available`.
Record the image's root device name.
Do not substitute an unverified third-party image.

1. Fill `region`, `availability_zone`, `ami_id`, `ami_owner_id`, and `nixos_release`.
2. Fill `vpc_id`, `subnet_id`, `notification_email`, and the deployment profile.
3. Confirm public HTTPS egress and a usable route for SSM and public packages.
4. Permit inbound UDP 41641 for Tailscale transport.
5. Permit no inbound TCP ports.
6. Activate the configured cost-allocation tag in the billing console.
7. Keep its key and value unchanged on retained resources.
8. Leave `existing_data_volume_id` empty for the first deployment.
9. Prepare the public bootstrap source and rendered file as described below.
10. Keep `offload=off` and omit AWS from `targets`.

Use the reviewed scripts commit for the public bootstrap archive.
The archive must be readable without credentials.
Set `scripts_source_url` to its immutable HTTPS URL.
Set `scripts_source_sha256` to its unpacked SHA-256 hash.
Set `scripts_source_commit` to its exact commit.
Copy only its tracked scripts and public settings during bootstrap.
Set `bootstrap_template_path` to `hosts/aws-bootstrap.sh.in` in the reviewed separate NixOS checkout.

The bootstrap renderer replaces these public inputs through `conf.py`.
Its early watchdog uses `idle_stop_minutes` before package installation begins.
CloudFormation supplies `AWS_BUILD_DATA_VOLUME_ID` from the selected retained or new volume.
The shared host module verifies that ID against the attached device's serial.
The bootstrap formats only a new blank volume.
Never format a volume with an existing filesystem or retained data.
A reused volume must already have ext4 and label `aws-home`.
The renderer inserts `hosts/aws-build-base.nix` at `@INPUT@`.
The full host imports the same module.
The helper compresses the script for the CloudFormation `Bootstrap` parameter.
The compressed parameter must fit the 4096-byte limit.
CloudFormation sets the volume ID before it decompresses and executes the script.

Generate the bootstrap locally before the priced preflight:

```sh
cd "$HOME/.claude"
python3 scripts/aws_target/deploy.py bootstrap
```

Inspect the generated bootstrap file and remote settings.
They must contain no credential or published admission permission.
The generated settings enable remote lifecycle checks and leave execution unavailable.
The bootstrap saves the identified lifecycle settings in `/var/lib/aws-build/remote.conf`.
Both host configurations expose that file through `/etc/aws-build/remote.conf`.
The remote service reads this separate file.
Sender copies cannot disable its idle shutdown through natedev's `offload=off` setting.

## Private scaler credential file

The scaler uses one instance-scoped policy and the `build-scaler` profile.
It can start or stop that instance and perform the required reads.
Those reads include `ec2:DescribeSpotInstanceRequests` for the rollback check.
It has no general deployment permission.
The box receives an SSM instance profile and no scaler key.

Run these commands yourself after the scoped IAM user exists.
Do not copy this file to the box or print its contents.

```sh
(umask 077; aws configure --profile build-scaler)
```

The earlier credential block sets the private file and mode.
Use your administrator profile only for guarded deployment and administrative recovery.
Keep passwords, AWS keys, Tailscale auth keys, and GitHub credentials outside all tracked files.

## First-resource preflight and deployment

Use the default `~/.local/state/build-targets/aws` directory before and after registry activation.
The default bundle directory is `~/.local/state/aws-build/deployment`.
The default settings file is `~/.claude/config/aws_target.conf`.
Never delete its ledger, inventory, charges, claims, or deployment evidence to permit a retry.

```sh
cd "$HOME/.claude"
export AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials"
python3 scripts/aws_target/deploy.py prepare
```

Preparation runs locally.
It records priced inventory, current price evidence, generated parameters, and a deployment-purpose guard decision.
Inspect the generated public template, parameters, and preflight evidence.
Confirm the monthly Budget amount equals `monthly_limit_usd`.
Confirm email thresholds are 50, 80, and 100 percent.
Confirm the stop action threshold equals `budget_stop_percent`.
Confirm the Budget and IAM enforcement foundation precede compute and storage creation.
The native Budget stop action follows the instance ID because it requires that ID.
The synchronous deployment guard protects the earlier resource-creating stages.
Confirm the configured cost tag appears on the instance, root volume, Spot request, and retained data volume.
Confirm the instance policy names only the selected instance ARN for start and stop.

Run the guarded deployment yourself:

```sh
python3 scripts/aws_target/deploy.py apply --approve-resources
```

The helper rechecks live limits before each resource-creating stage.
It runs `aws cloudformation deploy` for the foundation before the compute stage.
It reconciles stack outputs on a retry.
If preparation expires or inputs change, run preparation again.
A prior allowed decision does not permit deployment after a live limit becomes invalid.
Record the stack, instance, persistent request, and data volume IDs.
Set `instance_id` from the confirmed stack output.
Keep the original priced inventory across reruns.

## First boot, SSM, and retained data

The official Amazon image supplies SSM.
The public bootstrap enables Tailscale and an idle-stop path before natedev control starts.
The remote bootstrap enables its own lifecycle switch.
It publishes no admission permission.
Execution remains refused until natedev binds valid permission to that boot.

1. Confirm the instance appears in SSM.

```sh
aws --profile build-admin ssm describe-instance-information --region REGION \
  --filters Key=InstanceIds,Values=INSTANCE_ID
aws --profile build-admin ssm start-session --region REGION --target INSTANCE_ID
```

2. Use the EC2 serial console if SSM remains unavailable.
3. Confirm the attached data volume ID before any filesystem operation.
4. Confirm the bootstrap checks the volume identity and existing filesystem.
5. Confirm ext4 label `aws-home` and the `/home` mount.
6. Confirm the expected EBS ID, encryption, capacity, IOPS, and throughput.
7. Confirm that the root volume holds NixOS and the Nix store.
8. Confirm that the remote lifecycle service runs without a published generation.
9. Observe idle shutdown within `idle_stop_minutes` before publishing permission.
10. Confirm the stopped state through EC2.

Restart the instance for the next setup interval:

1. Keep `offload=off` and the controller service disabled.
2. Run preparation again.

```sh
python3 scripts/aws_target/deploy.py prepare
```

3. Run the guarded setup start yourself.

```sh
python3 scripts/aws_target/deploy.py setup-start --approve-resources
```

The helper requires fresh preparation and the private mode `0600` credential file.
It checks the saved stack identity, instance ID, data volume ID, and instance state.
It repeats the deployment guard immediately before the start.
It preserves `offload=off` and publishes no remote permission.
It preserves the ledger and deployment evidence.

The bootstrap selects the NVMe device by its EBS serial.
The CloudFormation attachment name does not determine the Linux device name.
The [AWS EBS device guide](https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html) describes this mapping.
Its remote service requires the `/home` mount.
A missing mount must not create mirrors on the root filesystem.
SSM remains available for mount recovery.
Repeat the idle-stop check after bootstrap or lifecycle changes.

## Tailscale policy and branch handoff

Run this command yourself through SSM:

```sh
sudo tailscale up --ssh --advertise-tags=tag:build-aws
```

Use the [Tailscale SSH policy documentation](https://tailscale.com/docs/features/tailscale-ssh) when editing the tailnet policy.
Give `tag:build-aws` no initiated connections to natedev or the Mac.
Remove broader grants that would permit those connections.
Permit natedev's user to reach `tag:build-aws`.
Permit that user to enter the box's `natepiano` account through Tailscale SSH.
Test the permitted and denied directions in the policy console.
Do not store a Tailscale auth key on either branch.

natedev is already rebuilt. On 2026-11-03 it switched to `/etc/nixos` 10353a9, which holds the patch commit 78746da.
`aws-target.service` is installed on natedev and stays disabled until go-live.

1. Give the natedev showrunner the prepared `aws-build` branch and the director's commit.
2. Have the showrunner inspect the patch and the box's Nix evaluation result.
3. Rebuild the box yourself from the reviewed branch.
4. Keep the controller service inactive during the box activation.
5. Confirm SSM, Tailscale SSH, `/home`, and the remote lifecycle service after activation.

Install the reviewed public NixOS branch on the box before its user-run activation.
Use the `aws-build` output for that activation.
Do not copy credentials, private host keys, or a live machine's configuration directory.
The showrunner must supply the committed branch identity before the activation.

Agents must not activate a system or modify the live NixOS checkout.
The prepared AWS host installs Python 3.13, Nix, Git, rsync, Rust, nextest, and mend.
Its pinned Rust overlay and cargo-liner source supply the Rust tools.
A successful Nix evaluation proves neither package builds nor runtime tool parity.
If tool versions differ, prepare matching packages and repeat the user checks before enabling work.

## Update public scripts before the registry

The bootstrap supplies the first installation before registry activation.
Use this procedure to update the public scripts later.

1. Confirm the scripts and public settings belong to the reviewed tracked commit.
2. Copy the tracked `scripts/` and `config/` trees to the box.
3. Copy the script commit marker.
4. Keep `BUILDLOG_PEER` unset in the normal multi-peer service.

```sh
cd "$HOME/.claude"
ssh aws-build 'install -d -m 0700 "$HOME/.claude"'
git archive HEAD scripts config | ssh aws-build 'tar -x -C "$HOME/.claude"'
git rev-parse HEAD | ssh aws-build 'cat > "$HOME/.claude/.build-target-script-commit"'
```

The sender later copies tracked scripts, settings, and source before each reserved execution probe.
It selects `devshell` or `devshell.<repo>` from the live configuration.
It evaluates only cached shell inputs rather than a mirror containing build output.

## Registry and controller activation

1. Keep natedev's `offload=off`.
2. Set `kind.aws=ssh`, `platform.aws=linux`, and `settings.aws=aws_target.conf` in `config/build_targets.conf`.
3. Add `aws` to the ordered `targets` list.
4. Confirm record exchange while control remains disabled.

```sh
"$HOME/.claude/scripts/buildlog/buildlog" sync
"$HOME/.claude/scripts/buildlog/buildlog" report
python3 "$HOME/.claude/scripts/build_targets/build_targets.py" status
```

An active AWS registry row enables exchange even while offload is off.
It does not permit control actions or charge queries.
With no AWS registry row, status and reports show no AWS target section.
Confirm each peer's last successful exchange and latest failure.
Confirm a new peer shows its never-synchronized state.
Use `buildlog sync` to retry an exchange.
Use `buildlog sync resume` only when the global pause should end.
A Mac exchange failure must not hide AWS exchange status.
An AWS exchange failure must not hide Mac exchange status.

1. Confirm the live spending limits, price freshness, inventory, and private credential mode.
2. Set natedev's `offload=on`.
3. Enable the reviewed controller service yourself.
4. Confirm a current allowed decision and fresh boot-bound remote permission.

```sh
systemctl --user enable --now aws-target.service
systemctl --user status aws-target.service
journalctl --user -u aws-target.service
```

The service has no boot dependency until the user enables it.
Its `ExecStart` uses the Nix store's Python executable with these arguments:

```sh
python3 "$HOME/.claude/scripts/aws_target/aws_target.py" serve --settings "$HOME/.claude/config/aws_target.conf" --directory "$HOME/.local/state/build-targets/aws"
```

The service sets `AWS_SHARED_CREDENTIALS_FILE=%h/.config/aws-build/credentials`.
The AWS adapter selects the configured credential profile explicitly.
The remote service uses these arguments:

```sh
python3 /home/natepiano/.claude/scripts/aws_target/remote.py serve --settings /etc/aws-build/remote.conf
```

The controller reads `tick_interval_s` through `conf.py` during each cycle.
The remote service reads `interruption_poll_s` during each cycle.
Both services stop when their own offload switch becomes off.
Keep the controller service disabled until all preceding checks pass.
Confirm `go-live.json` records the first permitted running observation.
Keep that timestamp across restart and redeployment.

## User-run routing and parity proof

Use a small approved repository and package.
Select AWS through normal admission and claims.
Do not bypass permission or delete another slot's output.

1. Record the configured shell selector and the selected repository slot.
2. Confirm the reserved execution probe checks `rustc -V` inside that shell.
3. Confirm it checks `cargo nextest --version` and `cargo mend --version` inside the same shell.
4. Confirm it checks the tracked scripts commit.
5. Run cold test, check, and staged doc commands through the normal route.

```sh
"$HOME/.claude/scripts/direct/verify.sh" test PACKAGE --no-cache
"$HOME/.claude/scripts/direct/verify.sh" check PACKAGE
"$HOME/.claude/scripts/direct/verify.sh" doc
```

6. Read each detached result before proceeding.
7. Confirm `target=aws` and `host=aws-build` in the records.
8. Repeat the commands to test cached shell inputs and warm build output.
9. Confirm staged doc uses the staged tree identity.
10. Confirm no AWS result becomes natedev pass evidence.
11. Confirm trimming affects only the selected slot's output.
12. Confirm other slots retain their mirrors and warm output.
13. Confirm whole-folder removal occurs only for the configured disk floor.

Do not manufacture success by forcing a local route or changing a parity pin.
A refusal, missing completion, confirmed failure, and interruption loss have distinct results.
An interrupted child prints one `aws_interrupted=` line after completion and cleanup evidence.
The remote process returns the child status.
The sender records interruption loss and preserves the original queue timestamp.

## Status, recorder health, and reports

1. Read build status after active, stopped, starting, draining, and retained-capacity cases.
2. Confirm spending state, decision time, evidence time, and recovery action.
3. Confirm occupied and retained slots appear separately for each repository.
4. Confirm completion, cleanup state, unresolved count, and attempt evidence time.
5. Confirm a completed test with unconfirmed cleanup still retains its slot.
6. Read the report after an exchange.
7. Confirm the AWS data synchronization time and recorder health.
8. Confirm the report shows unavailable comparisons when data is missing.

Cost Explorer queries cover whole UTC days with date-only bounds.
The ledger reconciles the exact queried UTC period.
Accounting-day costs use each reconciliation's overlap in `accounting_zone`.
Comparison days use the report's local timezone.
The report includes query fees and reconciliation differences without adding actual costs twice.
Its wait-saving estimate preserves negative runtime differences.
The before/after comparison remains unmatched for load.
Inspect these consequences in the report rather than treating charge files as user controls.

## Restart and retained-ownership recovery

1. Record the instance generation, affected claims, slot markers, and retained volume ID before stopping.
2. Stop the instance while the controller remains enabled.
3. Confirm EC2 reports `stopping`, `stopped`, or `terminated` before claim release.
4. Confirm release evidence binds the target, generation, host, and exact affected claims.
5. Confirm running, unknown, or unreachable evidence preserves claims.
6. Restart through the guarded controller after spending permission returns.
7. Confirm the same `/home` volume, mirrors, affinity, ledger, charges, and peer status remain.
8. Confirm old markers carry their original boot identity.
9. Confirm recovery does not select or kill new work that reuses a PID.
10. Confirm uncertain marker recovery retains the claim and marker.
11. Confirm later claims survive an earlier stopped-state release.
12. Confirm fresh permission names the new boot before any shell evaluation.

Never delete `.linux-run.json`, claims, or accounting evidence to force recovery.
Use the recovery action shown by build status.
For an interrupted boot, confirm the controller stops an unexpected running generation when spend is refused.
Confirm a controlled drain keeps its original deadline across repeated refused ticks.

## Retained volume reuse and replacement

Use this replacement procedure from the deployment owner:

1. Stop and delete the old stack.
2. Keep its retained Budget and DataVolumeId.
3. Set a new stack_name and existing_data_volume_id to the saved DataVolumeId.
4. Preserve the ledger directory.
5. Render bootstrap, run prepare, then apply --approve-resources.

Confirm the old instance stops before deleting its stack.
Use your administrator profile for these user-run commands:

```sh
aws --profile build-admin ec2 stop-instances --region REGION --instance-ids OLD_INSTANCE_ID
aws --profile build-admin ec2 wait instance-stopped --region REGION --instance-ids OLD_INSTANCE_ID
aws --profile build-admin cloudformation delete-stack --region REGION --stack-name OLD_STACK_NAME
aws --profile build-admin cloudformation wait stack-delete-complete --region REGION --stack-name OLD_STACK_NAME
```

Keep the data volume in the same availability zone.
Preserve its filesystem identity, encryption, and cost-allocation tags.
Keep the existing ledger, priced inventory, and saved deployment record.
Do not change the volume selection inside an existing stack.

After recording the new stack name and saved volume ID, run these commands:

```sh
python3 scripts/aws_target/deploy.py bootstrap
python3 scripts/aws_target/deploy.py prepare
python3 scripts/aws_target/deploy.py apply --approve-resources
```

apply confirms that the old stack is absent.
It refuses an unselected retained volume or a second active stack.
The new deployment record replaces the stack identity after confirmed deployment.
The ledger and accounting evidence remain in the same directory.

1. Confirm the new instance mounts the same data at `/home`.
2. Configure the replacement scaler credential at your terminal after its scoped IAM user exists.
3. Repeat boot permission, tool parity, exchange, and retained-ownership checks before admitting work.

Stack deletion and replacement retain the data volume and Budget.
They do not retain the instance or its root volume.
Retention continues to incur storage and performance charges.
The guard and Budget do not remove that data automatically.

## Manual stop and rollback

Keep offload and control enabled until stopped evidence is confirmed.
`Controller._action` returns immediately when offload is off, including manual stop.

```sh
cd "$HOME/.claude"
AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials" \
  python3 scripts/aws_target/aws_target.py stop
aws --profile build-scaler ec2 describe-instances --region REGION \
  --instance-ids INSTANCE_ID \
  --query 'Reservations[].Instances[].State.Name'
aws --profile build-scaler ec2 describe-spot-instance-requests --region REGION \
  --filters Name=instance-id,Values=INSTANCE_ID
```

If the controller fails, use your terminal or the EC2 console to stop the instance.
Confirm its stopped state before changing admission or services.
Verify that a user stop leaves the persistent request inactive and does not restart the instance.
The [AWS Spot lifecycle documentation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/using-spot-instances-request.html) describes stop and start behavior.
The user-run check proves the deployed request's behavior.
AWS can restart capacity after its own interruption.
The guard stops an unexpected generation when spend is refused.
Remote idle shutdown supplies the second bound.
The Budget stop action remains a delayed billing backstop.

1. Confirm stopped-state claim recovery through build status.
2. Set natedev's `offload=off`.
3. Disable and stop the controller service.

```sh
systemctl --user disable --now aws-target.service
systemctl --user is-active aws-target.service
```

The service must show `inactive`.

4. Remove AWS from `targets` only if record exchange should also stop.
5. Preserve claims, affinity, warm mirrors, cost evidence, go-live time, and peer status.
6. Keep retained-volume identity and tags for later reuse.
7. Confirm the instance remains stopped and the service remains inactive.
8. Confirm natedev and Mac routing remain usable.

Do not terminate or delete retained resources as an automatic rollback step.
Any later deletion requires a separate user decision.
