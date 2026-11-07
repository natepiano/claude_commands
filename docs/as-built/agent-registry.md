# Model registry

Codex code reviews use the unit's persistent `review` and `review2` workers through `assign.sh --role review`.
The [daily simplify run](simplify-lens.md) uses a fresh Claude review.
Pool maintenance paths do not dispatch new reviews.

## What it is

Agent launchers resolve their family, model, effort, and service tier from `config/models.conf`.
Python uses `scripts/lib/models_registry.py`. Bash uses `scripts/agents/models_config.sh`.
Consumers include unit workers, production unit directors, CLI aliases, and `/ask_a_friend`.
The registry gives each task one source for its launch settings.
`/default_models` changes a function's family or a task's model and effort.

## How it works

### Registry schema

`config/models.conf` is the shared registry.
Its INI-style sections hold assignments, task rows, catalogs, family aliases, and options.
`#` starts a comment, including a trailing row comment.

```ini
[assignments]                 # <function>=<family>, plus optional <function>.<subtask>=<family>; `caller` = the family of the agent running it
direct=codex

[direct.codex]              # [<function>.<family>] — <subtask>=<agent>[:<effort>]
impl=sol:high
review=sol:high

[codex.agents]                # [<family>.agents] — <agent>=<comma-separated valid efforts>
gpt-5.6-sol=low,medium,high,xhigh,max,ultra

[direct.options]            # [<function>.options] — launch flags, not agent rows
codex_service_tier=fast
```

Options sections hold launch flags instead of agent rows.
`[<function>.options]` can set `codex_service_tier` and `codex_service_tier.<subtask>`.
The editor excludes options sections from family enumeration.

A family is `codex` or `claude`.
An agent is a model within that family.
A function names a consumer; a task has exactly two segments, `<function>.<subtask>`.

A function normally has complete task sets for both families.
A single-family function stays pinned to that family.
A global family change keeps pinned functions and prints their names.
A direct change to an unsupported family refuses without writing.

| Function | Sub-tasks |
| --- | --- |
| `direct` | `impl`, `test`, `fix`, `review`, plus the Claude `simplify` task |
| `cli` | `commit_prep`, `merge_branch`, `interactive` |
| `ask_a_friend` | `consultation` |
| `production` | `director` |
| `design_roles` | `designer`, `reviewer` |

`[codex.agents]` and `[codex.families]` come from catalog synchronization.
Family aliases such as `sol` resolve to a concrete model from `[codex.families]`.
The Claude catalog and family aliases are hand-maintained.
`production` has only a Claude set. `design_roles` has only a Codex set.
`ask_a_friend` selects the caller's family.

### Registry publication

`store_registry(registry, before, after, description, *, additive_catalog=False)` owns every registry publication.
It replays the changed rows against the latest text under an exclusive writer lock.
It preserves unrelated edits, file mode, comments, and spacing.
It returns the published text while it still holds the lock.
The `store` command builds `configuration_shell` from that text without a second read.

Publication applies only to the real registry under the Git root of `~/.claude`.
`MODELS_CONFIG_PUBLISH=0` disables publication.
Other paths use local atomic replacement through `atomic_file.write_text_atomic`.
The shared writer fetches origin and checks fast-forward safety with a temporary index.
An isolated index and `commit-tree` make a commit containing only `config/models.conf`.
Its subject is `config(models): <description>`.
Temporary index folders use the `models-index-` and `models-ff-index-` prefixes.
The commit uses local HEAD as its parent, so a push can include pre-existing local commits.
The writer stages, commits, stashes, resets, or rebases no other path.

A rejected push fetches and replays the same row changes, for at most three attempts.
Repeating an interrupted edit publishes all pending local rows, even when the requested value already matches. Recovery recognizes an accepted push without another commit.
Exhaustion reports that the file is edited but not shared.
Local integration uses fast-forward only.
A successful push with failed local reconciliation reports both facts.
Catalog publication adds models and efforts and advances family targets.
It never deletes catalog rows or moves family targets backwards.

### Configuration lifetimes

`models_registry.py` owns the path resolver and the only registry parser.
An explicit `--registry` selects the path before `registry_path()` supplies `MODELS_CONFIG_FILE` or the default path.
Bash passes a set `MODELS_CONFIG_FILE` explicitly, even when the shell variable is not exported.
An explicit caller argument wins over that shell variable.

`LaunchModelConfiguration(path, text, rows)` lasts for one launch or assignment operation.
`RegistryEditBase(path, text, rows)` lasts for one editor operation.
Each type contains the selected path, exact source text, and parsed `RegistryRow` values from one read.
The parser retains empty sections and row order. The exact text retains comments and editor formatting.
`configuration_shell` emits safely quoted section, key, and value arrays.
Bash readers compare parsed keys. They do not parse registry rows again.
Editor transformations use their base text and parsed values from the same generation.

Each configuration's `environment(mapping)` shares that snapshot with the operation's helper processes.
`MODELS_LAUNCH_CONFIGURATION` carries the launch snapshot.
`MODELS_REGISTRY_EDIT_BASE` carries the editor snapshot.
Neither variable is a persistent cache.
Implementation, review, allocation, and thread creation share one configuration within an operation.
Reusing an existing worker needs no allocation snapshot read.
`unit_effort.py` uses one edit base for one command invocation.

The next launch reads a fresh configuration.
Each later Codex submission also reads a fresh configuration, including continuations, capacity retries, and resident wakes.
`FreshLaunchConfiguration` marks that transition in the launcher.
Model, effort, and service tier for one submission come from the same configuration.
Explicit dispatch overrides retain their precedence.
The [progress recorder](unit-progress-history.md#configuration-digest) uses the inherited exact text for telemetry.

### Agent environment boundary

Operation snapshots reach helpers, but they never reach an agent or an app-server.
`models_registry.OPERATION_ENVIRONMENT` names both variables once for Python callers.
`models_registry.agent_environment` removes them from a supplied environment.
`staff.child_environment` applies this filter and removes staff lock proofs and copied staff identity.
Python agent and app-server starts use this filtered environment.

`models_agent_environment` removes both variables from the current shell.
`agent_exec.sh` and `cli_agent.sh` call it immediately before their agent starts.
`scripts/direct/claude_worker.py` filters the Claude start environment and settings JSON in the assign process.
`nix_env.py exec` applies `agent_environment` after it obtains its final environment, immediately before `os.execvpe`.
This prevents an environment restore from carrying an old snapshot into the agent.
`review.sh` retains the snapshot until `agent_exec.sh` resolves its settings.

### Resolution algorithm and precedence

`agents_resolve <task>` sets `AGENT_FAMILY`, `AGENT_MODEL`, `AGENT_EFFORT`, and `AGENT_SERVICE_TIER`.
A failure names the invalid value and allowed values on stderr.
An empty effort or tier omits its corresponding flag.

The resolver applies these rules:

1. Split the task into two nonempty segments.
2. Select the exact-task assignment before the function assignment.
3. Resolve `caller` through `AGENTS_CALLER_FAMILY`, then `CODEX_THREAD_ID`, then `CLAUDE_CODE_SESSION_ID`.
4. Read the task row from `<function>.<family>`.
5. Resolve a model alias through the family catalog.
6. Validate the model and effort against `<family>.agents`.
7. Select the task-specific service tier before the function tier.

`AGENTS_CALLER_ASSIGNMENT` names the `caller` assignment value.
No detectable caller family produces an error.
A trailing colon with no effort is invalid.
A model with an empty effort catalog accepts only a bare model name.
A row edit leaves the active family unchanged.

Codex tiers are `fast`, `flex`, `default`, or `pace`.
`pace` uses the recent `codex_pacer.py` value and defaults to `default` when unavailable or older than ten minutes.
An absent tier inherits `service_tier` from the Codex config.
Claude rows have no service tier.

### Resolver API (`scripts/agents/models_config.sh`)

Sourcing the file defines functions without reading or synchronizing the registry.
`models_launch_configuration` obtains and exports the launch snapshot.
`models_registry_edit` gives an editor one scoped base.
`_models_registry_scope --launch|--edit` holds the parsed arrays for that scope.
`models_registry_store` evaluates the configuration returned by the publication writer.

| Function | Result or change |
|---|---|
| `models_config_trim <value>` | Removes surrounding whitespace. |
| `agents_resolve <task>` | Sets the four resolved launch values. |
| `agents_resolve_print <task>` | Prints task, family, agent, effort, and tier. |
| `agents_list_assignments [filter]` | Lists each active task once and reports invalid rows. |
| `agents_list_function <function>` | Lists both family sets and marks active rows. |
| `agents_set_assignment <function> <family>` | Validates the target set and changes its assignment. |
| `agents_set_all_assignments <family>` | Changes supported assignments and keeps caller-based or pinned functions. |
| `agents_set_model <agent> [function]` | Changes supported sets to the model while preserving valid efforts. |
| `agents_set_row <task> <agent>[:<effort>]` | Changes the row in the model's family, without changing its assignment. |
| `agents_set_service_tier <tier> [function or task]` | Changes tier options; `inherit` removes the selected override. |
| `agents_codex_args` | Emits model, optional effort, and optional service-tier flags. |
| `agents_claude_args` | Emits model and optional effort flags. |

The editor validates the complete requested change before publication.
Ambiguous model names and missing task sets produce errors.
A function-wide tier change removes narrower task-tier overrides.
`caller` functions keep their family assignment during global edits.
`AGENT_KEPT_FUNCTIONS` reports pinned functions that a global edit preserved.

The argument emitters produce one space-separated line for `read -r -a`.
Callers do not evaluate that line as shell code.
The Codex effort token includes literal quotes within one argument.
`_models_registry_get` compares keys literally and returns zero with empty output when a key is missing. Use `_models_registry_has_key` to test presence.
The remaining lookup helpers use the same parsed arrays.
Awk transformations can produce edited text; they do not provide a second registry reader.
Pass edited values to awk through `ENVIRON`. `awk -v` interprets backslash escapes.

### Shared launcher (`scripts/agents/agent_exec.sh`)

```
agent_exec.sh <task> <write|readonly> <working_dir> <prompt_file> <output_file> <log_file>
```

A wrong argument count or mode returns 2.
A missing prompt writes `Prompt not found: <path>` to the log and returns 1.
`agents_exec_main` resolves the task from the operation's configuration.
It sends prompt contents through stdin, avoiding the command-line argument size limit.

| Family | Launch behavior |
|---|---|
| Codex | Uses the selected workers account, `codex exec`, `--ephemeral`, the working directory, and output file. |
| Claude | Uses print mode with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and retains stream-json events in the log. |

Write mode uses unrestricted write permissions.
Readonly mode selects the read-only Codex sandbox or Claude plan permissions.
`direct.simplify` also disables Claude editing tools.
`claude_result.py` writes the final answer to the output file.
An `is_error` result or nonempty errors array produces `ERROR: <reason>` and exit 1.
Otherwise the launcher preserves the agent's exit status.

`AGENT_EXEC_EXTRA_ARGS` adds arguments without interpreting shell quotes.
`AGENT_EXEC_DRY_RUN=1` prints the quoted command and redirections without starting an agent.
The launcher removes the operation snapshot at the [agent boundary](#agent-environment-boundary).
An executable invocation runs `assign_gate.py` before dispatch.

### Addressable workers

`assign.sh` is the worker launch entry point.
`assign.py` reads the run's transport pin.
A mesh run uses `codex_mesh.py` for Codex workers; a herdr run uses `worker.py` and a pane.
Claude workers of a mesh run use `scripts/direct/claude_worker.py` in the assign process.
The [worker design](long-lived-workers.md) owns admission, staff identity, family switches, retries, messages, and server lifecycle.
The plain `codex exec` worker fallback is absent.

`design_roles.py` uses `worker.py add`, `assign`, and `end` for held Astra roles.
These roles keep their staff row and current session in a pane.
Held roles receive messages through `send.py`.
An ad hoc Astra review outside the staff hierarchy uses the one-turn `codex exec` path in `astra_review.py`.
`side_pane.py` serves ask-a-friend consultations without a staff row.
The friend launches with the worker policy through `worker_panes.worker_policy`, so it replies with `send.py` and stops at no permission prompt.
A managed Astra review starts through `assign.sh --role review --operation astra-review`.
The mesh transport primes, holds, follows, and ends the reviewer through `codex_mesh.py`.
The herdr transport adds, assigns, and ends the reviewer through `worker.py`.
The mesh transport keeps the hold until the end of the worker succeeds.
The pane transport keeps the hold while `assign.sh` runs; after a failed end, the staff row is the record of the reviewer.
`assign.py` also retains `dispatch`, `adopt`, and `resume-account-move`.
The two recovery operations remain until Phase 18.

### Catalog sync (`scripts/agents/sync_codex_catalog.sh`)

Synchronization adds Codex models and efforts and advances family aliases.
It preserves existing catalog entries and unrelated rows.
It requires `jq` and reads the selected model from the Codex config.
It reads visible models and supported reasoning efforts from the Codex model cache.
A selected model absent from the visible list is included with its available cache efforts.

The synchronizer skips slugs that do not match `^[[:alnum:]][[:alnum:]./_-]*$` and prints a warning.
Parsed task assignments identify models that a refresh would remove.
The synchronizer preserves each assigned model's previous catalog row and warns with an editor command.
An assigned missing model without a previous row produces an error.
Missing Claude aliases produce warnings without automatic edits.
Unavailable Claude help produces no alias warning.

Synchronization takes `sync.lock` before its registry snapshot and parses that snapshot once.
Transformations use `MODELS_REGISTRY_BASE`.
Both generated sections use one parse of the temporary result.
Publication uses `store_registry` with additive replay.
A complete successful synchronization updates `CODEX_CATALOG_SYNC_STATE_FILE`. An incomplete catalog leaves the freshness gate open for the next synchronization.
`--check` reports stale data with exit 1 and publishes nothing.

The launchd job `com.natemccoy.codex-agent-catalog-sync.plist` runs at login and every 300 seconds.
Fresh `launch_configuration` and `edit_base` reads also check catalog freshness before taking their snapshot.
This synchronization is a separate edit operation.
An inherited snapshot skips synchronization. `MODELS_CONFIG_TIER_ONLY=1` also skips it.

The gate requires an existing registry and an executable sync script.
It compares the Codex config and cache modification times with the sync state file.
It synchronizes when the state file is absent or an existing source is newer.
Process settings come from `os.environ`, even when a caller passes `{}` to request a fresh snapshot.
The settings are `MODELS_CONFIG_FILE`, `MODELS_CONFIG_TIER_ONLY`, `CODEX_CONFIG_FILE`, `CODEX_MODELS_CACHE_FILE`, and `CODEX_CATALOG_SYNC_STATE_FILE`.
Defaults are `~/.claude/config/models.conf`, `~/.codex/config.toml`, `~/.codex/models_cache.json`, and `~/.local/state/codex-agent-catalog-sync/last_success`.
The child receives the selected registry path as `MODELS_CONFIG_FILE`.
A sync failure prints `WARNING: Codex catalog sync failed; using the current model registry.` and continues.
No-mode `shell` reads and `store` bypass the gate, so synchronization cannot call itself.

### Administration (`scripts/agents/default_models.sh`, `commands/default_models.md`)

`default_models.sh` dispatches to the shared resolver and scoped editor.

| Arguments | Operation |
|---|---|
| None | Lists active assignments and usage. |
| `skills` | Lists function names. |
| `<function>` | Lists both family sets. |
| `<family>` | Changes supported function assignments. |
| `<agent>` | Changes supported model rows and keeps their efforts. |
| `<function> <family or agent>` | Changes one function. |
| `<function>.<subtask> <agent>[:<effort>]` | Changes one task row. |
| A tier, alone or after a function or task | Changes the matching service-tier override. |

The output names preserved pinned functions and marks inactive rows.
Invalid argument counts print usage and return 1.
`commands/default_models.md` relays the script output and renders resolved rows as a table.

### Consumers

| Consumer | Configuration use |
|---|---|
| `assign.py`, `assign_claude.py`, `review.sh` | Share one launch configuration across allocation and delivery. |
| `workers.py` | Reads a configuration only when it opens a worker. |
| `codex_mesh.py` | Uses one configuration for each turn submission. |
| `agent_exec.sh`, `cli_agent.sh` | Resolve task values before removing the operation snapshot. |
| `unit_effort.py` | Shares one editor base through its command. |
| `progress_history.py` | Reads the [configuration digest](unit-progress-history.md#configuration-digest) without publication. |
| `add_unit.py` | Resolves `production.director` and requires Claude. |
| `side_pane.py` | Resolves `ask_a_friend.consultation` in the caller's family. |

[Long-lived workers](long-lived-workers.md) owns worker launch, handoff, and recovery behavior.
[Unit progress history](unit-progress-history.md) owns recorder events and progress reports.

## Invariants

- `config/models.conf` owns model choices. Consumers use the shared resolver and flag emitters.
- A task has exactly two non-empty segments. Model aliases resolve through the selected family's catalog.
- An empty effort omits the effort flag. A trailing colon with no effort is invalid.
- Catalog entries with no effort values admit only a bare model name.
- A family switch validates every target row before publication. A single-family function stays pinned.
- A row edit changes the model and effort without changing the active family.
- A service-tier edit changes no family or model. An absent tier inherits the Codex config.
- Editor validation and transformation use one exact base. Locked replay preserves concurrent unrelated edits.
- Shell output uses safe quoting. Dotted keys use literal comparisons.
- Catalog synchronization adds models and efforts. It never deletes them or edits assignments.
- Launch snapshots end at the agent boundary. Each later launch reads current settings.
- Registry writers publish no settings, aliases, credentials, or Codex config files.

## Gotchas

- An unexported `MODELS_CONFIG_FILE` still selects the Bash reader's registry.
- An inherited launch or edit snapshot suppresses catalog synchronization for that operation.
- A telemetry read never synchronizes or publishes the registry.
- A failed synchronization warns and uses the current registry. It does not stop the launch.
- Catalogs differ across machines. Assigned missing models remain in the shared registry and produce a warning.
- `agent_exec` owns its redirections. Wrappers retain absolute prompt, output, and log paths.
- Retired-name tests also match a name that sentence punctuation follows.
- [Worker lifecycle](long-lived-workers.md) owns worker dispatch and recovery.
- [Merge-gate infrastructure](merge-gate-and-test-speed.md) owns test isolation and verification behavior.
