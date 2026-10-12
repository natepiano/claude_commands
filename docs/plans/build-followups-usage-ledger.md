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

### Phase 8 — Kept up to date by itself · status: todo

#### Work Order

**Goal:** The store stays current with no agent involved: an hourly timer on natedev runs the collector for both machines, and the long-lived-seats readout (`seat_usage.py`) is a view over the store that weights each turn by its own model and says how fresh each source is.

**Spec:**
- One pass at a time. `collect.py`'s `main` takes an exclusive, non-blocking `fcntl.flock` on `<store path>.lock` before it opens the store. A pass that finds the lock held prints one line (`usage collect: another pass is running`) and exits 0 with the store untouched. The lock ends with the process, also when it is killed. The Mac half (`read_mac`) opens no store and takes no lock.
- ssh's own words. `collect_mac` already sends ssh's stderr to a temporary file and never reads it. When the Mac read fails, the reason printed for the Mac's sources is the first non-empty line of that stderr, and the caught error's text only when ssh wrote nothing. The stored state does not change.
- Outage and recovery are built and tested (`test_unreachable_mac_keeps_prior_rows_diagnostics_and_complete_times`, `test_cut_after_first_cursor_keeps_file_and_resume_adds_only_remaining_turns`, `test_mac_failures_before_any_file_record_unreachable_and_leave_no_turns` in `test_collect.py`): keep them green and change none of that behaviour. New here is the same outage seen through the readout.
- The readings from before go-live enter the store. `collect.py` gains `--baseline-readings PATH` (default: `seat_usage.default_sources().baseline_readings`); `outcomes.collect` reads it into `account_readings` as its own source family `baseline_readings`, with its own line and its own `turn_sources` row. A live reading replaces a baseline reading with the same machine, account and time, whichever file was read first; a baseline reading never replaces a stored row. No table or column is added.
- The seat readout reads the store. `python3 scripts/whoami/seat_usage.py [--db PATH] (--daily [--t-live WHEN] | --before START END --after START END)` opens the store read-only (default: the collector's store path) and reads no rollout, no ssh, no git and no readings file. It runs as a script from any directory with `PYTHONPATH` unset: the store view puts the repository root on `sys.path` inside the function, before it imports from `scripts.usage`.
- Its columns are today's, each from stored rows of Codex threads on both machines (`family = 'codex'`; a thread is one `machine, parent_id, agent_id`, so the same id on two machines is two threads):
  - threads opened: `openings` inside the piece;
  - active threads: threads with an opening, a turn, an edit or a compaction inside the piece; a thread with none of its turns there still counts;
  - weighted usage: the piece's turns;
  - before first edit: over threads with any edit, the weighted usage of their turns before the thread's first edit, as a share of those threads' weighted usage in the piece;
  - compactions, and compactions per active thread;
  - checkpointed phases: `checkpoints` inside the piece;
  - account switches per week and credits per day: the existing `switches_per_week` and `credits_per_day`, fed from `account_readings` rows whose account label starts `codex ` in any case.
- Weights by model and date, as grouped SQL. Token classes are summed per weight entry (`weights.sql_weights(model, at)` gives the entry in force as SQL), and each sum is multiplied by its entry's weights once. The readout never calls `weights.register`, and no Python function runs per turn. The fixed `SEAT_MODEL`, `CACHED_WEIGHT`, `OUTPUT_WEIGHT` and `PRICE_BASIS_DATE` go. A turn whose model has no weight entry is in no weighted figure; one `Unweighted:` line names each such model and its turn count over the printed windows.
- A window splits by itself. Each window (`Before`, `After`) is cut at each distinct instant strictly inside it at which a dated weight entry starts for a model that has Codex turns in that window. Two entries starting at the same instant make one cut; an entry in force from the beginning (`effective_from` is `datetime.min`) makes none. Each piece is one table row with every column computed for that piece alone. A window with no cut keeps today's label; a cut window's rows are labelled with the piece's start in UTC (`After, from 2026-10-10 18:17Z`, `After, from 2026-10-10 21:35Z`).
- The `Weighting:` line above the table names every weight version that weighted a turn in the printed rows, ordered by model and start: a dated one as `gpt-6.1-sol@2026-10-10T21:35Z: 1 / 0.05 / 5`, one in force from the beginning as `gpt-5.6-sol: 1 / 0.1 / 8`.
- The verdict line (`Weighted usage per checkpointed phase: before …; after …; <change>`) compares the whole `Before` window with the last piece of `After`. A side with no checkpointed phase reads `unavailable`, as today. Notes follow in one parenthesis, in this order, joined by `; `: `different weights: compare roughly` when the set of weight versions that weighted `Before`'s turns differs from the set for that last piece; `unweighted turns left out: before <n> of <N>, after <n> of <N>` when either side has a Codex turn with no weight entry; `Mac as of <last complete read, UTC>` when the Mac's Codex source is not `ready`, or `Mac never read in full` when it has no complete read.
- Freshness. Below the table the readout prints the `Sources` block from `usage_report.sources(db, zone)` with UTC as the zone: one rendering, called by both, never a copy. Every source not `ready` comes first under `Not read in full:` (`partial`, `unreachable`, `skipped`, `missing`, `unreadable`), with its last complete read or `never`. When the Mac's Codex source is not `ready`, the figures still include every stored Mac row. Today's `Mac: unreachable` line and its `unavailable` verdict go.
- Module shape. The collector sends `seat_usage.py` to the Mac as source, so at module level it imports the standard library only; the store view imports `weights` and `usage_report` inside the function that needs them. `MAC_HOST`, `decode_rollout`, `token_usage`, `is_edit`, `is_compaction`, `parse_timestamp`, `ThreadRolloutBuilder`, `KnownThreadOpening`, `default_sources`, `production_paths`, `repositories`, `git_text` and `cell_value` stay: the collector, the outcomes pass and the report import them. Code only the old readout used goes with its tests (`local_rollouts`, `mac_rollouts`, `remote_rollout_command`, `mac_failure_reason`, `checkpoint_count`, `merged_readings`, `reading_records`, and any type nothing else imports); search for each name's users before removing it.
- Timer. The writer leaves the text of a NixOS module at `${SESSION_DIR}/usage_collect_timer.nix`, in the form of `/etc/nixos/modules/linux/agent-sessions.nix` (read it; edit nothing under `/etc/nixos`): `systemd.user.services.usage-collect` with `Type = "oneshot"`, `TimeoutStartSec = "45min"` so the first read of the Mac fits, no `Restart`, and an `ExecStart` wrapper that puts `pkgs.openssh` and `pkgs.git` on `PATH` and runs `${pkgs.python3}/bin/python3` on `/home/${username}/.claude/scripts/usage/collect.py`; `systemd.user.timers.usage-collect` with `OnCalendar = "hourly"`, `Persistent = true` and `wantedBy = [ "timers.target" ]`. Output goes to the journal only. The unit director sends the text to natedev, who commits it; the rebuild is the user's. No Pushover and no message from the collector.
- Report cleanup. In `usage_report.py`, `Outcome` becomes two named kinds: an outcome read from its own query (`at` and the dimensions it carries) and an outcome counted over the turn groups (`threads`). The `over_turns` flag and the query string nothing reads go. Every report prints exactly as before.
- Types follow the Type Design Contract: a window piece, a weight version (dated, or in force from the beginning) and a source's freshness (read in full at a time, not in full since a time, never read in full) are named types. No bare string, boolean or optional stands for one past the argument parser.
- Done when the tests below pass, a second pass started beside a running one exits at once, and the day 1 readout is reproduced from the store.

**Files:**
- `scripts/usage/collect.py` — hub: the pass lock, ssh's error text, `--baseline-readings`.
- `scripts/usage/outcomes.py` — the baseline readings source and which reading wins.
- `scripts/whoami/seat_usage.py` — the readout as a view over the store; the old log readers removed.
- `scripts/usage/usage_report.py` — the two outcome kinds; the `Sources` rendering the readout calls.
- `scripts/whoami/test_seat_usage.py` — the readout tests.
- `scripts/usage/fixtures/day1_seats/` — new: invented logs and `expected.md`.
- `scripts/usage/test_collect.py`, `scripts/usage/fixtures/collect/mac/fake_ssh.py` — the lock, ssh's error text.
- `scripts/usage/test_outcomes.py` — the baseline readings.
- `scripts/usage/test_usage_report.py` — only where the outcome split needs it; every expected report stays byte-identical.

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/usage/collect.py`, `scripts/usage/outcomes.py`, `scripts/whoami/seat_usage.py`, `scripts/usage/usage_report.py`, the timer module text; hub: `collect.py`.
- `test` — `scripts/whoami/test_seat_usage.py`, `scripts/usage/fixtures/day1_seats/`, `scripts/usage/test_collect.py`, `scripts/usage/test_outcomes.py`, `scripts/usage/test_usage_report.py`, the fake ssh, from this Spec alone. Every collector run in a test names every source (`--db`, `--claude-root`, `--codex-root`, `--agents-log`, `--readings`, `--baseline-readings`, `--account-roles`, `--claude-repo`, `--production-docs`) and either passes `--no-mac` or puts the fixture ssh first on `PATH`: a fixture `HOME` alone does not stop the collector from reaching for the real Mac. A readout test builds its store by running the collector on invented logs. The tests pin:
  - the day 1 shape: a `Before` week and an `After` window that crosses 2026-10-10T21:35Z, with `gpt-5.6-sol` turns before it (1 / 0.1 / 8) and `gpt-6.1-sol` turns after (1 / 0.05 / 5), giving two `After` rows and a whole readout equal to hand-computed values in `expected.md`;
  - a model with no weight entry on the `Unweighted:` line and in no sum;
  - with weight entries the test installs: two cuts in one window, two entries starting at the same instant (one cut), and a last `After` piece with no checkpoint (`unavailable`);
  - a thread with an opening, an edit or a compaction in the piece and no turn there counted as active, and one thread id on both machines counted twice;
  - the before-first-edit share over edited threads only, checkpoints, switches per week and credits per day, each from stored rows;
  - a reading that exists only in the baseline file counted, and a live reading winning over a baseline one with the same account and time in both read orders;
  - the Mac `unreachable`, `partial`, `skipped` and never read in full: listed under `Not read in full:`, stored Mac rows still counted, the verdict's note right in each case, the unweighted note with its counts, and all three notes together in the stated order; then recovery, with each turn held once;
  - the lock: a pass started while another holds it exits 0, prints its one line and changes nothing;
  - ssh's own error text as the printed reason when ssh cannot connect;
  - no Python function called from SQL in a number that grows with the turn count, counted as `test_python_function_calls_depend_on_groups_folders_and_compactions_not_turn_count` counts it for the report;
  - `seat_usage.py` imported with only the standard library on the path; the readout run as a script from another directory with `PYTHONPATH` unset; the kept rollout tests still passing.

**Constraints from prior phases:** The store is SQLite at `collect.store_path()` (under `USAGE_STATE_DIR` when set), written only by `scripts/usage/collect.py`; `STORE_VERSION` is 7 and this phase adds no table or column. Its tables are `turns`, `compactions`, `openings`, `edits`, `folders`, `checkpoints`, `account_readings`, `account_role_changes`, `turn_sources`, `role_log_sources` and run history (`runs`, `phases`, `passes`, `findings`, `repair_rounds`); columns are in the as-built blocks above. `account_readings` has `machine, account, at, remaining_state, remaining, credits_state, credits` with key `(machine, account, at)`; every row is natedev's, and `outcomes.save_reading` inserts with `INSERT OR IGNORE` today. Every stored time is UTC text ending `+00:00`: the report's day arithmetic and the dated weights compare that text, so nothing may store another form. `weights.MODEL_WEIGHTS` holds the dated entries and `weights.sql_weights(model, at)` gives them as SQL; `weights.register(db)` defines `weighted_tokens(…)` as a Python function SQLite calls once per turn, which took minutes on natedev's 1.2 million turns, while one grouped scan takes 2.3 to 2.8 s. So anything per turn is SQL. On natedev the `Before` week of day 1 holds 143,767 `gpt-5.6-sol` turns and 131,665 turns of models with no weight entry (`gpt-6-sol` 129,422, `gpt-6-astra` 2,243), so until those models have entries its weighted figure covers about half its turns; the old readout gave every Codex turn `gpt-5.6-sol`'s weights. `usage_report.py` has three side kinds (`TurnSum`, `TurnAggregate`, `OutcomeCount`), `report_data(db, request)`, `render_report(db, request)` and `sources(db, zone)`, which prints `never` for a source with no complete read; it already puts the repository root on `sys.path` when run as a script. `turn_sources` has one row per machine and family with a `SourceState`; `last_complete_at` moves only on `ready`. The Mac half runs from three module sources sent on ssh stdin (`collect.py`, `agents_log.py`, `seat_usage.py`), standard library only; ssh's exit status is always 0 there, so each remote command prints `rc=$?`. The Mac is reached over the tailnet with no ssh agent. Measured 2026-10-12: a full read of natedev's logs takes 178 to 320 s and a pass with little new 21 to 50 s; the first read of the Mac takes about 10 minutes at 0.4 MB/s, and a pass with nothing new 9 s for both machines. During a live read the Mac went off the tailnet: the collector exited 0, kept every row and stored `unreachable`, but printed the pipe error (`[Errno 32] Broken pipe`) as the reason. On natedev a user timer runs only while the user is logged in (lingering is off), and the user manager's environment carries what ssh and git need except `PATH`. The showrunner's dailies (`scripts/production/dailies_render.py`) show no token figure and read only the live account readings, so this phase does not touch them. No Pushover, no `send.py --to user`, and tests never write to the real `~/.local/state/usage-model/`.

**Acceptance gate:** `python3 -m unittest scripts.whoami.test_seat_usage scripts.usage.test_collect scripts.usage.test_usage_report scripts.usage.test_outcomes scripts.usage.test_agents_log` green; basedpyright 0/0/0 on `scripts/usage scripts/whoami/seat_usage.py scripts/whoami/test_seat_usage.py`. Live, by the unit director, on a scratch store of natedev's real logs and the Mac rows already read: a second pass started beside a running one exits at once; `seat_usage.py --db <scratch store> --before 2026-10-03T18:17Z 2026-10-10T18:17Z --after 2026-10-10T18:17Z 2026-10-11T15:38Z` against the entry `Long-lived seats, day 1 (2026-10-11 15:38Z)` in `~/.local/state/usage-model/NOTES.md` (its per-day figures times its day counts): the same threads, compactions and checkpoints, and a weighted figure that covers only the turns with a weight entry, with the rest counted on the `Unweighted:` line and in the verdict's note; the timer text sent to natedev. The hourly run is checked after the user's rebuild: with no agent involved, no reachable source's last complete read is older than one hour plus the length of one pass.
