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
  - The command runs on demand. The weekly save job (Phase 4) is its one scheduled caller. It sends no message (memory `build-report-on-demand`).
  - The tracked settings keys and files come from `config/context_economy.conf`, never from code (memory `no-hard-coded-config`).
  - Add no guard, refusal or check the user did not ask for.

## Phases

### Phase 1 — Read sessions and settings at run time  · status: done
**Depends on:** none

#### As-built

`scripts/usage/context_economy_read.py` reads one time window at run time and writes nothing.
- `read_claude(projects_root, since, until, machine) -> list[Session]` reads `*/*.jsonl` and `*/*/subagents/*.jsonl` with modification time at or after `since`, and keeps records with `since <= timestamp < until`. Requests are deduplicated by (`message.id`, `requestId`); the last record gives the usage. It returns only sessions with requests, compactions or starts in the window.
- `Request(at, model, cli_version, input, cache_read, cache_write_5m, cache_write_1h, output)`; `context` is the sum of input and the three cache fields. Records with model `<synthetic>` are not requests: they have zero usage and no `requestId`.
- `CompactionEvent(at, trigger, pre_tokens, post_tokens, kind)`; `kind` is `precomputed`, `normal` or `unknown`.
- `Start(at, source, cli_version)`: one per `toolUseID` at its first hook record over the whole file. A change of the record `version` with no hook record adds a `resume` start: a restart does not always write a hook record. `starts` holds the starts inside the window and the latest start before `since`. `startup` and `resume` only.
- `Session(machine, family, session_id, agent_id, requests, compactions, starts, first_record_at)`; `first_record_at: datetime | SessionBeginningUnknown` is the first record of the file, also before `since`.
- The reader parses only the lines that can hold a request, a compaction, a start, a version change or the first timestamp.
- `configured_checkout()` gives `CONTEXT_ECONOMY_CHECKOUT`, else `~/.claude`. `settings_timeline(checkout, since, until) -> list[SettingsSet]` reads the checkout's HEAD reflog (`git log -g HEAD`), keeps an entry only when the tracked values change, and adds the set in force at `since`. The reflog time is when the live files changed; `main` moves by fast-forward, so commit times are wrong. Blobs come from two `git cat-file` batch calls. A tracked file is `{"bytes", "sha": sha256[:12]}`.
- `settings_for(at, timeline) -> SettingsSet | SettingsUnknown` gives the latest set at or before `at`; on equal times the later timeline entry wins. `label(current, previous)` gives `key=<json>` for each changed value, joined by `, `.
- `role_of(store, machine, family, session_id, agent_id) -> str` opens the ledger read-only, maps the identity through `collect.identity_columns`, and gives `unknown` when no row matches or the store is missing.

**Files:**
- `scripts/usage/context_economy_read.py` — the reader types and functions.
- `config/context_economy.conf` — `settings_keys`, `files`, `min_requests = 20`, `active_gap_seconds = 600`.
- `scripts/usage/test_context_economy_read.py` — reader tests; fixtures in `scripts/usage/fixtures/context_economy/`.

**Binds later work:** no reader function assigns settings to a request; the report applies the latest-start rule with `first_record_at` as the fallback. The reader imports `scripts.lib.conf`, `scripts.usage.collect`, `scripts.usage.rollouts` and `scripts.usage.usage_report`, so the Mac read must carry them.

**Gotchas:**
- The ledger stores a main Claude session with `parent_id = ''`.
- `~/.claude/scripts/lib/py -m` imports the main checkout; test a worktree with `python3 -m unittest` or the worktree's `scripts/lib/py`.

### Phase 2 — The report, saved results and comparisons  · status: done
**Depends on:** Phase 1

#### As-built

`scripts/usage/context_economy.py` gives `report`, `save`, `compare` and `trend`; it reads transcripts and git at run time and writes only saved results.
- Quantities per group, each with n, median, mean, p25 and p75: `fresh_start`, `after_compaction`, `at_compaction`, `context_per_request`, `cost_per_request`, `requests_per_cycle`, `compactions_per_active_hour` and `precomputed_share`. A set with `precomputeCompactionEnabled=false` reads 0% `precomputed_share`.
- `build_report(sessions, timeline, store, since, until, by, role, settings_label)`. `min_requests` applies once to each whole session in the window; the report counts the sessions left out on a `left out:` line. `_settings_at` gives each request and measurement the settings of its session's latest start at or before it, else `settings_for(first_record_at)`. Rate quantities are keyed by settings commit (`_rate_key` = commit, week, model).
- `main` asks the timeline from `_timeline_start(sessions, since)`, the earliest anchor, not from `since`.
- `GroupResult(settings_label, settings_values, settings_commit, week, role, model, requests, cli_versions, quantities)`.
- `report [--since] [--until] [--role] [--by settings|week]` prints one Markdown table per quantity in the user's zone; `--by week` marks the first week of a settings set, Claude Code version or model.
- `save <name>` writes `<state_dir()>/context_economy/<name>.json` with a temp file, then `os.link`; it refuses an existing name and keeps its bytes. `compare <name>` prints both values, the change, both n and a `confounds:` line; a side under n 10 prints `too few`. `trend` reads `*.json` in time order.
- `--settings <label>` matches one exact settings label.

**Files:**
- `scripts/usage/context_economy.py` — the quantities, the four commands and the CLI.
- `scripts/usage/test_context_economy.py` — report, save, compare and trend tests on fixture sessions.

**Binds later work:** a settings label is a diff against the previous timeline entry, so one setting can split into many labels; selection by a settings value needs `--where`. The saved-result format has no saved files yet and can change.

**Gotchas:**
- The natedev report takes 65-90 s for 66 sessions; the reader dominates.
- A Claude Code restart does not always write a `SessionStart:resume` hook record; the `version` change is the only trace.
- `~/.claude` main moves by fast-forward, so commit times are late; only the HEAD reflog gives the time the live files changed.

**Ruled out:** settings segments per start, each with its own `min_requests` (a session counts once, as a whole).

### Phase 3 — Mac sessions and Codex workers  · status: done
**Depends on:** Phase 2

#### Work Order

**Goal:** the report covers Claude sessions on the Mac and Codex worker threads on both machines.

**Spec:**
1. Mac: `read_mac(since, until) -> MacRead` sends the source of `context_economy_read.py` to the Mac over `ssh` with the options and bootstrap form of `collect.collect_mac`. On the Mac it calls `read_claude`, `read_codex` and `settings_timeline` on the Mac checkout (its HEAD reflog). The Mac prints JSON lines; natedev decodes each one strictly. `MacRead` is `MacSessions(sessions, timeline)` or `MacReadFailed(reason)`. An empty `MacSessions` is a Mac with no sessions in the window, not a failure. Nothing is written on the Mac.
2. Each session takes its settings from its own machine's timeline. `build_report` takes `timelines: Mapping[str, Sequence[SettingsSet]]` keyed by machine.
3. Codex: `read_codex(sessions_roots, since, until, machine) -> list[Session]` with `rollouts.py`. Requests come from `token_usage` (Codex `cached_input_tokens` is `cache_read`, the rest of input is `input`, no cache writes). The model comes from the rollout's `turn_context` (as `collect.CodexContext` reads it); the Codex version from `session_meta`. Compactions come from `is_compaction` with `kind = 'unknown'` and no sizes. Starts are the thread openings, with the same rule as Claude: the starts inside the window and the latest start before `since`. Roots are `~/.codex*/sessions`. Numbered Codex homes link `sessions` to `~/.codex/sessions` (`docs/accounts.md` setup), so read each file once by its resolved path.
4. `CompactionEvent.pre_tokens` and `post_tokens` become `int | SizeNotReported`. `at_compaction` takes only reported sizes.
5. A request whose model `weights.weights_at` cannot weight (`UnweightedModel`) counts in the context quantities and not in `cost_per_request`. The report prints `cost not weighted: <n> requests (<models>)`.
6. Groups and saved results carry `machine` and `family`. The report gets `--family claude|codex|all` (default `all`) and a `machine` column. `compare` pools by role and quantity across machines, as now; its `confounds:` line also names a machine or family present on one side only.
7. `report` and `save` get `--where <key>=<json>` (repeatable), which keeps only measurements whose settings values match each key. `--where precomputeCompactionEnabled=false` selects every OFF settings set, whatever file changes split it. `--settings <label>` is removed.
8. A failed Mac read prints `mac: <reason>` on the report and the natedev numbers still print.

**Files:**
- `scripts/usage/context_economy_read.py` — `read_mac`, `MacRead`, `read_codex`, `SizeNotReported`.
- `scripts/usage/test_context_economy_read.py` — the Codex reader on fixture rollouts (model, version, starts, overlapping linked roots); the Mac JSON decode; a failed Mac read.
- `scripts/usage/context_economy.py` — timelines per machine, `--family`, `--where`, the `machine` column, the `mac:` and `cost not weighted:` lines.
- `scripts/usage/test_context_economy.py` — two machines with different timelines over the same commits; `--where`; an unweighted model; saved results with machine and family.

**Workers:** 2 writers; split by file. Agree the `MacRead`, `Session` and `CompactionEvent` contract on the board before the report edits use it.
- `impl` — `scripts/usage/context_economy_read.py`, `scripts/usage/test_context_economy_read.py`, fixtures; hub: `scripts/usage/context_economy_read.py` (the `Session`, `CompactionEvent` and `MacRead` types).
- `test` — opens as impl: `scripts/usage/context_economy.py`, `scripts/usage/test_context_economy.py`.

**State:** none.

**Constraints from prior phases:** Phase 1: the reader types and `read_claude`; `Session.family` already holds `codex`. The reader imports `scripts.lib.conf`, `scripts.usage.collect`, `scripts.usage.rollouts` and `scripts.usage.usage_report`; the Mac call must carry the modules `read_claude` needs, or split them out of the payload.
- Phase 2: `read_claude` skips `<synthetic>` records, adds a `resume` start at each Claude Code `version` change, and keeps the latest start before `since`. The reader parses only lines that can hold a request, a compaction, a start, a version change or the first timestamp. `settings_timeline` reads the checkout HEAD reflog and batched `git cat-file`.
- Phase 2: `build_report(sessions, timeline, store, since, until, by, role, settings_label)` applies `min_requests` to each whole session in the window. `_settings_at` gives each request and measurement the settings of the session's latest start at or before it, else `settings_for(first_record_at)`. Rate quantities are keyed by settings commit. `main` asks the timeline from `_timeline_start(sessions, since)`.
- Phase 2: `GroupResult(settings_label, settings_values, settings_commit, week, role, model, requests, cli_versions, quantities)`; `save_result` writes a temp file, then `os.link`; `trend` reads `*.json`. No result is saved yet, so the saved format can change.
- The natedev report takes 65-90 s (66 sessions). Keep the Mac and Codex reads to one pass each.

**Acceptance gate:**
- `python3 -m unittest scripts.usage.test_context_economy_read scripts.usage.test_context_economy` passes.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
- On natedev, `report --family all` prints natedev and Mac rows, or the `mac:` line when the Mac is asleep. Codex rows show a model, and no request is counted twice.
- On natedev, `report --where precomputeCompactionEnabled=false --since 2026-11-08` prints only OFF rows, each with 0.0% `precomputed_share`.

### Phase 4 — The command, the as-built and the first saved results  · status: todo
**Depends on:** Phase 3

#### Work Order

**Goal:** `/context_economy` runs the report, the as-built describes it, the precompute ON and OFF results are saved, and a weekly job saves the trend.

**Spec:**
1. New `commands/context_economy.md`: `/context_economy [report|compare <name>|save <name>|trend] [args]` runs `~/.claude/scripts/lib/py ~/.claude/scripts/usage/context_economy.py ...` and prints its output as it comes. Keep it under 30 lines (`/succinct_style`).
2. `docs/as-built/usage-ledger.md` gets a `### Context economy` section: the run-time inputs, the quantities, `--where`, saved results and their names, how a comparison names confounds, the `left out:` line (it counts whole sessions under `min_requests`), and the measured run time.
3. Save `precompute-on-2026-11` with `--where precomputeCompactionEnabled=true` over the 11-08 sessions that ran with it on.
4. Save `precompute-off-2026-11` with `--where precomputeCompactionEnabled=false` from the first day where `requests_per_cycle` for role `director` has n of 30 or more. Until that day, report the n and save nothing.
5. Transcripts can be removed by Claude Code (`cleanupPeriodDays`), so the trend lives in saved results. A weekly job runs `save week-<ISO year>-W<week>`: a `nate.jobs.context-economy` entry in `/etc/nixos` (`intervalSeconds = 604800`). The job owner is natedev's user; it writes only `week-*` files under `<state_dir()>/context_economy/` and sends no message. The director commits it on a nixos branch and sends the branch and hash to natedev, which pushes it.

**Files:**
- `commands/context_economy.md` — new.
- `docs/as-built/usage-ledger.md` — the new section.

**Workers:** 1 writer; doc work does not split.
- `impl` — both repository files and the nixos entry.
- `test` — opens as impl: checks the as-built against `scripts/usage/context_economy.py` and `scripts/usage/context_economy_read.py`.

**State:** the saved result files under `<state_dir()>/context_economy/`: `precompute-on-2026-11`, `precompute-off-2026-11` (when n reaches 30), and one `week-*` file each week.

**Constraints from prior phases:** Phases 1-3 as built: `context_economy_read.py` readers for Claude, Mac and Codex; `context_economy.py report|save|compare|trend` with `--where`, `--family` and the `machine` column. `save` refuses an existing name.
- The invariant "the command runs on demand only" reads: the command runs on demand; the weekly save job is its one scheduled caller and sends no message.

**Acceptance gate:**
- `/context_economy compare precompute-on-2026-11` prints every quantity with both n and a `confounds:` line.
- `/context_economy save precompute-on-2026-11` a second time refuses and leaves the file unchanged.
- `/etc/nixos` evaluates with the new job (`nix flake check` or the repo's own check), and the job's command runs once by hand and writes one `week-*` file.
- `basedpyright scripts/usage` reports 0 errors and 0 warnings.
