# models-unit: rename-track phases from herdr-unit's plan

> **Production:** sdlc
> **Source:** `docs/plans/herdr-worker-panes.md` on `natedev-herdr` (herdr-unit). sdlc moved these three items to models-unit on 2026-11-02 08:2x EST, on the user's ask to make rename land sooner.

## Delegation Context

- **Project:** `~/.claude` scripts — the agent tooling.
- **Project started:** 2026-11-02T13:25:43.957+00:00
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, `unittest`, basedpyright; taplo 0.10.
- **Source plan:** read its Delegation Context in `/home/natepiano/worktrees/claude-natedev-herdr/docs/plans/herdr-worker-panes.md`, mainly **Staff design (2026-10-31)**, **One identity join**, **No hard-coded config**, **Rerun-safe tools**, **One file writer** and **Isolation gate**. They apply to every phase here unchanged.
- **Files this unit holds:** `scripts/production/staff_sources.py` and `scripts/production/staff_test_support.py`, for 3k and Phase 4 (sdlc, 2026-11-02).
- **Landing:** each phase lands alone through its own gate.
- **Order (sdlc, 2026-11-02):** Phase 4 is on the rename path (devtools-unit's 3t waits for it); 3k is needed only before herdr 4c. When herdr-unit's 4h merges into `sdlc`, Phase 4 goes ahead of whatever is left of 3k.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>`, run from `~/.claude` main's copy after a merge of `origin/sdlc`.
- **Lint:** `basedpyright <changed Python files> <every caller module of a changed file>`; pass is `0 errors, 0 warnings`.
- **The four sdlc lines (2026-10-30):** good engineering first; no copies; when many things break the same way, the copy is the cause; fix at the cause.

## Phases

### Phase 3k — `.unit.toml` writers emit taplo-formatted text  · status: done

**Depends on:** herdr Phase 3q (landed). Lands before the configuration-completeness audits (herdr 4c, 4d). Live repair (sdlc, 2026-10-31): the 16:24 fill wrote `.unit.toml` in a format that `taplo fmt --check` rejects under each repository's own `taplo.toml` (`align_entries`, `reorder_keys`).

#### As-built

- `unit_config.write`, `write_parked` and `park` emit the text that `taplo fmt --no-auto-config [--config <file>] --stdin-filepath <path> -` gives, and return `Published | AlreadyCurrent`.
- `_publish` compares formatted bytes, so a file with the right values in the wrong format is rewritten. `_formatted` checks that the formatted text decodes to the same values.
- `repository_formatting(worktree)` reads the repository's `.taplo.toml` or `taplo.toml` into `RepositoryTaploConfig(text)`, else `DefaultTaploConfig`. `UnitConfig.formatting` holds it (`compare=False`); parked files save it under the `taplo_config` key.
- A parked file takes its format from the live recorded start folder's repository config, else its saved `RepositoryTaploConfig`, else the caller's config.
- The staff fill prints `changed` or `unchanged` from the `Published` result, and rewrites existing live configs through `write`. `retirement_plan(..., check_format=...)` checks format only on dry runs.
- A taplo failure raises to the caller; unformatted text is never written.

**Files:**
- `scripts/production/unit_config.py` — formatting, `RepositoryTaploConfig`, `DefaultTaploConfig`, `Published | AlreadyCurrent`.
- `scripts/production/staff_sources.py` — fill report and rewrite.
- `scripts/production/unit_config_format_test_support.py` — aligning-config fixtures and a counting taplo wrapper.
- `scripts/production/test_unit_config.py`, `scripts/production/test_staff_sources.py` — tests that run real taplo on writer output.

**Binds later work:** parked files carry `taplo_config`; a reader older than this change rejects that key.

**Gotchas:** `unit_config` calls taplo through its own `run_taplo` alias of `subprocess.run`. `add_unit_test_support` fakes `subprocess.run` for every command that is not git, and the alias keeps that fake away from taplo.

### Phase 4 — Member names come from the staff row  · status: done

**Depends on:** herdr-unit's 4h checkpoint merged into `sdlc`. Must land before devtools-unit starts 3t and before herdr-unit's 4e. After herdr-unit's 3a lands, it also touches `scripts/shutdown/restart.py`.

#### As-built

- `add_unit.py`, `add_showrunner.py` and `resume_unit.py` take the staff row's name for `-n`, the tab title and the pane title.
- `scripts/shutdown/launch_names.py` holds `LaunchNames`: one staff snapshot per restore or restart, rows selected by native resume id, one owner ssh read on the Mac. An unreadable snapshot reuses each saved display name and prints one problem line. A missing row names the entry and keeps its saved name. No row is selected by a saved name.
- `pane_restore.py`, `restart.py` and `stop_resume.py` title a resumed pane and its tab from that snapshot inside `herdr.restore_staff(...)`. An empty name never clears a title. An existing pane is retitled when its tab title is stale, also when its pane label already matches.
- `pane_record.without_legacy_identity` is public; no launch or restart record sets or keeps `STAFF_ID`.
- `herdr.py` drops `STAFF_ID`, `CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID` from every agent environment. `copy_claude_trust` writes through `write_text_atomic(new_file_mode=0o600)`; `atomic_file.write_text_atomic` takes an optional `new_file_mode`.
- `herdr_tab_title.py` resolves a session by its native runtime id. A unit keeps its row name on the tab and pane, and a mismatch prints the `/showrunner:rename_unit` command. Showrunners and workers keep their row titles; a session with no row keeps the ad hoc title. The hook writes no staff row and never stops the turn. It prints the missing-row line only when an incomplete clear is proved.

**Files:**
- `scripts/production/add_unit.py`, `add_showrunner.py`, `resume_unit.py` — row names at launch and resume.
- `scripts/shutdown/launch_names.py` — `LaunchNames` and its read states.
- `scripts/shutdown/pane_record.py`, `pane_restore.py`, `restart.py`, `stop_resume.py` — restore names and titles from one snapshot.
- `scripts/herdr/herdr.py`, `scripts/lib/atomic_file.py` — identity-free agent environment; mode for a new atomic file.
- `scripts/hooks/herdr_tab_title.py` — the hand `/rename` rule.
- Tests: `test_add_unit.py`, `test_add_unit_restore.py`, `test_add_showrunner.py`, `test_resume_unit.py`, `test_herdr.py`, `test_atomic_file.py`, `test_herdr_tab_title.py`, `test_pane_record.py`, `test_pane_restore.py`, `test_restart.py`, `test_stop_resume.py`, `test_unit_watchdog.py`.

**Binds later work:** a launched agent gets no identity variable; its identity comes from the native runtime join. Launch titles go in the `NewTab` destination of `herdr.launch_prepared_agent`.

**Gotchas:** `pane_record` must not import `session_staff` (import cycle); `LaunchNames` lives in its own module for that reason. A test that patches `Herdr.retitle` must expect the pane id and the row name.

**Ruled out:** selecting a restore row by its saved name; writing a staff row from the title hook.

### Phase 4m — Port audit: every unit's port is in its `.unit.toml`  · status: done

**Depends on:** herdr-unit's 4h shrink, which writes this Work Order.

#### As-built

- `scripts/production/audit_unit_ports.py` is a read-only audit. For each staff unit row it prints the configuration port, its source path, and the Port cell from the Units row with that document's path. It reads active configuration from the worktree and retired configuration from the parked file, with `cleanup=False`, and remote configuration on the recorded machine. Each result is `AppPort`, `NoAppPort`, `ConfigMissing` or `unavailable: <cause>`; missing configuration never counts as `NoAppPort`. It prints each gap and a `note:` line, supports `--json`, writes nothing, and exits 0 with gaps (tools report anomalies and never block).
- `unit_config` holds the one Port cell parser: `N (note)` is port `N`, and `—` or an empty cell is `NoAppPort`. `production_units.lookup_unit` is the one Units row lookup: it reads the parent's document first, then each showrunner's bound document, with one file cache per run. Its results are `UnitRowFound`, `UnitRowMissing` and `UnitSearchIncomplete`, which keeps each unavailable source. The audit, `staff_sources.py fill` and `add_unit` use both; a new unit never takes another production's port.
- `unit_config.repair_port(target, recorded_old, settled)` is the guarded port repair for `InWorktree` and `Parked` targets. It requires the recorded old value, returns `AlreadyCurrent` for the settled value, refuses any third value and `ConfigMissing`, and publishes through the shared atomic writer under `writer_lock()`. `unit_config.decode` and `unit_config.parked_path(unit, *, home=None)` are public.
- `staff.set_worktree(..., expected=...)` requires the expected old location and compares the full location inside the staff transaction; the settled value returns unchanged. `staff.py set-worktree` takes `--expected-state` with `--expected-machine` and `--expected-path`, and the remote form carries them. Every caller passes the location it observed.
- `staff_sources.py fill` refuses a row on any `UnitSearchIncomplete` result or an unavailable parent document, and names each unread document. Session recovery stays only for `UnitRowMissing`. A refusal is a per-row report; later rows continue.

**Files:**
- `scripts/production/audit_unit_ports.py` — the audit command.
- `scripts/production/unit_config.py` — Port cell parser, `repair_port`, `parked_path`, `decode`.
- `scripts/production/production_units.py` — `lookup_unit` and its result types.
- `scripts/production/staff.py` — the guarded `set_worktree` and its CLI.
- `scripts/production/staff_sources.py`, `add_unit.py` — the shared parser and lookup; the incomplete-search refusal.
- `scripts/production/retire_unit.py`, `unit_move.py`, `production_lifecycle.py`, `rename_unit.py` — pass the observed location to `set_worktree`.
- Tests: `test_audit_unit_ports.py`, `test_unit_config.py`, `test_production_units.py`, `test_staff.py`, `test_staff_sources.py`, `test_add_unit.py`, and the shared test-support helpers.

**Binds later work:** every `staff.set_worktree` caller passes `expected=`; `staff.py set-worktree` without the expected flags refuses. Port cells parse only through `unit_config`, and Units rows resolve only through `production_units.lookup_unit`. Readers read `~/.local/state/staff/staff.json` directly; a format change bumps its version and tells cargo-liner.

**Gotchas:** an incomplete search with one match is not proof of one row. The recovery reader must keep the native timer owner when a document load fails, or the refusal loses the document path. A hand-run `staff.py set-worktree` once wrote a phase worktree into a unit row; the expected-old guard now refuses that form.

**Ruled out:** recovering a unit from a live session when the Units search is incomplete; a second Port parser or Units lookup in any caller; an audit that exits 1 on a gap.

### Phase 3n — Workers on the staff record (call sites)  · status: done

#### As-built

The staff row is the one record of a worker, per the staff-lifecycle spec (`docs/plans/staff-lifecycle.md` in the sdlc tree, user 2026-11-03: "the simplest possible system"). Rosters, the worker ledger, run records, inboxes and design-role records store no thread id, cwd, owner or family.

- `StaffRow.parent` is `ReportsTo(id: StaffId) | StaffRoot`; `StaffSnapshot.parent_of(row)` reads it. Nulls convert only in `StaffRow.to_json` / `from_json`.
- `staff.ensure_worker(identity, name, parent) -> StaffRow` publishes a worker at the `worker_admission.py` boundary (`admit`, `publish`, `WorkerStartAllowed`, `PublishedWorker`). A worker is found by its staff row from any run of its unit director.
- A worker's thread is found from `current_session`; its folder from the staff row's `worktree`. `StaffSnapshot.backends` finds `ClaudeBackend | CodexBackend` from the session that `current_session` finds; nothing stores it.
- `staff_sources.WorkerObservation.identity` is `ClaudeSession | CodexThread | BackendUnavailable`; `live()` keeps schema 3. The staff storage schema stays 2; no row carries a role.
- Adoption is one write of the worker row's `parent`. `staff.retire` ends a worker row.
- `worker_admission.family_switch_context(allowed, family)` reads the prior Codex rollout or Claude transcript of `current_session` when a slot opens in the other family and returns at most 32000 characters of user and assistant messages.
- The staff transaction and operation lock wait through `worker_lifecycle.acquire_flock` with the operation deadline. `config/staff.conf` holds `OPERATION_LOCK_SECONDS=30` and `REMOTE_SECONDS=20`, read at each use through `scripts/lib/conf.py`; each wait uses the smaller of the configured limit and the remaining deadline.
- Unexpected cases print one `anomaly:` line and continue; only the spec's section 4 refusals remain.

**Files:**
- `scripts/production/staff.py` — row types, `ensure_worker`, `retire`, found backends, deadline-bound locks, configured limits.
- `scripts/production/staff_sources.py` — `live()` and worker observations from staff rows.
- `scripts/production/worker_admission.py` — the one admission and publish path; `family_switch_context`.
- `config/staff.conf` — staff time limits.
- `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py`, `scripts/agents/worker_lifecycle.py`, `scripts/direct/workers.py`, `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/direct/remove_workers.py` — call sites read the staff row; no stored thread id or cwd.
- `scripts/astra/design_roles.py`, `scripts/astra/astra_roles.py` — held roles keyed by staff id; busy state read live.
- `scripts/message/roster.py`, `scripts/hierarchies/worker_owners.py`, `scripts/shutdown/inventory.py`, `scripts/shutdown/restart.py`, `scripts/accounts/unit_watchdog.py`, `scripts/lint/tmp_sweep.py` — readers of the staff row.

**Binds later work:** the family switch starts a session of the other family in the same slot and writes `current_session`; `id` and `name` stay, and no field records the family. No code stores a thread id, cwd, owner or family beside the staff row.

**Gotchas:**
- Staff test fixtures set `CLAUDE_CONFIG_DIR` when it is absent; a standalone launch test that needs the private home account passes an empty `CLAUDE_CONFIG_DIR`.
- Sibling-import test files run only through `python -m unittest discover -s <dir> -p <file>`.
- `staff.json` has an outside reader, cargo-liner; a schema change sends it a notice.

**Ruled out:** `WorkerRole` and a row `role` field; a `"backends"` section in `staff.json`; `staff.adopt_worker` and the adoption journal; `repair_roster` and the ledger-to-roster check; per-thread inbox and held-role files; readers of legacy identity records.

### Phase 3o — Transcript handoff when a slot changes family  · status: done

#### As-built

A worker switches between Claude and Codex in the same slot. Its staff `id`, `name`, `kind`, `parent` and worktree stay; only `current_session` changes.

- `worker_admission.switch_worker(allowed, family, ...)` runs the switch under the worker row lock (`staff.row_operation`): it selects the transcript, stops the source through `stop_resume.stop` by staff id, creates the destination through the family launcher, publishes `current_session` with `staff.update_current_session` after the new process proves its native id, then dispatches. A failed start or publication keeps the old `current_session` and cleans up the new session.
- `worker_admission.extract_transcript(row, run, backend) -> FamilyHandoff` is the one extractor for Claude transcripts and Codex rollouts. It keeps assignments, final replies and changed-file paths from transcript evidence, and drops tool output and tool request bodies. It writes the extract to the run directory; the opening prompt names the extract first and the full transcript second.
- Admission uses `NewWorkerSlot | ExistingWorkerSlot`; handoff results are `NoFamilyHandoff | TranscriptHandoff | TranscriptUnavailable(reason)`. Unavailable evidence prints one anomaly line and the switch continues.
- `accounts.py move workers <claudeN|codexN> [--dry-run]` (`accounts.move_workers`) selects workers from staff rows and uses one family check for the dry run and the real move. The dry run changes nothing and prints `not moved: <reason>` or `would stop; restart on <target> stays pending`. A cross-family move stops the worker and prints `<id>: stopped, resume pending on <account>: <reason>` (`move_result_line`). A locked row gives `SessionMoveRefused` for that worker; the loop continues. A completed rerun prints `unchanged`.
- `worker_fill` refuses a Claude resend when the transcript has a torn JSON line and no matching prompt: "Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused".
- `implement.sh` runs worker admission before it records a pass; a refusal prints its message and exits 2.
- The Codex row lock is released before streaming a turn; a sign-in pass stops each app-server once.

**Files:**
- `scripts/production/worker_admission.py` — switch, extractor, admission and handoff states.
- `scripts/production/worker_fill.py` — admission and publication adapter; delivery-evidence check.
- `scripts/accounts/accounts.py` — `move_workers`, `move_result_line`.
- `scripts/agents/codex_mesh.py` — Codex handoff, row lock release before streaming, sign-in server stops.
- `scripts/direct/workers.py` — allocation after a failed switch.
- `scripts/direct/implement.sh` — admission before the pass record.

**Binds later work:** assign.sh is the only start path; the plain launcher is removed. `assign.py` starts only Codex workers until the devtools one-launch-path change lands, so the live Codex-to-Claude and Claude-to-Codex proofs wait for it. `ResumePending` stays an explicit boundary until assign.sh accepts a fixed staff id and a destination account.

**Gotchas:**
- Tests write only to a fixture state folder; a test batch that reached the live `mesh_roster.json` dropped every thread id. Run phase tests through a runner that compares the live roster before and after.
- Changes to codex_mesh roster and pause code often open failures in other codex_mesh paths; run the full `test_codex_mesh.py` on a stable tree.

**Ruled out:** a plain launcher path for Claude workers; the roster adoption path (`adopted_to`, `handed_off`) and its tests.

### Phase 3p — 3o follow-ups: live switch proofs, regression tests, test cut  · status: todo

**Owner:** models-unit. 3o routed these items here under the hard landing rule (repair round 11, 2026-11-04).

**Depends on:** 3o. Only Spec items 10-11 (live proofs) depend on assign.sh starting a Claude worker; today `assign.py:207` refuses any non-Codex worker. sdlc sent that change to devtools-unit (one-launch-path) on 2026-11-04 and tells models-unit when it lands. Spec items 1-9 start at once.

#### Work Order

**Goal:** Finish the 3o proofs and cuts that 3o did not land.

**Spec:**

Milestone 1 (starts at once):

1. F010: define "already stopped" evidence for a rerun after a partial switch. Use explicit confirmed, unresolved and unknown release states. Keep the refusal for an unresolved identity or unknown server state. Test a source release followed by a failed destination creation (Astra review 20261104T173320Z, item 1).
2. F008: regression test for healthy allocation at `scripts/direct/workers.py:484` after a failed switch. The source fix landed in 3o.
3. F035: regression test that a caller acquires the worker row during a live Codex turn (`codex_mesh.py:4644` releases the lock before streaming). The source fix landed in 3o.
4. F012: regression test for the done-record ownership guard in `codex_mesh.py`.
5. F017: in the running regression in `test_codex_mesh.py`, exclude duplicate developer instructions.
6. F016: replace `bool | None` in `worker_admission.switch_worker` (`worker_admission.py:281`) with named states: backend published, dispatch deferred, assignment accepted. Convert stored strings at the roster boundary. Keep the Claude delivery callback; remove the Codex dummy callback (`codex_mesh.py:4262`).
7. F028: move the Python parent out of the worker turn (`worker_fill.py:92`): a Bash parent with a Python coprocess. Cover dispatch, exit status, cancellation, detach and descriptor cleanup.
8. Simplify: F013 (one family comparison), F015 (`end_friend.sh` inline staff lookup), F019 (duplicate filter and STAFF_ID removal in `test_worker_admission.py`), F026 (one admission path), F034 (session equality in `test_implement_launcher_claude_effort.py`).
9. Cut the 3o tests to one test per behavior, with table rows. 3o left tests net +309 lines against 5e72ab674. Give a coverage reason for each removed assertion.

Milestone 2 (after devtools one-launch-path lands, and after Milestone 1 and any 5a launcher change):

10. Prove one real Codex-to-Claude switch through assign.sh, with the worker using the extract (3o Acceptance gate 19).
11. Prove the reverse direction and record both outcomes for sdlc (3o Acceptance gate 20).

**Files:**
- `scripts/production/worker_admission.py`
- `scripts/production/worker_fill.py`
- `scripts/agents/codex_mesh.py`
- `scripts/accounts/accounts.py`
- `scripts/direct/workers.py`
- `scripts/direct/implement.sh`
- `scripts/ask_a_friend/end_friend.sh`
- `scripts/production/test_worker_admission.py`
- `scripts/production/test_worker_fill.py`
- `scripts/agents/test_codex_mesh.py`
- `scripts/accounts/test_moves.py`
- `scripts/accounts/test_accounts.py`
- `scripts/shutdown/test_account_move.py`
- `scripts/shutdown/test_stop_resume.py`
- `scripts/direct/test_workers.py`
- `scripts/direct/test_implement_launcher_claude.py`
- `scripts/direct/test_implement_launcher_claude_effort.py`

**Workers:** `1 writer + 1 tester`

`impl` — Own the source files above (F010, F016, F028 and the simplify items).

`test` — Own the test files above. Start with the regressions (items 2-5), then the test cut (item 9).

**Constraints from prior phases:**

1. Keep fixed staff identities: a move never changes a worker's staff id.
2. Test fixtures write only to a fixture state folder, never the live roster or state folder (3o F040). Run tests through a guarded runner that checks the live roster before and after.
3. `implement.sh` runs worker admission before it records a pass.
4. The plain launcher stays removed. Start workers only through assign.sh (one-door rule, sdlc 2026-11-04).
5. `ResumePending` stays an explicit boundary until assign.sh accepts a fixed staff id and a destination account. Output: "stopped, resume pending on <account>: <reason>".
6. A Claude resend needs readable delivery evidence (`worker_fill.py:66`): keep the matched, absent and unknown outcomes.
7. A `StaffRefused` row gives one refusal line for that worker; healthy workers continue (`accounts.py:2834`).
8. Overlap with 5a: both change `codex_mesh.py`, launcher code and their tests. Serialize the overlapping launcher edits.

**Acceptance gate:**

1. basedpyright zero errors and warnings on changed files and callers.
2. Affected suites green; source net negative.
3. These assertions stay: the dry-run preview lines (`test_moves.py:678`); the `ResumePending` text (`test_moves.py:668`); the locked-worker regression (`test_moves.py:709`); the matched, absent and unknown delivery outcomes (`test_worker_fill.py`); the fresh and attach admission refusals (`test_implement_launcher_claude.py:62`); the live-roster check of the guarded runner.
4. Pass the Delegation Context merge gate.

### Phase 5a — Bash reads the resolved model registry in one call  · status: todo

**Owner:** models-unit (user via natedev and sdlc, 2026-11-03: "going forward herdr should not own changes to /default_models - it should be owned by the models unit under sdlc"). models-unit owns `commands/default_models.md`, `scripts/agents/default_models.sh`, `models_config.sh`, `sync_codex_catalog.sh`, `scripts/lib/models_registry.py` and `config/models.conf`.

**Depends on:** 3o. It does not wait for 3p.

**Order:** after 3o. It runs beside herdr Phase 7 when no file overlaps; it never delays 3n or 3o.

#### Work Order

**Goal:** Read the model registry once for each launch operation. Give every reader parsed values from that same read.

**Spec:**

1. Make `models_registry.py shell` read the registry through `registry_path()`.
2. Return the selected path, exact source text, and parsed section, key, and value arrays from one read.
3. Preserve empty sections, row order, comments, and editor source text.
4. Quote generated Bash values safely.
5. Remove default registry-path selection from `models_config.sh` and `sync_codex_catalog.sh`.
6. Make Bash lookups compare parsed keys; remove raw registry-row parsing.
7. Replace both registry-reading awk parsers in `sync_codex_catalog.sh` with parsed-value reads.
8. Preserve transformations that produce edited text through the existing publication writer.
9. Define a `LaunchModelConfiguration` lifetime of one launch or assignment operation.
10. Share that configuration across implementation, review, allocation, and thread creation within the same operation.
11. Acquire a fresh configuration for the next launch, including the next launch in a long-running process. Within one running launcher, each later submission (continuation, capacity retry, resident wake) also acquires a fresh configuration; the wake test at `test_codex_mesh.py:5164` requires a changed tier there. Today `codex_mesh.py:6181` resolves settings for each submission.
12. Define a `RegistryEditBase` lifetime of one editor operation.
13. Give editor validation and transformation the exact text and values from that same base.
14. Preserve the existing locked delta replay against concurrent edits.
15. Define `unit_effort.py`'s read lifetime as one command invocation.
16. Cover `implement.sh`, `review.sh`, `assign.py`, `workers.py`, `codex_mesh.py`, `unit_effort.py`, and `agent_exec.sh`.
17. Count registry snapshot reads across child processes, not only within one shell.
18. Treat catalog synchronization as a separate edit operation before the launch snapshot.
19. Preserve task resolution, caller-family behavior, overrides, effort validation, and service tiers.
20. Make the retired-name check detect sentence punctuation, including `Use /agent.`.
21. Change publication commit subjects and temporary prefixes from `agents` to `models`.
22. Use semantic types for command configuration, edit bases, and domain absence states.
23. Extend the existing parser and fixture tests; do not create a second parser.

**Files:**
- `scripts/lib/models_registry.py`
- `scripts/agents/models_config.sh`
- `scripts/agents/default_models.sh`
- `scripts/agents/sync_codex_catalog.sh`
- `scripts/agents/agent_exec.sh`
- `scripts/agents/codex_mesh.py`
- `scripts/direct/implement.sh`
- `scripts/direct/review.sh`
- `scripts/direct/assign.py`
- `scripts/direct/workers.py`
- `scripts/production/unit_effort.py`
- `commands/default_models.md`
- `config/models.conf`
- `scripts/lib/test_models_registry.py`
- `scripts/lib/test_models_registry_fixture.py`
- `scripts/lib/test_models_registry.sh`
- `scripts/agents/test_models_config.sh`
- `scripts/agents/test_sync_codex_catalog.sh`
- `scripts/agents/test_models_conf_publish.py`
- `scripts/agents/test_no_old_model_names.py`
- `scripts/agents/test_agent_exec.py`
- `scripts/agents/test_codex_mesh.py`
- `scripts/direct/test_implement_launcher.py`
- `scripts/direct/test_review_launcher.py`
- `scripts/direct/test_assign_launcher.py`
- `scripts/direct/test_assign_launcher_identity.py`
- `scripts/direct/test_assign_launcher_reuse.py`
- `scripts/direct/test_assign_launcher_review.py`
- `scripts/direct/assign_launcher_test_support.py`
- `scripts/direct/test_workers.py`
- `scripts/production/test_unit_effort.py`

**Workers:** `1 writer + 1 tester`

`impl` — Own the source and command files above; the shared interface is in `models_registry.py`.

`test` — Own the listed test files; measure snapshot reads across complete launch paths and child processes.

**Constraints from prior phases:**

1. Follow the Delegation Context and preserve Phase 3o's staff and lifecycle behavior.
2. Start after Phase 3o lands because both phases change shared launch files.
3. Run beside herdr Phase 7 only when file ownership does not overlap.
4. Keep `MODELS_CONFIG_FILE`, `AGENTS_CALLER_FAMILY`, and `AGENTS_CALLER_ASSIGNMENT`.
5. Keep one parser and the existing atomic publication writer.
6. Preserve concurrent row replay, additive catalog changes, and unrelated edits.
7. Add no fallback to retired paths or variable names.
8. Store no persistent configuration cache.
9. Extend existing fixtures and publication tests.
10. Keep staff identity changes outside this phase.
11. Overlap with 3p and devtools one-launch-path: registry and editor work starts at once. Serialize edits to `codex_mesh.py`, `assign.py` and `implement.sh` with 3p and with the devtools launcher change. 3p's live switch proofs run on the combined result.
12. `implement.sh` runs worker admission before it records a pass (3o merge).

**Acceptance gate:**

1. Test default and overridden registry paths through the Python path resolver.
2. Test spaced rows, empty sections, comments, and quoted shell content.
3. Prove that `direct = codex` resolves identically in Python and Bash.
4. Prove one registry snapshot read for each complete launch operation, including allocation and thread creation.
5. Prove that a registry edit during launch cannot mix configuration generations.
6. Prove that the next launch reads the changed registry.
7. Prove that one editor read supplies matching exact text and parsed values.
8. Preserve concurrent publication and additive catalog tests.
9. Prove that `Use /agent.` fails the retired-name check.
10. Assert `models` publication subjects and temporary prefixes.
11. Pass affected Python and shell suites, including `test_implement_launcher_claude_accounts.py`, `test_implement_launcher_claude_follow.py` and `test_implement_launcher_follow.py`.
12. Pass basedpyright with zero errors and warnings on changed Python files and caller modules.
13. Pass the Delegation Context merge gate.
14. Give herdr-unit the checkpoint hash for routed findings F002, F003, F004, F006, and F008.
