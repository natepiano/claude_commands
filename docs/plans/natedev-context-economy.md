# Context economy: track context cost over time

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** One command reads the transcripts and the git history at run time, reports the context cost of each settings set, saves results as named baselines, and compares later data against them.

> **As-built disposition: amend** — `docs/as-built/usage-ledger.md`

## Delegation Context

- **Project:** `~/.claude` tooling (this repo). The context economy measures what each model request carries, when sessions compact, and which settings each session ran with. It reads its inputs at run time and stores only saved results (natedev, 2026-11-08: "store only what cannot be looked up").
- **Project started:** 2026-11-08T17:51:02.750+00:00
- **Stack:** Python 3, standard library only (`json`, `argparse`, `dataclasses`, `statistics`, `subprocess` for `git` and `ssh`, `sqlite3` read-only). basedpyright strict.
- **Layout:** `scripts/usage/` (new `context_economy.py`, new `context_economy_read.py`); `config/context_economy.conf` (new); `commands/context_economy.md` (new); `docs/as-built/usage-ledger.md`.
- **Key files:**
  - Claude transcripts: `~/.claude/projects/<folder>/<session id>.jsonl`, subagents under `<session id>/subagents/`. `~/.claude1..4/projects` are links to the same folder.
  - Claude transcript records the reader uses:
    - an `assistant` record with `message.id`, `message.model`, `message.usage` (`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`, `output_tokens`), top-level `timestamp`, `version` (the Claude Code version) and `requestId`. Deduplicate requests by (`message.id`, `requestId`); several records share one request.
    - a `system` record with `subtype == "compact_boundary"` and `compactMetadata`: `trigger` (`auto`/`manual`), `preTokens`, `postTokens`, `precomputed` (true only for a precomputed compaction). Example: `{"subtype":"compact_boundary","compactMetadata":{"trigger":"auto","preTokens":255292,"postTokens":15042,...}}`.
    - an `attachment` record with `attachment.type == "hook_success"`, `attachment.hookEvent == "SessionStart"` and `attachment.hookName` one of `SessionStart:startup`, `SessionStart:resume`, `SessionStart:compact`, `SessionStart:clear`. Only `startup` and `resume` are starts. Several hooks write one record each for one start; they share `toolUseID`.
  - `scripts/usage/rollouts.py` — the Codex rollout parser: `decode_rollout`, `timestamp_of`, `token_usage`, `is_compaction`, `ThreadRolloutBuilder`. Codex rollouts are under `~/.codex*/sessions/`.
  - `scripts/usage/weights.py` — `weights_at(model, at)` and `weighted(row, at)`: the weighted token cost of one request.
  - `scripts/usage/usage_report.py` — `open_read_store` and `check_store_version`; the ledger store `turns(machine, family, kind, parent_id, agent_id, ..., role, ...)` gives each agent's role.
  - `scripts/usage/collect.py` — the Mac pattern: `MAC_HOST` (line ~42), `REMOTE_BOOTSTRAP` (line ~1126), `reader_payload` (line ~1539), `collect_mac` (line ~1552): the module sources go over `ssh` to `python3 -c`, the Mac prints JSON lines, nothing is written on the Mac.
  - `scripts/usage/test_usage_report.py`, `scripts/usage/test_collect_mac.py` — test patterns: temporary folders and fixture files under `scripts/usage/fixtures/`.
  - `docs/as-built/usage-ledger.md` — the as-built this plan amends.
- **Test lanes:** `scripts/usage/test_*.py`; run with `python3 -m unittest` from the repo root.
- **Build:** none.
- **Test:** `python3 -m unittest scripts.usage.<module>` for each test module the phase names; then `python3 ~/.claude/scripts/production/targeted_test.py` before the checkpoint.
- **Lint:** `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- **Invariants:**
  - Store only what cannot be looked up later (user rule 2026-10-08; natedev 2026-11-08). Requests, compactions, starts and Claude Code versions come from transcripts and rollouts at run time. Settings and file changes come from git at run time. Only saved results are written.
  - Tests never read or write `~/.claude`, `~/.codex*`, the live `settings.json`, real transcripts, the real ledger store, the live checkout's git history or the Mac. Every path comes from an argument or an environment variable the tests set.
  - Estimated numbers and measured numbers never share a column. The report marks each estimate.
  - Every comparison prints both sample sizes and the other settings that changed between the two sides.
  - The command runs on demand only. It sends no message (memory `build-report-on-demand`).
  - The tracked settings keys and files come from `config/context_economy.conf`, never from code (memory `no-hard-coded-config`).
  - Add no guard, refusal or check the user did not ask for.

## Phases

### Phase 1 — Read sessions and settings at run time  · status: done
**Depends on:** none

#### Work Order

**Goal:** `context_economy_read.py` gives, for a time window, every Claude session's requests, compactions and starts from the transcripts, and the timeline of tracked settings from git.

**Spec:**
1. New `config/context_economy.conf`, read with the `scripts/lib` conf reader the other `config/*.conf` files use:
   ```
   # settings.json keys to track, space-separated
   settings_keys = autoCompactWindow precomputeCompactionEnabled model
   # repo files to track by size and short sha, space-separated
   files = commands/unit/direct.md docs/direct/write_prompt_contract.md
   # sessions with fewer requests than this are left out of comparisons
   min_requests = 20
   # a request gap longer than this ends an active stretch (seconds)
   active_gap_seconds = 600
   ```
2. Types in `scripts/usage/context_economy_read.py`:
   - `Request(at, model, cli_version, input, cache_read, cache_write_5m, cache_write_1h, output)`; `context` = input + cache_read + cache_write_5m + cache_write_1h.
   - `CompactionEvent(at, trigger: str | None, pre_tokens: int | None, post_tokens: int | None, kind: Literal['precomputed','normal','unknown'])`. `precomputed` when `compactMetadata.precomputed` is true; `normal` when `compactMetadata` exists without it; `unknown` when the record has no `compactMetadata`.
   - `Start(at, source: Literal['startup','resume'], cli_version: str | None)`, one per `toolUseID`, at the first record's time.
   - `Session(machine, family, session_id, agent_id, requests, compactions, starts)`; `agent_id` is the subagent id or the session id.
3. `read_claude(projects_root: Path, since: datetime, until: datetime, machine: str) -> list[Session]`. It opens only files whose modification time is at or after `since`, and keeps records with `since <= timestamp < until`. Requests are deduplicated by (`message.id`, `requestId`); the last record of a request gives its usage.
4. `SettingsSet(at, commit, values: dict[str, object])`: each tracked key's value (`None` when absent) and, for each tracked file, `{"bytes": N, "sha": "<12 hex>"}`. `settings_timeline(checkout: Path, since, until) -> list[SettingsSet]` reads `git -C <checkout> log --first-parent --format=%H%x00%cI main -- settings.json <tracked files>` and, for each commit, `git show <commit>:<path>`. It keeps a commit only when `values` differ from the previous kept one, and adds the set in force at `since`. The checkout path comes from `CONTEXT_ECONOMY_CHECKOUT` when set, else `Path.home() / ".claude"`.
5. `settings_for(session_start: datetime, timeline) -> SettingsSet | SettingsUnknown`: the latest set at or before the start. A session's requests belong to the set of its latest start at or before each request; a request before any start in the window uses the first record of its file.
6. `label(set, previous) -> str`: the changed values, for example `precomputeCompactionEnabled=false`.
7. `role_of(store: Path, machine, family, session_id, agent_id) -> str`: the latest `turns.role` in the ledger store, opened read-only with `usage_report.open_read_store`; `unknown` when the agent has no row or the store is missing.

**Files:**
- `config/context_economy.conf` — new.
- `scripts/usage/context_economy_read.py` — new.
- `scripts/usage/test_context_economy_read.py` — new.
- `scripts/usage/fixtures/context_economy/` — new fixture transcripts: a precomputed compaction, a normal compaction, a startup, a resume with two hooks, a compact start, a duplicated request.

**Workers:** 1 writer + 1 tester; one new module, the tester writes from the Spec.
- `impl` — `scripts/usage/context_economy_read.py`, `config/context_economy.conf`.
- `test` — `scripts/usage/test_context_economy_read.py`, the fixtures; the git timeline test builds a temporary repository with three commits: one changes `precomputeCompactionEnabled`, one changes nothing tracked, one changes `commands/unit/direct.md`.

**State:** none.

**Constraints from prior phases:**

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_context_economy_read` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- The temporary repository gives three settings sets: the one in force at `since` and the two tracked changes.

### Phase 2 — The report, saved results and comparisons  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** `context_economy.py report|save|compare|trend` prints the context cost of each settings set and role, saves a named result, and compares later data against it with sample sizes and confounds.

**Spec:**
1. Quantities per group (settings set × role × model), each with n, median, mean, p25 and p75:
   - `fresh_start` — `context` of the first request after a `startup` start;
   - `after_compaction` — `context` of the first request after a compaction;
   - `at_compaction` — `pre_tokens`;
   - `context_per_request` — `context`;
   - `cost_per_request` — `weights.weighted` of the request;
   - `requests_per_cycle` — requests between two compactions of one agent;
   - `compactions_per_active_hour` — compactions over active hours; active time sums request gaps under `active_gap_seconds`;
   - `precomputed_share` — `precomputed` compactions over `precomputed` + `normal`. It is the control: a set with `precomputeCompactionEnabled=false` must read 0%.
2. A session counts in a settings set only from its start under that set (natedev, 2026-11-08: "split by session, not by clock"). Sessions under `min_requests` are left out and counted on a `left out:` line.
3. `report [--since] [--until] [--role R] [--by settings|week]` prints one Markdown table per quantity. `--since` defaults to 7 days ago. Times are read and printed in the user's zone (`scripts/lib/user_zone.py`). `--by week` prints one row per ISO week and marks the week where a settings set, a Claude Code version or a model first appears.
4. `save <name> [--since] [--until] [--settings <label>]` writes `<state_dir()>/context_economy/<name>.json`: the settings set values, the window, the roles and models with request counts, the Claude Code versions seen, and each quantity's n, median, mean, p25 and p75 per role. `state_dir()` is the ledger's (`USAGE_STATE_DIR` when set). It refuses an existing name.
5. `compare <name> [--since] [--until]` prints the saved result against the window, per quantity and role: both values, the change in percent, both n, and a `confounds:` line naming each tracked setting, Claude Code version and model that differs between the two sides. A side with n under 10 prints `too few` in place of the change.
6. `trend [--role R]` prints one row per saved result in time order, so the record stays when old transcripts are gone.

**Files:**
- `scripts/usage/context_economy.py` — new: quantities, `report`, `save`, `compare`, `trend`, CLI.
- `scripts/usage/test_context_economy.py` — new: each quantity on fixture sessions; a compare with a confound; `too few`; `save` refuses an existing name.

**Workers:** 1 writer + 1 tester; one module, the tester writes from the Spec.
- `impl` — `scripts/usage/context_economy.py`.
- `test` — `scripts/usage/test_context_economy.py`.

**State:** `save` writes one result file per name and never overwrites one.

**Constraints from prior phases:** Phase 1 gives `context_economy_read.py`: `Request`, `CompactionEvent`, `Start`, `Session`, `read_claude`, `SettingsSet`, `SettingsUnknown`, `settings_timeline`, `settings_for`, `label`, `role_of`; `config/context_economy.conf` keys `settings_keys`, `files`, `min_requests`, `active_gap_seconds`.

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_context_economy` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- On natedev, `report --by settings --since 2026-11-08` prints the precompute ON and OFF sets, with 0% `precomputed_share` on the OFF set.

### Phase 3 — Mac sessions and Codex workers  · status: todo
**Depends on:** Phase 2

#### Work Order

**Goal:** the report covers Claude sessions on the Mac and Codex worker threads on both machines.

**Spec:**
1. Mac: `read_mac_sessions(since, until) -> list[Session]` sends the source of `context_economy_read.py` to the Mac over `ssh` with the options and bootstrap form of `collect.collect_mac`, and calls `read_claude` there. The Mac prints one JSON line per session; natedev decodes each one strictly. A failed read prints `mac: <reason>` on the report and the natedev numbers still print. Nothing is written on the Mac.
2. Settings on the Mac come from the Mac checkout's git log through the same ssh call, with `settings_timeline`.
3. Codex: `read_codex(sessions_roots, since, until, machine) -> list[Session]` with `rollouts.py`. Requests come from `token_usage` (Codex `cached_input_tokens` is `cache_read`, the rest of input is `input`, no cache writes). Compactions come from `is_compaction` with `kind = 'unknown'` and no sizes. Starts are the thread openings. Roots are `~/.codex*/sessions`.
4. The report gets `--family claude|codex|all` (default `all`) and a `machine` column.

**Files:**
- `scripts/usage/context_economy_read.py` — `read_mac_sessions`, `read_codex`.
- `scripts/usage/context_economy.py` — `--family`, the `machine` column, the `mac:` line.
- `scripts/usage/test_context_economy_read.py` — the Codex reader on fixture rollouts; the Mac JSON decode.

**Workers:** 2 writers; split by file.
- `impl` — `scripts/usage/context_economy_read.py`; hub: `scripts/usage/context_economy_read.py` (the `Session` type).
- `test` — opens as impl: `scripts/usage/context_economy.py`, `scripts/usage/test_context_economy_read.py`.

**State:** none.

**Constraints from prior phases:** Phase 1: the reader types and `read_claude`. Phase 2: `context_economy.py` quantities and commands; saved results under `<state_dir()>/context_economy/`.

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_context_economy_read scripts.usage.test_context_economy` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- On natedev, `report --family all` prints natedev and Mac rows, or the `mac:` line when the Mac is asleep.

### Phase 4 — The command, the as-built and the first saved results  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** `/context_economy` runs the report, the as-built describes it, and the precompute ON and OFF results are saved.

**Spec:**
1. New `commands/context_economy.md`: `/context_economy [report|compare <name>|save <name>|trend] [args]` runs `~/.claude/scripts/lib/py ~/.claude/scripts/usage/context_economy.py ...` and prints its output as it comes. Keep it under 30 lines (`/succinct_style`).
2. `docs/as-built/usage-ledger.md` gets a `### Context economy` section: the run-time inputs, the quantities, saved results, how a comparison names confounds, and the names of the saved results.
3. Save `precompute-on-2026-11` from the 11-08 sessions that ran with it on, and `precompute-off-2026-11` from the first day with at least 30 unit director cycles under `precomputeCompactionEnabled=false`.
4. Transcripts can be removed by Claude Code (`cleanupPeriodDays`), so the trend lives in saved results. A weekly job saves `week-<ISO year>-W<week>`: a `nate.jobs.context-economy` entry in `/etc/nixos` (`intervalSeconds = 604800`). The director commits it on a nixos branch and sends the branch and hash to natedev, which pushes it.

**Files:**
- `commands/context_economy.md` — new.
- `docs/as-built/usage-ledger.md` — the new section.

**Workers:** 1 writer; doc work does not split.
- `impl` — both files.
- `test` — opens as impl: checks the as-built against `scripts/usage/context_economy.py` and `scripts/usage/context_economy_read.py`.

**State:** two saved result files under `<state_dir()>/context_economy/`.

**Constraints from prior phases:** Phases 1-3 as built: `context_economy_read.py` readers for Claude, Mac and Codex; `context_economy.py report|save|compare|trend`.

**Acceptance gate:**
- `/context_economy compare precompute-on-2026-11` prints every quantity with both n and a `confounds:` line.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
