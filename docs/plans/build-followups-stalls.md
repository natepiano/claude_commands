# Build follow-ups: memory stalls — plan

> **Status: running.** The build report shows when memory, not CPU, held builds back.
> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there, Phase 3 of `docs/plans/build-followups.md`, moved here by the showrunner (2026-10-04) to run beside followups-unit; followups-unit's Phase 2 (temp-folder rows, test isolation) and other units also edit `report.py`: whoever lands second resolves conflicts. The user asked for them to run as a production with natedev as showrunner.
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

### Phase 1 — Memory stalls · status: todo

#### Work Order

**Goal:** the report shows when memory, not CPU, held builds back: which steps stalled, how long, and how many steps ran at once.

**Spec:**
- **Why:** `peak_mem_bytes` is per step and includes page cache, so it misses the real risk, many steps at once. The 2026-09-30 freeze filled RAM and swap; at 05:52 EDT 2026-10-04 swap was 7/7 GiB and the report showed nothing.
- **Per step:** where `record.py` reads the step's cgroup `memory.peak`, also read its `memory.pressure` `some` and `full` totals at step end, stored as stall seconds in new `steps` columns (schema change through the store's own migration path). Natedev only, like peak memory.
- **Per day, machine-wide:** a `buildlog sample` command records one row (time, used memory, swap used, `/proc/pressure/memory` `some` and `full` totals) into a new table. A systemd user timer runs it every 60 s; that timer goes in `/etc/nixos`, which this unit does not edit: write the command and its test, and name the timer's exact command line in the checkpoint notice for the showrunner to install. The sample costs one short process a minute; measure its run time and state it.
- **Section "Memory pressure":** the day's peak used memory and swap (from the samples, labelled as 60 s samples), total stall time (from the counter deltas, across reboots), and the steps with the most stall: kind, caller, stall seconds, and how many steps ran at once at their start (overlapping intervals in `steps`). Empty days print one line, not an empty table.

**Files:** `scripts/buildlog/record.py`, `scripts/buildlog/store.py`, `scripts/buildlog/cli.py`, `scripts/buildlog/buildlog`, `scripts/buildlog/report.py`, a new `scripts/buildlog/sample.py`, and their tests.

**Acceptance gate:** Test and Lint green; a step recorded in a test cgroup or a fixture carries stall seconds; the section renders from fixture rows.

