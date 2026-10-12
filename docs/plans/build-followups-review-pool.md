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

### Phase 2 — The pool script  · status: todo

#### Work Order

**Goal:** `review_pool.py` gives any review a reviewer in one call, keeps a phase's reviews on the same reviewers, and clears them when the phase ends.

**Spec:**
- State in `~/.local/state/review-pool/<slug>/` (override the root with `REVIEW_POOL_STATE` for tests): `pool.json`, `pool.lock` (one `fcntl.flock` around every read-modify-write), `mesh/` (the mesh session dir the reviewers live in), `prime/` (standing prompt files, named by hash).
- `pool.json`: `{"split": "hash"|"all"|"off", "templates": {type: {"name", "thread_id", "prime_hash"}}, "reviewers": {name: {"type", "thread_id", "prime_hash", "lease": {"unit", "phase", "ordinal"} | null, "busy_pid": int | null, "served": [[pass, lens]], "reviews": int}}}`. Reviewer names are `<type><n>`: `code1`, `ui2`. Types are `code` and `ui`.
- `prime --production S --type T --instructions-file F`: copy `F` to `prime/<sha256>.md`; when the hash differs from the template's, call `codex_mesh.py prime` under the name `<type>-template-<hash8>` with `--instructions-file` set to that copy's absolute path, and record it. Prints the hash. The mesh stores that path and re-reads the file on every later turn, fork, reset and compact of the template and its reviewers, so a file under `prime/` is never rewritten, and is deleted only once no template or reviewer in `pool.json` carries its hash.
- `arm --production S --unit U --phase P`: prints `pool` or `own`. `split` `all` gives `pool`; `off`, a missing `pool.json`, or no primed `code` template gives `own`; `hash` gives `pool` when the first byte of `sha256("U\nP")` is even.
- `split --production S --set hash|all|off`: stores it.
- `allocate --production S --type T --unit U --phase P --pass N [--lens L] --pid PID`: under the lock, first match wins:
  1. an idle reviewer of the type whose lease is `(U, P)` and whose `served` has no entry with pass `N` and a different lens;
  2. an idle reviewer of the type with no lease; it takes the lease with `ordinal` 0;
  3. a new reviewer: under the lock, reserve the next free name with `busy_pid` set and no thread id; outside the lock, `codex_mesh.py fork` from the type's template; under the lock again, record the thread id. A failed fork removes the reservation. A reserved name without a thread id is never chosen by rules 1 or 2.

  A reviewer is idle when `busy_pid` is null or names a dead process. A reviewer whose `prime_hash` differs from the template's is skipped by rule 2. The chosen reviewer gets `busy_pid`, `[N, L]` appended to `served`, and `ordinal` raised by 1. Prints one JSON line: `{"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"}`. No rule waits. Exit 1 with a reason only when the type has no template or the fork fails. With `--fresh`, the chosen reviewer is reset (`codex_mesh.py reset`, outside the lock, under this allocation's own `busy_pid`) before the JSON line prints, and keeps its lease; a reviewer whose reset fails is retired as in `end-lease` and the rules run again.
- `release --production S --reviewer R --pid PID`: clears `busy_pid` when it is `PID`, adds 1 to `reviews`; the lease stays.
- `end-lease --production S --unit U [--phase P]`: for each reviewer leased to the unit (and phase, when given) that is idle: under the lock set `busy_pid` to this process, so no `allocate` takes it mid-reset; outside the lock call `codex_mesh.py reset`; then under the lock clear `lease`, `served` and `busy_pid`. `reset` exits 0 when done, 1 when a request failed, 2 when the mesh refuses the reviewer (claimed, or not followable: a killed review can leave it so). On 1 or 2 the reviewer is retired: `codex_mesh.py end` on its name, removed from `pool.json`, named on stderr; the next `allocate` forks a new one. A reviewer whose `prime_hash` is old is retired the same way without a reset. A busy reviewer is left and named on stderr. Exit 0 either way.
- `status --production S`: one line per reviewer: name, type, lease, busy or idle, reviews.
- `health --production S [--stale-hours 12]`: ends leases whose reviewers have had no `allocate` for that long (keep a `last_allocated` stamp), and prints what it ended.
- `stop --production S`: `codex_mesh.py stop` on `mesh/`, which ends every name in the mesh roster; so it also empties `templates` and `reviewers` in `pool.json` (keeping `split`), and the next `prime` starts new templates. It ends the pool; nothing uses it to restart a server.
- Mesh calls are subprocess calls to `scripts/agents/codex_mesh.py`, and every one is made outside the lock: a mesh request can take minutes, and no `allocate` waits behind another caller's. `pool.json` carries a `generation` integer that `stop` raises. Each verb that makes a mesh call reads the generation under the lock before the call, and after it publishes its result only when the generation is unchanged; otherwise it drops the result and exits 1 naming the stop.
- A new `pool.json` starts with `split: "hash"`, `generation: 0` and no templates or reviewers. The template's `--cwd` is `~/.local/state/review-pool/<slug>/cwd/`, created empty.
- In code a lease and a reviewer's ownership are types, not optional fields: `Unleased` or `PhaseLease(unit, phase, ordinal)`; `IdleReviewer` or `ClaimedReviewer(pid)`. `null` appears only where `pool.json` is read and written.

**Files:**
- `scripts/delegate/review_pool.py` — new
- `scripts/delegate/test_review_pool.py` — new
- `pyrightconfig.json` — one entry if the file lists checked paths

**Seats:** 1 writer + 1 tester — code and tests split by file.
- `impl` — `scripts/delegate/review_pool.py`; hub: `pyrightconfig.json`
- `test` — `scripts/delegate/test_review_pool.py`: from the Spec, with `REVIEW_POOL_STATE` in a temp dir and a stub `codex_mesh.py` on a path override (`REVIEW_POOL_MESH`): the three allocation rules in order, the other-lens exclusion, dead-pid idleness, two concurrent `allocate` calls getting two reviewers, a stalled `fork` that does not block a second `allocate`, `stop` during a delayed mesh call (the late result is dropped), `allocate --fresh`, `arm` for each `split`, `end-lease` on idle and busy reviewers and on each `reset` exit code, old-hash retirement, a `prime/` file kept while a reviewer carries its hash, a second `prime` with a changed file giving the next new lease the new hash. The stub rejects a mesh call with a missing required argument

**Constraints from prior phases:** Mesh verbs, all in `scripts/agents/codex_mesh.py` (read each parser for its arguments): `prime --session-dir D --name N --instructions-file F --cwd C [--model M]` prints the thread id; `fork --session-dir D --from N --name M` prints the new thread id and refuses (exit 2) a name that belongs to a live thread; `reset --session-dir D --to N` (exit codes above) claims the reviewer itself while it runs; `end` ends one roster name; `stop` ends them all. Measured in `docs/plans/build-followups-review-pool-probes.md`: lease end is `reset`, because the first request after a reset costs 2,639 weighted tokens against 13,575 after a new fork (the cache is per thread, so a new fork reads about half its first request uncached); this is why rule 1 and rule 2 come before rule 3. The mesh's own idle watcher may stop the app-server after 30 minutes without use; reviewers stay followable and the next mesh call restarts it, so the pool script does nothing about it.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_pool.py'` green; `basedpyright scripts/delegate/review_pool.py scripts/delegate/test_review_pool.py` at 0 errors, 0 warnings.

### Phase 3 — Reviews go to the pool  · status: todo

#### Work Order

**Goal:** A unit director in a production with a review pool sends a phase's reviews through `review.sh --pool code`, and each review of either arm leaves a turn record.

**Spec:**
- `review.sh --pool <type> --lease <slug>/<unit>/<phase>` before the positional arguments, which are unchanged. It excludes `--to` and `--slot` (exit 2). Steps:
  1. Resolve the agent as today; require the Codex family and mesh (exit 2 otherwise).
  2. `review_pool.py allocate … --pass "${PASS_INDEX}" [--lens "${LENS}"] --pid $$`. On failure: `error` status, exit 1.
  3. `TEAM_SLOT="pool_<reviewer>"`; artifacts, status file, board post and `start-pass` as the seated path does, in the unit's `${SESSION_DIR}`.
  4. Compact when the reviewer's recorded context is over `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS`, then `codex_mesh.py follow --session-dir <mesh_dir> --to <reviewer> --role review --cwd "${WORKING_DIR}" --read-only --message-file … --summary-file "${FINDINGS_FILE}" --reply-file … --log-file …` with the review model, effort and tier.
  5. The tree-unchanged check, `finish-pass` and the status file as the seated path does.
  6. An `EXIT` trap runs `review_pool.py release`.
- The early-launch argument works as on the seated path.
- **Turn record**, written for `--pool` and for `--to` alike at the end of every pass, a failed one included: a new file `<history root>/review_turns/<session dir name>/<started epoch>-<pid>.json`, never overwritten. The history root is `PLAN_DELEGATE_HISTORY_DIR` or `~/.local/state/plan-delegate`, the home of the run history: it outlives `/tmp`, and `clear_phase_review.sh` never reaches it. Fields: `{"arm": "pool"|"own", "pool", "reviewer", "thread_id", "unit", "phase", "phase_instance_id", "history_file", "kind": "broad"|"closure"|"ux", "lens", "pass", "started_at", "finished_at", "lease_ordinal", "prime_hash", "status", "raised"}`. `kind` is `ux` for lens `ux`; otherwise `broad` for pass 1 with a lens; otherwise `closure`. `raised` is the count of numbered items in the findings file, read before the pass ends, so the report needs no findings file. `phase_instance_id` and `history_file` come from `${SESSION_DIR}/progress_history_state.json` (read the file for the exact keys). For `--to`, `thread_id` comes from the unit's mesh roster, and `unit` and `phase` from `REVIEW_LEASE` when set, else empty.
- Tests in `test_review_launcher.py`: the pool path reads the roster and takes compaction, claim and release in the pool's `mesh_dir` while its status, history and board lines go to `${SESSION_DIR}`; a compaction that exits 3 still refuses the review; two phases in a row leave both phases' records; a failed pool attempt followed by a `--to` retry leaves one record of each arm; pass 1 with lens `ux` is kind `ux`.
- `docs/production_format.md`: Words gain **Review pool**, **Reviewer**, **Template**, **Lease** (proposal, section 1); Production Context gains `- **Review pool:** code` (or `code, ui`), omitted when the production has none; the **Seat** entry's last sentence becomes "Reviews run on the unit's seats, or on the production's review pool when its doc names one."; <ProductionUnit/> gains one item: with a `**Review pool:**` line, at each phase's start run `review_pool.py arm`, and on `pool` send that phase's code reviews with `--pool code --lease …`; on `own` export `REVIEW_LEASE` and review as today.
- `commands/unit/direct.md` <LongLivedSeats/>, <EarlyReviewArm/> step 5 and <FixDispatch/>'s closure paragraph: name the pool form beside `--to`. A pool `error` falls back to `--to` for that review under <DelegateLaunchFailure/>, said in one line.
- `docs/delegate/dual_review.md`: <TeamReview/> seat choice does not apply on the `pool` arm; <DualReview/> step 3 shows the pool call; <ReviewPromptContract/>: a pool prompt opens with `You are a pooled reviewer for <unit>, phase <N>. Edit nothing. Your first command is git -C <worktree> rev-parse HEAD; if it is not <head>, report error. Re-read the tree; trust nothing from memory about its state.` and carries the Three Gods sentence and the Type Design Contract by pointer to its standing prompt.
- `docs/delegate/phase_end.md` step 5: with a review pool, run `review_pool.py end-lease --production <slug> --unit <unit> --phase <phase>` in the worker cleanup.
- A first standing prompt for the code pool at `config/review_pool/code.md`, at most 7 KB: the role line, the Three Gods sentence, `type_design.md` verbatim, and the Rust style guide's rule headings with each rule's file path (proposal, section 2). No review memory yet.

**Files:**
- `scripts/delegate/review.sh` — the `--pool` path and the turn record
- `scripts/delegate/test_review_launcher.py` — tests for both
- `config/review_pool/code.md` — new
- `docs/production_format.md`, `commands/unit/direct.md`, `docs/delegate/dual_review.md`, `docs/delegate/phase_end.md` — the text above

**Seats:** 2 writers — the launcher and its tests; the command and contract text.
- `impl` — `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py`
- `test` — opens as impl: `config/review_pool/code.md`, `docs/production_format.md`, `commands/unit/direct.md`, `docs/delegate/dual_review.md`, `docs/delegate/phase_end.md`

**Constraints from prior phases:** `review_pool.py` verbs and their output are in The pool script. `follow` takes `--cwd`, `--read-only`, `--image` and a session dir any number of launchers share. Recorder slot names allow letters, digits and `_` only, hence `pool_<reviewer>`. Today `review.sh` (`claim_follow`, the compaction call, the claim release and the cleanup, near lines 233–303) reads the roster, compacts, claims and releases in `${SESSION_DIR}`; changing only the `follow` call's session dir is not enough. On the pool path each of those uses the pool's `mesh_dir`, and everything the unit reads back stays in its own `${SESSION_DIR}`: status file, findings, history, board. The mesh sends a reviewer's standing prompt again on every resume, so the prompt reaches every turn and a pool prompt may point at it.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_launcher.py'` green; `basedpyright scripts/delegate/test_review_launcher.py` at 0 errors, 0 warnings; `bash -n scripts/delegate/review.sh`; a live check in this worktree: prime the code template from `config/review_pool/code.md` under a scratch slug, run one `--pool code` review of a two-line diff, and see findings, a `reviewed` status, a turn record and an idle reviewer with its lease; then `end-lease` and `stop`. After this merges to main, the showrunner asks Hana's showrunner to add `**Review pool:** code` and prime the template: the trial starts there.

### Phase 4 — The trial report  · status: todo

#### Work Order

**Goal:** One command prints weighted tokens per review for the pool arm and the own-seat arm, and the counts that show whether reviews improve.

**Spec:**
- `review_pool_report.py --since <date> [--root <history root>] [--no-mac]`: read every `review_turns/*/*.json` under the root (default `PLAN_DELEGATE_HISTORY_DIR` or `~/.local/state/plan-delegate`; and the Mac's, over ssh as `seat_usage.py` reaches the Mac), find every rollout file of each record's thread (a reset starts a new file under the same thread id), and sum the token usage between `started_at` and `finished_at` across them. Import `parse_rollout`, `is_compaction` and the weights from `scripts/whoami/seat_usage.py`; do not edit that file. Its `local_rollouts` and `mac_rollouts` return rollouts without their file path or thread id, so this script finds the files itself: each `*.jsonl` under `~/.codex/sessions/*/*/*/` modified since `--since`, keyed by the thread id in the file's first record (read a real rollout for the field), keeping machine, path and thread id beside each parsed rollout.
- Print one table by arm (`pool`, `own`) and kind (`broad`, `closure`, `ux`): reviews, median and mean weighted tokens, share from cached input. For the pool, a second table by lease ordinal (1, 2, 3+).
- `--quality`: per arm, joined by phase instance: raised (the sum of `raised` over the phase's records), confirmed (`finding_opened` events in the records' `history_file` with that `phase_instance_id` and `caught_by` `delegate` or `both`; rows with lens `ux` count for kind `ux`, the rest for the code kinds), precision, and escapes per phase (`finding_opened` events with `caught_by` `main`, and merge holds from `review_regime.py`). A phase's arm is its records' arm. A phase with records of both arms (a pool review that fell back to an own seat) is `mixed`: counted in one line and left out of the comparison.
- A final line: `pool reviews: N of 20 for the first reading` until 20, then `pool is at X% of own-seat tokens per review`.
- A record whose rollout is missing, or whose time window contains a compaction, is `cost incomplete`: left out of the medians and means, counted and named in one line, never shown as free and never dropped silently.

**Files:**
- `scripts/delegate/review_pool_report.py` — new
- `scripts/delegate/test_review_pool_report.py` — new, with small rollout fixtures

**Seats:** 1 writer + 1 tester — code and tests split by file.
- `impl` — `scripts/delegate/review_pool_report.py`
- `test` — `scripts/delegate/test_review_pool_report.py`: from the Spec, fixture records and rollouts giving known sums per arm and kind, the time-window bounds, two rollout files for one thread, an unrelated thread whose turns overlap the window, a missing rollout, a window containing a compaction, a `mixed` phase, the 20-review line on both sides of 20

**Constraints from prior phases:** The turn record's fields and directory are in Reviews go to the pool. Weights: uncached input 1, cached input 0.1, output 8. A compaction writes zero input and output counters to the rollout, so its cost cannot be read. The ledger appends `finding_opened` events (`phase_instance_id`, `phase_id`, `caught_by`, `lens`, `round`) to the run's history file; `scripts/delegate/findings.py` `_append_event` shows the fields.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_pool_report.py'` green; `basedpyright` on both files at 0 errors, 0 warnings; one run against the real root prints a table without a traceback.

### Phase 5 — The review unit director and review memory  · status: todo

#### Work Order

**Goal:** A production with a review pool runs one review unit director that keeps the pools primed and builds their review memory from closed findings; it is never on the path of a review.

**Spec:**
- `commands/review/direct.md` (new, `/review:direct <production doc>`): at start, build each type's standing prompt (the base file under `config/review_pool/` plus the memory's repository section) and run `review_pool.py prime`; on each merge line from the showrunner, read that phase's findings from the run's history file, `<history root>/runs/<session dir name>.jsonl` (the `finding_opened` and verdict events whose `phase_id` is the merged phase; by then the unit has usually moved on, and `findings.py status` shows only its active phase), with the design-check verdict, and record in memory a class seen twice or more, a ruling a unit director or the showrunner made, and a defect that got past review; after each change to `memory/repository.md`, rebuild that type's standing prompt and run `review_pool.py prime` again (a changed prompt is a new hash and a new template; a leased reviewer finishes its lease on the old prompt and retires at `end-lease`); each hour run `review_pool.py health`; at each dailies give `review_pool_report.py`'s tables. It dispatches no review and answers no allocation.
- Memory: `~/.local/state/review-pool/<slug>/memory/repository.md` (at most 4k tokens for code, 3k for ui) and `memory/<unit>.md`. Each entry carries its source and date. At the cap, prune the least-cited entry. Only this director writes memory (proposal, section 2).
- `review_pool.py memory-version --production S` prints the repository file's entry count; `review.sh` stores it in the turn record as `memory_version`.
- The unit's memory section is sent at lease start: `review.sh --pool` prepends `memory/<unit>.md` to the prompt when `lease_ordinal` is 1.
- `commands/showrunner/produce.md`: <LaunchUnits/> starts the review unit first when the doc has a `**Review pool:**` line; <MergeCheckpoint/> sends it one line per merged checkpoint (unit, phase, session dir, design-check verdict); <Wrap/> runs `review_pool.py stop`.
- `scripts/production/add_unit.py`: accept a `review-unit` row with no worktree and no port, whose session runs `/review:direct`.
- `docs/production_format.md`: Words gain **Review unit**; the Units table may carry the `review-unit` row with Owns `—`.

**Files:**
- `commands/review/direct.md` — new
- `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py` — `memory-version`
- `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py` — `memory_version` and the unit memory section
- `commands/showrunner/produce.md`, `docs/production_format.md` — the text above
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py` — the `review-unit` row

**Seats:** 2 writers — the scripts and their tests; the command text.
- `impl` — `scripts/delegate/review_pool.py`, `scripts/delegate/test_review_pool.py`, `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py`, `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`
- `test` — opens as impl: `commands/review/direct.md`, `commands/showrunner/produce.md`, `docs/production_format.md`

**Constraints from prior phases:** `review_pool.py prime`, `health`, `stop` and `status` exist. The standing prompt base is `config/review_pool/code.md`. The report is `scripts/delegate/review_pool_report.py`.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_*.py'` and `-s scripts/production -p 'test_add_unit.py'` green; `basedpyright` on the edited Python files at 0 errors, 0 warnings; `bash -n scripts/delegate/review.sh`.

### Phase 6 — The UI pool, in shadow first  · status: todo

#### Work Order

**Goal:** A long-lived Codex UI reviewer judges the same shots as today's design-check helper, each verdict from a freshly reset thread; its verdicts are recorded and not used until ten checks show it misses nothing.

**Spec:**
- `config/review_pool/ui.md`, at most 6 KB: the role line, the Three Gods sentence, the UX checklist from `zsh ~/.claude/scripts/ux_style/load-ux-style.sh --checklist` with each rule file's path, and `commands/ux_eval.md`'s report format.
- `review.sh --pool ui --lease …` with lens `ux`: `allocate --type ui --fresh`, so the reviewer is reset before the turn, no earlier shot is in its context, and it keeps its lease; a failed allocation is an `error` status and no turn runs. Shots go as `--image` arguments read from `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`, one path per line.
- `commands/unit/direct.md` <UXReview/> step 3: with `ui` in the production's `**Review pool:**` line, also launch the pool review on the same shots. Both verdict tables are saved as a pair beside the turn records, in `<history root>/review_turns/<session dir name>/`: `ux_<phase>_<k>_helper.txt` and `ux_<phase>_<k>_pool.txt`, where `k` counts the phase's design checks from 1, a recheck after a repair included. The Claude helper's verdict is the one used in steps 3, 5 and 6, unless the file `~/.local/state/review-pool/<slug>/ui_adopted` exists; then the pool's verdict is used and no helper runs. No code creates that file: a missing file, a new pool and a new production all mean shadow.
- `review_pool_report.py --shadow`: per design check (each `_helper` and `_pool` pair), the helper's defect rows, the pool's, and the rows the pool missed; a check with one file of its pair missing is named.
- Leaving shadow is the user's decision, on the report after ten checks (proposal, section 7); `ui_adopted` is then created by hand.
- Tests: `--pool ui` passes `--fresh` and one `--image` per listed shot; a failed allocation runs no turn; `--shadow` finds the missed rows in fixture pairs, a recheck pair included, and names an unpaired file.

**Files:**
- `config/review_pool/ui.md` — new
- `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py` — the `ui` path
- `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py` — `--shadow`
- `commands/unit/direct.md` — <UXReview/>

**Seats:** 2 writers — the launcher with its tests; the report with its tests and the text.
- `impl` — `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py`
- `test` — opens as impl: `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py`, `config/review_pool/ui.md`, `commands/unit/direct.md`

**Constraints from prior phases:** `follow --image` exists. `allocate --fresh` is in The pool script. The turn-record directory is in Reviews go to the pool. The review unit director primes every type listed on the `**Review pool:**` line from `config/review_pool/<type>.md`.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_*.py'` green; `basedpyright` on the edited Python files at 0 errors, 0 warnings; `bash -n scripts/delegate/review.sh`; one live shadow check on a stored Hana shot returns `ux_eval`'s table.
