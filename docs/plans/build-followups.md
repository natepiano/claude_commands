# Build follow-ups — plan

> **Status: running.** Agents' test builds cost less, and the build report shows what builds cost the machine: test scope and temp-folder builds.
> **Production: build-followups** — unit `followups-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user's adhoc review of 2026-10-04 (natedev session), five follow-up tasks recorded there; Phases 1–2 are below; Phase 3, the Disk table, the user added on 2026-10-04 through the showrunner; on 2026-10-04 memory stalls moved to stalls-unit, tests per edit to ratio-unit, and cancelling a superseded CI run to notifier-unit. The user asked for them to run as a production with natedev as showrunner.
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

### Phase 3 — Disk usage in the build report · status: done

#### As-built

- `buildlog disk` (`cli.py`, listed in its usage docstring) writes `<store.root()>/disk.json` (`store.DISK_NAME`) atomically, `disk.json.tmp` then `replace`, and prints nothing on success. The floor is read before the walk: a malformed one prints `buildlog disk: <reason>` to stderr and exits 1, leaving the previous `disk.json` in place.
- `scripts/buildlog/disk.py`: `DiskRow(TypedDict)` (`label`, `bytes`); `DiskSnapshot(TypedDict)` (`measured_at` via `store.utc_iso`, `host` via `store.host_name()`, `rows`, `used`, `free`, `floor: int | None`); `FOLDERS`, the four rows in order: `~/rust`, `/tmp`, `CI runner 1` (`/var/lib/hana-ci/hana-linux-1`), `CI runner 2` (`/var/lib/hana-ci/hana-linux-2`), `~` expanded at measure time; frozen `FilesystemUsage(used, free)` and `filesystem_usage(path)` from `os.statvfs` (used `(f_blocks - f_bfree) * f_frsize`, free `f_bavail * f_frsize`); `read_floor() -> int | None` from lint.conf's `sweep_free_floor_gib.<host>` (else `sweep_free_floor_gib`) through `sweep.config_values` and `sweep.floor_bytes`, raising `InvalidFloor` on a malformed value; `measure(folders, usage: Callable[[], FilesystemUsage], floor) -> DiskSnapshot`, which walks the folders, then calls `usage` once and stamps `measured_at`; `snapshot()`, `write_snapshot(snapshot)`, `read_snapshot() -> DiskSnapshot | None` (None when missing or unreadable).
- Counting is allocated blocks (`st_blocks * 512`), each inode once across all rows in row order, through one `seen` set shared by every `sweep.directory_blocks(directory, seen=None)` call: a file hard-linked into two rows counts in the first only, so `other` is exactly what the rows leave. Callers that pass no `seen` count per directory as before.
- `report.py`'s `disk_section()` renders after the CI section and before `### Summary: successes` (or `### Summary` on a day with no steps): `### Disk: <host>`, a `["Where", "Size"]` table of the four rows, `other` (used minus the rows, clamped at zero), `free (floor N GiB)` or plain `free` without a floor, then `Measured by the buildlog disk job at <sync_time>: allocated blocks, each hard-linked file once.` No snapshot (the Mac, or before the job's first run) leaves the section out.
- `disk.py` reaches `sweep` by putting `scripts/lint` at the front of `sys.path`; `pyrightconfig.json`'s `scripts/buildlog` environment lists `scripts/lint` in `extraPaths`.

**Files:**
- `scripts/buildlog/disk.py` — the rows, `measure`, `read_floor`/`InvalidFloor`, the snapshot file.
- `scripts/buildlog/cli.py` — the `disk` command.
- `scripts/buildlog/store.py` — `DISK_NAME`.
- `scripts/buildlog/report.py` — `disk_section()`.
- `scripts/lint/sweep.py` — `directory_blocks` with the optional shared `seen` set.
- `scripts/buildlog/test_disk.py` — fake-tree tests: hard links within and across rows, an unreadable folder, usage read once after the walk, malformed floor, snapshot round trip under the suite log; `snapshot()` runs only with `FOLDERS`, the walk and `filesystem_usage` patched.
- `scripts/buildlog/test_report.py` — the Disk section's rows, floor label, placement with and without steps, `other` clamping, absence without a snapshot.
- `scripts/lint/test_sweep.py` — a hard link across two calls sharing `seen` counts once.
- `pyrightconfig.json` — `scripts/lint` on the buildlog environment's `extraPaths`.

**Gotchas:** The walk takes 13 s warm and 67 s cold, so the report only reads `disk.json` and never walks. The 10-minute timer that runs `buildlog disk` (`nate.jobs.buildlog-disk`) lives in `/etc/nixos`, not this repo. Rows and used are read at different moments, so churn during the walk moves `other`; it clamps at zero. A directory the walk cannot read (each runner's `rustup/tmp`) is skipped and its blocks fall into `other`; the runner folders read because `natepiano` is in groups `hana-linux-1` and `hana-linux-2`. All five rows sit on the one ext4 filesystem `/`; `/tmp` is not a separate mount.

**Ruled out:** walking at report time (13–67 s per report); measuring the Mac's disk (natedev only, by the user's scope).
