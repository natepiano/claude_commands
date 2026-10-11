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
- **The role is recorded when it starts, by the code that assigns it**, not inferred later. An append-only `agents.jsonl` on each machine, with one line per assignment holding only what is lost once a run ends: agent id (Claude session id or Codex thread id), role, lens, and assignment time. Everything else is looked up from the agent id when it is read: the kind (from the id's form), the machine (from the file holding it), the worktree from the transcript's `cwd`, and the unit and production from the production doc's Units table. This follows the user's rule of 2026-10-08, store only what a script cannot look up. A Codex seat that switches from tester to reviewer writes a second line, so each turn is matched to the role in force at its timestamp. The `usage.db` turn rows are measurements and are stored in full.
- **Weights are a dated table per model**, not constants. Usage is reported in each model's weighted tokens, and a window that crosses a model change splits there by itself. This replaces `SEAT_MODEL` and the fixed weights in `seat_usage.py` (the pending add-on "weight seat usage by each turn's model" is folded in here).
- **Accounts come from `scripts/whoami/accounts.py` and its quota readings** once two-accounts' 4a02a1c merges. No second copy is built.
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

### Phase 2 — Every other Claude session can record itself · status: todo

#### Work Order

**Goal:** a hook script, tested but not yet registered, that records every Claude session no delegate writer has claimed as `interactive`, and every subagent as `subagent`, exactly once per agent.

**Spec:**
- `scripts/usage/agents_log.py`:
  - `Role` gains `interactive` and `subagent`, in the `Literal` alias, the CLI's choices and the reader's validation together.
  - New `register_default(agent_id: str, role: Role, at: datetime) -> bool`: under the same exclusive `fcntl.flock` that `append` takes, reads the log and appends `{"id", "role", "at"}` only when no line already names this agent. A line names it when its id equals `agent_id`, or when its id is 8 hex characters and a prefix of `agent_id` (an `agent_bg.sh` worker). Returns whether it wrote. The read-check-write is one locked step, so two deliveries at once write one line. A malformed or truncated line is skipped (and does not block the write); the partial-tail rule of `append` applies.
  - CLI: `register --id <id> --role <role>`; usage errors exit 2, write failures print `agents_log: <reason>` and exit 0, as `append`.
- New `scripts/hooks/session-start-agents-log.py`: reads the hook payload (JSON) from stdin. When it carries a non-empty `agent_id`, registers that id as `subagent`; otherwise registers `session_id` as `interactive`. Parent session is not stored: a subagent's transcript sits at `<parent transcript stem>/subagents/agent-<agent_id>.jsonl` (`scripts/hooks/context_usage.py` `resolve_transcript`), so the collector derives it. Unparseable input, a missing id or any failure: one stderr line, exit 0, never blocks the session. Imports `agents_log` from `scripts/usage` by path, as `update_registration.py` does. Event-agnostic, so Phase 3 can register it for `SessionStart` and, after a live check, whichever event delivers a subagent's `agent_id`.
- Ordering is settled by `role_at`, not by the hook: hook first, then a delegate writer, gives `interactive` then the delegate role, and the delegate role governs from its line on; writer first means the hook writes nothing. Resume, compaction, clear and repeated delivery for an id already present write nothing.
- No `settings.json` change in this phase.

**Files:**
- `scripts/usage/agents_log.py` — two roles; `register_default`; `register` CLI.
- `scripts/hooks/session-start-agents-log.py` — new: the hook.
- `scripts/usage/test_agents_log.py` — `register_default` and the new roles.
- `scripts/usage/test_session_start_hook.py` — new: drives the hook as a subprocess with fixture payloads.

**Seats:** `1 writer + 1 tester`.
- `impl` — `agents_log.py` and the hook; hub: `scripts/usage/agents_log.py`.
- `test` — `test_agents_log.py` additions and `test_session_start_hook.py`, from this Spec alone: interactive and subagent payloads each write one line with the right role; hook then writer and writer then hook, read through `role_at`, each give the delegate role after the writer's line; a worker's 8-hex id suppresses the hook's line for the full uuid it prefixes, and an unrelated 8-hex id does not; repeated delivery (startup, resume, compact) writes one line; concurrent `register_default` from several processes writes one line; bad JSON and a missing id exit 0 with a stderr line and write nothing; the new roles are accepted by `append`, `show` and the reader; every test sets `USAGE_STATE_DIR` to a temp dir.

**Constraints from prior phases:** `agents.jsonl` lines are `{"id","role","at"}` plus optional `"lens"` and `"approximate": true`; `append` locks the open file and starts a new line after a truncated tail; the reader skips and counts malformed lines and never raises; worker ids are 8-hex prefixes of their transcript uuids (142/142 measured); recording never fails its caller (Phase 1 as built).

**Acceptance gate:** `python3 -m unittest scripts.usage.test_agents_log scripts.usage.test_session_start_hook scripts.agents.test_codex_mesh scripts.production.test_update_registration` green; basedpyright 0/0/0 on `scripts/usage scripts/hooks/session-start-agents-log.py`; `settings.json` unchanged. Live, with `USAGE_STATE_DIR` at a scratch dir: piping a SessionStart payload for this session's id writes one `interactive` line, a second pipe writes none, and the hook returns in under a second.

### Phase 3 — Every other Claude session records itself · status: todo

- The `settings.json` change only: register Phase 2's hook for `SessionStart`, and for the event that carries a subagent's `agent_id` once a live check shows which one does. The showrunner's gate refuses settings diffs, and the user approves this one before main, so the other phases keep merging.
- Done when a new Claude session and a new subagent each appear in `agents.jsonl` within a second of starting.

### Phase 4 — One store of every model turn · status: todo

- `scripts/usage/collect.py` reads Claude transcripts and Codex rollouts on both machines into `usage.db`, one row per model turn: time, machine, agent id, model, effort, the token classes, and whether it was a compaction or came before the agent's first edit.
- Each turn is matched to the role in force at its time, through `agents_log.assignments` and `role_at`, keeping their malformed-line diagnostics. Identity comes from the source (family and machine), never the id's form: a Claude worker matches its transcript uuid by a unique 8-hex prefix (an ambiguous prefix stays unresolved); a subagent is scoped to its parent; a historical director comes from the run's `main_agent.session_id` in `progress_history.py` events, never from `cwd` alone. Archived Codex turns with no evidence stay unassigned.
- Attribution is an explicit state per turn: recorded, estimated (from an `approximate` line) or unassigned; a review lens is known, unrecorded or not applicable. Nullable values stay at the storage boundary.
- Owns the per-turn model and effort parser, a dated weight table per model (raw token classes kept apart from weighting), and the normalized compaction events Phase 5 consumes.
- Runs incrementally with transactional cursors, handling an incomplete tail; a change to `agents.jsonl` re-attributes the affected turns even when transcripts are unchanged.
- Done when a rerun adds nothing; a late assignment corrects an earlier unassigned turn without duplicating it; checked-in Claude and Codex fixtures give their expected raw totals; and mixed-model, changed-effort and weight-date-boundary tests pass (from review 2026-10-11; replaces the `api_cost.py` comparison, which is not in this checkout).

### Phase 5 — Outcomes in the same store · status: todo

- Collect checkpoints, phases, findings, review rounds and passes, and account readings, keyed by machine, run, phase and pass instance and source event, with an explicit checkpoint mapping, so usage can be divided by work done. Compactions come from Phase 4's events. Accounts come from `accounts.py` after two-accounts 4a02a1c merges.
- Reruns are idempotent, and a fixture joining several findings or passes to one stretch of work does not multiply its usage.
- Done when "weighted usage per checkpoint, by role and model, per day" is one query.

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
- Every report shows attribution coverage, the estimated-turn share, unassigned usage and unrecorded review lenses, and takes an exact-evidence-only filter.
- Done when the day 1 long-lived-seats readout is reproduced from a named input fixture to a named expected output under a stated weight version, and a new metric takes one entry.

### Phase 7 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Each source keeps its own last-success watermark: an unreachable Mac leaves its rows stale and marked so in the daily readout (`seat_usage.py --daily`) and the dailies (`dailies_render.py`), while local sources keep updating; the backlog fills when it is back.
- Done when every reachable source is never more than an hour behind, with no agent involved, and a simulated Mac outage shows as partial coverage and then recovers.
