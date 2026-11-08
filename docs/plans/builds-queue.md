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

- There is no set budget. Room = free memory now − `safety_gib` (3) − the keep-aside of every running run − the CI keep-aside. A run starts when its need fits the room, pressure is calm and it is its turn. 10t's budget controller (`budget.py`, `budget_replay.py`) and 10y's backfill and strict windows are deleted; neither landed alone.
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

- One process holds one `store` module object, whichever name imports it first. The buildlog and lint modules import the bare `store`; `head_facts.py`, `admission_service.py`, `admission_client.py` and `merge_gate.py` import `scripts.buildlog.store`.
- `scripts/buildlog/store.py` registers itself as `sys.modules["store"]` when it loads. It registers `scripts.buildlog.store` and sets the package attribute `store` only when `scripts.buildlog` is already loaded. It searches for and imports nothing at load.
- `scripts/buildlog/__init__.py` makes `scripts.buildlog` a regular package. At load it adopts a bare `store` module from its own directory: it registers it as `scripts.buildlog.store` and sets the package attribute. A `store` from another checkout is not adopted.

**Files:**
- `scripts/buildlog/store.py` — registers its module under both names.
- `scripts/buildlog/__init__.py` — adopts a bare-first store from the same directory.
- `scripts/buildlog/test_store_identity.py` — six fresh-interpreter cases: bare first, package first, `import sweep`, bare then dotted attribute, the index import order from a cwd outside the repository, and an unrelated raising `scripts/__init__.py`.

**Binds later work:** packaging or import changes in `scripts/buildlog` keep `__init__.py` and both import forms (`import store` and `from scripts.buildlog import store`).

**Gotchas:** a namespace package runs no code at load. A dotted child already in `sys.modules` makes `import scripts.buildlog.store` skip the parent's initializer, so store.py adds the dotted name only after the package exists. Deleting an alias from `sys.modules` and importing again makes a new module (a reload); no caller does that.

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

**Binds later work:** a module that names `session_send_argv` installs `forbid_real_sender` in `setUpModule`, because `scripts/message/test_session_send.py` requires it.

**Gotchas:** the installed `merge_gate.py` runs for every `--checkout` gate, so `suites:2` records keep arriving until this phase is installed. Stored `suites:2` records stay until removed.

**Ruled out:** a reader in main code that skips stored `suites:2` records.

### Phase 21 — Each moved admission name has one import path  · status: todo

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

### Phase 16 — The copy guard refuses only what makes a repository tree  · status: todo

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

### Phase 17 — copies.py list shows remote mirrors  · status: todo

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

### Phase 26 — A cloud gate holds a cloud slot and shows its modules done  · status: todo

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

### Phase 27 — The build memory page feed is a boot service  · status: todo

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

### Phase 10m — The build page is a repository tool and shows leftover-process ages  · status: todo

**Depends on:** 10z (the page reads the room evidence 10z publishes) and report-unit Phase 15 on builds (builds ruling, 2026-11-05). report Phase 15 builds one typed provider, `charts.page_data(days, n=3) -> PageData | IndexUnavailable`, and starts at once. report-unit waits for 10m only in its Phase 26.

1. **The ask (user, 2026-11-04, change reviews C959 and C969, through builds).** C959: "we are moving to the new build html page - we will use /build:queue for reference until we can retire it". C969: leftover-process ages on the queue board, "this should be the case on the new html page too".
2. **The page.** The Build Memory page (memory_live.html, fed by memory_feed.py) is today a prototype in the queue unit's session folder, served by review link h7uj5c. It moves into the repository with its feed and one command to open it. The command name needs the user's approval. New board features go on this page. report-unit's cost and chart views land on this page too, not on a second one.
3. **report-unit's sections (report-unit, 2026-11-04; C950 and C948).** The page gets two sections: the build history charts and a top-3 cost list. report-unit owns the data: one function in `scripts/buildlog`, `charts.page_data(days, n=3)`, returns typed data for both sections, no HTML, read in under 1 s for 7 days (builds ruling, 2026-11-05). The feed calls the function and the page draws its data. There is no JSON file between them.
4. **Leftover-process ages.** The page shows each leftover process with the same readable age as the queue board ("45 min", "19 h", "2 d"), from the same formatter.
5. **/build:queue.** It gets no new work. It stays for reference until the page covers it; 10n retires it.
6. **Wait states.** The page shows the three wait states the service records: unreadable pressure (with its path), start spacing, and head protection. 10z keeps the pressure text and `ReadingAlertTests`; 10h owns holder names.
7. **Proof.** The feed's tests pass, with feed and page tests for each of the three wait states. A pixel check of the page shows the cap, the room, the leftover ages and report-unit's sections. Each item returns as its own Change-Review row.

8. **Partial readings (architect review of 10q, 2026-11-05).** The feed reads each part of the page on its own. When one part is unavailable (the cap, pressure, a run's use), the page keeps the other parts and names the missing one. Use named availability states, not a bare `None` (today `memory_feed.py:109` stores missing pressure as `None`, and `MemoryPage.cap` uses `None`). 10s already keeps the rows with no cap line (`memory_feed.py:295`, `memory_live.html:354`); this item replaces the `None` fields. Acceptance tests: with the cap alone unavailable, the page keeps its rows, the room and the reason text, for both `CapUnavailable(runs=())` (an old record) and named unavailable runs.
9. **Sources.** The prototype is in `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/design/memory-view/` (`memory_live.html`, `memory_feed.py`, `test_memory_hover.py`). It moves to `scripts/build_page/` (page, feed and tests). The feed takes `--code-root`, reads `captured.ci_use`, `captured.cap_use` and `room_text(captured)`, and imports `snapshot_codec`; its hover shows phase, percent done, keep-aside and reason. `memory_feed.pre10x.py` is deleted at the 10x deploy and does not move. The feed makes one call to report Phase 15's `charts.page_data(days: int, n: int = 3) -> PageData | IndexUnavailable` (report e913e5fa2; types in `charts.py`; 0.42-0.48 s for 7 days on the live index). `PageData(charts: ChartSet, top: TopCosts)`. `TopCosts(start, end, recent_days, rows: tuple[TopCost, ...] | NoDataYet)`; `start` and `end` bound the recent window in the user zone. `TopCost(machine, cause, recent: ChartCost, window: WindowValue, action: CostAction)`; `WindowValue = WindowMatchesRecent | WindowDiffers(value: ChartCost)`; `CostAction(action, owner)`. The feed checks that one result: on `IndexUnavailable(reason)` the page shows the reason in place of both sections, and `NoDataYet` shows an empty top list with its window. `TOP_COST_RECENT_DAYS` greater than `days` raises `conf.Unusable`: the feed passes `days` of at least `TOP_COST_RECENT_DAYS` and shows a config error as the page reason. 10m starts only after report Phase 15 is on builds (it lands with report 24-25, after a user answer); builds says when. The item 7 pixel check uses their data.
10. **Prototype behavior to keep (2026-11-06).** The Running table shows a Machine column after Duration and before Unit, from `scripts.buildlog.store.host_name()` (page round 16, the user's ask). When one view read misses the client's 0.5 s limit, the feed writes the last good reading plus `read_error` and `error_at`, and the page keeps its data; only the first read of a feed with no good reading shows "The queue did not answer" (page round 17, the user's blank-page report).
11. **Browser proof (architect review of 18, 2026-11-06).** The browser proof today is `/tmp/claude/unit/2ac94b9b-8a9d-4a9b-b908-b4f87dd4d5d0/phases/adhoc/memory_page7/verify.mjs`: Machine column order, a first read with no answer, a kept reading, and the stale-reading messages. It moves with the page, and the tester owns it.
12. **Feed restart at install (architect review of 10s, 2026-11-05).** A running feed keeps the decoder it loaded, so a schema change breaks it until it restarts. The page command restarts the feed when the installed admission code changes. Test: a feed that started before a schema update reads the new record after the restart.

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

**Workers:** `impl` owns `scripts/build_page/` (the page and the feed); `test` owns `scripts/build_page/test_*.py` and the browser proof from item 11. report-unit owns `charts.page_data` in `scripts/buildlog` and its tests (report Phase 15).

### Phase 10n — /build:queue retires when the build page covers it  · status: todo

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

**Gotchas:** snapshot ancestry cannot hold a program family without a scope: a `setsid()` descendant orphaned between two samples escapes `ProgramTree`. A place that replaces the caller's signal handlers breaks the merge gate's stop (exit 143). Process-group signals in scripts and tests go through `scripts.lib.process_group.send_process_group`; `scripts/lib/test_process_group.py` checks every file.

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

### Phase 22 — The hana CI margin key goes after the runner activation  · status: todo

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

### Phase 23 — Detached Python runs have their own group and count in Other  · status: todo

**Depends on:** Phase 18 of the herdr worker-panes plan on GitHub builds. That phase renames `scripts/agents/mesh_runs.py` and its config. Source: user, 2026-11-06.

#### Work Order

**Goal:** A detached Python run stays outside the build queue and shows under Other on the memory page. A cargo or lint step inside a detached run still asks the queue and counts as a build.

**Spec:**

1. **User rulings.** "i think the workers detached python tests will almost never be that heavyh weight so it's good that they don't count against the build queue". "yes detached python tests should show under other along with the memory taken by all the agent sessions, browsers, editors, etc."
2. **The group.** The detached run starts in `builds-detached.slice`, a child of `builds.slice`, at the line that is `mesh_runs.py:647` today. The limits of `builds.slice` still hold it.
3. **Reader.** The scope reader in `memory_admit.py` (line 2648 today) treats the child group as part of the one builds total, and it makes no row for that group. A reserved cargo step inside it counts once, as a build.
4. **Page.** `memory_feed.py` makes no row for the group (line 419 today in the session prototype; `scripts/build_page/` after the build page moves). Its memory reads as Other.
5. **Trace rule.** Remove only the rule that shows a detached process group as a run because its processes trace to a live run. Keep the service's registered run identity (`admission_service.py:910`), the held-run rows (`queue_view.py:272`) and the card that a person selects by owner (`queue_card.py:217`).
6. **Proof.** Tests: a Python process in the group reads as Other and has no row; a reserved cargo descendant counts once; a cargo and a lint step inside a detached `verify.sh` ask the queue; a held run keeps its row.

**Files:**
- `scripts/agents/mesh_runs.py` — renamed by the herdr phase; use the new name.
- `scripts/lint/memory_admit.py`
- `scripts/lint/test_memory_admit.py`
- `scripts/build_queue/test_queue_view.py`
- `docs/as-built/build-memory-admission.md`

**Workers:** `1 writer + 1 tester` — `impl` owns the launcher line, the reader and the feed; `test` owns their tests.

**Gate:** the merge gate, and `basedpyright` 0/0/0 on each changed file and each caller module.

**Change-Review:** Detached Python runs count in Other | On natedev, open the memory page while a worker's detached Python tests run | The runs show no row, and Other holds their memory.

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

### Phase 23 — Detached Python runs show under Other  · status: todo

**Depends on:** herdr-unit Phase 18, which renames `config/mesh_runs.conf` to `config/detached_runs.conf` (user-approved, 2026-11-06).

1. **The ask (user, 2026-11-06).** "yes detached python tests should show under other along with the memory taken by all the agent sessions, browsers, editors, etc." The user approved the group name `builds-detached.slice`.
2. **Outline.** Detached Python test runs start in `builds-detached.slice`, not `builds.slice`. They do not ask the queue (10v does not land). The memory page shows their memory in the Other group; `/build:queue` does not show them.
3. The Work Order follows at phase start, after herdr's Phase 18 is on builds.

### Phase 10f — The queue names every build folder it makes and deletes it when its work ends  · status: todo
**Depends on:** none (checkpoint A is on main; checkpoint B changes `scripts/lib/build_folders.py`, the `tmp_sweep` call and `scripts/mac_test/linux_runner.py`). 18c also changes `linux_runner.py`, and 10g changes the offload path. The second to land resolves; the director names the merge owner before the work runs in parallel (architect review of 18, 2026-11-06).
**Why.** The user, 2026-11-03, via the builds showrunner: "our build-queue is supposed to handle making sure we name things and delete them when they are completed". The naming map is a design call that the builds showrunner settles. The showrunner approved it on 2026-11-03 (map and rulings: the 10f design in the unit's phase folder), with one change: a remote mirror ends when its copies.py remote lease from verify P35 ends.

**Scope (showrunner limits, 2026-11-03).** 10f does not change the callers that verify P18 moves to copies.py (merge_gate, bump_gate, fast_track, waiting, the style runs, rust_release). Its sweep never touches copies.py standing checkouts. Two checkpoints:
1. A: `build_folder_path` is the one name rule; `build_folders.py run --owner O --name N -- CMD` makes a folder, runs CMD in it and deletes it when CMD and its descendants end; `scratch.target` uses the name rule; the scratch guard refuses a clone under a unit or session tmp root and names the command.
2. B: `build_folders.py sweep` removes a marked folder only when its owner is gone (pid, start time, boot id; a read error keeps it); `tmp_sweep` calls it; one writer makes each removal record below, also for `linux_runner` floor and trim.

**Progress (2026-11-04):** checkpoint A is on main, with the test fixes for a `CONF_DIR` that lacks `build_folders.conf` (`build_folders._settings` then reads the repository file) and config read at use in `tmp_sweep`, `style_sync` and `folder_audit`. Checkpoint B is open.

**Removal record (aws-unit ask via the builds showrunner, 2026-11-03):** each folder removal writes one record with: time, target, host, repository key, slot, folder path, bytes freed, free GiB before and after, and the reason: `floor`, `trim`, or `owner-ended` for a folder that the sweep removes after its owner is gone (architect review of 10q, 2026-11-05). Source today: `scripts/mac_test/linux_runner.py:775-788` removes or trims a slot build folder after each step and prints only `mac_cleanup=0|1|kept` (`offload.py:797` parses it); no record names the folder or the bytes freed. aws-unit's weekly report (cold steps after a floor removal, time lost) reads this record. Test: an owner-ended removal writes one record with reason `owner-ended`, and a standing copy is never removed.

**Workers (checkpoint B):** `1 writer + 1 tester` — `impl` owns `scripts/lib/build_folders.py` (sweep and the record writer), the `tmp_sweep` call and the `linux_runner.py` record call (aws-unit reads the record: name it in the notice); `test` owns `scripts/lib/test_build_folders.py`, the `tmp_sweep` tests and the `linux_runner` record tests. B keeps the standing-copy exclusion and the remote-lease ownership.

### Phase 10g — The queue decides where every step runs  · status: todo
**Depends on:** verify-unit's Phase 27 hotfix on builds (the Work Order follows it), 10z and 10x (it changes the admission request and the queue service).
**Why.** The user, 2026-11-03: "should verify be handling this or should the build queue be handling what goes where - why is verify involved?" Showrunner ruling (builds, 2026-11-03): the queue owns placement; one owner decides where every step runs. Today `verify.sh` picks the host once at start and the queue admits memory only on natedev; at 14:04-14:17 a disk decline sent two hana tests to natedev and nothing sent them back to the free Mac.

**Outline (the Work Order follows at phase start, after verify's Phase 27 hotfix lands):**
1. A step's admission request carries its eligible hosts (natedev, the Mac, SSH slots) and each host's exclusion reason.
2. The queue service places each step on the first host that is free for it, and decides again on every pass while the step waits (Mac free, Mac disk over the floor, slot free).
3. `verify.sh` and the offload path run the step where the queue says; they make no host choice.
4. `build_targets.py` eligibility, slot reserve and the spending-guard exclusion move behind the queue decision.
5. Owners: queue-unit switches every caller; verify-unit owns `verify.sh` routing and offload today, aws-unit owns target selection; the edits are agreed with both at phase start.
6. Runs beside 10f where the file sets are disjoint.
7. **Slots per host (architect review, 2026-11-05).** A local 10z slot cannot stand for memory kept on the Mac or an SSH target. Define placement, reservation and departure states per host. Tests: a move to another host releases the local claim, and a local claim is never reused on a remote host.
8. **Machine column (Phase 24).** `/build:queue` (`queue_view.py`, `queue_card.py`) and the memory page feed read one `scripts.buildlog.store.host_name()` for every running row. When a step runs on another host, the service reply carries that host on its row, and both views show it in the Machine column. Tests: a running row placed on the Mac shows the Mac's name in the view, the card and the page.
9. **Host-scoped run identity (architect review of 18, 2026-11-06).** Two hosts can give the same pid and start time; `running_builds()` groups runs by `run_owner(pid, start_time)` today. One named run identity carries the host through placement, sampling, grouping and release. Test: a mixed-host fixture with the same pid and start time on two hosts keeps two runs, with separate accounting and the right Machine labels.
10. **Workers:** `impl` and `test` for the queue; adapter owners verify-unit (verify.sh routing and offload), aws-unit (target selection) and sdlc (lifecycle callers), agreed at phase start.

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
