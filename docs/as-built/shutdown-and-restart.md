# Shutdown and restart

Stop, resume and account move use `scripts/shutdown/stop_resume.py`.

`stop(session)` ends only the selected local session's Claude process. It reads identity through `sessions.session_liveness`, keeps the pid and start time, checks that identity before each signal, sends TERM, and sends KILL after two seconds if the same process survives. It waits up to five seconds after KILL. It waits for that pid and start time to leave. Registry removal alone does not prove exit. A surviving process returns `StopFailed`. Workers keep their threads. No stopping record, ready mark, account hold or conductor is created.

`resume(session, why=..., to=...)` finds the session's own herdr tab and its launch entry, then runs the account exec command through `Herdr.run_in_pane`. The resume arguments and message have one builder each. The message says why the session stopped and resumed, tells it to continue its work, and explains how to resume existing workers. When herdr has a stale or empty conversation id, the saved launch entry supplies it unless another Claude runs in that pane. The command sets the destination pane's `HERDR_PANE_ID` and `HERDR_WORKSPACE_ID`. It unsets `HERDR_TAB_ID`, so a moved tab does not reuse its shell's old marks. No tab opens or closes. A live Claude refuses resume.

`Resumed` means Claude starts within 20 seconds and stays live for 10 seconds. A failed start runs the command again in the same pane, up to three attempts in total. After that, `ResumeFailed` reports `never started` or `ended after N s`. A missing tab or unreadable identity returns a named failure. Batch resumes run concurrently.

The shared session registry reader skips a file that vanishes during the scan. It retries a transient unreadable file up to three times, 50 milliseconds apart. Other unreadable evidence remains visible.

`move_sessions` uses the same stop and resume functions. All stops complete before any resumes. Each member resumes in its own pane on the target account; failures are independent. A selected caller hands off the same batch before any signal. Linux uses a user systemd service; macOS uses a detached process. An optional warning lasts at most fifteen seconds. The detached batch clears `CLAUDE_CODE_SESSION_ID` so it does not hand off again. Final results are one line per member under the shutdown state folder's `logs` directory.

Retirement, checkout moves, running-session attach, nix-shell healing and watchdog recovery use this API. Retirement still forgets old recovery entries. Retirement and unit rename close the unit's own pane after their ownership checks. A pane already gone counts as closed. Checkout moves stop before moving files and resume afterward. Attach keeps its process and prompt-submission proofs. The conductor, bounded stop, pause messages and `down`, `now`, `ready`, `cancel`, `conduct` and `follow` commands are deleted.

Record-driven restart, status, recovery snapshots, forget, account holds and launch barriers remain until Phase 8. `restart_support.py` owns their retained message, notifier and record-status helpers. Inventory owns fresh session attribution. Unit rename and production rename keep their Phase 9 journals; their stop boundary uses `stop_resume`.

## Session hosting and manual recovery

Herdr is the session host. Unit records retain `session_host: "herdr"`;
standalone records carry the server and pane. A process inside a user-managed
terminal multiplexer is an unknown host even if it inherits herdr variables.
Shutdown never observes or mutates that multiplexer.

A Mac `terminal` host restarts into a new herdr session, attached in the same
window through its supervisor, else in a new Ghostty window. An
unavailable desktop refuses with an unlock-and-restart instruction. A session
with no conversation starts fresh under its recorded id and prints a plain
explanation. `forget` retires stopped and failed shutdown-record entries as
well as recovery snapshot entries.

Old host records normalize to explicit manual-recovery states. Their names
never become herdr destinations. Unconfirmed cleanup stays in the record;
status and restart print the cleanup and resume commands. An old Codex
snapshot prints its recorded `CODEX_HOME` and `codex resume <id>` (or its
picker), remains unacknowledged and makes restart partial with exit 1.

The Remote Control fixer resolves the newly registered pid's herdr pane and
checks identity and idle state before input. Tests use disposable state and
fake host commands.
