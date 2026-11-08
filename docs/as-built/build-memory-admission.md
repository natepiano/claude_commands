# Build memory admission: builds that fit in memory

## What it is

Every build on natedev, CI's and the sessions', is admitted so that it fits in memory, and earlyoom is a last resort rather than a routine event (target: a normal working day with no earlyoom kill). Before this, two admission systems could not see each other: CI ran outside steve in a slice whose limit counted page cache, sessions took steve slots that were admitted before their compilers grew, swap sat full of idle processes so earlyoom's swap condition always held, and a build hold released every session at the same instant. On 2026-10-04, 58 earlyoom SIGTERMs failed whole build steps and CI jobs. Now session builds and their compiles run in one bounded user slice, CI runs in its own smaller slice and slot pool with a kill order below the sessions', build steps sit above both so earlyoom kills the largest build first, every compiling step and every BRP launch waits for free memory before it starts, a build hold releases every held session at once into those gates, and the build log measures all of it: per-slice samples and snapshots, a window memory report, the day's waits, memory kills and CI freshness. The same body of work also hardened neighbors that share these files: the example-test guard in `verify.sh`, prompt reclaim of a cargo token whose `verify.sh` was killed, CI queue-time accounting, BRP launch recording, the disk-floor alerts, the Codex launcher's capacity handling, the dailies Agents section, and broadcasts to every live session.

## How it works

The service measures each handled request's wall time and process CPU time.
The measurements include configuration recovery for the logging threshold.
`request_slow_seconds=0.25` sets the logging threshold in `config/build_admission.conf`.
A request above that threshold logs its request kind, wall seconds, and CPU seconds to stderr.
The service reads the threshold at use.
An unusable threshold logs once through the existing configuration diagnostics.
Requests and cleanup continue when this logging setting is unusable.

### Cloud gates

`AdmitRequest.place` defaults to `LocalPlace()`.
This tree supplies the queue API and the cloud progress file.
The aws route integration belongs to a separate tree.
That route calls this API with `CloudPlace("aws")` for a `merge-gate` or `targeted-test` step.
The route process owns the reservation.
The service releases the cloud slot when that process ends.
An explicit release also ends the reservation.
A route that returns to natedev releases its cloud reservation before it requests local admission.

Cloud reservations need zero local memory.
They do not consume memory room, CI room, head protection, start spacing, or progress memory.
Local waits and cloud waits have separate admission orders.
Cloud gates wait in arrival order when all cloud slots are in use.
The wait text includes the used slots and the slot limit.
`cloud_gate_slots=9` sets the limit in `config/build_admission.conf`.
The service reads the limit through its configuration check before each pass.
The pass uses the checked values if the file changes during that pass.
The next check handles an unusable cloud setting like other unusable settings.

`cloud_progress.publish(sidecar, progress)` writes `<sidecar>.cloud.json` with the shared atomic writer.
`CloudProgress(state, size_vcpus, failed_modules, updated_at)` stores the cloud state.
`CloudState` names `requesting`, `setup`, `running`, and `uploading`.
`cloud_progress.read(sidecar)` returns `CloudProgress` or `NoCloudProgress`.
`NoCloudProgress.reason` distinguishes missing data from unreadable data.
The API supplies `progress_profile.publish_counter` for the route to write module counts.
The service removes the progress files when the reservation ends.

The queue view and card show cloud rows under `Cloud`, with used slots and total slots.
Each row shows the unit, step, state, module counts, vCPUs, and elapsed time.
Missing progress and module counts have explicit display states.
Waiting rows show their cloud slot wait.
`cloud_gate_stale_pushes=3` sets the update age threshold in `config/build_admission.conf`.
The queue reading carries the checked threshold to the display.
The display reads the push interval from `conf.read("cloud_gate").number("log_push_s")`.
An older update shows `no update for <age>`.
`CloudPushIntervalUnavailable` names an unavailable push interval.
The display omits the stale age when the push interval is unavailable.

The wire envelope uses protocol version 2 and keeps the existing admission code version rule.
The service accepts client versions 1 and 2.
The service sends a version 1 refusal when it cannot determine the request version.
Both client versions can decode that refusal.
Version 1 view and mine replies carry plain `QueueRead` records without cloud rows.
Version 2 replies can carry `CloudQueueRead` records.
The shared codec encodes the new place field and its frozen dataclasses.
Old requests default to local admission when they omit the place field.
`CloudSlotWait` carries the used slots and slot limit in wait replies.
`CloudQueueRead` extends the local queue reading with cloud rows and the slot limit.
Local queue readings keep their existing wire form when no cloud row exists.

Linux target steps set `BUILDLOG_CALLER=verify-linux`.
`memory_gate.sh` grants these steps without a local admission socket or a natedev hold check.
Target slots and the target build budget provide admission there.
AWS admission still requires the spending guard.
Other Linux callers keep the local admission and hold checks.

### Temporary build folders

`scripts.lib.build_folders.create_build_folder(request) -> Path` allocates temporary build folders.

1. `UniqueBuildFolder(prefix, parent)` creates a private folder atomically with mode `0700`.
2. `StableOwnerFolder(name, parent, mode)` retains the owner's folder, contents, and existing permissions.

`FOLDER_PREFIXES` defines each build kind's prefix.
Dynamic prefixes use an `owner` field for the process, module, review, version, or cache key.
`MKDTEMP_SUFFIX_PATTERN` defines the allocator's eight-character suffix rule for cleanup readers.
The sweep also reads old six-character shell suffixes until 2026-12-03.

`InheritedTemporaryParent` reads `TMPDIR` at each allocation.
`inherited_temporary_parent()` exposes that same rule to callers that retain their own folder creation.
`ExplicitBuildParent` retains an explicit parent.
`SystemTemporaryParent` uses the short system parent for socket and index folders.
`UnitRunParent` reads the current root from `unit_runs.run_roots()`.
The merge gate checks `/dev/shm` before allocation and uses an explicit parent without a disk fallback.

Shell callers use these forms:

1. `unique --kind KIND [--owner TEXT] [--parent PATH | --system-temp]`
2. `unique --prefix TEXT [--parent PATH | --system-temp]`
3. `stable --name NAME (--parent PATH | --unit-root)`
4. `stable --path PATH`
5. `stable --run-temp RUN`
6. `run-temp-path RUN`

The CLI prints one path on success.
Shell callers request allocation only when a stable folder is missing.
`verify.sh` first sources `invoke.sh --prepare-temp` to create a missing inherited `TMPDIR` before environment loading.
This mode uses the same allocation block as a normal `invoke.sh` call.
`unit_runs.RUN_TEMP_NAME` defines the run temporary folder name.
`unit_runs.activate()` writes the current marker and removes the former marker.

`temporary_build_folder()` yields a `Path` and uses the standard library's cleanup and permission repair.
Other callers retain their cleanup traps, owner records, task records, and release order.
The three shell verify allocations retain the CLI cost required by the Work Order.
The promotion check sent over ssh uses a self-contained standard-library temporary folder.
It imports no repository modules before the Mac fetch.

The director removed the unused task and scratch-parent wrappers during repair round 1.
This decision departs from Spec items 6 and 8.
`scratch.new()` and `scratch.target()` retain task registration and task naming.
`copy_record.root()` and `copy_record.copies_root()` retain the scratch and copies root rules.
The sweep reads those authorities and `unit_runs.run_roots()`.
An explicit private sweep root selects its own `copies` folder instead of the live copies root.

Stable session folders retain their short paths and mode `0700`.
The session hook defines the session temporary root.
The admission fixture uses `short_socket_directory(prefix)` for its separate short socket parent.
The service keeps `admission/state.json` and `admission/queue.json` outside temporary build folders.
`step_estimator.is_fresh_test()` and the disk sweep read `tmp_sweep.state_dir()` at use.
`tmp_sweep.TARGET_DELETES_NAME` defines the shared deletion record name.

### Machine side (`/etc/nixos`, natedev only)

Not in this repo; listed because the code here reads it.

| Piece | Setting |
| --- | --- |
| `builds.slice` (user slice, `modules/linux/development.nix`) | `MemoryMax=44G`, no `MemoryHigh`, `MemorySwapMax=4G`, `CPUWeight=20` against `app.slice`'s 100, so the sessions and their panes get the CPU first. CI's `hana.slice` sits at 5 against `user.slice`'s 100 (`modules/linux/hana-runners.nix`). Holds every recorded step's scope (`run-*.scope`), the `sccache.service` user service, and the `buildlog` unit (nightly Rust trial, `OOMScoreAdjust=200`). |
| `sccache.service` | Runs the server in the foreground with `SCCACHE_NO_DAEMON=1` and `SCCACHE_START_SERVER=1` on `ExecStart` only; takes the port from an already running server on start; `RestartSec=5`. Every compile handed to sccache therefore runs inside `builds.slice`. `OOMScoreAdjust=500`, so its compiles rank with the build steps. |
| `hana-ci.slice` | `MemoryMax=18G` only, `MemorySwapMax=2G`, no `MemoryHigh`. Runners `OOMPolicy=continue`. |
| CI slot pool | `/dev/steve-ci`, 14 slots, separate from the sessions' `/dev/steve` (31 slots). Runners: `CARGO_MAKEFLAGS=--jobserver-auth=fifo:/dev/steve-ci`, `BindPaths=-/dev/steve-ci`, `DeviceAllow` `char-rtc r` and `/dev/steve-ci rw`, ordered after `steve-ci.service`. No `CARGO_BUILD_JOBS`. |
| Build and CI kill order | Build steps and sccache at 500: each step's `builds.slice` scope sets `oom_score_adj=500` before starting the step, and `sccache.service` has `OOMScoreAdjust=500`. Session processes at 200 (rust-analyzer, builds outside `invoke.sh`). CI jobs at 100: `PIPELINE_JOB_OOMSCOREADJ=100` in the runners' `extraEnvironment`. earlyoom kills build steps first, then sessions' processes, then CI, the largest first within each level; the processes `--avoid` names go last. |
| Swap and earlyoom (`modules/linux/memory.nix`) | zram (zstd, 50%, priority 100), `vm.swappiness=100`. earlyoom decides on available RAM alone (SIGTERM at 5%, free-swap thresholds 100). Its score is roughly usage‰ of RAM plus swap, plus oom_score_adj, so the kill order comes from oom_score_adj. No `--prefer`; `--avoid` takes 300 from remote access, the desktop and the sessions themselves (`sshd`, `tailscaled`, kwin, plasmashell, `claude`, the tmux server and `herdr` among them). |
| Timers | `buildlog sample` every 60 s, `buildlog disk` every 600 s, `buildlog hourly` every 3600 s, disk-floor every 2 min (`sweep.py --floor-only`). |

The two ceilings (44G + 18G) deliberately exceed physical RAM together; decisions rest on kills and stall, not their sum.

On 2026-10-06 06:54 PDT, two hung hana tests in one local `verify.sh test` step held 11.4 GB and 15.5 GB; earlyoom first sent SIGTERM to 26 other processes, including six CI compilers (four hana CI jobs failed), and killed the tests last. `builds.slice` peaked at 37.7 GiB, under its 44G, because zram (10 GiB), sessions (8 GiB) and CI (3.4 GiB) had already taken the machine to earlyoom's 5% line.

### The verify run place

The outer Linux `verify.sh` sets `BUILDLOG_RUN` after detachment.
The value contains its PID and the start-time string from `/proc/<pid>/stat`, separated by `:`.
Nested calls retain that value.
The Mac uses `NoRun` and retains claim admission without reading `/proc`.

`admission_client.py admit` inherits the exported `BUILDLOG_RUN` through `memory_gate.sh`.
The client parses the value once into `RunIdentity` or `NoRun`.
A missing or malformed value gives the step its own place.

The first step creates one `RunPlace` with its arrival time and its `Owner`.
The service sets `order_since` on snapshot row copies from the run place.
It uses the earlier of the run time and the step arrival time.
Durable waiter records retain only the step arrival time.
Each step retains its own `since` for reconnects, waiting age, token reacquisition, and AWS eligibility.
Each step also retains its own owner, episode, and reservation.
Queue priority remains the first ordering field.

The service retains the run's process watch between steps.
The run holds memory for a 10-second gap between steps. It holds no CPU token during that gap.
When the run ends, waiting steps use their own arrival times for order again.
The service persists run places with waiter places through the generic codec in `state.json`.
After a restart, the service restores live run places and removes ended run places.

### The admission service

`scripts/build_queue/admission_service.py` is the only live admitter. Its epoll loop owns the queue and reservations.
`admission_protocol.py` defines every request and reply. `admission_client.py` handles connections, retries, wait output, and bounded queries.
`step_estimator.py` owns the estimates. `admission_records.py` owns the shared records, configuration, and wait phrases.

The queue Step column and cards show the project before the step, such as `nateroids nextest`.
A step without a project name shows only the step.
An absolute repository path does not supply a project name.
Matching and history retain the original step name.
The queue omits a live unowned scope when its run has no reservation and keeps no memory.
A run with a memory hold shows through that hold.
The services block and the block for processes from ended builds remain visible.
The card selector still finds an unowned run by its owner.
The memory page keeps this memory in Other.
`memory_admit.py` owns the readers, pure admission rules, projection, and finalizer.

[Free memory, head protection, and step slots](#free-memory-head-protection-and-step-slots) defines the room, keep-asides, and cap.
The first two minutes use the same calculation. A run never counts its use twice.
The display shows zero room for a shortfall. The cap explanation shows the shortfall.
These readings never reduce a promise from history.

Waiting runs follow priority, first run arrival, and PID order.
A younger run starts only when its need fits after the first run's need remains protected.
The first run can start despite insufficient room when no admitted run or held slot remains.
Readable host memory, clear readable pressure, and a current estimate remain necessary.
New starts have five seconds of spacing through `recovery_start_seconds`.

An ended step retains its slot for `slot_gap_seconds`, currently ten seconds.
A continuation reuses that slot without start spacing, even under pressure.
A larger continuation requests only the extra memory before waiting runs.
That extra requires sufficient room and clear readable pressure.
The gap deadline or the run's process end releases an unused slot.
A closed connection ends its wait and releases its claim.

Every pass reads one `MemoryConfig` from `[memory]` in `config/build_admission.conf`.
`read_admission_configuration()` returns `UsableAdmissionConfiguration` or `UnusableAdmissionConfiguration`.
`read_memory_config()` resolves each key through `ConfigRead.origin()` once.
It collects recovery diagnostics and emits them only after a successful read.
Estimates receive the cargo peak and history factors from that configuration.
A recovered setting remains usable and produces one warning per process.
An unavailable setting stops grants and returns a refusal that names the file and key.
The service listens during a refusal and watches the nearest existing configuration directory.
`scripts.lib.inotify` reports directory replacement, permission repairs, completed writes, and overflow events.
Each relevant event re-arms the watch before a configuration read; no stamp filter discards repairs.
Watch registration tries each ancestor and tolerates filesystem errors.
When no ancestor accepts a watch, the service retries after the last usable source-check interval or the protocol cleanup interval.
Only configuration resolution decides usability; watch errors do not stop cleanup.
The service retains the last usable `MemoryConfig` for transport bounds, timers, and replay retention.
Cleanup requests have bounded strings and a protocol size limit, independent of the configured request limit.
Persistence reads no admission configuration.
Program places retain their acquisition configuration for reservation endings and scope cleanup.
Standalone cleanup uses the timing saved with its grant, or the fixed protocol timing when no saved timing exists.
The service removes its AWS queue snapshot during a configuration refusal.
The pass uses that policy for readings, decisions, and explanations. Its stored inputs preserve the evidence for counterfactual release advice.
Queries project the stored `PassResult` through `queue_read_from_pass`. A release marks the stored result stale; the next query computes its evidence once without granting a step.
`queue_read()` supplies one `QueueRead` for the complete queue page.
A failed query raises `AdmissionUnavailable` within the query bound. Commands print `build queue: admission service unavailable: <reason>` and exit 2.
`/build:queue <position|session|folder>` shows the full queue, one blank line, and the selected cards.
`mine` uses the same layout. The page resolves labels once and reads optional history once through `head_facts.read_head_facts()`.
`queue_evidence.py` formats the recorded need, run keep-aside, room calculation, head cause, and release advice.
The card uses the reply's policy and limits. It reads no current admission configuration to reinterpret the reply.
The evidence includes host RAM, optional load, run needs, swap-backed use, held slots, keep-asides, room, and start spacing.
It also includes estimate history, shape tiers, failed-peak floors, factors, recorded limits, package, filters, width, and completed CPU-token waits.
A missing optional record remains unknown. Queue arrival and time waiting use `joined_at`. Start spacing has its own deadline.
Admission and release calculations remain in `memory_admit.py`. An expired estimate requires a client refresh through the service.
The page prints priority and unowned memory once in the queue view. It prints shared evidence once below the selected cards.
`build_why.py` and its command are removed. There is no alias.
The pane keeps the last successful page after a failed refresh.
A failed first query shows `Build queue could not be read.`

| Request | Result |
| --- | --- |
| `admit` | Wait, refusal, or a durable grant with reservation id, `.max`, `.wave`, and sampling context |
| `launch` | Wait, refusal, or launch permission without a reservation |
| `release` | Finalize one reservation |
| `width` | Record the requested test width |
| `token_wait` | Finalize a reservation and preserve its queue place |
| `priority_set`, `priority_clear` | Change the one unit priority |
| `admit_head` | Waive existing keep-asides for the head when free memory permits |
| `view`, `explain`, `mine` | Read the stored pass |

`/build:queue bump <position|session|folder>` uses the card's selector resolver to select a waiting step.
It sets priority for that step's unit. An unmatched selector exits 3 without a priority change.
The service refuses `priority_set` when no waiting step or current reservation belongs to the unit.
The queue commands `bump`, `clear`, and `admit-head` work from any session or a plain shell.
`queue_command_actor()` selects `CLAUDE_CODE_SESSION_ID`, then `CODEX_THREAD_ID`, then `"user"`.
The pane passes its session id to mutating commands. Read-only commands keep their existing environment.

Mutation requests have replay ids. The service persists each terminal reply before delivery.
A replay returns the same reply. Live grants remain in the ledger until their reservation ends.
Ended replies expire under `replay_retention_seconds`. Wait replies remain only on their connection.
`Wait` carries the decision, readings, need, policy, and `ConfigurationDiagnostic` records from the same pass.
Client commands print each recovery warning once per process.
Unavailable settings produce refusals rather than replacement policy values.

### Durable state and lifetime

`admission/state.json` holds reservations, waiter places, episodes, queue priority, head override, start spacing, and the replay ledger.
Held slots and waiting extras remain in memory. A restart removes them and retains the run's queue place.
The service replaces this file atomically. Reservation records retain accepted needs, run identities, scope paths, and finalization intent.
Width and call absence use named variants. `RequestedTestWidth`, `UnspecifiedTestWidth`, and `NoTestWidth` describe the width.
`CallRecord` and `NoCallRecord` describe the call.

`reservation_stem` gives each grant its `.max` and `.wave` paths. `invoke.sh` uses the grant's paths and sampling context.
The sampler reads no reservation file. It marks the nextest test phase through `record.py test-phase-context`.
The service watches owner pidfds and scope events. A populated scope retains its accepted need after its owner ends.
An empty scope and ended owner permit finalization. A reused scope path gets a new watch.

`finalize_reservation` is the one finalizer. `StateIntents.save` persists its evidence before the history append.
The finalizer matches `reservation_id` to prevent a duplicate history row after a crash.
`StateIntents.clear` removes the reservation after the append. Startup completes any saved intent.
The finalizer records completed measurements for history. Release creates the run slot without changing the accepted need.

At the first start without `state.json`, the service imports the old admission files through their existing readers.
It imports live reservations, saved intents, waiter places, token handoffs, priority, head override, and start spacing.
It writes state before it deletes imported files. Later starts import nothing.
The old `.waiting`, `.reservation`, `.token-wait`, and `queue_priority.json` forms have no live writer.
There is no admission lock.
`build_hold` imports `admission_service` inside `admitted_processes()` when it reads durable grants.
Importing `build_hold` or `build_targets` does not load the admission service.
`ServiceState` and `read_state` retain one definition in `admission_service.py` because the state uses several service types.
The service imports `argparse` inside `main()` and the reading alert message helpers inside `deliver_reading_alert()`.
After each pass, the service publishes `admission/queue.json` atomically through `scripts/build_queue/queue_snapshot.py`.
The snapshot contains the pass readings, its policy, and the remaining waiters.
The AWS controller reads this snapshot without a live queue request.
It refuses a start when the snapshot is missing, stale, or incomplete.
Queue views continue to use the service.

### Queue lifecycle and calibration

The service records queue events in `admission/queue_times.jsonl` through `append_queue_time`.
The same record holds Cargo, `targeted-test`, and `merge-gate` steps.
The append function locks the file and removes incomplete rows before an append.
At 8 MiB, it retains the newest events from the last 14 days in less than 6 MiB.

1. `admitted` records the original arrival, admission time, wait seconds, need, need source, and whether the step is first.
2. `left` records a cancelled waiter or a waiter whose owner is gone.
3. `ended` records the reservation's end time, outcome, end kind, and sampled peak state.

Each event identifies the logical step by `pid`, `start_time`, and the original `since`.
Each admission and end also identifies its reservation interval by `reservation_id`.
A token wait ends an interval with `TokenReturn`. It does not complete the step.
A normal end uses `StepEnd`. An exit before a positive sample records `NoPositiveSample`.
The saved finalization intent retains the facts for the end event across retries.
The event identity prevents a retry from publishing the same end twice.
The service saves pending admission and cancellation facts with each durable state transition.
One lifecycle outbox publishes these facts after the state write and during restart.
Reservation removal and removal of its non-token place use the same state write.

`read_queue_lifetimes` joins these events into `QueueStepLifetime` records.
Each record retains its reservation intervals, token gaps, and final end state.
Memory wait includes the first wait and each wait after a token returns.
It excludes the token gaps and running time.
A missing return time gives `MemoryWaitIncomplete`; an unfinished step gives `StepStillOpen`.
The Phase 16 comparison is due 24 hours after these lifecycle records reach main.
It compares Cargo waits and pressure holds with and without overlapping gate intervals.

`head_policy` resolves the deployed head rule and ordered walk from one frozen snapshot.
Admission, queue readers, blocker searches, and release searches use that result.
The head keeps its accepted need while it waits.
Younger runs can start only if they leave room for that need.
The rule applies with and without queue priority.

The Phase 20 calibration has insufficient evidence.
The calibration requires seven days under the deployed Phase 39 configuration.
The phase review found no eligible days or cohort budget after that deployment boundary.
Recorded wait minutes, replayed wait minutes, and their ratio are unavailable.
The main reference has a 24 GiB build budget; this is not a calibration cohort.
The comparative replay does not run. The capacity rule is not adopted.

### Estimates and keep-asides

The client imports `step_estimator` inside `step_request`. It calls `step_estimate` before admission and after `EstimateExpired`.
`system_clock.py` owns the shared `SystemClock`. Cleanup commands load no service or queue view.
Before each new build estimate, it reads `build_history` from the service, then reads history.
`StepProfile.build_history_generation` keeps that generation through reconnects and delayed replies.
The service grants a build only when its estimate generation equals `ServiceState.build_history_generation`.
A missing generation or a different generation gives `EstimateExpired`.
A matching pending finalization also gives `EstimateExpired` until history publication finishes.
`StateIntents.published` advances the durable generation after the history append and before intent removal.
A publication retry also advances the generation. The service never prunes or resets the generation.
A refresh uses the open admission connection. An unregistered client uses a read-only `build_history` query.
A pass retries a retained finalization intent even while its owner remains alive.
It preserves the first `since` and episode across refreshes and reconnects.

With `--wait-events`, the client emits `first`, then `holder <json>`.
It emits another holder event only when the holder changes.
The JSON identifies a session (`kind`, `session_id`, `session_name`), a unit (`kind`, `unit`), or no holder (`kind=none`).
The pass records the holder in `AdmissionWaitSnapshot.holder`.
An order hold names its protected waiter, including a continuation that requests extra memory.
A room wait names the run with the largest recorded charge, including held slots.
A held slot keeps the run's recorded unit after its reservation ends.
The charge uses grouped swap-backed use and the current keep-aside.
Unavailable run use retains the full keep-aside charge.
Unregistered CI jobs never become holders.
Pressure, unreadable host readings, start spacing, and estimate refresh report no holder.
The holder defaults to `NoHolder` when an older service omits the field.
An older client ignores the added field and keeps its wait.
At a service restart, the client reconnects with the same episode and arrival time.
It prints `admission service restarted; waiting again` once when the connection closes.
The existing unreachable limit still applies until a valid reply arrives.
`QueueInputs` and the queue snapshot version remain unchanged.

A fresh nextest estimate expires after one running-pass interval. Other history estimates expire after `estimate_refresh_seconds`.
A program uses its history estimate and refreshes it under the normal estimate policy.
The merge gate reads `program_first_peak_gib.merge-gate` at use; its first need is 4 GiB.
Legacy gate peak records do not size that need. A matching anonymous progress profile supplies the first pass's look-ahead window.
The gate removes its progress counter before reruns, which keep the full need.
The merge-gate progress profile key includes `shape` (`tests` or `types`).
Earlier profiles do not match, and nightly runs use separate profiles.
It reads `merge_gate_governor_gib` when the gate starts; the initial value is 4 GiB.
The governor bounds module starts when a CPU slot is free; with no job running, it always permits one job.
Memory peaks do not kill jobs or fail the gate.
The gate reports its first need as a fallback and explains any matching anonymous progress window.
The queue model reads the step names once from `config/build_steps.conf`.
Renderers use its `StepNames` value without file reads.
Other sentences read the step names when they need the text.
Queue tables and cards use the prose name, including `targeted test` for `targeted-test`.
`step_promise` gives every admitted step a `LifetimePromise` until it ends.
The run calculation uses the accepted need before tests start.
For nextest, an observed test phase can select the frozen test-phase need instead.

Other programs retain `HistoryPeakSizing` and `program_peak_estimate`.
Cargo estimates separate compile history from no-compile history.
`index_step_evidence` joins each anonymous peak by host, call id, and step.
`StepEvidence` keeps its shape and compile count independent.
For a test step (`TEST_STEPS`: `nextest`, `test`, `t`), it also keeps the decoded argv as `arguments`.
`history_selection` gives each anonymous row its package, scope and filter set.
A recorded field wins. Only a missing field is parsed from `arguments`, once, through `test_selection`.
A row with neither keeps the recorder defaults: no package, `full`, no filters.
Missing argv does not remove a compile count. A saved shape does not prove compilation.
`CompiledCrates` identifies a compile run. `NoCratesCompiled` identifies known zero-count attempts.
The join combines repeated attempts. Any positive count keeps the call in compile history.
An unknown count or a missing join keeps the peak usable until the index supplies a known count.
The peak's completion time controls its history window, including failed-build floors.
Merge-gate peaks keep their separate class and remain in the anonymous history.
The gate uses its own cap, so Cargo estimates do not use those peaks.

Five keyed rows exclude the older unkeyed rows for the same repository.
With one to four own rows, a non-build estimate uses their largest peak or a lower pool p90.
A repository without own history keeps the pool and fallback order.
A full test estimate with a package keeps only rows whose package and scope match.
Other steps, and requests with no package, keep the recorded scope rule.
A filtered compile estimate (`filtered_history_peak`) takes the larger of two anonymous p90 values:
the exact filter set's rows and the package's filtered rows.
Indexed peaks times `ANON_SHARE` come next, exact filter first. Check or build compile peaks come last.
A full-suite estimate does not cap a filtered compile estimate.
The 1 GiB filtered floor applies when the repository has no usable compile history.
Fresh nextest estimates retain `fresh_test_peak` and its 1 GiB floor.
Freshness still requires both Cargo output roots to survive.

The Phase 18 replay found no gain, so no estimate splits its history by build feature or commit.

#### Phase 19 source shares

Run date: 2026-11-05.
The snapshot ends at 2026-11-05 13:07:40 UTC.
The requested window starts at 2026-10-29 13:07:40 UTC.
The lifecycle file retains 571 events across 4.92 days, including older token-gap records.
The 220 admissions cover 2026-11-04 20:45:52 through 2026-11-05 13:03:36 UTC: 16.30 hours.
The anonymous file retains 46,262 rows from 2026-10-07 through 2026-11-05.
The requested window contains 5,205 anonymous rows.
Replay reconstructs 4,267 estimates and keeps 815 merge-gate cap rows separate.
Another 123 rows lack their own dated index match.
Recorded intervals and reconstructed estimates are separate populations. Do not add their counts.

| Population | Step kind | measured | pooled | buildlog | fallback | shape | cap |
|---|---|---:|---:|---:|---:|---:|---:|
| before Phase 60 | full nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | clippy | 6 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | check | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | build | 0 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | doc | 2 | 0 | 0 | 0 | 0 | 0 |
| before Phase 60 | other | 6 | 0 | 0 | 0 | 0 | 9 |
| before Phase 60 | **Total** | 14 | 0 | 0 | 0 | 0 | 9 |
| after Phase 60 | full nextest | 6 | 0 | 0 | 1 | 0 | 0 |
| after Phase 60 | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | clippy | 20 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | check | 0 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | build | 0 | 0 | 0 | 0 | 2 | 0 |
| after Phase 60 | doc | 4 | 0 | 0 | 0 | 0 | 0 |
| after Phase 60 | other | 19 | 0 | 0 | 0 | 0 | 145 |
| after Phase 60 | **Total** | 49 | 0 | 0 | 1 | 2 | 145 |
| before queue 10z | full nextest | 6 | 0 | 0 | 1 | 0 | 0 |
| before queue 10z | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | clippy | 26 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | check | 0 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | build | 0 | 0 | 0 | 0 | 2 | 0 |
| before queue 10z | doc | 6 | 0 | 0 | 0 | 0 | 0 |
| before queue 10z | other | 25 | 0 | 0 | 0 | 0 | 86 |
| before queue 10z | **Total** | 63 | 0 | 0 | 1 | 2 | 86 |
| after queue 10z | full nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | filtered nextest | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | clippy | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | check | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | build | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | doc | 0 | 0 | 0 | 0 | 0 | 0 |
| after queue 10z | other | 0 | 0 | 0 | 0 | 0 | 68 |
| after queue 10z | **Total** | 0 | 0 | 0 | 0 | 0 | 68 |
| Replay | full nextest | 655 | 23 | 0 | 3 | 0 | 0 |
| Replay | filtered nextest | 475 | 0 | 10 | 49 | 0 | 0 |
| Replay | clippy | 1429 | 20 | 0 | 0 | 0 | 0 |
| Replay | check | 426 | 13 | 0 | 0 | 0 | 0 |
| Replay | build | 0 | 0 | 0 | 0 | 149 | 0 |
| Replay | doc | 215 | 7 | 0 | 0 | 0 | 0 |
| Replay | other | 765 | 28 | 0 | 0 | 0 | 815 |
| Replay | **Total** | 3965 | 91 | 10 | 52 | 149 | 815 |

The Phase 60 boundary is its first origin/main ancestor at 2026-11-04 21:52:05 UTC.
The queue 10z boundary is its first origin/main ancestor at 2026-11-05 07:24:57 UTC.
Git merge times delimit cohorts. The records contain no process version, so these boundaries do not prove deployment times.

| Fresh tier | Full nextest | Filtered nextest |
|---|---:|---:|
| measured | 162 | 49 |
| buildlog | 0 | 0 |
| NoFreshHistory | 14 | 7 |

The fresh table contains replay subsets, not extra estimates. Recorded events contain no fresh-tier field.
A zero indexed compile count selects a fresh scenario; the records cannot prove that both output roots survived.
The 21 NoFreshHistory cases take the compile estimate. Fresh buildlog includes the fresh floor.

No recorded admission uses ANON_SHARE: 0 of 220, and 0 of 66 without caps.
Reconstructed ANON_SHARE share: 10/5,082 (0.20%); without caps: 10/4,267 (0.23%).
The 10 ANON_SHARE promises have 0 under-half cases and 1 over-three cases against their measured anonymous peaks.

| Replay step kind | ANON_SHARE share | Share without caps |
|---|---:|---:|
| full nextest | 0/681 (0.00%) | 0/681 (0.00%) |
| filtered nextest | 10/534 (1.87%) | 10/534 (1.87%) |
| clippy | 0/1,449 (0.00%) | 0/1,449 (0.00%) |
| check | 0/439 (0.00%) | 0/439 (0.00%) |
| build | 0/149 (0.00%) | 0/149 (0.00%) |
| doc | 0/222 (0.00%) | 0/222 (0.00%) |
| other | 0/1,608 (0.00%) | 0/793 (0.00%) |

Request selection evidence: 11 recovered legacy test rows and 1,204 rows with recorded fields.
Available fourteen-day test history: 3,655 recovered legacy rows, 1,250 recorded rows, and 39 rows with unknown selection.
All 149 replayed builds have shape evidence. ShapeFallback supplies 31 promises; these remain shape sources, not ordinary fallback sources.
Observed waits: 225 logical steps have recorded totals of 1,162.98 minutes.
3 steps have incomplete waits and contribute no value.
4 intervals remain open. No ended recorded interval has NoPositiveSample.
Phase 18 and Phase 68 are absent from origin/main at this snapshot, so their deployed cohorts are unavailable.
No evidence reaches 2026-11-12, when the legacy rows leave the window. An after-expiry share is unavailable.
The current estimator and configuration reconstruct earlier requests using only evidence available before each request.
The replay assigns each indexed attempt to at most one sample, including samples before the requested window.
Eight samples without their own indexed attempt are unresolved. The replay excludes them instead of using another attempt’s request time.
The earlier repairs corrected retry times and used the production repository identity rule.
Queue 10s changes no estimator function or configuration value in this comparison.
The showrunner stopped this phase at Spec 2 because the overall ANON_SHARE share is under 5%.
This measurement changes no estimator tier.

#### Phase 11 replay comparison

The replay covers 2026-10-21 through 2026-11-04 with the same frozen inputs.
It evaluates 15,053 runs and omits 195 runs without an index match.
Another 617 merge-gate rows retain their separate cap class.
Each estimate uses its historical budget: 21 GiB or 24 GiB.
Under-half means below half the measured peak. Over-three means above three times the measured peak.

| Compile class | Budget GiB | Runs | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|---:|---:|
| compile | 21 | 11,360 | 458 | 402 | 1,162 | 1,152 |
| compile | 24 | 2,300 | 18 | 12 | 332 | 358 |
| no compile | 21 | 1,185 | 12 | 12 | 937 | 937 |
| no compile | 24 | 208 | 3 | 4 | 174 | 173 |
| **Total** | | **15,053** | **491** | **430** | **2,605** | **2,620** |

These repositories have a higher count. Each row combines their steps, compile classes, and budgets.

| Repository | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|
| cargo-liner | 27 | 8 | 1,754 | 1,804 |
| check-only | 1 | 2 | 5 | 3 |
| clone | 1 | 2 | 15 | 14 |
| clone85 | 1 | 2 | 0 | 0 |
| fix | 3 | 3 | 20 | 22 |
| hana | 71 | 31 | 592 | 595 |
| lintproof_fix | 0 | 1 | 6 | 1 |
| p31-gates-834aebb04-8015c7caf3 | 1 | 2 | 3 | 1 |
| src | 0 | 2 | 46 | 40 |
| work | 1 | 2 | 0 | 0 |

Bar missed: under-half falls by 61, but over-three rises by 15.
At least one count rises in 10 repositories and 19 repository, step, and compile-class groups.
The largest rise is cargo-liner nextest compile runs at 21 GiB: under-half 27 to 7, over-three 101 to 166.
A filtered compile run takes its estimate from compile history, and the full-run estimate no longer caps it.
The showrunner accepted the miss (2026-11-04): an under-promise risks an out-of-memory kill; an over-promise only delays parallel starts.
Phase 60 found the promise source of these over-three runs and fixed it there; see the Phase 60 replay below.

#### Phase 60 replay comparison

The Phase 60 replay (`phases/60/replay.py`) rebuilds the Phase 11 replay with the same frozen inputs.
It classifies each call through `index_step_evidence` and `combine_compile_counts`. The baseline is `82f5c90d4`, the Phase 11 checkpoint.
The shared classification moves 51 runs from no compile to compile, so its class totals differ from the Phase 11 table.

The cause is in the selection of history rows, not in the tier order:
1. Anonymous rows recorded before 2026-10-29 have no package, scope or filter fields.
   `filtered_peak` rejected them, so 90 high promises came from indexed peaks times `ANON_SHARE`.
   Indexed peaks count memory outside the anonymous peak, so those promises were too high.
2. A full test estimate with a package mixed every package and older filtered rows: 79 high promises.
3. Three high promises came from large earlier samples. The fix keeps them.

The fix recovers the missing fields from the indexed argv, selects full test rows by package and scope,
and takes the larger of the exact-filter and package p90 before any indexed tier.

| Compile class | Budget GiB | Runs | Under-half before | Under-half after | Over-three before | Over-three after |
|---|---:|---:|---:|---:|---:|---:|
| compile | 21 | 11,411 | 402 | 367 | 1,171 | 1,071 |
| compile | 24 | 2,300 | 12 | 12 | 358 | 361 |
| no compile | 21 | 1,134 | 12 | 12 | 918 | 920 |
| no compile | 24 | 208 | 4 | 4 | 173 | 173 |
| **Total** | | **15,053** | **430** | **395** | **2,620** | **2,525** |

Bar met: cargo-liner nextest compile runs at 21 GiB go from under-half 7 to 0 and over-three 166 to 65 (bar: 101 or lower, under-half 7 or lower).
At 24 GiB, the same group goes from over-three 6 to 2.
Hana over-three rises 595 to 607 while its under-half falls 31 to 23. Six other repositories rise by one or two runs in one count.

#### Phase 17 compile-band measurement (not adopted)

Phase 17 replayed nextest promises split by compile count (bands 1–5, 6–20, 21–50 and more than 50 crates) against checkpoint `e88fb841d`.
The band chosen from the last compile count of the same worktree and step was right for 54% of compile requests.
Promises under half the peak rose from 395 to 423, and ten repositories got worse. Promises over three times the peak fell from 2,525 to 2,485.
The adopt rule needs fewer under-half promises in total and no rise in any repository, so bands are not adopted and the estimate is unchanged.

Build estimates use completed same-shape history from the last 14 days.
Ten or more earlier runs supply p90. One to nine earlier runs supply their largest peak.
The newest failed-build floor can raise that base.
Each new estimate reads `[memory]` in `config/build_admission.conf` through `memory_config()`.
A dev build uses `ceil(base × dev_size_factor + margin_bytes)`.
`dev_size_factor` is 1.1. `dev_size_margin_gib` is 1.1 GiB, rounded upward to bytes.
The Phase 39 replay uses the original 126-build cohort and the 64 measured builds from the latest three days.
The cargo-berth increase sets the margin. The measured S2 build sets the factor.
The S2 history peak of 6.3 GiB gives a promise of 8.03 GiB without truncation.
Release builds use `release_headroom`, currently 1.314. A build without same-shape history retains the same-profile fallback without headroom.
The retry after a memory kill stays unchanged.

Each estimate stores its history rule, factors, and failed-peak floor.
`DevSizeHeadroom` stores its factor and margin.
`LegacyDevMultiplierHeadroom` reads older saved multipliers through the existing `dev` wire tag.
New estimates remain uncut. Historical `CappedAtBudget` and `CapNotRecorded` values remain readable.

Each pass reads the resident working set and swap for service reports.
CI RAM and unowned scopes reduce host free memory. They do not create a run promise.
`ci_memory_slice` names the CI slice. A missing CI reading remains a labelled diagnostic.
`ci_memory_margin_gib=7` supplies one CI keep-aside while `job_state` is `RunningServiceJob`.
The service reads that value once with the other policy values.
`AdmissionPolicy.ci_memory_margin_bytes` stores its byte value.
`MemoryRoom.ci_keep_aside` stores the separate `CIKeepAside(bytes)` evidence.
`MemoryRoom.ci_use` stores readable CI use separately from admitted swap-backed use.
`MemoryRoom.kept` includes run keep-asides and this CI keep-aside once.
Idle CI and unavailable CI readings add no CI keep-aside.
CI RAM already reduces host free memory. The room calculation never subtracts it again.
The service does not require `memory.max` for admission.
[Free memory, head protection, and step slots](#free-memory-head-protection-and-step-slots) defines the cap.
The cap text names the CI keep-aside within kept memory.
The page shows CI use and the CI keep-aside inside the cap.
The page and the queue text show CI use under 0.1 GiB in MiB, so idle CI never reads 0.0 GiB.
The page includes both CI amounts in its taken total. Room and admission do not change.
`RunningServiceJob` and `IdleService` retain the CI job state.
`AnonymousRunUse` stores resident swap-backed memory: anonymous memory plus shared memory, which includes tmpfs files.
Scope readings use `memory.stat` `anon + shmem`.
Process readings use `/proc/<pid>/status` `RssAnon + RssShmem`.
`ProcessAnonymousUse` retains each process's contribution. Both keep their names because the snapshot and protocol codecs use the class name as the record kind.
The resident working set stays `max(0, memory.current - inactive_file)` and already includes shmem.
The reader does not add shmem to that working set again. It records swap separately.
Missing, invalid, or negative counters return `RunUseUnavailable`.
The shell peak sampler uses the same `anon + shmem` basis for step and test-phase peaks.
The queue-place sampler uses this basis for scoped programs and unscoped runner programs.
Its other unscoped program peaks retain their resident-memory basis.
CI subtraction removes registered scope use on that same basis, including shmem.
Nested registered scopes enter the subtraction once. Process subtraction excludes members already covered by those scopes.
An unavailable reading keeps the full accepted need aside and makes the cap unavailable.
The service freezes `TestPhaseNeed` and its margin when it writes a nextest reservation need.
A width update selects history again at the recorded width.
`test_phase_need` uses the existing history selector through `store.locked_rows`.
The selector uses the p90 test-phase peak from matching repository, scope, package, filters, and width history.
`test_phase_margin=1.2` has range `1..`. Missing or invalid values use `TEST_PHASE_MARGIN_FALLBACK`, which is 1.2.
The config reader resolves the margin once. Each reservation retains its selected margin.
`record.read_test_phase` supplies the observed test window and test peak.
After tests start, need equals `max(test peak so far, stored test-phase need) × stored margin`.
Without a stored need or observed test window, need remains the full step estimate.
The existing keep-aside formula remains `max(0, need − swap-backed use)`.
For a 12 GiB step, 3 GiB test use, and 3 GiB history need, margin 1.2 gives 3.6 GiB need.
That run keeps 0.6 GiB aside instead of 9 GiB.
The queue view, card, build-why, and memory page use the keep-aside captured by the last pass.
Build-why explains the selection: `tests started: need X (test peak Y, history N x 1.2)`.
No test-phase estimate worker remains.

### Clients and program places

`memory_gate.sh` starts one foreground client for an admission wait. The client writes quoted grant assignments for the shell.
Its stdout remains one protocol grant. Release, width, and token handoff use client commands.
The Mac branch retains claim admission without a socket or `/proc` read.
BRP launches use `launch`; launch permission creates no reservation.

Transport failures never permit a start and never select another admitter.
The client reconnects with its first request identity. It reports unavailable transport periodically and waits.
A valid wait resets checker-failure time. Checker failures after a wait retry until `BUILDLOG_MEM_CHECK_RETRY_S`.
A first checker failure or explicit refusal stops at once. A launch deadline applies during transport and checker retries.
A zero launch limit refuses its first hold and accepts an immediate grant.
The leave hook runs during held waits and returns `LeftQueue` when another machine can start the step.

`mesh_runs.py start` checks for a live twin before it applies `config/mesh_runs.conf` `LIVE_RUN_LIMIT` to the worker.
Orphan reaping uses the same runs-folder lock as start, including the `reap` verb and launcher reaping.
The reaper cannot finish a record while start publishes its supervisor PID.
Only running records with their matching supervisor hold a place.
An exclusive `<runs>/.start.lock` covers the checks, record creation, supervisor creation, and PID publication.
At the limit, exit 4 starts nothing and names each live run and label on stderr.
Wait for a run's result as the next message, or put more files in one run.
Exit 3 still means the caller is not a mesh worker; only that exit permits attached execution.

`queue_place` uses the client in process. Its public call retains `before_check`.
`release()` and `token_wait()` take explicit cleanup timing or the place's acquisition configuration.
`Admitted.cleanup_timing` supplies `CleanupTiming`; the shell retains it in `BUILDLOG_MEM_CLEANUP_TIMING`.
Stored `Admitted` replies without cleanup timing receive `PROTOCOL_CLEANUP_TIMING` before strict parsing.
Existing stored timing and valid shell timing retain their values.
The hook runs before each attempt and after a grant. An exception after a grant returns the reservation before program startup.
`popen` starts the program in its own scope, nested when the holder already runs in `builds.slice`.
Without a scope, it raises `QueuePlaceRefused` before the fork.
Only `BUILDLOG_SCOPE=0` enables process-tree sampling for tests.
`finish()` rereads `cgroup.procs` until empty before release. It sends TERM, then uses `cgroup.kill` after `program_stop_seconds`.
The scoped wrapper requires a published sidecar and a live holder before program execution.

### Startup, cut-over, and rollback

The system socket starts the system service as the user. The service runs outside `builds.slice` with `OOMScoreAdjust=-900`.
It sends `READY=1` after listener setup.
The main loop clears the outage claim once after `silence_seconds` of continuous operation.
It uses the last usable configuration and retries a failed clear on a later loop.
A shorter service start preserves the claim.
Source changes stop the service after the settle interval. A missing source also counts as a change.
A restored head override receives reconnect grace only after the listener is ready.
`down-alert` waits `silence_seconds` and claims `build-admission`.
It sends the outage message to the `builds` session first.
The message names the journal command and the restart command.
If the result is not `sent`, it sends an `alert.blocked` phone push.
The phone action is `sudo systemctl restart build-admission.socket build-admission.service` on natedev.
The push clears when `admission_client.py view`, through `scripts/lib/py`, succeeds.
The sender releases the claim only when both sends return `failed`, `refused`, or `retry`.
Every other outcome preserves the delivered claim.
The sender prints each send outcome header to the journal.

The builds showrunner owns cut-over. Workers do not change live units or admission state.

1. Stop and disable the socket and service before the prerequisite rebuild.
2. Remove the old `/var/lib/build-admission` state after its service drain succeeds.
3. Hold natedev and require quiet builds plus `admission_service.py drain-check` reporting `drained`.
4. Merge the cut-over alone.
5. Enable only the socket and rebuild the system configuration.
6. Check the service command, live view, cgroup, and OOM score.
7. Release the hold.

`drain-check` counts live imported waiters, reservations, and processes that retain old admission functions.
`drain-check --service` counts live service waiters, reservations, and clients through the same process list.
Both commands only read state. A nonempty count refuses the drain.

1. Hold natedev before rollback.
2. Require quiet builds and `drain-check --service` reporting `drained`.
3. Stop and disable both units before the rollback rebuild.
4. Run `export-priority` to preserve the active priority and remove the drained `admission/state.json`.
5. Revert the cut-over only after the export succeeds.
6. Release the hold.

A bare revert can hide live service charges from the restored checker. Never revert before the drain.
An export retry preserves the exported priority when `state.json` is absent.
The next cut-over imports the polling checker's current priority because the retired state file is absent.

### Cargo wrapper (natedev only)

`scripts/build_hold/cargo` puts building cargo commands from shells, scripts and TUIs in a `builds.slice` scope. This includes `cargo mend`, which builds the workspace. It finds the first other cargo on PATH, keeps its arguments and exit status, and passes non-building commands such as `metadata`, `fmt` and `tree` through at once. A call already in `builds.slice` skips the hold check and a second scope. Other building calls check `hold_applies.py` when the hold directory has an entry: an applicable hold refuses the command; unreadable state prints a note and allows it. An empty hold directory starts no Python. Without a user bus or `systemd-run`, cargo runs directly.

Install on natedev from the repo root with `scripts/build_hold/install_cargo_wrapper.sh`. It links `~/.local/bin/cargo` to the wrapper and refuses to replace another file. `~/.local/bin` must precede `~/.cargo/bin` on PATH. On macOS, the installer makes no link and the wrapper calls cargo directly. Tests use `BUILD_HOLD_DIR`, `CARGO_WRAPPER_CGROUP_FILE` and `XDG_RUNTIME_DIR` to select temporary state.

### BRP launches

`scripts/hooks/pre-tool-use-brp-launch-gate.sh` is a `PreToolUse` hook on `mcp__brp__brp_launch` (`settings.json`, timeout 960 s). It sets `CLAUDE_CODE_SESSION_ID` from the hook input, clears the call id and asks `build_targets.py check-local` first. A launch from the session whose hold file is the only hold in force is admitted. A hold or unreadable hold state prints a deny answer with its reason and exits 0 without memory admission; the session launches again after release. An allowed launch runs the memory gate. A launch's build runs in the session's own scope, not `builds.slice`. A launch after release enters the memory gate without another release message.

### Memory kills in `verify.sh`

`run()` wraps `run_once`. A failed step is a memory kill when its output shows `SIGKILL|SIGTERM|signal: (9|15)` and the journal shows, inside the step's window, earlyoom's `sending SIG(TERM|KILL) to process` (`journalctl -u earlyoom`) or the kernel's `Memory cgroup out of memory` / `Out of memory: Killed` (`journalctl -k`). The query lives in `memory_kill_in_journal` so tests can stub `journalctl`. A memory kill is never the tree's failure: the step re-runs once (`killed for memory: <step>; retrying once`), and a second kill exits 137 with `killed for memory twice: <step>`. Each kill appends a byte to `MEM_KILL_FILE`; the second creates `<file>.stopped`. `note_event` passes `BUILDLOG_MEM_KILLS` and `BUILDLOG_MEM_KILL_STOPPED` to `record.py call`.

`verify.sh` also times the `cargo` board-token acquisition and passes it as `BUILDLOG_TOKEN_WAIT_S`. The clock is Bash's `$SECONDS`, or the integer in `UNIT_TOKEN_CLOCK_FILE` when set, so a test advances a file instead of sleeping. A wait that times out exits 1 before any call is recorded, so its time is in no row.

Queue-priority commands accept only unit labels in the local roster. Only sessions with a real unit supply membership; the no-unit display placeholder supplies none. Unknown units, including that placeholder, leave the old priority unchanged and return `build queue: unknown unit <unit>; the queue priority did not change` on stdout with exit 3. The pane shows this through its existing refusal protocol. Callers pass a unit as `--unit=<unit>`, including names that start with `-`. The pane passes its own session id to the command. Each card shows the stored priority reason once and counts priority steps as `N of <unit> (queue priority)`; old per-step bump records are not card reasons. Clearing an unreadable priority file removes it and names the removed file.

Durable `Waiter` rows keep their join and token clocks. `RankedWaiter.order` holds `PriorityUnit` or `NormalOrder` only in the resolved queue. Priority transitions decode to `SettledPriority` or `PendingPriorityTransition`; a pending transition keeps its id and saved month-log offset through retries. One cleanup guard isolates both a failed token-wait intent and failed dead-owner reservation finalization. Each failure logs its record path and keeps the record for the next poll; other cleanup and admission continue.

### Cargo token reclaim (`scripts/direct/board.sh`)

`verify.sh` takes the board's `cargo` token before each local compiling attempt requests memory.
`acquire_token()` waits in the existing FIFO queue and reuses a token that this call already holds.
The attempt makes one memory request and retains the token during its memory wait.
The client's periodic leave hook renews the token with `board.sh renew` while the attempt waits.
The hook starts before the first connection attempt, so connection retries also renew the token.
The execution guard still checks the token owner before cargo starts.
`unreachable_limit_seconds` limits missing-socket and refused-connection retries to 60 seconds by default.
The client reports the retry interval once when a connection fails.
A successful connection resets that interval.
At the limit, the client exits with status 2, and verify releases the token without starting cargo.
A blocked token caller has no memory waiter or reservation.
A memory wait can delay peer calls on the same board until memory becomes available.

Admission failure, cancellation, a hold, and departure for the Mac release the token.
A later local attempt takes the token before its next memory request.
Each `run()` path releases tokens that it acquired, as required by verify Phase 36.
Noncompiling local steps retain their existing token path and request no memory.
The wait log names the holder and its PID, including holder files without a final newline.
It says `this call holds it` when the recorded PID equals the call's PID.

Old shells can still send `admit`, `token-wait`, and a second `admit` with `BUILDLOG_MEM_TOKEN_BACK_AT`.
The service keeps this compatibility path and records its token gap.
New shells generate neither the second request nor a token gap.

A hold releases the token until the hold ends.
A call that waits again keeps its original FIFO position through its `.again` waiter file.
The hold wait and token wait retain separate history records.
`TOKEN_WAIT_FILE` carries a step's token wait to the call record.
`TOKEN_WAIT_FILE.given_back` records a subshell release for its parent.
 An INT, TERM or HUP during the token wait stops the waiting acquire and gives back a token it took in the last instant, and with `--pid N` `board.sh acquire` itself exits 4 (`gone <res>: pid N ended while it waited`) once N is gone. A step whose call is gone stops its token wait (`token_wait_orphaned`): each pass checks that the call's pid (`$$`) still runs, an acquire that exits 4 ends the wait too, and the step prints `verify.sh: the call that started this step is gone, so the step stops waiting for the cargo token.` and exits 1. On 2026-10-20 a test killed a call's group and left its step, which runs in a group of its own (`run()`'s `set -m`), trying again for 45 h with 15 GiB of deleted output still open; the test's cleanup now also ends every process in the call's session. A release moves the lock's `reclaimed_holders` lines whose holder still runs to `locks/<res>.reclaimed`, and the next acquire moves them back into its lock, so a reclaimed holder's late release still exits 3 after the token changed hands again. `board.sh acquire --pid N` (a positive integer, else exit 2) records N in `locks/<res>.d/holder_pid`. A waiter treats the token as stale when that process is gone, checked first, or when its hold has expired. Gone means `ps -p N -o stat=` exits 1 with empty output or reports a zombie; anything else, a failure of `ps` included, counts as alive, and a lock with no or a malformed `holder_pid` waits for expiry. So a `verify.sh` killed before its EXIT trap frees the token within one 3 s poll, posting `token cargo reclaimed from <holder>: holder pid <N> is gone`, while a manual holder is never reclaimed early. A newer call from the same worker, with the same verb and arguments in the same worktree (a lint key leaves out its package), ends an older one that still waits for its first token: each waiting call keeps `token_waiters/<slot>.<key>.<start>.<pid>` on the board, and a newer call sends SIGUSR1 to each older one whose pid still runs `verify.sh`. The older call gives back a token its acquire took in the last instant, prints `verify.sh: superseded by a newer <verb> call`, records outcome `superseded` and exits 76 (`VERIFY_SUPERSEDED_STATUS`); `mesh_runs.py` delivers that as `Result: SUPERSEDED (exit 76)`, not a failure, also when the run has a done message. A call whose acquire already won runs to the end, and a SIGUSR1 after the first wait does nothing. On 2026-10-21 one worker queued 27 calls this way, most on trees it had already left, and its peers waited up to 20 h for the token. Reclaiming a dead holder first SIGKILLs the step process group it recorded in `locks/<res>.d/owned` and fails closed (acquire exits 1, token left in place) when that group cannot be inspected or stopped; an expired hold whose holder is alive is reclaimed without touching its build. Reclaim and release run under an exclusive flock on `locks/<res>.guard`, taken by python3 `fcntl` on descriptor 9 (the Mac has no `flock(1)`) and dropped by the kernel when its holder dies: a reclaimer re-reads the lock under it, so two waiters cannot both reclaim and a release cannot remove a lock a reclaimer just made. Reclaim never waits for the guard; release retries for up to 10 s, then exits 1 (`board.sh: cannot guard <res> release`).

### Build log: records and schema

Index `SCHEMA_VERSION = 33` (`scripts/buildlog/index.py`). `open_for_update` rebuilds the index on a version mismatch.
Memory and coordination columns:

| Table | Column | Meaning |
| --- | --- | --- |
| `steps` | `mem_wait_s` | seconds the gate held the step (`BUILDLOG_MEM_WAIT_S`) |
| `steps` | `slice` | `builds` (scope ran), `fallback` (scope failed, ran without one), `none` (no scope tried) |
| `calls` | `token_wait_s` | cargo-token acquisition seconds; 0 when absent or not a whole number |
| `calls` | `mem_kills`, `mem_kill_stopped` | memory-killed attempts; 1 when a step was killed twice |
| `samples` | `builds_anon_bytes`, `ci_anon_bytes` | the `anon` line of each slice's `memory.stat` (process memory); NULL when absent |
| `samples` | `sccache_in_service` | 1 when `builds.slice/sccache.service/cgroup.procs` holds a process, 0 when empty, NULL without `builds.slice` |
| `samples` | `zram` | JSON zram storage at the sample time; NULL in older rows |
| `memory_snapshots` | `src, host, at, slices, zram` | JSON slice counters and zram, indexed on `at` |
| `ci_jobs` | `run_state` | `JobRunState.RAN / SKIPPED / CARRIED_OVER` |

`steps.wait_s` keeps metadata and pass-record lookup time plus the ordinary build wait; `mem_wait_s` is memory admission. `calls.hold_wait_s` records held-target waiting separately, including each declined Mac retry, and `calls.target` names where the call ended. The Waiting table keeps `Held target` apart from `Memory admission`.

### Slice snapshots

`scripts/buildlog/memory.py` supplies the slice snapshots.
Each `buildlog sample` tick writes one sample row with slices and zram.
`memory_snapshots` holds standalone snapshots and the zram-only tick rows of older logs, so the memory window reads both tables.
The standalone `buildlog snapshot` row keeps slices and zram.
Each slice (`builds` or `ci`) stores its events, peaks, limits, and memory pressure total.
An absent slice has `state: absent`. An unreadable pressure total has the value `unavailable`.
The zram reading stores the data and compressed bytes from `mm_stat`.
It takes the nearest slices value, then the nearest zram value, each from both tables.
With no slices in range, the result is unavailable.
Snapshot rows have priority on equal distances.
`BUILDLOG_BUILDS_CGROUP`, `BUILDLOG_CI_CGROUP`, and `BUILDLOG_ZRAM` select fixture paths for tests.

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

**Daily report** (`buildlog report [day]`, `scripts/buildlog/report.py`). Every step-based section counts natedev's steps only (`host = 'natedev'`); `### Mac` holds the Mac's (`docs/as-built/mac-test-offload.md`). Order: `## Builds, <day>`; `### Waiting`; `### Rebuilds` (where compile time goes; `docs/as-built/build-followups.md`, Rebuilds section); one section per kind; `### Memory pressure`; `### CPU and disk pressure` (`docs/as-built/buildlog-memory-stalls.md`); agent calls; test builds; `### Power profile` (`docs/as-built/build-targets.md`); Tests by target; `### Mac`; port-lint; `### CI`; `### Tests per edit`; Disk; summaries; one-line totals and notes.

- `### Waiting`: a `Wait | Longest | Over 5 min | Waited | Total | Worst` table, always present, in this order: `Build-folder turn, behind another worker`, `Build-folder turn, behind its own call`, `Held target`, `Memory admission`, `CI queue`. Held-target seconds come from `calls.hold_wait_s`; memory seconds from `steps.mem_wait_s`. `Longest` names who and its time; `Over 5 min` counts above 300 s; `Waited` is the count out of the day's population; `Total` is worker-hours or job-hours; `Worst` gives the top three. Each row uses `wait_row`; an empty kind reads `none`.
  - **Build-folder split.** `build_folder_waits(connection, day) -> (another_waits, own_waits)` returns two `ReportedWait` lists (`tuple[float, str, str, str]`: duration, owner, name, started_at), one entry per call in each, with the call's part of the wait as the duration and the worktree as owner and name. It reads the day's `tool = 'verify.sh'` calls and the token holders: verify.sh calls with a `unit_session` and an `ended_at` on the day or later. `token_holders()` turns each into `TokenHolder(call_id, unit_session, worker, starts_at, ends_at)` over `[started_at + wait_s, ended_at]`, grouped by session so each call scans only its own session's holders.
  - The cargo token is held per worker session, so only calls of the same `unit_session` can cause a token wait. A positive `token_wait_s` on a call with a session goes through `attribute_token_wait(call_id, unit_session, worker, starts_at, wait_s, token_wait_s, holders) -> TokenWaitAttribution(behind_another_worker_s, behind_own_call_s)`; a call with no `unit_session` puts its whole wait in the another-worker row.
  - `attribute_token_wait` takes the wait interval `[started_at + wait_s - token_wait_s, started_at + wait_s]` and splits it at the boundaries of the overlapping same-session holders other than the call itself. A segment is own only when every holder covering it has the call's worker; every other segment, uncovered included, counts as another worker. Own is capped at `token_wait_s`; another worker is the remainder.
  - A NULL worker reads `(unknown worker)`. Session calls with no worker never wait but can hold the token, and a worker waiting behind one counts as another worker.
- `### Memory pressure` always has its heading. It carries `memory waits: N steps, total T, longest L (<caller>)` or `memory waits: none`, the three instrument lines (sccache, unsliced steps, memory kills; natedev's samples, steps and calls only), the stall table, and `60 s samples: peak used memory X, peak swap Y; peak builds B, peak CI C (process memory); machine stall: ...` (`unavailable` per slice without samples).
- A `p95` follows every average: the kind tables (`COMMON_HEAD = Runs, Failed, Avg, p95, Range`; `COMMON_SQL` fetches `group_concat(duration_s)`), each `AverageColumn` (`<title> p95`), the summaries, Tests per edit (`p95 to next green`) and the CI queue clause. `nearest_rank(values, percent)` uses sorted index `(percent*n+99)//100-1` and also serves the Test builds p75 and the Rebuilds package p50 and p95.
- `### CI`: head `Workflow, Runs, Failed, Cancelled, Avg, p95, Range`; the summary names `<n> cancelled` only when nonzero. The queue clause is `jobs queued <avg> on average, p95 <p95>`, plus `, <N> without a known queue time left out` when any, or `no job has a known queue time` (`(<N> left out)`). Days key on the run attempt's `started_at`. Both CI summary paths end with `ci_freshness(day)`: `; CI never polled`, or `; CI recorded through <time>`, plus `, no poll since` when older than `min(now, day end) - CI_STALE_AFTER_S` (2 h) and `, the last poll was capped` for a capped poll.
- `brp-launch` steps show as `example launches (brp)`; launches from temporary folders group under their worktree, not as scratch (`GROUP_AS_SCRATCH`).
- The Mac note starts `Mac: sync paused since <time> (<why>).` while paused.

### Mac sync pause (`scripts/buildlog/sync.py`)

The hourly task exchanges records with every configured SSH peer.
After a completed Linux offload run, natedev releases the slot claim before it calls `sync_target(target)`.
This exchange contacts only that target and preserves its configured host paths, including `aws-build/`.
It skips a paused sync and never waits for the sync lock.
It uses the configured connection, reach, and rsync timeouts.
A failed exchange prints one line and preserves the step result for the caller.
The hourly task can retry the exchange.

`buildlog sync pause <why>` writes `<root>/sync_paused.json` `{since, why}`; `resume` removes it. `sync()` holds an flock on `<root>/sync.lock` for its whole run; paused, it contacts nothing, prints `buildlog sync: paused since <time> (<why>); buildlog sync resume ends it`, returns 0 and leaves `sync.json` alone. `pause` takes the same lock (printing `buildlog sync: waiting for the running sync to finish`), so once it returns nothing contacts the Mac. `hourly` still polls CI while paused. `read_pause -> SyncPaused(since, why) | SyncRunning()`; `read_status -> SyncStatus(at, peer, ok, last_good: LastSynced | NeverSynced) | NeverSynced`. The reach check runs `mkdir -p <REMOTE_ROOT>; echo "rc=$?"` and judges the printed `rc=`; nonzero or missing (`reported rc=<n>` / `no rc= line in command output`) writes `sync.json` `ok: false` and exits 1. ssh exit 255 is a peer that did not answer (one line, exit 0). `rsync` keeps its exit-status checks.

### Build hold (`scripts/build_hold/build_hold.py`, `commands/build/hold.md`)

**Holder files.** One file per holder in `BUILD_HOLD_DIR` (default `~/.local/state/build-hold/`), one JSON line `{holder, since, for, release_eta, keep}`. `keep` holds PID/start-time pairs and defaults to empty for old files. Installed scripts refuse set directory variables or a changed `HOME`; unset the variable or use a shell with the user's home. The shell guard also refuses read-only `HOME`. The legacy free-text form reads, and an unreadable or malformed regular file stays a hold. Types include `HolderIdentity(key, name)`, `HoldState = NoHolders | ActiveHolders`, `Holder(name, since, purpose, release, key, keep)`, `ReleaseEta = KnownReleaseEta | UnknownReleaseEta`, and `ReleaseRequest = NoReleaseEta | ClockReleaseEta`. `write_hold` and `start_hold` take the resolved identity. Holder files supply state to status, dailies and Rust release checks. `natedev_holds(directory) -> NatedevHolds(in_force)` reads holder files once and never reads the cycle. Every present holder file is in force, even when the cycle has `released_at` or `release_started_at`. There is no releasing state. Only removal ends that holder's hold.

### Step state publication

`step_record.py write --report` writes the JSON record once for each verify call.
It prints the clock offset in microseconds, the JSON path, and the sibling state path.
`initialize_step_record` saves those values and the original JSON text.
Bash uses `cmp` to compare the JSON bytes with the saved bytes before each state publication.
A missing, damaged, or changed record refuses the step with exit 2.

`publish_step_state` writes a state word and an optional target to `.step-<call id>.state-$RANDOM` beside the record.
Bash replaces `<call id>.state` with `mv` in the same directory.
It starts no Python process and performs no fsync for this temporary call state.
The cleanup removes the JSON record, the state file, and this call’s `.step-<call id>.state-*` files, in that order.
The `state` and `remove` Python CLI verbs no longer exist.

`step_record.read` opens the state file before it reads the JSON record.
It keeps that file open through the join, then closes it on every path before process validation.
Without a state file, the reader keeps the original row.
The open state file supplies the UTC update time from its modification time.
An unknown state word returns `UnreadableRecord` through the existing validation path.
`on_mac` selects `mac`, `on_natedev` selects `natedev`, and `on_ssh` selects its supplied target.
Other state words clear the selected target.
Readers continue to find records through `*.json` only.

A tree-writing step publishes `writes_tree <state>` with an optional target for its whole lifetime.
The reader applies `natedev_only` with the reason `step writes the tree`, then joins the state and target normally.
The state remains visible so the hold enforcer can stop compiling steps in `on_natedev`.
A bare `writes_tree` has no state word and returns `UnreadableRecord`.
After the step, `run()` publishes `local_noncompiling` to restore the call's original decision.
The token boundary and the per-step release remain unchanged.
After a crash, readers ignore the record when its process identity is stale.

**Hold enforcer.** A holder file always means a hold.
Cycle release fields grant no permission.
`hold_enforcer.py run` checks once each second.
It reads the cheap process table once.
It then reads environments for this user's processes.
It reads arguments and working folders for candidates, their ancestors, and processes with call ids.

A process with `BUILDLOG_CALL_ID` uses `step_record.read`.
Live states `on_mac`, `waiting_hold`, `waiting_mac`, and `local_noncompiling` continue.
Children in `on_natedev` stop during another holder's hold.
The live record's controller PID and start time always continue, so verify can route the step again.
A missing, stale, or unreadable record grants no permission.
Only processes without a call id use command-name rules.

The enforcer matches arguments and executable basenames.
This includes absolute Rust toolchain, cargo bin, and Nix paths under direnv, systemd-run, or a Codex sandbox.
Matches include building cargo verbs from the shared set in `hold_applies.py`, nextest, compilers, clippy-driver, cargo-tile-real, and session-owned rust-analyzer.
They also include Python unittest, pytest, `test_*.py`, basedpyright, and shells that run `test_*.sh`.
Mac coordination scripts, version probes, cargo lookup verbs, and basedpyright-langserver continue.

The sole holder's runs continue.
Its `--keep <pid>` list and descendants continue after PID and start-time checks.
User terminal and desktop app runs continue without agent ancestors or session markers.
Shared sccache-server work, the enforcer, and its ancestors continue.
A run from before `since` has no special permission.

Every other run receives the holder and reason on stderr when possible.
The enforcer sends SIGTERM to the root first, then its descendants.
After two seconds, it sends SIGKILL to survivors.
It checks the start time before each signal.
A route state change does not cancel a pending kill for an identity that the enforcer already stopped.
New Mac children stay protected.
A release, sole-holder ownership, or an explicit keep still cancels that kill.

Each stopped root writes a permanent `hold_build` row through the shared audit writer.
Schema 21 adds `action=audit|stopped`; readers treat old rows as audit rows.
The row carries the root's `call_id` from `BUILDLOG_CALL_ID`, or an empty value.
Verify can thus find a stop after the hold ends.
The owning session registers for one release notice.
The enforcer does not signal `verify.sh`, `targeted_test.py`, or `merge_gate.py` without a call id.
Each waits at its own hold gate.
With a call id, the record decides.
Verify routes eligible steps to the Mac and waits for other steps.
Their running natedev test and type-check children still stop.
They write stopped rows with the PID and start time.
The merge gate reads those rows and reruns the stopped modules after release.
No run pauses: cargo must release `/dev/steve` slots and locks, and wall-clock limits continue.

`hold` starts the user unit `build-hold-enforcer` with absolute paths, or keeps an active unit.
Without systemd-run or a user bus, it starts a detached process.
`BUILD_HOLD_ENFORCER_DIR` holds the singleton lock.
No paused record or thaw exists.
`run` exits when no holder file remains.
`scan` reads state without changes and prints allowed, kept, or would stop, with the owner, reason, and pass time.
Status and the hold-start audit print kept runs and `--keep <pid>` for runs that would stop.
A read-only pass after release on 2026-10-28 took 0.585030 seconds over 1,736 processes.
This measurement sets no pass-time limit.
Other users' processes, such as the CI runner, are outside its reach.
Runs shorter than one pass can finish first, so call guards refuse those starts.

**Build call guard.** `settings.json` installs `scripts/hooks/pre-tool-use-build-hold-guard.sh` for Bash PreToolUse. 
The Bash front reads stdin once.
It starts no Python when the hold directory has no entry.
It also starts no Python when the input has none of `cargo`, `merge_gate`, or `targeted_test`.
 An unreadable hold path also starts the decision reader. Otherwise it passes the same input to `pre-tool-use-build-hold-guard.py` and relays its output and status. The guard refuses raw building `cargo` verbs (including short forms and `+toolchain`) while a non-holder natedev hold applies; removal ends that holder’s hold. All `verify.sh` calls pass, with or without a step record: verify routes eligible steps to the Mac during the hold and waits for other steps. Non-building calls pass. The Python guard checks the command through `tool_call_text.py`, checks for a hold entry, then imports `scripts/build_hold/hold_applies.py` in the same process. The helper uses `target_state(local_target())`, `natedev_holds` and `caller_holds_only_own()`. The sole in-force holder may build. Other sessions see `natedev builds are held by <holder>. Keep working, writes included. Use verify.sh: eligible steps go to the Mac; other steps wait. Raw cargo and merge gates must wait for the hold to end.` Only in-force holders are named, with `holder_text`. The helper exits 0 with that refusal on stdout, 1 silently when builds may run, or 2 with a read error on stderr. The guard denies hold read errors and any other exception in its decision path with a one-line reason that names the failed read. Only an explicit allow decision permits a build; no wait is registered.
The cargo verbs include `mend` and `miri`; the guard does not match `verify.sh` step names or options.
The guard also refuses `merge_gate.py` and `targeted_test.py` in every mode under the same hold decision.
This applies to direct calls and calls through Python or `lib/py`.
The refusal tells the caller to run after the hold ends.
Quoted mentions and written examples pass.
Codex workers use the same Bash front and guard through `scripts/hooks/codex_hooks.py`.
Unit run markers let workers of the sole holder build.

**CLI.**

| Command | Behavior |
| --- | --- |
| `hold [--holder N] --for TEXT [--keep PID]... [--release-eta HH:MM --zone IANA] [--target natedev\|mac]` | Default natedev; ETA must be later today. Starts the enforcer, prints its notice, keep list and audit. Every test and build outside the allow rules stops, including runs already started; its session gets one release notice. Repeated holds add kept PID/start pairs and reset release fields. Mac runs `mac_test.py block`; keep, ETA and zone are refused. |
| `quiet [--max-wait 600]` | Waits for remaining builds; busy while any user's `cargo`, `rustc` or `cargo-nextest` run; counts each command by user (`your builds still running: 7 cargo, 5 rustc; ci-runner: 2 rustc`). Exit 1 when busy. Load does not delay it. Exit 0 prints `builds are quiet: no build runs. 1-minute load <load> is other work: <five heaviest processes>`, with user, command and current CPU percent over about one second, or `none visible`. Only success takes the CPU window: two reads of `/proc/<pid>/stat` user and system ticks divided by elapsed clock ticks; processes that end or use no CPU in the window are skipped. |
| `wait` | Returns at once through the shared local hold check. Exit 0 permits a build only when no other session’s holder file exists. The sole holder may build. Exit 10 refuses and registers the session for release notice; exit 12 means unreadable state. Cycle release fields never grant permission. Start only on 0; after 10, wait for release and check again. |
| `record-recipient --session-id --name` | Written by `top_level.py`. |
| `release [--holder N] [--target natedev\|mac]` / `release --resume` | Removes its holder file first, then records released_at and release_started_at; notices follow after the last file is gone. Other holder files keep the hold in force. One notice per recipient; the memory gate orders builds. `--resume` refuses while any holder file exists. Mac runs `mac_test.py unblock` and refuses resume. |
| `status` | One line per holder with since, reason and ETA, its keep list, cycle delivery rows, and the audit with `--keep <pid>` for each run that would stop; or `no build hold`. Then `release pending:` lines name unfinished releases and their unsent recipients, using the release drain's reader, and `/build:hold release` to finish them, even with no holder file. A damaged record or cycle is named with its path, error and the command that sets it aside. Status changes no file. Present files stay held despite cycle release fields. |
| `audit` | Reads running build processes from `/proc`. Prints session-owned builds by class. Records other_session and no_session builds when a hold is in force. |

**Build audit.** `audit_builds(proc_root, holders)` reads `cargo-tile-real`, `cargo`, `cargo-nextest`, `rustc` and `rustdoc`. One top cargo or nextest process is one build. Nested cargo, nextest and compiler processes are its children, including through a shell. A compiler with no cargo ancestor is a separate build. The audit reads each process start time before and after its other files. It checks the process and its ancestors again before classification. A parent with a later start time cannot own an earlier child. An unreadable environment does not remove the build from the audit.

The owner comes from `CLAUDE_CODE_SESSION_ID`, `owning_session(environment)`, or `CODEX_THREAD_ID` on the process or an ancestor.
Otherwise, the nearest `claude` or `codex` ancestor supplies the owner.
An agent without a session id has a command, PID, and start time as its identity.
The `holder` class uses the file key rule from `caller_holds_only_own`.
That session's hold must be the only hold in force.
The `admitted` class requires membership in `builds.slice` or a matching reservation owner from the service's bounded `view` query.
The audit compares both the PID and start time for the process and its ancestors.
An unavailable service causes the same audit error as an unreadable admission folder did before the service.
An inherited verify call id does not prove admission.
The other classes are `other_session` and `no_session`.
These classes describe ownership and admission; the enforcer applies its allow rules separately.
The printed audit omits `no_session` builds.
The audit sends no messages and stops no processes.
The `hold`, `status`, and `audit` commands accept `--proc-root` for a fake process tree in tests.

The audit calls `hold_enforcer.record_build(..., action="audit")` for each `other_session` or `no_session` build. The enforcer uses the same writer with `action="stopped"` for each stopped root. Both call `store.append_line` in the current process, after the audit read; no detached audit recorder runs. The kernel boot id, PID and process start time identify the build. The permanent file is `buildlog/<host>/hold-builds-<hold hash>.jsonl`. A separate `.lock` file covers the duplicate check and append, once per build, holder key, `since` and action. A new hold or a reused PID gets a new row. Index schema 21 adds `action` to `hold_builds`; old rows read as `audit`. Rows record hold identity, build identity, class, owner, command as shell text, working directory and observation time. Holder and admitted builds get no audit row.

**Holder key (2026-10-08).** A hold file is named by the holder's Claude session id: the caller's (`CLAUDE_CODE_SESSION_ID`) when `--holder` is left out, else the live session `--holder` means (`sessions.addressed`: its name now, or a name it alone once had). `Holder.key` is that file name and `Holder.name` is the session's name now, looked up at each read; the `holder` field in the file is the name at hold time and is shown only while the session is not live. `cycle["holders"]` keeps the key given to `start_hold`; `holder_path` replaces unsafe characters to make `Holder.key`, the file name. Different keys can name the same file, so a recorded release matches both that file name and its `since` instant. A holder that is no live Claude session keeps its name as the key. `held_key` finds a hold taken under a name before this change by the name given, the session's name now and its former names.

**Hold cycle.** `BUILD_HOLD_RELEASE_DIR` defaults to `~/.local/state/build-hold-release`, outside the holder directory. `current` names `<id>/cycle.json`, atomically replaced under `release.lock`. `HoldCycle` stores its id, open time, holders with `{since, released_at}`, recipients by session id, release start and `ReleaseEntry` rows with session id, name and delivery outcome. The first hold with no files opens a cycle without reading an old one. With other holders it joins the cycle; without a readable cycle it writes its file and opens none. Old cycle files with call, generation and gate fields still read; those fields no longer control a build.

**Release** (`release_cycle`). Under `release.lock`, `release_hold` first removes the holder file and queues the `ended` event. Only then does `release_cycle` record the holder's `released_at`. With another holder file present it prints `released; still held by <holder> (for ..., release eta ...)` and sends no notice. After the last file is gone it records `release_started_at` and sends notices. A stopped release leaves either a present file that still holds, or no file and a released hold; cycle fields grant nothing. All `wait-local` calls can then return and enter their memory gates. No waiter acknowledgement or turn can delay release. The memory gate orders the builds and starts each only when memory is available.

After the file is gone, `notify_release` sends one notice to each recorded recipient: start builds and BRP launches now; the memory gate orders them. No answer is needed. A notice's outcome is saved after its sender returns; a stopped send stays pending and is retried, so a session can receive a notice twice. `socket_for` returns `RecipientLive | RecipientGone | RecipientLookupFailed`; lookup exit 1 means gone, other failures retry three times with one second between tries. Send exit 0 is delivered, 1 queued but unconfirmed, other failed. `Not reached, tell them yourself: <session name> (<reason>)` names failed, gone and unconfirmed deliveries. `cycle_status_lines` prints delivery outcomes. Release waits for no answer, build or memory allocation.

`release --resume` supports a file left by an old release. It refuses while any hold is in force. With no cycle, the holder broadcasts the release. `open_cycle`, `save_cycle` and `write_hold` write a complete temporary file before `os.replace`; holder temporary files stay outside the holder directory.

**Release intent.** The last holder's release is saved atomically in `BUILD_HOLD_RELEASE_DIR/pending-release.json` before its holder file is removed. A new hold keeps an earlier pending record under `releases/<release-id>/`, so it cannot drop that release's notices. `release` and `release --resume` drain each pending record on its own when no hold is in force: a record whose notice or broadcast fails stays pending, and the drain goes on to the next. Under the release lock, a damaged pending record or referenced cycle is renamed with the same `.damaged-<UTC YYYYMMDDTHHMMSSZ>` suffix used for a damaged current cycle (`-2`, `-3` when taken). One line names its path, error and saved path, and the drain continues. A damaged or missing cycle is rebuilt from its pending intent's holder and recipients before sending; an uncertain delivery can repeat. A stop after the rename leaves the intent available for a rerun. A drained record becomes the one completed receipt for its holder key, at `receipts/<holder-key>/completed-release.json`, replaced in place, so receipts do not grow; older receipt folders are moved there on release lookup. A release without a usable cycle keeps its broadcast pending until the broadcast returns. Live holder files take precedence over receipts. A finished release prints `hold is already released.` even after another cycle opens, followed by the details of any record it drained, `Not reached, tell them yourself` lines included.

`status` reads current and retained pending releases without taking the release lock or changing files, including old receipts. Each `release pending:` line gives the holder, release time in the user's zone, unsent names and `/build:hold release` to finish it. Status and the drain call `release_notices` to validate the referenced cycle id and select the same first entry for each recorded recipient. A missing cycle entry, `NotAttempted` or `DeliveryFailed` means unsent; a broadcast says `every session (broadcast)`. An unreadable record or cycle, including an id mismatch, is named by path with its error and the command that sets it aside. Completed receipts add no pending line.

**Damaged record.** `read_cycle` validates the `HoldCycle` shape, session identities and delivery outcomes and requires each holder's `since` to be an instant with an offset; it raises `ReleaseRecordReadError` (a `ValueError`) naming the fault. An invalid holder time follows the same recovery path as any other unreadable record. Changing commands read through `read_cycle_for_change() -> HoldCycle | NoCycle | DamagedRecordSetAside(path)` under the lock: on damage it renames `cycle.json` (when it exists or is a dangling symlink), else `current`, to `<name>.damaged-<UTC YYYYMMDDTHHMMSSZ>` (`-2`, `-3`... when taken), removes `current`, and prints `release record could not be read (<error>); set aside as <path>; this hold now releases every session at once`. Read-only `status` and the dailies footer print `release record could not be read: <error>; /build:hold release sets it aside and ends the hold` and change nothing.

**Recorded hold intervals.** A new holder file queues a `began` event after writing the file; every removal in `release_hold` queues `ended`, using that file's key, name and reason. Repeating a hold writes no event. The event's UTC `at` is taken under the coordination lock and queued there; a detached `record.py hold-event` starts after the lock closes. A stalled event log cannot delay hold or release. Both reports read current coordination state independently. A lost event reads `end not recorded` or `began not recorded`, never a changed hold. The index keeps one row per event with its source file; the `holds` view pairs by host, target and key, ordering equal instants began first. Overlapping holders count once in held duration. A present holder file keeps an interval with no recorded end open, regardless of cycle release fields. Times shown use the user's zone, stored instants UTC.

**Target option.** `--target` on hold and release defaults to natedev and is decoded into `NatedevHoldTarget | MacHoldTarget | UnknownHoldTarget | UncheckedHoldTarget`. Only a name other than natedev or mac reads the config, through `read_targets` imported at module level from `scripts/build_targets/target_config.py`; a readable config answers with the configured targets, an unreadable one with `unknown target <name>; the target config cannot be read: <reason>`, and both exit 2. The Mac subprocess is found beside build_hold via `Path(__file__)`, runs with `sys.executable`, and returns `MacHoldResult(status, stdout, stderr)`. It creates no natedev holder, cycle or event. `--release-eta`, `--zone` and `--resume` with Mac print the unsupported option and exit 2. `/build:hold`, `/build:mac` and `/build:status` are grouped beside the two reports; `/verify_saved`, `/lint_config` and `/validate_and_push` stay separate.

**No-cycle release.** With no cycle, `release` calls `release_hold` under the lock; the last holder's line becomes `released, builds may resume. No hold cycle: broadcast this release to every session.`, and the command doc has the holder broadcast it with `/notify_top_level --here`. A cycle release's final line never carries that clause.

### Broadcasts to sessions (`scripts/message/top_level.py`, `commands/notify_top_level.md`)

`top_level.py` takes no arguments (any argument: `usage: top_level.py`, exit 2) and runs no ssh. `forwarded_sessions()` reads this machine's roster (`roster.roster(os.environ, roster.ThisMachine())`, `docs/as-built/session-roster.md`) and keeps each `roster.is_top_level` row: a Claude session that is neither a worker nor a unit director under a showrunner, so a unit director with no running showrunner is included. It leaves out the sender by `roster.sender_identity` and `roster.is_sender`: `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`; with neither set nobody is left out. Roster problems print on stderr as `top_level: roster: <problem>` and the rest still prints. Each row's socket comes from the newest running record for its session id (`sessions.running_sessions()`), so a conversation two processes hold (resumed in a terminal while the desktop app still has it open; seen 2026-10-08 as `bevy_brp` listed twice) is listed once, and two sessions that share only a name are both listed. Each row becomes `AddressableSession(name, session_id, address) | UnaddressableSession(name, session_id)`. Only addressable sessions print, one `<name>\tuds:<messagingSocketPath>` line each, sorted by name then address, and are recorded as hold recipients (`build_hold.py record-recipient`; `main` returns 1 when a record fails); each unaddressable one prints `not reachable: <name> [<session id>] has no messaging socket` on stderr. `/notify_top_level` sends to each `uds:` address, adds every ListAgents peer on another machine unless `--here`, asks showrunners to pass the message to their unit directors, names held or refused deliveries by name and address, and reports each `not reachable` line as a session not told.

### Dailies (`scripts/production/dailies_render.py`)

- Holds: `read_dailies_hold()` reads the holder files. The report and `--footer` show each holder's purpose, start time and release ETA. Old files show `released HH:MM ZONE; the memory gate orders builds`. `hold_action_lines` names recipients without a confirmed notice: `<session> could not be told builds are released; tell it`. It uses the session name, or `an unnamed session` when only an id exists. `build_hold.py status` keeps the delivery list. The last release removes the holder file. The renderer refuses these states with exit 2: a top-level `build_hold` field; a build hold marker without a holder file; an invalid holder marker. Active holders require a marker when any report unit does not hold the builds itself. A report with no units can render. A report whose units all hold the builds can render. A holder marker is invalid when its unit carries `holds_builds` or matches an active holder's name. The diagnostic is `units.build_hold: <unit> holds the build hold itself; remove its marker`. A holder purpose cannot contain production plumbing words.
- `### Agents` sits after the hold lines and above `waiting on you:` (the `--footer` form writes the same lines as bullets, with no heading): one line per note in use (`role` or `sessions`) in `AGENTS_DIR` (`~/rust/hanadocs/agents/`), by account label, e.g. `- codex2: 78%; runs out about 20:45 PDT today, before its Sun 02:25 refill; 1 reset available until Oct 29`; `- none active` otherwise. Run-out = reading time + (100 - used) / rate, rounded to the minute. The rate is `run_out.weighted_rate` over the `READINGS_LOG` readings: each rise weighted by half per 12 h of age; refills, time at 100% and gaps over 15 min are skipped. Until the readings cover a weighted hour, it is used percent over the time since the later of the last refill and 24 h ago. `scripts/whoami/agent_notes.apply` appends `{"account", "at", "remaining"}` to `~/.local/state/agent-notes/readings.jsonl` and keeps 8 days.
- `same_phase` compares only the title after the first `: `, so a renumbered phase keeps its ETA history and a retitled one starts afresh. `draw_from` sets the latest cell only when it is after the ETA cell.

### Disk floor: what it removes (`scripts/lint/sweep.py`)

While `/` has less free than `sweep_free_floor_gib.<host>` in `config/lint.conf` (300 GiB on natedev), `hold_floor` removes build output from the cargo target directories under `sweep_floor_roots` that no build holds. It removes only the shortfall (`budget = total - (floor - scanned_free)`, with `scanned_free` read after the scan that measures the caches, so space another process frees during the scan is not taken again; at or above the floor after the scan it takes nothing), from idle targets in last-use order, oldest first, across all locations (`/tmp` targets have no special priority; `build-targets.md` → Build output retained by the sweep):

- A target that goes away during a pass counts as done. The pass finishes with status 0. Its bytes do not count as removed.
- **What a target is.** `is_cargo_target(directory)`: a directory holding `.rustc_info.json`, or cargo's `CACHEDIR.TAG` (matched on its line `# This file is a cache directory tag created by cargo.`) beside a build tree (`build_trees`). `target_dirs()` and `disk.py`'s `outside_directory_bytes()` both call it. Either marker alone misses targets: into a folder cargo creates, the tag and `debug/` appear at once and `.rustc_info.json` comes when the first build ends, or never; into a folder that already exists (mend's wrapper makes hana worktrees' first) cargo writes `.rustc_info.json` early and no tag.
- **Targets in use.** A target whose cargo locks a build holds is busy, and so is one a live process uses (`process_uses`, 2026-10-15): its `/proc/<pid>/exe`, `cwd` or a `maps` file lies under the target, or it is a cargo-nextest run (`nextest` as its first argument, or argv[0] `cargo-nextest`) whose `--target-dir`, `CARGO_TARGET_DIR`, or else the directory beside a target that holds its cwd, names it. nextest's run phase holds no cargo lock and executes binaries from `deps/`; a floor sweep at test 2105 of 4072 left 1965 hana tests failing to exec. `hold_floor` prints `lint sweep: pid <pid> <how>; <target> left alone` and counts it with the held targets; `sweep_workspace` prints `… skipped` and sweeps nothing. A running app keeps its target too. An exe or mapped file that reads `(deleted)` is no use (2026-10-21): the sweep can neither take nor break it, so a stale process whose exe and mappings under a target are all deleted, with its `cwd` outside, leaves the target idle (a hung `cargo-berth resolve` had held 26 GiB of cargo-liner-berth's target for three days). One scan of `/proc` covers every target (0.21 s over 110 targets and about 600 readable processes); it runs only below the floor. Without `/proc` (the Mac) nothing counts as in use.
- **Roots.** `sweep_floor_roots` is `~/rust`, `~/.local/state`, `~/.cache` and `/tmp`. `~/.cache` came in on 2026-10-21, after 421 GiB of idle targets under `~/.cache/trace-targets` had stayed unseen; it adds 0.4 s to the walk (21,000 folders). `~/.cache/sccache` holds no `.rustc_info.json` and no cargo tag, so it is never a target.
- **Agent scratch under /tmp.** `sweep.py --floor-only` calls `tmp_sweep.run_if_due` before `hold_floor`, so idle scratch goes before any build cache. It runs the sweep when `tmp-sweep.stamp` in the lint-sweep state folder is an hour old or more (`RUN_INTERVAL_SECONDS`). The stamp moves before the run, so a failed run also waits an hour. `tmp_sweep.py [--dry-run]` runs it at once. For hand cleanup, run `tmp_sweep.py --plan <new-plan.json>` to list every candidate with `keep` or `remove` and one reason, and save the plan without deleting anything. Then run `tmp_sweep.py --apply <plan.json>`; it judges every candidate again once, at its start, with the timed sweep's ownership and process-use checks, keeps planned paths that gained a keep reason, and logs each removal. It never deletes an unplanned path. Hand cleanup also requires end evidence: all recorded workers ended, an expired scratch task, or a recorded verify run that stopped. Age alone does not permit hand cleanup. On the Mac, the hourly `tmp-sweep` job (`modules/darwin/tmp-sweep.nix` in nixos) runs `tmp_sweep.py`. The separate disk-floor job runs every 120 seconds. The roots are the same on both machines, each by its real path, because the Mac's /tmp is a link to /private/tmp and lsof prints real paths. They are `/tmp/claude`, `/tmp/claude/delegate`, `/tmp/claude/unit`, each `-`-prefixed project folder in every `/tmp/claude-<uid>` this user owns (so Claude Code's own `bash-edit-diff/` stays), the scratch root `/tmp/claude/scratch` (one entry per direct child; a change review row's folder stays until its verdict), and the temp folders: /tmp, `$TMPDIR` and the Mac's `CS_DARWIN_USER_TEMP_DIR`. In a temp folder only a tempfile folder and a `lint/invoke.sh` log file (`INVOKE_TEMP_NAME`: `lint_invoke.*.log`, `buildlog.*.log`, `.peak` and `.peak.cgroup`; a killed run leaves them, and natedev /tmp held 1,308 and 1,438 on 2026-10-27) are judged. A tempfile folder (`TEMPFILE_NAME`) is `.tmp` and 6 letters or digits (Rust's tempfile crate), `tmp.` and 8 to 10 letters or digits (mktemp), or `tmp` and 8 lower-case letters, digits or `_` (Python's tempfile), the names test runs leave behind (natedev /tmp held 2,662 Rust ones on 2026-10-26 and the Mac's temp folder 273; the first folder audit that day found 3,917 mktemp and Python ones). In /tmp alone (the wide root; `$TMPDIR` and the Mac's temp folder hold app state, and macOS clears its own) every other empty folder this user owns is judged too, whatever its name, and uses `lint.conf` `folder_remove_after_seconds` with the same process-use check. A folder that holds only folders, however deep, counts as empty (`holds_no_file`; a tree of more than 1,000 folders does not), and its idle time is the newest mtime in the tree (2026-10-27: 155 nix-shell folders of empty `hana-runtime` folders stayed). A `direct/verify.sh` run folder (`VERIFY_TEMP_NAME`: `verify-hold.<pid>.XXXXXX`, with the run's `verify-copy.<pid>.XXXXXX` folders inside it) is judged in every temp folder. Its EXIT trap removes it, but a killed run leaves it (natedev /tmp held 47 hold and 6 copy folders on 2026-10-27). It stays while the named pid runs `verify.sh` (a `verify.sh` argument in `/proc/<pid>/cmdline`, or `ps` on the Mac) and that process started no later than the folder's mtime, so a reused pid does not keep it; otherwise it goes at the next run. An old name with no pid takes the 4-hour rule. A folder there that holds something can hold work, so the folder audit judges it, with a git check first (2026-10-27; until then the sweep took any idle folder, and on 2026-10-26 1,103 month-old ones went by hand, an rm the user had to approve). A folder a system tool owns stays (`SYSTEM_TEMP_NAMES`: `claude`, `.X11-unix` and the other X folders; `SYSTEM_TEMP_PREFIXES`: `systemd-private-`, `tmux-`, `nix-`, `ssh-`, `claude-`, `com.apple.`, `powerlog`), except an empty `nix-` one (`EMPTY_PREFIXES`: nix-shell and nix develop leave theirs, 1,321 in natedev /tmp on 2026-10-27), as do another root and a folder that holds one. The sweep does not judge or log other entries in a temp folder. An entry is one top-level file, folder or link under a root. It goes when nothing in it changed in 4 hours (`IDLE_SECONDS`) and no process has its cwd, an open file or a mapped file in it (one `/proc` scan per run; where there is no `/proc`, one `lsof -n -P -w -F pfn` scan: `cwd`, open files, and `txt`/`mem` mapped files). A change is the newer of mtime and ctime, over everything inside the entry, except a folder's ctime in a temp folder or a session or run temp root (systemd-tmpfiles-clean moves it daily). The entry's own times count only for a file, a link or an empty folder. These stay: the roots and both `active/` run-marker folders with every marker in them; a run folder under `unit/` or `delegate/` named by any active marker, with any `mesh_roster.json` worker not ended (paused included), or whose `progress_history_state.json` worktree belongs to a live session or a paused session in the shutdown mover's records; these runs and their temp entries stay even when their director process stops. A running director recorded in `director` or the opening `run_started` history event also keeps the run. A kept run also keeps any folder that contains a `mesh_runs/*.json` record's `cwd`, or is that working folder, only while that record has status `running`. Finished records do not keep worker folders. Only these records name worker folders; pending files are not read. An empty mesh run file is not a record. Each roster thread whose worker is not ended keeps its `/tmp/claude/session/<thread_id>` parent; old entries inside that parent still take the 4-hour rule. An unreadable non-empty mesh run record keeps scratch entries and top-level `/tmp/claude` entries and prints its path. It does not keep old temp entries in session roots. Before deleting an idle entry under any root, both timed and hand cleanup check for git worktrees down to three folder levels, without following links. Uncommitted changes, untracked files, a stash or commits absent from remote and local clone-source refs keep the entry. Git checks use `GIT_OPTIONAL_LOCKS=0`, a 10-second timeout per command, and never fetch. A failed check keeps the entry. The folder audit uses the same git check. The new reasons `a live worker uses it` and `it holds unsaved git work` include kept paths in the log. Any unreadable marker, roster, session, shutdown or run ownership record keeps the affected runs; a session folder named for a running session, though an entry of 1 GiB or more in its `scratchpad/` and `tasks/` is judged like any other (`SESSION_ENTRY_MIN_BYTES`; showrunner, 2026-10-25: a live session keeps small files there that it only reads, and a read does not move mtime); each of these when a session record cannot be read; an entry that cannot be read; and everything when neither `/proc` nor lsof can show which processes use the entries. Links are never followed, a read-only folder is made writable and deleted, and a project folder left empty goes. Each run appends one JSON line to `~/.local/state/lint-sweep/tmp-sweep.jsonl`: `deleted` (path, GiB, last change), `freed_gib`, `failed`, `kept` (a count per reason), and `kept_paths` (every reason except a recent change and a small entry in a running session's folder; an in-use path names its pid and how it uses the entry). Lines older than 7 days drop off. The sweep is the only remover under its roots: `disk-floor.nix` excludes `/tmp/claude` and `/tmp/claude-*` from systemd-tmpfiles' age cleanup (`x` rules). Its `q /tmp 10d` rule deletes each file in /tmp older than 10 days, and then each empty folder. On 2026-10-26 that rule had removed an idle unit's run folder and its run marker, one file at a time. The run also prints one line per deletion and a summary to the disk-floor journal. History: it replaced `remove_session_scratch` (2026-10-21: ended sessions only, 24 h idle) on 2026-10-25. That day /tmp/claude held 122 GiB and /tmp/claude-1000 held 86 GiB, and a one-time cleanup had deleted both `active/` folders. The first dry run, with the 1 GiB line, would delete 1,891 entries (20.4 GiB) in about 4 s. The rest stayed because processes used the entries (a 70 GiB target) or because running sessions and unit runs held them.
- **Use stamp.** `sweep_workspace()` sets the mtime of `.lint-sweep-used` (`USE_STAMP`) in every root `cargo_roots()` returns, before `lock_trees`, so a step that compiled nothing, or whose sweep was skipped because a build held a lock, still counts as use. A dry run and `--target-dir` (CI) write none; a write failure is ignored.
- **Target last use.** `target_last_use(root, groups)` is the newest of the stamp's mtime and the target's build units' `last_used`.
- **Order.** `shrink()` and `choose()` take a `RemovalOrder`, a sort key over `Group`; the smallest key goes first. The workspace budget sweep passes `unit_age` (most whole days unused, then oldest compile). For the floor's target order, see [Build output retained by the sweep](build-targets.md#build-output-retained-by-the-sweep). A scratch target a build holds is left like any held target. Orphaned files (`Scan.orphans`, one group per build tree) go first from any idle target, whatever the order.
- **Whole takes and nested targets.** An idle target with no build units is taken whole in the same last-use order (its folder and lock files stay), and a Cargo target nested outside another target's build trees is a target of its own: see [Build output retained by the sweep](build-targets.md#build-output-retained-by-the-sweep).
- **Lines.** After the removal lines, one line per target that lost output, in take order (oldest use first): `lint sweep: the floor took <gib> from <target>, last used <when>` (`would take` on a dry run). Orphan bytes count toward their target, and a group whose removal failed is left out, so each line is GiB actually taken. The removal line's `last used <oldest> to <newest>` spans the oldest and newest unit among those chosen.

On natedev (2026-10-08) the target rule finds 76 targets where `.rustc_info.json` alone found 41: all 35 added are scratch folders under `/tmp`, 31 of them finished builds carrying only the tag, and five under `/tmp/ct-g1l`, outside both Claude temp folders. The walk takes 2.2 s (1.5 s before).

Measured over the 24 h after the target-first order went live (2026-10-06 16:33 PDT, 300 GiB floor): the 2-minute timer took 84.0 GiB in 17 sweeps, 98.3% of it from targets idle over 6 h, and the newest unit a sweep took was last used a median 15.2 h earlier. The 24 h before, under the old order by build unit and almost all at a 500 GiB floor: 870.1 GiB in 155 sweeps, median 3.3 h. The two days differ in floor as well as order, and the memory gate above went live 85 minutes into the second.

### Scratch folders (`scripts/lib/scratch.py`, `scripts/hooks/pre-tool-use-scratch-guard.py`)

`scratch.conf` holds the default lifetime, visible root, hold file, repository depth, excluded folders, and random suffix length.
The scratch code reads those values through `scripts/lib/conf.py` when an operation needs them.
`CONF_DIR` selects the configuration folder.

The folder audit reads clone age from `hana_instances.conf` and the Mac disk floor from `mac_test.conf`.
It reads its other limits and default state folder from `lint.conf` through the same reader.
The audit and temporary sweep share `folder_remove_after_seconds`.
The audit and repository scan share `scratch.conf` `repo_skip`.
The audit also keeps the configured `scratch.visible_root()`.
`rust_roots.txt` remains a list file. All moved values retain their previous defaults.

Agents made loose repo copies, repros and test trees in /tmp and ~/rust, and nothing removed them.
Sessions run for weeks, so a session end is no cleanup point (user, 2026-10-26/27).
Each scratch folder belongs to a task. The task's end removes its folders.
`scratch.py new [--task <task> | --until <when>] [--name <word>] [--visible]` makes a folder and prints its path.
`scratch.py list` shows each folder's task and last change.
The other commands are `adopt`, `hold`, `release`, `renew` and `end`.

Tasks use these forms:

- `run:<run id>` ends through `end_session.sh`.
- `phase:<unit>/<phase>` ends through `merge_checkpoint.py` after the phase's code checkpoint.
- `gate:<id>` ends through the gate's caller.
- `row:<showrunner>-C<n>` ends through `change_review.py verdict` after either verdict.
- `until:<YYYY-MM-DDTHHMMZ>/<id>` ends through the hourly temporary sweep when its time passes.

`--until YYYY-MM-DD [HH:MM]` selects a deadline in the user's time zone.
Each call creates a random task id from `scratch.conf` `suffix_length` characters.
Folders with the same deadline have separate tasks.
The folder name includes the deadline stamp but excludes the task id.
With no task, each folder gets a separate deadline task that lasts 24 hours.
The deadline reader also accepts old tasks without an id.
`scratch.py end --task <task>` selects folders by their exact task.

Creation and transfer accept a `phase:<unit>/<phase>` task only when the phase id appears in that unit's recorded plan.
A missing or unreadable plan refuses the new task and names the cause.
Use a recorded phase id, `gate:<id>` or `--until <when>`.
Existing tasks with an invalid phase id can still update and end.
The sweep keeps a task's folder while the task remains open.
It removes folders after seven idle days, even when the task remains open.
A plain file directly in the scratch root reads as `NotRegistered`.
The sweep uses the existing idle limit for unregistered entries.
An unreadable folder record still keeps the folder.

`scratch.py hold <path> --reason <why>` keeps a folder at its path until its task ends.
`change_review.py add --hold <path>` also creates a hold.
The hold record stays in `~/.local/state/scratch/holds.json`.
`end` removes the held folder unless a repository in it has uncommitted changes, a stash or commits absent from all remotes.
The folder stays with its hold marked ended.

A folder uses `/tmp/claude/scratch/<kind>-<name>-<word>-<6 chars>`.
`--visible` puts a row task's folder at `~/rust/repro/<showrunner>-C<n>-<word>`, where cargo-port lists it.
Each folder holds `.scratch.json` with its task, maker session and times.
A Git worktree uses `git worktree remove` for removal.
A failed removal prints a warning and does not fail the merge or the verdict.

The scratch guard, a `PreToolUse` hook on `Bash` for Claude Code (`settings.json`) and Codex (`codex_hooks.py`), refuses a command that makes a new folder directly in /tmp, `$TMPDIR`, /tmp/claude, ~/rust or ~/rust/scratch (and judges a Write or NotebookEdit file path where its matcher names them): mkdir, mktemp -d, git clone, git worktree add, cp -r/-R/-a, rsync, cargo new/init, and tar extraction into a root. Allowed there: the session scratchpads (`/tmp/claude-<uid>`), `/tmp/claude/unit`, `/tmp/claude/delegate`, `/tmp/claude/session` and both scratch roots; ~/worktrees is no root, and a `make_a_worktree` command is never judged. A heredoc body, a quoted argument and a comment are data. A path the hook cannot follow (a variable other than HOME or TMPDIR, a command substitution, a command inside `bash -c` or ssh) is allowed, and the hourly tmp sweep is the backstop. A `TMPDIR` inside a session scratchpad, a session temp root or a run folder is no root.
Before its checks, the scratch guard uses the session-start path helper to restore only the input session id's missing temp folder with mode `0700`; an existing folder costs one `os.stat`, a missing or invalid id creates nothing, and a repair error allows the tool call to proceed to the checks.

Each Claude session's `TMPDIR` is `/tmp/claude/session/<session id>`: `hooks/session-start-temp-root.py` makes it at start, resume, clear and compact, and writes `export TMPDIR=...` to `$CLAUDE_ENV_FILE`. A run's workers, its codex app-server (`codex_mesh.py`) and `verify.sh --session-dir` use `<run folder>/tmp`. So mktemp, tempfile and Rust `.tmpXXXXXX` folders land there, and the hourly tmp sweep judges each entry with its 4 h rule; the root goes only once its session or director stops, and nothing waits on that. In /tmp and the other temp folders a folder's ctime is no change, since `systemd-tmpfiles-clean` moves it every day (its 26 Oct run kept month-old folders such as `cargo-installQOkrzS` looking fresh to the 7-day rule).

The daily folder audit (`scripts/lint/folder_audit.py --send`) checks loose folders that can hold work.
A `nate.jobs` job runs it on natedev and the Mac.
It checks ~/rust, ~/rust/scratch, /tmp, /tmp/claude and `$TMPDIR`.
In ~/rust, `config/rust_roots.txt`, `repro` and `scratch` name kept folders.
Every folder in ~/rust/scratch is loose.
In temporary roots, system folders, session scratchpads, scratch roots and run roots stay.
The audit also keeps `/tmp/codex-daemon-<uid>`, which holds sockets for running Codex processes.
The sweep checks tempfile folders, `verify.sh` folders and folders that contain only folders.
If the audit cannot read `config/rust_roots.txt`, it does not check ~/rust.
Only folders owned by this user and idle for a day enter the audit.
The audit checks modification times down to three folder levels and each repository's `HEAD`, `index` and `logs/HEAD`.
It skips `target/`, `node_modules/` and `.git/` during that scan.
It keeps folders with more than 50,000 entries at that depth.
A loose folder idle for `folder_remove_after_seconds` can be removed after seven days.
An open hold, process use, live socket or unsaved git work keeps it.
On the Mac, any socket in a folder keeps it.
A failed git check also keeps it.
The audit prunes removed linked worktrees from their source repositories.
It records each removal in `~/.local/state/folder-sweep/removed.jsonl`.
`folder_audit.py arm` enables removal, and `disarm` disables it.
Before arming, each selected folder prints `would remove`.
The first dry runs on 2026-10-27 found 106 loose folders on natedev and 13 on the Mac.
They selected 36 and eight folders for removal.

The maker is the earliest-started session whose own command creates the folder.
The audit uses the scratch guard's `commands()` and `targets()` parser and resolves targets against the transcript row's `cwd`.
Read-only commands, command output and message text do not prove ownership.
One ripgrep pass selects possible transcript rows by folder name before the parser checks their commands.
Each proven maker receives one message with its folders, outcomes, `scratch.py adopt` and `scratch.py hold`.
A folder without a proven maker prints `maker unknown` and receives no message.


### Disk-floor alerts (`scripts/lint/sweep.py`, `scripts/buildlog/disk.py`)

Each machine keeps its crossing state in `${LINT_SWEEP_STATE_DIR:-~/.local/state/lint-sweep}/floor.json`.
After a non-dry pass, natedev and the Mac use the same phone-floor crossing path.
A reading below `sweep_phone_floor_gib` opens an episode and sends a message to the natedev session.
The message names the Mac when its disk is low.
A current `sweep.disk_floor` text approval also permits one phone push per episode.
The push names the machine and recommends one measured folder, or asks for measurements when folder sizes are unavailable.
Each channel keeps its own receipt; a failed channel can retry without repeating the accepted delivery.
A reading above the alert line rearms both channels. Equality does not rearm them.
While text approval is stale, the natedev message still sends and the phone receipt stays pending.
The phone channel can send that pending episode after the showrunner records the new approval.

Natedev also records cache sizes, growth and removal accounting. These reports stay outside the Mac path:

- **Floor out of reach.** The sweep reports this state when it selects every removable unit and free space remains below the floor. It uses the `push_history` interval. It never asks the user to free space by hand (2026-10-21). `floor_holders` identifies each busy target, its size, its holding process and its session. `process_uses` finds the process, or `lock_holder` reads `/proc/locks` up to `sweep_proc_locks_reads` times for a held Cargo lock. `holder_sessions` finds the nearest ancestor with a live session record through `process_table.nearest_named`. Each session holding busy targets gets `holder_message_text` through `send_holder_message`. natedev gets `floor_out_of_reach_text`, which lists busy targets and the sessions that received a message. This report sends no phone page.
- **Growth** (free space fell more than `sweep_unexplained_fall_bytes` 10 GiB beyond build-cache growth since a floor sweep at most `sweep_growth_window_seconds` 30 min earlier; this sweep's free is read after the scan that measures its caches (`scanned_free`), and the record's free before its caches walk, so a target another process deletes during either walk cannot read as a fall. On 2026-10-25 a free read from before the scan reported a 122.6 GiB fall while 123 GiB was freed. A target held by a build hold counts 0 bytes, so the growth check runs only when the previous record's `held_roots` equals this pass's held set; an older record without it skips the check. The sweeper records its own last delete per target in `target-deletes.json` beside `floor.json`, and a directory mtime no later than that time plus 2 s does not make a target held. On 2026-10-29 its own deletes made a 34.7 GiB target look written during a hold, and a false 36.4 GiB fall was reported) and **large removal** (more than `sweep_large_removal_bytes` 32 GiB): natedev alone (`send.py --to natedev --from disk_floor`), at most one per `sweep_alert_interval_seconds` (1 h) counted from the last delivered alert.

`send_floor_alert()` delivers the session message; exit 0 or 1 counts as accepted.
`send_phone_push()` delivers the separate phone push; only exit 0 counts as accepted.
Folder choices come from `disk.json` as `AvailableDiskMeasurement | UnavailableDiskMeasurement`.
`buildlog disk` records large directories outside Cargo targets, their growth and their largest child.
It also records `outside_build_cache_totals` and `previous_measured_at`.
`send.py`'s `log.jsonl` lines carry `text`.

### Rust release trial

`rust_release.py` keeps a trial ended by a kill (status 128 or more, -9/-15, or signal 9/15 or SIGKILL/SIGTERM in its output) at `waiting` with reason `<step> killed; retry next night`; a genuine failure stays `failed`.

### `verify.sh` example guard

`verify.sh build [<pkg>] [--bin <name> | --example <name>] [--release] [--features <list>]` builds the selected binaries or example.
`BUILD_TARGETS_PY` reads the package's binaries, or every member's binaries when no package is given.
A named example supplies its required features.
`take_build_args` refuses a missing target or a package without a binary with exit 2.
The Cargo build keeps `--workspace` and the workspace's feature set.
A named package owns feature names without a slash; without a package, feature names pass unchanged.
The build uses the build hold, cargo token, memory admission, and a call record with verb `build`.
`print_built_binaries` prints `binary: <target_directory>/<debug|release>/[examples/]<name>` for each target.
The build saves no pass record and does not use Mac offload.
Use this command for smoke and shot helpers.

A gate run is `test [<pkg>]` without `--filter` or `--test`; `--features` is allowed.
A test without a package uses the workspace figure for memory admission and estimates.
Gate runs and `final` read `cargo metadata --no-deps --format-version 1` once into `GATE_METADATA`.
They run `UNTESTED_EXAMPLES_PY` before the cache lookup and any build.
An example is invalid when a source line matches `^\s*#\[(cfg\(test\)|(\w+::)*test)\]` or its manifest sets `test = true`.
The scan reads every `.rs` file inside a directory example (`examples/<dir>/main.rs`).
Each source violation prints a reason and a correction:

- `example <name> holds a test: <file>:<line> has <attribute>, and examples carry no tests.`
- `move it into <package>'s src/ or tests/, or delete it.`

Each manifest violation prints:

- `example <name> sets test = true in <manifest>, and examples carry no tests.`
- `remove test = true from its [[example]] entry.`

After all violations, the call exits 2 with no Cargo call except `metadata`.
`read_metadata` reuses `GATE_METADATA`.
`verify.sh example-test` exits 2 before any work.
It prints `verify.sh: example-test is removed: examples carry no tests (user, 2026-10-04).`.

### Codex launcher capacity (`scripts/agents/codex_mesh.py`)

Turns end as `TurnCompleted | TurnRefusedForCapacity | TurnFailed`; runs as `RunCompleted | FailedBeforeThread | FailedWithThread | CapacityRetriesExhausted`. Capacity is the error's structured `serverOverloaded` / `flexUnavailable` or the message `Selected model is at capacity`. Capacity resumes the same thread on the same roster entry: waits from `CAPACITY_WAIT_SECS` (30 s) doubling to `CAPACITY_MAX_WAIT_SECS` (300 s) within `CAPACITY_BUDGET_SECS` (1200 s), one worker-log line per wait, and a resume turn saying the last turn stopped for capacity with its edits in the tree. The budget is per busy spell: any turn that ends without a capacity refusal resets it, and an owed resume after such a turn starts at once. Roster status `waiting_capacity` holds the thread id and `launcher_pid`; once a spell's budget runs out it is `capacity_exhausted` and `start` exits 1 with `codex_mesh: <worker>: model still at capacity after <N> retries over <M> min; thread <id> stays on the roster (codex_mesh.py end --to <worker>)`. `send` to `waiting_capacity` stores the message in `<worker>.pending.json` under an flock for the resume turn; `send` to `capacity_exhausted` exits 2; to a finished (`done`/`failed`) worker it exits 1 with `codex_mesh: <worker> is <status>, not running; read its summary file`. The fast-failure retry on a new app-server runs only for `FailedBeforeThread`. Liveness comes from `thread/read`, typed `ThreadLive(turn_id) | ThreadActiveWithoutTurn | ThreadIdle | ThreadStateUnknown(reason)`: `idle`, `notLoaded` and `systemError` (the status the app-server leaves after any failed turn, a capacity refusal included) read `ThreadIdle`, and a status the protocol does not define reads `ThreadStateUnknown`. A relaunch replaces an entry only on `ThreadIdle`, except a `failed` entry, which relaunches after `_end_unwatched_turn(port, thread_id, log_path) -> RelaunchAllowed | RelaunchBlockedByLiveTurn` repeats the dead launcher's cleanup (blocked: exit 2, `thread <id> still has a live turn that could not be interrupted; relaunch once it ends`), and a `waiting_capacity` entry whose launcher is dead, which relaunches without a read. `agent-registry.md` (Addressable codex workers) has the full mechanism.

## Invariants

- Every compiling step and every BRP launch passes the memory gate before it starts. Sweep, `cargo fmt` and `taplo` steps never wait on it, so the disk floor is never held by memory.
- Admission and hold check errors stop the step. Only an explicit admission answer allows it to start; a wait limit grants nothing.
- The disk floor never removes output from a target whose cargo locks a build holds, removes no more than the shortfall plus the last group's overshoot, and prints no new line containing `removed` (`scripts/buildlog/parse.py` counts those lines as freed bytes).
- Build steps skip the Python hold check when the holder directory is missing or the shell confirms a readable, searchable, empty directory. A memory wait never starts a build without memory.
- `BUILDLOG_MEM_WAIT_S` and the gate outcome are set in the calling shell before any `| tee` pipe; a step's recorded start excludes the wait.
- The sccache server stays in `builds.slice`; moving only the steps would leave every compile outside the limit.
- Raising a step's oom_score_adj never stops or alters it: a failed write is silent, the `|| true` keeps the scope shell running when a caller exports `SHELLOPTS=errexit`, and exit status and output pass through.
- A memory kill is never recorded or cached as the tree's lint or test failure; it re-runs once and stops at the second.
- A cargo token is reclaimed early only through a `holder_pid` that names a gone process; holder and waiter share one pid namespace. The guard file is never removed.
- Holder files supply current hold state. Old cycles distinguish files still in force from completed holds. A release removes its holder file at once. Historical log rows never decide whether a build can start.
- Releases go by session id, never by name. A session that cannot be messaged is never recorded as a recipient.
- Hold changes use `release.lock`. Release delivery uses the ended cycle after the holder file is gone. A damaged record is set aside by a changing command, never by `status` or the dailies.
- `steps.mem_wait_s` is the memory wait. `steps.wait_s` is the call's lookup and token wait. The file name hashes the schema SQL. Bump `SCHEMA_VERSION` only when the way rows are derived changes.
- Only `ran` CI jobs carry duration and queue time. CI days key on the run attempt's `started_at` everywhere.
- The window report is host-scoped; the daily report aggregates hosts. A run's own peak comes from `ci_anon_bytes` / `builds_anon_bytes` samples, never from `memory.peak`.
- Only a phone-floor crossing reaches the phone, with current text approval; diagnostics go to natedev.
- Tests never read the real cgroup tree, `/proc/meminfo`, `/proc/pressure/memory`, journal or transcripts, and never write `~/.local/state/buildlog`, `build-hold`, `build-hold-release`, `notify` or the agent notes: they take `BUILDLOG_MEMINFO` and `BUILDLOG_MEMORY_PRESSURE` from `scripts/lint/memory_test_support.py`, set `BUILDLOG_*_CGROUP`, `BUILDLOG_TRANSCRIPTS`, `BUILD_HOLD_DIR`, `BUILD_HOLD_RELEASE_DIR`, `HOME`, and stub `journalctl`, `zramctl`, cargo and git on `PATH`. Tests set the hold variables for a checkout's scripts; installed scripts refuse to run with them set.
- When `BUILDLOG_LIVE_MEMORY_TRIPWIRE` names a record file, the shell gate and Python checker refuse a path that resolves to `/proc/meminfo` or `/proc/pressure/memory` before reading it, append a JSON record, and name the fake readings the test must set. The merge gate sets this variable for every test module and fails any module with a recorded live reading. The default-path test removes the tripwire but supplies fake pressure text. A failed record write still refuses the reading but leaves no record, so the gate detects only recorded readings.
- A verify.sh routing test follows `scripts/direct/test_verify_untested_examples.py`: cargo passes `metadata` to real cargo and logs every other call, git is stubbed, and the environment drops `UNIT_RUN_DIR`/`UNIT_TEAM_ROLE` and sets `BUILDLOG_OFF=1`, `BUILDLOG_SCOPE=0`, `BUILDLOG_MEMINFO`, `BUILD_HOLD_DIR` and `CARGO_TARGET_DIR`. Tests set `BUILD_HOLD_DIR` for a checkout's scripts; installed scripts refuse to run with it set.
- Times shown are in the user's zone through `user_zone()`; stored instants are UTC. An `HH:MM` release ETA names the zone in which it was supplied.

## Calibration and gotchas

- **Unfaked memory pressure.** On 2026-10-29, `test_brp_launch_gate.py` took 169 s against a 6 s median because its pressure reading was unfaked.

- **Memory gate sizes.** The first need comes from `program_first_peak_gib.cargo` in `config/build_admission.conf`. Build steps use shape tiers. Other steps retain measured, build-log, and pooled tiers. The service reads process memory before host memory during each pass.
- **One admitter after a hold release.** The service reads memory, decides, and persists grants in its loop. Each step retains its accepted need unless an observed test phase selects its frozen test-phase need. Swap-backed use reduces its keep-aside. Simultaneous callers cannot reserve the same room.
- **Release outcomes vanish with the last holder file**; set-aside `*.damaged-*` files are never cleaned up. A holder joining a no-cycle hold learns nothing until the final release line tells it to broadcast.
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
- **Disk floor order.** A no-op cargo build writes nothing under `target/`, and relatime refreshes atimes at most once a day, so only the use stamp records a step that compiled nothing. The stamp lags a lint step by up to 5 minutes (`invoke.sh`'s sweep rate limit), which only reorders targets used within the same 5 minutes. The build log's `sweep_freed_bytes` counts each workspace's budget sweep and the floor sweep after it together, so it is not the floor's cost alone; the journal's per-target lines are.
- **procps cuts user names** over 8 characters unless the format says `user:32`.
- **Codex.** On an idle thread the app-server opens a turn per queued message, so never `thread/queue/add` to a thread no launcher streams. `thread/read` can report active with no turn id; get the id from a steer probe before `turn/interrupt`. `turn/interrupt` errors when the turn ended in between; only a re-read tells that from a live turn.
- **Dailies.** The run-out time rounds to the minute in epoch seconds (rounding aware local time loses the DST fold). `resets` is the writing machine's wall time with its offset (2026-10-12: notes travel between machines in hanadocs, and an offset-free value read on the other machine was off by the zones' difference); `note_file.reset_time` reads it for `agent_notes.still_ahead`, `quota_alert.ahead` and its two account checks, and `codex_pacer.parse_local`, taking a value written before the offset as the reading machine's wall time. A naive `limit_reset` is the writing machine's wall time. Every subprocess dailies test sets `HOME` to a temporary directory.
- **`verify.sh` guard.** A failing `cargo metadata` exits with cargo's status under `set -e`, not 2.
- **Live scripts.** Units run `~/.claude/scripts/direct/board.sh` and `verify.sh` while they change, and bash reads a script as it runs: replace either by writing a temp file beside it, setting its mode, and `mv`ing it over.
- **A target with neither marker.** A folder made before a build that runs with `CARGO_CACHE_RUSTC_INFO=0` has no `.rustc_info.json` and no `CACHEDIR.TAG`; the floor and the disk measurement do not see it. Nothing on natedev sets that variable.
- **Cargo's tag is not only on targets.** `~/.cargo/registry` and `~/.cargo/git` carry the same `CACHEDIR.TAG` text, and other tools write the file with their own text; the tag counts only with cargo's line and a build tree beside it.
- **basedpyright** exits 0 when clean. Its summary reads `0 errors, 0 warnings, 0 notes`.

## Why

- **Admission by free memory, not by slice limit.** `memory.high` counts page cache and throttled builds that were not short of memory; `MemAvailable` is what earlyoom acts on. 12 GiB matches the floor steve already uses for slots, and a step's expected need replaces it once history exists, so parallel large builds no longer start together into memory only one of them fits in.
- **Target first below the disk floor.** Ordering every idle build unit by compile age spread each shortfall across the targets in active use, which rebuilt the output minutes later. One worktree's target is what a unit owns, so the least recently used target goes first and a target used minutes ago goes last. A forecast mode was ruled out: every build goes ahead, and the journal names whose cache went.
- **Scratch first below the disk floor.** A scratch target belongs to no worktree and is rebuilt only if its session is still working, so it goes before output a unit will need again. On 2026-10-08 a 140 GiB scratch target outlived managed targets idle for hours because it was used more recently, and a scratch target in its first build reached 22 GiB unseen, listed by the alert as growth outside build caches. No floor hook runs inside scratch builds: the 2-minute timer covers them, at about 2 GiB a minute against 300 GiB of headroom. A structural target rule (a child folder holding `.fingerprint` and a cargo lock) was rejected: one listing per walked folder for the no-marker case nothing here produces. Orphaned files still go first from every idle target, because losing one costs nobody a rebuild.
- **One slice for every session build, sccache included.** Every compile reaches the sccache server, so the server's cgroup is where compiles are charged; a limit on the step scopes alone would bound nothing.
- **CI in its own pool, a smaller slice and a lower kill order.** Shared steve slots alone did not stop kills (a CI run lost both Linux jobs after it). Its own 14-slot pool keeps CI from starving sessions and the reverse; `PIPELINE_JOB_OOMSCOREADJ` is the only setting that reaches job processes, because the runner sets every job to 500 itself. At 100 a CI job goes after a build step (500) or a session process (200) of the same size.
- **Build steps at 500, and no `--prefer`.** The level sets the order and size decides within it. A `--prefer` for compiler names (+300) let two hung hana tests (11.4 GB, 15.5 GB) outlive 26 smaller processes, six CI compilers among them; without it, a hung 15 GB test scores about 1100 against a 2 GB CI compiler's 750, and a session's rust-analyzer at 200 stays behind every build step of similar size. The adj is set inside the step's scope, so every process the step starts inherits it.
- **Admission after a hold release.** The 2026-10-04 incident had 21 kills after compilers grew beyond the admission readings. Each admitted step keeps its selected need. Its swap-backed use reduces its keep-aside. Every local build requests memory admission after a hold release.
- **A damaged record releases everyone, not no one.** Setting it aside and falling back to a broadcast cannot strand held sessions; each still passes its own memory gate.
- **Broadcast by id.** Names can belong to two sessions, and `top_level.py` once deduped them, so one of two same-named sessions never heard the hold.
- **A memory kill re-runs once.** The kill says nothing about the tree; a second kill stops the call so a starved machine does not loop.
- **zram over zswap.** A cache in front of a full swapfile adds no capacity; zram adds compressed space that does not depend on the swapfile.
- **Every average gets a p95.** An average hides the long runs that make waits; nearest rank needs no interpolation and matches the old p75 index for every n.
- **Two build-folder rows.** A worker's own verify.sh calls run one at a time, so its wait behind its own call costs nothing; only the wait behind another worker of the same worker session is delay.
- **Ruled out** (do not re-propose without new evidence):
  - systemd-oomd for builds: it kills a whole cgroup, and every session's compiles share one.
  - Admission by each crate's measured peak, or a per-crate gate: no source records the peak, and no isolated run met the threshold.
  - A nextest thread cap or a per-run memory cap in `verify.sh`: no isolated run met the threshold, and the widget drawn-pixel test group already bounds the largest step.
  - A `MemoryHigh` gate or `MemoryHigh` on either slice (see gotchas).
  - A `CARGO_BUILD_JOBS` fallback on the runners: the pool's jobserver reaches CI jobs.
  - A `steve-clients` group: the ACL already grants the runner users, and group membership is fixed at login.
  - Catching `AttributeError` instead of validating the cycle shape: it would hide code defects too. `release --resume` releasing the sole holder of any no-cycle hold: it could end a hold nobody had begun releasing.
  - A 10-minute snapshot search: an edge snapshot more than 2 minutes off misplaces the window. A per-call kill count alone: it cannot tell two single kills from one step killed twice.
  - Keying `ci_section` by job `created_at`: drops post-midnight jobs of a run started before midnight.
  - Counting build-folder wait that no same-session holder covers as the call's own: it would hide waits with no recorded holder.
  - A hook on `brp_launch` to record launches, or recording from inside bevy_brp_mcp: the collector reads transcripts instead. Appending launches to the host's monthly file: the per-launch file makes the record id the dedupe.
  - Keying the example guard on the target's `test` setting or advising `test = true`: examples carry no tests (user, 2026-10-04).
  - Withholding the phone push when a removal failed, or cutting a path's last component to fit 1,024 characters.
  - Queuing a message to an out-of-retries Codex thread (it opens a turn nothing streams); changing the service tier on capacity.
  - The dailies renderer importing the note code from `scripts/whoami`: it parses the flat frontmatter itself and imports only `run_out`.

## Not built

`verify.sh ... --target-dir <name>` (a per-helper build folder with its own cargo token, pass records shared across folders, `calls.target_dir`, schema 10) is parked. It returns when the report's `Build-folder turn, behind another worker` row shows real time queueing for a worktree's build slot.

A second promise rule is measured and declined (showrunner, 2026-10-28: simple wins). The rule: a running build whose need is above a limit keeps its full promise; the running builds at or under it are promised the largest gap in full plus `max(0, 0.6 x need - anon)` each. The replay used 4 h of 5 s admission samples (2026-10-28 03:27-07:27Z, 240 waits, 1863 wait minutes), and "short" counts admitting ticks where the running builds' real growth after the tick passes `MemAvailable` minus the reserve. With a 6 GiB limit the rule saves 331 of 1863 minutes against 249 for the current rule, with the short rate at 0.8% against 0% (5 GiB: 326 min, 0.9%; 8 GiB: 501 min, 1.4%). The same rule limited by the waiting step's need never gets below 6.9% short. At about 4% of the wait for a second rule and a nonzero short rate, it is not built; a shortfall is the 2026-09-30 freeze. Do not measure it again unless the mix of steps changes.

A scoped reservation retains its accepted need while its scope contains a process.
Admission and queue views use the same lifetime rule.
The service retains `.max` and `.wave` until the owner and scope both end.

A test-width update persists the reservation in `state.json` through `os.replace`. A stopped write leaves the previous state intact.

A cleanup plan is written to a temporary file and linked into place only when no plan exists; a rerun over an incomplete older plan names the file and asks the caller to inspect and remove it before making a new plan.

Python queue layouts, evidence tables and production dailies share `scripts/lib/terminal_cells.py::terminal_width`: combining marks occupy zero cells and wide characters two.

**One decision pass.** `admission_pass(inputs, now, limits, policy)` returns decisions, blockers, proposals, the head cause, run keep-asides, and start spacing.
The service commits proposals and stores the pass.
It encodes View and Explain replies on the first request after each pass.
It keeps those bytes until the next pass ends.
Mine keeps the full reading and reuses the View encoding.
The protocol encodes each reply tree once.
A pending configuration still causes a pass before a query reply.
One reader thread collects process, scope, pressure, and memory evidence.
The reader publishes physical memory and sampler observations under a lock and wakes the service loop.
Each reading contains the monotonic time when its read started.
The loop applies the current peak hold and frozen test-phase need at each pass.
The reader applies no reservation policy.
The loop combines the latest reading with its current reservations and waiters.
A reading can be one pass cadence old.
The reader uses `waiting_pass_seconds` while steps wait and `running_pass_seconds` otherwise.
A change from the running cadence to the waiting cadence wakes the reader.
The service collects the first reading before it starts the serving loop.
The reader starts when an unusable configuration becomes usable.
An unknown owner requests an early reading.
A deferred request uses only a reading that starts at or after the request arrives.
An older reading leaves the request deferred.
The reader sends exceptions to the loop and continues at the configured cadence.
Direct test instances can supply a fixed reader.
Persistence reuses each unchanged record's encoding.
The state bytes, atomic writes, and lifecycle commit order stay the same.
`query_timeout_seconds` stays 0.5.
`QueueExplanation.decided_from` keeps the inputs for release calculations. Those calculations admit nothing and write nothing.
`apply_queue_priority` supplies the priority order. The generic codec stores domain records for replies and service state.

Admission callers retain one hash of their loaded admission sources.
`admission_code.py` owns `ADMISSION_CODE_PATHS` and `admission_code_version()` and includes itself in the hash.
The shell runs this module directly with isolated Python; the module imports only standard-library modules.
Requests carry `code_version`; the service records it by process identity in `ServiceState.caller_versions`.
Persistence removes dead caller identities, including reused PIDs, and retains live callers without reservations.
The shell retains its version in `BUILDLOG_ADMISSION_CODE_VERSION` before admission starts.
The drain check requires matching entry content and a matching recorded version; an unknown version prevents a completed drain.

### Priority and token waits after the service switch

The service owns the durable admission state and the socket.
`RankedWaiter` adds `PriorityUnit` or `NormalOrder` to a resolved row through `rank_waiter`.
A durable waiter has no `priority_rank` or resolved `order` field.
The order is priority, the step's original order time, then pid.
Token waiters keep their clocks and places but cannot protect room as the head.
`TokenWaitersOnly` distinguishes a token-only queue from an empty queue.
The renderers use the eligible head's position and token history.

Priority accepts real roster units, including units without a waiting step.
The no-unit display placeholder is not a unit.
Unassigned sessions retain `NormalOrder`, including with an old placeholder priority record.
The pane passes its session id explicitly.
Unknown units return exit 3 with a stdout line starting `build queue: `.
Cards show `N of <unit> (queue priority)` once and use the saved reason.
Legacy move rows do not replace that reason.

`SettledPriority` and `PendingPriorityTransition` name the priority publication states.
A pending transition saves its log offset before the service publishes state.
Retries retain the transition id and read only the saved log tail.
The service imports old files once.
A failed cleanup logs its path and retains its record.
Each later finalization attempt uses the same guard, so other admissions continue.

### Mac disk floor

`sweep.py --floor-only --host mac` uses the existing floor scan and deletion order.
It reads `mac_test.conf` once for `sweep_floor_gib` and `sweep_floor_roots`.
The floor is 150 GiB. A missing floor or a floor below `free_floor_gib` refuses the pass.
The roots are `/tmp`, `~/rust`, and `~/.local/state/mac-test/mirror`.
Only targets with our use stamp, mirror mark, or a registered copy ancestor are eligible.
Resolved paths must stay inside these roots. The scan does not follow directory symlinks.

The sweep reads the local and natedev Mac claims under their existing admission locks.
It keeps these locks until deletion ends. A live or kept claim protects its mirror.
An unreadable claim reply or a lost lock process protects every target.
The Mac uses `lsof` for other process use. Unreadable process use protects every target.
The sweep checks protection again before each target deletion.
It takes only the shortfall, in the existing least recently used order.
The result line reports removed bytes, free bytes before and after, and skipped busy targets.
`--dry-run` lists proposed takes and removes nothing. It does not run the general tmp sweep.
The nix-darwin `nate.jobs.disk-floor` job runs this same command every 120 seconds on the Mac.
`modules/darwin/default.nix` imports the new `modules/darwin/disk-floor.nix` module.
The job uses the mirror leases and claim locks above, plus the shared floor lock.
It trims only the shortfall below `sweep_floor_gib`, without an offload request.
`ensure_mac_disk_floor()` keeps its 100 GiB on-demand trigger.

The launchd job PATH includes `/usr/sbin` after `/usr/bin:/bin`.
This lets the floor, scratch and folder-audit jobs find the system `lsof`.
The floor job writes both output streams to `~/Library/Logs/nate-jobs/disk-floor.log`.
The nixos patch must be applied and nix-darwin activated before this job runs.
The Mac floor finalizer uses the shared crossing path above and saves its receipts on the Mac.
Its session message goes to natedev; its phone title and body name `Mac`.


### Free memory, head protection, and step slots

`memory_room` calculates room from one captured admission pass.
For each admitted run, kept bytes equal `max(0, selected need − swap-backed use)` plus held slot bytes.
Room equals free memory minus `safety_gib`, run keep-asides, and the CI keep-aside. The safety is 3 GiB.
The cap equals our swap-backed use plus CI use plus free memory minus safety. It does not limit an estimate.
The first two minutes use the same formula. Use and kept bytes never count the need twice.

The first eligible run follows queue priority, first run arrival, and PID.
A younger run starts only if its need fits room minus the first run's need.
Pressure or unreadable pressure holds new starts and extra memory only while it lasts.
An unreadable pressure file shows `pressure unreadable: <path>` with a short error or `incomplete` when available.
The client, card, and queue explanation retain that reason. The queue marker says `pressure unreadable`.
An unreadable host memory reading retains its recorded reason too.
If an unreadable reading holds queued starts for `reading_alert_seconds`, currently 300 seconds, the service sends one message to `natedev`.
The message names the path and the hold time. `natedev` owns machine recovery and any later phone alert.
The service measures the hold with its clock. A daemon thread sends the message without delaying admission.
The sender reuses `scripts.message.session_send.session_send_argv` with sender `build-admission` and sends the text through stdin.
The `build-admission-reading` claim prevents repeated delivery during the same unreadable stretch.
Failed delivery releases the claim for retry. Restored readings clear the claim.
The first run can start without sufficient room when no admission or held slot remains.
It still needs readable host memory, clear pressure, a current estimate, and due spacing.
CI and ownerless scopes do not count as admitted runs for this exception.

Each new start sets the next start time through `start_spacing_after`.
The interval is `recovery_start_seconds`, currently 5 seconds, for launches and build steps alike.
A completed step leaves its run a slot for `slot_gap_seconds`, currently 10 seconds.
A continuation within the slot bypasses order, spacing, and pressure.
A larger continuation holds the slot and requests only its extra memory before new runs.
A closed connection frees its claim. An idle slot expires or ends with its verified run owner.
Repeated release cannot recreate a slot. A standalone request has no reusable slot.
Slots are not saved. Restart keeps active reservations and run order, but drops handoffs and waiting extras.

`queue_evidence.room_text` supplies the captured cap calculation to the view, card, and memory report.
The view shows zero room during a shortfall and records that shortfall in the cap explanation.
The queue view and cards show captured process ids as `pid N` for each process row.
Running rows show a `machine` column after `min` and before `pid`.
The column uses `scripts.buildlog.store.host_name()`, the shared short host name.
A narrow view omits the machine column before it reduces name space.
Waiting rows have no machine value. Services lines do not change.
The admission service admits each running step on its own machine. No per-row protocol field is added.
`keep_aside_text` explains each admitted run's need, swap-backed use, slot bytes, and kept bytes.
`RoomCheck.basis` records `Fits`, `StartsAlone`, `WithinSlot`, `OverrideRoom`, or `DoesNotFit` in the admission walk.
`admission_pass` records one `HeadCause` on `HeadAdmissionStatus`.
`HeadExplanation` retains that cause for the view and release advice.
The formatters read the captured cause and room without a second room calculation.
No adaptive controller, fixed admission limit, order window, CI growth promise, or test-phase estimate worker remains.

### Progress memory profiles

Merge gates start with the p90 of their host's recorded peaks from the last 14 days, as other programs do.
Without history, they use `program_first_peak_gib.merge-gate` from `config/build_admission.conf`.
Matching progress profiles still determine the running need; `progress_profile_rank` applies only to progress bins.

Cargo build, check, clippy, mend, nextest, and merge gates record attributed swap-backed use in five-percent phase bins.
Each bin selects the Nth-highest run value from matching completed profiles.
`progress_profile_rank=2` sets N in `config/build_admission.conf`.
A bin with fewer than N measurements uses its lowest measured run value.
Rank 1 selects the highest run value.
The service validates the rank through its existing configuration check before each pass.
It rebuilds merged profiles when the rank changes.
The service applies each step's recorded headroom rule to the highest selected bin within its look-ahead window.
`progress_lookahead_percent` is read at use; its initial value is 20.
The window crosses Cargo phase boundaries, including repeated build phases.
The merge gate window stops at the end of `suites:1`, even when history contains rerun phases.
Reruns without a measured window retain the full need. Each step has its own history key.
Missing counters, profiles, or anonymous readings retain the full need.
Each saved record names its peak reading source with `ScopeReading` or `ProcessTreeReading`.
`ScopeReading` stores the scope directory. `ProcessTreeReading` stores the process ids.
At each new run peak, the service records the phase, bin, anonymous bytes, and source processes.
Each `ProfileProcess` stores its pid, command arguments, and `RssAnon` bytes.
Scope evidence includes all recorded scope members, including helpers outside the original process tree.
The service reads process commands and `RssAnon` only for a new highest run value.
Lower samples, equal samples, and lower bin peaks retain the previous process evidence.
Active records retain this evidence across service restarts and completion.
Old records remain readable with `ReadingSourceNotRecorded` and `PeakProcessesNotRecorded`.
Unavailable process commands and anonymous readings have named states with their read errors.
The nextest test phase keeps its frozen baseline and margin as the fallback. Its live test peak remains a floor.
CI keeps its configured margin until registered CI steps supply counters and profiles.
`lint/invoke.sh` enables Cargo progress in its capture log. It removes redraw frames from worker output.
Cargo-tile enables progress for its own captures. The cargo wrapper does not enable progress for other pipes.


### CI cargo admission

The pinned client entry is `scripts/build_queue/ci_cargo_front.py`.
The runner package exposes this entry as `bin/cargo` from the pinned source tree.
The package wrapper passes `--front "$0"` before the cargo arguments.
Its PATH entry precedes the cargo-tile shim, which precedes the real cargo.
The front excludes that wrapper and removes its directory from the child's PATH.
An explicit `--cargo <shim> -- <arguments>` entry also selects the next shim.
Building calls hold one reservation until the child and its descendants end.
Non-building calls execute the next cargo without admission.
A direct call to a cargo executable outside this front bypasses admission.
Calls inside the admitted child tree retain the same reservation.

`ServiceEstimateRequest` carries the owner, repository, worktree, arguments, runner, and job.
The service reads configuration and history with its own account.
It uses `step_estimator.step_estimate` with a recorded repository location.
The service refreshes the estimate on each acquisition retry.
Build estimates use the current service history generation.
CI uses `NoRun`, so it claims no run place or continuation slot.
The configured `ci_label` expands `RUNNER_NAME` and `GITHUB_JOB`.
The configuration reader validates the template before the service uses it.
An invalid template uses a committed value through the normal configuration reader.
Without a usable committed value, configuration resolution fails as for other invalid values.
The queue records this label as the session name.

`SO_PEERCRED` supplies the connecting account before replay lookup.
Authorization also precedes caller version records and saved state changes.
Runner request ids start with `uid:<account>:`.
The service checks the owner's account and process start time.
It also checks a caller-named run's account and process start time.
Runner accounts cannot change priority or admit the head.
Cleanup and width changes require a reservation from the same account.
The account prefix also binds replay and pending priority access.
Saved reservation and ledger records retain their existing shapes.
The service wraps saved grants as `RunnerAdmitted` only when it sends them.

The runner owns its child process tree; it creates no user-manager scope.
The front adopts orphaned descendants before it starts cargo.
The cargo child sets a parent death signal before exec.
SIGKILL of the front also kills that cargo process.
Cargo descendants do not inherit that signal and can survive a SIGKILL of the front.
`QueuePlace` tracks descendants and terminates surviving descendants before release.
The runner samples swap-backed use in its process tree.
The service also samples swap-backed use during each admission pass.
The service reads the runner's peak and wave files beside its sidecar.
It copies the maximum into its own peak files before finalization.
The service writes completed and failed peaks through the normal history path.
The runner creates its sidecar and progress counter in a temporary directory.
The directory mode is `0755`; the sidecar, counter, peak, and wave modes are `0644`.
The runner account owns these files; the service reads them without group write access.
The service completes release when sample deletion fails with `PermissionError`.
The front removes its temporary directory after release.
A front killed by SIGKILL leaves that directory for the existing sweep.
The service reads the counter through the sidecar path in the reservation.
The child alone receives `CARGO_TERM_PROGRESS_WHEN=always` and `CARGO_TERM_PROGRESS_WIDTH=80`.
The shared progress reader and profile history apply the existing look-ahead rule.
A missing counter or profile retains the full need.

Acquisition uses named protocol timing without a local configuration read.
The deadline covers closed connections and connections without replies.
A valid reply renews the deadline.
Cleanup uses the separate timing from the grant.

CI memory keeps the mixed accounting rule during activation.
Registered process trees count through their reservations.
The CI slice reading subtracts their swap-backed use.
The CI keep-aside remains while any busy runner has no registered tree.
The keep-aside applies once to a mix of registered and unregistered jobs.
`ci_memory_margin_gib` remains in configuration until the approved activation step.

NixOS activation remains natedev's work.
The socket uses group `build-admission` and mode `0660`.
The group includes natepiano and the hana runner accounts.
Runner sandboxes need `BindPaths=/run/build-admission` when `/run` is hidden.
`ADMISSION_SOCKET` is needed only for a nondefault socket path.
The package must contain the pinned source tree and a Python interpreter.
No client code is copied into `/etc/nixos`.
The service must read runner `/proc` records and runner temporary counter files.
Set `TMPDIR` to a shared runner-owned directory if `PrivateTmp` hides the default directory.
For example, `/run/build-admission/ci/<uid>` uses mode `0755` and the runner's ownership.
The fixed slice limits remain `MemoryMax=34G` and `MemorySwapMax=2G`.
The service authorization and protocol must be live before runner socket access starts.
