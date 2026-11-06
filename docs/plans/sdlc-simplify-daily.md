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

### Phase 1 — One simplify run per day replaces the lens in each phase review  · status: todo

#### Work Order

**Goal:** A phase review runs no simplify lens. The first unit to finish a phase on a codebase each day starts one simplify run, on its own account, over everything changed since the last run on that codebase.

**Spec:**

**1. New launcher: `scripts/direct/simplify_daily.py`.** `assign.sh --role simplify --session-dir <run> --cwd <worktree>` calls it. It takes no phase, prompt, lens or pass. Steps, in order:

1. Read `SIMPLIFY_LENS`. If it is `off`, print `simplify: off` and exit 0.
2. In `<cwd>`, find the default branch with `git ls-remote --symref origin HEAD`. Fetch that branch and `+refs/simplify/*:refs/simplify/*` from origin. The branch tip is `tip`.
3. `base` is `refs/simplify/last`. With no such ref, `base` is `git rev-list -1 --before=<SIMPLIFY_FIRST_RUN_BEFORE> <tip>`. With no such commit, `base` is the repository's root commit.
4. If `git diff <base> <tip>` is empty, print `simplify: no changes` and exit 0.
5. Claim the day. `<date>` is the calendar day in the user's zone (`scripts/lib/zone.py`). Run `git push origin --force-with-lease=refs/simplify/claim/<date>: <tip>:refs/simplify/claim/<date>`. An empty lease value means the ref must not exist.
   - If git reports the lease rejection, print `simplify: claimed today` and exit 0.
   - Any other push failure prints git's reason on stderr and exits 1.
   - After a claim, delete the claim refs of earlier days from origin. A failed delete prints one line and does not stop the run.
6. Make a task clone of `<tip>` with the existing `copies.py clone`, detached at `<tip>`. The reviewer reads this clone, not the unit's worktree.
7. Write these files under `<run>/simplify/<date>/`: `diff` (the saved diff), `source_prompt.md` (states the repository, `base`, `tip`, and the user's words on scope), `prompt.md`, `report.txt`, `agent.log`, `status`.
8. Use `simplify_result.py` to prepare the prompt with `/simplify <absolute diff path>`. Run `agent_exec.sh direct.simplify readonly <clone> <prompt> <report> <log>`. Use `simplify_result.py` to check the result. Write the status atomically: `preparing`, `reviewing`, `reviewed` or `error`.
9. Record the fresh session in `agents.jsonl` as role `review`, lens `simplify`, with the record `review.sh` writes today.
10. After `reviewed`, run `git push origin --force-with-lease=refs/simplify/last:<old value> <tip>:refs/simplify/last`. On `error`, do not move `refs/simplify/last`. The failed run's changes then stay in scope for the next run.
11. Remove the task clone by its own path. Never run `scratch.py end --task until:<stamp>`.
12. Print `simplify: <status>; report <path>`. Exit 0 on `reviewed`, else exit 1 with the reason on stderr.

Add `SIMPLIFY_FIRST_RUN_BEFORE=2026-11-05T22:50:00-05:00` to `config/direct.conf`, with a comment: the first run on a codebase starts at this time, the hard stop of the lens in each phase. Rewrite the comment above `SIMPLIFY_LENS`: it now switches the daily run. Do not change the `SIMPLIFY_LENS` value line.

**2. `scripts/direct/assign.py`.** Add `simplify` to `--role`. Remove `simplify` from `--lens`. Delete `--diff`, the `--lens simplify` argument rules and `execute_simplify`. `--role simplify` calls the new launcher. Refuse `--to`, `--prompt`, `--phase`, `--lens` and `--pass` with this role only if the parser needs that to stay simple; add no other refusal.

**3. `scripts/direct/review.sh`.** Delete every `direct.simplify` branch, `SIMPLIFY_DIFF`, `SIMPLIFY_SUMMARY`, `--diff`, `--summary`, `simplify_result`, `finish_simplify` and the simplify lock. Keep a shared function only if a Codex lens still calls it.

**4. Records.** In `scripts/direct/progress_history.py`, delete the simplify counts and minutes, `SEPARATE_TRIAL_LENSES` entry `simplify`, and the `direct.simplify` minute exclusion. In `scripts/production/merge_checkpoint.py`, delete `SimplifyReviewMetrics`, `NoSimplifyReview`, the optional suffix in the review-trial pattern, and the two arguments it passes. In `scripts/production/review_regime.py`, delete `--simplify-findings`, `--simplify-minutes`, the two fields, and their summary lines. Old ledger rows with the two keys must still load; the reader ignores unknown keys. Report it if they do not.

Keep `findings.py` held items and decisions, and `--lens simplify` on `findings.py open`. Keep `simplify` in `scripts/usage/agents_log.py`.

**5. Standing instructions: `config/review/simplify.md`.** The scope is the saved diff of everything changed since the last run. Replace `in-phase` with `in-scope`. An in-scope item preserves behavior and stays within the diff. Hold every behavior change and every change outside the diff. Keep the other rules.

**6. Director steps.**

1. `docs/direct/dual_review.md`: delete the `simplify` lens row, its paragraph, its prompt line and its launch step. A broad review runs `adversary` and `contract`, plus armed `rerun`.
2. `docs/direct/phase_end.md`: after the code ready notice, the director runs `bash ~/.claude/scripts/direct/assign.sh --role simplify --session-dir "${SESSION_DIR}" --cwd "${WORKING_DIR}"` as a background shell. The next phase does not wait for it.
3. `commands/unit/direct.md` (Synthesize and FindingsLedger): when a daily run reports items, the director that started it acts on them:
   1. Add a phase `simplify-<date>` to its plan, from the report.
   2. Record every item in its ledger under that phase, with `--lens simplify`.
   3. Send in-scope items and items that remove a copy to Codex fix workers as one repair phase, through the normal checkpoint.
   4. Open other items with `--held-for-decision`. Keep the present rule for which held items reach the user.
4. `docs/production_format.md`: delete the simplify suffix from the review-trial line.
5. `commands/associate-producer.md`: `/simplify` failures now come from the daily run.

**7. Docs that state how things are now.** Correct `docs/as-built/simplify-lens.md` in full for the daily run. Correct `docs/as-built/phase-checks.md`, `docs/as-built/review-regime-trial.md`, `docs/as-built/agent-registry.md`, `docs/as-built/long-lived-workers.md` and `docs/as-built/showrunner-automation.md` where they name the lens in a phase review or the simplify counts.

**8. Tests.** Rewrite `scripts/direct/test_simplify_lens.py` as tests of the new launcher against a bare origin repository in a temporary directory, with a fake Claude. Cover these behaviors:

1. `off` starts no Claude.
2. A first run reviews from the commit before `SIMPLIFY_FIRST_RUN_BEFORE`.
3. A later run reviews from `refs/simplify/last`, with `tip` from origin's default branch, not the worktree's `HEAD`.
4. An empty diff starts no Claude and makes no claim.
5. A second unit on the same day starts no Claude.
6. A claim from an earlier day does not stop a run, and that claim ref goes.
7. A `reviewed` run moves `refs/simplify/last`. An `error` run does not, and the next day's run covers its changes.
8. The Claude launch keeps the caller's `CLAUDE_CONFIG_DIR` and reads the clone at `tip`.
9. A push failure that is not a lease rejection exits 1 and starts no Claude.

Delete tests of behavior this phase removes, in `test_simplify_lens.py`, `test_review_launcher.py`, `test_review_launcher_delivery.py`, `test_phase_artifacts.py`, `test_progress_history.py`, `test_merge_checkpoint.py` and `test_review_regime.py`. `test_simplify_result.py` keeps its tests of `simplify_result.py`.

Add no guard, refusal or check that this Work Order does not ask for. The new tests must take less time than the file they replace.

**Files:**
- `scripts/direct/simplify_daily.py` — new launcher
- `scripts/direct/assign.py` — role in, lens and diff out
- `scripts/direct/review.sh` — simplify path out
- `scripts/direct/simplify_result.py` — only if the launcher needs a change
- `scripts/direct/progress_history.py` — simplify counts out
- `scripts/production/merge_checkpoint.py` — suffix out
- `scripts/production/review_regime.py` — fields out
- `config/direct.conf` — comments and the first-run time
- `config/review/simplify.md` — diff scope
- `docs/direct/dual_review.md` — lens out
- `docs/direct/phase_end.md` — daily start
- `commands/unit/direct.md` — who acts on findings
- `docs/production_format.md` — suffix out
- `commands/associate-producer.md` — failure source
- `docs/as-built/simplify-lens.md` — rewritten for the daily run
- `docs/as-built/phase-checks.md` — lens out
- `docs/as-built/review-regime-trial.md` — counts out
- `docs/as-built/agent-registry.md` — lens launch
- `docs/as-built/long-lived-workers.md` — lens launch
- `docs/as-built/showrunner-automation.md` — suffix out
- `scripts/direct/test_simplify_lens.py` — rewritten
- `scripts/direct/test_review_launcher.py` — simplify tests out
- `scripts/direct/test_review_launcher_delivery.py` — simplify tests out
- `scripts/direct/test_phase_artifacts.py` — simplify tests out
- `scripts/direct/test_progress_history.py` — simplify tests out
- `scripts/production/test_merge_checkpoint.py` — suffix tests out
- `scripts/production/test_review_regime.py` — field tests out

**Workers:** 1 writer + 1 tester
- `impl`: every file above except the test files.
- `test`: the test files above.

**State:** `refs/simplify/last` and `refs/simplify/claim/<date>` on origin; `<run>/simplify/<date>/` files.

**Test:** `python3 -m unittest discover -s scripts/direct -p 'test_simplify_lens.py'`, `-p 'test_simplify_result.py'`, `-p 'test_review_launcher.py'`, `-p 'test_review_launcher_delivery.py'`, `-p 'test_phase_artifacts.py'`, `-p 'test_progress_history.py'`; `python3 -m unittest discover -s scripts/production -p 'test_merge_checkpoint.py'`, `-p 'test_review_regime.py'`; `bash scripts/agents/test_agent_exec.sh`

**Lint:** `basedpyright` on each slot's changed Python files.

**Acceptance gate:** All listed tests pass. No file names `--lens simplify` as a launch. A dry run of the broad review steps in `docs/direct/dual_review.md` names no simplify launch.
