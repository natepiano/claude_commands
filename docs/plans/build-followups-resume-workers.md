# Moved units resume their Codex workers

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A unit director stopped for an account move or restart comes back with a message, sent through the message infrastructure, that names each Codex worker it held and the exact command that resumes it, and tells it to resume them rather than open new ones.

> **As-built disposition: amend** — `docs/as-built/shutdown-and-restart.md` (Stopping, Restart, live check)

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Project started:** 2026-10-16T23:24:55+00:00
- **Stack:** Python 3.13 standard library, checked by basedpyright. No `Any`, a `TypedDict` per JSON shape, no file-level ignores.
- **Source:** the user, via the showrunner, 2026-10-16: "when we move units to a new account - they need to have an explicit message sent to them when they start up in the new account on how to resume their codex sessions they shut down prior. herdr started fresh sessions for the work it was doing. it said it would make a memory about this but that's not good enough - the move itself should use our messaging infrastructure to make sure that it sends a message about resumeing workers and not starting fresah ones and it should provide the commands that tell the unit directors how to do taht." Showrunner: the restart of every moved unit director sends it, through the message infrastructure, the exact resume command for each Codex worker it held when it stopped, and the instruction to resume those rather than start fresh. Test it on a move.
- **Cause (measured by the unit director):** `stop.py` stops a session's Codex work with `codex_mesh.py end` on each busy worker (`_end_busy_codex_workers`) and `codex_mesh.py stop` on each run dir (`_stop_codex_servers`). `command_stop` calls `_mark_all_roster_ended`, so after the restart every seat reads `ended`. `FOLLOWABLE_STATES` is `("done", "failed")`, so `implement.sh --to <seat>` refuses ("worker is busy or not followable") and the director opens a new seat, losing the thread. `resume-account-move` does not help: it serves a Codex workers-account swap and needs a live server on the recorded port. A follow-up on a followable seat whose server is gone already works: `_follow_refusal` returns "" on `ServerRestartRequired`, and the follow path starts a new app-server and calls `thread/resume` on the recorded thread id.
- **Key files** (find by name; line numbers move):
  - `scripts/agents/codex_mesh.py` — `command_stop` (~3924, `_mark_all_roster_ended`), `command_end` (~3787), `FOLLOWABLE_STATES` (~369), `_follow_refusal`, `_claim_follow`, `_roster_thread`, roster record types (~300-400, `launcher_pid`), `_interruptible_turn_id` / `_interrupt_turn` / `_drop_queued_messages`, parser (~6017-6170).
  - `scripts/delegate/implement.sh`, `scripts/delegate/review.sh` — the launchers; `codex_mesh.py start` (new seat) and `follow` (`--to`) are their children, and the roster's `launcher_pid` is that child.
  - `scripts/shutdown/stop.py` — `_end_busy_codex_workers` (~310), `_stop_codex_servers` (~318), `_run_directories`.
  - `scripts/shutdown/inventory.py` — `_codex_servers` (~882, reads `mesh_roster.json`), `Session.run_dirs` / `codex_servers`.
  - `scripts/shutdown/session_types.py` — `CodexServer` (~183), wire parse (~651).
  - `scripts/shutdown/record.py` — `SessionShutdown` and its wire parse.
  - `scripts/shutdown/restart.py` — `account_move_note` (~757), `resume_command`, `_resume_parts`, the confirm step after a session comes back, `SEND` (~139).
  - `scripts/message/send.py` — `--to session:<id>`, `--from`, `--key`; an undelivered message stays for `send.py pending`.
  - Tests: `scripts/agents/test_codex_mesh*.py`, `scripts/shutdown/test_stop.py`, `test_restart.py`, `test_account_move.py`, `test_inventory.py`, `test_support.py` (`ShutdownTestCase` blocks every send leaf).
- **Test:** from the worktree root, `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <tree> --tests-only`.
- **Lint:** `basedpyright <changed .py files>`; pass is `0 errors, 0 warnings, 0 notes`.
- **Invariants:** tests start no session, no Codex server and send no message or push; a Claude process gets only SIGTERM after its identity check; `end_session.sh` (run end) still ends every seat; a seat another session's run owns is never touched; the user-facing lines name no ids beyond what a command needs.

## Phases

### Phase 1 — A moved unit director resumes its Codex workers  · status: pending

#### Work Order

**Goal:** after any shutdown stop and restart of a unit director (an account move included), every Codex seat it held is followable on the same thread, and the restarted director receives one message naming each seat with the exact command that resumes it and the instruction to resume, never open a new seat.

**Spec:**

*Seats survive the stop:*
1. `codex_mesh.py stop --pause --session-dir <D>`: for each roster entry whose status is not `ended`, drop its queued messages and interrupt its live turn (as `command_end` does, without the end marker), then set its status to a new followable `paused` with `paused_from: <previous status>` and `paused_turn: true|false` (a turn was live). Then stop the server exactly as plain `stop` does. Plain `stop` (run end) is unchanged and still marks every seat `ended`. `sweep --stop` (run from `remove_workers.py` and `prepare_session.sh`) never ends a `paused` seat: only run end's plain `stop` does.
2. `paused` joins `FOLLOWABLE_STATES`; `_claim_follow` and `_follow_refusal` accept it. A follow on a paused seat clears `paused_from` / `paused_turn`.
3. `stop.py` stops a session's Codex work with `stop --pause` on every run dir and no longer calls `end` on busy workers.
4. `codex_mesh.py reopen --session-dir <D> --to <seat>` recovers a seat an earlier move already ended (fps's `tool-based-ui-frame-time-impl`, herdr-trial's seat): when the entry is `ended`, has a `thread_id`, and its `launcher_pid` is absent or dead, remove its end marker and set it `paused` (`paused_from: "ended"`, `paused_turn: false`). Print `reopened <seat>; send its next turn with --to <seat>`. It also takes a seat left `running` or `starting` by a launcher a move killed (launcher dead, no live server on its port), the same way (`paused_from: <that status>`, `paused_turn: true`). Refuse with one stderr line and exit 2 when the thread id is missing or a launcher is alive. A follow on it then resumes the recorded thread on a new server.
5. A launcher killed mid-turn (by a move or stop) leaves no stale refusal. `implement.sh`'s claim step today clears a dead claim pid but refuses `already implementing` when `impl_status_<slot>` reads `implementing` and the claim file is missing (cables hit this after its move). Treat a missing claim exactly like a dead one, unless the roster entry's `launcher_pid` is alive: write `error` to the status file and continue. `review.sh` gets the same rule if it has the same check.
6. Every one of these, and fps's `--to` refusal on an `ended` seat, is covered by one command the restart message can give: after `stop --pause` (or `reopen` for a seat ended before this change), `implement.sh --to <seat>` / `review.sh --to <seat>` follows the seat on its recorded thread with no hand edits.

*The stop records how to resume:*
7. Before pausing, the stop records, per non-ended seat: `run_dir`, `seat`, `role`, `thread_id`, `turn_live: bool`, and for a seat with a live launcher, the launcher's command line (`implement.sh` or `review.sh`, the nearest ancestor of the roster's `launcher_pid` whose argv names one of them). It stores them on the session's `SessionShutdown` as `codex_seats: list[PausedSeat]` (`TypedDict`; old records read `[]`).
8. For a seat with `turn_live`, write `<run_dir>/<seat>.resume-prompt.md`: "You were paused when your unit director stopped for an account move or restart. Continue the task you were on from where you stopped; your earlier instructions still hold." Its resume command is the recorded launcher command with `--to <seat>` added when absent and its prompt-file argument replaced by that file. A seat that was idle, or whose launcher command could not be read, has the resume command `implement.sh --to <seat>` (or `review.sh --to <seat>` for a review role) named as the form, with the director supplying its usual arguments.

*The restart sends it:*
9. After a restarted unit director's session is confirmed back (move or plain restart), `restart.py` sends it one message with `send.py --to session:<id> --from shutdown --key resume-workers-<session id>`. First line: `Resume your Codex workers; do not open new ones.` Then one line per seat: `<seat> (<role>, <was working | idle>): <exact command>`, run from `<working dir>`. Last line: `Each seat continues its own thread; after resuming, send later work with --to as usual.` No seats → no message. A failed send writes one line to the restart's own log and never fails the restart. The message also stays for `send.py pending`, which the opening prompt already tells the director to run first.
10. `account_move_note` adds one sentence when `codex_seats` is non-empty: `Your Codex workers are paused, not ended; a message lists the command that resumes each.`

*Docs:*
11. `docs/as-built/shutdown-and-restart.md`: stopping pauses seats, the record's `codex_seats`, the restart message. `commands/unit/direct.md` <PauseForMove/> (per `commands/succinct_style.md`): on resume after a move, run the commands in the shutdown message before any dispatch; never open a new seat for a role a paused seat holds.

**Files:**
- `scripts/agents/codex_mesh.py`
- `scripts/delegate/implement.sh`
- `scripts/delegate/review.sh`
- `scripts/shutdown/stop.py`
- `scripts/shutdown/inventory.py`
- `scripts/shutdown/record.py`
- `scripts/shutdown/session_types.py`
- `scripts/shutdown/restart.py`
- `docs/as-built/shutdown-and-restart.md`
- `commands/unit/direct.md`
- `scripts/agents/test_codex_mesh.py`
- `scripts/delegate/test_implement_launcher.py`
- `scripts/delegate/test_review_launcher.py`
- `scripts/shutdown/test_stop.py`
- `scripts/shutdown/test_restart.py`
- `scripts/shutdown/test_inventory.py`
- `scripts/shutdown/test_account_move.py`
- `scripts/shutdown/test_support.py`

**Workers:** 1 writer + 1 tester
- `impl` (impl): `scripts/agents/codex_mesh.py`, `scripts/delegate/{implement,review}.sh`, `scripts/shutdown/{stop,inventory,record,session_types,restart}.py`, `docs/as-built/shutdown-and-restart.md`, `commands/unit/direct.md`; hub file `scripts/shutdown/record.py` (the `PausedSeat` type).
- `test` (test): `scripts/agents/test_codex_mesh.py`, `scripts/delegate/test_implement_launcher.py`, `scripts/delegate/test_review_launcher.py`, `scripts/shutdown/test_{stop,restart,inventory,account_move}.py`, `scripts/shutdown/test_support.py`.

**Tests:**
- codex_mesh: `stop --pause` on a roster with `done`, `running` (fake server) and `ended` entries leaves the first two `paused` with the right `paused_from` / `paused_turn`, the third `ended`; plain `stop` still ends all; `can-follow` accepts a paused seat; `reopen` turns an ended seat, and a `running` seat with a dead launcher and no server, each with a thread, into `paused`; `sweep --stop` leaves a paused seat paused; and refuses one with a live launcher or no thread; a follow clears the pause fields.
- launchers: `implement.sh --to` on a slot whose status reads `implementing` with no claim file and a dead roster launcher continues (status becomes `error` first); with a live roster launcher it still refuses; same for `review.sh` if it has the check.
- stop: a stopped session with a busy and an idle seat calls `stop --pause` per run dir and no `end`; its record holds both seats with their launcher command or none.
- restart: a confirmed unit restart with two recorded seats sends exactly one message to `session:<id>` with both command lines (one rebuilt from a recorded `implement.sh` argv with `--to` and the resume prompt swapped in); zero seats sends none; a failed send leaves the restart's outcome unchanged; an old record without `codex_seats` parses.

**Live check (unit director, after install):** start a probe unit session with `accounts.py exec --probe claude2 -n resume-probe` outside any worktree and a scratch run dir holding one real Codex seat started on a two-line task. Move the probe to claude3 with the shutdown tool (`--only resume-probe --here --no-alert`). Confirm the message arrives in the probe with the seat's command, run it, and confirm the seat answers on the same `thread_id`. Also `reopen` one real seat a past move ended (ask fps or herdr-trial which) and confirm its next `--to` turn runs on its old thread. End the seat and the probe afterwards.
