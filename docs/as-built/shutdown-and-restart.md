# Shutdown and restart

Stop, resume and account move use `scripts/shutdown/stop_resume.py`.

`stop(session)` ends only the selected local session's Claude process. It reads identity through `sessions.session_liveness`, keeps the pid and start time, checks that identity before each signal, sends TERM, and checks exit every 0.1 seconds and sends KILL after two seconds if the same process survives. After exit, `stall_watch.retire_session_prompt(session_id)` atomically publishes only `{session_id, at}` with no launch, pane or parent lookup. It takes no watcher lock and sends nothing; stall-watch finds saved prompts by their owning session id and retires their stored keys on its next pass, even after launch deletion. Any cancellation exception prints a separate warning and does not prevent intentional-stop bookkeeping or change the stop result. It waits up to five seconds after KILL. It waits for that pid and start time to leave. Registry removal alone does not prove exit. A surviving process returns `StopFailed`. Workers keep their threads. No stopping record, ready mark, account hold or conductor is created.

`resume NAME [--why TEXT] [--to ACCOUNT]` works from any directory.
The executable is `scripts/shutdown/resume`.
The wrapper uses the shared Python launcher and locates `shutdown.py` beside the wrapper.
The installation link belongs in `~/.local/bin` after merge.

One `LaunchNames.read()` supplies the staff snapshot for lookup, labels, and restore.
The lookup accepts a short name, a staff id, or a current session id.
Unknown names return status 1.
They list up to five close names from local units and showrunners when available.
Ambiguous names return status 1 with up to five matching members.
A remote member returns status 1 with its machine name.
A running session returns status 0 with an `unchanged` line that names its current account.
Resume does not stop or move a running session.
The default reason is `resumed by name`.

The shared resume path reads the saved account, environment, arguments, and folder.
It restores a closed tab when a saved launch exists.
An explicit CLI resume can restore an intentionally stopped session.
Automatic recovery preserves an intentional stop.
A separate terminal starts the account command in the target pane.
The command changes to the saved folder before the account launch.
The pane receives one opening message after identity confirmation.

An empty target pane cannot receive its own foreground command through pane typing.
The own-pane path saves the resolved launch as an attempt before `exec`.
It preserves the intentional-stop state and records no verified proof before the launch.
An exec error uses the existing failure path and keeps the pane.
It uses the same account command with the opening message as a Claude argument.
It changes to the saved folder and replaces the foreground process.
The SessionStart hook records the new process and clears the intentional-stop state.
This path cannot perform the later pid confirmation or detached stay check.

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

`ClaudeUp` means the registry reports a live Claude within 20 seconds. The command prints `ID: Claude is up on ACCOUNT in PANE (2.4 s)` and returns. The elapsed time includes that session's move stop. A detached check watches its pid and start generation for ten seconds. If it ends, the check tells the showrunner, sends the reviewed `shutdown.restart_failed` push, and enters the existing unit watchdog pass for a unit. Probe sessions skip the user push. The watchdog owns later recovery; the stay check does not retry. Resume launches once. A Claude that never starts returns `ResumeFailed: never started`. A missing tab or unreadable identity returns a named failure. Batch stops and resumes run concurrently; all stops finish before any resume. Launch seeding reads process info only for selected panes; the seed CLI still reads all panes.

The shared session registry reader skips a file that vanishes during the scan. It retries a transient unreadable file up to three times, 50 milliseconds apart. Other unreadable evidence remains visible.

`move_sessions` uses the same stop and resume functions. All stops and prompt cancellation requests finish before any resumes. A retained request compares its wall-clock cutoff with the prompt's immutable observation-start time. An observation that spans stop/resume can be retired; the next watcher pass observes the resumed prompt afresh. A prompt first observed after the stop survives the older request, and failed retirement is retried by stall-watch. Each member resumes in its own pane on the target account; failures are independent. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts for the requested duration. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

Retirement, checkout moves, running-session attach and nix-shell healing use this API. Retirement removes the unit’s pane launch entries after successful stop and cleanup. Retirement and unit rename close the unit's own pane after their ownership checks. A pane already gone counts as closed. Checkout moves stop before moving files and resume afterward. The checkout coordinator excludes only its own pid from cwd holders; other holders still refuse. Attach keeps its process and prompt-submission proofs. The conductor, bounded stop, pause messages and `down`, `now`, `ready`, `cancel`, `conduct` and `follow` commands are deleted.

Watchdog recovery uses the retained pane launch and staff row.
An ended pane launch keeps an intentionally stopped session down.
The watchdog does not read shutdown session records.

## Restart

`shutdown.py restart --all` selects live Claude rows on this machine from `staff.json`.
It saves their row IDs, stops each session, then starts each session.
A stop checks the current conversation before it sends a signal.
A failed row prints one anomaly line. Other rows continue.

`shutdown.py restart` resumes the saved rows that do not run.
A running row prints `unchanged`.
A row leaves the saved list when it starts or already runs.
A failed start or a refused row lock leaves the row on the list.
`--only` leaves other saved rows on the list.
`--dry-run` prints the selected rows and changes nothing.
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
