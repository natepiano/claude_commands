# models-unit: rename-track phases from herdr-unit's plan

> **Production:** sdlc
> **Source:** `docs/plans/herdr-worker-panes.md` on `natedev-herdr` (herdr-unit). sdlc moved these three items to models-unit on 2026-11-02 08:2x EST, on the user's ask to make rename land sooner.

## Delegation Context

- **Project:** `~/.claude` scripts — the agent tooling.
- **Project started:** 2026-11-02T13:25:43.957+00:00
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, `unittest`, basedpyright; taplo 0.10.
- **Source plan:** read its Delegation Context in `docs/plans/herdr-worker-panes.md` in this checkout, mainly **Staff design (2026-10-31)**, **One identity join**, **No hard-coded config**, **Rerun-safe tools**, **One file writer** and **Isolation gate**. They apply to every phase here unchanged.
- **Files this unit holds:** none beyond each phase's Files list. The 3k and Phase 4 hold on `staff_sources.py` and `staff_test_support.py` ended with those phases; devtools 3t edits `staff_test_support.py` (2026-11-05).
- **Landing:** each phase lands alone through its own gate.
- **Order (sdlc, 2026-11-02):** Phase 4 is on the rename path (devtools-unit's 3t waits for it); 3k is needed only before herdr 4c. When herdr-unit's 4h merges into `sdlc`, Phase 4 goes ahead of whatever is left of 3k.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>`, run from `~/.claude` main's copy after a merge of `origin/sdlc`.
- **Lint:** `basedpyright <changed Python files> <every caller module of a changed file>`; pass is `0 errors, 0 warnings`.
- **The four sdlc lines (2026-10-30):** good engineering first; no copies; when many things break the same way, the copy is the cause; fix at the cause.
- **No unasked guards (user via sdlc, 2026-11-05):** add no guard, refusal or check that the Work Order did not ask for. A fix for a broken guard deletes the guard. A new check goes to the user first; only the user approves it (user via sdlc, 09:17 EST: "if you need to add new checks you need to run them by me"). Each one reaches the user as a new question through `/unit:question`, with a simple statement of why it is important (user via sdlc, 09:20 EST). A review finding that asks for a new check is dropped unless the Work Order asks for it or the user approves it.

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

### Phase 3p — 3o follow-ups: live switch proofs, regression tests, test cut  · status: done

#### As-built

- Source release before a family switch has three outcomes: `SourceReleased`, `SourceUnresolved` and `SourceReleaseUnknown`. Unknown server state refuses the switch. A valid record with only `previous_servers` means no current server; a current server without a valid pid is `SourceReleaseUnknown`.
- A switch records its intent before it creates the destination. The default retry request is `sha256(f"{staff id}:{destination family}")`, so a rerun after publication reuses the published destination.
- `SwitchState` has named values (backend published, dispatch deferred, assignment accepted). The roster boundary converts stored strings. Since 3q an unknown stored value prints one anomaly line and reads as no record.
- New-slot admission locks by parent and name.
- `agent_bg.sh` is a Bash parent with a Python coprocess. The Python parent is not in the worker turn.
- The friend stop records "ended" only after confirmed absence or a successful stop. Since 3q, `worker_fill.py end-friend` does this; `end_friend.sh` is deleted.
- `workers.py` reports a refused candidate and continues with healthy workers.

**Files:**
- `scripts/production/worker_admission.py` — admission, switch, source release, retry request key.
- `scripts/production/worker_fill.py` — Claude coprocess admission and dispatch.
- `scripts/agents/agent_bg.sh` — Bash parent of the worker turn.
- `scripts/agents/codex_mesh.py` — Codex start path and source-release evidence readers.
- `scripts/ask_a_friend/end_friend.sh` — friend stop and status (deleted in 3q).

**Binds later work:** the real-path regression tests `test_default_switch_request_reuses_the_published_destination_after_fresh_admission` and `test_source_release_requires_identity_and_complete_server_evidence` stay, apart from renames. `codex_mesh.py` stops on `SourceReleaseUnknown` and continues after `SourceUnresolved`.

**Gotchas:**
- Gate exports need their own `.git` (`git init`, add, commit), or copy-guard, offload and inventory tests fail.
- The fake host in tests needs its `HERDR_PROC` folder; the sdlc stop path reads it.
- Regression tests run the real reader and function. A test that stubs the function under test passes while the defect stays.

**Ruled out:**
- Merging `SourceUnresolved` and `SourceReleaseUnknown`: their consumers differ.
- Moving launch account setup in `agent_bg.sh`: it adds work before the `unchanged` exit.

### Phase 3q — 3p follow-ups: simplify cuts and the switch-state cleanup  · status: done

#### As-built

- `worker_admission.SwitchState` has plain names: `NO_RECORD`, `CREATION_INTENT_RECORDED`, `DISPATCH_IN_FLIGHT`, `ASSIGNMENT_ACCEPTED`. `DispatchOwner.TURN_SUBMISSION` names "turn submission owns dispatch". `PublishedWorker` has no `state` field; tests read the stored `DispatchRecord`.
- `record_dispatch` checks the expected request inside its lock. `dispatch_record` reads an unknown stored state as `NO_RECORD` and prints one `anomaly: <name>: unknown dispatch_state <repr>; using NO_RECORD` line. `advance_dispatch` is deleted.
- The transcript reader returns a named `TornTranscriptRecord`, not `None`.
- `worker_fill.py end-friend` stops a friend in process and writes `ended` only after a confirmed stop; a failed stop leaves the status for a retry. `end_friend.sh` is deleted; `/ask_a_friend` calls the verb.
- `stop_resume.release_source(row, runs, backend)` owns source release and its refusal reasons; `stop_results.py` holds the reason texts. Each refusal line gives the reason and `; inspect the source session and retry`.
- `staff.backend` gives one backend lookup, with the named `BackendUnavailable` state. Worker allocation in `workers.py` reads one staff snapshot and one role log.
- `UnitWorkerLedger.locked()` keeps an optional `membership` at its public boundary; its docstring states the policy.
- Source is net -59 lines against `67b2850c5`, so 3p and 3q together are net negative.

**Files:**
- `scripts/production/worker_admission.py` — switch states, dispatch record, transcript reader.
- `scripts/production/worker_fill.py` — Claude admission and the `end-friend` verb.
- `scripts/production/staff.py` — backend lookup and `BackendUnavailable`.
- `scripts/agents/codex_mesh.py`, `scripts/agents/agent_bg.sh`, `scripts/direct/workers.py`, `scripts/accounts/accounts.py` — callers of the cleaned switch path.
- `scripts/shutdown/stop_resume.py`, `scripts/shutdown/stop_results.py` — source release and its reasons (shutdown-unit's files).
- `commands/ask_a_friend.md` — calls `worker_fill.py end-friend`.

**Binds later work:** phase 3r removed the `role` parameter of `worker_admission.admit` at every caller and the `AGENT_BG_ROLE` assignments. `release_source` and `ReleaseSource` stay as built beside shutdown-unit's `stop(session, *, deadline=math.inf)`. The tests `test_claude_retry_reports_one_unknown_dispatch_state_and_completes_dispatch`, `test_worker_release_refusals_name_the_reason_and_next_action` and the `test_end_friend.py` stop and retry tests own the states above.

**Gotchas:**
- A ruling must be in the worker prompt. A queued codex_mesh message loses to the prompt's own text.
- `test_times/test_show.py` reads the checkout's origin remote and fails in a gate export that has no origin.

**Ruled out:**
- A sixth staff refusal for an unknown dispatch state: the staff spec allows five; the anomaly line continues the operation.
- Changing six other units' test files to remove `membership` from `UnitWorkerLedger.locked()`.

### Phase 3s — A moved session resumes only its own workers  · status: done

#### As-built

- A session move lists only the moved row's staff children: the worker rows whose `parent` is the moved row. Each child gives one `WorkerResume` result, keyed by its staff id: `Resumable` (its resume command from `resume_message.resume_worker_command`, unchanged), `Idle` ("idle: nothing to run now"), or `Unavailable` (one `anomaly:` line with the child and the reason). `PausedSeat`, `Resumable`, `Idle`, `Unavailable` and `WorkerResume` live in `scripts/lib/resume_message.py`; `ResumeWorkers.results` matches on them.
- `inventory.worker_resumes(parent, membership)` builds the list. It reads `agents_log.assignments` once, uses one clock reading, and takes the newest record pair from `session_staff.worker_run_entries`. Status comes from `worker_state.worker_status_path(directory, slot)` when the record has a slot, else from the roster entry. A slot record is live at `implementing`; a roster record is live at a `ROSTER_LIVE` status or a paused turn.
- `session_staff._worker_runs` is one shared scan of the owners' marker runs and `unit_runs.run_folders()`, in mtime order. In each run, `impl_worker_*.json` records come first and roster entries last, so the roster record wins. `worker_run_entries` returns real records only; `worker_run_index` returns record runs and owner-marker runs, so `release_source` finds a loaded source thread through the owner marker.
- `unit_runs.run_folders(root_override=None)` lists run folders in root order and name order, skips `root / "active"`, removes duplicate resolved paths and skips an unreadable root. `held_by_session` and the run index use it.
- `scripts/direct/worker_state.py` holds `ROSTER_LIVE`, `worker_status_paths(run, pattern)` (run root, then `phases/*`) and `worker_status_path(run, slot)`; `assign.py`, `implement.sh`, `direct_run.py` and the inventory use them.
- `worker_admission.admit` has no `role` parameter; the `AGENT_BG_ROLE` assignments and the prime `--role` option are gone.

**Files:**
- `scripts/shutdown/inventory.py` — `worker_resumes`, the move's worker list.
- `scripts/shutdown/stop_resume.py` — the move message's workers line.
- `scripts/lib/resume_message.py` — the `WorkerResume` result types.
- `scripts/production/session_staff.py` — the shared worker run scan.
- `scripts/lib/unit_runs.py` — `run_folders`.
- `scripts/direct/worker_state.py` — the slot status resolver.
- `scripts/production/worker_admission.py` — `admit` without `role`.

**Binds later work:** the live family-switch proofs merge the devtools launcher revision onto the 3s forms: `admit` takes no `role`, slot status comes from `worker_status_path`, the roster record wins over a slot record, and `worker_run_index` keeps marker-only runs.

**Gotchas:**
- A Claude worker has no mesh roster entry; its records are `impl_worker_<slot>.json` and the slot status file.
- A read of `mesh_roster.json` under its shared lock creates the lock file; scan run folders only, never the `active` marker folder.

**Ruled out:** a test that counts agents-log reads — it tests the implementation, not the behavior.

### Phase 3r — Live family-switch proofs through assign.sh  · status: todo

**Owner:** models-unit. 3p Milestone 2 (Spec items 10-11) moved here so that 3p could land Milestone 1.

**Depends on:** 3p, 3q, 5a, and the devtools one-launch-path change (assign.sh starts a Claude worker). sdlc tells models-unit when it lands. The proofs run on the final launcher code; a later launcher edit repeats the affected proof.

**Order (sdlc, 2026-11-05):** "Start 3r item 7 now. Run the live switch proofs after I tell you the devtools main hash." Devtools hash 37faa1a05 removes the Codex-only exit at `assign.py:206`.

#### Work Order

**Goal:** Prove a real worker family switch in both directions through assign.sh.

**Spec:**

1. Make a throwaway fixture unit with its own state folder. Start a Codex worker in it with assign.sh.
2. Switch that worker to Claude with assign.sh. Write the exact command at 3r start from the devtools one-launch-path interface as it landed.
3. Assign the same Claude worker again with assign.sh. Record that its destination identity does not change. Then switch it back to Codex with assign.sh, and assign it again the same way.
4. For each switch, save the staff row before and after, the transcript extract path, and the destination's first-turn log. Put the evidence under `${PHASE_DIR}/proofs/`.
5. Stop the worker and remove the fixture unit after the proofs.
6. A switch defect the proofs find is fixed at its cause, with a real-path regression test. The director assigns that repair.
7. Done in the 3s checkpoint df2a08345: `worker_admission.admit` has no `role` parameter (`worker_admission.py:167`), and the `AGENT_BG_ROLE` assignments are gone. Ledger 3q F027 and F028.

**Files:**
- `scripts/production/worker_admission.py`
- `scripts/production/worker_fill.py`
- `scripts/agents/agent_bg.sh`
- `scripts/agents/codex_mesh.py`
- `scripts/production/test_worker_admission.py`
- `scripts/agents/test_agent_bg.py`
- `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/astra/design_roles.py`, `scripts/ask_a_friend/launch_friend.sh`, `scripts/direct/workers.py` — the launch path the proofs drive
- `scripts/agents/test_codex_mesh.py`, `scripts/production/test_worker_fill.py`, `scripts/production/staff_test_support.py` — regression tests for a switch defect

**Workers:** `1 writer + 1 tester`

- `impl` — Any switch defect the director assigns: the source repair only.
- `test` — Run both live switches through assign.sh on a fixture unit, record the evidence paths, and write the regression tests and fixtures for each defect. Hand any defect to the director.

**Constraints from prior phases:**

1. 3q lands first; both phases edit the switch code.
2. Live proofs use a throwaway fixture unit and never the live roster of a real unit.
3. 5a: `codex_mesh.py` launch functions take a `models_registry.LaunchModelConfiguration` (`codex_mesh.py:6802`), and each later submission reads a fresh one. A fresh read runs catalog sync first; an inherited snapshot does not. Agent starts remove the operation variables after the environment loads. A switch repair keeps these, and the 5a lifetime and environment tests stay green.
4. 3s: `admit` takes no `role` argument, and slot status comes from `worker_state.worker_status_path` (run root, then `phases/*`). The devtools revision 37faa1a05 still passes `role` to `admit` (`assign.py:831-834`) and reads the root-only status path (`assign.py:540`). At the merge that brings it in, keep the 3s forms: no `role`, `worker_status_path`, roster record over slot record, and marker-only runs in `session_staff.worker_run_index`. The 3s suites (`test_session_staff.py`, `test_inventory.py`, `test_unit_runs.py`, `test_worker_state.py`) pass on the merged tree before the proofs.

**Acceptance gate:**

1. Both switches keep the worker's fixed staff id and open no second destination.
2. Each destination reads the extract before its first assignment.
3. `test_worker_admission.py`, `test_worker_fill.py`, `test_codex_mesh.py`, `test_agent_bg.py` and the 3s suites pass; basedpyright 0/0 on changed files and caller modules. `test_default_switch_request_reuses_the_published_destination_after_fresh_admission` and `test_claude_retry_reports_one_unknown_dispatch_state_and_completes_dispatch` stay.
4. Both outcomes go to sdlc with the evidence paths.

### Phase 5a — Bash reads the resolved model registry in one call  · status: done

#### As-built

- `scripts/lib/models_registry.py` is the one registry parser. It reads the registry once for each operation: `launch_configuration` gives a `LaunchModelConfiguration` for one launch or assignment, and `edit_base` gives a `RegistryEditBase` for one editor operation. The path comes from `registry_path()`; an explicit `--registry` argument wins over `MODELS_CONFIG_FILE`.
- A fresh read runs catalog sync first: `_snapshot(..., synchronize=True)` calls `_sync_catalog` only in its fresh branch. A child process inherits the encoded snapshot from `MODELS_LAUNCH_CONFIGURATION` or `MODELS_REGISTRY_EDIT_BASE` and never syncs. A sync failure prints `WARNING: Codex catalog sync failed; using the current model registry.` to stderr and continues.
- `models_registry.py shell` emits quoted Bash arrays of section, key and value from one read, with the exact source text, empty sections, row order and comments. Bash lookups compare parsed keys. `models_config.sh` and `sync_codex_catalog.sh` have no own path selection and no awk parser.
- `codex_mesh.py`, `workers.py`, `assign.py`, `implement.sh`, `review.sh`, `unit_effort.py` and the `progress_history.py` configuration digest take one configuration per operation. Each later submission of one launcher (continuation, capacity retry, resident wake) reads a fresh configuration.
- Every agent start ends the operation: `models_registry.agent_environment` (Python) and one `models_config.sh` function (shell) remove both operation variables.
- Publication commit subjects and temporary prefixes use `models`. The retired-name check catches sentence punctuation, such as `Use /agent.`.

**Files:**
- `scripts/lib/models_registry.py` — the parser, operation snapshots, the catalog sync trigger and `agent_environment`.
- `scripts/agents/models_config.sh` — Bash reads through the parsed arrays; the operation strip function.
- `scripts/agents/sync_codex_catalog.sh` — parses under the `sync.lock` folder and publishes through `store_registry`.
- `scripts/agents/codex_mesh.py`, `scripts/direct/workers.py`, `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/direct/review.sh`, `scripts/production/unit_effort.py`, `scripts/direct/progress_history.py` — one configuration per operation.
- `scripts/lib/staff.py`, `scripts/accounts/accounts.py`, `relaunch.py`, `send.py`, `berth_fix/launch.sh`, `nix_env.py`, `cli_agent.sh`, `agent_exec.sh`, `agent_bg.sh`, `notify/review.py` — agent starts that remove the operation variables.
- `scripts/lib/test_models_registry_fixture.py` — the sync-order test (fresh, stale, inherited and current state).

**Binds later work:** `codex_mesh.py`, `implement.sh`, `workers.py` and `assign.py` read the registry through `models_registry.launch_configuration`; keep one configuration per launch. A set `MODELS_REGISTRY_EDIT_BASE` means no sync, because the sync lives in the fresh branch of `_snapshot` (`models_registry.py:70`). Agent starts in `resume_message.py` and `stop_resume.py` keep `agent_environment`. The repair-sensitive tests for `workers.resolve` reuse, the `default_models.sh` write path and the `sync.lock` folder belong to the 5a follow-ups phase.

**Gotchas:**
- `nix_env.exec` restores environment variables in `ensure()`, so the strip runs after it.
- A test that makes fresh reads needs temporary catalog state and source files with fixed `os.utime` values, with the state newer than the sources. Otherwise every fresh read starts a live sync.
- Isolated test copies take the whole `scripts/lib` folder with `shutil.copytree`, not a list of file names.
- A merge that takes one side of a whole file drops this phase's fixture; use `git merge-file --theirs` for the conflict hunk only.

### Phase 5b — 5a follow-ups: tests that fail without the simplify repairs  · status: todo

**Owner:** models-unit. 5a routed findings F005, F007 and F009 here at repair round 8 under the hard landing (2026-11-05). The 5a repairs landed; only the tests that fail without them are missing.

**Depends on:** 5a.

**Order (models-unit, 2026-11-05):** starts after df2a08345, which landed 3r item 7; it does not wait for the 3r live proofs.

#### Work Order

**Goal:** Each 5a simplify repair has a test that fails when the repair is reverted.

**Spec:**

1. F005: A test proves that worker reuse reads the registry zero times. `workers.resolve` returns a reused worker at `workers.py:519`, before it acquires a configuration at `workers.py:542`. Test the reuse path with no inherited snapshot, so a read moved before line 519 fails the test.
2. F007: The registry-read count test drives the `default_models.sh` write path (not `--check`) with `MODELS_REGISTRY_EDIT_BASE` set and stale catalog sources. It proves no catalog sync runs inside an edit. The sensitivity revert targets the inherited-snapshot branch of `models_registry._snapshot` (`models_registry.py:70`); `models_config.sh` has no sync gate now.
3. F009: A test proves that `sync_codex_catalog.sh` parses the registry only after it holds the catalog synchronization lock (the `sync.lock` folder, `sync_codex_catalog.sh:64`). Run it in write mode: `--check` takes no synchronization lock.
4. A test proves that a failed catalog sync prints `WARNING: Codex catalog sync failed; using the current model registry.` to stderr and returns the current configuration (`models_registry.py:108`). The fixture sync script exits nonzero.
5. Revert each repair in a scratch copy and record that its test fails. Restore the repair.

**Files:**
- `scripts/agents/test_codex_mesh.py`
- `scripts/direct/test_workers.py`
- `scripts/agents/test_models_config.sh`
- `scripts/agents/test_sync_codex_catalog.sh`
- `scripts/lib/test_models_registry_fixture.py`

**Workers:** `1 tester`

**Constraints from prior phases:**

1. 5a: a fresh `launch_configuration` or `edit_base` read runs catalog sync first; an inherited snapshot never does. Tests that make fresh reads set `CODEX_CATALOG_SYNC_STATE_FILE`, `CODEX_CONFIG_FILE` and `CODEX_MODELS_CACHE_FILE` to temp paths with fixed `os.utime` values, as `test_codex_mesh.py` `setUpModule` does.

**Acceptance gate:**

1. Each new test from items 1-3 passes on the 5a tree and fails with its repair reverted. The summary quotes each failing assertion.
2. The item 4 test passes and covers the warning text and the returned configuration.
3. Pass the five affected suites.
4. Pass the Delegation Context merge gate.
