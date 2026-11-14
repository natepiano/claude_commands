# Build folders

Status: implementation plan. Phase bf1 is in progress at a devtools worker.
The user approved D1–D9 on 2026-11-12 at 12:45 EST.
This plan has nine phases and an estimate of **64 worker hours**.

## Cut

1. Remove the separate measurement feature and phase. The user approved the slot values on 2026-11-12.
2. Remove the fixture lease API and four separate fixture migration phases. Tests call the maker through one small private-config helper.
3. Merge queue attribution into slots. Merge gate migrations and assign other callers to their existing owners.
4. Remove mandatory human-link trees, extra lease states, persistent deletion records, and a separate rescue manifest format.
5. Remove the long search appendix and repeated test matrices. Keep one maker inventory and focused acceptance gates.

## Approved contract

1. **D1 — One maker.** `scripts/lib/build_folders.py` and `config/build_folders.conf` make every build and test folder.
   Human names have the form `<showrunner>-<unit>/<work>-<purpose>`.
   The work is a phase ID or a short name from the maker.
   Never infer ownership from a path.
2. **D2 — Repository slots.** Every repository task uses a configured, fixed slot pool for its machine and repository.
   Each slot has stable source and Cargo target paths.
   Taking a slot resets its source and retains its target.
   Returning a slot preserves affinity for its previous task.
   Convert the Mac, AWS, and natedev gate caches into these slots.
   Mac hana has exactly two slots, as the user approved.
3. **D3 — Visible leases.** Each lease records the showrunner, unit, and purpose.
   The build queue page shows those fields for every occupied slot.
4. **D4 — Size limits.** Each slot has a configured size limit.
   At acquisition, remove the target when the slot exceeds that limit.
   Then run one cold build.
5. **D5 — Process authority.** The working process holds a file lock beside its folder or slot.
   A session never owns a folder.
   Without a held lock, a slot is free and a one-off folder is eligible for removal.
   One-off folders contain temporary files, never repository work.
6. **D6 — Rescue before deletion.** Save all Git work under `refs/rescue/<name>` in the source repository before deletion.
   Delete without a confirmation after the rescue succeeds.
7. **D7 — Source enforcement.** A source test rejects `mkdtemp`, `git clone`, and `git worktree add` outside the maker function.
8. **D8 — Disk alarm.** The disk-floor temporary sweep becomes an alarm without deletion rules.
   The stall watch stops hung owners.
9. **D9 — Review folders.** Remove `~/rust/repro` row folders after the row verdict and the rescue ref.

## Implementation contract

1. Extend `scripts/lib/build_folders.py`; do not add another maker.
   Keep `create_build_folder()` as the sole physical allocation function.
   Put repository initialization, clone, and worktree creation inside that function.
   Use its existing Python API or CLI for local, shell, JavaScript, and remote callers.
2. Build the human name from explicit showrunner, unit, work, and purpose arguments.
   Keep stable physical slot paths and stable Cargo target paths.
   Keep the human name in the lease and caller-visible output without requiring another link tree.
3. Use Python `fcntl.flock` on a lock file beside the folder or slot.
   Keep the lock inode outside every reset or deletion boundary.
   Acquire the lock before exposing the folder.
   Pass the descriptor to the working process and its children.
   A held lock proves ownership; metadata only supplies attribution.
4. Serialize acquisition, rescue, source reset, target removal, and folder deletion under that lock.
   Preserve commits, refs, staged changes, unstaged changes, untracked source, ignored source, conflicts, and nested repository work.
   Publish the rescue objects under a unique `refs/rescue/<name>` in the source repository.
   Verify the rescue before deletion; retain the folder when rescue fails.
   Generated Cargo target data does not need a source snapshot.
   An ignore rule alone does not identify generated data.
5. Use one fixed pool for each machine and repository identity.
   Different worktrees of one source repository share that pool.
   Prefer the task's previous free slot, then another free slot.
   Use existing admission behavior when all slots are held; never create an overflow slot.
6. Measure the complete slot size when taking the slot.
   Rescue before resetting source or removing an over-limit target.
   Remove the target once, then run one cold build.
   Report a source-only excess without a repeated deletion loop.
7. Use one-offs only for temporary files outside repositories.
   Keep fixture repositories in private slots, separate from file-only fixture roots.
   Rescue an unexpected repository before removing its folder.
8. Make legacy collectors respect held leases in Phase 2, before any migrated caller activates.
   Acquire the same lease lock before a collector judges or deletes a managed folder.
   Keep old guards for legacy folders until their callers drain.
   Convert the disk-floor sweep to alarms only in Phase 8.
9. Preserve admission, memory, account, cancellation, and result-delivery behavior during migration.
   Drain old users before adopting their cache paths.
   Keep the stall watch's stop decision separate from proof that a slot is free.

## Configuration and user decisions

The builds queue unit owns `config/build_folders.conf`.
The user approved the slot values on 2026-11-12 (relayed by builds). Count x complete size limit:

| Machine | Repository | Slots | Size limit |
| --- | --- | ---: | ---: |
| natedev | hana | 4 | 40 GiB |
| natedev | hana merge gate | 1 | 120 GiB |
| natedev | cargo-liner | 6 | 15 GiB |
| natedev | claude | 16 | 2 GiB |
| natedev | tool-based-ui | 2 | 25 GiB |
| natedev | every other repository | 1 | 20 GiB |
| Mac | hana | 2 | 40 GiB |
| Mac | cargo-liner | 2 | 15 GiB |
| AWS | none (per-run boxes keep no cache) | 0 | - |

The values live in `config/build_folders.conf` as `slots.<machine>.<repository>.count` and `slots.<machine>.<repository>.max_bytes`.
They change only through /tune and apply at the next slot take, with no deploy.
`roots.slots.<machine>` keeps a suitable existing root. Existing `roots.*`, `names.*`, `patterns.*` and `prefixes.*` settings stay.
Missing required capacity values stop admission with a named config error.
Tests use explicit private values under the same schema; they do not set production limits.
The private-config helper supplies test roots and values, not another allocation API or fixture registry.
Do not add per-module production fixture keys or a parent lease that blocks its child fixture slots.
Mac hana's approved count remains two; its size limit still needs user go.

## Rollout and estimates

Hours include implementation, focused tests, review fixes, and owner coordination.
Hours exclude user decision time, machine admission waits, and cloud runtime charges.
**B** marks a builds-owned file or file group throughout this plan.
The named builds unit reviews changes to each B file before landing.
Devtools owns all other files.
Coordinate shared files through sdlc; phase ownership does not transfer file ownership.

| Phase | Deliverable | Owner unit | Hours | Depends on |
| --- | --- | --- | ---: | --- |
| bf1 | Maker and process lock; landed | devtools; queue reviews B files | 6 | None |
| 2 | Rescue and legacy collector protection | queue; resources and devtools own collector edits | 8 | bf1 |
| 3 | Slots, limits, affinity, and queue fields | queue; resources wires capacity | 10 | 2; values approved 2026-11-12 |
| 4 | Gate caches become slots | queue and aws | 8 | 3 |
| 5 | Other repository callers | verify, style, and devtools | 8 | 4 |
| 6 | One-offs and review folders | devtools; builds owners edit B files | 6 | 5 |
| 7 | Remaining fixtures and source enforcement | devtools; builds owners edit their fixtures | 8 | 6 |
| 8 | Stall watch and alarm-only sweep | resources and devtools | 6 | 7 |
| 9 | Test run folders; runs beside Phase 2 | devtools | 4 | bf1 |
| **Total** | | | **64** | |

Run the phases in this order.
Migrate adjacent fixtures with their callers during Phases 4–6.
Phase 7 completes the fixture audit and closes alternate allocation paths.
Phase 8 is last; enable the stall watch before removing disk-floor deletion rules.

## Maker inventory by phase

These entries name current makers and lifecycle consumers, not every textual match.
Line references describe the inspected tree; recheck them before implementation.
Each phase also owns the adjacent tests for its files.
All builds-owned adjacent tests carry the same B owner as their implementation.

| Phase | Makers or consumers | Owner |
| --- | --- | --- |
| bf1–3 | **B:** `scripts/lib/build_folders.py`, `scripts/lib/test_build_folders.py`, `config/build_folders.conf` | queue |
| 2, 8 | **B:** `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`, `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py` | resources |
| 2, 8 | `scripts/lint/folder_audit.py`, `scripts/lint/test_folder_audit.py`; cleanup in `scripts/lib/scratch.py` | devtools |
| 3 | **B:** `scripts/build_queue/queue_view.py`, `scripts/build_queue/memory_feed.py`; `scripts/build_targets/build_targets.py` only for required capacity wiring | queue for queue files; resources for target files |
| 4 | **B:** `scripts/copies/copies.py`; gate pools and natedev caches | queue |
| 4–5 | **B:** `scripts/mac_test/offload.py`, `scripts/mac_test/linux_runner.py`, `scripts/mac_test/mirror_root.py` | verify; queue coordinates shared gate slots in Phase 4 |
| 4 | **B:** `scripts/aws_target/gate_box.py`, `cloud_gate.py`, `gate_trial.py`, `box.py` | aws |
| 4 | `scripts/production/merge_gate.py`, `ci_points.py`, `waiting.py`, `bump_gate.py`, `fast_track.py` | devtools; queue coordinates gate adapters |
| 5 | **B:** `scripts/direct/verify.sh`, `scripts/mac_test/copy_watch.py`, `scripts/mac_test/loopback_proof.py` | verify |
| 5 | **B:** `scripts/style/crate_run.py`, `bulk_run.py`, `style_plan.py`, `style_sync.py`, `add_style.py` | style |
| 5 | `scripts/worktree/make_worktree.sh`, `worktrees.py`, `commands/make_a_worktree.md`, `scripts/production/add_unit.py`, `scripts/agents/shutdown.py` | devtools |
| 5 | `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `scripts/direct/simplify_daily.py`, `scripts/buildlog/rust_release.py` | devtools |
| 5–6 | `scripts/release/resolve_path_pins.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/berth/install/install.sh`, `scripts/production/smoke_launch.sh` | devtools; use slots when content includes repositories |
| 6 | `scripts/lib/scratch.py`, `scripts/production/change_review.py`, `commands/change_review.md`; `~/rust/repro` row folders | devtools |
| 6 | **B:** file-only allocations in the Phase 4–5 files; `scripts/style/style_ledger.py`, `style_footprint.py`, `style_proposals.py` | queue, verify, aws, or style according to the file above |
| 6 | **B:** `scripts/build_queue/ci_cargo_front.py`, `memory_page_proof.mjs`, `queue_place.py` | queue |
| 6 | `scripts/review_links/change_page.py`, `branch_page.py`, `scripts/whoami/profile_notes_pass.py`, `scripts/lib/models_registry.py` | devtools |
| 6 | `scripts/production/merge_checkpoint.py`, `unit_config.py`, `scripts/usage/collect.py`, `scripts/hana_shot/hana_shot.py` | devtools |
| 6 | `scripts/test_times/coverage_overlap.py`, `module_cost.py`, `module_run.py`; use slots for repository content | devtools |
| 6 | `scripts/lint/style_review.py`, `scripts/hooks/checkpoint_gate.py`, `session-start-temp-root.py`, `scripts/direct/demo_refusals.sh`, `scripts/validate_and_push/ci_tick.sh` | devtools |
| 7 | `scripts/production/test_merge_checkpoint_validation.py:248`, `scripts/production/cli_fixture.py:146`, `scripts/lint/memory_test_support.py:95` | devtools |
| 7 | `scripts/production/staff_test_support.py`, `merge_gate_test_support.py`, `scripts/berth/tests/installed_front_end.py` | devtools |
| 7 | **B:** `scripts/direct/recorder_fixture.py`, `scripts/build_queue/queue_fixture.py`; all remaining builds fixtures | verify or queue; other builds fixtures retain their runtime owner |
| 7 | `scripts/direct/assign_launcher_test_support.py`, `scripts/berth/tests/check_installed_hooks.py`; **B:** `scripts/style/test_style_ledger_tickets.py` | devtools; style owns the B file |
| 7 | Remaining Python, shell, and JavaScript fixtures under `scripts`; maker instructions under `commands` | devtools; builds fixtures retain their runtime owner |
| 9 | `scripts/production/targeted_test.py`, the shared test bootstrap under `scripts/lib`, and their tests | devtools |
| 8 | `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`; **B:** `scripts/build_targets/working_set.py` as a process identity reference | devtools; resources owns the B reference |

## Phase bf1 — Maker and process lock

**Status:** landed on main.
**Goal:** Implement D1 and D5 without moving callers.
**Owner unit:** devtools implements the current dispatch; builds queue reviews and retains ownership of the B files.
**Estimate:** 6 worker hours.
**Files:** The bf1–3 inventory row; no caller migration files.
**Dispatch:** `/tmp/claude/unit/bd19678b-c7b1-4e24-be9e-133ecfe24c7a/phases/bf1/prompt.md` remains authoritative for this phase.

1. Extend the existing `create_build_folder()`, `temporary_build_folder()`, and `run_build()` implementation.
2. Build `<showrunner>-<unit>/<work>-<purpose>` from explicit arguments.
3. Acquire a Python `fcntl.flock` before exposing the folder.
4. Keep the lock beside the folder so deletion preserves its inode.
5. Pass the held descriptor through `run_build`, the CLI, and working descendants.
6. Record showrunner, unit, work, purpose, pid, and start time beside the lock.
7. Return held or free from a query function without checking session liveness.
8. Keep every current caller working without moving callers in this phase.

The owner of a folder owns its whole subtree.
Parent removal removes every nested folder and record, whether held or free.
A process must release a nested folder before its parent context ends.
Keeping a nested folder after the parent context ends is a caller error.
Each nested context removes its own folder, lease, and lock when it ends first.

An asynchronous exception, such as `KeyboardInterrupt`, between cleanup bytecodes can leave the lock held until the process exits.
The kernel then releases the lock.
The folder becomes free recorded residue for Phase 9 cleanup.
The maker adds no signal handler or signal mask to close these bytecode windows.

**Acceptance gate:** Add tests for mutual exclusion, child inheritance, release after process death, and explicit names.
Show that these tests fail before the change.
Require basedpyright to report zero errors and zero warnings on changed files.
Run `scripts.lib.test_build_folders` and every caller's test modules as one detached run.
Discover callers of all three existing functions before selecting those modules.
Do not add slots, rescue refs, or size limits during bf1.

## Phase 2 — Rescue and legacy collector protection

**Goal:** Preserve Git work and protect every held lease from old deletion rules.
**Owner units:** queue owns rescue; resources owns `tmp_sweep.py`; devtools owns `folder_audit.py` and scratch integration.
**Devtools part (bf2d):** `build_folders.collector_lease` holds a managed folder's lease lock without waiting.
`scratch.removal_lock` and `scratch.judgment_lock` take it first and hold it through judgment and deletion; a held lease is busy.
The folder audit, the tmp sweep, scratch end, the floor sweep and copy removal get the guard through these locks.
**Held child leases (bf2p):** `removal_lock` also holds every lease lock below the folder through deletion; a held one keeps the parent.
**Lease records (bf9r):** a lock file or lease record is judged by its folder's lease; a live owner's records are busy.
`tmp_sweep.py` still reports the old busy reason; resources can use `scratch.BUSY_REASON`.
**Estimate:** 8 worker hours.
**Depends on:** bf1.
**Files:** Maker files, `scripts/lib/scratch.py`, and the Phase 2 collector inventory rows with their tests.

1. Add rescue and deletion under the same held lease lock.
2. Preserve the Git work listed in the implementation contract under `refs/rescue/<name>` in the source repository.
3. Verify the rescue objects and ref before reset or deletion.
4. Retain the source when rescue fails; do not request another confirmation after a successful rescue.
5. Make `tmp_sweep.py` and `folder_audit.py` acquire the lease lock before judging or deleting a managed folder.
6. Treat a held lease as busy even when legacy session, cwd, and `.claude-busy` checks permit deletion.
7. Preserve existing guards for folders that do not yet use the maker.

**Acceptance gate:** Restore commits, index changes, working files, ignored source, conflicts, and nested repository work from rescue refs.
Inject a rescue failure and require the source to remain.
Run each legacy collector against a held lease with no legacy busy marker.
Require both collectors to retain that folder.
Check the lock across judgment and deletion to prevent a race with acquisition.
Run maker, scratch, temporary sweep, and folder audit tests before Phase 3 activates leases.

## Phase 3 — Slots, limits, affinity, and queue fields

**Goal:** Implement D2–D4 through the maker and existing queue page.
**Owner units:** queue implements slots and queue fields; resources wires capacity.
**Estimate:** 10 worker hours.
**Depends on:** Phase 2; user go before production activation.
**Files:** Maker/config files and the Phase 3 queue/target inventory row with adjacent tests.

1. Define fixed pools by machine and repository identity.
2. Keep source and target paths stable; reset source and retain target at acquisition.
3. Preserve task affinity and select only free slots under their locks.
4. Use existing admission behavior when the pool is full.
5. Check complete slot size and remove an over-limit target once before a cold build.
6. Publish showrunner, unit, purpose, and machine beside each occupied slot on the existing queue page.
7. Write the approved count and size values (see Configuration and decisions) into config/build_folders.conf.
8. Provide one small private-config helper for tests that call the maker directly.

**Acceptance gate:** Test full pools, affinity, source reset, target retention, and one cold rebuild after target removal.
Require missing approved capacity keys to stop admission.
Verify Mac hana has exactly two slots.
Verify queue attribution for every held slot without using metadata as proof of life.
Use isolated config values for tests; require user go before any production pool activates.

## Phase 4 — Gate caches become slots

**Goal:** Replace copies gate pools and natedev, Mac, and AWS gate caches with the fixed slots.
**Owner units:** queue owns copies and gate pools; aws owns AWS files; verify owns Mac execution files.
Devtools owns production gate adapters.
**Estimate:** 8 worker hours.
**Depends on:** Phase 3.
**Files:** All Phase 4 inventory rows and their adjacent tests.

1. Drain legacy cache users before adopting their paths.
2. Replace gate allocation and release with maker acquisition and lease release.
3. Retain stable Cargo targets while resetting source for the requested revision.
4. Pass the lease descriptor into local and remote working processes.
5. Route destructive cache operations through rescue under the lease lock.
6. Migrate adjacent gate fixtures through the maker and private config.

**Acceptance gate:** Run copies, production gate, and affected Mac/AWS tests using isolated transports.
Verify cache reuse, revision reset, full-pool admission, cancellation, and lock release after failure.
Require the same lease fields on queue rows across all three machines.
Do not provision real machines or reset live caches during tests.

## Phase 5 — Other repository callers

**Goal:** Move remaining repository work into slots without changing task behavior.
**Owner units:** verify owns `verify.sh` and mirrors; style owns style callers; devtools owns unit and recovery worktrees.
**Estimate:** 8 worker hours.
**Depends on:** Phase 4.
**Files:** All Phase 5 inventory rows and their adjacent tests.

1. Route verify and mirror creation through the maker on each execution machine.
2. Route style base, worker, validation, and standing sources through slots.
3. Route explicit worktrees, unit worktrees, recovery worktrees, and remaining repository callers through slots.
4. Classify staging callers by content; repository content requires a slot.
5. Migrate adjacent fixtures with their runtime owners.
6. Preserve revision selection, cancellation, result delivery, and recovery behavior.

**Acceptance gate:** Run affected verify, mirror, style, worktree, unit, and recovery tests.
Prove that reused targets survive source reset.
Prove that cancellation releases the working process lock.
Require rescue before each replaced reset or repository deletion path.
Verify owns mirror execution; resources does not own this migration.

## Phase 6 — One-offs and review folders

**Goal:** Centralize file-only temporary folders and implement D9 at the row verdict.
**Owner unit:** devtools coordinates; builds owners edit their B files.
**Estimate:** 6 worker hours.
**Depends on:** Phase 5.
**Files:** All Phase 6 inventory rows, their adjacent tests, and `commands/change_review.md`.

1. Route file-only scratch, browser profiles, command output, and temporary-file parents through maker one-offs.
2. Keep ordinary child directories inside their existing allocation.
3. Route repository content discovered during this migration through slots.
4. Remove `~/rust/repro` row folders after the row verdict and a verified rescue ref.
5. Retain a row folder when rescue fails.
6. Migrate adjacent fixtures through the maker and private config.

**Acceptance gate:** Run affected scratch, review, and temporary-file caller tests.
Verify lease inheritance for shell and browser workers.
Verify row-verdict cleanup after rescue and folder retention after rescue failure.
Verify that no one-off request silently contains repository work.

## Phase 7 — Remaining fixtures and source enforcement

**Goal:** Complete test migration and enforce D7 without another fixture allocation layer.
**Owner unit:** devtools owns the audit and enforcement; builds owners migrate their remaining fixtures.
**Estimate:** 8 worker hours.
**Depends on:** Phase 6.
**Files:** Phase 7 inventory rows and remaining executable makers found by the audit.
Add `scripts/lib/test_build_folders_sources.py`.
Update **B:** `scripts/copies/copy_guard.py` and `test_copy_guard_sources.py` under queue ownership.
Update `scripts/hooks/pre-tool-use-scratch-guard.py`, `test_scratch_guard.py`, `commands/unit/direct.md`, and `commands/showrunner/produce.md` as needed.

1. Audit `scripts` and `commands` for allocation operations, including Git argument vectors and generated remote commands.
2. Migrate `test_merge_checkpoint_validation.py:248`, `cli_fixture.py:146`, and `memory_test_support.py:95` explicitly.
3. Audit Git initialization and repository-copy helpers as well as clone and worktree commands.
4. Migrate remaining Python, shell, and JavaScript fixtures directly through the maker.
5. Use private slot config for repository fixtures and one-offs for file-only fixtures.
6. Reject executable `mkdtemp`, `TemporaryDirectory`, directory `mktemp`, clone, and worktree-add operations outside `create_build_folder()`.
7. Detect Git argument vectors, aliases, shell text, and generated remote command text.
8. Keep comments, imports, annotations, and inert examples separate from executable makers.
9. Remove allocation adapters that bypass the maker; permit no per-file allocation allowlist.

**Scope (builds, 2026-11-12):** The source test fails on bare `mkdtemp`, `git clone` and `git worktree add` outside the maker.
`TemporaryDirectory` as a context manager and pytest tmp fixtures stay, because they delete themselves.
**Dispatches:** Split the migration into one dispatch for each owner unit and each scripts folder.
Each dispatch names its files and runs its own modules as its gate.
Run the source test last, after every dispatch lands.

**Acceptance gate:** Run the source test, existing command guard tests, and affected fixture modules.
Insert forbidden operations into temporary source inputs and require the source test to reject each operation.
Include vector-form Git clone, init, and worktree-add cases in the audit checks.
Confirm that shared fixture factories cannot create repositories outside maker slots.
Use this literal search as a starting point, not as the complete inventory:

```sh
rg -n 'mkdtemp|TemporaryDirectory|git clone|worktree add|make_worktree' scripts commands
```

## Phase 8 — Stall watch and alarm-only sweep

**Goal:** Implement D8 after all callers and tests use the maker.
**Owner units:** devtools owns stall watch and folder audit; resources owns the disk-floor sweep and alarm.
**Estimate:** 6 worker hours.
**Depends on:** Phase 7.
**Files:** Phase 8 inventory rows, `scripts/lib/process_group.py` as a reference, and adjacent tests.
Update **B:** `docs/as-built/copies.md` under queue ownership and `docs/as-built/accounts-queue-panes.md` under queue/resources ownership.
Amend existing devtools owner documents when needed.

1. Apply the existing configured stall policy to held lease owners.
2. Verify process identity before stopping a hung owner and its working descendants.
3. Observe lock release before treating the slot as free.
4. Preserve held status when remote evidence is unavailable.
5. Enable this behavior before removing the disk-floor deletion rules.
6. Convert `tmp_sweep.py` and its floor callers to measurement and alarms without deletion.
7. Delegate eligible one-off cleanup to the maker's lock and rescue path.
8. Retire competing managed-folder deletion paths after legacy users drain.

**Acceptance gate:** Test productive work, hung owners, PID reuse, remote loss, and remaining descendants.
Require unrelated processes to receive no stop signal.
Require low-disk checks to retain every fixture folder.
Verify that explicit maker cleanup can still rescue and remove an unlocked one-off.
Run stall watch, process group, sweep, folder audit, and source enforcement tests.
Run the existing complete gate after the final migration.

## Phase 9 — Test run folders

**Status:** built in bf9 (609b743ff); bf9r moved the start to interpreter startup only.
**Implementation:** Direct unittest and timed runs allocate through the shared Python bootstrap.
The startup hook and the shim bootstrap start the folder; `package_root.py` selects the checkout only.
`targeted_test.py` uses the same maker context.
Each run holds its lock and sets TMPDIR for itself and its children.
Normal exit removes the complete allocation.
The next run sweeps free residue in the same test-runs parent before allocation.
The sweep retains held allocations and removes unfinished temporary lease files under the allocation lock.
`run_build` keeps its command in the caller's process group and installs no signal handler.
Stopped commands leave recorded residue for the sweep after their locks become free.

**Goal:** Give each test run outside the merge gate its own TMPDIR, so a killed run leaves no folder behind.
**Owner unit:** devtools.
**Estimate:** 4 worker hours.
**Depends on:** bf1 only; this phase runs beside Phase 2.
**Files:** The Phase 9 inventory row and adjacent tests.

Killed test runs leave `TemporaryDirectory` folders, for example `sender-guard-*` (`scripts/message/session_send_test_support.py:39`), `review-link-test-*` (`scripts/review_links/test_review_link.py:116`) and `tmp*` (`scripts/lint/test_tmp_sweep.py`).
The tests are correct: Python runs no cleanup on SIGTERM.
The merge gate already gives each run its own TMPDIR (`scripts/production/merge_gate.py:1204`).

1. Make one per-run folder through the maker in `targeted_test.py` and in direct unittest runs through the shared bootstrap.
2. Set TMPDIR to that folder for the run and its children. The run holds the folder's lease lock.
3. Remove the folder when the run exits.
4. Remove a killed run's folder when its lease lock is free. Add no signal handler (user rule).
5. Do not change the tests that make temporary folders.

**Acceptance gate:** Kill a run with SIGTERM and with SIGKILL; require its folder to be removed once its lock is free.
Require a live run's folder to stay.
Run the targeted test and bootstrap tests.

## Shared completion gate

1. Run focused tests for changed behavior and every affected caller.
2. Run basedpyright on changed Python files with zero errors and zero warnings.
3. Run `bash -n` on changed shell files.
4. Run `git diff --check`.
5. Run long validation through `scripts/agents/detached_runs.py start` and record the completed result before phase acceptance.
6. Obtain the named builds owner's review for each B file before landing.

Do not weaken existing assertions or test behavior to obtain a pass.
This planning task changes only this document and creates no commit.
