# Long-lived workers and one worker tool

> **Status: IMPLEMENTATION PLAN — phased, direct-ready; approved by the user 2026-10-28 (relayed by the showrunner).** One launcher, `assign.sh`, opens and resumes long-lived workers that belong to the unit. Tools refuse worker work that a unit director does any other way.

> **As-built disposition: create** — `docs/as-built/workers.md`; it replaces `docs/as-built/long-lived-workers.md` and `docs/as-built/review-pool.md` (Phase 15).

> **Production: build-followups** — unit `models-unit`; production doc `docs/plans/build-followups-production.md`

> **Sources:** `~/.local/state/plan-backlog/2026-10-28-worker-tool.md` (WT) and `~/.local/state/plan-backlog/2026-10-27-long-lived-worker-seats.md` (WS). Each phase title names the source phases it builds.

## Delegation Context

- **Project:** `~/.claude` — the user's Claude Code configuration. This plan replaces `implement.sh` and `review.sh` with `assign.sh`, moves workers from the run to the unit, and adds hooks and checks that refuse work done outside workers.
- **Project started:** 2026-10-28T14:03:17.418+00:00
- **Stack:** Python 3.13, standard library only, run through `"$HOME/.claude/scripts/lib/py"`; bash launchers; Markdown commands and docs; `settings.json`. The cargo handler is a separate Rust repository (`~/rust/cargo-liner-handler`, package `cargo-handler`).
- **Layout:**
  - `scripts/direct/` — launchers, review pool, run state; new `assign.sh`, `workers.py`, `worker_provenance.py`
  - `scripts/agents/` — `codex_mesh.py`, `agents_config.sh`, `sync_codex_catalog.sh`, `agent_bg.sh`, `agent_admin.sh`
  - `scripts/hooks/` — hooks; new `pre-tool-use-worker-gate.py`
  - `scripts/production/` — `merge_gate.py`, `retire_unit.py`, `transfer_unit.py`, `rename_unit.py`, `merge_checkpoint.py`
  - `scripts/whoami/quota_alert.py`, `scripts/usage/weights.py`
  - `config/agents.conf`, `settings.json`
  - `commands/unit/`, `commands/showrunner/`, `commands/agent.md`, `commands/plan/phase_review.md`, `docs/direct/`, `docs/production_format.md`, `docs/as-built/`
  - `scripts/delegate/` holds only a stale `__pycache__`; the real scripts are in `scripts/direct/`.
- **Key files:**
  - `settings.json` — SessionStart `compact` hook at line 344 (line 353 on `main`)
  - `scripts/hooks/session-start-direct-resume.py` — the resume hook the compact entry must name
  - `scripts/direct/implement.sh` — writer launcher; claim code `worker_record` at ~217–396; `agents_resolve` at 203–204; positionals at 85–90
  - `scripts/direct/review.sh` — review launcher; `claim_follow` at ~284–349; turn record at ~454–489; tree guard at ~805–820
  - `scripts/direct/review_pool.py` — pool: `_prime`, `_choose`, `_repoint`, `allocate` (~536–599), `release`, `retire`, `migrate`, `stop`, `rename_unit`; `main` at 788
  - `scripts/direct/review_tree.py`, `scripts/direct/review_pool_report.py`
  - `scripts/agents/codex_mesh.py` — `WORKER_ROLES` at 382 (used at 636, 996, 7323, 7355, 7411); `command_reopen`, `_finished_run`, `prime`, `fork`, `set-instructions`, `follow`, `stop --pause`
  - `scripts/agents/agents_config.sh` — `_agents_validate_pair` at 260, `agents_resolve` at 291, `agents_resolve_print` at 452
  - `scripts/agents/sync_codex_catalog.sh` — writes `[codex.agents]`; incomplete-cache rule at ~261–279
  - `scripts/agents/agent_admin.sh` — backend of `/agent`; `commands/agent.md`
  - `config/agents.conf` — `[codex.agents]`, `[claude.agents]`, `[direct.codex]`; the overlay `config/agents.local.conf` is gitignored and exists only in `~/.claude`
  - `scripts/agents/agent_bg.sh` — named background Claude sessions
  - `scripts/direct/progress_history.py`, `end_session.sh`, `remove_workers.py`, `prepare_session.sh`, `phase_reservation.py`
  - `scripts/production/retire_unit.py`, `transfer_unit.py`, `rename_unit.py`, `merge_checkpoint.py`, `merge_gate.py` (`TEST_SUITES` at 47)
  - `scripts/hooks/pre-tool-use-change-review-gate.py`, `scripts/production/change_review.py` — the checkpoint commit hook
  - `scripts/whoami/quota_alert.py` — `routed to claude` routing; `moves.RoutedToClaude`
  - `scripts/guilds/roles.py`, `scripts/astra/astra_review.py`, `scripts/agents/mesh_runs.py`, `scripts/message/send.py`
  - `commands/unit/direct.md` — `<TagReferenceContract>` at 141–177, `<LongLivedWorkers>` at 179–216, `<WorkerLaunchFailure/>`
  - `docs/direct/launch_implementation.md` (step 5), `docs/direct/dual_review.md` (step 3), `docs/direct/phase_end.md`, `commands/plan/phase_review.md`, `docs/production_format.md`, `commands/showrunner/produce.md`
  - `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/mod.rs` — `HierarchyRole` at 191, `text` at 206–218, `RoleFallback` at 299
- **Test lanes:** `scripts/direct/test_*.py` (suite `direct`); `scripts/agents/test_*.py` plus `test_agents_config.sh`, `test_sync_codex_catalog.sh`, `test_launcher_tasks.sh`; `scripts/hooks/test_*.py`; `scripts/production/test_*.py`; `scripts/whoami/test_quota_alert.py`; `scripts/guilds/test_roles.py`; cargo handler: its crate tests. Docs and commands have no lane until Phase 12 adds the tag-reference test.
- **Build:** none
- **Test:** `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only` from the repository root; shell tests run directly (`bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_sync_codex_catalog.sh`, `bash scripts/agents/test_launcher_tasks.sh`); cargo handler: `bash ~/.claude/scripts/direct/verify.sh test cargo-handler`
- **Lint:** `basedpyright scripts/direct scripts/agents scripts/hooks scripts/production scripts/whoami` — pass is its `0 errors, 0 warnings` line; cargo handler: `bash ~/.claude/scripts/direct/verify.sh lint cargo-handler`
- **Invariants:**
  - A `settings.json` change needs the user's go, typed in the models session (the user's settings rule). The checkpoint body names each `settings.json` change, so the showrunner gates it with the new blob.
  - Standard library only. basedpyright 0/0, no `Any`, no file-level ignores (the user's Python rule).
  - `assign.sh` takes named options only.
  - The command line word is `implement`; the stored roster word and the handler label stay `impl` (user, 2026-10-28).
  - `review`, `research` and `propose` workers are read-only. `propose` may write only in `~/.local/state/plan-backlog/`.
  - `~/.local/state/plan-backlog/ledger.md` has one writer: the launcher.
  - A live worker keeps its model slug; a new worker takes the newest.
  - The cutover runs one unit at a time, with no shims (user, 2026-10-28).
  - The worker gate goes on only after self-heal exists, so it never refuses a director that has no worker it can start (WS).
- **Naming standard** (user-approved in the sdlc review, 2026-10-28; relayed by roles-unit). All remaining phases follow these rules. (1) "Seat" is retired as a name for a Codex thread; use "worker". A worker is a Codex thread that a unit director sends work to. Its full name (for example `claude-build-followups-sdlc-roles-impl`) is the worker name; `impl`, `test`, `fix2` and `review` are its roles in the unit. (2) Do not use "delegate", "seat", "helper" or "subagent" for a worker. A "small-fix helper" is a helper that a showrunner sends; it does no code work. (3) Name reviewers and designers by their job: "design reviewer", "code reviewer", "UI reviewer", "designer". (4) A session's name and its role are separate. Store the role apart from the name; never derive a role from a name. (5) In code, change prose, comments, help, printed labels and messages. Keep a stored key, flag, file name or transport field only where existing data or callers still read it, and note why next to it. A tests-only gate check fails on a new use of "seat" or "seats" as a role word. Every new name in this plan uses "worker". (6) Every name shown to a reader carries production · unit · job, for example "sdlc · roles · code reviewer 6": in reports, messages and Codex thread names. The worker address (its full name) does not change. (User decision via the sdlc showrunner, 2026-10-28.)

## Phases

### Phase 1 — Compaction hook path (WS P0)  · status: done

#### As-built

- The SessionStart `compact` hook in `settings.json` runs `scripts/hooks/session-start-direct-resume.py`.
- Before the fix, the hook named `scripts/hooks/session-start-delegate-resume.py`. The delegate -> direct rename of 2026-10-23 removed that file, so a compacted director got no resume context.
- A compacted director session gets the direct-resume context again.
- No hook-path test shipped. `settings.json` is the only changed file.

**Files:**
- `settings.json` — the SessionStart `compact` hook command names `session-start-direct-resume.py`

**Gotchas:** no test checks that a hook `command` in `settings.json` names a file that exists. A rename under `scripts/hooks/` must update `settings.json` in the same commit.

### Phase 2 — Short model names (WT P0)  · status: done

#### As-built

- An `agents.conf` row can name a model family word (`sol`, `astra`, `opus`, …) or a full slug. A full slug resolves and is checked as before.
- `sync_codex_catalog.sh` generates `[codex.families]` beside `[codex.agents]`. A Codex word maps to the newest visible (`visibility: list`) slug in `~/.codex/models_cache.json` that has a `[codex.agents]` effort row. The highest version wins; equal versions break by slug. The sync compares sorted catalog rows of both generated sections, not file bytes.
- `[claude.families]` is hand-kept: `fable`, `opus`, `sonnet`, `haiku`, each the alias the Claude CLI resolves itself. `[claude.agents]` has `haiku` with five efforts.
- `_agents_model_slug` in `agents_config.sh` resolves a word. `_agents_validate_pair` checks the effort against the resolved slug's row. `agents_resolve` returns the slug.
- `agent_admin.sh` and the status output print `word=<family>`. An unresolved word prints `<word> (unresolved: <reason>)`, and the listing continues.

**Files:**
- `scripts/agents/agents_config.sh` — `_agents_model_slug`, family lookup, failure line, word field in resolved and status lines
- `scripts/agents/sync_codex_catalog.sh` — `[codex.families]` generation, catalog-row compare
- `scripts/agents/agent_admin.sh`, `commands/agent.md` — family words in usage and docs
- `config/agents.conf` — `[codex.families]`, `[claude.families]`, `haiku`, `[direct.codex]` rows naming `sol`
- `scripts/agents/test_agents_config.sh`, `scripts/agents/test_sync_codex_catalog.sh` — family resolution and regression tests

**Binds later work:** `[direct.codex]` rows are family words. `agents_resolve` returns the slug, or exits 1 with `no Codex model for '<word>' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug`. A resolved line ends with ` word=<family word>`. `astra` resolves through `[codex.families]`. `gpt-6.1-terra` and `gpt-6-luna` have no `weights.py` row, so no row may name `terra` or `luna`.

**Gotchas:** a newest release without reasoning levels never becomes a family target. A turn on terra or luna raises `UnweightedModel` in usage accounting.

**Ruled out:** copying sol/astra weights for terra/luna (unsourced numbers feed accounting).

### Phase 3 — `follow` CPU: event waits, no idle wake-ups (WS P2, part 1, CPU half)  · status: done

#### As-built

- `codex_mesh.py follow` blocks on the app-server socket plus file watches, with a 60 s safety check (`UNSEEN_END_CHECK_SECS`). It does not wake each second. `RESIDENT_POLL_SECS` is removed.
- `FollowEvents` holds one follower's watches: inotify on Linux (`LinuxFileChanges`), kqueue on macOS (`KqueueFileChanges`). It watches the roster, the server file, the plan-history state file, `<name>.pending.json`, `<name>.end`, `<name>.account-move.json`, the accounts config and the groups file. A roster or server write wakes the follower only when a field in `_roster_state()` (`thread_id`, `turn_id`, `status`, `port`, `launcher_pid`, `continuation_owed`) or the server record changes, so the follower's own writes do not wake it. A wait returns `SocketReady`, `StateFilesChanged` or `WaitDeadlineExpired`.
- Socket reads use 256 KiB buffers. `_IGNORED_DELTA` drops output-delta frames (command output, agent message, reasoning, plan, realtime) before the JSON parse.
- `DetachedRunWatch` runs the dead-run and unseen-end checks in one `mesh_runs/*.json` scan every 60 s (`DETACHED_RUN_CHECK_SECS`). It keeps the names of finished records and does not read them again. Records stay in `mesh_runs/`.
- A lifecycle decision waits until the notices that its readback RPC buffered have drained. `state_change_owed` keeps a file wake owed until then; `capacity_backoff_owed` keeps a capacity refusal owed, and a peer turn start satisfies it in place of a 30 s backoff. `Client.wait_for_activity` waits for a turn start and consumes no frame.
- A follow ends when its worker's roster status is `done`, `failed`, `paused` or `ended`, or its run is finished. An off-host resident follower checks role selection over SSH every 5 s (`REMOTE_ROLE_CHECK_SECS`), not by file events.
- Measured on natedev, same method, 60 s windows: an idle follow 11.43% of a core before, 0.33% after; a 10,000-line output-delta stream 0.83%.

**Files:**
- `scripts/agents/codex_mesh.py` — `FollowEvents`, the inotify and kqueue watchers, the wait outcomes, `_IGNORED_DELTA`, `DetachedRunWatch`, the drain rule in `_on_quiet_poll`, follow exit on a finished worker
- `scripts/agents/test_codex_mesh.py` — fake app-server cases: 10,000 deltas under 10% of a core, quiet follow with no wake-up, marker wake, finished-worker exit, 3,000 finished records with no quiet reads, buffered-notice ordering regressions; `FixtureLoopBudget`

**Binds later work:** a lifecycle decision runs only after buffered notices drain (`state_change_owed`, `capacity_backoff_owed`). `DetachedRunWatch` is passed explicitly, and a lifecycle decision forces a fresh scan. A follower reacts to a state file only when `FollowEvents` watches it: a new file goes into the watch set, and its fields into `_roster_state()`. A test fixture blocks until its deadline (it never returns None at once) and keeps the 2,000-call guard (`FixtureLoopBudget`).

**Gotchas:** every lifecycle readback RPC can buffer peer-turn notices ahead of its reply; a decision taken before they drain loses a peer turn or starts a false 30 s capacity backoff, which shows only under load. A fake `next_frame` that returns None at once makes a real-time loop spin (one test run grew to 7.7 GB). The kqueue path and the 5 s SSH role check are not verified at runtime. Bounded process-management sleeps (`REAP_POLL_SECS` 0.2, the 0.05 s account-move resume wait) stay; they are not in the follow wait.

**Ruled out:** moving finished run records out of `mesh_runs/` (`mesh_runs.py`, `verify.sh`, `review.sh`, `unit_lookup.py`, `rename_unit.py` and `retire_unit.py` find a record by its path).

### Phase 4 — Unit worker ledger, `workers.py resolve`, the finished-run gap (WS P2, part 1)  · status: done

#### As-built

- A worker belongs to its unit, not to one run. `scripts/direct/workers.py` keeps the unit worker ledger at `~/.local/state/unit-workers/<production>/<unit>.json` (`UNIT_WORKERS_STATE` sets the root) under an `flock`. `ledger_for_run()` takes production and unit from the run's progress state, else from the plan's `> **Production: …** — unit` header, else uses `adhoc/<repository>-<12-hex hash of the worktree path>`. This ledger is not the run's `workers` launch log.
- `UnitWorker` has no nullable fields. Backend is `CodexThread(thread_id, codex_home)` or `ClaudeSession(session_id)`. Ownership is `HeldBy(run)`, `Paused`, `Adopted(to_run)` or `Retired`. Events are `WorkerEvent`s (`opened`, `replaced`, `adopted`, each with a cause). An empty required field raises `WorkerUnavailable`. An undecodable worker or adoption intent moves once, with its original bytes, into `quarantined_workers` or `quarantined_adoptions`; that command exits 2 and names the quarantine, and later commands continue.
- `workers.py resolve --session-dir <run> --role <impl|test|fix|review>` prints a worker name. Order: an idle worker of the role in this run; the unit's ledger worker, adopted from its last run (from the ledger's saved backend when that run folder is gone); an ended worker reopened on its thread; a new thread named `<role>`, then `<role>2`, `<role>3`, … when every worker of the role is busy. The returned worker is reserved: `allocation_pending` with an allocation id, an owner pid and a 120 s window (`ALLOCATION_CLAIM_WINDOW_SECS`). A reservation blocks reuse only while `allocation_is_live()` (owner alive, window open). A follow claim clears it; a refused follow clears only its own (`release_allocation()`). On a finished run, resolve exits 2 and names `progress_history.py start-phase` or a new run.
- `codex_mesh.py adopt --session-dir <run> --from <old run> --name <worker>` moves an idle worker to the new run on the same thread id. Idle (`worker_idle_reason()`) means: no live reservation, no attached launcher, no owed continuation, no queued pending or server messages, no live or unreaped detached run, and no live turn on any server in the worker's run chain. Adopt is one transaction. An adoption intent is saved before the first write, every roster write advances the revision fence, and the next `adopt`, `resolve`, `follow` or `reopen` in the unit rolls an open intent forward or back first. The giving entry gets `adopted_to: <run>`, a terminal state that `follow` refuses. The first exact-thread resume on the new run's server records the actual Codex home (`note_backend()`, also after an account move) and clears `adopted_from`.
- `adopted_to` is a `FollowEvents._roster_state()` field and maps to `AdoptedThread` in the typed roster states. The old launcher drains buffered notices, then stops with `WorkerHandedOff` (follow exits 2), and never writes the new owner's roster entry or ledger ownership.
- `progress_history.py start-phase` on a finished run makes it active, writes a `reopened` event and restores the run marker. `codex_mesh.py reopen` on a finished run exits 2 and names `start-phase` and `workers.py resolve`.
- `open_unit_worker()` starts the thread through `_thread_start_params` (role `review` is read-only), names it `<production> · <unit> · <role> (<name>)`, and releases the thread when the name set or the roster write fails.

**Files:**
- `scripts/direct/workers.py` — executable CLI: ledger types and I/O, quarantine, reservations, adoption intents and recovery, `adopt`, `note_backend`, `resolve`
- `scripts/agents/codex_mesh.py` — `adopt` subcommand, `worker_idle_reason`, `open_unit_worker`, `AdoptedThread`, `WorkerHandedOff`, `reopen` exit 2; loads `scripts.direct.workers` through `importlib` behind the `UnitWorkerRegistry` protocol (no static import cycle)
- `scripts/direct/progress_history.py` — `start-phase` reactivation
- `scripts/direct/test_workers.py`, `scripts/agents/test_codex_mesh.py`, `scripts/direct/test_progress_history.py` — fake app-server cases: same thread on a reactivated run, adoption and `follow` refusal, racing resolves and adoptions, a crash at each durable adopt write, account-move carry-over, reservation expiry and release, quarantine, the old launcher's handoff

**Binds later work:** lock order is the ledger, then the giving roster, then the taking roster, then the pending inbox; no caller enters `workers.py` while it holds a run lock. Pause, retire, transfer and rename carry `allocation_pending` (id, owner pid, window) and `quarantined_workers`: pause keeps them, retire clears them, transfer and rename move them unchanged. Every caller of `resolve` keeps the name in a long-lived process or passes its owner pid. Roles are listed in three places: `codex_mesh.WORKER_ROLES`, `codex_mesh.WorkerRole`, `workers.ROLES`. A launcher reads follow's exit 2 after `WorkerHandedOff` as handed off: completion unknown, never resubmitted.

**Gotchas:** the `resolve` CLI records `os.getppid()` as the reservation owner. Under Claude Code's Bash tool that is the one-call shell, so a hand-run reservation is dead when the call returns (the `--owner-pid` fix is in the worker lifecycle Work Order). A file opened `a+` reads from EOF; seek to 0 before reading an inbox. No command lists or clears a quarantined entry yet.

### Phase 5 — Worker lifecycle: run end, phase end, retire, transfer, rename (WS P2, part 2)  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** workers live until the unit is retired; run end, transfer and rename only pause them and keep their work, and retire alone ends them.

**Spec:**
- **Scope.** This phase covers writer workers (`impl`, `test`, `fix`) and the generic lifecycle that every ledger entry shares. Reviewer metadata, `import-pool` and reviewer rebinding belong to Phase 8. The switched-unit integration (`switched.json`) belongs to Phase 12. A unit's pool reviewers keep today's `review_pool.py` path in this phase (planner's split, from the Phase 4 review).
- **Pause keeps work.** Today `codex_mesh._pause_roster()` truncates each `<name>.pending.json`, calls `_drop_queued_messages()` on the server, and `command_stop()` reaps every server the run folder records. Pause now keeps the pending file. Before it stops a server, it moves each server-queued message of a paused thread into that worker's pending file, so the message outlives the server. Pause keeps `continuation_owed`, the detached run records and their supervisors, and the reservation fields (`allocation_pending`, `allocation_id`, `allocation_owner_pid`, `allocation_expires_at`). Pause reaps only the servers that this run folder records (`mesh_server.json`, `mesh_retired.json`). It never reaps a server that another run or a production records.
- **Retire cancels work.** `workers.end_unit` cancels pending messages, owed continuations and detached runs (it ends each supervisor by its record's `pid`), clears the reservation fields, ends each worker, prints one line for each thing it cancelled, prints one line for each quarantined entry it removes (name, reason, ledger path), and removes the ledger file.
- **A paused worker's work moves with it on adopt.** Phase 4's `codex_mesh.worker_idle_reason()` refuses adoption when messages, an owed continuation or a detached run remain. The idle rule now depends on ledger ownership:
  - `HeldBy(run)`: adoption is refused only for live activity: a live turn, an attached launcher (`worker_launcher_attached()`), or a live detached run.
  - `Paused`: pending messages, `continuation_owed` and detached runs do not refuse adoption. They move with the worker. Adopt copies the pending entries into the taking run's inbox under the same transaction (the inbox lock is last in the lock order). `continuation_owed` stays in the taken roster record. Detached run records stay in the giving run's `mesh_runs/` (Phase 3 ruled out moving them); the taking run's `DetachedRunWatch` reads the worker's giving runs through `worker_run_history()`.
  - The taking run's first `follow` drains the preserved queue before its new work: it delivers each detached run result and pending message, and runs the owed continuation, in that order. It never sends a prompt again.
- **No destructive step on pause, transfer or rename.**
  - `end_session.sh` runs `codex_mesh.py stop --pause` for each run (today it runs `stop`, which ends every worker). It leaves the workers in the ledger.
  - `remove_run_scratch.py` keeps each checkout that is, or holds, the `cwd` of a live detached run of a paused worker. It removes the rest as today.
  - `rename_unit._stop_runs()` (used by rename, and by transfer through `rename_unit.stop_unit()`) runs `codex_mesh.py stop --pause`, not `stop`. It does not terminate the supervisor of a paused worker's detached run, nor that supervisor's command. When such a run works inside the worktree that a rename moves, the rename refuses with `RenameRefused` (`detached run <id> of <worker> works inside <worktree>; rerun apply once it ends`) and kills nothing. A transfer keeps the worktree, so it never refuses for this.
  - `retire_unit.stop_rest()` keeps `codex_mesh.py stop` (it ends workers) and calls `workers.end_unit`.
- `remove_workers.py` (phase end) does not remove a Claude worker that the ledger holds.
- **A ledger move is a migration.** `workers.move_unit` (transfer: new production folder) and `workers.rename_unit` (rename: new unit file name) are one journalled migration that can run again after a fault and gives the same result:
  - It first finishes or rolls back each pending adoption on the source ledger (Phase 4's `_recover`).
  - It writes a move journal next to the source ledger before its first write. Any `resolve`, `adopt` or `follow` that finds the journal finishes the move before it acts, as with the adoption journal.
  - It takes both ledger locks in a fixed order, the lower path (string order) first, and only then any roster lock. This keeps Phase 4's order (ledger, then giving roster, then taking roster, then inbox); two moves can never deadlock.
  - It rewrites every reference: `unit_ledger` in the roster of each unit run (`retire_unit.runs_of(worktree)`), the `production` and `unit` keys of each run's `progress_history_state.json` when present, and the paths in each pending adoption journal entry.
  - It moves `quarantined_workers`, `quarantined_adoptions` and each worker's reservation fields unchanged.
  - It refuses a destination ledger that exists and names other owners. A destination that holds this move's own partial write continues.
- `progress_history.py set-unit --session-dir <run> --production <slug> --unit <unit>` writes the `production` and `unit` keys that `workers.ledger_for_run()` reads first. `move_unit` and `rename_unit` call its function. Phase 11 uses it for the showrunner run.
- A production rename (`scripts/production/rename.py`) moves every ledger of the production: a new step `_rename_unit_workers`, next to `_rename_review_pool`, calls `workers.move_unit` for each `<unit>.json` under `unit-workers/<old slug>/` (planner's addition: without it a production rename strands the ledgers).
- `retire_unit.py` calls `workers.end_unit`. `transfer_unit._move_state()` calls `workers.move_unit`; its `review_pool.py retire` for the giving production stays. `rename_unit._move_state()` calls `workers.rename_unit`; its `review_pool.rename_unit()` stays.
- **Transfer keeps threads.** Transfer keeps each writer worker's thread id, backend and ownership; only the ledger path and the references above change. Each transfer step can run again after a fault and gives the same result.
- **Rename changes the shown name only.** A rename changes the shown context (`<production> · <unit> · <job>`). The worker address never changes. Today `codex_mesh._post_board_status()` takes the board slot from the name suffix (`name.rsplit("-", 1)[-1]`), and `worker_state.worker_record_path()` takes `fix` from the name suffix (`name.endswith("-fix")`). Both read the stored role instead (naming rule 4).
- `workers.py` gains the functions these callers use: `pause_unit`, `end_unit`, `move_unit`, `rename_unit`.
- **A reservation outlives the resolve call (Phase 4 review, F017).** The `resolve` CLI (`workers.main()`) records `os.getppid()` as `allocation_owner_pid`. Under Claude Code's Bash tool that parent is the one-call shell, so the reservation is dead when the call returns, and a second `resolve` by hand returns `impl` again instead of `impl2`. `resolve` gains `--owner-pid <pid>`. Without it, the owner is the run's director pid. A script caller (`name=$(… resolve …)`) keeps working. The allocation id, the 120 s window (`ALLOCATION_CLAIM_WINDOW_SECS`) and the identity-fenced release (`release_allocation()` clears only its own `allocation_id`) do not change.

**Files:**
- `scripts/direct/workers.py` — `pause_unit`, `end_unit`, `move_unit`, `rename_unit`, the move journal, the idle rule by ownership, `--owner-pid`
- `scripts/agents/codex_mesh.py` — pause keeps pending and server-queued messages and reaps only this run's servers; adopt carries a paused worker's queue; `DetachedRunWatch` reads giving runs; the first follow drains the queue; `_post_board_status()` reads the stored role
- `scripts/direct/worker_state.py` — `worker_record_path()` reads the stored role
- `scripts/direct/progress_history.py` — `set-unit`
- `scripts/direct/end_session.sh` — `stop --pause`
- `scripts/direct/remove_run_scratch.py` — keeps a paused worker's detached-run checkout
- `scripts/direct/remove_workers.py` — keeps a ledger Claude worker
- `scripts/production/retire_unit.py` — `end_unit`
- `scripts/production/transfer_unit.py` — `move_unit`
- `scripts/production/rename_unit.py` — `_stop_runs()` pauses; `rename_unit`; the detached-run refusal
- `scripts/production/rename.py` — `_rename_unit_workers`
- `scripts/agents/test_codex_mesh.py` — pause, adopt-queue and board-slot cases
- `scripts/direct/test_worker_state.py` — arbitrary-address cases
- `scripts/direct/test_workers.py` — move, retire, idle-rule and owner cases
- `scripts/direct/test_progress_history.py` — `set-unit` cases
- `scripts/direct/test_end_session.py`
- `scripts/direct/test_remove_run_scratch.py`
- `scripts/direct/test_remove_workers.py`
- `scripts/production/test_retire_unit.py`
- `scripts/production/test_transfer_unit.py`
- `scripts/production/test_rename_unit.py`
- `scripts/production/test_rename.py` — production rename moves each ledger

**Workers:** `1 writer + 1 tester`
- `impl` — the eleven script files; hub: `scripts/direct/workers.py` (every caller uses its four functions)
- `test` — the eleven test files (fake app-server; bounded real waits): run end leaves workers paused and in the ledger; pause keeps pending messages, server-queued messages, owed continuations, detached runs and the reservation fields; pause leaves a server that another run records running; a paused detached command keeps its `cwd` through run end and through transfer and finishes; rename refuses, and kills nothing, while a paused worker's detached run works inside the worktree; a paused worker with pending messages, an owed continuation and a detached run is adopted and its queue moves; the taking run's first follow delivers the preserved queue before new work and sends no prompt twice; a `HeldBy` worker with a live turn, an attached launcher or a live detached run is not adopted; retire cancels and names each item, clears the reservation and reports each removed quarantined entry; phase end keeps a ledger Claude worker; a move stopped after each durable write finishes when run again and leaves exactly one owner; a move refuses a destination with other owners; a production rename moves each unit ledger; after a move, `ledger_for_run()` for each unit run resolves the new ledger, and every roster `unit_ledger` names it; a move with a pending adoption finishes the adoption first; `set-unit` writes the keys; `resolve` through `bash -c '…; true'` keeps its reservation and a second `resolve` gets `<role>2`; `--owner-pid` sets the owner; board slot and record path come from the stored role for an arbitrary address (for example `w7`)

**Constraints from prior phases:**
- Phase 4 (ledger): `~/.local/state/unit-workers/<production slug>/<unit>.json` (root `UNIT_WORKERS_STATE` in tests); a run outside a production uses `adhoc/<repository>-<hash of worktree path>`. `workers.ledger_for_run()` reads the `production` and `unit` keys of `progress_history_state.json` first, then the plan's `> **Production: …** — unit \`…\`;` header, else the adhoc key. `workers.py` holds all ledger I/O (`UnitWorkerLedger.locked()`, `save()`).
- Phase 4 (types): `UnitWorker(name, role, family, model, effort, access_policy, backend, ownership, events)`; backend `CodexThread(thread_id, codex_home) | ClaudeSession(session_id)`; ownership `HeldBy(run) | Paused | Adopted(to_run) | Retired`; events `opened`, `replaced`, `adopted`. Roster records carry `unit_ledger` (the ledger path), `adopted_from`, `adoption_id`, `adopted_to`.
- Phase 4 (lock order): the ledger lock, then the giving roster, then the taking roster, then the pending inbox. No caller enters `workers.py` while it holds a run lock. Each roster write advances the revision fence (`codex_mesh.advance_worker_roster_revision()`).
- Phase 4 (quarantine): `locked()` moves an undecodable worker into `quarantined_workers` and an undecodable adoption into `quarantined_adoptions`, with the original record, saves, and raises `WorkerUnavailable` once; later calls proceed. Keep quarantined entries; only retire removes them.
- Phase 4 (reservation): `resolve` writes `allocation_pending`, `allocation_id` (uuid), `allocation_owner_pid` and `allocation_expires_at` (120 s). `allocation_is_live()` needs all four and a live owner. `command_follow()` captures the reservation (`allocation_for_follow()`) and releases only its own `allocation_id` (`release_allocation()`); an adopted owner (`adopted_to`) is never released.
- Phase 4 (finished run): `resolve` and `codex_mesh.py reopen` exit 2 on a finished run and name `progress_history.py start-phase` (it writes a `reopened` event and restores the run marker) or a new run. `follow` exits 2 with `worker handed off to <run>; assignment completion unknown` on `WorkerHandedOff`.
- Phase 3: a lifecycle decision runs only after buffered notices drain (`state_change_owed`, `capacity_backoff_owed`). A new state file a follower must react to goes into the `FollowEvents` watch set and its fields into `_roster_state()`. A test fixture blocks until its deadline and keeps the 2,000-call guard (`FixtureLoopBudget`).

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/production scripts/agents` is 0/0.

### Phase 6 — Role words `research` and `propose` (WT P2)  · status: todo

**Depends on:** Phase 2, Phase 4

#### Work Order

**Goal:** the roster, the unit worker ledger, the hierarchy edge and the cargo handler know the roles `research` and `propose`, and both open read-only.

**Spec:**
- Three role registries take the two words. Update all three:
  - `codex_mesh.WorkerRole = Literal["impl", "test", "fix", "review", "research", "propose"]`
  - `codex_mesh.WORKER_ROLES = ("impl", "test", "fix", "review", "research", "propose")`
  - `workers.ROLES = ("impl", "test", "fix", "review", "research", "propose")`; today `workers.py resolve --role` and `workers._resolve()` refuse any other word before allocation.
- Check every use of `WORKER_ROLES` by symbol: `_roster_role()`, `_write_roster_record()`, and the `start` and `follow` parsers in `main()`. Check every `WorkerRole` annotation (the roster `TypedDict`s, `_worker_role()`, `_set_roster_assignment()`).
- **Access policy.** `codex_mesh.open_unit_worker()` today makes only `review` read-only (`{"kind": "ReadOnlyReviewer"}` and `READ_ONLY_SANDBOX`). `research` and `propose` open the same way: `ReadOnlyReviewer` policy and `READ_ONLY_SANDBOX`. The extra writable root for `propose` (`~/.local/state/plan-backlog`) comes with `assign.sh` in Phase 7.
- The hierarchy edge JSON `{"manager","member","role"}` carries the word.
- `config/agents.conf [direct.codex]`: `research=astra:xhigh`, `propose=astra:xhigh` (user, 2026-10-28; the model `/astra_review` uses for documents).
- Cargo handler: `HierarchyRole` gains `Research` and `Propose` (serde lowercase) with their `text`; an unknown word still takes `RoleFallback`. This is a separate repository; its commit lands there, and the checkpoint body names it.

**Files:**
- `scripts/agents/codex_mesh.py` — `WorkerRole`, `WORKER_ROLES`, `open_unit_worker()` access policy
- `scripts/direct/workers.py` — `ROLES`
- `config/agents.conf` — two rows
- `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/mod.rs` — two variants
- `scripts/agents/test_codex_mesh.py` — role cases
- `scripts/direct/test_workers.py` — `resolve` cases for both roles

**Workers:** `2 writers` — the two repositories split.
- `impl` — `codex_mesh.py`, `workers.py`, `config/agents.conf`, `test_codex_mesh.py`, `test_workers.py`: `workers.resolve` opens a `research` and a `propose` worker, each with `ReadOnlyReviewer` and the read-only sandbox; the roster and the edge carry the word
- `test` (opens as impl) — `census/mod.rs` and its crate tests

**Constraints from prior phases:**
- Phase 2: `[direct.codex]` rows take family words; `astra` resolves through `[codex.families]` to the newest cached release that has a `[codex.agents]` effort row (today `gpt-6-astra`, which has a `weights.py` row). `gpt-6.1-terra` and `gpt-6-luna` have no `weights.py` row: usage accounting raises `UnweightedModel` for them, so no row may name `terra` or `luna`.
- Phase 4: `codex_mesh.open_unit_worker(session_dir, name, role)` resolves `agents_resolve direct.<role>` and refuses a non-Codex family (`WorkerUnavailable("new persistent worker requires a Codex assignment")`); it records `access_policy` in the roster, and `workers._worker_from_record()` copies it to the ledger entry. Ledger lock order: ledger, giving roster, taking roster, inbox. Fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; `verify.sh test cargo-handler` and `verify.sh lint cargo-handler` pass; basedpyright on `scripts/direct scripts/agents` is 0/0; the handler shows `research` and `propose`, and an unknown word takes `RoleFallback`.

### Phase 7 — `assign.sh` core (WT P1, with WS P2's `--role`, run start and rules block)  · status: todo

**Depends on:** Phase 2, Phase 4, Phase 5, Phase 6

#### Work Order

**Goal:** one launcher with named options opens or resumes the right worker for six roles; `implement.sh` and `review.sh` do not change.

**Spec:**
- Call form:
  ```
  assign.sh --role <implement|test|fix|review|research|propose>
            [--to <worker> | --new-worker <name>]
            [--model <short[:effort]>] [--effort <e>]
            --prompt <file> [--summary <file>]
            [--paths <file>]
            [--phase <id>] [--lens <adversary|contract|craft|ux>] [--pass <n>]
            [--image <path>]... [--early-ready <file>]
            [--session-dir <run>] [--cwd <dir>]
  ```
- Named options only. A missing `--role` or `--prompt` exits 2 with the usage line. `--session-dir` defaults to the caller's run (`unit_runs.marker`). With no run, `assign.sh` starts one with `prepare_session.sh` and prints the folder (WS P2).
- **Finished run.** When `--session-dir` names a finished run (`codex_mesh.finished_run_status()`), `assign.sh` runs `progress_history.py start-phase` on it to make it active again (Phase 4), then resolves the worker. It never opens a new run for a finished run that the caller named.
- Role: `implement` is stored as `impl`. The word goes in the roster `role` field and in the hierarchy edge. `research` reads and writes one report to `--summary`. `propose` writes one proposal file into `~/.local/state/plan-backlog/`; the launcher, not the worker, appends the `ledger.md` line from the worker's summary. A worker that writes a second file there is named by the launcher's listing of the folder before and after the turn. `review`, `research`, `propose` open with `access: read-only` (`codex_mesh.py prime` / `fork` sandbox, roster `access_policy`); `propose` adds `~/.local/state/plan-backlog` as a writable root. `implement`, `test`, `fix` open with the default full-access sandbox.
- **Authorized paths.** `--paths <file>` lists the repository-relative paths this dispatch may change, one per line. It is required for `implement`, `test` and `fix` (a missing or empty file exits 2), and refused for `review`, `research` and `propose`. Each path must be relative, inside `--cwd`, with no `..` part; a bad path exits 2 and names it. At dispatch, `assign.sh` writes `${PHASE_DIR}/dispatch_<n>_paths.json`: `{"worker": <address>, "round": <n>, "paths": [...]}`, where `<n>` is the phase's next dispatch number. Phase 10 reads this manifest.
- **Worker choice.** With `--to`, the named worker. Otherwise `workers.py resolve --role <role> --owner-pid $$` (the launcher's own pid: `assign.sh` lives for the whole turn, so the reservation stays live until its follow claims it). `--new-worker <name>` opens a new named worker (today's `review.sh --slot`). The launcher keeps the reservation's `allocation_id`, its 120 s window and the identity-fenced release: a refused follow releases only its own reservation, and an adopted owner (`adopted_to`) is never released.
- **Model and effort.** `agents_resolve direct.<role>` gives family, model and effort. A live worker keeps its model and effort (live-worker identity invariant): `codex_mesh._follow_worker()` replaces a supplied model and effort with the roster identity when the record holds `unit_ledger`. So `--model sol:medium` or `--effort max` selects a worker with that identity: `workers.py resolve` gains `--model <slug> --effort <e>` and returns an idle worker of the role with exactly that identity, or opens `<role>N` with it. An override never changes an existing worker's model. `--to <worker>` with a `--model` or `--effort` that differs from that worker's identity exits 2 and names both. The identity check of `implement.sh` (`worker_record` `claim`, which keeps the stored model) stays the rule.
- Every launch: claim under the worker lock (port `implement.sh` `worker_record` and `review.sh` `claim_follow` into one path); compact above `UNIT_COMPACT_ABOVE_TOKENS`; append the rules block to every prompt (no poll; long commands through `mesh_runs.py start`, then end the turn; the Three Gods sentence); board register, heartbeat header, pass record keyed by role and phase (`progress_history.py start-pass` / `finish-pass`); summary settle; release.
- **Address and shown name (naming rule 6).** A worker has two separate values. The address is its full name (for example `impl2`, `claude-build-followups-sdlc-roles-impl`); it never changes, and `resolve` output, `--to`, roster keys, messages' routing and every file name use it. The shown name is `<production> · <unit> · <job>` (for example `build-followups · models · code reviewer 2`). New renderer `workers.shown_name(run, address) -> str` builds it at each call from the ledger path's production and unit (`ledger_for_run()`) and the stored role, never from the address. It is never stored, so it follows a rename or a transfer. CLI: `workers.py shown-name --session-dir <run> --name <address>`.
- `assign.sh` sets each Codex thread name and every board, heartbeat and summary label to the shown name. Messages and reports use it.
- `codex_mesh.py` uses the shown name too: every `thread/name/set` (in `open_unit_worker()`, which today builds `<production> · <unit> · <role> (<name>)` by hand, and on a replacement thread) and every follow-up message it prints. Help and diagnostics drop "seat" (for example the `reopen` help and the `command_stop()` docstring). A stored key or field that existing data still reads keeps its name, with a note next to it (naming rule 5). The tests-only gate check for "seat" belongs to Phase 12.
- Artifacts: `${PHASE_DIR}/<role>_status`, `<role>_summary.txt`, `<role>_agent.log`, with `_<lens>` and `_<pass>` suffixes for reviews.
- **Exit codes and outcomes.** 0 done; 1 the worker ran and failed; 2 a refused argument or claim; 3 blocked (no worker could start); 4 handed off. `follow` exits 2 for both a refusal and a `WorkerHandedOff`; after a `follow` exit 2, `assign.sh` reads the worker's roster record, and an `adopted_to` value means handed off. On handed off, `assign.sh` writes `handed_off` to `<role>_status`, records the destination run in `<role>_summary.txt`, prints `handed off: <worker> to <run>; completion unknown`, does not record the pass as done, and never sends the prompt again.
- Review in this phase: `--role review` takes an open writer for the lens, as today without a pool. Pooled reviewers come in Phase 8.

**Files:**
- `scripts/direct/assign.sh` — new
- `scripts/direct/workers.py` — `resolve --model --effort`, `shown_name`, `shown-name`
- `scripts/agents/codex_mesh.py` — shown name in thread names and follow-up messages; no "seat" in help and diagnostics
- `scripts/direct/test_assign_launcher.py` — new
- `scripts/direct/test_workers.py` — identity-selection and shown-name cases
- `scripts/agents/test_codex_mesh.py` — shown-name cases

**Workers:** `1 writer + 1 tester`
- `impl` — `assign.sh`, `workers.py`, `codex_mesh.py`; hub: `scripts/direct/workers.py`
- `test` — `test_assign_launcher.py` (fake app-server; bounded real waits): each role opens with its sandbox; `--to` resumes a thread; `--model` on a roster that holds `unit_ledger` selects or opens a worker with that identity and leaves the existing worker's model unchanged; `--to` with a different `--model` exits 2; `propose` writes the file and the launcher adds the ledger line once; a missing `--role` exits 2; a writer role without `--paths` exits 2, a reviewer with `--paths` exits 2, a `..` path exits 2; each dispatch writes its `dispatch_<n>_paths.json`; every worker prompt carries the rules block; the thread name and board label read `<production> · <unit> · <job>` while `--to` takes the full address; no run starts one; a named finished run is reactivated with `start-phase` and keeps its worker's thread; the reservation owner is the launcher pid and stays live through the claim; a handoff during the turn gives exit 4, status `handed_off`, the destination run, no finished pass and no second prompt. `test_workers.py`: `resolve --model --effort` picks only an exact identity; `shown_name` follows a `move_unit` and a `rename_unit` while `resolve` prints the same address. `test_codex_mesh.py`: a replacement thread name and a follow-up message use the shown name; `--help` output has no "seat"

**Constraints from prior phases:**
- Phase 2: rows are family words; `agents_resolve` returns the slug or exits 1 with `no Codex model for '<word>' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug`. A resolved line ends with ` word=<family word>` when the row names a family (`agents_config.sh` `_agents_print_resolved`); the slug is in `agent=`.
- Phase 4: `workers.py resolve --session-dir <run> --role <r>` prints the worker address; `<role>2` when all are busy. Its reservation is `allocation_pending`, `allocation_id`, `allocation_owner_pid` and `allocation_expires_at` (120 s, `ALLOCATION_CLAIM_WINDOW_SECS`); `command_follow()` releases only its own `allocation_id`. `resolve` and `reopen` exit 2 on a finished run and name `progress_history.py start-phase`. `follow` exits 2 on `WorkerHandedOff` (`worker handed off to <run>; assignment completion unknown`). A quarantined ledger entry makes the first `follow` or `reopen` exit 2 naming the quarantine; later calls proceed. Ledger lock order: ledger, giving roster, taking roster, inbox.
- Phase 5: `resolve --owner-pid <pid>`; pause keeps pending messages, owed continuations and detached runs, and adopt carries them; the taking run's first follow drains them before new work. `workers.move_unit` and `workers.rename_unit` move the ledger and rewrite each roster's `unit_ledger`; `progress_history.py set-unit` writes the run's `production` and `unit`.
- Phase 6: `WORKER_ROLES`, `WorkerRole` and `workers.ROLES` hold `research` and `propose`; `open_unit_worker()` opens both read-only; their rows are `astra:xhigh`.
- Phase 3: a lifecycle decision runs only after buffered notices drain; keep the `FixtureLoopBudget` guard in new fixtures.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/agents` is 0/0.

### Phase 8 — Reviewers in the worker ledger (WT P3)  · status: todo

**Depends on:** Phase 5, Phase 7

#### Work Order

**Goal:** pooled reviewers are workers in the unit ledger, with today's pool behavior; `assign.sh --role review` uses them, and a pool reviewer's authority moves to the ledger exactly once.

**Spec:**
- Keep (user, ledger 2026-10-10): one permanent read-only reviewer per owner and type; a concurrent review forks the owner's newest reviewer; a changed standing prompt re-points idle reviewers; the tree guard (`review_tree.py`) after every turn; the turn record with every field that `review.sh` `write_turn_record` writes; the owner's reviewers end at retire.
- **Named reviewer payload.** `UnitWorker` gains `payload: WriterPayload | ReviewerPayload`. `ReviewerPayload(reviewer_type: ReviewerType, instructions_file, prime_hash, reviews, mesh_dir, lease: Unleased | UnitLease, claim: IdleReviewer | ClaimedReviewer)`. The four state types and `ReviewerType` move from `review_pool.py` to `workers.py`; `review_pool.py` imports them from there, so Phase 15 can remove `review_pool.py`. No reviewer field becomes optional on `UnitWorker`. A ledger entry with no stored payload decodes as `WriterPayload`, so Phase 4 and Phase 5 ledger files still read. Nullable legacy JSON (`ReviewerJson.lease: LeaseJson | None`, `busy_pid: int | None`) stays only in `review_pool.py` and in the `import-pool` decode.
- `review_pool.py` `prime`, `allocate` (`_prime`, `_choose`, `_repoint`), `release`, `retire` become `workers.py` functions that act on `ReviewerPayload` entries; `migrate` and `stop` stay as `workers.py` verbs for the showrunner. The production template thread and its mesh dir live in `~/.local/state/unit-workers/<slug>/`.
- One app-server per production for reviewers stays. The production's mesh dir records it, not any run folder, so Phase 5's pause (which reaps only the servers its run folder records) never reaps it. `workers.py` passes the entry's mesh dir to `follow --cwd <worktree> --read-only`.
- No `--pool` flag and no `--lease`. `assign.sh --role review` reads the production doc's `**Review pool:** code|ui` line. With it, the worker is the owner's read-only code reviewer or UI reviewer. Without it, an open writer takes the lens. The unit and phase come from the run and `--phase`.
- **`workers.py import-pool --unit <unit>` is a fenced ownership transfer, not a copy.** For each reviewer of `~/.local/state/review-pool/<slug>/pool.json` leased to the unit:
  1. Under the ledger lock, then the pool lock (fixed order), it marks the pool reviewer `Transferred(ledger)` in `pool.json` (a third pool lease variant beside `Unleased` and `UnitLease`) and saves the pool. It keeps the thread id, prime hash and review count in the pool record.
  2. It then writes the ledger entry (`ReviewerPayload`, same thread id) as owned by the ledger, and saves.
  - A crash between the two writes leaves a `Transferred` pool reviewer with no ledger entry. The next `import-pool` or `resolve` on that ledger writes the entry from the pool record. A rerun after both writes changes nothing.
  - A reviewer with a live `ClaimedReviewer` pid is not marked. `import-pool` exits 2 and names it; a rerun after its review moves it.
  - Legacy `review_pool.py` refuses `Transferred`: `allocate` (`_choose`) never picks it, `_repoint_idle` and `migrate` skip it, `retire` and `rename_unit` skip it, and `status` prints `transferred` in the lease field, so `transfer_unit._leased()` and `retire_unit.reviewers()` do not count it.
  - It keeps the primed threads (1,435 against 12,982 weighted tokens a turn). Phase 12's `switched` calls it. `review.sh` keeps `pool.json` for units not yet switched.
- `review_pool_report.py` reads reviewers from the ledger and the non-`Transferred` reviewers of `pool.json` until Phase 15.
- **Lifecycle of ledger reviewers.** `workers.move_unit` (transfer, Phase 5) rebinds each `ReviewerPayload`'s `mesh_dir` and `instructions_file` to the new production and keeps its thread id; a transfer stopped after any step completes when run again. `workers.rename_unit` keeps them (the production does not change). `workers.end_unit` (retire) ends ledger reviewers. `transfer_unit._move_state()` and `retire_unit.stop_rest()` keep their `review_pool.py retire` call for pool reviewers; it skips `Transferred` ones.

**Files:**
- `scripts/direct/workers.py` — `ReviewerPayload`, the moved state types, review functions, `import-pool`, reviewer rebind in `move_unit`, reviewers in `end_unit`
- `scripts/direct/review_pool.py` — imports the state types; `Transferred`; legacy paths refuse or skip it
- `scripts/direct/assign.sh` — the review path, tree guard, turn record
- `scripts/direct/review_pool_report.py` — reads the ledger
- `scripts/direct/test_workers_review.py` — new, ported from the `test_review_pool.py` cases
- `scripts/direct/test_review_pool.py` — `Transferred` cases
- `scripts/direct/test_assign_launcher.py` — review cases
- `scripts/direct/test_review_pool_report.py` — review cases
- `scripts/production/test_transfer_unit.py` — reviewer transfer cases

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`, `review_pool.py`, `assign.sh`, `review_pool_report.py`; hub: `scripts/direct/workers.py`
- `test` — `test_workers_review.py`, `test_review_pool.py`, review cases in `test_assign_launcher.py`, `test_review_pool_report.py` and `test_transfer_unit.py` (fake app-server; bounded real waits): the existing `test_review_pool.py` cases pass against `workers.py`; a second concurrent review forks the owner's reviewer; a prompt change re-points an idle reviewer; the turn record keeps every field; a ledger file written before this phase decodes with `WriterPayload`; a `ReviewerPayload` round-trips with each lease and claim variant; `import-pool` keeps thread ids; a crash after the pool mark and before the ledger write is finished by the next `import-pool` and by `resolve`; a legacy `allocate` that runs during `import-pool` never gets a `Transferred` reviewer, and the thread has exactly one owner; a claimed reviewer is not imported; a transferred reviewer keeps its thread id and points to the new mesh dir and instructions file; a transfer stopped midway completes when run again; pause of a unit run leaves the production reviewer server running

**Constraints from prior phases:**
- Phase 4: ledger types (`UnitWorker`, `CodexThread | ClaudeSession`, `HeldBy | Paused | Adopted | Retired`), `UnitWorkerLedger.locked()` and its quarantine of undecodable entries (keep quarantined entries; only retire removes them). Lock order: ledger, giving roster, taking roster, inbox; this phase puts the pool lock directly after the ledger lock. Reservation: `allocation_id`, owner pid and the 120 s window, with identity-fenced release.
- Phase 5: `pause_unit`, `end_unit`, `move_unit`, `rename_unit` in `workers.py`; pause reaps only the servers its run folder records; a move is a journalled migration that takes both ledger locks in path order and rewrites roster `unit_ledger`, progress-state `production`/`unit` and adoption journal paths. Phase 5 covers writer workers only; reviewers are this phase.
- Phase 7: `assign.sh` options, artifacts and exit codes (4 = handed off); `--lens` and `--pass` suffixes; `--paths` is refused for `review`; the worker address and the shown name (`workers.shown_name()`) are separate.
- Phase 3: fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/production` is 0/0.

### Phase 9 — Self-heal (WS P3)  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** the launcher repairs each worker failure by itself; when nothing can start, it says `blocked:` and tells the showrunner.

**Spec:**
- Worker cannot start, fast local fault, stale sign-in: the existing retry on a new app-server stays.
- Thread gone or cannot resume: `workers.py resolve` opens a replacement under the same name. Its opening prompt has a catch-up brief: the Work Order, the worker's last two summaries and `git diff --stat` since the phase baseline. It posts one board line and records `replaced` with the cause in the ledger.
- **One replacement authority.** `workers.py` alone opens a replacement. Today `codex_mesh._resume_worker_thread()` calls `thread/start` and continues as a new thread when resume fails. It stops doing that and returns a named outcome (`ThreadMissing`). Each caller (the live follow and the resolve path) passes `ThreadMissing` up, and `workers.py` replaces the thread.
- **No duplicate submission.** "Send the same phase prompt again" applies only when no submitted turn may still run. An uncertain submission (for example `WorkerMoveUncertain`), an owed continuation (`continuation_owed`), a paused worker's preserved queue (Phase 5), or a handed-off dispatch (`assign.sh` exit 4, `handed_off`: completion unknown, the turn belongs to the destination run) waits or resumes; it never sends the prompt again.
- A turn still live after compaction (today exit 3): send `turn/interrupt`, wait 60 s; if still live, end the worker and open a replacement with the brief. The interrupt wait and the replacement wait keep every notice received before an RPC reply (Phase 3 drain rule).
- Second overflow with no progress: compact once and send the follow-up again; if that fails, open a replacement with the brief.
- **Claude route state.** `UnitWorker.backend` holds exactly one `CodexThread` or `ClaudeSession` today. Add a third backend variant `WorkerRoutedToClaude(claude: ClaudeSession, parked: CodexThread, catch_up_from: str)`, where `catch_up_from` is the commit at route start. The name differs from `moves.RoutedToClaude` (the account route record) on purpose. `WorkerEvent.kind` gains `routed` and `returned`. Pause during a route pauses the Claude worker and keeps `parked`. Retire during a route ends both the Claude session and the parked thread. The route ends when the next dispatch resumes `parked`; the backend is then `CodexThread` again.
- Quota refusal: after the capacity wait and the account move, `quota_alert.py` routes `direct` to Claude and writes `routed to claude`. `assign.sh` opens Claude workers (named background sessions through `agent_bg.sh`, which compact) under the same ledger entry (`WorkerRoutedToClaude`), sets `UNIT_WORKER=<address>`, and sends the same phase prompt again only under the no-duplicate rule above. When the route returns to Codex, the next dispatch resumes the parked thread with a catch-up message: the Claude worker's summaries and the diff since `catch_up_from`. A subagent is never in this chain.
- Codex outage (non-local errors from two servers over 10 minutes): new cause `outage` in `quota_alert.py`, same routing, 30-minute hold; after the hold the next dispatch tries Codex again. `scripts/accounts/moves.py` validates `RoutedToClaude.cause` (today `refusal`, `credits off`, `auto`); add `outage` to its type and its check.
- Nothing can start: `assign.sh` exits 3 with `blocked: no worker could start: <cause>` and sends the showrunner one message (`send.py`).

**Files:**
- `scripts/direct/workers.py` — replacement, brief, `WorkerRoutedToClaude`, `routed`/`returned` events
- `scripts/direct/assign.sh` — interrupt, overflow, Claude route, blocked message
- `scripts/whoami/quota_alert.py` — `outage` cause
- `scripts/accounts/moves.py` — `outage` in `RoutedToClaude.cause`
- `scripts/agents/codex_mesh.py` — `_resume_worker_thread()` returns `ThreadMissing`
- `scripts/direct/test_workers.py`
- `scripts/direct/test_assign_launcher.py`
- `scripts/whoami/test_quota_alert.py`
- `scripts/accounts/test_moves.py` — journal cases for `outage`
- `scripts/agents/test_codex_mesh.py` — `ThreadMissing` cases

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`, `assign.sh`, `quota_alert.py`, `moves.py`, `codex_mesh.py`
- `test` — fake-server cases (bounded real waits): lost thread gives a replacement with the brief in its prompt; a missing thread found during a live follow and during resolve gives `ThreadMissing` and one `workers.py` replacement, and `codex_mesh.py` starts no thread; live turn gives interrupt then replacement, and a notice received before the interrupt reply is kept; an uncertain submission, an owed continuation, a preserved paused queue or a handed-off dispatch is never sent again; second overflow gives compaction and one retry; outage gives a route with a 30-minute hold and a route back; the move journal accepts and reads back `outage`; quota route opens a Claude worker and returns to the parked Codex thread with the catch-up message; `WorkerRoutedToClaude` round-trips through the ledger; pause during a route keeps the parked thread, and retire during a route ends both backends; no case starts an Agent-tool subagent

**Constraints from prior phases:**
- Phase 4: ledger `replaced` event; `UnitWorker.backend` is `CodexThread | ClaudeSession`; lock order ledger, giving roster, taking roster, inbox; undecodable entries are quarantined once and kept; the reservation (`allocation_id`, owner pid, 120 s window) is released only by its own id.
- Phase 5: pause keeps pending messages, owed continuations and detached runs, and the taking run's first follow drains them before new work; retire (`end_unit`) cancels them.
- Phase 7: exit code 3 means blocked and 4 means handed off (status `handed_off`, destination run recorded, never resubmitted); `assign.sh` passes `--owner-pid $$` to `resolve`; the address and the shown name are separate.
- Phase 3: a lifecycle decision runs only after buffered notices drain (`state_change_owed`, `capacity_backoff_owed`); fixtures block until their deadline and keep the 2,000-call guard.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/whoami scripts/accounts scripts/agents` is 0/0.

### Phase 10 — Worker provenance E4–E7 (WS P4, with WT's E4 line and E7 in `assign.sh`)  · status: todo

**Depends on:** Phase 7, Phase 9

#### Work Order

**Goal:** a checkpoint with code that no worker and no attested user grant left is refused at commit and held at merge; a Claude worker with no open route is refused.

**Spec:**
- **E4.** New `scripts/direct/worker_provenance.py record`: at the end of each worker turn, append one line to `${PHASE_DIR}/worker_turns.jsonl`: worker, thread, family, route state, time, and `{path: blob id}` for each changed path (`git status` plus `git hash-object`). `assign.sh`, `implement.sh` and `review.sh` all call it, so E5 holds for every unit before the cutover.
- **Authorized paths input.** At dispatch, `worker_provenance.py start --manifest <file>` reads the dispatch manifest `${PHASE_DIR}/dispatch_<n>_paths.json` (`{"worker", "round", "paths"}`) that `assign.sh` writes (Phase 7), and records a starting snapshot of those paths. `implement.sh` gains `--paths <file>` and writes the same manifest. An `implement.sh` dispatch without `--paths` writes the legacy form `"paths": "worktree"`: every worktree path, attributed only when changed from the starting snapshot (planner's call, so units not yet switched keep passing E5). `review.sh` writes `"paths": []`. A turn with no manifest attributes no path, and its `record` line says `no manifest`.
- **Attribution.** At turn end, a path goes to the worker only when it is in that dispatch's authorized paths and changed from the starting snapshot. A read-only reviewer has no authorized paths, so it never attests a change. Two concurrent dispatches with disjoint path sets each attest only their own paths.
- **Path outcomes.** Each recorded path is `Present(blob)` or `Deleted`; `git hash-object` runs only for `Present`. A rename is a `Deleted` old path plus a `Present` new path.
- **User grant attestation.** Phase 13's hook writes `<run folder>/subagent_grants.json`; no other writer. This phase defines its schema and reads it: `{"grants": {<grant id>: {...}}, "attestations": {<grant id>: {"task": str, "session_id": str, "agent_id": str, "approval_text": str, "approved_at": float, "ended_at": float, "paths": {<path>: <blob id> | "deleted"}}}}`. An entry under `attestations` is an authorized writer: E5 accepts a non-Markdown path whose blob equals an attestation's recorded blob (or `deleted`). A path that an open grant (under `grants`) holds is refused: `Refused: <path> is in subagent grant <task>, which is still open. Type end subagent: <task>, then commit again.` An absent file means no grants.
- **E5.** `worker_provenance.py verify --phase <id> --commit <c>`, called from the change-review hook on a `checkpoint(...)` commit and from `phase_reservation.py committed`. It refuses a non-Markdown path whose blob is not one of: a worker's last record for that path, an attestation's recorded blob, the phase baseline, a merged merge-branch tip. It also refuses a Claude-family turn that no `routed to claude` window covers. Message: `Refused: <path> is not what a worker left (the director or a subagent changed it). Send the change to a worker, then commit again.` It stays closed when it fails. Two separate checks: `verify --staged` reads the index (the PreToolUse hook runs before the commit exists), and `verify --commit <c>` reads the committed tree (`phase_reservation.py committed`).
- **E6.** `worker_provenance.py trailer` writes a `Workers:` trailer: worker names, thread id prefixes and a hash of `worker_turns.jsonl`; and a separate `Grants:` trailer line with each attestation's grant id and `agent_id`. `merge_checkpoint.py` holds a unit code checkpoint with no `Workers:` trailer: `Held: checkpoint <sha> has no worker record; its unit must commit again with worker_provenance.py trailer.` When the run folder is on the same machine, E5 runs again.
- **E7.** `assign.sh` refuses a Claude-family launch when no `routed to claude` entry is open: `Refused: workers run on Codex. A Claude worker starts only after Codex refused the work and the work was routed to Claude. No route is open.` `--model opus:xhigh` from a unit director is refused the same way; a showrunner is not checked. `/agent` writes a `routed to claude` entry with the new cause `user` when the user moves `direct` to Claude by hand. `scripts/accounts/moves.py` adds `user` to `RoutedToClaude.cause` and its check.
- Cost: under 200 ms for 50 files.

**Files:**
- `scripts/direct/worker_provenance.py` — new
- `scripts/direct/assign.sh` — the `start` and `record` calls; E7
- `scripts/direct/implement.sh` — `--paths`, the manifest, the `start` and `record` calls
- `scripts/direct/review.sh` — the manifest, the `start` and `record` calls
- `scripts/hooks/pre-tool-use-change-review-gate.py` — the `verify` call
- `scripts/direct/phase_reservation.py` — the `verify` call
- `scripts/production/merge_checkpoint.py` — the hold
- `scripts/whoami/quota_alert.py` — cause `user`
- `scripts/agents/agent_admin.sh` — cause `user`
- `scripts/accounts/moves.py` — `user` in `RoutedToClaude.cause`
- `scripts/direct/test_worker_provenance.py` — new
- `scripts/production/test_merge_checkpoint.py` — cases
- `scripts/hooks/test_change_review_gate.py` — cases
- `scripts/accounts/test_moves.py` — journal cases for `user`

**Workers:** `1 writer + 1 tester`
- `impl` — every script file; hub: `scripts/direct/worker_provenance.py`
- `test` — test files: a worker-only commit passes; a director edit after the worker's turn is refused with its path; an unrelated edit made before or during another worker's turn is never attributed to that worker; two concurrent dispatches with disjoint manifests each attest only their own paths; a turn with no manifest attests nothing; an `implement.sh` legacy manifest attests changed worktree paths; a read-only reviewer attests no path; a deleted path and a renamed path pass when a worker made them; an attested grant's path passes and an open grant's path is refused with the grant line; staged content that differs from the worktree is checked as staged; the staged check and the committed check each run alone; a merge-branch merge passes; Markdown passes; a Claude turn with no route is refused and one with a route passes; the move journal accepts and reads back `user`; a checkpoint with no trailer is held; the trailer lists grants apart from workers; 50 files under 200 ms

**Constraints from prior phases:**
- Phase 7: `assign.sh` turn end and `${PHASE_DIR}`; `--paths` writes `${PHASE_DIR}/dispatch_<n>_paths.json` (`worker`, `round`, `paths`) for `implement`, `test`, `fix`, and is refused for read-only roles; exit 4 (handed off) records no completion, so its turn line names the destination run and attests nothing.
- Phase 9: Claude workers open only under a `routed to claude` entry, as a `WorkerRoutedToClaude` ledger backend; `moves.py` already holds the `outage` cause.
- Phase 4: the worker address is the stable key in every record; the shown name is display only.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks scripts/production scripts/whoami scripts/accounts` is 0/0.

### Phase 11 — `/unit:assign` and `/showrunner:assign` (WT P4)  · status: todo

**Depends on:** Phase 7, Phase 8

#### Work Order

**Goal:** a unit director and a showrunner each launch a worker with one skill.

**Spec:**
- `/unit:assign <role> [--to <worker>] [--model m[:e]] [--paths <file>] <prompt file> [...]` composes the call: `--session-dir` is the run, `--cwd` the worktree, `--phase` the open phase.
- `/showrunner:assign`: same skill, showrunner defaults: `--session-dir ~/.local/state/unit-workers/<slug>/showrunner/`, `--cwd` the showrunner checkout. It allows `research`, `propose` and `review` of a document. `implement`, `test` and `fix` are refused with one line: a showrunner never writes code; its units do.
- **The showrunner's ledger.** `workers.ledger_for_run()` resolves a ledger from the run's progress-state `production` and `unit` keys, then the plan header, else an adhoc key; the showrunner folder has neither. So `assign.sh`, on first use of the showrunner form, creates the folder and runs `progress_history.py set-unit --session-dir <folder> --production <slug> --unit showrunner` (Phase 5). `ledger_for_run()` then resolves `~/.local/state/unit-workers/<slug>/showrunner.json`. `resolve`, reviewer allocation and `workers.py stop` at wrap all address that one file.
- The showrunner's ledger file gets `code` reviewers by the Phase 8 rule, for document and plan reviews. `/astra_review` keeps its held-role path. Showrunner workers end at production wrap, with the production's reviewers (user, 2026-10-28).
- Both skills print the worker address, its shown name (`workers.shown_name()`, Phase 7), the model and effort used, and the summary path. The address stays raw for `--to` and routing. They read no transcripts and hold no state.

**Files:**
- `commands/unit/assign.md` — new
- `commands/showrunner/assign.md` — new
- `scripts/direct/assign.sh` — showrunner session-dir default, `set-unit`, role refusal
- `commands/showrunner/produce.md` — the wrap step (step 2, next to `review_pool.py stop`) runs `workers.py stop` for the showrunner's workers
- `scripts/direct/workers.py` — `stop` covers the showrunner ledger file
- `scripts/direct/test_assign_launcher.py` — cases
- `scripts/direct/test_workers.py` — cases

**Workers:** `1 writer + 1 tester`
- `impl` — the two commands, `produce.md`, `assign.sh`, `workers.py`
- `test` — the showrunner form refuses `implement`; the showrunner folder gets `production` and `unit: showrunner` and `ledger_for_run()` gives `unit-workers/<slug>/showrunner.json`; `resolve`, reviewer allocation and `workers.py stop` touch that same file; both forms print address, shown name, model and summary path; `workers.py stop` ends the showrunner's workers and its reviewers

**Constraints from prior phases:**
- Phase 5: `progress_history.py set-unit` writes the keys that `ledger_for_run()` reads first.
- Phase 7: call form, `--paths`, exit codes (4 = handed off), `workers.shown_name()` and the raw address.
- Phase 8: reviewers in the ledger as `ReviewerPayload` and the owner rule.
- Phase 4: lock order ledger, giving roster, taking roster, inbox; quarantined entries are kept; fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct` is 0/0.

### Phase 12 — The rule and the cutover (WS P1 + WT P5, merged: both change the same docs)  · status: todo

**Depends on:** Phase 5, Phase 8, Phase 9, Phase 10, Phase 11

#### Work Order

**Goal:** the worker rule lives in one doc and names `assign.sh`; every doc call form moves to `assign.sh` in one commit; a unit that switches can no longer use the old launchers.

**Spec:**
- New `docs/direct/worker_rule.md` defines `<WorkerRule/>`:
  > A unit director gives all implementation, test, repair and review work to its workers. A worker is a long-lived Codex thread that `assign.sh` opens and resumes. The director does not start a Claude subagent that writes, does not write code, and does not run a coding agent by hand. Tools enforce this rule. A showrunner cannot grant an exception. Only the user can: with the switch in `settings.json`, or for one subagent task by typing `allow subagent: <task name>` in the director session. A granted task's changes are attested and checked like a worker's.
  It covers unit directors only; showrunners and the natedev config shell keep their own rules.
- Pointers, no repeated text: `commands/unit/direct.md` gets a `<TagReferenceContract/>` row; `<LongLivedWorkers/>` becomes one line that points to `<WorkerRule/>`; `<CoreContract/>` keeps "does not write implementation code" and its exception "agreed doc-only/trivial post-review fixes" becomes "Markdown only". `docs/production_format.md`: the **Worker** definition and a new `<ProductionUnit/>` item 19 point to `<WorkerRule/>`. `commands/showrunner/produce.md`: one line — the showrunner never approves a flow without workers and sends an exception request to the user.
- The design check (screenshots) moves to a UI reviewer (`assign.sh --role review --image`) when there is no `ui` pool.
- In the same commit, call forms change to `assign.sh`: `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerLaunchFailure/>`), `docs/direct/launch_implementation.md` step 5, `docs/direct/dual_review.md` step 3, `docs/direct/phase_end.md`, `commands/plan/phase_review.md`. Each writer call form passes `--paths` with the files the Work Order's **Workers** line gives that worker. Each doc names exit 4 (handed off: completion unknown, never resubmit).
- **`workers.py switched --unit <unit>` is recoverable.** It writes the unit under `"state": "switching"` with the date into `~/.local/state/unit-workers/<slug>/switched.json`, runs `import-pool` (Phase 8) for it, then sets `"state": "switched"`. A rerun after a crash at any point finishes the switch; `import-pool` is idempotent.
- `implement.sh` and `review.sh`: the first thing each does is check the run's unit (`SHOWRUNNER_UNIT_ID`, else `unit_runs.marker`) against `switched.json`. A unit in either state gets exit 2: `refused: <unit> switched to assign.sh on <date>; use assign.sh` (state `switched`) or `refused: <unit> is switching to assign.sh; use assign.sh` (state `switching`).
- **Switched-unit integration.** A switched unit's transfer and rename keep every writer and reviewer thread id: Phase 5's `move_unit` moves the ledger, Phase 8 rebinds its reviewers, and the giving pool's `review_pool.py retire` skips its `Transferred` reviewers. This phase tests that path end to end.
- `commands/showrunner/produce.md`: the showrunner asks each unit director, one at a time, to switch, and runs `workers.py switched` on a yes. It asks first the units with a finished run or no run (style, messaging, builds), because only `assign.sh` starts or reopens their run.
- New tag-reference test: every `<Tag/>` that `commands/unit/direct.md` and `docs/direct/*.md` name resolves to a definition.
- New naming gate check (naming rule 5), tests-only: it fails on a new use of "seat" or "seats" as a role word in scripts, commands and docs. It allows an existing stored key, flag, file name or transport field that has a note next to it, and it does not read `docs/plans/`.

**Files:**
- `docs/direct/worker_rule.md` — new
- `commands/unit/direct.md`
- `docs/production_format.md`
- `commands/showrunner/produce.md`
- `docs/direct/launch_implementation.md`
- `docs/direct/dual_review.md`
- `docs/direct/phase_end.md`
- `commands/plan/phase_review.md`
- `scripts/direct/workers.py` — `switched`
- `scripts/direct/implement.sh` — the refusal
- `scripts/direct/review.sh` — the refusal
- `scripts/hooks/test_tag_reference.py` — new
- `scripts/hooks/test_worker_naming.py` — new: the naming gate check
- `scripts/direct/test_workers.py` — cases
- `scripts/direct/test_implement_launcher.py` — cases
- `scripts/direct/test_review_launcher.py` — cases
- `scripts/production/test_transfer_unit.py` — switched-unit transfer case

**Workers:** `2 writers` — docs and scripts split.
- `impl` — `workers.py`, `implement.sh`, `review.sh`, `test_workers.py`, `test_implement_launcher.py`, `test_review_launcher.py`, `test_transfer_unit.py`: a crash after `import-pool` and before `"state": "switched"` is finished by a rerun; a legacy review allocation during the switch never gets a `Transferred` reviewer; both refusal lines; a switched unit's transfer keeps every writer and reviewer thread id
- `test` (opens as impl) — every doc and command file, `test_tag_reference.py`, `test_worker_naming.py`; hub: `commands/unit/direct.md`

**Constraints from prior phases:**
- Phase 5: `move_unit` and `rename_unit` move the ledger as a journalled migration (both ledger locks in path order); pause keeps work and retire alone ends it.
- Phase 7: help, diagnostics and printed names in `codex_mesh.py` no longer say "seat"; stored compatibility fields keep their names with a note. `assign.sh` exit codes 0–4; `--paths` is required for writer roles.
- Phase 8: `workers.py import-pool --unit` marks pool reviewers `Transferred` before the ledger entry is written, and is idempotent; legacy `review_pool.py` refuses or skips `Transferred`.
- Phase 9: `blocked:` exit 3. Phase 10: E4 lines from all three launchers; the grant attestation schema in `subagent_grants.json`. Phase 11: `/unit:assign`.
- Phase 4: lock order ledger, giving roster, taking roster, inbox; quarantined entries are kept; fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks` is 0/0; the tag-reference test resolves `<WorkerRule/>`; a grep test finds no "spawn a fresh Claude Agent helper" and no `implement.sh --to` in `docs/direct/`; a switched unit's `implement.sh --to` call exits 2 with the refusal line; a unit not yet switched runs as before; the naming gate check passes, and fails on a new "seat" role word.

### Phase 13 — Worker gate E1–E3 (WS P5)  · status: todo

**Depends on:** Phase 9, Phase 10, Phase 12
**Approved:** the user typed the go for the `settings.json` registration in the models session (2026-10-28, reported by the models director).

#### Work Order

**Goal:** in a unit director session, a hook refuses writer subagents, code edits and coding agents started by hand; a user grant opens exactly one subagent.

**Spec:**
- settings.json change: register `scripts/hooks/pre-tool-use-worker-gate.py` as a PreToolUse hook for `Agent|Task`, `Edit|Write|MultiEdit|NotebookEdit` and `Bash`, and `scripts/hooks/user-prompt-subagent-grant.py` as a UserPromptSubmit hook; the checkpoint body names this change so the showrunner gates it with the new blob.
- **E1** (`Agent|Task`): any `subagent_type` except `Explore`, `Plan`, `claude-code-guide` is refused; `fork` and `general-purpose` are refused. Message: `Refused: a unit director sends worker work to its workers, not to a subagent. Use assign.sh --role implement|test|fix|review. For read-only search, use subagent_type Explore. Rule: docs/direct/worker_rule.md.`
- **User grant for E1** (user, 2026-10-28: "there are times where we can ask subagents to do work but i want that to be explicitly chosen by me - default is to use workers"). The default is refuse. The user opens one subagent task by typing a prompt line `allow subagent: <task name>` in the director session. `scripts/hooks/user-prompt-subagent-grant.py` writes the grant (grant id, session id, task name, the approval text, time; state `open`) under `grants` in `<run folder>/subagent_grants.json`, in the schema Phase 10 defined. The hook skips a prompt that holds a `<cross-session-message`, `<agent-message` or `<task-notification` frame, so a peer, a subagent or a notice cannot open a grant. E1's refusal message names the grant line. E2 and E3 refuse any director write to `subagent_grants.json`, so only the hooks write it.
- **Grant binding to one subagent.** The hook payload of a subagent's tool call carries its `agent_id`.
  - E1 allows one `Agent|Task` call whose `description` equals an `open` grant's task name, and sets that grant to `launched`. While a grant is `launched`, E1 refuses another granted launch in that session.
  - The first tool call after that, in that director session, whose `agent_id` no grant holds binds that `agent_id` to the `launched` grant (state `bound`).
  - From then on, only that `agent_id` has the grant. A sibling that reuses the description is refused by E1 (the grant is no longer `open`). A descendant subagent has another `agent_id`, so E2 refuses its writes; descendants are not covered.
  - E2 allows writes from the bound `agent_id`, and appends each written path to the grant's path list.
- **Grant end and attestation.** `end subagent: <task name>` deletes the grant from `grants`, so E1 and E2 allow nothing more for it. In the same write, the hook records an attestation under `attestations`: grant id, task, session id, `agent_id`, approval text, `approved_at`, `ended_at`, and `{path: blob id | "deleted"}` for each path the grant wrote (`git hash-object`). Phase 10's E5 accepts those blobs as an authorized writer. A run that ends with a grant still open leaves no attestation, so E5 refuses its paths.
- **E2** (`Edit|Write|MultiEdit|NotebookEdit`): in a director session and its subagents, a write to a non-Markdown file in the unit's worktree or a repository the run holds, except from a bound grant's `agent_id`. `/tmp` and the run folder are allowed. Message: `Refused: a unit director does not edit code. Send this change to your impl worker: assign.sh --role implement …. Markdown files stay open to you.`
- **E3** (`Bash`): `codex exec`, `codex` with a prompt, `claude -p`/`--print`/`--bg`, a direct `agent_exec.sh` or `agent_bg.sh` call, and the implementation mode of `/ask_a_friend`. Allowed: `assign.sh`, `implement.sh`, `review.sh`, `workers.py`, `codex_mesh.py`, `roles.py`, `astra_review.py`, `review_pool.py`. Message: `Refused: start coding agents only with assign.sh. It opens, resumes and compacts your workers.`
- Director session: `SHOWRUNNER_UNIT_ID` is set, or `direct_run.active_run(session_id)` names a run. A Claude worker is not a director: allow a session the ledger names as a Claude worker (`ClaudeSession` or `WorkerRoutedToClaude` backend), or one with `UNIT_WORKER=<address>`.
- On any hook fault, allow and write one log line, as the change-review hook does. Off switch: `UNIT_WORKER_GATE=off` in the `env` block of `settings.json`, the user's alone.
- Before registering, replay the hook over the last 7 days of director tool calls and report each refusal that would have happened, by unit and by kind.

**Files:**
- `scripts/hooks/pre-tool-use-worker-gate.py` — new
- `scripts/hooks/user-prompt-subagent-grant.py` — new
- `scripts/hooks/test_worker_gate.py` — new
- `scripts/hooks/test_settings_hook_paths.py` — new: every hook `command` in `settings.json` that names a `scripts/hooks/` file names one that exists
- `settings.json` — the registration

**Workers:** `1 writer + 1 tester`
- `impl` — `pre-tool-use-worker-gate.py`, `user-prompt-subagent-grant.py`, the replay, `settings.json`
- `test` — `test_settings_hook_paths.py` (fails on a hook path that names a missing file, for example the old `session-start-delegate-resume.py`); `test_worker_gate.py`: each matcher; worker exemption by ledger and by `UNIT_WORKER`; subagent edit refusal; a typed grant opens one named task and binds the first new `agent_id`; a sibling with the same description is refused; a descendant's write is refused; `end subagent:` deletes the grant and writes an attestation with each path's blob, which `worker_provenance.py verify` accepts; a grant line inside a cross-session, agent-message or task-notification frame opens nothing; a director write to the grant file is refused; fail-open with a log line; the off switch

**Constraints from prior phases:**
- Phase 1: the `compact` hook names `scripts/hooks/session-start-direct-resume.py`. No test checks that a `settings.json` hook command names a file that exists; this phase adds `test_settings_hook_paths.py`.
- Phase 9: self-heal exists, so a refused director always has a worker it can start; a Claude worker's ledger backend is `ClaudeSession` or `WorkerRoutedToClaude`.
- Phase 10: the `subagent_grants.json` schema (`grants`, `attestations`); E5 accepts an attestation's blobs and refuses a path of an open grant.
- Phase 12: the rule doc is `docs/direct/worker_rule.md`, and it names the `allow subagent:` grant.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/hooks` is 0/0; the replay shows 0 refusals of `Explore`, `assign.sh`, `implement.sh` or `review.sh` calls.

### Phase 14 — Gate announce and the three-day audit (WS P6, without the as-built)  · status: todo

**Depends on:** Phase 13
**Blocked by:** three days after the gate goes on, for the audit report.

#### Work Order

**Goal:** every unit knows the gate is on, and an audit after three days shows no unauthorized writer subagents.

**Spec:**
- One `/unit:announce` when the gate goes on: the rule, `assign.sh`, the `allow subagent:` grant, and the `blocked:` path.
- `workers.py audit` reports, for each live unit:
  - its workers, each as the shown name (`workers.shown_name()`, Phase 7) next to its raw address;
  - its authorized grants since the gate went on (from `subagent_grants.json` attestations: task, `agent_id`, approval time), listed apart from unauthorized writer subagents (target 0);
  - its worker replacements;
  - its quarantined ledger entries (`quarantined_workers` and `quarantined_adoptions`: name, reason, ledger path);
  - every E1–E7 refusal with its text (from the hook log and the `Refused:`/`Held:` lines).

**Files:**
- `scripts/direct/workers.py` — `audit`
- `scripts/direct/test_workers.py` — audit cases

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`
- `test` — the audit lists every live unit; it counts unauthorized writer subagents and lists attested grants apart; it names each quarantined entry with reason and ledger path; it names each refusal; each row shows the shown name and the raw address, and the label follows a rename

**Constraints from prior phases:**
- Phase 4: ledger and its quarantine (`quarantined_workers`, `quarantined_adoptions`, each with the original record and reason; kept until retire). Phase 5: retire reports each removed quarantined entry. Phase 7: `workers.shown_name()`; the address is the stable key. Phase 9: `replaced` events. Phase 10 and Phase 13: refusal texts and the `subagent_grants.json` schema.

**Acceptance gate:** the merge gate is green; the three-day audit lists every live unit with unauthorized writer subagents = 0.

### Phase 15 — Remove the old launchers; write the as-built (WT P6 + WS P6 as-built)  · status: todo

**Depends on:** Phase 12, Phase 14
**Blocked by:** every unit of every running production is in `switched.json`.

#### Work Order

**Goal:** `assign.sh` is the only worker launcher, and one as-built doc describes workers.

**Spec:**
- Remove `scripts/direct/implement.sh`, `review.sh`, `review_pool.py` and their tests (`test_implement_launcher.py`, `test_review_launcher.py`, `test_launcher_modes.py`, `test_review_pool.py`). Port any case still needed to `test_assign_launcher.py` first.
- Before the removal, `commands/showrunner/produce.md` calls `workers.py` in place of `review_pool.py`: the wrap step's `review_pool.py stop` becomes `workers.py stop`, and the unit retire line's `review_pool.py retire` becomes the `workers.py` retire call. Before each delete, grep for callers of the file; every operational caller uses `assign.sh` or `workers.py`.
- The production scripts drop their `review_pool.py` calls; every reviewer is in a ledger by now (`Transferred` in the pool): `retire_unit.reviewers()` and `stop_rest()` (`POOL` `status` and `retire`), `transfer_unit._move_state()` and `_leased()` (`POOL`), and `rename_unit._move_state()` (`import review_pool`, `review_pool.rename_unit()`).
- `review_pool_report.py` reads only the ledger. `worker_provenance.py record` has one caller, `assign.sh`. The worker gate's E3 allow list drops `implement.sh`, `review.sh` and `review_pool.py` (hook code; no `settings.json` change).
- The "no `implement.sh`" gate covers active scripts, commands and `docs/as-built/` only. It does not read `docs/plans/` or the grep test's own search text.
- Write `docs/as-built/workers.md`; remove `docs/as-built/long-lived-workers.md` and `docs/as-built/review-pool.md`.

**Files:**
- `scripts/direct/implement.sh` — removed
- `scripts/direct/review.sh` — removed
- `scripts/direct/review_pool.py` — removed
- `scripts/direct/test_implement_launcher.py` — removed
- `scripts/direct/test_review_launcher.py` — removed
- `scripts/direct/test_launcher_modes.py` — removed
- `scripts/direct/test_review_pool.py` — removed
- `scripts/direct/review_pool_report.py`
- `scripts/direct/assign.sh` — the only `worker_provenance.py` caller
- `scripts/production/retire_unit.py` — no `review_pool.py` call
- `scripts/production/transfer_unit.py` — no `review_pool.py` call
- `scripts/production/rename_unit.py` — no `review_pool` import
- `commands/showrunner/produce.md` — `review_pool.py stop` and `retire` calls move to `workers.py`
- `scripts/hooks/pre-tool-use-worker-gate.py`
- `scripts/hooks/test_worker_gate.py`
- `scripts/direct/test_review_pool_report.py` — ledger-only cases
- `scripts/direct/test_assign_launcher.py` — ported launcher cases
- `scripts/production/test_retire_unit.py`, `scripts/production/test_transfer_unit.py`, `scripts/production/test_rename_unit.py` — pool-call cases removed or moved to the ledger
- `scripts/hooks/test_old_launchers_gone.py` — new: the "no `implement.sh`" grep test
- `docs/as-built/workers.md` — new
- `docs/as-built/long-lived-workers.md` — removed
- `docs/as-built/review-pool.md` — removed

**Workers:** `1 writer + 1 tester`
- `impl` — removals, `review_pool_report.py`, `assign.sh`, `retire_unit.py`, `transfer_unit.py`, `rename_unit.py`, `produce.md`, the hook, the as-built
- `test` — `test_worker_gate.py`, `test_review_pool_report.py`, the ported cases in `test_assign_launcher.py`, the pool-call updates in `test_retire_unit.py`, `test_transfer_unit.py` and `test_rename_unit.py`, and `test_old_launchers_gone.py`: no active script, command or `docs/as-built/` file names `implement.sh` or calls `review_pool.py`

**Constraints from prior phases:**
- Phase 8: the reviewer state types and `ReviewerType` live in `workers.py`; `review_pool.py` only imports them, so its removal breaks no import. Every unit's pool reviewers are `Transferred` after its `switched`.
- Phase 12: `switched.json` path, its `switching`/`switched` states and the refusal. Phase 13: the allow list.
- Phase 4: the ledger, its lock order (ledger, giving roster, taking roster, inbox) and its quarantine stay as they are.

**Acceptance gate:** the merge gate is green (the lib suite included); basedpyright on `scripts/direct scripts/hooks scripts/production` is 0/0; the tag-reference test resolves every tag; no active script, command or `docs/as-built/` file names `implement.sh`.
