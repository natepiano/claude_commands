# Remove the clean capability from clean-fix, and rename it to `fix`

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Deletes the nightly `cargo clean` + build + mend + warmup pass and its launchd job, then renames the surviving style pipeline from `clean_fix` / `clean-fix` to `fix`.

## Delegation Context

- **Project:** `~/.claude` — the user's global Claude Code configuration repository: slash commands (`commands/`), supporting scripts (`scripts/`), shared config (`config/`), and docs (`docs/`). The clean-fix pipeline is an unattended launchd-driven Rust style evaluator living in `scripts/clean-fix/`.
- **Project started:** 2026-08-25T20:00:14.913+00:00
- **Stack:** Bash 3.2 (macOS system bash, `#!/bin/bash`), Python 3.13 (stdlib + `unittest`, type-checked by basedpyright), launchd plists, Graphviz `.dot`, and Markdown command/prompt files.
- **Layout:**
  - `scripts/clean-fix/` — the pipeline: orchestrator, launchd trigger, plists, conf files, stage scripts, report parser, usage screen, flow diagram
  - `scripts/clean-fix/tests/` — one `unittest` file (`test_style_fix_prompt_comments.py`)
  - `commands/clean_fix.md` — the `/clean_fix` slash command
  - `commands/` — sibling commands that cross-reference clean-fix
  - `config/` — `agents.conf` (agent registry), `lint.conf` (lint switches), `README.md`
  - `docs/as-built/agent-registry.md` — the registry as-built doc
  - `scripts/make_a_worktree/`, `scripts/worktree_delete/`, `scripts/new_rust_project/`, `scripts/bevy_migration_plan/` — consumers of `clean-fix.conf`
  - Outside the repo: `~/Library/LaunchAgents/` (one plist symlink), `~/.local/logs/clean-fix/` (run logs), `~/rust/nate_style/.history/` (durable style state)
- **Key files** — line refs are the **post-Phase-3** tree, which is what a delegate now opens. Phases 1 through 3 deleted the clean capability's config, helpers, and orchestration, so anything those phases removed is absent from this map by design: there is no `[clean]` stage, no `[settings]` / `[build]` / `[cargo_run]` / `[examples]` conf section, no `cf_print_stage_enabled`, no `SCOPE_SECTION`, no `clean-fix-warmup.sh`, and no `~/.local/state/clean-fix/`. Re-grep before trusting any single number; the counts move as later phases land.
  - `scripts/clean-fix/clean-fix.sh` — orchestrator, 247 lines. `run_once` argument parse `:15-18`; `LOG_DIR` `:34`; `LEGACY_LOG` `:36`; `CONF_FILE` `:37`; `log_run_once_summary()` `:60-65`; `checkout_root()` `:84`; `project_filter_key()` `:89`; start banner `:174`; the three stage `SKIP:` lines `:200,211,220`; completion banner `:226`; `REPORT_FILE` `:231`; the `agent_exec.sh cleanfix.report` call `:237`
  - `scripts/clean-fix/clean-fix-trigger.sh` — launchd wrapper, 19 lines. `CLEAN_FIX_SCRIPT` `:13`; pgrep guard `:15`; `exec` `:19`. No idle gate and no scope argument
  - `scripts/clean-fix/style-fix-monitor.py` — `LOG_DIR` `:38`; watches the run-log directory
  - `scripts/clean-fix/style-fix-manual.sh` — `LOG_DIR` `:25`; header comment `:3`
  - `scripts/clean-fix/tests/` — one `unittest` file today (`test_style_fix_prompt_comments.py`); Phase 4 adds `fixtures/six-phase-run.log` and `test_report_parse_phases.py`
  - `scripts/clean-fix/com.natemccoy.style-fix.plist` — the only launchd job. `ProgramArguments` `:10-14`, two strings, no scope word; `StartInterval` 600
  - `scripts/clean-fix/setup.sh` — installs the one surviving plist. The retired-pre-split `OLD_LABEL` cleanup block is unrelated history hygiene and stays
  - `scripts/clean-fix/agent_assignments.sh` — stage loading, 119 lines. `CLEAN_FIX_AGENT_DIR` `:7`; `CLEAN_FIX_AGENT_ASSIGNMENTS_FILE` `:8`; `cf_load_stage_enabled` `:48`; `cf_load_stage_assignment` `:84` with `agents_resolve "cleanfix.$want_section"` `:93`; `cf_print_stage_assignment` `:104`; `cf_print_agent_assignments` `:109`
  - `scripts/clean-fix/agent-assignments.conf` — stage enablement, three sections: `[style_eval]`, `[style_eval_review]`, `[style_fix]`, each carrying `enabled=` alone
  - `scripts/clean-fix/clean-fix.conf` — `[style_eval]` `:10`, `[style_fix]` `:24`, `[project_env]` `:39`, `[projects]` `:50`, `[active_checkout]` `:92`. One allowlist; nothing runs unless `[projects]` lists it
  - `scripts/clean-fix/clean_fix_report_parse.py` — 1986 lines. `LOG_DIR` `:27`; `MONITOR_FILTER_REGEX` `:36-41`; `PHASES` `:43` (now `eval`, `review`, `fix`, `verify`); `COMPLETE_RE` `:96`; `HISTORICAL_COMPLETE_RE` `:99-102`; `match_completion_banner()` `:104-106`; `PhaseStats` `:193-202` (`ok`, `fail`, `skip`, `running`, `footer_ok`, `footer_fail`, `footer_total`, `present`); conf project map `:395`; `detect_phase_boundaries()` `:658`; `parse_log()` `:1165` with the banner check at `:1183` and the project-marker read at `:1237`; `detect_current_phase()` `:1792` with its backward walk at `:1803`. The clean and warmup parsers are gone.
  - `scripts/clean-fix/clean-fix-usage.sh` — 590 lines. `MARKER` `:8`; `CLEAN_STATUSES` `:17,232,238-262,298,323,347-348`; usage lines `:33-55`; clean row `:461`
  - `scripts/clean-fix/report-render.md` — report prompt. `ROW` format `:26`; `[build]` gloss `:27`; phases note `:156`; log path `:11`
  - `scripts/clean-fix/phase_skip.py` — `MARKER` `:28`; `PASS_LABEL = "style"` `:32`; the skip-marker docstring `:8-9`. Every entry point takes projects only; the scope token is optional at the CLI and absent from every signature
  - `scripts/clean-fix/project_add.py` — `MARKER` `:19`; `add_to_section()` and `add_project()` `:301`, `[projects]` only, returning one `SectionResult`
  - `scripts/clean-fix/project_rename.py` — `update_active_checkout()` `:226`; keyed-section rename; marker glob `*_style_fix/.clean-fix-project` `:303`
  - `scripts/clean-fix/style-fix-worktrees.sh` — its own `project_env_for()` `:409-420`; marker write `:630,635-637`
  - `scripts/clean-fix/style_history.py` — `PROJECT_MARKER` `:462`
  - `scripts/clean-fix/README.md` — pipeline overview, file table `:9-25`, warmup section `:33-37`, flow diagram `:97-104`. Names `[cleanfix.<family>]` at `:18` and `agents_resolve cleanfix.<stage>` at `:19`
  - `scripts/clean-fix/clean-fix-style-flow.dot` — four clusters (`cluster_eval`, `cluster_review`, `cluster_fix`, `cluster_manual`), three per-stage enablement diamonds, an `activity_gate` before the report, and the ungrouped `report`/`report_idle` outcome nodes. Absolute `pos="X,Y!"` coordinates consumed by `neato -n2`
  - `scripts/clean-fix/render-flow.py` — renders the dot to SVG via `neato -n2`
  - `commands/clean_fix.md` — **335 lines after Phase 5 rewrote it.** `<Monitor/>`'s stop condition is at `:236`; the log-directory references are at `:65,75,164-165,263`. Phase 5 deleted the clean scope forms, the `**clean**` stage bullet, and three of the six skip lines, so every other offset the plan once recorded for this file has moved — **re-grep, do not trust a number written before Phase 5**.
  - `scripts/make_a_worktree/retarget_clean_fix.py` — redirect-only. `DetectResult` `:37`; `detect()` `:94`; `apply()` `:142` upserting `[active_checkout]`; `revert()` `:161`. No `[build]` add or revert
  - `scripts/worktree_delete/perform_deletion.sh` — marker read `:40,46-47`
  - `scripts/new_rust_project/rust_generate.sh` — conf enrollment heredoc `:148-183`, `ensure('build', rootdir)` `:180`. **Still writes a `[build]` section the conf no longer has** — Phase 7 deletes that call
  - `scripts/agents/test_sync_codex_catalog.sh` — registry test. Its `agents.conf` fixture heredocs and assertions carry the `cleanfix` family key at `:67,69,126,128,160,162,187,189,210,212,214,216`. It holds **no** `scripts/clean-fix` path
  - `scripts/claude_to_codex/run_sync.sh` -> `scripts/claude_to_codex/sync.py` — generates `~/.codex/skills/generated-from-claude/<command>/SKILL.md` from `commands/`. This, not anything under `scripts/agents/`, is the command-to-skill synchronizer
  - `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` — comment `:30-32`
  - `config/agents.conf` — `cleanfix=codex` `:21`; `[cleanfix.codex]` `:56`; `[cleanfix.claude]` `:62`
  - `config/lint.conf` — mend consumer list `:14,32`
  - `.claude/settings.local.json` — 12 permission entries hardcoding `scripts/clean-fix/` paths at `:7,11,12,23,53,55-59,61,75`
  - `pyrightconfig.json` — execution environment for `scripts/clean-fix` `:12`
- **Build:** `bash -n <each touched .sh file>` and `python3 -m py_compile <each touched .py file>` — both must exit 0. There is no compile step for this repo.
- **Test:** `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` — must print `OK` (3 tests). Baseline at plan time: green.
- **Lint:** `basedpyright scripts/clean-fix/` — must print `0 errors, 0 warnings, 0 notes`. Baseline at plan time: exactly that. Add the directory of any Python file touched outside `scripts/clean-fix/` to the same invocation.
- **Invariants:**
  - **Protected paths.** `CLAUDE.md`, `settings.json`, and the `commands/`, `skills/`, `hooks/`, `agents/` directories are carved out of the write sandbox. Edit them with the Edit/Write tools only — shell redirection, `sed -i`, and `mv` fail with `Operation not permitted`, and `dangerouslyDisableSandbox` is the wrong fix.
  - **Unsandboxed commands.** `launchctl`, and any `rm`/`ln` under `~/Library/LaunchAgents`, must run with `dangerouslyDisableSandbox: true` from the start.
  - **Bash 3.2.** macOS system bash. No associative arrays, no `${var,,}`, no bash-4 constructs. The pipeline scripts run under `#!/bin/bash`.
  - **basedpyright must stay at zero errors and zero warnings.** Never use a file-level type ignore (`# pyright: reportAny=false`). Avoid `Any`; annotate signatures; use `TypedDict` for known-key dicts.
  - **The style pipeline runs unattended every 10 minutes** via `com.natemccoy.style-fix`. `clean-fix.sh`, `clean-fix-trigger.sh`, `agent_assignments.sh`, the three stage scripts, and `clean_fix_report_parse.py` must never be left broken between phases.
  - **The conf is an opt-in allowlist model with no deny list.** Nothing runs unless it is listed. Preserve that framing in every comment rewrite.
  - **Terminology:** use allowlist/denylist, never whitelist/blacklist.
  - **Durable style state is out of scope.** `~/rust/nate_style/.history/` (pending JSON, history JSONL) is not named clean-fix and must not be touched or migrated by any phase.

## Phases

### Phase 1 — Retire the cargo-clean launchd job · status: done

#### As-built

The nightly 4 AM cargo-clean agent is gone: booted out of the user launchd domain, its symlink removed from `~/Library/LaunchAgents/`, and its plist deleted from the repo. `com.natemccoy.style-fix` is untouched, still loaded, and still firing on schedule. `setup.sh` installs exactly one agent and creates only `$HOME/.local/logs`; it no longer creates `$HOME/.local/state/clean-fix`. The style-fix plist comment is single-scope: it explains the pgrep guard as skipping the firing if any `clean-fix.sh` run is still in flight, with no mention of a clean job or a cross-reference to a second plist. The clean code path in `clean-fix.sh`, `clean-fix-trigger.sh`, and `clean-fix-warmup.sh` is still present and simply no longer triggered.

**Files:**
- `scripts/clean-fix/setup.sh` — single-element `PLIST_NAMES` (loop retained), one runtime directory, plus the unrelated cleanup block retiring the pre-split `com.natemccoy.clean-fix` label
- `scripts/clean-fix/com.natemccoy.style-fix.plist` — the only installed agent; comment is single-scope accurate
- `scripts/clean-fix/com.natemccoy.cargo-clean.plist` — deleted

**Binds later work:** `~/.local/state/clean-fix` is no longer created by the installer, and dropping it is inert — `clean-fix.sh:68` and `clean-fix-warmup.sh` each `mkdir -p` it themselves. The style-fix plist comment needs no further single-scope edit. User-visible text still advertising a nightly clean survives in `clean-fix.sh:7`, `clean-fix-usage.sh:43`, `README.md:21,99`, and `phase_skip.py:7`.

**Gotchas:** `setup.sh` decides whether an agent is current by comparing symlink targets, not plist contents, so editing a plist in place leaves the loaded agent stale until an explicit `bootout`/`bootstrap`. Every `launchctl` call against this domain must run unsandboxed.

**Ruled out:** unrolling the `PLIST_NAMES` loop now that it holds one element (churn); removing the pre-split `com.natemccoy.clean-fix` cleanup block (unrelated history hygiene); adding a block that retires a `com.natemccoy.cargo-clean` lingering in another checkout's launchd domain (implementation-only unless a second machine exists).

### Phase 2 — Strip the clean pass from the orchestrator and trigger · status: done

#### As-built

`clean-fix.sh` is a style-only orchestrator (247 lines). No `SCOPE` variable exists; the single question of whether an invocation is the one-time override is held by the `true`/`false` string `RUN_ONCE_REQUESTED`, which still exports `CLEAN_FIX_FORCE_STYLE_STAGES=1`. The `clean|style|all` case arm is gone, so any leading token other than `run_once` is a project filter. The clean block, the `cargo mend` call, the warmup invocation, `BUILD_TARGETS`, `CLEAN_ENABLED`, `TIMESTAMP_DIR`, the `build)` conf arm, `cf_load_stage_enabled clean`, and the six build-target helpers are removed; `project_key()`, `checkout_root()`, `project_filter_key()`, and the `backpopulate_settings.py --apply` call remain. The start banner has three forms — `(project: …)`, `(run_once)`, and bare — and the completion line is `=== Clean-fix complete (…) ===`. `clean-fix-trigger.sh` is a concurrency guard plus an `exec`: no argument, no validator, no `IDLE_THRESHOLD_SECONDS`, no `ioreg`/`awk` HID idle read, `pgrep -f` guard unchanged. `clean_fix_report_parse.py` recognizes both banner generations through one helper: `COMPLETE_RE` for the current wording, `HISTORICAL_COMPLETE_RE` for the retired `Clean-fix Rust clean + rebuild complete`, and `match_completion_banner()` as the single entry point for `parse_log` and `detect_current_phase`.

**Files:**
- `scripts/clean-fix/clean-fix.sh` — style-only orchestrator, 247 lines; `LOG_DIR` at 34, `LEGACY_LOG` at 36, completion banner at 226, `REPORT_FILE` at 231
- `scripts/clean-fix/clean-fix-trigger.sh` — 19 lines; orchestrator path variable at 13, `pgrep` guard at 15, `exec` at 19
- `scripts/clean-fix/clean_fix_report_parse.py` — both completion regexes and the shared matcher at 97-107, called at 1309 and 1931
- `scripts/clean-fix/com.natemccoy.style-fix.plist` — two-argument `ProgramArguments` (`/bin/bash` plus the trigger path); the loaded launchd job matches the file
- `scripts/clean-fix/clean-fix-warmup.sh` — deleted, along with its `~/.local/state/clean-fix` state directory

**Binds later work:** `HISTORICAL_COMPLETE_RE` is load-bearing compatibility, not brand residue — deleting it reclassifies every retained log as unfinished. `match_completion_banner()` is the only place banner wording is recognized, so a further rename is a single-site edit that adds a generation rather than replacing one. `detect_current_phase` still carries `warmup` and `clean+rebuild` arms. The command doc still advertises `[clean|style]` and `run clean`/`run style`/`run all`, and its armed-monitor stop condition still matches the retired banner, so a monitored run never reports finished.

**Gotchas:**
- Retained logs are a compatibility surface: the log directory holds about a day of runs, and 142 of the 145 on disk carry the retired banner.
- With the scope words gone, `clean-fix.sh clean` and `clean-fix.sh style` no longer fail — each reads as a project filter matching nothing, a silent misfire rather than an error.
- The launchd job fires every 600 seconds with no idle gate and will run against edited code mid-change; anything moving its inputs or its log directory has to quiesce it first.
- `setup.sh` compares symlink targets, not plist contents, so it reports "Already set up" after an in-place plist edit; an unsandboxed `launchctl bootout`/`bootstrap` pair is what loads a changed agent.

**Ruled out:**
- A semantic recognized/not-recognized return for `match_completion_banner()` in place of `re.Match[str] | None` — that is the `re` module boundary form.
- Restructuring pre-existing optionals the change does not touch (`PhaseStats.footer_*`, `Project.kind`/`workspace_root`, `Plan.pending_path`, config-section helpers) — a type refactor of working code.
- Deleting the retired banner literal as brand residue — it is compatibility, and a permitted exception in the residual-brand sweep.

### Phase 3 — Drop the clean stage, the build/warmup config, and their helper code · status: done

#### As-built

`clean-fix.conf` carries five sections — `[style_eval]`, `[style_fix]`, `[project_env]`, `[projects]`, `[active_checkout]` — and its header describes one allowlist: nothing runs unless `[projects]` lists it, there is no deny list, and `[active_checkout]` redirects an entry's eval/fix at a worktree while identity and history stay with the entry. `[settings]`, `[build]`, `[cargo_run]`, and `[examples]` are gone, and `[project_env]`'s comment names its one remaining consumer, `style-fix-worktrees.sh:409-420`.

`agent-assignments.conf` holds three stage sections, each carrying `enabled=` alone. `agent_assignments.sh` lost `cf_print_stage_enabled` and its call site; `cf_load_stage_enabled` survives because `cf_load_stage_assignment` still calls it for all three style stages.

The four conf helpers know only about sections that exist. `phase_skip.py` resolves `[projects]` through a `PROJECTS_SECTION` constant, takes the scope token as an optional leading word, and types its action as a closed `PhaseSkipAction = Literal["skip", "enable", "enable-all", "status"]` — argparse's `str | None` is converted at the namespace boundary, so no optional reaches module code. `project_add.py` adds to `[projects]` alone and returns one `SectionResult`. `project_rename.py` migrates `[project_env]` and `[active_checkout]`. `retarget_clean_fix.py` is redirect-only: detect, apply, revert against `[active_checkout]`, with `--commit` still committing `clean-fix.conf` alone.

**Files:**
- `scripts/clean-fix/clean-fix.conf` — five sections, one allowlist, 31 `[projects]` entries
- `scripts/clean-fix/agent-assignments.conf` — three stage sections, `enabled=` only
- `scripts/clean-fix/agent_assignments.sh` — `cf_load_stage_enabled` `:48`, `cf_load_stage_assignment` `:84` resolving `agents_resolve "cleanfix.$want_section"` `:93`, `cf_print_agent_assignments` `:109`
- `scripts/clean-fix/phase_skip.py` — `MARKER` `:28`, `PASS_LABEL = "style"` `:32`, six scope-free entry points
- `scripts/clean-fix/project_add.py` — `MARKER` `:19`, `add_project()` `:301`
- `scripts/clean-fix/project_rename.py` — `update_active_checkout()` `:226`, marker glob `:303`
- `scripts/make_a_worktree/retarget_clean_fix.py` — `DetectResult` `:37`, `detect()` `:94`, `apply()` `:142`, `revert()` `:161`

**Binds later work:**
- `phase_skip.py` accepts both `phase_skip.py <action> [project ...]` and a leading `style` token; the two forms print identical output. The short form is what the usage screen and command doc advertise.
- `PASS_LABEL = "style"` feeds six user-visible messages. It names the style *pass*, not the clean-fix brand, so the rename sweep leaves it and its message text byte-identical.
- `project_add.py` prints exactly one `[projects]` result line. `/clean_fix add` relays that verbatim, so a second line means a caller still formats for the retired two-allowlist shape.
- `retarget_clean_fix.py` emits neither `build_add` nor `build_already`. `commands/make_a_worktree.md:108` still instructs an agent to report `build_add`, and `scripts/new_rust_project/rust_generate.sh:180` still calls `ensure('build', rootdir)` and would recreate the deleted section — both belong to the documentation sweep.
- `#CLEAN_FIX_SKIP#` and `.clean-fix-project` keep their names here; the identifier rename owns them together with every reader.

**Gotchas:**
- All three `[build]` writers reached the section through a bounds lookup that **raises** on a missing section rather than degrading to a no-op, which is why the conf deletion and its readers had to land in one commit.
- `project_add.py` and `retarget_clean_fix.py` each carry their own private copy of the section-bounds helper; only `project_rename.py`'s copy was orphaned when its last caller went.
- `clean-fix-usage.sh` still scans for `[build]`, but as a read that skips a missing section rather than a bounds lookup, so it stays green until the usage-screen phase corrects it.

**Ruled out:**
- Splitting the conf deletion from its readers across two commits — the gap would break `/clean_fix add`, `/clean_fix rename`, and worktree retargeting.
- Keeping `SCOPE_SECTION` as a one-key map, `add_to_section`'s `unique_key` flag with its one-element result list, and the `scope` parameter on six `phase_skip.py` functions: single-valued indirection with nothing left to resolve.

### Phase 4 — Remove the clean and warmup phases from the report parser · status: done

#### As-built

`scripts/clean-fix/clean_fix_report_parse.py`'s `PHASES` tuple is now `("eval", "review", "fix", "verify")`. `parse_clean_phase()` and `parse_warmup_phase()` are gone, along with `detect_phase_boundaries`'s `clean_start`/`warmup_start`/`clean_end`/`warmup_end` computation and the `CLEAN:`/`WARMUP:` guards that fed it. `eval_start` is a constant `0` — the whole preamble falls inside the eval slice rather than starting at the eval header's index — because `parse_eval_phase` keys on its own line vocabulary, not slice position; `eval_present` now carries the "was there an eval section" question that `eval_start == -1` used to carry. `PhaseStats.processed` and `PhaseStats.warnings` (clean-only) are deleted along with the emitter's `phase == "clean"` branch. `MONITOR_FILTER_REGEX` no longer matches `CLEAN|BUILD|MEND|DONE` or any `WARMUP` verb, and `detect_current_phase`'s backward walk no longer returns `warmup` or `clean+rebuild`. The always-excluded-set comment and the conf-reader's project mapping now describe `[projects]` only. `COMPLETE_RE`, `HISTORICAL_COMPLETE_RE`, and `match_completion_banner()` are untouched. `report-render.md`'s `ROW` contract is `eval=<cell> review=<cell> fix=<cell> verify=<cell>`, its `ALWAYS_EXCLUDED` gloss says `[projects]`, and a separately-found stale claim that the orchestrator scans every `~/rust/` directory "automatically (no allowlist)" is corrected to name the `[projects]` allowlist.

**Files:**
- `scripts/clean-fix/clean_fix_report_parse.py` — four-phase parser; no clean/warmup parsing, boundary detection, or stats fields
- `scripts/clean-fix/tests/fixtures/six-phase-run.log` — hand-built fixture modeling a full pre-Phase-2 six-phase run (banner scope `all`, retired `Clean-fix Rust clean + rebuild complete` completion wording); deliberately contains `CLEAN:`, `BUILD:`, `MEND:`, `DONE:`, and every `WARMUP` verb — it is data, not code, and later vocabulary/residual-brand sweeps must exclude it
- `scripts/clean-fix/tests/test_report_parse_phases.py` — regression test asserting `PHASES`, the four surviving per-project cells against a captured pre-edit baseline, and that no `clean`/`warmup` phase appears in the result
- `scripts/clean-fix/report-render.md` — four-phase `ROW` contract, `[projects]`-only gloss, corrected allowlist claim

**Binds later work:** `PHASES` is `("eval", "review", "fix", "verify")`; `PhaseStats` no longer has `processed`/`warnings`. The file's line numbers shifted by roughly 150 lines from this edit alone — later phases must re-grep rather than trust a recorded offset. `test_report_parse_phases.py` asserts `PHASES` and `Cell` fields by name, so a later tree-wide identifier rename touching either must include this test file in its sweep. `COMPLETE_RE`/`HISTORICAL_COMPLETE_RE`/`match_completion_banner()` are load-bearing compatibility surface for the 148 retained pre-Phase-2 logs and must survive any later cleanup sweep untouched. The fixture's retired vocabulary is sanctioned data any later residual-brand grep must exclude, not a leftover to clean up. The `.clean-fix-project` marker is still unrenamed, left for later work. The parser's `*.log` enumerations are era-agnostic by construction and must stay that way so migrated log history stays reachable.

**Gotchas:** positional phase detection now only strictly holds for review/fix/verify — eval's start collapsed to a constant because `parse_eval_phase` detects by line vocabulary rather than position, so widening its slice is invisible in output; verified by differential testing (byte-identical `PHASE`/`ROW`/`SKIP_REASON`/`NOTE` lines old vs. new on the fixture and a variant fixture), not by argument.

**Ruled out:** reshaping `PhaseStats.footer_ok/footer_fail/footer_total`'s `int | None` shape in this commit — the `None` is a real state, not an absence, but reworking it here costs the reviewability this commit most needs; deferring the fixture and regression test to later work — without them this phase's own deletions would ship with no oracle.

### Phase 5 — Update the user-facing surface: usage screen and command doc · status: done

#### As-built

The `/clean_fix` usage screen offers one `run [project]` form plus `run_once`, three skip forms in the short spelling (`clean_fix skip`, `clean_fix skip <target>...`, `clean_fix skip enable <target>...`), and a project table with a single `Style` column. The `clean_fix clean` status line, the `clean` and `style` scope words, and the `CLEAN_STATUSES` array with every consumer — initializer, `set_project_status` column branch, the sort's parallel swap, the conf reader's `[build]` branch, the table's `%-6s` column, and the `--json` `clean` key — are gone; `set_project_status` takes `(name, key, value)`, and the three `*_build_entry` helpers that only `[build]` reached are deleted. `commands/clean_fix.md` documents a bare project filter and `run_once` as the only scopes, three configurable stages, one `[projects]` allowlist, and the scope-less skip form; `<Monitor/>`'s stop condition waits on `=== Clean-fix complete`, and its `PHASE <name>` list is `style-eval`, `style-eval-review`, `style-fix`, `done`, `unknown`. Every scope word leaves the surface, not only the clean ones, because a non-`run_once` first argument is read as a project filter that silently matches nothing. `MONITOR_FILTER_REGEX`'s `=== ` alternative is `(^|[[:space:]])=== ` rather than `^=== `, so the pipeline that stops an armed monitor actually sees the banner it waits on.

**Files:**
- `scripts/clean-fix/clean-fix-usage.sh` — 514 lines; usage rows, project table, and `--json` emitter, all single-column
- `commands/clean_fix.md` — 335 lines; monitor stop condition at `:236`, log-directory references at `:65,75,164-165,263`, no `/tmp/clean-fix-report.txt`
- `scripts/clean-fix/clean_fix_report_parse.py` — `MONITOR_FILTER_REGEX` at `:36-41`

**Binds later work:** The `=== ` anchor must stay `(^|[[:space:]])`; a renamed `=== `-prefixed banner written through `log()` is already covered, and re-narrowing to `^` breaks the monitor again. `MONITOR_FILTER_REGEX` has no test; a durable regression belongs with whatever change renames the banner, in `tests/test_report_parse_phases.py`. `commands/clean_fix.md` is finished for clean removal and its offsets have all moved. `commands/style_eval.md:347` is the only external caller of a `/clean_fix` invocation path.

**Gotchas:**
- `clean-fix.sh`'s `log()` prefixes `%Y-%m-%d %H:%M:%S`, so any `^`-anchored filter or matcher misses the completion banner — the reason `^=== ` defeated the live monitor across two banner generations.
- `detect_current_phase()` returns only `done`, `style-fix`, `style-eval`, `style-eval-review`, `unknown` — never `verify`. It is a different vocabulary from `PHASES`, which names report-table columns; the two lists are correct because they differ, and "syncing" them makes the doc wrong.
- `checkout_root` and the `ACTIVE_CHECKOUT_*` arrays survive the clean-column removal — `project_display_for_entry` calls `checkout_root` at `:149`. Deleting them as clean-only breaks redirected-project display.
- `report-render.md` treats an empty `/clean_fix report` argument as current keyed-project state; newest-log mode requires `latest` or `newest`.

**Ruled out:** deleting only the `clean` scope words and keeping the `style` pair, which would document a silent no-match; narrowing the parser's era-agnostic `*.log` enumeration globs to a branded pattern; routing the missing monitor-filter regression to a backlog rather than to the change that would break it.

### Phase 6 — Rebuild the flow diagram without the clean cluster · status: done

#### As-built

`clean-fix-style-flow.dot` describes the pipeline as it actually runs, and the checked-in SVG is its regenerated output from `render-flow.py`. The retired clean/build/clippy/warmup cluster and the `foreach_project -> clean -> build -> clippy_build -> warmup -> style_enabled` chain are gone, and three structures the diagram never carried are drawn: a `cluster_review` stage for evaluation review; three independent enablement gates (`eval_enabled`, `review_enabled`, `fix_enabled`), each falling through to the next stage rather than ending the run and each forced on by `run_once`; and an `activity_gate` ahead of the report that splits into `report` and `report_idle`. Four clusters, 38 nodes, 47 edges, two terminals (`report_idle`, `delete_wt`). Four labels now match the scripts: the trigger (every 10 minutes, per the plist's `StartInterval 600`), project selection (the `[projects]` allowlist, `[active_checkout]` resolution, and missing path / `Cargo.toml` skips), the findings cap (`[style_eval] max_new_findings`), and `prechecks` ("source tree"). The two `ttl_gate` outcome edges route to distinct per-project terminals instead of both to `report`, where they overlapped and drew a `neato` `Could not create control points` warning.

**Files:**
- `scripts/clean-fix/clean-fix-style-flow.dot` — hand-maintained source, absolute `pos="X,Y!"` coordinates consumed by `neato -n2`
- `scripts/clean-fix/clean-fix-style-flow.svg` — generated; never hand-edited

**Binds later work:**
- The diagram keeps its "Clean-fix" branding deliberately — `start`, `report`, `report_idle`, and the manual cluster label carry it, and the branding change belongs with the `.dot`/`.svg` rename-and-re-render.
- Pre-existing runtime behavior the diagram documents and the later acceptance gates test: three stage gates at `clean-fix.sh:194`, `:205`, `:214`, each reading its own `enabled=` switch in `agent-assignments.conf` and logging a distinct `SKIP:` line at `:200`, `:211`, `:220`; and the report branch at `:234`, which renders `/tmp/clean-fix-report.txt` only when the run log carries per-project result lines and otherwise logs `Report skipped (no per-project activity this run).` at `:246`. Both are user-controlled and user-visible: the disabled-stage run asserts all three `SKIP:` lines, the idle line, and an unchanged report file; the end-to-end run accepts either arm and compares the report's modification time, because a stale report from an earlier run satisfies a bare existence check.

**Gotchas:**
- The layout is absolute — every node carries `pos="X,Y!"` and `render-flow.py` runs `neato -n2`, so adding a node means computing its coordinate by hand and re-deriving the cluster's x-range. The comment block at the top of the `.dot` is the procedure; the column-centre comment at `:37-42` records the numbers the current layout was built from.
- Re-rendering an unchanged source is byte-stable, so `cmp` against the checked-in SVG is the cheapest proof that an edit changed only what it meant to.
- `render-flow.py` prints `Parsed N clusters` and then silently drops any cluster whose member nodes produce no bounding box; a successful run is not proof the diagram is whole. The structural audit is — every node reachable, no edge naming an undeclared node, exactly two terminals.
- `render-flow.py:38`'s `PHASE_CLUSTER_IDS` still names the deleted `cluster_build` and omits `cluster_review`. Output is correct regardless because all three stage gates share a `y` coordinate, and correcting the tuple renders byte-identically.
- Automated Safari inspection is unavailable here: macOS denies the UI-capture permission. Rasterize in memory or verify structurally.

**Ruled out:**
- Renaming the `.dot`/`.svg` alongside this content change — the rename and re-render land in one commit, and splitting them points the renderer at basenames that do not exist.
- Depicting every skip reason — `style-eval-all.sh:593` carries a further one ("already at cap of $MAX_NEW_FINDINGS findings"); the diagram is a summary of the pipeline's shape, not a branch table.
- Treating the report/idle split as implementation-only — it decides whether the pipeline's main visible product appears.

### Phase 7 — Sweep the repository documentation · status: done

#### As-built

No document in the repository claims a nightly clean/build/mend/warmup pass; every remaining
clean-fix reference describes the single style-only pipeline that runs every 10 minutes through
three independent stages. The pipeline README's **Pipeline flow** block opens at the style-fix job
and names all three stage switches independently, each falling through with its own `SKIP:` line,
ending in a check that either renders the report or logs `Report skipped (no per-project activity
this run)` — both outcomes are drawn. The README's **Style-Fix Worktrees** row still describes the
mend, clippy, test, and format work that stage genuinely performs, and that documentation is
current. `scripts/new_rust_project/rust_generate.sh`'s enrollment heredoc no longer calls
`ensure('build', rootdir)` — the removal is what stops `/new_rust_project` printing a
`[build] section not found — skipping <entry>` warning on stderr — and still calls
`ensure('projects', f'{rootdir}/crates/{name}')`. Two conf-section corrections landed beyond the
original file list: `config/README.md`'s lint.conf consumer count, and the `project_rename.py` row,
which also migrates `[project_env]`, not only `[projects]` and `[active_checkout]`.
`commands/validate_and_push.md:13`, missing from the original file list, was corrected in the same
pass to say the clippy switch quiets a *scheduled* style-fix pass, not a nightly one.

**Files:**
- `scripts/clean-fix/README.md` — opening describes the style-only, every-10-minute pipeline; file
  table drops the cargo-clean plist and warmup rows and names `[project_env]` in the
  `project_rename.py` row; **Pipeline flow** diagram redrawn per above; **Reliability guards** and
  **Style-Fix Worktrees** sections left as accurate.
- `docs/as-built/agent-registry.md` — driver row and unattended-run constraint describe the
  report-render step only, with no clean/build framing.
- `config/README.md`, `config/lint.conf` — mend-stage consumer lists name only `/clippy` and
  `scripts/delegate/verify.sh`.
- `CLAUDE.md:47` — SwiftPM cache-invalidation clause names a dependency bump, a toolchain change, or
  a manual `cargo clean` as the surviving triggers.
- `commands/make_a_worktree.md`, `commands/worktree_delete.md` — worktree offer/revert text drops
  the `[build]` enrollment and removal clauses; the redirect-only description stands.
- `commands/bevy_migration_plan.md:232`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`
  — allowlist reassurance narrowed to "never evaluated, reviewed, or fixed," with a caveat that
  permissions back-population still visits every directory under `~/rust/`.
- `commands/new_rust_project.md` — member completion message drops "nightly."
- `commands/validate_and_push.md:13` — names the clippy switch as quieting a scheduled style-fix
  pass.
- `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md`,
  `.../memory/MEMORY.md` — rewritten for the single surviving launchd job; the point that frequent
  style runs in the log list are normal, not a runaway, is kept.

**Binds later work:** The pipeline README's file table now names `[project_env]` in the
`project_rename.py` row, and its **Pipeline flow** block opens at the style-fix job with three
independent stage gates plus both report outcomes — later edits to that table land below a shifted
line count. `config/README.md`'s lint.conf consumer list is two bullets with a "two consumers"
sentence above it; the count must move with the list on any later edit. The reworded sentences in
`commands/validate_and_push.md:13` and `commands/bevy_migration_plan.md:232` are the current text —
later documentation sweeps correct residual wording around them, not the sentences themselves.

**Gotchas:** `backpopulate_settings.py --apply` runs unconditionally inside `clean-fix.sh` and walks
every non-dot directory under `~/rust/`, not the `[projects]` allowlist — a claim that clean-fix
"never touches" an unlisted directory is false; the accurate claim is that it is never evaluated,
reviewed, or fixed. The disabled-stage smoke run prints exactly three `SKIP:` lines, the completion
banner, and `Report skipped (no per-project activity this run).`, matching the README's Pipeline
flow prose word for word. Two dated 2026-06-02 accounts of a wedged run are preserved deliberately
as history, not rewritten: one in the README's **Reliability guards** section, one in
`scripts/clean-fix/rg-shim.sh:13`.

### Phase 8 — Rename identifiers, markers, and runtime paths · status: done

#### As-built

Renames pipeline internals from `clean-fix`/`cleanfix`/`CLEAN_FIX` to `fix`
(contents only — filenames/dirs are Phase 9). Five env vars drop `CLEAN_`;
two also get a clearer name: `CLEAN_FIX_AGENT_DIR` -> `FIX_PIPELINE_DIR`
(`agent_assignments.sh:7`) and `CLEAN_FIX_SCRIPT` -> `FIX_ORCHESTRATOR_PATH`
(`clean-fix-trigger.sh:13`, matched by its `pgrep -f` guard at `:15`). Agent
registry family `cleanfix` -> `fix` in `config/agents.conf`
(`fix=codex`, `[fix.codex]`, `[fix.claude]`) and every
`agents_resolve`/`agent_exec fix.<stage>` call site. Completion banner is
`=== Fix complete (…) ===`; `match_completion_banner()` also recognizes two
historical wordings (`Clean-fix complete`, `Clean-fix Rust clean + rebuild
complete`, kept not removed) so pre-rename logs still parse as finished, and
both parser call sites (`parse_log:1183`, `detect_current_phase:1799`) plus
`commands/clean_fix.md`'s `<Monitor/>` stop condition go through that one
helper. `MONITOR_FILTER_REGEX`'s `=== ` alternative is
`(^|[[:space:]])=== `, not line-anchored, because `clean-fix.sh`'s `log()`
prefixes a timestamp. Run logs live under `~/.local/logs/fix/` (migrated
from `~/.local/logs/clean-fix/`, launchd job booted out for the move and
reloaded via `setup.sh`); new logs are `fix-YYYYMMDDHHMMSS.log`. Retention
(`clean-fix.sh:49`) prunes both `fix-*.log` and migrated `clean-fix-*.log` in
two explicit branches; the parser's `LOG_DIR.glob("*.log")` enumeration is
untouched since it already spans both eras. Report file is
`/tmp/fix-report.txt`. Skip marker is `#FIX_SKIP#` (existing `clean-fix.conf`
entries rewritten in place); per-worktree marker is `.fix-project`.

**Files:**
- `scripts/clean-fix/clean-fix.sh` — env vars, `LOG_DIR`, `fix-` log prefix,
  two-branch retention glob, `REPORT_FILE`, `fix.<stage>` agent_exec calls
- `scripts/clean-fix/clean_fix_report_parse.py` — `LOG_DIR`, `COMPLETE_RE`,
  `HISTORICAL_COMPLETE_RE`, `match_completion_banner()`; `*.log`
  enumerations unchanged
- `config/agents.conf` — `fix` family key, `[fix.*]` sections
- `commands/clean_fix.md` — `/agent fix …`, marker name, `fix-report.txt`,
  `<Monitor/>` stop condition
- `scripts/clean-fix/tests/test_report_parse_phases.py` — three-generation
  banner test, `MONITOR_FILTER_REGEX` anchor regression test

**Binds later work:** `FIX_ORCHESTRATOR_PATH` in `clean-fix-trigger.sh:13`
still holds the pre-rename script path. `commands/clean_fix.md`'s
`<DetectLog/>` glob (`/tmp/claude/clean-fix-*.log`) still tracks the
orchestrator's script basename, untouched here — a future rename of that
script must add the new basename glob while keeping this one for logs
already on disk. `config/agents.conf` has a git clean filter that hides its
real content from `git status`/`git diff`; verifying a change there needs
`git show HEAD:config/agents.conf`, not a working-tree diff.

**Gotchas:** `setup.sh` prints `Loaded launchd agent`, not `Reloaded`, when
the job was explicitly booted out before reload — expected, not a failure.
The three completion-banner generations must keep resolving through
`match_completion_banner()` until pre-rename logs age out (~24h retention);
do not assert the retired wording is absent from parser source — the
historical set is exactly where those literals now belong.

### Phase 9 — Rename the files and directories · status: done

#### As-built

The pipeline lives at `scripts/fix/`, the command is `/fix`, and the scheduled
launchd job runs the renamed trigger. All 36 renames used `git mv`, so history
follows every moved file. Every `/clean_fix` invocation was rewritten to `/fix`
in the same commit — including five bare-form call sites (`clean_fix run`,
`clean_fix run <project>`, `clean_fix run_once`, `clean_fix skip enable
<target>`) that the Work Order's list of twelve slash-form self-references
missed, found and fixed under the Spec's own rule that this phase owns
invocations, not vocabulary. The Codex skill was regenerated afterward:
`~/.codex/skills/generated-from-claude/fix/SKILL.md` exists, the old
`clean_fix/` directory is gone, and the generated body contains zero
`clean_fix` occurrences.

**Files:**
- `commands/fix.md` — the command doc (was `commands/clean_fix.md`); no slash-command invocation of `/clean_fix` remains anywhere in it
- `scripts/fix/` — the pipeline directory (was `scripts/clean-fix/`), holding `fix.sh`, `fix.conf`, `fix-usage.sh`, `fix-trigger.sh`, `fix_report_parse.py`, `fix-style-flow.dot`/`.svg`, `tests/`, `docs/`
- `scripts/fix/fix-trigger.sh` — `FIX_ORCHESTRATOR_PATH` (renamed in Phase 8) now points at `scripts/fix/fix.sh`; the pgrep concurrency guard matches this exact path
- `scripts/fix/com.natemccoy.style-fix.plist` — `ProgramArguments` names the new trigger path; the launchd label stays `com.natemccoy.style-fix`
- `scripts/make_a_worktree/retarget_fix.py` — redirect helper (was `retarget_clean_fix.py`)
- `scripts/fix/render-flow.py` — `PHASE_CLUSTER_IDS` corrected to `("cluster_eval", "cluster_review", "cluster_fix")`; re-render is byte-identical to the pre-rename SVG
- `pyrightconfig.json`, `.claude/settings.local.json` (12 entries, including the `pkill -f 'fix.sh'` permission) — updated to the new paths

**Binds later work:** The repository-wide `clean-fix` sweep must run against
tracked files (`git grep`), never a raw recursive `grep -r`, inside `~/.claude`
— gitignored runtime state (`paste-cache/`, `history.jsonl`, `file-history/`,
`__pycache__/*.pyc`) still quotes the old name in recorded text and can never
be brought to zero matches. `commands/fix.md` has no remaining `/clean_fix`
invocation left to find — only brand-word prose. Two CamelCased workflow tags,
`<OfferCleanFixRedirect/>` and `<RevertCleanFixRedirect/>`, still carry the
brand name (case-sensitive greps miss them) and are local to one command file.
`scripts/fix/fix-usage.sh` still prints its command table as `clean_fix ...`.

**Gotchas:** `commands/fix.md`'s `<DetectLog/>` candidate glob deliberately
lists both `/tmp/claude/fix-*.log` and `/tmp/claude/clean-fix-*.log` — dropping
the old entry would make interactive logs written before the rename
undiscoverable by `/fix monitor`. The scheduled launchd job fires every 600
seconds with no idle gate and kept running through the renamed trigger during
and after this phase; expect fresh logs in `~/.local/logs/fix/` roughly ten
minutes apart as normal, not stalled, behavior.

### Phase 10 — Sweep the remaining cross-references and verify end to end · status: todo

#### Work Order

**Goal:** Every surviving `clean_fix`, `clean-fix`, or `cleanfix` reference outside session transcripts belongs to one of the six sanctioned compatibility survivals named in Constraints, and one full pipeline run proves the renamed system executes end to end and reports on itself.

The Goal is deliberately *not* "no match remains". Six classes of match must survive this phase — the retired launchd label, the two historical completion banners, the captured pre-change fixture, the test assertions quoting it, the log-retention glob that still prunes migrated `clean-fix-*` filenames, and `<DetectLog/>`'s retained `clean-fix-*` interactive-log pattern that Phase 9 kept beside its new one — and a goal stated as absolute absence contradicts the Constraints below and invites the sweep to delete exactly the compatibility this plan built. The sweep's job is to leave no **unsanctioned** match.

**The Goal is also deliberately not "a report renders".** `fix.sh` renders `/tmp/fix-report.txt` only when the run log carries per-project result lines, and logs `Report skipped (no per-project activity this run)` otherwise — both are the pipeline working correctly, and which one happens depends on whether any enrolled project has open findings that afternoon. This phase is satisfied by exactly one of two outcomes: result lines in the log **and** a freshly rendered report, or the skip line **and** no fresh report while the run still parses as complete. Requiring a rendered report would make the plan's final acceptance depend on the day's workload, and would pass just as readily on a stale file an earlier run left behind.

**Spec:**

**Command cross-references** (all under the protected `commands/` path — Edit/Write only). Each of these names the `/clean_fix` command or the clean-fix brand in prose; rewrite every occurrence as `/fix` and "fix". **Do not plan on rewriting `scripts/clean-fix/` paths here.** Phase 9's acceptance gate requires every one of them to be gone already, so this phase inherits a tree with none left; if one turns up, it is a Phase 9 defect caught late — repair it here and say so in the report, rather than treating path rewriting as this phase's work:
`commands/style_fix_review.md`, `commands/style_usage.md`, `commands/style_eval.md`, `commands/focused_eval.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/clippy.md`, `commands/lint_config.md`, `commands/add_banned_word.md`, `commands/validate_and_push.md`. `commands/style_delete.md` and `commands/style_rename.md` were on this list and are **not** any more: both were re-read whole, case-insensitively, at Phase 9's close, and neither carries any spelling of the old brand. Listing them would reserve two files this phase never edits.

Also sweep `commands/fix.md` itself — but only its **vocabulary**. Its twelve `/clean_fix` invocations and its `<DetectLog/>` glob moved into Phase 9, where the file is renamed and the Codex skill is regenerated from it; a command whose documented invocations resolve to nothing cannot be left standing across a checkpoint. What remains here is prose: the `Clean-fix already running (PID …)` message, `No clean-fix logs modified in the last 2 hours`, the frontmatter `description`, and the surrounding brand words. If a `/clean_fix` form has survived into this phase, that is a Phase 9 defect caught late — repair it here and say so in the report.

**The sweep is case-insensitive, and two of its targets are identifiers rather than prose.** The old brand also survives capitalized and CamelCased, which the literal `clean_fix\|clean-fix` alternation steps straight over: `README.md:13`'s section heading `### Clean-Fix Automation`, and two workflow tag names — `<OfferCleanFixRedirect/>` in `commands/make_a_worktree.md` (`:18`, `:81`, `:112`) and `<RevertCleanFixRedirect/>` in `commands/worktree_delete.md` (`:31`, `:99`, `:105`). Rename both tags to `<OfferFixPipelineRedirect/>` and `<RevertFixPipelineRedirect/>`, definition and call site together, three occurrences each. Each tag is local to the one command file that defines it, so nothing outside these two files refers to either name. Sweep with a case-insensitive `clean[-_]?fix` throughout, or all three of these stay behind.

**This phase is the prose and verification sweep.** Phase 9 already repointed every executable path reference, inside the renamed tree and outside it, and re-rendered the diagram. What is left here is wording — sentences that still say `/clean_fix` or "clean-fix" where nothing breaks but the text is now wrong — plus the end-to-end run that proves the whole plan. If this phase finds a *path* that still resolves to the old location, that is a Phase 9 defect being caught late: fix it here and say so, rather than filing it forward.

**Remaining docs and scripts** — `README.md:14`, `scripts/fix/README.md` (title, every path and command in the file table, the pipeline-flow diagram, the Evaluation State section, and the flowchart-generation instructions), `docs/as-built/agent-registry.md`, `scripts/fix/docs/candidate-enumeration-design.md`, `scripts/fix/style-fix-manual.sh`, `scripts/fix/style-eval-review-prompt.md`, `scripts/fix/rg-shim.sh`, `scripts/fix/setup.sh` (its header comment names the clean-fix agent), and `CLAUDE.md`. **`config/README.md` and `config/lint.conf` are deliberately absent**: Phase 7 removed their last clean-fix references, so both are already clean and listing them would reserve files this phase never edits.

**The renamed core files still carry the brand in their prose and comments** — `fix.sh`, `fix-trigger.sh`, `fix-usage.sh`, `fix.conf`, `fix_report_parse.py`, `agent-assignments.conf`, `agent_assignments.sh`, `phase_skip.py`, `project_add.py`, `project_rename.py`, `style_history.py`, `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-monitor.py`, `report-render.md`, and `fix-style-flow.dot`. Their identifiers and paths were settled in Phases 8 and 9; what remains is comment and message text. Sweep the whole `scripts/fix/` tree rather than working from this list.

Note `scripts/agents/clean_agents_conf.sh` is named for *cleaning the agents conf file* — unrelated to clean-fix. Do not rename it; only correct any `clean-fix` path it contains.

**One match is a live bug, not a brand reference — repair it, do not rename it.** `scripts/fix/style-fix-worktrees.sh` carries `cargo +clean-fix fmt` three times: at `:820` (the `Step 8:` prompt heading), `:821`, and `:999`. The `+name` slot after `cargo` is a **toolchain selector**, and the installed toolchains are `stable`, `nightly`, and `1.96.0` — there has never been one called `clean-fix`. These lines predate this plan: `git blame` puts them at 2026-05-24 and 2026-06-14, collateral damage from the earlier `nightly` -> `clean-fix` directory rename, which swept `cargo +nightly fmt` along with everything else. They have been failing ever since, inside an agent prompt where the failure surfaces as a formatting step that quietly does nothing.

Replace all three with `cargo +nightly fmt`, keeping each line's surrounding arguments intact. A mechanical brand sweep would turn them into `cargo +fix fmt` and preserve the break under a new spelling — which is the same mistake that created them, committed a second time. This is the one place in the phase where the correct edit is not the rename.

**Memory** — rename `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` to `project_fix_10min_schedule.md`, rewrite its frontmatter `name:` slug (`clean-fix-10min-schedule`) and `description:` along with its body, and update the pointer line and the `## Clean-fix` heading in `projects/-Users-natemccoy--claude/memory/MEMORY.md`.

**Two further memory files carry the brand, and one of them now states something false.** `sandbox_swiftpm_nesting.md:32` links `[[project_clean_fix_10min_schedule]]`, a wikilink that breaks the moment the rename above lands — repoint it to `[[project_fix_10min_schedule]]`. The same sentence still says a nightly `cargo clean` brings the condition back each morning; that stopped being true when this plan removed the clean capability, so restate the recurrence in terms of what actually triggers it now. `sccache_cargo_env_key_poisoning.md:20` names `(clean-fix, agent shells)` as one of two key spaces and `:49` calls a two-pass cache check the "nightly clean-fix pattern"; rewrite the first as the scheduled style pipeline and the second as a neutral description of the diagnostic sequence. A memory that names a retired job is worse than a stale document: it is loaded into every session as fact.

**The dot source is edited here, so the re-render is certain — not conditional.** Phase 9 re-rendered after the rename and the SVG came back byte-identical, because that rename touched no drawn text. `fix-style-flow.dot` still carries the old brand in five places: the graph identifier `digraph clean_fix_style` at `:1`, the trigger node's visible label at `:35`, the report node's visible label at `:101`, a section comment at `:104`, and the manual cluster's visible label at `:106`. Four of those are text the diagram draws. Rewrite all five, then run `cd scripts/fix && python3 render-flow.py` and commit the regenerated SVG. `render-flow.py` keys on the `cluster_*` **ids** and never on the graph name or any label, so renaming the digraph and rewording the labels cannot disturb its layout pass.

**Regenerate the Codex skill after the command edits.** `commands/fix.md` is the source for the live Codex skill at `~/.codex/skills/generated-from-claude/fix/SKILL.md`, generated by `scripts/claude_to_codex/run_sync.sh` over `~/.claude/commands` (`sync.py:41`). Phase 9 ran that synchronizer to prove the *directory* followed the rename; this phase rewrites the command's own remaining prose — its frontmatter `description`, the `Clean-fix already running (PID …)` message, `No clean-fix logs modified in the last 2 hours`, and the surrounding brand words — plus ten sibling commands, so the generated skill goes stale the moment those edits land. Its `/clean_fix` invocations and its `<DetectLog/>` globs are **not** on that list: Phase 9 rewrote every invocation and widened the glob list to carry both eras, and the published skill already reflects both. Run `bash scripts/claude_to_codex/run_sync.sh` **after** the command sweep and confirm the regenerated `fix/SKILL.md` carries the new wording. Running it before the edits just regenerates the stale text.

**End-to-end verification** (the real acceptance for the whole plan):
1. Record `scripts/fix/agent-assignments.conf`'s checksum.
2. **Invoke the orchestrator by its absolute path**, backgrounded, and wait for the notification; it dispatches real agents and takes minutes:

   `bash "$HOME/.claude/scripts/fix/fix.sh" run_once`

   The absolute path is load-bearing, and so is a preflight check. The only concurrency guard in the system is `fix-trigger.sh`'s `pgrep -f "$HOME/.claude/scripts/fix/fix.sh"`; `fix.sh` itself has none. The scheduled job fires every 600 seconds with no idle gate, and this run takes longer than that, so a firing **will** land during it. Invoked by absolute path, the manual run appears in the process table exactly as that guard expects and the scheduled firing skips itself; invoked as a relative `scripts/fix/fix.sh`, the guard does not match and two orchestrators contend for the same worktrees and history state. Guard the other direction too: run `pgrep -f "$HOME/.claude/scripts/fix/fix.sh"` first and wait for any run already in flight to finish rather than starting a second one. Do not boot the launchd job out for this — an interrupted phase would strand the user's automation unloaded, which is a worse failure than waiting.

   **Do not touch the stage switches.** `run_once` exports `FIX_FORCE_STYLE_STAGES=1`, and all three stage scripts (`style-eval-all.sh:430`, `style-eval-review-all.sh:81`, `style-fix-worktrees.sh:211`) run when that variable is set regardless of their `enabled=` value. Disabling and restoring the switches around the run is a no-op that risks leaving the user's configuration changed if the phase is interrupted between the two edits.
3. Confirm a log appears under `~/.local/logs/fix/` and the run reaches its completion line. **The report is a branch — do not require one.** Record `/tmp/fix-report.txt`'s modification time before the run, or its absence, then accept exactly one of two outcomes: the log carries per-project result lines **and** the report's timestamp advanced with rendered content in it; or the log carries `Report skipped (no per-project activity this run)` **and** the report is unchanged or still absent while the run still parses as complete. `fix.sh:237` gates the render on those result lines, so requiring a report here contradicts both the Goal above and the acceptance gate below, and would pass just as readily on a file an earlier run left behind.
4. Confirm `agent-assignments.conf`'s checksum is unchanged — the override left the persistent configuration exactly as it found it.

**Files:**
- `commands/fix.md` — self-references and frontmatter description
- `commands/style_fix_review.md`, `commands/style_usage.md`, `commands/style_eval.md`, `commands/focused_eval.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/clippy.md`, `commands/lint_config.md`, `commands/add_banned_word.md`, `commands/validate_and_push.md` — `/fix` and `scripts/fix/`. `make_a_worktree.md` and `worktree_delete.md` also carry the two redirect tag renames
- `scripts/fix/README.md` — full rewrite of paths and commands
- `scripts/fix/style-fix-manual.sh`, `scripts/fix/style-eval-review-prompt.md`, `scripts/fix/rg-shim.sh` — path references. `scripts/fix/docs/candidate-enumeration-design.md` was listed here and is not any more: a whole-file case-insensitive read at Phase 9's close found no brand word in it
- `scripts/fix/fix-style-flow.dot` — the graph identifier, three visible labels, and one section comment
- `scripts/fix/fix-style-flow.svg` — regenerated from that dot edit. `scripts/fix/render-flow.py` is **not** an edit target: it carries no brand word at all, and this phase only runs it
- `config/agents.conf` — the `clean-fix stages and report` comment at `:11`, staged with the `AGENTS_CONF_COMMIT=1` override described in Constraints
- `docs/as-built/agent-registry.md`, `README.md`, `CLAUDE.md` — remaining wording. **`config/README.md` and `config/lint.conf` are deliberately absent**: Phase 7 left neither a path nor a brand word in either file
- `commands/new_rust_project.md:87`, `commands/bevy_migration_plan.md:232` — the brand word `clean-fix` in prose, which Phase 7 left in place while it removed only the "nightly" claims. Re-verified against the tree: neither file contains a `/clean_fix` invocation, so neither was ever Phase 9's
- `scripts/fix/style-fix-worktrees.sh` — wording, plus the three broken `cargo +clean-fix fmt` toolchain selectors at `:820,821,999`, which become `cargo +nightly fmt`
- `scripts/fix/` — a whole-tree comment and message sweep across the renamed core files, `setup.sh` header included
- `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `scripts/worktree_delete/perform_deletion.sh` — wording only; Phase 9 already fixed their paths. `scripts/agents/test_sync_codex_catalog.sh` is not here: Phase 8 renamed its `cleanfix` family key and it carries no other clean-fix spelling, so this phase only runs it
- `scripts/make_a_worktree/retarget_fix.py` — **wording only.** Phase 9 already fixed its `DEFAULT_CONF` path and its usage string; what survives here is the docstring line calling a worktree name a valid clean-fix path and the two `chore(clean-fix):` commit messages it writes. Its type-design weaknesses are next-items 2-3 and stay deferred; the existing `basedpyright scripts/make_a_worktree/` gate already covers this file
- `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` — removed by the rename
- `projects/-Users-natemccoy--claude/memory/project_fix_10min_schedule.md` — the memory under its new name, rewritten to say /fix
- `projects/-Users-natemccoy--claude/memory/MEMORY.md` — heading and pointer line
- `projects/-Users-natemccoy--claude/memory/sandbox_swiftpm_nesting.md` — the `[[project_clean_fix_10min_schedule]]` wikilink and the now-false nightly-clean claim in the same sentence
- `projects/-Users-natemccoy--claude/memory/sccache_cargo_env_key_poisoning.md` — the key-space name at `:20` and the "nightly clean-fix pattern" phrasing at `:49`

**Reservations:**
- file: `commands/fix.md`
- file: `commands/style_fix_review.md`
- file: `commands/style_usage.md`
- file: `commands/style_eval.md`
- file: `commands/focused_eval.md`
- file: `commands/make_a_worktree.md`
- file: `commands/worktree_delete.md`
- file: `commands/clippy.md`
- file: `commands/lint_config.md`
- file: `commands/add_banned_word.md`
- file: `commands/validate_and_push.md`
- tree: `scripts/fix`
- file: `commands/new_rust_project.md`
- file: `commands/bevy_migration_plan.md`
- file: `config/agents.conf`
- file: `docs/as-built/agent-registry.md`
- file: `README.md`
- file: `CLAUDE.md`
- file: `scripts/lint/invoke.sh`
- file: `scripts/lint/lint`
- file: `scripts/lint/lint_config.sh`
- file: `scripts/lint/scope.py`
- file: `scripts/delegate/verify.sh`
- file: `scripts/hooks/banned_words_lib.py`
- file: `scripts/agents/clean_agents_conf.sh`
- file: `scripts/new_rust_project/rust_generate.sh`
- file: `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`
- file: `scripts/worktree_delete/perform_deletion.sh`
- file: `scripts/make_a_worktree/retarget_fix.py`
- file: `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md`
- file: `projects/-Users-natemccoy--claude/memory/project_fix_10min_schedule.md`
- file: `projects/-Users-natemccoy--claude/memory/MEMORY.md`
- file: `projects/-Users-natemccoy--claude/memory/sandbox_swiftpm_nesting.md`
- file: `projects/-Users-natemccoy--claude/memory/sccache_cargo_env_key_poisoning.md`

**Constraints from prior phases:**
- Phase 9 moved everything to `scripts/fix/` and `commands/fix.md`, so every path written here is a post-rename path.
- Phase 8 renamed the registry family to `fix` and the runtime log directory to `~/.local/logs/fix/`; docs updated here must name those.
- Phase 6 rebuilt the dot source's content — it removed the clean cluster and also added the evaluation-review stage, three independent stage gates, and an activity gate before the report. Phase 9 re-rendered it after the rename. This phase changes diagram content only if a comment edit lands in the `.dot`, and then re-renders; the node and edge set stays as Phase 6 left it.
- `commands/`, `CLAUDE.md`, and the memory directory are protected or gitignored — use Edit/Write throughout.
- **`config/agents.conf` needs a deliberate staging override or its edit lands empty.** Its comment at `:11` still reads `#   - clean-fix stages and report`, which this phase rewrites. The file carries a git clean filter (`.gitattributes` → `filter=claude-agents-conf`) that pins staged content to the existing index blob, so `git status` and `git diff` both show the edit while `git add` quietly commits the old text. Stage it as `touch config/agents.conf && AGENTS_CONF_COMMIT=1 git add config/agents.conf`, then read the staged blob back with `git cat-file -p :config/agents.conf` before the checkpoint. The filter itself is implementation-only, but a structural change that silently fails to commit is application-observable: a fresh checkout gets the stale registry.
- The surviving `style-*` names are deliberate and stay: `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style_history.py`, `style-fix-monitor.py`, `style-fix-manual.sh`, the `com.natemccoy.style-fix` launchd label, `/tmp/style-fix-stdout.log`, and the `_style_fix` worktree suffix. Do not rename any of them.
- `~/rust/nate_style/.history/` is durable style state and is out of scope.
- **The parser's own domain types are deferred to next-items 1-4 and must keep their current shape through this phase.** `Cell`, `ParseResult`, and `Warning` are named for representation rather than role; `ParseResult.running` reuses `Warning` for a non-warning; phase state and project status are free-form `str`; and a "no reason" is encoded as an empty string. All of it is real, all of it is recorded, and none of it lands here — a rename sweep that also re-tags a domain type is reviewable as neither. Rename identifiers and paths only.
- **Phase 9 already satisfied this phase's `commands/fix.md` fallback.** The Spec above says a surviving `/clean_fix` form would be a Phase 9 defect caught late. None survives: that file was verified clean of every invocation at Phase 9's close, and its `<DetectLog/>` candidate list already carries both `/tmp/claude/fix-*.log` and `/tmp/claude/clean-fix-*.log`. What is left there is brand words. The fallback stays written down because it would catch a regression, not because anything is expected to trip it.
- **Three more deferred semantic types belong to the next-items file, not to this phase.** Beyond the parser types above, `Project.workspace_root: Path | None` (`scripts/fix/project_add.py:32`), `Plan.pending_path: Path | None` with its companion boolean (`scripts/fix/project_rename.py:59`), and the `Bbox` geometry tuple with its absent-value handling (`scripts/fix/render-flow.py:41`) are all bare optionals in domain-owned types. Next-items 2, 3, and 5 own them. This phase edits comments and messages in `project_add.py` and `project_rename.py` and must leave every one of those declarations exactly as it finds them; `render-flow.py` it does not edit at all.
- `HISTORICAL_COMPLETE_RE` and `match_completion_banner()` in `fix_report_parse.py` carry the retired banner wording on purpose, so that logs written before the rename still read as finished runs. Phase 4's `tests/fixtures/six-phase-run.log` and `tests/test_report_parse_phases.py` carry it for the same kind of reason: the fixture *is* a pre-change log, and the test asserts on its exact strings. Together with `setup.sh`'s historical launchd label, these are the sanctioned survivals of the old brand; the acceptance gate below names all four as permitted exceptions rather than things to sweep. A grep-driven edit to any of them is a regression dressed as cleanup.
- **`<DetectLog/>`'s retained interactive-log pattern is the sixth sanctioned survival.** Phase 9 widened `commands/fix.md`'s candidate list to carry both `/tmp/claude/fix-*.log` and `/tmp/claude/clean-fix-*.log`, because that glob tracks the orchestrator's **script basename** and interactive logs written before the rename are still on disk. Deleting the branded entry to satisfy this phase's grep makes `/fix monitor` blind to every pre-rename run. Classify it with the other five and leave it alone.
- **The log-retention glob is the fifth sanctioned survival.** Phase 8 requires `fix.sh`'s retention `find` to prune both the new `fix-*.log` names and the migrated `clean-fix-*.log` files, which keep their old names after the directory move. That branded pattern is load-bearing compatibility, exactly like the historical banners: deleting it to satisfy this phase's grep either strands the migrated history forever or stops pruning it. Classify it with the other four and leave it alone.
- **Narrow the allowlist claim; do not merely rebrand it.** `fix.conf`'s header says "Allowlist model: nothing runs unless it is listed. There is no deny list." and `scripts/fix/README.md`'s conf row repeats it as "No deny list — nothing runs unless listed." Both are wrong as written: `fix.sh` runs `backpopulate_settings.py --apply` unconditionally on every pass, and that helper visits every non-dot directory under `~/rust/` without consulting `[projects]` at all. Rewrite both so the allowlist governs evaluation, review, and fixing, while permissions back-population is stated as repository-wide. This phase owns both files.
- **Preserve what Phase 7 established; these references are current, not stale.** `scripts/fix/README.md:18-19`, top-level `README.md:14`, `commands/validate_and_push.md:13`, and `commands/bevy_migration_plan.md:232` all say true things now, and this phase rebrands their wording without changing their claims. Specifically: the README's pipeline-flow block keeps naming three independent stage switches and both report outcomes; its style-fix row keeps naming the mend, clippy, test, and format work that stage really does; `validate_and_push.md` keeps saying the clippy switch quiets a *scheduled* style-fix pass rather than a nightly one; and `bevy_migration_plan.md` keeps "never evaluated, reviewed, or fixed" together with its caveat that back-population still visits every directory under `~/rust/`. A brand sweep that flattens any of these to a shorter phrasing reintroduces a false claim Phase 7 removed.

**Acceptance gate:**
- `git grep -n -I -i -E 'clean[-_]?fix' -- . ':!docs/plans' ':!projects'` returns **only matches belonging to the six sanctioned classes below, and nothing else**. **The sweep runs through `git grep`, not `grep -r`, and it is case-insensitive — both halves are load-bearing.** `~/.claude`'s `.gitignore` ignores everything by default and allowlists what is tracked, so a raw recursive grep also reads Claude Code's own runtime state: `history.jsonl`, `paste-cache/`, `file-history/`, telemetry, and backups. Every one of those quotes the old name inside recorded session text that nobody may rewrite, so a recursive sweep can never be brought to the sanctioned-six state this gate demands — it is unreachable as originally written, which Phase 9 hit and worked around by hand. Restricting it to tracked files is what makes the gate reachable. Case-insensitivity is what makes it honest: the brand also appears as `Clean-Fix`, `Clean-fix`, and CamelCased inside identifiers, and the original literal alternation missed all three. Read the output and classify every line; an empty result is not the pass condition and would in fact mean the compatibility survivals were destroyed. The six classes are: `setup.sh`'s retired-agent cleanup block referencing the historical label `com.natemccoy.clean-fix`; the retired completion banners inside `fix_report_parse.py`'s `HISTORICAL_COMPLETE_RE` — `Clean-fix complete` and `Clean-fix Rust clean + rebuild complete` — which exist so migrated logs still parse as finished runs; `scripts/fix/tests/fixtures/six-phase-run.log`, which is a captured pre-change log and is **data, not code** — its `=== Starting clean-fix (scope: all) ===` banner, its `CLEAN:`/`BUILD:`/`MEND:`/`DONE:`/`WARMUP:` lines, and its retired completion banner are the whole reason the fixture proves anything; and the assertions in `scripts/fix/tests/test_report_parse_phases.py` that quote those same strings, including all three banner generations; and the log-retention `find` in `scripts/fix/fix.sh`, whose `-name 'clean-fix-*.log'` branch prunes the migrated logs that kept their old filenames through the directory move, which Phase 8 put there deliberately; and `commands/fix.md`'s `<DetectLog/>` candidate entry `/tmp/claude/clean-fix-*.log`, which Phase 9 kept beside its new `fix-*` sibling so interactive logs written before the rename remain discoverable. Rewriting the fixture or the test assertions to satisfy a grep destroys the regression oracle the plan built and leaves the highest-risk file uncovered; rewriting the retention branch strands the log history the migration preserved. Confirm every surviving match falls into one of the six and nothing else; deleting any of them breaks reading the log history this plan deliberately preserved. The `plans` exclusion is deliberate and permanent — this plan and its siblings quote the old names as their subject matter, so the sweep's claim is about maintained implementation, command, configuration, and product documentation, never about the planning record.
- `grep -rn -i -E 'clean[-_]?fix' projects/-Users-natemccoy--claude/memory/` returns nothing. This is the separate ignored-state check: the memory directory is gitignored, so `git grep` cannot see it and the sweep above deliberately excludes it. Four files carry the brand today — the schedule memory this phase renames, `MEMORY.md`'s heading and pointer line, `sandbox_swiftpm_nesting.md`, and `sccache_cargo_env_key_poisoning.md` — and all four are named in Files above.
- `bash -n` exits 0 on every touched shell script.
- After staging, `git cat-file -p :config/agents.conf | grep -n 'clean-fix'` returns nothing — proof the clean filter did not pin the pre-edit blob over this phase's comment rewrite.
- `basedpyright scripts/fix/ scripts/make_a_worktree/ scripts/lint/ scripts/hooks/` prints `0 errors, 0 warnings, 0 notes`.
- `for t in scripts/fix/tests/test_*.py; do python3 "$t" || exit 1; done` prints `OK` for each — both the prompt-comment test and Phase 4's parser regression test.
- `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass.
- `cd scripts/fix && python3 render-flow.py` exits 0 and writes `fix-style-flow.svg`, and the regenerated SVG **differs** from the committed one. This phase edits drawn labels in the dot source, so an unchanged SVG here means the re-render never ran — the opposite of Phase 9's check, where byte-identity was the proof.
- `bash scripts/fix/fix-usage.sh` exits 0 and every command it prints reads `/fix …`.
- `bash scripts/claude_to_codex/run_sync.sh` exits 0 when run **after** the command sweep. Then sweep the whole generated tree case-insensitively: `grep -rn -i -E 'clean[-_]?fix' ~/.codex/skills/generated-from-claude/` returns **exactly** the `<DetectLog/>` compatibility glob `/tmp/claude/clean-fix-*.log` in `fix/SKILL.md` and nothing else, and `grep -c -F '/tmp/claude/fix-*.log' ~/.codex/skills/generated-from-claude/fix/SKILL.md` is non-zero so both eras are present. A `grep -c "clean_fix"` on that one file already returns 0 today — Phase 9's command edits satisfied it in advance — so it can no longer tell this phase's prose rewrite from Phase 9's frozen text, while extensive hyphenated prose sits in the same file unchecked. The directory-wide case-insensitive form discriminates; the old one does not.
- `launchctl print gui/$(id -u)/com.natemccoy.style-fix` names `scripts/fix/fix-trigger.sh` as the job's program argument, under its unchanged `style-fix` label. The end-to-end run below invokes `fix.sh` directly and so never exercises `fix-trigger.sh` at all; without this assertion, the scheduled path that actually fires every ten minutes — the whole point of the capability — has no acceptance owner anywhere in the plan. Scheduled completion stays independently observable as fresh `fix-*` logs accumulating in `~/.local/logs/fix/` about ten minutes apart.
- `grep -n "cargo +" scripts/fix/style-fix-worktrees.sh` shows `cargo +nightly fmt` and no `cargo +clean-fix` or `cargo +fix`; `rustup toolchain list` confirms `nightly` is installed while no `fix`-named toolchain exists.
- **End to end:** `bash "$HOME/.claude/scripts/fix/fix.sh" run_once` — the absolute form, run only once `pgrep -f "$HOME/.claude/scripts/fix/fix.sh"` comes back empty — completes without any change to the stage switches; a new `fix-*` log exists under `~/.local/logs/fix/`; `python3 scripts/fix/fix_report_parse.py --latest-log` reports the run **complete** and renders the four-column `Eval | Review | Fix | Verify` schema. `scripts/fix/agent-assignments.conf` is byte-identical before and after.
- **The report is a branch, so accept either arm and never accept a stale file.** `fix.sh` renders `/tmp/fix-report.txt` only when the run log carries project result lines, and otherwise logs `Report skipped (no per-project activity this run)`. Record `/tmp/fix-report.txt`'s modification time before the run, or its absence. Then require exactly one of two outcomes: the log has result lines **and** the report's timestamp advanced with rendered content in it; or the log has the skip line **and** the report is unchanged or still absent while `--latest-log` reports the run complete. Asserting a report exists satisfies the gate from a file an earlier run left behind, which proves nothing about this one.
- **Do not require populated cells from the live run.** The parser marks a phase present only after it sees that phase's progress markers, so a legitimate `run_once` over a tree with no eligible findings finishes with `Verify` showing `—` and `Fix` showing `SKIP:no-open-findings`. That is the pipeline working, and gating on populated cells makes this phase's outcome depend on whichever projects happen to have open findings that afternoon. The live run proves the renamed system executes and reports; **populated four-phase cell behavior is proven deterministically by Phase 4's fixture test**, which is why that fixture exists.
