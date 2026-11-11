# Main sync path collision

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A blocked main_sync report names an untracked path that collides with an incoming path.

> **As-built disposition: amend** — `docs/as-built/merge-gate-and-test-speed.md` records the result.

## Source

The sdlc showrunner, 2026-11-10: "Finding 1 is a wrong result: fix it as a small phase. When an untracked path is a prefix of an incoming path (dir blocks dir/file), main_sync must name the blocking path in the blocked report. Add a row to the existing main_sync test with a temp repo. Send the as-built hash and this phase's ETA."

Finding 1, from the Astra review of the main sync blocked report: an untracked file `dir` blocks an incoming `dir/file`. `_has_untracked_update` compares only exact paths, so `sync` reaches `git merge --ff-only`. The merge fails, and the report names no blocking path.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T18:57:00+00:00
- **Stack:** Python 3.13, standard library only, and the `git` command.
- **Layout:** `scripts/lib/`.
- **Key files:**
  - `scripts/lib/main_sync.py` — `_has_untracked_update` (line 110) compares untracked paths with the paths origin/main adds; `_finish_interrupted_update` (line 126) proves each candidate path; `sync` (line 199) builds the `dirty` detail from `blocking_details`
  - `scripts/lib/test_main_sync.py` — the existing tests, with a fixture origin, writer and checkout in a temporary directory
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.lib.test_main_sync`, from the worktree root.
- **Lint:** `basedpyright <the changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line.
- **Invariants:**
  - Tests use fixture repositories. They never touch a live checkout.
  - main_sync never deletes or changes local work. `git merge --ff-only` stays the only command that moves HEAD.
  - A failed proof changes no file.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — A blocked report names an untracked path that collides with an incoming path  · status: done

**Depends on:** none

#### Work Order

**Goal:** when an untracked path collides with a path that origin/main adds, `sync` returns `dirty` and the detail names the untracked path.

**Spec:**

1. A collision is an untracked path that is a parent directory path of a path origin/main adds, or a path origin/main adds that is a parent directory path of an untracked path. Example: untracked `dir` and incoming `dir/file`, or untracked `dir/file` and incoming `dir`.
2. Compare whole path components. `dir` collides with `dir/file`, but `di` does not collide with `dir/file`, and `dir` does not collide with `dir2/file`.
3. Each colliding untracked path blocks the update. `sync` returns `dirty`, and the detail names each colliding untracked path in the existing form `<checkout> has local changes that block the update (<paths>)`.
4. A collision changes no file, and `sync` does not run the merge.
5. An untracked path that equals an added path keeps its current proof in `_finish_interrupted_update`. Do not change that proof.
6. Add one row to the existing test `test_each_blocked_case_leaves_the_checkout_alone`: an untracked file `dir` in the checkout and a commit at origin/main that adds `dir/file`. Assert that `sync` returns `dirty`, the detail names `dir`, HEAD does not move, and `dir` keeps its content.
7. Keep each existing assertion. Add no other check.
8. Update the module docstring if it describes the blocked cases.

**Files:**
- `scripts/lib/main_sync.py` — the collision check
- `scripts/lib/test_main_sync.py` — the new row

**Workers:** 1 writer — one small change in one module.
- `impl` — the files above.

**State:** none — no record, mark or file write changes.

**Constraints from prior phases:** the interrupted-update proof and the blocked report from the earlier main sync runs stay as built.

**Acceptance gate:**
- The Test command above passes.
- `basedpyright scripts/lib/main_sync.py scripts/lib/test_main_sync.py` gives `0 errors, 0 warnings, 0 notes`.
