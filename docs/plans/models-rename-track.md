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

**Owner:** models-unit, after its Phase 4 and 4m (sdlc, 2026-11-02). `staff.py` and `staff_sources.py` keep one writer at a time: herdr-unit and models-unit settle each window on the berth board.

**Depends on:** 3h, 3l, 3p, 3q (staff storage schema changes with roles; live output remains schema 3), and the models unit's F024 checkpoint 3b67af71 on `origin/natedev-models` (the lock order, `worker_lifecycle.acquire_flock` and the `stop` path): merge it before any edit.

Split from 3l on 2026-10-31; the same user ask and the same sdlc acceptance as 3l.

#### Work Order

Copied from herdr-worker-panes.md Phase 3n at phase start (2026-11-03). Spec item 7 is dropped and item 6 is handler-unit's work: the handler reads `staff.json` directly (Phase 3s review). Deleting `production_tree.py` stays with Phase 4b.

**Goal:** keep Phase 3h's native-id admission and finish worker roles, permanent end, adoption, backend metadata, deadlines and reader conversion. The handler reads who reports to whom from the 3l staff command. A Codex worker is stopped and resumed by the same path as a Claude session, by name or staff id.

**Spec:**

1. **Remaining call sites.** A worker lives as long as its unit director, across runs (user 2026-10-28). Each call site finds a worker through its staff row, also from a later run of the same director; a finished run never makes the row wrong (sdlc ruling, 2026-11-02 08:44). Preserve the existing shared admission in `worker_admission.py`; do not build a second open path. All starters already switch together in 3h. Integrate its `WorkerStartAllowed` and `PublishedWorker` types with the remaining role, adoption and permanent-end work:
   - Codex: open `codex_mesh.open_unit_worker` and `start`; permanent end `codex_mesh end` and `workers.end_unit`; a resumable `stop` keeps the row; adoption `workers.adopt` calls the staff adoption below.
   - Claude workers: their one open point in `implement.sh` / `assign.sh` and their one end point, `remove_workers.py`.
   - Astra roles: `design_roles.open_role` and `design_roles.end`. A showrunner-held Astra role is a worker row under its showrunner (3l seeds and shows it).
   **The recorded role is on the worker's staff row.** `staff.WorkerRole` (new: `impl | test | review | fix | astra-designer | astra-reviewer`). `ensure_worker(native_id: StaffId, name: str, parent: StaffId, role: WorkerRole) -> StaffRow` publishes it at the 3h boundary; each admission point passes the role its producer records today (the board role, the ledger entry's `role`, the design role). The role is set once, like the name: an `ensure_worker` of an existing row with another role raises `StaffRefused` and changes nothing. `StaffRow` gains `role: WorkerRole | NotAWorker`, where the singleton `NotAWorker` is the value of every showrunner and unit row. In `staff.json` only a worker row carries a `"role"` key; `from_json` requires it on a worker row and refuses it on any other row, and the conversion happens only there. The record goes to the next schema (the staff file is schema 2 before this role change; `live()` already emits schema 3). The first seed run after this phase reads the 3q schema file, gives each worker row its role from the same producer records in one transaction and writes the next schema; a worker row with no recorded role in any producer stops that seed with one line naming the row and writes nothing. A second seed run changes nothing. `staff_sources.LiveRole` becomes `WorkerRole | Literal["showrunner", "unit"]`, and `live()` takes a worker's role from its row, so `_role` (`staff_sources.py:206`) goes. No code parses a role from a name: `WorkerName.parse` (`staff.py:63`) checks shape only.
   **Locks (models checkpoint 3b67af71).** The order is "unit ledger, design/pool, membership, lifecycle, server, inbox, roster"; the pool lock goes with the pool in Phase 3p, so the order is "unit ledger, design, membership, ..." from then on. At each admission and retirement point, `ensure_worker` / `retire` run while the run-level membership lock is held, after the lifecycle, server, inbox and roster locks are released, so a bulk stop cannot run between the roster write and the staff row. A staff lock is never held while membership is taken; the lock-order guard in `test_codex_mesh` checks it. Every lock wait uses `worker_lifecycle.acquire_flock` with the operation deadline; `_server_lock(session_dir, *, deadline=NO_LOCK_DEADLINE)` takes it the same way, and in a shutdown an omitted deadline takes the operation deadline (`effective_deadline`). Only `OperationDeadlineExpired` is re-raised; an ordinary socket timeout keeps its recovery.
   **The staff transaction obeys the deadline.** `staff._record_transaction` (`staff.py:532`) takes `staff.json.lock` with a blocking `fcntl.flock` today (:537); it moves to `worker_lifecycle.acquire_flock` with the operation deadline. Every time limit in `staff.py` moves to `config/staff.conf`, read at each use through `tunables.number`, and each wait uses `min(configured limit, remaining operation deadline)`, so a deadline only shortens a wait: `OPERATION_LOCK_SECONDS` (today `OPERATION_LOCK_TIMEOUT_SECONDS = 30.0`, :28) for `staff.operation()`'s wait (`_wait_for_operation_lock`, :281); `OPERATION_POLL_SECONDS` for that wait's poll interval; `REMOTE_SECONDS` for the Mac's ssh call (`_remote`, :439, `timeout=20` today), which also bounds the ssh connect time. Each value has a changed-config test.
   **One adoption operation.** `staff.adopt_worker(worker: StaffId, new_parent: StaffId) -> StaffRow` changes only the worker row's parent and keeps its id and name, in one staff transaction, run under both runs' membership locks (`worker_lifecycle.lock_memberships(source, destination)`) with the lock rules above. `staff.move` keeps refusing a worker (`staff.py:624`). A rerun after the parent changed changes nothing, prints `unchanged` and exits 0.
   **The tunables reader** is `scripts/lib/conf.py` (3q, 3r and 3u read config through it). `STAFF_CONFIG` is the test override for `config/staff.conf`.
   **Semantic types** (`~/.claude/docs/type_design.md`). `StaffRow.parent` becomes `ReportsTo(id: StaffId) | StaffRoot`, where `StaffRoot` means a top showrunner; `ensure` and `move` take that type where they take `StaffId | None` today; `find(parent=)` keeps its `StaffId` filter. `WorkerObservation.identity` becomes `ClaudeSession(session_id) | CodexThread(thread_id) | BackendUnavailable`. Nulls and dicts convert only at the JSON boundary (`StaffRow.to_json` / `from_json`, `live()`); `live()` keeps its existing schema 3 wire fields and meanings; the staff file schema is a separate contract. Pin both decoders with fixtures before the role change.
2. **One record holds a worker's backend identity (sdlc showrunner, 2026-10-31).** The backend identities are one `"backends"` section of `staff.json`, keyed by staff id, beside `"rows"`; they cover unit and showrunner-held workers. One file is one atomic publish: `_record_transaction` writes rows and backends in the one `write_text_atomic`, and `staff.snapshot()` reads both from the one file, so no reader sees a row and a backend from two generations. (Two files beside each other cannot be replaced together; a second file would need a generation protocol for no gain.) The section is written only by the staff worker writer (`ensure_worker`, a backend replace for a replaced thread, and `retire`, in the same staff transaction). It holds backend details only: backend kind and Codex home, keyed by the native row id; no second session or thread id value; no name, parent, kind or role, which stay on the row in `staff.json`. The role migration fills it from proved producer evidence under the 3h publication rules; a second run changes nothing. Missing facts stay listed, never inferred from names. When a reader moves to it, no other live copy of the thread id stays in a roster or ledger; history events may keep theirs. The herdr unit switches all callers (holders listed by the models unit):
   - `staff_sources.live` and the seed: identities from the record.
   - the unit ledger: keeps `replaced` / `ended` events as history only.
   - the run roster (`mesh_roster.json`): staff id, server and run details; no thread id.
   - the held-message inbox (`_pending_for_thread`, one file per thread today; a mismatch drops held messages with no error): one file per staff id.
   - `mesh_retired.json`: removed. A replaced or ended thread is a `replaced` / `ended` history event in the unit ledger (`note_backend` already logs `replaced`), and run end reads the threads to close from those events.
   - detached runs (`mesh_runs.py:155` matches `CODEX_THREAD_ID`): the caller's runtime id selects its row directly; no extra identity copy is stored.
   - the design roles' held threads (`design_roles.py:139-149`, `:1288`): staff id.
   - the shutdown pause records (`shutdown_record.py:486`): staff id.
   - the direct readers of a roster entry's `thread_id`: `roster.py:1279` (worker threads of the roster), `hierarchies/worker_owners.py:498` (it reports a missing identity as a problem) and `shutdown/inventory.py:930` (the paused seats of a shutdown): each uses the native row id as the session or thread identity and reads only backend details from the backend section. `roster.py` is the messaging unit's file and `inventory.py` the shutdown unit's; each agrees its change first.
   **Every consumer moves before any live copy goes.** Before the first roster or ledger copy of a thread id is removed, `grep -rnE "thread_id|CODEX_THREAD_ID|_pending_for_thread|held_thread" scripts --include=*.py` (non-test files) lists every field read and every decoder; each one reads the backend section, or is a history reader that the checkpoint notice names. The copies go in the same landing as the last reader's switch.
   Two copies stay, both Codex's own and outside these scripts: the app-server's thread store, and the `CODEX_THREAD_ID` that Codex sets in its worker's environment. progress_history and usage keep old thread ids as history, not as a live fact.
3. **Busy and idle are read live, never stored:** the server pid and the Astra exec pid. `astra_roles.py:248` reads a stored roster status today; that stops.
4. **Owner fields go.** The rosters and the unit ledger keep thread and run details only. The run `director` file and the Astra record's `manager` stop answering who owns a worker; staff answers it.
5. **One stop and one resume path, per member.** `shutdown.py stop <name|staff id>` and `resume <name|staff id>` (`stop_resume.stop` :97, `resume` :216) take a worker as they take a Claude session. For a Codex worker, stop pauses its thread and resume follows or reopens it on its account. This replaces the seat commands in `scripts/lib/resume_message.py`'s `resume_message` (`implement.sh --to <seat>`). It also replaces `workers.pause_run`, `pause_unit`, `codex_mesh reopen`, and `resume-account-move --to` as public paths. `move_sessions` (:405) and `move` (:463) go through the same per-member path. The split-tab shutdown record and restore are Phase 10's work, not this phase's. The old commands stay as internals only: no user or skill text names them, no second caller reaches them, and a check fails on any new caller. Each stop and each resume is safe to run twice: the rerun changes nothing, keeps the same tab, pane and terminal, and exits 0 (user, 2026-10-31); the proof shows the rerun.
6. **Handler switch.** The handler reads `~/.local/state/staff/staff.json` directly (Phase 3s review, 2026-11-02). That switch is handler-unit's work; this unit changes nothing for it.

**Owners:** `codex_mesh.py`, `worker_lifecycle.py` and `workers.py` are the models unit's: herdr workers write the call sites, and models reviews the diff before the merge. `stop` / `_mark_all_roster_ended` overlaps models' F024, so tell models before that function changes. `stop_resume.py` and `shutdown.py` are the shutdown unit's: shutdown writes its side against the herdr resolver (name or staff id → Claude session | Codex worker | not found). The handler reader and its config are handler-unit's.

**Files:**
- `scripts/production/staff.py` — the deadline, `adopt_worker`, `WorkerRole` and the row's `role`, `ensure_worker(native_id, name, parent, role)`, `ReportsTo | StaffRoot`, the backend-identity record writer, `OPERATION_LOCK_SECONDS` read.
- `scripts/production/staff_sources.py` — `live()` and the seed read the backend-identity record and the row's role; identity types; partial results.
- `config/staff.conf` — new (`OPERATION_LOCK_SECONDS=30`, `OPERATION_POLL_SECONDS`, `REMOTE_SECONDS=20`).
- `scripts/direct/assign.py`, `scripts/direct/implement.sh`, `scripts/direct/remove_workers.py`, `scripts/direct/workers.py` — call sites and readers.
- `scripts/agents/codex_mesh.py`, `scripts/agents/mesh_runs.py` — call sites and thread holders.
- `scripts/astra/design_roles.py`, `scripts/astra/astra_roles.py` — call sites, held threads, live busy state.
- `scripts/message/roster.py`, `scripts/hierarchies/worker_owners.py`, `scripts/shutdown/inventory.py` — `thread_id` readers move to the backend section (messaging and shutdown units agree their parts).
- `scripts/message/test_roster.py`, `scripts/hierarchies/test_worker_owners.py`, `scripts/shutdown/test_inventory.py` — their tests.
- `scripts/production/staff_test_support.py`, `scripts/production/test_staff.py`, `scripts/production/test_staff_operation.py`, `scripts/production/test_staff_sources.py`, `scripts/agents/test_codex_mesh.py`, `scripts/direct/test_workers.py`, `scripts/astra/test_design_roles.py` — fixtures and regressions.
- `scripts/production/worker_admission.py` — integrate 3h permission and publication with role and adoption.
- `scripts/production/session_staff.py` — current identity join; preserve retained cleanup evidence.
- `scripts/agents/worker_lifecycle.py` — read only.
- `scripts/shutdown/stop_resume.py`, `scripts/shutdown/shutdown.py`, `scripts/shutdown/shutdown_record.py` — the shutdown unit's stage-two work; the `cargo-handler` reader and config are handler-unit's.

**Workers:** `1 writer + 1 tester` — stage one, this unit.
- `impl` — source integration: every non-test file above in this repository except the other units' files; hub: `scripts/production/staff.py` (every call site needs its writer API).
- `test` — fixtures and regressions: `staff_test_support.py` and the `test_*.py` files above.
- Stage two is other units' work: the shutdown unit's stop and resume side, and handler-unit's reader switch.

**Constraints from prior phases:** Phase 3h: native row ids, `WorkerStartAllowed` and `PublishedWorker`, with no copied identity. Phase 3q: `scripts/lib/conf.py`. Phase 3l: worker `retire`, `WorkerName.parse` (shape only), `staff.py show --json --live` (schema 3 with `inputs`; staff storage has its own version before this role change), the seed; `staff_sources.WorkerObservation`, `BackendUnavailable`. Phase 3p: the review pool is gone. Delegation Context → One identity join, No hard-coded config, Lifecycle reruns keep the pane.
- Phase 3h review (2026-11-01): item 2's roster, hierarchy and shutdown consumers take the runtime session from `current_session`; the fixed `id` stays the `"backends"` key and the inbox key. `ensure_worker` today is `ensure_worker(name, parent, *, runtime_id)` with `id` from the first runtime id; update every signature and line reference against today's `staff.py`. Keep 3h's admission boundary (`worker_admission.py`); do not reopen starters. Test a Codex thread replacement: worker ownership and held messages unchanged.
- Phase 3s review (2026-11-02): `staff.json` has an outside reader, cargo-liner. The role and backend schema change sends a notice to that reader and adds a representative stored-record fixture to its consumer check. The live-output contract stays separate.
- Phase 3s review (2026-11-02): the handler reads `staff.json` directly and live pane state from herdr commands. No work on staff read speed (user, 2026-11-02: "herdr shouldn't focus on making staff fast - we should just read the staff page directly").

**Acceptance gate:** the item 2 search finds no live reader of a roster or ledger thread id; the affected suites pass; basedpyright 0/0 on changed files and every caller module; a test stops a write between rows and backends and shows that the file holds both old or both new, for admission, a backend replace and a retire; a test holds `staff.json.lock` during an interrupted stop and the stop ends at its deadline with `OperationDeadlineExpired`; a changed `OPERATION_LOCK_SECONDS`, `OPERATION_POLL_SECONDS` and `REMOTE_SECONDS` each take effect with no code change, and a deadline shorter than `REMOTE_SECONDS` ends the ssh call at the deadline while a longer one leaves `REMOTE_SECONDS`; `adopt_worker` stopped after its staff write, then rerun, ends as one clean adoption, and a rerun of a finished one changes nothing and exits 0; a launcher name whose recorded role differs from its name suffix (row `impl2` with role `review`) keeps its recorded role in its row and in `live()`; an `ensure_worker` of that row with another role is refused and changes nothing; a schema test reads a worker row with `"role"` and refuses a worker row without it and a unit row with it; a showrunner-held Astra worker keeps its row and identity; a test of a partial hierarchy shows it as incomplete; a real handler test shows busy become idle after a pid exit; watch tests for a remote owner input and a missing input that appears later; the real handler pane shows every session and worker correct after a real unit move and a real new worker; one real Codex worker is stopped and resumed by name and by staff id, and each rerun changes nothing, keeps the same tab, pane and terminal, and exits 0.

**Acceptance gate (admission and observation):** tests keep the exact exit-2 admission refusal and prove no dispatch on failed publication. Preserve `live()` schema 3 and its partial-result surface; do not use its version as the staff storage version. Keep `skipped N not current`; unresolved current workers stay listed with exit 1. A resumable stop keeps the row. Permanent end removes it only after stopped evidence; adoption keeps the native id.

### Phase 3o — Transcript handoff when a slot changes family  · status: todo

**Owner:** models-unit, the next phase after 3n (sdlc for the user, 2026-11-03). It goes before herdr Phases 7, 8, 9 and 12.

**User words (via sdlc, 2026-11-03):** "yes we need to be able to switch between claude and codex workers - and ideally they can have the same name and occupy the same slot and they can even read the transcript so they can start with the correct information".

**Depends on:** 3n (a slot changes family in both directions; the worker keeps its name and slot; its staff row changes family in place).

#### Work Order

1. **One shared extract tool.** One function reads a Codex rollout file or a Claude transcript. It writes a short extract: each assignment, each final reply, and the files changed. It includes no tool output. One function serves both families; there is no copy for each family.
2. **Opening prompt points at the extract.** When a slot changes family, the new worker's opening prompt names the extract first and the full old transcript second. The worker reads the full transcript only when the extract does not answer its question.
3. **The workers account move calls the switch.** `accounts.py move workers claudeN` uses the 3n family switch and the extract. models-unit changes that caller too.
4. **Live proof after 3n merges.** One real Claude worker takes a slot that a Codex worker held, through `implement.sh`, and starts from the extract. Send sdlc the result.

### Phase 5a — Bash reads the resolved model registry in one call  · status: todo

**Owner:** models-unit (user via natedev and sdlc, 2026-11-03: "going forward herdr should not own changes to /default_models - it should be owned by the models unit under sdlc"). models-unit owns `commands/default_models.md`, `scripts/agents/default_models.sh`, `models_config.sh`, `sync_codex_catalog.sh`, `scripts/lib/models_registry.py` and `config/models.conf`.

**Depends on:** 3o.

**Order:** after 3o. It runs beside herdr Phase 7 when no file overlaps; it never delays 3n or 3o.

**Source:** herdr-unit has no 5a branch; Phase 5 is on main as 731dd21aa. Start from the herdr plan section "Phase 5a — Bash reads resolved registry values from one call", the Phase 5 review findings F002, F003, F004, F006 and F008, and three architect items: cover every launch reader (review.sh, assign.py, codex_mesh.py, unit_effort.py, agent_exec.sh) with a defined snapshot lifetime; one read returns the text and the parsed values for editors; add test_models_registry_fixture.py and test_models_conf_publish.py to the test files. herdr-unit closes its routed findings when it gets the 5a checkpoint hash.
