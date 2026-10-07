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
  - The scan only reads transcripts. It never writes or moves one.
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

#### Work Order

**Goal:** a screenshot taken by a script the agent wrote earlier counts. Today a script written in one call (the Write tool, or a `cat >`/`tee` heredoc) and run in a later call is invisible: the later call holds only the script's path, so the report undercounts by-hand time in the projects that script their shots.

**Spec:**
- Within one transcript, remember each file a call writes whose content takes a screenshot or makes a related BRP call by Phase 1's executing forms: a Claude `Write` (and `Edit` of a file already remembered), a Codex `apply_patch` add or update, and a `cat >`/`cat >>`/`tee` heredoc in a shell command.
- A later shell call that runs a remembered file — `python3 <path>`, `bash`/`sh`/`zsh <path>`, `./<path>`, `source`/`. <path>`, a wrapper such as `timeout 580 <path>`, by full path or by name relative to the call's directory — is a shot call (or a related call) with the file's source. A remembered file that runs another remembered screenshot file is itself a screenshot file.
- A scripted shot call's image count is the distinct `.png` paths in its result; with none, the number of screenshot calls in the file, at least 1. Apply the same rule to a single shell command that takes several shots (a loop, a heredoc), which counts one image today.
- In the Claude reader, a `tool_result` line is skipped only when none of its `tool_use_id`s is pending, not just the first.
- A cwd under a Claude scratchpad (`/tmp/claude-<uid>/<encoded project dir>/<session>/scratchpad/…`) belongs to the project its encoded directory names (`-home-natepiano-rust-widget-enhancements` → that repository, worktrees folded), not to a project called `scratchpad`. Phase 1's real report shows 8 such episodes, 1.04 h.
- The full scan took 48 s in the repair seat and 98 s in the unit director's default run on 2026-10-06; find what varies and hold it at 60 s or less.
- Probe of the real transcripts on 2026-10-06 (loose match): 87 scripts written with a screenshot call, up to 487 later runs of them. State the measured counts in the As-built.
- Both readers filter rows before classifying (Claude skips tool uses without a screenshot hint; Codex skips calls the classifier does not recognise). Read Write, Edit, `apply_patch` and path-only shell runs before those filters, or a later run that holds only a path never reaches the linker.
- Track each remembered script by normalized path and its current content: an append, replacement or deletion updates it, so a run counts only the calls the file holds at that moment. Classify Python content with `_code_result` and shell content with `_shell_classification`.
- Project attribution is a named state — resolved repository, decoded from a scratchpad path, or last-path fallback — replacing `ToolCall.project_is_fallback: bool`.
- A heredoc script written and run in the same shell call already counts (Phase 1); the new tests cover runs in a later call.

**Files:**
- `scripts/shot_report/transcripts.py` — remembered script files per transcript; script-run detection; image counts for scripted shots
- `scripts/shot_report/test_transcripts.py` and `scripts/shot_report/fixtures/classify/` — tests and samples

**Seats:** 1 writer + 1 tester — the test lane is `scripts/shot_report/`, and the Spec names each write and run form.
- `impl` — `transcripts.py`; runs the real-data probe and one scan
- `test` — `test_transcripts.py` and `fixtures/classify/`: a Write then a later run, a `cat >` heredoc then a later `./` run, a script sourcing another screenshot script, a written script never run, a loop taking three shots, and a two-result line

**Constraints from prior phases:** Phase 1's executing forms in `_shell_classification` decide whether a file's content takes a screenshot; reuse them, never a second classifier. Tests never read real transcripts. The full scan stays at 60 s or less.

**Acceptance gate:**
- Tests green; basedpyright 0/0.
- On real data, the probe prints scripts remembered, later runs counted, and the by-hand change at the 5-min split; a sample of 20 counted runs read by hand are all runs of a screenshot script.
- The seed's Claude MCP subset still gives 519 episodes / 47.65 h.
- A full scan takes 60 s or less warm on natedev, printing the candidate-file count and bytes read; the As-built names the measured cause of the 48 s vs 98 s difference.
- Tests cover `episodes.jsonl` and `survey.json` after scripted runs are counted, and a script whose screenshot call was removed before a later run.

### Phase 3 — Every `/hana_shot` call is recorded, failures and the shot the agent kept included · status: todo

#### Work Order

**Goal:** failures and attempts-to-usable-shot become measurable. Today only successful shots are logged, and an episode assumes its last shot was kept.

**Spec:**
- Define two records: an invocation (one `hana_shot.py shot` run, `--view all` included) and a shot attempt (one view's capture), with partial failures; `views check` logs only after its black-image and empty-crop checks. Successful, failed and old-format records are named variants, and session and kept-shot evidence are named states, not bare `str | None` fields.
- Store each attempt's image paths so a later path citation in the transcript links to one attempt; the report shows cited, several cited, none cited, and no observable path.
- `hana_shot.py` writes one `timings.jsonl` line per call, success or not, carrying:
  - the exit code;
  - a reason for every failure exit, with one fixed slug per cause it can name: timeout, already in progress, black capture, empty crop, no app, and the like;
  - the session ID when one is in the environment.
- `stats` counts failures by reason.
- In the episode scan, a shot is kept when its path appears later in the same transcript in an outgoing message, a checkpoint notice or a file the agent wrote. An episode then reports attempts before the first kept shot.
- Old lines without the new fields still parse.

**Files:**
- `scripts/shot_report/shot_report.py` and `commands/shot_report.md` — the kept-shot columns in the report
- `scripts/hana_shot/hana_shot.py` — one `timings.jsonl` line per call with exit code, failure reason and session ID; `stats` counts failures by reason
- `scripts/hana_shot/test_hana_shot.py` — a fake BRP server for each failure path
- `commands/hana_shot.md` — the record's fields and the failure reasons
- `scripts/shot_report/episodes.py`, `scripts/shot_report/transcripts.py` — kept-shot detection and attempts before the first kept shot
- `scripts/shot_report/test_shot_report.py` and `scripts/shot_report/fixtures/` — kept-shot fixtures

**Seats:** 2 writers — the call record and the kept-shot scan touch disjoint files, each with tests beside it.
- `impl` — `scripts/hana_shot/hana_shot.py`, `test_hana_shot.py`, `commands/hana_shot.md`
- `test` opens as impl — `scripts/shot_report/episodes.py`, `transcripts.py`, `test_shot_report.py`, `fixtures/`

**Acceptance gate:**
- Tests green, with a fake BRP server for each failure path.
- After promotion, the unit reads the first real lines from other sessions' calls and quotes one success and, if any occurred, one failure in the As-built.

### Phase 4 — The report runs every hour and shows what each change did · status: todo

#### Work Order

**Goal:** the regime runs without anyone asking, and every `/hana_shot` change gets a before and after.

**Spec:**
- An hourly run inside the hour is skipped; the next hour updates. Keep pending results, remembered scripts and open 5- and 15-minute episodes across runs, so a transcript appended across the boundary neither loses nor doubles calls.
- The Mac is a delayed source, as `scripts/buildlog/sync.py` treats it: name the remote command, its timeout, a per-host cursor, and the report's coverage state; test unreachable then reachable.
- `changes.json` stores each change's repository, host coverage and effective time; the by-change table shows each method's count and window at both splits, and marks a side below 20 episodes.
- The report shows the last successful scan time, and exposes `/hana_shot` failure reasons and per-mode timing counts with their windows, so Phase 5 can price candidates.
- Times state the zone and the actual offset (PDT or PST), with a winter-window test.
- `buildlog hourly` calls the scan. It is incremental and runs once per hour even when the job runs more often.
- A change list in `~/.local/state/screenshot-analysis/changes.json` holds, for each change, its time, its commit and one line on what it changed. Seed it with the commits in What exists today and the hana-side changes in `analysis.md`. Phase 5 builds append to it.
- `/shot_report` adds a by-change table: episodes, median and p90 between consecutive changes. It flags a window with fewer than 20 episodes as too small to judge.
- It also adds a weekly trend line for by-hand vs `/hana_shot` agent-hours.
- The Mac:
  - Codex transcripts are read over `ssh mac`; print `rc=$?` in the command.
  - Claude transcripts are counted only if a cheap path exists. Otherwise the report states that they are out.

**Files:**
- `scripts/buildlog/cli.py` — the one `hourly` call to the scan, beside `rust_release.check_release`
- `scripts/shot_report/shot_report.py`, `scripts/shot_report/episodes.py`, `scripts/shot_report/transcripts.py` — the incremental scan cache, the once-per-hour guard, the Mac read, the by-change table and the weekly trend
- `scripts/shot_report/changes.py` — reads and seeds `changes.json`
- `scripts/shot_report/test_shot_report.py` and `scripts/shot_report/fixtures/` — tests
- `commands/shot_report.md` — the new tables

**Seats:** 1 writer + 1 tester — the test lane is `scripts/shot_report/`.
- `impl` — `scripts/buildlog/cli.py`, `shot_report.py`, `episodes.py`, `transcripts.py`, `changes.py`, `commands/shot_report.md`
- `test` — `test_shot_report.py`, `fixtures/` and `scripts/buildlog/test_sync.py` (the hourly call): two consecutive incremental runs with no duplicates, the once-per-hour guard, the by-change table with a small-window flag, the weekly trend

**Acceptance gate:**
- Two consecutive hourly runs add new episodes and leave no duplicates.
- A run with nothing new finishes in under 30 s.
- The by-change table shows the rollout's before and after at both splits.

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

**Acceptance gate:** each proposal cites report numbers from a report less than 24 h old, each number with its window.
