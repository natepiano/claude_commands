# Claude family resolver

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A Claude launch that names a family runs the newest model of that family.

> **As-built disposition: amend** — `docs/as-built/agent-registry.md`

## Delegation Context

- **Project:** `~/.claude` scripts — the agent registry and its launchers.
- **Project started:** 2026-11-11T13:23:32.076+00:00
- **Stack:** Bash 3.2+ with jq; Python 3.11+ (basedpyright strict).
- **Layout:**
  - `config/models.conf` — the registry; `[claude.families]` maps a family word to itself (`fable=fable`).
  - `scripts/agents/models_config.sh` — the one shell resolver (`agents_resolve`, `_agents_model_slug_from_table`).
  - `scripts/lib/models_registry.py` — `launch_settings(section, key)`, which runs `agents_resolve` in one bash.
- **Key files:**
  - `scripts/agents/models_config.sh` — `_agents_model_slug_from_table` (line ~368) turns a family word into a model. Its Codex branch already picks the newest visible cache model. Its Claude branch returns the `[claude.families]` value, the bare alias.
  - `scripts/lib/models_registry.py` — `launch_settings` (line ~134).
  - `scripts/agents/test_models_config.sh` — shell resolver tests.
  - `scripts/lib/test_models_registry.py` — `launch_settings` tests.
- **Test lanes:** `scripts/agents/test_models_config.sh`, `scripts/lib/test_models_registry.py`, plus the direct test module of each changed launcher.
- **Build:** none.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest <modules>` and `bash scripts/agents/test_models_config.sh`.
- **Lint:** `uvx basedpyright <changed Python files>` — `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - `agents_resolve` stays the one resolver. No resolver logic in Python.
  - `launch_settings` starts one bash and no Python registry process.
  - The user adds no checks: add no guard, refusal or scan that this Work Order does not name.

## Phases

### Phase 1 — A Claude family launch runs the newest model  · status: done

#### As-built

- `_agents_model_slug claude <word>` (Claude branch of `_agents_model_slug_from_table`) reads the `*-cc.json` file with the largest `.fetchedAt` under `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/cache/model-catalog/`. It prints `.catalog.config.provider_alias_targets.<word>.default` when the installed `claude --version` is at or above that model's `min_claude_code_version`. In every other case it prints the `[claude.families]` value. A full model id passes unchanged.
- `agents_resolve` and `launch_settings` keep the family word in `AGENT_MODEL`; `AGENT_WORD` keys `[claude.agents]` effort lookups.
- `models_registry.claude_model(model, environment)` and `claude_arguments(arguments, environment)` run that resolver with the launch account's environment. A failed call returns the input model.
- Each Claude launch resolves after the account is chosen: `herdr.launch_prepared_agent`, `stop_resume` (producer first start, own-pane exec), `accounts.exec_account`, `relaunch.child_launch`, `claude_worker`, `agent_exec.sh`, `cli_agent.sh`, the `send.py` relay and `review.py`.
- `worker_panes._live_policy` compares an unchanged explicit Claude policy with its resolved model, so reuse holds.

**Files:**
- `scripts/agents/models_config.sh` — the one resolver.
- `scripts/lib/models_registry.py` — `claude_model`, `claude_arguments`.
- `scripts/herdr/herdr.py`, `scripts/agents/stop_resume.py`, `scripts/accounts/accounts.py`, `scripts/lib/relaunch.py`, `scripts/direct/claude_worker.py`, `scripts/agents/agent_exec.sh`, `scripts/cli_agent/cli_agent.sh`, `scripts/message/send.py`, `scripts/notify/review.py` — launch-time resolution.
- `scripts/herdr/worker_panes.py` — `_live_policy` reuse comparison.

**Binds later work:** resolution happens at the launch with the launch account's environment; `AGENT_MODEL` and `launch.model` hold the family word until then.

**Gotchas:** each account has its own catalog; resolving with the caller's environment picks the wrong target.

**Ruled out:** resolving inside `agents_resolve` — it runs with the caller's account, not the launch account.

### Phase 2 — Tests never write the live model registry  · status: done

#### As-built

No code change. `test_sync_codex_catalog.sh` and `test_models_config.sh` set `HOME` to a temporary folder and `MODELS_CONFIG_PUBLISH=0`, so no test run reaches `DEFAULT_PATH` or publishes `config/models.conf`. The fixture rows that an earlier test run published are removed from `config/models.conf`.

**Files:**
- `scripts/agents/test_sync_codex_catalog.sh`, `scripts/agents/test_models_config.sh` — temporary `HOME`, publish off.

### Phase 3 — Run records name the model that ran  · status: done

**Depends on:** Phase 1.

#### Work Order

**Goal:** the review and assign run records store the model id that the Claude launch passed to `--model` (for example `claude-fable-5-2`), not the family word (`fable`).

**Spec:**

Cause: phase 1 keeps the family word in `AGENT_MODEL` and `launch.model` and resolves it only at the launch. The records are written before that, from the word.

1. `scripts/direct/review.sh` writes `agent=${AGENT_MODEL}` to the agent file (line ~242), the board `register` line (~245), `--called-model "${AGENT_MODEL}"` (~259) and the heartbeat header (~277). `agent_exec.sh` resolves later, after `claude_launch_environment`. Each of these must carry the resolved id for a Claude review.
2. `scripts/direct/assign_claude.py` writes `launch.model` to `impl_agent_<slot>` (~233), `--called-model` (~242), the heartbeat header (~251), the board line (~253) and the worker record (~266). `claude_worker.py:157` resolves with `loaded.env`. Each record must carry the id that `claude_worker.py` passes.
3. Resolve once per launch with the launch account's environment, through `models_registry.claude_model` or `_agents_model_slug claude`. Use that one value for the record and for the launch. Do not add a second resolver or a Python copy of its logic.
4. Effort validation and `[claude.agents]` lookups keep the family word.
5. Codex launches stay as they are.
6. Add no guard, refusal, scan or fallback (user rule). A failed resolver keeps the input model, as in phase 1.

**Files:**
- `scripts/direct/review.sh` — the review records listed in Spec 1.
- `scripts/agents/agent_exec.sh` — read where the Claude account environment and the resolved model are set.
- `scripts/direct/assign_claude.py` — the assign records listed in Spec 2.
- `scripts/direct/claude_worker.py` — the launch resolve at line ~157.
- `scripts/direct/test_review_launcher.py` — review record rows.
- `scripts/direct/test_assign_claude_records.py` — assign record rows.

**Workers:** 1 writer + 1 tester.
- `impl` — `review.sh`, `agent_exec.sh`, `assign_claude.py`, `claude_worker.py`.
- `test` — `test_review_launcher.py` and `test_assign_claude_records.py`. Each row makes a temporary `CLAUDE_CONFIG_DIR` with a fixture `*-cc.json` and a fake `claude` that prints a version. A Claude review and a Claude assign with the word `opus` record the account's catalog target in the agent file and in `--called-model`. Each row must fail on the phase 1 tree.

**State:** none.

**Constraints from prior phases:** resolve after the launch account is chosen (phase 1 design). `launch_settings` and `agents_resolve` keep the family word.

**Acceptance gate:**
1. The unittest line passes for `scripts.direct.test_review_launcher`, `scripts.direct.test_assign_claude_records`, every other `scripts.direct.test_assign_claude*` module, `scripts.direct.test_progress_history` and `scripts.lib.test_models_registry`.
2. `bash scripts/agents/test_agent_exec.sh` and `bash scripts/agents/test_models_config.sh` pass.
3. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.
