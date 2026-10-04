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

**Binds later work:** Phase 8's verify.sh acknowledgement goes after this refusal, at the first build step past the memory wait. A new verify.sh routing test follows `test_verify_untested_examples.py`'s stub pattern.

**Gotchas:** A failing `cargo metadata` exits with cargo's status under `set -e`, not 2. hana main has 7 offending examples; the ~/.claude main merge is held until tool-based-ui adds `test = true`.

**Ruled out:** Python 3.10 support for the check: the repo already needs 3.11+ (`tomllib`).

### Phase 7 — A measured working day · status: todo

#### Work Order

**Goal:** a measured 24-hour working day on natedev, judged against the target of no earlyoom kill, with the slice limits tuned from it.

**Started:** 2026-10-04 15:26:32 PDT (start snapshot, natedev; the check over 15:16–15:26 PDT showed no missing snapshot and a readable journal). Closes 2026-10-05 15:26 PDT.

**Spec:**

*The window.* It opened when Phase 3's records were live on `~/.claude` main (the buildlog timers run from there). It closes 24 hours later, a full day and night, because the production's units build overnight: take the end snapshot then and run the day's report over the window. During the day, a figure whose window shows a missing snapshot, an unavailable journal or a reset at an edge is inconclusive: say so, and repeat that window or rebuild the figure from the minute snapshots inside it.

Measure against the target, a normal working day with no earlyoom kill, from the report plus:
- CI's run time against a successful CI run from before the Phase 2 diff (the 2026-10-04 runs were killed and are not a runtime baseline);
- CI at its ceiling. In CI run 37227844227 attempt 4 (green 14:14 PDT 2026-10-04) the two Linux jobs peaked at 8.3 and 16.7 GiB, `hana-ci.slice` hit its 18G MemoryMax about 23,800 times (`max` events 3930 → 27773) and its memory stall grew 44.7 s in about 11 min, with no kill. Run 37236742478 (14:35–14:51 PDT, the first without CARGO_BUILD_JOBS) peaked at 16.5 and 11.1 GiB, with `max` events +33.1K, stall +33.9 s, no kill and no jobserver warning. For each CI run in the window, run `buildlog memory START END` over the run's own start and end (from `gh run view`) and record the slice's MemoryMax hits (the `max` events delta), its stall seconds (the `memory.pressure` some-total delta) and any `oom_kill`. `memory.peak` is the slice's lifetime high, so for a run that ran alone, report "highest observed minute sample": the largest `ci_anon_bytes` among this host's samples (`samples.host`) inside the run, with how many minute samples the run has against its length in minutes. A sample is taken once a minute, so it can miss a shorter peak; judge a limit change on the `max` events, stall seconds and `oom_kill` alongside it, never on the sample alone. Name the threshold that would justify raising CI's MemoryMax, and judge the day against it. The ceilings already sum past RAM (builds 34G + CI 18G + `app.slice` about 9G + system, on 60 GiB; `builds.slice` peaked at 32 GiB on 2026-10-04), so any raise to CI comes out of `builds.slice`'s MemoryMax and the sum holds;
- whether verify.sh ever counted a kill from outside the step as the step's own; if it did, tie the kill to the step's own processes;
- whether the report's `memory waits:` line agrees with the `waiting for memory since …` lines agents saw;
- the next `/build_hold` release that happens in the window, if any: when each held session's build started, its memory wait and the pressure. Do not stage a hold to produce one. Phase 4's helper reached the sessions when `~/.claude` main took the Phase 4 checkpoint, 2026-10-04 16:01:44 PDT. A release before that instant ran the old prose command and says nothing about the helper;
- overlapping CI runs: both runners share `hana-ci.slice`, so label a figure from overlapping runs as shared-slice, and base the per-run comparison and the threshold only on runs that ran alone;
- the day's hold time and workload (sessions building, CI runs). If holds kept session builds off for much of the day, repeat the day. If no natural release happens, the one-session-at-a-time step is unmeasured and says so.

Tune the slice numbers from what it shows. A changed limit needs another measured day after it. Say whether per-crate admission or nextest thread limits are needed after all. A per-slice sample field that still reads "unavailable" is a deployment fault.

*The staggering verdict.* From the observed release, say whether the admission already staggers the held sessions. If it does not, Phase 8 builds the one-session-at-a-time release; if it does, or no natural release happened, say so and Phase 8 is dropped or stays unmeasured.

*Who takes the end snapshot.* The unit director takes it at 2026-10-05 15:26 PDT whatever the state of Phase 6's merge; this phase never waits on another phase to keep its window. Sources the writer and the checker both use: hold times from each holder file's `since`; release times from the `/build_hold release` output in the session transcripts and natedev's relay log; each session's build start from build-log `steps.started_at` and `mem_wait_s`.

Baseline before the diff: natedev's stopgap 160efd9 put CI in steve on 2026-10-04. The first CI run with it, 37227844227, still lost both Linux jobs to earlyoom: the hana bin's rustc was killed at 12:29:58 and 12:30:09 PDT, about 3.3 GB RSS each with oom_score_adj 500, at about 2.8 of 56.5 GB available. Sharing steve's slots alone does not stop the kills.

**Files:**
- `docs/plans/build-followups-memory.md` — the day's figures, the CI threshold and the conclusions, in this phase
- `docs/plans/build-followups-memory-nixos-*.diff` — any follow-up `/etc/nixos` diff for natedev
- `scripts/buildlog/{memory,sample,cli,index}.py` — the instruments, read only

**Seats:** `1 writer + 1 tester` — the measurement and the test lane are disjoint.
- `impl` — measures the day and writes the result; owns any `/etc/nixos` diff; hub: `docs/plans/build-followups-memory.md` (the result lands here; the tester sends its checks to the writer)
- `test` — independently checks the journals, the CI figures and the record completeness from the same sources

**Constraints from prior phases:**
- `buildlog memory START END` (`scripts/buildlog/memory.py`) reads the snapshots nearest each edge, within 2 minutes, and counts only this host's records; the minute sample (`buildlog sample`, `scripts/buildlog/sample.py` and `cli.py`) writes one snapshot a minute with each slice's `memory.pressure` `some` total; the index is schema 7 (`scripts/buildlog/index.py`). `memory.peak` is a slice's lifetime high. rustc runs in `builds.slice/run-*.scope` under the sccache client as well as in `sccache.service`.
- Live since 2026-10-04: sccache in the foreground (nixos e669461), `hana-ci.slice` with no MemoryHigh and its runners at OOMPolicy=continue (087c7c1), no CARGO_BUILD_JOBS (832dad4); the CI jobserver check is closed. This supersedes Phase 2's notes that the machine half is not live and that `CARGO_BUILD_JOBS` stays at 8: plan no deployment or jobserver change from them.
- Phase 4: each holder file in `~/.local/state/build-hold/` (`BUILD_HOLD_DIR` overrides it; tests always set it) is one JSON line `{"holder", "since", "for", "release_eta"}`, where `release_eta` is an ISO instant or `unknown`; the old one-line form still reads. `scripts/build_hold/build_hold.py` has `hold` (`--release-eta HH:MM` needs `--zone`), `quiet`, `release` and `status`. `release` prints `released, builds may resume.` only when no holder file remains, else `released; still held by …`. `quiet` is busy while this user's `cargo`, `rustc` or `cargo-nextest` runs or the 1-minute load is at or above a quarter of the cores, and waits at most 10 minutes; `ps` needs `user:32`, or procps cuts long names. `HoldState = NoHolders | ActiveHolders`, `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`, and `quiet_verdict` takes `Cores = KnownCores | UnknownCores` (Phase 5). `dailies_render.py` reads holds through `read_dailies_hold()` and refuses a unit marker with no holder file, active holders with no marked unit, and plumbing words in a holder's purpose. `scripts/buildlog/rust_release.py` treats any regular file in the hold directory as a hold.
- Times carry their zone; natedev's clock and journal are EDT, and this plan states PDT.

**Acceptance gate:** the day's report, each CI run's figures, the threshold and the conclusions written in this phase, each figure's window complete or marked inconclusive; the staggering verdict stated; `bash -n` on any changed shell.

### Phase 8 — One session at a time · status: todo

#### Work Order

**Goal:** when Phase 7 finds the admission does not stagger held sessions, the last holder's `/build_hold release` releases them one at a time, each after the previous one's build has started.

**Spec:**
- Runs only if Phase 7's verdict says the admission does not stagger the sessions; otherwise this phase is dropped.
- Only the last holder starts it. While another holder file remains, `release` names who still holds and releases no session (Phase 4's behavior). The holder's file stays until the last session is released, so `status`, the renderer and `rust_release.py` keep seeing the hold.
- Release progress is one typed state per held session, in release order: `AwaitingRelease`, `ReleasedAwaitingBuildStart(released_at)`, `BuildStarted(started_at)`, `NoReply(released_at)`, each carrying only the instants valid in it. It is stored in `~/.local/state/build-hold-release/` (`BUILD_HOLD_RELEASE_DIR` overrides it), never in the holder directory, which every reader treats as holds; read back on each step, never kept in memory.
- `release` messages one session at a time, by the same message path `/build_hold` uses today. A session moves to `BuildStarted` when `verify.sh` starts its first build step past any memory wait and writes its acknowledgement, keyed by session name, into the release directory. The acknowledgement proves admission, not a finished build.
- A session with no acknowledgement 5 minutes after its release becomes `NoReply`; the next session is released and the release text names it.
- Tests: partial progress (some released, some waiting), a missing acknowledgement, another active holder, and `status`, the dailies renderer and `rust_release.py` showing no phantom holder from the release directory, including after the last release clears it.

**Files:**
- `scripts/build_hold/build_hold.py` — release states, release directory, one-at-a-time release
- `scripts/build_hold/test_build_hold.py` — the cases above
- `commands/build_hold.md` — the one-at-a-time release steps
- `scripts/delegate/verify.sh` — the acknowledgement at the first build step

**Seats:** `1 writer + 1 tester` — the helper and its tests split by file.
- `impl` — `scripts/build_hold/build_hold.py`, `commands/build_hold.md`, `scripts/delegate/verify.sh`; hub: `scripts/build_hold/build_hold.py`
- `test` — `scripts/build_hold/test_build_hold.py`, written from the Spec alone

**Constraints from prior phases:**
- Holder files in `~/.local/state/build-hold/` (`BUILD_HOLD_DIR` overrides it; tests always set it) are one JSON line `{"holder", "since", "for", "release_eta"}`; `read_holders` and `scripts/buildlog/rust_release.py` treat every regular file there as a hold. `release` prints `released, builds may resume.` only when no holder file remains. `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`; `quiet_verdict` takes `Cores = KnownCores | UnknownCores`.
- Tests never write `~/.local/state/build-hold` or the real release directory.
- `verify.sh test <pkg>` (gate run) and `verify.sh final` read `cargo metadata --no-deps` once into `GATE_METADATA` and refuse an example holding `#[cfg(test)]` with `test = false` (exit 2) before the cache lookup and before any build; `read_metadata` reuses that read for `require_member` and `take_test_targets`. The acknowledgement goes after that refusal, at the first build step past the memory wait. `scripts/delegate/test_verify_untested_examples.py` shows the pattern for running real verify.sh routing with stubs on `PATH` (cargo passes `metadata` through; git is stubbed so the pass-record key is fixed).

**Acceptance gate:** `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; `bash -n scripts/delegate/verify.sh`; `basedpyright` 0 errors and 0 warnings on changed Python.
