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

#### Work Order

**Goal:** `codex_mesh.py` can create a template thread, fork it, reset a fork to the template's state, and run a read-only turn in any worktree; a live probe records what each costs.

**Spec:**
- `prime --session-dir D --name N --instructions-file F --cwd C [--model M] [--service-tier T]`: `thread/start` with `developerInstructions` = the file's text, `sandbox: "read-only"`, `approvalPolicy: "never"`; then `thread/name/set`; then a roster entry `{thread_id, status: "done", role: "review"}`. It starts no turn. Prints the thread id.
- `fork --session-dir D --from N --name M`: `thread/resume` the source, `thread/fork {threadId, sandbox: "read-only", approvalPolicy: "never"}`, `thread/name/set`, roster entry as above for `M`. Refuses a name already in the roster with a thread that is not `ended` (exit 2). Prints the new thread id.
- `reset --session-dir D --to N`: refuses unless the roster state is followable and the thread reads idle (exit 2). `thread/resume`, read the thread's first turn id with `thread/turns/list`, and when one exists call `thread/revert {threadId, beforeTurnId}`. Sets the roster's `context_tokens` to 0. A thread with no turn is a success with no call.
- `follow` gains `--cwd <dir>`, `--read-only` and `--image <path>` (repeatable). `_start_turn` passes `cwd`, `sandboxPolicy: {"type": "readOnly", "networkAccess": false}`, and one `{"type": "localImage", "path": …}` input per image after the text. Without the flags its request is unchanged.
- Every new verb uses `ensure_server`, `Client` and `_require` as `follow` does, closes its client in `finally`, and prints failures as `codex_mesh: <name>: <reason>` with exit 1.
- **Live probe**, run once by the `impl` seat against a scratch session dir under `/tmp/claude/delegate/`, then `stop`. Record each answer with its measured numbers in the probes doc:
  1. First request after `reset` and first request after `fork`: `cached_input_tokens` and uncached input of each, read from the thread's rollout. State which is cheaper for lease end.
  2. Whether `developerInstructions` are still obeyed after `compact`.
  3. Whether per-turn `--cwd` and `--read-only` hold: the turn reads a second worktree and a write attempt fails.
  4. Whether two `follow` calls on two threads of one server run at once.
  5. Whether `fork` works when a new server has not loaded the template, and whether a 7 KB instructions file is accepted.

**Files:**
- `scripts/agents/codex_mesh.py` — the three verbs in one new block before `main`; `_start_turn` and the `follow` parser
- `scripts/agents/test_codex_mesh.py` — tests for the verbs and flags
- `docs/plans/build-followups-review-pool-probes.md` — new; the five answers

**Seats:** 1 writer + 1 tester — code and tests split by file.
- `impl` — `scripts/agents/codex_mesh.py`, `docs/plans/build-followups-review-pool-probes.md`
- `test` — `scripts/agents/test_codex_mesh.py`: from the Spec, each verb's requests in order, its roster entry, its refusals, and `follow`'s request with and without the new flags

**Constraints from prior phases:** none.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/agents -p 'test_codex_mesh.py'` green; `basedpyright scripts/agents/codex_mesh.py scripts/agents/test_codex_mesh.py` at 0 errors, 0 warnings; the probes doc answers all five with numbers; no scratch app-server left running.

### Phase 2 — The pool script  · status: todo

#### Work Order

**Goal:** `review_pool.py` gives any review a reviewer in one call, keeps a phase's reviews on the same reviewers, and clears them when the phase ends.

**Spec:**
- State in `~/.local/state/review-pool/<slug>/` (override the root with `REVIEW_POOL_STATE` for tests): `pool.json`, `pool.lock` (one `fcntl.flock` around every read-modify-write), `mesh/` (the mesh session dir the reviewers live in), `prime/` (standing prompt files, named by hash).
- `pool.json`: `{"split": "hash"|"all"|"off", "templates": {type: {"name", "thread_id", "prime_hash"}}, "reviewers": {name: {"type", "thread_id", "prime_hash", "lease": {"unit", "phase", "ordinal"} | null, "busy_pid": int | null, "served": [[pass, lens]], "reviews": int}}}`. Reviewer names are `<type><n>`: `code1`, `ui2`. Types are `code` and `ui`.
- `prime --production S --type T --instructions-file F`: copy `F` to `prime/<sha256>.md`; when the hash differs from the template's, call `codex_mesh.py prime` under the name `<type>-template-<hash8>` and record it. Prints the hash.
- `arm --production S --unit U --phase P`: prints `pool` or `own`. `split` `all` gives `pool`; `off`, a missing `pool.json`, or no primed `code` template gives `own`; `hash` gives `pool` when the first byte of `sha256("U\nP")` is even.
- `split --production S --set hash|all|off`: stores it.
- `allocate --production S --type T --unit U --phase P --pass N [--lens L] --pid PID`: under the lock, first match wins:
  1. an idle reviewer of the type whose lease is `(U, P)` and whose `served` has no entry with pass `N` and a different lens;
  2. an idle reviewer of the type with no lease; it takes the lease with `ordinal` 0;
  3. a new reviewer: `codex_mesh.py fork` from the type's template, next free number.

  A reviewer is idle when `busy_pid` is null or names a dead process. A reviewer whose `prime_hash` differs from the template's is skipped by rule 2. The chosen reviewer gets `busy_pid`, `[N, L]` appended to `served`, and `ordinal` raised by 1. Prints one JSON line: `{"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"}`. No rule waits. Exit 1 with a reason only when the type has no template or the fork fails.
- `release --production S --reviewer R --pid PID`: clears `busy_pid` when it is `PID`, adds 1 to `reviews`; the lease stays.
- `end-lease --production S --unit U [--phase P]`: for each reviewer leased to the unit (and phase, when given) that is idle, call `codex_mesh.py reset`, then clear `lease` and `served`. A reviewer whose `prime_hash` is old is marked ended in the mesh roster and removed. A busy reviewer is left and named on stderr. Exit 0 either way.
- `status --production S`: one line per reviewer: name, type, lease, busy or idle, reviews.
- `health --production S [--stale-hours 12]`: ends leases whose reviewers have had no `allocate` for that long (keep a `last_allocated` stamp), and prints what it ended.
- `stop --production S`: `codex_mesh.py stop` on `mesh/`.
- Mesh calls are subprocess calls to `scripts/agents/codex_mesh.py`, made outside the lock except `fork` in rule 3, which holds it so two callers never fork the same name.

**Files:**
- `scripts/delegate/review_pool.py` — new
- `scripts/delegate/test_review_pool.py` — new
- `pyrightconfig.json` — one entry if the file lists checked paths

**Seats:** 1 writer + 1 tester — code and tests split by file.
- `impl` — `scripts/delegate/review_pool.py`; hub: `pyrightconfig.json`
- `test` — `scripts/delegate/test_review_pool.py`: from the Spec, with `REVIEW_POOL_STATE` in a temp dir and a stub `codex_mesh.py` on a path override (`REVIEW_POOL_MESH`): the three allocation rules in order, the other-lens exclusion, dead-pid idleness, two concurrent `allocate` calls getting two reviewers, `arm` for each `split`, `end-lease` on idle and busy reviewers, old-hash retirement

**Constraints from prior phases:** Mesh verbs `prime`, `fork`, `reset` exist with the arguments in Mesh verbs for pooled threads, proved live. If its probes doc found `fork` cheaper than `reset` for the first request, `end-lease` ends the reviewer and removes it in place of `reset`; the doc's line 1 decides.

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
- **Turn record**, written for `--pool` and for `--to` alike at the end of the pass: `${SESSION_DIR}/review_turn_${PASS_INDEX}${SUFFIX}.json` = `{"arm": "pool"|"own", "pool", "reviewer", "thread_id", "unit", "phase", "kind": "broad"|"closure"|"ux", "lens", "pass", "started_at", "finished_at", "lease_ordinal", "prime_hash", "status"}`. `kind` is `broad` for pass 1 with a lens, `ux` for lens `ux`, else `closure`. For `--to`, `thread_id` comes from the unit's mesh roster, and `unit` and `phase` from `REVIEW_LEASE` when set, else empty.
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

**Constraints from prior phases:** `review_pool.py` verbs and their output are in The pool script. `follow` takes `--cwd`, `--read-only` and a session dir any number of launchers share. Recorder slot names allow letters, digits and `_` only, hence `pool_<reviewer>`.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_launcher.py'` green; `bash -n scripts/delegate/review.sh`; a live check in this worktree: prime the code template from `config/review_pool/code.md` under a scratch slug, run one `--pool code` review of a two-line diff, and see findings, a `reviewed` status, a turn record and an idle reviewer with its lease; then `end-lease` and `stop`. After this merges to main, the showrunner asks Hana's showrunner to add `**Review pool:** code` and prime the template: the trial starts there.

### Phase 4 — The trial report  · status: todo

#### Work Order

**Goal:** One command prints weighted tokens per review for the pool arm and the own-seat arm, and the counts that show whether reviews improve.

**Spec:**
- `review_pool_report.py --since <date> [--root /tmp/claude/delegate] [--no-mac]`: read every `review_turn_*.json` under the root (and the Mac's, as `seat_usage.py` reads its rollouts), find each record's thread rollout, and sum the token usage between `started_at` and `finished_at`. Import `parse_rollout`, `local_rollouts`, `mac_rollouts` and the weights from `scripts/whoami/seat_usage.py`; do not edit that file.
- Print one table by arm (`pool`, `own`) and kind (`broad`, `closure`, `ux`): reviews, median and mean weighted tokens, share from cached input. For the pool, a second table by lease ordinal (1, 2, 3+).
- `--quality`: per arm, raised (numbered items in the findings file), confirmed (ledger rows opened with that lens in that pass), precision, and escapes per phase (rows opened `--caught-by main` only, and merge holds from `review_regime.py`).
- A final line: `pool reviews: N of 20 for the first reading` until 20, then `pool is at X% of own-seat tokens per review`.
- A record whose rollout is missing is counted and named in one line, never dropped silently.

**Files:**
- `scripts/delegate/review_pool_report.py` — new
- `scripts/delegate/test_review_pool_report.py` — new, with small rollout fixtures

**Seats:** 1 writer + 1 tester — code and tests split by file.
- `impl` — `scripts/delegate/review_pool_report.py`
- `test` — `scripts/delegate/test_review_pool_report.py`: from the Spec, fixture records and rollouts giving known sums per arm and kind, the time-window bounds, a missing rollout, the 20-review line on both sides of 20

**Constraints from prior phases:** The turn record's fields are in Reviews go to the pool. Weights: uncached input 1, cached input 0.1, output 8.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_pool_report.py'` green; `basedpyright` on both files at 0 errors, 0 warnings; one run against the real root prints a table without a traceback.

### Phase 5 — The review unit director and review memory  · status: todo

#### Work Order

**Goal:** A production with a review pool runs one review unit director that keeps the pools primed and builds their review memory from closed findings; it is never on the path of a review.

**Spec:**
- `commands/review/direct.md` (new, `/review:direct <production doc>`): at start, build each type's standing prompt (the base file under `config/review_pool/` plus the memory's repository section) and run `review_pool.py prime`; on each merge line from the showrunner, read that phase's closed findings (`findings.py status` in the named session dir) and the design-check verdict, and record in memory a class seen twice or more, a ruling a unit director or the showrunner made, and a defect that got past review; each hour run `review_pool.py health`; at each dailies give `review_pool_report.py`'s tables. It dispatches no review and answers no allocation.
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
- `review.sh --pool ui --lease …` with lens `ux`: `allocate --type ui`; before the turn, `review_pool.py end-lease` semantics apply to the chosen reviewer (reset first, so no earlier shot is in context); shots go as `--image` arguments read from `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`, one path per line.
- `commands/unit/direct.md` <UXReview/> step 3: with `ui` in the production's `**Review pool:**` line and the file `~/.local/state/review-pool/<slug>/ui_shadow` present, also launch the pool review on the same shots, save its verdict as `ux_shadow_${PASS_INDEX}.txt`, and use only the Claude helper's verdict. Without the shadow file, the pool verdict replaces the helper's in steps 3, 5 and 6.
- `review_pool_report.py --shadow`: per design check, the helper's defect rows, the pool's, and the rows the pool missed.
- Leaving shadow is the user's decision, on the report after ten checks (proposal, section 7).

**Files:**
- `config/review_pool/ui.md` — new
- `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py` — the `ui` path
- `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py` — `--shadow`
- `commands/unit/direct.md` — <UXReview/>

**Seats:** 2 writers — the launcher with its tests; the report with its tests and the text.
- `impl` — `scripts/delegate/review.sh`, `scripts/delegate/test_review_launcher.py`
- `test` — opens as impl: `scripts/delegate/review_pool_report.py`, `scripts/delegate/test_review_pool_report.py`, `config/review_pool/ui.md`, `commands/unit/direct.md`

**Constraints from prior phases:** `follow --image` exists. The review unit director primes every type listed on the `**Review pool:**` line from `config/review_pool/<type>.md`.

**Acceptance gate:** `scripts/lib/py -m unittest discover -s scripts/delegate -p 'test_review_*.py'` green; `basedpyright` on the edited Python files at 0 errors, 0 warnings; `bash -n scripts/delegate/review.sh`; one live shadow check on a stored Hana shot returns `ux_eval`'s table.
