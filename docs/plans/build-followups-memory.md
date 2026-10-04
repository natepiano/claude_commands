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

### Phase 2 — One memory admission for every build · status: todo

#### Work Order

**Goal:** Every build on natedev runs under one memory admission. Session builds and their compiles share one systemd slice that slows them above a soft limit, a build step that must wait says why and since when, and a memory kill reads as a kill, not a broken tree. The machine half is one exact `/etc/nixos` diff for natedev.

**Spec:**
Worktree `/home/natepiano/worktrees/claude-build-followups-stalls`, branch `build-followups-stalls`. Two halves, two writers.

*A. `~/.claude` (this worktree):*
1. **Steps join the build slice.** In `scripts/lint/invoke.sh` `run()`, the `systemd-run --user --scope …` call (line 113) gains `--slice=builds.slice`. systemd creates a slice on first use when no unit defines it, so this works before the `/etc/nixos` half lands; a missing user manager keeps today's fallback path unchanged.
2. **A visible wait.** Before that `systemd-run`, `invoke.sh` reads `memory.high` and `memory.current` of the user `builds.slice` cgroup. The path is `/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/builds.slice/`, overridable by `BUILDLOG_BUILDS_CGROUP` for tests.
   - While `memory.current` > `memory.high` (and `memory.high` is not `max`), print once to stderr `waiting for memory since HH:MM PDT: builds use X.X of Y.Y GiB`. The time comes from `TZ=America/Los_Angeles date '+%H:%M %Z'`.
   - Recheck every 5 s. After 15 minutes, print `memory wait limit reached after 15 min; starting anyway` and start.
   - A missing directory, a missing file or `max` means no wait.
   - The waited seconds go into the step record. `record.py step` takes them; `index.py` adds a `wait_s` column (schema 6, migrated like 4→5).
   - The report's Memory pressure section adds one line: `memory waits: N steps, total T, longest L (caller)`, or `memory waits: none`.
3. **A kill reads as a kill.** In `scripts/delegate/verify.sh`, `lint_failure_is_the_tree` (lines 530–536) counts only `SIGKILL|signal: 9`. A step whose log shows `signal: 15`, `SIGTERM` or `signal: 9`, while `journalctl -u earlyoom` or `journalctl -k` (`Memory cgroup out of memory` / `Out of memory: Killed`) has a kill between the step's start and end, is a memory kill.
   - A memory kill is never the tree's failure.
   - verify.sh re-runs that step once. A second memory kill exits with its own message, `killed for memory twice: <step>`, and status 137.
   - Apply the same rule to `test`, wherever verify.sh classifies a run's failure.
4. **Per-slice memory in the sample.** `buildlog sample` also records `builds_anon_bytes` and `ci_anon_bytes`: the `anon` line of `memory.stat` in the user `builds.slice` and in `/sys/fs/cgroup/hana.slice/hana-ci.slice/`, or null when absent. `index.py` stores them (schema 6). The report's machine line adds `peak builds X GiB, peak CI Y GiB (process memory)`.

*B. `/etc/nixos` (diff only — never edit `/etc/nixos`):* write `docs/plans/build-followups-memory-nixos.diff`, a unified diff against `/etc/nixos` HEAD. Edit copies under `/tmp/claude/memory-nixos/`; check each changed file with `nix-instantiate --parse`, and that the diff applies with `git -C /etc/nixos apply --check`.
1. `modules/linux/memory.nix`:
   - `zramSwap = { enable = true; algorithm = "zstd"; memoryPercent = 50; priority = 100; }`. The swapfile, at priority -2, stays behind it.
   - `boot.kernel.sysctl."vm.swappiness" = 100`.
   - Remove `cargo-nextest` from earlyoom's `--prefer` regex.
   - Correct the comment that credits CI's oom_score_adj 500 to `hana-runners.nix`: the GitHub runner sets it.
2. **A user slice and the sccache service,** in the module that owns sccache (`modules/common/development.nix`, Linux only):
   - `systemd.user.slices.builds` with `MemoryHigh = "26G"; MemoryMax = "34G";`.
   - `systemd.user.services.sccache` with `Slice = "builds.slice"`, run in the foreground (`SCCACHE_NO_DAEMON=1`, `sccache --start-server`), and the same `SCCACHE_*` environment the sessions get plus `SCCACHE_IDLE_TIMEOUT=0`. It has `Restart=on-failure` and is wanted by `default.target`.
   - A comment says why: every compile runs in the server's cgroup.
3. `modules/linux/hana-runners.nix`:
   - hana-ci.slice: `MemoryHigh = "10G"; MemoryMax = "18G";` (was MemoryMax 40G).
   - The runners join steve: a `steve-clients` group holding natepiano, hana-linux-1 and hana-linux-2. The `/dev/steve` udev rule in `jobserver.nix` gets `GROUP="steve-clients", MODE="0660"`. Each runner unit gets `SupplementaryGroups`, `BindPaths=/dev/steve` and `DeviceAllow=/dev/steve rw`.
   - `extraEnvironment`: add `CARGO_MAKEFLAGS = "--jobserver-auth=fifo:/dev/steve"` and `PIPELINE_JOB_OOMSCOREADJ = "100"`, and remove `CARGO_BUILD_JOBS`.
4. **The nightly Rust trial build** (`scripts/buildlog/rust_release.py`, started by the hourly `buildlog` job between 02:00 and 05:00, taking steve slots through `CARGO_MAKEFLAGS`) runs in `builds.slice`: give the `buildlog` user job's unit `Slice=builds.slice` in `modules/linux/buildlog.nix`, through whatever its `nate.jobs` entry allows (read its definition). It sits on the session side of the kill order (oom_score_adj 200, above CI's 100): a killed trial is retried the next night.
5. The diff's header comment lists the activation steps for natedev:
   - The rebuild restarts both runners, so pick a time with no CI run.
   - Stop the running sccache server once (`sccache --stop-server`) so the service can bind its port.
   - Read `zramctl` and `oomctl` afterwards.

**Files:**
- `scripts/lint/invoke.sh` — `--slice=builds.slice`, the memory wait
- `scripts/delegate/verify.sh` — memory kill classification and one re-run
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py`, `scripts/buildlog/sample.py`, `scripts/buildlog/report.py` — wait seconds, per-slice anon, schema 6, the report lines
- `scripts/buildlog/test_record.py`, `scripts/buildlog/test_index.py`, `scripts/buildlog/test_sample.py`, `scripts/buildlog/test_report.py` — fixture tests for each
- `docs/plans/build-followups-memory-nixos.diff` — the `/etc/nixos` change (new)

**Seats:** `2 writers` — the `~/.claude` half and the `/etc/nixos` diff share no file.
- `impl` — `scripts/lint/invoke.sh`, `scripts/delegate/verify.sh`, `scripts/buildlog/` (record, index, sample, report and their tests); hub: `scripts/buildlog/index.py` (schema 6 carries both the wait and the slice columns)
- `nixos` opens as impl — `docs/plans/build-followups-memory-nixos.diff` only; reads `/etc/nixos/modules/linux/{memory,jobserver,hana-runners,buildlog}.nix` and `modules/common/development.nix`

**Constraints from prior phases:**
- Session compiles run as children of the one sccache server, in the cgroup of the session that started it (sccache pid 35931 in a `tmux-spawn` scope, 2026-10-04). A step's scope holds cargo, build scripts, sccache clients and test processes only.
- `user@1000.service` delegates memory, so a user slice can carry MemoryHigh and MemoryMax.
- Today steps land in `app.slice/run-p<pid>-i<n>.scope` with no memory property; `BUILDLOG_SCOPE_SH` (invoke.sh lines 64–66) writes the scope's `memory.peak` and `memory.pressure`.
- Index schema is 5. Every buildlog test module calls `use_test_log()` from `test_index` at import, and `point_root_at(test, root)` per test.
- earlyoom's prefer regex is `^(rustc|rustdoc|clippy-driver|cargo-mend|cargo-nextest|ld[.]mold|cc1|cc1plus)$`.
- CI runner units: `User=hana-linux-N`, `PrivateDevices=yes`, `DevicePolicy=closed`, `CARGO_BUILD_JOBS=8` in `extraEnvironment` (hana-runners.nix lines 518–540). `/dev/steve` is `crw------- natepiano`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` green, with new tests for:
  - the wait line, its limit, and no wait without the slice (a temp cgroup dir through `BUILDLOG_BUILDS_CGROUP`);
  - the schema 6 migration;
  - the per-slice sample fields;
  - both report lines.
- `basedpyright scripts/buildlog`: `0 errors, 0 warnings`.
- `bash -n` on `invoke.sh` and `verify.sh`.
- The diff applies with `git -C /etc/nixos apply --check`.
- No test builds anything, reads the real cgroup tree, or writes `~/.local/state/buildlog`.

### Phase 3 — A measured working day · status: todo

After natedev applies the diff and the user rebuilds, measure one working day (PDT) against the target:
- earlyoom kills;
- kernel OOM kills inside `builds.slice` and hana-ci.slice;
- the memory waits and their longest;
- peak `builds.slice` and CI process memory;
- zram's compression ratio (`zramctl`);
- CI's run time against the 2026-10-04 runs.

Tune the slice numbers from what it shows, and say whether per-crate admission or nextest thread limits are needed after all.

### Phase 4 — A build hold releases one session at a time · status: todo

- `/build_hold release` releases sessions one at a time behind Phase 2's admission. If the admission alone staggers them, measure it, say so, and drop the extra step.
- The quiet test before the held test also checks the load average, not only `pgrep`.
- `/build_hold` already writes one file per holder in `~/.local/state/build-hold/` (ratio-unit, shipped), each holding one free-text line: the holder, an ISO time, the test and the reason. Give it `since`, `for` and `release_eta` rather than adding a second file, and have `dailies_render.py --footer` read it, so a showrunner no longer passes `--build-hold-release` by hand.
- The release step still sends "released, builds may resume" while another holder's file remains. Send it only when the last holder's file is gone; otherwise name who still holds.
