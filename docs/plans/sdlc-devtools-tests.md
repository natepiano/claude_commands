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
- Local changes. Index and working-tree judgments compare against the same combined tree; no HEAD-to-tree supplement exists. The tree/index count is never above the `--base HEAD` count for the same local change; a committed removal plus an unrelated local removal stays 2 blocking.
- Owners and rulings. The printed owner is HEAD's current test name. A ruling clears exactly that occurrence; an identical assertion in another test stays blocking (identical assertions in `test_a` and `test_b`, `test_a`'s removed in a commit and `test_b`'s locally, a ruling for `test_b`: `test_a`'s removal stays blocking). The count limit is over the final reported blockers, after `aggregate_test_removals` and rulings.
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

**Acceptance gate:** from the worktree root, `python3 -m unittest scripts.direct.test_assertion_guard scripts.direct.test_assertion_guard_revisions scripts.direct.test_assertion_guard_strings scripts.direct.test_verify_assertion_guard scripts.direct.test_verify_assertion_guard_fake scripts.direct.test_verify_lint_guard_key` green, with (a)-(f) red on 4b353c42; `basedpyright` on each slot's files `0 errors, 0 warnings, 0 notes`; the director reruns the Inspectors and cargo-liner figures above and puts them in the checkpoint notice.

### Phase 7d — Small fixes fast-track to main  · status: todo
**Depends on:** Phase 8 (the one config reader; 7d lands after it and reads through it)

Source: the user via the sdlc showrunner, 2026-10-31: "yes i want a fast track" for fixes a unit director could make "in 2 minutes", and "a fast track can be followed by the standard process just to make sure things are in good shape". Showrunner ruling 2026-10-31 on devtools-unit's 10-line design: accepted, with these changes: the fast check never uses `merge_gate.py` and never waits on a queue or a running gate (target: minutes); a lock is held only for the push; settings.json, hooks and `scripts/lib/py` are refused; the nightly stays: a red run messages the sdlc showrunner by the existing path, and the showrunner acts and tells the user (showrunner ruling 2026-10-31 dropped the user push and Phase 7c); devtools-unit edits the rule lines in its own checkpoint (`--also` in the notice).

#### Work Order

**Goal:** one command lets a unit director push a small fix to `~/.claude` main in minutes, and the same fix then gets the unit's next blind review and the next merge gate with no step a director can skip.

**Spec:**
- Command: `~/.claude/scripts/lib/py ~/.claude/scripts/production/fast_track.py --unit <unit> --production <production doc> --why "<one line>" <path>...`, run from the director's own `~/.claude` worktree. `--production` is the doc named in the unit's plan header; its `Promote:` field gives the promotion targets (director's call, 2026-10-31: the approved shape had no way to find them). It takes the uncommitted changes of the named paths against HEAD (new files included). It refuses, exit 2 and nothing pushed: no change, a path outside the repo, a refused path, a limit exceeded.
- Config `config/fast_track.conf`, read at use through `scripts/lib/conf.py` (`conf.whole`, `conf.words`), a limit comment directly above each number key that matches its read (`# whole:` for `conf.whole`, as on `max_changed_lines`, `max_files` and `max_push_attempts`; `# range:` for `conf.number`), no defaults in code; a key `conf.py` cannot supply (`conf.Unusable`) refuses and names the key. No second reader and no copied parsing: `max_changed_lines`, `max_files`, `check_time_limit_seconds`, `max_push_attempts`, `refused_paths` (globs). Refused paths: `settings.json`, `scripts/hooks/`, `scripts/lib/py`, the staff record and every script that writes it, the account move, rename and shutdown scripts, `scripts/production/merge_gate.py`, `scripts/production/merge_checkpoint.py`, `scripts/direct/verify.sh`, `scripts/production/build_hold.py`, `scripts/production/fast_track.py` and `config/fast_track.conf`. Name each exact path in the summary.
- Copy: fetch origin main, `git clone --shared` into a temporary folder (its own git directory; never a worktree, never a copied `.git` pointer), check out origin/main detached, `git apply --3way` the patch. A conflict refuses with nothing pushed.
- Fast check, in the copy, under `check_time_limit_seconds`, with no build queue, memory queue or merge gate: (1) each changed Python file's direct test modules (`<dir>/test_<stem>.py`, `<dir>/test_<stem>_*.py`, and a changed test module itself), each run the merge gate's way from the copy root: `python3 -m unittest discover -s <dir> -p <file>` in the environment `merge_gate.test_module_environment` builds (`merge_gate.py:632`; import it, do not copy it, and never run the gate or its queues), with `HERDR_BIN=/nonexistent/herdr` and without `PYTHONUNBUFFERED`, `REFUSAL_WATCH_INVENTORY`, `STAFF_OPERATION_FD`; (2) an import of each changed module as a package (`python3 -c "import scripts.<pkg>.<mod>"` from the copy root); (3) `basedpyright <changed Python files>`, passing only on the `0 errors, 0 warnings, 0 notes` footer. No full-suite fallback. Over the limit: stop the check's process group and refuse with "over the fast-track time limit; use the normal path".
- Commit in the copy: subject `fast-track(<unit>): <why>`, trailer `Fast-Track: <unit>`.
- Push: one short lock, `<git common dir>/main-push.lock`, held only from the final fetch to the end of the push; `git push origin HEAD:main`, fast-forward only. When origin/main moved: fetch, re-apply, rerun the fast check, push again, at most `max_push_attempts` times. The fast check runs outside the lock.
- The lock in `merge_checkpoint.py` (one helper, one place, used by both): `push_git` fetches and merges origin/main into the merge branch, and `promote_local_checkout` then fast-forwards main to that tip and pushes it. A fast fix that lands between those two steps makes the tip lack it, so the main push is refused (current code, `merge_checkpoint.py:867-976`). So `run` and `run_handoff` hold the lock from `push_git`'s fetch of origin/main (or the start of `push_validate`) to the end of promotion's last `git push origin main`, or to the end of the merge-branch push when the production has no promotion. Tests run before that span, so they are never held. Name the measured span length in the summary.
- Promote: the existing path in `merge_checkpoint.py`. Split the checkout and Mac loop of `promote()` into one function that takes the promotion, the branch and the tip, and call it from `promote()` and from `fast_track.py` with branch `main` and the pushed commit. `promote_local_checkout(checkout, "main", tip)` fast-forwards each local checkout and finds nothing unpushed; `pull_mac_checkout` pulls the Mac. `merge_gate.py --promote-only --merge-branch main` refuses this case (`merge_gate.py:1358-1366`: local main behind origin/main) and is not used; do not edit `merge_gate.py`. A promotion failure after the push prints what failed and exits 1; the push stays, and a rerun promotes again (`already on main` path).
- Phase 8 leftover (2026-11-02 landing check): `scripts/direct/implement.sh:199` and `scripts/direct/review.sh:599` still parse `direct.conf` with `sed` (last value wins, `UNIT_CONFIG` override). Switch both to `conf_value direct UNIT_COMPACT_ABOVE_TOKENS` from `scripts/lib/conf.sh`, drop `UNIT_CONFIG`, and drop the `UNIT_CONFIG` env entries from `scripts/direct/demo_refusals.sh`, `implement_launcher_test_support.py`, `review_launcher_test_support.py` and `test_assign_launcher.py`. `MAC_TEST_CONFIG` stays: `build_hold/override_guard` still names it.
- Worktree: after a successful push, restore the named paths in the director's worktree to HEAD (new files removed): main has them, and the next merge of the merge branch brings them.
- Record and rerun: `${FAST_TRACK_STATE_DIR:-~/.local/state/fast-track}/records.jsonl`, one row per push: patch sha256, unit, commit, why, UTC time, `review: pending`. A rerun of a pushed patch, or a rerun after the restore, runs the promotion again (no change when it is done; it finishes a promotion that failed), prints `already on main at <short commit>` and exits 0.
- Follow-up review, automatic: `scripts/direct/review.sh` adds the unit's pending fast-track commits' diffs to every broad review prompt it sends for that unit (`fast_track.py --pending --unit <unit>` prints them), and when that review pass finishes it marks those rows `reviewed` with the pass. A finding goes through the normal ledger and repair. The merge gate needs nothing new: merge branches take main at their next push.
- One exception in one place: new `commands/unit/fast_track.md` defines `<FastTrack/>` (who may use it, the command, the limits by config key, the follow-up). Add it to the TagReferenceContract table in `commands/unit/direct.md`, and add "except <FastTrack/>" to: `commands/unit/direct.md` CoreContract lines 155-156 (no implementation code), 157-161 (no other commit) and 164-165 (never the default branch); `docs/production_format.md` ProductionUnit item 4, lines 318-319; `commands/showrunner/produce.md` line 1162.

**Files:**
- `scripts/production/fast_track.py` — the command and `--pending`
- `config/fast_track.conf` — limits and refused paths
- `scripts/production/merge_checkpoint.py` — the shared main-push lock around promotion's main push
- `scripts/direct/review.sh` — pending fast-track diffs in broad reviews; rows marked reviewed
- `scripts/direct/implement.sh` — reads `UNIT_COMPACT_ABOVE_TOKENS` through `conf.sh`
- `scripts/direct/demo_refusals.sh`, `scripts/direct/implement_launcher_test_support.py`, `scripts/direct/test_assign_launcher.py` — no `UNIT_CONFIG`
- `scripts/production/test_merge_checkpoint_bump.py`, `scripts/production/merge_checkpoint_test_support.py` — the saved patch's bump cases and fixture
- `commands/unit/fast_track.md`, `commands/unit/direct.md`, `docs/production_format.md`, `commands/showrunner/produce.md` — the one exception
- `scripts/production/test_fast_track.py` — the command's tests
- `scripts/production/test_merge_checkpoint_promotion.py` — the lock in promotion
- `scripts/direct/test_review_launcher.py` — the pending diffs and the reviewed mark

**Workers:** 1 writer + 1 tester
- `impl` — `fast_track.py`, `config/fast_track.conf`, `merge_checkpoint.py`, `review.sh`, `implement.sh`, `demo_refusals.sh`, the four docs
- `test` — `test_fast_track.py`, `test_merge_checkpoint_promotion.py`, `test_merge_checkpoint_bump.py`, `merge_checkpoint_test_support.py`, `test_review_launcher.py`, `review_launcher_test_support.py`, `implement_launcher_test_support.py`, `test_assign_launcher.py`, from this Spec alone

**State:** `~/.local/state/fast-track/records.jsonl` (rows per push); `<git common dir>/main-push.lock`.

**Constraints from prior phases:** the timeout cleanup signals its process group only through `process_group.send_process_group` (the guard `unguarded_group_signals` in `scripts/lib/test_process_group.py` must stay empty). Changes to `push_git` and `push_validate` keep the before-merge rollback of an unpublished tip at `scripts/production/merge_checkpoint.py:870`. The refused-path inventory includes the restoration writers. The environment helper is `merge_gate.test_module_environment` (`merge_gate.py:632`); it does not set `CONF_DIR`. The command, config, command docs and tests exist as uncommitted work in the worktree; the `merge_checkpoint.py` part is saved as a patch in the run's phase folder; keep both. Landing order (Phase 8 landing, 2026-11-02): `config/fast_track.conf` lands on main in its own commit first, and both machines' installed config holds it before the consumer's merge gate runs. Config contract from Phase 8: every test that reaches a conf read sets `CONF_DIR` to a fixture folder; fixture keys carry the real limit comments; a history-recovery fixture has its own committed git repository; `conf` loads as `conf` and as `scripts.lib.conf`, so a handler catches the producer's `conf.Unusable` or its translated domain error. Acceptance adds the process-group guard test and the unpublished-tip rollback regression.

**Acceptance gate:** from the worktree root, `python3 -m unittest discover -s scripts/production -p test_fast_track.py`, the same for `test_merge_checkpoint_promotion.py`, and `python3 -m unittest discover -s scripts/direct -p test_review_launcher.py` green, covering: each refusal and its exit 2, each config key missing and invalid, a conflicting patch, origin/main moving between check and push (re-apply, re-check, push), the time limit, a rerun (exit 0, no change), the worktree restore, the record row, `--pending`, and a review pass marking rows reviewed. `basedpyright` on each slot's Python files `0 errors, 0 warnings, 0 notes`. Live, by the director: one real small fix through the command to main, then the same command again (exit 0, `already on main`); a fast fix landing while a real `merge_checkpoint` runs, with that merge's promotion still finishing, and the showrunner's 08:54 gate result as the second case. Put the timings in the checkpoint notice.

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

Source: the sdlc showrunner, 2026-10-31. Phase 7f review found that `unbind_run()` and `list_roles()` in `design_roles.py` read every record strictly. One damaged record of any unit stops a run's cleanup unbind, and `list` prints the error on stdout.

#### Work Order

**Goal:** a run's cleanup unbind and the role list are not stopped by another unit's damaged record.

**Spec:**
1. `unbind_run()` uses the holder-aware scan from Phase 7f: another holder's damaged record is skipped with the one named stderr line; a damaged record bound to the run being unbound still refuses by name.
2. `list_roles()` skips a damaged record with the same one named stderr line, and nothing about it goes to stdout.
3. A rerun changes nothing: a second `unbind-run` of the same run exits 0 and writes nothing.
4. From Phase 7f review (F003): a `rebind` repair interrupted with no transition marker, whose retry then fails to publish the census, must not report success on the next retry: the retry finishes the census publication first (`design_roles.py:993`).

**Files:**
- `scripts/astra/design_roles.py` — `unbind_run()`, `list_roles()` and the markerless repair retry
- `scripts/astra/test_design_roles.py` — regression tests

**Workers:** 1 writer + 1 tester
- `impl` — `design_roles.py`
- `test` — `test_design_roles.py`, from this Spec alone

**State:** design-role records (tests use a temporary state root only).

**Constraints from prior phases:** `unbind_run` (`scripts/astra/design_roles.py:1480`) already reads through `_read_designs` and has tests that skip another unit's damage; this phase keeps that, refuses matching damage also when no role is live, fixes the `list` diagnostics, and completes markerless publication (the early return at `design_roles.py:981` remains). `rebind` (`:949`) takes `Holder | None` and `Binding | None`, where absence means "keep the current value" and also decides whether damaged-record repair is allowed: replace both with explicit keep/change types (convert the argument parser's absence at the CLI boundary) and test that keeping a binding differs from an explicit unbind. Acceptance adds: matching damage with ended or unopened roles is refused; a markerless retry that publishes the context, fails the census publication, then retries again ends published.

**Acceptance gate:** tests with a temporary state root and a planted damaged record of another unit show that `unbind-run` unbinds this run's records, prints one named stderr line, and exits 0; a rerun writes nothing; `list` prints the named line on stderr and nothing about the record on stdout. Also: matching damage with no live role refuses by name; the markerless retry publishes the census before it reports success; the CLI cases for `KeepHolder`/`ChangeHolder` and `KeepBinding`/`ChangeBinding` stay green.

### Phase 7h — The group-kill guard reads commands, not text  · status: todo
**Depends on:** Phase 7e (same test file; it checkpoints first)

Source: Phase 7e closure review (F002), moved here under the landing rule (models-unit waits on Phase 7e; the guard is a backstop, and no such call is in the tree). The guard in `scripts/lib/test_process_group.py` matches text with regular expressions. Each review round found one more form that it lets through: `kill -TERM -- 0` inside an `if`/`else` arm (line 43), and a Python heredoc that binds `send = platform.killpg`, then calls `send(pid, 15)` (line 116).

#### Work Order

**Goal:** the guard finds every group signal outside `process_group.py` by the meaning of the code, not by its spelling.

**Spec:**
1. Python files and Python text inside other files are parsed with `ast`: any reference to `killpg` (attribute or name, through any alias or assignment) outside `scripts/lib/process_group.py` fails the guard.
2. Shell text is split into commands with a real shell tokenizer (`shlex` in punctuation mode), so a `kill` in any command position (after `;`, `&&`, `||`, `then`, `else`, `do`, a pipe, or inside `bash -c '…'`) is found; a target of `0`, `-0`, or a negative group fails the guard unless the command is the process_group CLI.
3. Text the guard cannot parse fails the guard with its path, so a new form is refused, never let through.
4. Each form named in the Source, plus each form of Phase 7e's F002, is a planted case in a temporary tree that fails the guard. The real tree passes, `scripts/lib/conf.sh` included.
5. Scope (Phase 8 review): the guard already uses `ast` and `shlex` punctuation mode. Complete it, do not replace it: executable Python fragments inside other files, command-position gaps, and a shell parse error that today returns false. Literal fixture data stays allowed.

**Files:**
- `scripts/lib/test_process_group.py` — the guard and its planted cases

**Workers:** 1 writer
- `test` — `test_process_group.py`

**State:** none (a temporary tree only; no signal is sent, `os.killpg` stays patched).

**Constraints from prior phases:** the guard exempts only the one send inside `send_process_group` in `scripts/lib/process_group.py`, not every reference in that file; its planted fixture already rejects a second raw call there, and that stays. Keep the shell-helper failure and fallback tests. The caller baseline is da15fa97 (codex_mesh, signal_test_support, herdr_upkeep and repeat_guard all route through `send_process_group`). Plain Python AST detection and several zero and nested-shell cases already exist; a read-only probe shows the shell `else` branch case still escapes.

**Acceptance gate:** `python3 -m unittest discover -s scripts/lib -p test_process_group.py` passes on the real tree, and every planted case named in the Spec fails the guard.

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
**Depends on:** Phase 8 (the publish path it repairs)

Source: Phase 8 architect review, 2026-11-02 (finding 2). After a rejected `/tune` or `/zone` push, the publish commit stays on local main. When another writer then advances origin/main, `main_publish.ready()` refuses the next run: `merge --ff-only origin/main` cannot take a diverged main (`scripts/lib/main_publish.py:95-105`). The approved change then needs a hand repair. This is a defect in what Phase 8 shipped, so it runs next.

#### Work Order

**Goal:** a rerun of an approved `/tune` or `/zone` change after a rejected push publishes it with no new approval and no duplicate commit, also when origin/main moved in between.

**Spec:**
1. `ready()` accepts a local main whose only commits beyond origin/main are publisher commits (subjects `tune(`, `zone(`, `user zone: `, the same set `push()` accepts). It rebases those commits onto the fetched origin/main. Any other unpushed commit, or a rebase conflict, refuses by name and leaves main as it was.
2. A rebased commit whose change origin/main already holds drops out, so a same-value retry makes no duplicate commit.
3. The Mac pull queue and the `already` rerun keep their Phase 8 behavior.

**Files:**
- `scripts/lib/main_publish.py` — `ready()` recovery
- `scripts/lib/test_main_publish.py` — regression tests in a temporary repository

**Workers:** 1 writer + 1 tester
- `impl` — `main_publish.py`
- `test` — `test_main_publish.py`, from this Spec alone

**State:** none (tests use temporary repositories and `CONF_DIR` fixtures).

**Constraints from prior phases:** tests set `CONF_DIR`; never read the installed config.

**Acceptance gate:** `python3 -m unittest discover -s scripts/lib -p test_main_publish.py`, `-p test_tune.py` and `-p test_zone.py` green, covering: a rejected push, then another writer's push to origin, then a same-value retry that publishes with one commit and no approval; a non-publisher unpushed commit refused; a rebase conflict refused with main unchanged. `basedpyright` on each slot's files `0 errors, 0 warnings, 0 notes`. Live, by the director: the real `/tune` command and a real rerun. Showrunner limits 2026-11-02: the smallest fix, no new lock or design; a refused or conflicting commit gives one clear error. Runs in parallel with 7d (disjoint files).

