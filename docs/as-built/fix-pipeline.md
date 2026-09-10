# The fix pipeline — unattended Rust style evaluation, review, and repair

## What it is

`scripts/fix/` is an unattended, timer-driven pipeline that evaluates opt-in Rust projects under `~/rust/` against the shared style guide, reviews the resulting findings with a second agent, and then applies the fixes in a throwaway git worktree for the user to review at leisure. The problem it solves: style debt accumulates faster than anyone will pay it down by hand, and a style pass done interactively costs a full attention block per project. Here the work happens on a 10-minute schedule with no human in the loop, each project's state persists across runs, and the user's only interaction is reviewing a finished `_style_fix` worktree and merging it. The pipeline is style-only — it evaluates, reviews, and fixes, and does nothing else.

## How it works

`scripts/fix/README.md` is the companion file table — one row per file, how to regenerate the flow diagram, and the pending-JSON schema. This doc covers the rules, the traps, and the reasoning behind them.

### Trigger and concurrency

One job, declared once for both machines in `/etc/nixos/modules/common/style-fix.nix` as a `nate.jobs` entry with `intervalSeconds = 600`. `nate.jobs` renders it per platform:

| | Linux (natedev) | macOS |
|---|---|---|
| unit | `style-fix.timer` → `style-fix.service` (user units) | `org.nixos.style-fix` launchd user agent |
| interval | `OnUnitInactiveSec=600s` — 600s after the previous run **finished** | `StartInterval 600` — every 600s regardless |
| stdout/stderr | the journal (`journalctl --user -u style-fix`) | `~/Library/Logs/nate-jobs/style-fix.log` |
| extras | `TimeoutStartSec=4h`, from `modules/linux/style-fix.nix` | — |

There is **no idle gate** on either: a firing happens whether or not the machine is in use. The interval semantics differ on purpose — see the trigger's own header comment. Because launchd fires into a run that is still going and systemd does not, the `pgrep` guard in `fix-trigger.sh` is what absorbs the Mac's extra firings; on both platforms it also catches a `fix.sh` started by hand.

The job runs `scripts/fix/fix-trigger.sh`, which is a concurrency guard plus an `exec`. It holds one variable, `FIX_ORCHESTRATOR_PATH="$HOME/.claude/scripts/fix/fix.sh"`, runs `pgrep -f` against that exact path, exits 0 if a run is already in flight, and otherwise `exec`s the orchestrator. The guard is accurate because `fix.sh` runs synchronously start to finish — `style-fix-worktrees.sh` waits on its backgrounded agents before returning — so the orchestrator's presence in the process table means "a run is still going."

`scripts/fix/setup.sh` and `scripts/fix/com.natemccoy.style-fix.plist` are **retired**, superseded by the nix declaration above. The plist is kept as the rollback target snapshotted into the Mac's `~/nixos-recovery/`; `setup.sh` is a stub that refuses, because bootstrapping the old `com.natemccoy.style-fix` label would put a second ten-minute agent beside the nix-managed `org.nixos.style-fix` and both would fire.

### Orchestration — `fix.sh`

`fix.sh` takes at most one argument — an optional project name filtering all three stages to one target. There is no scope word and no mode word; any leading token is a project filter, and a second token is a usage error.

What varies is not the argument but the environment. `FIX_SCHEDULED=1`, exported by `fix-trigger.sh` and by nothing else, is what makes a run consult the three `enabled=` switches; `stage_runs()` reads `[[ "$SCHEDULED" != "1" || "$1" == "true" ]]`, so an interactive run passes the gate unconditionally. `fix.sh` re-exports the value so the stage scripts apply the same rule when the orchestrator calls them, and they default it to `0` so a direct call runs too.

Logging: `LOG_DIR` is `~/.local/logs/fix`, each run writes `fix-YYYYMMDD-HHMMSS.log`, and `~/.local/logs/fix.log` is re-pointed as a symlink to the newest run so older tooling keeps working. This symlink, not the platform's job log, is the portable place to tail a run. Retention runs at the top of every pass: `fix-*.log` and migrated `clean-fix-*.log` are pruned after `RUN_LOG_RETENTION_MINUTES=1440` (about a day) in two explicit `find` branches, and `style-fix-manual-*.log` after `MANUAL_LOG_RETENTION_DAYS=7`. The `log()` helper prefixes every line with `%Y-%m-%d %H:%M:%S`.

Configuration read: `fix.sh` parses `fix.conf` itself for `[active_checkout]`, filling the parallel arrays `cf_ac_keys` / `cf_ac_vals`, and hard-errors if `[style_eval]` or `[style_fix]` still carries a stale `mode=`, `enabled=`, `agent=`, `model=`, or `effort=` row — those moved to `agent-assignments.conf` and `config/agents.conf`. `project_key()`, `checkout_root()`, and `project_filter_key()` normalize a user-supplied filter (entry, checkout path, checkout root, or bare name) down to the project's identity key.

Stage resolution: three calls to `cf_load_stage_assignment` for `style_eval`, `style_eval_review`, and `style_fix`.

Run body, in order:

1. Start banner — one of `=== Starting fix (project: X) ===`, `=== Starting fix (scheduled) ===`, or `=== Starting fix ===`. Every non-scheduled pass additionally prints a stage/agent:effort summary table.
2. `backpopulate_settings.py --apply` — unconditional, before every pass. This walks **every** non-dot directory under `~/rust/`, independent of the allowlist, back-populating canonical `settings.local.json` permissions that the style-fix agents depend on.
3. Three independent stage gates. Each checks `<STAGE>_ENABLED == "true" || RUN_ONCE_REQUESTED == "true"`; when off it logs its own line (`SKIP: style eval disabled in agent-assignments.conf`, and the review and fix equivalents) and **falls through to the next stage** rather than ending the run.
4. Completion banner — `=== Fix complete (Xm Ys) ===`.
5. Report activity gate — `grep -qE '(^|[[:space:]])(OK|FAILED|ERROR|TIMEOUT|RECOVERED|Launched):'` over the run log. On a hit, `report-render.md` is templated (`$ARGUMENTS` → `rebuild`) into a scratch prompt and handed to `scripts/agents/agent_exec.sh fix.report write`, producing `/tmp/fix-report.txt`. On a miss, the run logs `Report skipped (no per-project activity this run).` and renders nothing — the pipeline fires 144 times a day and an agent call per idle cycle is pure cost.

### Configuration — two files, two owners

**`scripts/fix/fix.conf`** owns projects and tunables. Five sections:

| Section | Contents |
| --- | --- |
| `[style_eval]` | `max_new_findings`, `eval_unit_quota`, `eval_ttl_days` |
| `[style_fix]` | `agent_timeout_secs`, `post_summary_grace_secs`, `heartbeat_interval_secs` |
| `[project_env]` | per-project env applied to the style-fix agent and its build gate (e.g. `RUSTC_BOOTSTRAP=1` for cargo-mend); its one consumer is `style-fix-worktrees.sh` |
| `[projects]` | the opt-in allowlist; each line is `<dir>` (whole crate or workspace) or `<dir>/<subpath>` (one workspace member) |
| `[active_checkout]` | optional `<projects-entry> = <checkout-path>` redirects |

`[projects]` supplies each project's identity and history key — the last path segment of the entry. `[active_checkout]` redirects an entry's eval/fix work at a worktree while identity and history stay with the entry, so a project being worked in a `*_bevy_update` checkout keeps one continuous history.

**`scripts/fix/agent-assignments.conf`** owns stage enablement and nothing else: `[style_eval]`, `[style_eval_review]`, `[style_fix]`, each carrying `enabled=` alone.

**`config/agents.conf`** (the shared agent registry, outside this pipeline) owns family, agent, and effort. The `fix` function has four sub-tasks — `style_eval`, `style_eval_review`, `style_fix`, `report` — under `[fix.codex]` and `[fix.claude]`, selected by `fix=<family>` in `[assignments]`.

`scripts/fix/agent_assignments.sh` is the Bash bridge between the three. It sets `FIX_PIPELINE_DIR` and `FIX_AGENT_ASSIGNMENTS_FILE`, sources `scripts/agents/agents_config.sh`, and exposes:

- `cf_trim` — thin wrapper over `agents_config_trim`
- `cf_resolve_checkout <entry>` — `[active_checkout]` override or the entry itself, reading the caller's `cf_ac_keys`/`cf_ac_vals` arrays
- `cf_validate_bool` — rejects anything but `true`/`false`
- `cf_load_stage_enabled <section> <var>` — reads one `enabled=` switch
- `cf_load_stage_assignment <section> <enabled> <family> <agent> <effort>` — calls the above, then `agents_resolve "fix.$want_section"`
- `cf_print_stage_assignment` / `cf_print_agent_assignments` — the display path, also what running the file directly emits

### The three stages

**Stage 1 — evaluation (`style-eval-all.sh`).** Runs `/style_eval` against every `[projects]` entry in parallel, using the `[style_eval]` assignment. Writes pending evaluation markdown into `~/rust/nate_style/.history/.pending/<project>.json`. Skips any project that already has pending findings or a real `_style_fix` worktree, so pending JSON is never replaced while fixes await review. `candidate_generators.py` supplies deterministic candidate enumeration for guidelines whose frontmatter names a generator kind (regex / TOML / Rust-source parse) — `next-unit` hands the agent a closed list of sites and `record-unit` refuses a record that leaves any candidate undispositioned. `style-eval-heartbeat.sh` writes liveness records into the same pending JSON every 60s. Each per-agent wait is `wait_or_timeout`, which kills the agent's whole process tree after `agent_timeout_secs`.

**Stage 2 — evaluation review (`style-eval-review-all.sh`).** Re-reads each project's pending markdown with the `fix.style_eval_review` agent and the prompt at `style-eval-review-prompt.md` (a plain prompt file, not a slash command), keeping, improving, amending, or removing each finding, then saves the reviewed markdown back into pending JSON. Eligibility: pending JSON has markdown, the markdown has at least one `### N.` numbered finding, and it does not yet contain a `## Review Log` section — so the stage is idempotent.

**Stage 3 — style fix (`style-fix-worktrees.sh`).** For each project with pending findings: create a `<project>_style_fix` git worktree, write the `.fix-project` identity marker into it (and add that filename to the repo's `info/exclude`), export the pending markdown to a scratch file under `/tmp/claude`, then run the configured agent **twice**. Pass 1 applies the fixes and runs cargo mend, clippy, tests, and a style review, ending with a `## Fix Summary`. Pass 2 is the same agent verifying the applied fix against that summary, correcting mistakes and appending `## Fix Verification`. A `cargo check` build gate covers both passes; the finished summary is saved back into pending JSON, and `EVALUATION.md` is deliberately kept out of the worktree. The script installs an EXIT trap that always emits `[progress <proj>] phase=launcher-exit code=N` so a monitor tailing the log can self-terminate.

After the pipeline, the human path is manual: `/style_fix_review` → `/merge_branch` → `/worktree_delete`.

### Report parsing and monitoring — `fix_report_parse.py`

One module reads run logs and emits line-oriented `key=value` facts; every consumer (slash command, agent) does its own rendering. Modes:

| Mode | Meaning |
| --- | --- |
| default | current status for every keyed `[projects]` target |
| `--latest-log` | full report parse of the newest fix log |
| `--list` | enumerate logs in `~/.local/logs/fix/` with summaries |
| `--phase-detect <log>` | the currently-running phase, for `/fix monitor` |
| `--filter-regex` | print `MONITOR_FILTER_REGEX`, the single source for the live-monitor filter |

`PHASES` is `("eval", "review", "fix", "verify")` — the four columns of the report matrix. `PhaseStats` carries `ok`, `fail`, `skip`, `running`, `footer_ok`, `footer_fail`, `footer_total`, `present`.

Completion is recognized through exactly one helper. `COMPLETE_RE` matches `=== Fix complete (…) ===`; `HISTORICAL_COMPLETE_RE` matches the two retired wordings, `=== Clean-fix complete (…) ===` and `=== Clean-fix Rust clean + rebuild complete (…) ===`; `match_completion_banner()` tries both and is the only entry point, called from `parse_log` and `detect_current_phase`. A future rename adds a generation to `HISTORICAL_COMPLETE_RE` and edits nothing else.

`detect_current_phase()` returns one of `done`, `style-fix`, `style-eval`, `style-eval-review`, `unknown` by walking the last 200 lines backwards. This is a **different vocabulary from `PHASES`** on purpose: `PHASES` names report-table columns, `detect_current_phase` names live pipeline positions, and `verify` never appears in the latter.

Project identity in the parser comes from the `.fix-project` marker inside a candidate worktree. Skip reasons are bucketed into `ALWAYS_EXCLUDED_REASONS` (permanent — not in the allowlist, no `Cargo.toml`, is itself a style-fix worktree) and `FRAMEWORK_FILTER_REASONS` (transient — a leftover `_style_fix` directory the user can clean up). `ORPHAN_STUB_PREFIX` rows emit a `NOTE` carrying the full path so it surfaces under "Heads up" in the report. A launched eval agent is treated as alive while its heartbeat is newer than `HEARTBEAT_FRESH_SECS = 150` (2.5× the 60s cadence).

`report-render.md` is the prompt that turns parser output into `/tmp/fix-report.txt`. Its row contract is:

```
ROW <project>  eval=<cell> review=<cell> fix=<cell> verify=<cell> reason="<short reason | ->" [phase_now="<live phase>"]
```

An empty `/fix report` argument means current keyed-project state; newest-log mode requires the explicit `latest` or `newest`.

### Command surface

`commands/fix.md` defines `/fix`, dispatching on the first token: `run`, `add`, `rename`, `monitor`, `report`/`list`, `eval`/`review`/`fix`/`agent`/`on`/`off`, `skip`. With no subcommand it runs `scripts/fix/fix-usage.sh` and relays stdout verbatim — the script owns section order, column widths, wrapping, and formatting, and `--json` exposes the same usage, agent, and project data for tooling. The project table has one status column, `Style`.

### Supporting helpers

| File | Role |
| --- | --- |
| `project_add.py` | Adds a project to `[projects]` only, returning one result line. Accepts checkout names, paths under `~/rust`, absolute paths, and `Cargo.toml` paths; workspace members are written workspace-relative so the identity key stays the member directory name. |
| `project_rename.py` | Renames a project key after a path change. Migrates `[projects]`, `[active_checkout]`, and `[project_env]` entries plus history JSONL, pending JSON/lock, failure logs, and `.fix-project` markers (glob `*_style_fix/.fix-project`). Refuses collisions rather than merging histories. |
| `phase_skip.py` | Temporarily removes a target from the pass by commenting its `[projects]` line, tagged `#FIX_SKIP#` so `enable`/`enable-all` reverse only temp skips and never plain doc comments. Actions are a closed `PhaseSkipAction = Literal["skip","enable","enable-all","status"]`. Accepts both `<action> [project ...]` and a leading `style` token; both print identical output, and the short form is what the usage screen and command doc advertise. |
| `style_history.py` | Durable per-project style history: pending JSON lifecycle (`start-run`, `next-unit`, `record-unit`, `save-evaluation`, `export-evaluation`, `finalize-*`, `recover-evaluation`), history JSONL append, `PROJECT_MARKER = ".fix-project"`. |
| `style_admin.py`, `style_report.py` | Deterministic guideline admin operations and history-derived reporting. |
| `backpopulate_settings.py` | Canonical `settings.local.json` permission back-population; dry-run unless `--apply`. |
| `style-fix-manual.sh` | Manual launcher for `style-fix-worktrees.sh` that lands its log in `~/.local/logs/fix/` so `/fix report` picks it up. `--foreground` gives a real completion event. |
| `style-fix-monitor.py` | Streams orchestrator + agent log lines and exits on `launcher-exit`. Replaces a `tail -F \| awk` pipeline that relied on `pkill -f` to stop itself; reading the files in Python needs no process signalling and is one code path for both machines. |
| `rg-shim.sh` | Retired `rg` timeout shim, kept as incident context only; not on PATH. |
| `scripts/make_a_worktree/retarget_fix.py` | Redirect-only helper: `detect` / `apply` / `revert` against `[active_checkout]`. With `--commit` it commits `fix.conf` alone. It never touches `[projects]`, so the history key is always preserved. |
| `scripts/worktree_delete/perform_deletion.sh` | Reads `.fix-project` to recover a worktree's identity key before deleting it. |
| `scripts/new_rust_project/rust_generate.sh` | Enrolls a new crate with a single `ensure('projects', …)` call. |

### The flow diagram

`fix-style-flow.dot` (graph id `fix_style`) is the hand-maintained source; `fix-style-flow.svg` is generated and never hand-edited. Current structure: four clusters (`cluster_eval`, `cluster_review`, `cluster_fix`, `cluster_manual`), 38 nodes, 47 edges, and exactly two terminals (`report_idle`, `delete_wt`).

It draws three independent enablement diamonds (`eval_enabled`, `review_enabled`, `fix_enabled`), each labelled "(scheduled runs only)" and each falling through to the next stage on `no` rather than ending the run; a per-project `ttl_gate` whose two outcome edges route to distinct per-project terminals; and an `activity_gate` ahead of the report that splits into `report` and `report_idle`. Four labels are held in sync with the scripts by hand: the trigger interval (from `intervalSeconds` in `modules/common/style-fix.nix`), project selection (the `[projects]` allowlist, `[active_checkout]` resolution, missing path / `Cargo.toml` skips), the findings cap (`[style_eval] max_new_findings`), and `prechecks` ("source tree").

`render-flow.py` parses cluster membership, labels, and colors out of the `.dot`, runs `neato -n2`, injects dashed cluster borders, aligns the tops of the three phase clusters (`PHASE_CLUSTER_IDS`), and rewrites the SVG `viewBox`.

## Invariants

**Bash 3.2.** Every `.sh` in this tree is `#!/usr/bin/env bash` and avoids associative arrays, `${var,,}`, and other bash-4+ constructs. The path that forced this — the retired plist's `/bin/bash <script>`, macOS system bash 3.2 — is gone, and `env bash` now resolves to nix's bash 5.x on both machines. Keep the constraint anyway: it costs nothing, and `/bin/bash script.sh` still works on the Mac. Do not "modernize" a 3.2 workaround; several carry comments explaining a real misparse.

**basedpyright at zero errors and zero warnings.** `pyrightconfig.json` carries an execution environment rooted at `scripts/fix`. Never use a file-level type ignore (`# pyright: reportAny=false`). Avoid `Any`; annotate signatures; use `TypedDict` for known-key dicts. A line-level `# pyright: ignore[...]` is the last resort.

**The pipeline is never left broken.** It runs unattended every ten minutes, so `fix.sh`, `fix-trigger.sh`, `agent_assignments.sh`, the three stage scripts, and `fix_report_parse.py` must be working at every commit, not just at the end of a change.

**Opt-in allowlist, no deny list, and the allowlist's exact scope.** Nothing is evaluated, reviewed, or fixed unless `[projects]` lists it. There is no deny list — the model is opt-in and every comment must preserve that framing. The scope claim must stay precise: `backpopulate_settings.py --apply` runs repository-wide over every non-dot directory under `~/rust/` regardless of the allowlist, so "the pipeline never touches an unlisted directory" is false; the accurate statement is that an unlisted directory is never evaluated, reviewed, or fixed.

**Terminology.** allowlist/denylist, never whitelist/blacklist.

**Durable style state is out of scope.** `~/rust/nate_style/.history/` — pending JSON, per-project history JSONL, `.failures/` — is not named after this pipeline and is not committed. It is local operational state; do not rename or migrate it as part of a pipeline change.

**One banner recognizer.** All completion-banner matching goes through `match_completion_banner()`. Adding a generation means adding an alternative to `HISTORICAL_COMPLETE_RE`, never replacing one.

**The monitor filter's `=== ` alternative stays `(^|[[:space:]])=== `.** Re-narrowing it to `^=== ` breaks the live monitor, because `log()` prefixes a timestamp to every line.

**Log enumeration stays era-agnostic.** The parser's `LOG_DIR.glob("*.log")` calls must not be narrowed to a name-prefixed pattern, or migrated log history becomes unreachable.

**The generated SVG is generated.** Edit the `.dot` and re-run `render-flow.py`; never hand-edit `fix-style-flow.svg`.

**`PASS_LABEL = "style"`** in `phase_skip.py` feeds six user-visible messages. It names the style *pass*, not a product name, and its message text stays as written.

### Sanctioned compatibility survivals

Five places deliberately keep a `clean`/`clean-fix` spelling. A name sweep must leave all five alone.

| Location | Why it survives |
| --- | --- |
| `scripts/fix/fix_report_parse.py:99-101` — `HISTORICAL_COMPLETE_RE` | Recognizes the two retired completion wordings. Without it every retained pre-rename log parses as an unfinished run. Do not assert these literals are absent from parser source — the historical set is exactly where they belong. |
| `scripts/fix/fix.sh:51-52` — the `clean-fix-*.log` retention branch | Migrated logs carry the old filename prefix; without this branch they are never pruned. |
| `commands/fix.md:168` — `<DetectLog/>`'s `/tmp/claude/clean-fix-*.log` candidate | Interactive logs written before the rename would otherwise be undiscoverable by `/fix monitor`. |
| `scripts/fix/tests/fixtures/six-phase-run.log` | A hand-built fixture modeling a full pre-change six-phase run, deliberately containing `CLEAN:`, `BUILD:`, `MEND:`, `DONE:`, and every `WARMUP` verb. It is data, not code; every vocabulary sweep must exclude it. |
| `scripts/fix/tests/test_report_parse_phases.py:60-61` | The assertions quoting the two historical banners, which is what keeps the compatibility path tested. |

## Calibration / gotchas

**`config/agents.conf` passes through a git clean filter.** `.gitattributes` maps it to `filter=claude-agents-conf`, whose clean side is `scripts/agents/clean_agents_conf.sh`. The filter pins staged content to whatever is already in the index, because the file churns constantly (`/agent` rewrites `model:effort` rows, `sync_codex_catalog.sh` regenerates `[codex.agents]`) and none of that deserves a commit. Consequences:

- An edit to that file commits **empty** unless you use the documented escape: `touch config/agents.conf && AGENTS_CONF_COMMIT=1 git add config/agents.conf`.
- The `touch` matters. Once git has refreshed its stat cache for a path it considers up to date, it compares nothing and runs no filter, so the override reads as inert until the mtime moves again.
- The working tree lies about this file. To verify a committed change, read `git show HEAD:config/agents.conf`, not `git status` or `git diff`.
- The filter cannot strip the volatile rows the way the `settings.json` filter does: `agents_config.sh` validates every agent and effort and hard-errors on an empty one, so a stripped file would break on a fresh clone and take delegate, this pipeline, and the CLI aliases with it.

**`style-fix-worktrees.sh` builds its agent prompt in an unquoted heredoc.** `cat > "$prompt_file" <<PROMPT_EOF` at `:679`, closing at `:873`. The delimiter is unquoted on purpose so `$var` interpolation works, which means **a backtick in that body is evaluated at runtime instead of printed literally** — every backtick meant to reach the agent must be written `\``. The in-file comment at `:673-676` also explains why the prompt is written to a file and slurped back rather than captured with `$(cat <<EOF … EOF)`: macOS bash 3.2 misparses backticks and apostrophes in a heredoc body inside command substitution and emits spurious "command not found" errors at runtime. One line in the current body (`:835`) violates the backtick rule; it is a known, unrepaired defect.

**The flow diagram's layout is absolute.** Every node carries `pos="X,Y!"` and `render-flow.py` runs `neato -n2`, which honors those coordinates and computes nothing. Adding a node means computing its coordinate by hand and re-deriving the cluster's x-range; the procedure is the comment block at the top of the `.dot`, and the column-centre comment at `:37-42` records the numbers the current layout was built from. Two further traps:

- `render-flow.py` prints `Parsed N clusters` and then silently drops any cluster whose member nodes produce no bounding box. A successful run is not proof the diagram is whole. The structural audit is: every node reachable, no edge naming an undeclared node, exactly two terminals.
- Re-rendering an unchanged source is byte-stable, so `cmp` against the checked-in SVG is the cheapest proof that an edit changed only what it meant to.
- Automated Safari inspection is unavailable here — macOS denies the UI-capture permission. Rasterize in memory or verify structurally.

**Changing the schedule is a nix edit, not a script edit.** The interval lives in `/etc/nixos/modules/common/style-fix.nix`; edit it there and have the user run `rebuild`. Nothing under `scripts/fix/` installs or reloads a timer any more.

**Timestamps defeat `^` anchors.** `log()` prefixes `%Y-%m-%d %H:%M:%S` to every orchestrator line, so any `^`-anchored filter or matcher misses the completion banner. This is why `MONITOR_FILTER_REGEX` uses `(^|[[:space:]])=== `.

**An unknown first argument used to be a silent no-op.** `fix.sh <anything>` is a project filter, and a stale invocation like `fix.sh clean` or `fix.sh style` matched no project. The three stage scripts now exit 1 with `ERROR: no [projects] entry named '<name>'` instead, so a typo and an idle allowlist no longer look alike. `No projects to evaluate.` at exit 0 now means what it says.

**The job will run against edited code.** It fires every 600 seconds with no idle gate, so a change that moves the pipeline's inputs or its log directory must quiesce the job first, and mid-change firings against a half-edited tree are normal. Fresh logs appearing in `~/.local/logs/fix/` roughly ten minutes apart is healthy, not a runaway.

**Repository-wide sweeps must use `git grep`.** A raw `grep -r` inside `~/.claude` hits gitignored runtime state — `paste-cache/`, `history.jsonl`, `file-history/`, `__pycache__/*.pyc` — which quotes historical text permanently and can never reach zero matches. Case-sensitive greps also miss CamelCased workflow tags; the two redirect tags are now `<OfferFixPipelineRedirect/>` (`commands/make_a_worktree.md`) and `<RevertFixPipelineRedirect/>` (`commands/worktree_delete.md`).

**A stale report file satisfies a bare existence check.** `/tmp/fix-report.txt` persists between runs. Any test of the report branch must compare modification time, not existence.

**Positional phase detection only strictly holds for review/fix/verify.** `eval_start` is a constant `0` — the whole log preamble falls inside the eval slice — because `parse_eval_phase` keys on its own line vocabulary rather than slice position. `eval_present` carries the "was there an eval section at all" question. Widening the eval slice is therefore invisible in output, which also means a change there cannot be validated by reading the code; use differential testing against the fixture.

**`PHASES` and `detect_current_phase()` are two different vocabularies.** `PHASES` names report-table columns (`eval`, `review`, `fix`, `verify`); `detect_current_phase()` names live positions (`done`, `style-fix`, `style-eval`, `style-eval-review`, `unknown`) and never returns `verify`. They are correct because they differ; "syncing" them makes the documentation wrong.

**`checkout_root` and the `ACTIVE_CHECKOUT_*` arrays in `fix-usage.sh` are not optional.** `project_display_for_entry` calls `checkout_root`; removing them breaks display for any redirected project.

**Known unrepaired defects in the pipeline itself**, surfaced by a forced end-to-end run and confirmed pre-existing: a `codex exec --full-auto` flag the current binary rejects; a configured project path that no longer exists on disk; and the heredoc backtick violation above.

**Two dated accounts of the 2026-06-02 wedged run are preserved deliberately** — one in the README's **Reliability guards** section, one in `rg-shim.sh:13`. They are incident history, not stale prose to rewrite. The incident: `rg PATTERN -g '*.rs' | rg -v X | head` with glob filters but no path argument makes `rg` read stdin, the agent harness hands every command an open stdin pipe that never delivers and never closes, the eval stage's serial `wait` stalled, the parent orchestrator stayed alive, and the trigger's `pgrep` guard then suppressed every run for twelve hours. The containment that exists today is the eval-stage `wait_or_timeout` watchdog, which kills the agent's whole process tree.

## Why

**Why the pipeline is style-only.** It used to carry a second capability — a nightly `cargo clean` + build + mend + warmup pass — on the same orchestrator, the same conf file, the same report parser, and the same diagram. Every reader of any of those had to first answer "which scope am I in", and the config surface carried four sections (`[settings]`, `[build]`, `[cargo_run]`, `[examples]`) that only the retired capability used. Removing it left one purpose, one scheduled job, one allowlist, and one four-column report; the rename followed because the old name described a capability that no longer exists.

**Why the three stage switches are independent and fall through.** Each stage answers its own `enabled=` and, when off, logs a distinct `SKIP:` line and continues. A disabled eval stage does not prevent a review or a fix pass over work already pending.

**Why enablement is scheduled-run policy rather than a global off switch.** The switches exist to shape a job that fires 144 times a day; nothing about `/fix eval off` was ever meant to answer "should this fix, which I just asked for by hand, happen". Honoring them everywhere made three hand-invoked paths no-op — `/fix run` (which needed a `run_once` twin purely to escape its own gate), a standalone stage script, and `/style_eval <project> --fix`, which announced `fix running, log: <path>`, armed a monitor, and then watched a launcher exit 0 on `Style fix is disabled.`. Marking the *scheduled* caller instead of forcing the interactive ones inverts the default so that the failure mode is a stage that runs when you did not want it, which is visible, rather than one that silently does not. `run_once` disappeared in the same change: with the gate gone, `/fix run` already is it.

**Why stage enablement and agent assignment live in different files.** Enablement is pipeline-local operational state that the user flips constantly (`/fix eval off`). Family/agent/effort is a cross-cutting concern shared with `/plan:delegate`, the CLI aliases, and the review teams, and lives in the one registry so a vendor switch is a one-line edit. `fix.sh` hard-errors if a stale `agent=`/`model=`/`effort=` row reappears in `fix.conf`, rather than silently honoring a value nothing reads.

**Why the report is gated on activity.** The pipeline fires 144 times a day. An all-`SKIP` cycle produces no per-project result lines, and rendering a report through an agent on every idle cycle would be pure cost. The gate is also user-visible in both directions — it decides whether the pipeline's main visible product appears at all — which is why the diagram draws both outcomes rather than treating the split as implementation detail.

**Why `[active_checkout]` exists separately from `[projects]`.** A project's identity, history key, and `_style_fix` directory name all derive from its `[projects]` entry. Redirecting work at a worktree by editing the `[projects]` line would fork the history. The redirect is a second map keyed by the entry, so `retarget_fix.py` can point eval/fix at a worktree and revert it later without the entry ever changing.

**Why the retired banner literals stay in the parser.** They are compatibility, not residue. The log directory holds about a day of runs; at the time of the rename 142 of the 145 logs on disk carried a retired banner. Deleting the literals reclassifies every retained log as unfinished.

### Ruled out — settled, do not re-propose

**Structure and packaging**

- **Splitting a conf-section deletion from its readers across two commits.** All three writers of the deleted `[build]` section reached it through a bounds lookup that *raises* on a missing section rather than degrading to a no-op, so a gap between the two commits would break `/fix add`, `/fix rename`, and worktree retargeting outright.
- **Renaming the `.dot`/`.svg` in a different commit from a content change.** The rename and the re-render land together; splitting them points the renderer at basenames that do not exist.
- **Keeping single-valued indirection after the second value went away** — a one-key `SCOPE_SECTION` map, `add_to_section`'s `unique_key` flag with its one-element result list, and the `scope` parameter on six `phase_skip.py` functions. Nothing left to resolve.

**Type and API refactors that were tempting but out of scope**

- **A semantic recognized/not-recognized return for `match_completion_banner()`** in place of `re.Match[str] | None`. That is the `re` module's own boundary form.
- **Reshaping `PhaseStats.footer_ok`/`footer_fail`/`footer_total`'s `int | None`.** The `None` is a real state (footer not seen), not an absence, and reworking it costs reviewability.
- **Restructuring pre-existing optionals the work does not touch** — `Project.kind`/`workspace_root`, `Plan.pending_path`, the config-section helpers. A type refactor of working code is not part of a deletion or a rename.

**Compatibility and vocabulary**

- **Deleting the retired banner literals as name residue.** They are the compatibility path for retained logs; see above.
- **Narrowing the parser's era-agnostic `*.log` enumeration to a name-prefixed pattern.** Migrated history would stop being reachable.
- **Deleting only the retired scope words and keeping the surviving `style` pair.** With no scope arm left, `style` reads as a project filter matching nothing, so documenting it would document a silent no-match.
- **Reviving any launchd-agent management under `scripts/fix/`.** Both the pre-split `com.natemccoy.clean-fix` retirement and a hypothetical `com.natemccoy.cargo-clean` one went away with `setup.sh`, and neither label appears in the Mac's pre-migration launchd inventory, so nothing is stranded. Agent lifecycle belongs to the nixos flake now.

**Testing and scope**

- **Deferring the log fixture and the phase regression test.** Without them the parser deletions would have shipped with no oracle; the differential test on the fixture is what proved the eval-slice widening invisible.
- **Routing the missing `MONITOR_FILTER_REGEX` anchor regression to a backlog.** It belongs in `tests/test_report_parse_phases.py`, alongside the change that would break it.
- **Depicting every skip reason in the flow diagram.** `style-eval-all.sh` carries at least one more ("already at cap of `$MAX_NEW_FINDINGS` findings"). The diagram is a summary of the pipeline's structure, not a branch table.
- **Repairing the pre-existing pipeline defects listed under gotchas as part of a rename.** Each was proven unchanged at the pre-change baseline and is unrelated to the work that exposed it.
