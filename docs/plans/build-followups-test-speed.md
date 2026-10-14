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

#### As-built

- No test reaches a live home or shared path. The production quota tests supply `CodexAlertPassReadings` (`scripts/whoami/quota_alert.py`) and never start the real `codex app-server`; a patched `seats_home` raises, so any fallback to a live Codex home fails the test. `end_session.sh` reads `PLAN_DELEGATE_ACTIVE_DIR` (default `/tmp/claude/delegate/active`), and its test points it at a temp dir.
- The delegate signal test restores `SIG_DFL` for SIGINT before exec and waits for a `runner-signals-ready` marker written after the runner installs its handlers. The agents quota-continuation handoff test runs with a 30 s start timeout, so its socket round trips never race the five-second task deadline under load.
- agents carries `test_start_on_a_closed_ephemeral_port_fails_before_creating_a_thread`: it binds an ephemeral port, closes it, and expects the start refused. No test depends on fixed port 55555.
- No suite asserts wall-clock time. Every threshold across the 22 suites is a work count, a controlled clock, or a comparison calibrated in the same run: buildlog report compares CLI CPU at full and quarter log size, three runs each; `build_hold` traces subshells with a DEBUG trap; stall-watch sends meet at a six-way barrier and leave stderr empty; shutdown's `test_six_tmux_launches_run_at_once` counts six launches in flight; mac_test and delegate wait accounting read exact controlled durations.

**Files:**
- `scripts/production/test_waiting.py` — quota tests on supplied readings; `seats_home` guard
- `scripts/delegate/end_session.sh` — `PLAN_DELEGATE_ACTIVE_DIR` override for the active-marker root
- `scripts/delegate/test_end_session.py` — marker under a temp dir
- `scripts/delegate/test_verify_mac_offload.py` — `SIG_DFL` restore and `runner-signals-ready` wait
- `scripts/agents/test_codex_mesh.py` — closed ephemeral port test; 30 s start timeout on the quota handoff
- `scripts/buildlog/test_report.py`, `test_mac_report.py`, `test_record.py` — same-run CPU comparisons and work counts
- `scripts/build_hold/`, `scripts/production/test_stall_watch.py`, `scripts/shutdown/`, `scripts/mac_test/test_offload.py`, `scripts/delegate/test_verify_token_wait.py`, `scripts/message/`, `scripts/hooks/test_conversation_pause.py` — timing assertions replaced by counts, barriers or controlled clocks

**Binds later work:** the merge-gate command ("One committed gate command runs the suites in parallel") starts suites with default signal dispositions. The buildlog report speed tests run the CLI at two sizes three times each, a launch cost for "Fewer process launches per test".

**Gotchas:** a suite started in the background of a non-interactive shell inherits SIGINT ignored, and bash cannot forward a signal it ignores; any test that sends a signal to a child restores `SIG_DFL` before exec.

### Phase 2 — One committed gate command runs the suites in parallel  · status: done

#### As-built

- `scripts/production/merge_gate.py --merge-branch --unit-branch --checkpoint --previous-checkpoint --unit --phase` refuses unless the merge checkout is clean (untracked files included) and at `origin/<merge branch>`, the checkpoint is on `origin/<unit branch>` (fetched by explicit refspec), and the previous checkpoint is its ancestor. It merges `--no-ff` with the subject `Merge <unit> <phase> (<hash>) into <merge branch>`; on a conflict it aborts and names the files. On red it resets to origin, prints each red suite's failures and exits 1; SIGTERM and SIGHUP roll back the same way; on green it pushes.
- The gate runs basedpyright alongside all 163 test modules of the 22 suites (`TEST_SUITES` in the script is the one list), one process group per module, `TMPDIR` on `/dev/shm`. Modules start longest first by the previous run's times. Admission is on sampled RSS under a 4096 MiB cap (`--memory-cap-mib`): each running module holds a reservation of max(its RSS, largest module peak seen, initially 96 MiB) until it uses it, one module may always run, and an observed overshoot makes the run red. Every module's process group is killed on exit. It prints per-suite wall time, peak memory and the largest module.
- `--promote` fast-forwards `~/.claude` main (ff-only decides; no clean check) and pushes, refuses while a build-hold release runs (the process match covers relative and absolute invocations), keeps the settings.json filter check, then fast-forwards the Mac over ssh and reads the printed `ff_rc=`, since ssh's own status is always 0 there. A Mac failure after promotion reports `main promoted to <hash>; Mac fast-forward failed`. `--tests-only` runs the suites with no git operations. Callers' PYTHONPATH is unchanged.
- Measured: 75.17 s and 2.30 GiB peak at load 6–22; 112.69 s at load 31–72, all green; the sequential gate took about 30 minutes.
- Load reds the parallel run exposed are fixed at their source: `quota_alert.py` imports absolutely (no forged `__package__`); the usage `fake_ssh` writer drains; delegate tests advance on events; `verify.sh` and `build_targets` local waits wake on release through `scripts/delegate/local_release_wait.py`; production script-launch deadlines are 300 s hang guards, with lock tests advancing on pipe events; the mac_test gh-timeout test injects `TimeoutExpired`; codex_mesh follow tests use hang guards.

**Files:**
- `scripts/production/merge_gate.py` — the gate, the suite list, admission and promotion
- `scripts/production/test_merge_gate.py` — 44 tests on scratch repos
- `scripts/delegate/local_release_wait.py` — inotify wake on hold release, interval fallback
- `scripts/delegate/verify.sh`, `scripts/build_targets/build_targets.py` — local waits through it
- `scripts/whoami/quota_alert.py`, `scripts/usage/fixtures/collect/mac/fake_ssh.py` — load fixes
- `scripts/production/test_*.py`, `scripts/delegate/test_verify_*.py`, `scripts/mac_test/test_mac_test.py`, `scripts/agents/test_codex_mesh.py`, `scripts/usage/test_collect.py`, `scripts/shutdown/test_shutdown_cli.py` — event-driven waits, hang guards and regression tests

**Binds later work:** the gate's ordering cache is `${MERGE_GATE_STATE_DIR:-~/.local/state/merge-gate}/module_seconds.json`. `build_targets.py` imports `scripts.delegate.local_release_wait` by putting the repo root on `sys.path`.

**Gotchas:** admission works on RSS samples at least 0.25 s apart, not an OS limit; a module that grows suddenly can still pass the cap, which turns the run red. `local_release_wait` falls back to polling where inotify is absent (macOS). The red-suite rerun rule stays with the showrunner, outside the command.

### Phase 3 — The code under test stops waiting  · status: done

#### As-built

- Every fixed wait the tests sat through is an override the test sets, with production defaults unchanged and each default pinned by a test: `review.sh` early-delivery poll `PLAN_DELEGATE_EARLY_POLL_SECS` (5); `implement.sh` seat and turn probes `PLAN_DELEGATE_SEAT_POLL_SECS` (1) and `PLAN_DELEGATE_SEAT_WAIT_ATTEMPTS` (30); `add_showrunner.py` `ADD_SHOWRUNNER_POLL_S` (2.0); `OffloadConfig.leftover_timeout_s` in `offload.py` (10, from the settings file); `codex_mesh.py` `RESIDENT_POLL_SECS` (1.0), `REAP_WAIT_SECS` (5.0) and `REAP_POLL_SECS` (0.2), patched as module attributes in tests.
- `verify.sh` reads its token-wait clock from `PLAN_DELEGATE_TOKEN_CLOCK_FILE` when set, else `$SECONDS`, so the token-wait test advances a file instead of sleeping.
- Disposable fixtures skip disk sync: `PLAN_DELEGATE_SYNC_WRITES=0` skips fsync in `progress_history.py`, `findings.py` and `review_pool.py`; `collect.py --durability off` (`LedgerDurability`, default `full`) sets `PRAGMA synchronous=OFF`.
- The interpreter-cache miss is fixed in the delegate fixtures, not in `lib/py`: each temporary `HOME` gets `XDG_CACHE_HOME` with a seeded `claude/python3-<version>` cache.
- `scripts/delegate/wait_for_test_state.py` (`wait_until`, `wait_for_file`) waits on directory events and child pidfds through `local_release_wait`, polling where inotify or pidfd is absent. The stub app-server's accept loop blocks in `select` on the listener and a socketpair that `close()` writes, so closing wakes it at once.
- `merge_gate.py` refuses a `--type-dirs` path absent from the merge checkout before any suite starts.
- Measured: gate green at 183 s and 2.33 GiB peak at load 88–123; 151.6 s and 2.41 GiB on the merged tree at load 92–121. Matched-load A/B: `test_review_launcher` 69→36 s, `test_implement_launcher` 37→33 s, `test_progress_history` unchanged at 78 s.

**Files:**
- `scripts/delegate/review.sh`, `implement.sh`, `verify.sh` — poll, probe and token-clock overrides
- `scripts/delegate/progress_history.py`, `findings.py`, `review_pool.py` — `PLAN_DELEGATE_SYNC_WRITES`
- `scripts/delegate/wait_for_test_state.py` — event-driven fixture waits
- `scripts/production/add_showrunner.py`, `scripts/mac_test/offload.py`, `scripts/agents/codex_mesh.py`, `scripts/usage/collect.py` — poll, timeout, reap and sync overrides
- `scripts/agents/test_codex_mesh.py`, `scripts/delegate/test_*.py`, `scripts/mac_test/test_offload.py`, `scripts/production/test_add_showrunner.py`, `scripts/usage/test_collect.py` — fixtures on the overrides and regressions pinning each default

**Binds later work:** "Trivial tests are gone" keeps this phase's regressions (sync default, interpreter cache, wait helper, reap, stub close); each was mutation-proved. "Fewer process launches per test" starts from the gate's slowest modules at load 88–123: `delegate/test_verify_mac_offload` 181 s, `delegate/test_review_launcher` 165 s, `production/test_dailies_input` 163 s, `delegate/test_progress_history` 161 s, `mac_test/test_offload` 134 s, `production/test_add_unit` 132 s; the gate's wall time is its slowest module.

**Gotchas:** fsync was already cheap on tmpfs; `test_progress_history`'s cost is about 750 launches of `progress_history.py`. A pidfd stays readable after exit, so `wait_until` observes each one once and drops it.

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

**Constraints from prior phases:** Phase 3's regressions were each mutation-proved and are not trivial: the fsync-by-default tests (`test_findings.py`, `test_progress_history.py`, `test_review_pool.py`), the interpreter-cache launch count (`test_review_launcher.py`), the `wait_for_test_state` child-exit and no-pidfd tests, codex_mesh reap and stub-close tests, `test_add_showrunner.py`'s `ADD_SHOWRUNNER_POLL_S` tests, `test_offload.py`'s `leftover_timeout_s` test and `test_collect.py`'s `--durability` tests. These pin settings a caller can set (`collect.py --durability` is in its help; `leftover_timeout_s` is offload configuration). Every proposed deletion names the behavior no test then checks (none) or the surviving test with identical inputs and outcome.

### Phase 5 — Fewer process launches per test  · status: todo

**Depends on:** Phase 4

#### Work Order

**Goal:** the heaviest suites start far fewer processes.

**Spec:** the gate's wall time is its slowest module, so start from the slowest modules (learned timings at load 88–123: delegate/test_verify_mac_offload 181 s, delegate/test_review_launcher 165 s, production/test_dailies_input 163 s, delegate/test_progress_history 161 s, mac_test/test_offload 134 s, production/test_add_unit 132 s), and record each targeted module's launch count and matched-load duration before and after. `test_progress_history.py` launches `progress_history.py` about 750 times (its existing `invoke_handler` already runs handlers in-process). Run the heaviest scripts in-process or trim their import chains (`dailies_input`, `dailies_render`, `send`, `build_targets`, `waiting`); write the fake tmux and gh tools as shell instead of Python; share git fixtures where a test only reads them (e.g. `merge_checkpoint`). Keep each test's assertions.

In-process rules: restore environment, cwd, import-time configuration (e.g. `add_showrunner.POLL_S`), module globals and signal state after each case; keep at least one subprocess test per script for CLI parsing, exit status and the launch contract. Shared git fixtures: share only an immutable seed; each mutating case gets its own origin and worktrees, never shared refs, index, config or worktree metadata; modules pass in any order and alone. Add tests that run with the Phase 3 overrides absent (`PLAN_DELEGATE_SEAT_POLL_SECS`/`_WAIT_ATTEMPTS`, `PLAN_DELEGATE_EARLY_POLL_SECS`, `PLAN_DELEGATE_TOKEN_CLOCK_FILE`) and pin the production defaults. If recorder identity paths (`_main_identity_from_codex`/`_claude`, `AgentIdentity | None`) become an in-process API, return a semantic result (known / unavailable) instead of a bare optional, keeping the fallback in `_detect_main_identity()`.

**Files:**
- `scripts/production/` — tests, fake tmux and gh, and the named scripts
- `scripts/message/` — `send.py` and its tests
- `scripts/build_targets/` — `build_targets.py` and its tests
- `scripts/delegate/` — tests and fakes, including `wait_for_test_state.py`

**Seats:** 2 writers — split by suite
- `impl` — `scripts/production/`, `scripts/message/send.py`, `scripts/build_targets/`
- `test` — opens as impl; `scripts/delegate/` (sole owner of `wait_for_test_state.py`)

**Acceptance gate:** production, delegate, message and build_targets suites green, each targeted module also alone; per-module launch counts and matched-load durations, and gate time, recorded before and after.
