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

### Phase 5 — The Mac's turns are read on the Mac · status: todo

#### Work Order

**Goal:** A first read of the Mac's logs completes in minutes, a read that is cut off keeps what it finished, and a later run moves only what is new.

**Spec:**
- Measured 2026-10-12: the link from the Mac carries about 6 MB/s. The Mac holds 112 MB of Claude logs and 5.4 GB of Codex logs in 1,859 files (largest 754 MB, median 0.5 MB). Phase 4's pass sends every changed file whole, as base64 in JSON, inside one transaction: two first reads hit a 720 s limit and kept nothing. In the largest file the lines the parsers use are about 25 MB of 754 MB, and scanning it on the Mac takes 0.7 s.
- The file-reading half of `collect.py` runs on the Mac, and there is one parsing path: the local read and the Mac-side reader call the same functions. Today `collect_file` parses and saves in one loop, and `REMOTE_READER` is a separate script that only moves bytes; this phase separates reading a file into measurements from saving them, and `REMOTE_READER` goes away.
- natedev sends the Python the reader needs at run time: the source of `collect.py` and of the two modules it imports, `scripts/usage/agents_log.py` and `scripts/whoami/seat_usage.py` (standard library only, about 77 KB together), travels on the ssh command's stdin with the stored cursors. A short bootstrap given to `python3 -c` loads them under their own module names and runs the reader. Nothing is written to the Mac's disk, the Mac needs `python3` (3.13 there) and no checkout, and both machines always run the same parser.
- The stream from the Mac, in order: the inventory (the Claude session paths `known_sessions` needs, and each root's state); `agents.jsonl` and the run-history files as bytes, as today, because they are small and attribution needs them before any turn is saved; then, for each Claude and Codex file whose size differs from its stored cursor, one record per measurement (a turn or a compaction) followed by that file's new cursor. A file that grew is read from its start on the Mac, because the first edit and the Codex model and effort come from earlier lines, and only measurements at or past the stored cursor are sent, as `collect_file` does locally. A file smaller than its stored cursor is a replaced file: its cursor counts as zero and every measurement is sent, the same rule `collect_file` applies.
- Each record names its kind, so a later measurement kind is one more variant on both machines (the outcomes phase adds thread lifecycle). The record that crosses the link exists only at that boundary: natedev turns each one back into the same `Turn` or `Compaction` value before `save_measurement`, and a record it cannot read ends the pass as a failed read; natedev never fills in a value the Mac did not send.
- natedev alone stores, merges and attributes. The rows a Mac file produces go through the same save path as local rows, the one-row-per-message merge included, and that file's rows and cursor commit together. A pass cut off at any point keeps every file it finished, and the next pass reads the rest.
- On the Mac too, every session file is read before any `subagents/` file.
- The role log's snapshot (`role_log_sources`) and the re-attribution of the Mac's stored turns commit together, in a transaction of their own, once `agents.jsonl` and the history files have arrived and before the first turn file; turns saved later in the pass are attributed as they are saved. A pass cut later keeps that snapshot, and a pass in which no transcript changed still corrects attribution from new evidence.
- Which sources were read is stored, not only printed (from review 2026-10-12; the metrics report must show it): a new table `turn_sources(machine, family, state, read_at, last_complete_at)`, primary key (`machine`, `family`), written on every run for both machines and both families with the state that run prints (`ready`, `missing`, `unreadable`, `partial`, `skipped`, `unreachable`). `last_complete_at` moves only on `ready` and otherwise keeps its earlier value, so a Mac that is skipped or unreachable today still shows when it was last read in full. In Python the state is its own type; a bare `str` exists only in the SQL layer.
- The Mac's `agents.jsonl` and run-history files still arrive as bytes; they are small.
- Unchanged: the ssh options, `MAC_HOST`, the `rc=$?` last line, `--no-mac`, the printed line per source, and the recorded source states. An unreachable Mac, a remote Python error or a cut connection never fails the run. A pass that kept nothing prints `unreachable (<reason>)` for each source and records that state, as today. A pass cut after it kept at least one file prints, for Claude and for Codex, the turns it kept and `partial (<reason>)`; the role log's line and recorded state are what was received before the cut.
- For a given set of logs the store holds what a local read of the same files would hold: the same rows, keys, token classes, `before_first_edit`, model and effort.
- No test covers the Mac pass today; `test_no_mac_never_starts_ssh` shows how the tests put a `bin` directory first on `PATH` and set `HOME`.
- Done when a pass over a fixture Mac (a fake `ssh` first on `PATH` that runs the remote command locally from an empty directory outside the checkout, with `PYTHONPATH` unset and `HOME` set to a fixture home holding `.claude/projects`, `.codex/sessions` and `.local/state`, so a reader that leans on the checkout fails) stores exactly the rows a local read of the same fixtures stores; a pass cut after its first file keeps that file's rows and cursor, and the next pass adds the rest and nothing twice; a pass cut inside its second file, after a Claude record that merges into an earlier row, leaves no row, no merged count and no cursor from that file; a file replaced by a shorter one is read again from zero; a new ledger line with unchanged transcripts corrects the Mac's attribution; a cut pass keeps the role log snapshot it received; `turn_sources` holds the printed state for every machine and family after a full pass, a cut pass, an unreachable Mac and `--no-mac`, and `--no-mac` after a full pass keeps `last_complete_at`; a rerun over unchanged files receives no measurement records; and what is received for a file grows with its measurements, not its size (a fixture with one large line that is not a measurement).

**Files:**
- `scripts/usage/collect.py` — the Mac pass: the remote reader, the receiving side, the per-file commit.
- `scripts/usage/test_collect.py` — the Mac-pass tests.
- `scripts/usage/fixtures/collect/` — their fixtures.

**Seats:** `1 writer + 1 tester`.
- `impl` — `scripts/usage/collect.py`; hub: `collect.py`.
- `test` — `scripts/usage/test_collect.py` and `scripts/usage/fixtures/collect/`, from this Spec alone, driving the CLI with a fake `ssh` first on `PATH`; a cut pass is a fake `ssh` that stops its output at a chosen record (after the first file's cursor, and inside the second file) and exits without the `rc=` line.

**Constraints from prior phases:** A Claude message is one row per machine: a unique index on (`machine`, `turn_key`) for `claude` rows, and a repeat merges into the first row (the largest of each token class, the smallest `before_first_edit`; the first presenter keeps identity, time, model, effort and attribution). `save_measurement` is the one save path. The Codex helpers are imported from `scripts/whoami/seat_usage.py`, never copied. A changed file is re-read from its start. `state_dir()` is `USAGE_STATE_DIR` when set. Tests never write to the real `~/.local/state/usage-model/`, and seats never run a pass against the real Mac. No Pushover and no `send.py --to user`.

**Acceptance gate:** `python3 -m unittest scripts.usage.test_collect scripts.usage.test_agents_log` green; basedpyright 0/0/0 on `scripts/usage`, test files included. Live, by the unit director: a first read of the real Mac into a scratch database, with `USAGE_STATE_DIR` and `--db` in a temp directory and empty local roots, completes and is timed, and a second run adds only what the Mac wrote in between.

### Phase 6 — Outcomes in the same store · status: todo

- Collect checkpoints, phases, findings, review rounds and passes, and account readings, keyed by machine, run, phase and pass instance and source event, with an explicit checkpoint mapping, so usage can be divided by work done. Compactions come from Phase 4's events. Accounts come from `scripts/accounts/accounts.py` (accounts and role-change history), `scripts/whoami/agent_accounts.py`, and the readings `scripts/whoami/run_out.py` reads; all exist in this checkout, so nothing gates on another unit. A nullable quota or credit value from those modules becomes a semantic availability state (read, not reported, unreadable) before it enters a usage-owned type.
- Reruns are idempotent, and a fixture joining several findings or passes to one stretch of work does not multiply its usage.
- Thread lifecycle in the same store (from review 2026-10-12): `seat_usage.py` counts threads opened and whether a thread ever edited, and the store holds only turns and compactions. For every Codex thread and Claude session the store also holds when it opened and the time of each edit, read by Phase 5's one parsing path on both machines as further measurement kinds. Done when store-only fixtures give the right figures for a thread with no token event and for an edit after the last token event.
- Done when "weighted usage per checkpoint, by role and model, per day" is one query.
- **Seats:** `1 writer + 1 tester`. `impl` — outcome collection in `scripts/usage/collect.py` (hub). `test` — new `scripts/usage/test_outcomes.py` and its fixtures. Gate: `python3 -m unittest scripts.usage.test_outcomes scripts.usage.test_collect`; basedpyright 0/0/0 on `scripts/usage`, test files included.

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
- `interactive` and `subagent` are roles in their own rows; a subagent's usage is counted once, under the subagent, never folded into its parent.
- Done when the day 1 long-lived-seats readout (2026-10-11, split at the 21:35Z switch to gpt-6.1-sol) is reproduced from the input fixture `scripts/usage/fixtures/day1_seats/` to the expected output `scripts/usage/fixtures/day1_seats/expected.md` under weight version `gpt-6.1-sol@2026-10-10T21:35Z` (1 / 0.05 / 5), and a new metric takes one entry. Report fixtures also pin `interactive` and `subagent` rows, missing assignments, estimated attribution, unrecorded lenses, the skipped-line count and the exact-evidence filter.
- **Seats:** `1 writer + 1 tester`. `impl` — `scripts/usage/usage_report.py` and `scripts/whoami/seat_usage.py`. `test` — new `scripts/usage/test_usage_report.py`, `scripts/whoami/test_seat_usage.py`, and the report fixtures. Gate: `python3 -m unittest scripts.usage.test_usage_report scripts.whoami.test_seat_usage scripts.usage.test_collect`; basedpyright 0/0/0 on `scripts/usage scripts/whoami/seat_usage.py`, test files included.

### Phase 8 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh. Measured at Phase 4: a first import of natedev's logs takes about 5 minutes and a rerun about 11 seconds; the Mac is read only by Phase 5's pass.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Each source keeps its own last-success watermark (`last_complete_at` in Phase 5's `turn_sources`): an unreachable Mac leaves its rows stale and marked so in the daily readout (`seat_usage.py --daily`) and the dailies (`dailies_render.py`), while local sources keep updating; the backlog fills when it is back.
- Done when every reachable source is never more than an hour behind, with no agent involved, and a simulated Mac outage shows as partial coverage and then recovers.
- **Seats:** `1 writer + 1 tester`. `impl` — collector freshness and watermarks, both report adapters, and the timer-definition handoff to natedev. `test` — collection-outage tests, `scripts/whoami/test_seat_usage.py`, `scripts/production/test_dailies_render.py`. Gate: `python3 -m unittest scripts.usage.test_collect scripts.whoami.test_seat_usage scripts.production.test_dailies_render`; basedpyright 0/0/0 on every edited Python file, test files included.
