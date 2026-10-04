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

### Phase 3 — A measured working day · status: todo

**Starts when** natedev has run the diff header's sandbox check (prints `OPEN_OK`), applied `docs/plans/build-followups-memory-nixos.diff`, and the user has rebuilt with no CI job running. First confirm the rebuild took: `zramctl` shows the zram device, the user `builds.slice` and `hana-ci.slice` carry their MemoryHigh, MemoryMax and MemorySwapMax, and `systemctl --user status sccache` is active.

**Two checks on the rebuild itself:**
- The next Linux CI job's log has no `failed to connect to jobserver` warning. When a successful job shows that, remove `CARGO_BUILD_JOBS = "8"` from the runners in a new `/etc/nixos` diff for natedev. Until then it is the fallback that stops cargo taking all 32 cores per runner; the Phase 2 diff keeps it.
- Every kind of build runs where the design puts it: a session step and its compiles (`builds.slice`, the compiles in `builds.slice/sccache.service`), the nightly Rust trial (`builds.slice`, through the `buildlog` unit), and a CI job (`hana-ci.slice`). A session's sccache client starts its own server, outside the slice, whenever none answers, so the minute sample job also records whether `sccache.service`'s cgroup holds a process, and the report names the minutes it did not.

**The measured day** is one working day in PDT with explicit start and end instants. natedev's clock is EDT and `report.py` groups by machine local time, so pass the PDT window rather than reading a report's day. Measure against the target:
- earlyoom kills (`journalctl -u earlyoom`, `sending SIG(TERM|KILL) to process`) and kernel OOM kills inside either slice;
- each slice's `memory.events` (`high`, `max`, `oom_kill`) and `memory.peak` / `memory.swap.peak`, snapshotted at the start and end, since the minute samples of `builds_anon_bytes` / `ci_anon_bytes` miss short peaks; swap peaks against MemorySwapMax (builds 4G, CI 2G);
- memory waits from `steps.mem_wait_s` (not `wait_s`, which is the cargo-token wait): count, total, longest, and how many reached the 15-minute limit; check that the report's `memory waits:` line agrees with the `waiting for memory since …` lines agents saw;
- steps that fell back to an unsliced run because the scope could not be created (`invoke.sh`);
- verify.sh's memory-kill retries and `killed for memory twice` exits. verify.sh pairs a signal in the step's log with any earlyoom or kernel kill in the step's window; check whether an unrelated kill in the same window was ever counted, and if it was, tie the kill to the step's own processes;
- zram's compression ratio (`zramctl`);
- CI's run time against a successful CI run from before the diff (the 2026-10-04 runs were killed and are not a runtime baseline);
- the next `/build_hold` release that happens in the window, if any: when each held session's build started, its memory wait and the pressure. Phase 4 reads this. Do not stage a hold to produce one.

Tune the slice numbers from what it shows. A changed limit needs another measured day after it. Say whether per-crate admission or nextest thread limits are needed after all. The per-slice sample fields read "unavailable" before the rebuild; one still unavailable after it is a deployment fault.

Baseline before the diff: natedev's stopgap 160efd9 put CI in steve on 2026-10-04. The first CI run with it, 37227844227, still lost both Linux jobs to earlyoom: the hana bin's rustc was killed at 12:29:58 and 12:30:09 PDT, about 3.3 GB RSS each with oom_score_adj 500, at about 2.8 of 56.5 GB available. Sharing steve's slots alone does not stop the kills.

**Seats:** `1 writer + 1 tester` — the writer measures and writes the result and any follow-up `/etc/nixos` diff; the tester independently checks cgroup placement, the journals, the CI proof and the day's conclusion.

### Phase 4 — Build holds name their holders and release cleanly · status: todo

Three fixes needed whatever Phase 3 finds, and one step that depends on it:
- **Holder files are the one source.** `/build_hold` already writes one file per holder in `~/.local/state/build-hold/` (ratio-unit, shipped), each one free-text line: the holder, an ISO time, the test and the reason. Give each file `since`, `for` and `release_eta`, where `release_eta` is either a time or an explicit unknown (`hold` takes no ETA today, and its two-hour "ask me" rule is an escalation time, not an ETA). Read the old one-line form too. `dailies_render.py` derives both its report and `--footer` hold lines from these files, for any number of holders, so a showrunner no longer passes `--build-hold-release` by hand; update the showrunner commands that pass it. Model the states as types, not `BuildHold | None` and a string that is either a time or "unknown": no hold or active holders, and a known or unknown release time, at the file and JSON boundaries.
- **The last holder releases.** The release step sends "released, builds may resume" only when the last holder's file is gone; otherwise it names who still holds.
- **The quiet test checks load.** Before the held test, the quiet check also requires the 1-minute load average below a stated bound, waits at most a stated time, and tells the holder what is still running when the load stays high (CI, another session).
- **One session at a time, only if needed.** If Phase 3's observed release shows the admission already staggers the sessions, say so and drop this step. Otherwise `/build_hold release` releases sessions one at a time. The command broadcasts its release today, so this needs a release addressed to one session at a time and an acknowledgement from each.

Tests cover no holder, a legacy file, several holders at once, an unknown ETA, a load that stays high, and a partial release.

**Seats:** `1 writer + 1 tester` — the writer owns the holder-file contract: `commands/build_hold.md`, `scripts/production/dailies_render.py`, the showrunner commands that pass hold flags; the tester writes the cases above against that contract.
