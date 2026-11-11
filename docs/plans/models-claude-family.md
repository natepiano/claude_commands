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

#### As-built

- A Claude review and a Claude assign record the model id that the launch passes to `--model` (for example `claude-fable-5-2`), not the family word. The agent file, board `register` line, `--called-model`, heartbeat header and worker record carry that one value.
- `review.sh` sources `claude_account.sh`, runs `claude_launch_environment workers` and resolves `AGENT_MODEL` before it writes records. It passes `AGENT_EXEC_CLAUDE_MODEL`, so `agent_exec.sh` skips its own account choice and resolve.
- `claude_worker.prepare_launch(turn)` returns `PreparedClaudeLaunch(model, environment, loaded)`. `assign_claude.py` writes every record from `prepared.model` and passes `launch=prepared` to `open_worker`.
- `_switch` returns `NewClaudeWorker`, `ClaudeDeliveryRetry` or `ClaudeFollowUp`. Only `NewClaudeWorker` prepares a launch. A delivery retry and a follow-up take the model from the saved worker record.
- Effort validation and `[claude.agents]` lookups keep the family word. Codex launches are unchanged.

**Files:**
- `scripts/direct/review.sh`, `scripts/agents/agent_exec.sh` — review records and launch.
- `scripts/direct/assign_claude.py`, `scripts/direct/claude_worker.py` — assign records and launch.
- `scripts/direct/recorder_fixture.py` — removes the caller's Claude account variables from the shared fixture.
- `scripts/direct/test_review_launcher.py`, `scripts/direct/test_assign_claude_records.py`, `scripts/direct/test_assign_claude_follow.py` — record rows with a fixture catalog.

**Gotchas:** a delivery retry reuses the published session without a new start, so a new resolve there records a model that never ran.

**Ruled out:** a second resolve on a delivery retry.
