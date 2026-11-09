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

#### As-built

`_reap_orphans_locked` in `scripts/agents/detached_runs.py` retries a finished record's pending completion only when `detached_run_process.supervises(pid, path)` is false. A live supervisor owns its own completion, so `detached_runs.py start` and `reap` never wait on the worker lock for it. A supervisor that dies after it writes `completion_pending` gets one retry from the next reap. The `reap_orphans` docstring needed no change.

**Files:**
- `scripts/agents/detached_runs.py`
- Tests: `scripts/agents/test_detached_runs.py` (`PaneRunDeliveryTests`: a live supervisor is not retried; a gone supervisor is retried once)
- Docs: `docs/as-built/herdr-adoption.md` (the `reap_orphans` rule)
