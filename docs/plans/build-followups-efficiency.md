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
  - `scripts/direct/verify.sh` (2,434 lines). `lint` runs mend, fmt, taplo, clippy fix, fmt, clippy judgment, then rustdoc on the members that differ from HEAD (`CHANGED_MEMBERS_PY`, `invoke_doc`, near line 2395). Stages are gated by `config/lint.conf` through `lint_config_enabled` and `lint_config_skip_notice`. The header (lines 1–135) documents every verb, the pass records under `<session>/verify_cache/` (keyed by the judged tree, arguments, rustc, lint.conf, verify.sh, invoke.sh and environment), the build-log call record (`note_event`), and the Mac offload. `final` runs `cargo check --workspace --all-targets` and `run_nextest --workspace`.
  - `scripts/hooks/pre-tool-use-change-review-gate.py` — the model for the new hook: it finds `git commit` with a `checkpoint(...)` subject in a Bash call (`COMMIT`, `SUBJECT`, `DIRECTORY`, `ALL`, `staged()`), reads `-m`, heredoc and `-F` text, and denies through `hookSpecificOutput.permissionDecision`. It is registered in `settings.json` near line 261. Its tests: `scripts/hooks/test_change_review_gate.py`.
  - `docs/direct/phase_checks.md` — `<PhaseChecks/>`: the table puts rustdoc in "the workers' last `verify.sh lint`". `commands/unit/checkpoint.md` — `<CheckpointCommit/>` (the commit message format at line ~155). `config/lint.conf` — `doc=on` (line 46). `commands/unit/direct.md`, `docs/as-built/nextest-width-and-lint-scope.md`, `docs/as-built/phase-checks.md`, `docs/as-built/build-followups-mul-add.md` mention rustdoc in lint.
  - Tests: `scripts/direct/test_verify_lint_scope.py` (lint scope), other `scripts/direct/test_verify_*.py`.
- **Gate:** `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <worktree> --tests-only` from the repo root, and basedpyright with zero errors and zero warnings on every touched folder.

## Phases

### Phase 1 — Rustdoc once per phase, enforced at the checkpoint  · status: done
**Depends on:** none

#### Work Order

**Goal:** No phase of a Rust repository can be checkpointed without a passing rustdoc run on the exact tree it commits. `verify.sh lint` no longer runs rustdoc.

**Spec:** Decisions, settled; do not reopen.
1. **New verb `verify.sh doc`.** It runs `cargo doc --workspace --no-deps` with warnings denied (the same flags and features handling `invoke_doc` uses today), over the whole workspace, through the same cargo token and memory gate as `lint`. Whole workspace, not changed members: a change in one member can break another member's intra-doc links. It ignores `config/lint.conf`'s `doc` switch. If the switch is off, it prints one line saying that the phase rustdoc check cannot be switched off. Measure and report one hana workspace run's wall time and memory peak from the build log.
2. **The doc pass record.** When `verify.sh doc` exits 0, it writes a durable record to `~/.local/state/verify/doc_pass/`. The record is keyed by the git tree id of the judged tree: the working tree with untracked, non-ignored files included, computed with a temporary index (`GIT_INDEX_FILE`, `git add -A`, `git write-tree`). It is never the real index, and the real index must not change. The record holds the repo top level, the tree id, the time and the build-log call id. A run whose tree changed while it ran writes no record, the same rule as the existing pass records. These records do not live in the session folder, so `end_session.sh` does not delete them. Prune records older than 14 days on each write.
3. **`lint` drops rustdoc.** Remove the rustdoc stage, `CHANGED_MEMBERS_PY` if nothing else uses it, and the lint usage text that names rustdoc. `--features` handling moves to `doc`. Existing lint pass records stay valid only if the key machinery says so; do not special-case them.
4. **New hook `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py`, registered in `settings.json` beside the change-review gate.**
   - It acts on a Bash `git commit` whose subject is `checkpoint(...)`. Reuse the change-review gate's parsing. Share it by import if that is clean, and never by copying it.
   - It acts only when the commit's repository top level has a `Cargo.toml`.
   - It computes the tree the commit writes: the index, or with `-a`/`--all` a temporary index holding every tracked change. It allows the commit only when a doc pass record exists for that tree id and the same repo top level.
   - **It fails closed.** In a Cargo repository, any error (git failure, unreadable record, parse error) denies, with a reason that names the cause and the fix. This is the one hook allowed to block on its own failure, because the user made this check non-negotiable. Outside a Cargo repository, and for every other commit, it allows without reading anything.
   - The denial reason says to run `~/.claude/scripts/direct/verify.sh doc` as the last command before the commit. When the working tree holds untracked or unstaged files that the commit leaves out, it also says to commit or remove them, because the judged tree must equal the committed tree.
5. **Procedure docs say the same thing everywhere** (user rule: docs match current reality).
   - `<PhaseChecks/>`: the rustdoc row becomes "`verify.sh doc`, run by the unit director as the last command before the checkpoint commit | the workspace". State that `lint.conf` cannot switch it off. The paragraph "`config/lint.conf` switches each check off" excepts rustdoc.
   - `<CheckpointCommit/>`: one step in a Rust repository, `verify.sh doc` after staging and before `git commit`. On failure, it is a repair round through `<FixDispatch/>`, then `doc` again. The hook refuses the commit otherwise. A rerun after a plan-doc edit compiles nothing new and takes seconds.
   - `verify.sh` header and usage, `config/lint.conf` comment, `commands/unit/direct.md`, and every as-built doc that says lint runs rustdoc.
   - Grep the repository for `rustdoc` and `invoke_doc` and fix every stale sentence.
6. **Workers may run `verify.sh doc` any time.** The Work Order template does not require it. The unit director runs it at the checkpoint.

Tests the `test` worker writes:
- verify.sh: `doc` writes a record keyed by the judged tree, writes none on failure or when the tree changes mid-run, and ignores `doc=off`. `lint` no longer invokes rustdoc. Use the existing `test_verify_*.py` fake-cargo patterns.
- Hook: it allows a non-checkpoint commit and a checkpoint outside a Cargo repo. In a Cargo repo it allows a checkpoint whose index tree has a record. It denies a checkpoint with no record, with a record for another tree, with `-a` and an unstaged change, and on an internal error. It handles `-C <dir>`, heredoc and `-F` messages, and a call that has two commands.

**Files:**
- `scripts/direct/verify.sh` — the `doc` verb, its record, and `lint` without rustdoc
- `scripts/direct/test_verify_doc.py` — new: `doc` record and switch tests
- `scripts/direct/test_verify_lint_scope.py` — `lint` no longer runs rustdoc
- `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py` — new hook
- `scripts/hooks/test_checkpoint_doc_gate.py` — new hook tests
- `settings.json` — register the hook
- `docs/direct/phase_checks.md` — the rustdoc row
- `commands/unit/checkpoint.md` — the `verify.sh doc` step
- `commands/unit/direct.md` — sentences that say lint runs rustdoc
- `config/lint.conf` — the `doc` comment
- `docs/as-built/` — every as-built doc the grep finds

**Workers:** 1 writer + 1 tester. The tests depend on the behavior and not on the code, so they split cleanly.
- `impl` — `scripts/direct/verify.sh`, `scripts/hooks/pre-tool-use-checkpoint-doc-gate.py`, `settings.json`, and every doc file above
- `test` — `scripts/direct/test_verify_doc.py`, the `test_verify_lint_scope.py` update, `scripts/hooks/test_checkpoint_doc_gate.py`, all written from the Spec alone

**Constraints from prior phases:** none.

**Acceptance gate:** the gate passes, basedpyright is clean on touched folders, and a live check in a throwaway Cargo repo under `/tmp/claude` shows both paths: a checkpoint commit is refused before `verify.sh doc` and allowed after it.

### Phase 2 — `verify.sh final` stops linking examples  · status: todo
**Depends on:** Phase 1 (same file, `scripts/direct/verify.sh`)

#### Work Order

**Goal:** `verify.sh final` no longer builds and links workspace examples, and every phase still compiles every example.

**Spec:**
- Showrunner and hana request, 2026-10-27: `final` runs `cargo check --workspace --all-targets` and `run_nextest --workspace`. nextest's default target selection follows `cargo test`, so it builds and links every example; hana has 3. Pass `--lib --bins --tests` to the nextest build in `final` so examples are not linked.
- Keep the check that a broken example is still caught. `verify.sh lint` runs `cargo clippy --workspace --all-targets --all-features` in every phase, so clippy still type-checks every example. Confirm this in the code. Leave `final`'s `cargo check --all-targets` as it is.
- Measure the gain on hana from the build log (`buildlog`): wall time and build time of `final` before and after, on the same tree and under similar load. Report the numbers in the summary.

**Files:**
- `scripts/direct/verify.sh` — `final` nextest target selection, and its usage text
- `scripts/direct/test_verify_final_targets.py` — new: `final` passes `--lib --bins --tests` to nextest

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/direct/verify.sh`
- `test` — `scripts/direct/test_verify_final_targets.py`

**Constraints from prior phases:** Phase 1 owns the `doc` verb and the `lint` changes in the same file.

**Acceptance gate:** the gate passes, basedpyright is clean on the touched files, and the measured before and after times are recorded in the plan and the efficiency ledger line.
