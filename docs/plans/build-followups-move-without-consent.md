# Account move without the session's consent

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A move or restart never waits on the session agreeing: an idle session is stopped without a pause message, one that answers the pause and goes idle counts as ready, the pause text names a check the session can run itself, and `accounts.py move` takes `--no-alert`.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md` (settle, Results), `docs/accounts.md` (Move a session)

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-15, the self-run Ghostty live check of the account move (the user cancelled the desk visit: "we can't possibly require doing a test this way"). The throwaway session `pm-move-ghostty`, idle with nothing running, twice declined `Pause for move: claude4 → claude1` (11:25:24 and 11:27:54 EDT) because the request came from another session and not from its user. Settle re-asked at 2 minutes and at 4 minutes pushed `shutdown.holdout` to the user's phone, since `accounts.py move` has no `--no-alert`. On 2026-10-14 `pm-move-tmux`, on the same model, complied; compliance varies by session. The showrunner approved three fixes at 11:31 EDT, as a follow-up run with its own commit budget, the Ghostty result folded into the first checkpoint, and no CLAUDE.md rule.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Project started:** 2026-10-15T15:38:26.819+00:00
- **Stack:** Python 3.13 standard library, checked by basedpyright. No `Any`, a `TypedDict` per JSON shape, no file-level ignores.
- **Key files** (find by name; line numbers move):
  - `scripts/shutdown/settle.py` — `begin` (~720: every non-seat session gets `SessionWaiting` unless `prompt_state` says it stopped taking prompts, then `SessionReaping`), `next_step` (stops an idle session with idle workers at once, `idle; nothing in progress`, or after the pause once the registry moved past it, `idle after the pause message; no ready line` — Phase 1; otherwise `SendSettleMessage`, a re-ask after `REASK_AFTER`, and a stop on `ready`/`held by usage limit` once idle, `blocker(...)` clear and no form showing), `_settle_text` (~614, the pause text; first line `Pause for move: <label> → <target> (<why>, <time>)`), `_holdout_due` (~1454, gated on `saved["alerts"] == "user"`), `holdout_line`, `_send_holdout_alert`, `ready` (~907), `now` (~1053), `down` (~1124).
  - `scripts/shutdown/record.py` — `WhereSaid` / `WhereNotSaid` (~200), the `where` parser (~624), `SessionShutdown.alerts: Literal["user", "none"]`.
  - `scripts/shutdown/inventory.py` — `Session` (`status: str`, from the registry wire record; `updated_at` in registry ms since Phase 1).
  - `scripts/accounts/accounts.py` — `move_sessions` (~2106; runs `shutdown.py down <src> --here --only <ids> --restart --to <target> --why <why>` at ~2152), `move_group`, `move`, the `move` subparser (~3041).
  - Tests: `scripts/shutdown/test_settle.py` (`SettleTests`; patches `now_utc`, `merge_in_progress`, `_showing_form`), `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_account_move.py` (`AccountMoveTests`), `scripts/shutdown/test_support.py` (`ShutdownTestCase` blocks every send leaf).
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py' </dev/null`, the same for `scripts/accounts`, `scripts/lib`, `scripts/production` and `scripts/whoami`. Lint: `basedpyright <changed .py files>`; pass is the last line `0 errors, 0 warnings, 0 notes` (exit 3 from the missing `.venv`).
- **Invariants:** tests start no session and send no message or push; a Claude process gets only SIGTERM after its identity check; times shown to the user are in `user_zone()`; every user-facing line naming a command names where to run it; a changed push text is re-approved with `scripts/notify/review.py texts --id … --approve`; live steps belong to the unit director, on throwaway sessions only, always with alerts off.
- **Ordering with message-clarity:** message-clarity changes the `shutdown.holdout` push text in `settle.py` as its own checkpoint. Merge `origin/build-followups` after it lands and before Phase 2 edits the pause text.

## Phases

### Phase 1 — An idle session needs no consent  · status: done

#### As-built

- `settle.next_step` stops a waiting, matching session that is idle (session and every worker idle, `blocker(...)["kind"] == "clear"`, no form showing): with `settle_message` `not sent` or `queued` it returns `StopSession("idle; nothing in progress")` and sends no pause message; with `settle_message` `sent` and the registry's `updated_at` strictly later than the message's `at` it returns `StopSession("idle after the pause message; no ready line")`. A form, a non-clear blocker (unpushed work included) or a busy worker still holds, and a busy session keeps the pause, re-ask and holdout path.
- `StopSession` carries `idle_reason: stop.IdleStopReason` (default `"not an idle stop"`); `IdleStopReason` is a `Literal` of that default and the two idle texts.
- `stop.stop_session(record, *, idle_reason=...)` writes a non-default reason as the stopping claim's `where` (`{"kind": "said", ...}`) in the same record update that moves progress to stopping. `_stop_owner` admits a `waiting` prior only for an idle reason, and still runs fresh idle, worker and blocker checks.
- The conductor sends no holdout alert for a `StopSession` step.
- `inventory.SessionFields.updated_at: int` holds the registry's `updatedAt` in ms. `parse_session(..., require_updated_at=False)` defaults a missing value to 0; `parse_inventory` requires it.

**Files:**
- `scripts/shutdown/settle.py` — `next_step` idle rules; `StopSession.idle_reason`.
- `scripts/shutdown/stop.py` — `IdleStopReason`, `stop_session`, the `_stop_owner` waiting-prior guard.
- `scripts/shutdown/inventory.py` — `SessionFields.updated_at` and its parse.

**Binds later work:** idle sessions get no pause message, so pause text reaches only busy sessions. The stop reasons `idle; nothing in progress` and `idle after the pause message; no ready line` are what a live check reads in the stopping claim's `where`. Test fixtures carry `updated_at` and the `workers` names (`MatchingSession.workers`, `inventory.WorkerSession`, kind `"worker"`).

**Gotchas:** `updated_at` equal to or before the pause time is not an answer; only a strictly later value counts. Saved records without `updated_at` parse as 0.

### Phase 2 — The pause names its own check; `move --no-alert`  · status: done

#### As-built

- `settle.pause_confirmation_line(record)` builds the second pause line: the request comes from the shutdown the user started, not from another session's own choice; to confirm it, run in this terminal `shutdown.py status <account label> --here`, which lists this session under the move. A move cites `docs/accounts.md` "Move a session"; a `Pause for stop` says "under the stop" and cites `docs/as-built/shutdown-and-restart.md`.
- `_settle_text` places that line right after the unchanged first line; only busy sessions receive it, since an idle session is stopped with no pause message.
- `accounts.MoveAlerts = Literal["user", "none"]`; `alerts: MoveAlerts = "user"` on `move_sessions`, `move_group` and `move`. `move --no-alert` (store_const `"none"`), single and group, appends `--no-alert` to each `shutdown.py down` argv via `_move_down_argv`.
- Quiet `--dry-run` prints the planned `down` commands per source login.
- `docs/accounts.md` "Move a session" shows `--no-alert` in both usage lines, for live checks.

**Files:**
- `scripts/shutdown/settle.py` — `pause_confirmation_line`, `_settle_text`.
- `scripts/accounts/accounts.py` — `MoveAlerts`, `_move_down_argv`, `move_sessions`, `move_group`, `move`, the `move` subparser.
- `docs/accounts.md` — Move a session, with `--no-alert`.
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_account_move.py` — pause line for move and stop; `--no-alert` on each `down` argv, single and group, absent without the flag.
- `scripts/accounts/test_refusal_watch.py` — expects `move_group` to forward `alerts="user"`.

**Binds later work:** `accounts.py move … --dry-run --no-alert` prints each planned `shutdown.py down … --no-alert` command and changes nothing, so the live check previews every move this way. A busy session's pause message has a second line naming `shutdown.py status <account label> --here`, which a declining session in the live check can run before it answers.

**Gotchas:** the status command uses `note_file.canonical_label(record["label"])`, never the login. The first pause line stays byte-identical, because launching skills match its prefix. `test_refusal_watch.py` pins the `move_sessions` kwargs, so a new move parameter changes that test too.

**Ruled out:** a bare `no_alert: bool` parameter — the named `MoveAlerts` Literal holds the choice.

### Phase 3 — Live check without consent  · status: done

**Depends on:** Phase 1, Phase 2

#### As-built

- Live check on natedev, 2026-10-15, installed main, two throwaway Haiku sessions on claude4 in `/home/natepiano/postmaster`, every command with `--no-alert`. Results are recorded in `docs/as-built/shutdown-and-restart.md` (Results, "Move without consent").
- Idle Ghostty session: `accounts.py move <id> claude1 --no-alert` stops it in about 1 s with `settle_message` `not sent` and the stopping claim `idle; nothing in progress`, no pause message in either transcript, and restarts it on claude1 in the same KWin window, shell and supervisor, codeword recalled. No push.
- Busy tmux session: moved while reading files, it receives the pause with the second line naming `shutdown.py status claude4 --here`, runs `ready` without the status check, and moves to claude1. No push.

**Files:**
- `docs/as-built/shutdown-and-restart.md` — Results for the live check and its cleanup.

**Binds later work:** the cleanup found two defects that the remaining work owns. A not-ready refusal in `_stop_owner` is stored as a final `stop failed` that nothing retries and `ready` ignores, and a registry status `shell` never counts as idle ("A session that is not ready yet is waited on, not given up on"). A conductor keeps running the code it started with and holds the account's conduct lock while any of its records stays open ("A conductor runs the code on disk").

**Gotchas:** a declining session cannot be staged: a session refuses a peer's instruction to decline, so `idle after the pause message; no ready line` is covered by unit tests only. `move --dry-run` still labels an idle session `pause, then move`. Two claude1 records from 2026-10-14 stay open until the user cancels them.

**Ruled out:** the move preview reading live session status — `ListedSession` carries none, so the label becomes `move when idle`; broadening `remote_control.py`'s idle check to `shell` — it authorizes typing into the terminal.

### Phase 4 — A session that is not ready yet is waited on, not given up on  · status: done

**Depends on:** Phase 3

#### Work Order

**Goal:** a move never gives up on a session because it was briefly busy or had a background shell running: the stop waits until the session is idle, a session that says `ready` after such a refusal is taken at its word and a conductor picks it up, and `move --dry-run` no longer promises a pause it may not send.

**Spec:**
- Found live (dashboard-unit, request of 2026-10-14 16:18:50 EDT): the session said `ready` at 16:19:20, a message made it busy at 16:19:39, the conductor claimed the stop at 16:19:57, and `stop.py`'s guard in `_stop_owner` (the `current["status"] != "idle"` test) refused at 16:19:59. Refusing was right. Storing the refusal as the final progress `stop failed` (reason `session or worker is not idle and ready`) was not: `_done` (`settle.py`) counts `stop failed` as finished, the conductor exited, and nothing retried when the session went idle 12 seconds later. On 2026-10-15 the session ran `shutdown.py ready`, which looks only at `waiting`/`ready`/`held by usage limit` records, printed `no shutdown in progress for this session`, and went on working on claude1.
- **A not-ready refusal is not a failure.** In `_stop_owner`'s guard, write the progress the refusal already computes in `NotReadyToStop.progress` (`waiting`, `ready` or `held by usage limit`; a reclaimed stale `stopping` claim maps to `waiting`, as today), and keep the `NotReadyToStop` issue on the record's `stop_issues` so status can show why. Never write `stop failed` for it. `next_step` then stops the session on a later pass once it is idle, under every existing rule (requester goes last, `force == "now"`, forms, blockers). `report_stop` must not report the record as finished while it waits.
- **Old records recover at one boundary.** In `record.py`'s `_read_record` (the legacy normalization point every reader goes through: `session_records`, `all_session_records`, status, `follow`, restart selection), rewrite a record whose progress is `stop failed`, whose stop reason is exactly `session or worker is not idle and ready`, and whose `stop_issues` hold exactly one `NotReadyToStop`, into that issue's `progress`. No reader carries its own legacy test.
- **A real failure is never read as a refusal.** `_action_failed` (`settle.py`) and every other writer of `stop failed` replaces `stop_issues` with its own issues, so a stale `NotReadyToStop` from an earlier refusal cannot make a later real failure look recoverable.
- **`ready` restarts the work.** `ready()` (`settle.py`) records `ready` as today and then starts the account's conductor with `_launch_conductor(login, label)`; when one already holds the lock, the new one exits at once, as today. If the launch fails, print one line naming the command to run and where (`in a terminal on <machine>`), exit 2.
- **`"shell"` counts as idle for stopping.** The registry status `"shell"` means the session is at its prompt while a background Bash shell it started is still running; the shell ends with the session. Name the set once in `settle.py` (a private `frozenset`, e.g. `_STOPPABLE_STATUSES = frozenset({"idle", "shell"})`) and use it in `next_step`'s idle test, `stop.py`'s guard, and `_hold_details`. Leave `remote_control.py`'s idle check as it is: it authorizes typing into the terminal, which a running shell may still be using. Workers keep their own `"idle"` test.
- The automatic idle stops (`idle; nothing in progress`, `idle after the pause message; no ready line`) gain `; a background shell was running` when the status was `shell`. A session's own `ready --where` text is never replaced.
- **The preview stops promising a pause.** `move_preview_line` (`accounts.py`) has no session status (`ListedSession` carries none) and should not run an inventory for a preview. Replace `pause, then move (<account> <reason>)` with `move when idle (<account> <reason>)`, which is true for both paths; the `reap and restart now …` line stays.

**Tests:** `test_stop.py` — a stop whose fresh inventory shows `busy` writes progress `ready` (from `ready`), `waiting` (from `waiting` and from a stale `stopping` claim) and `held by usage limit`, keeps the `NotReadyToStop` issue, and never `stop failed`; a `"shell"` session passes the guard. `test_record.py` — `_read_record` turns a legacy not-ready `stop failed` record into its issue's progress, and leaves a `stop failed` with another reason or more issues alone. `test_settle.py` — a refused record is not `_done`, the next pass with status `idle` stops and restarts it, `report_stop` is not called while it waits; `_action_failed` after a refusal leaves only its own issue; `ready()` on a recovered record returns 0, records `ready` and calls `_launch_conductor` (patched), and a failed launch exits 2 with the command line; a `"shell"` session with idle workers in `waiting` (message not sent) gets `StopSession` with the shell suffix; one at `ready` with status `"shell"` gets `StopSession` and keeps its `where`; a `"busy"` session still gets the pause. `test_shutdown_cli.py` — status and `follow` show a recovered record as waiting, with its cancel and force commands, not as a failed move; a second move of the same session is still refused while the record is open. `test_account_move.py` — the preview line reads `move when idle (…)`.

**Files:**
- `scripts/shutdown/stop.py` — `_stop_owner`'s not-ready guard; the stoppable status set.
- `scripts/shutdown/settle.py` — `_STOPPABLE_STATUSES`, `next_step`, `_hold_details`, `_action_failed`, `ready`, the shell suffix.
- `scripts/shutdown/record.py` — `_read_record` legacy normalization.
- `scripts/accounts/accounts.py` — `move_preview_line`.
- `scripts/shutdown/test_stop.py` — the refusal and shell guard cases.
- `scripts/shutdown/test_record.py` — normalization cases.
- `scripts/shutdown/test_settle.py` — waiting, `ready`, shell and failure cases.
- `scripts/shutdown/test_shutdown_cli.py` — status and follow of a recovered record.
- `scripts/shutdown/test_account_move.py` — the preview line.
- `docs/as-built/shutdown-and-restart.md` — settle's not-ready refusal and stoppable statuses.
- `docs/accounts.md` — the preview line.

**Workers:** 1 writer + 1 tester.
- writer: `stop.py`, `settle.py`, `record.py`, `accounts.py`, the two docs.
- tester: `test_stop.py`, `test_record.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_account_move.py`.

**Constraints from prior phases:** Phase 1's idle stops record `idle; nothing in progress` or `idle after the pause message; no ready line`; keep both. Inventory's `Session` carries `updated_at` (registry ms) since Phase 1. `--dry-run` with `--no-alert` also prints each planned `shutdown.py down … --no-alert` command (Phase 2). The pause text's second line names `shutdown.py status <account label> --here` (Phase 2). Two open claude1 records from 2026-10-14 (tile-fixes `da82452f…` at `ready`, dashboard-unit `cf4e3c32…` at `stop failed`) are a live operational matter for the unit director and the user, never touched by tests.

**Acceptance gate:** the named tests pass; the shutdown, accounts, lib, production and whoami suites are green; basedpyright 0/0 on changed files.

### Phase 5 — A conductor runs the code on disk  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** a long-running shutdown conductor never carries out a request with code older than what is installed: when its modules change on disk it finishes what it has in hand and restarts itself in place, losing no outcome.

**Spec:**
- Found live in Phase 3: a claude1 conductor started at 11:34:29 EDT kept running pre-Phase-1 code until it was stopped at 12:48, held `conduct.lock`, and paused two idle sessions that the installed code would have stopped at once. It stayed alive because `conduct` (`settle.py`) loops until every record is done and one record stuck at `ready`. Phase 4 keeps a not-ready refusal's record open, so a conductor now lives until that session goes idle; that makes this phase more needed.
- **Entry shape.** `_launch_conductor` (`settle.py`) starts `python -u shutdown.py conduct <login>` as a transient systemd user unit. `settle.py` has no entry point of its own. Re-exec with `os.execv(sys.executable, [sys.executable, "-u", *sys.argv])`, which repeats that exact command; test it against the argv `_launch_conductor` builds.
- **Fingerprint.** At `conduct` start, record the mtime and size of the modules it runs: `shutdown.py`, `settle.py`, `stop.py`, `record.py`, `inventory.py`, `restart.py` in `scripts/shutdown/` (resolved from `Path(__file__).parent`). One private function and one private frozen type (`_ModuleFingerprint`); no public API. Compare once per pass of `_conduct_owned`'s loop: six `stat` calls.
- **Drain before reload.** On a change, enter a reload-requested state (a named private type, not a bool): take no new step on any record; let every owned action finish and be consumed — `pending` futures, `sends`, `messages`, `confirmations` and the `launched` queue; then leave `_conduct_owned` as it does today (desktop state restored), release the lock, and exec. Print one line to the conductor log naming the change, with its time in the user zone.
- **Outcomes survive.** `conduct` keeps restarted sessions' outcomes in the in-memory `archived` dict for its final `report_stop`. Before exec, write them to `conduct_archive.json` in the login's state directory (atomic write); at `conduct` start, read, merge and delete it. A reload never repeats a launch and never drops an outcome from the final report.

**Tests** (`scripts/shutdown/test_settle.py`): a changed fingerprint on the second pass, `os.execv` patched — `execv` called once with `[sys.executable, "-u", *argv]`, the lock released first, no step taken on any record after the change; a change while a launch, a send, or a registry confirmation is in flight — no exec until each finishes and is consumed; an unchanged fingerprint — never `execv`; one owner archived before the reload and another finishing after it — the final `report_stop` holds both, and no session is launched twice.

**Files:**
- `scripts/shutdown/settle.py` — `conduct`, `_conduct_owned`, `_ModuleFingerprint`, the reload state, archive persistence.
- `scripts/shutdown/test_settle.py` — the four tests.
- `docs/as-built/shutdown-and-restart.md` — the conductor's reload.

**Workers:** 1 writer + 1 tester.
- writer: `settle.py`, the as-built doc.
- tester: `test_settle.py`.

**Constraints from prior phases:** `settle` imports `record as store`; `record.py` owns `conductor_lock`, `stop_incomplete` and the state directory. `conduct` returns 0 on `ConductorAlreadyRunning`, so the re-exec'd process must find the lock free. Tests start no session and send no message or push. After the showrunner installs this phase, the unit director lists `systemctl --user list-units 'shutdown-*'`; a conductor started before the install has no reload check, so it is reported to the showrunner with its open records rather than stopped unasked.

**Acceptance gate:** the four tests pass; the shutdown, accounts, lib, production and whoami suites are green; basedpyright 0/0 on changed files.

## Recorded results

Ghostty leg, natedev, 2026-10-15, 11:23 to 11:35 EDT, self-run (no one at the desk). Fold this into `docs/as-built/shutdown-and-restart.md` at Phase 1's checkpoint:

- **Launch:** 11:23:25, `systemd-run --user --collect --quiet -- ghostty -e zsh -ic 'cd /home/natepiano/postmaster && script -qf -c "accounts.py exec claude4 --model haiku -n pm-move-ghostty" <log>; exec zsh -i'`, with `RELAUNCH_SUPERVISED` unset. `script` sits under the window's zsh and records every byte written to the terminal.
- **Before the move:**
  - Inventory host: `ghostty`, `window_shell` 4044053 (zsh, pts/57), supervisor pid 4044873. The desktop read `{named, "unknown"}` on this first read and `natedev` after the move.
  - KWin, read through kdotool queries only: window `{76f8590c-…}`, pid 4039675, desktop 4, 800x628 at 4760,406.
- **Codeword:** `cobalt-heron-4417`, sent 11:24:49, acknowledged `noted` 11:24:53. The preview at 11:25:01 read `pause, then move (claude4 has room)`.
- **First move:** `accounts.py move` at 11:25:14. The session declined the pause twice, sent a holdout push to the user at 11:29:33, and the move was cancelled at 11:30:33. This is why Phases 1 and 2 exist.
- **Second move:** `shutdown.py down claude4 --here --only <id> --restart --to claude1 --why "live check" --no-alert` at 11:31:42, then `now` at 11:31:57. The session stopped at 11:32:28 and restarted at 11:32:30, ending `all 1 back on claude1`.
- **After the move:**
  - Same Ghostty pid and KWin window id, desktop 4, 800x628; the position read 4037,372, and the user had the window in view.
  - Same shell (4044053, pts/57) and the same supervisor pid and start time.
  - The new child, `claude --resume <id> … --remote-control pm-move-ghostty`, runs under the same supervisor.
- **Scrollback:** the 57,554-byte terminal stream holds no `ESC[3J`, `ESC c` or `ESC[!p`.
  - Claude draws in the alternate screen: `ESC[?1049h` at the start, `ESC[?1049l` at the stop, `ESC[?1049h` again at the resume.
  - Between the two runs the main screen received `Resume this session with:`, the supervisor's `claude stopped for a restart; waiting for it here` and the resume prompt. The main-screen scroll history is kept.
- **Memory:** asked on claude1 at 11:33:37, the session answered `cobalt-heron-4417` at 11:33:51, read from its claude1 transcript.
- **Unproven:** no screenshot. The window was on desktop 4 while desktop 1 was current, and switching desktops is not read-only.
- **Cleanup:** `down claude1 --here --only <id> --no-alert` at 11:34:12, then `now`; `all 1 stopped` at 11:34:48. The window closed and Ghostty exited.
