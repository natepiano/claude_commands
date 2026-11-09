# Test fixture lifetimes

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The fast-track check fixture ends by itself when the test runner is killed first.

> **As-built disposition: amend** — no as-built doc describes these fixtures; the Phase 1 `As-built` block is the record.

## Source

The sdlc showrunner, 2026-11-09, on leftover processes that builds found on natedev: "Stop each by its exact pid (never by pattern), then fix the source so the process ends with its run; no new check or scan." The user's standing rule, through the showrunner: "our processes, jobs, agents, scripts should all clean up after themselves".

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-09T18:14:02.537+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:** `scripts/production/` (fast track).
- **Key files:**
  - `scripts/production/test_fast_track.py` — `test_check_time_limit_stops_the_check_process_group` (line 1187) writes a `test_item.py` that starts a child and both call `signal.pause()`
  - `scripts/direct/verify_tree_cancel_test_support.py` — line 79 shows the idiom: `signal.alarm(int(os.environ["TEST_HANG_GUARD_SECS"]))` before the wait
  - `scripts/lib/test_hang_guard.py` — `TEST_HANG_GUARD_SECS`
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_fast_track`, from the worktree root.
- **Lint:** `basedpyright <changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line; the exit status says nothing.
- **Invariants:**
  - Tests use fixtures; they never read live state.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — The fast-track check fixture ends by itself when its test runner is killed  · status: done

**Depends on:** none

#### Work Order

**Goal:** The fast-track check fixture and its child end by themselves within twice `TEST_HANG_GUARD_SECS` when nothing stops them. A killed test runner leaves no process.

**Spec:**

1. The `test_item.py` text in `test_check_time_limit_stops_the_check_process_group` in `scripts/production/test_fast_track.py`: the file and its child each call `signal.alarm(int(os.environ["FAST_TRACK_FIXTURE_ALARM_SECS"]))` before `signal.pause()`. The test sets `self.env["FAST_TRACK_FIXTURE_ALARM_SECS"]` to twice `TEST_HANG_GUARD_SECS`, as it sets `FAST_TRACK_DESCENDANT_PID`. The alarm is longer than the wait of the test, so the alarm cannot satisfy the assertions that the time limit stopped the child.
2. Add no test, check, scan or new helper. The showrunner set this boundary. Keep every existing assertion.
3. Do not edit `scripts/mac_test/test_copy_watch.py`. The builds report unit owns the same fix in that file (the showrunner, 2026-11-09).

**Files:**
- `scripts/production/test_fast_track.py` — the check fixture

**Workers:** 1 writer — one small edit; nothing splits.
- `impl` — the file above.

**State:** none — the phase changes test fixtures only.

**Constraints from prior phases:** none.

**Acceptance gate:**
- `scripts/lib/py -m unittest scripts.production.test_fast_track` passes.
- `basedpyright` on the file gives `0 errors, 0 warnings, 0 notes`.
