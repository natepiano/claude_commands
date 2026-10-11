# Usage ledger: token use by role and model, kept over time

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Start confirmed by the user 2026-10-11 10:21 PDT, via natedev. One durable store, on natedev, of every model turn across Claude and Codex on both machines. Each turn records who it belonged to: showrunner, unit director, worker (impl, test, fix), reviewer (by lens), subagent or interactive session. It also records which model and effort produced it. Metrics are queries over that store, so a new one is a few lines and not a new script.

> **As-built disposition: create**

> **Production: build-followups** — unit `model-study-unit`; production doc `docs/plans/build-followups-production.md`

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
  - Codex seats (`role` and `lens` per dispatch in `mesh_roster.json`, and the `handoff` posts on the board);
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

### Phase 1 — Every agent the delegate tooling starts records its role · status: todo

- A shared writer, `scripts/usage/agents_log.py append --id <agent id> --role <role> [--lens <lens>]`, adds one line (id, role, lens, time) to `~/.local/state/usage-model/agents.jsonl`.
- Called by the code that assigns roles:
  - `prepare_session.sh` (unit director);
  - the showrunner start in `commands/showrunner/produce.md` (natedev reviews at merge);
  - `agent_bg.sh` (Claude worker);
  - `codex_mesh.py` on every dispatch (seat role and lens);
  - `review.sh` (reviewer and lens).
- A reader, `agents_log.py`'s `roles()`, returns each agent's assignments in time order with the looked-up fields (kind, machine, worktree, unit, production).
- Backfill from the 58 archived run directories and the live ones: mesh rosters, seats ledgers, board handoffs, review status files.
- Done when a live run's seats, director and reviewers all appear with correct roles, and the backfill places most archived Codex threads.

### Phase 2 — Every other Claude session records itself · status: todo

- A SessionStart hook records interactive sessions and subagents with role `interactive` (or `subagent`, with its parent), unless Phase 1 already recorded that id.
- This phase carries the `settings.json` change, and nothing else goes in its checkpoint: the showrunner's gate refuses settings diffs, and the user approves this one before main, so the other phases keep merging.
- Done when a new Claude session appears in `agents.jsonl` within a second of starting.

### Phase 3 — One store of every model turn · status: todo

- `scripts/usage/collect.py` reads Claude transcripts and Codex rollouts on both machines into `usage.db`, one row per model turn: time, machine, agent id, model, effort, the token classes, and whether it was a compaction or came before the agent's first edit.
- Each turn is matched to the role in force at its time.
- Runs incrementally.
- Done when a rerun adds nothing, and a day's totals match `seat_usage.py` and `api_cost.py` for the same window.

### Phase 4 — Outcomes in the same store · status: todo

- Collect checkpoints, phases, findings, review rounds and passes, compactions and account readings, keyed by unit, run and time, so usage can be divided by work done.
- Done when "weighted usage per checkpoint, by role and model, per day" is one query.

### Phase 5 — Metrics as named queries · status: todo

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
- Done when the day 1 long-lived-seats readout can be reproduced from the store, and a new metric takes one entry.

### Phase 6 — Kept up to date by itself · status: todo

- An hourly timer on natedev runs the collector, including the Mac's logs over ssh.
- If the timer needs a declared systemd unit, its text goes to natedev, who commits it in `/etc/nixos`; the rebuild is the user's.
- The daily efficacy update and the showrunner's dailies read from the store, never from raw logs.
- Done when the store is never more than an hour behind, with no agent involved.
