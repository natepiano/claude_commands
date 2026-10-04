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

### Phase 1 — Tests per edit · status: done

#### As-built

- `report()` renders a "Tests per edit" section from `tests_per_edit_section(connection, day) -> list[str]`, after the test-build, port-lint and CI sections and before the summaries.
- `tests_per_edit_data(connection, end_day) -> TestsPerEditWindow` walks every `verify.sh` call through the report day in `started_at` order, per seat = `coalesce(nullif(delegate_session,''), nullif(session,''))`. `call_trees(connection, end_day) -> dict[str, KnownCallTrees]` gives each call its first and last known `steps.tree_key`, joined to `calls` by `steps.call_id` for `tool='verify.sh'`.
- An edit is a difference between the tree a seat's call ended on (its last step with a known key) and the tree its next call started on (first known key), so a lint call's own formatting rewrite is not an edit. A call with no known key is skipped and does not break the chain. Chains run over the whole log through the report day; tests, edits and failures count only inside the 7-day window.
- Tests are `verb = 'test'` calls, whole and `--filter`. Green = status 0 and outcome `ran` or `reused`; it resets the seat's edits since green and closes its open failures. Failure = outcome not `interrupted`, and outcome `failed` or a nonzero status.
- Trend table: 7 day rows (`DailyTestsPerEdit`, all seats summed), newest first: Day, Tests, Edits, Tests/edit, Target. `target_status(activity) -> str` gives `—` (no edits), `on target`, `above` or `below`, comparing ratio and target at two decimals; 0.5 is a sweet spot, not a ceiling.
- Bins table: `EDIT_BINS = ("0", "1", "2–3", "4–7", "8+")`, chosen by `failure_bin(edits: int) -> int`, each a `FailureRecoveryBin` (failures, recovered, minutes_to_green) shown as Failures and Avg to next green. The `0` bin holds failures right after a green. Bins cover the same 7 days as the trend; a failure with no later green counts in its bin but not in its average, and the line under the table gives how many.
- Each table has one source line naming the window `{first_day}–{day}`.
- `TESTS_PER_EDIT_TARGET = 0.5`; its comment gives the source: the 2026-10-01–04 log, average minutes to green 1 edit 12.54, 2–3 16.09, 4–7 20.94, 8+ 31.28; one test per two edits keeps most gaps within 2–3, before the climb at 4–7.

**Files:**
- `scripts/buildlog/report.py` — the section, its data pass (`TestsPerEditWindow`), the target constant
- `scripts/buildlog/test_report.py` — behavior tests for edits, ratio, bins, target labels and rendering

**Gotchas:**
- `calls` has no tree key; only `steps` does.
- 137 backfilled test calls carry outcome `failed` with status NULL, which is why the failure test checks outcome as well as status.
- `cli.py report` and `cli.py query` run `index.update()`, which writes the live index; the read-only path to the real log is `index.read_only()`.
- basedpyright exits 3 on every checkout because `pyrightconfig` names a missing `.venv`; the bar is 0 errors, 0 warnings.

**Ruled out:**
- A tree key column on `calls`: the `steps` join supplies it with no schema change, so `index.py` and `record.py` are unchanged.

