# Shutdown and restart

The `/agents` skill uses the `agents` program.
`agents` and `agents help` print the same usage and return status 0.
The public commands are `help`, `status`, `resume`, `restart`, and `stop`.
`agents status [ACCOUNT] [--json] [--here]` reports live sessions.
`agents stop SESSION` stops the selected session and its workers.
`agents` runs `scripts/shutdown/shutdown.py`; script callers keep that path.

Stop, resume and account move use `scripts/shutdown/stop_resume.py`.

`stop(session)` ends only the selected local session's process.
It reads the session identity, pid, and start time before TERM.
It checks the current conversation before each signal.
It checks exit every 0.1 seconds and sends KILL after two seconds when the conversation proof remains valid.
Registry loss after TERM is quiet.
Stop waits up to five more seconds with the saved pid and birth proof.
Other read errors print an anomaly.
It waits up to five seconds after KILL.
Registry removal alone does not prove exit.
A surviving process returns `StopFailed` and does not resume.
After exit, `stall_watch.retire_session_prompt(session_id)` publishes only `{session_id, at}`.
It takes no watcher lock and sends no message.
Stall-watch retires the saved prompt keys on its next pass, even after launch deletion.
A cancellation error prints a separate warning and does not change the stop result.
Workers keep their threads.
Staff membership selects worker children for stop and restart.
These operations do not read a transport pin.

`release_source(row, backend)` uses the current native conversation evidence.
A confirmed exit or `NotRunning` gives `SourceReleased`.
An unresolved staff reference gives `SourceUnresolved`.
Unknown process evidence or an unproved stop gives `SourceReleaseUnknown`.
A family switch preserves its source when release remains unproved.

`agents resume NAME [--why TEXT] [--to ACCOUNT]` works from any directory.
The short form is `resume NAME`, through `scripts/shutdown/resume`.
The wrapper uses the shared Python launcher and locates `shutdown.py` beside the wrapper.

The lookup accepts a short name, a staff id, or a current session id.
An exact Codex thread name resolves through the shared `session_index.jsonl`.
A raw session id takes priority over a thread name.
Unknown names return status 1.
They list up to five close names from local units and showrunners when available.
Ambiguous names return status 1 with up to five matching members.
A remote member returns status 1 with its machine name.
A running session returns status 0 with an `unchanged` line that names its current account.
Resume does not stop or move a running session.
Unknown Codex process evidence prints an anomaly and does not block resume.
A pane with the session id and a foreground program counts as already running.
Without process evidence, its account remains unknown.
A pane at its shell does not prevent resume.
The default reason is `resumed by name`.

The shared resume path reads the saved environment, arguments, and folder.
A named Codex resume uses the configured `workers` account, with or without a staff row.
Claude units and showrunners use their configured role account, even without a pane launch record.
Other Claude sessions, and sessions with no staff row, use the configured units account.
When the account does not change, the resume message names no move.
`--to ACCOUNT` overrides the account for either family.
Restart without ACCOUNT and folder moves use the role account.
A herdr server restore keeps the saved account for both families.
It restores a closed tab when a saved launch exists.
An explicit CLI resume can restore an intentionally stopped session.
Automatic recovery preserves an intentional stop.
A separate terminal starts the account command in the target pane.
The action check confirms the session before signals or pane input.
The command changes to the saved folder before the account launch.
The pane receives one opening message after identity confirmation.

Resume from its own empty pane executes the saved launch in that pane.

`resume_message.py` builds every opening message.
The restore paths use these causes:

1. A named resume uses `resumed by name` unless `--why` supplies another reason.
2. An account move uses its supplied reason and the source and target accounts.
3. A refusal-watch move uses the refusal-watch reason through the shared account move.
4. A folder move uses `the session folder moved`.
5. A herdr server restore resumes the saved conversation without a generated prompt.
6. A relaunch with no known cause uses `you were stopped and resumed`.

The relaunch supervisor preserves explicit prompts.
The pane path marks prompt delivery as `pane` to prevent a second automatic prompt.
Before launch, the prompt cursor skips complete old rows without parsing JSON.
Owner scans retain runs with idle workers.
Owner scans print no missing-owner anomaly for ended runs or runs without an active dispatch.

`ClaudeUp` means the registry reports a live Claude within 20 seconds.
Resume makes one attempt.
A detached check watches its pid and start generation for ten seconds.
The watchdog owns later recovery.
A missing tab or unreadable liveness returns a named error.
All batch stops finish before any resume.
Pane input waits up to ten seconds for the target shell to finish its startup work.
Launch seeding reads process information only for selected panes.

The shared session registry reader skips a file that vanishes during the scan. It retries a transient unreadable file up to three times, 50 milliseconds apart. Other unreadable evidence remains visible.

`move_sessions` writes each session request before the stops, then records its confirmed or failed outcome in the accounts moves log.
Each attempt has its own move ID and cause.
Handed-off callers and sessions already on the target write nothing.

`move_sessions` uses the same stop and resume functions. All stops and prompt cancellation requests finish before any resumes. A retained request compares its wall-clock cutoff with the prompt's immutable observation-start time. An observation that spans stop/resume can be retired; the next watcher pass observes the resumed prompt afresh. A prompt first observed after the stop survives the older request, and failed retirement is retried by stall-watch. Each member resumes in its own pane on the target account; failures are independent. A resumed session retries failed native identity reads within the start deadline before it submits the resume message. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts for the requested duration. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

The notes timer delegates account moves to this detached batch before any stop.
The supervisor needs a restart request; an exited child alone does not cause a restart.
A saved launch keeps its conversation marks but cannot replace an explicit destination account.
The batch includes the existing stay observation before it records a confirmed or failed move outcome.
Each completed member records its outcome without waiting for other resumes.
An early exit or failed resume uses the existing showrunner failure report.

Retirement, checkout moves, running-session attach and nix-shell healing use this API. Retirement removes the unit’s pane launch entries after successful stop and cleanup. Retirement and unit rename close the unit's own pane after their ownership checks. A pane already gone counts as closed. Checkout moves stop before moving files and resume afterward. The checkout coordinator excludes only its own pid from cwd holders; other holders still refuse. Attach keeps its process and prompt-submission proofs. The conductor, bounded stop, pause messages and `down`, `now`, `ready`, `cancel`, `conduct` and `follow` commands are deleted.

Watchdog recovery uses the retained pane launch and staff row.
An ended pane launch keeps an intentionally stopped session down.
A unit with no pane launch record stays down.
The watchdog does not read shutdown session records.

## Worker resume results

A session move selects only worker rows whose staff `parent` names the moved row.
Owning a run does not select every worker in that run.
The fixed staff ID identifies each child once, even when several runs name it.

`inventory.worker_resumes` returns `WorkerResume` results from `resume_message.py`.
`Resumable` and `Idle` carry `ClaudeChildResume` values.
Each child has one result:

| Result | Evidence | Message |
|---|---|---|
| `Resumable` | The child's turn was live | The unchanged command from `resume_worker_command` |
| `Idle` | The child's readable turn was not live | `idle` and `nothing to run now` |
| `Unavailable` | The run, worker record, or last assignment is unavailable | One `anomaly:` line with the child and reason |

The reader selects staff members under the moved parent.
It reads each member's backend and current native session.
A Claude child uses its `impl_worker_<slot>.json` record and the matching status file.
The status reader searches the run root and `phases/*`; it does not search `tmp/**`.
A readable status other than `implementing`, including `capacity_exhausted`, gives `Idle`.
An unavailable child does not hide another child's result.

`session_staff.worker_run_entries(snapshot, rows)` returns real `(run, entry)` pairs.
`worker_run_index(snapshot)` also includes owner-marker runs without a worker record.
Source release uses native process evidence and does not scan those runs.
Both readers share one scan and report unreadable evidence as anomalies.
`unit_runs.run_folders` preserves root and name order, removes duplicate resolved paths, and skips each root's `active` marker folder.
An unreadable root does not prevent reads of other roots.

Pane worker resumes use `PaneWorkerResume` with a fixed staff id and an active dispatch id.
The director receives `Worker <staff id>: busy dispatch <dispatch id>` for each active direct child.
An idle child receives no new assignment.
The message names idle children separately.
It keeps the account move source and destination.
It supplies no saved mesh launcher command.
Restart and pane restore select each worker's current staff parent directly.
Closed idle workers stay closed until their next assignment.

## Launch names

`LaunchNames.read()` supplies one staff snapshot for lookup, labels, and restore.
The Mac obtains that snapshot from the staff owner through one SSH read.
The native session ID selects the staff row and its current name.
An unavailable staff read preserves the saved launch name and prints a diagnostic.
An empty name does not clear an existing title.
Restore updates a stale tab title even when its pane title already matches.
A resume resolves the saved staff reference to its current native session.
Saved launch marks contain no copied `STAFF_ID`.
The [pane restore](herdr-restore.md) uses this same naming policy.

## Restart

`agents restart ACCOUNT --all` selects live rows on that Claude or Codex account.
It reads the account from each observed process environment.
A hosted Codex thread uses its daemon's account home.
Selected rows restart on ACCOUNT.
Each worker matches its own account.
Filtered restarts leave unselected owners and workers unchanged.
A live worker without account evidence prints `NAME: left alone; account unknown` once.
Without `--all`, ACCOUNT filters saved rows by their last session's account.
An unknown ACCOUNT prints one error and returns status 2 before any stop.
`--dry-run` lists only selected rows and changes nothing.
Without ACCOUNT, the existing selection and role accounts apply.

`--only ID[,ID...]` also limits `--all`; ACCOUNT and `--only` must both match.
If a stop includes the caller, restart continues in a detached process before the first stop.
The caller prints the restart log path and returns.
Other restarts run in the caller's process.

`agents restart --all` selects live Claude and Codex rows on this machine from `staff.json`.
It stops each session, then starts each session.
It saves only the row IDs whose starts failed.
A stop checks the current conversation before it sends a signal.
A failed row prints one anomaly line. Other rows continue.

`agents restart` resumes the saved rows that do not run.
A running row prints `unchanged`.
A row leaves the saved list when it starts or already runs.
A failed start adds the row to the list.
A refused row lock changes no saved row.
`--only` leaves other saved rows on the list.
`--dry-run` prints each selected name, row ID, kind, and saved tab position.
A row without a saved tab says so on the same line.
The command changes nothing.
`--only ID[,ID...]` selects staff rows for callers that resume a moved unit.

`shutdown_record.py` stores only a JSON list of staff row IDs in `shutdown/restart.json`.
The record stores no session facts, progress, claims, recovery states, or pane positions.
Herdr stores the tab order and associates each saved tab with its staff row ID.

Start reads `current_session` and the worktree from staff.
It finds the transcript and its account by the current session ID.
Claude uses `--resume ID`. Codex uses `resume ID` in the worktree.
The launch sends no work prompt.
Start leaves live sessions and other foreground work alone.
Restart does not send phone pushes or use `add_unit.py`.

`restart_support.py` retains the continuation-message API for the quota watcher.
`restart.wait_for_confirmation` supports the existing unit attachment path.
