# One simplify run per day

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The simplify lens leaves each phase review and runs once a day per codebase.

> **As-built disposition: amend** — `docs/as-built/simplify-lens.md`, `docs/as-built/phase-checks.md`, `docs/as-built/review-regime-trial.md`

> **Production: sdlc** — unit `phases-unit`; production doc `docs/plans/sdlc-production.md`

## Source

The user's words (2026-11-05):

1. "we're going to have to not do simplify in each phase - we're going to have to figure out how to do it just once a day"
2. "workers are not claude workers are codex - units are claude - simplify should run in the same account as the unit that runs it"
3. "the scope of the simplify needs to be for everything that has changed since the last simplify run on codebase that the unit is working on"

The showrunner approved the plan on 2026-11-05 and told this unit to keep `SIMPLIFY_LENS` off until this work is on main.

## Delegation Context

- **Project:** `~/.claude` commands and scripts for production work.
- **Stack:** Python 3, Bash, Markdown commands and docs.
- **Layout:** `scripts/direct/` launchers and records, `scripts/agents/` model launch, `scripts/production/` merge records, `config/` settings, `docs/direct/` and `commands/unit/` director steps, `docs/as-built/` shipped behavior.
- **Key files:**
  - `scripts/direct/assign.py` — the one way to start a worker.
  - `scripts/direct/review.sh` — phase review launcher. It now carries the simplify path.
  - `scripts/direct/simplify_result.py` — prepares the simplify prompt and checks its result.
  - `scripts/agents/agent_exec.sh` — starts Claude for `direct.simplify` on the caller's account.
  - `scripts/copies/copies.py` — makes a task clone.
  - `scripts/lib/zone.py` — the user's time zone.
- **Test lanes:** none. Tests are `scripts/*/test_*.py` files.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s <folder> -p '<test file>'`, one run per test file.
- **Lint:** `basedpyright <changed Python files>`: zero errors and zero warnings.
- **Invariants:**
  - The run uses the caller's `CLAUDE_CONFIG_DIR`, at the `direct.simplify` model setting.
  - The run keeps the four agents in `SIMPLIFY_AGENTS` and plan mode with `Edit`, `Write` and `NotebookEdit` removed.
  - Every finding is recorded. None is discarded.
  - Do not change the value of `SIMPLIFY_LENS`.

## Phases

### Phase 1 — One simplify run per day replaces the lens in each phase review  · status: done

#### As-built

A phase review runs `adversary` and `contract`, plus armed `rerun`, and no simplify lens. After the code ready notice, a director runs `assign.sh --role simplify --session-dir <run> --cwd <worktree>` as a background shell.

- `scripts/direct/simplify_daily.py` reads `SIMPLIFY_LENS`, fetches origin's default branch and `refs/simplify/*`, and diffs `refs/simplify/last` (first run: the last commit before `SIMPLIFY_FIRST_RUN_BEFORE`, or the root) against the tip.
- An empty diff makes no claim. A lease push to `refs/simplify/claim/<date>` allows one run per codebase per day in the user's zone. A lease rejection prints `simplify: claimed today`.
- Claude reads a clone detached at the tip, through `agent_exec.sh direct.simplify readonly`, on the caller's `CLAUDE_CONFIG_DIR`.
- Status goes `preparing`, `reviewing`, `reviewed` or `error`. `reviewed` is written before the push of `refs/simplify/last`. Each failure after `preparing` writes `error` and an `ERROR:` report, and leaves the ref in place.
- The simplify counts are gone from `progress_history.py`, `merge_checkpoint.py` and `review_regime.py`. Old ledger rows with those keys still load.

**Files:**
- `scripts/direct/simplify_daily.py` — the daily launcher.
- `scripts/direct/simplify_result.py` — library: `prepare`, `validate`, `failure_reason`, `log_errors`, `well_formed`.
- `scripts/direct/assign.py` — `--role simplify`.
- `scripts/usage/agents_log.py` — `PHASE_REVIEW_LENSES`.
- `config/direct.conf` — `SIMPLIFY_FIRST_RUN_BEFORE`.
- `config/review/simplify.md` — the saved diff is the scope.

**Gotchas:** a stopped launcher can leave status `reviewing` and its clone; the next day's run still covers the changes. A failed run keeps the day's claim, so the next run is on the next day.
