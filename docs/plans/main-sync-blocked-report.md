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

#### Work Order

**Goal:** each `dirty` detail names every file that blocks the update, and never shows an empty list.

**Spec:**

1. The proof collects every candidate path that fails it, in sorted order, instead of stopping at the first one. It still changes no file when any path fails.
2. A path fails the proof when its working file differs from origin/main, or when its index entry differs from both HEAD and origin/main, or when its index entry is unmerged.
3. The `dirty` detail names each failed path, separated by `, `. Candidate paths that equal origin/main do not block, so the detail does not name them.
4. This covers a clean status with an untracked file at a path that origin/main adds: the detail names that file.
5. When git cannot list the candidates (a failed query or a failed status), the detail gives git's first error line in place of the list. A detail never ends with an empty list `()`.
6. The detail text: `<checkout> has local changes that block the update (<paths>)`.
7. Add rows to the existing test `test_each_blocked_case_leaves_the_checkout_alone`:
   - A clean status and an untracked file at a path that origin/main adds, with other content: `sync` returns `dirty`, the detail names that file, and the file keeps its content.
   - Two blocked files and one file that equals origin/main: the detail names both blocked files and not the matching file.
8. Keep each existing assertion. If an existing assertion checks the old detail text, update only its text and say so in the summary. Add no other check.
9. Update the module docstring if it describes the detail.

**Files:**
- `scripts/lib/main_sync.py` — the collected failed paths and the detail
- `scripts/lib/test_main_sync.py` — the new rows

**Workers:** 1 writer — one small change in one module.
- `impl` — the files above.

**State:** none — the detail text is the only change; no record, mark or file write changes.

**Constraints from prior phases:** the interrupted-update proof from the main sync interrupted update run stays as built.

**Acceptance gate:**
- The Test command above passes.
- `basedpyright scripts/lib/main_sync.py scripts/lib/test_main_sync.py` gives `0 errors, 0 warnings, 0 notes`.
