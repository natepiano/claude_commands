# Build follow-ups: cancel a superseded CI run — plan

> **Status: running.** A push can cancel the branch's older CI run when a newer push makes its result moot, and the showrunner weighs that at every push.
> **Production: build-followups** — unit `notifier-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there, Phase 5 of `docs/plans/build-followups.md`, moved here by the showrunner (2026-10-04) to run beside followups-unit. The user asked for them to run as a production with natedev as showrunner.
- **Already done, do not redo:** `test --filter` takes several names (`verify.sh` 953ed15), and `commands/unit/delegate.md:360-364` already tells seats to run `check` after each batch of edits and `test --filter` once per finished change.
- **This repo is every session's live configuration.** `~/.claude` main is what every Claude session on natedev and the Mac loads. Edit only this worktree. Run the worktree's copies (`./scripts/...`), never `~/.claude/scripts/...`, when testing a change.
- **The build log** lives in `~/.local/state/buildlog` (`scripts/buildlog/store.py`; `BUILDLOG_DIR` moves it). `buildlog schema` lists its tables (steps, calls, ci_runs, ci_jobs, tests) and columns; `buildlog query "<SQL>"` reads it. Read the real log freely, but never write to or delete from it in a test.
- **Times** in every report and section carry their zone (EDT), and their date when not today.

## Delegation Context

- **Project:** `~/.claude` (commands, skills and scripts; Python 3.13 and shell, no Rust).
- **Project started:** 2026-10-04T16:13:27+00:00
- **Layout:** `scripts/delegate/verify.sh`; `scripts/buildlog/{report,record,store,index,sync,cli,ci,treekey}.py` with `test_*.py` beside them; `scripts/validate_and_push/validate_and_push.sh`; `commands/showrunner/produce.md`; `commands/unit/delegate.md`.
- **Test:** `env BUILDLOG_DIR=$(mktemp -d) python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` from the worktree root (Phase 2 makes the tests set their own location; drop the `env` after it).
- **Lint:** `basedpyright scripts/buildlog` with zero errors and zero warnings, and `bash -n` (or `zsh -n` for a zsh script) on each changed shell script.
- **Invariants:**
  - Python (user rules): annotate every signature; no `Any` (a `TypedDict` for dicts with known keys); no file-level type ignores; `uv pip install`, never `pip install`.
  - A test never writes the real build log (`~/.local/state/buildlog`) or touches the user's state.
  - A Rust build for testing `verify.sh` runs in a `git clone --local` under `/tmp`, never in another session's worktree or in `~/rust/hana`; hana links only inside its nix devShell (`nix develop`).
  - Every new report section states its source and is no longer than it needs to be: one table and at most one line under it.
  - `report.py` keeps one style: `table()`, `seconds()`, `count()`, `gib()`.

## Phases

### Phase 1 — CI: cancel a superseded run · status: done

#### As-built

- `validate_and_push.sh --cancel-prior` cancels superseded CI after a direct push, on both the `--to` path and the default direct push. It takes the workflow names from the pushed sha's own runs, lists each workflow's `queued` and `in_progress` runs on the branch (one `gh run list --status` call per status), cancels every run on another sha, and prints `cancelled run <id> (<sha7>)` per cancel.
- Cancellation is best effort: a failed list or cancel prints a warning on stderr and the loop continues; the script exits with `push_direct.sh`'s status, so a post-push hook failure still cancels and still fails the run.
- When no run for the pushed sha has appeared, it prints `no run for <sha7> yet; nothing cancelled` and cancels nothing. Without the flag, no cancel query runs.
- `produce.md`, at <MergeCheckpoint/> step 10 and <CIPoint/>, before the push: check for an older queued or running run on the merge branch, and add `--cancel-prior` when this push supersedes it and no CI point is watching it or diagnosing a red run.

**Files:**
- `scripts/validate_and_push/validate_and_push.sh` — the `--cancel-prior` flag and `cancel_prior_runs`.
- `scripts/validate_and_push/test_cancel_prior.sh` — stub `git`/`gh` test over disposable copies of `validate_and_push.sh` and `push_direct.sh`; nine cases, including failed cancels, failed queries, hook failures and a missing successor run.
- `commands/showrunner/produce.md` — the cancel-prior rule at both push points.

**Gotchas:** `gh`'s own error text is suppressed, so a warning names what failed, not why.

**Ruled out:** cancelling older runs when the pushed sha has no run — the branch would be left with no current CI result.

