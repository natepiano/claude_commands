# AWS build target

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Add a Linux SSH target with spending controls.

> **Production: builds** — unit `aws-unit`; production doc `docs/plans/builds-production.md`

Source: `docs/plans/builds-aws-design.md`, design Phases 1–3 only. The user authorized compilation on 2026-11-01.

Design Phase 0 is done at `bc636f47`. Its queue replay selected `m7a.4xlarge` Spot for test, check and doc.

**Later:** Design Phase 4 adds lint with write-back; see [the design](builds-aws-design.md#phases).

## Delegation Context

- **Project:** `~/.claude` supplies the build commands, target coordination, memory admission and build records.
- **Stack:** Python 3.13, standard library, unittest, strict basedpyright, Bash, AWS CLI, CloudFormation JSON and NixOS 26.05.
- **Layout:** `config/` holds settings. `scripts/mac_test/` holds SSH execution and claims. `scripts/build_targets/` reads targets and shows status.
  `scripts/build_hold/` controls holds. `scripts/lint/` controls admission. `scripts/buildlog/` records work. `scripts/aws_target/` will control AWS.
- **Key files:**
  - `docs/plans/builds-aws-design.md` — resolved behavior and source phase numbers.
  - `docs/plans/builds-production.md` — owners and checkpoint notices.
  - `config/build_targets.conf` — ordered targets, platform, kind and settings path.
  - `config/mac_test.conf` — existing Mac settings and compatibility values.
  - `config/aws_target.conf` — new AWS settings, deployment settings and spending limits.
  - `config/README.md` — configuration reference.
  - `scripts/build_targets/target_config.py` — `LocalTarget`, `SshTarget`, `read_targets`, `uses_legacy_mac_layout` and `BUILD_TARGETS_CONFIG`.
  - `scripts/build_targets/targets.py` — `target_eligibility`, `target_state` and typed state values.
  - `scripts/build_targets/build_targets.py` — `command_kind_eligible`, `mac_target_eligibility`, `command_status`, `format_state`, `command_wait_local`.
  - `scripts/mac_test/mac_test.py` — `StatePaths`, `state_paths`, `claim_scope`, `claim_path`, `claim_once`, `command_claim`, `command_release` and blocks.
  - `scripts/mac_test/offload.py` — `OffloadRequest`, `read_config`, `repository_identity`, `remote_mirror`, `probe_script`, `remote_test_command` and `claimed_run`.
  - `scripts/mac_test/linux_runner.py` — cached shell inputs, `probe_shell`, `run`, slot process markers and cleanup evidence.
  - `scripts/mac_test/test_linux_runner.py` — fake Linux execution, shell cache, process ownership and startup refusal regressions.
  - `scripts/mac_test/offload_test_support.py` — `OffloadCommandFixture`, temporary state and SSH/rsync command fakes.
  - `scripts/mac_test/offload_test_commands.py` — executable command fakes.
  - `scripts/mac_test/copy_watch.py` — Mac copy ownership and cleanup behavior.
  - `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py` — existing claim and execution test style.
  - `scripts/build_targets/test_targets.py`, `scripts/build_targets/test_build_targets.py` — local fixtures and forbidden external command checks.
  - `scripts/build_hold/build_hold.py` — `parse_hold_target`, `run_mac_hold`, `release_mac_hold` and hold events.
  - `scripts/build_hold/override_guard.py` — installed scripts refuse fixture path overrides; tests run from a checkout.
  - `scripts/direct/verify.sh` — `route_test`, `try_mac_test`, `mac_retry_hook`, `run_local_test` and local admission.
  - `scripts/direct/step_record.py` — live-step eligibility, selected targets and legacy record decoding.
  - `scripts/buildlog/mac_idle.py` — Mac idle observations from live-step records.
  - `scripts/direct/verify_mac_offload_test_support.py` — existing fake verify environment.
  - `scripts/lint/invoke.sh` — Linux memory admission and command execution.
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory`, `LINT_QUEUE_LEAVE_HOOK` and status 75.
  - `scripts/lint/memory_admit.py` — `remote_identity`, `git_identity`, `read_queue_snapshot`, `QueueInputs` and snapshot serialization.
  - `scripts/lint/memory_test_support.py` — `fake_memory_readings` and `gate_isolation_environment`.
  - `scripts/lib/atomic_file.py` — `write_text_atomic` and `create_text_atomic`.
  - `scripts/lib/conf.py` — the sole configuration reader; validates live values and recovers usable committed values.
  - `scripts/lib/nix_env.py` — existing shell environment loading and trust checks.
  - `scripts/lib/process_start.py` — process identity helper used by the held verify checkpoint.
  - `scripts/lib/scratch.py` — managed temporary copies and cleanup.
  - `scripts/buildlog/record.py` — `synchronized_hosts`, `target_hosts`, `recorded_target`, `step`, `queue_priority` and hold records.
  - `scripts/buildlog/sync.py` — `configured_peers` for multi-peer exchange; `peer_name` for the Mac vault path; per-peer status and pause handling.
  - `scripts/buildlog/report.py` — `reported_mac_calls`, `mac_section`, `copy_saving_estimate` and report assembly.
  - `scripts/buildlog/mac_report.py` — tree-key evidence by target; Linux passes never supply natedev pass evidence.
  - `scripts/buildlog/health.py` — recorder health through configured host mappings.
  - `scripts/buildlog/store.py`, `scripts/buildlog/index.py` — durable records and SQLite schema.
  - `scripts/production/cli_fixture.py` — synchronous CLI tests with isolated process state.
  - `pyrightconfig.json` — Python execution environments.
  - NixOS repository context: `flake.nix`, `configuration.nix`, `modules/linux/buildlog.nix`, `modules/linux/tailscale.nix`, `modules/common/agents.nix`.
    These paths are relative to the NixOS repository. Phase 11 prepares a separate branch for the natedev showrunner.
- **Test lanes:** Python uses adjacent `test_*.py` files, rather than Rust `tests/` directories.
  New tests use these same lanes in `scripts/mac_test/`, `scripts/build_targets/`, `scripts/build_hold/`, `scripts/direct/`, `scripts/buildlog/` and `scripts/aws_target/`.
- **Build:** none for Python and shell. Phase 11 names the NixOS branch checks separately.
- **Test:** use each Work Order's exact `python3 -m unittest discover` commands from the repository root.
- **Lint:** use each slot's exact basedpyright command. Each slot requires `0 errors, 0 warnings, 0 notes`.
  Shell changes require the listed `bash -n` command. No file-level ignores apply. Avoid `Any`; use `TypedDict` for known-key dictionaries.
- **Shipped claim contract:** An empty `OffloadRequest.target` selects the legacy Mac default.
  Pass explicit target names through every selected-target operation. Use `target_config.uses_legacy_mac_layout(platform)` for layout decisions.
  Read the slot from the claim answer. Preserve `repo_name`, `repo_key`, `slot`, and `cleanup_pending`.
  Status 13 requires cleanup before execution. Repeated takeover attempts must preserve this requirement.
  Keep affinity internal. It selects a preferred free slot and grants no ownership.
  Preserve `.local/state/build-target/mirror/<path_key>/<slot>` on Linux and all existing Mac paths.
- **Shipped execution contract:** Copy tracked scripts, public configuration and source before the reserved slot's Linux execution probe.
  The execution probe evaluates the cached shell and checks parity inside that shell.
  Use `probe_timeout_s` for reachability and `shell_timeout_s` for shell evaluation.
  Routing eligibility must not evaluate a shell before reservation.
  `linux_startup=unresolved` maps to `BusyMacDecline(LostRunLeftovers())` through the probe and stream parsers.
  `LeftoversRemain` keeps ownership and applies no unreachable backoff.
  Missing completion evidence remains `LostMacRun`.
  `.linux-run.json` identifies retained slot processes; unresolved ownership keeps the marker and claim.
  Reuse the existing LRU sweep for slot budgets; remove the whole build folder only for the disk floor.
- **Shipped record contract:** Phase 2 added `steps.target` in schema version 27.
  Reuse `record.recorded_target`, configured host mappings, and the existing Mac classification predicates.
  An active AWS `SshTarget` enables record exchange even when `offload=off`.
  Install tracked scripts and non-secret configuration before activation. Keep `BUILDLOG_PEER` unset in the normal multi-peer service.
  Buildlog synchronization excludes admission state. Phase 8 must provide an explicit natedev-initiated guard channel.
  `verify-linux` keeps `BUILDLOG_SENT_TREE_KEY`; its successful steps supply evidence for their own target, never natedev.
- **Configuration reader and fixtures:** Read configuration only through `scripts/lib/conf.py`.
  `conf.values` accepts an explicit `Path`; typed numeric reads require the corresponding limit comments.
  Tests set `CONF_DIR` beside `MAC_TEST_CONFIG` and install a private `build_targets.conf` there.
  Include numeric limits in private settings and the `recent_minutes` range in private `lint.conf`.
  A committed fallback value does not establish live spending permission.
- **Configuration contract:** Preserve the design's complete AWS key set:
  `host`, `region`, `instance_id`, `credentials_profile`, `connect_timeout_s`, `probe_timeout_s`, `unreachable_backoff_s`, `capacity_backoff_s`,
  `free_floor_gib`, `build_budget_gib`, `slots`, `slots.<repo>`, `target_budget_gib`, `target_budget_gib.<repo>`, `skip.<repo>`, `kinds`,
  `start_after_wait_s`, `start_min_waiting`, `idle_stop_minutes`, `daily_limit_usd`, `monthly_limit_usd`, `limit_action`,
  `fixed_usd_per_month`, `fallback_usd_per_hour`, `limit_reserve_usd`, `drain_max_minutes` and `ci_repo`.
  Set `credentials_profile=build-scaler`, `kinds=test,check,doc` and `ci_repo=`. The empty CI setting prevents GitHub calls from the box.
  Keep extra shell, storage, parity, accounting, deployment and timer tunables in these same configuration files.
  Set `kind.aws=ssh`, `platform.aws=linux` and `settings.aws=aws_target.conf` when the user activates AWS.
  Choose `us-east-1` or `us-east-2` from the user's placement-score read at go-live; store the result in `region`.
- **Invariants:**
  1. Preserve the settled machine: `m7a.4xlarge`, x86_64, persistent Spot request, interruption behavior `stop`, NixOS host `aws-build`.
  2. Preserve the retained, encrypted 500 GiB gp3 data volume for `/home`, with 6,000 IOPS and 500 MB/s.
  3. Permit test, check and doc on AWS. Keep lint for the design's later phase. Keep build, example and final on natedev.
  4. Use Tailscale SSH. Store no GitHub credential on the box. Transfer source and tracked scripts from natedev.
  5. Read every tunable at use from `config/aws_target.conf` or `config/build_targets.conf`.
  6. Keep `monthly_limit_usd=400` and `daily_limit_usd=20` in `config/aws_target.conf`, per the user, 2026-11-01.
  7. Derive the AWS Budget amount from `monthly_limit_usd`. Do not duplicate spending limits in code or raise either limit.
  8. Enforce the spending guard before the first AWS resource, including deployment and the first boot.
  9. Create or start no AWS resource before the final go-live gate in Phase 11, which completes design Phase 3.
  10. Let the user run console operations, CloudFormation deployment, Tailscale login and AWS credential configuration at their terminal.
  11. Pass no password, AWS key or Tailscale auth key through a session. Do not read credential files into output.
  12. Fake all AWS CLI/API calls in tests. Fake SSH, clocks, process state and machine memory in automated tests.
  13. Use `fake_memory_readings` for meminfo, pressure and cgroup isolation. Keep tests fast through injected time and synchronous fixtures.
  14. Preserve status 75, real failure results, tree identity checks, staged doc semantics and retained remote claims after uncertain cleanup.
  15. Let aws-unit make all Phase 1–3 design edits, including other units' files, per this dispatch.
  16. Carry each phase's Also touches line into its checkpoint notice, per the production document.
  17. Prepare the NixOS branch separately. Never push or modify `/etc/nixos` main, per this dispatch.
  18. Keep the public API small. Reuse claims, admission and records. Name costs added to frequent command paths.

## Held branch context

The requested `origin/builds-verify` ref does not exist locally or on origin.
The production document names `origin/natedev-verify`; this plan uses that actual verify branch for conflict research.
No branch was merged or copied during compilation.

| Ref | Inspected tip | Held work |
| --- | --- | --- |
| `origin/builds` | `773ec40d8` | comparison base |
| `origin/natedev-verify` | `a6905e09e` | claim transfer, Python startup reduction, token renewal and retry behavior |
| `origin/builds-resources` | `50d1cfb1d` | typed queue order, token wait exclusion, cleanup retry and lazy record imports |

Each Work Order carries the relevant function conflicts from its path-filtered diff and log inspection.
The source verify checkpoints include `13c460870`, `766b3e87d` and `3be58002a`.
The source resources checkpoints include `8c37748d1` and `fd1440b84`.
Keep these changes independent until the showrunner integrates them. Do not merge or copy either held branch.

## Phases

Design Phase 1 maps to Phases 1–2. Design Phase 2 maps to Phases 3–5. Design Phase 3 maps to Phases 6–11.
The divisions separate state, execution, routing, cost accounting, control, reporting and deployment.

### Phase 1 — Isolate target state and holds  · status: done
**Depends on:** none

#### As-built
- `mac_test.state_paths(target)` returns a target-aware `StatePaths`. The Mac keeps `~/.local/state/mac-test` and its fixture overrides; every other target uses `~/.local/state/build-targets/<target>/` for claims, locks, block and unreachable backoff.
- `mac_test.configured_ssh_target(target)` resolves every target, the Mac too, through `target_config.read_targets`. Platform and kind come from `config/build_targets.conf`; an unknown or non-ssh name is refused before any state write. `mac_test.read_config(target)` reads the target's `settings.<target>` file.
- Claim, release, status, block and backoff take the selected target. The no-target Mac CLI keeps its behavior. Hold notices, expiry and warning texts and their repeat keys name the actual target.
- `offload.state_directory`, `backoff_path`, `read_backoff` and `write_backoff` take the target name. `targets.target_state` reads the selected target's state with no probe and no write; a malformed block refuses admission for that target; only the named state-read errors count as unreadable state.
- `build_hold.py` accepts `target <name>` and `--target <name>` for hold and release. `run_mac_hold` and `release_mac_hold` always pass `--target <name>` to `mac_test.py`, so the Mac is checked against the registry like any target. natedev holds stay local. `record.py` records the actual target on hold begin and end events.
- `config/aws_target.conf` holds the disabled AWS settings: `host=aws-build`, empty `instance_id`, `credentials_profile=build-scaler`, `kinds=test,check,doc`, empty `ci_repo`, `monthly_limit_usd=400`, `daily_limit_usd=20`, `limit_reserve_usd=3`, `drain_max_minutes=60`, `build_budget_gib=32`, `target_budget_gib=24`, `target_budget_gib.hana=32`, `slots=2`, `slots.hana=4`, `slots.cargo-liner=4`. The active list stays `targets=mac,natedev`.

**Files:**
- `scripts/mac_test/mac_test.py` — target state, configuration and hold plumbing
- `scripts/mac_test/offload.py` — per-target backoff
- `scripts/build_targets/targets.py` — selected-target state reading
- `scripts/build_hold/build_hold.py` — configured SSH hold dispatch
- `scripts/buildlog/record.py` — actual target on hold events
- `config/aws_target.conf`, `config/README.md` — AWS settings and reference
- `commands/build/hold.md` — target syntax
- `scripts/mac_test/test_target_state.py`, `scripts/build_hold/test_target_hold.py` — regression tests

**Binds later work:** `state_paths(target)` is the one place that maps a target to its state directory. `configured_ssh_target` is the one registry check for every target. No repository budget in `config/aws_target.conf` may exceed `build_budget_gib`.

**Gotchas:** The held verify branch adds attributes to the same `mac_test.py` class as `self.target`; a merge keeps both.

**Also touches:** `scripts/mac_test/` (owner verify-unit); `scripts/build_targets/`, `scripts/build_hold/`, `commands/build/hold.md` (owner resources-unit); `scripts/buildlog/record.py` (owner report-unit).

### Phase 2 — Allocate repository slots and exchange target records  · status: done
**Depends on:** Phase 1

#### As-built
- `offload.repository_identity` returns `KnownRepository(name, key)`: `name` is the legacy source directory name, `key` is admission's normalized origin URL, and `path_key` encodes the key for paths. Worktrees, phase clones and temporary clones of one origin share the key; different origins with one name stay distinct.
- `target_config.uses_legacy_mac_layout(platform)` is true only for `macos`. The Mac keeps one mirror and one claim per source directory name and its old mirror paths. Linux SSH targets use `~/.local/state/build-target/mirror/<path_key>/<slot>`, one warm build folder per slot (`offload.remote_mirror(repo, target, slot)`).
- The claim protocol carries a slot. `mac_test.py` allocates under the target lock from `slot_capacity` (`slots.<repo>`, then `slots`), prefers the free slot that last ran the same worktree (an internal affinity file, pruned on write), and answers `slot: N`. `read_claim_answer` parses it; `OffloadRequest.target` (empty means the Mac) and `slot` carry it to execution.
- Claim records hold `repo_name`, `repo_key`, `slot` and `cleanup_pending`. A takeover of a lost run keeps the job fields, sets `cleanup_pending`, and does not need the original worktree; a claim retry against a slot with `cleanup_pending` answers status 13 again.
- `buildlog sync` on natedev pulls from every configured SSH peer (`SyncPeer(target, host)`); each peer gives back only its own host folders (`record.target_hosts(target)`). Per-peer status lives in `sync-peers/<host>.json`, seeded from and mirrored to the legacy `sync.json` for Mac targets. A `BUILDLOG_PEER` override keeps the legacy all-host pull. `sync.ssh_options()` reads `sync.connect_timeout_s`.
- `record.py` maps hosts to targets from `sync.hosts.<target>` in `config/build_targets.conf` (`target_hosts`, `synchronized_hosts`, `recorded_target`, `configured_targets`, `mac_targets`, `local_targets`); each step record has a `target` field, and `index.py` schema 27 adds a step `target` column. `report.py` selects Mac steps by target, so AWS steps never enter the Mac section; `health.py` adds `Machine.AWS` and reads active machines from configured targets.

**Files:**
- `scripts/mac_test/mac_test.py` — slot allocation, affinity, claim fields and takeover
- `scripts/mac_test/offload.py` — repository identity, slot mirror paths and claim answer
- `scripts/build_targets/target_config.py` — `uses_legacy_mac_layout`
- `scripts/buildlog/sync.py`, `record.py`, `report.py`, `health.py`, `index.py`, `vault_pull.py` — peer exchange, host-to-target mapping and target classification
- `config/aws_target.conf`, `config/build_targets.conf`, `config/README.md` — slot, host mapping and `sync.*_timeout_s` settings
- `scripts/mac_test/test_target_slots.py`, `scripts/buildlog/test_target_sync.py`, `scripts/buildlog/test_target_report.py` — regression tests

**Binds later work:** An empty `OffloadRequest.target` selects the legacy Mac default; selected-target operations take an explicit target name. Layout decisions go through `uses_legacy_mac_layout`. Status 13 requires cleanup before execution. `sync.hosts.<target>` is the one host-to-target mapping; AWS stays out of `targets`.

**Gotchas:** The origin name can differ from the source directory name (`.claude` and `claude_commands`), so Mac paths and claims key on the directory name. A multi-peer pull overwrites fresher records unless each peer returns only its own host folders.

**Ruled out:** Rewriting the claim's worktree on takeover; keying Mac mirrors by origin name.

Assertion-Change: scripts/buildlog/test_index.py schema version 26 -> 27 (step target column).
Assertion-Change: scripts/buildlog/test_sync.py pinned host lists gain mac, macbook, aws-build (item 5).
Assertion-Change: scripts/mac_test/test_offload_cleanup.py mirror selector call(HANA) -> call(HANA, "mac", 1).
Assertion-Change: scripts/buildlog/test_vault_pull.py transport expects sync.ssh_options() instead of sync.SSH_OPTIONS (timeout from config).
Assertion-Change: scripts/mac_test/test_mac_test.py takeover field comparison also excludes cleanup_pending and asserts it is True (F002 mark).
Assertion-Change: scripts/mac_test/test_target_slots.py origin-forms test compares normalized keys and legacy names; a Mac clone under another name claims its own mirror (status 0), per the Mac mirror-name claim scope.
Assertion-Change: scripts/buildlog/test_target_sync.py transfer and host-mapping tests expect each configured peer pull to carry only that peer's own host folders.

**Also touches:** `scripts/mac_test/` (owner verify-unit); `scripts/build_targets/`, `config/build_targets.conf` (owner resources-unit); `scripts/buildlog/` (owner report-unit); `config/README.md`, `config/aws_target.conf` (hub).

### Phase 3 — Execute Linux work in cached development shells  · status: done

#### As-built

- Linux dispatch uses the selected target for eligibility, backoff, probes, execution, cleanup, and results. Configured `kinds=test,check,doc` apply without Mac `linux_only.*` settings or Mac backoff. Admission's repository name governs Linux policy overrides across differently named clones. `RepositoryPolicyName` and `LegacyMirrorName` separate policy settings from legacy Mac paths.
- Each slot caches shell exports by input paths, contents, and the configured shell selector. Inputs contain only `flake.nix`, `flake.lock`, and `nix/`. Successful evaluation publishes exports atomically. Cache reuse checks referenced store paths and reevaluates missing paths. Repository trust checks remain active.
- Tracked scripts transfer before directory rsync copies source with `--delete` and an exclude file. The script stamp requires unchanged tracked content and HEAD. Source copies include untracked files and apply deletions. Exclusions protect ignored paths, `.git`, and `target`. Reachability uses `probe_timeout_s`; shell evaluation adds `shell_timeout_s`. Rust, nextest, and mend parity checks use the configured shell. The probe also checks the tracked script commit.
- Execution uses the claimed slot's mirror and warm build folder. It preserves caller arguments, staged doc snapshots, sent tree keys, and real failure status. `BUILDLOG_BUILD_BUDGET_GIB` sets the budget before Linux memory admission. The existing `sweep_workspace` trims old output under `target_budget_gib`; the disk floor removes the selected build folder. Repository overrides apply at use.
- `.linux-run.json` records ownership before shell evaluation. Process selection separates confirmed PIDs from unresolved evidence. Unconfirmed cleanup retains the marker and claim unless a later check confirms `LeftoversEnded`. `linux_startup=unresolved` starts no shell or work and maps to `BusyMacDecline(LostRunLeftovers())` without backoff. Target eligibility returns `ExcludedTarget` with reason `busy`. Missing completion remains `LostMacRun`. Cleanup after takeover affects only the retained slot.
- `verify-linux` records preserve the sent tree key and never provide natedev pass evidence. The manual loopback proof uses a managed scratch home for test, check, and staged doc. Configuration supplies SSH settings and private memory limits. The proof refuses limits that cannot admit its fixture.

**Files:**

- `scripts/mac_test/linux_runner.py` — shell cache, probes, execution, process selection, budgets, and cleanup markers.
- `scripts/mac_test/offload.py`, `scripts/mac_test/mac_test.py` — target dispatch, policy identity, transfers, claim recovery, and result parsing.
- `scripts/mac_test/loopback_proof.py` — manual scratch proof with configurable SSH settings and admission limits.
- `scripts/buildlog/record.py`, `scripts/buildlog/mac_report.py` — Linux tree keys and target-specific pass evidence.
- `scripts/buildlog/sync.py` — configuration reads through `conf.py`.
- `scripts/build_targets/build_targets.py` — busy eligibility for unresolved startup.
- `config/build_targets.conf`, `config/aws_target.conf`, `config/README.md` — shell, budget, parity, and loopback settings.
- `scripts/mac_test/test_linux_runner.py`, `scripts/mac_test/test_target_slots.py` — shell, policy, transfer, process, budget, and recovery tests.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_mac_report.py`, `scripts/build_targets/test_build_targets.py` — record, evidence, and eligibility regressions.

**Gotchas:**

- Linux mirrors use `.local/state/build-target/mirror/<path_key>/<slot>`. An empty `OffloadRequest.target` selects the legacy Mac default. Mac paths and output remain unchanged.
- Status 13 requires cleanup before execution, including repeated takeover attempts. Affinity grants no ownership.
- A process of the same user can deny reads of `environ` or `cwd`. Unresolved attribution never adds an unattributed PID to the kill list.
- Configuration reads use `scripts/lib/conf.py`. Tests set `CONF_DIR` with a private `build_targets.conf` and numeric limits beside `MAC_TEST_CONFIG`.

**Ruled out:** Manifest source copies; whole-folder removal for budget overflow; tool parity checks outside the shell; full-mirror `path:` flake evaluation; user commands for affinity.

### Phase 4 — Route queued work to eligible SSH targets  · status: done

#### As-built

- Queued test, check, and staged doc steps use eligible SSH targets in configured order, including Linux capacity without a natedev hold.
  Local-only requests, Mac platform restrictions, and local execution for build, example, final, and AWS lint remain enforced.
- `ssh_target_eligibility` checks configuration, backoff, capacity, and reachability without shell evaluation. Only busy/blocked exclusion answers retain SSH retry eligibility.
  `reserve` and `release-wait` manage process-bound claims. Reservation precedes queue departure; `MacClaimFromWait` transfers ownership without an unclaimed interval.
- `linux_startup=unresolved` and `LeftoversRemain` produce `BusyMacDecline(LostRunLeftovers())` without backoff. Unconfirmed cleanup retains the claim and original queue age.
  The next retry considers later targets first. Copies precede the execution probe; shell evaluation starts only after reservation.
- `BUILDLOG_MEM_WAIT_SINCE` and `BUILDLOG_MEM_WAIT_FIRST_FILE` preserve the first queue timestamp across remote loss and local fallback.
  Remote execution releases local reservations. Local fallback requires admission; confirmed remote failure does not trigger a local rerun.
- `PassedOnMac`/`FailedOnMac` become `PassedOnTarget`/`FailedOnTarget`; Mac wire results remain compatible. `AttemptEvidence`, `write_result`, and `persist_attempt` retain completion and cleanup separately.
  Evidence includes an explicit unavailable count and separate evidence time. Outcomes distinguish confirmed failure, loss, refusal, and `no_tests`.
  The schema version 28 `ssh_attempts` table retains target, repository identity, queue timestamp, departure timestamp, slot, outcome, and evidence across local fallback.
- `StepRecord` uses `SshAllowed`, `NamedPackage`, and `WorkspacePackages`, with explicit eligible targets and selected target. Existing records remain readable.
  AWS attempts record `target=aws`; remote steps record `host=aws-build`. Linux waits and execution remain outside Mac idle alerts and copy execution.

**Files:**

- `scripts/direct/verify.sh`, `scripts/direct/step_record.py` — routes, reservation transfer, queue age, and live target state.
- `scripts/build_targets/build_targets.py`, `scripts/mac_test/mac_test.py` — eligibility, reservation, adoption, and release commands.
- `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py` — target results and attempt evidence; `config/aws_target.conf` — timeout limits.
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py` — durable attempts; `scripts/buildlog/mac_idle.py`, `scripts/mac_test/copy_watch.py` — Mac consumers.
- Adjacent route, eligibility, record, index, idle, copy, and Linux runner tests cover these behaviors.

**Binds later work:** Admission uses `ssh_target_eligibility` and `command_reserve`.
The consumer titled Report AWS use, cost and estimated wait saved reads `ssh_attempts`, written by `offload.persist_attempt`.

**Gotchas:** Mac wait registration precedes reservation so `mac_idle.waiting_calls` sees failed reservations.
Unresolved cleanup advances to another target without backoff. Missing completion remains loss; confirmed completion alone does not release retained capacity.
`probe_timeout_s` and `shell_timeout_s` remain separate. Configuration fixtures use `CONF_DIR`, private configuration files, and numeric limit comments.

**Ruled out:** A separate admission route; immediate retry of the unresolved target; AWS use of Mac lint policy.

### Phase 5 — Repair verify and hold routes broken by SSH routing  · status: done

#### As-built

With no eligible SSH target, the Mac and natedev verify routes and the build-hold enforcer behave as they did before SSH routing.

- `hold_enforcer.protected_reason` protects every valid step state except `ON_NATEDEV`, so a hold stops only local work; `WAITING_SSH` and `ON_SSH` steps are not signalled.
- `build_targets.py reserve` returns 0 when it reserves or recovers a claim, 11 for a busy macOS target, and 1 for no reservation. A `claim_wait` answer of 11 on macOS also reports busy.
- `reserve --recover-only` recovers only the step's own wait claim or a retained claim and creates no new claim; otherwise a macOS target answers 11.
- `verify.sh` `mac_retry_hook` takes the reservation command as its third argument. A `free-now` answer of `free` or `busy` (an empty successful answer counts as free) lets the retry proceed; a `busy` answer passes `--recover-only`, so a busy Mac gets no extra runner attempt.
- `verify.sh` omits `--target` only when `target_for_settings` gives `mac`; every other macOS target is named explicitly. `target_platform` reads `build_targets platform`, and only under `mac_allowed` does the unresolved name `mac` fall back to macOS.
- The legacy `mac_allowed` route keeps its retry deadline, idle-marker, `free-now` event and local-release reservation transfer behavior.

**Files:**
- `scripts/build_hold/hold_enforcer.py` — natedev-work predicate for holds.
- `scripts/build_targets/build_targets.py` — reserve status contract and `--recover-only`.
- `scripts/direct/verify.sh` — Mac retry hook, explicit target naming and platform lookup.
- `scripts/direct/test_verify_ssh_targets.py` — retained-claim recovery, recovery without a reservation file, named-Mac reservation under default-Mac backoff, and Linux exclusion that records no Mac decline.

**Binds later work:** The spending guard keeps the 0/11/1 reserve statuses and adds a refusal reason of its own; only `busy` and `blocked` keep retry eligibility. Interruption fallback must record `ON_NATEDEV` before local compilation so a hold reaches it.

**Gotchas:** A module that hits the 1800 s test limit in the verify suite has so far been a route defect (a retry loop with no exit), not machine load. Read its log before rerunning it.

### Phase 6 — Account for AWS cost before admission  · status: done

#### As-built

A durable cost ledger estimates AWS spend, and one spending guard refuses AWS admission at either configured limit. AWS stays disabled; nothing creates a resource or enables a timer.

- `scripts/aws_target/config.py` reads the spending settings through `conf.py` into `SpendingSettings`, with `LimitSettingEvidence` for each limit. An unusable setting raises `SpendingConfigurationUnavailable`.
- `conf.value_origin` returns `UsableLiveValue`, `RecoveredValue` or `UnavailableValue`. Only a usable live limit permits spend; a recovered committed value never does, even above the intended limit. `conf.py` stays the sole reader.
- `scripts/aws_target/ledger.py` holds `CostLedger`: a locked, atomic JSON ledger under the AWS target directory. It records instance-state observations, price samples, charged intervals, reconciliations and limit events with integer time boundaries and `Decimal` money. Each running interval is charged once across repeated observations and restarts, and intervals split at day and month boundaries in the configured accounting zone. A corrupted ledger keeps its evidence and refuses new spend.
- The estimate uses Spot price history, `fallback_usd_per_hour` when no sample exists, and configured fixed costs (retained and root storage, IPv4, gp3 performance, Cost Explorer reads). Stopping the instance does not end retained-volume costs.
- `spending_guard` returns `SpendingAllowed`, `SpendingRefused` or `SpendingUnavailable` with `GuardEvidence` (decision time, evidence, limit origins) and records each decision through `ledger._decision_record`. A start or admission is refused when the estimate plus `limit_reserve_usd` reaches `daily_limit_usd` or `monthly_limit_usd`.
- `LimitEvent` carries `DeliveryState` (`PENDING`, `DELIVERED`). The guard saves under the lock, sends pending showrunner notices after it releases the lock, then marks them delivered under the lock. A restart or failed write does not reset a limit event.
- `build_targets.ssh_target_config` serves `ssh_target_eligibility` and `command_reserve`. Disabled AWS (`offload` not `on`) excludes itself as `off` before the guard or full validation. A guard refusal excludes AWS as `spending_limit`, and unusable AWS settings as `spending_unavailable`; selection continues with the next target and records no capacity retry. The reserve statuses 0/11/1 are unchanged.
- `cleanup_refused_claims` still cleans retained AWS claims on a refusal or unusable settings, through `take_over_kept_claim` and `check_leftovers`. The existing-reservation branch of `command_reserve` reuses a reservation only for an enabled, guard-permitted target.

**Files:**
- `scripts/aws_target/config.py` — spending settings reader.
- `scripts/aws_target/ledger.py` — cost ledger, estimate and spending guard.
- `scripts/aws_target/ledger_test_support.py`, `scripts/aws_target/test_ledger.py` — ledger and guard tests with an injected clock and AWS replies.
- `scripts/lib/conf.py`, `scripts/lib/test_conf.py` — value-origin states.
- `scripts/build_targets/build_targets.py`, `scripts/build_targets/test_ssh_eligibility.py` — guard in target selection, refusal cleanup and its tests.
- `config/aws_target.conf`, `config/README.md` — limits 400/20, `limit_action=drain`, `limit_reserve_usd=3`, pricing, accounting zone and notice settings.

**Binds later work:** Guard decisions are read from the ledger record shape `ledger._decision_record`, never recomputed. Deployment preflight, manual start and the scaler call the same `spending_guard` with their own purposes; each call writes a decision record. Cost totals come from the ledger (`CostEstimate`, `DailyTotal`, `MonthlyTotal`), and actual charges reconcile through `CostReconciliation`. `value_origin` stays internal to the spending reader.

**Gotchas:** Full `offload.read_config` validates the AWS spending keys, so AWS must be excluded before it runs; otherwise a malformed limit stops selection for every target. A pending notice that keeps failing delays each guard call by up to `notification_timeout_s` until retries are bounded.

**Ruled out:** A separate cleanup path for retained claims: refusal cleanup reuses `take_over_kept_claim` and `check_leftovers`.

### Phase 7 — Tighten the spending guard and SSH cleanup boundaries  · status: done

#### As-built

- `SshConnectionSettings(host, connect_timeout_s, leftover_timeout_s)` supplies `ssh_options` and `check_leftovers`; `OffloadConfig` carries these settings.
  `read_connection_settings` reads through `conf.py`; `check_current_leftovers` reads current settings before each cleanup check.
- `cleanup_refused_claims` uses connection settings and the claim's recorded `remote_host`.
  Unusable settings produce `LeftoverCheckFailed`, print the reason, and preserve the claim. Release requires `LeftoversEnded` and an unchanged record.
- `is_aws_target(target)` serves every AWS guard boundary: platform `linux` and either name `aws` or settings file `aws_target.conf`.
- `CostLedger.observe` raises `ObservationRejected` for unknown states and preserves existing evidence. Older stored `unknown` rows do not block later valid charges.
- Each `PENDING` limit event persists `LimitEvent.retry_after` and `owner` under the ledger lock; ownership uses `DeliveryOwner` or `DeliveryUnowned`.
  `cost-notice.lock` serializes delivery; retries require an expired deadline and no delivery owner. Successful delivery sets `DeliveryState.DELIVERED`.
- `CostLedger.guard(clock=...)` anchors elapsed clock time to explicit `now`; each event's attempt time starts its delivery deadline.
  `notification_retry_s=300` bounds retries. The send key `aws-limit:<event_id>` and its repeat window suppress duplicates across failures and restarts.

**Files:**

- `scripts/mac_test/offload.py` — connection settings, readers, and cleanup checks.
- `scripts/build_targets/target_config.py`, `scripts/build_targets/build_targets.py` — AWS classification and refusal cleanup.
- `scripts/aws_target/ledger.py`, `scripts/aws_target/config.py` — observations, delivery ownership, retry deadlines, and settings.
- `config/aws_target.conf`, `config/README.md` — `notification_retry_s=300` and its documentation.

**Binds later work:** **Control starts, drain and interruption recovery** passes its injected clock to `spending_guard` and records only confirmed instance states.

**Gotchas:** The repeat window covers all dates represented by `datetime`. Only exit 0 with `SENT` or `SKIPPED` confirms message delivery.
Delivery failures, restarts, and failed ledger writes preserve the event and duplicate suppression. Cleanup reads settings at use, including inside `claimed_run`.

**Ruled out:** Changes to `send.py`; its existing repeat window suppresses duplicates.

### Phase 8 — Control starts, drain and interruption recovery  · status: done
**Depends on:** Phase 7

#### As-built

- `aws_target.py tick|start|stop` run through `Controller` under `control_lock`. `AwsCli` is the one bounded AWS CLI adapter, with an injectable `AwsExecutor`; profile `build-scaler` is never read or printed.
  `controller.json` holds the state (`off`, `refused`, `stopped`, `starting`, `running`, `draining`, `stopping`), `generation`, `permitted_generation`, the drain deadline and capacity backoff. `instance_generation(directory)` reads the generation.
- `tick` reads a fresh `admission/queue.json` snapshot through `read_queue_snapshot` and starts only for enough eligible waiting rows, guard permission and ended backoff.
  A refused restart generation stops at once; a controlled drain keeps its recorded deadline (`drain_max_minutes`).
- `ledger.py` decodes decisions into `PermissionAllowed`, `PermissionRefused` and `PermissionUnavailable` (`decode_permission`, `permission_record`).
  `remote.py` publishes a versioned `AdmissionEnvelope` (target, purpose, generation, decision time, expiry) only after a confirmed guard write; `execution_permission` refuses missing, stale, malformed or wrong-generation evidence.
- `linux_runner.py` checks AWS permission before shell evaluation in `probe_shell` and `run`, and again after `prepare_shell`. `AwsExecutionContext` versus `IndependentLinuxExecution` keeps other Linux targets free of AWS permission.
  `boot_identity`, `SlotBoot` and `slot_boot` bind retained markers to boot identity, so a reused PID never selects new work.
- `remote.py` `RemoteAgent` stops the box after `idle_stop_minutes` idle, polls IMDSv2 every `interruption_poll_s`, and writes a generation-bound interruption record. A process-evidence failure counts as idle.
  The runner prints `aws_interrupted=<generation>`; offload parses it as `InterruptedTargetRun`, keeps the queue timestamp and records `ON_NATEDEV` before local work.
- `mac_test.RELEASE_STATES = ('stopping','stopped','terminated')`. `release_stopped_claims` releases only claims bound by `instance_generation` to the stopped host and generation, under the target lock, and writes `release-evidence/`.

**Files:**

- `scripts/aws_target/aws_target.py`, `scripts/aws_target/remote.py` — controller and box lifecycle.
- `scripts/aws_target/config.py`, `scripts/aws_target/ledger.py` — `ControlSettings` and named permission decisions.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py` — claim release, interruption results and permission checks.
- `config/aws_target.conf`, `config/README.md` — tick, timeout, snapshot, permission, interruption and remote-state settings.

**Binds later work:** Reports read controller state from `controller.json` and the generation through `aws_target.instance_generation`. `ControlSettings` has no `host` or `connect_timeout_s`; SSH uses `offload.read_connection_settings` and `offload.ssh_options`.

**Gotchas:** `permitted_generation`, not `generation`, is restart protection. `offload.py` puts its own directory on `sys.path` for callers outside `scripts/mac_test`. The tick timer stays disabled until go-live.

### Phase 9 — Report AWS use, cost and estimated wait saved  · status: done
**Depends on:** Phase 8

#### As-built

- `targets.read_aws_status` builds an `AwsStatusRow` from `controller.json`, slot records, attempt results and `measurement.read_status_measurement`; `build_targets.format_aws_status` renders state, spending decision, month dollars against the cap, occupied and retained slots per repository, cleanup state and recovery action. Status makes no AWS call, probe or write.
- `measurement.py` reads ledger snapshots, uptime and daily usage without calling `guard` or `estimate`; missing evidence decodes to `MeasurementUnavailable`. `record_go_live` keeps the first permitted running observation in `go-live.json`.
- `CostLedger.refresh_charges` reads Cost Explorer once per day for whole UTC days with date-only bounds, records the query cost and the reconciliation difference, and never adds the actual charge as a second charge. Each accounting day prices only the overlap of each UTC reconciliation (`period_cost`).
- `aws_report.estimate_wait_saved` selects the same day's natedev steps of the same kind and repository with wait greater than the departure time, takes `median(wait - t)`, and adds the median natedev run time minus the AWS run time; negative differences stay. Missing comparison data is unavailable.
- `report.py` renders `### AWS` with `AWS: up <h>, <n> steps, about <h> of queue wait saved for $<d> (...)`, and one `peer_sync_lines` line for each peer (last success, latest failure, never-synchronized, pause, `buildlog sync` / `buildlog sync resume`). The summary reads in one grouped query.
- `config.offload_enabled(path)` is the one owner of the offload switch. `record.is_local_step` is the one local-step rule; `mac_outcome.natedev_step_predicate` is the target rule and `natedev_host_predicate` the host rule. `verify-linux` success stays out of natedev pass evidence.

**Files:**

- `scripts/aws_target/measurement.py` — local measurement reader, go-live record and daily usage.
- `scripts/aws_target/ledger.py`, `scripts/aws_target/aws_target.py`, `scripts/aws_target/config.py` — charge reads, reconciliation, permission status and the offload owner.
- `scripts/buildlog/aws_report.py`, `scripts/buildlog/report.py` — wait-saved estimate, AWS section and peer sync lines.
- `scripts/buildlog/record.py`, `scripts/buildlog/mac_outcome.py`, `scripts/buildlog/mac_report.py` — local-step and natedev rules.
- `scripts/build_targets/targets.py`, `scripts/build_targets/build_targets.py` — AWS status row.
- `config/aws_target.conf`, `config/README.md` — reporting and charge-read settings.

**Binds later work:** AWS rows in build status and the `### AWS` report section appear only when an AWS `SshTarget` is configured. The controller tick runs `refresh_charges`; no separate charge timer exists. New offload reads use `config.offload_enabled`.

**Gotchas:** Cost Explorer `DateInterval` takes `YYYY-MM-DD`, and DAILY granularity reports UTC days. A NULL remote duration in an attempt row decodes to unavailable. The cost caps are strict: no admission past either cap.

**Ruled out:** Querying charges by local accounting-day bounds — Cost Explorer reports UTC days.

### Phase 10 — Print the interruption marker once  · status: done
**Depends on:** Phase 9

#### As-built

- `linux_runner.py` prints `aws_interrupted=<generation>` once, in the `finally` of `interruption_signals`, after `mac_exit` and `mac_cleanup`. The SIGTERM handler only records the generation through `interruption_generation`, which returns `''` when no interruption is recorded.
- The remote exit status of an interrupted slot is the child status, not 75. `interruptible_slot` is the one context for the run and probe paths, so the probe keeps its child status and generation evidence.
- The sender still reports `InterruptedTargetRun` with the same generation and attempt outcome.
- `runner_test_support.interrupt_runner` writes the interruption record and raises SIGTERM for the runner and sender tests; `seed_runner_permission` returns its permission folder.

**Files:**

- `scripts/mac_test/linux_runner.py` — single marker owner and `interruptible_slot`.
- `scripts/aws_target/runner_test_support.py` — shared SIGTERM test helper.
- `scripts/mac_test/test_linux_runner.py`, `scripts/direct/test_verify_ssh_targets.py` — one ordered marker check, child status and the sender result.

**Binds later work:** An interrupted slot prints one `aws_interrupted=` line after `mac_exit` and `mac_cleanup`, and exits with the child status.

### Phase 11 — Prepare guarded deployment and the NixOS branch  · status: done

#### As-built

- `deploy.py prepare` generates the CloudFormation parameters, priced inventory and preflight evidence locally, with no AWS command. `apply` and `setup_start` run only from the user's terminal. They create the monthly Budget (emails at 50, 80 and 100 percent, stop action at 100 percent) and its enforcement before any chargeable resource, and only inside `monthly_limit_usd=400` and `daily_limit_usd=20`.
- `docs/plans/builds-aws/cloudformation.json` defines one persistent Spot instance on the official x86_64 NixOS AMI, with the root volume for NixOS and the store and a retained encrypted data volume at `/home`. Retention applies on stack deletion and replacement, and replacement never formats a retained volume. Every charged resource carries the configured `cost_explorer_tag_key`/`cost_explorer_tag_value` tags. Shutdown behavior is `stop`, IMDSv2 is required, inbound is UDP 41641 only, and the instance profile has `AmazonSSMManagedInstanceCore` with HTTPS egress. The scaler IAM policy allows start/stop on the one instance ARN, the required describe calls and `ce:GetCostAndUsage`; no scaler key is on the box.
- The ledger records a `ComputeRequest(at, stack, region, spot_max_usd_per_hour)` before the compute deploy call. Until confirmed instance state or confirmed absence ends it, the request is charged as additional unconfirmed instance time at `max(fallback_usd_per_hour, submitted SpotMaxPrice)`, split at accounting-day boundaries. `prepare`, `setup_start` and `apply` reconcile open requests. Existing instance charges and their confirmed uptime stay unchanged.
- `aws_target.py serve` (controller) and `remote.py serve` (box) run the service loops. Each reads its first interval before any action and exits non-zero when that read fails, so systemd restarts it. The controller tick runs with `controller_tick_timeout_s` and the private credential-file environment, reads `tick_interval_s` through `conf.read`, and uses `CostLedger.refresh_charges` for charge queries.
- `docs/plans/builds-aws/nixos.patch` adds host `aws-build` (x86_64, data-volume mount, Tailscale SSH, SSM, remote lifecycle services, Python 3.13, Nix, Git, rsync, Rust, nextest, mend, configured `devshell` shells) and the natedev `aws-target` user unit, stopped by default (`wantedBy` empty). It is on `/etc/nixos` main as commit 78746da; no machine is rebuilt. First boot generates remote lifecycle settings, and execution stays refused until natedev publishes permission for that boot.
- `docs/plans/builds-aws-runbook.md` holds the complete user go-live sequence (account, placement and price reads, config inputs, priced preflight, guarded deploy, Tailscale login through SSM, `aws configure --profile build-scaler` with `AWS_SHARED_CREDENTIALS_FILE=~/.config/aws-build/credentials` at mode 0600, rebuilds, parity, exchange, recorder, routing, warm reuse, recovery and rollback checks, and the `go-live.json` first running observation). It also holds volume reuse, recovery, manual stop and rollback. Rollback stops the instance and confirms the stopped state before it disables offload, the timer or the registry entry.

**Files:**
- `scripts/aws_target/deploy.py` — local preparation and guarded deployment.
- `scripts/aws_target/ledger.py` — compute request accounting and reconciliation.
- `scripts/aws_target/config.py` — deployment settings, `deployment_price`, `money` and the service loop.
- `scripts/aws_target/test_deploy.py` — fake AWS executable tests of the deploy order, template contract and service loop.
- `docs/plans/builds-aws/cloudformation.json` — infrastructure template.
- `docs/plans/builds-aws/nixos.patch` — the NixOS `aws-build` host and natedev unit.
- `docs/plans/builds-aws-runbook.md` — user go-live, recovery and rollback procedure.

**Binds later work:** offload stays off and AWS stays out of `targets` until the user's go-live gate; no AWS resource exists before it. A NixOS change goes as a new commit on clone branch `aws-build` and then to the natedev showrunner. Compute request records stay internal: the guard refuses spend from them with the existing limit messages. The service first-interval failure is visible only through systemd status and the journal.

**Gotchas:** `deployment.json` is saved before the instance observation, so a failed lookup keeps the identity. The compressed bootstrap must stay within the 4096-byte CloudFormation parameter limit. A user stop disables the persistent Spot request until the next start; AWS restarts only a box it interrupted, and a tick that finds such a box while the guard refuses spend stops it. `limit_reserve_usd` covers one bounded drain and one idle interval; fixed costs are charged daily from deploy, and the Budget action is the late backstop, not the ceiling. A fake AWS test does not prove real bootstrap or persistent-request behavior; the runbook's user checks prove them.

**Ruled out:** replacing known instance charges with the fallback rate during an unconfirmed request — it under-counts; charging an unconfirmed request at a historical Spot price — it is not an upper bound.

### Phase 12 — Exact spending caps, the midnight total and the NixOS regression tests  · status: done

#### As-built

- `conf` range checks compare exact `Decimal` values, so a cap a fraction above `monthly_limit_usd=400` or `daily_limit_usd=20` is refused. Public `conf.decimal` is the one finite-number parser: it accepts a value only when the `Decimal` and its `float()` conversion are both finite, and `conf.check` refuses a value such as `1e400`. `_number`, `aws_target/config.money` and `measurement.number` use it. `decimal` loads only inside `conf.decimal`, so plain `conf` importers do not load it.
- Each Cost Explorer reconciliation stores `established_days`: the reconciled amount it established for each accounting day. A day is charged the higher of its computed total and its established total, so no day's daily or monthly total falls when a bill crosses local midnight or a later price sample lowers an estimate. A bill that matches the estimates adds no charge.
- `_charges` is pure and returns the updated reconciliations with the `CostEstimate`. Only `guard` and `estimate` save them, through `CostLedger._save_estimate`, which keeps distinct replies for the same period. Status and report read the saved amounts and write nothing.
- Regression tests cover the bootstrap retry that keeps an existing watchdog deadline, the controller restart after a failed first interval read, both volume modes (wrong serials refused, existing filesystems kept, matching blank media formatted once in bootstrap and refused in full-host mode with `existingVolume=""`), and pending and resolved request costs in status and report output. No NixOS file changed.

**Files:**
- `scripts/lib/conf.py` — `decimal` parser and exact range checks.
- `scripts/aws_target/ledger.py` — per-day established reconciliation amounts and `_save_estimate`.
- `scripts/aws_target/config.py`, `scripts/aws_target/measurement.py` — numbers through `conf.decimal`.
- `scripts/aws_target/measurement_test_support.py` — `pending_request` and `resolve_request` fixture helpers.
- `scripts/aws_target/test_deploy.py` — NixOS script regression tests (one shell dispatcher for the volume fakes) and the midnight reconciliation test.
- `scripts/build_targets/test_aws_status.py`, `scripts/buildlog/test_aws_report.py`, `scripts/lib/test_conf.py` — status, report and conf tests.

**Binds later work:** offload stays off and AWS stays out of `targets` until the user's go-live gate; no AWS resource exists before it.

**Gotchas:** an established day total can exceed a later bill or estimate; that is the cost of a total that never falls. Read-only paths must not save established amounts.

**Ruled out:** a stateless per-day floor (bill share minus day estimate) — it over-counts the common uneven case, a $10 bill becoming $17.92 across two days; moving the midnight test to `test_ledger.py` — it stays with the deploy tests.

## Compilation record

The compiler changed only this plan in the repository. It left `docs/plans/builds-aws-design.md` unchanged.
The compiler ran no tests, builds, lint, structural validator or gates, per this dispatch.
The verification commands above belong to future implementation phases.
