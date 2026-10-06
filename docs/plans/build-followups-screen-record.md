# Screen recording skill

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The `/screen_record` skill on Linux, then `/unit:report` timers that are flagged when missing and never skipped after a rejected report.

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

- **Project:** `~/.claude` — Claude Code commands, skills and scripts; this plan adds the `/screen_record` skill (Phase 1), then makes `/unit:report` timers flagged when missing and never skipped after a rejected report (Phase 2). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-stalls` on branch `build-followups-stalls` (unit `stalls-unit` of production `build-followups`).
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

### Phase 2 — /unit:report timers: flagged when missing, never skipped after a rejected report · status: todo

#### Work Order

Work only in worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. State every time in PDT (America/Los_Angeles).

**Goal:** `unit_status.sh` prints `TICKS FAILING (…)` for any unit whose run is active and whose notifier health fails, idle or working; and every `progress_history.py progress` call that gets as far as its session directory restarts the unit's notifier clock, including one it then refuses.

**Spec:**

*Status while idle* (`scripts/production/unit_status.sh`). Today the health check at lines 97-104 runs only when `delegate_run.py check` exits 0 (work in flight), so an idle unit with no notifier instance prints nothing; hana's geometry-material unit showed `failing: no instance` from 2026-09-29 and dropped out of every status whenever idle (showrunner, 2026-10-06).
- Drop the `"$PY" "$CHECK" check …` condition. Whenever the active marker for the unit's Claude session id names a non-empty session directory, run `zsh "$NOTIFIER" health "delegate-${session_dir:t}"` and, on exit 1, print `TICKS FAILING (${health#failing: })` exactly as today. No marker, an empty marker, or health exit 0 prints nothing. `CHECK` and its assignment go once nothing reads them.
- The marker directory becomes `${PLAN_DELEGATE_ACTIVE_DIR:-/tmp/claude/delegate/active}`, the test-only override `delegate_run.py` and `unit_notifier.sh` already honour; a live caller leaves it unset.
- Update the header comment (lines 4-6) to say the tick health line prints for every unit with an active run.
- The script stays zsh and portable (Linux and the Mac): no GNU-only flags, no `${PIPESTATUS[0]}`.

*Refused report* (`scripts/delegate/progress_history.py`). `_progress` (from line 3557) calls `_restart_unit_notifier(session_dir)` only at line 3716, after every refusal: the "No open window to report" exit at 3589, the percent checks at 3614-3632, the override-reason refusal raised inside `_progress_decision` (lines 1842-1874), and the missing `--cap-stage` exit at 3679. A refused call leaves the notifier hold engaged (`LAST_SENT > LAST_RESTART`), so the next slot is skipped until the hold releases after two intervals: frame-time's 04:02 PDT report on 2026-10-06, refused for a missing `--phase-override-reason`, left a 30 minute gap; in the 24 hours to then, hold skips were trunk 7, frame-time 4, startup 1 (showrunner, 2026-10-06).
- In `_progress`, call `_restart_unit_notifier(session_dir)` once, immediately after `session_dir` and `now` are set and before `_read_state`, keep its result in a local, and pass that local to `_next_report_at` where line 3716 calls the restart today. No other call site changes. A refused call still exits with its current message and status; it now also leaves `LAST_RESTART` at the call time and `NEXT_DUE` one interval later. A restart that fails or finds no instance still never fails or changes the report (`_restart_unit_notifier` returns `None`).

**Files:**
- `scripts/production/unit_status.sh` — health check for every active run; marker-directory override; header comment.
- `scripts/production/test_unit_status.py` — new: the status tests below.
- `scripts/delegate/progress_history.py` — `_progress` restarts the notifier before any refusal.
- `scripts/delegate/test_progress_history.py` — refused-report restart tests beside `test_the_clock_line_uses_the_restarted_unit_notifier` (line 685).

**Seats:** 1 writer + 1 tester; the tests are concrete from the Spec alone.
- `impl` — `scripts/production/unit_status.sh`, `scripts/delegate/progress_history.py`.
- `test` — `scripts/production/test_unit_status.py` and the new methods in `scripts/delegate/test_progress_history.py`:
  - `test_unit_status.py` copies `unit_status.sh` into a temp tree at `<tmp>/scripts/production/unit_status.sh` (its `REPO` is `${0:A:h:h:h}`) and puts stubs beside it: `scripts/lib/py` (execs `python3 "$@"`), `scripts/message/sessions.py` (prints a fixed session id for `id <pid>`), `scripts/message/notifier.sh` (records its argv to a file, then prints the health line and exits with the code the test sets), `scripts/hooks/delegate_run.py` (exits 1, idle). A temp `PATH` directory holds fake `tmux` (`has-session` exits 0, `capture-pane` prints a short pane with a `— holding: waiting on x` line) and `pgrep` (prints one pid). `PLAN_DELEGATE_ACTIVE_DIR` points at a temp marker directory. Cases: idle unit with health `failing: no instance` (exit 1) prints `TICKS FAILING (no instance)`; health `ok` (exit 0) prints no `TICKS FAILING`; no marker file means `notifier.sh` is never called; an empty marker file means the same.
  - In `test_progress_history.py`, create the instance with `notifier.sh new` as the existing test does (`NOTIFIER_STATE_DIR` and `NOTIFIER_NOW_EPOCH` in a temp root), then run a `progress` call that is refused — once with no `--cap-stage` on the dual layout, once with no open window (after the pass closes) — and assert the command fails and the instance `state` has `LAST_RESTART` equal to the call time and `NEXT_DUE` equal to the call time plus 15 minutes.

**Constraints from prior phases:** The `/screen_record` phase owns `commands/screen_record.md` and `scripts/screen_record/` only; this phase touches none of its files.
- Tests never write `~/.local/state/notifier` or the real `/tmp/claude/delegate/active`, never run real `tmux`, ssh, rsync, gh, messages or pushes, and never kill processes they did not start.
- Saved run output stays under a few GB; read each run and delete it before the next. A disk-floor sweeper keeps 500 GiB free on `/` by deleting every unit's build caches.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_unit_status.py'` green.
- `python3 -m unittest discover -s scripts/delegate -p 'test_progress_history.py'` green, including the refused-report restart tests.
- `zsh -n scripts/production/unit_status.sh` clean.
- `basedpyright scripts/production/test_unit_status.py scripts/delegate/progress_history.py scripts/delegate/test_progress_history.py` reports 0 errors and 0 warnings (the tool's own exit status is 3 in every checkout; the counts are the gate).
