# Screen recording skill

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The `/screen_record` skill on Linux, then `/unit:report` timers that are flagged when missing and never skipped after a rejected report, then Codex seats that survive a refused turn instead of dying on its `systemError` status.

> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via the `startup` session, 2026-10-06 04:2x PDT: "Ask natedev session to make a skill for it that others can also use - screen recording. Make sure we have guardrails built in to not accidentally create recordings that run too long. Make sure we have created a cleanup policy so we don't fill our disk with videos."

First consumer: `startup` (Hana's startup-animation video). Reply to it by SendMessage with the skill's name and usage when merged.

## What already works (startup's findings, natedev, KDE Plasma on Wayland)

- ffmpeg has no PipeWire input, so it cannot grab a native Wayland window. An app launched with `env -u WAYLAND_DISPLAY` opens an Xwayland window that `x11grab` can record.
- Window id: `xprop -root _NET_CLIENT_LIST`, then `xprop -id <id> WM_NAME`. Hana debug titles contain `debug <port>`. xwininfo, xdotool and wmctrl are not installed.
- `ffmpeg -f x11grab -framerate 60 -window_id <id> -i $DISPLAY -t <secs> -c:v libx264 -preset veryfast -crf 18 -pix_fmt yuv420p out.mp4`: 7 s at 1280×720, 60 fps = 330 KB, real speed.
- Working prototype: `/tmp/claude-1000/-home-natepiano-rust-tool-based-ui-startup-polish/556caf6e-713d-47c0-a8fc-5dd5e8c26ce4/scratchpad/p14_record.sh` (launch Hana on a test port, find window, record, shut down over BRP `brp_extras/shutdown`). Memory: `~/.claude/projects/-home-natepiano-rust-hana/memory/record-hana-video-via-xwayland.md`.
- Clip check without watching: per-frame `signalstats` `lavfi.signalstats.YAVG`; for a design review, extract stills for a fresh helper.
- Bevy 0.19 `EasyScreenRecordPlugin` is not a substitute (needs libx264 neither machine has, toggles on Space, steps `Time<Virtual>`).

## Decisions (showrunner)

- **Name:** `/screen_record` (`commands/screen_record.md`), script under `scripts/screen_record/`.
- **Linux only now.** The Mac (`screencapture -v` / avfoundation) waits for a real consumer there.
- **Records an existing window by title substring.** Launching and shutting down the app stays with the caller; the skill doc gives the Hana recipe (Xwayland launch, BRP shutdown after).
- **Length guardrails:** the duration is required, no default; a hard ceiling of 60 s with no override flag; an outer `timeout` (duration + 15 s) kills ffmpeg if `-t` fails; `-fs` caps the file at 500 MB. A refusal names the limit it hit.
- **Cleanup:** one directory, `~/.cache/screen-record/`. Every run first prunes clips older than 7 days, then the oldest until the directory is under 2 GiB. The skill doc tells callers to delete their clips once sent or reviewed. Check it against the disk-floor sweeper (500 GiB floor on `/`) and say in the as-built how the two relate.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills and scripts; this plan adds the `/screen_record` skill (Phase 1), then makes `/unit:report` timers flagged when missing and never skipped after a rejected report (Phase 2), then keeps a Codex seat alive when its thread reads `systemError` after a refused turn (Phase 3). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
- **Project started:** 2026-10-06T11:37:09.858+00:00
- **Stack:** zsh for `scripts/production/unit_status.sh` and `scripts/message/notifier.sh`; Python 3.13, standard library only (`argparse`, `subprocess`, `pathlib`, `shutil`, `unittest`); Markdown command docs. At run time the script calls `xprop`, coreutils `timeout` and `ffmpeg` (x11grab input, libx264), all in `/run/current-system/sw/bin` on natedev. natedev runs KDE Plasma on Wayland with Xwayland (`DISPLAY=:0`).
- **Layout:**
  - `commands/screen_record.md` — the skill doc (new)
  - `scripts/screen_record/screen_record.py` — the recorder (new)
  - `scripts/screen_record/test_screen_record.py` — its tests (new)
  - outside the repository, at run time: clips in `~/.cache/screen-record/`
  - `scripts/production/unit_status.sh` — the showrunner's per-unit status script (Phase 2)
  - `scripts/production/test_unit_status.py` — its tests (new, Phase 2)
  - `scripts/delegate/progress_history.py` — the progress recorder; `progress` restarts the unit's notifier (Phase 2)
  - `scripts/delegate/test_progress_history.py` — the recorder's tests (Phase 2)
  - `scripts/agents/codex_mesh.py` — the Codex seat launcher and app-server client (Phase 3)
  - `scripts/agents/test_codex_mesh.py` — its tests, with a stub app-server (Phase 3)
- **Key files:**
  - `commands/build_hold.md` — command doc convention: one-line frontmatter `description:` ending in `Args - …`, then numbered steps that run `python3 ~/.claude/scripts/<dir>/<script>.py`
  - `scripts/build_hold/build_hold.py` — typed Python convention: `from __future__ import annotations`, dataclasses and `TypedDict`, no `Any`
  - `scripts/berth/work_order.py:507` — `class WorkOrderArguments(argparse.Namespace)`, the typed pattern for parsed arguments that keeps basedpyright free of `Any`
  - `scripts/build_hold/test_build_hold.py` — test convention: `setUp` points every state directory at a temporary directory through environment variables (`BUILD_HOLD_DIR`), `@override` on `setUp`, `_ =` for ignored results
  - `scripts/delegate/test_verify_untested_examples.py` — a test that runs a script as a subprocess with stub executables on `PATH`
  - `pyrightconfig.json` — gives a sibling-import path only to the script directories it lists; `scripts/screen_record` has none, so the tests run the script as a subprocess and never import it (plan author's choice; this plan does not edit `pyrightconfig.json`)
  - `/etc/nixos/modules/linux/disk-floor.nix` and `scripts/lint/sweep.py` — the disk-floor job, read only for how it relates to the clip cap; nothing changes in either (`/etc/nixos` is natedev's machine configuration)
- **Key files (Phase 2, read only):**
  - `scripts/message/notifier.sh` — `health` (line 330: exit 1 with `failing: no instance`, `failing: no tick since …` or `failing: last two sends exit a, b`) and `restart` (sets `LAST_RESTART`, prints `next_due=`); the hold skips a tick while `LAST_SENT > LAST_RESTART`
  - `scripts/hooks/delegate_run.py` — `check`: exit 0 while work is in flight, 1 while idle; honours the test-only `PLAN_DELEGATE_ACTIVE_DIR`
  - `docs/as-built/session-notifier.md` — the notifier's as-built: hold, restart, and the `TICKS FAILING` line
- **Test lanes:** `scripts/screen_record/`, `scripts/production/` and `scripts/delegate/` — `test_*.py` beside the scripts; this repository keeps each script's tests beside it and has no `tests/` directories.
- **Build:** none — Python and Markdown; nothing compiles.
- **Test:** `python3 -m unittest discover -s <dir> -p '<pattern>'`, run from the worktree root, with the directory and pattern each Acceptance gate names.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`; it exits 3 in every checkout because `pyrightconfig.json` names a `.venv` no checkout has, so its exit status says nothing. `bash -n <file>` on any changed bash script, `zsh -n <file>` on any changed zsh script.
- **Style:** none — not Rust (showrunner, 2026-10-06).
- **Invariants:**
  - Linux only; the Mac waits for a real consumer there (showrunner decision, 2026-10-06).
  - A test never writes the real `~/.cache/screen-record` (it sets `SCREEN_RECORD_DIR` and `HOME` to a temporary directory), never runs the real `ffmpeg` or `xprop` (stubs on a `PATH` that holds only the stub directory), and never launches an app or opens a window (showrunner, 2026-10-06). The live recording is the unit director's smoke in the Acceptance gate, not a seat's.
  - Python is typed throughout with no `Any` and no file-level type ignores; basedpyright reports 0 errors and 0 warnings (user rule, `~/.claude/CLAUDE.md`).
  - `~/.claude` main is the live configuration and each merged phase goes to it at once (production rule): seats edit only the worktree copies, and the doc names the script by its live path, `~/.claude/scripts/screen_record/screen_record.py`.
  - Tests never write `~/.local/state/notifier`, `~/.local/state/buildlog` or the real `/tmp/claude/delegate/active`, never run real `tmux`, ssh, rsync, gh, messages or pushes, and never kill processes they did not start (user rules for this production).
  - Saved run output stays under a few GB: read each run and delete it before the next. A disk-floor sweeper keeps 500 GiB free on `/` by deleting every unit's build caches (user, 2026-10-04).
  - Times carry their zone: this plan states PDT (America/Los_Angeles); natedev's clock and journal are EDT.

## Phases

### Phase 1 — /screen_record records a window on Linux, with length and disk guardrails · status: done

#### As-built

- `python3 ~/.claude/scripts/screen_record/screen_record.py --window <title substring> --seconds <N> [--name <label>]` (Python 3.13 standard library; runs `xprop`, `timeout` and `ffmpeg` by argument list, never a shell) records one existing X11/Xwayland window and prints the clip's absolute path as stdout's only line. `--seconds` is required with no default, a whole number from 1 to 60; `--window` is a non-empty, case-sensitive substring of `WM_NAME`; `--name` defaults to `clip` and matches `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. No flag, environment variable or setting raises the 60 s ceiling, the 15 s timeout margin or the 500 MB file cap.
- Exit codes: 0 recorded. 2 refused before anything runs (not Linux, `--seconds` missing or outside 1–60, empty `--window`, bad `--name`, unknown flag): one stderr line naming the limit, values echoed with `repr`, no directory created, nothing pruned, no tool run. 1 could not record (missing `xprop`/`timeout`/`ffmpeg` named together, unset `DISPLAY`, failed lookup, any other ffmpeg exit; partial clip deleted). 3 a limit stopped it: the outer `timeout --kill-after=5 <seconds + 15>` (partial clip deleted, stdout empty) or the `-fs 500000000` cap (clip kept, path printed).
- Clip directory: `SCREEN_RECORD_DIR` when set and non-empty, else `~/.cache/screen-record/` (mode 0700). Before the window lookup, every run deletes regular `*.mp4` files there older than 7 days, then the oldest by mtime until the total is under 2 GiB, and reports the counts on one stderr line; symlinks, subdirectories and other names are left alone, and a failed delete is reported and skipped.
- Window lookup uses `xprop` alone (xwininfo, xdotool and wmctrl are not installed on natedev): `xprop -root _NET_CLIENT_LIST`, then `xprop -id <id> WM_NAME` per window, polled every 25 ms for at most 10 s, each call bounded by a subprocess timeout equal to the time left. Exactly one match records; several fail at once and none fail after 10 s, both naming the titles seen. `find_window` returns `FoundWindow(window_id, title)` or raises `WindowLookupFailure`.
- The clip path `<dir>/<name>-<UTC YYYYmmddTHHMMSSZ>.mp4` (`-2`, `-3`, … on collision) is reserved with `os.open(O_CREAT|O_EXCL|O_WRONLY, 0o600)`, then ffmpeg writes it with `-y` (x11grab at 60 fps, decimal `-window_id`, libx264 veryfast crf 18, yuv420p); every failure deletes only that reserved path.
- `commands/screen_record.md` is the `/screen_record` skill: args `'<substring>' <seconds> [name]`; the caller launches the app before and shuts it down after; the Hana recipe launches under `env -u WAYLAND_DISPLAY` on a port chosen per launch, never 15702, and matches the title `debug <port>`; clips are checked by length (ffprobe) and brightness (signalstats, 16 is black), stills for a design review go to the caller's scratchpad, and the clip is deleted once sent or reviewed.

**Files:**
- `scripts/screen_record/screen_record.py` — the recorder: refusals, tool and display checks, prune, window lookup, recording under `timeout`, outcomes
- `scripts/screen_record/test_screen_record.py` — 22 `unittest` cases driving the script as a subprocess, with `xprop`/`timeout`/`ffmpeg` stubs alone on `PATH` and a temporary `HOME`; the no-match case waits the full 10 s lookup by design
- `commands/screen_record.md` — the skill doc

**Gotchas:**
- ffmpeg has no PipeWire input, so a native Wayland window cannot be recorded; launch the app with `env -u WAYLAND_DISPLAY`. An ffplay test window also needs `SDL_VIDEODRIVER=x11`, or SDL opens a native Wayland window absent from Xwayland's client list.
- The disk-floor job (`/etc/nixos/modules/linux/disk-floor.nix`) frees only cargo target directories and never sweeps the clip directory; the prune is that directory's only bound: under 2 GiB at run start, plus one clip of at most 500 MB.

**Ruled out:**
- `-n` with an `exists()` check: two runs with one label in one second could delete each other's clip.
- Bevy 0.19's `EasyScreenRecordPlugin`: it needs libx264 in the app, toggles on Space, and steps `Time<Virtual>`.
- A Mac backend (`screencapture -v` or avfoundation): no consumer there yet.

### Phase 2 — /unit:report timers: flagged when missing, never skipped after a rejected report · status: done

#### As-built

- `unit_status.sh` checks notifier health for every unit whose active-run marker (keyed by the unit's Claude session id) names a non-empty session directory, idle or working: it runs `zsh "$NOTIFIER" health "delegate-${session_dir:t}"` and on exit 1 prints `TICKS FAILING (<reason>)`. No marker, an empty marker, or health exit 0 prints nothing. The script stays zsh and portable to Linux and the Mac.
- The marker directory is `${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}`, the same test-only setting `delegate_run.py` and `unit_notifier.sh` honour; live callers leave it unset.
- `progress_history.py progress` restarts the unit's notifier as its first act: `_progress` calls `_restart_unit_notifier(session_dir)` once, right after `session_dir` and `now` are set and before `_read_state`, and passes the result to `_next_report_at`. A refused call (no open window, percent checks, missing phase reason flag, missing `--cap-stage`) keeps its message and exit status but leaves `LAST_RESTART` at the call time and `NEXT_DUE` one interval later, so the notifier hold (`LAST_SENT > LAST_RESTART`) no longer skips the next slot. A restart that fails or finds no instance returns `None` and never changes the report.

**Files:**
- `scripts/production/unit_status.sh` — showrunner's per-unit status; tick health for every active run; marker-directory setting.
- `scripts/production/test_unit_status.py` — runs a copy of the script in a temp tree with stub `py`, `sessions.py`, `notifier.sh`, idle `delegate_run.py`, and fake `tmux`/`pgrep` on `PATH`; covers idle failing (`TICKS FAILING (no instance)`), healthy, missing marker and empty marker.
- `scripts/delegate/progress_history.py` — `_progress` restarts the notifier before reading state or refusing.
- `scripts/delegate/test_progress_history.py` — two refused-report tests (missing `--cap-stage` on the dual layout, no open window after the pass closes) asserting failure plus `LAST_RESTART` at the call time and `NEXT_DUE` 15 minutes later.
- `docs/as-built/session-notifier.md` — TICKS FAILING for idle units, `PLAN_DELEGATE_ACTIVE_DIR`, and the `progress` restart.

**Gotchas:** A live check of `TICKS FAILING` prints nothing while every instance is healthy; prove the line with a temp `PLAN_DELEGATE_ACTIVE_DIR` whose marker names a run with no notifier instance. Tests point `PLAN_DELEGATE_ACTIVE_DIR` and `NOTIFIER_STATE_DIR` at temp directories and never touch the real marker or notifier state.

### Phase 3 — A Codex seat survives a refused turn: `systemError` means no live turn · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT (America/Los_Angeles).

**Source:** the user via trunk, 2026-10-06 05:1x PDT: Codex seats keep crashing mid-turn; "fixed, not worked around. Codex stays the seat we use." Seats die with `codex_mesh: <seat>: thread <id>: thread/read failed: unrecognized status systemError`, often then "could not be interrupted"; 10+ deaths since 2026-10-05 in widget-enhancements, frame-time, organon and trunk.

**Cause (showrunner's probe, checked):** every errored turn in `~/.codex/sessions/2026/10/05` and `/06` (45 of 45) ends in `task_complete` error "Selected model is at capacity. Please try a different model.", `codex_error_info` `server_overloaded`; the latest death, thread `01a1111b-c366-7340-a82a-b1fcc9208e6d`, ends so at 05:07:55 PDT. The app-server (codex rust-v0.160.1, same in 0.159.3) then sets the thread status `systemError` (`app-server/src/thread_status.rs:174-179, 456`; via `note_system_error`, `bespoke_event_handling.rs:1030-1033`). `systemError` is not terminal: the thread is loaded, no turn runs, the last turn ended in an error; `turn/start` has no status gate and `note_turn_started` clears it (`thread_status.rs:147-151`). `ThreadStatus` is `notLoaded | idle | systemError | active{activeFlags}` (`codex app-server generate-ts`, `v2/ThreadStatus.ts`). `_read_live_turn` (`codex_mesh.py:1392`, check at 1402) accepts only `idle`, `notLoaded` and `active`, so the capacity loop's `thread/read` (1050) gets `ThreadStateUnknown` and 1052 raises `SystemExit`; the capacity retry from Phase 8 (179aeb5) has never run against the real server. The stub at `test_codex_mesh.py:340` answers `idle` after a refused turn, so no test caught it.

**Goal:** a seat whose turn is refused for capacity backs off and resumes on the same thread, as Phase 8 designed; a seat whose turn fails otherwise reports that turn's own error; relaunch and `end` treat a `systemError` thread as having no live turn.

**Spec:**
- `_read_live_turn`: `systemError` returns `ThreadIdle()` alongside `idle` and `notLoaded`, with a one-line comment naming its meaning and the upstream source above. Any status the protocol does not define still returns `ThreadStateUnknown`.
- The capacity budget is per busy spell, not per seat: `capacity_waited` and `capacity_retries` (`codex_mesh.py:983-984`) never reset today, so a long seat's separate busy spells share one 20-minute budget and later spells start at a longer wait. Reset both to 0 when a turn completes without a capacity refusal. The schedule within a spell stays 30 s doubling to a 300 s cap, 1200 s in all.
- Nothing else in the capacity loop, `_retry_warranted` or the relaunch path changes unless a test below fails without it; say so in the checkpoint if one does.
- Stub app-server (`test_codex_mesh.py`): after any turn that ends with an error (`turn/failed`, or `turn/completed` carrying `error`), `thread/read` answers `systemError` for that thread until the next `turn/start`, as the real server does; an entry a test sets in `thread_statuses` still wins.
- `test_failed_seat_relaunches_when_old_thread_has_unknown_status` (line 632) uses `systemError` as its unknown status; give it a status the protocol does not define (`retired`), so it keeps covering the unknown path.

**Files:**
- `scripts/agents/codex_mesh.py` — `_read_live_turn` accepts `systemError`.
- `scripts/agents/test_codex_mesh.py` — the stub's error status; the unknown-status test's status; the new tests.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/agents/codex_mesh.py`.
- `test` — `scripts/agents/test_codex_mesh.py`:
  - every existing capacity test (`test_capacity_*`, `test_structured_capacity_error_resumes_the_same_thread`) passes with the stub answering `systemError` after the refusal, and resumes on the same thread without repeating the prompt;
  - a non-capacity turn failure (`test_other_turn_failure_does_not_use_capacity_backoff`) exits with the turn's own error text and no `thread/read failed`;
  - a failed seat whose old thread reads `systemError` relaunches, sends no `turn/interrupt`, and logs no "could not be interrupted";
  - `end` on a seat whose thread reads `systemError` logs no "could not be interrupted".
  - a seat refused, then completing a turn, then refused again starts its second wait at 30 s with the full 1200 s budget.

**Constraints from prior phases:** this phase touches no Phase 1 or Phase 2 file.
- Tests never start a real `codex` or app-server, never reach the network, and never write `~/.codex`.
- Saved run output stays under a few GB; read each run and delete it before the next.

**Acceptance gate:**
- From the worktree root, `python3 -m unittest scripts.agents.test_codex_mesh` green.
- `basedpyright scripts/agents/codex_mesh.py scripts/agents/test_codex_mesh.py` reports 0 errors and 0 warnings (the counts are the gate).
- The checkpoint notice says what a seat now prints when a capacity refusal outlasts the 20-minute budget.
