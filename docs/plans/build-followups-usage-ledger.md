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
  - `scripts/whoami/seat_usage.py` — today's Codex rollout parser (reused in Phase 3).
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

### Phase 1 — Every agent the delegate tooling starts records its role · status: done

#### Work Order

**Goal:** every unit director, showrunner, Claude worker and Codex seat (with its role and lens on every dispatch) appends one line to `agents.jsonl` when its role is assigned, and the 58 archived runs plus the live ones are backfilled.

**Spec:**
- New `scripts/usage/agents_log.py`:
  - `state_dir() -> Path`: `USAGE_STATE_DIR` when set, else `~/.local/state/usage-model`; the log is `<state_dir>/agents.jsonl`.
  - `append(agent_id: str, role: Role, lens: Lens | None, at: datetime, approximate: bool = False) -> None`: one JSON line `{"id", "role", "at"}` plus `"lens"` only when given and `"approximate": true` only for backfill. `at` is ISO-8601 UTC with seconds. Writes under an exclusive `fcntl.flock` on the open file, so concurrent appends never interleave.
  - `Role` is a closed set: `showrunner`, `director`, `impl`, `test`, `fix`, `review`, `worker`. `Lens`: `adversary`, `contract`, `craft`, `ux`. Model each as a `Literal` alias validated at the CLI boundary; an unknown value is a usage error (exit 2).
  - `assignments(path) -> dict[str, tuple[Assignment, ...]]`, `Assignment(at, role, lens, approximate)` frozen dataclass, each tuple sorted by `at`; malformed lines are skipped and counted in a returned `problems` tuple (return a `RoleLog(assignments, problems)`).
  - `role_at(log: RoleLog, agent_id: str, at: datetime) -> Assignment | Unassigned`: the latest assignment at or before `at`; `Unassigned()` frozen dataclass otherwise.
  - CLI: `append --id <id> --role <role> [--lens <lens>]` (time is now); `backfill [--archive DIR] [--live DIR]`; `show [--id <id>]` prints the assignments as a Markdown table.
  - On any write failure the CLI prints `agents_log: <reason>` to stderr and exits 0, so no launcher fails because of it.
- `backfill` reads each run directory under `~/.local/state/usage-model/delegate-archive/` and `/tmp/claude/delegate/` (skipping `active/`):
  - `mesh_roster.json`: each seat's `thread_id` with its `role` (and `previous_role` as an earlier assignment when present), stamped with the roster file's mtime, `approximate: true`;
  - the `seats` ledger (`<session id>\t<name>`): role `worker`, stamped with the ledger's mtime, `approximate: true`;
  - the run's director: the active marker naming this run directory when present, else nothing (the director is recovered in Phase 3 from transcripts' `cwd`);
  - review lens: the newest `review_agent_[<N>_]<lens>.log` naming the seat gives its lens; with no such log, no lens (never one inferred from the seat name).
  Backfill writes nothing for an id that already has an exact (non-approximate) line, and is idempotent: a line already present for the same (id, role, lens) is not written again. A bad roster or ledger loses only its own rows; an append after a truncated tail starts a new line.
  - A claim released back to its previous role records that role again.
- Writers, each one line, each `|| true` in shell (or caught in Python) so a failure never stops the caller:
  - `prepare_session.sh`: `agents_log.py append --id "$CLAUDE_CODE_SESSION_ID" --role director` when the id is set.
  - `update_registration.py register`: `append(<CLAUDE_CODE_SESSION_ID>, "showrunner", None, now)`; import from `scripts/usage/agents_log.py`.
  - `agent_bg.sh`: after it appends to the `seats` ledger, `append --id <session id> --role worker`.
  - `codex_mesh.py`: on a new thread and on every follow-up claim, `append(thread_id, role, lens, now)` when the role is assigned. Add an optional `--lens` argument to the dispatch subcommands; it is written only to `agents.jsonl`, never to `mesh_roster.json`.
  - `review.sh`: pass its lens to `codex_mesh.py` with `--lens` when it has one.
- Out of scope: a review outside a run (`codex exec` through `agent_exec.sh`) records nothing; its thread reads as unassigned.

**Files:**
- `scripts/usage/__init__.py` — new, empty.
- `scripts/usage/agents_log.py` — new: writer, reader, backfill, CLI.
- `scripts/usage/test_agents_log.py` — new.
- `scripts/delegate/prepare_session.sh` — director line.
- `scripts/production/update_registration.py` — showrunner line (natedev's file; reviewed at merge).
- `scripts/production/test_update_registration.py` — the copied command carries `scripts/usage`; registration records the showrunner.
- `scripts/agents/agent_bg.sh` — worker line.
- `scripts/agents/codex_mesh.py` — seat line on dispatch; `--lens`.
- `scripts/agents/test_codex_mesh.py` — dispatch writes the line with role and lens; follow-up writes a second line; no roster `lens` key.
- `scripts/delegate/review.sh` — passes `--lens`.

**Seats:** `1 writer + 1 tester` — the writers' edits are one line each beside the new module, so nothing splits.
- `impl` — `agents_log.py`, `__init__.py`, the five writer sites; hub: `scripts/usage/agents_log.py` (every caller imports or runs it).
- `test` — `scripts/usage/test_agents_log.py` and the `test_codex_mesh.py` additions, from this Spec alone: append and read back; concurrent appends from several processes produce whole lines; unknown role is exit 2; malformed line counted as a problem; `role_at` before, between and after assignments; backfill over a fixture archive (a roster with `previous_role`, a seats ledger, a `review_status_adversary`) and a rerun adds nothing; a failed write exits 0 with the stderr line; every test sets `USAGE_STATE_DIR` to a temp dir.

**Constraints from prior phases:** none. `mesh_roster.json` entries carry `role` (`impl`, `test`, `fix`, `review`) and on a follow-up `previous_role`; no launcher writes `lens` into the roster (long-lived seats as-built, `docs/as-built/long-lived-seats.md`).

**Acceptance gate:** `python3 -m unittest scripts.usage.test_agents_log scripts.agents.test_codex_mesh scripts.delegate.test_end_session scripts.production.test_update_registration` green; basedpyright 0/0/0 on `scripts/usage scripts/agents/codex_mesh.py scripts/production/update_registration.py scripts/production/test_update_registration.py`; `bash -n` clean on the three shell scripts. Live, with `USAGE_STATE_DIR` pointed at a scratch dir: `agents_log.py backfill` places every live run's roster threads that carry a role (print the count), and `show` lists them. The archive keeps no `mesh_roster.json` or `seats` ledger, so archived runs recover nothing and have no coverage to measure (found in review 2026-10-11).

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
