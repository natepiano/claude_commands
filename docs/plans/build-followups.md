# Build follow-ups — plan

> **Status: running.** Agents' test builds cost less, and the build report shows what builds cost the machine: test scope, temp-folder builds, memory stalls and tests per edit.
> **Production: build-followups** — unit `followups-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there; Phases 1–4 are below, and the fifth (cancelling a superseded CI run) moved to notifier-unit on 2026-10-04. The user asked for them to run as a production with natedev as showrunner.
- **Already done, do not redo:** `test --filter` takes several names (`verify.sh` 953ed15), and `commands/unit/delegate.md:360-364` already tells seats to run `check` after each batch of edits and `test --filter` once per finished change.
- **This repo is every session's live configuration.** `~/.claude` main is what every Claude session on natedev and the Mac loads. Edit only this worktree. Run the worktree's copies (`./scripts/...`), never `~/.claude/scripts/...`, when testing a change.
- **The build log** lives in `~/.local/state/buildlog` (`scripts/buildlog/store.py`; `BUILDLOG_DIR` moves it). `buildlog schema` lists its tables (steps, calls, ci_runs, ci_jobs, tests) and columns; `buildlog query "<SQL>"` reads it. Read the real log freely, but never write to or delete from it in a test.
- **Times** in every report and section carry their zone (EDT), and their date when not today.

## Delegation Context

- **Project:** `~/.claude` (commands, skills and scripts; Python 3.13 and shell, no Rust).
- **Project started:** 2026-10-04T16:13:27+00:00
- **Layout:** `scripts/delegate/verify.sh`; `scripts/buildlog/{report,record,store,index,sync,cli,ci,treekey}.py` with `test_*.py` beside them; `commands/unit/delegate.md`.
- **Test:** `env BUILDLOG_DIR=$(mktemp -d) python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` from the worktree root (Phase 2 makes the tests set their own location; drop the `env` after it).
- **Lint:** `basedpyright scripts/buildlog` with zero errors and zero warnings, and `bash -n` (or `zsh -n` for a zsh script) on each changed shell script.
- **Invariants:**
  - Python (user rules): annotate every signature; no `Any` (a `TypedDict` for dicts with known keys); no file-level type ignores; `uv pip install`, never `pip install`.
  - A test never writes the real build log (`~/.local/state/buildlog`) or touches the user's state.
  - A Rust build for testing `verify.sh` runs in a `git clone --local` under `/tmp`, never in another session's worktree or in `~/rust/hana`; hana links only inside its nix devShell (`nix develop`).
  - Every new report section states its source and is no longer than it needs to be: one table and at most one line under it.
  - `report.py` keeps one style: `table()`, `seconds()`, `count()`, `gib()`.

## Phases

### Phase 1 — Test builds: only the named package's test programs · status: done

#### As-built

- `verify.sh test <pkg>` and `test <pkg> --filter <name> [--filter <name> …]` build the selection `TEST_TARGETS_PY` computes from `cargo metadata --no-deps`: `--lib` when `<pkg>` has a lib target (proc-macro counts; under `--workspace` it still builds every member's lib tests), then each of `<pkg>`'s bins and its test-enabled tests, examples and benches by name. A target that needs a feature that is off, or whose name another member shares, switches the whole selection to `--bins --tests` (plus `--lib` when there is one); an empty selection also becomes `--bins --tests`. Enabled features are `default` plus the `<pkg>/`-qualified names from `--features`, expanded transitively. `test <pkg> <int_test>` is unchanged, and `-E 'package(<pkg>)'` (with the `--filter` union) still decides what runs.
- Same tests, fewer programs: hana runs the same 2,251 tests with 78 → 4 programs built; hana_catalyst the same 418 tests, 78 → 33 (33 derived from the broad listing; a direct listing was killed by earlyoom); obsidian_knife (no lib) failed before with "no library targets found" and now runs 135 tests across 1 program.
- `report.py` → `test_builds_section(connection, day)`, rendered between the verify.sh calls section and the port-lint section: `### Test builds (temporary)`, three fixed baseline rows, then one row per scope with measured `verify.sh test` calls that day — total build time and nearest-rank p75 per call, both through `seconds()`. A call is `--filter` when `--filter` is a whole word of its `command`. A day with no measured `verify.sh test` call has no section. Removal is the user's call: the line under the table reads `Temporary: kept until the user calls the result settled.`

**Files:**
- `scripts/delegate/verify.sh` — `TEST_TARGETS_PY`, `take_test_targets`, the test arm's scoped build; the header and usage text describe it, and the integration-test rationale comment stays.
- `commands/unit/delegate.md` — the `--filter` row reads `--filter <name> [--filter <name> …]`.
- `scripts/buildlog/report.py` — `test_builds_section`.
- `scripts/buildlog/test_report.py` — tests for the scope split, day totals and p75, the fixed baselines and note, and a name containing `--filter` counting as whole-package; `render(day)` takes an optional day.

**Gotchas:**
- In a worktree, `./scripts/buildlog/buildlog` execs the live `~/.claude` CLI; render the worktree's report with `python3 scripts/buildlog/cli.py report <day>`.
- `basedpyright scripts/buildlog` exits 3 in every checkout because `pyrightconfig.json` names a missing `.venv`; its diagnostics line, not its exit code, is the lint result.
- `test_report.py` runs under `unittest discover` only; it carries no `sys.path` setup.
- Hana Rust builds are killed by earlyoom under memory pressure (two SIGTERMs on 2026-10-04).
- The fixed baseline p75s come from earlier measurement; whether they used the same nearest-rank convention is unverified.

### Phase 2 — Temp-folder builds count · status: todo

#### Work Order

**Goal:** builds under a temp folder (agents' scratch clones) count in the report as their own caller, and the build log holds no rows from buildlog's own tests.

**Spec:**
- `report.py:28` `SCRATCH` now drops these steps from every table and counts them in a footer line. Count them instead, as one caller row labelled `scratch (temp folders)` in each kind's table, so they add to totals and peak memory. Remove the footer line.
- Find which buildlog tests write to the real log (rows whose `cwd` is a test's temp dir; `record.py` writes wherever `store.root()` points). Make every test use a test-only location by default, so a test run without `BUILDLOG_DIR` cannot reach `~/.local/state/buildlog`, and add a test that fails if one does. Then drop `env BUILDLOG_DIR=…` from the Test command in this plan.
- Rows already in the log stay; delete nothing from it.

**Files:**
- `scripts/buildlog/report.py` — the `scratch (temp folders)` caller row replaces the footer line
- `scripts/buildlog/store.py` — a test-only log location by default (or the tests' shared setup instead)
- `scripts/buildlog/test_*.py` — every test uses that location, plus a test that fails if one reaches the real log

**Seats:** 2 writers, split by the two Spec items
- `impl` — `scripts/buildlog/report.py`, `scripts/buildlog/test_report.py` (the scratch caller row)
- `test` opens as `impl` — `scripts/buildlog/store.py` and every other `scripts/buildlog/test_*.py` (the test-only log location and the guard test); hub: `scripts/buildlog/test_index.py` (its `point_root_at` helper is what the other tests share)

**Constraints from prior phases:**
- `report.py` has a "Test builds (temporary)" section (`test_builds_section`) after the verify.sh calls section; it formats with `seconds()` and is tested in `test_report.py`. Leave it in place.
- Render a worktree's report with `python3 scripts/buildlog/cli.py report <day>`: `./scripts/buildlog/buildlog` execs the live `~/.claude` CLI and shows main's report, not the worktree's.
- `basedpyright scripts/buildlog` exits 3 in every checkout because `pyrightconfig.json` names a `.venv` that does not exist; the gate is its `0 errors, 0 warnings` line.
- Run the tests with `discover` (the Test command); test modules do not set up `sys.path` themselves.

**Acceptance gate:** Test and Lint green; the report for 2026-10-04 run against the real log (read-only) shows the scratch row, and its totals equal the sum of the rows.

### Phase 3 — Memory stalls · status: todo

#### Work Order

**Goal:** the report shows when memory, not CPU, held builds back: which steps stalled, how long, and how many steps ran at once.

**Spec:**
- **Why:** `peak_mem_bytes` is per step and includes page cache, so it misses the real risk, many steps at once. The 2026-09-30 freeze filled RAM and swap; at 05:52 EDT 2026-10-04 swap was 7/7 GiB and the report showed nothing.
- **Per step:** where `record.py` reads the step's cgroup `memory.peak`, also read its `memory.pressure` `some` and `full` totals at step end, stored as stall seconds in new `steps` columns (schema change through the store's own migration path). Natedev only, like peak memory.
- **Per day, machine-wide:** a `buildlog sample` command records one row (time, used memory, swap used, `/proc/pressure/memory` `some` and `full` totals) into a new table. A systemd user timer runs it every 60 s; that timer goes in `/etc/nixos`, which this unit does not edit: write the command and its test, and name the timer's exact command line in the checkpoint notice for the showrunner to install. The sample costs one short process a minute; measure its run time and state it.
- **Section "Memory pressure":** the day's peak used memory and swap (from the samples, labelled as 60 s samples), total stall time (from the counter deltas, across reboots), and the steps with the most stall: kind, caller, stall seconds, and how many steps ran at once at their start (overlapping intervals in `steps`). Empty days print one line, not an empty table.

**Files:**
- `scripts/buildlog/record.py` — read each step's `memory.pressure` at step end
- `scripts/buildlog/store.py` — new `steps` columns and the samples table, through the migration path
- `scripts/buildlog/cli.py` — the `sample` command
- `scripts/buildlog/buildlog` — route `sample`
- `scripts/buildlog/sample.py` — new: one machine-wide memory sample
- `scripts/buildlog/report.py` — the "Memory pressure" section
- `scripts/buildlog/test_*.py` — tests for the above

**Seats:** 2 writers, split per-step versus machine-wide
- `impl` — `scripts/buildlog/record.py`, `scripts/buildlog/report.py`, `scripts/buildlog/test_record.py`, `scripts/buildlog/test_report.py`; hub: `scripts/buildlog/store.py` (both new schema pieces go through its migration)
- `test` opens as `impl` — `scripts/buildlog/sample.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/buildlog`, and a new `scripts/buildlog/test_sample.py`

**Constraints from prior phases:**
- `report.py` has a "Test builds (temporary)" section (`test_builds_section`) after the verify.sh calls section; it formats with `seconds()` and is tested in `test_report.py`. Leave it in place.
- Render a worktree's report with `python3 scripts/buildlog/cli.py report <day>`: `./scripts/buildlog/buildlog` execs the live `~/.claude` CLI and shows main's report, not the worktree's.
- `basedpyright scripts/buildlog` exits 3 in every checkout because `pyrightconfig.json` names a `.venv` that does not exist; the gate is its `0 errors, 0 warnings` line.
- Run the tests with `discover` (the Test command); test modules do not set up `sys.path` themselves.

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

**Files:**
- `scripts/buildlog/report.py` — the "Tests per edit" section and the target constant
- `scripts/buildlog/test_report.py` — tests for that section
- `scripts/buildlog/index.py` — only if the tree key is not already in `calls`
- `scripts/buildlog/record.py` — only if the tree key is not already in `calls`

**Seats:** 1 writer + 1 tester
- `impl` — `scripts/buildlog/report.py` (and `index.py`/`record.py` only if the tree key is missing from `calls`)
- `test` — `scripts/buildlog/test_report.py`, tests written from the Spec with fixture calls

**Constraints from prior phases:**
- `report.py` has a "Test builds (temporary)" section (`test_builds_section`) after the verify.sh calls section; it formats with `seconds()` and is tested in `test_report.py`. Leave it in place.
- Render a worktree's report with `python3 scripts/buildlog/cli.py report <day>`: `./scripts/buildlog/buildlog` execs the live `~/.claude` CLI and shows main's report, not the worktree's.
- `basedpyright scripts/buildlog` exits 3 in every checkout because `pyrightconfig.json` names a `.venv` that does not exist; the gate is its `0 errors, 0 warnings` line.
- Run the tests with `discover` (the Test command); test modules do not set up `sys.path` themselves.

**Acceptance gate:** Test and Lint green; the section renders for 2026-10-04 from the real log, read-only.
