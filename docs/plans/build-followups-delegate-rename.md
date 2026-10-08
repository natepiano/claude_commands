# delegate-rename

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The unit director's command becomes `/unit:direct`; `/unit:delegate` stays as a real stub file that forwards to it, and every file that names the command says the new name.

> **As-built disposition: amend** — `docs/as-built/agent-registry.md`

> **Production: build-followups** — unit `delegate-rename-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-delegate-rename`, branch `build-followups-delegate-rename`.
- **Project started:** 2026-10-08T16:34:43.978+00:00
- **Stack:** Markdown command and doc files; Python 3.10+ (stdlib only, `unittest`); zsh. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`.
- **Layout:** `commands/` (`commands/unit/` holds this command), `docs/` (`docs/delegate/`, `docs/as-built/`, the two format docs), `config/`, `scripts/hooks/`, `scripts/production/`, `scripts/model_study/`, `scripts/lint/`, `scripts/delegate/`.
- **Key files:**
  - `commands/unit/delegate.md` — today the whole workflow, 1,363 lines. It names itself on line 2 (`description:`), line 5 (`# Delegate`) and line 15 (`**Usage:**`). Phase 1 moves it to `commands/unit/direct.md` and leaves a stub at the old path.
  - `scripts/hooks/session-start-delegate-resume.py` — after a compaction its `CONTEXT` tells the director to re-read the command file by path (line 45).
  - `scripts/hooks/stop-delegate-continue.py` — its `REASON` names the same path (line 56) and is shown on screen.
  - `scripts/hooks/delegate_run.py` — the marker reader both hooks import; only its docstring names the command.
  - `scripts/production/add_unit.py` — `prompt_for` (lines 572–591) builds a unit's launch prompt; lines 584, 586 and 591 name the command. `scripts/production/test_add_unit.py` asserts them on lines 357, 538 and 643.
  - `scripts/model_study/turns.py` — `director_boundary` (lines 164–178) finds where a session became a unit director by the command's name in its transcript. Its cases are in `scripts/model_study/test_turns.py` (lines 250–275 and 454); `scripts/model_study/test_rerun.py` (176, 268) holds old-transcript fixtures.
  - `scripts/lint/lint_config.sh` line 28 and `commands/lint_config.md` line 53 — one table row, written in both files.
  - `scripts/production/test_merge_checkpoint.py` lines 33 and 318 — a frozen copy of a production doc's Owns cell that names `commands/unit/delegate.md` as a path. It stays.
- **Test lanes:** tests sit beside the code as `test_<module>.py` (`unittest`) in `scripts/hooks/`, `scripts/production/`, `scripts/model_study/` and `scripts/lint/`.
- **Build:** none (Markdown, Python, zsh).
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` (45 s), `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'` (23 s), `python3 -m unittest discover -s scripts/model_study -p 'test_*.py'` (8 s), `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` (7 s). All four pass on the base (run 2026-10-08).
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - **The rename rule.** The user chose the name `/unit:direct` and the sweep "the command and the prose people and agents read, leaving internals alone" (2026-10-08). The four clauses are the unit director's reading of that:
    1. The command's name: `/unit:delegate` → `/unit:direct`.
    2. The workflow file's path: `commands/unit/delegate.md` → `commands/unit/direct.md`, and a bare `delegate.md` that means that file → `direct.md`.
    3. The word standing for the command or its run: `delegate run(s)` → `/unit:direct run(s)`; `delegate phase(s)` → `/unit:direct phase(s)`; the titles `# Delegate` and `# Delegate — <x>` → `# Direct` and `# Direct — <x>`. The name takes backticks where the same file backticks command names in prose, and none in a frontmatter `description:`.
    4. Every other use keeps the word. It means the agent that receives the work, or the act of handing it over (`the delegate`, `a codex delegate`, `delegate agent`, `delegate prompt`, `delegate session`, `delegates`, `delegation`, `delegate-ready`, `delegate-sized`, `## Delegation Context`): the new name does not change who implements. Or it is an internal name: `scripts/delegate/`, `docs/delegate/`, `docs/delegate_plan_format.md`, `config/delegate.conf`, `/tmp/claude/delegate`, `~/.local/state/plan-delegate`, `PLAN_DELEGATE_*`, the `[delegate.*]` registry functions and `/agent delegate`, the `delegate-<run id>` notifier instance, the `delegate/<plan-slug>` CI branch, hook and script file names, tag names, `--caught-by delegate`, `--skill plan-delegate`.
  - **The old name is a real file.** `commands/unit/delegate.md` is a regular file, never a symlink: with a symlink Claude Code registered one name for the two paths and `/unit:delegate` answered "Unknown skill" from 09-28 to 10-01 (commit 98fe408). Running unit directors hold the old command text and old launch prompts, so the old name keeps working (the showrunner, 2026-10-08). Removing the stub is not part of this plan (the unit director's scope note).
  - **Never rewrap.** Change the words on a line and leave its length; a longer line is correct. Other units have unmerged work in `commands/unit/` (`eta.md`, `eta_breakdown.md`, `report.md`), `commands/showrunner/`, `scripts/production/` and `docs/production_format.md`, and the showrunner limits each edit there to the lines that name the command (2026-10-08). The unit director applies that to every file.
  - **Not touched:** `settings.json` (the showrunner, 2026-10-08); `CLAUDE.md`, the user's own instruction file, whose line 7 the stub keeps true (the showrunner, 2026-10-08, who put it to the user); memory files outside the repo, which each session owns (the showrunner, 2026-10-08); any file under `docs/plans/` but this plan (each belongs to its unit; build-report-unit's unmerged row for the command file is moved by the showrunner at that merge, per the user 2026-10-08); `docs/delegate_footprint_review.md` (a dated review of the older `/plan:delegate`; the unit director's scope note).
  - **Write nothing to a file that a script can find quickly** (the user, 2026-10-08). No doc, comment or stub gains a list of renamed files or of names that kept the old word.
  - Merge `build-followups` into this branch before each checkpoint; a conflict is resolved here, on the branch that meets it (the showrunner, 2026-10-08). enh-showrunner-unit is editing `scripts/production/add_unit.py` and `commands/showrunner/*.md`, and phase-tables-unit `commands/unit/eta.md`, `eta_breakdown.md` and `report.md`.
  - Before editing a file under `commands/`, read `~/.claude/commands/succinct_style.md`.
  - Forbidden-words hooks apply to code, comments and prose.

## Phases

### Phase 1 — `/unit:direct` runs the workflow and `/unit:delegate` forwards to it  · status: done

#### As-built

`/unit:direct` is the unit director's workflow: `commands/unit/direct.md` holds it whole, and its description, title and usage line name the new command. `/unit:delegate` still works: `commands/unit/delegate.md` is a short regular file that tells its caller the command is now `/unit:direct`, and to read `~/.claude/commands/unit/direct.md` in full and run it from the top with the same arguments (`$ARGUMENTS`). A caller that reaches the old file by path, from a compaction hook or an older instruction, is sent to the same file.

The SessionStart hook for a compacted run and the Stop hook name `/unit:direct` and send the run back to `~/.claude/commands/unit/direct.md`. The `Delegate session directory:` line and the hooks' file names are unchanged.

The model study finds where a unit director's work begins under either name: `DIRECTOR_COMMANDS = ("/unit:direct", "/unit:delegate")` in `scripts/model_study/turns.py`, and `director_boundary` accepts any of them in a `<command-name>` record or in a `human` or `peer` prompt.

**Files:**
- `commands/unit/direct.md` — the workflow
- `commands/unit/delegate.md` — the forwarding stub
- `scripts/hooks/session-start-delegate-resume.py`, `scripts/hooks/stop-delegate-continue.py`, `scripts/hooks/delegate_run.py` — name the new command and file
- `scripts/hooks/test_command_stub.py` — the stub is a regular file and forwards; the workflow's usage line; both hooks name only `commands/unit/direct.md`
- `scripts/model_study/turns.py`, `scripts/model_study/test_turns.py` — both names, with a case for each
- `docs/as-built/director-model-study.md` — the boundary rule names both

**Binds later work:** the sweep of every other file that names the command leaves these files as they are, and keeps the old name only in `CLAUDE.md` and where transcripts from before the rename are read. Launch prompts may name `/unit:direct` only once this workflow file is on `~/.claude` main, where sessions load commands from.

**Gotchas:**
- The stub stays a regular file. A symlink made Claude Code register one name for the two paths, and `/unit:delegate` answered "Unknown skill".
- The commit records no rename, because the old path is still a file; `git blame -C` reaches the workflow's earlier history.
- `basedpyright` exits 3 in this repository with a clean report; read its `0 errors, 0 warnings` line, never its status, and never inside a `pipefail` pipeline.

**Ruled out:**
- Forwarding through the Skill tool: a compacted run and a Codex seat reach the stub by path, and a read of the file serves every caller.
- A symlink in place of the stub.

### Phase 2 — Every file that names the command says `/unit:direct`  · status: done

#### Work Order

**Goal:** Launch prompts, commands, docs and config all name `/unit:direct`; the old name stays only in `CLAUDE.md`, which this plan does not touch, and where old transcripts are read.

**Spec:**

Apply the rename rule (Delegation Context → Invariants) to every tracked file under **Files**. These three searches list the lines; each line gets the clause that fits it, and no line is rewrapped:

- clause 1: `git grep -nI 'unit:delegate' -- . ':!docs/plans'` — about 143 lines in 57 files once `build-followups` with Phase 1 is merged in;
- clause 2: `git grep -nI 'delegate\.md' -- . ':!docs/plans'` — about 22 lines in 13 files;
- clause 3: `git grep -nIiE 'delegate (run|phase)s?\b|^# Delegate( |$)' -- commands docs config ':!docs/plans'`, about 36 lines. One more wraps across a line end: `commands/unit/checkpoint.md` line 11 ends `If no delegate` and line 12 begins `run is active`.

Clause 3 also covers two places where the bare word stands for the command: `commands/implement_issue.md` line 12 (`it runs no delegate` → ``it runs no `/unit:direct` ``) and `commands/unit/eta_breakdown.md` line 9 (`the delegate's own stages` → `` `/unit:direct`'s own stages``).

Lines these searches print that stay as they are:

- `config/agents.conf` line 58 (`A codex delegate runs as a thread`): the agent, clause 4.
- `CLAUDE.md` line 7: not touched (Invariants).
- `scripts/production/test_merge_checkpoint.py` lines 33 and 318: the frozen Owns cell.
- `scripts/production/test_add_unit.py` line 221 (the `enh-showrunner-unit` row of `test_retired_readers_use_marker_on_production_rows`): a frozen copy of a production row; its `commands/unit/delegate.md` stays.
- `scripts/model_study/test_rerun.py` and the existing cases in `scripts/model_study/test_turns.py`: transcripts from before the rename.
- `docs/as-built/director-model-study.md` line 108: it reports how recorded sessions were read.
- `docs/delegate_footprint_review.md`: not touched (Invariants).
- `commands/unit/direct.md`, `commands/unit/delegate.md`, the three hook files, `scripts/hooks/test_command_stub.py`, `scripts/model_study/turns.py`, `scripts/model_study/test_turns.py` and `docs/as-built/director-model-study.md` line 27: finished in Phase 1.

Code, beyond the word swap:

1. `scripts/production/`: `/unit:delegate` → `/unit:direct` on every line clause 1's search prints there: the three launch-prompt strings in `add_unit.py`'s `prompt_for` with the three assertions on them in `test_add_unit.py`, and the lines in `stall_watch.py`, `unit_lookup.py`, `test_stall_watch.py` and `test_unit_lookup.py` that arrive with the merge of `build-followups`. `test_merge_checkpoint.py` stays.
2. `scripts/lint/lint_config.sh` line 28 and `commands/lint_config.md` line 53 carry the same row; both become `/unit:direct phase-end gate`.
3. `scripts/delegate/findings.py` line 4: the docstring's `/unit:delegate` → `/unit:direct`.

**Files:**
- `commands/` — every line the three searches print, but for `commands/unit/direct.md` and `commands/unit/delegate.md`
- `docs/` — every line the three searches print, but for `docs/plans/` and `docs/delegate_footprint_review.md`
- `config/` — `README.md`, `delegate.conf`, `lint.conf`, `clippy.conf`
- `scripts/production/` — every line clause 1's search prints; `test_merge_checkpoint.py` and the frozen row in `test_add_unit.py` stay
- `scripts/lint/lint_config.sh` — line 28
- `scripts/delegate/findings.py` — line 4

**Seats:** 2 writers — the split is by tree; the test changes are renamed strings in existing assertions, too thin for a tester's lane.
- `impl` — `commands/`, `scripts/lint/lint_config.sh` (the row it shares with `commands/lint_config.md`)
- `test` — opens as impl: `docs/`, `config/`, `scripts/production/`, `scripts/delegate/findings.py`

**Constraints from prior phases:**
- `commands/unit/direct.md` is the workflow and `commands/unit/delegate.md` is the stub. Neither changes in this phase, nor do the three hook files or `scripts/hooks/test_command_stub.py`.
- `scripts/model_study/turns.py` reads both names through `DIRECTOR_COMMANDS`; it, `scripts/model_study/test_turns.py` and `docs/as-built/director-model-study.md` line 27 are the places the old name stays.
- The unit director, before dispatch, in this order: the showrunner has said Phase 1 is on `~/.claude` main; `build-followups` with Phase 1 in it is merged into this branch and the three searches are run again on the merged tree; and from an empty scratch directory `claude -p 'Run nothing. From the skills you can invoke, print every name that starts with "unit:", one per line, and nothing else.' --model haiku < /dev/null` prints both `unit:direct` and `unit:delegate`. If `unit:direct` is missing, stop and tell the showrunner: this phase points every launch prompt at that name.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_add_unit.py'`, the same for `'test_stall_watch.py'` and `'test_unit_lookup.py'`, `python3 -m unittest discover -s scripts/model_study -p 'test_*.py'`, `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` and `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green.
- `basedpyright` on every `.py` file the phase changed reports `0 errors, 0 warnings`.
- `git grep -lI 'unit:delegate' -- . ':!docs/plans'` prints exactly `CLAUDE.md`, `docs/as-built/director-model-study.md`, `scripts/model_study/test_rerun.py`, `scripts/model_study/test_turns.py` and `scripts/model_study/turns.py`.
- `git grep -nI 'unit/delegate\.md' -- . ':!docs/plans'` prints only lines of `scripts/production/test_merge_checkpoint.py`, `scripts/hooks/test_command_stub.py` and the one frozen row in `scripts/production/test_add_unit.py`.
- `git grep -nIiE 'delegate (run|phase)s?\b|^# Delegate( |$)' -- commands docs config ':!docs/plans'` prints only `config/agents.conf` line 58, and `git grep -nIiE '\bdelegate$' -- commands docs config ':!docs/plans'` no longer prints `commands/unit/checkpoint.md`.
- `git diff --stat <phase base>` names no file under `docs/plans/` but this plan, and neither `settings.json` nor `CLAUDE.md`.

## Source

2026-10-08 09:20 PDT

The user's words, 2026-10-08 (PDT), in order:
- "okay - if we want to continue with movie industry terms what would be a better name for the delegate skill - and also how much documentation and scripts would need to change to sweep a new name through?"
- The showrunner's summary of the study, which he asked for: "Recommended name: /unit:direct. Recommended sweep: rename the command and the prose people and agents read, leaving internals alone. That is 68 files, at most 277 lines. Main risk: the old name must stay as a real stub file, since a symlink alias failed before."
- On build-report's one unmerged row in delegate.md, the showrunner said: "If the rename goes first, that one row is moved into the new file by hand when build-report merges." The user: "that's what we should do".
- Asked who should do it, the showrunner proposed a new unit: "/unit:direct, the 68-file sweep, old name kept as a stub." The user: "yes call the new unit "delegate-rename"".

The showrunner's notes from the study (check each against the code; they are not the user's words):
- Rename /unit:delegate to /unit:direct: the command file, and the prose that people and agents read (commands, skills, docs). Internals keep their names: the scripts/delegate folder, state paths, hook file names, variable names.
- commands/unit/delegate.md stays as a real stub file that sends its caller to /unit:direct with the same arguments. A symlink alias failed from 09-28 to 10-01. Running unit directors hold the old command text and old launch prompts, so the old name must keep working.
- scripts/hooks/session-start-delegate-resume.py names delegate.md by path.
- scripts/model_study/turns.py must accept both names, since old transcripts hold the old one.
- add_unit.py's launch prompts (prompt_for) and /showrunner:produce and /showrunner:promote_unit name the command.
- Do not touch settings.json. Branch build-followups-build-report holds one unmerged row for delegate.md; leave it to the showrunner at that merge.
- Other units have work in flight in commands/unit (phase-tables-unit: eta.md, eta_breakdown.md, report.md) and commands/showrunner (enh-showrunner-unit). Keep each edit in those files to the lines that name the command.
- Design rule from the user today: write nothing to a file that a script can find quickly.
