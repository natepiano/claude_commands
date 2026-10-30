# Usage ledger

## What it is

The usage ledger is one SQLite store, on natedev, of every model request made on both machines, Claude and Codex alike. Each row records the request's token counts by class, its model and effort, and the role the agent held when it ran: showrunner, unit director, worker (`impl`, `test`, `fix`, `worker`), reviewer by lens, subagent or interactive session. The same store holds what the work produced: runs, phases, review passes, findings, checkpoints, compactions, account readings and account role changes. Metrics are named SQL queries over that store, so token efficiency can be followed over time across every role and every model. The vendors' own reporting does not answer these questions, but the logs on disk do. Before the ledger, each question needed its own scratch script that read raw logs again and knew only one model's weights. The role an agent held was lost once its run ended: the unit director's marker is deleted at run end, a Codex worker's role sits in a per-run `mesh_roster.json` that changes with every dispatch, and `/tmp` does not last. The token counts last (Claude transcripts under `~/.claude/projects`, Codex rollouts under `~/.codex/sessions`); the link from a request to its role did not.

## How it works

### Role log: `scripts/usage/agents_log.py`

The code that assigns a role records it when it assigns it, in an append-only `<state_dir>/agents.jsonl` on each machine. `state_dir()` is `USAGE_STATE_DIR` when set, else `~/.local/state/usage-model`.

- A line is `{"id", "role", "at"}`, plus `"lens"` when known and `"approximate": true` for a backfilled line. `id` is a Claude session id, a Claude subagent id (`a` + 16 hex), a Codex thread id, or the 8-hex prefix of a worker's transcript uuid.
- `Role = Literal["showrunner", "director", "impl", "test", "fix", "review", "worker", "interactive", "subagent"]`; `Lens = Literal["adversary", "contract", "craft", "ux"]`. The CLI refuses any other value with exit 2.
- `append(agent_id, role, lens, at, approximate=False)` writes under an exclusive `fcntl.flock` and starts a new line after a truncated tail.
- `register_default(agent_id, role, at) -> bool` reads the log and appends under the same lock, only when no line already names the agent. A line names it when its id equals `agent_id`, or when its id is 8 hex characters and a prefix of `agent_id`. Concurrent deliveries write one line.
- `assignments(path) -> RoleLog(assignments, problems)` reads the file as bytes and decodes each line inside its own handler. A malformed or undecodable line is a counted `problems` entry; the reader never raises.
- `role_at(log, agent_id, at) -> Assignment | Unassigned` returns the latest assignment at or before `at`. For a full id it merges the history recorded under `agent_id[:8]` when that prefix is a recorded 8-hex id, so a worker reads `interactive` until `agent_bg.sh`'s `worker` line, then `worker`.
- CLI: `append`, `register --id --role`, `backfill [--archive] [--live]`, `show [--id]`. A write failure prints one `agents_log: <reason>` stderr line and exits 0.

**Writers.**

| Writer | Role written |
| --- | --- |
| `scripts/direct/prepare_session.sh` | `director`, under `CLAUDE_CODE_SESSION_ID` |
| `scripts/production/update_registration.py register` | `showrunner`, at every showrunner start and resume |
| `scripts/agents/agent_bg.sh` | `worker`, under the 8-hex id |
| `scripts/agents/codex_mesh.py` | the worker's role and `--lens` on a new thread and on a follow-up claim; the previous role again when a claim is released |
| `scripts/direct/review.sh` | passes `--lens` to `codex_mesh.py` |
| `scripts/hooks/session-start-agents-log.py` | `subagent` for a payload with a non-empty `agent_id`, else `interactive` for `session_id`, through `register_default` |

The hook is registered twice in `settings.json`, with the command `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/session-start-agents-log.py"`: in the `hooks.SessionStart` entry with no `matcher` (startup, resume, clear, compact) and in a `hooks.SubagentStart` entry with no `matcher`. `SubagentStart` delivers the parent `session_id` and the `agent_id` before the subagent's first request; `SessionStart` does not fire for a subagent. The lens lives only in `agents.jsonl`, never in `mesh_roster.json`.

`backfill` reads each run directory's `mesh_roster.json` (`role`, and `previous_role` as an earlier line) and `workers` ledger (`worker`), stamped with the file's mtime and `approximate: true`. It skips any id that already has an exact line and never repeats an (id, role, lens) line. A review lens comes only from the newest `review_agent_[<N>_]<lens>.log` that names the worker.

### Collector: `scripts/usage/collect.py`

`python3 scripts/usage/collect.py [--db PATH] [--machine NAME] [--claude-root DIR] [--codex-root DIR] [--agents-log FILE] [--no-mac] [--readings FILE] [--baseline-readings FILE] [--account-roles FILE] [--claude-repo DIR] [--production-docs DIR] [--durability full|off]`

Defaults: `<state_dir()>/usage.db`, machine `natedev`, `~/.claude/projects`, `~/.codex/sessions`, `<state_dir()>/agents.jsonl`, the live readings log (`run_out.READINGS_LOG`), the baseline copy `~/.local/state/usage-model/workers-baseline/readings-2026-10-10.jsonl`, `accounts.roles_history_file()`, and the repository and production docs from `outcomes.default_sources()`. It prints one line per source, `<machine> <source>: <n> turns added; <state>` (or `outcomes added`), and exits 0 unless the store cannot be written or is refused.

**A pass**, in order:

1. Takes an exclusive, non-blocking `fcntl.flock` on `<db>.lock`. A second pass beside a running one prints `usage collect: another pass is running` and exits 0.
2. `check_store_key_version(db)`: a store stamped 1 through 7 raises `StorePredatesFileNamedCodexKeys`, and `main` prints `collect: store version <n> predates file-named Codex keys; delete <db> and run the collector again to rebuild it` and exits 1. This check runs before any statement that writes.
3. `PRAGMA synchronous=FULL` (`--durability off`, `LedgerDurability.DISPOSABLE`, sets `OFF` for a disposable test ledger; the default is `full`, `DURABLE`), `PRAGMA journal_mode=WAL`, the `SCHEMA` script, then `migrate_store(db)`. On a store below `STORE_VERSION` (10, `REQUEST_ACTIVITY_VERSION`) it adds the `activity` and `wait_action` columns missing from `turns` (`ADDED_TURN_COLUMNS`, below 10), renames stored `account_role_changes` rows with role `seats` to `workers` (`WORKER_ACCOUNT_ROLES_VERSION`, below 9), deletes every `cursors` row so the pass re-reads every local file and the next good Mac read every Mac file, and stamps `user_version` 10. A new store takes the same path and is stamped 10.
4. `collect_local`: Claude transcripts, Codex rollouts and run-history files on this machine, then the role log and `reattribute`.
5. `collect_mac` (or, under `--no-mac`, every Mac source stored as `skipped`).
6. `outcomes.collect`: baseline readings, live readings, account role changes and checkpoints, natedev only.
7. After a pass that returns 0, still under the pass lock, `watch_waste(args)` runs `waste_watch.py --db <args.db> --production-docs <args.production_docs>` as its own process (`sys.executable`, stdin from `/dev/null`, 5-minute timeout), so `collect` never imports `waste_watch` and the import cycle through `usage_report` stays broken. `args.db` is the store the pass wrote, the default store when no `--db` was given, so the watch sends only when the collector used the default store. `USAGE_WASTE_WATCH_SCRIPT` names another script, for tests. The watch's stdout passes through; its stderr passes through on exit 0. A nonzero exit, a timeout or an `OSError` replaces it with one `waste_watch: <reason>` stderr line (`exit <code>: <the watch's stderr on one line>`), and the pass still exits 0. The Mac never runs it.

**Reading a file.** `read_file(machine, path, family, previous) -> Iterator[Turn | Compaction | Opening | Edit | Folder | FileCursor]` is the one reader, used on natedev and on the Mac. `cursors(machine, path, offset)` holds each file's offset. A file whose size equals its cursor is skipped; a changed file is read from its start and yields only measurements at or past the cursor, then its new cursor; a file smaller than its cursor is read from zero; a line with no trailing newline waits for the next pass. Session files are read before `subagents/` files. Codex rollouts are parsed by `scripts/usage/rollouts.py` (`decode_rollout`, `token_usage`, `is_compaction`, `is_edit`, `KnownThreadOpening`).

**Keys.**

- A Claude request is keyed by `claude_turn_key` (`message.id`, `requestId`), one row per machine through the partial unique index `claude_message` on (`machine`, `turn_key`) where `family = 'claude'`. Repeated lines and copies in other files merge into the first row: the largest of each token class and the smallest `before_first_edit`, with the first row's identity, time, model, effort and attribution.
- A Codex request's `turn_key` and a Codex edit's `edit_key` are `<session file name>:<byte offset>`, built only by `codex_key(file_name, offset)`. `CodexContext(machine, file_name)` has no default file name. Keys are text and are never read as numbers.

**Agent identity.** In Python an agent is `ClaudeSession(machine, agent_id) | ClaudeSubagent(machine, parent_id, agent_id) | CodexThread(machine, agent_id)`; `parent_id` is `''` in SQL except for a subagent. The kind and machine come from which family's log, on which machine, holds the agent, never from the id's form. A subagent's parent comes from its transcript path, `<parent stem>/subagents/agent-<agent_id>.jsonl`.

**Request activity** (`scripts/usage/activity.py`, standard library only, shipped to the Mac). Each request is `Working() | Waiting(action) | PostingStatus()`, decided from atoms of the request's items: a final answer or any work atom makes it work; else any wait atom makes it wait, named by the first; else any status atom makes it a status post; else work. A Codex request's items are the `response_item` lines before its `token_count` record (`CodexContext.evidence`, also in the replayed prefix); a Claude request's are the lines of one message key, so a multi-line message takes the merged result and the merge `UPDATE` replaces `activity` and `wait_action`. A re-read Codex row that conflicts sets them with an `UPDATE` and does not count as added.

- Shell commands (Claude `Bash`, Codex `exec_command` `cmd`): heredoc bodies dropped, quoted text kept with its separators turned to `_`, `/dev/null` and fd-dup redirections dropped, then one atom per statement (`&&`, `||`, `;`, newline, `&`). A pipe into anything but a filter (`grep head tail wc awk sed sort uniq cut tr jq rg cat`), or a filter with `>`, is work. `sleep`, `wait`, `board.sh read`, `tail` without `>`, `cat`/`grep`/`rg`/`wc`/`stat`/`head` of a status file (`.log .out .output .status .exit .rc .done .pid .code`, `/proc/loadavg`, `sentinel`, `marker`, `exit_code`), `test`/`[`/`[[`, `ps`, `pgrep`, `kill -0` wait; `board.sh post <dir> <who> status` is a status post; `echo`, `cd`, `done` and the other neutral words add nothing; anything else is work.
- Codex: an `exec` custom tool call with `setTimeout(` waits; one calling any tool but `exec_command`/`write_stdin` works; an empty `chars` (or a `write_stdin` with none) waits as `empty write_stdin`; each `cmd` is classified as shell. `function_call` `sleep` and `wait` wait (`sleep tool`, `wait tool`); every other item type except messages, reasoning and outputs works.
- Claude: `Read` of a status file waits (`Read *.output`); every other tool use works; a text-only message is work.
- The Mac wire carries `activity` and `wait_action`; `remote_measurement` decodes them strictly (`decode_activity`) and a missing or invalid pair is a failed read.

**Token classes.** Claude: `input`, `cache_write_5m`, `cache_write_1h` (cache creation beyond the two `ephemeral_*` classes counts as 5-minute), `cache_read`, `output`. Codex: uncached input is `input`, cached input is `cache_read`, cache writes are 0. `before_first_edit` is 1 until the agent's first `Edit`, `Write` or `NotebookEdit` call (Claude) or the first record `is_edit` accepts (Codex), and the message holding that call is 0.

### Store: `<state_dir()>/usage.db`, `PRAGMA user_version` 10

Every stored time is UTC ISO-8601 text ending `+00:00`.

| Table | Key | Holds |
| --- | --- | --- |
| `turns(machine, family, kind, parent_id, agent_id, turn_key, at, model, effort, input, cache_write_5m, cache_write_1h, cache_read, output, before_first_edit, role, lens, attribution, lens_state, activity, wait_action)` | (`machine`, `family`, `parent_id`, `agent_id`, `turn_key`) | One row per model request. `activity` `work`, `wait`, `status` or `unclassified` (a row stored before version 10 whose source file is gone); `wait_action` names the first waiting action and is NULL unless `wait`. `family` `claude` or `codex`; `kind` `session`, `subagent` or `thread`; `model` `unreported` when the log names none; `attribution` `recorded`, `estimated` or `unassigned`; `lens_state` `known`, `unrecorded` or `not_applicable`. Indexes `turns_at` (`at`) and `turns_agent_at` (identity, `at`). |
| `compactions(machine, family, parent_id, agent_id, at)` | all five | Compaction events of both families. |
| `openings(…identity, at)` | identity | Claude: first timestamped record, never revised. Codex: the `KnownThreadOpening` time, following the latest session metadata. |
| `edits(…identity, edit_key, at)` | identity + `edit_key` | One row per edit; unique index `claude_edit_message` keeps a Claude edit to one row per machine. |
| `folders(…identity, folder)` | identity | Claude: `cwd` of the first record that has one. Codex: `cwd` of the latest session metadata. |
| `runs`, `phases`, `passes`, `findings`, `repair_rounds` | see below | Run history. |
| `checkpoints(commit_id, at, plan_slug, phase_id, mapping, machine, run_id, phase_instance_id)` | `commit_id` | Commits whose subject starts `checkpoint(`. |
| `account_readings(machine, account, at, remaining_state, remaining, credits_state, credits)` | (`machine`, `account`, `at`) | Account meter readings, live and baseline. |
| `account_role_changes(machine, at, role, label)` | all four | Which account held each role, over time. |
| `cursors(machine, path, offset)` | (`machine`, `path`) | Read offsets. |
| `turn_sources(machine, family, state, read_at, last_complete_at)` | (`machine`, `family`) | One row per source family per machine, written every pass. `last_complete_at` moves only on `ready`. |
| `role_log_sources(machine, problems, state, read_at)` | `machine` | The role log's state; `problems` describes the log only when `state` is `ready`. |

`SourceState` is `ready`, `missing`, `unreadable`, `partial`, `skipped` or `unreachable`. Source families: `claude`, `codex`, `history` on both machines; `checkpoints`, `readings`, `baseline_readings`, `account_roles` on natedev.

**Run history** (`history_root()/runs/<run id>.jsonl`, both machines, read through `cursors`). Eleven event types are kept; a start event inserts a row, later events update it, and an update with no row is skipped. `HISTORY_FIELDS` is the one field table behind the local decode, the wire encode and the wire decode. A field an event does not carry is NULL in SQL and `EventFieldNotCarried` in Python.

- `run_started`, `run_finished` → `runs`, key (`machine`, `run_id`).
- `phase_started`, `phase_finished` → `phases`, key (`machine`, `phase_instance_id`).
- `pass_started`, `pass_finished` → `passes`, key (`machine`, `pass_instance_id`); `kind` is `pass_kind`.
- `finding_opened`, `finding_verdict` → `findings`, key (`machine`, `phase_instance_id`, `finding_id`); `state` is `open` until a verdict, then its `resulting_state`.
- `finding_batch_dispatched`, `_landed`, `_abandoned` → `repair_rounds`, key (`machine`, `phase_instance_id`, `round`).

In `runs`, `phases` and `passes`, `state` is `open` until the finish event and `finished_at` is NULL only while `open`. `source_event` is the creating event's `event_id`, from which a finding's title and detail can be found in the history file.

**Checkpoints** (`scripts/usage/outcomes.py`, natedev only). `git log --all --format=%H%x00%ct%x00%s` in every repository `outcomes.repositories(...)` returns. `checkpoint_subject` parses `checkpoint(<plan-slug>): phase <id> — <title>`; the phase id is everything between `phase ` and the first ` — ` and may contain spaces. Any other `checkpoint(` subject is stored with NULL slug and phase. `checkpoint_mapping` is recomputed for every checkpoint on every pass: among `phases` whose run's `plan_doc` stem equals the slug, with the same phase id, started at or before the commit, and still `open` or finished at or after it, the latest-started one makes `mapping = 'mapped'`; otherwise `unmapped` with NULL `machine`, `run_id`, `phase_instance_id`.

**Readings.** Each number is stored with its state: `not_reported` (key absent or null), `read` (finite, in range: `remaining` 0 to 100, `credits` 0 or more), `unreadable`; the number column is NULL unless `read`. In Python: `AmountNotReported | AmountRead | AmountUnreadable`. A live reading replaces a baseline reading with the same machine, account and time in either read order; a baseline reading never replaces a stored row.

### Role attribution

`RoleResolver.resolve(agent, at) -> RecordedAttribution | EstimatedAttribution | UnassignedAttribution`:

- A role-log line in force gives `recorded`, or `estimated` when the line is `approximate`.
- A `main_agent.session_id` named in a run-history file gives an `estimated` `director` from the first event naming it. It wins over no line, and over an `interactive` line no later than it.
- Anything else is `unassigned`.
- The whole log, with its prefix merge, is used only for a Claude session whose 8-hex prefix matches exactly one known session and has a recorded `worker` history. Every other identity is looked up by exact id.
- A `review` role with no lens is `lens_state = 'unrecorded'`.

`reattribute` re-resolves every stored turn of the machine on every pass, so a late role-log line corrects earlier turns without adding rows. An `unreadable` role log leaves stored attribution as it was.

### The Mac half

The Mac is read on the Mac. `collect_mac` runs `python3 -c REMOTE_BOOTSTRAP` over `ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=5 -o ServerAliveCountMax=1 natemccoy@mac` (`MAC_HOST`), with `; printf "\nrc=%s\n" "$?"` appended. `reader_payload` sends the sources of `scripts/usage/activity.py`, `agents_log.py`, `rollouts.py` and `collect.py`, in that order, and the stored Mac cursors on stdin; the bootstrap loads them under their module names and calls `read_mac(offsets)`. The Mac needs `python3` and no checkout, and nothing is written to its disk.

`read_mac` prints one JSON record per line: `manifest`; `agents.jsonl` as base64 `chunk` records; one `director` record per run-history director; `attribution_ready`; then per changed file a `file` record, one `measurement` per turn, compaction, opening, edit or folder (`kind` names which), one `outcome` per kept run-history event, and its `cursor`, or `problem` when the file cannot be read. Transcripts and run-history files never cross the link.

On natedev, `consume_remote` decodes each record strictly (`remote_measurement`, `remote_director`, `remote_history_event`, `remote_string`, `remote_integer`) and raises `RemoteReadFailed` on a missing or invalid field. `receive_measurements` commits one file's rows and its cursor in one transaction. The role snapshot and `reattribute` commit together at `attribution_ready`, before any turn file. A stream with no final `rc=0` line is a failed read. A failed read keeps every file it finished: with none kept, the Mac's sources are `unreachable`, otherwise `partial`. The printed reason is ssh's first non-empty stderr line, or the caught error when ssh wrote nothing; the stored state carries no reason.

### Weights: `scripts/usage/weights.py`

Weights are a dated table per model, applied when the store is read and never stored. `ModelWeights(model, effective_from, cached, cache_write_5m, cache_write_1h, output)`, with uncached input as 1:

| Model | From | Cached | 5-min write | 1-hour write | Output |
| --- | --- | --- | --- | --- | --- |
| `gpt-5.6-sol` | the beginning | 0.1 | 0 | 0 | 8 |
| `gpt-6.1-sol` | 2026-10-10T21:35Z | 0.05 | 0 | 0 | 5 |
| `gpt-6-sol` | the beginning | 0.1 | 1.25 | 1.25 | 5 |
| `gpt-6-astra` | the beginning | 0.1 | 1.25 | 1.25 | 5 |
| `claude-*` | the beginning | 0.1 | 1.25 | 2 | 5 |

"The beginning" is `datetime.min` in UTC. A model matches an entry by equal name, or `claude-*` by the prefix `claude-`; of the matching entries, the latest `effective_from` at or before the request's time applies. `weights_at(model, at)` raises `UnweightedModel` when none is in force; `weighted(row, at)` applies one entry in Python. `sql_weights(model, at) -> DatedWeightRelation(entries, active_entry)` gives the same choice as SQL, and is what every report and the worker readout use. `register(db)` defines the SQL function `weighted_tokens(...)`, which returns NULL for an unweighted model and is not used by any reader on the full store.

### Report: `scripts/usage/usage_report.py`

`python3 scripts/usage/usage_report.py [--db PATH] [--production-docs DIR] list` prints each metric with its description. `… <metric> [--by DIM[,DIM…]] [--since WHEN] [--until WHEN] [--zone NAME] [--exact]` prints one Markdown report. The default grouping is `role,model` when the metric takes both, else every dimension it takes. The default zone is the user's zone from `config/user_zone.conf` through `scripts/lib/user_zone.py` (`USER_ZONE` in the environment wins). `WHEN` is an ISO date or datetime, read in the zone when it has no offset; `--since` is inclusive, `--until` exclusive. `--exact` keeps only `attribution = 'recorded'` turns. Every refusal is one `usage_report: <reason>` stderr line naming what is accepted, exit 2; a store it cannot open, or whose version is not `collect.STORE_VERSION` (`StoreVersionMismatch`), exits 1.

`open_read_store(path)` opens `?mode=ro` with `sqlite3.Row` and begins one transaction, so a run reads one snapshot and a missing path stays missing. The report, the worker readout and the replay all open the store through it.

**Three registries.**

- **Dimensions** (name → SQL): `day` (the date in the zone, SQLite arithmetic over the zone's offset changes: `zone_offsets`, `day_sql`), `role` (`unassigned` when NULL), `lens` (the lens, else its `lens_state`), `model`, `effort` (`unreported` when NULL), `machine`, `unit`, `activity` (turn sides only; `CONTEXT_DIMENSIONS`, every dimension but `activity`, is what `compactions`, `compaction_reread` and `threads` carry).
- **Outcomes**: `QueryOutcome(name, query, dimensions, aggregate="count(*)", exact_where="1")` or `TurnGroupOutcome(name, dimensions, aggregate)`.
  - `checkpoints`: `day`, `machine`, `unit` from the mapped phase's run worktree; an unmapped checkpoint is machine `natedev`, unit `unknown`.
  - `findings_closed`: `findings` with `state = 'accepted'`, at `state_at`.
  - `review_rounds`: `passes` with `kind = 'review'`, at `started_at`.
  - `compactions`: every dimension, taken from the agent's latest turn at or before the compaction, else its earliest turn.
  - `compaction_reread`: for each compaction, the first request after it on the same thread (`input + cache_read > 0`, first in `(at, turn_key)` order, the key compared as text), each request counted once. Its value is uncached input plus cache writes at their weights, no cache reads and no output. Under `--exact`, `exact_where` keeps only recorded first requests and does not replace a dropped one.
  - `threads` (`TurnGroupOutcome`): distinct agents with a turn in the group.
  - `account_switches`: `account_role_changes` rows whose `label` differs from the previous row of the same machine and role.
- **Metrics**: `Metric(name, description, numerator, denominator=NoDenominator(), format=NUMBER)`; a side is `TurnSum`, `TurnAggregate` or `OutcomeCount`; a format is `number`, `share`, `count` or `rate`.

| Metric | Numerator / denominator |
| --- | --- |
| `weighted` | weighted tokens; the table also shows turns |
| `weighted_per_checkpoint` | weighted / `checkpoints` |
| `before_first_edit_share` | weighted with `before_first_edit = 1` / weighted, over agents that edited |
| `cache_read_share` | `cache_read` / all four input classes, raw tokens |
| `output_share` | weighted output / weighted |
| `compactions_per_thread` | `compactions` / `threads` |
| `compaction_reread_share` | `compaction_reread` / weighted |
| `weighted_per_finding_closed` | weighted / `findings_closed` |
| `weighted_per_review_round` | weighted / `review_rounds` |
| `account_switches` | the count |
| `wait_share` | weighted with `activity = 'wait'` / weighted |
| `status_share` | weighted with `activity = 'status'` / weighted |
| `context_share` | `cache_read_weighted` (cache reads at their cached weight) / weighted |

A new metric, dimension or outcome is one registry entry. The report reads `turns` once into a query-local grain (`turn_grain`, `turn_groups`) with every dimension, `activity`, the five token classes, `weighted`, `output_weighted`, `cache_read_weighted`, `before_first_edit` and `edited`; a one-row-per-turn relation exists only for a `TurnAggregate` side. Each side is grouped by the requested dimensions it carries and the two are joined on the dimensions both carry, so an outcome count is one row per group before the join. A metric with no turn side refuses a dimension it does not carry.

**Unit.** Only the folder is stored. `register_report_functions(db, unit_worktrees(production_docs))` registers the unit lookup; `document_units(path)` reads one production doc and `longest_first` orders the rows: the `Unit` cell of a `## Units` table row, in any `*-production.md` under `--production-docs`, whose `Worktree` is the agent's folder or contains it (longest wins), else the folder's last path component, and `unknown` with no folder.

**Output**, in order: `<metric> — <description>`; the bounds, the zone and, under `--exact`, `exact evidence only`; the table (dimension columns, numerator, denominator, value; a zero denominator gives `—`, a group with no weighted turn reads `unweighted`); `Coverage` (weighted usage and turns per attribution with shares; `Unweighted:` with each model lacking a weight entry and its turn count; review turns with lens `unrecorded`; `Turns with unclassified activity: <n>.`; outcome rows with unit `unknown`); `Sources` (one row per `turn_sources` and `role_log_sources` row, each source not `ready` first under `Not read in full:`, times in the zone or `never`). With no turn in range the report prints its headers, `No turns in range.` and `Sources`. `report_data`, `render_report` and `sources(db, zone)` are the pieces other readers call.

### Worker readout: `scripts/whoami/worker_usage.py`

`python3 scripts/whoami/worker_usage.py [--db PATH] (--daily [--t-live WHEN] | --before START END --after START END)` compares Codex usage per checkpointed phase before and after long-lived workers went live (`DEFAULT_T_LIVE` 2026-10-10T18:17Z). It is a view over the store: no rollout, ssh, git or readings file. Rows cover `family = 'codex'` threads on both machines (a thread is one `machine, parent_id, agent_id`). Columns: threads opened, weighted usage, before-first-edit share, compactions with compactions per active thread (a thread with any opening, turn, edit or compaction in the piece), checkpointed phases, account switches per week and credits per day.

- **Window pieces.** Each window is cut at every instant strictly inside it at which a dated weight entry starts for a model with Codex turns there; each piece is its own row, labelled with its UTC start (`After, from 2026-10-10 21:35Z`). Weights apply as grouped SQL: token classes are summed per weight entry and multiplied once. `Weighting:` names each weight version used; `Unweighted:` names each model with no entry and its turn count.
- **Verdict.** `Weighted usage per checkpointed phase: before …; after …; <change>` compares the whole `Before` window with the last `After` piece, with notes for different weight versions, unweighted turns left out, and the Mac's freshness (`Mac as of <time>` or `Mac never read in full`). It is the last line, after the `Sources` block from `usage_report.sources(db, UTC)`.
- **Switches.** A switch is a change of the account holding the `workers` role (`accounts.CODEX_ROLE`) in `account_role_changes`, read by `store_switch_history(db) -> RoleSwitchHistory(start, switches)`. Readings before the stored history begins still count reading-label changes. `credits_per_day` sums each account's own decreases.

### Compaction replay: `scripts/usage/compaction_replay.py`

`python3 -m scripts.usage.compaction_replay [--db PATH] [--since DAY] [--until DAY] [--follow-up-prompt TOKENS]` replays stored Codex threads under other compaction limits, to set `UNIT_COMPACT_ABOVE_TOKENS`. Defaults: the collector's store, follow-up prompt 11,000 tokens. `--since` and `--until` are read in the user's zone and select whole threads by their first request.

- Covered: `codex` threads with a compaction and a request, every request of which has a weight entry. A thread's requests, in `(at, turn_key)` order, are cut into segments at each compaction; a request at the same instant as a compaction stays in the earlier segment.
- `RULES`: `LimitRule` at 100,000, 150,000, 200,000 and 250,000; `NeverRule()`; `NewWorkerRule()` (limit 100,000, and a new worker when the kept size is over 90,000).
- `fit_segments(threads) -> FittedLine(intercept, slope) | FitUnavailable(reason)`: least squares of the next segment's first size on the previous segment's last size. `FittedLine.kept` is never under 0.
- `boundary_outcome(rule, fit, end, first_size, prompt) -> CarriedOn | Compacted | NewWorker`. A carried segment's first request costs `end` at the cached weight, the prompt, and its stored output; a compacted or new-worker one costs its start size and that output. Each later request costs its stored weighted tokens plus `max(difference, -cache_read)` at the cached weight. A thread's first segment costs what is stored under every rule.
- Output: covered counts, the fit line, the median first-request size, the exclusion line, the rule table (`simulated compactions`, `new workers`, `weighted tokens`, `change against limit 100,000`, `requests over 272,000`), stored weighted tokens, threads left out by reason, rows of size 0 skipped, and requests held at the cache-read floor per rule. With no fit it prints `fit unavailable: <reason>` and no table, exit 0.
- Refusals print one `compaction_replay: <reason>` line, nothing on stdout, and leave the store's bytes unchanged: exit 2 for bounds (`invalid bounds; …`) and a prompt that is negative, NaN or infinite; exit 1 for an older store, a non-database file and a missing path.

### Waste watch: `scripts/usage/waste_watch.py`

`python3 scripts/usage/waste_watch.py [--db PATH] [--production-docs DIR] [--dry-run] [--now ISO]`; the collector runs this script after each good pass (step 7 above). It reads the store through `open_read_store` and `check_store_version`, groups the last 2 hours' requests (`at` compared as UTC text) by thread in SQL (`thread_usage`: folder, latest role, weighted, waiting weighted, waiting actions, the longest run of Codex requests whose `input + cache_read` is above 250,000 by gaps and islands in `(at, turn_key)` order), and prints nothing when nothing alerts.

- **Units**: each folder's unit comes from the `## Units` tables of every registered showrunner's production doc and of `--production-docs`, longest worktree first, else the folder's last component, else `unknown`.
- **Wait alert**: a unit whose Codex worker threads (`impl`, `test`, `fix`, `review`, `worker` or unassigned) spend over 10% of their weighted usage on waiting requests, on at least 1,000,000 of their weighted tokens in the window. Claude sessions and other Codex roles never count toward it: a Claude session reads a background task's output once after its completion notice, which the classifier labels wait but is the required step, not a poll. On 2026-10-16 00:30–01:20 EDT those reads alone put two units at 9–11% while no worker polled. Their waiting stays visible in `wait_share`.
- **Context flag**: a Codex thread with role `impl`, `test`, `fix`, `review`, `worker` or none, with 5 or more requests in a row above 250,000 tokens. It makes its unit alert too; one message per unit holds both.
- **Message**: first line `Message from usage-waste-watch: <unit> spent <x>% of its weighted usage in the last 2 hours on requests that only waited` (or the context finding alone), the window totals, the top 3 waiting threads, the most common waiting action, the flagged threads with run length and peak, and the `usage_report.py wait_share --by unit,role --since <start>` command.
- **Delivery**: the registered showrunner (`showrunners.registered_showrunners()`) whose doc lists the unit, running ones first, through `send.py --to uds:<socket> --from usage-waste-watch --text` (`WASTE_WATCH_SEND` replaces the script path in tests). Exit 0 or 1 (queued) is delivered. No showrunner listing the unit, a stopped one or a failed lookup prints the alert to stdout with the reason; a failed send prints it too.
- **Throttle**: at most one alert per unit per 6 hours, in `<state_dir()>/waste_watch.json` (unit → UTC time), written atomically after a delivery or a printed no-showrunner alert; a failed send is not recorded.
- **Dry run**: `--dry-run`, and any store other than the default `collect.store_path()`, print each alert with its destination, send nothing, write no state and ignore the throttle. Tests and scratch stores therefore never send.
- When the window's total waiting is at most 100,000 weighted tokens and no thread is flagged, no unit can alert and no showrunner is looked up.

### Timer

The hourly pass lives outside this repository, in `/etc/nixos/modules/linux/usage-collect.nix` (`nate.jobs.usage-collect`, `intervalSeconds = 3600`, `TimeoutStartSec = "45min"`): `exec python3 "$HOME/.claude/scripts/usage/collect.py"` with the user's PATH, one hour after the last pass ended and one hour after login. The collector's only message is the waste watch's alert to a showrunner; its output, and any alert with no showrunner, goes to the journal. It takes effect at a NixOS rebuild.

### Worker roster guards in `scripts/agents/codex_mesh.py`

The roster carries each worker's role and the context size that decides when a launcher compacts it. Its writes are guarded so that a command still at work for a worker's earlier thread, or a compaction that ends late, cannot replace a newer record. None of this enters the usage store.

- `_update_roster_if_expected(session_dir, name, record, expected) -> bool` compares and writes inside one `fcntl.flock` section. `ExpectedRoster = RosterOnThread(thread_id) | RosterAbsent() | RosterEndedOnThread(thread_id) | RosterUnchanged(thread_id, revision)`. `_write_roster_record` holds the storing rules for it and for `_update_roster`.
- Every entry carries `revision` (`RosterRevision`), raised by one on each write inside the lock. An entry without one reads as `RosterRevision(0)`. A writer that changes an entry in place calls `_advance_roster_revision(entry)`.
- `command_compact` reads the revision at its start and writes the new size under `RosterUnchanged`, so a turn that wrote in between keeps its record.
- `context_tokens` is an integer or `"not_measured_since_compaction"` (`RecordedContextSize = KnownContextSize | NotMeasuredSinceCompaction | ContextSizeUnavailable`). A compaction turn that reports 0 stores the state and prints `compacted <name>: <before> tokens, size not measured until its next turn`. `implement.sh` and `review.sh` ignore a non-integer size, so such a worker gets its follow-up with no second compaction. `compact` of such a worker exits 2 with `size not measured since its last compaction; compact refused`.
- A fork or a follow-up whose name was taken meanwhile releases only its own thread and refuses (`name was taken while the fork ran; fork refused`, `name was taken while the follow-up ran; follow-up did not start`).

### Key files

| File | Role |
| --- | --- |
| `scripts/usage/agents_log.py` | Role log: writer, `register_default`, reader, `role_at`, backfill, CLI |
| `scripts/hooks/session-start-agents-log.py` | `SessionStart` / `SubagentStart` hook: `interactive` or `subagent` |
| `settings.json` | The two hook registrations |
| `scripts/usage/collect.py` | Collector: schema, `STORE_VERSION`, keys, attribution, cursors, both halves of the Mac read, pass lock |
| `scripts/usage/rollouts.py` | Codex rollout parser, standard library only, sent to the Mac |
| `scripts/usage/activity.py` | Request activity classifier, standard library only, sent to the Mac |
| `scripts/usage/waste_watch.py` | Wait-share and context alerts to a unit's showrunner, after each pass |
| `scripts/usage/outcomes.py` | Checkpoints and their mapping, readings, baseline readings, account role changes; `default_sources`, `repositories` |
| `scripts/usage/weights.py` | `MODEL_WEIGHTS`, `weights_at`, `sql_weights`, `register` |
| `scripts/usage/usage_report.py` | Metric, dimension and outcome registries; `open_read_store`, `check_store_version`, `sources` |
| `scripts/usage/compaction_replay.py` | Compaction-limit replay |
| `scripts/whoami/worker_usage.py` | Codex before/after worker readout over the store |
| `scripts/lib/user_zone.py`, `config/user_zone.conf` | The zone report bounds and times are read in |
| `scripts/agents/codex_mesh.py` | Worker role and lens recording; roster revision and guarded writes |
| `scripts/direct/prepare_session.sh`, `scripts/agents/agent_bg.sh`, `scripts/production/update_registration.py`, `scripts/direct/review.sh` | Role writers |
| `/etc/nixos/modules/linux/usage-collect.nix` | Hourly timer |
| `scripts/usage/test_*.py`, `scripts/whoami/test_worker_usage.py`, `scripts/usage/fixtures/` | Tests over invented logs; expected reports compared whole |

## Invariants

- Store only what a script cannot look up later. The role log holds id, role, lens and time; kind, machine, worktree, unit and production are looked up when read. Turn rows are measurements and are stored in full.
- A role is recorded by the code that assigns it, when it assigns it, not inferred afterwards. A role change writes a new line; each request takes the role in force at its time.
- Recording never breaks the work it records: a failed append prints one `agents_log:` stderr line and the caller carries on with exit 0.
- Tests never touch the real `~/.local/state/usage-model/`: every path comes from `USAGE_STATE_DIR` or an explicit flag. A collector run in a test names every source and passes `--no-mac` or puts the fixture ssh first on `PATH`; a fixture `HOME` alone does not stop it reaching the real Mac.
- The collector is incremental and idempotent: a rerun adds only new requests and never stores one twice. One file's rows and its cursor commit together.
- `check_store_key_version` stays ahead of every statement in `collect_pass` that writes. A store stamped 1 through 7 is refused, never read again or rewritten in place. Any change to keys or to what a measurement is raises `STORE_VERSION`.
- `activity.py`, `agents_log.py`, `rollouts.py` and `collect.py` import only the standard library and each other at module level, because they run on the Mac from ssh stdin. Code the Mac runs lives in those four; `outcomes` is imported inside natedev-only functions. Nothing under `scripts/usage/` imports `scripts/whoami/worker_usage.py`.
- Weights are applied on read, never stored. A model with no entry in force is in no weighted figure, gets no default weight, and is listed as unweighted. A new model needs its own `MODEL_WEIGHTS` entry before it counts.
- Nothing runs a Python function per request over the full store: per-request work is SQL (`sql_weights`, `day_sql`).
- Readers open the store read-only through `open_read_store`, in one snapshot, and write nothing.
- Every figure reports its coverage: attribution shares, unweighted models, unrecorded lenses, unknown units and each source's state. A source not `ready` is listed first; a number is never presented as complete from partial data.
- An outcome count is grouped to one row per group before it joins turns; a row-for-row join multiplies weighted sums.
- Every stored time is UTC text ending `+00:00`; day arithmetic, dated weights and key order compare that text.
- A Codex key is text, compared as text; no code reads a number out of it.
- No Pushover and no `send.py --to user` from any of this. The waste watch writes only to a showrunner's session, and only for the default store. The collector fails quietly into its journal, and a watch failure never fails a pass.
- A change to a refusal's exit status or text changes its test in the same commit; a new refusal gets its own test.
- A roster writer that changes an entry in place advances `revision`. The next revision is computed in `_advance_roster_revision` and `_write_roster_record`, which change together; the not-measured size is encoded in `command_compact` and read back in `_write_roster_record`, which also change together.

## Calibration / gotchas

- **Store size and speed** (natedev, 2026-10-12): a full pass about 214 s, the next about 31 s, store about 600 MB. An additive report 2.3 to 2.8 s; a per-turn `TurnAggregate` metric 8 to 15 s; the worker readout about 2 s. A Python function called once per request costs minutes on 1.2 million requests.
- **The Mac link** ran at 0.42 MB/s and at 6 MB/s on the same day. The Mac held 112 MB of Claude logs and 5.4 GB of Codex logs in 1,859 files (largest 754 MB): 5,522 Claude requests, 278,850 Codex requests, 1,660 compactions. A measurement is 477 bytes on the wire. A first full read took about 9 minutes at the slow speed; a pass with nothing new 8.8 s. A time limit on a pass must allow about 10 minutes for a first Mac read; the timer allows 45.
- **The Mac has no `agents.jsonl` yet.** Its role log reads `missing`, and Mac roles come from run history only.
- **Token counts** outside 0..`MAX_SQLITE_INTEGER` (2**63-1) are never measurements: locally the line is skipped and its cursor advances; on the wire one ends the pass as a failed read.
- **Listing** uses `os.scandir`, because Python 3.13 `Path.glob` hides listing errors. A nested folder that cannot be listed makes the source `partial`.
- **Codex key collisions**: the key holds the file's last path component only, so a moved file keeps its keys, and two files of one thread with the same name would share them. Codex names files `rollout-<date-time>-<id>.jsonl`.
- **Hook order**: for a worker the hook writes `interactive` under the full uuid before `agent_bg.sh` writes `worker` under the prefix; the prefix merge in `role_at` handles that. `register_default` reads the whole log at every session start. Sessions started before the hook reached a machine's `~/.claude` have no line and are `unassigned`.
- **Backfill limits**: the worker archive keeps no `mesh_roster.json` or `workers` ledger, so archived runs backfill nothing; older review logs do not name the worker, so their lens stays unrecorded; a review outside a run (`agent_exec.sh`) records nothing.
- **Role coverage is thin for old history.** At first import most Codex requests were `unassigned` (460,601 of about 496,500); Claude about half (191,755 of about 400,000).
- **Checkpoints** are read on natedev only, so a commit made on the Mac counts once natedev's repository has it. A candidate repository path that no longer exists (a removed worktree; 4 of 18 at first read) is skipped. 6 of 637 real phase ids contain spaces.
- **Weights above 272K input**: the `gpt-6-sol` and `gpt-6-astra` entries leave out the 2× input and cache and 1.5× output premium, so a weighted figure for such a request is under its cost. The only model left unweighted on natedev's store is `<synthetic>` (458 requests).
- **Worker switch count**: two Codex accounts read at the same instants alternate labels row by row, so counting reading-label changes gave about 750 switches a week where the role history gave 0.0. natedev's role history held three `unit` rows and no `workers` row when read, so an `After` row moves only once a change to that role is stored.
- **WAL**: a pass that switches an older store to write-ahead mode while a reader holds it fails once and succeeds on the next pass. A refused replay over a WAL store leaves `-wal` and `-shm` files beside it.
- **Refusal texts disagree on remedy**: the report and replay tell the owner of an older store `run the collector once`; the collector then says to delete the store and rebuild. `file is not a database` and `unable to open database file` are SQLite's own texts, so a SQLite release that rewords them fails two tests.
- **`main` takes the pass lock before the version check**, so `<db>.lock` is created beside a refused store.
- **`scripts/whoami/` is not a package**: its tests import by full path (`from scripts.whoami import worker_usage`) and are loaded by discovery.
- **Compaction size**: a Codex compaction turn reports 0 input tokens, which is why the size after a compaction is a named state; a kept size over the limit would compact the worker before every follow-up. `_update_roster_if_expected` without `RosterUnchanged` refuses only a name that moved to another thread.
- **Replay reference table** (natedev scratch store, version 8, 2026-10-12; 664 threads, 2,001 compactions, 355,236 requests; fit `kept = 83,899.84 - 0.170678 × before`; median first request 27,080). Later limit proposals compare against it.

  | rule | simulated compactions | weighted tokens | change against `limit 100,000` | requests over 272,000 |
  | --- | --- | --- | --- | --- |
  | `limit 100,000` | 1,928 | 5,777,829,425 | the base | 1,953 |
  | `limit 150,000` | 1,707 | 5,798,934,432 | +0.4% | 3,234 |
  | `limit 200,000` | 1,523 | 5,887,358,506 | +1.9% | 8,555 |
  | `limit 250,000` | 683 | 8,066,593,866 | +39.6% | 111,029 |
  | `never` | 0 | 23,242,221,181 | +302.3% | 210,404 |

  The new-worker row equals `limit 100,000` with 0 new workers: the fitted slope is negative, so the kept size never passes about 84,000. Stored weighted tokens of the same threads: 5,551,641,863; the replay's base is about 4% over it, because it does not cost the compaction request and does not simulate Codex compacting a full context. `UNIT_COMPACT_ABOVE_TOKENS` stays 100000.
- **Compaction reread share** by role and model: `gpt-6.1-sol` 4.4% (director) and 6.8% (unassigned); Claude director models 4.9% to 7.0%; `gpt-5.6-sol` 0.4% to 0.5%.

- **Unclassified rows**: a row stored before version 10 whose file is gone (or a Mac file not yet re-read) stays `unclassified`; the report counts such turns in its coverage.
- **The shell classifier is broad**: for Claude it also counts `tail build.log`, `ps -p` and `board.sh read` as waiting. A request that waits and works in one call is work, so the wait share is a lower bound.

## Why

- **Request activity stored per request.** Codex workers spent about half their weighted usage waiting on builds for days, and nothing recorded what a request did, so it went unseen until an account used a week's usage in 20 hours.
- **The watch's thresholds.** At the measured median of 2.1M weighted tokens per folder per 2 hours and about 19K per request, 1M is about 50 requests; below it a handful of polls sets the share. At 10% the waste at the minimum is at least 100K weighted tokens, more than the showrunner turn an alert costs. Codex compacts workers at 200,000 tokens, so five requests in a row above 250,000 mean compaction is not happening. Six hours between alerts keeps one problem to one message per working session.

- **SQLite, one store on natedev.** Metrics are joins of requests against roles and outcomes, and SQLite ships with Python. The store and the timer are on natedev because the Mac sleeps with its lid closed; a Mac that does not answer is caught up on the next hourly pass.
- **Roles recorded at assignment.** The sources that knew a role (active markers, `mesh_roster.json`, `/tmp`) are deleted or overwritten when a run ends; inferring later loses it. Storing only id, role, lens and time follows the user's rule of 2026-10-08: store only what a script cannot look up.
- **A lens recorded, never inferred from a worker name.** Any worker runs any lens, so the name says nothing.
- **Every Claude session and subagent records itself by hook.** Worker tooling covers only its own agents; the hook makes interactive sessions and subagents rows of their own, so a subagent's usage counts once, under the subagent.
- **Claude requests deduplicated by (message id, request id), keeping the largest of each class.** Counting a message once per file or per agent double-counted 0.19% of all tokens; keeping the first copy of a repeated message lost 5.8% of output.
- **Run-history directors dated from the first event naming them**, not from the run's start: dating from the start backdated a mid-run takeover.
- **The Mac read on the Mac.** Copying raw logs to natedev would hold transcripts twice, and sending whole files in one transaction hit a 720 s limit twice and kept nothing. Sending run-history files as bytes cost 49.6 MB and 117 s on every pass, so only their kept events cross.
- **The remote status read from a printed `rc=` line.** ssh's own exit status is always 0 on the Mac, so a stream with no final `rc=0` is the only sign of a failed remote read.
- **Codex keys carry the session file name.** Two session files of one thread can reach the same byte offset; a bare offset merged them. An older store is refused and rebuilt instead of rewritten, because a rebuild costs one pass and natedev held no store when the change was made.
- **Weights dated per model, applied on read.** A window that crosses a model change splits by itself, a new entry reweights all history, and a model with no entry is visible as unweighted instead of silently given a default.
- **Weights as SQL, not a registered function.** A Python callback per request costs minutes over a million requests; grouped SQL costs seconds.
- **Every figure carries coverage and source states.** Old history is mostly `unassigned` or `estimated`, and the Mac is often unreachable; a figure without those is not readable.
- **A worker switch is a change in the `workers` role history**, not a reading-label change: two accounts read at the same instants alternate labels and inflate the count about 750 times over. Worker-name conversion in `scripts/accounts/worker_names.py` keeps the journal inode and its append lock. Before rewriting any bytes it saves the complete converted text to `.<journal name>.workers` through a temporary file and `os.replace`. A failed or interrupted rewrite leaves that recovery image for the next conversion; it is removed only after the journal is flushed and synced. Move and role writers recover the image before appending another event. Both auto-move replay readers also recover a pending image under the journal lock (and the role lock for role history) before parsing; without an image they retain their existing read path and lock cost. Malformed historical lines remain unchanged.
- **The compaction limit stays 100,000.** In the replay every higher limit used more weighted tokens and sent more requests over 272,000 input tokens; no rule came in at least 5% under the base.
- **A roster revision, not a comparison of status, turn id and size.** A turn's ending write removes its turn id, so a newer turn that ends with the same status and size would pass a field comparison.
- **The size after a compaction is a named state.** Codex reports 0 for the compaction turn; storing 0 hides the size, and keeping the size from before would compact the worker before every follow-up.
