# Account move: instant preview, every session handled by its state, restart in place, clear signals

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `accounts.py move` previews in under half a second, reaps and restarts at once a session that has stopped taking prompts, pauses the rest first, restarts a ghostty session in its own window, and tells the user at every step what is happening and where to act.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md` (stop, restart, records, conductor), `docs/accounts.md` (the move command)

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-14. The user, through the showrunner, after the 12:57 EDT `accounts.py move showrunners 'claude 1'` (claude 3 → claude 1): "completely broken from my standpoint and it needs to be fixed". Points: the dry-run took tens of seconds and must be instant; labels without a space (claude1, codex1; owned by two-accounts' "Accounts named by number only", not this plan, showrunner 2026-10-14); a closing line that names a skill without saying where to run it; each session checked for room, credits, or having stopped taking prompts, a stopped one reaped and restarted at once on the desktop it is on, the rest told to stop first; none of the four showrunners replied ready, two came back only after the user exited them by hand, one in a new window that lost its scroll history, and the user had no signal of what was happening or what to do. Root causes, measured 2026-10-14 from the code, the four records under `~/.local/state/shutdown/rubrics.dogwood-1f@icloud.com/history/2026-10-14T16:57:39*`, the transcripts and `journalctl --user -u shutdown-claude-3-3079277.service`, are folded into each phase's Spec. The showrunner approved the plan and added the hanadocs `agent login.md` update (the user's request).

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Project started:** 2026-10-14T18:22:35.591+00:00
- **Stack:** Python 3.13 standard library (`python3`; `~/.claude/scripts/lib/py` is the same binary and the form installed commands use), checked by basedpyright. Launch strings are zsh. No `Any`, a `TypedDict` per JSON shape, no file-level ignores.
- **Layout:** `scripts/shutdown/` (inventory, settle, stop, restart, record, the `shutdown.py` CLI, tests beside them) · `scripts/accounts/` (accounts, refusals, refusal_watch, moves) · `scripts/lib/` (new `relaunch.py`) · `scripts/whoami/usage_store.py` (read only) · `scripts/notify/` (push) · `commands/shutdown.md`, `commands/showrunner/accounts.md`, `commands/showrunner/move.md` · `docs/accounts.md`, `docs/as-built/shutdown-and-restart.md`. No `__init__.py`; modules put sibling dirs on `sys.path` at import (inventory.py:17–19 adds whoami/message/production/herdr; accounts.py:32–33 adds the repo root and whoami).
- **Key files:**
  - `scripts/shutdown/inventory.py` — per-login live session inventory. `WindowHost` (`kind` ghostty|zed, `window_shell`, `desktop`); `terminal_kind`; `_terminal_host` (walks ppid up from the claude pid; the first ghostty ancestor ends the walk, `window_shell` is its direct child zsh); `transcript_path(config_dir, session_id) -> Path | None` (one-level `config_dir/projects/*/<id>.jsonl`, newest by mtime); `last_model(path)` (backward 64 KiB blocks); `_model(session_id, config_dir: Path)` (config_dir required); `inventory(login, only: frozenset[str] = frozenset(), *, refresh_desktops=True)` (those ids plus the workers they own, every session when empty; full records only for the selection); `listing(login | None) -> list[ListedSession]` (frozen: `session_id, pid, proc_start, name, kind, config_dir: Path, login, production, doc`; no git, transcripts, timers or hosts); `unregistered_showrunners`. Since Phase 1 (8066673c); line numbers moved, find by name.
  - `scripts/accounts/refusal_watch.py` — refusal watcher. `_last_transcript_entry` 163–179 (last nonempty line only); `transcript_refusal` 182–218 (finds `glob("*/<id>.jsonl")` one level, a test at test_refusal_watch.py:889 pins that; checks `type`/`isApiErrorMessage`/`error` at 195; window parsed from the text at 216); `assumed_refusal` 221; `session_refusal` 236; state root `~/.local/state/accounts` (107).
  - `scripts/accounts/refusals.py` — `stored_reading` 68–82 (a reading is fresh when its weekly part is ≤10 min old), `_reading_window` 58, `load` 133, `resolved` 189.
  - `scripts/whoami/usage_store.py` — `store_file()` 66 (`~/.local/state/agent-notes/usage.json`, rewritten about every 2 min by `agent_notes.py`), `key` 43 (Claude by login; Codex `codex:<login>`), `load` 99. A Claude entry holds `five_hour` and `weekly` (`left` percent, `at`, `resets`); a Codex entry also holds `credits` (`balance`). Claude credits are recorded nowhere on this machine.
  - `scripts/shutdown/settle.py` — conductor and verbs. `REASK_AFTER` 49 (2 min), `NOT_ANSWERING_AFTER` 50 (4 min), `USAGE_LIMIT_PATTERNS` 52–58; `send_message` 242; `alert_user` 274; `_screen` ~401–432 (tmux pane capture; returns `ScreenUnreadable("no pane host")` for ghostty); `_held_by_usage_limit` 443; `_usage_limit_shown` 450–459; `_usage_limit_change` 462–473; `_apply_usage_limit_change` 476–498; `_progress_text` 558; `status_record_lines` 577; `begin` 614 (sets `force="wait for ready"` at ~640, runs an inventory at ~624); `_hold_details` 681; `hold_reason` 711; `next_step` 720–768 (session absent / identity lost → stop at ~738; `ready` or `held by usage limit` + idle + no blocker → stop at 744–747; reask at 755–757); `ready` 792; `cancel` 860; `now` 938; `_launch_conductor` 995–1006 (`systemd-run --user --collect --quiet --no-block --unit shutdown-<label-slug>-<pid> python3 shutdown.py conduct <login>`; darwin `launchctl submit`); `down` 1009–1062 (runs `github-warm-status` ~507, a second inventory ~1018; cold-key line 1014, peer-unreachable line 1058, started line 1061 `shutdown of <label> started; /shutdown status to watch, /shutdown cancel to undo`); `report_stop` 1095–1163 (`print(text)` 1161 for a terminal requester); `_holdout_due` 1315; `_reserve_holdout_alert` 1323; `_send_holdout_alert` 1340–1362 (pushes only when the pane shows a form); `_tell_holdout_requesters` 1365–1370 (prints for a terminal requester); `conduct` 1409; `_conduct_owned` 1427.
  - `scripts/shutdown/shutdown.py` — CLI. `_host_text` 136, `_session_lines` 171, `_status` 208, `_record_line` 281, `_shutdown_record_lines` 314, `resolve_sessions` 359 (a full unfiltered inventory of the account at ~364), `_status_actions` 439, `main` 515 (usage text ~527).
  - `scripts/shutdown/record.py` — record store. Progress types `SessionWaiting`…`SessionNeedsManualRestart` 211–291, union `SessionProgress` 294–309; `SessionShutdown` 351–381; `_RECORD_STATES` 415; `_PROGRESS_WITH_TIME` 427; `_progress` parser 613; target check ~962; `update_session` 1183; `archive_session` 1207 (moves the final file to `<state>/<login>/history/<requested_at>-<file>`); `conductor_lock` 1534. State root `$SHUTDOWN_STATE_DIR` or `~/.local/state/shutdown` (launch_permission.py:60). Kinds today: `waiting`, `ready`, `held by usage limit`, `stopping`, `stopped`, `already gone`, `process identity lost`, `stop failed`, `restarting`, `restarted`, `timers pending`, `restart failed`, `manual restart` (legacy `passive seat ready`, `seat available on demand`). Each replaces `progress` in place; no trail.
  - `scripts/shutdown/stop.py` — `_ghostty_still_owns_shell` 412, `_close_ghostty_window` 420–437 (SIGHUP to the window shell), `_stop_owner_resources` 440, `_stop_owner` 520 (session-absent path "already gone" 554–566; SIGTERM 590; second SIGTERM 654), `stop_session` 714.
  - `scripts/shutdown/restart.py` — `wait_for_confirmation` 399; `ResumeParts` 499; `_resume_parts` 509–545 (group sessions resume on their group's account; a move target makes `MoveResumeAccount`); `_resume_shell` 548–557 (`cd … && <resume>; exec zsh`, through `accounts.py exec <target>` on a move); `_desktop_numbers` 691; `_window_launch` 712–758 (switches to the session's named desktop with kdotool, runs `systemd-run --user --collect --quiet -- ghostty -e zsh -ic CMD`, a new window); `restore_desktop` 761; `_respawn_pane_argv` 868; `_tmux_launch` 899–972 (tmux respawns the pane in place); `_launch_session` 1020–1093 (ghostty/zed → `_window_launch` at 1062); `clear_running_on_target` 1274–1307; `confirm_entry` 1508. Test overrides: `SHUTDOWN_GHOSTTY`, `SHUTDOWN_SYSTEMD_RUN`, `SHUTDOWN_KDOTOOL`, `SHUTDOWN_TMUX`, `SHUTDOWN_OPEN`.
  - `scripts/accounts/accounts.py` — `LABEL_PATTERN` 140; `prepared_account` 1020; `exec_account` 1055–1067 (`os.execvpe("claude", …)`; `lib/py` execs python, so claude replaces both and is a direct child of the shell); `session_group` 1484; `live_group_members` 1492–1509 (one full `inventory.inventory()` per live login); `move_sessions` 1994–2096 (one inventory per session, then `shutdown.py down <src> --here --only <ids> --restart --to <target> --why <why>` per source login, then the moves journal); `_move_target` 2138–2152; `move_group` 2155–2196 (dry-run returns before `set_group_account`; unregistered showrunner notices 2176–2181); `session_on_account` 2199; `move` 2208; `_parser` 2810 (`move` subparser 2828); `main` 2851 (move dispatch 2868).
  - `scripts/accounts/moves.py` — move journal `~/.local/state/accounts/moves.jsonl`, `append_move` 101.
  - `scripts/notify/pushover.py` — `send` 242, `deliver` 355; `scripts/notify/user_action.py`; `scripts/notify/review.py texts --id … --approve` re-approves a changed push text.
  - Tests:
    - `scripts/shutdown/test_support.py` — `ShutdownTestCase` blocks every send leaf (`_deliver_message`, `_retire_shutdown_messages`, `restart._retire`, `run_remote`, `_launch_conductor`, `run_notifier`); `allow_leaf()` re-enables one.
    - `test_inventory.py` — `InventoryTests` 53; HOME is `root/home` with `Path.home` patched; sets `NOTIFIER_*`, `PLAN_DELEGATE_HISTORY_DIR`, `SHUTDOWN_DELEGATE_ROOT`, fake tmux and fake herdr; `_model` tests at 69 and 84 write transcripts under `home/.claude/projects/<dir>/<id>.jsonl`. `ProcessEnvironmentTests` 1092.
    - `test_settle.py` — `SettleTests` 227 (114 tests); HOME, `SHUTDOWN_STATE_DIR`, `NOTIFIER_SESSIONS_DIR` under a temp dir; patches `now_utc`, `merge_in_progress`, `_showing_form`; usage-limit tests 378–633; conductor launch test 251.
    - `test_record.py` — `SessionRecordTests` 176. `test_stop.py` — `StopTests` 230, `FreshInventorySequence` fixture, patches `record_time`. `test_restart.py` — `RestartTests` 271 (151 tests), `CommandRecorder` and the `entry/seat/showrunner/top_level/tmux_host/unit` builders other modules reuse; env HOME, `SHUTDOWN_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `CONVERSATION_PAUSE_STATE_DIR`, `AGENT_NOTES_DIR`.
    - `test_shutdown_cli.py` — `ShutdownLaunchCliTests` 127, `OnlySessionCliTests` 818, `setUpModule` 121; env `clear=True`, pops `CLAUDE_CONFIG_DIR`, runs the CLI as a subprocess. `test_shutdown_status.py` — `ShutdownStatusTests` 116, fake remote output via `SHUTDOWN_STATUS_*`.
    - `test_account_move.py` — `AccountMoveTests(ShutdownTestCase)` 31; patches `sessions.live_sessions`, `account.process_config_dir`, `inventory.inventory`; single moves 110–243, group moves 334–459, dry-run 522.
    - `scripts/accounts/test_refusal_watch.py` — `RefusalWatchTests` 53; env `HOME`, `PATH=os.defpath`, `AGENT_NOTES_DIR`, `SHUTDOWN_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `USER_ZONE`; patches `subprocess.run`, inventory, `live_sessions`, datetime (`WatchTime`); `transcript()` fixture 143.
    - `scripts/accounts/test_accounts.py` — `AccountsTests` 110, `PickerTests` 2684; `setUpModule` points `NOTIFIER_STATE_DIR` and `SEATS_RUN_ROOT` at temp dirs; env `clear=True`; stubs `claude_live`, codex reads, `codex_mesh`.
- **Test lanes:** `scripts/shutdown/` → `test_*.py` beside the code, importing siblings by bare name (`import inventory`, `from test_support import ShutdownTestCase`). `scripts/accounts/` → `test_*.py` importing `from scripts.accounts import accounts, refusal_watch, refusals, moves` (repo root from cwd) plus bare `account`, `agent_accounts`, `usage_store`. `scripts/lib/` → `test_*.py` beside the code.
- **Build:** none; Python is interpreted.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py' </dev/null` and `python3 -m unittest discover -s scripts/accounts -p 'test_*.py' </dev/null`; for `scripts/lib/relaunch.py`, `python3 -m unittest discover -s scripts/lib -p 'test_relaunch.py' </dev/null`. Each test sets its own temp `HOME` and state. Also run `scripts/whoami`, `scripts/production` and `scripts/nightly_review` the same way when a shared module changes. A phase's gate passes under the temp HOME the tests set, and again with the real HOME in the environment.
- **Lint:** `basedpyright <changed .py files>`; a pass is the last line `0 errors, 0 warnings, 0 notes` (the exit code is 3 everywhere because of the missing `.venv`; read the line). Lint once after the worker's edits; on failure fix what it names, then lint once.
- **Invariants:**
  - Tests start no real session, send no message or push, and never touch `~/.claude*`, `~/.claude.json` or live state; cases derive from `ShutdownTestCase` with fake send, notifier, process, tmux and host commands. A test reaching `user_zone()` patches `showrunners.NOTIFIER_STATE_DIR`.
  - Account isolation: a command for account A never stops or messages a session on B. An unreadable account means unattributed, never the default. Absent `CLAUDE_CONFIG_DIR` means `~/.claude`.
  - A Claude process gets only SIGTERM, and identity (pid + proc start) is checked before any signal. A ghostty shell gets SIGHUP only after the ownership check. Nothing ever types into a pane.
  - A relaunch is proven by the registry (`sessions.live_sessions`), never by pane text. Only `AllowedByShutdownState` permits a launch.
  - Record times are UTC ISO at whole seconds; times shown to the user are in the user's zone (`scripts/lib/user_zone.py`).
  - `account.py`, `agent_accounts.py` and `note_file.py` never import `scripts.accounts`; a lazy import counts as a cycle. `scripts/lib/relaunch.py` imports nothing from `scripts/accounts` or `scripts/shutdown`.
  - A changed push text is re-approved with `scripts/notify/review.py texts --id … --approve`.
  - Account labels are spelled as two-accounts' "Accounts named by number only" leaves them on `build-followups` (it moves them to `claude1`/`codex1`); this plan never spells a label itself in code, it uses the label functions, and tests build labels through them.
  - Every user-facing line that names a command or skill also names where to run it: `in this terminal`, `in a terminal on <machine>`, or `in any Claude session on <machine>`, and gives the account and the `--only` ids the command needs. A script path is written in full (`~/.claude/scripts/lib/py ~/.claude/scripts/…`).
  - Live steps belong to the unit director, never to a worker, and run only on throwaway sessions; a dry-run of the real group is allowed (it writes nothing).
  - Saved run output stays under a few GB; read each run and delete it before the next.

## Phases

### Phase 1 — Inventory reads only what it needs  · status: done

#### As-built

- `inventory(login, only: frozenset[str] = frozenset(), *, refresh_desktops=True)` keeps its single selection parameter: the given ids plus the workers they own, every session when empty. Full records (model, transcript) are built only for the selected sessions.
- `transcript_path(config_dir: Path, session_id: str) -> Path | None` does a one-level glob of `config_dir/projects/*/<id>.jsonl`, newest match wins, no recursive walk.
- `last_model(path: Path) -> str | None` reads the transcript backward in 64 KiB blocks and stops at the newest model line. `_model(session_id, config_dir: Path)` requires `config_dir`.
- `listing(login: str | None = None) -> list[ListedSession]` is the cheap per-session listing. `ListedSession` is frozen with `session_id, pid, proc_start, name, kind, config_dir: Path, login, production, doc`; it is built per call and never persisted.
- On natedev, listing 40 sessions takes 0.25 s, `_model` over all of them 0.02 s, and a one-session inventory 0.38 s (was 91 s).

**Files:**
- `scripts/shutdown/inventory.py` — session listing, selection, transcript lookup and backward model read.
- `scripts/shutdown/test_inventory.py` — selection semantics, transcript lookup, backward read and listing tests.

**Binds later work:** `ListedSession` is consumed by the move command's member listing via `listing(login)`. Callers that need one session or a named set call `inventory(login, only=frozenset(ids))`, which includes the workers the ids own; `down --only <director>` therefore includes that director's workers.

**Gotchas:**
- Any caller that wants one session must pass `only`; filtering after a full inventory reads every transcript. The old 91 s came from unfiltered callers plus a recursive glob over a 19 GB projects tree and whole-file reads.
- `_model` reports "no reply yet" for a session whose transcript lives under another account's projects dir; harmless while every config dir's `projects` is the same tree by symlink.

**Ruled out:** a second selection parameter with exact-id semantics beside `only`, because settle, stop, accounts and refusal_watch rely on the worker-expanding one.

### Phase 2 — A session's state decides its path  · status: done

**Depends on:** Phase 1

#### As-built

- `refusal_watch.transcript_refusal(session_id, *, config_dir: Path | None = None) -> TranscriptRefusal | NoSessionRefusal` matches when the newest exchange is an assistant entry with `isApiErrorMessage` and `error == "rate_limit"`. `_newest_exchange(path) -> NewestExchange | NoExchange` reads backward in 64 KiB blocks and skips every line whose `type` is not `user` or `assistant`, so the bookkeeping lines Claude Code writes after a refusal no longer hide it. Window and reset come from `quotaLimits` (`rateLimitType`, `resetsAt`) when present, else from the text; `TranscriptRefusal` carries the reset.
- `settle.prompt_state(PromptSubject(session_id, login, config_dir), now) -> SessionPromptState`, where `SessionPromptState = TakesPrompts | StoppedTakingPrompts`. Order: a transcript refusal → `StoppedTakingPrompts("refused in its transcript")`; a fresh reading with a window at 0 → `StoppedTakingPrompts("account out of usage")`; a fresh reading with both windows → `TakesPrompts("room")`; otherwise `TakesPrompts("no fresh reading")`. `detail` reads like `weekly limit, resets Thu 9:00 AM` in the user's zone. Claude sessions only: `TakesPrompts("credits")` exists in the type but no session produces it, since Codex workers have no attributed login.
- `begin` records a `StoppedTakingPrompts` session at progress `reaping` (`SessionReaping`: `because`, `detail`) with no pause message; `next_step` returns `StopSession` for it on the first tick, and `then: restart` relaunches it through the normal restart path. A `waiting` session whose transcript shows a refusal newer than the pause message's send time moves to `reaping` on the next tick, on every host, and gets no re-ask; the tmux screen check remains a second detector.
- Every record carries `trail: list[ProgressStep]` (`at`, `kind`, `detail`); `store.update_session` appends a step stamped at write time whenever progress changes, `create_session` writes the first step, `progress` stays the newest, a record without `trail` reads as an empty list, and `archive_session` keeps the trail in the history file. `status` prints each step as `<HH:MM:SS> <kind> <detail>` in the user's zone.
- `shutdown.resolve_sessions(login, items, *, here=False) -> ResolvedSessions(session_ids, report)` resolves exact names and ids from `inventory.listing(login)` and the records, then runs one `inventory(login, only=…)`; `down` passes that report to `settle.down` and `begin` (`report: Inventory | InventoryNeeded`), which inventory only when called without one. Without `--only`, `down` inventories once and passes it the same way.
- Each conductor tick calls `run_inventory(login, <live record ids>, refresh_desktops=False)`, and `stop._fresh_inventory` passes `refresh_desktops=False`, so neither runs the desktop snapshot.
- `stop.py`: a `reaping` owner skips the readiness and idle checks but keeps the pid and proc-start identity check before SIGTERM. `now` and `cancel` accept `reaping`; `restart --only` refuses a `reaping` record.

**Files:**
- `scripts/accounts/refusal_watch.py` — `_newest_exchange`, `transcript_refusal` with `config_dir` and `quotaLimits`.
- `scripts/shutdown/settle.py` — `PromptSubject`, `SessionPromptState`, `prompt_state`, reaping at `begin`, refusal scan during the wait, filtered conductor tick, trail lines in status.
- `scripts/shutdown/record.py` — `SessionReaping`, `ProgressStep`, `trail` and its writer in `update_session`.
- `scripts/shutdown/shutdown.py` — `ResolvedSessions`, name resolution from `listing`, one inventory passed down.
- `scripts/shutdown/stop.py` — reaping stop without readiness; identity reads without the desktop snapshot.
- `scripts/shutdown/restart.py` — `reaping` among the not-stopped progress kinds `restart --only` refuses.
- `scripts/shutdown/test_{settle,record,shutdown_cli,stop,restart}.py`, `scripts/accounts/test_refusal_watch.py` — each `prompt_state` branch, reaping through the real stop path, trail order and archive, name resolution, inventory call counts.

**Binds later work:** a reaped session restarts through `_launch_session`, so a reaped ghostty session follows whatever path "A ghostty session restarts in its own window" gives that function. The supervisor fields must be filled on the `refresh_desktops=False` path, which skips only the desktop snapshot; a relaunch is placed on the desktop stored in the record at request time. "The shutdown says what is happening and where to act" reads `trail` for `follow` and sends no holdout push for `reaping`. "The move command" builds its preview from `prompt_state(PromptSubject(...), now)`.

**Gotchas:**
- A record withdrawn while the conductor scans for refusals raises `store.NoLiveRecord`; the tick skips that record instead of ending.
- `transcript_refusal` defaults to `~/.claude/projects`; pass the session's `config_dir` for any other account.
- Running the full inventory each tick, desktop snapshot included (up to 60 s and a vault page rewrite), is what stretched one live run's ready-to-restart gap to 13 minutes; keep conductor reads filtered.

**Ruled out:** a credits branch for Codex workers in this phase, since the inventory attributes no login to them.

### Phase 3 — A ghostty session restarts in its own window  · status: done

**Depends on:** Phase 1

#### As-built

- `scripts/lib/relaunch.py run -- <argv>` is the window supervisor. It runs `<argv>` as a child on the same terminal, ignores SIGINT, SIGQUIT and SIGTSTP itself (caught handlers, so the child still gets them), and forwards SIGTERM and SIGHUP to the child. After the child exits it reads `STATE/relaunch/<own pid>.json` (`STATE` = `$SHUTDOWN_STATE_DIR` or `~/.local/state/shutdown`) as `RelaunchRequest(argv, cwd, environment, unset, written_at, note)`, deletes it and the hold, prints `note` as one line, and `os.execvpe`s itself with `run -- <argv>` in that cwd and environment, so the pid stays the same. Without a request, a `STATE/relaunch/<pid>.hold` younger than `HOLD_SECONDS` (300; `SHUTDOWN_RELAUNCH_HOLD_SECONDS` overrides) makes it print `claude stopped for a restart; waiting for it here` once and check every 0.5 s; on expiry it deletes the hold, prints `no restart came; this window is free` and exits with the child's code. A malformed request prints `could not restart here: <error>` and exits with the child's code. `write_hold(pid)` and `write_request(pid, request)` (temp file + fsync + rename) are the writers; standard library only.
- The supervisor sets no environment marker in its child; a process under it learns that only from its parent's argv.
- Inventory: `SupervisorIdentity(kind="supervisor", pid, proc_start) | NoSupervisor(kind="no supervisor")` sits as `supervisor: NotRequired[...]` on both `WindowHost` and `TerminalHost`. `_terminal_host` sets it from `_supervisor_process(parent)`, which matches the claude parent's argv for `relaunch.py run --` (`/proc/<pid>/cmdline`, `ps -o args=` fallback on the Mac); a supervisor that is the window shell itself reads as `NoSupervisor`. The field is filled on the `refresh_desktops=False` path too. `supervisor_identity(host)` reads a host from before this phase as `NoSupervisor`; `live_supervisor(host)` also rechecks pid + proc start.
- Supervised hosts are `ghostty` on Linux and `terminal` on darwin, where `terminal_kind` maps ghostty to `terminal`.
- Stop: `stop._hold_supervised_window(entry, claim) -> bool` writes the hold before SIGTERM when `then: restart` and the host has a live supervisor, on the live and already-absent paths alike; the stop then signals claude only and skips `_close_ghostty_window` (`retain_window`). A failed hold write ends the stop as `stop failed` with no signal sent. `then: stop`, or no supervisor, keeps the SIGHUP-after-ownership path.
- Restart: every window launch (`ghostty`, `zed`, darwin `terminal`) builds `_resume_shell(parts, supervised=True)`, which wraps the resume argv in `~/.claude/scripts/lib/py ~/.claude/scripts/lib/relaunch.py run -- …`. For a supervised host with a live supervisor, `_launch_session` writes `_relaunch_request(parts, note)` to `<supervisor>.json` instead of opening a window and returns `SessionLaunched()`; confirmation is the registry as before. `_relaunch_request` takes its environment from `accounts.prepared_account(target, resume=True)` on a move (its `remove` list into `unset`) and unsets `NAMED_ACCOUNT_VARIABLES` for the default account; `CLAUDE_CODE_OAUTH_TOKEN` is always unset. A gone or reused supervisor falls back to `_window_launch` on the session's named desktop (Linux) or the Mac open command.

**Files:**
- `scripts/lib/relaunch.py` — the supervisor, `RelaunchRequest`, state paths, `HOLD_SECONDS`, hold and request writers.
- `scripts/lib/test_relaunch.py` — the supervisor run as a subprocess over a fake child: exit code passthrough, same-pid request, late request under a hold, hold expiry, SIGINT ignored.
- `scripts/shutdown/inventory.py` — `SupervisorIdentity`, `NoSupervisor`, `_supervisor_process`, `supervisor_identity`, `live_supervisor`, wire parse of `supervisor`.
- `scripts/shutdown/stop.py` — `_hold_supervised_window` and the claude-only stop.
- `scripts/shutdown/restart.py` — supervised `_resume_shell`, `_relaunch_request`, the in-place branch of `_launch_session`.
- `scripts/shutdown/test_{inventory,stop,restart}.py` — supervisor discovery, hold and no-SIGHUP stop, unwritable hold, request written with no window, fallback window.

**Binds later work:** the supervisor runs on ghostty and darwin Terminal hosts, read through `supervisor_identity(host)` / `live_supervisor(host)`; the as-built doc covers the hold, both window lines and the new-window fallback, on both host kinds. `relaunch.py` sets no `RELAUNCH_SUPERVISED` yet; "The move command" adds it so `accounts.py exec` under `restart._resume_shell(parts, supervised=True)` does not nest a second supervisor. A move's request environment comes from `accounts.prepared_account(target, resume=True)`.

**Gotchas:**
- On darwin a ghostty window reads as `terminal`; its supervisor lives on `TerminalHost`, not `WindowHost`.
- A window started before this phase, or by hand outside a supervised launch, has no supervisor and still gets a new window.

**Ruled out:** reclassifying Mac windows as `ghostty`, since the fallback would then go through the Linux window launcher.

### Phase 4 — The shutdown says what is happening and where to act  · status: todo

**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** every line the shutdown path shows names where to run any command it gives; a terminal requester can follow each session's steps live; a session that holds out reaches the user as a push; the conductor's output appears as it happens.

**Spec:**

1. **Root causes.** `down` ends with `shutdown of <label> started; /shutdown status to watch, /shutdown cancel to undo` (settle.py:1165): a skill named with no account, no `--only` and no place, so typed in a session on another account it acts on that account. The conductor's later lines (`report_stop` 1199, `_tell_holdout_requesters` 1469–1474) print to the systemd unit's journal, block-buffered until the conductor exits, for a terminal requester; `_send_holdout_alert` pushes only when the pane shows a form, and a ghostty pane is never read. The 2026-10-14 user saw `<name>: added` four times, the closing line, then nothing for 13 minutes.
2. **`shutdown.py follow <account> --request <request id>`.** Prints one line per new trail step (Phase 2's `trail`) of every record in that request, as `<HH:MM:SS> <name>: <kind> <detail>` in the user's zone, reading the records every second (a local file read, no inventory). It ends when every record is final (`restarted`, `stopped`, `already gone`) with a summary line (`all 4 back on claude1` / `all 4 stopped`), or when one needs the user, with the line that says what to do and where. Ctrl-C ends the follow only and prints `the shutdown keeps running; to follow it again, in this terminal: <full follow command>`. Exit codes: 0 all done, 1 one needs the user, 130 detached.
3. **`down` from a terminal follows.** When `requested_by` is the terminal and stdout is a tty, `down` runs `follow` after starting the conductor, instead of the closing line. Otherwise (a session requester, or no tty) the closing line becomes: `shutdown of <label> started for <n> sessions. To follow it, in a terminal on <machine>: <full follow command>. To stop the ones not yet stopped, in a terminal on <machine>: <full cancel command with --only>.` A move's `down` (`--to`) says `stop the move of the ones not yet stopped`, and never says `undo`.
4. **Every next-step line names its place.** Sites, each rewritten to name `in this terminal`, `in a terminal on <machine>` or `in any Claude session on <machine>`, the account and the `--only` ids, with full script paths: settle.py 1116 (cold key: names `github-warm-status`/`github-warmup` and where), 1162 (peer unreachable), 1165 (above), the holdout lines (`waiting; /shutdown cancel --only …`), `_status_actions` (shutdown.py 446), the shutdown.py usage text (~534). A sweep test renders each of these with fixture records and asserts every line containing `/shutdown`, `shutdown.py`, `accounts.py` or `github-warm` also contains one of the three place phrases.
5. **A holdout reaches the user.** At `NOT_ANSWERING_AFTER`, a session still `waiting` (not reaping, not refused) gets one push through `alert_user` (settle.py 274) for every requester kind, not only for a pane showing a form: title `<name> has not answered the pause`, body naming the session, its window or pane, how long, and the exact command and place to stop it now (`shutdown.py now … --only <id>`). The push text gets a catalog entry and is approved with `scripts/notify/review.py texts --id … --approve` (unit director). A terminal requester following live also sees it as a follow line.
6. **The conductor's output is live.** `_launch_conductor` runs python with `-u` (and `PYTHONUNBUFFERED=1`), so journal lines appear as they happen.
7. **Docs.** `commands/shutdown.md` — the follow verb, the place phrases, reap vs pause (from Phase 2). `docs/as-built/shutdown-and-restart.md` — the prompt state, reaping, the trail, follow, the supervisor (Phase 3), holdout push.
- **Follow across archive and restart.** `follow` tracks the sessions of one request id and reads both active records and archived history files, since `archive_session` moves a finished record out at once. `stopped`/`already gone` are intermediate when `then == "restart"`: completion waits for the restart outcome. `process identity lost` ends that session's follow with its line naming what the user does. Tests: completion before follow starts, archive between polls, stop before a pending restart.

**Files:**
- `scripts/shutdown/settle.py` — closing lines, holdout push, `-u`, place phrases.
- `scripts/shutdown/shutdown.py` — `follow`, status actions, usage text.
- `scripts/notify/push_catalog.py` — the holdout push text (`PushText("shutdown.holdout", "shutdown", …)`, beside `shutdown.stop_failed`; its example facts from `test_settle.py`).
- `scripts/notify/push_texts_reviewed.json` — the approval `review.py texts --approve` writes (unit director).
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_shutdown_status.py` — the cases below, the sweep test.
- `commands/shutdown.md` — follow, place phrases, reap vs pause.
- `docs/as-built/shutdown-and-restart.md` — this plan's shutdown changes.

**Workers:** 1 writer + 1 tester — settle.py and shutdown.py share the follow and line formats, so one writer holds both; the docs go to the writer after the code.
- `impl` — every production file and doc above except `push_texts_reviewed.json`, which the unit director writes; hub: `scripts/shutdown/shutdown.py` (`follow`).
- `test` — `follow` over fixture records advancing step by step (prints each step once, ends with the summary, exit 0; one needing the user → exit 1 with its line); detach prints the follow command; `down` from a tty runs follow, from a non-tty prints the new closing line with account, `--only` and place; a move's line never says `undo`; the holdout push is sent once for a ghostty session with no form and not for a reaping one; the conductor argv carries `-u`; the sweep test.

**Constraints from prior phases:** "A session's state decides its path" adds `trail: list[ProgressStep]` (`at`, `kind`, `detail`) to every record, the `reaping` kind with `because`/`detail`, and `SessionPromptState`; `follow` reads the trail and a reaping session never gets the holdout push. "A ghostty session restarts in its own window" adds the supervisor on ghostty windows and on darwin Terminal hosts (`WindowHost`/`TerminalHost` carry `supervisor: SupervisorIdentity | NoSupervisor`, read through `inventory.supervisor_identity` / `inventory.live_supervisor`); a restart stop writes the hold first (`stop._hold_supervised_window`). The as-built describes it: the 300-second hold and its two window lines (`claude stopped for a restart; waiting for it here`, `no restart came; this window is free`), the in-place restart, and the new-window fallback when the supervisor is gone or reused.

**Acceptance gate:** the shutdown suite passes, also with the real HOME; `basedpyright` 0/0/0 on the changed files; the push text approved.

### Phase 5 — The move command  · status: todo

**Depends on:** Phase 1, Phase 2, Phase 3, Phase 4
**Blocked by:** two-accounts' "Accounts named by number only" checkpoint on `build-followups` (it holds `scripts/accounts/accounts.py` and spells the labels `claude1`/`codex1`); merge the merge branch first.

#### Work Order

**Goal:** `accounts.py move` previews in under half a second, refuses a target with no room, hands every session to the shutdown in one inventory, follows the move live in the terminal to its end, and `accounts.py exec` supervises claude so an alias-started session also restarts in its window.

**Spec:**

1. **Members from the listing.** Root cause of the 93 s preview: `live_group_members` (1492–1509) builds a full `inventory.inventory()` per live login to learn each session's kind and config dir. It uses `inventory.listing()` (Phase 1) instead; `session_on_account` and the dry-run's member lines read `ListedSession`. `move_sessions` (1994–2096) drops its per-session inventory and passes the ids to `shutdown.py down --only` (which inventories only them, Phase 2). The unregistered-showrunner check (2176–2181) reads the listing too; its line names where to run `update_registration.py` (inside that showrunner's own session) with the full path.
2. **The target must have room.** Before anything is written, `move_group` and `move` read the target's `refusals.stored_reading`: fresh and a window at 0 with no credits → refuse: `accounts: <target> is out of usage (weekly 0% left, resets <time>); pick another: <full pick command> in this terminal`. No fresh reading → go on and say `no fresh usage reading for <target> (last <age> ago)` on the preview and the run.
3. **The preview.** `move showrunners <target> --dry-run` prints, per member, its name, session id, and what will happen to it from Phase 2's `prompt_state`: `<name>: pause, then move (claude3 has room)` or `<name>: reap and restart now (claude3 out of usage: weekly limit, resets Thu 9:00 AM)`. Writes nothing, as today. Under 0.5 s on natedev with every live session (gate).
4. **The run follows itself.** After `down` starts, `move_group`/`move` run Phase 4's `follow` for each source login's request in this terminal and end with its summary; the closing lines are Phase 4's. The move-back line replaces `undo`: `to move them back, in this terminal: <full move command with the source account>`.
5. **`accounts.py exec` supervises.** `exec_account` (1055–1067) runs `relaunch.py run -- claude <arguments>` (Phase 3) through `os.execvpe` of the python interpreter when stdin is a tty and it is not already under a supervisor (`RELAUNCH_SUPERVISED` in its environment); otherwise it execs claude as today. `relaunch.run` does not set that marker yet: this phase makes it start the child with `RELAUNCH_SUPERVISED=1` in the child's environment (`subprocess.Popen(argv, env=…)`), so the restart path's `relaunch.py run -- accounts.py exec …` (`restart._resume_shell(parts, supervised=True)`) does not nest a second supervisor. The `claude1..3` aliases (`/etc/nixos/modules/linux/shell.nix:52–55`) need no change.
6. **Docs.** `docs/accounts.md`, `commands/showrunner/accounts.md`, `commands/showrunner/move.md`: the preview's per-session lines, the target check, reap vs pause, the live follow, the move back, same-window restart. `~/rust/hanadocs/agent login.md` (the user's request through the showrunner): rewrite "Move every showrunner off a spent account" and "Move a session by hand" to match — what the preview shows, that a spent session is restarted at once and the rest are paused, that the move stays in the terminal until all are back, that the window and its history are kept, and what to do on each line it can end with. The unit director hands that change to hanadocs to commit (the vault is not this repo); never commit there.
- **Registrations and single previews.** Keep `inventory.unregistered_showrunners()` beside `listing()` for the unregistered-showrunner check (the listing has no registration whose target is absent); a test covers a registered production whose target id is absent, with no full inventory. `accounts.py move <session> <label> --dry-run` previews one ungrouped session (today `accounts.py` ~2873 rejects it), writing nothing; a no-write test pins it.

**Files:**
- `scripts/accounts/accounts.py` — `live_group_members`, `move_sessions`, `move_group`, `move`, `exec_account`, the preview, the target check, the follow.
- `scripts/lib/relaunch.py` — `run` sets `RELAUNCH_SUPERVISED=1` in the child's environment.
- `scripts/lib/test_relaunch.py` — the child sees the marker.
- `scripts/accounts/test_accounts.py`, `scripts/shutdown/test_account_move.py` — the cases below.
- `docs/accounts.md` — the move command.
- `commands/showrunner/accounts.md` — the move command.
- `commands/showrunner/move.md` — the move command.
- `~/rust/hanadocs/agent login.md` — outside this repo; written by the unit director, handed to hanadocs.

**Workers:** 1 writer + 1 tester — one production file.
- `impl` — `scripts/accounts/accounts.py`, `scripts/lib/relaunch.py` and the three docs in this repo.
- `test` — `test_account_move.py`: the preview calls `inventory.listing` and never `inventory.inventory` (assert), prints the per-session plan; the target-out-of-usage refusal writes nothing; the run hands one `down --only` per source login and then follows; the move-back line. `test_accounts.py`: `exec_account` under a tty and unsupervised execs the supervisor, supervised or no tty execs claude. `scripts/lib/test_relaunch.py`: the supervised child's environment carries `RELAUNCH_SUPERVISED=1`.

**Constraints from prior phases:** "Inventory reads only what it needs": `inventory.listing(login | None) -> list[ListedSession]` (`session_id, pid, proc_start, name, kind, config_dir, login, production, doc`), `inventory(login, only=…)` (those ids plus their workers). "A session's state decides its path": `settle.prompt_state(PromptSubject(session_id, login, config_dir), now) -> TakesPrompts | StoppedTakingPrompts`, the `reaping` kind, `trail`, `down --only` inventories once. "A ghostty session restarts in its own window": `scripts/lib/relaunch.py run -- <argv>` (no environment marker yet; this phase adds `RELAUNCH_SUPERVISED`); restart's window launches already run `relaunch.py run -- <resume argv>`, where a move's argv is `accounts.py exec <target> …`; `restart._relaunch_request(parts, note)` builds the request environment from `accounts.prepared_account(target, resume=True)`. "The shutdown says what is happening and where to act": `shutdown.py follow <account> --request <id>` (exit 0 done, 1 needs the user, 130 detached), the place phrases. Two-accounts' "Accounts named by number only" spells labels `claude1`/`codex1` and adds `claude_account_dir`; use its functions.

**Acceptance gate:** the accounts, shutdown and `scripts/lib` suites pass, also with the real HOME; `basedpyright` 0/0/0 on the changed files. The unit director measures `accounts.py move showrunners <current account> --dry-run` on natedev under 0.5 s wall clock (`time`), and checks its output names no session wrongly.

### Phase 6 — Live check of the move  · status: todo

**Depends on:** Phase 5

#### Work Order

**Goal:** on natedev, throwaway sessions prove each path end to end: a pause-then-move, a reap of a session on a spent account, and a ghostty session restarted in its own window with its scroll history.

**Spec:**

1. Unit director only, after install (`~/.claude` main carries Phases 1–5). Throwaway sessions only, moved with `accounts.py move <session> <target>` (the ungrouped move); never a real group, never a real session's messages.
2. Start three throwaway Haiku sessions: `pm-move-tmux` in a tmux window, `pm-move-ghostty` in a ghostty window started through the `claude<N>` alias of an account with room, and `pm-move-spent` through the alias of an account whose fresh reading shows a window at 0 (on 2026-10-14 two accounts were at weekly 0). Give `pm-move-spent` one prompt so its transcript holds the refusal.
3. Run the preview for each move; it shows `pause, then move` for the first two and `reap and restart now` for the third, in under 0.5 s.
4. Move all three; the terminal follows each step live and ends with the summary. Check: `pm-move-spent` got no pause message and was back on the target within 30 s; the other two replied ready and came back; `pm-move-ghostty` is the same ghostty window (same window shell pid; its earlier output still in scroll history: the unit director reads it with the user or by a screenshot of that window); the history records carry the trail.
5. Kill nothing by pattern; end the three throwaways with `shutdown.py down --only <ids> --here --no-alert` and close what is left.
6. Record the results in `docs/as-built/shutdown-and-restart.md` with the times measured.

**Files:**
- `docs/as-built/shutdown-and-restart.md` — the live results.

**Workers:** unit director only; no workers open.

**Constraints from prior phases:** the follow, the preview lines, the reap, the supervisor and the target check as built by "The move command" and the phases before it.

**Acceptance gate:** every check in Spec step 4 holds; the timing of step 3 is under 0.5 s.
