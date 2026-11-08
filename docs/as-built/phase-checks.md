# Phase checks

**What it is.** Every `/unit:direct` phase runs one named step, `<PhaseChecks/>`, made of six checks: cargo mend `--fix`, fmt, taplo, clippy, rustdoc and the style review. Each check has one place in the phase. Lint can repeat clippy and fmt to apply multiply-add repairs. The phase's workers fix the remaining failures. Run end does not repeat lint or style review. Each Cargo checkpoint needs its rustdoc pass. There is no separate `/clippy` skill.

## How it works

**The six checks** (defined in `docs/direct/phase_checks.md`)

| Check | Runs in | Over |
| --- | --- | --- |
| cargo mend `--fix` | the workers' last `verify.sh lint` | the workspace |
| fmt (nightly) | the same `lint` | the named package plus every member that differs from HEAD |
| taplo fmt `--check` | the same `lint` | the workspace TOML, run from the cargo workspace root |
| clippy `--all-targets --all-features` | the same `lint` | the workspace |
| rustdoc `-D warnings` | `verify.sh doc`, run by the unit director as the last command before the checkpoint commit | the workspace |
| style review | the reviewer worker assigned by `<ReviewPromptContract/>` | the phase range selected under `<ReviewPromptContract/>` |

`verify.sh lint` runs its stages in this order: mend, fmt, taplo, clippy.
Mend rewrites the code first.
Fmt formats what mend wrote.
Clippy judges first.
A passing judgment skips repairs.
A failed judgment starts up to three `cargo clippy --fix` and fmt passes, followed by a final judgment.
Every clippy run in this path uses `--keep-going`.
`invoke_clippy_fix` removes `CARGO_MAKEFLAGS`, `MAKEFLAGS` and `MFLAGS` from cargo's environment on Linux and macOS.
It sets `--jobs` from `clippy_fix_jobs` in `config/lint.conf`.
The valid range is 1..9999. Missing or invalid values use 8; invalid values print one warning.
Plain `invoke_clippy` keeps the shared build pool.
Fix steps keep memory admission, lint gating and build log records, including the `--fix` argument.
The director runs workspace rustdoc through `verify.sh doc` after staging, immediately before the checkpoint commit.
The checkpoint requires its pass record for the exact tree.
[Lint-step fix](build-followups-mul-add.md#lint-step-fix) defines the pass rules.

**Files and their roles**

- `docs/direct/phase_checks.md` — the one definition of `<PhaseChecks/>`.
- `commands/unit/style_review.md` — `<StyleReview/>`, the manual entry point and its range options.
- `docs/direct/launch_implementation.md` — the launcher's `phase_reservation.py claim` saves the repository's full `HEAD` id to `${PHASE_DIR}/phase_baseline` and `git status --short` to `${PHASE_DIR}/progress_baseline_status` on the first launch, before the worker opens; a git failure stops the launch. Later launches, including repairs and reopened phases, keep both baselines. Ad hoc work uses `${SESSION_DIR}/phases/adhoc`.
- `docs/direct/dual_review.md` — `<ReviewPromptContract/>` owns the style assignment's completion and retry rules, baseline and artifact paths. It assigns a reviewer until the phase result returns, including after cancellation. The director reads the findings ledger and reported result.
- `docs/direct/write_prompt_contract.md` — item 2 keeps implementation, fix and test workers from loading the guide; reviewer prompts follow `<ReviewPromptContract/>`. Item 6 supplies the repair rules.
- `commands/unit/direct.md` — names `<PhaseChecks/>` in TagReferenceContract and runs it at ExecutionSteps step 6, within `<DualReview/>`.
- `docs/direct/verification.md` — VerificationContract names `verify.sh lint` as the phase lint checks under `<PhaseChecks/>`.
- `docs/direct/repair_rounds.md` — holds `<FixDispatch/>`.
- `docs/direct/final_gate_commit.md` — holds `<FinalGate/>` and the RunSummary `**Style review:**` line, which records each phase's reviewed range and result.
- `scripts/lint/style_review.py` and `config/review/style.md` — the shared input/report runner and its one read-only assignment template.
- `scripts/direct/verify.sh` — the `lint`, `doc` and `final` arms. There is no `fmt` arm.
- `config/lint.conf` and `scripts/lint/lint_config.sh` — the on/off switches.

**The style review**

`scripts/lint/style_review.py` prepares inputs and captures results for both
`assign.sh --role review --style-since`, the alternate `review.sh` entry point
and the manual command. `config/review/style.md` owns
the read-only assignment, guide load and checklist protocol. The runner saves
the report and completion state; the reviewer returns text and never writes
files. `<ReviewPromptContract/>` defines phase paths and retries.

**How findings flow**

Broad review runs `adversary`, `contract`, and armed `rerun` lenses.
The [daily simplify run](simplify-lens.md) starts after the code ready notice.
Its held items stay outside repair batches.
The contract reviewer retains the style assignment.

- The reviewer includes style violations in its normal findings summary. `<Synthesize/>` confirms, deduplicates and opens them with `--caught-by worker --lens contract`, using the same severity gate and closure process as code findings. The reviewer does not write the ledger.
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

**A phase with no Rust change** has no lint run. `<StyleReview/>` defines its remaining review scope.

**By hand.** `/unit:style_review` uses the same preparer without an active run; see `<StyleReview/>` for range options and result capture.

## Invariants

- Each of the six checks runs at the place `<PhaseChecks/>` names.
  The lint check owns its multiply-add repairs and the format and judgment that follow them.
- After the last phase, lint and style review do not repeat: `verify.sh final` stays check plus tests, `verify.sh` has no `fmt` arm, and `<FinalGate/>`, `<FinalGateCommit/>` and `/unit:checkpoint` do not lint, reformat or style-review. Each later Cargo checkpoint still needs its rustdoc pass.
- `config/review/style.md` is the one reviewer protocol. Callers use the shared preparer and never restate its steps.
- The assigned reviewer owns phase style review; the director, implementation, fix and test workers do not load the guide. The reviewer loads it once per applicable phase under `<StyleReview/>`.
- A contract file stays the one definition of its tag. Call sites name `<PhaseChecks/>` and `<StyleReview/>` and never restate them.
- An off check reports `Off`, never clean, and no replacement check runs.
- A cargo mend `--fix` rollback is a hard stop. `verify.sh` names it (and points at `/mend_fix`), and the repair prompt says to stop and keep the tree for inspection.
- `lint` reruns only after a repair changes the tree.
- An invalid baseline or failed diff leaves style owed; code review still runs.
- Phase prompts never call `verify.sh final`, raw cargo, or their own cargo flags.

## Calibration / gotchas

- **The diff directory belongs to one invocation.** The runner allocates a fresh directory for every attempt; phase paths come from `<ReviewPromptContract/>`. A fixed shared path would let concurrent units scan each other's diffs.
- **The baseline is the phase's starting `HEAD`**, saved before dispatch; `<ReviewPromptContract/>` defines the range end and working-tree scope.
- **fmt picks members by what differs from HEAD**, not from the phase baseline. Mend can rewrite a member the phase did not edit, and fmt covers that member too. Rustdoc covers the whole workspace.
- **Repos outside this one** (`~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style`, `/etc/nixos`) may still name `/clippy`. Hits there are reported to the showrunner, not edited from here.
- **Ruled out: a checkpoint gate that requires every PhaseChecks finding resolved.** It contradicts the ledger's severity gate (nits never gate; later rounds gate blockers only) and deadlocks against `<Synthesize/>`, which converges while such findings stay open.

## Why

- **Per-phase style review over the phase diff** is the chosen trade against one branch-wide pass at run end. Each phase's violations reach the workers who wrote the code, while it is still in their context, through the same repair rounds as every other finding.
- **No double work.** Mend, clippy and fmt run in the phase's `lint`. Run end does not repeat them. Rustdoc runs before each Cargo checkpoint.
- **The fixing rules live in the repair prompt** (`write_prompt_contract.md` item 6) because the repair workers do the fixing: remove dead code and unused bindings rather than marking them or adding a `_` prefix; run LSP `findReferences` before a rename or signature change and reread the body; find a lint's style rule by its `lint:` property; stop on a mend rollback. Workers get these rules without loading the style guide.
- **One named step** makes the check list explicit: each check has a stated place and scope; checkpoint rustdoc has no off state.
