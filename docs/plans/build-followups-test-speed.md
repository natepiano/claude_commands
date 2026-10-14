# Test speed: the merge gate's ~/.claude test list runs in minutes, not thirty

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The 22 Python suites the showrunner runs before every merge stop reaching live state, stop failing for load, run in parallel from one committed gate command, lose their named waits and their trivial tests, and start fewer processes.

> **As-built disposition: create** — a new as-built for the merge gate and the test-speed rules; amend the as-built docs of any script whose waits change.

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-13, EDT, the user via the showrunner (natedev): "I think 30 minutes is absurd. Can you give this to a unit director to analyze and fix?"

2026-10-13, the user via the showrunner: "I often find agents create trivial tests. If we have such, we should eliminate them also"

The showrunner's asks, 2026-10-13/14: analysis first (per-test times, sleeps, timeouts, subprocess and git or tmux launches, repeated setup; numbers, a quiet and a loaded baseline). Fix the cause in the tests and, where the code under test waits, in that code. No skipped tests, no weaker assertions, no "slow" tier the gate leaves out. The gate becomes a committed command in `scripts/production/` taking a branch, a checkpoint and its previous checkpoint; it runs the suites in parallel within a stated peak memory, prints each red suite's failures and exits non-zero on red; state peak GB with the time saving. A wall-clock threshold on a shared machine fails for load, not for a defect: make such a test count the work or measure against a calibrated baseline, and sweep the suites for others (shutdown's `test_six_tmux_launches_finish_in_under_one_second` failed once at load 34–50 and passed alone). Other units (showrunner-fixer, herdr-trial, herdr-panes, two-accounts) edit these files tonight: merge origin often, keep each checkpoint small.

## Analysis (measured 2026-10-14 00:00–00:30 EDT, natedev, 32 cores, 1-minute load 19–61 throughout: every number is under load)

| Suite | Tests | Wall s | CPU s | Peak RSS |
|---|--:|--:|--:|--:|
| production | 841 | 307 | 242 | 394 MB |
| delegate | 629 | 452 | 308 | 135 MB |
| mac_test | 176 | 98 | 65 | 110 MB |
| usage | 256 | 138 | 66 | 120 MB |
| agents | 303 | 80 | 5.6 | 66 MB |
| buildlog | 462 | 75 | 38 | 135 MB |

The showrunner's own sequential run measured about 1,830 s for all 22 suites. The cost is serial process launches, not setup: production runs 26,112 execs (4,089 Python, 14,815 git), delegate 21,924 (4,822 Python); no `setUpClass` cost is measurable.

- **production (307 s):** about 205 s running scripts as `python3 <script>.py` (`dailies_input` about 380 ms a launch, 152 ms of it import; `add_unit` about 440 ms); the fake tmux, gh and sessions tools are Python (1,608 tmux launches). About 74 s of git fixtures built in `setUp` (`merge_checkpoint` tests alone make 5,300 git calls). 20.6 s from `test_stall_watch.py:689` (`delay=20`, sleeping in the fake send at `:132`). 17.7 s of real Codex (below). 2 s from `add_showrunner.py:45` `POLL_S = 2.0`.
- **delegate (452 s):** about 200 s of `verify.sh`, `review.sh` and `implement.sh` runs, each starting about 19 Python helpers through `lib/py`, whose interpreter cache misses on every call because tests point `HOME` at a temp dir (367 extra launches). 88 s of 750 `progress_history.py` launches. About 40 s of fsync (`progress_history.py:492`, `review_pool.py:225`, `findings.py:369`); `TMPDIR=/dev/shm` cut the suite from 452 s to 413 s. 30 s from `review.sh:797` `sleep 5`. 13 s from `implement.sh:509/519` `sleep 1`. About 48 s of test polls and stub sleeps (`test_verify_mac_offload.py:548`, `test_verify_token_wait.py:69`, `test_verify_hold_wait.py:173`). 15 s of a timeout waited out (`test_verify_mac_offload.py:1253`).
- **mac_test (98 s):** 20 s of 10 s ssh timeouts waited out (`offload.py:1130`, not overridable); the rest is CLI launches.
- **usage (138 s):** 35 s of sqlite fsync (6,248 in `collect.py`).
- **agents (80 s, 5.6 s CPU):** 15 s of sleeps (`codex_mesh.py:3599` `sleep(0.2)` gives 10 s; `:3248`); 19 s of 1 s frame deadlines (`RESIDENT_POLL_SECS`, `codex_mesh.py:191`); about 15–25 s of stub-server shutdown (accept loop with a 0.1 s timeout that closing the socket does not wake, `test_codex_mesh.py:138,145-151`, 243 stub servers).
- **buildlog (75 s):** about 25 s of in-process synthetic-data and speed tests (`test_width_trial`, the two "under two seconds" tests).

**Parallel safety.** No test writes into the repo; sockets use private temp dirs or ephemeral ports; real tmux runs only with a private `TMUX_TMPDIR`; `HOME` and state dirs are redirected. Exceptions: five quota tests in production's `test_waiting` start the real `codex app-server` on `~/.codex` and `~/.codex1`, making network calls and writing `~/.codex1/plugins/cache` (`test_waiting.py:438-446` → `quota_alert.py:893-896,124-131` → `agent_accounts.py:424`; `HOME` is redirected only for the child, `test_waiting.py:166`; the `codex_readings=` parameter at `quota_alert.py:883` already lets a caller supply readings). `delegate/test_end_session.py:22,43` writes a marker into the live `/tmp/claude/delegate/active/`. agents expects a fixed `127.0.0.1:55555` to be refused. Two tests fail under load: delegate's signal test (2 of 3 runs) and agents' `test_refused_seat_continues_before_held_messages_in_order`.

**Memory.** All 22 suites at once: about 2.0–2.5 GB; one process per test module: about 3 GB; 36 GB available.

**Reachable.** Suites in parallel as they are: about 7.5 min (delegate bounds it). One process per test module as they are: about 2 min (slowest module `delegate/test_verify_mac_offload.py` at 108 s; CPU floor about 900 CPU-seconds over 10–16 free cores, 1–1.5 min). After the named waits go: about 1.5 min per module in parallel.

## Trivial tests: the criteria

A test is trivial when it cannot fail for a real defect:
1. it asserts a constant, a literal or a default against itself;
2. it repeats the implementation line for line;
3. it tests the standard library, or a mock it configured itself;
4. it only checks that a function exists or a module imports;
5. it duplicates another test's inputs and assertion.

A test that pins a behaviour a user or another script depends on is not trivial, however short. Where unsure, keep it and list it.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report` (unit `build-report-unit`). Name both in every dispatch.
- **Project started:** 2026-10-14T04:34:42.080+00:00
- **Stack:** Python 3 standard library, bash and zsh scripts; basedpyright (0 errors, 0 warnings; no file-level ignores; no `Any`).
- **Layout:** `scripts/<suite>/` for the 22 suites: lib hooks message accounts whoami agents build_hold mac_test usage bevy_upgrade production delegate buildlog lint nightly_review build_targets shutdown feature_log model_study herdr notify statusline.
- **Test:** per suite, from the worktree root, `python3 -m unittest discover -s scripts/<suite> -p 'test_*.py' </dev/null`.
- **Lint:** `basedpyright <changed files>`: a pass ends `0 errors, 0 warnings, 0 notes` (it exits 3 because pyrightconfig names a `.venv` no checkout has).
- **Invariants:**
  - No test is skipped, no assertion weakened, no tier left out of the gate.
  - No test reads or writes live state: `~/.codex*`, `~/.local/state/`, `/tmp/claude/delegate/active/`, the live tmux server.
  - Never kill by pattern; never touch another session's processes.
  - Saved run output stays under a few GB and is deleted once read; 300 GiB stays free on `/`.
  - Other units edit these files tonight: merge `origin/build-followups` before each phase and keep each checkpoint small.

## Phases

### Phase 1 — No test reaches live state or fails for load  · status: done

**Depends on:** none

#### Work Order

**Goal:** every suite can run beside any other: no test touches a live home or shared path, and no test asserts wall-clock time.

**Spec:**
- `scripts/production/test_waiting.py` (the `test_quota_*` tests, from `:923` after tonight's merge): the quota tests pass `codex_readings=` (`scripts/whoami/quota_alert.py`) instead of starting the real `codex app-server`; assert the same outcomes from readings. No `~/.codex*` access remains (a test that fails if `agent_accounts` resolves a real home is welcome).
- `scripts/delegate/test_end_session.py:22,43`: the marker goes under a temp dir through the script's existing override (or one added in `end_session.sh`, defaulting to the live path).
- agents: the test expecting `127.0.0.1:55555` refused binds an ephemeral port, closes it, and expects that refused.
- The two load flakes (delegate's signal test; agents' `test_refused_seat_continues_before_held_messages_in_order`): find the race and fix it at its cause, in the test or in the code.
- `scripts/shutdown/test_*`: `test_six_tmux_launches_finish_in_under_one_second` counts the launches (and subprocesses) instead of timing them.
- Sweep all 22 suites for wall-clock thresholds (`assertLess(elapsed…`, "under N seconds", `time.monotonic()` deltas compared to a constant), buildlog's two "under two seconds" tests included; each becomes a count of the work, or a comparison against a baseline calibrated in the same run. List every one found and what replaced it.

**Files:**
- `scripts/production/test_waiting.py` — the five quota tests pass `codex_readings=`
- `scripts/delegate/test_end_session.py` — the marker goes under a temp dir
- `scripts/delegate/end_session.sh` — an override for the active-marker root, if none exists
- `scripts/delegate/test_verify_mac_offload.py` — `test_runner_receives_each_forwarded_signal_before_local_work` race fixed
- `scripts/agents/test_codex_mesh.py` — ephemeral refused port; `test_refused_seat_continues_before_held_messages_in_order` race fixed
- `scripts/shutdown/test_restart.py` — `test_six_tmux_launches_finish_in_under_one_second` counts launches
- `scripts/buildlog/test_mac_report.py` — `test_mac_report_cli_runs_under_two_seconds_at_the_log_size` (`:591`, `:641`) counts the work
- `scripts/buildlog/test_record.py` — `assertLess(duration, 0.8)` (`:274`) counts the work
- `scripts/` — any other test file the wall-clock sweep finds

**Seats:** 2 writers — split by suite
- `impl` — `scripts/production/`, `scripts/delegate/` and `scripts/shutdown/` files above
- `test` — opens as impl; `scripts/agents/`, `scripts/buildlog/` and the wall-clock sweep in every other suite

**Acceptance gate:** every touched suite passes; basedpyright clean on changed files; a grep shows no `~/.codex` or `/tmp/claude/delegate/active` reach left in tests; each fixed flake passes 5 runs in a row with all six measured suites running at once.

### Phase 2 — One committed gate command runs the suites in parallel  · status: todo

**Depends on:** Phase 1

#### Work Order

**Goal:** `scripts/production/merge_gate.py` replaces the showrunner's scratch gate; the full test list runs in about 2 minutes within a stated peak memory.

**Spec:** (the showrunner's steps, 2026-10-14, to read and replace `/tmp/claude-1000/-etc-nixos/96b016bf-87cf-4a19-af7a-77425852c2e2/scratchpad/gate_mergeonly.zsh` and the first 18 lines of `chain_next12.zsh` in that folder)
- Takes a merge branch, a unit branch, the checkpoint and its previous checkpoint (plus unit and phase for the subject). Refuse unless the merge-branch checkout is clean and HEAD equals `origin/<merge branch>`; the checkpoint is on `origin/<unit branch>`; the previous checkpoint is an ancestor of it.
- Merge `--no-ff` with the subject `Merge <unit> <phase> (<hash>) into <merge branch>` and a short body; on a conflict, abort and exit naming the files.
- basedpyright on the named dirs, then the suites; on red, reset to origin, print each red suite's failures, exit 1. On green, push the merge branch.
- `--promote`: fast-forward `~/.claude` main to the merge branch and push; refuse while a build-hold release is running; then fast-forward the Mac over ssh and read the printed `ff_rc=` (ssh's own exit status is always 0 there). Keep the scratch script's settings.json filter check.
- The test list comes from one place in the repo (a constant or file the command reads), not the caller.
- Suites run one process per test module, in parallel, bounded by a worker count derived from a stated peak-memory cap (measured: about 3 GB for all modules at once); `TMPDIR` on tmpfs for the run. Print per-suite wall time and the run's peak memory.
- The red-suite rerun rule stays the showrunner's, outside the command.

**Files:**
- `scripts/production/merge_gate.py` — the gate command and the one test list
- `scripts/production/test_merge_gate.py` — its tests on scratch repos

**Seats:** 1 writer + 1 tester
- `impl` — `scripts/production/merge_gate.py`
- `test` — `scripts/production/test_merge_gate.py` from the Spec alone

**Acceptance gate:** tests for refusal cases, conflict abort, red reset and failure print, green push, promote refusals and the `ff_rc=` read, using scratch repos; a real run on a scratch merge tree records the gate time and peak GB, once quiet and once under load.

### Phase 3 — The code under test stops waiting  · status: todo

**Depends on:** Phase 2

#### Work Order

**Goal:** the named waits leave the suites without weakening any test.

**Spec:** `review.sh:797` `sleep 5`; `implement.sh:509/519` `sleep 1`; `add_showrunner.py:45` `POLL_S`; `offload.py:1130` ssh timeout made overridable and short in tests; `codex_mesh.py:3599`, `:3248` sleeps and `RESIDENT_POLL_SECS` (`:191`) made event-driven or overridable; the stub server's accept loop woken on close (`test_codex_mesh.py:138,145-151`); `test_stall_watch.py:689` `delay=20`; the delegate test polls and stub sleeps (`test_verify_mac_offload.py:548,1253`, `test_verify_token_wait.py:69`, `test_verify_hold_wait.py:173`); fsync in `progress_history.py:492`, `review_pool.py:225`, `findings.py:369`, usage `collect.py`; `lib/py`'s interpreter cache under a test `HOME`. Each wait becomes an event, a parameter the test passes, or a value the test overrides; production defaults stay.

**Files:**
- `scripts/delegate/review.sh` — `:797` `sleep 5`
- `scripts/delegate/implement.sh` — `:509`, `:519` `sleep 1`
- `scripts/delegate/progress_history.py` — fsync (`:492`)
- `scripts/delegate/review_pool.py` — fsync (`:225`)
- `scripts/delegate/findings.py` — fsync (`:369`)
- `scripts/delegate/` — the test polls and stub sleeps named in the Spec
- `scripts/lib/py` — interpreter cache under a test `HOME`
- `scripts/production/add_showrunner.py` — `POLL_S` (`:45`)
- `scripts/production/test_stall_watch.py` — `delay=20` (`:689`)
- `scripts/mac_test/offload.py` — ssh timeout (`:1130`)
- `scripts/agents/codex_mesh.py` — sleeps (`:3248`, `:3599`) and `RESIDENT_POLL_SECS` (`:191`)
- `scripts/agents/test_codex_mesh.py` — stub server accept loop (`:138`, `:145-151`)
- `scripts/usage/collect.py` — sqlite fsync

**Seats:** 2 writers — split by suite
- `impl` — `scripts/delegate/` and `scripts/lib/py`
- `test` — opens as impl; `scripts/agents/`, `scripts/mac_test/`, `scripts/usage/`, `scripts/production/`

**Acceptance gate:** touched suites green; basedpyright clean; the gate command's time recorded before and after.

### Phase 4 — Trivial tests are gone  · status: todo

**Depends on:** Phase 3

#### Work Order

**Goal:** no test in the 22 suites meets the criteria in "Trivial tests: the criteria"; agents are told the criteria before they write tests.

**Spec:** audit all 22 suites against the criteria; report per-suite counts (and the unsure list) to the showrunner before deleting. Delete trivial tests outright; merge true duplicates into one; never weaken a non-trivial test. Report the tests and seconds removed separately from the speed fixes. Add one line naming the criteria to the one source agents read before writing tests (find it: the unit and seat instructions that govern test writing).

**Files:**
- `scripts/` — test files across the 22 suites
- `docs/delegate/write_prompt_contract.md` — one line naming the criteria (or the one source agents read before writing tests, if another)

**Seats:** 2 writers — split by suite
- `impl` — production, delegate, mac_test, usage, agents, buildlog, hooks
- `test` — opens as impl; the other fifteen suites and the instruction line

**Acceptance gate:** every suite green; the counts report sent before deletion; the instruction line in place.

### Phase 5 — Fewer process launches per test  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** the heaviest suites start far fewer processes.

**Spec:** run the heaviest scripts in-process or trim their import chains (`dailies_input`, `dailies_render`, `send`, `build_targets`, `waiting`); write the fake tmux and gh tools as shell instead of Python; share git fixtures where a test only reads them (e.g. `merge_checkpoint`). Keep each test's assertions.

**Files:**
- `scripts/production/` — tests, fake tmux and gh, and the named scripts
- `scripts/delegate/` — tests and fakes

**Seats:** 2 writers — split by suite
- `impl` — `scripts/production/`
- `test` — opens as impl; `scripts/delegate/`

**Acceptance gate:** suites green; exec counts and gate time recorded before and after.
