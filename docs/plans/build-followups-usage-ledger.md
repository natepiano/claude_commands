# Usage ledger: token use by role and model, kept over time

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Start confirmed by the user 2026-10-11 10:21 PDT, via natedev. One durable store, on natedev, of every model turn across Claude and Codex on both machines. Each turn records who it belonged to: showrunner, unit director, worker (impl, test, fix), reviewer (by lens), subagent or interactive session. It also records which model and effort produced it. Metrics are queries over that store, so a new one is a few lines and not a new script.

> **As-built disposition: create**

> **Production: build-followups** — unit `model-study-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` tooling (this repo) — a durable store of model usage by role and model.
- **Project started:** 2026-10-11T17:25:57.890+00:00
- **Stack:** Python 3 (stdlib only: `json`, `fcntl`, `sqlite3`, `argparse`, `dataclasses`), zsh/bash launchers.
- **Layout:** `scripts/usage/` (new) for the ledger; role-assignment points in `scripts/delegate/`, `scripts/agents/`, `scripts/production/`.
- **Key files:**
  - `scripts/delegate/prepare_session.sh` — starts a unit director's run; knows `CLAUDE_CODE_SESSION_ID` (line ~32).
  - `scripts/agents/agent_bg.sh` — launches Claude workers; appends `<session id>\t<name>` to the run's `seats` ledger.
  - `scripts/agents/codex_mesh.py` — opens and follows Codex seats; writes `role` on a new thread (~line 1895) and on a follow-up claim (~line 721).
  - `scripts/delegate/review.sh` — reviews; knows the lens; dispatches through `codex_mesh.py` with `--role review` (~line 278).
  - `scripts/production/update_registration.py` — `register` runs at every showrunner start and resume.
  - `scripts/whoami/seat_usage.py` — today's Codex rollout parser (reused in Phase 4).
- **Test lanes:** `scripts/usage/test_*.py`, `scripts/agents/test_codex_mesh.py`; run with `python3 -m unittest` from the repo root (pytest is not installed).
- **Build:** none.
- **Test:** `python3 -m unittest scripts.usage.test_agents_log scripts.agents.test_codex_mesh` (per phase, the modules it names).
- **Lint:** `basedpyright scripts/usage scripts/agents/codex_mesh.py scripts/production/update_registration.py` reports 0 errors, 0 warnings, 0 notes (it exits 3 for the missing `.venv`; read the counts). `bash -n` on each edited shell script.
- **Invariants:**
  - Store only what a script cannot look up later (user rule 2026-10-08).
  - Recording usage never breaks the work it records: a failed append prints one stderr line and the caller carries on.
  - Tests never write to the real `~/.local/state/usage-model/`: every path comes from `USAGE_STATE_DIR` when set.
  - No Pushover, no `send.py --to user` from any of this.

## Why

The user, via hana, 2026-10-11: "Tell model-study to make sure we can mine token efficiency overtime. The last model usage report was great. We need to be able to create our own metrics with the data we have access to because codex and Claude are opaque to some info. We need to make sure we can track model stats across all roles such as showrunners, reviewers, unit directors, workers and across al model types that we use"

Today, each question needs its own scratch script (`seat_usage.py`, `api_cost.py`, `startup2.py`, `daily_split.py`). Each one re-reads raw logs and knows only one model's weights. The role an agent held is mostly lost once its run ends. The unit director's marker in `/tmp/claude/delegate/active/` is deleted by `end_session.sh`. A Codex seat's role sits in a per-run `mesh_roster.json` that changes with every dispatch, and `/tmp` does not last. The raw token counts last (Claude transcripts in `~/.claude/projects`, Codex rollouts in `~/.codex/sessions`, on both machines). The link to role does not.

## What we have to work with

- **Claude turns:** every assistant reply in `~/.claude/projects/**/*.jsonl` (subagents under `subagents/`) carries `model` and `usage`: input, 5-minute and 1-hour cache writes, cache reads and output. Deduplicate by (message id, request id), as `api_cost.py` already does.
- **Codex turns:** each rollout in `~/.codex/sessions` carries `turn_context` (model, effort) and `token_count` events (input, cached input, output); `seat_usage.py` already parses them, including the compaction records and the first edit.
- **Roles, live only today:**
  - the showrunner (production doc, `SHOWRUNNER_UNIT` tmux marker);
  - unit directors (the active marker, and the run's session directory);
  - Claude workers (the `seats` ledger in the run directory);
  - Codex seats (`role` per dispatch in `mesh_roster.json`, the lens only in `agents.jsonl`, and the `handoff` posts on the board);
  - reviewers (`review.sh` status files);
  - subagents (the parent session that spawned them).
  58 finished run directories survive in `~/.local/state/usage-model/delegate-archive/`.
- **Outcomes:** checkpoint and shrink commits (git), phases (plan docs), findings opened and closed (`findings.py` ledgers), review rounds and passes (`progress_history.py` events), compactions, account meter readings (`~/.local/state/agent-notes/readings.jsonl`).
- **Prices:** published per model, with dates: gpt-5.6-sol, gpt-6.1-sol ($2 / $0.10 cached / $10 output per million; $4 / $15 above 272K input), and the Claude models in `api_cost.py`.

## Design decisions

- **One SQLite store** at `~/.local/state/usage-model/usage.db` on natedev. The Mac's logs are read over ssh, the way `seat_usage.py` reads them today. SQLite because metrics are queries over joins, and it ships with Python.
- **The role is recorded when it starts, by the code that assigns it**, not inferred later. An append-only `agents.jsonl` on each machine, with one line per assignment holding only what is lost once a run ends: agent id (Claude session id or Codex thread id), role, lens, and assignment time. Everything else is looked up from the agent id when it is read: the kind and the machine (from the source holding it — which family's log, on which machine; never from the id's form), the worktree from the transcript's `cwd`, and the unit and production from the production doc's Units table. This follows the user's rule of 2026-10-08, store only what a script cannot look up. A Codex seat that switches from tester to reviewer writes a second line, so each turn is matched to the role in force at its timestamp. The `usage.db` turn rows are measurements and are stored in full.
- **Weights are a dated table per model**, not constants. Usage is reported in each model's weighted tokens, and a window that crosses a model change splits there by itself. This replaces `SEAT_MODEL` and the fixed weights in `seat_usage.py` (the pending add-on "weight seat usage by each turn's model" is folded in here).
- **Accounts come from the existing modules**: `scripts/accounts/accounts.py` (accounts and the role-change history), `scripts/whoami/agent_accounts.py`, and the readings that `scripts/whoami/run_out.py` reads. All are already in this checkout. No second copy is built.
- **The Mac is read over ssh, and ssh's own exit status is always 0 there**, so each remote command prints `rc=$?` and the collector reads that.
- **No Pushover from the collector.** It fails quietly into its own log; the daily update reports staleness.
- **Incremental and idempotent.** The collector remembers each log file's size and offset, so a rerun adds only new turns and never doubles one.
- **Metrics live in one file**, each a named query with a one-line description. `usage_report.py <metric> [--by role,model,unit,day] [--since …]` prints a Markdown table. Adding a metric means adding one entry.

## Phases

### Phase 1 — Every agent the delegate tooling starts records its role · status: done

#### As-built

- `scripts/usage/agents_log.py` keeps `<state_dir>/agents.jsonl`, `state_dir()` being `USAGE_STATE_DIR` when set, else `~/.local/state/usage-model`. Each line is `{"id", "role", "at"}`, plus `"lens"` when known and `"approximate": true` for backfill. `Role` and `Lens` are `Literal` aliases checked at the CLI (unknown value exits 2).
- `append(agent_id, role, lens, at, approximate=False)` writes under an exclusive `fcntl.flock` and starts a new line after a truncated tail. `assignments(path) -> RoleLog(assignments, problems)` skips and counts any malformed line, deep nesting included, and never raises. `role_at(log, agent_id, at) -> Assignment | Unassigned` returns the latest assignment at or before `at`.
- CLI `append`, `backfill [--archive] [--live]`, `show [--id]`; a write failure prints one `agents_log:` stderr line and exits 0.
- Writers: `prepare_session.sh` records `director`, `update_registration.py register` records `showrunner`, `agent_bg.sh` records `worker`. `codex_mesh.py` records the seat's role and `--lens` on a new thread and on a follow-up claim. It records the previous role again when a claim is released. `review.sh` passes `--lens`. The lens lives only in `agents.jsonl`, never in `mesh_roster.json`.
- `backfill` reads each run directory's `mesh_roster.json` (`role`, and `previous_role` as an earlier line) and `seats` ledger (`worker`), stamped with the file's mtime and `approximate: true`. A roster and its ledger are read separately, so one bad file loses only its own rows. It skips any id that already has an exact line and never repeats an (id, role, lens) line. A review lens comes only from the newest `review_agent_[<N>_]<lens>.log` naming the seat; with no such log, no lens.

**Files:**
- `scripts/usage/agents_log.py`, `scripts/usage/__init__.py` — the log: writer, reader, backfill, CLI.
- `scripts/usage/test_agents_log.py`, `scripts/agents/test_codex_mesh.py`, `scripts/production/test_update_registration.py` — tests; the registration test copies `scripts/usage`.
- `scripts/delegate/prepare_session.sh`, `scripts/production/update_registration.py`, `scripts/agents/agent_bg.sh`, `scripts/agents/codex_mesh.py`, `scripts/delegate/review.sh` — the writers.

**Binds later work:** `assignments(Path) -> RoleLog` and `role_at` are the attribution API, with `approximate` marking estimated lines and a nullable lens. A Claude worker's id is the 8-hex prefix of its transcript uuid. Recording never fails its caller.

**Gotchas:** the delegate archive keeps no `mesh_roster.json` or `seats` ledger, so archived runs backfill nothing. Older review logs do not name the seat, so their lens stays unrecorded. A review outside a run (`agent_exec.sh`) records nothing.

**Ruled out:** a lens inferred from the seat name (any seat runs any lens); the lens in `mesh_roster.json`.

### Phase 2 — Every other Claude session can record itself · status: done

#### As-built

- `Role` includes `interactive` and `subagent` in the `Literal` alias, the CLI's choices and the reader's validation.
- `register_default(agent_id: str, role: Role, at: datetime) -> bool` reads the log and appends `{"id", "role", "at"}` in one step under the exclusive `fcntl.flock` that `append` takes, only when no line already names the agent. A line names it when its id equals `agent_id`, or when its id is 8 hex characters and a prefix of `agent_id` (an `agent_bg.sh` worker). It returns whether it wrote; concurrent deliveries write one line.
- `role_at(log, agent_id, at)` on a full id merges the history under `agent_id[:8]` when that is a recorded 8-hex id, ordered by `at`; a lookup by the prefix itself sees only the prefix's lines. So a worker reads `interactive` until `agent_bg.sh`'s `worker` line, then `worker`.
- The ledger is read as bytes, and each line is decoded inside its own error handler: an undecodable line is a counted `problems` entry and never blocks a registration.
- CLI `register --id <id> --role <role>`: usage errors exit 2, write failures print `agents_log: <reason>` and exit 0.
- `scripts/hooks/session-start-agents-log.py` reads the hook payload from stdin and registers a non-empty `agent_id` as `subagent`, otherwise `session_id` as `interactive`. Any failure prints one `agents_log:` stderr line and exits 0. It is event-agnostic, and imports `agents_log` from `scripts/usage` by path.

**Files:**
- `scripts/usage/agents_log.py` — roles, `register_default`, the prefix-merged `role_at`, the byte-wise reader, and the `register` CLI.
- `scripts/hooks/session-start-agents-log.py` — the hook.
- `scripts/usage/test_agents_log.py`, `scripts/usage/test_session_start_hook.py` — tests; the hook is driven as a subprocess.

**Binds later work:** the `role_at` prefix merge does not know the family or whether the prefix is unique, so the turn store validates a unique Claude worker binding before relying on it. A subagent's transcript is `<parent transcript stem>/subagents/agent-<agent_id>.jsonl` (`scripts/hooks/context_usage.py` `resolve_transcript`); the parent is derived from that path, not stored.

**Gotchas:** hook-first is the normal order for a delegate worker: the hook writes `interactive` under the full uuid before `agent_bg.sh` writes `worker` under the prefix. `register_default` reads the whole log at every session start. The type-check gate covers test files too.

**Ruled out:** an optional parent field on the agent identity; a session and a subagent are distinct identity variants.

### Phase 3 — Every other Claude session records itself · status: done

#### Work Order

**Goal:** every Claude session and every subagent records its role in `agents.jsonl` as it starts, by registering Phase 2's hook in `settings.json`.

**Spec:**
- `settings.json`, `hooks.SessionStart`: in the entry with no `matcher` (it fires for startup, resume, clear and compact), append one command hook, written like its neighbours: `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/session-start-agents-log.py"`. Do not touch the `compact` entry or any other hook.
- `settings.json`, `hooks.SubagentStart`: a new event key holding one entry with no `matcher` and the same command. Live check 2026-10-11 (Claude Code 2.1.296): `SubagentStart` delivers `agent_id` (form `a` + 16 hex, e.g. `a4008deac73c98365`) and `agent_type`; the hook wrote `subagent` at 21:01:02Z and the subagent's first assistant turn came at 21:01:03.685Z. `SessionStart` does not fire for a subagent. `SubagentStop` also carries `agent_id` but fires after the work, so it is not registered.
- No change to the hook or `agents_log.py`; the edit is JSON only, keys and formatting kept as the file has them (two-space indent).
- The showrunner's merge check refuses settings diffs: this checkpoint goes to main only after the user approves it in this session. The user approved it on 2026-10-11 at about 23:15 PDT, relayed by the showrunner.

**Files:**
- `settings.json` — two hook registrations.
- `scripts/usage/test_session_start_hook.py` — activation tests.

**Seats:** `1 writer + 1 tester`.
- `impl` — `settings.json`; hub: `settings.json`.
- `test` — activation tests in `scripts/usage/test_session_start_hook.py`, from this Spec alone: load the repo's `settings.json`; exactly one `SessionStart` command and exactly one `SubagentStart` command name `scripts/hooks/session-start-agents-log.py`, the first in the matcher-less entry; no other event names it; running each registered command line through `sh -c` with `HOME` set so `$HOME/.claude` resolves to the repo root (a temp dir holding a `.claude` symlink to the repo), `USAGE_STATE_DIR` at a temp dir and a fixture payload on stdin, from an unrelated working directory, writes one `interactive` line for a SessionStart payload and one `subagent` line for a SubagentStart payload; a second delivery writes none.

**Constraints from prior phases:** the hook registers a non-empty `agent_id` as `subagent`, else `session_id` as `interactive`, prints one `agents_log:` stderr line and exits 0 on any failure, and writes nothing for an id already named (exact, or by a recorded 8-hex worker prefix). Hook commands in `settings.json` run through `$HOME/.claude/scripts/lib/py`. Tests never write to the real `~/.local/state/usage-model/`.

**Acceptance gate:** `python3 -m unittest scripts.usage.test_agents_log scripts.usage.test_session_start_hook` green; basedpyright 0/0/0 on `scripts/usage scripts/hooks/session-start-agents-log.py`, test files included; `python3 -m json.tool settings.json` parses; the settings diff holds only the two registrations. Live, after the user approves and the merge reaches `~/.claude`: a new Claude session and a new subagent each appear in `agents.jsonl` within a second of starting, the subagent's line dated before its first turn.

### Phase 4 — One store of every model turn · status: todo

#### Work Order

**Goal:** `usage.db` holds every Claude and Codex model turn from both machines with its role, model, effort and token classes, and a rerun adds nothing.

**Spec:**
- CLI, the contract the tests drive: `python3 scripts/usage/collect.py [--db PATH] [--machine NAME] [--claude-root DIR] [--codex-root DIR] [--agents-log FILE] [--no-mac]`. Defaults: `--db` `<state_dir()>/usage.db`, `--machine` `natedev`, `--claude-root` `~/.claude/projects`, `--codex-root` `~/.codex/sessions`, `--agents-log` `<state_dir()>/agents.jsonl`. It prints one line per source (turns added, source state) and exits 0 unless the database cannot be written.
- Claude turns: files `<claude-root>/*/*.jsonl` (a session; agent id is the file stem) and `<claude-root>/*/<session stem>/subagents/agent-<agent_id>.jsonl` (a subagent; parent is the session stem). Every account's `projects` directory is a symlink to `~/.claude/projects`, so there is one root per machine. A turn is a line with `type == "assistant"` and a `message.usage`; its key is (`message.id`, `requestId`), which also deduplicates repeats. Time is `timestamp`, model `message.model`, effort the line's `effort` (absent means not reported). Token classes: `input_tokens`; `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` (any remainder of `cache_creation_input_tokens` counts as 5-minute); `cache_read_input_tokens`; `output_tokens`. A compaction is a `system` line with `subtype == "compact_boundary"`. The first edit is the first `tool_use` content block named `Edit`, `Write` or `NotebookEdit`.
- Codex turns: files `<codex-root>/*/*/*/*.jsonl`; the thread id is the `session_meta` payload `id`. Model and effort come from the latest `turn_context` payload (`model`, `effort`) before the turn. A turn is an `event_msg` `token_count` with `info.last_token_usage`; its key is the byte offset of that line. Import `decode_rollout`, `token_usage`, `is_edit`, `is_compaction` and `parse_timestamp` from `scripts/whoami/seat_usage.py`; do not copy them. Codex uncached input is stored as `input`, cached input as `cache_read`, both cache-write classes as 0.
- The Mac: the same two roots and its `agents.jsonl` are read over ssh the way `seat_usage.py` `mac_rollouts` does (`MAC_HOST`, `BatchMode=yes`, `ConnectTimeout=5`), each remote command printing `rc=$?` as its last line because ssh's own exit status is always 0 there. An unreachable Mac is a recorded source state and never fails the run. `--no-mac` skips it.
- Schema (SQLite, created when absent):
  - `turns(machine, family, kind, parent_id, agent_id, turn_key, at, model, effort, input, cache_write_5m, cache_write_1h, cache_read, output, before_first_edit, role, lens, attribution, lens_state)`, primary key (`machine`, `family`, `parent_id`, `agent_id`, `turn_key`). `family` is `claude` or `codex`; `kind` is `session`, `subagent` or `thread`; `parent_id` is the parent session for a subagent and `''` otherwise; `at` is UTC ISO-8601; `effort`, `role` and `lens` may be NULL; `attribution` is `recorded`, `estimated` or `unassigned`; `lens_state` is `known`, `unrecorded` or `not_applicable`.
  - `compactions(machine, family, parent_id, agent_id, at)`, primary key all five.
  - `cursors(machine, path, offset)`, primary key (`machine`, `path`).
  - `role_log_sources(machine PRIMARY KEY, problems, state, read_at)`.
- In Python the agent is a tagged identity, `ClaudeSession | ClaudeSubagent | CodexThread`, each carrying its machine; attribution and lens state are their own types. The NULL and `''` forms exist only in the SQL layer.
- `scripts/usage/weights.py` holds the dated table: `ModelWeights(model, effective_from, cached, cache_write_5m, cache_write_1h, output)` with input as 1, and `weighted(turn_row, at) -> float`. Entries: `gpt-5.6-sol` 0.1 cached, 8 output; `gpt-6.1-sol` from 2026-10-10T21:35Z 0.05 cached, 5 output; every Claude model 0.1 cache read, 1.25 five-minute write, 2 one-hour write, 5 output. A model with no entry is reported as unweighted, never weighted by a default. Weights are applied on read and never stored.
- `scripts/usage/collect.py` reads Claude transcripts and Codex rollouts on both machines into `usage.db`, one row per model turn: time, machine, agent id, model, effort, the token classes, and whether it was a compaction or came before the agent's first edit.
- Each turn is matched to the role in force at its time, through `agents_log.assignments` and `role_at`, keeping their malformed-line diagnostics. Identity comes from the source (family and machine), never the id's form: a Claude worker matches its transcript uuid by a unique 8-hex prefix (an ambiguous prefix stays unresolved), and `role_at` on the full uuid already merges the lines under the uuid and under its recorded prefix, because the session-start hook writes `interactive` under the full uuid before `agent_bg.sh` writes `worker` under the prefix. `role_at` merges any recorded 8-hex history without knowing family or uniqueness, so the collector calls it on a full uuid only after validating that the prefix binding is a unique Claude worker on that machine; otherwise it looks up the exact id alone. A subagent's turns are read from `<parent transcript stem>/subagents/agent-<agent_id>.jsonl`, looked up under the child's `agent_id`, and stored under an identity scoped by machine, family, parent and child — distinct session and subagent identity variants, never an optional parent field — so parent and child usage stay separate and are each counted once. A historical director comes from the run's `main_agent.session_id` in `progress_history.py` events, never from `cwd` alone. Archived Codex turns with no evidence stay unassigned.
- Attribution is an explicit state per turn: recorded, estimated (from an `approximate` line) or unassigned; a review lens is known, unrecorded or not applicable. Nullable values stay at the storage boundary.
- Owns the per-turn model and effort parser, a dated weight table per model (raw token classes kept apart from weighting), and the normalized compaction events Phase 5 consumes.
- Runs incrementally with transactional cursors, handling an incomplete tail; a change to `agents.jsonl` re-attributes the affected turns even when transcripts are unchanged.
- Keeps one diagnostic snapshot per `agents.jsonl` source (its `RoleLog.problems` count, malformed and undecodable lines alike), replaced on each run rather than added to, for Phase 6 to show.
- Done when a rerun adds nothing; a late assignment corrects an earlier unassigned turn without duplicating it; checked-in Claude and Codex fixtures give their expected raw totals; and mixed-model, changed-effort and weight-date-boundary tests pass (from review 2026-10-11; replaces the `api_cost.py` comparison, which is not in this checkout). Attribution fixtures cover a hook-first worker, two uuids sharing an 8-hex prefix (both stay unresolved on the prefix), a cross-family id collision, late prefix evidence correcting unchanged turns, and a parent with a subagent whose usage stays separate. A ledger fixture with malformed bytes keeps its problems count stable across reruns while its valid lines still attribute.
- Read, and not edited in this phase (this plan's choice, not a rule): `scripts/whoami/seat_usage.py`, `scripts/usage/agents_log.py`, `scripts/hooks/context_usage.py` (`resolve_transcript`) and `scripts/delegate/progress_history.py` (`main_agent`, written at run start).

**Files:**
- `scripts/usage/collect.py` — new: sources, parsers, identity, attribution, schema, cursors, CLI.
- `scripts/usage/weights.py` — new: the dated weight table.
- `scripts/usage/test_collect.py` — new: the tests.
- `scripts/usage/fixtures/collect/` — new: Claude, Codex and ledger fixtures.

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/usage/collect.py` and `scripts/usage/weights.py`; hub: `collect.py`.
- `test` — `scripts/usage/test_collect.py` and `scripts/usage/fixtures/collect/`, from this Spec alone: every test drives the CLI with `--db`, both roots, `--agents-log` and `--no-mac` pointed at a temp copy of the fixtures, then reads the tables with `sqlite3`.

**Constraints from prior phases:** `agents_log.assignments(Path) -> RoleLog(assignments, problems)` and `role_at(log, agent_id, at) -> Assignment | Unassigned` are the attribution API; `approximate` marks an estimated line and the lens may be absent. A Claude worker's recorded id is the 8-hex prefix of its transcript uuid, and the session-start hook writes `interactive` under the full uuid first. `role_at` merges any recorded 8-hex prefix history without knowing family or uniqueness. Phase 3's hook registration is not on main yet, so the real `agents.jsonl` holds no `interactive` or `subagent` lines; a turn with no line is `unassigned`, never an error. `state_dir()` is `USAGE_STATE_DIR` when set. Tests never write to the real `~/.local/state/usage-model/`. No Pushover and no `send.py --to user`.

**Acceptance gate:** `python3 -m unittest scripts.usage.test_collect scripts.usage.test_agents_log` green; basedpyright 0/0/0 on `scripts/usage`, test files included. Live, by the unit director: a run with `USAGE_STATE_DIR` and `--db` in a temp directory and `--no-mac`, over the real local logs, completes, and a second run adds no rows.

### Phase 5 — Outcomes in the same store · status: todo

- Collect checkpoints, phases, findings, review rounds and passes, and account readings, keyed by machine, run, phase and pass instance and source event, with an explicit checkpoint mapping, so usage can be divided by work done. Compactions come from Phase 4's events. Accounts come from `scripts/accounts/accounts.py` (accounts and role-change history), `scripts/whoami/agent_accounts.py`, and the readings `scripts/whoami/run_out.py` reads; all exist in this checkout, so nothing gates on another unit. A nullable quota or credit value from those modules becomes a semantic availability state (read, not reported, unreadable) before it enters a usage-owned type.
- Reruns are idempotent, and a fixture joining several findings or passes to one stretch of work does not multiply its usage.
- Done when "weighted usage per checkpoint, by role and model, per day" is one query.
- **Seats:** `1 writer + 1 tester`. `impl` — outcome collection in `scripts/usage/collect.py` (hub). `test` — new `scripts/usage/test_outcomes.py` and its fixtures. Gate: `python3 -m unittest scripts.usage.test_outcomes scripts.usage.test_collect`; basedpyright 0/0/0 on `scripts/usage`, test files included.

### Phase 6 — Metrics as named queries · status: todo

- `usage_report.py`, with an initial set:
  - weighted usage per day and per checkpoint;
  - the share of usage spent before the first edit;
  - cache-read share;
  - output share;
  - compactions per thread;
  - usage per finding closed;
  - usage per review round;
  - account switches;
  - all of them by role × model × unit.
- `seat_usage.py` becomes a thin view over the store, and its tests keep passing.
- Every report shows attribution coverage, the estimated-turn share, unassigned usage, unrecorded review lenses and each assignment log's skipped-line count (Phase 4's diagnostic snapshot), and takes an exact-evidence-only filter.
- `interactive` and `subagent` are roles in their own rows; a subagent's usage is counted once, under the subagent, never folded into its parent.
- Done when the day 1 long-lived-seats readout (2026-10-11, split at the 21:35Z switch to gpt-6.1-sol) is reproduced from the input fixture `scripts/usage/fixtures/day1_seats/` to the expected output `scripts/usage/fixtures/day1_seats/expected.md` under weight version `gpt-6.1-sol@2026-10-10T21:35Z` (1 / 0.05 / 5), and a new metric takes one entry. Report fixtures also pin `interactive` and `subagent` rows, missing assignments, estimated attribution, unrecorded lenses, the skipped-line count and the exact-evidence filter.
- **Seats:** `1 writer + 1 tester`. `impl` — `scripts/usage/usage_report.py` and `scripts/whoami/seat_usage.py`. `test` — new `scripts/usage/test_usage_report.py`, `scripts/whoami/test_seat_usage.py`, and the report fixtures. Gate: `python3 -m unittest scripts.usage.test_usage_report scripts.whoami.test_seat_usage`; basedpyright 0/0/0 on `scripts/usage scripts/whoami/seat_usage.py`, test files included.

### Phase 7 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Each source keeps its own last-success watermark: an unreachable Mac leaves its rows stale and marked so in the daily readout (`seat_usage.py --daily`) and the dailies (`dailies_render.py`), while local sources keep updating; the backlog fills when it is back.
- Done when every reachable source is never more than an hour behind, with no agent involved, and a simulated Mac outage shows as partial coverage and then recovers.
- **Seats:** `1 writer + 1 tester`. `impl` — collector freshness and watermarks, both report adapters, and the timer-definition handoff to natedev. `test` — collection-outage tests, `scripts/whoami/test_seat_usage.py`, `scripts/production/test_dailies_render.py`. Gate: `python3 -m unittest scripts.usage.test_collect scripts.whoami.test_seat_usage scripts.production.test_dailies_render`; basedpyright 0/0/0 on every edited Python file, test files included.
