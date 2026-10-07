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

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds the screenshot episode report (Phase 1), counts screenshot scripts run in later calls (Phase 2), records every `/hana_shot` call with failures and the kept shot (Phase 3), runs the report hourly with each change's effect (Phase 4), then proposes improvements (Phase 5). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-screenshot` on branch `build-followups-screenshot` (unit `screenshot-unit` of production `build-followups`).
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

None.

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

#### Work Order

**Goal:** the regime runs without anyone asking, every `/hana_shot` change gets a before and after, and an hour's run never rereads what an earlier run already read.

**Spec:**
- An hourly run inside the hour is skipped; the next hour updates. `buildlog hourly` calls the scan, which is incremental and runs once per hour even when the job runs more often.
- The scan cache keeps, per transcript and at its byte cursor, everything a later read needs: pending tool uses and results, script writes and remembered scripts, session metadata and citation positions. It also keeps raw calls, citations, unlinked timing records and the open 5- and 15-minute episodes. A use and its result split by a cursor still pair, a transcript appended across the boundary neither loses nor doubles calls, and a citation that arrives after an episode was saved still updates that episode.
- Every run reruns the timing join, the kept-path match and the repository fold over cached and new evidence together. Timing records carry an identity across runs, so a repeated join never duplicates an attempt, and a record read before its transcript call stays pending until the call arrives.
- A run with nothing new reads no transcript or timing content: candidate discovery uses cached file identity and metadata, not a content search, and the report states the byte count read.
- Mac Codex transcripts and the Mac's `timings.jsonl` are delayed inputs, as `scripts/buildlog/sync.py` treats a delayed host. Read them over `ssh mac`, print `rc=$?` in the command, bound it with a timeout, and keep one cursor per source host. Keep the last successful coverage time per host and catch up after an outage. Label every episode and timing record by its source host, separately from the `host` a timing record carries for the machine running Hana. Mac Claude transcripts are counted only if a cheap path exists; otherwise the report states that they are out. Test unreachable then reachable.
- `changes.json` in `~/.local/state/screenshot-analysis/` holds, per change: repository, commit, one line on what it changed, effective time and host coverage; a change that only alters what is measured is marked as a measurement change. Seed it with the commits in What exists today and the hana-side changes in `analysis.md`. Builds from approved proposals append to it.
- `/shot_report` adds a by-change table: between consecutive product changes, for the covered repository and host windows only, each method's window, episode count, median and p90 at both splits. A side below 20 episodes is flagged as too small to judge, and a window with missing host coverage is marked incomplete. It also adds a weekly trend line for by-hand vs `/hana_shot` agent-hours.
- The report shows the last successful scan time and exposes `/hana_shot` evidence with its windows: success, failure and legacy-success counts; each of the 11 failure reasons; and per-mode and per-crop counts and phase-time medians over successful attempts. Invocation records are aggregated on their own, so a failure with no completed transcript result stays visible. The record gains an invocation kind, so `shot` and `views check` lines are told apart and an old line reads as an unknown kind; `views check` stays outside shot episodes.
- Each reported attempts-before-first-kept-shot number says whether it is exact (`ExactOrderedCaptureAttempts`) or inferred from images (`AttemptCountInferredFromImages`), and the episode save and reload keep that.
- Times state the zone and the actual offset (PDT or PST), with a winter-window test.

**Files:**
- `scripts/buildlog/cli.py` — the one `hourly` call to the scan, beside `rust_release.check_release`
- `scripts/shot_report/transcripts.py`, `scripts/shot_report/episodes.py` — the incremental scan cache, the cached timing join, evidence provenance in the episode save, candidate discovery from file identity
- `scripts/shot_report/shot_report.py` — the once-per-hour guard, the Mac read, the by-change table, the weekly trend, the `/hana_shot` evidence tables
- `scripts/shot_report/changes.py` — reads and seeds `changes.json`
- `scripts/hana_shot/hana_shot.py`, `scripts/hana_shot/test_hana_shot.py`, `commands/hana_shot.md` — record the invocation kind on new lines and document how an old line reads
- `scripts/shot_report/test_shot_report.py`, `scripts/shot_report/test_transcripts.py` and `scripts/shot_report/fixtures/` — tests
- `commands/shot_report.md` — the new tables

**Seats:** 2 writers — agree the cached evidence contract on the board first; the owner sets are disjoint, with tests beside each slice.
- `impl` — `scripts/shot_report/transcripts.py`, `episodes.py`, `test_transcripts.py`, `fixtures/classify/`; `scripts/hana_shot/hana_shot.py`, `test_hana_shot.py`, `commands/hana_shot.md`
- `test` opens as impl — `scripts/shot_report/shot_report.py`, `changes.py`, `test_shot_report.py`, `fixtures/claude/`, `fixtures/codex/`, `commands/shot_report.md`; `scripts/buildlog/cli.py`, `scripts/buildlog/test_sync.py`

**Constraints from prior phases:**
- `scan_calls(claude_root, codex_root, timings_path)` reads each transcript in full, in a worker pool of up to four processes, one transcript per task, and now also reads `~/.cache/hana-shot/timings.jsonl` read-only through `_timing_invocations`; the scan reads transcripts and timing logs and writes neither. A transcript's remembered scripts (`RememberedScript`, keyed by normalized path, holding the current content) live only inside that read, and a script's content and its removals decide how a later run counts: a run counts as a shot only when its arguments select the screenshot branch of the content at that moment. An incremental scan therefore stores each transcript's remembered scripts, pending uses and results, and citation positions beside its byte offset, so a script written before the cursor still counts a run after it, and a removal before the cursor still stops one.
- After the workers finish, `_recorded_calls` links timing records to calls once, over all calls: by session id plus time (±10 s), present-session records to the call whose end is nearest, absent-session records only when exactly one call overlaps; one call may take several records, in time order. It runs after the repository-name fold of calls from deleted worktrees. Both folds must rerun over the cached and the new calls together, and a call already carrying attempts from an earlier join must not take the same record twice.
- A timing record is appended when a `/hana_shot` call ends, and its transcript result lands moments later, so a record can be read one hourly run before its call.
- `views check` runs write timing records and are not shot episodes; classifying them as shots is a one-line change.
- The Codex session id on an episode is the full five-group thread id from the rollout file name (Phase 1 kept only the last 12-hex group); a Claude main transcript's file stem and `sessionId` equal `CLAUDE_CODE_SESSION_ID`. `hana_shot.py` records `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`, else an absent session.
- The record has success, failure and legacy-success variants; each successful attempt carries its own per-view timing fields; failure reasons are 11 fixed slugs (timeout, already_in_progress, black_capture, empty_crop, no_app, invalid_request, no_target, invalid_png, copy_failed, brp_error, shot_failed). `hana_shot.py stats` already counts successes and failures by reason.
- `episodes.jsonl` is rewritten whole by each scan and saves `kept_shot_state`, `attempts_before_first_kept_shot` and `cited_attempt_count`; it does not save whether the attempt count is exact or inferred.
- Convert nullable timing-file fields into named domain states at ingestion, and represent an unavailable timing source with a named state instead of `Path | None` in the scan API. Name new cache and host-coverage states for the guarantee they give. The mutable `ShotInvocation` recorder in `hana_shot.py` is renamed for its in-progress lifetime and for both commands that use it (`shot` and `views check`), for example `InProgressCaptureInvocation`.
- A full scan takes 34–85 s on natedev depending on machine load (39.6 s quiet in the earlier phase, 41.7 s at load 27–60, up to 85 s at load 250 for the same code), so the 30 s bound for a run with nothing new needs the cache, and 60 s is a quiet-machine bound for a full scan. Reference numbers at the 5-minute split, 2026-09-09 to the rollout cut: 584 episodes / 53.73 h by hand, and the seed's Claude MCP subset 519 / 47.65 h; the earlier 568 / 51.89 h predates script-run counting. A real full scan today reads about 26,100 calls from about 3,050 candidate files (13.9 GB) and saves about 1,208 episodes at 300 s and 900 s.

**Acceptance gate:**
- Two consecutive hourly runs add new episodes and leave no duplicate call, attempt or episode.
- A script written before the cache cursor and run after it counts the run, and one removed before the cursor does not.
- A tool use and result split by a cursor, a timing record seen before its transcript result, and a citation added after an episode was saved each produce the same episodes as a full scan.
- On natedev, a run with nothing new reads zero transcript and timing content bytes, reports that byte count, and finishes in under 30 s.
- Mac unreachable then reachable catches up transcripts and timing records without duplication; the by-change table marks missing host coverage, and it does not present the recording rollout of the earlier phase as a speed gain.
- The by-change table shows the `/hana_shot` rollout's before and after at both splits.
- Fixtures cover success, failure, legacy success, a partial multi-view failure, `views check`, all 11 failure slugs, and exact vs inferred first-kept counts after saving and reloading episodes; every report statistic gives its window and n.

### Phase 5 — Proposals: the next change, ranked by measured minutes saved · status: todo (standing)

#### Work Order

**Goal:** the unit is always ready with the next improvement that saves the most agent time.

**Spec:**
- From the report, rank candidates by agent-minutes saved per week. A candidate the report cannot price ranks below every priced one.
- Seed the candidates from `analysis.md` → Next improvements:
  - what fills long `/hana_shot` episodes;
  - hana-frame settle (787 ms);
  - magick crops moved to extras' rect;
  - the parked speed plan;
  - one shot tool for any BRP app, not only Hana.
- Send natedev the top proposal only. It holds:
  - the measured cost today, with n;
  - what changes, in file:line terms;
  - the expected saving and how Phase 4's table will show it;
  - the cost to build.
- Then wait. natedev brings it to the user.
- An approved proposal becomes the next numbered phase, appended to this plan. Its As-built names the change list entry that measures it.
- A declined one is recorded here with the user's reason. The next proposal goes only when natedev asks.
- After each approved phase merges, re-rank with fresh data before proposing again.

**Files:**
- `docs/plans/build-followups-screenshot.md` — each approved proposal as a new phase, each declined one with the user's reason

**Seats:** none — the unit director ranks and proposes from the report; no code is written until a proposal becomes its own phase.

**Constraints from prior phases:** Failure logging, Codex transcript scanning and kept-shot detection are delivered, so `analysis.md` → Next improvements entry 2, entry 3 and the failure-logging half of entry 1 are not proposed again; the question of what fills long `/hana_shot` episodes stays seeded. The hourly phase supplies source-host coverage and says whether each attempt count is exact or inferred from images. Price weekly agent minutes from an observed eligible weekly count, keep per-shot milliseconds distinct from episode minutes, and leave a candidate unpriced when its evidence cannot support that calculation.

**Acceptance gate:** each proposal cites a report less than 24 h old, the covered window and n for every number, both splits when it uses episode time, exact or inferred attempt evidence when it uses kept shots, and the calculation of expected agent-minutes saved per week. A candidate without those inputs is labeled unpriced and ranks below every priced one.
