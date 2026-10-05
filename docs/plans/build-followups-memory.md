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

**Binds later work:** Phase 12's verify.sh acknowledgement goes after this refusal, at the first build step past the memory wait. A new verify.sh routing test follows `test_verify_untested_examples.py`'s stub pattern.

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

### Phase 10 — A build waits for free memory, not for the slice's soft limit · status: todo

#### Work Order

**Goal:** a build step waits to start while the machine has less than 12 GiB of memory available, the floor below which steve stops handing out build slots, instead of while `builds.slice` sits above its `memory.high`, which the slice no longer has.

**Source:** natedev (showrunner), 2026-10-04 19:29 PDT, accepting the unit director's memory proposal. `/etc/nixos` 6459e48 drops `builds.slice`'s MemoryHigh, raises its MemoryMax from 34G to 44G and keeps MemorySwapMax 4G, and gives MemoryLow 8G to `user.slice`, `user-1000.slice`, `user@1000.service` and `app.slice`; it went live at 19:36 PDT (generation 199), when the user also reverted the runtime override (MemoryHigh 38G, MemoryMax 44G) applied at 19:21 PDT. natedev read it live: `memory.high` max, `memory.max` 44.0G, `memory.low` 8.0G at all four levels, MemAvailable 42 GiB. Measured on 2026-10-04: MemoryHigh throttled every build for 45 minutes over one test run's growth; the slice's process memory reached 31.1 GiB with 18.5 million `high` events, no `max` event and no kill; widget's full test step peaked at 19.2–25.4 GiB; 25 test binaries came to about 12 GiB of page cache, which `memory.high` and `memory.current` both count. Since then `memory.high` reads `max` and today's admission (`buildlog_wait_for_memory`, `scripts/lint/invoke.sh:107-130`) returns at once on every step (`:115`), so builds have no admission at all until this phase lands. steve already stops handing out slots while MemAvailable is under 12 GiB (`--min-memory-avail 12288`, `/etc/nixos/modules/linux/jobserver.nix:32-37`), and earlyoom acts at about 3 GiB.

**Spec:**
- **What it waits on.** `buildlog_wait_for_memory` waits while the machine's `MemAvailable` is under 12 GiB (12288 MiB, steve's floor). It reads `/proc/meminfo`; `BUILDLOG_MEMINFO` names another file, and every test sets it. A file that is missing or unreadable, or has no `MemAvailable` line with a whole number of kB, means no wait. It no longer reads `memory.high` or `memory.current`, and no longer reads `BUILDLOG_BUILDS_CGROUP` (`memory.py` and `sample.py` still read that variable for their own figures).
- **Every step waits.** `run_once` (`scripts/lint/invoke.sh:172-179`) calls the gate only when `BUILDLOG_PEAK` is set, so a step that records nothing (`BUILDLOG_SCOPE=0`) has no admission today. The gate runs for every build step; only a recorded step's `started_at` and `mem_wait_s` take the wait.
- **What it says.** On its first check below the floor it prints once to stderr `waiting for memory since HH:MM PDT: the machine has X.X GiB free; a build starts at 12.0` (the time in America/Los_Angeles with its zone, X.X the MemAvailable it read). It rechecks every `BUILDLOG_MEM_POLL_S` seconds (5 by default) and after `BUILDLOG_MEM_WAIT_LIMIT_S` (900 by default) prints `memory wait limit reached after 15 min; starting anyway` and starts. These two knobs, `BUILDLOG_MEM_WAIT_S`, and the step's start time, which excludes the wait, keep their Phase 2 meaning, so `steps.mem_wait_s` and the Waiting section's Memory admission row read the same as before.
- **Ruled out:** a per-run memory cap in verify.sh (natedev, 2026-10-04: widget's drawn-pixel test group already bounds the largest step); keeping a `memory.high` gate beside the new one (the slice has none since 6459e48, and a page-cache count throttles builds that are not short of memory).
- **Tests,** with a fixture meminfo file under a temporary root and never the real `/proc/meminfo` or cgroup tree: a step waits while the file shows under 12 GiB and starts once it shows 12 GiB or more, with its wait in `mem_wait_s` and its start time after the wait; the message prints once with the free figure and its zone; the limit message prints and the step starts; a missing file, a file with no `MemAvailable` line and a non-numeric value each mean no wait; a step run with `BUILDLOG_SCOPE=0` waits too. The routing test that sets `BUILDLOG_BUILDS_CGROUP` for invoke.sh's sake (`scripts/delegate/test_verify_token_wait.py:91`) points `BUILDLOG_MEMINFO` at a file well above the floor instead.

**Files:**
- `scripts/lint/invoke.sh` — `buildlog_wait_for_memory` waits on MemAvailable
- `scripts/buildlog/test_record.py` — the memory admission cases (`:193-250` today)
- `scripts/delegate/test_verify_token_wait.py` — its fixture points `BUILDLOG_MEMINFO` above the floor

**Seats:** `1 writer + 1 tester` — the gate and its tests split by file.
- `impl` — `scripts/lint/invoke.sh`; hub: `scripts/lint/invoke.sh`
- `test` — `scripts/buildlog/test_record.py`, `scripts/delegate/test_verify_token_wait.py`, written from the Spec alone

**Constraints from prior phases:**
- Phase 2: `invoke.sh` runs each step in a `builds.slice` scope; `run_once` calls `buildlog_wait_for_memory` before the step (`:176`), the step's `started_at` excludes the wait, and the waited seconds reach `steps.mem_wait_s` through `BUILDLOG_MEM_WAIT_S`. A pipe element runs in a subshell, so the wait and its seconds stay before the pipe.
- Phase 9: the Waiting section's Memory admission row reads `steps.mem_wait_s` on the day; the index is schema 9. `test_verify_token_wait.py` runs real verify.sh routing with a cargo stub on `PATH`.
- Tests never read the real `/proc/meminfo`, cgroup tree or journal, never write `~/.local/state/buildlog` or `~/.local/state/build-hold`, and never build.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` and `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` green; `bash -n scripts/lint/invoke.sh`; `basedpyright` 0 errors and 0 warnings on changed Python.

### Phase 11 — A sweep that clears build caches for something else says so · status: todo

#### Work Order

**Goal:** when the disk floor's sweep removes build caches because something other than build output is filling the disk, or removes more than 32 GiB at once, natedev hears it after that first sweep, by message and on the phone, with the directories outside the build caches that grew.

**Source:** natedev (showrunner), 2026-10-04 19:55 PDT. Between 19:37 and 19:46 PDT three floor sweeps removed 234.7 GiB of build caches (35.2, 89.0 and 110.5 GiB; `journalctl --user -u disk-floor.service`), and nothing said so. The cause was frame's timing traces, about 300 GB, not cache growth: buildlog-disk's 19:53 PDT snapshot measured `/tmp` at 405 GB, and `/tmp` held 143 GiB at 19:57 PDT once they were gone. Every build after those sweeps started cold, and a cold build is a wait. Free space fell 32.5, 36.9 and 27.9 GiB in the 5, 6 and 3 minutes before those three sweeps. The same journal holds 103 sweeps on 2026-10-04 that removed output: apart from these three, none removed more than 29.2 GiB (09:33 PDT). The only alert today is disk-floor's own (`/etc/nixos/modules/linux/disk-floor.nix`), a high-priority phone alert sent once free space is under 300 GiB. On 2026-10-04 the sweeps held free space near 500 GiB, so it never fired.

**Spec:**
- **When it alerts.** `hold_floor` (`scripts/lint/sweep.py:704-742`) alerts after a sweep that removed output when either holds:
  - *something else is filling the disk:* free space fell more than 10 GiB beyond what the build caches grew since the previous floor sweep, and that sweep was at most 30 minutes earlier;
  - *one sweep removed a lot:* the sweep removed more than 32 GiB.
  On 2026-10-04 the first test held about 30 GiB of unexplained fall before each of the three trace-driven sweeps. The second sits between the largest ordinary sweep (29.2 GiB) and the smallest trace-driven one (35.2). Both thresholds are constants with this evidence beside them.
- **What counts as build caches.** The previous sweep's figures are its free space and build-cache bytes after it ran, measured once its removals finish, never the remainder `shrink` estimates (`sweep.py:642`), which counts a failed removal as gone. They are kept in `floor.json` in the sweep's state directory: `~/.local/state/lint-sweep/`, which `LINT_SWEEP_STATE_DIR` overrides and every test sets. A floor sweep with no earlier record, or an earlier record more than 30 minutes old, writes the record and sends no growth alert. Build-cache bytes are counted in three parts:
  - the idle target directories the sweep already scans;
  - every target directory it leaves alone because a build holds it, measured with `directory_blocks`;
  - CI's two runner folders, `/var/lib/hana-ci/hana-linux-1` and `-2`, measured the same way and never swept.
  Without the last two, a busy worktree or a CI job growing would read as something else filling the disk. The busy and CI figures are taken once per sweep; the sweep never changes them.
- **What it says.** It sends one message to natedev with `scripts/message/send.py --to natedev --from disk_floor --timeout 30`, the text on stdin. It sends the same text with `scripts/notify/pushover.py --priority 0 "natedev: build caches swept" <text>`.
  - The text names: the sweep's time in PDT; what it removed; how far free space fell since the previous sweep against how much the build caches grew; which threshold fired.
  - It then names the three directories outside the build caches that grew most, with their growth and size, measured by buildlog-disk at the times it names. When such a directory's largest child holds more than half of its growth, it names that child as well.
  - It names the snapshot's two measurement times and how much of the fall the measured folders' growth explains. The rest is named as outside the measured folders: buildlog-disk measures `~/rust`, `/tmp` and the CI folders, while the sweep also covers `~/.local/state`. A snapshot older than 15 minutes is named as such.
  - When buildlog-disk's snapshot has no earlier measurement to compare against, the text says so and names the three largest directories outside the build caches.
  - A send that fails prints to stderr and never fails the sweep.
  - At most one such alert per hour, counted from the last alert at least one channel delivered; send.py queueing it for natedev counts as delivered. Each channel's result is printed. When both fail, the next qualifying sweep tries again.
- **The directories outside the build caches come from buildlog-disk** (`scripts/buildlog/disk.py`, every 10 minutes), never from a fresh `du` of `/`.
  - Its walk of the measured folders (`FOLDERS`, `:34-39`) also records each directory one and two levels under each folder that holds at least 1 GiB outside cargo target directories (a directory holding `.rustc_info.json`, as `sweep.target_dirs` finds them), with those bytes.
  - Before it writes, it reads the snapshot it is about to replace. Each recorded directory then carries its growth since that snapshot's `measured_at`: a directory the earlier snapshot did not list grew by its whole size.
  - The snapshot keeps its existing fields, so the build report's disk table (`scripts/buildlog/report.py:591`) reads it unchanged. The sweep reads `disk.json` from `BUILDLOG_DIR`, or `~/.local/state/buildlog`, by path. A snapshot that is missing, unreadable or of the old shape means the text says the measurement is unavailable; the snapshot read returns a named state for each case, not `DiskSnapshot | None`.
- **Ruled out:** changing disk-floor's own 300 GiB alert or its priority; stopping the sweep when growth comes from elsewhere. The floor still holds; the alert is how the cause gets stopped.
- **Tests,** under a temporary root, with the sends replaced so that no test calls send.py or Pushover, and with fixture target folders and fixture `disk.json` files. Never the real disk, journal or state:
  - a sweep after a fall the build caches explain sends nothing;
  - a fall 10 GiB beyond their growth within 30 minutes sends one alert naming the grown directories and the child that holds most of one's growth;
  - a busy target's growth and a CI folder's growth count as build caches;
  - an earlier record over 30 minutes old sends no growth alert;
  - a removal over 32 GiB alerts on its own;
  - a removal that fails leaves its bytes in the recorded build-cache figure;
  - both sends failing leaves the next qualifying sweep free to alert, and one channel delivering counts as sent;
  - a second qualifying sweep within the hour sends nothing;
  - a failed send leaves the sweep's exit status unchanged;
  - buildlog-disk records directories outside target folders with their growth since the previous snapshot, a new directory growing by its whole size, and nested directories counted at each level without counting a hard-linked file twice;
  - the build report's disk table reads both the new and the old snapshot shape.

**Files:**
- `scripts/lint/sweep.py` — the floor record, the two tests, the alert
- `scripts/lint/test_sweep.py` — the sweep cases above
- `scripts/buildlog/disk.py` — directories outside the build caches and their growth
- `scripts/buildlog/test_disk.py` — the measurement cases above
- `scripts/buildlog/test_report.py` — the disk table reads both snapshot shapes

**Seats:** `2 writers` — the sweep alert and the disk measurement have separate hubs and tests; they meet only at `disk.json`'s shape, which the disk writer posts first.
- `impl` — `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`; hub: `scripts/lint/sweep.py`
- `test` — `scripts/buildlog/disk.py`, `scripts/buildlog/test_disk.py`, `scripts/buildlog/test_report.py`; hub: `scripts/buildlog/disk.py`

**Constraints from prior phases:**
- `disk.py` imports `sweep` at load (`sys.path` insert of `scripts/lint`), so `sweep.py` never imports from `scripts/buildlog` at load. It reads `disk.json` as a file.
- `hold_floor` runs from every build step's background sweep as well as from disk-floor's 2-minute timer (`sweep.py --floor-only`); the floor lock (`FLOOR_LOCK`) already lets one run at a time, and the record and the alert stamp are written under it.
- Tests never read the real journal, `/var/lib/hana-ci` or `/proc`, never write `~/.local/state/lint-sweep` or `~/.local/state/buildlog`, never call `send.py` or `pushover.py`, and never build.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** `python3 -m unittest discover -s scripts/lint -p 'test_sweep.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; `basedpyright` 0 errors and 0 warnings on changed Python. Smoke: `BUILDLOG_DIR=<scratch> buildlog disk` run twice on the real tree; the second `disk.json` lists the directories outside the build caches with their growth.

### Phase 12 — Launches started without a path are recorded · status: todo

#### Work Order

**Goal:** every `brp_launch` in the transcripts becomes a build step, including a launch called without a `path` parameter.

**Source:** the unit director's smoke run of Phase 9 on a copy of the real build log, 2026-10-04 20:10 PDT. The collector recorded 238 of the 360 launches in the transcripts. All 122 it missed carry `metadata.working_directory` but have no `parameters.path`, because the launch used the session's own folder. `launch_record` (`scripts/buildlog/launches.py`) needs `parameters.path` for the worktree fields, so it drops them. The missed launches include 2026-10-01 and 2026-10-03 launches. This was moved out of Phase 9 under the production's hard landing rule: it is no regression, and no test fails.

**Spec:**
- When `parameters.path` is absent, take the repo and worktree fields from `record.git_facts(metadata.working_directory)`, the crate folder inside the worktree. `parameters.path` stays first when present. A launch with neither a usable path nor a working directory is still skipped.
- **A worktree that is gone.** `record.git_facts` returns unknown fields for a removed worktree. Such a launch is still recorded, with unknown worktree fields, and counted as unresolved. A back-filled launch leaves branch and SHA blank, so collection-time facts never pose as launch-time ones.
- **Why a launch is skipped.** `launch_record` (`launches.py:73`) returns a named outcome (recorded, not a launch, no result, no location) instead of `dict | None`, and the collector counts each.
- Collection state written by Phase 9 marks every existing transcript as read to its end. So the next run would never revisit the missed lines. Revisit them once, without writing any launch twice: the record id, session plus `launch_timestamp`, already prevents duplicates.
- **Tests,** with fixture transcripts under a temporary root: a launch with no `parameters.path` becomes one step whose worktree is the git worktree holding its working directory, and a second run adds nothing. A run over a transcript whose state was saved before this phase picks up such a launch once; an interrupted re-read resumes without writing a launch twice.

**Files:**
- `scripts/buildlog/launches.py` — the working-directory fallback and the one-time re-read
- `scripts/buildlog/test_launches.py` — the cases above

**Seats:** `1 writer + 1 tester` — the collector and its tests split by file.
- `impl` — `scripts/buildlog/launches.py`; hub: `scripts/buildlog/launches.py`
- `test` — `scripts/buildlog/test_launches.py`, written from the Spec alone

**Constraints from prior phases:**
- Phase 9: `launches.collect()` keeps per-transcript offset, head and tail marks in `launches_state.json` in the store root. It writes one `launch-<sha256(session:timestamp)>.jsonl` file per launch in the host folder. It reads results from `mcpMeta.structuredContent`, from list `tool_result` items, and from `<task-notification>` strings. Its argv uses `--bin` only when `metadata.launched_as` is `app`. `BUILDLOG_TRANSCRIPTS` overrides the transcript root, and tests always set it.
- Tests never read the real `~/.claude/projects` and never write `~/.local/state/buildlog`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; `basedpyright` 0 errors and 0 warnings on changed Python; a smoke run of `buildlog report` over a copy of the real store collects every launch that has a working directory and prints the eligible, recorded and unresolved counts.

### Phase 13 — A measured working day · status: todo

#### Work Order

**Goal:** a measured 24-hour working day on natedev, judged against the target of no earlyoom kill, with the slice limits tuned from it.

**Started:** 2026-10-04 15:26:32 PDT (start snapshot, natedev; the check over 15:16–15:26 PDT showed no missing snapshot and a readable journal). Closes 2026-10-05 15:26 PDT.

**Spec:**

*The window.* It opened when Phase 3's records were live on `~/.claude` main (the buildlog timers run from there). It closes 24 hours later, a full day and night, because the production's units build overnight: take the end snapshot then and run the day's report over the window. During the day, a figure whose window shows a missing snapshot, an unavailable journal or a reset at an edge is inconclusive: say so, and repeat that window or rebuild the figure from the minute snapshots inside it.

Measure against the target, a normal working day with no earlyoom kill, from the report plus:
- CI's run time against a successful CI run from before the Phase 2 diff (the 2026-10-04 runs were killed and are not a runtime baseline);
- CI at its ceiling. In CI run 37227844227 attempt 4 (green 14:14 PDT 2026-10-04) the two Linux jobs peaked at 8.3 and 16.7 GiB, `hana-ci.slice` hit its 18G MemoryMax about 23,800 times (`max` events 3930 → 27773) and its memory stall grew 44.7 s in about 11 min, with no kill. Run 37236742478 (14:35–14:51 PDT, the first without CARGO_BUILD_JOBS) peaked at 16.5 and 11.1 GiB, with `max` events +33.1K, stall +33.9 s, no kill and no jobserver warning. For each CI run in the window, run `buildlog memory START END` over the run's own start and end (from `gh run view`) and record the slice's MemoryMax hits (the `max` events delta), its stall seconds (the `memory.pressure` some-total delta) and any `oom_kill`. `memory.peak` is the slice's lifetime high, so for a run that ran alone, report "highest observed minute sample": the largest `ci_anon_bytes` among this host's samples (`samples.host`) inside the run, with how many minute samples the run has against its length in minutes. A sample is taken once a minute, so it can miss a shorter peak; judge a limit change on the `max` events, stall seconds and `oom_kill` alongside it, never on the sample alone. Name the threshold that would justify raising CI's MemoryMax, and judge the day against it. Before 6459e48 the ceilings already summed past RAM (builds 34G + CI 18G + `app.slice` about 9G + system, on 60 GiB; `builds.slice` peaked at 32 GiB on 2026-10-04), so any raise to CI comes out of `builds.slice`'s MemoryMax and the sum holds;
- the showrunner's readings inside the window, to check against the figures above: run 37247551616 (c14a984b7, 17:26:47–17:36:29 PDT 2026-10-04) was green with 13 jobs; CI processes peaked at 8.4 GiB, builds at 10.7 GiB, used memory at 33.7 GiB and swap at 21.7 GiB; stall some 1.2 min, full 0.9 min; no earlyoom or kernel kill;
- the CI kill at 18:18:46 PDT 2026-10-04 (hana run 37250536724, red): the kernel killed a CI rustc at `hana-ci.slice`'s 18G MemoryMax (memcg OOM, the slice's 2G swap full) with the machine at 43 of 60 GiB. The kill-time task list held 26 rustc from Test Suite and Rendering Diagnostic, 17 of them linking with mold: 17.5 GiB, about 0.67 GiB a slot, because CI took 26 of the shared pool's slots while the sessions were quiet. The proposed repair is CI's own slot pool, `/dev/steve-ci` at 14 slots with the same 12 GiB floor (`docs/plans/build-followups-memory-nixos-ci-pool.diff`, sent to natedev 18:40 PDT): at most 16 CI units at once, projected 10.7 GiB at that mix and 14.6 GiB with two 3.1 GiB release rustc among them. It went live as `/etc/nixos` 06416b5, rebuilt at 18:43:35 PDT 2026-10-04 (generation 198): split every CI figure there, judge the runs after it against that projection, and count the change's own measured day from that instant;
- the builds slice change: `/etc/nixos` 6459e48 drops `builds.slice`'s MemoryHigh, raises its MemoryMax to 44G and gives MemoryLow 8G from `user.slice` down to `app.slice`, live since 19:36 PDT 2026-10-04 (generation 199); before it, the user's runtime override (MemoryHigh 38G, MemoryMax 44G) held from 19:21 PDT, reverted at the switch. Split every builds figure at both instants, as for CI's pool, and count the change's own measured day from 19:36 PDT. From then until Phase 10 lands, builds have no memory admission at all. From then the ceilings, builds 44G and CI 18G, sum past RAM by design: a slice at its ceiling ends in one compiler kill and never a throttle, so judge the day on kills and stall seconds, not on the sum;
- the cold caches: between 19:37 and 19:46 PDT 2026-10-04 three disk-floor sweeps removed 234.7 GiB of build caches while frame's timing traces filled `/tmp`, so the builds after them rebuilt from cold. Mark build times in that stretch as cold-cache and keep them out of any limit judged on build length. Phase 11's alert names such a stretch when it happens again in the window;
- launch builds: `launches.py` records brp_launch builds with `slice: none` and no memory or admission fields. Correlate their intervals with kills and pressure, mark their memory unmeasured, and say whether launch builds need admission. The launch count is final only once Phase 12 has landed;
- whether verify.sh ever counted a kill from outside the step as the step's own; if it did, tie the kill to the step's own processes;
- whether the report's `memory waits:` line agrees with the `waiting for memory since …` lines agents saw;
- the next `/build_hold` release that happens in the window, if any: when each held session's build started, its memory wait and the pressure. Do not stage a hold to produce one. Phase 4's helper reached the sessions when `~/.claude` main took the Phase 4 checkpoint, 2026-10-04 16:01:44 PDT. A release before that instant ran the old prose command and says nothing about the helper;
- overlapping CI runs: both runners share `hana-ci.slice`, so label a figure from overlapping runs as shared-slice, and base the per-run comparison and the threshold only on runs that ran alone;
- the day's hold time and workload (sessions building, CI runs). If holds kept session builds off for much of the day, repeat the day. If no natural release happens, the one-session-at-a-time step is unmeasured and says so.

Tune the slice numbers from what it shows. A changed limit needs another measured day after it. Say whether per-crate admission or nextest thread limits are needed after all. A per-slice sample field that still reads "unavailable" is a deployment fault.

*The staggering verdict.* From the observed release, say whether the admission already staggers the held sessions. If it does not, Phase 14 builds the one-session-at-a-time release; if it does, or no natural release happened, say so and Phase 14 is dropped or stays unmeasured.

*Who takes the end snapshot.* The unit director takes it at 2026-10-05 15:26 PDT whatever the state of Phase 6's merge; this phase never waits on another phase to keep its window. Sources the writer and the checker both use: hold times from each holder file's `since`; release times from the `/build_hold release` output in the session transcripts and natedev's relay log; each session's build start from build-log `steps.started_at` and `mem_wait_s`.

Baseline before the diff: natedev's stopgap 160efd9 put CI in steve on 2026-10-04. The first CI run with it, 37227844227, still lost both Linux jobs to earlyoom: the hana bin's rustc was killed at 12:29:58 and 12:30:09 PDT, about 3.3 GB RSS each with oom_score_adj 500, at about 2.8 of 56.5 GB available. Sharing steve's slots alone does not stop the kills.

**Files:**
- `docs/plans/build-followups-memory.md` — the day's figures, the CI threshold and the conclusions, in this phase
- `docs/plans/build-followups-memory-nixos-*.diff` — any follow-up `/etc/nixos` diff for natedev; `-ci-pool.diff` is CI's own slot pool
- `scripts/buildlog/{memory,sample,cli,index}.py` — the instruments, read only

**Seats:** `1 writer + 1 tester` — the measurement and the test lane are disjoint.
- `impl` — measures the day and writes the result; owns any `/etc/nixos` diff; hub: `docs/plans/build-followups-memory.md` (the result lands here; the tester sends its checks to the writer)
- `test` — independently checks the journals, the CI figures and the record completeness from the same sources

**Constraints from prior phases:**
- `buildlog memory START END` (`scripts/buildlog/memory.py`) reads the snapshots nearest each edge, within 2 minutes, and counts only this host's records; the minute sample (`buildlog sample`, `scripts/buildlog/sample.py` and `cli.py`) writes one snapshot a minute with each slice's `memory.pressure` `some` total; the index is schema 8 since Phase 7 (`ci_jobs.run_state`: a job GitHub carried into a re-run attempt is `carried_over` and has no times of its own, so a re-run's CI figures use the attempt's own `ran` jobs), 9 after Phase 9 (`scripts/buildlog/index.py`). The window opened before Phase 9 changed how waits and launch builds are recorded and before Phase 10 moved the memory admission to MemAvailable: the day's memory verdict stands on the window as measured, and any reading of those phases' effects is a separate, later figure. `memory.peak` is a slice's lifetime high. rustc runs in `builds.slice/run-*.scope` under the sccache client as well as in `sccache.service`.
- Live since 2026-10-04: sccache in the foreground (nixos e669461), `hana-ci.slice` with no MemoryHigh and its runners at OOMPolicy=continue (087c7c1), no CARGO_BUILD_JOBS (832dad4); the CI jobserver check is closed. This supersedes Phase 2's notes that the machine half is not live and that `CARGO_BUILD_JOBS` stays at 8: plan no deployment or jobserver change from them.
- Phase 4: each holder file in `~/.local/state/build-hold/` (`BUILD_HOLD_DIR` overrides it; tests always set it) is one JSON line `{"holder", "since", "for", "release_eta"}`, where `release_eta` is an ISO instant or `unknown`; the old one-line form still reads. `scripts/build_hold/build_hold.py` has `hold` (`--release-eta HH:MM` needs `--zone`), `quiet`, `release` and `status`. `release` prints `released, builds may resume.` only when no holder file remains, else `released; still held by …`. `quiet` is busy while this user's `cargo`, `rustc` or `cargo-nextest` runs or the 1-minute load is at or above a quarter of the cores, and waits at most 10 minutes; `ps` needs `user:32`, or procps cuts long names. `HoldState = NoHolders | ActiveHolders`, `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`, and `quiet_verdict` takes `Cores = KnownCores | UnknownCores` (Phase 5). `dailies_render.py` reads holds through `read_dailies_hold()` and refuses a unit marker with no holder file, active holders with no marked unit, and plumbing words in a holder's purpose. `scripts/buildlog/rust_release.py` treats any regular file in the hold directory as a hold.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** the original day reported at 2026-10-05 15:26 PDT; each newer control (CI's pool from 18:43 PDT, the builds slice from 19:36 PDT, Phase 10's admission once live) labeled provisional until its own complete day, with its workload and snapshot coverage; the day's report, each CI run's figures, the threshold and the conclusions written in this phase, each figure's window complete or marked inconclusive; the staggering verdict stated; `bash -n` on any changed shell.

### Phase 14 — One session at a time · status: todo

#### Work Order

**Goal:** when Phase 13 finds the admission does not stagger held sessions, the last holder's `/build_hold release` releases them one at a time, each after the previous one's build has started.

**Spec:**
- Runs only if Phase 13's verdict says the admission does not stagger the sessions; otherwise this phase is dropped.
- Only the last holder starts it. While another holder file remains, `release` names who still holds and releases no session (Phase 4's behavior). The holder's file stays until the last session is released, so `status`, the renderer and `rust_release.py` keep seeing the hold.
- Release progress is one typed state per held session, in release order: `AwaitingRelease`, `ReleasedAwaitingAdmission(released_at)`, `PastMemoryWait(admitted_at, outcome)` with outcome `Granted`, `TimedOut` or `MeminfoUnavailable`, `NoAdmissionAck(released_at)`, each carrying only the instants valid in it. It is stored in `~/.local/state/build-hold-release/` (`BUILD_HOLD_RELEASE_DIR` overrides it), never in the holder directory, which every reader treats as holds; read back on each step, never kept in memory.
- `release` messages one session at a time, by the same message path `/build_hold` uses today. A session moves to `PastMemoryWait` when its first build step is past the memory wait, whatever the outcome, since its build has started either way: `run_once` in `scripts/lint/invoke.sh` (`:172-179`) writes the acknowledgement right after `buildlog_wait_for_memory` returns, keyed by session name, into the release directory. A call that builds nothing (an example-gate refusal, a pass-record hit) writes none.
- **Who is released.** The holds know holders, not held sessions (`build_hold.py:220`), and today's release goes to every top-level session (`commands/build_hold.md:10`). A session that finds the hold records itself, in arrival order, in the release directory; `release` messages those sessions one at a time by name, through the relay `/build_hold` uses, and after `NoAdmissionAck` moves to the next. `status` and the dailies renderer show each held session's state. The acknowledgement proves admission, not a finished build.
- A session with no acknowledgement 5 minutes after its release becomes `NoAdmissionAck`; the next session is released and the release text names it.
- **The driver.** `release` stays running in the last holder's session: it messages one session, polls the release directory every 15 s, and moves on at an acknowledgement or after 5 minutes. A held session registers with `build_hold.py wait --session <name>` when it finds the hold. Progress is the stored states, so `release --resume` continues after a restart. A session whose first build is a BRP launch writes no acknowledgement, since launches do not run through `invoke.sh`, and becomes `NoAdmissionAck` after 5 minutes.
- Tests: partial progress (some released, some waiting), a missing acknowledgement, another active holder, and `status`, the dailies renderer and `rust_release.py` showing no phantom holder from the release directory, including after the last release clears it.

**Files:**
- `scripts/build_hold/build_hold.py` — release states, release directory, one-at-a-time release
- `scripts/build_hold/test_build_hold.py` — the cases above
- `commands/build_hold.md` — the one-at-a-time release steps
- `scripts/lint/invoke.sh` — the acknowledgement after the memory wait
- `scripts/production/dailies_render.py` — each held session's release state
- `scripts/delegate/test_verify_release_ack.py` — the acknowledgement routing cases
- `scripts/production/test_dailies_render_holds.py` — each held session's state

**Seats:** `1 writer + 1 tester` — the helper and its tests split by file.
- `impl` — `scripts/build_hold/build_hold.py`, `commands/build_hold.md`, `scripts/lint/invoke.sh`, `scripts/production/dailies_render.py`; hub: `scripts/build_hold/build_hold.py`
- `test` — `scripts/build_hold/test_build_hold.py`, `scripts/delegate/test_verify_release_ack.py` (a delayed memory wait acknowledges after it, an example-gate refusal and a pass-record hit acknowledge nothing) and `scripts/production/test_dailies_render_holds.py`, written from the Spec alone

**Constraints from prior phases:**
- Phase 10: `buildlog_wait_for_memory` waits while the machine's MemAvailable is under 12 GiB (`BUILDLOG_MEMINFO` names the file; tests always set it), so the acknowledgement follows that wait.
- Holder files in `~/.local/state/build-hold/` (`BUILD_HOLD_DIR` overrides it; tests always set it) are one JSON line `{"holder", "since", "for", "release_eta"}`; `read_holders` and `scripts/buildlog/rust_release.py` treat every regular file there as a hold. `release` prints `released, builds may resume.` only when no holder file remains. `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`; `quiet_verdict` takes `Cores = KnownCores | UnknownCores`.
- Tests never write `~/.local/state/build-hold` or the real release directory.
- Phase 6's example-test guard is reverted on build-followups as df7301b until hana's examples fix lands; the showrunner restores it by reverting df7301b. With the guard present, `verify.sh test <pkg>` (gate run) and `verify.sh final` refuse an example holding `#[cfg(test)]` with `test = false` (exit 2) before the cache lookup and before any build. The acknowledgement goes at the first build step past the memory wait, so it follows that refusal either way; while the guard is reverted, the routing test drops its example-gate case. If the Follow-up's "examples carry no tests" rule lands first, the guard refuses any example holding `#[cfg(test)]`. `git show 7692f80:scripts/delegate/test_verify_untested_examples.py` shows the pattern for running real verify.sh routing with stubs on `PATH` (cargo passes `metadata` through; git is stubbed so the pass-record key is fixed).

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'`, `python3 -m unittest discover -s scripts/delegate -p 'test_verify_release_ack.py'` and `python3 -m unittest discover -s scripts/production -p 'test_dailies_render*.py'` green; `bash -n scripts/lint/invoke.sh`; `basedpyright` 0 errors and 0 warnings on changed Python.

## Follow-ups

Work that waits on a trigger outside this plan. None is a numbered phase; each lands as one checkpoint when its trigger holds.

### Follow-up — the example-test guard returns with the rule "examples carry no tests"

**Trigger:** tool-based-ui tells the showrunner that its examples Phase 8, which strips the `#[cfg(test)]` modules from examples, is on init/catalyst and merged into every live unit branch. Nothing is done before then.

**What lands, in one checkpoint:** revert df7301b, which restores Phase 6's guard, and change its rule in the same checkpoint. The rule is now that examples carry no tests. The guard flags any example whose source holds a `#[cfg(test)]` module, whether or not the target sets `test = true`, and the refusal no longer tells the user to set `test = true`. The trigger and the place the check runs are unchanged: `verify.sh test <pkg>` as a gate run and `verify.sh final`, before the cache lookup and before any build, with exit 2.

**Source:** natedev (showrunner), 2026-10-04 evening PDT, relaying tool-based-ui's ruling after the user questioned tests in examples.

## Parked

Work taken out of the phase sequence. Each entry keeps its Work Order and returns as a new phase when its condition holds.

### Parked — verify.sh builds in a named target folder

**Parked 2026-10-04 19:12 PDT by the showrunner:** the user stopped the widget-examples lane after its P7, and that lane was the one consumer of per-helper build folders; building this now adds disk use and parallel-build memory with no user for it. It comes back when the build report's Waiting section shows real time spent queueing for a worktree's build slot.

#### When it returns

On return: the token wait (`calls.token_wait_s`, schema 9) shipped in Phase 9, so this phase is per-folder tokens and `target_dir` only, at the next schema version after 9; the example guard it names is the Follow-up's "examples carry no tests" rule once that lands.

**Goal:** `verify.sh … --target-dir <name>` runs every cargo step of that call in `<worktree>/target/<name>`, under a cargo token of that folder's own, so a helper with its own folder never waits on the shared folder's lock; its pass records count the same as the shared folder's, and the build log records which folder each call used.

**Source:** natedev (showrunner), 2026-10-04 evening PDT. widget-examples gives up to 2 helpers their own build folders so they stop queueing on one folder's lock; the user wants the waits gone. The settings allow rule (`settings.json:55`, `Bash(bash ~/.claude/scripts/delegate/verify.sh *)`) runs verify.sh as one plain command, so a `CARGO_TARGET_DIR=` prefix would prompt the user: the folder has to be a flag. This goes to `~/.claude` main as soon as it merges. Today verify.sh neither sets nor reads `CARGO_TARGET_DIR`, and every seat under `implement.sh` takes the one `cargo` token (`verify.sh:729-740`).

**Spec:**
- **The flag.** `--target-dir <name>` may sit anywhere after the verb, like `--no-cache`, and is stripped from `ARGS` in the same loop (`verify.sh:501-511`), so nothing below sees it: `FILTER_RUN`, the untested-example gate's argument count (`verify.sh:695-696`), `tree_key`'s `lint` collapse (`verify.sh:540`) and the recorded command text (`verify.sh:614`, `:653`) are unchanged. The name matches `^[a-z][a-z0-9-]{0,31}$` and is not one of the folders cargo makes directly under `target/` (`debug`, `release`, `doc`, `tmp`, `package`, `nextest`; a constant with that comment). Anything else is refused with exit 2 and one line naming the rule, before any cargo runs: a slash, `..`, an absolute path, an empty or missing value, a reserved name, or the flag given twice. A name in `rustc --print target-list` is refused too, since cargo puts cross-compiled output under `target/<triple>`.
- **Every cargo step uses the folder.** Right after the flag is read, verify.sh exports `CARGO_TARGET_DIR=<toplevel>/target/<name>`, where `<toplevel>` is `git rev-parse --show-toplevel` (the root `TREE_KEY_PY` hashes). Every cargo call of the run inherits it: the `run()` steps, `cargo metadata` (`verify.sh:429`, `:440`, `:697`, `:929`, `:943`), mend (`env RUSTC_WRAPPER=`), rustdoc (`env -u CARGO_MAKEFLAGS`) and the background sweep. Without the flag, verify.sh leaves `CARGO_TARGET_DIR` exactly as the caller's environment has it. The flag wins over a caller's `CARGO_TARGET_DIR`. The call's build folder is one typed location, and the token and the record follow it: `Shared` (no flag, and no caller `CARGO_TARGET_DIR` or one that resolves to `<toplevel>/target`): token `cargo`, record `shared`; `Named(<name>)`: token `cargo-<name>`, record `<name>`; `External(<path>)` (a caller's `CARGO_TARGET_DIR` anywhere else): token `cargo-ext-<first 8 hex of sha256 of the path>`, record `external:<path>`.
- **One token per folder.** The shared folder keeps the `cargo` token; `--target-dir <name>` takes `cargo-<name>` (board.sh's resource pattern, `board.sh:100-102`, accepts it). Acquire, `release_token` and the second cache lookup use that one name. `implement.sh`, when a seat exits on either path (`implement.sh:348`, `:370`), releases every `cargo` and `cargo-*` token that seat holds, found under `<session_dir>/locks/`; `cmd_release` already refuses a token another holder owns.
- **Pass records count across folders.** `TREE_KEY_PY` leaves `CARGO_TARGET_DIR` out of the environment it hashes, next to `CARGO_MAKEFLAGS` (`verify.sh:416-418`), so a `test` or `lint` pass in `seat-1` is found by the same call in the shared folder, and the reverse. The cache directory stays `<board or session dir>/verify_cache`. The helper folder sits under `target/`, which the repos ignore, so it never enters `git status` or the key.
- **The sweep follows the folder.** `sweep_after_step` (`scripts/lint/invoke.sh:241-254`) keys its once-per-300-s stamp by `$PWD` and `CARGO_TARGET_DIR`, so a helper folder's sweep never postpones the shared folder's.
- **The token wait on its own.** verify.sh measures the seconds it spends acquiring the cargo token (0 when it takes none) and passes them to `record.py call`; the call record gains `token_wait_s`, and the index `calls.token_wait_s` INTEGER, described as seconds waiting for the build folder's cargo token, 0 when none was taken. `wait_s` keeps today's meaning.
- **The sweep finds named folders.** `scripts/lint/sweep.py` stops discovering target directories at the outer `target/` (`sweep.py:671`) and counts nested files against the outer budget without their build units. It treats each `target/<name>` that holds a cargo layout as its own root with its own budget and eviction, and the outer folder's sweep leaves them out.
- **The build log records the folder.** verify.sh exports `BUILDLOG_TARGET_DIR` as the record value of the call's location; `record.py`'s `write_call` (`scripts/buildlog/record.py:289-313`) writes it as the call record's `target_dir` field. The index's `calls` table gains `target_dir` TEXT, described as the build folder: `shared` for the worktree's `target/`, else the `--target-dir` name, a folder under `target/`. A call record with no field reads `shared`: every call before this phase used the shared folder, and port-lint records set none. `SCHEMA_VERSION` goes from 8 to 9, so `open_for_update` rebuilds the index.
- **Unchanged:** steve's slots (`CARGO_MAKEFLAGS`, `invoke.sh:36-42`), the memory admission (`buildlog_wait_for_memory`, `invoke.sh:107-130`) and the per-step cgroup peak (`buildlog_exec`, `buildlog_end`). A call without the flag behaves exactly as today.
- **Docs.** The usage block in verify.sh's header (`verify.sh:70-114`) gains a `… --target-dir <name>` entry: what it builds where, the token it takes, and that its passes count in the shared folder. `<BuildTokenContract/>` in `docs/delegate/write_prompt_contract.md` (`:252-277`) says the token is per build folder: the shared `target/` has `cargo`, and a named folder its own.
- **Tests.** In a new `scripts/delegate/test_verify_target_dir.py`, through real verify.sh routing with the stubs of Phase 6's routing test (`git show 7692f80:scripts/delegate/test_verify_untested_examples.py`; the file is off this branch while the guard is reverted) (a cargo stub on `PATH` that also logs `CARGO_TARGET_DIR` for each call, a git stub): `test <pkg> --target-dir seat-1` and `lint <pkg> --target-dir seat-1` run every cargo call with `CARGO_TARGET_DIR=<toplevel>/target/seat-1`; each refused form exits 2 with no cargo call; without the flag, each cargo call sees the caller's `CARGO_TARGET_DIR`; with `PLAN_DELEGATE_BOARD_DIR` and `PLAN_DELEGATE_TEAM_ROLE` set, the board log shows `cargo-seat-1` taken and released with the flag and `cargo` without; a `test` pass recorded with `--target-dir seat-1` is reused by the same call without it (no `nextest run` call, `PASS (recorded)`), and the reverse; the flag before `--filter` and after it gives the same run. An inherited `CARGO_TARGET_DIR` outside the worktree's `target/` takes its `cargo-ext-` token and records `external:<path>`; a name in the target list is refused; a slow preflight with no token contention records `token_wait_s` 0, and a call that waits on a held token records the wait. In `scripts/lint/test_sweep.py`: a named folder under `target/` is swept as its own root and left out of the outer folder's budget. In `scripts/buildlog/test_record.py` and `test_index.py`: the call record carries `target_dir` and `token_wait_s`, and a record without them indexes as `shared` and 0.

**Files:**
- `scripts/delegate/verify.sh` — the flag, its refusals, `CARGO_TARGET_DIR`, the per-folder token, the key, `BUILDLOG_TARGET_DIR`, the usage block
- `scripts/lint/invoke.sh` — the sweep stamp keyed by folder
- `scripts/lint/sweep.py` — named folders under `target/` swept as their own roots
- `scripts/delegate/implement.sh` — the seat-exit release of every cargo token the seat holds
- `scripts/buildlog/record.py` — `target_dir` on the call record
- `scripts/buildlog/index.py` — `calls.target_dir`, `SCHEMA_VERSION` 9
- `docs/delegate/write_prompt_contract.md` — `<BuildTokenContract/>`'s per-folder token
- `scripts/delegate/test_verify_target_dir.py` — the routing cases above (new)
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py` — the record and index cases
- `scripts/lint/test_sweep.py` — the nested-folder sweep case

**Seats:** `1 writer + 1 tester` — the scripts and their tests split by file.
- `impl` — `scripts/delegate/verify.sh`, `scripts/lint/invoke.sh`, `scripts/lint/sweep.py`, `scripts/delegate/implement.sh`, `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `docs/delegate/write_prompt_contract.md`; hub: `scripts/delegate/verify.sh`
- `test` — `scripts/delegate/test_verify_target_dir.py`, `scripts/lint/test_sweep.py`, `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, written from the Spec alone

**Constraints from prior phases:**
- Phase 6's example-test guard (`GATE_METADATA`; exit 2 on an example holding `#[cfg(test)]` with `test = false`) is reverted on build-followups as df7301b until hana's examples fix lands, and the showrunner restores it by reverting df7301b. Build on the guard-free verify.sh and never restore the guard here. Its routing test, `git show 7692f80:scripts/delegate/test_verify_untested_examples.py`, runs real verify.sh routing with stubs on `PATH`: cargo passes `metadata` to real cargo and logs every other call, git is stubbed so the pass-record key is fixed, and the environment strips `PLAN_DELEGATE_BOARD_DIR`/`PLAN_DELEGATE_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`.
- Phase 7 made the index schema 8 (`ci_jobs.run_state`); `open_for_update` rebuilds the index whenever `PRAGMA user_version` differs from `SCHEMA_VERSION`. Tests point the store root at a temporary directory and never write `~/.local/state/buildlog`.
- Every delegate seat of this run runs the live `~/.claude/scripts/delegate/verify.sh` and `implement.sh`: seats edit only the worktree copies.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_verify_*.py'` and `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/lint -p 'test_sweep.py'` green; `bash -n` on `scripts/delegate/verify.sh`, `scripts/lint/invoke.sh` and `scripts/delegate/implement.sh`; `basedpyright` 0 errors and 0 warnings on changed Python.
