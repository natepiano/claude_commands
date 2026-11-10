# Main sync blocked report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A blocked main_sync report names each file that blocks it.

> **As-built disposition: amend** — `docs/as-built/merge-gate-and-test-speed.md` records the result.

## Source

The sdlc showrunner, 2026-11-10: "Fix the open minor as a next phase: a blocked report must name each file that blocks it. A 'dirty' report with an empty file list tells the reader nothing. Add a row to the existing test. Send me the hash."

The open minor: with a clean status and an untracked file at a path that origin/main adds, whose content differs from origin/main, `sync` returns `dirty` with the detail `has changed tracked files ()`.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T18:57:00+00:00
- **Stack:** Python 3.13, standard library only, and the `git` command.
- **Layout:** `scripts/lib/`.
- **Key files:**
  - `scripts/lib/main_sync.py` — `_finish_interrupted_update` (line 120) proves each candidate path and returns `bool`; `sync` (line 179) builds the `dirty` detail from the first five status lines (line 212)
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

### Phase 1 — A blocked report names each file that blocks it  · status: done

**Depends on:** none

#### As-built

- `_finish_interrupted_update` in `scripts/lib/main_sync.py` checks every candidate path and collects each path that fails the proof. Any failed path stops the restore, and no file changes.
- The `dirty` detail reads `<checkout> has local changes that block the update (<paths>)`. It names each failed path, and never a path that equals origin/main.
- A failed git query gives git's first error line in place of the list.
- Rows in `test_each_blocked_case_leaves_the_checkout_alone` cover a clean status with an untracked file that differs from origin/main, and two blocked files beside one matching file.
