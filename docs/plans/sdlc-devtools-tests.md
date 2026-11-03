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
  - Preserve Phase 7d recovery snapshots and their Git refs.
  - Restore an attempt only when its paths overlap the requested paths.
  - Validate every original path before restoring any path in that attempt.
  - Preserve later index and worktree edits.
  - Retain attempt JSON records after permanent restore closure.
  - Remove attempt recovery refs only after permanent restore closure.
  - Acquire the gate-record lock before `main-push.lock`.
  - Release the gate-record lock before local promotion.
  - Hold `main-push.lock` through the last promotion push to main.
  - Run Mac pulls and notice delivery after releasing `main-push.lock`.
  - Record publication even when promotion fails.
  - Mark a style notice complete only after successful delivery.
  - Preserve silent, idempotent completion of repeated handoffs.
- **Config reads (Phase 4r).** A pass calls `conf.read(file)` once and gives the `ConfigRead` to its helpers. A helper default is `UseConfiguredValue.CURRENT`, never `None`. The module-level readers (`conf.text`, `conf.number`, `conf.whole`, `conf.words`, `conf.values`, `conf.value_origin`) are gone. A faulty setting prints the same stderr warning when the pass uses it. Keep the `scripts/lib/test_conf.py` tests for read counts, edits between reads, deferred recovery, unchanged warnings and refusal without usable history.
- **Test runs.** A writer runs only the few test files its prompt names. The director runs the one merged-tree gate. A test double that replaces a function whose signature changed must take the new arguments: a rejected argument can fail silently inside a caught path.
- **Ownership.** The staff row owns a unit's location. Git owns its branch. `.unit.toml` owns unit settings. Each command takes one identity snapshot and joins through it. An interrupted operation leaves state a rerun completes; a completed rerun changes nothing.

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

#### As-built

- `queue_grace_secs()` in `scripts/agents/codex_mesh.py` reads `CODEX_MESH_QUEUE_GRACE_SECS` at each use in `_more_work_coming`. Unset or empty gives `QUEUE_GRACE_SECS` (3.0), the one default. A value that is not a finite number of 0 or more raises `SystemExit` with the variable's name; it never falls back in silence.
- `MeshCommandFixture` sets `CODEX_MESH_QUEUE_GRACE_SECS=0` through its `patch.dict` and does not patch the constant. A codex_mesh child process that a test starts inherits the 0.
- `direct/test_assign_launcher.py` takes 122 s wall, down from 326 s on natedev (43 tests). Production keeps its 3 s grace.

**Files:**
- `scripts/agents/codex_mesh.py` — `queue_grace_secs()` and its one use
- `scripts/agents/test_codex_mesh.py` — the fixture's env var; `QueueGraceSettingsTests` (unset, empty, changed and invalid values); `MeshCommandTests.test_child_inherits_zero_queue_grace_without_waiting_for_a_frame`; the positive-grace test in `FollowEventRegressionTests` sets its own 0.05 grace

**Gotchas:** a test that needs a positive grace sets the env var itself. Otherwise the fixture's inherited 0 turns the grace off.

**Ruled out:** a fixture patch of the module constant — a child process never sees it.

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

#### Work Order

**Goal:** with no explicit base, the assertion guard compares HEAD, the index or the working tree against one combined base, the merge of its bases, so a branch-made removal of an assertion whose text the merge changed counts once, with HEAD's spelling, and nothing the bases already removed or changed counts at all.

**Spec:**
- Combined base. When `commit_bases` returns more than one base and none was given by `--base` or `VERIFY_ASSERTION_BASE`, `run` builds one combined tree with `git merge-tree --write-tree` over those bases (folding them in order; the Phase 7 round 3 fold in `/tmp/claude/unit/fe59e5e3-0e2d-4361-a843-3881414f5f2e/phases/7/round3_wip/assertion_guard.py` is a design input) and compares the target against that tree in one pass. One base needs no fold. `commit_bases` stays the one selector.
- Conflicts. A path the fold reports as conflicted is compared against each base and intersected for that path only (Phase 7's `intersect_findings`), never by a whole-path fallback that reports imported removals. A distant header conflict where production removed one assertion and the branch removes another gives the branch's removal only, for commit, index and pending-merge judgments.
- Local changes. Compare the index and working tree against the combined tree.
  Use no HEAD-to-tree supplement.
  Count each local removal once.
  Apply the local count limit after aggregation and rulings.
  Apply that limit only to matching local occurrences.
  Keep committed removals in the automatic total.
  A committed removal and a distinct local removal produce two blockers.
- Owners and rulings. The printed owner is HEAD's current test name. A ruling clears exactly that occurrence; an identical assertion in another test stays blocking (identical assertions in `test_a` and `test_b`, `test_a`'s removed in a commit and `test_b`'s locally, a ruling for `test_b`: `test_a`'s removal stays blocking). Apply aggregation and rulings before checking duplicate local occurrences.
  Do not cap the automatic total at the total from `--base HEAD`.
- Explicit bases. `--base` and `VERIFY_ASSERTION_BASE` keep their list, their order and Phase 7's per-base intersection. Name in the summary every natedev-verify multi-`--base` test whose expectation changes; the aim is none.
- Keep: the commit hook's HEAD/index and pending-merge behavior, the shared `RevisionSourceCache`, `test_large_diff_guard_call_uses_less_than_one_cpu_second`, and the lint cache key in `tree_watch.py` (a changed comparison input, the combined tree included, invalidates a cached green lint).
- New regressions, each red on 4b353c42: (a) the plan's distinguishing test above (fork `assertEqual(value, 1)`; pushed U changes it to `2`; P changes another file; merge U and P, replace the assertion with `pass`; add an unrelated commit; no `--base`, `--head HEAD` gives exactly one blocking removal of `self.assertEqual(value, 2)`); (b) the same removal made in the index and in the working tree instead, one blocker with HEAD's spelling each; (c) production renames `test_old` to `test_new`, the merge weakens `assertEqual(value, 1)` to `assertTrue(value)`, then a local removal: one blocker; (d) the header-conflict case; (e) the `test_a`/`test_b` ruling case; (f) a base test with two assertions whose signature HEAD changes, deleted in the tree: one grouped `test removed` blocker. Keep `test_local_removal_after_production_test_rename_is_counted_once` and the changed-spelling regression near `test_assertion_guard_revisions.py:1185` (one removal with HEAD's spelling in both modes).

Assertion-Change: scripts/direct/test_assertion_guard_revisions.py::test_local_removal_after_production_test_rename_is_counted_once — with one combined base the automatic comparison reports HEAD's name; the expected owner changes from `test_old` to `test_new` (`:1128`), and the single-blocker assertion stays.

**Files:**
- `scripts/direct/assertion_guard.py` — the combined base, the per-path conflict intersection, local judgments against the combined tree
- `scripts/direct/tree_watch.py` — the lint cache key carries the combined comparison input
- `scripts/direct/test_assertion_guard_revisions.py` — regressions (a)-(f)
- `scripts/direct/assertion_guard_test_support.py` — shared fixture steps for those histories
- `scripts/direct/test_verify_lint_guard_key.py` — the cache key test for the combined input
- `scripts/direct/verify_assertion_guard_test_support.py` — the one fake guard, if its surface changes

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/assertion_guard.py`, `scripts/direct/tree_watch.py`
- `test` — `scripts/direct/test_assertion_guard_revisions.py`, `scripts/direct/assertion_guard_test_support.py`, `scripts/direct/test_verify_lint_guard_key.py`, `scripts/direct/verify_assertion_guard_test_support.py`, from this Spec alone

**State:** none

**Constraints from prior phases:** Phase 7 As-built above (one selector, one comparison path, owner-free identity). Phase 7 round 4's HEAD supplement double-counted after a rename merge; Astra reviews `/tmp/claude/astra/20261031T113652Z-318765/review.md` and `/tmp/claude/astra/20261031T121403Z-1704209/review.md` list the owner, replacement and grouping edges. Inspectors HEAD 03fafc1d6c with no `--base` gives 1 blocking; `~/rust/cargo-liner` `--base e21d5b93^ --head e21d5b93` gives `assertion guard: 73 blocking`; both must hold.

**Acceptance gate:** each of `test_assertion_guard.py`, `test_assertion_guard_revisions.py`, `test_assertion_guard_strings.py`, `test_verify_assertion_guard.py`, `test_verify_assertion_guard_fake.py` and `test_verify_lint_guard_key.py` green in its own run (`python3 -m unittest discover -s scripts/direct -p <file>`), with (a)-(f) red on 4b353c42; the full merged-tree gate (`merge_gate.py --tests-only`) green before the checkpoint; `basedpyright` on each slot's files `0 errors, 0 warnings, 0 notes`; the director reruns the Inspectors and cargo-liner figures above and puts them in the checkpoint notice.
Add a regression with one committed removal and one distinct local removal.
The automatic comparison reports two blockers.
The `--base HEAD` comparison reports one blocker.
A ruling for the local occurrence leaves the committed occurrence blocking.

### Phase 7d — Small fixes fast-track to main  · status: done
**Depends on:** Phase 8 (the one config reader; 7d lands after it and reads through it)

#### As-built

- `~/.claude/scripts/lib/py ~/.claude/scripts/production/fast_track.py --unit <unit> --production <production doc> --why "<one line>" <path>...` takes the named paths' changes against HEAD (new files included), applies them with `git apply --3way` in a `git clone --shared` copy of origin/main, and runs the fast check there under `check_time_limit_seconds`: each changed Python file's direct test modules in `merge_gate.test_module_environment`, a package import of each changed module, and `basedpyright` (footer only). No merge gate, build queue or memory queue runs. It commits `fast-track(<unit>): <why>` with trailer `Fast-Track: <unit>` and pushes main fast-forward only under `main-push.lock`; a moved main re-applies and re-checks, at most `max_push_attempts` times.
- Refusals exit 2 with nothing pushed: no change, a path outside the repo, a `refused_paths` match, a Python file that imports a refused repo module (directly or through repo modules), a limit exceeded, a conflict with main, or a key `conf.py` cannot supply (`conf.Unusable`).
- After the push it promotes through the production doc's `Promote:` targets with `merge_checkpoint.promote_targets(promotion, "main", commit, ...)`, restores the named worktree paths to HEAD (new files removed) and appends a `review: pending` row to `${FAST_TRACK_STATE_DIR:-~/.local/state/fast-track}/records.jsonl`. A promotion failure keeps the push and exits 1. A rerun prints `already on main at <commit>`, promotes again and exits 0.
- Recovery: each attempt keeps a `PushAttempt` JSON record and refs under `refs/fast-track/{selections,attempts}/`, which protect the original trees from GC. Restore runs only for an attempt whose paths overlap the request (equal, or one contains the other), validates every original path before any restore, and keeps later index and worktree edits; an unrelated pending attempt prints a warning. `--close-restore <attempt>` retires automatic restore permanently: the JSON record stays and the refs go. HEAD that contains the pushed commit closes the restore.
- `--pending --unit <unit>` prints pending fast-track diffs (review input, not recovery status). `scripts/direct/review.sh` adds them to every broad review prompt for the unit and marks the rows `reviewed` with the pass. The next merge gate tests the fix when merge branches take main. The nightly full suite (natedev `claude-nightly.timer`, 03:00, `claude-nightly.service` on origin/main) messages the sdlc showrunner on red.
- `merge_checkpoint.main_push_lock` is the one lock helper. Lock order: the gate-record lock, then `main-push.lock`; the gate-record lock is released before local promotion; `main-push.lock` holds through the last promotion push to main; Mac pulls and notice delivery run after its release. Publication is recorded even when promotion fails; a style notice is marked complete only after delivery, so a failed delivery retries; a repeated hand-off completes silently. The measured lock span in the checkpoint suite is 0.05 s.
- `implement.sh` and `review.sh` read `UNIT_COMPACT_ABOVE_TOKENS` through `conf_value`; `UNIT_CONFIG` no longer exists. `commands/unit/fast_track.md` defines `<FastTrack/>`; `direct.md` CoreContract, `production_format.md` ProductionUnit item 4 and `produce.md` name it as the one exception.
- Live proof (2026-11-02): one real fix pushed and promoted on natedev and the Mac in 6.5 s; the same command then printed `already on main` in 2.6 s with main unchanged. The concurrent case (a fast fix during a real `merge_checkpoint`, its promotion still finishing) stays with the director in this phase.

**Files:**
- `scripts/production/fast_track.py` — the command, recovery, `--close-restore` and `--pending`
- `config/fast_track.conf` — `max_changed_lines` 100, `max_files` 5, `check_time_limit_seconds` 120, `max_push_attempts` 3, `refused_paths` (settings, hooks, `scripts/lib/py`, staff writers, account move/rename/shutdown scripts, `merge_gate.py`, `merge_checkpoint.py`, `verify.sh`, `build_hold.py`, `fast_track.py`, this file)
- `scripts/production/merge_checkpoint.py` — `main_push_lock`, `promote_targets`, publication and notice records
- `scripts/direct/review.sh`, `scripts/direct/implement.sh` — pending diffs in broad reviews; conf reads
- `commands/unit/fast_track.md`, `commands/unit/direct.md`, `docs/production_format.md`, `commands/showrunner/produce.md` — the one exception
- `scripts/production/test_fast_track.py`, `test_merge_checkpoint_promotion.py`, `test_merge_checkpoint_bump.py`, `test_merge_checkpoint_validation.py`, `merge_checkpoint_test_support.py` — command, lock, promotion and notice tests
- `scripts/direct/test_review_launcher.py`, `review_launcher_test_support.py`, `implement_launcher_test_support.py`, `test_assign_launcher.py`, `test_implement_launcher_compaction.py` — launcher tests without `UNIT_CONFIG`

**Binds later work:** the Delegation Context invariants hold the recovery and lock-order rules; a change to `fast_track.py` or `merge_checkpoint.py` goes through the normal phase path, never through the fast track.

**Gotchas:** `notify_style` returns bool; a test wrapper that patches it returns the value, or basedpyright reports the unused result. The fast check's empty-suite exception applies only to discovery; an import run that finds 0 tests fails. basedpyright exits 3 when the configured `.venv` is absent; only the footer counts.

**Ruled out:** `merge_gate.py --promote-only --merge-branch main` (it refuses a local main behind origin/main); a worktree or a copied `.git` pointer for the copy; a full-suite fallback in the fast check.

### Phase 7l — A fast fix during a checkpoint merge exits 0  · status: done

#### As-built

- `fetch_main(repo)` in `scripts/production/fast_track.py` fetches origin main in the director's repository. `reconcile` and `push_patch` call it. The fetches in the temporary copy use plain `git()`; the copy has its own refs.
- When a fetch fails and its stderr contains `cannot lock ref`, `fetch_main` waits `fetch_lock_wait_seconds` and fetches again, at most `fetch_lock_attempts` fetches in total. Any other failure, and a lock failure on the last attempt, raises `Refused` with the current text. A failure before the push exits 2; a failure after the push exits 1.
- `run_git()` runs git with literal pathspecs and returns the `CompletedProcess`; `git()` and `fetch_main()` both call it.
- `config/fast_track.conf` holds `fetch_lock_attempts = 3` (`# whole: 1..`) and `fetch_lock_wait_seconds = 0.5` (`# range: 0..`). `fetch_main` reads them at use through `scripts/lib/conf.py`; the first call comes before any push, so a missing or invalid key refuses with exit 2.

**Files:**
- `scripts/production/fast_track.py` — `fetch_main`, `run_git`
- `config/fast_track.conf` — the two retry keys
- `commands/unit/fast_track.md` — names the two keys
- `scripts/production/test_fast_track.py` — lock regressions that hold a real `refs/remotes/origin/main.lock` while real git fetches: before the push, after the push, exhausted attempts, other failures, invalid keys

**Gotchas:** The test fixture `save_config()` copies the real conf and drops keys its settings do not name; a dropped key takes its limit comment with it, or the comment applies to the next key. A conf change that lands before its code must pass the old tests on that exact tree.

**Ruled out:** reading the retry keys through `FastTrackLimits`/`limits()` — keys are read at use, and the first fetch precedes the push.

### Phase 7e — Every process-group kill checks its pid  · status: done

#### As-built

- `check_process_group(pid: object) -> int` in `scripts/lib/process_group.py` raises `ValueError` for non-`int` values, including bools and mocks, ids <= 1, and `os.getpgrp()`; `send_process_group(pid: object, signum: int) -> None` checks before the sole raw `os.killpg`, leaving `ProcessLookupError` handling to callers.
- Python callers and signal fixtures use the shared sender; shell callers use their own tree's `process_group.py PID SIGNAL` through `scripts/lib/py` (exit 3 means missing group; exit 2 means refusal or failure).
- `unguarded_group_signals(scripts: Path) -> list[str]` in `scripts/lib/test_process_group.py` checks Python AST references and embedded/shell text for raw sends, allowing only the shared send.

**Files:**
- `scripts/lib/process_group.py`, `scripts/lib/test_process_group.py` — validation, send, CLI, refusal tests and source guard
- `scripts/direct/verify.sh`, `scripts/direct/board.sh`, `scripts/production/smoke_launch.sh` — CLI callers; Python callers and signal test/support files share the send boundary

**Binds later work:** “The group-kill guard reads commands, not text” extends `unguarded_group_signals` in `scripts/lib/test_process_group.py`; `process_group.py` remains the sole raw-send exception.

**Gotchas:** `MagicMock.__index__` returns 1; refusal tests patch `os.killpg`, and real test signals target only owned children started with `start_new_session=True`. Embedded text checks still miss some aliases and shell command positions.

### Phase 7f — A damaged design-role record names itself and stops no other unit  · status: done

#### As-built

- `open_role` and `adopt` resolve an omitted phase-binding repository to the authority's checkout; `_save` validates the complete record with `_decode_design` before writing and refuses an empty `binding.repository`.
- `rebind <id> --unbind` or `rebind <id> --checkout <path> --run-directory <dir> --phase <id>` repairs an otherwise valid empty-repository binding under the lock and generation check, retains omitted output paths, and validates the target before server release or generation advance.
- `DamagedDesignRecord(RoleRefused)` carries `path`, `field` and `holder`; strict reads wrap it in `WorkerStateUnreadable`, and CLI refusals name the path and `binding.repository`.
- `_read_designs` skips unrelated damaged records with `warning: <path>: <reason>` once per `record_operation`; `end_holder` and `move_holder` refuse matching damage, while `holder_failures` reports matching damage with live roles.
- `_finish_rebind` publishes launch context and role projections before removing `transition.json`; a completed identical retry writes nothing and prints `<id> rebound at generation <N>`.

**Files:**
- `scripts/astra/design_roles.py` — binding validation, repair, named errors and holder-aware scans
- `scripts/astra/test_design_roles.py`, `scripts/production/test_retire_unit.py` — repair, interruption, scan and retirement regressions

**Binds later work:** “Run cleanup and the role list skip another unit's damaged record” uses `_read_designs`, `unbind_run()` and `list_roles()` with the named stderr diagnostic and addresses markerless `rebind` publication retries.

**Gotchas:** The markerless identical-retry shortcut checks launch context before returning; matching context alone does not prove census publication finished. Scan warnings use the decoded reason (`missing or invalid repository` for an empty binding repository), while strict CLI refusals include `binding.repository`.

### Phase 7g — Run cleanup and the role list skip another unit's damaged record  · status: todo
**Depends on:** Phase 7f (same file; it checkpoints first)

Source: Phase 7f already skips unrelated damaged records through `_read_designs`.
Remaining defects affect matching damage, list diagnostics, markerless publication retries, and domain types.

#### Work Order

**Goal:** a run's cleanup unbind and the role list are not stopped by another unit's damaged record.

**Spec:**
1. Preserve the holder-aware scan and its single named warning for each damaged record.
2. Refuse matching damaged records during `unbind-run`, including records with ended or unopened roles.
3. Print plain-text list diagnostics on stderr.
4. Keep plain-text stdout limited to valid roles or `no held roles`.
5. Preserve the structured `roles` and `problems` fields from `list_roles()` and `list --json`.
6. Keep a repeated successful `unbind-run` free of writes.
7. Complete census publication before a markerless repair retry reports success.
8. Preserve the generation and lifecycle effects during publication retries.
9. Replace optional holder and binding arguments with explicit keep/change types.
10. Convert omitted CLI arguments to `KeepHolder` and `KeepBinding` at the CLI boundary.
11. Represent explicit unbinding as `ChangeBinding(DesignUnbound())`.
12. Replace `held_document()` absence with `NoHeldDocument`.
13. Preserve `HeldDesign` as the successful lookup result.
14. Update `astra_review.py` to select held and standalone reviews through those result types.

Assertion-Change: scripts/astra/test_design_roles.py::test_list_prints_empty_inventory_and_record_problems
— replace the two-line stdout expectation with exactly `no held roles\n`.
Move the damaged-record path and nonempty-reason assertions to stderr.
Require exactly one warning line.
Keep the JSON inventory and problem assertions unchanged.

**Files:**
- `scripts/astra/design_roles.py` — cleanup, diagnostics, publication retries, and semantic domain results
- `scripts/astra/astra_review.py` — the held-document lookup consumer
- `scripts/astra/test_design_roles.py` — domain and CLI regressions
- `scripts/astra/test_astra_review.py` — held and standalone review selection

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/astra/design_roles.py`, `scripts/astra/astra_review.py`
- `test` — `scripts/astra/test_design_roles.py`, `scripts/astra/test_astra_review.py`, from this Spec alone

**State:** design-role records (tests use a temporary state root only).

**Constraints from prior phases:** `unbind_run` (`scripts/astra/design_roles.py:1480`) already reads through `_read_designs` and has tests that skip another unit's damage; this phase keeps that, refuses matching damage also when no role is live, fixes the `list` diagnostics, and completes markerless publication (the early return at `design_roles.py:981` remains). `rebind` (`:949`) takes `Holder | None` and `Binding | None`, where absence means "keep the current value" and also decides whether damaged-record repair is allowed: replace both with explicit keep/change types (convert the argument parser's absence at the CLI boundary) and test that keeping a binding differs from an explicit unbind. Acceptance adds: matching damage with ended or unopened roles is refused; a markerless retry that publishes the context, fails the census publication, then retries again ends published.

**Acceptance gate:** tests with a temporary state root and a planted damaged record of another unit show that `unbind-run` unbinds this run's records, prints one named stderr line, and exits 0; a rerun writes nothing; `list` prints the named line on stderr and nothing about the record on stdout. Also: matching damage with no live role refuses by name; the markerless retry publishes the census before it reports success; the CLI cases for `KeepHolder`/`ChangeHolder` and `KeepBinding`/`ChangeBinding` stay green. Each changed test file runs green in its own `python3 -m unittest discover -s <its folder> -p <file>` run, and the full merged-tree gate (`merge_gate.py --tests-only`) is green before the checkpoint.
Test omitted holder and binding arguments separately from explicit changes.
Test explicit unbinding and damaged-binding repair.
Test a markerless retry that saves context, fails census publication, and then completes census publication.
Test `NoHeldDocument` and `HeldDesign` results through the review caller.
Run `python3 -m unittest scripts.astra.test_design_roles scripts.astra.test_astra_review`.
Run `basedpyright` on all four owned Python files.
Require `0 errors, 0 warnings, 0 notes`.

### Phase 7h — The group-kill guard reads commands, not text  · status: todo
**Depends on:** Phase 7e (same test file; it checkpoints first)

Source: Phase 7e already added Python AST checks and shell tokenization.
Remaining gaps include shell command positions, executable Python aliases, and shell parse failures.

#### Work Order

**Goal:** the guard finds every group signal outside `process_group.py` by the meaning of the code, not by its spelling.

**Spec:**
1. Python files and Python text inside other files are parsed with `ast`: any reference to `killpg` (attribute or name, through any alias or assignment) outside `scripts/lib/process_group.py` fails the guard.
2. Shell text is split into commands with a real shell tokenizer (`shlex` in punctuation mode), so a `kill` in any command position (after `;`, `&&`, `||`, `then`, `else`, `do`, a pipe, or inside `bash -c '…'`) is found; a target of `0`, `-0`, or a negative group fails the guard unless the command is the process_group CLI.
3. Parse complete executable units before treating a parse failure as unsafe.
   Report an unparseable executable unit with its path.
   Preserve valid multiline commands and literal fixture data.
4. Add planted cases for an `else` command and a Python heredoc assignment alias.
   Add malformed executable shell text.
   Keep the existing direct, imported-alias, zero-group, negative-group, nested-shell, continuation, and shared-sender cases.
5. Extend the existing AST and `shlex` implementation.
   Distinguish executable fragments from literal fixture data.
   Keep the sole raw-send exception inside `send_process_group`.
   Send no real signal in these tests.

**Files:**
- `scripts/lib/test_process_group.py` — the guard and its planted cases

**Workers:** 1 writer
- `test` — `test_process_group.py`

**State:** none (a temporary tree only; no signal is sent, `os.killpg` stays patched).

**Constraints from prior phases:** the guard exempts only the one send inside `send_process_group` in `scripts/lib/process_group.py`, not every reference in that file; its planted fixture already rejects a second raw call there, and that stays. Keep the shell-helper failure and fallback tests. The caller baseline is da15fa97 (codex_mesh, signal_test_support, herdr_upkeep and repeat_guard all route through `send_process_group`). Plain Python AST detection and several zero and nested-shell cases already exist; a read-only probe shows the shell `else` branch case still escapes.

**Acceptance gate:** `python3 -m unittest discover -s scripts/lib -p test_process_group.py` passes on the real tree, and every planted case named in the Spec fails the guard; the full merged-tree gate (`merge_gate.py --tests-only`) is green before the checkpoint.

### Phase 7i — Retire keeps the work, and resume brings it back  · status: done

#### As-built

- `retire_unit.py` saves a branch with commits the merge branch lacks: it pushes the tip to origin `refs/retired/<staff id>`, confirms the exact tip with `ls-remote`, rechecks the tip after every writer stops and again before removal, and records `, ref <ref>, tip <tip>` in the retired note. Uncommitted work still refuses.
- `resume_unit.py` fetches the saved ref, restores the branch and worktree at that tip, confirms HEAD, then deletes the ref with an exact-tip lease. A retry accepts a branch that descends from the saved tip. No ref means the old resume path.
- A berth release that exits 5 with "final disposition" counts as released. The unit's own pane shell does not block retire. Only a proven orphan group of the stopped session is stopped: its env names the unit session, the session is dead, and pid and start time match.
- `merge_checkpoint.py`: a failed merge-branch push resets to the tip before the merge (`HEAD^1`), which keeps local merge-branch commits.
- Every step reruns safely: a rerun changes nothing and exits 0.

**Files:**
- `scripts/production/retire_unit.py` — save, confirm, orphan stop, remove
- `scripts/production/resume_unit.py` — restore from the saved ref
- `scripts/production/merge_checkpoint.py` — failed-push rollback target
- `commands/showrunner/retire_unit.md`, `commands/showrunner/resume_unit.md`, `docs/as-built/showrunner-automation.md` — docs

**Gotchas:** the retired note keeps its session id when ref or tip change; resume parses `, ref ..., tip ...` after the folder.

### Phase 7j — The retire dry run names the saved ref  · status: done

#### As-built

- `retire_unit.py --dry-run` prints `would save: <full tip> to origin <ref>` before the remove lines when the branch has commits the merge branch lacks; `<ref>` is `refs/retired/<staff id>` or the ref already in the note. Its `would mark:` note carries the ref and tip. With no unmerged commits, the output is unchanged.
- The dry run and a real run decide the tip through one path, `plan_saved_branch`: an origin tip not yet fetched is saved, and a tip that diverges from a saved ref gets the same refusal in both.
- The dry run writes nothing: no push, no ref, no note, no FETCH_HEAD.

**Files:**
- `scripts/production/retire_unit.py` — dry-run save line, read-only object fetch
- `scripts/production/test_retire_unit.py` — dry-run tests

**Gotchas:** the read-only fetch is `git fetch --no-write-fetch-head --refmap= -q origin <ref>` with no destination refspec, then resolves the sha `ls-remote` advertised; `--refmap=` stops configured tracking-ref updates.

### Phase 7k — Retire says only what it did, and its dry run agrees with a real run  · status: done

#### As-built

- `retire_unit.py` prints each save, stop and mark line once, and only for what the run did; a session already gone gets no stop call and no line. A rerun on a fully retired unit prints `<unit> already retired`, exits 0 and writes no record (stop, cancellation, staff, commit or push), also with a Mac claim record whose process is gone.
- One function decides the unit's proven pane shell for both the dry run and the real run: the dry run prints `would close: pane <pane id> (shell <pid>)` for it; any other process inside the worktree prints `blocks retirement: unowned process <pid>`. The real run closes that pane through `unit_move.close_unit_pane` and prints no line for it.
- Retire removes the staff row and writes `staff <id>` in the retired note. Resume reads the id and calls `staff.restore_unit(id, name=, parent=)` (remote verb `restore-unit`) before `ensure_staff_unit`; `restore_unit` runs under `_record_transaction` and drops the id from `pending_retire`.
- `add_unit.commit_paths` returns `bool` (whether it committed); `add_unit.ensure_staff_showrunner` holds add_unit's staff step.

**Files:**
- `scripts/production/retire_unit.py` — action lines, already-retired path, shared pane-shell rule, staff id in the note
- `scripts/production/resume_unit.py` — restores the staff row by the note's id
- `scripts/production/staff.py` — `restore_unit` and the `restore-unit` verb
- `scripts/production/add_unit.py`, `scripts/production/promote_showrunner.py` — `commit_paths` result, `ensure_staff_showrunner`
- `commands/showrunner/retire_unit.md` — removal for good: the Units row and `origin` `refs/retired/<id>`
- `scripts/production/test_retire_unit.py`, `test_resume_unit.py`, `test_staff.py` — rerun, pane-shell, note and restore cases

**Gotchas:** `unit_lookup.unit_is_retired` reads "no staff row" as retired, so keeping a row is never a way to keep an id. `staff.defer_retire` schedules deletion through `retire_pending`; it does not retain. A restore that leaves its id in `pending_retire` is deleted by the next `retire_pending`.

**Ruled out:** keeping the staff row at retire (every `unit_is_retired` reader then sees a live unit); `staff.defer_retire` to keep the id.

### Phase 8 — One config reader, and a value change goes live without a release cycle  · status: done

#### As-built

- `scripts/lib/conf.py` is the one Python reader of `config/*.conf`. It reads `CONF_DIR` when set, else `~/.claude/config`, at each call with no cache. Calls: `number`, `whole`, `text`, `words`, `values`, `check(file, key, value)` and `effective_line(file, key) -> tuple[int, str]`. The one error is `conf.Unusable(ValueError)`; its text names the file, the key and the reason.
- Format: `#` starts a comment, `[section]` lines are skipped, the first value wins, and `key.<host>` wins over `key`. An empty value is a fault. The limit is the comment line directly above the key: `# whole: A..B` for `conf.whole`, `# range: A..B` for `conf.number`, `# one of: a, b`. A read whose form does not match the comment is a fault.
- Fault path: one stderr warning, then the newest committed value of that key that passes its revision's limits. With no such revision, or no git checkout, it raises `conf.Unusable`. No code defaults remain in any caller.
- `scripts/lib/conf.sh` (bash 3.2, sourced): `conf_value <file> <key>` forks nothing on the normal path and runs `conf.py get` on a fault.
- `scripts/lib/tunables.py` and `scripts/lint/config_values.py` are deleted; every caller reads through `conf.py`. `CONF_DIR` is on the override guard list (`scripts/build_hold/override_guard.py`, `override_guard.sh`).
- `/tune` (`scripts/lib/tune.py`, `commands/tune.md`): only the user approves a value change. It checks the value, changes one line in the main checkout, commits `tune(<file>): <key> <old> -> <new>` with a `User-Approved:` trailer, pushes, then pulls the Mac or queues the pull. It refuses with exit 2 and changes nothing on a missing file or key, a failed check, an empty `--why` or `--user-words`, or a key with a host line. A rerun of a set value prints `already <key>=<value>`, finishes an unpublished push and Mac pull, and exits 0.
- `scripts/lib/main_publish.py` holds the commit, push, Mac pull and queue code for `zone.py` and `tune.py`. `push()` accepts unpushed commits whose subjects start `tune(`, `zone(` or `user zone: `. A `.conf` change no longer selects the full suite. `merge_gate.py` has no settings check.

**Files:**
- `scripts/lib/conf.py`, `scripts/lib/conf.sh` — the reader
- `scripts/lib/tune.py`, `commands/tune.md` — the change command
- `scripts/lib/main_publish.py`, `scripts/lib/zone.py` — the shared publish code
- `config/*.conf`, `config/README.md` — limit comments and the reader section
- `scripts/lib/test_conf.py`, `scripts/lib/test_conf_sh.py`, `scripts/lib/test_tune.py`, `scripts/lib/test_main_publish.py` — tests

**Binds later work:** every test that reaches a conf read sets `CONF_DIR` to a fixture folder whose keys carry the real limit comments; a history-recovery fixture has its own committed git repository. `conf` loads as `conf` and as `scripts.lib.conf`, so a handler catches the producer's `conf.Unusable` or its translated domain error (`queue_place` raises `QueuePlaceRefused`). A limit comment lands on main, and in the installed config on both machines, before the code that reads it.

**Gotchas:** the merged runner reads the installed `~/.claude/config`, not the branch's, so a branch check of its own config proves nothing about landing. The codex_mesh repeat-guard timing tests also fail on main under load.

**Ruled out:** a second reader for any one file's settings; code defaults beside the config; a persistent status for `conf.Unusable`.

### Phase 8b — A rejected config publish recovers after main moves  · status: done

#### As-built

- `main_publish.ready()` fetches origin, lists `origin/main..main`, and refuses any commit whose subject does not start with `tune(`, `zone(` or `user zone: ` with `main holds other commit <id> <subject> that origin does not have; main is unchanged` (exit 2, stderr). The refusal runs before the edit, so `/tune` and `/zone` leave the config and HEAD unchanged.
- It then takes origin/main as a fast-forward. When that fails and publish commits exist, it rebases them onto origin/main (`--no-autostash --no-update-refs --empty=drop`). A conflict names the commit and its files, runs `rebase --abort`, and refuses with main unchanged.
- After a rejected push and another writer's push, a same-value rerun publishes one commit on top of origin/main and exits 0 with one line; a further rerun makes no new commit.

**Files:**
- `scripts/lib/main_publish.py` — `ready()` recovery and refusals
- `scripts/lib/test_main_publish.py` — temporary-repository recovery, refusal and conflict tests
- `scripts/lib/test_tune.py`, `scripts/lib/test_zone.py` — caller refusals read stderr and check HEAD unchanged

**Ruled out:** a new lock or a wider publish design (showrunner limit 2026-11-02).

### Phase 4i — Questions are their own type  · status: done

#### As-built

- An open question is a `QuestionRecord` in the production's proposals store (`issues_folder`), beside proposals. The asker is its fixed staff id in `asked_by`, never a name. Filing, review, queues, answers, relay, counts, and the deferred and review states read these records; no outstanding-JSON copy is kept.
- `update_registration.py migrate-questions` moves a production's old outstanding-JSON questions into the store once; a rerun changes nothing and exits 0. The showrunner runs it once per production after the merge.
- `update_registration.py questions-table` backs `/showrunner:questions` (`commands/showrunner/questions.md`), which lists the open questions in the form of `/showrunner:proposals`. `/next question` keeps its behavior.
- Every reader (footers, status line `counts_row.py`, dailies, `/next`, review sample, `measure`) takes one `staff.snapshot()` and one `session_staff.member_sources(...)` and joins `asked_by` through them for names.
- A unit rename rewrites no question record.
- `tidy_wrapped.py` closes the production's questions before it deletes the state files.

**Files:**
- `scripts/production/update_registration.py` — `QuestionRecord`, `question_records`, `write_question_record`, the `migrate-questions` and `questions-table` verbs.
- `scripts/production/production_units.py` — `WRAPPED`, the wrapped status line.
- `scripts/production/tidy_wrapped.py` — question close at wrap cleanup.
- `scripts/notify/review.py`, `scripts/hierarchies/measure.py`, `scripts/statusline/counts_row.py`, `scripts/production/footer_counts.py` — question readers that join staff.
- `commands/showrunner/questions.md`, `commands/showrunner/question.md`, `commands/showrunner/proposals.md` — the question and proposal commands.
- `scripts/production/test_update_registration_questions.py`, `test_update_registration_answers.py`, `test_update_registration_review.py`, `test_rename_state.py`, `test_rename_unit_state.py`, `scripts/notify/test_review.py`, `scripts/hierarchies/test_measure.py` — the question-record and rename tests.

**Binds later work:** `WRAPPED` is defined once in `production_units.py`; `tidy_wrapped.py`, `staff_sources.py` and `staff_test_support.py` import it there. Question records in the proposals folder keep their contents, timestamps, `production_id` and `asked_by` through any proposal migration. `showrunner_footer.py` and `tidy_wrapped.py` read the timer folder name `showrunner-<slug>`.

**Gotchas:** the module that holds `WRAPPED` must not import `update_registration` or `tidy_wrapped`, or basedpyright reports an import cycle. Per-file test runs do not cover the rename state tests; the full merged-tree gate does.

**Ruled out:** dual-writing questions to outstanding JSON and records (one store only); a rename that rewrites question rows (sdlc, 2026-11-01); recovering questions lost during the migration (user accepted the loss).

### Phase 4j — A production with no Proposals folder keeps its questions  · status: done

#### As-built

- `update_registration.question_folder(document, identities) -> QuestionFolder | QuestionOwnerUnavailable` is the one place that selects a production's question folder. `QuestionFolder(path, owner)` holds the folder and the showrunner's fixed staff id; `QuestionOwnerUnavailable(document)` means the doc has no local showrunner row.
- The folder is the doc's Proposals folder when it names one, else `<SHOWRUNNER_STATE_DIR or ~/.local/state/showrunner>/<showrunner staff id>/questions`.
- `store_questions` and `question_records_for_document` use it. A doc with no Proposals line files, lists and answers questions, and `migrate-questions` works for it; a doc with no showrunner row refuses with "no showrunner staff row".
- `footer_counts.sources` counts and watches the question folder through `question_folder`.
- `tidy_wrapped.can_hold_questions` reads the staff record only when the doc names a Proposals folder or a `<state>/*/questions` folder exists; the wrap then closes the questions in either folder.

**Files:**
- `scripts/production/update_registration.py` — `QuestionFolder`, `QuestionOwnerUnavailable`, `question_folder` and its record callers.
- `scripts/production/footer_counts.py` — the question count and watched sources.
- `scripts/production/tidy_wrapped.py` — question close at wrap cleanup.
- `scripts/production/test_update_registration_questions.py`, `scripts/production/test_footer_counts.py`, `scripts/production/test_tidy_wrapped.py` — regressions for a doc with no Proposals line.

**Binds later work:** the `issues_folder` result change switches `question_folder` and `tidy_wrapped.can_hold_questions` in the same commit and keeps the `<state>/<showrunner staff id>/questions` result. A proposal migration of the proposals folder does not touch that state folder.

**Gotchas:** `store_questions` writes the outstanding JSON only when reminders exist; with questions only, the file stays absent. `tidy_wrapped` deletes `<state>/<slug>/`; question folders sit under `<state>/<staff id>/`, so a move of a production's state folder to its staff id must keep `questions/` out of that delete.

### Phase 4k — A unit files a question with any copy of its production doc  · status: done

#### As-built

- `StaffSnapshot.by_doc(machine, path)` (`scripts/production/staff.py`) is the one doc lookup. It returns the row whose doc is exactly `path` first. Otherwise it matches the same file in another checkout of the same git repository: the same git common directory and the same repo-relative path.
- `_document_checkout(path) -> CheckoutDocument(common_directory, relative_path) | OutsideGitCheckout` reads `.git`, `gitdir:` and `commondir` with no subprocess. A path outside git matches only exactly.
- Two rows that match by repository raise `StaffRefused("<doc>: ambiguous production document; staff rows: ...")`.
- The callers (`QuestionStaff.owner`, `question_folder`) did not change. A unit's `ask` from its own worktree or from `~/.claude` main files into the showrunner's queue.
- Tests: `test_staff.DocumentLookupTests` (real repo with two linked worktrees and a subprocess guard) and `test_update_registration_questions.test_unit_files_and_lists_questions_from_linked_and_main_checkouts`.
- Open in Phase 4p: a relative Proposals line resolves from the caller's copy; the consumers do not catch `StaffRefused`; unreadable `.git` metadata reads as outside git.

### Phase 4p — Every checkout files a question in the showrunner's own folder  · status: done

#### As-built

- `question_folder` (`scripts/production/update_registration.py`) resolves the Proposals folder from the owner row's doc (`owner.doc.path`), so a relative Proposals line gives the showrunner's folder from every checkout.
- `update_registration.main`, `footer_counts.sources` and `tidy_wrapped` catch `StaffRefused`. `ask` and the readers print the refusal (`questions: refused — <reason>`) and exit 2. `tidy` raises `TidyRefused` and changes nothing.
- `_document_checkout(path) -> CheckoutDocument | OutsideGitCheckout | UnreadableGitCheckout(path, reason)` (`scripts/production/staff.py`). A path with no `.git` is `OutsideGitCheckout` and matches only exactly.
- `by_doc` matches exactly first. It refuses with `StaffRefused` when the asked document's own checkout is unreadable. It skips a candidate row whose checkout is unreadable. Two rows that match by repository still refuse as ambiguous.
- Tests: `test_staff.DocumentLookupTests` (unreadable asked checkout refuses, unreadable candidate is skipped), `test_update_registration_questions.py` and `test_tidy_wrapped.py` (refusal and unchanged state per consumer, relative Proposals line from a linked worktree).

**Gotchas:** a stale staff row whose `.git` names a missing gitdir is normal; a lookup must skip it, not refuse.

**Ruled out:** refusing on any unreadable candidate row — one stale row would block every lookup.

### Phase 4o — A unit folder moves at an open phase  · status: done

#### As-built

- `unit_move.py move-folder --unit <staff id> --to <path>` moves a unit folder while its phase is open. It stops the unit's own processes through the shutdown path: its Claude session, its run's codex app-server and watch shell, and its workers.
- The move record keeps each session's and worker's prior state (`sessions`, `FolderWorker` with its original cwd). After the move, only what was running resumes; each worker resumes on its same thread in its own cwd.
- A rerun recovers a worker whose resume launcher died (`_recover_worker_claim`, proved by start time and exact argv), refuses a running worker with no proved live launcher (`_worker_launcher_live`), and accepts a worker that finished.
- Any other process inside the folder refuses the move before any session stops or any file changes.
- Tests: `test_unit_move.py` (running and paused unit, running and paused worker, foreign process with and without an open phase, dead launcher rerun, finished worker).

**Gotchas:** `unit_move` imports bare `pane_record`; a test patches both `pane_record` module objects.

**Ruled out:** refusing every open-phase move — the user (via Morty, 2026-11-02) accepts a stop and resume.

### Phase 4t — Smoke launch builds in its checkout  · status: done

#### As-built

- `smoke_launch.sh` makes `CHECKOUT` absolute at start, then runs `cargo build` and `cargo metadata` through `checkout_cargo()`, a subshell that runs `direnv exec` from the checkout. The checkout's `.cargo/config.toml` applies from any caller folder, and the caller's folder does not change. Log text and exit codes are unchanged.
- `test_smoke_launch.py` runs the script from an outside folder with fake `direnv`, `cargo` and `uname` that record their folder, for an absolute and a relative checkout path with spaces.
- `git_test_support.commit_repository(repository, message, *paths, allow_empty=False, env=None)` passes `env` to its git calls.

**Files:**
- `scripts/production/smoke_launch.sh` — the showrunner smoke launch.
- `scripts/production/test_smoke_launch.py` — cargo folder regression test.
- `scripts/lib/git_test_support.py` — test commits with a given environment.

**Gotchas:** a relative `CHECKOUT` must become absolute before any `cd`, or `--manifest-path` and the log paths break.

### Phase 4s — Units rows take the worktree from staff  · status: done

#### As-built

Every tool takes a unit's location from its staff row. A stale or absent Units Worktree cell changes no result.

- One rule for a location: `unit_lookup.plan_worktree(cells, member)`. A relative Plan cell names a file in the staff worktree, so the staff worktree is the branch folder. An absolute Plan cell takes the Git root of its folder. A missing Plan folder raises `staff.StaffUnreadable`, with no walk up to a parent checkout.
- A reader takes one `staff.snapshot()` for each pass and joins with `staff_sources.member(staff_sources.UnitMember(<production slug>, <unit>), record)`. Readers: `merge_checkpoint`, `retire_unit`, `transfer_unit`, `promote_showrunner`, `add_unit`, `rename_unit`, `rename`, `production_lifecycle`, `units_table`, `unit_run`, `change_review`, `producer_backfill`, `assertion_guard`, `style/unit_files`, `usage/usage_report` and `usage/outcomes`.
- Unavailable facts keep their types: `NoSuchMember`, `StaffUnreadable` (`StaffAbsent` included) and `WorktreeUnfilled`. `RemovedWorktree` stays distinct. A command that changes state refuses and names the fact. A read-only report skips the row with one line.
- `merge_checkpoint.parse_units` returns `Unit` for the requested unit and `OtherUnit` (cells and member) for each other row. Only the requested unit's location can refuse the request. An other unit with no readable location prints one `other units: unavailable` line.
- `resume_unit.find_unit` has no cell veto. `staff_check` has no document-path comparison.
- `staff_sources` reads the cell only at its legacy import boundary, and only into a member with no typed location.
- The callers of `unit_lookup.live_unit_table` keep their projected `Worktree` field. The writers (`add_unit`, `rename_unit`, `rename`, `transfer_unit`, `style/add_style`) still write the column.
- The SessionStart pane-record hook writes no record for a print-mode child, so a child never replaces the director's membership record.

**Files:**
- `scripts/production/unit_lookup.py` — `plan_worktree`, the location rule.
- `scripts/production/staff_sources.py` — the member join and the legacy import boundary.
- `scripts/production/merge_checkpoint.py` — `Unit` and `OtherUnit` request rows.
- `scripts/hooks/session-start-pane-record.py` — the print-mode skip.
- `scripts/production/staff_test_support.py` — `fixture_unit_location(owner, unit, location) -> staff.StaffRow`, which records a canonical unit with a local location.
- `scripts/style/style_plan_test_support.py` — staff setup for the style tests.

**Binds later work:** a reader of a unit location uses `unit_lookup.plan_worktree` or the staff member join, never the Worktree cell. Staff rows hold canonical unit names (`unit_names.name` strips `-unit`).

**Gotchas:**
- A test that reaches a unit-location reader writes the staff rows it needs. No reader treats a missing staff file as an empty record.
- `resume_unit` imports the bare `unit_lookup` module and `add_unit` imports the package copy. A test patches each copy that its callee uses.
- The phase test list does not cover every reader caller. The gate for a reader change is `merge_gate.py --tests-only --full`.

**Ruled out:** a fallback reader for a missing staff file; a walk up from a missing Plan folder to a parent checkout.

### Phase 4v — Folder moves name what blocks them  · status: todo

**Depends on:** Phase 4o

Source: the sdlc showrunner, 2026-11-03. The devtools folder move refused with `unit_move: processes inside <folder>: <pids>`. The blocker was a real `codex app-server` (port 33335) and its idle-stop watcher, left running for 29 hours by a `scripts/agents/test_codex_mesh.py` fixture (run folder `/tmp/codex-mesh-test-1yxmd08g/run`, `CODEX_HOME` in the temp folder). The refusal was correct: the unit did not own that server. The refusal named only pids, and the test leaked a process.

#### Work Order

**Goal:** a folder move that meets a process the unit does not own refuses with a message that names each such process. No test leaves a real app-server or watcher running.

**Spec:**
1. `unit_move.py:1345`: the refusal names each unowned process: pid, its command (cut to one line), and the run folder of a mesh server or watcher when its record names one. Processes the unit owns (its run servers, watchers and launchers) keep their current stop path.
2. Find the `test_codex_mesh.py` path that starts a real `codex app-server` (the fixture with `CODEX_HOME` under `codex-mesh-test-*/home/.codex`, the `project-review` reviewer, thread `thread-capacity-test`). Make it use the stub server, or stop the server and its watcher in cleanup that also runs on failure.
3. A guard in that test class fails a test that leaves a live `codex app-server` or idle-stop watcher with its run folder under the test's temporary folder.
4. Check before stop: move-folder checks every process in the folder (owned and unowned, `unit_move.py:812` arguments check included) before it stops any process. A refusal stops nothing. Source: the showrunner, 2026-11-03: the first move run printed `process 2236417: arguments changed` and `stopped app-server 2236059`, then refused.
6. A live session picks its launch. `unit_move.folder_launch` (`unit_move.py:516-523`) counts every retained record whose `session_id` matches, ended ones included, and refuses on two. Case (records only, 2026-11-03): phases-unit `ee6d3351-30ca-427c-9ce7-fce82ba66155` has two records in `~/.local/state/herdr-panes/agents.json`, `wM:pK` (2026-10-31T21:53Z) and `wY:p7` (22:28Z), both `ended: true`, while its director runs live in a pane neither names. `herdr.py:2721` then finds no proving pane and prints `current pane is unknown; retained launches kept`. Fix: resolve the live member process to its pane first and take the record of that pane; with no record for the live pane, use the live pane and process. Each other match prints one anomaly line and does not block. Refuse only when two live panes prove the same conversation. Find the writer that sets `ended` and keeps the older record when the same session launches in a new pane; retire the superseded record there.
5. One line per anomaly: `herdr.py:2938` prints `anomaly: <name> (Codex thread unknown); not blocking` four times for each of morty and recipes in one move run. Print each anomaly once per command.
7. Extend `herdr.repair_retained_launches` (`scripts/herdr/herdr.py:2732`); add no second repair command. `move_folder` already calls it (`unit_move.py:1415`). Resolve each retained reference through the command's staff snapshot. Rewrite a proved member's `session_id` to its staff id. Remove a record only after confirmed absence and the existing live-pane proof. Keep the replacement-conversation proof, idle-shell proof, and publication checks (`herdr.py:2750-2800`). Use `pane_launch` for locked record changes (`pane_launch.py:185,285`); compare the expected publication under the lock. Return named repaired, removed, unchanged, and unavailable outcomes. An unreadable staff record or unproved pane is unavailable, not confirmed absence. Print one diagnostic line for each repaired or removed record. Test a concurrent publication change; preserve the changed record. A completed rerun changes no record and reports no repair.
8. The test that writes alpha-unit records uses its own temporary state folder. The repair policy stays in `herdr.repair_retained_launches`; `pane_launch` owns its locked writes. A test proves the real `~/.local/state/herdr-panes` folder stays the same.
9. `test_assertion_guard_revisions.py::test_large_diff_guard_call_uses_less_than_one_cpu_second` (`:982-994`, added by Phase 7, `7aed97d7b`) fails at 1.01-1.02 CPU seconds against its 1.0 limit at load 40. It also fails on the tree before Phase 4s. Keep its purpose: a guard call on a large diff stays cheap, and a slow (for example quadratic) comparison fails the test. Make the smallest change that does not fail under load: for example, compare the guard's CPU time with a fixed reference workload measured in the same test, so that contention slows both. Do not use wall time. Showrunner, 2026-11-03.
10. The real staff record holds a showrunner row named `fixture` whose doc points into a merge-gate test folder (`/dev/shm/merge-gate-*/direct-test_verify_lint_guard_key-*`). `scripts/direct/test_verify_lint_guard_key.py`, or a test it calls, wrote it through the real staff record. Give that test its own staff file. Add one guard in `scripts/production/staff.py`: one helper, called at the staff write entry for the doc path and the worktree path, refuses a path under a test root and names the path. Do not put a check in each writer (models-unit 3n also changes `staff.py` and merges after 4v). Remove the stray row through the staff API; a second run of the removal changes nothing.

**Files:**
- `scripts/production/unit_move.py` — refusal text.
- `scripts/production/test_unit_move.py` — a folder with an unowned process refuses and names its pid, command and run folder; owned processes still stop; a refusal stops no process.
- `scripts/agents/test_codex_mesh.py` — no real app-server; leak guard.
- `scripts/herdr/herdr.py` — one line per anomaly; extend the existing retained-launch repair.
- `scripts/herdr/test_herdr.py` — anomaly deduplication, live-pane proof, typed unavailable outcomes, repair diagnostics, concurrent changes, and an unchanged rerun.
- `scripts/herdr/pane_launch.py` — locked publication updates and removal; retire a superseded launch record.
- `scripts/herdr/test_pane_launch.py` — new test module; a relaunch leaves one record. Locked repair preserves concurrent changes and isolates fixture state.
- `scripts/direct/test_assertion_guard_revisions.py` — the CPU test keeps its purpose and passes under load.
- `scripts/production/staff.py` — one test-root write guard at the write entry; stray-row removal through the staff API.
- `scripts/production/test_staff.py` — the guard refuses a test-root doc or worktree and names the path; the removal rerun changes nothing.
- `scripts/direct/test_verify_lint_guard_key.py` — uses its own staff file; the real staff record stays the same.

**Workers:** `1 writer + 1 tester`
- `impl` — `unit_move.py`, `herdr.py`, `pane_launch.py` and `staff.py`; owns each source hub.
- `test` — all seven test modules; owns regressions, fixture isolation, leak checks, and the CPU test.

**State:** retained launch records gain fixed staff ids or lose proved stale entries through locked atomic writes. Unavailable evidence leaves records unchanged. A completed rerun writes nothing.

**Constraints from prior phases:** herdr 6a also changes the Darwin `startup_processes` reader in `scripts/herdr/herdr.py`. Coordinate these source changes and merge origin/sdlc before the 4v gate. Phase 4o preserves this move sequence: stop, worktree, staff, launch, run, resume. Never kill by pattern. Stop a server through `codex_mesh.py stop --session-dir <run>`. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.

**Acceptance gate:** each regression fails before its repair and passes after it. A refused move stops no process. Live-pane proof and publication checks stay intact. Concurrent changes survive; unavailable evidence preserves records; a completed repair rerun writes nothing. The fixture isolation test preserves the real launch folder. The CPU test rejects a slow comparison without wall time. Run all five modules: `scripts/production/test_unit_move.py`, `scripts/agents/test_codex_mesh.py`, `scripts/herdr/test_herdr.py`, `scripts/herdr/test_pane_launch.py` (new), and `scripts/direct/test_assertion_guard_revisions.py`. The mesh suite leaves no app-server or watcher under its temporary run folder; check with `ps` after the run. `basedpyright` covers all three source files and all five test files; require `0 errors, 0 warnings, 0 notes`. One director gate.

### Phase 4u — Units tables have no Worktree column  · status: todo

**Depends on:** Phase 4s

Source: split from Phase 4s (director, 2026-11-03). After 4s no tool reads the cell. This phase stops writing it and removes it from existing documents.

#### Work Order

**Goal:** new and existing Units tables have no Worktree column, and the format guide says the staff row owns the location.

**Spec:**
1. One heading-based operation in `scripts/production/unit_table.py` removes the complete Worktree column after the caller proves item 3. Remove the header, separator cell, and every row cell together. Keep other tables, extra columns, retirement notes, and other text. A second run changes nothing.
2. Writers stop writing the cell: `add_unit.desired_row` (`add_unit.py:439`) and `append_row` (`:759`), `rename_unit._doc_text` (`rename_unit.py:477`), `rename._unit_doc` (`rename.py:773-784`), `transfer_unit._row_line` (`transfer_unit.py:332`, both table layouts), `style/add_style._unit_row` (`add_style.py:190`), `_production_text` (`:209`) and its pilot-row replacement. Each writer that edits an existing table removes the column through the operation in item 1.
3. Before removal, the caller checks every Units row against one staff snapshot. Each row needs its matching member and a typed location (`PresentWorktree` or `RemovedWorktree`). If any member or location is unavailable, keep the entire Worktree column, including its header and separator. Name each unavailable row for repair; never remove only some cells. Admission can precede membership publication (`add_unit.py:1428-1446`); keep the column during that pending state. A new proposed location does not prove published membership. Test mixed valid and `WorktreeUnfilled` rows, missing membership, unreadable staff, and admission before publication. After all required facts are published, remove the column once; a rerun changes nothing.
4. `docs/production_format.md:143` and `:158`, `commands/producer/to_production.md:105` and `commands/showrunner/rename.md:35` say the staff row owns the location.
5. Test fixtures change to the new layout. Some legacy fixtures stay for the tests that read an old table.

**Files:**
- `scripts/production/unit_table.py` — column removal.
- `scripts/production/add_unit.py`, `rename_unit.py`, `rename.py`, `transfer_unit.py`, `scripts/style/add_style.py` — writers.
- `docs/production_format.md`, `commands/producer/to_production.md`, `commands/showrunner/rename.md` — format text.
- `scripts/production/test_unit_table.py` — both layouts, repeated removal, extra columns, other tables, and mixed valid and unavailable rows.
- `scripts/production/test_add_unit.py`, `scripts/production/test_add_unit_rerun.py` — admission before membership publication preserves the complete legacy column.
- `scripts/style/test_add_style.py` — the new layout.
- `scripts/production/add_unit_test_support.py`, `merge_checkpoint_test_support.py`, `production_lifecycle_test_support.py`, `rename_test_support.py`, `rename_unit_test_support.py`, `staff_test_support.py`, `transfer_unit_test_support.py`, `scripts/direct/assertion_guard_test_support.py`, `scripts/style/style_plan_test_support.py` — fixture builders in the new layout.

**Workers:** `1 writer + 1 tester`
- `impl` — the source and format files.
- `test` — the tests and fixtures.

**State:** a writer removes the complete Worktree column only after every row has its required staff facts. A pending admission or unavailable row preserves the complete column.

**Constraints from prior phases:** Phase 4s readers ignore the cell. `staff_sources` reads it only during legacy import. Keep Delegation Context → Ownership and Test runs. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.

**Acceptance gate:** fully populated tables lose the complete Worktree column. Mixed valid and unfilled rows preserve the complete column. Admission before membership publication preserves it; publication permits removal. A second removal changes nothing. Each writer runs `python3 -m unittest` on its named test files. Require `0 errors, 0 warnings, 0 notes` from `basedpyright` for each slot. One director gate.

### Phase 4q — The folder move uses its owners' readers  · status: todo

**Depends on:** Phase 4o

Source: Phase 4o review (simplify lens), 2026-11-02. Each item removes a copy that Phase 4o added; the owners' modules are outside the 4o file set.

#### Work Order

**Depends on:** 4v.

**Goal:** the folder move keeps no private copy of a rule that another module owns, and each owner's callers all use the one copy.

**Spec:**
1. `workers._moved_path` (`workers.py:1726`) becomes public. `unit_move.moved_folder_argument`, `rename_unit._moved` (`rename_unit.py:933`) and the inline rule at `progress_history.py:478` call it.
2. `move_folder_worker_paths` calls `workers.repoint_unit_worktree`. Its `repoint_roster` (`workers.py:1870`) also moves `instructions_file` and `authorized_prompt`. Settle the `unit_ledger` difference in the merge.
3. One shared step pauses runs, classifies run processes and stops them, in `unit_move`. `rename_unit._stop_runs` (`rename_unit.py:899`) and `retire_unit.run_processes` (`retire_unit.py:439`) use it. Keep the stronger proof: start time plus exact argv, and detached `mesh_runs` supervisors. Classification is a read-only call; stopping is a separate call. `rename_unit.check_stoppable` (`rename_unit.py:364`) uses only the classification in preflight; keep `test_rename_unit_processes.py:188` (refusal with no signal).
4. `codex_mesh` exposes `recorded_run_servers(run)`, `is_run_server(pid, port, run)`, `is_worker_launcher(pid, run, name)` and `run_watcher(arguments)` beside their writers. `unit_move` and `retire_unit.py:460` call them.
5. `unit_move` drops `PriorFolderWorker` and checks a status against `codex_mesh.ENDABLE_STATUSES`, with the classes of a public `_roster_thread` (`codex_mesh.py:636`). The exit code for an unknown status stays 1.
6. `resume_message` exposes `resume_prompt_path(seat)` and `add_resume_note(seat, note)`. `unit_move`, `rename_unit.py:973`, `resume_message.py:87` and `inventory.py:818` use them, with one atomic write form.
7. `herdr.child_environment` (`herdr.py:871`) owns one list of session identity variables, and `unit_move` uses it. The change to `herdr.py` goes through herdr-unit. First define the required launch environment for each caller and test the inherited identities: `herdr.child_environment` (`herdr.py:903`) removes fewer identity variables than either folder-resume path, the unit path removes `HERDR_TAB_ID` and the worker path removes `STAFF_OPERATION_FD`.
8. `progress_history` exposes one call that returns the run states for a history file; `move_run` (`progress_history.py:499`) and `unit_move.folder_runs` use it.
9. A worker process that exits between the liveness check and its argument read in `pause_folder_workers`, or just after `Popen` in the resume path, reads as exited; the move goes on. Add a regression test for each read.
10. Each `codex_mesh` import in `unit_move` comes after the early exits, so a move whose runs have no mesh imports no `codex_mesh`.
11. Copy removals from the Phase 4o review (sdlc showrunner ruling, 2026-11-02; where two copies differ, keep the owner's rule): the move starts workers through the unit's resume message, as `stop_resume.py:337` and `restart.py:2246` do, and starts no worker itself; one worker resume path keeps the `codex_seats` "was working" rule; one `codex_mesh.pause_run` serves `unit_move` and `rename_unit.py:909`, in place of the `Namespace` call and the subprocess call; `codex_mesh` owns "unpause an idle worker" for a run and its idle workers, and `unit_move` writes no roster status; `has_mesh` comes from the mesh files only; the move record always holds `sessions`, and the legacy branch goes.
12. For each of these, first find why the code is as it is: two folder scans (`unit_move.py:736, 1237`) — is the second needed?; does `codex_mesh` lock the app-server start, so all workers can start before one wait loop?; does anything read `launcher_command` of a worker that is not running (else list Codex seats only for runs with a running worker)?; why the unlocked first roster read (can all runs be checked under the locks before any write)?; can a run state folder have another name, and who writes it (else read the run's own state file first)? The director settles each item under DecisionEconomy and states the call in one line (user via builds and the sdlc showrunner, 2026-11-02). Only an item that touches money, safety, behavior the user sees, or an act that cannot be undone goes to /unit:question, and its change waits for the user's answer.
13. Resources-unit P47 (`fd1508d72` on `builds-resources`) moves `UseConfiguredValue` to `scripts/lib/conf.py` with one definition; devtools accepted it on 2026-11-03. At 4q start, confirm that it is on the merge branch with `rg -n 'class UseConfiguredValue' scripts` (one hit, `scripts/lib/conf.py`). If it is not there, do this move as written in the binding below. Keep the settings-sharing regression in `test_rename_unit_processes.py:207` and the `test_conf.py` read-count and recovery tests.

**Files:**
- `scripts/production/unit_move.py`, `scripts/direct/workers.py`, `scripts/production/rename_unit.py`, `scripts/production/retire_unit.py`, `scripts/agents/codex_mesh.py`, `scripts/lib/resume_message.py`, `scripts/shutdown/inventory.py`, `scripts/direct/progress_history.py`, `scripts/herdr/herdr.py` — source.
- `scripts/production/test_unit_move.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_retire_unit.py` — regressions.
- `scripts/lib/conf.py`
- `scripts/issue_score/selection.py`
- `scripts/issue_score/apply_labels.py`
- `scripts/accounts/next_moves.py`, `scripts/direct/simplify_result.py` — configuration-default consumers.
- `scripts/issue_score/{blind,client,decide,evaluate,questions,records,score_issues,vault}.py` — configuration-default consumers.
- `scripts/production/test_rename_unit_processes.py`, `scripts/lib/test_conf.py`, `scripts/accounts/test_next_moves.py` — ownership and configuration preservation.
- `scripts/issue_score/test_score_issues.py`, `scripts/issue_score/test_config.py` — configuration-default consumers.

**Workers:** `1 writer + 1 tester`.
- `impl` — all source files above, including `conf`, `selection`, `accounts/next_moves`, `direct/simplify_result`, and the eight issue-score consumers.
- `test` — all test files above, including `test_conf`, `test_rename_unit_processes`, `test_next_moves`, `test_score_issues`, and `test_config`.

**State:** none; behavior stays the same.

**Constraints from prior phases:** Phase 4o pauses and resumes workers on their same threads. Keep every `test_unit_move.py` test green. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.

**Acceptance gate:** Phase 4v passes first. Keep its refusal-before-stop tests and the `staff.set_worktree(..., expected=row.worktree)` guard (`unit_move.py:1494`). Keep the settings-sharing, lookup, requested-checkpoint, and partial-report regressions. `grep` shows one definition of each rule above. Each changed module's existing suite passes without assertion changes. Require `0 errors, 0 warnings, 0 notes` from `basedpyright` on changed files and their callers. Run each test file with `unittest discover`. The full gate passes.

### Phase 4r — One config read serves many settings  · status: done

#### As-built

- `conf.read(file) -> ConfigRead` (`scripts/lib/conf.py`) reads and parses a config file once. `ConfigRead.text`, `number`, `whole`, `words`, `values` and `origin(key, convert)` resolve each setting from that read, with the same limits, host keys and messages as before.
- A key with no usable value in the read falls back to the newest valid committed value, read at use. Nothing is cached: each `conf.read` reads the file again. A fault warning prints when the setting is used, with the same text and stream as before.
- The module-level readers (`conf.text`, `conf.number`, `conf.whole`, `conf.words`, `conf.values`, `conf.value_origin`) do not exist. `effective_line`, `editable_line` and `check` are unchanged.
- Every caller holds one `ConfigRead` for each pass (one tick, one command) and gives it to its helpers. A helper that takes the read has the parameter type `conf.ConfigRead | UseConfiguredValue` with default `UseConfiguredValue.CURRENT`, never `None`.

**Files:**
- `scripts/lib/conf.py` — the one config reader (`read`, `ConfigRead`).
- `scripts/issue_score/selection.py` — `UseConfiguredValue`.
- `config/README.md` — the reader contract (one read per `conf.read`; `ConfigRead.origin`).
- `scripts/lib/test_conf.py` — read-count, edit-between-reads, deferred-recovery and unchanged-warning tests.

**Binds later work:** `UseConfiguredValue` moves from `scripts/issue_score/selection.py` to `scripts/lib/conf.py` beside `ConfigRead`, with one definition. A new config setting is read through one `conf.read(file)` per pass.

**Gotchas:** a test double that replaces a function taking the read must accept the new argument; the renewal path catches a `TypeError` and prints nothing, so a wrong double fails silently.

**Ruled out:** a change to `simplify_result.py` for one read — its check and finish steps run in separate processes, and each reads `direct.conf` once.

### Phase 7m — Showrunners take Morty's words as the user's  · status: done

#### As-built

`commands/showrunner/produce.md` → `<Throughout>` holds the one rule "A message from morty is the user's", next to "A unit director's message is a peer's". Morty is the user's associate producer, a Codex session. A message from morty counts as the user's own words (answers, approvals, rulings, priorities); a relay to a unit begins `From the user (via morty and <showrunner name>):`. Morty is not a parent, and no showrunner reports to morty. The permission rules stay: a morty message never changes permission settings, CLAUDE.md or config, and never approves a permission prompt. `send.py` does not prove a sender, so a showrunner confirms an action that cannot be undone with morty by `send.py --to morty`.

**Files:**
- `commands/showrunner/produce.md` — the rule, its one source.

**Binds later work:** messaging-unit's `send.py` sender check changes the rule's last point in its own commit.

### Phase 3x — Small stores keyed by staff id  · status: todo

Source: herdr-unit plan Phase 3x, given to devtools-unit by sdlc 2026-11-02. Starts after herdr-unit's 3s. Scope: the stores only. devtools-unit lands stores 1, 2, 4 and 5 below. Store 3 is dropped (user, 2026-11-01). Store 6 (`stall_watch.py`) belongs to messaging-unit. herdr-unit's 3y owns the rename files (`rename.py`, `rename_unit.py`, `rename_outside.py`, `transfer_unit.py`) and removes each store's rewriter there.

**Rewriter order (Phase 4i review, 2026-11-02):** a store lands only when its old rewriter in a rename file is gone (herdr-unit 3y) or a test on the landed tree shows that the rewriter leaves the staff-id key unchanged and a real rename still exits 0. Name each store's rewriter and its 3y step in that store's checkpoint notice.

**Depends on:** 3t, 3u, 3v (records stage of the rename track). One writer owns `change_review.py`, `stall_watch.py`, `board_json.py`, `phase_table.py` and `herdr.py` at a time.

#### Work Order

**Goal:** four small stores that hold a unit or production name hold the staff id; no rename or transfer needs to rewrite them, and herdr-unit 3y removes the rewriter of each.

**Spec:** For each store: readers take the id from the row; a rerun-safe migration renames each old file or rewrites each old key to the id once (a key whose name has no row is listed, never filled from a likely row); then the rewriter goes. **One store, one landing (2026-10-31, packaging).** Each numbered store is its own landing step, in the order listed: one commit with its readers, its writers, its command text and its Codex mirror (`claude_to_codex/sync.py`), its migration and its rewriter's removal, then its own real proof before the next live store migration starts (other stores can be prepared in parallel): the real migration on natedev (and the Mac where the store exists) stopped after its first file and run again ends equal to one clean run, and a rerun of the finished migration changes nothing and exits 0. The inventory of each store, done before its edit and given in the checkpoint notice, lists its path, its internal keys, every writer, every reader, every command and skill line (`grep -rn '<store path or key>' scripts commands ~/.codex/skills/generated-from-claude`) and its rewriter. Smallest first:
1. **Change review cell:** new rows already write staff ids (`change_review.py:620`). `change_routing.owner` already resolves the current parent (`change_routing.py:157-182`). Keep these paths; do not rebuild them. Inventory remaining legacy open rows, switch remaining consumers, and remove the obsolete rename rewriter. Migrate only proved legacy open-row ownership to the unit or showrunner staff id. Keep reviewed rows byte-identical, including legacy names and recorded production; test that the migration never edits them. A transferred unit's open row follows its current parent without a rewrite. Use the existing routing tests to preserve this behavior. Do not import or reuse `producer_backfill.py`; its owner deletes it after Phase 4s lands. The remaining migration belongs to this store's landing and preserves the reviewed-row boundary. `/change_review` displays current names for open rows and recorded history for reviewed rows.
2. **Phase note `unit:`** (`phase_table.py:1811`, `rename_unit` :2070): the note holds the unit's staff id.
3. **Saved tab titles — dropped (user, 2026-11-01).** The saved layout keeps herdr names; `herdr.rename_saved_tab` (`herdr.py:2418`) replaces the title in the file at a rename, also for a stopped member (4g calls it).
4. **Board cache** (`~/.local/state/unit-board/<slug>.json`, `board_json.py:424`; moved by `rename_outside.py:64`): `<showrunner staff id>.json`. The unit-board plugin's reader switches in the same step (the unit-board unit's file).
5. **Proposal `unit:`** (`proposal_issue.py:261`; 4 readers): the issue field holds the staff id, and readers show the row's name. The proposals folder also holds question records, which Phase 4i already keyed by staff id. A production with no Proposals line keeps its question records in `<state>/<showrunner staff id>/questions` instead (Phase 4j, `update_registration.question_folder`); the migration does not touch that folder. The migration changes proposal records only; each question record keeps its contents, timestamps, `production_id` and `asked_by` byte for byte. A shared-folder regression holds both kinds of record and checks proposal ownership and the `/showrunner:questions` table after the migration, after a rename and after a transfer.
6. **Stall stretch** (`stall_watch.py:412` `stretch_path`; 4 readers): stretch files, prompt files and notification keys by staff id. The migration cancels and re-sends a live alert under the new key, so elapsed time and cancellation survive (old Phase 3h). The moves in `rename_unit.py`, `rename.py` and `transfer_unit.py` go. `PROMPT_ALERT_SECONDS = 60` (`stall_watch.py:66`) moves to `config/production.conf`, read at use through `conf.read("production")`, one read for each pass, with a changed-config test. `stall_watch.py` belongs to messaging-unit: coordinate this item with it, outside the devtools store landings.

**Files:**
- `scripts/production/change_review.py`, `scripts/direct/phase_table.py`, `scripts/direct/board_json.py`, `scripts/production/proposal_issue.py`, `commands/change_review.md` — the four stores' writers and readers; every other reader that a store's inventory search finds joins its step.
- `scripts/production/change_routing.py` — remaining change-review consumers; preserve current-parent routing.
- `scripts/production/test_change_routing.py` — transferred open rows and unchanged reviewed history.
- `scripts/production/test_change_review.py`, `scripts/direct/test_phase_table.py`, `scripts/direct/test_board_json.py`, `scripts/production/test_proposal_issue.py`, `scripts/production/test_update_registration_questions.py` — their tests and the shared-folder regression.
- `scripts/production/rename.py`, `scripts/production/rename_unit.py`, `scripts/production/rename_outside.py`, `scripts/production/transfer_unit.py` — read only (herdr-unit 3y).

**Workers:** `1 writer + 1 tester` per store, one store at a time.
- `impl` — that store's non-test files; hub: the store's own module (`change_review.py`, `phase_table.py`, `board_json.py`, `proposal_issue.py`).
- `test` — that store's tests.

**State:** each store moves to staff-id keys, once.

**Constraints from prior phases:** keep the Phase 3s resolver and Delegation Context → Ownership. Phase 4p resolves relative question folders from the staff owner's document through `update_registration.question_folder`. Unreadable ownership is not confirmed absence. Keep the linked-checkout, ambiguous-owner, and shared-question tests. Before dispatch, check that 3t, 3u, and 3v are on the branch. `RowResolver` comes from origin/sdlc. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.
- Phase 3h review (2026-11-01): item 2 also removes `showrunner:` and `production_file:` from the phase note (read from staff). Item 5: `unit:`, `proposed_by:` and `approved_in:` hold fixed ids; `showrunner:` and `plan:` go. Each with all its readers and its own store proof.

**Acceptance gate:** require `0 errors, 0 warnings, 0 notes` from `basedpyright` on changed files and their callers. Each store's suite passes; run each test file with `unittest discover`. Each store meets the rewriter order and its own real proof before its separate landing. A transferred unit's open row appears under its current production; a production-owned row stays with its production. Every reviewed row stays byte-identical through legacy migration and its rerun. A real `/change_review` shows a moved unit's open row under its new production without a rewrite. The item 5 shared-folder regression passes. Keep the lookup, requested-checkpoint, and partial-report tests. The full gate passes before each store's checkpoint.

**Constraints from prior phases (native ids):** Phase 3h changes a live id and child links only in one locked atomic `staff.json` write. Add no store registry, identity alias or live-rekey journal. Parked files and retired refs belong to stopped units. The one-time identity migration writes reference stores first and staff last; it is safe to rerun and is deleted in Phase 18. This store conversion must test a clear between its reads: resolve current runtime ownership again, report unavailable old-id evidence, and make no unproved write. Do not add a second row to recover an old store key.

### Phase 3t — Production tools read the row and `.unit.toml`  · status: todo

Source: herdr-unit plan Phase 3t, given to devtools-unit by sdlc 2026-11-02. Starts after herdr-unit's 3s and after models-unit's Phase 4 lands.

**Depends on:** 3s. Shared gate with 3u/3v over the exact final batch; before the record and deletion stages.

#### Work Order

**Goal:** the production tools in `scripts/production/` read the worktree, branch, plan, port, retired state, doc and merge branch through the Phase 3s resolver, not from a Units table cell, a retired note, a timer's doc or a doc field.

**Spec:**
1. **Readers.** Every non-test file in `scripts/production/` that this search finds, except the ones later steps rebuild or delete (`rename.py`, `rename_unit.py`, `rename_outside.py`, `rename_checkout.py`, `rename_state.py`, `unit_rename_sweep.py`, `transfer_unit.py`, `showrunner_parent.py`, `showrunner_tree.py`, `production_units.py`, `units_table.py`): `grep -rlE 'unit_table|units_table|## Units|production_units|unit_is_retired|Showrunner session|space_from_text|former_slugs|Formerly|Parent production' scripts/production`. A timer read for session, socket, zone or interval is allowed (decision 16), so the search does not forbid `registered_showrunners`; a timer's doc read is found by the gate test below. At ac680222 it lists `add_showrunner`, `add_unit`, `change_review`, `ci_points`, `codex_winddown`, `completion_receipt`, `footer_counts`, `log_backup`, `merge_checkpoint`, `merge_gate`, `next_item`, `production_lifecycle`, `promote_showrunner`, `proposals_folder`, `proposals_table`, `prune_phase_notes`, `resume_unit`, `retire_unit`, `roles_audit`, `staff_check`, `stall_watch`, `style_units`, `unit_run`, `update_registration`, `waiting`.
2. Each reader takes membership from one snapshot and facts from the shared resolver. Extend `unit_lookup.plan_worktree` (`unit_lookup.py:93`) for the branch folder; add no second branch-folder rule. Use semantic location types to distinguish the director folder from the branch folder. The director folder comes from staff. A relative Plan names a file there; its branch folder is the director folder without a Git call. An absolute Plan uses its own existing folder's Git root. Missing folders and folders outside Git stay unavailable. When Plan readers move to `.unit.toml`, keep this same rule in the shared resolver. `RowResolver.unit_branch` currently reads Git in the director folder (`session_staff.py:245`); change it to use the shared branch-folder result. Keep `test_checkpoint_uses_the_plan_worktree_when_the_director_runs_elsewhere`. A timer supplies its session, socket, zone, and interval through `showrunners.timer_for` (decision 16). The document comes from the showrunner row. The excluded files keep their `production_units` import until Phase 4c moves it. `resume_unit.py` and `retire_unit.py` read retirement facts from the parked file. `add_unit.py` finds an existing unit through staff membership.
3. The Owns scope check of `merge_checkpoint.py` (:243-248, :583) is not changed here; Phase 4b deletes it.
4. History stays history: a reader that reports who owned a past commit or run keeps what the artifact recorded (old 3i rule).
5. **The resume recovery type states its guarantee (moved here from the Phase 3r closure review, 2026-11-01).** `resume_unit.py:89` holds `recovery: UnitConfig | NoRetiredSession`. A `UnitConfig` can lack the retirement facts, and the sentinel means only that no recovery file is outstanding, so the callers keep the guarantee by procedure. Replace it with one named recovery type whose retirement facts are mandatory, and a separate `NoRecoveryRequired` state. A test pins that a recovery value without the retirement facts cannot be made.
6. **An unreadable staff record is its own result (Phase 4i review, 2026-11-02).** The question readers that join staff (`scripts/hierarchies/measure.py`, the review sample in `scripts/notify/review.py`) give an explicit incomplete-read result when `staff.json` cannot be read, distinct from confirmed absence. `measure` prints it as one note and never refuses (user, 2026-11-02). The review sample never classifies a stored question as unregistered from an unreadable record. Tests inject an unreadable staff record and show the two results differ.

**Files:**
- `scripts/production/{add_showrunner,add_unit,change_review,ci_points,codex_winddown,completion_receipt,footer_counts,log_backup,merge_checkpoint,merge_gate,next_item,production_lifecycle,promote_showrunner,proposals_folder,proposals_table,prune_phase_notes,resume_unit,retire_unit,roles_audit,staff_check,stall_watch,style_units,unit_run,update_registration,waiting}.py` — the readers item 1 lists (ac680222); a reader the search finds at dispatch joins them.
- `scripts/production/test_{add_showrunner,add_unit,change_review,ci_points,codex_winddown,completion_receipt,footer_counts,log_backup,merge_checkpoint,merge_gate,next_item,production_lifecycle,promote_showrunner,proposals_table,prune_phase_notes,resume_unit,retire_unit,roles_audit,staff_check,stall_watch,style_units,unit_run,update_registration,waiting}.py` — their tests.
- `scripts/production/{add_unit,production_lifecycle,merge_checkpoint,dailies_input,update_registration,staff}_test_support.py` — fixtures.
- `scripts/production/unit_lookup.py`, `scripts/production/session_staff.py` — shared branch-folder resolution and its row resolver caller.
- `scripts/production/test_unit_lookup.py` — distinct director and branch folders; unavailable Plan facts.
- `scripts/production/test_session_staff.py` — new test module for the shared branch-folder resolver caller.
- `scripts/production/session_labels.py` — read only.
- `scripts/hierarchies/measure.py`, `scripts/notify/review.py`, `scripts/hierarchies/test_measure.py`, `scripts/notify/test_review.py` — item 6 (`measure.py` owner: hierarchies-unit; name it in the notice's also-touches line).
- `scripts/production/tidy_wrapped.py`, `scripts/review_links/change_browser.py`, `scripts/review_links/change_page.py` — `issues_folder` callers (constraint below).
- `scripts/production/test_proposals_folder.py` — new test module; distinguish unavailable documents from unconfigured proposals and ledgers.
- `scripts/review_links/test_change_browser.py` — new test module for the proposal-folder caller.
- `scripts/production/test_tidy_wrapped.py`, `scripts/review_links/test_change_page.py` — proposal and ledger caller outcomes.

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/production_lifecycle.py` (wrap, attach and the lifecycle reads that others call).
- `test` — the tests and fixtures.

**State:** none.

**Constraints from prior phases:** keep the Phase 3s resolver, Phase 3r writers, and Delegation Context → Ownership. Phase 4p resolves relative question folders from the staff owner's document through `update_registration.question_folder`. Unreadable ownership is not confirmed absence. Keep the linked-checkout, ambiguous-owner, and shared-question tests. Phase 4s keeps the writers' Worktree cells. Phase 4u removes the column; do not remove it here. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.
- Phase 3h review (2026-11-01): regression: `add_unit --resume --check` with a dead pending opening reports it and writes nothing; the test checks the pending file bytes unchanged. Keep the planned change of `recovery: UnitConfig | NoRetiredSession` to guaranteed retirement facts and `NoRecoveryRequired`.
- Make `issues_folder` (`proposals_folder.py:41`) return `ProposalsFolder | ProposalsUnconfigured | ProposalsUnavailable`. `ProposalsUnavailable` names the document and read failure. Make `ledger` (`proposals_folder.py:32`) return `ImprovementLedger | LedgerUnconfigured | LedgerUnavailable`, with the same distinction. An unreadable document never proves an unconfigured folder or ledger. Test unreadable documents and confirmed absence as different outcomes.
- Phase 4i review (2026-11-02): the `issues_folder` (`proposals_folder.py:41`) and `ledger` (`:32`) result change and every caller switch in one commit, with no `Path | None` adapter. Callers at 2f49beff4: `update_registration.py:32`, `tidy_wrapped.py:52,89`, `footer_counts`, `proposals_table`, `next_item`, `change_review`, `review_links/change_browser.py`, `review_links/change_page.py` and `rename_unit.py:1081`. `rename_unit.py` is herdr-unit's file: before dispatch, send the overlap to the showrunner and record its answer.
- Phase 4j (d671740ef): two more `issues_folder` callers switch in the same commit: `update_registration.question_folder` (`update_registration.py:813`) and `tidy_wrapped.can_hold_questions` (`tidy_wrapped.py:52`). `footer_counts` reads questions through `question_folder`. With no Proposals folder, `question_folder` gives `<state>/<showrunner staff id>/questions`; keep that result.
- Phase 4i (2f49beff4): `WRAPPED`, the wrapped status line, is defined once in `production_units.py:27`. `tidy_wrapped.py`, `staff_sources.py` (two function-level imports) and `staff_test_support.py` import it there. This phase does not move it. A phase that rebuilds or deletes `production_units.py` (herdr-unit 4c) moves `WRAPPED` to one leaf module and switches all three consumers in the same commit. That module must not import `update_registration` or `tidy_wrapped`: the 4i gate found that import cycle.
- Phase 3h review (2026-11-01): `add_unit` stops writing the `> **Production:**` header only after 3u's header readers switch; 4b deletes the headers.

**Acceptance gate:** the item 1 search finds only the excluded files, read-only `session_labels.py`, and `production_units.WRAPPED` imports. It may also find the Units scope reads that item 3 keeps until Phase 4b. Name each remaining match and its owning phase in the checkpoint notice. The dispatch search includes `bump_gate`, `dailies_input`, `live_units`, `showrunners`, `staff_sources`, `unit_lookup`, `unit_table`, and `worker_admission`. Each joins this phase or has a named owning phase. No other match passes. Use different timer and row documents to prove that every changed reader takes the row's document. Keep `test_checkpoint_uses_the_plan_worktree_when_the_director_runs_elsewhere`, the lookup tests, requested-checkpoint tests, and partial-report tests. Tests distinguish `ProposalsUnavailable` and `LedgerUnavailable` from confirmed absence. Require `0 errors, 0 warnings, 0 notes` from `basedpyright` on changed files and their callers. Each changed module's suite passes. A real `merge_gate.py --tests-only` and `add_unit.py --check` use the correct director and branch folders. A real `resume_unit.py sdlc guild-process-unit --dry-run` passes. The full gate passes. Only reads change.

**Acceptance gate (checker):** `test_staff_check.py` runs with no Units table and no timer doc metadata; rows, git and `.unit.toml` supply current facts. A remote read failure stays uncertain; confirmed absence remains a different diagnostic. This test must pass before 4c removes tables or 4d removes their parser.

### Phase 4a — Run owner and showrunner timers keyed by staff id  · status: todo

Source: herdr-unit plan Phase 4a, given to devtools-unit by sdlc 2026-11-02. Starts after herdr-unit's 3u and 3z.

**Depends on:** 3t (the doc from the row), 3z. Records stage of the rename track.

#### Work Order

**Goal:** the two largest name-keyed stores hold the staff id: the run owner in progress history and the update timer folders.

**Spec:** The same rule as Phase 3x, one store per landing with its own real proof, for:
1. **Run owner** (`progress_history_state.json`; find all readers with `grep -rln progress_history_state scripts`): new runs record the unit's staff id; old runs keep their names as history. Use one source for each folder kind: the director folder comes from staff; the branch folder uses `unit_lookup.plan_worktree`. A relative Plan uses the director folder; an absolute Plan uses its existing folder's Git root. The worker folder comes from its run record, not staff (`progress_history.py:475,484`). Test three distinct folders through migration: the director folder, the absolute Plan's branch folder, and the worker's run folder. Keep the worker's folder and thread. A completed rerun changes no stored bytes and preserves all three folder roles.
2. **Showrunner timers** (`~/.local/state/notifier/showrunner-<slug>/`, written by `update_registration.py`, read by `notifier.sh`, `statusline.sh`, `showrunners.registered_showrunners` and about 37 files): the folder is `showrunner-<staff id>`, and its `CHECK` line passes the staff id, so `production_check.sh` reads the doc from the row. The timer keeps its session, socket, zone and interval (decision 16), and `showrunners.timer_for` reads the folder by staff id, so its doc join (Phase 3s) goes. The search for readers covers `commands` and the Codex mirror too (`produce.md`'s `UPDATES`). The messaging unit owns `notifier.sh`; agree it first. Two question readers take meaning from the folder name and switch in the same landing (Phase 4i review, 2026-11-02): `scripts/hooks/showrunner_footer.py:112` takes the production slug from the timer folder name, and `scripts/production/tidy_wrapped.py:87` refuses cleanup while `showrunner-<slug>` exists. After the migration, tests show that cleanup still refuses while the production has a timer, the footer reminder and question counts stay the same, and a deferred question still expires on time.

**Files:**
- `scripts/hooks/showrunner_footer.py`, `scripts/production/tidy_wrapped.py`, `scripts/hooks/test_showrunner_footer.py`, `scripts/production/test_tidy_wrapped.py` — the timer-dependent question readers and their tests.
- `scripts/direct/progress_history.py`, `scripts/production/update_registration.py`, `scripts/message/notifier.sh`, `scripts/production/production_check.sh`, `scripts/production/showrunners.py`, `scripts/statusline/statusline.sh`, `scripts/production/rename.py` — the two stores' writers and readers; every other reader that a search for the two paths finds joins them.
- `scripts/direct/test_progress_history.py`, `scripts/production/test_update_registration.py`, `scripts/message/test_notifier.py`, `scripts/production/test_showrunners.py`, `scripts/statusline/test_statusline.py`, `scripts/production/test_rename.py` — their tests.
- `scripts/production/update_registration_test_support.py` — fixtures.
- `scripts/lib/unit_runs.py`, `scripts/lib/test_unit_runs.py` — the active run record (Phase 3h review).

**Workers:** `1 writer + 1 tester`
- `impl` — the non-test files; hub: `scripts/production/showrunners.py` (`registered_showrunners`, the timer reader every caller uses).
- `test` — the tests and fixtures.

**State:** timer folders and the progress state move to staff-id keys, once.

**Constraints from prior phases:** keep the Phase 3x migration pattern. Phase 3t reads the document from the showrunner row. Fast-track `3ac207239` permits a worker's separate worktree. Staff records the director folder; each worker's run record holds its location. Preserve the external worker's location and thread through migration. Phase 4p resolves relative question folders from the staff owner's document through `update_registration.question_folder`. Unreadable ownership is not confirmed absence. Keep the linked-checkout, ambiguous-owner, and shared-question tests. Staff rows hold canonical unit names from `unit_names.name`. A missing absolute Plan folder is unavailable (`staff.StaffUnreadable`; `unit_lookup.py:107-108`). A requested checkpoint refuses with the unit and folder; unrelated rows do not block it (`merge_checkpoint.py:277-306`). Read-only consumers report the bad row and keep the other rows. Keep `test_unit_lookup.py`, the requested-checkpoint regressions in `test_merge_checkpoint.py`, and the partial-report regressions.
- Item 1 covers active run records and their folder and branch consumers. The run points at the unit's fixed id. The director folder comes from staff; the branch folder uses the shared Plan rule. Read the branch from Git in the branch folder. Preserve each worker's run-record folder; finished runs keep their recorded paths as history. Run-folder paths stay the same across `/clear`. Keep the native-session and socket resolution that shipped.

**Acceptance gate:** each store has its own landing and real proof under the Phase 3x rule. Every running showrunner's timer fires once after migration; dailies and the status line show it. A migration stopped after the first timer folder completes on rerun. Three distinct director, Plan, and worker folders survive migration and a no-change rerun. Keep the lookup, requested-checkpoint, and partial-report tests. The full gate passes.

**Constraints from prior phases (native ids):** Phase 3h changes a live id and child links only in one locked atomic `staff.json` write. Add no store registry, identity alias or live-rekey journal. Parked files and retired refs belong to stopped units. The one-time identity migration writes reference stores first and staff last; it is safe to rerun and is deleted in Phase 18. This store conversion must test a clear between its reads: resolve current runtime ownership again, report unavailable old-id evidence, and make no unproved write. Do not add a second row to recover an old store key.
