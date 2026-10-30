# Long-lived Codex workers and held Astra roles

Code reviewers belong to the unit ledger. `assign.sh --role review` resumes
`<worktree>-review`; concurrent work uses `<worktree>-review2`. Both are
read-only and persist across phases and plans. The first dispatch adopts the
unit's leased thread before ordinary allocation. A busy lease holds its slot
until it can move. After the move, resume, queued messages, idle checks and
restart use only the unit transport; the former pool state is an unread archive.
`review.sh --pool code` and `--lease` are ignored for one release with one
stderr line. `--pool ui` refuses; the fresh Claude helper in `<UXReview/>`
remains the design check.

## What it is

A unit keeps its worker threads across phases and plans. `assign.sh` selects
an idle worker by role, resumes it, and claims it for the dispatch. Code review
uses the unit's read-only `review` workers. Writers keep their writing roles.
Each follow compacts when the recorded context exceeds the configured limit.
Phase cleanup ends turns and keeps threads. Unit pause, move and end use the
worker ledger. Usage reports compare checkpointed phases through the usage store.

## Unit code reviewers

Code reviewers use the ordinary unit worker ledger. The first own dispatch
adopts the unit's leased code thread before opening any new reviewer. A busy
lease holds `review`, so concurrent work uses `review2`. A later dispatch
adopts the first thread when its turn ends.

| Worker slot | Role | Access | Lifetime |
| --- | --- | --- | --- |
| `review` | `review` | Read only | Keep the thread across phases and restarts. |
| `review2` (and later numbers) | `review` | Read only | Keep each concurrent reviewer's thread. |

A reviewer's address is `<worktree>-review` or `<worktree>-review2`. Its record
keeps `slot: review` or `slot: review2` apart from `role: review`. Its shown job
uses the `code_reviewer` key from `config/names.toml`. Reviewers persist, pause,
move and end by the same ledger operations as writers. `workers.py resolve
--role review` reuses an idle reviewer; it opens a read-only thread only when no
reviewer can take the work.

One renderer makes every worker's thread title and message prefix:
`workers.shown_name`. A unit run gives `<production> · <unit> · <job>[ <n>]`,
with the unit's `-unit` suffix removed. The job label comes from
`config/names.toml`: `implementer`, `tester`, `repair_worker` or
`code_reviewer` for the `impl`, `test`, `fix` and `review` roles, and `worker`
for any other role. The second holder of a role gets the number 2, and so on.
A held design, or a run outside a production, takes its placement from the
roster (`roster.mesh_title`). Each turn sets the title again, so it follows the
current assignment.

### Transfer the leased code reviewers

Read the proposed transfer first. Name each destination run explicitly:

```sh
python3 scripts/direct/workers.py transfer-reviewers --production <slug> \
  --session-dir <unit-run> --session-dir <another-unit-run>
```

The default prints `preview: would migrate` and writes nothing. It also lists
busy reviewers as deferred, conflicts, and each skipped UI or unleased reviewer.
Use `--apply` for an explicit move. Each applied result names the destination
owner and the checked thread id. The command exits non-zero while any leased
code reviewer is deferred or in conflict.

The move keeps the thread id, account home, account-move marker, standing
instructions and queued messages. It rejects an inbox with invalid queue fields
and names its path. Both inboxes keep their bytes on that refusal. It fences
the old transport, carries queued server input and releases the old writer,
then registers the unit worker. The journal and inbox receipts let a rerun
finish an interrupted move without copying messages twice. The unit receipt
ends archive reads after the durable lease removal. The command prints the
archive path; moved workers do not read it. This phase does not run a live apply.

## How it works

### Worker lifecycle in `scripts/agents/codex_mesh.py`

Each run has one detached `codex app-server --listen ws://127.0.0.1:<port>`. Each worker is a named, non-ephemeral thread on it.

**Server environment.** `_start_server` gives the server the caller's environment with `CODEX_HOME` set to the workers home. When `--session-dir` is a child of a unit run root (`RUN_ROOTS`, `/tmp/claude/unit` and `/tmp/claude/delegate`), it also sets `UNIT_RUN_DIR` to that folder and `TMPDIR` to its `tmp/` folder, replacing any values the caller carries. Worker processes inherit it: `verify.sh` takes the cargo token on that board, and the berth hook (`scripts/hooks/pre-tool-use-berth.py`) checks that run's open phases. The caller's copy cannot be used: implement.sh exports it only after its claim step, and the claim's `compact` call starts the server when none is running. On 2026-10-17, 11 of 13 live app-servers lacked it, all three started by that `compact` call among them. A retained legacy mesh directory (`mesh/<generation>/`), and any other mesh directory outside the run roots, keeps the caller's environment unchanged: one pool server serves the turns of many units, so no single run is right for it, and a reviewer edits nothing.

For unit runs, `start`, `prime`, an explicit `follow --cwd`, and resident turn transitions pass their known cwd to `ensure_server`. Direnv's nearest `.envrc` at or above that cwd selects the dev-shell folder, so turns in worktree subfolders share the same server. `direnv status --json` must identify that file as allowed (`state.foundRC.allowed == 0`) before the server starts through `direnv exec <dev-shell folder> env <managed assignments> codex …`. The approval check does not evaluate the shell. A missing or unapproved `.envrc` selects the ordinary launch. A missing direnv, failed or timed-out probe, or unreadable status is an unknown result: it keeps a healthy recorded server and its saved dev-shell preference. Without a healthy server, an unknown result starts an ordinary server.

There is no wrapper on worker commands: the app-server passes the loaded dev-shell to all its children. The assignments after direnv restore `CODEX_HOME`, `UNIT_RUN_DIR`, `TMPDIR` and any managed held-role identity keys, so `.envrc` cannot replace them. [Direnv uses `syscall.Exec`](https://github.com/direnv/direnv/blob/v2.37.1/internal/cmd/cmd_exec.go), and `env` also executes its command in place: the recorded pid is the app-server's own pid. Plain launches retain the 30-second `SERVER_START_TIMEOUT_SECS` budget and sweep record-start window. Only dev-shell launches get `DEV_SHELL_START_TIMEOUT_SECS` (300 seconds) for cold direnv/Nix evaluation. While one starts, `mesh_server_start.json` names its pid, port, start time and dev-shell folder. The sweep discovers this temporary record under the run roots even without a recoverable log handle, and keeps that server through its dev-shell startup budget. The marker is removed after success or failure; expired markers do not extend the plain record-start window. If that launch cannot execute, exits, or times out, the failure and direnv's stderr go to `mesh_server.log`; the same start path retries with the caller's environment and records a plain server. `mesh_failed_dev_shell.json` remembers the failed folder, an input revision and a retry deadline 30 minutes after the failure was written. The revision covers `.envrc`, `flake.nix`, `flake.lock`, `shell.nix`, `default.nix` and `devenv.nix` in that folder, using each file’s nanosecond modification/change times and size, or its absence. The remembered-failure type owns both the input key and the deadline. Until the deadline, an unchanged revision makes a cwd caller reuse the healthy plain fallback and keeps restarts plain, instead of repeating the failed evaluation. Editing, creating or removing a tracked input, or reaching the deadline, permits a fresh attempt. Older failure records without the input key and deadline also permit a fresh attempt. A successful dev-shell start clears the failure record. The failure check runs under the server lock, so queued callers observe a failure saved by the preceding launch. Failed-start cleanup signals the whole process group, including evaluation children. No live worktree evaluation was measured for this change.

The server record stores `dev_shell_worktree` for a successful dev-shell launch. When a cwd caller receives a definite probe answer and the required dev-shell differs from the record (including a legacy record with no dev-shell), `ensure_server` retires the server and starts its replacement through the same path as an account or compaction-limit change. Existing turns retain the retired server until released. This also removes a previously loaded shell when its `.envrc` disappears or is no longer allowed. An unknown probe answer does not retire the server for a dev-shell mismatch. A caller without a cwd never retires a server for that reason.

`mesh_dev_shell.json` in the session folder stores the last successfully served dev-shell folder as `worktree`, independently of the current server record. When `compact`, `serve`, `fork`, or another cwd-free caller must restart after idle-stop, it rechecks that folder's approval and uses the same dev-shell when still allowed. A definite answer selecting the ordinary environment clears this preference; an unknown answer preserves it for a later retry. A pre-existing server record can also supply the folder when the preference has not yet been written. Review pools and other shared mesh folders never load a worktree's dev-shell.

The berth hook takes the session directory from `UNIT_RUN_DIR` when it names a folder that exists. When the variable is unset or its folder is gone, it reads the director's run marker (`unit_runs.marker`), so a stale variable never lets an edit through unchecked. The marker names one run, so a worker in a director's side run would otherwise check the main run's reservations.

The verbs:

| Verb | Signature | Behavior |
| --- | --- | --- |
| `start` | `--session-dir --name [--role impl\|test\|fix\|review] [--lens] --cwd --prompt-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--sandbox danger-full-access] [--resident]` | Opens a thread and blocks until its turn ends. `--role` is optional, and roleless threads such as ask-a-friend friends leave it out. A given role, with `--lens` when passed, is appended to `agents.jsonl` for the new thread. |
| `follow` | `--session-dir --to --role (required) [--lens] [--claim-pid] --message-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--cwd] [--read-only] [--image …]` | Runs one more turn on a finished thread. If the old server is gone, it resumes the thread on a new one. `--cwd`, `--read-only` and `--image` apply to that turn only and also apply to unit-owned reviewers; without them the request is the worker's usual one. |
| `prime`, `fork`, `reset` | see `docs/as-built/review-pool.md` | Create a read-only reviewer template with standing instructions, fork a named reviewer from it, and remove every turn from an idle reviewer. Only the retained legacy allocator calls them. |
| `compact` | `--session-dir --to` | Exits 2 for a worker that is running or busy, a worker with no recorded context size, and a worker whose size is `not_measured_since_compaction` (`size not measured since its last compaction; compact refused`). Otherwise it reads the entry's `revision`, resumes the thread, waits up to 60 s (`COMPACT_IDLE_WAIT_SECS`) for a turn still live on it to end, exits 3 (`COMPACT_TURN_STILL_LIVE`) if one stays live, and calls `thread/compact/start`. It waits until the compaction turn has a `thread/compacted` notice or a `contextCompaction` item, a token-usage update and a `turn/completed`. It then writes the new size under `RosterUnchanged(thread_id, revision)`, so a turn that wrote the entry in between keeps its record. A size above 0 prints `compacted <worker>: <before> -> <after> tokens`; a compaction turn that reports 0 stores `not_measured_since_compaction` and prints `compacted <worker>: <before> tokens, size not measured until its next turn`. A compaction that has not finished in 300 s (`CompactionTimedOut`) is interrupted with `turn/interrupt` and the thread is read until idle, up to the same 60 s: idle is exit 1 with `…; interrupted the compaction turn, the worker is idle and not compacted`, so a follow-up can still be sent; a turn still live is exit 3. |
| `can-follow` | `--session-dir --to --role [--lens] [--claim-pid]` | Refuses a worker that cannot take a follow-up. With `--claim-pid`, it claims the worker under the roster `flock`: it sets `status: starting`, `launcher_pid`, `previous_status`, the new `role`, and `previous_role` (the role the worker held before), and appends the role and lens to the role log `agents.jsonl` (`docs/as-built/usage-ledger.md`). |
| `release-follow` | `--session-dir --to --claim-pid` | Releases a claim that never started a turn. It restores `previous_role`, or removes `role` if the worker had none, returns the worker to its previous status, and appends the restored role to `agents.jsonl`. A follow that did run keeps the role it was claimed with, so after a completed review the worker's role stays `review`. |
| `end` | `--session-dir --to` | Clears the worker's pending queue, writes its end marker and sets the worker's roster `status` to `"ended"`. |
| `stop` | `--session-dir [--pause]` | Sets every roster worker to `"ended"` and stops the current server and every retired one. With `--pause` (a shutdown stop, `docs/as-built/shutdown-and-restart.md`) it instead sets every non-ended worker to the followable `"paused"`, with `paused_from` and `paused_turn`, after dropping its queued messages and interrupting a live turn. |
| `reopen` | `--session-dir --to` | Returns an `ended` worker, or a `running`/`starting` one whose launcher is dead and whose server is gone, to `"paused"` on its recorded thread. Exits 2 with one stderr line when the thread id is missing or a launcher or server is alive. |
| `sweep` | `[--root] [--stop]` | Gives a verdict on every app-server listening on `ws://` (see Cleanup). |
| `idle-stop` | `--session-dir --pid --port` | The server's own watcher calls this. It stops the server, or touches its record if something still uses it. |
| `list` | `--session-dir` | Prints the roster. |

A held design role (a roster entry with `held_design_id`, `docs/as-built/long-lived-workers.md#lasting-roles`) changes only through `scripts/guilds/roles.py`: `start`, `prime` over it, `fork` from it, `reopen`, `set-instructions`, raw `stop` and `stop --pause` refuse it, `follow` takes only its `authorized_prompt` (or an account-move continuation), and `send` queues the text in the role store.

`follow` and `start` both send `model` on every `turn/start`. A `review.sh` follow-up therefore runs the configured model of the role the worker was reassigned to. An `implement.sh --to` follow-up runs the model in the worker's identity file, so a registry row changed after the worker opened does not strand it (rca of 2026-10-23).

**Roster fields (`mesh_roster.json`).** Each entry has:

- `thread_id`.
- `status`: `starting`, `running`, `done`, `failed`, `paused`, `waiting_capacity`, `capacity_exhausted`, or the terminal `ended`. `done`, `failed` and `paused` are followable (`FOLLOWABLE_STATES`); a follow clears `paused_from` and `paused_turn`.
- `role`: `impl`, `test`, `fix` or `review`, or absent for a roleless thread.
- `context_tokens`: `tokenUsage.last.inputTokens` from the turn's latest `thread/tokenUsage/updated`, or `"not_measured_since_compaction"` after a compaction turn that reported 0, until the worker's next turn reports a size.
- `revision`: raised by one on every write inside the roster lock; an entry without one reads as 0. `_update_roster_if_expected` writes only when the entry still matches what the writer expects (still on its thread, absent, ended on its thread, or at an unchanged revision), so a command still at work for an earlier thread, or a compaction that ends late, cannot replace a newer record (`docs/as-built/usage-ledger.md`, Worker roster guards).
- During a claim only: `previous_role` and `previous_status`.
- For a pooled reviewer, its template or a held design role: `instructions_file`, whose text is sent again as `developerInstructions` on every resume and fork.
- `access_policy`, which decides the sandbox (`_worker_sandbox`, `_resume_params`): `{"kind": "ReadOnlyReviewer"}` (written by `prime` and `fork`, and for a held reviewer) gives `read-only`; `PhaseBoundDesigner` (a held designer) gives its `launch_sandbox`. An entry with no policy takes `--sandbox` or the default `danger-full-access`; an entry with `instructions_file` and no policy refuses with `recovery required`. Access never comes from the instructions text.

The read-only roster reader `scripts/message/roster.py` (`docs/as-built/session-roster.md`) reads `thread_id`, `status`, `role`, `lens`, `launcher_pid` and `cwd`, and leaves `ended` workers out. A `review` worker with a `lens` field shows as `review (<lens>)`, but no launcher writes `lens` into the mesh roster. `codex_mesh.py --lens` records the lens in `agents.jsonl` only; it also appears in artifact names and in the progress and findings records.

**Overflow press-on.** When a turn ends with `codexErrorInfo: contextWindowExceeded`, `start` or `follow` does three things in the same call:

1. Posts a board status line: `context window filled; sent "Continue where you stopped."`.
2. Starts a continuation turn with that text.
3. Sets a guard flag.

The flag is cleared by any `item/completed` from the worker's own work, but not by a `userMessage` or `contextCompaction` item. A second overflow while the flag is still set stops the worker with `context window filled twice in a row without progress; automatic continuation stopped`. Overflows that have work between them get one continuation each, with no limit.

### Launchers

**`scripts/direct/implement.sh [--to <worker>] <session_dir> [working_dir] [prompt_file] [task] [role_description] <pass_kind> [pass_activity] [fix_pass] <team_role> [mesh_prefix]`**

- Without `--to`, it opens a new worker named by `worker_name.sh`. After the turn it writes the worker's identity file.
- With `--to`, it follows the open worker after these steps, all under `<session>/impl_worker_claim.lock`:
  1. Checks that the identity file `impl_worker_<team_role>.json` (`impl_worker_fix.json` for the `-fix` repair worker, which runs in slot `impl`; `worker_state.worker_record_path` owns the rule) equals `{name, slot, family, worker_id}` for this launch, with `worker_id` the thread id. `model` is the model recorded when the worker opened; the follow-up runs that model, not the registry's current row. A mismatch fails with `has no matching durable worker identity`. A missing file with a roster thread for the name is not a mismatch: the claim writes the identity from the roster and continues. That resumes a reopened worker, and a fix worker of a run from before 2026-10-17, whose record the impl worker's replaced. A worker recorded under another slot is still refused.
  2. Refuses a worker that another live launcher has claimed (`impl_claim_<slot>.json`), or whose status is `implementing` while its roster `launcher_pid` is alive. A claim whose launcher is dead is dropped, and an `implementing` status with no claim file and no live roster launcher is treated the same way: it writes `error` and continues.
  3. If the roster's `context_tokens` is an integer greater than `UNIT_COMPACT_ABOVE_TOKENS`, runs `codex_mesh.py compact`; a `not_measured_since_compaction` size skips this step, so the worker gets its follow-up with no second compaction. On success it posts `launcher: compacted …`. On exit 3 (a turn is still live on the thread) it posts `launcher: <worker> still has a live turn after compaction: <detail>; follow-up not sent` and exits 2, naming `codex_mesh.py end --to <worker>` and a fresh worker as the remedy; `review.sh` does the same. On any other failure it posts `launcher: compaction failed for <worker>: <detail>; continuing follow-up` and continues.
  4. Claims the worker with `can-follow --role <pass_kind>`.
- On exit, it releases the claim.
- After launch it waits for the worker's record (with `--to` on Codex, the turn record) for up to `UNIT_WORKER_WAIT_ATTEMPTS` (30) checks `UNIT_WORKER_POLL_SECS` (1 s) apart, stopping early when the launcher exits. Tests shorten the interval and raise the count.
- It reads the threshold from `config/direct.conf` (`UNIT_CONFIG` can override the path). A missing, non-numeric or non-positive value exits 2 with an error naming the key.
- A worker assigned a different role keeps its slot as the 9th argument and changes only the task and pass-kind words.

**Code review dispatch.** Use `assign.sh --role review --session-dir <run>
--phase <phase> --prompt <file> --cwd <worktree>`. Each broad-review lens has
its own dispatch; closure review omits the lens. The returned handle names its
status, log and summary. The allocator uses `review`, then `review2` when the
first worker is busy. Both threads stay read-only.

`review.sh` remains a compatibility launcher. With no recipient it resolves
the unit's own reviewer. `--to` follows an explicit worker and `--slot` names
an explicit extra worker. Both use read-only access. `--pool code` and `--lease`
are ignored with one stderr line; `--pool ui` refuses and points to the fresh
Claude helper in `<UXReview/>`. It never calls the old allocator.

Every review attempt records `arm: own`. Records under
`review_turns/<run>/<started>-<pid>-<sequence>.json` keep separate attempts from
one launcher. History reads both earlier `arm: pool` and current `arm: own`
records. No recorded review differs from a recorded review with zero findings.

**Compatibility launcher tree guard.** Before the turn, `review.sh` runs `review_tree.py baseline`, which records HEAD and, for each path `git status` names (untracked, non-ignored files included), its HEAD, index and worktree entries: mode and blob id, a symlink's target standing for its content.

After the turn `review_tree.py changes` checks every path that was dirty before or after, or that HEAD moved under, and lists each one whose entries differ. When two or more phases are open in the session and the review names its phase with `--phase`, a changed path that another open phase's `phases/<phase>/files` list holds is that phase's writer at work: the launcher log notes it and the check leaves it out. A path the reviewed phase holds, or no phase holds, stays the review's. Any path left makes the review status `error`, prints `ERROR: review worker <worker> changed the working tree: <paths>`, posts `blocked` on the board, and keeps the findings file. An early review, which starts while the writer is still running, takes this snapshot when its early-ready sentinel appears.

### Choosing a worker for a review

The rules live in `commands/unit/direct.md` `<LongLivedWorkers/>`, `docs/direct/dual_review.md` (`<TeamReview/>` and `<ReviewPromptContract/>`), `docs/direct/run_phase_review.md`, `docs/direct/write_prompt_contract.md` (`<RoleReassignment/>`) and `commands/plan/phase_review.md`:

- Each lens dispatches the unit's own read-only reviewer with `assign.sh --role
  review`. The allocator reuses a thread before opening the next concurrent slot.
- Only the adversary lens starts early. Keep its dispatch handle and selected
  slot for Stage 2 and cancellation. Closure review uses the same role without
  a lens.
- Each prompt tells the reviewer to read the tree again, names the expected HEAD,
  and keeps `config/review/code.md` as its standing instructions.
- Each prompt carries the Three Gods sentence and the Type Design Contract.
  Compaction can remove earlier text, so later dispatches repeat them.

`scripts/direct/progress_history.py` gives each extra worker (`review2`, `fix2`, …) its own `Agent N` number after `impl` and `test`, in the order workers first appear: a `- **Agent N**` line in every report, and a round-table column under `progress --tables`. A worker appears when it records a pass or receives a board handoff. The original `review` worker keeps its earlier rule: it shows only while a displayed stage assigns it.

### Cleanup

- **Phase end.** `<PhaseCleanup/>` runs `remove_workers.py --session-dir`. It leaves Codex threads open and preserves Claude workers held by the unit ledger or a live unit director. It does not launch server reclamation.
- **Run end.** `scripts/direct/end_session.sh` acts on every run folder the ending director holds, not only the one its marker names: a director runs independent phases side by side, each in its own run folder. `unit_runs.py held <session id>` lists them, the marker's run first, then every folder under the run roots whose `unit_runs.director(run)` is the session. It is a plain scan: reading the director of all 224 live folders took about 25 ms (2026-10-17). The marker's run is kept even when the scan fails. A run of another director is never touched. The steps, in order:
  1. `roles.py unbind-run <run>` for each held run, which unbinds every held design role bound to it and leaves its thread, prompts and replies (`docs/as-built/long-lived-workers.md#lasting-roles`); a failure stops the script before any other step
  2. one `remove_workers.py` call with a `--session-dir` per held run
  3. `codex_mesh.py stop --pause` for each held run with `mesh_roster.json`, `mesh_server.json` or `mesh_retired.json`. A live unit director's workers, queues and servers remain untouched. Otherwise pause preserves pending messages, owed continuations, reservations and detached commands before stopping only that run's recorded servers.
  4. one `remove_run_scratch.py --tracked` call for every held run: it removes `verify_cache/` and git checkouts and cargo targets at any depth, without descending into what it removes or following a link. It keeps any checkout or tracked scratch folder containing the working directory of a live detached command belonging to a worker that is not retired. A linked worktree goes through `git worktree remove --force --force` in its source repo, or is deleted and its source pruned; ledgers, logs, diffs, json and the run folder stay.
  5. removal of the session's own run-active marker. Judging another session's marker is separate: `unit_runs.py removal-check` keeps the former folder while a marker's owner is live or unknown.

  Steps 1 to 4 run even with no marker, for runs the session still holds. Steps 2 to 4 each continue after a failure. Run end does not launch a sweep. Tests point the scan at a temp folder with `UNIT_RUN_ROOT` and the marker with `UNIT_ACTIVE_DIR`.
- **Sweep.** It first decides who owns each `ws://` app-server: the current server of a run, the current server of a held design role, a retired server of a run, an unowned server whose log names its run, or an unowned server with no log. Alongside the run folders selected by `--root`, it reads the durable design mesh folders returned by `roles.durable_meshes()` (folders with `mesh_roster.json`). A matching current server record belongs to a held design role only while its roster has an entry with `held_design_id`; a released role or deleted folder gets the usual ownership rules. It then judges whether anything still needs the server:
  - Any connected client means in use.
  - For a current run server, a run-active marker or a running worker means in use.
  - A retired server is unused unless a conversation is mid-turn or a loaded thread has queued or unreadable work.
  - An unowned server is in use during the record-start window, while a conversation is mid-turn, or when a loaded thread has queued or unreadable work.
  - Current run servers and held design servers share the 30-minute quiet window (`SERVER_IDLE_SECS`): a live worker launcher, a server under 30 minutes old, a mesh folder changed in the last 30 minutes, or a conversation mid-turn means in use.

  It reads `thread/queue/list` for loaded threads; queued submissions or unreadable queue state keep the server. It prints `pid  port  verdict  reason  owner` with verdict `in use`, `unused`, `stopped` or `kept`. With `--stop`, it judges a second time under the owner's mesh folder server lock before stopping a server. Ordinary idle-stop and retired-server reclamation can free an unused server while the director lives; the next dispatch resumes the same worker name and thread. Servers listening on `--listen unix://` are never touched. Each server also has its own watcher, which wakes every 300 s and runs `idle-stop` once the server record has gone 30 minutes untouched. If a server fails to start, its process is terminated and its record dropped.

`scripts/direct/workers.py` stores unit ownership under
`~/.local/state/unit-workers/<production>/<unit>.json` (`UNIT_WORKERS_STATE`
selects a private root). On first ledger binding, a production unit's run
saves explicit production and unit fields in `progress_history_state.json`.
Plan headers accept both Production forms and resolve former production names.
Later reads use the saved identity, so moving a worktree does not change owners.
`rename.py unit` also repoints run worktree and plan paths and worker roster
launch paths, while keeping paused threads and Codex homes. For older moves,
use `rename.py repair-workers <production> [--dry-run]`. It matches current
Units rows with saved run or worker paths even without journaled moves, and
skips ambiguous unit matches; see `production-rename.md`.

### Measurement: `scripts/whoami/worker_usage.py`

`worker_usage.py` is a view over the usage store (`docs/as-built/usage-ledger.md`, Worker readout). It reads no rollout, ssh, git or readings file; the hourly collector stores Codex requests from both machines, compactions, edits, checkpoints, account readings and account role changes, and the readout opens the store read-only through `usage_report.open_read_store`.

- **Modes.** `[--db PATH]` with `--before <start> <end> --after <start> <end>`, or `--daily [--t-live <time>]`. In `--daily` mode, the before window is the 7 days before T_live and the after window runs from T_live to now. T_live defaults to `2026-10-10T18:17:00Z` (`DEFAULT_T_LIVE`). `--t-live` is accepted only with `--daily`. A bad window exits 2; a store it cannot open or query exits 1 with one `worker_usage.py:` line.
- **Weights.** Dated per-model weights from `scripts/usage/weights.py`, applied as grouped SQL through `weights.sql_weights`. A window is cut into pieces at each dated weight entry that starts inside it for a model with Codex turns there (`After, from 2026-10-10 21:35Z` for `gpt-6.1-sol`). `Weighting:` heads the report with each weight version used (`<model>[@<start>]: 1 / <cached> / <output>`); `Unweighted:` names each model with no entry and its turn count.
- **Table, per window piece.**
  - threads opened
  - weighted usage
  - share of usage before each thread's first edit, over threads that ever edited
  - compactions and compactions per active thread (a thread with any opening, turn, edit or compaction in the piece)
  - checkpointed phases
  - account switches per week
  - credits used per day
- **Verdict line.** `Weighted usage per checkpointed phase: before X; after Y; up|down N.N%`, or `unchanged` when the change is under 0.05%. It compares the whole before window with the last after piece. A parenthesis notes `different weights: compare roughly`, unweighted turns left out of each side, and `Mac as of <time>` or `Mac never read in full` when the store's Mac Codex source is not `ready`. The `Sources` block (`usage_report.sources`, in UTC) sits above the verdict.
- **Unavailable states.**
  - Switches show `—` when the window has no readings.
  - Credits show `no credit history` or `insufficient credit history`.
  - A window with no checkpoints shows `unavailable`.
  - A zero before-value gives `change unavailable`.
- **Switch counting.** A switch is a change of the account holding the `workers` role (`accounts.CODEX_ROLE`) in the store's `account_role_changes` (`store_switch_history`). Before the stored role history begins, a change of `codex …` account between consecutive readings still counts. The total is scaled to per week over the time the readings cover.
- **Credit counting.** Credits used is the sum, per account, of balance decreases between consecutive `account_readings` rows.
- **Credit readings.** `scripts/whoami/agent_notes.py` stores `credits` with each Codex reading, and the collector copies the readings into `account_readings`. The value is the note's `credit_balance` if it is a finite, non-negative number.
- **Edit detection (`scripts/usage/rollouts.py` `is_edit`).** The collector stores an `edits` row for each record that counts as an edit:
  - an `apply_patch` custom tool call
  - a JSON input whose `command`, `name` or `tool` is `apply_patch`
  - input starting with `apply_patch `
  - `apply_patch(` or `tools.apply_patch(` in code, outside JavaScript string literals and template text, where `${…}` expressions still count as code

### Key files

| File | Role |
| --- | --- |
| `scripts/agents/codex_mesh.py` | Worker threads, roster, compaction, overflow press-on, role claim and restore, sweep |
| `scripts/direct/implement.sh` | Writer, tester and repair dispatch; `--to` reuse with identity check, claim and compaction |
| `scripts/direct/assign.py`, `assign.sh` | Unit role allocation, persistent reviewer dispatch, read-only follow and own review records |
| `scripts/direct/review.sh` | Compatibility flags, explicit recipient follow, own allocation and tree guard |
| `scripts/direct/end_session.sh` | Unbinds held design roles, preserves a live director's workers and servers, otherwise pauses recorded runs; no sweep |
| `scripts/direct/remove_run_scratch.py` | Removes run and tracked scratch while preserving live detached commands' working directories |
| `scripts/direct/remove_workers.py` | Removes unheld Claude workers at phase end (`--session-dir`, repeatable); preserves unit workers; no sweep |
| `scripts/lib/unit_runs.py` | `held`: every run folder a director holds |
| `scripts/direct/progress_history.py` | Agent columns for extra workers |
| `scripts/whoami/worker_usage.py` | Before/after usage report over the usage store |
| `scripts/usage/weights.py` | Dated per-model weights the report applies |
| `scripts/usage/rollouts.py` | `is_edit`, used by the collector |
| `scripts/whoami/agent_notes.py` | `credits` on Codex readings |
| `config/direct.conf` | `UNIT_COMPACT_ABOVE_TOKENS=100000` |
| `commands/unit/direct.md` | `<LongLivedWorkers/>`, referenced from `<LaunchImplementation/>`, `<FixDispatch/>`, `<PhaseCleanup/>` and `<CompactionContract/>`; early review worker (`EARLY_REVIEW_SLOT`) |
| `docs/direct/launch_implementation.md` | `codex_mesh.py list`, then `--to` for every slot with an open worker |
| `docs/direct/dual_review.md`, `docs/direct/run_phase_review.md`, `commands/plan/phase_review.md` | Review worker selection and `--to`/`--slot` calls |
| `docs/direct/write_prompt_contract.md` | `<RoleReassignment/>`; Three Gods and Type Design in every prompt |

## Invariants

- Codex workers open once per run and end only at `end_session.sh`. A held design role is not a run worker: run end unbinds it and never ends its thread. Phase cleanup never ends a Codex thread. A new worker opens only when no open worker can take the work.
- Claude workers keep their per-phase lifecycle; this design covers Codex workers only.
- Only the unit director starts a finished worker's next turn through `implement.sh --to <worker>` or `review.sh --to <worker>`. A peer message sent with `send` is held for that turn, which receives it after its dispatch prompt.
- Compaction, the worker claim and the identity check run under one lock (`impl_worker_claim.lock`). Compaction is best-effort: a failure posts one board line and never blocks the follow-up.
- A worker's identity file is anchored to the implementation model, whichever launcher opened the worker.
- A assigned review claims its worker before writing any artifact, and fails if the working tree changed in any of HEAD, status, tracked diff or untracked content.
- Review artifacts are keyed by pass and lens; the recorder pass is keyed by the slot that ran it and a fresh Claude design check records no code-review pass.
- Every prompt to a worker, follow-ups included, repeats the Three Gods sentence and the Type Design Contract, verbatim or by pointer.
- Only the worker's own work items clear the overflow guard; `userMessage` and `contextCompaction` items never do.
- `sweep` treats every doubt as in use, judges a second time under the owner's lock before stopping, and never touches the `unix://` daemon. A sweep failure never stops worker cleanup.
- `worker_usage.py` reads only the usage store and writes nothing. It prints an explicit unavailable state where a value has no data, and its verdict names unweighted turns left out and the Mac's freshness when the Mac was not read in full.
- A roster writer that changes an entry in place advances `revision`; `compact` writes its size only when the revision it read is unchanged.

## Calibration / gotchas

- **Compaction threshold**: 100,000 tokens of a 258,400-token window. The comparison is strictly greater than the threshold, on the roster's last `context_tokens` when it is an integer. The compaction replay (`scripts/usage/compaction_replay.py`, `docs/as-built/usage-ledger.md`) found every higher limit used more weighted tokens, so the threshold stays 100,000.
- **Weights**: dated per model in `scripts/usage/weights.py`. `gpt-5.6-sol` is 1 / 0.1 / 8 for uncached input, cached input and output; `gpt-6.1-sol` is 1 / 0.05 / 5 from 2026-10-10T21:35Z.
- **Idle rule**: 30 minutes (`SERVER_IDLE_SECS = 1800`). The watcher wakes every 300 s.
- **Slot patterns**: `--slot` must match `^[A-Za-z][A-Za-z0-9_]{0,31}$`. A slot derived from `--to` must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`.
- **T_live**: 2026-10-10T18:17:00Z. The before window is the 7 days before it.
- The real app-server emits an `item/completed` for the submitted `userMessage` on every `turn/start`, so the continuation's own message must not count as progress. The fake server in the tests emits it too.
- Compaction is finished at the compaction turn's `turn/completed`, not at `thread/compacted`. Either `thread/compacted` or a `contextCompaction` item identifies that turn.
- A Codex compaction turn reports 0 input tokens, so a launcher's compaction line reads `compacted <worker>: <before> tokens, size not measured until its next turn` and the roster holds `not_measured_since_compaction` until the worker's next turn. Storing 0 would hide the size, and keeping the size from before would compact the worker before every follow-up.
- Real Codex edits arrive as `exec` custom tool calls whose JavaScript calls `tools.apply_patch(`, not as `apply_patch` tool calls.
- A run with no run-active marker, such as ask-a-friend friends or a Codex-hosted director, keeps its server only while there is a client, a launcher, a running worker, or activity in the last 30 minutes. A stopped server comes back on the next use, and its threads resume.
- `--to` takes the slot from the last hyphen segment of the worker name, so `--slot` refuses hyphens.
- A worker opened by `review.sh` must record its identity with the implementation model, not the review model, or `implement.sh --to` refuses it.
- The prior role is restored only when a claim is released before its turn ran. A worker that completed a review keeps `role: review` until its next claim.

## Why

- **Open once.** Before this design, each phase opened new `impl` and `test` threads and every review was a new thread, so every task paid a worker's startup cost. A worker that stays open pays that cost once per run.
- **Compact between tasks.** Startup turned out to be a small share of usage. Over 1,437 natedev threads, usage before the first edit was 19% of uncached input, 4% of cached input and 10% of output: about 6% of weighted usage. Most usage is cached context that long threads read again on every call. A long-lived worker saves its startup but carries its context, so frequent compaction is what keeps the saving.
- **Codex workers only.** The cost being addressed is Codex worker startup. Claude workers are the fallback family and keep their existing lifecycle.
- **Read-only unit reviewers.** Code review uses the unit's own role and read-only sandbox. Writers keep their roles. The fresh Claude helper remains the design check.
- **Verdict per checkpointed phase.** The amount of work changes from day to day, so usage per day would not compare the two windows. Account switches are too rare for a verdict: the baseline had 2 in about five days, too few to show a change within a week. They are reported alongside the verdict.
- **Hyphens refused in `--slot`.** `--to` derives the slot from the worker name, so refusing hyphens is simpler than resolving a hyphenated slot from its recorded identity.
- **Second-overflow error kept.** It stops a worker that fills its context again and again without doing work, so press-on cannot loop forever.
- **`tools.apply_patch(` kept in edit detection.** That call is how real edits appear in rollouts. Detection skips mentions inside strings, so a quoted mention does not count as an edit.

## Lasting roles

Each design has one lasting Astra designer and at most one lasting Astra reviewer.
One unit director or showrunner holds the pair and resumes the same threads
across rounds, phases, runs, restarts and account moves.

`scripts/guilds/roles.py` keeps one designer and one reviewer slot per design.

**Store.** Root `GUILD_ROLES_STATE`, else `~/.local/state/guilds/roles/`, on the holder's host. One directory per UUID `design_id`:

| Path | Holds |
| --- | --- |
| `design.json` | schema 1, placement, `repository_id`, checkout (`repository`), absolute `document`, area, `created_at_utc`, tagged holder and binding, both role slots (`role`, `name`, tagged lifecycle, `assignment_claim`) |
| `lock` | the design lock (`fcntl.flock`) |
| `mesh/` | the role's mesh: `mesh_roster.json`, `mesh_server.json`, `guild_context.json`, `serving_home.json`, `<name>.pending.json`, `<name>.account-move.json` |
| `prompts/<role>-<stamp>-<uuid>.md` | each headed prompt, exclusive create, with a `.messages.json` list of included pending IDs |
| `replies/<role>-<stamp>-<uuid>.md` | each reply, at the header's `reply_path` |
| `pending/<role>/<id>.json` | one `PendingMessage(id, source, text)` per queued message |
| `transition.json` | an incomplete rebind, unit rename, explicit end or adoption |
| `transfer.json`, `adoption.json` | the adoption fence and its completion record |

Role IDs are `<design_id>/designer` and `<design_id>/reviewer`. Instance names are `<production>-<area>-astra-designer` / `-astra-reviewer`; an adopted role keeps its old mesh name.

**Types.** Frozen dataclasses, tagged by `kind` at the JSON boundary (`../type_design.md`): `UnitHolder(production_slug, unit_name)` | `ShowrunnerHolder(production_slug)`; `DesignUnbound` | `DesignBoundToPhase(run_directory, phase, repository, output_paths)`; `PhaseBoundDesigner(binding_generation, launch_sandbox)` | `ReadOnlyReviewer` (`ThreadAccessPolicy`); `RoleNotOpened(claim)` | `RoleOpen(thread_id)` | `RolePausedForMove(thread_id)` | `RoleNeedsRecovery(thread_id, reason)` | `RoleEnded(thread_id)`; `DesignPlacement(design_id, host, generation)`; `RoleInstance`, `HeldDesign`, `PendingMessage`, `HolderReach(session_id)` | `HolderUnavailable(reason)`. Refusals raise `RoleRefused(ValueError)`.

**CLI** (exit 2 and one `roles:` stderr line on any refusal):

| Verb | Function | Does |
| --- | --- | --- |
| `open --repository <id> --document --role --production [--unit] --area [--run-directory --phase --checkout --output-path …]` | `open_role` | Reserve placement, create the slot, start one thread. `--repository` is the registered repository ID; the checkout comes from the authority. |
| `adopt … --source-mesh --name --thread-id --adoption-id` | `adopt` | Move an existing idle thread into the store. |
| `resume <instance> --prompt-file` | `resume` | Build a headed prompt, claim and dispatch one turn, print the reply path. |
| `recover <instance>` | `recover` | Prove the recorded thread idle and clear an uncertain claim. |
| `rebind <design> --expected-generation --reason [--unbind / --run-directory --phase --output-path / --production --unit / --checkout --document]` | `rebind` | Change holder, binding, checkout or document while idle; advances the generation. |
| `end <instance>` | `end` | End one role. When no role of the design stays open, it also stops the design mesh server and writes `end-server-release.json`; a failed stop leaves the role `RoleNeedsRecovery`. |
| `unbind-run <run>` | `unbind_run` | Unbind every design bound to that run. |
| `list [--json]` | `list_roles` | Default: one plain line per role with instance ID, role, lifecycle state, holder, holder session or unavailable reason, thread ID when present, and document; `no held roles` when empty, then one line per problem. `--json`: the full local roles and problems dict, with `transition.json`, move marker and failed status mapped to recovery/paused states; no network calls. |
| `register --repository --host --checkout [--inventory-complete]` | `register_repository` | Register a repository with the authority. |
| `authority <operation>` | `_authority_local` | Run one authority operation from JSON on stdin; refuses on a host that is not the authority. |

Other public functions: `queue_message`, `pending_messages`, `held_document`, `end_holder`, `check_patch`, `rename_holder`, `rename_failures`, `move_holder`, `holder_failures`, `transfer_route`, `durable_meshes`, `resolve_holder`, `resolve_model`, `access_policy`, `refresh_census`, `authority_request`.

**Ownership authority.** `config/guilds.conf` pins `[ownership] host=natedev` (override file: `GUILD_CONFIG_FILE`; local host: `GUILD_ROLES_HOST`, else the short hostname). `authority_request` runs locally on that host, else over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> … roles.py authority <op>` with a 30 s limit; failure is `RoleRefused('ownership authority unavailable: …')`. Only the authority writes `GUILD_OWNERSHIP_STATE`, else `~/.local/state/guilds/ownership/`: `repositories.json` (ID to per-host absolute checkouts and `inventory_complete`) and `placements/<sha256(repository_id \0 relative document)>.json`. Operations: `register`, `reserve`, `lookup`, `advance` (generation +1 on the placement and its aliases), `checkout` (move a host's checkout mapping), `alias`, `legacy` (record `adoption_required`), `adopt-reserve`, `adopt-commit`. Every operation except `register` refuses until `inventory_complete` is true, and refuses a host with no explicit checkout mapping or a document outside it. A placement on another host refuses: the pair always lives on its holder's host. `[roles] new_open=false` makes `open_role` and `adopt` refuse and keeps every existing identity.

**Authorization.** `_authorize` requires placement host = local host, a live holder from `resolve_holder` (unit holder: `unit_lookup.every_marked_unit` with a `LiveClaude`; showrunner holder: `production_tree.showrunner_sessions` and `sessions.running_sessions`), and `CLAUDE_CODE_SESSION_ID` equal to that session. A missing or ambiguous holder blocks dispatch but does not change the holder.

**Models.** `resolve_model(role)` sources `scripts/agents/agents_config.sh` and runs `agents_resolve "guild.<role>"`; the family must be `codex`. `config/agents.conf` has `[assignments] guild=codex` and `[guild.codex] designer=astra:xhigh`, `reviewer=astra:xhigh` (the `astra` word resolves to `gpt-6-astra`). A missing or invalid row refuses; there is no fallback model.

**Prompts.** A designer prompt is `config/guilds/designer.md` + `Use apply_patch for all repository edits. Edit only the bound output paths.` A reviewer prompt is `astra_review.review_prompt(document, "")`. `_prompt` writes the `astra_work.header_line` first (`PhaseWork` from the binding, else `StandaloneWork`), then the instructions, the holder's text and each pending message as `Pending message <id> from <source>: <text>`.

**Access.** The roster entry's `access_policy` decides the sandbox (`codex_mesh.py` `_worker_sandbox`, `_resume_params`): `ReadOnlyReviewer` gives `read-only`; `PhaseBoundDesigner` gives its `launch_sandbox` (`danger-full-access`). A legacy entry with `instructions_file` and no policy refuses with "recovery required". A designer must be bound; `_validate_binding` checks the phase resolves, the output paths stay inside the repository and are listed in the open phase's `files`, and the reservation ledger reads.

**Lifecycle as built.**

| Event | Result |
| --- | --- |
| First open or retry | Authority `reserve`, then under the design lock create `design.json` or reuse it (another holder refuses). Validate binding, resolve the model, save the prompt, write `RoleNotOpened(claim)` and `guild_context.json`. Outside the lock, `codex_mesh.py prime --held-design --held-role` starts the thread; prime refuses without the holder's claim and writes `<role>.creation-claimed` by exclusive create. Under the lock again, accept the thread only if the claim is unchanged, set `RoleOpen`, then write the census hold. A retry with a claim or an open role returns the current design. A failed launch keeps the claim. `open` on `RoleEnded` refuses: `role ended; this design identity cannot be reopened.` |
| Assignment | `resume`: recover first if the role needs recovery or has a claim; under the design lock check `RoleOpen`/`RolePausedForMove`, no `transition.json`, idle, binding valid. Save the prompt, then under design and roster locks record `authorized_prompt`, `authorized_generation`, `authorized_holder` and `access_policy` and set `assignment_claim`. Dispatch with `codex_mesh.py follow --role impl|review --message-file <prompt> --summary-file <reply>`. Success clears the claim and deletes the included pending files; failure sets `RoleNeedsRecovery` and keeps pending input. |
| New scope during a turn | Not steerable. Finish the turn, update phase files and reservation, `rebind` while idle, then resume. |
| Phase or run ends | `end_session.sh` runs `roles.py unbind-run <run>` per run before it removes workers and stops run meshes; a failure stops the script. Threads, prompts and replies stay. |
| Rebind or holder transfer | Same host only, while idle, with a reason and the expected generation. Write `transition.json`, release servers (`_release_held_server`), authority `advance`, save the new holder/binding/checkout/document and generation, remove `transition.json`, refresh `guild_context.json` and the census holds. |
| Account move | `retire_workers_servers` adds `roles.durable_meshes()` and takes the holder from `guild_context.json`. A running role gets the move marker before interruption (`_interrupt_worker_for_account_move`); an idle one has its queue parked and its thread released. `command_resume_account_move` reads the saved header and writes to its `reply_path`. `list` shows `RolePausedForMove` while the marker exists. `accounts.py` `_swap_workers` needs no held-role code. |
| Crash or uncertainty | `recover` refuses while the launcher pid lives, resumes the recorded thread on the mesh server, requires the same thread ID and `ThreadIdle`, then sets `RoleOpen` and clears the claim. A resume that cannot keep the same thread gives `RoleNeedsRecovery`; no replacement thread starts. |
| End | Holder only, idle, not during an adoption. Write `transition.json`, run `codex_mesh.py end`, then require `held_thread_end_confirmed(session_dir, thread_id)`: every recorded live server shows the thread idle or unloaded. Unconfirmed gives `RoleNeedsRecovery` with the roster end marker kept. Confirmed sets `RoleEnded` and releases the census hold. Evidence stays. |
| Retire | `retire_unit.py` `retire` calls `roles.end_holder(production, unit)` after the unit's sessions stop; only the production showrunner's session may run it; each open role must be idle. A failure leaves the unit not retired. |

**Pending messages.** `codex_mesh.py send` to a held entry calls `roles.queue_message` and prints `queued for <name>; holder adds it to the next authorized assignment`. `mesh_runs.py` queues a held role's detached result with a stable `uuid5` ID and source `mesh_runs:<run_id>`, following a `transfer_route` to the adoption destination. Nothing wakes the role. `commands/message.md` and `codex/AGENTS-messaging.md` `<HeldRoleMessages>` state the same rules for senders.

**Mesh refusals for held entries** (`codex_mesh.py`): `start`, `prime` on an existing held entry, `fork` from one, `reopen`, raw `stop --pause` (`_pause_roster`), raw `stop`, and `set-instructions` refuse with a pointer to `roles.py`. `follow` refuses a message file other than the `authorized_prompt`, except an account-move continuation. `implement.sh --to` refuses a held or fenced entry.

**Turn claim and lock order.** `_claim_follow` runs inside `_design_claim_lock` (design `lock`, then the pending file), then the roster flock: design, pending, roster, server. `_validate_held_claim` checks the role is open on the same thread, the caller is the live holder, the authorized generation and holder match, `assignment_claim` equals `authorized_prompt`, and no `transition.json` exists. It then sets `instructions_file` and `access_policy`. The claim pops `authorized_prompt`, so one authorization starts one turn. A stale launcher claim on a held entry is not taken over.

**Server environment.** `_guild_context(session_dir)` rebuilds `guild_context.json` from `design.json` and the live holder. `ensure_server` compares it with the server record's `guild_context` (and home, compact limit, sign-in); any difference runs `_release_held_server` first. That function saves the serving home, parks each queue (`_move_queue_to_pending`), requires `ThreadIdle`, releases the thread (`_release_worker_thread`) and reaps the server; uncertainty raises `RoleNeedsRecovery`. `_server_environment` for a durable mesh drops `UNIT_*`, `PLAN_DELEGATE_*`, `CLAUDE_CODE_SESSION_ID`, `GUILD_DESIGN_ID` and `GUILD_BINDING_GENERATION` from the caller and sets them from the context: holder session, design ID, generation, and `PLAN_DELEGATE_BOARD_DIR` only when bound.

**File ownership.** `scripts/hooks/pre-tool-use-berth.py` checks `apply_patch` only. With `GUILD_DESIGN_ID` set, it calls `roles.check_patch(design_id, GUILD_BINDING_GENERATION, cwd, paths)` before phase attribution. It rejects the whole patch for `transition.json`, a stale generation, a holder mismatch, an unbound designer, any path (rename sources included, symlinks resolved) outside the bound output paths or the repository, or an unreadable ledger. The binding's run directory then drives attribution, and an unreadable reservation ledger refuses instead of failing open.

**Adoption.** `adopt` needs the authority's `adoption_required` source (`thread_id`, `name`, `mesh`, holder) to match. Steps: first the checks that change no authority state: the binding, the configured model, and the source entry, which must be idle on that thread (a `paused` entry counts as idle when `paused_from` is not busy). The source model and effort come from the roster entry; when it has none, from that thread's launch record (`impl_worker_<slot>.json` with the same `worker_id`, then the `phases/*/impl_agent_<slot>` file whose log names the worker and thread), and last from the source server's `thread/read` reply. Missing, conflicting or different values refuse with `AdoptionRequired:`. A refused adopt therefore leaves no `AdoptionReserved` claim. Then `adopt-reserve`; check the source entry again under its roster lock; write `transition.json` and `transfer.json` and set `transferring_role` on the source entry under its roster lock; on the source server park the queue, require `ThreadIdle` and release the writer; write the destination roster entry (same thread and name, saved prompt, access policy) and carry pending text; `confirm_adoption` resumes the same thread on the destination and requires `ThreadIdle`; set `role_redirect` on the source; write `adoption.json`; `adopt-commit`; write the hold. A retry with the same adoption ID completes from `adoption.json`. `_role_fence` and `transfer_route` make every old route refuse (`TransferringRole: use the destination holder route`), also after the source run folder is gone.

**Unit rename.** `rename_unit.py` `_move_state` calls `roles.rename_holder(slug, old, new, old_worktree, new_worktree)` before relaunch: idle required, authority `checkout` then `advance`, holder, checkout, document and binding paths moved, generation +1, `guild_context.json` rewritten, roster `cwd` set and `authorized_*` cleared. A failure raises `RenameRefused`; the rename journal retries it. `_state_failures` adds `roles.rename_failures`, which flags an old holder, old checkout in live fields or roster `cwd`, or a stale context. `unit_rename_sweep.py` `_history` treats `prompts/`, `replies/`, `adoption.json` and `transfer.json` as history, and `sweep` ignores role `name` and `aliases` in `design.json` and `aliases` in the roster as identity.

**Unit transfer and production rename.** `rename_holder` is `move_holder(old_holder, new_holder, old_checkout, new_checkout, reason=...)` for one unit. `move_holder` moves every design held by `old_holder` to `new_holder`; when the checkout does not change, it keeps the repository, document, binding paths and roster `cwd`, and skips the `checkout` request. It writes `transition.json`, releases the servers, bumps the generation and clears `authorized_*`; a second call changes nothing. `holder_failures(old_holder, old_checkout='')` flags an old holder, a stale context and, with a checkout, the old checkout. `transfer_unit.py` `_move_state` moves `UnitHolder(from, unit)` to `UnitHolder(to, unit)` and `verify_failures` adds `holder_failures`. Production `rename.py` moves `ShowrunnerHolder(old)` to `ShowrunnerHolder(new)` in apply step 4, and each `UnitHolder(old, unit)` with its worktree paths in unit step 3. Role names keep the old slug: they are thread aliases, kept as history. A failure raises `RenameRefused`.

### Astra work header

`scripts/guilds/astra_work.py` saves assignment identity before dispatch as the first line of a brief or prompt: `<!-- astra_work {JSON} -->`.

- Frozen types: `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)` (`kind="phase"`), `StandaloneWork()` (`kind="standalone"`), `AstraWork(role, member, document, assigned_at_utc, reply_path, work)` (`schema=1`, role `astra-designer` | `astra-reviewer`), and read results `HeaderMissing(path)`, `HeaderInvalid(path, reason)`.
- `phase_work(run_directory, phase_id) -> PhaseWork` reads `progress_history_state.json`: `open_phases` plus the `phase` when its status is `active`. Exactly one match or `ValueError`.
- `header_line(work) -> str`; `read_header(path)` checks the exact key sets, `schema` 1, the role and a UTC `assigned_at_utc`.
- CLI `astra_work.py brief --run-directory --phase --member --document --round --text-file` writes `<run>/design/phase<id>/round<N>_brief.md` by exclusive create, with `reply_path` `round<N>_reply.md`; it refuses (exit 2) when that reply exists. Holders use it only for a designer that `roles.py` does not hold, and save the reply there without overwrite.
- `astra_review.py [--run-directory <dir> --phase <id>] <document> [focus]` takes both flags or neither. A one-turn review writes the header as the first line of `/tmp/claude/astra/<stamp>-<pid>/prompt.md`, with `member` the run folder and `reply_path` its `review.md`. Exit 2 also covers an unresolved phase.

### Census for cargo-handler

- `scripts/astra/astra_roles.py` writes one file per thread, `~/.local/state/astra/roles/<thread id>.json` (`ASTRA_ROLES_STATE`), by temp file and rename. `RoleRecord` keys are exactly `role`, `thread_id`, `codex_home`, `manager`, `held_at`, `session_dir`, `worker`, `pid`, plus `design_id` and `role_id` for a durable hold. In memory `HeldRole`/`LiveRole` carry `lifetime: MeshWorker(session_dir, worker) | ExecProcess(pid) | HeldDesignRole(design_id, role_id)`.
- `hold(role, thread_id, codex_home, *, session_dir, worker, pid, manager, design_id, role_id) -> HeldRole`, `release(thread_id)`, `live_roles() -> list[LiveRole]`; CLI `hold`, `release`, `list [--json]`, exit 2 on a refusal.
- `live_roles` removes a mesh hold whose run folder, roster worker or thread is gone or whose status is `ended`/`failed`, and an exec hold whose pid is gone. A `HeldDesignRole` hold ends only when the durable role is `RoleEnded`; a missing run folder cannot end it. Its `manager` is refreshed to the current holder session. `busy` means status `starting`, `running` or `waiting_capacity` (or a live exec pid). A bad record is one stderr line; other records still return.
- `astra_review.py` `run_review` holds an `ExecProcess` reviewer during its one-turn `codex exec` when `CLAUDE_CODE_SESSION_ID` is set, after reading `session id:` from `codex.log` (30 s), with `codex_home` from `accounts.py env workers`, and releases in `finally`. For a document with a held design (`roles.held_document`), `held_review` sends it to the sole reviewer through `roles.resume`; busy or unavailable exits 1 and starts no other reviewer.
- `scripts/production/production_tree.py --json` keeps schema 1 and adds one `AstraEdge` (`manager`, `member` = thread ID, `role` `astra-designer` | `astra-reviewer`, `codex_home`, `state`) per live hold, and drops the Codex worker edge for that thread.
- `scripts/message/roster.py` `_durable_roles` adds one Codex entry per open held role (role `astra-designer` / `astra-reviewer`, reach `CannotReceiveMessages`, director or showrunner from the holder), skipping fenced threads. `scripts/agents/mesh_runs.py` `_roster_folders` lists durable meshes.
- Colors: `config/statusline.conf` `role.astra-designer=mauve`, `role.astra-reviewer=bright-green`.

### Held-role invariants

- One designer and at most one reviewer per design, one holder per design, and the pair lives on the holder's host. No host transfer and no authority failover.
- A lost or uncertain thread becomes `RoleNeedsRecovery`; nothing starts a replacement thread, forks a held thread or reopens an ended identity.
- Held roles change only through `roles.py`. Every mesh route that can start, follow, pause, stop, reopen, fork or re-instruct a thread checks `held_design_id` and `_role_fence`. A new route must do the same.
- Lock order is design, pending, roster, server. No SSH, authority call or LLM turn runs under these locks; `authority_request` runs after design and mesh locks are released.
- Park queued text before any server stop or release. Clear a pending message only after its turn is accepted; keep it and its ID on uncertainty.
- Access comes from the roster `access_policy`, never from the instructions text. Reviews stay `ReadOnlyReviewer`.
- The berth check covers `apply_patch` only. Designers edit by patch; shell or Python writes are outside the check. Do not copy reservation state into the role store.
- Role records outlive run folders. Ordinary run cleanup unbinds; it never ends a held role or stops a durable mesh.
- A census hold is a projection of the durable role, repeatable on the next lifecycle action. Removing it cannot delete a design. `AstraEdge` fields and `production_tree.py` schema 1 stay; cargo-handler reads them.
- `production_tree.py --json` reads only local records, never prompts, answers well under a second, and drops only the edge it cannot read, with a stderr line.
- A showrunner may hold a read-only role but cannot launch an unbound writer, because no production-design phase exists yet. A writer needs an authorized phase with exact output paths, baseline and reservation.
- Tests use fixtures and environment overrides (`GUILD_ROLES_STATE`, `GUILD_OWNERSHIP_STATE`, `GUILD_CONFIG_FILE`, `GUILD_ROLES_HOST`, `ASTRA_ROLES_STATE`, `ASTRA_REVIEW_DIR`), never real state or a live Codex.
- Stored stamps are UTC; user-facing times use `scripts/lib/user_zone.py`.

### Held-role limits and gotchas

| Value | Where |
| --- | --- |
| `THREAD_HEADER_TIMEOUT` 30 s | `astra_review.py` |
| Authority SSH: `ConnectTimeout=10`, 30 s run limit; `resolve_model` 15 s | `roles.py` |
| `THREAD_RELEASE_TIMEOUT_SECS` 20 s | `codex_mesh.py` |

- The server context comparison uses JSON-normalized values. A tuple `output_paths` against the saved JSON list never matches, and the release then takes the pending lock a second time. Keep `_guild_context` passing through `json.dumps`/`_loads`.
- The adopt commit writes `adoption.json` before it removes `adoption_required`, so a same-ID retry completes after a failure at any step.
- Account-move recovery checks the saved header before it claims the roster entry.
- The first `register` for a repository must carry every host's checkout: a later `register` with a different mapping refuses. The `register` CLI takes one host; use `register_repository` for two, or the authority `checkout` operation to move one. `alias` and `legacy` are reached only through `roles.py authority <op>`.
- Scripts import as `from scripts.<folder> import <module>` after `sys.path.insert(0, <repo root>)`. `roles.py` imports `codex_mesh` and `astra_roles` through `importlib` at the call to avoid an import cycle; do not name a module function `open`.
- A new script folder runs in the merge gate only when listed in `scripts/production/merge_gate.py` `TEST_SUITES`; `test_merge_gate.py` `EXPECTED_SUITES` changes with it. `pyrightconfig.json` gives `scripts/guilds` its extraPaths.

### Why roles outlive runs

- **One store outside run roots.** A run folder ends with its run, and a `MeshWorker` hold ends with its folder. A design outlives several phases and runs, so its threads, prompts, replies and pending text live in `~/.local/state/guilds/roles/`, and the census uses a `HeldDesignRole` lifetime tied to that store. Lasting design threads need their own holder records. Copying a live roster into another run would give one thread two owners.
- **One designer and at most one reviewer per design.** The user's rule: one Astra writes and revises in the same thread; one Astra reviews in one thread. A fresh `codex exec` per round cannot prove one reviewer per design, and a reviewer pool that grows when busy breaks the rule, so a busy reviewer is reported, not replaced. One thread per role keeps the context and the cited rulings across rounds.
- **One natedev authority with explicit checkout mappings.** A URL spelling, a new worktree or an unreachable host must not make a second identity for the same document. Reservation before creation and retries with the same request ID mean an uncertain launch never frees the slot.
- **Raw pause refused for held roles.** `stop --pause` truncates pending text and drops queued text, and raw `stop` deletes server records without move markers. Held roles release servers only through `_release_held_server`, which parks queues and proves idle first.
- **Patch-only write check.** The berth hook sees `apply_patch` paths before the edit; other tools give no path list, and writers run with full sandbox access. So the guarantee covers patches, and the designer prompt tells designers to edit only by patch.
- **Explicit access policy.** Access must not follow from prompt text. The mesh once inferred read-only access from `instructions_file`; a typed `ThreadAccessPolicy` on the roster entry now keeps access through start, follow, account recovery and adoption. `roster_access_policy` in `codex_mesh.py` is the one reader. An entry from before the policy existed gets its policy by what it is: with `instructions_file` and no `held_design_id` it is a reviewer template or fork (pool or `review.sh --slot`) and gets `ReadOnlyReviewer`; with `held_design_id` it is a damaged held role and refuses, naming `roles.py recover`; with neither it is a worker and keeps its launch sandbox. Each locked roster load stores the reviewer policy on every such entry in the file, so the first write after install migrates a whole pool; a read without the lock writes nothing.
- **Short cited rulings.** Revisions that answered every finding with more mechanism grew a design without a correctness gain. Holders resume the same writer with short rulings that cite files, and change the Work Order only when scope changes.
