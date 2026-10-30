# Screenshots: measured time to a framed shot, and proposals to cut it

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready; long-running.** A regime, run every hour, that measures how long any agent takes to get a usable framed screenshot, shows that shots taken by hand cost far more than `/hana_shot`, measures each `/hana_shot` change, and proposes the next change by measured minutes saved. The user approves each proposal before it is built.

> **Production: natedev** — unit `screenshot-unit`; production doc `docs/plans/natedev-production.md`

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
- **Key files:** `~/.local/state/screenshot-analysis/seed/analysis.md` and its scripts; `scripts/hana_shot/hana_shot.py` (`stats`, the timings writer); `scripts/buildlog/` (the hourly entry and how `rust_release.py` hooks in); `commands/build/report.md` (the shape of a report command; it replaced `commands/builds.md`, and it reaches this worktree with the next merge of `build-followups`).
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
  - User-facing times use `config/user_zone.conf` through `scripts/lib/user_zone.py`, with the applicable zone label and offset; `USER_ZONE` may pin tests. Transcript and buildlog stamps remain UTC.

## Gates

| Gate | Waiting | Waits on | Clears when |
| --- | --- | --- | --- |
| G1 | Phase 6's next re-rank | a week after Phase 10's failure texts landed | the clock: Thu 2026-10-22 15:07:01Z (11:07:01 AST) |

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
- Mac candidates are priced from Claude transcripts, Codex transcripts and timings; Mac Claude transcripts are included since the report says "Mac Claude transcripts: included" (re-rank 2026-10-26). The first Mac catch-up took about five scans.
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

**Depends on:** Phase 10 — the next re-rank measures the week after the failure texts land.

**Blocked by:** G1 — do not re-rank before 2026-10-22T15:07:01Z (11:07:01 AST); the acceptance gate also requires source coverage through that instant.

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
  - a fit that gets no answer on an open Screen's root (hana, 2026-10-08: the `AnimateToFit` event sent at `scripts/hana_shot/hana_shot.py:763` got no answer in 10 s, four tries, on organon's tip 6751bf6f6 with a stock new Screen open; `--mode hana-frame` on the same root shot in 2 s). The cause is on Hana's side, dated by organon to trunk's assembly staging merges, and trunk traces it after its Phase 86 gates, so it is not this unit's to fix. The tool-side candidate is a failure text that names `--mode hana-frame`; price it from the `failure_reason: timeout` records on fit shots, and drop it if Hana's fix lands first;
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
- Carry the Mac's source limits into every estimate that uses Mac data: say which Mac sources are in (Claude transcripts, Codex transcripts and timings since the 2026-10-26 re-rank) and which are out, and whether the Mac read is still catching up.
- Each approved phase appends its product change to `changes.json` with its effective time and affected hosts before the by-change table is used to assess it.

**Files:**
- `docs/plans/build-followups-screenshot.md` — each approved proposal as a new phase, each declined one with the user's reason

**Seats:** none — the unit director ranks and proposes from the report; no code is written until a proposal becomes its own phase.

**Constraints from prior phases:** Failure logging, Codex transcript scanning and kept-shot detection are delivered, so `analysis.md` → Next improvements entry 2, entry 3 and the failure-logging half of entry 1 are not proposed again; what fills long `/hana_shot` episodes is answered by the report's "What fills long /hana_shot episodes" section: agent thinking holds 61–67% of long minutes, other BRP calls 7–12%, file work 8–11%, retries 3–5% and shot calls 2–3%, so it is not proposed again. Its first window was under one day; re-rank from that section after a full week of `/hana_shot` data. The hourly phase supplies source-host coverage and says whether each attempt count is exact or inferred from images. The Mac catch-up delivers Codex transcripts and timings and finished in five hourly runs (`scan_status.json` → `mac_read_state`; check it before each estimate); Mac Claude transcripts are included since the 2026-10-26 re-rank (the report says "Mac Claude transcripts: included"), and every Mac estimate names which Mac sources are in. A `changes.json` entry is either a product change, which records changed product behavior and bounds comparison windows, or a measurement change, which the report lists separately and never counts as a speed gain; an approved proposal appends a product change carrying its effective time and affected hosts. Scale-aware `--window` (Phase 8, d1efe4c) is in `changes.json` as a product change on natedev and the Mac, effective 2026-10-07 08:04 PDT; it is not proposed again. The window-level restore (Phase 9) is in `changes.json` as a product change on natedev and the Mac, effective 2026-10-07 10:08 PDT; it is not proposed again. Since that change, a `/hana_shot` run ended by Ctrl-C, SIGTERM or SIGHUP writes its invocation record with `status: failure`, `exit_code` 130, 143 or 129 and `failure_reason: shot_failed`; before it, SIGTERM and SIGHUP left no record. When a candidate is priced from failures in `invocations.jsonl`, count records with those exit codes apart from failed shots. Price weekly agent minutes from an observed eligible weekly count, keep per-shot milliseconds distinct from episode minutes, and leave a candidate unpriced when its evidence cannot support that calculation. Since Phase 10 (540c062, in `changes.json` as a product change and a measurement change on natedev and the Mac, effective 2026-10-15 11:07 EDT), every failed invocation and failed attempt carries its failure text in `error`, so a re-rank reads failure texts from the records, not from transcripts; it compares the weeks before and after that time, and does not propose the failure texts again. Read `error` when present. Older failures without `error` remain in total failure counts but have unavailable text classification; do not read a missing field as no failure, or compare text-category counts across the measurement change without equivalent evidence. Report the classified and unclassified counts on each side, and leave category-specific savings unpriced when either side lacks sufficient evidence. Phase 11 (6d32781) delivers target-local face orientation (`face` on views, invocation and attempt records); do not propose it again. Face shots remain ordinary shots for pricing, and records without `face` remain eligible. Its product-change entry at 2026-10-15T20:53:36Z ends Phase 10's isolated after window on both hosts; name the full following week as containing both changes, and do not attribute its whole difference to Phase 10. Phase 12 (4727ce04) adds a launch warning that names uncommitted `crates/` files a Hana binary was built with; it changes no shot behavior and has no `changes.json` entry by design, so do not add one and do not propose it. Phase 13 (b208263a) adds a next step to the last three failure texts that named none: the settle timeout, the setup target that did not appear, and no mesh bounds for the crop. Do not propose them again; price their recovery before and after its product change. Since its measurement change, a failed invocation record carries top-level `view`, `target` and `mode` when the request parsed to a shot; older failure records lack them, so per-mode failure pricing reads the attempt `label` before that time. The same measurement change counts `hana/<name>` as a BRP call only when it is not a path segment, and the first scan after it lands re-parses local transcripts once. Other-BRP and file-work minutes from earlier re-ranks, the 2026-10-26 figures included, do not compare with later reports, and the shift is not a speed gain. Its two `changes.json` entries (product and measurement, natedev and the Mac) are in, effective 2026-10-26 18:22 AST, when b208263a reached the live `~/.claude` (merge 8430cd7d). Phase 14 (5a832e6f) makes the launch record write atomic and lets `shutdown` clean up after a stopped launch. Standalone `launch` and `shutdown` write no invocation records, so it has no `changes.json` entry and no direct report measurement by design. Do not add one and do not propose it.

**Acceptance gate:** each proposal cites a report less than 24 h old whose scan covers every host it uses through a time at or after G1 (`scan_status.json` → `last_success` and `host_coverage`), the covered window and n for every number, both splits when it uses episode time, exact or inferred attempt evidence when it uses kept shots, and the calculation of expected agent-minutes saved per week. A candidate without those inputs is labeled unpriced and ranks below every priced one.

**Declined:** one shot tool for any BRP app, not only Hana (about 1.5 agent-minutes a week, 2026-10-07) — not approved for now; the user approved finding out why long `/hana_shot` episodes run long instead.

**Re-rank 2026-10-14 (after G1):** report from the 15:31 EDT scan (both hosts covered through 15:31 EDT; Mac read finished; Mac Codex transcripts and timings in, Mac Claude transcripts out), window 2026-10-07 13:08 to 2026-10-14 15:31 EDT (7.10 days). All 2,429 shot records came from natedev; the Mac recorded none. 184 failed shots, priced as the time from each failure to the same session's next successful shot (at most 15 min): 151 min in the window, 149 min a week. The error text came from the transcript within 20 s of each record. Ranked by the minutes a fix would save, assuming a failure text that names its fix leaves about 10 s of recovery (the median recovery after the margin refusal, which already names its range):
1. **Approved by the user 2026-10-15; built as Phase 10. Every failure text names its next step.** It merges the deferred refusal fix with the two largest priced failure groups. Rejected fits (the camera did not move): n=20, 21.8 min. A target that found nothing: n=10, 19.9 min, 18 of them from one stale `entity:` id. Margin out of range: n=15, 10.4 min. Pose without focus and radius, and the view-name format: n=11, 8.3 min. About 53 agent-minutes a week saved, 60 at most.
2. The parked speed plan: about 2,245 successful shots a week at a median of 0.73 to 0.88 s each, so at most about 8 min a week even if the time is cut by 30%.
3. Moving magick crops to extras' rect: fit with a rect crop, n=108, takes 1.36 s against 0.75 s with no crop, about 1 min a week. Hana-frame settle: n=39 at a median of 1.09 s, under 1 min a week.
4. Unpriced: other BRP calls (52 min in long episodes at the 5-min split, but the timelines do not name the BRP calls), and the 107 failed shots with no attributable error text (74 min a week). The proposal records each failure's text so the next re-rank can price that group.
5. Dropped: the fit-timeout text. All nine timeouts fell on 2026-10-07 and 08, and there have been none since 2026-10-08 12:48 EDT.

**Deferred:** the shot tool's refusals fix (help text for `--margin` 0–0.45 and a fix hint in the "pose needs focus and radius" and "not inside a git worktree; pass --views-file" refusals) — the user, 2026-10-07: "wait a week on the screenshot refusal fix". Deferred, not declined: when G1 clears, re-rank every candidate, this one included, from a week of data and send natedev the top one. Built as Phase 10 (540c062) after the 2026-10-14 re-rank.

**Re-rank 2026-10-26 (after G1):** report from the 17:10 AST scan (`scan_status.json` → `last_success` 2026-10-26T21:10:47Z). Host coverage: natedev through 17:10 AST (Claude transcripts, Codex transcripts, timings); Mac through 14:06 AST (Claude transcripts, Codex transcripts, timings). Both hosts are covered past G1 (2026-10-22 11:07:01 AST). Mac sources: all three are in, and the report says "Mac Claude transcripts: included", so the 100-file / 100 MB limit no longer keeps them out. The Mac read state is `unavailable`: the 17:10 AST read did not finish, so Mac coverage stays at its last finished read (14:06 AST). The read is not catching up; it is stopped at that time. The Mac gave 0 shot records and 0 episodes in the window (its newest saved episode is 2026-08-20), so every number below is natedev data. Window: 2026-10-19 17:10 to 2026-10-26 17:10 AST (7.00 days), command `shot_report.py report --since 2026-10-19T21:10:47Z`.

Volume fell again. Shot records: 2,071 in the week before Phase 10 (2026-10-08 to 10-15 11:07 AST), 778 in the week after it, 471 in this window (427 successes, 44 failures, 0 stop-signal exits). `/hana_shot` episodes: n=47 at the 5-min split (3.40 h, median 3.8 min, p90 10.1) and n=44 at the 15-min split (median 3.9, p90 11.2). By hand: n=37 at both splits (2.52 h). Kept-shot evidence in the window is mostly inferred from images (exact ordered captures in 3 of 47 `/hana_shot` episodes); no candidate below uses kept shots.

Failure pricing method: each failure costs the time to the same session's next record, counted only when the session reaches a successful shot within 15 min. This counts a run of failures once. (The 2026-10-14 method, time from each failure to the next success, counts a run once per failure; with it the window gives 36.7 min, against 149 min a week on 2026-10-14.) All 44 failures carry `error`, so every text is classified (44 classified, 0 unclassified). The week before Phase 10 has 127 failures, all without `error` (0 classified, 127 unclassified), so text categories are not compared across the measurement change. Totals only: before, 84.4 min over 120 recovered failures (42 s each); the week after (it holds Phase 10 and Phase 11), 44.0 min over 74 (36 s each); this window, 23.9 min over 36 (40 s each). The failure rate went from 6.1% to 10.3% to 9.3% of shot records. The fall in failure minutes comes from the fall in volume; the cost per failure did not change.

Saving rule, as on 2026-10-14: a failure text that names its next step leaves about 10 s of recovery. Saving per failure = max(0, recovery − 10 s).

Ranked by agent-minutes saved per week:

1. **Next-step text for the three failures that still name none.** The window has 15 such failures (all natedev): "the camera did not settle within 5 s" (`hana_shot.py:841`), n=9 in 2 sessions, 156 s; "the target has no visible mesh bounds to crop to" (`hana_shot.py:1242`), n=4 in 1 session, 147 s; "setup X ran but Y did not appear within 5 s" (`hana_shot.py:1078`), n=2 in 2 sessions, 80 s. Cost today: 6.3 min a week. Saving: (156 − 85) + (147 − 13) + (80 − 10) = 70 + 134 + 69 s = 273 s, about 4.6 agent-minutes a week. Over the 11.25 days since Phase 10 the same three give n=46 (settle 23 in 8 sessions, crop bounds 21 in 3, setup 2) and 11.4 min a week. Constraint note: Phase 10's goal was that every failure names its next step, and these three were left out. This is not a new text for a failure Phase 10 already covers. The showrunner decides if this counts as a proposal or as a repair of Phase 10. The showrunner ruled it a repair of Phase 10; built as Phase 13 (b208263a).
2. **The parked speed plan.** 584 successful attempts in the window, total_ms median 321 ms (n=584), 6.5 min of in-tool time in all. A 30% cut saves at most 6.5 × 0.3 = 2.0 agent-minutes a week. This is per-shot time only, not episode minutes.
3. **Not proposed again (Phase 10 texts), for the record:** rejected fits n=10 (median recovery 15 s), view-name, margin and selector refusals n=5, stale `entity:` ids n=2. Together they cost 3.3 min and could save 1.3 min a week. Rejected fits now recover in about 15 s each with the Phase 10 hint.
4. **Hana-frame settle:** n=1 attempt (settle 774 ms). Removing the whole settle saves 0.01 min a week.
5. **Magick crops moved to extras' rect:** n=0. All 92 rect crops in the window used extras (crop_ms 0, n=92); no attempt used magick. Saving 0.
6. **Dropped again: the fit-timeout text.** `failure_reason: timeout` n=0 in the window; none since 2026-10-08 12:48 EDT.

Unpriced (below every priced one):

7. **Other BRP calls in long `/hana_shot` episodes.** The report gives them 28.8 min, 25.0% of long minutes at the 5-min split (n=12 long episodes), and 33.5 min, 23.8% at the 15-min split. That number is too high. `parsed_tool_event_category` (`scripts/shot_report/transcripts.py:45`) tests `\b(?:world\.|bevy/|hana/|brp_extras/)` before it tests file commands. Paths such as `crates/hana/src` match, so `grep` and `ls` over the Hana tree count as BRP calls. A pass over the transcripts moved those calls to file work. Long episodes, 5-min split: other BRP falls to 19.6 min (17%) and file work rises from 11.2 to 20.4 min (18%). At the 15-min split: 20.4 min (14%) and 27.0 min (19%). Agent time stays the largest category, 54–56%. The same pass names the remaining BRP time (27.3 min over all 47 episodes, 5-min split): scripted input with `sleep` (mouse moves, clicks, `hana/invoke_command`), `world.query` and `world.get_components` look-ups, and by-hand `brp_extras_screenshot` calls inside `/hana_shot` episodes. No single change has a measured effect on this time, so it stays unpriced. The regex is a defect in this unit's report and is fixed where it lives, as a measurement change. It is not a speed gain.
8. **New: TUI screenshots in cargo-liner.** 26 by-hand episodes in the window (source `import`, natedev Claude), 1.51 h at both splits, median 2.7 min. The cargo-liner shot subagents start the TUIs in xterm through a pty relay that each agent writes again, send keys through a FIFO, then shoot with ImageMagick `import` and crop by character cell. No tool exists for this, so the report has no measured per-episode gain to apply. This week the Hana split gives none: `/hana_shot` median 3.9 min (n=35) against 3.4 min by hand (n=9). The setup before each subagent's first shot is not in the report (median about 1.5 min over 19 subagents, from transcripts). The newest episode is from the "ashots-32-final" subagent on 2026-10-25, so this work may be ending.
9. **New: agent-written drivers that import `hana_shot` and raise its `Failure`.** n=3 in 1 session ("Chooser did not open without a device pick", `output_crowded`). They cost 13.4 min, and one 13.1 min gap is most of it. The text comes from the agent's own script, so no tool change applies.
10. **By-hand Hana interaction shots.** 11 episodes (1.01 h): curl or MCP `brp_extras` input followed by a screenshot. `/hana_shot` shows no measured per-episode gain over them this week (see 8).

**Declined, still declined:** one shot tool for any BRP app. The window has 0 by-hand episodes in a BRP app other than Hana, so about 0 minutes a week.

**Not proposed again (Constraints):** failure logging, Codex scanning, kept-shot detection, long-episode attribution, scale-aware `--window`, the window-level restore, the Phase 10 failure texts, `face`, the launch warning.

**Measurement notes (not candidates; never counted as speed gains):**
- `transcripts.py:45` counts Hana source paths as BRP calls (item 7). Fixed in Phase 13.
- Failure invocation records carry no top-level `mode`, `target` or `view`. All 44 in the window have them empty, and only the attempt `label` names the shot. Per-mode failure pricing has to read the label. Phase 13 adds the three keys to new failure records.
- This unit's re-rank session (e6ea3318) added 15 shot records and 4 `.claude` `/hana_shot` episodes (13.7 min) on 2026-10-26. They are counted like any other use.
- The plan's Constraints still say that Mac Claude transcripts stay out above 100 files / 100 MB. The report now says they are included. Correct that text.

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

### Phase 9 — `/hana_shot` leaves no Mac window always on top · status: done

#### As-built

- `Session.window_level` is `WindowLevelPending | WindowLeftAlone | WindowRaised(entity, restore_level)`; nothing outside the tool reads it. `keep_visible` reads the primary window's `window_level` before it writes AlwaysOnTop and records `WindowRaised`. A level read as AlwaysOnTop, or one that cannot be read, restores to Normal: Hana sets no level itself, so AlwaysOnTop is one an earlier run left behind.
- `Session.restore_window_and_end_keep_awake()` puts the level back, then ends the keep-awake. `Session.close()` and a failed preparation both call it. The restore uses `Brp.call`'s default `CALL_TIMEOUT`. A restore that fails prints one stderr line, `hana_shot: could not put the window level back to <level>: <reason>`; the keep-awake still ends and the run keeps its exit code.
- Preparation counts as done only after the raise, the keep-awake start and the frame wait all succeed, because a session marked prepared early stops `views check` from preparing later views. On a failure or a signal it undoes itself and the state returns to `WindowLevelPending`, so the next view prepares again.
- `ExitSignalLatch`: the first SIGINT, SIGTERM or SIGHUP raises `ExitSignal` (a `BaseException`) once, and `main` exits with 128 + the signal number (130, 143, 129), with no traceback and with the timings line and the invocation record written. Later signals, and any that arrive once `Session.close()` has begun, do not interrupt. A signal during a failed preparation's rollback is delivered after the rollback. The latch states are `ExitSignalsEnabled`, `ExitSignalsDeferred`, `DeferredExitSignal` and `ExitSignalsLatched`.
- A local macOS run takes the same cleanup path: it keeps its `caffeinate` handle and terminates it at cleanup.

**Files:**
- `scripts/hana_shot/hana_shot.py` — the level read, the session window state, the shared cleanup, the signal latch and handlers
- `commands/hana_shot.md` — items 6, 8 and 9 state the restore, the stale-level repair and the signal exit codes
- `scripts/hana_shot/test_hana_shot.py` — the BRP fake's window level, a one-shot raise refusal, a held screenshot, generated driver subprocesses that wrap `Brp.mutate` and `Session.close`; exit-path cases for success from Normal, AlwaysOnBottom and AlwaysOnTop, a failed remote copy, SIGINT, SIGTERM, SIGHUP, a signal during cleanup, a second signal during the unwinding, `views check` retrying preparation, a refused restore

**Binds later work:** **Proposals: the next change, ranked by measured minutes saved** reads invocation records, where a run ended by a stop signal is a failure with exit code 130, 143 or 129 and `failure_reason: shot_failed`. `changes.json` carries this change as a product change for natedev and the Mac, effective 2026-10-07 10:08 PDT.

**Gotchas:**
- A signal handler that raises can fire inside cleanup, so `closing(...)` alone does not protect the restore; the latch engages before cleanup starts.
- SIGKILL cannot be caught; the next run's AlwaysOnTop-to-Normal rule repairs the level it leaves.
- The local macOS branch cannot run on Linux; no test exercises it, only a live Mac run.

**Ruled out:**
- `SIG_IGN` during cleanup — child processes inherit an ignored disposition; a latch the handler checks does not leak.
- A `timeout` parameter on `Brp.mutate` — it only repeated `Brp.call`'s default.
- Leaving Ctrl-C on the `KeyboardInterrupt` path — one path for all three signals gives one rule and one exit-code scheme.


### Phase 10 — Every `/hana_shot` failure names its next step, and the screenshot report follows the user's zone · status: done

#### As-built

- A rejected fit names `shot --target <quoted label> --mode pose --focus 0,0,0 --radius 6` (a stored `--view` ignores pose flags) or a fit on a parent with geometry. It also names the nearest cached ancestor with an AABB as `--target entity:<id> (<name>)`, from `TargetEvidence`: names, parents and the AABB set, observed from queries the run already makes. An explicit `has[AABB]` false drops an entity.
- A target that finds nothing: an `entity:` selector says ids change when Hana restarts; `name:`/`tool:` lists up to five closest names or kinds (`difflib`). An ambiguous target lists `entity:<id> (<name>)` per match.
- The margin refusal and `--margin` help state the range 0 to 0.45 and "fraction of the viewport kept clear on each side (0.1 = 10%)". The pose refusal names the missing flags with an example. View-name refusals (`view_name_refusal`) suggest a valid name built from the refused one.
- Every failed invocation and failed attempt record carries `error`: one string, several failures joined by newlines; success records have none, and old records without it still read. `CaptureFailure(reason, error)` replaces the bare reason list. Argparse errors reach the record through `RequestArgumentParser`/`ArgumentExit`. A signal exit appends "terminated by SIG…" after earlier failures, so the first real `failure_reason` stays; an unhandled exception appends its type and text, then re-raises.
- `/shot_report`: `_user_time` prints in `user_zone.conf`'s zone, the weekly heading names the zone, and baseline lines keep the instant 2026-09-09T07:00Z but print it in the user zone. No fixed zone name remains in either script dir or the two command docs. `changes.json` holds a product change and a measurement change for this, on natedev and the Mac.

**Files:**
- `scripts/hana_shot/hana_shot.py` — failure hints, `TargetEvidence`, `CaptureFailure`, `view_name_refusal`, `RequestArgumentParser`/`ArgumentExit`, `error` on failure records
- `scripts/hana_shot/test_hana_shot.py` — a test per hint and per record path, against the fake BRP server
- `commands/hana_shot.md` — lists the `error` field and the margin meaning
- `scripts/shot_report/shot_report.py` — `_user_time`, zone-named weekly heading and baseline lines
- `scripts/shot_report/test_shot_report.py` — zone tests with a pinned `USER_ZONE`
- `commands/shot_report.md` — no fixed zone name

**Binds later work:** the re-rank in "Proposals: the next change, ranked by measured minutes saved" reads failure texts from the records' `error` field, not transcripts, and compares the weeks before and after this change.

**Gotchas:** the hints that need live Hana are covered by the fake BRP server only. The fake server projects only requested components, so a test that depends on parents needs the query to ask for `ChildOf`.

### Phase 11 — A view can face its target along the target's own axis · status: done

#### As-built

**Source:** a named view could not shoot a tilted panel face-on, because view yaw and pitch are world angles only.

- View key `face` and `shot` flag `--face` take one of `FACES` (`+x`, `-x`, `+y`, `-y`, `+z`, `-z`), a local axis of the target; the camera sits on that side and looks back along it. `face_angles(matrix, axis) -> (yaw, pitch)` takes the axis column of the target's `GlobalTransform`, negates it for `-`, normalizes it (scale has no effect), and returns `yaw = atan2(d.x, d.z)`, `pitch = asin(d.y)` in Hana's orbit convention.
- `request_orientation` turns the given flags into `DefaultAngles`, `ExplicitAngles` or `TargetFace`; `resolve_face` reads the target's `GlobalTransform` over BRP at shot time and yields `ResolvedTargetFace`. Only `take` reads these states, and its move key uses the angles derived on this run, so a face view still moves when the target has turned.
- `fit` passes the derived angles to `AnimateToFit` with margin as before. `pose` applies radius, and its focus defaults to the target's world translation; an explicit `--focus` wins.
- Refusals, each naming its next step on stderr and in the invocation record's `error`: `face` with an explicit yaw or pitch; `face` with `hana-frame` or `home` (names `fit` and `pose`); `face` without a target or, in pose, without a radius; an axis within 1° of vertical (suggests `--yaw 0 --pitch 1.55` or `-1.55`); `views add --from-current --face`.
- `views add` stores `face` without yaw or pitch; `views show` prints it; `views_check` counts a face refusal as that view's failure and continues to the next view. `pose get/set` is unchanged.
- Invocation and attempt records carry `face` as an optional field when a shot used it.

**Files:**
- `scripts/hana_shot/hana_shot.py` — `FACES`, `face_angles`, the orientation states, `request_orientation`, `resolve_face`, the face refusals, `views_check`
- `scripts/hana_shot/test_hana_shot.py` — `face_angles` round trips over 24 transforms (all six axes on identity, yawed, pitched, tilted and scaled), fake-server fit and pose face shots, the refusals, `face` on success and failure records, stored face views through add/show/check, `views check` continuing past a refusal
- `commands/hana_shot.md` — `face`/`--face` docs and the example `face = "+z"` on `tool:FrameTimeInfoPanel`

**Binds later work:** the screenshot proposals re-rank prices face shots as ordinary shots and does not propose the face option as a speed candidate; records carry `face` when a shot used it.

**Gotchas:**
- Hana's orbit pitch clamp keeps the camera from a vertical face axis, so that axis is refused.
- The trunk Frame time panel faces local +z and is only yawed (2.4°), not tilted; the tilted case is proven by the unit tests, not live.
- The fake BRP server returns only requested components, so a test reading the target's `GlobalTransform` must request it.

### Phase 12 — `/hana_shot launch` says when its binary holds uncommitted code · status: done

#### As-built

**Source:** a live run rebuilt `hana` from a worktree with another phase's uncommitted `crates/` edits, and nothing said so (showrunner, 2026-10-19).

- `uncommitted_build_paths(root, executable) -> list[str]` runs one `git -C <root> status --porcelain=v1 -z --untracked-files=all -- crates/ Cargo.toml Cargo.lock` (3 s timeout, stdin closed) and returns sorted repository-relative paths. With the binary present: each modified, added, untracked or renamed-to file whose mtime is at or before the binary's, plus each deleted or renamed-away path (its removal cannot be dated). With the binary missing: every dirty path under `crates/` and each dirty root `Cargo.toml` or `Cargo.lock`. A git failure, a timeout or a non-git root returns `[]`, so the check never blocks a launch.
- `launch` calls it once after it resolves the executable; every launch path (`launch`, and `--launch` on `shot` and `views check`) goes through `launch`. Binary present and paths found: stderr gets `hana_shot: warning: <binary> was built after uncommitted edits in crates/ or the root Cargo files, so this Hana runs that code: <paths>` before the start, and the launch continues. Binary missing: the existing refusal adds `; uncommitted edits in crates/ or the root Cargo files will be included by a build: <paths>`. `build_paths_label` names at most 10 paths, then `, and N more`.
- `LaunchState` has an optional `uncommitted_paths: list[str]`, saved only when non-empty and holding every path. No other command changes and no refusal is added.

**Files:**
- `scripts/hana_shot/hana_shot.py` — `uncommitted_build_paths`, `build_paths_label`, the launch warning and refusal hint, `LaunchState.uncommitted_paths`
- `scripts/hana_shot/test_hana_shot.py` — `LaunchBuildWarningTests`: temporary git repositories with a fake binary and `os.utime` mtimes (clean, older/equal/newer, untracked with unusual names, staged, deleted, renamed, path scope, missing binary, non-git, git timeout, root `Cargo.lock` and `Cargo.toml`, a `Cargo.toml` outside the root), the missing-binary refusal, and the capped warning before a stubbed `Popen`
- `commands/hana_shot.md` — one sentence under launch

**Binds later work:** the screenshot proposals re-rank treats the launch warning as no shot behavior change; it has no `changes.json` entry and is not a candidate.

**Gotchas:**
- In `-z` output a rename or copy record is followed by a separate original-path record; the parser consumes it so it is not read as a status entry.
- The mtime test is a heuristic, not proof of what the binary holds. Only `crates/` and the root `Cargo.toml` and `Cargo.lock` are checked (the root files added at e031f5e9).
- Tests never start Hana: the natedev Hana ban holds, so live launch is untested.

### Phase 13 — The last three `/hana_shot` failures name their next step, and failures can be priced by mode · status: done

#### As-built

**Source:** the 2026-10-26 re-rank found 15 failures a week with no next step; the showrunner ruled it a repair of Phase 10's promise and made the two measurement fixes current (2026-10-26).

- `next_step(action) -> str` returns `"; next step: <action>"`, so the three texts read alike. Each text is one `Failure` string, so it reaches the record's `error` unchanged.
- Settle timeout (`Session.settle`): "the camera did not settle within N s", then rerun once; if it fails again, read the pose with `hana_shot.py pose` and shoot with `--mode pose --focus x,y,z --radius N` (no fit move).
- Setup target missing (`run_setup`): "setup S ran but T did not appear within N s", then `closest_target_hint(evidence, text)` (up to five `difflib` names or kinds for `name:`/`tool:`, shared with the target-not-found text in `resolve_one`), then "run `hana_shot.py targets` to list the selectors that resolve now".
- No mesh bounds for the crop: `hierarchy_rect(bounds, root, state, padding, evidence)` adds `TargetEvidence.parent_fit_hint(root)` when an ancestor with an AABB is known, then "rerun with --crop none to shoot without a crop".
- `InProgressCaptureInvocation.shot: Shot | UnparsedShotRequest`; `command_shot`, `run_shots` and `views_check` set it to the shot in progress. A failed invocation record carries top-level `view`, `target` and `mode` only when a shot parsed; a parser error omits them. Old records without them still read.
- `scripts/shot_report/transcripts.py`: `_BRP_METHOD_NAME` counts `hana/<name>` as `other_brp` only when it is not a path segment (no word, `/`, `.` or `-` next to it). Escaped-quote curl bodies, `rpc("hana/…")` and `mcp__brp__*` tools count; `rg`/`cat` over `crates/hana/src` is file work. `CACHE_FORMAT_VERSION` is 7.

**Files:**
- `scripts/hana_shot/hana_shot.py` — `next_step`, `closest_target_hint`, the three texts, `UnparsedShotRequest`, `view`/`target`/`mode` on `FailedInvocation`
- `scripts/hana_shot/test_hana_shot.py` — fake-server tests for the settle, setup and crop texts (known and absent ancestor), `error` on invocation and attempt records, failure-record fields for stored and ad hoc shots, omission on a parser error
- `commands/hana_shot.md` — one line: failure records carry `view`, `target` and `mode` when the request parsed to a shot
- `scripts/shot_report/transcripts.py` — `_BRP_METHOD_NAME`, cache version 7
- `scripts/shot_report/test_transcripts.py` — path forms are file work; compact, spaced and escaped JSON, `rpc(...)` and MCP forms are BRP; a version-6 cache re-parses

**Binds later work:** the screenshot proposals re-rank does not propose the three texts again and prices their recovery before and after the product change. After the measurement change, per-mode failure pricing reads top-level `mode` on failed records; before it, the attempt `label`. Other-BRP and file-work minutes from earlier re-ranks do not compare with later reports. The product and measurement `changes.json` entries, on natedev and the Mac, go in when this change reaches the live `~/.claude`.

**Gotchas:**
- The first scan after landing re-parses local transcripts once; the Mac mirror and its cursors do not change.
- `--settle` takes integer extra frames, not seconds.
- Tests never start Hana: the natedev Hana ban holds, so the texts are proven against the fake BRP server only.

### Phase 14 — A stopped `/hana_shot launch` never blocks the next launch or `shutdown` · status: done

#### As-built

- `_write_launch_state(state_path, state)` writes the launch record atomically. It writes a temp file `.<name>.*.tmp` in the same folder, flushes and fsyncs it, then calls `os.replace`. On any `BaseException` before the replace, it removes the temp file. A stop leaves no record or a whole record. `LaunchState` and its fields are unchanged.
- `_read_launch_state(state_path) -> LaunchRecord`, where `LaunchRecord = AbsentLaunchRecord | UnreadableLaunchRecord(reason) | WholeLaunchRecord(state)`. Bad JSON, a non-object, a missing `pid`/`binary`/`log`/`scratch`, a non-positive `pid` or an empty string field gives `UnreadableLaunchRecord`.
- `shutdown(port)` uses the newest whole temp record (by mtime) when the main record is absent or unreadable. It then calls `brp_extras/shutdown`. With a whole record, it waits up to 20 s on the pid, kills the process group only when `same_binary` matches, prints panic lines and removes the scratch. With no whole record, it waits up to 20 s for the port to go quiet. If the port still answers, it raises `Failure` ("… still answers after 20 s without a whole launch record; next step: run shutdown again on port N after the launch finishes starting") and keeps the records. Otherwise it removes the record and all temp records.
- Stderr lines: `hana_shot: launch record <path> is unreadable (<reason>); asked Hana on port <port> to stop and removed the record; check scratch parent <tmpdir> for hana-shot-<port>-* folders`. `hana_shot: Hana on port <port> shut down` prints only when Hana answered the stop or a whole record was found. Otherwise: `hana_shot: no Hana answered on port <port>; if a launch on this port is still starting, run shutdown again when the next launch reports the port in use`.

**Files:**
- `scripts/hana_shot/hana_shot.py` — atomic record write, three-state reader, recovering `shutdown`.
- `scripts/hana_shot/test_hana_shot.py` — `LaunchRecordRecoveryTests` (rerun tests); the fake BRP server has an opt-in that closes its listener on shutdown, so relaunch tests use the real port guard.

**Binds later work:** `launch` and `shutdown` write no invocation records, so this change has no `changes.json` entry and no direct report measurement.

**Gotchas:**
- Never delete a temp record before reading it. After a stop between fsync and replace, it is the only pid record.
- A stop between `Popen` and the temp write leaves no pid. `shutdown` reports no answer, and the next launch names "run shutdown again".
- A test that patches `port_answers` while a fake server listens does not prove relaunch; close the fake listener instead.

**Ruled out:** `LaunchState | None` plus a reason as the reader result (a three-state union instead); deleting temp records without reading them first (it can destroy the only pid record); killing a process group that no whole record identifies.
