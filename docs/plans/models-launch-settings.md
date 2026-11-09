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

#### As-built

- `launch_settings(section, key)` builds `LaunchModelConfiguration(*_snapshot(os.environ, OPERATION_ENVIRONMENT[0], registry_path()))` in Python. It starts one `bash`, which sources `models_config.sh`, reads `configuration_shell(configuration)` from stdin, loads it with `_models_table_load`, and runs the real `agents_resolve`. No Python registry process starts.
- An inherited `MODELS_LAUNCH_CONFIGURATION` decides the result, not the file on disk. The child keeps `MODELS_CONFIG_TIER_ONLY=1`, so the Codex catalog sync never runs from this path, and the four-line output contract is unchanged.
- `_models_table_load <assignments>` in `models_config.sh` is the one table loader: it evaluates the assignments and sets `_models_table_file="$MODELS_CONFIG_FILE"`. `models_registry_store`, `_models_registry_parse` and `launch_settings` all call it.
- A missing registry raises `OSError` in Python before bash starts. `worker_panes.launch_policy` and `assign.pane_policy` catch `OSError` and `ValueError` and refuse with an invalid role configuration, as before.
- One same-load pair of `test_assign_launcher.py`: 68.3 to 62.1 CPU s (9.1% less).

**Files:**
- `scripts/lib/models_registry.py` — `launch_settings`.
- `scripts/agents/models_config.sh` — `_models_table_load`.
- `scripts/lib/test_models_registry.py` — inherited-configuration rows; `test_launch_settings_starts_no_python_registry_process`, with a sitecustomize probe and a 70,000-character registry row.

**Gotchas:**
- The table goes to bash on stdin, never as an argument. It holds the registry text about twice, and Linux limits one argument to 128 KiB.
- `read -r -d ""` returns 1 at end of input, so it is not chained with `&&`. `source /dev/stdin` is not used, because Bash 3.2 on macOS fails with it.
- The table text is plain assignments, never `declare`, so `_models_table_load` sets the caller's locals in `_models_registry_scope` through dynamic scope.

**Ruled out:** resolver logic in Python — `agents_resolve` stays the one resolver.
