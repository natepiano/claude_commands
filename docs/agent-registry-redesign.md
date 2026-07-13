# Agent Registry Redesign — one place for family/agent/effort assignments

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** One registry
> (`config/agents.conf`) + one resolver + one shared launcher for every
> external-CLI agent consumer; each major function switches between the codex
> and claude families with a single assignment edit.

## Delegation Context

- **Project:** `/Users/natemccoy/.claude` — a git repo of personal Claude Code configuration (shell scripts, markdown skill/command docs, and clean-fix Python report tooling); this plan reworks how every external-CLI agent consumer resolves family/model/effort through one registry.
- **Stack:** Bash (mixed shebangs — see below), one Python report parser (`clean_fix_report_parse.py`) plus sibling `.py` clean-fix modules, INI-style `.conf` files, JSON config, and Markdown command/skill docs. Not Rust. Bash gotcha: macOS ships bash 3.2, so no bash-4 features (no associative arrays / `${var,,}`); registry/delegate/cli_agent/ask_a_friend scripts use `#!/usr/bin/env bash` while the clean-fix pipeline scripts (`clean-fix.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh`) use `#!/bin/bash` (`clean-fix-usage.sh` uses `env bash`). Python: basedpyright (zed's LSP) must report zero errors and zero warnings.
- **Layout:** `config/` registry + per-consumer confs and READMEs; `scripts/agents/` the resolver + codex-catalog sync + tests; `scripts/delegate/` codex implement/review launchers + old profile config + its test + `prepare_session.sh`; `scripts/cli_agent/` zshrc-alias dispatcher + its private conf; `scripts/clean-fix/` the launchd style pipeline (drivers, stage scripts, usage/report, Python parsers, README, plists); `scripts/ask_a_friend/` two codex launchers + `prepare_session.sh`; `commands/` + `commands/plan/` the Markdown command docs that call these scripts.
- **Key files:**
  - `config/agents.conf` — the registry; legacy `[codex]`/`[claude]` defaults + `[codex.models]`/`[codex.efforts]`/`[claude.models]`/`[claude.efforts]` sections plus the new schema (Phase 1: `[assignments]`, `[<function>.<family>]` sets, `[<family>.agents]` catalogs), with `[codex.agents]` live-synced from the cache since Phase 2; loses the legacy sections (Phase 11).
  - `config/delegate.conf` — per-profile agent/effort for delegate launchers; deleted in Phase 4.
  - `config/README.md` — describes `agents.conf` (the `## agents.conf` block); rewritten in Phase 11.
  - `config/orphans_expected.json` — `{"scripts":[],"config":[]}`, empty; nothing to do.
  - `scripts/agents/agents_config.sh` — the ini reader/resolver; current funcs prefixed `agents_config_*` (`agents_config_trim`, `_model`, `_effort`, `_allowed_*`, `_validate_*`, `_apply_defaults`); gains the new API (Phase 1), loses unused legacy funcs (Phase 11).
  - `scripts/agents/sync_codex_catalog.sh` — writes the codex catalog from `~/.codex/models_cache.json`; retargeted `[codex.models]` → `[codex.agents]` in Phase 2.
  - `scripts/agents/test_sync_codex_catalog.sh` — test for the sync script; updated in Phase 2.
  - `scripts/delegate/codex_implement.sh` — codex write launcher → `implement.sh` (Phase 4).
  - `scripts/delegate/codex_review.sh` — codex readonly review launcher → `review.sh` (Phase 4).
  - `scripts/delegate/delegate_config.sh` — `delegate_config_resolve` profile reader; deleted in Phase 4.
  - `scripts/delegate/test_delegate_config.sh` — its test (self-contained, `bash <script>`); deleted in Phase 4.
  - `scripts/cli_agent/cli_agent.sh` — zshrc-alias dispatcher; funcs `cli_agent_load/print_status/set/run`; codex `service_tier="fast"` at lines 112/123; migrated in Phase 6.
  - `scripts/cli_agent/agent-assignment.conf` — private per-alias conf; deleted in Phase 6.
  - `scripts/clean-fix/agent_assignments.sh` — `cf_load_stage_assignment` + `cf_*` validators; reworked in Phase 7.
  - `scripts/clean-fix/agent-assignments.conf` — per-stage `[style_eval]/[style_eval_review]/[style_fix]` with enabled/agent/model/effort; stripped to `enabled=` only in Phase 7.
  - `scripts/clean-fix/style-eval-all.sh` — eval stage; `case "$STYLE_AGENT"` dispatch + codex exec-marker transcript filtering (~line 110).
  - `scripts/clean-fix/style-eval-review-all.sh` — review stage; same `case` dispatch.
  - `scripts/clean-fix/style-fix-worktrees.sh` — fix stage; `case` dispatch + codex usage/weekly-limit detection (~line 434).
  - `scripts/clean-fix/clean-fix.sh` — pipeline driver; `cf_load_stage_assignment` calls at lines 225-229, agent log lines ~327+, report render is a bare `claude --print` at line 370.
  - `scripts/clean-fix/clean-fix-usage.sh` — usage screen; `print_stage_json` (lines 90/95), `print_stage_text` (lines 433/438/444) render agent/model/effort columns; help text ~44-45.
  - `scripts/clean-fix/clean_fix_report_parse.py` — report parser; codex usage-limit wording / `codex-usage-limit` reason codes at ~1166/1194/1836; must stay basedpyright-clean.
  - `scripts/clean-fix/README.md` — documents the `agent-assignments.conf` per-stage override schema (~line 18); rewritten in Phase 8.
  - `scripts/ask_a_friend/ask_a_friend.sh` — per-round consultation launcher (runs `--full-auto` deliberately); migrated in Phase 9.
  - `scripts/ask_a_friend/codex_implement.sh` — implementation launcher → `implement.sh` (Phase 9).
  - `commands/plan/delegate.md` — /plan:delegate orchestration; launcher call sites at lines 213 (implement), 266 (review), 389 (fix); `<SelectProfile>` at 193; `IMPLEMENTATION_PROFILE`/`FIX_PROFILE` wording; log-filename + provenance refs.
  - `commands/ask_a_friend.md` — resolution blurb at lines 15-16, call sites at 88 and 285, `codex.log`/`impl_codex.log` refs at 93 and 291.
  - `commands/cli_agent.md` — the /cli_agent doc; replaced by `commands/agent.md` in Phase 5.
  - `commands/clean_fix.md` — configure-agents surface (`agent`/`eval|review|fix` subcommands) around lines 267-300; shrinks to a status view in Phase 8.
  - `commands/team_review.md` — `<LaunchExpertTeam>` (~86-117) Agent-tool subagents; migrated in Phase 10.
  - `commands/api_review.md` — 5 reviewers (~63) + 2 adversaries (~116); migrated in Phase 10.
  - `commands/module_review.md` — pass 1 (~79) / pass 2 (~192) reviewers + pass 3 (~226) validation; migrated in Phase 10.
  - `.claude/settings.local.json` — at `/Users/natemccoy/.claude/.claude/settings.local.json`; `ask_a_friend/codex_implement.sh` permission entries at lines 20 and 73 updated in Phase 9; `Bash(codex exec:*)` (26) and `Bash(pkill -f 'claude --print')` (33) still match after migration.
  - `~/.codex/models_cache.json` — external input the sync script parses; per-model `supported_reasoning_levels[].effort`.
- **Build:** None — no compile/build step for this repo.
- **Test:** Standalone bash test scripts run directly (`bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_sync_codex_catalog.sh`, `bash scripts/agents/test_agent_exec.sh` — the first and last are created by this plan); each is self-contained (uses `mktemp -d`, sources the script under test, prints a "…passed" line and exits nonzero on failure). `scripts/delegate/test_delegate_config.sh` exists until Phase 4 deletes it. The resolver code is bash-only (`BASH_REMATCH`, process substitution) — run every test and manual smoke via `bash <script>` / `bash -c`, never zsh. Sourcing `agents_config.sh` fires the codex-catalog freshness sync (can hang in a network-blocked sandbox); fixtures and probes suppress it by exporting `CODEX_CATALOG_SYNC_STATE_FILE` pointing at a freshly `touch`ed temp file before sourcing.
- **Lint:** No project-wide shellcheck harness. For Python, basedpyright must report zero errors and zero warnings (`clean_fix_report_parse.py` edits must stay clean); never add file-level type ignores.
- **Style:** Not Rust — no Rust style loader. Repo conventions in the touched scripts: `set -euo pipefail` at the top of the `#!/usr/bin/env bash` scripts; function-name prefixes namespaced by module — `agents_*` (resolver), `cf_*` (clean-fix), `cli_agent_*` (alias dispatcher); ini sections rewritten in place via awk. Codex effort is passed as `-c model_reasoning_effort="…"` and omitted entirely when empty (empty effort = "use CLI default"). Use allowlist/denylist vocabulary, never whitelist/blacklist.
- **Invariants:**
  - clean-fix runs unattended via launchd every 10 minutes (`com.natemccoy.style-fix.plist`, `StartInterval=600`, no idle gate) — the clean-fix scripts and `clean_fix_report_parse.py` must never be left broken at the end of any phase.
  - `/plan:delegate` is itself implemented by `scripts/delegate/*` — the very tooling dispatching this plan — so the delegate launchers must work at the end of every phase; the Phase 4 renames and the `commands/plan/delegate.md` call-site edits must land together in that one phase.
  - The migration is a strangler: Phase 1 **adds** the new schema sections and resolver API alongside the legacy ones; every pre-Phase-11 phase leaves the legacy sections/functions in `config/agents.conf` / `agents_config.sh` untouched so unmigrated consumers keep working; Phase 11 strips them once nothing references them.
  - Accepted transitional behavior (Phase 2+): the sync stopped updating the legacy `[codex] model=` mirror, so unmigrated consumers (delegate until Phase 4, cli until 6, clean-fix until 7, ask_a_friend until 9) no longer track codex's selected-model changes — harmless while the values coincide, but pressure against long pauses between Phase 2 and Phases 4-9.
  - codex is launched with `dangerouslyDisableSandbox: true` (and `run_in_background: true`) from Claude Code sessions; `codex --sandbox read-only` panics codex's system-configuration crate on macOS, which is why ask_a_friend runs `--full-auto` (write) even though the consult is conceptually read-only. The delegate reviewer's `--sandbox read-only` usage is proven and stays.
  - The interactive codex REPL keeps `-c service_tier="fast"` (cli_agent.sh lines 112/123).
  - Never use `AskUserQuestion` in the command docs; the migrated review docs decide via in-session synthesis.
  - Accepted risk: the source-time catalog sync and the `/agent` assignment/row editors both rewrite `config/agents.conf` (tmp file + `mv`, no locking); interleaved writers can silently revert the other's change (last-writer-wins) but never corrupt the file. Acceptable on a single-user machine — do not add locking.
  - `agent_exec` callers must pass absolute prompt/output/log paths: the claude branch redirects after `cd <working_dir>`, so relative output/log paths resolve against `working_dir` there but against the caller's cwd on the codex branch (`<prompt_file>` is read pre-`cd` in both).
  - `AGENT_EXEC_EXTRA_ARGS` is whitespace-split with no quote interpretation — flag+value pairs like `--add-dir /path` work, but no single argument may contain a space (no prompt preambles, no `--settings` JSON); no planned phase uses it.
  - Out-of-scope guardrails (all user-confirmed 2026-07-12): do not touch `~/.zshrc`'s interactive `claude` alias, `settings.json`'s `model` or `statusLine`, `scripts/claude_to_codex/`, or `~/.codex/config.toml` beyond the existing sync trigger; no per-project override mechanism — one global `config/agents.conf` governs everything.

## Phases

### Phase 1 — Registry core: new schema + resolver + tests  · status: done (`ae2c744`)

#### Work Order

**Goal:** `config/agents.conf` carries the new three-layer schema and `scripts/agents/agents_config.sh` resolves any `<function>.<subtask>` task to a validated (family, agent, effort) triple — with every legacy section, function, and consumer untouched and still working.

**Spec:**

Terminology (binds all phases): a **family** is a CLI vendor (`codex` | `claude`). An **agent** is a specific model within a family (`gpt-5.6-sol`, `opus`, …). A **function** is a major consumer (`delegate`, `cli`, `cleanfix`, `ask_a_friend`, `team_review`, `api_review`, `module_review`) containing **sub-tasks**. A task's full name is `<function>.<subtask>` — exactly two segments.

Append the following new sections to `config/agents.conf`, leaving the existing `[codex]`, `[claude]`, `[codex.models]`, `[claude.models]`, and `[codex.efforts]`-style legacy sections exactly as they are (Phase 11 removes them). Claude-side leveling values are first-cut defaults; codex effort lists are placeholders until Phase 2's sync writes the real ones:

```ini
# ── major function → family: the switch ──
[assignments]
delegate=codex
cli=codex
cleanfix=codex
ask_a_friend=codex
team_review=codex
api_review=codex
module_review=codex

# ── delegate (/plan:delegate) ──
[delegate.codex]
implementation=gpt-5.6-sol:high
review=gpt-5.6-sol:high
mechanical=gpt-5.6-sol:medium
escalation=gpt-5.6-sol:xhigh

[delegate.claude]
implementation=opus:max
review=opus:max
mechanical=sonnet:medium
escalation=fable:max

# ── cli (~/.zshrc aliases via scripts/cli_agent/cli_agent.sh) ──
[cli.codex]
style_fix_review=gpt-5.6-sol:high    # alias: review
commit_prep=gpt-5.6-sol:high         # aliases: commit_no, commit_yes
merge_branch=gpt-5.6-sol:high        # alias: merge
interactive=gpt-5.6-sol:high         # alias: code (REPL)

[cli.claude]
style_fix_review=opus:high
commit_prep=sonnet:high
merge_branch=sonnet:high
interactive=opus:max

# ── cleanfix (launchd style pipeline) ──
[cleanfix.codex]
style_eval=gpt-5.6-sol:xhigh
style_eval_review=gpt-5.6-sol:xhigh
style_fix=gpt-5.6-sol:xhigh
report=gpt-5.6-sol:medium

[cleanfix.claude]
style_eval=opus:max
style_eval_review=opus:max
style_fix=opus:max
report=sonnet:medium

# ── ask_a_friend ──
[ask_a_friend.codex]
consultation=gpt-5.6-sol:high
implementation=gpt-5.6-sol:high

[ask_a_friend.claude]
consultation=opus:max
implementation=opus:max

# ── review commands (/team_review, /api_review, /module_review) ──
[team_review.codex]
expert=gpt-5.6-sol:high          # 3-5 parallel dimension reviewers

[team_review.claude]
expert=opus:max

[api_review.codex]
reviewer=gpt-5.6-sol:high        # 5 parallel inventory/finding agents
adversary=gpt-5.6-sol:high       # 2 stress-test agents

[api_review.claude]
reviewer=opus:max
adversary=opus:max

[module_review.codex]
reviewer=gpt-5.6-sol:high        # pass 1 structure + pass 2 over-large files
validation=gpt-5.6-sol:medium    # pass 3 doc-vs-code validation

[module_review.claude]
reviewer=opus:max
validation=sonnet:medium

# ── validation catalogs: agent=comma-separated valid efforts ──
[codex.agents]
# Generated by scripts/agents/sync_codex_catalog.sh (Phase 2).
gpt-5.6-sol=low,medium,high,xhigh
gpt-5.6-terra=low,medium,high,xhigh
gpt-5.6-luna=low,medium,high,xhigh
gpt-5.5=low,medium,high,xhigh
gpt-5.4=low,medium,high,xhigh
gpt-5.4-mini=low,medium,high,xhigh
gpt-5.3-codex-spark=low,medium,high,xhigh

[claude.agents]
# Hand-maintained. Claude agent names are the CLI's short aliases.
fable=low,medium,high,xhigh,max
opus=low,medium,high,xhigh,max
sonnet=low,medium,high,xhigh,max
```

Each `[<function>.<family>]` row maps a sub-task to an `agent:effort` pair; a bare `agent` (no colon) means "omit the effort flag, use the CLI's default". Every function carries *both* family sets, fully specified at all times.

Resolution for task `T = f.s`:

```
family = [assignments] f          # exact task-name key f.s wins over f if present
pair   = [f.family] s             # "agent:effort" or "agent"
validate: agent ∈ [family.agents], effort ∈ that agent's effort list
→ (family, agent, effort)
```

The exact-task override (`delegate.review=claude` beating `delegate=codex`) is supported by the resolver for one-off cross-vendor setups, but function-level assignment is the norm and the only thing `/agent` writes.

Add to `scripts/agents/agents_config.sh` — new functions alongside the existing `agents_config_*` API (which stays byte-identical; legacy consumers still source it):

- `agents_resolve <task>` — sets `AGENT_FAMILY`, `AGENT_MODEL`, `AGENT_EFFORT` (effort may be empty = omit the flag). Errors loudly (nonzero, stderr names the missing/invalid piece and the allowed values) when: the function has no `[assignments]` entry, the set section or sub-task row is missing, the agent isn't in `[<family>.agents]`, or the effort isn't in that agent's list.
- `agents_resolve_print <task>` — one-line `task=… family=… agent=… effort=…` for status output and tests.
- `agents_list_assignments` — render the full resolution table (every `[assignments]` entry, the active set's rows, resolved pairs).
- `agents_set_assignment <function> <family>` — validate the family has a `[<function>.<family>]` section whose every row resolves, then rewrite the `[assignments]` line in place (awk rewrite, same pattern as `cli_agent_set` today). On any invalid row: reject, name the bad row, leave the file untouched.
- `agents_codex_args` → emits `-m "$AGENT_MODEL"` plus `-c model_reasoning_effort="$AGENT_EFFORT"` when effort is non-empty.
- `agents_claude_args` → emits `--model "$AGENT_MODEL"` plus `--effort "$AGENT_EFFORT"` when effort is non-empty.
- Keep: `agents_config_trim`, the low-level ini reader, and the freshness-triggered catalog sync at source time (unchanged behavior).

Create `scripts/agents/test_agents_config.sh` (same self-contained pattern as `test_sync_codex_catalog.sh`: `mktemp -d`, fixture conf, source the resolver with the fixture path, assert, print "…passed"). Cases: resolution happy path both families; exact-task override beats function assignment; missing assignment / set section / row; agent not in catalog; effort not in agent's list; bare `agent` pair → empty effort; `agents_set_assignment` rejects a family whose set has an invalid row and leaves the file untouched; `agents_codex_args`/`agents_claude_args` output with and without effort.

**Files:**
- `config/agents.conf` — append the new sections above; legacy sections untouched.
- `scripts/agents/agents_config.sh` — add the six new functions; legacy functions untouched.
- `scripts/agents/test_agents_config.sh` — new test script.

**Constraints from prior phases:** none (Phase 1).

**Acceptance gate:** `bash scripts/agents/test_agents_config.sh` passes; `bash scripts/delegate/test_delegate_config.sh` and `bash scripts/agents/test_sync_codex_catalog.sh` still pass unchanged (proves legacy surface untouched).

#### Retrospective

**What worked:** The strangler split held exactly as designed — lines 1-163 of `agents_config.sh` and all legacy conf sections are byte-identical, both legacy test suites pass unchanged. Fixture-conf testing (temp `AGENTS_CONFIG_FILE`) made every behavior verifiable without live CLIs.

**What deviated from the plan:** The new code does not use the legacy `_agents_config_get` for lookups — its unescaped-regex matching lets `.` match any character. Fix pass 1 added literal-comparison helpers `_agents_registry_get` (value; prints nothing and returns 0 on not-found — errexit-safe in `$(...)`) and fix pass 2 added `_agents_registry_has_key` (presence; 0/1 for `if` conditions). Two codex fix passes were used: (1) awk rejects `function` as a variable name, which had made `agents_set_assignment`'s rewrite path always fail; plus regex-lookup exactness, rejecting `agent:` (empty effort after colon), and deduping exact-override rows in `agents_list_assignments`; (2) distinguishing a catalog row with an empty effort list (`model=` — valid, bare pairs only) from a missing agent.

**Surprises:**
- The resolver is bash-only (`BASH_REMATCH`, process substitution); the orchestrator's shell is zsh, where sourcing it hangs/misbehaves — all manual checks and tests must run via `bash -c` / `bash <script>`.
- Sourcing `agents_config.sh` fires the catalog freshness sync, which can hang in a network-blocked sandbox; set `CODEX_CATALOG_SYNC_STATE_FILE` to a freshly touched temp file to suppress it in tests/probes.
- Empty-effort catalog rows are load-bearing for Phase 2 (the sync writes `model=` for models without reasoning levels); the resolver now supports them and `test_agents_config.sh` pins the behavior.

**Implications for remaining phases:** Phase 2's sync can rely on `model=` rows validating; consumers switching families via `agents_set_assignment` is proven (round-trips the conf byte-identically); Phase 3+ should resolve exclusively through `agents_resolve`/`agents_codex_args`/`agents_claude_args` and never re-derive flag vocabulary.

#### Phase 1 Review

- Phase 2: pending decision (sync drops a model an assignment still uses) resolved 2026-07-13 — warn-and-keep adopted; the warning must name the stale row and the `/agent` commands to reconfigure.
- Phase 2: sync must succeed on a conf without the legacy sections; `:` dropped from the slug charset (collides with pair syntax); alias-staleness check no-ops when no `claude` binary is on PATH.
- Phase 3: recorded the arg-emitters' one-line output contract (word-split into argv) and the provenance rule (wrappers re-resolve; `agent_exec` exports nothing); named the test-fixture pattern file.
- Phase 4: provenance values come from `agents_resolve` in the wrappers; acceptance grep narrowed to delegate paths so ask_a_friend (migrated in Phase 9) doesn't trip it.
- Phase 5: row editor moved into `agents_config.sh` with test coverage; constraints name the new lookup helpers, warn off `_agents_config_get`, and record the awk reserved-word gotcha and inline-comment preservation.
- Phase 7: gate gains a `/bin/bash` (3.2) smoke — the resolver's first execution under the system bash.
- Phase 8: report-render spec corrected (no existing prompt file or dedicated log — create both); clean_fix.md rewrite must retain the enable/disable subcommands.
- Phase 11: dead private helpers named for deletion; kept helpers named explicitly.
- Delegation Context: Test line carries the bash-only and sync-suppression facts; Invariants record the accepted last-writer-wins risk between the sync and `/agent` writes.

### Phase 2 — Catalog sync: `[codex.agents]` + claude alias staleness  · status: done (`7ef5164`)

#### Work Order

**Goal:** `sync_codex_catalog.sh` maintains the new `[codex.agents]` catalog with real per-model effort lists and warns when the claude CLI grows an alias missing from `[claude.agents]`.

**Spec:**

- Target section becomes `[codex.agents]`; each visible model is written as `slug=<comma-joined efforts>` where efforts come from `supported_reasoning_levels[].effort` in `~/.codex/models_cache.json` (order preserved). A model with no levels array gets an empty list (only bare-agent pairs validate against it).
- The selected-model prepend logic (which today prepends the `~/.codex/config.toml` selected model) now prepends into `[codex.agents]` (with its efforts if the cache has them, empty otherwise).
- Transition rule: the script **stops managing** the legacy `[codex] model=` mirror and `[codex.models]` section but does **not** delete them — they stay static in the file for unmigrated consumers until Phase 11 removes them.
- `--check` semantics unchanged.
- The retargeted sync must not *require* the legacy sections: today it hard-fails when `[codex] model=` / `[codex.models]` are absent (~lines 126-127). It must succeed on a conf containing only the new sections (the post-Phase-11 shape) while still leaving legacy sections byte-identical when they are present.
- Drop `:` from the allowed slug charset (~lines 42/70): `_agents_validate_pair` splits `agent:effort` pairs at the first colon, so a colon-bearing slug could never be assigned — skip such models with a stderr warning.
- Claude alias staleness check: the same sync run parses the quoted aliases from `claude --help`'s `--model` flag text (today: 'fable', 'opus', 'sonnet') and warns to stderr when one is missing from `[claude.agents]`. Warn-only, never auto-add — the effort list for a new alias is a human call, and exclusions like haiku are deliberate. A help-text wording change degrades to a no-op (no aliases parsed → no warning), never a false edit; likewise a missing `claude` binary on PATH (launchd context) degrades to a no-op.
- Vanished-model protection (user decision 2026-07-13: **warn-and-keep**): after rewriting `[codex.agents]`, the sync resolves every codex-assigned row; for any row whose agent is no longer in the cache it keeps that agent's previous catalog row (efforts unchanged) and warns to stderr. Rationale: without the kept row, `agents_resolve` hard-fails at the config layer and wedges the whole registry (`agents_list_assignments`, `/agent status`, every `cf_load_stage_assignment`) — including the unattended 10-minute launchd run; with it, resolution stays green and a truly-retired model fails only in that one stage's own execution logs. Never auto-edit assignments (auto-repoint rejected — assignment edits are a human call).
- The warning must tell the user to reconfigure agents, naming the stale row and the `/agent` commands to fix it, e.g.: `WARNING: cleanfix.style_eval is assigned to 'gpt-5.6-sol', which is gone from the codex catalog — re-point it: /agent cleanfix.style_eval <agent>[:<effort>], or switch the family: /agent cleanfix <family>`. One warning per stale row, every sync run, until fixed.
- Update `scripts/agents/test_sync_codex_catalog.sh` for the new output shape: `[codex.agents]` rows with efforts, legacy sections left byte-identical when present, sync succeeds on a new-sections-only conf, empty-levels model → empty list, colon-bearing slug skipped with a warning, alias-staleness warn fires on a fixture missing an alias and stays silent when help text parses to nothing, and a cache that drops a still-assigned model → previous catalog row preserved, stderr warning names the stale row and `/agent`.

**Files:**
- `scripts/agents/sync_codex_catalog.sh` — retarget + staleness check.
- `scripts/agents/test_sync_codex_catalog.sh` — updated cases.

**Constraints from prior phases:** Phase 1 defined `[codex.agents]` / `[claude.agents]` as `agent=comma-separated-efforts` rows and seeded placeholder codex rows; this phase's sync overwrites the `[codex.agents]` body with real cache data. The resolver validates efforts against these rows, so a sync that wrote wrong shapes would break `agents_resolve` — run `bash scripts/agents/test_agents_config.sh` after changes.

**Acceptance gate:** `bash scripts/agents/test_sync_codex_catalog.sh` and `bash scripts/agents/test_agents_config.sh` pass; a real run against `~/.codex/models_cache.json` rewrites `[codex.agents]` to match the cache and leaves the legacy `[codex] model=` / `[codex.models]` lines byte-identical.

#### Retrospective

**What worked:** Single codex pass, zero fix passes; both test suites green and the real-run diff touched only `[codex.agents]` effort lists — legacy sections byte-identical, proving the stops-managing transition.

**What deviated from the plan:** Vanished-model detection keys on "would the row still resolve against the refreshed `[codex.agents]`" — membership in the *visible* catalog AND the cache — not cache membership alone. Required: a hidden-but-cached assigned model is dropped from the sync output (row must be kept or the resolver wedges), and a selected-but-uncached model is prepended with an *empty* effort list (previous row must be kept or `agent:effort` assignments fail validation). The blind reviewer read the spec's "no longer in the cache" literally and filed a blocker; overruled — the spec's own wedge-prevention rationale demands the implemented condition.

**Surprises:**
- The live cache now reports `max`/`ultra` efforts for the gpt-5.6 models, so `[codex.agents]` rows exceed the legacy `[codex.efforts]` list — harmless; the new resolver validates per-agent, not against the legacy list.
- An assigned agent with no previous `[codex.agents]` row hard-fails the sync ("cannot preserve") — acceptable: that state means the registry was already broken before the sync ran, and `/agent` cannot create it.
- The sync now shells out to `claude --help` on every triggered run (~1-2s when the freshness gate fires); alias warnings will surface in launchd stderr logs.

**Implications for remaining phases:** Phase 11 can strip the legacy sections knowing the sync no longer reads or writes them; `[codex.agents]` effort lists are now live cache data, so later phases must not hard-code effort vocabularies in tests against the real conf (use fixture confs, as Phases 1-2 do).

#### Phase 2 Review

- Phase 11: the sync test's fixtures pin legacy-section pass-through using literal `[codex.models]`/`[codex.efforts]` names, which would trip Phase 11's clean-grep gate — Phase 11 now renames those fixture sections to neutral names and lists the test file; its verification grep widened to cover the allowed/validate/apply_defaults legacy helpers and the `[*.efforts]` sections.
- Phase 7: gained the accepted transitional-gap note (until Phase 8, `/clean_fix agent …` doc text still writes conf keys the phase removes — ignored, harmless), the out-of-file-list callers of `cf_load_stage_assignment` (clean-fix-usage.sh lines 95/438, print helpers), and the fact that sync `WARNING:` lines can appear in launchd logs; its gate no longer hard-codes `gpt-5.6-sol:xhigh`.
- Phase 8: closes the Phase 7 transitional gap via the clean_fix.md rewrite; parser constraint added — generalized usage-limit patterns must not match the sync's new `WARNING:` lines.
- Phase 5: constraint added that `agent_admin.sh`'s argument grammar must match the `/agent` command forms the Phase 2 warnings already print (sequencing pressure to ship promptly); round-trip gate snapshots the conf after one resolver source so a freshness sync can't fake a diff; post-ship note that revisiting `xhigh`-era effort choices (`max`/`ultra` now exist) is a user `/agent` pass, not plan work.
- Phase 4: gate reads the current `[delegate.codex]` row at run time instead of hard-coding `gpt-5.6-sol:high`.
- Delegation Context: recorded the accepted transitional freeze of the legacy `[codex] model=` mirror (unmigrated consumers stop tracking codex model switches until their migration phase) and refreshed the agents.conf key-file description.
- Blind-review disagreement resolved (no plan change): the reviewer read "vanished = not in cache" literally and called the implemented keep-condition (must resolve against the refreshed visible catalog AND the cache) a blocker; overruled — cache-membership alone would still drop hidden-but-cached assigned models from `[codex.agents]` and wedge the resolver, defeating the decision's stated purpose.

### Phase 3 — Shared launcher `scripts/agents/agent_exec.sh`  · status: done (`56861f9`)

#### Work Order

**Goal:** One family-dispatch launcher that every consumer (delegate, ask_a_friend, clean-fix, review commands) can call, testable without invoking a real CLI.

**Spec:**

```
agent_exec <task> <mode:write|readonly> <working_dir> <prompt_file> <output_file> <log_file>
```

- Resolves via `agents_resolve <task>` (sources `agents_config.sh`).
- codex write: `codex exec -m … [-c model_reasoning_effort="…"] --ephemeral --full-auto -C <working_dir> -o <output_file> "$PROMPT" > <log_file> 2>&1` — flags via `agents_codex_args`.
- codex readonly: same with `--sandbox read-only` replacing `--full-auto`.
- claude write: `claude --print --dangerously-skip-permissions --settings '{"sandbox":{"enabled":false}}' --model … [--effort …] -- "$PROMPT" > <output_file> 2> <log_file>` — flags via `agents_claude_args`; claude prints the final message to stdout, there is no transcript log — stderr is the log.
- claude readonly: same but `--permission-mode plan` replaces `--dangerously-skip-permissions`.
- `$PROMPT` is the contents of `<prompt_file>`; error out (nonzero, message to log file) if the prompt file is missing — same contract as today's delegate launchers.
- Extra per-consumer flags (e.g. codex `--add-dir`, prompt preambles) pass through via an `AGENT_EXEC_EXTRA_ARGS` env hook appended to the family CLI's arg list — keep the signature minimal; clean-fix may keep its codex-specific preamble where it is if wiring it through is awkward.
- Testability: when `AGENT_EXEC_DRY_RUN=1` is set, print the fully assembled command line (one shell-quoted token per argument) to stdout and exit 0 without executing. This is the hook every later phase's smoke gate uses.
- Create `scripts/agents/test_agent_exec.sh` (self-contained fixture-conf pattern from Phase 1): asserts the assembled command for all four family × mode combinations, effort-flag omission when the pair is bare, `AGENT_EXEC_EXTRA_ARGS` pass-through, and missing-prompt-file failure.

**Files:**
- `scripts/agents/agent_exec.sh` — new.
- `scripts/agents/test_agent_exec.sh` — new.
- `scripts/agents/test_agents_config.sh` — pattern reference only (fixture conf + sync-suppression touch); not modified.

**Constraints from prior phases:** Phase 1 provides `agents_resolve` (sets `AGENT_FAMILY`/`AGENT_MODEL`/`AGENT_EFFORT`, empty effort = omit flag) and the `agents_codex_args`/`agents_claude_args` emitters in `scripts/agents/agents_config.sh` — use them; do not re-implement flag vocabulary here. The emitters print one space-joined line whose effort token carries literal embedded quotes (`model_reasoning_effort="high"` is a single argv token) — word-split the line into an argv array (e.g. `read -r -a`), matching the existing launchers' convention. Consumers that write provenance (Phases 4/9) re-resolve by sourcing `agents_config.sh` and calling `agents_resolve <task>` themselves — `agent_exec` does not export or write resolved values (double resolution is consistent: both read the same conf).

**Acceptance gate:** `bash scripts/agents/test_agent_exec.sh` passes; `bash scripts/agents/test_agents_config.sh` still passes.

#### Retrospective

**What worked:** Fast-path dispatch straight from the Work Order; codex implemented to spec with no deviations; the dry-run test pins the exact assembled argv for all four family × mode combos.
**What deviated from the plan:** One fix pass. The spec's claude command line had no working-directory mechanism (the claude CLI has no `-C` flag), so a claude-family task would have run against the caller's cwd. Fixed: the claude branch executes via subshell `( cd "$working_dir" && … )`, and dry-run prints a `cd <working_dir> && ` prefix before the claude argv.
**Surprises:** The blind reviewer caught the working_dir gap that both the spec and the conformance review missed — spec-conformance review cannot catch spec gaps. `AGENT_EXEC_EXTRA_ARGS` is whitespace-split with no quote interpretation (args cannot contain spaces, matching the resolver emitters' convention) — now documented in a comment at `agent_exec.sh:48`.
**Implications for remaining phases:** Dry-run output shapes for smoke gates (Phases 4, 9, 10): codex = `codex exec … "PROMPT" > <log> 2>&1` (no prefix); claude = `cd <working_dir> && claude … -- "PROMPT" > <out> 2> <log>`. Gates matching assembled commands should match on flags/substrings, not whole lines, or account for the claude `cd` prefix and both families' redirection suffixes.

#### Phase 3 Review

- Copied the dry-run output shapes (`%q`-quoted tokens, claude `cd` prefix, redirection suffixes, escaped effort quotes) into Phases 4/8/9's Constraints — gates must match substrings, not whole lines.
- Phase 4 and 9 constraints now state the wrappers must not redirect `agent_exec`'s stdout (it owns all redirection; dry-run must reach the wrapper's stdout) and that `agent_exec` already handles the missing-prompt log write.
- Phase 4/9 gates note they assume the shipped codex family assignment (registry is live-editable).
- Phase 7's gate parenthetical corrected: `env bash` IS 3.2 on this machine and the resolver already passes under it — the gate now pins first-launchd-context execution instead.
- Phase 8's report render gained its missing `working_dir` (`$HOME/.claude`), output-file, and failure-guard facts plus a direct-`agent_exec` smoke recipe (don't smoke through the whole driver).
- Phase 10's acceptance grep widened (`subagent_type\|Explore agents\|using the Agent tool`) — the bare pattern was vacuous for two of the three docs; added a warm-the-freshness-gate step before its parallel launches.
- Two new Delegation Context invariants: `agent_exec` callers pass absolute prompt/output/log paths (claude redirects post-`cd`); `AGENT_EXEC_EXTRA_ARGS` is whitespace-split, no space-containing argument, and no planned phase uses it.

### Phase 4 — Delegate migration: family-neutral launchers  · status: todo

#### Work Order

**Goal:** `/plan:delegate` dispatches through family-neutral `implement.sh`/`review.sh` that resolve via the registry; the bespoke delegate profile plumbing is gone.

**Spec:**

- `git mv scripts/delegate/codex_implement.sh scripts/delegate/implement.sh` and `git mv scripts/delegate/codex_review.sh scripts/delegate/review.sh`. Signature keeps `<session_dir> [working_dir] [prompt_file] [task]` where the old profile arg becomes the sub-task (`implementation` | `mechanical` | `escalation`; review script defaults to `review`). Internally: prefix the sub-task with `delegate.`, then call `agent_exec delegate.<subtask> write …` (implement) / `agent_exec delegate.<subtask> readonly …` (review) — the bodies collapse to argument handling, status-file writes, provenance, and the `agent_exec` call.
- Status-file protocol unchanged: `impl_status`/`review_status` write `implementing|implemented|error` / `reviewing|reviewed|error` exactly as today.
- Provenance files (`impl_agent`, `review_agent`) record `task=/family=/agent=/effort=` (replacing `profile=/agent=/model=/effort=`) — values obtained by sourcing `scripts/agents/agents_config.sh` and calling `agents_resolve delegate.<subtask>` in the wrapper; `agent_exec` does not export them.
- Log files go agent-neutral: `impl_codex.log` → `impl_agent.log`, `review_codex.log` → `review_agent.log`.
- Delete `scripts/delegate/delegate_config.sh`, `scripts/delegate/test_delegate_config.sh`, and `config/delegate.conf` (all superseded by the resolver and its tests).
- `commands/plan/delegate.md`: update the three launcher call sites (lines 213 implement, 266 review, 389 fix) to the new script names and sub-task args; rename `IMPLEMENTATION_PROFILE`/`FIX_PROFILE` wording from "profile" to "task"; update the log-filename references and the provenance note near the end; rewrite `<SelectProfile>` (~line 193) — its factual claim "profile effort is configured in `~/.claude/config/delegate.conf`; the model in `agents.conf`" is false after this phase; the replacement states that `agents.conf` `[delegate.<family>]` rows own both agent and effort, switched via `/agent`.
- These edits are atomic: the renames and the delegate.md call-site updates must land in this single phase — `/plan:delegate` runs on these scripts.

**Files:**
- `scripts/delegate/implement.sh` — renamed + rewritten around `agent_exec`.
- `scripts/delegate/review.sh` — renamed + rewritten around `agent_exec`.
- `scripts/delegate/delegate_config.sh` — delete.
- `scripts/delegate/test_delegate_config.sh` — delete.
- `config/delegate.conf` — delete.
- `commands/plan/delegate.md` — call sites, wording, log names, `<SelectProfile>`.

**Constraints from prior phases:** Phase 3's `agent_exec <task> <mode> <working_dir> <prompt_file> <output_file> <log_file>` with `AGENT_EXEC_DRY_RUN=1` printing the assembled command; Phase 1's registry has `[delegate.codex]`/`[delegate.claude]` rows for `implementation`, `review`, `mechanical`, `escalation` and `[assignments] delegate=codex`. Dry-run output shapes (Phase 3): one line per invocation, every argv token `printf '%q'`-quoted, plus a redirection suffix — codex: `codex exec -m <agent> [-c model_reasoning_effort="<effort>"] --ephemeral --full-auto|--sandbox read-only -C <dir> -o <output> <prompt> > <log> 2>&1` (no prefix); claude: `cd <dir> && claude --print … -- <prompt> > <output> 2> <log>`. The codex effort token renders with escaped quotes (`model_reasoning_effort=\"high\"`) — match gate checks on substrings (`--full-auto`, `-m <agent>`, the effort word), never on literal flag text or whole lines. `agent_exec` performs all log/output redirection itself — the wrapper must not redirect the `agent_exec` call's stdout/stderr to files (dry-run output must reach the wrapper's stdout for the gate); `agent_exec` already writes `Prompt not found: <path>` to `<log_file>` and returns 1 on a missing prompt file, so the wrapper only maps nonzero exits to the `error` status write.

**Acceptance gate:** `AGENT_EXEC_DRY_RUN=1 bash scripts/delegate/implement.sh <tmp_session> . <tmp_prompt> implementation` prints a codex `--full-auto` command whose model/effort flags match the *current* `[delegate.codex] implementation` row (the registry is live-editable via `/agent` — read the row at run time, don't hard-code `gpt-5.6-sol:high`); same for `review.sh` printing `--sandbox read-only` (both assume the shipped `[assignments] delegate=codex`; if the family was switched via `/agent`, expect the claude shape instead — `cd` prefix, `--dangerously-skip-permissions`/`--permission-mode plan`); `grep -rn "codex_implement\|codex_review\|delegate_config\|delegate\.conf" scripts/delegate/ commands/plan/ config/` returns no live references (ask_a_friend keeps its own `codex_implement.sh` until Phase 9, so the grep is scoped to delegate paths); `bash scripts/agents/test_agents_config.sh` and `bash scripts/agents/test_agent_exec.sh` pass.

### Phase 5 — `/agent` skill: registry administration  · status: todo

#### Work Order

**Goal:** `/agent` shows and edits the registry; `/cli_agent` is retired.

**Spec:**

Create `scripts/agents/agent_admin.sh` — thin CLI over the Phase 1 resolver functions:

- `agent_admin.sh status` (also the no-arg default) — `agents_list_assignments`: full table of each function, its family, and the active set's resolved rows.
- `agent_admin.sh <function> <codex|claude>` — `agents_set_assignment`; refuses if any row of the target set fails validation, naming the bad row.
- `agent_admin.sh <function>.<subtask> <agent>[:<effort>]` — edit one row of the *active* set for that function: validate agent ∈ `[<family>.agents]` and effort ∈ that agent's list, then awk-rewrite the row in place; invalid input leaves the file untouched. Implement the rewrite as a resolver-level function in `scripts/agents/agents_config.sh` (e.g. `agents_set_row`) with cases in `test_agents_config.sh` — `agent_admin.sh` stays a thin dispatcher. The rewrite must preserve a row's trailing inline comment (real conf rows carry `# alias: …` comments).

Create `commands/agent.md` — the `/agent` skill, a thin wrapper that runs `agent_admin.sh` with the user's args and relays script stdout/stderr exactly; on error, stop (no guessed corrections) — same contract as today's `/cli_agent`. Subcommands documented: `status` (default), `<function> <family>`, `<function>.<subtask> <agent>[:<effort>]`.

Delete `commands/cli_agent.md`. Known transitional gap (accepted): until Phase 6, `cli_agent.sh` still reads its private `agent-assignment.conf`, so `/agent cli …` edits don't affect the zshrc aliases yet — note this in the `/agent` doc's status output section and remove the note in Phase 6.

Post-ship note (no plan-time edits): the live `[codex.agents]` catalog now advertises `max`/`ultra` efforts for the gpt-5.6 models; the seeded `[<function>.codex]` rows were written when `xhigh` was the ceiling. Revisiting those effort choices is a user `/agent` pass after this phase ships, not part of any phase.

**Files:**
- `scripts/agents/agent_admin.sh` — new.
- `scripts/agents/agents_config.sh` — add the row-edit function beside the Phase 1 API.
- `scripts/agents/test_agents_config.sh` — row-edit cases.
- `commands/agent.md` — new.
- `commands/cli_agent.md` — delete.

**Constraints from prior phases:** Phase 1 provides `agents_list_assignments`, `agents_set_assignment` (validate-then-awk-rewrite, reject invalid leaving file untouched), `agents_resolve_print`; the registry file is `config/agents.conf` with `[assignments]` + `[<function>.<family>]` + `[<family>.agents]` sections. New-code lookup/validation primitives are `_agents_registry_get` (prints value; returns 0 even on not-found — errexit-safe in `$(...)`), `_agents_registry_has_key` (0/1 presence for `if` conditions), and `_agents_validate_pair` — use these; never `_agents_config_get`, whose unescaped `^key=` regex mis-matches dotted keys. awk gotcha: `function` is a reserved awk word — Phase 1's rewrite passes `-v fn=`/`-v fam=`. Phase 2's sync already emits warnings that name `/agent` command forms — `re-point it: /agent <function>.<subtask> <agent>[:<effort>], or switch the family: /agent <function> <family>` (`sync_codex_catalog.sh` ~line 283) — so `agent_admin.sh`'s argument grammar must match those forms exactly; until this phase ships those warnings are dangling pointers, which is sequencing pressure to land it promptly.

**Acceptance gate:** `bash scripts/agents/agent_admin.sh status` renders every function with family and resolved rows; a `cli codex→claude→codex` round-trip leaves `config/agents.conf` byte-identical (diff clean — take the "before" snapshot *after* sourcing the resolver once, so a pending freshness sync can't rewrite `[codex.agents]` mid-round-trip and fake a diff); an invalid switch (`agent_admin.sh delegate nosuch`) and an invalid row edit (`agent_admin.sh cli.interactive nosuch:high`) both fail nonzero naming the problem with the file untouched; a valid row edit and its reversal rewrite only that row and preserve its trailing inline comment; `bash scripts/agents/test_agents_config.sh` passes.

### Phase 6 — cli aliases migration  · status: todo

#### Work Order

**Goal:** the `~/.zshrc` aliases (`review`, `commit_no`, `commit_yes`, `merge`, `code`) resolve through the registry; the private conf is gone.

**Spec:**

- `scripts/cli_agent/cli_agent.sh`: drop `agent-assignment.conf` and the local load/set logic (`cli_agent_load`, `cli_agent_set`). Map invocation → task: no args → `cli.interactive`; first arg (skill name) → `cli.<skill>` (`style_fix_review`, `commit_prep`, `merge_branch` — the existing alias→skill mapping stays); unknown skill errors with the known list. Resolve via `agents_resolve`, then exec as today: codex keeps `-c service_tier="fast"` (lines 112/123) and gets model/effort flags from `agents_codex_args`; non-interactive claude keeps the `-- "/$invocation"` form with `agents_claude_args`. Empty effort omits the flag (both families).
- `--status` prints the cli rows of `agents_list_assignments`; `--set` is removed — error message points at `/agent`.
- `~/.zshrc` needs no changes — aliases already pass the skill name. (Out-of-scope guardrail: do not touch the interactive `claude` alias.)
- Delete `scripts/cli_agent/agent-assignment.conf`. Remove the transitional-gap note added to `commands/agent.md` in Phase 5.

**Files:**
- `scripts/cli_agent/cli_agent.sh` — resolve via registry; drop conf handling.
- `scripts/cli_agent/agent-assignment.conf` — delete.
- `commands/agent.md` — drop the Phase 5 transitional note.

**Constraints from prior phases:** Phase 1 rows exist for `cli.style_fix_review`, `cli.commit_prep`, `cli.merge_branch`, `cli.interactive` in both family sets; `agents_codex_args`/`agents_claude_args` own the flag vocabulary; `/agent` (Phase 5) is the only assignment editor.

**Acceptance gate:** `bash scripts/cli_agent/cli_agent.sh --status` prints all four cli sub-tasks with family/agent/effort; an unknown skill arg exits nonzero listing known skills; `--set` exits nonzero pointing at `/agent`; `grep -rn "agent-assignment.conf" scripts/ commands/` returns nothing.

### Phase 7 — clean-fix stage resolution  · status: todo

#### Work Order

**Goal:** the three clean-fix stages resolve family/agent/effort from the registry; the stage conf keeps only `enabled=`. The launchd pipeline (fires every 10 minutes) must work at the end of this phase.

**Spec:**

- `scripts/clean-fix/agent-assignments.conf`: strip each stage section (`[style_eval]`, `[style_eval_review]`, `[style_fix]`) to only `enabled=`; agent/model/effort keys removed.
- `scripts/clean-fix/agent_assignments.sh`: `cf_load_stage_assignment` reads `enabled=` locally and fills the rest from `agents_resolve cleanfix.<stage>`. Variable meaning shifts: `STYLE_AGENT` = family (`codex`|`claude`), `STYLE_AGENT_MODEL` = agent — so the existing `case "$STYLE_AGENT" in claude|codex)` dispatch in the three style scripts keeps working unmodified in shape. Drop validators for the removed conf keys.
- `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh`: drop the `${STYLE_AGENT_EFFORT:-xhigh}` fallbacks — empty effort now means "omit the flag" (mirror cli_agent.sh's empty-effort handling in the codex branches). Codex-specific plumbing stays but must be reached only on the codex family path: the exec-marker transcript filtering (`style-eval-all.sh` ~line 110) and the codex usage/weekly-limit detection (`style-fix-worktrees.sh` ~line 434) already live in codex branches — verify, don't assume.
- These scripts are `#!/bin/bash` (bash 3.2) — no bash-4 features.
- Known transitional gap (accepted, same pattern as Phase 5's `/agent cli` note): until Phase 8 rewrites `commands/clean_fix.md`, its `<StyleAgentConfig>` subcommands (`/clean_fix agent eval claude` etc., ~lines 267-300) still instruct writing `agent=`/`model=`/`effort=` keys into `agent-assignments.conf` — keys this phase removes and `cf_load_stage_assignment` now ignores. Harmless (ignored keys, no breakage) and short-lived; Phase 8 replaces that doc surface.

**Files:**
- `scripts/clean-fix/agent-assignments.conf` — `enabled=` only.
- `scripts/clean-fix/agent_assignments.sh` — resolve via registry.
- `scripts/clean-fix/style-eval-all.sh` — effort-fallback removal; family-conditional check.
- `scripts/clean-fix/style-eval-review-all.sh` — same.
- `scripts/clean-fix/style-fix-worktrees.sh` — same + usage-limit branch check.

**Constraints from prior phases:** Phase 1 rows exist for `cleanfix.style_eval`, `cleanfix.style_eval_review`, `cleanfix.style_fix`, `cleanfix.report` in both family sets; `agents_resolve` errors loudly on bad rows — `cf_load_stage_assignment` should surface that error, not swallow it. `agent_exec` (Phase 3) is available but this phase only changes resolution; the stage scripts keep their own codex/claude launch code until a later cleanup if ever. `cf_load_stage_assignment` has callers outside this phase's file list: `clean-fix-usage.sh` calls it positionally at lines 95 and 438 (`cf_load_stage_assignment "$section" enabled agent model effort`), and `cf_print_stage_assignment`/`cf_print_agent_assignments` (`agent_assignments.sh` lines 115-129) back Phase 8's status view — keep the 5-arg out-var signature and the print helpers working; `clean-fix-usage.sh` itself isn't touched until Phase 8. Sourcing `agent_assignments.sh` (line 10) fires the freshness-gated catalog sync, which since Phase 2 can shell out to `claude --help` (~1-2s) and emit stale-row/alias `WARNING:` lines on stderr — these can appear in launchd run logs and are not stage failures.

**Acceptance gate:** `bash -n` passes on all five files; sourcing `agent_assignments.sh` and calling `cf_load_stage_assignment` for each of the three stages yields `STYLE_AGENT`/`STYLE_AGENT_MODEL`/`STYLE_AGENT_EFFORT` matching the *current* `[assignments] cleanfix` family and its `[cleanfix.<family>]` rows (the registry is live-editable via `/agent` — compare against the rows at run time, don't hard-code `gpt-5.6-sol:xhigh`); the resolver already passes under `/bin/bash` 3.2 (`env bash` resolves there on this machine — verified via `test_agent_exec.sh` after Phase 3), but this is its first execution in the launchd pipeline context — `/bin/bash -c 'source scripts/clean-fix/agent_assignments.sh && cf_load_stage_assignment style_eval'` must succeed before the launchd run is trusted; the next scheduled launchd style run completes — verify the newest clean-fix log shows a successful stage pass, not a resolution error.

### Phase 8 — clean-fix driver, usage, and report surfaces  · status: todo

#### Work Order

**Goal:** the clean-fix driver, usage screen, report parser, and docs are family-aware; the report render goes through the registry.

**Spec:**

- `scripts/clean-fix/clean-fix.sh` (driver): the `cf_load_stage_assignment` calls (lines 225-229) keep working per Phase 7; reword the agent log lines (~327+) from naming `$STYLE_EVAL_AGENT` alone to `family/agent` so the resolved model is visible. The report-render step (line 370) becomes `agent_exec cleanfix.report write …`. Today it builds the prompt inline (`"$(sed 's/\$ARGUMENTS/rebuild/g' … report-render.md)"`) and appends stderr to the main run log — there is no existing prompt file or dedicated log to map. Write the substituted prompt to a file under the run's tmp dir and give `agent_exec` a dedicated report log path (e.g. `report_render.log` beside the run log). Pass `$HOME/.claude` as `<working_dir>` (the current call runs in the driver's cwd; `agent_exec` needs a real directory for codex `-C` / the claude `cd`); `<output_file>` stays the existing `REPORT_FILE=/tmp/clean-fix-report.txt`; keep the `|| log "WARNING: failed to generate clean-fix report"` failure guard.
- `scripts/clean-fix/clean-fix-usage.sh`: `print_stage_json` (lines 90/95) and `print_stage_text` (lines 433-444) render agent/model/effort columns — update to family/agent/effort; effort may legitimately be empty (CLI default), keep the `<default>` placeholder. Help text (~44-45) for `/clean_fix agent …` updates to the new semantics below.
- `scripts/clean-fix/clean_fix_report_parse.py`: the codex usage-limit wording and reason codes ("codex hit its usage limit", `codex-usage-limit` at ~1166/1194/1836) generalize — name the resolved family in the strings or key the reason codes on it. basedpyright must stay at zero errors/warnings; no file-level ignores; no `Any`. Since Phase 2 the catalog sync can emit `WARNING:` lines (stale assigned rows, missing claude aliases) into launchd run logs — the current usage-limit regexes don't collide with them, and the generalized patterns must not start matching them either.
- `commands/clean_fix.md` (~lines 267-300): the scoped `agent|model|effort` subcommands (`/clean_fix agent eval claude`, `/clean_fix eval model opus`, …) lose their backing (per-stage keys are gone). `/clean_fix agent` becomes a status view (family + resolved rows via the existing `cf_print_agent_assignments` path) that points at `/agent cleanfix <family>` for switching and `/agent cleanfix.<stage> <agent>[:<effort>]` for row edits. The same `<StyleAgentConfig>` block also owns the `on|off` / `eval|review|fix on|off` enable/disable subcommands — those keep their backing (`enabled=` stays per Phase 7) and must be retained; only the agent/model/effort subcommands are replaced. This rewrite closes Phase 7's accepted transitional gap (the doc instructing writes of removed conf keys) — nothing extra to do beyond the rewrite itself.
- `scripts/clean-fix/README.md` (~line 18): rewrite the per-stage override schema rows — `agent-assignments.conf` now holds only `enabled=`; agent/model/effort live in `config/agents.conf` under `[cleanfix.<family>]`.

**Files:**
- `scripts/clean-fix/clean-fix.sh` — log wording + report render via `agent_exec`.
- `scripts/clean-fix/clean-fix-usage.sh` — columns, help text.
- `scripts/clean-fix/clean_fix_report_parse.py` — family-keyed limit strings/codes.
- `commands/clean_fix.md` — configure surface → status view pointing at `/agent`.
- `scripts/clean-fix/README.md` — override schema rows.

**Constraints from prior phases:** Phase 7 set `STYLE_AGENT`=family / `STYLE_AGENT_MODEL`=agent and stripped the stage conf to `enabled=`; Phase 3's `agent_exec` signature is `<task> <mode> <working_dir> <prompt_file> <output_file> <log_file>` with `AGENT_EXEC_DRY_RUN=1` for smoke tests; Phase 5's `/agent` is the switch/edit surface these docs point at. Dry-run output shapes (Phase 3): codex prints `codex exec … <prompt> > <log> 2>&1`; claude prints `cd <working_dir> && claude … -- <prompt> > <output> 2> <log>` — match smoke checks on substrings, not whole lines.

**Acceptance gate:** basedpyright reports zero errors and zero warnings on `clean_fix_report_parse.py`; `bash -n` passes on both shell files; `bash scripts/clean-fix/clean-fix-usage.sh` (status path) renders family/agent/effort with `<default>` where effort is empty; `AGENT_EXEC_DRY_RUN=1 bash scripts/agents/agent_exec.sh cleanfix.report write …` run directly with the same six arguments the driver passes prints a command matching the *current* `cleanfix.report` row (the render is gated behind the driver's activity grep — don't smoke through the whole driver); the next launchd run stays green (newest log clean).

### Phase 9 — ask_a_friend migration  · status: todo

#### Work Order

**Goal:** both ask_a_friend launchers resolve via the registry under family-neutral names.

**Spec:**

- `scripts/ask_a_friend/ask_a_friend.sh` (the per-round consultation, called at `commands/ask_a_friend.md` line 88): today reads codex model/effort from the old registry defaults; it becomes status/provenance handling plus `agent_exec ask_a_friend.consultation write …`. It runs write mode (`--full-auto`) deliberately — `--sandbox read-only` panics codex's system-configuration crate on macOS — keep the explanatory comment.
- `git mv scripts/ask_a_friend/codex_implement.sh scripts/ask_a_friend/implement.sh`; body collapses to status/provenance plus `agent_exec ask_a_friend.implementation write …`. Log naming goes agent-neutral (`impl_codex.log` → `impl_agent.log`, `codex.log` → `agent.log`); provenance records `task=/family=/agent=/effort=`.
- `commands/ask_a_friend.md`: both call sites (lines 88 and 285) get the new script names/paths; the resolution blurb at lines 15-16 ("resolve Codex model/effort defaults through `agents.conf`") rewrites to name the registry rows (`[ask_a_friend.<family>]`, switched via `/agent`); the `impl_codex.log`/`codex.log` filename references (lines 93 and 291) update.
- `.claude/settings.local.json`: the two permission entries naming `ask_a_friend/codex_implement.sh` (lines 20 and 73) update to `ask_a_friend/implement.sh`. `Bash(codex exec:*)` (line 26) and `Bash(pkill -f 'claude --print')` (line 33) still match — leave them.

**Files:**
- `scripts/ask_a_friend/ask_a_friend.sh` — `agent_exec` consultation.
- `scripts/ask_a_friend/implement.sh` — renamed + `agent_exec` implementation.
- `commands/ask_a_friend.md` — call sites, blurb, log names.
- `.claude/settings.local.json` — two renamed permission entries.

**Constraints from prior phases:** Phase 1 rows exist for `ask_a_friend.consultation` and `ask_a_friend.implementation` in both family sets; Phase 3's `agent_exec` write mode emits codex `--full-auto` / claude `--dangerously-skip-permissions`, which matches this consumer's requirements; delegate (Phase 4) set the provenance/log-naming precedent (`task=/family=/agent=/effort=`, `*_agent.log`). Dry-run output shapes (Phase 3): every argv token is `printf '%q'`-quoted; codex prints `codex exec … <prompt> > <log> 2>&1` (no prefix), claude prints `cd <working_dir> && claude … -- <prompt> > <output> 2> <log>`; the codex effort token renders as `model_reasoning_effort=\"high\"` — match gate checks on substrings, never whole lines. The wrappers must not redirect the `agent_exec` call's stdout/stderr to files — `agent_exec` owns all redirection, and dry-run output must reach the wrapper's stdout.

**Acceptance gate:** `AGENT_EXEC_DRY_RUN=1` smoke of both launchers prints codex `--full-auto` commands with the `[ask_a_friend.codex]` model/effort (assumes the shipped `[assignments] ask_a_friend=codex`; if the family was switched via `/agent`, expect the claude shape instead — `cd` prefix, `--dangerously-skip-permissions`); `grep -rn "codex_implement\|impl_codex" scripts/ask_a_friend/ commands/ask_a_friend.md .claude/settings.local.json` returns nothing.

### Phase 10 — review commands migration  · status: todo

#### Work Order

**Goal:** `/team_review`, `/api_review`, and `/module_review` launch their review teams through registry-resolved external CLI agents, so a whole review team switches family with one `[assignments]` row.

**Spec:**

Today all three launch in-session Agent-tool subagents (`subagent_type: Explore`). Replace those launches:

- `commands/team_review.md` `<LaunchExpertTeam>` (~86-117): "Launch 3-5 agents in parallel using the Agent tool" becomes 3-5 parallel backgrounded `agent_exec team_review.expert readonly …` invocations, one per dimension lens, each with its own prompt file and output file under a session dir.
- `commands/api_review.md`: the 5 parallel reviewers (~line 63) → `agent_exec api_review.reviewer readonly …`; the 2 adversarial stress-testers (~line 116) → `agent_exec api_review.adversary readonly …`.
- `commands/module_review.md`: pass 1 (~79) and pass 2 over-large-files (~192) agents → `agent_exec module_review.reviewer readonly …`; pass 3 doc-vs-code validation (~226) → `agent_exec module_review.validation readonly …`.

Shared mechanics all three docs must specify:

- External CLI agents inherit no session context. Each prompt file must be self-contained: the verbatim charter preamble (agents still Read `~/rust/nate_style/review-charter.md` themselves — both CLIs can read files), the review topic/intent/posture, explicit file paths, the dimension/lens, and the finding schema.
- Reviewers run `readonly` mode (codex `--sandbox read-only`, claude `--permission-mode plan`) — same as delegate review.
- The command backgrounds all `agent_exec` calls in one turn (each via Bash `run_in_background: true` with `dangerouslyDisableSandbox: true`) and yields; task-notifications signal completion. Synthesis, deduplication, and the decision walk stay in-session as today.
- Session dirs live under the scratchpad (per-agent `prompt_N.md`/`findings_N.txt`/`agent_N.log` plus provenance files), same layout as delegate sessions.
- Warm the freshness gate once before backgrounding (e.g. run `bash scripts/agents/agent_admin.sh status`): every backgrounded `agent_exec` sources `agents_config.sh`, and a stale gate would make 5-7 parallel launches each fire the catalog sync (concurrent conf rewrites — covered by the last-writer-wins invariant — plus a ~1-2s `claude --help` shell-out apiece).
- Everything else in the three docs (dimension menus, finding schema, synthesis, firewall/posture logic, decision walks) is untouched.

**Files:**
- `commands/team_review.md` — `<LaunchExpertTeam>` launch step.
- `commands/api_review.md` — reviewer + adversary launch steps.
- `commands/module_review.md` — three pass launch steps.

**Constraints from prior phases:** Phase 1 rows exist for `team_review.expert`, `api_review.reviewer`, `api_review.adversary`, `module_review.reviewer`, `module_review.validation` in both family sets; Phase 3's `agent_exec` signature and readonly-mode flag mapping are the contract these docs cite — reference the script path `scripts/agents/agent_exec.sh`, don't restate its internals.

**Acceptance gate:** each doc's launch step invokes `agent_exec` with the correct task name and `readonly` mode; `grep -n "subagent_type\|Explore agents\|using the Agent tool" commands/team_review.md commands/api_review.md commands/module_review.md` shows no remaining Agent-tool launch for the review-team members (only `team_review.md` contains the literal `subagent_type` today — `api_review.md` and `module_review.md` phrase launches as "Launch N Explore agents in parallel", so the bare `subagent_type` grep is vacuous for them); each doc enumerates the self-contained-prompt requirements (charter preamble, topic/intent/posture, file paths, lens, finding schema).

### Phase 11 — legacy strip + docs  · status: todo

#### Work Order

**Goal:** the legacy registry surface is gone; docs describe only the new model.

**Spec:**

- `config/agents.conf`: delete the legacy sections — `[codex]` (`model=`), `[claude]` (`model=`), `[codex.models]`, `[claude.models]`, and any `[codex.efforts]`/`[claude.efforts]` remnants. The file then contains only `[assignments]`, the `[<function>.<family>]` sets, and the two `[<family>.agents]` catalogs. Update the header comment (resolver path, sync note, consumers list).
- `scripts/agents/agents_config.sh`: remove the legacy `agents_config_*` API that no longer has callers (`agents_config_model`, `agents_config_effort`, `agents_config_allowed_*`, `agents_config_validate_*`, `agents_config_apply_defaults`) and the private helpers that go dead with them (`_agents_config_get` — the dotted-key-regex footgun — `_agents_config_value_allowed`, `_agents_config_values_inline`) — verify each with grep before deleting. Keep `agents_config_trim`, `_agents_config_has_section`, and `_agents_config_section_values` (the new API uses both), the freshness-triggered sync, and the Phase 1+ API.
- `config/README.md`: rewrite the `## agents.conf` block for the new schema (three layers, `/agent` as the editor, sync behavior, claude catalog hand-maintained with alias-staleness warn).
- `scripts/agents/test_sync_codex_catalog.sh`: its fixtures (heredocs ~lines 58 and 117) deliberately carry `[codex]`/`[codex.models]`/`[codex.efforts]` sections to pin "legacy sections left byte-identical when present". Rename those fixture sections to neutral names (e.g. `[legacy.unmanaged]` + plain rows) — the pass-through behavior being pinned is generic (the sync rewrites only `[codex.agents]`), so the test stays meaningful and the verification grep below can come back clean.
- Verify `config/orphans_expected.json` needs nothing (it is empty).

**Files:**
- `config/agents.conf` — legacy sections removed.
- `scripts/agents/agents_config.sh` — legacy functions removed.
- `config/README.md` — agents.conf description rewritten.
- `scripts/agents/test_sync_codex_catalog.sh` — fixture legacy-section names neutralized.

**Constraints from prior phases:** every consumer migrated in Phases 4-10; the only remaining readers of the legacy sections/functions should be the legacy functions themselves — `grep -rn "agents_config_model\|agents_config_effort\|agents_config_allowed\|agents_config_validate\|agents_config_apply_defaults\|codex.models\|claude.models\|codex.efforts\|claude.efforts" scripts/ commands/ config/` must come back clean (excluding this plan doc) before deleting — the wider pattern covers the allowed/validate/apply_defaults helpers and the `[*.efforts]` sections, whose last callers (`cli_agent.sh` ~lines 53-56/75-77, `agent_assignments.sh` ~lines 45-57) are removed in Phases 6/7. Phase 2 left the legacy conf sections static specifically so this phase could remove them wholesale; the Phase 2 sync neither reads nor writes any legacy section, so the wholesale strip is safe.

**Acceptance gate:** `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass; the grep above returns nothing live; manual smoke: `/agent status` renders, one `codex`→`claude`→`codex` round-trip on `cli` leaves the conf byte-identical, and a delegate dry run (`AGENT_EXEC_DRY_RUN=1` through `implement.sh`) resolves correctly.
