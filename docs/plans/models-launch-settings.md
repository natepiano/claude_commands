# Launch settings without a registry subprocess

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** sdlc approved this on 2026-11-09. It comes from the builds natest phase 89 proposal: `launch_settings` reads the registry in Python and passes it to the existing shell resolver. Each real launch and each test resolution then starts one fewer Python process.

## Delegation Context

- **Project:** `~/.claude` scripts — the model registry reader and the shell agent resolver.
- **Project started:** 2026-11-09T03:55:00.000+00:00
- **Stack:** Python 3.13, `unittest`, basedpyright; bash.
- **Layout:**
  - `scripts/lib/models_registry.py` — `launch_settings`, `LaunchModelConfiguration`, `_snapshot`, `configuration_shell`.
  - `scripts/agents/models_config.sh` — `agents_resolve`, `_models_registry_read`, `_models_registry_parse`.
  - `scripts/lib/test_models_registry.py` — `launch_settings` tests against the shell resolver.
  - Callers: `scripts/herdr/worker_panes.py` `launch_policy`, `scripts/direct/assign.py` `pane_policy`.
- **Test lanes:** none (unit tests sit beside their modules).
- **Build:** none.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest <modules>`
- **Lint:** `basedpyright <changed files>` must end `0 errors, 0 warnings, 0 notes`.
- **Invariants:** add no check the Work Order does not name (user, 2026-11-05 and 2026-11-07).

## Phases

### Phase 1 — A launch reads the registry once, in Python  · status: done
**Depends on:** none

#### Work Order

**Source:** builds natest phase 89 architecture proposal (`/tmp/claude/unit/2113de4f-9913-485a-9a51-61558bfd81db/phases/89/current/architecture_proposal.md`). One same-load pair: `test_assign_launcher.py` 64.3 to 54.7 CPU s (14.9% less), all 63 tests pass.

**Goal:** `models_registry.launch_settings(section, key)` starts one `bash` process and no Python registry process. The resolver output stays the same for every caller.

**Spec:**
1. In `launch_settings`, build the launch configuration in Python with `_snapshot(os.environ, OPERATION_ENVIRONMENT[0], registry_path())`. A `MODELS_LAUNCH_CONFIGURATION` that the caller inherits still wins, as now. Do not sync the Codex catalog; the current call sets `MODELS_CONFIG_TIER_ONLY=1`, which skips it.
2. Pass `configuration_shell(configuration)` to the `bash -c` command. The shell evaluates it and sets `_models_table_file` to `MODELS_CONFIG_FILE`, so `_models_registry_read` uses the table and `_models_registry_parse` never runs. Then the shell calls the real `agents_resolve`.
3. Keep `agents_resolve`, its validation and its error text. There is one resolver: do not copy any resolver logic into Python.
4. Keep `MODELS_CONFIG_TIER_ONLY=1` in the child environment and the four-line output contract.
5. If the shell resolver needs a helper to load a given table, add it to `models_config.sh` beside `_models_registry_scope` and use the same table variables.
6. Read every caller of `launch_settings` (`worker_panes.launch_policy`, `assign.pane_policy`) and every test that patches or calls it. Each one keeps its result.
7. Add no guard, refusal or check (user rule, 2026-11-05).

**Files:**
- `scripts/lib/models_registry.py`
- `scripts/agents/models_config.sh`
- `scripts/lib/test_models_registry.py`

**Workers:** 1 writer; one function and its tests do not split.
- `impl` — `models_registry.py`, `models_config.sh`, `test_models_registry.py`

**Acceptance gate:**
- The existing `launch_settings` tests pass unchanged: shell parity for every family, section, key and tier; a registry change affects the next launch; invalid rows raise `ValueError`.
- A new test row: an inherited `MODELS_LAUNCH_CONFIGURATION` decides the result, not the file on disk.
- A new test row: one `launch_settings` call starts no `models_registry.py` process.
- `scripts/lib/test_models_registry.py`, `scripts/agents/test_models_config.sh`, `scripts/herdr/test_worker_panes.py`, `scripts/direct/test_assign_launcher.py` and `scripts/direct/test_assign_launcher_review.py` pass.
- CPU s for `test_assign_launcher.py` before and after, from one same-load pair, in the checkpoint notice.
- `basedpyright` on the changed files prints `0 errors, 0 warnings, 0 notes`.
