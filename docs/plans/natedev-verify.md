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
  - `scripts/direct/verify.sh` — the verify entry point (3003 lines)
  - `scripts/direct/final_pass.py`, `step_record.py`, `local_release_wait.py`, `progress_history.py` — helpers `verify.sh` starts
  - `scripts/direct/test_verify_*.py` — verify pipeline tests
  - `scripts/mac_test/` — Mac offload: `mac_test.py`, `offload.py`, `copy_watch.py`, `audit.py` and tests
  - `scripts/buildlog/record.py`, `scripts/buildlog/mac_idle.py`, `scripts/build_targets/build_targets.py` — helpers `verify.sh` starts, owned by other units (read, do not edit without the owner)
  - `scripts/lib/py` — interpreter shim; caches the resolved path, so it adds no Python start of its own on a warm cache
- **Key files:**
  - `scripts/direct/verify.sh` — 54 Python starts in the source. Per step: `step_record.py` (lines 248, 253, 1563, 1633, 2683), `final_pass.py` (236, 1002), `record.py call` (1316), `mac_idle.py` (1096, 1645, 1662), `build_targets.py` (1088, 1506, 1522, 2131), `progress_history.py` (2655, 2669), inline `-c` scripts that parse `cargo metadata` separately (`MEMBER_PY` 747, `EXAMPLE_FEATURES_PY` 761, `TEST_TARGETS_PY` 768, `BUILD_TARGETS_PY` 866, `UNTESTED_EXAMPLES_PY` 2077, 2777), `DEV_FEATURES_PY` (1046), `TREE_KEY_PY` (1168), clock reads (`time.monotonic()` at 1070, 1670, 1685, 1759), a hash (2238), `STEP_OWNER_PY` through plain `python3` (2520), and `user_zone.py` (1981)
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

### Phase 1 — Cut Python helper starts in each verify run  · status: todo

#### Work Order

**Goal:** a `verify.sh` step spends at most 2 s of real CPU on Python helper starts, down from the ~11 s shutdown-unit measured, with every output and record unchanged.

**Spec:**

1. **Measure first.** Add an opt-in trace: with `VERIFY_PY_TRACE=<file>` set, wrap `$PY` so each call appends one line: helper name (script basename or the `-c` constant's variable name), wall ms and CPU ms (from `/proc/self/stat` deltas or `times`). Run the verify pipeline tests (`test_verify_final_pass.py`, `test_verify_dev_features.py`, `test_verify_checks_fail_closed.py`, `test_verify_doc.py`) and one real `verify.sh test` and one `verify.sh lint` on a small crate (for example `cargo-handler`) with the trace on. Record the per-helper table (calls, total CPU ms, import ms from `python -X importtime` for the top three) in the phase report. Keep the trace in the code: it is how a later change proves its cost.
2. **Remove starts that need no Python.**
   - Clock reads (`time.monotonic()` at lines 1070, 1670, 1685, 1759): use bash `$EPOCHREALTIME`. Bash 5 is on natedev and the Mac's verify path; confirm the Mac bash version from `scripts/mac_test/offload.py`'s remote shell before the change, and keep the Python fallback only if the Mac runs bash 3.2.
   - The hash at line 2238: `sha256sum` / `shasum -a 256` by platform, same first 16 hex chars.
   - `STEP_OWNER_PY` at line 2520 runs through plain `python3`: move it to `$PY` (invariant), and fold it into the step-record call if the trace shows them always paired.
3. **One `cargo metadata` parse per run.** `MEMBER_PY`, `EXAMPLE_FEATURES_PY`, `TEST_TARGETS_PY`, `BUILD_TARGETS_PY`, `UNTESTED_EXAMPLES_PY` and the `workspace_root` read at 2777 each start Python and parse the same metadata JSON. Replace them with one helper module `scripts/direct/verify_metadata.py` that reads the metadata once and prints every value the run needs as `NAME=value` lines (shell-quoted with `shlex.quote`), which `verify.sh` reads with one `eval` or a `while read` loop. Each old `-c` constant goes away. Edge: a verb that needs no metadata (`doc` early exits, the query path at line 183) must not start the new helper.
4. **Slow imports in this unit's helpers.** For `final_pass.py`, `step_record.py` and `local_release_wait.py`: where `-X importtime` shows a module import over 30 ms that only some subcommands need, move the import into that subcommand. For helpers owned by other units (`record.py`, `mac_idle.py`, `build_targets.py`, `progress_history.py`): write the measured cost and the proposed fix into the phase report and the checkpoint notice for the showrunner to route; do not edit them.
5. **Merge repeated per-step calls** only where the trace shows two calls of this unit's helpers always run back to back with no shell work between them (candidate: `step_record.py state` then `remove`); give the helper one subcommand that does both.
6. builds 2026-10-29 (hana's showrunner, organon 21:4x EDT): `token_wait_note` reads `locks/cargo.d/holder` with `read -r holder < … || holder=""`. The holder file has no trailing newline (`w74`), so `read` returns 1 after it fills `holder`, and the `||` clears it: the log says "it came free just now" while w74 holds the lock. Keep the value `read` fills when it returns non-zero at end of file (check every `read` of a board file in verify.sh for the same form). Test: a holder file `w74` with no newline gives "w74 holds it".

Done when the trace on the same runs shows ≤ 2 s CPU of helper starts per step. If the floor sits above 2 s only because of other units' helpers, the phase ends with this unit's share removed and the rest named per owner.

**Files:**
- `scripts/direct/verify.sh` — trace wrapper, bash clock and hash, one metadata call, `$PY` for `STEP_OWNER_PY`
- `scripts/direct/verify_metadata.py` — new: one parse of `cargo metadata`, prints the run's values
- `scripts/direct/test_verify_metadata.py` — new: each value the old `-c` constants printed, on a fixture metadata JSON (workspace with members, features, examples, test targets; a package with no tests; an unknown package → exit 2 like `MEMBER_PY`)
- `scripts/direct/final_pass.py`, `scripts/direct/step_record.py`, `scripts/direct/local_release_wait.py` — lazy imports, merged subcommand if the trace supports it
- `scripts/direct/test_verify_final_pass.py` — expectations unchanged; holds the new trace test: `VERIFY_PY_TRACE` writes one line per helper start
- `scripts/direct/test_verify_dev_features.py`, `scripts/direct/test_verify_checks_fail_closed.py`, `scripts/direct/test_verify_doc.py` — expectations unchanged; edit only when a removed `-c` constant or helper call is named in them
- `pyrightconfig.json` — one entry for the new module if the include list names files (builds-unit lists it as touched)

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/verify.sh`, `scripts/direct/verify_metadata.py`, `final_pass.py`, `step_record.py`, `local_release_wait.py`; hub: `scripts/direct/verify.sh` (every change routes through it)
- `test` opens as impl — `scripts/direct/test_verify_metadata.py` and the trace test, written from the Spec alone

**Constraints from prior phases:** none. Merge `origin/natedev` before dispatch so phases-unit's `TOKEN_WAITED` fix is in the tree.

**Acceptance gate:** merge gate `--tests-only` green on a tree merged with `origin/natedev`; basedpyright `0 errors, 0 warnings` on `scripts/direct scripts/mac_test` and every caller module; `bash -n` clean; the before/after trace table in the phase report shows ≤ 2 s CPU of helper starts per step for this unit's share, with the other owners' remaining cost named.

### Phase 1b — Refuse a step with a failing exit, and read a process start on the Mac  · status: todo

**Origin:** builds showrunner order 2026-10-30 03:5x EDT, a wrong-result defect from hana (trunk P170 board, 02:53 EDT). Runs in this worktree right after Phase 1, which owns the same files; ahead of Phase 7.

**Defect:** on the Mac, `verify.sh`'s step recorder (`step_record.py process_start`) reads `/proc/<pid>/stat`, which Darwin does not have. The step is refused, but the wrapper exits 0, so the failure reads as a pass.

**Fix:** (1) a refused step exits non-zero on every host, at the one place the wrapper turns a refusal into its exit status. (2) Process start comes from one shared helper in `scripts/lib/` that works on Linux (`/proc/<pid>/stat` field 22) and Darwin (`ps -o lstart=` under `LC_ALL=C TZ=UTC`, based on `message/sessions.py process_start`'s tested form) with one return type. `scripts/lib/` belongs to natedev: builds approved the path as outside this unit's Owns (03:5x); the checkpoint notice names it in its also-touches line. `step_record.py` and the inline readers in `scripts/direct/verify.sh` and `scripts/direct/board.sh` use it; no second copy. The other copies (`message/sessions.py`, `build_hold/build_hold.py`, `lint/memory_admit.py`, `lint/tmp_sweep.py`, `production/retire_unit.py`) go to their owners through builds after 1b merges: build_hold, memory_admit and tmp_sweep to resources; sessions.py and retire_unit.py to natedev.

**Files:** `scripts/lib/process_start.py` (new), its test, `scripts/direct/step_record.py`, `scripts/direct/verify.sh`, `scripts/direct/board.sh`, `scripts/direct/test_step_record.py`.

**Acceptance gate:** a test for each fix: a refused step makes `verify.sh` exit non-zero (a fake process root with no `stat`); the helper reads a start on Darwin (faked `sys.platform` and `ps`) and on Linux, and both reads agree with the record's stale check. basedpyright 0/0/0 on changed files; the merge gate passes. Hana is told when it lands.

### Phase 2 — Cancel a test or lint run when the tree changes under it  · status: done

**Depends on:** none (runs beside Phase 1 in its own worktree; the second to land merges the first in `scripts/direct/verify.sh`).

#### Work Order

**Source:** the user, via hana's showrunner and the natedev showrunner, 2026-10-29: "a unit director editing over and over would simply cause cancellations and restarts - blocking other units from ever entering the queue. The only sane thing is if a unit is editing we cancel the current test run as it is now obsolete. verify should tell the unit that it cancelled its current test because the unit was editing and that it should resubmit a test when it is ready. that is the only possible approach that is fair and even makes sense". This replaces an earlier restart design. <!-- allow-banned: verbatim user quote -->

**Goal:** a `verify.sh test` or `lint` whose input tree changes mid-run stops within one watch interval, gives back its memory admission, cargo token and queue place, records nothing, does not run again, and exits with status 77 and a message telling the caller the run was cancelled because the tree changed while it ran and to resubmit when its edits are done.

**Spec:**

1. **What counts as a change** (the user, 2026-10-29: cancel only when the result is surely stale; resources are the problem, and a wrong cancel wastes a whole run). One rule, built from today's test key and no new logic: the watcher counts a file only when today's `tree_key()` counts it (`counts()` in `TREE_KEY_PY`, `scripts/direct/verify.sh`) **and** it sits inside a workspace member, or it is one of cargo's own root inputs: `Cargo.toml`, `Cargo.lock`, `.cargo/`, `rust-toolchain`, `rust-toolchain.toml`. Files outside every member (plans, docs, scripts) never cancel. A miss is safe: the guard in `cache_record` still refuses a stale record at the end, which is today's behavior; a wrong cancel throws away a run, so when in doubt the watcher does not cancel. No dep-info reading, no second cargo call.
2. **When to watch.**
   - `test`: from the moment `VERDICT_KEY` is taken (`verify.sh:2678`, and the Mac path at 1437) to the end of the cargo / nextest run, local or on the Mac.
   - `lint`: only after lint's own rewrites are done, from `VERDICT_KEY` at `verify.sh:2962` through `invoke_clippy`. Lint's mend fixes, `clippy --fix` passes and format change the tree by design and never cancel.
   - `doc`, `check`, `build`, `example`, `final`: no watch.
3. **The watcher.** One background process per watched run, started once (no Python start per check): new `scripts/direct/tree_watch.py`, which computes item 1's key in-process (move the code `TREE_KEY_PY` runs into the module with one flag for item 1's filter, so the cache key and the watch key share one file walk and cannot drift) every `VERIFY_TREE_WATCH_S` seconds (default 5; the env value is for tests). On a key that differs from its start key it writes the new key to a file named on its command line and exits 0; it exits when its parent `verify.sh` is gone.
4. **Cancel.** When the watcher fires, `verify.sh` stops the run's process group (local cargo / nextest; on the Mac, the offload runner's cancel path in `scripts/mac_test/offload.py`), releases the memory admission, the cargo token and any queue place through the normal exit paths, writes no verify cache record, and exits 77 (`TREE_CHANGED_STATUS=77`, a status no other path uses). It prints: `verify.sh: cancelled — the tree changed while <cmd> ran, so this result would be stale. Resubmit the <cmd> when your edits are done.` Both the local and the Mac path print the same line.
5. **No rerun.** The cancelled call never starts the step again; the caller resubmits.
6. **Records.** The build log records the cancelled run with a new outcome word, `cancelled`, in `scripts/buildlog/record.py`, so reports do not count it as a failure.
7. **Callers.** The worker prompt rules (`docs/direct/write_prompt_contract.md` Verification and the `## Builds and tests` block) and the result message of `scripts/agents/mesh_runs.py` read status 77 as "cancelled: resubmit after your edits", not as a test failure. These edits are this phase's (showrunner, 2026-10-29); the checkpoint notice lists each under Also touches.
8. **The old refusal stays** as the last guard: a change the watcher missed between its last check and the end of the run still refuses the record with today's message.

**Files:**
- `scripts/direct/verify.sh` — start and stop the watcher around the watched spans, the cancel path, status 77 and its message
- `scripts/direct/tree_watch.py` — new: the in-process key loop; owns the code `TREE_KEY_PY` runs today
- `scripts/direct/test_tree_watch.py` — new: the watcher fires on an in-scope edit within one interval, ignores an out-of-scope edit, exits when its parent is gone
- `scripts/direct/test_verify_tree_cancel.py` — new: end-to-end with a fake cargo that sleeps: an edit mid-`test` ends the call with status 77 and the message, no cache record, no second run, token and memory admission released; an edit during lint's rewrite stages cancels nothing; an edit during lint's clippy stage cancels it
- `scripts/buildlog/record.py` — the `cancelled` outcome word
- `scripts/agents/mesh_runs.py` — result message for status 77
- `docs/direct/write_prompt_contract.md` — status 77 in the worker verification rules and the `## Builds and tests` block
- `pyrightconfig.json` — one entry for each new module if the include list names files

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/verify.sh`, `scripts/direct/tree_watch.py`, `scripts/buildlog/record.py`, `scripts/agents/mesh_runs.py`, `docs/direct/write_prompt_contract.md`, `pyrightconfig.json`; hub: `scripts/direct/verify.sh` (every change routes through it)
- `test` opens as impl — `scripts/direct/test_tree_watch.py` and `scripts/direct/test_verify_tree_cancel.py`, written from the Spec alone

**Constraints from prior phases:** none; Phase 1 runs beside it and changes how `verify.sh` starts its Python helpers, so the second to land merges the other's `verify.sh`.

**Runs in parallel with Phase 1** (the user, 2026-10-29) in its own worktree `/home/natepiano/worktrees/claude-build-followups-verify-cancel`, branch `natedev-verify-cancel`, because both phases edit `scripts/direct/verify.sh`; the second to land merges the first.

**Two things the tests must prove beyond doubt** (the user, 2026-10-29: "we have to be absolutely clear on that"):
- **The watcher is right.** A table test over real file edits: each in-scope edit (tested package source, a path dependency's file, a fixture inside the package, root `Cargo.toml`, `Cargo.lock`, `.cargo/config.toml`, `rust-toolchain.toml`) cancels; each out-of-scope edit (a plan or doc outside every member, another member the package does not depend on, the session folder, lint's own rewrites) does not.
- **The message reaches the right session.** Two callers run `verify.sh test` at once through `mesh_runs.py` from two different sessions; the tree of one changes; only that caller's run is cancelled, and the cancel message (status 77 and its text) is delivered to that caller's session and to no other; the other run finishes and is recorded.

**Acceptance gate:** the two proofs above green; merge gate `--tests-only` green on a tree merged with `origin/natedev`; basedpyright `0 errors, 0 warnings` on the changed Python files and every caller module; `bash -n` clean; a live check in a scratch clone of a small crate: start `verify.sh test`, edit an in-scope source file 10 s in, and see the cancel line within 5 s, exit status 77, no cache record, the build log showing the memory admission released, and the message in the calling session.

### Phase 3 — Scale the memory promise for a filtered test run  · status: done

**Depends on:** none. Runs in parallel in its own worktree `/home/natepiano/worktrees/claude-build-followups-verify-memory`, branch `natedev-verify-memory` (the user, 2026-10-29: "far more important than speeding up the python or cancelling a running test that is obsolete - this case is common and is creating huge backlogs"). <!-- allow-banned: verbatim user quote -->

#### Work Order

**Source:** hana's showrunner, relayed by the natedev showrunner 2026-10-29: the memory promise ignores `--filter`. `verify.sh test hana --filter native_rate` (one test) is promised 10.8 GiB, the p90 of all nextest runs of that step, so single-test runs queue like full suites.

**Goal:** a `verify.sh test <pkg> --filter <name>...` run is promised memory measured for filtered runs of that package, not the full-suite p90, so a single-test run no longer queues like a full suite.

**Spec:**

1. Find where the promise is computed (`scripts/lint/memory_admit.py`, `scripts/lint/memory_gate.sh`, or the build log under `scripts/buildlog/`) and what key its measured peaks use. Change the code where the math lives, in any of those files.
2. Key measured peaks by whether the run was filtered: a filtered run's promise comes from measured filtered runs of the same package (the filter names sorted, as the verify cache key sorts them; fall back to all filtered runs of the package when that exact set has no history).
3. With no filtered history for the package, a filtered run is promised the larger of a fixed floor and the package's measured compile peak (the build step still runs). Pick the floor from the anon peak history and state it in the phase report.
4. The full-suite promise is unchanged.

**Files:**
- `scripts/lint/memory_admit.py` — the promise for filtered runs
- `scripts/lint/test_memory_admit.py` — filtered and unfiltered promise cases
- `scripts/direct/verify.sh` — pass the filter set to the gate if it does not reach it today

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/lint/memory_admit.py`, `scripts/direct/verify.sh`, and any `scripts/buildlog/` file the promise math lives in; hub: `scripts/lint/memory_admit.py`
- `test` opens as impl — `scripts/lint/test_memory_admit.py`

**Constraints from prior phases:** none; Phases 1 and 2 edit `verify.sh` in other worktrees beside it. Keep any `verify.sh` edit to passing the filter set to the gate, so the merge stays small. This phase lands first.

**Acceptance gate:** merge gate `--tests-only` green; basedpyright `0 errors, 0 warnings` on changed files and callers; a test shows `test hana --filter native_rate` with filtered history promised that history's p90, without history the floor rule, and an unfiltered run unchanged.

### Phase 4 — Promise a fresh test run only its test memory  · status: done

**Depends on:** Phase 3 (same function). Runs in Phase 3's worktree `/home/natepiano/worktrees/claude-build-followups-verify-memory`, branch `natedev-verify-memory`, after Phase 3's checkpoint.

#### Work Order

**Source:** Phase 3's measurement, 2026-10-29. Filtered hana nextest runs on natedev since 2026-10-15: 2328 compiled crates, anon p50 8.32 GiB, p90 11.49 GiB; 571 compiled nothing, anon p50 0.90 GiB, p90 3.31 GiB. The fresh runs waited 738 s on memory on average. A filtered run still builds every test target of the package, so a run that compiles costs about what a full suite costs; Phase 3 can only cut the promise of `test hana --filter native_rate` from 10.87 to 9.38 GiB. The backlog the user named (2026-10-29: "this case is common and is creating huge backlogs") is the fresh runs, which are promised the compile cost they never pay. <!-- allow-banned: verbatim user quote -->

**Goal:** a test run whose test build is already up to date is promised the measured memory of fresh runs of that package and scope, not the compile cost.

**Spec:**

1. One rule: a run is fresh when the last finished `nextest` step in the same worktree, for the same package and scope, recorded the same tree key as this run and built its tests (exit 0, or tests ran). Everything else is a compile run, including no tree key, no earlier step, another key, and a disk sweep of this worktree's target since that step. When in doubt, it is a compile run.
2. Find where the build log records a step's tree key, and confirm it is the key `verify.sh` computes for the test. If admission cannot see this run's key today, `verify.sh` passes it (one environment variable, no other change to `verify.sh`).
3. A fresh run is promised the p90 of measured fresh runs (`crates_compiled` 0, joined by `call_id`) of the same package and scope, at least `FILTERED_FLOOR`; with no fresh history, the compile promise. Prefer the exact filter set as Phase 3 does.
4. A compile run keeps Phase 3's promise. Full-suite compile runs are unchanged.
5. Report the fresh and compile promises for `test hana --filter native_rate` from real history, before and after.
6. Showrunner 2026-10-29 (hana's trunk found it): the lint cache key leaves out the assertion guard's base (Phase 5 picks it), so after origin/HEAD moved, lint replayed an old recorded failure. Put the guard base commit, and any other input the guard reads, in the lint key. A test moves origin/HEAD and sees lint re-run.
7. builds 2026-10-29 (hana's writer found it, 12:10 EDT): (a) the assertion guard reads `Assertion-Change:` rulings only from `UNIT_SESSION_DIR`, and a Codex mesh worker has only `UNIT_RUN_DIR`, so it never sees the rulings; (b) the lint key must change when those rulings change. One reading of the rulings folders — `UNIT_SESSION_DIR` and `UNIT_RUN_DIR`, each when set — feeds both the guard's `--rulings` and the lint key. Tests: a run with only `UNIT_RUN_DIR` reads a ruling there; adding a ruling re-runs a cached failed lint.

**Files:**
- `scripts/lint/memory_admit.py` — the fresh-run rule and promise
- `scripts/lint/test_memory_admit.py` — fresh and compile cases
- `scripts/direct/verify.sh` — pass this run's tree key to admission, only if it does not reach it today; the guard base in the lint key
- `scripts/direct/test_verify_lint_guard_key.py` — origin/HEAD moves, lint re-runs

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/lint/memory_admit.py`, `scripts/direct/verify.sh`, and any `scripts/buildlog/` file the tree key lives in; hub: `scripts/lint/memory_admit.py`
- `test` — `scripts/lint/test_memory_admit.py`

**Constraints from prior phases:** Phase 3 keyed filtered promises by package and sorted filter set, and stores `package` and `filters` in `anon_peaks.jsonl`. Phases 1 and 2 edit `verify.sh` in other worktrees; keep any `verify.sh` edit to the one variable.

**Acceptance gate:** merge gate `--tests-only` green; basedpyright `0 errors, 0 warnings` on changed files and callers; tests show a fresh run (same worktree, package, scope and tree key) promised fresh history, and each doubt case (other key, other worktree, no earlier step, sweep since, no key) promised the compile promise; the real-history report from Spec 5.

### Phase 5 — Compare assertions against the branch's own base  · status: done

**Depends on:** none. Runs in its own worktree `/home/natepiano/worktrees/claude-build-followups-verify-guard`, branch `natedev-verify-guard`, from `origin/natedev`, and is pushed alone. Urgent: it blocks hana's priority landing (deadline 11:00 EDT 2026-10-29).

#### Work Order

**Source:** natedev showrunner, 2026-10-29, from hana. `verify.sh lint` runs `assertion_guard.py` first with only `--repo`. Its `default_base()` is the merge-base of HEAD with origin/HEAD (main), so on a hana branch it counts every assertion change since main: "assertion guard: 676 blocking", exit 1, and clippy never runs. Many trees have no upstream: merge-gate gate trees and `/tmp/claude/delegate` clones on a detached HEAD.

**Goal:** the guard compares a tree against the base of its own branch, never against main by default, and refuses with one clear line when it cannot tell.

**Spec:**

1. `default_base()` picks the base in this order, and the first that resolves wins:
   1. `--base` (verify.sh passes `VERIFY_ASSERTION_BASE` as `--base` when it is set);
   2. the merge-base of HEAD with `@{upstream}`;
   3. on a detached HEAD or a branch with no upstream: the remote branch the tree came from — of every `refs/remotes/*` branch except `*/HEAD` and origin/HEAD's target, the one whose merge-base with HEAD has the fewest commits from merge-base to HEAD;
   4. no candidate, or two nearest candidates with different merge-bases: refuse (exit 2) with one line that names `VERIFY_ASSERTION_BASE`.
2. Remove the old fallback chain (`origin/HEAD`, `origin/main`, `origin/master`, `main`, `master`). The fallback is never main.
3. `verify.sh lint` passes `--base "$VERIFY_ASSERTION_BASE"` when it is set; nothing else in `verify.sh` changes.
4. Never change origin/HEAD, in tests or code (hana worktrees share refs); tests build their own repositories.

**Files:**
- `scripts/direct/assertion_guard.py` — `default_base()`
- `scripts/direct/test_assertion_guard.py` — base cases
- `scripts/direct/verify.sh` — `VERIFY_ASSERTION_BASE` pass-through
- `scripts/direct/test_verify_assertion_guard.py` — pass-through case

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/assertion_guard.py`, `scripts/direct/verify.sh`; hub: `scripts/direct/assertion_guard.py`
- `test` — `scripts/direct/test_assertion_guard.py`, `scripts/direct/test_verify_assertion_guard.py`

**Constraints from prior phases:** Phases 1 and 2 edit `verify.sh` in other worktrees; keep the `verify.sh` edit to the one pass-through. Phase 4 Spec 6 keys lint on the base this phase picks.

**Acceptance gate:** merge gate `--tests-only` green; basedpyright `0 errors, 0 warnings` on changed files; tests cover an upstream that is not main, a detached HEAD made from a feature branch, a tie and a no-candidate refusal, `--base` and `VERIFY_ASSERTION_BASE` winning, and no test touching a shared origin/HEAD.

### Phase 6 — Count only our own assertion removals on a merge, and make the commit hook agree with the guard  · status: done

**Depends on:** Phase 5 (same file). Runs in Phase 5's worktree `/home/natepiano/worktrees/claude-build-followups-verify-guard`, branch `natedev-verify-guard`, right after Phase 5's push. Urgent: it blocks shutdown-unit's merge.

#### Work Order

**Source:** natedev showrunner, 2026-10-29. (1) On a merge commit the guard compares the index with HEAD only, so it counts the other parent's own removals as ours: 80 entries in shutdown-unit's merge of origin/natedev, for example the guilds tests in `test_add_unit_restore` that origin removed. Measured there: no assertion is gone against both parents. (2) `assertion_guard.py --base HEAD --staged --rulings <message file>` exits 0, but `pre-tool-use-change-review-gate.py` says "2 blocking" for the same `git commit -F`, because the hook passes no `--base` and the guard then picks the branch base, not the commit's parent.

**Goal:** the guard blocks only assertions this branch removed, and the commit hook and the guard give the same answer for the same commit.

**Spec:**

1. The guard compares against one or more bases, and a finding blocks only when it shows against every base: `--base` may repeat.
2. The bases of a commit being made are HEAD, plus `MERGE_HEAD` when a merge is in progress. With no `--base`, the guard on a tree whose HEAD is a merge commit adds each parent of HEAD except the first to Phase 5's default base.
3. One function in `assertion_guard.py` gives the bases of a commit being made; the hook calls it (or calls the guard with the same `--base` values and the same rulings text), so hook and guard cannot differ.
4. Rulings: the hook passes the same message text the guard reads with `--rulings`.

**Files:**
- `scripts/direct/assertion_guard.py` — several bases; the commit-bases function
- `scripts/hooks/pre-tool-use-change-review-gate.py` — call it
- `scripts/direct/test_assertion_guard.py` — merge cases
- `scripts/hooks/test_change_review_gate.py` — hook answer = guard answer

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/assertion_guard.py`, `scripts/hooks/pre-tool-use-change-review-gate.py`; hub: `scripts/direct/assertion_guard.py`
- `test` — `scripts/direct/test_assertion_guard.py`, `scripts/hooks/test_change_review_gate.py`

**Constraints from prior phases:** Phase 5's base order stays as it is. Never change origin/HEAD in tests.

**Acceptance gate (showrunner bump):** basedpyright `0 errors, 0 warnings` on changed files; the guard and hook test files pass; tests show a merge whose other parent removed assertions passes, our own removal on a merge still blocks, and the hook and the guard return the same answer for a staged commit with a ruling in its `-F` file.

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

### Phase 7 — Keep the cargo token through a long lint, and keep a run's queue place when the Mac is taken  · status: todo

**Depends on:** none. Urgent, ahead of Phases 1, 2 and 4. Runs in `/home/natepiano/worktrees/claude-build-followups-verify-guard`, branch `natedev-verify-token` (from origin/natedev d41fbd8c).

#### Work Order

**Source:** natedev and builds showrunners, 2026-10-29, from hana. (1) Lint run 20261029T062917-3558291-d939 (`verify.sh lint hana`, 06:29-12:37 EDT) lost the cargo token before fmt and exited 125. verify.sh holds the token with `--hold 3600` and never renews it, and board.sh reclaims an expired hold while its holder lives (board.log: "12:33:40 [splash] claim: token cargo reclaimed from panels after its hold expired"). clippy ran past the hour because the tree key hashes verify.sh's own file. The ~/.claude main checkout moved verify.sh three times during the run, so the fix loop saw "changed" and ran two more passes (the last took 3801 s), and `cache_record` printed a false "tree changed" notice. `lint_failure_is_the_tree` counts 125 as the tree's lint failure. (2) widget run 20261029T070857-3319484-a8ef left the memory wait 12 times on an unlocked `mac_test.py free-now` read. Each time it lost the real claim to another hana step, ran `run_nextest` again without `BUILDLOG_MEM_WAIT_SINCE`, and queued at the back.

**Goal:** a run that holds the cargo token never loses it between or during its steps; a change to verify's own scripts never makes a run do more work or print a tree-changed notice; a run that does not get the Mac keeps its memory-queue place.

**Spec:**

1. While verify.sh holds the cargo token, it renews the hold before each step and from a background loop while a step runs (well inside the hold), and stops renewing when it releases. Use `board.sh renew`; add `--pid` to `cmd_renew` so verify renews for its holder pid.
2. The fix loop's "did the fixes change anything" check and the tree-changed notice use tree content only, not the hashes of verify.sh, invoke.sh or lint.conf. The pass-record key may keep them.
3. `lint_failure_is_the_tree` never counts status 125, or any lost-token status, as the tree's lint failure.
4. In `run_local_test`, the first memory-wait start (`BUILDLOG_MEM_WAIT_FROM`) carries through a call file to every later `run_nextest` as `BUILDLOG_MEM_WAIT_SINCE`, so a run whose Mac claim failed keeps its queue place. The recorded memory wait (`BUILDLOG_MEM_WAIT_S`, the build record's `mem_wait_s`) counts from that first start, so a run that rejoined 12 times records its whole wait, not the last part (builds, 2026-10-29, from resources-unit: widget's 4 h 34 min wait recorded 1428 s).
5. board.sh `lock_stale_reason` never reclaims an expired hold while `holder_pid` is alive and its `owned` group record still matches; a dead holder is still freed.
6. One decision point for the Mac: the memory wait's leave hook takes the Mac claim under `state.lock` (as `claim_once` does) and hands the held claim to the runner, instead of reading `free-now`. A step leaves the wait only when it already holds the Mac; a refused claim keeps waiting in place.
7. Script hashes stay in the pass-record (cache) key, so a lint result does not survive a lint-script change (builds, 2026-10-29).

**Files:**
- `scripts/direct/verify.sh` — specs 1-4
- `scripts/direct/board.sh` — spec 5 and renew --pid (owner: natedev; name it in the notice)
- `scripts/mac_test/mac_test.py`, `scripts/mac_test/offload.py` — spec 6 claim handoff
- `scripts/lint/memory_gate.sh` — spec 6 leave hook (owner: resources-unit; trial-merge against origin/builds-resources)
- `scripts/direct/test_verify_token_renew.py` — new: regressions for specs 1-6

**Workers:** 1 writer (the idle cancel test worker, `claude-build-followups-verify-cancel-test`, slot `test`), source and tests.

**Constraints:** never change origin/HEAD in tests. builds approved the board.sh, mac_test.py, offload.py and memory_gate.sh edits (2026-10-29); memory_admit.py stays read only.

**Acceptance gate:** `bash -n`; basedpyright `0 errors, 0 warnings` on the new test file; `python3 -m unittest discover -s scripts/direct -p 'test_verify*.py'` passes; regressions show: a step that runs past the hold keeps the token; a verify.sh content change during lint runs no extra fix pass and prints no notice; 125 is not cached as a lint FAIL; a failed Mac claim reuses the first wait start.

### Phase 8 — Name the holder of what a wait is on  · status: todo

**Depends on:** sdlc phases-unit Phase 11 merged on builds (adds `start-wait --holder-*` and `wait-holder`; builds tells verify-unit, ETA 18:30 EDT). Needed by hierarchies' wait experiment.

#### Work Order

**Source:** sdlc hand-off, approved by builds 2026-10-29.

**Goal:** every wait verify.sh records names who holds what it waits on.

**Spec:**

1. `phase_wait_start` passes the holder to `progress_history.py start-wait`: `--holder-resource-kind slot|memory --holder-resource <slot name | memory pool> --holder-session <session id>`, or `--holder-unit <unit>` when only the unit is known. All flags or none; none records `HolderNotNamed`.
2. The holder is: for a slot, the board token owner; for memory, the owner of the reservation that blocks admission (a read function in `memory_admit.py`, resources-unit's); for a build hold, the hold's holder.
3. When the holder changes during the same wait, call `progress_history.py wait-holder --session-dir <dir> --wait-id <id>` with the same flags.
4. Tests: one per resource kind, a holder change mid-wait, and the no-holder case.

**Files:**
- `scripts/direct/verify.sh` — holder lookup and the two calls
- `scripts/direct/test_verify_wait_holder.py` — new
- `scripts/lint/memory_admit.py` — read only here: resources-unit's 45e60cc9 on builds-resources (merged to builds by the showrunner before this phase). `python3 scripts/lint/memory_admit.py blocker --waiter <waiter file>` prints one JSON line `{"session_id","session_name","pool":"memory"}`, or `{"unit"}` when only a systemd unit is known, or `{}`; exit 2 when the waiter file is gone. On the existing `check` poll, `BUILDLOG_MEM_BLOCKER=1` adds the same JSON as pipe field 16 (field 19 with `BUILDLOG_MEM_DETAIL=1`); a `|` in a name comes escaped. Use the `check` field inside the memory wait loop (no extra process per poll); without the opt-in the output is unchanged

**Workers:** 1 writer + 1 tester.

**Acceptance gate:** merge gate `--tests-only` green on a tree merged with origin/builds; basedpyright 0/0 on changed files and callers.

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

### Phase 12 — Pair an assertion whose local binding was renamed  · status: todo

**Origin:** builds showrunner, 2026-10-31 03:15Z, wrong result, current; it lands before the bulk prefer-type rename run. Branch `natedev-verify-base` (carries Phases 6c and 11), worktree `/home/natepiano/worktrees/claude-build-followups-verify-base`.

**Depends on:** Phase 6b (constant pairing path), Phase 6c (test naming), Phase 11 (bases) — same file.

#### Work Order

**Source:** builds, 2026-10-31. cargo-liner-style-names-unit commit `8dd754b1` in `~/rust/cargo-liner-style-names` renames local bindings in cargo-port tests: `crates/cargo-port/src/tui/panes/layout.rs` (`short`/`tall` → `short_resolved_pane_layout`/`tall_resolved_pane_layout`) and `crates/cargo-port/src/tui/panes/ci/render.rs` (`expected` → `expected_ci_display_columns`, and nightly rustfmt rewrapped the grown lines). The guard reads `layout.rs:410` and `render.rs:833` as `removed -> (none)`. That commit passes only through two `Assertion-Change:` rulings.

**Goal:** an assertion whose only change is a renamed local binding pairs with its new form and never reads as removed; any other change to the assertion still shows, and a rename the guard cannot prove blocks.

**Spec:**

1. Reproduce first: build a fixture repo from the two real shapes (two bindings renamed in one assert; one binding renamed in a multi-line assert that rustfmt rewraps). Record in the summary that the guard reports both as `removed` before the first edit.
2. Extend the one Phase 6b pairing path (the place that pairs a removed literal assertion with its replacement when a literal became a `const` name). Add a binding-rename pairing there: an old and a new assertion pair when their token streams are equal after a consistent one-to-one rename of local `let` bindings and parameters of the enclosing test function. Compare tokens, not text, so a rustfmt rewrap pairs. No second matcher, no second tokenizer, no copy of the census.
3. A rename is proven only when, in the same test function on each side: the old name has exactly one binding and no use after the rename point, the new name has exactly one binding and did not exist on the old side, and the binding's right-hand side is token-equal across the two sides (after the same rename map). Every other form refuses the pair and blocks: shadowed names, pattern bindings (tuple, struct, `ref`, `mut` patterns beyond plain `let mut x`), closure parameters, names used inside macros other than the trusted assert family, a name that maps to two names, and two names that map to one. Refuse whole classes, not single edge cases.
4. A rename plus any other change (an operator, a literal, a call, an argument order) still reports, as it does today.
5. Fix the guard row in `docs/as-built/phase-checks.md` so it lists binding renames among the forms that pair, and the refusal classes.

**Files:**
- `scripts/direct/assertion_guard.py` — the pairing path
- `scripts/direct/test_assertion_guard_revisions.py` (or the shared test module the 6b pairing tests live in) — rename cases
- `docs/as-built/phase-checks.md` — guard row

**Workers:** 1 writer (one owner for all three files).

**State:** none.

**Constraints from prior phases:** Phase 6b's fail-closed rules, its binding census and its git-revision reads stay as they are and are reused. Phase 6c's test naming and Phase 11's one bases function stay as they are. Never change origin/HEAD in tests.

**Acceptance gate:**
- New tests, each failing on e7c2f25c: (a) two bindings renamed in one `assert!` pair, no `removed`; (b) one binding renamed in a multi-line `assert_eq!` that rustfmt rewraps pairs, no `removed`; (c) a rename plus a changed operator or literal still reports and blocks; (d) a rename the guard cannot prove (a shadowed name, a name mapped two ways, a binding whose value changed) blocks; (e) a Python test with a renamed local passes the same way, or refuses as a class if Python is out of scope — the summary says which.
- The real case: with the commit's own `Assertion-Change:` lines left out (re-apply `8dd754b1`'s diff to a scratch clone at `8dd754b1^` with a plain message, or run the guard on that diff staged), `assertion_guard.py` prints no `removed` line for `layout.rs` or `render.rs`.
- basedpyright `0 errors, 0 warnings, 0 notes` on each changed Python file; every `scripts/direct/test_assertion_guard*` and `scripts/hooks/test_change_review_gate.py` module passes; the merge gate passes.
