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
**Depends on:** Phase 1 (same file, `scripts/direct/verify.sh`)

#### Work Order

**Goal:** `verify.sh final` no longer builds and links workspace examples, and every example is still compiled at the final gate.

**Spec:**
- Showrunner and hana request, 2026-10-27: `final` runs `cargo check --workspace --all-targets` and `run_nextest --workspace`. nextest's default target selection follows `cargo test`, so it builds and links every example; hana has 3. Pass `--tests` to the nextest run in `final` (`run_nextest --workspace --tests`) so examples are not linked. `--tests` selects every target with `test = true`: by default each lib, bin and integration test. Do not add `--lib`: it fails with "no library targets found" in a workspace with no library (review round 1). No test is lost: `final` first runs `UNTESTED_EXAMPLES_PY` (near line 1660) and exits 2 when any example holds a test.
- The same scan also refuses an example that sets `test = true` (its metadata `test` is true), exit 2 before any build: `example <name> sets test = true in <manifest>, and examples carry no tests.` then `remove test = true from its [[example]] entry.` Such an example holds no test, so `--tests` would link it for nothing. Hana has two, left from its phase 15 (review round 1; the hana owner removes them). `TEST_TARGETS_PY` stops selecting examples, and the header line for `test` stops naming them.
- Examples stay compiled. `final`'s `cargo check --workspace --all-targets` type-checks every example at the final gate; leave it as it is. In a phase with Rust changes and clippy enabled, `verify.sh lint`'s `invoke_clippy --workspace --all-targets --all-features` also type-checks every example. Do not change the switch policy.
- Update `final`'s header and usage text (near line 143) to say its nextest run skips example targets and why no test is lost.
- Procedure text for the checkpoint hook: in `commands/unit/checkpoint.md`, say that the hook treats a call as a checkpoint when its text holds `checkpoint(` anywhere after quote removal (a body line or an example counts too), and that it refuses `--git-dir`/`--work-tree`/`GIT_DIR=`-style overrides and `-C` directories it cannot resolve (shell expansion, glob). Each refusal names the fix. Check the wording against `scripts/hooks/test_checkpoint_doc_gate.py`.
- Measurement (unit director, after the phase lands): on `~/rust/hana`, same tree, compare `final` with the installed verifier (`~/.claude/scripts/direct/verify.sh`, links examples) against the worktree verifier. Before each run, touch the same workspace source file so both rebuild the same set. Take wall time and build time from the build log (`~/.local/state/buildlog/index.sqlite`, table `steps`); report memory-gate and build-lock waits separately from build time. Record before and after in this plan's as-built and in the efficiency line of `~/.local/state/nightly-review/ledger.md`. Memory-gate repairs are outside this phase (sent to the showrunner 2026-10-27).

**Files:**
- `scripts/direct/verify.sh` — `final` nextest target selection, and its header and usage text
- `commands/unit/checkpoint.md` — the hook's recognition and refusal rules
- `scripts/direct/test_verify_final_targets.py` — new: `final` passes `--tests` to nextest; passes in a workspace with no library; still runs `cargo check --workspace --all-targets`; a failed check stops before nextest; an example with a test or with `test = true` exits 2 before any build
- `scripts/direct/test_verify_untested_examples.py` — its exact `nextest run --workspace` assertion (near line 377) takes the new flags

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/direct/verify.sh`, `commands/unit/checkpoint.md`
- `test` — `scripts/direct/test_verify_final_targets.py`, `scripts/direct/test_verify_untested_examples.py`

**Constraints from prior phases:** Phase 1 owns the `doc` verb, `doc_tree`, `doc_record`, the `doc` usage lines and `lint`'s refusal of `--features` in the same file; keep them unchanged, and keep `scripts/direct/test_verify_doc.py`, `scripts/direct/test_verify_lint_scope.py` and `scripts/hooks/test_checkpoint_doc_gate.py` green. This repository has no `Cargo.toml`: its checkpoint needs the own-call rule but no doc record.

**Acceptance gate:** the gate passes, basedpyright is clean on the touched files, and the measured before and after times are recorded in the plan and the efficiency ledger line.
