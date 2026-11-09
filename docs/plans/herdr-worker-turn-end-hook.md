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

### Phase 1 — Codex workers end turns through a Stop hook  · status: todo

#### Work Order

**Goal:** A Codex worker's turn end reaches `turn_end.py` through a Codex `Stop` hook on stdin, so no payload size can stop it.

**Spec:**
1. `scripts/hooks/codex_hooks.py`: add one `HOOKS` entry, `CodexHook("turn-end", PythonHookScript("../herdr/turn_end.py"), "Stop", ...)`. List it with the matcher form Codex reports for `Stop`, the same way `_listed_fields` lists `UserPromptSubmit` with a null matcher.
2. `scripts/herdr/worker_panes.py`: `_turn_end_arguments("codex")` returns `[]`. A Codex worker launch line has no `-c notify`. The Claude `--settings` Stop hook stays. Fix the `turn_end_command` docstring.
3. `scripts/herdr/turn_end.py`:
   1. `hook_event` drops the `agent-turn-complete` branch. A legacy notify payload from a pane launched by older code becomes `WorkerEvent("")` and is ignored. The global Stop hook carries that turn.
   2. A `Stop` payload with `last_assistant_message: null` gives `reply=""`, never `"None"`.
   3. `main` gives the payload to the detached `--handle-hook` child on its stdin, not as an argument. The child reads stdin. No payload size reaches argv.
   4. `main` writes nothing to stdout and never exits 2.
4. `scripts/herdr/test_pane_launch.py` parses saved launch lines; its `notify` rows stay, because old panes still carry them.

**Files:**
- `scripts/hooks/codex_hooks.py` — the `turn-end` Stop handler.
- `scripts/herdr/worker_panes.py` — no Codex notify argument.
- `scripts/herdr/turn_end.py` — `hook_event`, `main` stdin hand-off.
- `scripts/hooks/test_codex_hooks.py` — the `turn-end` handler is installed and listed.
- `scripts/herdr/test_turn_end.py` — a Codex Stop payload row with a null message; a legacy notify payload is ignored; `main` hands a payload over 128 KiB to the child through stdin.
- `scripts/herdr/test_worker_panes.py` — a Codex launch line has no `notify`.
- `docs/as-built/long-lived-workers.md` — lines 46 and 116 name `-c notify` and `agent-turn-complete`; the deploy step.
- `docs/as-built/build-followups-fn-length-hook.md` — the handler count and the `turn-end` handler.

**Workers:** `1 writer` — the change is small and every file depends on the one hook transport.
- `impl` — all files above.

**State:** none in the code. The deploy writes `~/.codex/hooks.json` and its trust through the installer.

**Constraints from prior phases:** herdr-worker-panes Phase 34c added `_ignored` (one JSON line per ignored exit) and the hook-reply completion in `turn_end.handle`. Both stay.

**Acceptance gate:** basedpyright 0/0/0 on every changed module and its callers; `test_codex_hooks`, `test_turn_end`, `test_worker_panes` and `test_pane_launch` green; the targeted test green.

**Deploy:** panes-unit runs `codex_hooks.py install` and `check` on natedev and the Mac after the merge reaches main (sdlc, 2026-11-09).

**Gotchas:**
- Every Codex session now runs `turn_end.py` at each turn end. A non-worker turn exits as ignored and adds one line to `~/.local/state/herdr/turn-end-ignored.jsonl`.
- A pane launched before deploy keeps `-c notify`. Its notify payload is ignored, and the Stop hook carries its turn ends.
