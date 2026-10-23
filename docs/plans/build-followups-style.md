# Style showrunner follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up phases for the style showrunner after its as-built (`docs/as-built/style-showrunner.md`).

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-23, natedev (showrunner of build-followups): "new follow-up, current, root-cause fix. The user wants no more 'auto mode stopped' alerts of this kind. On 10-22 10:13 AST auto mode refused cargo-liner-style-modules' two implement.sh dispatches for ticket 22 (P542, first build of an approved proposal). Cause chain: style_plan.py wrote a Work Order that still said 'Judge the proposal ticket…' and 'Approved proposal … overrides the mechanical-only invariant in the copied context'. With no build model, the director searched other units' /tmp/claude/unit/*/phases/*/prompt_*.md, rewrote values-unit-15's prompts with a script (refused: Instruction Poisoning), then wrote them by hand; the classifier judged the dispatches a bypass (Auto-Mode Bypass, Modify Shared Resources), and later cancels were refused too. Fixes: (1) ~/.claude/scripts/style/style_plan.py ~1331-1340: for an approved-proposal ticket write a build Goal ('Apply the approved findings of <issue> in <files>'), not 'Judge'; restate the override line as a plain scope statement; also fix stale scripts/delegate/verify.sh paths in the Build/Test/Lint lines of the unit plan's Delegation Context. (2) ~/.claude/docs/delegate/compose_work_order.md:83-86 Fallback: build prompts only from this unit's Work Order; never read, copy or transform another unit's /tmp/claude/unit/* prompt files. Add a test that an approved-proposal ticket gets a build Goal."

style-unit checked the verify.sh paths: `style_plan.py` and `add_style.py` already write `scripts/direct/verify.sh`. The stale `scripts/delegate/verify.sh` lines are in git-excluded unit plan copies in three cargo-liner unit worktrees, which this unit does not change; cargo-liner-style was asked to correct them (2026-10-23).

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree.
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 through `~/.claude/scripts/lib/py`; basedpyright 1.39; markdown docs.
- **Layout:** `scripts/style/` — style ledger and refill, tests beside them; `docs/delegate/` — unit director contracts; `docs/as-built/style-showrunner.md` — the style showrunner as-built.
- **Key files:**
  - `scripts/style/style_plan.py` — `phase_text` :1281 (`approved = ticket["kind"] == "proposal"` :1312, `authorization` :1331-1336, **Goal** :1339-1340, "Apply the approved findings." :1423).
  - `scripts/style/test_style_plan.py` — `test_approved_proposal_precedes_coverage_and_restart_rebuild_keeps_authorization` :3252 (asserts the proposal ticket's Work Order text), `test_…` near :1090 (structure Goal).
  - `docs/delegate/compose_work_order.md` — **Fallback** :83-86.
  - `docs/as-built/style-showrunner.md` — proposal ticket lines (:134, :197).
- **Test lanes:** `scripts/style/` — `test_*.py` beside each module.
- **Build:** none (Python and markdown).
- **Test:** from the worktree root, `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest <modules>`; then the merge gate `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the root.
- **Lint:** `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style <files>`; pass is `0 errors, 0 warnings`.
- **Invariants:**
  - basedpyright reports zero errors and zero warnings; no file-level ignores; no `Any`; line-level ignores only as a last resort (user, CLAUDE.md).
  - Every module has `from __future__ import annotations`; discarded results are written `_ = …`; test classes use `@final`/`@override` (repo convention).
  - Never write the live ledger or a live production doc; read them only from a copy (style-unit rule).
  - The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to code, comments, docs and commits (user).

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, contiguous, in execution order. Never a letter suffix.
     Phases 1-43 are in docs/as-built/style-showrunner.md; this doc continues the same numbers. -->

### Phase 44 — a style unit's approved-proposal ticket reads as build work · status: done

**Depends on:** none

#### Work Order

**Goal:** the Work Order that `style_plan.py` writes for an approved-proposal ticket tells the unit to build the approved findings, and the unit director contract forbids building prompts from another unit's prompt files.

**Spec:**
- In `phase_text`, a proposal ticket (`approved`) gets the **Goal** `Apply the approved findings of `<ticket["proposal"]>` in <N> file(s) of `<crate>`.` (N is `len(ticket["files"])`; "file" when N is 1, else "files"). It never contains "Judge". The structure Goal ("Judge these N files …") and the read/listed Goal ("Judge the <kind> ticket …") stay as they are for the other kinds.
- Replace the approved branch of `authorization` with a plain scope statement that overrides nothing: `This ticket builds the approved proposal `<issue>`: its approved findings are this ticket's work. Read the issue for its sites and before/after changes. Hold every other non-mechanical finding. Package verification remains required.` It must not contain "overrides" or "invariant". The non-approved branch stays as it is.
- The rest of the proposal Work Order (the "Apply the approved findings." line at :1423, the record lines, the acceptance gate) stays as it is.
- `docs/delegate/compose_work_order.md` **Fallback**: add that the fallback builds the prompt only from this unit's own Work Order, plan and conversation, and never reads, copies or transforms another unit's prompt files under `/tmp/claude/unit/*` (they are another session's state). When this unit's Work Order does not say what a build needs, send the gap to the showrunner (a production unit) or the user, and do not borrow another unit's prompt.
- `docs/as-built/style-showrunner.md`: where it describes the proposal ticket, add one line: its Work Order Goal is "Apply the approved findings of <issue> in N files of <crate>", and its Spec states the build scope plainly, with no override of the copied invariants.

**Files:**
- `scripts/style/style_plan.py` — proposal Goal and scope statement in `phase_text`.
- `docs/delegate/compose_work_order.md` — Fallback rule.
- `docs/as-built/style-showrunner.md` — proposal Work Order line.
- `scripts/style/test_style_plan.py` — proposal Goal test.

**Workers:** 1 writer + 1 tester; the code and docs split from the tests by file.
- `impl`: `scripts/style/style_plan.py`, `docs/delegate/compose_work_order.md`, `docs/as-built/style-showrunner.md`.
- `test` opens as `test`: `scripts/style/test_style_plan.py`. From the Spec alone: in `test_approved_proposal_precedes_coverage_and_restart_rebuild_keeps_authorization` (or a new test beside it using the same fixture), assert the proposal ticket's Work Order has the Goal line `Apply the approved findings of `<issue>` in 1 file of `crates/alpha`.`, contains `This ticket builds the approved proposal`, and contains neither "Judge" nor "overrides the mechanical-only invariant"; replace the existing `assertIn("overrides the mechanical-only invariant", first)`.

**Constraints from prior phases:**
- Phase 43 put a refill lock around selection in `next_phase`; this phase changes only the text `phase_text` returns.

**Acceptance gate:**
- `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest test_style_plan test_style_proposals` passes.
- The proposal Goal test fails against the old `phase_text` and passes with the change.
- `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style scripts/style/style_plan.py scripts/style/test_style_plan.py` reports `0 errors, 0 warnings`.
- `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root passes.
