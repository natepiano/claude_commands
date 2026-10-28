# efficiency

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Rustdoc runs once per phase, before the checkpoint commit, and not on every `verify.sh lint` call. A hook refuses a Rust checkpoint commit that has no passing rustdoc run on its exact tree.

> **Production: build-followups** — unit `efficiency-unit`; production doc `docs/plans/build-followups-production.md`

## Source

User, 2026-10-27 (efficiency ledger 2026-10-10, new evidence 10-17 and 10-18: 755 rustdoc steps cost 1,372 min in 7 days and caught 2 errors; gain about 18.6 h/week of seats waiting on rustdoc):

> okay - i would like you to prioritize all 3 of these proposals for implementation now … i want these optimizations as soon as possible

> i do want to make sure that it is impossible to NOT run the cargo doc at least once a phase, though - that is non-negotaible

> goahead with both

The two hana proposals in the same request went to hana's production (assembly-unit Phases 3 and 4, showrunner 2026-10-27). This plan holds only the rustdoc change.

## Delegation Context

- **Project:** `~/.claude` config repo. The unit director names the worktree and branch git reports at dispatch in every Work Order.
- **Project started:** 2026-10-28T00:53:00+00:00
- **Stack:** Bash (`verify.sh`), Python 3 standard library checked by basedpyright, Claude Code PreToolUse hooks.
- **Layout:** `scripts/direct/verify.sh` and its tests in `scripts/direct/`; hooks and their tests in `scripts/hooks/`; hook registration in `settings.json`; procedure docs in `docs/direct/`, `commands/unit/`, `config/lint.conf`, `docs/as-built/`.
- **Key files:**
  - `scripts/direct/verify.sh` (2,502 lines). `lint` runs mend, fmt, taplo, clippy fix, fmt, clippy judgment; clippy is `invoke_clippy --workspace --all-targets --all-features` (near line 2455), and only fmt is scoped to changed members (`CHANGED_MEMBERS_PY`). `lint` refuses `--features`. `doc` runs `invoke_doc --workspace` and writes the doc pass record (`doc_tree`, `doc_record`) to `~/.local/state/verify/doc_pass/<tree>.json`. Stages are gated by `config/lint.conf` through `lint_config_enabled` and `lint_config_skip_notice`; `doc` ignores the switch. The header (lines 1–150) documents every verb, the pass records under `<session>/verify_cache/` (keyed by the judged tree, arguments, rustc, lint.conf, verify.sh, invoke.sh and environment), the build-log call record (`note_event`), and the Mac offload. `final` (near line 2494) runs `cargo check --workspace --all-targets` and `run_nextest --workspace`.
  - `scripts/hooks/pre-tool-use-change-review-gate.py` — the model for the new hook: it finds `git commit` with a `checkpoint(...)` subject in a Bash call (`COMMIT`, `SUBJECT`, `DIRECTORY`, `ALL`, `staged()`), reads `-m`, heredoc and `-F` text, and denies through `hookSpecificOutput.permissionDecision`. It is registered in `settings.json` near line 261. Its tests: `scripts/hooks/test_change_review_gate.py`.
  - `docs/direct/phase_checks.md` — `<PhaseChecks/>`: the table puts rustdoc in "the workers' last `verify.sh lint`". `commands/unit/checkpoint.md` — `<CheckpointCommit/>` (the commit message format at line ~155). `config/lint.conf` — `doc=on` (line 46). `commands/unit/direct.md`, `docs/as-built/nextest-width-and-lint-scope.md`, `docs/as-built/phase-checks.md`, `docs/as-built/build-followups-mul-add.md` mention rustdoc in lint.
  - Tests: `scripts/direct/test_verify_lint_scope.py` (lint scope), other `scripts/direct/test_verify_*.py`.
- **Gate:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <worktree> --tests-only` from the repo root, and basedpyright with zero errors and zero warnings on every touched folder.

## Phases

### Phase 1 — Rustdoc once per phase, enforced at the checkpoint  · status: done

#### As-built

- `verify.sh doc` runs `cargo doc --workspace --no-deps` with warnings denied over the whole workspace, through the cargo token and memory gate. It sets `LINT_CONFIG_FORCE=1`, so `lint.conf`'s `doc=off` cannot switch it off.
- On exit 0 it writes a doc pass record to `~/.local/state/verify/doc_pass/<tree>.json` with keys `repo_top`, `tree_id`, `time` and `call_id`. The tree id comes from a private index (`GIT_INDEX_FILE`, `git add -A`, `git write-tree`), so untracked files count and the real index does not change. A tree that changes during the run writes no record. Each write prunes records older than 14 days.
- `verify.sh lint` does not run rustdoc and refuses `--features`.
- The PreToolUse hook `pre-tool-use-checkpoint-doc-gate.py` sits beside the change-review gate in `settings.json`. In a repo with a `Cargo.toml`, it allows a checkpoint commit only when a record matches the commit's tree (the index, or a private index for `-a`) and repo top. It fails closed. Its refusal names the cause and the fix: run `verify.sh doc` last, and commit or remove files the commit leaves out.

**Files:**
- `scripts/direct/verify.sh` — the `doc` verb, `doc_tree`, `doc_record`, and `lint` without rustdoc
- `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` — the checkpoint doc gate
- `scripts/hooks/pre-tool-use-change-review-gate.py` — commit, subject, directory and heredoc patterns that both hooks share
- `commands/unit/checkpoint.md`, `docs/direct/final_gate_commit.md`, `docs/direct/phase_checks.md` — the doc step and the own-call rule

**Binds later work:** the `doc` verb, `doc_tree`, `doc_record`, the `doc` usage lines and `lint`'s refusal of `--features` live in `scripts/direct/verify.sh`; edits there keep them. `test_verify_doc.py`, `test_verify_lint_scope.py` and `test_checkpoint_doc_gate.py` pin them. This repo has no `Cargo.toml`, so its checkpoints need only the own-call rule and no doc record.

**Gotchas:**
- In every repo, the checkpoint commit is the first command in its own Bash call. An earlier command can change the directory or the index.
- After quote removal, any `checkpoint(` text in the call makes every commit in that call a checkpoint.
- A shell expansion in a commit message counts as unreadable, and unreadable counts as a checkpoint in a Cargo repo.
- The change-review hook refuses Bash text that holds example checkpoint commit text. Write such text with file tools.
- The memory gate can hold a doc step for minutes: a one-file crate waited 15 min for a 2.6 s rustdoc run.

**Ruled out:** a git commit-msg hook (it needs an install in each repo); rustdoc on changed members only (a change in one member can break the intra-doc links of another member).

### Phase 2 — `verify.sh final` stops linking examples  · status: done

#### As-built

- `verify.sh final` runs `cargo check --workspace --all-targets`, then `run_nextest --workspace --tests`. The check type-checks every example once; the test build no longer builds and links them again. `--tests` selects every target with `test = true`: by default each lib, bin and integration test. `--lib` is not passed: cargo stops with "no library targets found" (exit 101) in a workspace with no library.
- `UNTESTED_EXAMPLES_PY` runs before any build for `final` and `verify.sh test <package>`. It refuses (exit 2) an example that holds a test, and an example whose metadata `test` is true: `example <name> sets test = true in <manifest>, and examples carry no tests.` then `remove test = true from its [[example]] entry.` One example can get both refusals, the test one first. So no test is lost, and `--tests` selects no example.
- `TEST_TARGETS_PY` no longer selects examples for the `test` verb.
- `commands/unit/checkpoint.md` states the hook's rules: any `checkpoint(` text after quote removal makes every commit in the call a checkpoint, and `--git-dir`/`--work-tree`/`GIT_DIR=`-style overrides and unresolved `-C` directories are refused.

**Measured** (2026-10-28, hana init/catalyst 48dd8e2209, 84 examples in 27 packages, natedev, same file touched before each run): the test build fell from 1,051 build units to 942, and its log holds no example unit. Timing gave no valid result. Load average was 180–220, both test runs stopped early on different failing hana tests, and each step waited about 900 s on the memory gate. Under that load, the test-profile build took 22 min 21 s before and 26 min 51 s after, which is noise. A clean timing needs a quiet natedev under a build hold.

**Files:**
- `scripts/direct/verify.sh` — `final`'s nextest selection, the example scan, `TEST_TARGETS_PY`, header and usage text
- `scripts/direct/test_verify_final_targets.py` (new), `scripts/direct/test_verify_untested_examples.py` — target selection, the no-library workspace, and the `test = true` refusals
- `commands/unit/checkpoint.md` — the hook's recognition and refusal rules

**Gotchas:**
- hana main and bevy_hana keep two `test = true` example lines (fairy_dust:21, hana_clerestory:22) until hana's next main promotion, so their `final` and `test` runs refuse until then. A search for `test = true` misses hana_clerestory's, which pads the `=`; search with `test *= *true`.
- A hana build outside its checkout needs its flake shell (`direnv exec <hana checkout> …`), or mold fails with "library not found: gbm".

**Ruled out:** `--lib --bins --tests` (fails with no library); exact per-target selection from metadata (more code, and `--bins` still links `test = false` bins).
