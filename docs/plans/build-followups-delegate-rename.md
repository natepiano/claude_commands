# delegate-rename

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The workflow's scripts call a run a "unit run". Phases 1 and 2 (the command is `/unit:direct`; `/unit:delegate` forwards to it) are finished and described in `docs/as-built/agent-registry.md`.

> **As-built disposition: amend** — `docs/as-built/session-notifier.md`

> **Production: build-followups** — unit `delegate-rename-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-delegate-rename`, branch `build-followups-delegate-rename`.
- **Project started:** 2026-10-08T16:34:43.978+00:00
- **Stack:** bash and zsh scripts; Python 3.10+ (stdlib only, `unittest`). basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`.
- **Layout:** `scripts/delegate/` (the scripts `/unit:direct` runs), `scripts/hooks/` (the hooks that read a run's marker).
- **Key files:**
  - `scripts/delegate/end_session.sh` — ends a run and removes its marker; prints one of two messages (lines 56 and 58).
  - `scripts/delegate/unit_notifier.sh` — makes a run's status timer; prints one of two errors when the marker is missing or empty (lines 17 and 22). `scripts/delegate/test_delegate_check.py` asserts both (lines 155 and 162).
  - `scripts/delegate/phase_table.py` — its `--help` description (line 1337) and module docstring (line 2) name the run.
  - `scripts/delegate/progress_history.py` — one docstring line (242) names the run.
  - `scripts/hooks/context_usage.py` (lines 6 and 42) and `scripts/hooks/delegate_run.py` (line 7) — docstring and comment lines that name the run.
- **Test lanes:** none. Tests sit beside the code as `test_<module>.py` (`unittest`).
- **Build:** none (bash, zsh, Python).
- **Test:** `python3 -m unittest discover -s scripts/delegate -p 'test_delegate_check.py'`, the same for `'test_phase_table.py'` and `'test_progress_history.py'`, and `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` (45 s).
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has). `bash -n` and `zsh -n` for the two shell scripts.
- **Invariants:**
  - **Internal names keep the word.** `scripts/delegate/`, `docs/delegate/`, `config/delegate.conf`, `/tmp/claude/delegate`, `PLAN_DELEGATE_*`, `plan-delegate`, the `delegate-<run id>` notifier instance, the `delegate/<plan-slug>` branch, hook and script file names, and `delegate session directory` (the hooks read a line that begins `Delegate session directory:`). "delegate" also stays wherever it means the agent that receives the work.
  - **Never rewrap.** Change the words on a line and leave the rest of it. `phase_table.py` and `progress_history.py` belong to phase-tables-unit, which has unmerged work in them.
  - **Not touched:** `settings.json`, `CLAUDE.md`, `config/agents.conf`, any file under `docs/plans/` but this plan, and every file under `commands/`, `docs/` and `config/`.
  - Forbidden-words hooks apply to code, comments and prose.

## Phases

### Phase 3 — The workflow's scripts say "unit run"  · status: done

#### Work Order

**Goal:** Every message, `--help` text, comment and docstring in `scripts/` that calls a run a "delegate run" calls it a "unit run".

**Spec:**

The user, 2026-10-08: "workflow scripts should say unit run not delegate run".

On each line below, replace the phrase and change nothing else on the line. Keep the capital where the line has one: `Delegate run` → `Unit run`, `delegate run` → `unit run`, `mid-delegate-run` → `mid-unit-run`.

1. `scripts/delegate/end_session.sh` — line 2 (the header comment), line 56 (`echo "Unit run ended; marker cleared."`), line 58 (`echo "No active unit run marker for this session."`).
2. `scripts/delegate/unit_notifier.sh` — line 2 (the header comment), line 17 (`"no active unit run marker: $marker"`), line 22 (`"empty unit run marker: $marker"`).
3. `scripts/delegate/test_delegate_check.py` — lines 155 and 162: the two `assertIn` strings follow the messages on lines 17 and 22 of `unit_notifier.sh`.
4. `scripts/delegate/phase_table.py` — line 2 (the module docstring) and line 1337 (`description="Show or refresh a unit run's plan phases."`).
5. `scripts/delegate/progress_history.py` — line 242 (`a unit run`).
6. `scripts/hooks/context_usage.py` — lines 6 and 42.
7. `scripts/hooks/delegate_run.py` — line 7 (`mid-unit-run`).

Lines that keep the word: `scripts/delegate/style_branch.sh` line 135 (`delegate-run`, a fallback branch slug) and `scripts/delegate/test_delegate_check.py` line 34 (a temporary directory's name). Both are internal names.

**Files:**
- `scripts/delegate/end_session.sh` — lines 2, 56, 58
- `scripts/delegate/unit_notifier.sh` — lines 2, 17, 22
- `scripts/delegate/test_delegate_check.py` — lines 155, 162
- `scripts/delegate/phase_table.py` — lines 2, 1337
- `scripts/delegate/progress_history.py` — line 242
- `scripts/hooks/context_usage.py` — lines 6, 42
- `scripts/hooks/delegate_run.py` — line 7

**Seats:** 2 writers — the split is by language; the test change is two renamed strings in existing assertions, too thin for a tester's lane.
- `impl` — `scripts/delegate/end_session.sh`, `scripts/delegate/unit_notifier.sh`, `scripts/delegate/test_delegate_check.py`
- `test` — opens as impl: `scripts/delegate/phase_table.py`, `scripts/delegate/progress_history.py`, `scripts/hooks/context_usage.py`, `scripts/hooks/delegate_run.py`

**Constraints from prior phases:**
- The command is `/unit:direct` and `commands/unit/delegate.md` forwards to it. Command pages and docs call a run "a `/unit:direct` run"; they do not change in this phase.
- The two hook messages a session shows after its context is compacted already name `/unit:direct`; they do not change.

**Acceptance gate:**
- `git grep -niE 'delegate[ -]run' -- scripts` prints exactly two lines: `scripts/delegate/style_branch.sh:135` and `scripts/delegate/test_delegate_check.py:34`.
- `python3 -m unittest discover -s scripts/delegate -p 'test_delegate_check.py'`, the same for `'test_phase_table.py'` and `'test_progress_history.py'`, and `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green.
- `bash -n scripts/delegate/end_session.sh` and `zsh -n scripts/delegate/unit_notifier.sh` exit 0.
- `basedpyright` on each changed `.py` file reports no error or warning that the phase base does not report.
- `git diff --stat <phase base>` names only the seven files under **Files** and this plan.

## Source

2026-10-08 PDT

- The user, relayed by the showrunner into this session: "workflow scripts should say unit run not delegate run".
- The showrunner (a peer's coordination message, not the user's words): build it as a small phase now and send the hash.
