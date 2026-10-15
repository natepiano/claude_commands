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
  - `scripts/shutdown/settle.py` — `begin` (~720: every non-seat session gets `SessionWaiting` unless `prompt_state` says it stopped taking prompts, then `SessionReaping`), `next_step` (~833: `waiting` → `SendSettleMessage` when not sent, re-ask after `REASK_AFTER`; stop only on `ready`/`held by usage limit` with the session idle, every seat idle, `blocker(...)` clear and no form showing), `_settle_text` (~614, the pause text; first line `Pause for move: <label> → <target> (<why>, <time>)`), `_holdout_due` (~1454, gated on `saved["alerts"] == "user"`), `holdout_line`, `_send_holdout_alert`, `ready` (~907), `now` (~1053), `down` (~1124).
  - `scripts/shutdown/record.py` — `WhereSaid` / `WhereNotSaid` (~200), the `where` parser (~624), `SessionShutdown.alerts: Literal["user", "none"]`.
  - `scripts/shutdown/inventory.py` — `Session` (`status: str` ~236/314, from the registry wire record ~711). `scripts/message/sessions.py` reads the registry's `updatedAt` (ms) and does not pass it to inventory today.
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

#### Work Order

**Goal:** on natedev, an idle Ghostty session moves with no pause message and a declining tmux session is stopped once idle, with no push.

**Spec:** unit director only, installed main after the merge, throwaway Haiku sessions in `/home/natepiano/postmaster`, every command with `--no-alert`:
1. Idle Ghostty session (launched as in **Recorded results**): `accounts.py move <id> claude1 --no-alert`. Expect no pause message in its transcript, the stop recorded as `idle; nothing in progress`, stop and restart within seconds, the same KWin window id, desktop and size, the same shell and supervisor, codeword recalled.
2. Tmux session asked by message to decline any hand-off: move it with `--no-alert`. Expect it to answer, go idle, and be stopped with `idle after the pause message; no ready line`, with no push.
3. Clean up with `down claude1 --only <ids> --here --no-alert`, close the windows, delete logs. Record results in the as-built Results.

**Files:**
- `docs/as-built/shutdown-and-restart.md` — Results.

**Workers:** none — live steps belong to the unit director.

**Constraints from prior phases:** Phases 1 and 2 installed on main. `accounts.py move <id> claude1 --dry-run --no-alert` prints each planned `shutdown.py down … --no-alert` command and changes nothing; run it before each leg. A busy session's pause message now has a second line naming `shutdown.py status <account label> --here`, which lists the session under the move; leg 2's session may run it before it answers.

**Acceptance gate:** both legs behave as stated; no push sent.

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
