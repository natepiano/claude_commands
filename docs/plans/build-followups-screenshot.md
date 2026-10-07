# Screenshots: measured time to a framed shot, and proposals to cut it

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** A regime, run every hour, that measures how long any agent takes to get a usable framed screenshot, shows that shots taken by hand cost far more than `/hana_shot`, measures each `/hana_shot` change, and proposes the next change by measured minutes saved. The user approves each proposal before it is built.

> **Production: build-followups** — unit `screenshot-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-06 13:1x PDT: "create a long running unit director called screenshot … create the analysis regime that we can run on a continuous basis to first continue to show that hand run screenshots didn't work very well and that our hana_shot improvements are measurable and we should have this unit director available to suggest improvements so we can reduce the time for any agent to be able to take a screenshot (hana or even other projects) as quickly as possible". This is the user's 08:22 PDT ask, which the hana showrunner recorded: agents get a framed shot without working out the camera by hand.

## What exists today (checked 2026-10-06 by the plan author)

- **The hana showrunner's analysis** is at `~/.local/state/screenshot-analysis/seed/analysis.md`; read it first. It covers the method, the numbers, the improvements so far, what is not measured, and its list of next improvements. Its scripts sit beside it: `shot_analysis/shot_episodes.py` (the main measure), `shot_now.py`, `shot_history.py` (first pass; it timed the raw call, which is the wrong target), `episodes_300_now.txt`, and `hs_perf/` (`bench.py`, `trace.py`, `probe.py`, `before.json`, `hana_shot_before.py`, the parked speed work). They are unreviewed scratch code: port them, don't import them.
- **Baseline** (5-min split, Sep 9 to the 09:35 PDT rollout): 519 by-hand episodes, 48 h in all, median 4.0 min, p90 12.8. With `/hana_shot` since the rollout: 17 episodes, median 1.4 min, p90 7.5. One `/hana_shot` call takes about 0.5 s, so the rest of an episode is agent turns.
- **`/hana_shot`:**
  - `commands/hana_shot.md` and `scripts/hana_shot/hana_shot.py` (2,085 lines, with `test_hana_shot.py`).
  - Commits: b01a299 (rollout), 7d19a7b, ec6703e (`--remote`), 4f10e77; efb0eab names it in `delegate.md`'s live-probe step.
  - The hana showrunner built it and still edits it, for example when Hana's startup log text changes.
  - It writes one line per successful shot to `~/.cache/hana-shot/timings.jsonl`; `hana_shot.py stats [--since DATE]` reads that file.
  - Failures are not recorded.
- **Transcripts:**
  - Claude: `~/.claude/projects/*/*.jsonl`, subagents included; scan with `rg -l --no-ignore --hidden`.
  - Codex: `~/.codex/sessions/`, on natedev and the Mac.
  - The Mac's Claude transcripts are not on natedev.
- **The hourly job:** `buildlog hourly` (`scripts/buildlog/`; nixos `modules/linux/buildlog.nix` runs it on natedev), which already calls `rust_release.py`.

## Decisions (showrunner)

- **A ~/.claude worktree, not a hana one** (user): the regime is tooling for every project.
- **New code goes in `scripts/shot_report/`**, with the report command `commands/shot_report.md` (`/shot_report`). History and caches go in `~/.local/state/screenshot-analysis/`.
- **One scheduler:** the scan runs from `buildlog hourly`. There is no new systemd unit and no nixos edit.
- **An episode** follows the showrunner's method (`analysis.md` → Method):
  - The 5-min split is the headline and the 15-min split is reported beside it.
  - Every project counts. A project is the git top-level of the transcript's cwd.
  - Claude and Codex both count, split by agent.
- **By hand** means any screenshot not taken through `/hana_shot`: the raw BRP MCP screenshot tool, or BRP's screenshot method sent from a Bash command (curl, python). Other projects' screenshot commands count once Phase 1 finds them.
- **The user approves every improvement** (user): after Phase 4 the unit only measures and proposes, until a proposal is approved.
- **`scripts/hana_shot/` is this unit's to change** within this production. The hana showrunner may still land urgent fixes on `~/.claude` main. Merge `origin/main` into the unit branch before each phase.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds the screenshot episode report (Phase 1), counts screenshot scripts run in later calls (Phase 2), records every `/hana_shot` call with failures and the kept shot (Phase 3), runs the report hourly with each change's effect (Phase 4), brings the Mac's transcripts up to date in a few hourly runs and fixes two report types (Phase 5), then proposes improvements (Phase 6). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-screenshot` on branch `build-followups-screenshot` (unit `screenshot-unit` of production `build-followups`).
- **Project started:** 2026-10-06T20:30:00+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/shot_report/` — transcript scan, episodes, history, report (new)
  - `commands/shot_report.md` — `/shot_report [--since DATE] [--project NAME]` (new)
  - `scripts/hana_shot/hana_shot.py`, `test_hana_shot.py`, `commands/hana_shot.md` — Phase 3 recording
  - the `buildlog hourly` call site in `scripts/buildlog/` — Phase 4 (one call, like `rust_release.py`'s)
  - outside the repository: `~/.local/state/screenshot-analysis/` (history, scan cache, seed)
- **Key files:** `~/.local/state/screenshot-analysis/seed/analysis.md` and its scripts; `scripts/hana_shot/hana_shot.py` (`stats`, the timings writer); `scripts/buildlog/` (the hourly entry and how `rust_release.py` hooks in); `commands/builds.md` (the shape of a report command).
- **Port:** 15797, for any live Hana smoke. Never the user's default port or another unit's.
- **Test lanes:** `scripts/shot_report/`, `scripts/hana_shot/`, `scripts/buildlog/` — `test_*.py` beside the scripts.
- **Test:** `python3 -m unittest discover -s scripts/shot_report -p 'test_*.py'` (plus `-s scripts/hana_shot` and `-s scripts/buildlog` when touched), from the worktree root.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never read the real `~/.claude/projects`, `~/.codex` or `~/.cache/hana-shot`; they use fixtures.
  - The scan reads transcripts and timing logs read-only. It never writes or moves either source.
  - The hourly scan reads only new bytes since its cache, and stays under 30 s on a run with nothing new.
  - Each number in a report states its window, its split and its count. Medians and p90 come with n.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - Times state PDT. Transcript and buildlog stamps are UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 6's next re-rank | a week of `/hana_shot` data after the user deferred the refusal fix | the clock: Wed 2026-10-14 07:50 PDT |

## Phases

### Phase 1 — One command shows how long an agent takes to get a framed shot, by hand vs `/hana_shot`, in every project · status: done

#### As-built

- `/shot_report` runs `shot_report.py` with `scan`, `report [--since DATE] [--until DATE] [--project NAME]` and `survey`. `scan` reads Claude (`~/.claude/projects`) and Codex (`~/.codex/sessions`) transcripts into `ToolCall`s (kind `shot` or `other`, a `source`, `image_count`, project = repo name from the cwd's git common dir), splits each transcript into episodes at 300 s and 900 s measured from the previous result time, drops episodes with no shot, and writes `~/.local/state/screenshot-analysis/episodes.jsonl` (atomic replace) and `survey.json`. An episode is `/hana_shot` when any shot in it is a `/hana_shot` call, otherwise by hand.
- `report` reads only `episodes.jsonl`: one aligned row per method × agent × project with 5-min and 15-min columns side by side (count, total hours, median/p75/p90 minutes, shots and other BRP calls per episode), then median and p90 minutes per image, `/hana_shot` calls and images, the by-hand session count with the newest 20, and by-source totals. Dates without a time are America/Los_Angeles.
- Shell shots are classified by parsing the command: positional BRP methods, `"method":` keys, Python call literals (`rpc(...)` and kin), heredocs fed to an interpreter, scripts written and run in the same call, compound commands, and wrappers such as `bash brp.sh brp_extras/screenshot …`. MCP BRP calls matching `BRP_SKIPS` (launch, shutdown, status, list, logs, type guide, schema, discover) are dropped. A `/hana_shot` call's image count is its distinct `.png` paths in the result; every other shot counts one image.
- Real data, measured 2026-10-06 on natedev (PDT): the scan covers 25,144 tool calls and saves 1,156 episodes, in 48 s in one run and 98 s in another (variance unexplained). The strict Claude-MCP subset gives 519 episodes / 47.65 h at the 5-minute split, against the seed's 519 / 48 h. Full by hand, 2026-09-09 to the `/hana_shot` rollout cut (2026-10-06T16:35Z): 568 episodes / 51.89 h at 5 minutes. All by hand: 585 / 52.82 h at 5 minutes; 536 / 61.84 h at 15 minutes. `/hana_shot` at 5 minutes: 81 calls, 73 images; by-hand sessions 49.
- Survey (calls / sessions / projects): bash_brp 165/15/5, hana_shot 81/7/2, mcp_brp 4212/32/14, spectacle 33/8/8; grim, screencapture, import and browser find none.

**Files:**
- `scripts/shot_report/transcripts.py` — `ToolCall`, `scan_calls`, `survey_counts`, the shell classifier
- `scripts/shot_report/episodes.py` — `Episode`, `split_episodes`, `write_episodes` / `read_episodes`
- `scripts/shot_report/shot_report.py` — CLI (`scan`, `report`, `survey`), tables, `STATE_DIR`
- `scripts/shot_report/test_shot_report.py`, `test_transcripts.py`, `fixtures/` (Claude and Codex transcripts; `classify/` shell and execution cases) — tests
- `commands/shot_report.md` — `/shot_report`

**Binds later work:** Each `episodes.jsonl` record carries `split`, `agent`, `project`, `method`, `source` (a sorted list), `start` and `end` (ISO), `screenshot_count`, `image_count`, `hana_shot_call_count`, `other_call_count`, `transcript_path`, `session_id`. "Every `/hana_shot` call is recorded…" and "The report runs every hour…" read this file. "Screenshot scripts run in later calls count as screenshots" owns the known undercounts: a script written in one call and run in a later call is not counted (87 such scripts, up to 487 runs); a scripted call taking several shots counts one image; a tool_result line carrying several tool_use_ids is matched on the first only; a scratchpad cwd shows as a project named `scratchpad` (8 episodes, 1.04 h).

**Gotchas:** Most by-hand shell shots go through wrapper scripts, inline Python `rpc(...)` calls and heredocs, not curl with a JSON body. `read_episodes` fills a missing `image_count` or `hana_shot_call_count` from `screenshot_count`, so an older file reads without error but with those defaults.

**Ruled out:** classifying a shell command by searching its whole text (it counted rg/cat/echo mentions and dropped real shots); treating heredoc bodies as text (a body fed to python/bash/sh runs).

### Phase 2 — Screenshot scripts run in later calls count as screenshots · status: done

#### As-built

- `transcripts.py` remembers scripts per transcript as `RememberedScript` (`content`, `classification_state`, `source`, `screenshot_calls`, `selected_content`). Sources: Claude `Write`, `Edit` of a remembered file, Codex `apply_patch` add/update/delete, `cat >`/`cat >>`/`tee` heredocs, and shell removals. Each script is tracked by normalized path and current content, so an append, replacement or deletion changes what a later run counts. Python content classifies through `_code_result` and shell content through `_shell_classification`, the same executing forms that classify a live call; there is no second classifier. Both readers read Write, Edit, `apply_patch` and path-only shell runs before their row filters; the Claude reader skips a `tool_result` line only when none of its `tool_use_id`s is pending.
- A later shell call links to a remembered script through `python3`/`bash`/`sh`/`zsh <path>`, `./path`, `source`/`. path`, wrappers such as `timeout`, `os.exec*` and `subprocess.run`, by full path or by name relative to the call's directory (including `cd`). A remembered file that runs another remembered screenshot file is itself a screenshot file.
- A run counts as a screenshot only when its arguments select the screenshot branch of a script that dispatches on arguments (argparse options and aliases, shell `$N` tests, `case`, sourced functions in command position); `selected_content` holds that branch. Any other run is a related call. `/hana_shot` calls are classified from their command line and never pass through script memory.
- Image count of a scripted shot: the distinct `.png` paths in the result; with none, the screenshot calls the selected content makes (loops, `range()`, called functions, functions passed by reference, decorators), at least 1. One shell command running several scripts counts each.
- `ToolCall` carries `script_path`, `script_paths` and `project_attribution`, which replaces `project_is_fallback: bool`. `ProjectAttribution` has five values: repository, scratchpad, last_path, removed_worktree, scratchpad_target_missing. A cwd under `/tmp/claude-<uid>/<encoded project dir>/<session>/scratchpad/…` belongs to the repository its encoded directory names (worktrees folded); a worktree deleted after the session folds into the live repository whose name it carries. The fold runs at the end of `scan_calls` over every call.
- `scan_calls` reads transcripts in a `ProcessPoolExecutor` (up to 4 workers, one transcript per task, input sorted largest first) and returns `TranscriptScan` (a list of `ToolCall` with `candidate_file_count` and `bytes_read`); `shot_report.py scan` prints both. The scan only reads transcripts.

**Measured** (2026-10-06 unless noted):
- Seed Claude MCP subset: 519 episodes / 47.65 h, unchanged.
- By hand at the 300 s split, 2026-09-09 to the `/hana_shot` rollout: 584 episodes / 53.73 h (the figure before scripted runs counted was 568 / 51.89 h).
- Real default scan, 18:09 PDT: 26,124 calls, 3,038 candidate files, 13.83 GB read, 39.6 s wall / 85.2 s user / 69.7 s sys, under load average 23/37/61.
- Same-load A/B/B/A: before the repair rounds 27.7 s wall / 69.6 s user; final 31.4 s wall / 76.6 s user (+13% wall, +10% CPU).
- Seat probe: 80 shot/bash_brp + 4 spectacle + 339 related scripts remembered; 140 later shot runs linked; 491 scratchpad calls all map to hana (26 by-hand episodes at the 300 s split before the rollout).- The 48 s vs 98 s scan difference came from transcript traversal (page cache and machine load), not the state-file location. Keeping ordinary source and prose files out of the script-name filter cut the traversal.
- 69 tests; basedpyright 0 errors, 0 warnings, 0 notes. Tests never read the real `~/.claude/projects`, `~/.codex` or `~/.cache/hana-shot`.

**Files:**
- `scripts/shot_report/transcripts.py` — script memory, run linking, branch selection, image counts, attribution, worker-pool scan
- `scripts/shot_report/shot_report.py` — scan summary line (candidate files, bytes read)
- `scripts/shot_report/test_transcripts.py`, `scripts/shot_report/fixtures/classify/{claude,codex}/` — the 69 tests and the write-and-run samples

**Binds later work:**
- Remembered script content and removals decide how a later run counts, so a transcript is not independent of its earlier bytes. The remembered-script memory lives for one transcript's read; the incremental scan persists it beside the cursor and reruns the repository fold over cached and new calls. Acceptance for that: a script written before the cursor and run after it counts once across two consecutive incremental runs.
- Kept-shot detection reads a scripted shot's image paths from the later run's result; `ToolCall.script_path` names the script it ran. A `/hana_shot` call skips script memory.
- One transcript is the pool's unit of work. The full scan runs 30–40 s, so a 30 s no-news bound needs the incremental cache; its before/after table starts from the numbers above.
- `RememberedScript` and `project_attribution` are implementation-only: the episode record carries `project` alone.

**Gotchas:**
- A script's classification depends on its current content and the run's arguments; one file can be a shot in one call and a related call in another.
- The 60 s scan bound holds only while ordinary source and prose files stay out of the script-name filter; wall time also swings with page cache and machine load.
- Six known limits, each a script shape none of 20 hand-read real runs showed, left uncounted until a real transcript shows one:
  1. `os.execle`/`os.execlpe` with a non-literal env expression is not followed.
  2. A zero-iteration loop (`range(0)`, an empty list) still reports one image.
  3. An omitted argparse option falls back to the first argument.
  4. `async def` and class methods are not followed by call counting (image count stays at least 1).
  5. `rm x; bash -c ./x` does not see the outer `rm`.
  6. A decorated function also called directly counts its body twice when the result has no `.png` path.

**Ruled out:**
- Counting any run of a file that holds a screenshot call: it made every helper subcommand (`brp.py query`) a shot.
- A second classifier for script content.
- The state-file location as the cause of the scan-time difference.

### Phase 3 — Every `/hana_shot` call is recorded, failures and the shot the agent kept included · status: done

#### As-built

- `scripts/hana_shot/hana_shot.py` appends one JSON line per `shot` call and per `views check` call to `~/.cache/hana-shot/timings.jsonl` when the call ends. Fields: `status` (success or failure), `exit_code`, `failure_reason` (a `FailureReason` string enum of 11 slugs: timeout, already_in_progress, black_capture, empty_crop, no_app, invalid_request, no_target, invalid_png, copy_failed, brp_error, shot_failed), `session` (state `present` with a value, or `absent`) and an ordered `attempts` list.
- Each successful attempt carries its own per-view timing fields (`SuccessfulAttempt` extends `TimingRecord`), so a multi-view call keeps one set per view. The legacy top-level timing fields stay on success lines; lines without `status` read as legacy successes. A failed attempt lists an image path only when the file's `mtime_ns` is at or after that attempt's start, so a stale `--out` file is never claimed.
- A failure to write the record prints one warning on stderr and leaves the call's exit code and stdout unchanged. `hana_shot.py stats` counts successes and failures by reason, and counts each successful view once.
- The session value is `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`, otherwise absent. A Codex seat inherits the director's `CLAUDE_CODE_SESSION_ID`, so the Codex variable has to come first. A Codex episode's session id is the full five-group thread id from the rollout file name; a Claude main transcript's file stem and row `sessionId` equal `CLAUDE_CODE_SESSION_ID`.
- `scan_calls(claude_root, codex_root, timings_path)` in `scripts/shot_report/transcripts.py` reads transcripts as before and also reads the timing log (`scan --timings-path PATH`, default `~/.cache/hana-shot/timings.jsonl`). It reads transcripts and the log read-only and never writes or moves either. Transcript output omits the failed views of `--view all`, so exact attempt counts come from the timing records.
- After the worker pool finishes, `_recorded_calls` runs once over all calls and links records to calls by session id plus time (within 10 s): a present-session record goes to the call whose end is nearest; an absent-session record links only when exactly one call overlaps; one call may take several records, in time order. Calls that ran in a deleted worktree are folded into their repository first.
- Each episode carries a kept-shot state (`OneCitedShot`, `SeveralCitedShots`, `NoneCited`, `NoObservablePath`, `LegacyEvidenceUnavailable`) and the saved fields `kept_shot_state`, `attempts_before_first_kept_shot` and `cited_attempt_count`. The attempt count is `ExactOrderedCaptureAttempts` when ordered timing records supply it and `AttemptCountInferredFromImages` (an estimate) otherwise.
- A citation is a recorded image path, whole and at a path boundary, in assistant text, in a `SendMessage` input (message, summary or object form), in a `codex_mesh.py --message` argument (only lines containing `.png`), in a checkpoint row, or in a file the agent wrote; paths with spaces link. Checkpoint notices leave a transcript as a `SendMessage` call or a `codex_mesh.py` argument, not as assistant text.
- Scan cost, real default roots, 2026-10-06, natedev: 26,136 calls from 3,050-3,053 candidate files, 13.88 GB read, 1,208 episodes saved at the 300 s and 900 s splits. The earlier baselines still hold: the seed's Claude MCP subset 519 episodes / 47.65 h and the full by-hand set 584 / 53.73 h (5-minute split, 2026-09-09 to the rollout cut). Same machine, alternating the two trees in a balanced order (two runs each, load average 36 to 254): this code averaged 43.3 s wall / 96.2 s user against 74.3 s / 118.0 s for the code before the change. Quiet-machine references: 39.6 s wall / 85.2 s user before the change, and 41.7 s wall / 98.3 s user for an earlier version of this tree (load 27 to 60). The change adds no measurable scan cost.
- Evidence on real calls: `~/.cache/hana-shot/timings.jsonl` holds five new-format lines from other sessions' calls (2026-10-06 19:23-19:25 PDT). All five are `status: success`, `exit_code: 0`, `session.state: present`, one attempt each (three `fit`, two `pose`, crop `none`, 0.6 s to 2.1 s total); all 202 earlier lines are legacy successes, and no failure line exists yet. The first, abbreviated, session and image path elided:

```json
{"time": "2026-10-07T02:23:41.080+00:00", "host": "natedev", "port": 15711, "mode": "fit", "crop": "none", "window": "1280x720", "total_ms": 726.7, "status": "success", "exit_code": 0, "session": {"state": "present", "value": "<session id>"}, "attempts": [{"mode": "fit", "total_ms": 726.7, "status": "success", "image_paths": ["<one .png path>"]}]}
```

**Files:**
- `scripts/hana_shot/hana_shot.py` — the invocation record, failure slugs, session evidence, `stats`
- `scripts/hana_shot/test_hana_shot.py` — a fake BRP server for each failure path, session precedence, the fresh-image rule, the write-failure test
- `commands/hana_shot.md` — record fields, failure reasons, session precedence, stats counting
- `scripts/shot_report/transcripts.py` — the timing join `_recorded_calls`, citation extraction, `_kept_calls`, the evidence types
- `scripts/shot_report/episodes.py` — kept-shot states and the saved counts
- `scripts/shot_report/shot_report.py`, `commands/shot_report.md` — the kept-shot table and the `--timings-path` option
- `scripts/shot_report/test_transcripts.py`, `test_shot_report.py` — join, citation and report tests

**Binds later work:**
- The incremental scan keeps each transcript's remembered scripts, pending uses and results and citation positions beside its byte offset, because a script's content decides how a later run counts.
- The timing join and the kept-path match run over all calls, so a cached scan reruns them over cached plus new evidence without taking one record twice; the repository-name fold reruns the same way.
- A timing record is appended when a call ends and its transcript result lands moments later, so a record can arrive one run before its call; unlinked records stay pending until their call is cached.
- The saved episode keeps exact-vs-inferred provenance for the attempt count; `episodes.jsonl` is rewritten whole by each scan and does not save it.
- The session variable order and the full Codex thread id are what link records to calls.
- The report shows the kept-shot table only; the record already carries the 11 failure reasons and per-view timing (mode, crop, phase times) that failure-reason and per-mode tables read.
- The 60 s bound for a full scan is a quiet-machine bound.

**Gotchas:**
- A failed attempt's image path is judged by file `mtime_ns` against the attempt's start clock; a coarse kernel mtime can lag by a few milliseconds while a capture takes tens of milliseconds.
- An absent-session record links only when exactly one call overlaps within 10 s, so two absent-session records near two calls are skipped as ambiguous.
- `views check` runs write timing records and are not shot episodes.
- Citation matching substring-tests each citation against the latest shot paths, with no measurable cost.

**Ruled out:** classifying `views check` runs as shots (they record a stored-view check, not an agent's framing attempts); a second classifier next to the existing one.

### Phase 4 — The report runs every hour and shows what each change did · status: done

#### As-built

`buildlog hourly` runs the screenshot scan once per UTC hour under `hourly.lock`; a second run in the same hour prints `Screenshot scan skipped: already ran this UTC hour at <time> (<zone offset>)`. The scan cache is `PersistentScanCache` (`format_version` 3; a version change forces one cold rebuild) in `~/.local/state/screenshot-analysis/scan-cache.pickle`. It stores parsed state, not matched lines: per transcript at its byte cursor, the resumable Claude and Codex parser progress (pending uses and results, remembered scripts, session metadata, citation positions), raw calls, citations, unlinked timing records and open 5- and 15-minute episodes. `scan_calls(claude_root, codex_root, timings_path)` and the cache states keep their signatures. Discovery uses cached file identity, and the scan line states the bytes read. Every run reruns the timing join, kept-path match and repository fold over cached and new evidence; timing records are keyed `TimingRecordId(source_host, source_path, byte_offset)`, so a repeated join never duplicates an attempt and a record read before its call stays pending.

- `Episode` saves and reloads whether the attempts-before-first-kept-shot count is exact (`ExactOrderedCaptureAttempts`) or inferred from images (`AttemptCountInferredFromImages`). The recorder's in-flight record is `InProgressCaptureInvocation`. Invocation records carry an invocation kind (`shot`, `views_check`, `unknown` for old lines) and a source host, kept apart from the `host` a timing record carries for the machine running Hana; `views check` stays outside shot episodes.
- `changes.py` types the ledger `changes.json`: repository, commit, one-line description, effective time, host coverage and a measurement-change marker.
- `/shot_report` shows the by-change table (per method: window, episode count, median and p90 at 300 s and 900 s; under 20 episodes is flagged too small, missing host coverage marks the window incomplete; the heading names zone and offset per boundary), the weekly by-hand vs `/hana_shot` agent-hours trend, the last successful scan time, and `/hana_shot` evidence with windows: success, failure and legacy-success counts, each of the 11 failure reasons, per-mode and per-crop counts and phase-time medians over successful attempts. Invocation records aggregate on their own, so a failure with no transcript result stays visible.
- Mac Codex transcripts and the Mac `timings.jsonl` are delayed inputs read over `ssh mac` (the command prints `rc=$?`), one cursor per source host, mirrored in `mac-source/` with `mac_manifest.json`. The read is budgeted (about 80 s inside the 120 s ssh timeout), prefiltered at the source with the scan's own candidate hints, per-file and resumable. Coverage advances only on a finished read, stamped with the read start time; an outage keeps the mirror and its history. Mac Claude transcripts are out above 100 files / 100 MB (61 files / 112 MB measured).

Rules: by-change windows end at the next product change in any repository that reaches an overlapping host; every project counts (no project filter on change rows).

Measured 2026-10-06/07 on the build machine, 300 s split, Sep 9 PDT to 2026-10-06T16:35Z:
- First hourly run, cold state: 26,297 calls from 3,091 candidate files, 20,624,840,574 transcript and timing bytes read, 1,232 episodes saved at 300 s and 900 s in 124.6 s. Mac line: read 132,126,372 source bytes; catching up; Claude transcripts out. Both baselines reproduce: Claude MCP n=519 total_h=47.65, full by hand n=584 total_h=53.73.
- A second run in the same UTC hour is skipped in 0.5 s. The cache file is 12,253,845 bytes (about 12 MB, parsed state).
- A default-root scan with nothing new locally: 89,423,794 bytes read, 84,992,205 (about 85 MB) of them the Mac, in 86.5 s, almost all of it the Mac read budget. A local-only no-news run takes 0.5 to 0.7 s.
- The Mac holds 213 of 1,856 Codex files as candidates (3.75 GB) and one 80 s read copies about 100 MB, so its first catch-up is still running after 14 scans and takes about 37 hourly runs. The reader moves about a quarter of what the link carries: 50 MB raw over ssh took 9.1 s (5.5 MB/s), and `gzip -1` made the same 50 MB 22.9 MB.

**Files:**
- `scripts/shot_report/transcripts.py` — scan cache, parsed state, candidate hints, timing join
- `scripts/shot_report/shot_report.py` — report, Mac reader and receiver, coverage, by-change windows
- `scripts/shot_report/changes.py` — typed change ledger; `scripts/shot_report/episodes.py` — episodes with attempt-count provenance
- `scripts/hana_shot/hana_shot.py` — invocation kind on every call; `scripts/buildlog/cli.py` — the hourly job
- `commands/shot_report.md`, `commands/hana_shot.md` — report and recorder wording
- `test_shot_report.py`, `test_transcripts.py`, `scripts/hana_shot/test_hana_shot.py`, `scripts/buildlog/test_sync.py`, `scripts/shot_report/fixtures/` — tests; the Mac reader test runs the generated reader under a temporary `HOME` with a deterministic clock

**Binds later work:** the Mac catch-up and report-types work ("The Mac's transcripts catch up in hours, and two report types say what they hold") owns the catch-up speed (the Mac line reads finished within 8 runs), coverage by included source, the saved per-outcome read state, the mtime-aware resume, the exact-count provenance type and the two change variants. The by-change table and any priced proposal lack Mac data until then. Until it lands: Mac coverage excludes Mac Claude transcripts yet reads as covered; the Mac read state (finished or catching up) is printed on the scan line only, not saved or shown by `/shot_report`; a same-size in-place rewrite of a Mac file leaves a stale mirror; `Episode` carries a placeholder `ExactOrderedCaptureAttempts(())` payload and `Change.measurement_change` is a bare boolean. The Proposals phase states Mac coverage beside every ranked minute count, prices a host-specific candidate only from an eligible weekly count with its coverage (host-combined weekly counts cannot), and appends each approved product change to `changes.json`.

**Gotchas:**
- A transcript cached as a non-candidate is parsed whole from byte 0 when it first matches a hint, or it loses its session context.

**Ruled out:** a cache that replays matched transcript lines (9.2 GB, failed the speed gate); a project filter on change rows (a change's repository is where the tool lives, not where affected episodes ran).

### Phase 5 — The Mac's transcripts catch up in hours, and two report types say what they hold · status: done

#### As-built

- The Mac read is prefiltered at the source, keeps a cursor per file, and resumes without re-reading received bytes. Each read sends `zlib`-compressed, `base64`-encoded 64 KiB chunks, and byte accounting stays in source bytes. Coverage advances only on a finished read, stamped with the read-start time; an outage keeps the mirror and the coverage. Mac Claude transcripts are out above 100 files or 100 MB. A file rewritten in place at the same size is read again from byte 0.
- `scripts/shot_report/shot_report.py` holds `MAC_READ_BUDGET_SECONDS = 300` and `MAC_SSH_TIMEOUT_MARGIN_SECONDS = 30`; `MAC_SSH_TIMEOUT_SECONDS = 330` is derived from that pair, never a second literal. The ssh stream is the limit of a read: the reader's hint scan took 2.9 s of an 80 s read and the receiver 0.25 s.
- The hourly job's bound is `SCREENSHOT_HOURLY_TIMEOUT_SECONDS = 900` in `scripts/buildlog/cli.py`; `scripts/buildlog/test_sync.py` asserts it is at least `MAC_SSH_TIMEOUT_SECONDS + 300`.
- Host coverage is `CoveredHostSourcesThrough(at, sources)` or `HostNeverCovered`; the measured sources are claude, codex and timings. A legacy status file without `host_coverage` gives the Mac Codex and timings only; natedev lists timings only when the timing source is available. The Mac read state is `MacReadFinished`, `MacReadCatchingUp` or `MacReadUnavailable`, saved in the scan status and printed as `Mac: finished|catching up|unavailable` with the covered-through time and the included sources. A by-change window over the Mac is labeled `incomplete:mac`.
- `ExactAttemptCountFromOrderedCaptures` is the exact attempt-count provenance, distinct from the inferred type; both survive save and reload. `changes.py` holds `ProductChange` and `MeasurementChange` under the union `Change`; a measurement change is listed separately and never counted as a speed gain. No `ExactOrderedCaptureAttempts(())` marker or `measurement_change` bool remains. A by-change window is bounded by the neighbouring product change of any repository on an overlapping host.

**Measured:** on the real Mac, default-root scans in a scratch state directory under natedev load 60-150: the Mac read finished at scan 5 (gate: within 8). Source MB per scan: 681, 456, 649, 690, 1271 (about 3.75 GB). Scan walls 377, 334, 329, 324, 222 s. The scan after it read 0 Mac source bytes and took 4.6 s. Before the change: about 100 MB per 80 s run and not finished after 14 runs; an earlier 105 s budget took 12 runs (208, 263, 198, 352, 390, 194, 168, 191, 329, 565, 840 MB, then 48 MB). Parsed-state cache about 12 MB (plus 0.5 MB for the Mac). Suites: shot_report 117, hana_shot 31, buildlog 257, basedpyright 0 errors 0 warnings. The hourly job itself was not run against the real Mac; the acceptance loop called `scan` directly, and the 900 s bound is held by the test.

**Files:**
- `scripts/shot_report/shot_report.py` — Mac reader, read budget and ssh constants, coverage by source, read state, report lines
- `scripts/shot_report/changes.py` — `ProductChange` / `MeasurementChange`
- `scripts/shot_report/episodes.py` — exact attempt-count type
- `scripts/shot_report/transcripts.py` — prefiltered resumable mirror, per-file cursors, same-size rewrite re-read
- `scripts/shot_report/test_shot_report.py`, `scripts/shot_report/test_transcripts.py` — tests, including the real generated reader under a temp HOME with a deterministic clock
- `scripts/buildlog/cli.py`, `scripts/buildlog/test_sync.py` — the hourly bound and its test
- `commands/shot_report.md` — first run starts `hourly` once in the background, no polling; states the Mac read state and coverage

**Binds later work:**
- Mac candidates are priced from Codex transcripts and timings only, because Mac Claude transcripts are out; the first Mac catch-up takes about five scans.
- The saved scan status and the report text are what a proposal cites for the Mac read state and coverage.
- An approved proposal appends a `ProductChange` to `changes.json`; a `MeasurementChange` is listed separately and never counted as a speed gain.
- The hourly bound stays at least 300 s above `MAC_SSH_TIMEOUT_SECONDS`; the test fails when a raised read budget leaves the job bound behind.

**Gotchas:**
- A scan with an empty corpus rereads rg.
- `bytes_read` counts discovery once.
- The acceptance loop calls `scan` directly and cannot see the hourly job's time bound.

**Ruled out:**
- A project filter on change rows: every project counts, so a change is assessed against all of them.
- Raising the read budget without measuring: the split of one 80 s read (hint scans 2.9 s, chunk loop 77.1 s, receiver 0.25 s) shows the ssh stream, not the reader or receiver, ends a run.

### Phase 6 — Proposals: the next change, ranked by measured minutes saved · status: todo (standing)

**Blocked by:** G1 — the next re-rank waits until Wed 2026-10-14 07:50 PDT.

#### Work Order

**Goal:** the unit is always ready with the next improvement that saves the most agent time.

**Spec:**
- From the report, rank candidates by agent-minutes saved per week. A candidate the report cannot price ranks below every priced one.
- Seed the candidates from `analysis.md` → Next improvements:
  - other BRP calls and the thinking after them in long `/hana_shot` episodes (unpriced until timelines name the BRP calls);
  - the shot tool's refusals (deferred a week by the user on 2026-10-07; 5 of 98 shots and 35 s is the 2026-10-07 baseline, so re-price it from the week's data);
  - hana-frame settle (787 ms);
  - magick crops moved to extras' rect;
  - the parked speed plan;
  - one shot tool for any BRP app, not only Hana (declined 2026-10-07, below).
- Send natedev the top proposal only. It holds:
  - the measured cost today, with n;
  - what changes, in file:line terms;
  - the expected saving and how Phase 4's table will show it;
  - the cost to build.
- Then wait. natedev brings it to the user.
- An approved proposal becomes the next numbered phase, appended to this plan. Its As-built names the change list entry that measures it.
- A declined one is recorded here with the user's reason. The next proposal goes only when natedev asks.
- After each approved phase merges, re-rank with fresh data before proposing again.
- Price a candidate only from its eligible weekly count. The weekly trend and the mode and crop tables combine hosts, so read the saved `episodes.jsonl` and `invocations.jsonl` in `~/.local/state/screenshot-analysis/` by name, and show each candidate's eligible weekly count and its host coverage. A candidate whose eligible count is unavailable stays unpriced, and when none can be priced the proposal says so.
- Carry the Mac's source limits into every estimate that uses Mac data: say which Mac sources are in (Codex transcripts and timings) and which are out (Claude transcripts), and whether the Mac read is still catching up.
- Each approved phase appends its product change to `changes.json` with its effective time and affected hosts before the by-change table is used to assess it.

**Files:**
- `docs/plans/build-followups-screenshot.md` — each approved proposal as a new phase, each declined one with the user's reason

**Seats:** none — the unit director ranks and proposes from the report; no code is written until a proposal becomes its own phase.

**Constraints from prior phases:** Failure logging, Codex transcript scanning and kept-shot detection are delivered, so `analysis.md` → Next improvements entry 2, entry 3 and the failure-logging half of entry 1 are not proposed again; what fills long `/hana_shot` episodes is answered by the report's "What fills long /hana_shot episodes" section: agent thinking holds 61–67% of long minutes, other BRP calls 7–12%, file work 8–11%, retries 3–5% and shot calls 2–3%, so it is not proposed again. Its first window was under one day; re-rank from that section after a full week of `/hana_shot` data. The hourly phase supplies source-host coverage and says whether each attempt count is exact or inferred from images. The Mac catch-up delivers Codex transcripts and timings and finished in five hourly runs (`scan_status.json` → `mac_read_state`; check it before each estimate); Mac Claude transcripts stay out above 100 files or 100 MB, so every Mac estimate names that limit. A `changes.json` entry is either a product change, which is a speed candidate, or a measurement change, which the report lists separately and never counts as a speed gain; an approved proposal appends a product change carrying its effective time and affected hosts. Scale-aware `--window` (Phase 8, d1efe4c) is in `changes.json` as a product change on natedev and the Mac, effective 2026-10-07 08:04 PDT; it is not proposed again. Price weekly agent minutes from an observed eligible weekly count, keep per-shot milliseconds distinct from episode minutes, and leave a candidate unpriced when its evidence cannot support that calculation.

**Acceptance gate:** each proposal cites a report less than 24 h old whose scan covers every host it uses through a time at or after G1 (`scan_status.json` → `last_success` and `host_coverage`), the covered window and n for every number, both splits when it uses episode time, exact or inferred attempt evidence when it uses kept shots, and the calculation of expected agent-minutes saved per week. A candidate without those inputs is labeled unpriced and ranks below every priced one.

**Declined:** one shot tool for any BRP app, not only Hana (about 1.5 agent-minutes a week, 2026-10-07) — not approved for now; the user approved finding out why long `/hana_shot` episodes run long instead.

**Deferred:** the shot tool's refusals fix (help text for `--margin` 0–0.45 and a fix hint in the "pose needs focus and radius" and "not inside a git worktree; pass --views-file" refusals) — the user, 2026-10-07: "wait a week on the screenshot refusal fix". Deferred, not declined: when G1 clears, re-rank every candidate, this one included, from a week of data and send natedev the top one.

### Phase 7 — We know what fills a long `/hana_shot` episode, and its fix comes back priced · status: done

#### As-built

- Each `/hana_shot` episode, at both splits, carries an `EpisodeTimeline` built from its transcript's `TranscriptToolEvent`s, joined by `tool_use_id`. Every gap from start to end goes to one `GapCategory`: `hana_shot` (timed from its matching timing record when one exists), `retry` (a shot of the same view or target after a failed or uncited shot), `other_brp`, `builds_and_app_launches`, `file_reads_searches_and_edits`, `other_tool_calls`, `agent_time` (tool result to the next call), or `unattributed`, which is never folded into a named category. An episode with no transcript timeline is `TimelineUnavailable`, persists in `episodes.jsonl`, and the report counts it as excluded.
- Tool events are indexed per transcript (bisect by start and by latest relevant end) and categorized at parse time. The parsed-state cache is format 6; an older cache rebuilds itself on the first scan (446 s, inside the 900 s hourly bound). Timelines read only saved state and transcripts the scan already holds.
- The report section "What fills long /hana_shot episodes" gives, per category, long and short minutes, share of long minutes, median minutes per episode, and n. Long is at or above the 5-min split's p75, plus a 10 min and over row set; short is below the median. Rows cover all, natedev and mac, with window, split and host coverage stated; Mac rows say Mac Claude transcripts are out. The 10 longest episodes follow, with agent, project, session id, start, duration and their two largest categories.
- On 2026-10-07 data (about 15 h, all natedev), named categories hold 95–97% of long-episode minutes at both splits: agent thinking 61–67%, other BRP calls 7–12%, file work 8–11%, retries 3–5%, shot calls 2–3%; no-news scan 12.7 s, report 0.88 s. The 2026-10-07 proposal recommends building nothing yet and re-ranking after a full week of `/hana_shot` data, with the refusal fix (5 of 98 shots, 35 s) offered as a follow-up phase.

**Files:**
- `scripts/shot_report/episodes.py` — timelines, gap attribution, long and short cohorts
- `scripts/shot_report/transcripts.py` — tool event extraction with parse-time category
- `scripts/shot_report/shot_report.py` — the section and the longest-episode list
- `scripts/shot_report/test_shot_report.py`, `test_transcripts.py`, `fixtures/timeline/` — exact-gap fixtures, the exclusion test, a scale test
- `commands/shot_report.md` — the section documented

**Binds later work:** **Proposals: the next change, ranked by measured minutes saved** builds on "What fills long /hana_shot episodes" and its categories. Other BRP calls stay unpriced until timelines name the BRP calls, and the window is under one day, so weekly prices are extrapolations.

**Gotchas:** the no-news scan's 30 s bound is sensitive to per-episode work over all events; per-episode linear event scans or deep-copied episode records push it past the bound, so lookups stay indexed and records compact.

**Ruled out:** a rebuild-timelines command — the scan rebuilds timelines itself.

### Phase 8 — `--window 1280x720` gives a 1280x720 window on any display · status: done

#### As-built

- `ensure_window` (`scripts/hana_shot/hana_shot.py`) sizes the window from the effective scale, `resolution.scale_factor_override` when set, else the OS `resolution.scale_factor` (as Bevy's `WindowResolution::scale_factor()` does), and writes a physical target of round(logical × scale). The early return and the wait compare the camera's physical size with that target; the timings line's `window` field stays the camera's physical size, and crops and padding keep their units.
- `WindowResolutionValue` mirrors Bevy's `WindowResolution` wire record field for field, including `scale_factor_override: float | None`.
- `--window WxH` and a stored view's `window` are logical sizes; the PNG is that size times the effective scale (2560x1440 for 1280x720 at 2x). The `--window` help and `commands/hana_shot.md` item 3 say so.
- `changes.json` carries "Scale-aware --window" as a product change for natedev and the Mac, effective 2026-10-07 08:04 PDT.

**Files:**
- `scripts/hana_shot/hana_shot.py` — effective-scale window sizing, the resolution type, the `--window` help
- `commands/hana_shot.md` — item 3 states the logical size and the PNG size
- `scripts/hana_shot/test_hana_shot.py` — the BRP fake answers a scaled primary window, records `world.mutate_components`, and its camera's `physical_size` follows the mutation; three `test_window_uses_*` tests at 1280x720: base 1.0 with override 2.0 writes 2560x1440, base 2.0 with override 1.0 writes nothing, base 2.0 with no override writes 2560x1440

**Gotchas:** Bevy serializes an absent override as `null`. Hana's own crates set no scale override; one seen at runtime comes from test or session setup.

**Ruled out:** a semantic `EffectiveWindowScale` type in place of `scale_factor_override: float | None` — the TypedDict mirrors the wire record, and the optional resolves into a plain effective scale on the next line.
