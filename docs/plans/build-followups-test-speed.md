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

### Phase 4 — Trivial tests are gone  · status: done

#### As-built

- All 5,713 tests in the 22 gate suites are audited against "Trivial tests: the criteria": 7 candidates, 21 unsure tests kept.
- Five tests are deleted, 1.37 s of test time (1.365 s of it the add_unit duplicate): criterion-5 duplicates in `production/test_add_unit.py` (survivor `test_restart_restore_is_allowed_for_its_restarting_unit`), `production/test_review_pause.py` (survivor `test_pause_and_resume_both`) and `buildlog/test_treekey.py` (survivor `test_symlink_read_error_leaves_tree_key_unavailable`); the `build_hold` import-name check; the `shutdown` standard-library `StrEnum` lookup check. No duplicates are merged; each survivor is unchanged.
- The two fixture guards that block real phone sends stay, renamed with a docstring: `whoami/test_quota_alert.py::test_setup_blocks_real_phone_sends` and `lint/test_sweep.py::test_base_blocks_real_phone_sends`.
- `docs/delegate/write_prompt_contract.md` section 6 (`## Work Specification`) carries one line with the five criteria, which prompt authors copy into every seat prompt that writes tests.
- Gate after: all 22 suites green, 193.8 s, 2.44 GiB peak at load 64–101 (183 s at load 88–123 after "The code under test stops waiting"; loads differ, so not comparable).

**Files:**
- `docs/delegate/write_prompt_contract.md` — section 6 carries the trivial-test criteria
- `scripts/production/test_add_unit.py`, `scripts/production/test_review_pause.py`, `scripts/buildlog/test_treekey.py`, `scripts/build_hold/test_build_hold.py`, `scripts/shutdown/test_send_guard.py` — one test deleted from each
- `scripts/whoami/test_quota_alert.py`, `scripts/lint/test_sweep.py` — phone-send guards renamed

**Binds later work:** "Fewer process launches per test" receives the criteria through `write_prompt_contract.md` section 6: a moved test replaces its subprocess form, never sits beside it with the same inputs and assertion; a default-pinning test runs the real script with the override unset and observes the default through behavior, never by reading the literal from source. The add_unit restore case lives only in `test_restart_restore_is_allowed_for_its_restarting_unit`. The unsure tests kept in its target modules (`test_progress_history.py::test_recorder_helpers_use_public_names_without_private_aliases`, `test_offload.py::test_activity_and_leftover_texts_use_the_shared_mirror_selector`) stay.

**Gotchas:** the suites hold almost no trivial tests, so this phase does not move gate time. The shell overrides `PLAN_DELEGATE_SEAT_POLL_SECS`, `PLAN_DELEGATE_SEAT_WAIT_ATTEMPTS`, `PLAN_DELEGATE_EARLY_POLL_SECS` and `PLAN_DELEGATE_TOKEN_CLOCK_FILE` have no test that runs with them unset; "each default pinned by a test" holds for the Python overrides only.

**Ruled out:** deleting the two phone-send guards — they pin the fixture patch that stops real sends.

### Phase 5 — Fewer process launches per test  · status: done

#### As-built

- `scripts/production/cli_fixture.py` `invoke_cli(module, main, arguments, environment, cwd, *, combined_output=False, production_state=False) -> subprocess.CompletedProcess[str]` runs a script's real parser, handlers and child commands in-process, and after each case restores environment, cwd, argv, `sys.path`, the SIGINT/SIGTERM/SIGHUP/SIGQUIT handlers and the module's global bindings. With `production_state` it imports `dailies_render` and `showrunners` in the caller's environment first, then patches their import-time paths (`CHART_CONF`, `AGENTS_DIR`, `READINGS_LOG`; `CONFIG`, `NOTIFIER_STATE_DIR`, `SESSIONS_DIR`, `SHOWRUNNER_STATE_DIR`, `SESSIONS`, `SEND`) under the fixture HOME. Each converted script keeps one subprocess test for CLI parsing, exit status and the launch contract (`subprocess_cli=True`).
- `ImmutableGitSeed.create(root, branch, author, email, files)` builds one checkout and bare origin per test class; `copy_to(checkout, origin)` gives each case private refs, config, index and origin by copying both and repointing the remote.
- `scripts/production/fake_tmux.sh` (bash + jq) reads the same JSON state as `fake_tmux.py` and replaces it on the hot path (`UNIT_LOOKUP_TMUX`). It rejects malformed calls with exit 2; a missing session or pane exits 1 with a diagnostic naming the target and leaves the state unchanged. The `ci_points` fake `gh` is a shell + jq stub.
- `send.py` imports `roster` only when a send needs local session discovery; acknowledgements, explicit sockets and named-machine sends skip it, with routes and output unchanged.
- `scripts/delegate/` is untouched; its share of the work is the phase "The delegate tests start fewer processes". `build_targets` is unchanged, already at 4 launches per module run.
- Measured launches across the targeted modules: 11682 → 8764. `test_dailies_input` 3569 → 1387 (422 → 126 s), `test_dailies_render` 143 → 3 (83 → 6 s), `test_add_unit` 3773 → 3205 (393 → 318 s); `test_waiting`, `test_send`, `test_ci_points` and `mac_test/test_offload` roughly flat. Gate: 100.87 s, 2.54 GiB peak at load 30–58 (193.8 s at load 64–101 after "Trivial tests are gone").

**Files:**
- `scripts/production/cli_fixture.py` — `invoke_cli` and `ImmutableGitSeed`
- `scripts/production/fake_tmux.sh` — shell fake tmux over the `fake_tmux.py` JSON state
- `scripts/production/test_dailies_input.py`, `test_dailies_render.py`, `test_add_unit.py`, `test_waiting.py`, `test_ci_points.py` — in-process CLI cases, per-class git seeds, shell fakes
- `scripts/mac_test/test_offload.py` — in-process offload CLI cases; imports `cli_fixture` through a `sys.path` insertion
- `scripts/message/send.py`, `scripts/message/test_send.py` — deferred session discovery and its tests

**Binds later work:** "The delegate tests start fewer processes" reuses `invoke_cli` with `production_state` off through a thin delegate wrapper for its own preloads, path rebinding and cache reset. The gate's slowest remaining modules are delegate's.

**Gotchas:** a module first imported inside the fixture computes its import-time paths from the fixture HOME, and restoring bindings then restores those fixture paths; preload such modules before swapping the environment (pinned by `test_dailies_render.py::ReportFooterTests::test_cold_imported_production_paths_return_to_the_callers_home_after_each_case`). `invoke_cli` restores module bindings, not mutable containers held in module globals, so it suits synchronous cases only. `fake_tmux.sh` signals a missing target through jq `halt_error(1)`; jq's own failure exit is 5 (pinned by `test_waiting.py::WaitingTests::test_shell_tmux_names_missing_targets_and_preserves_state_on_every_targeted_call`). The gate's wall time is its slowest module.

**Ruled out:** a delegate-local copy of the CLI fixture — one restore implementation is simpler.

### Phase 6 — The delegate tests start fewer processes  · status: done

**Depends on:** Phase 5

Split from Phase 5 on 2026-10-14 (packaging): `scripts/delegate/` was released to message-clarity for its seat→worker rename, so this phase starts once that rename has merged into build-followups; merge it into this branch first.

#### Work Order

**Goal:** the delegate suite starts far fewer processes.

**Spec:** start from the slowest delegate modules (learned timings at load 88–123: test_verify_mac_offload 181 s, test_review_launcher 165 s, test_progress_history 161 s); after the prerequisite merge and before editing, re-time the delegate modules to confirm that order. Record each targeted module's launch count and matched-load duration before and after: `strace -f -qq -e trace=execve -e signal=none -o <scratch file> python3 -m unittest discover -s scripts/delegate -p '<module>' </dev/null`, count successful `execve` lines (ending `= 0`), record the duration and `/proc/loadavg`, and delete the trace once read; measure after the final edit at comparable load or with old and new scratch copies running concurrently, and report recorder launches apart from total execs. `test_progress_history.py` launches `progress_history.py` about 750 times at run time through `run_command` (`:146`), `run_failing_command` (`:181`) and `run_unowned_command` (`:1830`); `invoke_handler` (`:209`) runs four cases in-process but skips the recorder lock (`progress_history.py:4895`) and mocks every child process, so it is not the model for the conversion. Keep each test's assertions. Phase 5's in-process rules hold here (restore environment, cwd, import-time configuration, module globals and signal state after each case; keep at least one subprocess test per script for CLI parsing, exit status and the launch contract). Add tests that run `review.sh`, `implement.sh` and `verify.sh` with the Phase 3 overrides absent (`PLAN_DELEGATE_SEAT_POLL_SECS`/`_WAIT_ATTEMPTS`, `PLAN_DELEGATE_EARLY_POLL_SECS`, `PLAN_DELEGATE_TOKEN_CLOCK_FILE`) and pin the production defaults. If recorder identity paths (`_main_identity_from_codex`/`_claude`, `AgentIdentity | None`) become an in-process API, return a semantic result (known / unavailable) instead of a bare optional, keeping the fallback in `_detect_main_identity()`.

**Files:**
- `scripts/delegate/` — targeted tests and fakes, the delegate CLI wrapper, `wait_for_test_state.py`, and `progress_history.py` where invocation isolation or semantic identity results need changes

**Seats:** 1 writer
- `test` — opens as impl; `scripts/delegate/` (sole owner of `wait_for_test_state.py`)

**Constraints from prior phases:** the trivial-test criteria in `docs/delegate/write_prompt_contract.md` section 6 bind every test this phase moves or adds: a test moved in-process replaces its subprocess form, and each new default-pinning test runs the real script with the override unset and observes the default through behavior (a `sleep` stub on `PATH` recording its argument, an attempt count), never by reading the literal from the script source. Review and implement fixtures set their poll overrides (`test_review_launcher.py:471`, `test_implement_launcher.py:342`); ordinary verify cases already run with `PLAN_DELEGATE_TOKEN_CLOCK_FILE` unset but pin no wait; no test removes the overrides and pins the defaults (`review.sh:797` sleep 5; `implement.sh:505,511,515,521` 30 attempts of sleep 1 in both probe loops; `verify.sh:1172` `$SECONDS`). Remove the variables from each test environment explicitly; a `sleep` stub on `PATH` records its argument and returns at once (5 for review; 1 and 30 exhausted attempts for both implement loops, the agent alive and readiness absent); for verify, a stub board `acquire` that blocks just over one second shows a recorded wait of at least 1 with the clock file absent, a lower bound that load cannot fail, and no upper wall-time bound or real default wait.

"Fewer process launches per test" built `scripts/production/cli_fixture.py`: `invoke_cli` runs a script's real parser and handlers in-process and restores environment, cwd, argv, `sys.path`, signal handlers and module bindings after each case. Reuse it (`from scripts.production.cli_fixture import invoke_cli`, the package form these tests already use; no `sys.path` insertion) with `production_state` off, and keep delegate-only work in a delegate wrapper: import, before the environment swap, every module the recorder imports lazily (a module first imported under a fixture `HOME` keeps fixture paths after restore); rebind import-time paths derived from `HOME`/XDG with patches computed from the fixture environment, the recorder's default configuration paths included; clear `progress_history._config_digest`'s cache (`progress_history.py:262`, cached per process) before each invocation; raise `CalledProcessError` where `run_command` checks. Convert `run_command`, `run_failing_command` and `run_unowned_command` through the real recorder `main()`, keeping its lock and real child integrations, with output, exit status and errors unchanged. Add a cold-interpreter regression: two fixture homes, then caller paths restored. Keep `PLAN_DELEGATE_SYNC_WRITES=0` fixture-scoped, the durable-default and changed-configuration (`test_progress_history.py:1938`) regressions, the event waits and the per-`HOME` interpreter-cache seeding.

`scripts/delegate/` is renamed seat→worker by the prerequisite merge: after merging, re-read the launchers, recorder and fixture helpers and use the names they hold (environment variables, functions, files, vocabulary); names in this Work Order describe the tree before the rename and never authorize restoring old ones.

**Acceptance gate:** delegate suite green, each targeted module also alone; per-module launch counts and matched-load durations, and gate time, recorded before and after.
