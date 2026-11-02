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
    These paths are relative to the NixOS repository. Phase 8 prepares a separate branch for the natedev showrunner.
- **Test lanes:** Python uses adjacent `test_*.py` files, rather than Rust `tests/` directories.
  New tests use these same lanes in `scripts/mac_test/`, `scripts/build_targets/`, `scripts/build_hold/`, `scripts/direct/`, `scripts/buildlog/` and `scripts/aws_target/`.
- **Build:** none for Python and shell. Phase 8 names the NixOS branch checks separately.
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
  Buildlog synchronization excludes admission state. Phase 6 must provide an explicit natedev-initiated guard channel.
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
  9. Create or start no AWS resource before the final go-live gate in Phase 8, which completes design Phase 3.
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

Design Phase 1 maps to Phases 1–2. Design Phase 2 maps to Phases 3–4. Design Phase 3 maps to Phases 5–8.
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
**Depends on:** Phase 3

#### Work Order

**Goal:** Waiting test, check and doc steps use free Linux SSH capacity while preserving their local queue place.

**Spec:**

1. Generalize SSH eligibility in `command_kind_eligible` and target iteration.
   Use configured order `mac,natedev,aws` after go-live; use fake target configuration in tests before then.
   Let eligible Linux SSH targets take test, check and doc without a natedev hold.
   Continue past a held natedev to AWS. Preserve Mac platform restrictions and local-only requests.
   Represent eligible targets and the selected target explicitly in live-step records.
   Preserve decoding of existing records.
   Keep AWS waits and execution outside Mac idle alerts and Mac copy execution.
   Test Linux-only eligibility with no eligible Mac. Test the record through its consumers.
   Check config, backoff, slot capacity and SSH reachability before reservation.
   Use a cheap eligibility check; start no shell evaluation during queue retries.
   Run the shell-aware execution probe only after reservation and the required copies.
   If this phase changes `StepRecord`, replace `StepRecord.package: str | None` with named package-selection states.
2. Extend the existing leave hook to every eligible SSH target in configured order.
   Reserve its target and slot before returning status 75 from the local wait.
   Transfer the reservation to the runner without an unclaimed interval.
   If another caller takes capacity, remain queued instead of starting an unreserved remote command.
   Treat `linux_startup=unresolved` and `LeftoversRemain` as `BusyMacDecline(LostRunLeftovers())`.
   Keep the claim and original queue age while leftovers remain or cleanup stays unconfirmed.
   Apply no backoff for this busy state; continue to the next eligible target.
3. Apply the same route to the existing test, check and staged doc paths.
   Keep build, example, final and AWS lint local by the settled scope.
   Do not move the Mac lint policy into the AWS path.
4. Preserve the first `BUILDLOG_MEM_WAIT_SINCE` across remote loss and local retry.
   Release a local memory reservation and cargo token before remote work.
   Reacquire local admission before fallback when natedev is eligible and unheld.
   Never treat a confirmed remote test failure as a reason to rerun locally.
5. Keep one selected-target result contract for the new route.
   Record `target=aws` for AWS attempts and `host=aws-build` for remote steps.
   Record each SSH attempt's target, repository identity, original queue timestamp, departure timestamp, slot, and outcome.
   Preserve earlier AWS attempts when fallback changes the call's final target to natedev.
   Record confirmed failure separately from loss and admission refusal.
   Introduce target-neutral Linux outcomes, such as `PassedOnTarget` and `FailedOnTarget`.
   Keep Mac serialization unchanged at its boundary.
   Extend `write_result` and durable attempts with completion evidence and cleanup state: confirmed or unconfirmed.
   Persist the unresolved count and evidence time separately from completion.
   Represent an unavailable count explicitly; keep numeric selector status internal.
   Test AWS loss followed by local success without loss of the AWS attempt or original queue timestamp.
   Preserve legacy Mac wire results for existing consumers.
6. Test route order, direct use during a natedev hold, slot races, local-only refusal and fallback queue age.
   Test check and staged doc, plus excluded kinds. Fake memory, clocks, SSH and all build commands.
   Extend `test_verify_ssh_targets.py` through the real probe and stream parsers.
   Cover probe refusal, run refusal, and successful probe followed by unresolved cleanup.
   Test cold-shell routing: queue retries start no shell before reservation.

**Held changes:** Verify changes `mac_retry_hook`, `run_local_test`, `try_mac_test` and adds `release_wait_claim`.
Read `offload.py` on `origin/natedev-verify` before changing its result types or serialization; preserve the held claim-transfer boundaries.
It preserves queue age through `BUILDLOG_MEM_WAIT_FIRST_FILE` and renews the token through `admit_with_token` and `with_token_renewal`.
Extend routing at these boundaries; do not replace token ownership, metadata loading, clock offsets or lint cache logic.
Resources changes `RankedWaiter`, `waiter_state`, `first_queued_waiter`, `admission_pass` and `queue_read_from` in `memory_admit.py`.
Keep AWS selection outside admission policy. Never rewrite queue ranking or count token-wait rows as available build work.
Resources changes `record.admission_metadata`, `record.step` and `record.queue_priority(start=...)`.
Add attempt evidence at the record boundary. Preserve lazy imports and append deduplication.

**Files:**

- `scripts/direct/verify.sh` — target iteration, leave hooks and remote fallback.
- `scripts/direct/step_record.py` — target-aware eligibility and execution state; named package-selection states if `StepRecord` changes.
- `scripts/buildlog/mac_idle.py`, `scripts/mac_test/copy_watch.py` — preserve Mac-only observation and copy behavior.
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py` — durable SSH attempt records and indexed measurement evidence.
- `scripts/build_targets/build_targets.py`, `scripts/build_targets/targets.py` — SSH kind eligibility.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` — reserve-before-leave and runner transfer boundaries.
- `scripts/mac_test/linux_runner.py` — completion and cleanup evidence; preserve shell evaluation after reservation.
- `scripts/lint/memory_gate.sh` — target-neutral leave integration only where required.
- `scripts/lint/memory_admit.py` — read queue-age and admission contracts; preserve policy functions.
- `scripts/direct/verify_mac_offload_test_support.py`, `scripts/lint/memory_test_support.py` — read fixture patterns.
- `config/build_targets.conf`, `config/aws_target.conf` — route and retry tunables; keep AWS disabled.
- `scripts/direct/test_verify_ssh_targets.py`, `scripts/build_targets/test_ssh_eligibility.py` — fake route and attempt regressions.
- `scripts/direct/test_step_record.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/mac_test/test_copy_watch.py` — live-step consumer regressions.
- `scripts/buildlog/test_index.py` — attempt evidence decoding and index regressions.

**Workers:** 1 writer + 1 tester; the writer owns the shared route and claim protocol.

- `impl` — source and config files; hub: `scripts/direct/verify.sh`, `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`.
- `test` — the named test files; cover routes, attempt evidence and live-step consumers without real tools or live memory.

**State:** A leave attempt holds a target slot until transfer, refusal or cleanup. Retain the first local queue timestamp across retries.
The exit path must release an untransferred local claim. Uncertain remote cleanup keeps its claim.
Live-step records preserve eligibility and selected-target state across decoding.
Durable attempt records retain earlier remote outcomes when the final call target changes.
Attempt results retain completion, cleanup state, unresolved count and evidence time.
Confirmed completion does not release capacity when cleanup remains unconfirmed.

**Constraints from prior phases:** Phase 3 provides Linux execution and parity checks. Phase 2 provides slots. Phase 1 isolates holds and backoff.
The Linux execution probe evaluates the shell; copy tracked scripts, public configuration and source before that probe.
Use a cheap routing check before reservation; preserve `probe_timeout_s` and `shell_timeout_s` as separate limits.
Startup refusal and unresolved leftovers are busy states without backoff; missing completion remains loss.
Read configuration through `conf.py`; tests use `CONF_DIR`, private `build_targets.conf` and numeric limit comments.

**Verification — impl:** `bash -n scripts/direct/verify.sh scripts/lint/memory_gate.sh`

**Verification — impl types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/build_targets/build_targets.py scripts/build_targets/targets.py scripts/mac_test/mac_test.py scripts/mac_test/offload.py scripts/mac_test/linux_runner.py scripts/direct/step_record.py scripts/buildlog/mac_idle.py scripts/mac_test/copy_watch.py scripts/buildlog/record.py scripts/buildlog/index.py`

**Verification — test:** `python3 -m unittest discover -s scripts/direct -p 'test_verify_ssh_targets.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/build_targets -p 'test_ssh_eligibility.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/direct -p 'test_step_record.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_mac_idle.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/mac_test -p 'test_copy_watch.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_index.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/direct/test_verify_ssh_targets.py scripts/build_targets/test_ssh_eligibility.py scripts/direct/test_step_record.py scripts/buildlog/test_mac_idle.py scripts/mac_test/test_copy_watch.py scripts/buildlog/test_index.py`

**Acceptance gate:** The commands pass. A fake Linux slot takes a queued check without a hold.
AWS loss followed by local success preserves the AWS attempt, its slot and the original queue timestamp.
Linux-only eligibility remains outside Mac idle alerts and Mac copy execution through the live-step consumers.
Cold-shell queue retries start no shell before reservation.
All three startup-refusal cases preserve ownership and queue age, apply no backoff, and continue to another eligible target.
Attempt results distinguish confirmed completion from unconfirmed cleanup and retain the unresolved count and evidence time.
Target-neutral Linux outcomes preserve the existing Mac wire results.

**Also touches:** `scripts/direct/`, `scripts/mac_test/`, offload (owner verify-unit); `scripts/build_targets/`, `config/build_targets.conf` (owner resources-unit); `scripts/buildlog/` (owner report-unit); `scripts/lint/memory_gate.sh`, `scripts/lint/memory_admit.py` if changed (owner queue-unit).

### Phase 5 — Account for AWS cost before admission  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** A durable cost estimate refuses AWS admission at either configured spending limit.

**Spec:**

1. Add `scripts/aws_target/config.py` and `ledger.py` before wiring control commands.
   Validate required values at each use. Missing, nonfinite, negative or unreadable spending data refuses starts and new admission.
   Keep disabled AWS configuration inert and free of AWS calls during ordinary builds.
   Read all configuration through `conf.py`.
   Permit spend only from a usable live value; refuse spend when a live spending limit is missing or malformed.
   A recovered committed value never permits spend, even when it exceeds the current intended limit.
   Report the invalid live setting and the refusal reason.
   If needed, add a narrow `conf.py` interface that distinguishes usable live values from recovered committed values.
   Name these states for their spending guarantee; keep `conf.py` as the sole reader.
2. Define typed records for observed instance states, price samples, charged intervals, daily totals, monthly totals and limit events.
   Use integer time boundaries and `Decimal` money arithmetic. Record observation sources and timestamps.
   Account each running interval once across repeated observations and process restarts.
   Split intervals across day and month boundaries using a configured accounting zone.
3. Estimate instance time from recorded state evidence and price history.
   Read Spot price history hourly; use configured `fallback_usd_per_hour` when no price sample exists.
   Add configured fixed costs, including retained storage, root storage, IPv4 and provisioned gp3 performance.
   Include the cost of scheduled Cost Explorer reads.
   Do not treat stopping the instance as ending retained-volume costs.
4. Read `monthly_limit_usd=400`, `daily_limit_usd=20` and `limit_action=drain` from AWS config.
   Refuse starts when either estimate reaches its limit. Publish a drain decision that rejects new remote work.
   Expose guard decisions as named allowed, refused, and unavailable states.
   Include decision time and the evidence needed to validate the decision.
   Notify the showrunner once per limit event through the existing message interface; tests intercept delivery.
   Do not reset limit events through a process restart or a failed ledger write.
5. Expose the same guard for deployment preflight, manual start and the future scaler.
   Require a priced resource inventory before the first resource is created.
   Reserve known fixed costs and configured operating headroom before admitting a start.
   The headroom is `limit_reserve_usd` in config, initially 3: one `drain_max_minutes` drain plus one `idle_stop_minutes` interval at `fallback_usd_per_hour`.
   A start or new admission is refused when the estimate plus `limit_reserve_usd` reaches either limit.
   Do not create a parallel deployment limit or increase a limit to make preflight pass.
6. Test duplicate observations, missing transitions, first boot, stopped-volume costs, stale prices, calendar boundaries and exact limit equality.
   Inject the clock and AWS replies. A corrupted ledger must preserve evidence and refuse new spend.
   Test missing and malformed live limits against committed history, including a recovered limit above the current intended limit.
   Require refusal at every spending entry point when only a recovered value exists.

**Held changes:** Resources changes `read_queue_snapshot`'s decoded input types through `RankedWaiter` and typed token-wait state.
Keep cost accounting independent of those types; Phase 6 reads the snapshot through its existing decoder.
Neither held branch has source changes under the new `scripts/aws_target/` paths.

**Files:**

- `scripts/aws_target/config.py`, `scripts/aws_target/ledger.py` — new settings validation, typed ledger and pure guard decisions.
- `scripts/aws_target/test_ledger.py` — isolated cost and recovery tests.
- `scripts/lib/atomic_file.py`, `scripts/lint/memory_admit.py` — read atomic persistence and snapshot contracts.
- `scripts/lib/conf.py` — the sole reader; add value-origin states only if no existing interface provides them.
- `scripts/lib/test_conf.py` — live-value and recovered-value regressions if the reader interface changes.
- `config/aws_target.conf`, `config/README.md` — complete controller, pricing and spending settings.

**Workers:** 1 writer + 1 tester; accounting remains separate from AWS command execution.

- `impl` — config and ledger implementation; hub: `scripts/aws_target/config.py`, `config/aws_target.conf`.
- `test` — `scripts/aws_target/test_ledger.py` and reader regressions in `scripts/lib/test_conf.py` if needed.
  Use fixed state evidence, committed configuration history, prices, time and temporary files.

**State:** A locked ledger records charged intervals, price observations, reconciliations and durable limit events under the AWS target directory.
Commit a decision atomically before any caller can act on it. Preserve the last valid evidence during interrupted writes.
Each named guard state carries its decision time and validation evidence for Phase 6's explicit publication channel.
Guard evidence records whether each spending limit came from a usable live setting.

**Constraints from prior phases:** Phase 1 defines AWS state isolation. Phase 4 establishes remote admission and fallback boundaries.
AWS remains disabled; this phase creates no resource and enables no timer.
Phase 3 uses `conf.py`, which can recover committed values after a missing or malformed live setting.
That recovery does not permit spend; preserve ordinary readers while adding the narrow spending guarantee.
Tests use `CONF_DIR`, private `build_targets.conf` and numeric limit comments.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/config.py scripts/aws_target/ledger.py scripts/lib/conf.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_ledger.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/lib -p 'test_conf.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_ledger.py scripts/lib/test_conf.py`

**Acceptance gate:** The commands pass. At either configured limit, every guard entry refuses new spend without changing the configured limit.
Named allowed, refused and unavailable decisions expose their decision times and validation evidence.
Missing or malformed live limits refuse spend despite usable committed history, including a recovered value above the intended limit.
The guard reports why it refused spend; `conf.py` remains the sole configuration reader.

**Also touches:** `scripts/lib/conf.py` and `scripts/lib/test_conf.py` if changed; `scripts/lint/memory_admit.py` remains an input owned by queue-unit.

### Phase 6 — Control starts, drain and interruption recovery  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** The controller runs AWS only for eligible queued work within the spending guard.

**Spec:**

1. Add `aws_target.py tick`, `start` and `stop`, with one bounded AWS CLI adapter and an injectable command executor.
   Read region, instance ID and profile from config. Use profile `build-scaler` without reading or printing its credential file.
   Keep every AWS call absent from imports, tests and disabled target paths.
2. Have `tick` read `admission/queue.json` through `read_queue_snapshot`; never call the live queue reader.
   Require a fresh, complete snapshot. Select queued test, check and doc rows with known repository identity.
   Preserve typed queue order and exclude `token_left_at` rows.
   Start only when configured `start_min_waiting` rows exceed `start_after_wait_s`, the guard permits spend, and capacity backoff has ended.
   Record a capacity miss and retry after `capacity_backoff_s`; leave local work queued.
3. Record `stopped`, `starting`, `running`, `draining` and refused-start reasons with evidence timestamps.
   Reconcile instance state before each action. Serialize tick, manual start and manual stop.
   Treat API uncertainty as refusal for starts. Keep ledger evidence across command failures.
4. Publish the guard's admission state to the Linux probe and runner.
   Transfer guard decisions through an explicit natedev-initiated channel.
   Do not use buildlog synchronization to distribute admission state.
   Refuse execution when guard evidence is missing, stale, malformed, or belongs to another instance generation.
   For AWS targets, validate spending permission before shell evaluation or `shellHook` in both `linux_runner.probe_shell` and `linux_runner.run`.
   Keep other Linux targets and the loopback proof usable without AWS permission.
   Test guard refusal after slot reservation but before execution.
   At a limit, reject new work, let running work end, then stop the instance.
   Bound the drain by `drain_max_minutes` in config, initially 60: at the deadline, stop the instance; its running steps are lost and re-enter natedev's queue at their place.
   Account for an AWS-initiated restart before accepting work. It must not bypass the guard:
   a tick that finds the instance running while the guard refuses spend stops it at once, and the box's own idle stop is the second bound.
5. Add `remote.py` for the box's idle and interruption checks.
   Read `idle_stop_minutes` from its synchronized config. Shut down with instance shutdown behavior `stop` after the idle interval.
   Read IMDSv2 interruption notices at the configured interval, initially 5 seconds in config.
   Mark draining before ending active step process groups.
   Have `remote.py` write a generation-bound interruption record on the target.
   Have the runner read that record when the stream ends and print `aws_interrupted=<generation>`.
   Parse this record as a distinct interruption-loss result; preserve the original queue timestamp.
   Persist the interruption evidence in the attempt record through `write_result`.
   Reject evidence from another generation; distinguish interruption loss from startup refusal and confirmed completion.
6. Release kept AWS claims only after instance evidence reports `stopping`, `stopped` or `terminated`.
   Record the state evidence with each release; do not require SSH cleanup for those states.
   Preserve claims for running, unknown or unreachable state. Keep release idempotent under tick reruns.
   Bind release evidence to the target, instance generation, and claim identities.
   Validate those identities under the target lock before release.
   Preserve claims created after the observed stop.
   Never release another target's claims or replace retained job fields during recovery.
   Test stop evidence followed by restart and a new claim before release.
   Reconcile retained `.linux-run.json` markers on the target and local claims before new work.
   Bind marker process identity to boot identity because `/home` survives restart.
   Use evidence to recover old markers without selecting or killing new work that reuses a PID.
   Keep claims and markers when evidence cannot confirm recovery.
   Test restart with a retained marker and reused PID; preserve new work and permit recovery from sufficient evidence.
7. Add tick settings with an initial 30-second interval in config.
   Keep the actual timer disabled until Phase 8. Store every retry, timeout, stale threshold and poll interval in config.
   Bound routine tick work; refresh prices only when due, rather than on each tick.
8. Test threshold selection, stale snapshots, concurrent tick/start, capacity backoff, limit drain, interruption and evidence-based release.
   Inject AWS, IMDS, process groups, clocks and notification delivery. Test no-work AWS restart behavior without real AWS.
   Extend `test_remote.py` through both runner entry points; invalid permission starts neither shell evaluation nor work.
   Test interruption through the real stream parser and attempt writer.
   Distinguish interruption loss, startup refusal and confirmed completion while preserving the original queue timestamp.

**Held changes:** Resources changes `QueueInputs`, `RankedWaiter`, `waiter_state`, `first_queued_waiter` and `stored_waiter`.
Consume decoded rows without replacing snapshot serialization or the admission walk.
Verify changes `run_is_kept`, `claim_once`, `command_claim` and wait-claim adoption in `mac_test.py`.
Add one explicit AWS state-evidence release operation; preserve the existing live-process and remote-leftover checks.
Keep interruption decoding separate from `OffloadRequest.admission` and `claim_arguments` transfer changes.

**Files:**

- `scripts/aws_target/aws_target.py`, `scripts/aws_target/remote.py` — new controller and remote lifecycle checks.
- `scripts/aws_target/config.py`, `scripts/aws_target/ledger.py` — named guard decisions, validation evidence and publication settings.
- `scripts/mac_test/mac_test.py` — state-evidence claim release and local claim reconciliation.
- `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py` — guard checks before shell evaluation and generation-bound interruption records.
  Reconcile retained markers with boot identity and preserve startup-refusal handling.
- `scripts/lib/conf.py` — read the shared configuration and live-value spending guarantee.
- `scripts/lint/memory_admit.py` — read snapshot schema and decoder; modify only a necessary narrow reader boundary.
- `config/aws_target.conf`, `config/README.md` — lifecycle settings.
- `scripts/aws_target/test_control.py`, `scripts/aws_target/test_remote.py` — fake control and remote tests.

**Workers:** 1 writer + 1 tester; the controller owns its side-effect order and claim release.

- `impl` — source and config files; hub: `scripts/aws_target/config.py`, `scripts/mac_test/mac_test.py`, `config/aws_target.conf`.
- `test` — the two new tests; model AWS, IMDS and queue snapshots with temporary typed fixtures.

**State:** Controller lock, capacity backoff, current state, drain marker, idle evidence and claim-release evidence persist.
Record intent before an AWS action; reconcile actual state after an uncertain response instead of repeating an unguarded start.
Guard evidence and claim-release evidence identify the target and instance generation.
Release evidence also identifies the affected claims. Later claims remain intact.
Retained slot markers carry boot identity; recovery reconciles remote markers and local claims before new work.
Generation-bound interruption records persist on the target and in durable attempts.

**Constraints from prior phases:** Phase 5 supplies the ledger and shared guard. Phase 4 preserves queue age on loss.
Phase 3 supplies the Linux probe. Phase 2 supplies slot claims and retained job fields.
Phase 5 supplies named allowed, refused and unavailable guard decisions with decision times and validation evidence.
Only usable live limits permit spend; committed recovery values do not permit spend.
Phase 3 evaluates the shell in both `probe_shell` and `run`; copy scripts, public configuration and source before the execution probe.
Phase 3 keeps `.linux-run.json` during uncertain cleanup; `/home` preserves these markers across restarts.
Phase 4 preserves completion, cleanup state, unresolved count and evidence time in each attempt.
Startup refusal and `LeftoversRemain` stay busy without backoff; missing completion remains loss.
Keep numeric selector status internal and preserve the existing Mac serialization.
Read configuration through `conf.py`; tests use `CONF_DIR`, private `build_targets.conf` and numeric limit comments.
Buildlog synchronization excludes admission state. Use an explicit natedev-initiated channel for guard transfer.
No real resource exists before Phase 8's user gate.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/config.py scripts/aws_target/ledger.py scripts/aws_target/aws_target.py scripts/aws_target/remote.py scripts/mac_test/mac_test.py scripts/mac_test/offload.py scripts/mac_test/linux_runner.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_control.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_remote.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_control.py scripts/aws_target/test_remote.py`

**Acceptance gate:** The commands pass. Fake interruption preserves queue age; stopped-state evidence releases only the affected AWS claims.
An over-limit AWS restart never becomes ready for new work.
AWS guard refusal after slot reservation prevents shell evaluation, `shellHook` and work through both runner entry points.
Other Linux targets and the loopback proof remain usable without AWS permission.
Missing, stale, malformed or wrong-generation evidence prevents execution.
Stop evidence followed by restart releases only the identified old claims under the target lock.
Recovery reconciles remote markers and local claims using boot identity before new work.
A retained marker with a reused PID cannot select or kill new work; sufficient evidence permits recovery.
Real parsers distinguish generation-bound interruption loss, startup refusal and confirmed completion in durable attempts without changing queue age.

**Also touches:** `scripts/mac_test/`, offload (owner verify-unit); `scripts/lint/memory_admit.py` if changed (owner queue-unit).

### Phase 7 — Report AWS use, cost and estimated wait saved  · status: todo
**Depends on:** Phase 6

#### Work Order

**Goal:** Build status and reports show AWS capacity and the cost of its estimated benefit.

**Spec:**

1. Add a local AWS status reader to `build_targets.py`; ordinary status must not call AWS.
   Show the controller's recorded state, evidence time and month's dollars against the configured cap.
   Support `stopped`, `starting since HH:MM`, per-repository slot counts, interruption drain and limit refusal.
   Show occupied slots per repository, including slots retained pending cleanup.
   Show confirmed test results separately from retained capacity, including confirmed completion with unconfirmed cleanup.
   Render completion, confirmed or unconfirmed cleanup, unresolved count and evidence time from the attempt result.
   Show Phase 6's recovery action for retained capacity; keep numeric selector status internal.
   Do not interpret `slots=2` as a global target capacity.
   Preserve read-only status without probes or writes.
   Test active work, dead owners, `cleanup_pending`, malformed claims, and recovery.
   Extend `test_aws_status.py` with confirmed results, unconfirmed cleanup, unresolved counts and retained-capacity recovery output.
   Preserve the user's time zone and existing unreadable-state behavior.
2. Keep `host=aws-build` on remote steps. Preserve each attempt's target when fallback changes the final call target to natedev.
   Consume Phase 4's SSH attempt evidence for estimation.
   Use each attempt's repository identity, original queue timestamp, departure timestamp, slot, and outcome.
   Preserve confirmed failure, loss and admission refusal as distinct outcomes.
   Add only absent AWS measurement fields at the record and index boundaries.
   Preserve old records with absent AWS fields.
3. Read actual charges through Cost Explorer once per configured accounting day.
   Record the query cost and reconciliation difference. Do not add the actual charge to the estimate as a second charge.
   Keep reports local; the controller performs scheduled reads.
4. For departure after `t` seconds, select that day's natedev steps with the same kind and repository and wait greater than `t`.
   Estimate saved wait as `median(wait - t)` for those rows.
   Add the same command's median natedev run time minus the AWS run time. Preserve negative differences.
   Mark missing comparison data as unavailable; never manufacture a saving.
5. Add an `### AWS` section with this lead-line shape:
   `AWS: up <h>, <n> steps, about <h> of queue wait saved for $<d> ($<d> per saved hour, estimated).`
   Show missing or zero saved-hour ratios without division errors.
   Compare daily summed natedev queue wait before and after go-live; label the comparison as unmatched for load.
6. Keep Mac reports restricted to Mac work and existing natedev predicates restricted to natedev work.
   Reuse Phase 3's Linux sent-tree keys and target-specific evidence classification.
   Keep `verify-linux` success out of natedev pass evidence.
   Test mixed-host data, negative estimates, missing comparisons, daily reconciliation and all status states.
7. Show each active peer's last successful exchange and latest failure.
   Show never-synchronized and globally paused states.
   Label AWS report data with its synchronization time.
   Provide the relevant `buildlog sync` or `buildlog sync resume` action.
   Preserve the legacy Mac status reader.
   Test isolated AWS failure, isolated Mac failure, first failure, pause, and recovery through rendered report output.

**Held changes:** Resources replaces eager `memory_admit` imports with `admission_metadata` in `record.py`.
It also adds the `start` boundary to `queue_priority` for append deduplication.
Keep both function contracts unchanged; add AWS metadata at the record boundary.
Neither held branch changes `report.py` or `build_targets.py` source for these AWS sections at the inspected tips.

**Files:**

- `scripts/aws_target/measurement.py` — new local measurement reader and reconciliation support.
- `scripts/aws_target/aws_target.py`, `scripts/aws_target/ledger.py` — scheduled charge reads and reconciliation records.
- `scripts/build_targets/build_targets.py`, `scripts/build_targets/targets.py` — AWS status row and repository slot state types.
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py` — absent measurement fields and AWS report output.
- `scripts/buildlog/mac_report.py` — read and preserve target-specific tree-key evidence.
- `scripts/buildlog/sync.py` — active peer status access and legacy Mac reader compatibility.
- `scripts/buildlog/store.py` — read the existing persistence contract.
- `config/aws_target.conf`, `config/README.md` — reporting and charge-read settings.
- `scripts/aws_target/test_measurement.py`, `scripts/buildlog/test_aws_report.py`, `scripts/build_targets/test_aws_status.py` — isolated regressions.
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_mac_report.py` — Linux tree-key and natedev evidence regression gate.

**Workers:** 1 writer + 1 tester; report and index changes share one metadata owner.

- `impl` — source and config files; hub: `scripts/buildlog/index.py`, `scripts/buildlog/record.py`, `config/aws_target.conf`.
- `test` — the three new tests and existing evidence regressions; use temporary records, SQLite fixtures, fixed clocks and fake charge replies.

**State:** Daily charge-read marks, actual-charge records and the go-live timestamp persist beside the ledger.
Reuse the existing schema and classification contracts. Migrate only absent measurement fields while preserving existing records.
A retry must not repeat a successful daily charge read or duplicate a reconciliation.
Read retained slot evidence and peer exchange status without probes or writes.

**Constraints from prior phases:** Phase 6 records controller state, drain reason and slot-release evidence.
Phase 5 owns cost totals. Phase 4 records each SSH attempt independently of the call's final target.
Phase 4 persists completion, cleanup state, unresolved count and evidence time through `write_result`.
Confirmed completion can coexist with retained capacity; Phase 6 owns recovery of markers and claims using boot identity.
Phase 6 records generation-bound interruption loss separately from startup refusal and confirmed completion.
Phase 3 preserves `verify-linux` sent-tree keys and excludes their success from natedev pass evidence.
Reuse those guarantees; add only missing measurement fields.
Read configuration through `conf.py`; tests use `CONF_DIR`, private `build_targets.conf` and numeric limit comments.
Phase 2 added `steps.target` in schema version 27.
Reuse `record.recorded_target`, configured host mappings, and the existing Mac classification predicates.
Add only absent AWS measurement fields.
Preserve legacy records with missing target values and explicit target precedence over host aliases.
Preserve AWS recorder health for active AWS configuration.
Phase 2 keeps per-peer exchange status and the legacy Mac status reader.
Phase 6 owns retained-claim recovery. This phase exposes retained capacity and its recovery action.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/measurement.py scripts/aws_target/aws_target.py scripts/aws_target/ledger.py scripts/build_targets/build_targets.py scripts/build_targets/targets.py scripts/buildlog/record.py scripts/buildlog/index.py scripts/buildlog/report.py scripts/buildlog/sync.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_measurement.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_aws_report.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/build_targets -p 'test_aws_status.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_record.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_mac_report.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_measurement.py scripts/buildlog/test_aws_report.py scripts/build_targets/test_aws_status.py scripts/buildlog/test_record.py scripts/buildlog/test_mac_report.py`

**Acceptance gate:** The commands pass. Fixed mixed-host data produces the specified AWS line without changing Mac totals or double-counting charges.
Status distinguishes active execution from retained slots per repository without probes or writes.
`test_aws_status.py` renders confirmed results separately from retained capacity with cleanup state, unresolved count, evidence time and recovery action.
The record and Mac report tests preserve Linux tree keys without treating Linux success as natedev pass evidence.
Rendered reports show each active peer's success, failure, never-synchronized, paused and recovered states with the relevant action.
AWS report data carries its synchronization time. Legacy Mac records and explicit target precedence remain correct.

**Also touches:** `scripts/build_targets/build_targets.py`, `scripts/build_targets/targets.py` (owner resources-unit); `scripts/buildlog/` (owner report-unit).

### Phase 8 — Prepare guarded deployment and the NixOS branch  · status: todo
**Depends on:** Phase 7

#### Work Order

**Goal:** The user receives a complete guarded go-live procedure after all code and configuration are ready.

**Spec:**

1. Create a CloudFormation JSON template for the settled persistent Spot instance and retained data volume.
   Parameterize type, region inputs, AMI, volume sizes, IOPS, throughput, timeouts, price inputs and budget settings from config.
   Use the official x86_64 NixOS AMI selected for the region.
   Keep the root volume for NixOS and the store; mount the retained encrypted data volume at `/home`.
   Apply retention on stack deletion and replacement. Never format an existing retained volume during replacement.
2. Require the spend guard before the first resource, not only before the first scaler start.
   Provide `deploy.py prepare` to generate parameters, priced inventory and preflight evidence locally.
   Have the user run guarded deployment from their terminal. Do not run deployment as an agent acceptance command.
   Create the monthly Budget and enforcement dependencies before chargeable compute or storage.
   Use the config's $400 monthly limit, with emails at configured 50, 80 and 100 percent thresholds.
   Prepare the stop action at 100 percent; keep delayed billing outside the primary admission guard.
3. Scope the scaler IAM policy to start/stop on the one instance ARN, required describe calls and `ce:GetCostAndUsage`.
   Put no scaler key on the box. Give the box the SSM instance profile and public package access.
   Open inbound UDP 41641 for direct Tailscale transport; open no inbound TCP port.
   Make instance shutdown behavior `stop`. Require IMDSv2 for interruption checks.
4. Prepare a dedicated NixOS branch with host `aws-build`, rather than importing natedev's desktop configuration.
   Add x86_64 host output, data-volume mounting, Tailscale SSH, SSM and remote lifecycle services.
   Install Python 3.13, Nix, Git, rsync, Rust, nextest and mend.
   Select shells through configured `devshell` and `devshell.<repo>` settings.
   Check `rustc -V`, `cargo nextest --version`, mend version and tracked script commit inside the configured shell.
   Supply matching tools through the NixOS configuration before enabling AWS work.
   Add a natedev user tick timer that reads its interval from config at use.
   Keep the timer and AWS admission disabled before the user completes go-live inputs.
   Hand the branch and commit to the natedev showrunner. Never push `/etc/nixos` main or rebuild either machine as an agent.
5. Record the NixOS changes in `docs/plans/builds-aws/nixos.patch` for review.
   The patch targets NixOS-relative `flake.nix`, `configuration.nix`, `hosts/aws-build.nix` and `modules/linux/aws-build.nix`.
   Use a separate NixOS checkout to prepare the branch.
   Read the existing NixOS entry points listed in Delegation Context; keep natedev's current jobs unchanged.
6. Complete `docs/plans/builds-aws-runbook.md` with initial deploy, volume reuse, recovery, manual stop and rollback steps.
   Include free region placement and current-price reads, required account inputs, notification email and selected AMI validation.
   Read credentials through `AWS_SHARED_CREDENTIALS_FILE=~/.config/aws-build/credentials` and profile `build-scaler` on natedev.
   Require mode 0600. Let only the user run `aws configure --profile build-scaler` with that credential-file setting.
   Copy only tracked scripts and non-secret settings to the box.
   Keep AWS outside `targets` until the user activation gate.
   An AWS `SshTarget` entry enables record exchange even when `offload=off`.
   Install tracked scripts and non-secret configuration before enabling the registry entry.
   Keep `BUILDLOG_PEER` unset in the normal multi-peer service.
   Include user checks for AWS exchange, recorder health, retained-capacity status, and rollback.
   Include cold and cached test, check and staged doc runs through the configured shell.
   Check warm reuse and budget trimming only in the selected slot.
   Check retained-ownership recovery, including marker reconciliation after restart, without selecting or killing new work.
   Give `tag:build-aws` no initiated connections to natedev or the Mac; permit natedev's user to reach it.
7. Build the deployment decisions below into the template, runbook and NixOS patch.
   Keep all settled choices and both caps unchanged.
   Do not present a fake AWS test as proof of a real bootstrap or persistent-request behavior; the runbook's user-run checks prove them at go-live.
8. Test parameter generation and the full deployment command sequence through a fake AWS executable.
   Assert guard-before-resource order, disabled defaults, Budget amount, IAM scope, retention and secret exclusion.
   Parse template JSON locally. Keep NixOS evaluation separate from system activation.
   Test disabled configuration with the scheduled sync path.
   Preserve target claims, affinity, warm mirrors, cost evidence, and peer status during restart and redeployment.

**Held changes:** Neither held branch modifies the new AWS deployment files or the NixOS patch paths.
Keep the target activation edit to `config/build_targets.conf` separate from earlier routing changes.
Preserve resources' Mac floor cleanup settings and verify's helper/token changes when reviewing the assembled checkout.

**Files:**

- `scripts/aws_target/deploy.py` — parameter preparation and guarded user deployment support.
- `scripts/aws_target/test_deploy.py` — fake AWS deployment sequence and resource-contract tests.
- `scripts/aws_target/config.py` — deployment config validation.
- `config/aws_target.conf`, `config/build_targets.conf`, `config/README.md` — complete values, disabled defaults and activation procedure.
- `docs/plans/builds-aws/cloudformation.json` — new infrastructure template.
- `docs/plans/builds-aws/nixos.patch` — reviewable NixOS branch changes.
- `docs/plans/builds-aws-runbook.md` — complete user procedure and branch handoff.
- `pyrightconfig.json` — add only the AWS Python execution environment if required.

**Workers:** 2 writers; deployment and NixOS preparation have independent files. The NixOS writer has no Python test lane.

- `impl` — deployment Python, its tests, CloudFormation, config and `pyrightconfig.json`; hub: `config/aws_target.conf`, `config/build_targets.conf`.
- `test` opens as impl — NixOS patch, separate NixOS branch and runbook; request config additions from `impl`.

**State:** Before go-live, only local generated parameters and a separate NixOS branch exist.
After the user gate, stack IDs, volume ID, instance ID, guard evidence and activation time persist.
Make reruns reconcile existing resources and retained volume identity. Never reset cost evidence during redeployment.
Preserve target claims, affinity, warm mirrors, cost evidence, and peer status during restart and redeployment.

**Constraints from prior phases:** Phase 7 supplies reports. Phase 6 supplies tick, remote idle stop and interruption handling.
Phase 5 supplies deployment preflight and the ledger. Phases 1–4 supply target state, slots and Linux routing.
The source design's Phase 3 go-live occurs only here, after implementation and the runbook are complete.
An active AWS `SshTarget` enables record exchange even when `offload=off`.
Keep AWS outside `targets` until tracked scripts and non-secret configuration are installed at the user gate.
Phase 7 supplies AWS exchange status, recorder health and retained-capacity status for the runbook's user checks.
Phase 3 checks parity inside the configured shell after script, public configuration and source copies.
Its runner needs Python 3.13, Nix, Git, rsync, Rust, nextest and mend.
Its LRU sweep trims selected-slot output; whole-folder removal occurs only for the disk floor.
Phase 4 separates completion from cleanup state, unresolved count and evidence time in durable attempts.
Startup refusal and remaining leftovers preserve ownership without backoff.
Phase 5 permits spend only with usable live limits through `conf.py`; committed recovery values never permit spend.
Phase 6 checks AWS permission before shell evaluation and `shellHook` in both runner entry points.
Phase 6 reconciles retained markers and claims using boot identity after restart.
Phase 6 carries generation-bound interruption evidence into the stream parser and durable attempt record.
Read configuration through `conf.py`; tests use `CONF_DIR`, private `build_targets.conf` and numeric limit comments.

**Verification — impl:** `python3 -m json.tool docs/plans/builds-aws/cloudformation.json`

**Verification — impl types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/deploy.py scripts/aws_target/config.py scripts/aws_target/test_deploy.py`

**Verification — impl tests:** `python3 -m unittest discover -s scripts/aws_target -p 'test_deploy.py'`

**Verification — NixOS writer:** In the prepared NixOS checkout, run `nix eval --raw .#nixosConfigurations.aws-build.config.system.build.toplevel.drvPath`.

**Verification — NixOS writer:** In that checkout, run `nix eval --raw .#nixosConfigurations.natedev.config.system.build.toplevel.drvPath`.

**Acceptance gate:** Local verification passes, the patch matches the prepared NixOS branch, and the runbook contains the complete guarded command sequence.
The implementation checkpoint requires no AWS resource. Record go-live separately after the user gate.
The fake scheduled sync path leaves disabled AWS configuration inert.
The runbook checks AWS exchange, recorder health, retained-capacity status and rollback after activation.
The user-run gate checks parity inside the configured shell during cold and cached test, check and staged doc runs.
The gate confirms warm reuse, selected-slot budget trimming and retained-ownership recovery across restart.
Restart and redeployment preserve target claims, affinity, warm mirrors, cost evidence and peer status.

**Go-live gate — user actions:** Finish the code, template, runbook and NixOS branch before these user steps.

1. Create the AWS account and admin login in the console.
2. Read placement scores and current prices through the runbook at the user's terminal.
3. Fill the region, AMI, prices and required account inputs in configuration.
4. Run the priced first-resource preflight from the runbook.
5. Run the guarded `aws cloudformation deploy` procedure with the generated parameters.
6. Run `sudo tailscale up --ssh --advertise-tags=tag:build-aws` through SSM Session Manager.
7. Configure the Tailscale tag policy in the console.
8. Run `aws configure --profile build-scaler` with `AWS_SHARED_CREDENTIALS_FILE` set to the private file named above.
9. Rebuild natedev and the box from the prepared NixOS branch.
10. Confirm tool parity inside the configured shell through the runbook.
11. Confirm spending-guard readiness through the runbook.
12. Confirm that tracked scripts and non-secret configuration are installed on the box.
13. Keep `BUILDLOG_PEER` unset in the normal multi-peer service.
14. Enable `targets=mac,natedev,aws` and the tick timer.
15. Confirm AWS exchange through the runbook.
16. Confirm AWS recorder health through the runbook.
17. Confirm retained-capacity status through the runbook.
18. Confirm cold and cached test, check and staged doc routing through the runbook's user-run proof.
19. Confirm warm reuse and budget trimming in the selected slot.
20. Confirm retained-ownership recovery after restart through the runbook.
21. Confirm the rollback procedure through the runbook.
22. Record the activation time for the report.

Keep AWS lint disabled. Pass no password or key through a session.

**Deployment decisions (aws-unit, 2026-11-01):**

1. **Persistent Spot restart at a spending limit.** A user stop (the scaler's `StopInstances`, or the box's own shutdown) disables a persistent request until the next start, so AWS does not restart a stopped box.
   AWS restarts only a box it interrupted. Phase 6 bounds that case: a tick that finds the box running while the guard refuses spend stops it at once, and the box's idle stop is the second bound.
   The runbook's go-live check confirms the request state after a manual stop.
2. **First SSM connection.** Use the official NixOS AMI for the selected release and region (the runbook reads its ID from the NixOS AMI list and records it in config).
   The NixOS Amazon image runs `amazon-ssm-agent`. The template gives the instance a profile with `AmazonSSMManagedInstanceCore` and open HTTPS egress.
   User data carries the non-secret NixOS config that enables Tailscale and the idle stop, so the box stops itself from first boot.
   The runbook checks `aws ssm describe-instance-information` before the Tailscale login; the EC2 serial console is the fallback.
3. **Strict cost boundary during drain.** Phase 5's `limit_reserve_usd` covers one bounded drain (`drain_max_minutes`) and one idle interval.
   Fixed costs (retained volume, IPv4) are charged to the ledger daily from deploy, so the daily limit already includes them.
   Between deploy and natedev control, the box's idle stop bounds run time. The AWS Budget action is the late backstop, not the ceiling.

**Also touches:** `config/build_targets.conf` (owner resources-unit); `pyrightconfig.json` (owner natedev showrunner); NixOS `flake.nix`, `configuration.nix`, `hosts/aws-build.nix`, `modules/linux/aws-build.nix` (owner natedev showrunner, separate branch only).

## Compilation record

The compiler changed only this plan in the repository. It left `docs/plans/builds-aws-design.md` unchanged.
The compiler ran no tests, builds, lint, structural validator or gates, per this dispatch.
The verification commands above belong to future implementation phases.
