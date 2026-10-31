# verify

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Owner of `verify.sh`, its final pass, its cache, its lint and test scope, its speed and the Mac test code; first change: cut the Python helper start time in each verify run.

> **Production: builds** — unit `verify-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 08:52 EDT

i need changes to verify

## Scope

- **From the showrunner (2026-10-29):** this unit owns `scripts/direct/verify.sh`, `scripts/direct/final_pass.py`, the verify cache, lint and test scope, verify speed and the Mac test code (`scripts/mac_test/`), taken over from builds-unit. The user will name the verify changes they want. This unit moves under the builds showrunner when builds is promoted.
- **Known item:** shutdown-unit measured about 11 s of Python helper start per verify pipeline step (real CPU). This is Phase 1.
- **Not this unit's work:** the `TOKEN_WAITED` unbound fix in `verify.sh`. sdlc's phases-unit lands it; do not write a second fix. Phase 1 merges origin first, so it builds on that fix.
- **The user's changes:** each change the user names becomes one new phase after the last phase, written here and sent to the showrunner before dispatch.

## Delegation Context

- **Project:** `~/.claude` scripts — `verify.sh` is the one entry point every Rust build, test, lint and doc step uses on natedev and the Mac.
- **Project started:** 2026-10-29T12:55:26.085+00:00
- **Stack:** bash (`verify.sh`), Python 3.11+ through the `scripts/lib/py` shim, `unittest`, basedpyright.
- **Layout:**
  - `scripts/direct/verify.sh` — the verify entry point
  - `scripts/direct/final_pass.py`, `step_record.py`, `local_release_wait.py`, `progress_history.py` — helpers `verify.sh` starts
  - `scripts/direct/test_verify_*.py` — verify pipeline tests
  - `scripts/mac_test/` — Mac offload: `mac_test.py`, `offload.py`, `copy_watch.py`, `audit.py` and tests
  - `scripts/buildlog/record.py`, `scripts/buildlog/mac_idle.py`, `scripts/build_targets/build_targets.py` — helpers `verify.sh` starts, owned by other units (read, do not edit without the owner)
  - `scripts/lib/py` — interpreter shim; caches the resolved path, so it adds no Python start of its own on a warm cache
- **Key files:**
  - `scripts/direct/verify.sh` — a `VERIFY_PY_TRACE=<file>` run writes one line per helper start (name, wall ms, CPU ms); every inline helper runs as `VERIFY_PY_HELPER=<NAME> "$PY" …`
  - `docs/as-built/rustdoc-once-and-final-targets.md`, `docs/as-built/mac-test-offload.md`, `docs/as-built/build-targets.md` — how verify, its final pass, the Mac offload and build targets work now
- **Test lanes:** none (no Rust crate); tests sit beside their modules as `test_*.py`.
- **Build:** none (scripts).
- **Test:** `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>` after a merge of `origin/natedev` (memory: merge before a gate). For a quick loop: `python3 -m unittest discover -s scripts/direct -p 'test_verify*.py'` and `python3 -m unittest discover -s scripts/mac_test -p 'test_*.py'`.
- **Lint:** `basedpyright scripts/direct scripts/mac_test` plus every caller module of a changed Python file — pass is its `0 errors, 0 warnings` line; `bash -n scripts/direct/verify.sh`; `shellcheck scripts/direct/verify.sh` with no new findings.
- **Invariants:**
  - `verify.sh` output, exit codes, cache records and build-log records stay the same for every verb (`check`, `test`, `lint`, `doc`, `final`), local and on the Mac.
  - Every Python call goes through `$PY` (the shim), never plain `python3`: the Mac's `python3` on PATH is Apple 3.9.
  - Before any run that builds or tests, check `build_hold.py status`; while a natedev hold that is not ours is in force, run nothing (memory: no work during a build hold).
  - Helpers owned by other units (`scripts/buildlog/`, `scripts/build_targets/`) change only through their owner, or with the owner named in the checkpoint notice.

## Phases

### Phase 1 — Cut Python helper starts in each verify run  · status: done

#### As-built

- `verify.sh` reads `cargo metadata` once per run: `read_metadata` prints `GATE_METADATA` or runs `cargo metadata --no-deps --format-version 1`, and `load_metadata` (once, `METADATA_READY`) sends it to one `verify_metadata.py` start with every request the verb needs. `require_member`, `example_features` and `take_test_targets` read the shell values it sets. The query path and early `doc` exits start no metadata helper.
- `verify_metadata.py` reads the JSON on stdin and prints `NAME=<shlex-quoted value>` per request: `member:<pkg>`, `example-features:<pkg>:<example>`, `test-targets:<pkg>:<features>`, `build-targets:<pkg>:<kind>:<name>`, `untested-examples:<scope>`, `workspace-root`, `target-directory`. Refusals keep the old stdout, stderr and exit 2.
- `VERIFY_PY_TRACE=<file>` makes `$PY` a runner that runs each helper in the same interpreter and appends one row, `helper<TAB>wall ms<TAB>CPU ms`. CPU excludes child processes. The helper is the script basename, or `VERIFY_PY_HELPER` for an inline `-c` or stdin script. A row is written once, at exit, exec or SIGTERM; SIGTERM is sent again, so the exit status stays. Unset, it adds no start.
- Clock reads use `$EPOCHREALTIME`. `step_record.py write --clock-offset` prints wall minus monotonic microseconds (`VERIFY_MONOTONIC_OFFSET_US`), so Mac retry deadlines stay in `mac_idle.py`'s monotonic time. `token_waiters_key` hashes with `sha256sum` or `shasum -a 256` (first 16 hex). `STEP_OWNER_PY` runs through `$PY`.
- `step_record.py` parses `write`, `state`, `remove` and `stopped` from one table, `_COMMAND_ARGUMENTS`, before `argparse`, `dataclasses` and `inspect` load. Help, other syntax and record diagnostics fall through to `main()`, whose argparse parser comes from the same table.
- `token_wait_note` keeps a holder name read from a file with no trailing newline (`read … || true`).
- Real lint run: this unit's helper CPU 2529 → 1824 ms over 48 starts (`step_record.py` 30 starts, 1860 → 1247 ms). The other 525 ms is in helpers outside this phase: `build_targets.py` 199, `mac_idle.py` 122, `nix_env.py` 96, `record.py` 71, `user_zone.py` 37.

**Files:**
- `scripts/direct/verify.sh` — trace runner, `read_metadata`, `load_metadata`, Bash clocks, `token_waiters_key`, `token_wait_note`
- `scripts/direct/verify_metadata.py` — `answer`, `main`: one metadata parse per run
- `scripts/direct/step_record.py` — `_COMMAND_ARGUMENTS`, `_parse_record_arguments`, `_execute_record_command`, `--clock-offset`
- `scripts/direct/test_verify_metadata.py`, `scripts/direct/fixtures/verify_metadata/` — 55 golden outputs of the old inline helpers on three fixture workspaces
- `scripts/direct/test_verify_final_pass_script.py` — trace rows, query row, unset trace, SIGTERM
- `scripts/direct/test_step_record.py` — the CLI path loads no `argparse`, `dataclasses` or `inspect`
- `scripts/direct/test_verify_step_owner.py` — holder with no trailing newline
- `scripts/direct/test_verify_assertion_guard.py`, `verify_hold_wait_test_support.py`, `verify_lint_scope_test_support.py`, `verify_step_routes_test_support.py`, `verify_mac_offload_test_support.py` — harnesses on the metadata route, the `$PY` shim, the SHA tool and Bash clocks; one metadata call per lint

**Gotchas:** a harness that runs `verify.sh`'s lint case needs `read_metadata`, `METADATA_READY=0`, `load_metadata` and `require_member` from `verify.sh`. `DEV_FEATURES_PY` reads `Cargo.toml`, not metadata, so a malformed list refuses before Cargo; `CHANGED_MEMBERS_PY` still decodes the cached JSON after mend. The clock offset is read once, so a wall clock jump moves a Mac retry deadline.

**Ruled out:** merging `STEP_OWNER_PY` with the step record (different lifetimes) or `state` with `remove` (shell work between them); lazy imports in `final_pass.py` and `local_release_wait.py` (no import over 30 ms); a Bash 3.2 clock fallback (the Mac's verify path runs Bash 5.3.9).

### Phase 1b — Refuse a step with a failing exit, and read a process start on the Mac  · status: todo

**Depends on:** Phase 1 (same files: `step_record.py`, `verify.sh`). Phase 7 lands first (shared extractor, board.sh and verify.sh).

**Origin:** builds showrunner order 2026-10-30 03:5x EDT, a wrong-result defect from hana (trunk P170 board, 02:53 EDT). Runs in this worktree.

#### Work Order

**Goal:** a refused step makes `verify.sh` exit non-zero on every host, and every process-start reader in this unit's code reads a start on the Mac as it does on Linux, through one shared reader.

**Defect:** on the Mac, `verify.sh`'s step recorder (`step_record.py process_start`) reads `/proc/<pid>/stat`, which Darwin does not have. The step is refused, but the wrapper exits 0, so the failure reads as a pass.

**Spec:**

(1) Write the test that shows the exit 0 first: `verify.sh` on a faked Darwin host (no `/proc`) whose step record refuses. Both trees already end a failed `write` with `|| exit 2` (Phase 7's `env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" "${record_args[@]}" || exit 2`; Phase 1's `VERIFY_MONOTONIC_OFFSET_US="$(… write … --clock-offset)" || exit 2`). Use that test to find the path that turns the refusal into 0, and change only that path, so a recorder refusal exits non-zero. If the test shows a non-zero exit already, make no exit-status change: the Darwin read in (2) closes the defect. Two Phase 7 statuses are not recorder refusals and keep their behavior: board renewal's inconclusive status 2 and the ownership guard's internal 125 retry (Constraints).

(2) One shared reader, `scripts/lib/process_start.py`, for Linux (`/proc/<pid>/stat` field 22, under `$VERIFY_PROC_ROOT`, default `/proc`, as `step_record.py` reads it now) and Darwin (`ps -p <pid> -o lstart=` under `LC_ALL=C TZ=UTC`, from `message/sessions.py process_start`'s tested form). It returns named results, not a bare `str | None`:
- `Started(start)` — a live process and its start.
- `Zombie(start)` — a zombie process; its start is still read.
- `Gone` — no such process.
- `Unreadable(reason)` — the start cannot be read, but the process may exist (a read error other than not-found, a field that does not parse, a `ps` failure other than no such process).

`start` has one stored form per host, the same text that every writer stores and every reader compares: the field-22 digits on Linux, the `LC_ALL=C TZ=UTC` `lstart` text on Darwin. A match is a start equal to the stored one; a different start is PID reuse. Every reader in this unit's code uses the shared reader, and its policy is now explicit:
- `scripts/direct/step_record.py` `process_start` — `write` stores a `Started` start. `_read_fields` reads `Gone`, `Zombie` and a different start as `stale`, and `Unreadable` as `unreadable`.
- `scripts/direct/verify.sh` `STEP_OWNER_PY` `identity(pid)` — writes the `start` of the `owned` record.
- `scripts/direct/board.sh` `owned_group_matches` — a `Started` match keeps an expired hold. `Unreadable` also keeps it, so a live owner keeps its token. `Gone` and a different start free it.
- `scripts/direct/board.sh` `cleanup_owned_group` `identity(pid)` — a `Zombie` leader with live children in its group still matches. `Gone`, `Unreadable` and a different start take the existing mismatch branch (refuse while the group has live members, else done).
- `scripts/mac_test/mac_test.py` `process_start_time` — removed. `run_is_live`, `run_is_kept`, `copy_gives_way`, the claim writes and `adopt_wait_claim` read the shared result and keep their outcomes: only a match is live, and an adoption refuses (exit 12) unless the from-pid start matches the stored `proc_start` and the runner's start reads as `Started`.

The writer (`STEP_OWNER_PY`), both board.sh readers and mac_test.py change in one commit and keep one stored start form. A reader with another form reclaims a live hold on the Mac (Phase 7 Spec 5).

`scripts/lib/` and `scripts/direct/board.sh` belong to natedev. builds approved `scripts/lib/` as outside this unit's Owns (03:5x), and the showrunner ruled board.sh into this phase (2026-10-30). The checkpoint notice names both in also-touches lines (owner natedev), tested against `origin/natedev` after a trial merge (production contract item 9). The other copies (`message/sessions.py`, `build_hold/build_hold.py`, `lint/memory_admit.py`, `lint/tmp_sweep.py`, `production/retire_unit.py`) go to their owners through builds after 1b merges: build_hold, memory_admit and tmp_sweep to resources; sessions.py and retire_unit.py to natedev. `step_record.py stopped` (`consume_stopped_build`) also reads `/proc/sys/kernel/random/boot_id`, which Darwin does not have; the Darwin test covers it too.

(3) Test-local Bash function extractors use the shared `shell_functions(path, *names)` in `scripts/direct/shell_function_test_support.py` (each named top-level definition, in the order given; `ValueError` for a missing name). It extracts every function named here byte-equal to the old extractors, so it needs no change; write no second extractor. In `scripts/direct/test_verify_step_owner.py`, delete `shell_function` and call `shell_functions(VERIFY, "token_wait_note")`, `shell_functions(INVOKE, "buildlog_exec")` and `shell_functions(INVOKE, "sweep_after_step")`. In `scripts/direct/test_verify_assertion_guard.py`, `VerifyAssertionGuardTests.lint` replaces the `read_metadata() {` … `require_taplo() {` text slice with `shell_functions(VERIFY, "read_metadata", "load_metadata", "require_member")` plus a `METADATA_READY=0` line in its script (the slice carried that line). It replaces the `assertion_rulings_args() {` slice with `shell_functions(VERIFY, "assertion_rulings_args")` plus an `assertion_rulings_args` call line after it: the old slice carried that call (Phase 1 `verify.sh:841`), and without it the `with_session` cases lose their rulings. No assertion changes; the session-ruling assertions stay as they are.

**Files:**
- `scripts/lib/process_start.py` — new: the shared reader and its four named results
- `scripts/lib/test_process_start.py` — new: Linux and faked-Darwin reads, each named result, agreement with the record's stale check
- `scripts/direct/step_record.py` — `process_start` on the shared reader
- `scripts/direct/verify.sh` — `STEP_OWNER_PY` `identity(pid)` on the shared reader; the Spec (1) exit path if the test finds one
- `scripts/direct/board.sh` — `owned_group_matches`, `cleanup_owned_group` `identity(pid)` (owner natedev; also-touches line)
- `scripts/mac_test/mac_test.py` — `process_start_time` replaced by the shared reader
- `scripts/mac_test/test_mac_test.py` — patches of `mac_test.process_start_time` move to the shared reader; Darwin claim adoption
- `scripts/direct/test_step_record.py`
- `scripts/direct/test_verify_token_ownership.py` — board cases: unreadable start keeps the hold, PID reuse frees it, zombie leader with live children
- `scripts/direct/test_verify_step_owner.py` — Spec (3): shared extractor in place of `shell_function`
- `scripts/direct/test_verify_assertion_guard.py` — Spec (3): shared extractor in the `lint` harness

**Workers:** 1 writer (the shared reader, every caller and their tests share one owner set).

**Constraints from prior phases:**
- Combined baseline first. Phase 1 is checkpoint 766b3e87 on `natedev-verify` (built on builds 31eaac9c); Phase 7 is checkpoint 3be58002 on `natedev-verify-token` (built on builds 9a564a8b); neither is on builds yet. Before any code change, work on one tree that holds both. A read-only trial merge of the two has two code conflicts, both in `verify.sh`: the comment above `mac_retry_hook` (keep both facts), and `buildlog_exec`. `buildlog_exec` takes Phase 7's body (start and stop the renewer, `printf lost > token-lost; return 125` when the renewer cannot start, `VERIFY_TOKEN_LOST_FILE`, `step_status`) with Phase 1's `VERIFY_PY_HELPER=STEP_OWNER_PY` label on the `buildlog_exec_unowned "$PY" -c "$STEP_OWNER_PY"` call. Both phases edited `verify_cleanup`; keep both. On the combined tree, run both phases' regressions before the first change: `test_verify_metadata`, `test_verify_final_pass_script`, `test_step_record`, `test_verify_step_owner`, `test_verify_token_renew`, `test_verify_token_ownership`, `test_verify_token_same_slot`, `test_verify_lint_output` and `scripts/mac_test/test_mac_test.py`.
- Phase 7 statuses that are not recorder refusals. board.sh `cmd_renew` returns 2 when its bounded guard wait (`wait_lock_guard`, 200 × 0.05 s) runs out; `renew_token` reads 0 and 2 as renewed, and the renewer keeps going. `STEP_OWNER_PY` exits 125 and writes `$VERIFY_TOKEN_LOST_FILE` when `cargo.d/holder_pid` no longer names this run; invoke.sh's `run_once` retry loop over `run_step_attempt` then calls `buildlog_retry_unstarted_step`, which releases memory, takes the token again and admits the step again. That attempt records no step result. A terminal 125 (lost token, or `cargo mend` 125) ends lint with 125: non-zero, rerunnable, and `lint_failure_is_the_tree` does not cache it as a tree failure.
- Phase 7 owner record. `STEP_OWNER_PY` writes `{"group": <pid>, "start": <identity>}` to `cargo.d/owned` under the `cargo.guard` lock. `lock_stale_reason` keeps an expired hold while `owned_group_matches` succeeds; a dead `holder_pid` is still freed. `cleanup_owned_group` checks the group with `live_group` (`ps -A -o pgid= -o stat=`). Today `STEP_OWNER_PY.identity` and both board.sh readers fall back to `ps -p <pid> -o lstart=` with no `LC_ALL` or `TZ`, so their Darwin text changes with locale and zone. board.sh starts its readers with plain `python3` (Apple 3.9 on the Mac), so `process_start.py` imports and runs under Python 3.9.
- Phase 7 Mac claims. `mac_test.py process_start_time(pid) -> str | None` reads `/proc` only and gives `None` for every failure. `adopt_wait_claim` (`claim --from-pid`) moves a wait claim to the runner only when `record["pid"] == from_pid` and the from-pid start equals `proc_start`; else it exits 12.
- Phase 1. In `step_record.py`, `process_start(pid)` reads `$VERIFY_PROC_ROOT` (default `/proc`) and treats a zombie (`Z`) as ended; `write` stores it as `proc_start`, and `_read_fields` calls a record stale on `FileNotFoundError`, `ProcessLookupError` or a different start. The CLI path (`write`, `state`, `remove`, `stopped`) runs before `argparse`, `dataclasses` and `inspect` load; `StepRecordTests.test_cli_updates_records_without_importing_argument_or_dataclass_machinery` fails if the helper import brings any of them, so the shared reader loads none of them (its named results are not dataclasses). A CLI error prints `step record: <error>; step refused` and exits 2; `verify.sh` takes the `write` status at `VERIFY_MONOTONIC_OFFSET_US="$(… write … --clock-offset)" || exit 2`. The `verify.sh` inline reader is `identity(pid)` in `STEP_OWNER_PY`, run as `"$PY" -c`, so `scripts/lib/` is not on its `sys.path` unless it is given.

**Acceptance gate:**
- `test_verify_step_owner` and `test_verify_assertion_guard` pass with no assertion change.
- The Spec (1) test comes before any exit-status change. A recorder refusal makes `verify.sh` exit non-zero (a fake process root with no `stat`).
- The shared reader reads a start on Darwin (faked `sys.platform` and `ps`) and on Linux, and both reads agree with the record's stale check.
- One case per named result, on Linux and faked Darwin. Gone. Unreadable: while the hold is expired and the owner's start cannot be read, a rival's `board.sh acquire` is refused, so a live owner keeps its token. Zombie leader with live children: `cleanup_owned_group` still matches it, and a step record calls it stale. PID reuse: a new process on the stored pid with a different start frees the board hold, makes the step record stale and makes a Mac claim not live. Darwin claim adoption: `mac_test.py claim --from-pid` adopts a wait claim whose from-pid start matches in the `lstart` form, and exits 12 when it differs.
- A second hana hit is its own test case: `verify.sh` run directly on the Mac refused at step recording with `[Errno 2] No such file or directory: /proc/22626/stat; step refused (exit 2)` while widget took P34's Mac shots (builds, 2026-10-30). The test runs `verify.sh`'s step record on a faked Darwin host with no `/proc` and expects the step recorded, not refused.
- Phase 7 outcomes stay. These `test_verify_token_renew.py` regressions pass unchanged: `test_busy_board_guard_does_not_stop_memory_wait_renewal` (renew status 2 keeps the renewer going); `test_ownership_guard_refusal_readmits_before_running_and_records_only_the_step` and `test_token_lost_during_memory_wait_is_reacquired_before_the_step` (the internal 125 retry gives no user-facing failure and no phantom step record); `test_running_step_renews_past_its_original_hold_and_stops_on_release` and `test_expired_matching_live_step_keeps_ownership_but_dead_holder_is_freed`; `test_lost_token_status_is_not_cached_as_a_tree_lint_failure` (terminal 125: non-zero, rerunnable, no cached tree failure).
- `scripts/mac_test` tests pass. basedpyright 0/0/0 on changed files and their callers; the merge gate passes. A trial merge of `origin/natedev` passes the board.sh and `scripts/lib/` tests before the notice. Hana is told when it lands.

### Phase 2 — Cancel a test or lint run when the tree changes under it  · status: done

#### As-built

- `scripts/direct/tree_watch.py` owns the verify cache key and the watch key, so both come from one file walk. `TreeInputs(args, metadata)` takes the call's scope and the `cargo metadata` JSON on stdin. `--key [--watch-start FILE]` prints the cache key and can write the start watch key; `--watch-key` prints only the watch key; `START FIRED PARENT [--signal-parent]` runs the watch loop.
- `TreeInputs.watched(path)` counts a file only when the cache key counts it and it is inside a workspace member or is a Cargo root input (`Cargo.toml`, `Cargo.lock`, `rust-toolchain`, `rust-toolchain.toml`, `.cargo/`). Root inputs are relative to the metadata's `workspace_root`, not the git root; with no `workspace_root`, root inputs do not cancel. A `test` watches its package, its path dependencies and each member's `Cargo.toml`. Files under `UNIT_SESSION_DIR` and `UNIT_RUN_DIR` never count.
- The loop reads the watch key every `VERIFY_TREE_WATCH_S` seconds (default 5). On a non-empty key that differs from the start key, it writes FIRED, sends `SIGUSR2` to the parent and exits 0; it exits 3 when the parent is gone. An unreadable git snapshot gives an empty key and never fires.
- `verify.sh` puts `start_tree_watch` / `finish_tree_watch` around each watched span: `test` from its verdict key to the end of nextest, local and in `try_mac_test`; `lint` only around `invoke_clippy`, after mend, `clippy --fix` and format. Other verbs do not watch.
- `cancel_tree_run` runs on `SIGUSR2`, after the step ends, or in `finish_tree_watch` when FIRED is set. It stops the Mac runner (TERM), releases the cargo token, kills the step's process group, removes this call's token queue entries (also `.again`), releases the memory reservations and waiter files listed in the watch folder, prints `verify.sh: cancelled — the tree changed while <cmd> ran, so this result would be stale. Resubmit the <cmd> when your edits are done.`, records the outcome `cancelled` and exits `TREE_CHANGED_STATUS=77`. It writes no cache record and does not run the step again. The stale-record refusal in `cache_record` stays as the last check.
- The build log outcome `cancelled` is in `record.py`, and `report.py` keeps it out of failure counts. `mesh_runs.py` reports `CANCELLED (exit 77): resubmit after your edits.` with the verify cancel line, and `write_prompt_contract.md` tells workers that 77 is not a test failure.
- `merge_checkpoint.py`: `stop_cancelled_test` stops on 77 from a first run or a flaky-test rerun, with no retry and no rollback, and the CLI exits 77 with a resubmit message. A child handoff touches `git rev-parse --git-path checkpoint-tests-pending/<checkpoint>` before it merges; a resumed handoff with that marker runs `checkpoint_tests` before any push or promotion, and a pass deletes it.

**Files:**
- `scripts/direct/tree_watch.py` — `TreeInputs`, the two keys, the watch loop
- `scripts/direct/verify.sh` — `tree_key`, `start_tree_watch`, `finish_tree_watch`, `cancel_tree_run`
- `scripts/buildlog/record.py`, `scripts/buildlog/report.py`, `scripts/agents/mesh_runs.py`, `docs/direct/write_prompt_contract.md` — status 77 for readers
- `scripts/production/merge_checkpoint.py` — `stop_cancelled_test`, `handoff_tests_pending_path`
- `scripts/direct/test_tree_watch.py`, `scripts/direct/tree_watch_test_support.py` — scope table, parent exit, unreadable git, nested workspace
- `scripts/direct/test_verify_tree_cancel*.py`, `scripts/direct/verify_tree_cancel_test_support.py` — end to end with a fake cargo: local, Mac, lint, token reacquire, progress history
- `scripts/direct/test_mesh_tree_cancel.py` — of two callers, only the caller whose tree changed gets the cancel
- `scripts/buildlog/test_report_tree_cancel.py`, `scripts/production/test_merge_checkpoint_tree_cancel.py`

**Gotchas:** `progress_history.py finish-activity` accepts only `canceled`; `finish_activity` in `verify.sh` maps `cancelled` to it, and the build log keeps `cancelled`. `run_once` takes memory admission in a pipeline subshell, so it writes each reservation path to the watch folder before cargo starts; a kill otherwise leaks it. A harness that sources the lint path of `verify.sh` must stub `start_tree_watch`, `finish_tree_watch` and `stop_tree_watch`. A test that makes a fixture class by hand (`Fixture()` then `setUp()`) must also call its `setUpClass()` and `doClassCleanups`.

### Phase 3 — Scale the memory promise for a filtered test run  · status: done

#### As-built

- `expected_peak` sends a filtered run of a known package to `filtered_peak`, then caps the result at the full-suite promise of the same step. The source and sample count stay those of the filtered history.
- `filtered_peak` uses this repository and host over `HISTORY_DAYS` and takes the first history with samples: `anon_peaks.jsonl` rows with the exact sorted filter set; index rows (argv) with that set; all filtered rows of the package in `anon_peaks.jsonl`; then in the index. Index peaks are scaled by `ANON_SHARE`. With no filtered history, the promise is the larger of `FILTERED_FLOOR` (1 GiB) and the p90 of the package's `check` and `build` peaks, else `FILTERED_FLOOR`.
- `verify_test_selection(argv)` gives `PackageTestSelection(package, filters)` only for the whole expression `verify.sh` emits: `package(<pkg>) & test(<a>)`, a union of `test()` names, or a bare `package(<pkg>)` for a full run. Every other expression is `BroadTestSelection`. `test_package` and `test_filters` read it. A filtered run with no package (`--workspace -E 'test(x)'`) keeps the filtered-scope history and `FALLBACK`.
- Waiters, reservations and `anon_peaks.jsonl` rows carry `package` and `filters`. A waiter's cached estimate is used again only when its package and filter set match the call's argv.
- From history: `test hana --filter native_rate` 10.87 → 9.38 GiB; one `hana_diegetic` test 19.33 → 12.14 GiB (the full-suite cap).

**Files:**
- `scripts/lint/memory_admit.py` — `FILTERED_FLOOR`, `filtered_peak`, `verify_test_selection`, `test_package`, `test_filters`
- `scripts/lint/test_memory_admit.py` — exact set, package fallback, floor, compile fallback, cap, negated and package-less expressions

**Gotchas:** a negated predicate (`package(hana) & !test(x)`) selects almost the full suite, so only the whole positive expression gives an exact-set key. `FILTERED_FLOOR` comes from 111 natedev filtered runs with no compile (anon p90 0.671 GiB).

### Phase 4 — Promise a fresh test run only its test memory  · status: done

#### As-built

- `is_fresh_test` calls a run fresh only when the last finished `nextest` step of the same worktree, package and scope has the same `test_build_key`, exited 0 or ran tests, ended less than 24 h ago, and its target directory still exists with no lint-sweep delete record and no `sweep` step since. Every other case, and every read error, is a compile run. `current_worktree_steps` reads the host's build-log JSONL and uses a validated index prefix only to read less.
- `fresh_test_peak` promises the p90 of zero-compile steps (`crates_compiled = 0`) of the same package and scope, joined to `anon_peaks.jsonl` by `call_id`: exact filter set first, then package and scope; at least `FILTERED_FLOOR`. With no such history it returns the compile promise of Phase 3. A fresh waiter is estimated again at each check.
- The build key comes from `TreeInputs.keys` (Phase 2): the test cache key without `--filter` names and without `NEXTEST_*` variables other than `NEXTEST_CARGO_PROFILE`, plus the metadata's `target_directory`. A target directory that is not absolute gives no key. `refresh_test_build_inputs` in `verify.sh` writes it with `VERIFY_COMPUTE_TEST_BUILD=1` and exports `BUILDLOG_TEST_BUILD_KEY` and `BUILDLOG_TEST_TARGET_DIR`. It runs once per test call and again after each build-hold wait; then it also refreshes `BUILDLOG_TREE` when recording has started.
- `record.py` stores `test_build_key` on a `nextest` step whose source did not change during the step; index schema 23 adds the nullable column. Older rows have no key and count as compile.
- The lint cache key holds the guard's inputs, read through the configured `ASSERTION_GUARD_SCRIPT` with `runpy` in the same Python start: each base the guard uses (Phases 5 and 6), the commit messages from each base to HEAD, HEAD, the guard file and the sorted rulings it extracts. A base that does not resolve gives no cache key. Prose around an unchanged ruling does not change the key.
- `assertion_rulings_args` makes one list from the nonempty `UNIT_SESSION_DIR` and `UNIT_RUN_DIR`, without a second spelling of the same folder. The guard's `--rulings` and the lint key both read it, so a Codex worker with only `UNIT_RUN_DIR` sees its rulings.
- From history: `test hana --filter native_rate` fresh 9.38 → 3.45 GiB; a compile run stays at 9.38 GiB.

**Files:**
- `scripts/lint/memory_admit.py` — `current_worktree_steps`, `is_fresh_test`, `fresh_test_peak`
- `scripts/direct/tree_watch.py` — build key and guard inputs in `TreeInputs.keys`
- `scripts/direct/verify.sh` — `refresh_test_build_inputs`, `assertion_rulings_args`
- `scripts/buildlog/record.py`, `scripts/buildlog/index.py` — `test_build_key`, schema 23
- `scripts/lint/test_memory_admit.py` — fresh and compile cases, index prefix and JSONL tail, target and sweep
- `scripts/direct/test_verify_lint_guard_key.py` — build-key inputs, hold refresh, private origin/HEAD move, rulings, merge parent, guard file
- `scripts/direct/verify_lint_scope_test_support.py` — guard stub with the readers the key loads

**Gotchas:** `BUILDLOG_TREE` leaves out features, toolchain and environment, so it cannot prove a fresh build; only `test_build_key` can. The index can lag the JSONL, so a newer unindexed step must still be read. A guard stub for lint tests must expose `GuardError`, `default_base`, `git` and `read_rulings`, and exit only under `__main__`.

### Phase 5 — Compare assertions against the branch's own base  · status: done

#### As-built

- `default_base(repo, explicit_base="")` in `assertion_guard.py` takes the first base that resolves: the explicit base (`--base`; `verify.sh lint` passes `VERIFY_ASSERTION_BASE` as `--base`); the merge-base of HEAD with `@{upstream}`; the merge-base with the remote branch nearest to HEAD (fewest commits from merge-base to HEAD), from `refs/remotes/*` without `*/HEAD` and origin/HEAD's target. Phase 6a adds the production merge-branch base to the last two steps.
- Nearest candidates with one merge-base are accepted. No candidate, or nearest candidates with different merge-bases, raises `GuardError("cannot determine branch base; set VERIFY_ASSERTION_BASE (or --base)")`, and `main()` exits 2.
- There is no fallback to `origin/HEAD`, `main` or `master`. An explicit base that does not resolve fails closed; the upstream never replaces it.

**Files:**
- `scripts/direct/assertion_guard.py` — `default_base`
- `scripts/direct/verify.sh` — `VERIFY_ASSERTION_BASE` as `--base` in the lint stage
- `scripts/direct/test_assertion_guard_revisions.py` — upstream, detached HEAD, no upstream, ties, origin/HEAD exclusion, explicit base
- `scripts/direct/test_verify_assertion_guard.py` — pass-through in each lint mode, invalid base, no-base refusal

**Gotchas:** never write origin/HEAD in code or tests: hana worktrees share refs. Tests build private repositories with their own bare remote, and the clone sets origin/HEAD.

### Phase 6 — Count only our own assertion removals on a merge, and make the commit hook agree with the guard  · status: done

#### As-built

- `--base` repeats. `run()` judges each base with `findings_against_base` and keeps a finding only when it shows against every base. `finding_identity` matches on path, test, kind and normalized old and new text, not on line numbers; a repeated assertion counts the smallest number of times it shows against any base.
- With no `--base`, the bases are `default_base` (Phase 5) plus each parent of HEAD after the first, so on a merge commit the other parent's own removals do not block.
- `commit_bases(repo)` gives the parents of a commit being made: HEAD plus each commit in the worktree's `MERGE_HEAD`, resolved and deduplicated. `pre-tool-use-change-review-gate.py` passes each one as `--base`, adds `--staged` unless the commit uses `-a`, and sends the commit message on stdin with `--rulings -`, so the hook and the guard give one answer for one commit.
- Rulings come from the `--rulings` sources and the commit messages from each base to HEAD.

**Files:**
- `scripts/direct/assertion_guard.py` — `commit_bases`, `findings_against_base`, `finding_identity`, repeated `--base` in `run`
- `scripts/hooks/pre-tool-use-change-review-gate.py` — calls `commit_bases`
- `scripts/direct/test_assertion_guard_revisions.py` — merge in progress, completed merge, own removal, duplicate counts
- `scripts/hooks/test_change_review_gate.py` — hook output equals guard output for `commit -F` with and without a ruling, and during a merge

### Phase 6a — Take the newer of the upstream base and the production merge-branch base  · status: done

**Origin:** showrunner bump 2026-10-29 19:5x, ticket 108 from cargo-liner-style-names-unit. Branch `natedev-verify-base` from origin/builds, worktree `claude-build-followups-verify-base`; lands ahead of the open phases.

**Defect:** `default_base` takes `merge-base HEAD @{upstream}`. In a style unit worktree `@{upstream}` is the unit's own pushed branch, while refill fast-forwards the worktree to `style/main`. Commits that came in from `style/main` then count as the unit's change, and lint fails on six `assert_eq!` that values-unit phase 77 (b1408dc8) removed in cargo-mend `fixes/facade_redirect/module_aliases.rs:223-282`.

**Fix (ruling):** for every unit, with no style-only branch in the code, the base is the newer of `merge-base HEAD @{upstream}` and `merge-base HEAD origin/<merge branch>`. The merge branch comes from the production doc's `Merge branch` field (`production_units.production_field`), for the production whose Units table lists this worktree (or, failing that, this branch). No production match or no such remote ref leaves the upstream base alone. An explicit `--base` / `VERIFY_ASSERTION_BASE` still wins.

**Files:** `scripts/direct/assertion_guard.py`, `scripts/direct/test_assertion_guard.py`, `docs/as-built/phase-checks.md` (its guard row still says "origin's default branch").

**Acceptance gate:** basedpyright `0 errors, 0 warnings` on changed files; the guard tests pass, with one that reproduces ticket 108 (upstream behind, worktree fast-forwarded to the merge branch, the merge branch's removed assertions do not block, our own still do); the merge gate passes.

### Phase 6b — Pair an assertion whose literal became a named constant, and read no test start inside a string literal  · status: done

#### As-built

Checkpoint db889600 on `natedev-verify-base`, merged into builds as ee2f8c80.

- The assertion guard pairs a removed literal assertion with its replacement when the literal became a bare `const` name in the same file. The `const` has one literal value and one binding in that file. The guard reads each side's file at that side's own git revision, never the working tree.
- A pair needs an exact `&str` / `&'static str` string (plain or raw), `char` or `bool` literal, or an unsigned number with a type the guard can prove (a suffix, or the default `i32` / `f64`). A range endpoint in the second argument of a trusted `matches!` also pairs.
- All other forms stay as written and still report `removed`: statics, imported and qualified names, composed constants (`concat!`, arithmetic, a `const` set to another `const`), byte strings, signed numbers, and names inside nested macros.
- The guard fails closed. When it cannot be certain of a binding, it refuses to pair and blocks: non-ASCII code, `r#` identifiers, `extern crate … as`, incomplete macro paths, trusted macro names that are imported or bound again, `macro_use`, item and statement macros (`include!`), and `#[path]` / `cfg_attr(path)` modules. A glob import (`use x::*`), seen or not, can only refuse a pair.
- The test-owner scan skips text inside string literals (plain, raw, byte, Python triple-quoted) and comments, so `pub fn` text in a fixture string does not start a test.
- The production merge-branch match skips Units rows marked retired or removed.
- Real cargo-liner commits: 7a4fb6b8 pairs all 14 former removals (0 blocking); b1408dc8 pairs all 6; 2520c27d has 0 blocking removals and 2 nonblocking notes for values inside a nested `vec!`.

**Files:**
- `scripts/direct/assertion_guard.py` — constant table, binding census, refusal rules, literal-aware test-owner scan.
- `scripts/direct/assertion_guard_test_support.py` — shared fixture repo helpers.
- `scripts/direct/test_assertion_guard_revisions.py` — pairing across git revisions, refusals, glob census.
- `scripts/direct/test_assertion_guard_strings.py` — string and comment skips, macro and byte-string refusals.
- `docs/as-built/phase-checks.md` — the guard row lists the forms that pair and the forms that refuse.

**Binds later work:** the "Pair a literal with an imported crate constant" follow-up in `docs/plans/natedev-verify-next.md` takes the imported and exotic forms that refuse here. Four tests carry Assertion-Change lines for the narrower pairing: `test_literal_constants_pair_across_git_revisions`, `test_constants_composed_of_literals_pair_without_resolving_runtime_calls`, `test_imported_constants_use_each_sides_revision`, `test_visible_and_unknown_globs_take_part_in_file_binding_census`.

**Gotchas:** a pair that the guard cannot prove must block, not pass; every new form needs a refusal test first. Shadowing in Rust (function-local `mod`, inner `use`, type aliases, generic parameters, `include!`) makes a bare name mean a different value, so the census counts every binding of the name in the file.

**Ruled out:** evaluating `concat!` or arithmetic in a `const` — a constant pairs only when its value is one literal.

### Phase 6c — Name a removed assertion by the test that holds it, not by a nested function  · status: done

#### As-built

- `name_tests(findings, sources, code)` in `assertion_guard.py` gives each finding the name of the innermost test that encloses its line. An assertion after or inside a nested helper carries the test name, and an `Assertion-Change: <path>::<test>` ruling names that test.
- `enclosing_tests(rows, rust)` returns `TestScope(first, last, name)` items. Python uses `ast` to find `test_*` functions. Rust walks masked code for `fn` items marked by `TEST_MARK` (`#[test]`, `tokio::test`, `async_std::test`, `rstest`, `test_case`) and pairs their braces. A nested helper's scope ends where its body ends.
- A line outside every test keeps the name of the nearest `def` or `fn` above it. A Python syntax error or unbalanced Rust braces gives no test scopes, so all names in that file use this rule.
- `aggregate_test_removals(findings)` folds each `removed` finding of a removed test into the one `test removed` finding for that test and sets `Finding.removed_assertions`. The report prints `(N assertions)`. `run()` calls it once, after the intersection across all bases.
- `finding_identity` excludes the owner name, so the base intersection does not depend on naming. The report keeps the first base's name.

**Files:**
- `scripts/direct/assertion_guard.py` — `TestScope`, `enclosing_tests`, `name_tests`, `aggregate_test_removals`, `Finding.removed_assertions`
- `scripts/direct/assertion_guard_test_support.py` — shared repo fixtures and guard helpers
- `scripts/direct/test_assertion_guard_revisions.py`, `scripts/direct/test_assertion_guard_strings.py` — owner and aggregation tests for Python and Rust

**Gotchas:** Aggregate only after every base is compared. Aggregation per base drops a removal that only one base holds. Rust test bounds come from masked code, so `RUST_LITERAL` must hide every string form (`r`, `br`, `cr` raw strings). A brace in an unmasked literal moves a test scope.

### Phase 7 — Keep the cargo token through a long lint, and keep a run's queue place when the Mac is taken  · status: done

#### As-built

- `verify.sh` holds the cargo token for `TOKEN_HOLD_S` (`VERIFY_TOKEN_HOLD_S`, default 3600) and renews it every `TOKEN_RENEW_S` (`VERIFY_TOKEN_RENEW_S`, default 60, at most a third of the hold). `start_token_renewer`, `stop_token_renewer` and `with_token_renewal` give one renewer per span; a nested span uses its caller's renewer, and `release_token` stops it. `buildlog_exec` renews before each step and while it runs, and renewal also runs during the memory wait (`admit_with_token`), a Mac claim wait (`route_test` wraps `route_test_without_renewal`) and a held target (`build_hold_wait_local`).
- `renew_token` calls `board.sh renew … --pid $$`. `cmd_renew` takes the guard through `wait_lock_guard` (200 × 0.05 s) and returns 2 when the guard stays busy (inconclusive); the renewer keeps going on 0 and 2. `--pid` refuses a renew when `holder_pid` names another process.
- board.sh `lock_stale_reason` keeps an expired hold while `holder_pid` is set and `owned_group_matches` finds the `owned` record's group leader with its stored `start`. A dead `holder_pid` is still freed.
- A token lost before a step starts is not a step result. `STEP_OWNER_PY` writes `$VERIFY_TOKEN_LOST_FILE` and exits 125; invoke.sh `run_once` is a retry loop over `run_step_attempt`, and `buildlog_retry_unstarted_step` releases memory, takes the token again and admits the step again, with no step result for that attempt.
- The fix loop's change check and the tree-changed notice use `tree_key --tree-content` (no script hashes). The cache key keeps the hashes of verify.sh, invoke.sh and lint.conf, and `cache_record` refuses a lint pass when `lint_code_key` differs from `LINT_LOADED_CODE_KEY`, the key read when verify.sh loaded.
- `lint_failure_is_the_tree` never counts 125. `cargo mend` 125 ends lint with 125.
- The memory wait counts from its first start. `run_local_test` keeps it in `$HOLD_WAIT_DIR/memory-wait-first` and passes `BUILDLOG_MEM_WAIT_SINCE` and `BUILDLOG_MEM_WAIT_FIRST_FILE` to each `run_nextest`; memory_gate.sh `buildlog_memory_wait_time` reports the full age, so `mem_wait_s` covers every rejoin.
- One decision point for the Mac. The memory wait's leave hook runs `mac_test.py claim-wait`, which takes the repository's Mac claim under `state.lock` (the copy-yield protocol included) and touches `MAC_WAIT_CLAIM_FILE`. The runner gets `offload.py run --claim-owner-pid`, and `claim --from-pid` (`adopt_wait_claim`) moves the claim to the runner. A refused claim keeps the run waiting in place. `release_wait_claim` frees an unused claim after the Mac attempt and at cleanup.
- `shell_functions(path, *names)` in `scripts/direct/shell_function_test_support.py` returns each named top-level Bash definition in the order given, and raises `ValueError` for a missing name.

**Files:**
- `scripts/direct/verify.sh` — token renewer, `admit_with_token`, `buildlog_retry_unstarted_step`, `lint_code_key`, `tree_key --tree-content`, `release_wait_claim`, the `claim-wait` hook
- `scripts/direct/board.sh` — `wait_lock_guard`, `renew --pid`, `owned_group_matches` (owner natedev)
- `scripts/lint/invoke.sh` — `run_step_attempt` and the `run_once` retry loop
- `scripts/lint/memory_gate.sh` — `buildlog_memory_wait_time`, `BUILDLOG_MEM_WAIT_FIRST_FILE` (owner resources-unit)
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` — `claim-wait`, `claim --from-pid`, `adopt_wait_claim`, `OffloadRequest.admission`
- `scripts/direct/shell_function_test_support.py` — the shared Bash extractor
- `scripts/direct/test_verify_token_renew.py` — renewal, busy guard, unstarted-step retry, lint keys, first wait start, claim handoff
- `scripts/direct/test_verify_lint_output.py`, `test_verify_token_ownership.py`, `test_verify_token_same_slot.py`, `scripts/mac_test/test_mac_test.py`

**Gotchas:** board.sh has two process-start readers (`owned_group_matches` and `identity` in `cleanup_owned_group`), and both compare against the `start` string that `STEP_OWNER_PY`'s `identity(pid)` writes to `cargo.d/owned`. All three, and `mac_test.py process_start_time`, must keep one start form, or the Mac reclaims a live hold. A rejoined memory wait opens a new wait record at each rejoin's `first` line. `lint_code_key` runs at load for every verb, `tree_key --tree-content` adds one `tree_watch.py` start beside each `VERDICT_KEY`, and the renewer starts `"$PY" -c time.sleep` each interval; none of them sets `VERIFY_PY_HELPER`.

**Ruled out:** `mac_test.py free-now` as the leave hook (an unlocked read lost the claim to another step).

### Phase 8 — Name the holder of what a wait is on  · status: todo

**Depends on:** satisfied. sdlc phases-unit Phase 11 (`start-wait --holder-*`, `wait-holder` in `progress_history.py`) and resources-unit 45e60cc9 (`memory_admit.py blocker`, `BUILDLOG_MEM_BLOCKER`) are on builds 9a564a8b. Phase 7 (first memory-wait start, Mac claim handoff) is done. Needed by hierarchies' wait experiment.

#### Work Order

**Source:** sdlc hand-off, approved by builds 2026-10-29.

**Goal:** every wait verify.sh records names who holds what it waits on.

**Starting point:** checkpoint 4fa7a873 on `natedev-verify-holder` (reviewed before Phase 7) holds this work on the older base: `buildlog_memory_poll_hook` in `memory_gate.sh`, the holder lookups in `verify.sh` and 23 tests in `test_verify_wait_holder.py`. It has not landed. Port it to this Spec on the Phase 1 + Phase 7 tree: keep what fits, and change the hook to the form in Spec 5.

**Spec:**

1. `phase_wait_start` passes the holder to `progress_history.py start-wait`: `--holder-resource-kind slot|memory --holder-resource <slot name | memory pool> --holder-session <session id>`, or `--holder-unit <unit>` when only the unit is known. All flags or none; none records `HolderNotNamed`.
2. The holder is: for a slot, the board token owner; for memory, the blocker that the gate's `check` poll reports (spec 5); for a build hold, the hold's holder.
3. When the holder changes during the same wait, call `progress_history.py wait-holder --session-dir <dir> --wait-id <id>` with the same flags. A holder that goes away calls `wait-holder` with no flags (`HolderNotNamed`).
4. Tests: see the Acceptance gate.
5. Observation hook in `scripts/lint/memory_gate.sh` (owner resources-unit; showrunner ruling 2026-10-30; the edit goes through cargo-berth's normal flow). Keep it small:
   - `memory_gate.sh` defines `buildlog_memory_holder() { :; }` beside `buildlog_memory_line`, an override point of the same kind. `verify.sh` defines its own after it sources invoke.sh.
   - When the caller sets the shell variable `BUILDLOG_MEM_BLOCKER=1`, `buildlog_wait_for_memory` passes it to its `check` call, accepts one more `|` separator (15 or 18), and takes the last field off before the 18-field `IFS='|' read`. Without it, the call, the field count and every decision stay as they are.
   - The gate calls `buildlog_memory_holder "<blocker JSON>"` once per check answer, after admission validation (state and reason, source, numeric fields, reservation) and before the admit branch. It calls it in its own shell, not in a command substitution, so `BASHPID` and the `phase-wait.<BASHPID>.memory` file stay the same. The gate ignores the hook's status and output. The hook never changes admission, Phase 7's first-wait accounting (`BUILDLOG_MEM_WAIT_FROM`, `BUILDLOG_MEM_WAIT_FIRST_FILE`, `buildlog_memory_wait_time`) or resources' live-memory tripwire.
6. verify.sh's hook keeps the last JSON it saw and does nothing when the JSON is the same, so a steady wait starts no process. The JSON maps to flags: `{"session_id":…,"pool":…}` gives `--holder-resource-kind memory --holder-resource <pool> --holder-session <session_id>`; `{"unit":…}` gives `--holder-resource-kind memory --holder-resource memory --holder-unit <unit>`; `{}` gives no flags. The first blocked poll runs the hook before its `first` line, so before the memory wait is open the hook only keeps the value, and `phase_wait_start memory` passes it to `start-wait`. Once the wait is open, a different holder calls `wait-holder`. When `start-wait` or `wait-holder` fails, print `verify.sh: wait history NotRecorded: …` as `phase_wait_start` does, and keep the last recorded holder, so the next poll tries again.
7. Queue age and recorder intervals are different things. `mem_wait_s` and the waiting lines count from the first wait start (`$HOLD_WAIT_DIR/memory-wait-first`). Each recorder interval runs from its own `first` line to its `done` or `left` line. A rejoin after a refused Mac claim opens a new interval with the holder of that poll and keeps the first start for the queue age. A first check that admits opens no interval and makes no holder call.

**Files:**
- `scripts/direct/verify.sh` — holder lookup, `buildlog_memory_holder`, the two calls
- `scripts/lint/memory_gate.sh` — the observation hook (owner resources-unit, through cargo-berth; trial-merge against `origin/builds-resources`; the notice carries `also touches scripts/lint/memory_gate.sh (owner resources-unit), tested against <origin/builds-resources tip>`)
- `scripts/direct/test_verify_wait_holder.py` — new
- `scripts/lint/memory_admit.py` — read only here: resources-unit's 45e60cc9, on builds 9a564a8b. `python3 scripts/lint/memory_admit.py blocker --waiter <waiter file>` prints one JSON line `{"session_id","session_name","pool":"memory"}`, or `{"unit"}` when only a systemd unit is known, or `{}`; exit 2 when the waiter file is gone. On the existing `check` poll, `BUILDLOG_MEM_BLOCKER=1` adds the same JSON as pipe field 16 (field 19 with `BUILDLOG_MEM_DETAIL=1`, which the gate always sets); a `|` in a name comes escaped. Use the `check` field inside the memory wait loop (no extra process per poll); without the opt-in the output is unchanged

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/direct/verify.sh` and the `scripts/lint/memory_gate.sh` hook; hub: `scripts/direct/verify.sh` (every wait and the hook live there).
- `test` — `scripts/direct/test_verify_wait_holder.py`, from the Spec alone. It reuses `shell_functions` (`scripts/direct/shell_function_test_support.py`) and the shared fixtures `VerifyTokenWaitFixture` (`verify_token_wait_test_support.py`) and `VerifyHoldWaitFixture` (`verify_hold_wait_test_support.py`); no second extractor or fixture copy.

**Constraints from prior phases:**
- Phase 1: an inline `-c` or stdin Python helper in `verify.sh` runs as `VERIFY_PY_HELPER=<NAME> "$PY" …`, so a `VERIFY_PY_TRACE=<file>` run names its start.
- Phase 7: `buildlog_memory_line` in `verify.sh` opens the memory wait on `first` and closes it on `done` or `left`; the names `phase_wait_start`, `phase_wait_finish` and `buildlog_memory_line` are unchanged. `run_local_test` reads `$HOLD_WAIT_DIR/memory-wait-first` and passes it as `BUILDLOG_MEM_WAIT_SINCE` and `BUILDLOG_MEM_WAIT_FIRST_FILE`, so `mem_wait_s` counts from the first start. Each rejoin after a refused or declined Mac claim opens a new memory wait record at its `first` line. A step leaves the memory wait only when `mac_test.py claim-wait` holds the Mac; a refused claim keeps waiting in place.
- memory_gate.sh: `buildlog_wait_for_memory` calls `check` with `BUILDLOG_MEM_DETAIL=1`, accepts 14 or 17 `|` separators and reads 18 named fields with `IFS='|' read`; an extra field would join `head_label`. Per poll: the live-memory tripwire, the `check` call, admission validation, the cache update, then admit, `first`, the leave hook or a periodic line. Phase 7's edits (`buildlog_memory_wait_time`, `BUILDLOG_MEM_WAIT_FIRST_FILE`) are the base for the hook.
- `progress_history.py`: holder flags are `--holder-resource-kind {account,slot,memory} --holder-resource <text>` and exactly one of `--holder-session` or `--holder-unit`; all or none. Only the wait kinds `WaitingForQuotaRefusal`, `WaitingForMemoryOrSlot` and `ReadyWithoutCapacity` take holders (verify uses `WaitingForMemoryOrSlot`). `wait-holder` exits 2 for a wait that is not open or a time before its last holder observation. `phase_wait_start` keeps each wait id in `$HOLD_WAIT_DIR/phase-wait.<BASHPID>.<key>`.

**Acceptance gate:**
- Real-loop tests (real `verify.sh` and the real memory gate loop, fake checker answers) for: one holder per resource kind (slot, memory, build hold); a holder change mid-wait; a holder that goes away; a failed `start-wait` or `wait-holder` and the retry on the next poll; a rejoin, which opens a new interval with its own holder while `mem_wait_s` still counts from the first start; immediate admission, with no interval and no holder call; the no-holder case.
- These Phase 7 tests pass unchanged: the first-start `mem_wait_s` build-log assertions (`MemoryQueueIdentityTests` in `test_verify_token_renew.py`) and the claim handoff tests that prove a refused Mac claim leaves the run waiting (`MacWaitClaimTests.test_competing_waiters_cannot_leave_without_a_claim_and_runner_adopts_it` and `scripts/mac_test/test_mac_test.py`).
- Without `BUILDLOG_MEM_BLOCKER=1`, the memory gate's output and decisions are unchanged (`scripts/lint` tests pass).
- Merge gate `--tests-only` green on a tree merged with origin/builds; basedpyright 0/0 on changed files and callers. A trial merge of `origin/builds-resources` passes the memory gate tests before the notice.

### Phase 9 — Remove Phase 7's new Python starts  · status: done

#### As-built

- `verify.sh` `hash_stream` reads stdin and prints its sha256 with `sha256sum`, or `shasum -a 256` when `sha256sum` is absent; it returns 1 when the hash program fails. `lint_code_key` and `token_waiters_key` both use it.
- `lint_code_key` hashes `verify.sh`, `lint/invoke.sh` and the lint config (`LINT_CONFIG_FILE`, else `config/lint.conf`), each framed as `present\0<bytes>\0` or `absent\0\0`, under `set -o pipefail` in its own substitution. An unreadable input gives an empty key and exit 0, so the lint-result cache misses and never matches a wrong tree.
- One `tree_watch.py --key` start gives both `VERDICT_KEY` and `VERDICT_TREE_KEY`, byte-equal to the two separate calls; `cache_record` keeps its lazy `tree_key --tree-content` call for the mismatch case.
- The token renewer's delay is a background Bash `sleep "$TOKEN_RENEW_S"` that a stop ends at once.
- Helper Python starts per `verify.sh lint` run: 47 → 41 (`tree_watch.py` 14 → 10, inline Python 2 → 0).

**Files:**
- `scripts/direct/verify.sh` — `hash_stream`, `lint_code_key`, the paired key function, the Bash renew delay
- `scripts/direct/tree_watch.py` — `--key` mode also prints the tree-content key
- `scripts/direct/test_verify_python_starts.py` — trace count, key-pair equality, code-key change on edit or removal, unreadable input

**Binds later work:** `verify.sh` starts no new Python; any inline Python names itself with `VERIFY_PY_HELPER=<NAME>`.

**Gotchas:** an empty lint code key means a cache miss, never a hit.

### Phase 10 — A stopped verify run ends all of its processes  · status: done

#### As-built

Checkpoints c0377368 and 4751eb55 on `natedev-verify-cancel`.

- A TERM, INT or HUP sent to a verify run's process group ends every process the run started, frees its memory-queue place and exits 128 + signal (143, 130, 129), never 141. `verify_interrupted` is the one stop path: it stops the step group (its own group, from `set -m`) and releases what the step holds. `memory_admit` is unchanged.
- `run_log_tee` in `verify.sh` runs both run-log tees (`exec > >(run_log_tee) 2> >(run_log_tee >&2)`). Each tee ignores INT, TERM and HUP, runs `tee -i -a "$RUN_LOG"` and reads to EOF, so the run's last writes during the stop reach the log.
- `user_clock` runs `TZ="$(python3 -S …/lib/user_zone.py)"`, outside the `VERIFY_PY_TRACE` runner; `test_verify_python_starts` expects 40 traced rows.
- Test support: `VerifyHoldWaitFixture.start_memory_queued_lint()` parks a real `verify.sh lint` in the memory queue (real checker and ledger, `MemAvailable: 0`, `BUILDLOG_MEM_POLL_S=3600`), with `memory_queue_pids()`, `owned_process_snapshot()` and `wait_for_owned_exit(timeout)`. `wait_for_marker_exit(marker, timeout) -> set[int]` in `verify_mac_offload_test_support.py` is the one process-exit waiter, also for `VerifyMacOffloadFixture.assert_nothing_outlives`; reaping stays separate. The `sleep` stub in `VerifyTokenWaitFixture` ends only the zero-memory poll at once and passes every other delay to the real `sleep`.

**Files:**
- `scripts/direct/verify.sh` — `run_log_tee`, `user_clock`
- `scripts/direct/test_verify_hold_signals.py` — TERM, INT and HUP group-stop tests: no survivor within 5 s, queue no longer lists the run, exit 128 + signal, `outcome: interrupted`, no saved result
- `scripts/direct/verify_hold_wait_test_support.py` — memory-queued lint starter, queue and process readers
- `scripts/direct/verify_mac_offload_test_support.py` — `wait_for_marker_exit`
- `scripts/direct/verify_token_wait_test_support.py` — `sleep` stub with real delays
- `scripts/direct/test_verify_python_starts.py` — 40 traced rows

**Binds later work:** a group stop goes through `verify_interrupted` only; the three group-stop tests in `test_verify_hold_signals.py` stay green. A parked memory wait in a test uses `start_memory_queued_lint`, and a process-exit wait uses `wait_for_marker_exit`.

**Gotchas:**
- A run-log tee that dies on a group signal turns the next write into SIGPIPE (141) and skips `verify_interrupted`.
- The renewer's delay is Bash `sleep`; a `sleep` stub that returns at once makes the renewer spin and rewrite a lease that a test set to expire.

**Ruled out:** a sweep that cleans up after a stop, or a second cancel path beside `verify_interrupted`.

### Phase 13 — Stop the Mac copy watcher when verify ends before the watcher starts  · status: done

#### As-built

Checkpoint be7ffc50 on `natedev-verify-cancel`.

- `start_copy_watch` in `verify.sh` passes the launching shell's pid as `--parent-pid`: `BASHPID`, because `$$` stays the outer shell's pid in a subshell. Bash 3.2 has no `BASHPID`; there an exec'd child `sh` prints its `PPID`.
- `copy_watch.py` `Wakeup(read_fd, parent_pid)` and `wakeup_on_signals(parent_pid)` take that pid from the arguments. `Wakeup.stop_requested` stops when `os.getppid()` differs from it, also at the first check, so a watcher whose `verify.sh` ended before the watcher started stops at once.
- `--parent-pid` is a required argument; without it the watcher exits 2 (argparse error) and writes no result.

**Files:**
- `scripts/direct/verify.sh` — `start_copy_watch`
- `scripts/mac_test/copy_watch.py` — `Wakeup`, `wakeup_on_signals`, `--parent-pid`
- `scripts/mac_test/test_copy_watch.py` — `assert_parent_loss_stops_watcher(before_start=True)`; `test_parent_already_exited_before_watcher_initializes` (fails on f133fa6e), `test_missing_parent_pid_is_an_argument_error`

**Gotchas:**
- The gap test's wrapper waits for the reparent, then runs `os.execv` with the real watcher command. `runpy.run_path` inside `python -c` does not put `scripts/mac_test` on `sys.path`, so that form passes only with `PYTHONPATH` set and fails in the merge gate.
- Watch item: one `test_verify_hold_signals` TERM run had 2 run-log tees alive at 5.2 s under load; the rerun passed. A repeat points at the Phase 10 stop path.

**Ruled out:** `$$` as the parent pid; a fallback to `os.getppid()` when `--parent-pid` is missing; a second parent check or a sweep.

### Phase 14 — Run a test step on the Mac only, with named variables and a folder brought back  · status: done

**Origin:** two builds showrunner add-ons, joined into one phase (2026-10-31). (1) A test step that runs on the Mac only and never falls back to natedev. Tell performance when it is on main. (2) hana Phase 34: when `HANA_TOOLTIP_PLACEMENT_SHOTS` is set, hana test fixtures save PNGs. A Mac test run names the environment variables it forwards and the folder it fetches back. Tell hana when it is on main. This phase runs in worktree `claude-build-followups-verify-cancel`, branch `natedev-verify-cancel`.

**Depends on:** Phase 13 (same branch, HEAD 17a556b7, which holds Phases 9, 10 and 13 and origin/builds b43cf221). It does not depend on Phase 1b. Its edits stay apart from 1b's hunks (Constraints).

#### Work Order

**Source:** builds showrunner add-ons, 2026-10-31.

**Goal:** `verify.sh test <package> --mac "<why>"` runs the step on the Mac only. It waits while the Mac is busy, never runs on natedev, and refuses in one line that names the cause for every other Mac answer. On a Mac-only call, `--env NAME` sends named variables to the Mac test, and `--fetch <folder>` copies one folder back to natedev. Each forward goes through the one existing path: `try_mac_test`, then `offload.py run`, then `remote_test_command`.

**Spec:**

1. Flags. Like `--local`, each flag can be anywhere after the verb. The parse loop removes it from `ARGS`, so no flag enters `tree_key`, the cargo words or the call record's `command`.
   - `--mac "<why>"` sets `MAC_ONLY=1` and `MAC_REASON`. The why is required, with the same rule as `--local`: when it is missing, blank or starts with `--`, verify exits 2. This keeps `--local`'s form. Each run prints the why in its first routing line, so the run log shows why natedev was not used.
   - `--env NAME` can be given more than once. It forwards the value that NAME has in verify.sh's own environment (`NAME=value verify.sh test …`). A value never goes on verify.sh's argv, so it never enters the call's command, the cache key or a record. It does pass in the ssh remote command, which must carry it to the Mac. A variable that is not named never goes to the Mac. This is true now, and a test keeps it true.
   - `--fetch <folder>` can be given once. The folder is relative to the repository root, which is the mirror root on the Mac. One folder serves hana's need. A second `--fetch` is refused.
2. Refusals in verify.sh. Each one happens before any work and before the eligibility check. Each one exits 2 with one stderr line, and starts no runner, no cargo and no call record, like the `--local` refusals. These are the only policy checks:
   - `verify.sh: --mac needs a reason: --mac "<why this test must run on macOS>".`
   - `verify.sh: --mac applies only to test.`
   - `verify.sh: --mac and --local cannot be used together.`
   - `verify.sh: --env and --fetch need --mac "<why>".` The reason: without `--mac`, the step can run on natedev or as a copy, and neither of those paths forwards or fetches anything.
   - `verify.sh: --env <NAME>: not a variable name.` NAME must match `^[A-Za-z_][A-Za-z0-9_]*$`. A missing NAME gets the same line form.
   - `verify.sh: --env <NAME>: the runner sets it, or it changes what Cargo builds; it is not forwarded.` Refused names: `PATH`, `HOME`, `MACOSX_DEPLOYMENT_TARGET`, `RUSTFLAGS`, `RUSTDOCFLAGS`, and every name with the prefix `BUILDLOG_`, `LINT_`, `CARGO_`, `RUSTC`, `NEXTEST_`, `DYLD_`, `MAC_TEST_` or `VERIFY_`. The reasons: the remote command sets some of these names. Cargo and nextest names change the build or the test scope, and they also change the Mac build output for later runs. `DYLD_` names change what the loader loads.
   - `verify.sh: --env <NAME>: not set here; set it on the call (NAME=value verify.sh …).` The test is `${!NAME+x}`. An empty value is allowed.
   - `verify.sh: --fetch needs a folder relative to the repository root, such as target/shots.` and `verify.sh: --fetch is given twice; name one folder.`
   - `verify.sh: --fetch <folder>: not a plain relative folder.` This refuses a leading `/` or `-`, an empty, `.` or `..` part, and any character other than letters, digits, `.`, `_`, `-` and `/`. One trailing `/` is removed first.
   - `verify.sh: --fetch <folder>: git does not ignore it; name a folder the tests write, such as one under target/.` The check is `git -C <top level> check-ignore -q -- <folder>/`. An ignored folder is never in the tree key, the tree watch or the copy to the Mac. So the fetch and the Mac-side removal in Spec 6 cannot change a judged tree.
3. Mac-only routing. This is in `route_test_without_renewal` only. Do not edit the `route_test` wrapper.
   - `initialize_step_record`: a `natedev_only <reason>` answer refuses the call (Spec 4). The Mac reach and disk probe in `eligible` already finds an unreachable Mac at this point.
   - In the target loop, skip a natedev row (settings `-`), as `STEP_FORCE_MAC` does. Under `--mac`, `BUILDLOG_LOCAL_REASON` stays unset.
   - After each `try_mac_test`: a pass or a failure ends the call as it does today. `declined`/`busy` goes into the retry list through `remember_mac_retry`. Every other result refuses the call (Spec 4).
   - Retry loop under `MAC_ONLY`. Do not call `check_routing_hold` or `local_release_wait.py`. Open `phase_wait_start hold "Mac busy"`, set `ROUTING_WAIT_STARTED`, and wait with a background Bash `sleep "${BUILD_TARGETS_POLL_S:-15}"` held in `HOLD_WAITER_PID`. Then use the existing free-now hook and `try_mac_test` attempt. Print `verify.sh: Mac only (<why>): waiting for the Mac (busy); natedev runs nothing for this call.` once.
   - The step's queue place is its step record in state `waiting_mac` plus this poll. The first waiter whose claim succeeds gets the Mac. This phase adds no order among waiters. A Mac-only claim is a non-copy claim, so a running copy gives way to it and it never gives way to anything. The normal copy rules apply, with no change.
   - Main flow: after `route_test`, a `MAC_ONLY` call that is still running refuses with `runner`. This covers a missing runner file and an empty retry list, so the call can never reach the local path, the cargo token or the memory queue.
4. Refusal during a call. `mac_only_refuse <word>` prints `verify.sh: test --mac refused: <text>; nothing ran on natedev.` It calls `note_event failed "$SECONDS" 0 "" 0 2` and exits 2. Texts:
   - `blocked`: "the Mac is blocked (/build:status names the holder and when it lifts)". offload's `blocked by <holder>: <for>` line shows above it, as it does today.
   - `unreachable`: "the Mac cannot be reached (/build:status --probe)".
   - `backoff`: "the Mac was unreachable a short time ago (retry backoff)".
   - `disk`: "the Mac is below its free-space floor".
   - `off`: "Mac offload is off".
   - `repo`: "this repository has no Mac setting".
   - `linux_only`: "<package> is Linux only on the Mac".
   - `state`: "the Mac claim state cannot be read".
   - `copy`: "the tree copy to the Mac failed".
   - `no_tests`: "no test matched on the Mac".
   - `lost`: "the link to the Mac dropped during the run".
   - `runner`: "the Mac runner gave no usable result".
   - Any other eligibility text is printed as it is.

   The record keeps the `BUILDLOG_MAC*` values that `try_mac_test` set (`declined`/`<word>`/seconds, `lost`, or `declined`/`runner`). `target` stays empty because nothing ran. An eligibility refusal records `mac=declined`, `mac_s=0`, and `mac_reason` = the excluded Mac row's reason when that reason is one of offload's decline words, else `state`. This keeps `mac_outcome.offload_record` able to read every refusal row.
5. Cache. A `--mac` call reuses only a pass record marked `host=mac`. This is the reverse of the `--local` rule, at the same line in `cache_lookup`. A call with `--env` or `--fetch` empties `CACHE_DIR` after it is set, so it reads no record and writes none. A forwarded variable is not part of the key, and a reused pass would fetch nothing.
6. offload.py arguments and the remote command.
   - `run` gets `--env NAME` (append) and `--fetch FOLDER`. `main` refuses them, as argparse errors, for a verb other than `nextest` and with `--copy`. It also refuses a NAME that does not match the Spec 2 pattern or is unset, and a folder that fails the Spec 2 character and part rule. These guards are for the shell text that offload builds. The policy checks stay in verify.sh.
   - `OffloadRequest` gets `environment: tuple[tuple[str, str], ...] = ()` and `fetch: FetchFolder | NoFetch`. These are named types, not `str | None`.
   - `remote_test_command` puts `NAME=<shlex-quoted value>` for each forwarded pair after `cd <mirror> &&` and before the fixed assignments. When a name repeats, the later one wins, so a fixed one always wins.
   - With a fetch, the command runs `rm -rf -- <folder> &&` before the lint call, so only this run's files come back. The cleanup is `echo mac_cleanup=kept`, so a disk cleanup below the floor cannot remove a folder under `target/` before the fetch. The next run without `--fetch` cleans up as usual.
   - Without `--env` and `--fetch`, the command is byte-equal to today's command.
7. offload.py fetch. In `claimed_run`, after `stream_mac_run` returns a `ReportedMacStatus` (pass, failure or no tests) and before the claim is released:
   - One ssh call, limited by `probe_timeout_s`, reads `du -sk` of the mirror folder: `fetch_kib=<n>`/`rc=0`, or `fetch=missing`/`rc=0`.
   - When the size is over `fetch_limit_mib`, nothing is fetched. `fetch_limit_mib` is read with `positive_number`, default 512.
   - Otherwise the call runs `rsync -rlp --safe-links -e "ssh <ssh_options>" <host>:<mirror>/<folder>/ <state>/fetched/<call id>.partial/<folder>/`. Then `os.replace` moves `<call id>.partial` to `<call id>`. `<state>` is `state_directory()` on natedev (`~/.local/state/mac-test`).
   - Overwrite rule: none. Each call gets its own folder, and an existing destination is a failure.
   - Before the fetch, entries under `fetched/` (`.partial` included) whose mtime is more than 14 days old are removed.
   - A failure or a signal removes the `.partial` folder.
   - Results: `Fetched(path, kib) | FetchMissing | FetchTooLarge(kib, limit_mib) | FetchFailed(reason) | FetchNotRun`. `FetchNotRun` is for a lost or declined run.
   - Printed lines: `mac_test: fetched <folder> from the Mac to <path> (<kib> KiB)`, or `mac_test: nothing fetched: <reason>`.
8. Wire. When `--fetch` was given, `write_result` adds `fetch=fetched|missing|too_large|failed|not_run`. No other result changes. `try_mac_test` passes `--env NAME` for each name in order and `--fetch <folder>`, and reads `fetch=`. A missing `fetch=` line on a fetch call counts as `failed`.
   - On a Mac pass whose fetch word is not `fetched`, verify prints `verify.sh: FAIL: the tests passed on the Mac, but --fetch <folder> brought nothing back (<word>).` It records `mac=passed_full` or `passed_filter`, outcome `failed`, status 2, and exits 2.
   - On a Mac failure, the call exits with the runner's status as it does today. A fetch after a failed run still runs, and its line shows.
9. Stops.
   - A link drop during the run is `lost`. offload's leftover check and claim rules do not change. The fetch does not run, and verify refuses (`lost`).
   - TERM, INT or HUP during the Mac-only wait goes to `hold_interrupted`. It ends the `sleep`, records `interrupted` and exits 128 + signal.
   - During the run or the fetch, `forward_mac_runner_signal` does not change. offload stops ssh or rsync, removes the `.partial` folder, checks leftovers and releases only when their end is confirmed.
   - Tests that a process on the Mac stops (for example `mac_exit=143`) are a Mac failure with that status.
10. Record. This phase adds no build-log field and does not edit `scripts/buildlog/`. A Mac-only pass or failure is recorded as any Mac run (`mac=passed_full|passed_filter|failed`, `target=mac`, `mac_s`, Mac steps with `caller=verify-mac`). Forwarded names, values and the fetched folder are not recorded. The why appears only in the run log.
11. Docs.
    - verify.sh header: add the usage line for `--mac`/`--env`/`--fetch` beside `--local`. Add "or a Mac-only call refused" to exit code 2. Change the comment above the parse loop.
    - `docs/as-built/mac-test-offload.md`:
      - the `verify.sh test` section: flags, refusals, wait, cache
      - the offload runner section: `--env`, `--fetch`, the `fetch=` line, `mac_cleanup=kept` on a fetch run
      - Invariants: a Mac-only call never falls back
      - Config: `fetch_limit_mib` is not set, so the limit is 512
    - `commands/build/mac.md` "Taking the Mac for a job of your own": add one line that says a test that must run on macOS uses `verify.sh test <package> --mac "<why>"`, not a claim of its own.

**Files:**
- `scripts/direct/verify.sh` — header usage and exit codes, flag parse and refusals, `CACHE_DIR` for `--env`/`--fetch`, `cache_lookup` host rule, `try_mac_test` runner arguments and `fetch=` word, `route_test_without_renewal` Mac-only branch, `mac_only_refuse`, main-flow guard after `route_test`
- `scripts/mac_test/offload.py` — `--env`, `--fetch`, `OffloadRequest.environment` and `.fetch`, `remote_test_command`, the fetch step in `claimed_run`, the fetch result types, `write_result`'s `fetch=` line, `OffloadConfig.fetch_limit_mib`
- `scripts/mac_test/offload_test_commands.py` — `COMMAND_STUB`: the size-check answer and a pull `rsync` that writes the files the fixture gives
- `scripts/mac_test/test_offload_forward.py` — new: remote command, argument refusals, each fetch result, order before release, signal during pull, prune
- `scripts/direct/verify_mac_offload_test_support.py` — the fake runner writes `fetch=<word>` from `TEST_RUNNER_FETCH`
- `scripts/direct/test_verify_mac_only.py` — new: busy wait then pass, each refusal line, eligibility refusal, cache rules, runner arguments, fetch word, TERM during the wait, missing runner
- `docs/as-built/mac-test-offload.md` — Spec 11
- `commands/build/mac.md` — Spec 11

**Workers:** 2 writers. The Spec fixes the interface: runner arguments in Spec 6 and 8, and the `fetch=` line.
- `offload` — `scripts/mac_test/offload.py`, `scripts/mac_test/offload_test_commands.py`, `scripts/mac_test/test_offload_forward.py`.
- `verify` — `scripts/direct/verify.sh` (hub), `scripts/direct/verify_mac_offload_test_support.py`, `scripts/direct/test_verify_mac_only.py`, both docs. It reuses `VerifyMacOffloadFixture` and `wait_for_marker_exit`, with no second fixture or waiter.

**State:**
- New natedev folders `~/.local/state/mac-test/fetched/<call id>/<folder>`, removed after 14 days at the next fetch.
- On the Mac, a fetch run removes `<mirror>/<folder>` before its tests, and it keeps the mirror's `target` folder for that run.
- No new build-log field, and no verify cache record for `--env` or `--fetch` calls.

**Constraints from prior phases:**
- Phase 1b (todo, not committed in the main worktree `natedev-verify`) changes these regions:
  - In `verify.sh`: `STEP_OWNER_PY` (`sys.path` argument, `identity`) and the `STEP_OWNER_PY` call in `buildlog_exec`.
  - All of `mac_test.py`, `copy_watch.py`, `test_copy_watch.py` and `test_mac_test.py`.
  - In `offload_test_support.py`: the `process_start` import, `CLAIM_STUB`'s start read and the kept-claim assertion.

  This phase edits none of these regions or files. Its `verify.sh` hunks are in the header, the parse loop, `CACHE_DIR`/`cache_lookup`, `try_mac_test`, the body of `route_test_without_renewal` and the main flow after `route_test`. It does not edit the `route_test` wrapper. New stub fixture keys are set from `test_offload_forward.py` (the stub reads `fixture.sh`), not in `offload_test_support.py`'s `setUp`.
- Phase 9: verify.sh starts no new Python, and any inline Python names itself. The Mac-only wait uses Bash `sleep` and the existing free-now hook. `test_verify_python_starts` stays at 40 rows.
- Phase 10: a group stop after routing goes through `verify_interrupted` only. During routing, `hold_interrupted` ends `HOLD_WAITER_PID`, so the Mac-only `sleep` is held there. The three group-stop tests in `test_verify_hold_signals.py` pass unchanged.
- Phase 13: `start_copy_watch` and `copy_watch.py` do not change. A Mac-only call never starts a copy watch.
- Phase 7: a refused claim keeps a natedev run waiting in place, and `claim --from-pid` adoption does not change. A Mac-only call never enters the memory wait or takes the cargo token, because the token is taken after `route_test`.
- Phase 8 (todo) names a holder for each verify wait. The Mac-only wait opens with the existing `phase_wait_start hold` key, so Phase 8 finds it there.
- `build_targets.py` and `scripts/buildlog/` belong to other units. Read them, do not edit them.

**Acceptance gate:**
- Reproduce first. Before any code change, add two tests on 17a556b7 and record that both fail, with their output, in the phase report:
  - `test_verify_mac_only`'s case: the Mac answers busy, then passes. `verify.sh test sample --mac "<why>"` must have no natedev run and end as a Mac pass.
  - `test_offload_forward`'s case: `offload.py run --env NAME` puts `NAME=<value>` in the remote command.
- `test_verify_mac_only` passes:
  - Busy then pass: the call waits, then passes on the Mac. There are 0 natedev runs, and the record has `mac=passed_full`, `target=mac` and `hold_wait_s > 0`.
  - Each Spec 2 line is exact, with exit 2, no runner, no cargo and no call record.
  - Each Spec 4 word gives its one line, exit 2, no natedev run and no retry. An eligibility `natedev_only unreachable` refuses before any runner start.
  - A Mac failure exits with the runner's status.
  - A `--mac` call does not reuse a natedev pass and does reuse a `host=mac` pass. A `--env`/`--fetch` call reads and writes no record.
  - The runner gets `--env` for each name in order, and `--fetch`.
  - A pass with `fetch=missing` exits 2 with `mac=passed_full` and outcome `failed`.
  - TERM during the wait gives exit 143, `interrupted`, and nothing left behind (`assert_nothing_outlives`).
- `test_offload_forward` passes:
  - Forwarded assignments come before the fixed ones, and the fixed ones do not change. A variable that is set but not named is not in the command. A value with spaces, quotes and a newline keeps its bytes. Without the flags, the command is byte-equal to today's.
  - `rm -rf -- <folder> &&` and `mac_cleanup=kept` appear only with `--fetch`.
  - Each argparse refusal.
  - `fetched`: files under `<state>/fetched/<call id>/<folder>`, no `.partial` left. `missing`, `too_large` (with `fetch_limit_mib` set), `failed` (`.partial` removed) and `not_run`.
  - The fetch comes before the claim release.
  - SIGTERM during the pull rsync gives exit 143 and removes `.partial`.
  - The 14-day prune.
- These pass unchanged:
  - In `scripts/direct`: `test_verify_mac_offload`, `test_verify_mac_local`, `test_verify_mac_local_arguments`, `test_verify_mac_arguments`, `test_verify_mac_routing`, `test_verify_mac_results`, `test_verify_mac_retry_hold`, `test_verify_mac_signals`, `test_verify_tree_cancel_mac`, `test_verify_hold_signals`, `test_verify_python_starts`.
  - In `scripts/mac_test`: `test_offload`, `test_offload_lifecycle`, `test_offload_cleanup*`, `test_copy_watch`, `test_mac_test`.
- basedpyright gives 0 errors, 0 warnings, 0 notes on each changed Python file: `offload.py`, `offload_test_commands.py`, `test_offload_forward.py`, `verify_mac_offload_test_support.py`, `test_verify_mac_only.py`. It gives the same on offload.py's callers: `copy_watch.py`, `mac_test.py`, `audit.py`, `offload_test_support.py`, `offload_cleanup_test_support.py`, `scripts/buildlog/mac_report.py`, `scripts/build_targets/build_targets.py`, `scripts/build_targets/targets.py`. Read the last line, because `basedpyright scripts/mac_test` exits 3 on a clean pass.
- `bash -n scripts/direct/verify.sh` passes, and `shellcheck scripts/direct/verify.sh` has no new findings.
- Live check, after `build_hold.py status` shows no natedev or Mac hold of another session: `verify.sh test <small Mac-enabled package> --mac "phase 14 live check"` passes on the Mac, and the build log shows no natedev nextest step for that call.
- Merge gate: `~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>` passes on the tree merged with origin/builds.
- When the phase is on main, tell performance: Mac-only calls are normal Mac rows, and refusals are `declined`/`<word>` with outcome `failed` and status 2. Tell hana: the flag form, that `--fetch` is relative to the repository root while nextest runs each test in its package folder, the landing path, and the 512 MiB limit.
