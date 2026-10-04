# Build follow-ups — plan

> **Status: running.** Agents' test builds cost less, and the build report shows what builds cost the machine: test scope, temp-folder builds, memory stalls, tests per edit, and a way to cancel a superseded CI run.
> **Production: build-followups** — unit `followups-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there, copied below as Phases 1–5. The user asked for them to run as a production with natedev as showrunner.
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

### Phase 1 — Test builds: only the named package's test programs · status: todo

#### Work Order

**Goal:** `verify.sh test <pkg>` builds only `<pkg>`'s own test programs instead of every package's, and the build report shows what that saved.

**Spec:**
- **Apply the patch.** `~/.local/state/nightly-review/2026-10-03/work/verify_scope_2026-10-04/verify_scope.diff` (88 lines; the patched `verify.sh` beside it) was written before 953ed15 made `--filter` repeatable. Port it onto today's `verify.sh` by hand and keep both. What it does: the test arm passes `--lib` plus `<pkg>`'s named targets from `cargo metadata --no-deps`: every bin (`--bins` also builds bins with `test = false`), and the tests, examples and benches whose `test` is true. It falls back to today's `--lib --bins --tests` when a target needs a feature that is off or a target name is shared with another package.
- **A package with no lib.** Today `verify.sh test` fails in `nateroids` and `obsidian_knife` because `--lib` errors on a package without one. Pass `--lib` only when the package has a lib target.
- **Proof it runs the same tests.** The 2026-10-04 retest (`equiv.sh`, `equiv/`, `e2e.sh` beside the diff) showed the same tests run with fewer programs built. Repeat that check on the ported script for `hana`, `hana_catalyst` and one package without a lib, in clones under `/tmp`: the list of tests run is identical before and after, and the number of test programs built drops.
- **The table row.** `commands/unit/delegate.md:348` shows one `--filter <name>`; show `--filter <name> [--filter <name> …]`.
- **Report section "Test builds (temporary)".** In `report.py`, after the verify.sh calls section: test build hours for the day and p75 build seconds per call, split whole-package versus `--filter` (a call whose `command` holds `--filter`), with the baselines as fixed rows: 2026-10-01/02 5.20 build-h/day, p75 137 s; 2026-10-04 from 00:07 EDT, whole 5.0 h/day p75 103 s, `--filter` 14.4 h/day p75 71 s. One line under it: `Temporary: kept until the user calls the result settled.`

**Files:** `scripts/delegate/verify.sh`, `commands/unit/delegate.md`, `scripts/buildlog/report.py`, `scripts/buildlog/test_report.py`.

**Acceptance gate:** Test and Lint green; the equivalence check's before/after test lists identical for the three packages, with the programs built before and after in the checkpoint notice.

### Phase 2 — Temp-folder builds count · status: todo

#### Work Order

**Goal:** builds under a temp folder (agents' scratch clones) count in the report as their own caller, and the build log holds no rows from buildlog's own tests.

**Spec:**
- `report.py:28` `SCRATCH` now drops these steps from every table and counts them in a footer line. Count them instead, as one caller row labelled `scratch (temp folders)` in each kind's table, so they add to totals and peak memory. Remove the footer line.
- Find which buildlog tests write to the real log (rows whose `cwd` is a test's temp dir; `record.py` writes wherever `store.root()` points). Make every test use a test-only location by default, so a test run without `BUILDLOG_DIR` cannot reach `~/.local/state/buildlog`, and add a test that fails if one does. Then drop `env BUILDLOG_DIR=…` from the Test command in this plan.
- Rows already in the log stay; delete nothing from it.

**Files:** `scripts/buildlog/report.py`, `scripts/buildlog/store.py` or the tests' shared setup, `scripts/buildlog/test_*.py`.

**Acceptance gate:** Test and Lint green; the report for 2026-10-04 run against the real log (read-only) shows the scratch row, and its totals equal the sum of the rows.

### Phase 3 — Memory stalls · status: todo

#### Work Order

**Goal:** the report shows when memory, not CPU, held builds back: which steps stalled, how long, and how many steps ran at once.

**Spec:**
- **Why:** `peak_mem_bytes` is per step and includes page cache, so it misses the real risk, many steps at once. The 2026-09-30 freeze filled RAM and swap; at 05:52 EDT 2026-10-04 swap was 7/7 GiB and the report showed nothing.
- **Per step:** where `record.py` reads the step's cgroup `memory.peak`, also read its `memory.pressure` `some` and `full` totals at step end, stored as stall seconds in new `steps` columns (schema change through the store's own migration path). Natedev only, like peak memory.
- **Per day, machine-wide:** a `buildlog sample` command records one row (time, used memory, swap used, `/proc/pressure/memory` `some` and `full` totals) into a new table. A systemd user timer runs it every 60 s; that timer goes in `/etc/nixos`, which this unit does not edit: write the command and its test, and name the timer's exact command line in the checkpoint notice for the showrunner to install. The sample costs one short process a minute; measure its run time and state it.
- **Section "Memory pressure":** the day's peak used memory and swap (from the samples, labelled as 60 s samples), total stall time (from the counter deltas, across reboots), and the steps with the most stall: kind, caller, stall seconds, and how many steps ran at once at their start (overlapping intervals in `steps`). Empty days print one line, not an empty table.

**Files:** `scripts/buildlog/record.py`, `scripts/buildlog/store.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/buildlog`, `scripts/buildlog/report.py`, a new `scripts/buildlog/sample.py`, and their tests.

**Acceptance gate:** Test and Lint green; a step recorded in a test cgroup or a fixture carries stall seconds; the section renders from fixture rows.

### Phase 4 — Tests per edit · status: todo

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

### Phase 5 — CI: cancel a superseded run · status: todo

#### Work Order

**Goal:** a push can cancel the branch's older CI run when its result no longer matters, so a newer run does not queue behind it, and the showrunner weighs that choice at every push.

**Spec:**
- `validate_and_push.sh --cancel-prior`: after its push, cancel every queued or in-progress run of the same workflow on the same branch whose head sha is not the one just pushed (`gh run list --branch … --json databaseId,headSha,status`, `gh run cancel`). Print one line per cancelled run. Without the flag nothing changes. No workflow file is edited: the choice stays per push.
- `commands/showrunner/produce.md`, at <MergeCheckpoint/> step 10 and <CIPoint/>: one short rule. Before a push, check for an older run of the merge branch still queued or running; pass `--cancel-prior` when the new push supersedes it and nothing waits on its result (not a CI point being watched, not a red run being diagnosed). Load `succinct_style` before editing a command file.
- Test it with a stub `gh` first on `PATH` that records its arguments.

**Files:** `scripts/validate_and_push/validate_and_push.sh`, `commands/showrunner/produce.md`, a test script beside `validate_and_push.sh`.

**Acceptance gate:** Lint green; the stub test shows the right runs cancelled and none without the flag.
