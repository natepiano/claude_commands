# Build log: memory stalls

## What it is

The build log records how long each build step waited on memory. A machine-wide sampler also records the same counters for the whole machine once a minute, and the daily `buildlog report` shows both. Before this feature, the report showed only `peak_mem_bytes` for each step. That figure counts page cache, so it runs above what the step's processes used, and because it is per step it cannot show many steps running at once and competing for the same RAM. Memory stall time is the kernel's own count of time tasks spent waiting for memory. A step with stall time was held back by memory, not by CPU.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/lint/invoke.sh` | `BUILDLOG_SCOPE_SH`, the shell run inside each step's systemd scope. It writes the scope cgroup's `memory.peak` and `memory.pressure` to the scope file. |
| `scripts/buildlog/record.py` | `peak_and_stall()` parses the scope file into a `StepScopeMemory`. `step()` writes `peak_mem_bytes`, `mem_stall_some_s` and `mem_stall_full_s` into the step record. |
| `scripts/buildlog/sample.py` | `sample()` (the `buildlog sample` command) and the parsers `parse_meminfo`, `parse_pressure` and `parse_boot_id`. Also the types `MachineMemoryUse`, `MemoryStallTotals`, `PartialMemoryStallTotals` and `Unmeasured`. |
| `scripts/buildlog/store.py` | `sample_file(host, epoch)` names `<root>/<host>/samples-<YYYY-MM>.jsonl`. |
| `scripts/buildlog/index.py` | `SCHEMA_VERSION = 5`: the two step stall columns, `SAMPLE_COLUMNS` and the `samples` table, `add_sample`, and the indexes `samples_host_at` and `steps_host_started`. |
| `scripts/buildlog/cli.py` | The `sample` command. It takes no arguments and exits 2 if given any. |
| `scripts/buildlog/report.py` | `memory_pressure_section()` and `MAX_SAMPLE_GAP_S`. It reuses `SCRATCH` and `SCRATCH_LABEL`. |
| `scripts/buildlog/test_{record,index,store,sample,report}.py` | Fixture tests; see Tests below. |

### Per-step stall: scope file → step record → `steps`

1. On natedev, `buildlog_exec` runs each step through `systemd-run --user --scope … /bin/sh -c "$BUILDLOG_SCOPE_SH" "$BUILDLOG_PEAK" "$@"`:

   ```sh
   exec 2>&3 3>&-; { : > "$0"; } 2>/dev/null || exit 125; "$@"; s=$?; cgroup="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; cat "$cgroup/memory.peak" "$cgroup/memory.pressure" > "$0" 2>/dev/null; exit $s
   ```

   `$0` is the scope file. The empty marker written before the step proves the scope ran it. After the step, the shell is still inside the scope. It finds its cgroup v2 path and overwrites the marker with three lines: the peak in bytes, then the `some … total=N` and `full … total=N` lines, whose totals are microseconds. It then exits with the step's own status.
2. `record.py step` receives that path as its `PEAK` argument. `peak_and_stall()` reads line 1 as the peak. It passes the remaining lines to `sample.parse_pressure(…, require_both=False)` and converts each total to seconds. Each value that is missing or malformed becomes `sample.Unmeasured.VALUE`. Only at the record boundary does `step()` turn `Unmeasured` into JSON `null`. The scope file is deleted afterwards.
3. `index.add_step` copies the fields into `steps.mem_stall_some_s` and `steps.mem_stall_full_s`, both `REAL` seconds. `some` is time during which at least one task waited on memory; `full` is time during which every task waited.

### Machine samples: `buildlog sample` → monthly JSONL → `samples`

1. `sample()` checks that `/proc/meminfo`, `/proc/pressure/memory` and `/proc/sys/kernel/random/boot_id` exist. It parses all three and appends one line to `store.sample_file(host, now)` through `store.append_line`, which writes one line under a flock with `O_APPEND`:
   `{"kind":"sample","host":…,"at":<UTC ISO ms Z>,"boot_id":…,"mem_used_bytes":…,"swap_used_bytes":…,"stall_some_us":…,"stall_full_us":…}`
   - `mem_used_bytes` is MemTotal minus MemAvailable.
   - `swap_used_bytes` is SwapTotal minus SwapFree.
   - The stall fields are the raw counters since boot, in microseconds.
2. The file sits in the host folder beside `<YYYY-MM>.jsonl`. `index.update()` already reads every `<root>/*/*.jsonl` and dispatches on `kind` through `ADDERS`, so `add_sample` picks the lines up incrementally with no special case. The hourly `buildlog sync` copies the host folder, so the Mac's index gets natedev's samples too.
3. The `samples` table has one row per sample: `src, host, at, boot_id, mem_used_bytes, swap_used_bytes, stall_some_us, stall_full_us`. It is indexed on `(host, at)`.

### The report section

`memory_pressure_section(connection, day, hosts)` runs after the per-kind sections and before the verify.sh calls section. `day` is the local day of the machine running the report.

- **Nothing that day:** if no step stalled and there are no samples, the whole section is the line `Memory pressure: no samples and no step stalls.` It has no heading.
- **Otherwise:** the heading `### Memory pressure`, then `Source: 60 s machine samples and step cgroup stall counters.`, then:
  - **Stall table**, shown only when a step stalled: `| Caller | Kind | Stall | At once |`.
    - Rows are the five steps with the largest `mem_stall_some_s > 0`, largest first. `full` is recorded but not shown.
    - Caller is `caller_label()`, which appends the host when more than one host built that day. A step whose cwd is under a temp folder shows as `scratch (temp folders)` instead.
    - "At once" counts the steps on the same host for which `started_at <= s.started_at < ended_at`. The count includes the step itself, every caller and scratch steps. Steps on another host never count. `steps_host_started` serves this subquery. It compares ISO strings, which works because `store.utc_iso` writes one fixed-width UTC format.
  - **One line under the table**, either:
    - `60 s samples: peak used memory X GiB, peak swap Y GiB; machine stall: some A, full B.`, or
    - `60 s samples: none; machine stall: unavailable.` when there are no samples.
- **How the machine stall is summed:**
  - The day's samples are taken per host, in time order. For a host's first sample of the day, the baseline is that host's latest earlier sample from any day.
  - A pair of samples counts only when the gap is between 0 and `MAX_SAMPLE_GAP_S` (300 s).
  - With the same `boot_id`, the pair adds `max(0, later − earlier)`. With a different `boot_id`, it adds the later counter.
  - The stall totals are summed over all hosts, and the peaks are the maximum over all of the day's samples.

### Tests (what is pinned)

- `test_sample.py`:
  - The meminfo arithmetic and the pressure totals in microseconds.
  - `require_both=False` leaves a missing or invalid counter `Unmeasured`, while the default raises.
  - The boot id is trimmed, and an empty one is rejected.
  - Without `/proc`, `sample` prints exactly `buildlog sample is Linux only (requires /proc).` to stderr, exits nonzero and writes nothing.
  - A real `cli.py sample` appends exactly one record to `<host>/samples-YYYY-MM.jsonl`, with every field an integer ≥ 0, and creates no `index.sqlite`.
- `test_store.py`: `sample_file` is `<root>/<host>/samples-YYYY-MM.jsonl` and is not `host_file`.
- `test_record.py`:
  - Stall seconds are read from a three-line scope file.
  - `StepScopeMemory` marks absent stalls `Unmeasured`.
  - A file with only some counters gives `some` and a `null` `full`.
  - A peak-only file still records the peak, with null stalls.
- `test_index.py`: schema 5 or higher; the stall columns ingest, as null when absent; sample rows ingest; a second `update()` reads 0 lines; `buildlog schema` lists `samples` and both stall columns.
- `test_report.py` (memory section):
  - The top five ranking, with "at once" counted on the same host only.
  - The section placed after the kind sections.
  - Scratch rows labelled and counted in "at once".
  - Sample peaks, and the delta across a reboot.
  - Samples with no stalled step give no table.
  - Pairs are taken within each host and the totals summed across hosts.
  - A pair more than 5 minutes apart is ignored.
  - A pair across local midnight that is close enough counts, with `TZ=UTC` pinned.
  - An empty day is one line with no heading.

## Invariants

- **The sampler never opens the SQLite index.** `buildlog sample` appends one JSON line and exits; the index ingests that line on the next query. A test pins that no `index.sqlite` appears.
- **A test never writes the real log at `~/.local/state/buildlog`.** Every test module imports `test_index`, whose import calls `use_test_log()` and points `BUILDLOG_DIR` at a temp dir for the whole suite. `point_root_at()` moves it for a single test. Subprocess tests also pass `BUILDLOG_DIR` explicitly. The real `cli.py sample` test runs against a temp root.
- **Every report section follows one layout.** It states its source, then has one table at most and at most one line under it. An empty day collapses to one line.
- **`report.py` has one style.** Cells are formatted through `table()`, `seconds()`, `count()` and `gib()`, with no new formatters.
- **Unmeasured stall is null, never zero.** Inside Python it is `sample.Unmeasured.VALUE`; in JSON it is `null` and in SQLite `NULL`. Zero means measured with no stall. The report's `> 0` filter drops null rows, and SQL `avg()` skips them.
- **The scope string must not change the step's outcome.** It keeps the step's exit status, prints nothing, and writes the marker before the step runs. Callers run under `set -euo pipefail`, and a missing marker means the scope never started the step, so `buildlog_exec` runs it without the scope.
- **`peak_and_stall` never raises.** An empty path, an unreadable file, a peak-only file and malformed lines all give `Unmeasured`, because a recorder never fails its caller. Peak-only files still arrive from long-lived shells that sourced an older `invoke.sh`.
- **A machine sample is all or nothing.** `parse_pressure`'s default is `require_both=True`, typed by its overload to return `MemoryStallTotals`, which has plain ints. A sample row with a missing counter would break the per-host delta chain.
- **Units differ by table, deliberately.** `steps` stores stall in seconds (`REAL`, the scope's own total). `samples` stores the raw counters since boot in microseconds (`INTEGER`).
- **Any column change bumps `SCHEMA_VERSION`.** `open_for_update` deletes an index at another version and rebuilds it from the JSON lines.
- **`sample.py` stays cheap to import and has no side effects.** `record.py` imports it after every step for `parse_pressure` and `Unmeasured`.

## Calibration and gotchas

- **The 5-minute gap rule.** `MAX_SAMPLE_GAP_S = 300`, five 60 s intervals. A counter rise across a longer gap cannot be placed on a day, so it is dropped. That happens when the timer stopped or the machine was off. Across a `boot_id` change, PSI counters restart at zero, so the later counter is the delta; this still applies only within the 5-minute gap.
- **Temp-folder steps are in the stall table.** They appear as `scratch (temp folders)` (`SCRATCH_LABEL`), and they count in every step's "at once". `SCRATCH` matches a cwd under `/tmp/`, `/var/folders/` or `/private/var/folders/`.
- **Linux only.** `buildlog sample` without any of its three `/proc` files prints one line and exits 1. That includes a Linux kernel without PSI. A malformed `/proc/pressure/memory` raises and writes no row, and the next minute tries again. Step stalls need the systemd scope as well: a user manager, `systemd-run`, and `BUILDLOG_SCOPE` not set to `0`. That means natedev only, the same as `peak_mem_bytes`.
- **The 60 s timer lives in `/etc/nixos`, on natedev.** It is a `nate.jobs` entry like the hourly `buildlog` job in `modules/linux/buildlog.nix`, not anything in `~/.claude`. Without it the `samples` table stays empty. The report then shows only the stall table and `60 s samples: none; machine stall: unavailable.`, or the one-line empty message.
- **Cost.** One `buildlog sample` run takes 0.04 s (median). A month of samples is 43,200 rows; ingesting it costs 0.32 s the first time and 2 ms after that, because the index reads only bytes past its stored offset. `invoke.sh` prices the scope at 8 ms per step on top of the recorder's 2 ms (natedev, 2026-10-02); that figure predates the stall read.
- **The machine line has no host label.** It merges every host that has samples. Today only natedev samples, so it is natedev's figure. A second sampling host would add its stall into the same number.
- **The scope file keeps its old name.** It is still `BUILDLOG_PEAK`, a `*.peak` temp file and `record.py step`'s `PEAK` argument, but it now holds peak and pressure.
- **Peak still counts page cache.** The report's closing note says so. Stall, not peak, is the evidence of memory starvation.
- **A step's peak and stall leave out its compiles.** `rustc-wrapper = "sccache"` hands every compile to the one shared sccache server. The server starts rustc (and rustc starts the linker) in the cgroup of whichever session started the server. So a step's scope holds cargo, build scripts, the sccache clients and test processes, but not the compiler or the linker. Seen 2026-10-04 12:2x PDT: two live rustc processes were children of the server in a session's `tmux-spawn` scope, while their step's scope held only sccache clients. The machine sample does see the compiles.
- **The cgroup path assumes cgroup v2.** The scope string reads the unified `0::` line of `/proc/self/cgroup`.
- **basedpyright exits 3 in every checkout.** `pyrightconfig.json` names an absent `.venv`. The lint result is the error and warning counts line, not the exit code.

## Why it is this way

- **Stall rather than more peak figures.** Peak memory includes page cache and is per step, so it cannot say whether memory held a build back. PSI stall time is the kernel's direct measure of that waiting.
- **Read the scope's own cgroup, inside the scope.** Each step gets a fresh scope, so its `memory.pressure` totals are exactly that step's stall, with no subtraction and no sampling. A scope's cgroup is removed when its last process exits, so the read happens in the shell that ran the step, before that shell exits.
- **`total=` counters, not `avg10`/`avg60`/`avg300`.** The totals only grow, so the difference between two samples is the exact stall time between them, whenever the samples land. The rolling averages would lose the time between samples.
- **JSONL, never a direct table write (ruled out).** The sampler runs every minute. Opening the index would take `index.lock` and `BEGIN IMMEDIATE`, contending with every query's update. The index is derived and can be rebuilt from the JSON lines, so the lines are the record.
- **A separate monthly samples file.** At 1,440 lines a day, samples would bury the step and call records in `<YYYY-MM>.jsonl`. The index needs no change to find the new file.
- **"At once" is computed at report time, per host.** Every step's start and end are already in the index, so a query gives the overlap for any step without recording state at step start. Only steps on the same machine compete for its memory.
- **Scratch steps are labelled, not dropped.** A temp-folder build, such as a `git clone --local` under `/tmp`, takes the same RAM as any other build. The report shows such steps as one scratch caller everywhere, the same as in the per-kind tables.
- **`parse_pressure` has two overloads.** The step side tolerates a partial file, because a missing counter becomes `Unmeasured`. The machine side requires both counters. The `Literal` overloads give each caller its exact return type with no casts, and one parser serves both.
- **The timer lives in `/etc/nixos`.** Machine jobs on natedev are `nate.jobs` entries in the system config. `~/.claude` holds the command, not when it runs.
