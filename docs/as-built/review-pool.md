# Review pool

## What it is

Each production has its own pool of long-lived, read-only Codex reviewers. A unit that needs a review calls a script, which hands it a reviewer at once. Each unit has a permanent reviewer per type: its first review forks one from the type's template, and every later review resumes that same thread, so the reviewer keeps building context about the unit's work across phases and plans. When every reviewer of the unit is busy, the pool forks the unit's newest reviewer, and the fork stays as the unit's second reviewer. A reviewer ends only when its unit is retired, `migrate` finds its unit gone, or the pool stops. A second reviewer type judges screenshots; it runs in shadow beside the Claude design-check helper until the user adopts it.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/direct/review_pool.py` | The pool CLI: templates, reviewers, unit leases, re-pointing, retire, migrate, stop, and the standing prompt each template is primed from |
| `scripts/direct/review.sh` | The review launcher; its `--pool` path allocates, runs and records one review turn |
| `scripts/direct/review_pool_report.py` | The cost and quality report over turn records, and `--shadow` |
| `scripts/agents/codex_mesh.py` | The Codex mesh; `prime`, `fork`, `set-instructions` and three `follow` flags exist for the pool |
| `config/review/code.md` | Standing instructions of the code reviewer |
| `config/review_pool/ui.md` | Standing instructions of the UI reviewer |
| `commands/unit/direct.md` | The unit director's side: pool form of each review launch, <UXReview/> |
| `docs/direct/dual_review.md` | Pool prompt opening, pool launch call, cancel through the slot file |
| `docs/direct/phase_end.md` | States that a pooled reviewer belongs to the unit and stays at phase end |
| `docs/production_format.md` | Words, the `**Review pool:**` line, `retire` before a unit is marked retired, <ProductionUnit/> items 3 and 16 |
| `commands/showrunner/produce.md` | `retire` for a retired unit, `stop` at wrap |

### State

A pool belongs to one production. Its state is `~/.local/state/review-pool/<slug>/`; `REVIEW_POOL_STATE` replaces the root. `<slug>` is the production doc's file name without `-production.md`, and `--production` accepts the slug or the doc's path. `_production` passes that slug through `showrunners.canonical_slug`, so a former slug reaches the current pool; a production rename moves the pool directory, leases included (`docs/as-built/production-rename.md`).

| Path | Holds |
| --- | --- |
| `pool.json` | Templates, reviewers, leases, the generation. Written through a temporary file, `fsync` (skipped when `UNIT_SYNC_WRITES=0`, for test fixtures) and `os.replace` |
| `pool.lock` | One `fcntl.flock` around every read-modify-write of `pool.json` |
| `prime/<sha256>.md` | Copies of standing prompts, named by content hash |
| `cwd/` | The templates' working directory, empty |
| `mesh/<generation>/` | One mesh session directory per pool lifetime |
| `ui_adopted` | The UI pool's adoption marker; present only after the user creates it |

`pool.json`:

```
{"generation": int, "stopping": true,
 "next_index": {"code": n, "ui": n},
 "templates": {type: {"name", "thread_id", "prime_hash"}},
 "reviewers": {name: {"type", "thread_id", "prime_hash",
                      "lease": {"unit", "ordinal"} | null,
                      "busy_pid": int | null,
                      "reviews": int, "last_allocated"}}}
```

A new file starts with `generation: 0`. `stopping` is present only while a stop is unfinished. A file without `next_index` reads each counter as 1. A file written before reviewers became permanent may still hold `split`, a lease `phase` or a reviewer's `served`; they load and the next write drops them. Types are `code` and `ui`; reviewer names are `<type><n>` (`code1`, `ui2`); a template is named `<type>-template-<hash8>`. In code a lease is `Unleased` or `UnitLease(unit, ordinal)`, and a reviewer's ownership is `IdleReviewer` or `ClaimedReviewer`; `null` appears only where `pool.json` is read and written. A lease's `ordinal` counts the reviews the reviewer has carried on its current thread.

`<history root>` is `UNIT_HISTORY_DIR`, or `~/.local/state/unit-history` (`~/.local/state/plan-delegate` until `unit_runs.py move-history` moves it). Review turn records and UI verdict pairs live in its `review_turns/<session dir name>/`.

### `review_pool.py` verbs

Every verb takes `--production <slug>`. A failure prints `review_pool: <reason>` on stderr and exits 1. Every mesh call runs outside the lock: a verb reads `generation` before the call and publishes the result only when it is unchanged; otherwise it drops the result and exits 1 with `pool was stopped during the mesh call; result dropped`. `REVIEW_POOL_MESH` names another script to run in place of `codex_mesh.py`.

- **`prime --type T --instructions-file F`** copies `F` to `prime/<sha256>.md` and prints the hash. When the hash differs from the type's template, it calls `codex_mesh.py prime` as `<type>-template-<hash8>` with that copy's absolute path and `cwd/`, and records the template. An unchanged hash makes no mesh call. Then it re-points every idle reviewer of the type whose `prime_hash` differs from the template's (Re-pointing). A `prime/` file is deleted once no template or reviewer carries its hash and no process holds a shared lock on it, and not before.
- **`allocate --type T --unit U --phase P --pass N [--lens L] --pid PID`** first builds the type's standing prompt (Priming) and primes it as `prime` does; with no change that makes no mesh call. When the build fails, a primed template keeps its prompt and the cause goes to stderr; with no template, allocate exits 1 with `no primed <type> template: <cause>`. An unfinished stop is reported before the build. Then it takes the first match under the lock:
  1. the unit's most recently allocated idle reviewer of the type;
  2. when the unit has no reviewer of the type, an idle unleased reviewer carrying the template's hash (a pool written before leases were per unit can hold one);
  3. a new reviewer: a name reserved under the lock with `busy_pid` and no thread id, then `codex_mesh.py fork` outside the lock, the thread id recorded under the lock. The fork's source is the unit's most recently allocated reviewer of the type when the unit has one (every one of them is busy), so the fork starts with the unit's context and inherits its lease count and hash; otherwise the template. When the fork of the unit's reviewer fails, the pool prints `review_pool: fork of <source> failed (<reason>); forking the template` on stderr and forks the template instead.

  A reviewer forked from the template takes the template's hash and lease count 1, and starts the unit's reviews again. A reviewer is idle when `busy_pid` is null or names a dead process; a reservation with no thread id is never a candidate. The chosen reviewer gets `busy_pid`, its lease set to `U` with `ordinal` raised by 1, and `last_allocated` set. When its `prime_hash` is no longer the template's, allocate re-points it before returning. The verb prints one JSON line `{"reviewer", "thread_id", "mesh_dir", "lease_ordinal", "prime_hash"}`, where `mesh_dir` is the current `mesh/<generation>/`. It exits 1 with `no primed <type> template: <cause>` or `fork <name> failed: <reason>`; a failed fork removes its reservation. `--phase` and `--pass` are required and only describe the turn: the lease names the unit alone. `--fresh` is accepted from a launcher older than permanent reviewers and changes nothing.
- **`release --reviewer R --pid PID [--thread-id ID]`** clears `busy_pid` when it is `PID` and adds 1 to `reviews`; the lease stays. With a `--thread-id` that differs from the stored one it stores the new id and sets the lease count to 0: the mesh replaced a thread it could not resume, and the new thread holds none of the unit's history, so the next review carries the unit memory again.
- **`retire --unit U`** ends the unit's reviewers of both types. `U` matches one unit id exactly. A name some reviewer is leased under wins; only when none is does a short name resolve to its Units row (`_lease_unit`, `_unit_id`): `trunk` is `trunk-unit` when the doc has that row and no `trunk` row. `allocate` resolves its `--unit` the same way, so `handler-enh` never retires `handler-enh-unit`'s reviewers (cargo-liner, 2026-10-19). An idle reviewer is claimed and ended with `codex_mesh.py end`, removed, and its name printed on stdout, with `review_pool: retired <name>: <unit> retired` on stderr. A reservation with no thread id is removed and printed. One still reviewing is left in place with `review_pool: <name> is reviewing; left in place` on stderr; run `retire` again once its review ends.
- **`migrate [--apply]`** reads the production's running units from `unit_lookup.marked_units(slug)`; a unit whose state is unknown counts as running. It prints one tab-separated line per reviewer, `keep` or `retire`, name and reason, newest first: a busy reviewer and a fork in flight are kept; an unleased reviewer retires (`no unit`), as does one whose unit is not running (`trunk` matches `trunk-unit`); each running unit keeps its newest reviewer per type and retires the rest (`<unit> keeps a newer <type> reviewer`). With `--apply` it retires each `retire` line whose reviewer is unchanged and idle, and names any other on stderr with `changed since the plan; left in place`. The showrunner runs `--apply`.
- **`end-lease --unit U [--phase P]`** is accepted and ends nothing, so a director still running the old phase-end step leaves its reviewer in place.
- **`status`** prints one tab-separated line per reviewer: name, type, lease (`unleased` or `<unit>:<ordinal>`), `idle` or `busy`, reviews.
- **`stop`** removes `ui_adopted` when present (printing `review_pool: ui adoption cleared` on stderr), raises `generation`, empties `templates` and `reviewers`, sets each name counter back to 1, and runs `codex_mesh.py stop` on the ended lifetime's directory. When that call fails, `stop` exits 1 with the mesh's reason and leaves `stopping` set: `prime`, `allocate`, `retire` and `migrate` then exit 1 with `pool stop did not finish; run stop again` until a `stop` succeeds, and a repeated `stop` runs against the same ended directory.

### Re-pointing

A reviewer is never reset, so a changed standing prompt reaches it in place. `codex_mesh._resume_params` sends the roster entry's `instructions_file` as `developerInstructions` on every resume, so changing that path changes what the reviewer's next turn carries, and the thread and its turns stay.

`_repoint` runs while the pool claims the reviewer: it opens `prime/<hash>.md` and takes a shared lock on it, so prompt collection cannot delete the file before the pool names its hash; runs `codex_mesh.py set-instructions --to <name> --instructions-file <copy>`; and, on success, sets the reviewer's `prime_hash` under the pool lock. `prime` re-points each idle reviewer of the type, claiming each one for the call and releasing it after. A reviewer busy at prime time is passed over, and its next `allocate` re-points it before the turn. A refused re-point (the copy was already collected, or the mesh refused) prints `review_pool: <name> keeps its standing prompt: <reason>` on stderr and keeps the old hash, so the allocation line and the turn record name the prompt the follow actually sends.

### Mesh verbs the pool relies on

- **`prime --session-dir D --name N --instructions-file F --cwd C [--model M] [--service-tier T]`** sends `thread/start` with `developerInstructions` set to the file's text, `sandbox: "read-only"` and `approvalPolicy: "never"`, then `thread/name/set`, then writes the roster entry `{thread_id, status: "done", role: "review", context_tokens: 0, instructions_file, model, effort, access_policy: {"kind": "ReadOnlyReviewer"}}` with the file's absolute path. It starts no turn and prints the thread id.
- **`fork --session-dir D --from N --name M`** resumes the source, sends `thread/fork` with the same sandbox and approval policy, names the thread, and writes the same roster entry for `M` with `access_policy: {"kind": "ReadOnlyReviewer"}`, copying the source's `instructions_file`. A source that is a held design role refuses. It exits 2 when `M` already names a thread that is not `ended`, or when the name was taken while the fork ran. It prints the new thread id.
- **`set-instructions --session-dir D --to N --instructions-file F`** stores `F`'s absolute path as the roster entry's `instructions_file`. A missing file exits 1, and a held design role refuses (`docs/as-built/guilds.md`). A reviewer that is busy or not followable, or whose roster entry changed while the call ran, is refused with exit 2 and `instructions unchanged`.
- **`follow`** takes `--cwd <dir>` (that turn's working directory), `--read-only` (`sandboxPolicy: {"type": "readOnly", "networkAccess": false}` for that turn) and `--image <path>` (repeatable; one `{"type": "localImage", "path": …}` input per image after the text). Without the flags its request is unchanged.
- **`_resume_params`** reads the roster's `instructions_file` and sends its text as `developerInstructions` on every `thread/resume` and on `thread/fork`, because the app-server drops standing instructions on both. The sandbox comes from the entry's `access_policy`, not from the instructions: `ReadOnlyReviewer` adds `sandbox: "read-only"` and `approvalPolicy: "never"`. When that file is missing, `follow`, `fork`, `compact` and `send` exit 1. When `follow` has to replace a thread whose resume fails (`_resume_worker_thread`), the replacement gets the same instructions and the sandbox its `access_policy` gives under the same name, and its id goes to the roster; the launcher passes that id to `release --thread-id`.
- The pool also uses `end --to` (retire a reviewer) and `stop` (end a pool lifetime); the launcher uses `compact`, `can-follow --claim-pid` and `release-follow`. The mesh still has `reset`, which the pool no longer calls.

`prime`, `fork` and `set-instructions` print a failure as `codex_mesh: <name>: <reason>`.

### A pooled review turn

```
review.sh --pool code|ui --lease <slug>/<unit>/<phase> \
  <session_dir> [working_dir] [prompt_file] [task] [role_description] \
  [pass_activity] [pass_index] [lens] [early_ready_file]
```

The two options go before the positional arguments, which are the same as on every other path. `--lease` keeps the phase because `allocate --phase` and the turn record need it. These refusals exit 2 before any artifact, allocation or turn record:

- `ERROR: --pool excludes --to and --slot.`
- `ERROR: --pool requires code or ui.`
- `ERROR: --pool requires --lease slug/unit/phase.`
- `ERROR: --lease requires --pool.`
- `ERROR: --pool ui requires lens ux.`
- `ERROR: --pool code does not accept lens ux.`

Lens `ux` with no pool is accepted. From there the launcher:

1. Removes `${SESSION_DIR}/review_slot<suffix>` (the suffix is `_<lens>` with a lens and empty without one), reads its start time, and installs the exit handler that writes the turn record.
2. Resolves the review agent and requires the Codex family with the mesh; otherwise `ERROR: --pool requires the Codex mesh.`, exit 2.
3. For `ui`, reads `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`, one path per line, into `--image` arguments. An unreadable file (`ERROR: UI shots file is unreadable: <path>.`) or one with no paths (`ERROR: UI shots file has no paths: <path>.`) exits 1 before allocation.
4. Runs `review_pool.py allocate --production <slug> --type <type> --unit <unit> --phase <phase> --pass <pass_index> [--lens L] --pid $$`. A failure prints `ERROR: pool allocation failed: <the pool's last stderr line>; see <log>.` and exits 1 with no turn; the log is `review_agent_<N>[_<lens>]-pool.log`.
5. Takes `reviewer`, `mesh_dir`, `thread_id`, `lease_ordinal` and `prime_hash` from the allocation line. Its slot is `pool_<reviewer>`: exported as `UNIT_TEAM_ROLE` and written as one line to the slot file, since the slot is known only after allocation and the unit director needs it to cancel.
7. Claims the reviewer in the pool's `mesh_dir` (`can-follow --claim-pid $$`), compacting it first when its recorded context is an integer over `UNIT_COMPACT_ABOVE_TOKENS` (100000 in `config/direct.conf`). Roster, compaction, claim and claim release use the pool's `mesh_dir`; status file, findings, agent log, heartbeat, board and pass stay in the unit's `${SESSION_DIR}`.
8. Records the review pass under its slot, except for lens `ux` (`record_pass_start` returns at once, with a pool and without one): the unit director's `UX review` activity already carries that time, and a pass would count it twice. An early launch defers the pass until its sentinel file appears, as on the assigned path; it checks for the file every `UNIT_EARLY_POLL_SECS` (default 5 s; tests set it small).
9. Runs `codex_mesh.py follow --session-dir <mesh_dir> --to <reviewer> --claim-pid $$ --role review [--lens L] --cwd "${WORKING_DIR}" --read-only [--image …] --message-file … --summary-file <findings> --reply-file … --log-file … --model … --effort … --service-tier …` with the review model, effort and tier.
10. After the turn, checks that the working tree is unchanged, counts the findings, writes `reviewed` or `error` to the status file and closes the pass (`finish-pass --status completed` or `error`).
11. On every exit: writes `error` to the status file when the exit code is not 0, reads the reviewer's thread id from the mesh roster, writes the turn record, releases the mesh claim, runs `review_pool.py release --production <slug> --reviewer R --pid $$ --thread-id <id>` and removes the temporary prompt file. A record that cannot be written prints `ERROR: unable to write review turn record.` and makes the exit 1.

A pool launcher stopped by TERM, INT or HUP stops its child and its heartbeat watcher, marks the record `canceled` and exits 143, 130 or 129. It leaves its pass open. The unit director closes that pass with `finish-pass --status canceled --orphaned-launcher` under the slot it reads from the slot file (`review_slot_adversary` for a pass-1 early review, `review_slot_<lens>` for a lens, `review_slot` for a closure); a missing file means no pass to close. A `--to` launcher marks its record `canceled` on the same signals and exits with the same codes.

A pool `error` falls back to `review.sh --to <worker>` for that review under <WorkerLaunchFailure/>.

### Turn record

A `--pool` launch and a `--to` launch each write one new file per attempt, including an attempt that fails after the argument checks above: `<history root>/review_turns/<session dir name>/<started epoch>-<pid>.json`, created exclusively and never overwritten. `--slot` and fresh launches write none. `clear_phase_review.sh` never reaches this directory.

| Field | Value |
| --- | --- |
| `arm` | `pool` for a `--pool` launch, `own` for a `--to` launch. The report counts both together |
| `pool` | The production slug on a pool launch, empty otherwise |
| `reviewer` | The reviewer or worker name |
| `thread_id` | The roster's id after the turn, so it names the thread that ran it |
| `unit`, `phase` | From `--lease`; empty on a `--to` launch |
| `phase_instance_id`, `history_file` | From the unit's progress recorder state |
| `kind` | `ux` for lens `ux`, otherwise `broad` for pass 1 with a lens, otherwise `closure` |
| `lens`, `pass` | The lens; the pass index as an integer |
| `started_at`, `finished_at` | Whole epoch seconds; fractional epoch seconds |
| `lease_ordinal` | The allocation's lease count; 0 on a `--to` launch |
| `prime_hash` | The hash of the standing prompt the turn carried; empty on a `--to` launch |
| `raised` | Numbered items in the findings file. For lens `ux`: data rows of the table headed `\| Shot \| Rule \| Defect \| Fix \|`, with or without a closing bar, never a header, a separator, numbered prose or a `Canonical candidates` row |
| `status` | `reviewed` after a successful turn; `void` for an early-launched review that finished before its final diff was delivered; `canceled` for TERM, INT or HUP; `error` otherwise |

`status` is held in the shell variable `TURN_RECORD_STATUS` and never derived from the exit code: a bash `EXIT` trap sees exit code 0 when the shell dies of TERM with no TERM trap, which is also why both launch paths trap the three signals. `raised` is kept for every status and is 0 for an attempt that ended before its findings file was opened.

### Switching the pool on

`**Review pool:** code` (or `code, ui`) in a production doc's Production Context switches the pool on for that production; the first review of each type primes it. Every code review of every unit then goes to the unit's reviewer with `--pool code --lease <slug>/<unit>/<phase>`; the UI pool is used on every design check. Without the line, reviews go to the unit's own workers with `--to`.

### Prompts

A reviewer's standing instructions come from its template, or from its latest re-point, and reach every turn of its thread. `config/review/code.md` holds the role line, the Three Gods sentence, `type_design.md` verbatim and the Rust style guide's rule headings with each rule's file path. `config/review_pool/ui.md` holds the UI reviewer's whole procedure: the role line, the Three Gods sentence, the UX checklist with each rule file's name, the judging steps (shrink each shot, check that the shots cover every state the context names, propose canonical candidates only for empty slots, measure contrast and text size with `measure_text.py`, normalise sizes to a 1440×900 window, confirm each defect against the rule's included text) and the report format (`Verdict: pass | defects`, the `| Shot | Rule | Defect | Fix |` table, canonical candidates). It tells the reviewer not to load command or rule files; the rule texts come with the standing prompt (Review unit director and review memory). Neither file holds review memory.

On every design check the unit director writes two prompts (<UXReview/> step 3): `${SESSION_DIR}/ux_prompt_<pass>_helper.md`, the Claude helper's prompt that points at `commands/ux_eval.md`, and `ux_prompt_<pass>_pool.md`, which opens "Judge these shots by your standing instructions, which hold the rules and the report format:" and carries the shots, scale, context and production rules. The pool is launched with the pool file, so a pooled UI turn reads no rule or command file; it may still read `measure_text.py`'s source for its flags.

A pool code review prompt opens with the pooled-reviewer line (unit, phase, `Re-read the tree; trust nothing from memory about its state`, and `git -C <worktree> rev-parse HEAD` as the first command, reporting an error when it is not the expected head), and its Three Gods and Type Design Contract sections each hold `As in your standing instructions.` <TeamReview/>'s worker choice and role reassignment do not apply.

### Priming

No unit runs the pool, and no file holds review memory. Each permanent reviewer keeps its own thread of the reviews it did (user, 2026-10-19: "each individual reviewer needs to manage it's own reviews"). `review_pool.standing_prompt` builds a type's prompt on every allocate. Each type has a default base file: `code` uses `config/review/code.md`; `ui` uses `config/review_pool/ui.md`. A non-empty `REVIEW_POOL_CONFIG` replaces the folder for both types, so the base file is `<REVIEW_POOL_CONFIG>/<type>.md`. The prompt contains:

- `code`: `code.md`.
- `ui`, in order:
  1. `ui.md`;
  2. `## Rules`, then `UX guide: <absolute guide path>`, then each `.md` in the guide root with a non-empty frontmatter `test:`, in file-name order, as `### <file name>` and its text; the selection is `scripts/ux_style/load-ux-style.sh`'s, so README and notes are left out;
  3. when present, `docs/design-decisions.md` of the repository that holds the production doc, under `## Design decisions`;
  4. when present, that repository's `docs/hana/targets.md`, under `## Targets`.

The ui build finds the production doc through `showrunners.timer_docs()` and the guide from its `- **UX guide:** ` line. With no doc or no guide it fails, and a ui review falls back as allocate says. A changed base prompt or rule changes the hash, so the next allocate primes a new template and re-points the idle reviewers.

The showrunner runs `review_pool.py retire` for a unit before marking it `retired`, and `review_pool.py stop` at wrap, repeating it until it succeeds.

### Cost and quality report

```
review_pool_report.py --since <date> [--root <history root>] [--no-mac] [--quality]
review_pool_report.py --shadow [--root <history root>]
```

`--since` is required unless `--shadow` is given. The report reads every `review_turns/*/*.json` under the root, plus the Mac's records, history files and merge holds over ssh to `collect.MAC_HOST` unless `--no-mac`. A local record that fails validation (a missing field, a `lease_ordinal` or `raised` that is not an integer, an unknown arm, kind or status) is skipped and named on stderr as `review_pool_report: skipped local:<path>: <reason>`. Every record takes part in cost attribution; only records started on or after the date are reported. Records of both launch paths count together.

**Cost.** Review cost comes from the shared usage store, not from Codex logs: `usage_report.open_read_store(collect.store_path())`, tables `openings`, `folders`, `turns`, `compactions` and `turn_sources`, selected by the records' thread ids. The store names this machine `natedev`; records call it `local`. Each request is weighed per model and time through `weights.weights_at` and `weights.weighted` (`scripts/usage/weights.py`); the report holds no rollout parsing and no weights. A request belongs to at most one record: the earliest-started record of its thread whose window from `started_at` to `finished_at`, both bounds included, contains the request's time. Records of every status own requests, so a `void` or `canceled` turn's tokens never count for a `reviewed` one.

**Output**, in order:

- `Tokens by kind`: `| Kind | Reviews | Median weighted tokens | Mean weighted tokens | Cached input share |`, one row per kind (`broad`, `closure`, `ux`).
- `Pool tokens by lease ordinal`: the same cost columns over pool records, for ordinals `1`, `2` and `3+`.
- `outcomes: void N; canceled N; error N`.
- `cost incomplete: <n>`, followed by `<machine>:<path> (<reason>)` for each `reviewed` record left out of medians and means.
- `began with a compaction: <n>`: `reviewed` records whose window holds a compaction before the first request. Their cost stays complete.
- With `--quality`, the quality table.
- `source warning: <text>` lines.

Only a `reviewed` record is a completed review: reviews, medians, means and `raised` sums take those records alone. A cell with no value prints `-`.

A `cost incomplete` reason is the first that applies: `usage store unavailable` (the store is absent or unreadable), `missing rollout` (the store was read and holds no such thread), `no request in its window`, `a compaction after its first request`; a request whose model has no weights gives `unweighted model <model>`.

**Quality.** `--quality` adds `Code review quality`: `| Phases | Raised | Confirmed | Precision | Escapes per phase |`, one row. It covers code reviews only: records of kind `ux` and findings with lens `ux` are left out, because shadow turns double the raised count for one check. Records are joined by phase instance (machine, `history_file`, `phase_instance_id`); a record with no `phase_instance_id` is a phase of its own. Raised sums `raised` over `reviewed` records. Confirmed counts `finding_opened` events in the phase's `history_file` with `caught_by` `worker` or `both`. Escapes count `caught_by` `main` events plus the `holds` of matching `review_regime.py` ledger rows, from the same machine as the phase.

Merge holds are matched by plan as well as unit and phase, because a unit that runs a second plan starts again at phase 1. `quality_lines` reads the ledger's raw JSON rows through `read_events`, not `review_regime.read_rows()`, so it sees each row's `plan`. Neither the turn record nor `ReviewRecord` carries a plan: `review_plan(record, sources) -> PlanKnown(path) | PlanUnavailable(reason)` takes `plan_doc` and `working_dir` from the first `run_started` event of the record's `history_file`. `normalize_plan(path, working_dir="")` makes an absolute path under `working_dir` relative to it, then applies `os.path.normpath`; a ledger plan is normalized with no `working_dir`, so `docs/plans/alpha.md`, `./docs/plans/alpha.md` and `<working_dir>/docs/plans/alpha.md` are one plan. A row matches by:

- no string `plan`: unit and phase;
- a `plan` and `PlanKnown`: unit, phase and normalized plan;
- a `plan` and `PlanUnavailable`: none; the row's holds are left out and the phase prints one `source warning: merge holds unmatched: <unit> phase <phase>: review plan unavailable (<reason>)`. The reasons are `history file unavailable`, `no run_started event` and `run_started has no plan_doc`.

Mac rows and histories from `mac_sources` take the same path. The table closes with `ui checks: compared by --shadow, not in this table`, then the `merge holds unmatched` warnings.

**Source warnings:** `usage store unavailable: <reason>`; `Mac usage source <state>; last complete read <time>` (the store's `turn_sources` row for the Mac is not `ready`); `merge holds unavailable: <reason>` (quality continues without holds); `merge holds unmatched: <unit> phase <phase>: review plan unavailable (<reason>)` (printed with the quality table, see Quality); `history unavailable: <path>: <reason>`; `Mac unavailable: <reason>`; `Mac file unavailable: <path>: <reason>`; `invalid Mac file: <reason>`.

### UI pool and adoption

With `ui` on the production's `**Review pool:**` line, every design check in <UXReview/> (steps 3, 5 and 6, rechecks and notice checks included) also runs the pooled UI reviewer on the same shots with its own prompt file (Prompts), after the unit director writes the shot paths to `${SESSION_DIR}/ux_shots_${PASS_INDEX}.txt`:

```
review.sh --pool ui --lease <slug>/<unit>/<phase> "${SESSION_DIR}" <worktree> \
  "${SESSION_DIR}/ux_prompt_${PASS_INDEX}_pool.md" review "UI design check" \
  "judging the shots" "${PASS_INDEX}" ux
```

The UI reviewer is permanent too: each check resumes the unit's UI reviewer, which keeps the rules in its standing prompt and judges each check's shots from the prompt. While the pool is in shadow the Claude helper's prompt and verdict path stay as they are: the helper is the baseline the pool is compared with.

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
- A lease names a unit, never a phase. A unit grows only to the number of reviews it runs at once.
- A reviewer is never reset. A unit's plan run ending keeps it; only `retire`, `migrate` and `stop` end it.
- A reviewer edits nothing. Every turn is read-only, and the launcher still checks the tree afterwards. The launcher writes every result into the requesting unit's own session directory and records the pass there; no other process writes a unit's ledger or history.
- With no `**Review pool:**` line in the production doc, and outside a production, every command behaves as it did without the pool. `review.sh`'s `--slot` and fresh paths are unchanged; `--to` gains only the turn record and the signal traps that mark it `canceled`.
- A reviewer name is issued once per pool lifetime from `next_index`: a retirement, a failed fork or a removed reservation never frees a name. Names start again at `code1` after a `stop`, so anything that follows a reviewer across time keys on `thread_id`.
- Every caller reads `mesh_dir` from the allocation line and never builds the path.
- A change to `review.sh` keeps `TURN_RECORD_STATUS`, the slot file and `release --thread-id`.
- A reviewer's `prime_hash` changes only after `set-instructions` succeeded for it.
- A caller of `stop` reports a nonzero exit and runs `stop` again.

## Calibration and gotchas

**Numbers**

- The prompt cache is per thread. First request after a new fork: 12,372 uncached / 11,392 cached / 8 output = 12,982 weighted tokens; on a thread that already held the prompt, 212 / 23,552 / 9 = 1,435. A fork took 0.189 s. This is one measured pair (2026-10-12, Codex CLI 0.162.1, `gpt-6.1-sol` weights uncached 1, cached 0.05, output 5; the run is recorded in `docs/as-built/review-pool-probes.md`), not a standing guarantee of cache behavior.
- `config/review/code.md` stays under 7 KB. `ui.md` holds the whole judging procedure and measures 8,376 bytes; `standing/ui.md` adds every rule's full text.
- A pooled UI turn with the rules in its standing prompt (2026-10-13: scratch pool, hana guide, 30 rules, one stored shot) read neither `ux_eval.md` nor a rule file, `--shadow` gave `pool missed defects: 0`, and it cost 79,903 weighted tokens in 6 requests against 102,264 in 8 for a turn that read them.

**Mesh**

- `codex_mesh.py stop` marks every roster name `ended`; `idle-stop` and `sweep --stop` do not. `review_pool.py stop` therefore empties the pool. Real `codex_mesh.py stop` on an empty session directory exits 0.
- The mesh's idle watcher may stop the app-server after 30 minutes without use; reviewers stay followable and the next mesh call restarts it, so the sweep was not changed to protect pooled threads.
- A long-lived thread's context grows. The launcher compacts it above `UNIT_COMPACT_ABOVE_TOKENS`, and Codex compaction keeps the thread and its standing instructions. A compaction writes zero input and output counters, so a compaction turn has no readable cost.
- A fork taken while its source's turn is still running gets the state before that turn.

**Pool**

- Old `mesh/<generation>/` directories stay on disk after `stop`.
- A launcher killed with KILL writes no record and releases nothing; its reviewer reads as idle once the pid is dead, and the next `allocate` takes it.
- `retire` and `migrate` leave a busy reviewer; run them again after its review.
- `stop` removes `ui_adopted` before the mesh call, so a failed stop has already cleared adoption.
- `basedpyright` exits 3 for the missing `.venv`; its `0 errors, 0 warnings` line is the pass.

**Launcher**

- `review.sh` reads its start time before it compacts a large thread, so a window can open with a compaction; the first request after it re-reads the kept context (about 86,000 uncached tokens). The start time stays where it is because that part of the launcher belongs to the model-study unit; the report's `began with a compaction` line counts these records.
- `started_at` is whole seconds, so two windows on one thread can overlap inside one second; the overlap goes to the earlier record.
- A pooled UI turn may read `measure_text.py`'s source for its flags; that is a script, not a rule.

**Report**

- The hourly `usage-collect` job on natedev builds the store for both machines; the Mac has no store of its own. The report runs on natedev, and its tables carry costs only once the store exists. An empty cost cell means unavailable, never zero.
- The collector keys a Codex request by `<session file name>:<byte offset>` (`collect.codex_key`), so a new rollout file under the same thread id (a replaced thread, or a revert from before reviewers became permanent) keeps every request of its own. Two files of one thread with the same last path component would still share keys (`docs/as-built/usage-ledger.md`).
- `read_events` returns `[]` for a missing file, so `local_sources` stores a history only when `is_file()` holds: an absent entry in `sources.histories` is what tells a missing history (`history file unavailable`) apart from an empty one (`no run_started event`). Symlinks in plan paths are not resolved.
- A live check of the UI pool belongs in a scratch `REVIEW_POOL_STATE` and a scratch `UNIT_HISTORY_DIR`; the real pool state and history then hold no record of it.

- `--shadow` reads only this machine's verdict pairs. Reading a second machine's pairs waits until a unit on the Mac runs design checks.

## Why

A reason given beside its mechanism above is not repeated here.

- **One permanent reviewer per unit.** User, 2026-10-15: reviewers should never restart; each unit keeps its own reviewer, which builds up context about the unit's work, and the pool grows only when a unit needs two reviews at once. Under the earlier design a lease was keyed by `(unit, phase)`, so a new phase never matched the unit's own reviewer: hana's tool-based-ui pool reached 22 reviewer records, names up to `code81`, and 20 idle reviewers still leased to finished phases.
- **A concurrent review forks the unit's reviewer, not the template.** The fork starts with the unit's context, and its first request reads that context as cached input from the source thread's prefix.
- **A changed prompt re-points instead of retiring.** Retiring a reviewer on every prompt change would throw away the unit context the design keeps it for; `_resume_params` already sends the roster's file on every resume, so pointing the roster at the new copy is enough.
- **A replaced thread's lease count goes to 0.** The new thread holds none of the unit's turns, so its next review counts as the first.
- **No trial, no arm split.** The trial that divided phases between the pool and a unit's own workers by a hash of unit and phase ended with the user's rule; `arm`, `split`, `REVIEW_LEASE`, `--fresh`, the phase-end `end-lease` reset and `health`'s stale-lease ending went with it, and `/review:direct` lost its hourly health timer and the dailies report.
- **Allocation is a script with no queue, and each production has its own pool.** The user's rules: a review never waits in line (a new reviewer is forked instead), no tokens go to allocation, and each showrunner has its own reviewer pool.
- **One mesh directory per pool lifetime.** With one shared `mesh/`, a fork that finishes after `stop` writes into the roster the next lifetime reads.
- **Names are never reused.** A process still attached to a retired thread can point a reused name back at it.
- **A failed `stop` keeps the `stopping` marker.** Clearing it would leave no later `stop` able to reach the ended lifetime's server.
- **The launcher leaves its pass open on a signal.** If it closed the pass, the unit director's cancel call would fail on a closed pass.
- **The pool prompt says `As in your standing instructions.` and names no file.** A reviewer holds the text it was last pointed at, which `config/review/code.md` may no longer match.
- **Cost comes from the usage store.** It already holds every request with its model, time and compactions for both machines, so reading rollout files through `scripts/usage/rollouts.py` was ruled out.
- **The pooled UI reviewer's standing prompt holds the rule texts.** A prompt that names `commands/ux_eval.md` and the rule files has them read again on every turn; with the texts standing, the turn reads none and cost about 22% fewer weighted tokens (Numbers).
- **Design decisions come from the production's repository, never from the guide.** It is the file the helper's `ux_eval.md` reads; taking it from the guide would make the helper and the pool judge against different exceptions.

## Risks

| Risk | What contains it |
| --- | --- |
| Standing context cost per request | Prefix tokens are re-billed on every request. Standing prompts are capped (7 KB code), the launcher compacts a thread above the configured limit, and the report's cost tables and lease-ordinal rows show what a review costs as a thread ages. |
| One unit's details in another unit's review | A reviewer belongs to one unit for good and never serves another. Its thread holds only that unit's reviews. |
| Stale context | The prompt says to re-read the tree each review and checks the head hash first. A changed prompt re-points every reviewer. |
| One point of failure | The request path is the unit's own `review.sh` plus one app-server. A dead server is restarted on the next call, and its threads resume. If the pool cannot open a thread at all, `review.sh --pool` exits `error`, and the unit director retries on its own worker with `--to` under <WorkerLaunchFailure/>. |
| Sandbox reads of every worktree | Codex `readOnly` reads the whole filesystem, so it needs no grant per worktree. It also cannot stop a reviewer reading a sibling unit's tree. All trees are the user's own, so this is a correctness risk, not a privacy one. The prompt names the worktree and head hash. |
| Writer independence | A pool reviewer never wrote what it reads. |

## History

- **2026-10-10 to 2026-10-13: built as a trial** (`docs/plans/proposal-review-unit.md`, removed once built). Leases were per phase and reset with `thread/revert` at phase end; a hash of unit and phase sent half of the phases to the pool and half to the unit's own workers, and the report compared the two. The four directors asked then (hana, organon, widget, trunk) wanted no queue in front of a review, findings in the unit's own files with triage kept by its director, the guides and rulings kept, and each diff and tree belief forgotten; the user's rule replaced queueing with forking, and the review memory holds what they asked to keep.
- **2026-10-16: permanent reviewers** (`docs/plans/build-followups-review-pool-permanent.md`, removed once built). Phase 1 made leases per unit and added `migrate`; Phase 2 removed the arm, the split and the resets, and added `retire` and re-pointing; Phase 3 removed `health`, the director's timer and dailies report, and the report's arm comparison.
- **2026-10-19: no review unit** (user: "each individual reviewer needs to manage it's own reviews"). `/review:direct`, the review unit, its merge lines and review memory (`unit-memory`, `memory/`) were removed; allocate primes the pool.

## Tests

- `scripts/direct/test_review_pool.py` (56 tests) runs the pool CLI as subprocesses against a stub mesh named by `REVIEW_POOL_MESH`: primes and re-pointing (idle reviewers at prime, a busy one at its next allocate, a refused re-point keeping the old hash), allocation across phases of one unit, forks from the unit's reviewer and the template fallback, name issue, `release --thread-id` and the replaced-thread lease count, `retire`, `migrate` on a copy of the tool-based-ui layout, an old `split` key, `end-lease` ending nothing and `health` gone, dead reservations, `stop` and its recovery, a stop that lands during a prime, fork or retire, priming on allocate from the base prompt and the UX rules, and the adoption marker. `UIStandingPromptTests` pins `ui.md`'s verdict line, its table header and `measure_text.py`, and the absence of the old instructions to follow `ux_eval.md` and read a rule's full text.
- `scripts/direct/test_review_launcher.py` (65 tests) covers `review.sh`: the pool path with no reset, the prompt sent as written on a first turn, the slot file, turn records of both launch paths and all four statuses, signals, a replaced thread, the tree check, the argument refusals, shots for `ui`, the `ux` row count, no pass for lens `ux`, and the assigned and fresh paths.
- `scripts/direct/test_review_pool_report.py` (73 tests) runs the report as subprocesses. `populate_usage_store` collects fixture rollouts into a fixture store, and `USAGE_STATE_DIR` points the script at it. It covers request attribution, every incomplete reason, compaction counts, outcome lines, records of both launch paths counted together, the quality table with merge holds, each merge-hold match case and the `merge holds unmatched` warning, plan normalization, Mac rows and histories, an absent and an unreadable store, the Mac source warning, and `--shadow` with its `Machine:` line. These tests call the collector's `SCHEMA`, `migrate_store`, `read_file`, `save_measurement` and `RoleResolver`; a rename in `scripts/usage/collect.py` breaks them. The fixtures prefix the collector's request key with the file path.
- `scripts/agents/test_codex_mesh.py`, with a fake app-server, covers `prime`, `fork`, `set-instructions`, `reset`, the three `follow` flags, and standing instructions restored on resume, fork, compact and a replaced thread.
- `scripts/production/test_review_unit_phase.py` (3 tests): the standing phase reads as `1 of 1`, running; a resume adds no run and no phase; shutdown finishes the run while the plan heading stays `todo`.
- `scripts/direct/test_progress_history.py` covers the `UX review` activity's time.
