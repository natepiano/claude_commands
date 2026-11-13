# Shutdown and restart

## Process-group signals

`scripts/lib/process_group.py` owns process-group signal validation.
`check_process_group(pid)` requires an integer greater than one and refuses the caller's own group.
It also refuses booleans and mock values.
`send_process_group(pid, signum)` validates before the sole raw `os.killpg` call.
Callers handle `ProcessLookupError` when the group has ended.
Shell callers use their checkout's helper through `scripts/lib/py`.
The helper returns exit 3 for a missing group and exit 2 for refusal or failure.
`test_process_group.unguarded_group_signals` checks Python references, embedded scripts, and shell commands for unguarded signals.

## Session commands

The `/agents` skill uses the `agents` program.
`agents` and `agents help` print the same usage and return status 0.
The public commands are `help`, `status`, `pause`, `exit`, `resume`, `restart`, and `stop`.
`agents status [ACCOUNT] [--json] [--here]` reports live sessions.
`agents stop SESSION` stops the selected session and its workers.
Add `--dry-run` to print their names without stopping them.
`agents` runs `scripts/agents/shutdown.py`.
All lifecycle scripts use `scripts/agents/`.

`agents pause NAME --why TEXT` stops the workers now.
The director finishes its current turn and holds at the prompt.
`agents exit NAME --why TEXT` stops the director and its workers.
Both commands preserve the staff rows, tabs, and shells.
Add `--finish-step` to wait for the workers' current tasks before stopping them.
The staff row records the time, caller, and reason under `paused` or `exited`.
The board shows that state.
`agents pause --production SHOWRUNNER [--except UNIT ...] --why TEXT` selects units within one production.
`agents resume --production SHOWRUNNER` resumes its units.
Resume removes a pause record and sends a running director one continuation message.
A paused director on the other machine gets the continuation message without a local liveness read.
The machine comes from its worktree or its showrunner document.
A stopped paused director starts through the shared resume path, including its requested `--to` account.
Queued or failed delivery restores the same pause record and returns failure.
A failed start or a start exception also restores the pause record.
Resume starts an exited director through the shared role-account path and removes its exit record after success.
The prompt hook retains a paused director's hold for every prompt source.
It adds context without blocking or removing the message.
An own-pane resume resolves the agent executable before it removes the exit record.
It executes that path directly. A missing executable retains the exit record.
The stop hook skips the automatic continuation for that director.
Stop does not save or restore composer drafts.

Stop, resume and account move use `scripts/agents/stop_resume.py`.

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
The short form is `resume NAME`, through `scripts/agents/resume`.
The wrapper uses the shared Python launcher and locates `shutdown.py` beside the wrapper.

The lookup accepts a short name, a staff id, or a current session id.
An exact Codex thread name resolves through the shared `session_index.jsonl`.
A canonical UUID takes priority over a thread name and reads no index.
Unknown names return status 1.
They list up to five close names from local units and showrunners when available.
Ambiguous names return status 1 with up to five matching members.
A remote member returns status 1 with its machine name.
A running session returns status 0 with an `unchanged` line that names its current account.
Resume does not stop or move a running session.
Unknown Codex process evidence prints an anomaly and does not block resume.
A pane with the session id and a foreground program counts as already running.
Without process evidence, its account remains unknown.
Unknown process evidence without a pane that reports the id can allow a second process on the same conversation.
A pane at its shell does not prevent resume.
The default reason is `resumed by name`.

The shared resume path reads the saved environment, arguments, and folder.
`member_account.resume_account` selects the account for both families at invocation.
The source account does not select the target account.
A named Codex resume uses the configured `workers` account, with or without a staff row.
Claude units and showrunners use their configured role account, even without a pane launch record.
Claude producers use the configured showrunners account.
Other Claude sessions use the configured units account.
A configured producer without a staff row uses the producer first-start account rule.
When the account does not change, the resume message names no move.
`--to ACCOUNT` overrides the account for either family.
Restart without ACCOUNT and folder moves use the role account.
Explicit pane restore keeps the saved account for both families.
It passes the saved home to `restart.start_row` as an `AccountSelection`.
Explicit pane restore restores a closed tab when a saved launch exists.
Producer upkeep uses shared resume and selects the current family account.
An explicit CLI resume can restore an intentionally stopped session.
Unit watchdog recovery preserves an intentional stop.
Producer upkeep excludes paused and exited rows, but it can resume an otherwise active producer after a process stop.
A separate terminal starts the account command in the target pane.
The action check confirms the session before signals or pane input.
The command changes to the saved folder before the account launch.
The pane receives one opening message after identity confirmation.

Resume from its own empty pane executes the saved launch in that pane.
`stop_resume._resume_once` and `restart.start_row` use `herdr.row_workspace` for new tabs.
Producers use `PRODUCER_WORKSPACE` from `config/staff.conf`, normally `producers`.
Other rows use their parent showrunner's workspace, the entry name, or the shared session.
A role account change does not update an existing pane shell's `resume` alias.
Each agent launch prepares the pane aliases through `herdr.agent_alias_script(kind)` for its selected family.

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

`agents move SESSION_OR_GROUP ACCOUNT [--dry-run] [--why TEXT]` delegates to the accounts move command.
It normalizes the account label and preserves the result and output.

`move_sessions` writes each session request before the stops, then records its confirmed or failed outcome in the accounts moves log.
Each attempt has its own move ID and cause.
Delegated callers and sessions proved live on the target write nothing.

`move_sessions` uses the same stop and resume functions. All stops and prompt cancellation requests finish before any resumes. A retained request compares its wall-clock cutoff with the prompt's immutable observation-start time. An observation that spans stop/resume can be retired; the next watcher pass observes the resumed prompt afresh. A prompt first observed after the stop survives the older request, and failed retirement is retried by stall-watch. Each member resumes in its own pane on the target account; failures are independent. A resumed session retries failed native identity reads within the start deadline before it submits the resume message. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts for the requested duration. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

The notes timer delegates account moves to this detached batch before any stop.
The supervisor needs a restart request; an exited child alone does not cause a restart.
A saved launch keeps its conversation marks but cannot replace an explicit destination account.
The batch includes the existing stay observation before it records a confirmed or failed move outcome.
Each completed member records its outcome without waiting for other resumes.
An early exit or failed resume uses the existing showrunner failure report.

Retirement, checkout moves, running-session attach and nix-shell healing use this API.
Retirement reads process facts with the reader for the host that owns the worktree.
Retirement keeps the recorded pane address and its terminal identity before cleanup.
The remote retirement journal keeps both identities for completion retries.
Cleanup excludes only that pane's proved idle shell from the remaining processes.
After successful retirement, it closes that pane as its last step on the pane's machine.
It sends no signal to that shell.
A pane already gone needs no second close.
Foreground work inside the worktree keeps the process refusal.
Foreground work outside the worktree keeps the pane open and produces one anomaly line.
A failed retirement closes no pane.
Herdr refuses a changed terminal and keeps the new pane and its launch record.
Retirement reports that mismatch without changing its result.
The confirmed close retires the pane launch record.
Unit rename closes the unit's own pane after its ownership checks.
A pane already gone counts as closed.
Checkout moves stop before moving files and resume afterward.
The checkout coordinator excludes only its own pid from cwd holders.
Other holders still refuse.
Attach keeps its process and prompt-submission proofs.

Watchdog recovery uses the retained pane launch and staff row.
An ended pane launch keeps an intentionally stopped session down.
A unit with no pane launch record stays down.
The watchdog does not read shutdown session records.
Unit recovery writes its completion receipt and discards the parked facts before it restores workers.
It returns the worker restore exit code.
A completed recovery rerun restores the workers again.

## Producer launch and admission

`resume morty`, `resume brain`, and `resume dwight` use `stop_resume.resume`.
Account moves and restart share the same resume arguments, account selection, and pane launch code.
Each producer uses its own tab in the `producers` workspace of the shared herdr session.
`config/staff.conf` supplies the workspace through `PRODUCER_WORKSPACE`.
`restart.producer_launch(row)` resumes `row.current_session` with the producer's configured settings.
`restart.producer_resume(row, entry)` preserves saved launch options through `pane_record.configured_resume_arguments`.
It takes the family, model, and effort from the current producer settings.
`pane_launch` reads `--effort` as an option with a value.

`models_registry.launch_settings("producers", name)` validates each `family:model:effort` value in `config/models.conf`.
The configured lines are:

```ini
[producers]
morty=codex:astra:high
brain=codex:astra:medium
dwight=claude:fable:high
```

These model names are aliases that the shared model registry resolves.
`member_account.producer_account(kind, *, to="")` selects the account before admission.
Admitted producers use `member_account.resume_account`.
Both functions select the workers account for Codex and the showrunners account for Claude.
An explicit `--to` overrides that selection.

`accounts.eligible_producers(membership, family)` selects active, unpaused, local producers of the configured family.
`accounts.producer_settings(member)` returns `LaunchSettings` or `InvalidProducerSettings`.
Invalid settings exclude that producer.
`settings_report_scope` limits its warning to one per operation.
A workers swap includes eligible Codex producers.
A showrunners group move includes eligible Claude producers.
Units and showrunners retain their existing group selection.

A repeated move selects a stopped producer even when its recorded home already names the target.
Only proof that the producer runs on the target permits the move to skip it.
`LiveAccountUnproved`, a `ResumeFailed`, identifies a live process with unreadable account evidence.
A move returns `StopFailed` with the unreadable account reason.
It sends no stop or start and makes no home change.
Producers, units, and showrunners share this move check and `member_account.repair_home`.
The move writes `home` only when it changes.
The home records an account; it does not prove that a start finished.

`pane_launch.write` owns retained producer bindings.
`pane_launch.producer_start(directory, expected)` writes the start contract before each retained launch, including an own-pane exec.
`ProducerStartExpectation(member, session, kind, folder, home)` defines the expected launch.
Publication checks the native identity, process generation, family, folder, and account.
The shared `.publication.lock` protects every pane record writer.
Publication replaces earlier bindings through the recoverable `.producer-publication` record without closing their panes.
`pane_launch.read_entries` returns no entries or lock file when the record folder is absent.
Resume compares source and destination pane locations before it lists panes.
An own-pane resume therefore needs no pane list call.

### First start

`ProducerFirstStart(name, folder)` represents a configured producer before staff admission.
It is separate from an admitted `StaffRow`.
`PRODUCER_FOLDER_ROOT` in `config/staff.conf` defaults to `~/rust/hanadocs`.
The first start requires `<root>/<name>/README.md`.
Dwight uses `~/rust/hanadocs/dwight/README.md`, with role `strategist`.

The first-start path uses this order:

1. Lock the producer's first admission through `staff.slot_operation`.
2. Adopt one proved live session with the producer name before a new reservation.
3. Reserve the producer's tab in the configured workspace.
4. Save `PendingProducerAdmission(pane, terminal, kind, home)` at `_producer_admission_path(name)`.
5. Start without a resume argument through `restart.producer_first_arguments(name)`.
6. Prove the native identity through `Herdr.producer_admission_launch`.
7. Admit the row through `staff.ensure("producer", ..., runtime_id=identity)`.
8. Publish the retained binding through `pane_launch.staff_admitted` under `producer_start`.
9. Remove the pending admission record after publication succeeds.

`producer_conflicts` uses `sessions.running_sessions`, the Codex index, and loaded daemon threads.
It checks live agents in producer workspace tabs and panes with the producer label.
An empty labeled shell does not count as a live session.
`adopt_named_producer` adopts exactly one proved native conversation without a launch.
It records that session as `current_session` and preserves its account and working folder.
It records the configured producer folder as the document location.
It publishes a retained binding when the live session has a proved pane.
Two live sessions cause refusal before any staff write or launch.
An unproved live session also causes refusal.
The refusal names each conflicting session ID and pane on one line.
An unreadable daemon registry also refuses admission.
Recovery checks live sessions before it resumes a retained conversation.
Recovery adopts a different proved live session and removes the pending admission record.
Recovery resumes the retained conversation only when no other live session carries the producer name.

`agents stop NAME` resolves a producer name to its row's `current_session` before tab label selection.
Several tabs with the producer label therefore do not select a different conversation.
The shared native process reader uses Claude's session registry to prove a plain `claude` start.
The process arguments need no session ID.

`agents remove-stray NAME SESSION` removes a local stray producer conversation.
The command uses this order:

1. Refuse a session that any staff row records as its `current_session`.
2. Prove the producer name and native process identity before the stop.
3. Save `PendingProducerCleanup` before the first change.
4. Stop the selected conversation through `stop_resume.stop`.
5. Delete the admission record if its saved content still matches the proved admission record.
6. Close its proved terminal through `Herdr.close_retained_pane`.
7. Delete its transcripts across Claude account projects folders through `Path.unlink`.
8. Delete the cleanup record after every step completes.

The pane and terminal match also applies when no retained launch entry exists.
The cleanup record path is `<pane record directory>/.producer-cleanup-<name>-<session>`.
The record holds the name, session ID, process IDs, process start times, agent kinds, and herdr session.
It also holds the pane IDs, tab IDs, terminal IDs, admission match, admission content, and transcript paths.
`ProducerCleanupStep` stores a progress hint.
Each command checks every saved target, regardless of that hint.
A process stop is complete when the saved PID is absent or has a different process start time.
An absent saved pane needs no closure.
The saved tab and terminal IDs protect other targets when the pane still exists.
An absent admission record or transcript needs no deletion.
Each completed step atomically saves its progress before the next step starts.
A repeated command reads the saved targets without native identity discovery.
Each step permits repetition after a failure between its action and its progress write.
A lost stop progress write cannot require a removed registry entry or absent pane metadata.
Cleanup also completes when only the saved transcripts remain.
A failed pane closure retains the terminal target even without herdr session metadata or a retained launch entry.
A failed transcript deletion retains the transcript targets.

Pane closure preserves other panes in the same tab.
The command adds no permission rule and uses no shell deletion command.
A repeated command prints `unchanged` when no cleanup record or selected resource remains.

The record path is `<pane record directory>/.producer-admission-<name>`.
The saved record also holds the folder.
A repeated resume completes an interrupted admission without opening another conversation.
Later resumes use the admitted row's `current_session`.
The pending record retains the original account.
A conflicting `--to`, family, or folder returns failure and preserves that record.

A lost or replaced reserved pane requires proof that its retained conversation stopped.
Recovery reserves another pane and transfers the retained entry without `pid` or `proc_start`.
It then retires the lost pane's launch.
A shell is eligible only without a retained entry or with this pending producer's identity.
A shell with another session's retained launch stays unchanged; recovery opens another tab.

`_resume` checks pending admission before it reads the model registry.
A registry error during pending admission returns `ResumeFailed` and preserves the record.
Without pending admission, a registry error permits the generic resume lookup.
The [upkeep service](herdr-restore.md#restore-services) recovers admitted producers after server loss or reboot.

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
| `Unavailable` | The run, worker record, or known staff role is unavailable | One `anomaly:` line with the child and reason |

The reader selects staff members under the moved parent.
It reads each member's backend and current native session.
The member's `KnownWorkerRole` supplies the child's role without a history fallback.
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
The message names idle children separately.
It keeps the account move source and destination.
Restart and pane restore select each worker's current staff parent directly.
Closed idle workers stay closed until their next assignment.
Pane worker resume uses the dispatch record and transcript to select one step.
An idle worker receives a continuation prompt when its transcript contains the open dispatch.
Restore holds the dispatch lock across the record read, receipt read, and prompt delivery.
Restore, named resume, and director notes use one receipt rule.
An unsubmitted dispatch keeps the saved assign command and its saved prompt path.
Director notes reuse shared dispatch history reads.
A DONE line or a finished record closes the dispatch and suppresses its resume note and step.
The decision reads the record and transcript once per worker and uses the restart's shared run index.

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

A status pass shares one Codex snapshot, one worker-run search, and one pane observation.
It reads pane processes through a direct socket batch and reuses the results during that pass.
The server accepts one request per socket. The batch reads those sockets in parallel.
Read-only layout results share their data. Listed rows use shallow copies for caller edits.
The launch helper uses the same Codex snapshot.
Confirmed stops remove the selected rows' old Codex liveness entries.
An unconfirmed stop checks only its target's current identity and process generation before a launch.
A live target stays running. An unreadable target stays on the restart list.
A missing rollout file or changed generation alone does not prove exit.
Restart launches after a failed stop only when it proves absence or a different process running another conversation.
Worker restore uses the shared run index.
The stop helpers use the shared Codex snapshot.
Action checks still prove the target process before a signal.
Independent rows stop and start in parallel.
Each restore line starts with the staff name, including worker restore results.

Before a power-off, run `agents stop --all`.
After login, run `agents restart`.

`agents stop --all` selects live Claude and Codex rows on this machine from `staff.json`.
The selection includes producers, such as brain, morty, and dwight.
It saves each row ID before its stop. It starts nothing.
A live session without a staff row prints one anomaly line. Stop does not save it.
`agents restart --all` uses the same selection and stop loop, then resumes the selected rows.
It keeps only the row IDs whose starts failed.

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
An `--only` run observes only the requested rows as restart candidates.
It judges workers only for selected worker rows and their parents.
It retains incomplete evidence and anomaly lines for a selected worker.
It skips the scan for live sessions without staff rows.
The shared pane read still supplies complete identity evidence.
A run without `--only` keeps the existing observations.
If a stop includes the caller, restart continues in a detached process before the first stop.
The caller prints the restart log path and returns.
Other restarts run in the caller's process.
A stop checks the current conversation before it sends a signal.
A failed row prints one anomaly line. Other rows continue.

`agents restart` resumes the saved rows that do not run.
A running row prints `unchanged`.
A restarted unit director or showrunner gets one continuation message in its current conversation.
The shared resume message builder supplies the text.
The default cause is "agents restart ran".
The restart option `--why TEXT` replaces that cause.
The message asks the session to continue its work.
If Codex message delivery fails after launch, restart reports one anomaly line with the director's name and the first error line.
The director counts as started and leaves the restart record.
Restart checks each unit director and showrunner that it starts with a continuation message.
The check uses the launched account home and native session identity.
It reads only turns after that director's start.
The reader leaves a final record without a newline unread until the writer completes it.
The launch boundary follows the last complete newline.
A malformed Codex record with a newline makes the transcript unreadable.
A Claude transcript missing from the current read also makes the transcript unreadable.
A matching user turn followed by an assistant turn or tool call confirms resumed work.
All checked directors share one deadline.
The check ends as soon as every director resumes.
At the deadline, each failed check prints one of these lines:

- `<name>: restart check: no continuation message`
- `<name>: restart check: no work after the continuation message`
- `<name>: restart check: transcript unreadable`

The check then prints `restart check: <passed> of <checked> directors resumed`.
The check prints nothing when it checks no director.
It skips held directors, unchanged rows, failed starts, workers, producers, dry runs, and stop-only commands.
It does not change the exit code, restart record, or staff.
config/shutdown.conf sets RESUME_CHECK_SECONDS=180 and RESUME_CHECK_INTERVAL_SECONDS=5.

A paused or exited director stays stopped and keeps its hold record and restart entry.
A running row gets no continuation message.
Producers keep their existing start behavior.
Herdr server restore keeps its existing start behavior.
A row leaves the saved list when it starts or already runs.
A failed start adds the row to the list.
A refused row lock changes no saved row.
`--only` leaves other saved rows on the list.
`stop --all --dry-run` prints each selected name, row ID, kind, and tab position with `would stop and save`.
`restart --dry-run` prints saved rows and identifies live rows outside the saved list.
A row without a saved tab says so on the same line.
Both commands change nothing.
`--only ID[,ID...]` selects staff rows for callers that resume a moved unit.

`shutdown_record.py` stores only a JSON list of staff row IDs in `shutdown/restart.json`.
The record stores no session facts, progress, claims, recovery states, or pane positions.
Herdr stores the tab order and associates each saved tab with its staff row ID.

Start reads `current_session` and the worktree from staff.
A producer without a recorded folder uses the user's home folder.
The native pane reader identifies Codex sessions from their resume arguments or pane reports.
Callers without a report read the process's own pane through the same reader.
No thread identity comes from an open rollout file.
It finds the transcript and its account by the current session ID.
Claude uses `--resume ID`. Codex uses `resume ID` in the worktree.
The launch sends no work prompt.
Start leaves live sessions and other foreground work alone.
Restart does not send phone pushes or use `add_unit.py`.

`restart_support.py` retains the continuation-message API for the quota watcher.
`restart.wait_for_confirmation` supports the existing unit attachment path.

Claude launches write folder trust in the target account before the process starts.
The write uses the repository trust key, or the folder itself outside a repository.
A failed write prints one anomaly with the folder, account file, and error.
The launch then returns its failure result without a process start.
A composer with a draft waits until the deadline without changes to the draft.

Worker restore shares the command's run index and Codex snapshot through stopped-worker startup.
Restore uses `worker_panes.worker_run` for run selection and fallback.
Stop selects workers from their current staff parent.

An unconfirmed worker stop needs targeted proof of process absence before restart can launch that worker.
Live or unknown evidence leaves the worker pending.
The worker check uses the same function as the Codex row check.

### Gates during a session restart

A session stop sends TERM, then KILL after the grace period, to the one proved agent PID (`stop_resume.py`).
It sends no signal to the process group, the child processes, or the build cgroup.
A normal stop keeps the pane and its shell.

A detached run continues across a session stop or restart.
`detached_runs.py start` starts its supervisor in a new process session.
The command runs in a `builds-detached.slice` scope when systemd is available.
The record, the log and the exit code stay in the run folder's `detached_runs` directory.
After the command ends, the supervisor starts a new Python process to deliver the result from the current code tree.
The supervisor waits for delivery before it marks the run finished.
The result goes to the worker's fixed staff row and its current conversation.
A stopped worker's result goes to its parent. A stopped parent's result waits in its queue.

`verify.sh final` detaches by itself only for a registered Codex worker.
A bare call from a Claude director stays attached.
`assign.sh` does not detach the commands that its worker starts later.

A merge gate in a Claude background shell has no proved survival.
`copies.py run` starts the gate attached, and `merge_gate.py` stops its module groups on TERM or HUP.
Before another run, check for a complete saved gate result.
Put a gate that must survive a restart in a detached run.

The cloud gate runs as its own user service and on a remote guest.
A local stop does not end it. Its outcome stays under the Git `cloud-gates` folder.
The local import and type checks after the cloud result still need the local caller.

A surviving run keeps its memory queue place and its age.
A new process does not take over the place of a dead waiter.

A session restart is not a machine restart. A reboot or power-off can end local supervisors and waiting processes.
