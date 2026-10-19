# Clippy fix before the lint judgment

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The lint step runs `cargo clippy --fix`, formats, then judges with clippy. This replaces the multiply-add JSON replay loop and its applier.

> **As-built disposition: amend** — `docs/as-built/build-followups-mul-add.md`

> **Production: build-followups** — unit `mul_add-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via the showrunner, 2026-10-18: "then our lint step in verify.sh that runs clippy should do the clippy --fix and then clippy to find out what remains - will this take care of mul_add?"

The showrunner, 2026-10-18: "build this. In verify.sh lint, the clippy stage runs `cargo clippy --fix --allow-dirty --allow-staged` (same targets, features and lint levels as the judgment), formats, then judges with clippy as today. … If it does, retire apply_mul_add.py and the JSON replay loop." Then the user, via the showrunner: "you don't need to measure how much time this adds".

## Probe (unit director, 2026-10-18)

On a copy of `~/rust/obsidian_knife` with its own lint levels (`all`, `nursery` and `pedantic` at deny), `cargo clippy --fix --allow-dirty --allow-staged --workspace --all-targets --all-features`, with and without `-- -D warnings`:
- rewrote `a * b + c` to `a.mul_add(b, c)`;
- left a function with `#[allow(clippy::suboptimal_flops, reason = …)]` unchanged;
- also applied the other automatic fixes in the same pass: it removed an unused import and a needless `return`, and added `const` to a function;
- rolled back all fixes for one target (the test build) when a fix there did not compile, and kept the other target's fixes. It exited 0.

One fix pass can expose a new fix (here `missing_const_for_fn` after the `mul_add` rewrite).

## Decisions (unit director, 2026-10-18, under the decision criteria)

- **Fix on every lint call, before the judgment**, as the user asked. On a clean tree the fix pass finds nothing to change.
- **The fix pass never sets lint's status.** Its exit code is ignored, because a tree with compile errors makes it fail and the judgment reports those errors. The judgment's status is lint's status.
- **Fix passes repeat while they change the tree, at most 3.** One pass can expose a new fix, and a failed judgment is cached under the fixed tree, so a single pass would leave that fix undone on every later call (the adversary review, phase 1). A pass that changes nothing ends the loop, so a clean tree costs one pass. A failure after a third changing pass is not cached.
- **Retire the rewrite count log.** The measurement it fed has ended. `~/.local/state/mul-add-hook/lint-rewrite/watch.py` is outside the repository and stays as it is.
- **`--check` mode and other verbs stay judgment-only.** `lint --workspace --check`, `example`, `check`, `test`, `final` and the release check never rewrite.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Project started:** 2026-10-19T01:44:00+00:00
- **Stack:** Bash (3.2 compatible) and Python 3.13, standard library only.
- **Layout:**
  - `scripts/delegate/verify.sh` — the `lint` verb; the clippy stage starts at line 1892, and the JSON loop runs from line 1904 to 1951. `MUL_ADD_APPLIER` is at line 162 and enters the cache key near line 876.
  - `scripts/lint/invoke.sh` — `invoke_clippy` (line 523) and `invoke_clippy_mul_add_json` (line 546); `run_json_stdout` serves the JSON replay.
  - `scripts/lint/apply_mul_add.py`, `scripts/lint/test_apply_mul_add.py` — the applier and its tests; deleted by this plan.
  - `scripts/delegate/test_verify_lint_scope.py` — fake-Cargo tests for the lint step, including the rewrite rounds and count logs.
- **Key files:** `docs/as-built/build-followups-mul-add.md`; `docs/as-built/phase-checks.md` (line 18); `docs/as-built/nextest-width-and-lint-scope.md` (line 72).
- **Test lanes:** `scripts/delegate/`, `scripts/lint/` — `test_*.py` beside the scripts.
- **Build:** none in the repository.
- **Test:** from the worktree root: `python3 -m unittest scripts.delegate.test_verify_lint_scope`; `python3 -m unittest discover -s scripts/lint -p 'test_*.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing). `bash -n` on each changed shell file. Run each once after the edits, fix what it names, and never re-run on an unchanged tree.
- **Style:** none — not Rust in the repository.
- **Invariants:**
  - Tests never run the real `cargo` and never write outside their temporary folder: Cargo is a fake.
  - `clippy::suboptimal_flops` stays enabled in Cargo (the user, 2026-10-06).
  - The judgment is unchanged: `cargo clippy --workspace --all-targets --all-features --keep-going -- -D warnings`.
  - Shell stays Bash 3.2 compatible. Python is typed throughout with no `Any` and no file-level type ignores (the user's `CLAUDE.md`).
  - The words in `~/rust/nate_style/rust/forbidden-words.md` never appear in code, comments or docs.
  - Write prose in Simplified Technical English (the user, 2026-10-18).

## Phases

### Phase 1 — Fix, format, judge  · status: done

#### Work Order

**Goal:** every normal `verify.sh lint` call applies clippy's automatic fixes, formats, then judges, and the multiply-add JSON route is gone.

**Spec:**
- Add `invoke_clippy_fix` to `scripts/lint/invoke.sh`. It takes the same cargo scope arguments as `invoke_clippy` and runs, through `run`, `cargo clippy --fix --allow-dirty --allow-staged --allow-no-vcs <scope> -- -D warnings <extra clippy args>`. Clippy off gives the existing skip notice and runs nothing. Keep the sandbox-failure detection that `run` gives.
- In the normal `verify.sh lint` path (not `--check`), the clippy stage runs `invoke_clippy_fix --workspace --all-targets --all-features --keep-going`, ignores its status, runs `fmt_cargo "${FMT_SCOPE[@]}"`, recomputes `VERDICT_KEY` when `RUN_LOG` is set, then runs the existing judgment `invoke_clippy --workspace --all-targets --all-features --keep-going`. Its status is lint's status. Keep the `style-lint: clippy start` and `passed` lines.
- Delete the JSON loop, `CLIPPY_LOG`, the rewrite count log (`MUL_ADD_REWRITE_LOG_DIR`, `MUL_ADD_HOOK_STATE`), `MUL_ADD_APPLIER` and its place in the cache key, and `invoke_clippy_mul_add_json`. Delete `run_json_stdout` and the `LINT_JSON_STDOUT` path if nothing else uses them; prove it with grep.
- Delete `scripts/lint/apply_mul_add.py` and `scripts/lint/test_apply_mul_add.py`.
- Add `invoke_clippy_fix` and the cache-key change to the comments that list the lint stages (the header of `verify.sh` and `invoke.sh`).

**Files:**
- `scripts/delegate/verify.sh`, `scripts/delegate/test_verify_lint_scope.py`
- `scripts/lint/invoke.sh`
- `scripts/lint/apply_mul_add.py`, `scripts/lint/test_apply_mul_add.py` — deleted

**Workers:** 1 writer + 1 adversary review.
- `impl` — every file above

**Acceptance gate:** `test_verify_lint_scope.py` replaces the rewrite-round tests with these cases, using the fake Cargo: a normal lint call runs, in order, the fix (with `--fix --allow-dirty --allow-staged --allow-no-vcs` and the judgment's scope and `-D warnings`), fmt, then the judgment; a failing fix still formats and judges, and lint returns the judgment's status; a passing judgment after a fix continues to rustdoc; `--check` runs no fix; `example` runs no fix; clippy off runs neither. `python3 -m unittest scripts.delegate.test_verify_lint_scope` and `python3 -m unittest discover -s scripts/lint -p 'test_*.py'` pass. `basedpyright` reports `0 errors, 0 warnings, 0 notes` on each changed Python file; `bash -n` passes on each changed shell file. `grep -rn -E 'apply_mul_add|mul_add_json|MUL_ADD_REWRITE|MUL_ADD_APPLIER' scripts` finds nothing.
