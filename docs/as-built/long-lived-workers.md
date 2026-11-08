# Long-lived workers and held Astra roles

Codex code reviewers use the unit ledger and staff identity. `assign.sh --role review` resumes
`<worktree>-review`; concurrent work uses `<worktree>-review2`. Both are
read-only and persist across phases and plans. The fresh Claude helper in
`<UXReview/>` remains the design check.

## What it is

A unit keeps its worker threads across phases and plans. `assign.sh` selects
an idle worker by role, resumes it, and claims it for the dispatch.
Codex code review uses the unit's read-only `review` workers.
The [daily simplify run](simplify-lens.md) uses a fresh Claude review.
Writers keep their writing roles.
Each follow compacts when the recorded context exceeds the configured limit.
After accepting a pass, the director closes its pane unless another fix round or phase dispatch starts immediately.
The close retains the staff row and thread. The next assign resumes that thread in a new pane.
A busy worker or live detached run prevents closure until that work ends.
Unit pause, move, and end use staff membership and the worker ledger.
Usage reports compare checkpointed phases through the usage store.

## Unit code reviewers

Codex code reviewers use the ordinary unit worker ledger. Resolution reuses an idle
reviewer and registers a roster-only reviewer in the ledger without changing its
thread. A concurrent review uses `review2`.

| Worker slot | Role | Access | Lifetime |
| --- | --- | --- | --- |
| `review` | `review` | Read only | Keep the thread across phases and restarts. |
| `review2` (and later numbers) | `review` | Read only | Keep each concurrent reviewer's thread. |

A reviewer's address is `<worktree>-review` or `<worktree>-review2`.
Its record keeps the slot. The assignment log supplies its task role.
Its shown job
uses the `code_reviewer` key from `config/names.toml`. Reviewers persist, pause,
move and end by the same ledger operations as writers. `workers.py resolve
--role review` reuses an idle reviewer; it opens a read-only thread only when no
reviewer can take the work.

One renderer makes every worker's thread title and message prefix:
`workers.shown_name`. A unit run gives `<production> · <unit> · <job>[ <n>]`,
with the unit's staff name. The job label comes from
`config/names.toml`: `implementer`, `tester`, `repair_worker` or
`code_reviewer` for the `impl`, `test`, `fix` and `review` roles, and `worker`
for any other role. The second holder of a role gets the number 2, and so on.
A held design, or a run outside a production, takes its placement from the
roster (`roster.mesh_title`). Each turn sets the title again, so it follows the
current assignment.

## How it works

### Staff identity and admission

A worker has one fixed staff id, name, parent, and worktree.
Its `current_session` selects its current Claude session or Codex thread.
A family switch changes `current_session` and preserves the fixed staff id.
The [staff design](staff-accessors.md#staff-identity-and-backends) owns row fields and backend evidence.

`worker_admission.admit(name, run, *, reference="")` returns `WorkerStartAllowed` before a backend starts.
Its slot is `NewWorkerSlot` or `ExistingWorkerSlot(row)`.
`PublishedWorker(row, handoff)` means staff publication succeeded.
Only publication permits dispatch success.
`assign.py` completes admission before it records the implementation pass.
A publication failure cleans up the backend that this attempt created and reports its native id.

A new slot takes the parent/name lock before it takes the row lock.
An existing slot takes the row lock.
`worker_operation` locks the admitted existing row. For a new slot, it first checks membership under the parent/name lock.
Allocation passes one staff snapshot and one role log through its readers.
An `AdmissionRefused` from admission or publication skips that candidate and continues selection.
`UnitWorkerLedger.locked` accepts optional membership at its public boundary.
`None` reads one fresh snapshot under the lock; allocation passes the snapshot that found the ledger.
Internal role and idle readers require their caller's snapshot or role log.

### Family handoff and retry

`switch_worker` extracts source context, releases the source, creates the destination, and publishes its native identity.
The same path serves both families.
`FamilyHandoff` has `NoFamilyHandoff`, `TranscriptHandoff(extract, source)`, and `TranscriptUnavailable(reason)` variants.
The extract contains assignments, final replies, and changed paths.
It excludes tool request bodies and tool output.
A missing transcript prints an anomaly and permits dispatch without a handoff.
A named `TornTranscriptRecord` lets extraction continue across valid transcript lines.

The source release result is `SourceReleased`, `SourceUnresolved(reason)`, or `SourceReleaseUnknown(reason)`.
`stop_resume.release_source` preserves each stop reason and adds the next action to a refusal.
`StopUnproved` does not prove that a source remains live.
For Codex, the release path resolves that result through server evidence.
Codex release checks the fixed id and current session against staff.
It parks queued submissions before interruption and checks every recorded source server.
A missing server record or a stopped server permits release without a diagnostic.
A record with only `previous_servers` names no current server.
An invalid record or an unavailable server prints an anomaly and permits release.
A changed staff identity prints an anomaly and permits release without signaling a process.
Only an answering server with a loaded target thread and an unknown stop state prevents Codex release.

The default switch request is `sha256(f"{staff id}:{destination family}")`.
Assignment delivery uses `assignment_id(prompt)`, which hashes the resolved path, modification time, and contents.
The original prompt file remains the assignment identity throughout a retry.
`DispatchRecord(assignment_id, state)` uses these states:

| State | Stored value | Meaning |
| --- | --- | --- |
| `NO_RECORD` | empty text | No known dispatch state. |
| `CREATION_INTENT_RECORDED` | `prepared` | The request is recorded before backend creation. |
| `DISPATCH_IN_FLIGHT` | `sending` | Delivery is in progress or its result is uncertain. |
| `ASSIGNMENT_ACCEPTED` | `accepted` | The destination accepted the assignment. |

`DispatchOwner.TURN_SUBMISSION` delegates acceptance recording to turn submission.
`record_dispatch` checks its expected request under the exclusive roster lock.
It preserves a request that another operation replaced before lock acquisition.
`dispatch_record(run, name, *, report_anomaly=True)` reports an unknown stored state once and uses `NO_RECORD`.
Its diagnostic is `anomaly: <name>: unknown dispatch_state <value>; using NO_RECORD`.
This anomaly does not refuse admission.

`dispatch_pending(run, name, prompt)` matches the assignment id and any state other than `ASSIGNMENT_ACCEPTED`.
An unreadable roster prints one anomaly and returns false.
`assign.py` checks the family before the pending record.
A family change still reaches the switch path.
A retry for the published destination restores the saved extract before its assignment.
It reuses that destination instead of creating another backend.
An accepted retry prints `unchanged`.

`assign.py` routes a Codex family change or pending delivery to `codex_mesh.py start`.
Its `--assignment-file` names the original prompt, so a retry computes the same assignment id.
`start` has no image option, so the generated prompt names each attached image path.
An accepted record or a record for another prompt keeps the normal follow path.
`assign.py` starts Claude implementation, test, and repair workers through `assign_claude.py`.
`review.sh` has no pending-dispatch check.
Its follow path does not restore a saved extract.

### Claude coordinator and friends

`scripts/direct/claude_worker.py` runs the Claude launch steps in the assign process.
`assign_claude._run` calls its functions directly.
The module has no command line or shell protocol.
It reads `CLAUDE_WORKER_REGISTER_WAIT_SECS` before it stops a source.
It verifies the destination against the running Claude registry before staff publication.
It records the role before delivery.
Delivery starts one Python child through `scripts/lib/py`: `send.py --to session:<id> --from launcher --file <prompt>`.
`CLAUDE_WORKER_SEND_SCRIPT` selects the delivery script.
`assign_claude.py` records the worker after publication.
The heartbeat watch follows the assign process during the wait.

A retry that finds its original user prompt in the transcript records acceptance without another send.
A complete transcript without that prompt permits delivery.
A torn transcript without that prompt refuses uncertain redelivery.
The refusal says `Claude prompt delivery is unknown: transcript has a torn JSON line; resend refused`.

`worker_fill.py end-friend` stops a friend in process.
For Codex, a failed `command_end` still reaches the server stop.
A failed staff retirement also reaches the server stop before the command returns 1.
A failed server stop keeps the retry eligible to stop that server again.
The command writes `ended` only after a confirmed stop or confirmed absence.
Claude friends use the same completion rule through their stop result.

### Account selection

The [account design](accounts.md) owns the shared workers account selection and its authority on natedev.
Both machines resolve that selection through `role_map.py`.
The selected account is separate from a worker's thread and role.
Account moves preserve those threads while the worker machinery changes the serving home.
Claude assignment fallback does not replace the selected Codex account.
The lifecycle and held-role rules below still apply during a move.

### Worker lifecycle in `scripts/agents/codex_mesh.py`

Each run has one detached `codex app-server --listen ws://127.0.0.1:<port>`. Each worker is a named, non-ephemeral thread on it.

**Protocol.** Initialize with `capabilities.experimentalApi=true` for queue calls. Pass a `SandboxMode` string in `thread/start.sandbox`. Worker threads remain non-ephemeral because ephemeral threads reject queued submissions.

Every `turn/start` request sets `approvalPolicy` to `never` and names its `sandboxPolicy`.
The server keeps the thread's last turn policy, so resumed workers also need both fields.

**Launch retry.** A nonresident launch can retry once after a fast failure from an inherited server. Eligible outcomes are `FailedBeforeThread` and `FailedWithThread` containing `STALE_SIGN_IN`. The failure must occur within 120 seconds.

**Settlement.** A launcher remains attached while queued work, pending messages, or buffered notices remain. A missing completion notice requires idle-thread evidence before settlement. Settlement without an answer reports failure.

**Server environment.** `_start_server` selects the workers account home and filters the child environment through `staff.child_environment`.
The [registry boundary](agent-registry.md#agent-environment-boundary) removes operation snapshots before the app-server starts.
For a unit run, the server receives that run's `UNIT_RUN_DIR` and private `tmp/` folder.
Worker children inherit these values for board coordination and reservation checks.
A mesh folder outside the run roots retains the caller's run settings.

For unit runs, `start`, `prime`, an explicit `follow --cwd`, and resident turn transitions pass their known cwd to `ensure_server`. Direnv's nearest `.envrc` at or above that cwd selects the dev-shell folder, so turns in worktree subfolders share the same server. `direnv status --json` must identify that file as allowed (`state.foundRC.allowed == 0`) before the server starts through `direnv exec <dev-shell folder> env <managed assignments> codex …`. The approval check does not evaluate the shell. A missing or unapproved `.envrc` selects the ordinary launch. A missing direnv, failed or timed-out probe, or unreadable status is an unknown result: it keeps a healthy recorded server and its saved dev-shell preference. Without a healthy server, an unknown result starts an ordinary server.

There is no wrapper on worker commands: the app-server passes the loaded dev-shell to all its children. The assignments after direnv restore `CODEX_HOME`, `UNIT_RUN_DIR`, `TMPDIR` and any managed held-role identity keys, so `.envrc` cannot replace them. [Direnv uses `syscall.Exec`](https://github.com/direnv/direnv/blob/v2.37.1/internal/cmd/cmd_exec.go), and `env` also executes its command in place: the recorded pid is the app-server's own pid. Plain launches retain the 30-second `SERVER_START_TIMEOUT_SECS` budget and sweep record-start window. Only dev-shell launches get `DEV_SHELL_START_TIMEOUT_SECS` (300 seconds) for cold direnv/Nix evaluation. Every launch writes `mesh_server_start.json` with its pid, port, start time and complete serving configuration, including a dev-shell folder when one applies. The sweep discovers this temporary record under the run roots even without a recoverable log handle, and keeps that server through its dev-shell startup budget. The marker is removed after complete current-server publication or confirmed process exit; an unconfirmed live start retains it for reconciliation. Expired markers do not extend the plain record-start window. If that launch cannot execute, exits, or times out, the failure and direnv's stderr go to `mesh_server.log`; the same start path retries with the caller's environment and records a plain server. `mesh_failed_dev_shell.json` remembers the failed folder, an input revision and a retry deadline 30 minutes after the failure was written. The revision covers `.envrc`, `flake.nix`, `flake.lock`, `shell.nix`, `default.nix` and `devenv.nix` in that folder, using each file’s nanosecond modification/change times and size, or its absence. The remembered-failure type owns both the input key and the deadline. Until the deadline, an unchanged revision makes a cwd caller reuse the healthy plain fallback and keeps restarts plain, instead of repeating the failed evaluation. Editing, creating or removing a tracked input, or reaching the deadline, permits a fresh attempt. Older failure records without the input key and deadline also permit a fresh attempt. A successful dev-shell start clears the failure record. The failure check runs under the server lock, so queued callers observe a failure saved by the preceding launch. Failed-start cleanup signals the whole process group, including evaluation children. No live worktree evaluation was measured for this change.

The server record stores `dev_shell_worktree` for a successful dev-shell launch. When a cwd caller receives a definite probe answer and the required dev-shell differs from the record (including a legacy record with no dev-shell), `ensure_server` retires the server and starts its replacement through the same path as an account or compaction-limit change. Existing turns retain the retired server until released. This also removes a previously loaded shell when its `.envrc` disappears or is no longer allowed. An unknown probe answer does not retire the server for a dev-shell mismatch. A caller without a cwd never retires a server for that reason.

`mesh_dev_shell.json` in the session folder stores the last successfully served dev-shell folder as `worktree`, independently of the current server record. When `compact`, `serve`, `fork`, or another cwd-free caller must restart after idle-stop, it rechecks that folder's approval and uses the same dev-shell when still allowed. A definite answer selecting the ordinary environment clears this preference; an unknown answer preserves it for a later retry. A pre-existing server record can also supply the folder when the preference has not yet been written. Shared mesh folders outside unit runs never load a worktree's dev-shell.

The berth hook takes the session directory from `UNIT_RUN_DIR` when it names a folder that exists. When the variable is unset or its folder is gone, it reads the director's run marker (`unit_runs.marker`), so a stale variable never lets an edit through unchecked. The marker names one run, so a worker in a director's side run would otherwise check the main run's reservations.

The verbs:

| Verb | Signature | Behavior |
| --- | --- | --- |
| `start` | `--session-dir --name [--staff-id] [--assignment-file] [--role impl\|test\|fix\|review] [--lens] --cwd --prompt-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--sandbox danger-full-access] [--resident]` | Opens a thread and blocks until its turn ends. `--role` is optional, and roleless threads such as ask-a-friend friends leave it out. A given role, with `--lens` when passed, is appended to `agents.jsonl` for the new thread. |
| `follow` | `--session-dir --to --role (required) [--lens] [--claim-pid] --message-file --summary-file [--reply-file] --log-file [--model --effort --service-tier] [--cwd] [--read-only] [--image …]` | Runs one more turn on a finished thread. If the old server is gone, it resumes the thread on a new one. `--cwd`, `--read-only` and `--image` apply to that turn only and also apply to unit-owned reviewers; without them the request is the worker's usual one. |
| `prime`, `fork`, `reset` | `--session-dir`, worker name and command-specific arguments | Prime a thread with standing instructions, fork a named thread, or remove turns from an idle thread. Held roles restrict these operations as described below. |
| `compact` | `--session-dir --to` | Exits 2 for a worker that is running or busy, a worker with no recorded context size, and a worker whose size is `not_measured_since_compaction` (`size not measured since its last compaction; compact refused`). Otherwise it reads the entry's `revision`, resumes the thread, waits up to 60 s (`COMPACT_IDLE_WAIT_SECS`) for a turn still live on it to end, exits 3 (`COMPACT_TURN_STILL_LIVE`) if one stays live, and calls `thread/compact/start`. It waits until the compaction turn has a `thread/compacted` notice or a `contextCompaction` item, a token-usage update and a `turn/completed`. It then writes the new size under `RosterUnchanged(thread_id, revision)`, so a turn that wrote the entry in between keeps its record. A refused write exits 4 (`COMPACT_ROSTER_CHANGED`), reports the current status, thread, turn and revision, and refuses the follow-up. Old followers detach once their launcher no longer owns the entry; broadcast compaction notices cannot change, settle or clean up a paused, finished or newly claimed worker. A size above 0 prints `compacted <worker>: <before> -> <after> tokens`; a compaction turn that reports 0 stores `not_measured_since_compaction` and prints `compacted <worker>: <before> tokens, size not measured until its next turn`. A compaction that has not finished in 300 s (`CompactionTimedOut`) is interrupted with `turn/interrupt` and the thread is read until idle, up to the same 60 s: idle is exit 1 with `…; interrupted the compaction turn, the worker is idle and not compacted`, so a follow-up can still be sent; a turn still live is exit 3. |

| `can-follow` | `--session-dir --to --role [--lens] [--claim-pid]` | Refuses a worker that cannot take a follow-up. With `--claim-pid`, it claims the worker under the roster `flock`: it sets `status: starting`, `launcher_pid`, and `previous_status`. It records the task role and lens in `agents.jsonl`. |
| `release-follow` | `--session-dir --to --claim-pid` | Releases a claim that never started a turn. It restores the previous status and records the restored task role in `agents.jsonl`. A follow that did run keeps the role it was claimed with, so after a completed review the worker's role stays `review`. |
| `end` | `--session-dir --to` | Clears the pending queue, writes the end marker and sets roster `status` to `"ended"`. Keeps the staff row and thread id. A second `end` retries cleanup. Permanent removal retires the staff row. |
| `stop` | `--session-dir [--pause]` | Sets every roster worker to `"ended"` and stops the current server and every retired one. With `--pause` (a shutdown stop, `docs/as-built/shutdown-and-restart.md`) it instead sets every non-ended worker to the followable `"paused"`, with `paused_from` and `paused_turn`, after dropping its queued messages and interrupting a live turn. |
| `reopen` | `--session-dir --to` | Returns an `ended`, `running`, `starting` or `waiting_capacity` worker to `"paused"` on its recorded thread when its launcher is dead. Requires positive idle evidence when recovering a live status or recorded server. Clears the stale turn id and stores `not_measured_since_compaction`. Prints the same thread id. A live turn, unreadable thread, missing thread id or live launcher refuses recovery with exit 2. `assign.sh --to` calls it before dispatch. |
| `sweep` | `[--root] [--stop]` | Gives a verdict on every app-server listening on `ws://` (see Cleanup). |
| `idle-stop` | `--session-dir --pid --port` | The server's own watcher calls this. It stops the server, or touches its record if something still uses it. |
| `list` | `--session-dir` | Prints the roster. |

A held design role (a roster entry with `held_design_id`, `docs/as-built/long-lived-workers.md#lasting-roles`) changes only through `scripts/astra/design_roles.py`: `start`, `prime` over it, `fork` from it, `reopen`, `set-instructions`, raw `stop` and `stop --pause` refuse it, `follow` takes only its `authorized_prompt` (or an account-move continuation), and `send` queues the text in the role store.

`follow` and `start` send the resolved model on every `turn/start`.
The [configuration lifetime](agent-registry.md#configuration-lifetimes) applies to each submission, including retries and continuations.
The thread keeps its context.
Saved model and effort values describe accepted turns; they do not select future settings.

### Roster fields

A `mesh_roster.json` entry stores `staff_id` and run state.
It stores no copied thread id, session id, account home, manager, family, role, or worktree.
`session_staff` resolves those facts through staff and the current runtime.
The assignment log supplies task roles and lenses.
`task_role` maps `astra-reviewer` to `review` and `astra-designer` to `impl`.
An unavailable usage log does not stop worker selection.

Run state includes these fields:

- `status`: `starting`, `running`, `done`, `failed`, `paused`, `waiting_capacity`, `capacity_exhausted`, or `ended`.
- `turn_id`, `launcher_pid`, and serving `port` describe the current turn.
- `context_tokens` records input size or `not_measured_since_compaction`.
- `revision` increases on each locked publication and protects conditional writes.
- `previous_status`, `paused_from`, and `paused_turn` support claims and moves.
- `turn_model` and `turn_effort` describe accepted turns; they do not select future settings.
- `instructions_file` and `access_policy` preserve standing instructions and access limits.
- `assignment_id` and `dispatch_state` preserve delivery progress.

A stream owns work only while its live status and launcher pid still match.
An old stream cannot settle a paused, finished, or newly claimed entry.
Terminal writes check ownership under the roster lock.
Server reads, cleanup, and summary delivery run outside that lock.
`ReadOnlyReviewer` selects read-only access. `PhaseBoundDesigner` supplies its launch sandbox.
The policy never comes from instructions text.
The [session roster](session-roster.md) derives display identity from these references.

**Overflow press-on.** When a turn ends with `codexErrorInfo: contextWindowExceeded`, `start` or `follow` does three things in the same call:

1. Posts a board status line: `context window filled; sent "Continue where you stopped."`.
2. Starts a continuation turn with that text.
3. Sets a guard flag.

The flag is cleared by any `item/completed` from the worker's own work, but not by a `userMessage` or `contextCompaction` item. A second overflow while the flag is still set stops the worker with `context window filled twice in a row without progress; automatic continuation stopped`. Overflows that have work between them get one continuation each, with no limit.

### Launchers

**`bash scripts/direct/assign.sh --role <implement|test|fix> --session-dir <run> --cwd <worktree> --prompt <file> --phase <phase> --paths <file>`**

`assign.py` resolves the family, model, and effort once.
For Claude implementation, test, and repair work, it calls `assign_claude.py` in the same process.
`assign_claude.py` has no command line.

- Without `--to`, assign starts a worker and records its staff identity.
- With `--to <worker>`, assign continues that worker or completes a family change.
- The Claude path checks admission before it records the pass.
- A claim protects a continued worker until the dispatch ends.
- Each slot keeps its `impl_status_<slot>`, `impl_summary_<slot>.txt`, `impl_agent_<slot>.log`, and `impl_agent_<slot>` files.
- `UNIT_WORKER_POLL_SECS` overrides the worker poll interval from `CLAUDE_WORKER_POLL_SECS`, which defaults to 15 seconds.
- `assign_claude.py` releases the claim and stops the heartbeat watch when the dispatch ends.

**Codex code review dispatch.** Use `assign.sh --role review --session-dir <run>
--phase <phase> --prompt <file> --cwd <worktree>`. Each Codex broad-review lens has
its own dispatch; closure review omits the lens.
Each artifact uses the worker name: `<worker>_status`, `<worker>_summary.txt`,
`<worker>_agent.log`, and `<worker>_reply.txt`.
Reviews add `_<lens>_<pass>`, or `_<pass>` without a lens.
The launcher claims the worker before it writes these artifacts.
A refused claim leaves the artifacts unchanged.
Use `arm-review --worker <worker>` for an early named reviewer.
Its marker reads `<worker>_status_<lens>` and `<worker>_pid_<lens>`.
Legacy `review.sh` markers retain their `review` prefix. The returned handle names its
status, log and summary. The allocator uses `review`, then `review2` when the
first worker is busy. Both threads stay read-only.

`review.sh` remains a compatibility launcher. With no recipient it resolves
the unit's own reviewer. `--to` follows an explicit worker and `--slot` names
an explicit extra worker. Both use read-only access. Any other option, such as
the retired `--pool` or `--lease`, exits 2 with `unknown review option` and the
flag name.

Every review attempt records `arm: own`. Records under
`review_turns/<run>/<started>-<pid>-<sequence>.json` keep separate attempts from
one launcher. History reads both earlier `arm: pool` and current `arm: own`
records. No recorded review differs from a recorded review with zero findings.

An explicit `--to <worker>` compacts under `impl_worker_claim.lock`, then
claims with `can-follow --role review` before writing artifacts. Compaction
exit 3 (live turn) or 4 (roster changed) refuses the follow-up; exit 4 advises
inspection and same-thread `reopen` when idle. Other compaction failures are
best-effort, with one board line. The launcher releases the claim on exit.
Each attempt records cancellation on TERM, INT or HUP. Explicit extra slots
retain their unit worker identity for later implementation or repair work.


### Choosing a worker for a review

The rules live in `commands/unit/direct.md` `<LongLivedWorkers/>`, `docs/direct/dual_review.md` (`<TeamReview/>` and `<ReviewPromptContract/>`), `docs/direct/run_phase_review.md`, `docs/direct/write_prompt_contract.md` (`<RoleReassignment/>`) and `commands/plan/phase_review.md`:

- Each Codex lens dispatches the unit's own read-only reviewer with `assign.sh --role
  review`. The allocator reuses a thread before opening the next concurrent slot.
- Only the adversary lens starts early. Keep its dispatch handle and selected
  slot for Stage 2 and cancellation. Closure review uses the same role without
  a lens.
- Each prompt tells the reviewer to read the tree again, names the expected HEAD,
  and keeps `config/review/code.md` as its standing instructions.
- Each prompt carries the Three Gods sentence and the Type Design Contract.
  Compaction can remove earlier text, so later dispatches repeat them.

`scripts/direct/progress_history.py` gives each extra worker (`review2`, `fix2`, …) its own `Agent N` number after `impl` and `test`, in the order workers first appear: a `- **Agent N**` line in every report, and a round-table column under `progress --tables`. A worker appears when it records a pass or receives a board handoff. The original `review` worker keeps its earlier rule: it shows only while a displayed stage assigns it.

### Resume messages

`resume_message` builds one prompt for resumed conversations.
`ResumeWorkers(results, cwd, panes)` combines `Resumable`, `Idle`, and `Unavailable` results with pane worker states.
`PausedSeat` retains the saved launcher arguments and fixed staff reference.
`worker_resumes` reads direct worker children across Claude and Codex.
`AccountMove` names the source and destination accounts.
A busy recipient gets only its `own_pass` continuation.
An idle worker row receives no prompt.
A director can receive the list of busy and idle worker children.
Restore reopens only workers with active dispatches.
It reads each worker's dispatch record and transcript once to select the resume step.
An accepted dispatch gets a continuation prompt only when the worker is idle.
Restore holds the dispatch lock across the record read, receipt read, and prompt delivery.
An unsubmitted dispatch keeps the saved assign command as its step.
Restore, named resume, and director notes use the same receipt rule.
Director notes keep the saved prompt path and reuse shared dispatch history reads.
A dispatch with a DONE line or a finished record gets no resume note or step.
Later chat does not reopen a completed dispatch.
The restart shares its run index and snapshots. Idle workers stay closed until their next assign.
Account moves must preserve closed idle workers.

### Cleanup

- **Worker end.** `codex_mesh.py end` stops the recorded servers when every roster worker has status `ended`.
  `_reap_validated_servers` and `_release_held_server_locked` use the same identity step, `_reap_recorded_server`.
  This step signals only a process whose pid and `app-server --listen` port match the record.
  A failed process scan prints an anomaly and sends no signal.
  It reports another live process and forgets its record without a signal.
  It stops them before staff retirement, including when retirement refuses.
  Another open worker keeps the servers.
  Held design roles keep their existing lifecycle.
- **Phase end.** `<PhaseCleanup/>` runs `remove_workers.py --session-dir`. It leaves Codex threads open and preserves Claude workers held by the unit ledger or a live unit director. It does not launch server reclamation.
- **Run end.** `scripts/direct/end_session.sh` acts on every run folder the ending director holds, not only the one its marker names: a director runs independent phases side by side, each in its own run folder. `unit_runs.py held <session id>` lists them, the marker's run first, then every folder under the run roots whose `unit_runs.director(run)` is the session. The scan uses `unit_runs.run_folders`, which excludes each root's `active` marker folder. The marker's run is kept even when the scan fails. A run of another director is never touched. The steps, in order:
  1. `design_roles.py unbind-run <run>` for each held run, which unbinds every held design role bound to it and leaves its thread, prompts and replies (`docs/as-built/long-lived-workers.md#lasting-roles`); a failure stops the script before any other step
  2. one `remove_workers.py` call with a `--session-dir` per held run
  3. `codex_mesh.py stop --pause` for each held run with `mesh_roster.json`, `mesh_server.json` or `mesh_retired.json`. A live unit director's workers, queues and servers remain untouched. Otherwise pause preserves pending messages, owed continuations, reservations and detached commands before stopping only that run's recorded servers.
  4. one `remove_run_scratch.py --tracked` call for every held run: it removes `verify_cache/` and git checkouts and cargo targets at any depth, without descending into what it removes or following a link. It keeps any checkout or tracked scratch folder containing the working directory of a live detached command belonging to a worker that is not retired. A linked worktree goes through `git worktree remove --force --force` in its source repo, or is deleted and its source pruned; ledgers, logs, diffs, json and the run folder stay.
  5. removal of the session's own run-active marker. Judging another session's marker is separate: `unit_runs.py removal-check` keeps the former folder while a marker's owner is live or unknown.

  Steps 1 to 4 run even with no marker, for runs the session still holds. Steps 2 to 4 each continue after a failure. Run end does not launch a sweep. Tests point the scan at a temp folder with `UNIT_RUN_ROOT` and the marker with `UNIT_ACTIVE_DIR`.
- **Sweep.** It first decides who owns each `ws://` app-server: the current server of a run, the current server of a held design role, a retired server of a run, an unowned server whose log names its run, or an unowned server with no log. Alongside the run folders selected by `--root`, it reads the durable design mesh folders returned by `design_roles.durable_meshes()` (folders with `mesh_roster.json`). A matching current server record belongs to a held design role only while its roster has an entry with `held_design_id`; a released role or deleted folder gets the usual ownership rules. It then judges whether anything still needs the server:
  - Any connected client means in use.
  - For a current run server, a run-active marker or a running worker means in use.
  - A retired server is unused unless a conversation is mid-turn or a loaded thread has queued or unreadable work.
  - An unowned server is in use during the record-start window, while a conversation is mid-turn, or when a loaded thread has queued or unreadable work.
  - Current run servers and held design servers share the 30-minute quiet window (`SERVER_IDLE_SECS`): a live worker launcher, a server under 30 minutes old, a mesh folder changed in the last 30 minutes, or a conversation mid-turn means in use.

  It reads `thread/queue/list` for loaded threads; queued submissions or unreadable queue state keep the server. It prints `pid  port  verdict  reason  owner` with verdict `in use`, `unused`, `stopped` or `kept`. With `--stop`, it judges a second time under the owner's mesh folder server lock before stopping a server. Ordinary idle-stop and retired-server reclamation can free an unused server while the director lives; the next dispatch resumes the same worker name and thread. Servers listening on `--listen unix://` are never touched. Each server also has its own watcher, which wakes every 300 s and runs `idle-stop` once the server record has gone 30 minutes untouched. If a server fails to start, its process is terminated and its record dropped.
- **Sign-in change.** `signin-changed` retires servers that hold the old sign-in. Running workers are interrupted and resumed on a new server. The move selects a recorded server whose loaded threads include the worker's thread. Unreadable server evidence refuses the move. Proven absence permits a marker with the roster's last `turn_id`, without queue transfer or interruption. `assign.sh --operation resume-account-move` resumes that thread on a new server, even when its launcher died. Idle workers move at their next turn.

`scripts/direct/workers.py` stores each unit ledger at `~/.local/state/unit-workers/<staff id>.json`.
`UNIT_WORKERS_STATE` selects a private root.
The ledger stores fixed staff references, access policy, ownership, and events.
Staff supplies current names, parents, worktrees, and backend identities.
A rename or transfer preserves those fixed references.
`rename.py` changes the unit row and repairs its session and display names.
A changed unit name changes matching folder and branch suffixes.
`--to-branch <name>` sets an explicit branch.
Every unit branch change pushes the target to origin before sessions resume.
Equal old and new unit names keep the folder and staff name.
See [production rename](production-rename.md) for unit branch moves.

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
| `scripts/direct/assign_claude.py` | Claude writer, tester, and repair dispatch inside assign; identity checks and claims for `--to` |
| `scripts/direct/assign.py`, `assign.sh` | Unit role allocation, persistent reviewer dispatch, read-only follow and own review records |
| `scripts/direct/review.sh` | Compatibility flags, explicit recipient follow and own allocation |
| `scripts/direct/end_session.sh` | Unbinds held design roles, preserves a live director's workers and servers, otherwise pauses recorded runs; no sweep |
| `scripts/direct/remove_run_scratch.py` | Removes run and tracked scratch while preserving live detached commands' working directories |
| `scripts/direct/remove_workers.py` | Removes unheld Claude workers at phase end (`--session-dir`, repeatable); preserves unit workers; no sweep |
| `scripts/lib/unit_runs.py` | `held`: every run folder a director holds |
| `scripts/direct/progress_history.py` | Agent columns for extra workers |
| `scripts/whoami/worker_usage.py` | Before/after usage report over the usage store |
| `scripts/usage/weights.py` | Dated per-model weights the report applies |
| `scripts/usage/rollouts.py` | `is_edit`, used by the collector |
| `scripts/whoami/agent_notes.py` | `credits` on Codex readings |
| `config/direct.conf` | `UNIT_COMPACT_ABOVE_TOKENS=100000`. `assign.py` compacts an idle mesh worker above it before new work. A pane worker has no in-turn compaction option. Codex keeps its own full-window limit. |
| `commands/unit/direct.md` | Defines `<LongLivedWorkers/>`, with references from `<PhaseCleanup/>` and `<CompactionContract/>`. |
| `docs/direct/repair_rounds.md` | References `<LongLivedWorkers/>` from `<FixDispatch/>`. |
| `docs/direct/early_review.md` | Defines `<EarlyReviewArm/>` and uses the early review worker (`EARLY_REVIEW_SLOT`). |
| `docs/direct/launch_implementation.md` | References `<LongLivedWorkers/>` from `<LaunchImplementation/>`; uses `codex_mesh.py list`, then `--to` for every slot with an open worker. |
| `docs/direct/dual_review.md`, `docs/direct/run_phase_review.md`, `commands/plan/phase_review.md` | Review worker selection and `--to`/`--slot` calls |
| `docs/direct/write_prompt_contract.md` | `<RoleReassignment/>`; Three Gods and Type Design in every prompt |

## Invariants

- `assign.py` ends a failed launch's run when no pass completed, no other worker exists, and the director holds only that run.
  The worker command must have started and returned an error. A launcher signal does not trigger this path.
  Each recorder slot counts as one worker, even without a worker name. A Claude launch must add its slot's `pass_started` event.
  Herdr exit 2 qualifies; exits 3 through 8 retain the run.
  The launcher calls `progress_history.py finish-run --session-dir <run> --status error`, then `end_session.sh`, through `call(check=False)`.
  Cleanup errors preserve the launch exit code. The phase reservation stays until a checkpoint releases it.
  `prepare_session.sh` and `end_session.sh` select `CODEX_THREAD_ID` before `CLAUDE_CODE_SESSION_ID`, as `staff.runtime_id()` does.
  A retry without `--session-dir` starts a new run. An explicit run folder retains its existing retry behavior.
- Workers retain their staff identity across runs. Allocation creates a worker only when no available worker can accept the task.
- Both worker families preserve their fixed staff ids across a family switch.
- The unit director starts each next worker turn through `assign.sh`.
  Peer messages wait for that turn and arrive before its dispatch prompt.
- Compaction, the worker claim and the identity check run under one launcher lock (`impl_worker_claim.lock`); each mesh operation also holds its worker lifecycle lock. A live compaction turn (exit 3) or changed roster (exit 4) blocks the follow-up. Other compaction failures post one board line and allow the follow-up.
- A worker identity file names its fixed staff id. Saved model values do not select the next dispatch.
- An assigned review claims its worker before writing any artifact.
  It fails if HEAD, status, tracked changes, or untracked content changes.
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
- Worker reuse follows staff identity, task role, and availability.
- A released unused claim restores the prior task role in the assignment log. A completed review retains its review assignment.

## Design rules

- Persistent threads retain worker context between assignments.
- Compaction between assignments limits repeated context reads.
- Unit reviewers use a read-only sandbox. Writers keep their writing roles.
- Checkpointed phases provide the comparison unit for usage reports.
- The second-overflow error stops repeated continuations without worker progress.
- Edit detection recognizes `tools.apply_patch(` outside quoted text.

## Lasting roles

A design has one designer slot and one reviewer slot.
Its open workers retain fixed staff ids across rounds, bindings, restarts, and account moves.
The worker row's parent supplies its holder. The design stores no copied holder identity.
`worker_parent` resolves that parent from the supplied staff snapshot.

### Store and types

`design_roles.py` stores each design under `DESIGN_ROLES_STATE`, defaulting to `~/.local/state/astra/design-roles/<design id>/`.
`design.json` stores placement, repository, document, area, binding, role slots, and assignment claims.
`mesh/` stores mesh execution state when that transport applies.
`prompts/`, `replies/`, and `pending/<role>/` retain work and queued messages.
`transition.json` retains an incomplete lifecycle operation.
The store has no roster-adoption path or adoption redirect records.

The binding is `DesignUnbound` or `DesignBoundToPhase(run_directory, phase, repository, output_paths)`.
The lifecycle is `RoleNotOpened(claim)`, `RoleOpen(staff_id)`, `RolePausedForMove(staff_id)`, `RoleNeedsRecovery(staff_id, reason)`, or `RoleEnded(staff_id)`.
`HeldWorker.thread_id` derives the current thread from staff.
`ReadOnlyReviewer` and `PhaseBoundDesigner` carry the access policy.
An unreadable design prints one warning per operation; other designs remain readable.

### Operations

| Operation | Current behavior |
| --- | --- |
| `open` | Reserves placement, validates the binding, records creation intent, and starts through the worker launcher. An uncertain start retains its claim. |
| `resume` | Requires an idle open role and valid binding. It records authorization, saves the headed prompt, and dispatches the assignment. |
| `recover` | Proves the recorded worker idle and clears its uncertain claim. It starts no replacement identity. |
| `rebind` | Requires a reason and the expected generation. It releases servers before publishing a new binding, checkout, or document. |
| `end` | Stops the worker and confirms release. Failed cleanup gives `RoleNeedsRecovery`; successful cleanup gives `RoleEnded`. |
| `unbind-run` | Removes the run binding without ending the role. |
| `list` | Reports roles and problems. A local read needs no network request. |

A pane binding uses `worker.py` operations. A mesh binding uses `assign.sh` for prime and follow operations.
`worker.py list --json` gives each pane worker row `name`, `id`, `current_session`, `state`, `role` and `activity`.
It also computes three fields at read time and stores none of them:
- `work`: `idle`, `busy` or `unknown`, from `worker_panes.worker_state`.
- `account`: the label of the account the member opens on, from `member_account.resume_account`.
- `home`: that account's home as an absolute path.
With no staff backend entry, `account` is `{"state": "AccountUnavailable", "reason": ...}` and `home` is absent.
An ended design identity cannot reopen.
`config/design_roles.conf` controls new role creation.
`[roles] new_open=false` preserves existing identities and refuses new opens.

The authority host comes from `[ownership]` in `config/design_roles.conf`.
Both roles remain on the design's recorded host. Host transfer is unsupported.
Repository mappings identify each host's absolute checkout.
Supply every host mapping on the first registration. Use `register_repository` for multiple mappings. A later `register` must repeat identical mappings.
Placement uses a hash of repository id and relative document path.
The authority supports registration, reservation, lookup, generation advance, checkout changes, and aliases.
An unavailable authority refuses the operation without changing placement.
Renames and transfers retain worker staff ids.
They do not rewrite design holder records.

### Dispatch and messages

`resolve_model` resolves `design_roles.designer` or `design_roles.reviewer` through the shared registry.
A designer needs a valid phase binding and authorized output paths.
A reviewer uses read-only access.
`resume` writes one assignment claim and includes pending message ids in the saved prompt.
`_validate_held_claim` requires the recorded thread, current generation, matching assignment claim, and no pending binding transition. `_claim_follow` removes `authorized_prompt`, so one authorization starts one turn.
Success removes only the included pending messages.
A failed dispatch retains pending input and records recovery state.
Raw mesh commands cannot bypass held-role authorization.
A held role's `send` queues text for its next authorized assignment and does not wake the role.
Detached results use stable message ids and the same pending store.

`check_patch` checks `apply_patch` paths against the binding generation and phase reservation.
It rejects a patch outside the bound paths or repository.
The check covers patches, not arbitrary shell or Python writes.
A binding change releases queues and proves idle before it releases the server.
The server environment comes from the current design context and staff holder.
`_design_context` normalizes values through JSON before comparison with the server record. This makes tuple output paths match saved JSON arrays.

### Astra work header

`scripts/astra/astra_work.py` saves assignment identity before dispatch as the first line of a brief or prompt: `<!-- astra_work {JSON} -->`.

- Frozen types: `PhaseWork(run_directory, run_id, phase_id, phase_instance_id, history_file)` (`kind="phase"`), `StandaloneWork()` (`kind="standalone"`), `AstraWork(role, member, document, assigned_at_utc, reply_path, work)` (`schema=1`, role `astra-designer` | `astra-reviewer`), and read results `HeaderMissing(path)`, `HeaderInvalid(path, reason)`.
- `phase_work(run_directory, phase_id) -> PhaseWork` reads `progress_history_state.json`: `open_phases` plus the `phase` when its status is `active`. Exactly one match or `ValueError`.
- `header_line(work) -> str`; `read_header(path)` checks the exact key sets, `schema` 1, the role and a UTC `assigned_at_utc`.
- CLI `astra_work.py brief --run-directory --phase --member --document --round --text-file` writes `<run>/design/phase<id>/round<N>_brief.md` by exclusive create, with `reply_path` `round<N>_reply.md`; it refuses (exit 2) when that reply exists. Holders use it only for a designer that `design_roles.py` does not hold, and save the reply there without overwrite.
- `astra_review.py [--run-directory <dir> --phase <id>] <document> [focus]` takes both flags or neither. A one-turn review writes the header as the first line of `/tmp/claude/astra/<stamp>-<pid>/prompt.md`, with `member` the run folder and `reply_path` its `review.md`. Exit 2 also covers an unresolved phase.

### Census for cargo-handler

`astra_roles.py` stores one record at `~/.local/state/astra/roles/<staff id>.json`.
`ASTRA_ROLES_STATE` selects another root.
`RoleRecord` stores `staff_id`, `held_at`, `session_dir`, `worker`, and `pid`.
Durable holds also store `design_id` and `role_id`.
The record stores no copied role, thread, account home, or manager.
`HeldRole` and `LiveRole` derive those facts through staff and the assignment log.
Their lifetime is `MeshWorker`, `ExecProcess`, or `HeldDesignRole`.

`hold(role, staff_id, ...)`, `release(staff_id)`, and `live_roles()` manage the census projection.
A missing runtime does not erase a durable design identity.
Cargo-handler reads staff names and parent relationships directly from staff schema 2.
The [session roster](session-roster.md) displays open design roles through their current staff identity.

## Worker operation fencing

`scripts/agents/worker_lifecycle.py` owns the per-worker flock at
`<session>/<name>.lifecycle.lock`. `lock_worker` takes one identity;
`lock_workers` canonicalizes and sorts identities for a transfer. Recursive
acquisition by the same thread fails immediately and names both call sites.
Competing threads and processes serialize on the flock.

The operation boundary runs from the authoritative identity check through
server effects to final roster publication. Settlement, pause, compact, claim,
claim restoration, release-follow, start, submission, replacement-thread
publication, account-move resume, reopen, end, reset, and bulk stop use it.
Worker parent changes and held-role recovery use the same lock module. Normal turn watching and capacity backoff release
the lifecycle lock. Ledger telemetry refresh runs after releasing lifecycle
and inbox locks, then reacquires ledger before lifecycle and roster.

The lock order is unit ledger, design, run membership, staff row, worker lifecycle, server, inbox, then roster.
`membership.lock` serializes run admission and shared-server maintenance.
Multiple runs use sorted path order.
Admission retains membership through roster publication.
Staff slot and row locks protect native identity publication.
Lifecycle, inbox, and roster locks release before membership.

Bulk pause, stop, held-server release and server reaping own membership through
cleanup. `_bulk_workers` acquires every lifecycle, then returns fresh validated
records; role, transfer and ownership refusals precede mutation. Effects use
only that work set. A changed set during acquisition is retried within the
same deadline. `_server_lock` serializes server selection, retirement, reaping
and conditional record removal; cleanup never removes a replacement record.
Held-server refresh explicitly passes its membership lease and lifecycle
ownership. A partially owned worker set refuses with recovery instructions.
Account moves pass bulk ownership through interruption and release all these
locks before waiting for continuations. Acquisition timeout reports incomplete
shutdown, without claiming a turn or server has stopped.

Shutdown establishes one monotonic deadline before director and ledger lookup. `worker_lifecycle.operation_deadline` carries
that budget through callbacks and nested helpers; an omitted deadline inherits
it, and an explicit limit can only shorten it. Nested scopes restore their
parent on every exit. New threads and processes must receive the absolute
deadline explicitly. `acquire_flock` is the sole acquisition primitive for
shared, exclusive and immediate try-locks, including roster/server-record
reads, ledger migration, design locks and inboxes. Ownership bookkeeping is
thread-local and takes no auxiliary mutex. Read fallbacks propagate lock
expiry as `OperationDeadlineExpired` to the incomplete-shutdown report.
Ordinary socket timeouts retain their queue-preservation and recovery results.
Legacy `Client(deadline=...)` and frame-read deadlines use wall time and are
converted once to monotonic limits. `Client(operation_deadline=...)` accepts an
absolute monotonic shutdown limit. One setup deadline covers connection and
handshake, and one RPC deadline covers its send and every reply fragment;
neither renews the operation budget. The operation wins equal deadlines, while
a strictly earlier transport limit keeps its transport result even if handling
runs late. A tighter active scope also applies to an existing client.
Reaping reports success only after exit is confirmed within that same budget;
a refused signal or surviving server retains its records.

Settlement closes only its own inbox and checks ownership before cleanup and
terminal publication. A replacement-thread failure normalizes both port and
thread before its first ownership check. Ownership loss is permanent for that
subscriber; paused and newly claimed entries retain their pending messages.
Unwatched cleanup shares one `THREAD_RELEASE_TIMEOUT_SECS` deadline across
connection, initialization, queue draining, readback and interruption. A
released lifecycle lock is not proof that the turn stopped: follow still
requires positive idle evidence.

`assign.sh --to` calls `reopen` for an ended worker or a live status with a dead launcher.
Recovery checks the old server, resumes the recorded thread and proves it idle before dispatch.
A follow-up after reopen refuses a replacement thread if the recorded thread cannot resume.
Live or unknown state preserves pending work and names the cause.
Reopen probes without a roster flock, then rechecks the revision and launcher under that flock.
Compact does not recover a dead launcher; its refusal directs the caller to follow or reopen.
Without a live server, reopen starts a server and proves the recorded thread idle.
`end` keeps the staff row and thread id for recovery. Permanent removal retires the row.
`assign.py` ends an Astra reviewer, releases its role, and retires its staff row after the review.
The unit ledger keeps ended workers so unit retirement can find and retire their rows.


## Whole worker-state publication

Worker rosters and pending inboxes use permanent sidecar locks:
`mesh_roster.json.lock` and `<worker>.pending.json.lock`. Lock acquisition precedes
opening the data path and the lock stays held through publication. A locked
`WorkerStateFile` reopens the current pathname for each operation; it retains no
data handle across a replacement. These lock files are never renamed separately
or unlinked. The order remains unit ledger, design, membership, staff row, lifecycle, server, inbox, then roster. Every flock uses `worker_lifecycle.acquire_flock`
and inherits the active operation deadline, including assign lock users.

All worker-state publications use `atomic_file.write_text_atomic`: a sibling file
is flushed and synced, replaced, then its folder is synced. `AtomicPublicationFailed`
means replacement did not occur and the old file remains whole.
`AtomicDurabilityUnconfirmed` means complete new content is already visible but
folder durability is unconfirmed. Neither failure permits a blind rollback or
fresh allocation; recoverable transactions retain their journal and inspect the
visible state on retry. Temporary cleanup belongs to that shared publisher.

`WorkerStateUnreadable` names existing empty, invalid, wrongly shaped, or unreadable
worker evidence. Operational readers refuse it; server cleanup retains the server
and reports the doubt. Board narration omits unavailable enrichment. Missing files
remain distinct: `resolve` checks ledger-owned names and exact thread identities
before any new allocation, so a missing roster does not establish a new run.
A missing inbox has no persisted messages.
An empty legacy inbox reads as an empty queue.
An exclusive reader retains that damaged file beside the original before the next publication.
Non-empty records must pass the queue field checks.
A newly cleared inbox contains `messages` and `accepting_messages`.
A published worker inbox carries a fixed `staff_id`, rather than a copied runtime id.

Atomic worker-state snapshots are strict but lock-free for previews and progress
presentation: each read sees one complete publication and creates no lock file.
Mutation still locks the stable sidecar before reading. Unreadable server
metadata becomes unavailable evidence at the availability boundary; recovery
refuses it without rewriting it. Cleanup keeps its strict uncertainty rule.

Server startup retains its start record after an unconfirmed durability outcome
until a complete current-server record exists or process exit is confirmed.
Before server selection or any new child, `ensure_server` reconciles that record
under the server lock. A confirmed own process at the recorded endpoint that
answers the existing WebSocket handshake is published as current and reused.
A proven-dead process permits clearing the record and starting one replacement.
Other states, including missing serving configuration in a legacy start record,
refuse with the path and a request to confirm or stop the recorded process before
retrying. The record stays intact; retry never overwrites an uncertain live start.
