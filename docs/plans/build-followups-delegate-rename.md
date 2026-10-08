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

#### As-built

Every message, `--help` text, comment and docstring under `scripts/` that names a run calls it a "unit run", never a "delegate run" (user rule, 2026-10-08). The three spellings in use are `Unit run`, `unit run` and `mid-unit-run`.

- `end_session.sh` prints "Unit run ended; marker cleared." or "No active unit run marker for this session." once at a run's end. The wording carries no contract: nothing parses it and no test asserts it.
- `unit_notifier.sh` writes "no active unit run marker: <path>" or "empty unit run marker: <path>" to stderr when a session's run marker is missing or empty. `/unit:report on|off` shows them, and `test_delegate_check.py` asserts both.

**Files:**
- `scripts/delegate/end_session.sh` — header comment and the two end-of-run messages
- `scripts/delegate/unit_notifier.sh` — header comment and the two marker errors
- `scripts/delegate/test_delegate_check.py` — the two `assertIn` strings that match those errors
- `scripts/delegate/phase_table.py` — module docstring and the `--help` description, "Show or refresh a unit run's plan phases."
- `scripts/delegate/progress_history.py` — one docstring line
- `scripts/hooks/context_usage.py` — one docstring line and one comment
- `scripts/hooks/delegate_run.py` — one docstring line ("mid-unit-run")

**Gotchas:** Two internal names keep the word: the `delegate-run` fallback branch slug in `scripts/delegate/style_branch.sh` and a temporary directory's name in `scripts/delegate/test_delegate_check.py`. `git grep -niE 'delegate[ -]run' -- scripts` prints exactly those two lines. The search matches a space or a hyphen only, so the names `scripts/delegate/` and `delegate_run.py` are outside it and are unchanged.

## Source

2026-10-08 PDT

- The user, relayed by the showrunner into this session: "workflow scripts should say unit run not delegate run".
- The showrunner (a peer's coordination message, not the user's words): build it as a small phase now and send the hash.
