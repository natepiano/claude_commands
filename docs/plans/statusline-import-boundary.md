# Statusline import boundary

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The statusline count reader does not import merge execution code.

> **As-built disposition: amend** — no as-built doc describes this import; the Phase 1 `As-built` block is the record.

## Source

The sdlc showrunner, 2026-11-09, on natest proposal 3: "Give the count reader an import boundary that excludes merge execution. Measure the gain with a prototype first; land only if it is real. No new checks."

Prototype, 2026-11-09, in a copy of the tree at `fcd332bc3`. The median of 15 starts gives this CPU for each start:

| Path | Before | After |
| --- | --- | --- |
| No production | 0.320 s | 0.120 s |
| Production | 0.380 s | 0.280 s |

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T22:06:09.000+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:** `scripts/production/`, `scripts/statusline/`, `scripts/hooks/`.
- **Key files:**
  - `scripts/production/update_registration.py` — line 31 imports `merged_checkpoints` at module level; only `state_block` (line 1312) calls it
  - `scripts/hooks/showrunner_footer.py` — lines 20–21 import `update_registration` at module level
  - `scripts/statusline/counts_row.py` — the count reader
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_update_registration_log scripts.production.test_waiting scripts.hooks.test_showrunner_footer scripts.production.test_footer_counts`, from the worktree root.
- **Lint:** `basedpyright scripts/production/update_registration.py` passes when its output has the `0 errors, 0 warnings, 0 notes` line.
- **Invariants:**
  - Tests use fixtures; they never read live state.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — The count reader starts without merge execution code  · status: done

**Depends on:** none

#### As-built

`update_registration.state_block` imports `merged_checkpoints` in its body, as its first statement. `state_block` is the only caller in the module. `import scripts.statusline.counts_row` loads neither `scripts.production.merge_checkpoint` nor `scripts.production.merge_gate`.

**Files:**
- `scripts/production/update_registration.py` — the function-level import

**Ruled out:** a new test or check for the boundary; the showrunner set no new check.
