# Main sync interrupted update

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** main_sync finishes an update that stopped after it wrote the files and before it moved HEAD.

> **As-built disposition: amend** — `docs/as-built/merge-gate-and-test-speed.md` records the result.

## Source

The sdlc showrunner, 2026-11-10: "a fault in scripts/lib/main_sync.py, reported by natedev. On the Mac, /Users/natemccoy/.claude has 32 changed tracked files. Each file equals origin/main 2f215c718, but HEAD stays at a2e0e44b0 (47 behind). All mtimes are 13:23 EST, when the Mac stopped answering ssh. An update wrote the files and stopped before it moved HEAD; each retry now refuses this half-done work as local changes. Wanted: main_sync finishes or recovers an interrupted update. When every changed file equals its content at the target, move HEAD to the target; real local edits still block as now. Add rows to the existing main_sync test with a fixture repo, never the live checkout. The fixed job then repairs the Mac on its next run. Send me the hash."

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T18:33:00+00:00
- **Stack:** Python 3.13, standard library only, and the `git` command.
- **Layout:** `scripts/lib/`.
- **Key files:**
  - `scripts/lib/main_sync.py` — `sync` (line 68) checks the branch, a Git operation in progress, the fetch and divergence, then refuses changed tracked files as `dirty` (lines 95–98) before `git merge --ff-only` (line 99)
  - `scripts/lib/test_main_sync.py` — the existing tests, with a fixture origin, writer and checkout in a temporary directory
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.lib.test_main_sync`, from the worktree root.
- **Lint:** `basedpyright <the changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line.
- **Invariants:**
  - Tests use fixture repositories. They never touch a live checkout.
  - main_sync never deletes or changes local work. `git merge --ff-only` stays the only command that moves HEAD.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — main_sync finishes an interrupted update  · status: done

**Depends on:** none

#### Work Order

**Goal:** a checkout whose every change already equals origin/main advances to origin/main. A checkout with a real local edit still blocks as `dirty` and stays unchanged.

**Spec:**

1. Apply the new path only where `sync` now returns `dirty`: after the fetch, after the `diverged` check, and only when HEAD is an ancestor of origin/main.
2. Collect the candidate paths:
   - every tracked path whose index entry or working file differs from HEAD;
   - every untracked file at a path that origin/main has and HEAD does not have.
3. A candidate path is an interrupted write when its working state equals origin/main at that path:
   - same content and same file mode; or
   - the file is absent and origin/main has no file there.
   A candidate whose working state equals HEAD is not a change and does not count.
4. When each candidate path is an interrupted write:
   - Put each tracked candidate back to its HEAD state in the index and the working tree.
   - Remove each untracked candidate file. origin/main holds the same file, so the merge writes it again.
   - Then run the existing `git merge --ff-only`. Git keeps its own guard over every other file.
   - The outcome is `ADVANCED`. Its detail names the range and says that the run finished an interrupted update.
5. When any candidate path differs from origin/main, return `dirty` as now. Change no file.
6. Paths that equal HEAD and differ at origin/main are files the update did not reach. Leave them; the merge writes them.
7. Add rows to the existing tests. Add no other check.
   - In `test_it_fast_forwards_and_leaves_untracked_files`: origin/main changes one file, adds one file and removes one file. The checkout holds the changed file and the new untracked file at their origin/main content. The removed file is still present. `sync` returns `ADVANCED`, HEAD is the origin/main tip, the status is clean, and `session-state.json` stays.
   - In `test_each_blocked_case_leaves_the_checkout_alone`: one changed file equals origin/main, and one other file has a real local edit. `sync` returns `dirty`, and both files keep their content.
8. Update the module docstring for the new path.

**Files:**
- `scripts/lib/main_sync.py` — the interrupted-write proof and the restore before the merge
- `scripts/lib/test_main_sync.py` — the new rows

**Workers:** 1 writer — one small change in one module.
- `impl` — the files above.

**State:** the run changes files in a live checkout. It restores only files proved equal to origin/main, so a stop before the merge leaves a clean checkout at HEAD and the next run advances it.

**Constraints from prior phases:** none.

**Acceptance gate:**
- The Test command above passes.
- `basedpyright scripts/lib/main_sync.py scripts/lib/test_main_sync.py` gives `0 errors, 0 warnings, 0 notes`.
