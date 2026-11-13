# builds-queue

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The user approved the queue service design on 2026-10-30 (`93c86342`); Phases 4, 5 and 10 come from it. `/build:queue` answers in under a second with one line per step; then one queue service process holds the queue, each build asks it over a socket and waits, and the polling admission goes.

> **Renumbered 2026-10-30:** Phase 2 (the approved queue view) was put in after Phase 1b; the old Phases 1c, 2, 3 and 4 are now Phases 3, 4, 5 and 6.

> **As-built disposition: amend** — `docs/as-built/unit-board.md` (the queue pane); `docs/as-built/build-memory-admission.md` (snapshot, `queue_read`, `LineWaiter` checks, `match_waiters`; later the service); `docs/as-built/build-targets.md` (the `/build:queue` row)


## Source

2026-10-30 11:26 EDT

add a queue unit - it should own all existing queue scripts and skills - transfer any current work on report unit to queue unit that is queue related - queue can keep it running - and it should task an agent to start the design and the migration plan

2026-10-30 (the user, to the showrunner; relayed verbatim 11:4x EDT)

why does every build run the queue code - why don't we have a system where there is a queue that guards and releases?

should we use rust or python?

2026-10-30 ~11:25 EDT (the showrunner, relaying the user; its answer to rust or python: Python, for one memory model and no tool build)

The design: the user asked why every build runs the queue code, and wants a queue service that guards and releases. One Python process holds the queue. Each build asks it over a socket and waits. It admits in queue order, frees memory when a build ends, and serves override, priority and the view as commands. It needs a rule for when the service is down. Python, not Rust: one memory model, no tool build. Have an agent write the design and the migration plan from today's polling admission. The user reviews the design before any code.

2026-10-30 (the user, to report-unit, carried over with its Phase 4g)

build queue is unusable for me ... i can't parse this ... it is acceptable to be wider ... be more creative ... it is TOO slow

i need instantaneous answers on builds ALWAYS ... i cannot get clarity

i need to get sub second responses to any questions like these

only I can bump

## Delegation Context

- **Project:** `~/.claude` — the build queue: memory admission, the `/build:queue` view and evidence cards, and the unit-board queue pane.
- **Project started:** 2026-10-30T11:26:00-04:00
- **Stack:** Python 3.13 (basedpyright strict, unittest); TypeScript mod under `mods/unit-board/` (its own tests in `mods/unit-board/tests/`).
- **Layout:** `scripts/build_queue/` (`build_queue.py`, `queue_evidence.py`, `head_facts.py` from report-unit 4e); `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py`; `scripts/message/roster.py`; `mods/unit-board/hooks/` and `mods/unit-board/tests/`; `commands/build/queue.md`.
- **Key files:** `scripts/lint/memory_admit.py` — admission decisions, the queue rows and `explain_queue`; `scripts/build_queue/build_queue.py` — the queue view and the bump CLI; `scripts/build_queue/queue_evidence.py` — recorded need, charge, and release text; `scripts/build_queue/head_facts.py` — bump rows, head time, token waits; `mods/unit-board/hooks/register.tsx` — `tablePane()` and the `build:queue` command; `mods/unit-board/hooks/queue.tsx` — the pane view; `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_client.py` — the queue service, its socket protocol and its client (new in Phase 5).
- **Test lanes:** `scripts/build_queue/test_*.py`, `scripts/lint/test_memory_admit.py`, `mods/unit-board/tests/queue.test.ts`.
- **Test:** `env -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY python3 -m unittest <module>` from the worktree root; the mod's test command as `docs/as-built/unit-board.md` names it.
- **Lint:** `basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <the slot's own Python files>`; footer must read `0 errors, 0 warnings, 0 notes`.
- **Ownership (showrunner, 2026-10-30):** queue-unit owns `scripts/build_queue/` and `commands/build/queue.md` (from report-unit), and `scripts/lint/memory_admit.py` with `scripts/lint/test_memory_admit.py` (from resources-unit), and `scripts/lint/memory_gate.sh` (from resources-unit, with Phase 7; builds showrunner, 2026-10-31). queue-unit also owns resources Phases 15 and 27, now Phases 8 and 9 of this plan: one owner for admission changes in `memory_admit.py` (builds showrunner, 2026-10-31). resources-unit keeps its other in-flight `memory_admit.py` phases until they land (admit-head, queue order, `/showrunner:bump` priority, strict-window cap, 24B); until then each `memory_admit.py` edit is coordinated with resources-unit through berth, and resources-unit reviews the diff. `scripts/message/roster.py` is shutdown-unit's (natedev): moving `unit_labels_for` and its file-only helpers to the new `scripts/message/unit_labels.py` is approved (showrunner, 2026-10-30), and the notice carries an also-touches line. `mods/unit-board/` is natedev unit-board-unit's; the build queue parts of `register.tsx` change under an also-touches line.
- **Resources Phase 26 is built (58391add, on `origin/builds` at e2773be6, in this branch at d309bbed):** the queue order comes from `priority_rank` and the user's bump sets a priority in `memory_admit.py`; `bump_order`, `holds_younger` and `bump_waiter` are gone, and `priority_since` stays. Phase 4 carries its fields (Phase 4d). Phase 10 carries Phase 26's state into the queue service (Phase 10's Decided block, 2026-10-31).
- **Cut over, no shadow (user, 2026-10-31):** "we should cutover and not try to run the old and the new at the same time." Phase 5 builds the queue service's new files, and no build uses them. Phase 10 makes the service live in one merge; nothing runs the service beside the old checker.
- **Stored records (user, 2026-11-05):** main code keeps no reader of an old record shape. A phase that changes a stored record writes a conversion program outside the repository, proves it with a dry run, and names the deploy steps in its notice: stop the service, convert, land, start the service. The program is deleted after the deploy. Only the user can stop and start the service (root).
- **Invariants:** admission rules live once, in `memory_admit.py`; a view prints admission's answer and repeats no budget or free arithmetic. The user's bump is a queue priority (resources Phase 26): only a showrunner sets or clears the one `QueuePriority` (`priority_set`, `priority_clear`), and the queue order is `waiter_order` = `(priority_rank, since, pid)`; nothing else moves a waiting step. Every answer about the queue comes in under 1 s.

## Queue service design — the user reviews it before any code

- **Who writes it:** a queue-unit design agent, started 2026-10-30 11:3x EDT, read-only; it writes `docs/plans/builds-queue-service-design.md`.
- **What it settles:** today's measured cost of polling admission; the service (what it holds, persists, how it starts and restarts); the socket protocol; admission rules kept in one place; the commands (view, the user's override, `/showrunner:bump` priority, width, release, build log rows); the rule when the service is down; performance targets; tests; and a phased migration from today's polling admission, ordered around resources-unit's in-flight phases and Phase 1.
- **Gate:** queue-unit reviews the draft, sends it to the showrunner, and the showrunner puts it before the user. No service code until the user approves.
- **Approved:** the user, 2026-10-30, at `93c86342` ("yes i want the build queue", relayed by the builds showrunner).
- **After approval:** the design's migration phases become Phases 4+ of this plan through `/plan:to_phased_plan`, each with a Work Order, and go to the showrunner for approval before they run.

## Phases

### Phase 10r — The CI card shows the running job, not runner uptime  · status: done
**Depends on:** none (queue card and view only).

1. **Ask (builds showrunner, 2026-11-04, a defect the user saw).** The `/build:queue` card for `hana-ci.slice` says "Started 41 h 27 min ago". That is the uptime of the runner services (`github-runner-hana-linux-1` and `-2`), not a CI job, so it reads as a stuck job. This is a wrong result, so it goes first.
2. **Change.** Show whether a CI job runs now and that job's age. With no job, show "idle, runners up 41 h".
3. **Swap is not RAM (builds ruling, 2026-11-04).** Measured 10:12 EST: the queue showed `hana-ci.slice` at 2.1 GiB with no CI job, while `memory.current` was 0.09 GiB and `memory.swap.current` was 1.98 GiB. The admission charge and the queue view use resident memory (`memory.current`) only, for every slice, never `memory.swap.current`. A card that shows swap shows it as its own labelled number, never in Using or in the charge.
   - **Service rows (user, 2026-11-04).** A service slice (hana CI, compiler cache) always has its own row with its RAM use, at any size. An idle service shows "idle" and no age. A running CI job shows the age of the job, never the uptime of the runners.
4. **Proof.** A fixture with a running job shows the job age. A fixture with only the runner services shows the idle line. A fixture slice with swap shows a charge equal to `memory.current`, and the swap only on its own label. A 0.1 GiB idle service fixture keeps its own row, with no age.

### Phase 10t — The build budget follows measured use  · status: superseded by 10z (not landed)

**Depends on:** 10r (both change `scripts/lint/memory_admit.py`; 10t's code starts when 10r is checkpointed; its design runs beside 10r).

1. **The ask (user, 2026-11-04, via report-unit and builds).** "The memory budget should be dynamic. Measure promised vs used and free memory over the last 2 hours; raise the budget when there is no memory pressure, and lower it back to a floor when pressure rises." Reason: steps with different reservations start and use memory at different times, so the sum of reservations overstates real use.
2. **Evidence.** The d3ce51b2e bump gate waited 6 min 32 s for memory while 22-31 GiB was free. The queue showed 10.5-16.5 GiB promised against 4.2-8.1 GiB measured across 29-46 running steps. gate_runs: 10 runs peaked at 4.1-4.7 GiB against a 6 GiB reservation. The live page showed merge gates using 0.1-1.7 GiB with 4.3-5.9 GiB reserved each, room 0 and 3-4 steps waiting.
3. **The change.** The budget is a value the queue computes, not a fixed number. Today's `build_budget_gib` becomes the floor. Over a rolling window, the queue reads promised, used and free memory and the pressure signal. With no pressure in the window, it raises the budget one step at a time, up to a ceiling. When pressure rises, it drops the budget back to the floor at once. The free memory check and the safety floor stay as they are.
4. **Config.** Every tunable lives in `config/build_admission.conf`, read at use: the window, the floor, the ceiling, the raise step and interval, the lower rule and the pressure signal. Each new key goes to builds with its value, what it did before, and the other choices, for the user.
5. **Shown.** The queue view, the card and the memory page show the current budget, the floor, and why it moved last.
6. **Proof.** A replay of the last 2 days of queue history: the wait time saved, and no step OOM-killed. Unit tests for raise, hold, drop to the floor, and the ceiling.
7. **Constraints from 10r (checkpoint 64615a2c5).** Measured memory is resident only (cgroup memory.current minus inactive_file; RSS for processes); `swap_bytes` is kept apart and is never charged. Service rows (hana CI, compiler cache) are tagged in `scripts/build_queue/scope_activity.py`. An empty service row (no live process, zero RAM) is not a running step (`scripts/lint/memory_admit.py:1816`); idle CI with RAM still charges it. The design and rulings are in the session file phases/10t/design.md; ruling 8 adds `build_free_check_calm`.
### Phase 10u — Gate copies belong to a staff row  · status: replaced by 18c

Phase 18c makes the staff row id the copy owner, under its `RoleOwner` boundary and the Phase 18 command with no `--owner` flag. 10u's `--owner` flag that refuses unknown names is not built (architect review of 18, 2026-11-06).

### Phase 10w — A copy's origin is the source's GitHub remote  · status: done

**Depends on:** none (split from 10u by builds, 2026-11-04; `scripts/copies/copies.py` only; runs beside 10t and before 10u).

1. **The ask (builds, 2026-11-04).** The copy origin trap hit queue-unit (80299124b took builds' untested local merge) and sdlc devtools-unit (a phase branched from sdlc's untested local merge). Land it now, ahead of 10u.
2. **The change.** A copy's `origin` is the source repo's own origin URL (GitHub), not `~/.claude`. A copy's `refs/remotes/origin/*` equal the source's `refs/remotes/origin/*`, never its `refs/heads/*`. Objects still come from the local repo. Existing copies heal on their next use.
3. **Proof.** Tests: a copy's `origin/builds` equals the source's `origin/builds`, not local `builds`; the copy's origin URL equals the source's; an old copy heals on use. `docs/as-built/gate-checkouts.md` states the rule.

### Phase 10j — Tests follow the 10w copy origin  · status: done (b4099a4cc)

**Depends on:** 10w (b1fe9560c).

1. **The ask (builds, 2026-11-04).** The 10i-10r-10w merge held on two reds caused by 10w: test_ci_points expects the old copy origin; test_merge_checkpoint_bump's fake git misses `clone` after 10w's `-c` options.
2. **The change.** test_ci_points expects the source's origin; ci_points drops its now self-mapping url insteadOf; the fake git skips every leading -c and -C pair.
3. **Proof.** test_ci_points, test_merge_checkpoint_bump and test_copies pass; basedpyright clean.

### Phase 10l — A waiting client reads the queue's answer when the retry check is slow  · status: done (main d8c13aab4; builds-queue 2e485438b)

**Depends on:** 10j (f93c24914).

1. **The ask (builds, 2026-11-04 15:22 EST, top priority).** hana-migration's verify.sh doc step printed "admission service not answering since 13:39" for 103 min while the queue showed it running with 5.3 GiB kept. It blocked a hana release commit.
2. **Cause.** While a step waits, the client runs the leave hook (the AWS retry check) every `running_pass_seconds`=5. `AcquisitionRetry.left_queue` sets `next_leave` before the hook runs. The hook takes 13-34 s, so `next_leave` is already past when it returns. `_line` then raises its timeout before `recv`, and the loop runs the hook again. The client never reads the socket: the grant sat unread (83 KB in the receive queue).
3. **The change.** `left_queue` sets `next_leave` after the hook returns. A hook slower than the interval still leaves the client one interval to read.
4. **Proof.** A regression test with a hook slower than the interval: the client reads a grant sent while the hook ran. test_admission_client passes; basedpyright clean.

### Phase 10y — No younger step starts if it delays the first in line  · status: folded into 10z

**Depends on:** 10t (both change admission in `scripts/lint/memory_admit.py`; 10y's code starts on 10t's tree after 10t is checkpointed; one admission rule, not a second path).

1. **The ask (user, 2026-11-04 12:22 EST).** "notice that 3 merge gates each requiring 6GB have passed the waiting mend startup ... why are they all skipping past the first in line". Builds ruled go the same hour.
2. **Evidence.** mend startup (need 7.3 GiB, queue priority) waited 58 min as first in line. Three merge gates (6.0 GiB each, using 0.1-0.4 GiB) started ahead of it. Cause: the backfill windows (the head's first 600 s, then 120 s of every 720 s) admit any younger step that fits; the strict windows admit younger starts until their total since the head's priority reaches budget minus head need (`strict_room`, `head_room_used`, memory_admit.py:1105, 3016, 3043).
3. **The change.** While a step waits first in line, a younger step starts only if the first one could still start at once after it: charged + younger need + head need <= budget, and the free-memory check holds for both. The backfill and strict windows and their settings go (`head_seconds`, `strict_seconds`, `backfill_seconds`, `BackfillUntil`, `StrictUntil`, `strict_room`, `head_room_used`). The bump and the head override keep their meaning.
4. **Shown.** The queue view, card and memory page drop the window text. A held younger step says it waits so the first in line can start.
5. **Proof.** Unit tests: a younger step that would delay the head waits; a younger step that leaves room for the head starts; the head starts the moment it fits. A replay of today's 11:24-12:22 queue shows mend starting before the three gates.

### Phase 10z — A run starts when it fits free memory, and the first in line is protected  · status: done

#### As-built

- There is no set budget. Room = free memory now − `safety_gib` (3) − the keep-aside of every running run. A hana CI job takes no fixed margin (resources Phase 88, 9fb57973c): a registered job is charged through its reservation, and an unregistered job counts once in the cap. A run starts when its need fits the room, pressure is calm and it is its turn. 10t's budget controller (`budget.py`, `budget_replay.py`) and 10y's backfill and strict windows are deleted; neither landed alone.
- An admitted step keeps its accepted need for its whole life. Its keep-aside is max(0, need − anonymous use). A nextest step in an observed test phase uses its frozen need, max(test peak, history N × `test_phase_margin`). Need is never cut to a limit.
- Order: bumped first, then the time the run first joined, then pid. A younger run starts only when its need fits room minus the first run's need. When no run of ours is admitted, no slot is held and pressure is calm, the first run starts even if it does not fit. Idle CI rows and unowned scopes are not runs of ours.
- Pressure (full avg10 > 5, or swap > 85% with some avg10 > 25) holds starts while it lasts. An unreadable reading also holds starts. The head line then says "pressure unreadable: <path>", and after `reading_alert_seconds` one message goes to the natedev session. The alert clears when readings return.
- When a step of a running run (BUILDLOG_RUN identity) ends, its charge stays as the run's slot for `slot_gap_seconds`. The next step takes the slot at once, with no spacing and no order. A larger next step gets its extra before any waiting run when the extra fits the room with no pressure. A restart drops held slots; state.json holds no slot fields.
- At most one new start each `recovery_start_seconds`, at all times; slot reuse is not a start. Estimate refresh uses `estimate_refresh_seconds`.
- The queue view, card, build-why and memory page show used, kept aside, room, safety and other, and the **cap** (anonymous use + CI use + free − safety), with the numbers of the last admission pass. The word "budget" is gone.
- `session_send_argv` lives in `scripts/message/session_send.py`. `SEND_SCRIPT` is its one patch point, and a sender test fails if it starts the real `send.py`. Admission code never imports `sweep`: that edge made an import cycle.
- Outside test fixtures use `AdmissionFixture.occupy_host` for a host with a run of ours and a complete calm pressure reading. An admission rule change runs every caller suite (direct `test_verify_*`, buildlog, hooks, merge admission) before the full gate.

**Files:**
- `scripts/lint/memory_admit.py` — room, keep-aside, order, head protection, slots, spacing.
- `scripts/build_queue/admission_service.py`, `admission_client.py` — pass evidence, the unreadable-reading alert, slot hold.
- `scripts/build_queue/queue_view.py`, `queue_card.py`, `queue_evidence.py`, `head_facts.py` — cap and room text.
- `scripts/message/session_send.py` — the session sender.
- `config/build_admission.conf` — `safety_gib=3`, `slot_gap_seconds=10`, `estimate_refresh_seconds=600`, `reading_alert_seconds=300`, `recovery_start_seconds=5`, `test_phase_margin=1.2`; `peak_hold_seconds.merge-gate=180` stays. Deleted: the budget keys, `build_free_check_calm`, `head_seconds`, `strict_seconds`, `backfill_seconds`, `recovery_window_seconds`, `ramp_seconds`.
- `docs/as-built/build-memory-admission.md` — the rules.

**Ruled out:** a budget that follows measured use (10t); time windows that let younger runs pass the first in line (10y); slot fields in state.json; a cancel command.

### Phase 10q — One sweep process loads store.py once  · status: done (bbae0f7eb)

#### As-built

- Phase 33 replaces the two module names with `scripts.buildlog.store` in every caller.
- Phase 33 removes the aliases and `scripts/buildlog/__init__.py`.
- `scripts/buildlog/test_repo_root_imports.py` checks the script entries from an unrelated working folder.

**Files:**
- `scripts/buildlog/store.py` — callers use its repository module name.

**Binds later work:** callers import `scripts.buildlog.store` from the repo root.

**Gotchas:** `scripts.buildlog` is a namespace package. It has no initializer or module aliases.

**Ruled out:** record.py's `__package__` check (head_facts keeps a set `__package__` under sweep); a `find_spec` search or package import from store.py (it shadows modules through `sys.path` and runs an unrelated `scripts/__init__.py` from the cwd).

### Phase 10s — A run with no scope reads its anonymous use from its processes  · status: done

#### As-built

- `process_memory(source, proc)` reads one anonymous use per step kind: a step with a scope reads its scope's `memory.stat` anon (`ScopeSource`); an in-process holder with no scope, such as the merge gate's `queue_place("merge-gate")`, sums `RssAnon` over its process tree (`ProcessSource`); otherwise `NoSource`. `MeasuredMemory.anonymous_source` holds the source, and resident bytes and swap stay separate values beside it. A gone pid or a zombie counts 0.
- A missing or invalid `RssAnon` line, or a denied status read, makes that run's use `RunUseUnavailable`.
- `run_memories` counts a shared process once, independent of order: the smallest process set takes it (reservation key breaks a tie); scopes count by depth, and an outer scope subtracts nested scopes.
- `CapUnavailable(runs)` names the runs whose use is unavailable; `room_text` prints them and no use sum. An old reason-only `CapUnavailable` record reads as `runs=()` and prints `Cap unavailable; free …`.
- `queue_place.py` samples a scope through `slice_working_set`, the one reader; `queue_view.py` rows use `hold.use`.
- The admission service has no lock: one loop thread owns its state. The test fixture refuses an off-thread `handle_request`, `persist` or `run_pass`; tests call the server through `fixture.loop_turn()`.

**Files:**
- `scripts/lint/memory_admit.py` — the anonymous-use readers, shared-process attribution, the cap record decoder.
- `scripts/build_queue/queue_evidence.py`, `queue_place.py`, `queue_view.py` — cap text, scope sampler, rows.
- `scripts/build_queue/admission_test_support.py` — the loop-thread guard.

**Binds later work:** every new admission service test calls the server through `fixture.loop_turn()` and holds no fixture turn across a client call that needs the service loop. The holder pick for a memory wait uses the counted use from `run_memories`. A `RunUseUnavailable` run keeps its full keep-aside.

**Gotchas:** a feed or client process started before an install keeps the old decoder; restart it after the install.

**Ruled out:** a service lock (tests use the loop thread instead).

### Phase 10x — The admission code keeps one way to do each thing  · status: done

#### As-built

- Admission decisions and shown numbers are the same as 10z and 10s; the golden views did not change. The code holds one way to do each thing, and main code holds no reader of an old record shape. An old-shape record makes the decoder refuse, and the service then fails at start.
- `memory_room` takes `AdmissionPolicy` alone. `inputs_room` is the one room helper. `run_owner` and `reservation_run` are the one run lookup. `RunKeepAside` holds a typed run, `memory: RunMemory` and `bytes`. `ExplainedRunningStep.owner` is filled once. `ended_step_slot` and `slot_in_force` are the one slot rule. `ContinuationSlot(slot, owner)` names a continuation's slot.
- `admission_walk` walks the given order once. `queue_fixture.py` builds `QueueInputs` that make `admission_pass` give each wanted reason.
- The service reads a waiter's build run once for each pass and reads a step lifetime only when the owner has exited or is not ending. `_live_run` and `_run_alive` are its run helpers.
- `step_pid_text`, `member_programs` and `reason_phrase` are the one copy of each text helper; `scope_activity.py` builds members once. `write_scope` is the one fixture helper for a scope.
- Fields named `recovery` are now `spacing`. The config key `recovery_start_seconds` keeps its name.
- `admission_client.py` names config absence as `ReadConfigurationAtUse`. Cleanup timing stays apart as `wire.CleanupTiming`.
- `merge_gate_reservation_gib` and `merge_gate_governor_gib` have the range 0.125..64 in `config/build_admission.conf`; their values stay 4.

**Files:**
- `scripts/lint/memory_admit.py` — room helper, run lookup, slots, walk, spacing names.
- `scripts/build_queue/admission_service.py`, `admission_client.py`, `admission_protocol.py` — run helpers, spacing names, config absence.
- `scripts/build_queue/queue_evidence.py`, `queue_view.py`, `queue_lines.py`, `queue_card.py`, `scope_activity.py` — one copy of each text helper.
- `scripts/build_queue/queue_fixture.py`, `admission_test_support.py` — shared fixtures.
- `scripts/buildlog/memory.py`, `scripts/mac_test/loopback_proof.py` — `memory_room` callers.

**Binds later work:** `RunKeepAside` has `memory.use`, not `use`. A change to a stored record ships a conversion program outside the repository, as Delegation Context says.

**Gotchas:** the service and the memory page feed keep the code they loaded until they restart. systemd keeps `build-admission.socket` active while the service fails at start. Only the user can stop and start the service (root).

**Ruled out:** old-record readers in main code; importing `sweep` from admission code (the import cycle); dropping the member pid lines; deleting `PeakHold` or the test-phase path, which are in use.

### Phase 12 — The merge gate reserves 4 GiB from config and no longer kills on memory  · status: done

#### As-built

- `config/build_admission.conf` has two keys, each 4 GiB. `merge_gate_reservation_gib` sizes the merge gate's build queue reservation (`gate_reservation` reads it when the gate joins the queue, `FixedCapSizing`). `merge_gate_governor_gib` bounds the gate's own test runner (`run_suites` reads it at start). `merge_gate_memory_gib(key)` reads both and turns an unusable value into `GateRefused` naming the key.
- `SampledRSSAdmission.governor_mib` starts a job only when latest + reserved + expected stays within it and a CPU slot is free; with no job running it always starts one. At 4 GiB the full type check (4.76 GiB reservation) runs with no test module beside it.
- The gate has no memory kill and no memory fail: the result and the reservation status depend only on test results. A module still running at the time limit still fails the gate. `--memory-cap-mib`, `DEFAULT_MEMORY_CAP_MIB` and `GateRun.memory_within_cap` are gone.
- The runner prints `memory governor N GiB`; the summary prints the sampled peak with no cap. Gate ledger records keep the field `cap_mib`, which now holds the governor bound.

**Files:**
- `scripts/production/merge_gate.py` — the two key readers, the governor, no memory kill or fail
- `config/build_admission.conf` — `merge_gate_reservation_gib`, `merge_gate_governor_gib`
- `scripts/production/merge_gate_test_support.py` — `MergeGateFixture.configure_gate`
- `scripts/production/test_merge_gate*.py` — key, governor and no-kill tests
- `scripts/buildlog/index.py` — the `cap_mib` column label
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-memory-admission.md`

**Binds later work:** the percent-done keep-aside phase deletes `merge_gate_reservation_gib` and its reader when the gate switches to the profile rule; `merge_gate_governor_gib` stays.

**Ruled out:** one key for both uses; a `--memory-cap-mib` override beside config.

### Phase 13 — A caller test's fake clock skips the start spacing when no caller run is live  · status: done

#### As-built

- `ObservedAdmissionServer` in `scripts/build_queue/admission_test_support.py` calls `ReadyToStart()` before an `AdmitRequest` or a `LaunchRequest` when no caller reservation, waiter or between-steps slot in force is live, so an idle caller fixture has no start spacing. The fake clock does not move. Occupied-host scenery does not count as a caller.
- `caller_admission(skip_idle_spacing=False)` keeps the spacing for a test that checks it. The opt-out holds when a fixture is reused.
- `allow_next_start()` takes `loop_turn()` itself, on an `RLock`.
- `MemoryRoom.ci_use` (default 0, so a reply from an older service decodes as 0) and `MemoryRoom.cap_use` = `used + ci_use`. Cap = anonymous use + CI use + free - safety, at `memory_room`. `inputs_room` sums `ci_use` from readable `UnownedSliceMemory` service rows. Room, shortfall, kept memory and admission do not change.
- `queue_evidence.room_text` and the `queue_view` cap bar use that one cap. The CI term is absent at 0. `ci_amount` prints CI use under 0.1 GiB in MiB.
- `scripts/lint/memory_gate.sh` limits pipefail to a subshell around the admission pipeline, for bash 3.2 on the Mac.

**Files:**
- `scripts/build_queue/admission_test_support.py` — the skip rule, `caller_live`, `allow_next_start`
- `scripts/build_queue/test_caller_admission_clock.py` — idle, live caller, scenery, opt-out and lock tests
- `scripts/build_queue/queue_fixture.py` — `ci_step`
- `scripts/lint/memory_admit.py` — `ci_use`, `cap_use`, `inputs_room`
- `scripts/build_queue/queue_evidence.py`, `scripts/build_queue/queue_view.py` — the cap text and bar
- `scripts/lint/memory_gate.sh`, `scripts/lint/test_memory_gate_wait.py` — the pipefail scope
- `docs/as-built/build-memory-admission.md` — the cap formula

**Binds later work:** `MemoryRoom.ci_use` counts only CI rows with no reservation once hana CI builds wait in the queue, so a registered job counts once. A CI job has no run row, so it is not a holder of a memory wait while it has no reservation. Nothing in `run_gate` needs an `allow_next_start()` advance.

**Gotchas:** a service that has not restarted reports the old cap. The speed numbers do not prove a gain under load: lint_judgment 162.8 to 131.5 s, cache_versions 120 to 149 s, token_loss 13.6 to 9.9 s.

**Ruled out:** moving the fake clock ahead of real time; `recovery_start_seconds=0`; a mock of the admission result; a required `ci_use` wire field.

### Phase 10o — The admission client starts with only what it uses  · status: done

#### As-built

- `release`, `width` and `token_wait` load no `admission_service`, `queue_view`, `head_facts` or `memory_admit`. `admit` and `launch` load the estimator inside `step_request`. Replies, wire shapes, stored records, config keys and printed text do not change.
- `scripts/build_queue/system_clock.py` holds `SystemClock` (`now`, `monotonic`, `sleep`) and imports no service code.
- `scripts/build_queue/admission_records.py` holds the records, `GIB`, `MemoryConfig`, `read_memory_config`, `warn_configuration_diagnostics`, `AdmissionWaitSnapshot`, `reason_phrase`, `behind_place` and `eligible_head_position`.
- `scripts/build_queue/step_estimator.py` holds `step_estimate`, `program_peak_estimate` and the code only they reach. `TEST_STEPS` is in `scripts/lib/test_phase.py`.
- Each moved name has one definition. `memory_admit.py` re-exports each moved public name (`from x import A as A`), and `queue_view.py` re-exports `reason_phrase`. `ADMISSION_CODE_PATHS` holds the three new modules.

**Files:**
- `scripts/build_queue/system_clock.py`, `scripts/build_queue/admission_records.py`, `scripts/build_queue/step_estimator.py` — new
- `scripts/build_queue/admission_client.py`, `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_code.py`, `scripts/build_queue/queue_view.py`
- `scripts/lint/memory_admit.py`, `scripts/lint/build_shape.py`, `scripts/lib/test_phase.py`
- `docs/as-built/build-memory-admission.md`

**Binds later work:** a test patches a moved name where the code now reads it. `memory_admit.py` imports `conf`, `test_phase` and `build_shape` by bare name, and a second import form loads a second copy whose classes do not match.

**Gotchas:** the client still loads 158 modules, and the release list holds `scripts.message` and `scripts.build_hold.override_guard`.

**Ruled out:** a new import test; the `build_hold.py` import, under the builds file freeze.

### Phase 18a — A gate copy's folder comes from the repository's origin  · status: done

#### As-built

- `repository_name` in `scripts/copies/copies.py` follows local origins to the last one and takes the last URL part without `.git`. A cycle is an error. A source with no origin uses its main checkout's folder name.
- A standing gate borrows from `final_object_store`, the last store in the source's chain. Only `initialize` follows that chain; other `initialize_git` callers keep their one-step source.
- A workspace record holds `repository_identity`, and `workspace_repository` reads it.
- `remove_trees` continues past a busy or failed tree or mirror, removes the rest, and raises one `CopyError` that names each one kept (exit 2). A copy whose objects another workspace borrows stays (`_gate_borrowers`).

**Files:** `scripts/copies/copies.py`, `scripts/copies/test_copies.py`, `scripts/copies/test_remote_copies.py`, `scripts/lib/scratch.py`, `docs/as-built/gate-checkouts.md`.

**Binds later work:** Phase 18 merges its `remove_trees` rewrite over this rule and verify's mirrors. A rename must not call `remove_trees` before Phase 18c. Deploy runs the session's `migrate_18a.py --apply` before repository cleanup.

**Gotchas:** the 23 trees in `~/worktrees/gates/.claude` go idle after this lands; hierarchies Phase 28 deletes them.

**Ruled out:** deleting stale gate folders in this phase; a name list for repositories.

### Phase 20c — A merge gate near its end keeps less memory with rerun records in its history  · status: done

#### As-built

- The look-ahead window in `window_peak` stops at the end of `suites:1` (`GATE_FIRST_PASS` in `scripts/build_queue/progress_profile.py`).
- A gate that reads a profile with old `suites:2` records now gets its first pass tail: measured 0.23 GiB at 85-86%, 0.14 GiB at 95%.
- Crossings between `build:` and `test:` phases, and `build:1` to `build:2`, stay. A `suites:2` counter over measured bins follows the normal window rule.

**Files:** `scripts/build_queue/progress_profile.py`, `scripts/build_queue/test_progress_profile.py`, `scripts/production/test_merge_gate_suites_admission.py`, `docs/as-built/build-memory-admission.md`.

**Binds later work:** the service reads this rule after a restart. The Change-Review row comes after a live gate past 85% shows under 1 GiB.

**Gotchas:** old-code gates still write `suites:2` records until each tree carries 20a; they have no effect on the first pass.

**Ruled out:** removal of live records; a change to `merge_gate.py`.

### Phase 18b — A repository with no build cache gets a fresh copy for each run  · status: done

#### As-built

- `keeps_build_cache(source, base)` in `scripts/copies/copies.py` is the one decision point. It reads `Cargo.toml` at the root of commit `base`. `standing` uses `available_tree` when it is true and `fresh_tree(owner)` when it is false.
- `fresh_tree` makes a scratch folder with word `<owner>-gate` and removes it at success, at a caller exception and at a preparation failure. A fresh copy has no post-use work.
- `scratch.new(throwaway=True)` marks the folder. `scratch.is_throwaway` tells `tmp_sweep.py` to treat a killed run's folder as saved work, so the sweep removes it.
- Measured: one call on a synthetic tree of 2,003 files and 45 MB took 0.78 s (first call 0.53 s). This is one measure.

**Files:** `scripts/copies/copies.py`, `scripts/copies/test_copies.py`, `scripts/copies/test_remote_copies.py`, `scripts/copies/test_review_remote_copies.py`, `scripts/lib/scratch.py`, `scripts/lib/test_scratch.py`, `scripts/lint/tmp_sweep.py`, `scripts/lint/test_tmp_sweep.py`, caller test modules in `scripts/production/`, `scripts/style/test_crate_run.py`, `scripts/buildlog/test_rust_release.py`, `docs/as-built/gate-checkouts.md`.

**Binds later work:** Phase 18c carries the owner type into `fresh_tree`'s scratch word. `remove_trees` still has the same standing-tree rule.

**Gotchas:** a test fixture with a root manifest needs `fake_cargo_output_roots`, or the gate's real-cargo tripwire fails it. `gate_like.sh` does not run that tripwire. `rust_release.py` is frozen and its owner stays `toolchain-trial`.

**Ruled out:** a repository name list; deleting stale gate folders in this phase.

### Phase 18 — A standing gate tree takes its owner from the staff row and its folder from the repository's identity  · status: done

#### As-built

- `copies.py run` and `copies.py remove` take the owner from the calling session's staff row; a worker row uses its parent unit's name. The `--owner` flag is gone. A caller with no staff row gets a one-line error. `standing()` and `remove_trees()` keep an owner parameter for in-process callers (`merge_gate.py`, `style_sync.py`, `ci_points.py`, `bump_gate.py`); `rust_release.py` keeps its fixed owner (build freeze).
- Retire (`retire_unit.py`) and the end of a unit run (`end_session.sh`) remove the unit's idle standing trees and workspaces with the shared call that `tidy_wrapped.py` uses. A busy tree prints one line and stays for the later cleanup. A kept remote mirror still refuses the retire: `remove_trees` raises `RemoteMirrorKept`.
- `owned_workspaces` reads `repository_identity` from each workspace record (`workspace_repository`), so a workspace keeps its repository after its source is deleted.
- Deploy step, 2026-11-06 22:54: a program outside the repository wrote `repository_identity` into 2 live workspace records whose origin was a deleted test project (identity = the origin path text). It skipped 10 landing copies that have no `owner_unit`. `migrate_18a.py` now plans nothing.
- The copy guard's refusal text and the docs (`gate-checkouts.md`, `style-showrunner.md`, `style_production.md`, `mend_fix.md`, `adversarial.md`) name `copies.py run` with no flag.

**Files:** `scripts/copies/copies.py`, `copy_guard.py` and their tests; `scripts/production/retire_unit.py`, `staff.py`, `tidy_wrapped.py` and their tests; `scripts/direct/end_session.sh`, `test_end_session.py`; the item 6 docs; one-line caller changes in `bevy_upgrade.py`, `build_queue.py`, `hana_shot.py`, `scratch.py`, `unit_runs.py`, `memory_admit.py`, `proposal_issue.py`, `worker_admission.py`, `session_types.py`, `crate_run.py`.

**Binds later work:** 18c converts the same records from staff names to staff row ids; it keeps `repository_identity`, skips records with no `owner_unit`, and makes no lock file in a dry run.

**Gotchas:** live workspace records include copies of deleted test projects, so `repository_name` cannot resolve every origin. Landing copies under `/tmp/claude/scratch` carry `source_path` with no `owner_unit`.

**Ruled out:** a `--owner` flag that refuses unknown names (Phase 10u's design); a second repository-name rule beside 18a's.

### Phase 18c — Gate copies belong to a staff row, and a rename removes nothing  · status: done

#### As-built

- `scripts/copies/copies.py` holds one closed owner type: `StaffOwner(row_id)` for a staff row and `RoleOwner(name)` for a fixed role with no staff row (the `owner.<role>` values and `rust_release.py`'s `toolchain-trial`). `standing()` reads frozen `rust_release.py` text at one boundary: a staff row id is a `StaffOwner`, all other text is a `RoleOwner`.
- `GateRecord.owner`, `owner_unit`, the remote registry record and the mirror marker (`mark_mirror`) hold the row id. `remove_trees`, `owned_remote_mirrors` and the tree lookup compare owner types from those records. Readers show the current staff name, read from the id at read time. No reader parses an owner from a folder name.
- A new gate folder is `<staff name at creation>-gate` or `<role>-gate`, with the next free suffix when another owner holds the name. A folder name never changes. `fresh_tree`'s scratch word uses the same name. The tree lookup reads the records in `<root>/<repo>/`.
- A rename removes nothing and changes no copy. `remove_trees` is the one removal function. Retire keeps `RemoteMirrorKept`.
- Every staff-row caller passes the row id: `tidy_wrapped.py`, `retire_unit.py`, `end_session.sh`, `caller_owner`, `source_owner`, `fast_track.py`, `bump_gate.py`, `ci_points.py`, `waiting.py`, `merge_gate.py` `nightly_owner`, `style_sync.py` `gate_owner`, `mac_test/offload.py`, `mac_test/linux_runner.py`, `production/mac_run.sh`, and the mirror and clone options.
- A pending bump-gate record saves `owner_id` and `unit_id`. `bump_gate.py` reads `owner_id`. `merge_checkpoint.py` `send_result` reads the current sessions from the staff rows at each delivery attempt and sends from the current production name. It skips recipients in `delivered_to` before it reads their rows.
- A `.removing` tombstone has no owner; `remove_trees` finishes each idle tombstone in its repository scope. A gate folder with no record is free: the tree lookup can claim it, and `remove_trees` selects it for no owner.
- The deploy program `<phase folder>/migrate_18c.py` (outside the repository; dry run, then `--apply`) converts bare and `-unit` names to row ids in local records, the remote registry, mirror markers and every bump-gate record that lacks the ids. It moves no folder. A name no row holds or two rows hold, or a busy tree, stays and prints one line; a rerun finishes. `prove_migration.py` proves it.

**Files:**
- `scripts/copies/copies.py` — owner type, records, lookup, `remove_trees`
- `scripts/production/merge_checkpoint.py`, `scripts/production/bump_gate.py` — bump-gate ids and delivery by staff row
- `scripts/production/tidy_wrapped.py`, `retire_unit.py`, `fast_track.py`, `ci_points.py`, `waiting.py`, `merge_gate.py`, `mac_run.sh`, `scripts/style/style_sync.py`, `scripts/mac_test/offload.py`, `linux_runner.py`, `scripts/direct/end_session.sh` — callers pass the row id
- `docs/as-built/gate-checkouts.md`, `docs/as-built/staff-accessors.md` — current rules
- `scripts/copies/test_copies.py`, `test_remote_copies.py`, `scripts/production/test_merge_checkpoint_bump.py`, `merge_checkpoint_test_support.py`, `test_tidy_wrapped.py`, `test_retire_unit.py`, `scripts/direct/test_end_session.py` — proofs

**Gotchas:** main code keeps no old-record reader, so old records read as unowned until the deploy runs. Tell hierarchies when this phase is on main.

**Ruled out:** a second removal function (`remove_gate_trees`); row ids in folder names, because the user sees folder names in the project list.

### Phase 20b — A clippy fix step leaves the machine-wide build tokens to other builds  · status: done

#### As-built

- A `cargo clippy --fix` step runs with `CARGO_MAKEFLAGS`, `MAKEFLAGS` and `MFLAGS` removed and with `--jobs` from `clippy_fix_jobs=8` in `config/lint.conf`. The `fixing` branch of `invoke_clippy` in `scripts/lint/invoke.sh` holds the call; a plain clippy step keeps the pool, on natedev and on the Mac alike.
- `lint_config_clippy_fix_jobs` in `scripts/lint/lint_config.sh` reads the key with builtins only.
- One real hana `clippy --fix`: installed 323 s wall, 0 s wait; new 394 s wall, 192 s wait. The load differed, so there is no speed claim. The real cargo process had no pool variables.
- A waiting rustc holds its token: 86 of 105 samples had 20 or more shims with no compiler child, mean 27.4 idle holders, with 0 free tokens.

**Files:** `config/lint.conf`, `scripts/lint/lint_config.sh`, `scripts/lint/invoke.sh`, `scripts/lint/test_invoke_scope.py`, `docs/as-built/phase-checks.md`, `scripts/direct/test_verify_lint_output.py`, `scripts/direct/test_verify_lint_judgment.py`, `scripts/direct/verify_lint_scope_test_support.py`, `scripts/direct/test_verify_token_renew.py`.

**Binds later work:** a change to the fix call updates the four test files above that assert it.

**Gotchas:** a proof on the shared pool holds tokens for every other build. A proof runs at a quiet time or in a small pool.

**Ruled out:** a speed claim from the hana runs.

### Phase 20a — An outage message reaches the builds session, and a gate near its end keeps its profile's value  · status: done

#### As-built

- `scripts/message/roster.py:1286` also catches `herdr.HerdrFailure`, so `send.py` keeps the session entries and delivers from a service with no herdr on its PATH.
- `down_alert`'s phone push reads "The builds session did not take the message. Builds wait until the service answers."
- A merge gate rerun sets `progress_total = 0`, so `publish_progress` publishes nothing, and `progress_profile.clear_counter` removes the counter file. The rerun keeps the full need. `progress_phase` is gone, and the first pass counter is `suites:1`.

**Files:**
- `scripts/message/roster.py`, `scripts/message/test_roster.py` — the catch
- `scripts/build_queue/admission_client.py`, `scripts/build_queue/test_admission_client.py` — the push text, the proof with no herdr
- `scripts/build_queue/progress_profile.py` — `clear_counter`
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate_suites_admission.py` — the rerun
- `docs/as-built/build-memory-admission.md`, `docs/as-built/merge-gate-and-test-speed.md` — the rerun rule

**Binds later work:** a module that names `session_send_argv` installs `forbid_real_sender` in `setUpModule`, because `scripts/message/test_session_send_sources.py` requires it.

**Gotchas:** the installed `merge_gate.py` runs for every `--checkout` gate, so `suites:2` records keep arriving until this phase is installed. Stored `suites:2` records stay until removed.

**Ruled out:** a reader in main code that skips stored `suites:2` records.

### Phase 21 — Each moved admission name has one import path  · status: moved to report (11-08)

**Depends on:** 10o, and resources 0dfe65382 on the builds branch, because that branch conflicts in `memory_admit.py`. Source: builds showrunner, 2026-11-06 02:2x EST (design call).

#### Work Order

**Goal:** A name that phase 10o moved has one import path, its defining module. No caller reads it through `memory_admit`.

**Spec:**

1. **The rule (builds, 2026-11-06).** "The owner of a move switches every caller, and the old path goes." Phase 10o left 230 names re-exported from `memory_admit`, so one name has two import paths.
2. **Callers.** A static walk on `136497f98` found 39 files that read a moved name through `memory_admit`. Each one imports it from `scripts/build_queue/admission_records.py`, `scripts/build_queue/step_estimator.py`, `scripts/build_queue/system_clock.py` or `scripts/lib/test_phase.py`. Run the walk again on the phase's merged tree; other units' landings change the list.
3. **Test patches.** A `mock.patch` target on a moved name names the defining module. A patch that changes a name the code reads through another module keeps its effect; the tests prove it.
4. **`memory_admit`.** Each `from x import A as A` form goes. `memory_admit` imports only the names it uses. A name it does not use leaves its import.
5. **The frozen file.** `scripts/buildlog/mac_idle.py` gets the one import change, by builds' ruling on 2026-11-06. The notice names it on its also-touches line.
6. **Behavior.** Replies, wire shapes, stored records, config keys and printed text do not change. No assertion changes.
7. **Proof.** A one-off walk in the phase's folder prints no caller of a moved name through `memory_admit`, and the notice carries that result. Add no new import test: a new check goes to the user first. All caller suites pass. The client start measurement stays: `release` still loads no `memory_admit`.

**Files:**
- `scripts/lint/memory_admit.py` — and the 39 files of item 2, as the walk lists them on the merged tree.

**Workers:** `1 writer + 1 tester`
- `impl2` — production callers and `memory_admit.py`.
- `test2` — test files and test support, patch targets, and the walk.

**Gate:** the merge gate, and `basedpyright` 0/0/0 on each changed file and each caller module.

**Change-Review:** none — no behavior changes.

### Phase 10h — A memory wait names what holds it  · status: done

#### As-built

- `AdmissionWaitSnapshot.holder` is a `WaitHolder` in `scripts/build_queue/admission_records.py`: `SessionHolder(session_id, session_name)`, `UnitHolder(unit)` or `NoHolder()`. The field has a default, so a reply from an older service decodes as `{"kind":"none"}`.
- `admission_wait_holder(decision, reason, inputs, room)` in `scripts/lint/memory_admit.py` names the recorded order hold, else the largest grouped charge over `MemoryRoom.runs`. The charge is `RunKeepAside.memory` plus anonymous use, with the full charge for unavailable use. A tie breaks on the owner key. Session identity wins over unit.
- A wait has no holder for pressure, an unreadable reading, start spacing, an estimate refresh or an admitted row. Unregistered CI use is never a holder.
- `wire.RunPlace.unit` (default empty) keeps a run's unit, so a held slot names that unit after its reservation ends. An older stored row reads as no unit, with no conversion.
- `WaitOutput.last_holder` in `scripts/build_queue/admission_client.py` emits `holder <json>` after `first` and at each change, before notice throttling.

**Files:**
- `scripts/build_queue/admission_records.py` — the holder types
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py` — `PassRow.holder`, `wait_holder_identity`, `admission_wait_holder`
- `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_client.py` and their tests
- `docs/as-built/build-memory-admission.md` — the holder JSON and the held-slot rule

**Binds later work:** the verify wait display reads the `holder` event. A new `StepMemory` kind or a new required snapshot field stops a waiting client from older code at the next service restart, so the notice names it. A `RunPlace` change is a stored record change.

**Gotchas:** a held slot does not survive a service restart. The installed client decoder refuses a new memory kind with `invalid snapshot variant`. A client keeps its loaded code and emits no holder event until it restarts. The holder is computed for each waiter on each pass.

**Ruled out:** a permanent test against the previous revision, because `git show HEAD` compares new code with new code after a commit; a `queue.json` version raise.

### Phase 10k — Rust runs and merge gates keep aside memory by percent done  · status: done

#### As-built

- Each run of cargo build and check, clippy, cargo mend, nextest and the merge gate publishes a counter: a phase plus done of total. A merge gate's counter is suites done of suites total. Counter parsers, `CaptureReader`, the profile store and the window are in `scripts/build_queue/progress_profile.py`.
- Each run records its highest anonymous use in each 5% of each phase, under `reservation_profile_key(row, host)` = host, step and gate shape.
- `reservation_need` keeps aside max(0, highest recorded use from percent now to percent now + `progress_lookahead_percent` (20) - use now). The window crosses into the next phase. The rule works per step, before `run_memories` groups steps under one owner, and `RunKeepAside.memory` holds the value.
- A nextest test phase keeps its frozen N and `test_phase_margin`, and the test peak does not freeze. Progress lowers the need only from measured anonymous use.
- No counter, no profile and `RunUseUnavailable` keep the full need. A run with unavailable use writes no sample.
- The merge gate uses `queue_place` default sizing with `program_first_peak_gib.merge-gate` (4). `merge_gate_reservation_gib` and `FixedCapSizing` are gone; `merge_gate_governor_gib` stays. Legacy gate records do not size a keep-aside.
- Snapshot version 5, codec in `scripts/build_queue/snapshot_codec.py`. A version 4 read gives `NoQueueSnapshot` until the next pass. Old `FixedCapSizing` wire tags are refused. There is no restart conversion. The page hover shows phase, percent done, keep-aside and reason.

**Files:**
- `config/build_admission.conf` — `progress_lookahead_percent`, `program_first_peak_gib.merge-gate`
- `scripts/build_queue/progress_profile.py`, `scripts/build_queue/snapshot_codec.py` — new
- `scripts/build_queue/queue_snapshot.py`, `admission_service.py`, `admission_protocol.py`, `admission_client.py`, `admission_code.py`, `queue_place.py`, `queue_evidence.py`, `queue_fixture.py`
- `scripts/lint/memory_admit.py` — `reservation_need`, `test_phase_charge`, `run_memories`, `reservation_profile_key`
- `scripts/lint/invoke.sh` — `buildlog_exec_with_progress`, `without_progress_frames`
- `scripts/production/merge_gate.py` — the gate's counter and first-peak sizing
- `scripts/lib/boot_identity.py`, `scripts/lib/atomic_file.py` (`atomic_sample`)
- `docs/as-built/build-memory-admission.md`, `docs/as-built/merge-gate-and-test-speed.md`

**Binds later work:** a cargo front outside `invoke.sh` sets the progress settings on the child only, and a CI call follows the same look-ahead once it has a counter. A holder's charge reads `RunKeepAside.memory`. A snapshot shape change raises the version and needs no conversion. A rename of the `merge-gate` step touches `program_first_peak_gib.merge-gate` and `peak_hold_seconds.merge-gate`, and a config key rename needs the user's approval. The build page feed imports `snapshot_codec`.

**Gotchas:** `scripts/direct/tree_watch.py:235` puts every `CARGO_*` name into the build key, so a shell export of the progress settings changes the key of later steps. cargo refuses `always` on a pipe without a width. The replay of recorded gates had no cgroup OOM increments, so it does not prove safety for three future gates at once. `merge_gate.py` keeps its resident-plus-swap reading for its runner and governor; a profile reads only anonymous use.

**Ruled out:** a restart conversion for snapshots or profiles; a full-need fallback from legacy gate records; resident bytes or swap in a profile.

### Phase 19 — The merge gate runs no type check, and the daily run does  · status: done

#### As-built

- `merge_gate.py` with `--tests-only` or on the merge path runs no type job: `--type-dirs` defaults to no folders. An explicit `--type-dirs scripts` still runs the check. `gate_reservation(repo, temporary, hold, *, type_dirs=())` sizes from the folders.
- `merge_gate.py --nightly`, from the 03:00 `claude-nightly.timer`, sets `NIGHTLY_TYPE_DIRS = ("scripts",)` and runs the full check. It splits `type_failures` from `failed`.
- A nightly type failure tells the showrunner, with the failing files and the first `NIGHTLY_TYPE_DIAGNOSTIC_LINES` (20) lines, and writes no `full-required` marker. A red nightly with no failed job says the full suite is required.
- `memory_admit.merge_gate_shape(argv)` returns `MERGE_GATE_TYPES` when `--type-dirs` is in the argv, else `MERGE_GATE_TESTS`. `admission_service.py` stores the shape, and `reservation_profile_key` keys on it, so a nightly profile does not raise a test-only gate's need.
- Workers keep their type check on changed files and caller modules.

**Files:**
- `scripts/production/merge_gate.py` — the type folder rule, the nightly split, `tell_nightly_red`
- `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate_suites_types.py`
- `scripts/lint/memory_admit.py`, `scripts/lint/test_memory_admit.py` — the gate shape
- `scripts/build_queue/admission_service.py` — stores the shape
- `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/build-memory-admission.md`

**Binds later work:** a rename of the `merge-gate` step carries the shape in the profile key.

**Gotchas:** a type error can reach main, and the next 03:00 run reports it. Before this change, 142 full passing gates had wall median 1500 s, CPU median 6642 s and peak median 4649 MiB; one type job had wall median 164 s, CPU median 227 s and peak median 4067 MiB.

**Ruled out:** a separate `type_gate.py`, a promotion hook or a new timer; a client that sends the gate shape.

### Phase 24 — Each running row names its machine  · status: done

#### As-built

- The run rows of `build_queue.py view` show a `machine` column after `min` and before `pid`. The value is `scripts.buildlog.store.host_name()`, read once per view: the admission service admits work only on the machine where it runs.
- A narrow view (60, 80) leaves the column out before it cuts a name, so `view_60.txt` and `view_80.txt` did not change.
- The one-step card names the machine after the pid in its running line. The card has no second table.
- `queue_fixture.py` sets a private host name, so no test reads the live host.

**Files:** `scripts/build_queue/queue_view.py`, `queue_card.py`, `queue_fixture.py`; `test_queue_view.py`, `test_queue_card.py`; goldens `view_100.txt`, `view_140.txt`, `card_1.txt`, `card_4.txt`, `no_bump_100.txt`, `unit_override_view.txt`; `docs/as-built/build-memory-admission.md`; `commands/build/queue.md`.

**Binds later work:** Phase 10g places steps on other hosts; its rows need a host per row from the service, because the view reads one `host_name()` for all rows.

**Ruled out:** a new host name function; a per-row protocol field before 10g.

### Phase 14 — A waiting build stops with a clear message when the admission service keeps failing to start  · status: done

#### As-built

- In `_acquire` (`scripts/build_queue/admission_client.py`), only a parsed reply sets `retry.unreachable_deadline` to no limit. A connect does not clear it. A restart error (`ECONNRESET`, `EPIPE`, `TransportUnavailable`) starts the same limit as a missing socket: `unreachable_limit_seconds`.
- The receive loop reads `retry.unreachable_deadline`, so an accepted silent connection stops at the limit. On a read timeout the order is: the leave check, the launch deadline, then `check_unreachable()`.
- At the limit the client stops with "build queue unreachable for <time>; step stopped; cargo token given back".
- A service that answers within the limit keeps the wait and its place. Launch clients keep their launch limit. Release uses `CleanupTiming`, which has no unreachable limit.

**Files:**
- `scripts/build_queue/admission_client.py` — the retry loop
- `scripts/build_queue/test_admission_client.py` — stop at the limit with closed and silent connections, a valid reply after the limit, a reply that keeps the wait

**Gotchas:** the socket tests assert the fixture clock, because elapsed-time bounds fail under load.

**Ruled out:** a new config key.

### Phase 15 — A queue priority change stops blocking other queue requests while it reads the roster  · status: done

#### As-built

- A priority set reads the session roster on the daemon thread `priority-roster`. The service loop keeps serving queries and releases during the read. One loop thread still changes service state, and an eventfd (`priority_events`) in the epoll set wakes the loop when the read ends.
- `QueueChange` covers priority set, priority clear and head admission. `handle_request` sends each one, after its replay check, to `_submit_queue_change`. `OrderedQueueChange(request, connections)` keeps arrival order, and a later change waits behind the pending one. Queries and releases do not wait.
- `PendingPriorityRoster(change, result: Future)` or `NoPendingPriorityRoster` holds the pending state. `_finish_priority_roster` runs in `run_once` after the ready loop. A retry with the same key joins the pending change and gets one reply.
- Nothing about a pending change is saved, so a restart during the read leaves the priority unchanged, and the client retries. `_complete_queue_change` refuses all clients of the change on `ValueError` or `TypeError`, with no replay record.

**Files:**
- `scripts/build_queue/admission_service.py` — the pending change, the roster thread and the wake
- `scripts/build_queue/admission_test_support.py` — `AdmissionFixture.completed_queue_change(request)`
- `scripts/build_queue/test_admission_service.py` (`PendingPriorityTests`), `scripts/build_queue/test_queue_view.py`, `scripts/lint/test_memory_admit.py`

**Binds later work:** a retry connection attaches to a pending change before the roster read ends, so authorization covers `_submit_queue_change`. A test of a queue change uses `completed_queue_change`, because the reply comes after the loop finishes the read.

**Ruled out:** a service lock; saving a pending change.

### Phase 16 — The copy guard refuses only what makes a repository tree  · status: moved to verify (11-08)

**Depends on:** 18c, and verify P35 on GitHub builds, which hands `scripts/copies/` to queue-unit and edits `copy_guard.py`. Order of the copies items: 18c, then 16, then 17 (Phase 18 retrospective). The builds ruling of 2026-11-05 stands: "A broken guard is fixed at once".

#### Work Order

**Goal:** A command that makes no repository tree passes the copy guard, and a refusal says what the guard could not read.

**Spec:**
1. **Refused today.** `python3 - <<'EOF'` for a program that only rewrites a showrunner state file ("cannot parse Python program"). `python3 /dev/stdin`, `sed` loops and `bash -n` from the builds showrunner's commands. `python3 - <<EOF` with an expansion ("cannot resolve expanded interpreter stdin").
2. **Fix.** Parse valid Python whose string holds nested quotes. A form with no tree-making operation passes. An input the guard cannot read gets a message that names what it cannot read, and the message names `copies.py` only for a tree-making operation.
3. **Keep.** Each tree-making form in `test_copy_guard.py` stays refused. Change an assertion only for a form item 1 names, and state why in the summary.
4. **Proof.** One regression test for each form in item 1, with the real hook command text.
5. **Command list (architect review of 15 and 10o, 2026-11-06).** Before dispatch, the director writes `<phase folder>/refused_commands.md`: each complete command and its expected result, with the failing Python bodies, the expanded heredoc and the `sed` loop. Today the corpus has only a simple `sed` read (`test_copy_guard.py:145`). The prompt names this file.

**Files:**
- `scripts/copies/copy_guard.py`, `scripts/copies/shell_syntax.py` — the parser and the messages.
- `scripts/copies/test_copy_guard.py` — the tests.

**Workers:** `1 writer + 1 tester`.

**Acceptance gate:** copies and hook suites pass; basedpyright 0/0/0. Change-Review: a command that the guard refused now runs.

### Phase 17 — copies.py list shows remote mirrors  · status: done by verify 41u (11-08)

**Depends on:** 18c, 16 and verify P35 on GitHub builds. Builds rulings, 2026-11-05. The verify 35b unit keeps the 26 simplify items. The copy start object scan moved to natest's copies.py Git-work speed phase (builds, 2026-11-05). On `copies.py` the second lander resolves.

#### Work Order

**Goal:** `copies.py list` answers who holds a mirror.

**Spec:**
1. **List.** `copies.py list` prints a header row with labeled columns, the number column included: owner, repository, host, path and session. It shows remote mirrors. The owner column shows the current staff name, looked up from the row id; a role owner shows its role name. Today it shows local standing checkouts only and skips `.remote-mirrors` (`copies.py:804`).
2. **Holder states (architect review of 18, 2026-11-06).** `RemoteMirror.session` stays stored after `remote_lease()` ends, so it can name a former holder. The session column names a holder only with live lease evidence. A mirror is in one of three named states: held (the current staff name, read through 18c's row id), idle, or unavailable.
3. **Owner and holder are two columns (architect review of 18c, 2026-11-07).** `request_mirror` carries an owner and a holder as two values (`offload.py:2233`). The owner column shows the mirror's owner. The session column shows the live holder. Each has its own display rule. A role owner keeps its fixed role name.
4. **Owner name states (architect review of 18c).** `owner_name` returns the raw row id when the staff read fails or the row is missing (`copies.py:82`). The list must not show a row id. The owner is in one of named states: a current staff name, a role name, or unavailable. An unavailable owner keeps its row in the list and keeps its lease state.
5. **Proof.** List tests for remote-only records and mixed local and remote records. Lease-state tests for held, idle and unavailable, with a former holder that the list does not name. A renamed owner with a different live holder shows the new owner name and the holder. A missing staff row shows the owner as unavailable, keeps the row and keeps its lease state.

**Files:**
- `scripts/copies/copies.py` — the list.
- `scripts/copies/test_copies.py`, `scripts/copies/test_remote_copies.py` — the tests.

**Workers:** `1 writer + 1 tester` — `impl` owns `copies.py`; `test` owns `test_copies.py` and `test_remote_copies.py`.

**Acceptance gate:** copies suites pass; basedpyright 0/0/0. Change-Review: `copies.py list` output.

### Phase 20 — One build queue outage sends one message to the builds session and no repeated phone pushes  · status: done

#### As-built

- A long outage of the build queue service sends one message. `down_alert` in `scripts/build_queue/admission_client.py` sends it to session `builds` with `session_send_argv`, sender `build-admission`. The text names the journal command and the restart command.
- A send result other than `sent` adds one blocked phone push through `alert.blocked`, with the action "On natedev, run: sudo systemctl restart build-admission.socket build-admission.service". `--clears-when` runs `admission_client.py view`, which exits nonzero while the service is down.
- The claim stays when a send sent or kept a message. It releases only when both sends return `failed`, `refused` or `retry`.
- `_check_outage_alert()` in `scripts/build_queue/admission_service.py` runs from `run_once`. It clears the `build-admission` claim once, after `silence_seconds` of listener uptime, with the last usable config. A failed clear prints one line and retries. Listener setup clears nothing.
- `build_admission.down` is not in the push catalog.

**Files:**
- `scripts/build_queue/admission_client.py` — `down_alert`
- `scripts/build_queue/admission_service.py` — `_check_outage_alert`, `listener_started_at`
- `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json` — the removed entry
- `docs/as-built/build-memory-admission.md` — the startup section
- `scripts/build_queue/test_admission_client.py`, `scripts/build_queue/test_admission_service.py`

**Binds later work:** a move of `scripts/notify` to repo-root imports moves the bare `push_texts` import in `down_alert` too.

**Gotchas:** `from scripts.notify import push_texts` beside a bare `push_catalog` loads a second `ActionTemplate`, and `render` raises `extra values ['action']`. The alert half runs from main at once; the service half needs a service restart.

**Ruled out:** a nonzero send as the cause of the repeats; a new config key for the steady run.

### Phase 26 — A cloud gate holds a cloud slot and shows its modules done  · status: done

**Depends on:** none. aws builds the runner (builds-aws-up phase 5) and calls this phase's publish function. Priority over 27 (builds, 2026-11-07).

1. **The ask (user ruling 2026-11-07, through builds; builds-aws-up phase 6).** Merge gates and targeted tests run on cloud Spot boxes as soon as possible. The queue tracks each cloud gate as one cloud slot and charges it no natedev memory. Up to 9 cloud gates run at once, and later ones wait. The HTML page and `/build:queue` show modules done of total for each cloud gate. Every tunable is in config.
2. **Admission.** `AdmitRequest` gets a place: `LocalPlace` or `CloudPlace(target)`. The natedev route process admits through `admission_client` with step `merge-gate` or `targeted-test` and `CloudPlace("aws")`. Its pid is the run identity, so a dead route frees its slot. A cloud row needs no memory, takes no memory room, and gets no progress-memory adjustment. It waits only for a free cloud slot.
3. **Slots.** `config/build_admission.conf` gets `cloud_gate_slots=9`, read at use. A waiting cloud gate names its wait: all cloud slots are in use, with the count.
4. **Progress data (agreed with aws-unit, 2026-11-07).** The route writes `modules` done and total with `progress_profile.publish_counter(sidecar, ...)`, the file `merge_gate.py` writes today. A new module `scripts/build_queue/cloud_progress.py` holds `CloudProgress` (state `requesting|setup|running|uploading` as a named type, `size_vcpus`, `failed_modules` count, `updated_at`), its atomic writer `publish(sidecar, progress)` and its reader. The file is `<sidecar>.cloud.json`. A missing or unreadable file is a named state, not `None`.
5. **View.** `queue_view` and `/build:queue` show cloud rows in their own group, with slots used of the total. Each row shows unit, step, state, modules done of total, vCPUs and elapsed time. A row with no update for `cloud_gate_stale_pushes` push intervals shows its age. `cloud_gate_stale_pushes=3` is a new key in `config/build_admission.conf` (builds, 2026-11-07). The push interval is aws's key `log_push_s=5` in `config/cloud_gate.conf`; the view reads it with `conf.read("cloud_gate").number("log_push_s")`; aws-unit names `conf.read_or_repository`, which is on builds-aws only (aws-unit correction, 2026-11-07). Until aws's route lands, a missing key is a named state and the view shows no stale age.
6. **The HTML page.** The memory page shows the same cloud group. It changes the page files after the legend change ends (the same files).
7. **Fallback.** A gate that falls back to natedev ends its cloud row and admits again as a local gate.
8. **Proof.** Tests by behavior: a cloud admit charges no memory; the tenth cloud gate waits and names its wait; a dead route frees its slot; the view and card show the progress, the missing-file state and the stale age. Goldens for the card and view. Change-Review: a fake cloud row on a private service shows on the card.

**Files:** `scripts/build_queue/admission_protocol.py`, `admission_client.py`, `admission_service.py`, `cloud_progress.py` (new), `queue_view.py`, `queue_card.py`, `scripts/lint/memory_admit.py`, `config/build_admission.conf`, `scripts/lint/fixtures/build_admission.conf`, their tests and goldens, `docs/as-built/build-memory-admission.md`.

**Workers:** `1 writer + 1 tester`.

**Acceptance gate:** the merge gate; basedpyright 0/0/0 on each changed file; adversary and contract reviews.

### Phase 27 — The build memory page feed is a boot service  · status: done

**Depends on:** the memory page legend change now with the impl worker (the same two files). 10m keeps its other items.

1. **The ask (user, 2026-11-07).** "we need to make this a service that runs outside of agent shells - a service that starts on boot so probably gets added to nixos". Today the feed runs in a queue-unit shell from the session folder, and it stops when that shell stops.
2. **The files move into the repository.** `memory_feed.py` and `memory_live.html` move from the design folder to `scripts/build_queue/`. This is 10m item 2, pulled forward. The open command and its name stay in 10m.
3. **Config.** The new file `config/memory_feed.conf` holds the output folder (the review link folder for `h7uj5c`) and the update interval (3 s). The feed reads both at start. No path or interval stays in code. All keys are new (builds, 2026-11-07: a new key needs no go; a moved or changed key does).
4. **The service.** `/etc/nixos/modules/linux/development.nix` gets `systemd.services.build-memory-feed`, next to `build-admission`. It starts at boot (`wantedBy = [ "multi-user.target" ]`), runs as the user, restarts on failure, and has `ConditionPathExists` on the feed script. It runs outside `builds.slice`. The nixos change goes on a branch; the branch and hash go to builds, and natedev pushes it.
5. **Cut-over.** After the deploy, the queue unit stops its shell copy of the feed. One feed writes the folder.
6. **Proof.** The feed's tests pass from the repository path, with a test for the config read. After the deploy, `systemctl status build-memory-feed` is active, and the page's update time moves. Change-Review: the page updates with no agent shell running.

**Workers:** `1 writer`.

**Acceptance gate:** build_queue suites pass; basedpyright 0/0/0; `nixos-rebuild build` passes on the branch.

### Phase 28 — A reservation matches the memory a run really uses  · status: done

1. **The ask.** The user, 2026-11-07 17:30: "this is completely busted - we've been running with about 1.2GiB reserved for merge gates for the last few days - this is completely unacceptable", and of a hana build "reserving 15.6GiB and using 0.2GiB - it is ridiculous".
2. **Facts found.** The need for a run with progress is the highest anonymous use any recorded run of the same key had in the next 20% of progress, over 14 days (`progress_profile.merge_profiles`, `HISTORY_DAYS`). Two full merge gates today recorded 17.3 GiB (builds, 16:28) and 16.0 GiB (herdr, 16:58) in one 5% bin. Their own gate samplers saw at most 2.8 GiB for the whole job tree. The other 263 gate runs have a median of 0.9 GiB in those bins. The director moved the two records to `$SD/evidence/bad-gate-profiles-20261107/` (17:33). Hana `build:1` profiles show 10 to 15 GiB at the end of a build.
3. **Cause, as found (research 17:50).** Hana's memory is real: one live hana scope read 5.89 GiB `anon` and 5.89 GiB summed process `RssAnon` (difference under 1 MiB), almost all in one `rustc`; the same build later reached 10.9 GiB. The gate outliers are not explained by the saved records, which hold no process list or reading source. The likely cause: test helpers that leave the gate's process tree stay in its scope; the scope reader (`memory_admit.py` `slice_working_set`) counts them and the gate sampler (`merge_gate.py`) does not. To prove it, each saved profile records its reading source, and at the run's highest bin the scope's processes (pid, command, `RssAnon`). The next outlier then names the process. Fix its lifetime where it lives in a later phase.
4. **One run never sets the need.** Per bin, the need uses the Nth-highest recorded run, with N from a new config key `progress_profile_rank` (start 2). A key with fewer than N runs uses its lowest run in that bin. Director's call: N=2 keeps hana's end at 12.9 GiB (3 runs: 15.2, 12.9, 10.2) and stops any one bad run; the risk is a run 2.3 GiB over its reservation.
5. **Proof.** The two moved records, put back in a test copy, give a gate need under 3 GiB. A replay of today's live profiles shows each step's need before and after.

**Workers:** 1 writer, 1 tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review.

### Phase 29 — A gate with no running module waits in line for a CPU slot  · status: done

The stall of 2026-11-07 18:20 to 19:20: six merge gates were admitted by the memory queue. The fix-rounds gate ran 60 minutes with no module, while a gate that started 30 minutes later held 11 CPU slots. Each gate takes a slot from the shared pool `/dev/steve` (32 slots) before each module (`merge_gate.py` `SharedCPUPool.try_acquire`). When no slot is free, the gate sleeps 0.05 s and tries again, and it logs nothing. cargo and rustc wait in a blocking read, so the pool gives them each freed slot first. Builds ruled (a) and the request timing on 2026-11-07 19:30; it ruled out a cap on full gates.

1. **Wait in line.** A gate with no running module and a pending module waits in a blocking read on the pool, in line with cargo. A gate with a running module keeps its present non-blocking try, so it still samples memory and collects results.
2. **One wait line.** The gate prints `waits for a CPU slot` once when such a wait starts, and the wait time when it ends.
3. **Signals.** A stop signal during the wait stops the gate at once and returns every slot it holds. A slot read just as the signal comes is returned.
4. **Request timing.** `admission_service.py` logs the wall time and the CPU time of each request that takes longer than a configured limit (new key; start 0.25 s). The log line names the request kind. The next "no answer in 0.5 s" then shows whether the time goes to CPU work in the service or to a wait.
5. **Proof.** Tests with a fake pool: a gate with no running module blocks and starts its module when a slot comes back; the wait line prints once; a stop signal during the wait returns the slots. A service test logs a slow request and logs no fast one.

### Phase 30 — A build target command does not load the admission service  · status: done

Builds asked on 2026-11-08 (verify 41p facts; the build_hold import chain item of 11-06). `build_targets.py` eligible, reserve and release-wait import `admission_service` through `mac_test.py` → `build_hold.py`. `build_hold.py:38` imports it at module top, and its only use is `admission_service.read_state` in `admitted_processes()` (`build_hold.py:567`). `admission_service` loads records, protocols, estimators, queue snapshots and the memory admission code at module top; the estimator loads SQLite and the build-log modules. The import costs eligible 310 ms, reserve 203 ms and release-wait 267 ms wall (about 110 ms under load).

1. **build_hold.** `build_hold.py` no longer imports `admission_service`. `admitted_processes()` reads the service state through the smallest module that holds `read_state` and `ServiceState`. If those two move out of `admission_service.py`, `admission_service` imports them from there; one definition stays. If a move costs more than it saves, import inside `admitted_processes()`.
2. **admission_service top-level imports.** Find each top-level import that only a rare path uses. Move it to its user when the gain is measurable for a service client or `build_targets.py`. Do not change the service's hot path.
3. **Measure.** CPU time (user + system) and wall time per `build_targets.py` start for eligible, reserve and release-wait, before and after, at the same load: interleave the runs, 15 each. Also `python3 -X importtime` for the top modules.
4. **Proof.** A test proves `build_hold` and `build_targets` import without `scripts.build_queue.admission_service` in `sys.modules`. The existing build_hold, build_targets and admission service tests pass.

### Phase 31 — A build target command starts with only the imports it uses  · status: done

After Phase 30, `build_targets.py` eligible, reserve and release-wait still use about 530 ms of CPU per start. Builds asked on 2026-11-08: trace the rest now; base the measure and every edit on verify phase 41p, which changes `build_targets.py` imports (ETA 06:30 on builds). The queue unit edits only queue-owned files and sends facts on other owners' files to builds.

1. **Trace.** `python3 -X importtime` and a CPU profile of each command's start. List each import over 10 ms cumulative: the module, its cost, which caller pulls it in, whether the command uses it, and the owner of the file that imports it.
2. **Queue-owned edits.** For each queue-owned module on the path that the command does not use, move the import to its user. Queue-owned: `scripts/build_queue/`, `scripts/lint/memory_admit.py`, and the files earlier queue phases changed.
3. **Facts to builds.** For each import in another owner's file, send builds the module, its cost and the proposed move.
4. **Measure.** CPU (user + system, children counted) and wall per start, 15 interleaved runs before and after, on top of 41p.
5. **Proof.** An import test for each moved import. The existing tests of each changed module pass.

### Phase 35 — Cloud gates are work rows on /build:queue and the build page  · status: moved to report (11-08)

The user approved aws-unit's design on 2026-11-08. Builds moved it to report, which starts after Phase 36 lands, because both edit memory_feed.py and memory_live.html.

### Phase 41 — Every natedev process that talks to the Mac ends when the link dies  · status: done

**Depends on:** none.

1. **The ask (user, 2026-11-09).** "these connections should be self-healing". Mac cleanup must never need the user again. At 13:40 the Mac went off during a floor sweep. `scripts/mac_test/floor_claims.py` held `~/.local/state/mac-test/state.lock` for more than two hours, four build steps waited, and the user had to kill the process.
2. **Goal.** When the Mac drops, every natedev process that talks to it ends by itself within a short bound, and every lock or claim it held is free with no person. Bounds come from config. The design adds no signal handlers and no watchdog process.
3. **Cause (checked 2026-11-09).** `ssh -G mac` shows `ServerAliveInterval 15` and `ServerAliveCountMax 3`, set under `Host *` on both machines, so a dead outbound link ends in 45 s. `ConnectTimeout` has no shared value. The 13:40 hang ran inbound: the Mac opened ssh to natedev, and `floor_claims.py` held `state.lock` in `sys.stdin.read()`. Nothing on natedev checks that an inbound client is alive. The stdin wait is an inference from the code, because the process and its journal are gone.
4. **Lock (verify).** Verify's phase 42c, tip `e990e8be7`, releases the lock before the print and removes the stdin wait. Queue adds no lock code.
5. **One place for link bounds (natedev).** `ConnectTimeout 10` goes under `Host *` in `/etc/nixos/modules/linux/ssh.nix` and `modules/darwin/ssh.nix`. Natedev removes the per-call value in `rebuild.nix` and `remote-desktop.nix`.
6. **Callers outside `scripts/mac_test` (queue).** Remove `-o ConnectTimeout` and `-o ServerAlive*`. Keep each whole-call limit and `BatchMode`. Delete the two config keys that set the same bound, `codex_update_connect_timeout_seconds` and `role_map_connect_timeout_seconds`. The user approved the deletion through builds on 2026-11-10. `scripts/buildlog/sync.py` chooses its options by destination host, from `sync.hosts.mac` and `sync.hosts.natedev`, so an AWS destination keeps its connect option. Land only after item 5 is live on natedev.
7. **Callers in `scripts/mac_test` (verify).** The same removal, on top of 42c (tip `e990e8be7`), after queue tells verify that the shared `ConnectTimeout` is live.
8. **Inbound check (queue).** The check listed each command the Mac runs on natedev. Besides `floor_claims.py`, one command held a lock while it waited on the link: `staff.py apply-seed` read stdin inside the staff operation lock. It now reads stdin before it takes the lock. An offload `mirror-receive` holds its mirror lease across the rsync pipe only when natedev is the target, which the loopback proof alone does.
9. **Retained remote claims stay.** A claim stays while the Mac is off and frees `claim_idle_minutes` after the Mac returns, with no person. Verify found that the 1 h 52 min wait was the same held `state.lock` as the 13:40 case, from 13:34:30 to about 15:52:38, so no claim change is necessary.

**Workers:** 1 writer for items 6 and 8. **Gate:** targeted test, basedpyright 0/0/0, adversary review.
**Change-Review:** none — a Mac link that dies ends the same way; only where the bound is set changes.

### Phase 40 — The admission client starts with only the wire format  · status: done

**Depends on:** none.

1. **The measurement (builds and natest, 2026-11-09).** One whole gate starts `scripts/build_queue/admission_client.py` 910 times, at 142 ms of CPU per start and 129 CPU seconds in all. `admission_protocol` and `admission_records` take 58 ms of its 87 ms warm import. Report: `/tmp/claude/unit/2113de4f-9913-485a-9a51-61558bfd81db/phases/rank113/whole_gate_report.md`.
2. **Fix.** The client path that a `verify.sh` run takes imports only the wire format. The target is about 60 ms less per start. Code that needs the record types, such as reading the queue view, imports them when it runs.
3. **Unchanged.** The command line, the bytes on the socket, exit codes and printed text.
4. **Measure.** CPU (user + system, children counted) and wall per start, 15 interleaved runs before and after, warm, on the same command.
5. **Proof.** A test that the client path starts without loading the record types, and fails without the fix. The existing tests of the changed modules pass.
6. **The feed restarts itself (builds and natedev, 2026-11-09).** `installed_code_version` in `memory_feed.py` hashes only the admission sources, so an install that changes the feed's own modules leaves the old feed running. The fingerprint now also covers each Python module the feed loaded under the install root, and each other file it reads once at start. An install that changes one of those files makes the feed exit, and systemd starts the new code. A listed file with a modification time after the feed's import started also makes the feed exit. A row in the existing fingerprint test proves it and fails without the fix.

**Workers:** 2 writers and testers, one for the client and one for the feed. **Gate:** targeted test, basedpyright 0/0/0, adversary review, the measured times.
**Change-Review:** none — verify runs start faster, and nothing visible changes.

### Phase 39 — The build page shows step names from the config  · status: done

1. **The defect (builds, 2026-11-09; change review C1285 returned).** The build page shows `merge-gate`, not `merge gate`. `scripts/build_queue/memory_feed.py` passes `RECORDED_STEP_NAMES`, an empty map, at five places: `step_text` for swap holds, held rows, running rows and waiters, and `names=` on the `QueueModel`.
2. **Fix.** The feed calls `read_step_names()` once for each reading and uses that value at all five places. `RECORDED_STEP_NAMES` leaves `memory_feed.py`.
3. **Unchanged.** Comparisons of `step.step` with raw step names keep the raw name. `/build:queue` already reads the config.
4. **Hover note.** `progressTip` in `memory_live.html` tested the row name for `merge-gate`. A row name is shown text and can hold a project, so the ordering note now tests `progress.unit === "modules"`.
5. **Proof.** A row in the existing feed test: a `merge-gate` step shows as `merge gate` in running and waiting rows. The test fails without the fix. The existing expected values that use `RECORDED_STEP_NAMES` change to the config names. Rows in the existing hover test: a `modules` row has the ordering note, and a row with another unit has none.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review, page proof.
**Change-Review:** The build page shows "merge gate" | On natedev, run /build:page and open the link while a merge gate runs or waits | The row says "merge gate", with a space and no hyphen. | after restart

### Phase 38 — AWS rows on the build page show a progress bar  · status: done

1. **The ask (user, 2026-11-09).** "the word \"running\" is superfluous on the aws rows - if they are there they are running - i would only want to see issues if they are \"not running\" when they should be." The module count "should be small text underneath a progress update bar - similar to the progress update bars on local rows."
2. **Bar.** A cloud row with a module counter draws the same progress track and line as a local row, in the testing color, in the same layout pass. The feed gives the cloud row the `progress` field that local rows have: `RowProgress` with phase `testing` and unit `modules`, or `NoProgress` with the counter's reason. The cloud counter's phase is `modules`, so `row_progress` does not map it as it stands.
3. **Text.** Small text in the row's `.amounts` line, under the bar: the module count, then the failed-module count when it is above zero, then "no update for <age>" in the cap color when the row is late. The word "running" never shows. Another state (`requesting`, `setup`, `uploading`, `progress missing`, `progress unreadable`) shows first, because the row is not running. "modules unknown" never shows.
4. **Waiting rows.** A cloud row that waits for a slot draws no bar. Its reason stays as the same small text.
5. **Hover.** The row tip keeps the machine and reservation sentence, and it adds the local rows' progress line. A row with no text shows no stray period.
6. **Not in this phase.** The `/build:queue` terminal table keeps its text. The user named the page.
7. **Proof.** A row in `test_cloud_rows_share_the_feed_without_memory_reservations` for each state and for a row with no counter. `memory_page_proof.mjs` checks the bar width, the text under the bar and the absence of "running", and it writes a shot.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review, page proof.
**Change-Review:** AWS rows show a progress bar | On natedev, run /build:page and open the link while a cloud gate runs | Each AWS row has a progress line with small text such as "443 of 702 modules" under it, and the word "running" is gone.

### Phase 37 — Build folders delete themselves when their process ends  · status: done

1. **The rule (user via builds, 2026-11-09).** Our processes clean up after themselves, as `tempfile.TemporaryDirectory` does. Source: `/tmp/claude/leftover_sources_2026-11-09.md`, section "builds", item 2; detail in `/tmp/claude/leftover_sources_2026-11-09_detail.txt` lines 321-400.
2. **Python callers.** Each bare `build_folders.create_build_folder(...)` caller whose folder only its own process uses moves to `with build_folders.temporary_build_folder(...) as path:`. Known callers: `scripts/production/merge_gate.py:1236`, `scripts/lint/style_review.py:183`, `scripts/style/style_sync.py:371`. Find every other caller with `rg -n 'create_build_folder|build_folders\.py.*(unique|stable)'`.
3. **verify.sh folders.** `scripts/direct/verify.sh` makes three private folders with the `unique` CLI: verify-tree, verify-copy and verify-hold. The existing EXIT and signal traps remove each one on every handled exit, so `verify.sh` keeps them. A trial that ran verify under `build_folders.py run` changed its process ID and signal path and failed 22 test modules, so it was removed. Decision: director, builds agreed 2026-11-09.
4. **Folders that must outlive their process stay.** Example: `scripts/hooks/session-start-temp-root.py:56` (session TMPDIR), `scripts/direct/prepare_session.sh:31` (unit session folder), `scripts/lint/invoke.sh:19` (`stable --path "$TMPDIR"`), `verify.sh:1281`. Each such call gets a one-line comment that says why the folder outlives its process.
5. **Not in this phase.** Add no check, scan or audit. Add no signal handler (TMPDIR is cleared at reboot). `copies.py` belongs to verify; `memory_page_proof.mjs` belongs to report. If the `unique` CLI has no caller left, remove it; otherwise keep it.
6. **Proof.** Rows in existing tests: a caller's folder is gone after the caller returns and after it raises. For verify.sh, the existing verify tests still pass, and the three folders are gone after a verify run ends.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review.

### Phase 36 — Swap by group adds up to the swap bar  · status: done

1. **The ask (user via builds, 2026-11-08).** The bar said 23.8 of 38.2 GiB swap used; the groups summed to about 0.3 GiB. Add a Total row so the rows add up to the bar, show the real owner of the gap, and give the three "between steps" rows labels that tell them apart.
2. **Cause (measured 11:2x EST).** The groups list only build-queue steps. memory.swap.current by cgroup: user.slice 24.3 GiB, of which app.slice/app-agents.slice 19.3 GiB across about 187 agent session scopes (0.3-0.4 GiB each); other app.slice 3.1 GiB; builds.slice 0.88; session.slice 0.69; system.slice 0.27; hana.slice 0.19. SwapCached is 15 MiB.
3. **Rule.** The feed reads swap by owner from the cgroup tree, from the same reading as the bar. Build steps keep their rows. Other swap goes to named owners: agent sessions (count), other apps (by app), desktop session, system services, hana CI. The rest, not charged to any group read, is one row "not in a group". A Total row equals the bar's used value.
4. **Labels.** A held reservation with no current step names its owner as the row name and "between steps" as the detail.
5. **Both views.** The build page and any text view of swap groups show the same rows and Total.
6. **Proof.** Tests: groups plus "not in a group" equal the used value; agent scopes group into one row with a count; a between-steps hold names its owner.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review, page proof.

### Phase 34 — One command prints the build page link  · status: done

1. **The ask (builds, 2026-11-08).** Report Phase 26 needs a command that returns the memory page link. Phase 10m built the feed and page but no command. The user named it `/build:page` (2026-11-08).
2. **The command.** `commands/build/page.md` runs `scripts/build_queue/memory_feed.py link`, which prints the page URL from `output_folder` in `config/memory_feed.conf`.
3. **One URL rule.** The URL comes from the review-link rule in `scripts/review_links/review_link.py` (host, port, path), through one shared function, not a copy.
4. **Proof.** A test: the printed link names the configured folder and `memory_live.html`, with the review-link host and port.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review.

### Phase 33 — Runaway runs never set a progress bin  · status: done

1. **The defect (user, 2026-11-08 09:55).** Merge gates kept 15.3 GiB and blocked the queue. Phase 28 progress bins take the 2nd-highest run (`progress_profile_rank=2`). Two runaway gate runs (25.4 and 16.5 GiB, the verify.sh trap defect) were in one key, so every bin took a runaway value for 14 days. The director purged both records by hand (backups in `~/.local/state/buildlog/admission-purge-2026-11-08/`).
2. **One rule for peak history (builds, 2026-11-08).** A ranked pick takes the Nth-highest value with N = max(floor, share of the values), capped at the count. The share is a new key in `config/build_admission.conf`, starting at 10%; no share value is in the code.
3. **Progress bins** use floor = `progress_profile_rank`. **Program peaks** (the Phase 32 p90 and the other `step_estimator.percentile` callers) use floor 1, which gives the same result as today's p90 at a 10% share.
4. **Proof.** Rows in the existing tests: two runaway runs in one key do not set any bin; a key with few runs still uses `progress_profile_rank`; the program p90 result does not change at 10%.

**Workers:** 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review.

### Phase 32 — A merge gate claims the memory its runs used  · status: done

1. **The ask (builds, 2026-11-08 06:50).** `step_estimator.py:244` `program_peak_estimate` returns `FirstProgramPeak` for every `merge-gate`, so admission claims the fixed 4 GiB. A herdr merge gate peaked at 26.4 GiB anonymous while admission counted 4 GiB; natedev went into earlyoom and hana's gate was killed twice. Evidence: `/tmp/claude/unit/20523639-3e08-48d6-9277-73e052bca297/phases/83/memory_kill_report.md`.
2. **Measured claims.** A merge-gate claim uses the measured anonymous peak history of its host, as other programs do. The fixed value applies only when no history exists.
3. **Outliers never set the claim.** The claim is the 90th percentile of the host's merge-gate peaks in 14 days, the rule other programs use. The research (phases/32/notes/research.md) withdrew the first rule, the 2nd-highest peak: 1,348 of 1,352 rows are 4.5-4.8 GiB, and 4 runaway rows (16.7-26.4 GiB, all after 2026-11-07 21:00) would set every gate's claim near 21 GiB for 14 days. The p90 is 4.65 GiB. The gate keeps itself near its own 4 GiB start limit (`merge_gate_governor_gib`), so a runaway test module is a gate defect, not a sizing input: verify fixes the tests, and a module memory limit in `merge_gate.py` belongs to its owner.
4. **tmpfs is not in this phase.** The builds slice held up to 27 GiB of shmem in the same minutes, and no reservation counts it. Builds ruled (2026-11-08): `scripts/lint/memory_admit.py` is a resources file, so resources adds shmem to the slice's used memory. Verify fixes the two runaway tests.
5. **Research first.** Why `merge-gate` was excluded (`a4d4c9988`, Phase 10o), where merge-gate peaks are written, and how gate claims and Phase 28 progress profiles relate.
6. **Proof.** Tests: no history gives the fixed value; history gives the Nth-highest; one runaway peak does not set the claim. A replay of today's records shows the herdr gate claim before and after.

**Workers:** 1 researcher, then 1 writer and tester. **Gate:** targeted test, basedpyright 0/0/0, adversary review.

### Phase 10m — The build page is a repository tool and shows leftover-process ages  · status: done

**Depends on:** 10z (the page reads the room evidence 10z publishes) and report-unit Phase 15 on builds (builds ruling, 2026-11-05). report Phase 15 builds one typed provider, `charts.page_data(days, n=3) -> PageData | IndexUnavailable`, and starts at once. report-unit waits for 10m only in its Phase 26.

1. **The ask (user, 2026-11-04, change reviews C959 and C969, through builds).** C959: "we are moving to the new build html page - we will use /build:queue for reference until we can retire it". C969: leftover-process ages on the queue board, "this should be the case on the new html page too".
2. **The page.** The Build Memory page (memory_live.html, fed by memory_feed.py) is served by review link h7uj5c. Phase 27 moved the page, the feed and the hover test to `scripts/build_queue/`, and the boot service `build-memory-feed` runs the feed from there. 10m adds one command to open the page. The command name needs the user's approval. New board features go on this page. report-unit's cost and chart views land on this page too, not on a second one.
3. **report-unit's sections (report-unit, 2026-11-04; C950 and C948).** The page gets two sections: the build history charts and a top-3 cost list. report-unit owns the data: one function in `scripts/buildlog`, `charts.page_data(days, n=3)`, returns typed data for both sections, no HTML, read in under 1 s for 7 days (builds ruling, 2026-11-05). The feed calls the function and the page draws its data. There is no JSON file between them.
4. **Leftover-process ages.** The page shows each leftover process with the same readable age as the queue board ("45 min", "19 h", "2 d"), from the same formatter.
5. **/build:queue.** It gets no new work. It stays for reference until the page covers it; 10n retires it.
6. **Wait states.** The page shows the three wait states the service records: unreadable pressure (with its path), start spacing, and head protection. 10z keeps the pressure text and `ReadingAlertTests`; 10h owns holder names.
7. **Proof.** The feed's tests pass, with feed and page tests for each of the three wait states. A pixel check of the page shows the cap, the room, the leftover ages and report-unit's sections. Each item returns as its own Change-Review row.

8. **Partial readings (architect review of 10q, 2026-11-05).** The feed reads each part of the page on its own. When one part is unavailable (the cap, pressure, a run's use), the page keeps the other parts and names the missing one. Use named availability states, not a bare `None` (today `memory_feed.py:109` stores missing pressure as `None`, and `MemoryPage.cap` uses `None`). 10s already keeps the rows with no cap line (`memory_feed.py:295`, `memory_live.html:354`); this item replaces the `None` fields. Acceptance tests: with the cap alone unavailable, the page keeps its rows, the room and the reason text, for both `CapUnavailable(runs=())` (an old record) and named unavailable runs.
9. **Sources.** The prototype is in `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/design/memory-view/` (`memory_live.html`, `memory_feed.py`, `test_memory_hover.py`). Phase 27 moved them to `scripts/build_queue/` (`memory_feed.py`, `memory_live.html`, `test_memory_feed.py`, `test_memory_hover.py`); they stay there, because the boot service runs that path. The feed takes `--code-root`, reads `captured.ci_use`, `captured.cap_use` and `room_text(captured)`, and imports `snapshot_codec`; its hover shows phase, percent done, keep-aside and reason. `memory_feed.pre10x.py` is deleted at the 10x deploy and does not move. The feed makes one call to report Phase 15's `charts.page_data(days: int, n: int = 3) -> PageData | IndexUnavailable` (report e913e5fa2; types in `charts.py`; 0.42-0.48 s for 7 days on the live index). `PageData(charts: ChartSet, top: TopCosts)`. `TopCosts(start, end, recent_days, rows: tuple[TopCost, ...] | NoDataYet)`; `start` and `end` bound the recent window in the user zone. `TopCost(machine, cause, recent: ChartCost, window: WindowValue, action: CostAction)`; `WindowValue = WindowMatchesRecent | WindowDiffers(value: ChartCost)`; `CostAction(action, owner)`. The feed checks that one result: on `IndexUnavailable(reason)` the page shows the reason in place of both sections, and `NoDataYet` shows an empty top list with its window. `TOP_COST_RECENT_DAYS` greater than `days` raises `conf.Unusable`: the feed passes `days` of at least `TOP_COST_RECENT_DAYS` and shows a config error as the page reason. 10m starts only after report Phase 15 is on builds (it lands with report 24-25, after a user answer); builds says when. The item 7 pixel check uses their data.
10. **Prototype behavior to keep (2026-11-06).** The Running table shows a Machine column after Duration and before Unit, from `scripts.buildlog.store.host_name()` (page round 16, the user's ask). When one view read misses the client's 0.5 s limit, the feed writes the last good reading plus `read_error` and `error_at`, and the page keeps its data; only the first read of a feed with no good reading shows "The queue did not answer" (page round 17, the user's blank-page report).
11. **Browser proof (architect review of 18, 2026-11-06).** The browser proof today is `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/phases/adhoc/memory_page7/verify.mjs`: Machine column order, a first read with no answer, a kept reading, and the stale-reading messages. It moves with the page, and the tester owns it.
12. **Feed restart at install (architect review of 10s, 2026-11-05).** A running feed keeps the decoder it loaded, so a schema change breaks it until it restarts. The feed notices when the installed admission code changes and exits with a failure status; the boot service (`Restart=on-failure`) starts it again with the new decoder. Test: a feed that started before a schema update reads the new record after the restart.

13. **Project names.** The feed prints each running and waiting step with `queue_lines.step_text`, not the raw step (`memory_feed.py:398` and `:471` today). Feed tests: a known project, a missing project, and an absolute path repository that shows the step alone.
14. **Progress line and row list.** The running rows keep the thin progress line. A browser test keeps a walk whose row list shrinks between two readings.
15. **Page defects from the design checks of 2026-11-06.**
    1. The unit shows twice in a row: remove the second line of the name.
    2. "not recorded / 25%" and "not recorded / 5%" wrap: keep them on one line.
    3. The swap list's second label sits 3 px above the main baseline.
    4. The swap list has 31 rows: show the large rows and sum the rest in one line.
    5. Swap use shows three times: keep the Swap used bar with its limit, put the GiB figures on its line, and remove the purple bar.
    6. Hatched "Reserved, not used yet" blocks reach 2.06:1 in light and 2.61:1 in dark; the floor is 3:1.
    7. Small grey swap tags reach 4.18:1 in light; the floor is 4.5:1.
    8. Rows sit 6 px inside the card edges: align them with the card text.
    9. A queued reservation value ends at x=351 against x=302 above: use the dim style.
    10. A missing unit shows a bare dash: remove the dash and the dot.
    11. The Unit and Pid columns move 39 px when a step queues: reserve the Duration width.
    12. The lower axis runs past the cap: end it at the last tick before the cap.
    13. "0.2 GiB · 1.1 GiB" has no header: add "Used · reserved".
    14. Open Legend values sit about 850 px from their labels: put them in a column near the longest label.
    15. "Next step room" puts its value on a second line: keep it on the first line, right-aligned to the label column end.

**Workers:** `impl` owns `scripts/build_queue/memory_feed.py` and `scripts/build_queue/memory_live.html`; `test` owns `scripts/build_queue/test_memory_*.py` and the browser proof from item 11. report-unit owns `charts.page_data` in `scripts/buildlog` and its tests (report Phase 15).

### Phase 10n — /build:queue retires when the build page covers it  · status: moved to report (11-08)

**Depends on:** 10m.

1. **The ask.** C959 (above): retire /build:queue once the page covers it.
2. **The change.** List what /build:queue shows and confirm the page shows each item, the 10m wait states included. Then remove the view routes and the view code that only they use. `build_queue.py` also carries the `bump`, `clear` and `admit-head` controls, which the page does not have: these routes stay until the user approves a replacement (architect review of 18, 2026-11-06). Keep each formatter that a current caller uses: the card uses queue_view formatters. `admission_service.py:32` imports `reason_phrase` from `admission_records`, and `queue_view.py:10` only re-exports it, so that re-export is no reason to keep view code (architect review of 15 and 10o, 2026-11-06). Keep `queue_lines.step_text`: the card and the build page use it. The queue card and the unit-board queue pane stay unless the user says otherwise.
3. **Proof.** No caller of the removed code remains; the suites pass.

**Workers:** `1 writer + 1 tester` — `impl` owns `commands/build/queue.md`, `scripts/build_queue/build_queue.py` and the view code that only it uses; `test` owns `scripts/build_queue/test_build_queue.py`, the golden files it alone reads and the suites of the shared formatters.

### Phase 10v — Every run in builds.slice asks the queue before it starts  · status: not landing

**Not landing (user, 2026-11-06):** "local python testing for units seems as if we should just allow that - and it shouldn't be part of build quueue". A unit's local Python test runs start at once. Cargo steps and gates keep their queue places. Detached Python runs move to `builds-detached.slice` in Phase 23.

#### As-built

- No entry point exists. A plain Python run in `builds.slice` does not ask the queue, and its memory lowers the cap and the room through Other.
- The `targeted test` name for a unit's own full run is in natest's work: `program_first_peak_gib.targeted-test=4`, `peak_hold_seconds.targeted-test=180`.

**Binds later work:** the detached run group phase keeps detached Python runs outside the queue. Do not route a plain Python run through the queue.

**Ruled out:** one entry point that every `builds.slice` run must pass. User, 2026-11-06: "local python testing for units seems as if we should just allow that - and it shouldn't be part of build quueue".

### Phase 1 — /build:queue answers in under a second, one line per step, and says why each step waits  · status: done

#### As-built

- `build_queue.py [--columns N] [--json] [<selector>]` and `build_queue.py bump <selector>`. No word prints the view, one word prints the card, `bump` keeps its refusal (`memory_admit.UnmatchedWaiter` on stdout, exit 3); read errors exit 2. `--columns` defaults to a positive `COLUMNS`, else the stdout terminal width, else 100.
- The read path reads the queue once (`memory_admit.locked_queue_read()`), then `head_facts.read_head_facts`, then one `unit_labels.unit_labels_for(ids, os.environ)` call with every shown id (running, waiting, `head_facts.session_ids`). No full roster build, no thread pool, no `Roster:` problems line. Live: 0.4–1.2 s at load 100–135, against 4.7 s before.
- One line form: `queue_lines.Span(text, tone, bold)`, `Tone = Literal["default", "success", "error", "warning", "inactive"]`, `Line = tuple[Span, ...]`. Text output is `plain(lines)` (no Markdown table); `--json` is `json_form(lines, columns, as_of)` → `{"columns", "as_of", "lines": [[{"text", "tone", "bold"}]]}`.
- `queue_view.render_view(model: QueueModel, columns)` (Mock A): header with the head's state, waiting count per production, running rows, move lines (`↑ #<n> moved up: from #<old> by <actor> at HH:MM`, at most 5), one row per waiting step with `↑<old place>` and a `starts` estimate, the `#1 waits on` ✓/✗ block from `HeadExplanation` through one `check_lines` function, and the rest of the queue said once (`rest_lines`, `reason_phrase`). Width rules: 80–99 drop the production part, under 80 drop `starts`, then cut the name tail with `…`; a step never becomes a record.
- `queue_card.render_selection(model, selector, columns)` (Mock B): `select_waiters` matches shown name, then waiter rows; one card per matched waiting step whose first line answers `Next?`; a running match prints one `runs now` line; no match prints `No waiting or running step matches <selector>.` (exit 0).
- `head_facts` reads bump rows back to the oldest waiter's `since` as `BumpRecord`; a row with `bump_order < 0` shows its earliest move's `old_position`. `read_pace(now)` counts this host's `admission/anon_peaks.jsonl` lines from the last 60 min; place `p` starts in `round(p * 60 / pace)` min, at least 1; no pace prints `—` and the footer `starts: no step ended in the last hour`.
- The pane (`queue.tsx`) only paints one `Text` per span; `register.tsx` runs `--json --columns <n> [selector]` and asks one forced refresh when the pane width differs from `data.columns`. `QueuePaneState` keeps `selector: string | null` in the loading, queue and failed states, so a reload keeps the card.

**Files:**
- `scripts/build_queue/build_queue.py` — CLI words, `--columns`, `--json`, the one read and label call
- `scripts/build_queue/queue_lines.py` — spans, its own cell width, pad, `cut_tail`, `clip`, `wrap`, `merge_spans`, `plain`, `json_form`
- `scripts/build_queue/queue_view.py` — `QueueModel`, `step_checks`, `ChecksNotRecorded`, `step_labels`, `render_view`
- `scripts/build_queue/queue_card.py` — `select_waiters`, `render_card`, `render_selection`
- `scripts/build_queue/head_facts.py` — `BumpRecord` scan, `moves_by_step`, `queue_moves`, `read_pace`
- `scripts/build_queue/queue_fixture.py`, `golden/{view_80,view_100,view_140,card_1,card_4}.txt` — fixed 10:46 queue and byte-exact renders
- `scripts/message/unit_labels.py` — `UnitLabels`, `unit_labels_for` and file-only helpers; `roster.py` imports them
- `scripts/production/unit_marks.py` — session unit marks moved out of `unit_lookup.py`
- `scripts/herdr/place.py` — `HerdrPlace`, no herdr import; `herdr.py` imports it
- `mods/unit-board/hooks/queue.tsx`, `hooks/register.tsx`, `types/queue.d.ts` — pane paint, run args, width refresh, `QueueLines` type
- `mods/unit-board/tests/kit.ts` — one drawing inspector shared by queue and accounts tests
- `commands/build/queue.md` — runs the script, prints output in a `text` block; moving a step up is the user's, through `/showrunner:bump`

**Binds later work:**
- `QueueLines` is the one line form for text, JSON and pane; new output goes through `Span`/`Line`, never a second renderer.
- `queue_view.step_checks` returns `ChecksNotRecorded` for every step behind #1, so those steps print no budget or free-memory line. The lock-free queue snapshot work replaces it with `LineWaiter` checks and also brings `queue_read`, `match_waiters` and `queue.json`; none exists yet.
- The pane `selector` survives reloads in pane state; the selection-union work renames it to a `QueueSelection` union and converts the old saved form at load.
- `unit_labels_for` loads no herdr, role_map or design_roles (`unit_marks.py` split from `unit_lookup.py`, `place.py` from `herdr.py`, a lazy `unit_lookup` import in `production_units.py`); `test_unit_labels.py` holds this.
- `--json` stays at most 3 times the text size (Phase 1b).

**Gotchas:**
- `build_queue.py` and `build_why.py` still take the admission lock through `locked_queue_read()`; 3 of 12 live runs went over 1 s until the snapshot work lands.
- `build_why.py` keeps its own `_terminal_width`; `queue_lines.terminal_width` is a second cell width until the snapshot work joins them.
- `queueColumns` and `queueWidthRefresh` are module-level in `register.tsx`, not pane state: a mod reload resets them to 100 and idle.
- The mod runs the installed `~/.claude/scripts`; a live pane check works only after the change is on main.
- A fresh-checkout `tsc` needs the engine-written types folder copied in.

**Ruled out:** a `--wide` mode (the card holds every fact of one step); Markdown table output (Claude Code turns it into records); the full roster build on the read path; a 7-day rollback path (user said no).

### Phase 1b — The queue view renders each row once, aligns past place 99, and sends whole spans  · status: done

#### As-built

- `head_facts.HeadFacts.moves_by_step` indexes bump records once per model by `MoveIdentity`; `queue_view.move_lines`, `move_mark` and the card reuse each row's moves. A render with 5,000 unrelated records stays within 10% of one with none.
- `queue_view.step_labels` builds its fallback name only when the label is missing.
- In the card's steps-ahead list, a moved step with no move record that joined after the selected step says `joined HH:MM, N min after it`.
- The view sizes its place column from the largest shown place (`max(2, len(str(len(waiting))))`), so names align at #99 and #100.
- `queue_lines.wrap` merges neighbouring spans of equal tone and bold (`merge_spans`) as each row closes; every span keeps its `tone` and `bold` keys.

**Binds later work:** live `build_queue.py --json` stays at most 3 times the text size (2.3–2.6 times now; one span per word was 9.4). The five golden renders did not change.

### Phase 2 — /build:queue shows a budget bar and one row per waiting step, with a size bar for each  · status: done

#### As-built

- `/build:queue` with no selector prints `Build queue · HH:MM`, a budget bar (`free <F> │ taken <T>`, one cell per GiB, `T` = `QueueMemoryTotals.charged`), then one row per waiting step: place, `▸` mark, showrunner, unit (showrunner prefix removed), step, minutes waited, an amber size bar on the budget's scale and the need in GiB. It shows no running-step, move or start-estimate lines. The drawn page `docs/plans/builds-queue-view-boards.html` is the visual spec.
- Bars use whole `█` cells and the nearest eighth block; all bars share one rounding; a need over the budget draws the budget's width. Plain form (inline, `queue.md` text block) draws free as `░` and taken as `█`; one flag picks plain or colour, and `plain()` and `json_form()` read the same lines.
- Every line fits `columns`. Above 60 columns the name cells grow to the longest name; when a line still passes the width, the showrunner and unit cells of all rows shrink together (to 3 cells minimum), then `queue_lines.clip` cuts. All bars start at one column (36 at 60 columns with the room suffix).
- Row 1 state: `↑ ≤N.N mm:ss` (strict, need below budget, N = `strict_room`), `⊘ mm:ss` (strict, room ≤ 0), `↑ mm:ss` (backfill; the strict window starts at its end), `↑` with no countdown (`WindowNotStarted`), `⊘` with no countdown (head priority, first window start not written). Red hold words follow: `low memory`, `pressure` or `no reading`; a budget wait gets none.
- `--json` keeps `QueueLines`; spans add `background` and, on the countdown, `until` (epoch seconds). The pane redraws the countdown each second from `until − now` with no script run; the 10 s refresh stays. `--you <session id>` marks that session's rows; inline falls back to `caller()`'s id.
- `BumpOff` text is `bump is off; nothing can move a waiting step`; the CLI prints it on stdout and exits 3.

**Files:**
- `scripts/lint/memory_admit.py` — `strict_room`, `HeadExplanation.order` forms, `HeadAdmissionStatus`, pressure-aware `LineWaiter.fits_now`, `BumpOff` text, `older_fit_text`
- `scripts/build_queue/queue_view.py` — `render_view`, budget bar, rows, size bars, `head_status`
- `scripts/build_queue/queue_lines.py` — `Span` (defaults, `background`, `until`), sparse `json_form`, `suggestion` tone
- `scripts/build_queue/head_facts.py` — clock, budget, head need, `window_clause`; handles `WindowNotStarted` before any deadline read
- `scripts/build_queue/queue_card.py` — `order_check` with `BehindOlderFit`
- `scripts/build_queue/build_queue.py` — CLI, `--you`, plain/colour flag, bump refusal
- `scripts/build_queue/queue_fixture.py`, `scripts/build_queue/golden/view_{60,80,100,140}.txt` — fixed queue and plain renders
- `mods/unit-board/hooks/register.tsx`, `mods/unit-board/hooks/queue.tsx`, `mods/unit-board/types/queue.d.ts` — pane paint, countdown, timer
- `mods/unit-board/tests/queue.test.ts`, `mods/unit-board/tests/kit.ts` — pane tests and the shared session fixture
- `commands/build/queue.md`, `docs/as-built/build-targets.md`, `docs/as-built/unit-board.md`, `docs/as-built/build-memory-admission.md` — docs

**Binds later work:** `memory_admit.strict_room(budget, head_need) -> int` is the one room formula: `HeadRoom.room`, `window_clause` and the view's row 1 call it. `HeadExplanation.order` is `StrictUntil | BackfillUntil | WindowNotStarted`; admission (`head_window`, `queue_decision`) does not change. `LineWaiter.fits_now` is true only when budget and free checks pass and no pressure hold applies; queue order is separate. `queue_view.head_status` reads low memory from `HeadAdmissionStatus.decision.free_passes`. `queue_card.order_check` prints `BehindOlderFit` through `older_fit_text`, the one source of that text. Sparse `json_form` omits every span field at its `queue_lines.Span` default, and the pane reads a missing field as the default. The pane timer's generation guard in `register.tsx` stops a close during `await $.ui.panes()` from starting a timer. The `kit.ts` session fixture is the one session setup the queue pane tests use. The queue snapshot keeps every row 1 state identical and carries every input these read.

**Gotchas:**
- `HeldBehindOlderFit` holds a younger step whenever an older step fits, so strict-room tests need a budget no older step fits (60 GiB in the fixture).
- Python and TypeScript each format `mm:ss`; the countdown width changes at 10:00 → 9:59.
- A countdown span that the width cuts loses `until`, so the pane shows the cut text as sent.

**Ruled out:** JSON at 9x the plain text (sparse defaults keep it within 4x); a tone on a blank mark cell (blank cells join the next span); `⊘` for every strict head (a strict head with room lets younger steps start).

### Phase 3 — /build:queue reads a lock-free snapshot and answers in under a second every time  · status: done

#### As-built

- `memory_admit.gather_queue_inputs(directory, processes, now, memory={}, *, readings=READING_NOT_TAKEN) -> QueueInputs` does every file, `/proc` and environment read; the pure `queue_read_from(inputs) -> QueueRead` reads none. `QueueInputs` holds `ordered`, `running`, `reservations`, `host`, `pressure`, raw `recovery: RecoverySpacing`, `limits: AdmissionLimits` (resolved once by `resolve_admission_limits`, the one resolver that `decide` also calls), `override` and `now`. `read_queue_pass`, the locked read and the snapshot run this one decision path.
- `exclusive_queue(directory)` yields `QueueSnapshotReadings` and, in its finally block, publishes `admission/queue.json` (`{"v": 1, "written_at", "inputs"}`) through `write_queue_snapshot` (`queue.json.tmp` + `os.replace`, no fsync). Every exclusive section, `admit_head` included, publishes this way, also on an exception path; a failed write never changes the section's result. A bump takes no lock and writes no snapshot.
- `queue_read() -> QueueRead | QueueBusy` uses the snapshot when `0 <= age <= SNAPSHOT_MAX_AGE_SECONDS` (10 s). Else it calls `locked_queue_read(timeout=QUEUE_READ_LOCK_SECONDS)` (0.5 s). On timeout, a parsed but old snapshot gives `QueueRead.freshness = StaleReading(written_at)` (the view header time prints in the warning tone); a missing or bad file gives `QueueBusy`. `build_queue.py` (view, card, `--json`) and `build_why.py` print `queue busy; try again` on `QueueBusy` and exit 0.
- `decision_checks(decision, need, available) -> tuple[BudgetCheck, FreeCheck]` is the only check constructor; `LineWaiter` carries `decision`, `budget` and `free`, so every card prints its own pressure, budget and free lines. `fits_now` stays the step's own resource fit.
- `step_matches(selector, step)` is the one identity rule (session id, recorded name, folder, folder name); `match_waiters(selector, view)` takes a place or an identity. `reason_phrase(reason, waiting)` gives `behind #<k>, an older step that fits`, or `behind an older step that fits` when no waiting step matches.
- The pane holds `QueueSelection = { kind: 'view' } | { kind: 'card'; selector: string }`; `register.tsx` converts an older string or null `selector` at load.

**Files:**
- `scripts/lint/memory_admit.py` — inputs, pure read, snapshot codec, `queue_read`, `exclusive_queue`, `resolve_admission_limits`, `decision_checks`, matchers
- `scripts/build_queue/build_queue.py`, `build_why.py` — call `queue_read()`; print the `QueueBusy` line
- `scripts/build_queue/queue_view.py`, `queue_card.py` — checks for every step, stale header time, `BehindOlderFit` place phrase, selection through the matchers
- `scripts/build_queue/queue_fixture.py`, `golden/card_4.txt` — fixed 10:46 queue as `QueueInputs`; card #4 with its checks
- `scripts/lib/terminal_cells.py` — `terminal_width`, the one cell width (also used by `queue_lines.py` and `scripts/production/dailies_render.py`)
- `mods/unit-board/types/queue.d.ts`, `hooks/register.tsx` — `QueueSelection` and the load conversion

**Binds later work:** "One decision pass…", "The queue service runs beside…" and "Every build, view and command uses the queue service…" build on: `queue.json`, its writer (published in the exclusive_queue finally block); `queue_read`'s snapshot path (snapshot used when 0 ≤ age ≤ 10 s, else a locked read with 0.5 s timeout); `StaleReading` and `QueueBusy` ("queue busy; try again", exit 0); `queue_read()` as the one entry point of the view, the card and build-why; the `QueueInputs` serial form (`snapshot_fields`, `snapshot_value`, `parse_snapshot_value`); `queue_read_from`; `gather_queue_inputs`; `decision_checks`; `resolve_admission_limits`; the card and renderer reading `QueueRead`; the pane's `QueueSelection`; `scripts/lib/terminal_cells.py` `terminal_width`.

**Gotchas:**
- The snapshot codec must encode and parse every type in `QueueInputs`. A type it cannot parse makes every read fall back to the locked read without an error. Each new input type needs a round-trip test and a held-lock test that proves the snapshot path.
- A snapshot whose non-compiler running steps do not match its reservation count reads as absent. Legacy reservation rows get `test_threads` and `call_id` set to `None`.
- Importing `roster` from `build_why.py` adds about 80 ms; import the label reader from `scripts.message.unit_labels`.

### Phase 4 — One decision pass decides for every waiting step, and the check and the queue read both call it  · status: done

#### As-built

- `admission_pass(inputs: QueueInputs, now: float) -> PassResult` in `memory_admit.py` is the one decision for every waiting row. It is pure: no file, `/proc`, environment or clock read, and no write. It calls `admission_walk` once for each input scenario and reads every row's decision from that one walk. A running step that ends is its own scenario: `inputs_after_steps_end(inputs, ended)` makes the changed copy, and one walk runs on it. `queue_decision` and `reservation_blocker` are gone.
- `PassResult` holds `rows: tuple[PassRow, ...]` in queue order, `head: HeadPriority | NoHeadPriority`, `totals: AdmissionDecision` (`decide(available, total, 0, memory)`), `recovery: RecoverySpacing`, `head_marks: tuple[HeadMark, ...]` and `override: OverrideKept | OverrideEnded`. `PassRow` holds `owner` (`<pid>-<start_time>`, from `waiter_owner`), `decision`, `reason`, `blocker: BlockingIndex | NoSingleBlocker | BlockerNotRequested` and `admission: ProposedAdmission | NotAdmitted`. `ProposedAdmission` holds `rank`, `recovery` and `override`. A `HeadMark(path, waiter)` sets `holds_younger` and `priority_since` on a head that has no `priority_since`. `OverrideEnded.action` is `"admitted"` or `"lapsed"`.
- Rules of the pass: each admitted row charges its need to the rows after it. In a recovery window (`RecoveryStarts`) at most one row admits; its proposal sets `next_start_at = now + RECOVERY_START_SECONDS`, and `PassResult.recovery` keeps the start due. `next_recovery(recovery, pressure, now)` is the one recovery change. `estimate_expired(waiter, now)` is the one expiry rule (estimate `HEAD_SECONDS` or more old): the row holds with `EstimateExpired(estimated_at)`, a `WaitReason`, and keeps its place and its room. A `ReadingUnavailable` host holds every row.
- Proposed, not committed. `check` builds `QueueInputs` with `gather_queue_inputs` from the readings it already took, adds its own waiter row, sets `blockers_for` to its own owner, calls the pass and commits only its own row. `commit_pass_effects` writes its proposal's `recovery` and `override`, or `PassResult.recovery` and `PassResult.override` when its row does not admit, before the reservation write. `check` writes the head marks and prints `memory admission: estimate expired while waiting for the lock; refresh on the next poll` when its own row expires. A launch with no ledger (no `admission/` folder) decides alone through `decide()`.
- `QueueInputs.blockers_for: frozenset[str]` names the owners that get a blocker; every other row has `BlockerNotRequested`. `blocking_index(owner, endings)` finds the one running step whose end admits the row. `blocker_identity(index, reservations, processes, proc) -> Blocker` resolves it at the process and sidecar boundary, for `check`, `blocker_snapshot` and `current_blocker`. The snapshot codec (`snapshot_value`, `parse_snapshot_value`) writes a `frozenset` as a sorted list.
- `QueueExplanation.decided_from: QueueInputs` holds the inputs of its pass. `queue_read_from` runs the pass once. `head_after_steps_end` and `smallest_head_release` run it on `inputs_after_steps_end(explanation.decided_from, ended)`. `released_resident_bytes(running, ended)` is the one calculation of freed memory (measured bytes less swap). `HeadAfterEnd` includes `EstimateExpired`.
- Output of `EstimateExpired`: `reason_phrase` gives `estimate expired`; `head_status` ends row 1 with `estimate expired` in the error tone; the card adds `✗ estimate       expired at HH:MM; refreshes on its next poll` above its resource checks; build-why's head cause is `estimate refresh: the estimate expired at HH:MM; it refreshes on the next poll`, and its release line is the same text without the `estimate refresh:` prefix (`estimate_refresh_text`). The card's `↑` move mark and `queue_view.is_moved` are gone. `build_why.py` reads labels from `scripts.message.unit_labels`, not `roster`.

**Files:**
- `scripts/lint/memory_admit.py` — the pass and its types, `next_recovery`, `estimate_expired`, `commit_pass_effects`, `blocking_index`, `blocker_identity`, `inputs_after_steps_end`, `released_resident_bytes`, `QueueExplanation.decided_from`, the `frozenset` codec
- `scripts/lint/test_memory_admit.py` — the pass tests: check and view agree, one walk per scenario, proposed against committed, codec round-trip, release helpers
- `scripts/build_queue/queue_view.py`, `queue_card.py`, `build_why.py` — the `EstimateExpired` output; no move mark; the `unit_labels` import
- `scripts/build_queue/queue_fixture.py`, `golden/card_4.txt` — the fixture carries `decided_from`; card #4 has no `↑` marks
- `scripts/build_queue/test_build_queue.py`, `test_build_why.py`, `test_queue_card.py`, `test_queue_view.py` — the new reason and its 60-column pane form
- `docs/as-built/build-memory-admission.md` — the "One decision pass" paragraph

**Binds later work:** "Every build, view and command uses the queue service…" commits a `ProposedAdmission`'s `recovery` and `override` only for an admission it commits, and else writes `PassResult.recovery` and `PassResult.override`. Its release helpers read `decided_from`. The no-ledger launch rule is held by `test_aged_launch_without_a_ledger_keeps_its_own_memory_checks` until `check` goes.

**Gotchas:**
- Resources Phase 26's queue priority record is not in the code. Waiter order and priority come from `since`, `bump_order`, `holds_younger` and `priority_since`; `queue_view.has_move_priority` reads `bump_order < 0`, and `head_facts.py` is unchanged.
- A test that moves its simulated clock `HEAD_SECONDS` past a persisted `estimated_at` tests expiry, not the window. Refresh the estimate before the clock moves.
- Blocker scenarios cost one walk for each running step. The pass makes them only when a requested row holds.

**Ruled out:** a second inputs type beside `QueueInputs`; a `queue_decision` call per row (a full walk to read one row).

### Phase 4b — The build budget is a config value, 24 GiB for a one-day trial  · status: done

As built (fe2f920a, on origin/builds cc16b3fe, 2026-10-31): `[memory] build_budget_gib=24` in `config/lint.conf`. One reader, `memory_admit.build_budget_bytes()`: `BUILDLOG_BUILD_BUDGET_GIB` wins, then the config value, then `FALLBACK_BUILD_BUDGET_BYTES` (21 GiB) for a missing or invalid value; read at each check. `head_facts`, `queue_view`, `build_why`, `AdmissionDecision` and the expected-peak cap call it. Tests pin the budget by env. Showrunner ask; the user approved 24 for a one-day trial.

### Phase 4c — The queue snapshot no longer stores the build budget; every reader resolves it live  · status: done

As built (b20effdf, on origin/builds e2773be6, 2026-10-31; builds showrunner, urgent): a `verify.sh lint` in a worktree from before Phase 4b ran its own `memory_admit.py` and wrote budget 21 into `admission/queue.json`, so `/build:queue` showed 21.0 or 24.0 at random. `QueueInputs` has no `limits`; `queue_read_from(inputs, limits)` takes them from its caller, and the private `_queue_read_live` resolves them once per reading for `queue_read` and `read_queue_pass`. The snapshot is version 2; a version-1 snapshot reads as absent and the reader takes the bounded locked read. A check from an old tree still decides its own step with its own budget until that tree merges builds.

### Phase 4d — Phase 4 carries resources Phase 26: merge of builds at e2773be6  · status: done

#### As-built

`origin/builds` at e2773be6 is merged into this branch, so Phase 4's one decision pass carries resources Phase 26, one copy of each rule. No `head_since`: waiter order is `(priority_rank, since, pid)`, and `priority_rank == 0` marks the bumped unit (it replaces `bump_order`). `/build:queue bump <unit>` runs `memory_admit.run_priority(["set", ...])` (`priority set`), which prints `queue priority set for <unit>: <result>`; `bump_waiter` is gone. During a token wait the waiter row stays; `token_wait` records `token_left_at`, and the token gap appends under the admission lock with a duplicate check, so a retry after a stop adds no second row. `head_facts.py` and `queue_view.py` read `priority_rank`; the view and card goldens follow.

**Files:**
- `scripts/lint/memory_admit.py` — `priority_rank` order, `priority set`, token-wait row and gap
- `scripts/build_queue/build_queue.py` — `bump <unit>` runs `priority set`
- `scripts/build_queue/head_facts.py`, `queue_view.py`, `golden/` — the priority read and the goldens

### Phase 4e — unit_marks.py is session_staff.py, and rca reads the one showrunner mark  · status: done

#### As-built

`scripts/production/session_staff.py` (was `unit_marks.py`, no wrapper and no old name) matches a running session to its staff row through its session labels (the unit name and staff id a session carries), and holds the director lookup too, because one caller uses both. It loads no herdr module. `scripts/message/rca.py` imports `PRODUCTION_MARK` from it and keeps no copy. Every importer (`herdr.py`, `unit_labels.py`, `unit_lookup.py`, `rename_unit.py`, `stall_watch.py`) and its tests use the new name; `docs/as-built/herdr-adoption.md` and `session-roster.md` say "Matching a running session to its staff row". `git grep unit_marks` finds only this plan.

**Files:**
- `scripts/production/session_staff.py` — the session-to-staff-row match and the director lookup
- `scripts/message/rca.py` — imports `PRODUCTION_MARK`

### Phase 4f — builds at a546fe66: Phase 4 and the rename carry the live-budget view fix  · status: done
**Depends on:** Phase 4 and Phase 4e.
The builds showrunner asked (2026-10-31): merge `origin/builds` (a546fe66, the view fix b20effdf) into this branch and send one notice for Phase 4 and Phase 4e. Two merges meet: Phase 4's `admission_pass(inputs, now)` reads `inputs.limits`, and the view fix removed `limits` from `QueueInputs` (the reader resolves them live and passes them to `queue_read_from(inputs, limits)`). The second to land (this branch) carries the view fix's state: no budget in `QueueInputs` or the snapshot, limits passed in. `unit_lookup.py`: keep this branch's name `observed_staff_marks` with the builds side's `running` parameter.

### Phase 4g — memory_admit.py's admission limits are config values in config/lint.conf, read at use  · status: done
**Depends on:** Phase 4f (the reader resolves limits live and passes them in; the snapshot holds no budget).

#### As-built

- The admission timing (ramp, head, strict, backfill, recovery spacing), the pressure limits and the peak hold are `[memory]` keys in `config/lint.conf`. `peak_hold_seconds` is the default hold, and `peak_hold_seconds.<kind>` sets one step kind's hold (`merge-gate` holds 180 s).
- One reader: `read_memory_config() -> MemoryConfig(policy, test_phase_margin, build_budget_bytes, fallbacks)` is silent (the `check` path); `memory_config()` prints each `ConfigFallback` once per process on stderr (queue views, build-why, priority, the merge gate). A missing or invalid value, an overflowing `build_budget_gib` included, uses its default.
- `AdmissionPolicy(timing, pressure, peak_hold: PeakHold)` is frozen. Every rule takes it as a required argument: `admission_pass(inputs, now, limits, policy)`, `queue_read_from(inputs, limits, policy)`, `step_promise(row, now, reading, policy)`, `estimate_expired(waiter, now, policy)`, `build_why.charge_text(basis, zone, policy, step)`. No module constant for these values exists.
- `QueueExplanation` keeps the policy it was decided by, so its counterfactuals do not change with a later edit.
- `queue.json` v4 is `{"v": 4, "written_at", "policy", "inputs"}`. `queue_read()` reads the config once, uses the snapshot only when its policy equals the live one, else takes the bounded live read (`_queue_read_live(inputs, config)`); a lock timeout answers `QueueBusy` unless the snapshot is compatible.
- The queue service passes the pass's `MemoryConfig` into test-phase estimation.
- One config read costs median 0.035 ms.

**Files:**
- `scripts/lint/memory_admit.py` — the reader, `MemoryConfig`, `ConfigFallback`, `AdmissionPolicy`, `PeakHold`, the policy-taking rules, the v4 snapshot
- `config/lint.conf` — the `[memory]` keys
- `scripts/build_queue/build_why.py` — renders the hold of the row's step kind
- `scripts/build_queue/admission_service.py` — passes the config into estimation
- `scripts/build_queue/admission_test_support.py` — `transient_scope` passes `--expand-environment=no`
- `docs/as-built/build-memory-admission.md` — the config contract

**Binds later work:** the policy is a required argument of every rule; a projection never joins old evidence with a newly read policy. The `ConfigFallback` line is owned by the client-facing commands, and protocol output carries no warning text.

**Gotchas:** systemd-run v254 and later expands `$$` in its arguments; a scope fixture without `--expand-environment=no` makes the scope test always skip.

**Ruled out:** a queue cache that serves evidence under a policy other than the one it was captured under.

### Phase 4h — A queue place helper lets a non-cargo program wait for memory in the build queue  · status: done
**Depends on:** Phase 4g (its `[memory]` reader gives each program's first peak).

#### As-built

- `scripts/build_queue/queue_place.py`: `queue_place(program, repo, *, sizing=CONFIGURED_FIRST_PEAK, before_check=no_hold_wait)` is a context manager that yields `QueuePlace`. It waits for admission through `memory_admit.check` (each turn calls `before_check()` first), prints `<program> memory wait: <GiB> needed; <reasons>` on stderr on a change or once a minute, and releases on every exit (`memory_admit.release`, `cancel_waiter`, and any reservation published with its sidecar).
- `QueuePlace.popen(argv, **popen_arguments)` starts one program per place. Scoped mode starts it in a transient `builds.slice` scope through the `--scope-start` wrapper: the wrapper publishes the scope's cgroup in the sidecar and checks the holder (pid plus start time) before and after the publish, then execs. Unscoped mode (`BUILDLOG_SCOPE=0`, holder already in `builds.slice`, failed probe) samples the family with `ProgramTree` (ppid walk from seen pids plus the process group).
- `QueuePlace.observe(peak_bytes, current_bytes)` writes `.max` and `.wave`; one sampler thread per program reads the scope's `anon`. `finalize_reservation` writes the one `anon_peaks.jsonl` row per `reservation_id`.
- Exit: `status` is the program's exit code, 1 on an exception. A program still running at exit gets SIGTERM, then SIGKILL after `program_stop_seconds`, before the release. The place installs its SIGTERM/SIGINT handler only where the caller has the default handler; a caller's own handler (the merge gate's) stays. `defer_start_signals` and `defer_cleanup_signals` guard start and cleanup.
- `memory_admit.py`: `ProgramProfile(program, repo, worktree, sizing: FixedCapSizing | HistoryPeakSizing)`; `HistoryPeakSizing(first_peak_bytes, estimate: MeasuredProgramPeak | FirstProgramPeak, estimated_at)` refreshes every `head_seconds`; `FixedCapSizing(cap_bytes)` never estimates. `program_peak_estimate(program, host, now, root, first_peak_bytes)` is the p90 of the host's rows for that program. `MemoryConfig.program_first_peaks` reads `program_first_peak_gib.<program>` (`rust-analyzer=18`). An invalid first peak raises `ProgramSizeUnavailable`; no first peak and no sizing raises `QueuePlaceRefused`.
- `merge_gate.gate_reservation` keeps its signature and calls `queue_place("merge-gate", repo, sizing=HistoryPeakSizing(cap, FirstProgramPeak(cap), 0.0), before_check=hold.wait_until_free)`. `suite_scope_prefix` and `defer_cleanup_signals` live in `queue_place.py`; `merge_gate.py` and `style_plan.py` import them from there.

**Files:**
- `scripts/build_queue/queue_place.py` — `queue_place`, `QueuePlace`, the wait loop, the scope prefix and wrapper, `ProgramTree`, the sampler, the signal guards
- `scripts/build_queue/test_queue_place.py` — scope, exit-kind, sizing, warning and fixed-cap tests
- `scripts/lint/memory_admit.py` — `ProgramProfile`, the two sizings, `program_peak_estimate`, `program_first_peaks`, `ProgramSizeUnavailable`
- `scripts/build_queue/admission_protocol.py` — the `Profile` union carries both sizings
- `scripts/production/merge_gate.py` — `gate_reservation` on `queue_place`
- `config/lint.conf` — `program_first_peak_gib.rust-analyzer=18`
- `docs/as-built/build-memory-admission.md` — the queue place helper and the two sizings

**Binds later work:** the merge gate's sizing is the one `sizing=` argument of `gate_reservation` (The merge gate holds its whole memory cap in the admission queue). `queue_place` calls `memory_admit.check` and `release` directly and moves to the service client with the other callers (Every build, view and command uses the queue service).

**Gotchas:** snapshot ancestry cannot hold a program family without a scope: a `setsid()` descendant orphaned between two samples escapes `ProgramTree`. A place that replaces the caller's signal handlers breaks the merge gate's stop (exit 143). Process-group signals in scripts and tests go through `scripts.lib.process_group.send_process_group`; `scripts/lib/test_process_group_sources.py` checks every file.

**Ruled out:** a `PR_SET_CHILD_SUBREAPER` reaper wrapper for the unscoped path (it needs an ECHILD-confirmed completion message and a new Popen contract).

### Phase 4i — A queue place with no memory scope stops a detached descendant before release  · status: done
**Depends on:** Phase 4h (`queue_place.py`)

#### As-built

- `QueuePlace.popen` always starts the program in its own transient scope under `builds.slice`, nested when the holder already runs there (builds showrunner, 2026-11-01: option B). With no scope it raises `QueuePlaceRefused("the queued program requires a memory scope; set BUILDLOG_SCOPE=0 only for tests")` before the fork. `probe_scope_prefix()` is split out; `suite_scope_prefix()` keeps its output for `merge_gate.run_suites`.
- `StartedProgram.tracking` is `ScopedProgram | ProgramTree`. `ScopedProgram` samples the scope's `cgroup.procs` (`program_members`, nested scopes included). `ProgramTree` runs only with `BUILDLOG_SCOPE=0`, for tests, and is outside the guarantee.
- `finish()` calls `stop_scope(scope, deadline, sample_seconds)`: it rereads the scope's `cgroup.procs` until it is empty, sends TERM until `program_stop_seconds`, then writes `cgroup.kill` (per-pid SIGKILL where the kernel has no `cgroup.kill`). The release runs only after the scope is empty. The leader's exit status stays the place's `status`.
- Live check on natedev: holder 55.8 MB + program 71.6 MB = charge 127.8 MB; a `setsid()` grandchild whose parent exits before the first sample is stopped before the release. No fallback.
- Callers: `merge_gate.gate_reservation` is the only `queue_place` caller; it runs on natedev, which has a systemd user manager.

**Files:**
- `scripts/build_queue/queue_place.py` — `probe_scope_prefix`, `ScopedProgram`, `scope_members`, `stop_scope`, the refusal before the fork
- `scripts/build_queue/test_queue_place.py` — refusal, nested-scope, fork-during-stop and `BUILDLOG_SCOPE=0` tests

**Binds later work:** the scope-only start, the refusal before the fork and the stop that empties the scope before release move with `queue_place` to the service (Every build, view and command uses the queue service).

**Gotchas:** one read of `cgroup.procs` is not a stop: a process can fork between the read and the signal, so the stop loops until the scope is empty.

### Phase 5 — The queue service, its socket protocol and its client exist and pass in-process tests; no build uses them yet  · status: done
**Depends on:** Phase 4

#### As-built

- `scripts/build_queue/admission_protocol.py` holds the framed socket messages, their codec and their size limits; its `Profile` union is the step and program profiles.
- `scripts/build_queue/admission_service.py` is the queue's one owner: one epoll loop holds waiters and reservations, calls `admission_pass` on each pass, promotes launches, makes one override decision per pass, and arms a reconnect grace after the listener is ready (self-bound or `LISTEN_FDS`). Reads are bounded and nonblocking; the total request size has a configured limit; a refused admission detaches its connection. `serve` requires `--state-dir`. Test-phase estimation runs on one worker thread with the pass's `MemoryConfig`.
- `scripts/build_queue/admission_client.py` sends request, wait and release, and retries inside the grace window.
- `scripts/build_queue/admission_test_support.py` holds the in-process server, the fake clock, the per-test socket under a short folder (the AF_UNIX path limit) and the fault hooks. No build uses the service.
- The service interval, grace and size values are `[memory]` keys read at use; `test_phase_need` takes a history path.

**Files:**
- `scripts/build_queue/admission_protocol.py`, `admission_service.py`, `admission_client.py`, `admission_test_support.py` — the service, its protocol, its client and its test support
- `scripts/build_queue/test_admission_protocol.py`, `test_admission_service.py`, `test_admission_client.py` — in-process tests

**Gotchas:** a long `TMPDIR` breaks AF_UNIX socket paths; tests bind under a short folder.

### Phase 8 — A nextest step's charge falls to its test-phase need when its tests start  · status: done

#### As-built

- **The mark.** `test_phase_path(row) -> Path | NoTestPhaseFile` gives `$BUILDLOG_PEAK.test-phase.json` from the reservation's `sidecar` (`.cgroup` replaced); `observed_test_phase` reads it only through `record.read_test_phase`, at run time. Admission never writes or deletes that file; `record.py` owns it. `test_phase_history(row, reading, phase)` is the one eligibility rule: an `ObservedTestPhase` window and a stored `TestPhaseNeed` give `TestPhaseStarted(started_at, peak_so_far, need)`. No file, no window, an unreadable file or no stored need keep today's `PeakSoFar` / `PeakDecayed`; an unreadable `.max` keeps `PeakFileUnreadable`. `step_promise(row, now, reading)` is split from `step_memory`.
- **N, the test-phase need.** `test_phase_need_from_rows(rows, row, host, now)` is the pure rule; `test_phase_need` feeds it from the one anonymous-history reader. Key: this host's `nextest` rows over `HISTORY_DAYS` with the same `repo_identity`, `test_scope`, `package` and sorted `filters`, no widening. Width (`test_threads`) picks the form in order `same_width`, `wider_unscaled` (nearest wider width, no scale-down), `scaled_up` (widest narrower width × width / recorded width, rounded up); N = max(p90, `FILTERED_FLOOR`). `TestPhaseNeed(bytes, runs, width, recorded_width, form, margin)` or `NoTestPhaseNeed(reason)`, reason `no_width`, `no_history` or `not_recorded`.
- **Stored outside the lock.** After `check` admits a nextest step, and in `set_width` for the new width, N is computed outside the admission lock and written by `update_reservation` (locked, atomic replace, skips a gone reservation). The field `test_phase_need` uses the one tagged codec (`_need_record` / `_read_need_record`) behind the typed entry points `need_basis_record` / `read_need_basis` and `test_phase_need_record` / `read_test_phase_need`; a missing field reads as `NoTestPhaseNeed("not_recorded")`.
- **The charge.** For `TestPhaseStarted`, `charge_basis` gives `TestPhaseCharge(bytes, peak_so_far, need, started_at)`, `bytes = ceil(max(peak_so_far, need.bytes) × need.margin)`. It does not decay; the compile `.max` and its `.wave` decay no longer count. `UsingNow`, the promise and `PeakFileUnreadable` stay; `growth_headroom` does not change. With no mark or no N the step keeps today's charge and its 1500 s decay.
- **The margin.** `test_phase_margin=1.2` in `config/lint.conf` `[memory]`, read at use by `test_phase_margin()`; an absent or bad value (not finite, below 1.0) gives `FALLBACK_TEST_PHASE_MARGIN` (1.2). The margin is frozen into the need at the need write; a need record with no margin reads with the fallback. The snapshot format is version 2 (the need carries its margin).
- **Charge-use evidence and the history row.** When a real `check` decision selects `TestPhaseCharge`, `check` writes `TestPhaseChargeUse(bytes, selected_at, reservation_id, need)` to the reservation's `.test-phase-charge.json` at its write boundary; a failed write never changes admission. `FinalizationIntent` snapshots `test_phase_peak_bytes`, `test_phase_started_at`, `test_phase_need` and the evidence before the first history append, so every retry publishes the same evidence after `record.py` has deleted the test-phase file. The finalizer copies the evidence into the `anon_peaks.jsonl` row and never infers use at cleanup.
- `build_why.charge_text` prints `tests since <HH:MM>: peak so far <GiB>, need <GiB> (<form>, p90 of <runs> runs at width <width>), × <margin>`.

**Files:**
- `scripts/lint/memory_admit.py` — mark read, need rule and codec, charge, margin reader, evidence, finalizer fields
- `scripts/lint/test_memory_admit.py` — charge, need, codec, storage, finalizer retry and evidence tests on `MemoryQueueFixture`
- `scripts/build_queue/build_why.py`, `test_build_why.py` — the `TestPhaseCharge` line
- `config/lint.conf` — `test_phase_margin`
- `docs/as-built/build-memory-admission.md` — the test-phase charge

**Binds later work:** "Every build, view and command uses the queue service…" moves the charge-use evidence write with the decision site that selects `TestPhaseCharge`, and keeps the frozen need margin. "memory_admit.py's admission limits are config values…" reads limits with the same read-at-use pattern as `test_phase_margin` and `build_budget_gib`. A reservation from "A queue place helper lets a non-cargo program wait…" or "The merge gate holds its whole memory cap…" has no test-phase need (`NoTestPhaseNeed`) and no evidence file.

**Gotchas:**
- Evidence is written only at `check`'s write boundary, outside the pure pass. Any later admission home must keep that site.
- The margin in force is the one frozen in the need, not today's config.
- Replay on 4c data: hana 2 of 22 and tool-based-ui-trunk 1 of 2 rows with N went above their charge (worst +0.397 GiB); cargo-liner 0 of 146. A 24 h check after the merge to main is owed by queue-unit; hana's margin rises by config if it is above 2.3 % on 50 or more rows with N.

**Ruled out:** `AdmissionLimits.test_phase_margin` (the applied margin could differ from the stored one); classifying charge use at cleanup time (owner-gone cleanup invented or missed evidence); widening N's rows to the package or the repository (4.3 % against 2.3 % above the charge).

### Phase 9 — The merge gate holds its whole memory cap in the admission queue  · status: done
Moved from builds-resources Phase 27 (builds showrunner, 2026-10-31): one owner for admission changes in `memory_admit.py`.
**Depends on:** devtools-unit's merge-gate checkpoint that raises the gate memory cap to 6 GiB (`a70f99e6`), which is on `main`, `origin/builds` and this branch: `merge_gate.py` :73 has `DEFAULT_MEMORY_CAP_MIB = 6144`. Phases 4g and 4h (the policy reader, and the program profiles and gate adapter this phase changes). Phase 8 (resources Phase 15; same files, `memory_admit.py` and `build_why.py`; resources 26 → Phase 8 → this phase land one after another). Merge `origin/builds` first. Global order (resources numbering): Phases 21, 16, 25, 30, 28, 4, 24 and 32 are checkpointed; serial memory chain 31 → 33 → 26 → 15 (Phase 8) → **27** (this phase) → 20 → 11 → 17 → 18 → 19 → 8; 29 runs beside the chain, checkpoints after 33 (same file, `sweep.py`) and joins before 11.

#### As-built

- `merge_gate.gate_reservation` sizes its queue place with `FixedCapSizing(cap)` from `--memory-cap-mib` (default `DEFAULT_MEMORY_CAP_MIB`, 6 GiB). The row holds the cap from the gate's start to its end, with no estimate and no refresh.
- `NeedSource` includes `"cap"`. A `FixedCapSizing` profile's check gives `ExpectedPeak(cap_bytes, "cap", 0, ProgramMemoryCap(program))`. `ProgramMemoryCap(program)` is a `NeedBasis` with the codec form `{"kind": "program_memory_cap", "program": ...}`; a `cap` source without that basis raises `ValueError`.
- `step_promise(row, now, reading, policy)` gives `LifetimePromise(row["need"])` to a row whose waiter records `need_source == "cap"`, beside a release build. The charge never decays for that row, whatever the peak hold.
- `estimate_expired` is never true for a `cap` waiter: the cap never asks for a new estimate after `head_seconds`.
- build-why: `need_text` prints "the merge gate's memory cap" for the gate and "<program>'s memory cap" for another program; `charge_text` names the step kind ("merge gate promise: holds until the step ends", "release build promise: …"). `memory_gate.sh` `buildlog_memory_line_inner` has a `cap` arm with the same text.
- `HistoryPeakSizing`, `program_peak_estimate`, `MeasuredProgramPeak` and `FirstProgramPeak` stay for the other programs.

**Files:**
- `scripts/lint/memory_admit.py` — the `cap` source and `ProgramMemoryCap` basis, the `step_promise` case, the `estimate_expired` exception
- `scripts/production/merge_gate.py` — `FixedCapSizing(cap)`
- `scripts/build_queue/build_why.py` — the cap need text, the step-kind promise text
- `scripts/lint/memory_gate.sh` — the `cap` wait-line arm
- `docs/as-built/build-memory-admission.md`, `docs/as-built/merge-gate-and-test-speed.md` — the gate row holds its cap
- Tests: `test_memory_admit.py`, `test_merge_gate_suites_admission.py`, `test_build_why.py`, `test_queue_place.py`, `test_memory_gate_wait.py`

**Binds later work:** the queue service keeps the `cap` source, the `ProgramMemoryCap` basis, the lifetime cap charge and the no-expiry rule for a `cap` waiter (Every build, view and command uses the queue service).

**Gotchas:** `step_promise` reads the cap case from the waiter's recorded `need_source`, not from the step name.

### Phase 10 — Every build, view and command uses the queue service, and the polling admission goes  · status: done
#### As-built

- The queue service (`admission_service.py`) is the one admitter. `memory_gate.sh`, `verify.sh`, `invoke.sh`, the BRP launch gate, `build_hold.py`, `merge_gate.py` and `queue_place.py` call `admission_client.py` (`admit`, `launch`, `release`, `width`, `token_wait`, `view`, `explain`, `mine`). The polling `check`, the admission lock, the file readers, `.waiting`, `.reservation` and the old `queue.json` ledger do not exist.
- The shared rules stay in `memory_admit.py` (`step_estimate` with `StepEstimate`, the pure `QueueInputs` builder, `queue_read_from_pass`); the service calls them and holds no copy. `view`, `explain` and `mine` read the stored pass.
- `admission/state.json` holds reservations, waiter order and episodes, the `QueuePriority`, the admit-head record, the recovery spacing, the replay ledger and a durable build-history generation, written through `os.replace`. A first start imports the old files once and deletes them.
- A finished build advances the build-history generation. An estimate carries the generation it read; a grant needs an equal generation, otherwise the reply is `EstimateExpired` and the client refreshes the estimate.
- A durable grant replays on reconnect with the same reply and reservation, charged once. The client keeps one receive buffer per socket across timeouts.
- Each pass writes the snapshot `admission/queue.json` through `scripts/build_queue/queue_snapshot.py` (atomic replace; a write error is ignored). AWS control reads it and refuses a start on a missing, stale or partial snapshot.
- `admission_client.py --wait-events` prints `first` and `done`. `memory_gate.sh` pipes them to `buildlog_memory_event`, and `verify.sh` records the memory phase waits in phase history.
- Each pass counts `builds.slice` memory that no reservation owns (`UnownedSliceMemory`), and a held head shows real free, reserve and need in the view and the card.
- `admission_service.py drain-check [--service]` is the drain barrier; `export-priority` exports the active priority for a rollback.

**Files:**
- `scripts/build_queue/admission_service.py` — the service, `StateIntents`, the import, the snapshot writer, the generation rule, `drain-check`, `export-priority`
- `scripts/build_queue/admission_client.py` — the client for every caller, the wait line, the wait events
- `scripts/build_queue/admission_protocol.py` — the messages; `admission_socket_path` calls the override guard
- `scripts/build_queue/queue_snapshot.py` — the one queue snapshot form, its reader and writer
- `scripts/build_queue/admission_test_support.py` — the in-process service on a per-test socket, the fake clock, the fault hooks
- `scripts/lint/memory_admit.py` — the shared rules
- `scripts/lint/memory_gate.sh`, `scripts/direct/verify.sh`, `scripts/lint/invoke.sh`, `scripts/hooks/pre-tool-use-brp-launch-gate.sh`, `scripts/build_hold/build_hold.py`, `scripts/build_hold/override_guard.py`, `scripts/build_hold/override_guard.sh`, `scripts/build_queue/queue_place.py`, `scripts/buildlog/mac_idle.py`, `scripts/buildlog/record.py` — client callers
- `scripts/build_queue/build_queue.py`, `build_why.py`, `queue_view.py`, `queue_card.py`, `commands/build/queue.md` — the view and the card on `admission_client.view` and `explain`
- `scripts/build_queue/test_admission_client_wait.py` — the wait-line regressions against the in-process service
- `docs/as-built/build-memory-admission.md` — the service, the protocol, the state file, the cut-over and rollback

**Binds later work:**
- The cut-over and rollback steps are in `docs/as-built/build-memory-admission.md` → "Startup, cut-over, and rollback". The builds showrunner runs them at the merge to main, after the user's go.
- The service owns `admission/` (`state.json`, the `queue.json` snapshot) under its state folder; `queue_snapshot.py` names `admission/queue.json`.
- The owner key is `Owner` in `admission_protocol.py` and the service. A run place keeps the `--wait-events` client path.
- The card and `build_why.py` read the service only through `admission_client.view` and `explain`.
- `[memory]` keys are read at each service pass; the snapshot writer and AWS control do not read them.

**Gotchas:**
- A test that counts grants counts distinct reservations, because a replay resends the grant.
- Tests and `verify.sh` fixtures use the private service socket, never the live one. A direct `handle_request` call in a test goes inside `fixture.loop_turn`.
- Timing tests (`test_queue_view` render time, `review_links` `test_change_page`, `codex_mesh`) can fail once at load 60-100 and pass alone.

**Ruled out:**
- A shadow run beside the old checker (user, 2026-10-31): one cut-over, no second admitter.

### Phase 10a — One function names every /tmp and /dev/shm build folder  · status: done

#### As-built

- `scripts.lib.build_folders.create_build_folder(request: BuildFolderRequest) -> Path` accepts `UniqueBuildFolder(prefix, parent)` or `StableOwnerFolder(name, parent, mode)`. Unique allocation is atomic with mode `0700`. Stable allocation preserves existing contents and permissions.
- `InheritedTemporaryParent` reads `TMPDIR` at allocation. `ExplicitBuildParent` preserves the supplied parent. `SystemTemporaryParent` selects `/tmp`. `UnitRunParent` uses `unit_runs.run_roots()`. The merge gate requires `/dev/shm` without a disk fallback.
- `FOLDER_PREFIXES` defines build names. `MKDTEMP_SUFFIX_PATTERN` defines eight-character allocator suffixes. `temporary_build_folder(request: UniqueBuildFolder) -> Generator[Path]` uses standard-library cleanup and permission repair. Callers retain ownership records and cleanup order.
- The CLI prints one path. Forms are `unique --kind KIND [--owner TEXT] [--parent PATH | --system-temp]` and `unique --prefix TEXT [--parent PATH | --system-temp]`. Stable forms are `stable --name NAME (--parent PATH | --unit-root)`, `stable --path PATH`, and `stable --run-temp RUN`. `run-temp-path RUN` only names the path.
- `scratch.new()` and `scratch.target()` retain registration and task naming. `copy_record.root()` and `copy_record.copies_root()` retain root authority. `unit_runs.RUN_TEMP_NAME` names run temporary folders. `unit_runs.activate()` maintains current and former markers.
- `memory_admit.is_fresh_test()` reads `tmp_sweep.state_dir()` at use. `tmp_sweep.TARGET_DELETES_NAME` names the shared deletion record. Private sweep roots select private copies folders. Admission state and snapshots remain outside temporary build folders.

**Files:**

- `scripts/lib/build_folders.py`, `scripts/lib/unit_runs.py`, `scripts/lib/socket_test_support.py` — naming, run paths, markers, and short socket parents.
- `scripts/production/{merge_gate,fast_track,waiting,bump_gate,ci_points}.py`, `scripts/style/{bulk_run,style_sync}.py` — temporary checkout and index callers.
- `scripts/buildlog/rust_release.py` — shared trial prefix and inherited parent; Git creates the clone.
- `scripts/direct/{verify,prepare_session,implement}.sh`, `scripts/lint/invoke.sh`, `scripts/agents/codex_mesh.py` — CLI allocations and run temporary folders.
- `scripts/hooks/{session-start-temp-root,pre-tool-use-checkpoint-doc-gate}.py` — stable session roots and temporary checkpoint folders.
- `scripts/build_queue/{queue_place,admission_test_support}.py`, `scripts/mac_test/{copy_watch,linux_runner}.py`, `scripts/lint/style_review.py` — sidecar, admission, staging, and review folders.
- `scripts/lint/{tmp_sweep,sweep,folder_audit,memory_admit}.py` — shared names, ownership classification, and sweep-state reads.
- `docs/as-built/build-memory-admission.md`, `scripts/lib/test_build_folders.py`, and adjacent tests — naming reference and cleanup coverage.

**Gotchas:** `verify.sh` prepares a missing `TMPDIR` through `invoke.sh --prepare-temp` before environment loading. Script-copy fixtures require adjacent dependencies. `test_verify_nix_shell`, `test_verify_ssh_targets`, and `test_verify_step_owner` cover these dependencies. Sweeps also accept six-character shell suffixes until 2026-12-03. The SSH promotion check uses self-contained `tempfile.TemporaryDirectory(prefix="promotion-index-")` before the Mac fetch. The assertion guard can associate a decorator with the preceding test.

**Ruled out:** Unused `RegisteredTaskFolder`, `ScratchParent`, `CopiesParent`, `RequiredMemoryParent`, `task`, and `--mode` wrappers; their existing authorities remain separate.

### Phase 10b — The /build:queue card carries the build-why facts, and build-why goes  · status: done

#### As-built

- `/build:queue <position|session|folder>` shows the full queue, one blank line, and selected cards; `mine` uses the same layout.
  One `QueueRead`, one optional `head_facts.read_head_facts(reading)`, and one label resolution supply the complete page.
- Running and Queued sections share aligned memory bars with the budget summary.
  Live runs show use against grants, including merge gates; runs without reservations show `between steps`.
  Ended runs with processes have a separate section; empty scopes remain charged but have no row.
  Staff records supply unit names; missing units use session names, and unit columns remove the showrunner prefix.
- `queue_evidence.py` formats recorded need, package, filters, width, charge basis, pressure, arithmetic, token waits, follower fit, and release advice.
  `need_text(source, *, budget: int, zone: ZoneInfo)` requires the recorded budget and an explicit zone; formatters never reinterpret replies through live configuration.
- `build_queue.main` reads optional host load and computes `smallest_head_release` once for selected pages; `release_text` formats that result.
  Admission rules remain in `memory_admit.py`; `MemoryPressure.condition` records `full` or `swap`, and both pressure formatters use that cause.
- Cards show queue arrival, time waiting, and the separate order-window deadline; unavailable optional evidence remains unknown.
  The pane retains its selection and last complete page after a failed refresh.
- `build_why.py` and its command are absent; `queue_evidence.py` owns the retained formatters, including `release_text`.

**Files:**

- `scripts/build_queue/queue_evidence.py`, `queue_card.py`, `queue_view.py`, `build_queue.py` — evidence formatters, selected cards, section layout, and page inputs.
- `scripts/build_queue/head_facts.py`, `queue_lines.py` — bounded history, shared clocks, and terminal alignment.
- `scripts/build_queue/scope_activity.py`, `admission_service.py` — process ownership, live runs, and scope evidence.
- `scripts/lint/memory_admit.py` — admission rules, recorded pressure causes, reservation matching, and release calculations.
- `scripts/build_queue/test_queue_evidence.py`, `test_queue_card.py`, `test_queue_view.py`, `test_build_queue.py`, `golden/` — formatter, selection, section, and complete-page regressions.
- `mods/unit-board/tests/queue.test.ts`, `selected_page_fixture.ts` — shared terminal/pane output and failed-refresh coverage.
- `commands/build/queue.md`, `docs/as-built/build-memory-admission.md` — command instructions and evidence ownership.

**Binds later work:** `read_pressure(host, *, policy)` uses supplied thresholds; evidence formatters use reply policy and limits, with no new admission state or history format.

**Gotchas:** Unselected pages perform no release search. History scans stop at 32 MiB, and bump details show at most eight groups. Missing step checks return `ReadingUnavailable`.

**Ruled out:** A `build-why` alias; separate `head_since` state; live configuration reads for recorded evidence.

### Phase 10c — The admission settings live in config/build_admission.conf, and the code keeps no copy of them  · status: done

#### As-built

- `config/build_admission.conf` holds every admission key (the old `config/lint.conf` `[memory]` section, with `program_first_peak_gib.cargo=12`). `memory_admit.py` keeps no default copy of a value. Measured constants (`RELEASE_HEADROOM`, `ANON_SHARE`, `FILTERED_FLOOR`, `HISTORY_DAYS`) stay in code.
- Each pass makes one `conf.read` and gets a usable or an unusable `MemoryConfig`. A recovered value is usable and warns once per process at the client boundary (`ConfigurationDiagnostic`). An unusable key stops grants with one refusal line that names the file, the key and the value.
- Release and cleanup need no config: each grant saves its cleanup timing, and the service serves releases and cleanup while the config is unusable. Old ledger grants without cleanup timing restore after an upgrade.
- The service listens with unusable config, answers views with the refusal, and watches the nearest existing config folder through `scripts/lib/inotify.py`; it re-arms the watch on each event and retries a failed watch.
- Each request carries one hash of the loaded admission code (`scripts/build_queue/admission_code.py`), so `drain_check` counts only callers that run older code. Dead and reused caller records are pruned.
- `ExplainReply` carries `ConfigurationDiagnostic` diagnostics; `ConfigFallback` is gone, with no alias.
- Tests read fixture settings through `CONF_DIR`; no test depends on the live file.

**Files:**

- `config/build_admission.conf`, `config/lint.conf` — the admission keys and their move.
- `scripts/lint/memory_admit.py` — `MemoryConfig`, one read per pass, the refusal line.
- `scripts/build_queue/admission_service.py`, `admission_client.py`, `admission_protocol.py`, `admission_code.py`, `queue_place.py`, `queue_view.py` — consumers, the config watch, the code hash, diagnostics and the refusal in the view.
- `scripts/lib/inotify.py` — the shared folder watch.
- `scripts/mac_test/loopback_proof.py` — reads `cargo_first_peak_bytes()`.
- `scripts/lint/fixtures/build_admission.conf` and the build_queue, lint, hooks and production admission tests — fixture settings through `CONF_DIR`.
- `docs/as-built/build-memory-admission.md` — the settings file and the refusal.

**Gotchas:** A step loads the admission code without an extra Python start (gate fix). `admission_service.py` `main()` leaves argparse defaults to argparse; a namespace class attribute hides the default.

**Ruled out:** Optional profile fields for a missing key; an alias for `ConfigFallback`; config reads in release or cleanup.

### Phase 10d — hana CI builds wait in the build admission queue  · status: done

#### As-built

- `scripts/build_queue/ci_cargo_front.py` is the cargo front of a hana CI runner, packaged from the store so a runner account reads no `~/.claude`. Each building cargo call holds one reservation for its lifetime. Other commands pass through.
- `--front <path>` names the front's own path. `cargo_path(explicit, front)` skips it, so the packaged wrapper does not select itself.
- `end_with_front(front_pid)` sets `PR_SET_PDEATHSIG` with a `getppid` check, so cargo ends when the front is killed. Existing cargo descendants do not inherit the death signal.
- `ServiceEstimateRequest` lets a client send no estimate: the service reads config and history and replies with the need it charged. `AcquisitionTiming` is separate from `CleanupTiming`, and `RunnerExecutionTiming` and `RunnerAdmitted` carry what a runner needs. A runner client reads no config and no history.
- The service reads the peer account. A runner account gets a refusal for priority set and clear, `admit_head`, and any release, width, token, run place or replay request for an owner it did not register. Authorization comes before any saved state change.
- `admission_client.width` uses `request_id()`, so a runner changes its own width through the client. `memory_admit.py:2447` removes a run's progress json, log and sidecar in a loop that catches only `PermissionError`, so runner files do not stop a release.
- `config/build_admission.conf` `ci_label=hana CI · {runner} · {job}` is the queue label. `admission_records._ci_label(value)` validates it, so an invalid label cannot stop the service.
- `memory_admit.unowned_slice_memory` subtracts registered CI use, and only an unregistered worker starts a `RunningServiceJob`, so a registered job counts once in the cap. `ci_keep_aside` applies only while a running CI job has no reservation. `ProgramTree.adopter` tracks a program the CI front adopts.
- A CI call sets the progress settings on the child only and uses the look-ahead keep-aside.

**Files:**
- `scripts/build_queue/ci_cargo_front.py`, `scripts/build_queue/test_ci_cargo_front.py` — the front
- `scripts/build_queue/admission_service.py`, `admission_protocol.py`, `admission_client.py`, `admission_records.py` — peer authorization, service estimates, runner timing
- `scripts/build_queue/queue_place.py`, `queue_view.py` — `ProgramTree.adopter`, the CI row
- `scripts/lint/memory_admit.py` — one count of CI use, the release cleanup
- `config/build_admission.conf` — `ci_label`
- `docs/as-built/build-memory-admission.md` — the front, the socket rule, the limits

**Binds later work:** natedev owns the NixOS activation: the socket group, the runner PATH, `ADMISSION_SOCKET` and `BindPaths` where needed. `hana-ci.slice` stays at `MemoryMax=34G`. Until that activation, a CI job has no reservation, so `ci_memory_margin_gib` and the mixed keep-aside rule stay. The deletion is its own todo phase, "The hana CI margin key goes after the runner activation". The runner client takes no local config read.

**Gotchas:** an existing cargo descendant can survive a SIGKILL of the front. An absolute call to another cargo goes past the front. A service that has not restarted has no runner authorization, so the socket opens to the group only after the service is on main and live.

**Ruled out:** a fixed 57G limit (user, 2026-11-05: "57GiB is impossible if we're running other programs"); a runner client that reads config or history; a copy of client code in `/etc/nixos`; an optional profile on `AdmitRequest` in place of a named request.

### Phase 22 — The hana CI margin key goes after the runner activation  · status: moved to resources (11-08)

**Depends on:** 10d, natedev's confirmation that the runners are active behind the cargo front, and the user's approval of a config key deletion. Source: the accounting transition of the CI queue phase, moved here at its shrink (director call, packaging).

#### Work Order

**Goal:** A registered CI job is charged only through its reservation. The fixed CI margin is gone.

**Spec:**

1. **The wait.** natedev confirms the runner activation. builds asks the user, because a config key deletion needs the user's approval. Do not start before both answers are on the plan.
2. **The deletion.** `ci_memory_margin_gib` leaves `config/build_admission.conf` and `AdmissionPolicy` (`ci_memory_margin_bytes`, `admission_records.py:78`). `ci_keep_aside` (`scripts/lint/memory_admit.py`) and the mixed rule go with it.
3. **The limit.** A CI job that the front cannot cover has no keep-aside. `MemoryMax=34G` is its limit. The notice says so.
4. **Proof.** Tests: registered jobs still have no keep-aside; an unregistered job counts once in the cap and now takes no margin, so the room rises only in that case. Remove the tests of the mixed rule only with an approved assertion change.

**Files:**
- `config/build_admission.conf`
- `scripts/build_queue/admission_records.py`
- `scripts/lint/memory_admit.py`
- `scripts/lint/test_memory_admit.py`
- `docs/as-built/build-memory-admission.md`

**Workers:** `1 writer`
- `impl2` — the deletion and its tests.

**Gate:** the merge gate, and `basedpyright` 0/0/0 on each changed file and each caller module.

**Change-Review:** none — the queue view shows the same rows; the room rises only when an unregistered job caused the margin. Registered jobs take no margin today (`test_memory_admit.py:5809`).

### Phase 23 — Detached Python runs have their own group and count in Other  · status: done

#### As-built

- `scripts/agents/detached_runs.py` starts each detached run with `--slice=builds-detached.slice`. The dash nests the group under `builds.slice`, so the memory limit and CPU weight of the parent hold it.
- `unowned_slice_memory` in `memory_admit.py` skips the child group. The memory feed takes build memory from reservation rows, so a detached Python run has no row and counts in Other. A reserved cargo step in the group counts once, as a build.
- The memory page labels a stray process in `builds.slice` "unowned processes".
- `scope_activity.py` does not treat a detached run supervisor as a build. The registered run identity, the held-run rows and the card selected by owner stay.
- `scripts/build_hold/cargo` and `build_hold.py` exclude the nested path. A cargo call in the group takes the hold check and starts a new build scope.

**Files:**
- `scripts/agents/detached_runs.py` — starts detached runs in their group
- `scripts/lint/memory_admit.py` — `unowned_slice_memory` skips the group
- `scripts/build_queue/memory_feed.py` — stray-process row label
- `scripts/build_queue/scope_activity.py` — row detection
- `scripts/build_hold/cargo`, `scripts/build_hold/build_hold.py` — exclude the nested path
- `docs/as-built/build-memory-admission.md` — slice table and wrapper paragraph

**Binds later work:** a check that means "this process is an admitted build" excludes `/builds.slice/builds-detached.slice/`.

**Gotchas:** a dash in a slice name nests it. A substring match on `/builds.slice/` also matches the nested path. `scripts/lint/invoke.sh` always starts its own scope and reads no cgroup path.

**Ruled out:** a row for detached runs in the queue view.

### Phase 20d — A queue row names the project of its step  · status: done

#### As-built

- `queue_lines.step_text(step)` prints `<project> <step>`, or the step alone when no project is known. The Step column in `/build:queue`, its card and every other person-facing line use it. Matching and history keep `step.step`.
- `RunningStep` and `WaitingStep` have `project=""`. `memory_admit.step_project(repository_name, repo)` fills it and returns empty for an absolute path, so a gate stays `merge-gate`.
- `/build:queue` hides every `UnownedSliceMemory` row. `queue_card.py` still prints that row for a person who selects it, and the memory page shows the memory as Other.
- `test_admission_client.py::test_import_does_not_load_memory_admission` holds the client import boundary.

**Files:** `scripts/build_queue/queue_lines.py`, `scripts/build_queue/admission_records.py`, `scripts/build_queue/admission_service.py`, `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/queue_view.py`, `scripts/build_queue/queue_card.py`, `scripts/lint/memory_admit.py`, `scripts/build_queue/golden/`, `docs/as-built/build-memory-admission.md`.

**Binds later work:** the build page feed prints `queue_lines.step_text`. The service restart after landing puts the new text in the snapshot.

**Gotchas:** `queue_lines.py` holds the function because `queue_evidence.py` loads `memory_admit`, which the client must not load.

**Ruled out:** a row for runs outside the queue in `/build:queue`.

### Phase 42 — A file copied to a remote tree always counts as changed for Cargo  · status: done

#### As-built

- Both remote copy paths use `rsync -rlpc`, defined once as `RSYNC_RECEIVER_WRITE_TIME_FLAGS` in `scripts/copies/copies.py`. `remote_rsync` and `copy_tracked_scripts` in `scripts/mac_test/offload.py` read that definition.
- A changed file takes the write time on the receiver, so Cargo rebuilds it. Checksums skip equal content, and an equal file keeps its destination mtime.
- Proof: with `-rlptc`, a changed file with an old source mtime left Cargo Fresh with the old binary. With `-rlpc`, Cargo reports Dirty and rebuilds.

**Files:**
- `scripts/copies/copies.py` — the flag definition.
- `scripts/mac_test/offload.py` — uses the definition.
- `scripts/copies/test_remote_copies.py` — real `rsync` rows for a changed file and an equal file.
- `scripts/mac_test/test_offload.py` — the expected flags.
- `docs/as-built/mac-test-offload.md`, `docs/as-built/builds-aws-target.md` — the rule.

**Binds later work:** A copy into a tree where Cargo builds does not use `-t`.

**Gotchas:**
- A mirror that is already stale stays stale after the flag change, because equal content is skipped and `target/` stays. The deploy step touches the sources of each remote mirror once, after the flags land. A touch before landing is undone by the next copy with the old flags.
- A Mac ssh exit is always 0 under Tailscale SSH, so a remote step reports its result with a sentinel.

**Ruled out:** A touch step or a guard at each caller.

### Phase 43 — The copies configuration is parsed only when its file changes  · status: done

#### As-built

- No code changed. A prototype added one cache of the parsed copies configuration and a public `ConfigRead` method in `scripts/lib/conf.py`. The gain did not pay for them, so the prototype did not land.
- Same load, 14,425 `copies.load_config` calls on the real config, old and new code one after the other, five runs each: 0.71 to 0.75 CPU seconds before and 0.24 to 0.25 after. The saving is about 0.5 CPU seconds per `test_crate_run` run.
- `cProfile` reports 5.9 CPU seconds for the same calls. Most of that cost is profiler overhead.
- `module_cost.py` on `style/test_crate_run.py`, three runs on each tree: the before mean is 59.974 CPU seconds (spread 1.657) and the after mean is 32.222 (spread 12.277). A machine load change caused most of that difference.

**Gotchas:** `module_cost.py` runs made at different machine loads cannot show a gain of this size. Run the old and new code one after the other. A cache of these settings must ask `ConfigRead` about recovery, because a text comparison of `values()` with the stored settings treats a host override as a recovered value and a recovered value with equal text as a live value. Its key must also hold the host name, and validation must run at each use.

**Ruled out:** a cache of the parsed copies configuration, until a measurement shows a gain that pays for the added code.

### Phase 23a — Queue boundary imports stay small, if a prototype shows a real gain  · status: done

#### As-built

- No code changed. The prototype moved the five admission imports of `queue_place.py` into the functions that use them. The gain was not real under the phase rule, so the prototype was discarded.
- Five style test modules, three repeats on each tree, 49,651 process starts: the baseline mean is 124.521 CPU seconds and the prototype mean is 107.340. The gain is 17.182 CPU seconds. The baseline spread is 36.988 and the prototype spread is 26.310.
- `test_standing_checkpoint`, the module with natest's 46 starts, shows a gain of −0.434 CPU seconds.
- The import of `queue_place` costs 188,760 µs at baseline and 17,392 µs with the prototype.

**Gotchas:** the repeats of these modules differ by up to 37 CPU seconds on one tree, so three repeats cannot show a gain of this size.

**Ruled out:** lazy admission imports in `queue_place.py`, and a separate boundary module, until a measurement shows a gain larger than the spread.

### Phase 10f — The queue names every build folder it makes and deletes it when its work ends  · status: todo
**Depends on:** none (checkpoint A is on main; checkpoint B changes `scripts/lib/build_folders.py`, the `tmp_sweep` call and `scripts/mac_test/linux_runner.py`). 10g changes the offload path. The second to land resolves; the director names the merge owner before the work runs in parallel (architect review of 18, 2026-11-06). 18c is complete.
**Why.** The user, 2026-11-03, via the builds showrunner: "our build-queue is supposed to handle making sure we name things and delete them when they are completed". The naming map is a design call that the builds showrunner settles. The showrunner approved it on 2026-11-03 (map and rulings: the 10f design in the unit's phase folder), with one change: a remote mirror ends when its copies.py remote lease from verify P35 ends.

**Scope (showrunner limits, 2026-11-03).** 10f does not change the callers that verify P18 moves to copies.py (merge_gate, bump_gate, fast_track, waiting, the style runs, rust_release). Its sweep never touches copies.py standing checkouts. Two checkpoints:
1. A: `build_folder_path` is the one name rule; `build_folders.py run --owner O --name N -- CMD` makes a folder, runs CMD in it and deletes it when CMD and its descendants end; `scratch.target` uses the name rule; the scratch guard refuses a clone under a unit or session tmp root and names the command.
2. B: `build_folders.py sweep` removes a marked folder only when its owner is gone (pid, start time, boot id; a read error keeps it); `tmp_sweep` calls it; one writer makes each removal record below, also for `linux_runner` floor and trim.

**Markers and descendants (architect review of 23, 2026-11-09).** Only `run_build()` writes `OWNER_MARKER` today. The context-managed folders from Phase 37 write none, so the Work Order names each marker producer. A folder stays while a surviving descendant of the dead owner still uses it. Tests: owner death with a live child keeps the folder; unreadable ownership keeps the folder; a standing copy and a folder under an active remote lease are never removed.

**Progress (2026-11-04):** checkpoint A is on main, with the test fixes for a `CONF_DIR` that lacks `build_folders.conf` (`build_folders._settings` then reads the repository file) and config read at use in `tmp_sweep`, `style_sync` and `folder_audit`. Checkpoint B is open.

**Removal record (aws-unit ask via the builds showrunner, 2026-11-03):** each folder removal writes one record with: time, target, host, repository key, slot, folder path, bytes freed, free GiB before and after, and the reason: `floor`, `trim`, or `owner-ended` for a folder that the sweep removes after its owner is gone (architect review of 10q, 2026-11-05). Source today: `scripts/mac_test/linux_runner.py` removes or trims a slot build folder after each step and prints only `mac_cleanup=<result>` (line 839; `offload.py:934` parses it); no record names the folder or the bytes freed. aws-unit's weekly report (cold steps after a floor removal, time lost) reads this record. Test: an owner-ended removal writes one record with reason `owner-ended`, and a standing copy is never removed. One record contract: the floor sweep already records removals through `record_sweep_removal()` (`scripts/lint/sweep.py`) and `record.sweep_trim()` (`scripts/buildlog/record.py`). Extend or reuse that path, including an interrupted write; add no separate journal (architect review of 23, 2026-11-09). The readers are part of the contract (architect review of 43, 2026-11-09): `record.sweep_trim()` accepts the causes `budget take` and `floor take`, and `read_rebuild_evidence()` (`scripts/lint/sweep.py`) reads only natedev records with those causes. The Work Order names the schema writer, compatibility tests for the new causes, fields and remote records, and aws-unit's report consumer.

**Workers (checkpoint B):** `1 writer + 1 tester` — `impl` owns `scripts/lib/build_folders.py` (sweep and the record writer), the record contract in `scripts/buildlog/record.py` and its reader in `scripts/lint/sweep.py`, the `tmp_sweep` call and the `linux_runner.py` record call (aws-unit reads the record: name it in the notice); `test` owns `scripts/lib/test_build_folders.py`, the `tmp_sweep` tests, the `linux_runner` record tests and the record reader compatibility tests. B keeps the standing-copy exclusion and the remote-lease ownership.

### Phase 10g — The queue records each placement and gives one launch permission  · status: done

#### As-built

- `scripts/build_queue/placement.py` holds the placement types: `SubmittingOwner`, `SubmittingSession`, `PlacementConstraints`, `HostSelection`, `PlacementAssignment`, `AssignmentState`, `LaunchPermission`, `ProviderClaimReference`, `ExecutionIdentity`, `ExecutionEvidence`, `HostEvidence`, `HostEligibility`.
- The admission service saves one `PlacementAssignment` per episode and attempt before it replies. The first valid consumption moves `PermissionAvailable` to `PermissionConsumed` durably, and only its first response authorizes execution. A repeated, unknown or closed consumption returns `DispatchStatus` only; ledger pruning never makes a new permission.
- Version 3 wire messages carry placement. Version 1 and 2 requests decode as before and keep their reply shapes. No caller changes its route: `route_test`, `_cloud_pass` and every existing client keep their behavior.
- `HostEvidenceReader` reads host evidence in the background, in the pattern of `OperatingSystemReader`. Evidence goes stale on the existing poll interval; no network call or waiting lock runs in the service loop.
- Each Mac and SSH claim record carries `claim_id`, `episode`, `attempt` and `submitting_session`. Release, release-wait and adoption match that exact identity under the target lock, never the pid alone. `submitting_session` names the current owner: `take_over_kept_claim(..., session=)` sets it with `pid` and `proc_start`, and `holder_session` keeps the original job evidence. `adopt_wait_claim` requires strict session equality, exact identity and a live process start.
- Hold and claim policy take `SubmittingSession` explicitly; cached eligibility keys include it. `wait_claim_identity` recovers the identity for the session that owns the claim now. `verify.sh` and `offload.py` carry that identity to release and adoption; `mac_run.sh` releases by the exact claim; `/build:mac` prints a manual claim's identity and its release takes `--claim-id`, `--episode` and `--attempt`.
- Claim records, run.json and admission state are decoded strictly.

**Files:**
- `scripts/build_queue/placement.py` — placement types and decision records.
- `scripts/build_queue/admission_wire.py`, `admission_protocol.py`, `admission_client.py` — version 3 messages, decoding and client calls.
- `scripts/build_queue/admission_service.py`, `admission_records.py`, `admission_code.py` — assignments, consumption, background evidence, restart; `placement.py` in the service code paths.
- `scripts/mac_test/claim_state.py`, `scripts/mac_test/mac_test.py` — exact claim identity, takeover session, strict adoption and release.
- `scripts/build_targets/build_targets.py` — explicit session, `wait_claim_identity`, `cleanup_refused_claims(..., session=)`.
- `scripts/mac_test/offload.py`, `scripts/direct/verify.sh` — identity hand-off (verify-unit owns both).
- `scripts/production/mac_run.sh`, `commands/build/mac.md` — exact manual claim and release.
- `scripts/build_queue/test_placement.py` and rows in the admission, claim, target and verify tests; `docs/as-built/build-memory-admission.md` — messages and records.

**Binds later work:** the verify, merge-gate and view cutovers build on `PlacementAssignment`, the single launch permission, `HostEvidenceReader` and the exact claim identity. `submitting_session` is the current claim owner; `wait_claim_identity` replaces `owns_wait_claim`.

**Gotchas:** a merge that changes a strictly decoded record is a deploy. Stop `build-admission.service` and `.socket`, `build-memory-feed.service` and the claim writers; run the conversion program (outside the repository) on admission state, `~/.local/state/mac-test` and `~/.local/state/build-targets/aws`; merge; then start. Live services restart on main at once.

**Ruled out:** a cleanup-pending bypass of the adoption session check, because it let an unrelated session adopt a live claim.

### Phase 43a — A gate copy at a new path rebuilds what holds its manifest path  · status: done

#### As-built

`copies.standing` restores the saved file time of each tracked file with an unchanged blob, except a file whose content names `CARGO_MANIFEST_DIR`. `copies.manifest_path_files(tree)` finds those files with one `git grep --no-color -l -z -F` in the checkout. Those files keep their write time from the new checkout, so Cargo rebuilds their crates and the dependents. Every other unchanged file keeps its saved time, so a same-commit gate stays a cache hit for the other crates. `standing` is the only path in `copies.py` that restores saved times.

**Files:**
- `scripts/copies/copies.py` — `manifest_path_files` and the restore rule in `standing`.
- `scripts/copies/test_copies.py` — rows in the repeated-gate test, with forced Git color.
- `docs/as-built/gate-checkouts.md` — the rule and its limit.

**Gotchas:** Cargo does not track `CARGO_MANIFEST_DIR`, because Cargo sets it for rustc. A dependency macro that reads the variable at expansion, in a caller without that text, stays outside this rule.

### Phase 44 — Every verify step runs where the queue places it  · status: todo
**Depends on:** 10g. Checkpoint 2 of section 8 in `docs/plans/builds-queue-placement.md`. This phase first fixes the 14:04 disk-decline defect. verify-unit has the function list.

**Constraints from 10g (architect review of 10g, 2026-11-10).** The Work Order settles each item below before work is divided.
1. **Queue policy.** `_read_host_evidence` excludes the local and scaler providers, and `_placement_pass` orders requests without the priority rank. Integrate local admission, priority, continuation release and retry deadlines through the existing planner. Tests: competing waiters; a priority change; a remote-to-local retry that charges local memory once.
2. **Coordinator and claim holder.** `admission_service.py:2149` requires the adopted claim owner to equal the assignment owner. Adoption today moves ownership from the verify pid to the offload pid. Add a semantic claim-holder identity, separate from the coordinator. Test: a real verify-to-offload handoff that keeps the coordinator death check.
3. **Completion and retry.** `_handle_placement` keeps execution evidence in memory only, ignores `ExecutionReport.outcome` and never closes a consumed assignment. Define terminal reports, a safe replacement attempt, and rebuild from provider evidence after a restart. Keep unstarted execution separate from unavailable evidence. Tests: lost response, restart, pending-launch barrier, safe replacement. Phase 45 reuses this contract.
4. **Claim ownership.** `submitting_session` names the current claim owner. A takeover changes it with `pid` and `proc_start`; `holder_session` keeps the original job evidence. Use `wait_claim_identity`, not `owns_wait_claim`. Exact release and the offload identity hand-off already exist. Keep the interrupted-publication and unrelated-session refusal tests.
5. **Deploy.** Claim records, run.json and admission state are decoded strictly. A phase that changes one of them names in its Work Order: the writers to stop (`build-admission.service` and `.socket`, `build-memory-feed.service`, the claim writers), how restart is prevented, the conversion roots (`~/.local/state/mac-test`, `~/.local/state/build-targets/aws`, admission state), the interrupted-conversion check, and the stop, convert, merge, start order. The showrunner holds the merge until the conversion runs.
6. **Views.** Converted paths show correct host accounting and the Machine label of the host that runs the step.

#### Work Order

**Goal:** Every converted verify step executes at the queue’s assigned destination, with correct claims, local accounting, and Machine labels.

**Spec:**

1. **Convert checkpoint 2 together.** The implementation baseline is `a10f44978` with builds merged.
   Switch verify, offload, copy-watch, and shared lint adapters in one checkpoint.
   Preserve fixed local requests from standalone lint, cargo, and `queue_place`.
   Phase 45 changes scaler gate routing.
   Phase 46 completes the remaining readers and removes unused selectors.
   Preserve source transfers through `copies.remote_rsync` and `RSYNC_RECEIVER_WRITE_TIME_FLAGS`.

2. **Fix the 14:04 disk-decline defect first.**
   Replace saved dynamic exclusions with fixed placement constraints.
   Keep every configured candidate unless a fixed rule excludes it.
   Use `step_routing_rule` for pins, platform requirements, tree writes, local artifacts, and `final` steps.
   Keep disk, reachability, busy, spending, and hold results in refreshed evidence.
   Treat `StepRecord.mac_disk_exclusions` as diagnostic evidence.
   Never use that field to remove a candidate permanently.

   Extend `test_verify_mac_disk_leave.py` first.
   Start one episode while Mac disk evidence refuses placement and local memory refuses execution.
   Refresh Mac disk evidence without changing the episode.
   Require a Mac assignment, one authorized remote execution, and zero local executions.
   Repeat with an additional disabled Linux target.
   Preserve the episode, `since`, run place, and priority.

3. **Use one queue order and the existing local planner.**
   Extend `PlacementRequest` with the existing run and caller identities needed for queue ordering.
   Carry the actual step arguments and local admission context.
   Use `LocalAdmissionContext` and `LocalAdmissionUnavailable(reason)` for that context.
   Convert missing wire values into these variants at the boundary.

   Replace the independent placement ordering in `_placement_pass` and `_placement_reply`.
   Use `_queue_order`, `waiter_order`, the existing run place, and the current `QueuePriority`.
   Include converted placement waiters and existing fixed local waiters in that order.
   A priority change affects the next placement pass.

   Evaluate local candidates through `memory_admit.admission_pass`.
   Reuse its memory, pressure, continuation, spacing, starts-alone, and head-protection rules.
   Use the same immutable inputs for placement evaluation and the final local admission decision.
   Remove remote-selected projections before committing local effects.
   Commit spacing only when `_admit` creates a local reservation.
   Never create a local reservation for a remote assignment.

   Add `LocalPlacementGranted(assignment, grant)` as a typed reply.
   Its grant uses the existing local reservation and execution controls.
   The converted local adapter consumes that grant without another admission request.
   Remote permission consumption remains separate from local memory admission.

4. **Preserve token and hold prerequisites.**
   Use `LocalExecutionPrerequisite = TokenRequired | TokenHeld | TokenNotRequired`.
   Report changes separately from immutable placement constraints.
   Treat token readiness as unavailable after restart until the caller confirms ownership.

   Keep the episode queued while its token is unavailable.
   Permit remote placement during that wait.
   Permit local memory admission only after the caller holds its required token.
   Keep `with_token_renewal` around routing and memory waits.
   Release the token when the selected route becomes remote.
   Preserve nested token reuse and the existing execution ownership guard.

   Remove host selection from converted token, hold, and memory wait hooks.
   A maintenance callback may renew a token.
   It cannot choose a destination.
   Keep explicit submitting-session identity at every hold-policy boundary.
   A local hold recheck can prevent execution.
   It cannot select another host independently.

5. **Separate the coordinator from the claim holder.**
   Keep `SubmittingOwner` as the checked local coordinator identity.
   Add `ClaimHolderIdentity(host, pid, start_time)` for the local adapter that owns the provider claim.
   Add `ClaimHolderUnconfirmed(reason)` for unresolved recovery evidence.
   Replace `AdoptedProviderClaim.owner` with its semantic holder identity.
   Carry the holder in `ConsumePermission`.
   Save the validated holder with permission consumption.

   Validate the coordinator’s lifetime separately from the holder’s lifetime.
   Validate the holder through checked local process evidence and the existing claim transfer.
   For verify, require the holder to belong to the coordinator’s process tree.
   For a direct offload request, permit the coordinator itself as the holder.
   Use background process evidence for these checks.
   Wait for fresh evidence when ownership cannot be confirmed.

   Require the provider claim to match the assignment’s provider, target, scope, slot, episode, and attempt.
   Require the current submitting session and host generation to match.
   Reject a cleanup takeover as evidence for a new launch.
   Represent retained cleanup claims separately from launch-capable adopted claims.
   Only a launch-capable adopted claim can construct `ReservedHost`.

   Keep `_placement_owner_live` as the coordinator death check.
   Never require the offload PID to equal the verify PID.
   Never open a local pidfd for a remote execution PID.

6. **Claim the exact assignment before consuming permission.**
   Add an assignment-bound path to `command_reserve`, `command_claim_wait`, and `command_claim`.
   Pass the queue’s episode and attempt into the existing claim identity.
   Acquire only the assigned scope and slot under the existing target lock.
   Do not scan another target or another slot after refusal.

   Preserve `RepositoryScope` and `WholeMacScope` overlap rules.
   Use the slot carried by `RepositoryScope` for Linux claims.
   Validate agreement between that slot and `PlacementDestination.slot`.
   Distinct known Mac repository scopes can coexist.
   An unknown whole-Mac scope overlaps those scopes.

   Adopt the exact claim before requesting permission.
   Use `wait_claim_identity` for the current claim owner.
   Preserve strict session equality in `adopt_wait_claim`.
   Preserve takeover updates to `pid`, `proc_start`, and `submitting_session`.
   Preserve the original job evidence in `holder_session`.

   A definite claim refusal reports `ClaimFailure`.
   An uncertain claim response requires inspection of the same claim identity.
   It cannot trigger a new claim or another placement attempt.
   A cleanup takeover completes cleanup and reports its result.
   It cannot reclaim capacity and launch again within the same assignment.

7. **Consume remote permission once.**
   Extend `_consume_permission` to validate the separate holder identity.
   Preserve assignment, source, coordinator, exact claim, session, and generation validation.
   Persist consumption and its provider reference before returning `FirstAuthorization`.

   Execute only after receiving that first authorization.
   A repeated consumption request returns `DispatchStatus`.
   This rule applies across transport request IDs and service restarts.
   A lost authorization response permits zero executions for that attempt.
   Never recover authorization from status.
   Never resend an uncertain remote start command.

   Keep consumed status while an assignment or its provider claim remains unresolved.
   Preserve status-only behavior after closure and replay pruning.
   Only a new queue decision creates another attempt.

8. **Define completion and retry reports.**
   Replace `ExecutionReport.outcome: str` with `AttemptOutcome`.
   Use these variants:

   | Variant | Meaning |
   |---|---|
   | `AttemptRunning` | The adapter reports active execution. |
   | `AttemptCompleted(status)` | The adapter has a final command result. |
   | `PrelaunchRefused(reason)` | The adapter confirms that it started no command. |
   | `AttemptCancelled(reason)` | The adapter has stopped its execution path. |
   | `AttemptOutcomeUnavailable(reason)` | The adapter cannot establish a final result. |

   Keep `ExecutionNotStarted`, `ObservedExecution`, and `ExecutionEvidenceUnavailable` distinct.
   An unavailable observation never means that execution did not start.

   Add `RetryDecision = RetryAt(deadline) | DoNotRetry(reason)`.
   Apply the existing `remember_mac_retry` and `ssh_retry_hook` policy in the queue.
   Preserve normal polling for busy, blocked, and disk declines.
   Preserve the 180-second delay for other temporary Mac outcomes.
   Preserve permanent declines, pinned startup behavior, Mac-only refusals, and permitted fallback.
   Persist retry deadlines separately from evidence freshness.

   Process terminal reports in `_handle_placement`.
   Close the exact assignment after its execution path has settled.
   Remove only that assignment’s current pointer.
   Preserve the episode’s queue position for permitted retries.
   Give each retry a new attempt ID.
   Reject late reports as authority over a replacement assignment.
   Still accept relevant evidence for an older retained provider claim.

   Keep final command results terminal under the existing caller policy.
   Do not retry a failed test merely because another host becomes available.

9. **Require a pending-operation barrier before absence.**
   Track pending claim and start operations in `ProcessController`.
   Give those operations semantic states.
   Do not publish a settled state before their child processes finish.

   Preserve deferred claim interruption in `complete_claim`.
   Stop the active adapter path before cleanup accepts absence.
   Resolve each pending claim and start operation before releasing its exact claim.
   Keep `claimed_run` and its existing cleanup path as the release authority.

   A local SSH exit, owner death, timeout, or empty remote scan proves no departure.
   If pending startup cannot be resolved, retain the provider claim.
   Report unavailable evidence and refuse overlapping reuse.
   Do not add a remote replay marker or another cleanup protocol, except the start fence below.

   Amendment (builds, 2026-11-12, from the closure review): one start fence settles an unacknowledged start.
   The race: ssh ends with no start acknowledgement, a scan finds no process, and the delayed original command starts after release.
   Existing claim records cannot tell that delayed start from an abandoned one.
   Without the fence, each lost acknowledgement keeps a Mac slot out of service until a hand cleanup.
   The fence is a lock and a cancel file per exact claim under the provider state folder, plus one local pending-start record.
   It lives outside the mirror, so a source copy cannot delete it.
   The fence files remove themselves when their claim settles. No hand step and no signal handler.

   After restart, reconstruct execution and cleanup views from existing provider records and background observations.
   Observe existing runner markers, mirror process evidence, and host generation.
   Do not invoke destructive cleanup from the service reader.
   A lost in-memory execution report cannot release capacity.
   A replacement attempt requires a settled prior dispatch path and valid provider evidence.

10. **Move speculative destination policy into the queue.**
    Add `PlacementPurpose = NormalStep | SpeculativeCopy(parent_episode, call_started)`.
    Keep a speculative copy separate from its local parent attempt.
    Submit its source facts and candidate lifetime from `Watch.update_candidate`.
    Move newest-candidate ordering from `newest_candidate` into queue policy.
    Preserve its current ordering and copy-yield rules.

    `Watch.try_copy` requests placement.
    It does not call `free_now` to choose capacity.
    `start_copy` starts only the assigned attempt.
    The offload adapter claims, adopts, and consumes that attempt normally.

    Preserve first-result handling, loser stopping, logs, fetch behavior, and source locks.
    Keep the local parent’s actual reservation while it continues compiling.
    Release only a remote-selected waiter’s unused local continuation claim.
    An uncertain speculative loser retains its provider claim until departure is confirmed.

11. **Carry the execution host through converted rows.**
    Use `ExecutionIdentity` for physical execution grouping.
    Keep the logical verify run identity separate.
    Group physical executions by host, generation, PID, and start time.
    Equal PID and start values on different hosts remain separate.

    Extend the service snapshot and `RunningStep` representation for converted execution identity.
    Use a semantic unavailable-execution variant when process identity is unknown.
    Show the assigned host with an uncertain state when execution evidence is unavailable.
    Do not invent a remote PID or a remote memory value.

    Update `queue_view.running_builds`, `queue_view.render_view`, and `queue_card._recorded_evidence`.
    Update `memory_feed.reading` and its grouping.
    Render each converted row’s Machine value from its execution or assignment host.
    Stop applying one `store.host_name()` value to all converted rows.
    Keep existing cloud labels correct.

    Exclude remote execution memory from local admission totals, continuation slots, local samplers, and local run keep-asides.
    Preserve any actual local parent charge during speculative execution.
    Unknown remote memory remains unknown.

12. **Give verify-unit the complete adapter edit list before implementation.**
    Use the following lines from `a10f44978`.
    Reconcile this list against any accepted incoming verify revision before editing.
    Verify-unit remains the owner of `verify.sh` and `offload.py`.

    | `scripts/direct/verify.sh` function | Current line | Required change |
    |---|---:|---|
    | `run_step` | 773 | Submit the actual step and remove saved eligibility refresh and selecting hooks. |
    | `step_can_retry` | 2058 | Replace saved-table retry policy with the queue’s outcome. |
    | `try_mac_test` | 2125 | Pass the assignment, exact claim, coordinator, and session. |
    | `check_routing_hold` | 2358 | Preserve execution checks without choosing a route. |
    | `initialize_step_record` | 2370 | Record fixed candidates and submit explicit identity. |
    | `route_compiling_step` | 2484 | Translate the command and execute its assigned route. |
    | `step_stopped_by_hold` | 2555 | Return the queue outcome without an independent destination decision. |
    | `register_mac_wait` | 2567 | Replace the selecting idle hook with queue waiter reporting. |
    | `remember_mac_retry` | 2581 | Report the attempt outcome instead of calculating retry policy. |
    | `target_platform` | 2615 | Read assigned destination metadata. |
    | `target_for_settings` | 2626 | Read assigned destination metadata. |
    | `release_wait_claim` | 2634 | Preserve exact release and cancel the corresponding assignment. |
    | `ssh_reserve_hook` | 2645 | Remove its selecting role from converted paths. |
    | `ssh_retry_hook` | 2672 | Remove its target loop from converted paths. |
    | `try_reserved_target` | 2703 | Execute only the assignment associated with the exact claim. |
    | `mac_retry_hook` | 2719 | Remove its availability and retry decisions from converted paths. |
    | `check_mac_only_result` | 2770 | Render the queue’s refusal or retry outcome. |
    | `route_test` | 2777 | Replace the target loop with placement waiting and exact dispatch. |
    | `start_copy_watch` | 2946 | Pass fixed source constraints, parent identity, and explicit session. |
    | `run_local_test` | 3033 | Reuse the selected local grant and preserve copy result handling. |
    | `acquire_token` | 3540 | Replace its SSH retry hooks with queue placement handling. |
    | `route_test` | 3797 | Preserve the `with_token_renewal` wrapper. |
    | `buildlog_wait_for_memory` | 3859 | Report token readiness and reuse the selected local grant. |
    | `build_hold_wait_local` | 3915 | Preserve hold and token handling without selecting another host. |
    | `verify_cleanup` | 4022 | Cancel the exact episode and preserve provider cleanup ownership. |

    The definition at line 3797 wraps the earlier `route_test`.
    Preserve the generated `route_test_without_renewal` boundary.
    Also update the top-level routing calls near lines 3272–3288 and initial token handling near line 3647.

    | `scripts/mac_test/offload.py` function | Current line | Required change |
    |---|---:|---|
    | `ProcessController.__init__` | 767 | Hold typed assignment, holder, and pending-operation state. |
    | `ProcessController.stop` | 773 | Finish child stopping before declaring the operation settled. |
    | `ProcessController.complete_claim` | 796 | Retain exact claim evidence through interruption and response loss. |
    | `ProcessController.stream_mac_run` | 884 | Track one pending start and prohibit uncertain command resends. |
    | `claim_arguments` | 1331 | Pass the assigned scope, slot, identity, and submitting session. |
    | `release_claim` | 1362 | Preserve exact release and return a typed confirmed or unconfirmed result. |
    | `read_claim_answer` | 1388 | Reject identity or slot differences from the assignment. |
    | `claim_for_run` | 2168 | Acquire or adopt only the assigned claim. |
    | `claimed_run` | 2197 | Consume permission before execution and report typed outcomes through existing cleanup. |
    | `run_offload` | 2469 | Execute one assigned target without another destination decision. |
    | `leased_offload` | 2529 | Lease only the assigned slot and report refusal to the queue. |
    | `parser` | 2603 | Accept assignment, coordinator, claim, and explicit session inputs. |
    | `request_from_arguments` | 2638 | Convert missing inputs into semantic request variants. |
    | `main` | 2723 | Submit fixed-destination requests for direct callers and report final placement outcomes. |

    Extend `OffloadRequest`, `CliArguments`, and `AttemptEvidence` for these boundaries.
    Direct offload callers retain their existing fixed destination through `PinnedHost`.
    Preserve `release_claim`’s existing exact identity comparison.
    This phase does not repeat the Phase 10g PID-release repair.

13. **Preserve wire compatibility and one authority per caller.**
    Keep version 1 and 2 local and cloud request decoding and reply shapes.
    Keep their existing episodes, positions, and fixed destinations.
    Preserve existing Phase 10g placement decoding at the wire boundary with semantic defaults.
    Require placement support for converted callers.
    Refuse a version disagreement without restoring client-side selection.

    Drain older routing shells through the existing caller-drain mechanism.
    Do not terminate their work for this checkpoint.
    Declare conversion complete only after the older selecting callers finish.
    Leave shared selectors available while unconverted callers still need them.

**Files:**

- `scripts/build_queue/placement.py` — Add holder, prerequisite, purpose, outcome, and retry types; add pure retry and copy ordering policy.
- `scripts/build_queue/admission_wire.py` — Extend placement requests, permission consumption, reports, and local placement replies; update codecs.
- `scripts/build_queue/admission_protocol.py` — Export the new messages and semantic variants.
- `scripts/build_queue/admission_records.py` — Extend saved assignments and host-scoped snapshot rows.
- `scripts/build_queue/admission_service.py` — Change `_run_pass`, `_inputs`, `_queue_order`, `_placement_pass`, `_placement_reply`, `_consume_permission`, `_handle_placement`, `_restore`, and `persist`.
- `scripts/build_queue/admission_client.py` — Extend placement calls, step context, shell results, prerequisite reports, cancellation, and local grant reuse.
- `scripts/lint/memory_admit.py` — Integrate placement projections through `admission_pass`, `admission_walk`, and `waiter_order`; preserve admission arithmetic.
- `scripts/build_targets/build_targets.py` — Add fixed constraint construction; change assignment-bound reservation and explicit-session hold boundaries.
- `scripts/mac_test/mac_test.py` — Extend `claim_once`, `command_claim_wait`, and `command_claim` for exact assigned scope and slot.
- `scripts/direct/verify.sh` — Change the functions and top-level calls listed in Spec 12.
- `scripts/mac_test/offload.py` — Change the functions and domain records listed in Spec 12.
- `scripts/mac_test/copy_watch.py` — Change `WatchRequest`, `Watch.update_candidate`, `Watch.try_copy`, `newest_candidate`, `start_copy`, and argument decoding.
- `scripts/direct/step_record.py` — Change `execute_cli`, `mac_eligible`, and record use so dynamic exclusions remain diagnostic.
- `scripts/lint/memory_gate.sh` — Change `buildlog_wait_for_memory` and hold adapters for placement context and grant reuse.
- `scripts/lint/invoke.sh` — Change `run_step_attempt` to use the selected local grant and preserve its release path.
- `scripts/build_queue/queue_view.py` — Change `running_builds` and `render_view` for physical grouping and Machine labels.
- `scripts/build_queue/queue_card.py` — Change `_recorded_evidence` for per-execution Machine labels.
- `scripts/build_queue/memory_feed.py` — Change `reading`, physical grouping, and converted row labels.
- `scripts/build_queue/test_placement.py` — Test queue ordering, claims, consumption, completion, recovery, and responsiveness.
- `scripts/build_queue/test_admission_service.py` — Test local planner integration, priority, continuation release, and restart.
- `scripts/build_queue/test_admission_protocol.py` — Test new messages, typed outcomes, and older request compatibility.
- `scripts/build_queue/test_admission_client.py` — Test placement waiting, prerequisite reports, grant reuse, and lost replies.
- `scripts/build_queue/admission_test_support.py` — Extend private service, process, clock, and provider fixtures.
- `scripts/build_queue/test_queue_view.py` — Test host-scoped grouping and Machine labels.
- `scripts/build_queue/test_queue_card.py` — Test converted execution labels and unavailable evidence.
- `scripts/build_queue/test_memory_feed.py` — Test host-scoped rows and local memory isolation.
- `scripts/lint/test_memory_admit.py` — Test competing local projections and unchanged admission guarantees.
- `scripts/lint/test_memory_gate_wait.py` — Test placement-aware waits and fixed local compatibility.
- `scripts/build_targets/test_targets.py` — Test exact assignment claims, explicit sessions, and publication recovery.
- `scripts/mac_test/test_mac_test.py` — Test assigned slots, adoption, takeover, and exact release.
- `scripts/mac_test/test_process_start_claims.py` — Preserve process-start and unrelated-session adoption cases.
- `scripts/mac_test/test_offload.py` — Test assigned execution and fixed direct callers.
- `scripts/mac_test/test_offload_lifecycle.py` — Test adoption, consumption, execution reports, and terminal outcomes.
- `scripts/mac_test/test_offload_cleanup_leftovers.py` — Test pending startup and uncertain departure.
- `scripts/mac_test/test_offload_cleanup_takeover.py` — Test cleanup takeover without launch authority.
- `scripts/mac_test/test_copy_watch.py` — Test queue copy policy, first results, and retained losers.
- `scripts/direct/test_verify_mac_disk_leave.py` — Test the initial disk decline and refreshed Mac placement.
- `scripts/direct/test_verify_mac_memory_handoff.py` — Test the real verify-to-offload transfer and local continuation release.
- `scripts/direct/test_verify_mac_only.py` — Preserve Mac-only refusal behavior.
- `scripts/direct/test_verify_mac_retry_clock.py` — Preserve retry deadlines through queue policy.
- `scripts/direct/test_verify_ssh_targets_routing.py` — Preserve configured order, pins, and assigned slots.
- `scripts/direct/test_verify_step_routes.py` — Test compiling steps and fixed local steps.
- `scripts/direct/test_verify_step_routes_lint.py` — Test shared lint conversion and local tree writes.
- `scripts/direct/test_verify_token_memory_history.py` — Test token-before-memory behavior and one local charge.
- `scripts/direct/test_verify_token_renew.py` — Test renewal during placement waits.
- `scripts/direct/test_verify_mac_copy_results.py` — Preserve speculative first-result behavior.
- `scripts/direct/verify_mac_memory_retry_test_support.py` — Extend the real-shell fixture for placement responses.
- `scripts/direct/verify_ssh_targets_test_support.py` — Extend assigned SSH target fixtures.
- `scripts/direct/verify_step_routes_test_support.py` — Extend compiling-step placement fixtures.
- `scripts/mac_test/offload_test_support.py` — pinned placement fixture setup
- `scripts/direct/verify_mac_offload_test_support.py` — pinned placement fixture setup
- `scripts/direct/test_verify_lint_local_writes.py` — pinned local repair rows
- `docs/as-built/build-memory-admission.md` — Document the placement, retry, identity, and recovery contracts.
- `docs/as-built/mac-test-offload.md` — Document assigned offload execution and provider-owned cleanup.

**Workers:** 2 writers + 1 tester.

- `impl` owns the queue production files, local planner integration, views and as-built docs.
  Files are `placement.py`, `admission_wire.py`, `admission_protocol.py`, `admission_records.py`, `admission_service.py`, `admission_client.py`, `memory_admit.py`, `queue_view.py`, `queue_card.py`, `memory_feed.py` and the two as-built docs.
  Hub files are `placement.py`, `admission_wire.py`, `admission_records.py`, `admission_service.py` and `memory_admit.py`.
- `adapter` owns `verify.sh`, `offload.py`, `copy_watch.py`, `step_record.py`, `mac_test.py`, `build_targets.py`, `memory_gate.sh` and `invoke.sh`.
  The director sends Spec 12 to verify-unit, and the target and lint lists to aws-unit and sdlc, before this writer starts.
- `test` owns the named test modules and fixture helpers.
  Real test lanes are `scripts/build_queue/test_*.py`, `scripts/direct/test_verify_*.py`, `scripts/mac_test/test_*.py`, `scripts/build_targets/test_*.py` and `scripts/lint/test_*.py`.
  The tester writes the acceptance cases from this Spec and requests production changes from the owning writer.
- No hub file has two writers. A new message or type that both writers need belongs to `impl`.

**State:** This phase changes stored admission records.

The changed records contain placement request context, consumed claim-holder identity, and retry deadlines.
Physical claims remain in the existing provider stores.
Execution observations remain reconstructed evidence.
The phase adds no second durable provider lifecycle. The start fence in item 9 is the one exception; it settles a start and removes itself.

Claim records and `run.json` retain their Phase 10g shape.
Their conversion is **none**.
Validate them during the stopped-writer deploy.

Use the following deploy procedure:

1. Prepare `deploy_convert.py` outside the repository in the director’s Phase 44 record folder.
2. Prove conversion against private copies of admission state and provider records.
3. Run `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py hold --drain --for 'Phase 44 deploy'` (Phase 44b). Continue only on exit 0 and a last line `drained`. Keep the hold until step 12 ends. The drain covers verify.sh, merge_gate.py and targeted_test.py only: drain standalone offload adapters, copy watchers and manual claim commands separately.
4. Prevent new launches through their units, timers, sockets, and supervising directors.
5. Stop `build-admission.service`, `build-admission.socket`, and `build-memory-feed.service`.
6. Keep their activation sources disabled or runtime-masked throughout conversion and installation.
7. Confirm that no claim writer remains active.
8. Convert the admission `state.json` under the service’s configured `--state-dir`.
9. Validate `~/.local/state/mac-test` and `~/.local/state/build-targets/aws`.
10. Validate every additional configured SSH target root from `ssh_target_state_directory`.
11. Merge and install the converted code while writers remain stopped.
12. Restore activation and start the services.
13. Verify strict decoding, preserved positions, consumed status, and provider retention.
14. Resume claim writers and converted callers.
15. Delete the external converter after the deploy succeeds.

The default admission state root is `~/.local/state/buildlog/admission`.
Use the service’s actual root when configuration changes it.

Convert stored records to the new shape outside the main reader.
Recover holder identity from exact provider evidence where available.
Use `ClaimHolderUnconfirmed` when recovery cannot prove it.
Preserve consumed permission in that case.
Never infer an available permission or provider absence during conversion.

Preserve version 1 and 2 local/cloud records, IDs, reservations, and queue positions.
Preserve assignment IDs, claim references, consumption, and unresolved capacity.
Do not invent retry deadlines from unavailable history.

Keep the original backup across interrupted conversion.
Use atomic replacement and an idempotent rerun.
Test interruption before replacement and after partial conversion.
Keep services stopped if conversion or strict validation fails.
The showrunner holds the merge until conversion succeeds.
A root-owned service stops and starts only by the user or under a sudo rule that the user grants.

**Constraints from prior phases:**

1. Phase 10g already provides durable assignments, single-use permission, version 3 messages, and asynchronous host evidence.
2. Phase 10g already provides exact release, explicit sessions, and strict claim decoding.
3. `submitting_session` identifies the current claim owner.
   `holder_session` preserves original job evidence.
4. `wait_claim_identity` replaces `owns_wait_claim`.
   Preserve interrupted-publication recovery and unrelated-session refusal.
5. Admission arithmetic remains in `memory_admit.py`.
   Preserve priority, run place, token ownership, and continuation semantics.
6. The user requires conversion outside the repository for changed stored shapes.
   The main reader supports only the installed shape.
7. The design contract assigns checkpoint 2 to this phase.
   Gate routing remains Phase 45 work.
8. Correct accounting and Machine labels ship with each converted path.
   Phase 46 cannot defer these corrections.
9. Phase 44b (on builds before this checkpoint) made `build_targets.hold_waited_on` skip a hold whose keep list holds the caller or an ancestor. It reads that lineage from `os.getpid()`.
   The admission service calls it for a requester (`admission_service._read_host_evidence`), so it reads the service's lineage. Phase 44c repairs this on the merged tree, and the two phases deploy together.

9. These files are read only in this phase: `admission_code.py`, `claim_state.py`, `copies.py`, `linux_runner.py`, `target_config.py`, `store.py` and `config/`.
   `claim_state.py` keeps its stored claim shape.
   A new service module must join `admission_code.py`; the writer that adds it reports that to the director.
10. aws-unit's builder cleanup removes the `aws` target from `config/build_targets.conf`, which leaves `targets=mac,natedev`.
   Mac then stays the only ssh target. Code and tests must not assume an `aws` row in that file.
   The same cleanup deletes `RELEASE_STATES`, `claim_matches_instance`, `capture_release_evidence` and `release_stopped_claims` from `mac_test.py`. Do not edit those four names.

**Acceptance gate:**

Run these checks from the phase checkout:

```sh
./scripts/lib/py scripts/production/targeted_test.py --checkout "$PWD"
basedpyright --pythonpath ~/.local/state/merge-gate/typecheck-venv/bin/python <changed-Python-files>
./scripts/lib/py -m scripts.berth.work_order --repository-root "$PWD" validate --document docs/plans/builds-queue.md --phase 44
```

Expand `<changed-Python-files>` to the changed Python paths.
Require `0 errors, 0 warnings, 0 notes`.
Require the checkout’s targeted tests to pass.

Use private roots, real fixture sockets, fake transports, and existing provider locks.
Contact no live service, session, Mac, or AWS account.

1. **Disk recovery:** Initial Mac disk refusal and local memory pressure keep one waiter.
   Refreshed disk evidence assigns that waiter to the Mac.
   Local execution count remains zero.
2. **Changing evidence:** Busy, reachability, spending, and hold evidence refresh during a wait.
   Fixed platform and source-write exclusions remain fixed.
3. **Queue order:** Competing fixed local and movable waiters use the same order.
   A priority change affects the next decision.
4. **Retry accounting:** A definite remote refusal permits a new local attempt.
   That attempt takes one local reservation and charges memory once.
5. **Continuation release:** Remote selection releases the waiting step’s local continuation claim.
   It changes no local start spacing.
6. **Token prerequisite:** A token waiter holds no local memory reservation.
   Remote placement remains possible.
   Renewal continues during a placement wait.
7. **Exact contention:** Two episodes race for one Mac repository scope.
   One overlapping claim wins under the provider lock.
8. **Scope overlap:** Two distinct known Mac repository scopes coexist.
   A whole-Mac claim overlaps both.
9. **Linux slot:** Two episodes race for one Linux repository slot.
   At most one adopted claim supports authorization.
   Neither adapter substitutes another slot.
10. **Real handoff:** Verify transfers its exact claim to an offload child with a different PID.
    Consumption succeeds while the coordinator lives.
    Coordinator death still prevents unused permission consumption.
11. **Ownership recovery:** Interrupted reservation or identity publication preserves recoverable ownership.
    A third session cannot adopt the retained claim.
12. **Obsolete assignment:** Pause an adapter after assignment and replace that assignment.
    Its late claim receives no authorization.
    Its cleanup uses the original exact identity.
13. **Owner death:** Death before adoption permits unlaunched cleanup.
    Death after adoption retains remote-bound capacity.
14. **Late release:** Attempt A cannot release attempt B under the same local process.
15. **Lost authorization:** Drop the first authorization response.
    Repeat consumption before and after restart.
    The command executes zero times for that attempt.
16. **Delivered authorization:** Deliver the first authorization and repeat consumption across restart.
    The command executes once.
17. **Pending startup:** Pause an authorized adapter before its start operation finishes.
    An empty scan cannot release its claim.
    Cleanup requires the pending-operation barrier.
18. **Unreachable cleanup:** Retain an uncertain claim across restart.
    Another episode cannot reuse its overlapping scope.
19. **Terminal reports:** Completion closes the exact assignment.
    A permitted retry gets a new attempt with the original position and deadline.
    A late old report cannot close its replacement.
20. **Recovery evidence:** Restart after losing an in-memory execution report.
    Rebuild from provider evidence.
    Keep unstarted execution separate from unavailable evidence.
21. **Session policy:** One session’s hold exemption cannot authorize another session.
    Configuration changes invalidate affected evidence.
22. **Host identity:** Equal PID and start values on local and remote hosts remain separate.
    Only local submitting owners reach pidfd watches.
23. **Converted views:** View, card, and memory feed show the actual Mac or SSH host.
    Mixed local and cloud rows retain correct labels.
24. **Memory isolation:** Remote rows contribute no remote memory to local room or continuation accounting.
    A speculative local parent retains its actual local charge.
25. **Responsiveness:** Block evidence, claim, and cleanup operations in separate cases.
    Real socket View, Release, and eligible local admission requests each answer within one second.
26. **Evidence expiry:** Expire evidence during a blocked probe.
    New placement waits for evidence.
    Existing provider claims remain held.
27. **Caller policy:** Preserve pins, configured order, holds, priority, retry deadlines, and Mac-only refusals.
    Preserve local tree writes, artifacts, and `final` behavior.
28. **Speculative copies:** The newest eligible copy follows existing policy.
    First-result handling stops the loser.
    Uncertain loser cleanup retains its exact claim.
29. **Compatibility:** Restore old fixed local/cloud waiters, an unconsumed assignment, and a consumed uncertain attempt.
    Preserve positions, identities, and permission status.
30. **Transfers:** Changed content receives receiver write time.
    Unchanged content retains its earlier time.
31. **Conversion:** Interrupt conversion before replacement and after partial progress.
    Rerun without replacing the original backup.
    Strict readers accept the converted records.
32. **Caller coverage:** Exercise test, check, build, doc, example, lint, and `final` through converted paths.
    Exercise direct offload and fixed local shared lint callers.

### Phase 44c — The queue service lets a run that a drain keeps pass the hold  · status: todo
**Depends on:** 44 and 44b (44b is on builds and main at b09bc83ae). It lands with Phase 44 in one deploy: the showrunner holds the Phase 44 merge until this phase is ready. Found by the 44b architect review, 2026-11-10. Schedule change (user, 2026-11-12: "do what you can to parallelize"): 44c starts at once in its own worktree, on a local snapshot of the Phase 44 tree with builds merged in, while Phase 44 finishes its final test run and review. Its merge resolution is recorded and reused for the Phase 44 checkpoint merge.

#### Work Order

**Goal:** A run that a hold keeps (a working run the drain keeps, or a `--keep <pid>`) and its children get a local placement from the admission service while the hold is in force. Another run of the same session still waits.

**Spec:**

1. **Merge first.** Merge builds (with 44b) into the Phase 44 branch at the Phase 44 checkpoint. 44b's `build_targets.hold_waited_on(holds, owner, directory)` reads the keep lineage from `build_hold.caller_processes(build_hold.PROC_ROOT, os.getpid())`.
2. **The requester's lineage, not the service's.** `admission_service._read_host_evidence` calls `hold_waited_on` for a local destination inside the service, so it reads the service's lineage. Give `hold_waited_on` the caller lineage as an argument of a semantic type: a confirmed lineage, or an unavailable lineage with its reason. `caller_waits_on_hold` passes the current process's lineage. The service passes the lineage of the checked requesting process, `PlacementRequest.owner` (`placement.SubmittingOwner(host, pid, start_time)`), read only when the start time still matches.
3. **Unavailable lineage.** When the requester's lineage cannot be read or its start time differs, no keep list applies: a hold of another session blocks, as for a caller that no hold keeps. Do not treat an unavailable lineage as kept.
4. **One decision per process.** `placement.HostEvidenceKey` caches host evidence per session. Add the requesting process identity to the key of a local hold decision, so two processes of one session never share a keep decision.
5. **Docs.** In `docs/plans/builds-queue-placement.md` (adapter table, section 8), replace `build_targets.py::caller_holds_only_own` with `caller_waits_on_hold`.
6. **Drain wording.** With `--drain`, the hold text says that the drain stops waiting runs. Today the banner (`build_hold.py:880`, "verify.sh waits by itself") and the audit line (`hold_enforcer.py:227`, "waits at its own hold gate") say a waiting run continues, and then the drain stops it. Without `--drain`, the text stays the same. builds placed this here on 2026-11-11 (C1381 validation): it must land before the deploy that uses `--drain`.

**Files:**

- `scripts/build_targets/build_targets.py`
- `scripts/build_hold/build_hold.py`
- `scripts/build_hold/hold_enforcer.py`
- `scripts/build_hold/test_hold_enforcer.py`
- `scripts/build_queue/admission_service.py`
- `scripts/build_queue/placement.py`
- `docs/plans/builds-queue-placement.md`
- `scripts/build_targets/test_build_targets.py`
- `scripts/build_queue/test_admission_service.py`
- `scripts/build_hold/test_hold_applies.py`

**Workers:** 1 writer + 1 tester.

- `impl` owns `build_targets.py`, `build_hold.py`, `hold_enforcer.py`, `admission_service.py`, `placement.py` and the placement doc. Hub file: `build_targets.py`.
- `test` owns the four test files and their support files. Tests use a private proc root and private hold folders, never live processes, the live hold or the live admission service.

**State:** no stored record changes. Conversion: none.

**Acceptance:**

1. Add rows to the existing start-check and local host-evidence tables: a kept descendant gets a local placement; an unrelated process of the same session waits; a reused pid (start time differs) waits; with two holds, a caller kept by one hold waits on the other.
2. The 44b kept-caller rows in `test_hold_applies.py` and `test_build_targets.py` stay green with no assertion change.
3. Add a row to the `hold --drain` test in `test_hold_enforcer.py`: the output has no "waits by itself" and no "its own hold gate" text, and it says the drain stops waiting runs. The `test_build_hold.py:1020` row without `--drain` stays the same.

**Verification:** `python3 -m unittest scripts.build_targets.test_build_targets scripts.build_hold.test_hold_applies scripts.build_hold.test_hold_enforcer scripts.build_hold.test_build_hold scripts.build_queue.test_admission_service`, then `targeted_test.py` for the unit, and basedpyright on each changed file with zero findings.

### Phase 44i — Queue priority holds an ordered list of units  · status: done
**Depends on:** none. Runs on builds-queue beside Phase 44, ahead of it. Ordered urgent by builds on 2026-11-11 for the user: the user bumped demo, then cables. The second bump replaced the first, and cables' nextest stayed at place 6.

Work:
1. Queue priority holds an ordered list of units, each with its why, time and actor.
2. `bump` adds its unit at the end. A unit already in the list keeps its place.
3. `clear` empties the list. `clear <unit>` removes that unit only.
4. Admission orders the steps of the first unit, then the second unit, then all other steps.
5. A stored single-unit priority reads as a list of one. No data step.
6. The queue page and the head explanation name every unit in order.
7. `/build:queue` describes the new bump and clear.

**Workers:** 1 writer. **Verification:** the admission service, memory_admit, build_queue, queue_view and head_facts tests, `targeted_test.py`, and basedpyright with zero findings. **Deploy:** restart `build-admission.service` after the hash reaches main.

### Phase 44f — The cap stays known while a step starts or ends  · status: todo
**Depends on:** 44c, because Phase 44 also edits `scripts/lint/memory_admit.py`. Starts after 44j, before 44h. Order set by builds on 2026-11-11: 44g, then 44j, go first after the 44 and 44c deploy. The user on 2026-11-11: "the cap keeps showing as unavailable - then it comes back - this should never happen".

Cause, measured 08:29–08:33: 4 of 50 build page readings showed "Cap unavailable: swap-backed use unavailable for <run>: scope not measured". Each was a run in its first pass after admission. `memory_admit.py` (line 1775 on main) gives `RunUseUnavailable("scope not measured")` to every step whose reading is not `MeasuredMemory`, that is `PromisedMemory` (admitted, scope not populated yet) and `GoneMemory` (its processes ended). One such run makes `memory_room` set `CapUnavailable`.

Work:
1. A `PromisedMemory` or `GoneMemory` step has `AnonymousRunUse(0)`: no process of that step holds memory. Its keep-aside stays its full need, so room does not change. Only the cap becomes known.
2. `RunUseUnavailable` stays only for a real read failure.
3. Tests: one row each for a promised step and a gone step: the cap is a number, and room equals the value before the change.

**Workers:** 1 writer. **Verification:** the memory_admit and admission tests, `targeted_test.py`, and basedpyright with zero findings.

### Phase 44h — Admission never starts a GPU test step that the GPU cannot hold  · status: todo
**Depends on:** 44f, because both edit `scripts/lint/memory_admit.py`. Starts after 44f, before 44e. Ordered by builds on 2026-11-11. Facts from hana: the natedev GPU has 16303 MiB. One full hana suite peaks at 15768 MiB. Per test: median 279 MiB, p90 791 MiB, largest 7971 MiB. Two hana suites at once ran out of GPU memory (OutOfMemory RenderError at 05:34 and 09:17).

More facts from hana (full suite on ca801be38c, 32 threads):
1. The board peak is 15768 MiB. The peak sum of the test processes is 8951 MiB. Other users of the GPU hold the rest.
2. The largest tests: clock_button_pixel_cases 7971 MiB, material_card_pixel_cases 5153 MiB, size-thumbs 2629 MiB, transform_translation_and_rotation 2585 MiB.
3. The out-of-memory errors come from large render tests that run at the same time, not from growth inside one test.
4. Per-test table: a copy is at `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/phase44h_gpu_memory.md`.

Item 3 means the GPU need of a step is its process sum, and room must come from live free GPU memory, because other processes hold GPU memory too.

Design (builds approved it with two changes, items 2 and 6):
1. GPU history: the sampler that records a nextest step's RAM peak also records its GPU peak, as `gpu_peak_bytes` beside `test_phase_peak_bytes` in `anon_peaks.jsonl`.
2. Per-process GPU use: bevy renders through Vulkan, and `nvidia-smi --query-compute-apps` can leave out graphics (G) processes. First, prove on one live hana render test that its pid shows with its used memory. If it does not, use the method hana's Phase 89 used for its per-test numbers (ask hana), or `nvidia-smi --query-accounted-apps`, or NVML per-process graphics use.
3. A GPU step: no list. A step is a GPU step when its history profile (the RAM key: host, repo, scope, package, filters, width) has a GPU peak above zero. Need = the RAM ranked peak and width scaling, plus a new margin key set with /tune.
4. No history: a profile with no GPU history, in a repo with GPU history on this host, needs the largest GPU peak of that repo within the history days. So two full hana suites never start together. A repo with no GPU history has no GPU steps. Its first run is sampled, and the next run is gated.
5. GPU room = live free GPU memory − the GPU need not yet reached by each admitted GPU step (the same rule as the RAM keep-aside). A host without `nvidia-smi` has no GPU gate. Mac and AWS placements take no natedev GPU room.
6. Speed: read free GPU memory only when the head step is a GPU step, not at every admission decision. Time the call, and name its cost in the checkpoint.
7. Wait text: a held step says that it waits for GPU memory, with its need and the free GPU memory in GiB.

The Work Order follows at phase start.

### Phase 44e — Every build page tooltip shows labelled rows in one shape  · status: todo
**Depends on:** 44c. Starts next after the 44 and 44c deploy, before 44d and 44a. The user asked on 2026-11-11, from a shot of the cap line tooltip ("Cap 24.3 GiB; anonymous use 0.0 GiB + CI use 0.1 GiB + free 27.2 GiB – safety 3.0 GiB; reserved 0.0 GiB; room 24.2 GiB"): "i would like this tooltip for the cap (and other tooltips) to be more structured showing name: value pairs in rows - i also don't know what anonymous means so you need to provide better context - i don't want huge tool tips but i do want them to be informative and communicate information consistently". The Work Order follows at phase start.

Every tooltip on `scripts/build_queue/memory_live.html` uses one shape:
1. A title line: the item name and its main value, as now ("Cap line · 24.3 GiB").
2. At most one short sentence that says what the item is.
3. Name and value rows. Names align left. Values align right in tabular numbers. A calculation shows its terms with `+` and `−`, a rule line, then the `=` result row.
4. The tooltip keeps its current width limit. No paragraph text.

The cap line tooltip becomes:

| Name | Value |
| --- | --- |
| Builds use | 0.0 GiB |
| hana CI | + 0.1 GiB |
| Free | + 27.2 GiB |
| Safety | − 3.0 GiB |
| Cap | = 24.3 GiB |

Reserved and room leave the cap tooltip, because the room tooltip shows them in the same shape.

Rules:
1. No page text says "anonymous". The page says "Builds use" and the builds-use tooltip sentence gives the context: memory that build processes hold now, not file cache, because the system can free file cache.
2. The feed sends numbers with names, not sentences. `room_text` and `keep_aside_text` in `scripts/build_queue/queue_evidence.py` and the run `keep_text` in `scripts/build_queue/memory_feed.py` become row lists. The page formats every amount through one function.
3. Run, waiting step, CI, group, swap and pressure tooltips use the same rows: for example Started, Uses, Reserves, Counted, Needs, Waiting for, Reason.
   The hana CI row shows only while a CI job runs. The user, 2026-11-11: "i don't think we need to show the ci reservation row unless a ci is running - it is really neglibible and can just be included in the build memory taken tool tips on the summary row at the top but not necessary to be on its own row without ci running". With no job running, the idle runner memory is one row in the summary row's build memory tooltip ("hana CI, idle"), and the Running table has no hana CI row.
   The hana CI tooltip adds a Limit row for a job with no reservation: "34 GiB (the hana-ci.slice MemoryMax)". Since resources Phase 88 removed the CI margin, no page text gives this limit.
4. The rename approved for Phase 44a moves here, because this phase rewrites that tooltip: the memory page row "worker background runs" becomes "unowned processes" (`memory_feed.py` group name and its `memory_live.html` text).
5. The estimate source (change C1309) says in plain words what happens, with no "same-shape", "promise", "headroom" or "floor". The user asked through builds on 2026-11-11: "stop using same-shape and just say in words what is actually happening - probably this only needs to show up in the html in any case". It shows on the build page only: `need_text` in `queue_evidence.py` becomes rows, and the waiting-for-memory notice in `admission_client.py` drops its source description. Builds approved this wording on 2026-11-11. Title "Estimate · 6.2 GiB"; sentence "Memory this step should need, from earlier runs of this build: the same project, step, profile and tests." Rows:
   1. From: "the largest peak of the last 20 runs, after the top 10% are dropped"; "the largest peak of the last 3 runs"; or "no earlier runs: the larger of the default amount and the peak of a build with the same profile".
   2. Runs from: "the last 14 days".
   3. Failed run: "4.1 GiB, from a failed run at 07:40" or "none".
   4. Extra room: "× 1.150 for a release build", "× 1.100 + 0.5 GiB for a dev build", or "none".
   5. Limit: "cut to 24.0 GiB from 26.3 GiB" or "none".

**Workers:** 1 writer + 1 tester.
- Writer: `queue_evidence.py` and `memory_feed.py` row data, and the `memory_live.html` tooltip renderer and CSS.
- Tester: `test_memory_hover.py`, `test_memory_feed.py` and `memory_page_proof.mjs` rows for each tooltip kind, with no "anonymous" text.

### Phase 44g — Mac builds show in the queue as their own rows, with their kind and progress  · status: done
**Landed alone (user, 2026-11-12: "deploy working things when they work"):** rows for build work started on the Mac, owner names from the owner record, local test progress, the one-decimal round-down percent, and the unit name from the staff record in each step name ("trunk check", repository name only when no unit is recorded). Rows for steps sent from natedev moved to Phase 44m, because their source is Phase 44 placement.
**Depends on:** 44 and 44c. Starts after their deploy, at the same time as 44j in its own worktree (builds, 2026-11-12, item 78). (Phase 44 records each Mac placement and gives each running step its machine through `admission_records.execution_host`). The user asked on 2026-11-11: "you should find a way to add the mac builds and indicate whether they are test mirrors or actual runs somehow - they should show in the build queue with their machine just like we show aws in their own rows with their own machine. progress should show". The Work Order follows at phase start.

Goal: the queue view, the queue card and the build page show each build or test on the Mac as its own row, as AWS rows show today (`memory_feed.CloudWorkRow`, `machine: "aws"`). Each Mac row shows:
1. **Machine:** `mac`.
2. **Kind:** in words the user reads at once (builds, 2026-11-11: no coined term). Working values: "sent from natedev" (the user's "test mirror") and "started on the Mac" (the user's "actual run"). The row shows the same columns and look as an AWS row. The split is the one the Mac activity check already uses (`docs/as-built/mac-test-offload.md`, `mac_activity_script`):
   - A test mirror is a natedev run that the queue placed on the Mac. It runs under `~/.local/state/mac-test/mirror/`, and a speculative copy is one too. The row names its unit, as a local row does.
   - A Mac build is other build work on the Mac: `cargo`, `rustc`, `cargo-nextest` or a `/target/` binary outside the mirror, such as a hana macOS CI job or a build in the user's own Mac clone. The row names the folder.
3. **Progress:** the same done / total bar as a local or AWS row. A test mirror gets its counter from the run's output, which the natedev caller already reads. A Mac build shows progress only when a counter can be read; otherwise the row says why, as a local row does.
4. **Memory:** a Mac row takes no natedev memory, so it stays out of the cap, the room and the bar, as AWS rows do.

Interpretation (director, 2026-11-11, a one-line revert): "test mirrors or actual runs" means queue-placed mirror work against all other Mac build work. If the user meant a speculative copy against a placed run, that is a third value of the same Kind field.

Schedule change (user, 2026-11-12: "do what you can to parallelize"): 44g starts at once in its own worktree, on a local snapshot of the Phase 44 tree, while Phase 44 finishes review and 44c runs. It lands in the same deploy as 44 and 44c.

#### Work Order

**Goal:** The queue view, the queue card and the build page show each build or test on the Mac as its own row, with machine `mac`, a kind, progress, and no natedev memory, as AWS rows show today.

**Spec:**

1. **Placed Mac steps.** Phase 44 already gives a running step its machine through `admission_records.execution_host` (`memory_feed.py:876`, `queue_card.py:146`, `queue_view.py:545`). Each running step whose execution host is the Mac shows as a Mac row with kind "sent from natedev". It names its unit, as a local row does.
2. **Other Mac build work.** Build work on the Mac that no queue step owns (`cargo`, `rustc`, `cargo-nextest`, or a `/target/` binary outside the mirror) shows as a Mac row with kind "started on the Mac". The row names the folder. Take the split from `mac_test.mac_activity_script`. Read it from a periodic reader with a stated age, never with an ssh call on each feed pass. If no periodic reader exists, add one owned by the feed's existing refresh loop and name its interval as a config key.
3. **One row type.** The Mac row uses the same columns and look as the AWS row (`memory_feed.CloudWorkRow`): widen `machine` to the machines it can hold, add the kind field. No coined term reaches the page.
4. **Progress.** A placed Mac step shows the same done / total bar as a local or AWS row, from the counter its natedev caller already reads. A "started on the Mac" row shows progress only when a counter can be read; otherwise it says why, as a local row does.
5. **Memory.** A Mac row takes no natedev memory: it stays out of the cap, the room and the bar, as AWS rows do.
6. **Absent Mac.** When the Mac is unreachable or its reading is older than its stated age, the placed rows stay and the "started on the Mac" rows show one line that says the reading is old, with its age.

**Files:**

- `scripts/build_queue/memory_feed.py`
- `scripts/build_queue/queue_view.py`
- `scripts/build_queue/queue_card.py`
- `scripts/build_queue/memory_live.html`
- `scripts/build_queue/test_memory_feed.py`
- `scripts/build_queue/test_queue_view.py`
- `scripts/build_queue/test_queue_card.py`

**Workers:** 1 writer + 1 tester.

- `impl` owns `memory_feed.py`, `queue_view.py`, `queue_card.py` and `memory_live.html`. Hub file: `memory_feed.py`.
- `test` owns the three test files. Tests use private state folders and a fake Mac reading, never the live Mac, the live feed or the live admission service.

**State:** none written beyond the periodic Mac reading, if Spec item 2 adds one. Conversion: none.

**Acceptance:**

1. A running step placed on the Mac shows as a Mac row, kind "sent from natedev", with its unit and a done / total bar, in the feed, the card and the view.
2. Mac build work outside the mirror shows as a Mac row, kind "started on the Mac", with its folder.
3. Mac rows add nothing to the cap, the room or the memory bar.
4. An old or absent Mac reading shows its age and keeps the placed rows.
5. Existing local and AWS row tests stay green with no assertion change.

**Added 2026-11-12 (user requests, same deploy):**
1. The step table has a `Stage` column after `Duration`. It shows the row's progress phase ("building" or "testing"). The user: "when we have things that have a build phase vs a test (or other phase) i would like to show the word "building" or "testing"".
2. When steps wait, a second header row sits directly above the first waiting row. Its duration header reads `Waiting`. The chart has a spacer of the same height at the same place. The user: "the column header should repeat when we have the Queue - and the column headers can repeat on the same row because they can differ".
3. Column order: `#`, `Step`, `Duration`, `Stage`, `Unit`, `Pid`, `Machine`, `vCPU`. The user: "Machine and vCPU should go at the end of the columns because they are only active while a build is running".
4. The heading reads `Next step room` with no waiting count. The user: "the 12 steps wait at the top can be removed".
5. Every percent on the build page and in `memory_admit.py` `progress_text` shows one decimal place and rounds down, so only a complete step shows 100.0%. The user, via builds: "the queue needs to show % complete with a decimal place because 100% is not accurate here" (835 of 836 units showed 100%).
6. Every running and waiting row shows its showrunner and unit, also for gate trials that systemd starts and for detached runs. The owner comes from the run's owner record, not only the process tree. The user, via builds: "i do not see the showrunner or the unit on these targeted test runs so i have no idea what they are for".

**Verification:** `python3 -m unittest scripts.build_queue.test_memory_feed scripts.build_queue.test_queue_view scripts.build_queue.test_queue_card`, then `targeted_test.py` for the unit, and basedpyright on each changed file with zero findings. The director checks the page in a browser after the deploy, with a Mac step running.

### Phase 44m — Mac steps sent from natedev show as Mac rows  · status: todo
**Depends on:** 44 (placement records each Mac step and its execution host).

Split from 44g on 2026-11-12. Current builds has no source for these rows: `verify.sh` sends a Mac test through `try_mac_test`, and `copy_watch` starts speculative copies, with no record the feed can read.

1. Each running step whose execution host is the Mac shows as a Mac row with kind "sent from natedev", its unit, and a done / total bar, in the feed, the card and the view. A speculative copy shows the same way.
2. The rows take no natedev memory.
3. An old or absent Mac reading keeps these rows.
4. Port from the saved 44g tree `f86053a80` (the parts imp41 left out: the `verify.sh` placement log, `copy_watch` placement constraints and `SpeculativeCopy`, the placed-Mac fields and their tests).

### Phase 44s — The AWS row shows its stage, region and zone  · status: todo

User ask, relayed by aws 2026-11-12: show region and zone on the AWS row, with a smaller row font.
aws landed `CloudProgress.stage`, `.region` and `.zone` (868eb2a14, all `str`, default `''`).

1. The page and terminal AWS rows show the live stage text when it is not empty.
2. The AWS row shows the region and the zone after launch, for example `eu-south-2, zone a`. An empty value shows nothing.
3. The AWS row detail text uses the smaller row font of the owner line.
4. Old progress files without the fields render as today.

### Phase 44k — A hana or cargo-liner step waits for a busy Mac instead of running on natedev  · status: todo
**Depends on:** 44. Starts after the 14:00 deploy on 2026-11-12. ETA 17:00 EST.

builds gave this to verify-unit on 2026-11-12 as urgent. After Phase 44 the queue places every step. A busy Mac (a live or cleanup claim in an overlapping scope) is `TemporaryExclusion("busy")`, and `_placement_reply` then places the step on natedev when natedev is possible. verify-unit measured 143 hana runs that fell back to natedev since 11-10 (43.5 h). The policy lives in queue placement, so it is a queue phase. verify-unit sends the measured case and the wanted rule, and changes `verify.sh` only if the wake needs it.

Goal: when the Mac is busy with another live run of the same repository, a hana or cargo-liner step waits for the Mac. It wakes when the holder's claim or copy lease is released, not after the 120 s mirror-busy backoff. The Work Order follows at phase start.

**Measured (verify-unit, buildlog since 2026-11-10):** hana: 143 busy fallbacks to natedev, 43.5 h natedev wall time (median 250 s, p90 3733 s), against 178 Mac runs (19.8 h). cargo-liner: 89 busy fallbacks (2.7 h), against 157 Mac runs (7.7 h). The Mac was in use about 27.5 h of about 60 h.

**Rule (verify-unit, with the resources conditions):**
1. Scope: a step whose repository the Mac can run (a Mac mirror and Mac-eligible packages: hana, cargo-liner). Linux-only packages keep their Linux placement.
2. When the only Mac exclusion is busy (another holder's live or cleanup claim in an overlapping scope, or a held copy lease), hold the step for the Mac. Do not place it on natedev.
3. Wake on the release of that claim or lease, not on a timer. The 120 s mirror-busy backoff (`mirror_busy_backoff_s`, `offload.py` `write_mirror_busy`) must not delay a start after the release.
4. No fixed time cap. The wait ends when the holder releases or the Mac claim expires (`claim_idle_minutes`, dead-holder release).
5. The wait obeys a natedev build hold.
6. Natedev placement stays for every other reason: unreachable, a user Mac block, disk, an unreachable backoff, state errors, `--local` and a pinned target.
7. A step never waits on a claim or lease that its own session holds.
8. The call row records the wait (target mac, wait seconds in an existing field).

**Proof:** a row that fails without the change (busy Mac, natedev possible: the step waits, then runs on the Mac at release, with no local run), and rows for each fallback that stays. Diffs in `mac_test.py`, `offload.py` and `build_targets.py` go to resources before the checkpoint. verify-unit counts the after value from the buildlog after the deploy.

### Phase 44j — A test run with no history for its filters keeps only what its tests use  · status: todo
**Depends on:** 44c, because Phase 44 also edits `scripts/lint/memory_admit.py`. Runs at the same time as 44g, in its own worktree, after the 44 + 44c deploy (builds, 2026-11-12, item 78). It shares only `admission_records.py` with 44g, which only reads it.

Cause, from the saved records of pid 2940313 (reservation `3629442-94520057-1794433606678362112`): a hana nextest step in tool-based-ui-trunk, 7 filters, 1 test thread. It kept its 11.47 GiB build need for its 18-minute test phase (16:55–17:13 EST), and its tests used at most 1.03 GiB. Room was about 1.2 GiB, so 13 waiting Hana steps did not fit.
1. `test_phase_need_from_rows` (`memory_admit.py:784`) finds history only for the exact filter set (line 803). With none, it returns `NoTestPhaseNeed("no_history")`.
2. `reservation_need` (line 1711) then keeps `row["need"]` for the whole step. `test_phase_margin` (`config/build_admission.conf:84`) multiplies only a history need (`test_phase_charge`, line 1706), so it does not apply.
3. On 2026-11-11, 158 of 352 filtered nextest steps had no test-phase history. 95 of them had a measured test phase and kept about 14.4 GiB-hours above 1.2 × their measured test peak.

Design (builds, 2026-11-11; revised after the Astra blind design review, 2026-11-12, `/tmp/claude/astra/20261112T080039Z-3011320/review.md`):
1. **Eligibility.** A filtered nextest step whose history search succeeded and found no row for its exact filter set. Split today's `NoTestPhaseNeed("no_history")` into a successful search with no match and unavailable evidence. Unavailable history, a read error, a missing repository identity, an invalid width and an unreadable memory reading keep the build need (`memory_admit.py:793`, `:841`).
2. **Wait.** Elapsed time from `ObservedTestPhase.started_at`. The observation must match the current reservation; `observed_test_phase` (`memory_admit.py:850`) keeps the record identity for that check.
3. **Charge.** Fallback = max(test-phase peak so far × `test_phase_margin`, current readable use × `test_phase_margin`, floor). Recompute it each pass. It rises at once with the peak and may rise above the original build need. `row["need"]` stays unchanged; the fallback feeds the existing `memory_room` keep (`kept = max(0, charge − current use)`, `memory_admit.py:612`). Head protection and start spacing stay.
4. **One path.** `reservation_need` (`memory_admit.py:1713`) applies the fallback through the progress path too: a progress profile never lowers the charge below the floor or the observed peak with margin (`progress_memory`, `:1830`).
5. **Observation loss.** The sampler writes the phase file only when its peak rises (`scripts/lint/invoke.sh:258`), so a file age does not prove a live sampler. A lost observation, a width change or a service restart returns the step to the build need until a valid observation returns.
6. **Settings.** The floor and the wait are new keys in `config/build_admission.conf`, read at use, set with /tune. Trial candidates: floor 2 GiB, wait 60 s. Before the keys get values, a replay over pinned records (named source files, cutoff and zone) compares candidates by peak growth after activation and by concurrent memory deficit. The 14.4 GiB-hours figure is an upper bound, not a wait-time saving.
7. **Record.** The captured explanation names the fallback reason, activation time, floor, margin and charge. A fallback is not written as history: `TestPhaseStarted` (`admission_records.py:354`) requires `TestPhaseNeed`, so a new record variant needs codec and reader tests. This adds `admission_records.py` to 44j; 44g only reads `execution_host` from it, so the second phase to land merges the first.
8. **Limitation.** The wait and the rising charge do not bound later growth. Memory a test takes after activation may already be promised to another step. Pressure checks stop new starts only. Another filtered run with no history reached 6.63 GiB (`~/.local/state/buildlog/admission/anon_peaks.jsonl:52932`), more than a 2 GiB charge plus the 3 GiB safety.
9. Other history shapes do not fit: same repo and package at the nearest width gives 10.6 GiB here, and width scaling gives 0.66 GiB, below the real 1.03 GiB.

Acceptance (rows in the existing `memory_admit` and admission-service tests; one service test covers the progress wrapper):

| Case | Result |
| --- | --- |
| Before, at and after the wait | The fallback starts at the stated boundary. |
| Missing observation, invalid width, unavailable history | The build need stays. |
| Peak rises after activation, then current use falls | The charge rises at once and keeps the peak. |
| Current use above the saved peak | The charge includes current use. |
| A progress profile appears or goes away | Neither path goes below the floor or the peak with margin. |
| Head and a younger waiter compete | Freed room follows head protection and spacing. |
| The charge rises while builds run | Kept memory rises; ordinary starts stop when room is short. |
| Config change, width change, service restart | The shown charge matches the stated rule. |

The Work Order follows at phase start.

### Phase 44d — The queue holder signal test passes under a loaded gate  · status: todo
**Depends on:** 44c. Ordered by builds 2026-11-10 (not urgent). In a full AWS gate run (hierarchies 62efaf), `scripts/build_queue/test_queue_place.py` `test_holder_signals_release_and_restore_handlers` (SIGINT case) failed its fixed 8 s wait, "signal cleanup did not finish" (line 1019), then passed alone. The wait must not depend on load. The forked holder already arms `signal.alarm(TEST_HANG_GUARD_SECS)` (line 997), so the parent can wait on the holder's pidfd with no fixed limit of its own and keep every assertion. The Work Order follows at phase start.

### Phase 44a — The queue page shows the AWS costs that matter most  · status: todo
**Depends on:** the aws-unit change that removes the `aws` build target and makes `aws_section` stop needing it: commit `1b1fa1842`, on builds since 2026-11-10. Starts after Phase 44. Ordered by builds 2026-11-10 from the user's request: "tell queue i want it to build an aws costs pane into its html page and surface the costs that are most important to me". The Work Order follows at phase start; ask aws-unit about any ledger field.

The pane leads with these, in this order:
1. Yesterday's real AWS charge, and today so far against the daily cap (`daily_limit_usd` in `config/aws_target.conf`).
2. Month to date against the monthly cap.
3. Where the spend goes: full merge gates against targeted tests, by unit, and the cost of one gate.
4. Fixed costs that run with no gates, such as the gate image snapshot and other kept gate resources.

Rules (public repository):
1. Show no AWS account id, instance id, subnet, volume, snapshot or ARN.
2. Show no cost-ledger `recipient` field and no notification email.
3. Make no AWS call from the page. Read only the saved ledger and bill records (`scripts/buildlog/report.py` `aws_section` reads them now).

The "unowned processes" rename moved to Phase 44e on 2026-11-11, because 44e rewrites that tooltip. The pane's tooltips use the 44e shape.

**Workers:** 1 writer + 1 tester.
- Writer: the saved cost projection, `memory_feed.py` and `memory_live.html`.
- Tester: cost fixtures, public-data exclusions and missing-data behavior.

### Phase 44b — One hold command drains natedev for a service deploy  · status: done

#### As-built

- `build_hold.py hold --drain [--for <purpose>] [--keep <pid>]` sets the hold, then stops each live verify.sh, merge_gate.py and targeted_test.py run that waits: no cargo child, no admission reservation, no live Mac or AWS claim. `hold` without `--drain` is unchanged.
- A stop checks the process start time from discovery. For a detached run it checks the `detached_runs.py supervise` argv structure (script name, `supervise`, record path resolved against the supervisor's cwd) and signals the run group through `send_process_group`. A changed identity is skipped and named.
- `detached_runs.stop_with_reason` writes the stop reason, rechecks identity, and signals or removes the reason in one `record_lock` section. The worker's result reads "stopped for <purpose>; run again after the release" only when the signal was sent.
- The drain records each stopped run and registers its owner (`audit_owner`) before TERM, with `stop_status` "stopped" or "not_stopped". Release sends each stopped owner one notice; a second release sends none. `step_record.consume_stopped_build` counts only "stopped".
- After the stops it waits for working runs, names each with owner and age, prints a heartbeat each 60 s, and ends with `drained`, exit 0. INT ends the wait and keeps the hold. Only the outermost gate in each process family counts (verify.sh forks subshells).
- `build_targets.caller_waits_on_hold(holds, directory)` is the one start-check question. `hold_waited_on` skips the caller's own hold and a hold whose keep list holds the caller or an ancestor; it reads the caller lineage once per check. A kept caller and its children pass every start check; status and report readers show every hold in force.

**Files:**
- `scripts/build_hold/build_hold.py`, `hold_enforcer.py`, `hold_applies.py` — drain, stop record, start check
- `scripts/agents/detached_runs.py` — stop reason under `record_lock`
- `scripts/build_targets/build_targets.py` — `hold_waited_on`, `caller_waits_on_hold`
- `scripts/direct/step_record.py` — stopped-build consumption
- `scripts/production/merge_gate.py:472` — one-line start check
- `commands/build/hold.md` — drain usage
- Tests: `test_build_hold.py`, `test_hold_enforcer.py`, `test_hold_applies.py`, `test_detached_runs.py`, `test_build_targets.py`, `test_step_record.py`

**Binds later work:** a deploy runs `build_hold.py hold --drain --for '<purpose>'` before the stop-convert and continues only on `drained`. The drain covers verify.sh, merge_gate.py and targeted_test.py only. `hold_waited_on` reads the keep lineage from `os.getpid()`, so a caller in another process must pass the requester's lineage. `holds_only_own`, `caller_holds_only_own` and `holds_apply_to_caller` are gone.

**Gotchas:** never take `record_lock` twice in one process: `flock` on a second open file blocks the same process. The deploy stop script keeps its own refusal as the last guard.

### Phase 45 — Merge gates take their route from the queue  · status: todo
**Depends on:** 44 and aws-unit's repair of the uncertain cloud launch. Checkpoint 3 of section 8 in `docs/plans/builds-queue-placement.md`. `merge_gate --tests-only` takes the same queue route as a full gate and keeps its exit codes. Tell the sdlc showrunner before this phase edits `merge_gate.py`, because natest phase 119 changed `run_gate_suites`. Phase 119 is at 14581684f on builds-natest: merge builds first, keep its selection lines before the `cloud.route` call, and keep its empty-selection result. Tell the sdlc showrunner when this phase is on main. The Work Order follows at phase start.

**Constraints from 10g (architect review of 10g, 2026-11-10).** aws-unit's repair is commit `0ab15ffc4` (on builds and main): it keeps `GateLaunchPending` after an uncertain launch and adds reconciliation tests. Build on it and keep its tests through the scaler cutover. Reuse Phase 44's completion and retry contract and its claim ownership and deploy constraints (Phase 44 items 3 to 5).

**Constraints from 44b (architect review of 44b, 2026-11-10).**
1. The hold check at `merge_gate.py:472` is `build_targets.caller_waits_on_hold(holds, directory)`; `caller_holds_only_own` is gone. Keep this check when gate routing moves to the queue.
2. Keep the Phase 44c rule: a hold decision uses the checked requesting process's lineage, never the service's.
3. The deploy runs `build_hold.py hold --drain --for '<purpose>'` before the stop-convert, as Phase 44 deploy step 3 does.

**Workers:** 2 writers.
- Queue writer: placement types, protocol, service policy, and their tests.
- Adapter writer: `merge_gate.py`, `cloud_gate.py`, and their route and lifecycle tests.

### Phase 46 — Every view names the machine a step runs on, and the old route code is gone  · status: todo
**Depends on:** 45. Checkpoint 4 of section 8 in `docs/plans/builds-queue-placement.md`. The Work Order follows at phase start. It switches the remaining readers first and deletes the old selectors only after their last caller switches. Phase 44 item 4 (claim ownership) applies.

**Constraints from 44b.** The hold-policy helpers in `build_targets.py` (`hold_waited_on`, `caller_waits_on_hold`) are not host selectors: keep them. Status and report readers keep the complete list of holds in force. The deploy runs `build_hold.py hold --drain --for '<purpose>'` before the stop-convert.

**Workers:** 1 writer + 1 tester.
- Writer: the remaining readers, and deletion of the old selectors after their last caller switches.
- Tester: reader coverage, Machine labels, restart behavior, and lost-authorization cases.

### Phase 10p — The snapshot client accepts records from an older or a newer admission service  · status: done

#### As-built

- The snapshot record parser in `scripts/lint/memory_admit.py` ignores unknown fields and fills a missing field from its dataclass default, at every nesting level. A missing field with no default and a wrong tag are still refused. A step that waits across a service restart (older client code) reads the new records.
- The four single-field shims are gone. The `fallbacks` to `diagnostics` conversion stays because it renames a field.

**Files:** `scripts/lint/memory_admit.py`; `scripts/lint/test_memory_admit.py`, `scripts/build_queue/test_admission_service.py`, `test_build_queue.py`, `test_queue_view.py` — added, removed and missing field regressions.

**Ruled out:** A version field on each record; per-field shims.

### Phase 10i — A local cargo step takes its token first, then asks for memory once  · status: done
**Why.** Builds showrunner, 2026-11-03: fastest's clippy was admitted at 20:11 EST, gave its grant back to wait 24 min for the cargo token, then waited 5 min for memory again. Cause: `verify.sh` gives back the memory grant before a busy-token wait (the `TokenWaiting` end), then asks again. Showrunner ruling: queue owns the whole change as one phase.

**Outline:**
1. A local cargo step takes its board token first. Then it makes one memory request and holds both until it ends.
2. A step with no token makes no memory request, so no idle grant blocks the token holder.
3. Every local cargo path uses the same order, the steps after Mac routing included. Nested work reuses the held token.
4. Cancel, failed admission and Mac departure give the token back.
5. Files: `scripts/direct/verify.sh` 2833-2850, `scripts/lint/memory_gate.sh`, `test_verify_token_wait.py`, `test_verify_token_reservation.py`, `test_verify_token_memory_history.py`. verify-unit holds its edits there until queue merges.
6. Evidence and proposed tests: `token_wait_findings.md` in the 10f session folder.
7. Separate defect, sent to verify-unit: the call's own cargo token stayed held for 1 h after its sweep. verify Phase 36 fixes it; 10i builds on Phase 36 after it lands on builds.
8. Design: `design.md` in the 10i session folder, with the director rulings at its end. Showrunner approved it 2026-11-03.
9. **Removal, temporary path (showrunner condition, 2026-11-03).** The service keeps serving main's old two-pass shell only until 10i reaches main. **Trigger:** the first queue checkpoint after 10i is on origin/main. That checkpoint deletes `admission_client.token_wait` and the `token-wait` command, `TokenWaitRequest` and its codec and fixture row, the service `TokenWaiting` end and handler, `AdmitRequest.token_back_at` and the token-gap writer, and their tests. After it, one way remains. **Status (2026-11-05):** the behavior is done (`verify.sh:2968` takes the token before memory admission). This removal is still owed: origin/main does not yet hold `e539c2906`.
10. **True wait cause (cargo-liner defect, 2026-11-03). Done (architect review of 10q, 2026-11-05):** `verify.sh:2692` reads the holder file with no newline, and `verify.sh:2698` names this call as the holder. No work remains; the text below is the record. `token_wait_note` in `scripts/direct/verify.sh` reads `locks/cargo.d/holder` with `read -r ... || holder=""`. board.sh writes that file with no newline, so `read` fails and the holder is cleared; every token wait then says "it came free just now". Run 20261103T220514-274283-ecf4 (handler-unit) printed that for 35 min while its own call held the token from 22:12:06 to 22:50:14. Read the whole file, name the holder agent and pid, and say "this call holds it" when `holder_pid` is the call. A memory wait prints the memory line, never a token line. Test: a holder file with no newline names its holder.

### Phase 11 — Every later step of one verify.sh run keeps the queue place of the run's first step  · status: done

#### As-built

- Linux `verify.sh` exports `BUILDLOG_RUN=<pid>:<start_time>` after detachment through `step_record.py start <pid>`. Nested calls retain it. Darwin reads no `/proc` and uses `NoRun`.
- `AdmitRequest.run` uses `RunIdentity(pid: int, start_time: str) | NoRun`, with shared `NO_RUN`. `BUILDLOG_RUN` is parsed once; missing or malformed values use `NoRun`. Older requests without `run` also use `NoRun`. `RunPlace(run: RunIdentity, since: float, first_owner: Owner)` persists through the generic codec in `state.json`. `read_state` defaults missing `run_places` without changing existing state.
- Snapshot copies use `order_since=min(run.since, step.since)`; `waiter_order` sorts `(priority_rank, order_since, pid)`. Durable rows retain step `since` for reconnects, token reacquisition, display age, and AWS eligibility. Process watches remain between steps. The service restores run places and opens watches after restart. Each pass drops ended runs; waiting steps then use their own arrival order. Owners, episodes, and reservations remain per step. Runs hold no memory or CPU token between steps.

**Files:**
- `scripts/build_queue/admission_protocol.py`, `scripts/build_queue/admission_client.py`, `scripts/build_queue/admission_service.py` — run types, parsing, persistence, watches, and snapshot order.
- `scripts/direct/verify.sh`, `scripts/direct/step_record.py` — exported identity and the shared process-start reader; `scripts/lint/memory_admit.py` — snapshot ordering and durable-row filtering.
- `scripts/build_queue/admission_test_support.py`, `scripts/build_queue/test_admission_service.py`, `scripts/build_queue/test_admission_client.py` — shared fixture and run-lifetime regressions.
- `scripts/direct/test_verify_run_place.py`, `scripts/direct/test_step_record.py` — nested, detached, Darwin, and start-command regressions; `docs/as-built/build-memory-admission.md` — run-place contract.
- `scripts/direct/verify_examples_test_support.py` — loop cleanup precedes temporary-folder removal.

**Gotchas:** The PID and start-time string identify a run together. Without `--state-dir`, the service namespace defaults to the buildlog admission folder, including under systemd.
**Ruled out:** Merging `RunIdentity` with `Owner`; removing the client's Darwin guard; storing resolved run order in durable waiter rows.

### Phase 7 — A promote never refuses a step that already waits for memory  · status: done

#### As-built

- `buildlog_wait_for_memory` in `scripts/lint/memory_gate.sh` holds no closed list of `source` values. An unknown source prints with the fallback description of `buildlog_memory_line_inner`; it never refuses.
- After the first wait line (`announced != 0`), a failed checker call prints `memory admission check failed (<last line of the checker output, or "checker returned an error">); trying again, the step keeps its place` to stderr, sleeps the poll interval and polls again. The step is refused with status 2 only when failed calls run for `BUILDLOG_MEM_CHECK_RETRY_S` seconds (default 60) with no success. A successful call clears the failure run. Before the first wait line, a failed call refuses at once.
- `buildlog_memory_check_launch_limit` is the one launch wait limit check (an app launch with no argv; `BUILDLOG_MEM_WAIT_LIMIT_S`, default 900). It runs on the normal path and on the retry path, so failed polls do not extend a launch's limit.
- The caller pid stays the same across polls, so the step's `.waiting` row, its `since` and its `priority_since` stay. All other refusals (invalid output, invalid answer, invalid reading, no reservation, unreadable memory) do not change.

**Files:**
- `scripts/lint/memory_gate.sh` — no source check; the retry while a step waits; `buildlog_memory_check_launch_limit`
- `scripts/lint/test_memory_gate_wait.py` — six regressions that run the real loop: unknown source and one failed poll keep the real waiting row; continued failures refuse with status 2; failed polls do not extend a launch limit; a success resets the retry deadline; an empty checker error uses the fallback text; a failure before the first wait line refuses at once
- `scripts/lint/memory_test_support.py` — `gate_isolation_environment(root)`, which keeps a gate subprocess away from the caller's holds and session records
- `scripts/lint/test_memory_admit.py` — `test_shell_accepts_shape_source` keeps only the `shape` case
- `docs/as-built/build-memory-admission.md` — the fail-closed paragraph states the retry and that the shell does not check `source`
- `docs/plans/builds-production.md` — queue-unit's Units row owns `scripts/lint/memory_gate.sh`

**Binds later work:** Phase 10 ("Every build, view and command uses the queue service…") replaces this wait loop with `admission_client.py`. These wait rules move with the loop, and `test_memory_gate_wait.py` moves to the client with its assertions.

**Gotchas:** a running bash keeps the `memory_gate.sh` functions it read at its start, so a step that waits across a promote runs the old loop against the new checker. For this reason no shell holds a closed list of checker values.

**Ruled out:** a version-matching rule between the shell and the checker; a test that loads a previous release.

### Phase 25 — A queue view answers inside its limit  · status: done

#### As-built

- One `OperatingSystemReader` thread reads the process table, the unowned slices, the scope activity files and the memory readings at the pass cadence (`waiting_pass_seconds` while steps wait, `running_pass_seconds` else). It returns raw evidence only and wakes the loop through an eventfd. The loop applies the current configuration and reservation records (need, width, peak hold) at each pass, so a pending configuration or a width change takes effect at once. The loop thread still owns all service state and makes no `/proc` or cgroup read.
- `update()` wakes the reader when the cadence changes from running to waiting. A read that raises does not end the reader; the loop raises it as the pass did before.
- Each reading carries `started_monotonic`. An Admit, Launch or estimate request for an owner whose pid and start time the reading lacks waits as a `DeferredOwnerRequest` and asks for an early read. It runs on the first reading that started after it arrived. A finished, refused or closed connection drops its deferred requests.
- Each reply tree is encoded once (`snapshot_codec.py` returns scalars directly and caches dataclass field names). View and Explain bytes are encoded on the first request after a pass and kept until the next pass ends. Mine keeps its full content. `persist` encodes only the changed records; file bytes, fsync order and wire bytes do not change.
- `query_timeout_seconds` stays 0.5; no configuration key changed.
- Replay with no profiler (`measure_25.py --reuse`, 20 samples per group): a pass takes 16 ms median, 33 ms worst (372 ms median before). A View queued behind a pass, a Release, a TokenWait and a pending configuration waits 255 ms median, 406 ms worst; most of it is disk sync. The live probe after deploy is the proof.

**Files:** `scripts/build_queue/admission_service.py`, `admission_protocol.py`, `snapshot_codec.py`; `test_admission_service.py`, `test_admission_protocol.py`; `docs/as-built/build-memory-admission.md`.

**Binds later work:** a durable write still syncs the state file on the loop thread, so a View waits behind each Release and TokenWait sync. The next item "Durable writes leave the admission service loop" waits on the live probe.

**Ruled out:** a larger `query_timeout_seconds`; a lock on service state; a second state-owning thread.
