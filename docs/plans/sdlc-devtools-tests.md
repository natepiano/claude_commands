# sdlc-devtools-tests

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Make the `~/.claude` repo tests run in every gate, faster, and with less CPU. Phase 1 fixes the gate's test list. Phases 4-6 come from the measured inventory of full gate 3 (2026-10-30).

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
- **Test:** `python3 -m unittest discover -s scripts/production -p 'test_merge_gate*.py'`, from the worktree root.
  Targeted test: `env HERDR_BIN=/nonexistent/herdr python3 scripts/production/targeted_test.py --repo . --full`, from the worktree root.
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

### Phase 7g — Run cleanup and the role list skip another unit's damaged record  · status: done

#### As-built

`unbind_run` reads with `_read_designs(..., refuse_matching_damage=True)`, so a damaged record that matches the run refuses by name even with no live role. `list` prints each record problem on stderr; plain stdout holds only roles or `no held roles`, and `--json` keeps `problems`. A markerless `rebind` retry returns early only when the census lists every open role thread. The keep/change type refactor was dropped (sdlc, 2026-11-04: delete more than you add). Checkpoint d2b280ea2 on `sdlc-devtools-roles`.

**Files:**
- `scripts/astra/{design_roles.py,test_design_roles.py}` — the three defects and their regressions.

### Phase 7h — The group-kill guard reads commands, not text  · status: done

#### As-built

Dropped, not built (sdlc, 2026-11-04): it grows a guard, and a phase deletes more than it adds.

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
- The phase test list does not cover every reader caller. The gate for a reader change is `full_test.py --checkout <tree> --full`.

**Ruled out:** a fallback reader for a missing staff file; a walk up from a missing Plan folder to a parent checkout.

### Phase 4v — Folder moves name what blocks them  · status: done

#### As-built

- `move_folder` stops the session, pauses mesh runs without losing queued messages, moves the Git worktree, publishes staff, and resumes only its stopped session.
- `folder_move_lock` serializes moves per staff row; `staff.set_worktree(..., expected=row.worktree)` checks the published location. Git rejects an occupied target; a failed Git move resumes the stopped session.
- Claude signals use the `session_liveness` pid in the target pane. Resume requires `SessionNotLive` and a shell; unproved targets receive only a manual command.
- Each selected history reaches `move_run`; each paused seat retains its task prompt and one move note. Other failures print one line.
- `request_from` reads no staff. The commit check tries the production checkout first; an unavailable folder omits `merge_gate_wait` without stopping tests.
- `merge_gate.py` assigns private `STAFF_FILE` and `HERDR_PANE_RECORD_DIR` paths. Mesh fixtures use a stub with leak checks; the CPU regression uses a reference workload.

**Files:**
- `scripts/production/unit_move.py`, `scripts/shutdown/pane_record.py` — folder movement and launch-folder handling.
- `scripts/production/merge_checkpoint.py`, `scripts/production/add_unit.py` — deferred staff lookup and launch-gate deletion.
- `scripts/production/merge_gate.py`, `scripts/production/staff.py`, and their fixture tests — private state and test-root write protection.

**Binds later work:** Checkpoints merge into the unit's staff parent preserves deferred folder reads. Production tools read the row and `.unit.toml` preserves per-row locking and expected-location writes. Run owner and showrunner timers keyed by staff id has no move journal, process scan, identity-proof subsystem, or direct worker-resume waits.
**Gotchas:** A crash rerun checks the destination's Git common directory against the repository named by staff metadata. It starts no session automatically.
**Ruled out:** Move journals, folder process scans, worker relaunch proofs, and `FOLDER_MOVE_*` waits.

### Phase 4w — Folder paths live only in the staff row  · status: done

#### As-built

- Staff supplies unit and worker folders; absolute Plan paths do not select another checkout. Run, pane, recovery, and resume readers ignore legacy folder copies.
- Claude transcripts and Codex threads use session ids. Codex turns read the current staff folder; transcripts stay in place.
- `launcher_command` remains task state. Resume replaces its first three task arguments and preserves task options.
- The move restarts the sessions in the folder. A completed rerun changes no records and starts only a down director.

**Files:**
- `docs/plans/sdlc-devtools-path-copies.md` — record writers, readers, and retained task fields.
- `scripts/production/{unit_lookup,unit_move,rename_unit,transfer_unit}.py`, `scripts/direct/{plan_history,progress_history}.py` — staff-folder joins without path-copy journals.
- `scripts/shutdown/`, `scripts/agents/codex_mesh.py`, `scripts/lib/resume_message.py` — identity-based discovery and current-folder launch behavior.

**Binds later work:** Units tables have no Worktree column and Checkpoints merge into the unit's staff parent use staff-only folders. Small stores keyed by staff id and Production tools read the row and `.unit.toml` use the same folder source. Run owner and showrunner timers keyed by staff id keeps no folder copy. Transfer preserves the move's restart behavior.
**Gotchas:** `SessionNotFound` and `NotRunning` do not prove the saved pid stopped; restart checks that pid. `unchanged` and `resumed` distinguish move rerun outcomes.
**Ruled out:** Absolute-Plan routing, worker-folder copies, transcript relocation, rename journals, and old-name sweeps.

### Phase 4n — Reviewer naming reads the staff folder  · status: done

#### As-built

`workers._reviewer_destination` reads the unit folder through `plan_history.staff_directory` with one staff snapshot. The launcher `--working-dir` stays the fallback for a run with no unit row. Checkpoint 5535d493e on `sdlc-devtools-reviewer`, off main b4cff7692.

**Files:**
- `scripts/direct/{workers.py,test_workers.py}` — the reviewer folder read and its fixtures.

### Phase 4g — Both promotions use one rule for local edits  · status: done

#### As-built

`merge_gate.promotion_edit_conflict(repo: Path, target: str) -> str` reads staged edits and worktree edits through a fresh index. Local and Mac promotion and `merge_checkpoint.promote_local_checkout` reject only edited paths changed between `HEAD` and the target before `git merge --ff-only`. The fake Git helper in `test_fast_track.py` releases its commit FIFO in `finally`.

**Files:**
- `scripts/production/{merge_gate,merge_checkpoint}.py` — shared promotion rule and callers; their promotion tests cover both outcomes.
- `scripts/production/test_fast_track.py` — unconditional FIFO cleanup.

**Binds later work:** Checkpoints merge into the unit's staff parent and Production tools read the row and `.unit.toml` preserve the shared promotion rule.
**Ruled out:** Promotion refusal for every tracked edit.

### Phase 4f — The bump gate tests the pushed tip  · status: done

#### As-built

The bump gate tests the pushed tip in a detached clone. `tree_watch.py` records the first changed watched path beside its fired file. The cancellation line in `verify.sh` names that path.

**Files:**
- `scripts/direct/{tree_watch.py,verify.sh,test_tree_watch.py,tree_watch_test_support.py}` — changed-path reporting and its regression.

### Phase 4e — The change row date is in the user zone  · status: done

#### As-built

The merge gate uses `change_review.today()` for the user-zone date. Its test computes the expected date in the gate's fixture environment. The UTC-boundary regression checks that 01:00 UTC on November 4 writes November 3.

**Files:**
- `scripts/production/test_merge_gate.py` — fixture-zone expectations and the UTC-boundary regression.

### Phase 4b — 4w merge reds repair  · status: done

#### As-built

Fixture staff rows hold unit folders; add-unit assertions read staff and expect a dash in the legacy table. Watchdog, inventory, and identity-migration fixtures publish the staff facts their readers use. A missing staff file reads as empty staff; the missing-file merge-refusal test is absent.

**Files:**
- `scripts/{accounts/test_unit_watchdog,shutdown/test_inventory,production/test_add_unit,production/test_migrate_staff_ids,production/test_merge_gate}.py` — staff-based fixtures and expectations.

**Binds later work:** Checkpoints merge into the unit's staff parent and Production tools read the row and `.unit.toml` retain empty-staff semantics and explicit fixture locations.

### Phase 4d — Every Units table reader reads staff  · status: done

#### As-built

- Staff supplies membership and row order with canonical names from `unit_names.name`; `.unit.toml` supplies unit configuration, including Port and `nightly-review` responsibility; Git supplies the branch.
- `Production`, `read_production`, `production_field`, `Refusal`, and `WRAPPED` live in `scripts/production/production_doc.py`, without `update_registration` or `tidy_wrapped` imports.
- `UnitMetadata` retains metadata and `UnitProblem` diagnostics for partial reports. `transfer_unit.apply` changes only the staff parent under the per-row move lock.
- `unit_table.py` and `audit_unit_ports.py` are absent. `unit_phases.py` replaces `units_table.py`; `display_cells.py` replaces `unit_table_cells.py`. The style `split-all` command is absent.

**Files:**
- `scripts/production/{production_doc,unit_lookup,unit_phases,display_cells}.py` — document fields, staff facts, phase projection, and display cells.
- `scripts/production/`, outside readers, command text, and fixture builders — staff and `.unit.toml` consumers without table writes.

**Binds later work:** Units tables have no Worktree column and Checkpoints merge into the unit's staff parent use these sources. Small stores keyed by staff id and Production tools read the row and `.unit.toml` retain these module names. Run owner and showrunner timers keyed by staff id uses staff and `.unit.toml`.
**Gotchas:** `production_units.py` retains only `Refusal` and `production_field` re-exports for `merge_gate.py`. The table-removal helper and absolute-Plan resolver no longer exist.
**Ruled out:** A table-removal script, a second row-order field, and special unit-name constants for nightly review.

### Phase 4y — basedpyright exits 0 when the tree is clean  · status: done

#### As-built

`pyrightconfig.json` has no `venvPath` or `venv`; a clean checkout needs no `.venv`. `merge_gate` requires status 0 and a clean basedpyright footer; status 3 fails even with a clean footer. The gate retains its pinned `--pythonpath`. Tests cover statuses 0, 1, and 3, including a real clean file without `.venv`.

**Files:**
- `pyrightconfig.json`, `scripts/production/{merge_gate.py,merge_gate_test_support.py,test_merge_gate_suites_types.py,test_merge_gate_suites_tripwires.py}` — configuration and result checks.

**Binds later work:** Checkpoints merge into the unit's staff parent preserves status-0 judging and changes the separate `merge_checkpoint.test_passed` judge.
**Gotchas:** `merge_checkpoint.test_passed` still accepts status 3.

### Phase 4u — Units tables are gone  · status: done

#### As-built

No production doc has a Units table, and no tool writes one. `retire_unit.py` lost its Units row rewrite; main had already removed the other writers, and `unit_table.py` was gone. `docs/production_format.md` says the staff row holds identity, parent and worktree, and `.unit.toml` holds plan, port and responsibilities. No port needed a copy into `.unit.toml`. Checkpoint f26c05f0e on `sdlc-devtools`.

**Files:**
- `scripts/production/retire_unit.py` — the last Units row writer deleted.
- `docs/production_format.md`, `commands/producer/to_production.md`, `commands/showrunner/rename.md` — format text.
- `docs/plans/{builds,sdlc,natedev}-production.md` — Units tables deleted.
- `scripts/production/test_tidy_wrapped.py`, `scripts/style/test_style_plan_borrowing.py` — fixtures without Units tables.

**Binds later work:** the plan's one home is `.unit.toml`; staff holds no plan or port.

### Phase 4x — Checkpoints merge into the unit's staff parent  · status: done

#### As-built

`merge_checkpoint.request_from` reads the staff record once. `MergeRequest.staff_record` carries that snapshot to unit lookup, `record_publication_changes`, `change_review.publication_changes` and `change_review.foreign_units`. A unit whose staff parent is not the named production's owner refuses before any merge with `unit <name>: staff parent is <parent>; use that production`; a rerun changes nothing. `change_review` finds child productions through `showrunner_tree.descendants_of` with the same snapshot. `test_passed` accepts only status zero; the basedpyright status-3 acceptance is gone. Checkpoint f48d505ae on `sdlc-devtools-parent`.

**Files:**
- `scripts/production/{merge_checkpoint,change_review}.py` — one snapshot, the parent refusal, the status-zero judge.
- `scripts/production/{test_merge_checkpoint,test_merge_checkpoint_rerun,test_change_review,merge_checkpoint_test_support}.py` — the refusal, one-snapshot and status tests.
- `docs/as-built/showrunner-automation.md` — the judge text.

**Binds later work:** a Merge tests command must exit 0; a basedpyright line that exits 3 is red.

### Phase 4q — The folder move uses its owners' readers  · status: done

#### As-built

The affected account, checkpoint, merge-gate, status, and log fixtures publish staff rows and `.unit.toml` files instead of table rows. An unregistered producer stays empty and receives one note naming the checkpoint branch. Source APIs remain unchanged.

**Files:**
- `scripts/production/test_{add_unit_accounts,merge_checkpoint_rerun,merge_gate,producer_backfill,unit_status_dailies,unit_status_routing,update_registration_log}.py`, `scripts/production/unit_status_test_support.py` — staff fixtures and exact producer diagnostics.

**Binds later work:** Checkpoints merge into the unit's staff parent preserves the empty-producer diagnostic. Production tools read the row and `.unit.toml` preserves explicit staff and configuration fixtures.

### Phase 4c — A resumed showrunner reads its next list from the state file  · status: done

#### As-built

`load --resume` reads `showrunners.session_state_dir(slug) / "showrunner_state.json"` through `update_registration.judgment_state`. It prints the current `next` list or `no next list`; refused state produces one anomaly without stopping load. `showrunner-compaction.py` supplies only SessionStart text; no PreCompact writer or registration remains. `log --before-compaction` remains a manual command.

**Files:**
- `scripts/production/production_lifecycle.py`, `scripts/hooks/showrunner-compaction.py`, `settings.json`, and their tests — current state reading without a compaction write.

**Binds later work:** Run owner and showrunner timers keyed by staff id and Production tools read the row and `.unit.toml` preserve this state-file source.

### Phase 4z — Retire is a staff operation  · status: done

#### As-built

Retire stops the unit and worker staff rows by `current_session`. It refuses uncommitted work and commits missing from the merge branch; a sole added `Project started` line does not count. Resource releases and folder-process SIGTERM remain. `RemovedWorktree` records retirement on the retained staff row; resume by name or id restores that row from the merge branch. Rename uses the staff name without session-name or former-name selectors.

**Files:**
- `scripts/production/{retire_unit,resume_unit,rename}.py` and their tests — staff retirement, restoration, and naming.
- `commands/showrunner/retire_unit.md`, `docs/as-built/` — retirement behavior.

**Binds later work:** Production tools read the row and `.unit.toml` preserves retirement identity, lost-work refusals, the recorder-line exception, and resource releases. Small stores keyed by staff id uses staff-name selection.
**Ruled out:** Saved branch refs, retired notes, and `--wait-minutes`.

### Phase 4h — A folder move restarts a unit with no launch record  · status: done

#### As-built

`NoFolderLaunch` does not prevent stopping `current_session` through `stop_resume.stop`. After `staff.set_worktree`, a fresh snapshot supplies `restart.start_row(row, record, herdr.Herdr(herdr.shared_session()))`. This path restarts only a unit that was live before the move. A stop or start failure prints one `unit_move:` line without undoing the move.

**Files:**
- `scripts/production/{unit_move.py,test_unit_move.py}` — no-record stop/start behavior and its regression.

**Binds later work:** Checkpoints merge into the unit's staff parent and Production tools read the row and `.unit.toml` preserve this identity-based restart path.

### Phase 4l — Main sync sends a blocked state once per window  · status: done

#### As-built

`main_sync.py` keeps no blocked marker; `record()` only appends the log line. Each blocked run with `--notify` sends through `send.py --key main-sync-<host>-<state> --repeat-minutes <M>`, with `<M>` from `send.conf` `main_sync_repeat_minutes`. The fetch-failed text advises `github-warmup` only when `github-warm-status` fails, is missing or passes its 5-second limit; at exit 0 it names a likely GitHub incident. Checkpoint 5e796820d on `sdlc-devtools-sync`, off main 48dc96882.

**Files:**
- `scripts/lib/{main_sync.py,test_main_sync.py}`, `config/send.conf` — send-once through send.py and its regressions.

### Phase 4m — Assigning a unit a plan writes the staff plan  · status: done

#### As-built

The plan has one home, `.unit.toml`; staff holds no plan. `unit_config.set_plan` changes only the plan in the existing `.unit.toml`, and `progress_history.py start-run` calls it for a unit run with a plan doc. phases-unit edits its own `.unit.toml` for its as-built. Checkpoint a63d95e05 on `sdlc-devtools-plan`, off main 3ffe2193f.

**Files:**
- `scripts/production/unit_config.py`, `scripts/direct/{progress_history.py,test_progress_history.py}` — the plan write and its regression.

**Ruled out:** a staff `set-plan` operation.

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

### Phase 7n — The proposals table widths live in config  · status: done

#### As-built

`config/proposals_table.conf` holds `title_width = 110` and `state_width = 140`. `proposals_table.table` reads both through `conf.read("proposals_table")` at use; the `TITLE_WIDTH` and `STATE_WIDTH` constants are gone. Checkpoint fa006856e on `sdlc-devtools-width`.

**Files:**
- `scripts/production/proposals_table.py`, `config/proposals_table.conf`, `scripts/production/test_proposals_table.py` — the config reads and a test that a changed width moves the cut.

### Phase 7o — The simplify test calls a function, not a patched exec  · status: dropped

**Satisfied** (Phase 3y architect review, 2026-11-05): `assign.py:899` takes `exec_review`, and `test_simplify_lens.py:84` passes it; no test patches the global `os.execvpe`. The next item "A simplify lens test leaves no child process alive" stays.

Source: builds add-on natest F017, given by sdlc 2026-11-04. Settled `next`, after 3t.

**Review amendment (Phase 3t architect review, 2026-11-05):** `a26bb05b8` already did Spec item 1 (`assign.main` takes `exec_review`). Item 2 remains: the in-process `test_simplify_lens` test passes `exec_review` and patches no global `os.execvpe`. The add-on "a simplify lens test leaves no child process alive" (next list) touches the same test; take both together.

#### Work Order

**Goal:** `test_simplify_lens.py` stops patching the global `os.execvpe`.

**Spec:**
1. `assign.main` takes the exec function as a parameter (`exec_review`, default `os.execvpe`) and passes it to `execute_simplify`.
2. The `os.execvpe` patch is only in builds-natest's in-process test (`test_simplify_lens.py:143`, not yet on sdlc). That test passes its `run_review` as `exec_review`; the checkpoint notice tells builds.
3. Delete more than you add. Add no guards.

**Files:**
- `scripts/direct/assign.py`

**Workers:** `1 writer` — `impl`.

**Acceptance gate:** `test_simplify_lens` and the assign launcher suites pass; basedpyright `0 errors, 0 warnings, 0 notes`; one director gate.

### Phase 7p — Gate test modules run in their own roots  · status: dropped

**Dropped** (user via sdlc 2cbc67b8, 2026-11-05): "only code the things that i ask you to code"; a fix for a broken guard deletes the guard. The suite guard broke 43 modules in the combined gate and was removed from the combined branch (2620b3858). Its re-land branch `sdlc-devtools-guard` is abandoned. The test fixes that made modules use private paths stay.

**Review amendment (Phase 3t architect review, 2026-11-05):** the private roots and the live-write check look already shipped (`merge_gate.py:115,714-720`; `test_merge_gate.py:50,66,111`). The first step checks each Spec item against the tree; a met item is marked done here with its file and line, and only the rest is built.

Source: sdlc 2026-11-04 (urgent; builds and natedev agree). Two tests wrote live state: fixture rows into the live staff.json, and a 3o launcher test into models-unit's live roster.

#### Work Order

**Goal:** a merge_gate test module cannot write live state.

**Spec:**
1. `merge_gate.py` gives each test module its own HOME, XDG_STATE_HOME and `/tmp/claude` roots, set in one place (the suite bootstrap).
2. A test that writes under a live root fails and names the path. The check costs almost nothing per test.

**Files:**
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py`

**Workers:** `1 writer` — `item26`.

**Acceptance gate:** `test_merge_gate` passes, with a test for each rule; basedpyright `0 errors, 0 warnings, 0 notes`; one director gate. Branch `sdlc-devtools-isolate` off main.

### Phase 7q — Unit responsibilities carry the Owns paths  · status: dropped

**Dropped** (Phase 3t architect review, 2026-11-05): Phase 3t deleted the Owns readers on purpose; responsibilities come from `.unit.toml` and staff. No work remains.

Source: sdlc 2026-11-04 11:2x (urgent 4u defect, found by cargo-liner). Every `.unit.toml` has `responsibilities = []`: the 4u move did not carry the Units table Owns cells. `merge_checkpoint` reads responsibilities, so every scope-checked merge holds "outside responsibilities".

#### Work Order

**Goal:** each unit's `.unit.toml` holds its Owns paths, and an empty carry can never pass in silence.

**Spec:**
1. `unit_config.responsibilities_from_owns(cell)` turns one Owns cell into responsibilities. A cell that is not a path list (prose, a parenthetical file list) raises `ConfigRefused`. The cell `none — takes what the board leaves free` gives `(unit_config.BOARD_FREE,)`, the string `board-free`.
2. `owns_fill.py` copies each production doc's Owns cells into its units' `.unit.toml` through `unit_config.write`, keeps short names such as `nightly-review`, prints one line per unit, and refuses a prose cell with its production and unit. A rerun changes nothing.
3. A test fails when a non-empty Owns cell gives empty responsibilities.
4. `merge_checkpoint` refuses a unit with empty responsibilities, and the message names `owns_fill.py`. A `board-free` unit permits every changed path; the berth board is its guard.

**Files:**
- `scripts/production/unit_config.py`, `scripts/production/owns_fill.py` (new) — `item28`.
- `scripts/production/test_owns_fill.py` (new) — `item29`.
- `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint.py` — `item30`.

**Workers:** `3 writers` — `item28`, `item29`, `item30`.

**Acceptance gate:** `test_owns_fill`, `test_unit_config` and the `test_merge_checkpoint*` modules pass; basedpyright `0 errors, 0 warnings, 0 notes`; the live fill prints one line per unit on natedev and the Mac. Branch `sdlc-devtools-owns` off origin/sdlc 5e8bc5a52.

### Phase 7r — A merge reuses a gate that already passed its tree  · status: dropped

**Dropped** (sdlc 2026-11-04 13:1x): sdlc moves at each showrunner merge, so a unit's gated tree is almost never the merge tree. The store would add state to skip a gate that it almost never skips. Commit ab7870e7f stays off every branch; no replacement.

Source: sdlc 2026-11-04 11:5x (save gate time). A unit gates a tree that already has origin/sdlc merged in; the showrunner merge then runs the same full gate again.

#### Work Order

**Goal:** a merge whose merge tree already passed a full tests-only gate runs no second full gate.

**Spec:**
1. `merge_gate.py --tests-only` records each tree it passed: the git tree hash of the checkout and the time. One record store, read and written in one place.
2. `merge_checkpoint` test step: when the merge tree hash equals a recorded passed tree, skip the test commands and print `test: ok — tree <hash> already passed at <time>`. A tree with no record, or a record of a failed gate, runs the tests as now.
3. One test for each: a passed tree is recorded; a matching merge tree skips its test step with that line; a different tree runs the tests.

**Files:** `scripts/production/merge_gate.py`, `scripts/production/merge_checkpoint.py` and their test modules — `item34`.

**Workers:** `1 writer` — `item34`.

**Acceptance gate:** the merge_gate and `test_merge_checkpoint*` modules pass; basedpyright `0 errors, 0 warnings, 0 notes`. Branch `sdlc-devtools-passed` off origin/sdlc 5e8bc5a52.

### Phase 7s — review.sh keeps no tree check  · status: done (d32150dfb)

Source: sdlc [e0265011], 2026-11-05. The tree check failed a review for the third time with no reviewer write (2026-10-12, 10-15, 11-05: the director's own plan write ran during the review). User rule 2026-11-05: a fix for a broken guard deletes the guard.

**Depends on:** none. It lands as its own hash from the sdlc tip.

#### Work Order

**Goal:** `review.sh` makes no before and after tree fingerprint and reports no "changed the working tree" error.

**Spec:**
1. Delete the baseline and changes calls in `review.sh`, their variables, their error and board posts, and the status branch that the error sets.
2. Delete `review_tree.py` and `test_review_tree.py`.
3. Delete the tests that pin only the tree check. Keep every other test in those files.
4. Delete the tree check text in `docs/as-built/long-lived-workers.md`.
5. Add nothing in its place.

**Files:** `scripts/direct/review.sh`, `scripts/direct/review_tree.py`, `scripts/direct/test_review_tree.py`, `scripts/direct/test_review_launcher_trees.py`, `scripts/direct/review_launcher_test_support.py`, `scripts/direct/test_simplify_lens.py`, `docs/as-built/long-lived-workers.md`.

**Workers:** `1 writer`.

**Acceptance gate:** no file names `review_tree` or "changed the working tree"; the direct review suites pass; the full gate passes; source net negative.

### Phase 7t — Withdraw an open question whose cause is gone  · status: done (bb5854152)

Source: sdlc [e93bf7b3], 2026-11-05, for natedev. First user: report-unit's hanadocs sync question. It lands as its own hash on top of 4fa36df93.

**Depends on:** none.

#### Work Order

**Goal:** `update_registration.py withdraw --item <text>` removes one open item, records it in the question history as withdrawn, and prints no relay line.

**Spec:**
1. Move the open-item removal in `answer` into one function. `answer` and `withdraw` both call it. Do not copy it.
2. `withdraw` matches one open item by its exact text, in the same lists that `answer` reads.
3. `withdraw` writes the item to `<slug>.history.jsonl` with a `withdrawn` time. It writes nothing to `<slug>.answered.jsonl`.
4. `withdraw` prints no `relay:` line.
5. Add no other guard or check.

**Files:** `scripts/production/update_registration.py`, `scripts/production/test_update_registration_answers.py`, `commands/showrunner/produce.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/message-clarity.md`.

**Workers:** `1 writer`.

**Acceptance gate:** a withdraw test removes the item, writes the history record, writes no answered record, and prints no relay line; the answer tests pass unchanged; the production suite passes.

### Phase 7u — The production name is the showrunner's staff name  · status: done (829824277)

**Source:** the user, through the sdlc showrunner, 2026-11-05 [fe103398]: "the name of the showrunner is the name of the productino wherever production is used we should just use the showrunner's name. the units alrady know what is there place int he staff so the prodution header line doesn't nee to exist nor does the name of the production doc because that also comes from the staff record."

**Depends on:** Phase 3y. It lands as its own hash after 3t and 3y, in the clone branch `sdlc-devtools-prodname`.

#### Work Order

**Goal:** A production has one name: its showrunner's staff name. A unit finds its production from its staff row. No code reads a name from a doc file name or from a plan header.

**Spec:**
1. `row.name` of the showrunner row is the production name. Delete `showrunners.owner_slug`, `production_slug`, `canonical_slug`, `former_slugs`, `rename_history`, `MemberSources.canonical_slug`, the `Formerly` field reader, the legacy branch of `timer_name` and the doc-name branches of `owner_for` and `_source`.
2. `production_doc.read_production(name, record=None)` takes a staff name and finds the doc in the row. Delete `production_name` and `ProductionName`, which read the doc's title line. A type field `slug` that holds a production name becomes `name`.
3. Every `--production` option takes the staff name, with type `str`. A command that takes or prints a slug takes or prints the staff name. Doc file names do not change. A doc path comes only from `row.doc`.
4. A unit is a production unit when its staff row has a showrunner parent. Delete each reader of the plan's `Production:` header line. `add_unit` writes no such line. `/unit:direct`, `docs/production_format.md` and `docs/direct_plan_format.md` say this.
5. Remove the `Production:` header line from each plan in this repo. Every other repo gets one command through its showrunner. No commit goes into another showrunner's worktree.
6. A one-off program, outside the repo, moves state keyed by an old name once at deploy: notifier and state folders, ledgers, stored records and change-review Production cells. Only `tool-based-ui` to `hana` and `performance` to `fps` differ. The program is deleted after it ran on natedev and the Mac. No old-name reader stays in the code.
7. A plan's own slug, a model slug and other names that are not a production name stay.

**Files:**
- `scripts/`
- `commands/`
- `docs/`
- `config/`

**Workers:** `7 writers`: core, unit lifecycle, merge, registration and reports, other folders, style, docs. A seventh job writes the one-off program.

**Acceptance gate:**
1. `git grep -E 'owner_slug|production_slug|canonical_slug|former_slugs|rename_history'` finds nothing.
2. No code outside tests reads `-production.md` for a name, or a `Production:` plan line.
3. With a fixture whose doc is `tool-based-ui-production.md` and whose row name is `hana`, `merge_checkpoint.py`, `add_unit.py` and `update_registration.py` take `--production hana`, and their output and files say `hana`.
4. Every suite of the changed folders passes on the clone, then the full merge gate passes. Source net negative.

### Phase 7v — The simplify review starts as a child of assign  · status: done

**Depends on:** none

**Source:** the sdlc showrunner, 2026-11-05 [2edc2aea]: `assign.sh --role review --lens simplify` fails with "Start workers through assign.sh from the director session." Fix it as a fast hash on top of main, apart from 7u.

#### Work Order

**Goal:** `execute_simplify` starts `review.sh` as a child, as every other assign path does. No launch gate changes. The fixture stops replacing the start path, so a test now covers the real one.

**Spec:**
1. `execute_simplify` runs `review.sh` with `subprocess.run` and returns its exit code. Delete `ReviewExec`, the `exec_review` parameter and the `os.execvpe` default.
2. `test_simplify_lens.py` stops passing a review runner to `main`.

**Files:**
- `scripts/direct/assign.py`
- `scripts/direct/test_simplify_lens.py`

**Workers:** `1 writer`.

**Acceptance gate:** `scripts/direct/test_simplify_lens.py` passes through the real `assign.py` start path, and fails with the refusal when the old `os.execvpe` call returns.

### Phase 7w — Tests read the live notifier folder  · status: done

**Depends on:** none

**Source:** the merge gate on 2026-11-05, after the live notifier timer folders moved to `showrunner-<staff id>`. Main's suites read those live folders and go red.

#### Work Order

**Goal:** No test reads the live `~/.local/state/notifier` folder. A test run gives the same result before and after a live state migration.

**Spec:**
1. Each test that reaches `showrunners.registered_showrunners` uses a private notifier folder. Prefer one change in shared test support. Keep the assertions.
2. Add no guard or check.

**Files:**
- `scripts/` test files and test support only.

**Workers:** `1 writer`.

**Acceptance gate:** `shutdown.test_stop_resume`, `production.test_session_pane`, `production.test_roles_audit` and `direct.test_progress_history` pass on main with the live folders in place, and the full merge gate is green.

### Phase 7x — The merge scope check goes  · status: done

**Depends on:** none

The user said go on 2026-11-05, through sdlc [9ce110ba]. User decisions: 2026-10-31 "Owns is deleted"; 2026-11-01 "cargo-berth is what maintains ownership of who goes when - this was gold plating i never asked for". Change review C1107 returned for this.

1. `merge_checkpoint.py`: delete the empty-responsibilities refusal, the outside-responsibilities step, `allowed`, `hub_paths`, `Unit.owns` and `--also`. Keep the staged-paths stop and the shrink and as-built path rules.
2. Delete `owns_fill.py` and its test.
3. `add_unit.py`: delete `owned_responsibilities`, the refusal for a unit with no owned paths.
4. `unit_config.py`: delete `responsibilities_from_owns` and `BOARD_FREE`. `unit_lookup.py`: delete the `Owns` cell. The `responsibilities` field stays, because `nightly_review.py` and `add_unit.launch_profile` read short duty names from it.
5. A one-off program outside the repo removes path entries and `board-free` from each `.unit.toml` at deploy, once on each machine. Then it is deleted.

**Acceptance gate:** a checkpoint that changes any path merges with no scope hold; `git grep -E 'owns_fill|responsibilities_from_owns|BOARD_FREE|outside responsibilities|hub_paths'` finds nothing in scripts, commands or docs/as-built; suites of the production and style folders pass; source net negative.

### Phase 7y — A refused launch is not a repair round  · status: done (bfa635d64)

**Depends on:** none

Source: builds resources-unit, through sdlc [dcad96ef]. The ledger counted two launches that `assign_gate` refused as repair rounds, so Phase 57 reached repair round 4 with one landed repair.

1. `scripts/history/rounds.py`: a round comes from fix passes only. A recorded dispatch with no started pass counts for nothing.
2. `scripts/direct/findings.py`: a refused launch spends no fix attempt and no repair budget.

**Acceptance gate:** events with two refused launches and one landed repair give `repair_round` 2, no Astra review due and no advisory; `history.test_rounds`, `direct.test_findings`, `direct.test_progress_history` pass.

### Phase 7z — An installed script imports from its own install  · status: done

**Depends on:** none

Source: sdlc [22612d13] and [6dab7317]. A stdin or `-c` Python script puts the working directory first on `sys.path`. So `from scripts.lib import ...` in a shell heredoc reads the reviewed tree. In a cargo-liner worktree the import fails. In a `~/.claude` tree behind main it reads old code.

1. `scripts/lib/py`: put the shim's own root on `PYTHONPATH` and start a stdin, `-c` or `-m` script with `-P`, so the working directory never enters `sys.path`.
2. Every heredoc or `-c` Python script that imports `scripts.` starts through `scripts/lib/py`.

**Acceptance gate:** one heredoc from a folder with its own `scripts/`, and one from a tree whose `scripts/lib` is behind the install, both import the install's module. The suites of the changed folders pass.

### Phase 8a — One lookup for the production owner  · status: done (0ed0dc329)

**Depends on:** Phase 7u

#### As-built

- **One owner lookup.** `read_production` keeps the found row on `Production.owner`; callers read it. `ProductionDocument` is a document with no owner, and `ProductionDocument.for_owner` binds one. `unit_lookup.doc_owner` is the one doc-to-owner function. `showrunner_footer.read_production` looks up the row once.
- **`sources` removed.** `staff_sources.member(selector, record)` replaces `member_sources()` at each caller. `MemberSources.productions`, `store_owner.showrunner_id(sources=)` and the `sources` parameter of `production_members` and `unit_lookup.unit_metadata` are gone.
- **Dead code removed.** `source_allowed`, `_fill_machine`, `BoundMigrationSources`, `migration_peer_identities`, `migration_sources`, `showrunners.read_bound_source`, `production_name_for_document`, `fixture_showrunner_name`, `ProductionNames.showrunner` and the `ShowrunnerSource` fallback in `production_lifecycle`.
- **Fewer reads.** `production_check.sh` and timers pass a staff id, with no extra Python start. `live_units` takes the production name. `phase_note_ownership` takes a `record`. `stall_watch` calls `every_labeled_session` once per tick. `unit_labels` runs `timer_for` once per parent. `scope_list` and `footer_counts` share one record.
- **Parent lookup.** `record.parent_of` is the one parent lookup. `row_production` and `progress_history.resolve_production_unit` call it and read no document.
- `add_unit.py:1279` keeps `ensure_staff_showrunner(production)`, because that call also records a moved document.
- `worker.py` passes `worker_id` to `begin_dispatch`. The old `CHECK=... production_check.sh {doc}` fixture shape and the old slug text are gone.

**Acceptance gate:** main merge gate green on 2578d686d (which holds the phase); hash 0ed0dc329 adds the stack and sdlc c7754c2d6, with suites and types green.

### Phase 8bb — Merge checkpoint tests read the live registries  · status: done (829824277)

Source: builds queue-unit, through sdlc [a84d0d7a]. `scripts/production/test_merge_checkpoint*.py` (10 files) read the live showrunner and session registries.

1. `scripts/production/staff_test_support.py`: `isolated_staff_file` gives private registry paths, in one place.
2. The 10 suites use it. No copy of the fixture.

**Acceptance gate:** a run with a throwaway audit hook that is not committed finds no read of a live registry; `production.test_merge_checkpoint*` pass, plus each suite that uses the helper.

### Phase 8c — The merge gate removes its own temp folder  · status: done (829824277)

Source: sdlc [eca4284e]. Ended merge gates leave `/dev/shm/merge-gate-*` folders; natest counts 75 folders and 234 MiB of RAM. The gate removes its own folder when it ends, also on failure and on a kill signal, with no separate sweep.

**Depends on:** none

**Acceptance gate:** a gate that fails and a gate that gets SIGTERM, SIGHUP or SIGINT each leave no folder, in a test that runs the real gate process; `production` suites pass, then the full merge gate.

### Phase 8d — Git and the unit's records answer what merged  · status: done (829824277)

Source: the user through sdlc [3e19fcc0]: "yes delete the title-reading code and let git and the unit's own records answer".

1. Delete `merge_checkpoint.merge_branch_history`, its parse of merge commit subjects and its rename handling. Switch every caller.
2. Git answers whether work merged: a hash already merged is an ancestor of the merge branch.
3. The unit's phase records answer when a phase started.
4. Delete the "follows the last merged checkpoint" check. No new check.
5. Delete the `-unit` suffix, by the user's approval through sdlc [25a7d0d1]: delete `unit_names.unit_id`, switch its callers to the staff name, and delete the suffix handling in `unit_names.name`, `promote_showrunner`, `sessions.py`, `push_texts` and `user_action`.

**Depends on:** Phase 7u

**Acceptance gate:** `git grep merge_branch_history` finds nothing, and no code outside tests reads a unit or phase from a merge commit subject; a fixture whose merge subjects carry no unit name gives the same answers; suites of changed folders pass, then the full merge gate; source net negative.

### Phase 8e — Importing staff costs less  · status: done (455aa383e)

Source: sdlc [209c11a3]. Importing `scripts.production.staff` takes 38.9 ms of a 92.6 ms status line refresh. Cut the cost inside `staff.py`. Do not add a second light reader: that is a copy.

**Depends on:** Phase 8a

**As built:** `staff.py` defers each import that only some paths use into its functions; `_Arguments` stays with the command line. The same-load median fell from 29.4 ms to 18.9 ms. The remainder is `dataclasses` with `inspect` and the module's own code. No command or behavior changed.

### Phase 3x — Small stores keyed by staff id  · status: done (735747766)

Source: herdr-unit plan Phase 3x, given to devtools-unit by sdlc 2026-11-02. Reduced by the Phase 3t architect review (2026-11-05): the store migration code already shipped (`change_review.py:1118`, `phase_table.py:1939`, `board_json.py:462`), with tests for interruption, reruns and ownership changes, and proposal readers already resolve staff ids. Store 3 (saved tab titles) is dropped (user, 2026-11-01). Store 6 (`stall_watch.py`) belongs to messaging-unit. herdr-unit's 3y owns the rename files and removes each store's rewriter there.

**Depends on:** 3t, 3u, 3v (records stage of the rename track).

**As built:** the change review cell, the phase note `unit:`, the board cache and the proposal `unit:` field hold staff ids, and `change_routing` routes an open row from its staff id. `change_review.py migrate-owners` moved 222 rows on natedev. The Mac run waits on the user's Mac step. After its final read shows staff ids in all open rows, delete the `by_id` condition and `migrate_owners`.

The read-only acceptance checks also retain these requirements:

1. Name each excluded reader and its owning phase in the checkpoint notice.
2. Include the dispatch readers in that search: `bump_gate`, `dailies_input`, `live_units`, `showrunners`, `staff_sources`, `unit_lookup`, `unit_table`, and `worker_admission`.
3. Use different timer and row documents to prove that each changed reader takes the row's document.
4. Keep `test_checkpoint_uses_the_plan_worktree_when_the_director_runs_elsewhere`, lookup tests, requested-checkpoint tests, and partial-report tests.
5. Distinguish `ProposalsUnavailable` and `LedgerUnavailable` from confirmed absence.
6. Verify the director and branch folders with `targeted_test.py` and `add_unit.py --check`.
7. Verify `resume_unit.py sdlc guild-process-unit --dry-run`.

The search may retain read-only `session_labels.py`, `production_units.WRAPPED` imports, and Units scope reads owned by Phase 4b.
Only reads change.

### Phase 3t — Production tools read the row and `.unit.toml`  · status: done

#### As-built

Codex workers start one way, and the old ownership paths are gone.

- `owns_fill.py` and the four one-time migrations (`migrate_proposals`, `migrate_showrunner_settings`, `migrate_showrunner_timers`, `remove_showrunner_parents`) are deleted; their reader tests live in `test_showrunners`. `add_unit` and `unit_config` take responsibilities only from launch options or the saved `.unit.toml`.
- `stall_watch` builds its watch from the staff row (`watched_staff_unit`). `resume_unit` returns `RetiredUnitRecovery | NoRecoveryRequired`; `RetiredUnitRecovery` holds the row name once.
- `implement.sh` and `review.sh` run Claude workers only. Both call `models_config.sh agents_resolve_claude_launcher <task> <caller>`, which exits 2 with "<caller>: Codex workers must use assign.sh." before any file is written. `review.sh` first refuses a Codex simplify ("direct.simplify requires a Claude assignment."). Turn records, the tree check and `setsid` run only for simplify reviews.
- `assign_gate.py` is a leaf: `ASSIGN_LAUNCHERS`, `WORKER_LAUNCHERS`, `assign_role` (the one `impl` → `implement` mapping) and `assign_command` (forces `--role`, `--session-dir`, `--to`; drops `--new-worker`).
- `progress_history.last_dispatch(worker_id, run) -> RecordedDispatch | NoDispatchRecorded` reads the pass record first, then the role log; a missing field is `DispatchDetailNotRecorded.UNRECORDED`. `resume_message` builds `assign.sh` commands from it only. `progress_history.write_review_turn` is the one review-turn writer.
- Remaining matches of the old reader search: `showrunners.former_slugs` and its caller `session_staff.py:492` (former names as history) and test fixtures.

**Files:**
- `scripts/direct/{implement.sh,review.sh,assign.py,assign_gate.py,progress_history.py}` — launchers, gate, dispatch reader.
- `scripts/agents/{models_config.sh,codex_mesh.py,mesh_runs.py,agent_bg.sh}`, `scripts/lib/resume_message.py`, `scripts/shutdown/inventory.py` — one launch path, resume and account move.
- `scripts/production/{add_unit,unit_config,stall_watch,resume_unit,retire_unit,worker_admission}.py` — row and `.unit.toml` readers.

**Binds later work:** `assign_gate.py` stays a leaf. `last_dispatch` is the one reader of a worker's role, phase, lens and paths. Only `implement.sh` resolves a repair round today.

**Gotchas:** one unittest process over many modules leaks state and shows false reds; run modules alone or in small groups.

**Ruled out:** a second branch-folder rule; a `Path | None` adapter for the proposals readers.

### Phase 3y — Astra review starts Codex only through assign.sh  · status: done

#### As-built

Codex workers start only through `assign.sh`, and that path does what the old Codex launchers did.

- `codex_mesh.main()` calls `assign_gate.require_assign()` for `start`, `follow`, `reopen`, `fork`, `adopt`, `prime` and `resume-account-move`; other verbs stay open. `test_one_launch_path.py` fails on an in-process call of a gated verb outside `assign.py`.
- A managed Astra review runs through an `assign.py` operation that keeps reviewer publication, role ownership, read-only access and cleanup. The standalone review with no staff identity keeps its direct `codex exec`.
- An `assign.sh --role fix` dispatch with `UNIT_RESOLVES_ROUND=1` resolves its round in `finish_repair`: `findings.py landed` on a clean exit, `abandon --edits-landed --reason "the fix worker exited with code N"` on an error, then a plain `finish-pass`. A failed call prints the commands to run by hand; the round stays in flight.
- `progress_history.last_dispatch(staff_id) -> RecordedDispatch | NoDispatchRecorded` matches a pass by the stored worker staff id (`start-pass --worker-id`), and by name only for a pass with no id. It returns the newest finished pass, searches every run of the staff parent, and carries `instance_id` and `status`. The account-move resume and `resume_message` read it; a roster entry with no staff id, `NoDispatchRecorded` and `DispatchDetailNotRecorded.UNRECORDED` each give a named result.
- `RecordedTurnSettings` and `recorded_turn_settings` live in `scripts/agents/worker_lifecycle.py`.

**Files:**
- `scripts/agents/codex_mesh.py`, `scripts/astra/astra_review.py`, `scripts/direct/assign_gate.py` — the gate, the managed review, account move.
- `scripts/direct/assign.py` — the managed Astra operation and `finish_repair`.
- `scripts/direct/progress_history.py`, `scripts/lib/resume_message.py` — `last_dispatch` and its callers.
- `scripts/agents/worker_lifecycle.py` — recorded turn settings.
- `docs/as-built/long-lived-workers.md`, `docs/as-built/agent-registry.md` — Codex workers continue with `assign.sh --to`.

**Binds later work:** a test that calls `codex_mesh.main` in process patches `scripts.direct.assign_gate.called_by_assign`. An import of `recorded_turn_settings` from `codex_mesh` brings back the `codex_mesh`/`progress_history` cycle. `last_dispatch` takes no run argument.

**Gotchas:** a tree's own older `merge_gate.py` refuses the live admission config; gate a tree with main's `merge_gate.py --checkout <tree>`.

**Ruled out:** a receipt that resolves the ledger on rerun, `findings.py --round`, `finish-pass --pass-id`, a pass-id check and `prepare_repair` (unasked checks, user rule 2026-11-05).

### Phase 3z — One import rule  · status: done

**Depends on:** Phase 3y

#### As-built

- Every repository import is package-qualified: `from scripts.<folder> import module` or `from scripts.<folder>.<module> import name`, mock patch targets included. A bare and a package import of one file are no longer two module objects; the dual-name aliases are gone.
- Each entry file loads `scripts/lib/package_root.py` through the 4-line `try` block after its docstring and `from __future__` imports. `package_root.py` puts the checkout root at `sys.path[1]` with no duplicate, so bare `python3 <path>` loads its own checkout.
- The `scripts/lib/py` shim sets the package root before its cached and uncached exits: the script's checkout for a script path, the shim's checkout for `-m`, `-c` and stdin. It keeps the caller's working directory, `CLAUDE_PYTHON`, the interpreter cache and the version floor. The gate starts each test run through the checked tree's shim and drops the caller's `PYTHONPATH`.
- Only three places change `sys.path`: `scripts/lib/package_root.py`, the isolated form in `scripts/lib/py`, and `scripts/aws_target/gate_box.py:118`, the cloud box guest program, which runs from a fresh clone.
- `module_run.py` and `fast_track.py` load each test by package name. `build_hold.py` and `record.py` register their CLI module under the package name. Generated scripts run through their root's `scripts/lib/py`.
- `scripts/lint/package_imports.py` keeps only `bootstrap <checkout>` (`add_bootstrap`, `_bootstrap_tree`), which adds the block to each new entry file. Any other form exits 1 with a usage line.
- Two entries keep a foreign interpreter and set `PYTHONPATH` to the checkout on their launch line: `banned_word_analysis/banned-word-gradient.py` (`uv`, for Pillow) and the Blender script in `commands/blender/bake_textures.md` (`bpy`).

**Files:**
- `scripts/lib/package_root.py` — sets the package root for entry files.
- `scripts/lib/py` — the shim; sets the root for script, `-m`, `-c` and stdin runs.
- `scripts/lint/package_imports.py`, `scripts/lint/test_package_imports.py` — the `bootstrap` command and its tests: shebang and main-guard entries under bare `python3`, a dynamic import, and a caller in another folder.
- `scripts/aws_target/gate_box.py` — keeps its `sys.path` line at 118.
- `scripts/build_queue/memory_feed.py` — keeps `--code-root` only for the installed-code fingerprint.

**Gotchas:** a shell heredoc program can hold Python, so a `sys.path` search must include `*.sh`. `buildlog/test_store_identity.py` keeps its bare import because it tests bare and package store identity.

**Ruled out:** a guard test (sdlc [e00ba3f9]); entry or caller changes to the shim form, so saved commands outside the repository keep their bare form (sdlc [432682a3]).

### Phase 8q — Converter edits only import lines  · status: done

#### As-built

`scripts/lint/package_imports.py` `rewrite` records, before its edits, the offset of each `if` whose `body` and `orelse` differ. It moves those offsets through each edit and drops an offset inside an edited range. After the edits it collapses an `if` only when its line holds a recorded offset and its two branches are now equal. An `if` with equal branches in the original source stays, and so does each import its branches use. `test_package_imports.py` holds the `buildlog/record.py` `test_phase_context` case; it fails before the repair. Checkpoint `be3e0af4b`.

**Gotchas:** On `origin/main` the `message` hash `e05e54ffb` collapsed three `if TYPE_CHECKING` blocks whose two branches held the same imports: `scripts/message/escalate.py:43`, `scripts/message/test_send.py:61` and `scripts/message/push_texts.py:13`. Behavior is the same, and they stay (sdlc [0f3d2d39]). `buildlog/record.py` on main was already restored by the verify unit.

### Phase 8r — The cancel case of the Claude admission test survives a closed socket  · status: done

**Depends on:** none

#### As-built

In `scripts/direct/test_claude_worker.py` `test_claude_admission_ends_at_dispatch_acceptance_or_cancellation`, the cancel case sends SIGTERM to the launcher and then sends the gate byte under `contextlib.suppress(BrokenPipeError, ConnectionResetError)`, because the launcher can close its socket first. The completion case keeps its strict `sendall`. No assertion changed. It shipped in the Phase 3z `production` hash (sdlc [e37b2ad2], [7b030393]).

### Phase 4a — Run owner and showrunner timers keyed by staff id  · status: done

#### As-built

`owner_staff_id` is the one owner field of a run. `_set_owner`, `resolve_production_unit` and the start path of `_start_phase` read only the staff row, and they accept no names. A saved event keeps its names as history. `phase_table.build` with no `working_dir` takes the folder from the staff row, and it reports a staff read error in the table result. It does not use `Path.cwd()`. `unit_runs.migrate_owners` does not exist.

`showrunners.timer_scratch` returns `TimerConfigurationUnreadable`, `TimerTargetUnresolved`, `TimerScratchAbsent` or `TimerScratchFound`. `adopt_session_state` prints the unreadable case and keeps going. Every reader of the timer folder uses `showrunner-<staff id>`.

**Files:**
- `scripts/direct/progress_history.py`, `scripts/direct/phase_table.py`, `scripts/lib/unit_runs.py` — the run readers.
- `scripts/production/showrunners.py`, `scripts/production/update_registration.py`, `scripts/production/production_check.sh`, `scripts/production/rename.py`, `scripts/statusline/statusline.sh` — the timer readers.
- `scripts/direct/test_progress_history.py`, `scripts/direct/test_phase_table.py`, `scripts/production/test_showrunners.py` — one test per result; fixtures store `owner_staff_id`.

**Binds later work:** a test fixture for a run stores `owner_staff_id` with the staff row's id, and no name fields.

**Gotchas:** `run_owner` accepts only `unit` rows, so no run has a showrunner owner, and `parent`'s `showrunner` kind check after `run_owner` is unreachable. Active runs from before the owner code convert once at landing, on natedev and the Mac, by a program outside the repository.

**Ruled out:** a reader of the old name fields in main code.

### Phase 8f — A run whose only worker dies at launch ends itself  · status: done

#### As-built

A launch that fails with no other work in its run ends the run through its normal end path. `scripts/direct/assign.py` `end_failed_launch(run, slot, env)` runs after a nonzero launch and returns without an action when any of these hold: a recorded pass completed; a pass names another slot; the mesh roster, an `impl_worker_*.json` staff row or a herdr staff row names another worker; or `unit_runs.held` names a run other than this one.

The action is `progress_history.py finish-run --status error`, then `end_session.sh`, each through `call` with `check=False`. A failed child prints one line and the launch keeps its exit code. An exception in the check prints one line and changes nothing. A signal to the launcher raises `SystemExit` and never reaches the check. A herdr launch reaches it only for `worker.py assign` exit 2, the no-worker-started exit. The phase reservation stays.

The run marker uses the Codex id first: `prepare_session.sh` and `end_session.sh` both read `CODEX_THREAD_ID`, then `CLAUDE_CODE_SESSION_ID`. A launch with no `--session-dir` after the end starts a new run through `prepare_run`.

**Files:**
- `scripts/direct/assign.py` — `end_failed_launch`, `recorded_passes`, and the three calls.
- `scripts/direct/end_session.sh`, `scripts/direct/prepare_session.sh` — the Codex-first director id.
- `scripts/direct/test_assign_launcher.py` — the real-path tests for each condition.
- `scripts/agents/test_codex_mesh.py` — its run-end fixture removes `CODEX_THREAD_ID`.
- `docs/direct/launch_implementation.md`, `commands/unit/direct.md`, `docs/as-built/long-lived-workers.md` — the end path and the retry forms.

**Gotchas:** a fixture that sets `CODEX_THREAD_ID` beside a different `CLAUDE_CODE_SESSION_ID` makes `end_session.sh` read the Codex marker.

**Ruled out:** a new refusal at the next launch; the end is the whole repair. Other open-marker cases belong to hierarchies.

### Phase 8g — The review links service restarts when its code changes  · status: done

#### As-built

A running review links server replaces itself when a module it loaded changes. `ReviewLinksServer.service_actions` in `scripts/review_links/serve.py` runs `loaded_module_stamps()` every `code_check_seconds` (`config/review_links.conf`, default 5). The function reads `st_mtime_ns` and `st_size` of each `sys.modules` file under the server's checkout. A file with no stamp (`ModuleStampUnavailable.UNREADABLE`), a file modified at or after process start (`STARTED_NS`) and a changed stamp each raise `CodeChanged`. `main` prints `review-links: code changed: <path>`, closes the socket through its `with` block and runs `os.execv(sys.executable, sys.orig_argv)`. The pid and the environment stay. A failed start of the new code exits 1, and the service manager starts it again.

While a verdict delivery request is live, the check returns and keeps its recorded stamps, so the next check restarts.

`change_review.py add --merged <hash>` refuses a hash that is not an ancestor of `origin/main`: one line, exit 1, no row. The `migrate-owners` action is gone. `commands/change_review.md` and `docs/production_format.md` item 17 state the rule: a row is added only when the user can check it now, merged to where the user runs it and live.

**Files:**
- `scripts/review_links/serve.py`, `config/review_links.conf`, `docs/as-built/review-links.md` — the code check.
- `scripts/review_links/test_serve.py` — a real child process, the same pid, new code, HTTP after the restart.
- `scripts/production/change_review.py`, `commands/change_review.md`, `docs/production_format.md` — the refusal, the deletion and the rule.
- `scripts/production/test_change_review.py` — `MergedAddTests` on a real bare origin.

**Gotchas:** a test fixture must set old modification times, because a file written after process start counts as changed.

**Ruled out:** a merge-time restart command; the server finds each change itself.

### Phase 8h — A change reaches the list only when it is on main and live  · status: done

#### As-built

A row reaches a change list only when its change is on `origin/main` and live. `change_review.list_ready(repo, production, merge_branch, *, review_list=None, record=None, selection=_READINESS_SCAN)` is the one path. It fetches `origin main`; a failed fetch prints one line and adds nothing. It reads the merges on the merge branch since `READY_FROM`, takes their lines through `publication_changes`, and adds a row for each line of a merge that is an ancestor of `origin/main`. Rows carry the merge's short hash. `add`, `live`, `merge_checkpoint.record_publication_changes` and `merge_gate.record_changes` all call it; the two merge paths run it after the last promotion step. The `append` and `drift` actions and `_ChangeNotOnMain` are gone.

A `Change-Review:` line takes an optional fourth field, `after <step>`. `Visible` holds `ReadyNow` or `AfterStep(step)`. A scan skips an `AfterStep` line and prints one line with the exact command: `change_review.py live --merged <hash> --step <step> --repo <checkout> --production <showrunner>`, with script paths from the tools' own checkout. `live` selects `CompletedStep(merged, step)`, releases only that merge's lines for that step, and raises `ChangeReviewError` when no line waits on that step.

`_covered(rows, change, repo)` runs under the list lock: a row with the same Change text covers a line unless the row has a known Merged hash that does not hold `change.checkpoint`. So a repeated scan adds nothing and a returned change gets a new row for its later commit. A child production with no list uses the merging production's list and keeps its own credit. `append_rows` prints `change review: no staff row for unit <name>; Producer stays empty` once per unit after a row write. A failed write in `merge_checkpoint` reports `<doc> not written: <error>; retry after the repair`.

**Files:**
- `scripts/production/change_review.py` — `list_ready`, `_covered`, `live`, `ReadyNow`, `AfterStep`, `CompletedStep`, `ReadinessScan`, `PublicationChange`.
- `scripts/production/merge_checkpoint.py`, `scripts/production/merge_gate.py` — the callers after promotion.
- `scripts/production/test_change_review.py`, `test_change_routing.py`, `test_merge_checkpoint_handoff.py`, `test_merge_checkpoint_promotion.py`, `test_merge_checkpoint_rerun.py`, `test_merge_checkpoint_validation.py`, `test_merge_gate.py`, `test_merge_gate_promotion.py`, `merge_gate_test_support.py` — the real-path tests on bare origins.
- `commands/change_review.md`, `commands/unit/checkpoint.md`, `docs/production_format.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/review-links.md` — the rule, the fourth field and who runs `live`.

**Gotchas:** `publication_changes` prints nothing, so a read has no side effect. A merge from before `READY_FROM` never adds a row.

**Ruled out:** a store or waiting section for held lines; Git holds them in `Change-Review:` lines.

### Phase 8i — A Claude worker starts only through assign  · status: done

#### As-built

`scripts/direct/implement.sh` does not exist. `assign.py` `execute_claude` calls `assign_claude.run(ClaudeLaunch)` in the same process, so `assign.sh` is the one start path for a Claude worker. `assign_claude.py` has no `__main__` and no argument parser. `ClaudeLaunch` is a frozen dataclass; its `worker` field holds `NewClaudeWorker` or `ClaudeFollowUp`.

The order stays: admit, `worker_state.py`, switch check, claim, `phase_reservation.py claim`, status, pass start, heartbeat, board, `TMPDIR`, worker start, worker wait, heartbeat watch, finish. File names and contents of `impl_status_<slot>`, `impl_summary_<slot>.txt`, `impl_agent_<slot>.log` and `impl_agent_<slot>`, board lines, heartbeat lines and recorder events stay the same. The claim holds `os.getpid()`. `run` releases the claim in `finally`, and a TERM, INT or HUP raises `SystemExit` through `_interrupted`, so the finish records run on a signal. The launch uses the family, model and effort from `resolve_model`, with no second resolve. `--launcher-command` holds the `assign.sh` command of the launch, so a replay runs assign.

`assign_gate.py` holds no `implement.sh` entry, and `agent_bg.sh` accepts assign as its direct parent. `stall_watch.py` `WORK` holds no `implement.sh`, and a Claude worker under assign still counts as work.

**Files:**
- `scripts/direct/assign_claude.py` — the moved launch.
- `scripts/direct/assign.py`, `scripts/direct/assign_gate.py`, `scripts/production/stall_watch.py`, `scripts/agents/models_config.sh` — the callers and the gates.
- `scripts/direct/test_assign_claude*.py`, `scripts/direct/assign_claude*_test_support.py` — the real-path tests through `bash scripts/direct/assign.sh` with a fixture Claude binary.
- `docs/as-built/agent-registry.md`, `docs/as-built/long-lived-workers.md`, `docs/as-built/staff-accessors.md`, `docs/as-built/unit-progress-history.md`, `docs/as-built/usage-ledger.md`, `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/build-followups-fn-length-hook.md` — the assign form.

**Binds later work:** the launcher Python starts count from `agent_bg.sh`, `claude_account.sh` and `heartbeat_watch.sh`; `worker_record` is now in process. `_claim` trusts any live PID in `impl_claim_<slot>.json`.

**Gotchas:** `git grep implement.sh` also prints `scripts/notify/` (the approved phone sample; a changed sample voids the user's approval), `docs/direct_footprint_review.md` (a dated review), `settings.json` and a negative assertion in `scripts/lib/test_relaunch.py`. A `claude --bg` start in a scratch copy reports an untrusted workspace, so a live check uses `--cwd` on the home worktree.

**Ruled out:** a compatibility shim for saved `implement.sh` commands; 0 of 20 records held one.

### Phase 8j — A worker claim belongs to the process that made it  · status: done

#### As-built

`impl_claim_<slot>.json` holds `pid`, `started` and `worker`. `worker_state.publish_worker_claim` is the one writer, for `assign_claude._claim` and `assign.claim_worker`: it writes atomically, and with an unreadable own start it omits `started` and prints an anomaly line. `worker_state.read_worker_claim` is the one reader. It returns `HeldWorkerClaim(pid)` for the same start or an unreadable one, `EndedWorkerClaim` for a positive `pid` that is gone, a zombie, another start or has no recorded start, and `NoWorkerClaim` for an absent or malformed file or a record with no positive `pid`. A reused process id therefore no longer blocks a launch. `_release` and the interrupted path of `assign.py` compare only `pid` with `os.getpid()`.

`remove_workers.removal_targets` gives `OWN_RUN_FINISHED` (`its run finished this phase`) to a worker of an own run and `DIRECTOR_NOT_LIVE` (`unit director is not live`) only to a sweep with no own run.

**Files:**
- `scripts/direct/worker_state.py`, `scripts/direct/assign.py`, `scripts/direct/assign_claude.py` — the claim.
- `scripts/direct/remove_workers.py` — the removal reason.
- `scripts/direct/test_worker_state.py`, `scripts/direct/test_assign_claude_claims.py`, `scripts/direct/test_assign_claude_claim_follow.py`, `scripts/direct/test_assign_launcher_review.py`, `scripts/direct/test_remove_workers.py` — one test per result; real-path tests through `assign.sh`.

**Binds later work:** a fixture that writes a live claim adds `started` from `process_start`.

**Gotchas:** a claim from a launcher that runs across the landing has no `started` and reads as ended. `assign.claim_worker` then refuses at `launcher_pid`. `assign_claude._claim` refuses at its idle check, except in two short moments, when a second launch can double-launch one Claude worker. A malformed claim on the Codex path counts as no claim.

**Ruled out:** an old-shape reader or a conversion for the claim. A fix of `launcher_pid`: its refusal is in the detached-run server, and that code retires.

### Phase 8k — A Claude worker launch runs in one Python process  · status: done

#### As-built

`scripts/direct/claude_worker.py` holds each step of a Claude worker launch and has no `__main__`. `assign_claude._run` calls `open_worker(turn)` or `follow_up(turn, native_id)`, then `wait_for_turn(turn, delivered)`, all in the assign process. `ClaudeTurn` holds the inputs of one turn. A launch returns `Delivered(native_id, turns_before, launch_environment)`, `Unchanged` or `NotDelivered(code)`; `TurnsNotCounted` is the turn count of a new session. `scripts/agents/agent_bg.sh`, `assign_gate.PYTHON_HOPS` and `AGENT_BG_WORKERS` do not exist.

`worker_admission.switch_worker` keeps its `start` and `dispatch` callbacks. The account step calls `accounts.account_for_unscoped_launch`. One `nix_env.ensure` serves the settings and the start. The id comes only from the `backgrounded` line of the banner, and `_registered` waits `CLAUDE_WORKER_REGISTER_WAIT_SECS` for the full id. `agents_log.append` runs before delivery, and delivery is one child: `send.py --to session:<id> --from launcher --file <prompt>`, with `CLAUDE_WORKER_SEND_SCRIPT` as its test override. `wait_for_turn` parses `claude agents --json` and `claude logs` in process each `CLAUDE_WORKER_POLL_SECS`, which `UNIT_WORKER_POLL_SECS` overrides.

`assign_claude._run` runs `_record` in `finally` around the launch, before `heartbeat_watch.sh`, and writes nothing when no session is published. A signal after the banner stops the unpublished session with `claude stop`. Exit codes: 127 no binary, 2 refused or not delivered, 1 no banner, gone with no summary or any other launch error, 143, 130 and 129 on a signal. An unreadable log gives the fallback summary. A transcript record with no message object does not count as a turn.

**Files:**
- `scripts/direct/claude_worker.py`, `scripts/direct/assign_claude.py`, `scripts/direct/assign_gate.py`, `config/direct.conf` — the launch.
- `scripts/direct/test_claude_worker.py`, `scripts/direct/test_assign_claude.py` — moved launcher behaviors and regression tests; other launcher tests use `fixtures/background.sh` or `assign.sh` with the fixture Claude binary.

**Binds later work:** the recorder and resolver children of `assign_claude._run` are the remaining launcher Python, with `send.py` (180 ms) and the `heartbeat_watch.sh` digest (24 ms each minute).

**Gotchas:** a `claude --bg` start in a scratch copy reports an untrusted workspace, so a live check uses `--cwd` on the home worktree with the baseline commit fetched there.

**Ruled out:** an in-process `send.py`: delivery has its own exit paths and an import still pays most of its cost. A port of `heartbeat_watch.sh`: `review.sh` and `assign.py` share it.
### Phase 8l — A worker whose launcher and server died recovers on its own thread  · status: done

#### As-built

A Codex worker whose launcher and app-server died recovers on its recorded thread through one more `assign.sh --to` dispatch.

- `assign.py` `claim_worker` calls `codex_mesh.py reopen` for an `ended` worker, and for a worker in `ROSTER_LIVE` or `WAITING_CAPACITY` whose launcher is not attached (`codex_mesh.worker_launcher_attached`).
- `command_reopen` accepts `WAITING_CAPACITY` and sets `dead_launcher_recovery` on the paused entry. A follow-up with that mark refuses a replacement thread when `thread/resume` fails.
- `codex_mesh.command_end` keeps the staff row and thread id; a second `end` on an ended worker retries the cleanup. `workers.ending_worker` records the end and keeps the worker in the unit ledger.
- Unit retirement stops workers through `stop_resume.stop_unit_workers`.
- `remove_workers.py` resolves staff membership before it calls the worker command.
- The held `worker.py` and `assign.py` callers retain their mesh interfaces until round 2.
- The account move tries each recorded server, the recorded port first, and uses the one that has the thread loaded. When `_server_availability` cannot prove that no server holds the thread, it refuses.

**Files:**
- `scripts/direct/assign.py` — reopen on a dead launcher; the Astra reviewer retire.
- `scripts/agents/codex_mesh.py` — end keeps the row; the recovery mark; the account move server choice.
- `scripts/direct/workers.py` — the unit ledger keeps ended workers.
- `docs/as-built/long-lived-workers.md`, `docs/as-built/session-roster.md` — end, reopen and retire rules.
- `scripts/direct/test_assign_launcher_reuse.py`, `scripts/agents/test_codex_mesh.py`, `scripts/direct/test_workers.py`, `scripts/astra/test_astra_review.py` — the tests.

**Binds later work:** `codex_mesh.py end` never retires a staff row; a new caller that ends a worker for good retires it after `end` exits 0.

**Gotchas:** find permanent-removal callers by their `end` calls, not by `staff.retire` calls. Look up a worker's row by its staff id (`session_staff.worker_row`); a lookup by session id can match an unrelated row when the session is empty.

**Ruled out:** a second recovery mark beside `dead_launcher_recovery`.

### Phase 8m — The end-session test does not read the live session  · status: done

#### As-built

`direct.test_end_session` gives the same result whatever session identity its caller has. `EndSessionTests.environment` removes `CODEX_THREAD_ID` and every `PLAN_DELEGATE_*` name from the environment it gives `end_session.sh`, and keeps its own `CLAUDE_CODE_SESSION_ID`. The in-process test patches `os.environ` with `clear=True` to that same environment.

**Files:**
- `scripts/direct/test_end_session.py` — the fixture.

**Gotchas:** `unit_runs.environ` and `unit_env.sh` read every `PLAN_DELEGATE_*` suffix, so a fixture that removes only the known names leaks the rest.

### Phase 8n — Entry scripts run under bare python3  · status: done

#### As-built

Every entry script under `scripts/` runs under bare `python3 <path>` from any working directory, with no change to a doc or a caller.

- `scripts/lib/package_root.py` puts the checkout root (the folder above `scripts/`, from its own resolved `__file__`) on `sys.path` directly after the entry script's folder. It adds nothing when an entry already resolves to that root. It imports only the standard library and defines no public names.
- Each entry file with a `scripts.` import (a statement, `__import__` or `importlib.import_module`) and a `__main__` block or shebang loads it after its docstring and `from __future__` imports, with one `../` for each folder level below `scripts/`:

  ```python
  try:
      with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
  except FileNotFoundError:
      pass
  ```

  A copy outside the checkout finds no file and runs with the root from `PYTHONPATH`. The block costs under 2 ms for each start.
- `scripts/lint/package_imports.py`: `add_bootstrap(file: Path) -> bool` adds the block, replaces an older one-line form, keeps CRLF and skips `test_*` and `*_test_support.py`. `_bootstrap_tree(root) -> int` runs it over `scripts/**/*.py`; `convert` and the `bootstrap <checkout>` command both call it.

**Files:**
- `scripts/lib/package_root.py` — the bootstrap.
- `scripts/lint/package_imports.py` — the block function, the tree helper and the `bootstrap` command.
- `scripts/lib/test_package_root.py` — bare, relative-path (`runpy.run_path` under `-I`) and copied-entry runs.
- `scripts/lint/test_package_imports.py` — block insertion, old-form replacement, dynamic imports and convert reruns.

**Binds later work:** `scripts/lib/package_root.py` is the one allowed `sys.path` change; the 4-line block holds no `sys.path` text. `package_imports.py bootstrap <checkout>` adds a missing block over a tree and stays when the import conversion is deleted.

**Gotchas:** `__file__.rpartition("/")[0]` is empty for a `__file__` with no folder part and gives `/../lib/...`; the block uses `rfind`. A `runpy` or `pathlib` form costs about 15 ms for each start; `open` + `exec` costs under 1 ms.

**Ruled out:** hand-written blocks after the converter goes — they drift.

### Phase 8o — Screen recording finds a window with no client list  · status: done

#### As-built

`find_window` in `screen_record.py` reads `xprop -root _NET_CLIENT_LIST`. When the property is missing or the list is empty, it reads `xwininfo -root -tree` and takes each window id and title from `TREE_WINDOW`. The fallback keeps the 10 s lookup limit, the poll, the one-match rule, the stop before ffmpeg on two or more matches, and the list of titles seen on no match. Tree titles are read as printed, with literal quotes and backslashes. `xwininfo` is required only on the fallback path: a missing binary gives `screen_record: missing on PATH: xwininfo`.

**Files:**
- `scripts/screen_record/screen_record.py` — window lookup with the tree fallback.
- `scripts/screen_record/test_screen_record.py` — fake `xprop` and `xwininfo`; tests for a missing and an empty client list, two matches, literal quotes, literal backslashes and a missing `xwininfo`.

**Gotchas:** `xwininfo` prints a title with no escapes, so `TREE_WINDOW` reads to the last `": (` on the line. natedev has `xwininfo` only in the nix store, not on PATH.

**Ruled out:** unescaping tree titles — `xwininfo` does not escape them.

### Phase 8p — An edited change row still covers its change  · status: done

#### As-built

A change review row whose Change text was edited still covers its own change, and no other change.

- `append_rows(..., on_hold=None, merge_changes: Sequence[PublicationChange] = ())` in `scripts/production/change_review.py`. With `merge_changes`, `_merge_covered` decides coverage per review list and per merge. A text match (`_covered`) runs first and reserves its row. Then each unused row whose Merged hash is this merge's hash covers one uncovered change: `ReadyNow` changes first, in commit order, then `AfterStep` changes only when every uncovered `AfterStep` change waits on one step.
- `list_ready` passes every routed change of the merge as `merge_changes`, also for `live --step`, and writes rows only for the selected step's changes.
- Callers that pass no `merge_changes` (`add`, `merge_checkpoint`, `merge_gate`) keep the text match only.
- A merge with two changes and one row keeps the second change uncovered. No check, refusal or column was added.

**Files:**
- `scripts/production/change_review.py` — `append_rows`, `_merge_covered`, `list_ready`.
- `scripts/production/test_change_review.py` — `ReadyListTests` for an edited row on a rescan, text match before pairing, one row for a two-change merge, a ready row beside a held change, a row of another merge, and two held steps released out of order.

**Gotchas:** a row does not record its step. With two held steps uncovered, an edited row covers nothing by its hash, and the scan adds a duplicate row rather than hide a change.

**Ruled out:** pairing every `AfterStep` change in commit order — with two steps released out of order, an edited row covers the wrong step and hides the other change.

## Phase 30 consumer conversion

Live readers use staff membership and backend evidence.
The board, roster and sweep use pane observations.
Shutdown keeps Claude child commands in `ClaudeChildResume`.
`refusal_watch.py` owns `QUOTA_REFUSAL`.
The unused `worker_fill.py` and its test are removed.

Round 2 removes the held launcher and shutdown dependencies.
It deletes `codex_mesh.py`, allocation helpers and worker ledgers.
It removes `PausedSeat`, transport pins and `unit_runs.worker_transport`.
It moves sandbox constants into `worker_panes.py`.
It renames the detached command modules, config and record folder.
Historical readers keep completed run rosters.

### Phase 8s — A hold that names any showrunner waits  · status: done

#### As-built

A holding or waiting status that names any showrunner, or a sibling unit, reads as a wait on an outside party, and stall-watch does not bump the unit.

- `scripts/production/stall_watch.py` builds the peers for `status_defers_bump` and `holding_names_outside` from the one staff snapshot of the pass: every `showrunner` row whose worktree is not removed, the unit's sibling units less its own row, the configured session and the production name. `PaneInspection` carries the peers. Showrunner names are read once per pass, and sibling names once per parent.
- A unit of another production matches as `<name>-unit` through the config pattern.
- `config/stall_watch.conf` no longer names `natedev` in `external_owner_pattern` or `holding_party_pattern`; the staff record supplies it.
- Own names still win: `names_outside` is unchanged.

**Files:**
- `scripts/production/stall_watch.py` — peer names per inspection.
- `config/stall_watch.conf` — outside-party patterns without fixed showrunner names.
- `scripts/production/test_stall_watch.py` — a cases table in `test_holding_names_a_peer_or_showrunner_anywhere_in_the_line` (a natedev unit waiting on sdlc waits), and a row in `test_own_work_order_and_own_names_do_not_hold_the_unit` (an unrelated `style` unit in the status still bumps).

**Gotchas:** a sibling unit name that is a common word still defers a bump.

**Ruled out:** every staff name as a peer — many unit names are common words (style, verify, queue) and hide an own-work hold.

### Phase 8t — The units table shows board status, phase work, other work and queued work  · status: done

**Depends on:** none

#### As-built

- `/showrunner:units` prints `| Unit | Status | Phase | Non-Phase Work | Queued |`.
- `session_state.classify(member, activity)` gives a unit session's state in this order: an exit record gives `exited`; a pause record gives `paused`; no live session gives `exited`; a live `busy` session gives `busy`; else `idle`. The accounts board (`_read_local_role_sessions`) and the units table both call it. The board keeps its account attribution and worker counts in `accounts.py`; its workers role keeps busy and idle only.
- A live herdr pane process (Claude or Codex) counts as a live session. A unit whose worktree is on another machine, with no hold, shows `on <machine>`.
- Phase shows `phase <n> of <m> — <task>` for an open plan phase. The task is the newest run's open step, else the phase title. With no open phase the cell keeps the old texts. Each cell is cut to 80 characters with `…`.
- Non-Phase Work reads only each unit's newest run (`open_unit_runs`). A finished run gives `—`. Otherwise it shows `<phase id>: <open step>` for open phases that are not plan phases, plus ` +<k> more`.
- `/unit:queue add <text> | done | clear | show` runs `unit_queue.py`. The list is at `UNIT_QUEUE_STATE_DIR/<staff id>.json`, default `~/.local/state/unit-queue/`. The Queued cell shows the first item plus ` +<n> more`, or `—`. An unreadable file gives `queue unreadable` and one stderr line; `clear` does not read the old list, so it repairs the file.

**Files:**
- `scripts/accounts/session_state.py` — `classify`, `staff_state` and the activity types.
- `scripts/accounts/accounts.py` — the board calls `classify`.
- `scripts/production/live_units.py` — `LiveUnit.member`, `staff_status`, pane-process and other-machine status.
- `scripts/production/unit_phases.py` — the table cells and the newest-run reader.
- `scripts/production/unit_queue.py`, `commands/unit/queue.md` — the queue.
- `commands/showrunner/units.md` — the columns and statuses.
- `docs/as-built/staff-accessors.md` — rows for the two new staff readers.

**Gotchas:** A held astra-role run never records a finish, so it can be a unit's newest run and show in Non-Phase Work.

**Ruled out:** Reading every open run of a unit's current session — old runs never record a finish, so 64 stale runs showed as open.

### Phase 8u — A held Astra review records the end of each turn  · status: done

**Depends on:** panes 28f

#### As-built

- A held Astra role with no phase binding: `design_roles.dispatch_turn` opens phase `astra-review` in the design run only when it is not open, and after each `assign.sh` return records `finish-phase` with `completed` (exit 0) or `error` (refusal). When that record fails after a refused dispatch, the raised `RoleRefused` keeps the dispatch's refusal text.
- `design_roles._finish_end` records `finish-run --status completed`, under the lock, for an active unbound design run. A finished run stays as it is.
- A design bound to a unit phase (`DesignBoundToPhase`) records nothing in its own run; its phase belongs to the unit run.
- The one-shot `/astra_review` path (`assign.py` `execute_astra_review_herdr`, run folder `/tmp/claude/astra/<run>`) records `start-run` and `start-phase` inside its `try`. Its `finally` ends the worker, then records `finish-phase --phase-id astra-review` (when the phase started) and `finish-run`, each `completed` on success and `error` otherwise. A recording failure makes the exit non-zero and keeps the first error text.
- The Astra pass end has one place: `worker.dispatch` records it on success, and `assign.py` records `finish-pass --status error` only for a failed dispatch.

**Files:**
- `scripts/astra/design_roles.py` — phase and run ends of an unbound design run.
- `scripts/direct/assign.py` — the one-shot `/astra_review` run lifetime.
- `scripts/astra/test_design_roles.py`, `scripts/direct/test_assign_launcher_review.py` — lifetime rows: no open phase after a turn or a review, a finished run, one `pass_finished` event.

**Gotchas:** Keep one place that records an Astra pass end; a second `finish-pass` gives two `pass_finished` events.

**Ruled out:** A `finish-pass` call in `execute_astra_review_herdr` for the success path — `worker.dispatch` owns it.
