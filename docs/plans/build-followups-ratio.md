# Build follow-ups: tests per edit — plan

> **Status: running.** The build report tracks tests per edit toward a target, without letting failures go unseen too long.
> **Production: build-followups** — unit `ratio-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there, Phase 4 of `docs/plans/build-followups.md`, moved here by the showrunner (2026-10-04) to run beside followups-unit; followups-unit's Phase 2 (temp-folder rows, test isolation) and other units also edit `report.py`: whoever lands second resolves conflicts. The user asked for them to run as a production with natedev as showrunner.
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

### Phase 1 — Tests per edit · status: todo

#### Work Order

**Goal:** the report tracks how often seats run tests per edit, with the goal of driving it down without letting failures go unseen long enough to cost more to fix.

**Spec:**
- **Edits:** for each seat (`calls.delegate_session`, else `session`), an edit is a change of tree key between its consecutive verify.sh calls.
- **Ratio:** test calls (whole and `--filter`) per edit, per seat and per day.
- **Cost of testing late:** for each failed test call, the edits since that seat's last green test, and the minutes from the failure to its next green test.
- **The sweet spot:** group failures by edits since the last green (1, 2–3, 4–7, 8+) with the average minutes to the next green and the count. The target ratio is a named constant in `report.py` with a comment giving its source: set it from the data at the end of this phase, where the minutes to green start to climb, and state the number and the reasoning in the checkpoint notice.
- **Section "Tests per edit":** the day's ratio against the target and the 7-day trend in one table; the bins in a second table. No more.

**Files:** `scripts/buildlog/report.py`, `scripts/buildlog/test_report.py`, and `scripts/buildlog/index.py` or `record.py` only if the tree key is not already in `calls`.

**Acceptance gate:** Test and Lint green; the section renders for 2026-10-04 from the real log, read-only.

