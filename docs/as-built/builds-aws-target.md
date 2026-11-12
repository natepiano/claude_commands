# AWS cloud gates and former build target

## What it is

The user removed the permanent AWS build box.
Its instance, Tailscale device, and data volumes are gone.
Build jobs use `mac` and `natedev` only, in that order.
Cloud gates still run merge gates and targeted tests on temporary AWS instances.
Cloud gates use `~/.local/state/build-targets/aws` without an `aws` target registration.
Cloud gates boot the official NixOS image and fetch their tools at boot.
A saved gate image was taken out of use. Five gates with it had a 32 s median; gates without it had 29 s.
`deploy.py remove-images --approve-destroy` switches the gates to the official image. It deletes the saved gate image, the former builder image, and their snapshots.

The former builder used host `aws-build` and one persistent Spot `m7a.4xlarge` instance.
It used NixOS and a retained, encrypted 500 GiB gp3 data volume at `/home`.
The repository retains its controller and deployment code.

The spending caps remain in `config/aws_target.conf`: `monthly_limit_usd=400` and `daily_limit_usd=20`.
The user approved the monthly cap on 2026-11-01 and the daily cap on 2026-11-03.
The daily cap derives from the monthly cap divided by 20.
No code may raise either cap or copy it.

The user approved these former builder settings on 2026-11-03.
The settings remain in `config/aws_target.conf`:

1. `limit_reserve_usd=3`: the spend reserve below each cap.
2. `drain_max_minutes=60`: at a cap, no new steps start, and the box stops within 60 minutes.
3. `spot_max_usd_per_hour=0.483`: the Spot maximum price.
4. `start_min_waiting=2`, `start_after_wait_s=120` and `idle_stop_minutes=15`: the box starts when 2 steps wait 120 s, and stops after 15 idle minutes.
5. `slots=2`, `slots.hana=4`, `slots.cargo-liner=4` and `build_budget_gib=32`: the slots per repository and the build space.

The user approved `notification_email_file=~/.config/aws-build/notification-email` on 2026-11-03. aws wrote that private file on natedev (mode 0600) from the address the user gave.
The user approved `deployment_price_file=~/.config/aws-build/deployment-price` on 2026-11-03. `deploy.py price` writes the Spot price and its read time there (mode 0600), outside the repository.

The shipped registry contains `targets=mac,natedev`.
The old `offload=on` setting does not register the former AWS target.
The user approved its historical activation on 2026-11-03.
[The runbook](../plans/builds-aws-runbook.md) and [source design](../plans/builds-aws-design.md) record the earlier setup.
The design's Phase 4, lint with write-back, was not built.

## Current state and removal

`report.py` reads the AWS state folder without target registration.
Its AWS section retains the account bill, daily gate spend, and historical builder comparisons.
`sync.hosts.aws=aws-build` maps historical steps to those comparisons without registering the former builder.
The AWS section shows no sync status because the permanent box no longer exists.

`deploy.py down --approve-destroy` can remove the remaining gate resources without restoring the former target registration.
Teardown resolves the former builder's claim and lock paths directly from the AWS state folder.
It preserves the existing lock and live claim checks.

The controller no longer releases claims after a box stop or generation change.
The code no longer writes `release-evidence/` through that path.

## Retained builder implementation

The following sections describe retained code and the historical builder setup.
No permanent box runs these services.
Legacy activation requires an explicit target registration.

### Target state, slots and records

- `mac_test.state_paths(target)` is the one map from a target to its state folder. The Mac keeps `~/.local/state/mac-test`. Every other target uses `~/.local/state/build-targets/<target>/` for claims, locks, block and unreachable backoff.
- `mac_test.configured_ssh_target(target)` checks every target, the Mac too, against `config/build_targets.conf`. It refuses an unknown or non-ssh name before any state write. `build_hold.py` accepts `target <name>` and `--target <name>`.
- `offload.repository_identity` returns `KnownRepository(name, key)`. `name` is the source folder name. `repository.repository_key` supplies the normalized origin URL, or `git-root:<root commit>` when no origin exists. The key is empty when the repository has no commits. `path_key` encodes the identity for local claim and affinity files. The Mac keeps `mirror_name`, the source folder name. Linux mirrors use the build queue rule in `config/build_folders.conf`: `~/.local/state/build-target/mirror/<repository>/<target>-s<slot>`. `build_folders.repository_name` joins the policy name and the identity hash. `names.repository_key_length` sets the hash length, which is eight characters. Worktrees and gate copies with the same policy name and identity share one mirror per target and slot. Different identities with the same policy name have separate mirrors.
- A claim carries a slot. `mac_test.py` allocates it under the target lock from `slots.<repo>`, then `slots`, and prefers the free slot that last ran the same worktree. Affinity grants no ownership. Claim records hold `repo_name`, `repo_key`, `slot` and `cleanup_pending`. Status 13 means cleanup must come before execution.
- `buildlog sync` pulls from every configured SSH peer. Each peer gives back only its own host folders. `sync.hosts.<target>` in `config/build_targets.conf` is the one host-to-target map. Step records carry `target` (index schema 27). The `ssh_attempts` table (schema 28) keeps target, repository identity, queue time, departure time, slot, outcome and evidence.

### Linux execution: `scripts/mac_test/linux_runner.py`

1. Tracked scripts transfer first. Then `rsync --delete` copies the source, with untracked files, and protects ignored paths, `.git` and `target`.
   Both copies use `copies.RSYNC_RECEIVER_WRITE_TIME_FLAGS` (`-rlpc`).
   Checksums skip files with equal content and keep their destination modification times.
   Changed files get the receiver write time, so Cargo detects changed source with an old source modification time.
2. Each slot caches the shell exports. The cache key is `flake.nix`, `flake.lock`, `nix/` and the shell selector. Reuse checks that the referenced store paths exist.
3. The execution probe evaluates the shell and checks Rust, nextest and mend parity inside it. `probe_timeout_s` covers reachability. `shell_timeout_s` covers shell evaluation.
4. `.linux-run.json` records ownership before shell evaluation. Unconfirmed cleanup keeps the marker and the claim. `linux_startup=unresolved` starts no work and maps to `BusyMacDecline(LostRunLeftovers())` with no backoff.
5. Execution keeps caller arguments, staged doc snapshots, the sent tree key and the real failure status. `verify-linux` passes never count as natedev pass evidence.

Tool parity uses one form for the sender, the probe, execution, and refusal messages.
The normalizer strips each version line and joins nonempty lines with one space.
This includes all lines from `cargo nextest --version`.

Linux cleanup reads the real UID from `/proc/<pid>/status` before it reads process ownership evidence.
It excludes processes from other users.
Cleanup excludes processes that started before the recorded run and ancestors of the checking process.
It also excludes direct children of another user, except children of PID 1.
Slot processes keep their user, but an orphan can acquire PID 1 as its parent.
Other same-user processes with unreadable evidence keep the slot busy.

`verify-linux` steps use the target slots and build budget for admission.
They do not contact natedev's admission service or obey natedev's hold.
The controller still checks spending before AWS admission.

A missing completion marker reports the runner exit status when available.
It does not prove a link failure.
The attempt record keeps the reason.
An interruption marker reports the instance generation.

### Routing: `scripts/direct/verify.sh`, `scripts/build_targets/build_targets.py`

- The retained routing code supports `verify.sh <command> [PACKAGE] --target aws` only with an explicit AWS target registration.
  This includes test, check, staged doc, example, build, final, and lint judgments.
  Tree-writing lint repairs remain on natedev.
  A pinned final runs its check and tests on AWS and saves no local pass.
  A pin keeps admission, spending, and version checks. It takes no natedev grant or cargo token.
  A refusal ends the step with a nonzero status. Remote step records include `target=aws`, `host=aws-build`, and `pinned=true`.
- Unpinned test, check, doc, example, and lint judgments try eligible SSH targets in configured order. `ssh_target_eligibility` checks configuration, backoff, capacity and reachability, with no shell evaluation.
- `build_targets.py reserve` returns 0 (reserved or recovered), 11 (busy macOS target) or 1 (no reservation). `--recover-only` recovers only an existing claim. `MacClaimFromWait` moves ownership from the wait to the claim with no unclaimed interval.
- `BUILDLOG_MEM_WAIT_SINCE` and `BUILDLOG_MEM_WAIT_FIRST_FILE` keep the first queue time across remote loss and local fallback. Local fallback needs admission. A confirmed remote failure does not cause a local rerun.
- A natedev hold permits a process that holds a live Mac or AWS claim, plus its ancestors and descendants.
  The reason is `holds a live Mac or AWS claim`, even when the step record is stale or unreadable.
  The enforcer reads the process table before it reads claims once per pass during a hold.
  It uses the same claims for decisions and signals.
  A claim or target config read error prevents TERM and KILL for that pass.
  The enforcer reports the error once and reports recovery once.
  Audits and reports show the error and list builds without claim protection.
  The drain reports the error once and stops no waiting run until claims are readable.
  If the initial claim read fails, the drain hold keeps every outermost gate.
  After claims become readable, the drain stops waiting gates and keeps working gates alive.
  A new claim cancels a pending kill. An ended claim permits the next pass to stop the family.
  `hold_enforcer.protected_reason` keeps live step controllers and route children alive in `waiting_hold` and `waiting_mac`.
  `--drain` also waits for cargo children and admission reservations. It still stops waiting runs.

### Cost ledger and spending guard: `scripts/aws_target/`

- `config.py` reads the spending settings into `SpendingSettings`.
  Only a usable live limit permits spend.
  `ConfigRead.origin` returns `UsableLiveValue`, `RecoveredValue`, or `UnavailableValue`.
  A recovered value never permits spend.
- `ledger.CostLedger` is a locked, atomic JSON ledger. It records instance-state observations, price samples, charged intervals, compute requests, reconciliations and limit events. Times are integers and money is `Decimal`. Intervals split at day and month boundaries in the accounting zone. A corrupt ledger keeps its evidence and refuses new spend.
- The estimate uses Spot price history, `fallback_usd_per_hour` when no sample exists, and the fixed costs (retained and root storage, IPv4, gp3 performance, Cost Explorer reads). Fixed costs continue while the instance is stopped.
- `spending_guard` returns `SpendingAllowed`, `SpendingRefused` or `SpendingUnavailable`. It refuses when the estimate plus `limit_reserve_usd` reaches either cap. Each decision is written through `ledger._decision_record`. Readers decode that record and never compute the decision again.
- `ComputeRequest` is recorded before the compute deploy call. Until confirmed state ends it, it is charged at `max(fallback_usd_per_hour, SpotMaxPrice)`.
- `CostLedger.refresh_charges` reads Cost Explorer once a day for whole UTC days. Each reconciliation stores `established_days`, the amount it set for each accounting day. A day is charged the higher of its computed total and its established total. Only `guard` and `estimate` save these amounts, through `_save_estimate`. Status and report write nothing.
- A limit event is `PENDING` until delivery. `cost-notice.lock` serializes delivery, `notification_retry_s` bounds retries, and the send key `aws-limit:<event_id>` stops duplicates.
- Target selection excludes AWS as `off` while offload is off, as `spending_limit` on a refusal and as `spending_unavailable` on unusable settings. Selection then tries the next target. `cleanup_refused_claims` still cleans retained AWS claims.

### Control: `aws_target.py`, `remote.py`

- `aws_target.py tick|start|stop|serve` runs `Controller` under `control_lock`. `AwsCli` is the one AWS CLI adapter. `controller.json` holds the state (`off`, `refused`, `stopped`, `starting`, `running`, `draining`, `stopping`), `generation`, `permitted_generation`, the drain deadline and the capacity backoff.
- `tick` starts the box for one live pinned waiting step or enough eligible rows in `admission/queue.json`.
  Both paths require guard permission and an ended backoff. Unpinned steps keep their queue thresholds. A drain keeps its deadline (`drain_max_minutes`). The tick also runs `refresh_charges`.
- Natedev compares the tree's tool versions with `box-tools.json` before it claims a Linux target slot, so a version decline never reaches the box and does not reset the idle timer.
  The comparison uses only the keys in the saved `versions` map. A missing or unreadable file leaves the probe and runner checks in control. It does not read the saved `instance_id`.
  A mismatch writes its time to `version-decline-<repository name>` in the target's local state directory. A later attempt without a mismatch removes that file before the claim.
- `tick` does not count queued or pinned work of a repository for `version_decline_hold_s=3600` after its decline. The controller only reads the file and runs no tool.
  A job reaches the comparison only after the box is up. So a repository with other tool versions still gets one paid start each hold, about 15 idle minutes.
  A corrected repository can wait until the hold ends for its own start. It still runs when the box is up for other work.
- `remote.py` publishes an `AdmissionEnvelope` (target, purpose, generation, decision time, expiry) only after a confirmed guard write. The runner checks it before and after shell preparation. Other Linux targets use `IndependentLinuxExecution` and need no AWS permission.
- On the box, `RemoteAgent` stops the instance after `idle_stop_minutes` idle and polls IMDSv2 every `interruption_poll_s`. An interrupted slot prints `aws_interrupted=<generation>` once, after `mac_exit` and `mac_cleanup`, and exits with the child status. Offload reads it as `InterruptedTargetRun` and records `ON_NATEDEV` before local work.
- The controller also stops a running instance after `idle_stop_minutes` without published permission when no target claim remains.
  It saves the first unpublished time for each generation in `controller.json`.
  A successful publication or a new generation resets the timer. The stop reason is `unpublished_idle`.
- `copy_tracked_scripts` sends every tracked file under `scripts/` and `config/`, including `config/build_folders.conf`.
  The remote service and Linux runner need this config during import.
- A version refusal names the target, each mismatched tool, both versions, and the file set by `version_pin_file`.
  The verify output and the step attempt record keep this reason.
- The box has `rustup`, `rustc`, `cargo` and `rustdoc` as rustup proxies. The Nix compiler builds only cargo-mend and cargo-tile. It does not select a job's compiler.
  The Linux runner keeps the caller PATH after the development shell PATH, then runs `shellHook`.
  Each job installs its toolchain in the mirror, before the parity checks and the work, in the job's process group.
  rustup's order selects the toolchain, the same as on natedev: `RUSTUP_TOOLCHAIN`, then the tree's toolchain file. With neither, a plain `x.y.z` version from natedev becomes `RUSTUP_TOOLCHAIN`. Other forms leave it unset, and parity declines the job.
  The runner passes the step's selected `RUSTUP_TOOLCHAIN` to the disk measurement and cleanup. A toolchain file still applies when that variable is unset.
  One `flock` per toolchain protects the install. The lock name is `RUSTUP_TOOLCHAIN`, or the file's channel, or the SHA-256 of a file with no channel. A second job waits on the lock and downloads nothing.
  An install failure declines the attempt with its error text, and the job goes to normal routing.
  Toolchains stay in `~/.rustup` on `/home`, not in the image. A new volume downloads each version once, about 1.6 GB and 20 s.
  The probe measures the checkout filesystem and runs no cargo or rustc. It reads nextest and mend through `cargo-nextest` and `cargo-mend`. The runner compares rustc after it enters the mirror.
- The controller keeps claim files after a confirmed stop or generation change.

### Status and report

- `targets.read_aws_status` and `build_targets.format_aws_status` show state, spending decision, month dollars against the cap, slots per repository, cleanup state and recovery action. Status makes no AWS call, probe or write.
- `report.py` renders `### AWS` from the AWS state folder without an SSH target registration.
  The section shows a cost headline, daily spend, a cost matrix, a daily time trend, and notes.
  Bill rows and headlines use `provisional bill` for provisional readings and `bill` for final readings.
  The daily spend table shows bill days and estimate days after the newest bill day.
  Historical reports omit estimate days after the report date.
  `aws_report.estimate_wait_saved` compares natedev steps from the same day, kind, and repository.
  The report shows one AWS section before the Mac copies section.
  The worker records `mode` in each launch, refusal, and finish event in `cloud-gate-launches.jsonl`.
  After result validation, the worker records `gate_s` in the finish event.
  `gate_type_window_days` sets the matrix and trend window; the trend ends with totals and median times for all shown days.

### Deployment: `deploy.py`, `docs/plans/builds-aws/`

- `deploy.py up --approve-resources` runs twelve stages.
  They run in this order: price, bootstrap, stacks, scaler-key, Tailscale policy, box join, box configuration, checks, image, idle stop, activation, and routing proof.
  Each stage checks live state and records its last result and times in `up.json`.
  A retry continues from incomplete stages. The price stage always refreshes the public price.
  The stacks stage preserves the guard, ledger, and retained-volume checks.
  A completed foundation wait renews preparation for unchanged inputs.
  `REVIEW_IN_PROGRESS` counts as absent because that stack has no resources.
- `deployment.json` holds the stack, instance, and data volume IDs outside the repository.
  The controller reads its instance ID from that record.
  Replacement reads the saved retained volume from the same record.
- The scaler-key stage verifies the configured profile through STS.
  It writes a new key atomically to the private credential file with mode `0600`.
  It keeps other profiles. It verifies the new key before it deletes old keys.
  A failed new key is deleted. The secret appears only in the private credential file.
- `deploy.py price` reads the current Spot price with the encrypted administrator key.
  It writes the price and its read time to the state file named by `deployment_price_file`.
  The default path is `~/.config/aws-build/deployment-price`. The file stays outside the repository and has mode `0600`.
  `prepare`, `apply`, and `setup-start` read this file. `bootstrap` needs no price file.

- `prepare` and `apply` read the Budget address from the private file named by `notification_email_file`.
  The default path is `~/.config/aws-build/notification-email`.
  The file must stay outside the public repository. `bootstrap` needs no address file.
  Prepared parameters and their fingerprint retain `NotificationEmail`. `setup-start` uses that prepared value.
- `deploy.py prepare` writes the CloudFormation parameters, priced inventory and preflight evidence locally, with no AWS command. `apply` and `setup_start` run only at the user's terminal. They create the monthly Budget (emails at 50, 80 and 100 percent, a stop action at 100 percent) before any chargeable resource.
- `cloudformation.json` defines the Spot instance, the root volume and the retained data volume. Shutdown behavior is `stop`, IMDSv2 is required and inbound is UDP 41641 only. The scaler IAM policy allows start and stop on the one instance, the needed describe calls and `ce:GetCostAndUsage`.
- `nixos.patch` adds host `aws-build` and the natedev `aws-target` user unit, stopped by default. It is on `/etc/nixos` main as commit 78746da. natedev was rebuilt on `/etc/nixos` 10353a9 (2026-11-03): `aws-target.service` is installed and disabled. `/etc/nixos` main 25966d8 adds sccache (one shared module with natedev), mold, natedev's cargo config and cargo-tile to `hosts/aws-build.nix`. The box is not built.

### Box setup stages

Device names in `config/aws-build-tailscale.json` must equal a machine name (first label of `DNSName` in `tailscale status`); `up` checks this first.

1. `tailscale-policy` reads `config/aws-build-tailscale.json` and the live policy.
   It resolves the natedev and Mac addresses from local Tailscale status.
   The network rule permits only these addresses on port 22.
   The SSH rule permits `autogroup:member` as `natepiano` and `root`; the network rule still limits access to these devices.
   It changes only `tag:build-aws` owners and destination rules.
   It prints each proposed change and uses the policy ETag to prevent concurrent overwrites.
   It refuses mixed destination rules and broad outbound access that requires an unrelated policy change.
   HTTP refusals include the status and validation details without user fields; network refusals include the failure reason.
2. `box-join` checks SSM and the instance's Tailscale identity.
   `box-join` writes the box host keys from Tailscale to `~/.ssh/known_hosts` for the host name and address; box setup refusals show the last stderr line.
   It starts a stopped instance through the existing guarded setup path.
   If AWS reports `InsufficientInstanceCapacity`, `up` waits `capacity_backoff_s` between start attempts until AWS accepts the start or the user interrupts.
   It creates a single-use, pre-approved key and revokes it after the join attempt.
   SSM disables terminal echo before the helper sends the key through stdin.
   The command reads the key from stdin and writes no key file.
3. `box-configuration` reads the clean `main` checkout named by `nixos_configuration_directory`.
   It compares `/run/current-system` with the `aws-build` output.
   It builds the output locally and copies the closure with destination substitutes.
   It waits for `/var/lib/aws-build/bootstrap-complete` before changing the box.
   It copies the closure and runs the box configuration commands as `root` without `sudo`.
   It holds the controller lock and stops the remote idle service during the copy.
   A bounded poweroff timer protects a failed reconnect.
   A transient systemd unit runs the switch independently of SSH.
   The helper resets the old idle timestamp before it restarts the remote service.
   It waits for the expected system and an active `aws-build-remote` service.
4. `checks` confirms SSM, the EBS serial under `/home`, ext4, and the remote service.
   It compares `cargo-nextest nextest --version` and `cargo-mend mend --version` in the plain environment on both hosts.
   It does not compare rustc: each job uses its tree's toolchain.
   Non-interactive SSH uses the box tools from `environment.systemPackages` in `/etc/nixos/hosts/aws-build.nix`.
   The stage reads no other source repository and copies no shell inputs.
   It uses `normalize_tool_version` for both results.
   `box-tools.json` records the instance ID and normalized tool versions.
   Each build still checks the tools inside its own repository shell.

5. `image` saves an image of the configured system before the idle stop proof.
   It checks the saved system path and the image state before creating an image.

### Box image cache

`image.json` records the image ID, system path, and completed idle stop system proof.
The image holds the root filesystem and the Nix store.
The helper excludes the `/dev/sdf` data volume from the image.
It moves the Tailscale identity and both bootstrap markers to `/home` while the root snapshot starts.
It restores these items after EC2 reports the root snapshot ID, including after a failed request.
`image-scrub.json` records the need to restore these items before the scrub starts.
A rerun restores the items through SSM before it resumes image creation or cleanup.
The helper deletes the marker only after a successful restore.
The helper copies each item to a `.partial` path before it renames the complete copy and removes the original.
The restore removes partial copies before it restores complete copies.
The moved files leave no live path in the image, but their deleted blocks can stay in the snapshot.
The image is private and encrypted, and is never shared.
`down` deletes the Tailscale node, which ends that key.
The helper holds the controller lock during this operation and stops no service.
A changed desired system refreshes the image.
The helper tags both the image and its snapshots with `aws-build-image=true` beside the cost tag.
The helper waits for the image to become available and removes other images with that ownership tag.
It lists snapshots with that ownership tag separately and deletes each snapshot outside the current image mappings.
The stage stays incomplete while either list holds an item to delete.
A rerun without a restore marker resumes a pending or available image and repeats cleanup without changing the box.
Only one image remains.
A 32 GiB root snapshot costs at most $1.60 per month at $0.05 per GB-month.
`down` keeps the image and its system proof.
The next `up` selects the available image for a new instance.
The unchanged bootstrap prepares the new data volume and installs the remote scripts.
Every stage still runs in the same order.
A completed idle stop proof also covers a new instance with the same desired system.

`admin_secret_file` names `~/.config/aws-build/admin.gpg` outside the repository, with mode `0600`.
The encrypted JSON holds `aws_access_key_id`, `aws_secret_access_key`, `tailscale_client_id`, and `tailscale_client_secret`.
`deploy.py secrets` uses symmetric AES256 encryption and hidden terminal input.
It can import the administrator profile and the old OAuth file.
It removes those plain credentials only after the encrypted write succeeds.
It preserves the scaler profile and all unrelated sections.
Enter retains a stored value during replacement.

One resolver selects the scaler credential file for deployment, key creation, and teardown.
It uses a nonempty `AWS_SHARED_CREDENTIALS_FILE` override before `credential_file` from `cloud_gate.conf`.
It expands `~` in either path.
Every `deploy.py` command sets the resolved path in its environment before dispatch.
AWS CLI subprocesses inherit the same path.
User deployment commands need no credential environment prefix.

`price`, `apply`, `setup-start`, and `up` decrypt at the first administrator request.
`prepare` decrypts only when it must reconcile existing AWS resources.
`bootstrap` needs no administrator credentials.
`up` shares one decryption across all twelve stages.
The price stage refresh and the live checks require credentials even when every stage reports `present`.
The policy check uses the same decrypted OAuth client for its token request.
AWS subprocesses receive the administrator key through their environment without an administrator profile argument.
The SSM subprocess uses that same environment.
Inherited AWS session tokens cause a refusal.
The unattended controller continues to use the plain scaler profile without a prompt.

The helper reads the passphrase from `/dev/tty` without echo.
A process without a terminal cannot decrypt through the helper.
GPG receives the passphrase through a private descriptor with `--no-symkey-cache` and loopback pinentry.
The helper never uses public-key encryption.
Decrypted values remain in process memory until the run ends.
AWS subprocesses contain the AWS key in their environment during their lifetime.
A process with sufficient access can inspect that memory or environment.
Administrator subprocesses ignore the AWS configuration file to prevent CLI history from storing secret responses.
The helper redacts administrator values from reported AWS errors.
Administrator commands refuse a remaining plain administrator profile or legacy OAuth file and name `deploy.py secrets`.
`deploy.py down --approve-destroy` uses the same encrypted administrator session.
No deployment stage edits or pushes the NixOS checkout.
The controller activation and routing proof remain separate plan stages.

## Invariants

1. No AWS resource and no AWS call exist before the user's go-live gate.
2. Read every tunable at use from `config/aws_target.conf` or `config/build_targets.conf`, through `scripts/lib/conf.py`.
3. The guard runs before every AWS start, admission, deployment and first boot. Derive the Budget amount from `monthly_limit_usd`.
4. No password, AWS key or Tailscale auth key passes through a session. The scaler key is not on the box. `build-scaler` credentials remain in the private credential file. The helper never prints them.
5. Use Tailscale SSH. Store no GitHub credential on the box; `ci_repo=` stays empty.
6. An empty `OffloadRequest.target` selects the legacy Mac default. Mac paths and output do not change.
7. Unresolved process ownership never adds an unattributed PID to the kill list. Missing completion stays `LostMacRun`.
8. `permitted_generation`, not `generation`, protects against restarts.
9. Tests fake every AWS call, SSH, clock and process state.

## Calibration and gotchas

- `conf.decimal` is the one finite-number parser. It accepts a value only when the `Decimal` and its `float()` are both finite. Its error text is `expected a finite number`; callers match that text.
- Full `offload.read_config` validates the AWS spending keys. AWS must be excluded before it runs, or one bad limit stops selection for every target.
- Cost Explorer `DateInterval` takes `YYYY-MM-DD`, and DAILY granularity reports UTC days.
- An established day total can exceed a later bill or estimate. That is the cost of a total that never falls.
- `limit_reserve_usd` covers one drain and one idle interval. The Budget action is the late backstop, not the cap.
- A user stop disables the persistent Spot request until the next start. AWS restarts only a box it interrupted.
- The compressed bootstrap must fit the 4096-byte CloudFormation parameter limit. On 2026-11-03 the encoded bootstrap measured 3908 bytes. `aws-build-base.nix` is copied into it, so put new box packages and services in `hosts/aws-build.nix` (`modules/linux/aws-build.nix` is the natedev `aws-target` unit).
- A pending notice that keeps failing delays each guard call by up to `notification_timeout_s`.
- A verify module that hits the 1800 s test limit has so far been a route defect, not machine load.
- Fake AWS tests do not prove real bootstrap or persistent-request behavior. The runbook's user checks prove them.

## Why

- Mac mirrors and claims key on the source folder name, because the origin name can differ (`.claude` and `claude_commands`).
- Each peer returns only its own host folders, because a multi-peer pull otherwise overwrites fresher records.
- Charges reconcile by UTC day, because Cost Explorer reports UTC days.
- An unconfirmed compute request is charged at the Spot maximum price. A historical price is not an upper bound, and the fallback rate under-counts.
- A stateless per-day floor was ruled out: it over-counts the common uneven case, a $10 bill becoming $17.92 across two days.
- Refusal cleanup reuses `take_over_kept_claim` and `check_leftovers`, so no second cleanup path exists.

### Activation and proof

Configuration, image creation, and the idle stop proof close AWS routing before they change the box.
Each stage waits without a time limit for AWS jobs that still run.
Kept claims do not delay the wait.
A stage with a valid proof leaves routing unchanged.
Activation opens routing after the idle stop proof.
An interrupted `up` leaves routing closed until the next `up` reaches activation.
Jobs use natedev and the Mac while AWS routing stays closed.

1. `idle-stop` observes EC2 until the box stops within `idle_stop_minutes` plus `idle_stop_margin_s`.
   Without a saved proof, the stage waits for pending or stopping instances to reach a stable state.
   It starts a stopped instance through the existing spending checks and waits for health.
   It holds the controller lock so the controller cannot satisfy the proof.
   A job that starts before routing closes resets the proof deadline to the full duration.
   A deadline failure requests an EC2 stop, confirms the stop, and refuses the stage.
2. `activation` writes `routing.json` under `~/.local/state/build-targets/aws`.
   This record enables routing for the configured AWS settings file.
   The controller and the sender read the same record before the legacy `offload` value.
   The stage enables and starts `aws-target.service` with `systemctl --user`.
   It requires the reviewed AWS registry rows and edits no tracked file.
3. `routing-proof` runs `verify.sh test aws-routing-proof --target aws --no-cache` in a scratch Git repository.
   The folder uses task `gate:aws-routing-proof`.
   The next `up` removes a folder left by a dead run.
   `scripts/aws_target/routing_proof.py` supplies the crate files.
   The crate has its own workspace, no Rust dependencies, and one test that runs the compiler.
   The runner requires `flake.nix` and `flake.lock`, so the crate supplies a small locked shell.
   The `default` shell retains the host tools on PATH.
   Git identifies the temporary folder as the source root.
   The stage deletes the folder after the command ends.
   The crate needs no other source repository.
   The normal queue retains admission, spending, and version checks.
   The stage requires a new successful pinned buildlog step with `target=aws` and `host=aws-build`.

Both proofs record the current instance ID and completion time in `up.json`.
A replacement instance invalidates the routing proof.
The idle stop proof remains valid when `image.json` records the same desired system.
A later run skips completed proofs without another idle wait or test.
A stopped instance retains its completed setup evidence for the same instance ID.
The price stage still refreshes the public price.

### Target removal

`deploy.py down` prints the removal plan without reading credentials or changing state.
`deploy.py down --approve-destroy` removes the target in this order.

1. Write disabled `routing.json` and disable `aws-target.service` with `systemctl --user`.
2. Refuse while an AWS job still runs. Kept claims do not delay removal.
3. Save public resource identities in `down.json` before removing any resource.
4. Delete the tagged `aws-build` Tailscale device and keep the policy rules.
5. Delete every scaler access key and remove only its credential profile.
6. Update the existing stack with `EnableCompute=false` and wait for compute removal.
7. Delete the foundation stack and wait for its removal.
8. Delete the retained data volume and Budget.
9. Remove deployment records, setup proofs, and the prepared bundle files.

The template implements compute and foundation as two phases of one stack.
The helper uses the previous template and parameter values during compute removal.
It does not read the private notification address file.
The helper excludes new claims and controller actions throughout resource removal.
The helper reports the removed resource kinds.
Deleting the data volume loses the `/home` build cache.
The helper preserves `admin.gpg`, disabled routing, claims, the spend ledger, and charge history.
It records confirmed termination without removing past spending evidence.
It retains the conservative fixed cost inventory.
The existing spending caps remain unchanged.

A repeated command checks live state and skips each completed removal.
`down.json` preserves identities after an interruption and prevents `up` until removal completes.
A successful removal deletes this temporary record.
The next `up` creates a new target and uses the existing monthly spending history.
The helper shares one administrator decryption across AWS and Tailscale requests.
The Tailscale OAuth client needs device deletion access for `tag:build-aws`.


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

Existing stack updates preserve the previous Bootstrap and AmiId when an InstanceId exists. These values apply only when the stack creates an instance.
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
The controller searches every configured region for an expired reservation without a recorded region.
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

The controller reads gate Spot prices every `price_refresh_seconds=3600`.
It reads prices after the tick releases `controller.lock`, in the same process.
It takes `spot-prices.lock` without blocking and skips the refresh when that lock is held.
The price lock stores the last refresh attempt time.
Slow price reads leave start and stop commands free to acquire the controller lock.
It reads each region in `spot_candidate_regions` and the gate deployment once per refresh.
Each read requests all configured gate `instance_types` for Linux/UNIX.
The controller keeps the latest price for each zone and type.
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
The price stage of `up` shows this quota for each gate region.
Only `deploy.py prices` also shows the G and VT Spot vCPU quota (`L-3819A6DF`) for each region in its list.
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

The service selects the first candidate region with an applied G and VT Spot quota of at least 32.
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
