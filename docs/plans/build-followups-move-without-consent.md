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

#### As-built

- A not-ready refusal in `stop._stop_owner` is no longer a failure: it writes the `NotReadyToStop` issue's progress (`waiting`, `ready` or `held by usage limit`; a stale `stopping` claim returns to `waiting`) and keeps that one issue in `stop_issues`. The record stays open, `report_stop` does not report it, and a later conductor pass stops it once the session is idle, under every existing rule.
- `record._recover_not_ready_stop`, called from `record._entry` (the validated entry parser every reader goes through, for legacy account files and current session records), turns an old `stop failed` with reason exactly `session or worker is not idle and ready` and exactly one `NotReadyToStop` issue into that issue's progress.
- `settle._action_failed` clears `stop_issues` when it writes `stop failed`, so a stale refusal never makes a real failure look recoverable.
- `settle.ready()` records `ready`, then starts the account conductor with `_launch_conductor(login, label)`; a conductor already holding the lock means the new one exits. On `OSError`, `RuntimeError` or `subprocess.SubprocessError` it keeps the ready mark, prints `ready recorded; conductor could not start; in a terminal on <machine>: <conduct command>` to stderr and exits 2.
- `settle._STOPPABLE_STATUSES = frozenset({"idle", "shell"})` with the public predicate `settle.stoppable_status(status)`, used by `next_step`'s idle test, `_hold_details` and `stop._stop_owner`'s guard (through the `ShutdownReadiness` protocol). Workers still require `"idle"`; `remote_control.py` still requires `"idle"`.
- With status `shell`, the automatic idle stops read `idle; nothing in progress; a background shell was running` and `idle after the pause message; no ready line; a background shell was running`; both are members of `stop.IdleStopReason`. `stop_session` writes the idle reason as `where` only when the prior progress is `waiting`, so a session's own `ready --where` text is kept.
- `accounts.move_preview_line` prints `move when idle (<account> <reason>)`; the `reap and restart now …` line is unchanged.

**Files:**
- `scripts/shutdown/stop.py` — `_stop_owner` refusal progress, `IdleStopReason` shell variants, `stop_session` `where` rule.
- `scripts/shutdown/settle.py` — `_STOPPABLE_STATUSES`, `stoppable_status`, `next_step`, `_hold_details`, `_action_failed`, `ready`.
- `scripts/shutdown/record.py` — `_recover_not_ready_stop` in `_entry`.
- `scripts/accounts/accounts.py` — `move_preview_line`.
- `docs/as-built/shutdown-and-restart.md` — not-ready refusal, legacy recovery, stoppable statuses, `ready` conductor launch.
- `docs/accounts.md` — the `move when idle` preview line.
- `scripts/shutdown/test_stop.py`, `test_record.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_account_move.py` — refusal progress, recovery, shell stops, `ready` launch, status and `follow` of a recovered record, the duplicate-move refusal, the preview line.

**Binds later work:** `ready()` launches the account conductor and a conductor holding the lock keeps the request, so a code reload inside the running conductor is the only path for new code to reach an open request ("A conductor runs the code on disk"). Recovered legacy records read as open everywhere.

**Gotchas:** recovery matches only the exact historical reason with exactly one `NotReadyToStop` issue; any other `stop failed` stays terminal. `record._read_record` reads only legacy account files, so recovery there would miss current session records.

**Ruled out:** counting `shell` as idle for workers or for Remote Control typing — a running shell may still be using the terminal.

### Phase 5 — A conductor runs the code on disk  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** a long-running shutdown conductor never carries out a request with code older than what is installed: when its modules change on disk it finishes what it has in hand and restarts itself in place, losing no outcome.

**Spec:**
- Found live in Phase 3: a claude1 conductor started at 11:34:29 EDT kept running pre-Phase-1 code until it was stopped at 12:48, held `conduct.lock`, and paused two idle sessions that the installed code would have stopped at once. It stayed alive because `conduct` (`settle.py`) loops until every record is done and one record stuck at `ready`. Phase 4 keeps a not-ready refusal's record open, so a conductor now lives until that session goes idle; that makes this phase more needed.
- **Entry point.** `_launch_conductor` (`settle.py`) starts `python -u shutdown.py conduct <login>` as a transient systemd user unit. `settle.py` has no entry point of its own. Re-exec with `os.execv(sys.executable, [sys.executable, "-u", *sys.argv])`, which repeats that exact command; test it against the argv `_launch_conductor` builds.
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

**Constraints from prior phases:** `settle` imports `record as store`; `record.py` owns `conductor_lock`, `stop_incomplete` and the state directory. `conduct` returns 0 on `ConductorAlreadyRunning`, so the re-exec'd process must find the lock free. Since Phase 4, `ready()` records its checkpoint and then calls `_launch_conductor`, so a conductor already holding the lock keeps that request; the reload is the only way new code reaches it. Legacy not-ready `stop failed` records are recovered as open records in `record._entry` (every reader, not only `_read_record`), so a reloaded conductor picks them up with no extra step. Tests start no session and send no message or push. After the showrunner installs this phase, the unit director lists `systemctl --user list-units 'shutdown-*'`; a conductor started before the install has no reload check, so it is reported to the showrunner with its open records rather than stopped unasked.

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
