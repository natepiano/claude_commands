# Context economy: track context cost over time

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** The usage ledger records each compaction's kind and size and each session's settings, and one command reports and compares the context cost of each settings set.

> **As-built disposition: amend** — `docs/as-built/usage-ledger.md`

## Delegation Context

- **Project:** `~/.claude` tooling (this repo). The usage ledger (`scripts/usage/`) keeps every model request in `<state_dir()>/usage.db`. This plan adds the context economy: what each request carries, when sessions compact, and which settings each session ran with.
- **Stack:** Python 3, standard library only (`sqlite3`, `json`, `argparse`, `dataclasses`, `subprocess` for `git`). basedpyright strict.
- **Layout:** `scripts/usage/` (collector, store, report, new `context_economy.py`); `config/context_economy.conf` (new); `docs/as-built/usage-ledger.md`; `commands/context_economy.md` (new).
- **Key files:**
  - `scripts/usage/collect.py` — the collector. `Compaction` dataclass (line ~188); `claude_measurement` reads `compact_boundary` records (line ~386); the schema `compactions` table (line ~430); `ADDED_TURN_COLUMNS` and `migrate_store` (lines ~524-552); `STORE_VERSION = REQUEST_ACTIVITY_VERSION` (10, line ~83); insert at line ~648; Mac wire encode `measurement_record` (line ~1170) and decode `remote_measurement` (line ~1332). Transcripts root `Path.home() / ".claude/projects"` (line ~1633); `~/.claude1..4/projects` are links to it.
  - `scripts/usage/usage_report.py` — metric registry, `open_read_store`, `check_store_version`, `sources(db, zone)`, the zone handling of `--since`/`--until`.
  - `scripts/usage/weights.py` — `sql_weights(model, at)`; the weighted token cost of a request.
  - `scripts/usage/test_collect.py`, `scripts/usage/test_collect_mac.py`, `scripts/usage/test_usage_report.py` — the test patterns (temporary store from `USAGE_STATE_DIR`, fixture transcripts under `scripts/usage/fixtures/`).
  - `docs/as-built/usage-ledger.md` — the as-built this plan amends.
- **Test lanes:** `scripts/usage/test_*.py`; run with `python3 -m unittest` from the repo root.
- **Build:** none.
- **Test:** `python3 -m unittest scripts.usage.<module>` for each test module the phase names; then `python3 ~/.claude/scripts/production/targeted_test.py` before the checkpoint.
- **Lint:** `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- **Invariants:**
  - Store only what a script cannot look up later (user rule 2026-10-08).
  - Tests never read or write the real store, `~/.claude`, the live `settings.json`, real transcripts or the live checkout's reflog. Every path comes from `USAGE_STATE_DIR` or a test argument.
  - Estimated numbers and measured numbers never share a column. The report marks each estimate.
  - Every comparison prints the sample sizes and the other settings that changed in the same window (the user, 2026-11-08: "establish a new baseline").
  - The report runs on demand only. It sends no message (memory `build-report-on-demand`).
  - The tracked settings keys and files come from `config/context_economy.conf`, never from code (memory `no-hard-coded-config`).
  - Add no guard, refusal or check the user did not ask for.

## Phases

### Phase 1 — Store each compaction's kind and size, and each session start  · status: todo
**Depends on:** none

#### Work Order

**Goal:** the store holds, for every Claude compaction, its trigger, its size before and after, and whether it was precomputed; and it holds every Claude session start and resume with the Claude Code version.

**Spec:**
1. Store version 11, `COMPACTION_DETAIL_VERSION = 11`; `STORE_VERSION` becomes it.
2. `compactions` gets four columns, added by `migrate_store` the same way as `ADDED_TURN_COLUMNS`:
   - `trigger TEXT` (`auto`, `manual`, or NULL when the record has none);
   - `pre_tokens INTEGER`, `post_tokens INTEGER` (NULL when absent);
   - `kind TEXT NOT NULL DEFAULT 'unknown'`: `precomputed` when `compactMetadata.precomputed` is true, `normal` when the record has `compactMetadata` and no true `precomputed`, `unknown` for a row stored before version 11 and for every Codex compaction.
3. The source is the `system` record with `subtype == "compact_boundary"`, field `compactMetadata` (`trigger`, `preTokens`, `postTokens`, `precomputed`). Example record: `{"subtype":"compact_boundary","compactMetadata":{"trigger":"auto","preTokens":255292,"postTokens":15042,...}}`.
4. `Compaction` gets `trigger: str | None`, `pre_tokens: int | None`, `post_tokens: int | None`, `kind: CompactionKind` (a `Literal['precomputed','normal','unknown']`).
5. New table `starts(machine, family, parent_id, agent_id, at, source, cli_version)`, key all identity columns plus `at`. One row per Claude `attachment` record with `attachment.type == "hook_success"` and `attachment.hookEvent == "SessionStart"` and `attachment.hookName` in `SessionStart:startup` or `SessionStart:resume`. `source` is the part after the colon. `cli_version` is the record's top-level `version` (NULL when absent). Several hooks write one record each for one start; they share `toolUseID`, so the row key takes the first record's time per `toolUseID`. A `SessionStart:compact` or `SessionStart:clear` record is not a start.
6. A new `Start` measurement kind joins `ThreadMeasurement`, with `measurement_record` encode and `remote_measurement` decode, so the Mac half sends starts and compaction detail with no change to the bootstrap.
7. The migration deletes `cursors`, as version 10 did, so every source is read again and every old compaction gets its detail. A compaction row stored before keeps its key; the re-read fills the new columns with `INSERT ... ON CONFLICT DO UPDATE`.

**Files:**
- `scripts/usage/collect.py` — version 11, migration, `Compaction` fields, `Start`, `starts` table, Claude reader, Mac encode and decode.
- `scripts/usage/test_collect.py` — compaction detail, start rows, migration from version 10.
- `scripts/usage/test_collect_mac.py` — the wire round trip of `Start` and the new compaction fields.
- `scripts/usage/fixtures/` — one fixture transcript with a precomputed compaction, a normal compaction, a startup, a resume and a compact start.

**Workers:** 1 writer + 1 tester; the writer holds the collector, the tester writes from the Spec.
- `impl` — `scripts/usage/collect.py`.
- `test` — `scripts/usage/test_collect.py`, `scripts/usage/test_collect_mac.py`, the fixture under `scripts/usage/fixtures/`.

**State:** store version 11 migration; it deletes `cursors` once.

**Constraints from prior phases:**

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_collect scripts.usage.test_collect_mac` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- A version 10 test store migrates to 11 and each re-read compaction has its kind.

### Phase 2 — Settings sets: which tracked settings each session ran with  · status: todo
**Depends on:** Phase 1

#### Work Order

**Goal:** the store holds each change of the tracked settings in the live `~/.claude` checkout, and a reader gives the settings set that a session start ran with.

**Spec:**
1. New file `config/context_economy.conf` (the `scripts/lib` conf reader the other `config/*.conf` files use):
   ```
   # settings.json keys to track; one per line
   settings_keys = autoCompactWindow precomputeCompactionEnabled model
   # repo files to track by size and sha; one per line
   files = commands/unit/direct.md docs/direct/write_prompt_contract.md
   # sessions with fewer requests than this are left out of comparisons
   min_requests = 20
   # a request gap longer than this ends an active stretch (seconds)
   active_gap_seconds = 600
   ```
2. New table `settings_changes(machine, at, commit_id, settings TEXT)`, key (`machine`, `at`). `settings` is canonical JSON: each tracked key's value (`null` when absent) and, for each tracked file, `{"bytes": N, "sha": "<12 hex>"}`.
3. The collector reads the live checkout's reflog on natedev: `git -C <checkout> reflog --date=iso-strict --format=%H%x00%gd main`. For each reflog entry not yet stored, it reads `settings.json` and the tracked files at that commit with `git show <commit>:<path>` and stores a row only when `settings` differs from the previous row. The reflog expires; the store keeps the rows. The checkout path comes from `USAGE_CLAUDE_CHECKOUT` when set, else `Path.home() / ".claude"`. The Mac sends its own reflog rows through the remote channel as a `settings_change` record.
4. Source family `settings` on both machines in `turn_sources`, with the usual `SourceState`.
5. New module `scripts/usage/context_economy.py`, first part: `settings_at(db, machine, at) -> SettingsSet | SettingsUnknown`, the latest `settings_changes` row at or before `at`. `session_settings(db)` gives each `starts` row with its settings set. A request belongs to the settings set of the latest start of its agent at or before it; a request with no earlier start uses the agent's `openings` time.
6. A settings set has a short label: the changed values against the previous set, for example `precomputeCompactionEnabled=false`. The full set is in the JSON.

**Files:**
- `config/context_economy.conf` — new.
- `scripts/usage/collect.py` — `settings_changes` table, reflog reader, Mac record.
- `scripts/usage/context_economy.py` — new; `settings_at`, `session_settings`, `SettingsSet`, `SettingsUnknown`.
- `scripts/usage/test_context_economy.py` — new.
- `scripts/usage/test_collect.py` — the reflog reader against a temporary git repository.

**Workers:** 2 writers; split by file.
- `impl` — `scripts/usage/collect.py`, `config/context_economy.conf`; hub: `scripts/usage/collect.py` (the store schema).
- `test` — opens as impl: `scripts/usage/context_economy.py`, `scripts/usage/test_context_economy.py`; it asks `impl` for schema lines.

**State:** new store rows; no migration beyond the new table.

**Constraints from prior phases:** Phase 1 gives `starts(machine, family, parent_id, agent_id, at, source, cli_version)` and `compactions.kind`, `pre_tokens`, `post_tokens`, `trigger`; store version 11.

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_collect scripts.usage.test_context_economy` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- A temporary repository with three commits that change `precomputeCompactionEnabled`, then nothing tracked, then `direct.md`, gives two `settings_changes` rows after the first.

### Phase 3 — The context economy report, baselines and comparisons  · status: todo
**Depends on:** Phase 2

#### Work Order

**Goal:** `context_economy.py report|save|compare` prints the context cost of each settings set and role, saves a named baseline, and compares later data against it with sample sizes and confounds.

**Spec:**
1. Quantities, per group (settings set × role × model); each gives n, median and mean:
   - `fresh_start` — context of the first request after a `startup` start (input + cache read + both cache writes);
   - `after_compaction` — context of the first request after a compaction;
   - `at_compaction` — `pre_tokens`;
   - `context_per_request` — input + cache read + both cache writes;
   - `cost_per_request` — weighted tokens from `weights.sql_weights`;
   - `requests_per_cycle` — requests between two compactions of one agent;
   - `compactions_per_active_hour` — compactions over active hours; active time sums request gaps under `active_gap_seconds`;
   - `precomputed_share` — `kind = 'precomputed'` compactions over known-kind compactions. It is the control: an OFF set must read 0%.
2. A session counts in a settings set only from its start under that set (natedev, 2026-11-08: "split by session, not by clock"). Sessions under `min_requests` are left out and counted on a `left out:` line.
3. `report [--since] [--until] [--role] [--by settings|week]` prints one Markdown table per quantity. `--by week` prints one row per week and marks the weeks where a settings set, a `cli_version` or a model first appears.
4. `save <name> [--since] [--until] [--settings <label>]` writes `<state_dir()>/baselines/<name>.json`: the settings set JSON, the window, the role and model mix, the Claude Code versions seen, and each quantity's n, median, mean, p25 and p75. It refuses an existing name.
5. `compare <name> [--since] [--until]` prints baseline vs now for each quantity: both values, the change in percent, both n, and a `confounds:` list: each tracked setting, `cli_version` and model that differs between the two sides. A side with n under 10 prints `too few` in place of the change.
6. Roles come from the ledger's `role` column; the unit director role is the ledger's unit director role.
7. The report reads the store read-only, as `usage_report.py` does, and checks the store version.

**Files:**
- `scripts/usage/context_economy.py` — quantities, `report`, `save`, `compare`, CLI.
- `scripts/usage/test_context_economy.py` — each quantity on a small fixture store; a compare with a confound; `too few`.

**Workers:** 1 writer + 1 tester; one module, the tester writes from the Spec.
- `impl` — `scripts/usage/context_economy.py`.
- `test` — `scripts/usage/test_context_economy.py`.

**State:** `save` writes one baseline file per name and never overwrites one.

**Constraints from prior phases:** Phase 1: `starts`, compaction detail. Phase 2: `settings_changes`, `settings_at`, `session_settings`, `config/context_economy.conf` keys `settings_keys`, `files`, `min_requests`, `active_gap_seconds`.

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_context_economy` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- On the real store, `report --by settings` prints the precompute ON and OFF sets with a 0% `precomputed_share` on the OFF set.

### Phase 4 — The command, the as-built and the first baseline  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** `/context_economy` runs the report and comparison, the as-built describes it, and the precompute-off baseline is saved.

**Spec:**
1. New `commands/context_economy.md`: `/context_economy [report|compare <name>|save <name>] [args]` runs `~/.claude/scripts/lib/py ~/.claude/scripts/usage/context_economy.py ...` and prints its output as it comes. Keep it under 30 lines (`/succinct_style`).
2. `docs/as-built/usage-ledger.md` gets a `### Context economy` section: the version 11 columns, `starts`, `settings_changes`, the quantities, baselines and how a comparison names confounds.
3. Save `precompute-off-2026-11` from the first day with at least 30 unit director cycles under `precomputeCompactionEnabled=false`, and `precompute-on-2026-11` from 11-08 sessions that ran with it on. Record both names in the as-built.

**Files:**
- `commands/context_economy.md` — new.
- `docs/as-built/usage-ledger.md` — the new section.

**Workers:** 1 writer; doc work does not split.
- `impl` — both files.
- `test` — opens as impl: reviews the as-built against `scripts/usage/context_economy.py`.

**State:** two baseline files under `<state_dir()>/baselines/`.

**Constraints from prior phases:** Phases 1-3 as built: store version 11, `starts`, compaction `kind`, `settings_changes`, `context_economy.py report|save|compare`.

**Acceptance gate:**
- `/context_economy compare precompute-on-2026-11` prints every quantity with both n and a `confounds:` line.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
