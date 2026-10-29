# Long-lived Codex workers

## What it is

A `/unit:direct` run opens its Codex workers once and keeps them for the rest of the run. Every later phase, repair round, broad review lens, closure review and phase-review architect pass goes to a worker that is already open, with `--to <full-worker-name>`. The exception is a phase on the pool arm of a production with a review pool: its code reviews go to the production's pooled reviewers, which are not workers of the run (`docs/as-built/review-pool.md`). A worker can change role between tasks: a writer becomes a tester, a tester becomes a reviewer, or a writer takes the adversary lens against its own code. Before an open worker gets new work, the launcher compacts its thread if the recorded context is over a threshold. A turn that fills the context window gets the follow-up "Continue where you stopped." When a phase finishes, its turns end but its threads stay open. The threads end only when `end_session.sh` stops the run's app-server. `scripts/whoami/worker_usage.py` checks whether this saves Codex usage. It reads the usage store (`docs/as-built/usage-ledger.md`), compares weighted Codex usage per checkpointed phase before and after the go-live time, and reports account switches per week and credits used per day alongside.

## How it works

### Worker lifecycle in `scripts/agents/codex_mesh.py`

Each run has one detached `codex app-server --listen ws://127.0.0.1:<port>`. Each worker is a named, non-ephemeral thread on it.

**Server environment.** `_start_server` gives the server the caller's environment with `CODEX_HOME` set to the workers home. When `--session-dir` is a child of a unit run root (`RUN_ROOTS`, `/tmp/claude/unit` and `/tmp/claude/delegate`), it also sets `UNIT_RUN_DIR` to that folder and `TMPDIR` to its `tmp/` folder, replacing any values the caller carries. Worker processes inherit it: `verify.sh` takes the cargo token on that board, and the berth hook (`scripts/hooks/pre-tool-use-berth.py`) checks that run's open phases. The caller's copy cannot be used: implement.sh exports it only after its claim step, and the claim's `compact` call starts the server when none is running. On 2026-10-17, 11 of 13 live app-servers lacked it, all three started by that `compact` call among them. A review pool's mesh directory (`mesh/<generation>/`), and any other mesh directory outside the run roots, keeps the caller's environment unchanged: one pool server serves the turns of many units, so no single run is right for it, and a reviewer edits nothing.

For unit runs, `start`, `prime`, an explicit `follow --cwd`, and resident turn transitions pass their known cwd to `ensure_server`. Direnv's nearest `.envrc` at or above that cwd selects the dev-shell folder, so turns in worktree subfolders share the same server. `direnv status --json` must identify that file as allowed (`state.foundRC.allowed == 0`) before the server starts through `direnv exec <dev-shell folder> env <managed assignments> codex …`. The approval check does not evaluate the shell. A missing or unapproved `.envrc` selects the ordinary launch. A missing direnv, failed or timed-out probe, or unreadable status is an unknown result: it keeps a healthy recorded server and its saved dev-shell preference. Without a healthy server, an unknown result starts an ordinary server.

There is no wrapper on worker commands: the app-server passes the loaded dev-shell to all its children. The assignments after direnv restore `CODEX_HOME`, `UNIT_RUN_DIR`, `TMPDIR` and any managed guild identity keys, so `.envrc` cannot replace them. [Direnv uses `syscall.Exec`](https://github.com/direnv/direnv/blob/v2.37.1/internal/cmd/cmd_exec.go), and `env` also executes its command in place: the recorded pid is the app-server's own pid. Plain launches retain the 30-second `SERVER_START_TIMEOUT_SECS` budget and sweep record-start window. Only dev-shell launches get `DEV_SHELL_START_TIMEOUT_SECS` (300 seconds) for cold direnv/Nix evaluation. While one starts, `mesh_server_start.json` names its pid, port, start time and dev-shell folder. The sweep discovers this temporary record under the run roots even without a recoverable log handle, and keeps that server through its dev-shell startup budget. The marker is removed after success or failure; expired markers do not extend the plain record-start window. If that launch cannot execute, exits, or times out, the failure and direnv's stderr go to `mesh_server.log`; the same start path retries with the caller's environment and records a plain server. `mesh_failed_dev_shell.json` remembers the failed folder, an input revision and a retry deadline 30 minutes after the failure was written. The revision covers `.envrc`, `flake.nix`, `flake.lock`, `shell.nix`, `default.nix` and `devenv.nix` in that folder, using each file’s nanosecond modification/change times and size, or its absence. The remembered-failure type owns both the input key and the deadline. Until the deadline, an unchanged revision makes a cwd caller reuse the healthy plain fallback and keeps restarts plain, instead of repeating the failed evaluation. Editing, creating or removing a tracked input, or reaching the deadline, permits a fresh attempt. Older failure records without the input key and deadline also permit a fresh attempt. A successful dev-shell start clears the failure record. The failure check runs under the server lock, so queued callers observe a failure saved by the preceding launch. Failed-start cleanup signals the whole process group, including evaluation children. No live worktree evaluation was measured for this change.

The server record stores `dev_shell_worktree` for a successful dev-shell launch. When a cwd caller receives a definite probe answer and the required dev-shell differs from the record (including a legacy record with no dev-shell), `ensure_server` retires the server and starts its replacement through the same path as an account or compaction-limit change. Existing turns retain the retired server until released. This also removes a previously loaded shell when its `.envrc` disappears or is no longer allowed. An unknown probe answer does not retire the server for a dev-shell mismatch. A caller without a cwd never retires a server for that reason.

`mesh_dev_shell.json` in the session folder stores the last successfully served dev-shell folder as `worktree`, independently of the current server record. When `compact`, `serve`, `fork`, or another cwd-free caller must restart after idle-stop, it rechecks that folder's approval and uses the same dev-shell when still allowed. A definite answer selecting the ordinary environment clears this preference; an unknown answer preserves it for a later retry. A pre-existing server record can also supply the folder when the preference has not yet been written. Review pools and other shared mesh folders never load a worktree's dev-shell.

The berth hook takes the session directory from `UNIT_RUN_DIR` when it names a folder that exists. When the variable is unset or its folder is gone, it reads the director's run marker (`unit_runs.marker`), so a stale variable never lets an edit through unchecked. The marker names one run, so a worker in a director's side run would otherwise check the main run's reservations.

The verbs:

| Verb | Signature | Behavior |
| --- | --- | --- |
| `start` | `--session-dir --name [--role impl\|test\|fix\|review] [--lens] --cwd --prompt-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--sandbox danger-full-access] [--resident]` | Opens a thread and blocks until its turn ends. `--role` is optional, and roleless threads such as ask-a-friend friends leave it out. A given role, with `--lens` when passed, is appended to `agents.jsonl` for the new thread. |
| `follow` | `--session-dir --to --role (required) [--lens] [--claim-pid] --message-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--cwd] [--read-only] [--image …]` | Runs one more turn on a finished thread. If the old server is gone, it resumes the thread on a new one. `--cwd`, `--read-only` and `--image` apply to that turn only and exist for the review pool; without them the request is the worker's usual one. |
| `prime`, `fork`, `reset` | see `docs/as-built/review-pool.md` | Create a read-only reviewer template with standing instructions, fork a named reviewer from it, and remove every turn from an idle reviewer. Only the review pool calls them. |
| `compact` | `--session-dir --to` | Exits 2 for a worker that is running or busy, a worker with no recorded context size, and a worker whose size is `not_measured_since_compaction` (`size not measured since its last compaction; compact refused`). Otherwise it reads the entry's `revision`, resumes the thread, waits up to 60 s (`COMPACT_IDLE_WAIT_SECS`) for a turn still live on it to end, exits 3 (`COMPACT_TURN_STILL_LIVE`) if one stays live, and calls `thread/compact/start`. It waits until the compaction turn has a `thread/compacted` notice or a `contextCompaction` item, a token-usage update and a `turn/completed`. It then writes the new size under `RosterUnchanged(thread_id, revision)`, so a turn that wrote the entry in between keeps its record. A size above 0 prints `compacted <worker>: <before> -> <after> tokens`; a compaction turn that reports 0 stores `not_measured_since_compaction` and prints `compacted <worker>: <before> tokens, size not measured until its next turn`. A compaction that has not finished in 300 s (`CompactionTimedOut`) is interrupted with `turn/interrupt` and the thread is read until idle, up to the same 60 s: idle is exit 1 with `…; interrupted the compaction turn, the worker is idle and not compacted`, so a follow-up can still be sent; a turn still live is exit 3. |
| `can-follow` | `--session-dir --to --role [--lens] [--claim-pid]` | Refuses a worker that cannot take a follow-up. With `--claim-pid`, it claims the worker under the roster `flock`: it sets `status: starting`, `launcher_pid`, `previous_status`, the new `role`, and `previous_role` (the role the worker held before), and appends the role and lens to the role log `agents.jsonl` (`docs/as-built/usage-ledger.md`). |
| `release-follow` | `--session-dir --to --claim-pid` | Releases a claim that never started a turn. It restores `previous_role`, or removes `role` if the worker had none, returns the worker to its previous status, and appends the restored role to `agents.jsonl`. A follow that did run keeps the role it was claimed with, so after a completed review the worker's role stays `review`. |
| `end` | `--session-dir --to` | Clears the worker's pending queue, writes its end marker and sets the worker's roster `status` to `"ended"`. |
| `stop` | `--session-dir [--pause]` | Sets every roster worker to `"ended"` and stops the current server and every retired one. With `--pause` (a shutdown stop, `docs/as-built/shutdown-and-restart.md`) it instead sets every non-ended worker to the followable `"paused"`, with `paused_from` and `paused_turn`, after dropping its queued messages and interrupting a live turn. |
| `reopen` | `--session-dir --to` | Returns an `ended` worker, or a `running`/`starting` one whose launcher is dead and whose server is gone, to `"paused"` on its recorded thread. Exits 2 with one stderr line when the thread id is missing or a launcher or server is alive. |
| `sweep` | `[--root] [--stop]` | Gives a verdict on every app-server listening on `ws://` (see Cleanup). |
| `idle-stop` | `--session-dir --pid --port` | The server's own watcher calls this. It stops the server, or touches its record if something still uses it. |
| `list` | `--session-dir` | Prints the roster. |

A held design role (a roster entry with `held_design_id`, `docs/as-built/guilds.md`) changes only through `scripts/guilds/roles.py`: `start`, `prime` over it, `fork` from it, `reopen`, `set-instructions`, raw `stop` and `stop --pause` refuse it, `follow` takes only its `authorized_prompt` (or an account-move continuation), and `send` queues the text in the role store.

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

**`scripts/direct/review.sh [--to <worker> | --slot <new-slot> | --pool code|ui --lease <slug>/<unit>/<phase>] <session_dir> [working_dir] [prompt_file] [task] [role_description] [pass_activity] [pass_index] [lens] [early_ready_file]`**

`lens` must be one of `adversary`, `contract`, `craft` or `ux`. Lens `ux` is the design check: it records no review pass, since the unit director's `UX review` activity carries that time. Findings and status are keyed by pass and lens, never by worker: `review_findings_<N>[_<lens>].txt`, `review_status[_<lens>]`. The agent log adds the worker, `review_agent_<N>[_<lens>]-<slot>.log` for a worker and `-pool` for a pool reviewer, so a second worker on the same pass cannot truncate the first one's log.

- **`--pool code|ui --lease …`**: reviews on a pooled reviewer of the production, on a read-only turn. `docs/as-built/review-pool.md` describes this path.
- **`--to <worker>`**: reviews on an open worker.
  - It writes one turn record per attempt and marks it `canceled` on TERM, INT or HUP, exiting 143, 130 or 129 (`docs/as-built/review-pool.md`, Turn record). Only a pool review's `--lease` names the unit and phase in that record.
  - The recorder slot is the worker name's last hyphen segment. It must match `^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$`.
  - The family must be Codex with the mesh on.
  - Under `impl_worker_claim.lock`, it compacts when the recorded size is an integer above the threshold (best-effort, one board line), then claims with `can-follow --role review`.
  - The claim happens before any artifact is written, so a refusal for a busy worker cannot overwrite another review's status or findings.
  - It then runs `codex_mesh.py follow --role review`, and releases the claim on exit.
- **`--slot <name>`**: opens a new worker named for its role (`review`, `review2`, `fix2`).
  - The name must match `^[A-Za-z][A-Za-z0-9_]{0,31}$`, so it cannot contain a hyphen.
  - It runs `codex_mesh.py start --role review` with the default `danger-full-access` sandbox.
  - After the turn it writes `impl_worker_<slot>.json` with the configured implementation model of the family (`worker.<family>` `implementation`, or `impl` if that is not set), not the review model. A later `implement.sh --to` can then give the worker writing or repair work.
  - The unit keeps the worker across run ends until the unit is retired.
- **Neither flag**: runs the fresh blind reviewer through `agent_exec.sh … readonly`, with no address (`mesh=none`). The skills use this form only for a review outside a run.

**Tree guard (assigned and pooled reviews).** Before the turn, `review.sh` runs `review_tree.py baseline`, which records HEAD and, for each path `git status` names (untracked, non-ignored files included), its HEAD, index and worktree entries: mode and blob id, a symlink's target standing for its content.

After the turn `review_tree.py changes` checks every path that was dirty before or after, or that HEAD moved under, and lists each one whose entries differ. When two or more phases are open in the session and the review names its phase with `--phase`, a changed path that another open phase's `phases/<phase>/files` list holds is that phase's writer at work: the launcher log notes it and the check leaves it out. A path the reviewed phase holds, or no phase holds, stays the review's. Any path left makes the review status `error`, prints `ERROR: review worker <worker> changed the working tree: <paths>`, posts `blocked` on the board, and keeps the findings file. An early review, which starts while the writer is still running, takes this snapshot when its early-ready sentinel appears.

### Choosing a worker for a review

The rules live in `commands/unit/direct.md` `<LongLivedWorkers/>`, `docs/direct/dual_review.md` (`<TeamReview/>` and `<ReviewPromptContract/>`), `docs/direct/run_phase_review.md`, `docs/direct/write_prompt_contract.md` (`<RoleReassignment/>`) and `commands/plan/phase_review.md`:

- Each lens gets a different open worker, and an idle open worker, a writer included, takes a lens before a new worker opens. Idle workers that did not write the files under review go first; then a writer takes one, arguing against its own choices on the `adversary` lens.
- Roles move between workers; slots do not. Any open worker may take any role, including in the middle of a phase. Before dispatching, post a board `handoff` that names the slot, the role it leaves and the role it takes. The new prompt has the worker run `board.sh role` before its first tool call.
- If every open worker is busy, open an extra worker named for its role with `--slot` (`review`, then `review2`; `fix2` for repair) and keep it with the unit across run ends. Never name an extra worker with a bare number.
- Only the adversary lens starts early, and the director records its worker in `EARLY_REVIEW_SLOT`. The closure review and the phase-review architect go to an open worker with `--to`.
- Every review prompt opens with a role switch: "You are slot <slot>, now working as a reviewer. Edit nothing. Read the diff cold as if someone else wrote it. If you wrote any of the code, argue against your own choices as an adversary would." The prompt also gives the concrete `board.sh role … review` command for the worker's first tool call.
- Every prompt carries the Three Gods sentence written out in full: opening, later-phase, repair, review and follow-up prompts alike. It also carries the Type Design Contract, verbatim for a new worker or as the allowed pointer for a follow-up. Compaction can erase earlier prompt text, which is why each prompt repeats them.
- A new worker opens only when no open worker can take the work: the assignment needs more workers, every open worker is busy, or a reusable thread is gone. A replacement under the same name starts fresh and gets an opening prompt.

`scripts/direct/progress_history.py` gives each extra worker (`review2`, `fix2`, …) its own `Agent N` number after `impl` and `test`, in the order workers first appear: a `- **Agent N**` line in every report, and a round-table column under `progress --tables`. A worker appears when it records a pass or receives a board handoff. The original `review` worker keeps its earlier rule: it shows only while a displayed stage assigns it.

### Cleanup

- **Phase end.** `<PhaseCleanup/>` runs `remove_workers.py --session-dir`. It leaves Codex threads open and preserves Claude workers held by the unit ledger or a live unit director. It does not launch server reclamation.
- **Run end.** `scripts/direct/end_session.sh` acts on every run folder the ending director holds, not only the one its marker names: a director runs independent phases side by side, each in its own run folder. `unit_runs.py held <session id>` lists them, the marker's run first, then every folder under the run roots whose `unit_runs.director(run)` is the session. It is a plain scan: reading the director of all 224 live folders took about 25 ms (2026-10-17). The marker's run is kept even when the scan fails. A run of another director is never touched. The steps, in order:
  1. `roles.py unbind-run <run>` for each held run, which unbinds every held design role bound to it and leaves its thread, prompts and replies (`docs/as-built/guilds.md`); a failure stops the script before any other step
  2. one `remove_workers.py` call with a `--session-dir` per held run
  3. `codex_mesh.py stop --pause` for each held run with `mesh_roster.json`, `mesh_server.json` or `mesh_retired.json`. A live unit director's workers, queues and servers remain untouched. Otherwise pause preserves pending messages, owed continuations, reservations and detached commands before stopping only that run's recorded servers.
  4. one `remove_run_scratch.py --tracked` call for every held run: it removes `verify_cache/` and git checkouts and cargo targets at any depth, without descending into what it removes or following a link. It keeps any checkout or tracked scratch folder containing the working directory of a live detached command belonging to a worker that is not retired. A linked worktree goes through `git worktree remove --force --force` in its source repo, or is deleted and its source pruned; ledgers, logs, diffs, json and the run folder stay.
  5. removal of the run-active marker

  Steps 1 to 4 run even with no marker, for runs the session still holds. Steps 2 to 4 each continue after a failure. Run end does not launch a sweep. Tests point the scan at a temp folder with `UNIT_RUN_ROOT` and the marker with `UNIT_ACTIVE_DIR`.
- **Sweep.** It first decides who owns each `ws://` app-server: the current server of a run, the current server of a held design role, a retired server of a run, an unowned server whose log names its run, or an unowned server with no log. Alongside the run folders selected by `--root`, it reads the durable design mesh folders returned by `roles.durable_meshes()` (folders with `mesh_roster.json`). A matching current server record belongs to a held design role only while its roster has an entry with `held_design_id`; a released role or deleted folder gets the usual ownership rules. It then judges whether anything still needs the server:
  - Any connected client means in use.
  - For a current run server, a run-active marker or a running worker means in use.
  - A retired server is unused unless a conversation is mid-turn or a loaded thread has queued or unreadable work.
  - An unowned server is in use during the record-start window, while a conversation is mid-turn, or when a loaded thread has queued or unreadable work.
  - Current run servers and held design servers share the 30-minute quiet window (`SERVER_IDLE_SECS`): a live worker launcher, a server under 30 minutes old, a mesh folder changed in the last 30 minutes, or a conversation mid-turn means in use.

  It reads `thread/queue/list` for loaded threads; queued submissions or unreadable queue state keep the server. It prints `pid  port  verdict  reason  owner` with verdict `in use`, `unused`, `stopped` or `kept`. With `--stop`, it judges a second time under the owner's mesh folder server lock before stopping a server. Ordinary idle-stop and retired-server reclamation can free an unused server while the director lives; the next dispatch resumes the same worker name and thread. Servers listening on `--listen unix://` are never touched. Each server also has its own watcher, which wakes every 300 s and runs `idle-stop` once the server record has gone 30 minutes untouched. If a server fails to start, its process is terminated and its record dropped.

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
| `scripts/direct/review.sh` | Reviews: `--to` reuse, `--slot` new worker, `--pool` pooled reviewer, tree guard, fresh read-only fallback |
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
- Review artifacts are keyed by pass and lens; the recorder pass is keyed by the slot that ran it (`pool_<reviewer>` for a pooled review), and a review with lens `ux` records none.
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
- **Reviews on open workers, without the read-only sandbox.** A review on an open worker runs on that thread's existing sandbox, and a worker may review its own work. The tree guard enforces "edits nothing" in place of the sandbox. A pooled reviewer is not a worker: its turn is read-only and the tree guard still runs after it. The fresh read-only reviewer remains only for reviews outside a run.
- **Verdict per checkpointed phase.** The amount of work changes from day to day, so usage per day would not compare the two windows. Account switches are too rare for a verdict: the baseline had 2 in about five days, too few to show a change within a week. They are reported alongside the verdict.
- **Hyphens refused in `--slot`.** `--to` derives the slot from the worker name, so refusing hyphens is simpler than resolving a hyphenated slot from its recorded identity.
- **Second-overflow error kept.** It stops a worker that fills its context again and again without doing work, so press-on cannot loop forever.
- **`tools.apply_patch(` kept in edit detection.** That call is how real edits appear in rollouts. Detection skips mentions inside strings, so a quoted mention does not count as an edit.
