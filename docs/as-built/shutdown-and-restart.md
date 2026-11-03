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
A session already running on the target account returns status 0 with an already-running line.
A session running on another account returns status 1 without a stop.
A retry accepts a session that started on the target account between attempts.
The default reason is `resumed by name`.

The shared resume path reads the saved account, environment, arguments, and folder.
It restores a closed tab through `restore_missing`.
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

`ClaudeUp` means the registry reports a live Claude within 20 seconds. The command prints `ID: Claude is up on ACCOUNT in PANE (2.4 s)` and returns. The elapsed time includes that session's move stop. A detached check watches its pid and start generation for ten seconds. If it ends, the check tells the showrunner, sends the reviewed `shutdown.restart_failed` push, and enters the existing unit watchdog pass for a unit. Probe sessions skip the user push. The watchdog owns later recovery; the stay check does not retry. A Claude that never starts runs the same command in the same pane up to three times, then returns `ResumeFailed: never started`. A missing tab or unreadable identity returns a named failure. Batch stops and resumes run concurrently; all stops finish before any resume. Launch seeding reads process info only for selected panes; the seed CLI still reads all panes.

The shared session registry reader skips a file that vanishes during the scan. It retries a transient unreadable file up to three times, 50 milliseconds apart. Other unreadable evidence remains visible.

`move_sessions` uses the same stop and resume functions. All stops and prompt cancellation requests finish before any resumes. A retained request compares its wall-clock cutoff with the prompt's immutable observation-start time. An observation that spans stop/resume can be retired; the next watcher pass observes the resumed prompt afresh. A prompt first observed after the stop survives the older request, and failed retirement is retried by stall-watch. Each member resumes in its own pane on the target account; failures are independent. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts at most fifteen seconds. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

Retirement, checkout moves, running-session attach and nix-shell healing use this API. Retirement removes the unit’s pane launch entries after successful stop and cleanup. Retirement and unit rename close the unit's own pane after their ownership checks. A pane already gone counts as closed. Checkout moves stop before moving files and resume afterward. The checkout coordinator excludes only its own pid from cwd holders; other holders still refuse. Attach keeps its process and prompt-submission proofs. The conductor, bounded stop, pause messages and `down`, `now`, `ready`, `cancel`, `conduct` and `follow` commands are deleted.

Watchdog recovery reads the unit's pane launch and live liveness. It keeps one notes pass of grace and resumes the same conversation on the launch account. The unit must not be retired, must have a worktree, and its run must not have finished. A unit with no run record is standing by and can recover. A queued recovery refuses a changed or ended launch. Shared resume creates a fresh tab when the pane is gone. Pending jobs prevent duplicate dispatch; unreadable live confirmation preserves their evidence. The watchdog excludes existing intentional stop records at or after the retained pane launch. It checks the record again before a queued recovery starts. An explicit later launch supersedes an older stop. It does not read account holds. Its outage state retains pending recovery jobs and first-seen times. The agent-notes job runs this check in its own process with the limits in `config/agent_notes.toml`. Shared resume supplies the restarted session’s prompt; the watchdog sends no messages to live sessions, showrunner notices or phone pushes. Record-driven restart still retries unconfirmed owner cleanup through `stop_resume.stop` before launching; a failed retry keeps the issue and a retry command.

Record-driven restart, status, forget, account holds, and launch barriers remain available.
Recovery snapshots and their restore and status readers are deleted.
`restart_support.py` owns the retained record-driven message, notifier, and status helpers.
Inventory owns fresh session attribution.
Unit rename and production rename retain journals; their stop boundary uses `stop_resume`.
The [account design](accounts.md) describes role selection, moves, and worker routing.

## Session hosting and manual recovery

Pane launch writes store the supplied labels unchanged: an unmarked launch does not
inherit a previous launch's staff identity. Restart, resume and the SessionStart
hook recover a conversation's saved labels explicitly through `record_membership`
before writing its launch.

Herdr is the session host. Unit records retain `session_host: "herdr"`;
standalone records carry the server and pane. A process inside a user-managed
terminal multiplexer is an unknown host even if it inherits herdr variables.
Shutdown never observes or mutates that multiplexer.

A Mac `terminal` host restarts into a new herdr session, attached in the same
window through its supervisor, else in a new Ghostty window. An
unavailable desktop refuses with an unlock-and-restart instruction. A session
with no conversation starts fresh under its recorded id and prints a plain
explanation. `forget` retires stopped and failed shutdown-record entries.

Old host records normalize to explicit manual-recovery states. Their names
never become herdr destinations. Unconfirmed cleanup stays in the record;
status and restart print the cleanup and resume commands. Recovery snapshots no longer supply Claude or Codex restore candidates.

The Remote Control fixer resolves the newly registered pid's herdr pane and
checks identity and idle state before input. Tests use disposable state and
fake host commands.

`herdr-upkeep` runs `scripts/maintenance/herdr_upkeep.py`. It saves the running shared session’s layout and refreshes detection rules. `config/herdr_upkeep.toml` supplies its interval and process deadline at each invocation. A successful run saves its completion time; a not-due rerun changes nothing. The worker and its descendants are killed at the configured deadline, and a failed run stays due. Inventory no longer runs a desktop updater or reads the deleted `agent sessions.md`; production identity comes from `staff.json`. No reboot is part of the removal proof.

A restart problem sends one phone push per kind of fix.
Retryable failures share one count push with a retry command and its machine.
Different retryable causes and long session names do not create more pushes.
Trust failures share one count push per machine for plain restarts and failed moves.
An all-account restart groups trust failures across accounts.
If every folder has a configured trust key, the action runs `accounts.py trust`, then retries the restart.
Otherwise, the action asks the natedev session to trust the stopped sessions’ folders from their shutdown records.
The restart output keeps the trust command for each folder.
Configured trust covers exact folder, repository-root, or main-checkout keys, not arbitrary descendants.
Manual failures share one count push per machine. Its action requests a resume from the shutdown records.
The same failed sessions, a subset, or a changed failure reason sends no second push.
A confirmed recovery clears only outage claims older than its live observation.
A later failure can send a new push.
Failed push delivery releases the claims for a retry.

## Outage alert ledger

`scripts/accounts/outage_alerts.py` owns `~/.local/state/accounts/unit_outage_alerts.json`.
A file lock and atomic replacement protect the ledger.
Session ids identify outages independently of shutdown-record archival.
An entry contains a token, owner pid, claim time, and `claimed` or `delivered` state.
`AlertClaim`, `AlertInFlight`, and `AlertDelivered` distinguish permission to send from suppression.

Each sender claims the sessions for one push immediately before that push.
It includes only claimed sessions when the text lists sessions.
It finishes delivery or releases those claims before reserving another push.
The session's machine owns its ledger and sends its alerts.
A remote caller prints peer results without claiming the peer's sessions locally.
`--no-alert` suppresses delivery on the owning machine too.

A successful push marks the matching token delivered.
A failed push releases it for a later retry.
An owner that exits leaves a stale claim that another sender can replace.
A claim also expires after the configured lease, which defaults to fifteen minutes.
That lease exceeds the normal send deadline.
An older token cannot deliver or release its replacement.
The record's `restore_alerted` mark remains separate from successful delivery in the ledger.

`clear(session_id, observed_at)` removes only an entry claimed before the live observation.
Callers pass the observation time, not the later archive time.
A slow probe or batch archive therefore cannot remove an alert for a newer crash.
The watchdog applies this rule when it sees a live unit, but sends no phone push itself.
