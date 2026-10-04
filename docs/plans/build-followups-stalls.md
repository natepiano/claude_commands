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

### Phase 1 — Memory stalls · status: done

#### As-built

- Each step records its cgroup memory stall time at step end: `BUILDLOG_SCOPE_SH` in `scripts/lint/invoke.sh` writes `memory.peak` and the `memory.pressure` `some`/`full` totals into the file `record.py` parses, stored as seconds in `steps.mem_stall_some_s` and `steps.mem_stall_full_s` (index schema 5). Natedev only, like peak memory; a step without a pressure file stores null, which the report treats as no stall.
- `buildlog sample` appends one machine sample (time, used memory, swap used, boot id, `/proc/pressure/memory` `some`/`full` totals) to `samples-<YYYY-MM>.jsonl` beside the step records; `index.py` ingests the file into a `samples` table.
- The daily report's "Memory pressure" section (`memory_pressure_section`) states its source, then a table of the five steps with the most stall (caller, kind, stall seconds, and how many steps on that host ran at once at the step's start), then one line with the day's peak used memory and swap (labelled as 60 s samples) and total machine stall. An empty day prints one line.

**Files:**
- `scripts/lint/invoke.sh` — `BUILDLOG_SCOPE_SH` writes `memory.peak` and `memory.pressure` to the scope file
- `scripts/buildlog/record.py` — parses the scope file into peak bytes and stall seconds
- `scripts/buildlog/sample.py` — the sample command and the meminfo, pressure and boot id parsers
- `scripts/buildlog/store.py` — `sample_file` names the monthly samples file
- `scripts/buildlog/index.py` — schema 5: step stall columns, the `samples` table and its indexes
- `scripts/buildlog/cli.py` — the `sample` command
- `scripts/buildlog/report.py` — `memory_pressure_section`
- `scripts/buildlog/test_{record,index,store,sample,report}.py` — fixture tests for the parsers, ingest and the section

**Gotchas:**
- Machine stall deltas count only between samples at most 5 minutes apart, since a counter rise across a longer gap cannot be placed on a day; across a boot id change the later counter is the delta.
- Temp-folder steps are left out of the stall table but counted in "at once".
- `peak_mem_bytes` is per step and includes page cache; it does not show the risk of many steps running at once.
- `buildlog sample` is Linux only: without `/proc` (the Mac) it prints one line and exits nonzero. The 60 s natedev systemd user timer that runs it belongs in `/etc/nixos`, not this repo; without it the `samples` table stays empty.
- Ingesting a month of samples (43,200 rows) costs 0.32 s the first time, then 2 ms.
- `basedpyright` exits 3 in every checkout because `pyrightconfig.json` names an absent `.venv`; the counts line, not the exit code, is the lint result.

**Ruled out:** a direct table write from `buildlog sample` — the per-minute sample never opens the SQLite index.

