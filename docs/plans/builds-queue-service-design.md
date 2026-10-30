# Build queue service: design and migration

> **Status: DESIGN for user review. No code is written until the user approves it.**
> Production: builds. Unit: queue-unit. Written 2026-10-30.

## The user's words (2026-10-30, relayed by the builds showrunner)

> "why does every build run the queue code - why don't we have a system where there is a queue that guards and releases?"

> "should we use rust or python?"

The showrunner's answer to the second question: Python, for one memory model and no tool build.

## Summary (one screen)

**Why every build runs the queue code.** No process holds the queue. The queue exists only as files under `admission/`, so each waiting step must rebuild it to know its place. Every 5 s, each waiting step starts its own Python check. The check takes the shared lock, reads all of `/proc` and every queue file, decides for that step alone, and writes its row. The gate was built this way when there was no service to ask, and the cost grows with the number of waiters. A queue that guards and releases is the fix, and this design builds it.

**What changes.** Today every waiting build step starts a new Python process every 5 s. Each process takes the admission lock, scans all of `/proc`, decides for itself, and writes its own file. One Python service will hold the queue in memory instead. A build step connects to a Unix socket, asks once, and waits on the socket. The service decides for all waiters in one pass when something changes, and answers the step when it may start. A step that ends, or dies, frees its memory at once: the kernel tells the service.

**Why.** The polling costs CPU and time. It also makes every queue question wait for the lock.
- 2026-10-30 10:30–11:00 EDT (load 130–184, 29–32 waiters): the lock was held exclusive in 77% of samples. A reader waited 0.13–7.24 s for it (median 2.07 s), so `/build:queue` took 3.3–6.1 s.
- 2026-10-30 11:31 EDT (load 61–80, 19 waiters): 3.5 check processes per second, at least 0.48 of a core.
- A step can start up to 5 s plus one lock wait after memory frees.

**What it costs.** About 37 h of worker time over 3 phases. One systemd user unit and socket on natedev. The Mac is not affected: it runs no memory admission.

**Down rule.** No build starts without an "admitted" answer from the service. The kernel holds the socket (socket activation), so a service restart is a short wait for the build, not an error. If the service cannot start, steps keep waiting, print why, and the user gets one alert. There is no second admission path.

**Phases.** S1: one decision pass. S2: the service runs in shadow. S3: builds, views and commands move to the service, and the same phase deletes the polling path. S2 needs the natedev NixOS session. S1 and S3 change other units' files. No second admission path exists at any time; the only rollback is a git revert (user, 2026-10-30 11:42 EDT).

---

## 1. Today, measured

**Why every build runs the queue code.** `buildlog_wait_for_memory` (`scripts/lint/memory_gate.sh:183`) loops while the step waits. Each pass starts `memory_admit.py check` (`memory_gate.sh:230`), sleeps `BUILDLOG_MEM_POLL_S` (default 5 s, `memory_gate.sh:191`), and repeats. No process holds the queue, so each waiter rebuilds it from files on every pass. Each `check` (`scripts/lint/memory_admit.py:1662`):
- takes `LOCK_EX` on `admission/lock` (`memory_admit.py:1708`);
- reads every `/proc/<pid>/stat` (`process_table`, `:1181`): 0.05–0.34 s under load (speed.md, 2026-10-30);
- reads every reservation's process tree, `.max` and `.wave` files;
- runs `reservation_blocker` (`:1897`), which runs `head_priority` once more per running step and globs every `~/.claude/sessions/*.json`;
- writes its own `.waiting` file with a non-atomic `write_text` (`:1795`).

**What it costs.**

| Measure | Value | Source |
| --- | --- | --- |
| Check starts per second | 3.5 (19 waiters); about 6 (29 waiters) | Measured 2026-10-30 11:31 EDT, 30 s `/proc` sample; speed.md §3 |
| CPU of live checks | at least 0.48 core (19 waiters) | Same 11:31 sample; lower bound, last tick seen per process |
| Lock held exclusive | 22–30% (load 61–80); 77% (load 130–184) | 11:31 sample; speed.md §3 |
| Exclusive hold per check | median 0.10 s, max 0.77 s | speed.md §3 |
| Reader wait for the shared lock | 0.13–7.24 s, median 2.07 s | speed.md §3 |
| `/build:queue` | 3.29–6.07 s | speed.md §1 |
| Decision delay after memory frees | 0–5 s poll, plus one lock wait | `memory_gate.sh:191`, `:290` |

**Faults it causes.**
- *Lock waits.* Linux `flock` gives a shared request no preference, so views, `bump` and `blocker` queue behind the checks.
- *Torn rows.* `.waiting` and `.reservation` files are written in place, so a lock-free reader can see a partial file (speed.md lever 1).
- *Late reclaim.* A dead waiter or owner leaves the queue only when the next check reads its file (`live_waiters`, `:1462`; `live_reservations`, `:1446`). The cancel path in `scripts/direct/verify.sh:1171–1181` has to carry reservation paths across a subshell, so that a kill does not leak them.
- *Two copies of the decision.* `check` and `read_queue_pass` (`:1975`) each build the waiter list and call `head_priority` and `queue_decision` separately. The view can disagree with the check that runs one second later.

**Also seen.** In the same 30 s, merge-gate test runs under `/dev/shm` started 46 checks and 30 releases against fake state. A design for tests must keep each test on its own private state (§8).

## 2. The service

**Choice.** One Python 3.13 process: `scripts/build_queue/admission_service.py`. It is single-threaded and uses one `selectors` loop, with no threads and no locks. It holds in memory:
- **waiters**: the connection, the owner (pid, start time), the profile and need, `since`, `bump_order`, `holds_younger`, `priority_since`, and the identity (session id, name, step, folder);
- **reservations**: the owner, the scope cgroup path, the need, `admitted_at`, the width, the call id, the shape, and later the test-phase need;
- **recovery spacing**, and the last host reading (meminfo, PSI, sccache anon);
- **the last pass result**: the decision and reason for every waiter. Views read this result.

**Persisted.** One file, `~/.local/state/buildlog/admission/state.json`. It is written with a temporary file and `os.replace` after each change to reservations, bump state or recovery. Waiters persist only their order fields (`since`, `bump_order`, `holds_younger`, `priority_since`), keyed by owner. A restarting waiter gets its place back. `anon_peaks.jsonl` and the build log rows do not change.

**Start and restart.** A systemd user socket unit (`build-admission.socket`, `ListenStream=%t/build-admission.sock`) and its service (`build-admission.service`, `Restart=always`, `RestartSec=1`, `WatchdogSec=30`). The service sends `WATCHDOG=1` from its loop to `$NOTIFY_SOCKET`. It runs outside `builds.slice` with `OOMScoreAdjust=-900` and `CPUWeight=1000`, so earlyoom does not kill it and it answers under load.
- *Restart with running builds.* The new process reads `state.json`. For each reservation it checks the owner (pid and start time) and the scope (`cgroup.procs`), as `reservation_lifetime` does on resources-unit's branch today. Live rows keep their charge, and ended rows are finalized.
- *Restart with waiting builds.* Their connections close. The client reconnects at once and sends its first request again with its original `since`. The service restores the waiter's order fields from `state.json`. Its place does not change.

**Protocol.** Newline-delimited JSON on a Unix stream socket. One request per connection. Every request carries `"v": 1`.

| Request | Reply |
| --- | --- |
| `admit` {owner pid, start time, argv, cwd, since, sidecar, identity, call id} | Lines of `wait` {reason, position, head, text} on each change, and at least every 60 s. Then one `admitted` {reservation id, need, source, ...} or `refused` {reason}. |
| `launch` (BRP, no argv) | Same as `admit`. No reservation is made. |
| `release` {reservation id, status} | `ok` |
| `width` {reservation id, width} | `ok` |
| `view`, `explain`, `mine` {session id} | One JSON answer from the last pass |
| `bump` {selector, actor}, `admit_head` {actor, minutes} | `ok` or `refused` |

A service that gets an unknown `v` answers `refused {"reason": "client version N, service supports 1..M"}`. The client stops the step with that line.

**How a build asks and waits.** The shell cannot hold a Unix socket, so `memory_gate.sh` runs `admission_client.py admit ...` in the foreground. The client is one Python start per step, not one every 5 s. It prints the same wait lines as today, from the `wait` messages. On `admitted`, it prints one protocol line and exits. The leave hook (`LINT_QUEUE_LEAVE_HOOK`) stays in the client: it runs every 5 s, as today, and a success sends `cancel` and exits as `LeftQueue`.

**Release (recommendation).** The connection is the waiter's lifetime, but not the reservation's.
- *Waiters.* A waiter ends when its connection closes, or when its owner process exits. The service watches the owner with `os.pidfd_open`. The kernel signals the service at once in both cases, so no dead row stays in the queue.
- *Reservations.* A reservation ends on an explicit `release` (which carries the exit status for `anon_peaks.jsonl`). It also ends when the owner has exited and the step scope is empty. The service learns this from the pidfd and from inotify on the scope's `cgroup.events` (`populated 0`). Both events come from the kernel, so a crash releases at once, and there is no polling.
- *Why not the connection.* Holding a connection for a whole build needs one helper process per running build. Killing that helper alone would free memory that the build still uses. A service restart would close every connection and free every running build's charge. That is the over-admission the gate exists to prevent. With pidfd and `state.json`, a build that outlives a restart keeps its reservation.

## 3. Decisions stay in one place

**Choice.** Make one pure function, `admission_pass(inputs: PassInputs, now) -> PassResult`, in `memory_admit.py`. It decides for every waiter in one pass. `PassInputs` holds the waiters, the reservation memories, the host reading, the pressure, the recovery spacing and the limits. `PassResult` holds, for each waiter, the `AdmissionDecision`, the reason (`WaitReason`), the blocker, the head window and the admit order. Today's `decide`, `build_charge`, `charge_basis`, `growth_headroom`, `head_priority`, `queue_decision`, `head_window`, `head_wait_text`, `reservation_blocker` (its rule part), `head_after_steps_end` and `smallest_head_release` stay as they are. `admission_pass` calls them, and nothing else calls `queue_decision`.

**What moves where.**
- `memory_admit.py` keeps the rules, the estimates (`expected_peak`, `filtered_peak`, `gate_estimate`, Phase 24's shape promises) and the readers (`read_meminfo`, `read_pressure`, `reservation_memory`, `sccache_memory`). The service and the old `check` both import it.
- In S1, `check` and `read_queue_pass` both call `admission_pass`. This removes today's second copy before the service exists.
- In S3, `check`, `locked_queue_read`, `current_blocker`, `blocker_snapshot`, `live_waiters`, `live_reservations`, `queue_rows`, `run_action`'s file protocol and every `flock` are deleted. The service is the only caller of `admission_pass`.
- Estimates (git identity, history reads) run in the client, before it connects, as they run outside the lock today (Phase 12). They are sent in `admit`. The service checks the estimate's age (`HEAD_SECONDS`) and sends `wait {"reason": "estimate expired"}`, and the client re-estimates.

**When a pass runs.** On every event: an admit, a cancel, a release, an owner exit, an empty scope, a width change, a bump, or an override. It also runs once per second while any waiter waits, because memory freed outside builds shows only in `/proc/meminfo`. A pass reads meminfo, PSI, sccache's `memory.stat`, and each running scope's `memory.current` and `memory.swap.current`. It reads the `/proc` tree only for unscoped reservations.

## 4. Commands

| Command | Today | With the service | Who may call |
| --- | --- | --- | --- |
| View (`/build:queue`, Phase 1 card) | Lock plus `/proc` scan, or Phase 1's `queue.json` | `view` from the last pass. Labels from `roster.unit_labels_for` (Phase 1). | Anyone |
| Why (`build_why.py`) | `explain_queue` under the lock | `explain` | Anyone |
| Mine (where am I, who is ahead) | Phase 1 per-step answer | `mine {session id}` | Anyone |
| Override: admit the head (resources Phase 32) | Locked file write | `admit_head` | The user only: from `/showrunner:bump` or `/build:queue bump`, which pass the user's command as the actor. The service records the actor. |
| Priority (resources Phase 26/30, `/showrunner:bump`) | `bump_waiter` (`:2211`) | `bump {selector, actor, minutes}` | Same as the override |
| Width change | `set_width` (`:2280`) | `width` | The step's own client |
| Release | `release` (`:2250`) | `release`, or the owner ends | The step's own client; the kernel |
| Build log rows (`queue_bump`, `anon_peaks.jsonl`, Phase 26's `queue_times.jsonl`) | Written under the lock | Written by the service, from the same functions | The service only |

The socket file is mode 0600, so only the user's processes can connect. The service cannot tell a user from an agent, because both run as the same Unix user. The rule "only the user bumps" stays where it is enforced today: in the commands that the user runs.

## 5. When the service is down

**One rule.** No step starts without an `admitted` answer. The client never falls back to the file lock.

| Failure | What happens | What the user sees |
| --- | --- | --- |
| Service crash | systemd restarts it in 1 s. The socket stays open, so new connections wait in the backlog. Waiters reconnect with their `since`. Reservations come back from `state.json`. | Nothing, or one `admission service restarted; waiting again` line in the step output. |
| Hang | The watchdog (30 s) kills and restarts it. A client that gets no `wait` line for 120 s prints `admission service not answering since HH:MM` and keeps waiting. | That line. Builds start again after the restart. |
| Crash loop | systemd stops after its start limit, and the socket refuses. Clients retry every 2 s and print one line a minute. `OnFailure=` sends one push through send.py's outage path after 2 min down, one per outage. | One phone alert: `build admission service is down; builds wait`. |
| Version skew | The service answers `refused` with both versions. The service supports versions N and N−1 for one release. The install step restarts the service after the clients are in place. | The step stops with that line, only in a mixed install. |

**No second path.** The only rollback is a git revert of S3 (user, 2026-10-30 11:42 EDT).

**Rejected.** Falling back to the file lock: that is two admitters that cannot see each other's reservations, so both admit into the same free memory. Clients without socket activation: every restart would show as an error.

## 6. Performance targets

| Measure | Today | Target | How it is reached |
| --- | --- | --- | --- |
| Decision delay after a release or step end | 0–5 s plus a lock wait (median 2.07 s) | under 0.2 s | The event runs a pass at once. |
| Decision delay after free memory rises outside builds | 0–5 s plus a lock wait | under 1.2 s | The 1 s pass. |
| `/build:queue`, build-why, mine | 3.3–6.1 s | under 0.25 s at load 150 | Python start 0.03 s, lean imports, one socket round trip, the answer from memory (speed.md §6). |
| CPU at 30+ waiters | about 6 Python starts a second, at least 0.48 core at 19 waiters | under 0.02 core | One pass a second reads a few small files, about 5 ms (to be measured in S2). |
| Lock time | 22–77% held exclusive | none | No lock. |

## 7. Migration plan

The resources-unit chain edits `memory_admit.py` serially: 21 → 16 → 25 → 24 → 26 → 15 → 27 → 20 → 11 → 17 → 18 → 19 → 8, with 30, 31 and 32 before 26 (`builds-resources.md:391`, `:809`). Those phases change rules, so they keep their value: the service runs their rules. The service makes these moot:
- speed.md lever 5 (shorter exclusive holds);
- the lock timeout path in `locked_queue_read`;
- Phase 32's file-write transport (its rule stays, and it becomes the `admit_head` command);
- queue-unit Phase 1's `queue.json`. S3 deletes it and its writer. Phase 1 still lands, because it brings a sub-second view now (see Questions).

| Phase | Goal | Files (owner) | What changes for builds | Proof | Rollback | Effort |
| --- | --- | --- | --- | --- | --- | --- |
| **S1 One decision pass** | `admission_pass` decides for all waiters. `check` and `read_queue_pass` call it. | `memory_admit.py`, `test_memory_admit.py` (resources-unit) | Nothing. | The existing tests pass unchanged. A test feeds one `PassInputs` to the check path and the view path and asserts the same decisions. Land after Phases 30, 31, 32 and 26, and before 15. | Revert the commit. | 6 h |
| **S2 Shadow** | The service runs, owns nothing, and logs what it would decide. | `admission_service.py`, `admission_client.py`, tests (queue-unit); systemd socket and service in `/etc/nixos/modules/linux/development.nix` (natedev session) | Nothing. The service reads Phase 1's `queue.json` and the reservation rows read-only every second. It runs `admission_pass` with its own fresh readings, and logs each "would admit W" with its time to `admission/shadow.jsonl`. | 24 h with at least 200 admissions (10-29 had 592, `builds-resources.md:403`). Adopt when: the service admits the same steps in the same order for at least 99% of real admissions; it never admits when a check at that instant held on budget, free memory or pressure; and its median "would admit" time is at least 2 s before the real admission. | Stop the unit. | 10 h |
| **S3 Cut over and delete the polling path** | All callers, views and commands use the service, at once. The same phase deletes `check`, the locks, the file readers, the poll loop, `.waiting`, `.reservation`, `admission/lock` and the Phase 1 `queue.json` writer. | `memory_admit.py`, `memory_gate.sh`, `pre-tool-use-brp-launch-gate.sh`, `build_hold.py` `admitted_processes` (resources-unit); `invoke.sh`, `verify.sh` (verify-unit); `merge_gate.py` `gate_reservation`, `shutdown/stop.py` (natedev showrunner); `mac_idle.py`, `build_why.py` (report-unit); `build_queue.py`, `commands/build/queue.md` (queue-unit); `docs/as-built/build-memory-admission.md` | One Python start per step instead of one every 5 s. The cancel path in `verify.sh:1171–1181` drops its reservation list, because the owner's end releases. The view answers from the socket in under 0.25 s. | Before the cut-over: the full merge gate and the in-process server tests pass. Cut over in a quiet hour. At start, the service imports the live `.reservation` files once, so running builds keep their charge. Then: `grep` finds no `flock` and no `.waiting` in admission code; 50 timed views at load above 100 all under 1 s, p95 under 0.25 s. 24 h checks: no earlyoom kill caused by admission; `steps.mem_wait_s` against the 24 h before; no step that started without an `admitted` row. | A git revert of the phase. No mode file and no second path. | 21 h |

Total: about 37 h of worker time over 3 phases. Packaging call: views and commands (the former S4) fold into S3, because a separate phase would keep the `queue.json` writer alive as a second copy of queue state.

## 8. Tests

- **In-process server.** `AdmissionServer(state_dir, clock, readings, proc)` runs its loop in the test thread on a socket in a temporary folder. `ADMISSION_SOCKET` selects the socket, and the installed client refuses it when the folder is not the test's own (as `BUILD_HOLD_DIR` is refused today). Merge-gate test runs then never reach the live service.
- **Fake clock.** The server takes `now()` and timers from a `Clock` protocol. Tests advance it to cross `RAMP_SECONDS`, `HEAD_SECONDS`, the strict and backfill windows, and the recovery spacing, with no sleep.
- **Fake readings.** `fake_memory_readings` (meminfo, PSI, cgroup tree) as today. The live-memory tripwire stays and covers the service's readers.
- **Fake owners.** Tests spawn short `sleep` children for owners and kill them, so pidfd and scope events are real kernel events with no live builds.
- **Cases.** A crash between `admitted` and the first release (restart keeps the reservation); a restart with 30 waiters (order and `since` kept); an owner killed while it waits (the waiter leaves at once); a scope that outlives its shell; a client of version 2 against a service of version 1; a hung service (no `wait` line in 120 s); a crash loop (the client retries and never starts the step); and S1's equality of the check path and the view path.

## Rejected options

- One Python process per check, with a shorter poll: the cost scales with waiters, and the lock stays.
- A Rust service: a second memory model and a tool build (user, 2026-10-30).
- The connection as the reservation's lifetime: see §2.
- Fallback to the file lock while the service is down: two admitters (§5).
- A rollback path behind a mode file after the cut-over: the user did not approve it (2026-10-30 11:42 EDT). The rollback is a git revert.
- A TCP socket: it needs a port and authentication. A 0600 Unix socket gets both from the file system.
- Threads in the service: they need locks again. One loop decides in about 5 ms.

## Questions for the user

1. **Land queue-unit Phase 1 (the `queue.json` snapshot) now, and remove it in S3?** Settled by your words (2026-10-30 11:26: "queue can keep it running"): yes. It makes the view sub-second before the service exists. The cost is about 2 h of code that S5 deletes.
2. **A rollback path after the cut-over?** Settled by the user (2026-10-30 11:42 EDT, "i do not approve"): no second admission path at any time. S3 deletes the polling path in the same phase, and the only rollback is a git revert.
3. **Push to your phone when the service is down for 2 min?** Settled by the showrunner (2026-10-30 11:4x EDT): yes, one push per outage after 2 min down, through send.py's outage path. All natedev builds wait while it is down. Recommendation: yes, one push per outage.
