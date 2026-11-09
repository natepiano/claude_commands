# Codex workers end turns through a Stop hook

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Approved to build on 2026-11-09 by the sdlc showrunner (go on the Stop hook, and the installer run at deploy by panes-unit).

Written by panes-unit on 2026-11-09 for the sdlc showrunner. It follows herdr-worker-panes Phase 34c (`docs/as-built/long-lived-workers.md`).

## The request

sdlc, 2026-11-09 06:5x: the fastest worker's `DONE` at 06:50:27 EST reached no director. Find where it went and fix it at the cause.

## Cause

Codex legacy notify (`-c notify=[argv]`) passes the whole JSON payload as one program argument. Its `input-messages` field holds every user message in the thread history. Linux refuses one argument over 128 KiB (`E2BIG`). Codex logs `after_agent hook failed; hook_name=legacy_notify error=Argument list too long (os error 7)` and the turn ends with no hook. `turn_end.py` never runs.

Evidence:
- The fastest impl thread `01a1870d-1a99-79d3-9406-5cb6803abc55` held about 288K characters of user messages after its last compaction. Its 11:50:27 UTC turn failed this way.
- `~/.codex1/logs_2.sqlite` showed 31 such failures in one hour, across 5 long-lived worker threads.

## Delegation Context

- **Project:** `~/.claude` scripts. Python 3.11+ through `scripts/lib/py`, `unittest`, basedpyright.
- **Project started:** 2026-11-09T12:10:03.806+00:00
- **Codex:** 0.167.0. Its `Stop` hook gets JSON on stdin with the fields `cwd`, `hook_event_name` (`"Stop"`), `last_assistant_message` (string or null), `model`, `permission_mode`, `session_id` (the thread id), `stop_hook_active`, `transcript_path` (string or null) and `turn_id`. Stdout must be empty or valid JSON. Exit 2 blocks the stop; never exit 2.
- **Hook installer:** `scripts/hooks/codex_hooks.py install|check` writes each `HOOKS` entry into the shared `~/.codex/hooks.json` and trusts it through `codex app-server` (`docs/as-built/build-followups-fn-length-hook.md`).

## Phases

### Phase 1 — Codex workers end turns through a Stop hook  · status: done

#### As-built

A Codex worker's turn end reaches `scripts/herdr/turn_end.py` through the `turn-end` Codex `Stop` handler in `scripts/hooks/codex_hooks.py` (null listed matcher, like `UserPromptSubmit`). Codex worker launch lines carry no `-c notify`; `worker_panes._live_policy` still reuses a live pane launched with the old line. `turn_end.main` hands the payload to the detached `--handle-hook` child on stdin, so no payload reaches argv. A null `last_assistant_message` is an empty reply. A legacy `agent-turn-complete` payload is ignored; the global Stop hook carries that turn.

**Files:**
- `scripts/hooks/codex_hooks.py`, `scripts/herdr/worker_panes.py`, `scripts/herdr/turn_end.py`
- Tests: `scripts/hooks/test_codex_hooks.py`, `scripts/herdr/test_turn_end.py`, `scripts/herdr/test_worker_panes.py`
- Docs: `docs/as-built/long-lived-workers.md` (Stop hook deployment), `docs/as-built/build-followups-fn-length-hook.md`

**Deploy:** panes-unit runs `codex_hooks.py install` and `check` on natedev and the Mac after main has this change (sdlc, 2026-11-09).

**Gotchas:** every Codex session now runs `turn_end.py` at each turn end; a non-worker turn exits as ignored and adds one line to `~/.local/state/herdr/turn-end-ignored.jsonl`.

**Ruled out:** a test row for a child that exits before it reads stdin (rare; the reviewer probed it; the hook timeout bounds it).
