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
- **Files:** `scripts/delegate/verify.sh`, `scripts/validate_and_push/`, `commands/build_hold.md`, `scripts/production/dailies_render.py` (`--footer`), and any new shared script (one place, used by every caller). The showrunner settles any clash with the owning units.
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` plus tests for every new script; `basedpyright` clean on changed Python; `bash -n`/`zsh -n` on changed shell.
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

### Phase 4 — Build holds name their holders and release cleanly · status: todo

Three fixes needed whatever the measured day finds. The step that depends on it, releasing one session at a time, is in Phase 5.
- **Holder files are the one source.** `/build_hold` already writes one file per holder in `~/.local/state/build-hold/` (ratio-unit, shipped), each one free-text line: the holder, an ISO time, the test and the reason. Give each file `since`, `for` and `release_eta`, where `release_eta` is either a time or an explicit unknown (`hold` takes no ETA today, and its two-hour "ask me" rule is an escalation time, not an ETA). Read the old one-line form too. `dailies_render.py` derives both its report and `--footer` hold lines from these files, for any number of holders, so a showrunner no longer passes `--build-hold-release` by hand; update the showrunner commands that pass it. Model the states as types, not `BuildHold | None` and a string that is either a time or "unknown": no hold or active holders, and a known or unknown release time, at the file and JSON boundaries.
- **The last holder releases.** The release step sends "released, builds may resume" only when the last holder's file is gone; otherwise it names who still holds.
- **The quiet test checks load.** Before the held test, the quiet check also requires the 1-minute load average below 8 (a quarter of the 32 cores), waits at most 10 minutes, and tells the holder what is still running when the load stays high (CI, another session).
- **One holder state.** `scripts/production/dailies_render.py` renders from one `BuildHold | None`, a free-form release string and unit hold markers passed in separately, which can disagree with several holder files. Read the holder files into one semantic state, no holders or active holders, each with a release that is a time or unknown; render one entry per holder; the report, the footer and the unit hold markers agree after a partial release.
- **A callable helper.** The hold, quiet check and release live only as prose in `commands/build_hold.md`. Move the quiet check and the release decision into a script that takes the holder directory and the load and process readings as inputs, so tests can drive it, and have the command call it. `scripts/buildlog/rust_release.py` treats any regular file in the hold directory as a hold; keep that through the format change.

Tests cover no holder, a legacy file, several holders at once, an unknown ETA, a load that stays high, a partial release, and a report, footer and unit markers that agree after it.

The measured day (Phase 5) runs during this phase.

**Seats:** `1 writer + 1 tester` — the writer owns the helper script, `commands/build_hold.md`, `scripts/production/dailies_render.py` and the showrunner commands that pass hold flags; the tester writes the helper and renderer cases with fake holder files, load and processes.

### Phase 5 — A measured working day · status: todo

**The window** opens when Phase 3's records are live on `~/.claude` main (the buildlog timers run from there): take the start snapshot then and write its instant here, in PDT. It closes 24 hours later, a full day and night, because the production's units build overnight; take the end snapshot then and run the day's report over the window.

**Constraints from prior phases:** `buildlog memory START END` (`scripts/buildlog/memory.py`) reads the snapshots nearest each edge, within 2 minutes, and counts only this host's records; the minute sample (`buildlog sample`, `scripts/buildlog/sample.py` and `cli.py`) writes one snapshot a minute with each slice's `memory.pressure` `some` total; the index is schema 7 (`scripts/buildlog/index.py`). `memory.peak` is a slice's lifetime high. rustc runs in `builds.slice/run-*.scope` under the sccache client as well as in `sccache.service`. Live since 2026-10-04: sccache in the foreground (nixos e669461), `hana-ci.slice` with no MemoryHigh and its runners at OOMPolicy=continue (087c7c1), no CARGO_BUILD_JOBS (832dad4); the CI jobserver check is closed.

**Before the clock starts:** on the deployed sampler, `buildlog memory` over the last 10 minutes shows no missing snapshot and no unavailable journal. During the day, a figure whose window shows a missing snapshot, an unavailable journal or a reset at an edge is inconclusive: say so, and repeat that window or rebuild the figure from the minute snapshots inside it.

Measure against the target, a normal working day with no earlyoom kill, from the report plus:
- CI's run time against a successful CI run from before the Phase 2 diff (the 2026-10-04 runs were killed and are not a runtime baseline);
- CI at its ceiling. In CI run 37227844227 attempt 4 (green 14:14 PDT 2026-10-04) the two Linux jobs peaked at 8.3 and 16.7 GiB, `hana-ci.slice` hit its 18G MemoryMax about 23,800 times (`max` events 3930 → 27773) and its memory stall grew 44.7 s in about 11 min, with no kill. Run 37236742478 (14:35–14:51 PDT, the first without CARGO_BUILD_JOBS) peaked at 16.5 and 11.1 GiB, with `max` events +33.1K, stall +33.9 s, no kill and no jobserver warning. For each CI run in the window, run `buildlog memory START END` over the run's own start and end (from `gh run view`) and record the slice's MemoryMax hits (the `max` events delta), its stall seconds (the `memory.pressure` some-total delta) and any `oom_kill`; `memory.peak` is the slice's lifetime high, so a run's own peak is the highest minute sample's `ci_anon_bytes` over the run. Name the threshold that would justify raising CI's MemoryMax, and judge the day against it. The ceilings already sum past RAM (builds 34G + CI 18G + `app.slice` about 9G + system, on 60 GiB; `builds.slice` peaked at 32 GiB on 2026-10-04), so any raise to CI comes out of `builds.slice`'s MemoryMax and the sum holds;
- whether verify.sh ever counted a kill from outside the step as the step's own; if it did, tie the kill to the step's own processes;
- whether the report's `memory waits:` line agrees with the `waiting for memory since …` lines agents saw;
- the next `/build_hold` release that happens in the window, if any: when each held session's build started, its memory wait and the pressure. Do not stage a hold to produce one.
- overlapping CI runs: both runners share `hana-ci.slice`, so label a figure from overlapping runs as shared-slice, and base the per-run comparison and the threshold only on runs that ran alone;
- the day's hold time and workload (sessions building, CI runs). If holds kept session builds off for much of the day, repeat the day. If no natural release happens, the one-session-at-a-time step is unmeasured and says so.

Tune the slice numbers from what it shows. A changed limit needs another measured day after it. Say whether per-crate admission or nextest thread limits are needed after all. A per-slice sample field that still reads "unavailable" is a deployment fault.

- **One session at a time, only if needed.** If the observed release shows the admission already staggers the sessions, say so and drop this step. Otherwise `/build_hold release` releases sessions one at a time. The command broadcasts its release today, so this needs a release addressed to one session at a time and an acknowledgement from each.

Baseline before the diff: natedev's stopgap 160efd9 put CI in steve on 2026-10-04. The first CI run with it, 37227844227, still lost both Linux jobs to earlyoom: the hana bin's rustc was killed at 12:29:58 and 12:30:09 PDT, about 3.3 GB RSS each with oom_score_adj 500, at about 2.8 of 56.5 GB available. Sharing steve's slots alone does not stop the kills.

**Seats:** `1 writer + 1 tester` — the writer measures and writes the result, any follow-up `/etc/nixos` diff, and the one-at-a-time release if it is needed; the tester independently checks the journals, the CI proof and the day's conclusion. The tester's slot covers record completeness, the independent journal and CI checks, and the tests for the one-at-a-time release if it is built.
