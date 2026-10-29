# Phase checks

**What it is.** Every `/unit:direct` phase runs one named step, `<PhaseChecks/>`, made of seven checks: the assertion guard, cargo mend `--fix`, fmt, taplo, clippy, rustdoc and the style review. Each check has one place in the phase. Lint can repeat clippy and fmt to apply multiply-add repairs. The phase's workers fix the remaining failures. Run end does not repeat lint or style review. Each Cargo checkpoint needs its rustdoc pass. There is no separate `/clippy` skill.

## How it works

**The seven checks** (defined in `docs/direct/phase_checks.md`)

| Check | Runs in | Over |
| --- | --- | --- |
| assertion guard (`scripts/direct/assertion_guard.py`) | the first stage of every `verify.sh lint`, and the checkpoint commit hook | `.rs` and `.py` changes from the merge base with origin's default branch |
| cargo mend `--fix` | the workers' last `verify.sh lint` | the workspace |
| fmt (nightly) | the same `lint` | the named package plus every member that differs from HEAD |
| taplo fmt `--check` | the same `lint` | the workspace TOML, run from the cargo workspace root |
| clippy `--all-targets --all-features` | the same `lint` | the workspace |
| rustdoc `-D warnings` | `verify.sh doc`, run by the unit director as the last command before the checkpoint commit | the workspace |
| style review | the unit director's main review in `<DualReview/>`, pass 1 only | the phase's diff from `${PHASE_DIR}/phase_baseline`, untracked files included |

`verify.sh lint` runs its stages in this order: assertion guard, mend, fmt, taplo, clippy. The assertion guard reads one `git diff -U0` and the commit messages from the base, and reports removed, weaker, changed (a note that does not block), test removed and test turned off; it judges a multi-line assertion as one whole statement and ignores whitespace, so a reflow or a move is no finding. A finding blocks unless an `Assertion-Change: <path>::<test> — <why>` line in the run's work orders (`UNIT_SESSION_DIR`) or a commit message names it. It takes about 0.5 s on the 87,000-line cargo-liner-tile checkpoint e21d5b93. Mend rewrites the code first. Fmt formats what mend wrote. The clippy stage runs `cargo clippy --fix` and fmt, repeated while a pass changes the tree, at most three times. Then it updates the cache key and judges. Every clippy run in this path uses `--keep-going`. Workspace rustdoc runs separately through `verify.sh doc` after staging, immediately before the checkpoint commit; its exact-tree pass record is mandatory. See [Lint-step fix](build-followups-mul-add.md#lint-step-fix) for the pass rules.

**Files and their roles**

- `docs/direct/phase_checks.md` — the one definition of `<PhaseChecks/>`.
- `commands/unit/style_review.md` — the one definition of `<StyleReview/>`, and the `/unit:style_review [since <ref>]` command for a run by hand.
- `docs/direct/launch_implementation.md` — before a phase's first dispatch, step 1 saves `git rev-parse HEAD` to `${PHASE_DIR}/phase_baseline` (`${SESSION_DIR}/phases/<phase id>/`) and stops if that fails. Repairs keep the baseline.
- `docs/direct/dual_review.md` — on pass 1 the main review reads `style_review.md` and applies `<StyleReview/>` with `since <phase_baseline commit>` and `${SESSION_DIR}` as the diff directory. It saves the result, `Off` or an error included, to `${SESSION_DIR}/phase_style_review_<phase-id>.md`. Blind reviewers never audit style, and later passes do not walk the checklist again.
- `docs/direct/write_prompt_contract.md` — item 2 tells every worker not to open `rust_style` or run `load-rust-style.sh`; item 6 puts the fixing rules into every repair prompt's Work Specification.
- `commands/unit/direct.md` — names `<PhaseChecks/>` in TagReferenceContract, names `verify.sh lint` in VerificationContract as the phase lint checks under `<PhaseChecks/>`, and runs the step at ExecutionSteps step 6 (`<PhaseChecks/>` at `<DualReview/>`). It also holds `<FixDispatch/>`, `<FinalGate/>`, and the RunSummary `**Style review:**` line (each phase's reviewed range and result).
- `scripts/direct/verify.sh` — the `lint`, `doc` and `final` arms. There is no `fmt` arm.
- `config/lint.conf` and `scripts/lint/lint_config.sh` — the on/off switches.

**The style review** (`<StyleReview/>`)

1. Reads the switches with `lint_config.sh export`. With `LINT_OP_STYLE_REVIEW=off` it reports `Style review: Off` and stops.
2. Makes a diff directory for this invocation: the directory the caller names, or a fresh `mktemp -d /tmp/claude/style-review.XXXXXX`.
3. Checks the baseline with `git rev-parse --verify`. An invalid baseline or a failed tracked `git diff <since>` stops the review; neither is ever read as an empty or passing review.
4. Appends untracked files as all-additions diffs.
5. When any path in the diff is `.rs`, `Cargo.toml` or `Cargo.lock`, loads the guide with `load-rust-style.sh` and walks every numbered checklist rule against the additions, one table row per rule (`Pass` / `VIOLATION … at path:line` / `Skip`).
6. Always runs `banned_words_lib.py --diff`.
7. Opens every violation in the caller's ledger. With no ledger (a run by hand), violations are reported in the table.

**How findings flow**

- The main review opens each style violation with `findings.py open --caught-by main` (severity, title, file, line) and keeps the returned ids for `<Synthesize/>`, which does not open them again.
- A lint failure is fixed by the worker that ran `lint`, before it reports done.
- Workers start `test` as soon as the code is ready. Lint and test can run in either order, and both must pass before the phase is done. A test counts only if no later lint changed a file; when `git diff --stat` shows that a later lint changed a file, the worker runs test once more. Neither runs again on an unchanged tree. `<VerificationContract/>` defines these rules.
- PhaseChecks findings pass the ledger's severity gate like any other finding: round 1 gates blockers and minors, later rounds gate blockers only, nits never gate.
- A `dispatch` verdict sends `<FixDispatch/>` repair rounds to the phase's workers. A repair prompt's Verification includes `lint` after the worker's last edit, when the phase has a lint run.
- `lint` never reruns on an unchanged tree: in a run session `verify.sh` caches a pass or failure keyed on the tree and replays it.

**Switches.** `config/lint.conf` has keys for `mend`, `style_review`, `clippy`, `doc` and `fmt` (plus `sweep`, which is not a phase check).

- An off cargo stage prints a `SKIPPED` line and `lint` still exits 0. An off check is reported `Off`, never clean, and no replacement check runs.
- taplo has no switch and always runs.
- `verify.sh doc` ignores the `doc` switch; checkpoint rustdoc cannot be disabled.
- `check`, `test`, and the check and tests inside `final` are never switched.

**Run end.**

- `<FinalGate/>` runs `verify.sh final` once: `cargo check --workspace --all-targets`, then nextest `--workspace --tests`, which links no example. It has no fmt, no lint and no style review.
- On failure, a synthetic `final` phase opens the failures in the ledger and repairs them through `<FixDispatch/>`, then the gate reruns.
- `<FinalGateCommit/>` commits only synthetic-phase repairs and source-comment edits from the last plan review. After staging in a Cargo repository, it runs `verify.sh doc` as its own command. On failure, it repairs through `<FixDispatch/>`, stages, and runs `doc` again. It does not reformat; the repair workers' `lint` covers formatting.

**`/unit:checkpoint`** requires a smoke result and a `converged` phase findings ledger. Every Cargo checkpoint needs an exact-tree pass from workspace `verify.sh doc`, run as its own command after staging and immediately before committing. A failure returns through `<FixDispatch/>`, then staging and `doc` repeat. In every repository, the checkpoint commit goes first in its Bash call; commands after it are allowed. The hook reads literal or reused subjects and treats an unreadable subject as a checkpoint. See `<CheckpointCommit/>` (`commands/unit/checkpoint.md`) for all cases and message forms. It does not rerun formatting.

**A phase with no Rust change** has no lint run. Its style review skips the guide, marks the checklist `Skip (no Rust change)`, and runs only the banned-words scan.

**By hand.** `/unit:style_review [since <ref>]` reviews the working tree against `HEAD` by default (staged, unstaged and untracked), or against `<ref>`. It needs no active run and reports violations in its table.

## Invariants

- Each of the seven checks runs at the place `<PhaseChecks/>` names. The lint check owns its multiply-add repair rounds and the format and judgment that follow them.
- After the last phase, lint and style review do not repeat: `verify.sh final` stays check plus tests, `verify.sh` has no `fmt` arm, and `<FinalGate/>`, `<FinalGateCommit/>` and `/unit:checkpoint` do not lint, reformat or style-review. Each later Cargo checkpoint still needs its rustdoc pass.
- `commands/unit/style_review.md` is the one source of the style review. The phase review reads it at the point of use and never restates it.
- Workers never load the style guide. The unit director's main review is the only place style is audited in a phase.
- A contract file stays the one definition of its tag. Call sites name `<PhaseChecks/>` and `<StyleReview/>` and never restate them.
- An off check reports `Off`, never clean, and no replacement check runs.
- A cargo mend `--fix` rollback is a hard stop. `verify.sh` names it (and points at `/mend_fix`), and the repair prompt says to stop and keep the tree for inspection.
- `lint` reruns only after a repair changes the tree.
- An invalid baseline or a failed tracked diff stops the style review; it never passes.
- Phase prompts never call `verify.sh final`, raw cargo, or their own cargo flags.

## Calibration / gotchas

- **The diff directory belongs to one invocation.** A run passes `${SESSION_DIR}`; a run by hand gets a fresh `mktemp -d`. A fixed shared path would let concurrent units scan each other's diffs.
- **The baseline is the phase's starting `HEAD`**, saved before dispatch, so the review covers committed, staged, unstaged and untracked work since then.
- **fmt picks members by what differs from HEAD**, not from the phase baseline. Mend can rewrite a member the phase did not edit, and fmt covers that member too. Rustdoc covers the whole workspace.
- **Repos outside this one** (`~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style`, `/etc/nixos`) may still name `/clippy`. Hits there are reported to the showrunner, not edited from here.
- **Ruled out: a checkpoint gate that requires every PhaseChecks finding resolved.** It contradicts the ledger's severity gate (nits never gate; later rounds gate blockers only) and deadlocks against `<Synthesize/>`, which converges while such findings stay open.

## Why

- **Per-phase style review over the phase diff** is the chosen trade against one branch-wide pass at run end. Each phase's violations reach the workers who wrote the code, while it is still in their context, through the same repair rounds as every other finding.
- **No double work.** Mend, clippy and fmt run in the phase's `lint`. Run end does not repeat them. Rustdoc runs before each Cargo checkpoint.
- **The fixing rules live in the repair prompt** (`write_prompt_contract.md` item 6) because the repair workers do the fixing: remove dead code and unused bindings rather than marking them or adding a `_` prefix; run LSP `findReferences` before a rename or signature change and reread the body; find a lint's style rule by its `lint:` property; stop on a mend rollback. Workers get these rules without loading the style guide.
- **One named step** makes the check list explicit: each check has a stated place and scope; checkpoint rustdoc has no off state.
