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

### Phase 1 — A moved unit director resumes its Codex workers  · status: done

#### As-built

- `codex_mesh.py stop --pause --session-dir <D>` drops queued messages, interrupts live turns and sets every non-ended seat to the followable status `paused` with `paused_from` and `paused_turn`; a late launcher completion cannot overwrite the pause. Plain `stop` (run end) still ends every seat, and `sweep --stop` leaves a paused seat paused. `FOLLOWABLE_STATES` is `("done", "failed", "paused")`; a follow clears the pause fields and resumes the recorded thread on a new server.
- `codex_mesh.py reopen --session-dir <D> --to <seat>` turns an `ended` seat, or a `running`/`starting` seat whose launcher is dead and whose server is gone, back into `paused` on its recorded thread. It refuses (exit 2, one stderr line) when the thread id is missing or a launcher is alive.
- `implement.sh` treats an `implementing` status with no claim file and no live roster launcher like a dead claim: it writes `error` and continues.
- The launcher re-reads the account-move marker after an idle answer, so a turn a move interrupts between the marker read and the idle check resumes on the same thread instead of ending the worker.
- `stop.py` pauses each run dir with `stop --pause` and calls no `end`. Before signalling it records `SessionShutdown.codex_seats: list[PausedSeat]` (`run_dir`, `seat`, `role`, `thread_id`, `turn_live`, `launcher_command`, the nearest live `implement.sh`/`review.sh` ancestor's argv, empty when unreadable; old records read `[]`) and writes `<run_dir>/<seat>.resume-prompt.md` for every seat: continue the earlier task when working, reply ready and wait when idle.
- After any confirmed session with recorded seats comes back (unit director or standalone run), `restart.py` sends one `send.py --to session:<id> --from shutdown --key resume-workers-<session id>` message: `Resume your Codex workers; do not open new ones.`, one line per seat with a complete paste-and-run command, then `Each seat continues its own thread; after resuming, send later work with --to as usual.` A working seat with a complete recorded argv keeps its task arguments with the resume prompt swapped in and `--to` set; any other seat gets a full `implement.sh --to` (or `review.sh --to` for a review role) built from its role. A failed send logs one line and never fails the restart. `account_move_note` adds `Your Codex workers are paused, not ended; a message lists the command that resumes each.` when seats are recorded.
- `shutdown.py restart` skips a local session whose unchanged not-back failure was already pushed, and marks the ones it pushes.
- Live check: a probe session holding a real Codex seat moved between accounts; the seat paused, the resume message arrived, and its command resumed the same thread.

**Files:**
- `scripts/agents/codex_mesh.py` — `paused` status, `stop --pause`, `reopen`, account-move marker re-read
- `scripts/delegate/implement.sh` — stale `implementing` claim cleared
- `scripts/shutdown/stop.py` — pause per run dir, seat record, resume prompts
- `scripts/shutdown/inventory.py` — seat and launcher argv collection
- `scripts/shutdown/record.py` — `PausedSeat`, `SessionShutdown.codex_seats`
- `scripts/shutdown/restart.py` — resume-workers message, `account_move_note` sentence, no repeat push
- `scripts/shutdown/settle.py` — not-back push text
- `commands/unit/direct.md` — <PauseForMove/>: run the shutdown message's resume commands before any dispatch; never open a new seat for a role a paused seat holds
- `docs/as-built/shutdown-and-restart.md` — stopping, restart and resume message
