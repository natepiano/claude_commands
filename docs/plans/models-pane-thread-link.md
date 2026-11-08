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

#### Work Order

**Goal:** Every reader finds a Codex process's thread one way: its resume argument, or the `SessionStart` report on its herdr pane. No code reads a thread from an open rollout file.

**Spec:**
Source: sdlc accepted the 28c design ([1a39967b]) and asked models to remove the other rollout-file thread reads so one way remains: `staff_sources.py` `CodexProcessThreads.thread`, `assign.py` `herdr.codex_transcript`, the `RolloutProofPhase.STABLE` read in `herdr.native_process`, and the app-server multi-thread scan in `herdr.py`.

The one rule is Phase 3's `_codex_launch_identity(pid, reported)`, unchanged:
- A `resume <id>` argument and a matching pane report give `PaneSessionReport`. Another report gives `LaunchArgument(<id>)`. No report in a pane gives `UnreportedResume(<id>)` (today's `pane_native_process` conversion).
- No `resume` argument and a pane report give `PaneSessionReport(reported)`. No report raises `Codex launch identity is unavailable`.
- The pane report counts only when the pane's sole foreground process is that Codex (`codex_without_session`), as today.

Live evidence (natedev, 2026-11-08 15:10 EST): of 35 Codex processes, all sit in herdr panes. Each resume argument or pane report names the same thread as its open rollout. One pane (w16:p86, started 12:17 before the Phase 2 hooks fix) has neither; it becomes "Codex thread unknown" until it ends.

1. `scripts/herdr/herdr.py`:
   - `native_process(pid, "codex", reported)` reads no rollout file. It always uses the one rule.
   - A caller that passes no report and whose Codex process runs in a herdr pane gets that pane's report and `UnreportedResume` conversion, so today's results for pane processes do not change. Callers today: `Herdr._codex_launch`, `check_native_target`, `codex_conversations`, `_process_marks`, `shutdown/pane_record.py`, `shutdown/stop_resume.py`, `shutdown/restart.py`. Do the pane read in one place, not at each caller.
   - `codex_conversations` reads process places once per scan (`herdr_places()` and the shared pane observation), not one `/proc` scan per process. It drops the "each open rollout proves its thread" fallback. An `app-server` in a run stays resolved by the run read (`loaded_threads`). A daemon-attached TUI whose account has a daemon read stays covered by that read. Any other Codex process with no identity gives the existing `Codex thread unknown` line.
   - Remove `RolloutThread`, `NoOpenCodexRollout`, `_open_codex_rollouts`, `RolloutProofPhase`, `_rollout_proof_phase`, `launch_rollout_proof`, `codex_rollout_threads`, `codex_rollout_thread` and `codex_transcript`.
2. `scripts/herdr/worker_panes.py`: remove the two `with herdr.launch_rollout_proof():` wrappers in `_start_locked`; the bodies stay.
3. `scripts/production/staff_sources.py` `CodexProcessThreads.thread`: remove the rollout read. Read the resume argument first, then the pane report, then the inherited `CODEX_THREAD_ID`, then the terminal title lookup. A child Codex inherits its parent's `CODEX_THREAD_ID`, so that value comes after both direct sources (repair F003).
4. `scripts/direct/assign.py` `execute_consultation`: read the consultation thread from its pane with `client.native_session(pane, "codex", ...)`. The question is typed before this read, so wait for the report until `herdr.PROGRAM_TIMEOUT_SECONDS`, as `worker_panes.first_turn_session` does. Find the transcript by thread id under the process's `CODEX_HOME` the way `worker_admission.transcript_source` does for a Codex row. Use one shared helper, not a copy.
5. Update the as-built text that describes the removed reads: `docs/as-built/showrunner-automation.md` (`CodexProcessThreads.thread`), `docs/as-built/herdr-restore.md` ("an open rollout proves the pane"), `docs/as-built/shutdown-and-restart.md` (native pane reader).

Add no other check (user, 2026-11-05 and 2026-11-07).

Test rows: change the existing rows; add none beyond these.
- `scripts/herdr/test_herdr.py`:
  1. `native_process` rows: a new Codex in a pane with a foreign rollout open gives the pane report; a resume gives its argument; a stable call with no report reads the pane report of its own pane.
  2. `codex_conversations`: a pane Codex with an open rollout of another thread is live under its reported thread.
- No consultation test exists, so item 4 adds no row; the merge gate and review cover it.
- Fixtures that prove identity with an open rollout (fd symlink or a `codex_rollout_thread` mock) use a pane report or a resume argument instead. Do not weaken an assertion.

**Files:**
- `scripts/herdr/herdr.py` — `native_process`, `codex_conversations`, removed rollout readers.
- `scripts/herdr/worker_panes.py` — `_start_locked` wrappers.
- `scripts/herdr/test_herdr.py`, `scripts/herdr/test_herdr_sessions.py`, `scripts/herdr/test_conversation_reader.py`, `scripts/herdr/test_worker_panes.py` — rows 1–2 and fixtures.
- `scripts/production/staff_sources.py` — `CodexProcessThreads.thread`.
- `scripts/direct/assign.py` — `execute_consultation`.
- `scripts/production/worker_admission.py` — the shared transcript-by-thread helper.
- `scripts/production/test_staff_sources.py`, `scripts/production/test_session_staff.py`, `scripts/production/test_worker.py`, `scripts/production/test_session_pane.py`, `scripts/production/test_worker_admission.py`, `scripts/production/rename_test_support.py`, `scripts/production/rename_unit_test_support.py` — fixtures.
- `scripts/shutdown/test_pane_restore.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_stop_resume.py`, `scripts/shutdown/test_shutdown_cli.py`, `scripts/hooks/test_conversation_pause.py` — fixtures.
- `docs/as-built/showrunner-automation.md`, `docs/as-built/herdr-restore.md`, `docs/as-built/shutdown-and-restart.md` — item 5.

**Workers:** 2 writers.
- `impl`: `scripts/herdr/` files (items 1–2, rows 1–2, herdr fixtures).
- `test`, as a writer: items 3–5, and every fixture file outside `scripts/herdr/`.

**State:** none.

**Acceptance gate:** every changed test module passes; `grep -rnE "_open_codex_rollouts|codex_rollout_thread|launch_rollout_proof|RolloutThread|NoOpenCodexRollout" scripts` finds nothing; basedpyright is clean on the changed files; the merge gate passes.
