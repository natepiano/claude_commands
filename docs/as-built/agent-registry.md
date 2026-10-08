# Agent Registry — one place for family, agent, and effort assignments

## What it is

Every external-CLI agent this configuration launches — `/unit:delegate`'s implementer and reviewer, the production unit directors `add_unit.py` starts, the `~/.zshrc` CLI aliases, the unattended fix style pipeline and its report render, `/ask_a_friend`, and the `/team_review` / `/api_review` / `/module_review` review teams — resolves which vendor CLI to run, which model, and at what reasoning effort from one file (`config/agents.conf`) through one resolver (`scripts/agents/agents_config.sh`), and launches through one dispatcher (`scripts/agents/agent_exec.sh`). The problem it solves: without a registry each consumer carries its own private assignment state in its own conf file and hard-codes its own vendor flags, so switching a function between vendors means editing several scripts and auditing "what runs what" means reading all of them. Here, a major function switches between the `codex` and `claude` families, or a single sub-task is re-pointed to a different model or effort, with one `/agent` edit, and no consumer assembles vendor flags itself.

## How it works

### Registry schema

`config/agents.conf` is an INI-style file with four kinds of section. Comments (`#`) are stripped to end-of-line everywhere, including trailing inline comments on rows.

```ini
[assignments]                 # <function>=<family>, plus optional <function>.<subtask>=<family>; `caller` = the family of the agent running it
delegate=codex

[delegate.codex]              # [<function>.<family>] — <subtask>=<agent>[:<effort>]
implementation=gpt-5.6-terra:xhigh
review=gpt-5.6-sol:xhigh

[codex.agents]                # [<family>.agents] — <agent>=<comma-separated valid efforts>
gpt-5.6-sol=low,medium,high,xhigh,max,ultra

[delegate.options]            # [<function>.options] — launch flags, not agent rows
codex_mesh=1
codex_service_tier=fast
```

An **options** section is the odd one out: it holds a function's launch flags
rather than agent rows, and the consumer that owns the flag reads its own key with
`_agents_registry_get <function>.options <key>`. It is invisible to the family
enumeration — which only ever loops `codex` and `claude` — so `/agent` does
not list it. `delegate.options` holds `codex_mesh` and `codex_service_tier`;
`cli.options` holds `codex_service_tier`. The tier keys — `codex_service_tier`
and a row's own `codex_service_tier.<subtask>` — are the ones `agents_resolve`
reads itself, because the tier is a codex CLI flag like the effort (step 6
below), and the ones `/agent` edits (`agents_set_service_tier`).

Vocabulary: a **family** is a CLI vendor (`codex` | `claude`); an **agent** is a model within a family (`gpt-5.6-sol`, `opus`); a **function** is a consumer; a **task** is `<function>.<subtask>` — exactly two segments.

Every function carries *both* family sets, fully specified at all times, so a family switch is a one-line edit and never a row edit, except a function whose rows name exactly one family: it is pinned to that family (`production` has only `[production.claude]`, for unit directors). Every-function switches keep a pinned function and print `# kept <function> on <family>: its only set`; switching it alone to the other family is refused. The functions and their complete sub-task sets:

| Function | Sub-tasks |
| --- | --- |
| `delegate` | `impl`, `test`, `fix`, `review` |
| `cli` | `style_fix_review`, `commit_prep`, `merge_branch`, `interactive` |
| `fix` | `style_eval`, `style_eval_review`, `style_fix`, `report` |
| `ask_a_friend` | `consultation` |
| `team_review` | `expert` |
| `api_review` | `reviewer`, `adversary` |
| `module_review` | `reviewer`, `validation` |
| `production` | `director` |

`[codex.agents]` is machine-generated (`gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`, `gpt-5.3-codex-spark` today). `[claude.agents]` is hand-maintained: `fable`, `opus`, `sonnet`, each `low,medium,high,xhigh,max`. Six functions are assigned `codex`, `production` is assigned `claude` (its only set; `director=opus:xhigh`), and `ask_a_friend` is assigned `caller` — it runs on the family of the agent asking, because only a like-to-like pair talks both ways (claude reaches claude by `SendMessage`, codex reaches codex through `codex_mesh.py`, and a codex friend has no route back to a claude caller).

### Resolution algorithm and precedence

`agents_resolve <task>` sets `AGENT_FAMILY`, `AGENT_MODEL`, `AGENT_EFFORT` (effort may be empty), `AGENT_SERVICE_TIER` (codex only, may be empty) and returns nonzero with a stderr message naming the offending piece *and* the allowed values on any failure:

1. Split the task at the first dot. Reject anything that is not exactly two non-empty segments.
2. **Family precedence:** an exact-task key in `[assignments]` (`delegate.review=claude`) wins over the function key (`delegate=codex`). Neither present → error listing the configured assignments. A value of `caller` maps to the running agent's family — `AGENTS_CALLER_FAMILY` if set, else `CODEX_THREAD_ID` ⇒ codex, else `CLAUDE_CODE_SESSION_ID` ⇒ claude (codex wins when both are set: a codex launched from a claude session inherits claude's variable); none detectable → error naming the override.
3. Section `<function>.<family>` must exist → otherwise error listing the families that do have a set for that function.
4. Row `<subtask>` must exist in that section → otherwise error listing the section's sub-tasks.
5. Validate the pair: agent is everything before the first colon, effort everything after. A trailing colon with nothing after it is rejected. The agent must be a key in `[<family>.agents]`; a non-empty effort must appear in that agent's comma list. A catalog row with an *empty* effort list is legal and admits only bare (effort-less) pairs.
6. Speed tier, codex family only: `[<function>.options] codex_service_tier.<subtask>`, else `codex_service_tier`, must be `fast`, `flex`, `default`, or `pace`; `pace` takes the tier `scripts/whoami/codex_pacer.py` last wrote (`fast` or `default`), and `default` once that is over ten minutes old or missing. An absent key leaves `AGENT_SERVICE_TIER` empty and the launch inherits `service_tier` from `~/.codex/config.toml`. A claude row always gets an empty tier, because Claude's fast mode bills extra usage. An unknown value is an error, because codex silently runs an unrecognised tier at standard speed.

Exact-task overrides exist for one-off cross-vendor setups; function-level assignment is the norm and the only thing `/agent <function> <family>` writes.

### Resolver API (`scripts/agents/agents_config.sh`)

Sourcing the file sets `AGENTS_CONFIG_FILE` (overridable, defaults to `~/.claude/config/agents.conf`), `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE`, `CODEX_CATALOG_SYNC_STATE_FILE`, and fires the catalog freshness sync (below).

Public:

- `agents_config_trim <value>` — strip leading/trailing whitespace; the shared primitive.
- `agents_resolve <task>` — the algorithm above; sets `AGENT_FAMILY` / `AGENT_MODEL` / `AGENT_EFFORT` / `AGENT_SERVICE_TIER`.
- `agents_resolve_print <task>` — resolves, then prints one line: `task=… family=… agent=… effort=… tier=…`. `tier` is the resolved codex tier, `inherit(<x>)` when the registry sets none and `~/.codex/config.toml` gives `<x>` (bare `inherit` when it gives none), or `-` for a claude row.
- `agents_list_assignments [filter]` — walks `[assignments]`; for a bare function key it resolve-prints every row of the active set, skipping sub-tasks shadowed by an exact-task override (which are printed once from their own key). Returns nonzero if *any* row fails to resolve; with a filter that matches no assignment it errors. A `caller` function prints the detected family's rows plus `# <fn>: caller — the calling agent's family (<fam> here)`, or both families' rows plus `(none detectable here)`.
- `agents_list_function <function>` — prints every row of *both* families for one function as `task=… family=… agent=… effort=… active=yes|no tier=…` (a dormant codex row shows its stored tier), then a `# current family: X` line (with `(overrides: …)` when exact-task assignments exist). For a `caller` function `active=yes` marks the detected family's rows and the line reads `# current family: caller — the calling agent's family (X here|none detectable here)`.
- `agents_set_assignment <function> <family>` — validates that every row of `[<function>.<family>]` resolves, then awk-rewrites the `[assignments]` line. Any invalid row → reject, name the row, file untouched. A `caller` function has no switch — rejected, pointing at row edits — and `caller` is not a switch target. A pinned function switched to the other family is refused with `ERROR: '<function>' runs only on <family>: [<function>.<family>] is its only set.`
- `agents_set_all_assignments <family>` — switches **every** `[assignments]` entry, exact-task overrides included, to one family. Validates the whole target set first — a function with no `[<function>.<family>]` section, an override key with no matching row, or any invalid row rejects the switch with the file untouched — then awk-rewrites every assignment line in one pass, preserving trailing inline comments and spacing byte-exactly. `caller` lines are skipped: neither validated against the target nor rewritten. A pinned function is kept the same way and named in `AGENT_KEPT_FUNCTIONS`.
- `agents_set_model <agent> [function]` — puts every function, or one, on one agent, keeping each row's effort. The agent names its family, as in `agents_set_row`. Every fixed assignment in scope, exact-task overrides included, switches to that family, and every row of each `[<function>.<family>]` set takes the agent. A `caller` function keeps its assignment, but its set for that family takes the agent too, since that set is live whenever an agent of that family asks. With no function named, a pinned function keeps its assignment and rows and is named in `AGENT_KEPT_FUNCTIONS`; named alone with an agent of the other family, it is refused with the pinned error. Validates the whole change first — an unknown or ambiguous agent, an agent given with `:<effort>`, a missing set, an override with no row, or a kept effort the agent's catalog lacks rejects it with the file untouched — then one awk pass rewrites the assignment and row lines, preserving trailing comments and spacing. Sets `AGENT_SWEEP_FAMILY`.
- `agents_set_row <task> <agent>[:<effort>]` — edits one row. The **agent** names the family (the two catalogs share no names), so the row written is the one the agent could only have meant, live or dormant; an agent listed by both catalogs is refused as ambiguous, and an agent whose family has no `[<function>.<family>]` section names the missing section, and an agent of the other family on a pinned function gets the pinned error. Validates the pair, then awk-rewrites the row preserving its trailing inline comment and spacing byte-exactly. Sets `AGENT_ROW_FAMILY`, `AGENT_ROW_ACTIVE_FAMILY`, `AGENT_ROW_ACTIVE`. Editing a row never changes which family is live.
- `agents_set_service_tier <fast|flex|default|inherit> [<function>|<function>.<subtask>]` — writes `codex_service_tier` (or `codex_service_tier.<subtask>`) in `[<function>.options]` for every function with a codex set, one function, or one row; `inherit` deletes the key. A function or every-function write also deletes the row keys beneath it, so each codex row in scope ends up on the named tier. Refuses an unknown tier word, function, or codex row, and a function with no codex set, with the file untouched. A missing options section is created after the function's last row, and one that `inherit` empties is removed with the blank line above it, so a set and its `inherit` round-trip byte for byte. Two awk passes over the file (plan, then write); replaces a key in place, preserving its trailing comment. Never changes which family is live. Sets `AGENT_TIER_FUNCTIONS`.
- `agents_codex_args` — one line: `-m <agent>`, plus `-c model_reasoning_effort="<effort>"` when effort is non-empty and `-c service_tier="<tier>"` when the tier is.
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
- `_agents_stored_service_tier <function> <subtask>` — the row key, else the function key, unvalidated; shared by resolution and `agents_list_function`.
- `_agents_codex_config_tier` / `_agents_tier_display <family> <tier>` — the top-level `service_tier` in `~/.codex/config.toml`, and the `tier=` field built from it.
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
an address, so peers and the unit director can message and interrupt it mid-run
the way they already can a claude delegate. It launches `/unit:delegate`'s codex
seats and `/ask_a_friend`'s codex friend (`start --resident`). For delegates it
is selected per `[delegate.options] codex_mesh` in the registry (`1` as shipped,
so the mesh is the normal codex launch path), overridable for one run with
`PLAN_DELEGATE_CODEX_MESH`; `implement.sh` reads it and branches, and `0`, or a
missing key, runs a phase on `codex exec` instead.

One `codex app-server` per delegate session, each delegate a **thread** on it. A
thread has an id, and that id is the delegate's address:

```
codex_mesh.py serve  --session-dir <dir>                      # start/reuse, print port
codex_mesh.py start  --session-dir --name --cwd --prompt-file \
                     --summary-file --log-file [--reply-file --model --effort \
                     --service-tier --sandbox --timeout --resident]
codex_mesh.py send   --session-dir --to <name> (--message <text> | --message-file <path>)
codex_mesh.py steer  --session-dir --to <name> (--message <text> | --message-file <path>)
codex_mesh.py end    --session-dir --to <name>
codex_mesh.py list   --session-dir                            # roster: name, status, thread id
codex_mesh.py stop   --session-dir
```

`serve` spawns `codex app-server --listen ws://127.0.0.1:<free port>` detached
(`start_new_session=True`) and records `{port, pid}` in
`<session_dir>/mesh_server.json`; a later call reuses that server when the pid is
still alive. Starting one takes an exclusive `flock` on
`<session_dir>/mesh_server.lock`, because the seats of a phase reach
`ensure_server` inside the same second and unlocked each would start a server of
its own, orphaning two. `ensure_server` returns the port **and whether this call
is what started it** — the flag the retry below turns on. The transport is
newline-delimited JSON-RPC over a hand-written RFC 6455 client — loopback only,
so no `--ws-auth` token.

`start` connects, calls `thread/start` then `turn/start`, writes the delegate's
entry into `<session_dir>/mesh_roster.json`
(`{name: {thread_id, turn_id, status, launcher_pid?}}`, `launcher_pid` on a
`waiting_capacity` entry and on a follow-up's `starting` and `running` entries,
which also carry `previous_status`; every read-modify-write under `fcntl.LOCK_EX`, because
the delegates register concurrently), and **blocks until its last turn ends**,
translating the notification stream into the log file (`agent:`, `exec:`,
`edit:`, `thinking`) that `heartbeat_watch.sh` narrates. On a `turn/completed`
with nothing queued it writes the final message to the summary file — or, given
`--reply-file` as `implement.sh` passes, to that file, filling the summary only
when the delegate left it empty — and exits, so `implement.sh`'s `wait`,
heartbeat, awake timer, and pass recording are untouched.

A delegate is **not one turn**. `send` puts a message in the thread queue and the
server starts a turn for it by itself, so exiting at the first `turn/completed`
would strand the message and kill the delegate mid-reply. At each turn boundary
`start` checks `thread/queue/list` and the thread's state, then watches
`QUEUE_GRACE_SECS` (3 s) for a queued turn already in flight, and keeps
streaming while work remains. The summary is the last turn's answer, so a peer's
follow-up is reflected in what the unit director reads. With `--resident`
(ask_a_friend's friend) `start` stays attached across turns instead: each
finished turn's reply is printed and delivered as it lands, the thread keeps
accepting `send`, and only `end` releases the block.

`send` and `steer` address a delegate by mesh name through the roster file, which
is why they work from an unrelated process — the unit director, or a peer
delegate. `send` delivers only while a launcher is attached. To a `running` seat
it calls `thread/queue/add`, and the message lands at the start of the target's
**next** turn; to a `waiting_capacity` seat it stores the message in
`<seat>.pending.json` under an `flock`, for the resume turn. It refuses a
`capacity_exhausted` seat or one being ended (exit 2), and any other status
(`done`, `failed`) with `<seat> is <status>, not running; read its summary file`
(exit 1): the thread outlives the launcher, so the server would start a turn for
a late message with nothing streaming it and nothing writing the summary.
`steer` needs a `running` seat; it calls `turn/steer` with the roster's
`expectedTurnId` and interrupts the turn in flight. `end` accepts `running`,
`waiting_capacity` and `capacity_exhausted` (`ENDABLE_STATUSES`): it drops the
thread's queued messages, interrupts any live turn, and leaves the marker the
`start` loop polls for. Any other status prints `<seat> is <status>; nothing to
end` and exits 0.

`stop` SIGTERMs each recorded pid (SIGKILL after 5 s) and removes the server
file. `scripts/delegate/end_session.sh` calls it, reading the session directory
out of the run-active marker: the server is detached on purpose so it outlives
each delegate, so the end of the run is the only point that knows nobody needs
it. That is also why it reaps `mesh_retired.json` as well as the live record —
a server the retry below abandoned is left running, since it may still be
finishing a peer delegate's turn.

**Recovering from a wedged app-server.** A stuck server answers every thread
that attaches with a provider message it cached earlier — no round trip — so a
usage limit that was real hours ago is replayed word for word to work that would
have succeeded. Reading the message cannot tell that from a live refusal; the
launcher settles it by running the work again on a server it starts itself,
which has cached nothing. `command_start` retries once, through `_retry_warranted`, when all of these hold: the run ended `FailedBeforeThread` (no thread exists, so repeating the prompt cannot repeat edits), the server was inherited rather than started by this call, the failure arrived within `RETRY_FAST_FAILURE_SECS` (120), and the delegate is not resident. A run that failed after `thread/start` is `FailedWithThread` and is never retried, since its prompt may already have done work. `_retire_server`
drops the record only when it still names the server that failed, so seats
racing the same recovery replace one server between them rather than one each.
Same failure on the fresh server means the provider really did refuse, and it is
reported unchanged.

**A proven usage limit reaches the quota alert.** When a start fails with the
provider's usage-limit words on a server the launcher started or retried on, or
after a non-resident run longer than `RETRY_FAST_FAILURE_SECS`, `command_start`
runs `scripts/whoami/agent_notes.py blocked` (`_quota_refused`,
`_report_quota_refusal`). `quota_alert.blocked()` then moves every function on
codex to claude, but only while the active Codex account is at or under the
alert threshold, and tells every alert recipient at once. A fast refusal from an
inherited server that was never retried proves nothing and is not reported.

**Model at capacity.** A turn ends `TurnCompleted`, `TurnRefusedForCapacity` or `TurnFailed`; a run ends `RunCompleted`, `FailedBeforeThread`, `FailedWithThread` or `CapacityRetriesExhausted`. Capacity is the error's `codexErrorInfo` `serverOverloaded` / `flexUnavailable`, or the message `Selected model is at capacity`. On a refusal the launcher keeps the same thread and roster entry and owes the seat a resume turn. It moves messages queued for the thread to `<seat>.pending.json`, reads the thread (`_read_live_turn`), and, if a queued peer turn is already running, streams that turn first with the resume still owed. Once the thread reads `ThreadIdle`, it marks the entry `waiting_capacity` with its own `launcher_pid`, logs `capacity retry N: next turn at <time>`, waits, and starts a resume turn: `Your last turn stopped because the model was at capacity. Continue from where you stopped; your edits are already in the tree.`, followed by the held messages. The original prompt is never sent again. Before each resume it checks, under the pending-file lock, that no `end` marker exists and the roster still names this thread; otherwise it logs `capacity launcher ended|replaced; no resume turn started` and returns `FailedWithThread`.

The wait budget is per busy spell. Within a spell the wait is `CAPACITY_WAIT_SECS` (30 s) doubling to `CAPACITY_MAX_WAIT_SECS` (300 s), with `CAPACITY_BUDGET_SECS` (1200 s) in all. `capacity_waited` and `capacity_retries` reset at one site in `_attach_and_run`, the `else` of the capacity check, just before the `resume_owed` branch: any turn that ends without a capacity refusal resets them, a peer turn that completes while a resume is owed included. Only a capacity refusal earns a wait, so an owed resume starts at once after a turn that ended any other way. A refusal that finds the budget spent sets the entry to `capacity_exhausted`, drops the thread's queue and interrupts any live turn (`_end_unwatched_turn`), and `start` exits 1 with `codex_mesh: <seat>: model still at capacity after 7 retries over 20 min; thread <id> stays on the roster (codex_mesh.py end --to <seat>)`.

After a refused turn the app-server reports the thread as `systemError`, and `_read_live_turn` reads that as `ThreadIdle`, so the wait and the resume go ahead. Upstream (codex rust-v0.160.1), the app-server sets `systemError` after any failed turn, a capacity refusal (`server_overloaded`) included, and `note_turn_started` clears it (`app-server/src/thread_status.rs`, `bespoke_event_handling.rs`). The protocol's `ThreadStatus` is `notLoaded | idle | systemError | active{activeFlags}` (`v2/ThreadStatus.ts` from `codex app-server generate-ts`).

**Relaunching a seat.** `start` on a name already on the roster handles the old entry by its status. A `failed` entry first repeats the dead launcher's cleanup, `_end_unwatched_turn(port, thread_id, log_path) -> RelaunchAllowed | RelaunchBlockedByLiveTurn`; blocked exits 2 with `codex_mesh: <seat>: thread <id> still has a live turn that could not be interrupted; relaunch once it ends`, and anything else relaunches, even when the old thread cannot be read. A `waiting_capacity` entry whose `launcher_pid` is alive exits 2 and names `codex_mesh.py end --to <seat>`; a dead one relaunches. Any other entry, while the session's server is up, is read through `thread/read`. `_read_live_turn(client, thread_id) -> ThreadLive(turn_id) | ThreadActiveWithoutTurn | ThreadIdle | ThreadStateUnknown(reason)` maps the thread's status: `idle`, `notLoaded` and `systemError` are `ThreadIdle`; `active` is `ThreadLive` when an `inProgress` turn carries an id, else `ThreadActiveWithoutTurn`; any other status, a read error or a missing thread is `ThreadStateUnknown`. `start` replaces the entry only on `ThreadIdle`. `ThreadStateUnknown` exits 2 with `codex_mesh: <seat>: thread <id>'s state could not be read (<reason>); use codex_mesh.py end --to <seat> before relaunching`, and a live turn exits 2 with `codex_mesh: <seat>: thread <id> has a live turn; use codex_mesh.py end --to <seat> before relaunching`. A thread left `systemError` by a refused turn reads `ThreadIdle`, so its seat relaunches, and `end` sends it no `turn/interrupt`.

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
- `<family>` alone → `agents_set_all_assignments`, then a `# switched every function to <family>` line, a `# kept <fn> on <family>: its only set` line per pinned function, and the no-arg listing;
- `<agent>` alone → `agents_set_model`, then `# switched every function to <agent> (<family>), efforts kept`, the `# kept …` lines, and the no-arg listing;
- `<function> <family>` → `agents_set_assignment`;
- `<function> <agent>` → `agents_set_model <agent> <function>`, then a `# switched …` line (or `# set [<fn>.<family>] …` for a `caller` function) and the function's rows;
- `<function>.<subtask> <agent>[:<effort>]` → `agents_set_row`, then a `# updated [<function>.<family>] <task> — live|dormant` line (dormant hints the `agent_admin.sh <fn> <family>` that would make it live), then the function's rows;
- a tier word (`fast`, `flex`, `default`, `inherit`) alone, after `<function>`, or after `<function>.<subtask>` → `agents_set_service_tier`, checked before the family and agent branches, then a `# set [<fn>.options] <key>=<tier> — live|dormant|live whenever a codex session runs <fn>` line (every function: `— dormant for <fns>: they run on claude` when any do), or for `inherit` a `# cleared …` line naming what the scope now follows; then the function's rows, or the no-arg listing for every function;
- a lone dotted argument or three-plus args → usage on stderr, exit 1.

`commands/agent.md` is the `/agent` skill: it runs the script with `dangerouslyDisableSandbox: true`, relays stdout/stderr exactly, and renders the row lines as a markdown table.

### Consumers

| File | Role |
| --- | --- |
| `config/agents.conf` | The registry. |
| `config/README.md` | The `## agents.conf` section: three-layer schema, `/agent` as the editor, sync behavior. |
| `scripts/agents/agents_config.sh` | Resolver + editors + freshness-gated sync trigger. |
| `scripts/agents/agent_exec.sh` | Family dispatch launcher, dry-run hook. |
| `scripts/agents/codex_mesh.py` | Addressable codex launcher: the default for codex `/unit:delegate` seats and the path for `/ask_a_friend`'s codex friend (`start --resident`). One app-server per session, one thread per delegate, `send`/`steer`/`end`/`list`/`stop`, `follow`/`can-follow`/`release-follow` (a new turn on a finished seat's thread, for `implement.sh --to`), and a capacity backoff that resumes the same thread. |
| `scripts/agents/agent_admin.sh` | `/agent` backend. |
| `scripts/agents/sync_codex_catalog.sh` + `.plist` | `[codex.agents]` materialization, staleness warnings. |
| `scripts/agents/heartbeat.sh`, `heartbeat_watch.sh` | Liveness log helpers used by the delegate wrappers (role header block, 60 s beats with an activity digest decoded from the agent log). |
| `scripts/agents/test_agents_config.sh`, `test_agent_exec.sh`, `test_sync_codex_catalog.sh` | Self-contained fixture-conf suites (`mktemp -d`, temp `AGENTS_CONFIG_FILE`, print a "…passed" line, nonzero on failure). |
| `scripts/delegate/implement.sh` / `review.sh` | `/unit:delegate`'s launchers. The implementation launcher adds a **required** pass kind as its 6th argument, optional activity and fix-count arguments, and a **required** `team_role` as its 9th (`impl` or `test`; the legacy `review` writer seat is still accepted): a phase runs a two-seat team and a repair its one `impl` seat, so one artifact layout covers both rather than a solo set and a team set. The role suffixes every artifact (`impl_status_<role>`, `impl_summary_<role>.txt`, `impl_agent_<role>.log`, `impl_agent_<role>`, `impl_awake_<role>`), tags the wrapper beats `<subtask>:<role>`, names the slot this dispatch posts under on the board, and is exported to the agent as `PLAN_DELEGATE_TEAM_ROLE` beside `PLAN_DELEGATE_BOARD_DIR`. Every member records its own progress pass: `state["pass"]` holds one record per seat, and `start-pass` closes only a stale pass of the same seat, so three concurrent recorders no longer leave the ledger describing whichever finished last. The launcher also stamps `role=<name>` on its `register` line, which is what fills the progress table's per-slot columns before any agent posts. The kind is checked against the four words at the argument, because an empty one used to be tolerated: the launcher skipped `start-pass`, ran its agent normally, and left the seat's previous pass standing -- so a repair round dispatched with the kind on `impl` alone produced a phase whose ledger held one live window and two records closed `error` an hour before, and refused every progress call once that one window closed. Nothing in the sequence looked like a launch fault; a register line with no `role=` field is the signature. The reviewer adds optional pass activity, a `pass_index` (7th arg, default 1), and a `lens` (8th, before the early-ready sentinel that follows it). Both write status, provenance, agent logs, and shared heartbeat data; when durable progress state exists, they also record the resolved called model/effort and pass outcome through `progress_history.py`. `review.sh` writes `review_findings_<N>.txt` / `review_agent_<N>.log` per pass and `ln -sfn`s the unnumbered names to the current one, so a run that failed to converge can be read back round by round while existing readers keep working. A phase's broad review runs three reviewers at once under one lens each (`adversary`, `contract`, `craft`), so the lens suffixes every one of those names plus `review_status`, `review_pid`, `review_agent`, and `review_awake`; empty is the single-reviewer layout a closure review and `commands/plan/phase_review.md` still run. The lens also selects the seat the pass records under — `test`, `impl`, `review` respectively, a fixed address rather than a judgment; the `review` seat renders as an `Agent 3` column only when a shown stage seated it — which `review.sh` exports as `PLAN_DELEGATE_TEAM_ROLE`, empty lens included, so a lone reviewer cannot inherit a seat from its environment and close a live pass there so three concurrent reviewers key three pass records instead of each closing the last as interrupted, and posts as a `register` line so the progress note stops attributing the previous occupant's words to the seat. `arm-review` takes a matching `--lens`, because the marker watches the status and pid files the launcher it precedes will write. |
| `scripts/delegate/board.sh` | The phase team's coordination substrate: an append-only `board.log` plus `mkdir`-atomic tokens under `locks/`, shared by every member writing into one session directory. Commands are `post` / `read` / `acquire` / `release` / `renew` / `role` / `roles` / `locks`. A file rather than messages because one append *is* the broadcast to every peer and the wrapper, where N-1 addressed sends can each half-fail; because it outlives the turn that wrote it, so a member resumed hours later reads the whole history; and because a message channel cannot make anything mutually exclusive, which the `mkdir` tokens can. Post kinds are a closed set (`register`, `claim`, `release`, `status`, `blocked`, `handoff`, `done`) so a peer can scan for what concerns it — `ask` and `answer` are deliberately absent, since a question to a peer is a message (`SendMessage`, or `codex_mesh.py` for a codex member) and two ways to ask one question means one of them goes unread; `read --since N` numbers every line before filtering, so a cursor counts board positions and stays valid across different filters. `role` is the only writer of `handoff`, and those lines are how `progress_history.py` learns which role each slot holds now. |
| `scripts/delegate/progress_history.py` | Cross-agent append-only plan-delegate event recorder and aggregator. Durable per-run JSONL lives under `~/.local/state/plan-delegate/runs/`; live state remains in the session directory. `start-run` alone resolves and records the project clock from the supplied plan, matching worktree/branch history, or the run start. It renders separate project and phase progress sections with independent unchanged timers and unambiguous `HH:MM:SS` durations, and calibrates phase estimates from completed-phase history. `progress` requires `--cap-stage` on dual-layout calls and clamps the calibrated percentage to that stage's ceiling; `start-phase --work-order-file` records Work Order size metrics. |
| `scripts/delegate/findings.py` | The delegate fix loop's convergence test — what replaced the fix-pass counter. Stable finding IDs (`F001…`) with states `open` / `fixed_pending_review` / `accepted`, held in `findings_state.json` beside the progress state and reset automatically when the active phase's `instance_id` changes. `gate` returns `converged` / `dispatch` / `stop`, gating on blocker+minor in round 1 and blocker only afterwards (nits never gate); `dispatch --covers` refuses a partial batch so one fix round repairs everything gating together. Stops on: a finding that failed to close twice, a finding reopened twice, two rounds with no decrease in the gating-open count, or a 10-round runaway backstop. Appends `finding_opened` / `finding_batch_dispatched` / `finding_verdict` / `finding_gate` to the same durable run JSONL. |
| `scripts/delegate/test_progress_history.py`, `test_findings.py` | `python3 -m unittest scripts.delegate.test_progress_history scripts.delegate.test_findings` from `~/.claude`. Both drive the real CLIs in a temp session dir with `PLAN_DELEGATE_NOW_EPOCH` / `PLAN_DELEGATE_HISTORY_DIR` pinning time and storage. |
| `scripts/cli_agent/cli_agent.sh` | zshrc-alias dispatcher (`review`, `commit_no`, `commit_yes`, `merge`, `code`). `cli_agent_print_status` prints the four `cli.*` rows via `agents_resolve_print`; `cli_agent_run` maps no args → `cli.interactive` REPL and a skill name → `cli.<skill>` (unknown skill errors with the known list), then `exec`s codex with `agents_codex_args` (the registry tier included) or claude with `-- "/$invocation"`. It has no assignment editor — assignment changes go through `/agent`. |
| `scripts/production/add_unit.py` | `production.director`, resolved directly (not through `agent_exec`) because a unit director is a long-lived tmux session: `director_agent` runs `bash -c` sourcing `agents_config.sh` and `agents_resolve production.director`, refuses any family but `claude`, and launches `claude --model <agent> [--effort <effort>] --remote-control …`. `produce.md`'s Resume resolves the same row and runs `agents_claude_args` before `claude --resume`. |
| `scripts/fix/agent_assignments.sh` | `cf_load_stage_assignment <section> <enabled_var> <family_var> <agent_var> <effort_var>` reads `enabled=` from `agent-assignments.conf`, validates it with `cf_validate_bool`, then fills family/agent/effort from `agents_resolve fix.<section>` (surfacing resolver errors). `cf_print_stage_assignment` / `cf_print_agent_assignments` back the `/fix agent` status view; `cf_trim` and `cf_resolve_checkout` also live here. |
| `scripts/fix/agent-assignments.conf` | Stage enablement only — `[style_eval]` / `[style_eval_review]` / `[style_fix]` with `enabled=`. Scheduled-run policy: consulted only when `FIX_SCHEDULED=1`. |
| `scripts/fix/fix.sh` | Driver: loads all three stage assignments before checking `enabled` through `stage_runs()` (which only consults it on a scheduled run), logs `family/agent`, and renders the report through `agent_exec fix.report write "$HOME/.claude" <prompt> /tmp/fix-report.txt <log_dir>/report_render.txt` behind an activity grep, with a guarded prompt build and WARN-and-continue. |
| `scripts/fix/style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh` | Stage scripts; `case "$STYLE_AGENT"` dispatches on the *family*, `STYLE_AGENT_MODEL` is the agent, and the codex effort flag is wrapped in an `[[ -n … ]]` guard. |
| `scripts/fix/fix-usage.sh`, `fix_report_parse.py` | Usage screen renders family/agent/effort columns (`<default>` for empty effort); the parser carries the family into `<family>-usage-limit` reason codes via `AGENT_LIMIT_LINE_RE` and the `AgentLimit` dataclass. |
| `scripts/ask_a_friend/prepare_session.sh` / `launch_friend.sh` / `end_friend.sh` | `ask_a_friend.consultation`, resolved directly (not through `agent_exec`) because the friend is a live peer, not a one-shot: claude → `agent_bg.sh` with `AGENT_BG_DETACH=1` (the one `claude --bg --name <friend>` recipe; answers by `SendMessage`; `friend_id` for `claude stop`), codex → `codex_mesh.py start --resident --service-tier "$AGENT_SERVICE_TIER"` (a thread that outlives its turns; replies print between `=== reply from <friend> (N) ===` / `=== end reply ===`; `end` releases it). Protocol files: `friend_name`, `question.md`, `message_<N>.md`, `answer.txt` (codex), `status` `running\|ended\|error`, `agent.log`, provenance `consult_agent`. One friend carries consultation and implementation, so there is one row per family. |
| `commands/team_review.md`, `api_review.md`, `module_review.md` | Call `agent_exec … readonly` directly, backgrounded, one call per lens/pass, with self-contained prompt files under wave-namespaced session dirs (`cycle2/`, `adversary/`, `pass3/`) and provenance captured in-session via `agents_resolve_print`. |
| `commands/unit/delegate.md`, `commands/ask_a_friend.md`, `commands/fix.md` | Consumer docs: call sites, log/provenance names, and `/agent` as the switch surface. |

All four wrappers capture resolver stderr into their log (`agents_resolve "$TASK" 2>"$LOG_FILE"`) so an "on error read the log" instruction is true even on the resolution-failure path.

## Invariants

- The registry is the only home for family/agent/effort. Consumers resolve through `agents_resolve` or `agent_exec` and never re-derive flag vocabulary — `agents_codex_args` / `agents_claude_args` own it.
- Every function keeps **both** family sets fully specified, so switching families is a one-row edit (a one-family function is pinned and kept by every-function switches); `agents_set_assignment` (and `agents_set_all_assignments`, across every function at once) refuses a switch if any row of the target set fails validation, and leaves the file untouched.
- Agent names stay disjoint between `[codex.agents]` and `[claude.agents]` — `agents_set_row` infers the family from the agent and refuses a name listed by both instead of picking a family for it.
- Task names are exactly two segments. Empty effort means "omit the flag"; `agent:` with nothing after the colon is invalid; a catalog row with an empty effort list is valid and admits only bare pairs.
- Only `agents_set_assignment`, `agents_set_all_assignments`, and `agents_set_model` change which family is live. `agents_set_row` writes a row (live or dormant) and `agents_set_service_tier` writes a tier (live or dormant); neither flips liveness. The one exception is a `caller` function: its live family is whichever agent is asking, it is written by hand in the file, and neither switch touches it.
- The sync rewrites only `[codex.agents]` and never touches assignments. `[claude.agents]` stays hand-maintained; alias warnings never auto-add, vanished-agent warnings never auto-repoint.
- New lookups use `_agents_registry_get` / `_agents_registry_has_key` (literal key comparison). Never match keys with an unescaped `^key=` regex — dotted keys like `delegate.review` mis-match.
- Any awk that writes a user-supplied value into the conf passes it through `ENVIRON`, not `awk -v`. Row rewrites preserve trailing inline comments and spacing byte-exactly; conf writes go through a tmp file + `mv` (mode preserved), never in place.
- `agent_exec` owns all redirection: wrappers must not redirect its stdout/stderr to a file, or dry-run output never reaches them. It exports nothing — provenance comes from the wrapper's own `agents_resolve`.
- Callers pass **absolute** prompt/output/log paths to `agent_exec`.
- `codex_mesh.py` is a launcher for `/unit:delegate` and `/ask_a_friend`, not a
  second dispatcher. It resolves nothing: `implement.sh` and `launch_friend.sh`
  resolve family, agent and effort through `agents_resolve`, exactly as for
  `agent_exec`, and pass them in as `--model` / `--effort`, so the mesh path
  changes only whether the delegate has an address, never which agent runs or at
  what effort. Anything else that needs a codex agent still goes through
  `agent_exec`.
- Every delegate in one phase shares one app-server, and its pid file is the only
  record of it. A launch path that starts a server without writing
  `mesh_server.json` leaks a process that nothing will reap.
- Provenance files are four lines: `task=`, `family=`, `agent=`, `effort=`.
- The session app-server is deliberately detached, so it outlives each delegate
  and a peer can still reach a thread between turns. `end_session.sh` is the only
  thing that reaps it (`codex_mesh.py stop`, from the session directory recorded
  in the run-active marker); a launcher that killed it would break the mesh it
  exists to provide.
- A codex delegate's launcher ends with its last turn, and `send` then refuses it
  though the thread persists. Unlike a claude delegate, whose
  background session stays resumable, a finished codex peer cannot be messaged;
  only the unit director's `implement.sh --to <seat>` (`codex_mesh.py follow`) gives it new work —
  `<PhaseMesh/>` in `commands/unit/delegate.md` states this, and the register
  line's `reach=` field is what tells a peer which of the two it is addressing.
- The fix pipeline runs unattended every 10 minutes on both machines from the `nate.jobs.style-fix` job in `/etc/nixos/modules/common/style-fix.nix` (`intervalSeconds = 600`, no idle gate; see `fix-pipeline.md`). `agents_config.sh`, `agent_assignments.sh`, the three stage scripts, and `fix_report_parse.py` must never be left broken, and the resolver must keep working under `/bin/bash` (3.2).
- `/unit:delegate` is itself implemented by `scripts/delegate/*`, so any rename or signature change to those launchers must land together with the `commands/unit/delegate.md` call-site edits in one change.
- Every `implement.sh` dispatch is a member of a phase team: `team_role` is required, every artifact it writes is suffixed with that role, and every member carries a `pass_kind`, so a team phase records one pass per seat rather than one for the whole phase. The kind names the work the seat was assigned and nothing else: the `test` seat records `test`, a writing seat `impl`, the repairing seat `fix`. An earlier claim here that a `test` kind would rewrite the `implementation` stage across the back corpus was wrong -- adding a kind cannot change stored events, only adding a stage could. A kind never triggers behavior either: round resolution is `PLAN_DELEGATE_RESOLVES_ROUND=1`, set on the one repairing seat, so recording `fix` never resolves a round by itself. The board, not the launcher, is what a third concurrent member would change.
- No `/unit:delegate` prompt tells an agent to acquire the `cargo` token. `verify.sh` takes it, and a prompt that takes it too deadlocks the agent against its own held token.
- `cf_load_stage_assignment` keeps its five-argument out-var signature; `fix-usage.sh` and the print helpers call it positionally.
- The report parser slices launchd runs on exact substrings of the driver's stage-start lines — a reword must keep those leading phrases byte-identical or update the parser in the same change.
- In the fix pipeline stage scripts, do **not** add family guards around the exec-marker transcript filter or the usage-limit detection: the first handles both families by pattern union, the second's codex-worded grep no-ops on claude logs, and the durable lines print `(${STYLE_AGENT} …)` with the parser accepting any family word.
- Before committing, inspect `git diff config/agents.conf`: an unsandboxed run that sources the resolver can legitimately rewrite `[codex.agents]`. Fold sync drift in deliberately or exclude it; never commit it unnoticed. `settings.json`'s app-generated key reorder stays excluded, and `settings.json`'s `model` / `statusLine`, `~/.zshrc`'s interactive `claude` alias, `scripts/claude_to_codex/`, and `~/.codex/config.toml` are out of scope.
- Never use `AskUserQuestion` in the command docs; the review docs decide via in-session synthesis.

## Calibration / gotchas

- **bash 3.2.** macOS ships bash 3.2 — no associative arrays, no `${var,,}`, no bash-4 anything. The fix pipeline scripts run under `#!/bin/bash`; the registry, delegate, cli_agent, and ask_a_friend scripts use `#!/usr/bin/env bash` (also 3.2 here).
- **bash only, never zsh.** The resolver uses `BASH_REMATCH` and process substitution. Sourcing it from zsh hangs or misbehaves — run every test and probe as `bash <script>` / `bash -c '…'`.
- **Sourcing fires the sync.** `agents_config.sh`'s freshness gate can shell out to the sync (which itself shells out to `claude --help`, ~1-2 s). Tests and probes suppress it by exporting `CODEX_CATALOG_SYNC_STATE_FILE` at a freshly `touch`ed temp file *before* sourcing.
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
  are not ephemeral, since an ephemeral thread also refuses `thread/queue/add`.
  The cost is the ordinary codex rollout file under `~/.codex/sessions`, which
  also keeps a delegate's transcript readable afterward. Naming stays
  best-effort and `mesh_roster.json` is the address of record.
- **`--listen unix://<path>` closes every connection silently** while the server
  stays up. Use `ws://127.0.0.1:<port>`.
- **`codex queue --thread` exits 0 for a thread with no live session.** The
  acknowledgement says nothing about delivery — do not use it as a reachability
  test.
- **A finished codex delegate takes no messages.** Its thread persists, but `send` refuses
  it once `start` returns (`implement.sh --to` reaches it through `follow`), unlike a claude background session, which a message resumes from its transcript. The
  exception is a thread started `--resident` (ask_a_friend's friend): it stays
  `running` across turns, prints each reply as it lands, and ends only on
  `codex_mesh.py end`. `send`
  refuses every status but `running` and `waiting_capacity`.
- **`systemError` is not terminal.** The thread stays loaded, and `turn/start` has no status gate, so a resume turn on the same thread is valid. A status outside the protocol's four reads `ThreadStateUnknown`: relaunching a `done` or `capacity_exhausted` entry exits 2, `end` exits 1, and inside the capacity loop the seat ends `failed` with `thread <id>: thread/read failed: unrecognized status <status>`.
- **Capacity numbers.** The waits run 30, 60, 120, 240, 300, 300 and 150 s, so a spent budget reads `7 retries over 20 min`. The budget counts the measured sleep (`max(wait, elapsed)`), so a late wake spends it, and it counts only waits, not the time refused turns took, so a spell runs longer than 20 min of wall time.
- **Stub app-server.** In `scripts/agents/test_codex_mesh.py` the stub answers `systemError` after an errored turn (`turn/failed`, or `turn/completed` carrying `error`) until the next `turn/start`, and an explicit `thread_statuses` entry wins. A stub that answered `idle` after a refused turn would hide the crash this guards against. `completed_then_capacity` and `capacity_with_peer` drive the budget-reset tests, and `test_failed_seat_relaunches_when_old_thread_has_unknown_status` uses `retired`, a status the protocol does not define. Seats run the live `~/.claude` copy of `codex_mesh.py`, so a change protects running seats only once it is there.
- **`AGENT_EXEC_EXTRA_ARGS` is whitespace-split with no quote interpretation.** Flag+value pairs (`--add-dir /path`) work; no single argument may contain a space — no prompt preambles, no `--settings` JSON.
- **`AGENT_EXEC_DRY_RUN=1`** is the testing hook: `%q`-quoted argv plus redirection suffix, with a `cd <dir> && ` prefix on the claude branch. Match smoke checks on substrings (`--full-auto`, `--sandbox read-only`, `-m <agent>`, the effort word), never whole lines — the codex effort token renders with escaped quotes (`model_reasoning_effort=\"high\"`).
- **awk gotchas.** `function` is a reserved awk word — pass it as `-v fn=`. `awk -v` decodes backslash escapes, so a value containing `\n` / `\t` would corrupt the row; user-supplied values go through `ENVIRON["…"]`.
- **`codex --sandbox read-only` panics** codex's system-configuration crate on macOS in some contexts, which is why ask_a_friend's conceptually read-only consult ran `write` (`--full-auto`) while it went through `agent_exec`; today the friend is a mesh thread (`codex_mesh.py start --resident`) and never touches `--sandbox read-only`. The delegate reviewer's `--sandbox read-only` usage is proven and stays. Codex launched from a Claude Code session needs `dangerouslyDisableSandbox: true` (and usually `run_in_background: true`).
- **The cli aliases run fast from the registry.** `cli_agent.sh` once hardcoded `-c service_tier="fast"` after `agents_codex_args`, which would have overridden any `/agent cli <tier>`; the hardcode is gone and each machine's `[cli.options]` carries `codex_service_tier=fast` instead.
- **Last-writer-wins on conf rewrites.** The source-time sync and the `/agent` editors both rewrite the file with tmp + `mv` and no locking; interleaved writers can silently revert each other's change but never corrupt the file.
- **claude-family output needs `python3`.** The claude branch logs stream-JSON and extracts the final result event into the output file; without `python3` the output file would be empty even though the log is complete. Claude-family `readonly` reviewers running the style loader script under `--permission-mode plan --print` is untested — a family switch may silently degrade style loading.
- **`_agents_registry_get` returns 0 on a miss** (prints nothing) so it is safe under `set -e` in command substitution; use `_agents_registry_has_key` when you need presence as a condition.
- **The `cargo` build token belongs to `verify.sh`, and never to a prompt.** `verify.sh` acquires `board.sh … cargo` itself from `PLAN_DELEGATE_BOARD_DIR` / `PLAN_DELEGATE_TEAM_ROLE` and releases it from one unified EXIT/INT/TERM path, so concurrent team members serialize their builds without being asked to. It acquires with `--pid $$`, so a `verify.sh` killed before that path runs leaves no stranded token: a waiter reclaims it as soon as that pid is gone (`build-memory-admission.md`, Cargo token reclaim). Putting the acquire in a prompt as well makes that agent wait out the full `--wait` against a token it already holds, and the symptom — a member that just sits there — is indistinguishable from a slow test. With either env var unset the token step is skipped entirely, which is what keeps a standalone `verify.sh` run unchanged.
- **`verify.sh` remembers passes within a delegate session.** A `test` or `lint` that exits 0 under `PLAN_DELEGATE_BOARD_DIR` or `PLAN_DELEGATE_SESSION_DIR` is recorded in `<session>/verify_cache/`, keyed by the tree as the run left it plus the arguments, toolchain, `lint.conf`, the scripts and the cargo environment; the same call on the same tree prints `PASS (recorded)` with that run's log and takes no token. It looks again after taking the token, so a seat queued behind its peer's identical run gets the peer's record. Failures are never recorded; `--no-cache` forces a run. Each record holds what a repeat would cost (wall time minus cargo's `Finished … in` build time); every such call, `--no-cache` included, appends one line (ran, failed, interrupted, or reused with seconds saved; machine, workspace, worktree, branch, commit) to `~/.local/state/verify/events.jsonl`, which `/verify_saved` (`verify_saved.py`) totals for any span by workspace, worktree, day or week, the Mac's ledger read over ssh. `end_session.sh` deletes the records; the ledger stays.
- **A green `verify.sh` only means what the tree it ran against means.** With three members editing one worktree, a pass is authoritative for a slot's work only after that slot has posted `done` to the board.
- **cargo-berth claims are per harness session id, so cross-slot edits are blocked, not merged.** Each delegate is its own claim holder: the tester cannot add a `#[cfg(test)]` block to a file `impl` claimed, which is why the contract routes it to an integration test under `tests/`.
- **Editing a live launcher in place** (a script currently running) produces a spurious `unexpected EOF` exit 2 after the real work completes — bash re-reads the modified file at a stale byte offset. Check the status file and diff before treating it as a failure.
- Sync `WARNING:` lines land in launchd stderr logs; they are not stage failures, and the usage-limit regexes are written not to match them.

## Why it is this way

- **Warn-and-keep for vanished codex models.** If the sync simply dropped a model an assignment still uses, `agents_resolve` would hard-fail at the config layer and wedge the whole registry — `agents_list_assignments`, `/agent status`, every `cf_load_stage_assignment`, including the unattended 10-minute launchd run. Keeping the previous catalog row leaves resolution green so a truly retired model fails only in that one stage's own execution log, and the warning names the stale row plus the exact `/agent` commands to fix it. The keep condition is deliberately "would this row still resolve against the refreshed catalog *and* the cache", not bare cache membership: a hidden-but-cached assigned model still disappears from the sync output, and a selected-but-uncached model is prepended with an empty effort list, so cache membership alone would still wedge `agent:effort` assignments. Auto-repointing assignments was rejected — assignment edits are a human call.
- **Warn-only for claude alias staleness.** The effort list for a new alias is a judgment call and omissions (haiku) are deliberate, so the sync reports and never edits. A help-text wording change or a missing `claude` binary degrades to a no-op rather than a false edit.
- **No locking on conf writes.** Interleaved writers can only lose an edit, never corrupt the file, and this is a single-user machine; a lock would add failure modes (stale locks in launchd context) worse than the loss it prevents.
- **No per-project overrides.** One global `config/agents.conf` governs everything. Per-project layering would reintroduce exactly the scattered, hard-to-audit assignment state the registry exists to prevent, and `/agent status` would stop being the truth.
- **Double resolution in the wrappers.** `agent_exec` deliberately exports nothing, so a wrapper that wants provenance resolves again itself. Both reads hit the same conf, so they agree; the alternative — `agent_exec` exporting or writing resolved values — would couple every consumer to the launcher's variable names and make the launcher responsible for file layout it does not own.
- **Effort omission for a bare agent pair.** A row of just `agent` means "omit the effort flag entirely and let the CLI pick", which is materially different from any explicit level and is the only thing that validates against a catalog row with no reasoning levels. Hence no default-effort fallback anywhere: the stage scripts guard the flag with `[[ -n … ]]` rather than substituting a level, so the registry's silence is transmitted faithfully to the CLI.
- **`systemError` reads as idle.** It means the last turn failed and none is active, which is what relaunch, `end` and the capacity loop need to know. Read as unknown, every capacity refusal on the real app-server ended the seat with `thread/read failed`, so the backoff and resume never ran outside a stub that answered `idle`. Any status the protocol does not define still reads as unknown, so a new upstream status is classified on purpose rather than defaulted to idle.
- **A capacity budget per busy spell.** A turn that ends without a capacity refusal proves the model is answering again, so the next refusal starts a new spell. A budget for the whole run would let refusals hours apart add up and retire a long-lived seat, such as a resident friend, after one bad spell too many.
- **An owed resume after a completed turn starts at once.** The completed turn shows the model is serving, so a wait would only delay the seat's own work; only a capacity refusal earns one.
