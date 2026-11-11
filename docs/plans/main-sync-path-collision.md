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

#### As-built

- `_has_untracked_update` in `scripts/lib/main_sync.py` finds each untracked path that is a parent directory path of a path origin/main adds, or the reverse. It compares whole path components and adds each colliding untracked path to the blocking details.
- `sync` runs that check on each run, also when the status shows tracked changes. A collision gives `dirty` with the detail `<checkout> has local changes that block the update (<paths>)`. No file changes, and the merge does not run.
- An untracked path that equals an added path keeps the interrupted-update proof.
- A row in `test_each_blocked_case_leaves_the_checkout_alone` covers an untracked file `dir` and an incoming `dir/file`.
