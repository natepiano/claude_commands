# Registry fetch and pane thread link

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** Three fixes sdlc assigned to models on 2026-11-08: the registry reads its own fetched branch, Codex can parse the hooks file again, and a new Codex worker is linked to its pane's thread.

## Delegation Context

- **Project:** `~/.claude` scripts — the model registry writer and the herdr worker panes.
- **Project started:** 2026-11-08T17:13:02.366+00:00
- **Stack:** Python 3.13, `unittest`, basedpyright; git; herdr; codex-cli 0.167.0.
- **Layout:**
  - `scripts/lib/models_registry.py` — registry edit, fetch, fast-forward and publish.
  - `scripts/agents/test_models_conf_publish.py` — publish tests against a temporary bare remote.
  - `scripts/herdr/herdr.py` — native process identity (`native_process`, `pane_native_process`, `launch_rollout_proof`).
  - `scripts/herdr/worker_panes.py` — worker start (`_start_locked`, `_codex_start_session`, `first_turn_session`, `_live_policy`).
  - `scripts/herdr/test_herdr.py`, `scripts/herdr/test_worker_panes.py` — their tests.
  - `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py` — Codex hook install and its tests.
- **Test lanes:** none (unit tests sit beside their modules).
- **Build:** none.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest <modules>`
- **Lint:** `basedpyright <changed files>` must end `0 errors, 0 warnings, 0 notes`.
- **Invariants:** add no check the Work Order does not name (user, 2026-11-05 and 2026-11-07).

## Phases

### Phase 1 — The registry writer reads the branch it fetched  · status: done

#### As-built

`models_registry._fetch(repo, branch)` fetches `+refs/heads/<branch>:refs/remotes/origin/<branch>` and returns `rev-parse --verify refs/remotes/origin/<branch>^{commit}`. It never reads `FETCH_HEAD`, because every fetch in the checkout writes `FETCH_HEAD`.

**Files:**
- `scripts/lib/models_registry.py` — `_fetch` reads the branch's own remote ref.
- `scripts/agents/test_models_conf_publish.py` — a fetch of another branch between the fetch and the read does not change the target.

**Gotchas:** with `fetch.prune=true`, this one-ref refspec prunes no other `origin/*` ref (git 2.54.0, checked 2026-11-08). Only a wildcard destination prunes others.

### Phase 2 — Codex can read the hooks file again  · status: done

#### As-built

`CodexHook.timeout` returns an `int`. For `scratch-guard` it is `math.ceil(copy_guard.hook_timeout())`. Codex 0.167.0 refuses the whole `hooks.json` when any timeout is not an integer, and then runs no hook, `SessionStart` included.

**Files:**
- `scripts/hooks/codex_hooks.py` — `CodexHook.timeout` returns whole seconds.
- `scripts/hooks/test_codex_hooks.py` — the installed hooks file carries an `int` timeout for each handler.

**Binds later work:** the herdr `SessionStart` hook reports a new Codex pane's thread at the first prompt, not at process start.

**Gotchas:** `install` compares timeouts with `==`, and `10.0 == 10` in Python, so `install` does not rewrite an old file. An old `hooks.json` needs a one-time edit of `10.0` to `10`, then `install` in each Codex home, then `check`. natedev and the Mac were migrated on 2026-11-08.

### Phase 3 — A Codex worker launch proves its thread without reading its rollout file  · status: done

**Depends on:** Phase 2. panes Phase 28f runs after it.

#### Work Order

**Goal:** A Codex worker launch publishes the right thread on the first try, for a new and a resumed worker, and a dispatch that typed its prompt never says it did not.

**Spec:**
Faults (sdlc, 2026-11-08):
1. A resumed worker fails assign with exit 8, `pid N: current Codex rollout changed` (agents fix3, 150 MB rollout, pid 2973548; fix4, 86 MB). A large rollout keeps it failing on retry. `codex_rollout_threads` stats the open rollout, reads its first line and stats it again. A resumed Codex appends to that same file, so the size changes between the two stats.
2. A new worker fails with `Codex launch identity is unavailable`. Phase 2 found the main cause: Codex could not parse the hooks file, so the herdr `SessionStart` hook never reported the pane's thread. fix5 (pid 3736521) then published `01a1b950…`, a 13 h old thread of another worker, although the process held only its own rollout open.
3. agents fix3's second resume exited 8 and printed `…; no prompt sent`, but the pane got the prompt and worked on it. `worker.py` `_dispatch_turn` prints `no prompt sent` for `DispatchEvidenceUnknown` also in its wait loop, after `_agent_command(…, "prompt", …)` typed the prompt. A retry then sends the job twice.

Proved timing (models, 2026-11-08 12:30 EST, scratch pane, `~/.codex2`): herdr records the pane's `agent_session` about 2 s after the first prompt goes in, not at process start. The value equals the thread in the new rollout file name. `worker_panes.first_turn_session` already types a first turn and waits for that report.

Design (panes 28c handoff, accepted by sdlc [1a39967b]):
1. Inside `herdr.launch_rollout_proof()`, `herdr.native_process` for `codex` reads no rollout file.
   - With a `resume <id>` argument: the thread is that argument. A matching pane report gives `PaneSessionReport`; no report gives `LaunchArgument`, which `pane_native_process` already turns into `UnreportedResume`. A report that names another thread keeps today's result, `LaunchArgument`.
   - With no `resume` argument: the thread is the pane report, `PaneSessionReport(reported)`. With no report, raise `Codex launch identity is unavailable`, as today.
2. Outside the launch proof (`RolloutProofPhase.STABLE`), the rollout read stays in this phase. Phase 5 removes it.
3. In `worker.py` `_dispatch_turn`, every failure line printed after the prompt was typed says the prompt was delivered and names the pane, for example `anomaly: <worker>: <reason>; prompt delivered to pane <pane>`. Lines printed before typing keep `no prompt sent`. Exit codes do not change.

Add no other check (user, 2026-11-05 and 2026-11-07).

Test rows:
- `scripts/herdr/test_herdr.py`, in the existing `native_process` launch tests:
  1. A new launch with a foreign rollout open publishes the pane's reported thread.
  2. A new launch with no rollout open publishes the pane's reported thread.
  3. A resume publishes its argument thread.
  4. A resume whose only open rollout is its own older file, growing while it is read, proves.
- `scripts/production/test_worker.py`, in the existing dispatch tests:
  5. A transcript failure after the prompt is typed reports the prompt as delivered.

**Files:**
- `scripts/herdr/herdr.py` — `native_process` under `RolloutProofPhase.LAUNCH`.
- `scripts/herdr/test_herdr.py` — rows 1–4.
- `scripts/production/worker.py` — `_dispatch_turn` failure lines after typing.
- `scripts/production/test_worker.py` — row 5.

**Workers:** 2 writers.
- `impl`: `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` (rows 1–4).
- `test`, as a writer: `scripts/production/worker.py`, `scripts/production/test_worker.py` (row 5).

**State:** none.

**Acceptance gate:** `scripts.herdr.test_herdr`, `scripts.herdr.test_worker_panes`, `scripts.production.test_worker` and `scripts.production.test_worker_admission` pass; basedpyright is clean on the changed files; the merge gate passes.

### Phase 4 — A restored Codex worker resumes with its full launch policy  · status: todo

**Depends on:** none.

**Spec:** Written at phase start. Source: sdlc asked models to find the cause of `running launch policy is incomplete` and fix it where it lives. `scripts/shutdown/pane_record.py` `resume_arguments` resumes Codex with `resume <id>` and `--model` only, without `--sandbox` and `--ask-for-approval`. `worker_panes._live_policy` then finds the running policy incomplete.

### Phase 5 — One way to read a Codex thread  · status: todo

**Depends on:** Phase 3.

**Spec:** Written at phase start. Source: sdlc asked models to remove the other rollout-file thread reads so one way remains: `staff_sources.py` `CodexProcessThreads.thread`, `assign.py` `herdr.codex_transcript`, the `RolloutProofPhase.STABLE` read in `herdr.native_process`, and the app-server multi-thread scan in `herdr.py`.
