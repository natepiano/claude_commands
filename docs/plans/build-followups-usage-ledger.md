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
  - `scripts/usage/rollouts.py` — the standard-library Codex rollout parser; `scripts/whoami/seat_usage.py` — the store-backed seat readout.
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

#### As-built

- `settings.json` registers the command `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/session-start-agents-log.py"` twice: in the `hooks.SessionStart` entry that has no `matcher` (it fires for startup, resume, clear and compact), and in a new `hooks.SubagentStart` key with one entry that has no `matcher`.
- Every Claude session then records itself as `interactive` in `agents.jsonl` as it starts, and every subagent as `subagent`, unless a line already names it.
- `SubagentStart` delivers the parent `session_id`, `agent_id` (form `a` + 16 hex) and `agent_type` before the subagent's first turn. `SessionStart` does not fire for a subagent. `SubagentStop` also carries `agent_id` but fires after the work, so it is not registered.
- Three activation tests load the repository's `settings.json`, require exactly those two registrations and no other event naming the hook, and run each registered command through `sh -c` from an unrelated working directory, with `HOME` at a temporary directory whose `.claude` links to the repository and `USAGE_STATE_DIR` at a temporary directory. A second delivery writes nothing.

**Files:**
- `settings.json` — the two hook registrations.
- `scripts/usage/test_session_start_hook.py` — the activation tests, beside the hook's own tests.

**Binds later work:** `agents.jsonl` holds `interactive` and `subagent` lines only for sessions started after this registration reached a machine's `~/.claude`; an earlier turn has no line and is unassigned. The live check stays owed after the merge: a new session and a new subagent each appear in the real log within a second, the subagent's line dated before its first turn.

**Gotchas:** the showrunner's merge check refuses settings diffs, so a change to `settings.json` needs the user's approval before main; the user approved this one on 2026-10-11. Hook commands in `settings.json` run through `$HOME/.claude/scripts/lib/py`.

### Phase 4 — One store of every model turn · status: done

#### As-built

- `python3 scripts/usage/collect.py [--db PATH] [--machine NAME] [--claude-root DIR] [--codex-root DIR] [--agents-log FILE] [--no-mac]` reads Claude transcripts, Codex rollouts, the role log and run-history files into SQLite, one row per model turn. Defaults: `<state_dir()>/usage.db`, `natedev`, `~/.claude/projects`, `~/.codex/sessions`, `<state_dir()>/agents.jsonl`. It prints `<machine> <source>: <n> turns added; <state>` per source (`ready`, `missing`, `unreadable`, `partial`, `skipped`, `unreachable`) and exits 0 unless the database cannot be written.
- Tables: `turns(machine, family, kind, parent_id, agent_id, turn_key, at, model, effort, input, cache_write_5m, cache_write_1h, cache_read, output, before_first_edit, role, lens, attribution, lens_state)`, primary key (`machine`, `family`, `parent_id`, `agent_id`, `turn_key`); `compactions(machine, family, parent_id, agent_id, at)`, primary key all five; `cursors(machine, path, offset)`; `role_log_sources(machine PRIMARY KEY, problems, state, read_at)`, replaced each run. `family` is `claude` or `codex`; `kind` is `session`, `subagent` or `thread`; `parent_id` is `''` except for a subagent; `at` is UTC ISO-8601; `model` is `unreported` when the log names none; `effort`, `role` and `lens` may be NULL; `attribution` is `recorded`, `estimated` or `unassigned`; `lens_state` is `known`, `unrecorded` or `not_applicable`. In Python the agent is `ClaudeSession | ClaudeSubagent | CodexThread`; NULL and `''` exist only in the SQL layer.
- A Claude turn is keyed by (`message.id`, `requestId`) and is one row per machine, enforced by the partial unique index `claude_message` on (`machine`, `turn_key`) where `family = 'claude'`: repeated lines and copies in other files merge into the first row (largest of each token class, smallest `before_first_edit`), which keeps its identity, time, model, effort and attribution. Cache-creation tokens beyond the two `ephemeral_*` classes count as 5-minute. `before_first_edit` is 1 until the agent's first `Edit`, `Write` or `NotebookEdit` call, and the message holding that call is 0. A Codex turn is keyed by its line's byte offset; uncached input is `input`, cached input `cache_read`, cache writes 0; its parsers are imported from `scripts/whoami/seat_usage.py`.
- Roles (`RoleResolver.resolve`): a role-log line gives `recorded`, or `estimated` when `approximate`; `history_root()/runs/*.jsonl` gives an `estimated` `director` for each `main_agent.session_id` from the first event naming it, which wins over no line or an `interactive` line no later than it; anything else is `unassigned`. `role_at` gets the whole log only for a Claude session whose 8-hex prefix matches exactly one known session with a recorded `worker` history; every other identity is looked up by exact id. `reattribute` re-resolves every stored turn of the machine each run, so a late role-log line corrects earlier turns without adding rows; an `unreadable` role log leaves stored attribution as it was. `history_root()` treats an empty `PLAN_DELEGATE_HISTORY_DIR` as unset.
- Incremental: a file whose size equals its cursor is skipped; a changed file is replayed from its start and only lines at or past the cursor are saved; a line with no trailing newline waits for the next run; one machine's pass is one transaction.
- `scripts/usage/weights.py`: `ModelWeights(model, effective_from, cached, cache_write_5m, cache_write_1h, output)` with input as 1, `weights_at(model, at) -> ModelWeights`, `weighted(turn_row, at) -> float`. Entries: `gpt-5.6-sol` 0.1 cached, 8 output; `gpt-6.1-sol` from 2026-10-10T21:35Z 0.05 cached, 5 output; `claude-*` 0.1 cache read, 1.25 five-minute write, 2 one-hour write, 5 output. No entry in force raises `UnweightedModel`; weights are applied on read, never stored.

**Files:**
- `scripts/usage/collect.py` — sources, parsers, identity, attribution, schema, cursors, the Mac pass, the CLI.
- `scripts/usage/weights.py` — the dated weight table.
- `scripts/usage/test_collect.py` — 45 tests that drive the CLI and read the tables.
- `scripts/usage/fixtures/collect/` — hand-written logs, role logs and run-history files.

**Binds later work:**
- The Mac's turns are read on the Mac: `collect_mac` and `REMOTE_READER` are the whole Mac half and store rows under machine `mac`. In the largest Mac file the parser needs about 25 MB of 754 MB, and scanning it there takes 0.7 s.
- Outcomes in the same store: `compactions` holds the normalized compaction events of both families.
- Metrics as named queries: `role_log_sources.state` and `turns.attribution` are the coverage each figure reports; a run-history director is always `estimated`.
- Kept up to date by itself: nothing runs the collector yet.

**Gotchas:**
- The Mac pass is not usable; run with `--no-mac`. The ssh link carries about 6 MB/s and the Mac holds 112 MB of Claude logs and 5.4 GB of Codex logs in 1,859 files (largest 754 MB, median 0.5 MB). Each changed file is sent whole as base64 in JSON inside one transaction, so a first read needs at least 20 minutes and an interrupted pass keeps nothing.
- Session files are read before `subagents/` files, so a parent keeps a message its fork copied; sorted path order puts the subagent file first.
- Creating `claude_message` fails on a database that already holds duplicate Claude rows; no `usage.db` predates the index.
- A first import of natedev's logs takes 287 s (896,514 turns, 322 MB database); a rerun takes 11 s.
- `gpt-6-sol` (273,189 turns) and `gpt-6-astra` (34,226 turns) have no weight entry; 357 rows carry the model `<synthetic>`.
- Role coverage at first import: Claude 206,448 estimated / 1,755 recorded / 191,755 unassigned; Codex 25,557 estimated / 10,398 recorded / 460,601 unassigned.
- `role_log_sources.problems` describes the log only when `state` is `ready`; read `state` first.

**Ruled out:**
- Counting a Claude message once per file or per agent: 0.19% of all tokens were counted twice.
- Keeping the first line of a repeated message: 5.8% of output was lost.
- Dating a director from the run's start: a takeover mid-run was backdated.
- Source states as an enumerated type in this phase: no consumer yet; the type arrives with the stored source record.
- Patching the whole-file Mac transport: the reader has to run on the Mac, a redesign.

### Phase 5 — The Mac's turns are read on the Mac · status: done

#### As-built

- `read_file(machine, path, family, previous) -> Iterator[Turn | Compaction | FileCursor]` is the one reader; `collect_file` and the Mac half both call it. A changed file is read from its start and yields only measurements at or past `previous`, then its new cursor. A file smaller than `previous` is read from zero.
- `collect_mac` runs `REMOTE_BOOTSTRAP` with `python3 -c` over ssh. `reader_payload` sends the sources of `scripts/usage/collect.py`, `scripts/usage/agents_log.py` and `scripts/whoami/seat_usage.py` and the stored cursors on stdin; the bootstrap loads them under their own module names and calls `read_mac(offsets)`. The Mac needs `python3` and no checkout, and nothing is written to its disk.
- `read_mac` prints one JSON record per line, in order: `manifest` (Claude session paths, each root's state); `agents.jsonl` as base64 `chunk` records; one `director` record per assignment from `local_directors("mac")`; `attribution_ready`; then per changed file a `file` record, one `measurement` per turn or compaction (`kind` names which) and its `cursor`, or `problem` when the file cannot be read, which makes that family `partial`. Session files come before `subagents/` files. Transcripts and run-history files never cross the link.
- `consume_remote` receives. `remote_measurement` and `remote_director` turn each record back into a `Turn`, `Compaction` or `EstimatedDirectorAssignment` and raise `RemoteReadFailed` on a missing or invalid field. `receive_measurements` commits one file's rows (through `save_measurement`, merges included) and its cursor in one transaction. The role log snapshot and `reattribute` commit together at `attribution_ready`, on every pass and before any turn file.
- A failed Mac read exits 0 and keeps every file it finished. With no file kept, Claude and Codex are `unreachable`; otherwise they are `partial` and print the turns kept. The printed line carries `(<reason>)`; the stored state does not. A committed role snapshot is kept: `unreachable` reaches `role_log_sources` only while the snapshot is pending.
- `SourceState` is an `Enum` of `ready`, `missing`, `unreadable`, `partial`, `skipped`, `unreachable`. `save_turn_source` writes `turn_sources(machine, family, state, read_at, last_complete_at)`, primary key (`machine`, `family`), on every run for both machines and families; `last_complete_at` moves only on `ready`; `--no-mac` stores `skipped`. `source_paths(root, family) -> tuple[tuple[Path, ...], SourceState]` gives `missing` for an absent root, `unreadable` for a root that cannot be listed, and `partial` with the readable paths for a nested folder that cannot be listed.

**Files:**
- `scripts/usage/collect.py` — the collector: local read, both halves of the Mac read, source states, strict wire decoding.
- `scripts/usage/test_collect.py` — the collector tests; every Mac case runs through the fake `ssh`.
- `scripts/usage/fixtures/collect/` — invented logs. `mac/fake_ssh.py` runs the remote command in a fixture home, from a directory outside the checkout with `PYTHONPATH` unset, and can cut or corrupt the stream.

**Binds later work:** Code that runs on the Mac lives in the three sent modules, standard library only. A new measurement kind is one more `kind` in `measurement_record` and `remote_measurement`; lines before a stored cursor are not sent again, so it needs the files already read to be read once more from zero. `compactions` already holds Mac rows, and a thread's first and last turn are in `turns`. An unreachable or cut Mac is a finished run, and a time limit on a run must allow about 10 minutes for a first read of the Mac.

**Gotchas:**
- A token count outside 0..`MAX_SQLITE_INTEGER` (2**63-1) is never a measurement: locally the line is skipped and its cursor advances; a received record with one ends the pass as a failed read.
- Listing uses `os.scandir` because Python 3.13 `Path.glob` hides listing errors.
- ssh runs with `BatchMode=yes`, `ConnectTimeout=5`, `ServerAliveInterval=5`, `ServerAliveCountMax=1` to `MAC_HOST`. A stream with no final `rc=0` line is a failed read; a line without its newline fails with `remote connection cut inside a record`.
- Measured on the real Mac, 2026-10-12: 112 MB of Claude logs and 5.4 GB of Codex logs in 1,859 files (largest 754 MB) hold 5,522 Claude turns, 278,850 Codex turns and 1,660 compactions. A measurement is 477 bytes on the wire. The link ran at 0.42 MB/s and at 6 MB/s on the same day. The first full read took about 9 minutes at the slow speed; a pass with nothing new takes 8.8 s.
- The Mac has no `agents.jsonl` yet: its role log reads `missing`, and Mac roles are estimated from run history only.

**Ruled out:** copying the Mac's raw logs to natedev (holds transcripts twice; whole files in one transaction hit a 720 s limit twice and kept nothing); sending run-history files as bytes (59 files, 49.6 MB on the wire, 117 s on every pass, 2026-10-12).

### Phase 6 — Outcomes in the same store · status: done

#### As-built

The store is at `PRAGMA user_version` 6 (`STORE_VERSION`) and holds what the work produced beside the turns it used: ten more tables, four more source families in `turn_sources` (`history`, `checkpoints`, `readings`, `account_roles`) and a SQL function for weighted usage. `migrate_store` deletes every `cursors` row once, in the transaction that raises an older store to version 6, so every file is read again from zero; the turn keys keep each turn single. Each new family prints one line, `<machine> <family>: <n> outcomes added; <state>`, with the same states as the turn families.

- **Run history, both machines (family `history`, one `turn_sources` row per machine).** `read_history_file` reads `history_root()/runs/<run id>.jsonl` through `cursors` like every other log; the run id is the file's stem. Eleven event types are kept and every other type is skipped. A start event inserts the row, later events update it, and an update for a thing with no row is skipped. In `runs`, `phases` and `passes`, `state` is `open` until the finish event and then that event's `status`; `finished_at` is NULL only while `state` is `open`. `source_event` is the `event_id` of the event that created the row. A field an event does not carry is NULL in SQL and `EventFieldNotCarried` in Python, never a bare optional.
  - `run_started`, `run_finished` → `runs(machine, run_id, started_at, plan_doc, worktree, branch, state, finished_at, source_event)`, key (`machine`, `run_id`).
  - `phase_started`, `phase_finished` → `phases(machine, run_id, phase_instance_id, phase_id, title, started_at, state, finished_at, source_event)`, key (`machine`, `phase_instance_id`).
  - `pass_started`, `pass_finished` → `passes(machine, run_id, phase_instance_id, pass_instance_id, kind, task, slot, fix_pass, started_at, state, finished_at, source_event)`, key (`machine`, `pass_instance_id`); `kind` is the event's `pass_kind`, `task` its `called_task`, `slot` its `team_slot`.
  - `finding_opened`, `finding_verdict` → `findings(machine, run_id, phase_instance_id, finding_id, severity, caught_by, lens, opened_round, opened_at, state, state_at, source_event)`, key (`machine`, `phase_instance_id`, `finding_id`); `state` is `open` until a verdict, then that verdict's `resulting_state`.
  - `finding_batch_dispatched`, `finding_batch_landed`, `finding_batch_abandoned` → `repair_rounds(machine, run_id, phase_instance_id, round, dispatched_at, covered, state, state_at, source_event)`, key (`machine`, `phase_instance_id`, `round`); `covered` is how many findings the batch names; `state` is `dispatched`, `landed` or `abandoned`.
  - `HISTORY_FIELDS` is the one field table behind the local decode (`history_event`), the wire encode (`history_record`) and the wire decode (`remote_history_event`). The Mac's events cross the link as one `outcome` record per kept event, inside that file's records and before its cursor. natedev checks every field (times through `parse_timestamp`, counts through `remote_integer`), and a record it cannot read ends the Mac read as failed with earlier files kept.
- **Checkpoints, natedev only.** `outcomes.collect_checkpoints` runs `git log --all --format=%H%x00%ct%x00%s` in every repository `seat_usage.repositories(claude_repo, production_docs)` returns (`--claude-repo DIR`, `--production-docs DIR`, defaults from `seat_usage.default_sources()`) and stores each commit whose subject starts `checkpoint(` in `checkpoints(commit_id PRIMARY KEY, at, plan_slug, phase_id, mapping, machine, run_id, phase_instance_id)`; `at` is the committer time in UTC. `checkpoint_subject` reads `checkpoint(<plan-slug>): phase <id> — <title>`: the phase id is everything between `phase ` and the first ` — `, and it may contain spaces (6 of 637 real phases have one). Any other `checkpoint(` subject is stored with `plan_slug` and `phase_id` NULL. A stored checkpoint keeps the `plan_slug` and `phase_id` parsed when it was first stored; only its mapping changes afterwards.
- **Checkpoint mapping.** `checkpoint_mapping` is recomputed for every stored checkpoint on every collector run. Among `phases` rows on any machine whose run's `plan_doc` file name without `.md` equals `plan_slug`, whose `phase_id` equals the checkpoint's, whose `started_at` is at or before the commit, and whose `state` is `open` or whose `finished_at` is at or after the commit, the one with the latest `started_at` is taken: `mapping` is `mapped` and `machine`, `run_id`, `phase_instance_id` name that phase. With no such phase `mapping` is `unmapped` and the three are NULL.
- **Account readings and role changes, natedev only, each read with a cursor.** `account_readings(machine, account, at, remaining_state, remaining, credits_state, credits)`, key (`machine`, `account`, `at`), from `--readings FILE` (default `run_out.READINGS_LOG`). Each number is stored with its state: `not_reported` when the key is absent or null, `read` when it is a finite number in range (`remaining` 0 to 100, `credits` 0 or more), `unreadable` otherwise; the number column is NULL unless its state is `read`. In Python the three are `AmountNotReported`, `AmountRead` and `AmountUnreadable`. A line without a string `account` and a time `parse_timestamp` accepts is skipped. `account_role_changes(machine, at, role, label)`, key all four, from `--account-roles FILE` (default `accounts.roles_history_file()`). An absent file makes its source `missing`.
- **Thread lifecycle, both machines.** `read_file` yields `Opening` and `Edit` beside `Turn` and `Compaction`. `openings(machine, family, parent_id, agent_id, at)`, key the four identity columns: a Claude opening is the timestamp of the file's first record that has one and is never revised; a Codex opening is the `KnownThreadOpening` time and follows the thread's latest session metadata record (an upsert that writes only when the time differs). An agent whose opening is not known has no row. `edits(machine, family, parent_id, agent_id, edit_key, at)`, key the first five: one row per Codex record `is_edit` accepts (`edit_key` is the line's byte offset) and per Claude message `claude_edit` accepts (`edit_key` is that message's turn key). Unique index `claude_edit_message` on (`machine`, `edit_key`) keeps a Claude edit to one row per machine: a repeated line or a copy in another file adds nothing, and the first row keeps its identity. Over the link each is a `measurement` record of `kind` `opening` or `edit`, decoded strictly by `remote_measurement`.
- **Weights.** `weights.register(db)` defines the SQL function `weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output)`: what `weighted` returns, and NULL when no weight entry is in force. Weights are applied on read and never stored.

**Files:**
- `scripts/usage/collect.py` — the schema, `STORE_VERSION` and `migrate_store`, the run-history reader and its wire records, `Opening` and `Edit`, the source rows, the four flags.
- `scripts/usage/outcomes.py` — commits, readings, account role changes, the checkpoint mapping.
- `scripts/usage/weights.py` — `register`.
- `scripts/usage/test_outcomes.py`, `scripts/usage/fixtures/outcomes/` — CLI tests with a fixture `HOME`, explicit paths and temporary repositories with set committer dates.
- `scripts/usage/test_collect.py`, `scripts/usage/fixtures/collect/mac/fake_ssh.py` — the store version, and a fake ssh that counts `outcome` and `measurement` records by kind.

**Binds later work:** The usage report reads these ten tables and four source families, calls `weights.register(db)` on its connection before any `weighted_tokens` query, and owns showing the source states, `unmapped` checkpoints (NULL identity, so they count under no unit) and the three reading states. The hourly timer inherits the times measured 2026-10-12 on natedev (407,342 Claude and 502,912 Codex turns: first full read 320 s, rerun 43 to 50 s) and owns printing ssh's own reason when the Mac is unreachable: when the Mac leaves the tailnet during a read, the printed reason is the pipe error, not ssh's text.

**Gotchas:**
- `collect.py` imports only `agents_log`, `seat_usage` and the standard library at module level, because the Mac runs `read_mac` from those three sources sent on ssh stdin. Everything the Mac runs lives in `collect.py`; `outcomes` is imported inside `main`, which the Mac never runs.
- A candidate repository path that no longer exists is skipped: a worktree removed after its merge leaves its commits in the shared repository, and 4 of 18 real candidate paths were removed worktrees. A path that exists and `git` cannot read makes `checkpoints` `partial`, as does a failed `git log` or no repository at all. Commits are read on natedev only, so one made on the Mac counts once natedev's repository has it.
- 365,159 real turns have no weight entry, so `weighted_tokens` is NULL for them and a sum reports them as a separate count. A count from an outcome table is summed to one row per day before it joins `turns`; a row-for-row join multiplies the weighted sum.

**Ruled out:** importing `scripts/whoami/agent_accounts.py` (it reports the live state the readings file already records over time); storing weights; storing a finding's title, file or detail (`source_event` finds them in the history file).

### Phase 7 — Metrics as named queries · status: done

#### As-built

- **Command.** `python3 scripts/usage/usage_report.py [--db PATH] [--production-docs DIR] list` prints each metric's name and one-line description; `… <metric> [--by DIM[,DIM…]] [--since WHEN] [--until WHEN] [--zone NAME] [--exact]` prints one Markdown report. Defaults: the collector's store path, `seat_usage.default_sources().production_docs`, `--by role,model`, no bounds, zone `America/Los_Angeles`. `WHEN` is an ISO date or datetime, read in `--zone` when it names no offset; `--since` is inclusive, `--until` exclusive; `--exact` keeps only turns with `attribution = 'recorded'`. The store is opened read-only: the report reads no log and writes nothing. Every refusal (unknown metric, dimension, zone or date; a dimension the metric cannot take) is one stderr line naming what is accepted, with exit 2.
- **Registries, sides and turns.** `usage_report.py` holds three registries, one entry each; metric, dimension, outcome, format and side are named types past the argument parser. Dimensions (name → SQL expression): `day` (the date in `--zone`, SQLite date arithmetic over the zone's offset changes: `zone_offsets`, `day_sql`), `role` (`unassigned` when NULL), `lens` (the stored lens, else its `lens_state`), `model`, `effort` (`unreported` when NULL), `machine`, `unit`. A metric is a name, a one-line description, a numerator, an optional denominator and a format (`number`, `share`, `count`, `rate`); one added entry prints by name with no other change. A side is a `TurnSum` (a sum of named token values, answered from grouped totals), a `TurnAggregate` (any SQL aggregate over one row per turn) or an `OutcomeCount`. The report reads `turns` once into a query-local grain (`turn_grain`, `turn_groups`) and answers the table, the coverage figures and the thread counts from it; a one-row-per-turn relation exists only for a `TurnAggregate` side. Per turn it has every dimension, the five token classes, `weighted` (SQL from `weights.sql_weights(model, at)`, NULL with no weight entry), `output_weighted` (the output class alone), `before_first_edit` and `edited` (1 when the agent has an `edits` row). `register_report_functions(db, worktrees)` registers only the unit lookup. `interactive` and `subagent` are roles with their own rows, so a subagent's usage is counted once, under the subagent.
- **Outcomes and grouping.** An outcome is a name → a SELECT of `at` and the dimensions it carries: `checkpoints` (`checkpoints.at`; `day`, `machine`, `unit` from the mapped phase's `runs.worktree`; an unmapped checkpoint is machine `natedev`, unit `unknown`); `findings_closed` (`findings` with `state = 'accepted'`, at `state_at`; `day`, `machine`, `unit` from its run's worktree); `review_rounds` (`passes` with `kind = 'review'`, at `started_at`; `day`, `machine`, `unit`); `compactions` (every dimension: `unit` from the agent's folder; `role`, `lens`, `model`, `effort` from the agent's latest turn at or before it, else its earliest; with no turn, `unassigned`, lens `not_applicable`, model `unreported`); `threads` (distinct agents with a turn in the group; every dimension); `account_switches` (`account_role_changes` rows whose `label` differs from the previous row of the same `role`, a role's first row being no switch; `day`, `machine`). Each side is grouped by the requested dimensions it carries and the two are joined on the dimensions both carry, so an outcome count is one row per group before the join and no usage multiplies; a metric with no turn side refuses a dimension it does not carry.
- **Metrics**, all `TurnSum` or `OutcomeCount`: `weighted` (its table also shows `turns`); `weighted_per_checkpoint`, `weighted_per_finding_closed` and `weighted_per_review_round` (`weighted` over `checkpoints`, `findings_closed`, `review_rounds`); `before_first_edit_share` (over turns of agents that edited, `weighted` where `before_first_edit = 1` over `weighted`); `cache_read_share` (`cache_read` over `input + cache_write_5m + cache_write_1h + cache_read`, raw tokens); `output_share` (`output_weighted` over `weighted`); `compactions_per_thread` (`compactions` over `threads`); `account_switches` (the count).
- **Output**, in order: `<metric> — <description>`; one line with the bounds, the zone and, under `--exact`, `exact evidence only`; the table (one column per `--by` dimension in order, then the numerator, the denominator when there is one, and the value; rows sorted by the dimension columns); `Coverage`; `Sources`. `number` has thousands separators and no decimals, `share` reads `12.3%`, `count` is an integer, `rate` has two decimals; a zero or absent denominator gives `—`; a group with no weighted turn reads `unweighted`. With no turn inside the bounds the report prints its two header lines, `No turns in range.` and `Sources`. `Coverage` describes the turns the table covers: weighted usage and turns per attribution (`recorded`, `estimated`, `unassigned`) with shares; `Unweighted:` with each model that has no weight entry and its turn count (those turns are in no weighted figure and never get a default weight); the count of review turns with lens `unrecorded`; for a metric with an outcome side, the count of its outcome rows with unit `unknown`. `Sources` has one row per `turn_sources` row (machine, family, state, `last_complete_at` in `--zone`, or `never`) and one per `role_log_sources` row (machine, `role log`, state, `read_at`); `skipped lines: <problems>` shows only when the role log's state is `ready`; every source whose state is not `ready` is listed first, under `Not read in full:`.
- **Folder and unit.** `read_file` yields a folder measurement on both machines, stored in `folders(machine, family, parent_id, agent_id, folder)` with the four identity columns as primary key. Claude: the `cwd` of the agent's first record that has one (the first row is kept). Codex: the `cwd` of the session metadata record, following the latest metadata and written only when it differs. Over the link it is a `measurement` record with its own `kind`, decoded strictly by `remote_measurement`. The store holds only the folder; `unit` is looked up when the report runs: the `Unit` cell of a `## Units` table row, in any `*-production.md` under `--production-docs`, whose `Worktree` is the agent's folder or contains it (the longest worktree wins), else the folder's last path component, and `unknown` for an agent with no `folders` row. `STORE_VERSION` is 7, so an older store has its cursors cleared once and every file is read again.

**Files:**
- `scripts/usage/usage_report.py` — the three registries, the report, the command line; runs on natedev only.
- `scripts/usage/weights.py` — the dated weights, as the Python `weighted_tokens` and as SQL (`sql_weights`).
- `scripts/usage/collect.py` — the store: the folder kind and its wire record, the `folders` table, the indexes `turns_at` on `turns (at)` and `turns_agent_at`, `STORE_VERSION` 7.
- `scripts/usage/test_usage_report.py`, `scripts/usage/fixtures/report/` — the report tests: each drives the command line with explicit paths (`--db`, `--production-docs`, `USAGE_STATE_DIR`, a fixture `HOME`) against a store the collector builds from invented logs, and compares whole expected Markdown files exactly.
- `scripts/usage/test_collect.py`, `scripts/usage/test_outcomes.py`, `scripts/usage/fixtures/collect/`, `scripts/usage/fixtures/outcomes/` — the folder kind, version 7, the fake ssh's record counts.

**Binds later work:** A metric side is one of three kinds: `TurnSum`, `TurnAggregate`, `OutcomeCount`. `report_data(db, request)`, `render_report(db, request)` and `sources(db, zone)` in `scripts/usage/usage_report.py` are the report's own pieces, open to the seat readout; `sources` prints `never` for a source with no complete read. `weights.sql_weights(model, at)` gives the dated weights as SQL, while `weights.register(db)` defines a Python function that SQLite calls once per turn. The index `turns_agent_at` on `turns (machine, family, parent_id, agent_id, at)` exists and `STORE_VERSION` is still 7. Every stored time is UTC text ending `+00:00`, and the day arithmetic and the dated weights compare that text. `Outcome("threads", …, over_turns=True)` carries a query string nothing reads and a boolean that chooses between two forms.

**Gotchas:** A Python function called from SQL once per turn costs minutes on 1.2 million turns, so anything per turn is SQL. A stored time is 25 or 32 characters of UTC text ending `+00:00`, and the text comparisons named above depend on that form. Measured on natedev, 2026-10-12: a full read 178 s, a rerun 21 s; every additive report 2.3 to 2.8 s; a per-turn `TurnAggregate` metric 8 to 15 s. Models with no weight entry on natedev, by turns: gpt-6-sol 273,189; gpt-5.5 45,646; gpt-6-astra 34,295; gpt-5.3-codex-spark 6,610; unreported 3,402; gpt-5.6-terra 948; gpt-5.6-luna 688; `<synthetic>` 377; gpt-5.4-mini 7.

**Ruled out:** moving the showrunner's dailies onto the store (`dailies_render.py` shows no token figure and reads only the live account readings through `run_out.read_readings`); weighting each turn through the Python function from `weights.register(db)`, in the report or in the seat readout (the per-turn cost above); a registered function for the local day (the same cost).

### Phase 8 — Kept up to date by itself · status: done

#### As-built

- **Collector pass.** `collect.py`'s `main` takes an exclusive, non-blocking `fcntl.flock` on `<store path>.lock` before it opens the store; a second pass started beside a running one exits 0 at once with the one line `usage collect: another pass is running` and leaves the store untouched. The lock ends with the process, also when it is killed; the Mac half (`read_mac`) opens no store and takes no lock. The collector sets SQLite's write-ahead mode on the store (`PRAGMA journal_mode=WAL`), and both readers, the report and the seat readout, open the store through one function, `usage_report.open_read_store` (read-only, `sqlite3.Row`, one snapshot per run). When the Mac read fails, ssh's own first error line (the first non-empty line of its stderr) is the printed reason for the Mac's sources, and the caught error's text only when ssh wrote nothing; the stored state does not change.
- **Baseline readings.** `collect.py --baseline-readings PATH` (default: `default_sources().baseline_readings`) reads the readings from before go-live into `account_readings` as their own source family `baseline_readings`, with its own `turn_sources` row (`ready` or `missing`) shown in the `Sources` block; each expected report file carries that `baseline_readings` row. A live reading replaces a baseline reading with the same machine, account and time in either read order; a baseline reading never replaces a stored row. No table or column is added.
- **Readout.** `python3 scripts/whoami/seat_usage.py [--db PATH] (--daily [--t-live WHEN] | --before START END --after START END)` is a view over the store: it opens the store read-only (default: the collector's store path), reads no rollout, no ssh, no git and no readings file, and runs as a script from any directory with `PYTHONPATH` unset. Each column comes from stored rows of Codex threads on both machines (`family = 'codex'`; a thread is one `machine, parent_id, agent_id`, so the same id on two machines is two threads): threads opened (`openings` inside the piece); active threads (an opening, a turn, an edit or a compaction inside the piece, so a thread with none of its turns there still counts); weighted usage (the piece's turns); before first edit (over threads with any edit, the weighted usage of their turns before the thread's first edit, as a share of those threads' weighted usage in the piece); compactions, and compactions per active thread; checkpointed phases (`checkpoints` inside the piece); account switches per week and credits per day (`switches_per_week` and `credits_per_day`, fed from `account_readings` rows whose account label starts `codex ` in any case).
- **Weights and windows.** Weights apply by model and date as grouped SQL: token classes are summed per weight entry (`weights.sql_weights(model, at)` gives the entry in force as SQL) and each sum is multiplied by its entry's weights once; the readout never calls `weights.register`, and no Python function runs per turn. A turn whose model has no weight entry is in no weighted figure, and one `Unweighted:` line names each such model and its turn count over the printed windows. Each window (`Before`, `After`) is cut at each distinct instant strictly inside it at which a dated weight entry starts for a model that has Codex turns in that window: two entries starting at the same instant make one cut, and an entry in force from the beginning (`effective_from` is `datetime.min`) makes none. Each piece is one table row with every column computed for that piece alone; a window with no cut keeps its own label, and a cut window's rows are labelled with the piece's start in UTC (`After, from 2026-10-10 18:17Z`, `After, from 2026-10-10 21:35Z`). The `Weighting:` line names every weight version that weighted a turn in the printed rows, ordered by model and start: a dated one as `gpt-6.1-sol@2026-10-10T21:35Z: 1 / 0.05 / 5`, one in force from the beginning as `gpt-5.6-sol: 1 / 0.1 / 8`.
- **Verdict and freshness.** The verdict line (`Weighted usage per checkpointed phase: before …; after …; <change>`) compares the whole `Before` window with the last piece of `After`; a side with no checkpointed phase reads `unavailable`. Notes follow in one parenthesis, in this order, joined by `; `: `different weights: compare roughly` when the set of weight versions that weighted `Before`'s turns differs from the set for that last piece; `unweighted turns left out: before <n> of <N>, after <n> of <N>` when either side has a Codex turn with no weight entry; `Mac as of <last complete read, UTC>` when the Mac's Codex source is not `ready`, or `Mac never read in full` when it has no complete read. Below the table the readout prints the `Sources` block from `usage_report.sources(db, zone)` with UTC as the zone, one rendering called by both the report and the readout. Every source not `ready` comes first under `Not read in full:` (`partial`, `unreachable`, `skipped`, `missing`, `unreadable`) with its last complete read or `never`; the figures still include every stored Mac row. A window piece, a weight version (dated, or in force from the beginning) and a source's freshness (read in full at a time, not in full since a time, never read in full) are named types: no bare string, boolean or optional stands for one past the argument parser.
- **Timer.** The hourly timer lives outside this repository, in the NixOS configuration at `/etc/nixos/modules/linux/usage-collect.nix`: a pass starts one hour after the last one ended and one hour after login, with the user's PATH and `TimeoutStartSec = 45min`. The collector sends no message; a pass's output goes to the journal. The timer becomes active at the user's next system rebuild; no pass started by the timer and no pass with the Mac reachable has run live yet.

**Files:**
- `scripts/usage/collect.py` — the pass lock, write-ahead mode, ssh's error text, `--baseline-readings`, `MAC_HOST`. It sends `agents_log`, `rollouts` and `collect` to the Mac, so each of those three imports the standard library only and no `scripts` module (`collect.py` imports `outcomes` inside the functions that run on natedev).
- `scripts/usage/rollouts.py` — the Codex log parser: standard library only, the module sent to the Mac.
- `scripts/usage/outcomes.py` — the baseline readings source, which reading wins, and the shared helpers `git_text`, `cell_value`, `production_paths`, `repositories`, `ReportSources` and `default_sources`.
- `scripts/usage/usage_report.py` — `open_read_store`, the `Sources` rendering, and `Outcome` as two kinds, `QueryOutcome` (read from its own query) and `TurnGroupOutcome` (counted over the turn groups). It imports the module `outcomes` and also has local variables of that name in four functions.
- `scripts/whoami/seat_usage.py` — the readout only; imports `collect`, `usage_report` and `weights` at module level. Nothing under `scripts/usage/` imports `seat_usage.py`: that import is a cycle.
- `scripts/usage/test_rollouts.py`, `scripts/whoami/test_seat_usage.py`, `scripts/usage/test_collect.py`, `scripts/usage/test_outcomes.py`, `scripts/usage/test_usage_report.py`, `scripts/usage/fixtures/day1_seats/` — the tests and fixtures.

**Binds later work:** "Seat roster and switch-count repairs" builds on the readout's `store_readings(db)`, `switches_per_week(readings, piece.window)`, `credits_per_day(readings, piece.window)` and `build_report`, on the read-only opener `usage_report.open_read_store`, and on the `account_role_changes` table (columns `machine, at, role, label`). `switches_per_week` counts every account-label change between neighbouring readings, so with two Codex accounts read at the same instants the daily readout prints about 750 switches a week for the newest piece; `credits_per_day` already sums per account, and the day 1 readout is not affected. "Weights for gpt-6-sol and gpt-6-astra" adds to the dated entries of `weights.MODEL_WEIGHTS`: turns of a model with no entry are left out of the weighted figure and counted on the readout's `Unweighted:` line and in its verdict note.

**Gotchas:** The test gate of this area names `scripts.usage.test_rollouts` as well as `scripts.whoami.test_seat_usage`, `scripts.usage.test_collect`, `scripts.usage.test_usage_report`, `scripts.usage.test_outcomes` and `scripts.usage.test_agents_log`. `scripts/whoami/` is not a package (it has no `__init__.py`), and the merge check loads its tests by discovery (`python3 -m unittest discover -s scripts/whoami -p 'test_*.py'` from the repository root), so `scripts/whoami/test_seat_usage.py` imports by full path (`from scripts.whoami import seat_usage`, `from scripts.usage.test_outcomes import OutcomeFixture`, `from scripts.usage.test_collect import ...`). Every collector run in a test names every source (`--db`, `--claude-root`, `--codex-root`, `--agents-log`, `--readings`, `--baseline-readings`, `--account-roles`, `--claude-repo`, `--production-docs`) and either passes `--no-mac` or puts the fixture ssh first on `PATH`: a fixture `HOME` alone does not stop the collector from reaching for the real Mac. A pass that switches an older store to write-ahead mode while a reader holds it fails once, and the next pass succeeds. Measured 2026-10-12 on natedev: a full pass 214 s, the next 31 s, the readout about 2 s, the store about 600 MB.

**Ruled out:** function-level imports to hide the import cycle, and an ignore comment or a `pyrightconfig.json` edit for it; a re-read rule for stores built by older code (the version check already clears the cursors); a spliced comparison to keep the expected report files byte-identical; a clock-hour timer with catch-up and a pinned PATH (the timer follows the NixOS repository's own job form).

### Phase 9 — Seat roster and switch-count repairs · status: done

#### As-built

- **Guarded roster write.** `_update_roster_if_expected(session_dir: str, name: str, record: ThreadRecord, expected: ExpectedRoster) -> bool` reads the name's roster entry, compares it with `expected` and writes, all inside one `fcntl.flock` section of the roster file, and returns whether it wrote. `ExpectedRoster = RosterOnThread | RosterAbsent | RosterEndedOnThread`: `RosterOnThread(thread_id)` matches an entry on that thread, `RosterAbsent()` a name with no entry, `RosterEndedOnThread(thread_id)` an entry on that thread with status `ended`. `_write_roster_record(handle, roster, name, record)` holds the storing rules for both writers, `_update_roster` and the guarded one: the fields kept from the previous entry (`context_tokens`, `role`, `instructions_file`, `launcher_pid`, `port`) and the rule that keeps an entry ended on the same thread. No roster field is added.
- **Writers on it.** Every roster write in `_stream_turn` (status and context updates, the capacity states, both ends, the `failed` write of its failure path), `command_compact`, `command_steer`, `command_fork`'s destination entry, `_resume_seat_thread`'s replacement write (`RosterOnThread` of the thread whose resume was refused) and `command_follow`'s write after it (`RosterOnThread` of the replacement). A name ended and started again therefore keeps its new thread while an older launcher, compaction, steer, fork or follow-up is still at work for the earlier one. The turn stream, the compaction and the steer write only while the name is on the thread their record names; otherwise nothing is written for the name and the caller discards the result: the run streams to its end with the same outcome (`RunCompleted`, `FailedWithThread` or the capacity outcome), the compaction prints its sizes and exits, the steer returns.
- **Name taken during a fork or a follow-up.** `command_fork` writes its destination only when the entry is what it saw at its start (`RosterAbsent`, or `RosterEndedOnThread`). Otherwise it writes nothing for the name, releases only its own thread with `_release_seat_thread(client, <fork thread id>, session_dir)`, and prints one stderr line: `codex_mesh: <name>: name was taken while the fork ran; fork refused` with exit 2, or, when the release is not confirmed (`SeatThreadStateUnknown`), `codex_mesh: <name>: name was taken while the fork ran; fork thread <thread id> not released: <reason>` with exit 1. It never calls `command_end` and leaves the name's end marker and pending file untouched. A follow-up whose resume was refused and whose name was taken meanwhile goes through `_refuse_taken_follow_up(client: Client, replacement: str, session_dir: str) -> NoReturn`: it releases only the replacement thread it opened and ends with exit 1 and `name was taken while the follow-up ran; follow-up did not start`, or `name was taken while the follow-up ran; replacement thread <id> not released: <reason>`; the launcher reports it as any failed follow-up.
- **Switch count.** In the seat readout a switch is a change of the account that holds the `seats` role (`accounts.CODEX_ROLE`, imported inside `store_switch_history`, never a second literal): two Codex accounts read at the same instants alternate their labels row by row, so a reading-label change is no switch there. `store_switch_history(db: sqlite3.Connection) -> RoleSwitchHistory` reads `account_role_changes` through the read-only connection. `RoleSwitchHistory(start: HistoryStart, switches: tuple[datetime, ...])`, with `HistoryStart = HistoryBeginsAt | NoStoredHistory`: `start` is the earliest `at` of any row of any role, and `switches` holds each `seats` row whose label differs from the previous `seats` row's on the same machine, ordered by `at`, then `label` (the role's first row is no switch, a repeated label is none, a `unit` row is none, and the previous row comes from the whole history). `switches_per_week(readings, window, history=NO_ROLE_HISTORY) -> SwitchRate` adds two counts for one table row: label changes between neighbouring readings that both lie before the start of the history (every reading under `NoStoredHistory`), and history switches with `coverage_start <= at <= coverage_end`. A reading's label is never compared with a role row's label. `build_report` reads the history once and passes it to each row. Coverage, the `SwitchHistoryUnavailable` cases and `credits_per_day` (the sum of each account's own decrease) are as before; no table, no column, no `STORE_VERSION` change, and the collector is not touched.

**Files:**
- `scripts/agents/codex_mesh.py` — the seat tool: `ExpectedRoster` and its three variants, `_write_roster_record`, `_update_roster_if_expected`, `_refuse_taken_follow_up`, and the guarded callers.
- `scripts/agents/test_codex_mesh.py` — its tests, the lost-write cases included.
- `scripts/whoami/seat_usage.py` — the seat readout: `HistoryStart`, `RoleSwitchHistory`, `store_switch_history`, `NO_ROLE_HISTORY` and the switch count.
- `scripts/whoami/test_seat_usage.py` — its tests.

**Binds later work:** The compacted-seat size work builds on these. `command_compact` writes its size through `_update_roster_if_expected(session_dir, name, record, RosterOnThread(thread_id))`. `_write_roster_record` is where a size (`context_tokens`) is stored and kept for every write through `_update_roster` and the guarded write; reset, prime and fork write an explicit size of 0, and reset does so outside it, in `_clear_reset_context`'s own locked section of the roster file. `list` prints no size. `scripts/delegate/implement.sh` reads the recorded size as `entry.get("context_tokens", 0)` and compares it with its limit, and `scripts/delegate/review.sh` has its own copy of that read; their tests are `scripts/delegate/test_implement_launcher.py` and `scripts/delegate/test_review_launcher.py`. Measured 2026-10-12: `scripts/agents/codex_mesh.py` is 4,298 lines and `scripts/agents/test_codex_mesh.py` holds 268 tests.

**Gotchas:** Still on the unguarded `_update_roster`, because each puts a new thread under a name: the launcher's `starting` write in `_attach_and_run`, `_move_idle_seat`'s two writes (it holds the name's pending-file lock across its check and both writes; `command_end` and a launcher's start take the same lock) and `command_prime`'s template entry. That lock does not cover `command_follow`. The roster lock is per open file, so the comparison lives in the same locked section as the write: a function that takes the lock and then calls `_update_roster` waits on itself. `command_end` works by seat name (pending-file lock, end marker), so it never closes a thread whose name now belongs to another thread. The seat tool wraps provider requests in `_serving_request` and binds each client to a serving account with `_bind_serving_server`: a test's fake client carries what those read, and `_stream_turn`'s `FailedWithThread` carries the serving-account fields. Measured 2026-10-12 on a scratch store of natedev's logs: 2.8 switches a week for `Before` and 0.0 for each `After` row of the day 1 readout; counting reading-label changes on that store gives 750.3 for the daily readout's last row. natedev's role history holds three `unit` rows and no `seats` row, so an `After` row moves only once a `seats` change is stored.

**Ruled out:** `_resume_seat_thread`'s replacement write and `command_follow`'s write on the unguarded `_update_roster` (a restarted name's thread is overwritten); a `_roster_still_on_thread` read followed by a separate write (another writer fits between the two); an optional thread id for what a writer expects, and a bare optional for where the history begins; a size column in `list` for the compacted-seat size work (nothing reads one).

### Phase 10 — A compacted seat shows its real size · status: done

#### As-built

- `RecordedContextSize = KnownContextSize | NotMeasuredSinceCompaction | ContextSizeUnavailable` in `scripts/agents/codex_mesh.py`. A roster entry's `context_tokens` is an integer or the literal `"not_measured_since_compaction"`. The size after a compaction is a named state: never 0, never an optional integer, and never the size from before, because a kept size over the limit would compact the seat before every follow-up.
- `command_compact` stores the state and prints `compacted <name>: <before> tokens, size not measured until its next turn` when the compaction turn reports 0 (`_compact_thread` returns that turn's own usage as a `KnownContextSize`). A compaction turn that reports a size above 0 is stored as measured and printed as `compacted <name>: <before> -> <after> tokens`. A compaction that fails or times out leaves the entry's size unchanged. The write goes through `_update_roster_if_expected` with `RosterOnThread` for the compacted thread, so a compaction that ends after its name was replaced writes nothing.
- `_write_roster_record(handle, roster, name, record)` keeps the state, as it keeps a measured size, when a record carries no size. A record that carries a size replaces it: the first token-usage update of the seat's next turn, `_clear_reset_context` on a reset, and prime and fork (0 for a thread that has had no turn), each stored as a measured size. The stored value is encoded in `command_compact` and read back in `_write_roster_record`, so the two change together.
- `implement.sh` and `review.sh` read the recorded size in their inline Python block with no default of 0, and their integer check ignores a non-integer size: a seat in the state is not over `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` and gets its follow-up with no second compaction. Each posts the line `compact` prints, unchanged, with the prefix `launcher: `. `command_list` prints name, status and thread id, and no size.

**Files:**
- `scripts/agents/codex_mesh.py` — the size state, the compaction's roster write and printed line.
- `scripts/agents/test_codex_mesh.py` — 275 tests, seven of them on the size state.
- `scripts/delegate/implement.sh`, `scripts/delegate/review.sh` — the inline size read. In `review.sh` the comment above `if [[ -z "${REVIEW_SEAT}" ]]` gives only the lasting reason: a solo review creates its artifacts before agent resolution; a seated review claims first, so a busy-seat refusal cannot write over another review's status or findings.
- `scripts/delegate/test_implement_launcher.py`, `scripts/delegate/test_review_launcher.py` — two tests each: a follow-up to a seat in the state with no compaction first, and the compaction line posted as printed. All four are tests of kept behavior: they hold on the launchers as they were, whose integer check already ignored a non-integer size.

**Binds later work:**
- "A late compaction leaves a newer turn's record alone" builds on `RecordedContextSize`, the `ExpectedRoster` variants `RosterOnThread(thread_id)`, `RosterAbsent()` and `RosterEndedOnThread(thread_id)`, `_update_roster_if_expected(session_dir, name, record, expected) -> bool` and `_write_roster_record`. It also corrects the refusal `compact` gives a seat in the not-measured state, which today carries the text for a seat that has no recorded size.
- "What seat compactions cost, and a limit chosen by replay" sets the limit: `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` stays 100000 until that work measures. A compaction keeps about 100,000 tokens once a seat has aged, so the limit compacts before nearly every follow-up.

**Gotchas:**
- `_update_roster_if_expected` refuses only a name that moved to another thread. A write that lands after a later turn on the same thread still replaces that turn's record.
- A run's launchers execute the live checkout under `~/.claude`, not a worktree. The new board line and the follow-up with no second compaction are not yet observed live.

**Ruled out:** repairing the late same-thread roster write together with the size state (that defect is older than the size state); raising `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` to 200,000 as a stopgap (arithmetic on 42 compactions shows no gain).

### Phase 11 — A late compaction leaves a newer turn's record alone · status: done

#### As-built

- Every roster entry carries `revision`, an integer that rises by one on each write of that entry, inside the roster file's lock. Its type is `RosterRevision(value)`, and `_roster_revision(value) -> RosterRevision` reads it from an entry. An entry written by older code has none and reads as `RosterRevision(0)`, as does a value that is not an integer of 0 or more. No optional integer stands for a missing revision.
- Every writer of an entry advances it. `_write_roster_record` stores the previous revision plus one. The writers that change an entry in place call `_advance_roster_revision(entry)`: `_set_roster_role`, `_mark_roster_ended`, `_mark_all_roster_ended`, `_claim_follow`, `_restore_follow_claim`, `_note_seat_port` and `_clear_reset_context`. The count belongs to the name: a write that puts another thread under the name continues it. A refused guarded write changes nothing, the revision included.
- `ExpectedRoster` is `RosterOnThread | RosterAbsent | RosterEndedOnThread | RosterUnchanged`. With `RosterUnchanged(thread_id, revision)`, `_update_roster_if_expected` writes only when the name's entry still has that thread id and that revision. `command_compact` reads the revision from the entry it looks up at its start and writes the size after the compaction under `RosterUnchanged(state.thread_id, revision)`. A turn on the same thread that wrote the entry in between keeps its status, turn id and measured size. So does a turn that has already ended with the status and size the command read. The command's result is the same either way: it prints its compaction line and exits 0, because the compaction happened.
- `compact` asked of a seat whose size is `not_measured_since_compaction` prints `codex_mesh: <name>: size not measured since its last compaction; compact refused` on stderr and exits 2. It sends the server no request and leaves the entry unchanged. A seat with no recorded size keeps `codex_mesh: <name>: no recorded context size; compact refused`, exit 2.

**Files:**
- `scripts/agents/codex_mesh.py` — the revision, `RosterUnchanged`, the compaction's guarded write, the refusal text.
- `scripts/agents/test_codex_mesh.py` — 282 tests, seven of them new. Older tests that compare a whole entry (steer, reset, a write with no size) expect `revision` in it.

**State and consequence:**
- Roster entry `revision`, a new field on disk: every roster write in the seat tool produces it, and only the `RosterUnchanged` check reads it. `list`, the launchers and every other reader of the roster ignore it. Tests: `test_each_roster_write_advances_revision_even_when_ended_fields_are_kept`, `test_compaction_reads_a_legacy_entry_as_revision_zero`.
- A compaction whose roster write is refused, a changed outcome: the roster keeps the later turn's record, and a person sees only the unchanged compaction line on the board. Tests: `test_late_zero_compaction_keeps_the_newer_turns_complete_record`, `test_late_measured_compaction_keeps_the_newer_turns_complete_record`, `test_late_compaction_keeps_a_finished_turn_with_the_same_status_and_size`.
- The refusal text for a seat in the not-measured state, a changed diagnostic: seen by a person who runs `compact` by hand, because the launchers never compact such a seat. Tests: `test_compact_refuses_an_unmeasured_size_with_its_specific_reason`, and `test_compact_without_a_recorded_size_keeps_the_existing_refusal` for the kept text.

**Deviations:** none.

**Binds later work:**
- `revision` lives only in a session's roster file. It never enters the usage store and nothing outside the seat tool reads it, so "A Codex request is keyed by its session file too", "What seat compactions cost, and a limit chosen by replay" and "Weights for gpt-6-sol and gpt-6-astra" take nothing from it.
- The live check of the compaction board line is still not observed. It stays with the final gate, because a run's launchers execute the live checkout under `~/.claude`, not a worktree.

**Gotchas:**
- A new writer that changes an entry in place must call `_advance_roster_revision`. A late compaction can replace a write that leaves the revision as it was.
- The next revision is computed in two places, `_advance_roster_revision` and `_write_roster_record`, so the two change together.

**Ruled out:** comparing status, turn id and size in place of a revision (a turn's ending write removes its turn id, so a newer turn that ends with the same status and size would pass).

### Phase 12 — A Codex request is keyed by its session file too · status: done

#### As-built

- A Codex request's `turn_key` and a Codex edit's `edit_key` are `<session file name>:<byte offset>`. `codex_key(file_name: str, offset: int) -> str` in `scripts/usage/collect.py` is the one place the key is built. `CodexContext(machine, file_name)` has no default for the file name, so a context cannot be built without one, and no optional file name exists. `read_file` passes `path.name`, the last path component, on this machine and in the reader that runs on the Mac.
- Two session files of one thread that reach the same byte offset each keep their rows. A file read again from its start, or moved to another directory under the same name, produces the same keys, so `save_measurement`'s `INSERT OR IGNORE` stores nothing twice. A Claude request's key still comes from `claude_turn_key`.
- `FILE_NAMED_CODEX_KEYS_VERSION = 8` names the first store version whose Codex keys carry the file name, and `STORE_VERSION` equals it. `migrate_store(db)` deletes the cursors of a new store, or of a store never stamped (version 0), and stamps it 8.
- `check_store_key_version(db) -> int` returns the store's version, or raises `StorePredatesFileNamedCodexKeys`, which carries `version`, for a store stamped 1 through 7. Such a store holds Codex rows under bare offsets, and reading its files again would store each one a second time. `collect_pass` calls the check before its first write, ahead of `PRAGMA journal_mode=WAL` and the schema script, and before any local or Mac source is read. `migrate_store(db)` calls it too. `main` prints `collect: store version <n> predates file-named Codex keys; delete <db path> and run the collector again to rebuild it` on stderr and returns 1. The refused store's file bytes, journal mode, tables, version, rows and cursors are what they were.
- The reader that runs on the Mac sends an edit's key as text (`measurement_record`), and `remote_measurement` reads it with `remote_string`, for Claude and Codex alike. No code in `scripts/usage`, `scripts/whoami` or `scripts/model_study` reads a key as a number.

**Files:**
- `scripts/usage/collect.py` — the key, the version constant, the refusal, the remote edit key as text.
- `scripts/usage/test_collect.py` — 84 tests, eight of them new and one renamed for the new key form: requests and edits at matching offsets, repeated and replayed reads and a moved file, the refusals, the stamp of a new store, the Claude key, the remote text keys.
- `scripts/usage/test_outcomes.py` — 46 tests. The two that stamp a store 6 pin the refusal: no folder or turn is read again, and the Mac is not read.
- `scripts/usage/test_usage_report.py` — 35 tests. The version tests build the refusal text from `collect.STORE_VERSION`, for one version below it and one above. `fixtures/report/expected/store-version-6.txt` and `store-version-8.txt` are removed.

**State and consequence:**
- The refusal of a store stamped 1 through 7, a new outcome: `check_store_key_version` produces it on every pass over such a store until the store is deleted. Whoever runs the collector sees exit 1 and the one stderr line, which says what to do. Tests: `test_offset_key_store_versions_are_refused_without_changing_rows_or_cursors`, `test_legacy_store_refusal_preserves_bytes_journal_and_schema`, and in `test_outcomes.py` `test_version_six_store_is_refused_without_replaying_folders_or_turns` and `test_version_six_mac_store_is_refused_without_reading_remote_sources`.
- The key form `<session file name>:<byte offset>`, a changed stored value: the collector writes it, and every reader takes a key as text. Tests: `test_two_session_files_keep_requests_at_the_same_byte_offset`, `test_two_session_files_keep_edits_at_the_same_byte_offset`, `test_codex_turn_keys_use_the_file_name_and_byte_offset`, `test_repeated_cursor_reads_replayed_files_and_relocation_keep_each_event_once`.
- The remote reader's edit key as text, a changed value on the wire between the collector and the reader it sends to the Mac: nothing else reads it. Test: `test_remote_session_files_keep_matching_request_and_edit_offsets_as_text_keys`.

**Deviations:** the refusal check is its own function, called before the collector's first write as well as from `migrate_store`: the journal-mode change and the schema script both write to the store file, so a check inside `migrate_store` alone comes after them. The usage report's version tests read no fixture file.

**Binds later work:**
- "What seat compactions cost, and a limit chosen by replay" orders requests by `(at, turn_key)`. A Codex `turn_key` is text of the form `<session file name>:<byte offset>`: it is compared as text, and no number is read out of it. No store holds a bare-offset Codex key, because a store stamped 1 through 7 is refused.
- `migrate_store(db)` keeps its one parameter, and `SCHEMA`, `read_file`, `save_measurement` and the store's tables and columns are unchanged, so a caller that builds its own store through them gets a store stamped 8. `usage_report.check_store_version` still reads `collect.STORE_VERSION`, so the report accepts only a store stamped 8.
- A store for a later live run, the one for "Weights for gpt-6-sol and gpt-6-astra" included, is built by this code: a store stamped by older code is refused, not read again.

**Gotchas:**
- `check_store_key_version` stays ahead of every statement in `collect_pass` that changes the store. A step placed above it writes to a store that is then refused. `main` takes the pass lock before the check, so `<db path>.lock` is still created beside a refused store.
- A refusal test starts from a store the collector would not create: another journal mode, a missing table. A store with the current schema in WAL mode cannot show a write made before the refusal.
- The key holds the last path component only: a moved file keeps its keys, and two files of one thread with one name would share them. Codex names a session file `rollout-<date-time>-<id>.jsonl`.
- The usage report tells the owner of an older store `run the collector once`; the collector then says to delete the store and run it again. `usage_report.check_store_version` is unchanged.
- Measured 2026-10-12 on natedev, into a new store with no Mac read: 519,380 Codex requests and 34,019 Codex edits stored from 2,485 session files, equal to the counts taken from the files, every key distinct; a second pass changed no stored row and stored nothing twice.

**Ruled out:** rewriting an older store's keys in place (checked 2026-10-12: natedev, the machine the hourly collector is to run on, holds no usage store, so a rebuild costs one collection pass).

### Phase 13 — What seat compactions cost, and a limit chosen by replay · status: done

#### As-built

- `scripts/usage/usage_report.py` has the metric `compaction_reread_share`, titled `Share of weighted tokens spent on the first request after a compaction`: `OutcomeCount(OUTCOMES["compaction_reread"])` over `WEIGHTED`, printed as a share. A request is a `turns` row whose `input + cache_read` is above 0. For each stored compaction the outcome takes the first request after it on the same thread (same `machine`, `family`, `parent_id`, `agent_id`; `at` after the compaction's; first in `(at, turn_key)` order, the key compared as text). Each such request is counted once, however many compactions come before it with no request between them. The numerator sums that request's uncached input and its cache writes at their weights, with no cache reads and no output; the denominator is weighted tokens. The row's time and every dimension value are the request's, and the report's window applies to the request's `at`. A first request whose model has no weight entry adds nothing, is listed as unweighted, and is not replaced by a later one.
- `QueryOutcome` has a fifth field, `exact_where: str = "1"`, an SQL predicate that `turn_relation` adds to the outcome's rows only under `--exact`. For `compaction_reread` it is `t.attribution = 'recorded'`: under `--exact` both sides of the share keep recorded requests only, and a first request that is estimated or unassigned is dropped, not replaced by a later recorded one. The line `Outcome rows with unknown unit (<outcome>): <n>.` counts rows (`count(*)`) for a `QueryOutcome`; a `TurnGroupOutcome` keeps its own aggregate there.
- `scripts/usage/compaction_replay.py` is run as `python3 -m scripts.usage.compaction_replay --db <store> [--since <day>] [--until <day>] [--follow-up-prompt <tokens>]`; `--db` defaults to `collect.store_path()` and the prompt to 11,000 tokens. It reads through `usage_report.open_read_store` and `usage_report.check_store_version`, in one read-only transaction. It covers threads of family `codex` that have a stored compaction and a request, and whose every request has a weight entry for its model on its date. `--since` and `--until` take whole threads by the `at` of their first request. A thread's requests, in `(at, turn_key)` order, are cut into segments at each compaction: a request whose `at` equals a compaction's stays in the earlier segment, and a compaction with no request after it starts none. The compaction request's own cost is in no rule, and Codex compacting a full context is not simulated.
- The six rules are `RULES`: `LimitRule` at 100,000, 150,000, 200,000 and 250,000 tokens, `NeverRule()` and `NewSeatRule()`, printed as `limit 100,000` to `limit 250,000`, `never` and `new seat when kept is over 90,000 (estimate)`. `boundary_outcome(rule, fit, end, first_size, prompt) -> BoundaryOutcome` returns `CarriedOn(end + prompt)` when the earlier segment's end size is at or under the limit (always for `never`; the new-seat rule uses 100,000), else `Compacted(fit.kept(end))`, or for the new-seat rule `NewSeat(first_size)` when the kept size is over 90,000. `first_size` is the median size of a covered thread's first request. A carried segment's first request costs `end` at the cached weight, the prompt, and its stored output at the output weight; a compacted or new-seat one costs its start size and that output. Each later request costs its stored weighted tokens plus `max(difference, -cache_read)` at the cached weight, where `difference` is the simulated start less the stored first size, and the requests held at that floor are counted per rule. A thread's first segment costs what is stored under every rule. `fit_segments(threads) -> Fit` returns `FittedLine(intercept, slope)`, the least-squares line over every pair of neighboring segments (`before` is the earlier segment's last request size, `kept` the later one's first), or `FitUnavailable(reason)` with `no neighboring segments` or `all before sizes are equal`. `FittedLine.kept` is never under 0.
- Printed, in order: `Covered: <n> threads, <n> compactions, <n> requests.`; `Fit: kept = <a> + <b> × before.`; `Median first-request size: <n> tokens.`; `Replay excludes compaction-request cost and does not simulate Codex compaction of a full context.`; a table with the columns `rule`, `simulated compactions`, `new seats`, `weighted tokens`, `change against limit 100,000`, `requests over 272,000`; then `Stored weighted tokens: <x>; limit 100,000 replay: <y>.`, `Threads left out: no compaction=<n>; request with no weight entry=<n>.`, `Rows of size 0 skipped: <n>.`, and `Requests with simulated cache read held at 0:` with `none.` or one `- <rule>: <n>.` line per rule. With no fitted line, `fit unavailable: <reason>` stands where the fit line was, and the exclusion line, the table and the stored-tokens line are not printed. Both cases exit 0. A refused flag, date, window or prompt value exits 2, and a store that cannot be read or whose version is not `collect.STORE_VERSION` exits 1, each with one `compaction_replay: <message>` line on stderr.
- The limit stays. Read 2026-10-12 over a scratch store of natedev's logs: 336 threads, 1,535 compactions and 181,208 requests covered; fit `kept = 84,684.96 - 0.156363 × before`; median first request 25,270.

| rule | simulated compactions | weighted tokens | change against `limit 100,000` | requests over 272,000 |
| --- | --- | --- | --- | --- |
| `limit 100,000` | 1,470 | 3,057,687,520 | the base | 2,040 |
| `limit 150,000` | 1,257 | 3,077,483,094 | +0.6% | 3,284 |
| `limit 200,000` | 1,081 | 3,139,277,362 | +2.7% | 7,884 |
| `limit 250,000` | 584 | 4,113,698,149 | +34.5% | 59,368 |
| `never` | 0 | 17,596,918,875 | +475.5% | 130,424 |

- The `new seat when kept is over 90,000 (estimate)` row is the same as `limit 100,000`, with 0 new seats. Stored weighted tokens of the same threads: 2,904,658,499. Left out: 1,764 threads with no compaction and 328 with a request with no weight entry; 1,551 rows of size 0 skipped. The report's share by role and model: `gpt-6.1-sol` 4.4% (director) and 6.8% (unassigned); Claude director models 4.9% to 7.0%; `gpt-5.6-sol` 0.4% to 0.5%. No row is at least 5% under `limit 100,000` with no more requests over 272,000, so `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` stays 100000. `config/delegate.conf` and the five docs that state the default (`commands/unit/direct.md`, `commands/unit/delegate.md`, `docs/production_format.md`, `docs/delegate/write_prompt_contract.md`, `docs/as-built/long-lived-seats.md`) are unchanged.

**Files:**
- `scripts/usage/usage_report.py` — the `compaction_reread` outcome, the metric, `exact_where`, the unknown-unit row count.
- `scripts/usage/compaction_replay.py` — new: the replay.
- `scripts/usage/test_usage_report.py` — 41 tests, six of them new: the first request in text key order, the window and dimensions taken from the request, an unweighted first request, `--exact`, the unknown-unit row count, one count per request across threads.
- `scripts/usage/test_compaction_replay.py` — new, 9 tests on stores each test builds: one thread worked by hand under four rules, one read state and an unchanged store file, whole threads by date, both `fit unavailable` reasons, the left-out counts, the prompt flag, rows of size 0, compactions with no request.
- `scripts/usage/fixtures/report/expected/` — `list.md` and `unknown-metric.txt` name the metric; `compaction_reread_share.md` is new.

**State and consequence:**
- `QueryOutcome.exact_where`, a new field: the metric table produces it and `turn_relation` reads it under `--exact`. It makes the numerator follow the report's existing `--exact` rule, so no reader sees a new state. Test: `test_exact_reread_excludes_nonrecorded_first_requests_without_selecting_a_replacement`.
- `fit unavailable: <reason>`, a new outcome of the replay: `fit_segments` produces it, and whoever runs the replay sees the reason, no table and exit 0, and decides no limit from that run. Tests: `test_fit_without_neighboring_segments_has_a_reason_and_no_rule_table`, `test_fit_with_equal_before_sizes_has_a_reason_and_no_rule_table`. The replay's refusals, exit 2 and exit 1: whoever runs it sees the one stderr line. No test runs a refused replay; the `replay` helper in `test_compaction_replay.py` requires exit 0 and an empty stderr on every call.
- The counts under the table (threads left out by reason, rows of size 0, cache reads held at 0): diagnostics for the table's reader. Tests: `test_missing_weights_exclude_a_whole_thread_and_model_changes_are_covered`, `test_zero_size_rows_are_counted_only_for_covered_threads`, `test_rules_match_hand_arithmetic_and_count_cache_floor_and_new_seats`.

**Deviations:** the Spec had the numerator sum once per stored compaction. The code counts each first request once: a request after two compactions with no request between them was otherwise summed twice (9 of 5,497 on the scratch store). Test: `test_reread_counts_a_shared_successor_once_per_request_across_threads`.

**Binds later work:**
- "Weights for gpt-6-sol and gpt-6-astra" gives both models a weight entry, so the replay then covers the 328 threads it left out here. It is run again there over a scratch store and its table is recorded; `scripts.usage.test_compaction_replay` is in that gate. The replay and its tests name neither model, and the left-out count prints as `request with no weight entry=<n>`.
- `scripts/usage/fixtures/report/expected/compaction_reread_share.md` prints `gpt-6-sol` as an unweighted model, in the row `| unassigned | gpt-6-sol | — | unweighted | unweighted |` and in the `Unweighted:` table, so a new name for the unweighted example reaches that file too.

**Gotchas:**
- The replay leaves out a whole thread when any of its requests has no weight entry. A Codex thread with a compaction and no request is in neither left-out count, and its rows of size 0 are not in the skipped count.
- The replay does not cost the compaction request itself and does not simulate Codex compacting a full context. Its `limit 100,000` row was 5.3% over the stored tokens of the same threads. The fitted slope is negative, so the kept size never passes 84,684.96 and the new-seat rule never opens a seat on this store. The hand measurement that came before the replay, about 55,000 + 0.28 × the size before, is not what the store shows.
- The replay parses `--since` and `--until` in America/Los_Angeles, the report's default zone, and has no `--zone` flag.

**Ruled out:** a third printed left-out reason for a thread with a compaction and no request (one more printed line for a case with no cost effect); raising the limit (every higher limit used more weighted tokens and more requests over 272,000).

### Phase 14 — Weights for gpt-6-sol and gpt-6-astra · status: done

#### As-built

- `MODEL_WEIGHTS` in `scripts/usage/weights.py` has an entry for `gpt-6-sol` and one for `gpt-6-astra`, each `ModelWeights(<model>, datetime.min.replace(tzinfo=timezone.utc), 0.1, 1.25, 1.25, 5)`: cached input 0.1, both cache writes 1.25, output 5, uncached input 1, from the start of time. `weights_at` and `sql_weights` match a model to its entry by equal name, and to the `claude-*` entry by the prefix `claude-`; of the matching entries, the one with the latest `effective_from` at or before the turn's time is used. Turns of both models count in every weighted figure, and the replay covers their threads.
- The comment above the two entries reads `Above 272K input tokens, these weights omit 2x input and cache costs and 1.5x output costs.` The weights leave out the premium on a request over 272K input tokens.
- The fixtures' example of a model with no entry is the invented name `invented-unweighted-model`, the same in both source fixtures and in all 39 expected files that print it. No fixture names `gpt-6-sol` or `gpt-6-astra`.
- Read 2026-10-12 over a scratch store of natedev's logs (store version 8). The role report (`usage_report.py --db <store> --by role,model weighted`) lists neither model as unweighted; the only model in its `Unweighted:` table is `<synthetic>`, 458 turns. The seat readout (`seat_usage.py --db <store> --before 2026-10-03 2026-10-10 --after 2026-10-10 2026-10-13`) prints `Unweighted: none`. The replay (`python3 -m scripts.usage.compaction_replay --db <store>`) covered 664 threads, 2,001 compactions and 355,236 requests; fit `kept = 83,899.84 - 0.170678 × before`; median first request 27,080 tokens.

| rule | simulated compactions | weighted tokens | change against `limit 100,000` | requests over 272,000 |
| --- | --- | --- | --- | --- |
| `limit 100,000` | 1,928 | 5,777,829,425 | the base | 1,953 |
| `limit 150,000` | 1,707 | 5,798,934,432 | +0.4% | 3,234 |
| `limit 200,000` | 1,523 | 5,887,358,506 | +1.9% | 8,555 |
| `limit 250,000` | 683 | 8,066,593,866 | +39.6% | 111,029 |
| `never` | 0 | 23,242,221,181 | +302.3% | 210,404 |

- The new-seat estimate row equals `limit 100,000`, with 0 new seats. Stored weighted tokens of the same threads: 5,551,641,863. Left out: 1,764 threads with no compaction and 0 with a request with no weight entry; 2,023 rows of size 0 skipped. Requests with simulated cache read held at 0: 56, 62, 48 and 158 for the four limits. No rule is at least 5% under `limit 100,000`, so `PLAN_DELEGATE_COMPACT_ABOVE_TOKENS` stays 100000 and no phase changes the limit.

**Files:**
- `scripts/usage/weights.py` — the two entries and the comment.
- `scripts/usage/test_usage_report.py` — 44 tests, three of them new: `ModelWeightTests` holds `test_sol_and_astra_weights_cover_all_dates_and_token_kinds` and `test_sql_weights_agree_with_python_for_both_models_from_the_start_of_time`; `UsageReportTests` holds `test_new_model_turns_are_weighted_and_the_invented_model_stays_unweighted`.
- `scripts/usage/fixtures/day1_seats/` — `unweighted.jsonl` and seven expected files name the invented model.
- `scripts/usage/fixtures/report/` — `home/.codex/sessions/2026/10/10/rollout-unweighted.jsonl` and 32 files in `expected/` name the invented model.

**State and consequence:** none new. `UnweightedModel` and the `Unweighted:` outputs are unchanged and stay covered by the invented model: in the report by `test_new_model_turns_are_weighted_and_the_invented_model_stays_unweighted` and the expected files, in the seat readout by the `day1_seats` expected files that `scripts/whoami/test_seat_usage.py` compares in full.

**Deviations:** each expected file changes only in the example's name, except `scripts/usage/fixtures/report/expected/day-model.md`. That table is sorted by model name, so the renamed row also moves from above the `gpt-6.1-sol` row to below it. The Work Order was amended to allow it.

**Binds later work:**
- A new Codex model needs its own entry in `MODEL_WEIGHTS` before its turns count. Until then the report and the seat readout print it as unweighted, and the replay leaves out every thread that has a request of that model.
- The replay's table here is the one to compare a later limit proposal against. It covers the 328 threads that the table in "What seat compactions cost, and a limit chosen by replay" left out.

**Gotchas:**
- `seat_usage.py` refuses `--before START END` without `--after START END`: `--before and --after must be given together`.
- A weighted figure for a request over 272K input tokens is under its cost: the two entries leave out 2 times for input and cache and 1.5 times for output.
- `scripts/whoami/test_quota_alert.py` and `commands/agent.md` name `gpt-6-sol` for other reasons and need no change.

**Ruled out:** a higher limit (every higher limit used more weighted tokens and more requests over 272,000).

### Phase 15 — The compaction replay's refusals are tested · status: done

#### As-built

- `python3 -m scripts.usage.compaction_replay` refuses in two ways, and each refusal prints one stderr line `compaction_replay: <reason>`, prints nothing on stdout, and leaves the store file's bytes unchanged. `main` raises `usage_report.ReportRefused` for refused arguments (exit 2) and catches `OSError`, `sqlite3.Error` and `usage_report.StoreVersionMismatch` for a store it cannot use (exit 1). `usage_report.open_read_store` opens the store with `?mode=ro`, so a missing path stays missing.
- Exit 2, `invalid bounds; accepted: --since earlier than --until (inclusive start, exclusive end)`: `test_equal_bounds_are_refused_without_changing_the_store` and `test_reversed_bounds_are_refused_without_changing_the_store`.
- Exit 2, `follow-up prompt must be a finite nonnegative token count`: `test_negative_follow_up_prompt_is_refused_without_changing_the_store` (`-1`), `test_nan_follow_up_prompt_is_refused_without_changing_the_store` and `test_infinite_follow_up_prompt_is_refused_without_changing_the_store`.
- Exit 1, `store version 7 is older than report version 8; run the collector once`, built from `collect.STORE_VERSION`: `test_older_store_is_refused_without_changing_its_bytes`.
- Exit 1, `file is not a database`: `test_non_sqlite_file_is_refused_without_changing_its_bytes`. Exit 1, `unable to open database file`: `test_missing_store_is_refused_without_creating_a_file`, which also pins that no file exists at the path after the run.
- `run_replay` runs the replay with `--db` and a temporary `HOME` and `USAGE_STATE_DIR`, and returns the result. `replay` calls it and requires exit 0 and an empty stderr. `assert_replay_refused` pins the exit status, an empty stdout, and stderr equal to `compaction_replay: <reason>` plus one newline, one line only. `assert_store_preserved_on_refusal` calls it and pins that the store file's bytes are the same after the run; every refusal test but the missing-store test uses it.
- `scripts/usage/compaction_replay.py` did not change in this phase: every refusal already behaved so.

**Files:**
- `scripts/usage/test_compaction_replay.py` — 17 tests, eight of them new, and the four helpers.

**State and consequence:** none. The phase adds tests; no state, exit status or message changes.

**Deviations:** none.

**Binds later work:** a change to a refusal's exit status or text changes its test in the same commit. A new refusal gets its own test.

**Gotchas:**
- `file is not a database` and `unable to open database file` are SQLite's own error texts, so a SQLite release that rewords them fails those two tests.
- A refused replay over a store in WAL mode leaves SQLite's `-wal` and `-shm` files beside it. The tests compare the store file only.
- The older-store reason says `run the collector once`, while the collector refuses a store older than version 8 and tells the user to delete it and run the collector again to rebuild it.

**Ruled out:** a change to the replay (every refusal already behaved as specified).
