# Build log: memory stalls

## What it is

The build log records how long each build step waited on memory. A machine-wide sampler also records the same counters for the whole machine once a minute, with the machine's CPU and IO stall counters, its CPU idle and iowait ticks and its power profile, and the daily `buildlog report` shows them. The CPU and IO readings let the history say whether natedev was CPU bound or disk bound, not only memory bound. Before this feature, the report showed only `peak_mem_bytes` for each step. That figure counts page cache, so it runs above what the step's processes used, and because it is per step it cannot show many steps running at once and competing for the same RAM. Memory stall time is the kernel's own count of time tasks spent waiting for memory. A step with stall time was held back by memory, not by CPU.

## How it works

### Files

| File | Role |
| --- | --- |
| `scripts/lint/invoke.sh` | `BUILDLOG_SCOPE_SH`, the shell run inside each step's systemd scope. It writes the scope cgroup's `memory.peak` and `memory.pressure` to the scope file. |
| `scripts/buildlog/record.py` | `peak_and_stall()` parses the scope file into a `StepScopeMemory`. `step()` writes `peak_mem_bytes`, `mem_stall_some_s` and `mem_stall_full_s` into the step record. |
| `scripts/buildlog/sample.py` | `sample()` (the `buildlog sample` command), the parsers `parse_meminfo`, `parse_pressure`, `parse_cpu_ticks` and `parse_boot_id`, the readers `read_pressure` and `read_cpu_ticks`, and `read_power_profile` with its path `POWER_PROFILE_STATE`. The `/proc` paths are module values (`MEMINFO`, `PRESSURE`, `CPU_PRESSURE`, `IO_PRESSURE`, `STAT`, `BOOT_ID`) that tests patch. Also the types `MachineMemoryUse`, `StallTotals`, `PartialStallTotals` (shared by memory, CPU and IO), `CpuTicks`, `PowerProfile` and `Unmeasured`. |
| `scripts/buildlog/store.py` | `sample_file(host, epoch)` names `<root>/<host>/samples-<YYYY-MM>.jsonl`. |
| `scripts/buildlog/index.py` | `SCHEMA_VERSION = 25` (the memory-admission columns are in `docs/as-built/build-memory-admission.md`): the two step stall columns, `SAMPLE_COLUMNS` and the `samples` table, `add_sample`, and the indexes `samples_host_at` and `steps_host_started`. |
| `scripts/buildlog/cli.py` | The `sample` command. It takes no arguments and exits 2 if given any. |
| `scripts/buildlog/report.py` | `memory_pressure_section()`, `cpu_disk_pressure_section()`, the pairing helper `sample_counter_deltas()` with `SampleCounterInterval` and `CounterNotRecorded`, and `MAX_SAMPLE_GAP_S`. It reuses `SCRATCH` and `SCRATCH_LABEL`. The samples' power profile feeds `power_profile_section()` (`docs/as-built/build-targets.md`, "What the reports read"). |
| `scripts/buildlog/test_{record,index,store,sample,report,memory_records}.py` | Fixture tests; see Tests below. |

The index reads the former step/call field names in `index.FORMER_FIELDS` as `worker` and `unit_session`, keeping a present new field's value and leaving source JSONL bytes unchanged.

### App launches and admission waits

`hana_shot.launch()` loads the build-log recorder by file path at the call, without changing `sys.path`, after its existing atomic launch-state write. The call is inside the cleanup block, so interruption also shuts the new process down. The recorder reads the state once and records a `step: launch`, `caller: hana-shot` process-start event. Its stable identity uses the launching host, PID, port and recorded start time. The event has no measured startup duration and leaves the screenshot unit's launch-state file unchanged.

Before publication, `record.launch()` saves the complete row under the build log's own `pending-launches/` directory using a temporary file, fsync and rename. `record.finish_pending_launch()` publishes one complete immutable `launch-<identity hash>.jsonl` using a hard link, then removes the pending record only after the row is durable or already present. The launcher and `buildlog launches` share that completion function and lock. A failed publication remains recoverable even after Hana shutdown removes its cache state; interruptions and repeated recorder, collector or report calls keep one row per launch.

Launches skip memory admission by design. Their `mem_wait_s` is null and the report's **App launches** section displays **no admission**, ignoring ambient wait values and admission-record paths. Legacy BRP rows keep their existing recorded/missing wait wording. Rows start with this recorder change: there is no Hana transcript or cache backfill, and the collector continues its existing legacy BRP transcript collection while finishing build-log-owned pending records. `verify.sh build` compiles and prints binary paths; it does not start an app. Its existing build-step records retain their own admission wait. No resources admission follow-up is needed for app launches.

### Per-step stall: scope file → step record → `steps`

1. On natedev, `buildlog_exec` runs each step through `systemd-run --user --scope … --slice=builds.slice -- /bin/sh -c "$BUILDLOG_SCOPE_SH" "$BUILDLOG_PEAK" "$@"`:

   ```sh
   exec 2>&3 3>&-; { : > "$0"; } 2>/dev/null || exit 125; "$@"; s=$?; cgroup="/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup)"; cat "$cgroup/memory.peak" "$cgroup/memory.pressure" > "$0" 2>/dev/null; exit $s
   ```

   `$0` is the scope file. The empty marker written before the step proves the scope ran it. After the step, the shell is still inside the scope. It finds its cgroup v2 path and overwrites the marker with three lines: the peak in bytes, then the `some … total=N` and `full … total=N` lines, whose totals are microseconds. It then exits with the step's own status.
2. `record.py step` receives that path as its `PEAK` argument. `peak_and_stall()` reads line 1 as the peak. It passes the remaining lines to `sample.parse_pressure(…, require_both=False)` and converts each total to seconds. Each value that is missing or malformed becomes `sample.Unmeasured.VALUE`. Only at the record boundary does `step()` turn `Unmeasured` into JSON `null`. The scope file is deleted afterwards.
3. `index.add_step` copies the fields into `steps.mem_stall_some_s` and `steps.mem_stall_full_s`, both `REAL` seconds. `some` is time during which at least one task waited on memory; `full` is time during which every task waited.

### Machine samples: `buildlog sample` → monthly JSONL → `samples`

1. `sample()` checks that `/proc/meminfo`, `/proc/pressure/memory` and `/proc/sys/kernel/random/boot_id` exist. It parses all three, reads `/proc/pressure/cpu`, `/proc/pressure/io` and `/proc/stat` once each, reads the slice counters and the power profile, and appends one line to `store.sample_file(host, now)` through `store.append_line`, which first cuts an unfinished tail back to the last newline, then writes one line under the same flock with `O_APPEND`:
   `{"kind":"sample","host":…,"at":<UTC ISO ms Z>,"boot_id":…,"mem_used_bytes":…,"swap_used_bytes":…,"builds_anon_bytes":…,"sccache_in_service":…,"ci_anon_bytes":…,"stall_some_us":…,"stall_full_us":…,"cpu_stall_some_us":…,"cpu_stall_full_us":…,"io_stall_some_us":…,"io_stall_full_us":…,"cpu_idle_ticks":…,"cpu_iowait_ticks":…,"cpu_total_ticks":…,"power_profile":…,"power_profile_since":…}`
   - `mem_used_bytes` is MemTotal minus MemAvailable.
   - `swap_used_bytes` is SwapTotal minus SwapFree.
   - The stall fields are the raw counters since boot, in microseconds.
   - `cpu_stall_some_us`, `cpu_stall_full_us`, `io_stall_some_us` and `io_stall_full_us` are optional PSI counters since boot, in microseconds, from `/proc/pressure/cpu` and `/proc/pressure/io` (`read_pressure(path) -> PartialStallTotals`). A missing file or a malformed counter makes only that counter `null`, and the sample is still written; CPU kernels may omit `full`.
   - `cpu_idle_ticks`, `cpu_iowait_ticks` and `cpu_total_ticks` come from the aggregate `cpu ` line in `/proc/stat`, in USER_HZ ticks. `parse_cpu_ticks(text) -> CpuTicks(idle, iowait, total)` sums the first eight fields (user, nice, system, idle, iowait, irq, softirq, steal); guest fields are already included in user and nice. It raises `ValueError` on a missing or short line, and `read_cpu_ticks(path) -> CpuTicks | Unmeasured` turns that or an `OSError` into `Unmeasured`, which writes all three as `null`.
   - The slice fields (`builds_anon_bytes`, `sccache_in_service`, `ci_anon_bytes`) are in `docs/as-built/build-memory-admission.md`.
   - `power_profile` is power-profiles-daemon's `Profile=` value under `[State]` (`performance`, `balanced`, `power-saver`); `power_profile_since` is the state file's mtime as `store.utc_iso`, the moment the daemon last wrote it. Both are `null` when `read_power_profile` answers `Unmeasured.VALUE`.
2. The file sits in the host folder beside `<YYYY-MM>.jsonl`. `index.update()` already reads every `<root>/*/*.jsonl` and dispatches on `kind` through `ADDERS`, so `add_sample` picks the lines up incrementally with no special case. The hourly `buildlog sync` copies the host folder, so the Mac's index gets natedev's samples too.
3. The `samples` table has one row per sample: `src, host, at, boot_id, mem_used_bytes, swap_used_bytes, builds_anon_bytes, sccache_in_service, ci_anon_bytes, stall_some_us, stall_full_us, cpu_stall_some_us, cpu_stall_full_us, io_stall_some_us, io_stall_full_us, cpu_idle_ticks, cpu_iowait_ticks, cpu_total_ticks, power_profile, power_profile_since` (`SAMPLE_COLUMNS`; the seven CPU and IO columns `INTEGER`, both profile columns `TEXT`). It is indexed on `(host, at)`. A line written before a field existed indexes it as NULL; nothing is backfilled.

### The power profile: `read_power_profile(POWER_PROFILE_STATE)`

- `POWER_PROFILE_STATE` is `/var/lib/power-profiles-daemon/state.ini`; tests patch it like the `/proc` paths. `read_power_profile(path) -> PowerProfile | Unmeasured` returns `PowerProfile(name: str, since_epoch: float)`.
- It opens the file once, takes `os.fstat` on the descriptor, and reads that same handle. It scans the lines for `Profile=` under `[State]`.
- It returns `Unmeasured.VALUE` on `OSError`, undecodable text, a file of 1 KiB or more, no `[State]` section, or a missing or empty `Profile=` value. A missing file (the Mac, or any machine without the daemon) gives two `null` fields, and the sample is still written.
- It starts no process and opens no D-Bus connection.

### The report section

`memory_pressure_section(connection, day, hosts)` runs after the per-kind sections, followed by `cpu_disk_pressure_section(connection, day)`, then the verify.sh calls section. `day` is the local day of the machine running the report.

The section always has its heading `### Memory pressure`, then `Source: 60 s machine samples and step cgroup stall counters.`, then the memory-waits line and the sccache, unsliced-step and memory-kill lines (`docs/as-built/build-memory-admission.md`), then:
  - **Stall table**, shown only when a step stalled: `| Caller | Kind | Stall | At once |`.
    - Rows are natedev's five steps (`host = 'natedev'`) with the largest `mem_stall_some_s > 0`, largest first. `full` is recorded but not shown.
    - Caller is `caller_label()`. The section counts natedev alone, so no host is appended. A step whose cwd is under a temp folder shows as `scratch (temp folders)` instead.
    - "At once" counts the steps on the same host for which `started_at <= s.started_at < ended_at`. The count includes the step itself, every caller and scratch steps. Only natedev's steps count. `steps_host_started` serves this subquery. It compares ISO strings, which works because `store.utc_iso` writes one fixed-width UTC format.
  - **The sample line**, after the stall table's blank line (or after the instrument lines when there is no table), either:
    - `60 s samples: peak used memory X GiB, peak swap Y GiB; peak builds B GiB, peak CI C GiB (process memory); machine stall: some A, full B.` Each slice peak is the day's highest `builds_anon_bytes` or `ci_anon_bytes`, or `unavailable` when no sample has it., or
    - `60 s samples: none; machine stall: unavailable.` when there are no samples.
- **How the machine stall is summed:**
  - `sample_counter_deltas(connection, day, columns) -> list[SampleCounterInterval]` holds the one pairing rule for memory, CPU and IO counters. `SampleCounterInterval(host, elapsed_s, deltas)` keys `deltas` by counter name, never by position. A NULL at either end gives `CounterNotRecorded.VALUE`, which adds nothing to that counter or its denominator.
  - The day's samples are taken per host, in time order. For a host's first sample of the day, the baseline is that host's latest earlier sample from any day.
  - A pair of samples counts only when the gap is between 0 and `MAX_SAMPLE_GAP_S` (300 s).
  - With the same `boot_id`, the pair adds `max(0, later − earlier)`. With a different `boot_id`, it adds the later counter.
  - The stall totals are summed over all hosts, and the peaks are the maximum over all of the day's samples.

### CPU and disk pressure

`### CPU and disk pressure` follows Memory pressure, with `Source: 60 s machine samples (/proc/pressure/cpu, /proc/pressure/io, /proc/stat).`
Each host with a counted pair gets one line, `<host>: CPU some X%, full Y%; IO some X%, full Y%; CPU idle X%, iowait Y% (of <duration> sampled)`, each share to one decimal place. PSI divides counter deltas by wall time for that counter's counted pairs. Idle and iowait divide tick deltas by total-tick deltas from the same pairs. A share without a denominator is `—`. The sampled duration is the union of counted PSI intervals, formatted with the report's `seconds()` function; tick-only samples have zero sampled PSI time. With no counted host, the section says `CPU and IO samples: none.`

### Tests (what is pinned)

- `test_sample.py`:
  - The meminfo arithmetic and the pressure totals in microseconds.
  - `require_both=False` leaves a missing or invalid counter `Unmeasured`, while the default raises.
  - The boot id is trimmed, and an empty one is rejected.
  - Without `/proc`, `sample` prints exactly `buildlog sample is Linux only (requires /proc).` to stderr, exits nonzero and writes nothing.
  - A real `cli.py sample` appends a sample record and its memory snapshot to `<host>/samples-YYYY-MM.jsonl`, with the memory and stall counters integers ≥ 0, and creates no `index.sqlite`.
  - The power profile reads its name and mtime from one open file; a state file replaced by rename after the open keeps the open file's profile and mtime; a missing, unreadable, sectionless or valueless file is `Unmeasured`; a sample writes `power_profile` with a UTC `power_profile_since`, or two `null` fields with no state file.
- `test_store.py`: `sample_file` is `<root>/<host>/samples-YYYY-MM.jsonl` and is not `host_file`.
- `test_record.py`:
  - Stall seconds are read from a three-line scope file.
  - `StepScopeMemory` marks absent stalls `Unmeasured`.
  - A file with only some counters gives `some` and a `null` `full`.
  - A peak-only file still records the peak, with null stalls.
- `test_index.py`: schema 5 or higher; the stall columns ingest, as null when absent; sample rows ingest; a second `update()` reads 0 lines; `buildlog schema` lists `samples` and both stall columns; the profile columns index from new lines and stay NULL for old ones, and an index at the previous schema rebuilds them from the JSON lines.
- `test_sample.py` (CPU and IO): `parse_cpu_ticks` reads the aggregate line without guest fields and refuses a missing, short or malformed one; a sample records the seven fields; CPU pressure without `full` keeps `some` and the other readings; a missing optional reading makes only its own counters `null`; malformed optional readings still write the memory sample.
- `test_memory_records.py`: its subprocess fixture points `CPU_PRESSURE`, `IO_PRESSURE` and `STAT` at scratch files.
- `test_index.py` (schema 23): the seven columns index new counters and stay NULL for old lines, and an index at schema 22 rebuilds them from the JSON lines.
- `test_report.py` (CPU and disk section): it follows Memory pressure with one line per host; reboot counters count from zero; a long gap is not sampled time, while the maximum gap and the previous day's baseline count; a NULL pair does not dilute the other shares; empty, old-only and single samples give `CPU and IO samples: none.`; a host with ticks and no PSI still gets its line, with `—` PSI shares and `0.0 s` sampled.
- `test_report.py` (memory section):
  - The top five ranking, with "at once" counted on the same host only.
  - The section placed after the kind sections.
  - Scratch rows labelled and counted in "at once".
  - Sample peaks, and the delta across a reboot.
  - Samples with no stalled step give no table.
  - Pairs are taken within each host and the totals summed across hosts.
  - A pair more than 5 minutes apart is ignored.
  - A pair across local midnight that is close enough counts, with `TZ=UTC` pinned.
  - An empty day keeps the heading, `memory waits: none` and `60 s samples: none; machine stall: unavailable.`

## Invariants

- **The sampler never opens the SQLite index.** `buildlog sample` appends one JSON line and exits; the index ingests that line on the next query. A test pins that no `index.sqlite` appears.
- **A test never writes the real log at `~/.local/state/buildlog`.** Every test module imports `test_index`, whose import calls `use_test_log()` and points `BUILDLOG_DIR` at a temp dir for the whole suite. `point_root_at()` moves it for a single test. Subprocess tests also pass `BUILDLOG_DIR` explicitly. The real `cli.py sample` test runs against a temp root.
- **Nothing sits directly under a report table but its one `Source:` line.** That line carries any explanation of the table; a table without one ends at the next blank line. This section's `Source:` line sits under the heading, so its stall table ends at the blank line (`docs/as-built/build-followups.md` lists which tables carry the line).
- **`report.py` has one style.** Cells are formatted through `table()`, `seconds()`, `count()` and `gib()`, with no new formatters.
- **Unmeasured stall is null, never zero.** Inside Python it is `sample.Unmeasured.VALUE`; in JSON it is `null` and in SQLite `NULL`. Zero means measured with no stall. The report's `> 0` filter drops null rows, and SQL `avg()` skips them.
- **The scope string must not change the step's outcome.** It keeps the step's exit status, prints nothing, and writes the marker before the step runs. Callers run under `set -euo pipefail`, and a missing marker means the scope never started the step, so `buildlog_exec` runs it without the scope.
- **`peak_and_stall` never raises.** An empty path, an unreadable file, a peak-only file and malformed lines all give `Unmeasured`, because a recorder never fails its caller. Peak-only files still arrive from long-lived shells that sourced an older `invoke.sh`.
- **CPU and IO readings never stop a sample.** Each is optional and `null` when unreadable; only the memory readings are required.
- **One pairing rule.** Memory, CPU and IO deltas all come from `sample_counter_deltas`; a new section uses it, not its own pairing.
- **A machine sample is all or nothing for its memory counters.** `parse_pressure`'s default is `require_both=True`, typed by its overload to return `StallTotals`, whose counters are `int`, never `Unmeasured`. A sample row with a missing counter would break the per-host delta chain.
- **The power profile never stops a sample.** `read_power_profile` never raises; an unreadable profile is two `null` fields. `buildlog sample` never forks a process or opens D-Bus for it.
- **Old samples stay as written.** A sample line without the profile fields indexes as NULL; nothing infers or backfills a profile for it.
- **Units differ by table, deliberately.** `steps` stores stall in seconds (`REAL`, the scope's own total). `samples` stores the raw counters since boot in microseconds (`INTEGER`).
- **Any column change bumps `SCHEMA_VERSION`.** `open_for_update` deletes an index at another version and rebuilds it from the JSON lines.
- **`sample.py` stays cheap to import and has no side effects.** `record.py` imports it after every step for `parse_pressure` and `Unmeasured`.

## Calibration and gotchas

- **The 5-minute gap rule.** `MAX_SAMPLE_GAP_S = 300`, five 60 s intervals. A counter rise across a longer gap cannot be placed on a day, so it is dropped. That happens when the timer stopped or the machine was off. Across a `boot_id` change, PSI counters restart at zero, so the later counter is the delta; this still applies only within the 5-minute gap.
- **Temp-folder steps are in the stall table.** They appear as `scratch (temp folders)` (`SCRATCH_LABEL`), and they count in every step's "at once". `SCRATCH` matches a cwd under `/tmp/`, `/var/folders/` or `/private/var/folders/`.
- **Linux only.** `buildlog sample` without any of its three `/proc` files prints one line and exits 1. That includes a Linux kernel without PSI. A malformed `/proc/pressure/memory` raises and writes no row, and the next minute tries again. Step stalls need the systemd scope as well: a user manager, `systemd-run`, and `BUILDLOG_SCOPE` not set to `0`. That means natedev only, the same as `peak_mem_bytes`.
- **The 60 s timer lives in `/etc/nixos`, on natedev.** It is a `nate.jobs` entry like the hourly `buildlog` job in `modules/linux/buildlog.nix`, not anything in `~/.claude`. Without it the `samples` table stays empty. The report then shows the stall table, if any, and `60 s samples: none; machine stall: unavailable.`
- **Cost.** One `buildlog sample` run takes 0.04 s (median); the CPU, IO and stat reads add about 0.11 ms to the in-process `sample()` call (20-run median 0.234 ms → 0.347 ms). A month of samples is 43,200 rows; ingesting it costs 0.32 s the first time and 2 ms after that, because the index reads only bytes past its stored offset. `invoke.sh` prices the scope at 8 ms per step on top of the recorder's 2 ms (natedev, 2026-10-02); that figure predates the stall read.
- **The machine line has no host label.** It merges every host that has samples. Today only natedev samples, so it is natedev's figure. A second sampling host would add its stall into the same number.
- **The scope file keeps its old name.** It is still `BUILDLOG_PEAK`, a `*.peak` temp file and `record.py step`'s `PEAK` argument, but it now holds peak and pressure.
- **Peak still counts page cache.** The report's closing note says so. Stall, not peak, is the evidence of memory starvation.
- **A step's peak and stall leave out its compiles.** `rustc-wrapper = "sccache"` hands every compile to the one shared sccache server, which runs as the `sccache.service` user service inside `builds.slice`. The server starts rustc (and rustc starts the linker) in its own cgroup, so a step's scope holds cargo, build scripts, the sccache clients and test processes, but not the compiler or the linker. The machine sample, and the `builds.slice` figures in the samples and snapshots, do see the compiles.
- **The daemon replaces its state file by rename.** A path `stat` followed by a separate read can pair the new profile with the old file's mtime. `read_power_profile` keeps one open handle, `fstat`s its descriptor and reads that handle once.
- **The profile read costs 21.9 µs** per call (natedev). The state file holds 168 bytes on natedev.
- **`power_profile_since` is the last write, not the last switch.** The daemon can rewrite its file with the same profile and a newer mtime. The report counts a change only when the profile name differs from the previous profiled sample.
- **A subprocess test reads live `/proc` unless told otherwise.** A test that runs `sample()` in a subprocess must point `CPU_PRESSURE`, `IO_PRESSURE` and `STAT` at scratch paths, as `test_memory_records.py` does.
- **The `(of …)` duration counts PSI pairs only.** It is the time of pairs where any PSI counter counted, so a host with only tick readings shows `0.0 s` sampled beside valid idle and iowait shares.
- **A `SCHEMA_VERSION` raise changes `test_index.py`'s schema expectation**, which needs its own `Assertion-Change:` line at checkpoint.
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
- **Ticks share against the total-tick delta (ruled out: wall time times CPU count).** The total-tick delta of the same pair stays correct when CPUs go offline; wall time times CPU count does not.
- **`parse_pressure` has two overloads.** The step side tolerates a partial file, because a missing counter becomes `Unmeasured`. The machine side requires both counters. The `Literal` overloads give each caller its exact return type with no casts, and one parser serves both.
- **The power profile comes from the daemon's state file (ruled out: `powerprofilesctl`, D-Bus, `platform_profile`).** Forking `powerprofilesctl` or opening D-Bus every 60 s costs a process or a connection per sample. `/sys/firmware/acpi/platform_profile` is not driven by this daemon (`PlatformDriver=placeholder`), so it does not show a switch. NixOS sets no profile, so the state file survives a reboot.
- **The file's mtime records the switch time.** The first sample after a switch carries the moment of the switch, so the samples alone hold every change and no hand-kept marker exists.
- **The timer lives in `/etc/nixos`.** Machine jobs on natedev are `nate.jobs` entries in the system config. `~/.claude` holds the command, not when it runs.
