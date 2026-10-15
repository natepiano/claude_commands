# Review pool

## What it is

Each production has its own pool of long-lived, read-only Codex reviewers. A unit that needs a review calls a script, which hands it a reviewer at once. A reviewer keeps its context for one unit's phase (a lease) and is reset when the phase ends, so the next lease starts from a warm prompt cache instead of a new thread. A trial compares weighted tokens per review on the pool against the same reviews on a unit's own workers, with phases divided between the two arms. A second reviewer type judges screenshots; it runs in shadow beside the Claude design-check helper until the user adopts it.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/delegate/review_pool.py` | The pool CLI: templates, reviewers, leases, the arm split, unit memory lookup, stop |
| `scripts/delegate/review.sh` | The review launcher; its `--pool` path allocates, runs and records one review turn |
| `scripts/delegate/review_pool_report.py` | The trial report: cost tables, `--quality`, `--shadow` |
| `scripts/agents/codex_mesh.py` | The Codex mesh; `prime`, `fork`, `reset` and three `follow` flags exist for the pool |
| `scripts/production/add_unit.py` | Opens `/review:direct` for the review unit |
| `config/review_pool/code.md` | Standing instructions of the code reviewer |
| `config/review_pool/ui.md` | Standing instructions of the UI reviewer |
| `commands/review/direct.md` | `/review:direct`, the review unit director's command |
| `commands/unit/direct.md` | The unit director's side: pool form of each review launch, <UXReview/> |
| `docs/delegate/dual_review.md` | Pool prompt opening, pool launch call, cancel through the slot file |
| `docs/delegate/phase_end.md` | `end-lease` in the worker cleanup |
| `docs/production_format.md` | Words, the `**Review pool:**` line, the `review-unit` row, <ProductionUnit/> items 3 and 16 |
| `commands/showrunner/produce.md` | Launch of the review unit, the merge line, dailies tables, `stop` at wrap |

### State

A pool belongs to one production. Its state is `~/.local/state/review-pool/<slug>/`; `REVIEW_POOL_STATE` replaces the root. `<slug>` is the production doc's file name without `-production.md`, and `--production` accepts the slug or the doc's path.

| Path | Holds |
| --- | --- |
| `pool.json` | Templates, reviewers, leases, the split, the generation. Written through a temporary file, `fsync` (skipped when `PLAN_DELEGATE_SYNC_WRITES=0`, for test fixtures) and `os.replace` |
| `pool.lock` | One `fcntl.flock` around every read-modify-write of `pool.json` |
| `prime/<sha256>.md` | Copies of standing prompts, named by content hash |
| `cwd/` | The templates' working directory, empty |
| `mesh/<generation>/` | One mesh session directory per pool lifetime |
| `standing/<type>.md` | The standing prompt the review director builds and primes from |
| `memory/repository.md`, `memory/<unit>.md` | Review memory |
| `ui_adopted` | The UI pool's adoption marker; present only after the user creates it |

`pool.json`:

```
{"split": "hash"|"all"|"off", "generation": int, "stopping": true,
 "next_index": {"code": n, "ui": n},
 "templates": {type: {"name", "thread_id", "prime_hash"}},
 "reviewers": {name: {"type", "thread_id", "prime_hash",
                      "lease": {"unit", "phase", "ordinal"} | null,
                      "busy_pid": int | null, "served": [[pass, lens]],
                      "reviews": int, "last_allocated"}}}
```

A new file starts with `split: "hash"` and `generation: 0`. `stopping` is present only while a stop is unfinished. A file without `next_index` reads each counter as 1. Types are `code` and `ui`; reviewer names are `<type><n>` (`code1`, `ui2`); a template is named `<type>-template-<hash8>`. In code a lease is `Unleased` or `PhaseLease`, and a reviewer's ownership is `IdleReviewer` or `ClaimedReviewer`; `null` appears only where `pool.json` is read and written.

`<history root>` is `PLAN_DELEGATE_HISTORY_DIR`, or `~/.local/state/plan-delegate`. Review turn records and UI verdict pairs live in its `review_turns/<session dir name>/`; the review director's own recorder state is its `review_units/review-<slug>`.

### `review_pool.py` verbs

Every verb takes `--production <slug>`. A failure prints `review_pool: <reason>` on stderr and exits 1. Every mesh call runs outside the lock: a verb reads `generation` before the call and publishes the result only when it is unchanged; otherwise it drops the result and exits 1 with `pool was stopped during the mesh call; result dropped`. `REVIEW_POOL_MESH` names another script to run in place of `codex_mesh.py`.

- **`prime --type T --instructions-file F`** copies `F` to `prime/<sha256>.md` and prints the hash. When the hash differs from the type's template, it calls `codex_mesh.py prime` as `<type>-template-<hash8>` with that copy's absolute path and `cwd/`, and records the template. An unchanged hash makes no mesh call. A changed prime retires idle unleased reviewers of the old hash at once; a leased one keeps its prompt and retires at lease end. A `prime/` file is deleted once no template or reviewer carries its hash, and not before.
- **`arm --unit U --phase P`** prints `pool` or `own` (see Arm split).
- **`split --set hash|all|off`** stores the split.
- **`allocate --type T --unit U --phase P --pass N [--lens L] --pid PID [--fresh]`** takes the first match under the lock:
  1. an idle reviewer of the type leased to `(U, P)` whose `served` has no entry with pass `N` and a different lens;
  2. an idle reviewer of the type with no lease and the template's current hash; it takes the lease;
  3. a new reviewer: a name reserved under the lock with `busy_pid` and no thread id, `codex_mesh.py fork` from the template outside the lock, the thread id recorded under the lock.

  A reviewer is idle when `busy_pid` is null or names a dead process; a reservation with no thread id is never a candidate. The chosen reviewer gets `busy_pid`, `[N, L]` appended to `served`, `ordinal` raised by 1 and `last_allocated` set. The verb prints one JSON line `{"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"}`, where `mesh_dir` is the current `mesh/<generation>/`. It exits 1 with `no primed <type> template` or `fork <name> failed: <reason>`; a failed fork removes its reservation. `--fresh` resets a reviewer chosen by rule 1 or 2 before the line prints and keeps its lease; it does not reset a reviewer it has just forked; a failed reset retires the reviewer and the rules run again.
- **`release --reviewer R --pid PID [--thread-id ID]`** clears `busy_pid` when it is `PID` and adds 1 to `reviews`; the lease stays. With `--thread-id` it stores a changed thread id, under the lock and only for that owner.
- **`end-lease --unit U [--phase P]`** handles each leased reviewer of the unit (every phase when `--phase` is absent). An idle one is claimed, reset with `codex_mesh.py reset` outside the lock, then cleared of `lease`, `served` and `busy_pid`. One whose reset exits nonzero, or whose `prime_hash` is no longer the template's, is retired: `codex_mesh.py end`, removal from `pool.json`, and `review_pool: retired <name>: <reason>` on stderr (`old prime hash` or `reset failed: <mesh reason>`, with `; end failed: <reason>` appended when the mesh `end` fails). A busy one is left: `review_pool: <name> is busy; lease left intact`. A reservation whose owner is dead is removed: `review_pool: removed <name>: reservation owner is dead`. None of these three changes the exit code from 0.
- **`health [--stale-hours 12]`** does the same for every lease whose reviewer has had no `allocate` for that long, and prints each name it ended on stdout.
- **`status`** prints one tab-separated line per reviewer: name, type, lease (`unleased` or `<unit>:<phase>:<ordinal>`), `idle` or `busy`, reviews.
- **`unit-memory --unit U`** prints `memory/<unit>.md` byte for byte, or nothing when it is absent. A unit name with a directory component exits 1 with `unit must name one memory file`.
- **`stop`** removes `ui_adopted` when present (printing `review_pool: ui adoption cleared` on stderr), raises `generation`, empties `templates` and `reviewers`, sets each name counter back to 1, keeps `split`, and runs `codex_mesh.py stop` on the ended lifetime's directory. When that call fails, `stop` exits 1 with the mesh's reason and leaves `stopping` set: `prime`, `allocate`, `end-lease` and `health` then exit 1 with `pool stop did not finish; run stop again` until a `stop` succeeds, and a repeated `stop` runs against the same ended directory.

### Mesh verbs the pool relies on

- **`prime --session-dir D --name N --instructions-file F --cwd C [--model M] [--service-tier T]`** sends `thread/start` with `developerInstructions` set to the file's text, `sandbox: "read-only"` and `approvalPolicy: "never"`, then `thread/name/set`, then writes the roster entry `{thread_id, status: "done", role: "review", context_tokens: 0, instructions_file}` with the file's absolute path. It starts no turn and prints the thread id.
- **`fork --session-dir D --from N --name M`** resumes the source, sends `thread/fork` with the same sandbox and approval policy, names the thread, and writes the same roster entry for `M`, copying the source's `instructions_file`. It exits 2 when `M` already names a thread that is not `ended`, or when the name was taken while the fork ran. It prints the new thread id.
- **`reset --session-dir D --to N`** takes the roster claim `follow` takes (`starting` with its own pid, released on every exit), resumes the thread, requires it to read `idle`, and calls `thread/revert {threadId, beforeTurnId}` with the first turn id from `thread/turns/list`; a thread with no turn succeeds with no revert. It sets `context_tokens` to 0 only while it still owns the claim. Exit 0 done, 1 request failed, 2 refused (busy, claimed, not idle, or not followable).
- **`follow`** takes `--cwd <dir>` (that turn's working directory), `--read-only` (`sandboxPolicy: {"type": "readOnly", "networkAccess": false}` for that turn) and `--image <path>` (repeatable; one `{"type": "localImage", "path": …}` input per image after the text). Without the flags its request is unchanged.
- **`_resume_params`** reads the roster's `instructions_file` and sends its text as `developerInstructions` on every `thread/resume` and on `thread/fork`, because the app-server drops standing instructions on both. When that file is missing, `follow`, `fork`, `reset`, `compact` and `send` exit 1. When `follow` has to replace a thread whose resume fails, the replacement gets the same instructions and read-only policy, and its id goes to the roster.
- The pool also uses `end --to` (retire a reviewer) and `stop` (end a pool lifetime); the launcher uses `compact`, `can-follow --claim-pid` and `release-follow`.

`prime`, `fork` and `reset` print a failure as `codex_mesh: <name>: <reason>`.

### A pooled review turn

```
review.sh --pool code|ui --lease <slug>/<unit>/<phase> \
  <session_dir> [working_dir] [prompt_file] [task] [role_description] \
  [pass_activity] [pass_index] [lens] [early_ready_file]
```

The two options go before the positional arguments, which are the same as on every other path. These refusals exit 2 before any artifact, allocation or turn record:

- `ERROR: --pool excludes --to and --slot.`
- `ERROR: --pool requires code or ui.`
- `ERROR: --pool requires --lease slug/unit/phase.`
- `ERROR: --lease requires --pool; use REVIEW_LEASE for an own review.`
- `ERROR: --pool ui requires lens ux.`
- `ERROR: --pool code does not accept lens ux.`

Lens `ux` with no pool is accepted. From there the launcher:

1. Removes `${SESSION_DIR}/review_slot<suffix>` (the suffix is `_<lens>` with a lens and empty without one), reads its start time, and installs the exit handler that writes the turn record.
2. Resolves the review agent and requires the Codex family with the mesh; otherwise `ERROR: --pool requires the Codex mesh.`, exit 2.
3. For `ui`, reads `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`, one path per line, into `--image` arguments. An unreadable file (`ERROR: UI shots file is unreadable: <path>.`) or one with no paths (`ERROR: UI shots file has no paths: <path>.`) exits 1 before allocation.
4. Runs `review_pool.py allocate --production <slug> --type <type> --unit <unit> --phase <phase> --pass <pass_index> [--lens L] [--fresh] --pid $$`; `--fresh` is passed for `ui` only. A failure prints `ERROR: pool allocation failed; see <log>.` and exits 1 with no turn.
5. Takes `reviewer`, `mesh_dir`, `thread_id`, `lease_ordinal` and `prime_hash` from the allocation line. Its slot is `pool_<reviewer>`: exported as `PLAN_DELEGATE_TEAM_ROLE` and written as one line to the slot file, since the slot is known only after allocation and the unit director needs it to cancel.
6. Prepends the unit's memory (`review_pool.py unit-memory`) for `code` when `lease_ordinal` is 1, and on every `ui` turn, which starts from a reset thread that holds nothing from an earlier turn. It writes `${SESSION_DIR}/review_message_<pass><suffix>.XXXXXX` (the memory, a blank line, the prompt), sends that file as the prompt and removes it at exit. With no memory the prompt goes unchanged. A failed `unit-memory` call ends the attempt as an `error`.
7. Claims the reviewer in the pool's `mesh_dir` (`can-follow --claim-pid $$`), compacting it first when its recorded context is an integer over `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` (100000 in `config/delegate.conf`). Roster, compaction, claim and claim release use the pool's `mesh_dir`; status file, findings, agent log, heartbeat, board and pass stay in the unit's `${SESSION_DIR}`.
8. Records the review pass under its slot, except for lens `ux` (`record_pass_start` returns at once, with a pool and without one): the unit director's `UX review` activity already carries that time, and a pass would count it twice. An early launch defers the pass until its sentinel file appears, as on the assigned path; it checks for the file every `PLAN_DELEGATE_EARLY_POLL_SECS` (default 5 s; tests set it small).
9. Runs `codex_mesh.py follow --session-dir <mesh_dir> --to <reviewer> --claim-pid $$ --role review [--lens L] --cwd "${WORKING_DIR}" --read-only [--image …] --message-file … --summary-file <findings> --reply-file … --log-file … --model … --effort … --service-tier …` with the review model, effort and tier.
10. After the turn, checks that the working tree is unchanged, counts the findings, writes `reviewed` or `error` to the status file and closes the pass (`finish-pass --status completed` or `error`).
11. On every exit: writes `error` to the status file when the exit code is not 0, reads the reviewer's thread id from the mesh roster, writes the turn record, releases the mesh claim, runs `review_pool.py release --production <slug> --reviewer R --pid $$ --thread-id <id>` and removes the temporary prompt file. A record that cannot be written prints `ERROR: unable to write review turn record.` and makes the exit 1.

A pool launcher stopped by TERM, INT or HUP stops its child and its heartbeat watcher, marks the record `canceled` and exits 143, 130 or 129. It leaves its pass open. The unit director closes that pass with `finish-pass --status canceled --orphaned-launcher` under the slot it reads from the slot file (`review_slot_adversary` for a pass-1 early review, `review_slot_<lens>` for a lens, `review_slot` for a closure); a missing file means no pass to close. An own-arm launcher marks its record `canceled` on the same signals and exits with the same codes.

A pool `error` falls back to `review.sh --to <worker>` for that review under <DelegateLaunchFailure/>, with `REVIEW_LEASE=<slug>/<unit>/<phase>` exported.

### Turn record

A `--pool` launch and a `--to` launch each write one new file per attempt, including an attempt that fails after the argument checks above: `<history root>/review_turns/<session dir name>/<started epoch>-<pid>.json`, created exclusively and never overwritten. `--slot` and fresh launches write none. `clear_phase_review.sh` never reaches this directory.

| Field | Value |
| --- | --- |
| `arm` | `pool` or `own` |
| `pool` | The production slug on the pool arm, empty on the own arm |
| `reviewer` | The reviewer or worker name |
| `thread_id` | The roster's id after the turn, so it names the thread that ran it |
| `unit`, `phase` | From `--lease`, or from `REVIEW_LEASE` on the own arm |
| `phase_instance_id`, `history_file` | From the unit's progress recorder state |
| `kind` | `ux` for lens `ux`, otherwise `broad` for pass 1 with a lens, otherwise `closure` |
| `lens`, `pass` | The lens; the pass index as an integer |
| `started_at`, `finished_at` | Whole epoch seconds; fractional epoch seconds |
| `lease_ordinal` | 0 on the own arm |
| `prime_hash` | Empty on the own arm |
| `raised` | Numbered items in the findings file. For lens `ux`: data rows of the table headed `\| Shot \| Rule \| Defect \| Fix \|`, with or without a closing bar, never a header, a separator, numbered prose or a `Canonical candidates` row |
| `status` | `reviewed` after a successful turn; `void` for an early-launched review that finished before its final diff was delivered; `canceled` for TERM, INT or HUP; `error` otherwise |

`status` is held in the shell variable `TURN_RECORD_STATUS` and never derived from the exit code: a bash `EXIT` trap sees exit code 0 when the shell dies of TERM with no TERM trap, which is also why both arms trap the three signals. `raised` is kept for every status and is 0 for an attempt that ended before its findings file was opened.

### Arm split

`**Review pool:** code` (or `code, ui`) in a production doc's Production Context switches the pool on for that production, once `/review:direct` has primed each type. At each phase's start a unit runs `review_pool.py arm --production <slug> --unit <unit> --phase <phase>`:

- `all` gives `pool`; `off`, a missing `pool.json` or no primed `code` template gives `own`;
- `hash` gives `pool` when the first byte of `sha256("<unit>\n<phase>")` is even.

On `pool` the unit sends that phase's code reviews with `--pool code --lease <slug>/<unit>/<phase>`. On `own` it exports `REVIEW_LEASE=<slug>/<unit>/<phase>` and reviews on its own workers with `--to`, so both arms leave records the report can compare. The UI pool is used on every design check regardless of the phase's code-review arm. At phase end the worker cleanup runs `review_pool.py end-lease --production <slug> --unit <unit> --phase <phase>`.

### Prompts

A reviewer's standing instructions come from its template and reach every turn of its thread. `config/review_pool/code.md` holds the role line, the Three Gods sentence, `type_design.md` verbatim and the Rust style guide's rule headings with each rule's file path. `config/review_pool/ui.md` holds the UI reviewer's whole procedure: the role line, the Three Gods sentence, the UX checklist with each rule file's name, the judging steps (shrink each shot, check that the shots cover every state the context names, propose canonical candidates only for empty slots, measure contrast and text size with `measure_text.py`, normalise sizes to a 1440×900 window, confirm each defect against the rule's included text) and the report format (`Verdict: pass | defects`, the `| Shot | Rule | Defect | Fix |` table, canonical candidates). It tells the reviewer not to load command or rule files; the rule texts come with the standing prompt (Review unit director and review memory). Neither file holds review memory.

On every design check the unit director writes two prompts (<UXReview/> step 3): `${SESSION_DIR}/ux_prompt_<pass>_helper.md`, the Claude helper's prompt that points at `commands/ux_eval.md`, and `ux_prompt_<pass>_pool.md`, which opens "Judge these shots by your standing instructions, which hold the rules and the report format:" and carries the shots, scale, context and production rules. The pool is launched with the pool file, so a pooled UI turn reads no rule or command file; it may still read `measure_text.py`'s source for its flags.

On the pool arm a code review prompt opens with the pooled-reviewer line (unit, phase, and `git -C <worktree> rev-parse HEAD` as the first command, reporting an error when it is not the expected head), and its Three Gods and Type Design Contract sections each hold `As in your standing instructions.` <TeamReview/>'s worker choice and role reassignment do not apply.

### Review unit director and review memory

`/review:direct <production doc>` dispatches no review and answers no allocation. `add_unit.py` opens it, in place of `/unit:direct`, for the unit named `review-unit` when the doc has a `- **Review pool:** ` line. The Units table carries the review unit as an ordinary row (worktree, branch, plan, Port `—`, Owns `—`); its plan has one standing phase that stays `todo`, and `/review:direct` writes that plan when it is absent.

At start and on resume the director:

- records one standing phase (`progress_history.py start-run`, then `start-phase --phase-id 1`) in `<history root>/review_units/review-<slug>`; both calls reuse an existing run;
- builds each type's standing prompt into `<pool dir>/standing/<type>.md` and runs `review_pool.py prime` on it (below);
- registers an hourly health message to itself (`notifier.sh new review-health-<slug> --every 60`).

`standing/code.md` is `config/review_pool/code.md` plus the `code` section of `memory/repository.md`. `standing/ui.md` is, in order:

1. `config/review_pool/ui.md`;
2. `## Rules`, then `UX guide: <absolute guide path>`, then each `.md` in the guide root with a non-empty frontmatter `test:`, in file-name order, as `### <file name>` and its text; the selection is `scripts/ux_style/load-ux-style.sh`'s, so README and notes are left out;
3. when present, `docs/design-decisions.md` of the repository that holds the production doc, under `## Design decisions`: the file the helper's `ux_eval.md` reads from a unit's worktree;
4. the `ui` section of `memory/repository.md`.

A production doc with no **UX guide** primes no `ui`, and the director tells the showrunner `UI pool not primed: production has no UX guide`. An unreadable guide or rule fails the prime; an incomplete UI prompt is never primed. The prompt is rebuilt at start and on resume, so a changed rule's text changes the prompt hash.

On each showrunner line `review merge: unit <unit>; phase <phase>; session dir <path>; design check: <verdict>` (the showrunner takes the session directory's name from `unit_run.py`, `docs/as-built/showrunner-automation.md`) it reads `<history root>/runs/<session dir name>.jsonl` (local, else the Mac's over ssh at `collect.MAC_HOST`), joins the merged phase's `finding_opened` and `finding_verdict` events by `finding_id` and `phase_instance_id`, and records in memory a defect class seen in two or more findings, a ruling by a unit director or the showrunner, and a defect that got past review. A finding with no verdict is open and is processed with the rest; only a `session dir unavailable` line or an unreadable history file keeps a merge pending, and the director reports the cause to the showrunner.

Memory lives under `<pool dir>/memory/`: `repository.md` with `code` and `ui` sections, and `<unit>.md`. Each entry carries a date and its sources. After a repository section changes, the director primes that type again, which makes a new template. A turn record's `prime_hash` names the memory a reviewer received; no separate version count exists. Unit memory reaches a reviewer through the launcher, not through the standing prompt.

Each hour the director runs `review_pool.py health` and tells the showrunner only when a lease was ended or a command failed. At dailies it runs `review_pool_report.py --since <date> --quality` on natedev, where the date is the day the production doc entered Git (`git log --diff-filter=A --follow --format=%as`), and sends the tables, outcome counts, incomplete costs and source warnings. At shutdown it removes its timer and runs `progress_history.py finish-run --status completed`. An idle director ends its turn with `— holding: waiting for review requests`.

The showrunner launches the review unit first, sends the merge line after each merged code checkpoint (a shrink sends none), asks for the trial tables at dailies, and at wrap tells the review unit to end, waits for its acknowledgment, then runs `review_pool.py stop`, repeating it until it succeeds.

### Trial report

```
review_pool_report.py --since <date> [--root <history root>] [--no-mac] [--quality]
review_pool_report.py --shadow [--root <history root>]
```

`--since` is required unless `--shadow` is given. The report reads every `review_turns/*/*.json` under the root, plus the Mac's records, history files and merge holds over ssh to `collect.MAC_HOST` unless `--no-mac`. A local record that fails validation (a missing field, a `lease_ordinal` or `raised` that is not an integer, an unknown arm, kind or status) is skipped and named on stderr as `review_pool_report: skipped local:<path>: <reason>`. Every record takes part in cost attribution; only records started on or after the date are reported.

**Cost.** Review cost comes from the shared usage store, not from Codex logs: `usage_report.open_read_store(collect.store_path())`, tables `openings`, `folders`, `turns`, `compactions` and `turn_sources`, selected by the records' thread ids. The store names this machine `natedev`; records call it `local`. Each request is weighed per model and time through `weights.weights_at` and `weights.weighted` (`scripts/usage/weights.py`); the report holds no rollout parsing and no weights. A request belongs to at most one record: the earliest-started record of its thread whose window from `started_at` to `finished_at`, both bounds included, contains the request's time. Records of every status own requests, so a `void` or `canceled` turn's tokens never count for a `reviewed` one.

**Output**, in order:

- `Tokens by arm and kind`: `| Arm | Kind | Reviews | Median weighted tokens | Mean weighted tokens | Cached input share |`, one row per arm (`pool`, `own`) and kind (`broad`, `closure`, `ux`).
- `Pool tokens by lease ordinal`: the same cost columns for ordinals `1`, `2` and `3+`.
- `<arm> outcomes: void N; canceled N; error N`, one line per arm.
- `cost incomplete: <n>`, followed by `<machine>:<path> (<reason>)` for each `reviewed` record left out of medians and means.
- `began with a compaction: pool <n>; own <n>`: `reviewed` records whose window holds a compaction before the first request. Their cost stays complete.
- With `--quality`, the quality table.
- `source warning: <text>` lines.
- The final line.

Only a `reviewed` record is a completed review: reviews, medians, means and `raised` sums take those records alone. A cell with no value prints `-`.

A `cost incomplete` reason is the first that applies: `usage store unavailable` (the store is absent or unreadable), `missing rollout` (the store was read and holds no such thread), `no request in its window`, `a compaction after its first request`; a request whose model has no weights gives `unweighted model <model>`.

**Quality.** `--quality` adds `Code review quality by arm`: `| Arm | Phases | Raised | Confirmed | Precision | Escapes per phase |`. It covers code reviews only: records of kind `ux` and findings with lens `ux` are left out, because shadow turns double the raised count for one check and skew the arm comparison. Records are joined by phase instance (machine, `history_file`, `phase_instance_id`); a record with no `phase_instance_id` is a phase of its own. A phase's arm is the arm of its code-review records of every status; a phase with records of both arms is mixed and left out of both rows. Raised sums `raised` over `reviewed` records. Confirmed counts `finding_opened` events in the phase's `history_file` with `caught_by` `delegate` or `both`. Escapes count `caught_by` `main` events plus the `holds` of matching `review_regime.py` ledger rows, from the same machine as the phase.

Merge holds are matched by plan as well as unit and phase, because a unit that runs a second plan starts again at phase 1. `quality_lines` reads the ledger's raw JSON rows through `read_events`, not `review_regime.read_rows()`, so it sees each row's `plan`. Neither the turn record nor `ReviewRecord` carries a plan: `review_plan(record, sources) -> PlanKnown(path) | PlanUnavailable(reason)` takes `plan_doc` and `working_dir` from the first `run_started` event of the record's `history_file`. `normalize_plan(path, working_dir="")` makes an absolute path under `working_dir` relative to it, then applies `os.path.normpath`; a ledger plan is normalized with no `working_dir`, so `docs/plans/alpha.md`, `./docs/plans/alpha.md` and `<working_dir>/docs/plans/alpha.md` are one plan. A row matches by:

- no string `plan`: unit and phase;
- a `plan` and `PlanKnown`: unit, phase and normalized plan;
- a `plan` and `PlanUnavailable`: none; the row's holds are left out and the phase prints one `source warning: merge holds unmatched: <unit> phase <phase>: review plan unavailable (<reason>)`. The reasons are `history file unavailable`, `no run_started event` and `run_started has no plan_doc`.

Mac rows and histories from `mac_sources` take the same path. The table closes with `mixed phases: N (excluded from comparison)`, `ui checks: compared by --shadow, not in this table`, then the `merge holds unmatched` warnings.

**Source warnings:** `usage store unavailable: <reason>`; `Mac usage source <state>; last complete read <time>` (the store's `turn_sources` row for the Mac is not `ready`); `merge holds unavailable: <reason>` (quality continues without holds); `merge holds unmatched: <unit> phase <phase>: review plan unavailable (<reason>)` (printed with the quality table, see Quality); `history unavailable: <path>: <reason>`; `Mac unavailable: <reason>`; `Mac file unavailable: <path>: <reason>`; `invalid Mac file: <reason>`.

**Final line.** `pool reviews: N of 20 for the first reading` until 20, then `pool is at X% of own-worker tokens per review`, a ratio of mean complete costs. N and the comparison count `reviewed` records of kinds `broad` and `closure` only. At 20 with no complete costs on one arm: `pool tokens per review: no comparison yet (complete costs needed for both arms)`.

### UI pool and adoption

With `ui` on the production's `**Review pool:**` line, every design check in <UXReview/> (steps 3, 5 and 6, rechecks and notice checks included) also runs the pooled UI reviewer on the same shots with its own prompt file (Prompts), after the unit director writes the shot paths to `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`:

```
review.sh --pool ui --lease <slug>/<unit>/<phase> "${SESSION_DIR}" <worktree> \
  "${SESSION_DIR}/ux_prompt_${PASS_INDEX}_pool.md" review "UI design check" \
  "judging the shots" "${PASS_INDEX}" ux
```

While the pool is in shadow the Claude helper's prompt and verdict path stay as they are: the helper is the baseline the pool is compared with.

Both verdicts are saved as a pair in `<history root>/review_turns/<session dir name>/`, as `ux_<phase>_<k>_helper.txt` and `ux_<phase>_<k>_pool.txt`; `k` counts the phase's design checks from 1. While `~/.local/state/review-pool/<slug>/ui_adopted` is absent the pool is in shadow: the Claude helper's verdict is the one used, and a failed pool check only leaves its file missing. Once the marker exists, no helper runs, the pool's verdict is used, and a failed pool check holds the design check until a retry succeeds. No code creates the marker: leaving shadow is the user's decision, made by hand after reading the shadow report from ten checks. `review_pool.py stop` removes it, so a pool primed after a stop starts in shadow.

`review_pool_report.py --shadow` reads those pairs under the local root only (pairs saved by a unit on another machine are not counted) and needs neither the usage store nor turn records. It prints:

- `UI shadow checks (all local pairs; --since and --quality do not filter them)`
- `Comparison: rule and shot file name, by count; Defect and Fix wording is not compared.`
- `Machine: <short host name> (pairs saved on another machine are not counted)`, the name being `socket.gethostname().split(".")[0]`; the rest of the report calls this machine `local`. This line keeps its text and its place after `Comparison:`.
- per check, `check: <session dir name>/ux_<phase>_<k>`, then `helper defects: N`, `pool defects: N` and `pool missed defects: N`, each followed by its rows as a `| Shot | Rule | Defect | Fix |` table;
- `unpaired file: <path>; missing <path>` for a check with one file of its pair, and `verdict unavailable: <path>: <reason>` for an unusable file; neither counts as a complete check;
- `complete shadow checks: <N>; incomplete: <K>; pool missed rows: <M>`
- `Adoption is the user's decision after ten checks; this report creates no marker.`

A helper row is matched to pool rows on its (rule, shot) pairs, by count: the rule stem lower-cased without backticks or `.md`, and each shot's file name. Wording is not matched, since two reviewers never word a defect alike. A helper row that lists several shots is found only when the pool has every one of its pairs left.

`read_ux_verdict(path)` returns `PassUXVerdict`, `DefectsUXVerdict(first, remaining)` or `UnusableUXVerdict(reason)`. It requires exactly one verdict line (`Verdict: pass|defects`, any letter case, emphasis marks ignored, no other text), exactly one four-column `Shot | Rule | Defect | Fix` table with a valid separator, rows of four non-empty cells, and agreement between the verdict and the rows.

In `docs/production_format.md` item 3 and `commands/showrunner/produce.md`, the notice's design-check line comes from a "fresh check": the Claude helper, or the pooled UI reviewer once the production has adopted it.

## Invariants

A rule stated with its mechanism above is not repeated here.

- Allocation is a script call that returns at once. It never waits, never queues and never involves a session.
- A reviewer edits nothing. Every turn is read-only, and the launcher still checks the tree afterwards. The launcher writes every result into the requesting unit's own session directory and records the pass there; no other process writes a unit's ledger or history.
- With no `**Review pool:**` line in the production doc, and outside a production, every command behaves as it did without the pool. `review.sh`'s `--slot` and fresh paths are unchanged; `--to` gains only the turn record and the signal traps that mark it `canceled`.
- A reviewer name is issued once per pool lifetime from `next_index`: a retirement, a failed fork or a removed reservation never frees a name. Names start again at `code1` after a `stop`, so anything that follows a reviewer across time keys on `thread_id`.
- Every caller reads `mesh_dir` from the allocation line and never builds the path.
- A change to `review.sh` keeps `TURN_RECORD_STATUS`, the slot file and `release --thread-id`.
- Only the review director writes memory. It stores rules and defect classes, never diffs, code, shots or complete review output.
- A caller of `stop` reports a nonzero exit and runs `stop` again.

## Calibration and gotchas

**Numbers**

- The prompt cache is per thread. First request after a reset: 212 uncached / 23,552 cached / 9 output = 1,435 weighted tokens. First after a new fork: 12,372 / 11,392 / 8 = 12,982. A reset took 0.199 s, a fork 0.189 s. This is one measured pair (2026-10-12, Codex CLI 0.162.1, `gpt-6.1-sol` weights uncached 1, cached 0.05, output 5; the run is recorded in `docs/as-built/review-pool-probes.md`), not a standing guarantee of cache behavior.
- `config/review_pool/code.md` stays under 7 KB. `ui.md` holds the whole judging procedure and measures 8,376 bytes; `standing/ui.md` adds every rule's full text.
- A pooled UI turn with the rules in its standing prompt (2026-10-13: scratch pool, hana guide, 30 rules, one stored shot) read neither `ux_eval.md` nor a rule file, `--shadow` gave `pool missed defects: 0`, and it cost 79,903 weighted tokens in 6 requests against 102,264 in 8 for a turn that read them.
- Repository memory: at most 4,000 tokens for `code` and 3,000 for `ui`, counted as UTF-8 bytes divided by four, rounded up; the least-cited entry is pruned at the cap.

**Mesh**

- `reset` refuses a stale claim left by a killed launcher, where `follow` takes one over. `reset` also writes one role-assignment line to the usage log through its claim, though it runs no turn.
- `codex_mesh.py stop` marks every roster name `ended`; `idle-stop` and `sweep --stop` do not. `review_pool.py stop` therefore empties the pool. Real `codex_mesh.py stop` on an empty session directory exits 0.
- The mesh's idle watcher may stop the app-server after 30 minutes without use; reviewers stay followable and the next mesh call restarts it, so the sweep was not changed to protect pooled threads.
- A revert writes a new rollout file under the same thread id, and a compaction writes zero input and output counters, so a compaction turn has no readable cost.

**Pool**

- Old `mesh/<generation>/` directories stay on disk after `stop`.
- A launcher killed with KILL writes no record and releases nothing; `end-lease` and `health` clear its reviewer once its pid is dead.
- `health` prints ended leases on stdout and busy reviewers on stderr with `lease left intact`; busy is normal and is not reported.
- `stop` removes `ui_adopted` before the mesh call, so a failed stop has already cleared adoption.
- `basedpyright` exits 3 for the missing `.venv`; its `0 errors, 0 warnings` line is the pass.

**Launcher**

- `review.sh` reads its start time before it compacts a large thread, so most own-arm windows open with a compaction; the first request after it re-reads the kept context (about 86,000 uncached tokens), which leans the comparison toward the pool. A compaction keeps 55,000 to 108,000 tokens, so such a thread is compacted again at nearly every follow-up. The start time stays where it is because that part of the launcher belongs to the model-study unit; the report's `began with a compaction` line counts these records.
- `started_at` is whole seconds, so two windows on one thread can overlap inside one second; the overlap goes to the earlier record.
- A pooled UI turn may read `measure_text.py`'s source for its flags; that is a script, not a rule.

**Report**

- The hourly `usage-collect` job on natedev builds the store for both machines; the Mac has no store of its own. The report runs on natedev, and its tables carry costs only once the store exists. An empty cost cell means unavailable, never zero.
- The collector keys a Codex request by `<session file name>:<byte offset>` (`collect.codex_key`), so the new rollout file a `--fresh` reset or a revert starts keeps every request of its own. Two files of one thread with the same last path component would still share keys (`docs/as-built/usage-ledger.md`).
- `read_events` returns `[]` for a missing file, so `local_sources` stores a history only when `is_file()` holds: an absent entry in `sources.histories` is what tells a missing history (`history file unavailable`) apart from an empty one (`no run_started event`). Symlinks in plan paths are not resolved.
- A live check of the UI pool belongs in a scratch `REVIEW_POOL_STATE` and a scratch `PLAN_DELEGATE_HISTORY_DIR`; the real pool state and history then hold no trial record of it.

**Review director**

- The director's session directory lives under the history root, never under `/tmp`: no `prepare_session.sh`, no run-active marker, no report timer.
- `start-run` returns early when the session's state file exists, so a resume from another session is safe. With no `--main-*` flags the recorder detects the director's model and effort itself, so the director does not read them from its transcript.
- A production doc that was never committed has no start date; the director reports that and runs no report.
- `--shadow` reads only this machine's verdict pairs. Reading a second machine's pairs waits until a unit on the Mac runs design checks.
- No live pool, live director session or Mac history has exercised `/review:direct`; the tests cover the scripts and the recorder calls.

## Why

A reason given beside its mechanism above is not repeated here.

- **Allocation is a script with no queue, and each production has its own pool.** The user's rules: a review never waits in line (a new reviewer is forked instead), no tokens go to allocation, and each showrunner has its own reviewer pool.
- **A lease ends with `reset`, and rules 1 and 2 come before a fork.** The first request after a new fork costs about five times the first after a reset, so retiring a thread and forking a new one at lease end was ruled out.
- **One mesh directory per pool lifetime.** With one shared `mesh/`, a fork that finishes after `stop` writes into the roster the next lifetime reads.
- **Names are never reused.** A process still attached to a retired thread can point a reused name back at it.
- **A failed `stop` keeps the `stopping` marker.** Clearing it would leave no later `stop` able to reach the ended lifetime's server.
- **The launcher leaves its pass open on a signal.** If it closed the pass, the unit director's cancel call would fail on a closed pass, and the two arms would differ.
- **The pool prompt says `As in your standing instructions.` and names no file.** A leased reviewer holds the earlier pinned text, which `config/review_pool/code.md` may no longer match.
- **Cost comes from the usage store.** It already holds every request with its model, time and compactions for both machines, so reading rollout files through `scripts/usage/rollouts.py` was ruled out.
- **The production's start date comes from Git.** A date typed onto the production log's title line was ruled out: existing productions have none.
- **The director reads run history, not `findings.py status`.** By merge time the unit has usually moved on, and that command shows only the active phase.
- **The pooled UI reviewer's standing prompt holds the rule texts.** A `ui` turn starts from a reset thread, so a prompt that names `commands/ux_eval.md` and the rule files has them read again on every turn; with the texts standing, the turn reads none and cost about 22% fewer weighted tokens (Numbers).
- **Design decisions come from the production's repository, never from the guide.** It is the file the helper's `ux_eval.md` reads; taking it from the guide would make the helper and the pool judge against different exceptions.

## Risks

From the proposal (`docs/plans/proposal-review-unit.md`, 2026-10-10, removed 2026-10-13 once built), as weighed then.

| Risk | What contains it |
| --- | --- |
| **The cost of standing context per request** (the largest risk) | Prefix tokens are re-billed on every request, so a pool that keeps growing context loses to fresh reviewers without anyone seeing it. Standing prompts are capped (7k code, 6k UI) and leases revert. The trial (Trial design below) ends the pool if the per-review number does not drop. |
| One unit's details in another unit's review | A lease is one unit's. A revert removes the whole lease before the reviewer serves anyone else. Per-unit memory is sent only in that unit's lease. The repository memory holds classes and rulings, never diffs; its writer is the director, who applies that rule. |
| Stale context | The prompt says to re-read the tree each review. A lease never outlives its phase. Memory entries carry a date and source and are pruned when a ruling changes. A changed guide hash retires reviewers at lease end. |
| One point of failure | The request path is the unit's own `review.sh` plus one app-server. A dead server is restarted on the next call, and its threads resume (existing `follow` behavior). If the pool cannot open a thread at all, `review.sh --pool` exits `error`, and the unit director runs today's fresh path, as <DelegateLaunchFailure/> already directs. This applies on failure, not on load. |
| Sandbox reads of every worktree | Codex `readOnly` reads the whole filesystem, so it needs no grant per worktree. It also cannot stop a reviewer reading a sibling unit's tree. All trees are the user's own, so this is a correctness risk, not a privacy one. The prompt names the worktree and head hash. The reviewer's first command is `git -C <worktree> rev-parse HEAD`, and a mismatch reports `error`. |
| Writer independence | An improvement on own-worker review, where a writer can review its own code: a pool reviewer never wrote what it reads. |
| UI reviewer on Codex | Today's design check runs on a Claude helper. Phase D runs the Codex UI reviewer in shadow first and adopts it only if it misses nothing the Claude helper found. |

## Trial design

The proposal's build order and the measurement the trial report implements. Phase A's first probe was settled by measurement: a lease ends with `reset` (Why).

**Phase A — probes (one session, no skill text).** On a scratch app-server:
1. Does the first request after `thread/revert` read the standing prompt as
   cached input? Compare with the first request after `thread/fork`. The answer
   decides revert or fork for lease end; the measurement for each is the
   `cached_input_tokens` of that request.
2. Do `developerInstructions` survive `thread/compact/start`?
3. Do per-turn `cwd`, `readOnly` and `outputSchema` work?
4. Can one app-server serve two clients' turns at once?

Also drop `--ephemeral` for reviews, so the baseline starts collecting rollouts.

**Phase B — code pool on build-followups.** `review_pool.py`, the mesh verbs,
`review.sh --pool code`, the `direct.md` and `dual_review.md` text. Trial
assignment: `allocate` sends a phase's reviews to the pool or to the unit's own
workers (`--to`) by a hash of unit and phase. That gives an even mix in
the same window. This is an assignment for comparison, not a fallback.

**Phase C — review unit director, memory, Hana.** `/review:direct`, memory
curation, the production doc, the `produce.md` changes. Roll out to the Hana
production with the same assignment by hash.

**Phase D — UI pool.** Shadow first: for ten design checks, the Codex UI
reviewer judges the same shots as the Claude helper. Its verdict is recorded
but not used. Adopt when it found every defect the helper found.

**Measuring tokens per review.** `worker_usage.py --reviews --since <date>` reads
every `review_turn_*.json` under `/tmp/claude/delegate/` on natedev and the
Mac. It sums the rollout's token counts for that turn id, and prints weighted
tokens per review by arm (fresh, own worker, pool) and kind (broad lens, closure,
ux):
- median and mean;
- the share from cached input, which shows standing-context cost directly;
- for the pool, the lease ordinal, so the warm-closure saving shows apart from
  the startup saving.

Fresh reviews are measured from their rollouts once `--ephemeral` is gone.
`codex exec`'s `tokens used` total does not split cached from uncached input.

**Measuring whether context build-up improves reviews.** Per review, from the
records each unit already keeps:
- **raised:** numbered items in its findings file;
- **confirmed:** ledger rows opened with its lens in that pass;
- **precision:** confirmed ÷ raised;
- **escapes:** defects in the same phase the review should have caught. These
  are rows opened `--caught-by main` only, new defects found by a later closure
  in code the broad review read, merge holds and merge design-check defects
  (`review_regime.py`, regime `pool`), and red CI traced to the phase.

`worker_usage.py --reviews --quality` plots precision and escapes per review
against two measures:
- the memory entry count when the review ran (memory version, stored with the
  turn record);
- the reviewer's review count.

It does the same for the own-worker and fresh arms over the same window.

**Verdict, after 60 pool reviews or 3 weeks, whichever is later:**
- **Keep the pool** when its weighted tokens per review are at most 85% of the
  own-worker arm's, and its escapes per phase are no higher.
- **Keep the memory** when precision or escapes improve with memory version
  (a falling trend across at least four memory versions). Otherwise cut memory
  to the guides and contracts alone. It then costs fewer prefix tokens and buys
  no measured quality.
- **Remove the pool** when it misses the 85% line. Own-worker reviews
  stay. The user decides with the report in hand.

The likely sources of a saving, to check against the report:
- a pooled reviewer's first request is cached, saving most of the 19k–26k
  preamble that a fresh reviewer pays uncached, about 18k weighted per review;
- a closure on a warm lease reads less code. Closures are two thirds of reviews.

The likely loss: a pool reviewer is cold on the unit's code, where an own
worker is warm. The trial exists to find which effect is larger.

## What the showrunner and directors said

Asked 2026-10-10. All four answered; replies are paraphrased by the coordinator
and attributed by session name.

- **hana** (showrunner): send a request file with "worktree path, base and head
  hashes, files, the Work Order and the guide set". The reviewer writes
  findings.py-schema JSON into a file the unit owns, and "the director keeps
  triage". Keep "the guides, the type-design contract, recurring defect
  classes, per-crate idioms". Forget "each diff, each unit's plan, earlier
  verdicts on a file", and compact between reviews. Worried because
  "checkpoints bunch up after build holds"; wanted a wait limit with a fallback
  to the unit's own worker, and no single pool that can block everyone. UX: a
  fresh helper per checkpoint; a long-lived UI reviewer "may keep the UX rule
  list, but reads each shot fresh".
- **organon** (geometry-material-unit): hand over "base and tip, Work Order
  path, lens list, session dir". The pass goes in organon's progress table
  "with the reviewer's id". Keep "Hana's codebase map, the unit's standing
  rules (never-edit files, ownership, the 9 px floor)" with the style guide and
  type contract. Forget "earlier verdicts on its code, and other units' diffs".
  Fall back to its own worker after about 10 minutes. UX: fresh at each
  checkpoint.
- **widget** (widget-unit): send "base..HEAD plus uncommitted paths", the plan
  heading, the lens and the session dir. Get back "findings.py-ready JSON
  lines" plus one board post. Keep the crate's settled rulings "and its past
  findings with how each closed". Forget "any belief about the tree's current
  state; re-read every time". Warned that "a reviewer warm on another unit is
  cold on this one". Wanted a 15-minute limit and a readable queue position.
  UX: fresh, because "a long-lived eye gets used to the look".
- **trunk** (trunk-unit): the pass shows under slot `review`. Either the review
  unit writes trunk's history, "or sends two lines for trunk to record". Keep
  "crate layout, style and type rulings already made, recurring defect
  classes". Closure reviews "must come first, never behind another unit's
  broad review". Opens its own worker after about 10 minutes of waiting. UX: "a
  fresh verdict on exactly the shots in the notice". A long-lived UI reviewer
  "may hold the guide for repair rounds".

**How the proposal answers them.**
- Queueing, their shared first worry, is gone by the user's rule. Allocation
  creates a reviewer rather than waiting, so wait limits, queue positions and
  closure priority have nothing to act on.
- Findings land in each unit's own files, and triage stays with its director
  (all four).
- The pass is recorded by the unit's own launcher (organon, trunk).
- What to keep became the review memory, and what to forget is removed by the
  lease revert (all four).
- Widget's "warm on another unit is cold on this one" is the reason leases are
  per unit and the trial compares against warm own workers.
- UX verdicts are given from a reverted UI reviewer, which is the fresh eye the
  four asked for. The rule list and rulings persist, as the user asked.

## Tests

- `scripts/delegate/test_review_pool.py` (61 tests) runs the pool CLI as subprocesses against a stub mesh named by `REVIEW_POOL_MESH`: primes and old-hash retirement, the arm split, allocation, name issue, `--fresh`, `release --thread-id`, `end-lease`, `health`, dead reservations, `stop` and its recovery, a stop that lands during a prime, fork or reset, `unit-memory` and the adoption marker. `UIStandingPromptTests` pins `ui.md`'s verdict line, its table header and `measure_text.py`, and the absence of the old instructions to follow `ux_eval.md` and read a rule's full text.
- `scripts/delegate/test_review_launcher.py` (55 tests) covers `review.sh`: the pool path, unit memory, the slot file, turn records of both arms and all four statuses, signals, a replaced thread, the tree check, the argument refusals, shots for `ui`, the `ux` row count, no pass for lens `ux`, and the assigned and fresh paths.
- `scripts/delegate/test_review_pool_report.py` (80 tests) runs the report as subprocesses. `populate_usage_store` collects fixture rollouts into a fixture store, and `USAGE_STATE_DIR` points the script at it. It covers request attribution, every incomplete reason, compaction counts, outcome lines, the quality table with merge holds and mixed phases, each merge-hold match case and the `merge holds unmatched` warning, plan normalization, Mac rows and histories, the final line on both sides of 20, an absent and an unreadable store, the Mac source warning, and `--shadow` with its `Machine:` line. These tests call the collector's `SCHEMA`, `migrate_store`, `read_file`, `save_measurement` and `RoleResolver`; a rename in `scripts/usage/collect.py` breaks them. The fixtures prefix the collector's request key with the file path.
- `scripts/agents/test_codex_mesh.py`, with a fake app-server, covers `prime`, `fork` and `reset`, the three `follow` flags, and standing instructions restored on resume, fork, compact, reset and a replaced thread.
- `scripts/production/test_add_unit.py` covers the review unit's opening message: `/review:direct` with a pool line, `/unit:direct` without one.
- `scripts/production/test_review_unit_phase.py` (3 tests): the standing phase reads as `1 of 1`, running; a resume adds no run and no phase; shutdown finishes the run while the plan heading stays `todo`.
- `scripts/delegate/test_progress_history.py` covers the `UX review` activity's time in the review trial.
