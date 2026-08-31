# Agent Registry — one place for family, agent, and effort assignments

## What it is

Every external-CLI agent this configuration launches — `/plan:delegate`'s implementer and reviewer, the `~/.zshrc` CLI aliases, the unattended fix style pipeline and its report render, `/ask_a_friend`, and the `/team_review` / `/api_review` / `/module_review` review teams — resolves which vendor CLI to run, which model, and at what reasoning effort from one file (`config/agents.conf`) through one resolver (`scripts/agents/agents_config.sh`), and launches through one dispatcher (`scripts/agents/agent_exec.sh`). The problem it solves: without a registry each consumer carries its own private assignment state in its own conf file and hard-codes its own vendor flags, so switching a function between vendors means editing several scripts and auditing "what runs what" means reading all of them. Here, a major function switches between the `codex` and `claude` families, or a single sub-task is re-pointed to a different model or effort, with one `/agent` edit, and no consumer assembles vendor flags itself.

## How it works

### Registry schema

`config/agents.conf` is an INI-style file with four kinds of section. Comments (`#`) are stripped to end-of-line everywhere, including trailing inline comments on rows.

```ini
[assignments]                 # <function>=<family>, plus optional <function>.<subtask>=<family>
delegate=codex

[delegate.codex]              # [<function>.<family>] — <subtask>=<agent>[:<effort>]
implementation=gpt-5.6-terra:xhigh
review=gpt-5.6-sol:xhigh

[codex.agents]                # [<family>.agents] — <agent>=<comma-separated valid efforts>
gpt-5.6-sol=low,medium,high,xhigh,max,ultra

[delegate.options]            # [<function>.options] — launch flags, not agent rows
codex_mesh=0
```

An **options** section is the odd one out: it holds a function's launch flags
rather than agent rows, `agents_resolve` never reads it, and the consumer that
owns the flag reads its own key with `_agents_registry_get <function>.options
<key>`. It is invisible to the family enumeration — which only ever loops `codex`
and `claude` — so `/agent` neither lists nor edits it. `delegate.options` holds
`codex_mesh` today.

Vocabulary: a **family** is a CLI vendor (`codex` | `claude`); an **agent** is a model within a family (`gpt-5.6-sol`, `opus`); a **function** is a consumer; a **task** is `<function>.<subtask>` — exactly two segments.

Every function carries *both* family sets, fully specified at all times, so a family switch is a one-line edit and never a row edit. The functions and their complete sub-task sets:

| Function | Sub-tasks |
| --- | --- |
| `delegate` | `implementation`, `review`, `architect`, `mechanical`, `escalation` |
| `cli` | `style_fix_review`, `commit_prep`, `merge_branch`, `interactive` |
| `fix` | `style_eval`, `style_eval_review`, `style_fix`, `report` |
| `ask_a_friend` | `consultation`, `implementation` |
| `team_review` | `expert` |
| `api_review` | `reviewer`, `adversary` |
| `module_review` | `reviewer`, `validation` |

`[codex.agents]` is machine-generated (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark` today). `[claude.agents]` is hand-maintained: `fable`, `opus`, `sonnet`, each `low,medium,high,xhigh,max`. All seven functions are assigned `codex`.

### Resolution algorithm and precedence

`agents_resolve <task>` sets `AGENT_FAMILY`, `AGENT_MODEL`, `AGENT_EFFORT` (effort may be empty) and returns nonzero with a stderr message naming the offending piece *and* the allowed values on any failure:

1. Split the task at the first dot. Reject anything that is not exactly two non-empty segments.
2. **Family precedence:** an exact-task key in `[assignments]` (`delegate.review=claude`) wins over the function key (`delegate=codex`). Neither present → error listing the configured assignments.
3. Section `<function>.<family>` must exist → otherwise error listing the families that do have a set for that function.
4. Row `<subtask>` must exist in that section → otherwise error listing the section's sub-tasks.
5. Validate the pair: agent is everything before the first colon, effort everything after. A trailing colon with nothing after it is rejected. The agent must be a key in `[<family>.agents]`; a non-empty effort must appear in that agent's comma list. A catalog row with an *empty* effort list is legal and admits only bare (effort-less) pairs.

Exact-task overrides exist for one-off cross-vendor setups; function-level assignment is the norm and the only thing `/agent <function> <family>` writes.

### Resolver API (`scripts/agents/agents_config.sh`)

Sourcing the file sets `AGENTS_CONFIG_FILE` (overridable, defaults to `~/.claude/config/agents.conf`), `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE`, `CODEX_CATALOG_SYNC_STATE_FILE`, and fires the catalog freshness sync (below).

Public:

- `agents_config_trim <value>` — strip leading/trailing whitespace; the shared primitive.
- `agents_resolve <task>` — the algorithm above; sets `AGENT_FAMILY` / `AGENT_MODEL` / `AGENT_EFFORT`.
- `agents_resolve_print <task>` — resolves, then prints one line: `task=… family=… agent=… effort=…`.
- `agents_list_assignments [filter]` — walks `[assignments]`; for a bare function key it resolve-prints every row of the active set, skipping sub-tasks shadowed by an exact-task override (which are printed once from their own key). Returns nonzero if *any* row fails to resolve; with a filter that matches no assignment it errors.
- `agents_list_function <function>` — prints every row of *both* families for one function as `task=… family=… agent=… effort=… active=yes|no`, then a `# current family: X` line (with `(overrides: …)` when exact-task assignments exist).
- `agents_set_assignment <function> <family>` — validates that every row of `[<function>.<family>]` resolves, then awk-rewrites the `[assignments]` line. Any invalid row → reject, name the row, file untouched.
- `agents_set_all_assignments <family>` — switches **every** `[assignments]` entry, exact-task overrides included, to one family. Validates the whole target set first — a function with no `[<function>.<family>]` section, an override key with no matching row, or any invalid row rejects the switch with the file untouched — then awk-rewrites every assignment line in one pass, preserving trailing inline comments and spacing byte-exactly.
- `agents_set_row <task> <agent>[:<effort>]` — edits one row. The **agent** names the family (the two catalogs share no names), so the row written is the one the agent could only have meant, live or dormant; an agent listed by both catalogs is refused as ambiguous, and an agent whose family has no `[<function>.<family>]` section names the missing section. Validates the pair, then awk-rewrites the row preserving its trailing inline comment and spacing byte-exactly. Sets `AGENT_ROW_FAMILY`, `AGENT_ROW_ACTIVE_FAMILY`, `AGENT_ROW_ACTIVE`. Editing a row never changes which family is live.
- `agents_codex_args` — one line: `-m <agent>`, plus `-c model_reasoning_effort="<effort>"` when effort is non-empty.
- `agents_claude_args` — one line: `--model <agent>`, plus `--effort <effort>` when effort is non-empty.

Both emitters print a single space-joined line meant to be word-split into an argv array (`read -r -a`), never `eval`'d; the codex effort token carries literal embedded quotes and is one argv token.

Private helpers:

- `_agents_config_has_section <section>` / `_agents_config_section_values <section>` — the low-level ini reader (comment-stripped, trimmed rows).
- `_agents_registry_get <section> <key>` — prints the value by **literal** key comparison; returns 0 even on a miss, so it is errexit-safe inside `$(...)`.
- `_agents_registry_has_key <section> <key>` — 0/1 presence, for `if` conditions.
- `_agents_section_keys_inline <section>` — comma-joined key list for error text.
- `_agents_function_families_inline <function>` — families that have a set section for a function.
- `_agents_agent_families_inline <agent>` — families whose catalog lists an agent (one match names its family; two means the catalogs collided).
- `_agents_families_inline` — comma-joined list of families that have an agent catalog, for the unknown-family error.
- `_agents_active_family <function> <subtask>` — the family a task resolves through today, honoring exact-task overrides; shared by `agents_list_function` and `agents_set_row`.
- `_agents_effort_allowed <csv> <effort>` and `_agents_validate_pair <context> <family> <pair>` — pair splitting and catalog validation; `_agents_validate_pair` is what sets `AGENT_MODEL` / `AGENT_EFFORT`.

### Shared launcher (`scripts/agents/agent_exec.sh`)

```
agent_exec.sh <task> <write|readonly> <working_dir> <prompt_file> <output_file> <log_file>
```

Wrong arg count or a bad mode returns 2. A missing prompt file writes `Prompt not found: <path>` to the log file and returns 1. Otherwise it sources `agents_config.sh`, calls `agents_resolve <task>`, reads the prompt into a variable, and dispatches on family (internal entry point `agents_exec_main`):

- **codex** — `codex exec <agents_codex_args> [extra] --ephemeral (--full-auto | --sandbox read-only) -C <working_dir> -o <output_file> "$PROMPT" > <log_file> 2>&1`.
- **claude** — `claude --print (--dangerously-skip-permissions | --permission-mode plan) --settings '{"sandbox":{"enabled":false}}' --verbose --output-format stream-json <agents_claude_args> [extra] -- "$PROMPT"`, executed in a subshell as `( cd "$working_dir" && … > "$log_file" 2>&1 )` because the claude CLI has no `-C`. The streamed JSON log is what `heartbeat_watch.sh` narrates; afterwards `agents_claude_extract_result` (an inline `python3` heredoc) pulls the final `result` event's text into `<output_file>`, preserving the caller contract "output = final answer, log = full log". Claude's own exit code is returned.

`AGENT_EXEC_EXTRA_ARGS` is appended to the family CLI's arg list. `AGENT_EXEC_DRY_RUN=1` prints the fully assembled command — every argv token `printf '%q'`-quoted by `agents_exec_print_argv`, with the redirection suffix, and a `cd <working_dir> && ` prefix on the claude branch — then exits 0 without executing. `agent_exec` exports nothing; consumers that need provenance re-resolve themselves.

### Addressable codex delegates (`scripts/agents/codex_mesh.py`)

`agent_exec`'s codex branch runs `codex exec`, a process nothing outside it can
reach: a delegate launched that way takes one prompt and is unreachable until it
exits. `codex_mesh.py` is the alternative launch path that gives a codex delegate
an address, so peers and the orchestrator can message and interrupt it mid-run
the way they already can a claude delegate. Opt-in per `[delegate.options]
codex_mesh` in the registry (`0` by default), overridable for one run with
`PLAN_DELEGATE_CODEX_MESH`; `implement.sh` reads it and branches.

One `codex app-server` per delegate session, each delegate a thread on it:

```
codex_mesh.py serve  --session-dir <dir>                      # start/reuse, print port
codex_mesh.py start  --session-dir --name --cwd --prompt-file \
                     --summary-file --log-file [--model --effort --sandbox]
codex_mesh.py send   --session-dir --to <name> --message <text>
codex_mesh.py steer  --session-dir --to <name> --message <text>
codex_mesh.py list   --session-dir
codex_mesh.py stop   --session-dir
```

`serve` spawns `codex app-server --listen ws://127.0.0.1:<free port>` detached
(`start_new_session=True`) and records `{port, pid}` in
`<session_dir>/mesh_server.json`; a later call reuses that server when the pid is
still alive. The transport is newline-delimited JSON-RPC over a hand-written
RFC 6455 client — loopback only, so no `--ws-auth` token.

`start` connects, calls `thread/start` then `turn/start`, writes the delegate's
thread id and status into `<session_dir>/mesh_roster.json` under an exclusive
`flock`, and **blocks for the whole turn**, translating the notification stream
into the log file (`agent:`, `exec:`, `edit:`, `thinking`) that
`heartbeat_watch.sh` narrates. On `turn/completed` it writes the final message to
the summary file and exits, so `implement.sh`'s `wait`, heartbeat, awake timer,
and pass recording are untouched.

`send` calls `thread/queue/add`: the message lands at the start of the target's
**next** turn. `steer` calls `turn/steer` with the roster's `expectedTurnId` and
interrupts the turn in flight. Both address a delegate by mesh name through the
roster file, which is why they work from an unrelated process — the orchestrator,
or a peer delegate.

`stop` SIGTERMs the recorded pid (SIGKILL after 5 s) and removes the server file.
`scripts/delegate/end_session.sh` calls it, reading the session directory out of
the run-active marker: the server is detached on purpose so it outlives each
delegate, so the end of the run is the only point that knows nobody needs it.

### Addressable codex delegates (`scripts/agents/codex_mesh.py`)

`agent_exec`'s codex branch runs `codex exec`, which is a closed process: nothing
outside it can hand the running agent a message. `codex_mesh.py` is the alternate
launcher that removes that limit for `/plan:delegate`, and it is used only there.
It is opt-in — `[delegate.options] codex_mesh` in the registry, overridable for a
single run with `PLAN_DELEGATE_CODEX_MESH` — and off by default, so a phase runs
exactly as before until it is set.

Instead of one process per delegate, the session gets one `codex app-server`
(`--listen ws://127.0.0.1:<port>`, newline-delimited JSON-RPC) and each delegate
becomes a **thread** on it. A thread has an id, and an id is an address:

```
codex_mesh.py serve --session-dir DIR             # start/return the session server
codex_mesh.py start --session-dir DIR --name N …  # launch a delegate, block until its turn ends
codex_mesh.py send  --session-dir DIR --to N --message TEXT   # queued; lands at N's next turn
codex_mesh.py steer --session-dir DIR --to N --message TEXT   # interrupts N's running turn now
codex_mesh.py list  --session-dir DIR             # roster: name, status, thread id
codex_mesh.py stop  --session-dir DIR             # reap the session server
```

`start` blocks for the delegate's lifetime — every turn of it, see below — and
writes the same summary and log files `agent_exec` does, so `implement.sh`'s `wait`, heartbeat watch, awake timer
and pass recording are unchanged — the only thing it adds is the address. Two
files carry the session's mesh state, both under the delegate session directory:
`mesh_server.json` (`{port, pid}`) and `mesh_roster.json`
(`{name: {thread_id, turn_id, status}}`, every read-modify-write under
`fcntl.LOCK_EX` because three delegates register concurrently).

The protocol has three traps, each of which cost a run to rediscover:

- `initialize` must pass `capabilities.experimentalApi: true`. Without it every
  `thread/queue/*` method fails `-32600` with nothing naming the missing flag.
- `thread/start`'s `sandbox` takes the SandboxMode **string**
  (`"danger-full-access"`), not the `SandboxPolicy` object the schema shows for
  other fields.
- **The thread must not be `ephemeral`**, even though `agent_exec`'s codex branch
  passes `--ephemeral`. An ephemeral thread refuses `thread/queue/add` outright
  ("ephemeral thread does not support queued submissions") and refuses
  `thread/name/set` as well, so ephemerality costs the mesh the one call peers
  use most. The price of dropping it is the ordinary codex rollout file under
  `~/.codex/sessions`, which is also what makes a delegate's transcript readable
  after the fact. Naming is still best effort — a failed rename is not worth
  losing a delegate over, and the roster file is the address of record.

A delegate is **not one turn**. `send` puts a message in the thread queue and the
server starts a turn for it by itself, so `start` cannot exit at the first
`turn/completed` — that would strand the message and kill the delegate mid-reply.
It instead checks `thread/queue/list` at each turn boundary and keeps streaming
while work remains, plus a short grace window for a queued turn already in
flight, and only then writes the summary and exits. The summary is the last
turn's answer, so a peer's follow-up is reflected in what the orchestrator reads.

The mirror of that: `send` **refuses** a delegate whose roster status is not
`running`. The thread outlives the launcher, so the server would cheerfully start
a turn for a late message with nothing streaming it and nothing writing the
summary. A refusal is better than work no one ever sees.

### Catalog sync (`scripts/agents/sync_codex_catalog.sh`)

Rewrites **only** `[codex.agents]`, leaving every other line of the file byte-identical. It requires `jq`, reads the top-level `model=` from `~/.codex/config.toml` and the visible models (`visibility == "list"`) from `~/.codex/models_cache.json`, and writes `slug=<efforts>` from each model's `supported_reasoning_levels[].effort` (order preserved; no levels → empty list). Details:

- Slugs must match `^[[:alnum:]][[:alnum:]./_-]*$` — notably no colon, which would collide with pair syntax; violators are skipped with a stderr warning.
- If codex's selected model is not in the visible catalog it is prepended, with its cache efforts if present, empty otherwise.
- **Vanished-agent protection:** an awk pass computes every codex-assigned row (function assignments plus exact-task overrides). For any assigned agent that would not survive — absent from the refreshed visible catalog *or* absent from the cache — the sync keeps that agent's previous `[codex.agents]` row and warns, naming the stale row and the fix: `re-point it: /agent <function>.<subtask> <agent>[:<effort>], or switch the family: /agent <function> <family>`. If no previous row exists to preserve, the sync hard-fails.
- **Claude alias staleness:** `warn_missing_claude_aliases` parses the quoted aliases out of `claude --help`'s `--model` text and warns once per alias missing from `[claude.agents]`. Warn-only, never an auto-edit; no `claude` on PATH or help text that parses to nothing degrades to a silent no-op.
- Writes via `mktemp` + `chmod` (copying the original mode) + `mv`, then touches `CODEX_CATALOG_SYNC_STATE_FILE`. `--check` reports staleness with exit 1 and writes nothing.

Two triggers: the launchd job `scripts/agents/com.natemccoy.codex-agent-catalog-sync.plist` (`StartInterval` 300, plus at login), and a freshness gate at the top of `agents_config.sh` — if the state file is missing or either codex source file is newer than it, the sync runs at source time; a failure is warn-and-continue (`WARNING: Codex catalog sync failed; using … as-is.`).

### Administration (`scripts/agents/agent_admin.sh`, `commands/agent.md`)

A thin dispatcher over the resolver:

- no args → `agents_list_assignments` + usage block;
- `skills` → the unique sorted function names from `[assignments]`;
- `<function>` → `agents_list_function` + usage with examples tuned to that function's real subtask and current pair;
- `<family>` alone → `agents_set_all_assignments`, then a `# switched every function to <family>` line and the no-arg listing;
- `<function> <family>` → `agents_set_assignment`;
- `<function>.<subtask> <agent>[:<effort>]` → `agents_set_row`, then a `# updated [<function>.<family>] <task> — live|dormant` line (dormant hints the `agent_admin.sh <fn> <family>` that would make it live), then the function's rows;
- a lone dotted argument or three-plus args → usage on stderr, exit 1.

`commands/agent.md` is the `/agent` skill: it runs the script with `dangerouslyDisableSandbox: true`, relays stdout/stderr exactly, and renders the row lines as a markdown table.

### Consumers

| File | Role |
| --- | --- |
| `config/agents.conf` | The registry. |
| `config/README.md` | The `## agents.conf` section: three-layer schema, `/agent` as the editor, sync behavior. |
| `scripts/agents/agents_config.sh` | Resolver + editors + freshness-gated sync trigger. |
| `scripts/agents/agent_exec.sh` | Family dispatch launcher, dry-run hook. |
| `scripts/agents/codex_mesh.py` | Addressable codex launch path: session app-server, thread per delegate, `send`/`steer`/`list`/`stop`. |
| `scripts/agents/agent_admin.sh` | `/agent` backend. |
| `scripts/agents/sync_codex_catalog.sh` + `.plist` | `[codex.agents]` materialization, staleness warnings. |
| `scripts/agents/codex_mesh.py` | Opt-in addressable launcher for codex `/plan:delegate` delegates: one app-server per session, one thread per delegate, `send`/`steer`/`list`/`stop`. |
| `scripts/agents/heartbeat.sh`, `heartbeat_watch.sh` | Liveness log helpers used by the delegate wrappers (role header block, 60 s beats with an activity digest decoded from the agent log). |
| `scripts/agents/test_agents_config.sh`, `test_agent_exec.sh`, `test_sync_codex_catalog.sh` | Self-contained fixture-conf suites (`mktemp -d`, temp `AGENTS_CONFIG_FILE`, print a "…passed" line, nonzero on failure). |
| `scripts/delegate/implement.sh` / `review.sh` | `/plan:delegate`'s launchers. The implementation launcher adds optional pass kind/activity/fix-count arguments and a **required** `team_role` as its 9th (`impl`, `impl2`, `test`, `review`): both call sites run a three-agent phase team, so there is one artifact shape rather than a solo shape and a team shape. The role suffixes every artifact (`impl_status_<role>`, `impl_summary_<role>.txt`, `impl_agent_<role>.log`, `impl_agent_<role>`, `impl_awake_<role>`), tags the wrapper beats `<subtask>:<role>`, names the slot this dispatch posts under on the board, and is exported to the agent as `PLAN_DELEGATE_TEAM_ROLE` beside `PLAN_DELEGATE_BOARD_DIR`. Only the member the orchestrator gives a `pass_kind` records a progress pass — `start-pass` closes any open pass, so three concurrent recorders would leave the ledger describing whichever finished last. The reviewer adds optional pass activity plus a `pass_index` (7th arg, default 1). Both write status, provenance, agent logs, and shared heartbeat data; when durable progress state exists, they also record the resolved called model/effort and pass outcome through `progress_history.py`. `review.sh` writes `review_findings_<N>.txt` / `review_agent_<N>.log` per pass and `ln -sfn`s the unnumbered names to the current one, so a run that failed to converge can be read back round by round while existing readers keep working. |
| `scripts/delegate/board.sh` | The phase team's coordination substrate: an append-only `board.log` plus `mkdir`-atomic tokens under `locks/`, shared by every member writing into one session directory. Commands are `post` / `read` / `acquire` / `release` / `renew` / `role` / `roles` / `locks`. A file rather than messages because a codex-family delegate has no `ListAgents`/`SendMessage` at all and the orchestrator is asleep between progress ticks — and because one append *is* the broadcast to every peer and the wrapper, where N-1 addressed sends can each half-fail. Post kinds are a closed set (`register`, `claim`, `release`, `ask`, `answer`, `status`, `blocked`, `handoff`) so a peer can scan for what concerns it; `read --since N` numbers every line before filtering, so a cursor counts board positions and stays valid across different filters. `role` is the only writer of `handoff`, and those lines are how `progress_history.py` learns which role each slot holds now. |
| `scripts/delegate/progress_history.py` | Cross-agent append-only plan-delegate event recorder and aggregator. Durable per-run JSONL lives under `~/.local/state/plan-delegate/runs/`; live state remains in the session directory. `start-run` alone resolves and records the project clock from the supplied plan, matching worktree/branch history, or the run start. It renders separate project and phase progress sections with independent unchanged timers and unambiguous `HH:MM:SS` durations, and calibrates phase estimates from completed-phase history. `progress` requires `--cap-stage` on dual-layout calls and clamps the calibrated percentage to that stage's ceiling; `start-phase --work-order-file` records Work Order size metrics. |
| `scripts/delegate/findings.py` | The delegate fix loop's convergence test — what replaced the fix-pass counter. Stable finding IDs (`F001…`) with states `open` / `fixed_pending_review` / `accepted`, held in `findings_state.json` beside the progress state and reset automatically when the active phase's `instance_id` changes. `gate` returns `converged` / `dispatch` / `stop`, gating on blocker+minor in round 1 and blocker only afterwards (nits never gate); `dispatch --covers` refuses a partial batch so one fix round repairs everything gating together. Stops on: a finding that failed to close twice, a finding reopened twice, two rounds with no decrease in the gating-open count, or a 10-round runaway backstop. Appends `finding_opened` / `finding_batch_dispatched` / `finding_verdict` / `finding_gate` to the same durable run JSONL. |
| `scripts/delegate/test_progress_history.py`, `test_findings.py` | `python3 -m unittest scripts.delegate.test_progress_history scripts.delegate.test_findings` from `~/.claude`. Both drive the real CLIs in a temp session dir with `PLAN_DELEGATE_NOW_EPOCH` / `PLAN_DELEGATE_HISTORY_DIR` pinning time and storage. |
| `scripts/cli_agent/cli_agent.sh` | zshrc-alias dispatcher (`review`, `commit_no`, `commit_yes`, `merge`, `code`). `cli_agent_print_status` prints the four `cli.*` rows via `agents_resolve_print`; `cli_agent_run` maps no args → `cli.interactive` REPL and a skill name → `cli.<skill>` (unknown skill errors with the known list), then `exec`s codex with `-c service_tier="fast"` or claude with `-- "/$invocation"`. It has no assignment editor — assignment changes go through `/agent`. |
| `scripts/fix/agent_assignments.sh` | `cf_load_stage_assignment <section> <enabled_var> <family_var> <agent_var> <effort_var>` reads `enabled=` from `agent-assignments.conf`, validates it with `cf_validate_bool`, then fills family/agent/effort from `agents_resolve fix.<section>` (surfacing resolver errors). `cf_print_stage_assignment` / `cf_print_agent_assignments` back the `/fix agent` status view; `cf_trim` and `cf_resolve_checkout` also live here. |
| `scripts/fix/agent-assignments.conf` | Stage enablement only — `[style_eval]` / `[style_eval_review]` / `[style_fix]` with `enabled=`. |
| `scripts/fix/fix.sh` | Driver: loads all three stage assignments before checking `enabled`, logs `family/agent`, and renders the report through `agent_exec fix.report write "$HOME/.claude" <prompt> /tmp/fix-report.txt <log_dir>/report_render.txt` behind an activity grep, with a guarded prompt build and WARN-and-continue. |
| `scripts/fix/style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh` | Stage scripts; `case "$STYLE_AGENT"` dispatches on the *family*, `STYLE_AGENT_MODEL` is the agent, and the codex effort flag is wrapped in an `[[ -n … ]]` guard. |
| `scripts/fix/fix-usage.sh`, `fix_report_parse.py` | Usage screen renders family/agent/effort columns (`<default>` for empty effort); the parser carries the family into `<family>-usage-limit` reason codes via `AGENT_LIMIT_LINE_RE` and the `AgentLimit` dataclass. |
| `scripts/ask_a_friend/ask_a_friend.sh` / `implement.sh` | `ask_a_friend.consultation` / `ask_a_friend.implementation`, both `write` mode; protocol names are their own (`question.md` / `answer.txt`, status `asking→answered\|error`; `implementation_prompt.md` / `impl_summary.txt`, `impl_status`), logs `agent.log` / `impl_agent.log`, provenance `consult_agent` / `impl_agent`. |
| `commands/team_review.md`, `api_review.md`, `module_review.md` | Call `agent_exec … readonly` directly, backgrounded, one call per lens/pass, with self-contained prompt files under wave-namespaced session dirs (`cycle2/`, `adversary/`, `pass3/`) and provenance captured in-session via `agents_resolve_print`. |
| `commands/plan/delegate.md`, `commands/ask_a_friend.md`, `commands/fix.md` | Consumer docs: call sites, log/provenance names, and `/agent` as the switch surface. |

All four wrappers capture resolver stderr into their log (`agents_resolve "$TASK" 2>"$LOG_FILE"`) so an "on error read the log" instruction is true even on the resolution-failure path.

## Invariants

- The registry is the only home for family/agent/effort. Consumers resolve through `agents_resolve` or `agent_exec` and never re-derive flag vocabulary — `agents_codex_args` / `agents_claude_args` own it.
- Every function keeps **both** family sets fully specified, so switching families is a one-row edit; `agents_set_assignment` (and `agents_set_all_assignments`, across every function at once) refuses a switch if any row of the target set fails validation, and leaves the file untouched.
- Agent names stay disjoint between `[codex.agents]` and `[claude.agents]` — `agents_set_row` infers the family from the agent and refuses a name listed by both rather than guessing.
- Task names are exactly two segments. Empty effort means "omit the flag"; `agent:` with nothing after the colon is invalid; a catalog row with an empty effort list is valid and admits only bare pairs.
- Only `agents_set_assignment` and `agents_set_all_assignments` change which family is live. `agents_set_row` writes a row (live or dormant) and never flips liveness.
- The sync rewrites only `[codex.agents]` and never touches assignments. `[claude.agents]` stays hand-maintained; alias warnings never auto-add, vanished-agent warnings never auto-repoint.
- New lookups use `_agents_registry_get` / `_agents_registry_has_key` (literal key comparison). Never match keys with an unescaped `^key=` regex — dotted keys like `delegate.review` mis-match.
- Any awk that writes a user-supplied value into the conf passes it through `ENVIRON`, not `awk -v`. Row rewrites preserve trailing inline comments and spacing byte-exactly; conf writes go through a tmp file + `mv` (mode preserved), never in place.
- `agent_exec` owns all redirection: wrappers must not redirect its stdout/stderr to a file, or dry-run output never reaches them. It exports nothing — provenance comes from the wrapper's own `agents_resolve`.
- Callers pass **absolute** prompt/output/log paths to `agent_exec`.
- `codex_mesh.py` resolves nothing. `implement.sh` resolves model and effort
  through `agents_resolve` exactly as it does for `agent_exec`, then passes them
  as `--model` / `--effort`; the mesh path changes only whether the delegate has
  an address, never which agent runs or at what effort.
- Every delegate in one phase shares one app-server, and its pid file is the only
  record of it. A launch path that starts a server without writing
  `mesh_server.json` leaks a process that nothing will reap.
- Provenance files are four lines: `task=`, `family=`, `agent=`, `effort=`.
- `codex_mesh.py` is a `/plan:delegate` launcher, not a second dispatcher. It
  resolves nothing: `implement.sh` has already resolved family, agent and effort
  through the registry and passes them in as `--model` / `--effort`. Anything
  else that needs a codex agent still goes through `agent_exec`.
- The session app-server is deliberately detached, so it outlives each delegate
  and a peer can still reach a thread between turns. `end_session.sh` is the only
  thing that reaps it (`codex_mesh.py stop`, from the session directory recorded
  in the run-active marker); a launcher that killed it would break the mesh it
  exists to provide.
- A codex delegate's thread ends with its turn. Unlike a claude delegate, whose
  background session stays resumable, a finished codex peer cannot be messaged —
  `<PhaseMesh/>` in `commands/plan/delegate.md` states this, and the register
  line's `reach=` field is what tells a peer which of the two it is addressing.
- The fix pipeline runs unattended via launchd every 10 minutes (`com.natemccoy.style-fix.plist`, `StartInterval=600`, no idle gate). `agents_config.sh`, `agent_assignments.sh`, the three stage scripts, and `fix_report_parse.py` must never be left broken, and the resolver must keep working under `/bin/bash` (3.2).
- `/plan:delegate` is itself implemented by `scripts/delegate/*`, so any rename or signature change to those launchers must land together with the `commands/plan/delegate.md` call-site edits in one change.
- Every `implement.sh` dispatch is a member of a phase team: `team_role` is required, every artifact it writes is suffixed with that role, and at most one member of a phase carries a `pass_kind`. The board, not the launcher, is what a fourth concurrent member would change.
- No `/plan:delegate` prompt tells an agent to acquire the `cargo` token. `verify.sh` takes it, and a prompt that takes it too deadlocks the agent against its own held token.
- `cf_load_stage_assignment` keeps its five-argument out-var signature; `fix-usage.sh` and the print helpers call it positionally.
- The report parser slices launchd runs on exact substrings of the driver's stage-start lines — a reword must keep those leading phrases byte-identical or update the parser in the same change.
- In the fix pipeline stage scripts, do **not** add family guards around the exec-marker transcript filter or the usage-limit detection: the first handles both families by pattern union, the second's codex-worded grep no-ops on claude logs, and the durable lines print `(${STYLE_AGENT} …)` with the parser accepting any family word.
- Before committing, inspect `git diff config/agents.conf`: an unsandboxed run that sources the resolver can legitimately rewrite `[codex.agents]`. Fold sync drift in deliberately or exclude it, never let it ride silently. `settings.json`'s app-generated key reorder stays excluded, and `settings.json`'s `model` / `statusLine`, `~/.zshrc`'s interactive `claude` alias, `scripts/claude_to_codex/`, and `~/.codex/config.toml` are out of scope.
- Never use `AskUserQuestion` in the command docs; the review docs decide via in-session synthesis.

## Calibration / gotchas

- **bash 3.2.** macOS ships bash 3.2 — no associative arrays, no `${var,,}`, no bash-4 anything. The fix pipeline scripts run under `#!/bin/bash`; the registry, delegate, cli_agent, and ask_a_friend scripts use `#!/usr/bin/env bash` (also 3.2 here).
- **bash only, never zsh.** The resolver uses `BASH_REMATCH` and process substitution. Sourcing it from zsh hangs or misbehaves — run every test and probe as `bash <script>` / `bash -c '…'`.
- **Sourcing fires the sync.** `agents_config.sh`'s freshness gate can shell out to the sync (which itself shells out to `claude --help`, ~1-2 s) and can hang in a network-blocked sandbox. Tests and probes suppress it by exporting `CODEX_CATALOG_SYNC_STATE_FILE` at a freshly `touch`ed temp file *before* sourcing.
- **Sandbox write denials.** `~/.claude/config` is a sandbox deny path and `~/.local/state/` is outside the write allowlist, so anything that rewrites `config/agents.conf` or completes the sync (`/agent` edits, conf round-trips, warming the freshness gate) must run with `dangerouslyDisableSandbox: true`. Sandboxed, only the `mktemp` fails and the sync's warn-and-continue masks it as a merely-stale catalog.
- **Double resolution per launch.** Wrapper (provenance) plus `agent_exec` (execution) each source `agents_config.sh`, so a stale freshness gate fires the sync twice and doubles its `WARNING:` lines. Expected, not a defect.
- **Absolute paths for `agent_exec`.** The claude branch redirects after `cd <working_dir>`, so relative output/log paths resolve against `working_dir` there but against the caller's cwd on the codex branch (the prompt file is read pre-`cd` in both).
- **`initialize` needs `capabilities.experimentalApi: true`.** Without it the
  app-server accepts the handshake and then fails `thread/queue/add` and
  `turn/steer` with `-32600` and no indication a capability is missing.
- **`thread/start` takes the SandboxMode *string*** (`"danger-full-access"`), not
  the `SandboxPolicy` object (`{"type": "dangerFullAccess"}`) the generated
  schema shows for other fields. The object is rejected as `unknown variant`.
- **`ephemeral: true` and `thread/name/set` are incompatible** — the rename
  returns `-32600 "ephemeral thread does not support metadata updates"`. Delegates
  are ephemeral to match `codex exec --ephemeral`, so naming is best-effort and
  `mesh_roster.json` is the address of record.
- **`--listen unix://<path>` closes every connection silently** while the server
  stays up. Use `ws://127.0.0.1:<port>`.
- **`codex queue --thread` exits 0 for a thread with no live session.** The
  acknowledgement says nothing about delivery — do not use it as a reachability
  test.
- **A finished codex delegate is gone.** Its thread ends with its turn, unlike a
  claude background session, which a message resumes from its transcript. `send`
  to a delegate whose roster status is not `running` will not be read.
- **`AGENT_EXEC_EXTRA_ARGS` is whitespace-split with no quote interpretation.** Flag+value pairs (`--add-dir /path`) work; no single argument may contain a space — no prompt preambles, no `--settings` JSON.
- **`AGENT_EXEC_DRY_RUN=1`** is the testing hook: `%q`-quoted argv plus redirection suffix, with a `cd <dir> && ` prefix on the claude branch. Match smoke checks on substrings (`--full-auto`, `--sandbox read-only`, `-m <agent>`, the effort word), never whole lines — the codex effort token renders with escaped quotes (`model_reasoning_effort=\"high\"`).
- **awk gotchas.** `function` is a reserved awk word — pass it as `-v fn=`. `awk -v` decodes backslash escapes, so a value containing `\n` / `\t` would corrupt the row; user-supplied values go through `ENVIRON["…"]`.
- **`codex --sandbox read-only` panics** codex's system-configuration crate on macOS in some contexts, which is why ask_a_friend's conceptually read-only consult runs `write` (`--full-auto`). The delegate reviewer's `--sandbox read-only` usage is proven and stays. Codex launched from a Claude Code session needs `dangerouslyDisableSandbox: true` (and usually `run_in_background: true`).
- **`service_tier="fast"`** is passed on both codex paths in `cli_agent.sh`, including the interactive REPL — keep it.
- **Last-writer-wins on conf rewrites.** The source-time sync and the `/agent` editors both rewrite the file with tmp + `mv` and no locking; interleaved writers can silently revert each other's change but never corrupt the file.
- **claude-family output needs `python3`.** The claude branch logs stream-JSON and extracts the final result event into the output file; without `python3` the output file would be empty even though the log is complete. Claude-family `readonly` reviewers running the style loader script under `--permission-mode plan --print` is untested — a family switch may silently degrade style loading.
- **`_agents_registry_get` returns 0 on a miss** (prints nothing) so it is safe under `set -e` in command substitution; use `_agents_registry_has_key` when you need presence as a condition.
- **The `cargo` build token belongs to `verify.sh`, and never to a prompt.** `verify.sh` acquires `board.sh … cargo` itself from `PLAN_DELEGATE_BOARD_DIR` / `PLAN_DELEGATE_TEAM_ROLE` and releases it from one unified EXIT/INT/TERM path, so concurrent team members serialize their builds without being asked to. Putting the acquire in a prompt as well makes that agent wait out the full `--wait` against a token it already holds, and the symptom — a member that just sits there — is indistinguishable from a slow test. With either env var unset the token step is skipped entirely, which is what keeps a standalone `verify.sh` run unchanged.
- **A green `verify.sh` only means what the tree it ran against means.** With three members editing one worktree, a pass is authoritative for a slot's work only after that slot has posted `done` to the board.
- **cargo-berth claims are per harness session id, so cross-slot edits are blocked, not merged.** Three delegates are three claim holders: the tester cannot add a `#[cfg(test)]` block to a file `impl` claimed, which is why the contract routes it to an integration test under `tests/`.
- **Editing a live launcher in place** (a script currently running) produces a spurious `unexpected EOF` exit 2 after the real work completes — bash re-reads the modified file at a stale byte offset. Check the status file and diff before treating it as a failure.
- Sync `WARNING:` lines land in launchd stderr logs; they are not stage failures, and the usage-limit regexes are written not to match them.

## Why it is this way

- **Warn-and-keep for vanished codex models.** If the sync simply dropped a model an assignment still uses, `agents_resolve` would hard-fail at the config layer and wedge the whole registry — `agents_list_assignments`, `/agent status`, every `cf_load_stage_assignment`, including the unattended 10-minute launchd run. Keeping the previous catalog row leaves resolution green so a truly retired model fails only in that one stage's own execution log, and the warning names the stale row plus the exact `/agent` commands to fix it. The keep condition is deliberately "would this row still resolve against the refreshed catalog *and* the cache", not bare cache membership: a hidden-but-cached assigned model still disappears from the sync output, and a selected-but-uncached model is prepended with an empty effort list, so cache membership alone would still wedge `agent:effort` assignments. Auto-repointing assignments was rejected — assignment edits are a human call.
- **Warn-only for claude alias staleness.** The effort list for a new alias is a judgment call and omissions (haiku) are deliberate, so the sync reports and never edits. A help-text wording change or a missing `claude` binary degrades to a no-op rather than a false edit.
- **No locking on conf writes.** Interleaved writers can only lose an edit, never corrupt the file, and this is a single-user machine; a lock would add failure modes (stale locks in launchd context) worse than the loss it prevents.
- **No per-project overrides.** One global `config/agents.conf` governs everything. Per-project layering would reintroduce exactly the scattered, hard-to-audit assignment state the registry exists to prevent, and `/agent status` would stop being the truth.
- **Double resolution in the wrappers.** `agent_exec` deliberately exports nothing, so a wrapper that wants provenance resolves again itself. Both reads hit the same conf, so they agree; the alternative — `agent_exec` exporting or writing resolved values — would couple every consumer to the launcher's variable names and make the launcher responsible for file layout it does not own.
- **Effort omission for a bare agent pair.** A row of just `agent` means "omit the effort flag entirely and let the CLI pick", which is materially different from any explicit level and is the only thing that validates against a catalog row with no reasoning levels. Hence no default-effort fallback anywhere: the stage scripts guard the flag with `[[ -n … ]]` rather than substituting a level, so the registry's silence is transmitted faithfully to the CLI.
