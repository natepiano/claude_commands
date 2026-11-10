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

#### As-built

- `sync` in `scripts/lib/main_sync.py` runs the proof when the status shows changed tracked files, or when an untracked file is at a path that origin/main adds.
- A candidate is proved when its working file equals origin/main in content and mode, or is absent where origin/main has no file. A path under a file counts as absent. A tracked candidate's index entry must equal HEAD or origin/main.
- When every candidate is proved, `sync` removes the proved untracked files, restores the tracked files to HEAD, and runs `git merge --ff-only`. The detail says `finished an interrupted update`.
- Any unproved candidate returns `dirty`, and no file changes. Git output keeps the bytes of each file name.
- Rows in `test_it_fast_forwards_and_leaves_untracked_files` and `test_each_blocked_case_leaves_the_checkout_alone` cover the half-done update, a retry after a stopped restore, a directory replaced by a file, a staged edit and a real local edit.
