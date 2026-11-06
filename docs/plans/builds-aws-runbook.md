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
Keep `daily_limit_usd=20` (derived: the monthly cap / 20; the user approved it on 2026-11-03). Do not raise it.
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
3. Run `python3 scripts/aws_target/deploy.py secrets` in your terminal to encrypt the administrator keys.
4. Read the free placement scores for `m7a.4xlarge` in `us-east-2`.
5. Read current Spot, EBS, gp3 performance, root storage, and public IPv4 prices.
6. Select one available zone in `us-east-2` from those reads.
7. Read the standard Spot vCPU quota. Request an increase if it is below 16.
8. Run `python3 scripts/aws_target/deploy.py up --approve-resources` for deployment.
   Deployment refuses a price older than `price_max_age_seconds`.
   Confirm that `notification_email_file` names an existing private file before deployment.
9. Confirm that the fixed components sum to `fixed_usd_per_month`.
10. Confirm that `limit_reserve_usd` covers the configured drain and idle intervals.

A placement score for one instance type is always low. AWS needs at least three types for a useful score. On 2026-11-03 every `us-east-2` zone scored 1, so the zone choice uses price. The user approved `us-east-2c` (`use2-az3`) at $0.2250/h on 2026-11-03.

A new account's "All Standard Spot Instance Requests" quota (`L-34B43A08`) defaults to 5 vCPU. `m7a.4xlarge` needs 16. A start below the quota fails with "Max spot instance count exceeded".

The account (created 2026-11-03) is a project account. The user activated advanced features on 2026-11-03. Its service control policies now set these limits:

- `RegionFloor` allows only `us-east-1`, `us-east-2` and `us-west-2`. The policy denies most actions in `us-east-1`, the Pricing API included. Use `us-east-2`, and read prices from the public AWS price pages.
- No policy denies Spot. Activation removed the Paid Plan policy. A `run-instances --dry-run` with `MarketType=spot` in `us-east-2` succeeded on 2026-11-03.
- The AWS spend limit is gone. The stack's $400 Budget with its stop action is the AWS-side backstop.
- The four `BudgetsSpendLimit*` policies must have no target. In the Organizations console, open each policy's Targets tab, and detach the project account if it is listed.

Create or update the encrypted administrator file at your terminal.
Never send a credential or passphrase to a session.

```sh
export AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials"
python3 scripts/aws_target/deploy.py secrets
```

The command asks for four hidden values and a passphrase confirmation.
Enter retains an imported or stored value.
The command removes the plain administrator profile after encryption succeeds.
It keeps the unattended scaler profile.
Use the AWS console for placement and quota inspection.
Do not create a plain administrator profile for CLI inspection.

Read gp3, root storage and public IPv4 prices for `us-east-2` from the public [EC2](https://aws.amazon.com/ec2/pricing/on-demand/) and [EBS](https://aws.amazon.com/ebs/pricing/) price pages.
Use the price history to select the zone.
The `up` command reads the current price before stack preparation.
The command saves the price and its read time outside the repository.
Do not edit tracked files to record the price.
Price-list and placement reads do not launch capacity.
Cost Explorer reads have the configured query fee.

## Official AMI and required inputs

1. Open the [official NixOS AMI list](https://nixos.github.io/amis/).
2. Select the configured NixOS release, region, and x86_64 architecture.
3. Record the AMI ID and its official owner ID in configuration.
4. Verify the AMI in the AWS console.

The result must show the selected release, `x86_64`, and `available`.
Record the image's root device name.
Do not substitute an unverified third-party image.

1. Fill `region`, `availability_zone`, `ami_id`, `ami_owner_id`, and `nixos_release`.
2. Fill `vpc_id`, `subnet_id`, and the deployment profile.
   Write the Budget address to its private file at your terminal:

   ```sh
   (umask 077; mkdir -p ~/.config/aws-build; printf '%s\n' '<address>' > ~/.config/aws-build/notification-email)
   ```

   Keep the address outside the public repository. Never put it in a tracked file, commit, message, or test fixture.
3. Confirm public HTTPS egress and a usable route for SSM and public packages.
4. Permit inbound UDP 41641 for Tailscale transport.
5. Permit no inbound TCP ports.
6. Activate the configured cost-allocation tag in the billing console.
7. Keep its key and value unchanged on retained resources.
8. Keep the saved deployment record for retained-volume replacement.
9. Set the public bootstrap source as described below.
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

The `up` command renders the bootstrap and remote settings before it prepares the stacks.

Inspect the generated bootstrap file and remote settings.
They must contain no credential or published admission permission.
The generated settings enable remote lifecycle checks and leave execution unavailable.
The bootstrap saves the identified lifecycle settings in `/var/lib/aws-build/remote.conf`.
Both host configurations expose that file through `/etc/aws-build/remote.conf`.
The remote service reads this separate file.
Sender copies cannot disable its idle shutdown through natedev's `offload=off` setting.
The sender copies every tracked file under `scripts/` and `config/`.
This includes `config/build_folders.conf`, which the remote service and Linux runner need during import.
A version refusal names each mismatched tool, both versions, and the file set by `version_pin_file`.
Move the box pins in that file before you retry the step. A rustc difference does not refuse a step: each job uses its tree's toolchain.
The controller also stops a running box after `idle_stop_minutes` without published permission when no target claim remains.
It saves the first unpublished time for each generation in `controller.json`.
A successful publication or a new generation resets this timer.
The stop reason is `unpublished_idle`.

## Private scaler credential file

The scaler uses one instance-scoped policy and the `build-scaler` profile.
It can start or stop that instance and perform the required reads.
Those reads include `ec2:DescribeSpotInstanceRequests` for the rollback check.
It has no general deployment permission.
The box receives an SSM instance profile and no scaler key.

The `up` command checks the scaler profile through STS.
It creates a key only when the profile does not identify the configured scaler user.
It writes the key to the existing private credential file with mode `0600`.
It keeps every unrelated profile.
The administrator key stays in `admin.gpg`.
It verifies the new key before it deletes the old keys.
It deletes a new key that fails verification.
Do not copy the credential file to the box or print its contents.

## Deployment

Use the default `~/.local/state/build-targets/aws` directory before and after registry activation.
The default bundle directory is `~/.local/state/aws-build/deployment`.
The default settings file is `~/.claude/config/aws_target.conf`.
Keep the encrypted administrator file and the private Budget address file in place.
Run this command at your terminal:

```sh
AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials" python3 scripts/aws_target/deploy.py up --approve-resources
```

The command runs eleven ordered stages through activation and routing proof.
It prints `present`, `done`, or `refused` for each stage.
It stops at the first refusal.
Correct the reported problem and run the same command again.
A retry uses the completed stages and preserves the ledger.
If AWS reports `InsufficientInstanceCapacity`, `up` waits `capacity_backoff_s` between start attempts until AWS accepts the start or the user interrupts.
The command refreshes the price on every run.
A complete target requires no stack deployment or key creation on a later run.
The command stores each stage result and its times in `up.json`.
The command stores stack, instance, and volume identities in `deployment.json`.
Do not copy resource IDs into tracked configuration.
Never delete the ledger, inventory, claims, or deployment record to permit a retry.
The spending guard runs before each resource request.
A foundation wait renews preparation only when its inputs remain unchanged.
A stack in `REVIEW_IN_PROGRESS` has no resources and counts as absent.
Other incomplete stack states require recovery before deployment.
Separate `price`, `bootstrap`, `prepare`, `apply`, and `setup-start` commands remain available.
The command completes the box setup and records both proofs for the current instance ID.

## First boot, SSM, and retained data

The official Amazon image supplies SSM.
The public bootstrap enables Tailscale and an idle-stop path before natedev control starts.
The remote bootstrap enables its own lifecycle switch.
It publishes no admission permission.
Execution remains refused until natedev binds valid permission to that boot.

1. Confirm the instance appears in SSM.

Use the AWS console session manager for manual SSM access.

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
python3 scripts/aws_target/deploy.py price
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
Linux mirrors use `~/.local/state/build-target/mirror/<repository>/<target>-s<slot>`, from the build queue rule.
The repository folder joins the policy name and the identity hash.
Worktrees and gate copies with the same policy name and identity share one mirror per target and slot.
The Mac keeps its source folder name rule.
Repeat the idle-stop check after bootstrap or lifecycle changes.

## Tailscale policy and box configuration

`deploy.py up --approve-resources` now runs the policy, join, configuration, and checks stages.
The helper decrypts the OAuth values from `admin_secret_file`.
The file stays outside the repository with mode `0600`.
The OAuth client needs three scopes: `auth_keys` for `tag:build-aws`, `devices:core` for `tag:build-aws` (device deletion in `down`), and `policy_file`.
Never paste its secret into a command, log, or tracked file.

1. Confirm the natedev and Mac device names in `config/aws-build-tailscale.json`.
2. Confirm the clean `main` checkout at `nixos_configuration_directory` contains the reviewed box configuration.
3. Run `python3 scripts/aws_target/deploy.py up --approve-resources` yourself.

Device names in `config/aws-build-tailscale.json` must equal a machine name (first label of `DNSName` in `tailscale status`); `up` checks this first.

The policy stage prints each change to `tag:build-aws` rules.
The network rule permits only natedev and the Mac on port 22.
The SSH rule permits `autogroup:member` as `natepiano`; the network rule still limits access to these devices.
HTTP refusals include the status and validation details without user fields; network refusals include the failure reason.
It preserves unrelated rules and refuses a broad outbound rule that needs policy review.
The join stage waits for SSM and sends one short-lived auth key through SSM stdin.
`box-join` writes the box host keys from Tailscale to `~/.ssh/known_hosts` for the host name and address; box setup refusals show the last stderr line.
The key never enters a command argument or a disk file.
The configuration stage uses substitutes during closure transfer.
It holds idle shutdown during the copy and detached switch.
A bounded poweroff timer protects a failed SSH reconnect.
The stage confirms the running system and restarts `aws-build-remote` before it removes the timer.
The checks stage confirms SSM, the expected `/home` volume, the remote service, and plain tool versions on both hosts.
The stage uses the tools on PATH without another repository or development shell.
Each build still checks its own repository shell.
A refusal stops `up` and records the stage in `up.json`.
Correct the reported cause and run `up` again.
Completed stages report `present`.

Agents must not activate a system or modify the live NixOS checkout.
The helper reads the NixOS checkout only when the user runs `up`.
Controller activation and the routing proof remain separate plan stages.

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
Use `--target aws` to select AWS for one step.
The step keeps the normal admission, spending, and version checks.
The step never runs on natedev.
One waiting pinned step gives the controller immediate work to start the box.
Do not bypass permission or delete another slot's output.

1. Record the configured shell selector and the selected repository slot.
2. Confirm the runner checks `rustc -V` inside that shell, in the mirror, after it installs the tree's toolchain.
3. Confirm the reserved execution probe checks `cargo-nextest nextest --version` and `cargo-mend mend --version` inside the same shell.
4. Confirm it checks the tracked scripts commit.
5. Run pinned test, check, and staged doc commands.

```sh
"$HOME/.claude/scripts/direct/verify.sh" test PACKAGE --target aws
"$HOME/.claude/scripts/direct/verify.sh" check PACKAGE --target aws
"$HOME/.claude/scripts/direct/verify.sh" doc --target aws
```

6. Read each detached result before proceeding.
7. Confirm `target=aws`, `host=aws-build`, and `pinned=true` in the step records.
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
3. Set a new `stack_name`. The helper reads the retained volume ID from the saved deployment record.
4. Preserve the ledger directory.
5. Run `deploy.py up --approve-resources`.

Confirm the old instance stops before deleting its stack.
Use the AWS console for manual stack deletion.
Use `deploy.py down --approve-destroy` only when you want to discard the build cache and remove the complete target.
Do not restore a plain administrator profile.

Keep the data volume in the same availability zone.
Preserve its filesystem identity, encryption, and cost-allocation tags.
Keep the existing ledger, priced inventory, and saved deployment record.
Do not change the volume selection inside an existing stack.

After setting the new stack name, run this command:

```sh
python3 scripts/aws_target/deploy.py up --approve-resources
```

apply confirms that the old stack is absent.
It verifies the saved retained volume and refuses a second active stack.
The new deployment record replaces the stack identity after confirmed deployment.
The ledger and accounting evidence remain in the same directory.

1. Confirm the new instance mounts the same data at `/home`.
2. Confirm the scaler-key stage reports `done` or `present`.
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

## Activation and proof stages

Configuration, image creation, and the idle stop proof close AWS routing before they change the box.
Each stage waits without a time limit for AWS jobs that still run.
Kept claims do not delay the wait.
A stage with a valid proof leaves routing unchanged.
Activation opens routing after the idle stop proof.
An interrupted `up` leaves routing closed until the next `up` reaches activation.
Jobs use natedev and the Mac while AWS routing stays closed.

Run `python3 scripts/aws_target/deploy.py up --approve-resources` at your terminal.

1. Allow `idle_stop_minutes` plus `idle_stop_margin_s` for the idle-stop proof.
   Without a saved proof, the stage waits for pending or stopping instances to reach a stable state.
   It starts a stopped instance through the existing spending checks and waits for health.
   The command observes EC2 while the box stops itself.
   A job that starts before routing closes resets the proof deadline to the full duration.
   A timeout makes `up` stop the instance and report a refusal.
2. The activation stage writes `~/.local/state/build-targets/aws/routing.json` and enables `aws-target.service`.
   The reviewed registry already contains `targets=mac,natedev,aws`, `kind.aws=ssh`, `platform.aws=linux`, and `settings.aws=aws_target.conf`.
   The command refuses missing rows and edits no tracked file.
3. The routing proof runs `verify.sh test aws-routing-proof --target aws --no-cache` in a scratch Git repository.
   The folder uses task `gate:aws-routing-proof`.
   The next `up` removes a folder left by a dead run.
   `scripts/aws_target/routing_proof.py` supplies the crate files.
   The normal queue keeps spending, admission, and per-run tool checks.
   The crate has no Rust dependencies and uses its own locked `default` shell with the host tools.
   The stage needs no other source repository.
   The command requires a new successful pinned AWS step in the buildlog.

A retry skips proofs recorded for the current instance ID.
A replacement instance requires new proofs.
Keep `routing.json`, `deployment.json`, and `up.json` outside the repository.

## Encrypted administrator credentials

1. Run `deploy.py secrets` in your terminal before the first encrypted deployment.
2. Enter the AWS administrator key and Tailscale OAuth values without echo.
3. Enter the encryption passphrase twice.
4. Run `deploy.py up --approve-resources` with the scaler credential file environment.

The default encrypted path is `~/.config/aws-build/admin.gpg`.
The helper uses symmetric AES256 encryption and disables the GPG symmetric passphrase cache.
It refuses a terminal-free process and never uses cached public-key decryption.
Each deployment asks for one passphrase at its first administrator request.
`up` always needs this passphrase because it refreshes the price and checks live state.
`price`, `apply`, and `setup-start` also need it.
`prepare` needs it only when existing resources require an AWS read.
`bootstrap` and normal builds never need it.
A remaining plain administrator profile or `tailscale-oauth` file causes a refusal that names `deploy.py secrets`.
The helper stores decrypted values only in memory.
The local AWS subprocess environment contains the AWS key while the subprocess runs.
Do not inspect or capture that environment.

## Remove the complete target

The dry run changes nothing and requires no passphrase.

```sh
python3 scripts/aws_target/deploy.py down
```

The plan states that the `/home` build cache is lost.
Run the approved removal at your terminal.

```sh
AWS_SHARED_CREDENTIALS_FILE="$HOME/.config/aws-build/credentials" python3 scripts/aws_target/deploy.py down --approve-destroy
```

The helper disables routing and the controller before checking jobs that still run.
Let these jobs finish before another removal attempt.
Kept claims do not delay removal.
The helper deletes the tagged Tailscale device, scaler keys, compute resources, foundation, retained volume, and retained Budget.
The helper removes only the scaler profile from the plain credentials file.
It keeps the policy rules, encrypted administrator file, ledger, and charge history.
The helper removes deployment records, proofs, and prepared bundle files after resource deletion.
The OAuth client needs device deletion access for `tag:build-aws`.
Enter the administrator passphrase once when requested.
Correct any reported failure and repeat the same removal command.
Do not remove `down.json` to bypass incomplete removal.
Use the normal `up --approve-resources` command to create the target again.
Past spending still counts against the same caps.


### Deployment ownership and changed setup inputs

Preparation saves a deployment ID in deployment.json.
The helper records the caller account and region before creating resources.
A CREATE change set reserves the StackId before the foundation deploy creates resources.
The helper saves that StackId before resource creation.
The helper records the scaler resource's IAM name, ARN, and UserId from the verified stack.
The confirmed Tailscale node ID stays with its instance ID in the deployment record.
The data volume has a DeploymentId tag.
A new Budget name includes the deployment ID.
Migration preserves the existing Budget name.
The deployment record saves the actual volume ID and Budget name.

Removal checks the saved account, region, and StackId before changing routing or deleting resources.
Missing or mismatched ownership evidence causes a refusal.
Removal uses the StackId for every stack mutation.
Removal deletes only the saved Tailscale node after checking tag:build-aws.
Removal checks all required saved identities before changing routing or writing `down.json`.
A missing identity causes a refusal that names `deploy.py up` as the recovery command.
This refusal leaves `up` available to record the missing identity.
One exception applies when the box never joined Tailscale.
The record must have no node ID, and `up.json` must have no successful box-join stage.
The Tailscale API must list no device with hostname `aws-build` and tag `tag:build-aws`.
Then removal reports `tailscale-device: no device` and deletes no device.
Any matching device without a saved node ID causes a refusal before routing changes.
Removal resolves ScalerUser through the verified stack and checks its saved ARN and UserId before deleting keys.
Removal requires the saved volume ID and a matching DeploymentId tag.
Removal uses the saved Budget name after a live check confirms that name.
Removal never selects a device or resource through a name or tag alone.
Removal refuses a stale down.json with another deployment ID.
A completed removal keeps a public down-complete.json receipt for repeated commands.
The next up creates a new deployment identity.
A reused volume requires its previous DeploymentId receipt, or no DeploymentId tag.
The helper verifies the volume and sets its new DeploymentId before attachment.

An interrupted removal can leave a retained volume that continues to incur charges.
AWS status shows down incomplete, the retained resource IDs, and their daily storage cost.
It shows the action deploy.py down --approve-destroy.
The spend ledger keeps counting retained storage during the interruption.
The helper does not create a background removal job.

The box-configuration proof includes the evaluated desired system path from /etc/nixos.
The helper evaluates that path once during each up run.
The box-tools proof includes natedev's normalized tool versions.
Changed inputs invalidate that stage and every later proof.
A stopped instance must complete the changed checks before the helper accepts its old proofs.

### Legacy identity migration and reservation recovery

An existing deployment without StackId can migrate through up.
The helper checks the configured region and the caller account against the live StackId.
The helper requires a complete stack and the saved physical InstanceId.
The helper checks the volume, Budget, and scaler identities before it changes state.
The helper saves these identities and tags the verified volume with DeploymentId.
The existing Budget name and ExistingDataVolumeId parameter stay the same during the update.
The box-join stage records the confirmed Tailscale node ID.
A failed migration check changes no resource or deployment record.
A missing deletion identity requires deploy.py up before deploy.py down.

Before CREATE, the helper checks identity-<deployment_id> for a previous reservation.
It verifies the saved DeploymentId and the StackId account before it saves the StackId.
It resumes the change-set waiter without a second CREATE request.

Existing stack updates preserve the previous Bootstrap when an InstanceId exists. Bootstrap applies only at first boot.
The box-configuration stage changes a live box.

The helper inspects an UPDATE change set before execution. It refuses protected resource removals and replacements, including conditional replacements.
The protected types include the instance, volumes, volume attachments, network interfaces, interface attachments, Elastic IPs, IP associations, and Budget.
Use `deploy.py down --approve-destroy`, then `deploy.py up --approve-resources` when replacement is required.
A refusal deletes an unexecuted change set. A cleanup failure preserves the original error and names the remaining change set.
An empty change set succeeds without execution. A new stack uses the current Bootstrap and the existing CREATE reservation path.

Before a new compute request, the helper reconciles every outstanding request against the live stack.
A completed rollback with an InstanceId confirms the instance. An operation in progress or a failed rollback refuses with its status.
CREATE reservation failures and UPDATE failures include current failed resource events, the change-set reason, and the final stack status.
A stable failure reconciles the compute request at once. A timeout or uncertain stack status keeps the request pending.
The helper redacts the notification address from the prepared parameters. It does not read the private address file during failure reporting.
The same administrator session reads failure details without another passphrase prompt.
