# Merge checkpoint as-built scope — accept the plan deletion

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The merge gate accepts an as-built commit that changes or deletes `docs/plans/<slug>.md`, wherever `.unit.toml` now points.

## Delegation Context

- **Project:** `~/.claude` scripts.
- **Project started:** 2026-11-13T00:30:00+00:00
- **Stack:** Python 3, unittest, basedpyright.
- **sdlc (2026-11-12):** "In scripts/production/merge_checkpoint.py, make the smallest change: an as-built commit that changes or deletes docs/plans/<slug>.md passes scope, wherever .unit.toml now points. Add a row to the existing scope test for your case."
- **Defect:** `checkpoint_plan()` (`scripts/production/merge_checkpoint.py:555`) builds the expected plan path as `request.unit.plan.with_name(<slug>.md)`. `request.unit.plan` comes from the unit's `.unit.toml`. The as-built step repoints `.unit.toml` to an as-built doc before the land. The gate then expects `docs/as-built/<slug>.md`, and holds a correct as-built commit. Case: models as-built `610b54f7f` deletes `docs/plans/remove-command-dry-run.md`; `.unit.toml` points at `docs/as-built/remove-command.md`; the gate expected `docs/as-built/remove-command-dry-run.md`.
- **Invariants:**
  - Do not change `config/copies.conf`, `config/direct.conf` or any permission setting (`settings*.json`, `templates/settings_local.json`).
  - The user adds no checks: add no guard, refusal or scan that a Work Order does not name.
  - Never commit a test project. A test makes the project it needs in a tempdir at run time.

## Phases

### Phase 1 — The as-built scope accepts the plan deletion  · status: done

#### Work Order

**Goal:** an as-built commit that changes or deletes `docs/plans/<slug>.md` passes the merge gate's scope step, wherever `.unit.toml` points.

**Spec:**

1. In `scripts/production/merge_checkpoint.py`, make the smallest change so that an as-built commit (subject `docs(<slug>): as-built`) that changes or deletes `docs/plans/<slug>.md` passes `scope()`. The directory of `.unit.toml`'s `plan` must not change the expected path.
2. Keep every other rule: an as-built commit carries only `.md` paths; a commit with a non-doc path is held; a shrink commit keeps its own rule.
3. Add a row to the existing loop in `test_shrink_merges_only_plan_and_next_items_after_code_checkpoint` (`scripts/production/test_merge_checkpoint_history.py`). In the row, `.unit.toml`'s `plan` points at an as-built doc with a name other than the slug, such as `docs/as-built/beta.md`. The commit deletes `docs/plans/alpha.md` and changes that as-built doc; it does not add `docs/as-built/alpha.md`. Like every row, it also commits `docs/plans/alpha-next.md`. The row passes scope. Before the fix, the row fails.

**Files:**
- `scripts/production/merge_checkpoint.py` — the scope fix.
- `scripts/production/test_merge_checkpoint_history.py` — the new row.

**Workers:** 1 writer; one small fix and its test row, nothing splits.
- `impl` — both files.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** the new row fails before the fix and passes after it. `scripts.production.test_merge_checkpoint_history` passes. basedpyright reports zero errors and zero warnings on both files.

**Change-Review:** none — the merge gate lands an as-built commit that it held before; nothing a person types shows a new result.
