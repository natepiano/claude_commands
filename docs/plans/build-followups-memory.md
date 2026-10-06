# Build follow-ups: builds that fit in memory — plan

> **Status: planned.** Every build on natedev, CI's and the sessions', fits in memory because of how it's admitted, so earlyoom is a rare last resort and not a routine event. Target: a normal working day has no earlyoom kill.
> **Production: build-followups** — unit `stalls-unit`; production doc `docs/plans/build-followups-production.md`

## Context

- **Source:** the user, 2026-10-04 11:46 PDT: "we need a better way for everything to live together rather than having the failsafe constantly engaging". It was prompted by the tool-based-ui showrunner's earlyoom report (11:43 PDT), whose items are inputs here. On 2026-10-04 the user held its item 1 (CI's kill order) for this plan's design.
- **Measured by natedev, 2026-10-04 (earlyoom journal, gh):**
  - 49 earlyoom kills, all on 2026-10-04 (none on Oct 3), all SIGTERM. 45 were session builds and 4 were CI.
  - Episodes (PDT): 01:23 (1), 02:41 (1), 03:39 (1), 07:06 (1), 07:21 (2, CI), 09:21 (2), 09:31 (8), 10:07 (8), 11:12–11:13 (21, the minute after one build hold was released to every session at once; largest 1.9 GB), 11:28–11:30 (2 CI, 2 session).
  - CI runs hit: 37208624435 (one job killed at 07:21; passed on attempt 2), and 37224315720 (both Linux jobs, "Compile and Production Wiring" and "Test Suite", failed 11:28 and 11:30).
  - At 11:29 one session rustc (crate hana, bin, release with tracing) held 15,020 MiB on its own.
- **Why the failsafe fires:** two admission systems that can't see each other.
  - CI: 2 runners, `CARGO_BUILD_JOBS=8` each, in `hana-ci.slice` (MemoryMax 40G, which counts page cache; MemoryPeak reached 40G). It isn't in steve.
  - Sessions: steve (`/etc/nixos/modules/linux/jobserver.nix`) gives out 31 slots plus one free slot per build. It stops giving slots while MemAvailable is under 12 GiB, but a compiler grows after it's admitted.
  - earlyoom (`/etc/nixos/modules/linux/memory.nix`) SIGTERMs at about 3 GiB available while free swap is at or under 25%. The 8 GB swapfile was full at 11:47 PDT, so the swap condition is always met.
  - No session build has a cap, and app.slice must not get one (memory `connected-but-unreachable-is-memory-livelock`: a cap there stalls the sessions).
- **CI's kill order:** the GitHub runner sets every job process to oom_score_adj 500 (Runner.Sdk), overriding what it inherits; session processes are at 200. So the runner service's OOMScoreAdjust can't reach the jobs, and the setting that does is `PIPELINE_JOB_OOMSCOREADJ` in the runners' `extraEnvironment` (`/etc/nixos/modules/linux/hana-runners.nix`). natedev drafted `PIPELINE_JOB_OOMSCOREADJ = "0"` and set it aside until this design. Changing it restarts both runners at the next rebuild.
- **Your Phase 1 work** (`buildlog sample`, memory stalls) is the measurement to build on.

## Delegation Context

- **Project:** `~/.claude` (commands, skills, scripts; Python 3.13 and shell). Machine changes (`/etc/nixos`) go to natedev as an exact diff: you don't edit `/etc/nixos`.
- **Project started:** 2026-10-04T18:59:00.215+00:00
- **Files:** `scripts/delegate/verify.sh`, `scripts/validate_and_push/`, `commands/build_hold.md`, `scripts/build_hold/build_hold.py` (the hold, quiet and release helper), `scripts/production/dailies_render.py` (`--footer`), and any new shared script (one place, used by every caller). The showrunner settles any clash with the owning units.
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`, `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` and `python3 -m unittest discover -s scripts/production -p 'test_*.py'` when their files change, plus tests for every new script; `basedpyright` clean on changed Python; `bash -n`/`zsh -n` on changed shell.
- **Invariants:** a test never builds hana or loads the real machine, and never writes `~/.local/state/buildlog`. A Rust build for testing runs in a `git clone --local` under `/tmp`. Times carry their zone.

## Phases

### Phase 1 — Where the memory goes, and the design · status: done

#### As-built

Measured 2026-10-04 11:59–12:25 PDT, read-only, from the earlyoom journal, the build log and cgroup files. The design was approved whole by the user, 2026-10-04 PDT: "do what you think is best". The next phase's Work Order carries it.

- **Swap was full for 35 hours or more,** held by idle processes: 11 BRP MCP servers 2.3 GiB, the sccache server 1.8 GiB, 11 claude 0.75 GiB. earlyoom's swap condition was therefore always met, so it fired on RAM alone at 2.1–2.8 GiB available: 58 SIGTERMs on 2026-10-04 (54 session, 4 CI), no SIGKILL.
- **A session nextest step ran in all 11 kill episodes;** CI ran in 2. nextest steps peak at 37 GiB (p90 22 GiB, page cache included); mend and clippy at 12 GiB.
- **`--prefer` killed whatever matched first.** It killed 21 `ld.mold` linkers and five `cargo-nextest` processes of 22–33 MiB; each kill failed a whole step.
- **Every session compile runs in one session's cgroup.** `rustc-wrapper = "sccache"` hands each compile to the one shared sccache server, which starts rustc in the cgroup of whichever session launched it.
- **The 15,020 MiB rustc was an agent's own build:** `cargo build --release -p hana --features bevy/trace_chrome` with `[profile.release] debug = true`, outside the build log. The largest kill-time figures otherwise: debug test builds 6.8 GiB, and CI's release build (debug info off) 3.1 GiB.
- **hana-ci.slice:** MemoryPeak equals its 40 GiB MemoryMax, reached 3,930 times with no OOM kill. Its process memory read 5.8 GiB, the rest page cache. Its job processes get oom_score_adj 500 from the GitHub runner itself.
- **Quiet use outside builds:** about 20 GiB of 60.5. 32 CPUs.
- **Standard pieces available but idle:**
  - systemd-oomd runs but watches only ghostty scopes, and has killed nothing.
  - zram and zswap are off.
  - `user@1000.service` delegates cpu, memory and pids.
  - steve 1.5.2 sets only `--jobs 31 --min-memory-avail 12288`.
  - CI cannot open `/dev/steve` (mode 0600, `PrivateDevices=yes`).

**Files:** `docs/as-built/buildlog-memory-stalls.md` — gains the gotcha that a step's peak and stall leave out its compiles.
**Binds later work:**
- "One memory admission for every build" must put the sccache server in `builds.slice`. Moving only the steps would leave every compile outside the limit.
- The approved CI changes (steve, kill order, smaller slice) restart both runners at the rebuild.
**Ruled out:**
- systemd-oomd for builds: it kills a whole cgroup, and every session's compiles share the sccache server's one cgroup.
- Admission by each crate's measured peak: no source records it.
- A `cargo` shim: agents' compiles reach `builds.slice` through sccache.
- zswap: a cache in front of the full swapfile adds no capacity.
- nextest test-thread limits: none until the per-slice samples show test execution filling memory.

### Phase 2 — One memory admission for every build · status: done

#### As-built

- `invoke.sh` runs each step with `systemd-run --user --scope --slice=builds.slice`. While the slice's `memory.current` exceeds a non-`max` `memory.high`, `run_once` prints `waiting for memory since HH:MM PDT: builds use X.X of Y.Y GiB` once to stderr, rechecks every 5 s, and after 15 min prints `memory wait limit reached after 15 min; starting anyway`. A missing slice directory or file means no wait; `BUILDLOG_BUILDS_CGROUP` points the check at a test directory. The step's start time excludes the wait, and the waited seconds land in `steps.mem_wait_s`.
- `verify.sh` calls a step a memory kill when its log shows signal 9/15 or SIGKILL/SIGTERM and earlyoom (`sending SIG(TERM|KILL) to process`) or the kernel (`Memory cgroup out of memory` / `Out of memory: Killed`) killed a process inside the step's window. A memory kill is never the tree's lint or test failure: the step re-runs once, the last attempt decides, and a second memory kill exits 137 with `killed for memory twice: <step>`. No status of 128 or more is cached as the tree's failure.
- Buildlog index schema 6 adds `steps.mem_wait_s`, `samples.builds_anon_bytes` and `samples.ci_anon_bytes` (the `anon` line of each slice's `memory.stat`, null when absent). The report adds `memory waits: N steps, total T, longest L (caller)` or `memory waits: none`, and `peak builds X, peak CI Y (process memory)`, "unavailable" without samples.
- `rust_release.py` keeps a trial ended by a kill (status 128 or more, -9/-15, or signal 9/15 or SIGKILL/SIGTERM in its output) at `waiting` with reason "… killed; retry next night"; a genuine failure stays `failed`.
- The natedev diff sets: zram zstd 50% at priority 100 and `vm.swappiness` 100; earlyoom decides on available RAM alone (free-swap thresholds 100) and no longer prefers `cargo-nextest`; the user `builds.slice` (MemoryHigh 26G, MemoryMax 34G, MemorySwapMax 4G) holding an `sccache` user service (`SCCACHE_NO_DAEMON=1`, takes the port from an already running sccache server on start, RestartSec 5), both in `modules/linux/development.nix`; `hana-ci.slice` MemoryHigh 10G, MemoryMax 18G, MemorySwapMax 2G; each runner ordered after `steve.service` with `BindPaths=-/dev/steve` and `DeviceAllow` `char-rtc r` + `/dev/steve rw`, on top of 160efd9's ACL and `CARGO_MAKEFLAGS`; `CARGO_BUILD_JOBS` 8 kept; `PIPELINE_JOB_OOMSCOREADJ` 100 (HEAD 36a5cf4 has 0); the `buildlog` user unit, which runs the nightly Rust trial, in `builds.slice` at OOMScoreAdjust 200. Its header lists the activation steps, the sandbox open check and the after-rebuild checks.

**Files:**
- `scripts/lint/invoke.sh` — step slice and the memory wait
- `scripts/delegate/verify.sh` — memory-kill classification and the one re-run
- `scripts/delegate/test_verify_memory_kill.py` — verify.sh's functions against a stubbed `journalctl`
- `scripts/buildlog/{index,record,sample,report,rust_release}.py` and their `test_*.py` — schema 6, per-slice samples, report lines, trial kill state
- `docs/plans/build-followups-memory-nixos.diff` — the `/etc/nixos` change for natedev, not yet applied

**Binds later work:**
- The machine half is not live until natedev applies the diff and the user rebuilds with no CI job running (both runners restart). Until then the per-slice samples are null and the report says "unavailable"; after it, a persistent "unavailable" is a deployment fault.
- `CARGO_BUILD_JOBS` 8 stays until a CI log shows no `failed to connect to jobserver` warning; its removal is a later `/etc/nixos` diff item.
- `steps.mem_wait_s` is the memory wait; `steps.wait_s` remains the cargo-token wait.
- earlyoom kills are read from `journalctl -u earlyoom` lines `sending SIG(TERM|KILL) to process`; verify.sh's `killed for memory twice: <step>` (exit 137) is countable in run logs.
- The memory admission that "Build holds name their holders and release cleanly" builds on is the invoke.sh wait on `builds.slice` `memory.high` (26G).

**Gotchas:**
- earlyoom never logs "killed"; its line is `sending SIGTERM to process <pid> uid ... "<comm>": ...`.
- A pipe element runs in a subshell: anything `run_once` must keep (wait seconds, start time) happens before the pipe.
- nix-darwin imports `modules/common` and has no `systemd` option, and `lib.mkIf false` does not hide an undeclared option; Linux-only systemd units go in `modules/linux`.
- Inside a runner unit (PrivateDevices, DevicePolicy=closed) `/dev/steve` exists only with BindPaths and DeviceAllow; with an ACL alone `CARGO_MAKEFLAGS` yields a jobserver warning that hana's release job treats as failure (ci.yml:444).
- earlyoom counts zram's virtual size as swap; MemoryMax counts RAM only, and zram's compressed pages are charged to no slice.
- Shared steve slots alone do not stop kills: CI run 37227844227 lost both Linux jobs to earlyoom after 160efd9.
- natedev's clock and journal are EDT.

**Ruled out:** a `steve-clients` group (160efd9's ACL already grants the runner users, and group membership is fixed at login); removing `CARGO_BUILD_JOBS` before a CI log proves the jobserver connects.

### Phase 3 — Instruments for the measured day · status: done

#### As-built

- **Slice snapshots.** `buildlog snapshot` writes a `memory_snapshot` record stamped with its instant: per slice (`builds`, `ci`, or absent) `memory.events` (`high`, `max`, `oom_kill`), `memory.peak`, `memory.swap.peak`, the limits, and the `memory.pressure` `some` total as `stall_some_us` (or a named unavailable state), plus zram's data and compressed sizes. `buildlog sample` also writes one every minute: `sample.sample()` returns `SampleTaken(at, host)` and cli.py's `sample` command passes it to `memory.write_snapshot(at, host)`; a snapshot failure prints `memory snapshot unavailable` to stderr and never fails the sample.
- **Window report.** `buildlog memory START END` (ISO instants with offsets; `memory.window_report`) counts only this host's records (`store.host_name()`). It prints earlyoom and kernel OOM kills from the journal; then per slice, between the snapshots nearest each edge within 2 minutes (`SNAPSHOT_DISTANCE_S = 120`, else "no snapshot within 2 min of …"), the `high`/`max`/`oom_kill` deltas, the pressure delta as "stall +N.N s" (or "stall unavailable"), and peak and swap peak against their limits; then memory waits, minutes sccache ran outside its service, unsliced steps ("N fell back to a plain run, M never tried a scope"), memory kills and zram. Window edges round up to the next millisecond (`stored_bound`) to match stored records.
- **Report rules.** Journal unavailable (journalctl error or nonzero exit) prints "journal unavailable", never zero kills; an empty window is zero kills. memory.peak is a lifetime high, so it is the window's peak only when it rose in the window, else "at most X GiB (no new high in the window)". A counter (events, peak or stall) that falls in the window is a reset: "reset in the window; since the reset …". Memory kills read "N calls passed after a re-run, M failed later, K stopped after a step was killed twice".
- **Daily report.** `buildlog report` shows the same lines (`memory.instrument_lines`) under its memory heading, aggregated across hosts, even on a day with no samples.
- **Schema 7.** `samples.sccache_in_service` (1 when `sccache.service`'s cgroup holds a process, 0 when empty, NULL without `builds.slice`); `steps.slice` (`builds` / `fallback` / `none`, from the scope marker `invoke.sh` leaves); `calls.mem_kills` and `calls.mem_kill_stopped`, which verify.sh counts per call in a temp file and passes as `BUILDLOG_MEM_KILLS` and `BUILDLOG_MEM_KILL_STOPPED` (set on the second kill, the `killed for memory twice` exit) to `record.py call`; indexed `memory_snapshots`.
- **Where builds run** (live, 2026-10-04). The sccache server runs alone in `builds.slice/sccache.service`; cargo, its sccache client and some rustc processes run in `builds.slice/run-*.scope` (the client compiles non-cacheable work locally). Both are inside `builds.slice`, as is the `buildlog` unit that runs the nightly Rust trial.
- **Machine** (`/etc/nixos` diffs: `docs/plans/build-followups-memory-nixos-*.diff`). `sccache.service` runs its server in the foreground (`SCCACHE_START_SERVER=1` on ExecStart only). `hana-ci.slice` has MemoryMax 18G only, no MemoryHigh, and the runners `OOMPolicy=continue`, so a kill at MemoryMax takes one compiler, not the runner. `builds.slice` keeps MemoryHigh 26G as the admission line. The runners carry no `CARGO_BUILD_JOBS`: a clean CI run's Linux jobs used the steve jobserver with no `failed to connect to jobserver` warning.

**Files:**
- `scripts/buildlog/memory.py` — `snapshot`, `write_snapshot`, `window_report`, `instrument_lines`, `stored_bound`
- `scripts/buildlog/sample.py` — minute sample, `sccache_in_service`, `SampleTaken`
- `scripts/buildlog/cli.py` — `snapshot` and `memory` commands; `sample` writes the minute snapshot
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py` — schema 7 fields and the `memory_snapshots` table
- `scripts/buildlog/report.py` — instrument lines in the daily report
- `scripts/delegate/verify.sh` — memory-kill count and twice-killed stop
- `scripts/buildlog/test_memory_window.py`, `test_memory_records.py`, `test_sample.py`, `test_report.py`, `test_index.py`, `scripts/delegate/test_verify_memory_kill.py` — stub the cgroup tree, `journalctl` and `zramctl`; never read the real cgroup tree or journal, never write `~/.local/state/buildlog`

**Binds later work:** the measured working day runs `buildlog memory START END` per window and per CI run, and takes a run's own peak from the minute samples (`ci_anon_bytes` / `builds_anon_bytes`), never from memory.peak. The window report is host-scoped; the daily report is not. Snapshots exist only from when this code is live on `~/.claude` main (about 830 KB a day). The CI jobserver check is closed.

**Gotchas:**
- `sccache --start-server` forks the server and exits, so a service using it dies at start; with `SCCACHE_START_SERVER` in the environment every other sccache command fails, hence ExecStart only.
- MemoryHigh on a slice that holds a heartbeat (the runner's Runner.Listener) stalls it until GitHub drops the runner; such a slice gets MemoryMax only and its services `OOMPolicy=continue`.
- memory.peak never falls without a reset; CI's reads above its 18G limit, from before the limit existed.
- At its ceiling CI hits MemoryMax thousands of times with no kill: a large `max` delta is reclaim, and its cost shows as stall seconds.

**Ruled out:** a `CARGO_BUILD_JOBS` fallback on the runners (the steve jobserver reaches CI jobs); a 10-minute snapshot search (an edge's snapshot more than 2 minutes off misplaces the window); a per-call kill count alone (it cannot tell two single kills from one step killed twice, hence `mem_kill_stopped`).

### Phase 4 — Build holds name their holders and release cleanly · status: done

#### As-built

- Each holder file in `~/.local/state/build-hold/` (`BUILD_HOLD_DIR` overrides it) is one JSON line `{holder, since, for, release_eta}`; the legacy one-line free-text form is still read, and an unreadable file stays a hold. Types: `HoldState = NoHolders | ActiveHolders` (non-empty, ordered by `since`), `ReleaseEta = KnownRelease | UnknownRelease`, `ReleaseRequest = NoReleaseEta | ClockReleaseEta(clock, zone)`. `scripts/buildlog/rust_release.py` treats any regular file in the directory as a hold.
- CLI `scripts/build_hold/build_hold.py`, called by `commands/build_hold.md`: `hold --holder --for [--release-eta HH:MM --zone <IANA>]` (zone required with an ETA; the ETA must be later today in that zone), `quiet [--max-wait, default 600 s]`, `release --holder`, `status`. `release` prints `released, builds may resume.` only when no holder file remains, else `released; still held by <holder> (for …, release eta …)`.
- `quiet` is busy while this user's cargo, rustc or cargo-nextest run (`your builds still running: 7 cargo, 5 rustc`) or the 1-minute load is at or above cores/4 (it names the five heaviest processes); `ps` reads `user:32=`; an unknown core count reads as one core.
- `dailies_render.py` reads holds through `read_dailies_hold()` and prints one `build hold: <holder> since HH:MM <zone>, for <purpose> - release eta: <in N min | overdue | unknown>` line per holder in the report and `--footer`; the countdown uses real instants, so it stays right across a clock change. It refuses a top-level `build_hold` field, a unit marker with no holder file, active holders with no marked unit, and production plumbing words in a holder purpose; each refusal names the holder or its fix (re-hold).

**Files:**
- `scripts/build_hold/build_hold.py` — holder files, hold state, quiet check, release decision, CLI; `test_build_hold.py` beside it — scratch `BUILD_HOLD_DIR`, injected clock, load and processes
- `scripts/production/dailies_render.py` — hold lines and marker checks from the holder files; `test_dailies_render.py`, `test_dailies_render_holds.py` — file-backed hold cases
- `scripts/buildlog/test_rust_release.py` — a JSON holder file still holds the nightly trial
- `commands/build_hold.md`, `commands/showrunner/produce.md`, `commands/showrunner/dailies.md` — the command calls the helper; holds come from the holder files
- `pyrightconfig.json` — execution environments for `scripts/production` and `scripts/build_hold`, so both import `build_hold` as a sibling module

**Binds later work:** Holder files are the one source of hold state for `build_hold.py status`, the dailies renderer and the nightly Rust trial. The release decision (`released, builds may resume.` only when no holder file remains) is what a one-session-at-a-time release builds on; `release_hold` is still a broadcast to every session. Unit markers are checked only as "some unit marked iff some holder active", never against the holder's own unit. Any hold with an ETA passes `--zone`.

**Gotchas:** procps cuts user names over 8 characters (`natepia+`) unless the format says `user:32`; an `HH:MM` release ETA must name its zone, because natedev's clock is EDT while sessions state PDT; a holder file can vanish between listing and reading during a concurrent release, and reading skips it.

**Ruled out:** passing hold details as renderer flags (`--build-hold-release`, the `build_hold` input field) — the holder files are the one source.

### Phase 5 — Held units and unknown core counts · status: done

#### As-built

- `parse_report` in `scripts/production/dailies_render.py` refuses a unit marker whose `unit` equals an active holder's name, since that unit runs the held test: `InputError("units.build_hold: <unit> holds the build hold itself; remove its marker")`, exit 2. While any holder file remains, every other marked unit stays held and keeps `build hold` after a partial release, with one hold line per holder in the report and footer.
- `scripts/build_hold/build_hold.py` has `Cores = KnownCores(count) | UnknownCores` (frozen dataclasses). `read_cores()` converts `os.cpu_count()` at that boundary and is the injectable `cores` reader.
- `quiet_verdict(load_1m, cores, processes, user)` takes `Cores`. With `UnknownCores` it skips the load comparison and adds the reason `core count unavailable, so the load limit cannot be judged`, so the verdict is `Busy`, still listing this user's running builds; `KnownCores(32)` at load 1 with no builds is `Quiet`.
- `ReleaseEta = KnownReleaseEta | UnknownReleaseEta` (formerly `KnownRelease`/`UnknownRelease`) in `build_hold.py`, `dailies_render.py` and every test.
- `draw_from` sets the latest cell only when `latest_cell > eta_cell`, so a row whose ETA and latest time fall in one hour keeps its ETA marker in both chart styles.

**Files:**
- `scripts/build_hold/build_hold.py` — core-count states, `read_cores()`, ETA states; `test_build_hold.py` beside it — unknown-core and known-core verdicts
- `scripts/production/dailies_render.py` — the holder's-own-unit refusal, the same-hour ETA cell; `test_dailies_render_holds.py` — two marked units through a partial release, the own-unit refusal; `test_dailies_render.py` — the same-hour ETA marker in both chart styles
- `commands/showrunner/dailies.md` — the unit `build_hold` row: a holder's own unit is never marked

**Binds later work:** A unit's `unit` field is its session name, the same name a holder writes as `holder`; markers are checked against active holders' own units. The ETA types are `KnownReleaseEta`/`UnknownReleaseEta`, and the old names no longer exist. Any load-against-cores comparison matches on `Cores`, never on `None`.

**Gotchas:** With `UnknownCores`, `quiet` waits its full `--max-wait` before reporting busy, since the verdict cannot change; `os.cpu_count()` is never `None` on Linux, so the path is rare. `dailies_render.py --footer` needs `--zone`.

### Phase 6 — verify.sh refuses an example whose tests never run · status: done

#### As-built

- `verify.sh test <pkg>` as a gate run (no `--filter`, no named target; `--features` allowed) and `verify.sh final` read `cargo metadata --no-deps --format-version 1` once into `GATE_METADATA` and pipe it to `UNTESTED_EXAMPLES_PY` with the package name, or `--workspace` for `final`. The check runs before the cache lookup, any build and `final`'s format check, so a recorded pass never stands in for it; `--filter` and named-target runs are feedback runs and skip it.
- An offender is an example target with `test` false whose source holds a line matching `^\s*#\[cfg\(test\)\]`; the first match gives the file and line. A directory example (`examples/<name>/main.rs`) is scanned through every `.rs` file under its directory.
- Each offender gets one block on stderr, blocks separated by a blank line, then exit 2 with nothing built: `example <name> holds tests that never run: <file>:<line> has #[cfg(test)], and cargo builds examples with test = false.` The fix follows: `in <manifest>: add test = true to its [[example]] entry` when the manifest (read with `tomllib`) already names the example, otherwise `in <manifest>:` and the three lines `[[example]]` / `name = "<name>"` / `test = true`.
- `read_metadata` prints `GATE_METADATA` when set and runs `cargo metadata` otherwise; `require_member` and `take_test_targets` read through it, so a gate run reads metadata once.
- The header's `test` and `final` usage lines say each refuses examples with tests cargo skips.

**Files:**
- `scripts/delegate/verify.sh` — `UNTESTED_EXAMPLES_PY`, `read_metadata`, the check before `cache_lookup`, header comments
- `scripts/delegate/test_verify_untested_examples.py` — eight scratch-package cases through real verify.sh routing: real `cargo metadata`, a cargo stub on `PATH` that passes `metadata` to real cargo and logs every other call, and a git stub that fixes the pass-record key

**Binds later work:** Phase 19's verify.sh acknowledgement goes after this refusal, at the first build step past the memory wait. A new verify.sh routing test follows `test_verify_untested_examples.py`'s stub pattern.

**Gotchas:** A failing `cargo metadata` exits with cargo's status under `set -e`, not 2. hana main has 7 offending examples; the ~/.claude main merge is held until tool-based-ui adds `test = true`.

**Ruled out:** Python 3.10 support for the check: the repo already needs 3.11+ (`tomllib`).

### Phase 7 — Unknown CI queue times · status: done

#### As-built

- `class JobRunState(StrEnum)`: `RAN = "ran"`, `SKIPPED = "skipped"`, `CARRIED_OVER = "carried_over"`, decided once per job at index time and stored in `ci_jobs.run_state`. `carried_over` when the run attempt is above 1 and `started_at` precedes `created_at`; else `skipped` when the conclusion is `skipped`; else `ran`. The rule reads only the row's own stamps, so file and attempt order never matter.
- Only a `ran` row stores `duration_s` and `queued_s`; `carried_over` and `skipped` rows hold NULL for both. A `ran` job's `queued_s` is `started_at - created_at` when both stamps parse and the result is zero or more, else NULL (unknown, never negative).
- `elapsed_seconds(start, end) -> float | None` returns the unrounded difference; `seconds_between()` wraps it, rounded to 3 places.
- `ci_job_days` leaves `carried_over` rows out of every column and keys days by `date(created_at,'localtime')`.
- `ci_section` counts the day's jobs through their run attempt (`JOIN ci_runs` on `run_id` and `attempt`, day = `date(r.started_at,'localtime')`), averages known `queued_s`, and counts non-skipped jobs with NULL `queued_s` as left out: `jobs queued <avg> on average.`, `jobs queued <avg> on average, <N> without a known queue time left out.`, or `no job has a known queue time` + ` (<N> left out)` when N > 0 + `.`.

**Files:**
- `scripts/buildlog/index.py` — `JobRunState`, `run_state`, queue and duration rules, `ci_job_days`, `SCHEMA_VERSION = 8`
- `scripts/buildlog/report.py` — `ci_section`'s day key, average and clause
- `scripts/buildlog/test_index.py`, `test_report.py` — the run states, zero queue, view exclusion, clause forms, midnight day key

**Binds later work:** the build-report Waiting phase's CI queue row reads only `ran` jobs with a known `queued_s` and keys days as `ci_section` does; it also re-keys `ci_job_days` by run attempt. The next schema change bumps `SCHEMA_VERSION` from 8 (`open_for_update` rebuilds on any `user_version` mismatch).

**Gotchas:**
- On a re-run, GitHub copies every finished job into the new attempt under a new `job_id`, `created_at` at the re-run, `started_at`/`completed_at` from the earlier attempt; read raw, it gives a negative queue time and a second copy of the duration.
- `ci_section` keys a job's day by its run attempt's `started_at`, `ci_job_days` by the job's `created_at`; a job created just after local midnight in a run started before it lands on different days.

**Ruled out:** keying `ci_section` by job `created_at` (drops post-midnight jobs of a run started before midnight); looking up each re-run job's earlier-attempt twin (the stamp rule marks the same rows).

### Phase 8 — A Codex seat survives "model at capacity" · status: done

#### As-built

- `scripts/agents/codex_mesh.py` types every ending. A turn ends as `TurnCompleted | TurnRefusedForCapacity | TurnFailed`, read in `_attach_and_run`; capacity comes from the error's structured field (`serverOverloaded`, `flexUnavailable`) or the message `Selected model is at capacity`. A run ends as `RunCompleted | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted`. The fast-failure retry on a new app-server (`_retry_warranted`) runs only for `FailedBeforeThread`. A disconnect after `turn/start` is `FailedWithThread`, so the prompt is never sent twice.
- Capacity resumes the same thread on the same roster entry. Waits start at 30 s, double up to 5 min, and stop after 20 min of waiting; the waits are injectable. Each wait writes one seat-log line with the retry number and when the next turn starts. The resume turn tells the delegate that its last turn stopped for capacity and that its edits are already in the tree. There is no second `thread/start` and no re-sent prompt. If a live peer turn is found first, it is streamed and the resume starts after it.
- Roster statuses: during backoff the entry is `waiting_capacity` and holds the thread id and `launcher_pid`. Once the waits run out it becomes `capacity_exhausted`, and `start` exits 1 with `codex_mesh: <seat>: model still at capacity after <N> retries over <M> min; thread <thread id> stays on the roster (codex_mesh.py end --to <seat>)`.
- `send` to a `waiting_capacity` seat puts the message in `<seat>.pending.json` in the session dir, under an flock, and the resume turn delivers it. That path never calls `thread/queue/add`, and a capacity failure moves items already in the server queue into the same store. `send` to a `capacity_exhausted` seat, or to a seat that `end` has marked, refuses with exit 2. `end` treats both statuses as live: it drops queued and pending messages, then interrupts any live turn.
- Liveness comes from a `thread/read` call that never steers the thread, typed `ThreadLive(turn_id) | ThreadActiveWithoutTurn | ThreadIdle | ThreadStateUnknown(reason)`. A relaunch replaces an entry only on `ThreadIdle`. Otherwise it exits 2, names the thread and `end` (or gives the unknown reason), and starts nothing. While a `waiting_capacity` entry's `launcher_pid` is alive, a relaunch refuses whatever the server reports. Once that pid is dead, the relaunch replaces the entry and drops the old thread's pending messages with a log line.
- On every failure path, before `start` returns, a live turn is either streamed to its end or interrupted. An unreadable thread is never treated as idle: `end` exits 1, and the launcher leaves through the failure path that interrupts any live turn. `_retire_server` leaves the roster untouched.

**Files:**
- `scripts/agents/codex_mesh.py` — the launcher: typed turn, run and liveness outcomes; capacity backoff and resume; the pending store; the relaunch refusal; cleanup of unwatched turns
- `scripts/agents/test_codex_mesh.py` — an in-process WebSocket stub app-server that speaks `Client`'s JSON-RPC frames, with injected waits; 41 cases, and no real codex runs

**Gotchas:**
- On an idle thread, the real app-server opens a turn for each queued message. Never `thread/queue/add` to a thread that no launcher streams. The stub models this, and without it the `send` tests pass for the wrong reason.
- `thread/read` can report a thread as active with no turn id (`ThreadActiveWithoutTurn`). Get the id from a steer probe with an empty `expectedTurnId` before calling `turn/interrupt`; `end` and cleanup interrupt only a real turn id, and say so when they cannot.

**Ruled out:**
- Queuing a message to an out-of-retries thread: it would open a turn that nothing streams.
- Changing the service tier: the pacer stayed on `default` through every capacity stop.

### Phase 9 — The build report opens with the day's waits · status: done

#### As-built

- `report()` in `scripts/buildlog/report.py` puts `### Waiting` right after `## Builds, <day>`, before the kind sections; the `memory waits:` line under Memory pressure is unchanged. Rows, in order: `Build-folder turn` (`calls.token_wait_s > 0`, `<n> of <calls> calls`, by `worktree_name`), `Memory admission` (`steps.mem_wait_s > 0`, `<n> of <steps> steps`, by `worktree_name`, naming the seat when the step has one), `CI queue` (`ci_jobs.queued_s > 0` over `ran` jobs with a known queue time, `<n> of <jobs> jobs`, `Worst` as `<workflow> / <job name>`).
- Columns: `Wait`, `Longest` (the wait, who, and the time with its zone), `Over 5 min` (above 300 s), `Waited`, `Total` (seat-hours; job-hours for CI), `Worst` (top three by summed wait, in minutes). Durations use `seconds()`; a kind with nothing on the day prints `none` and blank cells after it; no averages.
- verify.sh times the cargo token acquisition, a timed-out wait included, and passes it as `BUILDLOG_TOKEN_WAIT_S`; the call record stores `token_wait_s`, 0 when the value is not a whole number. Index schema 9 adds `calls.token_wait_s INTEGER` (0 for a record without it) and keys `ci_job_days` by the run attempt's `started_at`, the same day `ci_section` and the CI queue row use. `wait_s` keeps its meaning: metadata, pass-record lookups and the token wait together.
- `launches.collect()` turns each `brp_launch` result in `~/.claude/projects/*/*.jsonl` (`BUILDLOG_TRANSCRIPTS` replaces the root) into a `build` step with caller `brp-launch`: `ended_at` is `metadata.launch_timestamp`, `started_at` that minus the launch's duration, `cwd` the crate, repo and worktree from `parameters.path`, `session` the transcript's id, argv `cargo build --workspace --bin <target>` when `metadata.launched_as` is `app`, else `--example <target>`, `--release` when set, and no memory fields (the build runs in the session's own scope). It reads `mcpMeta.structuredContent`, list `tool_result` items, and `<task-notification><result>JSON</result>` strings in user message and queue-operation content.
- Each launch is its own file, `launch-<sha256(session:timestamp)>.jsonl` in the host folder, written to a temporary file and hard-linked into place, so a repeat run writes nothing twice. `launches_state.json` in the store root holds each transcript's offset, head and tail marks, so a repeat run reads only new lines.
- `day_report` collects launches before `index.update()`, so `buildlog report` shows a launch in the same invocation. `CALLER_LABELS` maps `brp-launch` to `example launches (brp)`, a duration covering build and app start; launches from temporary folders group under their worktree, not as scratch (`GROUP_AS_SCRATCH`), and the host count follows the same rule.

**Files:**
- `scripts/buildlog/report.py` — the Waiting section; the `brp-launch` label and grouping
- `scripts/buildlog/launches.py` — the launch collector
- `scripts/buildlog/cli.py` — `buildlog launches`; `day_report` collects before `index.update()`
- `scripts/buildlog/record.py` — `token_wait_s` on the call record; `optional_int` returns None for a non-integer
- `scripts/buildlog/index.py` — schema 9, `calls.token_wait_s`, `ci_job_days` by run attempt
- `scripts/delegate/verify.sh` — the cargo token wait measurement
- `scripts/buildlog/test_{report,launches,index,record}.py`, `scripts/delegate/test_verify_token_wait.py` — the cases; the last drives real verify.sh routing with a cargo stub on `PATH`

**Binds later work:** index schema 9 and `calls.token_wait_s`; the Waiting section as the report's home for waits; `brp-launch` steps carry no memory fields; "Launches started without a path are recorded" re-reads `launches_state.json` once; `test_verify_token_wait.py` is the verify.sh routing harness later verify.sh tests extend.

**Gotchas:**
- A long `brp_launch` finishes as an MCP task, so its result is a `<task-notification>` string, not a `tool_result`; `binary_path` appears for examples too, so only `metadata.launched_as` names an app.
- A launch without `parameters.path` is skipped (122 of 360 real launches); `metadata.workspace` holds only the folder name.
- The Build-folder turn row reads `none` until verify.sh with the token wait is on `~/.claude` main.
- Fixtures in a made-up result shape missed the traps above; transcript fixtures copy real lines.

**Ruled out:** a Claude Code hook on `brp_launch` (a settings change); a buildlog record written from inside bevy_brp_mcp (a published crate in another repository); appending launches to the host's monthly file (the per-launch file makes the record id the dedupe); counting a timed-out token wait as zero (it is real queueing time).

### Phase 10 — A build waits for free memory, not for the slice's soft limit · status: done

#### As-built

- `buildlog_wait_for_memory` holds a step while `MemAvailable` in `${BUILDLOG_MEMINFO:-/proc/meminfo}` is under 12 GiB (12582912 kB, steve's slot floor), polling every `BUILDLOG_MEM_POLL_S` (5 s) up to `BUILDLOG_MEM_WAIT_LIMIT_S` (900 s), then prints `memory wait limit reached after 15 min; starting anyway`.
- Its first read below the floor prints once to stderr `waiting for memory since HH:MM PDT: the machine has X.X GiB free; a build starts at 12.0`. A missing or unreadable file, or no `MemAvailable: <digits> kB` line, means no wait.
- `run_once` gates every step that compiles, recorded or not (`BUILDLOG_SCOPE=0` too); `buildlog_step_compiles` lets `*/sweep.py` and `cargo [+toolchain] fmt` start at once.
- The clock starts at the first below-floor read, so `BUILDLOG_MEM_WAIT_S` → `steps.mem_wait_s` is non-zero only when the gate waited; a recorded step's `started_at` is taken after the wait.

**Files:**
- `scripts/lint/invoke.sh` — the gate, `buildlog_step_compiles`, the call in `run_once`
- `scripts/buildlog/test_record.py` — admission cases on fixture meminfo files, incl. a FIFO slow read above the floor and sweep/fmt exemptions
- `scripts/delegate/test_verify_token_wait.py` — routing fixture points `BUILDLOG_MEMINFO` at a 48 GiB meminfo

**Binds later work:** a Memory admission entry means a real wait below 12 GiB MemAvailable; the gate's three ends (released, limit reached, no reading → no wait) map to `PastMemoryWait`'s `Granted`, `TimedOut`, `MeminfoUnavailable`; the disk-floor sweep is never held by the gate.

**Gotchas:**
- `invoke_sweep` runs `sweep.py` through `run_once`, so any step-level gate reaches the sweep unless `buildlog_step_compiles` exempts it.
- The gate sets `BUILDLOG_MEM_WAIT_S` in the calling shell, so it runs before the `buildlog_exec | tee` pipe.
- The limit message says "15 min" whatever `BUILDLOG_MEM_WAIT_LIMIT_S` holds.
- Tests never read the real `/proc/meminfo`; `memory.py` and `sample.py` still read `BUILDLOG_BUILDS_CGROUP`, the gate does not.

**Ruled out:** a `memory.high` gate — `builds.slice` has no MemoryHigh (MemoryMax 44G), and `memory.high` counts page cache, throttling builds not short of memory; a per-run memory cap in verify.sh — widget's drawn-pixel test group already bounds the largest step.

### Phase 11 — A sweep that clears build caches for something else says so · status: done

#### As-built

- `hold_floor` (`scripts/lint/sweep.py`) writes `floor.json` in `${LINT_SWEEP_STATE_DIR:-~/.local/state/lint-sweep}` under `FLOOR_LOCK` after each floor sweep: its time, free bytes and build-cache bytes measured once removals finish (a failed removal stays counted), and the last delivered alert. Read states: `NoFloorRecord | FloorRecord`, `NoDeliveredAlert | DeliveredAlert`.
- Build-cache bytes = the idle target dirs the sweep scans + busy target dirs + `/var/lib/hana-ci/hana-linux-1` and `-2` (never swept); the last two are measured once per sweep with `directory_blocks`.
- After a sweep that removed output, it alerts when free space fell more than 10 GiB beyond build-cache growth since a floor sweep at most 30 min earlier, or when the sweep removed more than 32 GiB. 32 GiB sits between the largest ordinary sweep (29.2 GiB) and the smallest of three driven by frame's timing traces (35.2 GiB) on 2026-10-04 PDT. With no earlier record, or one over 30 min old, it writes the record and sends no growth alert.
- One text goes to `scripts/message/send.py --to natedev --from disk_floor --timeout 30` (stdin) and `scripts/notify/pushover.py --priority 0 "natedev: build caches swept" <text>`. At most one an hour, counted from the last alert a channel delivered (send.py exit 0, or 1 = queued; Pushover exit 0); both failing leaves the next qualifying sweep free to send. Each channel's result prints; a failed send prints to stderr and leaves the sweep's exit status unchanged.
- The text: sweep time in PDT, bytes removed, the threshold that fired; fall and cache growth since the previous sweep, naming its time; buildlog-disk's two measurement times, a snapshot over 15 min old named as such; measured-folder growth from `outside_build_cache_totals` and how much of the fall beyond cache growth it covers (clamped, never negative; omitted with no earlier floor sweep), the rest named as outside the measured folders; the top 3 grown directories outside the caches with growth and size, plus the largest child when it holds more than half that growth. Signed changes are worded by sign; `~` for home and middle-shortened paths keep it under Pushover's 1,024 characters.
- `disk.json` is read by path from `${BUILDLOG_DIR:-~/.local/state/buildlog}` into `AvailableDiskMeasurement` or `UnavailableDiskMeasurement` with its reason (missing, unreadable, or a snapshot without the new fields), which the text prints as "buildlog-disk measurement unavailable (<reason>)". With no earlier measurement, the text names the three largest directories outside the caches.
- `buildlog disk` (`scripts/buildlog/disk.py`, every 10 min) adds `outside_build_caches` (directories one and two levels under each `FOLDERS` entry holding ≥1 GiB outside cargo target dirs, i.e. dirs with `.rustc_info.json`; bytes, growth since the previous snapshot's `measured_at`, a new directory growing by its whole size, largest child, `child_bytes`), `outside_build_cache_totals` per measured folder, and `previous_measured_at`. A hard-linked file counts once. Existing fields stay, so `scripts/buildlog/report.py`'s disk table reads both formats; a prior snapshot without totals counts as no earlier measurement.

**Files:**
- `scripts/lint/sweep.py` — floor record, thresholds, alert text, sends
- `scripts/lint/test_sweep.py` — alert, delivery, record and text cases
- `scripts/buildlog/disk.py` — outside-cache directories and folder totals with growth
- `scripts/buildlog/test_disk.py` — exclusion, hard links, growth, deletion, a snapshot without the new fields
- `scripts/buildlog/test_report.py` — the disk table reads old and new snapshots

**Binds later work:** `floor.json` and `disk.json` hold only the latest sweep and measurement, so past sweeps come from `journalctl --user -u disk-floor.service`; `sweep.py` reads `disk.json` by path and never imports `scripts/buildlog`, since `disk.py` imports `sweep` at load.

**Gotchas:**
- send.py exits 1 when it queues a message; that counts as delivered.
- `disk.py` walks each folder twice (all bytes, then outside target dirs); the first walk is the cost (35 s under load average 104), the second adds about 3 s.
- A file hard-linked into both a target dir and a non-target dir counts outside the caches, so `/tmp`'s outside total can exceed its row.
- buildlog-disk measures `~/rust`, `/tmp` and the CI folders; the sweep also covers `~/.local/state`, so growth there reads as outside the measured folders.
- `hold_floor` runs from every build step's background sweep and from disk-floor's 2-minute timer (`sweep.py --floor-only`); `FLOOR_LOCK` lets one run at a time.

**Ruled out:** changing disk-floor's own 300 GiB high-priority alert, or stopping the sweep when growth comes from elsewhere — the floor still holds, and the alert is how the cause gets stopped; a fresh `du` of `/` (directories come from buildlog-disk); a third walk for folder totals (counted from the outside walk).

### Phase 12 — Launches started without a path are recorded · status: done

#### As-built

- `launch_record(line, transcript, *, backfill=False) -> RecordedLaunch | LaunchOutcome` (`scripts/buildlog/launches.py`) takes the launch location from `parameters.path`, else `metadata.working_directory`; with neither it returns `LaunchOutcome.NO_LOCATION`. The other outcomes are `NOT_A_LAUNCH` and `NO_RESULT`; `RecordedLaunch.step` holds the step record, whose `cwd` is the working directory when present, else the location.
- Repo and worktree fields come only from `record.git_facts(location)`: a path anywhere inside a worktree, a crate folder included, records the worktree's top level; a path outside any repository, or in a removed worktree, records `worktree` null and counts as unresolved.
- A `launches_state.json` entry without `reread_complete` has its transcript re-read once from the start; launches on lines before that entry's saved offset are recorded with `branch` and `sha` null. Every entry written now carries `reread_complete: true`. `save_launch` writes `launch-<sha256(session:timestamp)>.jsonl` only when absent, so an interrupted re-read writes no launch twice.
- `collect() -> LaunchCounts` (`recorded`, `not_a_launch`, `no_result`, `no_location`, `unresolved`, `added`) covers its one pass. `LaunchCounts.summary()` is printed by `buildlog launches` on stdout and by `buildlog report` on stderr, which keeps the report's markdown alone on stdout.
- On the real log the fallback took stored launches from 241 to 366 (37 with an unknown worktree); the one-time re-read took 9–16 s.

**Files:**
- `scripts/buildlog/launches.py` — the location fallback, the outcomes, the one-time re-read and `LaunchCounts`
- `scripts/buildlog/cli.py` — prints the counts line for `buildlog launches` and `buildlog report`
- `scripts/buildlog/test_launches.py` — fixture transcripts and git worktrees under a temporary root

**Binds later work:**
- Counts are per pass: a second pass reports 0 with every launch stored, so a launch count comes from the stored `caller: brp-launch` steps, and that count reflects this collector only once it runs from `~/.claude` main.
- Branch and SHA are collection-time facts, never launch-time ones; back-filled launches carry neither.
- Skipped results (`no_result`, `no_location`) are counted per pass and stored nowhere.

**Gotchas:**
- The collector skips any line without `brp_launch` (or without a `structuredContent`, `tool_result` or `<task-notification>` marker) before parsing it; without that filter one pass reads every transcript line (1.58 M) and takes 16 s.
- A result repeated in a task notification is counted once: each pass dedups recorded and `no_location` launches by `session:timestamp`.

### Phase 13 — The example-test guard returns: examples carry no tests · status: done

#### As-built

- `verify.sh test <pkg>` as a gate run (no `--filter`, no named target; `--features` allowed) and `verify.sh final` run `UNTESTED_EXAMPLES_PY` before the cache lookup, any build and `final`'s format check, so a recorded pass never stands in for it. `--filter` and named-target runs skip it.
- The rule: an example target of the selected packages is an offender when its source holds a line matching `^\s*#\[(cfg\(test\)|(\w+::)*test)\]` (`#[cfg(test)]`, `#[test]`, `#[tokio::test]`), whatever its `test` setting; `// #[cfg(test)]` does not match. A directory example (`examples/<dir>/main.rs`, whatever the target's name) is scanned through every `.rs` file under its directory; the first match gives the file and line.
- The refusal: one block per offender on stderr, a blank line between blocks, then exit 2 with no cargo call besides `metadata`:
  `example <name> holds a test: <file>:<line> has <attribute>, and examples carry no tests.`
  `move it into <package>'s src/ or tests/, or delete it.`
- The guard reads `cargo metadata --no-deps --format-version 1` once into `GATE_METADATA`; `require_member` and `take_test_targets` read through `read_metadata`, which reuses `GATE_METADATA` when set.
- `example-test` is removed: any call exits 2 with `verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).` on stderr, before `--no-cache` parsing, the cache, the progress activity or any cargo call.
- Header: the `test` and `final` usage lines say each refuses an example that holds a test; `test` still includes examples with `test = true` (`TEST_TARGETS_PY`). The `example` verb and everything after the guard (memory wait, tokens, pass records) are unchanged.

**Files:**
- `scripts/delegate/verify.sh` — the guard, its rule and refusal, `read_metadata`/`GATE_METADATA`, the removed verb, the usage lines
- `scripts/delegate/test_verify_untested_examples.py` — 13 routing cases through real verify.sh; cargo on `PATH` passes `metadata` to real cargo and logs every other call, git is stubbed; the environment drops `PLAN_DELEGATE_BOARD_DIR`/`PLAN_DELEGATE_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILDLOG_MEMINFO`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`
- `commands/lint_config.md`, `config/lint.conf` — no longer name `example-test`

**Binds later work:**
- The one-session release's acknowledgement goes after this refusal.
- The routing test `scripts/delegate/test_verify_untested_examples.py` is the stub pattern (cargo passes `metadata` through, git stubbed) for verify.sh routing tests.

**Gotchas:** A failing `cargo metadata` in the guard exits with cargo's status under `set -e`, not 2.

**Ruled out:**
- Keying the guard on the target's `test` setting or advising `test = true`: examples carry no tests (user, 2026-10-04).
- Rewriting `scripts/buildlog/index.py`'s `verb` column description, which still lists `example-test`: it documents recorded rows, which include the old verb.
- Failing closed when a build token is not acquired: the step still runs, and cargo's own folder lock serializes one folder.

### Phase 14 — A failed Codex seat relaunches · status: done

#### As-built

- `command_start` treats a `failed` roster entry as having no launcher, because `failed` is always its launcher's last write; the old thread no longer holds the slot, and the relaunch starts a new thread on the same server. This is the one exception to "a relaunch replaces an entry only on `ThreadIdle`". With no recorded server it starts a new server and never reads the old thread.
- With a recorded server it first repeats the dead launcher's cleanup on the old thread through `_end_unwatched_turn(port, thread_id, log_path) -> UnwatchedTurnCleanupResult` (`RelaunchAllowed | RelaunchBlockedByLiveTurn`), logging to the seat's `--log-file`: drop queued messages, interrupt a live turn.
- A refused `turn/interrupt` re-reads the thread. Still `ThreadLive`, or `ThreadActiveWithoutTurn`, logs `thread <id> could not be interrupted (<reason>)` and blocks: exit 2 with `codex_mesh: <seat>: thread <id> still has a live turn that could not be interrupted; relaunch once it ends`. An unreadable thread or unknown status (e.g. `systemError`) or a connection failure is logged and relaunched. The launcher's own exit paths ignore the result.
- `running` and `done` entries keep the `ThreadIdle`-only relaunch and refuse on a live turn or an unreadable thread; a `waiting_capacity` entry with a live launcher still refuses; `failed` stays outside `ENDABLE_STATUSES`.

**Files:**
- `scripts/agents/codex_mesh.py` — `command_start`'s failed-entry relaunch; `_end_unwatched_turn` and its typed result.
- `scripts/agents/test_codex_mesh.py` — six `test_failed_seat_*` cases (unknown status, interrupt before `thread/start`, refused interrupt with the turn still live or ended, active turn with no id, no server record); the stub app-server's `thread/read` answers a named status per thread.

**Gotchas:** `turn/interrupt` answers with an error when the turn ended between the read and the call; only a re-read tells that apart from a turn still live.

**Ruled out:** adding `failed` to `ENDABLE_STATUSES` (a relaunch no longer needs `end` first); recording `launcher_pid` on a failed entry (the status is always the launcher's last write).

### Phase 15 — Every dailies shows each active agent's week · status: done

#### As-built

- Every dailies report carries `### Agents` after the build-hold lines and directly above `waiting on you:` (above the closing time line when nothing waits): one line per `*.md` in `AGENTS_DIR` (`~/rust/hanadocs/agents/`) whose frontmatter says `state: active`, in file-name order, named by file stem; `- none active` when no note is active. The `--footer` form writes no Agents section, and the input JSON is unchanged.
- The line format (user, 2026-10-05): `- codex 2: 78% of the week used; runs out about 20:45 PDT today, before its Sun 02:25 refill; 1 reset available until Oct 29`. Used is `100 − weekly_remaining_usage` written with `:g`; at 100 or more the middle clause reads `out until its <refill> refill`; a refill today reads `23:00 today`. The resets clause reads `no resets available` at 0, drops `until` with no `limit_reset`, and reads `resets unknown` with no count. Null usage or a `resets` at or before now reads `- <name>: week's usage unknown; refills <refill>; <resets>`, with `refill time unknown` when `resets` is null or past.
- Run-out = reading time + (100 − used) / rate, not counting resets. The rate is the rise across the last 24 h of logged readings since the last refill (`resets` − 7 days), when those readings span at least 1 h; otherwise, or with no readable log, it is the week's pace. A rate of zero or less, or a run-out at or after the refill, reads `lasts to its <refill> refill`.
- `agent_notes.apply` appends one `{"account", "at", "remaining"}` JSON line to `READINGS_LOG` (`~/.local/state/agent-notes/readings.jsonl`) per active note it writes a non-null value to, keeps 8 days through a temporary file and rename, and turns a write error into one line in its messages. The renderer skips a malformed line.

**Files:**
- `scripts/production/dailies_render.py` — `AGENTS_DIR`, `READINGS_LOG`, private `_AgentReading` and `_AgentWeekUsage`, the `agent_*` helpers; `footer` takes `agent_lines`
- `scripts/whoami/agent_notes.py`, `scripts/whoami/test_agent_notes.py` — `READINGS_LOG` and `append_readings`; the reading-log cases
- `commands/showrunner/dailies.md` — one Agents bullet under "What the renderer writes" (user, 2026-10-05)
- `scripts/production/test_dailies_render_agents.py` — renderer cases, DST rounding, placement; `test_dailies_render.py` and `test_dailies_render_holds.py` carry the section in their exact report tails

**Binds later work:** `footer` in `scripts/production/dailies_render.py` takes `agent_lines` and writes them after the build-hold lines and before `waiting on you:`; a held session's state line belongs with the hold lines, above `### Agents`; every subprocess test in `test_dailies_render.py` and `test_dailies_render_holds.py` sets `HOME` to a temporary directory so `AGENTS_DIR` and `READINGS_LOG` never reach the real notes or log.

**Gotchas:** the run-out time is rounded to the minute in epoch seconds, since rounding in aware local wall time loses the DST fold (01:59:45 PDT became 01:00 PST); `resets` and a naive `limit_reset` are the writing machine's wall time, read in the rendering machine's zone through one conversion function that tests pin with `TZ` and `time.tzset()`; the reading log exists only once the updated `agent_notes.py` runs, and until then the run-out uses the week's pace.

**Ruled out:** the renderer importing from `scripts/whoami`: it parses the flat frontmatter itself; `render` splicing the Agents lines in after `footer` returns: `footer` places them.

### Phase 16 — The phone hears about the disk only when the user has something to do · status: done

#### As-built

- After a sweep that is not a dry run, `hold_floor` in `scripts/lint/sweep.py` sends at most one alert (user, 2026-10-05). Floor out of reach — `shrink` chose every removable build unit and incremental dir and still leaves `left > budget`, and free space measured after the removals is under the floor — goes to the phone (`pushover.py --priority 0 "natedev: disk under its floor"`) and to natedev, whether or not the sweep removed anything. A sweep that ends under the floor with removable output still left sends none; the next sweep takes the rest.
- The floor-out-of-reach alert has its own hour, `FloorRecord.push_history` (`last_push_at` in `floor.json`), apart from `alert_history` (`last_alert_at`). When it is due it is the only alert sent; when its hour holds it back, the growth and large-removal alerts keep their thresholds, text and hour, and go to natedev alone.
- `send_floor_alert(message, channels)` takes `FloorAlertChannels.NATEDEV` or `NATEDEV_AND_PHONE` and returns whether any channel delivered.
- `floor_out_of_reach_text` gives the sweep time and amount removed, free space against the floor, the amount to free outside build caches, a `Not swept:` line (held target dirs, CI's targets), `Could not remove <n> path(s) this sweep; the next sweep tries again.` when removals failed, then buildlog-disk's lines without the coverage line. The coverage line, with the fall starting at the previous sweep's `measured_at`: a measurement at or before that start reads `The <unaccounted> fall beyond cache growth came after buildlog-disk's last measurement at <time>.`; otherwise a remainder reads `<remainder> came after <time> or outside measured folders`.
- `read_floor_record` rejects a JSON boolean or non-finite number in any of its five fields through `_finite_number`; a record without `last_push_at` reads with `NoDeliveredAlert`.
- Every push is logged (user, 2026-10-05): `pushover.py` appends one JSON line per call to `~/.local/state/notify/pushover.jsonl` with `time`, `priority`, `title`, `message` as posted after the cuts, and `outcome` (`sent`, `sent, receipt <r>`, `unreachable: <error>`, `refused: <errors>`, `keys missing`). Keys are never logged, a usage error logs nothing, an unwritable log leaves the exit status unchanged, and a reply that is not a JSON object counts as refused. Every message is logged with its text (user, 2026-10-05): each `log.jsonl` line `send.py` writes carries `text`, for every outcome.

**Files:**
- `scripts/lint/sweep.py` — floor sweep, alert channels, alert texts, floor record
- `scripts/notify/pushover.py` — Pushover sender and its JSON log
- `scripts/message/send.py` — message sender; log lines carry the full text
- `scripts/lint/test_sweep.py`, `scripts/notify/test_pushover.py`, `scripts/message/test_send.py` — tests
- `commands/showrunner/produce.md` — names the push log

**Binds later work:** Only the floor-out-of-reach alert reaches the phone; the growth and large-removal alerts reach natedev alone. A past push reads back from `pushover.jsonl`, a past message from `log.jsonl` `text`.

**Gotchas:**
- A failed removal does not hold back the phone push; the push names the failed count.
- `alert_path` keeps each path's last component whole, so a text passes 1,024 characters only with leaf names over ~100 characters; `pushover.py` cuts at `MESSAGE_MAX`, after the action lines.
- `FloorTests.base()` patches `send_floor_alert` to fail the test; a test that needs the real sender captures it before `base()` and wraps it with `subprocess.run` mocked.
- The old `~/.local/state/notify/pushover.log` stays on disk; nothing writes or reads it.

**Ruled out:** cutting a path's last component to fit 1,024 characters; withholding the push when a removal failed.

### Phase 17 — A renumbered phase starts its ETA history afresh · status: done

#### As-built

- `same_phase` in `scripts/production/dailies_render.py` treats two reports as one phase only when the title after the first `: ` matches (`partition`, so a saved phase with no title matches none); the number no longer counts. A renumbered phase with the same title keeps its change note, first ETA and held-example history.
- A new title gets no `(changed: …)` or `(unchanged)` note and no `why` demand; its first ETA is `eta.first` when given, else this report's ETA; the state saves the new title, ETA and first ETA; a simple report prints the held examples again.

**Files:**
- `scripts/production/dailies_render.py` — `same_phase`, read by `change_minutes`, `first_eta` and the simple report's held-example repeat
- `scripts/production/test_dailies_render.py` — `ChangedPhaseTitleTests`: the reported renumber, an explicit `eta.first`, held examples, a saved phase with no title
- `commands/showrunner/dailies.md` — under the eta note: a new phase has a new title; a renumbered phase with the same title keeps its notes

**Gotchas:** `load_state` accepts any phase string, so `same_phase` must not assume a saved phase carries `: `.

**Ruled out:** hiding the first-ETA line when a new phase reports fix rounds: `drift_text` shows `(now +0:00, N fix rounds added)` by design, the same as on a unit's first report.

### Phase 18 — A measured working day · status: done

#### As-built

- One 24-hour working day on natedev, 2026-10-04 15:26:32 PDT to 2026-10-05 15:26:32 PDT, is measured against the target of a normal working day with no earlyoom kill. The window holds zero earlyoom kills and one kernel kill, a CI rustc at 2026-10-04 18:18:47 PDT, before CI's own slot pool went live.
- Sources: `buildlog memory` at the exact window edges and at each CI attempt's GitHub start and end; `samples` and `memory_snapshots` through `buildlog query`; `gh run view` and the GitHub attempts endpoint; the `disk-floor` user journal; stored `brp-launch` steps checked against session transcripts; and the `/build_hold release` output in transcripts.
- The phase ships no code. The Result below is the record: every CI attempt's figures, the CI raise threshold, the newer controls marked provisional with their own day ends, the cold-cache sweeps, launch coverage and the staggering verdict.

**Files:** `docs/plans/build-followups-memory.md` holds the result; no code and no `/etc/nixos` diff.

**Binds later work:**
- The memory gate does not stagger held sessions: after the 2026-10-04 22:42:17 PDT release, four held sessions started builds between 22:42:52 and 22:44:05 PDT with zero memory waits. The one-session-at-a-time release therefore runs, and covers a session whose first build is a BRP launch.
- That release did no harm: 10.1 seconds of builds-slice stall over 22:42–22:50 PDT, no MemoryMax event and no kill.
- Limits stay as they are pending each control's own full day: CI's 18G MemoryMax and the 14-slot `/dev/steve-ci` pool through 2026-10-05 18:43:35 PDT; builds' 44G MemoryMax with no MemoryHigh through 2026-10-05 19:36 PDT; the 12 GiB MemAvailable memory gate through 2026-10-05 20:42:01 PDT. A changed limit needs another measured day after it.
- CI MemoryMax raise threshold: an isolated post-pool run with a CI `oom_kill`, or repeated isolated post-pool runs with sustained ceiling hits and at least 60 seconds of slice stall each, after checking their minute coverage and build mix. A shared-slice run is not evidence for a per-run raise.

**Gotchas:**
- Released holder files keep no end instant, so a day's total hold time cannot be rebuilt.
- The launch collector (`launches.py`, `glob("*/*.jsonl")`) reads one transcript level and missed 11 of 37 launches, the ones in subagent transcripts; those have no `steps` rows and no measured memory.
- `buildlog report` covers one calendar day, so an exact window needs `buildlog memory` at its edges.
- A minute sample can miss a brief peak, and both CI runners share `hana-ci.slice`, so a figure from overlapping runs cannot be assigned to one run.

**Ruled out:**
- A CI MemoryMax raise: no isolated post-pool run met the threshold.
- A per-crate gate: no isolated post-pool run met the threshold, and the missing control is the one-session-at-a-time release.
- A nextest thread cap: no isolated post-pool run met the threshold.
- Any `/etc/nixos` limit change from this day alone: each control first needs its own measured day.

#### Result

**Original day, 2026-10-04 15:26:32 PDT–2026-10-05 15:26:32 PDT.** `buildlog memory` found **zero earlyoom kills** and one kernel kill, the CI rustc at 2026-10-04 18:18:47 PDT before CI's own slot pool went live. `builds.slice` had 18,030,189 MemoryHigh events, 435 MemoryMax events, zero `oom_kill`, and 9,637.9 seconds of `memory.pressure` some stall. `hana-ci.slice` had 2,520,164 MemoryMax events, one `oom_kill`, and 851.8 seconds of stall. The 44 GiB builds peak was a new lifetime high inside this window; the CI lifetime peak of 40 GiB predates it and is not a run peak. The report counted 16 memory-waiting steps, 1.7 step-hours in all, with three reaching 15 minutes; all sampled minutes had sccache in its service. The 1,380 natedev minute samples show peak machine used memory 55.9 GiB, swap used 31.8 GiB, builds process memory 31.2 GiB and CI process memory 17.0 GiB; machine some/full stall grew 9,209.6/7,569.6 seconds between the first and last samples. Those samples span the 1,440-minute window with one boot ID, no gap above 90 seconds, and no null builds or CI process-memory fields. All 1,382 slice snapshots in the window have readable stall fields. The edge snapshots are within two minutes and the journal returned kill counts. Source: `buildlog memory` at the exact window edges, `samples` and `memory_snapshots` in `buildlog query`; the 2026-10-04 and 2026-10-05 `buildlog report` calendar-day reports provide workload context, not exact-window totals.

The exact window contains 2,815 natedev steps whose intervals intersect it, across 60 sessions and 11 worktrees, including 26 stored BRP launches. The independent check's 2,330 steps counts only verify steps other than sweeps, so the two workload figures have different filters. Nine holds relayed during the window took roughly 1.5 hours by relay times, well under the day; their exact total is inconclusive because released holder files no longer retain their `since` instants and several relays give arrival rather than release time. This was a working day with substantial session and CI activity, so it does not need repeating for lack of workload. The CI attempt list below has 26 attempts across 22 run IDs, including reruns, from `gh run view`/the GitHub attempts GET endpoint and the buildlog index; the index had not yet collected 12 later attempts. The two daily reports include activity outside this exact window and cannot be summed for this count.

**CI attempt figures.** Each interval is in PDT in 2026. `max` and stall are the `hana-ci.slice` deltas from `buildlog memory` at that attempt's GitHub start and end. `kill` is the slice's `oom_kill` delta. Rerun comparisons use jobs actually run in that attempt; GitHub jobs carried from an earlier attempt have no new runtime. For a run that ran alone, the last column is its **highest observed minute sample** of natedev `ci_anon_bytes`, followed by observed sample count / run length in minutes. A minute sample can miss a brief peak. “Shared” means another CI attempt overlapped; both runners use the same slice, so those deltas cannot be assigned to one run. `memory.peak` was excluded because it is a slice lifetime high.

| Run / attempt | Window, PDT | Outcome | `max` hits | Stall, s | Kill | Highest observed minute sample, GiB (samples / min) |
|---|---|---|---:|---:|---:|---:|
| 37247551616 / 1 | Oct 4 17:26–17:36 | success | 38,310 | 37.1 | 0 | 8.4 (9 / 9.7) |
| 37250536724 / 1 | Oct 4 18:12–18:31 | failure | 2,118,718 | 157.1 | 1 | 17.0 (19 / 19.2) |
| 37250536724 / 2 | Oct 4 18:33–18:41 | success | 0 | 30.1 | 0 | 10.3 (7 / 7.6) |
| 37257631646 / 1 | Oct 4 20:00–20:15 | failure | 87,223 | 38.1 | 0 | 15.0 (14 / 14.6) |
| 37262324936 / 1 | Oct 4 21:10–21:13 | cancelled, shared | 0 | 0.9 | 0 | shared slice |
| 37262542187 / 1 | Oct 4 21:13–21:27 | cancelled, shared | 41,570 | 35.8 | 0 | shared slice |
| 37263588689 / 1 | Oct 4 21:27–21:44 | success, shared | 5,320 | 34.8 | 0 | shared slice |
| 37265854016 / 1 | Oct 4 21:59–22:10 | cancelled, shared | 6,546 | 27.0 | 0 | shared slice |
| 37266588685 / 1 | Oct 4 22:10–22:27 | success, shared | 22,267 | 33.5 | 0 | shared slice |
| 37279337015 / 1 | Oct 5 00:43–00:56 | success | 45,131 | 12.0 | 0 | 9.3 (12 / 13.2) |
| 37281146666 / 1 | Oct 5 01:02–01:16 | success | 25,540 | 24.1 | 0 | 15.8 (14 / 14.6) |
| 37305088675 / 1 | Oct 5 04:46–04:57 | success | 194 | 6.1 | 0 | 7.0 (11 / 10.7) |
| 37315850645 / 1 | Oct 5 06:19–06:31 | success | 35,807 | 22.8 | 0 | 8.7 (12 / 12.2) |
| 37325554900 / 1 | Oct 5 07:32–07:43 | success | 11,649 | 26.9 | 0 | 9.8 (10 / 11.1) |
| 37346001450 / 1 | Oct 5 10:06–10:13 | cancelled | 3,061 | 22.6 | 0 | 7.2 (7 / 6.9) |
| 37346001450 / 2 | Oct 5 10:26–10:41 | failure | 11,755 | 12.1 | 0 | 10.4 (14 / 15.0) |
| 37346001450 / 3 | Oct 5 11:02–11:10 | failure | 397 | 26.5 | 0 | 6.4 (7 / 8.0) |
| 37355861916 / 1 | Oct 5 11:25–11:43 | failure | 4,785 | 36.3 | 0 | 9.3 (17 / 17.4) |
| 37355861916 / 2 | Oct 5 11:43–11:51 | failure | 1,228 | 25.2 | 0 | 7.7 (8 / 7.8) |
| 37362644607 / 1 | Oct 5 12:20–12:40 | cancelled, shared | 6,243 | 35.4 | 0 | shared slice |
| 37364837852 / 1 | Oct 5 12:39–12:46 | cancelled, shared | 6,908 | 27.9 | 0 | shared slice |
| 37365450427 / 1 | Oct 5 12:45–13:13 | failure, shared | 31,704 | 41.2 | 0 | shared slice |
| 37369777474 / 1 | Oct 5 13:26–13:39 | cancelled, shared | 436 | 31.4 | 0 | shared slice |
| 37371002445 / 1 | Oct 5 13:38–13:49 | cancelled, shared | 754 | 12.9 | 0 | shared slice |
| 37372002638 / 1 | Oct 5 13:48–14:15 | failure, shared | 3,333 | 54.5 | 0 | shared slice |
| 37375671935 / 1 | Oct 5 14:24–14:38 | failure | 11,456 | 38.0 | 0 | 10.5 (13 / 13.6) |

The successful pre-Phase-2 reference run 37149132991 / 1 on 2026-10-03 12:46–13:02 PDT took 15.4 minutes with 13 jobs (`gh run view`, `ci_runs`). The later isolated green attempts in this window took 9.7, 7.6, 13.2, 14.6, 10.7, 12.2 and 11.1 minutes; changed commits and cache state limit a speed inference. The 2026-10-04 killed attempts are excluded from the baseline. The showrunner's 37247551616 reading of 8.4 GiB CI process memory agrees with its 9-minute maximum; its 10.7 GiB builds, 33.7 GiB machine used, 21.7 GiB swap, and 1.2/0.9-minute machine some/full readings refer to that run's observation, not the CI slice stall alone. The 37250536724 / 1 kernel kill at 18:18:47 PDT matches its 2,118,718 ceiling hits, 157.1 seconds of slice stall, and one `oom_kill`; the machine had available RAM, so the 18 GiB CI ceiling was decisive. The kernel logged the OOM invocation at 18:18:46 PDT and the actual rustc kill at 18:18:47 PDT; the earlier relay used the invocation second. Its successful rerun preceded the new pool.

**Changes inside the day.** The `/dev/steve-ci` 14-slot pool went live 2026-10-04 18:43:35 PDT. Its observed 20 hours 43 minutes through the day end contains 23 CI attempts and 1,192 minute samples; `buildlog memory` gives zero earlyoom or kernel kills, CI `max` +363,136 and stall +623.7 seconds. The 1,192 samples include the first one at 2026-10-04 18:43:35.152 PDT (2026-10-05T01:43:35.152Z). Isolated runs after it reached observed minute maxima of 15.0 and 15.8 GiB, above the 10.7 GiB ordinary-mix projection and the 14.6 GiB two-large-compiler projection, but had no kill. These are observations of different mixes, not a failure of the slot count. The control still needs its own full 24 hours, through 2026-10-05 18:43:35 PDT.

The builds limits changed at 2026-10-04 19:21 PDT to a temporary 38G MemoryHigh/44G MemoryMax setting, then at 19:36 PDT to the live 44G MemoryMax with no MemoryHigh. The original-limit interval to 18:43:35 PDT had `high` +17,348,053, `max` +0, stall +5,265.3 seconds and no builds kill. The 18:43–19:21 PDT segment had `high` +682,136 and stall +1,616.1 seconds, but its end snapshot is at 19:20:32 PDT with the temporary values already visible; the precise limit at that boundary is inconclusive. The 19:21–19:36 PDT segment had no new `high`/`max` events, 4.6 seconds of stall and no kill. From 19:36 PDT through 2026-10-05 15:26:32 PDT, the live setting had `high` +0, `max` +435, stall +2,751.9 seconds and no builds kill across 2,439 steps and 1,142 samples. It needs a complete day through 2026-10-05 19:36 PDT. The MemAvailable admission reached main at 20:42:01 PDT; over its 18 hours 44 minutes, 2,356 steps across 60 sessions and 9 worktrees yielded two memory waits totaling 30 seconds, builds `max` +136, builds stall +2,624.2 seconds and no kill. Its own full day ends 2026-10-05 20:42:01 PDT. All these figures use `buildlog memory` at their stated edges and `buildlog query` for workload. The sampled slice fields and stall counters are populated, so there is no deployment fault of that kind.

**Cold caches, launches, and attribution.** The `disk-floor` user journal records 35.2, 89.0 and 110.5 GiB removed at 2026-10-04 19:37, 19:43 and 19:46 PDT, 234.7 GiB in all. Builds spanning those sweeps and their subsequent rebuilds are cold-cache work, excluded from a limit decision based on duration. The same journal records further 20.0 GiB at 2026-10-05 10:35 PDT, 14.2 GiB at 12:27 PDT, 27.7 GiB at 12:40 PDT, 10.8 GiB at 12:54 PDT, 13.2 GiB at 13:39 PDT, 20.4 GiB at 14:03 PDT and 11.0 GiB at 14:19 PDT; subsequent build lengths also have cache churn. The 12:40 PDT removal generated a `disk_floor` message in the natedev message log. Background post-step sweeps discard output, and `floor.json` keeps only the latest sweep: stretches without a journal entry cannot be certified free of cache removals.

The stored `caller='brp-launch'` steps whose intervals intersect this window number 26, all `slice='none'` with no admission or memory field (`buildlog query`). The collector's current pass says zero new launches, which is not the stored count; its one-time Phase 12 re-read is marked complete for all 558 currently present top-level transcripts. The independent transcript scan found 29 top-level result lines for 26 unique launches, with three duplicate lines and **zero** top-level results omitted for missing launch facts or location. It also found 11 unique results in nested subagent transcripts: 37 unique launches across all transcript levels versus 26 stored, a gap caused by the collector's one-level `glob("*/*.jsonl")`. The 11 nested launches have no `steps` rows, so their own memory is unmeasured. Thus 26 is final for the collector's top-level scope but not the full transcript count. The longest stored launch, widget-examples at 2026-10-04 16:15:49–16:40:18 PDT (24.5 minutes), overlaps builds-slice stall +1,219.2 seconds and three memory waits totaling 29.9 minutes; the next long launch at 16:42–16:54 PDT overlaps another 530.5 seconds of builds stall (`buildlog memory`). Their own memory is unmeasured, so overlap does not establish that either launch caused the pressure. No launch overlaps the 18:18:47 PDT CI kill. Launches should still enter memory admission, a judgment from design rather than from this day's figures: a launch's build skips `invoke.sh`'s MemAvailable wait and nothing records its memory; a session whose first build is a BRP launch needs its own acknowledgement because `invoke.sh` cannot mark it today. The window's `calls` have zero `mem_kills`, while the journal's only kill is inside the CI runner: there is no observed verify.sh claim of a kill outside its own step. The report's 16 steps and 1.7 hours of memory waits match the 16 positive `steps.mem_wait_s` rows totaling 6,261 seconds. Transcript visibility cannot verify that count: the independent scan of hidden project JSONL files found only one distinct observed line, `waiting for memory since 15:41 PDT: builds use 26.3 of 26.0 GiB`, copied twice in the same session at 2026-10-04 17:07 and 17:27 PDT. Other text matches quote plans or code. The remaining 15 indexed waits have no visible transcript line to compare, so the agent-view check is inconclusive for them.

**Staggering verdict and limits.** The first natural release after the new admission printed `released, builds may resume.` at 2026-10-04 22:42:17.944 PDT in the holder's top-level transcript. The broadcast reached another top-level transcript at 22:42:30 PDT, consistent with the unit's 22:42 PDT relay; the output is the release instant. Build steps from widget-enhancements (22:42:52 PDT), startup-polish (22:42:55 PDT), geometry-material (22:43:03 PDT), and trunk (22:44:05 PDT) all started with `mem_wait_s=0` (`buildlog query`); the first three started within 11 seconds. Over 22:42–22:50 PDT the builds slice had 10.1 seconds of stall, no MemoryMax event and no kill (`buildlog memory`); minute samples show used RAM rising from 17.1 to 25.1 GiB by 22:43:38 PDT. Admission therefore did **not** stagger the held sessions under ample available memory. Phase 19 should implement one-session-at-a-time release, including a BRP launch acknowledgement. The remaining relayed holds are corroborating workload, not needed to decide this verdict.

For a CI MemoryMax increase, require an isolated post-pool run with a CI `oom_kill`, or repeated isolated post-pool runs with sustained ceiling hits and at least 60 seconds of slice stall each, after checking their own minute coverage and build mix. No isolated post-pool run meets that threshold; a shared-slice run is not evidence for a per-run raise. The post-pool ceiling still sees hits, so retain 18G and the 14-slot pool pending its full day. Retain builds MemoryMax 44G and no MemoryHigh pending that control's full day: there was no builds kill, despite 435 ceiling events and 2,751.9 seconds of aggregate stall. Since 6459e48, the builds and CI ceilings intentionally exceed physical RAM together; the decision uses kills and stall, not their sum. The 12 GiB MemAvailable gate prevented no simultaneous starts in the observed release, so the needed next control is Phase 19 rather than a per-crate gate or nextest thread cap. Neither further change is justified by this day alone. No `/etc/nixos` follow-up diff is proposed; any future limit change needs another measured day.

### Phase 19 — One session at a time · status: done

#### As-built

- The last holder's `/build_hold release` releases held sessions one at a time. While another holder file remains it names who still holds and releases none; the last holder's file stays until the final session is released, so `status`, the dailies renderer and `rust_release.py` keep seeing the hold.
- **Hold cycle.** The first `hold` with no holder file opens a cycle under `BUILD_HOLD_RELEASE_DIR` (default `~/.local/state/build-hold-release`, never the holder directory, whose every file reads as a hold): a `current` file names `<id>/cycle.json`, which holds recipients, registrations, marks, delivery records, release states, and each holder's `since` and release instant, so a hold's length survives its holder file. Nothing reads an earlier cycle; every step reads stored state back. `release.lock` (`flock`) serializes cycle creation, `wait`, every `release` step, `release --resume` and the final check.
- **States.** Each recipient entry carries a typed `ReleaseState` with only the instants valid in it: `AwaitingRelease`, `RecipientGone`, `DeliveryFailed`, `DeliveryQueued`, `ReleasedAwaitingAdmission`, `WaitingForMemory`, `MemoryGateReturned(Granted|TimedOut|MeminfoUnavailable)`, `NoAdmissionAck`, `NoRegistration`; `read_release_state` validates every entry at the read boundary. Only `Granted` says memory was there; no outcome proves the compiler started. `status` and the dailies footer show each state through `cycle_status_lines`.
- **Recipients.** The hold text tells each recipient to run `build_hold.py wait`, which registers its `CLAUDE_CODE_SESSION_ID` in arrival order, only while a holder file exists. `/notify_top_level` records every forwarded session id with `record-recipient`. A recorded recipient that never registers becomes `NoRegistration` `NO_REGISTRATION_S` (60 s) after the last holder's release starts; a late `wait` re-queues it at the end.
- **Driver.** `release` resolves each id with `sessions.py socket session:<id>` and sends with `send.py --to uds:<socket>`, by id and never by name, since `top_level.py` dedupes names. Exit 0 is `ReleasedAwaitingAdmission` (clock from delivery), 1 is `DeliveryQueued` (clock from the attempt), any other is `DeliveryFailed`; a failed or gone recipient passes at once. It polls every 15 s and releases the next session `RELEASE_SETTLE_S` (60 s) after the previous `MemoryGateReturned`, whatever its outcome, so the next session's gate sees the previous compiler; during the settle the release text and `status` name the next session and its release instant. A released session with no mark after `NO_ADMISSION_ACK_S` (15 min), or a `WaitingForMemory` older than `BUILDLOG_MEM_WAIT_LIMIT_S` plus one minute, becomes `NoAdmissionAck`. Under the lock, the last holder file goes only after registrations are re-read and none is `AwaitingRelease`. `release --resume` continues the current cycle after a restart.
- **Marks.** `scripts/lint/memory_gate.sh` holds `buildlog_wait_for_memory` (which now reports its outcome) and `build_hold_mark`. A compiling step writes `WaitingForMemory` when the gate begins to wait and `MemoryGateReturned(outcome)` as the last thing before `buildlog_exec`; sweep and fmt steps, example-gate refusals and pass-record hits write none. Marks key on `CLAUDE_CODE_SESSION_ID`, which seats inherit; a step without one ends no turn.
- **BRP launches.** `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, a `PreToolUse` hook on `mcp__brp__brp_launch`, runs the same gate and marks keyed by the hook input's `session_id`; with no session id it still gates but writes no mark. It always exits 0, and a launch that then fails to build keeps its mark.

**Files:**
- `scripts/build_hold/build_hold.py` — hold cycles, release states, driver, CLI (`wait`, `mark`, `record-recipient`, `release --resume`)
- `scripts/lint/memory_gate.sh` — the memory gate and marks, sourced by `scripts/lint/invoke.sh` (marks around each step's gate) and the BRP hook
- `scripts/hooks/pre-tool-use-brp-launch-gate.sh` — the BRP launch gate; `settings.json` registers it with a 960 s timeout, above `BUILDLOG_MEM_WAIT_LIMIT_S`
- `scripts/message/top_level.py` — records each forwarded session as a hold recipient
- `scripts/production/dailies_render.py` — held sessions' states in the footer
- `commands/build_hold.md` — recipients run `wait`; the release runs in the background
- Tests: `scripts/build_hold/test_build_hold.py`, `scripts/delegate/test_verify_release_ack.py`, `scripts/hooks/test_brp_launch_gate.py`, `scripts/message/test_top_level.py`, `scripts/production/test_dailies_render_holds.py`

**Binds later work:** `forwarded_sessions(me, sessions_dir, unit)` in `scripts/message/top_level.py` returns `(name, session_id)` and `main` records each as a hold recipient via `build_hold.py record-recipient`, which the broadcast fix (Every live session hears a broadcast) builds on. `release.lock` serializes cycle changes; `start_hold` opens a new cycle when no holder exists without reading the old one. `read_cycle` raises `ReleaseRecordReadError` (a `ValueError`), which `status` and the dailies `footer` catch and print as one line via `release_record_error_line`. `build_hold_mark` in `scripts/lint/memory_gate.sh` never fails a build step. A hold with no cycle releases through `release_hold`.

**Gotchas:**
- A held session's turn can take ~16 min (memory wait up to 900 s), so `release` must run in the background.
- Marks are skipped without a holder file before any lock or directory is touched, so ordinary builds pay nothing.
- Release outcomes are not shown once the last holder file goes.
- basedpyright exits 3 in every checkout (pyrightconfig names an absent `.venv`); 0 errors and 0 warnings in its output is the gate.

**Ruled out:** staggering held sessions by the memory gate alone (it does not stagger them); a fixed 5-minute admission timeout (a first build that starts 10 minutes after delivery overlaps the next).

### Phase 20 — Every live session hears a broadcast · status: done

#### As-built

- `scripts/message/top_level.py` takes no arguments and prints one line per live, non-unit top-level session, `<name>\tuds:<messagingSocketPath>`, sorted by name then address. Two sessions that share a name print two lines and both are recorded as hold recipients through `build_hold.py record-recipient`; `main` returns 1 when a record fails.
- `forwarded_sessions(sessions_dir, unit)` turns each live record of `~/.claude/sessions/*.json` into `AddressableSession(name, session_id, address)` or `UnaddressableSession(name, session_id)` at the read boundary. Only addressable sessions are printed and recorded, since a session that cannot be messaged cannot hear the hold; each unaddressable one is named on stderr as `not reachable: <name> [<session id>] has no messaging socket`.
- This session is left out by `CLAUDE_CODE_SESSION_ID` alone, never by name, since a name can belong to two sessions. `--self` and the `me` parameter are gone: any argument prints `usage: top_level.py` and exits 2. With the id unset or empty it prints `top_level: CLAUDE_CODE_SESSION_ID is unset, so this session cannot be left out` on stderr, records nothing and exits 2.
- `/notify_top_level` sends `SendMessage` to each `uds:` address and reports each session by name. A held or refused delivery is named by name and address, so two sessions with one name stay distinct; each `not reachable` line is reported as a session that was not told. Peers on other machines keep their ListAgents name.

**Files:**
- `scripts/message/top_level.py` — the per-session list, the two session types, hold-recipient recording
- `commands/notify_top_level.md` — sends to each `uds:` address; the only caller of `top_level.py`
- `scripts/message/test_top_level.py` — duplicate names, ordering, missing or empty sockets, own-id exclusion, unset id, unit directors, ended or reused pids, record failure, `--self` rejected

**Gotchas:** a record whose `messagingSocketPath` is set counts as addressable even when the socket file is gone, because `top_level.py` keys on the path and judges liveness by the pid's `procStart`; `sessions.py` treats the same record as not live.

### Phase 21 — The build report says how current CI is, and its p95 · status: done

#### As-built

- `ci.ci()` atomically writes `<root>/ci/polled.json` `{repo: {polled_at, complete}}` after each poll that reached GitHub; a cold-credential skip or `GhError` writes nothing. `ci.read_poll_state(repo)` returns `CompletedPoll(polled_at) | CappedPoll(polled_at) | NeverPolled()`; a malformed or zone-less stamp is `NeverPolled`.
- `report.ci_freshness(day)` appends to both CI summary paths `; CI never polled`, or `; CI recorded through <sync_time>` plus `, no poll since` when older than `min(now, day end) - CI_STALE_AFTER_S` (2 h) and `, the last poll was capped` for any capped poll. The CI table head is `Workflow, Runs, Failed, Cancelled, Avg, p95, Range`; the summary names `<n> cancelled` only when nonzero.
- A `p95` follows every average: `COMMON_HEAD` (`COMMON_SQL` fetches `group_concat(duration_s)`, as SQLite has no percentile), each `EXTRA` `AverageColumn` (`<title> p95` from `values_sql`), the Summary tables, edits-since-green (`p95 to next green`) and the CI queue clause (`jobs queued <avg> on average, p95 <p95>`). `nearest_rank(values, percent)`, sorted index `(percent*n+99)//100-1`, serves p95 and the Test builds p75.
- `buildlog sync pause <why>` writes `<root>/sync_paused.json` `{since, why}`; `buildlog sync resume` removes it; other arguments print `usage: buildlog sync [pause <why> | resume]` and exit 2. `sync()` holds an flock on `<root>/sync.lock` for its whole run; paused, it contacts nothing, prints `buildlog sync: paused since <time> (<why>); buildlog sync resume ends it`, returns 0 and leaves `sync.json` alone. `pause` takes the same lock (printing `buildlog sync: waiting for the running sync to finish`), so once it returns nothing contacts the Mac. `hourly` still polls CI while paused.
- `read_pause` returns `SyncPaused(since, why) | SyncRunning()`; `read_status` returns `SyncStatus(at, peer, ok, last_good: LastSynced | NeverSynced) | NeverSynced` (on disk, `SyncRecord`). `mac_note` prefixes `Mac: sync paused since <time> (<why>).`; `sync_time` lives in `sync.py`.
- The mkdir reach check runs `mkdir -p <REMOTE_ROOT>; echo "rc=$?"`; a nonzero or missing last `rc=<n>` line fails it (`reported rc=<n>` / `no rc= line in command output`), writes `sync.json` `ok: false` and exits 1. ssh exit 255 is still a peer that did not answer (one line, exit 0); `rsync` keeps its exit-status checks.

**Files:**
- `scripts/buildlog/ci.py` — poll stamp and its states
- `scripts/buildlog/report.py` — freshness clause, `Cancelled`, p95 columns, `nearest_rank`, pause line
- `scripts/buildlog/sync.py` — pause, resume, `sync.lock`, printed-status reach check, sync states, `sync_time`
- `scripts/buildlog/cli.py` — `sync pause <why>`, `sync resume`, usage
- `scripts/buildlog/store.py` — `CI_POLLED_NAME`, `SYNC_PAUSED_NAME`
- `scripts/buildlog/test_ci.py`, `test_sync.py`, `test_report.py` — their cases

**Binds later work:** a Mac hold is `buildlog sync pause <why>` / `buildlog sync resume`; stopping `buildlog.timer` also stops the CI poll.

**Gotchas:**
- Tailscale SSH runs Mac commands under `/usr/bin/login`, which exits 0 whatever the command did, so a Mac command is judged by the status it prints and ssh's own exit means something only at 255. `rsync` is exempt: its protocol stream carries remote failure.
- A pause never expires; the report's Mac line shows a forgotten one. An unreadable pause file reads as paused (`since` "unknown").
- `pause` can wait up to a sync's timeouts (30 s reach + 2 × 600 s rsync).
- `polled.json` is in `ci/` so sync carries it to the Mac (the index reads only `*/*.jsonl`); `sync_paused.json` is at the root so it stays local.
- Freshness tests use the real clock with relative stamps.
- `basedpyright` exits 3 in every checkout (`pyrightconfig.json` names an absent `.venv`); 0 errors and 0 warnings is the pass.

**Ruled out:** a p75 formula change for small groups — the old formula and `nearest_rank` pick the same index for every n.

### Phase 22 — A damaged release record cannot strand a hold · status: done

#### As-built

- `read_cycle` validates the whole `HoldCycle` shape: top-level object; `id`, `opened_at`, `release_started_at` strings; `holders` maps names to objects with `since` and `released_at` strings; `recipients` maps session ids to name strings; `entries` is a list of objects, each through `read_release_state`. Any failure, unparseable JSON included, raises `ReleaseRecordReadError` naming what is wrong.
- `read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside` (frozen dataclasses; `DamagedRecordSetAside.path` is the renamed file) runs under `release.lock` for `hold`, `release`, `release --resume`, `wait`, `mark` and `record-recipient`. On damage it renames `cycle.json` (when it exists or is a dangling symlink) or else `current` to `<name>.damaged-<UTC YYYYMMDDTHHMMSSZ>` (`-2`, `-3`… when taken), removes `current`, and prints `release record could not be read (<error>); set aside as <path>; this hold now releases every session at once`.
- `release_record_error_line` reads `release record could not be read: <error>; /build_hold release sets it aside and ends the hold`. `status` (`main`) and `footer` in `scripts/production/dailies_render.py` print it and change nothing.
- With no cycle: `hold` beside another active holder writes its holder file and opens no cycle, since a new cycle would strand the sessions registered before it; `wait` prints `no hold cycle: wait for the release broadcast`; `mark` prints `no hold cycle`; `release` always goes through `release_cycle`, whose no-cycle branch calls `release_hold` under the lock and ends with `released, builds may resume. No hold cycle: broadcast this release to every session.` A partial release still names the remaining holders; a cycle release's final line never carries the broadcast clause.
- `release --resume` without `--holder` picks the holder whose release began when a cycle exists, the sole holder when its own call set damage aside, and otherwise exits 1 with `release needs --holder, or --resume for an active release`, keeping the holder file.
- `commands/build_hold.md` step 1 says the no-cycle `wait` line means the release comes as a broadcast; step 4 broadcasts with `/notify_top_level --here` on the no-cycle final line, each session's next build still passing its own memory gate.

**Files:**
- `scripts/build_hold/build_hold.py` — full-shape `read_cycle`, `NoCycle`, `DamagedRecordSetAside`, `read_cycle_for_change`, the no-cycle paths, the error line's recovery
- `commands/build_hold.md` — the no-cycle `wait` line and the broadcast on the no-cycle final release
- `scripts/build_hold/test_build_hold.py` — damaged `cycle.json`, `current`, dangling symlink and ten malformed shapes across every changing command; read-only `status`; no-cycle hold, release, resume and broadcast line (`test_intact_cycle_final_line_does_not_request_broadcast`)
- `scripts/production/test_dailies_render_holds.py` — every release state (`AwaitingRelease`, `RecipientGone`, `DeliveryFailed`, `DeliveryQueued`, `ReleasedAwaitingAdmission`, `WaitingForMemory`, `MemoryGateReturned` with `Granted` / `TimedOut` / `MeminfoUnavailable`, `NoAdmissionAck`, `NoRegistration`) and the damaged `cycle.json` and `current` lines with their recovery, the record left byte-identical

**Gotchas:**
- A holder joining a no-cycle hold sees nothing in its `hold` output; only the final release line tells it to broadcast.
- `read_release_state` assumes a mapping (`"entries": [null]` raises `AttributeError`, outside every `except` in the read path); `read_cycle` checks each entry is an object before calling it.
- Set-aside files are never cleaned up; they stay beside the cycle for inspection.
- Tests set `BUILD_HOLD_DIR` and `BUILD_HOLD_RELEASE_DIR` to temporary directories, so none writes `~/.local/state/build-hold` or `~/.local/state/build-hold-release`.

**Ruled out:** catching `AttributeError` in the read path instead of validating the shape — it would also hide defects in the code; `release --resume` releasing the sole holder of any no-cycle hold — it could end a hold nobody had begun releasing.

## Parked

Work taken out of the phase sequence. Each entry keeps its Work Order and returns as a new phase when its condition holds.

### Parked — verify.sh builds in a named target folder

**Parked 2026-10-04 19:12 PDT by the showrunner:** the user stopped the widget-examples lane after its P7, and that lane was the one consumer of per-helper build folders; building this now adds disk use and parallel-build memory with no user for it. It comes back when the build report's Waiting section shows real time spent queueing for a worktree's build slot.

#### When it returns

On return: re-check every `file:line` reference against the code; they were current on 2026-10-05 after Phase 13.

**Goal:** `verify.sh … --target-dir <name>` runs every cargo step of that call in `<worktree>/target/<name>`, under a cargo token of that folder's own, so a helper with its own folder never waits on the shared folder's lock; its pass records count the same as the shared folder's, and the build log records which folder each call used.

**Source:** natedev (showrunner), 2026-10-04 evening PDT. widget-examples gives up to 2 helpers their own build folders so they stop queueing on one folder's lock; the user wants the waits gone. The settings allow rule (`settings.json:55`, `Bash(bash ~/.claude/scripts/delegate/verify.sh *)`) runs verify.sh as one plain command, so a `CARGO_TARGET_DIR=` prefix would prompt the user: the folder has to be a flag. This goes to `~/.claude` main as soon as it merges. Today verify.sh neither sets nor reads `CARGO_TARGET_DIR`, and every seat under `implement.sh` takes the one `cargo` token (`verify.sh:725-740`).

**Spec:**
- **The flag.** `--target-dir <name>` may sit anywhere after the verb, like `--no-cache`, and is stripped from `ARGS` in the same loop (`verify.sh:492-502`), so nothing below sees it: `FILTER_RUN`, the example guard's argument count (`verify.sh:687-688`), `tree_key`'s `lint` collapse (`verify.sh:532-533`) and the recorded command text (`verify.sh:606`, `:645-648`) are unchanged. The name matches `^[a-z][a-z0-9-]{0,31}$` and is not one of the folders cargo makes directly under `target/` (`debug`, `release`, `doc`, `tmp`, `package`, `nextest`; a constant with that comment). Anything else is refused with exit 2 and one line naming the rule, before any cargo runs: a slash, `..`, an absolute path, an empty or missing value, a reserved name, or the flag given twice. A name in `rustc --print target-list` is refused too, since cargo puts cross-compiled output under `target/<triple>`.
- **Every cargo step uses the folder.** Right after the flag is read, verify.sh exports `CARGO_TARGET_DIR=<toplevel>/target/<name>`, where `<toplevel>` is `git rev-parse --show-toplevel` (the root `TREE_KEY_PY` hashes). Every cargo call of the run inherits it: the `run()` steps, `cargo metadata` (`verify.sh:426-427`, `:433-434`, `:689`, `:923-924`, `:937-938`), mend (`env RUSTC_WRAPPER=`), rustdoc (`env -u CARGO_MAKEFLAGS`) and the background sweep. Without the flag, verify.sh leaves `CARGO_TARGET_DIR` exactly as the caller's environment has it. The flag wins over a caller's `CARGO_TARGET_DIR`. The call's build folder is one typed location, read from the canonical directory (`realpath -m` of the flag's folder, else of the caller's `CARGO_TARGET_DIR`, else `<toplevel>/target`), never from how it was selected, so one physical folder always gets one token and one record value: `Shared` (`<toplevel>/target`): token `cargo`, record `shared`; `Named(<name>)` (`<toplevel>/target/<name>` with a name the flag accepts): token `cargo-<name>`, record `<name>`; `External(<path>)` (anywhere else): token `cargo-ext-<first 8 hex of sha256 of the path>`, record `external:<path>`.
- **One token per folder.** The shared folder keeps the `cargo` token; `--target-dir <name>` takes `cargo-<name>` (board.sh's resource pattern, `board.sh:100-102`, accepts it). Acquire, `release_token` and the second cache lookup use that one name. `implement.sh`, when a seat exits on either path (`implement.sh:348`, `:370`), releases every `cargo` and `cargo-*` token that seat holds, found under `<session_dir>/locks/`; `cmd_release` already refuses a token another holder owns.
- **Pass records count across folders.** `TREE_KEY_PY` leaves `CARGO_TARGET_DIR` out of the environment it hashes, next to `CARGO_MAKEFLAGS` (`verify.sh:402-404`), so a `test` or `lint` pass in `seat-1` is found by the same call in the shared folder, and the reverse. The cache directory stays `<board or session dir>/verify_cache`. The helper folder sits under `target/`, which the repos ignore, so it never enters `git status` or the key.
- **The sweep follows the folder.** `sweep_after_step` (`scripts/lint/invoke.sh:262-275`) keys its once-per-300-s stamp by `$PWD` and `CARGO_TARGET_DIR`, so a helper folder's sweep never postpones the shared folder's.
- **The sweep finds named folders.** `scripts/lint/sweep.py` stops discovering target directories at the outer `target/` (`sweep.py:752-768`) and counts nested files against the outer budget without their build units. It treats each `target/<name>` that holds a cargo layout as its own root with its own budget and eviction, and the outer folder's sweep leaves them out.
- **The build log records the folder.** verify.sh exports `BUILDLOG_TARGET_DIR` as the record value of the call's location; `record.py`'s `write_call` (`scripts/buildlog/record.py:293-317`) writes it as the call record's `target_dir` field. The index's `calls` table gains `target_dir` TEXT, described as the build folder: `shared` for the worktree's `target/`, the name of a folder under `target/`, `external:<path>` for any other folder, or `unknown` for a record written without the field. A call record with no field reads `unknown`: verify.sh has always kept a caller's `CARGO_TARGET_DIR`, and port-lint records set none. The call record's `token_wait_s` (Phase 9, `record.py:333`, `index.py:135`) is unchanged. `SCHEMA_VERSION` goes from 9 to 10, so `open_for_update` rebuilds the index.
- **Unchanged:** a token not acquired within its 1800 s wait runs anyway with its warning (`verify.sh:725-734`), now for each folder's token; cargo's own build-folder lock still serializes two runs in one folder. Steve's slots (`CARGO_MAKEFLAGS`, `invoke.sh:36-42`), the memory admission (`buildlog_wait_for_memory`, `invoke.sh:107-130`) and the per-step cgroup peak (`buildlog_exec`, `buildlog_end`). A call without the flag behaves exactly as today.
- **Docs.** The usage block in verify.sh's header (`verify.sh:70-114`) gains a `… --target-dir <name>` entry: what it builds where, the token it takes, and that its passes count in the shared folder. `<BuildTokenContract/>` in `docs/delegate/write_prompt_contract.md` (`:252-277`) says the token is per build folder: the shared `target/` has `cargo`, and a named folder its own.
- **Tests.** In a new `scripts/delegate/test_verify_target_dir.py`, through real verify.sh routing with the stubs of Phase 6's routing test (`scripts/delegate/test_verify_untested_examples.py`) (a cargo stub on `PATH` that also logs `CARGO_TARGET_DIR` for each call, a git stub): `test <pkg> --target-dir seat-1` and `lint <pkg> --target-dir seat-1` run every cargo call with `CARGO_TARGET_DIR=<toplevel>/target/seat-1`; each refused form exits 2 with no cargo call; without the flag, each cargo call sees the caller's `CARGO_TARGET_DIR`; with `PLAN_DELEGATE_BOARD_DIR` and `PLAN_DELEGATE_TEAM_ROLE` set, the board log shows `cargo-seat-1` taken and released with the flag and `cargo` without; a `test` pass recorded with `--target-dir seat-1` is reused by the same call without it (no `nextest run` call, `PASS (recorded)`), and the reverse; the flag before `--filter` and after it gives the same run. An inherited `CARGO_TARGET_DIR` outside the worktree's `target/` takes its `cargo-ext-` token and records `external:<path>`; an inherited `CARGO_TARGET_DIR=<toplevel>/target/seat-1` takes `cargo-seat-1` and records `seat-1`, the same as `--target-dir seat-1`; a name in the target list is refused; `test <pkg> --target-dir seat-1` on a package with an example holding a test is refused by the example guard with no cargo call besides `metadata`. In `scripts/lint/test_sweep.py`: a named folder under `target/` is swept as its own root and left out of the outer folder's budget. In `scripts/buildlog/test_record.py` and `test_index.py`: the call record carries `target_dir`, and a record without it indexes as `unknown`.

**Files:**
- `scripts/delegate/verify.sh` — the flag, its refusals, `CARGO_TARGET_DIR`, the per-folder token, the key, `BUILDLOG_TARGET_DIR`, the usage block
- `scripts/lint/invoke.sh` — the sweep stamp keyed by folder
- `scripts/lint/sweep.py` — named folders under `target/` swept as their own roots
- `scripts/delegate/implement.sh` — the seat-exit release of every cargo token the seat holds
- `scripts/buildlog/record.py` — `target_dir` on the call record
- `scripts/buildlog/index.py` — `calls.target_dir`, `SCHEMA_VERSION` 10
- `docs/delegate/write_prompt_contract.md` — `<BuildTokenContract/>`'s per-folder token
- `scripts/delegate/test_verify_target_dir.py` — the routing cases above (new)
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py` — the record and index cases
- `scripts/lint/test_sweep.py` — the nested-folder sweep case

**Seats:** `1 writer + 1 tester` — the scripts and their tests split by file.
- `impl` — `scripts/delegate/verify.sh`, `scripts/lint/invoke.sh`, `scripts/lint/sweep.py`, `scripts/delegate/implement.sh`, `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `docs/delegate/write_prompt_contract.md`; hub: `scripts/delegate/verify.sh`
- `test` — `scripts/delegate/test_verify_target_dir.py`, `scripts/lint/test_sweep.py`, `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, written from the Spec alone

**Constraints from prior phases:**
- Phase 13 restored Phase 6's example-test guard (`GATE_METADATA`; exit 2 on an example holding a test, whatever its `test` setting, because examples carry no tests). Its routing test, `scripts/delegate/test_verify_untested_examples.py`, runs real verify.sh routing with stubs on `PATH`: cargo passes `metadata` to real cargo and logs every other call, git is stubbed so the pass-record key is fixed, and the environment strips `PLAN_DELEGATE_BOARD_DIR`/`PLAN_DELEGATE_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`.
- Phase 7 made the index schema 8 (`ci_jobs.run_state`) and Phase 9 made it 9 (`calls.token_wait_s`, seconds acquiring the cargo token); `open_for_update` rebuilds the index whenever `PRAGMA user_version` differs from `SCHEMA_VERSION`. Tests point the store root at a temporary directory and never write `~/.local/state/buildlog`.
- Every delegate seat of this run runs the live `~/.claude/scripts/delegate/verify.sh` and `implement.sh`: seats edit only the worktree copies.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/lint -p 'test_sweep.py'` green; `bash -n` on `scripts/delegate/verify.sh`, `scripts/lint/invoke.sh` and `scripts/delegate/implement.sh`; `basedpyright` 0 errors and 0 warnings on changed Python.
