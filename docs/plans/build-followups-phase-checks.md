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

### Phase 2 — Unit directors run the step, and /clippy is gone  · status: todo (fenced)

**Depends on:** Phase 1

- `docs/delegate/dual_review.md`: the main review runs `<PhaseChecks/>`'s style review by reading `commands/unit/style_review.md` → `<StyleReview/>` with the phase baseline as its range and `${SESSION_DIR}` as its directory (it writes its diff there, never a shared path), opens each violation in the phase's findings ledger, and records its table. A phase with no Rust change runs only the banned-words scan.
- `docs/delegate/write_prompt_contract.md` item 2: the `rust_style` line's reason ("this run's one style audit happens at the end") becomes the per-phase review: the unit director runs the style review in its main review, so seats still never load the guide.
- `commands/unit/style_review.md`: remove `<ResolveStyleDiffBase/>`, `<RunProjectStyleReview/>` and its `clippy.md` read, and the intro's "`/unit:direct` reads this file twice" paragraph. `<StyleReview/>` already takes `since <ref>` and a caller directory, checks the baseline, and reports `Off` from `LINT_OP_STYLE_REVIEW`.
- `commands/unit/direct.md`: name `<PhaseChecks/>` in the TagReferenceContract table and at the phase's review point. `<FinalGate/>` loses its `clippy.md` step and its `<RunProjectStyleReview/>` step, so `final` runs once with no cleanup rerun. Remove `<ResolveStyleDiffBase/>`, `STYLE_GATE_CONFIG`, `STYLE_REVIEW_DONE`, `STYLE_DIFF_BASE` and their handoff fields, the `/clippy` load rule (line 87) and the "full `clippy` skill" phase rule.
- `scripts/delegate/verify.sh`: `final` drops fmt `--check`, which repeats the phases' fmt. It keeps `cargo check --workspace --all-targets` and the full tests, which no phase runs. Its comment naming `/clippy` is reworded.
- Delete `commands/clippy.md` and `scripts/delegate/style_branch.sh`. Delete `scripts/lint/scan.sh`, `scripts/lint/clippy_config.sh` and `config/clippy.conf` only after confirming nothing else calls them — hooks, the nightly review, cargo-port's lint mirror, any script or settings entry; a file that has another caller stays. `scripts/delegate/progress_timer.sh` stays; the unit notifier uses it.
- Reword every remaining reference: `commands/unit/checkpoint.md`, `commands/unit/ci.md`, `docs/delegate/compose_work_order.md`, `docs/delegate/final_gate_commit.md` (its report names "style review and closing repairs"), `docs/delegate_footprint_review.md`, `docs/delegate_plan_format.md` and `commands/plan/to_phased_plan.md` (the plan's **Style** field goes), `commands/commit_prep.md`, `commands/validate_and_push.md` (it states mend's rollback stop itself instead of pointing at `/clippy`), `commands/lint_config.md` and `scripts/lint/lint_config.sh` (consumer columns), `config/README.md` (the `clippy.conf` section goes), `scripts/lint/invoke.sh` and `scripts/hooks/banned_words_lib.py` (comments).
- Gate: `scripts/lint` and `scripts/delegate` suites and basedpyright on the touched Python. Then `rg -n '/clippy\b|clippy\.md|style_branch|clippy_config|clippy\.conf|scan\.sh'` over this repository returns only as-built history, and the same search over `~/rust/*/CLAUDE.md`, `~/rust/*/docs`, `~/rust/nate_style` and `/etc/nixos` is reported to the showrunner hit by hit — those repositories are not this unit's to edit.

As-built at run end: `docs/as-built/session-notifier.md` (its `/clippy launches progress_timer.sh` line), `docs/as-built/build-memory-admission.md`, and the as-built that describes `/unit:direct`'s gates.

Out of scope, stated: editing other repositories (their hits are reported, above), and memories that mention `/clippy` (six memory files under `~/.claude/projects/`); this run fixes memories it owns only.
