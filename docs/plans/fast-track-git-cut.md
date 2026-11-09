# Fast track Git cut

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `fast_track.py` and the `copies.py` remote refresh start fewer Git processes.

> **As-built disposition: amend** — `docs/as-built/merge-gate-and-test-speed.md` records the result.

## Source

The sdlc showrunner, 2026-11-09: "start your next item now: 'Cut the Git process starts in fast_track and the copies refresh' (sdlc-devtools-tests-next.md). builds measured test_fast_track at 63.3 CPU s per whole gate, 2,937 Git calls, 27.6 s of Git. It is now one of the two largest test costs."

The item in `docs/plans/sdlc-devtools-tests-next.md`: `reconcile` repeats `rev-parse` (898), `update-ref` (777) and `cat-file` (283). The `copies.py` remote refresh makes 403 fetches (870 calls, 10.1 s). Completion condition: "one injected Git runner serves `fast_track.py`, so tests start fewer processes. `reconcile` reads each ref and object once. The remote refresh fetches each remote once. Fewer measured Git calls and less time in a new profile. Add no check."

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T22:45:38.952+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:** `scripts/production/`, `scripts/copies/`.
- **Key files:**
  - `scripts/production/fast_track.py` — `run_git` and `git` at lines 127–134; direct `subprocess.run` Git calls at lines 184, 193, 467, 507, 563, 567 and 730; `reconcile` at line 603
  - `scripts/production/test_fast_track.py` — the test module that drives `reconcile`
  - `scripts/copies/copies.py` — the remote refresh fetch at line 728
  - `scripts/copies/test_copies.py`, `scripts/copies/test_remote_copies.py`, `scripts/copies/test_review_remote_copies.py` — the copies tests
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_fast_track`, and `scripts/lib/py -m unittest scripts.copies.test_copies scripts.copies.test_remote_copies scripts.copies.test_review_remote_copies`, from the worktree root.
- **Lint:** `basedpyright <the changed files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line.
- **Invariants:**
  - Tests use fixtures and real Git. They never read live state.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — Fewer Git process starts in fast_track and the copies refresh  · status: done

**Depends on:** none

#### As-built

`GitRunner` in `scripts/production/fast_track.py` is the only place that starts Git. It keeps successful reads of objects addressed by full hash for one `reconcile` pass. Reads of refs and ancestry stay fresh. `refresh_remotes` in `scripts/copies/copies.py` makes one fetch for both mappings. Checkpoint 0be4e208c.
