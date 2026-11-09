# A worker's run start never waits on its worker lock

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Requested on 2026-11-09 by the sdlc showrunner (fault from cargo-liner: orphan cleanup blocks on worker_lock).

Written by panes-unit on 2026-11-09 for the sdlc showrunner.

## The request

sdlc, 2026-11-09 08:10 EST: at 08:05 EST a cargo-liner port 4r worker ran `verify.sh test cargo-mend --local` through `detached_runs`. The run blocked in orphan cleanup on `worker_panes.worker_lock` (`blocking=True`, `scripts/herdr/turn_end.py:259`) until the worker interrupted it. Find the caller and the holder, and fix the cause.

## Cause

- **Holder:** port-impl's dispatch launcher, started 02:41 EST by code from before herdr-worker-panes Phase 34. That code held the worker lock through submission and completion. It held the lock until it exited 3 at 08:08:12. Current dispatch returns at submission.
- **First waiter:** the supervisor of run `20261109T080530-3200962-cb8f` delivered its result, wrote `completion_pending: true`, then called `turn_end.run_finished` → `handle` → `worker_lock(blocking=True)`.
- **Second waiter (the stall):** the worker's next `detached_runs.py start` holds `.start.lock` and runs `_reap_orphans_locked`. That function retries `_finish_record` for every finished record with `completion_pending`, also when the record's supervisor is still alive and still doing that completion. So the worker's own tool call waited on the worker lock.

## Delegation Context

- **Project:** `~/.claude` scripts. Python 3.11+ through `scripts/lib/py`, `unittest`, basedpyright.
- **Project started:** 2026-11-09T13:12:49.043+00:00
- **Files:** `scripts/agents/detached_runs.py` (`_reap_orphans_locked`, `_finish_record`, `command_supervise`), `scripts/agents/detached_run_process.py` (`supervises(pid, record_path)`).

## Phases

### Phase 1 — The reap retries a pending completion only when its supervisor is gone  · status: done

#### Work Order

**Goal:** `detached_runs.py start` and `reap` never call a worker's completion while that run's supervisor is alive, so a worker's run start never waits on its worker lock.

**Spec:**
1. `scripts/agents/detached_runs.py` `_reap_orphans_locked`: the branch for `status == "finished"` with `completion_pending` calls `_finish_record` only when the existing liveness read, `detached_run_process.supervises(pid, path)`, is false for the record's `pid`. Add no new liveness read (sdlc, 2026-11-09). A live supervisor owns its own completion. Change nothing else in the reap.
2. Fix the `reap_orphans` docstring if it names the pending retry.

**Files:**
- `scripts/agents/detached_runs.py` — the pending-completion retry in `_reap_orphans_locked`.
- `scripts/agents/test_detached_runs.py` — add rows to the existing pending-completion test: a pending record with a live supervisor is not retried; a pending record whose supervisor is gone is retried once.
- `docs/as-built/herdr-adoption.md` — one sentence for the rule beside its `reap_orphans` text.

**Workers:** `1 writer` — one function and its test.
- `impl` — all files above.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** basedpyright 0/0/0 on `scripts/agents/detached_runs.py` and its test; `scripts.agents.test_detached_runs` green; the targeted test green.
