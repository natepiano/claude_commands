# Long-lived workers and one worker tool

> **Status: IMPLEMENTATION PLAN — phased, direct-ready; approved by the user 2026-10-28 (relayed by the showrunner).** One launcher, `assign.sh`, opens and resumes long-lived workers that belong to the unit. Tools refuse worker work that a unit director does any other way.

> **As-built disposition: create** — `docs/as-built/workers.md`; it replaces `docs/as-built/long-lived-workers.md` (Phase 15). roles-unit (sdlc) removes `docs/as-built/review-pool.md` with `review_pool.py` (showrunner, 2026-10-29).

> **Production: natedev** — unit `models-unit`; production doc `docs/plans/natedev-production.md`

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
  - `settings.json` — SessionStart `compact` hook at line 294
  - `scripts/hooks/session-start-direct-resume.py` — the resume hook the compact entry must name
  - `scripts/direct/implement.sh` — writer launcher; claim code `worker_record` at 219–400; `agents_resolve` at 206; positionals at 85–93; `--fix-cause` at 65
  - `scripts/direct/review.sh` — review launcher; `claim_follow` at 297–363; `check-launch` at 251; turn record at ~454–489; tree guard at ~805–820
  - `scripts/direct/review_pool.py` — pool: `_prime`, `_choose`, `_repoint`, `allocate` (~536–599), `release`, `retire`, `migrate`, `stop`, `rename_unit`; `main` at 788
  - `scripts/direct/review_tree.py`, `scripts/direct/review_pool_report.py`
  - `scripts/agents/codex_mesh.py` — `WORKER_ROLES` at 422 (used by `_roster_role()` 694, `_write_roster_record()` 1412, and the `start` 8346, `follow` 8378 and `can-follow` 8434 parsers); `command_reopen`, `_finished_run`, `prime`, `fork`, `set-instructions`, `follow`, `stop --pause`
  - `scripts/agents/agents_config.sh` — `_agents_validate_pair` at 297, `agents_resolve` at 330, `agents_resolve_print` at 494
  - `scripts/agents/sync_codex_catalog.sh` — writes `[codex.agents]`; incomplete-cache rule at ~261–279
  - `scripts/agents/agent_admin.sh` — backend of `/agent`; `commands/agent.md`
  - `config/agents.conf` — `[codex.agents]`, `[claude.agents]`, `[direct.codex]`; the one registry on every machine, with no local overlay
  - `scripts/agents/agent_bg.sh` — named background Claude sessions
  - `scripts/direct/progress_history.py`, `end_session.sh`, `remove_workers.py`, `prepare_session.sh`, `phase_reservation.py`
  - `scripts/production/retire_unit.py`, `transfer_unit.py`, `rename_unit.py`, `merge_checkpoint.py`, `merge_gate.py` (`TEST_SUITES` at 63)
  - `scripts/hooks/pre-tool-use-change-review-gate.py`, `scripts/production/change_review.py` — the checkpoint commit hook
  - `scripts/whoami/quota_alert.py` — `routed to claude` routing; `moves.RoutedToClaude`
  - `scripts/astra/design_roles.py`, `scripts/astra/astra_review.py`, `scripts/agents/mesh_runs.py`, `scripts/message/send.py`
  - `commands/unit/direct.md` — `<TagReferenceContract>` at 141–177, `<LongLivedWorkers>` at 179–216, `<WorkerLaunchFailure/>`
  - `docs/direct/launch_implementation.md` (step 5), `docs/direct/dual_review.md` (step 3), `docs/direct/phase_end.md`, `commands/plan/phase_review.md`, `docs/production_format.md`, `commands/showrunner/produce.md`
  - `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/mod.rs` — `HierarchyRole` at 191, `text` at 206–218, `RoleFallback` at 299
- **Test lanes:** `scripts/direct/test_*.py` (suite `direct`); `scripts/agents/test_*.py` plus `test_agents_config.sh`, `test_sync_codex_catalog.sh`, `test_launcher_tasks.sh`; `scripts/hooks/test_*.py`; `scripts/production/test_*.py`; `scripts/whoami/test_quota_alert.py`; `scripts/astra/test_design_roles.py`; cargo handler: its crate tests. Docs and commands have no lane until Phase 12 adds the tag-reference test.
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

**Order (showrunner, 2026-10-29):** the remaining phases run in this order: 5, 7, 11, 6, 9, 10, 12, 13, 14, 15. Phase 8 is dropped. Phase numbers do not change.

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

### Phase 5 — Worker lifecycle: run end, phase end, retire, transfer, rename (WS P2, part 2)  · status: done

#### As-built

- Workers live until their unit is retired. Run end, transfer and rename pause workers; retire alone ends them. The scope is writer workers (`impl`, `test`, `fix`) and the lifecycle every ledger entry shares; pool reviewers keep the `review_pool.py` path (transfer still calls `review_pool.py retire`, rename `review_pool.rename_unit()`).
- `workers.live_unit_director()` reads `unit_runs.director`, live sessions and shutdown history, so a director restarted on another account counts as live. While the director lives, `codex_mesh.py stop --pause` prints `kept workers and app-servers: unit director is live` and writes nothing. A run marker or a run folder alone does not prove the director is alive.
- Pause (`workers.pause_run`, `pause_unit`) keeps pending messages (it moves each server-queued message into `<name>.pending.json` before it reaps), `continuation_owed`, detached runs and the reservation fields. It reaps only the servers this run folder records. Ownership stays `Paused` until adopt or retire.
- Adopt carries a paused worker's queue (`AdoptionQueuePolicy`; old journals keep the legacy refuse). The taking run's first `follow` delivers detached results, then pending messages, then the owed continuation, before new work, and sends no prompt twice.
- `workers.end_unit(production, unit)` cancels and names each pending message, detached run (signalled only through `mesh_runs.supervises`), owed continuation and reservation; ends each worker (Codex through `command_end`, Claude through `claude rm`); prints each removed quarantined entry; deletes the ledger. A worker it cannot end prints `cannot end worker <w> in <run>; retry retirement`. `retire_unit.py` `main()` refuses on `WorkerUnavailable` and `shutdown_record.InvalidRecord`.
- `workers.move_unit` (transfer) and `workers.rename_unit` (rename) are one journalled migration (`<unit>.move.json`, error `worker ledger moved to <dest>; retry using current unit identity`). It finishes pending adoptions first, takes both ledger locks in path order, rewrites every roster `unit_ledger`, progress-state `production`/`unit` and adoption path, moves quarantine and reservation fields unchanged, and refuses a destination with other owners. `rename._rename_unit_workers` moves each ledger of a production.
- Rename and transfer run `stop --pause` and `workers.pause_unit`. Rename refuses with `detached run <id> of <w> works inside <worktree>; rerun apply once it ends` and kills nothing; transfer keeps the worktree and never refuses for this.
- `codex_mesh.py end --expected-thread-id <id> --idle-only` takes the inbox lock, then the roster lock, under one 5 s budget; a refusal exits nonzero and changes nothing. A retry on an ended worker drains and interrupts again.
- Run end runs no server sweep. Server reclaim is codex_mesh idle-stop and retired-server cleanup; `_busy_threads` keeps a server that has queued work.
- The board slot (`_post_board_status`) and the identity path (`worker_state.worker_record_path`) come from the stored role and the address. Identity records carry no `role`; the roster `role` is the current assignment, and a worker opened as `review` can be followed as `impl`.
- `workers.py resolve --owner-pid <pid>`; the default owner is the live director's pid. With no live director and no `--owner-pid`, it exits 2: `run director pid is unknown; pass --owner-pid <pid>`. `progress_history.py set-unit --session-dir <run> --production <slug> --unit <unit>` writes the keys `ledger_for_run()` reads first.

**Files:**
- `scripts/direct/workers.py` — ledger lifecycle: pause, end, move, rename, live director, retained detached runs, held Claude sessions; CLI `resolve` only
- `scripts/agents/codex_mesh.py` — pause keeps work, adopt queue, first-follow drain, guarded `end`, live-director keep in `stop --pause`, `_busy_threads`
- `scripts/agents/mesh_runs.py` — public `supervises()` on resolved paths
- `scripts/direct/worker_state.py` — `worker_record_path()` by address
- `scripts/direct/implement.sh`, `scripts/direct/review.sh` — identity without `role`
- `scripts/direct/progress_history.py` — `set-unit`
- `scripts/direct/end_session.sh` — `stop --pause` per run, no sweep
- `scripts/direct/remove_run_scratch.py`, `scripts/lib/scratch.py` — keep a retained detached command's cwd (`end(keep=)`)
- `scripts/direct/remove_workers.py` — keeps ledger Claude workers and workers of live-director runs; no sweep
- `scripts/production/retire_unit.py` — `end_unit`; refuses on `WorkerUnavailable`
- `scripts/production/transfer_unit.py`, `rename_unit.py`, `rename.py` — `move_unit`, `rename_unit`, `pause_unit`, the detached-run refusal
- `docs/as-built/agent-registry.md`, `docs/as-built/long-lived-workers.md` — run end without sweeps

**Binds later work:** the `workers.py` CLI has only `resolve` (no `stop`); `end_unit()` is a library call. `assign.sh` passes `--owner-pid $$` to `resolve`, `--fix-cause` with `--role fix`, and a prompt whose phase line matches the phase. Self-heal extends `end_unit` and `held_claude_sessions` for a routed worker, and a replacement end passes `--expected-thread-id`. No non-ledger file (for example `switched.json`) goes under `unit-workers/<slug>/`; the worker gate and audit read ledgers without `locked()`.

**Gotchas:**
- `UnitWorkerLedger.locked()` saves on exit and finishes move journals. Code that globs `unit-workers/<slug>/*.json` and opens each file through `locked()` rewrites it as a ledger (`held_claude_sessions()` and `rename._rename_unit_workers` do this).
- Inside one run, launchers and `end` take the inbox lock, then the roster lock. Adoption holds the roster and only tries the inbox without blocking.
- `progress_history.py start-pass`/`check-launch` refuse a `fix` launch with no findings round and no `--fix-cause`, and a prompt whose `Phase <id> — <title>` line names another phase.
- Test fixtures that copy scripts go stale when an import appears. Symlink the real script, or put the repo root on the shim path. Timing and CPU-share tests use counted ticks, not wall clock.

**Ruled out:** a live-director veto inside server reclaim (`_judge`) — an empty server is reclaimed while the director lives; run-end server sweeps (`sweep --stop`, `sweep_app_servers`); a fixture-only `director_pid` state key; role in durable identity.

**Deferred:** held run results reach the director when an account move fails; a next item after the `/unit:assign` and `/showrunner:assign` work, in no Work Order.

### Phase 6 — Role words `research` and `propose` (WT P2)  · status: todo

**Depends on:** Phase 2, Phase 4, Phase 7, Phase 11 (runs after Phase 11; showrunner, 2026-10-29)

#### Work Order

**Goal:** the roster, the unit worker ledger, the hierarchy edge, `assign.sh` and the cargo handler know the roles `research` and `propose`, and both open read-only.

**Spec:**
- Three role registries take the two words. Update all three:
  - `codex_mesh.WorkerRole = Literal["impl", "test", "fix", "review", "research", "propose"]`
  - `codex_mesh.WORKER_ROLES = ("impl", "test", "fix", "review", "research", "propose")`
  - `workers.ROLES = ("impl", "test", "fix", "review", "research", "propose")`; today `workers.py resolve --role` and `workers._resolve()` refuse any other word before allocation.
- Check every use of `WORKER_ROLES` by symbol: `_roster_role()`, `_write_roster_record()`, and the `start`, `follow` and `can-follow` parsers in `main()`. Check every `WorkerRole` annotation (the roster `TypedDict`s, `_worker_role()`, `_set_roster_assignment()`).
- **Access policy.** `codex_mesh.open_unit_worker()` today makes only `review` read-only (`{"kind": "ReadOnlyReviewer"}` and `READ_ONLY_SANDBOX`). `research` and `propose` open the same way: `ReadOnlyReviewer` policy and `READ_ONLY_SANDBOX`. The extra writable root for `propose` (`~/.local/state/plan-backlog`) comes with `assign.sh`.
- **`assign.sh` takes the two roles (showrunner, 2026-10-29).** Phase 7 built four roles. This phase adds `--role research` and `--role propose` as Phase 7's spec describes them: the `--summary` report, the one proposal file, the launcher's `ledger.md` line, the read-only sandbox and the `propose` writable root. `/showrunner:assign` (Phase 11) then allows both.
- The hierarchy edge JSON `{"manager","member","role"}` carries the word.
- `scripts/direct/board.sh` lists roles in two places: the handoff check `role_lead='^role=(impl|test|fix|review)( |$)'` (:124) and the `case` in `cmd_role` (:505–507). Add `research` and `propose` to both. The board slot is the stored role, so these workers post under slots `research` and `propose`.
- `config/agents.conf [direct.codex]`: `research=astra:xhigh`, `propose=astra:xhigh` (user, 2026-10-28; the model `/astra_review` uses for documents).
- Cargo handler: `HierarchyRole` gains `Research` and `Propose` (serde lowercase) with their `text`; an unknown word still takes `RoleFallback`. This is a separate repository; its commit lands there, and the checkpoint body names it.

**Files:**
- `scripts/agents/codex_mesh.py` — `WorkerRole`, `WORKER_ROLES`, `open_unit_worker()` access policy
- `scripts/direct/workers.py` — `ROLES`
- `scripts/direct/board.sh` — the two role lists
- `scripts/direct/assign.sh` — `--role research|propose`
- `scripts/direct/test_assign_launcher.py` — `research` and `propose` cases
- `config/agents.conf` — two rows
- `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/mod.rs` — two variants
- `scripts/agents/test_codex_mesh.py` — role cases
- `scripts/direct/test_workers.py` — `resolve` cases for both roles

**Workers:** `2 writers` — the two repositories split.
- `impl` — `codex_mesh.py`, `workers.py`, `assign.sh`, `config/agents.conf`, `test_codex_mesh.py`, `test_workers.py`, `test_assign_launcher.py`: `workers.resolve` opens a `research` and a `propose` worker, each with `ReadOnlyReviewer` and the read-only sandbox; the roster and the edge carry the word; `propose` writes the file and the launcher adds the ledger line once
- `test` (opens as impl) — `census/mod.rs` and its crate tests

**Constraints from prior phases:**
- Phase 2: `[direct.codex]` rows take family words; `astra` resolves through `[codex.families]` to the newest cached release that has a `[codex.agents]` effort row (today `gpt-6-astra`, which has a `weights.py` row). `gpt-6.1-terra` and `gpt-6-luna` have no `weights.py` row: usage accounting raises `UnweightedModel` for them, so no row may name `terra` or `luna`.
- Phase 4: `codex_mesh.open_unit_worker(session_dir, name, role)` resolves `agents_resolve direct.<role>` and refuses a non-Codex family (`WorkerUnavailable("new persistent worker requires a Codex assignment")`); it records `access_policy` in the roster, and `workers._worker_from_record()` copies it to the ledger entry. Ledger lock order: ledger, giving roster, taking roster, inbox. Fixtures block until their deadline and keep `FixtureLoopBudget`.
- Phase 7: `assign.sh` takes `implement`, `test`, `fix` and `review`; its call form, artifacts and exit codes.
- Phase 5: `_post_board_status()` takes the board slot from the roster `role`. `worker_state.worker_record_path()` keys the identity file by address (a `fix` role uses `impl_worker_fix.json`; other roles use the slot). Role is not durable identity.

**Acceptance gate:** the merge gate is green; `verify.sh test cargo-handler` and `verify.sh lint cargo-handler` pass; basedpyright on `scripts/direct scripts/agents scripts/production scripts/astra` is 0/0; the handler shows `research` and `propose`, and an unknown word takes `RoleFallback`.

### Phase 7 — `assign.sh` core (WT P1, with WS P2's `--role`, run start and rules block)  · status: done

**Depends on:** Phase 2, Phase 4, Phase 5

#### Work Order

**Goal:** one launcher with named options opens or resumes the right worker for four roles; `implement.sh` and `review.sh` do not change.

**Spec:**
- **Roles in this phase (showrunner, 2026-10-29).** This phase builds `implement`, `test`, `fix` and `review`. `research` and `propose` join when Phase 6 lands; the call form and the spec lines below that name them are Phase 6 work, and until then `assign.sh` refuses both with exit 2.
- Call form:
  ```
  assign.sh --role <implement|test|fix|review|research|propose>
            [--to <worker> | --new-worker <name>]
            [--model <short[:effort]>] [--effort <e>]
            --prompt <file> [--summary <file>]
            [--fix-cause <text>]
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
- **Launch check (natedev 56b8f93b).** Before the claim, run `progress_history.py check-launch`, as `review.sh` does. `start-pass` and `check-launch` refuse a `fix` launch that has no findings round in flight or awaiting closure and no `--fix-cause`. They also refuse a prompt whose `Phase <id> — <title>` line names another phase. `assign.sh --role fix` passes `--fix-cause` through; a refusal exits 2 with the helper's line.
- **Address and shown name (naming rule 6).** A worker has two separate values. The address is its full name (for example `impl2`, `claude-build-followups-sdlc-roles-impl`); it never changes, and `resolve` output, `--to`, roster keys, messages' routing and every file name use it. The shown name is `<production> · <unit> · <job>` (for example `natedev · models-unit · code reviewer 2`; production and unit come from the ledger path, which this plan's header sets). New renderer `workers.shown_name(run, address) -> str` builds it at each call from the ledger path's production and unit (`ledger_for_run()`) and the stored role, never from the address. It is never stored, so it follows a rename or a transfer. CLI: `workers.py shown-name --session-dir <run> --name <address>`.
- `assign.sh` sets each Codex thread name and every board, heartbeat and summary label to the shown name. Messages and reports use it.
- `codex_mesh.py` uses the shown name too: every `thread/name/set` (in `open_unit_worker()`, which today builds `<production> · <unit> · <role> (<name>)` by hand, and on a replacement thread) and every follow-up message it prints. Help, docstrings and diagnostics in `codex_mesh.py` already say "worker". Rename the private `_seat_sandbox()` (codex_mesh.py:1538) and its `seat_sandbox` parameter (:5357) to `_worker_sandbox()` and `worker_sandbox`. A stored key or field that existing data still reads keeps its name, with a note next to it (naming rule 5). The tests-only gate check for "seat" belongs to Phase 12.
- Artifacts: `${PHASE_DIR}/<role>_status`, `<role>_summary.txt`, `<role>_agent.log`, with `_<lens>` and `_<pass>` suffixes for reviews.
- **Exit codes and outcomes.** 0 done; 1 the worker ran and failed; 2 a refused argument or claim; 3 blocked (no worker could start); 4 handed off. `follow` exits 2 for both a refusal and a `WorkerHandedOff`; after a `follow` exit 2, `assign.sh` reads the worker's roster record, and an `adopted_to` value means handed off. On handed off, `assign.sh` writes `handed_off` to `<role>_status`, records the destination run in `<role>_summary.txt`, prints `handed off: <worker> to <run>; completion unknown`, does not record the pass as done, and never sends the prompt again.
- Review in this phase: `--role review` takes an open writer for the lens, as today without a pool. Reviewers become ordinary unit workers (`<worktree>-review`, `-review2`), opened and resumed by `assign.sh --role review`; roles-unit (sdlc) builds that after this phase, and Phase 8 is dropped (showrunner, 2026-10-29).

**Files:**
- `scripts/direct/assign.sh` — new
- `scripts/direct/workers.py` — `resolve --model --effort`, `shown_name`, `shown-name`
- `scripts/agents/codex_mesh.py` — shown name in thread names and follow-up messages; no "seat" in help and diagnostics
- `scripts/direct/test_assign_launcher.py` — new
- `scripts/direct/test_workers.py` — identity-selection and shown-name cases
- `scripts/agents/test_codex_mesh.py` — shown-name cases

**Workers:** `1 writer + 1 tester`
- `impl` — `assign.sh`, `workers.py`, `codex_mesh.py`; hub: `scripts/direct/workers.py`
- `test` — `test_assign_launcher.py` (fake app-server; bounded real waits): each role opens with its sandbox; `--to` resumes a thread; `--model` on a roster that holds `unit_ledger` selects or opens a worker with that identity and leaves the existing worker's model unchanged; `--to` with a different `--model` exits 2; `--role research` and `--role propose` exit 2; a missing `--role` exits 2; a writer role without `--paths` exits 2, a reviewer with `--paths` exits 2, a `..` path exits 2; each dispatch writes its `dispatch_<n>_paths.json`; every worker prompt carries the rules block; the thread name and board label read `<production> · <unit> · <job>` while `--to` takes the full address; no run starts one; a named finished run is reactivated with `start-phase` and keeps its worker's thread; the reservation owner is the launcher pid and stays live through the claim; a handoff during the turn gives exit 4, status `handed_off`, the destination run, no finished pass and no second prompt. `test_workers.py`: `resolve --model --effort` picks only an exact identity; `shown_name` follows a `move_unit` and a `rename_unit` while `resolve` prints the same address. `test_codex_mesh.py`: a replacement thread name and a follow-up message use the shown name; `--help` output has no "seat"; `--role fix` with no findings round and no `--fix-cause` exits 2, and with `--fix-cause` records the pass; a prompt with another phase's `Phase <id> — <title>` line exits 2; a worker opened as `review` and dispatched as `implement` keeps one identity record

**Constraints from prior phases:**
- Phase 2: rows are family words; `agents_resolve` returns the slug or exits 1 with `no Codex model for '<word>' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug`. A resolved line ends with ` word=<family word>` when the row names a family (`agents_config.sh` `_agents_print_resolved`); the slug is in `agent=`.
- Phase 4: `workers.py resolve --session-dir <run> --role <r>` prints the worker address; `<role>2` when all are busy. Its reservation is `allocation_pending`, `allocation_id`, `allocation_owner_pid` and `allocation_expires_at` (120 s, `ALLOCATION_CLAIM_WINDOW_SECS`); `command_follow()` releases only its own `allocation_id`. `resolve` and `reopen` exit 2 on a finished run and name `progress_history.py start-phase`. `follow` exits 2 on `WorkerHandedOff` (`worker handed off to <run>; assignment completion unknown`). A quarantined ledger entry makes the first `follow` or `reopen` exit 2 naming the quarantine; later calls proceed. Ledger lock order: ledger, giving roster, taking roster, inbox.
- Phase 5: `resolve --owner-pid <pid>`. Without it, the owner is the live director's pid (`workers.director_pid()`); with no live director, `resolve` exits 2 `run director pid is unknown; pass --owner-pid <pid>`. Run end pauses workers only when the unit director is gone; while it lives, `codex_mesh.py stop --pause` keeps workers and servers, and the next run resolves the same worker by name. Pause keeps pending messages, owed continuations and detached runs, and adopt carries them; the taking run's first follow drains them before new work. Role is not durable identity: identity records (`impl_worker_*.json`, keyed by address through `worker_state.worker_record_path()`) hold `name`, `slot`, `family`, `model`, `worker_id` and no `role`; the roster `role` is the current assignment, and the board slot comes from it. Inside one run, launchers and `codex_mesh.py end` take the inbox lock, then the roster lock. `codex_mesh.py end --expected-thread-id <id> --idle-only` ends only that thread, and only when idle. `workers.move_unit` and `workers.rename_unit` move the ledger and rewrite each roster's `unit_ledger`; `progress_history.py set-unit` writes the run's `production` and `unit`.
- Phase 3: a lifecycle decision runs only after buffered notices drain; keep the `FixtureLoopBudget` guard in new fixtures.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/agents scripts/production scripts/guilds` is 0/0.

### Phase 8 — Reviewers in the worker ledger (WT P3)  · status: dropped

Dropped (showrunner, 2026-10-29). Reviewers become ordinary unit workers (`<worktree>-review`, `-review2`), opened and resumed by `assign.sh --role review`. roles-unit (sdlc) owns reviewers as workers and builds them after Phase 7. This plan has no pool import, no `ReviewerPayload` and no `Transferred` pool lease.

### Phase 9 — Self-heal (WS P3)  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** the launcher repairs each worker failure by itself; when nothing can start, it says `blocked:` and tells the showrunner.

**Spec:**
- Worker cannot start, fast local fault, stale sign-in: the existing retry on a new app-server stays.
- Thread gone or cannot resume: `workers.py resolve` opens a replacement under the same name. Its opening prompt has a catch-up brief: the Work Order, the worker's last two summaries and `git diff --stat` since the phase baseline. It posts one board line and records `replaced` with the cause in the ledger.
- **One replacement authority.** `workers.py` alone opens a replacement. Today `codex_mesh._resume_worker_thread()` calls `thread/start` and continues as a new thread when resume fails. It stops doing that and returns a named outcome (`ThreadMissing`). Each caller (the live follow and the resolve path) passes `ThreadMissing` up, and `workers.py` replaces the thread.
- **No duplicate submission.** "Send the same phase prompt again" applies only when no submitted turn may still run. An uncertain submission (for example `WorkerMoveUncertain`), an owed continuation (`continuation_owed`), a paused worker's preserved queue (Phase 5), or a handed-off dispatch (`assign.sh` exit 4, `handed_off`: completion unknown, the turn belongs to the destination run) waits or resumes; it never sends the prompt again.
- A turn still live after compaction (today exit 3): send `turn/interrupt`, wait 60 s; if still live, end the worker with `codex_mesh.py end --to <worker> --expected-thread-id <thread>` (never `--idle-only`: the turn is live), so a racing replacement is never ended, and open a replacement with the brief. The interrupt wait and the replacement wait keep every notice received before an RPC reply (Phase 3 drain rule).
- Second overflow with no progress: compact once and send the follow-up again; if that fails, open a replacement with the brief.
- **Claude route state.** `UnitWorker.backend` holds exactly one `CodexThread` or `ClaudeSession` today. Add a third backend variant `WorkerRoutedToClaude(claude: ClaudeSession, parked: CodexThread, catch_up_from: str)`, where `catch_up_from` is the commit at route start. The name differs from `moves.RoutedToClaude` (the account route record) on purpose. `WorkerEvent.kind` gains `routed` and `returned`. Pause during a route pauses the Claude worker and keeps `parked`. Retire during a route ends both the Claude session and the parked thread: `workers.end_unit()` branches on backend (`CodexThread` -> `command_end` per run; `ClaudeSession` -> `claude rm`) and gains a `WorkerRoutedToClaude` branch that does both. `workers.held_claude_sessions()` also returns `WorkerRoutedToClaude.claude.session_id`, so phase-end `remove_workers.py` keeps a routed Claude worker. The route ends when the next dispatch resumes `parked`; the backend is then `CodexThread` again.
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
- `test` — fake-server cases (bounded real waits): lost thread gives a replacement with the brief in its prompt; a missing thread found during a live follow and during resolve gives `ThreadMissing` and one `workers.py` replacement, and `codex_mesh.py` starts no thread; live turn gives interrupt then replacement, and a notice received before the interrupt reply is kept; an uncertain submission, an owed continuation, a preserved paused queue or a handed-off dispatch is never sent again; second overflow gives compaction and one retry; outage gives a route with a 30-minute hold and a route back; the move journal accepts and reads back `outage`; quota route opens a Claude worker and returns to the parked Codex thread with the catch-up message; `WorkerRoutedToClaude` round-trips through the ledger; pause during a route keeps the parked thread, and retire during a route ends both backends; no case starts an Agent-tool subagent; phase end keeps a routed Claude worker; a dead-launcher worker is followed, not replaced; a live-turn replacement ends only the old thread id

**Constraints from prior phases:**
- Phase 4: ledger `replaced` event; `UnitWorker.backend` is `CodexThread | ClaudeSession`; lock order ledger, giving roster, taking roster, inbox; undecodable entries are quarantined once and kept; the reservation (`allocation_id`, owner pid, 120 s window) is released only by its own id.
- Phase 5: pause keeps pending messages, owed continuations and detached runs, and the taking run's first follow drains them before new work; retire (`end_unit`) cancels them. Run end pauses only when the unit director is gone. `codex_mesh.py end` takes the inbox lock, then the roster lock, with one 5 s budget; a retry on an ended worker drains and interrupts again. A worker left in `waiting_capacity`, `running` or `starting` by a launcher that died can be followed again (956114f0); it is not a lost thread and gets no replacement.
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
- Phase 5: the taking run's first follow delivers a paused worker's carried detached results and messages, and runs its owed continuation, before the new prompt, in the same follow. Turns from that queue end under the new dispatch, so `record` attributes their paths with that dispatch's manifest.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks scripts/production scripts/whoami scripts/accounts` is 0/0.

### Phase 11 — `/unit:assign` and `/showrunner:assign` (WT P4)  · status: todo

**Depends on:** Phase 7 (showrunner, 2026-10-29)

#### Work Order

**Goal:** a unit director and a showrunner each launch a worker with one skill.

**Spec:**
- `/unit:assign <role> [--to <worker>] [--model m[:e]] [--effort <level>] [--paths <file>] <prompt file> [...]` composes the call: `--session-dir` is the run, `--cwd` the worktree, `--phase` the open phase.
- **Per-launch model and effort (showrunner, 2026-10-29; user priority: `/showrunner:assign` with per-launch `--model` and `--effort` comes first).** Both skills take `--model m[:e]` and `--effort <level>` for one launch and pass them to `assign.sh`. Phase 7's identity rule then picks or opens a worker with that model and effort.
- `/showrunner:assign`: same skill, showrunner defaults: `--session-dir ~/.local/state/unit-workers/<slug>/showrunner/`, `--cwd` the showrunner checkout. It allows `review` of a document; `research` and `propose` join when Phase 6 adds them to `assign.sh`. `implement`, `test` and `fix` are refused with one line: a showrunner never writes code; its units do.
- **The showrunner's ledger.** `workers.ledger_for_run()` resolves a ledger from the run's progress-state `production` and `unit` keys, then the plan header, else an adhoc key; the showrunner folder has neither. So `assign.sh`, on first use of the showrunner form, creates the folder and runs `progress_history.py set-unit --session-dir <folder> --production <slug> --unit showrunner` (Phase 5). `ledger_for_run()` then resolves `~/.local/state/unit-workers/<slug>/showrunner.json`. `resolve` and `workers.py end --production <slug> --unit showrunner` (a new CLI over `workers.end_unit()`) at wrap address that one file.
- The review role is what `assign.sh --role review` gives, with no pool (showrunner, 2026-10-29). `/astra_review` keeps its held-role path. Showrunner workers end at production wrap, with the production's reviewers (user, 2026-10-28).
- Both skills print the worker address, its shown name (`workers.shown_name()`, Phase 7), the model and effort used, and the summary path. The address stays raw for `--to` and routing. They read no transcripts and hold no state.

**Files:**
- `commands/unit/assign.md` — new
- `commands/showrunner/assign.md` — new
- `scripts/direct/assign.sh` — showrunner session-dir default, `set-unit`, role refusal
- `commands/showrunner/produce.md` — the wrap step (step 2, next to `review_pool.py stop`) runs `workers.py end --production <slug> --unit showrunner` for the showrunner's workers
- `scripts/direct/workers.py` — `end` subcommand: `--production`, `--unit`; calls `end_unit()`; exit 2 on `WorkerUnavailable`
- `scripts/direct/test_assign_launcher.py` — cases
- `scripts/direct/test_workers.py` — cases

**Workers:** `1 writer + 1 tester`
- `impl` — the two commands, `produce.md`, `assign.sh`, `workers.py`
- `test` — the showrunner form refuses `implement`; the showrunner folder gets `production` and `unit: showrunner` and `ledger_for_run()` gives `unit-workers/<slug>/showrunner.json`; `resolve` and `workers.py end` touch that same file; `--model` and `--effort` reach `assign.sh` for one launch; both forms print address, shown name, model and summary path; `workers.py end` ends the showrunner's workers and its reviewers; after a production rename (`rename._rename_unit_workers` moves `showrunner.json`, not the run folder), the next showrunner `resolve` on the new slug adopts each worker on its thread

**Constraints from prior phases:**
- Phase 5: `progress_history.py set-unit` writes the keys that `ledger_for_run()` reads first. `workers.end_unit(production, unit)` cancels pending messages, owed continuations, detached runs and reservations, prints one line for each, ends each worker, reports each removed quarantined entry and deletes the ledger; it raises `WorkerUnavailable` (`cannot end worker <w> in <run>; retry retirement`) when a worker cannot end. The `workers.py` CLI has only `resolve`. The showrunner folder has no director record, so `resolve` there needs `--owner-pid` (`assign.sh` passes `$$`). Keep only ledger files in `unit-workers/<slug>/*.json`: `held_claude_sessions()` and `rename._rename_unit_workers` open each one through `locked()`, which rewrites it as a ledger.
- Phase 7: call form, `--paths`, exit codes (4 = handed off), `workers.shown_name()` and the raw address.
- Phase 4: lock order ledger, giving roster, taking roster, inbox; quarantined entries are kept; fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/production` is 0/0.

### Phase 12 — The rule and the cutover (WS P1 + WT P5, merged: both change the same docs)  · status: todo

**Depends on:** Phase 5, Phase 9, Phase 10, Phase 11

#### Work Order

**Goal:** the worker rule lives in one doc and names `assign.sh`; every doc call form moves to `assign.sh` in one commit; a unit that switches can no longer use the old launchers.

**Spec:**
- New `docs/direct/worker_rule.md` defines `<WorkerRule/>`:
  > A unit director gives all implementation, test, repair and review work to its workers. A worker is a long-lived Codex thread that `assign.sh` opens and resumes. The director does not start a Claude subagent that writes, does not write code, and does not run a coding agent by hand. Tools enforce this rule. A showrunner cannot grant an exception. Only the user can: with the switch in `settings.json`, or for one subagent task by typing `allow subagent: <task name>` in the director session. A granted task's changes are attested and checked like a worker's.
  It covers unit directors only; showrunners and the natedev config shell keep their own rules.
- Pointers, no repeated text: `commands/unit/direct.md` gets a `<TagReferenceContract/>` row; `<LongLivedWorkers/>` becomes one line that points to `<WorkerRule/>`; `<CoreContract/>` keeps "does not write implementation code" and its exception "agreed doc-only/trivial post-review fixes" becomes "Markdown only". `docs/production_format.md`: the **Worker** definition and a new `<ProductionUnit/>` item 19 point to `<WorkerRule/>`. `commands/showrunner/produce.md`: one line — the showrunner never approves a flow without workers and sends an exception request to the user.
- The design check (screenshots) moves to a UI reviewer (`assign.sh --role review --image`) when there is no `ui` pool.
- In the same commit, call forms change to `assign.sh`: `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerLaunchFailure/>`), `docs/direct/launch_implementation.md` step 5, `docs/direct/dual_review.md` step 3, `docs/direct/phase_end.md`, `commands/plan/phase_review.md`. Each writer call form passes `--paths` with the files the Work Order's **Workers** line gives that worker. Each doc names exit 4 (handed off: completion unknown, never resubmit).
- **`workers.py switched --unit <unit>` is recoverable.** It writes the unit under `"state": "switching"` with the date into the switch file `~/.local/state/worker-switch/<slug>.json`, then sets `"state": "switched"`. A rerun after a crash at any point finishes the switch. The file is not under `unit-workers/<slug>/`: `held_claude_sessions()` and `rename._rename_unit_workers` open every `*.json` there through `locked()`, which rewrites it as an empty ledger. A production rename moves the switch file in a new `rename.py` step next to `_rename_unit_workers`. The pool import is dropped (showrunner, 2026-10-29): reviewers are ordinary unit workers that roles-unit (sdlc) builds.
- `implement.sh` and `review.sh`: the first thing each does is check the run's unit (`SHOWRUNNER_UNIT_ID`, else `unit_runs.marker`) against the switch file. A unit in either state gets exit 2: `refused: <unit> switched to assign.sh on <date>; use assign.sh` (state `switched`) or `refused: <unit> is switching to assign.sh; use assign.sh` (state `switching`).
- **Switched-unit integration.** A switched unit's transfer and rename keep every worker thread id: Phase 5's `move_unit` moves the ledger. This phase tests that path end to end.
- `commands/showrunner/produce.md`: the showrunner asks each unit director, one at a time, to switch, and runs `workers.py switched` on a yes. It asks first the units with a finished run or no run (style, messaging, builds), because only `assign.sh` starts or reopens their run.
- New tag-reference test: every `<Tag/>` that `commands/unit/direct.md` and `docs/direct/*.md` name resolves to a definition.
- The naming gate exists: `scripts/production/roles_audit.py check` (sdlc-roles f8f5b962) runs in every merge gate and fails on a retired name such as "seat" outside a kept context. This phase adds no naming check.

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
- `scripts/production/rename.py` — moves the switch file
- `scripts/production/test_rename.py` — production rename moves the switch file
- `scripts/direct/test_workers.py` — cases
- `scripts/direct/test_implement_launcher.py` — cases
- `scripts/direct/test_review_launcher.py` — cases
- `scripts/production/test_transfer_unit.py` — switched-unit transfer case

**Workers:** `2 writers` — docs and scripts split.
- `impl` — `workers.py`, `implement.sh`, `review.sh`, `test_workers.py`, `test_implement_launcher.py`, `test_review_launcher.py`, `test_transfer_unit.py`, `rename.py`, `test_rename.py`: a crash after the `switching` write and before `"state": "switched"` is finished by a rerun; both refusal lines; a switched unit's transfer keeps every worker thread id; a production rename moves the switch file
- `test` (opens as impl) — every doc and command file, `test_tag_reference.py`; hub: `commands/unit/direct.md`

**Constraints from prior phases:**
- Phase 5: `move_unit` and `rename_unit` move the ledger as a journalled migration (both ledger locks in path order). Workers live as long as their unit director: run end pauses only when the director is gone, and while it lives, `stop --pause` keeps workers and servers. Pause keeps work; retire alone ends it. `<WorkerRule/>` and `docs/direct/phase_end.md` say this.
- Phase 7: help, diagnostics and printed names in `codex_mesh.py` no longer say "seat"; stored compatibility fields keep their names with a note. `assign.sh` exit codes 0–4; `--paths` is required for writer roles.
- Phase 9: `blocked:` exit 3. Phase 10: E4 lines from all three launchers; the grant attestation schema in `subagent_grants.json`. Phase 11: `/unit:assign`.
- Phase 4: lock order ledger, giving roster, taking roster, inbox; quarantined entries are kept; fixtures block until their deadline and keep `FixtureLoopBudget`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks` is 0/0; the tag-reference test resolves `<WorkerRule/>`; a grep test finds no "spawn a fresh Claude Agent helper" and no `implement.sh --to` in `docs/direct/`; a switched unit's `implement.sh --to` call exits 2 with the refusal line; a unit not yet switched runs as before; `roles_audit.py --root . check` exits 0.

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
- Phase 5: `workers.held_claude_sessions()` gives the session ids of non-retired Claude ledger workers (Phase 9 adds routed ones). It locks and rewrites every ledger (`locked()` saves on exit), so the hook does not call it on each tool call: it checks `UNIT_WORKER` first, then reads ledger JSON without `locked()`.

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
- Phase 5: a retired unit has no ledger (`end_unit` deletes it). `audit` reads ledgers without `locked()` (it finishes move journals, quarantines, raises `WorkerUnavailable` once and saves), and skips `*.move.json` and `*.lock`.

**Acceptance gate:** the merge gate is green; the three-day audit lists every live unit with unauthorized writer subagents = 0.

### Phase 15 — Remove the old launchers; write the as-built (WT P6 + WS P6 as-built)  · status: todo

**Depends on:** Phase 12, Phase 14
**Blocked by:** every unit of every running production is in the switch file (`~/.local/state/worker-switch/<slug>.json`).

#### Work Order

**Goal:** `assign.sh` is the only worker launcher, and one as-built doc describes workers.

**Spec:**
- Remove `scripts/direct/implement.sh`, `review.sh` and their tests (`test_implement_launcher.py`, `test_review_launcher.py`, `test_launcher_modes.py`). Port any case still needed to `test_assign_launcher.py` first.
- **Not in this phase (showrunner, 2026-10-29).** roles-unit (sdlc) owns the removal of `review_pool.py`: the file, its tests (`test_review_pool.py`), its callers (`produce.md`, `retire_unit.py`, `transfer_unit.py`, `rename_unit.py`, `review_pool_report.py`) and `docs/as-built/review-pool.md`.
- Before each delete, grep for callers of the file; every operational caller uses `assign.sh` or `workers.py`.
- `worker_provenance.py record` has one caller, `assign.sh`. The worker gate's E3 allow list drops `implement.sh` and `review.sh` (hook code; no `settings.json` change).
- The "no `implement.sh`" gate covers active scripts, commands and `docs/as-built/` only. It does not read `docs/plans/` or the grep test's own search text.
- Write `docs/as-built/workers.md`; remove `docs/as-built/long-lived-workers.md`.

**Files:**
- `scripts/direct/implement.sh` — removed
- `scripts/direct/review.sh` — removed
- `scripts/direct/test_implement_launcher.py` — removed
- `scripts/direct/test_review_launcher.py` — removed
- `scripts/direct/test_launcher_modes.py` — removed
- `scripts/direct/assign.sh` — the only `worker_provenance.py` caller
- `scripts/hooks/pre-tool-use-worker-gate.py`
- `scripts/hooks/test_worker_gate.py`
- `scripts/direct/test_assign_launcher.py` — ported launcher cases
- `scripts/hooks/test_old_launchers_gone.py` — new: the "no `implement.sh`" grep test
- `docs/as-built/workers.md` — new
- `docs/as-built/long-lived-workers.md` — removed

**Workers:** `1 writer + 1 tester`
- `impl` — removals, `assign.sh`, the hook, the as-built
- `test` — `test_worker_gate.py`, the ported cases in `test_assign_launcher.py`, and `test_old_launchers_gone.py`: no active script, command or `docs/as-built/` file names `implement.sh`

**Constraints from prior phases:**
- Phase 12: the switch file path (`~/.local/state/worker-switch/<slug>.json`), its `switching`/`switched` states and the refusal. Phase 13: the allow list.
- Phase 4: the ledger, its lock order (ledger, giving roster, taking roster, inbox) and its quarantine stay as they are.

**Acceptance gate:** the merge gate is green (the lib suite included); basedpyright on `scripts/direct scripts/hooks scripts/production` is 0/0; the tag-reference test resolves every tag; no active script, command or `docs/as-built/` file names `implement.sh`.
