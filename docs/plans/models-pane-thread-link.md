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
- `scripts/hooks/test_codex_hooks.py` — `install` repairs managed and unmanaged float timeouts.

The same fix also landed from natedev-messaging (`Codex hook install writes integer timeouts`); the merge keeps that version, so this phase adds no change of its own.

**Binds later work:** the herdr `SessionStart` hook reports a new Codex pane's thread at the first prompt, not at process start.

**Gotchas:** `install` rewrites a float timeout in an old `hooks.json` and trusts the changed handler again. natedev and the Mac were migrated by hand on 2026-11-08.

### Phase 3 — A Codex worker launch proves its thread without reading its rollout file  · status: done

#### As-built

Inside `herdr.launch_rollout_proof()` (`RolloutProofPhase.LAUNCH`), `herdr.native_process` for `codex` reads no rollout file. `_codex_launch_identity(pid, reported)` decides the thread:
- With a `resume <id>` argument, a matching pane report gives `PaneSessionReport`; no report or another report gives `LaunchArgument(<id>)`, which `pane_native_process` turns into `UnreportedResume` when unreported.
- With no `resume` argument, the pane report gives `PaneSessionReport(reported)`; no report raises `Codex launch identity is unavailable`.

Under `RolloutProofPhase.STABLE` the rollout read stays, and `NoOpenCodexRollout` falls back to `_codex_launch_identity`.

In `worker.py` `_dispatch_turn`, a `DispatchEvidenceUnknown` in the wait loop after the prompt is typed prints `anomaly: <worker>: <reason>; prompt delivered to pane <pane>`. The same failure before typing prints `no prompt sent`. Exit codes do not change.

**Files:**
- `scripts/herdr/herdr.py` — `_codex_launch_identity` and `native_process`.
- `scripts/herdr/test_herdr.py` — `native_process` rows: stable, new launch with a foreign rollout, new launch with no rollout, resume, resume with a growing rollout.
- `scripts/production/worker.py` — the wait-loop failure line.
- `scripts/production/test_worker.py` — a transcript failure before and after delivery.

**Binds later work:** the `RolloutProofPhase.STABLE` read in `herdr.native_process` is the last rollout read on the launch path.

**Gotchas:** the herdr `SessionStart` hook reports a new Codex pane's thread about 2 s after the first prompt, not at process start. A resumed Codex appends to its own rollout file, so a stat, read and second stat of that file fails as "current Codex rollout changed".

### Phase 4 — A restored Codex worker resumes with its full launch policy  · status: done

**Depends on:** none. panes Phase 34 adds a `-c notify=` pair that this record keeps.

#### Work Order

**Goal:** A Codex worker restored from its pane record resumes with the same launch policy it started with, so the pane worker check finds its policy complete.

**Spec:**
Fault (sdlc asked models to find the cause and fix it where it lives): `scripts/shutdown/pane_record.py` `resume_arguments` resumes Codex with `resume <id>` and `--model <m>` only. The record keeps no `--sandbox`, `--ask-for-approval` or `-c` options. `worker_panes._live_policy` requires `--model`, `--sandbox` and `--ask-for-approval` in the running command, so a restored worker reads `running launch policy is incomplete`. A resumed reviewer also runs without its sandbox and approval policy (cargo-liner defect).

Fix:
1. `scripts/herdr/pane_launch.py`:
   - `PaneLaunch` gets `codex_policy: NotRequired[list[str]]`: the Codex policy options of the launch, in launch order, as option and value pairs.
   - `launch(..., kind="codex")` fills it from the arguments: each `--sandbox <v>`, `--ask-for-approval <v>` and `-c <v>`, also the `--opt=<v>` form. A `kind="claude"` launch does not get the field.
   - `_validated_launch` accepts the field only as a list of strings.
2. `scripts/shutdown/pane_record.py` `resume_arguments`: a Codex entry gives `resume <id>`, then `--model <m>` when set, then the recorded `codex_policy` unchanged. An entry without the field gives today's arguments.

Every `-c` pair is kept, so the `model_reasoning_effort`, `notice.hide_rate_limit_model_nudge`, `service_tier` and the later panes Phase 34 `notify` pair all come back on a resume.

Add no other check (user, 2026-11-05 and 2026-11-07).

Test rows:
- `scripts/herdr/test_pane_launch.py`, in the existing launch-record tests:
  1. A Codex launch records its `--sandbox`, `--ask-for-approval` and `-c` pairs in order; a Claude launch records none.
- `scripts/shutdown/test_pane_record.py`, in the existing resume-argument or Codex record tests:
  2. A Codex entry with `codex_policy` resumes with `resume <id> --model <m>` followed by the recorded pairs.

**Files:**
- `scripts/herdr/pane_launch.py` — `PaneLaunch.codex_policy`, `launch`, `_validated_launch`.
- `scripts/herdr/test_pane_launch.py` — row 1.
- `scripts/shutdown/pane_record.py` — `resume_arguments`.
- `scripts/shutdown/test_pane_record.py` — row 2.

**Workers:** 2 writers.
- `impl`: `scripts/herdr/pane_launch.py`, `scripts/herdr/test_pane_launch.py` (row 1).
- `test`, as a writer: `scripts/shutdown/pane_record.py`, `scripts/shutdown/test_pane_record.py` (row 2).

**State:** a new optional field in the retained pane launch record. Old records without it resume as today.

**Acceptance gate:** `scripts.herdr.test_pane_launch`, `scripts.shutdown.test_pane_record`, `scripts.shutdown.test_stop_resume` and `scripts.herdr.test_worker_panes` pass; basedpyright is clean on the changed files; the merge gate passes.

### Phase 5 — One way to read a Codex thread  · status: todo

**Depends on:** Phase 3.

**Spec:** Written at phase start. Source: sdlc asked models to remove the other rollout-file thread reads so one way remains: `staff_sources.py` `CodexProcessThreads.thread`, `assign.py` `herdr.codex_transcript`, the `RolloutProofPhase.STABLE` read in `herdr.native_process`, and the app-server multi-thread scan in `herdr.py`.
