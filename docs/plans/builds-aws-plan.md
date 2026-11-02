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
  - `scripts/build_targets/target_config.py` — `LocalTarget`, `SshTarget`, `read_targets` and `BUILD_TARGETS_CONFIG`.
  - `scripts/build_targets/targets.py` — `target_eligibility`, `target_state` and typed state values.
  - `scripts/build_targets/build_targets.py` — `command_kind_eligible`, `mac_target_eligibility`, `command_status`, `format_state`, `command_wait_local`.
  - `scripts/mac_test/mac_test.py` — `StatePaths`, `state_paths`, `claim_scope`, `claim_path`, `claim_once`, `command_claim`, `command_release` and blocks.
  - `scripts/mac_test/offload.py` — `OffloadRequest`, `read_config`, `repository_identity`, `remote_mirror`, `probe_script`, `remote_test_command` and `claimed_run`.
  - `scripts/mac_test/offload_test_support.py` — `OffloadCommandFixture`, temporary state and SSH/rsync command fakes.
  - `scripts/mac_test/offload_test_commands.py` — executable command fakes.
  - `scripts/mac_test/copy_watch.py` — Mac copy ownership and cleanup behavior.
  - `scripts/mac_test/test_mac_test.py`, `scripts/mac_test/test_offload.py` — existing claim and execution test style.
  - `scripts/build_targets/test_targets.py`, `scripts/build_targets/test_build_targets.py` — local fixtures and forbidden external command checks.
  - `scripts/build_hold/build_hold.py` — `parse_hold_target`, `run_mac_hold`, `release_mac_hold` and hold events.
  - `scripts/build_hold/override_guard.py` — installed scripts refuse fixture path overrides; tests run from a checkout.
  - `scripts/direct/verify.sh` — `route_test`, `try_mac_test`, `mac_retry_hook`, `run_local_test` and local admission.
  - `scripts/direct/verify_mac_offload_test_support.py` — existing fake verify environment.
  - `scripts/lint/invoke.sh` — Linux memory admission and command execution.
  - `scripts/lint/memory_gate.sh` — `buildlog_wait_for_memory`, `LINT_QUEUE_LEAVE_HOOK` and status 75.
  - `scripts/lint/memory_admit.py` — `remote_identity`, `git_identity`, `read_queue_snapshot`, `QueueInputs` and snapshot serialization.
  - `scripts/lint/memory_test_support.py` — `fake_memory_readings` and `gate_isolation_environment`.
  - `scripts/lib/atomic_file.py` — `write_text_atomic` and `create_text_atomic`.
  - `scripts/lib/nix_env.py` — existing shell environment loading and trust checks.
  - `scripts/lib/process_start.py` — process identity helper used by the held verify checkpoint.
  - `scripts/lib/scratch.py` — managed temporary copies and cleanup.
  - `scripts/buildlog/record.py` — `SYNC_HOSTS`, `step`, `queue_priority` and hold records.
  - `scripts/buildlog/sync.py` — `peer_name`, `_sync_locked`, `read_status`, `write_status` and pause handling.
  - `scripts/buildlog/report.py` — `reported_mac_calls`, `mac_section`, `copy_saving_estimate` and report assembly.
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

#### Work Order

**Goal:** A configured SSH target has independent claims, blocks and backoff state.

**Spec:**

1. Add target-aware state resolution beside `StatePaths` and `state_paths` in `mac_test.py`.
   Keep the Mac's existing `~/.local/state/mac-test` paths and fixture overrides.
   Use `~/.local/state/build-targets/<target>/` for each other target's claims, locks, block and unreachable backoff.
2. Thread the selected target through claim, release, status, block and backoff operations.
   Resolve its settings through `read_targets`; reject unknown names before any state write.
   Preserve the current no-target Mac CLI behavior.
3. Generalize `parse_hold_target`, `run_mac_hold` and `release_mac_hold` to configured SSH targets.
   Accept `target aws`, `--target aws` and the existing Mac forms.
   Keep natedev holds local to natedev. Record the actual target on hold begin and end events.
4. Make `targets.target_state` use the selected target's state without probing hosts.
   Keep read-only status free of state writes. Make a malformed block refuse admission for that target.
5. Create `config/aws_target.conf` as disabled configuration with `host=aws-build`, empty `instance_id`, and the settled spending limits.
   Keep `targets=mac,natedev` active until Phase 8's user gate.
   Add required target settings without code defaults for new tunables.
6. Test independent holds, claims and backoff for Mac and two fake Linux targets.
   Test unknown targets, malformed state and compatibility with existing Mac records.

**Held changes:** Verify changes `process_matches`, `run_is_live`, `run_is_kept`, `claim_once` and `command_claim` in `mac_test.py`.
It adds `adopt_wait_claim` and `command_claim_wait`. Add state arguments at their boundaries; preserve their ownership checks when integrated.
Do not replace process identity code or copy the held implementations.
Resources has no held source diff for the state and hold functions in this phase.

**Files:**

- `scripts/mac_test/mac_test.py` — target state selection and CLI plumbing.
- `scripts/mac_test/offload.py` — target-aware backoff paths.
- `scripts/build_targets/targets.py` — state selection.
- `scripts/build_hold/build_hold.py` — configured SSH hold dispatch.
- `scripts/buildlog/record.py` — actual target on hold events, if the existing event path needs it.
- `config/aws_target.conf`, `config/README.md` — disabled AWS settings and reference.
- `commands/build/hold.md` — target syntax.
- `scripts/mac_test/test_target_state.py`, `scripts/build_hold/test_target_hold.py` — new isolated regression tests.

**Workers:** 1 writer + 1 tester; the writer owns coordination changes across the shared files.

- `impl` — all source, config and command files; hub: `scripts/mac_test/mac_test.py` and `config/aws_target.conf`.
- `test` — `scripts/mac_test/test_target_state.py`, `scripts/build_hold/test_target_hold.py`; test only the specified target boundaries.

**State:** Independent target directories contain locks, claims, blocks and backoff records. Preserve existing Mac state in place.

**Constraints from prior phases:** none.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/mac_test.py scripts/mac_test/offload.py scripts/build_targets/targets.py scripts/build_hold/build_hold.py scripts/buildlog/record.py`

**Verification — test:** `python3 -m unittest discover -s scripts/mac_test -p 'test_target_state.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/build_hold -p 'test_target_hold.py'`

**Verification — regression:** `python3 -m unittest discover -s scripts/mac_test -p 'test_mac_test.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/test_target_state.py scripts/build_hold/test_target_hold.py`

**Acceptance gate:** The verification commands pass. Blocking fake AWS leaves Mac admission unchanged and writes no AWS state outside its directory.

**Also touches:** `scripts/mac_test/` (owner verify-unit); `scripts/build_targets/`, `scripts/build_hold/`, `commands/build/hold.md` (owner resources-unit); `scripts/buildlog/record.py` (owner report-unit).

### Phase 2 — Allocate repository slots and exchange target records  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** Each SSH target uses its own repository capacity without mixing target records.

**Spec:**

1. Key repositories by admission's normalized origin URL, using `remote_identity` and `git_identity` behavior.
   Preserve a readable repository name for `slots.<repo>`, skip lists and budget settings.
   Encode the full identity safely for paths; different origins with the same name must remain distinct.
   Make worktrees, phase clones and temporary clones of one origin share its identity.
2. Extend the existing claim protocol with a slot identity.
   Keep one claim per repository on the Mac.
   Put `slots=2`, `slots.hana=4` and `slots.cargo-liner=4` in AWS config.
   Prefer the free slot that last ran the same worktree, then another free slot.
   Allocate under the target lock; concurrent requests must never share a slot.
3. Use `~/.local/state/build-target/mirror/<repo>/<slot>` on Linux SSH targets.
   Give each slot a separate warm build folder. Preserve the Mac's mirror paths.
   Return the selected slot through the existing claim answer and request path.
   Retain lost-run claims until the corresponding remote work has ended.
4. Make `buildlog sync` exchange records with every configured SSH peer from natedev.
   Keep per-peer success state, pause behavior, timeouts and the existing Mac status reader compatible.
   Pull from the AWS peer; do not require AWS to open connections to natedev.
   Exclude admission and pending-state trees. Preserve host-owned append files and atomic rsync replacement.
5. Select Mac steps through target identity, with explicit legacy Mac host aliases for old records.
   Replace `host <> 'natedev'` in `mac_section`; never classify AWS steps as Mac steps.
   Include `aws-build` in allowed synchronized hosts. Read new sync timeouts and host mappings from configuration.
6. Test origin normalization, clone identity, slot contention, worktree preference, retained claims and isolated peer failures.
   Test a day with Mac, natedev and AWS rows; the Mac section must contain only Mac work.

**Held changes:** Verify adds `OffloadRequest.admission`, `MacClaimFromWait`, `claim_arguments` transfer and `--claim-owner-pid` parsing.
Add slot fields separately; preserve claim transfer as a distinct concern.
Resources changes `record.admission_metadata`, `record.step` and `record.queue_priority(start=...)`.
Change only host/event boundaries in `record.py`; do not add eager admission imports.
Resources also changes the Mac `free_floor_gib` comment for clone cleanup. Keep its value and cleanup behavior.

**Files:**

- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` — identity, slot allocation and mirror paths.
- `scripts/build_targets/targets.py` — all active slot claims in target state.
- `scripts/lint/memory_admit.py` — read existing identity functions; change only a narrow export if import separation requires it.
- `scripts/buildlog/sync.py`, `scripts/buildlog/record.py`, `scripts/buildlog/report.py` — peer exchange and target classification.
- `config/aws_target.conf`, `config/build_targets.conf`, `config/README.md` — slot and peer settings.
- `scripts/mac_test/test_target_slots.py`, `scripts/buildlog/test_target_sync.py`, `scripts/buildlog/test_target_report.py` — new regressions.

**Workers:** 1 writer + 1 tester; one writer owns the slot protocol and its consumers.

- `impl` — all source and config files; hub: `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`, `config/aws_target.conf`.
- `test` — the three new test files; use fake claims, command replies and a temporary SQLite index.

**State:** Slot claims, worktree affinity, warm mirror paths and per-peer sync status persist across runs.
Write affinity only with confirmed claim ownership. Preserve an occupied slot during reruns and uncertain cleanup.

**Constraints from prior phases:** Phase 1 provides target state directories and target-aware hold dispatch. The active target list still excludes AWS.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/mac_test.py scripts/mac_test/offload.py scripts/build_targets/targets.py scripts/lint/memory_admit.py scripts/buildlog/sync.py scripts/buildlog/record.py scripts/buildlog/report.py`

**Verification — test:** `python3 -m unittest discover -s scripts/mac_test -p 'test_target_slots.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_target_*.py'`

**Verification — regression:** `python3 -m unittest discover -s scripts/mac_test -p 'test_offload.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/test_target_slots.py scripts/buildlog/test_target_sync.py scripts/buildlog/test_target_report.py`

**Acceptance gate:** The commands pass. Four fake hana slots run independently, a fifth declines, and AWS records never enter the Mac section.

**Also touches:** `scripts/mac_test/` (owner verify-unit); `scripts/build_targets/`, `config/build_targets.conf` (owner resources-unit); `scripts/buildlog/` (owner report-unit); `scripts/lint/memory_admit.py` (owner queue-unit).

### Phase 3 — Execute Linux work in cached development shells  · status: todo
**Depends on:** Phase 2

#### Work Order

**Goal:** A Linux SSH target executes the selected test, check or doc command in its configured shell.

**Spec:**

1. Add `scripts/mac_test/linux_runner.py` for Linux-specific probe and command generation.
   Dispatch by the configured target platform from `offload.py`; preserve Mac command generation.
   Accept only `kinds=test,check,doc` for AWS, with `test` using the existing nextest command selection.
2. Copy only `flake.nix`, `flake.lock` and `nix/` into the shell input directory.
   Hash their paths and contents; cache `nix print-dev-env` output under that key.
   Use the configured shell selector, with `devshell.hana=ci` in config.
   Never evaluate the full mirror as a `path:` flake. Invalidate cached exports when shell inputs change.
   Keep the existing trust boundary; do not approve an unrelated repository's environment automatically.
3. Run the received source in the claimed slot's mirror.
   Set `BUILDLOG_BUILD_BUDGET_GIB` from `build_budget_gib` before the existing Linux memory gate.
   Read `target_budget_gib`, `target_budget_gib.<repo>`, `skip.<repo>` and `free_floor_gib` at use.
   Preserve caller arguments, the sent tree key, staged doc snapshots, real failure status and cleanup evidence.
4. Extend the probe with `rustc -V`, `cargo nextest --version`, mend version and tracked script commit.
   Keep the target not ready during a mismatch.
   Install matching public tools or copy tracked scripts before work begins.
   Transfer no GitHub credentials, AWS credentials, untracked secret files or source remote credentials.
5. Use Linux disk and process commands in Linux probes and cleanup.
   Quote every remote path and argument. Preserve a claim when the runner cannot confirm cleanup.
6. Add a loopback proof command with a managed scratch home and a tiny source fixture.
   Read its host and limits from `loopback.*` keys in `config/build_targets.conf`.
   Use natedev over Tailscale SSH; use private fake memory files for its admission checks.
   Keep this manual proof separate from automated tests, which fake all SSH and tool commands.

**Held changes:** Verify touches `OffloadRequest`, `claim_arguments`, `parser` and `request_from_arguments` in `offload.py`.
Keep Linux shell code in `linux_runner.py`; use small platform dispatch changes in the existing functions.
Preserve the held claim-owner transfer when the showrunner integrates it. Resources has no held diff in Linux command generation.

**Files:**

- `scripts/mac_test/linux_runner.py` — new Linux probe, shell cache and command generation.
- `scripts/mac_test/offload.py` — platform dispatch and result handling.
- `scripts/mac_test/loopback_proof.py` — manual scratch proof.
- `scripts/mac_test/offload_test_support.py`, `scripts/mac_test/offload_test_commands.py` — read existing command fake patterns.
- `scripts/lib/nix_env.py`, `scripts/lib/scratch.py`, `scripts/lint/invoke.sh` — read shell, scratch and admission contracts.
- `config/aws_target.conf`, `config/build_targets.conf`, `config/README.md` — shell, budget, parity and loopback settings.
- `scripts/mac_test/test_linux_runner.py` — fake Linux command and cache tests.

**Workers:** 1 writer + 1 tester; the new module contains the platform-specific implementation.

- `impl` — source and config files; hub: `scripts/mac_test/offload.py`, `config/aws_target.conf`, `config/build_targets.conf`.
- `test` — `scripts/mac_test/test_linux_runner.py`; test command arguments, shell keys, parity refusal and cleanup from the Spec.

**State:** Shell input copies, cached exports, matching tools and synchronized tracked scripts persist on the target.
Publish exports atomically only after successful evaluation. An incomplete cache remains unusable.

**Constraints from prior phases:** Phase 2 supplies repository identity, the selected slot and Linux mirror paths. Phase 1 supplies isolated state.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/linux_runner.py scripts/mac_test/offload.py scripts/mac_test/loopback_proof.py`

**Verification — shell:** `bash -n scripts/lint/invoke.sh`

**Verification — test:** `python3 -m unittest discover -s scripts/mac_test -p 'test_linux_runner.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/mac_test/test_linux_runner.py`

**Verification — manual loopback:** `python3 scripts/mac_test/loopback_proof.py`

**Acceptance gate:** Automated commands pass. The manual proof shows the scratch mirror and shell key for test, check and staged doc.
The proof creates no AWS resource and leaves no scratch home after completion.

**Go-live gate:** This compiled phase does not start AWS. Phase 8 completes design Phase 3 after code and the runbook are ready.
The user performs these steps at that gate:

1. Create the AWS account and admin login in the console.
2. Run the guarded `aws cloudformation deploy` procedure from the runbook.
3. Run `sudo tailscale up --ssh --advertise-tags=tag:build-aws` through SSM Session Manager.
4. Configure the tailnet tag policy in the Tailscale console.
5. Run `aws configure --profile build-scaler` at the natedev terminal.
6. Rebuild natedev and the box from the prepared NixOS branch.

No session receives the credentials or login secrets. Phase 8 supplies the complete command arguments and deployment order.

**Also touches:** `scripts/mac_test/`, offload and `scripts/lint/invoke.sh` if its existing budget hook needs a narrow fix (owner verify-unit); `config/build_targets.conf` (owner resources-unit).

### Phase 4 — Route queued work to eligible SSH targets  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** Waiting test, check and doc steps use free Linux SSH capacity while preserving their local queue place.

**Spec:**

1. Generalize SSH eligibility in `command_kind_eligible` and target iteration.
   Use configured order `mac,natedev,aws` after go-live; use fake target configuration in tests before then.
   Let eligible Linux SSH targets take test, check and doc without a natedev hold.
   Continue past a held natedev to AWS. Preserve Mac platform restrictions and local-only requests.
2. Extend the existing leave hook to every eligible SSH target in configured order.
   Reserve its target and slot before returning status 75 from the local wait.
   Transfer the reservation to the runner without an unclaimed interval.
   If another caller takes capacity, remain queued instead of starting an unreserved remote command.
3. Apply the same route to the existing test, check and staged doc paths.
   Keep build, example, final and AWS lint local by the settled scope.
   Do not move the Mac lint policy into the AWS path.
4. Preserve the first `BUILDLOG_MEM_WAIT_SINCE` across remote loss and local retry.
   Release a local memory reservation and cargo token before remote work.
   Reacquire local admission before fallback when natedev is eligible and unheld.
   Never treat a confirmed remote test failure as a reason to rerun locally.
5. Keep one selected-target result contract for the new route.
   Record `target=aws` for AWS attempts and `host=aws-build` for remote steps.
   Preserve legacy Mac wire results for existing consumers.
6. Test route order, direct use during a natedev hold, slot races, local-only refusal and fallback queue age.
   Test check and staged doc, plus excluded kinds. Fake memory, clocks, SSH and all build commands.

**Held changes:** Verify changes `mac_retry_hook`, `run_local_test`, `try_mac_test` and adds `release_wait_claim`.
It preserves queue age through `BUILDLOG_MEM_WAIT_FIRST_FILE` and renews the token through `admit_with_token` and `with_token_renewal`.
Extend routing at these boundaries; do not replace token ownership, metadata loading, clock offsets or lint cache logic.
Resources changes `RankedWaiter`, `waiter_state`, `first_queued_waiter`, `admission_pass` and `queue_read_from` in `memory_admit.py`.
Keep AWS selection outside admission policy. Never rewrite queue ranking or count token-wait rows as available build work.

**Files:**

- `scripts/direct/verify.sh` — target iteration, leave hooks and remote fallback.
- `scripts/build_targets/build_targets.py`, `scripts/build_targets/targets.py` — SSH kind eligibility.
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` — reserve-before-leave and runner transfer boundaries.
- `scripts/lint/memory_gate.sh` — target-neutral leave integration only where required.
- `scripts/lint/memory_admit.py` — read queue-age and admission contracts; preserve policy functions.
- `scripts/direct/verify_mac_offload_test_support.py`, `scripts/lint/memory_test_support.py` — read fixture patterns.
- `config/build_targets.conf`, `config/aws_target.conf` — route and retry tunables; keep AWS disabled.
- `scripts/direct/test_verify_ssh_targets.py`, `scripts/build_targets/test_ssh_eligibility.py` — fake route regressions.

**Workers:** 1 writer + 1 tester; the writer owns the shared route and claim protocol.

- `impl` — source and config files; hub: `scripts/direct/verify.sh`, `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py`.
- `test` — the two new test files; cover test, check and staged doc without real tools or live memory.

**State:** A leave attempt holds a target slot until transfer, refusal or cleanup. Retain the first local queue timestamp across retries.
The exit path must release an untransferred local claim. Uncertain remote cleanup keeps its claim.

**Constraints from prior phases:** Phase 3 provides Linux execution and parity checks. Phase 2 provides slots. Phase 1 isolates holds and backoff.

**Verification — impl:** `bash -n scripts/direct/verify.sh scripts/lint/memory_gate.sh`

**Verification — impl types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/build_targets/build_targets.py scripts/build_targets/targets.py scripts/mac_test/mac_test.py scripts/mac_test/offload.py`

**Verification — test:** `python3 -m unittest discover -s scripts/direct -p 'test_verify_ssh_targets.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/build_targets -p 'test_ssh_eligibility.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/direct/test_verify_ssh_targets.py scripts/build_targets/test_ssh_eligibility.py`

**Acceptance gate:** The commands pass. A fake Linux slot takes a queued check without a hold; an interrupted run resumes its original local queue age.

**Also touches:** `scripts/direct/verify.sh`, `scripts/mac_test/`, offload (owner verify-unit); `scripts/build_targets/`, `config/build_targets.conf` (owner resources-unit); `scripts/lint/memory_gate.sh`, `scripts/lint/memory_admit.py` if changed (owner queue-unit).

### Phase 5 — Account for AWS cost before admission  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** A durable cost estimate refuses AWS admission at either configured spending limit.

**Spec:**

1. Add `scripts/aws_target/config.py` and `ledger.py` before wiring control commands.
   Validate required values at each use. Missing, nonfinite, negative or unreadable spending data refuses starts and new admission.
   Keep disabled AWS configuration inert and free of AWS calls during ordinary builds.
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

**Held changes:** Resources changes `read_queue_snapshot`'s decoded input types through `RankedWaiter` and typed token-wait state.
Keep cost accounting independent of those types; Phase 6 reads the snapshot through its existing decoder.
Neither held branch has source changes under the new `scripts/aws_target/` paths.

**Files:**

- `scripts/aws_target/config.py`, `scripts/aws_target/ledger.py` — new settings validation, typed ledger and pure guard decisions.
- `scripts/aws_target/test_ledger.py` — isolated cost and recovery tests.
- `scripts/lib/atomic_file.py`, `scripts/lint/memory_admit.py` — read atomic persistence and snapshot contracts.
- `config/aws_target.conf`, `config/README.md` — complete controller, pricing and spending settings.

**Workers:** 1 writer + 1 tester; accounting remains separate from AWS command execution.

- `impl` — config and ledger implementation; hub: `scripts/aws_target/config.py`, `config/aws_target.conf`.
- `test` — `scripts/aws_target/test_ledger.py`; use fixed state evidence, prices, time and temporary files.

**State:** A locked ledger records charged intervals, price observations, reconciliations and durable limit events under the AWS target directory.
Commit a decision atomically before any caller can act on it. Preserve the last valid evidence during interrupted writes.

**Constraints from prior phases:** Phase 1 defines AWS state isolation. Phase 4 establishes remote admission and fallback boundaries.
AWS remains disabled; this phase creates no resource and enables no timer.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/config.py scripts/aws_target/ledger.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_ledger.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_ledger.py`

**Acceptance gate:** The commands pass. At either configured limit, every guard entry refuses new spend without changing the configured limit.

**Also touches:** none outside aws-unit; `scripts/lint/memory_admit.py` remains an input owned by queue-unit.

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
   At a limit, reject new work, let running work end, then stop the instance.
   Bound the drain by `drain_max_minutes` in config, initially 60: at the deadline, stop the instance; its running steps are lost and re-enter natedev's queue at their place.
   Account for an AWS-initiated restart before accepting work. It must not bypass the guard:
   a tick that finds the instance running while the guard refuses spend stops it at once, and the box's own idle stop is the second bound.
5. Add `remote.py` for the box's idle and interruption checks.
   Read `idle_stop_minutes` from its synchronized config. Shut down with instance shutdown behavior `stop` after the idle interval.
   Read IMDSv2 interruption notices at the configured interval, initially 5 seconds in config.
   Mark draining before ending active step process groups. Emit `aws_interrupted=1`; record lost runs with reason `interruption`.
6. Release kept AWS claims only after instance evidence reports `stopping`, `stopped` or `terminated`.
   Record the state evidence with each release; do not require SSH cleanup for those states.
   Preserve claims for running, unknown or unreachable state. Keep release idempotent under tick reruns.
7. Add tick settings with an initial 30-second interval in config.
   Keep the actual timer disabled until Phase 8. Store every retry, timeout, stale threshold and poll interval in config.
   Bound routine tick work; refresh prices only when due, rather than on each tick.
8. Test threshold selection, stale snapshots, concurrent tick/start, capacity backoff, limit drain, interruption and evidence-based release.
   Inject AWS, IMDS, process groups, clocks and notification delivery. Test no-work AWS restart behavior without real AWS.

**Held changes:** Resources changes `QueueInputs`, `RankedWaiter`, `waiter_state`, `first_queued_waiter` and `stored_waiter`.
Consume decoded rows without replacing snapshot serialization or the admission walk.
Verify changes `run_is_kept`, `claim_once`, `command_claim` and wait-claim adoption in `mac_test.py`.
Add one explicit AWS state-evidence release operation; preserve the existing live-process and remote-leftover checks.
Keep interruption decoding separate from `OffloadRequest.admission` and `claim_arguments` transfer changes.

**Files:**

- `scripts/aws_target/aws_target.py`, `scripts/aws_target/remote.py` — new controller and remote lifecycle checks.
- `scripts/aws_target/config.py`, `scripts/aws_target/ledger.py` — consume the guard and complete controller settings.
- `scripts/mac_test/mac_test.py` — state-evidence claim release.
- `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py` — guard refusal and interruption decoding.
- `scripts/lint/memory_admit.py` — read snapshot schema and decoder; modify only a necessary narrow reader boundary.
- `config/aws_target.conf`, `config/README.md` — lifecycle settings.
- `scripts/aws_target/test_control.py`, `scripts/aws_target/test_remote.py` — fake control and remote tests.

**Workers:** 1 writer + 1 tester; the controller owns its side-effect order and claim release.

- `impl` — source and config files; hub: `scripts/aws_target/config.py`, `scripts/mac_test/mac_test.py`, `config/aws_target.conf`.
- `test` — the two new tests; model AWS, IMDS and queue snapshots with temporary typed fixtures.

**State:** Controller lock, capacity backoff, current state, drain marker, idle evidence and claim-release evidence persist.
Record intent before an AWS action; reconcile actual state after an uncertain response instead of repeating an unguarded start.

**Constraints from prior phases:** Phase 5 supplies the ledger and shared guard. Phase 4 preserves queue age on loss.
Phase 3 supplies the Linux probe. Phase 2 supplies slot claims. No real resource exists before Phase 8's user gate.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/config.py scripts/aws_target/ledger.py scripts/aws_target/aws_target.py scripts/aws_target/remote.py scripts/mac_test/mac_test.py scripts/mac_test/offload.py scripts/mac_test/linux_runner.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_control.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_remote.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_control.py scripts/aws_target/test_remote.py`

**Acceptance gate:** The commands pass. Fake interruption preserves queue age; stopped-state evidence releases only the affected AWS claims.
An over-limit AWS restart never becomes ready for new work.

**Also touches:** `scripts/mac_test/`, offload (owner verify-unit); `scripts/lint/memory_admit.py` if changed (owner queue-unit).

### Phase 7 — Report AWS use, cost and estimated wait saved  · status: todo
**Depends on:** Phase 6

#### Work Order

**Goal:** Build status and reports show AWS capacity and the cost of its estimated benefit.

**Spec:**

1. Add a local AWS status reader to `build_targets.py`; ordinary status must not call AWS.
   Show the controller's recorded state, evidence time and month's dollars against the configured cap.
   Support `stopped`, `starting since HH:MM`, `running <n> of <m> slots`, interruption drain and limit refusal.
   Preserve the user's time zone and existing unreadable-state behavior.
2. Keep `host=aws-build` on remote steps and `target=aws` on calls.
   Persist original queue age, queue departure time, outcome and interruption reason for estimation.
   Extend record and index boundaries only where these fields are absent. Preserve old records with absent AWS fields.
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
   Test mixed-host data, negative estimates, missing comparisons, daily reconciliation and all status states.

**Held changes:** Resources replaces eager `memory_admit` imports with `admission_metadata` in `record.py`.
It also adds the `start` boundary to `queue_priority` for append deduplication.
Keep both function contracts unchanged; add AWS metadata at the record boundary.
Neither held branch changes `report.py` or `build_targets.py` source for these AWS sections at the inspected tips.

**Files:**

- `scripts/aws_target/measurement.py` — new local measurement reader and reconciliation support.
- `scripts/aws_target/aws_target.py`, `scripts/aws_target/ledger.py` — scheduled charge reads and reconciliation records.
- `scripts/build_targets/build_targets.py` — AWS status row.
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/report.py` — metadata, schema and AWS section.
- `scripts/buildlog/store.py`, `scripts/buildlog/sync.py` — read persistence and target sync contracts.
- `config/aws_target.conf`, `config/README.md` — reporting and charge-read settings.
- `scripts/aws_target/test_measurement.py`, `scripts/buildlog/test_aws_report.py`, `scripts/build_targets/test_aws_status.py` — isolated regressions.

**Workers:** 1 writer + 1 tester; report and index changes share one metadata owner.

- `impl` — source and config files; hub: `scripts/buildlog/index.py`, `scripts/buildlog/record.py`, `config/aws_target.conf`.
- `test` — the three new tests; use temporary records, SQLite fixtures, fixed clocks and fake charge replies.

**State:** Daily charge-read marks, actual-charge records and the go-live timestamp persist beside the ledger.
Index migration must accept existing records. A retry must not repeat a successful daily charge read or duplicate a reconciliation.

**Constraints from prior phases:** Phase 6 records controller state, drain reason and slot-release evidence.
Phase 5 owns cost totals. Phase 4 records AWS target identity. Phase 2 keeps peer records separate.

**Verification — impl:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/measurement.py scripts/aws_target/aws_target.py scripts/aws_target/ledger.py scripts/build_targets/build_targets.py scripts/buildlog/record.py scripts/buildlog/index.py scripts/buildlog/report.py`

**Verification — test:** `python3 -m unittest discover -s scripts/aws_target -p 'test_measurement.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_aws_report.py'`

**Verification — test:** `python3 -m unittest discover -s scripts/build_targets -p 'test_aws_status.py'`

**Verification — test types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/test_measurement.py scripts/buildlog/test_aws_report.py scripts/build_targets/test_aws_status.py`

**Acceptance gate:** The commands pass. Fixed mixed-host data produces the specified AWS line without changing Mac totals or double-counting charges.

**Also touches:** `scripts/build_targets/build_targets.py` (owner resources-unit); `scripts/buildlog/` (owner report-unit).

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
   Add x86_64 host output, data-volume mounting, Tailscale SSH, SSM, required build tools and remote lifecycle services.
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
   Give `tag:build-aws` no initiated connections to natedev or the Mac; permit natedev's user to reach it.
7. Build the deployment decisions below into the template, runbook and NixOS patch.
   Keep all settled choices and both caps unchanged.
   Do not present a fake AWS test as proof of a real bootstrap or persistent-request behavior; the runbook's user-run checks prove them at go-live.
8. Test parameter generation and the full deployment command sequence through a fake AWS executable.
   Assert guard-before-resource order, disabled defaults, Budget amount, IAM scope, retention and secret exclusion.
   Parse template JSON locally. Keep NixOS evaluation separate from system activation.

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

**Constraints from prior phases:** Phase 7 supplies reports. Phase 6 supplies tick, remote idle stop and interruption handling.
Phase 5 supplies deployment preflight and the ledger. Phases 1–4 supply target state, slots and Linux routing.
The source design's Phase 3 go-live occurs only here, after implementation and the runbook are complete.

**Verification — impl:** `python3 -m json.tool docs/plans/builds-aws/cloudformation.json`

**Verification — impl types:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python scripts/aws_target/deploy.py scripts/aws_target/config.py scripts/aws_target/test_deploy.py`

**Verification — impl tests:** `python3 -m unittest discover -s scripts/aws_target -p 'test_deploy.py'`

**Verification — NixOS writer:** In the prepared NixOS checkout, run `nix eval --raw .#nixosConfigurations.aws-build.config.system.build.toplevel.drvPath`.

**Verification — NixOS writer:** In that checkout, run `nix eval --raw .#nixosConfigurations.natedev.config.system.build.toplevel.drvPath`.

**Acceptance gate:** Local verification passes, the patch matches the prepared NixOS branch, and the runbook contains the complete guarded command sequence.
The implementation checkpoint requires no AWS resource. Record go-live separately after the user gate.

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
10. Confirm tool parity and guard readiness through the runbook.
11. Enable `targets=mac,natedev,aws` and the tick timer.
12. Confirm test, check and doc routing through the runbook's user-run proof.
13. Record the activation time for the report.

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
