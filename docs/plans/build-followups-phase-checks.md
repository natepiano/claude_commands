# One end-of-phase check step, and no /clippy

> **Status: APPROVED 2026-10-14 — 2 phases, Phase 2 fenced.** Every lint and style check runs once per phase, in one named step, and nothing at the end of the plan repeats it.
> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

Source: the user, 2026-10-14, via the showrunner: "/clippy is probably something that should go away as an explicit named skill. the unit directors should have an end of phase to run the things that are in clippy - including a style review but we should explicitly name the things that it does … we are too overloaded to do double work." The showrunner's scope, same day: retire `/clippy`; give unit directors one named end-of-phase step that lists mend, fmt, taplo, clippy, doc and the style review; each runs once per phase and nothing at the end of the plan repeats them; findings become fixes by the phase's workers; remove every caller and reference to `/clippy` and `commands/clippy.md`.

Showrunner's approval, 2026-10-14, with three changes: `commands/unit/style_review.md` stays the one source of the style review (the phase review reads it; the user can still run `/unit:style_review` by hand); the reference gate also covers `~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style` and `/etc/nixos`, with hits there reported to the showrunner, not edited; `scan.sh`, `clippy_config.sh` and `clippy.conf` are deleted only once nothing else calls them. Per-phase style review over the phase diff is the agreed trade against a branch-wide pass.

Fence (showrunner): Phase 2 edits `commands/unit/direct.md` and `scripts/delegate/verify.sh`, so it starts only after message-clarity's Phase 10 merges, on the showrunner's word.

## Delegation Context

- **Project:** claude_commands (`~/.claude`) — the slash commands, delegate contracts and scripts that run units and productions
- **Project started:** 2026-10-14T18:40:42.553+00:00
- **Stack:** Markdown prompt contracts; Python 3.13 and zsh/bash scripts under `scripts/`
- **Layout:** `commands/` (slash commands), `commands/unit/` (unit-run commands), `docs/delegate/` (contracts `/unit:direct` reads at the point of use), `scripts/delegate/`, `scripts/lint/`
- **Key files:** `commands/clippy.md` — the skill being retired (source of `<StyleReview/>` at about lines 821–875 and `<FixingGuidelines/>`); `commands/unit/style_review.md` — the style review's one home; `docs/delegate/write_prompt_contract.md` — rules every seat prompt carries; `docs/delegate/dual_review.md` — the phase review; `commands/unit/direct.md` — the run (Phase 2 only); `scripts/delegate/verify.sh` — `lint` and `final` (Phase 2 only)
- **Test lanes:** `scripts/lint/test_*.py`, `scripts/delegate/test_*.py` (run by explicit module name; the folder is not importable for discovery)
- **Build:** none
- **Test:** `python3 -m unittest $(ls scripts/delegate/test_*.py | sed 's#/#.#g; s#\.py$##')` and the same for `scripts/lint`
- **Lint:** basedpyright on touched Python: 0 errors, 0 warnings (it exits 3 on success; read the summary line)
- **Invariants:** the forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) holds in every edited line; a contract file stays the one definition of its tag, and a call site never restates it

## What runs today

- Every phase: the seats' last `verify.sh lint <pkg>` runs mend `--fix` over the workspace, fmt on changed members, `taplo fmt --check`, workspace clippy (`--all-targets --all-features`), and rustdoc on changed members.
- End of plan, `<FinalGate/>`: `verify.sh final` (fmt `--check`, `cargo check --workspace --all-targets`, the full tests), then `commands/clippy.md` read inline with `auto-proceed no-style`, which runs mend, mend `--fix`, clippy, doc and fmt a second time. Then `<RunProjectStyleReview/>` (`commands/unit/style_review.md`) runs the run's single style review through `clippy.md style-only`, over a branch-wide diff from `STYLE_DIFF_BASE`, and reruns `final` and `clippy.md` over its cleanup.
- So mend, clippy, doc and fmt run twice at the end of every Rust plan, and again for each manual `/clippy`.

## The step

`<PhaseChecks/>`, defined in `docs/delegate/phase_checks.md` and named at one point in each phase. It lists exactly these checks, each once per phase:

| Check | Runs in | Over |
| --- | --- | --- |
| cargo mend `--fix` | the seats' last `verify.sh lint` | the workspace |
| fmt | the same `lint` | the changed members |
| taplo fmt `--check` | the same `lint` | the workspace TOML |
| clippy `--all-targets --all-features` | the same `lint` | the workspace |
| rustdoc `-D warnings` | the same `lint` | the changed members |
| style review | the unit director's main review in `<DualReview/>`, reading `commands/unit/style_review.md` | the phase's diff from its baseline, untracked files included |

- `config/lint.conf` switches each check off as today; an off check is reported `Off`, never clean.
- `commands/unit/style_review.md` becomes the style review itself and keeps its command, `/unit:style_review`, for a run by hand. It keeps what `clippy.md`'s `<StyleReview/>` does: load the style guide with `load-rust-style.sh`, walk the checklist rule by rule in a table, and scan for banned words with `banned_words_lib.py --diff`. A violation is opened in the phase's findings ledger like any review finding.
- Every finding goes to the phase's workers through the existing `<FixDispatch/>` repair rounds. A repair changes the tree, so its seat runs `lint` once more; `lint` never reruns on an unchanged tree (as today).
- `clippy.md`'s `<FixingGuidelines/>` move into the repair prompt rules in `docs/delegate/write_prompt_contract.md`: remove dead code rather than marking it, remove an unused binding rather than prefixing `_`, find every reference with LSP before a rename or signature change and reread the changed body, and look up a clippy lint's governing style rule by its `lint:` property before fixing it.
- `cargo mend --fix` rolling back stays a hard stop: `verify.sh` already names it, and the repair prompt says to stop and keep the tree.

## Phases

### Phase 1 — The step is written, and its rules reach the seats  · status: done

**Depends on:** none

#### As-built

- `<PhaseChecks/>` lists the six checks run once per phase: mend `--fix`, fmt, taplo `--check`, clippy and rustdoc in the seats' last `verify.sh lint`, and the style review in the unit director's main review. An off check reports `Off`; findings go through `<FixDispatch/>`; `lint` reruns only on a changed tree; a mend rollback is a hard stop. A phase with no Rust change has no lint run, and its style review runs only the banned-words scan with the checklist marked `Skip (no Rust change)`. Nothing after the last phase repeats them.
- `<StyleReview/>` reviews the range its caller names (`since <ref>`, default `HEAD`) in a directory its caller names or a fresh `mktemp -d` under `/tmp/claude`. It verifies the baseline with `git rev-parse --verify` and stops when the baseline or the tracked diff fails. It appends untracked files, walks the guide's checklist in a table, and runs `banned_words_lib.py --diff`. `<ResolveStyleDiffBase/>`, `<RunProjectStyleReview/>` and the `clippy.md` read are still present; `/unit:direct` reads them.
- Every repair prompt's Work Specification (`write_prompt_contract.md` item 6) carries the fixing rules: remove dead code and unused bindings rather than marking or `_`-prefixing them; run LSP `findReferences` before a rename or signature change and reread the body; find a lint's style rule by its `lint:` property; stop on a mend rollback.

**Files:**
- `docs/delegate/phase_checks.md` — `<PhaseChecks/>`
- `commands/unit/style_review.md` — `<StyleReview/>`, plus the end-of-run sections Phase 2 removes
- `docs/delegate/write_prompt_contract.md` — the fixing rules under item 6

**Binds later work:** the phase review passes the phase baseline and the run's session directory to `<StyleReview/>`; seats never load the style guide.

**Gotchas:** the style review's diff directory is per invocation; a fixed shared path lets concurrent units scan each other's diffs.

### Phase 2 — Unit directors run the step, and /clippy is gone  · status: done

**Depends on:** Phase 1

- `docs/delegate/dual_review.md`: the main review runs `<PhaseChecks/>`'s style review by reading `commands/unit/style_review.md` → `<StyleReview/>` with the phase baseline as its range and `${SESSION_DIR}` as its directory (it writes its diff there, never a shared path), opens each violation in the phase's findings ledger, and records its table. A phase with no Rust change runs only the banned-words scan.
- `docs/delegate/write_prompt_contract.md` item 2: the `rust_style` line's reason ("this run's one style audit happens at the end") becomes the per-phase review: the unit director runs the style review in its main review, so seats still never load the guide.
- `commands/unit/style_review.md`: remove `<ResolveStyleDiffBase/>`, `<RunProjectStyleReview/>` and its `clippy.md` read, and the intro's "`/unit:direct` reads this file twice" paragraph. `<StyleReview/>` already takes `since <ref>` and a caller directory, checks the baseline, and reports `Off` from `LINT_OP_STYLE_REVIEW`.
- `commands/unit/direct.md`: name `<PhaseChecks/>` in the TagReferenceContract table and at the phase's review point. `<FinalGate/>` loses its `clippy.md` step and its `<RunProjectStyleReview/>` step, so `final` runs once with no cleanup rerun. Remove `<ResolveStyleDiffBase/>`, `STYLE_GATE_CONFIG`, `STYLE_REVIEW_DONE`, `STYLE_DIFF_BASE` and their handoff fields, the `/clippy` load rule (line 87) and the "full `clippy` skill" phase rule.
- `scripts/delegate/verify.sh`: `final` drops fmt `--check`, which repeats the phases' fmt. It keeps `cargo check --workspace --all-targets` and the full tests, which no phase runs. Its comment naming `/clippy` is reworded.
- Delete `commands/clippy.md` and `scripts/delegate/style_branch.sh`. Delete `scripts/lint/scan.sh`, `scripts/lint/clippy_config.sh` and `config/clippy.conf` only after confirming nothing else calls them — hooks, the nightly review, cargo-port's lint mirror, any script or settings entry; a file that has another caller stays. `scripts/delegate/progress_timer.sh` stays; the unit notifier uses it.
- Reword every remaining reference: `commands/unit/checkpoint.md`, `commands/unit/ci.md`, `docs/delegate/compose_work_order.md`, `docs/delegate/final_gate_commit.md` (its report names "style review and closing repairs"), `docs/delegate_footprint_review.md`, `docs/delegate_plan_format.md` and `commands/plan/to_phased_plan.md` (the plan's **Style** field goes), `commands/commit_prep.md`, `commands/validate_and_push.md` (it states mend's rollback stop itself instead of pointing at `/clippy`), `commands/lint_config.md` and `scripts/lint/lint_config.sh` (consumer columns), `config/README.md` (the `clippy.conf` section goes), `scripts/lint/invoke.sh` and `scripts/hooks/banned_words_lib.py` (comments).
- Gate: `scripts/lint` and `scripts/delegate` suites and basedpyright on the touched Python. Then `rg -n '/clippy\b|clippy\.md|style_branch|clippy_config|clippy\.conf|scan\.sh'` over this repository returns only as-built history, and the same search over `~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style` and `/etc/nixos` is reported to the showrunner hit by hit — those repositories are not this unit's to edit.

#### Work Order

**Goal:** every unit phase runs `<PhaseChecks/>` once — lint in the workers' last `verify.sh lint`, the style review in the unit director's main review — and nothing at the end of the plan repeats them; `/clippy` and its files are gone and nothing points at them.

**Spec:** the bullets above under this phase heading are the Spec, with these facts:
1. Vocabulary: production branch now says "workers", not "seats" (`docs/long-lived-workers.md`); every edit uses "workers".
2. `commands/unit/direct.md` (current lines): description line 2; state `STYLE_GATE_CONFIG`, `STYLE_REVIEW_DONE`, `STYLE_DIFF_BASE` (45–49); TagReferenceContract row 73 becomes `<PhaseChecks/>` → `docs/delegate/phase_checks.md` and `<StyleReview/>` → `commands/unit/style_review.md` (`/unit:style_review`); the `/clippy` load rule 87 keeps only the `plan:` commands; CoreContract 132 (branch approved in `<ResolveStyleDiffBase/>` goes; the run creates only the remote branch `<PushCheckpoint/>` pushes) and 136 (the inline cleanup exception goes); CompactionContract 380 drops the two STYLE fields; VerificationContract 442–488 (the "full `clippy` skill" and "Workspace breadth belongs to <FinalGate/>" rules point at `<PhaseChecks/>`; the `style_review=off` line becomes: `LINT_OP_STYLE_REVIEW=off` makes the phase style review report `Off`); ExecutionSteps 3 and 10 go and the list renumbers; the `<ResolveStyleDiffBase>` (734–743) and `<RunProjectStyleReview>` (1303–1311) stubs go; 1437–1438 drop the style-state sentence; `<FinalGate/>` (about 1445–1475) keeps `verify.sh final` once and drops step 2 (`clippy.md auto-proceed no-style`) and step 4 (the style review and its rerun), renumbering; RunSummary 1499's **Style review** line reports each phase's style result. Name `<PhaseChecks/>` at the phase's review point (where `<DualReview/>` is called after implementation).
3. `docs/delegate/dual_review.md` `<DualReview/>` step 4: on pass 1 only, the main review runs `<PhaseChecks/>`'s style review by reading `commands/unit/style_review.md` → `<StyleReview/>` with `since <phase baseline commit>` and `${SESSION_DIR}` as its directory; violations are opened in the ledger (`findings.py open --caught-by main`). Phase baseline = HEAD when the phase's implementation launched (record it in `${SESSION_DIR}/phase_baseline` at `<LaunchImplementation/>` step 1 in `docs/delegate/launch_implementation.md`). Remove the "style-guide conformance is out of scope" wording only if it now contradicts; the blind lenses still never audit style.
4. `scripts/delegate/verify.sh` `final` (about line 1480): drop `fmt_cargo --check`; keep `cargo check --workspace --all-targets` and `run_nextest`; reword the comment at about line 36 naming `/clippy`. Update any test in `scripts/delegate/test_*.py` that pins the old `final` steps.
5. Deletion caller check is done: nothing outside `commands/clippy.md`, `commands/lint_config.md`, `config/README.md` and the files themselves names `scripts/lint/scan.sh`, `scripts/lint/clippy_config.sh` or `config/clippy.conf` (no hook, settings entry, nightly review, cargo-port or `~/rust` file). Delete all three, plus `commands/clippy.md` and `scripts/delegate/style_branch.sh`.

**Files:**
- `commands/unit/direct.md` — as Spec 2
- `docs/delegate/dual_review.md` — style review in the main review
- `docs/delegate/launch_implementation.md` — record the phase baseline
- `commands/unit/style_review.md` — remove the end-of-run sections, intro paragraph and `clippy.md` read
- `docs/delegate/write_prompt_contract.md` — item 2's `rust_style` reason
- `docs/delegate/phase_checks.md` — only if a wording must change
- `scripts/delegate/verify.sh` — `final` without fmt, comment
- `commands/clippy.md`, `scripts/delegate/style_branch.sh`, `scripts/lint/scan.sh`, `scripts/lint/clippy_config.sh`, `config/clippy.conf` — deleted
- `commands/unit/checkpoint.md`, `commands/unit/ci.md`, `docs/delegate/compose_work_order.md`, `docs/delegate/final_gate_commit.md`, `docs/delegate_footprint_review.md`, `docs/delegate_plan_format.md`, `commands/plan/to_phased_plan.md`, `commands/commit_prep.md`, `commands/validate_and_push.md`, `commands/lint_config.md`, `scripts/lint/lint_config.sh`, `config/README.md`, `scripts/lint/invoke.sh`, `scripts/hooks/banned_words_lib.py` — reworded

**Seats:** 2 writers — the run's contracts and the scripts they run, split from the reference cleanup
- `impl` — `commands/unit/direct.md`, `docs/delegate/dual_review.md`, `docs/delegate/launch_implementation.md`, `commands/unit/style_review.md`, `docs/delegate/write_prompt_contract.md`, `docs/delegate/phase_checks.md`, `scripts/delegate/verify.sh` and its tests
- `review` (opens as impl) — the five deletions and every reworded file in the last Files line

**Constraints from prior phases:** `<StyleReview/>` takes `since <ref>` and a caller directory, verifies the baseline, and reports `Off` from `LINT_OP_STYLE_REVIEW`; the repair rules already sit under `write_prompt_contract.md` item 6.

**Acceptance gate:** `scripts/delegate`, `scripts/lint` and `scripts/lib` suites green (explicit module names); basedpyright `0 errors, 0 warnings` on touched Python; `rg -n '/clippy\b|clippy\.md|style_branch|clippy_config|clippy\.conf|scan\.sh|ResolveStyleDiffBase|RunProjectStyleReview|STYLE_DIFF_BASE|STYLE_REVIEW_DONE|STYLE_GATE_CONFIG'` over the repository returns only `docs/as-built/` and `docs/plans/` lines; the same search over `~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style` and `/etc/nixos` is listed in the summary, not edited.

As-built at run end: `docs/as-built/session-notifier.md` (its `/clippy launches progress_timer.sh` line), `docs/as-built/build-memory-admission.md`, and the as-built that describes `/unit:direct`'s gates.

Out of scope, stated: editing other repositories (their hits are reported, above), and memories that mention `/clippy` (six memory files under `~/.claude/projects/`); this run fixes memories it owns only.
