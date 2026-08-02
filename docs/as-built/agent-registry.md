# Agent Registry — one place for family, agent, and effort assignments

## What it is

Every external-CLI agent this configuration launches — `/plan:delegate`'s implementer and reviewer, the `~/.zshrc` CLI aliases, the unattended clean-fix style pipeline and its report render, `/ask_a_friend`, and the `/team_review` / `/api_review` / `/module_review` review teams — resolves which vendor CLI to run, which model, and at what reasoning effort from one file (`config/agents.conf`) through one resolver (`scripts/agents/agents_config.sh`), and launches through one dispatcher (`scripts/agents/agent_exec.sh`). The problem it solves: without a registry each consumer carries its own private assignment state in its own conf file and hard-codes its own vendor flags, so switching a function between vendors means editing several scripts and auditing "what runs what" means reading all of them. Here, a major function switches between the `codex` and `claude` families, or a single sub-task is re-pointed to a different model or effort, with one `/agent` edit, and no consumer assembles vendor flags itself.

## How it works

### Registry schema

`config/agents.conf` is an INI-style file with exactly three kinds of section. Comments (`#`) are stripped to end-of-line everywhere, including trailing inline comments on rows.

```ini
[assignments]                 # <function>=<family>, plus optional <function>.<subtask>=<family>
delegate=codex

[delegate.codex]              # [<function>.<family>] — <subtask>=<agent>[:<effort>]
implementation=gpt-5.6-terra:xhigh
review=gpt-5.6-sol:xhigh

[codex.agents]                # [<family>.agents] — <agent>=<comma-separated valid efforts>
gpt-5.6-sol=low,medium,high,xhigh,max,ultra
```

Vocabulary: a **family** is a CLI vendor (`codex` | `claude`); an **agent** is a model within a family (`gpt-5.6-sol`, `opus`); a **function** is a consumer; a **task** is `<function>.<subtask>` — exactly two segments.

Every function carries *both* family sets, fully specified at all times, so a family switch is a one-line edit and never a row edit. The functions and their complete sub-task sets:

| Function | Sub-tasks |
| --- | --- |
| `delegate` | `implementation`, `review`, `mechanical`, `escalation` |
| `cli` | `style_fix_review`, `commit_prep`, `merge_branch`, `interactive` |
| `cleanfix` | `style_eval`, `style_eval_review`, `style_fix`, `report` |
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
| `scripts/agents/agent_admin.sh` | `/agent` backend. |
| `scripts/agents/sync_codex_catalog.sh` + `.plist` | `[codex.agents]` materialization, staleness warnings. |
| `scripts/agents/heartbeat.sh`, `heartbeat_watch.sh` | Liveness log helpers used by the delegate wrappers (role header block, 60 s beats with an activity digest decoded from the agent log). |
| `scripts/agents/test_agents_config.sh`, `test_agent_exec.sh`, `test_sync_codex_catalog.sh` | Self-contained fixture-conf suites (`mktemp -d`, temp `AGENTS_CONFIG_FILE`, print a "…passed" line, nonzero on failure). |
| `scripts/delegate/implement.sh` / `review.sh` | `/plan:delegate`'s launchers. The implementation launcher adds optional pass kind/activity/fix-count arguments; the reviewer adds optional pass activity. Both write status, provenance, agent logs, and shared heartbeat data; when durable progress state exists, they also record the resolved called model/effort and pass outcome through `progress_history.py`. |
| `scripts/delegate/progress_history.py` | Cross-agent append-only plan-delegate event recorder and aggregator. Durable per-run JSONL lives under `~/.local/state/plan-delegate/runs/`; live state remains in the session directory. It renders the five-line progress header and calibrates raw percentage estimates from completed-phase history. |
| `scripts/cli_agent/cli_agent.sh` | zshrc-alias dispatcher (`review`, `commit_no`, `commit_yes`, `merge`, `code`). `cli_agent_print_status` prints the four `cli.*` rows via `agents_resolve_print`; `cli_agent_run` maps no args → `cli.interactive` REPL and a skill name → `cli.<skill>` (unknown skill errors with the known list), then `exec`s codex with `-c service_tier="fast"` or claude with `-- "/$invocation"`. It has no assignment editor — assignment changes go through `/agent`. |
| `scripts/clean-fix/agent_assignments.sh` | `cf_load_stage_assignment <section> <enabled_var> <family_var> <agent_var> <effort_var>` reads `enabled=` from `agent-assignments.conf`, validates it with `cf_validate_bool`, then fills family/agent/effort from `agents_resolve cleanfix.<section>` (surfacing resolver errors). `cf_print_stage_assignment` / `cf_print_agent_assignments` back the `/clean_fix agent` status view; `cf_trim` and `cf_resolve_checkout` also live here. |
| `scripts/clean-fix/agent-assignments.conf` | Stage enablement only — `[style_eval]` / `[style_eval_review]` / `[style_fix]` with `enabled=`. |
| `scripts/clean-fix/clean-fix.sh` | Driver: loads all three stage assignments before checking `enabled`, logs `family/agent`, and renders the report through `agent_exec cleanfix.report write "$HOME/.claude" <prompt> /tmp/clean-fix-report.txt <log_dir>/report_render.txt` behind an activity grep, with a guarded prompt build and WARN-and-continue. |
| `scripts/clean-fix/style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh` | Stage scripts; `case "$STYLE_AGENT"` dispatches on the *family*, `STYLE_AGENT_MODEL` is the agent, and the codex effort flag is wrapped in an `[[ -n … ]]` guard. |
| `scripts/clean-fix/clean-fix-usage.sh`, `clean_fix_report_parse.py` | Usage screen renders family/agent/effort columns (`<default>` for empty effort); the parser carries the family into `<family>-usage-limit` reason codes via `AGENT_LIMIT_LINE_RE` and the `AgentLimit` dataclass. |
| `scripts/ask_a_friend/ask_a_friend.sh` / `implement.sh` | `ask_a_friend.consultation` / `ask_a_friend.implementation`, both `write` mode; protocol names are their own (`question.md` / `answer.txt`, status `asking→answered\|error`; `implementation_prompt.md` / `impl_summary.txt`, `impl_status`), logs `agent.log` / `impl_agent.log`, provenance `consult_agent` / `impl_agent`. |
| `commands/team_review.md`, `api_review.md`, `module_review.md` | Call `agent_exec … readonly` directly, backgrounded, one call per lens/pass, with self-contained prompt files under wave-namespaced session dirs (`cycle2/`, `adversary/`, `pass3/`) and provenance captured in-session via `agents_resolve_print`. |
| `commands/plan/delegate.md`, `commands/ask_a_friend.md`, `commands/clean_fix.md` | Consumer docs: call sites, log/provenance names, and `/agent` as the switch surface. |

All four wrappers capture resolver stderr into their log (`agents_resolve "$TASK" 2>"$LOG_FILE"`) so an "on error read the log" instruction is true even on the resolution-failure path.

## Invariants

- The registry is the only home for family/agent/effort. Consumers resolve through `agents_resolve` or `agent_exec` and never re-derive flag vocabulary — `agents_codex_args` / `agents_claude_args` own it.
- Every function keeps **both** family sets fully specified, so switching families is a one-row edit; `agents_set_assignment` refuses a switch if any row of the target set fails validation, and leaves the file untouched.
- Agent names stay disjoint between `[codex.agents]` and `[claude.agents]` — `agents_set_row` infers the family from the agent and refuses a name listed by both rather than guessing.
- Task names are exactly two segments. Empty effort means "omit the flag"; `agent:` with nothing after the colon is invalid; a catalog row with an empty effort list is valid and admits only bare pairs.
- Only `agents_set_assignment` changes which family is live. `agents_set_row` writes a row (live or dormant) and never flips liveness.
- The sync rewrites only `[codex.agents]` and never touches assignments. `[claude.agents]` stays hand-maintained; alias warnings never auto-add, vanished-agent warnings never auto-repoint.
- New lookups use `_agents_registry_get` / `_agents_registry_has_key` (literal key comparison). Never match keys with an unescaped `^key=` regex — dotted keys like `delegate.review` mis-match.
- Any awk that writes a user-supplied value into the conf passes it through `ENVIRON`, not `awk -v`. Row rewrites preserve trailing inline comments and spacing byte-exactly; conf writes go through a tmp file + `mv` (mode preserved), never in place.
- `agent_exec` owns all redirection: wrappers must not redirect its stdout/stderr to a file, or dry-run output never reaches them. It exports nothing — provenance comes from the wrapper's own `agents_resolve`.
- Callers pass **absolute** prompt/output/log paths to `agent_exec`.
- Provenance files are four lines: `task=`, `family=`, `agent=`, `effort=`.
- clean-fix runs unattended via launchd every 10 minutes (`com.natemccoy.style-fix.plist`, `StartInterval=600`, no idle gate). `agents_config.sh`, `agent_assignments.sh`, the three stage scripts, and `clean_fix_report_parse.py` must never be left broken, and the resolver must keep working under `/bin/bash` (3.2).
- `/plan:delegate` is itself implemented by `scripts/delegate/*`, so any rename or signature change to those launchers must land together with the `commands/plan/delegate.md` call-site edits in one change.
- `cf_load_stage_assignment` keeps its five-argument out-var signature; `clean-fix-usage.sh` and the print helpers call it positionally.
- The report parser slices launchd runs on exact substrings of the driver's stage-start lines — a reword must keep those leading phrases byte-identical or update the parser in the same change.
- In the clean-fix stage scripts, do **not** add family guards around the exec-marker transcript filter or the usage-limit detection: the first handles both families by pattern union, the second's codex-worded grep no-ops on claude logs, and the durable lines print `(${STYLE_AGENT} …)` with the parser accepting any family word.
- Before committing, inspect `git diff config/agents.conf`: an unsandboxed run that sources the resolver can legitimately rewrite `[codex.agents]`. Fold sync drift in deliberately or exclude it, never let it ride silently. `settings.json`'s app-generated key reorder stays excluded, and `settings.json`'s `model` / `statusLine`, `~/.zshrc`'s interactive `claude` alias, `scripts/claude_to_codex/`, and `~/.codex/config.toml` are out of scope.
- Never use `AskUserQuestion` in the command docs; the review docs decide via in-session synthesis.

## Calibration / gotchas

- **bash 3.2.** macOS ships bash 3.2 — no associative arrays, no `${var,,}`, no bash-4 anything. The clean-fix pipeline scripts run under `#!/bin/bash`; the registry, delegate, cli_agent, and ask_a_friend scripts use `#!/usr/bin/env bash` (also 3.2 here).
- **bash only, never zsh.** The resolver uses `BASH_REMATCH` and process substitution. Sourcing it from zsh hangs or misbehaves — run every test and probe as `bash <script>` / `bash -c '…'`.
- **Sourcing fires the sync.** `agents_config.sh`'s freshness gate can shell out to the sync (which itself shells out to `claude --help`, ~1-2 s) and can hang in a network-blocked sandbox. Tests and probes suppress it by exporting `CODEX_CATALOG_SYNC_STATE_FILE` at a freshly `touch`ed temp file *before* sourcing.
- **Sandbox write denials.** `~/.claude/config` is a sandbox deny path and `~/.local/state/` is outside the write allowlist, so anything that rewrites `config/agents.conf` or completes the sync (`/agent` edits, conf round-trips, warming the freshness gate) must run with `dangerouslyDisableSandbox: true`. Sandboxed, only the `mktemp` fails and the sync's warn-and-continue masks it as a merely-stale catalog.
- **Double resolution per launch.** Wrapper (provenance) plus `agent_exec` (execution) each source `agents_config.sh`, so a stale freshness gate fires the sync twice and doubles its `WARNING:` lines. Expected, not a defect.
- **Absolute paths for `agent_exec`.** The claude branch redirects after `cd <working_dir>`, so relative output/log paths resolve against `working_dir` there but against the caller's cwd on the codex branch (the prompt file is read pre-`cd` in both).
- **`AGENT_EXEC_EXTRA_ARGS` is whitespace-split with no quote interpretation.** Flag+value pairs (`--add-dir /path`) work; no single argument may contain a space — no prompt preambles, no `--settings` JSON.
- **`AGENT_EXEC_DRY_RUN=1`** is the testing hook: `%q`-quoted argv plus redirection suffix, with a `cd <dir> && ` prefix on the claude branch. Match smoke checks on substrings (`--full-auto`, `--sandbox read-only`, `-m <agent>`, the effort word), never whole lines — the codex effort token renders with escaped quotes (`model_reasoning_effort=\"high\"`).
- **awk gotchas.** `function` is a reserved awk word — pass it as `-v fn=`. `awk -v` decodes backslash escapes, so a value containing `\n` / `\t` would corrupt the row; user-supplied values go through `ENVIRON["…"]`.
- **`codex --sandbox read-only` panics** codex's system-configuration crate on macOS in some contexts, which is why ask_a_friend's conceptually read-only consult runs `write` (`--full-auto`). The delegate reviewer's `--sandbox read-only` usage is proven and stays. Codex launched from a Claude Code session needs `dangerouslyDisableSandbox: true` (and usually `run_in_background: true`).
- **`service_tier="fast"`** is passed on both codex paths in `cli_agent.sh`, including the interactive REPL — keep it.
- **Last-writer-wins on conf rewrites.** The source-time sync and the `/agent` editors both rewrite the file with tmp + `mv` and no locking; interleaved writers can silently revert each other's change but never corrupt the file.
- **claude-family output needs `python3`.** The claude branch logs stream-JSON and extracts the final result event into the output file; without `python3` the output file would be empty even though the log is complete. Claude-family `readonly` reviewers running the style loader script under `--permission-mode plan --print` is untested — a family switch may silently degrade style loading.
- **`_agents_registry_get` returns 0 on a miss** (prints nothing) so it is safe under `set -e` in command substitution; use `_agents_registry_has_key` when you need presence as a condition.
- **Editing a live launcher in place** (a script currently running) produces a spurious `unexpected EOF` exit 2 after the real work completes — bash re-reads the modified file at a stale byte offset. Check the status file and diff before treating it as a failure.
- Sync `WARNING:` lines land in launchd stderr logs; they are not stage failures, and the usage-limit regexes are written not to match them.

## Why it is this way

- **Warn-and-keep for vanished codex models.** If the sync simply dropped a model an assignment still uses, `agents_resolve` would hard-fail at the config layer and wedge the whole registry — `agents_list_assignments`, `/agent status`, every `cf_load_stage_assignment`, including the unattended 10-minute launchd run. Keeping the previous catalog row leaves resolution green so a truly retired model fails only in that one stage's own execution log, and the warning names the stale row plus the exact `/agent` commands to fix it. The keep condition is deliberately "would this row still resolve against the refreshed catalog *and* the cache", not bare cache membership: a hidden-but-cached assigned model still disappears from the sync output, and a selected-but-uncached model is prepended with an empty effort list, so cache membership alone would still wedge `agent:effort` assignments. Auto-repointing assignments was rejected — assignment edits are a human call.
- **Warn-only for claude alias staleness.** The effort list for a new alias is a judgment call and omissions (haiku) are deliberate, so the sync reports and never edits. A help-text wording change or a missing `claude` binary degrades to a no-op rather than a false edit.
- **No locking on conf writes.** Interleaved writers can only lose an edit, never corrupt the file, and this is a single-user machine; a lock would add failure modes (stale locks in launchd context) worse than the loss it prevents.
- **No per-project overrides.** One global `config/agents.conf` governs everything. Per-project layering would reintroduce exactly the scattered, hard-to-audit assignment state the registry exists to prevent, and `/agent status` would stop being the truth.
- **Double resolution in the wrappers.** `agent_exec` deliberately exports nothing, so a wrapper that wants provenance resolves again itself. Both reads hit the same conf, so they agree; the alternative — `agent_exec` exporting or writing resolved values — would couple every consumer to the launcher's variable names and make the launcher responsible for file layout it does not own.
- **Effort omission for a bare agent pair.** A row of just `agent` means "omit the effort flag entirely and let the CLI pick", which is materially different from any explicit level and is the only thing that validates against a catalog row with no reasoning levels. Hence no default-effort fallback anywhere: the stage scripts guard the flag with `[[ -n … ]]` rather than substituting a level, so the registry's silence is transmitted faithfully to the CLI.
