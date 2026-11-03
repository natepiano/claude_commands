# AWS build target

## What it is

AWS is a Linux SSH build target named `aws` on host `aws-build`. It runs `test`, `check` and staged `doc` steps. `build`, `example`, `final` and lint stay on natedev. The machine is one persistent Spot `m7a.4xlarge` (x86_64, NixOS) with a retained, encrypted 500 GiB gp3 data volume at `/home`. A durable cost ledger and one spending guard refuse AWS work at either cap: `monthly_limit_usd=400` and `daily_limit_usd=20` in `config/aws_target.conf`. The user approved the $400 monthly cap on 2026-11-01. `daily_limit_usd=20` is derived (the monthly cap / 20); the user approved it on 2026-11-03. No code may raise either cap or copy it.

The target is built and disabled. `offload=off`, and `targets=mac,natedev` does not list AWS. No AWS resource exists. The user's go-live gate creates the resources and turns the target on, with [the runbook](../plans/builds-aws-runbook.md). The source design is [builds-aws-design.md](../plans/builds-aws-design.md). Its Phase 4 (lint with write-back) is not built.

## How it works

### Target state, slots and records

- `mac_test.state_paths(target)` is the one map from a target to its state folder. The Mac keeps `~/.local/state/mac-test`. Every other target uses `~/.local/state/build-targets/<target>/` for claims, locks, block and unreachable backoff.
- `mac_test.configured_ssh_target(target)` checks every target, the Mac too, against `config/build_targets.conf`. It refuses an unknown or non-ssh name before any state write. `build_hold.py` accepts `target <name>` and `--target <name>`.
- `offload.repository_identity` returns `KnownRepository(name, key)`. `name` is the source folder name. `key` is the normalized origin URL, and `path_key` encodes it for paths. `target_config.uses_legacy_mac_layout(platform)` is true only for `macos`. The Mac keeps one mirror per source folder name. Linux targets use `~/.local/state/build-target/mirror/<path_key>/<slot>`, with one warm build folder per slot.
- A claim carries a slot. `mac_test.py` allocates it under the target lock from `slots.<repo>`, then `slots`, and prefers the free slot that last ran the same worktree. Affinity grants no ownership. Claim records hold `repo_name`, `repo_key`, `slot` and `cleanup_pending`. Status 13 means cleanup must come before execution.
- `buildlog sync` pulls from every configured SSH peer. Each peer gives back only its own host folders. `sync.hosts.<target>` in `config/build_targets.conf` is the one host-to-target map. Step records carry `target` (index schema 27). The `ssh_attempts` table (schema 28) keeps target, repository identity, queue time, departure time, slot, outcome and evidence.

### Linux execution: `scripts/mac_test/linux_runner.py`

1. Tracked scripts transfer first. Then `rsync --delete` copies the source, with untracked files, and protects ignored paths, `.git` and `target`.
2. Each slot caches the shell exports. The cache key is `flake.nix`, `flake.lock`, `nix/` and the shell selector. Reuse checks that the referenced store paths exist.
3. The execution probe evaluates the shell and checks Rust, nextest and mend parity inside it. `probe_timeout_s` covers reachability. `shell_timeout_s` covers shell evaluation.
4. `.linux-run.json` records ownership before shell evaluation. Unconfirmed cleanup keeps the marker and the claim. `linux_startup=unresolved` starts no work and maps to `BusyMacDecline(LostRunLeftovers())` with no backoff.
5. Execution keeps caller arguments, staged doc snapshots, the sent tree key and the real failure status. `verify-linux` passes never count as natedev pass evidence.

### Routing: `scripts/direct/verify.sh`, `scripts/build_targets/build_targets.py`

- Queued `test`, `check` and staged `doc` steps try eligible SSH targets in configured order. `ssh_target_eligibility` checks configuration, backoff, capacity and reachability, with no shell evaluation.
- `build_targets.py reserve` returns 0 (reserved or recovered), 11 (busy macOS target) or 1 (no reservation). `--recover-only` recovers only an existing claim. `MacClaimFromWait` moves ownership from the wait to the claim with no unclaimed interval.
- `BUILDLOG_MEM_WAIT_SINCE` and `BUILDLOG_MEM_WAIT_FIRST_FILE` keep the first queue time across remote loss and local fallback. Local fallback needs admission. A confirmed remote failure does not cause a local rerun.
- `hold_enforcer.protected_reason` protects every step state except `ON_NATEDEV`, so a natedev hold stops only local work.

### Cost ledger and spending guard: `scripts/aws_target/`

- `config.py` reads the spending settings into `SpendingSettings`. Only a usable live limit permits spend. A committed value that `conf` recovers never permits spend (`conf.value_origin`: `UsableLiveValue`, `RecoveredValue`, `UnavailableValue`).
- `ledger.CostLedger` is a locked, atomic JSON ledger. It records instance-state observations, price samples, charged intervals, compute requests, reconciliations and limit events. Times are integers and money is `Decimal`. Intervals split at day and month boundaries in the accounting zone. A corrupt ledger keeps its evidence and refuses new spend.
- The estimate uses Spot price history, `fallback_usd_per_hour` when no sample exists, and the fixed costs (retained and root storage, IPv4, gp3 performance, Cost Explorer reads). Fixed costs continue while the instance is stopped.
- `spending_guard` returns `SpendingAllowed`, `SpendingRefused` or `SpendingUnavailable`. It refuses when the estimate plus `limit_reserve_usd` reaches either cap. Each decision is written through `ledger._decision_record`. Readers decode that record and never compute the decision again.
- `ComputeRequest` is recorded before the compute deploy call. Until confirmed state ends it, it is charged at `max(fallback_usd_per_hour, SpotMaxPrice)`.
- `CostLedger.refresh_charges` reads Cost Explorer once a day for whole UTC days. Each reconciliation stores `established_days`, the amount it set for each accounting day. A day is charged the higher of its computed total and its established total. Only `guard` and `estimate` save these amounts, through `_save_estimate`. Status and report write nothing.
- A limit event is `PENDING` until delivery. `cost-notice.lock` serializes delivery, `notification_retry_s` bounds retries, and the send key `aws-limit:<event_id>` stops duplicates.
- Target selection excludes AWS as `off` while offload is off, as `spending_limit` on a refusal and as `spending_unavailable` on unusable settings. Selection then tries the next target. `cleanup_refused_claims` still cleans retained AWS claims.

### Control: `aws_target.py`, `remote.py`

- `aws_target.py tick|start|stop|serve` runs `Controller` under `control_lock`. `AwsCli` is the one AWS CLI adapter. `controller.json` holds the state (`off`, `refused`, `stopped`, `starting`, `running`, `draining`, `stopping`), `generation`, `permitted_generation`, the drain deadline and the capacity backoff.
- `tick` reads `admission/queue.json` and starts the box only for enough eligible waiting rows, guard permission and an ended backoff. A drain keeps its deadline (`drain_max_minutes`). The tick also runs `refresh_charges`.
- `remote.py` publishes an `AdmissionEnvelope` (target, purpose, generation, decision time, expiry) only after a confirmed guard write. The runner checks it before and after shell preparation. Other Linux targets use `IndependentLinuxExecution` and need no AWS permission.
- On the box, `RemoteAgent` stops the instance after `idle_stop_minutes` idle and polls IMDSv2 every `interruption_poll_s`. An interrupted slot prints `aws_interrupted=<generation>` once, after `mac_exit` and `mac_cleanup`, and exits with the child status. Offload reads it as `InterruptedTargetRun` and records `ON_NATEDEV` before local work.
- `release_stopped_claims` releases only claims bound to the stopped host and generation, and writes `release-evidence/`.

### Status and report

- `targets.read_aws_status` and `build_targets.format_aws_status` show state, spending decision, month dollars against the cap, slots per repository, cleanup state and recovery action. Status makes no AWS call, probe or write.
- `report.py` renders `### AWS`: uptime, steps, estimated queue wait saved and cost. `aws_report.estimate_wait_saved` compares same-day natedev steps of the same kind and repository. AWS rows appear only when an AWS `SshTarget` is configured.

### Deployment: `deploy.py`, `docs/plans/builds-aws/`

- `prepare` and `apply` read the Budget address from the private file named by `notification_email_file`.
  The default path is `~/.config/aws-build/notification-email`.
  The file must stay outside the public repository. `bootstrap` needs no address file.
  Prepared parameters and their fingerprint retain `NotificationEmail`. `setup-start` uses that prepared value.
- `deploy.py prepare` writes the CloudFormation parameters, priced inventory and preflight evidence locally, with no AWS command. `apply` and `setup_start` run only at the user's terminal. They create the monthly Budget (emails at 50, 80 and 100 percent, a stop action at 100 percent) before any chargeable resource.
- `cloudformation.json` defines the Spot instance, the root volume and the retained data volume. Shutdown behavior is `stop`, IMDSv2 is required and inbound is UDP 41641 only. The scaler IAM policy allows start and stop on the one instance, the needed describe calls and `ce:GetCostAndUsage`.
- `nixos.patch` adds host `aws-build` and the natedev `aws-target` user unit, stopped by default. It is on `/etc/nixos` main as commit 78746da. natedev was rebuilt on `/etc/nixos` 10353a9 (2026-11-03): `aws-target.service` is installed and disabled. The box is not built.

## Invariants

1. No AWS resource and no AWS call exist before the user's go-live gate.
2. Read every tunable at use from `config/aws_target.conf` or `config/build_targets.conf`, through `scripts/lib/conf.py`.
3. The guard runs before every AWS start, admission, deployment and first boot. Derive the Budget amount from `monthly_limit_usd`.
4. No password, AWS key or Tailscale auth key passes through a session. The scaler key is not on the box. `build-scaler` credentials are never read or printed.
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
- The compressed bootstrap must fit the 4096-byte CloudFormation parameter limit. On 2026-11-03 the encoded bootstrap measured 3908 bytes. `aws-build-base.nix` is copied into it, so put new host packages and services in `modules/linux/aws-build.nix`.
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
