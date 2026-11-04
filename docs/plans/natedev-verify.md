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
  - `scripts/buildlog/record.py`, `scripts/build_targets/build_targets.py` — helpers `verify.sh` starts, owned by other units (read, do not edit without the owner)
  - `scripts/buildlog/mac_idle.py` — a helper `verify.sh` starts; this unit owns it (production doc Units row)
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
  - Helpers owned by other units (`scripts/buildlog/` except `mac_idle.py`, `scripts/build_targets/`) change only through their owner, or with the owner named in the checkpoint notice.

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

**Constraints from the phase 20, 23 and 24 review:** Phase 23 cancellation behavior stays: a group TERM of a lint run exits 143, repeated INT/TERM/HUP during cleanup are ignored, and no verify process, admission place or step record remains. The acceptance gate keeps the `test_verify_tree_cancel_lint.py` checks green.

**Goal:** a refused step makes `verify.sh` exit non-zero on every host, and every process-start reader in this unit's code reads a start on the Mac as it does on Linux, through one shared reader.

**Defect:** on the Mac, `verify.sh`'s step recorder (`step_record.py process_start`) reads `/proc/<pid>/stat`, which Darwin does not have. The step is refused, but the wrapper exits 0, so the failure reads as a pass.

**Spec:**

(1) Write the test that shows the exit 0 first: `verify.sh` on a faked Darwin host (no `/proc`) whose step record refuses. Both trees already end a failed `write` with `|| exit 2` (Phase 7's `env -u BUILDLOG_CALL_ID "$PY" "$STEP_RECORD_SCRIPT" "${record_args[@]}" || exit 2`; Phase 1's `VERIFY_MONOTONIC_OFFSET_US="$(… write … --clock-offset)" || exit 2`). Use that test to find the path that turns the refusal into 0, and change only that path, so a recorder refusal exits non-zero. If the test shows a non-zero exit already, make no exit-status change: the Darwin read in (2) closes the defect. Two Phase 7 statuses are not recorder refusals and keep their behavior: board renewal's inconclusive status 2 and the ownership guard's internal 125 retry (Constraints).

(2) One shared reader, `scripts/lib/process_start.py`, for Linux (`/proc/<pid>/stat` field 22, under `$VERIFY_PROC_ROOT`, default `/proc`, as `step_record.py` reads it now) and Darwin (`ps -p <pid> -o lstart=` under `LC_ALL=C TZ=UTC`, from `message/sessions.py process_start`'s tested form). It returns named results, not a bare `str | None`:
- `Started(start)` — a live process and its start.
- `Zombie(start)` — a zombie process; its start is still read.
- `Gone` — no such process.
- `Unreadable(reason)` — the start cannot be read, but the process may exist (a read error other than not-found, a field that does not parse, a `ps` failure other than no such process).

Results of different kinds never compare equal: a `Started` never equals a `Zombie` with the same start. Tests check the exact type of each result, not only its value.

`start` has one stored form per host, the same text that every writer stores and every reader compares: the field-22 digits on Linux, the `LC_ALL=C TZ=UTC` `lstart` text on Darwin. A match is a start equal to the stored one; a different start is PID reuse. Every reader in this unit's code uses the shared reader, and its policy is now explicit:
- `scripts/direct/step_record.py` `process_start` — `write` stores a `Started` start. `_read_fields` reads `Gone`, `Zombie` and a different start as `stale`, and `Unreadable` as `unreadable`.
- `scripts/direct/verify.sh` `STEP_OWNER_PY` `identity(pid)` — writes the `start` of the `owned` record.
- `scripts/direct/board.sh` `owned_group_matches` — a `Started` match keeps an expired hold. `Unreadable` also keeps it, so a live owner keeps its token. `Gone` and a different start free it.
- `scripts/direct/board.sh` `cleanup_owned_group` `identity(pid)` — a `Zombie` leader with live children in its group still matches. `Gone`, `Unreadable` and a different start take the existing mismatch branch (refuse while the group has live members, else done).
- `scripts/mac_test/mac_test.py` `process_start_time` — removed. `run_is_live`, `run_is_kept`, `copy_gives_way`, `stop_copy`, the claim writes and `adopt_wait_claim` read the shared result and keep their outcomes: only a match is live, `stop_copy` sends TERM only to a match, and an adoption refuses (exit 12) unless the from-pid start matches the stored `proc_start` and the runner's start reads as `Started`.
- `scripts/mac_test/copy_watch.py` — `StepProgress` reads its group leader's start once, and `leader_alive` is true only for a `Started` start that still matches with the same group. `newest_candidate` removes a candidate whose start does not match. `Watch` writes its own `Started` start to a new candidate record, else an empty start.

The writer (`STEP_OWNER_PY`), both board.sh readers, mac_test.py and copy_watch.py change in one commit and keep one stored start form. A reader with another form reclaims a live hold on the Mac (Phase 7 Spec 5).

`scripts/lib/` and `scripts/direct/board.sh` belong to natedev. builds approved `scripts/lib/` as outside this unit's Owns (03:5x), and the showrunner ruled board.sh into this phase (2026-10-30). The checkpoint notice names both in also-touches lines (owner natedev), tested against `origin/natedev` after a trial merge (production contract item 9). The other copies (`message/sessions.py`, `build_hold/build_hold.py`, `lint/memory_admit.py`, `lint/tmp_sweep.py`, `production/retire_unit.py`, `herdr/herdr.py` `darwin_process_identity`, `prioritize/runner_lock.py`, `shutdown/inventory.py`) go to their owners through builds after 1b merges: build_hold, memory_admit and tmp_sweep to resources; sessions.py and retire_unit.py to natedev; herdr.py to herdr-unit; runner_lock.py and inventory.py to the owners builds names. `step_record.py stopped` (`consume_stopped_build`) also reads `/proc/sys/kernel/random/boot_id`, which Darwin does not have; the Darwin test covers it too.

(3) Test-local Bash function extractors use the shared `shell_functions(path, *names)` in `scripts/direct/shell_function_test_support.py` (each named top-level definition, in the order given; `ValueError` for a missing name). It extracts every function named here byte-equal to the old extractors, so it needs no change; write no second extractor. In `scripts/direct/test_verify_step_owner.py`, delete `shell_function` and call `shell_functions(VERIFY, "token_wait_note")`, `shell_functions(INVOKE, "buildlog_exec")` and `shell_functions(INVOKE, "sweep_after_step")`. In `scripts/direct/test_verify_assertion_guard.py`, `VerifyAssertionGuardTests.lint` replaces the `read_metadata() {` … `require_taplo() {` text slice with `shell_functions(VERIFY, "read_metadata", "load_metadata", "require_member")` plus a `METADATA_READY=0` line in its script (the slice carried that line). It replaces the `assertion_rulings_args() {` slice with `shell_functions(VERIFY, "assertion_rulings_args")` plus an `assertion_rulings_args` call line after it: the old slice carried that call (Phase 1 `verify.sh:841`), and without it the `with_session` cases lose their rulings. No assertion changes; the session-ruling assertions stay as they are.

**Files:**
- `scripts/lib/process_start.py` — new: the shared reader and its four named results
- `scripts/lib/test_process_start.py` — new: Linux and faked-Darwin reads, each named result, agreement with the record's stale check
- `scripts/direct/step_record.py` — `process_start` on the shared reader
- `scripts/direct/verify.sh` — `STEP_OWNER_PY` `identity(pid)` on the shared reader; the Spec (1) exit path if the test finds one
- `scripts/direct/board.sh` — `owned_group_matches`, `cleanup_owned_group` `identity(pid)` (owner natedev; also-touches line)
- `scripts/mac_test/mac_test.py` — `process_start_time` replaced by the shared reader
- `scripts/mac_test/test_mac_test.py` — patches of `mac_test.process_start_time` move to the shared reader; Darwin claim adoption
- `scripts/mac_test/copy_watch.py` — the Spec (2) readers on the shared reader
- `scripts/mac_test/test_copy_watch.py`, `scripts/mac_test/offload_test_support.py` — candidate and claim fixtures read their start through the shared reader; a gone run reads as `Gone`
- `scripts/build_targets/test_targets.py`, `scripts/build_targets/test_build_targets.py` — patches of `mac_test.process_start_time` move to the shared reader (owner resources-unit; trial-merge against `origin/builds-resources`; the notice carries `also touches scripts/build_targets/test_targets.py, scripts/build_targets/test_build_targets.py (owner resources-unit), tested against <origin/builds-resources tip>`)
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
- Phases 9 and 10 (branch `natedev-verify-cancel`; a merge of origin/builds brings them once they land). The token renewer's delay is Bash `sleep`. The `sleep` stub in `VerifyTokenWaitFixture` (`verify_token_wait_test_support.py`) ends only the zero-memory poll at once and passes every other delay to the real `sleep`, so a board case in `test_verify_token_ownership.py` that writes an expired lease stops the renewer first, or its heartbeat writes a new expiry. `user_clock` in `verify.sh` runs `python3 -S` outside the `VERIFY_PY_TRACE` runner; `test_verify_python_starts` expects 40 traced rows.
- Phase 13 (checkpoint be7ffc50, shrink 17a556b7, on `natedev-verify-cancel`). `copy_watch.py` `Wakeup(read_fd, parent_pid)`, its required `--parent-pid` argument and `verify.sh` `start_copy_watch` stay as Phase 13 left them; 1b changes only the start readers in `copy_watch.py` and `test_copy_watch.py`.
- Phase 14 (checkpoint 404a4ea0, merge 10424e70 on `natedev-verify-cancel`). Its `verify.sh` hunks are the header, the flag parse loop, `CACHE_DIR`/`cache_lookup`, `try_mac_test`, the body of `route_test_without_renewal`, `mac_only_refuse` and the main flow after `route_test`; it also edits `offload.py`, `offload_test_commands.py` and `verify_mac_offload_test_support.py`, and in `offload_test_support.py` only `OffloadCommandFixture`'s run helper (`runner_options`, `call_id`). None of these overlap 1b's regions; take both sides at the merge.

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

**Constraints from the phase 20, 23 and 24 review:** Phase 20 replaced the `memory_admit.py check` loop: `scripts/direct/memory_gate.sh` (near line 155) now uses one admission client with wait events. Agree the holder event contract with queue-unit before dispatch, then use one writer and one tester. Phase 23 cancellation behavior stays: a group TERM of a lint run exits 143, repeated INT/TERM/HUP during cleanup are ignored, and no verify process, admission place or step record remains. The acceptance gate keeps the `test_verify_tree_cancel_lint.py` checks green.

**Source:** sdlc hand-off, approved by builds 2026-10-29.

**Goal:** every wait verify.sh records names who holds what it waits on.

**Starting point:** checkpoint 4fa7a873 on `natedev-verify-holder` (reviewed before Phase 7) holds this work on the older base: `buildlog_memory_poll_hook` in `memory_gate.sh`, the holder lookups in `verify.sh` and 23 tests in `test_verify_wait_holder.py`. It has not landed. Port it to this Spec on the tree of Phase 1b plus `natedev-verify-cancel`: keep what fits, change the hook to the form in Spec 5, and drop its background `hold-watch` poller and inline Python holder lookup (Spec 3).

**Spec:**

1. `phase_wait_start` passes the holder to `progress_history.py start-wait`: `--holder-resource-kind slot|memory --holder-resource <slot name | build target | memory pool> --holder-session <session id>`, or `--holder-unit <unit>` when only the unit is known. All flags or none; none records `HolderNotNamed`.
2. The holder is: for a slot, the board token owner; for memory, the blocker that the gate's `check` poll reports (spec 5); for a build hold, the hold's holder, with kind `slot` and the held target as the resource; for the Mac-only busy wait (Phase 14, `phase_wait_start hold "Mac busy"` in `route_test_without_renewal`), the session that holds the Mac claim, with kind `slot` and resource `mac`, named again on each poll of that wait.
3. When the holder changes during the same wait, call `progress_history.py wait-holder --session-dir <dir> --wait-id <id>` with the same flags. A holder that goes away calls `wait-holder` with no flags (`HolderNotNamed`). For a slot and a build hold, verify.sh names the holder at `start-wait` and again on each pass of its hold loop, in the loop's own shell. No watcher process runs beside the wait; a change while one blocking wait call runs shows at the next pass.
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
- Phase 9 (on `natedev-verify-cancel`). `verify.sh` starts no new Python: the holder lookups add no Python start, and `test_verify_python_starts` keeps 40 traced rows.
- Phase 14 (checkpoint 404a4ea0, merge 10424e70 on `natedev-verify-cancel`). A `--mac "<why>"` call waits in `route_test_without_renewal` under `phase_wait_start hold "Mac busy"` with a Bash `sleep` held in `HOLD_WAITER_PID`; each poll's free-now answer decides, so a Mac that turns blocked, off or unreadable during the wait refuses the call. It never enters the memory wait or takes the cargo token. `hold_interrupted` ends the poll `sleep` for TERM, INT and HUP.
- Phase 10 (on `natedev-verify-cancel`). A TERM, INT or HUP to the run's process group goes through `verify_interrupted` only: it stops the step group, frees the memory-queue place and exits 143, 130 or 129. The holder lookups add no second stop path. Both run-log tees run `run_log_tee` (ignores INT, TERM and HUP, `tee -i`, reads to EOF). The three group-stop tests in `test_verify_hold_signals.py` pass unchanged. `VerifyHoldWaitFixture` has `start_memory_queued_lint()` (a real `verify.sh lint` parked in the memory queue: the real checker, `MemAvailable: 0`, `BUILDLOG_MEM_POLL_S=3600`), `memory_queue_pids()`, `owned_process_snapshot()` and `wait_for_owned_exit(timeout)`; `wait_for_marker_exit(marker, timeout)` in `verify_mac_offload_test_support.py` is the one process-exit waiter. Reuse them for a parked memory wait; write no second starter or waiter.

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

### Phase 11 — Judge a merge commit against both of its parents, also when a base is given  · status: done

#### As-built

Checkpoint 49a471ca on `natedev-verify-base`.

- `assertion_guard.py` `commit_bases(repo, selected_bases=("HEAD",), *, head="", staged=True) -> tuple[str, ...]` is the one bases function. It keeps the selected bases in order (each through `default_base`; an empty selection uses the branch default), adds each non-first parent of every merge in `<base>..<head or HEAD>`, adds the `MERGE_HEAD` entries when `staged`, and keeps only the parents that `git merge-base --independent` lists, so repeated merges of one trunk add one base. With no arguments it is the commit hook's HEAD/index comparison.
- `run` calls only `commit_bases(repo, tuple(args.bases), head=args.head, staged=args.staged)`. A finding blocks only when it shows against every base, so a merged-in trunk's removals never block and our own removal after a merge still does, with `--base`, `VERIFY_ASSERTION_BASE` or the default. `verify.sh` passes `VERIFY_ASSERTION_BASE` unchanged.
- Real case: `assertion_guard.py --repo ~/rust/hana --head f08af7de59 --base f8b6c546ba` prints no blocking line (was 120); against fork 25d64d92 it reports 58, lfo's own removals (was 403).

**Files:**
- `scripts/direct/assertion_guard.py` — `commit_bases`, `run`
- `scripts/direct/test_assertion_guard.py` — `MergeBaseSelectionTests`: explicit fork base ignores trunk removals, still blocks our removal after a merge, two trunk merges then a commit, `--head` takes that revision's parents
- `scripts/direct/test_assertion_guard_revisions.py` — the two `MergeAssertionTests` explicit-base cases expect exit 0 and no `test_incoming removed:` line
- `docs/as-built/phase-checks.md` — guard row: explicit or default bases, merged-in parents, pending merge heads, findings against every base

**Binds later work:** later guard changes reuse `commit_bases` unchanged and add no second bases path.

**Gotchas:** f08af7de59's first parent is the earlier merge b997a8038a, not f8b6c546ba; a base at f8b6c546ba still crosses one trunk merge.

**Ruled out:** a new setting, flag or skip for merges; parent discovery wider than the non-first parents of merges in the judged range.

### Phase 12 — Pair an assertion whose local binding was renamed  · status: done

#### As-built

Checkpoint 48859c93 on `natedev-verify-base`.

- `assertion_guard.py` pairs a Rust assertion whose only change is a renamed local `let` binding, also across a rustfmt rewrap. `touched_assertions` calls `binding_renames(sources, code, constants)` once. For each test that `enclosing_tests` finds once on each side, `prove_binding_rename(old_rows, new_rows, scope) -> BindingRename | NoBindingRename` compares the whole test token by token (`rust_tokens`, on the shared `lexical_spans` lexer). The proof holds only when every token difference is one bijection of plain `let` names, each bound once on its side and absent from the other side, so binding values must also be token-equal. `BindingRename.rewrite` maps the new names back to the old ones in each new-side assertion of that test before matching.
- The proof reuses the Phase 6b census. `ConstantTable` adds `local_declarations` (plain `let [mut] name [: T] =`), `function_bodies` and `local_renames_refused`; `refuse_environment()` sets `refuse_all` and `local_renames_refused`. An untrusted macro inside another function does not refuse a local rename (`pairing_code_is_certain(code, function_bodies)`); constant pairing keeps its file-wide refusal.
- These forms refuse the proof, and normal matching runs: shadowing; patterns; function and closure parameters; a generic signature, a nested function or any attribute inside the test; a literal on either side that holds `{name` for a mapped name; a mapped name inside a macro other than the assert family; member, path, field, label, type and `as` positions; a use before the binding's statement ends or inside a nested block; split or merged maps; the Phase 6b file refusals (`macro_rules`, a trusted-macro import, an unresolved glob, uncertain code); every Python file.
- Real case: the staged `8dd754b1` diff from `~/rust/cargo-liner-style-names` at its parent, with no `Assertion-Change:` lines, prints `assertion guard: no removed or weaker assertions`.

**Files:**
- `scripts/direct/assertion_guard.py` — `RustToken`, `rust_tokens`, `BindingRename`, `NoBindingRename`, `prove_binding_rename`, `rename_references_are_plain`, `binding_renames`; `ConstantTable.local_declarations`, `function_bodies`, `local_renames_refused`, `refuse_environment`
- `scripts/direct/test_assertion_guard_revisions.py` — `BindingRenameTests`: the two real shapes, a rename plus another change, each refusal form, Python renames stay `removed`
- `docs/as-built/phase-checks.md` — guard row: plain local `let` renames pair, and the forms that refuse

**Binds later work:** a rename proof change goes in `prove_binding_rename` and reuses `rust_tokens` and the census; no second tokenizer or matcher.

**Gotchas:** a refused proof does not block by itself: normal matching runs, so an in-place edit of one assertion stays a non-blocking `changed` note.

**Ruled out:** pairing a renamed function parameter (a test attribute such as `#[rstest]` picks a fixture by parameter name); Python renames; a block for every refused proof.

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

#### As-built

Checkpoint 404a4ea0 on `natedev-verify-cancel`.

- `verify.sh test <package> --mac "<why>"` runs the step on the Mac only. It never runs on natedev, never takes the cargo token, never enters the memory wait and never starts a copy watch. The why has the `--local` rule (required, not blank, no leading `--`) and shows only in the first routing line.
- `--env NAME` (repeatable, in order) and one `--fetch <folder>` need `--mac`. Each flag can be anywhere after the verb; the parse loop removes it, so it never enters `tree_key`, the cargo words or the recorded command. Each refusal exits 2 with one stderr line before a call record, runner or cargo starts: no why; `--mac` on a verb other than `test`; `--mac` with `--local`; `--env`/`--fetch` without `--mac`; a name that does not match `^[A-Za-z_][A-Za-z0-9_]*$`; a refused name (`PATH`, `HOME`, `MACOSX_DEPLOYMENT_TARGET`, `RUSTFLAGS`, `RUSTDOCFLAGS`, prefixes `BUILDLOG_`, `LINT_`, `CARGO_`, `RUSTC`, `NEXTEST_`, `DYLD_`, `MAC_TEST_`, `VERIFY_`); an unset name (an empty value is allowed); a second `--fetch`; a folder that is not plain relative (letters, digits, `.`, `_`, `-`, `/`; no leading `/` or `-`; no empty, `.` or `..` part; one trailing `/` removed); a folder that `git check-ignore -q -- <folder>/` does not ignore.
- Forwarded values: at start, `verify.sh` saves the exported environment in `VERIFY_CALL_ENVIRONMENT` (`VERIFY_` names left out), so a caller name such as `PKG` keeps the caller's value. `try_mac_test` passes `--env NAME` per name and `--fetch <folder>` to the runner. With forwarded names it starts the runner through `VERIFY_MAC_ENV_PY`: that inline Python reads NUL-delimited `NAME=value` entries on fd 3, sets them in `os.environb` and runs the runner with `runpy`. A value never goes on argv, and readonly or array names in Bash work. Without names the runner starts directly.
- Routing (`route_test_without_renewal`): `initialize_step_record` refuses a `natedev_only` answer before the cache and the runner, with `mac=declined`, `mac_s=0` and `mac_reason` = the excluded Mac row's decline word, else `state`. The target loop skips natedev rows. `check_mac_only_result` keeps only `declined`/`busy` for a retry; every other answer goes to `mac_only_refuse <word>`, which prints `verify.sh: test --mac refused: <text>; nothing ran on natedev.`, sets an empty target, records `failed` with status 2 and exits 2. The busy wait opens `phase_wait_start hold "Mac busy"`, prints one waiting line and polls with a background Bash `sleep "${BUILD_TARGETS_POLL_S:-15}"` in `HOLD_WAITER_PID`, without `check_routing_hold` or `local_release_wait.py`. Each poll calls the runner also when the free-now hook fails, so a Mac that turns blocked, off or unreadable refuses the call. The first waiter whose claim succeeds gets the Mac; waiters have no order. After `route_test`, a Mac-only call that is still running refuses with `runner` (missing runner, empty retry list).
- Cache: a `--mac` call reuses only a `host=mac` pass. A call with `--env` or `--fetch` empties `CACHE_DIR`, so it reads and writes no record.
- `offload.py run` takes `--env NAME` (append) and `--fetch FOLDER`. `main` refuses them as argparse errors for a verb other than nextest, with `--copy`, for a bad or unset name and for a folder that is not plain relative. `OffloadRequest` has `environment: tuple[tuple[str, str], ...] = ()` and `fetch: FetchFolder | NoFetch = NO_FETCH`. `remote_test_command` puts shlex-quoted `NAME=value` pairs after `cd <mirror> &&` and before the fixed assignments, which win. A fetch adds `rm -rf -- <folder> &&` before the test and makes the cleanup `echo mac_cleanup=kept`. Without the flags the command is byte-equal to the earlier one.
- `fetch_folder` runs in `claimed_run` after a reported result (pass, failure or no tests) and before the claim release. It removes `fetched/` entries older than 14 days, reads `du -sk` in one ssh call bounded by `probe_timeout_s`, refuses a size over `OffloadConfig.fetch_limit_mib` (default 512), then runs `rsync -rlp --safe-links` into `~/.local/state/mac-test/fetched/<call id>.partial/<folder>` and `os.replace`s it to `<call id>`. An existing destination fails; a failure or a signal removes `.partial`. Results are `Fetched(path, kib) | FetchMissing | FetchTooLarge(kib, limit_mib) | FetchFailed(reason) | FetchNotRun`, held in `fetch` on `PassedOnMac`, `FailedOnMac` and `NoTestMatchedOnMac`. `write_result` adds `fetch=fetched|missing|too_large|failed|not_run` on a fetch call and prints `mac_test: fetched <folder> from the Mac to <path> (<kib> KiB)` or `mac_test: nothing fetched: <reason>`.
- `verify.sh` reads `fetch=`; a missing line counts as `failed`. A Mac pass with any other word than `fetched` prints `verify.sh: FAIL: the tests passed on the Mac, but --fetch <folder> brought nothing back (<word>).`, keeps `mac=passed_full|passed_filter`, records `failed` with status 2 and exits 2. A Mac failure keeps the runner's status.
- No build-log field: forwarded names, values and the fetched folder are not recorded.

**Files:**
- `scripts/direct/verify.sh` — header usage and exit code 2, `VERIFY_CALL_ENVIRONMENT`, flag parse and refusals, `CACHE_DIR` and the `cache_lookup` host rule, `try_mac_test` runner arguments, `VERIFY_MAC_ENV_PY` and the `fetch=` word, the `initialize_step_record` refusal, `mac_only_refuse`, `check_mac_only_result`, the Mac-only branch of `route_test_without_renewal`, `hold_interrupted`, the main-flow refusal after `route_test`
- `scripts/mac_test/offload.py` — fetch types, `OffloadRequest.environment` and `.fetch`, `OffloadConfig.fetch_limit_mib`, `remote_test_command`, `fetch_folder`, the `write_result` `fetch=` line, `--env`/`--fetch`
- `scripts/mac_test/offload_test_commands.py` — `COMMAND_STUB` size answer and a pull `rsync` that writes the fixture's files
- `scripts/mac_test/offload_test_support.py` — `OffloadCommandFixture` run helpers take `runner_options` and `call_id`
- `scripts/mac_test/test_offload_forward.py` — remote command, argument refusals, each fetch result, fetch before release, signal during the pull, 14-day prune
- `scripts/direct/verify_mac_offload_test_support.py` — git stub `check-ignore` (ignores `target/*`); the fake runner writes `forwarded-environment.json` and `fetch=` from `TEST_RUNNER_FETCH`
- `scripts/direct/test_verify_mac_only.py` — busy wait then pass, refusal lines, eligibility refusal before the cache, cache rules, forwarded values, fetch word, TERM and INT during the wait, missing runner
- `docs/as-built/mac-test-offload.md` — `verify.sh test` flags, runner `--env`/`--fetch`, 512 MiB limit, the never-fall-back invariant
- `commands/build/mac.md` — a test that must run on macOS uses `--mac "<why>"`, not a separate claim

**Binds later work:** the Mac-only busy wait is `phase_wait_start hold "Mac busy"` in `route_test_without_renewal`, with the Bash `sleep` in `HOLD_WAITER_PID`; "Name the holder of what a wait is on" names the Mac claim's session as its holder (kind `slot`, resource `mac`). A Mac-only refusal goes through `mac_only_refuse` only.

**Gotchas:**
- A background Bash `sleep` inherits an ignored SIGINT. `hold_interrupted` ends a Mac-only `HOLD_WAITER_PID` with TERM for every caller signal and records the caller's signal (exit 128 + signal).
- A forwarded value passes in the ssh remote command to the Mac; it never goes on `verify.sh` argv, in the cache key or in a record.
- `--fetch` is relative to the repository root (the mirror root); nextest runs each test in its package folder, so a fixture must write to that root-relative folder.

**Ruled out:** forwarded names, values or the fetched folder in the build log; an order among Mac-only waiters; a natedev fallback for any Mac answer.

### Phase 16 — Raise a red alert when the Mac takes no work while steps wait  · status: todo

**Origin:** builds showrunner, 2026-10-31 07:47 EDT, with the user's words: "you should have some kind of red alert that occurs when you have nothing running on the mac as is the case right now - it is a major failure when the mac is available and not taking work". Built on branch `natedev-verify-cancel`, worktree `/home/natepiano/worktrees/claude-build-followups-verify-cancel` (HEAD e6f8540f, Phase 14 shrink).

**Depends on:** Phase 14 (same branch). Nothing else. This unit owns `scripts/buildlog/mac_idle.py` (production doc Units row). The two views belong to queue-unit (`/build:queue`, `/build:status`); this phase adds only the small read of the saved result there and names those paths in the checkpoint's also-touches line.

#### Work Order

**Constraints from the phase 20, 23 and 24 review:** The Phase 16 alert code (reader, watcher, views, notification recovery and tests) shipped on the Phase 20 branch (natedev-verify-cancel 41a3ce6d4). Phase 24 (verify-idle-disk 4b0e023bf) adds the disk exclusion and the admission-query-error cause, and supersedes this Work Order's exclusion rule and constants-based timing. This phase is now closure with one writer: check the shipped code against the acceptance gate, and add the two view checks (`/build:queue`, `/build:status` read the saved alert) and the admission-error recovery check.

**Source:** builds showrunner ruling, 2026-10-31 07:47 EDT (binding). Cause today: `mac_idle.watch_once` (`scripts/buildlog/mac_idle.py:257-337`) returns at lines 277-280 when a claim, `run.json` or `block.json` exists, and at 287-289 for a future `unreachable_until`. It reads only steps in `waiting_hold`/`waiting_mac`, never reaches the user, and `notify_director` (lines 220-246) sends only to the `builds-unit` director of production `build-followups` (line 235), which no longer directs. At 07:47 EDT research held a block (`~/.local/state/mac-test/block.json`, holder `research`, for `P15 F11 L0 run`), no natedev job ran on the Mac, and live `mac_allowed` test steps waited in `waiting_hold` on natedev (7 at 07:49). The watch saw nothing. From 07:50 to about 07:57 the second idle stretch was a live research claim with only a parked hana app on the Mac: no compile or test ran while 7 test runs waited. The claim watcher counts the app as Mac activity, so it wrote no `idle_since`.

**Goal:** when no job from natedev runs on the Mac for 3 minutes and a step the Mac could run waits, `/build:queue` and `/build:status` show a red line at the top, the builds showrunner gets one message, and at 10 minutes the user gets one phone push per outage.

**Spec:**

1. **One check.** All of this is in `mac_idle.watch_once`. Add no second checker, timer or service. The `buildlog sample` pass on natedev already calls it every 60 s (`scripts/buildlog/cli.py:289-293`, `buildlog-sample.timer`); keep that caller and the non-blocking `watch.lock` (lines 260-264).
2. **Mac work reading.** Replace the early returns at lines 268-289 with one function `mac_work(paths: MacIdlePaths, now: float) -> NatedevJobOnMac | MacIdle | MacUnreachable | MacStateUnreadable`. It reads local files only. It never reads the Mac's load, never runs ssh and never matches a process or command name.
   - `NatedevJobOnMac`: a legacy `run.json` exists, or a claim file in `paths.mac_state / "claims"` is live and has no `work_idle_since` (Spec 12). A running app does not count as a job. Live is the claim rule of `mac_test.run_is_live`: a process-bound claim while `/proc/<pid>/stat` field 22 equals `proc_start`; a remote claim (`remote_host`) always. All claim files live on natedev, so every claim is a job sent from natedev.
   - `MacIdle(cause, holder, detail)`, in this order: `blocked` when `block.json` exists (holder = the block's `holder`, detail = its `for`, also the block's `session`); `claim_idle` when every live claim has `work_idle_since` (the claim watcher writes it when the Mac has no build or test process, also while an app runs; holder = the claim's `holder_session` shown name, detail = its `what`); `free` when there is no block and no live claim.
   - `MacUnreachable(until)`: `unreachable_until` holds a finite time later than `now`. It wins over `MacIdle`.
   - `MacStateUnreadable(reason)`: the claims folder or a claim or block file cannot be read. Write it to the saved result; send nothing.
3. **Idle interval.** `idle.json` keeps `since`. A `NatedevJobOnMac` reading removes it. The first idle reading writes `since = now`. A newer claims-folder mtime moves `since` forward, as lines 294-296 do now. The interval key of an outage is `int(since)`.
4. **Waiting steps.** Count the steps the Mac could run with the recorded decision: `waiting_calls(paths, live)` (lines 179-207), which keeps only live step records with `runs_on` `MacAllowed` and state `waiting_hold` or `waiting_mac`. Steps in natedev's memory queue are `waiting_hold` (`buildlog_wait_for_memory`, `scripts/direct/verify.sh:2998-3036`), so they count. Leave out a call whose registered record has a `local_reason` (set by `mac_idle.py exclude` for `no_tests`, `off`, `repo`, `linux_only`, `local_flag`). A step that the Mac declined for a block is not excluded and counts. A `natedev_only` record never counts.
5. **Saved result.** Each pass writes `<state>/alert.json` through `write_record` (atomic replace), also when nothing is wrong: `{"version": 1, "written": <now>, "state": "busy"|"idle"|"unreachable"|"unreadable", "since": <float or 0>, "waiting": <int>, "cause": "blocked"|"claim_idle"|"free"|"", "holder": <text>, "detail": <text>, "until": <float or 0>, "messaged": <bool>, "pushed": <bool>}`. `messaged` and `pushed` belong to the outage `since`; a new `since` sets both to false. The holder session's shown name is read once per outage with `scripts.message.unit_labels.unit_labels_for` and stored in `holder`; the views never resolve a name.
6. **New module `scripts/buildlog/mac_idle_alert.py`** (stdlib, `scripts.buildlog.store` and `scripts.lib.user_zone` only; no subprocess). It is the one place for the path, the file form and the line text:
   - `state_dir() -> Path`: `MAC_IDLE_STATE_DIR`, else `store.root().parent / "mac-idle"`. `mac_idle.watch_paths` (lines 42-50) uses it.
   - `ALERT_SECONDS = 180`, `PUSH_SECONDS = 600`, `STALE_SECONDS = 300`; `mac_idle.IDLE_SECONDS` (line 28) becomes `ALERT_SECONDS`.
   - `class SavedAlert(TypedDict)` with the fields of item 5; `read_alert(path: Path) -> SavedAlert | NoSavedAlert | UnreadableAlert` (validates every field at the JSON boundary).
   - `alert_line(path: Path, now: float, zone: ZoneInfo) -> str | None`. `None` when the file is missing, `state` is `busy`, `waiting` is 0, or the Mac is idle for less than `ALERT_SECONDS`. Otherwise one line:
     - idle: `MAC IDLE <minutes> min: <waiting> steps wait for the Mac. <cause text>.` with cause text `Blocked by <holder> since <HH:MM> (<detail>); /build:mac unblock clears it`, `Claim of <holder> (<detail>) runs no build or test on the Mac`, or `No block and no claim; the waiting steps do not take the Mac`.
     - unreachable: `MAC UNREACHABLE until <HH:MM>: <waiting> steps wait for the Mac.` (a separate line, also before 3 minutes).
     - unreadable: `MAC IDLE WATCH: Mac state unreadable: <reason>.`
     - stale (`written` older than `STALE_SECONDS`): `MAC IDLE WATCH: no reading since <HH:MM>; check buildlog-sample.timer.`
     Times are in the user's zone. Fields pass through `display_field`-style control-character removal.
7. **The views (queue-unit paths).**
   - `scripts/build_queue/build_queue.py` `main` (lines 62-85): after `lines` is built (both `render_view` and `render_selection`), when `alert_line(state_dir() / "alert.json", time.time(), user_zone())` returns text, insert `(Span(text, "error", bold=True),)` as line 0. Text and `--json` output both carry it; the pane shows tone `error` in red. The queue-busy branch (lines 66-71) also prints the line first.
   - `scripts/build_targets/build_targets.py` `command_status` (lines 141-167): print the line before the target lines. Use ANSI bold red only when `sys.stdout.isatty()`; piped output stays plain. Import as `scripts.buildlog.mac_idle_alert` with the `TYPE_CHECKING` pattern of lines 54-57.
   - The read is one small file read: no subprocess, no lock, no ssh.
8. **Stage 1, 3 minutes: message the builds showrunner.** When `state` is `idle`, `waiting > 0` and `now - since >= ALERT_SECONDS` and not `messaged`: resolve the address with `[sys.executable, SCRIPTS / "production/showrunners.py", "address", "builds"]` (the same answer as `~/.claude/scripts/lib/py ~/.claude/scripts/production/showrunners.py address builds`, which prints `uds:<socket>`), then `send.py --to <address> --from "Mac idle watch" --key mac-idle-showrunner-<since> --text <alert line, then one line per waiting step: repo, kind, package, pid, wait from waiting_evidence>`. Exit 0 or 1 (queued) sets `messaged`. An empty address or a failed send leaves it false; the next pass tries again and prints one stderr line. Replace `notify_director` with `notify_showrunner(text: str) -> bool` and use it for the per-call repair messages too: action word `message_builds_unit` becomes `message_showrunner` (and `message_pending` stays). No unit, production or roster name is written in code.
9. **Stage 2, 10 minutes: one phone push.** When still idle with `waiting > 0`, `now - since >= PUSH_SECONDS` and not `pushed`: send a registered push through `push_texts.render` and `push_texts.user_send_argv`, `--from "Mac idle watch"`, `--key mac-idle-<since>`.
   - Cause `blocked`: text id `mac_idle.blocked`, need `blocked` (an emergency push: a block waits on a person to finish at the Mac and run `/build:mac unblock`), with `--clears-when "<SCRIPTS>/lib/py <SCRIPTS>/buildlog/mac_idle.py cleared --since <since>"`. Add `clears_when: str = ""` to `push_texts.user_send_argv` (`scripts/notify/push_texts.py:151-169`); it appends `--clears-when <text>`.
   - Causes `claim_idle` and `free`: text id `mac_idle.stuck`, need `decision`, with `--repeat-minutes 1440`.
   - Values: `minutes`, `waiting`, `holder`, `detail`, `since` (HH:MM, user zone) and `clears` (what clears it: `/build:mac unblock by <holder>`, `release of the idle claim <detail>`, or `the builds showrunner checks why the waiting steps do not take the Mac`).
   - Exit 0 (SENT, SKIPPED, KEPT, REPLACED) sets `pushed`. HELD (4), FAILED (3) and REFUSED (2) leave it false and print one stderr line; the next pass tries again with the same key, so the user gets at most one push per outage.
   - New `mac_idle.py cleared --since <float>`: exits 0 when `alert.json` has a `state` other than `idle`, a different `since`, or `waiting` 0; exits 1 otherwise and when the file is missing or stale. It reads only the saved result.
10. **Push texts.** Add `PushText("mac_idle.blocked", ...)` (need `blocked`) and `PushText("mac_idle.stuck", ...)` (need `decision`) to `scripts/notify/push_catalog.py`, each with an action line and two examples from today's case (block by research with 7 steps; idle claim with 3 steps). Title under 45 characters, body under 500, no `@` (`test_push_texts.py`). Approve them with `review.py texts --approve` (`docs/as-built/message-clarity.md`, `push_texts_reviewed.json`). After 3 failed review rounds, send the drafts to the builds showrunner and record its answer with `--user-spec`.
11. **Repairs keep their condition.** The per-call repair loop (lines 299-337: temp root, retry marker, message) runs only for `MacIdle` with cause `free`, as today with no block and no claim. A block or an idle claim gets the alert, not repairs.
12. **Build or test work on a claim.** `mac_test.mac_activity_script()` (`scripts/mac_test/mac_test.py:1508`) also prints `work=<n>`: the processes it counts now, less those whose executable path contains `/target/` but not `/deps/` (an app; test binaries run from `target/<profile>/deps/`). `cargo`, `rustc` and `cargo-nextest` count as work. `MacActive` carries `work: int`. `claim_watch_pass` (lines 1568-1640) writes the optional claim key `work_idle_since` when `work` is 0 and removes it when `work` > 0, under the same `pid`/`proc_start`/`since` check as `idle_since`. `idle_since` and the 15-minute release keep their rule, so a parked app keeps its claim. `read_run` (line 555) accepts the new key. A failed activity check removes neither key.
13. **Docs.** `docs/as-built/mac-test-offload.md` "The idle Mac watch" and `docs/as-built/build-targets.md:69-71` state the new trigger, the saved result, both stages and the showrunner address; `commands/build/queue.md` and `commands/build/status.md` name the top line and what clears it; `scripts/buildlog/index.py:325` column text names `message_showrunner`.

**Files:**
- `scripts/buildlog/mac_idle.py` — `mac_work`, the interval, the waiting count, `alert.json`, `notify_showrunner`, both stages, `cleared`
- `scripts/buildlog/mac_idle_alert.py` — new: `state_dir`, the constants, `SavedAlert`, `read_alert`, `alert_line`
- `scripts/buildlog/test_mac_idle.py` — the outage tests; replace `test_message_resolves_only_builds_unit_in_build_followups`
- `scripts/buildlog/index.py` — action column text only (no schema change)
- `scripts/mac_test/mac_test.py` — `work=` in `mac_activity_script`, `MacActive.work`, `work_idle_since` in `claim_watch_pass` and `read_run`
- `scripts/mac_test/test_mac_test.py` — `work_idle_since` written with only an app process, removed with a test process; `idle_since` rule unchanged
- `scripts/notify/push_texts.py` — `user_send_argv(clears_when=...)`
- `scripts/notify/push_catalog.py` — `mac_idle.blocked`, `mac_idle.stuck`
- `scripts/notify/push_texts_reviewed.json` — their approvals
- `scripts/build_queue/build_queue.py` — the top line (queue-unit path, also-touches)
- `scripts/build_queue/test_build_queue.py` — top line tests; `setUp` (line 51) sets `MAC_IDLE_STATE_DIR` to a private empty folder so the real alert never enters the goldens
- `scripts/build_targets/build_targets.py` — the top line in `command_status` (queue-unit path, also-touches)
- `scripts/build_targets/test_build_targets.py` — top line tests
- `commands/build/queue.md` — the top line
- `commands/build/status.md` — the top line
- `docs/as-built/mac-test-offload.md` — the idle Mac watch section
- `docs/as-built/build-targets.md` — idle watch lines

**Workers:** 1 writer, `impl`, for the closure checks: compare the shipped code with the acceptance gate, then add the two view checks and the admission-error recovery check. The Spec and Files above describe the code that shipped on the Phase 20 branch.

**State:** `~/.local/state/mac-idle/alert.json` (rewritten each pass), `idle.json` (outage `since`), the `messaged`/`pushed` marks, the send keys `mac-idle-showrunner-<since>` and `mac-idle-<since>` in send.py state, and the `emergencies.json` record that `--clears-when` adds. A rerun of a pass with the same files sends nothing more and writes the same result.

**Constraints from prior phases:**
- Showrunner ruling (binding): one check in `mac_idle.py`; idle means no natedev job of ours on the Mac, never the Mac's load; a block counts as idle when no natedev job runs, and so does a claim whose Mac has no process; only a future `unreachable_until` is a separate "Mac unreachable" line; use the recorded `runs_on`, no name matching; the showrunner address comes from `showrunners.py address builds`, never a hard-coded unit name.
- `mac_idle.py` today: `register`, `remove` and `exclude` (lines 340-357) are verify.sh's interface (`scripts/direct/verify.sh:1133-1134`, `1840-1851`, `1853-1878`); keep their arguments and output. `.cases` records and `record_case` rows (lines 249-254) stay one per cause per call.
- The claim watcher (`mac_test.py claim_watch_pass`, lines 1568-1640) writes and removes `idle_since` from one ssh activity check per host every 60 s and releases a claim after 15 idle minutes; Spec 12 adds `work_idle_since` from the same check. `mac_idle.py` reads `work_idle_since` and runs no ssh of its own.
- Phase 9: `verify.sh` starts no new Python; this phase does not change `verify.sh`.
- Phase 14: a `--mac` step waits in `route_test_without_renewal` in state `waiting_mac`; it counts as a waiting step.
- Memory rules: no second copy of the line text or the path (Spec 6 is the one place); a push that asks nothing is held by review, so both texts name an action; the 3 minutes and 10 minutes come from the user's ruling and live as named constants.
- Build rules: fix every visible failure before a rerun; no rerun after a pass until the edits are done; while editing, run the quick unittest modules (`verify.sh check` for any Rust); lint (basedpyright) once, at the phase gate only.

**Acceptance gate:**
- Reproduce first, in `test_mac_idle.py`: `block.json` held by `research`, no claim (and a second case with one claim that has `idle_since`), 3 live `mac_allowed` test records in `waiting_hold`, one with a memory-queue admission file. On e6f8540f the watch writes nothing and sends nothing; after the change, the pass at 3 minutes writes `alert.json` (`idle`, `blocked`, holder `research`, `waiting` 3) and sends one message to the address that a stubbed `showrunners.py address builds` returns, and no push.
- At 10 minutes: one push with `--text-id mac_idle.blocked --need blocked --key mac-idle-<since>` and the `--clears-when ... cleared --since <since>` command; later passes send nothing; a claim without `idle_since` ends the outage; the next outage uses a new key. Cause `claim_idle` and `free` push `mac_idle.stuck` with `--need decision`. A failed send or an empty address tries again on the next pass and never sends twice after success. No waiting step: no line, no message, no push. `natedev_only` and excluded records do not count.
- Parked app (today's 07:50-07:57 case): a live claim with `work_idle_since` set and only an app process on the Mac, no block, 7 `mac_allowed` steps waiting gives cause `claim_idle`, a red line that names the claim's holder and its `what`, the showrunner message at 3 minutes and `mac_idle.stuck` at 10 minutes. In `test_mac_test.py`, an activity answer with only an app gives `work=0` and writes `work_idle_since`; a test binary under `target/debug/deps/` gives `work>0` and removes it.
- A future `unreachable_until` gives the `MAC UNREACHABLE` line, no message and no push. `cleared --since` exits 0 and 1 as Spec 9 says.
- View tests: `build_queue.py` text and `--json` put the line first with tone `error`; `build_targets.py status` prints it first; a missing file adds nothing; a stale file shows the stale line. `alert_line` on a fixture runs with `subprocess.run` patched to raise and returns in under 50 ms.
- `scripts.buildlog.test_mac_idle`, `scripts.buildlog.test_index`, `scripts.build_queue.test_build_queue`, `scripts.build_queue.test_queue_view`, `scripts.build_targets.test_build_targets`, `scripts.notify.test_push_texts` and `scripts.notify.test_review` pass; basedpyright `0 errors, 0 warnings, 0 notes` on every changed Python file and its callers (`scripts/buildlog/cli.py`, the importers of `build_targets.py`, `push_texts.py` and `build_queue.py`).
- Live proof on natedev: if a block with no natedev job still holds and steps wait, run the worktree's `mac_idle.py watch` with `MAC_IDLE_STATE_DIR` set to a scratch folder whose `idle.json` has `since` 200 s ago; it writes the alert and the builds showrunner gets the message; `build_queue.py` and `build_targets.py status` with that folder show the red line in under a second. Else the same run against a fixture state folder.
- Merge gate `--tests-only` green on a tree merged with `origin/natedev`. The checkpoint notice names the also-touches paths (`scripts/build_queue/build_queue.py`, `scripts/build_targets/build_targets.py`, `commands/build/queue.md`, `commands/build/status.md`, owner queue-unit; `scripts/notify/push_texts.py`, `scripts/notify/push_catalog.py`, `scripts/notify/push_texts_reviewed.json`, owner per the production doc Units row).

### Phase 17 — Move a step to the Mac when a short-term decline clears  · status: todo

**Origin:** builds showrunner, 2026-10-31 ~07:57 EDT, accepted as current work: "The temporary-decline fix is current work: its own phase right after 16, beside it if the files allow. Record disk, unreachable, backoff and state declines as Mac-allowed with their Mac target, so a step moves when the cause clears. Permanent reasons stay natedev-only." At about 07:57, 4 live steps had the record `natedev_only disk`. Built on branch `natedev-verify-cancel`, worktree `/home/natepiano/worktrees/claude-build-followups-verify-cancel`, after Phase 16.

**Depends on:** Phase 16, and it runs after Phase 16 on the same branch. It cannot run beside Phase 16: the files overlap. The cause is in `scripts/build_targets/build_targets.py` (`mac_target_eligibility`, `command_kind_eligible`), and its tests are in `scripts/build_targets/test_build_targets.py`. Phase 16 also edits both files (its `test` worker owns them; `command_status` and its tests), and it owns `docs/as-built/mac-test-offload.md` and `docs/as-built/build-targets.md`, which this phase also changes. `scripts/direct/verify.sh` is not a Phase 16 file. Start this phase when Phase 16's checkpoint is on the branch.

#### Work Order

**Constraints from the phase 20, 23 and 24 review:** Phase 27 lands first: it changes Mac eligibility and the retry hooks (`target_can_retry`) and gives a disk decline the same leave hook as busy, with no 180-second delay. After Phase 27, reduce this phase to the cases Phase 27 does not cover, with one integration writer and one tester. Phases 17 and 27 share one acceptance test: a disk decline keeps the Phase 24 Mac disk alert (`mac_idle.py` counts `NatedevOnly("disk")`) through retry eligibility and local execution.

**Source:** builds showrunner ruling, 2026-10-31 ~07:57 EDT (binding). Cause at e6f8540f:
- `initialize_step_record` (`scripts/direct/verify.sh:1697-1773`) runs `build_targets.py eligible --kind` once. Its first line becomes the step's `runs_on` (`step_record.py write --runs-on`, lines 1741-1745).
- `mac_target_eligibility` (`scripts/build_targets/build_targets.py:204-226`) excludes a Mac target for `backoff` (`offload.mac_step_eligibility`, `scripts/mac_test/offload.py:1586-1587`), `unreachable` (line 221, the ssh probe failed), `disk` (line 223, `free_gib` below `free_floor_gib`) and `state unreadable: <error>` (line 225). `command_kind_eligible` (lines 229-258) then answers `natedev_only <reason>` (line 252), the same answer as for a permanent reason.
- A `natedev_only` step never sets `MAC_LEAVE_SETTINGS`. `route_test` (verify.sh:1953-1966) only records the decline of an `excluded` row, and the non-test path (1764-1772) reads only `eligible` rows. No leave hook exists, so the step stays on natedev after the cause clears.
- A waiting `mac_allowed` step moves with no rerun. The memory queue leave hook (`scripts/lint/memory_gate.sh:319-320`, from `LINT_QUEUE_LEAVE_HOOK` in `run_local_test`, verify.sh:2155-2193) runs `mac_test.py claim-wait`; the step leaves its wait with status 75 and runs `try_mac_test` (2190). The hold wait passes `--leave-hook` (2416-2417, 2425-2428). The token wait runs the same hook (2695-2698). The routing loop tries each Mac again (1991-2061). `remember_mac_retry` (1853-1878) is the one retry rule: `no_tests`, `off`, `repo`, `linux_only` and `local_flag` remove the hook; `busy` and `blocked` try again on the next poll; every other result sets a deadline 180 s later.

Start reasons, by class (evidence at e6f8540f):

| Reason | Where it is decided | Class | Why |
| --- | --- | --- | --- |
| `backoff` | `offload.py:1586-1587`; `write_backoff` after a failed reach (`offload.py:1452`, `1613`) for `unreachable_backoff_s` (default 300, `offload.py:781-782`) | short-term | It has an end time. `mac_test.py free_now` (2070-2100) answers `backoff` until then. |
| `unreachable` | `build_targets.py:220-221` | short-term | One ssh probe failed or timed out (`offload.py:1020-1040`). |
| `disk` | `build_targets.py:222-223` | short-term | Free space comes back after a sweep. `claimed_run` probes again before each run (`offload.py:1450-1455`). |
| `state unreadable: <error>` | `build_targets.py:224-225` | short-term | An `OSError`, `UnicodeError` or `ValueError` while the settings are read or the Mac is probed. |
| `off` | `offload.py:1568-1569` | permanent | Offload is off in the settings. |
| `repo` | `offload.py:1570-1575`, `1584-1585` | permanent | Unknown repository, no `linux_only.<repo>` key, or a workspace that cannot be read. |
| `linux_only` | `offload.py:1577-1583` | permanent | The package or a workspace member is Linux only. |
| `--local <why>` (`LocalRequired`) | `build_targets.py:211-212`, `244-245`; recorded as `local_flag` (verify.sh:1178-1181) | permanent | The caller asked for natedev. |
| `makes natedev artifacts`, `full gate runs on natedev` | `build_targets.py:207-210`, `240-243` | permanent | The kind runs on natedev. |

`blocked`, `busy`, `mac_busy`, `copy`, `lost`, `runner` and `no_tests` are Mac run results, never start reasons. This phase does not change how they are handled.

**Goal:** a step that starts while the Mac declines for a short-term cause (`disk`, `unreachable`, `backoff`, unreadable state) is recorded `mac_allowed` with its Mac target, waits on natedev with the leave hooks set, and moves to the Mac when the cause clears, with no rerun. A permanent reason still gives `natedev_only`.

**Spec:**

1. **Reproduce first.** Write the Acceptance gate's first test before any code change, and see it fail on e6f8540f.
2. **One classification, in `build_targets.py`.** Add `@dataclass(frozen=True) class RetryTarget` with `target: LocalTarget | SshTarget` and `reason: str`, beside `MacAllowed` (line 199). `mac_target_eligibility` (lines 204-226) returns `RetryTarget` for `IneligibleMacStep("backoff")`, for `unreachable` (line 221), for `disk` (line 223) and for `state unreadable: <error>` (line 225). Every other reason stays `ExcludedTarget`. The kind and `--local` rules (lines 207-212) return before the probe, so they never give `RetryTarget`.
3. **The table row.** `print_eligible_target` (line 183) prints a `RetryTarget` as `<name>\tretry\t<reason>\t<settings>`. The row form is the interface between the two writers. `eligible` without `--kind` (`command_eligible`, `targets.target_eligibility`) does not probe and does not change.
4. **The decision.** `command_kind_eligible` (lines 229-258) answers `mac_allowed` when a `macos` target is an `EligibleTarget` or a `RetryTarget`. Otherwise it answers `natedev_only <reason>` as today. A failed `read_targets` (lines 236-238) keeps `natedev_only state unreadable: <error>` with no table: there is no Mac target to record.
5. **verify.sh reads the row.** No new Python start (Phase 9).
   - The row check (1721) accepts `retry`. In `initialize_step_record`, a `retry` row with settings counts as a Mac target (1726-1735; it needs `mac_allowed`).
   - New function `start_mac_retry <settings> <reason>`: it sets `MAC_LEAVE_SETTINGS`, `MAC_LEAVE_REASON` and a deadline 180 s from now with the clock rule of `remember_mac_retry` (1866-1871), prints once per call `verify.sh: the Mac declined at start (<reason>); waiting 180 s before each retry while natedev waits.` (sets `MAC_RETRY_NOTICE_SHOWN=1`), and calls `register_mac_wait <settings> <deadline>` (1840-1851). `remember_mac_retry` uses the same deadline code; keep one copy of it.
   - `route_test`, first loop (1953-1966): for a `retry` row with settings and `-f "$MAC_RUNNER"`, set `BUILDLOG_MAC=declined`, `BUILDLOG_MAC_REASON` and `BUILDLOG_LOCAL_REASON` as an `excluded` row does now (1959-1964), so the build-log row stays the same. Then call `start_mac_retry`, and add the row to `retry_settings`, `retry_reasons` and `retry_after`, so the routing loop (1991-2061) tries it after the deadline. The selection loop (1968-1989) still tries only `eligible` rows at once.
   - Non-test kinds (1764-1772): when no row is `eligible`, the first `retry` row with settings calls `start_mac_retry`. Do not set `BUILDLOG_MAC`: a compiling step goes to the Mac only under a hold, as now.
   - `--mac` (`MAC_ONLY`, 1749-1762): refuse at once when no row is `eligible`, also when the answer is `mac_allowed`. Read the refusal word from `retry` rows and `excluded` rows (the case list at 1756). Text and exit 2 stay as now (`test_verify_mac_only.py:182`).
   - After the deadline, the hooks run `free-now` or `claim-wait` and then `try_mac_test`. If the Mac declines again (`disk`, `unreachable`, `runner`), `remember_mac_retry` sets a new 180 s deadline, as now. Its permanent list (1855) does not change.
6. **What stays.** `step_record.py` does not change: a `mac_allowed` record has no reason field (line 250). The `mac_idle.py register`, `exclude` and `remove` arguments do not change. `mac_test.py` does not change.
7. **Docs.** `docs/as-built/mac-test-offload.md:7` names the `retry` row and the four short-term reasons. `docs/as-built/build-targets.md:60` and `:64-65` say that a `retry` row sets the leave settings and a 180 s deadline at the start.

**Files:**
- `scripts/build_targets/build_targets.py` — `RetryTarget`, `mac_target_eligibility`, `print_eligible_target`, `command_kind_eligible`
- `scripts/build_targets/test_build_targets.py` — `StepKindEligibilityTests`: replace `test_off_backoff_and_disk_floor_refuse_mac_steps` (line 537 at e6f8540f), which asserts the old `natedev_only disk` and `natedev_only backoff` answers
- `scripts/direct/verify.sh` — the `retry` row, `start_mac_retry`, `route_test`, the non-test path, the `--mac` refusal
- `scripts/direct/test_verify_mac_start_decline.py` — new: the reproduce test, the permanent cases, the `--mac` case
- `docs/as-built/mac-test-offload.md` — the decision point text
- `docs/as-built/build-targets.md` — the routing and leave lines

**Workers:** 1 integration writer (`impl`) and 1 tester (`test`). Before dispatch, cut Spec 2-5 to the start declines that Phase 27 does not cover. Phase 27 removes the 180 s delay for a disk decline, so `RetryTarget` and the 180 s start deadline are not built unless a case still needs them.

**State:** the step record `runs_on` in `~/.local/state/verify-steps/<call>.json` (now `mac_allowed` for a short-term decline) and the `mac_idle.py register` record with its retry deadline. Each is written once per call, as for an `eligible` row now. No new file, lock or mark. A rerun is a new call with a new record.

**Constraints from prior phases:**
- Phase 9: `verify.sh` starts no new Python. A `retry` row makes the one `mac_idle.py register` start that an `eligible` row makes now (1955-1957); add no other start.
- Phase 16: its `waiting_calls` counts live `mac_allowed` records in `waiting_hold` or `waiting_mac` with no `local_reason`. After this phase, a step that waits on a short-term decline counts there. `mac_idle.py`, `mac_idle_alert.py` and `mac_test.py` are Phase 16 files; this phase does not change them.
- `mac_test.py free_now` (2070-2100) answers `off`, `backoff`, `blocked`, `state`, `busy` or `free`; it never reads disk space or reach. The 180 s deadline covers `disk` and `unreachable`, and `claimed_run` probes again before a run (`offload.py:1450-1455`).
- Phase 14: a `--mac` step waits only on `busy`; every other decline ends it (`check_mac_only_result`, verify.sh:1930-1935). A `retry` row keeps that.
- `remember_mac_retry` and `mac_retry_hook` (1853-1898) are the one retry rule (`docs/as-built/build-targets.md:64`). A start decline uses the same 180 s deadline; add no second timer.
- Delegation Context invariant: build-log rows keep `mac=declined` and `mac_reason=<word>` for these steps.
- Build rules: fix every visible failure before a rerun; no rerun after a pass until the edits are done; while editing, run the quick test modules; lint once, at the phase gate only.

**Acceptance gate:**
- Reproduce first, in `test_verify_mac_start_decline.py` (the `VerifyMacOffloadFixture` family, with the real `build_targets.py`): settings `free_floor_gib=60`, the `ssh` stub answers `free_gib=1`, memory held. On e6f8540f the step record has `runs_on` `{"kind": "natedev_only", "reason": "disk"}`, and after the stub answers `free_gib=1000` and the clock passes 180 s, `free-now` is never asked and the runner gets no call: the test fails. After the change the record has `runs_on` `{"kind": "mac_allowed"}`; once the stub answers `free_gib=1000` and the clock passes the deadline, the step leaves the memory wait, the runner gets one call and natedev runs nothing. A second case does the same from the hold routing wait.
- A permanent reason stays `natedev_only`: `linux_only.<repo>=sample` gives `natedev_only linux_only`, no `free-now` ask and no runner call; `--local` gives `natedev_only` with its reason. `--mac` with `free_gib=1` refuses at once with `the Mac is below its free-space floor`, exit 2, and nothing runs.
- In `test_build_targets.py`: `disk`, `backoff`, `unreachable` (`probe_mac` gives `UNAVAILABLE_PROBE`) and `state unreadable` (`read_config` raises `OSError`) each answer `mac_allowed` with a `retry` row; `off`, `repo`, `linux_only`, `--local`, `build`, `example` and `final` still answer `natedev_only`; one `retry` Mac and one `off` Mac give `mac_allowed`.
- `scripts.direct.test_verify_mac_start_decline`, `scripts.build_targets.test_build_targets`, `scripts.build_targets.test_targets`, `scripts.direct.test_verify_mac_only`, `scripts.direct.test_verify_mac_memory_excluded`, `scripts.direct.test_verify_mac_memory_retry`, `scripts.direct.test_verify_mac_memory_retry_clock`, `scripts.direct.test_verify_mac_retry_clock`, `scripts.direct.test_verify_mac_retry_hold`, `scripts.direct.test_verify_mac_routing`, `scripts.direct.test_verify_mac_wait_records`, `scripts.direct.test_verify_step_routes`, `scripts.direct.test_verify_checks_fail_closed` and `scripts.direct.test_verify_python_starts` pass.
- basedpyright `0 errors, 0 warnings, 0 notes` on every changed Python file and its callers (`scripts/build_hold/hold_applies.py`, `scripts/direct/local_release_wait.py`, `scripts/production/merge_gate.py`); `bash -n scripts/direct/verify.sh`; `shellcheck scripts/direct/verify.sh` with no new findings.
- Merge gate `--tests-only` green on a tree merged with `origin/natedev`. The checkpoint notice names the also-touches paths `scripts/build_targets/build_targets.py` and `scripts/build_targets/test_build_targets.py`, owner queue-unit.

### Phase 18 — One standing gate checkout per unit, made only by copies.py  · status: done

#### As-built

Checkpoint `f8a20f368` on `verify-copies`.

- `copies.standing(owner, repo, commit, *, apply_uncommitted=None, merge=None)` supplies two checkouts with separate Git directories and retained targets.
- The configured root contains `<repo>/<owner>-gate` and `<owner>-gate-2`. A third caller waits; linked sources use the canonical repository name.
- Each use fetches, detaches and cleans untracked files without deleting ignored output. Uncommitted changes precede the optional merge.
- `run`, `list` and `remove` share this implementation. Commands receive `{tree}` substitution; refusals exit 2, apply or merge failures exit 3.
- Existing busy flocks protect preparation and use. Commands inherit their descriptors, so wrapper death cannot release a surviving command's lock.
- Atomic `.gate.json` publication and `.removing` directories permit retries. A replacement source rebuilds Git metadata without deleting the target.
- `PostUseError` preserves a caller's error separately from cache or record failures. Applied untracked files receive current modification times.
- `config/copies.conf`, read through `scripts.lib.conf`, supplies `root`, `wait_seconds`, `git_timeout_seconds`, `guard_timeout_seconds`, fallback owners and creator exceptions.
- The shipped secondary recheck interval is one second. Primary release wakes acquisition immediately; the checkout count is fixed at two.
- CI, nightly, scratch, style and toolchain callers use the service. Both agent hook fronts and the repository scanner use the copy guard.
- Crate frozen bases and integration proofs use standing checkouts. Editable package workers use `copies.workspace()` and caller-owned `gate:` tasks.
- Worker names are `crate-<run>.<package>`. One canonical owner reaches nested proofs; initialized workers retain edits during resume.
- Legacy manifest paths remain authoritative. Adoption validates source, branch and base; terminal cleanup preserves busy or unsaved workers and external result evidence.

**Files:**

- `scripts/copies/{copies,copy_guard,shell_syntax}.py`, their tests and `config/copies.conf` — checkout lifetimes, creation policy and configuration.
- `scripts/lib/scratch.py`, `scripts/lint/sweep.py` and their tests — inherited busy descriptors, task cleanup and configured cache discovery.
- `scripts/production/{ci_points,merge_gate,waiting}.py`, `scripts/buildlog/rust_release.py` and tests — standing callers and verdict preservation.
- `scripts/style/{bulk_run,style_sync,crate_run}.py` and tests — retained targets, canonical ownership and editable worker recovery.
- `scripts/hooks/{hook_dispatch,codex_hooks}.py` and tests — the combined copy and scratch guard.
- `docs/as-built/gate-checkouts.md`, caller documentation and command recipes — checkout use, original-worktree exceptions and non-Git reproduction.

**Binds later work:** The checkout APIs own repository creation. Broader guard import coverage remains in `docs/plans/natedev-verify-next.md`.

**Gotchas:** Standing targets can be swept while idle. Run workers need task completion evidence, not a fixed deadline. Guard coverage has documented syntax limits.

**Ruled out:** Named frozen gate copies, seeded targets, shared target directories, a third standing checkout, and a creator exception for crate runs.

**Assertion-Change:** Checkout-retention, target-ownership, registration and cleanup assertions use the new lifetimes. The changed tests are:

- `scripts/buildlog/test_rust_release.py`: `test_finished_trial_records_counts_sizes_and_one_result_text`, `test_next_trial_resets_sources_and_keeps_its_build_cache`, `test_mend_nonzero_finishes_and_text_says_does_not_build`, `test_failed_clippy_step_records_first_error_and_is_not_retried`, `test_killed_trial_waits_and_retries_next_night`, `test_hold_arriving_mid_trial_leaves_pending_before_checkout`, `test_timeout_releases_checkout_and_records_failed_step`, `test_hold_after_nonzero_clippy_keeps_trial_waiting_and_checkout`.
- `scripts/production/test_ci_points.py`: `test_a_contained_validated_commit_passes_after_the_branch_moves_without_pushing`, `test_a_validation_fix_fast_forwards_an_unchanged_remote_branch`, `test_rerun_after_a_pushed_fix_with_no_handoff_reuses_the_fix_commit`, `test_retry_after_a_failed_push_publishes_the_existing_fix_without_a_sibling_commit`, `test_a_merge_landing_while_ci_publish_waits_for_the_lock_is_preserved`, `test_a_validation_fix_cannot_overwrite_a_branch_that_moves_during_validation`, `test_validation_uses_a_private_copy_and_leaves_the_production_checkout_unchanged`, `test_a_failed_validation_keeps_its_private_checkout_without_pushing`.
- `scripts/production/test_merge_gate_ledger.py`: `test_nightly_reuses_a_standing_checkout_and_marks_and_notifies_only_red`.
- `scripts/style/test_bulk_run.py`: `test_workspace_validation_runs_once_and_one_clean_test_commit_merges_all_decisions`, `test_tested_tree_proof_rejects_drift_after_each_command`, `test_hidden_untracked_test_output_never_publishes_or_keeps_the_batch_commit`.
- `scripts/style/test_style_sync.py`: `test_clean_check_mode_records_all_tools_passing_and_returns_zero`, `test_standing_target_survives_task_end_and_reuses_artifacts`, `test_lint_keeps_its_own_warm_target_after_metadata_cleanup`, `test_a_missing_or_swept_target_runs_cold`, `test_redirecting_the_target_cannot_grant_successful_lint_coverage`, `test_the_owner_record_exists_before_the_checkout_is_added` (removed), `test_standing_checkout_has_no_registration_in_the_source`, `test_a_git_failure_in_cleanup_keeps_metadata_until_the_next_sweep`, `test_lint_takes_second_checkout_when_first_is_busy`.
- `scripts/lib/test_scratch.py`: `test_standing_checkout_creates_a_stamped_cache_the_sweep_accepts`.
- `scripts/lint/test_sweep.py`: `test_floor_keeps_a_standing_target_only_while_its_checkout_is_busy`, `test_floor_finds_and_reclaims_an_idle_retained_standing_target`.
- `scripts/copies/test_copies.py`: `test_configured_root_contains_only_the_two_standing_checkouts`, `test_missing_or_invalid_required_configuration_refuses_before_clone`, `test_legacy_workspace_keeps_commits_index_and_journal_and_ends_through_holds`.
- `scripts/hooks/test_codex_hooks.py`: installation tests using `GUARD_COMMAND` and `GUARD_GROUP`.
- `scripts/hooks/test_hook_dispatch.py`: `test_dispatch_tables_equal_legacy_files_matchers_and_timeouts`.
- `scripts/production/test_waiting.py`: `test_scratch_uses_no_worktree_cleanup_and_reuses_the_checkout`.
- `scripts/copies/test_copy_guard.py`: `test_scan_resolves_keyword_variable_and_multiline_argv_without_losing_git_options`, `test_shell_script_scan_retains_the_same_literal_and_executable_distinction`.
- `scripts/style/test_crate_run.py`: `test_combined_test_failure_retains_pending_claims_for_resume`, `test_moved_destination_invalidates_prepared_combined_proof`, `test_other_crate_refresh_cannot_publish_stale_tested_candidate`.

### Phase 20 — Run clippy judgment first, fix only on a finding, and send Mac-allowed lint to a free Mac  · status: done

#### As-built

Checkpoint 41a3ce6d4 on `natedev-verify-cancel`.

- `verify.sh lint` runs mend, fmt and Taplo before workspace clippy judgment with all targets, all features and `--keep-going`. A clean judgment ends the stage without clippy fixes.
- Any Cargo status 101 permits clippy fixes unless a lint hold or kill report prevents repair. Compile errors, mixed failures and unknown 101 diagnostics also permit repair; no lint-finding classifier exists.
- Fixes and fmt repeat until the tree stops changing or reaches `clippy_fix_pass_limit`, then another judgment supplies the result. `config/lint.conf` supplies the limit at repair time; integers 1–9999 are valid, with 3 for missing or invalid values.
- Mac-allowed lint, check and doc steps use the same `free-now` decision as tests during the token wait. A free Mac triggers reservation and routing without an idle marker; unavailable capacity leaves the step waiting locally.
- The Mac idle watcher and alert display also ship on this branch. They read `config/mac_test.conf` through `conf.py`, query admission waiters, display the saved alert in build views, and notify the builds showrunner.

**Files:**

- `scripts/direct/verify.sh`, `config/lint.conf` — judgment order, bounded repairs, verdict keys, tree watches and Mac routing.
- `scripts/direct/test_verify_lint_judgment.py`, `scripts/direct/test_verify_mac_lint_token.py` — judgment, kill, hold, limit and shared-routing contracts.
- `scripts/direct/verify_tree_cancel_test_support.py`, `scripts/direct/test_verify_tree_cancel_lint.py` — the first stub judgment exits 101; rewrites retain cancellation coverage.
- `scripts/buildlog/mac_idle.py`, `scripts/buildlog/mac_idle_alert.py`, `scripts/buildlog/test_mac_idle_alert.py` — the idle watcher, saved alert and config contract.
- `scripts/mac_test/offload.py`, `scripts/mac_test/test_offload_forward.py`, `scripts/build_targets/test_build_targets.py` — fetch connection settings and isolated alert configuration.

**Binds later work:** Repair eligibility uses status, hold and kill evidence. Mac routing uses the existing availability decision and admission reservation.

**Gotchas:** A kill flag or Cargo signal report vetoes repair independently. A final changing fix prevents a cached lint failure. The stub must return 101 to exercise repairs.

**Ruled out:** A lint-finding classifier; an idle-marker prerequisite for Mac routing; splitting whole-workspace hana lint between Mac and Linux. A workspace containing configured Linux-only members remains ineligible for Mac lint.

### Phase 22 — Refuse an unknown verify verb, and key the lint cache by tool versions  · status: done

#### As-built

Checkpoints `776c74281` and `eabe13cac` on `verify-verb-cache`; branch tip `47a58912c` is origin/builds `e7ca3f68b` plus this change only; builds-verify tip `42e58332f`.

- `verify.sh` refuses an unknown verb right after `CMD=` is set: it prints the usage text and exits 2, before a mesh worker's call detaches, before any run identity, target query or step record.
- The lint cache key in `tree_watch.py` holds the versions of mend, nightly rustfmt, clippy and taplo. A cached result made by another tool version never replays. Each lint call keeps its version answers in its wait directory.
- The doc test fixture sets `LINT_SKIP_SWEEP=1`, so no background lint sweep outlives the fixture's cargo stub.

**Files:**
- `scripts/direct/verify.sh` — the verb check; the version answers per lint call.
- `scripts/direct/tree_watch.py` — tool versions in the lint cache key.
- `scripts/lint/invoke.sh` — the version answers.
- `scripts/direct/test_verify_verbs.py`, `scripts/direct/test_verify_lint_cache_versions.py`, `scripts/direct/test_tree_watch.py`, `scripts/direct/test_verify_lint_scope.py` — the verb refusal and the version key.
- Fixtures: `verify_lint_scope_test_support.py`, `verify_examples_test_support.py`, `verify_mac_offload_test_support.py`, `verify_step_routes_test_support.py`, `verify_tree_cancel_test_support.py`, `verify_doc_test_support.py` — fake tools answer `--version`.

**Gotchas:** a fake clippy or cargo in a test fixture must answer `--version` before its judgment logic, or the version probe counts as a lint call.

### Phase 23 — A cancelled lint run leaves no verify process and no queue place  · status: done

#### As-built

Checkpoint 353e00511 on `verify-lint-cancel`.

- The lint mend stage calls `invoke_mend` directly. The parent verifier retains `RUN_STEP_PID` and stops the active step group during interruption cleanup.
- The existing `run()` tee captures mend diagnostics through `MEND_LOG`; the mend stage reads its command status directly.
- A group TERM ends the verifier and its step, releases admission and token resources, removes the step record, and records status 143. Cancellation creates no cached verdict.
- `verify_interrupted` and `hold_interrupted` ignore repeated INT, TERM and HUP signals until cleanup completes.

**Files:**

- `scripts/direct/verify.sh` — parent-owned mend execution, shared diagnostic capture and signal-safe cleanup.
- `scripts/direct/test_verify_tree_cancel_lint.py` — cancellation during memory admission, mend, clippy and repeated TERM; `CompletedLintRun` types the recorded exit status.
- `scripts/direct/verify_tree_cancel_test_support.py` — an optional real `mesh_runs.py` supervisor with private result delivery.
- `scripts/direct/verify_token_ownership_test_support.py` — bounded group-exit observation with the existing default retained.

**Binds later work:** Lint execution retains the active step PID in the parent verifier. Interruption cleanup retains ownership until admission, token and step records are removed.

**Gotchas:** An outer shell pipeline can place the step PID in a subshell. A supervisor can forward TERM after a group TERM reaches the verifier; cleanup must tolerate both signals.

**Ruled out:** A separate cancellation service; a cleanup sweep; reliance on admission-service cleanup alone.

### Phase 24 — Raise the Mac idle alert on a disk exclusion, and survive an admission query error  · status: done

#### As-built

Checkpoint 23aa6712f on `verify-idle-disk`.

- The Mac idle watcher counts `NatedevOnly("disk")` steps in `WAITING_HOLD`, `WAITING_MAC` and `ON_NATEDEV`. Other natedev-only reasons retain their existing behavior.
- The saved alert includes the disk exclusion and displays “Mac disk exclusion”. Its push names sufficient Mac disk space as the clearing condition; excluded calls receive no retry marker.
- `admission_waiters()` returns `AdmissionWaiters` or `AdmissionQueryFailed`. A failed query records its reason in the saved alert and stderr; the watcher continues independent checks.
- `SavedAlert.admission_error` stores the query failure when present. A later successful sample removes it.
- The admission decoder converts arithmetic decode errors, including numeric overflow, into an invalid-message error. The client reports that error as `AdmissionUnavailable`.

**Files:**

- `scripts/buildlog/mac_idle.py` — disk-exclusion counting, typed admission results, continued checks and disk-specific notification text.
- `scripts/buildlog/mac_idle_alert.py` — the `disk` cause, optional admission error and display validation.
- `scripts/buildlog/test_mac_idle.py` — disk states, query failures, recovery and an overflowing reply through `watch_once`.
- `scripts/build_queue/admission_protocol.py` — arithmetic decode errors become invalid admission messages.

**Binds later work:** Mac disk exclusions use `NatedevOnly("disk")`. The idle alert preserves that reason and does not retry an excluded call.

**Gotchas:** A huge integer can overflow during numeric validation. The watcher catches `AdmissionUnavailable`; the decoder converts malformed replies at the protocol boundary. The Mac disk minimum remains unchanged.

**Ruled out:** Lowering the disk minimum to restore routing; treating every natedev-only reason as a Mac exclusion; catching all exceptions in the watcher.

### Phase 25 — Run the Mac floor sweep on a disk exclusion, then check again  · status: todo

**Origin:** builds showrunner ruling, 2026-11-03 15:0x EST: a disk exclusion runs the existing floor sweep, then checks again; no second sweeper. resources-unit owns `sweep.py` and adds the Mac entry point and the mac_test claim check (their Phase 50, checkpoint d70f1dedf).

#### Work Order

**Constraints from the phase 20, 23 and 24 review:** Safe call sites: disk checks are in `scripts/build_targets/build_targets.py` (near line 280) and `scripts/mac_test/offload.py` (near line 1869); `mac_test.free_now` reads local state only and stays so. Resources' `floor_claims.py` (near line 104) holds claim locks during the sweep, so run the sweep with no claim lock held. Test concurrent sweeps, cancellation, a failed sweep and a fresh disk read after the sweep. The acceptance gate also owns the disk alert's recovery and clearing checks.

1. When admission finds the Mac under its disk minimum, mac_test runs resources' entry point once (`ssh mac '~/.claude/scripts/lib/py ~/.claude/scripts/lint/sweep.py --floor-only --host mac'`), then reads Mac free space again and admits the step when it is above the minimum. One sweep at a time; a run that finds a sweep running waits for its result.

**Pending decision:** none.

**Depends on:** Phase 24 (the disk exclusion read), and resources Phase 50 (checkpoint d70f1dedf, the Mac floor sweep) landed in origin/builds.

2. The Mac test mirror root is spelled in one place: one key in `config/mac_test.conf` (value `~/.local/state/mac-test/mirror`, no move), read at use by `scripts/mac_test/offload.py` and by resources' `sweep.py`. No other literal of that path stays in code. Builds ruling 2026-11-03 (this replaces the dropped mirror-move phase).

**Acceptance gate:** a test with a fake Mac that is under the minimum before the sweep and over it after, which admits the step; one with a sweep that frees too little, which keeps the step on natedev; a scan that finds the mirror root only in `config/mac_test.conf`; merge gate `--tests-only` green on a tree merged with origin/builds.

### Phase 27 — A step sent to natedev after a Mac disk decline goes back to a free Mac  · status: done

#### As-built

Checkpoint `a153ef36a` on `verify-disk-leave`; builds-verify tip `540aa02db` (merged with builds `e7ca3f68b`).

- A `disk` decline gets the leave hook, like `busy` and `blocked`: a step that waits on natedev leaves for the Mac when free-now says the Mac is free.
- Every temporary decline reason (backoff, disk, unreachable, spending) keeps the leave route. Permanent reasons (`off`) do not.
- `step_can_retry()` reads the whole target table, so target order does not change the answer. `STEP_DECLINE_REASON` is removed.
- The step record keeps `NatedevOnly("disk")` whenever a target is excluded for disk, so the Mac idle disk alert keeps its evidence.
- AWS spending refusals still block each claim and each command; only the retry answer changes.

**Files:**
- `scripts/build_targets/build_targets.py` — temporary and permanent decline answers.
- `scripts/direct/verify.sh` — `step_can_retry()` on the full target table; the disk leave hook.
- `scripts/build_targets/test_build_targets.py`, `scripts/build_targets/test_ssh_eligibility.py`, `scripts/direct/test_verify_ssh_targets.py` — the changed decline answers.
- `scripts/direct/test_verify_mac_disk_leave.py` — a disk decline, then a free Mac, and the step leaves for the Mac.

**Binds later work:** a tree-writing step (Phase 29) never retries to a remote target. Keep `NatedevOnly("disk")` in the step record for the Mac idle disk alert.

**Gotchas:** the routing clock fixture in `verify_mac_offload_test_support.py` copies `build_folders.py`, `conf.py`, `process_group.py` and `unit_runs.py` into its fake library; a new import from verify's Python helpers goes on that list.

### Phase 28 — Keep the cargo token from a Mac step to the next local step  · status: done

#### As-built

Checkpoint `3a005d504` on `verify-token-keep`.

- `release_token()` writes `.given_back` when a shell releases the token. The parent formatter reads the mark and acquires the token again.
- `acquire_token local` prevents a local formatter from entering remote routing.
- `test_local_fmt_starts_after_mend_passes_on_the_mac` covers mend on the Mac followed by local fmt through the build-hold route.

**Files:**

- `scripts/direct/verify.sh` — shared release evidence and local formatter acquisition.
- `scripts/direct/test_verify_token_mac_step.py` — Mac-to-local ownership through a build hold.

**Binds later work:** Every release must remain visible across shells until acquisition succeeds.

**Gotchas:** This checkpoint still consumes an existing release mark during a second release. The memory-wait route can still exit 125 at fmt.

**Ruled out:** Blaming the original local-only token policy; the later direct Mac release introduced the missing evidence.

### Phase 29 — A lint step that writes the tree stays on natedev, and a lost token is never cached  · status: done

#### As-built

Checkpoint `1a2800ef6`, merged to builds as `d1d655a94`.

- A lint step that writes the tree (`cargo mend --fix`, fmt, `clippy --fix`) never moves to the Mac. verify.sh `step_writes_tree` keeps it on natedev, where it waits in the natedev queue. A read-only step (`mend --fail-on-warn`, `fmt --check`) can still go to the Mac.
- The lint result cache never records status 125 (a lost cargo token). The next run does the step again.

**Files:**
- `scripts/direct/verify.sh` — `step_writes_tree` routing and the 125 cache rule.
- `scripts/direct/test_verify_lint_local_writes.py`, `test_verify_lint_cache_token_loss.py` — the two regressions.
- `scripts/direct/test_verify_token_mac_step.py` — the Mac step in its fixtures is `mend --fail-on-warn`, not `mend --fix`.

**Binds later work:** a tree-writing step stays on natedev with both remote hooks off.

### Phase 31 — A lint step that leaves the memory wait for the Mac keeps the token for its next local step · status: done

#### As-built

Checkpoint `b076a291e` on `verify-token-mac-wait`.

- `release_token()` keeps `.given_back` across repeated releases. `sync_token_state()` updates shell ownership before both acquisition paths.
- Only `token_acquired()` removes the mark after successful acquisition. Memory and build-hold waits use the same release rule.
- A memory wait can leave for the Mac, record its retry note, and then run local fmt with the token.
- `blocking_claim_block()` reuses the existing Mac block reader. Foreign blocks prevent local reservation reuse and transfer under the existing locks.
- Existing remote work retains its cleanup route. A reservation does not override a later block.

**Files:**

- `scripts/direct/verify.sh` and `test_verify_token_mac_step.py` — shared ownership state and memory-to-Mac regressions.
- `scripts/mac_test/mac_test.py` and `test_mac_test.py` — one block ownership rule and transfer refusal.
- `scripts/build_targets/build_targets.py` and `test_ssh_eligibility.py` — block checks for published and unpublished reservation reuse.

**Binds later work:** A release mark persists until acquisition succeeds. All new Mac work observes the shared block rule.

**Gotchas:** Pipeline shells do not share variable changes. Both reservation reuse and transfer need block checks; ordinary free-now routing alone cannot protect them.

**Ruled out:** A formatter-only workaround, separate release marks in wait wrappers, and a second Mac block reader.

### Phase 32 — The cargo token wait line reads the user zone without an unset variable · status: done

#### As-built

Checkpoint `ce0d0cc6b` on `verify-zone-token`; builds integration `647ae6bbb`.

- `token_wait_note()` calls `scripts/lib/user_zone.py` directly through `python3 -S` for its first wait timestamp.
- The wait no longer requires `USER_ZONE_SCRIPT`. The handoff test removes that variable and checks the UTC wait line and successful continuation.

**Files:**

- `scripts/direct/verify.sh` — direct user-zone lookup for the token wait timestamp.
- `scripts/direct/test_verify_token_handoff.py` — unset-variable coverage and the visible wait line.

**Binds later work:** Token wait timestamps use the user-zone helper without a variable supplied by the memory module.

**Gotchas:** An inherited test variable can hide a production failure under `set -u`. `fast_track.py` refuses changes to `verify.sh`.

**Ruled out:** Restoring a dependency on `memory_gate.sh` to define `USER_ZONE_SCRIPT`.

### Phase 33 — Two verify test files pass on the builds tip again · status: done

#### As-built

Checkpoint `d8b1b0c3c` on `verify-red-tests`.

- The memory-kill fixture extracts `buildlog_step_compiles()` from `scripts/lint/invoke.sh` with the functions it extracts from `verify.sh`.
- The fixture supplies routing and hold helpers outside its classification scope. Token tests cover admission; existing memory-kill assertions remain unchanged.

**Files:**

- `scripts/direct/test_verify_memory_kill.py` — complete dependencies for the extracted shell functions.

**Binds later work:** Fixtures that extract `run()` must provide its current helper dependencies from their owning modules.

**Gotchas:** Missing fixture helpers produce shell status 127. The offload exclusion mismatch belongs to builds-resources `d70f1dedf`; this checkpoint changes no offload code.

**Ruled out:** Changing production memory-kill behavior or changing offload expectations for an unrelated branch.

### Phase 34 — Remove an owner's standing checkouts when the owner retires or wraps · status: done

#### As-built

Checkpoint `ce12a797f` on `verify-copies-owner-end` (with Phase 18), merged to builds as `d1fc65123`.

- `copies.py remove --owner O` with no `--repo` removes every tree the owner holds, in every repository and kind; `--repo` narrows it. The Python API takes a `RepositoryScope` with the same two forms.
- `retire_unit.py` removes the unit's standing checkouts after it stops the unit's processes and before it writes the retired note. A busy tree refuses the retirement and names the holder's session (a pid only when no name exists) and the tree path.
- `tidy_wrapped.py` removes the standing checkouts of the wrapped production and of each of its units.
- A retry finishes an interrupted removal; a rerun after a full removal changes nothing and exits 0. An adopted borrower keeps its source.
- `lock_holder` lives in `scripts/lib/file_lock.py`, shared by `copies.py` and `sweep.py`.
- `bump_gate.py` sets `BUILDLOG_UNIT` from the bump record, so the build queue names the unit of a bump gate.

**Files:**
- `scripts/copies/copies.py`, `test_copies.py` — owner-wide removal and `RepositoryScope`.
- `scripts/production/retire_unit.py`, `tidy_wrapped.py` and their tests — removal at retire and at wrap.
- `scripts/lib/file_lock.py`, `scripts/lib/scratch.py`, `scripts/lint/sweep.py` — the shared lock holder reader.
- `scripts/production/bump_gate.py` — the unit name for a bump gate.
- `docs/as-built/gate-checkouts.md` — the removal rules.

**Binds later work:** owner removal covers local trees only. Remote mirrors carry no owner yet; the owner removal for remote mirrors uses the same `RepositoryScope` and order.

**Gotchas:** a test that mocks the holder's line read patches `file_lock.open`, not `sweep.open`.

### Phase 35 — Every build copy maker left outside copies.py moves onto it · status: todo

> Builds ruling, 2026-11-03 21:0x EST: copies.py is the one way to copy a repository for a build. When this plan ends, copies.py passes to queue-unit as the build-folder owner.

**Today.** `config/copies.conf` allows three build copy makers outside copies.py: `mac_test/offload.py` and `production/mac_run.sh` (Mac mirrors, including `~/.local/state/mac-test/mirror`) and `direct/verify.sh` (its documentation checkout). The copy guard also misses some import forms (next item "Copy guard: refuse tree-making git calls through every import form", F037), so a copy maker can pass the guard unseen.

**Depends on:** Phase 18; Phase 25 (one mirror root key in mac_test.conf); resources Phase 50 (checkpoint `d70f1dedf`, owner resources-unit) landed in origin/builds. That checkpoint adds `scripts/mac_test/floor_claims.py` and changes `offload.py`, `mac_run.sh` and `retire_unit.py`, the files this phase changes. On 2026-11-04 it is on origin/builds-resources only. `offload.remote_mirror` still builds the legacy Mac path in code until Phase 25 item 2 lands.

**Workers:** one integration writer for the copies API and its callers (`copies.py`, `offload.py`, `mac_run.sh`, `linux_runner.py`, `verify.sh`, `retire_unit.py`, `tidy_wrapped.py` and their tests). One guard writer for `scripts/copies/copy_guard.py` and its tests. The integration writer fixes the copies API (items 1, 7 and 9) before it moves a caller. An independent test lane runs the acceptance tests before the hand-over in item 8.

**Constraints from the phase 29, 34, 36 and 37 review:**

- Keep the Phase 36 token boundary: verify.sh `run()` wraps `run_step` and releases each token it takes when the step ends; the sweep in invoke.sh `sweep_after_step` takes no board token. Keep pre-held tokens, the `.given_back` mark, the memory re-acquire, retry attempt 2 and the pipeline EXIT trap.
- Keep the Phase 29 local repair rule and the Phase 37 step record: a tree-writing step stays on natedev with both remote hooks off, records `natedev_only` for its lifetime, and gives the call its own decision back before the next read-only step.
- The acceptance run adds the verify token, memory-kill and Mac-step suites, `test_verify_lint_local_writes.py`, `scripts/buildlog/test_mac_idle.py` and the Phase 23 cancellation tests (`test_verify_tree_cancel_lint.py`).
- One lock order for remote mirrors: the mirror lease first, then the floor claim lock, then the copy-record lock. Mirror refresh, claim adoption, owner removal and the sweep take them in that order. No sweep starts while a process holds an admission or claim lock.

#### Work Order

1. Add a copies.py kind for a Mac mirror (named, registered, swept like the others). `offload.py` and `mac_run.sh` make and reuse Mac mirrors only through it. Remove their allow-list entries.
2. `verify.sh` makes its documentation checkout through `copies.standing`. Remove its allow-list entry.
3. Fold the F037 next item into this phase: the guard finds tree-making git calls through every import form named there. Remove the item from `docs/plans/natedev-verify-next.md`.
4. The allow list keeps only original work trees (`add_unit.py`, `resume_unit.py`, `add_style.py`, `make_worktree.sh`, `berth_fix/launch.sh`), the external upstream clone (`bevy_migration_ensure_repo.sh`) and copies.py.
5. The remote mirror kind covers every configured remote target, not only the Mac. `offload.remote_mirror` and `offload.copy_tree` also serve Linux and AWS slots, and `linux_runner.probe_script` makes their folders.
6. A remote copy has a lease for its lifetime. `copy_tree --delete` and the sweep never remove a mirror that a live remote run uses, and they keep its metadata.
7. The documentation checkout is a staged tree: verify.sh `doc_tree` writes the index tree, and `copies.commit_at` needs a commit. Add a staged-source form to `copies.standing`. It copies only staged content and leaves the source index and file mtimes unchanged. Name the source and merge policies with types first (architect review finding 7; this replaces the next item "Name copies.py source and merge policies with semantic types"): `CommittedSource`, `WorktreeSource`, `StagedSource`, `NoMerge` and `MergeRevision`. Convert the CLI flags at the boundary; callers and tests change in the same edit. No bare `None` policy argument stays in the copies API. Remove that item from `docs/plans/natedev-verify-next.md`.
8. Hand-over: queue-unit takes ownership of copies.py, its cleanup and the floor interfaces. Queue-unit's separate warm-target copy work is removed.
9. Owner removal reaches remote mirrors. Each remote mirror records its owner (the `RepositoryScope` and owner that Phase 34 uses for local trees); `OffloadRequest` carries it. `copies.py remove --owner O` and the retire and tidy paths remove that owner's remote mirrors in the same order as its local trees. A mirror that a live run holds refuses the removal and names the holder's session and the mirror path.

**Acceptance gate:** the repository scan test finds no build copy maker outside copies.py; one test per item that fails on the old code; tests for a cancelled remote copy, a surviving remote child, kept metadata and a sweep of a busy mirror; concurrent sweep, admission and owner-removal tests in the lock order above, with a cancelled sweep; retire and tidy tests that keep another owner's mirrors and refuse a busy mirror with the holder named; tests for staged-only content, an unchanged source index, kept mtimes, target reuse, recovery and correct pass records. `test_verify_doc_checkout.py` (the `target/verify-doc` path) and `test_verify_doc_recovery.py` (the private index beside the checkout) change only their layout assertions, each named by an `Assertion-Change:` line. Queue-unit confirms the hand-over; the copies, offload, Mac run, Linux runner and verify suites pass.

### Phase 36 — A verify call never waits on its own cargo token · status: done

#### As-built

Checkpoint `272005ed6` on `verify-token-self`, merged to builds as `d79fe5488`.

- invoke.sh `sweep_after_step` runs the detached sweep with `run() { run_once "$@"; }`, so the sweep never takes the board token. Cargo's own locks protect its scan.
- verify.sh `run()` wraps `run_step` and `finish_step_token`. Each token that `run()` takes for a step is released when the step ends: on success, failure, each early return and the check-failed exit. A hold the call had before the step stays.

**Files:**
- `scripts/direct/verify.sh` — the `run()` / `run_step` split and `finish_step_token`.
- `scripts/lint/invoke.sh` — the token-free sweep.
- `scripts/direct/test_verify_token_self_hold.py` — the next local step starts at once after a sweep; no step leaves a hold behind.

**Binds later work:** keep pre-held tokens, the `.given_back` mark, the memory re-acquire, retry attempt 2 and the pipeline EXIT trap across the `run()` boundary.

**Gotchas:** `test_verify_memory_kill.py` copies verify.sh functions into its fixture by name; a new helper that `run()` calls goes on that copy list.

**Ruled out:** a take-over of a hold the call already has (it hides a leak instead of removing it).

### Phase 37 — A tree-writing lint step that stays on natedev is not counted as a waiting Mac step · status: done

#### As-built

Checkpoint `1f9e9c484` on `verify-not-mac-wait`, merged to builds as `a62793f07`.

- A tree-writing step records `natedev_only` ("step writes the tree") before routing, token and memory waits, and keeps it for its whole lifetime. verify.sh `STEP_RECORD_ARGS` saves the call's record arguments; after the step, `run()` gives the call its own decision back, so a later read-only step can go to the Mac again.
- `mac_idle.py` already skips `natedev_only` records, so a local repair raises no false Mac idle alert. `mac_idle.py` has no change.

**Files:**
- `scripts/direct/verify.sh` — `STEP_RECORD_ARGS` and the record restore after `run_step`.
- `scripts/buildlog/test_mac_idle.py` — `test_tree_writing_lint_wait_is_local_and_next_judgment_counts_again`.

**Binds later work:** a tree-writing step records `natedev_only` for its lifetime; the call's decision comes back before the next read-only step.
