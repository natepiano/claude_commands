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

### Phase 1 — CI: cancel a superseded run · status: todo

#### Work Order

**Goal:** a push can cancel the branch's older CI run when its result no longer matters, so a newer run does not queue behind it, and the showrunner weighs that choice at every push.

**Spec:**
- `validate_and_push.sh --cancel-prior`: after its push, cancel every queued or in-progress run of the same workflow on the same branch whose head sha is not the one just pushed (`gh run list --branch … --json databaseId,headSha,status`, `gh run cancel`). Print one line per cancelled run. Without the flag nothing changes. No workflow file is edited: the choice stays per push.
- `commands/showrunner/produce.md`, at <MergeCheckpoint/> step 10 and <CIPoint/>: one short rule. Before a push, check for an older run of the merge branch still queued or running; pass `--cancel-prior` when the new push supersedes it and nothing waits on its result (not a CI point being watched, not a red run being diagnosed). Load `succinct_style` before editing a command file.
- Test it with a stub `gh` first on `PATH` that records its arguments.

**Files:** `scripts/validate_and_push/validate_and_push.sh`, `commands/showrunner/produce.md`, a test script beside `validate_and_push.sh`.

**Acceptance gate:** Lint green; the stub test shows the right runs cancelled and none without the flag.
