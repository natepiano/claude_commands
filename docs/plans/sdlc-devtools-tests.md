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

### Phase 2 — Every test uses the one hang guard  · status: done
**Depends on:** Phase 1; models' dc80ee4c (`scripts/lib/test_hang_guard.py`, `scripts/agents` on it), on origin/sdlc and merged here at d721ac43

#### As-built

- Every test module and test support module imports its hang limit from `scripts/lib/test_hang_guard.py` (`TEST_HANG_GUARD_SECS = 600`, `LauncherHangGuard`). No local copy or alias stays, `HANG_GUARD_S` included. The change spans every `scripts/` folder except `scripts/agents/`, which already used the guard.
- `scripts/lib/test_one_hang_guard.py` parses every `scripts/**/*.py`. It refuses an assignment to `TEST_HANG_GUARD_SECS` outside the guard module, a module-level alias, and an import of either name from any other module. Each refusal names `path:line`. A second test runs the check on a temporary tree with one copy and expects the refusal.
- A literal timeout under 120 s on a wait for an expected event now uses the guard, because short real-time limits fail under load. A wait that proves an event does not occur in a window stays. A sleep that is the behavior under test stays. A timeout given to product code as its own setting stays.
- The fixture script text in `scripts/production/merge_gate_test_support.py` keeps its `__TEST_HANG_GUARD_SECS__` placeholder, because the fixture repo has no `scripts.lib`. The value put in comes from the imported name. Text in a string is not code, so the check passes it.

**Files:**
- `scripts/lib/test_one_hang_guard.py` — the copy check and its temporary-tree test
- `scripts/**/test_*.py`, `scripts/**/*_test_support.py` — take the guard from the one place
- `docs/as-built/merge-gate-and-test-speed.md` — names the one 600-second guard

**Binds later work:** Every test hang limit comes from `scripts/lib/test_hang_guard.py` and the check refuses a copy. The merge gate finds the check by itself. `scripts/production/test_merge_gate_suites_resources.py` keeps its real CPU workloads and their assertions, so `os.wait4` can tell the workloads apart.

**Gotchas:**
- Assertions on elapsed wall time keep their literal limits. No shared test clock exists.
- Code from a merge can add a copy. The check then goes red in the gate, and the merge must move the copy to the guard.
- A commit message with `Assertion-Change` lines must come from a file (`git commit -F`), because the commit hook reads `-m` text badly.

**Ruled out:** A support module that passes the guard name on: each file imports it directly. `LifecycleTestClock` as the shared clock: it lives only in `scripts/agents/test_codex_mesh.py` and retires with the mesh.

### Phase 2b — A user's bump merges first and gates the pushed tip  · status: done
**Depends on:** none

#### As-built

- `merge_checkpoint.py --bump` is for a user's bump only, and its help text says so. It applies only to a code checkpoint. `user_bump` refuses it with `--shrink`, `--as-built` or `--from-child` before any merge.
- With `--bump`, `run` keeps scope, conflicts, other units, merge, push, records and promotion in their order, but runs no tests before the push. The `test` step reports that the gate runs on the pushed tip in the background, and the merge log line says `gate after push`. A rerun after a stop between merge and push also runs no tests. Without `--bump`, nothing changes: a red test stops the merge.
- After the push and before `promote`, `start_gate` writes a `BumpGateRecord` to `checkpoint_step_record(checkout, merge_head, "bump-gate")`, with `bump-gate.log` beside it. It starts `scripts/production/bump_gate.py --record <path>` in its own session, with stdin closed and output in the log, and does not wait. Promotion runs as for any merge. A start failure is a red result and does not stop promotion.
- The record holds the tip, the gate plan (paths, Merge tests, flakes, flaky tests), the production and unit names, the runner `pid` and `proc_start`, `result` (`pending`, `green`, `red`), `detail`, `log` and `delivered_to`. A rerun for the same tip starts no second gate while the runner lives, reports `held` when liveness is unknown, starts the gate again when the runner died with no result, and sends a completed result only to recipients not in `delivered_to`.
- `bump_gate.py` claims the record under `record_lock`, and stops with no tests when the result is done or a live other runner holds it. It makes a `git clone --shared` of the production checkout in a temporary folder beside the record, detached at the tip, and runs `run_checkpoint_test_steps` there. Flake notes go to the real checkout's production log. The clone goes at the end; the log stays. Any failure, also before the first step, is red.
- `run_checkpoint_test_steps(production, paths, merge_tests, flakes, flaky_tests, log) -> str` is the one copy of the test steps, pass rule, flake rule and rerun of named failures. `checkpoint_tests` and the bump gate both call it. It appends to the log, and the caller owns merge rollback. A red step names its failing tests when known.
- A red result goes to the unit and the showrunner, a green result to the showrunner only, through `scripts/message/send.py` (`MERGE_CHECKPOINT_SEND` overrides it). The text is `Bump gate <red|green>: <unit>; tip <tip>; <detail>; log <log>`. A failed send is written to the log and retried once. Each sent recipient is saved in `delivered_to`.

**Files:**
- `scripts/production/merge_checkpoint.py` — the flag, the path with no tests, `start_gate`, the record and its lock, `run_checkpoint_test_steps`, `complete_gate`, `send_result`, `bump_gate_addresses`.
- `scripts/production/bump_gate.py` — the background gate on the pushed tip.
- `scripts/production/test_merge_checkpoint_bump.py`, `scripts/production/merge_checkpoint_test_support.py` — tests through the real `run` path on a fixture repo with a real origin, and their fixture.
- `commands/showrunner/bump.md` — step 3 runs <MergeCheckpoint/> with `--bump`; step 4 says what the gate does; `bump done` is logged once the tip is green. `commands/showrunner/produce.md` and `docs/as-built/showrunner-automation.md` name the flag.

**Gotchas:**
- `complete_gate` and `send_result` read the record again under `record_lock` and write only when it still names this runner (`same_gate_runner`: tip, pid, process start). An old runner cannot replace a new owner's result or send one.
- Addresses come at send time from `bump_gate_addresses(slug, unit)` (`unit_lookup.marked_unit`, `showrunners.current_address`). The record keeps only slug and unit, so a notice follows a recipient that moved during the run.

**Ruled out:** the gate in the production checkout (a merge may start there), in a copied `.git` pointer (it moves the real branch) or in an export with no git (it fails a shutdown test); a second copy of the test-step, pass or flake code.

### Phase 2c — The assertion guard pairs a moved Rust string continuation  · status: done
**Depends on:** none

#### As-built

- `normalized(text: str, rust: bool) -> str` builds the comparison key. Inside a non-raw Rust string (`"…"`, `b"…"`, `c"…"`), `rust_string_key` drops each `\` + newline + the leading whitespace on the next line, which is Rust's own rule. A Rust assertion that moves and changes indent after a `\` continuation in its message pairs with its old copy and is no finding. A changed word after the `\` is still a finding.
- `rust_string_key` reads escape pairs in order, so an escaped `\\` before a newline is no continuation. Raw strings (`r"…"`, `r#"…"#`, `br#"…"#`, `cr#"…"#`) and every Python literal keep their text as written.
- The `rust` flag is `path.endswith(".rs")` of the file. `DiffLine.key`, `parse_assertion`, `comparison_operands`, `numeric_comparison` and `finding_identity` pass it. Every file uses the Rust lexer (`lexical_spans(text, True)`), as before; the flag decides only whether the continuation rule runs.
- `RUST_LITERAL` reads the `c"…"` and `cr#"…"#` prefixes.
- In `~/rust/cargo-liner`, `assertion_guard.py --base e21d5b93^ --head e21d5b93` prints `assertion guard: 73 blocking`, with the same 73 items as before. Change-review row C437 stays as written.

**Files:**
- `scripts/direct/assertion_guard.py` — `rust_string_key`, the `rust` flag on `normalized` and its callers, the `c`/`cr` prefixes in `RUST_LITERAL`.
- `scripts/direct/test_assertion_guard_strings.py` — tests for a moved continuation (`""`, `b`, `c`), a changed word, escaped backslashes, raw strings, and Python continuations and f-strings.
- `scripts/direct/assertion_guard_test_support.py` — `move_rust_assertion_to_test_file`, which moves a test from `render.rs` to `render/tests/mod.rs` with a new indent.

**Gotchas:**
- `numeric_comparison` passes the file's own flag. Python never gets to numeric pairing, because `numeric_constants` is set only for `.rs` files.

**Ruled out:** the Python lexer for `.py` files in the key (it changes the 6b literal-exact pairing outside this rule); the continuation rule for Python literals.

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

#### As-built

- `reap_gate_job` in `merge_gate.py` reaps a job leader with `os.wait4`. It stores the exit status and `ru_utime + ru_stime` on the job, and `GateJobResult.cpu_seconds` carries the value.
- `reap_gate_job` blocks SIGTERM and SIGHUP (`ROLLBACK_SIGNALS`) with `signal.pthread_sigmask` across `os.wait4` and the stores. An interrupt arrives only after the status and CPU are stored, so it cannot lose a reaped status or stop the cleanup of later jobs.
- At run end, `save_measures` writes `module_cpu_seconds.json` in the gate state dir, under the `<suite>/<module>` keys of `module_seconds.json`; the type-check job uses `types/basedpyright`. A job killed for a limit records nothing.
- After `Largest module peak:`, the report prints `Most CPU: <s> s <suite/module>, …; total <s> s`: the three keys with the most CPU, largest first. The run total adds every attempt, killed and requeued attempts too.

**Files:**
- `scripts/production/merge_gate.py` — `reap_gate_job`, the CPU record, the `Most CPU:` line
- `scripts/production/test_merge_gate_suites_resources.py` — spin and sleep CPU, killed job, report order, interrupt after a reap
- `docs/as-built/merge-gate-and-test-speed.md` — names the record and the report line

**Binds later work:** "The slowest modules lose their real-time waits" ranks modules by wall seconds minus CPU seconds from `module_seconds.json` and `module_cpu_seconds.json`. "The gate runs a large module's test classes as separate jobs" builds on two facts: `reap_gate_job` is the one path that reaps a job and reads its CPU, and one CPU key names one job.

**Gotchas:**
- `systemd-run --scope` execs the test in place, so the leader's rusage holds the test and the children it waited for. A child that `kill_job_group` kills after its leader exits is not counted.
- The signal mask covers the calling thread only. The gate starts no other thread.
- `module_seconds.json` holds the first attempt's wall time, and `module_cpu_seconds.json` holds the last finished attempt's CPU. Compare the two only from one attempt.

### Phase 5 — The gate runs a large module's test classes as separate jobs  · status: done

Dropped 2026-10-30 by its own rule: no module that natedev's condition allows to split would cut the gate's wall time. From `module_history.json` (last five runs) and each module's classes: the slowest module, `direct/test_assign_launcher.py` (413-1204 s), has one class and `setUpModule`; `agents/test_codex_mesh.py` has `setUpModule`; the modules with more than one class and no module setup all ran under 60 s last time (`hana_shot/test_hana_shot.py` 59 s, 12 classes; `agents/test_agents_conf_publish.py` 46 s, 3 classes; `herdr/test_herdr.py` 41 s, 3 classes). The wall-time lever is Phase 6: the waits in `direct/test_assign_launcher.py`.

### Phase 6 — One agents.conf parse per resolve  · status: done

#### As-built

- `_agents_registry_read` in `scripts/agents/agents_config.sh` parses agents.conf once per top-level reader call. It builds one in-shell table of sections and trimmed `key=value` rows (`_agents_table_*` locals) from `agents_registry_text`, which stays the one read point. Every registry reader uses this table. Nested readers and command substitutions reuse it.
- One resolve reads the registry once, not 9 times. `direct/test_assign_launcher.py` takes 300 s, down from 545 s on the same machine.
- `agents_config_trim <value> [<out var>]` trims in the same shell when it gets an output variable. No line gets its own subshell.
- `agents_registry_edit` and `agents_registry_store` mark the table stale. A changed `AGENTS_CONFIG_FILE` or `AGENTS_REGISTRY_BASE` makes the next read parse again. The table does not live across separate `bash -c` calls.
- Every reader gives the same outputs, errors and exit codes as before, the `codex_mesh.py` callers included. The agents.conf format is unchanged.

**Files:**
- `scripts/agents/agents_config.sh` — the one parse, the table and its readers
- `scripts/agents/test_agents_config.sh` — one-read-per-resolve, edit refresh and file-switch tests, and the pinned reader results

**Binds later work:** the codex_mesh queue grace change measures `direct/test_assign_launcher.py` from 300 s.

**Gotchas:**
- A write to agents.conf that does not go through `agents_registry_store` is not seen until the next top-level call. Each call reads one snapshot.
- The merge gate runs only `test_*.py` modules. Run `bash scripts/agents/test_agents_config.sh` by hand from the worktree root.

**Ruled out:** a table refresh on outside writes during one call — every writer uses the store path.

### Phase 6b — The queue grace reaches child processes in tests  · status: done
**Depends on:** Phase 6; models' compaction repair 2 checkpoint (met: 99d7b894)

Source: devtools-unit diagnosis, 2026-10-30; models-unit's go, 2026-10-30. `QUEUE_GRACE_SECS = 3.0` (`scripts/agents/codex_mesh.py:306`, used at about line 2978) makes `follow` wait 3 s after each turn. `MeshCommandFixture` patches it to 0.0 in the test process (`scripts/agents/test_codex_mesh.py:831`), but `direct/test_assign_launcher.py` runs codex_mesh as a child process, so the child still waits: 3 s per dispatch. Goal: codex_mesh reads the grace from one env var at use (default 3.0), and `MeshCommandFixture` sets that env var where it patches the constant now. Build on models' checkpoint, which rewrites much of both files; send models the diff for those two files before the merge. The Work Order is written when the hash arrives.

From Phase 6: the agents.conf parse fix took `direct/test_assign_launcher.py` from 545 s to 300 s (42 tests, natedev, 2026-10-30). 300 s is the before figure for this phase's timing.

**From the Phase 7 plan review (architect, 2026-10-31):** The models dependency is met: 99d7b894 is in this branch through d4749972. Current references: the grace use is at `scripts/agents/codex_mesh.py:3071`, the fixture patch at `scripts/agents/test_codex_mesh.py:927`. Keep the positive-grace tests at `test_codex_mesh.py` lines 10212-10314: an inherited 0 in the environment must not turn off their single-window assertions. Add child-process coverage and a fresh timing baseline for `direct/test_assign_launcher.py`. **Workers:** 1 writer + 1 tester. impl: `scripts/agents/codex_mesh.py`. test: `scripts/agents/test_codex_mesh.py`, `scripts/direct/test_assign_launcher.py`, through the shared fixture environment.

#### Work Order

**Goal:** a codex_mesh child process that a test starts uses the test's queue grace, so `direct/test_assign_launcher.py` no longer waits 3 s after each dispatch, and production keeps its 3 s grace.

**Spec:**
- `codex_mesh.py` reads the queue grace from the env var `CODEX_MESH_QUEUE_GRACE_SECS` at the point of use (`_more_work_coming`, about line 3071), through one small reader function. Unset or empty gives the default, 3.0, named once in the module where `QUEUE_GRACE_SECS` is now. A value that is not a finite number of 0 or more is an error that names the variable; it never falls back in silence.
- `QUEUE_GRACE_SECS` stays only as the default's name; no code reads it as the live value, and no second copy of the default exists.
- `MeshCommandFixture` (`test_codex_mesh.py` about line 927) sets `CODEX_MESH_QUEUE_GRACE_SECS=0` in `os.environ`, through its existing `patch.dict`, where it patches the constant now. A child process the test starts inherits it (`test_assign_launcher.py` passes `os.environ.copy()` at line 133).
- The positive-grace tests (`test_codex_mesh.py` about lines 10212-10314, `SingleGraceWindow`) keep their single-window assertions. They set their own grace, so the fixture's inherited 0 does not turn them off.
- New coverage: (a) a child process started under the fixture uses grace 0 (it fails on the old code, where the child uses 3.0); (b) unset gives 3.0; (c) an invalid value is refused with the variable's name.
- Base: models-unit's F024 checkpoint 3b67af71 (origin/natedev-models) also changes `codex_mesh.py`. Before the checkpoint, the director proves a clean merge of this phase with it (`git merge-tree`) and sends models the diff of the two mesh files.

**Files:**
- `scripts/agents/codex_mesh.py` — the grace reader and its one use
- `scripts/agents/test_codex_mesh.py` — the fixture's env var, the positive-grace tests, the new reader tests
- `scripts/direct/test_assign_launcher.py` — the child-process grace test, if it fits there better than in the mesh module

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/agents/codex_mesh.py`
- `test` — `scripts/agents/test_codex_mesh.py` and `scripts/direct/test_assign_launcher.py`, from this Spec alone

**State:** none

**Constraints from prior phases:** Phase 6's agents.conf parse fix took `direct/test_assign_launcher.py` from 545 s to 300 s. The fresh baseline is the same module on 91080cc7, measured by the director before dispatch.

**Acceptance gate:** `python3 -m unittest scripts.agents.test_codex_mesh` and `python3 -m unittest scripts.direct.test_assign_launcher` green from the worktree root, with the new child-process test failing on the old code; `basedpyright` on each slot's files `0 errors, 0 warnings, 0 notes`; the `test_assign_launcher` wall time against the 91080cc7 baseline goes in the checkpoint notice; `git merge-tree` of the phase with 3b67af71 has no conflict.

### Phase 7 — The assertion guard keeps the branch's own base after a merge of the merge branch  · status: done

#### As-built

- `commit_bases(repo, selected_bases=("HEAD",), *, head="", staged=True)` in `scripts/direct/assertion_guard.py` is the one base selector. With no `--base` and no `VERIFY_ASSERTION_BASE`, it starts from every distinct one of the upstream merge-base and the production merge-base (the nearest-remote fallback and its ambiguity refusal stay). It adds merge parents in `base..target` and `MERGE_HEAD`, then drops every base another base descends from (`git merge-base --independent`). An explicit `--base` or `VERIFY_ASSERTION_BASE` keeps its list and order. `default_base` is gone.
- `run` has one comparison path in every mode: findings against each base, with one shared `RevisionSourceCache`, and their owner-free intersection (`finding_identity` = path, kind, normalized old, normalized new; `intersect_findings`).
- After a merge of the merge branch, the merge's trunk commits no longer count against the upstream base. Inspectors HEAD 03fafc1d6c with no `--base` gives 1 blocking (6 before); `--head HEAD` is clean. In `~/rust/cargo-liner`, `--base e21d5b93^ --head e21d5b93` still prints `assertion guard: 73 blocking`.
- `scripts/direct/tree_watch.py` keys the lint cache on the bases `commit_bases` returns.

**Files:**
- `scripts/direct/assertion_guard.py` — `commit_bases`, the per-base intersection in `run`
- `scripts/direct/tree_watch.py` — the lint cache key from `commit_bases`
- `scripts/direct/test_assertion_guard_revisions.py`, `scripts/direct/assertion_guard_test_support.py` — merge-of-the-merge-branch, rename-merge and diverged-base tests and their shared fixture steps
- `scripts/direct/verify_assertion_guard_test_support.py` — the one fake guard both verifier fixtures use; it exposes `commit_bases`

**Binds later work:** `commit_bases` stays the one selector, and the comparison has no second path; the combined-history guard work builds on both.

**Gotchas:** the intersection hides a branch-made removal of an assertion whose text differs between bases, committed or local (merge-changed text). The rename-merge regression `test_local_removal_after_production_test_rename_is_counted_once` pins the single count.

**Ruled out:** choosing one base by the fewer `base..HEAD` commits (a merge's trunk commits count against the upstream base); a HEAD-to-tree supplement beside the intersection (it counted one removal twice after a production rename merge, and each reconciliation of it opened new owner or grouping edges).

### Phase 7b — The assertion guard compares against the combined history of its bases  · status: todo
**Depends on:** Phase 7

Source: Phase 7 repair round 4 (landing rule; showrunner ruling 2026-10-31). Astra review `/tmp/claude/astra/20261031T113652Z-318765/review.md` items 1-3. Phase 7's per-base intersection hides a branch-made removal of an assertion whose text differs per base. Distinguishing test (clean tree, no conflicts, one test name): fork with `self.assertEqual(value, 1)`; pushed U changes it to `2`; P changes another file; merge U and P and replace the assertion with `pass`; add an unrelated commit. With no `--base` and `--head HEAD`, the guard must give exactly one blocking removal of `self.assertEqual(value, 2)`. The same hole covers a working-tree or index removal of an assertion whose text the merge changed (Phase 7 round 4 tried a HEAD-to-tree supplement; it double-counted after a rename merge, and Astra review `/tmp/claude/astra/20261031T121403Z-1704209/review.md` lists the owner, replacement and grouping edges a fix must handle): the guard must give exactly one blocking removal with HEAD's spelling, and its count on the tree or index must never exceed the `--base HEAD` count for local changes. Design inputs: Phase 7 round 3's merge-tree fold (saved in the Phase 7 run folder as `round3_wip/assertion_guard.py`), and that its whole-path conflict fallback reports imported removals. natedev-verify's explicit multi-`--base` tests require intersection semantics: the Work Order must name each one it changes. The Work Order is written when the phase starts.

**From the Phase 7 plan review (architect, 2026-10-31):** The count limit covers only duplicate representations of local changes: a committed removal plus an unrelated local removal stays 2 blocking. Acceptance adds a distant header conflict where production already removed one assertion and the branch removes another (imported excluded, branch kept), and the same in the pending-merge and index case. The printed owner is HEAD's current test name; a ruling clears exactly that occurrence, and an identical assertion in another test stays blocking. Constraints: `commit_bases` stays the one selector; the commit hook keeps its HEAD/index and pending-merge behavior; explicit and VERIFY_ASSERTION_BASE bases keep their list and order; the shared revision cache and the CPU test (`test_large_diff_guard_call_uses_less_than_one_cpu_second`) stay; the lint cache-key test proves a changed comparison input invalidates a cached green lint. **Workers:** 1 writer + 1 tester. impl: `scripts/direct/assertion_guard.py`, `scripts/direct/tree_watch.py`. test: `test_assertion_guard_revisions.py`, `assertion_guard_test_support.py`, `test_verify_lint_guard_key.py`, `verify_assertion_guard_test_support.py`.
