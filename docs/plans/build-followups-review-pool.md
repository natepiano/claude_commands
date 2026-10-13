# review-pool

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each production gets its own pool of long-lived Codex reviewers; a script hands every review to one at once, and a trial on Hana measures tokens per review against a unit's own seats.

> **Production: build-followups** — unit `review-pool-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` — the Claude Code configuration repository: commands, the `/unit:direct` launchers and the Codex mesh.
- **Project started:** 2026-10-12T18:56:58.313+00:00
- **Stack:** Python 3.10+ run through `scripts/lib/py`, `unittest`, basedpyright; bash launchers; Codex CLI 0.162 `app-server` over a local websocket.
- **Layout:** `scripts/agents/` (the mesh), `scripts/delegate/` (launchers, pool, report), `commands/unit/`, `commands/showrunner/`, `commands/review/` (new), `docs/delegate/`, `docs/production_format.md`, `config/`.
- **Worktree:** `/home/natepiano/worktrees/claude-build-followups-review-pool`, branch `build-followups-review-pool`. Every seat works only there.
- **Key files:**
  - `scripts/agents/codex_mesh.py` — `Client`, `ensure_server`, `_update_roster`, `_start_turn` (l. 2350), `command_follow` (l. 1646), `_thread_start_params` (l. 1943), the parser in `main` (l. 3249)
  - `scripts/agents/test_codex_mesh.py` — its tests, with a fake app-server
  - `scripts/delegate/review.sh` — the review launcher: `--to`, `--slot`, fresh; `claim_follow`, pass recording, the tree-unchanged check
  - `scripts/delegate/test_review_launcher.py` — its tests
  - `scripts/whoami/seat_usage.py` — `parse_rollout`, `local_rollouts`, `mac_rollouts`, `TokenUsage` and the weights (uncached 1, cached 0.1, output 8); read only
  - `scripts/delegate/findings.py`, `scripts/production/review_regime.py` — ledger rows and merge holds the report counts; read only
  - `commands/unit/direct.md` — <LongLivedSeats/>, <EarlyReviewArm/>, <FixDispatch/>, <UXReview/>, <PhaseCleanup/>
  - `docs/delegate/dual_review.md` — <TeamReview/>, <ReviewPromptContract/>, <DualReview/>, <ClosureReview/>
  - `docs/delegate/phase_end.md` — <PhaseEnd/>
  - `docs/production_format.md` — Words, Production Context, <ProductionUnit/>
  - `commands/showrunner/produce.md` — <LaunchUnits/>, <MergeCheckpoint/>, <Wrap/>
  - `scripts/production/add_unit.py` — adds a Units row and starts its session
  - `docs/plans/proposal-review-unit.md` — the design this plan compiles; read for rationale only
- **Test lanes:** Python tests sit beside the code as `test_*.py`: `scripts/agents/`, `scripts/delegate/`, `scripts/production/`.
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest discover -s <dir> -p '<pattern>'` from the worktree root, with the directory and pattern each phase names.
- **Lint:** `basedpyright <the phase's Python files>`; pass is its `0 errors, 0 warnings` line (it exits 3 because pyrightconfig names a `.venv` no checkout has). `bash -n` on each edited shell file.
- **Invariants:**
  - Allocation is a script call that returns at once. It never waits, never queues and never involves a session (user, 2026-10-10: "waiting in line won't happen - we'll spawn new reviewers"; 2026-10-12: "script allocates directly for sure - i want no tokens wasted on allocation").
  - A pool belongs to one production (user, 2026-10-12: "showrunners would have their reviewer pool"). Its state is `~/.local/state/review-pool/<slug>/`, where `<slug>` is the production doc's file name without `-production.md`.
  - A reviewer edits nothing. The launcher writes every result into the requesting unit's own session directory and records the pass there; no other process writes a unit's ledger or history (proposal, section 3).
  - With no `**Review pool:**` line in the production doc, and outside a production, every command behaves as today. `review.sh --to`, `--slot` and the fresh path keep their behavior byte for byte.
  - `codex_mesh.py`: two-accounts-unit holds uncommitted edits in `_start_server`, `ensure_server` and the server record (its Phase 11). This plan edits only `_start_turn`, the `follow` parser and one new block of verbs, never the server start path (this unit's scope note, 2026-10-12).
  - Python: every signature annotated, no `Any`, no file-level ignores; basedpyright reports zero errors and zero warnings (repository CLAUDE.md).
  - Files outside this unit's **Owns** (`scripts/delegate/review_pool.py`, `commands/review/`) are named in each checkpoint notice as `also touches` (production_format.md, <ProductionUnit/> item 9).

## Phases

### Phase 1 — Mesh verbs for pooled threads, proved live  · status: done

#### As-built

- `prime --session-dir D --name N --instructions-file F --cwd C [--model M] [--service-tier T]` — `thread/start` with `developerInstructions` = the file's text, `sandbox: "read-only"`, `approvalPolicy: "never"`; then `thread/name/set`; then the roster entry `{thread_id, status: "done", role: "review", context_tokens: 0, instructions_file}`, where `instructions_file` is the file's absolute path. Starts no turn; prints the thread id.
- `fork --session-dir D --from N --name M` — `thread/resume` the source, `thread/fork` with `sandbox: "read-only"`, `approvalPolicy: "never"`, `thread/name/set`, then the same roster entry for `M`, copying the source's `instructions_file`. Exit 2 when `M` already names a thread that is not `ended`. Prints the new thread id.
- `reset --session-dir D --to N` — takes the roster claim `follow` takes (`starting` with its own pid, released on every exit), runs `thread/resume`, and calls `thread/revert {threadId, beforeTurnId}` with the first turn id from `thread/turns/list`; a thread with no turn succeeds with no revert. Sets `context_tokens` to 0 only while it still owns the claim. Exit 0 done, 1 request failed, 2 refused (busy, claimed, or not followable).
- `follow` takes `--cwd <dir>` (a turn's working directory, stored as `turn_cwd`; `start` keeps its own `cwd`), `--read-only` (`sandboxPolicy: {"type": "readOnly", "networkAccess": false}` for that turn) and `--image <path>` (repeatable; one `{"type": "localImage", "path": …}` input per image after the text). Without the flags its request is unchanged.
- `_resume_params` reads the roster's `instructions_file` and sends its text as `developerInstructions` on every `thread/resume` and on `thread/fork`, because the app-server drops standing instructions on both.
- Each of the three verbs prints a failure as `codex_mesh: <name>: <reason>` and exits 1.

**Files:**
- `scripts/agents/codex_mesh.py` — the three verbs, the three `follow` flags, `_resume_params`
- `scripts/agents/test_codex_mesh.py` — tests for the verbs, the flags, and standing instructions restored on resume and fork
- `docs/plans/build-followups-review-pool-probes.md` — five live probes with their measured numbers

**Binds later work:**
- The prompt cache is per thread. First request after a reset: 212 uncached / 23,552 cached / 9 output = 2,639 weighted; after a new fork: 12,372 / 11,392 / 8 = 13,575 weighted; reset 0.199 s, fork 0.189 s. The pool script ends a lease with `reset`, prefers an existing pooled thread to a new fork, and retires with `codex_mesh.py end` a thread whose `reset` exits 1 or 2.
- `instructions_file` stays at its path for the life of every thread that names it; when the file is missing, `follow`, `fork`, `reset`, `compact` and `send` exit 1. The pool script keeps its `prime/` files in place while any thread uses them.
- `codex_mesh.py stop` marks every roster name `ended`; `idle-stop` and `sweep --stop` do not. The pool script's `stop` therefore empties the pool.
- A revert writes a new rollout file under the same thread id, and a compaction writes zero input and output counters. The trial report sums a thread's usage across all of its rollout files and has no readable cost for a compaction turn.
- Standing instructions reach every turn of a thread. In Reviews go to the pool, the pool prompt can point at them; in The review unit director and review memory, a changed standing prompt is a new file and a new template, and threads forked from the old template retire at lease end.

**Gotchas:** `reset` refuses a stale claim left by a killed launcher, where `follow` takes one over. `reset` also writes one role-assignment line to the usage log through its claim, though it runs no turn.

**Ruled out:** ending a lease by retiring the pooled thread and forking a new one — the first request after a new fork costs five times the first after a reset; a sweep change to protect pooled threads — an idle server stop leaves them followable.

### Phase 2 — The pool script  · status: done

#### As-built

- `scripts/delegate/review_pool.py` has the verbs `prime`, `arm`, `split`, `allocate`, `release`, `end-lease`, `status`, `health` and `stop`; each takes `--production <slug>`. State is `~/.local/state/review-pool/<slug>/` (`REVIEW_POOL_STATE` overrides the root): `pool.json` under `pool.lock` (one `fcntl.flock` around every read-modify-write), `prime/<sha256>.md`, `cwd/` (the template's working directory, empty), and one mesh session dir per pool lifetime, `mesh/<generation>/`. `REVIEW_POOL_MESH` overrides the path of `codex_mesh.py`.
- `pool.json`: `{"split": "hash"|"all"|"off", "generation": int, "stopping": true, "next_index": {"code": n, "ui": n}, "templates": {type: {"name", "thread_id", "prime_hash"}}, "reviewers": {name: {"type", "thread_id", "prime_hash", "lease": {"unit", "phase", "ordinal"} | null, "busy_pid": int | null, "served": [[pass, lens]], "reviews": int, "last_allocated"}}}`. `stopping` is present only while a stop is unfinished. A new file starts with `split: "hash"` and `generation: 0`; a file without `next_index` reads each counter as 1. Reviewer names are `<type><n>` (`code1`, `ui2`); types are `code` and `ui`.
- `prime --type T --instructions-file F` copies `F` to `prime/<sha256>.md` and, when the hash differs from the template's, calls `codex_mesh.py prime` as `<type>-template-<hash8>` with that copy's absolute path, records it and prints the hash. A changed prime retires idle unleased reviewers of the old hash at once; leased ones retire at lease end. A `prime/` file is deleted once no template or reviewer carries its hash.
- `arm --unit U --phase P` prints `pool` or `own`: `all` gives `pool`; `off`, a missing `pool.json` or no primed `code` template gives `own`; `hash` gives `pool` when the first byte of `sha256("U\nP")` is even. `split --set hash|all|off` stores the split.
- `allocate --type T --unit U --phase P --pass N [--lens L] --pid PID [--fresh]` takes the first match under the lock:
  1. an idle reviewer of the type leased to `(U, P)` whose `served` has no entry with pass `N` and a different lens;
  2. an idle reviewer of the type with no lease and the template's current hash; it takes the lease;
  3. a new reviewer: a name reserved under the lock with `busy_pid` and no thread id, `codex_mesh.py fork` from the template outside the lock, the thread id recorded under the lock.

  A reviewer is idle when `busy_pid` is null or names a dead process. Each name is issued once per pool lifetime from `next_index`: a retirement, a failed fork or a removed reservation never frees a name. The chosen reviewer gets `busy_pid`, `[N, L]` appended to `served` and `ordinal` raised by 1. `allocate` prints one JSON line `{"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"}`, where `mesh_dir` is the current `mesh/<generation>/`. No rule waits. Exit 1 with a reason when the type has no template or the fork fails. `--fresh` resets a reviewer chosen by rule 1 or 2 before the line prints and keeps its lease; it does not reset a reviewer it has just forked; a failed reset retires the reviewer and the rules run again.
- `release --reviewer R --pid PID` clears `busy_pid` when it is `PID` and adds 1 to `reviews`; the lease stays.
- `end-lease --unit U [--phase P]`: each idle leased reviewer is claimed, reset with `codex_mesh.py reset` outside the lock, then cleared of `lease`, `served` and `busy_pid`. A reviewer whose reset exits 1 or 2, or whose `prime_hash` is old, is retired: `codex_mesh.py end`, removed from `pool.json`, named on stderr as `review_pool: retired <name>: <reason>`. A busy reviewer is left and named on stderr. Exit 0 either way.
- `status` prints one line per reviewer: name, type, lease, busy or idle, reviews. `health [--stale-hours 12]` ends leases whose reviewers have had no `allocate` for that long and prints what it ended. `end-lease` and `health` also remove a reservation whose owner process is dead, named on stderr as `review_pool: removed <name>: reservation owner is dead`.
- `stop` raises `generation`, empties `templates` and `reviewers`, sets each name counter back to 1, keeps `split`, and runs `codex_mesh.py stop` on the ended lifetime's dir. When that call fails, `stop` exits 1 with the mesh's reason and leaves `stopping` set: `prime`, `allocate`, `end-lease` and `health` then exit 1 with `pool stop did not finish; run stop again` until a `stop` succeeds, and a repeated `stop` runs against the same ended dir.
- Every mesh call runs outside the lock. A verb reads `generation` before its mesh call and publishes the result only when it is unchanged; otherwise it drops the result and exits 1 with `pool was stopped during the mesh call; result dropped`.
- In code a lease is `Unleased` or `PhaseLease`, and a reviewer's ownership is `IdleReviewer` or `ClaimedReviewer`; `null` appears only where `pool.json` is read and written.

**Files:**
- `scripts/delegate/review_pool.py` — the pool CLI
- `scripts/delegate/test_review_pool.py` — 52 tests that run the CLI as subprocesses against a stub mesh named by `REVIEW_POOL_MESH`

**Binds later work:**
- Every caller reads `mesh_dir` from the allocation line and never builds the path.
- Reviewer names start again at `code1` after a `stop`, so anything that follows a reviewer across time keys on `thread_id`.
- A caller of `stop` reports a nonzero exit and runs `stop` again.
- The first request after a `reset` costs 2,639 weighted tokens against 13,575 after a new fork, which is why rules 1 and 2 come before rule 3.

**Gotchas:** Old `mesh/<generation>/` dirs stay on disk after `stop`. `basedpyright` exits 3 for the missing `.venv`; its `0 errors, 0 warnings` line is the pass. Real `codex_mesh.py stop` on an empty session dir exits 0. `codex_mesh.py` writes its roster late and unchecked in `fork` and `follow`, so a name used twice in one session dir can be pointed back at the older thread; the pool never reuses a name. The mesh's idle watcher may stop the app-server after 30 minutes without use; reviewers stay followable and the next mesh call restarts it.

**Ruled out:** one shared `mesh/` dir for every pool lifetime — a fork that finishes after `stop` writes into the roster the next lifetime reads; reusing the lowest free reviewer name — a process still attached to the retired thread can point the name back at it; clearing the unfinished-stop marker when the mesh stop fails — no later `stop` would reach the ended lifetime's server.

### Phase 3 — Reviews go to the pool  · status: done

#### As-built

- `review.sh --pool <type> --lease <slug>/<unit>/<phase>` goes before the positional arguments, which are unchanged. It excludes `--to` and `--slot` and requires the Codex family with the mesh (exit 2 otherwise). It calls `review_pool.py allocate … --pass N [--lens L] --pid $$`, records its pass under the slot `pool_<reviewer>`, compacts the reviewer when its recorded context is over `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS`, and runs `codex_mesh.py follow --read-only --cwd "${WORKING_DIR}"` with the review model, effort and tier. Roster, compaction, claim and claim release use the pool's `mesh_dir`; status file, findings, history, board and pass stay in the unit's `${SESSION_DIR}`. A failed allocation is an `error` status and exit 1. The early-launch argument works as on the seated path.
- The pool launcher writes its slot, one line `pool_<reviewer>`, to `${SESSION_DIR}/review_slot<suffix>` after allocation; the suffix is `_<lens>` with a lens and empty without one. Each pool launch removes that file first, so a failed allocation leaves none.
- On every exit the launcher counts the findings, reads the reviewer's thread id from the mesh roster, writes the turn record, releases the mesh claim and runs `review_pool.py release --reviewer R --pid $$ --thread-id <id>`. `release --thread-id` stores a changed id under the lock, only when `busy_pid` is the given pid.
- A launcher stopped by TERM, INT or HUP stops its child and its heartbeat watcher and leaves its pass open, on both arms. The unit director closes that pass with `finish-pass --status canceled --orphaned-launcher` under the slot it reads from the slot file; a missing file means no pass to close.
- **Turn record.** Every attempt of either arm, refused and failed ones included, writes a new file `<history root>/review_turns/<session dir name>/<started epoch>-<pid>.json`, never overwritten. The history root is `PLAN_DELEGATE_HISTORY_DIR` or `~/.local/state/plan-delegate`; `clear_phase_review.sh` never reaches it. Fields:
  - `arm` — `pool` or `own`; `pool` — the production slug on the pool arm, empty on the own arm
  - `reviewer`; `thread_id` — the roster's id after the turn, so it names the thread that ran it
  - `unit`, `phase` — from `--lease`, or from `REVIEW_LEASE` on the own arm; `phase_instance_id`, `history_file`
  - `kind` — `ux` for lens `ux`, otherwise `broad` for pass 1 with a lens, otherwise `closure`; `lens`; `pass` — an integer
  - `started_at` — whole epoch seconds; `finished_at` — fractional
  - `lease_ordinal` — 0 on the own arm; `prime_hash` — empty on the own arm
  - `raised` — the count of numbered items in the findings file, kept for every status
  - `status` — `reviewed` after a successful `finish-pass`; `void` for an early-launched review that finished before its final diff was delivered; `canceled` for TERM, INT or HUP; `error` otherwise. It is held in `TURN_RECORD_STATUS` and never derived from the exit code.
- `docs/production_format.md`: Words define **Review pool**, **Reviewer**, **Template** and **Lease**; Production Context may carry `- **Review pool:** code` (or `code, ui`); <ProductionUnit/> item 16 has a unit run `review_pool.py arm` at each phase's start, send that phase's code reviews with `--pool code --lease …` on `pool`, and export `REVIEW_LEASE` and review on its own seats on `own`.
- `commands/unit/direct.md` names the pool form beside `--to` in <LongLivedSeats/>, <EarlyReviewArm/> and <FixDispatch/>; a pool `error` falls back to `--to` for that review under <DelegateLaunchFailure/>. A cancel reads the slot from `${SESSION_DIR}/review_slot_adversary` for pass 1 or `${SESSION_DIR}/review_slot` for a closure review.
- `docs/delegate/dual_review.md`: <TeamReview/> seat choice does not apply on the pool arm; <DualReview/> shows the pool call and reads `${SESSION_DIR}/review_slot_<lens>` on a cancel; under <ReviewPromptContract/> a pool prompt opens with the pooled-reviewer line (unit, phase, the `git rev-parse HEAD` check) and its Three Gods and Type Design Contract sections each hold `As in your standing instructions.`
- `docs/delegate/phase_end.md` step 5 runs `review_pool.py end-lease --production <slug> --unit <unit> --phase <phase>` in the worker cleanup.
- `config/review_pool/code.md` is the code pool's standing prompt, under 7 KB: the role line, the Three Gods sentence, `type_design.md` verbatim, and the Rust style guide's rule headings with each rule's file path. It holds no review memory.

**Files:**
- `scripts/delegate/review.sh` — the `--pool` path, the turn record, the slot file
- `scripts/delegate/test_review_launcher.py` — 37 tests
- `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py` — `release --thread-id`; 53 tests
- `config/review_pool/code.md` — the code pool's standing prompt
- `docs/production_format.md`, `commands/unit/direct.md`, `docs/delegate/dual_review.md`, `docs/delegate/phase_end.md` — the pool arm's text

**Binds later work:**
- Only a `reviewed` record is a completed review; a report counts the other three statuses apart.
- `kind` is `ux` only for lens `ux`, so a UI pool launch passes that lens.
- A launcher killed with KILL writes no record and releases nothing; `end-lease` and `health` clear its reviewer.
- A later edit to `review.sh` keeps `TURN_RECORD_STATUS`, the slot file and `release --thread-id`.
- Switching a production's pool on takes `review_pool.py prime --production <slug> --type code --instructions-file config/review_pool/code.md` and the `**Review pool:**` line in its doc.

**Gotchas:** A bash `EXIT` trap sees exit code 0 when the shell dies of TERM with no TERM trap, which is why the status is a variable and both arms trap the three signals. `codex_mesh.py follow` can replace a thread whose resume fails, and writes the new id to the roster.

**Ruled out:** the pool launcher closing its own pass on a signal — the unit director's cancel call then fails on a closed pass, and the two arms differ; a pool prompt that points at `config/review_pool/code.md` — a leased reviewer holds the earlier pinned text; the record's status derived from the exit code.

### Phase 4 — The trial report  · status: done

#### As-built

- `review_pool_report.py --since <date> [--root <history root>] [--no-mac] [--quality]` reads every `review_turns/*/*.json` under the root (default `PLAN_DELEGATE_HISTORY_DIR` or `~/.local/state/plan-delegate`), plus the Mac's records, history files and merge holds over ssh to `collect.MAC_HOST`. A record whose `lease_ordinal` or `raised` is not an integer is skipped and named on a `source warning` line.
- Review cost comes from the shared usage store, not from Codex logs: `usage_report.open_read_store(collect.store_path())` (`scripts/usage/collect.py`), tables `openings`, `folders`, `turns`, `compactions`, `turn_sources`, selected by the records' thread ids. The store names this machine `natedev`; records call it `local`. Each request is weighed per model and time through `weights.weights_at` and `weights.weighted` (`scripts/usage/weights.py`); the report holds no rollout parsing and no weights. Two warning lines report on the store: `source warning: usage store unavailable: <reason>` and `source warning: Mac usage source <state>; last complete read <time>`.
- A request belongs to at most one record: the earliest-started record of its thread whose window from `started_at` to `finished_at` contains the request's time. `started_at` is whole seconds, so two windows on one thread can overlap inside one second; the overlap goes to the earlier record. Records of every status own requests, so a `void` or `canceled` turn's tokens never count for a `reviewed` one.
- Output: a table by arm (`pool`, `own`) and kind (`broad`, `closure`, `ux`) with reviews, median and mean weighted tokens, and share from cached input; a pool table by lease ordinal (1, 2, 3+); one line per arm counting `void`, `canceled` and `error` records. Reviews, medians, means and `raised` sums take `reviewed` records only.
- `cost incomplete: <n>` names each `reviewed` record left out of medians and means with one reason, the first that applies: `missing rollout` (the store has no such thread), `no request in its window`, `a compaction after its first request`; a request whose model has no weights gives `unweighted model <model>`. `began with a compaction: pool <n>; own <n>` counts `reviewed` records whose window holds a compaction before the first request; their cost stays complete.
- `--quality`, per arm and joined by phase instance: raised, confirmed (`finding_opened` events in the records' `history_file` with `caught_by` `delegate` or `both`; lens `ux` counts for kind `ux`, the rest for the code kinds), precision, and escapes per phase (`caught_by` `main` events plus merge holds from `review_regime.py`). A phase's arm is the arm of its records of every status; a phase with records of both arms is `mixed`: counted in one line and left out of the comparison. An unreadable merge-hold ledger prints `source warning: merge holds unavailable: <reason>` and quality continues without holds.
- Final line: `pool reviews: N of 20 for the first reading` until 20, then `pool is at X% of own-seat tokens per review`. N and the comparison count kinds `broad` and `closure` only. At 20 with no complete costs on one arm: `pool tokens per review: no comparison yet (complete costs needed for both arms)`.

**Files:**
- `scripts/delegate/review_pool_report.py` — the report
- `scripts/delegate/test_review_pool_report.py` — 35 subprocess tests; `populate_usage_store` collects fixture rollouts into a fixture store, and `USAGE_STATE_DIR` points the script at it

**Binds later work:**
- The review unit director and review memory: the report runs on natedev and reads the usage store; its tables carry costs only once the store exists; the `source warning` line goes with the dailies tables. `test_review_launcher.py` has 39 tests.
- The UI pool, in shadow first: a `ux` pool review never counts toward the 20; the report's tests need a fixture store built by `populate_usage_store`; `--shadow` needs no store; each `--fresh` reset starts a new file, where the collector's key can lose a request.

**Gotchas:**
- The hourly `usage-collect` job on natedev builds the store for both machines; the Mac has no store of its own. With the store absent, every `reviewed` record reads `cost incomplete … (missing rollout)`.
- `review.sh` reads its start time before it compacts a large thread, so most own-arm windows open with a compaction; the first request after it re-reads the kept context (about 86,000 uncached tokens), which leans the comparison toward the pool. A compaction keeps 55,000 to 108,000 tokens, so such a thread is compacted again at nearly every follow-up.
- The tests call the collector's `SCHEMA`, `migrate_store`, `read_file`, `save_measurement` and `RoleResolver`; a rename in `scripts/usage/collect.py` breaks them.
- The collector keys a Codex request by its byte offset inside one file and inserts with `INSERT OR IGNORE`, so the fixtures prefix the key with the file path; two real files of one thread can lose a request at a shared offset.
- The `usage store unavailable` and `Mac usage source` warning lines have no test.

**Ruled out:**
- Moving `review.sh`'s start time after its compaction: the launcher belongs to the model-study unit, and the report counts those records itself.
- Reading rollout files through `scripts/usage/rollouts.py`: the store already holds every request with its model, time and compactions for both machines.

### Phase 5 — The review unit director and review memory  · status: done

#### As-built

- `/review:direct <production doc>` (`commands/review/direct.md`) is the review unit director's command. It dispatches no review and answers no allocation. At start and on resume it records one standing phase (`progress_history.py start-run`, then `start-phase --phase-id 1`, in `<history root>/review_units/review-<slug>`; both reuse an existing run), builds each type's standing prompt (`config/review_pool/<type>.md` plus that type's section of `memory/repository.md`) into `<pool dir>/standing/<type>.md`, runs `review_pool.py prime`, and registers an hourly health message to itself (`notifier.sh new review-health-<slug> --every 60`).
- On each showrunner line `review merge: unit <unit>; phase <phase>; session dir <path>; design check: <verdict>` it reads `<history root>/runs/<session dir name>.jsonl` (local, else the Mac's over ssh at `collect.MAC_HOST`), joins the merged phase's `finding_opened` and `finding_verdict` events by `finding_id` and `phase_instance_id`, and records in memory a defect class seen in two or more findings, a ruling by a unit director or the showrunner, and a defect that got past review. A finding with no verdict is open and is processed with the rest; only an unreadable history file keeps a merge pending.
- Memory lives under `~/.local/state/review-pool/<slug>/memory/` (`REVIEW_POOL_STATE` replaces the root): `repository.md` with `code` and `ui` sections, at most 4,000 and 3,000 tokens (UTF-8 bytes divided by four, rounded up; the least-cited entry is pruned at the cap), and `<unit>.md`. Each entry carries a date and its sources. Only the director writes memory. After a repository section changes, the director primes that type again. A turn record's `prime_hash` names the memory a reviewer received, and no separate version count exists; a reviewer leased before a new `prime` keeps its earlier `prime_hash` through closure.
- `review_pool.py unit-memory --production S --unit U` prints `memory/<unit>.md`, or nothing when it is absent; a unit name with a directory component is refused.
- `review.sh --pool` prepends that text to the prompt when `lease_ordinal` is 1: it writes `${SESSION_DIR}/review_message_<pass><suffix>.XXXXXX` (the memory, a blank line, the prompt), points `PROMPT_FILE` at it and removes it in `finish_review_exit`. With no memory the prompt goes unchanged.
- Health: `review_pool.py health` each hour; the showrunner hears only when a lease was ended or a command failed. Dailies: `review_pool_report.py --since <date> --quality` on natedev, where the date is the day the production doc entered Git (`git log --diff-filter=A --follow --format=%as`). At shutdown the director removes its timer and runs `progress_history.py finish-run --status completed`.
- `scripts/production/add_unit.py` opens `/review:direct <doc>` for the unit named `review-unit` when the doc has a `- **Review pool:** ` line; every other unit opens `/unit:direct`.
- `commands/showrunner/produce.md`: the showrunner launches the review unit first, sends the merge line after each merged checkpoint (the unit's session directory is the newest run record whose `working_dir` is its Worktree cell), asks for the trial tables at dailies, and runs `review_pool.py stop` at wrap.
- `docs/production_format.md`: Words gain **Review unit**; the Units table carries it as an ordinary row (worktree, branch, a plan doc with one standing phase that stays `todo`) with Owns `—`. `/review:direct` writes that plan when it is absent. An idle review director ends its turn with `— holding: waiting for review requests`.

**Files:**
- `commands/review/direct.md` — the review director's command
- `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py` — `unit-memory`; 57 tests, one pinning the leased reviewer's earlier `prime_hash`
- `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py` — the unit memory at lease ordinal 1; 43 tests
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py` — the review unit's opening message
- `scripts/production/test_review_unit_phase.py` — 3 tests: the standing phase reads as `1 of 1`, running; a resume adds no run and no phase; shutdown finishes the run while the plan heading stays `todo`
- `commands/showrunner/produce.md`, `docs/production_format.md` — the showrunner's side

**Binds later work:**
- The UI pool, in shadow first: the launcher sends a unit's memory at lease ordinal 1 only, with the text from `review_pool.py unit-memory`; a `ui` turn starts from a reset thread, so it needs that memory on every turn. `test_review_launcher.py` has 43 tests and `test_review_pool.py` 57.

**Gotchas:**
- The director's session directory lives under the history root, never under `/tmp`: no `prepare_session.sh`, no run-active marker, no report timer.
- `start-run` returns early when the session's state file exists, so a resume from another session is safe; with no `--main-*` flags the recorder detects the director's model itself.
- `health` prints ended leases on stdout and busy reviewers on stderr with `lease left intact`; busy is normal and is not reported.
- A production doc that was never committed has no start date; the director reports that and runs no report.
- No live pool, live director session or Mac history has exercised this command; the tests cover the scripts and the recorder calls.
- The showrunner finds a unit's session directory by reading run records; no script prints it.

**Ruled out:**
- A date typed by hand onto the production log's title line: existing productions have none.
- The director reading its own model and effort out of its transcript: the recorder detects them.
- `findings.py status` for a merged phase: the unit has usually moved on, and it shows only the active phase.

### Phase 6 — The UI pool, in shadow first  · status: todo

#### Work Order

**Goal:** A long-lived Codex UI reviewer judges the same shots as today's design-check helper, each verdict from a freshly reset thread; its verdicts are recorded and not used until ten checks show it misses nothing.

**Spec:**
- `config/review_pool/ui.md`, at most 6 KB: the role line, the Three Gods sentence, the UX checklist from `zsh ~/.claude/scripts/ux_style/load-ux-style.sh --checklist` with each rule file's path, and `commands/ux_eval.md`'s report format.
- `review.sh --pool ui --lease …` with lens `ux`: `allocate --type ui --fresh`, so the reviewer is reset before the turn, no earlier shot is in its context, and it keeps its lease; a failed allocation is an `error` status and no turn runs. Each `ui` turn starts from a reset thread, so the launcher prepends the unit's memory (`review_pool.py unit-memory`) on every `ui` turn; the `code` path keeps sending it at lease ordinal 1 only. Shots go as `--image` arguments read from `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`, one path per line. For lens `ux` the record's `raised` counts the data rows of the defect table in the findings file (`commands/ux_eval.md` <Report/>: the table headed `| Shot | Rule | Defect | Fix |`), never a header or separator row and never a row of the `Canonical candidates` table; today `count_turn_findings` counts numbered items only, which is 0 for that format.
- `commands/unit/direct.md` <UXReview/> steps 3, 5 and 6: with `ui` in the production's `**Review pool:**` line, every design check also launches the pool review on the same shots: the first check (step 3), a recheck after a repair (step 5, which replaces steps 1–4 and starts its own helper) and the check for the notice (step 6). Both verdict tables are saved as a pair beside the turn records, in `<history root>/review_turns/<session dir name>/`: `ux_<phase>_<k>_helper.txt` and `ux_<phase>_<k>_pool.txt`, where `k` counts the phase's design checks from 1, a recheck after a repair included. The Claude helper's verdict is the one used in steps 3, 5 and 6, unless the file `~/.local/state/review-pool/<slug>/ui_adopted` exists; then the pool's verdict is used and no helper runs. No code creates that file: a missing file, a new pool and a new production all mean shadow. `review_pool.py stop` removes it and says so on stderr (`review_pool: ui adoption cleared`), so a pool primed after a `stop` starts in shadow.
- `review_pool_report.py --shadow`: per design check (each `_helper` and `_pool` pair), the helper's defect rows, the pool's, and the rows the pool missed; a check with one file of its pair missing is named.
- `review_pool_report.py`, with the usage store absent or unreadable: each `reviewed` record's `cost incomplete` reason reads `usage store unavailable`, where today it reads `missing rollout`; the `source warning: usage store unavailable: …` line stays. `missing rollout` remains the reason when the store is read and holds no such thread.
- Leaving shadow is the user's decision, on the report after ten checks (proposal, section 7); `ui_adopted` is then created by hand.
- Tests: `--pool ui` passes `--fresh` and one `--image` per listed shot; a failed allocation runs no turn; a `ui` turn at lease ordinal 2 still carries the unit's memory before the prompt; `--shadow` finds the missed rows in fixture pairs, a recheck pair included, and names an unpaired file; for lens `ux`, `raised` counts defect rows and neither the header nor a canonical-candidate row; `stop` removes `ui_adopted`, and a `prime` after it leaves the file absent; with no usage store the report names each reviewed record `usage store unavailable` and prints the warning line; a store whose `turn_sources` row for the Mac is not `ready` prints `source warning: Mac usage source <state>; last complete read <time>`.

**Files:**
- `config/review_pool/ui.md` — new
- `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py` — the `ui` path and the `ux` row count
- `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py` — `stop` clears the adoption marker
- `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py` — `--shadow`, and the reason and tests for a missing usage store
- `commands/unit/direct.md` — <UXReview/>

**Seats:** 2 writers — the launcher with its tests; the report with its tests and the text.
- `impl` — `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py`, `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py`
- `test` — opens as impl: `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py`, `config/review_pool/ui.md`, `commands/unit/direct.md`

**Constraints from prior phases:** `follow --image` exists. `review.sh --pool` sends a unit's memory at lease ordinal 1: when `review_pool.py unit-memory --production S --unit U` prints text, the launcher writes `${SESSION_DIR}/review_message_<pass><suffix>.XXXXXX` (the memory, a blank line, the prompt), points `PROMPT_FILE` at it and removes it in `finish_review_exit`; with no memory file the prompt goes unchanged. `test_review_launcher.py` has 43 tests and `test_review_pool.py` 57. `allocate --fresh` is in The pool script; it skips the reset for a reviewer it has just forked, which has no earlier turn. The turn-record directory is in Reviews go to the pool. The review unit director primes every type listed on the `**Review pool:**` line from `config/review_pool/<type>.md`. A turn record's `kind` is `ux` only when the lens is `ux`; `--pool ui` alone does not set it, so the UI launch passes lens `ux`, which `review.sh` accepts. Record `status` is `reviewed`, `void`, `canceled` or `error`. The pool launcher writes `pool_<reviewer>` to `${SESSION_DIR}/review_slot<suffix>`; the unit director reads it to cancel a stopped pool review. `review_pool_report.py` reads requests, models and compactions from the shared usage store by thread id (`usage_report.open_read_store(collect.store_path())`) and weighs them through `scripts/usage/weights.py`; it holds no rollout parsing. `--shadow` reads only the `ux_*` pairs and needs no store. The report's final line counts kinds `broad` and `closure` only, so a `ux` pool review never counts toward the 20. `test_review_pool_report.py` has 35 tests; its `report()` helper builds a fixture store from the fixture rollouts through `populate_usage_store`, with `USAGE_STATE_DIR` pointing the script at it. The collector keys a Codex request by its byte offset inside one rollout file and inserts with `INSERT OR IGNORE`, so a second file of one thread loses a request that sits at an offset the first file used; a `--fresh` reset starts a new file each time, so a `ux` cost row can read low until usage-ledger keys by file (sent to the showrunner 2026-10-12).

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_*.py'` green; `basedpyright` on the edited Python files at 0 errors, 0 warnings; `bash -n scripts/delegate/review.sh`; one live shadow check on a stored Hana shot returns `ux_eval`'s table.
