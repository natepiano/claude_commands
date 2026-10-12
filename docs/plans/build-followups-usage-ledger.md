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

### Phase 6 — Outcomes in the same store · status: todo

#### Work Order

**Goal:** The store holds what the work produced beside what it used (checkpoints, runs, phases, passes, findings, repair rounds, account readings) and when every thread opened and edited, so "weighted usage per checkpoint, by role and model, per day" is one query.

**Spec:**
- Run history. `history_root()/runs/<run id>.jsonl` holds one JSON object per line; every line has `event_id`, `event_type` and `timestamp`, and the run id is the file's stem. The collector reads these files through the `cursors` table like every other log (a changed file is read from its start and only lines at or past its cursor are saved; a line without a newline waits). It keeps these event types, one row per thing, updated by later events:
  - `run_started`, `run_finished` → `runs(machine, run_id, started_at, plan_doc, worktree, branch, state, finished_at, source_event)`, primary key (`machine`, `run_id`).
  - `phase_started`, `phase_finished` → `phases(machine, run_id, phase_instance_id, phase_id, title, started_at, state, finished_at, source_event)`, primary key (`machine`, `phase_instance_id`).
  - `pass_started`, `pass_finished` → `passes(machine, run_id, phase_instance_id, pass_instance_id, kind, task, slot, fix_pass, started_at, state, finished_at, source_event)`, primary key (`machine`, `pass_instance_id`); `kind` is the event's `pass_kind`, `task` its `called_task`, `slot` its `team_slot`.
  - `finding_opened`, `finding_verdict` → `findings(machine, run_id, phase_instance_id, finding_id, severity, caught_by, lens, opened_round, opened_at, state, state_at, source_event)`, primary key (`machine`, `phase_instance_id`, `finding_id`); `state` is `open` until a verdict, then that verdict's `resulting_state`.
  - `finding_batch_dispatched`, `finding_batch_landed`, `finding_batch_abandoned` → `repair_rounds(machine, run_id, phase_instance_id, round, dispatched_at, covered, state, state_at, source_event)`, primary key (`machine`, `phase_instance_id`, `round`); `covered` is how many findings the batch names; `state` is `dispatched`, `landed` or `abandoned`.
- Every other event type is skipped. For a run, phase or pass, `state` is `open` until its finish event and then that event's `status`; `finished_at` is NULL only while `state` is `open`. `source_event` is the `event_id` of the event that created the row. An event that updates a thing with no row is skipped. A field an event does not carry is NULL in SQL and a named state in Python, never a bare optional. A finding's title, file and detail are never stored; `source_event` finds them in the history file.
- Both machines. The Mac's run history is read on the Mac by the same reader and crosses the link as one record per kept event, inside that file's records and before its cursor. natedev decodes each one strictly (every field checked, times through `parse_timestamp`, counts through `remote_integer`); a record it cannot read ends the pass as a failed read with earlier files kept. The Mac's director evidence is unchanged.
- Run history is a source family, `history`, beside `claude` and `codex`: one row per machine in `turn_sources`, one printed line (`<machine> history: <n> outcomes added; <state>`), the same states.
- Checkpoints. A checkpoint is a commit whose subject starts `checkpoint(`, read with `git log --all --format=%H%x00%ct%x00%s` in each repository that `seat_usage.repositories(claude_repo, production_docs)` returns (new flags `--claude-repo DIR` and `--production-docs DIR`, defaults from `seat_usage.default_sources()`). Table `checkpoints(commit_id PRIMARY KEY, at, plan_slug, phase_id, mapping, machine, run_id, phase_instance_id)`. `at` is the committer time in UTC. A subject of the form `checkpoint(<plan-slug>): phase <id> — <title>` gives `plan_slug` and `phase_id`; any other `checkpoint(` subject is stored with both NULL. Commits are read on natedev only: one made on the Mac counts once natedev's repository has it.
- The checkpoint mapping is explicit and is recomputed for every checkpoint on every run. Rule: among `phases` rows on any machine whose run's `plan_doc` file name without `.md` equals `plan_slug`, whose `phase_id` equals the checkpoint's, whose `started_at` is at or before the commit, and whose `state` is `open` or whose `finished_at` is at or after the commit, take the one with the latest `started_at`. `mapping` is `mapped` and the three identity columns name that phase; with no such phase `mapping` is `unmapped` and the three are NULL.
- Account readings. `account_readings(machine, account, at, remaining_state, remaining, credits_state, credits)`, primary key (`machine`, `account`, `at`), from the readings file (`--readings FILE`, default `run_out.READINGS_LOG`), read with a cursor. Each of `remaining` and `credits` is stored with its availability: `not_reported` when the key is absent or null; `read`, with the number, when it is a finite number in range (`remaining` 0 to 100, `credits` 0 or more); `unreadable` otherwise. The number column is NULL unless its state is `read`. In Python the three states are three types. A line without a string `account` and a time `parse_timestamp` accepts is skipped.
- Account role changes. `account_role_changes(machine, at, role, label)`, primary key all four, from `accounts.roles_history_file()` (`--account-roles FILE`), read with a cursor.
- Nothing is imported from `scripts/whoami/agent_accounts.py`: it reports the live state that the readings file already records over time.
- Commits, readings and account role changes are natedev's own sources: read locally under machine `natedev`, never asked of the Mac. Each has a `turn_sources` row (families `checkpoints`, `readings`, `account_roles`) and a printed line like the others; an absent file is `missing`, and a repository `git` cannot read makes `checkpoints` `partial`.
- Thread lifecycle. `read_file` yields two more measurement kinds on both machines, beside `Turn` and `Compaction`:
  - an opening → `openings(machine, family, parent_id, agent_id, at)`, primary key the four identity columns. Codex: the time `seat_usage` reads as the thread's opening (`KnownThreadOpening`). Claude: the timestamp of the file's first record that has one. An agent whose opening is not known has no row.
  - an edit → `edits(machine, family, parent_id, agent_id, edit_key, at)`, primary key the first five. One row per Codex record `is_edit` accepts (its `edit_key` is the line's byte offset) and per Claude message `claude_edit` accepts (its `edit_key` is that message's turn key). A Claude edit is one row per machine, like a Claude turn: a repeated line or a copy in another file adds nothing, and the first row keeps its identity.
  - Over the link each is a `measurement` record with its own `kind`, decoded strictly by `remote_measurement`.
- A store written before this phase has cursors past lines whose openings and edits were never saved. The store carries a version (`PRAGMA user_version`). A store below this phase's version has its `cursors` rows deleted once, in the transaction that raises the version, so every file is read again from zero; the turn keys keep each turn single.
- Module boundary. The Mac runs `read_mac` from three sources sent on ssh stdin (`collect.py`, `agents_log.py`, `seat_usage.py`), so `collect.py` keeps importing only those two modules and the standard library at module level, and everything the Mac runs (run-history reading, openings, edits, their wire records) lives in `collect.py`. The sources only natedev has (commits, readings, account role changes) and the checkpoint mapping live in a new `scripts/usage/outcomes.py`, which `collect.py` imports inside the natedev-only call, with a comment giving this reason.
- `scripts/usage/weights.py` gains `register(db)`. It defines the SQL function `weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output)`, which returns what `weighted` returns, and NULL when no weight entry is in force. Weights stay applied on read and are never stored.
- Done when this one statement, on a connection after `weights.register(db)`, gives the right rows for the fixtures:

  ```sql
  WITH used AS (
      SELECT day, role, model, sum(tokens) AS weighted, sum(tokens IS NULL) AS unweighted_turns
      FROM (SELECT substr(at, 1, 10) AS day, coalesce(role, 'unassigned') AS role, model,
                   weighted_tokens(model, at, input, cache_write_5m, cache_write_1h, cache_read, output) AS tokens
            FROM turns)
      GROUP BY day, role, model
  ), landed AS (
      SELECT substr(at, 1, 10) AS day, count(*) AS checkpoints FROM checkpoints GROUP BY 1
  )
  SELECT used.day, used.role, used.model, used.weighted, used.unweighted_turns,
         coalesce(landed.checkpoints, 0) AS checkpoints,
         used.weighted / landed.checkpoints AS weighted_per_checkpoint
  FROM used LEFT JOIN landed ON landed.day = used.day
  ORDER BY used.day, used.role, used.model
  ```

- Also done when: a fixture day with several findings, passes and repair rounds beside its turns gives the same `weighted` as its turns alone, in this statement and in one that also counts findings closed per day (every count is summed to one row per day before it joins); a second run on unchanged sources adds no row to any table and changes none; store-only fixtures give the right figures for a thread with no token event (an opening, no turn) and for an edit after the last token event; a fixture Mac's runs, phases, passes, findings, repair rounds, openings and edits are stored under machine `mac` exactly as a local read of the same files stores them under its machine; a store from before this phase is read again once and ends with the same turns.

**Files:**
- `scripts/usage/collect.py` — hub: the schema, run-history outcomes, openings and edits, their wire records, source rows, the new flags.
- `scripts/usage/outcomes.py` — new: commits, readings, account role changes, the checkpoint mapping.
- `scripts/usage/weights.py` — `register`.
- `scripts/usage/test_outcomes.py` — new tests.
- `scripts/usage/fixtures/outcomes/` — their fixtures.
- `scripts/usage/test_collect.py` — expectations the store version, the new source lines or the new record kinds change.
- `scripts/usage/fixtures/collect/` — the fake ssh's record counts.

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/usage/collect.py`, `scripts/usage/outcomes.py`, `scripts/usage/weights.py`; hub: `collect.py`.
- `test` — `scripts/usage/test_outcomes.py`, `scripts/usage/fixtures/outcomes/`, `scripts/usage/test_collect.py`, `scripts/usage/fixtures/collect/`, from this Spec alone, driving the CLI. Every test gives the collector a fixture `HOME` and explicit paths (`--db`, `USAGE_STATE_DIR`, `PLAN_DELEGATE_HISTORY_DIR`, `--readings`, `--account-roles`, `--claude-repo`, `--production-docs`), so no real repository, readings file, history or store is read or written. Commit fixtures are small repositories the test creates in a temporary directory with set committer dates. Every fixture line is invented.

**Constraints from prior phases:** The Mac half runs from module sources sent on ssh stdin, standard library only. A new measurement kind is a `measurement` record whose `kind` `remote_measurement` accepts; a field it cannot read ends the pass. Every count passes `token_count` or `remote_integer` (0 to 2**63 − 1). Each file commits with its cursor in one transaction, and a cut pass keeps every file it finished. `scripts/usage/fixtures/collect/mac/fake_ssh.py` counts records by kind (`StreamKind`). A Claude message is one row per machine (unique index `claude_message`); session files are read before `subagents/` files. `history_root()` treats an empty `PLAN_DELEGATE_HISTORY_DIR` as unset. The collector exits 0 unless the database cannot be written, and an unreachable or cut Mac never fails the run. Source states are `SourceState`; `last_complete_at` moves only on `ready`. `compactions` already holds both machines' compaction events. `scripts/whoami/seat_usage.py` imports the standard library only. Measured 2026-10-12: the real Mac holds 5,522 Claude turns and 278,850 Codex turns, the link carried 0.4 MB/s, and reading every Mac file again costs about 10 minutes, once.

**Acceptance gate:** `python3 -m unittest scripts.usage.test_outcomes scripts.usage.test_collect scripts.usage.test_agents_log` green; basedpyright 0/0/0 on `scripts/usage`, test files included. Live, by the unit director: the new collector on the scratch store of the last live read, natedev and the real Mac. Every file is read again once, the turn count per machine and family is unchanged, every new table holds rows, a rerun adds nothing and takes seconds, and the statement above returns rows.

### Phase 7 — Metrics as named queries · status: todo

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
- `seat_usage.py` becomes a thin view over the store, and its tests keep passing. Phase 5 sends `seat_usage.py` to the Mac as source: `MAC_HOST`, `decode_rollout`, `token_usage`, `is_edit`, `is_compaction` and `parse_timestamp` stay in it, and the module keeps importing the standard library only, never the collector or the report.
- Every report shows which sources were read, from Phase 5's `turn_sources` and `role_log_sources`: each machine and family with its state and when it was last read in full. Report fixtures pin a Mac that is `partial`, one that is `unreachable`, and one that is `skipped` after an earlier full read.
- Every report shows attribution coverage, the estimated-turn share, unassigned usage, unrecorded review lenses and each assignment log's skipped-line count (Phase 4's diagnostic snapshot), and takes an exact-evidence-only filter.
- From Phase 4: a role log's problems count is shown only when that source's state is `ready`; a model with no weight entry (`gpt-6-sol`, `gpt-6-astra` and `<synthetic>` on 2026-10-12) is named as unweighted, never dropped or given a default; a director role taken from run history is `estimated`.
- From Phase 5: `partial` also means a folder could not be listed, on either machine, and a local root that cannot be listed is `unreadable`; `last_complete_at` moves only on `ready`. The reason a read failed is printed on the collector's source line and is not stored. The Mac has no `agents.jsonl` on 2026-10-12 (its role log reads `missing`), so Mac roles come from run-history evidence alone and are `estimated`.
- `interactive` and `subagent` are roles in their own rows; a subagent's usage is counted once, under the subagent, never folded into its parent.
- Done when the day 1 long-lived-seats readout (2026-10-11, split at the 21:35Z switch to gpt-6.1-sol) is reproduced from the input fixture `scripts/usage/fixtures/day1_seats/` to the expected output `scripts/usage/fixtures/day1_seats/expected.md` under weight version `gpt-6.1-sol@2026-10-10T21:35Z` (1 / 0.05 / 5), and a new metric takes one entry. Report fixtures also pin `interactive` and `subagent` rows, missing assignments, estimated attribution, unrecorded lenses, the skipped-line count and the exact-evidence filter.
- **Seats:** `1 writer + 1 tester`. `impl` — `scripts/usage/usage_report.py` and `scripts/whoami/seat_usage.py`. `test` — new `scripts/usage/test_usage_report.py`, `scripts/whoami/test_seat_usage.py`, and the report fixtures. Gate: `python3 -m unittest scripts.usage.test_usage_report scripts.whoami.test_seat_usage scripts.usage.test_collect`; basedpyright 0/0/0 on `scripts/usage scripts/whoami/seat_usage.py`, test files included.

### Phase 8 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh. Measured at Phase 4: a first import of natedev's logs takes about 5 minutes and a rerun about 11 seconds; the Mac is read only by Phase 5's pass.
- From Phase 5 (measured 2026-10-12 on the real Mac): the first read of the Mac takes about 10 minutes at 0.4 MB/s, the link has also run at 6 MB/s, and a pass with nothing new takes 9 seconds for both machines. A Mac that is off the network or cut mid-read exits 0, keeps every row and stores `unreachable` or `partial`; the timer treats that as a finished run, and its time limit allows the first read.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Each source keeps its own last-success watermark (`last_complete_at` in Phase 5's `turn_sources`): an unreachable Mac leaves its rows stale and marked so in the daily readout (`seat_usage.py --daily`) and the dailies (`dailies_render.py`), while local sources keep updating; the backlog fills when it is back.
- Done when every reachable source is never more than an hour behind, with no agent involved, and a simulated Mac outage shows as partial coverage and then recovers.
- **Seats:** `1 writer + 1 tester`. `impl` — collector freshness and watermarks, both report adapters, and the timer-definition handoff to natedev. `test` — collection-outage tests, `scripts/whoami/test_seat_usage.py`, `scripts/production/test_dailies_render.py`. Gate: `python3 -m unittest scripts.usage.test_collect scripts.whoami.test_seat_usage scripts.production.test_dailies_render`; basedpyright 0/0/0 on every edited Python file, test files included.
