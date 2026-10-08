# Build memory admission: builds that fit in memory

## What it is

Every build on natedev, CI's and the sessions', is admitted so that it fits in memory, and earlyoom is a last resort rather than a routine event (target: a normal working day with no earlyoom kill). Before this, two admission systems could not see each other: CI ran outside steve in a slice whose limit counted page cache, sessions took steve slots that were admitted before their compilers grew, swap sat full of idle processes so earlyoom's swap condition always held, and a build hold released every session at the same instant. On 2026-10-04, 58 earlyoom SIGTERMs failed whole build steps and CI jobs. Now session builds and their compiles run in one bounded user slice, CI runs in its own smaller slice and slot pool with a kill order below the sessions', build steps sit above both so earlyoom kills the largest build first, every compiling step and every BRP launch waits for free memory before it starts, a build hold releases held sessions one at a time, and the build log measures all of it: per-slice samples and snapshots, a window memory report, the day's waits, memory kills and CI freshness. The same body of work also hardened neighbors that share these files: the example-test guard in `verify.sh`, prompt reclaim of a cargo token whose `verify.sh` was killed, CI queue-time accounting, BRP launch recording, the disk-floor alerts, the Codex launcher's capacity handling, the dailies Agents section, and broadcasts to every live session.

## How it works

### Machine side (`/etc/nixos`, natedev only)

Not in this repo; listed because the code here reads it.

| Piece | Setting |
| --- | --- |
| `builds.slice` (user slice, `modules/linux/development.nix`) | `MemoryMax=44G`, no `MemoryHigh`, `MemorySwapMax=4G`. Holds every recorded step's scope (`run-*.scope`), the `sccache.service` user service, and the `buildlog` unit (nightly Rust trial, `OOMScoreAdjust=200`). |
| `sccache.service` | Runs the server in the foreground with `SCCACHE_NO_DAEMON=1` and `SCCACHE_START_SERVER=1` on `ExecStart` only; takes the port from an already running server on start; `RestartSec=5`. Every compile handed to sccache therefore runs inside `builds.slice`. `OOMScoreAdjust=500`, so its compiles rank with the build steps. |
| `hana-ci.slice` | `MemoryMax=18G` only, `MemorySwapMax=2G`, no `MemoryHigh`. Runners `OOMPolicy=continue`. |
| CI slot pool | `/dev/steve-ci`, 14 slots, separate from the sessions' `/dev/steve` (31 slots). Runners: `CARGO_MAKEFLAGS=--jobserver-auth=fifo:/dev/steve-ci`, `BindPaths=-/dev/steve-ci`, `DeviceAllow` `char-rtc r` and `/dev/steve-ci rw`, ordered after `steve-ci.service`. No `CARGO_BUILD_JOBS`. |
| Build and CI kill order | Build steps and sccache at 500: each step's `builds.slice` scope sets `oom_score_adj=500` before starting the step, and `sccache.service` has `OOMScoreAdjust=500`. Session processes at 200 (rust-analyzer, builds outside `invoke.sh`). CI jobs at 100: `PIPELINE_JOB_OOMSCOREADJ=100` in the runners' `extraEnvironment`. earlyoom kills build steps first, then sessions' processes, then CI, the largest first within each level; the processes `--avoid` names go last. |
| Swap and earlyoom (`modules/linux/memory.nix`) | zram (zstd, 50%, priority 100), `vm.swappiness=100`. earlyoom decides on available RAM alone (SIGTERM at 5%, free-swap thresholds 100). Its score is roughly usage‰ of RAM plus swap, plus oom_score_adj, so the kill order comes from oom_score_adj. No `--prefer`; `--avoid` takes 300 from remote access, the desktop and the sessions themselves (`sshd`, `tailscaled`, kwin, plasmashell, `claude` and the tmux server among them). |
| Timers | `buildlog sample` every 60 s, `buildlog disk` every 600 s, `buildlog hourly` every 3600 s, disk-floor every 2 min (`sweep.py --floor-only`). |

The two ceilings (44G + 18G) deliberately exceed physical RAM together; decisions rest on kills and stall, not their sum.

On 2026-10-06 06:54 PDT, two hung hana tests in one local `verify.sh test` step held 11.4 GB and 15.5 GB; earlyoom first sent SIGTERM to 26 other processes, including six CI compilers (four hana CI jobs failed), and killed the tests last. `builds.slice` peaked at 37.7 GiB, under its 44G, because zram (10 GiB), sessions (8 GiB) and CI (3.4 GiB) had already taken the machine to earlyoom's 5% line.

### The memory gate (`scripts/lint/memory_gate.sh`)

Sourced by `scripts/lint/invoke.sh` and by the BRP launch hook. `buildlog_wait_for_memory` holds a step while `MemAvailable` in `${BUILDLOG_MEMINFO:-/proc/meminfo}` is under 12 GiB (12582912 kB, steve's slot floor), polling every `BUILDLOG_MEM_POLL_S` (5 s) up to `BUILDLOG_MEM_WAIT_LIMIT_S` (900 s). It sets, in the calling shell:

- `BUILDLOG_MEM_OUTCOME`: `Granted` (read at or above the floor), `TimedOut` (limit reached), `MeminfoUnavailable` (file missing or unreadable, or no `MemAvailable: <digits> kB` line; no wait).
- `BUILDLOG_MEM_WAIT_S`: seconds from the first below-floor read; 0 when it never waited.

Printed to stderr, once each:

```
waiting for memory since HH:MM PDT: the machine has X.X GiB free; a build starts at 12.0
memory wait limit reached after 15 min; starting anyway
```

`build_hold_mark <state> [outcome]` calls `build_hold.py mark` only when `CLAUDE_CODE_SESSION_ID` is set and a holder file exists; it checks for the file before touching any lock or directory, discards output and never fails. The gate writes `WaitingForMemory` when it first starts waiting.

In `invoke.sh`, `run_once` runs the gate for every step that compiles, recorded or not (`BUILDLOG_SCOPE=0` too), then marks `MemoryGateReturned <outcome>`, then restarts the step's clock, then calls `buildlog_exec`. `buildlog_step_compiles` exempts `*/sweep.py` and `cargo [+toolchain] fmt`. `buildlog_exec` runs a recorded step with `systemd-run --user --scope --slice=builds.slice`. The scope's shell, `BUILDLOG_SCOPE_SH`, writes its marker, then runs `{ echo 500 > /proc/self/oom_score_adj; } 2>/dev/null || true;` before `"$@"`, so every process the step starts in the scope (cargo, build scripts, test binaries, any rustc or linker the step runs itself) inherits oom_score_adj 500; an unprivileged process may raise its own. Builds outside `invoke.sh` keep 200, and the Mac runs no scope. `scripts/lint/test_invoke_scope.py` runs the extracted string under `/bin/sh -c` with fd 3 set as `buildlog_exec` sets it: step and child read 500, exit 7 passes through, and a failed write stays silent under an exported `SHELLOPTS=errexit`; the 500 checks skip when the runner already runs at 500 or more.

### BRP launches

`scripts/hooks/pre-tool-use-brp-launch-gate.sh` is a `PreToolUse` hook on `mcp__brp__brp_launch` (`settings.json`, timeout 960 s, above the 900 s wait limit). It reads `session_id` from the hook input into `CLAUDE_CODE_SESSION_ID`, runs the same gate, marks `MemoryGateReturned` (no mark without a session id) and always exits 0. A launch's build itself runs in the session's own scope, not `builds.slice`.

### Memory kills in `verify.sh`

`run()` wraps `run_once`. A failed step is a memory kill when its output shows `SIGKILL|SIGTERM|signal: (9|15)` and the journal shows, inside the step's window, earlyoom's `sending SIG(TERM|KILL) to process` (`journalctl -u earlyoom`) or the kernel's `Memory cgroup out of memory` / `Out of memory: Killed` (`journalctl -k`). The query lives in `memory_kill_in_journal` so tests can stub `journalctl`. A memory kill is never the tree's failure: the step re-runs once (`killed for memory: <step>; retrying once`), and a second kill exits 137 with `killed for memory twice: <step>`. Each kill appends a byte to `MEM_KILL_FILE`; the second creates `<file>.stopped`. `note_event` passes `BUILDLOG_MEM_KILLS` and `BUILDLOG_MEM_KILL_STOPPED` to `record.py call`.

`verify.sh` also times the `cargo` board-token acquisition and passes it as `BUILDLOG_TOKEN_WAIT_S`. A wait that times out exits 1 before any call is recorded, so its time is in no row.

### Cargo token reclaim (`scripts/delegate/board.sh`)

`verify.sh` acquires the board's `cargo` token with `--pid $$ --hold 3600 --wait 1800`, and `board.sh acquire --pid N` (a positive integer, else exit 2) records N in `locks/<res>.d/holder_pid`. A waiter treats the token as stale when that process is gone, checked first, or when its hold has expired. Gone means `ps -p N -o stat=` exits 1 with empty output or reports a zombie; anything else, a failure of `ps` included, counts as alive, and a lock with no or a malformed `holder_pid` waits for expiry. So a `verify.sh` killed before its EXIT trap frees the token within one 3 s poll, posting `token cargo reclaimed from <holder>: holder pid <N> is gone`, while a manual holder is never reclaimed early. Reclaiming a dead holder first SIGKILLs the step process group it recorded in `locks/<res>.d/owned` and fails closed (acquire exits 1, token left in place) when that group cannot be inspected or stopped; an expired hold whose holder is alive is reclaimed without touching its build. Reclaim and release run under an exclusive flock on `locks/<res>.guard`, taken by python3 `fcntl` on descriptor 9 (the Mac has no `flock(1)`) and dropped by the kernel when its holder dies: a reclaimer re-reads the lock under it, so two waiters cannot both reclaim and a release cannot remove a lock a reclaimer just made. Reclaim never waits for the guard; release retries for up to 10 s, then exits 1 (`board.sh: cannot guard <res> release`).

### Build log: records and schema

Index `SCHEMA_VERSION = 9` (`scripts/buildlog/index.py`; `open_for_update` rebuilds on any mismatch). Added columns:

| Table | Column | Meaning |
| --- | --- | --- |
| `steps` | `mem_wait_s` | seconds the gate held the step (`BUILDLOG_MEM_WAIT_S`) |
| `steps` | `slice` | `builds` (scope ran), `fallback` (scope failed, plain run), `none` (no scope tried) |
| `calls` | `token_wait_s` | cargo-token acquisition seconds; 0 when absent or not a whole number |
| `calls` | `mem_kills`, `mem_kill_stopped` | memory-killed attempts; 1 when a step was killed twice |
| `samples` | `builds_anon_bytes`, `ci_anon_bytes` | the `anon` line of each slice's `memory.stat` (process memory); NULL when absent |
| `samples` | `sccache_in_service` | 1 when `builds.slice/sccache.service/cgroup.procs` holds a process, 0 when empty, NULL without `builds.slice` |
| `memory_snapshots` | `src, host, at, slices, zram` | JSON slice counters and zram, indexed on `at` |
| `ci_jobs` | `run_state` | `JobRunState.RAN / SKIPPED / CARRIED_OVER` |

`steps.wait_s` keeps its meaning (metadata, pass-record lookups and the token wait together); `mem_wait_s` is the memory wait.

**Slice snapshots** (`scripts/buildlog/memory.py`). `buildlog snapshot`, and every `buildlog sample` (via `SampleTaken(at, host)` and `memory.write_snapshot(at, host)`), append a `memory_snapshot` record to the host's monthly samples file. Per slice (`builds`, `ci`, or `{"state": "absent"}`): `memory.events` `high`/`max`/`oom_kill`, `memory.peak`, `memory.swap.peak`, the three limits, and `memory.pressure` `some` total as `stall_some_us` (or `"unavailable"`); plus zram `mm_stat` data and compressed bytes. A snapshot failure prints `memory snapshot unavailable` and never fails the sample. `BUILDLOG_BUILDS_CGROUP`, `BUILDLOG_CI_CGROUP` and `BUILDLOG_ZRAM` point tests at fixture trees.

**CI jobs.** A job is `carried_over` when its attempt is above 1 and `started_at` precedes `created_at`, else `skipped` when its conclusion is `skipped`, else `ran`. Only `ran` rows store `duration_s` and `queued_s`; `queued_s` is `started_at - created_at` when both parse and the result is zero or more, else NULL. `elapsed_seconds(start, end) -> float | None` is unrounded; `seconds_between` rounds to 3 places. `ci_job_days` excludes `carried_over` and keys days by the run attempt's `started_at`.

**CI poll freshness** (`scripts/buildlog/ci.py`). Each poll that reached GitHub writes `<root>/ci/polled.json` `{repo: {polled_at, complete}}` atomically; a cold-credential skip or `GhError` writes nothing. `read_poll_state(repo) -> CompletedPoll(polled_at) | CappedPoll(polled_at) | NeverPolled()`; a malformed or zone-less stamp is `NeverPolled`.

**BRP launches** (`scripts/buildlog/launches.py`). `collect() -> LaunchCounts` turns each `brp_launch` result in `~/.claude/projects/*/*.jsonl` (`BUILDLOG_TRANSCRIPTS` replaces the root) into a `build` step with caller `brp-launch`: `ended_at` = `metadata.launch_timestamp`, `started_at` = that minus the launch duration, argv `cargo build --workspace --bin|--example <target> [--release]` (`--bin` only when `metadata.launched_as` is `app`), no memory fields. It reads `mcpMeta.structuredContent`, list `tool_result` items and `<task-notification><result>JSON</result>` strings, and skips any line lacking `brp_launch` or one of those markers before parsing. `launch_record(line, transcript, *, backfill=False) -> RecordedLaunch | LaunchOutcome` takes the location from `parameters.path`, else `metadata.working_directory`, else `LaunchOutcome.NO_LOCATION`; repo and worktree come only from `record.git_facts(location)`. Each launch is `launch-<sha256(session:timestamp)>.jsonl`, written only when absent (temporary file, hard link). `launches_state.json` holds each transcript's offset and marks; an entry without `reread_complete` is re-read once from the start, launches before its saved offset recorded with `branch` and `sha` null. `LaunchCounts.summary()` prints on stdout for `buildlog launches` and on stderr for `buildlog report`. `day_report` collects before `index.update()`.

### Build log: commands and report

| Command | Output |
| --- | --- |
| `buildlog snapshot` | writes one snapshot |
| `buildlog memory START END` | window report; ISO instants with offsets, else `usage: buildlog memory START END (ISO 8601 instants with offsets)`, exit 2 |
| `buildlog launches` | collects; prints the counts line |
| `buildlog sync pause <why>` / `resume` | see Mac sync below; other arguments: `usage: buildlog sync [pause <why> \| resume]`, exit 2 |

**Window report** (`memory.window_report`), host-scoped (`store.host_name()`), times in PDT:

```
Memory, <date time PDT> to <date time PDT>
earlyoom kills: N            (then one "<HH:MM PDT> <comm> pid <pid>" line each)
kernel OOM kills: N (builds B, CI C, elsewhere E)
builds: high +N, max +N, oom_kill +N; stall +N.N s; peak X GiB, limit Y GiB; swap peak ...
CI: ...
memory waits: N steps, total T, longest L, K reached the 15-min limit | memory waits: none
sccache in its service: every sampled minute | sccache outside its service: N min — <runs> | sccache service: unavailable
unsliced steps: N fell back to a plain run, M never tried a scope | unsliced steps: none
memory kills: N calls passed after a re-run, M failed later, K stopped after a step was killed twice | memory kills: none
zram: X GiB stored in Y GiB (R:1) | zram: unavailable
```

Slice lines use the snapshots nearest each edge within `SNAPSHOT_DISTANCE_S` (120 s), else `<slice>: no snapshot within 2 min of <time>`. A journal error or nonzero exit prints `journal unavailable`, never zero kills. A peak that did not rise in the window reads `at most X GiB (no new high in the window)`. A counter that falls in the window is a reset: `<slice>: reset in the window; since the reset ...`. Edges round up to the next millisecond (`stored_bound`) to match stored records.

**Daily report** (`buildlog report [day]`, `scripts/buildlog/report.py`) order: `## Builds, <day>`; `### Waiting`; `### Rebuilds` (where compile time goes; `docs/as-built/build-followups.md`, Rebuilds section); one section per kind; `### Memory pressure`; agent calls; test builds; port-lint; `### CI`; `### Tests per edit`; Disk; summaries; one-line totals and notes.

- `### Waiting`: a `Wait | Longest | Over 5 min | Waited | Total | Worst` table with four rows, always present, in this order: `Build-folder turn, behind another seat` and `Build-folder turn, behind its own call` (`calls.token_wait_s` split between them as below, by worktree), `Memory admission` (`steps.mem_wait_s`, by worktree and seat), `CI queue` (`ran` jobs with known `queued_s`, by `<workflow> / <job>`). Every row goes through `wait_row()`. `Longest` carries who and the time with its zone; `Over 5 min` counts waits above 300 s; `Waited` reads `<n> of <m> calls|steps|jobs`, and each build-folder row counts the calls with a positive part in that row out of all the day's verify.sh calls; `Total` is seat-hours (job-hours for CI); `Worst` is the top three by summed wait. An empty kind reads `none`. No averages. The one line under the table: `Source: verify.sh calls, memory-gated steps and CI jobs; a seat's own calls run one at a time, so waiting behind its own call adds no delay, behind another seat does.`
  - **Build-folder split.** `build_folder_waits(connection, day) -> (another_waits, own_waits)` returns two `ReportedWait` lists (`tuple[float, str, str, str]`: duration, owner, name, started_at), one entry per call in each, with the call's part of the wait as the duration and the worktree as owner and name. It reads the day's `tool = 'verify.sh'` calls and the token holders: verify.sh calls with a `delegate_session` and an `ended_at` on the day or later. `token_holders()` turns each into `TokenHolder(call_id, delegate_session, seat, starts_at, ends_at)` over `[started_at + wait_s, ended_at]`, grouped by session so each call scans only its own session's holders.
  - The cargo token is held per delegate session, so only calls of the same `delegate_session` can cause a token wait. A positive `token_wait_s` on a call with a session goes through `attribute_token_wait(call_id, delegate_session, seat, starts_at, wait_s, token_wait_s, holders) -> TokenWaitAttribution(behind_another_seat_s, behind_own_call_s)`; a call with no `delegate_session` puts its whole wait in the another-seat row.
  - `attribute_token_wait` takes the wait interval `[started_at + wait_s - token_wait_s, started_at + wait_s]` and splits it at the boundaries of the overlapping same-session holders other than the call itself. A segment is own only when every holder covering it has the call's seat; every other segment, uncovered included, counts as another seat. Own is capped at `token_wait_s`; another seat is the remainder.
  - A NULL seat reads `(unknown seat)`. Session calls with no seat never wait but can hold the token, and a seat waiting behind one counts as another seat.
- `### Memory pressure` always has its heading. It carries `memory waits: N steps, total T, longest L (<caller>)` or `memory waits: none`, the three instrument lines (sccache, unsliced steps, memory kills; aggregated across hosts), the stall table, and `60 s samples: peak used memory X, peak swap Y; peak builds B, peak CI C (process memory); machine stall: ...` (`unavailable` per slice without samples).
- A `p95` follows every average: the kind tables (`COMMON_HEAD = Runs, Failed, Avg, p95, Range`; `COMMON_SQL` fetches `group_concat(duration_s)`), each `AverageColumn` (`<title> p95`), the summaries, Tests per edit (`p95 to next green`) and the CI queue clause. `nearest_rank(values, percent)` uses sorted index `(percent*n+99)//100-1` and also serves the Test builds p75 and the Rebuilds package p50 and p95.
- `### CI`: head `Workflow, Runs, Failed, Cancelled, Avg, p95, Range`; the summary names `<n> cancelled` only when nonzero. The queue clause is `jobs queued <avg> on average, p95 <p95>`, plus `, <N> without a known queue time left out` when any, or `no job has a known queue time` (`(<N> left out)`). Days key on the run attempt's `started_at`. Both CI summary paths end with `ci_freshness(day)`: `; CI never polled`, or `; CI recorded through <time>`, plus `, no poll since` when older than `min(now, day end) - CI_STALE_AFTER_S` (2 h) and `, the last poll was capped` for a capped poll.
- `brp-launch` steps show as `example launches (brp)`; launches from temporary folders group under their worktree, not as scratch (`GROUP_AS_SCRATCH`).
- The Mac note starts `Mac: sync paused since <time> (<why>).` while paused.

### Mac sync pause (`scripts/buildlog/sync.py`)

`buildlog sync pause <why>` writes `<root>/sync_paused.json` `{since, why}`; `resume` removes it. `sync()` holds an flock on `<root>/sync.lock` for its whole run; paused, it contacts nothing, prints `buildlog sync: paused since <time> (<why>); buildlog sync resume ends it`, returns 0 and leaves `sync.json` alone. `pause` takes the same lock (printing `buildlog sync: waiting for the running sync to finish`), so once it returns nothing contacts the Mac. `hourly` still polls CI while paused. `read_pause -> SyncPaused(since, why) | SyncRunning()`; `read_status -> SyncStatus(at, peer, ok, last_good: LastSynced | NeverSynced) | NeverSynced`. The reach check runs `mkdir -p <REMOTE_ROOT>; echo "rc=$?"` and judges the printed `rc=`; nonzero or missing (`reported rc=<n>` / `no rc= line in command output`) writes `sync.json` `ok: false` and exits 1. ssh exit 255 is a peer that did not answer (one line, exit 0). `rsync` keeps its exit-status checks.

### Build hold (`scripts/build_hold/build_hold.py`, `commands/build_hold.md`)

**Holder files.** One file per holder in `BUILD_HOLD_DIR` (default `~/.local/state/build-hold/`), one JSON line `{holder, since, for, release_eta}`. The legacy free-text form still reads, and an unreadable or malformed regular file stays a hold. Types: `HoldState = NoHolders | ActiveHolders` (non-empty, ordered by `since`), `Holder(name, since, purpose, release)`, `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`, `ReleaseRequest = NoReleaseEta | ClockReleaseEta(clock, zone)`, `Cores = KnownCores(count) | UnknownCores`. Holder files are the one source of hold state for `status`, the dailies renderer and `rust_release.py` (which treats any regular file as a hold).

**CLI.**

| Command | Behavior |
| --- | --- |
| `hold --holder N --for TEXT [--release-eta HH:MM --zone IANA]` | Zone required with an ETA; ETA must be later today in that zone. Prints `/build_hold from N: stop any cargo or verify.sh you are running and start none until I release. No release after 2 h: ask me. For: TEXT Run python3 ~/.claude/scripts/build_hold/build_hold.py wait now in this session.` |
| `quiet [--max-wait 600]` | Busy while this user's `cargo`, `rustc` or `cargo-nextest` run (`your builds still running: 7 cargo, 5 rustc`) or the 1-minute load is at or above cores/4 (names the five heaviest processes); with `UnknownCores`, `core count unavailable, so the load limit cannot be judged`. Exit 1 when busy, else `builds are quiet; load is below the limit`. |
| `wait` | Registers `CLAUDE_CODE_SESSION_ID`: `registered <id>; wait for direct release before building`, `no build hold`, `no hold cycle: wait for the release broadcast`, or `NoSessionId: hold remains active; this session cannot register`. |
| `mark --state WaitingForMemory\|MemoryGateReturned [--outcome] [--session-id]` | Written by the gate. |
| `nothing-to-build` | A released session's answer when it has no build or BRP launch to start. A `ReleasedAwaitingAdmission` or `DeliveryQueued` entry for `CLAUDE_CODE_SESSION_ID` becomes `NothingToBuild` and prints `<name> [<id>]: NothingToBuild; the release moves on to the next session`. Any other state prints `nothing-to-build ignored: <name> [<id>]: <State>`; an unregistered session `nothing-to-build ignored: this session is not registered`; otherwise `NoSessionId`, `no build hold` or `no hold cycle`. |
| `record-recipient --session-id --name` | Written by `top_level.py`. |
| `release --holder N` / `release --resume` | The release driver; run in the background. |
| `status` | One line per holder (`N since HH:MM ZONE, for TEXT - release eta: ...`), then the cycle's state lines, or `no build hold`. |

**Hold cycle.** State lives under `BUILD_HOLD_RELEASE_DIR` (default `~/.local/state/build-hold-release`, never the holder directory, whose every file reads as a hold). `current` names `<id>/cycle.json`, a `HoldCycle` `{id, opened_at, holders: {name: {since, released_at}}, recipients: {session_id: name}, entries: [ReleaseEntry], release_started_at}`, written by temp file and replace. `release.lock` (`flock`) serializes cycle creation, `wait`, `mark`, `nothing-to-build`, `record-recipient`, every `release` step and the final check. The first `hold` with no holder file opens a new cycle without reading the old one. A `hold` beside another holder joins the current cycle; with no readable cycle it writes its holder file and opens none.

**Release states.** Each entry carries one `ReleaseState` with only the instants valid in it: `AwaitingRelease`, `RecipientGone`, `DeliveryFailed(attempted_at)`, `DeliveryQueued(attempted_at)`, `ReleasedAwaitingAdmission(released_at)`, `WaitingForMemory(released_at, wait_started_at)`, `MemoryGateReturned(wait_ended_at, outcome: Granted|TimedOut|MeminfoUnavailable)`, `NothingToBuild(answered_at)`, `NoAdmissionAck(released_at)`, `NoRegistration`. `read_release_state` validates the exact field set per state; `store_release_state` writes it. `cycle_status_lines` renders `<name> [<id>]: <State>[(<outcome>)]`, plus `next <name> [<id>] at HH:MM:SS ZONE` during a settle.

**Release driver** (`release_cycle`, `advance_release`).
- While another holder file remains, `release` records this holder's `released_at`, removes its file and prints `released; still held by <holder> (for ..., release eta ...)`; nobody is released.
- The last holder's release sets `release_started_at` and then, every `RELEASE_POLL_S` (15 s), advances the queue in registration order. It resolves each session with `sessions.py socket session:<id>` and sends `/build_hold: your hold has been released. Start your next build or BRP launch now; its memory gate records admission. With nothing to build, run python3 ~/.claude/scripts/build_hold/build_hold.py nothing-to-build now, so the next session is released. Session <id>.` through `send.py --to uds:<socket>`: exit 0 is `ReleasedAwaitingAdmission`, 1 `DeliveryQueued`, other `DeliveryFailed`; no socket is `RecipientGone`. Failed and gone recipients pass at once.
- The next session is released `RELEASE_SETTLE_S` (60 s) after the previous entry's `MemoryGateReturned`, whatever its outcome, and at the next poll after a `NothingToBuild`, which started nothing to settle. A released or queued entry with no mark after `NO_ADMISSION_ACK_S` (900 s), or a `WaitingForMemory` older than `BUILDLOG_MEM_WAIT_LIMIT_S` + 60 s, becomes `NoAdmissionAck`.
- A recorded recipient that never registered becomes `NoRegistration` `NO_REGISTRATION_S` (60 s) after the release starts; a late `wait` moves it to the end of the queue as `AwaitingRelease`.
- Progress lines print as it goes (an entry's state, `...; next <entry> at <time>`, `waiting for registrations: <names>`, `waiting for release progress`). The last holder file is removed only once no entry is active, and the final line is `released, builds may resume. <every entry's state>` (or `NoRegistration timeout; no recipients registered`).
- `release --resume` without `--holder` picks the holder whose release began; after its own call set damage aside, the sole remaining holder; otherwise exit 1 with `release needs --holder, or --resume for an active release`.

**Damaged record.** `read_cycle` validates the whole `HoldCycle` shape (each entry an object, then `read_release_state`) and raises `ReleaseRecordReadError` (a `ValueError`) naming the fault. Changing commands read through `read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside(path)` under the lock: on damage it renames `cycle.json` (when it exists or is a dangling symlink), else `current`, to `<name>.damaged-<UTC YYYYMMDDTHHMMSSZ>` (`-2`, `-3`... when taken), removes `current`, and prints `release record could not be read (<error>); set aside as <path>; this hold now releases every session at once`. Read-only `status` and the dailies footer print `release record could not be read: <error>; /build_hold release sets it aside and ends the hold` and change nothing.

**No-cycle release.** With no cycle, `release` calls `release_hold` under the lock; the last holder's line becomes `released, builds may resume. No hold cycle: broadcast this release to every session.`, and the command doc has the holder broadcast it with `/notify_top_level --here`. A cycle release's final line never carries that clause.

### Broadcasts to sessions (`scripts/message/top_level.py`, `commands/notify_top_level.md`)

`top_level.py` takes no arguments (any argument: `usage: top_level.py`, exit 2) and prints one line per live, non-unit top-level session, `<name>\tuds:<messagingSocketPath>`, sorted by name then address. `forwarded_sessions(sessions_dir, unit)` reads `~/.claude/sessions/*.json` into `AddressableSession(name, session_id, address) | UnaddressableSession(name, session_id)`. Only addressable sessions print and are recorded as hold recipients (`build_hold.py record-recipient`; `main` returns 1 when a record fails); each unaddressable one prints `not reachable: <name> [<session id>] has no messaging socket` on stderr. This session is left out by `CLAUDE_CODE_SESSION_ID` alone; unset or empty prints `top_level: CLAUDE_CODE_SESSION_ID is unset, so this session cannot be left out` and exits 2. `/notify_top_level` sends to each `uds:` address, names held or refused deliveries by name and address, and reports each `not reachable` line as a session not told.

### Dailies (`scripts/production/dailies_render.py`)

- Holds: `read_dailies_hold()` reads the holder files. Report and `--footer` print one `build hold: <holder> since HH:MM <zone>, for <purpose> - release eta: <HH:MM ZONE (N minutes) | (overdue N minutes) | unknown>` line per holder, then the cycle's state lines (indented) when a holder belongs to the cycle. Refusals (exit 2): a top-level `build_hold` field; a unit marked with no holder file; active holders with no marked unit; a marked unit whose `unit` equals an active holder's name (`units.build_hold: <unit> holds the build hold itself; remove its marker`); production plumbing words in a holder purpose (re-hold with other words).
- `### Agents` sits after the hold lines and above `waiting on you:` (the `--footer` form writes the same lines as bullets, with no heading): one line per `state: active` note in `AGENTS_DIR` (`~/rust/hanadocs/agents/`), by file stem, e.g. `- codex 2: 78%; runs out about 20:45 PDT today, before its Sun 02:25 refill; 1 reset available until Oct 29`; `- none active` otherwise. Run-out = reading time + (100 - used) / rate, rounded to the minute. The rate is `run_out.weighted_rate` over the `READINGS_LOG` readings: each rise weighted by half per 12 h of age; refills, time at 100% and gaps over 15 min are skipped. Until the readings cover a weighted hour, it is used percent over the time since the later of the last refill and 24 h ago. `scripts/whoami/agent_notes.apply` appends `{"account", "at", "remaining"}` to `~/.local/state/agent-notes/readings.jsonl` and keeps 8 days.
- `same_phase` compares only the title after the first `: `, so a renumbered phase keeps its ETA history and a retitled one starts afresh. `draw_from` sets the latest cell only when it is after the ETA cell.

### Disk-floor alerts (`scripts/lint/sweep.py`, `scripts/buildlog/disk.py`)

`hold_floor` writes `floor.json` in `${LINT_SWEEP_STATE_DIR:-~/.local/state/lint-sweep}` under `FLOOR_LOCK` after each floor sweep: time, free bytes, build-cache bytes (idle and busy target dirs plus both CI runner trees), `last_alert_at` and `last_push_at`. After a non-dry sweep it sends at most one alert:

- **Floor out of reach** (every removable unit chosen, still `left > budget`, and free space under the floor after removals): phone (`send.py --to user --need note --summary "natedev: disk under its floor" --text <message>`) and natedev, on its own hour (`push_history`).
- **Growth** (free space fell more than `UNEXPLAINED_FALL_BYTES` 10 GiB beyond build-cache growth since a floor sweep at most `GROWTH_WINDOW_SECONDS` 30 min earlier) and **large removal** (more than `LARGE_REMOVAL_BYTES` 32 GiB): natedev alone (`send.py --to natedev --from disk_floor`), at most one per `ALERT_INTERVAL_SECONDS` (1 h) counted from the last delivered alert.

`send_floor_alert(message, channels: FloorAlertChannels.NATEDEV | NATEDEV_AND_PHONE)` returns whether either `send.py` call delivered: exit 0 or 1 counts for the natedev message, while only exit 0 counts for the user channel. The texts name what fell, what grew and the largest directories outside the caches, read from `disk.json` as `AvailableDiskMeasurement | UnavailableDiskMeasurement`. `buildlog disk` adds `outside_build_caches` (directories one and two levels under each measured folder holding at least 1 GiB outside cargo target dirs, with growth and largest child), `outside_build_cache_totals` and `previous_measured_at`. `send.py`'s `log.jsonl` lines carry `text`.

### Rust release trial

`rust_release.py` keeps a trial ended by a kill (status 128 or more, -9/-15, or signal 9/15 or SIGKILL/SIGTERM in its output) at `waiting` with reason `<step> killed; retry next night`; a genuine failure stays `failed`.

### `verify.sh` example guard

A gate run (`test <pkg>` with no `--filter` and no named target, `--features` allowed) and `final` read `cargo metadata --no-deps --format-version 1` once into `GATE_METADATA` and run `UNTESTED_EXAMPLES_PY` before the cache lookup, any build and `final`'s format check. An example target is an offender when a source line matches `^\s*#\[(cfg\(test\)|(\w+::)*test)\]`, whatever its `test` setting; a directory example (`examples/<dir>/main.rs`) is scanned through every `.rs` under its directory. Each offender prints `example <name> holds a test: <file>:<line> has <attribute>, and examples carry no tests.` and `move it into <package>'s src/ or tests/, or delete it.`; then exit 2 with no cargo call besides `metadata`. `read_metadata` reuses `GATE_METADATA`. `verify.sh example-test` exits 2 with `verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).` before anything else.

### Codex launcher capacity (`scripts/agents/codex_mesh.py`)

Turns end as `TurnCompleted | TurnRefusedForCapacity | TurnFailed`; runs as `RunCompleted | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted`. Capacity is the error's structured `serverOverloaded` / `flexUnavailable` or the message `Selected model is at capacity`. Capacity resumes the same thread on the same roster entry: waits from `CAPACITY_WAIT_SECS` (30 s) doubling to `CAPACITY_MAX_WAIT_SECS` (300 s) within `CAPACITY_BUDGET_SECS` (1200 s), one seat-log line per wait, and a resume turn saying the last turn stopped for capacity with its edits in the tree. The budget is per busy spell: any turn that ends without a capacity refusal resets it, and an owed resume after such a turn starts at once. Roster status `waiting_capacity` holds the thread id and `launcher_pid`; once a spell's budget runs out it is `capacity_exhausted` and `start` exits 1 with `codex_mesh: <seat>: model still at capacity after <N> retries over <M> min; thread <id> stays on the roster (codex_mesh.py end --to <seat>)`. `send` to `waiting_capacity` stores the message in `<seat>.pending.json` under an flock for the resume turn; `send` to `capacity_exhausted` exits 2; to a finished (`done`/`failed`) seat it exits 1 with `codex_mesh: <seat> is <status>, not running; read its summary file`. The fast-failure retry on a new app-server runs only for `FailedBeforeThread`. Liveness comes from `thread/read`, typed `ThreadLive(turn_id) | ThreadActiveWithoutTurn | ThreadIdle | ThreadStateUnknown(reason)`: `idle`, `notLoaded` and `systemError` (the status the app-server leaves after any failed turn, a capacity refusal included) read `ThreadIdle`, and a status the protocol does not define reads `ThreadStateUnknown`. A relaunch replaces an entry only on `ThreadIdle`, except a `failed` entry, which relaunches after `_end_unwatched_turn(port, thread_id, log_path) -> RelaunchAllowed | RelaunchBlockedByLiveTurn` repeats the dead launcher's cleanup (blocked: exit 2, `thread <id> still has a live turn that could not be interrupted; relaunch once it ends`), and a `waiting_capacity` entry whose launcher is dead, which relaunches without a read. `agent-registry.md` (Addressable codex delegates) has the full mechanism.

## Invariants

- Every compiling step and every BRP launch passes the memory gate before it starts. Sweep and `cargo fmt` steps never wait on it, so the disk floor is never held by memory.
- The gate never fails or alters a step: no reading means no wait, the limit always starts the step, and `build_hold_mark` swallows every error. Ordinary builds without a holder file pay no lock or directory access.
- `BUILDLOG_MEM_WAIT_S` and the gate outcome are set in the calling shell before any `| tee` pipe; a step's recorded start excludes the wait.
- The sccache server stays in `builds.slice`; moving only the steps would leave every compile outside the limit.
- Raising a step's oom_score_adj never stops or alters it: a failed write is silent, the `|| true` keeps the scope shell running when a caller exports `SHELLOPTS=errexit`, and exit status and output pass through.
- A memory kill is never recorded or cached as the tree's lint or test failure; it re-runs once and stops at the second.
- A cargo token is reclaimed early only through a `holder_pid` that names a gone process; holder and waiter share one pid namespace. The guard file is never removed.
- Holder files are the one source of hold state. Cycle state lives outside the holder directory, and the last holder file stays until every held session has passed, so every reader keeps seeing the hold during a release.
- Releases go by session id, never by name. A session that cannot be messaged is never recorded as a recipient.
- Every change to a hold cycle happens under `release.lock` and re-reads stored state; nothing reads an earlier cycle. A damaged record is set aside by a changing command, never by `status` or the dailies.
- `steps.mem_wait_s` is the memory wait and `steps.wait_s` the call's lookup and token wait; any column change bumps `SCHEMA_VERSION`.
- Only `ran` CI jobs carry duration and queue time. CI days key on the run attempt's `started_at` everywhere.
- The window report is host-scoped; the daily report aggregates hosts. A run's own peak comes from `ci_anon_bytes` / `builds_anon_bytes` samples, never from `memory.peak`.
- Only the floor-out-of-reach alert reaches the phone; diagnostics go to natedev.
- Tests never read the real cgroup tree, `/proc/meminfo`, journal or transcripts, and never write `~/.local/state/buildlog`, `build-hold`, `build-hold-release`, `notify` or the agent notes: they set `BUILDLOG_MEMINFO`, `BUILDLOG_*_CGROUP`, `BUILDLOG_TRANSCRIPTS`, `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR`, `HOME`, and stub `journalctl`, `zramctl`, cargo and git on `PATH`.
- A verify.sh routing test follows `scripts/delegate/test_verify_untested_examples.py`: cargo passes `metadata` to real cargo and logs every other call, git is stubbed, and the environment drops `PLAN_DELEGATE_BOARD_DIR`/`PLAN_DELEGATE_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILDLOG_MEMINFO`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`.
- Times carry their zone. natedev's clock and journal are EDT; sessions and printed lines state PDT; an `HH:MM` hold ETA names its zone.

## Calibration and gotchas

- **12 GiB floor.** The gate admits at `MemAvailable` 12 GiB, steve's slot floor. Quiet use outside builds is about 20 GiB of 60.5. The limit message says "15 min" whatever `BUILDLOG_MEM_WAIT_LIMIT_S` holds.
- **The gate does not stagger.** Measured 2026-10-04 22:42 PDT: four held sessions started builds within 73 s of one release with zero memory waits, because memory was ample at that instant and compilers grow after admission. That is why the release is one session at a time with a 60 s settle after each gate return.
- **A held session's turn can take about 16 minutes** (gate up to 900 s), so `release` runs in the background and the BRP hook's timeout is 960 s.
- **Release outcomes vanish with the last holder file**; set-aside `*.damaged-*` files are never cleaned up. A holder joining a no-cycle hold learns nothing until the final release line tells it to broadcast.
- **`read_release_state` assumes a mapping**; `read_cycle` checks each entry is an object first, since `"entries": [null]` would raise `AttributeError` outside every handler.
- **earlyoom never logs "killed";** its line is `sending SIGTERM to process <pid> uid ... "<comm>": ...`.
- **zram is invisible to slice limits.** earlyoom counts zram's virtual size as swap; `MemoryMax` counts RAM only, and compressed pages are charged to no slice.
- **`MemoryHigh` on a slice holding a heartbeat** (the runner's `Runner.Listener`) stalls it until GitHub drops the runner; such a slice gets `MemoryMax` only and its services `OOMPolicy=continue`. `MemoryHigh` on `builds.slice` throttled builds not short of memory, since it counts page cache.
- **At its ceiling CI hits `MemoryMax` thousands of times with no kill;** a large `max` delta is reclaim, and its cost shows as stall seconds. `memory.peak` never falls without a reset; CI's reads above 18G from before the limit existed.
- **CI raise threshold.** Raise CI's `MemoryMax` only for an isolated run (one CI attempt alone in `hana-ci.slice`) with an `oom_kill`, or repeated isolated runs with sustained ceiling hits and at least 60 s of slice stall each, after checking minute coverage and build mix. Both runners share the slice, so an overlapping run is no evidence for a per-run raise. A changed limit needs another measured day after it.
- **CI's jobserver.** Inside a runner unit (`PrivateDevices`, `DevicePolicy=closed`) the pool device exists only with `BindPaths` and `DeviceAllow`; with an ACL alone `CARGO_MAKEFLAGS` yields a jobserver warning that hana's release job treats as failure.
- **sccache.** `sccache --start-server` forks and exits, so a service using it dies at start; with `SCCACHE_START_SERVER` in the environment every other sccache command fails, hence `ExecStart` only.
- **A step's own peak and stall exclude its compiles**, which run in `sccache.service`; the slice samples and snapshots see them.
- **sccache's compiles do not inherit a step's 500.** The server starts them in `sccache.service`, not in the step's scope; the unit's `OOMScoreAdjust=500` is what ranks them with the build steps; without it they sit at 200 with the sessions' processes.
- **Launch coverage.** `launches.py` globs one transcript level and misses launches in subagent transcripts (11 of 37 on the measured day). About a third of real launches lack `parameters.path`; `metadata.workspace` is only a folder name. `binary_path` appears for examples too, so only `metadata.launched_as` names an app. Counts are per pass; a launch count comes from stored `brp-launch` steps. Branch and SHA are collection-time facts. Transcript fixtures copy real lines.
- **CI re-runs.** GitHub copies every finished job into the new attempt under a new `job_id` with `created_at` at the re-run and the earlier stamps, which read raw gives a negative queue time and a duplicate duration.
- **Build-folder waits.** The `calls` table also holds port-lint calls, so every query over the call population, counted calls and token holders alike, filters `tool = 'verify.sh'`. Holders are fetched by `ended_at`, not `started_at`, so a holder that began the previous day is found.
- **Mac commands.** Tailscale SSH runs them under `/usr/bin/login`, which exits 0; judge the printed `rc=`. A sync pause never expires (the report's Mac line shows it), an unreadable pause file reads as paused, and `pause` can wait up to 30 s + 2 x 600 s for a running sync. `polled.json` is in `ci/` so sync carries it to the Mac; `sync_paused.json` stays local.
- **Disk.** send.py exit 1 (queued) counts as delivered. `disk.py`'s first walk is the cost (35 s under load 104). A file hard-linked into a target dir and elsewhere counts outside the caches. `floor.json` and `disk.json` hold only the latest record; past sweeps come from `journalctl --user -u disk-floor.service`. `sweep.py` reads `disk.json` by path and never imports `scripts/buildlog`, since `disk.py` imports `sweep`.
- **procps cuts user names** over 8 characters unless the format says `user:32`. With `UnknownCores`, `quiet` waits its full `--max-wait`.
- **Codex.** On an idle thread the app-server opens a turn per queued message, so never `thread/queue/add` to a thread no launcher streams. `thread/read` can report active with no turn id; get the id from a steer probe before `turn/interrupt`. `turn/interrupt` errors when the turn ended in between; only a re-read tells that from a live turn.
- **Dailies.** The run-out time rounds to the minute in epoch seconds (rounding aware local time loses the DST fold). `resets` and a naive `limit_reset` are the writing machine's wall time. Every subprocess dailies test sets `HOME` to a temporary directory.
- **`verify.sh` guard.** A failing `cargo metadata` exits with cargo's status under `set -e`, not 2.
- **Live scripts.** Units run `~/.claude/scripts/delegate/board.sh` and `verify.sh` while they change, and bash reads a script as it runs: replace either by writing a temp file beside it, setting its mode, and `mv`ing it over.
- **basedpyright** exits 3 in every checkout (`pyrightconfig.json` names an absent `.venv`); 0 errors and 0 warnings in its output is the gate.

## Why

- **Admission by free memory, not by slice limit.** `memory.high` counts page cache and throttled builds that were not short of memory; `MemAvailable` is what earlyoom acts on. 12 GiB matches the floor steve already uses for slots.
- **One slice for every session build, sccache included.** Every compile reaches the sccache server, so the server's cgroup is where compiles are charged; a limit on the step scopes alone would bound nothing.
- **CI in its own pool, a smaller slice and a lower kill order.** Shared steve slots alone did not stop kills (a CI run lost both Linux jobs after it). Its own 14-slot pool keeps CI from starving sessions and the reverse; `PIPELINE_JOB_OOMSCOREADJ` is the only setting that reaches job processes, because the runner sets every job to 500 itself. At 100 a CI job goes after a build step (500) or a session process (200) of the same size.
- **Build steps at 500, and no `--prefer`.** The level sets the order and size decides within it. A `--prefer` for compiler names (+300) let two hung hana tests (11.4 GB, 15.5 GB) outlive 26 smaller processes, six CI compilers among them; without it, a hung 15 GB test scores about 1100 against a 2 GB CI compiler's 750, and a session's rust-analyzer at 200 stays behind every build step of similar size. The adj is set inside the step's scope, so every process the step starts inherits it.
- **One session at a time.** A held release otherwise starts every session in the same minute (the 11:12 PDT episode on 2026-10-04: 21 kills); the gate reads memory before compilers grow, so it cannot spread them. The settle starts at the previous session's gate return, whatever its outcome, so the next gate sees the previous compiler.
- **Typed release states with their own instants.** Each state carries only what is valid in it, so a reader cannot treat a queued delivery as a release, and only `Granted` claims memory was there.
- **A damaged record releases everyone, not no one.** Setting it aside and falling back to a broadcast cannot strand held sessions; each still passes its own memory gate.
- **Broadcast by id.** Names can belong to two sessions, and `top_level.py` once deduped them, so one of two same-named sessions never heard the hold.
- **A memory kill re-runs once.** The kill says nothing about the tree; a second kill stops the call so a starved machine does not loop.
- **zram over zswap.** A cache in front of a full swapfile adds no capacity; zram adds compressed space that does not depend on the swapfile.
- **Every average gets a p95.** An average hides the long runs that make waits; nearest rank needs no interpolation and matches the old p75 index for every n.
- **Two build-folder rows.** A seat's own verify.sh calls run one at a time, so its wait behind its own call costs nothing; only the wait behind another seat of the same delegate session is delay.
- **Ruled out** (do not re-propose without new evidence):
  - systemd-oomd for builds: it kills a whole cgroup, and every session's compiles share one.
  - Admission by each crate's measured peak, or a per-crate gate: no source records the peak, and no isolated run met the threshold.
  - A `cargo` shim: compiles reach the slice through sccache.
  - A nextest thread cap or a per-run memory cap in `verify.sh`: no isolated run met the threshold, and the widget drawn-pixel test group already bounds the largest step.
  - A `MemoryHigh` gate or `MemoryHigh` on either slice (see gotchas).
  - A `CARGO_BUILD_JOBS` fallback on the runners: the pool's jobserver reaches CI jobs.
  - A `steve-clients` group: the ACL already grants the runner users, and group membership is fixed at login.
  - A fixed 5-minute admission timeout: a first build that starts 10 minutes after delivery would overlap the next.
  - Catching `AttributeError` instead of validating the cycle shape: it would hide code defects too. `release --resume` releasing the sole holder of any no-cycle hold: it could end a hold nobody had begun releasing.
  - A 10-minute snapshot search: an edge snapshot more than 2 minutes off misplaces the window. A per-call kill count alone: it cannot tell two single kills from one step killed twice.
  - Keying `ci_section` by job `created_at`: drops post-midnight jobs of a run started before midnight.
  - Counting build-folder wait that no same-session holder covers as the call's own: it would hide waits with no recorded holder.
  - A hook on `brp_launch` to record launches, or recording from inside bevy_brp_mcp: the collector reads transcripts instead. Appending launches to the host's monthly file: the per-launch file makes the record id the dedupe.
  - Keying the example guard on the target's `test` setting or advising `test = true`: examples carry no tests (user, 2026-10-04).
  - Withholding the phone push when a removal failed, or cutting a path's last component to fit 1,024 characters.
  - Queuing a message to an out-of-retries Codex thread (it opens a turn nothing streams); changing the service tier on capacity; adding `failed` to `ENDABLE_STATUSES`.
  - The dailies renderer importing the note code from `scripts/whoami`: it parses the flat frontmatter itself and imports only `run_out`.

## Not built

`verify.sh ... --target-dir <name>` (a per-helper build folder with its own cargo token, pass records shared across folders, `calls.target_dir`, schema 10) is parked. It returns when the report's `Build-folder turn, behind another seat` row shows real time queueing for a worktree's build slot.
