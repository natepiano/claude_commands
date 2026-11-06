# verify

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Owner of `verify.sh`, its final pass, its cache, its lint and test scope, its speed and the Mac test code; first change: cut the Python helper start time in each verify run.


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

## Landed outside the phase list

- **Remake.** Phases 1, 7, 9, 10, 13, 14, 16, 20, 23 and 24 and the earlyoom retry came to builds as plain commits on `verify-remake-1-24`. An older as-built that names `natedev-verify-cancel` or another first branch describes the same behavior; the remake commit is the one on builds.
- **Mac retry tests** (`988b6690b`, `3c86b5424`). Mac memory retry tests no longer hang under load.
- **Exported names** (`85eeadbe6`, `efd942d68`). `verify.sh` reads exported names with `env -0`, not `compgen`.
- **Mac claim** (`8f0698cc1`, `b1d37cdb3`). A `verify.sh` call gives back its Mac reservation before each step. A step starts Python for this only when the call holds a reservation.
- **aws hold** (`83646a81a`). `mac_test.read_config` reads the idle settings only for the Mac; `MacTestConfig.idle` is gone.
- **verify help** (`83df23b82`, on `bb4dfb560`). `verify.sh --help` prints a short list. The header keeps its `# Usage:` and `# Help <command>:` blocks and the doc gate sentence.
- **Phase 17 is dropped.** Phase 27 delivered its behavior: `build_targets.py` classifies a temporary decline for retry, `verify.sh` installs the retry route, and a disk decline keeps `NatedevOnly("disk")` while it stays retryable.

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

### Phase 1b — Refuse a step with a failing exit, and read a process start on the Mac  · status: done

#### As-built

Checkpoints `ca6b2e24d` and `57aad3f7f` on `verify-remake-1-24`.

- A step record refusal makes `verify.sh` exit non-zero on every host, also on a Darwin host with no `/proc`.
- `scripts/lib/process_start.py` `process_start(pid)` is the one reader. Linux reads field 22 of `$VERIFY_PROC_ROOT/<pid>/stat`. Darwin reads `ps -p <pid> -o lstart=` under `LC_ALL=C TZ=UTC`.
- Results are `Started`, `Zombie`, `Gone` and `Unreadable`. Results of different kinds never compare equal.
- `step_record.py`, `verify.sh` `STEP_OWNER_PY`, `board.sh` `owned_group_matches` and `cleanup_owned_group`, `mac_test.py` and `copy_watch.py` use the reader and store one start form per host.
- An `Unreadable` start keeps a hold, so a live owner keeps its token. `Gone` and a different start free it.
- Test files extract Bash functions through `shell_functions(path, *names)` in `scripts/direct/shell_function_test_support.py`.

**Files:**
- `scripts/lib/process_start.py`, `scripts/lib/test_process_start.py` — the reader and its results.
- `scripts/direct/test_verify_step_refusal.py` — the recorder refusal exit.
- `scripts/direct/shell_function_test_support.py` — the shared extractor.

**Gotchas:** a writer and a reader with different start forms reclaim a live hold on the Mac. Change all of them together.

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

**Depends on:** queue Phase 10h in `docs/plans/builds-queue.md`, which prints the `holder <json>` event, and queue-unit's work before it on the admission client. sdlc phases-unit Phase 11 (`start-wait --holder-*`, `wait-holder` in `progress_history.py`) is on builds. Hierarchies' wait experiment needs this phase.

#### Work Order

**Source:** sdlc hand-off, approved by builds 2026-10-29.

**Goal:** every wait `verify.sh` records names who holds what it waits on.

**Starting point:** checkpoint `4fa7a8738` on `natedev-verify-holder` holds older work on an older base. Reuse its slot and hold lookups and its test ideas where they fit. Do not reuse its `check` field parsing, its `buildlog_memory_poll_hook` or its background poller: `scripts/lint/memory_gate.sh` no longer parses `memory_admit.py check` fields.

**Spec:**

1. **Flags.** `phase_wait_start` (`verify.sh:1260`) passes the holder to `progress_history.py start-wait`: `--holder-resource-kind slot|memory --holder-resource <slot name | build target | memory> --holder-session <session id>`, or `--holder-unit <unit>` when only the unit is known. All flags or none. None records `HolderNotNamed`.
2. **Memory holder.** `scripts/lint/memory_gate.sh` (near line 178) pipes each line of `scripts/build_queue/admission_client.py --wait-events` to `buildlog_memory_event` (`verify.sh:1302`). Add a `holder <json>` case there. No change goes into `memory_gate.sh` or the admission client.
   - The client prints `holder` after `first`, and again only when the holder changes.
   - `{"kind":"session","session_id":…,"session_name":…}` gives `--holder-resource-kind memory --holder-resource memory --holder-session <session_id>`.
   - `{"kind":"unit","unit":…}` gives `--holder-resource-kind memory --holder-resource memory --holder-unit <unit>`.
   - `{"kind":"none"}` is an explicit absence: `wait-holder` with no flags.
   - An unchanged holder prints no event, so a steady wait starts no process.
3. **Same shell.** The event loop runs in the pipe's subshell, and each wait id is in `$HOLD_WAIT_DIR/phase-wait.<BASHPID>.<key>`. `first`, `holder`, `done` and `left` for one client run use that one subshell. Keep the holder state in that subshell.
4. **Token holder.** In `acquire_token` (`verify.sh:3039`, wait starts near 3072 and 3110) the holder is the board token owner, with kind `slot` and resource `cargo`. Read it in Bash on each pass of the wait loop. Call `wait-holder` only when it changes.
5. **Build hold.** At `verify.sh:2797` and `:3401`, and in the SSH route (`route_test` at `:2294`, wait at `:2420`; `:3279` copies that body to `route_test_without_renewal`, and `:3280` wraps it with the token renewal), the holder is the hold's holder, with kind `slot` and the held target as the resource.
6. **Mac busy.** For the Mac-only wait (`phase_wait_start hold "Mac busy"`), the holder is the session that holds the Mac claim, with kind `slot` and resource `mac`. Name it again on each poll.
7. **Changes.** A changed holder calls `progress_history.py wait-holder --session-dir <dir> --wait-id <id>` with the same flags. A holder that goes away calls it with no flags. No watcher process runs beside a wait. A change during one blocking wait call shows at the next pass. A routing pass that tries a reserved target closes its interval, as a refused Mac claim does in item 9, so the next interval's `start-wait` names the current holder; `wait-holder` applies while an interval stays open.
8. **Recorder failure.** A failed `start-wait` or `wait-holder` prints the existing `NotRecorded` line. It never changes admission or the step's exit. The next `holder` event or pass tries again.
9. **Queue age.** `mem_wait_s` and the waiting lines count from the first queued time in `BUILDLOG_MEM_WAIT_FIRST_FILE`, normally `$HOLD_WAIT_DIR/first-queued-at` (`verify.sh:1984-1989`). Step setup writes that file before routing, so a memory event never creates it. Each recorder interval runs from its own `first` line to its `done` or `left` line. A rejoin after a refused Mac claim opens a new interval and keeps the first queued time. A first check that admits opens no interval and makes no holder call.

**Files:**
- `scripts/direct/verify.sh` — the `holder` case, the lookups and the `wait-holder` calls.
- `scripts/direct/test_verify_wait_holder.py` — new.

**Workers:** 1 integration writer + 1 tester.
- `impl` — `scripts/direct/verify.sh`.
- `test` — `scripts/direct/test_verify_wait_holder.py`, from this Spec alone. It reuses `shell_functions`, `VerifyTokenWaitFixture` and `VerifyHoldWaitFixture`; no second extractor or fixture copy.

**Constraints from prior phases:**
- Add no guard, refusal or check that this Spec does not ask for.
- Read `scripts/lint/memory_gate.sh`, `scripts/build_queue/admission_client.py` and `scripts/direct/progress_history.py` only; do not change them.
- Phase 1: an inline helper in `verify.sh` runs as `VERIFY_PY_HELPER=<NAME> "$PY" …`, so a `VERIFY_PY_TRACE` run names its start.
- Phase 9: a step with no wait starts no new Python. `test_verify_python_starts` keeps its count.
- Phase 14: a `--mac` wait polls with a Bash `sleep` held in `HOLD_WAITER_PID`, and `hold_interrupted` ends that `sleep`.
- Phase 23: a group TERM of a lint run exits 143, repeated INT, TERM and HUP during cleanup are ignored, and no verify process, admission place or step record remains.
- Phases 7, 28, 31 and 36: token ownership and the step token boundary stay as they are.
- Phases 29 and 37: a tree-writing step stays on natedev and records `natedev_only` for its lifetime.
- Phase 38: keep Bash state publication and cleanup. Read a step's state, target, `updated` and run location through `step_record.read`. A fixture that hides tools names `cmp` in its tool list.
- verify help: a `verify.sh` header edit keeps the `# Usage:` and `# Help <command>:` blocks and the doc gate sentence.

**Acceptance gate:**
- Through the real event pipe, the recorded wait shows: a holder of each kind; a holder change; an explicit absence; a recorder failure and its retry; a rejoin after a refused Mac claim, with a new interval and the same first queued time; and an immediate admission with no interval and no holder call.
- The recorded wait names the token owner, the hold's holder and the Mac claim's session, and a change on the next pass.
- `test_verify_step_state.py`, `test_verify_tree_cancel_lint.py`, `test_verify_python_starts`, the verify token, memory-kill and Mac-step suites and `test_verify_lint_local_writes.py` pass.
- basedpyright 0/0/0 on the changed Python files; merge gate `--tests-only` green on a tree merged with origin/builds.

### Phase 8a — Resolve the token holder from recorded identities  · status: todo

**Depends on:** Phase 8.

#### Work Order

**Source:** Phase 8 review findings F009 and F010, routed under the landing rule at repair round 7, with the Astra review of 2026-11-06.

**Goal:** a recorded token wait names the token owner on a current run, for the director and for each worker record form. Phase 8's acceptance line "the recorded wait names the token owner" stays open until this checkpoint: a director-held token and a slot with only a saved worker record do not name a session after Phase 8.

**Spec:**

1. **Run owner.** The `director` token owner and the unit fallback resolve through `unit_runs.run_owner` and the staff record. No production writer creates a `director` file, and `_set_owner` removes the state's `unit` field when it stores `owner_staff_id`. Remove both reads.
2. **Worker identity.** Read the writers of `locks/cargo.d/holder`, `mesh_roster.json` and `impl_worker_*.json`, and state in the summary which forms they write. Resolve through the recorded `staff_id` and the staff record. An exact mapping wins: a roster key equal to the owner, then an `impl_worker_*` record whose `slot` or file stem equals the owner, then a roster entry whose `slot` equals the owner. Keep a `-<owner>` suffix match only if a production writer needs it; `fixture-first` in `verify_token_wait_test_support.py` is test setup, not that evidence. More than one match at one step gives no session.
3. **Occupied slot, no answer.** A lookup that finds no holder for an occupied slot records no holder and tries again on the next pass. An absent holder and the slot `unknown` need no lookup. A steady named token wait starts no process.

**Files:**
- `scripts/direct/verify.sh` — the token branch of `phase_wait_holder_flags`, and `phase_wait_start`.
- `scripts/direct/test_verify_wait_holder.py`

**Workers:** 1 integration writer + 1 tester.

**Constraints from prior phases:**
- Add no guard, refusal or check that this Spec does not ask for.
- The constraints in Phase 8 apply. Token ownership and the step token boundary stay as they are.

**Acceptance gate:**
- Tests through the real `phase_wait_start`, in one fixture set: a director-held token with `owner_staff_id` and no `director` file; a saved worker record with no roster entry; an exact match beside a second suffix match; a worker with no session, which gives the owner row's unit; an occupied slot that resolves on a later pass.
- `test_verify_python_starts`, the verify token suites and `test_verify_wait_holder.py` pass.
- basedpyright 0/0/0 on the changed Python files; merge gate `--tests-only` green on a tree merged with origin/builds.

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

### Phase 16 — Raise a red alert when the Mac takes no work while steps wait  · status: done

#### As-built

Checkpoint `ee03d190c` on `verify-remake-1-24`; Phase 24 adds the disk exclusion.

- `mac_idle.watch_once` is the one check. The `buildlog sample` pass on natedev calls it every 60 s.
- `mac_work(paths, now)` returns `NatedevJobOnMac`, an idle state, `MacUnreachable` or `MacStateUnreadable`. It reads local files only and never reads the Mac's load.
- Each pass writes `<state>/alert.json`. `scripts/buildlog/mac_idle_alert.py` owns the path, the file form and the line text; `MacIdleConfig` holds the alert, push and stale times.
- `/build:queue` and `/build:status` show the red line first. A missing file adds nothing, and a stale file shows the stale line.
- A waiting step counts when its record is live, `MacAllowed` and in `waiting_hold` or `waiting_mac`.
- After the alert time the builds showrunner gets one message. After the push time the user gets one phone push per outage. `mac_idle.py cleared --since <float>` is the push's clear check.
- `mac_test.py` `work_idle_since` marks a claim whose Mac runs no build or test.
- An admission query error does not stop the watch.

**Files:**
- `scripts/buildlog/mac_idle.py`, `mac_idle_alert.py`, `test_mac_idle.py` — the watch, the saved result and the outage tests.
- `scripts/build_queue/build_queue.py`, `test_build_queue.py`, `scripts/build_targets/build_targets.py`, `test_build_targets.py` — the top line in each view.
- `scripts/notify/push_catalog.py` — `mac_idle.blocked` and `mac_idle.stuck`.

**Binds later work:** `natedev_only` records do not count as waiting Mac steps. Idle means no natedev job of ours on the Mac, never the Mac's load.

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

### Phase 25 — Run the Mac floor sweep on a disk exclusion, then check again  · status: done

#### As-built

Checkpoint `926453c07` on `verify-remake-1-24`.

- `offload.ensure_mac_disk_floor(controller, config, repository, probe, target="mac", slot=1)` returns `MacDiskReady`, `SweepShort` or `SweepFailed`. Under the minimum it runs one shared floor sweep, then reads free space again.
- One sweep runs per host, under a `floor-sweep-<host>.lock` file in the offload state folder. A run that finds a sweep running waits for its result.
- `config/mac_test.conf` `mirror_root` is the one spelling of the Mac mirror root. `scripts/mac_test/mirror_root.py` reads it for `offload.py` and `sweep.py`.

**Files:**
- `scripts/mac_test/offload.py`, `scripts/mac_test/mirror_root.py`, `config/mac_test.conf` — the sweep call and the root.
- `scripts/mac_test/test_floor_sweep.py` — concurrency, cancellation, failure, timeout and minimum-space tests.

**Binds later work:** `mac_test.free_now` reads local state only. Never hold an admission or claim lock while a sweep runs.

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

### Phase 35 — Every build copy maker left outside copies.py moves onto it · status: done

#### As-built

Checkpoints `f81a7ba55` and `3aea5698d` on `verify-remake-1-24`, landed with the Mac claim and aws hold fixes as `be7f4d47f`.

- `copies.py` is the one maker of build copies. The `config/copies.conf` allow list keeps only original work trees, the external upstream clone and copies.py. A repository scan test finds no other maker.
- `RemoteMirror(owner, repository, host, path, session)` is a registered copy on any remote target: Mac, Linux or AWS. Records and lease files are under `<gates root>/.remote-mirrors`. `offload.py`, `linux_runner.py` and `mac_run.sh` make and reuse mirrors only through `copies.py` (`mirror-receive`, `mirror-use`, `mirror-remove`, `register-remote`).
- A remote copy has a lease for its lifetime (`remote_lease`, `BusyRemoteMirror`). The lock order is the mirror lease, then the floor claim lock, then the copy-record lock. The floor sweep skips a busy mirror, and `copy_tree --delete` keeps the mirror's metadata.
- `copies.py remove --owner O`, retire and tidy remove an owner's remote mirrors in the order of its local trees. A busy mirror refuses and names the holder's session and the mirror path. An empty owner removes no mirror.
- `.<name>.copy-removing.json` keeps removal authority across an interrupted removal until the replacement record exists.
- `verify.sh doc` takes its checkout from `copies.py hold` with `StagedSource`. It copies only staged content and leaves the source index and file mtimes unchanged. The owner holds the lease until the verify shell closes its input.
- `CommittedSource`, `WorktreeSource`, `StagedSource`, `NoMerge` and `MergeRevision` name the source and merge policies. The CLI converts its flags at the boundary.
- `copy_guard.py` binds every import form in one scope model. It scans deferred functions with a work queue of (function, execution) pairs, so call order does not hide a tree-making call.

**Files:**
- `scripts/copies/copies.py`, `copy_guard.py`, `test_remote_copies.py`, `test_review_remote_copies.py`, `test_copy_guard.py`, `remote_test_support.py` — the API, the guard and their tests.
- `scripts/mac_test/offload.py`, `linux_runner.py`, `floor_claims.py`, `mac_test.py` — remote callers and `HeldMirror`.
- `scripts/production/mac_run.sh`, `retire_unit.py`, `tidy_wrapped.py` — the clone registration and owner removal.
- `scripts/direct/verify.sh`, `test_verify_doc_checkout.py`, `test_verify_doc_recovery.py` — the documentation checkout.
- `docs/as-built/mac-test-offload.md`, `docs/as-built/rustdoc-once-and-final-targets.md` — the mirror and documentation rules.

**Binds later work:**
- Queue-unit owns `scripts/copies/`, its cleanup, `scripts/mac_test/floor_claims.py`, the lock order and the `lint/sweep.py` callers once this phase is on GitHub builds. Queue-unit holds no separate warm-target copy work.
- `lint/sweep.py` stays under the builds file freeze.
- `copies.py list` shows local standing checkouts only and skips `.remote-mirrors`.
- Moving `.remote-mirrors` changes each lock path while a live process can hold the old inode.

**Gotchas:**
- Direct, production and shutdown tests read the live notifier folder. Main's timer migration rewrites those confs, and builds without it refuses them. Set `NOTIFIER_STATE_DIR` to an empty folder until sdlc's test isolation fix is on main.
- `python3 - <<EOF` with expanded stdin is refused by the guard. Write the script to a file first.

**Ruled out:** taking the claim lock only in `mirror-remove` and claiming a slot before its lease (both change the lock order that mirror removal safety needs); one lease layer per mirror; one owner spelling written at record time; dropping `OffloadRequest.repository_scope`.

### Phase 35b — Simplify the copies remote mirror code · status: done

#### As-built

- **Discovery.** `mirror_sweep_leases` globs `*/.mac-test-mirror` and `*/*/.mac-test-mirror`, and finds `.*.copy-removing.json` authority files in the same two layouts, so an interrupted removal stays visible. It runs on the Mac mirror root `~/.local/state/mac-test/mirror` (`sweep.py:4332`); natedev has no such root. A pass takes under 0.001 s (from 0.95 s).
- **Process scan.** `scan_mirror_processes(mirrors) -> MirrorProcessScan` uses `tmp_sweep.lsof_paths` and `process_paths`. `mirror_has_process(tree, processes)` matches the mirror path or a descendant, and an unavailable scan means busy. `lsof_paths` accepts output only with status 0 or 1. `ProcessUsesKnown(paths, unreadable_cwd_pids=())` keeps the process-group rule for a process that cannot be read. `mirror_lease_set` takes all candidate leases, scans once, classifies while the leases are held and releases the busy ones. A Mac call on the hana mirror takes 0.22 s (from 5.2 s).
- **Locks.** `local_mirror_lease(tree, session, *, create=True)` is the lease; `mirror_metadata_locks(tree)` takes the claim lock, then the record lock. `mirror_remove(args)` and `mirror_run(args)` replace `mirror_action`. `receive_metadata(tree, args)` writes received metadata. `file_lock.device_key` and `held_flock_keys` read `/proc/locks`.
- **Records.** `mark_mirror(tree, owner, repo, session, **extra)` is the one writer. `floor_claims.read_mirror_record(path)` is the one reader, with `mirror_local_path`, `mirror_lock_path`, `removal_authority_path`, `record_lock_path` and `MIRROR_RECORD`, `MIRROR_SESSION`, `MIRROR_METADATA`. `remote_rsync` adds the `--exclude` entries for the metadata names.
- **Remote registry.** `remote_lease_path(mirror)`, `held_remote_mirrors()` (one `/proc/locks` read, `HeldMirror` values) and `remote_copies_command(action, mirror, *tail)`. `register-remote` requires `--owner` and `--repository`; `unregister-remote --host --path` removes a record. `mac_run.sh` removes the natedev record and the Mac `localhost` record after a successful clone cleanup, and keeps both after a failed one. `mirror_probe_setup` tests the mark first, so a marked mirror starts no remote Python.
- **Staged source.** `StagedSource.tree` is mandatory. `hold` computes `staged_tree(repo)` and prints the tree and the checkout; `doc_checkout` reads both lines. `run --commit` is required. `staged_tree` copies the source index in a `build_folders.temporary_build_folder` with prefix `copies-index`.
- **Owner and config.** `copies.unit_owner` is the one suffix helper (`alpha_unit` gives `alpha_unit-unit`), used by `tidy_wrapped.py`. `fallback_owner("verify")` reads `owner.verify=verify`. `OffloadRequest` requires `owner` and `holder_session`; the session comes from `build_hold.owning_session()`. `target_config.slot_capacity(values, repository, *, missing)` is the one reader for `mac_test` and admission; a bad or empty value gives `ConfigError`. `prefixes.verify-doc` is gone from `config/build_folders.conf`, and the `allow.*` rows for `mac_test/offload.py`, `production/mac_run.sh` and `direct/verify.sh` are gone from `config/copies.conf`.
- **Removed.** `MirrorRepository`, `RegisteredRemoteMirror`, `RemoteMirrorLeaseRegistry`, `ScanMirrorProcesses`, `MirrorLocks`, `finish_updates`, the `mirror-probe` action, `run --staged`, `run --staged-tree`, `hold --staged-tree`, `hold --owner`, `register-remote --repo`, `lease.close()` in `offload.py` and `CommandEvent.transport_args`.

**Files:**
- `scripts/copies/copies.py` — mirror discovery, scan, leases, locks, records and the remote registry.
- `scripts/mac_test/floor_claims.py` — mirror record and lock paths, and the record reader.
- `scripts/mac_test/offload.py` — admission, slot capacity reader use, holder session and `remote_rsync` excludes.
- `scripts/mac_test/mac_test.py`, `scripts/build_targets/target_config.py` — the one slot-capacity reader.
- `scripts/lint/tmp_sweep.py` — `lsof` status rule and `unreadable_cwd_pids`.
- `scripts/lib/file_lock.py` — `device_key`, `held_flock_keys`.
- `scripts/production/mac_run.sh`, `scripts/production/tidy_wrapped.py` — record removal; owner helper use.
- `scripts/direct/verify.sh` — reads the `hold` tree and checkout lines.
- `config/copies.conf`, `config/build_folders.conf` — owner row, `copies-index` prefix.
- `docs/as-built/gate-checkouts.md`, `docs/as-built/mac-test-offload.md`, `docs/as-built/rustdoc-once-and-final-targets.md` — current mirror and checkout facts.
- `scripts/copies/copies_test_support.py`, `scripts/mac_test/offload_test_support.py` (`mirror_use_payload(case, argv)`) — test support.
- `scripts/copies/test_copies.py`, `test_remote_copies.py`, `test_review_remote_copies.py`, `remote_test_support.py`; `scripts/mac_test/test_linux_runner.py`, `test_offload.py`, `test_offload_cleanup.py`, `test_offload_forward.py`, `test_target_slots.py`; `scripts/production/test_mac_run.py`, `test_retire_unit.py`, `test_tidy_wrapped.py` — tests.

**Gotchas:** `lsof` exit 1 means no match, and any status above 1 is an unavailable scan. `copies.py` imports `tmp_sweep` inside its two scanner functions, with the top-level import under `TYPE_CHECKING` only.

**Ruled out:** moving the `mac_run.sh` shell lease to the `hold` coproc (`flock` waits and `remote_lease` refuses at once, so it adds a refusal); moving `.remote-mirrors` out of the pool (it splits lease exclusion for a process that holds the old inode).

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

### Phase 38 — Publish a step state without starting Python · status: done

#### As-built

- `verify.sh` starts `step_record.py` only for `start` and its first `write --report`. Every state publication, tree-writing steps included, and the record removal run in Bash. The JSON record does not change after that write, and `step_record.py` is the only code that knows its schema.
- `<call id>.state` sits beside `<call id>.json` and holds one line: a state word, and a target when the state has one (`on_ssh aws`), or `writes_tree <state>` during a tree-writing step. `step_record.state_path(call_id)` owns the name.
- `publish_step_state <word> [target]` compares the record bytes with `STEP_RECORD_TEXT` through `cmp -s`. It refuses with `step record: cannot update step record: record missing or changed; step refused` and exit 2. It then writes `.step-<state file name>-$RANDOM` and moves that file over the state file.
- `remove_step_record` removes the record, the state file and every `.step-<state file name>-*` file, in that order.
- `step_record.read` is the one join point. It opens the state file before the JSON read and keeps it open through the join. No file means no publication. `updated` is the file's modification time. `writes_tree <state>` gives `runs_on` `natedev_only` and joins `<state>` by the normal rules. An unknown word gives `UnreadableRecord`.
- `step_record.py write --report` prints the offset, the record path and the state file path. The `state` and `remove` verbs, `--clock-offset`, `STEP_RECORD_ARGS`, `read_fields` and `state_fields` do not exist.
- Measured `step_record.py` cost: starts 255 to 26 and CPU 12.88 to 1.25 s on `test_verify_token_renew.py`; starts 486 to 58 and CPU 24.09 to 3.08 s on `test_verify_lint_cache_versions.py`.

**Files:**
- `scripts/direct/verify.sh` — `publish_step_state`, `step_state`, `remove_step_record`, the saved values.
- `scripts/direct/step_record.py` — `state_path`, `--report`, the join in `read`.
- `scripts/direct/test_verify_step_state.py` — real-path publication, refusal, read-back and cleanup tests.
- `scripts/direct/test_step_record.py`, `scripts/direct/test_step_record_cli.py`, `scripts/direct/test_verify_tree_cancel_lint.py`, `scripts/buildlog/test_mac_idle.py`, `scripts/aws_target/test_control.py`, `scripts/direct/test_verify_ssh_targets_results.py`, `scripts/direct/verify_lint_scope_test_support.py` — join tests and fixtures.
- `docs/as-built/build-memory-admission.md` — how a step state is published and read.

**Binds later work:** a test reads a step's state, target, `updated` or run location through `step_record.read`, never from the JSON file. A fixture that hides tools names `cmp` in its tool list.

**Gotchas:**
- A Bash text read drops NUL bytes, so the record comparison uses `cmp`.
- `run_once` publishes in the step subshell, so a parent and a child can both publish during an interrupt. The temporary name is therefore not fixed, and cleanup removes it with a glob.
- `hold_enforcer.py:228` stops a step only when its state is `on_natedev`, so a tree-writing compile keeps its real state through `writes_tree <state>`.
- The Bash write has no fsync. After a crash the pid is dead and readers ignore the record as stale.

**Ruled out:**
- Editing the JSON record in Bash: a state file keeps the schema in `step_record.py` alone.
- A check that a state file left by an old call overrides a new record: each record write gets a new call id.

### Phase 39 — The first lint judgment's repair decision has one callable home · status: done

#### As-built

- `first_judgment_permits_repair <status> <log path>` in `verify.sh` returns 0 when a repair is permitted: `HOLD_LINT` is 0, the status is 101, `RUN_LAST_ATTEMPT_KILLED` (default 0) is 0, and the log has no `process didn.t exit successfully:.*signal:` text. It starts no process other than `grep`.
- The first-judgment step calls the function with `"$clippy_status"` and `"$CLIPPY_LOG"`, and sets `clippy_repair=1` when it returns 0. The read of the `lint-held` file stays at the call site, because it sets `HOLD_LINT` for later steps.
- `FirstJudgmentRepairTests` in `test_verify_lint_judgment.py` runs the function in `bash` with a log in a temporary directory and no `VerifyLintCacheFixture`. `test_first_judgment_nonlint_status_never_repairs` runs statuses 1 and 143 through the real `verify.sh`.
- Measured `test_verify_lint_judgment.py` over two paired runs: wall 148.8 to 107.4 s, CPU 95.1 to 83.9 s, with noise from a load of 31 to 77.

**Files:**
- `scripts/direct/verify.sh` — the function and its call site.
- `scripts/direct/test_verify_lint_judgment.py` — `FirstJudgmentRepairTests` and the two-status end-to-end test.
- `scripts/direct/test_verify_python_starts.py` — the lint trace total is 24.

**Gotchas:**
- A status above 128 enters `step_stopped_by_hold` in `run_step`, so an end-to-end status table needs one such status.

**Ruled out:**
- One end-to-end status (1): it drops the only real-path test of the hold-stop checks.

### Phase 40 — A verify step reads each target's platform from the eligibility answer · status: done

#### As-built

- `build_targets.py eligible` prints a fifth tab-separated field, `platform`, on each target row. The `platform` command does not exist.
- `verify.sh` reads `platform` in each row loop. `target_platform` reads `STEP_TARGET_TABLE` in the same shell, and starts no process. When no row answers, it prints `macos` for a target named `mac` under `mac_allowed`, and otherwise prints nothing.
- The validation loop in `initialize_step_record` reads a sixth name, `extra`, and takes the `invalid targets` refusal when `extra` is not empty. So a settings path that holds a tab takes the same refusal, exit 2.
- Measured `test_verify_ssh_targets_routing.py` over two runs: `build_targets.py platform` starts 69 to 0, CPU 73.6 to 68.3 s, wall 93.7 s to 96.7 and 152.5 s, with noise from a load of 47 to 57.

**Files:**
- `scripts/build_targets/build_targets.py` — the fifth field.
- `scripts/direct/verify.sh` — the row loops, `target_platform` and the validation loop.
- `scripts/direct/test_verify_target_platform.py` — direct tests of `target_platform`.
- `scripts/direct/test_verify_checks_fail_closed.py` — the tabbed settings path refusal.
- `scripts/direct/test_verify_step_state.py` — the initial record's `mac_targets` list from a `macos` row and a `linux` row.
- `docs/as-built/build-targets.md`, `docs/as-built/mac-test-offload.md` — the row form.

**Gotchas:**
- `read` with five names puts the second part of a tabbed settings path in `platform`, so `settings` holds no tab. Only a sixth name can detect the extra field.
- `STEP_TARGET_TABLE` is not exported, so `target_platform` answers only in `verify.sh`'s own process. Pinned routing replaces the table with a pinned-only table at `route_test`.

**Ruled out:**
- A test for the read in the `MAC_ONLY` loop: `register_mac_wait` repeats the same check.

### Phase 41 — Test Mac arguments by direct calls, and measure the verify test set  · status: done

#### As-built

- `NextestArgumentTests` in `test_verify_mac_arguments.py` calls the real `compose_nextest_args` and the metadata helpers in one Bash process, for a plain package, a filter and a target.
- The file keeps three real `verify.sh` runs: one Mac decline that runs locally, one real offload busy result, and one plain-package `no_tests` result. `route_test` needs admission, reservations and hold decisions, so a direct call of the `no_tests` fallback needs a stand-in for the routing decision.
- The `no_tests` run reads `--no-tests=warn` at the Mac runner and at the local run, and counts one call of each.
- Measured the file with `strace`, following each child: CPU 56.0 to 30.2 s, processes 1,925 to 982, wall 62.1 to 34.0 s.
- `docs/plans/natedev-verify-test-cost.md` holds the method, the per-file rows and the baseline: 1,618.6 median CPU s over 122 files, largest first, and 22 files with no gate record.

**Files:**
- `scripts/direct/test_verify_mac_arguments.py` — the direct tests and the three kept runs.
- `docs/plans/natedev-verify-test-cost.md` — the method, the rows and the baseline.

**Gotchas:**
- A check on a composed value proves nothing about the value that a consumer receives.
- A result word with its own branch in `route_test` or `remember_mac_retry` needs a check that the following step ran.
- One real `verify.sh` run costs about 8 CPU s and about 300 processes in this fixture.
- Some small files use much CPU: `test_verify_mac_retry_hold.py` has 28 lines and 3 tests, and its cost is in `VerifyMacOffloadFixture`.

### Phase 41a — Test the other verify files by direct calls, largest CPU first  · status: done

#### As-built

- Six of ten test files call real functions directly for checks that do not need a full `verify.sh` run. The batch fell from 340.7 to 313.5 CPU s, measured once each on a copy. Wall s did not fall.
- `call_mac_functions` calls the real result and retry functions with private tools and records.
- `run_unblocked_step`, `invoke_taplo_at` and `start_owned_step` call their real steps directly.
- Largest savings in CPU s: `test_verify_lint_taplo.py` 9.0, `test_verify_hold_wait.py` 8.5, `test_verify_mac_retry_clock.py` 7.7.
- Four files keep all their real runs, because each run proves something that no direct call can.

**Files:**
- `scripts/direct/verify_mac_offload_test_support.py` — `call_mac_functions`, `check_routing_idle_hook`.
- `scripts/direct/verify_token_wait_test_support.py` — `start_owned_step`.
- `scripts/direct/verify_hold_wait_test_support.py` — `run_unblocked_step`.
- `scripts/direct/verify_lint_scope_test_support.py` — `invoke_taplo_at`.
- `scripts/direct/test_verify_hold_wait.py`, `test_verify_lint_taplo.py`, `test_verify_mac_results.py`, `test_verify_mac_retry_clock.py`, `test_verify_mac_retry_hold.py`, `test_verify_token_renew.py` — the moved checks.
- `docs/plans/natedev-verify-test-cost.md` — the per-file table of the batch.

**Binds later work:** natest owns the other `test_verify*` files. A new test for a production change goes in its own new file.

**Gotchas:**
- `check_temporary_mac_decline` and the memory and token idle checks keep their full `verify.sh` runs. A direct call of the deadline check passed with the check removed from `route_test`.
- A mutation that removes a retry deadline ends in the 600 s hang guard, not in an assertion.
- Phases 35, 35b, 38, 39 and 40 landed inside the phase 41 commit `0ed5b347f` and have no separate phase records.

**Ruled out:**
- A direct call of the retry deadline check: it does not pass through `route_test`.

### Phase 41b — Run verify.sh check on a workspace with no library, and name the lasting tree path in the refusal  · status: done

#### As-built

- `verify.sh check` sends `--lib` only when a workspace member has a library target. A workspace of binaries passes. A mixed workspace keeps `--lib`.
- The refusal of a bare `git worktree add` names `make_worktree.sh` for a lasting tree. `/unit:direct` names the same script.
- Live check on a tempdir workspace with no library: the old `verify.sh check` exits 101 with `no library targets found`, and the new one exits 0.

**Files:**
- `scripts/direct/verify_metadata.py` — `package_has_library`, `check_targets`, the `check-targets` request.
- `scripts/direct/verify.sh` — `take_check_targets`, the check verb's selection.
- `scripts/direct/test_verify_check_no_library.py` — a real no-library run, and the mixed workspace selection.
- `scripts/copies/copy_guard.py` — `REFUSAL` text.
- `scripts/copies/test_copy_guard.py` — the assertion on both paths.
- `commands/unit/direct.md`, `docs/as-built/gate-checkouts.md` — one sentence each.

**Gotchas:**
- Metadata is `cargo metadata --no-deps`, so `packages` holds only workspace members. A dependency's library does not count.

**Ruled out:**
- A change to a guard decision or the allow list: the builds showrunner ruled that this phase is the text and one line only.

### Phase 41e — Answer not free for a target whose last probe failed  · status: todo

**Depends on:** Phase 41b, and aws's `offload.py` work on `builds`.

#### Work Order

**Source:** builds showrunner, 2026-11-06: defect from hana (hana-migration). A nateroids nextest left its natedev memory wait twice for aws, aws failed each time, and the build started about 50 min after submission. Builds accepted this phase with one rule: a good probe clears the mark without a build waiting on that target, through the existing probe path, with no new timer.

**Goal:** a step in a natedev memory wait never leaves for a target whose last probe failed. A fixed target returns by itself, at the next step that starts.

**Causes found:**
- A probe or stream version mismatch writes no record that `free_now` reads. `free_now` keeps answering free.
- A failed reach writes a backoff of 300 s, and the backoff ends while the target is still bad. Hana's second leave came at 4 min 56 s.
- The `could not reach` line prints all of the remote stderr, a Python traceback included.

**Base:** aws's commit f0850070b adds `version-decline-<repository>` in `offload.py`: `run_offload` writes it on a mismatch against `box-tools.json`, and removes it on a match. This phase starts from `builds` after that commit lands, in its own tree, because it reads that record.

**Spec:**

1. **A version mismatch is per repository.** Tools depend on the tree, so one repository's mismatch says nothing about another. The probe's `detail` branch and the stream path's `ToolVersionMismatch` branch write the existing `version-decline-<repository>` record, through one function shared with `run_offload`. Do not write the timed backoff here: it stops every repository on that target.
2. **A failed reach is per target.** A new file `failed_probe` in `state_directory(target)`, beside `unreachable_until`, holds no time. Read it through a typed function in the form of `read_backoff`, with a state type in the form of `BackoffState`.
3. **The probe owns the target mark.** `probe_mac` is the one writer. An `UnavailableProbe` result with no `detail` writes the mark, from each of its return points. A good `ProbeSnapshot` removes it. `UnresolvedSlotStartup` and `InterruptedTargetStatus` leave it as it is.
4. **`free_now` answers not free.** Add `"bad"` to `FreeNowAnswer`. `free_now` returns `"bad"` where it would return `"free"` when the target mark exists, or when the record for this repository exists. The record's age does not matter: only a match removes it. Each earlier answer keeps its place, and the function stays local: no SSH, no lock. `free-now` exits 1 on `"bad"`, so each leave hook and the `reserve` form of the retry hook answer no. Make no change to `verify.sh`.
5. **A new step still tries.** A new step's own try is the existing probe path that clears the marks. `ssh_target_eligibility` and the `reserve` command in `build_targets.py` accept `"bad"` as they accept `"free"`, and go on to their reach check and claim. The timed backoff keeps its current effect on new steps.
6. **One line in the log.** The `could not reach` reason is the last non-empty line of the remote stderr. A traceback never reaches the log.
7. **Status tells the truth.** `/build:status` (`targets.py` `target_state`) and `mac_test.py status` report a target with the mark as unreachable, not free.
8. **Docs.** Update `docs/as-built/mac-test-offload.md`: the probe, the decline list and the two records.

**Not in this phase:** the toolchain on the aws box (aws owns it), a new timer, an SSH call from `free_now`, a change to `mac_idle.py`, a change to the controller's `version_decline_hold_s`.

**Remaining risk, stated to builds:** a fixed target stays unused by steps already in the memory wait until the next step starts and probes it. With no new step, those steps run on natedev in their queue place, as they would with no target.

**Files:**
- `scripts/mac_test/offload.py`
- `scripts/mac_test/mac_test.py`
- `scripts/build_targets/build_targets.py`
- `scripts/build_targets/targets.py`
- `scripts/mac_test/test_failed_probe_mark.py`
- `docs/as-built/mac-test-offload.md`

**Workers:** 1 writer: fix16.

**Constraints from prior phases:**
- Add no guard, refusal or check that this Spec does not ask for.
- A test project is made in a tempdir at run time. No test commits one.
- Tests use a temporary state folder. Live state stays out of worker prompts.
- natest owns the `test_verify*` files. Put new tests in the new file.
- A merge commit on this branch carries no visible Change-Review line.

**Acceptance gate:**
- The hana sequence, in `test_failed_probe_mark.py`, through the real `probe_mac`, `free_now`, `free-now` and `reserve` with a fake SSH: a mismatch probe, then the end of the timed backoff, then `free-now` still exits 1 with `bad` for that repository and exits 0 for another. A failed reach, then the end of the timed backoff, then `free-now` exits 1 with `bad` for each repository. A good probe by a new step removes the mark, and `free-now` exits 0 with no waiting step involved.
- `busy`, `blocked` and `off` keep their answers with the mark set. A remote stderr with a traceback prints one `could not reach` line that holds no `Traceback`.
- Each case fails when the matching code is removed. Show this on a tempdir copy.
- `test_mac_test.py`, `test_offload.py`, `test_ssh_eligibility.py`, `test_build_targets.py`, `test_targets.py`, `test_verify_ssh_targets.py` and `scripts/aws_target/test_control.py` pass.
- basedpyright 0/0/0 on the changed Python files. The merge gate runs all modules.

### Phase 41c — Send a lint that waits for memory to a free Mac  · status: todo

**Depends on:** Phase 41b.

#### Work Order

**Source:** builds showrunner, 2026-11-06: the accepted lint item, and natest request d76b7136.

**Goal:** a lint that waits for memory at its first step uses a free Mac and does not wait on natedev. The lint sequence becomes functions that a test can call.

**Spec:**

1. **Function move first.** Move the lint judgment block and the main lint sequence of the `lint` case in `verify.sh` into functions callable through `shell_functions`. The move changes no behavior. natest's `test_verify_lint_judgment.py` and `test_verify_lint_scope.py` use these functions.
2. **Leave the mend wait for a free Mac.** At the memory wait of the `mend --fix` step, a free Mac takes the lint onto the path that it takes under a hold: `lint-held`, `HOLD_LINT=1`, local fmt, then `mend --fail-on-warn` and clippy judgment on the Mac. Use the existing hold path and the existing retry hook. Add no new decision point. `step_writes_tree` still keeps every writing step on natedev.
3. **A clean tree ends there.** The lint exits 0 with no `mend --fix` run.
4. **A finding returns to natedev.** On a finding from the Mac, the lint returns to the natedev queue, and runs `mend --fix`, as a clippy finding does now.
5. **Linux-only repositories stay.** A repository under `linux_only.<repo>` in `config/mac_test.conf`, hana for one, keeps its natedev path.
6. **Docs.** Update `docs/as-built/mac-test-offload.md`: its text says that lint steps keep their local path outside a hold.

**Risk accepted by the builds showrunner:** a Linux-only lint error can appear later, at the merge gate. The user accepted this on 2026-10-28 for a lint during a hold, and this item widens it.

**Files:**
- `scripts/direct/verify.sh`
- `scripts/direct/test_verify_lint_free_mac.py`
- `docs/as-built/mac-test-offload.md`

**Workers:** 1 writer: merge16. The director measures the Mac-idle minutes from the build index.

**Constraints from prior phases:**
- Add no guard, refusal or check that this Spec does not ask for.
- A test project is made in a tempdir at run time. No test commits one.
- natest owns the other `test_verify*` files and may edit the support files after a36296ed2 lands. Put new tests in the new file, and import the support files. Name in the notice any support file that this phase must change.
- A direct test reads a value where its consumer receives it. A retry or hold transition keeps a real `verify.sh` run.
- This phase lands on `verify-plain`. A merge commit on this branch carries no visible Change-Review line.

**Acceptance gate:**
- Real `verify.sh lint` runs with the existing fake tools, in `test_verify_lint_free_mac.py`: a memory wait with a free Mac and a clean tree runs no `mend --fix` and exits 0; a Mac finding runs `mend --fix` on natedev; no free Mac leaves the path as it is; a `linux_only` repository never leaves. Each case fails when the matching code is removed. Show this on a tempdir copy.
- `test_verify_lint_judgment.py`, `test_verify_lint_scope.py`, `test_verify_tree_cancel_lint.py` and `test_verify_python_starts.py` pass.
- The notice names the Mac-idle minutes this could have saved, measured from the `mac_idle` and `steps` tables, or says why they cannot be measured.
- basedpyright 0/0/0 on the changed Python files. The merge gate runs all modules.

### Phase 41d — Move the test argument parser to one function  · status: todo

**Depends on:** Phase 41c.

#### Work Order

**Source:** natest request 4537b50f, 2026-11-06, for `test_verify_mac_only.py` (38 policy cases).

**Goal:** a test can call the `test` argument parser of `verify.sh` through `shell_functions`.

**Spec:**

1. Move the routing-flag loop at the top of `verify.sh` (it starts under `# Remove routing flags before constructing command words and records.`) to one function. The function keeps each refusal text and each exit status.
2. The move starts no process and changes no behavior.
3. Add one test that calls the function through `shell_functions` for one accepted form and one refused form, with the exit status.

**Files:**
- `scripts/direct/verify.sh`
- `scripts/direct/test_verify_argument_parser.py`

**Workers:** 1 writer: merge16.

**Constraints from prior phases:**
- Add no guard, refusal or check that this Spec does not ask for.
- natest changes `test_verify_mac_only.py`. Do not change it here.
- This phase lands on `verify-plain`. After its checkpoint, send natest the hashes of Phases 41c and 41d in one message.

**Acceptance gate:**
- `test_verify_mac_only.py`, `test_verify_mac_arguments.py` and `test_verify_argument_parser.py` pass with no change to the first two.
- basedpyright 0/0/0 on the changed Python files. The merge gate runs all modules.
