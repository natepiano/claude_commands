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

#### Work Order

**Goal:** a Claude launch that names a family word (`fable`, `opus`, `sonnet`, `haiku`) passes the full model id from the launch account's model catalog, when the installed Claude CLI can run it. Otherwise it passes the bare alias, as now.

**Spec:**

Cause: `claude --model fable` maps the bare alias through the CLI's built-in table (2.1.319: `claude-fable-5-1`). The downloaded catalog names `claude-fable-5-2`. `/model` reads the catalog; `--model` does not.

1. In `_agents_model_slug_from_table`, the Claude branch (family `claude`, word is a `[claude.families]` key) resolves the word as follows:
   1. Catalog folder: `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/cache/model-catalog/`. Read only files named `*-cc.json`. When more than one exists, use the one with the largest `.fetchedAt`.
   2. Target: `.catalog.config.provider_alias_targets.<word>.default`.
   3. Minimum CLI version: the `.min_claude_code_version` of the entry in `.catalog.config.models[]` whose `.id` equals the target. A `null` or absent value means no minimum.
   4. Installed CLI version: the first word of `claude --version` (`2.1.319 (Claude Code)` gives `2.1.319`). Compare versions as dotted integers.
   5. Print the target when the target exists and the installed version is at or above the minimum. In every other case, print the `[claude.families]` value as now: no catalog, an unreadable catalog, no target, no `claude` binary, or an older CLI.
2. This branch is the one resolver. It sits beside the Codex newest-model branch in the same function. Do not add a copy in Python or elsewhere.
3. `launch_settings` already runs `agents_resolve`, so it gets the full id with no change. Keep one bash and no Python registry process.
4. Find every Claude launch that passes a family word to `--model` without `agents_resolve` or `launch_settings`. Candidates to read: `scripts/production/add_unit.py` (`director_agent`, line ~458 and ~796), `scripts/direct/assign.py` (`resolve_model`, line ~480), `scripts/direct/claude_worker.py` (line ~157), `scripts/agents/restart.py`, `scripts/agents/stop_resume.py`, `scripts/agents/pane_record.py`, the review launchers. Route each one that bypasses the resolver through `launch_settings` or `agents_resolve`. A path that passes the caller's own full model id (`add_showrunner.py --model`, `promote_showrunner.py`) stays as it is. Name every path you read and what it does in your summary.
5. Effort validation (`[claude.agents]`) keys on the family word and stays as it is. Resolve the model after validation.
6. Store no model ids by hand. Do not change `config/models.conf`.
7. Add no guard, refusal, scan or fallback beyond step 1.5.
8. Repair round 1 (blind review F001-F003): resolve at the launch, after the account is chosen. `agents_resolve` keeps the Claude family word in `AGENT_MODEL`. Each Claude launch, resume and override passes its model through the step 1 resolver with the launch account's environment. A full model id passes through unchanged.

**Files:**
- `scripts/agents/models_config.sh` — the Claude branch of `_agents_model_slug_from_table`.
- `scripts/direct/assign.py` — `resolve_model`: an `--model` override sets `AGENT_MODEL` to the bare pair word and skips `_agents_model_slug`.
- `scripts/message/send.py` — `MODEL = "sonnet"` (line ~184) goes straight to `claude -p --model`.
- `scripts/notify/review.py` — the `claude -p --model` call (line ~757); read where its model comes from.
- `scripts/production/unit_effort.py` — `READ_ROW` looks up `[claude.agents]` by `AGENT_MODEL`; use `AGENT_WORD` (step 5).
- `scripts/agents/test_models_config.sh` — fixture-catalog rows.
- `scripts/lib/test_models_registry.py` — one `launch_settings` row that returns the catalog target.

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/agents/models_config.sh` and the launchers from step 4.
- `test` — `scripts/agents/test_models_config.sh` and `scripts/lib/test_models_registry.py`. Each test makes a temporary `CLAUDE_CONFIG_DIR` with a fixture `*-cc.json`, and a fake `claude` on `PATH` that prints a version. Rows: target used at or above the minimum; bare alias below the minimum; bare alias with no catalog; the largest `fetchedAt` wins; a null minimum. Use no live unit, account folder or real catalog.

**State:** none.

**Constraints from prior phases:** `launch_settings` sends the registry table to bash on stdin and loads it with `_models_table_load`. Keep that.

**Acceptance gate:**
1. `bash scripts/agents/test_models_config.sh` passes.
2. The unittest line passes for `scripts.lib.test_models_registry`, `scripts.herdr.test_worker_panes`, `scripts.direct.test_assign_launcher`, and the test module of each changed launcher.
3. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.
4. On natedev, the Claude command that a `dwight` restart builds carries `--model claude-fable-5-2` with the live catalog and CLI 2.1.319.

### Phase 2 — Tests never write the live model registry  · status: done

**Result:** no code change. Commit `091b0cdbc` closed the write path: `test_sync_codex_catalog.sh` and `test_models_config.sh` now set `HOME` to a temporary folder and `MODELS_CONFIG_PUBLISH=0`. Commit `4c0d4a0fd` removed the fixture rows from `config/models.conf`.

#### Work Order

**Goal:** no test run writes or publishes `~/.claude/config/models.conf`. Fix the write path at its source.

**Spec:**

Evidence: commit `ca1c79027` (2026-11-05 01:33, description "add Codex catalog models and efforts") published the fixture rows of `scripts/agents/test_sync_codex_catalog.sh` into the live registry: `gpt-9-sol=high`, `gpt-6.9-sol=high,xhigh`, `gpt-6.10-sol=high,xhigh` in `[codex.agents]` and `sol=gpt-6.10-sol` in `[codex.families]`. The live file equals that test's expected output at line ~124. Only `sync_codex_catalog.sh` uses that description, through `models_registry_store`. `store_registry` publishes only when the registry path equals `DEFAULT_PATH` and `MODELS_CONFIG_PUBLISH` is not `0`. At that commit, the sync test and `test_models_config.sh` did not set `HOME`; commit `091b0cdbc` (08:04 the same day) added `export HOME="$TEST_DIR/home"`.

1. Find the exact path that made the live write. Read `scripts/agents/test_sync_codex_catalog.sh` and `scripts/agents/test_models_config.sh` at `ca1c79027`, and the code they drove (`sync_codex_catalog.sh`, `models_config.sh`, `models_registry.py` `_sync_catalog` and `store_registry`). Name the file, line and environment that reached `DEFAULT_PATH`.
2. Check the current tree for the same path. Read every test that runs `sync_codex_catalog.sh`, `models_registry_store` or `models_registry.py store|catalog`.
3. When the current tree still reaches the live registry, fix it at the source. When `091b0cdbc` or a later commit closed it, change no code and name that commit.
4. Do not change `config/models.conf`. The director removes the fixture rows in a separate commit.
5. Add no guard, refusal, scan or check (user rule). A fix changes the path that wrote; it does not add a detector.

**Files:**
- `scripts/agents/test_sync_codex_catalog.sh` — the fixture rows and the sync calls.
- `scripts/agents/sync_codex_catalog.sh` — the sync writer.
- `scripts/lib/models_registry.py` — `_sync_catalog`, `store_registry`, `_publish_repository`.

**Workers:** 1 writer. `fix2` — the three files above. Read `scripts/agents/test_models_config.sh` and `scripts/lib/test_models_registry.py` only; phase 1 owns them.

**State:** none.

**Constraints from prior phases:** phase 1 edits `scripts/agents/models_config.sh` at the same time. Do not edit it.

**Acceptance gate:**
1. `bash scripts/agents/test_sync_codex_catalog.sh` passes.
2. basedpyright over a changed Python file reads `0 errors, 0 warnings, 0 notes`.
3. The reply names the write path, its cause, and the fix or the commit that closed it.
