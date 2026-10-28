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

**Depends on:** none

#### Work Order

**Goal:** an idle `codex_mesh.py follow` uses under 1% of a core and a streaming one under 10%, so followers stop taking CPU from builds.

**Spec:**
- **`follow` waits on events, not a poll loop** (showrunner, 2026-10-28: builds-unit measured 26 `codex_mesh.py follow` processes using 1.5 cores on a quiet natedev, two at about 27% each, which takes CPU from builds). Each wait in `follow` (and any helper it runs) blocks on a socket read, a pipe, or inotify on the files it watches, with a long timeout only as a safety net. Measure before and after with the same method: CPU per `follow` process over 60 s on an idle turn (`ps -o pid,pcpu,cputime` deltas or `/proc/<pid>/stat`), and report both numbers in the summary. Goal: an idle `follow` uses under 1% of a core. Message delivery and turn-end detection stay as fast as today.
- **The high-CPU `follow` (showrunner, 2026-10-28: one process at 0.92 cores).** Reading /proc showed this (pid 2428570, a cargo-liner worker running merge tests, 2 h 30 min old, 24% average CPU): about 5,500 read calls and 3.3 MB per second (about 600 bytes a read). The one-second `mesh_runs` scans below fit those numbers best (a file opened and closed inside one poll does not show in a sampled fd list); the notification stream is a second possible source. The implementer measures which, before the fix. Fix: `follow` asks the server only for the events it needs (turn and item completion, messages for its own thread), or skips output-delta events without a JSON parse, and reads in large buffers. Test it with a fake app-server that sends 10,000 output-delta lines: `follow` uses under 10% of a core and still reports the turn end. Report the CPU before and after for both cases (idle wait, and a worker streaming output).
- **One-second wake-ups (builds-unit via the showrunner, 2026-10-28).** The follow loops wake every 1 s (`codex_mesh.py` about line 277 for the interval, the loop at about 3496, `_on_quiet_poll` at about 3590): 0.52 cores across 33 loops. Block on the socket plus inotify on the marker files instead. A follow whose worker has finished (terminal roster status, turn ended, or its run finished) ends its loop and exits. Test: a quiet follow makes no wake-up while nothing changes for 30 s; touching a marker file wakes it within 1 s; a finished worker's follow exits.
- **`mesh_runs` scans (builds-unit via the showrunner, 2026-10-28).** Each 1 s poll globs `mesh_runs/*.json` twice (`_end_dead_runs` about line 1644, `_detached_run_live` about 1622; the second scan came from builds' 556e44d1) and parses every record. Records are never pruned (3,536 in one session), so one follower does 7,182 reads per second. Run the dead-run and unseen-end checks every 60 s, in one scan. A finished record never changes, so the follower remembers its name and does not read it again. Records stay in `mesh_runs/`: `mesh_runs.py`, `verify.sh`, `review.sh`, `unit_lookup.py`, `rename_unit.py` and `retire_unit.py` find a record by its path. Test: 3,000 finished records cost no reads on a quiet poll; a run that ends is still reported within 60 s.
- **Short sleeps (efficiency-unit via the showrunner, 2026-10-28 12:05 EDT).** 32 follows at once used 1.8 cores over their lifetimes; the cargo-liner tile follower used 2,334 s CPU in 10,721 s (22%), several more 4-10%. Candidate loops: the 0.05 s sleep (about line 6823) while a turn is in flight, `REAP_POLL_SECS` 0.2 and `RESIDENT_POLL_SECS` 1.0. Measure first, then fix the loop the numbers name. `kernel.yama.ptrace_scope` is 1, so a running follower cannot be traced from outside: start a follower under `strace -c -f` (py-spy is not installed) against the fake app-server with a turn in flight, and report the syscall and sleep counts per loop before and after.

**Files:**
- `scripts/agents/codex_mesh.py` — event-driven `follow` waits, the 60 s run scan that skips finished records, follow exit on a finished worker
- `scripts/agents/test_codex_mesh.py` — new cases

**Workers:** `1 writer + 1 tester` — one file holds every loop, so one writer.
- `impl` — `codex_mesh.py`; also takes the before/after CPU and `strace -c -f` measurements
- `test` — `test_codex_mesh.py` (fake app-server): 10,000 output-delta lines under 10% of a core with the turn end still reported; a quiet follow makes no wake-up for 30 s; a touched marker wakes it within 1 s; a finished worker's follow exits; 3,000 finished records cost no reads on a quiet poll; an ended run is reported within 60 s

**Constraints from prior phases:**

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/agents` is 0/0; the summary reports CPU per `follow` before and after for an idle wait and a streaming worker, and the syscall and sleep counts per loop.

### Phase 4 — Unit worker ledger, `workers.py resolve`, the finished-run gap (WS P2, part 1)  · status: todo

**Depends on:** Phase 3

#### Work Order

**Goal:** a worker belongs to the unit: a new run takes the unit's workers with their threads, and a finished run never leaves a director without a worker.

**Spec:**
- **Ledger.** New file `~/.local/state/unit-workers/<production slug>/<unit>.json`, one entry per worker: name, role, family, `thread_id`, Codex home or Claude session id, the run that holds it now, `opened` and `replaced` events with cause. A run outside a production uses the key `adhoc/<repository>-<hash of worktree path>`. Every write is under an `flock`. This unit worker ledger is not the run's `workers` file that `worker_state.worker_ledger` reads.
- **`codex_mesh.py adopt --from <old run> --name <worker>`.** The source entry must be idle. The destination roster gets the same thread id and name. The source gets `adopted_to: <run>`, a terminal state that refuses `follow`. The thread resumes on the new run's server, as `follow` does when the old server is gone. This keeps `UNIT_RUN_DIR`, the cargo token and the berth hook correct for the current run.
- **`scripts/direct/workers.py resolve --session-dir <run> --role <impl|test|fix|review>`** prints the worker name. Order: an open worker with this role in this run; the unit's worker from the ledger (adopted into the run); reopen; a new worker. When every worker of the role is busy, it opens `<role>2` (`impl2`, `test2`, `fix2`, `review2`). Two runs that race for one worker: one wins, the other gets the `2` worker.
- **Finished run.** `progress_history.py start-phase` on a finished run makes it active again: it writes a `reopened` event and restores the run marker. `codex_mesh.py reopen` on a finished run exits 2 and names `workers.py resolve`; it no longer exits 0.
- A thread carried across a workers account move resumes on the new Codex home (`AccountMoveMarker`).

**Files:**
- `scripts/direct/workers.py` — new: ledger I/O, `resolve`
- `scripts/agents/codex_mesh.py` — `adopt`, `reopen` exit 2, event-driven `follow` waits
- `scripts/direct/progress_history.py` — `start-phase` reactivation
- `scripts/direct/test_workers.py` — new
- `scripts/agents/test_codex_mesh.py` — new cases
- `scripts/direct/test_progress_history.py` — new cases

**Workers:** `1 writer + 1 tester` — the three scripts share the ledger shape, so one writer holds them.
- `impl` — `workers.py`, `codex_mesh.py`, `progress_history.py`
- `test` — `test_workers.py`, `test_codex_mesh.py`, `test_progress_history.py` (fake app-server): a finished run that gets a new phase resumes the same `thread_id`; a new run takes an idle worker and the old entry refuses `follow`; two racing runs give one winner and one `2` worker; reopen on a finished run exits 2; a carry-over after an account move resumes the thread

**Constraints from prior phases:**

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/agents` is 0/0.

### Phase 5 — Worker lifecycle: run end, phase end, retire, transfer, rename (WS P2, part 2)  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** workers live until the unit is retired; run end only pauses them, and retire, transfer and rename keep the ledger correct.

**Spec:**
- `end_session.sh` pauses the unit's workers (`codex_mesh.py stop --pause`) and leaves them in the ledger. It does not end them.
- `remove_workers.py` (phase end) does not remove a Claude worker that the ledger holds.
- `retire_unit.py` ends the ledger's workers and removes the file. `transfer_unit.py` moves the file to the new production's folder. `rename_unit.py` renames the file and the worker names, as `review_pool.rename_unit` does now.
- `workers.py` gains the functions these callers use: `pause_unit`, `end_unit`, `move_unit`, `rename_unit`.

**Files:**
- `scripts/direct/workers.py` — the four functions
- `scripts/direct/end_session.sh`
- `scripts/direct/remove_workers.py`
- `scripts/production/retire_unit.py`
- `scripts/production/transfer_unit.py`
- `scripts/production/rename_unit.py`
- `scripts/direct/test_end_session.py`
- `scripts/direct/test_remove_workers.py`
- `scripts/production/test_retire_unit.py`
- `scripts/production/test_transfer_unit.py`
- `scripts/production/test_rename_unit.py`

**Workers:** `1 writer + 1 tester`
- `impl` — all script files; hub: `scripts/direct/workers.py`
- `test` — the five test files: run end leaves workers paused and present; phase end keeps a ledger Claude worker; retire ends workers and removes the file; transfer moves it; rename renames file and worker names

**Constraints from prior phases:**
- Phase 4: the ledger path, entry fields and `flock` rule; `workers.py` holds all ledger I/O; `codex_mesh.py stop --pause` exists.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/production` is 0/0.

### Phase 6 — Role words `research` and `propose` (WT P2)  · status: todo

**Depends on:** Phase 2

#### Work Order

**Goal:** the roster, the hierarchy edge and the cargo handler know the roles `research` and `propose`.

**Spec:**
- `codex_mesh.py`: `WORKER_ROLES = ("impl", "test", "fix", "review", "research", "propose")`. Check every use (lines 636, 996, 7323, 7355, 7411).
- The hierarchy edge JSON `{"manager","member","role"}` carries the word.
- `config/agents.conf [direct.codex]`: `research=astra:xhigh`, `propose=astra:xhigh` (user, 2026-10-28; the model `/astra_review` uses for documents).
- Cargo handler: `HierarchyRole` gains `Research` and `Propose` (serde lowercase) with their `text`; an unknown word still takes `RoleFallback`. This is a separate repository; its commit lands there, and the checkpoint body names it.

**Files:**
- `scripts/agents/codex_mesh.py` — `WORKER_ROLES`
- `config/agents.conf` — two rows
- `~/rust/cargo-liner-handler/crates/cargo-handler/src/census/mod.rs` — two variants
- `scripts/agents/test_codex_mesh.py` — role cases

**Workers:** `2 writers` — the two repositories split.
- `impl` — `codex_mesh.py`, `config/agents.conf`, `test_codex_mesh.py`
- `test` (opens as impl) — `census/mod.rs` and its crate tests

**Constraints from prior phases:**
- Phase 2: `[direct.codex]` rows take family words; `astra` resolves through `[codex.families]` to the newest cached release that has a `[codex.agents]` effort row (today `gpt-6-astra`, which has a `weights.py` row). `gpt-6.1-terra` and `gpt-6-luna` have no `weights.py` row: usage accounting raises `UnweightedModel` for them, so no row may name `terra` or `luna`.

**Acceptance gate:** the merge gate is green; `verify.sh test cargo-handler` and `verify.sh lint cargo-handler` pass; the handler shows `research` and `propose`, and an unknown word takes `RoleFallback`.

### Phase 7 — `assign.sh` core (WT P1, with WS P2's `--role`, run start and rules block)  · status: todo

**Depends on:** Phase 2, Phase 4, Phase 6

#### Work Order

**Goal:** one launcher with named options opens or resumes the right worker for six roles; `implement.sh` and `review.sh` do not change.

**Spec:**
- Call form:
  ```
  assign.sh --role <implement|test|fix|review|research|propose>
            [--to <worker> | --new-worker <name>]
            [--model <short[:effort]>] [--effort <e>]
            --prompt <file> [--summary <file>]
            [--phase <id>] [--lens <adversary|contract|craft|ux>] [--pass <n>]
            [--image <path>]... [--early-ready <file>]
            [--session-dir <run>] [--cwd <dir>]
  ```
- Named options only. A missing `--role` or `--prompt` exits 2 with the usage line. `--session-dir` defaults to the caller's run (`unit_runs.marker`). With no run, `assign.sh` starts one with `prepare_session.sh` and prints the folder (WS P2).
- Role: `implement` is stored as `impl`. The word goes in the roster `role` field and in the hierarchy edge. `research` reads and writes one report to `--summary`. `propose` writes one proposal file into `~/.local/state/plan-backlog/`; the launcher, not the worker, appends the `ledger.md` line from the worker's summary. A worker that writes a second file there is named by the launcher's listing of the folder before and after the turn. `review`, `research`, `propose` open with `access: read-only` (`codex_mesh.py prime` / `fork` sandbox, roster `access_policy`); `propose` adds `~/.local/state/plan-backlog` as a writable root. `implement`, `test`, `fix` open with the default full-access sandbox.
- Worker choice: with `--to`, the named worker. Otherwise `workers.py resolve --role <role>`. `--new-worker <name>` opens a new named worker (today's `review.sh --slot`).
- Defaults and overrides: `agents_resolve direct.<role>` gives family, model and effort. `--model sol:medium` or `--effort max` replaces them for this launch only. A worker's identity file keeps the slug it opened with for the identity check (`implement.sh:294-298`); the turn runs the resolved or overridden model, as `follow` does today.
- Every launch: claim under the worker lock (port `implement.sh` `worker_record` and `review.sh` `claim_follow` into one path); compact above `UNIT_COMPACT_ABOVE_TOKENS`; append the rules block to every prompt (no poll; long commands through `mesh_runs.py start`, then end the turn; the Three Gods sentence); board register, heartbeat header, pass record keyed by role and phase (`progress_history.py start-pass` / `finish-pass`); summary settle; release.
- Shown name (naming rule 6): `assign.sh` sets each Codex thread name and every board, heartbeat and summary label to `<production> · <unit> · <job>` (for example `build-followups · models · code reviewer 2`), from the run's production and unit and the role. Messages and reports use the same form. The `--to` address stays the full worker name.
- Artifacts: `${PHASE_DIR}/<role>_status`, `<role>_summary.txt`, `<role>_agent.log`, with `_<lens>` and `_<pass>` suffixes for reviews.
- Exit codes: 0 done; 1 the worker ran and failed; 2 a refused argument or claim; 3 blocked (no worker could start).
- Review in this phase: `--role review` takes an open writer for the lens, as today without a pool. Pooled reviewers come in Phase 8.

**Files:**
- `scripts/direct/assign.sh` — new
- `scripts/direct/test_assign_launcher.py` — new

**Workers:** `1 writer + 1 tester`
- `impl` — `assign.sh`
- `test` — `test_assign_launcher.py` (fake app-server): each role opens with its sandbox; `--to` resumes a thread; `--model` runs the override and keeps the identity slug; `propose` writes the file and the launcher adds the ledger line once; a missing `--role` exits 2; every worker prompt carries the rules block; the thread name and board label read `<production> · <unit> · <job>` while `--to` takes the full worker name; no run starts one

**Constraints from prior phases:**
- Phase 2: rows are family words; `agents_resolve` returns the slug or exits 1 with `no Codex model for '<word>' in ~/.codex/models_cache.json; run codex once to refresh the cache, or name the full slug`. A resolved line ends with ` word=<family word>` when the row names a family (`agents_config.sh` `_agents_print_resolved`); the slug is in `agent=`.
- Phase 4: `workers.py resolve --session-dir <run> --role <r>` prints the worker; `<role>2` when all are busy.
- Phase 6: `WORKER_ROLES` holds `research` and `propose`; their rows are `astra:xhigh`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct` is 0/0.

### Phase 8 — Reviewers in the worker ledger (WT P3)  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** pooled reviewers are workers in the unit ledger, with today's pool behavior, and `assign.sh --role review` uses them.

**Spec:**
- Keep (user, ledger 2026-10-10): one permanent read-only reviewer per owner and type; a concurrent review forks the owner's newest reviewer; a changed standing prompt re-points idle reviewers; the tree guard (`review_tree.py`) after every turn; the turn record with every field of `review.sh:454-489`; the owner's reviewers end at retire.
- A reviewer is a ledger entry with `role: review`, `access: read-only`, `instructions_file`, `prime_hash`, `reviews`, and the mesh dir that holds its thread. `review_pool.py` `prime`, `allocate` (`_prime`, `_choose`, `_repoint`), `release`, `retire` become `workers.py` functions; `migrate` and `stop` stay as `workers.py` verbs for the showrunner. The production template thread and its mesh dir live in `~/.local/state/unit-workers/<slug>/`.
- One app-server per production for reviewers stays; `workers.py` passes the entry's mesh dir to `follow --cwd <worktree> --read-only`.
- No `--pool` flag and no `--lease`. `assign.sh --role review` reads the production doc's `**Review pool:** code|ui` line. With it, the worker is the owner's read-only code reviewer or UI reviewer. Without it, an open writer takes the lens. The unit and phase come from the run and `--phase`.
- `workers.py import-pool --unit <unit>` copies that unit's entries from `~/.local/state/review-pool/<slug>/pool.json` into its ledger with their thread ids, so the primed threads are kept (1,435 against 12,982 weighted tokens a turn). Phase 12's `switched` calls it. `review.sh` keeps `pool.json` for units not yet switched.
- `review_pool_report.py` reads reviewers from the ledger and from `pool.json` until Phase 15.

**Files:**
- `scripts/direct/workers.py` — review functions, `import-pool`
- `scripts/direct/assign.sh` — the review path, tree guard, turn record
- `scripts/direct/review_pool_report.py` — reads the ledger
- `scripts/direct/test_workers_review.py` — new, ported from the `test_review_pool.py` cases
- `scripts/direct/test_assign_launcher.py` — review cases
- `scripts/direct/test_review_pool_report.py` — review cases

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`, `assign.sh`, `review_pool_report.py`
- `test` — `test_workers_review.py`, review cases in `test_assign_launcher.py` and `test_review_pool_report.py`: the existing `test_review_pool.py` cases pass against `workers.py`; a second concurrent review forks the owner's reviewer; a prompt change re-points an idle reviewer; the turn record keeps every field; `import-pool` keeps thread ids

**Constraints from prior phases:**
- Phase 4: ledger shape and `flock`; Phase 5: `end_unit` and `rename_unit` cover reviewers too.
- Phase 7: `assign.sh` options, artifacts and exit codes; `--lens` and `--pass` suffixes.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct` is 0/0.

### Phase 9 — Self-heal (WS P3)  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** the launcher repairs each worker failure by itself; when nothing can start, it says `blocked:` and tells the showrunner.

**Spec:**
- Worker cannot start, fast local fault, stale sign-in: the existing retry on a new app-server stays.
- Thread gone or cannot resume: `workers.py resolve` opens a replacement under the same name. Its opening prompt has a catch-up brief: the Work Order, the worker's last two summaries and `git diff --stat` since the phase baseline. It posts one board line and records `replaced` with the cause in the ledger.
- A turn still live after compaction (today exit 3): send `turn/interrupt`, wait 60 s; if still live, end the worker and open a replacement with the brief.
- Second overflow with no progress: compact once and send the follow-up again; if that fails, open a replacement with the brief.
- Quota refusal: after the capacity wait and the account move, `quota_alert.py` routes `direct` to Claude and writes `routed to claude`. `assign.sh` opens Claude workers (named background sessions through `agent_bg.sh`, which compact) under the same ledger, sets `UNIT_WORKER=<name>`, and sends the same phase prompt again. When the route returns to Codex, the next dispatch goes back to the Codex worker's thread with a catch-up message: the Claude worker's summaries and the diff since its last turn. A subagent is never in this chain.
- Codex outage (non-local errors from two servers over 10 minutes): new cause `outage` in `quota_alert.py`, same routing, 30-minute hold; after the hold the next dispatch tries Codex again.
- Nothing can start: `assign.sh` exits 3 with `blocked: no worker could start: <cause>` and sends the showrunner one message (`send.py`).

**Files:**
- `scripts/direct/workers.py` — replacement, brief
- `scripts/direct/assign.sh` — interrupt, overflow, Claude route, blocked message
- `scripts/whoami/quota_alert.py` — `outage` cause
- `scripts/direct/test_workers.py`
- `scripts/direct/test_assign_launcher.py`
- `scripts/whoami/test_quota_alert.py`

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`, `assign.sh`, `quota_alert.py`
- `test` — fake-server cases: lost thread gives a replacement with the brief in its prompt; live turn gives interrupt then replacement; second overflow gives compaction and one retry; outage gives a route with a 30-minute hold and a route back; quota route opens a Claude worker and returns to the Codex thread; no case starts an Agent-tool subagent

**Constraints from prior phases:**
- Phase 4: ledger `replaced` event; Phase 7: exit code 3 means blocked.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/whoami` is 0/0.

### Phase 10 — Worker provenance E4–E7 (WS P4, with WT's E4 line and E7 in `assign.sh`)  · status: todo

**Depends on:** Phase 7

#### Work Order

**Goal:** a checkpoint with code that no worker left is refused at commit and held at merge; a Claude worker with no open route is refused.

**Spec:**
- **E4.** New `scripts/direct/worker_provenance.py record`: at the end of each worker turn, append one line to `${PHASE_DIR}/worker_turns.jsonl`: worker, thread, family, route state, time, and `{path: blob id}` for each changed path (`git status` plus `git hash-object`). `assign.sh`, `implement.sh` and `review.sh` all call it, so E5 holds for every unit before the cutover.
- **E5.** `worker_provenance.py verify --phase <id> --commit <c>`, called from the change-review hook on a `checkpoint(...)` commit and from `phase_reservation.py committed`. It refuses a non-Markdown path whose blob is not one of: a worker's last record for that path, the phase baseline, a merged merge-branch tip. It also refuses a Claude-family turn that no `routed to claude` window covers. Message: `Refused: <path> is not what a worker left (the director or a subagent changed it). Send the change to a worker, then commit again.` It stays closed when it fails.
- **E6.** `worker_provenance.py trailer` writes a `Workers:` trailer: worker names, thread id prefixes and a hash of `worker_turns.jsonl`. `merge_checkpoint.py` holds a unit code checkpoint with no trailer: `Held: checkpoint <sha> has no worker record; its unit must commit again with worker_provenance.py trailer.` When the run folder is on the same machine, E5 runs again.
- **E7.** `assign.sh` refuses a Claude-family launch when no `routed to claude` entry is open: `Refused: workers run on Codex. A Claude worker starts only after Codex refused the work and the work was routed to Claude. No route is open.` `--model opus:xhigh` from a unit director is refused the same way; a showrunner is not checked. `/agent` writes a `routed to claude` entry with the new cause `user` when the user moves `direct` to Claude by hand.
- Cost: under 200 ms for 50 files.

**Files:**
- `scripts/direct/worker_provenance.py` — new
- `scripts/direct/assign.sh` — the `record` call; E7 in `assign.sh`
- `scripts/direct/implement.sh` — the `record` call; E7 in `assign.sh`
- `scripts/direct/review.sh` — the `record` call; E7 in `assign.sh`
- `scripts/hooks/pre-tool-use-change-review-gate.py` — the `verify` call
- `scripts/direct/phase_reservation.py` — the `verify` call
- `scripts/production/merge_checkpoint.py` — the hold
- `scripts/whoami/quota_alert.py` — cause `user`
- `scripts/agents/agent_admin.sh` — cause `user`
- `scripts/direct/test_worker_provenance.py` — new
- `scripts/production/test_merge_checkpoint.py` — cases
- `scripts/hooks/test_change_review_gate.py` — cases

**Workers:** `1 writer + 1 tester`
- `impl` — every script file; hub: `scripts/direct/worker_provenance.py`
- `test` — test files: a worker-only commit passes; a director edit after the worker's turn is refused with its path; a merge-branch merge passes; Markdown passes; a Claude turn with no route is refused and one with a route passes; a checkpoint with no trailer is held; 50 files under 200 ms

**Constraints from prior phases:**
- Phase 7: `assign.sh` turn end and `${PHASE_DIR}`; Phase 9: Claude workers open only under a `routed to claude` entry.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks scripts/production scripts/whoami` is 0/0.

### Phase 11 — `/unit:assign` and `/showrunner:assign` (WT P4)  · status: todo

**Depends on:** Phase 7, Phase 8

#### Work Order

**Goal:** a unit director and a showrunner each launch a worker with one skill.

**Spec:**
- `/unit:assign <role> [--to <worker>] [--model m[:e]] <prompt file> [...]` composes the call: `--session-dir` is the run, `--cwd` the worktree, `--phase` the open phase.
- `/showrunner:assign`: same skill, showrunner defaults: `--session-dir ~/.local/state/unit-workers/<slug>/showrunner/`, `--cwd` the showrunner checkout. It allows `research`, `propose` and `review` of a document. `implement`, `test` and `fix` are refused with one line: a showrunner never writes code; its units do.
- The showrunner's ledger file gets `code` reviewers by the Phase 8 rule, for document and plan reviews. `/astra_review` keeps its held-role path. Showrunner workers end at production wrap, with the production's reviewers (user, 2026-10-28).
- Both skills print the worker name, the model and effort used, and the summary path. They read no transcripts and hold no state.

**Files:**
- `commands/unit/assign.md` — new
- `commands/showrunner/assign.md` — new
- `scripts/direct/assign.sh` — showrunner session-dir default and role refusal
- `scripts/direct/test_assign_launcher.py` — cases

**Workers:** `1 writer + 1 tester`
- `impl` — the two commands, `assign.sh`
- `test` — the showrunner form refuses `implement`; both print worker, model and summary path

**Constraints from prior phases:**
- Phase 7: call form; Phase 8: reviewers in the ledger and the owner rule.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct` is 0/0.

### Phase 12 — The rule and the cutover (WS P1 + WT P5, merged: both change the same docs)  · status: todo

**Depends on:** Phase 5, Phase 8, Phase 9, Phase 10, Phase 11

#### Work Order

**Goal:** the worker rule lives in one doc and names `assign.sh`; every doc call form moves to `assign.sh` in one commit; a unit that switches can no longer use the old launchers.

**Spec:**
- New `docs/direct/worker_rule.md` defines `<WorkerRule/>`:
  > A unit director gives all implementation, test, repair and review work to its workers. A worker is a long-lived Codex thread that `assign.sh` opens and resumes. The director does not start a Claude subagent that writes, does not write code, and does not run a coding agent by hand. Tools enforce this rule. A showrunner cannot grant an exception. Only the user can, with the switch in `settings.json`.
  It covers unit directors only; showrunners and the natedev config shell keep their own rules.
- Pointers, no repeated text: `commands/unit/direct.md` gets a `<TagReferenceContract/>` row; `<LongLivedWorkers/>` becomes one line that points to `<WorkerRule/>`; `<CoreContract/>` keeps "does not write implementation code" and its exception "agreed doc-only/trivial post-review fixes" becomes "Markdown only". `docs/production_format.md`: the **Worker** definition and a new `<ProductionUnit/>` item 19 point to `<WorkerRule/>`. `commands/showrunner/produce.md`: one line — the showrunner never approves a flow without workers and sends an exception request to the user.
- The design check (screenshots) moves to a UI reviewer (`assign.sh --role review --image`) when there is no `ui` pool.
- In the same commit, call forms change to `assign.sh`: `commands/unit/direct.md` (`<LongLivedWorkers/>`, `<WorkerLaunchFailure/>`), `docs/direct/launch_implementation.md` step 5, `docs/direct/dual_review.md` step 3, `docs/direct/phase_end.md`, `commands/plan/phase_review.md`.
- `workers.py switched --unit <unit>` writes the unit and date into `~/.local/state/unit-workers/<slug>/switched.json` and runs `import-pool` for it.
- `implement.sh` and `review.sh`: the first thing each does is check the run's unit (`SHOWRUNNER_UNIT_ID`, else `unit_runs.marker`) against `switched.json`. A switched unit gets exit 2 and `refused: <unit> switched to assign.sh on <date>; use assign.sh`.
- `commands/showrunner/produce.md`: the showrunner asks each unit director, one at a time, to switch, and runs `workers.py switched` on a yes. It asks first the units with a finished run or no run (style, messaging, builds), because only `assign.sh` starts or reopens their run.
- New tag-reference test: every `<Tag/>` that `commands/unit/direct.md` and `docs/direct/*.md` name resolves to a definition.

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
- `scripts/direct/test_workers.py` — cases
- `scripts/direct/test_implement_launcher.py` — cases
- `scripts/direct/test_review_launcher.py` — cases

**Workers:** `2 writers` — docs and scripts split.
- `impl` — `workers.py`, `implement.sh`, `review.sh`, their test cases
- `test` (opens as impl) — every doc and command file, `test_tag_reference.py`; hub: `commands/unit/direct.md`

**Constraints from prior phases:**
- Phase 8: `workers.py import-pool --unit`; Phase 9: `blocked:` exit 3; Phase 10: E4 lines from all three launchers; Phase 11: `/unit:assign`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/direct scripts/hooks` is 0/0; the tag-reference test resolves `<WorkerRule/>`; a grep test finds no "spawn a fresh Claude Agent helper" and no `implement.sh --to` in `docs/direct/`; a switched unit's `implement.sh --to` call exits 2 with the refusal line; a unit not yet switched runs as before.

### Phase 13 — Worker gate E1–E3 (WS P5)  · status: todo

**Depends on:** Phase 9, Phase 12
**Approved:** the user typed the go for the `settings.json` registration in the models session (2026-10-28, reported by the models director).

#### Work Order

**Goal:** in a unit director session, a hook refuses writer subagents, code edits and coding agents started by hand.

**Spec:**
- settings.json change: register `scripts/hooks/pre-tool-use-worker-gate.py` as a PreToolUse hook for `Agent|Task`, `Edit|Write|MultiEdit|NotebookEdit` and `Bash`, and `scripts/hooks/user-prompt-subagent-grant.py` as a UserPromptSubmit hook; the checkpoint body names this change so the showrunner gates it with the new blob.
- **E1** (`Agent|Task`): any `subagent_type` except `Explore`, `Plan`, `claude-code-guide` is refused; `fork` and `general-purpose` are refused. Message: `Refused: a unit director sends worker work to its workers, not to a subagent. Use assign.sh --role implement|test|fix|review. For read-only search, use subagent_type Explore. Rule: docs/direct/worker_rule.md.`
- **User grant for E1** (user, 2026-10-28: "there are times where we can ask subagents to do work but i want that to be explicitly chosen by me - default is to use workers"). The default is refuse. The user opens one subagent task by typing a prompt line `allow subagent: <task name>` in the director session. A new `UserPromptSubmit` hook, `scripts/hooks/user-prompt-subagent-grant.py`, writes the grant (session id, task name, time) to `<run folder>/subagent_grants.json`. The hook skips a prompt that holds a `<cross-session-message`, `<agent-message` or `<task-notification` frame, so a peer, a subagent or a notice cannot open a grant. E1 then allows an `Agent|Task` call whose `description` equals a granted task name. The grant ends when the user types `end subagent: <task name>` or when the run ends. E1's refusal message names the grant line. E2 and E3 refuse any director write to `subagent_grants.json`, so only the hook writes it. A grant also opens E2 inside that subagent, for that task only.
- **E2** (`Edit|Write|MultiEdit|NotebookEdit`): in a director session and its subagents, a write to a non-Markdown file in the unit's worktree or a repository the run holds. `/tmp` and the run folder are allowed. Message: `Refused: a unit director does not edit code. Send this change to your impl worker: assign.sh --role implement …. Markdown files stay open to you.`
- **E3** (`Bash`): `codex exec`, `codex` with a prompt, `claude -p`/`--print`/`--bg`, a direct `agent_exec.sh` or `agent_bg.sh` call, and the implementation mode of `/ask_a_friend`. Allowed: `assign.sh`, `implement.sh`, `review.sh`, `workers.py`, `codex_mesh.py`, `roles.py`, `astra_review.py`, `review_pool.py`. Message: `Refused: start coding agents only with assign.sh. It opens, resumes and compacts your workers.`
- Director session: `SHOWRUNNER_UNIT_ID` is set, or `direct_run.active_run(session_id)` names a run. A Claude worker is not a director: allow a session the ledger names as a Claude worker, or one with `UNIT_WORKER=<name>`.
- On any hook fault, allow and write one log line, as the change-review hook does. Off switch: `UNIT_WORKER_GATE=off` in the `env` block of `settings.json`, the user's alone.
- Before registering, replay the hook over the last 7 days of director tool calls and report each refusal that would have happened, by unit and by kind.

**Files:**
- `scripts/hooks/pre-tool-use-worker-gate.py` — new
- `scripts/hooks/user-prompt-subagent-grant.py` — new
- `scripts/hooks/test_worker_gate.py` — new
- `scripts/hooks/test_settings_hook_paths.py` — new: every hook `command` in `settings.json` that names a `scripts/hooks/` file names one that exists
- `settings.json` — the registration

**Workers:** `1 writer + 1 tester`
- `impl` — `pre-tool-use-worker-gate.py`, the replay, `settings.json`
- `test` — `test_settings_hook_paths.py` (fails on a hook path that names a missing file, for example the old `session-start-delegate-resume.py`); `test_worker_gate.py`: each matcher; worker exemption by ledger and by `UNIT_WORKER`; subagent edit refusal; a typed grant opens one named task and `end subagent:` closes it; a grant line inside a cross-session, agent-message or task-notification frame opens nothing; a director write to the grant file is refused; fail-open with a log line; the off switch

**Constraints from prior phases:**
- Phase 1: the `compact` hook names `scripts/hooks/session-start-direct-resume.py`. No test checks that a `settings.json` hook command names a file that exists; this phase adds `test_settings_hook_paths.py`.
- Phase 9: self-heal exists, so a refused director always has a worker it can start.
- Phase 12: the rule doc is `docs/direct/worker_rule.md`.

**Acceptance gate:** the merge gate is green; basedpyright on `scripts/hooks` is 0/0; the replay shows 0 refusals of `Explore`, `assign.sh`, `implement.sh` or `review.sh` calls.

### Phase 14 — Gate announce and the three-day audit (WS P6, without the as-built)  · status: todo

**Depends on:** Phase 13
**Blocked by:** three days after the gate goes on, for the audit report.

#### Work Order

**Goal:** every unit knows the gate is on, and an audit after three days shows no writer subagents.

**Spec:**
- One `/unit:announce` when the gate goes on: the rule, `assign.sh`, and the `blocked:` path.
- `workers.py audit` reports, for each live unit: its workers, its writer subagents since the gate went on (target 0), its worker replacements, and every E1–E7 refusal with its text (from the hook log and the `Refused:`/`Held:` lines).

**Files:**
- `scripts/direct/workers.py` — `audit`
- `scripts/direct/test_workers.py` — audit cases

**Workers:** `1 writer + 1 tester`
- `impl` — `workers.py`
- `test` — the audit lists every live unit, counts writer subagents and names each refusal

**Constraints from prior phases:**
- Phase 4: ledger; Phase 9: `replaced` events; Phase 10 and Phase 13: refusal texts.

**Acceptance gate:** the merge gate is green; the three-day audit lists every live unit with writer subagents = 0.

### Phase 15 — Remove the old launchers; write the as-built (WT P6 + WS P6 as-built)  · status: todo

**Depends on:** Phase 12, Phase 14
**Blocked by:** every unit of every running production is in `switched.json`.

#### Work Order

**Goal:** `assign.sh` is the only worker launcher, and one as-built doc describes workers.

**Spec:**
- Remove `scripts/direct/implement.sh`, `review.sh`, `review_pool.py` and their tests (`test_implement_launcher.py`, `test_review_launcher.py`, `test_launcher_modes.py`, `test_review_pool.py`). Port any case still needed to `test_assign_launcher.py` first.
- `review_pool_report.py` reads only the ledger. `worker_provenance.py record` has one caller, `assign.sh`. The worker gate's E3 allow list drops `implement.sh`, `review.sh` and `review_pool.py` (hook code; no `settings.json` change).
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
- `scripts/hooks/pre-tool-use-worker-gate.py`
- `scripts/hooks/test_worker_gate.py`
- `docs/as-built/workers.md` — new
- `docs/as-built/long-lived-workers.md` — removed
- `docs/as-built/review-pool.md` — removed

**Workers:** `1 writer + 1 tester`
- `impl` — removals, `review_pool_report.py`, the hook, the as-built
- `test` — `test_worker_gate.py` and `test_review_pool_report.py` updates; a grep test that no file names `implement.sh`

**Constraints from prior phases:**
- Phase 12: `switched.json` path and the refusal; Phase 13: the allow list.

**Acceptance gate:** the merge gate is green (the lib suite included); basedpyright on `scripts/direct scripts/hooks` is 0/0; the tag-reference test resolves every tag; no file names `implement.sh`.
