# AWS build target — design

> **Status: DESIGN** (aws-unit, 2026-10-31). No AWS resource exists and nothing is spent. The user is still creating the account. No password or key goes through a session.

## Goal

An AWS Linux machine is one more `ssh` build target. It takes compile, lint and test steps that would otherwise wait in natedev's memory queue. It runs only while there is work for it and stops when idle. We measure it as queue wait saved against dollars spent.

## What the build log says (natedev, last 7 days, sweeps excluded)

| Measure | Value |
| --- | --- |
| Steps | 12,973 |
| Steps that waited for memory | 7,486 (mean wait 18.5 min) |
| Summed waiting time | 2,314 h (about 14 steps waiting at any moment) |
| Summed run time | 736 h (about 4.4 steps running at any moment) |
| Hours with queue | 222 of the 240 hours from 2026-10-21 to 2026-10-30: the steps that started in the hour had waited more than 30 min in total |
| natedev CPU busy | 94 % on 2026-10-29, 85 % on 2026-10-30 (the first full days with CPU samples) |

Waiting time by kind and source:

| Kind | hana | cargo-liner | clones, gate copies, other folders |
| --- | --- | --- | --- |
| lint (clippy, mend) | 372 h | 413 h | 457 h |
| test, check, doc | 297 h | 196 h | 513 h |
| build | — | 24 h | 38 h |

Three results follow:

1. natedev is out of CPU as well as memory budget. A second machine adds capacity; a config change on natedev does not.
2. The queue is full almost all day, weekends included. "Start when the queue backs up" will keep the machine up most hours. The cost model below uses about 20 to 24 h a day.
3. Lint is 54 % of the waiting. Clones and gate copies are 42 %. Today the Mac takes neither: lint goes to the Mac only during a natedev hold, and a clone's repository has no name the Mac knows (`offload.repository_identity` uses the folder name).

## Decisions

### Machine

- **Type: m7a.4xlarge** to start (Phase 0 result below; m7a.8xlarge was the first proposal). 16 vCPU (on 7a types each vCPU is a physical core), 64 GiB: about half of natedev's build throughput, enough for the large hana steps (hana nextest peaks at 11.6 GiB). m7a.8xlarge (32 vCPU, 128 GiB) is the next size. x86_64 only: an arm64 machine would be a third platform, and its results would not stand for natedev's.
- **Market: Spot**, a persistent request with interruption behavior `stop`. The machine will run most hours, so the price gap matters more than the interruptions (estimates below). A capacity miss at start is recorded and retried after `capacity_backoff_s`; the steps stay in natedev's queue as today. If misses are frequent in the report, the instance is replaced with an on-demand one. That needs a template change: the launch template has Spot market options only. The data volume keeps the caches.
  The account created 2026-11-03 is a project account. Its Paid Plan policy denied Spot launches. The user chose Spot and activated advanced features on 2026-11-03, which removed that policy.
- **Region:** us-east-2. The account's `RegionFloor` policy allows us-east-1, us-east-2 and us-west-2, and it denies most actions in us-east-1. The region is a config value.
- **Storage:** a separate 500 GiB gp3 data volume for `/home` (mirrors, build folders, sccache, rustup, cargo), 6,000 IOPS and 500 MB/s, encrypted, `DeleteOnTermination=false` and retained by the stack. The root volume holds only NixOS and the nix store. A stopped or replaced instance keeps every warm build folder. Size: hana slots at the Mac's 56 GiB budget, cargo-liner slots at 24 GiB, plus the nix store and sccache.
- **OS: NixOS**, a host `aws-build` in the nixos repo, from the official NixOS AMI. hana's flake already has a headless shell, `.#ci`, with llvmpipe, lavapipe and `xvfb-run`. hana's CI runs in it on a NixOS machine without a GPU. So the box runs the same tests CI runs, window tests included, and its results match natedev's.

### Cost (us-east list prices as known mid-2026; day one reads the real ones from the pricing API and Spot price history, both free)

| Item | Per month at 24 h a day |
| --- | --- |
| m7a.8xlarge on-demand, about $1.86/h | about $1,340 |
| m7a.8xlarge Spot, often a third to a half of on-demand | about $450 to $670 |
| Data volume (500 GiB gp3, extra IOPS and throughput) | about $70 |
| Public IPv4 address | about $4 |

The spending limit is the user's number; it is the one question in this design.

### Spending limit: three layers

1. **The box stops itself.** A systemd timer on the box powers it off after `idle_stop_minutes` with no build process. The instance's shutdown behavior is `stop`. This works when natedev is down or its scripts fail.
2. **natedev's spend guard.** The scaler keeps a ledger of instance run time (from the instance's state times) times the recorded price (Spot price history, read each hour) plus the fixed monthly costs. It does not start the box when the day's or the month's estimate reaches `daily_limit_usd` or `monthly_limit_usd`. At the limit it stops new work on the box, lets running steps end, and then stops the box (`limit_action=drain`). It tells the showrunner once per limit event.
3. **AWS Budgets.** A monthly budget at the cap, emails at 50, 80 and 100 %, and a budget action that stops the instance at 100 %. Cost Explorer data lags by up to a day, so this is the backstop, not the control.

### Access and keys

- **natedev to the box: Tailscale SSH.** natedev reaches the Mac the same way today (`ssh -G mac` shows `identityagent none`; the host is `mac.tail21e383.ts.net`). Tailscale checks the tailnet identity, so no ssh key and no gpg-agent take part. A cold gpg-agent after a reboot does not stop the box's work.
- **The box joins the tailnet once.** The user runs `sudo tailscale up --ssh --advertise-tags=tag:build-aws` in an SSM Session Manager window and logs in through the printed URL. No auth key exists to store. The tailnet policy lets natedev's user and the Mac's user ssh into `tag:build-aws` and gives the tag no connections out, so the box cannot open a connection to natedev or the Mac.
- **The box holds no GitHub credential.** Source trees arrive by rsync from natedev, as on the Mac. `~/.claude` scripts and config arrive the same way: natedev copies the tracked files when the box reports a different commit in its probe answer. crates.io, rustup and cache.nixos.org are public. hana has no git dependencies (it vendors `clay-layout`). If a workspace gets a git dependency later, natedev fetches it and the tree carries it.
- **natedev's AWS key.** A dedicated IAM user whose policy allows only `StartInstances` and `StopInstances` on the one instance ARN, the read-only `Describe*` calls for instances, Spot requests and Spot prices, and `ce:GetCostAndUsage`. The user writes its key at their own terminal to `~/.config/aws-build/credentials` (mode 0600, profile `build-scaler`). It is a file, not a gpg-agent secret, for the same reboot reason. A leaked key can only start or stop that instance, and layer 3 caps the cost.
- **Break-glass login:** SSM Session Manager (instance profile with `AmazonSSMManagedInstanceCore`). No inbound TCP port is open. The security group allows inbound UDP 41641 so Tailscale connects directly and not through a relay.
- Private source code is stored on an AWS disk. The volume is encrypted at rest with the account's default key.

### How the box fits the build targets

- `config/build_targets.conf`: `targets=mac,natedev,aws`, `kind.aws=ssh`, `platform.aws=linux`, `settings.aws=aws_target.conf`. AWS comes after natedev: a step goes to natedev's queue as today, and leaves it for the box while it waits. When natedev is held, routing goes on to the box directly.
- **Leaving the queue.** A step that waits for memory already polls its leave hook (`LINT_QUEUE_LEAVE_HOOK`) and leaves with status 75 when the Mac is free. The hook covers every eligible `ssh` target, the box included. The box is used at once when it is ready and has a free slot: once it runs, idle capacity costs the same as busy capacity.
- **Kinds.** A Linux `ssh` target takes `test`, `check` and `doc` at any time, not only during a natedev hold (its results are Linux results). `lint` follows in Phase 4 with write-back. `build`, `example` and `final` stay on natedev: they make natedev's own artifacts or are the full gate.
- **Slots.** The Mac takes one claim per repository. The box takes `slots.<repo>` claims per repository (default 2; hana 4, cargo-liner 4 to start). Each slot has its own mirror at `~/.local/state/build-target/mirror/<repository>/<target>-s<slot>` and a warm build folder. The build queue names `<repository>` from the policy name and the identity hash. Different repository identities have separate mirrors. A claim prefers the slot that last ran the same worktree. The box runs its existing memory gate with `build_budget_gib` from its settings, so slots never overfill its memory.
- **Repository identity.** Local claims and affinity files use the normalized origin URL, which memory admission also uses (`repo_identity`). Linux mirrors use the policy name and the identity hash. Copies with the same policy name and identity share a mirror per target and slot. The Mac keeps its source folder name rule.
- **Nix shell on the box.** The remote command enters the repository's devshell (`.#ci` for hana) from a small copy that holds only `flake.nix`, `flake.lock` and `nix/`, keyed by their hash, and caches `nix print-dev-env` for that key. It never evaluates the mirror as a flake: a `path:` flake copies the whole folder, build folder included, into the store.
- **Tool parity.** The probe answer carries `rustc -V`, `cargo nextest --version`, the mend version and the `~/.claude` commit. A difference from natedev makes the box install the matching version (or copy the scripts) before it takes work; until then it is `not ready`.
- **Natedev holds** do not apply on the box (it has no holder folder). A hold on the box is `/build:hold hold <why> target aws`.

### Start, stop and Spot interruptions

- **Start.** A natedev systemd user timer runs `aws_target.py tick` every 30 s. It reads the queue snapshot (`admission/queue.json`). It starts the box when at least `start_min_waiting` steps the box can take have waited `start_after_wait_s` or more, the spend guard allows it, and no capacity backoff is active. From stopped to ready (instance start, boot, Tailscale, probe) is about 1 to 2 minutes. The mean wait of a waiting step is 18.5 minutes.
- **Stop.** The box stops itself after `idle_stop_minutes` (layer 1). natedev stops it only for the spend limit or by hand (`aws_target.py stop`).
- **Interruption notice.** A box service reads the instance metadata (IMDSv2 `spot/instance-action`) every 5 s. On a notice it marks the box `draining`: the probe declines new runs, and each running step ends at once and prints `aws_interrupted=1`. natedev records the run as `lost`, reason `interruption`, two minutes before the box stops.
- **Retry.** A lost run already falls back to natedev when natedev is eligible and not held. The step goes back into natedev's memory queue at its original place (it keeps `BUILDLOG_MEM_WAIT_SINCE`); a step lost to an interruption does not go to the back.
- **Claims after a stop.** A remote claim stays until its leftovers are confirmed ended. For the box, an instance state of `stopping`, `stopped` or `terminated` proves that nothing runs. `tick` releases the box's kept claims when it reads such a state and records the evidence. No leftover check over ssh is needed.
- **Restart by AWS.** A persistent Spot request starts the box again when capacity returns. With no work, the box stops itself after `idle_stop_minutes`.

### Measurement

- **Dollars.** The ledger (above) per day, and once a day one Cost Explorer read of the real charge ($0.01 a call), so the estimate is checked against the bill.
- **Uptime.** Hours up count pending, running and stopping time, as the cost rule does (Phase 9 F045).
- **Work.** Box steps carry `host=aws-build` and reach natedev through `buildlog sync`, as Mac steps do. Calls carry `target=aws`.
- **Wait saved (estimate).** For a step that left natedev's queue after waiting t seconds: the median of (wait − t) over that day's natedev steps of the same kind and repository that waited longer than t. This is the rule `### Mac copies` uses. Add the difference between natedev's median run time and the box's run time for that command; it can be negative.
- **Report.** `/build:status` gets an `aws:` row: `stopped`, `starting since HH:MM`, `running <n> of <m> slots`, `draining — Spot interruption at HH:MM`, or `start refused — month limit reached`, each with the month's dollars against the cap. `/build:report` gets an `### AWS` section and one lead line: `AWS: up <h>, <n> steps, about <h> of queue wait saved for $<d> ($<d> per saved hour, estimated).` It also shows natedev's daily summed queue wait before and after go-live, labelled as not matched for load.

## Phases

| Phase | Delivers | Files outside aws-unit | Needs |
| --- | --- | --- | --- |
| 0. Replay | A replay of the last 14 days of natedev's queue with a second machine of each candidate size: wait saved, up-hours and expected dollars per saved hour. It confirms or changes the instance type and gives the user numbers before any spend. | none (reads the build log) | nothing |
| 1. Per-target storage and slots | The open item in `build-followups-build-targets-next.md`: claims, block and backoff under `~/.local/state/build-targets/<target>/` (the Mac's paths unchanged); `slots.<repo>`; `/build:hold --target <name>`; hold events per target; `buildlog sync` with each `ssh` peer; reports select the Mac by target, not by `host <> 'natedev'` (that test would count box steps as Mac steps). | `scripts/mac_test/` (verify-unit), `scripts/build_targets/`, `scripts/build_hold/` (resources-unit), `scripts/buildlog/` (report-unit) | showrunner: who owns these edits |
| 2. Linux runner and queue leave | The Linux remote command (devshell from the flake copy, box budget, `skip.<repo>`), mirrors by policy name and repository identity, and the leave hook for every eligible `ssh` target, for test, check and doc. Live proof against a loopback target (natedev over Tailscale SSH into a scratch home folder), at no cost. | `scripts/mac_test/offload.py`, `scripts/direct/verify.sh` (verify-unit), `scripts/lint/memory_gate.sh` (queue-unit) | Phase 1 |
| 3. AWS control and measurement — go-live | `scripts/aws_target/` (tick, start, stop, ledger, spend guard, claim release on instance state, interruption handling), `config/aws_target.conf`, the `aws:` status row, the `### AWS` report section, a CloudFormation template and a runbook the user runs, and the `aws-build` host plus natedev's tick timer in the nixos repo (a branch to the natedev showrunner). | `scripts/build_targets/build_targets.py` (resources-unit), `scripts/buildlog/report.py` (report-unit), `/etc/nixos` (natedev) | the user: account, stack deploy, Tailscale login, key file |
| 4. Lint on the box with write-back | The full lint order (mend --fix, fmt, taplo, clippy fix, fmt, clippy judgment) runs in the mirror. The rewritten files come back and are applied only when the local tree key still equals the key sent with the tree (`copy_tree_with_key` already reads it before and after the copy). Otherwise the result is discarded and the step runs again. Agents keep their automatic fixes. | `scripts/direct/verify.sh`, `scripts/mac_test/offload.py` (verify-unit) | Phase 3 |

Go-live is the end of Phase 3, with test, check and doc: about 1,000 of last week's 2,314 waiting hours. Phase 4 adds lint, about 1,240 more.

## Config — `config/aws_target.conf` (Phase 3)

`host`, `region`, `instance_id` (stack output, not a secret), `credentials_profile`, `connect_timeout_s`, `probe_timeout_s`, `unreachable_backoff_s`, `capacity_backoff_s`, `free_floor_gib`, `build_budget_gib`, `slots` and `slots.<repo>`, `target_budget_gib` and `target_budget_gib.<repo>`, `skip.<repo>`, `kinds`, `start_after_wait_s`, `start_min_waiting`, `idle_stop_minutes`, `daily_limit_usd`, `monthly_limit_usd`, `limit_action`, `fixed_usd_per_month`, `fallback_usd_per_hour` (used when no Spot price is recorded), `ci_repo=` (empty: the block's CI switch is off). The box reads `idle_stop_minutes` from its copy of the config.

## What the user does (at go-live, at their own terminal)

1. Create the account and an admin login (in progress).
2. Run the runbook: `aws cloudformation deploy` of the template, with the region from the placement score and the $400 cap.
3. In SSM Session Manager on the box: `sudo tailscale up --ssh --advertise-tags=tag:build-aws`, then add the tag rule to the tailnet policy.
4. Create the scaler user's access key and write it with `aws configure --profile build-scaler` on natedev.
5. Rebuild the box from the nixos branch. natedev was rebuilt on `/etc/nixos` 10353a9 (2026-11-03), which holds the patch.

## Spending cap (user, 2026-11-01)

`monthly_limit_usd=400`: the user's words were "$400/month cap is fine to start". `daily_limit_usd=20` is derived (the monthly cap / 20); the user approved it on 2026-11-03 ("approve all 4"). The AWS Budget uses the same $400. Phase 0 expects about $250 a month on Spot for test, check and doc, so the cap leaves room for Phase 4's lint (about $274).

## Review answers (user, 2026-11-02)

Finding ids restart in each phase, so each answer names its phase.

### Phase 8

- **F038, "a".** The Linux runner prints the `aws_interrupted=` marker once. The second print in `execute_slot` goes; the sender still reports the interrupted run, and the exit status is the child's. This is plan Phase 10.
- **F039, "d".** The build log entry does not store `interruption_generation`. Every outcome writes a plain SSH attempt record; the generation stays in the remote record for routing.
- **F040, "a - i want only one way of doing things".** `conf.py` reads a config file once, and every caller (`scripts/aws_target/config.py` included) uses that one reader. devtools-unit makes this change.

### Phase 9 (AWS cost and status reporting)

- **F034, "b": declined.** Status keeps reading the build log files for attempt results, not the build log index. Reading only the `YYYY-MM.jsonl` files gives the speed.
- **F035, "yes we should honor the cost limits": approved.** The cap, the accounting zone and both Cost Explorer tag keys are read only through the strict settings reader. An invalid cap is refused and the guard stops; status shows the cap as unavailable. A missing tag key stops AWS work.
- **F036 and F040, copy removals: approved** under "you never have to ask me to remove duplication". One local-step rule serves the SQL and Python paths for old rows with no target. The report prints the Mac sync state once (`peer_sync_lines` replaces `mac_note`).
- **F039, "a": approved.** The build report shows every AWS target, not only the target named `aws`.
- **F041, "a": approved.** The status reader is separate from the report reader. Status does not read the charge files or `go-live.json`.
- **F042, "a": approved.** Each tick reads only the newest charge file. A saved reply that is not reconciled still reconciles first.
- **"settle": approved** (the showrunner's recommendations):
  - F037: the memory section counts steps and calls by the target rule; machine samples keep the host rule.
  - F038: the wait-saved estimate uses the report's local day.
  - F043: a day's cost prices only that day, not the whole ledger history.
  - F044: `CostLedger.refresh_charges` owns the charge schedule, its lock and the reconcile-once check.
  - F045: uptime counts pending, running and stopping time, with the cost rule's cap on a stale observation.
  - F046: the controller record stores the recovery case; status builds the recovery words.
  - F047: the controller state file has no `charge_error`.
  - F048: the charge difference is computed (actual minus estimate), not stored in `charges/<day>.json`.
- **F049, "settle": declined.** No index on `ssh_attempts` for now; the table is small.

## Phase 0 result — queue replay (2026-10-31)

Script: `docs/plans/builds-aws/replay.py` (scratch analysis, reads the build log index read-only; rerun with `python3 -I docs/plans/builds-aws/replay.py`).

**Data.** natedev steps from 2026-10-24T00:00Z to 2026-10-31T17:43Z (7.75 days): 12,983 steps, 2,316 waiting hours. Sweep steps (4,297) are left out: they are disk cleanup and never wait for memory. With them the count is 17,280. Mac steps are left out (they never enter natedev's queue).

**Model.** natedev is a memory budget with first-fit admission in queue order; after 600 s the head reserves its need (the real head rule, simplified). Each step needs 0.65 of its recorded cgroup peak (admission's `ANON_SHARE`). Steps of one verify call run in order: the next step arrives at the previous step's end plus the recorded gap. The budget that reproduces the real wait is 32.5 GiB (simulated 2,308 h against 2,316 h). The box has 0.5 / 1.0 / 1.5 times that budget for m7a.4xlarge / 8xlarge / 12xlarge (natedev's Ryzen 9 9950X has about the build throughput of 32 Genoa cores). A box step runs the recorded natedev duration times f (1.0, and 1.3 for slower cores). A waiting `verify.sh` step leaves for the box when the box is up and has room. The box starts when 2 steps it can take have waited 120 s, is ready 120 s later, and stops after 15 idle minutes. Lint means clippy, mend and fmt steps (Phase 4, write-back).

**Prices.** On-demand and Spot from instances.vantage.sh on 2026-10-31: m7a.4xlarge $0.927 / $0.483, m7a.8xlarge $1.855 / $0.893; m7a.12xlarge $2.782 / about $1.34 (estimated from the 8xlarge ratio). Plus $74 a month for the data volume and the IPv4 address.

**Result, per 30 days, f = 1.0** (f = 1.3 changes saved hours by less than 1 % and adds 2 to 15 % to the cost):

| Size | Lint | Saved wait | natedev wait left | Box up | Spot $/month | On-demand $/month | $ per saved hour, Spot / OD |
| --- | --- | --- | --- | --- | --- | --- | --- |
| no box | — | — | 8,939 h | — | — | — | — |
| m7a.4xlarge | no | 8,211 h | 728 h | 12.1 h/day | 250 | 411 | 0.03 / 0.05 |
| m7a.4xlarge | yes | 8,463 h | 477 h | 13.8 h/day | 274 | 457 | 0.03 / 0.05 |
| m7a.8xlarge | no | 8,539 h | 401 h | 12.2 h/day | 400 | 752 | 0.05 / 0.09 |
| m7a.8xlarge | yes | 8,711 h | 229 h | 13.3 h/day | 431 | 816 | 0.05 / 0.09 |
| m7a.12xlarge | no | 8,567 h | 373 h | 12.1 h/day | 559 | 1,080 | 0.07 / 0.13 |
| m7a.12xlarge | yes | 8,745 h | 194 h | 13.1 h/day | 599 | 1,164 | 0.07 / 0.15 |

**Reading.** natedev's queue is at saturation: a small added capacity removes most of the wait. m7a.4xlarge with test, check and doc alone removes 92 % of it. 8xlarge saves 328 more hours a month for $150 more on Spot. Lint adds about 250 saved hours on the 4xlarge, because tests leaving natedev also shorten the lint queue.

**Limits of the model.** New calls arrive at their recorded times; with shorter waits agents send work sooner, so real demand rises, and the box's up hours and cost with it. Routing is per step, not per call. Spot interruptions are not modelled. The need comes from each step's own peak, not admission's estimate. 7.75 days of data.

**Decision.** Start with **m7a.4xlarge Spot**, test, check and doc (go-live at the end of Phase 3): about $250 to $275 a month expected. Lint (Phase 4) follows only when the live report shows lint waits still worth it. Move to m7a.8xlarge when the live report shows the box full (its slots busy while natedev still queues); the data volume keeps the caches through that change. The design's Machine section is updated to this choice.
