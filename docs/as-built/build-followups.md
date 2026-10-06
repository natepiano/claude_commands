# Build cost: scoped test builds, scratch rows and the disk table

## What it is

Agents' test builds linked far more than the tests they ran, and the daily build report did not show what builds cost the machine. `verify.sh test <pkg>` builds only the named package's test programs (4 in hana, against 78 for `--lib --bins --tests` across the workspace) and runs the same tests. The build report (`buildlog report [day]`) shows that cost three ways: a temporary table of daily test-build time against fixed baselines; builds under temp folders (agent scratch clones, delegate session folders) as one `scratch (temp folders)` caller in every kind's table and summary; and a Disk table of where natedev's root filesystem goes: `~/rust`, `/tmp`, the two CI runners, everything else, and free space against the sweep's floor. The buildlog test suite writes only to a temporary log and never walks the real disks, so nothing it does reaches the real log or shows up as a temp-folder build.

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/delegate/verify.sh` | `TEST_TARGETS_PY` picks the targets from `cargo metadata`; `take_test_targets` reads them into `TEST_SELECTION`; the `test` arm builds with them. |
| `scripts/buildlog/report.py` | `report()` assembles the day. `SCRATCH`, `GROUP_AS_SCRATCH`, `SCRATCH_LABEL`, `kind_section`, `summary` for scratch rows; `test_builds_section`; `disk_section`. |
| `scripts/buildlog/disk.py` | The Disk rows (`FOLDERS`), the walk (`measure`), the floor (`read_floor`, `InvalidFloor`), the snapshot file, and the directories outside the build caches that the disk-floor alerts read (`docs/as-built/build-memory-admission.md`). |
| `scripts/buildlog/cli.py` | `buildlog disk`, listed in the usage docstring. |
| `scripts/buildlog/store.py` | `root()` (`BUILDLOG_DIR` moves it), `DISK_NAME`, `host_name()`, `utc_iso()`. |
| `scripts/lint/sweep.py` | `directory_blocks(directory, seen=None)`, `config_values`, `floor_bytes`, shared with the lint sweep. |
| `scripts/buildlog/test_index.py` | `use_test_log`, `point_root_at`, `LogIsolationTests`, and the record builders other tests import. |
| `scripts/buildlog/test_report.py`, `test_disk.py`, `scripts/lint/test_sweep.py` | Report sections; the walk on fake trees; a hard link across two `directory_blocks` calls sharing `seen`. |
| `commands/unit/delegate.md` | The seats' command table; its `--filter` row reads `--filter <name> [--filter <name> …]`. |
| `pyrightconfig.json` | `scripts/lint` on the `scripts/buildlog` environment's `extraPaths`, for `import sweep`. |
| `/etc/nixos`, `nate.jobs.buildlog-disk` | The 10-minute timer that runs `buildlog disk` on natedev. Not in this repo. |

### Report layout

`report(connection, day)` emits, in order:

1. `## Builds, <Weekday YYYY-MM-DD>`
2. `### Waiting` (build-folder turns, memory admission, CI queue; see `docs/as-built/build-memory-admission.md`)
3. `### <kind>` for each kind seen that day (`KIND_ORDER` first), one row per caller by run count, the scratch row among them
4. `### Memory pressure`, always present: memory waits, the sccache, unsliced-step and memory-kill lines, the stall table and the sample line (`docs/as-built/buildlog-memory-stalls.md`, `docs/as-built/build-memory-admission.md`)
5. `### Agent calls (verify.sh)`
6. `### Test builds (temporary)`, only on a day with a measured `verify.sh test` call
7. `### cargo-port calls (port-lint)`
8. `### CI`
9. `### Tests per edit` (`docs/as-built/buildlog-tests-per-edit-rust-release.md`)
10. `### Disk: <host>`, only when a snapshot exists
11. `### Summary: successes`, `failures`, `all`, or `### Summary` / `No build steps recorded.` on a day with no steps
12. The calls, port-lint and CI totals lines and the Rust release line, then the Mac-sync and peak-memory notes

Every average carries a `p95` beside it, by nearest rank (`nearest_rank(values, percent)`, sorted index `(percent*n+99)//100-1`): each kind table (`COMMON_HEAD` is `Runs, Failed, Avg, p95, Range`; each `AverageColumn` adds `<title> p95`), the summaries (`Kind, Runs, [Failed], Total, Avg, p95, Peak memory`) and the CI table (`Workflow, Runs, Failed, Cancelled, Avg, p95, Range`).

### Test target selection

`take_test_targets` pipes `cargo metadata --no-deps` into `TEST_TARGETS_PY` with the package and its `--features` list. The script expands the enabled features (`default` plus the `<pkg>/`-qualified names from `--features`, transitively). It emits `--lib` when the package has any lib kind (proc-macro included), then `--bin <name>` for each bin and `--test`/`--example`/`--bench <name>` for each target with `test` enabled. If one target needs a feature that is off, or shares its kind and name with a target in another member, the whole selection becomes `--bins --tests` (plus `--lib` when there is one); an empty selection becomes `--bins --tests` too.

The arm runs `cargo nextest --no-fail-fast --workspace <selection> -E <filter>` with the feature flags. The filter is `package(<pkg>)`, or `package(<pkg>) & test(a)` for one `--filter`, or `package(<pkg>) & (test(a) | test(b) …)` for several. `test <pkg> <integration_test>` keeps its own `--test <name>` build and never calls the selector.

### Test builds table

`test_builds_section(connection, day)` reads `calls` rows with `tool = 'verify.sh'`, `verb = 'test'` and a non-null `build_s`. A call is `--filter` when `--filter` is a whole word of its `command`, otherwise whole-package. The section is a source line, a `Period | Scope | Build/day | p75 build/call` table holding three fixed baseline rows and then one row per scope seen that day (total build time and nearest-rank p75, `nearest_rank(values, 75)`, both through `seconds()`), and the line `Temporary: kept until the user calls the result settled.`

### Scratch rows

`SCRATCH` is a SQL predicate on `cwd`: `/tmp/%`, `/var/folders/%`, `/private/var/folders/%`. `GROUP_AS_SCRATCH` is `SCRATCH` for every caller except `brp-launch`: a BRP launch from a temp folder stays under its own caller, `example launches (brp)`. `kind_section` selects `GROUP_AS_SCRATCH` as `is_scratch` and nulls host and caller on those rows, so `GROUP BY is_scratch, caller_host, grouped_caller` folds every other temp-folder step of a kind into one row labelled `SCRATCH_LABEL`, across hosts and callers. `kinds` and `summary` apply no scratch filter, so kind discovery and all three summaries, totals and peak memory included, count scratch steps. The host count in `report()` leaves out the rows `GROUP_AS_SCRATCH` folds: it decides whether caller labels carry a `(host)` suffix, and the scratch row names no host. The memory-pressure stall table labels by `SCRATCH` itself.

### Disk table

The walk is too slow for report time, so a job measures and the report reads.

**Measuring.** `buildlog disk` runs `disk.write_snapshot(disk.snapshot())` and prints nothing on success. `snapshot()` reads the floor first, then calls `measure(FOLDERS, lambda: filesystem_usage("/"), floor)`. `write_snapshot` writes `<store.root()>/disk.json.tmp` and renames it over `disk.json` (`store.DISK_NAME`). A malformed floor raises `InvalidFloor` before any walking; the command prints `buildlog disk: <key> in <config> must be a non-negative number of GiB` to stderr, exits 1, and the previous `disk.json` stays.

```python
FOLDERS = [("~/rust", "~/rust"), ("/tmp", "/tmp"),
           ("CI runner 1", "/var/lib/hana-ci/hana-linux-1"),
           ("CI runner 2", "/var/lib/hana-ci/hana-linux-2")]

class DiskRow(TypedDict):
    label: str
    bytes: int

class MeasuredDiskSnapshot(TypedDict):
    measured_at: str     # store.utc_iso, stamped after the walk
    host: str            # store.host_name(), the short name
    rows: list[DiskRow]  # FOLDERS order
    used: int
    free: int
    floor: int | None    # bytes; None when lint.conf sets none
    previous_measured_at: str | None
    outside_build_caches: list[OutsideBuildCacheDirectory]
    outside_build_cache_totals: list[OutsideBuildCacheTotal]

def measure(folders: list[tuple[str, str]], usage: Callable[[], FilesystemUsage], floor: int | None,
            previous: PreviousOutsideMeasurement = NO_EARLIER_OUTSIDE_MEASUREMENT) -> MeasuredDiskSnapshot
```

`DiskSnapshot`, what `read_snapshot` returns, has the same keys with the last three `NotRequired`, so a `disk.json` written before they existed still reads. The Disk table uses only the first six; the three outside-cache keys (directories one and two levels under each `FOLDERS` entry holding at least 1 GiB outside cargo target dirs, with growth since `previous_measured_at`) serve the disk-floor alerts in `scripts/lint/sweep.py` (`docs/as-built/build-memory-admission.md`).

`measure` expands `~` and walks the rows in order with one `seen: set[sweep.InodeKey]` passed to every `sweep.directory_blocks(path, seen)` call, then measures the outside-cache directories, then calls `usage()` once, then stamps `measured_at`. A row's size is allocated blocks (`st_blocks * 512`, symlinks not followed), each `(st_dev, st_ino)` counted once across all rows: a file hard-linked into two rows counts in the first. `directory_blocks` skips any directory or entry it cannot read; called without `seen`, it counts per directory, as the sweep's own doc-index sizing does.

`filesystem_usage(path)` is a frozen `FilesystemUsage(used, free)` from `os.statvfs`: used is `(f_blocks - f_bfree) * f_frsize`, free is `f_bavail * f_frsize`, the numbers `df -B1 /` prints as Used and Avail.

`read_floor() -> int | None` reads lint.conf (`LINT_CONFIG_FILE`, else `~/.claude/config/lint.conf`) through `sweep.config_values`, and takes `sweep_free_floor_gib.<host>`, else `sweep_free_floor_gib`, through `sweep.floor_bytes`, with the host from `socket.gethostname()` as the sweep reads it.

**Rendering.** `disk_section()` takes no connection and no day. `disk.read_snapshot()` returns the parsed `disk.json`, or None when it is missing or not JSON; None leaves the section out (the Mac, or before the job's first run). Otherwise:

```
### Disk: natedev

| Where | Size |
|---|--:|
| ~/rust | … GiB |
| /tmp | … GiB |
| CI runner 1 | … GiB |
| CI runner 2 | … GiB |
| other | … GiB |
| free (floor 500.0 GiB) | … GiB |

Measured by the buildlog disk job at 12:14 EDT: allocated blocks, each hard-linked file once.
```

`other` is used minus the sum of the rows, clamped at zero. The free label is plain `free` when the floor is None. The time goes through `sync_time`, which adds the date when it is not today.

### Test log isolation

`test_index.py` creates one `TemporaryDirectory` per process and calls `use_test_log()` at import, setting `BUILDLOG_DIR` to `<tmp>/buildlog`; `store.root()` reads that variable. Every test module imports `test_index`, so the variable is set before any test runs; modules that import `use_test_log` by name also call it. `point_root_at(test, root)` points one test at its own folder and restores the previous value through `addCleanup`. `LogIsolationTests.test_each_module_uses_a_temporary_log` imports each `test_*.py` alone in a subprocess with `BUILDLOG_DIR` removed, and fails, naming the module, when `store.root()` is `~/.local/state/buildlog`.

## Invariants

- A test never reaches `~/.local/state/buildlog` and never walks the real disks. The suite needs no `BUILDLOG_DIR` from its caller; importing `test_index` sets it. Disk tests hand `measure` fake trees and run `snapshot()` only with `FOLDERS`, the walk and `filesystem_usage` patched.
- A new `test_*.py` imports `test_index` or calls `use_test_log()`; `LogIsolationTests` fails until it does.
- `store.py` carries no test-only default location.
- `verify.sh test` builds under `--workspace`, never `-p <pkg>`. The selection decides what builds; `-E 'package(<pkg>)'` and the `--filter` union decide what runs.
- The package's integration tests stay in its test build.
- A temp-folder step is counted, never dropped: one scratch row per kind, and in every summary.
- The report never walks the disk; it reads `disk.json` only. `buildlog disk` is its one writer and replaces it by rename, so a reader sees a whole snapshot, old or new.
- Every inode counts once across all Disk rows, in `FOLDERS` order, through one shared `seen`; `other` is used minus the rows, clamped at zero.
- `disk.json` stays at the log root beside `sync.json`. `buildlog sync` moves only `<host>/` and `ci/`, so the snapshot never reaches the Mac, and the Mac's report has no Disk section.
- `report.py` keeps one style: `table()`, `seconds()`, `count()`, `gib()`. A section names its source and has at most one line under each table. Times carry their zone, and their date when not today.
- The test-builds table stays until the user calls the result settled.

## Calibration / gotchas

- Measured 2026-10-04: hana runs the same 2,251 tests with 78 → 4 test programs built. hana_catalyst runs the same 418 tests with 78 → 33 (33 derived from the broad listing; a direct listing was killed by earlyoom). obsidian_knife has no lib, so the selection leaves `--lib` out (`--lib --bins --tests` fails there with "no library targets found"); it runs 135 tests from 1 program.
- `--lib` cannot be narrowed to one member under `--workspace`, so a package with a lib still builds every member's lib tests.
- Baselines: 2026-10-01/02 whole-package 5.2 h/day, p75 137 s; 2026-10-04 from 00:07 EDT whole-package 5.0 h, p75 103 s, and `--filter` 14.4 h, p75 71 s. They come from earlier measurement, and whether those p75s used the nearest-rank convention is unverified.
- A command with `--filter` inside a word (`test demo--filter`) counts as whole-package.
- Hana Rust builds are killed by earlyoom under memory pressure (two SIGTERMs on 2026-10-04).
- Temp-folder rows in the real log (read 2026-10-04) come from agent scratch clones and delegate session folders, none from buildlog's own tests.
- The leak check runs one short subprocess per test module.
- The disk walk takes 13 s warm and 67 s cold.
- The timer (`nate.jobs.buildlog-disk`, every 600 s) lives in `/etc/nixos` and runs the live `~/.claude` copy, so a change to `disk.py` reaches it only from `~/.claude` main, and a snapshot can be ten minutes old.
- The Disk table is the latest snapshot whatever day the report covers; `sync_time` dates it against now, not against the report's day.
- Rows and used are read at different moments, so churn during the walk moves `other`; that is why it clamps at zero.
- A directory the walk cannot read (each runner's `rustup/tmp`) is skipped and its blocks fall into `other`. The runner folders read because `natepiano` is in groups `hana-linux-1` and `hana-linux-2`; without that membership a runner row drops to zero and its bytes move to `other`.
- All rows sit on the one ext4 filesystem `/`; `/tmp` is not a separate mount. `other` means something only while that holds: a row on another filesystem (a tmpfs `/tmp`) would subtract bytes that `used` never counted.
- Rows, `other` and free add up to less than the disk: ext4's reserved blocks are in neither used (`f_blocks - f_bfree`) nor free (`f_bavail`), about 93 GiB on natedev (`stat -f /`, 2026-10-04).
- `read_snapshot` casts the JSON to `DiskSnapshot` without checking its shape. A change to the snapshot's keys must still render the old `disk.json`, or the report raises `KeyError` until the job's next write.
- The heading's host is `store.host_name()` (short); the floor key's host is `socket.gethostname()` (full), as the sweep reads it.
- To try a disk change by hand without replacing the live snapshot: `BUILDLOG_DIR=$(mktemp -d) python3 scripts/buildlog/cli.py disk`, then `report` with the same `BUILDLOG_DIR`, and compare used and free with `df -B1 /`.
- In a worktree, `./scripts/buildlog/buildlog` execs the live `~/.claude` CLI; render the worktree's report with `python3 scripts/buildlog/cli.py report <day>`.
- `test_report.py` has no `sys.path` setup and runs only under `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`.
- `disk.py` puts `scripts/lint` at the front of `sys.path` to import `sweep`; basedpyright finds it through `extraPaths`.
- `basedpyright scripts/buildlog` exits 3 in every checkout because `pyrightconfig.json` names a `.venv` that is absent; the gate is its `0 errors, 0 warnings` line, not its exit code.

## Why

- **Only the package's targets.** `--bins --tests` linked every member's test executables to run one package's tests, and the filter then skipped all of theirs.
- **`--workspace`, not `-p`.** Cargo resolves features per invocation. `-p <pkg>` gives each member its own feature set and its own compile of bevy and the rest of the tree, nine to eleven copies of bevy_render evicting one another under the sweep budget. One workspace resolution reuses one compiled copy, which is also why `--lib` stays workspace-wide.
- **The broad fallback.** A target named explicitly whose required features are off errors where `--tests` would skip it, and a name shared with another member selects both; `--bins --tests` is correct in both cases.
- **Integration tests stay in.** Lint compiles them under clippy; leaving them out of `test` would let a change lint an integration test and pass its gate without ever running it.
- **A temporary table.** It shows whether the narrowed build lowers daily test-build time against the baselines; the user removes it once that is settled.
- **Scratch builds count.** Builds under temp folders are agent work (scratch clones, delegate session folders), not throwaway crates or buildlog's own test runs, so leaving them out understates what builds cost the machine. With tests held to a temporary log, nothing under a temp folder comes from the suite.
- **Isolation in `test_index`, not `store.py`.** Every module already imports `test_index`, so one call at import covers the suite and production code carries no test logic. The subprocess check imports each module alone, so a module that only worked because another had set `BUILDLOG_DIR` first still fails.
- **A snapshot job, not a report-time walk.** A walk costs 13–67 s; a report should cost a file read.
- **Allocated blocks, each inode once, across rows.** Cargo hard-links artifacts within a target directory, and a `git clone --local` under `/tmp` hard-links its objects to the repo in `~/rust`. Counting apparent sizes, or each row on its own, counts those blocks twice, and the rows could exceed used. One `seen` across all rows makes rows plus `other` equal used.
- **Usage read once, after the walk.** It puts used as close to the rows in time as the walk allows, and `measured_at` marks when the numbers were complete.
- **Floor read before the walk.** A malformed lint.conf fails in milliseconds instead of after a minute's walk, and the last good snapshot stays.
- **Reuse the sweep's walk and config reader.** One block counter and one lint.conf parser; the floor shown is the one the sweep enforces, against free measured the same way (`f_bavail`).
- **These four rows.** They are where builds write: worktrees and their targets under `~/rust`, scratch clones and session folders under `/tmp`, and the two CI runners' trees. Everything else is `other`.
- **natedev only.** The user scoped the Disk table to natedev; the Mac is not measured.
