# verify

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Owner of `verify.sh`, its final pass, its cache, its lint and test scope, its speed and the Mac test code; first change: cut the Python helper start time in each verify run.

> **Production: natedev** — unit `verify-unit`; production doc `docs/plans/natedev-production.md`

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

Done when the trace on the same runs shows ≤ 2 s CPU of helper starts per step. If the floor sits above 2 s only because of other units' helpers, the phase ends with this unit's share removed and the rest named per owner.

**Files:**
- `scripts/direct/verify.sh` — trace wrapper, bash clock and hash, one metadata call, `$PY` for `STEP_OWNER_PY`
- `scripts/direct/verify_metadata.py` — new: one parse of `cargo metadata`, prints the run's values
- `scripts/direct/test_verify_metadata.py` — new: each value the old `-c` constants printed, on a fixture metadata JSON (workspace with members, features, examples, test targets; a package with no tests; an unknown package → exit 2 like `MEMBER_PY`)
- `scripts/direct/final_pass.py`, `scripts/direct/step_record.py`, `scripts/direct/local_release_wait.py` — lazy imports, merged subcommand if the trace supports it
- `scripts/direct/test_verify_final_pass.py` and the other `test_verify_*.py` — unchanged expectations; add a trace test that `VERIFY_PY_TRACE` writes one line per helper start
- `pyrightconfig.json` — one entry for the new module if the include list names files (builds-unit lists it as touched)

**Workers:** 1 writer + 1 tester
- `impl` — `scripts/direct/verify.sh`, `scripts/direct/verify_metadata.py`, `final_pass.py`, `step_record.py`, `local_release_wait.py`; hub: `scripts/direct/verify.sh` (every change routes through it)
- `test` opens as impl — `scripts/direct/test_verify_metadata.py` and the trace test, written from the Spec alone

**Constraints from prior phases:** none. Merge `origin/natedev` before dispatch so phases-unit's `TOKEN_WAITED` fix is in the tree.

**Acceptance gate:** merge gate `--tests-only` green on a tree merged with `origin/natedev`; basedpyright `0 errors, 0 warnings` on `scripts/direct scripts/mac_test` and every caller module; `bash -n` clean; the before/after trace table in the phase report shows ≤ 2 s CPU of helper starts per step for this unit's share, with the other owners' remaining cost named.
