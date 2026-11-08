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

#### As-built

`PaneLaunch.codex_policy: NotRequired[list[str]]` holds the Codex launch's `--sandbox`, `--ask-for-approval` and `-c` option and value pairs in launch order. `pane_launch.launch(..., kind="codex")` fills it from the paired and the `--opt=<v>` forms, stops at `--`, and does not record a `-c projects=...` pair. A Claude launch has no field. `_validated_launch` accepts the field only as a list of strings.

`pane_record.resume_arguments` gives `resume <id>`, then `--model <m>` when set, then the recorded pairs unchanged. A record without the field resumes with `resume <id>` and `--model <m>` only. A restored worker passes `worker_panes._live_policy`, and every `-c` pair (`model_reasoning_effort`, `notice.hide_rate_limit_model_nudge`, `service_tier`, the panes `notify` pair) comes back on a resume.

**Files:**
- `scripts/herdr/pane_launch.py` — `PaneLaunch.codex_policy`, `launch`, `_validated_launch`.
- `scripts/herdr/test_pane_launch.py` — launch-record rows: Claude, Codex paired, Codex `=` form, a restored-process argv with the trust override.
- `scripts/shutdown/pane_record.py` — `resume_arguments`.
- `scripts/shutdown/test_pane_record.py` — resume rows with and without a recorded policy and a model.

**Gotchas:** `codex_trust.trust_arguments` puts `-C <root> -c projects={<root>={trust_level="trusted"}}` before every Codex resume, and the record of a restored process is read from that full argv. A recorded `projects` pair would come after the new one and win after a folder move, so the record leaves it out.

### Phase 5 — One way to read a Codex thread  · status: done

**Depends on:** Phase 3.

#### As-built

A Codex process's thread comes from its `resume <id>` argument or from the `SessionStart` report on its herdr pane, through `_codex_launch_identity(pid, reported)`. No code reads a thread from an open rollout file.

- `herdr.native_process(pid, kind, reported: str | PaneNotRead = PANE_NOT_READ)` reads no rollout. A Codex call with no `reported` finds the process's pane place and calls `codex_pane_process(pid, place)`, which reads that pane's report and keeps the `UnreportedResume` conversion. The pane read is in this one place, not at each caller.
- `codex_conversations` is `@observation_snapshot()`, so process places are read once per scan. An `app-server` stays resolved by `loaded_threads`, a daemon-attached TUI by its daemon read, and any other Codex with no identity gives `Codex thread unknown`.
- `worker_panes._start_locked` has no launch-proof wrapper.
- `staff_sources.CodexProcessThreads.thread` reads the resume argument, then the pane report, then the inherited `CODEX_THREAD_ID`, then the terminal title lookup. A process outside herdr, a pane whose foreground is not that Codex, and a failed herdr read give the `CODEX_THREAD_ID` value.
- `assign.execute_consultation` waits for `client.native_session(pane, "codex", ...)` until `herdr.PROGRAM_TIMEOUT_SECONDS`, reads `CODEX_HOME` from `herdr.process_environment(pid)`, and finds the transcript with `worker_admission.codex_transcript_source(thread_id, home)`, the helper `transcript_source` also uses.

**Files:**
- `scripts/herdr/herdr.py` — `native_process`, `codex_pane_process`, `codex_conversations`.
- `scripts/herdr/worker_panes.py` — `_start_locked`.
- `scripts/production/staff_sources.py` — `CodexProcessThreads.thread`.
- `scripts/direct/assign.py` — `execute_consultation`.
- `scripts/production/worker_admission.py` — `codex_transcript_source`.
- `scripts/production/test_staff_sources.py` — `test_resume_argument_overrides_an_inherited_thread_and_does_not_block_the_old_thread` rows: resume, non-foreground, failed herdr read.
- `scripts/herdr/test_herdr.py`, `scripts/herdr/test_worker_panes.py`, `scripts/shutdown/test_pane_record.py` and the other fixture modules — identity through a pane report or a resume argument; `PaneWorld.launch` writes the process `environ` file.

**Gotchas:** A child Codex inherits its parent's `CODEX_THREAD_ID`, so that value must never come before the resume argument or the pane report. A test fixture that gives identity only through an open rollout descriptor now fails with `Codex launch identity is unavailable`.

**Ruled out:** Reading the thread from an open rollout file — two Codex processes can hold one rollout, and a new pane read another worker's thread (w16:p94).
