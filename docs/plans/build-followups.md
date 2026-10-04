# Build follow-ups — plan

> **Status: running.** Agents' test builds cost less, and the build report shows what builds cost the machine: test scope and temp-folder builds.
> **Production: build-followups** — unit `followups-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there; Phases 1–2 are below; on 2026-10-04 memory stalls moved to stalls-unit, tests per edit to ratio-unit, and cancelling a superseded CI run to notifier-unit. The user asked for them to run as a production with natedev as showrunner.
- **Already done, do not redo:** `test --filter` takes several names (`verify.sh` 953ed15), and `commands/unit/delegate.md:360-364` already tells seats to run `check` after each batch of edits and `test --filter` once per finished change.
- **This repo is every session's live configuration.** `~/.claude` main is what every Claude session on natedev and the Mac loads. Edit only this worktree. Run the worktree's copies (`./scripts/...`), never `~/.claude/scripts/...`, when testing a change.
- **The build log** lives in `~/.local/state/buildlog` (`scripts/buildlog/store.py`; `BUILDLOG_DIR` moves it). `buildlog schema` lists its tables (steps, calls, ci_runs, ci_jobs, tests) and columns; `buildlog query "<SQL>"` reads it. Read the real log freely, but never write to or delete from it in a test.
- **Times** in every report and section carry their zone (EDT), and their date when not today.

## Delegation Context

- **Project:** `~/.claude` (commands, skills and scripts; Python 3.13 and shell, no Rust).
- **Project started:** 2026-10-04T16:13:27+00:00
- **Layout:** `scripts/delegate/verify.sh`; `scripts/buildlog/{report,record,store,index,sync,cli,ci,treekey}.py` with `test_*.py` beside them; `commands/unit/delegate.md`.
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` from the worktree root.
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

### Phase 2 — Temp-folder builds count · status: done

#### As-built

- `report.py` groups steps under a temp folder (`SCRATCH`: `/tmp`, `/var/folders`, `/private/var/folders`) into one caller row per kind labelled `scratch (temp folders)` (`SCRATCH_LABEL`), across hosts and callers, selected by an `is_scratch` column. Scratch steps count in kind discovery and all three summaries, totals and peak memory included; no footer line remains. The host count leaves them out, since their row carries no host.
- `test_index.py` calls `use_test_log()` at import, pointing `BUILDLOG_DIR` at a suite temporary folder; `point_root_at` still overrides it per test. Every test module reaches it by importing `test_index`, and `test_parse` and `test_treekey` also call `use_test_log()` explicitly. `store.py` is unchanged: this shared setup covers every module.
- `LogIsolationTests.test_each_module_uses_a_temporary_log` imports each `test_*.py` in its own subprocess with `BUILDLOG_DIR` removed and fails, naming the module, if `store.root()` is the real log.
- The Test command sets no `BUILDLOG_DIR`; a test run cannot reach `~/.local/state/buildlog`.

**Files:**
- `scripts/buildlog/report.py` — `SCRATCH`, `SCRATCH_LABEL`, the scratch caller row.
- `scripts/buildlog/test_report.py` — scratch row tests: the three temp prefixes, cross-host grouping, failure counts, peak memory.
- `scripts/buildlog/test_index.py` — `use_test_log()`, `point_root_at`, `LogIsolationTests`.
- `scripts/buildlog/test_parse.py`, `test_treekey.py`, `test_record.py` — use the suite log location.

**Gotchas:**
- A new test module must import `test_index` or call `use_test_log()`; the leak check fails until it does.
- The leak check runs one short subprocess per test module.
- Temp-folder rows in the real log (read 2026-10-04) come from agent scratch clones and delegate session folders, none from buildlog's own tests.

**Ruled out:**
- A test-only default location in `store.py`: the shared test setup makes it unneeded.
