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
- **The user approves every improvement** (user): after Phase 3 the unit only measures and proposes, until a proposal is approved.
- **`scripts/hana_shot/` is this unit's to change** within this production. The hana showrunner may still land urgent fixes on `~/.claude` main. Merge `origin/main` into the unit branch before each phase.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. This plan builds the screenshot episode report (Phase 1), records every `/hana_shot` call with failures and the kept shot (Phase 2), runs the report hourly with each change's effect (Phase 3), then proposes improvements (Phase 4). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-screenshot` on branch `build-followups-screenshot` (unit `screenshot-unit` of production `build-followups`).
- **Project started:** 2026-10-06T20:30:00+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/shot_report/` — transcript scan, episodes, history, report (new)
  - `commands/shot_report.md` — `/shot_report [--since DATE] [--project NAME]` (new)
  - `scripts/hana_shot/hana_shot.py`, `test_hana_shot.py`, `commands/hana_shot.md` — Phase 2 recording
  - the `buildlog hourly` call site in `scripts/buildlog/` — Phase 3 (one call, like `rust_release.py`'s)
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

### Phase 1 — One command shows how long an agent takes to get a framed shot, by hand vs `/hana_shot`, in every project · status: todo

#### Work Order

**Goal:** `/shot_report` prints, for a window, episodes per method (by hand, `/hana_shot`) × agent (Claude, Codex) × project. For each it gives the count, total hours, the median, p75 and p90 minutes, and the shots and other BRP calls per episode. The 5-min and 15-min splits are shown side by side.

**Spec:**
- Port `shot_episodes.py` and `shot_now.py` into `scripts/shot_report/`, typed and tested.
- Add Codex transcripts and BRP screenshots sent from Bash (curl or python posting BRP's screenshot method).
- Survey every project's transcripts for other screenshot commands (grim, spectacle, `screencapture`, `import`, Playwright, browser MCP tools). List what is found with counts in the As-built, and count the ones with more than 10 calls.
- Write `~/.local/state/screenshot-analysis/episodes.jsonl`, one record per episode, so later phases compare windows without rescanning.

**Acceptance gate:**
- Tests green; basedpyright 0/0.
- On real data, the by-hand window Sep 9 to 2026-10-06T16:35Z reproduces the seed's 519 episodes and 48 h at the 5-min split within 2%. Explain any larger gap in the As-built before the phase closes.
- Control: one fixture episode per method and agent appears in a fixture run's output.

### Phase 2 — Every `/hana_shot` call is recorded, failures and the shot the agent kept included · status: todo

#### Work Order

**Goal:** failures and attempts-to-usable-shot become measurable. Today only successful shots are logged, and an episode assumes its last shot was kept.

**Spec:**
- `hana_shot.py` writes one `timings.jsonl` line per call, success or not, carrying:
  - the exit code;
  - a reason for every failure exit, with one fixed slug per cause it can name: timeout, already in progress, black capture, empty crop, no app, and the like;
  - the session ID when one is in the environment.
- `stats` counts failures by reason.
- In the episode scan, a shot is kept when its path appears later in the same transcript in an outgoing message, a checkpoint notice or a file the agent wrote. An episode then reports attempts before the first kept shot.
- Old lines without the new fields still parse.

**Acceptance gate:**
- Tests green, with a fake BRP server for each failure path.
- After promotion, the unit reads the first real lines from other sessions' calls and quotes one success and, if any occurred, one failure in the As-built.

### Phase 3 — The report runs every hour and shows what each change did · status: todo

#### Work Order

**Goal:** the regime runs without anyone asking, and every `/hana_shot` change gets a before and after.

**Spec:**
- `buildlog hourly` calls the scan. It is incremental and runs once per hour even when the job runs more often.
- A change list in `~/.local/state/screenshot-analysis/changes.json` holds, for each change, its time, its commit and one line on what it changed. Seed it with the commits in What exists today and the hana-side changes in `analysis.md`. Phase 4 builds append to it.
- `/shot_report` adds a by-change table: episodes, median and p90 between consecutive changes. It flags a window with fewer than 20 episodes as too small to judge.
- It also adds a weekly trend line for by-hand vs `/hana_shot` agent-hours.
- The Mac:
  - Codex transcripts are read over `ssh mac`; print `rc=$?` in the command.
  - Claude transcripts are counted only if a cheap path exists. Otherwise the report states that they are out.

**Acceptance gate:**
- Two consecutive hourly runs add new episodes and leave no duplicates.
- A run with nothing new finishes in under 30 s.
- The by-change table shows the rollout's before and after at both splits.

### Phase 4 — Proposals: the next change, ranked by measured minutes saved · status: todo (standing)

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
  - the expected saving and how Phase 3's table will show it;
  - the cost to build.
- Then wait. natedev brings it to the user.
- An approved proposal becomes the next numbered phase, appended to this plan. Its As-built names the change list entry that measures it.
- A declined one is recorded here with the user's reason. The next proposal goes only when natedev asks.
- After each approved phase merges, re-rank with fresh data before proposing again.

**Acceptance gate:** each proposal cites report numbers from within the last 24 h.
