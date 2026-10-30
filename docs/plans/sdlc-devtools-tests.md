# sdlc-devtools-tests

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Make the `~/.claude` repo tests run in every gate, faster, and with less CPU. Phase 1 fixes the gate's test list. Phases 4-6 come from the measured inventory of full gate 3 (2026-10-30).

> **Production: sdlc** — unit `devtools-unit`; production doc `docs/plans/sdlc-production.md`

## Source

The user, through the sdlc showrunner, 2026-10-30: "it can also be where we look at making repo tests go faster and take less cpu". Phase 1: herdr through natedev and the showrunner, 2026-10-30. natedev agreed the `merge_gate.py` edit ("take it"; no open natedev branch touches it).

Known input for the inventory: `scripts/agents/test_codex_mesh.py` was killed at the 1800 s module limit under load (gate_merge77.log). It takes about 1251 s unloaded and 741 s in a later gate, and it grows with each mesh test. Check first if herdr's plan retires the mesh soon.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-10-30T11:40:41.136+00:00
- **Stack:** Python 3.13, standard library only; Markdown.
- **Key files:**
  - `scripts/production/merge_gate.py` — the merge gate: finds test modules, runs them under a memory cap, keeps the passed-tree ledger (`PassedTreeLedger`)
  - `scripts/production/merge_gate_test_support.py`, `scripts/production/test_merge_gate*.py` — its fixture and tests
  - `docs/as-built/merge-gate-and-test-speed.md` — the gate's as-built
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'`, from the worktree root. Gate: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo . --tests-only --full`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state (`~/.claude` main, `/tmp/claude`, `~/.cargo/bin`).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - All text uses Simplified Technical English (user, 2026-10-18).
  - One place for each thing: no second list of suites anywhere (user, 2026-10-30).

## Phases

### Phase 1 — The merge gate finds every test module itself  · status: done
**Depends on:** none

#### As-built

- `scripts/production/merge_gate.py` has no suite list. `discover_test_modules` finds every `scripts/**/test_*.py` outside `__pycache__`, sorted by path. A module's suite is its first folder under `scripts/`, so a module in a nested `tests/` folder runs under its top folder's suite. Each module runs as `unittest discover -s <its folder> -p <its file>`, with the repo as cwd.
- A test file directly in `scripts/` makes the gate refuse before any job starts, and the message names the path. An empty test list is refused too. The passed-tree ledger `suites` field holds the sorted found suite names.
- `GateJobKind` (`TEST_MODULE`, `TYPE_CHECK`) selects job behavior, so a folder named `types` runs its tests. `GateJobRequest` keeps each job's real path, so holds and retries run that path.
- Each job reserves its last recorded peak × `MEMORY_PEAK_MARGIN` (1.2). One margin covers modules and the types job. A module with no record reserves the largest recorded module peak × 1.2, or `UNRECORDED_MODULE_RESERVATION_MIB` (640) when no module has a record. A types job with no record reserves `TYPES_MEMORY_MIB` (3072).
- A job starts only when the latest aggregate sample, the remaining reservations of the running jobs and its own reservation sum to the cap or less. With nothing running, one job always starts.
- `module_peak_mib.json` in the gate state dir holds each module's last sampled peak under its `<suite>/<path within the suite>` key (`test_module_name`), beside `types_peak_mib.json`. The gate writes it at run end. A job killed for a limit records nothing.
- `DEFAULT_MEMORY_CAP_MIB` is 6144. It holds the types job's reservation (full-tree basedpyright peak 4065 MiB × 1.2 = 4878 MiB) with modules beside it. A job over the cap is killed, and a sampled peak over the cap makes the run red.

**Files:**
- `scripts/production/merge_gate.py` — discovery, job kinds, recorded-peak admission, peak records, the cap.
- `scripts/production/merge_gate_test_support.py` — the fixture; the folder names it writes are fixture data only.
- `scripts/production/test_merge_gate*.py` — tests for discovery, nested modules, the `types` folder, the root-file refusal, the ledger key and admission.
- `scripts/berth/tests/check_installed_hooks.py` — checks the installed front end, not the checkout, so it is not a gate test. `scripts/berth/install/README.md` tells how to run it after an install: `cd ~/.claude/scripts && python3 -m unittest berth.tests.check_installed_hooks`.
- `docs/as-built/merge-gate-and-test-speed.md` — discovery and recorded-peak admission. `long-lived-workers.md`, `style-showrunner.md`, `bevy-upgrade.md` and `showrunner-automation.md` in `docs/as-built/` describe discovery, not a list.

**Binds later work:** a test module in any `scripts/<folder>/` runs in every gate with no list edit. The copy check in "Every test uses the one hang guard" uses this.

**Gotchas:**
- A new test folder changes the ledger `suites` key, so the next gate runs in full one time.
- `shot_report/test_transcripts.py` fails only when TMPDIR is inside a Claude scratchpad path. Under the gate's `/dev/shm` TMPDIR it passes.

**Ruled out:** a `"types"` suite name to select the type check; a retry path built again from suite and module names.

### Phase 2 — Every test uses the one hang guard  · status: todo
**Depends on:** Phase 1; models' checkpoint that moves the hang guard to `scripts/lib` and moves `scripts/agents` onto it (ETA 10:45 EDT, 2026-10-30)

Source: the sdlc showrunner, 2026-10-30. 106 test files under `scripts/` each define their own `TEST_HANG_GUARD_SECS` (105 at 300 s, `recorder_fixture.py` at 600 s), and many set their own short real-time limits that fail under load (the cause of the `agents/test_codex_mesh.py` red in Phase 1's gate). After models' checkpoint, one mechanical checkpoint from devtools: every other test imports the one guard; each short real-time limit goes onto the guard, or onto the shared `LifecycleTestClock` where time is the behavior under test; and a check refuses a new local copy. Owners are many; tell natedev which natedev-owned folders it touches. The merge gate finds every `scripts/**/test_*.py` itself (Phase 1), so the copy check runs in every gate from any folder with no list edit; a test file directly in `scripts/` is refused. The Work Order is written when models sends the import form.

### Phase 3 — Close-route closes a route made before its phase record  · status: done
**Depends on:** none

#### As-built

- `scripts/history/routed.py` keys each route `<run id>:<phase instance id>:<finding id>` from its `finding_routed` event. A route made before its run's phase record has an empty phase instance id, so the routed-work list prints its key as `<run id>::<finding id>`.
- `findings.py close-route --from` takes the exact key that the routed-work list prints, and it accepts an empty phase instance id. It refuses a key without exactly three parts, and a key with an empty or blank run id or finding id. The refusal message names both forms: `<run id>:<phase instance id>:<finding id>` and `<run id>::<finding id>`.
- The `finding_route_closed` event keeps its shape. No reader of it changed.

**Files:**
- `scripts/direct/findings.py` — `_close_route` checks the `--from` key.
- `scripts/direct/test_findings.py` — one test closes a route from before a phase record through the real routed-work join (`routed_work`); one test shows that nine malformed keys refuse and record no event.

**Ruled out:** a second `--from` form, or a translation from one form to the other — `--from` is always the exact listed key.

### Phase 4 — The gate records each job's CPU time  · status: done
**Depends on:** Phase 1

#### Work Order

**Goal:** each gate job's CPU time is recorded beside its seconds and memory peak, and the report names the jobs that used the most CPU, so "less CPU" has a measure.

**Spec:**

- Cause: the gate records each module's seconds (`module_seconds.json`) and peak (`module_peak_mib.json`), but no CPU time. In full gate 3 (2026-10-30) the one-minute load went to 97 on 32 CPUs, and nothing says which modules made it.
- Measure: a job's CPU seconds are the user + system time of its leader process and every child that the leader waited for, read when the gate reaps the leader (for example `os.wait4` in place of `poll` in the run loop, `scripts/production/merge_gate.py:891`). A child killed by `kill_job_group` after its leader exits is not counted; say so in the as-built.
- `GateJobResult` gets the CPU seconds. The runner saves them at run end in `module_cpu_seconds.json` in the gate state dir, under the same `<suite>/<path within the suite>` key as `module_seconds.json`, with `save_measures`. The type-check job's CPU seconds go under its own key, as its peak does. A job killed for a limit records nothing, the same as for its peak.
- Report: after the `Largest module peak:` line, one line names the three jobs with the most CPU seconds and the run's total CPU seconds: `Most CPU: <seconds> s <suite/module>, …; total <seconds> s`.
- Docs: `docs/as-built/merge-gate-and-test-speed.md` names the new record and the report line.
- Tests: a job that spins for a measured time records CPU seconds near that time; a job that sleeps records near zero; a killed job records nothing; the report line names the largest job first.

**Files:**
- `scripts/production/merge_gate.py`
- `scripts/production/test_merge_gate_suites_resources.py`
- `docs/as-built/merge-gate-and-test-speed.md`

**Workers:** `1 writer` — one change in one module and its tests.

**State:** new `module_cpu_seconds.json` in the gate state dir, written at run end with `save_measures`.

**Acceptance gate:** the merge gate tests pass (`python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'`); basedpyright prints `0 errors, 0 warnings, 0 notes` on the changed Python files; the checkout's own gate (`merge_gate.py --repo . --tests-only --full`) is green and prints the `Most CPU:` line.

### Phase 5 — The gate runs a large module's test classes as separate jobs  · status: todo
**Depends on:** Phase 4; resources-unit's be4d46f7 on main (it changes the same `merge_gate.py` hunks)

Source: devtools-unit from the full gate 3 measure, 2026-10-30; natedev's go, 2026-10-30, with one condition (below). The gate already starts the slowest recorded module first (`module_order`), so the wall time is the largest single module: `agents/test_codex_mesh.py` took 1057 s of the gate's 1062 s (32 classes, 593 tests). The next largest are `direct/test_assign_launcher.py` 734 s (1 class), `shutdown/test_bounded_stop.py` 442 s (2 classes) and `direct/test_verify_hold_wait.py` 311 s (1 class). herdr deletes `test_codex_mesh.py` only in its Phase 18. Goal: a module whose recorded time is over a limit runs as one job per test class, each with its own recorded time, peak and CPU time, so no test file changes and the wall time drops to about 734 s. natedev's condition: a module with `setUpModule`, `tearDownModule` or module-level state that its classes share stays one job; split only modules whose classes run alone, and a test proves that a split module gives the same pass and fail result as one job. The Work Order is written when be4d46f7 is on main.

Precondition (the sdlc showrunner, 2026-10-30): `production/test_dailies_input.py` `test_style_summary_is_saved_only_for_a_style_production_and_keeps_every_ledger_key` fails only in a full `scripts/production` discover run (`rules_total` 58, expected 1) and passes alone. The likely cause: `style_rules` reads `NATE_STYLE_DIR` at import, and `dailies_input_test_support.py:276` runs `style_ledger` in the same process, so an earlier test's rule catalog stays. Fix it at the cause (read the variable when called, not at import). A class or module split must not hide it.

### Phase 6 — The slowest modules lose their real-time waits  · status: todo
**Depends on:** Phase 4; Phase 2

Source: devtools-unit, 2026-10-30. From Phase 4's CPU measure, each module over 60 s whose time is mostly waiting (low CPU) gets its cause found and fixed in its own test or fixture. First candidates: `direct/test_verify_hold_wait.py` (5 tests, 311 s) and `direct/test_assign_launcher.py` (41 tests, 734 s). Fence (the sdlc showrunner, 2026-10-30): models owns the real-time waits in `scripts/agents/test_codex_mesh.py` and `scripts/agents/test_heartbeat.py` (its hang guard checkpoint). This phase leaves those two files out, unless Phase 4's measure still shows them on top after models' checkpoint merges; then agree the split with models first. The Work Order is written from Phase 4's measure.
