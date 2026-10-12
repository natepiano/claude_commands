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

### Phase 7 — Metrics as named queries · status: todo

#### Work Order

**Goal:** Any usage question is one named entry: `usage_report.py <metric> --by …` prints a Markdown table from the store, grouped by role, lens, model, effort, unit, machine and day, and says what its figures cover.

**Spec:**
- Folder. The store learns where each agent ran. `read_file` yields one more measurement kind on both machines → `folders(machine, family, parent_id, agent_id, folder)`, primary key the four identity columns. Claude: the `cwd` string of the agent's first record that has one; the first row is kept. Codex: the `cwd` of the session metadata record, following the latest metadata like the opening and written only when it differs. Over the link it is a `measurement` record with its own `kind`, decoded strictly by `remote_measurement`. `STORE_VERSION` becomes 7, so an older store has its cursors cleared once and every file is read again. The schema also gains `CREATE INDEX IF NOT EXISTS turns_at ON turns (at)`.
- Command. `python3 scripts/usage/usage_report.py [--db PATH] [--production-docs DIR] list` prints each metric's name and one-line description. `… <metric> [--by DIM[,DIM…]] [--since WHEN] [--until WHEN] [--zone NAME] [--exact]` prints one report. Defaults: the collector's store path, `seat_usage.default_sources().production_docs`, `--by role,model`, no bounds, zone `America/Los_Angeles` (the user's zone). `WHEN` is an ISO date or datetime, read in `--zone` when it names no offset; `--since` is inclusive and `--until` exclusive. The store is opened read-only; the report reads no log and writes nothing.
- Dimensions are one registry, one entry each (name → SQL expression): `day` (the date in `--zone`), `role` (`unassigned` when NULL), `lens` (the stored lens, else its `lens_state`), `model`, `effort` (`unreported` when NULL), `machine`, `unit`.
- `unit` is looked up when the report runs; the store holds only the folder. It is the `Unit` cell of a `## Units` table row, in any `*-production.md` under `--production-docs`, whose `Worktree` is the agent's folder or contains it (the longest worktree wins). With no such row it is the folder's last path component. An agent with no `folders` row has unit `unknown`.
- Metrics are one registry in `usage_report.py`, one entry each: name, one-line description, numerator, optional denominator, and format (`number`, `share`, `count`, `rate`). A numerator or denominator is either an SQL aggregate over the turn relation or a named outcome count. Adding a metric is adding one entry and nothing else.
- The turn relation has one row per stored turn inside the bounds (under `--exact`, only turns with `attribution = 'recorded'`): every dimension, the five token classes, `weighted` (`weighted_tokens(…)` after `weights.register(db)`, NULL with no weight entry), `output_weighted` (the same function given only the output class), `before_first_edit`, and `edited` (1 when the agent has an `edits` row).
- Outcome counts are one registry, one entry each (name → a SELECT of `at` and the dimensions it carries):
  - `checkpoints` — `checkpoints.at`; carries `day`, `machine`, `unit`. The unit comes from the mapped phase's run (`runs.worktree`); an unmapped checkpoint has machine `natedev` and unit `unknown`.
  - `findings_closed` — `findings` with `state = 'accepted'`, at `state_at`; carries `day`, `machine`, `unit` (its run's worktree).
  - `review_rounds` — `passes` with `kind = 'review'`, at `started_at`; carries `day`, `machine`, `unit`.
  - `compactions` — carries every dimension: `unit` from the agent's folder; `role`, `lens`, `model` and `effort` from the same agent's latest turn at or before it, else its earliest turn. A compaction of an agent with no turn is `unassigned`, lens `not_applicable`, model `unreported`.
  - `threads` — distinct agents with a turn in the group; an aggregate over the turn relation, so it carries every dimension.
  - `account_switches` — `account_role_changes` rows whose `label` differs from the previous row of the same `role`; the first row of a role is no switch; carries `day`, `machine`.
- Grouping. Each side of a metric is grouped by the requested dimensions it carries, and the two are joined on the dimensions both carry. An outcome count is therefore one row per group before it joins, and no usage multiplies. A metric with no turn side refuses a dimension it does not carry. Every refusal (unknown metric, dimension, zone or date; a dimension the metric cannot take) is one stderr line that names what is accepted, with exit 2.
- Initial metrics:
  - `weighted` — weighted tokens; its table also shows `turns`.
  - `weighted_per_checkpoint` — `weighted` over `checkpoints`.
  - `before_first_edit_share` — over turns of agents that edited: `weighted` where `before_first_edit = 1`, over `weighted`.
  - `cache_read_share` — `cache_read` over `input + cache_write_5m + cache_write_1h + cache_read`, raw tokens.
  - `output_share` — `output_weighted` over `weighted`.
  - `compactions_per_thread` — `compactions` over `threads`.
  - `weighted_per_finding_closed` — `weighted` over `findings_closed`.
  - `weighted_per_review_round` — `weighted` over `review_rounds`.
  - `account_switches` — the count.
- Output is Markdown, in this order: the line `<metric> — <description>`; one line with the bounds, the zone, and `exact evidence only` under `--exact`; the table (one column per dimension in `--by` order, then the numerator, the denominator when there is one, and the value; rows sorted by the dimension columns); `Coverage`; `Sources`. `number` has thousands separators and no decimals, `share` reads `12.3%`, `count` is an integer, `rate` has two decimals. A value whose denominator is zero or absent is `—`. A group with no weighted turn reads `unweighted`. With no turn inside the bounds the report prints its two header lines, `No turns in range.` and `Sources`.
- `Coverage` describes the same turns the table covers:
  - weighted usage and turns for each attribution (`recorded`, `estimated`, `unassigned`), each with its share;
  - `Unweighted:` each model with no weight entry and its turn count. These turns are in no weighted figure and are never given a default weight;
  - how many review turns have lens `unrecorded`;
  - for a metric with an outcome side, how many of its outcome rows have unit `unknown`.
- `Sources` has one row per `turn_sources` row (machine, family, state, `last_complete_at` in `--zone`, or `never`) and one per `role_log_sources` row (machine, `role log`, state, `read_at`). `skipped lines: <problems>` is shown only when the role log's state is `ready`. Every source whose state is not `ready` is listed first, under the line `Not read in full:`.
- A subagent's usage is counted once, under the subagent: `interactive` and `subagent` are roles with their own rows, never folded into a parent.
- Types follow the Type Design Contract: a metric, a dimension, an outcome, a format and each side of a metric are named types. No bare string or optional stands for one past the argument parser.
- Done when the report tests below pass and a metric added as one registry entry prints by name with no other change.

**Files:**
- `scripts/usage/usage_report.py` — new: the three registries, the report, the command line.
- `scripts/usage/collect.py` — hub: the folder kind, its wire record, the `folders` table, the index, version 7.
- `scripts/usage/test_usage_report.py` — new: the report tests.
- `scripts/usage/fixtures/report/` — new: invented logs, production docs and expected Markdown.
- `scripts/usage/test_collect.py`, `scripts/usage/test_outcomes.py`, `scripts/usage/fixtures/collect/`, `scripts/usage/fixtures/outcomes/` — the folder kind, version 7, the fake ssh's record counts.

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/usage/usage_report.py`, `scripts/usage/collect.py`; hub: `collect.py`.
- `test` — `scripts/usage/test_usage_report.py`, `scripts/usage/fixtures/report/`, and the collector tests and fixtures the folder kind changes, from this Spec alone. Every test drives the command line with explicit paths (`--db`, `--production-docs`, `USAGE_STATE_DIR`, a fixture `HOME`), so no real store, log or production doc is read. A report test builds its store by running the collector on invented logs (`--no-mac`, or the fake ssh for a Mac), then, only for a state the collector cannot be made to produce, inserts rows into that store. Expected reports are whole Markdown files compared exactly. The tests pin:
  - a window crossing 2026-10-10T21:35Z with `gpt-5.6-sol` turns before it and `gpt-6.1-sol` turns after, each weighted by its own entry (1 / 0.1 / 8 and 1 / 0.05 / 5), by day and by model;
  - a model with no weight entry, named under `Unweighted:` and in no sum;
  - `interactive` and `subagent` rows, with a subagent's usage counted once;
  - turns with no assignment, estimated attribution, an unrecorded lens, the skipped-line count, and `--exact`;
  - a Mac that is `partial`, one that is `unreachable`, and one that is `skipped` after an earlier full read;
  - every initial metric, with a day holding several checkpoints, findings, review passes and compactions beside its turns giving the same `weighted` as its turns alone;
  - `unit` from a production doc, from the folder name, and `unknown`; a folder inside a worktree; a Codex folder that follows its latest metadata;
  - each refusal, with its exit status and its stderr line;
  - a metric appended to the registry in the test and printed by name;
  - a fixture Mac's folders stored under machine `mac` exactly as a local read of the same files stores them; a store from before this phase read again once and ending with the same turns.

**Constraints from prior phases:** The store is SQLite, written only by `scripts/usage/collect.py`; `weights.register(db)` defines `weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output)`, NULL for a model with no weight entry (`gpt-6-sol`, `gpt-6-astra` and `<synthetic>` on 2026-10-12: 365,159 turns). Tables and columns are in the as-built blocks of the three collector phases above; `turns.attribution` is `recorded`, `estimated` or `unassigned`, and a director role taken from run history is always `estimated`; `lens_state` is `known`, `unrecorded` or `not_applicable`. `turn_sources` has one row per machine and family (`claude`, `codex`, `history` on both machines; `checkpoints`, `readings`, `account_roles` on natedev) with a `SourceState`; `last_complete_at` moves only on `ready`; `partial` also means a folder could not be listed. `role_log_sources.problems` describes the log only when its state is `ready`. The reason a read failed is printed by the collector and is not stored. The Mac has no `agents.jsonl` on 2026-10-12, so its roles are `estimated` from run history. The Mac half runs from three module sources sent on ssh stdin (`collect.py`, `agents_log.py`, `seat_usage.py`), standard library only: everything the Mac runs lives in `collect.py`, a new measurement kind is one more `kind` in `measurement_record` and `remote_measurement`, and `scripts/usage/fixtures/collect/mac/fake_ssh.py` counts records by kind. `usage_report.py` runs on natedev only and may import `collect`, `weights` and `seat_usage`; nothing imports it. Pass kinds in run history are `review`, `fix`, `impl` and `test`; a finding verdict's state is `accepted` or `open`. Measured 2026-10-12: natedev holds 407,342 Claude and 502,912 Codex turns, the Mac 5,522 and 278,850; 267 checkpoints, 90 unmapped; a full read of natedev takes 320 s and of the Mac about 10 minutes.

**Acceptance gate:** `python3 -m unittest scripts.usage.test_usage_report scripts.usage.test_collect scripts.usage.test_outcomes scripts.usage.test_agents_log` green; basedpyright 0/0/0 on `scripts/usage`, test files included. Live, by the unit director: the collector, then every metric, on a scratch store of natedev's real logs and of the Mac when it answers. Each report prints in under 10 seconds on the full store, `weighted` summed over all its rows equals one `sum(weighted_tokens(…))` over `turns`, and `weighted --by day,model` for 2026-10-10 through 2026-10-12 shows the switch to `gpt-6.1-sol` at 21:35Z.

### Phase 8 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh. Measured at Phase 4: a first import of natedev's logs takes about 5 minutes and a rerun about 11 seconds; the Mac is read only by Phase 5's pass.
- From Phase 5 (measured 2026-10-12 on the real Mac): the first read of the Mac takes about 10 minutes at 0.4 MB/s, the link has also run at 6 MB/s, and a pass with nothing new takes 9 seconds for both machines. A Mac that is off the network or cut mid-read exits 0, keeps every row and stores `unreachable` or `partial`; the timer treats that as a finished run, and its time limit allows the first read.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Each source keeps its own last-success watermark (`last_complete_at` in Phase 5's `turn_sources`): an unreachable Mac leaves its rows stale and marked so in the daily readout (`seat_usage.py --daily`) and the dailies (`dailies_render.py`), while local sources keep updating; the backlog fills when it is back.
- Done when every reachable source is never more than an hour behind, with no agent involved, and a simulated Mac outage shows as partial coverage and then recovers.
- From Phase 6 (measured 2026-10-12): with run history, commits, readings, openings and edits in the pass, a first full read of natedev's logs takes 320 seconds and a pass with little new takes 43 to 50 seconds. `checkpoints`, `readings` and `account_roles` are natedev's own sources, each with its `turn_sources` row and watermark; a worktree a production doc names that no longer exists is skipped and leaves `checkpoints` `ready`. The Mac went off the tailnet during a live read: the collector exited 0, kept every row and stored `unreachable`, but the reason it printed was the pipe error (`[Errno 32] Broken pipe`). When ssh cannot connect, the source line shows ssh's own error text (`read_mac` in `scripts/usage/collect.py`), with a test through the fake ssh.
- `seat_usage.py` becomes a thin view over the store, and its tests keep passing; moved here from the report phase by the unit director, 2026-10-12, so both readers of the store (this readout and the dailies) change together. Phase 5 sends `seat_usage.py` to the Mac as source: `MAC_HOST`, `decode_rollout`, `token_usage`, `is_edit`, `is_compaction` and `parse_timestamp` stay in it, and at module level it keeps importing the standard library only, never the collector or the report.
- Also done when the day 1 long-lived-seats readout (2026-10-11, split at the 21:35Z switch to gpt-6.1-sol) is reproduced from the store under weight version `gpt-6.1-sol@2026-10-10T21:35Z` (1 / 0.05 / 5): from an invented fixture `scripts/usage/fixtures/day1_seats/` to its `expected.md` in the tests, and live against the update sent that day.
- **Seats:** `1 writer + 1 tester`. `impl` — collector freshness and watermarks, both report adapters, and the timer-definition handoff to natedev. `test` — collection-outage tests, `scripts/whoami/test_seat_usage.py`, `scripts/production/test_dailies_render.py`. Gate: `python3 -m unittest scripts.usage.test_collect scripts.whoami.test_seat_usage scripts.production.test_dailies_render`; basedpyright 0/0/0 on every edited Python file, test files included.
