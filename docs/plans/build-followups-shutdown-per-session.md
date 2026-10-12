# Shutdown per session: each session settles, stops and returns on its own

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `/shutdown` keeps one record per session. Each session stops as soon as it is ready and, when a restart was asked for, comes back at once; restarts launch in parallel and are confirmed by the session registry; one session can be taken out, forced or restarted while others are still settling.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md`

> **Production: build-followups** — unit `shutdown-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-12

The user, 2026-10-12: "You need to rewrite the shutdown tool to deal with each session individually" and, on that morning's claude 3 restart, "this restart needs to happen faster - waiting a minute for each is ridiculous".

The showrunner's account of that morning, 2026-10-12 (claude 3, 20 sessions, `down --only … --restart`): it waited over an hour on one session (demo) that was showing a permission form, and the tool had no way to take one session out once it had started; model-study never stopped (a background shell) and so missed the restart; the restart then launched sessions one at a time, about a minute each; and add_unit refused a single session's restore while the account record was `restarting`. Wanted: each session stops as soon as it is ready and, when a restart was asked for, comes back on its own at once, so one blocked or busy session holds only itself; restarts launch in parallel; a single session can be restarted while others are still settling.

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. This plan rewrites `scripts/shutdown/` around one record per session. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Stack:** Python 3 standard library (`fcntl`, `json`, `subprocess`, `concurrent.futures`, `argparse`, `unittest`), checked by basedpyright; `TypedDict` wire types tagged by `kind`; frozen dataclasses for in-process results.
- **Layout:** `scripts/shutdown/` (`record.py`, `settle.py`, `stop.py`, `restart.py`, `shutdown.py`, `launch_permission.py`, `remote.py`, `inventory.py`, tests beside them) · `scripts/production/add_unit.py` (owner enh-showrunner-unit) · `scripts/message/sessions.py` · `commands/shutdown.md` · `docs/as-built/shutdown-and-restart.md`. State on disk: `SHUTDOWN_STATE_DIR` (default `~/.local/state/shutdown`) `/<login case-folded>/`.
- **Key files:**
  - `scripts/shutdown/record.py` — wire types 21-294 (`ShutdownSessionEntry` 258, `ShutdownRecord` 282, `SessionProgress` 242), validators `_entry` 618 and `_record` 650, locks `_account_lock` 371 and `stop_lock` 385, the slot store 821-964 (`find_live`, `create`, `update`, `archive`, `park`, `unpark`, `live_records`), `_write_record` 741 (temp file, `fsync`, `os.replace`).
  - `scripts/shutdown/settle.py` — `alert_user` 352, `begin` 477, `_blocker` 570, `_counts_ready` 591, `_refresh_entries` 604, `_settle_text` 673, `_send_settle_message` 704, `refresh` 770, `ready` 810, `_message_due` 869, `_showing_form` 982, `_holdout_line` 1001, `_send_holdout_alert` 1036, `conduct_cycle` 1078, `_stop_machines` 1193, `report_stop` 1343, `retry_stop` 1428, `conduct` 1452, `_restore_record` 1525, `cancel` 1591, `now` 1641, `_launch_conductor` 1748, `down` 1788, `status_record_lines` 1897; timings 59-62.
  - `scripts/shutdown/stop.py` — claims 175-235, `_enter_stopping` 238, `close_failed_stop` 261, `_fresh_session` 299, `_wait_for_exit` 371, `_stop_owner_resources` 531, `_retry_owner_cleanup` 572, `_stop_owner` 637 (the signals), `_stop_seats` 838, `_finish` 912, `stop` 951, `parse_stop_report` 1010.
  - `scripts/shutdown/restart.py` — `SESSION_WAIT_SECONDS` 75, `_live_session_ids` 285, `_session_liveness` 289, `wait_for_session` 320, `restart_note` 338, `_resume_command` 364, `_window_launch` 511, `_launch_session` 689, `_restore_timers` 791, `_entry_order` 893, `_claim` 907, `_launch_failure_left_session_live` 946, `_process_session_entry` 998, `_process_seats` 1096, `_finish_record` 1159, `_restart_record` 1323, `_up` 1388, `restart` 1529.
  - `scripts/shutdown/shutdown.py` — `_restore_launch_allowed` 243, `launch_blocked` 268, `held_session_ids` 405, `main` 425 (hand-written `metavar` 430; the `conduct` dispatcher 607-640).
  - `scripts/shutdown/launch_permission.py` — `ShutdownUnitRestoreLaunchPurpose` 23, `launch_barrier` 82, `launch_permission` 106. `scripts/shutdown/remote.py` — `run_remote` 30 (trailing `rc=<n>`).
  - `scripts/shutdown/inventory.py` — unchanged by this plan (user decision): `inventory(login, only)`, `Session`, `parse_session`.
  - `scripts/production/add_unit.py` — `UnitRestoreLaunch` 64, `launch_session` 684, `wait_for_remote_control` 753-770 (polls `tmux capture-pane` for `/remote-control is active` until `--timeout`, default 90.0 at 792), `main` 783 (the wait at 899).
  - `scripts/message/sessions.py` — `running_session` 106 (a record whose socket exists is accepted without comparing `procStart`), `live_session` 127, `live_sessions` 154.
  - `scripts/shutdown/test_support.py` — `ShutdownTestCase` 51 stubs every real send leaf; `scripts/shutdown/test_settle.py` 283 — the fake sender reads stdin on every call; `scripts/message/send.py` 978-980 reads it only for a message body.
  - Consumers of the verbs: `scripts/delegate/remove_seats.py` 125 (`held-sessions`), `commands/showrunner/produce.md` 237 (`launch-blocked --restart-of`), `scripts/accounts/accounts.py` 1261 (`down <label> --restart`), `scripts/nightly_review/nightly_review.py` and `scripts/fix/fix-trigger.sh` (through `launch_permission`), `scripts/hooks/conversation_pause.py` (`release`).
- **Test lanes:** `scripts/shutdown/` → `test_record.py`, `test_settle.py`, `test_stop.py`, `test_restart.py`, `test_shutdown_cli.py`, `test_shutdown_status.py`, `test_remote.py`, `test_send_guard.py` · `scripts/production/` → `test_add_unit.py`. Tests sit beside the code and import their sibling by bare name.
- **Build:** none; the code is interpreted.
- **Test:** from the worktree root, `HOME=<scratch home> python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` (309 pass before Phase 1) and `cd scripts/production && python3 -m unittest test_add_unit` (78 pass before Phase 1).
- **Lint:** `basedpyright <changed files>`: 0 errors, 0 warnings. Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Measured (the 2026-10-12 claude 3 restart, from its live record):** 19 sessions came back between 07:16:18 and 07:28:58 PDT. Nine took 90–92 s each; the other ten took 2–6 s. The 90 s is `add_unit.py`'s `--timeout` default spent in `wait_for_remote_control`; on those nine the text `/remote-control is active` never appeared, `add_unit` exited 1, and restart (since commit 8c91743) found the session live and counted it restarted. `up` handled entries one after another. So 12 m 40 s were spent where about 30 s was needed.
- **Design (the user's decisions, 2026-10-12; binds every phase):**
  - One record per top-level session (unit director, showrunner, plain session). Its seats, Codex app-servers and unit tmux session belong to it and travel with it. No account record, no slot, no parking, no record-level state machine, no both-machines stop claim.
  - Each session moves on its own: settle message → ready → stopped → (restart asked) relaunched. Sessions proceed concurrently; a blocked or busy one holds only itself.
  - Each machine conducts its own sessions. Nothing waits on the other machine.
  - A showrunner does not wait for its units, in either direction. The requester goes last on its machine.
  - Only an account-wide shutdown holds the account against new launches and scheduled work. A selected shutdown holds only its sessions. A restore of one session is permitted by that session's own record.
  - Kept as they are (user decision): `inventory.py`, the stop signals and owner cleanup, launch-by-host commands, restart notes, timer and footer restore, account choice on resume, the settle message wording, the `ready --where` contract.
- **Invariants:**
  - Account isolation: a command for account A never messages, stops or changes a timer of a session whose account is not A. Unreadable means "not A".
  - Never stop a busy session unless forced. Never type into a pane. Stopping is `SIGTERM` while idle; never `SIGKILL`.
  - A relaunch is confirmed by `sessions.live_sessions()`, never by screen text.
  - Only `AllowedByShutdownState` permits a launch; unreadable state refuses. The launch barrier spans check to spawn.
  - `settle.alert_user` is the only path to the phone. The UPS alert is never refused. No code or test reads or prints `~/.config/pushover/env`.
  - Never trust ssh's exit status; every remote command ends `rc=<n>`.
  - Every record timestamp is `settle.record_time()`; every user-facing time is America/Los_Angeles.
  - Tests use temp state and fakes only: no real session, sender, tmux server or Pushover. Every `TestCase` derives from `ShutdownTestCase`.
  - Python: basedpyright 0 errors and 0 warnings on changed files; no file-level ignores; no `Any` (a `TypedDict` per JSON format); no implicit string concatenation.
  - Never edit `scripts/agents/codex_mesh.py`, the seat launcher or `/etc/nixos` (user rule).
  - Never run `shutdown.py`, `restart.py`, `settle.py`, `stop.py` or `add_unit.py` against real state, and never message or launch a real session, outside Phase 5's live check.

## Phases

### Phase 1 — Restarts launch in parallel and are confirmed by the session registry  · status: todo

#### Work Order

**Goal:** `up` brings one machine's sessions back together in about the time of the slowest one, and a unit restore no longer waits for screen text.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

`scripts/shutdown/restart.py`:
- Split `_process_session_entry` into two functions that take one record and one entry, so Phase 3 can call them for a single session:
  - `launch_entry(record, entry, restarted_at, desktop_state, *, dry_run) -> EntryLaunched | EntrySettled`. Everything the function does today up to and including `_launch_session` and the failed-launch check by `_session_liveness`. `EntryLaunched(relaunching_pending_timers: bool)` means a process was started (or found live after its launcher failed) and the registry must confirm it. `EntrySettled` means nothing is left to confirm (already complete, live and noted, manual, failed, liveness unknown).
  - `confirm_entry(record, entry, restarted_at, live: bool) -> None`. Today's steps after the wait: not live → `restart failed{session not live after <N> seconds}`; a showrunner gets its note; timers and footers; `restarted` or `timers pending`.
- `_restart_record` runs three steps. (1) Launch every non-seat entry but the requester: `ghostty`, `zed` and `terminal` hosts one after another on the calling thread (desktop switching needs the 1.2 s spacing), every other host through `concurrent.futures.ThreadPoolExecutor(max_workers=RESTART_LAUNCH_WORKERS)` with `RESTART_LAUNCH_WORKERS = 6`. (2) One shared wait, `wait_for_sessions(session_ids, timeout) -> frozenset[str]`: poll `_live_session_ids()` every `SESSION_POLL_SECONDS` until every launched id is live or `SESSION_WAIT_SECONDS` passed, and return the live ones; then `confirm_entry` for each. (3) The requester's entry, launched and confirmed alone. `_process_seats` and `_finish_record` follow unchanged.
- Drop the order "each unit is live before any showrunner launches" (`_entry_order` keeps only requester-last and a stable name order).
- An exception from one entry's launch marks that entry `restart failed` with `_error_reason` and never stops the others; the record still ends `restart partial` through `_finish_record`.
- `--dry-run` stays serial and prints the same lines as today.
- Result lines add `<machine>: restarted <n> in <s> s`.

`scripts/production/add_unit.py` (also touches; owner enh-showrunner-unit):
- With a `UnitRestoreLaunch`, `main` does not call `wait_for_remote_control`. After `launch_session` returns (or the marked unit was found already running) it prints `<unit> restore launched: tmux attach -t <tmux session>` and returns 0. New-work launches keep the wait and `--timeout`.

`scripts/message/sessions.py`:
- `running_session`: when the record carries a non-empty `procStart`, it must equal `process_start(pid)` whether or not the socket exists. A record without `procStart` keeps today's socket rule. This is on the path: the registry is now the only confirmation of a relaunch, and a leftover socket beside a reused pid would confirm a session that is not back.

Diagnosis, read-only, before the code (the result goes in this phase's As-built):
- Read `~/.local/state/shutdown/<claude 3 login>/history/` for the 2026-10-12 record; the nine units are the entries whose `restarted.at` follows the previous one by 90 s or more.
- For each that is still live, read its pane with `tmux capture-pane -p -S - -t <pane>` (the whole history, not only the visible screen) and its registry record `~/.claude/sessions/<pid>.json`. Answer two questions: was the text printed and then scrolled out of the visible screen before a poll saw it, and was remote control active on the session.
- Act on the answer. Text present in history, remote control active: nothing more; the As-built says so. Remote control not active on any of them: `confirm_entry` reads whichever registry field proves it, prints `<machine>: <name> is back without remote control`, and adds that line to the not-fully-back alert; it never types into the pane. No field proves it: say so in the As-built and report it to the showrunner as an open finding.

Tests: `test_restart.py` — six tmux-host entries with a fake launcher that sleeps 0.2 s finish in under 1 s; window hosts launch in order with the delay; one launcher that raises leaves the others `restarted`; a session that never appears is `restart failed` and the rest are confirmed; the requester launches after the others are confirmed; a showrunner launches without any unit live. `test_add_unit.py` — a restore returns 0 without calling `wait_for_remote_control`; a new-work launch still calls it. A `sessions.py` test beside the roster tests in `scripts/message/` — a record with a socket and a wrong `procStart` is not running.

**Files:**
- `scripts/shutdown/restart.py` — `launch_entry`, `confirm_entry`, `wait_for_sessions`, the three-step `_restart_record`.
- `scripts/shutdown/test_restart.py` — the cases above; update cases that assumed serial order.
- `scripts/production/add_unit.py` — also touches (owner enh-showrunner-unit): no remote-control wait on a restore.
- `scripts/production/test_add_unit.py` — the two cases above.
- `scripts/message/sessions.py` — `running_session` proves the process whenever `procStart` is recorded.
- `scripts/message/test_sessions.py` — the proof case (add to the existing test file for `sessions.py` if it has another name).

**Seats:** 1 writer + 1 tester — the code is one flow in `restart.py`; the tests come from the Spec.
- `impl` — `scripts/shutdown/restart.py`, `scripts/production/add_unit.py`, `scripts/message/sessions.py`; runs the diagnosis.
- `test` — `scripts/shutdown/test_restart.py`, `scripts/production/test_add_unit.py`, `scripts/message/test_sessions.py`.

**Constraints from prior phases:** none.

**Acceptance gate:** both Test commands green; basedpyright clean on the changed files; the named tests pass; `shutdown.py restart --dry-run` output for a fixture record is unchanged but for the new timing line; the diagnosis answer is written down.

### Phase 2 — One record per session: the store, the read verbs and the launch check  · status: todo

#### Work Order

**Goal:** the per-session store exists, old-format records are read through it, and `status`, `records`, `launch-blocked` and `held-sessions` answer per session — so a unit restore is permitted by its own record whatever the rest of the account is doing.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. This phase adds the store and moves the readers. The writers (`begin`, `conduct`, `stop`, `up`, `cancel`, `now`) keep the old slot store until Phase 3 (the plan's own packaging: readers and writers cannot both move in one delegate-sized phase).

`scripts/shutdown/record.py` — new types and API beside the old ones:
- `SessionShutdown{login, label, machine, requested_at, requested_by: RequestOrigin, hold: "account" | "session", then: "stop" | "restart", force: StopTiming, alerts: "user" | "none", session: Session, seats: list[Session], timers: list[TimerRestore], settle_message, where, progress: SessionProgress, stop_issues: list[SessionStopIssue], alerted_at: NotRequired[str]}`. `session` is never a seat. `seats` are the sessions `inventory` attributes to this owner; a seat has no progress of its own.
- `SessionProgress` gains `restarting{at}` (a relaunch is underway; it is the per-session claim). `passive seat ready`, `seat available on demand` and the issue `seat still live` are not written by new code; the validators keep reading them until Phase 3 deletes them.
- `AccountHold{login, label, machine, requested_at, requested_by, then}` at `<account dir>/hold.json`: written only by an account-wide shutdown, so a machine with no session of the account is still held.
- Files: `<account dir>/sessions/<session id with every character outside [A-Za-z0-9_.-] replaced by ->.json`; finished records move to `history/<requested_at>-<same name>.json`.
- API, all under the existing account lock, writes through `_write_record`'s temp-file pattern: `session_records(login) -> list[SessionShutdown]` (oldest `requested_at` first, then name); `all_session_records() -> list[SessionShutdown]`; `find_session(login, session_id) -> SessionShutdown | None`; `create_session(record) -> bool` (holds the launch barrier; `False` and nothing written when that session already has a record); `update_session(login, session_id, change) -> SessionShutdown` (`NoLiveRecord` when absent; `ValueError` when `change` alters `login`, `machine` or the session id); `archive_session(login, session_id)`; `account_hold(login) -> AccountHold | None`; `set_account_hold(hold)`; `clear_account_hold(login)`; `parse_session_records(text)`.
- Old-format records: `session_records` and `all_session_records` also return, converted in memory, every entry of an old `record.json` and `partial/*.json` whose session has no new-format file: one `SessionShutdown` per non-seat entry (`hold` from the scope, `then` from `continuation`, `alerts: "user"`, seats attached to their owner; an old record in `restarting` gives its unfinished entries `stopped`), and `account_hold` answers from an old account-wide record in a held state. An old record that does not validate raises `InvalidRecord` naming the file, as today. Phase 3 turns this same conversion into the on-disk migration.
- Read failures keep today's rule: absent is "none"; a non-directory account path, a non-regular file or a lock that cannot be taken raises `InvalidRecord`.

`scripts/shutdown/shutdown.py`:
- `launch-blocked [account]`: exit 0 with `<label> is held by a shutdown (since <%Y-%m-%d %H:%M %Z>)` when `account_hold` is set; exit 1 silent otherwise; exit 3 on any unreadable state. A selected shutdown never holds the account.
- `launch-blocked --restart-of ID`: exit 1 (allowed) when `find_session(login, ID)` is a record whose progress is `stopped`, `already gone`, `process identity lost`, `restarting`, `restart failed` or `manual restart`; this wins over an account hold. No record for that id: the account rule above. Any session kind, not only units. Delete `_restore_launch_allowed`.
- `held-sessions`: every record's session id and each of its seats' ids, sorted.
- `records [--json] [--here]`: one line or object per session record: `<machine> <label> <name>: <progress> since <time>`.
- `status`: after the inventory, one line per session record, `  <name>: <progress>[ — <detail>][ · message sent|queued: <reason>]`, issues beneath, in place of the per-account block. `settle.status_record_lines` takes a `SessionShutdown`.

Consumers: `remove_seats.py`, `produce.md`'s manual resume, `nightly_review.py` and `fix-trigger.sh` call the same verbs with the same exit codes; change them only if a test shows a difference. `ShutdownUnitRestoreLaunchPurpose` keeps its name and field (user decision 9 keeps launch commands as they are).

Tests: `test_record.py` — create, find, update, archive, the hold file, a second create for the same session returns `False`, unsafe ids, unreadable paths, and conversion of old `settling`, `down`, `restarting` and parked records. `test_shutdown_cli.py` and `test_shutdown_status.py` — `launch-blocked` under an account hold, under a selected shutdown (exit 1), `--restart-of` for a stopped session while another is still `waiting` (exit 1), `held-sessions` with seats, `status` and `records` output.

**Files:**
- `scripts/shutdown/record.py` — `SessionShutdown`, `AccountHold`, the per-session API, old-format conversion.
- `scripts/shutdown/shutdown.py` — `launch-blocked`, `held-sessions`, `records`, `status` read the per-session store.
- `scripts/shutdown/settle.py` — `status_record_lines` for one session record.
- `scripts/shutdown/restart.py` — only where it reads records to pick an account or an alert scope (`_account_from_records`, `_local_restart_scope`).
- `scripts/shutdown/test_record.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_shutdown_status.py` — the cases above.

**Seats:** 2 writers — the store, and the verbs that read it.
- `impl` — `scripts/shutdown/record.py`, `scripts/shutdown/test_record.py`; hub: `scripts/shutdown/record.py` (every other file imports its types).
- `test` — opens as impl: `scripts/shutdown/shutdown.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_shutdown_status.py`, written against the API signatures above.

**Constraints from prior phases:** `restart.py` now has `launch_entry`, `confirm_entry` and `wait_for_sessions`; `add_unit.py` returns at once on a restore; leave them as they are.

**Acceptance gate:** both Test commands green; basedpyright clean; with an old `restarting` record in a temp state dir, `launch-blocked --restart-of <stopped unit id>` exits 1 with no output and `launch-blocked` exits 0; with only a selected shutdown, `launch-blocked` exits 1.

### Phase 3 — Each session stops when ready and returns at once; one conductor per machine  · status: todo

#### Work Order

**Goal:** `/shutdown [--restart]` moves every session through its own record on its own machine, and a blocked session delays no other.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. The writers move to the per-session store and the account record is deleted. The halves below cannot land apart: `settle.py` drives `stop.py` and `restart.py` through the store they share.

`scripts/shutdown/settle.py`:
- `begin(login, requested_by, only, *, then, alerts) -> list[SessionShutdown]`: first `record.migrate(login)` (see below); inventory under `only`; for each non-seat session without a record, `create_session` with `waiting`, its seats, then record and stop its timers as `begin` does today (a timer stop failure takes that one session out again and names it). A session that already has a record is left alone and listed as `already in progress`. With no `only`: `hold: "account"` and `set_account_hold`. A seat whose owner is not in the request gets no record (seats hold no process and are never signalled); `status` still lists it from inventory.
- `down(account, here, only, *, then, alerts)`: the `github-warm-status` check stays. It runs `begin` here and starts this machine's conductor; unless `--here`, it runs the one remote verb `down <login> --here --requested-by <id> [--only …] [--restart] [--no-alert]` on the peer, which does the same there. A peer that is unreachable or refuses is printed (`<machine>: not reached; run /shutdown there`) and never undoes or delays the local shutdown (decision 3; this replaces "nothing was shut down"). Exit 0 when this machine started.
- The conductor: `conduct(login)` holds a non-blocking `flock` on `<account dir>/conduct.lock`; a second one exits 0, since the running one sees new records on its next pass. Every `SETTLE_INTERVAL_SECONDS` it takes one inventory and calls `next_step(record, session: Session | None, others: list[SessionShutdown], now) -> Step` for each record, where `Step` is `SendSettleMessage | StopSession | RelaunchSession | Hold(reason: str) | Finished`:
  - `waiting`, settle message due, not the requester → `SendSettleMessage` (wording and key unchanged; a showrunner is messaged at once).
  - `ready` with `_blocker` clear, every seat idle and the session idle, or `force == "now"` → `StopSession`. Session gone from the inventory → `StopSession` too (the stop records `already gone` and cleans up).
  - `stopped`, `already gone` or `process identity lost` with `then == "restart"` → `RelaunchSession`.
  - The requester's record gets `Hold("requester goes last")` until every other record of the same `requested_at` on this machine is finished, failed or archived.
  - Anything else → `Hold(reason)` with the reason from `_holdout_line`'s details (`busy`, `showing a form`, `ahead N on <branch>`, `merge in progress`, `Codex seat <name> is still running`, `message queued: <reason>`). `hold_reason(record, session) -> str` is the one function `status` and the alert both print.
  - `StopSession` and `RelaunchSession` run in a `ThreadPoolExecutor(max_workers=RESTART_LAUNCH_WORKERS)`, one future per record, so a 20 s stop wait or a launch never delays another session; a record with a running future is skipped on later passes.
  - The conductor ends when no record on this machine has a step left: each is archived, or `stopped` with `then == "stop"`, or needs the user (`stop failed`, `restart failed`, `manual restart`, `timers pending`). When the last record of an account-wide request is archived it calls `clear_account_hold`.
  - On ending it sends the requester one result (`report_stop`'s text, key `shutdown-result-<login>`), built from this machine's records plus one read of the peer's `records --json --here`; a peer still in progress or unreachable is listed as it stands. An account-wide stop that left any session `stop failed` alerts the user as today.
- `ready --where`: unchanged contract; it finds this session's own record with `find_session` and sets `where` and `ready` when the progress is `waiting` or `ready`.
- `cancel` and `now` (account-wide forms, per machine then the peer with `--here`): `cancel` takes out every record still `waiting` or `ready` — timers and footers back, queued shutdown messages retired, the "cancelled by the user: continue where you were" message to each messaged session, `archive_session` — and clears the hold; records already stopped are left for restart and named. `now` sets `force: "now"` on every `waiting` or `ready` record.
- `alert_user(summary, text, action, *, machine="")`: the scope argument and `notify_selected` go; callers skip the call when the record's `alerts` is `"none"`.
- Delete: `refresh`, `conduct_cycle`, `_message_due`, `_stop_machines`, the remote claim, stop and close helpers, `retry_stop`, `_parkable`, `report_restart_skipped`.

`scripts/shutdown/stop.py`:
- `stop_session(record: SessionShutdown, *, kill, is_alive, session_record_exists, fresh_inventory, clock, codex_mesh, tmux, sleep) -> SessionShutdown`: today's `_stop_owner` for one record (identity check, readiness unless forced, `SIGTERM` twice with `STOP_WAIT`, owner resources, issues), ending `stopped`, `already gone`, `process identity lost` or `stop failed`. A retry on a `stop failed` or already stopped record runs `_retry_owner_cleanup`.
- Delete the claims, `_enter_stopping`, `close_failed_stop`, `_stop_seats`, `_finish`, `stop`, `StopReport`, `parse_stop_report` and `record.stop_lock`.

`scripts/shutdown/restart.py`:
- `relaunch_session(record: SessionShutdown, *, dry_run) -> SessionShutdown`: claim by `update_session` setting `restarting{at}` (a record already `restarting` for under 120 s is left to its owner); then Phase 1's `launch_entry`, `wait_for_sessions` for the one id, `confirm_entry`; `restarted` → `archive_session`.
- `up(login, dry_run)`: every record on this machine that is stopped, `restart failed`, `timers pending` or a stale `restarting`: window hosts in order, the rest through the pool, the requester last. `restart(account, …)` runs `up` here and on the peer as today and keeps its alert; `MachineRestartResult` stays.
- Delete `_claim`, `_up_slot`, `_free_slot`, `_combined`, `_process_seats` and every use of `park`, `unpark`, `parked`.

`scripts/shutdown/record.py`:
- `migrate(login) -> list[str]`: under the account lock, writes each old-format entry as a session record (Phase 2's conversion), moves `record.json` and `partial/*.json` to `history/`, and returns one line per file (`shutdown: converted the <state> record of <label> from <time>`), which the caller prints. An old file that does not validate is refused: `InvalidRecord` naming it, nothing moved. Called by `begin`, `conduct`, `up`, `cancel` and `now`.
- Delete `ShutdownRecord`, `ShutdownScope`, `Conductor`, the orchestration issues, the slot API, `park`, `unpark`, `parked`, `stop_lock`, the dropped progress kinds and the in-memory conversion's callers.

`scripts/shutdown/shutdown.py`: remove the verbs `begin`, `refresh`, `retry-stop`, `stop`, `claim-stop`, `close-failed-stop`; `down` gains hidden `--requested-by` and `--no-alert`; `conduct <login>` only calls `settle.conduct`.

`scripts/shutdown/test_settle.py`: the fake sender (line 283) reads stdin only for the calls where `send.py` reads it (a message send, not `retire` or `pending`; `scripts/message/send.py` 978-980), so a test fails when the real sender would block on an open stdin.

Tests: `next_step` table cases for every row above; a session showing a form stays `Hold` while two others stop and relaunch in the same pass; a showrunner stops while its unit is `waiting`; the requester stops only after the others; a second `down --only` while one is in progress adds only the new session; an unreachable peer does not fail `down`; `stop_session` keeps every existing signal and cleanup case; `migrate` on each old state; `cancel` leaves stopped records.

**Files:**
- `scripts/shutdown/settle.py` — per-session `begin`, `down`, `conduct`, `next_step`, `hold_reason`, `ready`, `cancel`, `now`, `alert_user`.
- `scripts/shutdown/shutdown.py` — verbs removed and added as above.
- `scripts/shutdown/record.py` — `migrate`; the account record and slot deleted.
- `scripts/shutdown/stop.py` — `stop_session`.
- `scripts/shutdown/restart.py` — `relaunch_session`, per-session `up`.
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_record.py` — conductor, verbs, migration, the stdin rule.
- `scripts/shutdown/test_stop.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_send_guard.py`, `scripts/shutdown/test_support.py` — per-session stop and relaunch; leaf names that moved.
- `scripts/accounts/accounts.py` — only if `swap unit`'s `down <label> --restart` call or its retry text names a removed behavior.

**Seats:** 2 writers — the conductor and verbs, and the two per-session actions it calls. Not split into two phases: neither half passes its tests on the old store once the other has moved.
- `impl` — `scripts/shutdown/settle.py`, `scripts/shutdown/shutdown.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_record.py`, `scripts/accounts/accounts.py`; hub: `scripts/shutdown/record.py` (both writers import its types; the other writer asks for deletions it needs).
- `test` — opens as impl: `scripts/shutdown/stop.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/test_stop.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_send_guard.py`, `scripts/shutdown/test_support.py`.

**Constraints from prior phases:** Phase 1 built `launch_entry`, `confirm_entry`, `wait_for_sessions`, `RESTART_LAUNCH_WORKERS`. Phase 2 built `SessionShutdown`, `AccountHold`, the per-session API, the in-memory conversion, and per-session `launch-blocked`, `held-sessions`, `records`, `status`; their outputs do not change here.

**Acceptance gate:** both Test commands green; basedpyright clean; `grep -n 'park\|claim_stop\|stop_lock\|find_live' scripts/shutdown/*.py` finds nothing; the form-blocked, showrunner-independence, requester-last and second-request tests pass; `shutdown.py --help` lists no removed verb.

### Phase 4 — Acting on one session: take out, force, restart; what holds each one  · status: todo

#### Work Order

**Goal:** the user can take one session out, force one, or restart one at any time, and `status` says what holds each session.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- `--only` becomes public on `down`, `cancel`, `now` and `restart`: a comma list where each item is a session id or an exact session name, resolved against the account's records and inventory on both machines by `resolve_sessions(login, items) -> list[str]`; an item matching nothing or more than one session exits 2 with the candidates listed. Without `--only` each verb keeps its account-wide form.
- `cancel --only X` (take out): for a `waiting` or `ready` record — timers and footers back, queued shutdown messages retired, the session told to carry on, the record archived. For a record already stopped it prints `<name> is already stopped; /shutdown restart --only <name> brings it back` and exits 1. It never clears an account hold while other records of that request remain.
- `now --only X`: `force: "now"` on that record alone.
- `restart --only X`: `relaunch_session` for that record on its machine (through the peer's `up <login> --only <id>` when it lives there), whatever any other record is doing; `--dry-run` and `--no-alert` apply.
- `down --only X [--restart]` while another request is in progress adds a record for X only (Phase 3's `begin` already does this); this phase adds the test and the printed line `<name>: added`.
- `status`: each session record line reads `  <name>: <progress> — <hold_reason>` while it is `waiting` or `ready`, and each machine's block ends with the actions that apply (`/shutdown cancel --only <name>`, `/shutdown now --only <name>`, `/shutdown restart --only <name>`). A record whose machine has no running conductor says `no conductor running; /shutdown restart or /shutdown cancel`.
- Add `--only` to the hand-written `metavar` help where needed (`argparse.SUPPRESS` on a subparser prints `==SUPPRESS==`).

Tests: each verb with `--only` by id and by name on a three-session fixture; an ambiguous name exits 2; `restart --only` succeeds while another record is `waiting`; `cancel --only` on the last `waiting` record of an account-wide request clears the hold only when no record of it remains; `status` lines for form, busy, unpushed and queued-message holds.

**Files:**
- `scripts/shutdown/shutdown.py` — `--only` on the four verbs, `resolve_sessions`, status lines.
- `scripts/shutdown/settle.py` — `cancel` and `now` for chosen records.
- `scripts/shutdown/restart.py` — `restart` and `up` for chosen records.
- `scripts/shutdown/test_shutdown_cli.py`, `scripts/shutdown/test_shutdown_status.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_restart.py` — the cases above.

**Seats:** 1 writer + 1 tester — the verbs share `resolve_sessions` and one argument parser, so the code does not split; the Spec fixes every output line.
- `impl` — `scripts/shutdown/shutdown.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py`.
- `test` — the four test files.

**Constraints from prior phases:** Phase 3 built `next_step`, `hold_reason`, `stop_session`, `relaunch_session`, per-record `cancel` and `now` internals, the conductor lock `conduct.lock`, and `launch-blocked --restart-of` permitted by the session's own record (Phase 2).

**Acceptance gate:** both Test commands green; basedpyright clean; the named tests pass; `shutdown.py cancel --help`, `now --help` and `restart --help` show `--only`.

### Phase 5 — The not-answering alert, the live check, the skill text and the as-built  · status: todo

#### Work Order

**Goal:** a session that does not answer pages the user by name with the actions available; the tool is proved on both machines; the docs describe what shipped.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- The alert, in the conductor: a record still `waiting`, or `ready` but held, `NOT_ANSWERING_AFTER = timedelta(minutes=5)` after its settle message was sent (or after `requested_at` when the message is still queued), with no `alerted_at` in the last `REPEAT_HOLDOUT_ALERT` (60 min), gets one `settle.alert_user` call and `alerted_at` set. Summary `<name> is not answering the shutdown of <label>`; text: the machine, `hold_reason`, and the two actions `/shutdown cancel --only <name>` (take it out; it carries on) and `/shutdown now --only <name>` (stop it anyway); `ActionRequired` names the same two. Several sessions due in one pass go in one alert, one line each. The alert never changes a step: every other record keeps moving. It is sent for selected shutdowns too and is skipped only when the record's `alerts` is `"none"`. From the Mac it routes `--machine natedev` as today. The requester session gets the same text as a `shutdown` message. Delete `_send_holdout_alert` and `FIRST_HOLDOUT_ALERT`.
- The UPS alert is never refused: no change in this phase may make `alert_user`, `send.py` or the launch check refuse or delay it; add a test that an account hold and a session record in every progress leave an alert send untouched. No code or test reads or prints `~/.config/pushover/env`.
- Live check, written into the as-built as a procedure and run once. It touches no real session without the user. natedev leg, run by the unit director after both seats are idle and never from inside the worktree: a scratch `HOME` and `SHUTDOWN_STATE_DIR`, two scratch Claude sessions in a scratch tmux session, `down --here --only <ids> --restart --no-alert`; one is left showing a permission form. Pass: the other stops and is back within 30 s, the blocked one holds only itself, `cancel --only` returns it, `restart --only` works while the other is `waiting`, `launch-blocked` exits 1 throughout. Mac leg: the Mac has no tmux and its Ghostty launches need the user, so the Work Order hands the user the exact commands through the showrunner and records the result they report; the phase stays open until it is recorded.
- `commands/shutdown.md`: the verbs with `--only`, what `status` prints, the two actions in the alert, the requester-last rule, that a selected shutdown holds only its sessions, and that the peer runs on its own. Load `/succinct_style` before editing it.
- `docs/as-built/shutdown-and-restart.md` (the amend): rewrite The record store, States, Settling, Stopping, Restart, the launch-check section, the invariants and the timings for one record per session; record Phase 1's diagnosis; keep Accounts, Inventory and the launch-by-host text.

**Files:**
- `scripts/shutdown/settle.py` — the not-answering alert.
- `scripts/shutdown/test_settle.py` — alert timing, repeat, grouping, `alerts: "none"`, the UPS test.
- `commands/shutdown.md` — the skill text.
- `docs/as-built/shutdown-and-restart.md` — the amended as-built and the live-check procedure.

**Seats:** 2 writers — code and its tests, and the two documents.
- `impl` — `scripts/shutdown/settle.py`, `scripts/shutdown/test_settle.py`.
- `test` — opens as impl: `commands/shutdown.md`, `docs/as-built/shutdown-and-restart.md`.

**Constraints from prior phases:** Phase 3 built the conductor pass, `hold_reason` and the `alerts` and `alerted_at` fields' uses; Phase 4 built `cancel --only`, `now --only`, `restart --only` and the status lines the alert and the skill text name.

**Acceptance gate:** both Test commands green; basedpyright clean; a test shows a form-blocked session alerted once at 5 min while two others finish; the natedev leg's results and the user's Mac result are written in the as-built; `commands/shutdown.md` names no removed verb or state.
