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
  - `scripts/clean-fix/clean-fix-style-flow.dot` — `cluster_build` `:43-53`; edge chain `:109`
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

### Phase 6 — Rebuild the flow diagram without the clean cluster · status: todo

#### Work Order

**Goal:** The pipeline flowchart shows only the style phases, and the checked-in SVG is regenerated from the edited dot source.

**Spec:**

**`scripts/clean-fix/clean-fix-style-flow.dot`**
- `:43-53` — delete `subgraph cluster_build` in full: the cluster declaration, its `label="Phase 1: Clean + Rebuild"`, and the four nodes `clean` (`cargo clean`), `build` (`cargo build --workspace`), `clippy_build` (`cargo clippy -D warnings`), and `warmup`.
- `:109` — the edge chain reads `foreach_project -> clean -> build -> clippy_build -> warmup -> style_enabled`. Rewrite as `foreach_project -> style_enabled`.
- Renumber the remaining cluster labels so the surviving phases read `Phase 1`, `Phase 2`, `Phase 3` in order rather than starting at 2.
- The dot file uses absolute `pos="X,Y!"` coordinates consumed by `neato -n2`. Removing the first cluster leaves a vertical gap. Read the layout guide comment at the top of the file and shift the surviving clusters' `pos` values so the diagram has no dead space, keeping relative spacing within each cluster intact. `render-flow.py` aligns cluster tops and rewrites the viewBox, so exact global offsets matter less than internal consistency — but the result must render without overlap.

**Regenerate** — `cd scripts/clean-fix && python3 render-flow.py`. This requires Graphviz (`neato`). If `neato` is not on PATH, install with `brew install graphviz` — do not hand-edit the SVG, which carries a do-not-edit marker.

`scripts/clean-fix/clean-fix-style-flow.svg` is generated output; commit the regenerated file but make no manual change to it.

Do not rename the `.dot`/`.svg` files here — Phase 9 renames them and re-renders in that same commit. This phase owns the diagram's *content*; Phase 9 owns its *filenames*. (An earlier draft of this plan credited the rename to the documentation sweep, which never touches those files.)

**Files:**
- `scripts/clean-fix/clean-fix-style-flow.dot` — delete `cluster_build`, rewire the entry edge, renumber and reposition the surviving clusters
- `scripts/clean-fix/clean-fix-style-flow.svg` — regenerated output, not hand-edited

**Reservations:**
- file: `scripts/clean-fix/clean-fix-style-flow.dot`
- file: `scripts/clean-fix/clean-fix-style-flow.svg`

**Constraints from prior phases:**
- The pipeline the diagram documents now has three style phases only: eval, eval review, and style-fix worktrees (with its apply and verify passes). Phase 4 fixed that set in the parser as `("eval", "review", "fix", "verify")`.
- `render-flow.py` is unchanged by this plan; it parses cluster membership, labels, and colors from the dot file, runs `neato -n2`, injects dashed cluster borders, aligns cluster tops, and rewrites the viewBox.

**Acceptance gate:**
- `grep -nE "cargo clean|clippy_build|warmup|cluster_build" scripts/clean-fix/clean-fix-style-flow.dot` returns nothing.
- `cd scripts/clean-fix && python3 render-flow.py` exits 0.
- `grep -cE "cargo clean|Warmup" scripts/clean-fix/clean-fix-style-flow.svg` returns 0.
- `git diff --stat scripts/clean-fix/clean-fix-style-flow.svg` shows the file changed (proving it was regenerated, not left stale).
- The SVG opens and renders: `open -a Safari scripts/clean-fix/clean-fix-style-flow.svg` — visually confirm no overlapping nodes, no orphaned edge, and cluster labels numbered from 1.

---

### Phase 7 — Sweep the repository documentation · status: todo

#### Work Order

**Goal:** No document in the repository claims clean-fix cleans, builds, mends, or warms up anything.

**Spec:**

Each edit below removes a claim that is now false. Where a sentence merely mentions clean-fix in passing and stays true, leave it.

**`scripts/clean-fix/README.md`**
- `:3` — "Automated clean-fix clean-build, style evaluation, and style-fix pipeline" and `:5` "Runs daily at **4:00 AM** via launchd" are both wrong. Rewrite the opening for a style-only pipeline running every 10 minutes.
- `:9-25` file table — delete the rows for `com.natemccoy.cargo-clean.plist` and `clean-fix-warmup.sh`. Rewrite the `clean-fix.sh` row (no scopes; a project filter and `run_once`), the `clean-fix.conf` row (one allowlist), the `project_add.py` row (adds to `[projects]` only), the `project_rename.py` row (no `[build]`), the `agent-assignments.conf` and `agent_assignments.sh` rows (three stages), and the `setup.sh` row (one agent).
- `:33-37` — delete the **Warmup** section and its table entry for `clean-fix-warmup.sh`.
- `:97-104` — the **Pipeline flow** ASCII diagram opens with the `cargo-clean job (nightly 4:00 AM, idle-gated)` block and its `cargo clean → cargo build → cargo mend → warmup` line. Delete that block; the diagram starts at the style-fix job.
- Leave the **Reliability guards (the rg-hang)** and **Evaluation State** sections alone — both are still accurate.

**`docs/as-built/agent-registry.md`**
- `:142` — the `clean-fix.sh` row describes the driver. Keep the report-render description; remove any clean/build framing if present.
- `:164` — the unattended-run constraint says clean-fix runs every 10 minutes via `com.natemccoy.style-fix.plist`; that is still true, so leave the substance. Verify it does not also reference the clean job.
- Sweep the file for `cargo-clean` and `[build]`; correct what you find.

**`config/README.md:36`** and **`config/lint.conf:14,32`** — both list `scripts/clean-fix/clean-fix.sh` as the mend stage's consumer. That call site is gone (Phase 2). Remove clean-fix from the mend consumer lists. The `mend` switch itself stays — `/clippy` and `scripts/delegate/verify.sh` still read it.

**`README.md:14`** — "Pipeline that evaluates and fixes style across projects; runs on a launchd schedule" is already accurate. Verify and leave, unless it mentions cleaning.

**`CLAUDE.md:47`** (protected path — Edit/Write only) — the SwiftPM sandbox note ends: "once built unsandboxed it stays green until a dependency bump, a toolchain change, or the nightly `cargo clean` makes the scripts re-run." The nightly `cargo clean` no longer exists. Rewrite the clause to name the surviving triggers — a dependency bump, a toolchain change, or a manual `cargo clean` — and delete "nightly".

**`commands/make_a_worktree.md`** (protected path) — `:101` the offer text asks "Point style eval/fix at this worktree (and add it to the nightly build set)?"; drop the parenthetical. `:107` says the helper "adds `[worktree-name]` to `[build]` and writes the `[active_checkout]` redirect(s)"; rewrite for the redirect alone. `:108` instructs the agent to "Report the edits from the JSON (`redirects`, `build_add`)" — `retarget_clean_fix.py` stopped emitting `build_add` in Phase 3, so this line tells the agent to read a key that is never there; reduce it to `redirects` plus the `commit` result. Check `:82-96` for any other build-set claim.

**`commands/worktree_delete.md:103`** (protected path) — "The helper drops any `[active_checkout]` redirect pointing into the worktree and removes the worktree's `[build]` entry"; drop the `[build]` clause.

**`commands/bevy_migration_plan.md:232`** (protected path) and **`scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh:30-32`** — both explain that a Bevy clone under `~/rust/` is never touched because the conf is an opt-in allowlist, naming `[build]` or `[projects]`. Keep the reassurance; reduce the section names to `[projects]` and drop "cleaned/built".

**`scripts/new_rust_project/rust_generate.sh`**
- `:148` — the comment "Enroll in the nightly clean-fix flow" and `:152`'s `echo "=== Enrolling in clean-fix ==="`: drop "nightly".
- `:181` — delete `ensure('build', rootdir)` from the heredoc. Keep `ensure('projects', f'{rootdir}/crates/{name}')`.
- `:203` — the closing message "Built, formatted, clean-fix enrolled, and committed to $ROOTDIR" is still accurate; leave it.

**`commands/new_rust_project.md:87`** (protected path) — "It is already built, formatted, enrolled in nightly clean-fix, and committed"; drop "nightly".

**Memory** (gitignored, but part of the deliverable) — `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` is currently the "Two launchd jobs" note describing style-fix every ~10 min plus cargo-clean nightly at 4 AM. Rewrite it for the single surviving job, keeping the point that frequent style runs in the log list are normal rather than a runaway. Update its one-line pointer in `projects/-Users-natemccoy--claude/memory/MEMORY.md` under "Clean-fix" so the hook matches.

**Files:**
- `scripts/clean-fix/README.md` — pipeline description, file table, warmup section, flow diagram
- `docs/as-built/agent-registry.md` — driver row and unattended-run constraint
- `config/README.md` — mend consumer list
- `config/lint.conf` — mend consumer list
- `README.md` — verify the clean_fix line
- `CLAUDE.md` — SwiftPM note's cache-invalidation trigger
- `commands/make_a_worktree.md` — redirect offer text
- `commands/worktree_delete.md` — revert description
- `commands/bevy_migration_plan.md` — allowlist reassurance
- `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` — allowlist comment
- `scripts/new_rust_project/rust_generate.sh` — enrollment comment and `ensure('build', …)`
- `commands/new_rust_project.md` — member completion message
- `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` — one launchd job
- `projects/-Users-natemccoy--claude/memory/MEMORY.md` — pointer line

**Reservations:**
- file: `scripts/clean-fix/README.md`
- file: `docs/as-built/agent-registry.md`
- file: `config/README.md`
- file: `config/lint.conf`
- file: `README.md`
- file: `CLAUDE.md`
- file: `commands/make_a_worktree.md`
- file: `commands/worktree_delete.md`
- file: `commands/bevy_migration_plan.md`
- file: `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`
- file: `scripts/new_rust_project/rust_generate.sh`
- file: `commands/new_rust_project.md`
- file: `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md`
- file: `projects/-Users-natemccoy--claude/memory/MEMORY.md`

**Constraints from prior phases:**
- Phase 1 removed the `com.natemccoy.cargo-clean` plist and reduced `setup.sh` to one agent — the README file table and setup row must match.
- Phase 2 deleted `clean-fix-warmup.sh` and the `cargo mend` call, which is why clean-fix leaves the mend consumer lists in `config/`.
- Phase 3 left one allowlist (`[projects]`) plus `[active_checkout]`, `[project_env]`, `[style_eval]`, and `[style_fix]` — every doc naming conf sections must name only those.
- Phase 3 made `retarget_clean_fix.py` redirect-only and `project_add.py` `[projects]`-only, which is what `make_a_worktree.md`, `worktree_delete.md`, and `rust_generate.sh` must now describe.
- `CLAUDE.md`, `commands/`, and the memory directory are protected or gitignored: use Edit/Write for all of them. `projects/` is gitignored, so those two files will not appear in `git status` — confirm the edits by reading the files back.
- **Phase 5 finished `commands/clean_fix.md`'s clean removal; do not re-open it here, and do not "sync" its `PHASE <name>` list against the parser's `PHASES`.** That doc's `<Monitor/>` section lists what `--phase-detect` can emit — `style-eval`, `style-eval-review`, `style-fix`, `done`, `unknown` — which is what `detect_current_phase()` actually returns. `PHASES` is a different tuple for a different purpose (`("eval", "review", "fix", "verify")`, the report table's columns), and `verify` is never a `--phase-detect` answer. The two lists are correct precisely because they differ; making them match breaks the monitor's phase display.
- Everything here keeps its current `clean-fix` / `clean_fix` naming; Phases 8 through 10 rename.

**Acceptance gate:**
- **Target current-behavior claims, never the historical record.** Two gates below would otherwise reject text this phase deliberately preserves, and a gate that cannot go quiet is a gate nobody can pass.
- `grep -rniE "cargo.clean|warmup|4:00 AM|\[build\]" scripts/clean-fix/README.md` returns nothing. `nightly` is checked separately, because the **Reliability guards (the rg-hang)** section records that on 2026-06-02 a nightly run wedged for 12+ hours — a dated incident that happened, which the Spec above says to leave alone. So: `grep -niE "nightly" scripts/clean-fix/README.md` matches only inside that section, and every match reads as history rather than a claim about how the pipeline runs now. Confirm each one; do not rewrite them to satisfy the grep.
- `grep -rn "clean-fix" config/README.md config/lint.conf` returns nothing.
- `grep -rn "\[build\]\|build_add\|build_already" commands/ scripts/ docs/as-built/ README.md CLAUDE.md config/` returns nothing. **`docs/as-built/`, not `docs/`** — `docs/plans/` holds this plan, whose Spec and gate quote `[build]` and `build_add` as their subject matter, so a `docs/`-wide sweep matches this very Work Order and can never be satisfied. The two JSON key names are in this grep because a doc can promise a key without ever spelling the section: `make_a_worktree.md:108` tells the agent to report `build_add`, which `retarget_clean_fix.py` stopped emitting in Phase 3, and a `[build]`-only sweep walks straight past it.
- `grep -rn "nightly clean-fix\|nightly build set\|nightly cargo clean" commands/ scripts/ CLAUDE.md README.md` returns nothing.
- `grep -n "ensure('build'" scripts/new_rust_project/rust_generate.sh` returns nothing; `grep -n "ensure('projects'" scripts/new_rust_project/rust_generate.sh` still matches.
- `bash -n scripts/new_rust_project/rust_generate.sh` and `bash -n scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` exit 0.
- `grep -c "cargo-clean" projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` returns 0, and `MEMORY.md`'s Clean-fix pointer line no longer says "Two launchd jobs".
- `bash scripts/clean-fix/clean-fix.sh` still exits 0 with the style stages disabled — this phase touches no executable path, so a regression here means a doc edit landed in a script.

---

### Phase 8 — Rename identifiers, markers, and runtime paths · status: todo

#### Work Order

**Goal:** Every internal name the pipeline uses says `fix` rather than `clean-fix` — environment variables, the agent-registry family, log and report paths, and both on-disk markers — with existing marker data migrated in the same commit.

**Spec:**

This phase renames **contents**, not filenames. Phase 9 renames the files and directories; splitting them keeps each commit reviewable and each tree green.

**Environment variables** — rename across every definition and use. Two of the five get more than a de-branding, because dropping `CLEAN_` from them would leave a name that states nothing: `FIX_SCRIPT` names one of a dozen scripts in the pipeline without saying which, and `FIX_AGENT_DIR` reads as an agent's directory when it is the pipeline's own. This sweep rewrites every occurrence either way, so the better name costs nothing extra:

- `CLEAN_FIX_AGENT_ASSIGNMENTS_FILE` -> `FIX_AGENT_ASSIGNMENTS_FILE`
- `CLEAN_FIX_AGENT_DIR` -> `FIX_PIPELINE_DIR` (`agent_assignments.sh:7`; it holds the directory the pipeline scripts live in, resolved from `BASH_SOURCE`)
- `CLEAN_FIX_CONF_FILE` -> `FIX_CONF_FILE`
- `CLEAN_FIX_FORCE_STYLE_STAGES` -> `FIX_FORCE_STYLE_STAGES`
- `CLEAN_FIX_SCRIPT` -> `FIX_ORCHESTRATOR_PATH` (`clean-fix-trigger.sh:13`; it holds the orchestrator's absolute path and the pgrep guard matches on it)

Find every occurrence first (`grep -rn "CLEAN_FIX_" --include='*.sh' --include='*.py' --include='*.md' .`) and rename all of them together; a partial rename silently breaks stage loading. The `cf_*` bash function prefix is cosmetic — leave it alone.

**Agent registry family** `cleanfix` -> `fix`:
- `config/agents.conf` — `:21` `cleanfix=codex` becomes `fix=codex`; `:55` the section comment; `:56` `[cleanfix.codex]` and `:62` `[cleanfix.claude]` become `[fix.codex]` and `[fix.claude]`.
- Every `agents_resolve cleanfix.<stage>` call site — `scripts/clean-fix/agent_assignments.sh:93` and the `agent_exec.sh cleanfix.report` call in `clean-fix.sh:237`.
- `docs/as-built/agent-registry.md` — `:33` the family/sub-task table row, and `:140-147` the consumer rows, plus every `/agent cleanfix …` example.
- `commands/clean_fix.md` — every `/agent cleanfix <family>` and `/agent cleanfix.<stage>` reference.
- `scripts/clean-fix/clean-fix-usage.sh:41-42` — the two `/agent cleanfix …` usage lines. (Phase 5 deleted four usage rows above them; the file is 514 lines.)
- `scripts/clean-fix/README.md:18-19` — the file table describes assignments living under `[cleanfix.<family>]` and resolving through `agents_resolve cleanfix.<stage>`. Both spellings change with the family key; the surrounding prose is Phase 7's and Phase 10's.
- `scripts/agents/test_sync_codex_catalog.sh` — **this is a live test, not documentation.** Its `agents.conf` fixture heredocs (`:67,69,126,128,160,162,187,189`) and its four assertion strings (`:210,212,214,216`) spell the family key `cleanfix`. Rename it in all twelve places and re-run the test in this phase: the assertions match on the registry's own diagnostic text, so a renamed family with an un-renamed test fails on the mismatch — which is the test doing its job, not a flake to work around.

**Runtime brand strings — one atomic edit, five participants.** Completion recognition is not a matched pair any more. Phase 2 made it four things: the emitter (`clean-fix.sh:226`, writing `=== Clean-fix complete (…) ===`), `COMPLETE_RE` (`clean_fix_report_parse.py:96`) for that current wording, `HISTORICAL_COMPLETE_RE` (`:99-102`) for the retired `=== Clean-fix Rust clean + rebuild complete (…) ===`, and `match_completion_banner()` (`:104-106`), the one helper both call sites go through — `parse_log` (`:1183`) and `detect_current_phase` (`:1799`).

Phase 5 adds the fifth: `<Monitor/>`'s stop condition in `commands/clean_fix.md:236`, which Phase 5 repoints at `=== Clean-fix complete` because the banner it waited on before that had already been retired. It is not a parser and not a regex, which is exactly why it gets missed — but it is the line that decides when an armed monitor calls TaskStop, so leaving it on the superseded wording strands the monitor watching a run that has already ended. It moves with the emitter, in this commit.

In this commit: change the emitter to `=== Fix complete (…) ===`, change `COMPLETE_RE` to match it, repoint the command doc's monitor stop condition at `=== Fix complete`, and **add the wording being replaced to the historical set** rather than dropping it. After this phase there are three generations — `Fix complete` current, `Clean-fix complete` and `Clean-fix Rust clean + rebuild complete` historical — because the log directory carries roughly a day of logs across the change and every one of them must still read as a finished run. Keep all three behind `match_completion_banner()`: a second copy of this literal went stale once already, which is why that helper exists.

Splitting the emitter and the current regex across commits makes every report in between call a healthy run crashed. Phase 10's residual sweep names these compatibility constants as a permitted exception — they are the one place the retired brand is load-bearing rather than left over.

**Runtime paths**
- Log directory `~/.local/logs/clean-fix/` -> `~/.local/logs/fix/`, and the legacy single-file symlink `~/.local/logs/clean-fix.log` -> `~/.local/logs/fix.log`. **Migrate the existing directory**: `mv ~/.local/logs/clean-fix ~/.local/logs/fix` and remove the stale `~/.local/logs/clean-fix.log` symlink, so `/clean_fix report` and `list` keep seeing history.
- **Quiesce the job before the directory moves.** `com.natemccoy.style-fix` is loaded and fires every 600 seconds, so a run can begin at any point during this phase. One that is mid-append when the directory moves out from under it either loses its output or recreates `~/.local/logs/clean-fix/` behind the migration, and the phase then passes its greps while the old path is quietly back. The order is: `launchctl bootout gui/$(id -u)/com.natemccoy.style-fix` (unsandboxed), confirm no orchestrator is running with the same `pgrep -f` guard the trigger uses, migrate and edit, then `bash scripts/clean-fix/setup.sh` to bootstrap it back and confirm it reports a reload. Leaving the job booted out silently stops the whole pipeline, so verify the reload before this phase reports done.
- **Own every reader of that directory in the same commit.** Verified at plan time, these are all of them: `clean-fix.sh:34` (`LOG_DIR`) and `:36` (`LEGACY_LOG`); `clean_fix_report_parse.py:27` (`LOG_DIR`) and its `:7` usage docstring; `style-fix-monitor.py:38` (`LOG_DIR` — the monitor waits on this directory, so a missed rename leaves it watching a path nothing writes to); `style-fix-manual.sh:3,25` (`LOG_DIR` — a manual run would otherwise recreate the old directory the moment someone uses it); `report-render.md:11`; and `commands/style_eval.md:334`. `commands/clean_fix.md` names the path at `:65,75,164-165,263` and is already in this phase's Files.
- **Log filenames follow the directory.** The orchestrator writes `clean-fix-YYYYMMDD-HHMMSS.log` (`clean-fix.sh:35`) and `style-fix-manual.sh` writes `style-fix-manual-*.log`. Rename the orchestrator's prefix to `fix-YYYYMMDD-HHMMSS.log` and leave the manual prefix alone (it is named for the surviving style job).
- **Retention is the glob that must change; enumeration already covers both eras.** Verified against the live parser: `clean_fix_report_parse.py` enumerates with `LOG_DIR.glob("*.log")` at `:338` and `:1912`, which matches any filename and therefore reads migrated `clean-fix-*` and new `fix-*` logs alike with no edit. **Do not "fix" those two lines** — narrowing an era-agnostic glob to a branded one is how the migrated history gets stranded. What genuinely needs the two-era treatment is `clean-fix.sh:49`, `find "$LOG_DIR" -name 'clean-fix-*.log' -mmin +"$RUN_LOG_RETENTION_MINUTES" -delete`, which prunes by name: after the prefix rename it must cover the new `fix-*` names *and* the migrated `clean-fix-*` files, which keep their old names after the `mv`. A glob matching only one either strands the history or never prunes it — say in the code comment which of the two patterns each branch is for. `commands/clean_fix.md:82,173` name the filename pattern in prose and follow the prefix. Leave `clean-fix.sh:50`'s `style-fix-manual-*.log` line alone.
- Report file `/tmp/clean-fix-report.txt` -> `/tmp/fix-report.txt` — `clean-fix.sh`'s `REPORT_FILE` and `docs/as-built/agent-registry.md:142`. **Not `commands/clean_fix.md`:** Phase 5 removed that path from the doc, so a rename sweep expecting to find it there will report a miss that is not one.
- Leave `/tmp/style-fix-stdout.log` and `/tmp/style-fix-stderr.log` alone; they are named for the surviving launchd job and are already correct.

**Skip marker** `#CLEAN_FIX_SKIP#` -> `#FIX_SKIP#`:
- Readers — all four, verified at plan time: `scripts/clean-fix/phase_skip.py:28` (`MARKER`, and its module docstring at `:8-9`), `scripts/clean-fix/project_add.py:19` (`MARKER`), `scripts/clean-fix/clean-fix-usage.sh:8` (`MARKER`), and the description in `commands/clean_fix.md`. `project_add.py` uses the marker to tell a deliberately skipped entry from an absent one; leaving it on the old spelling makes it read every skipped project as missing and insert a duplicate active entry beside it. It changes and is tested in this commit with the rest.
- **Migrate the data in the same commit.** Any line already commented out in `clean-fix.conf` carries the old marker; rewrite those occurrences in the conf too. Skipping this leaves temporarily-skipped entries invisible to `enable-all`, which silently strands them.

**Project marker** `.clean-fix-project` -> `.fix-project`:
- Writers and readers: `scripts/clean-fix/style-fix-worktrees.sh:630,635-637` (writes the file and adds it to `.git/info/exclude`), `scripts/clean-fix/style_history.py:462` (`PROJECT_MARKER`), `scripts/clean-fix/project_rename.py:303` (the `*_style_fix/.clean-fix-project` glob), `scripts/clean-fix/clean_fix_report_parse.py:1237`, `scripts/worktree_delete/perform_deletion.sh:40,46-47`, and `commands/style_fix_review.md:229-233`.
- **No data migration is needed**: verified at plan time that no `~/rust/*_style_fix` worktree exists. Re-verify with `ls -d ~/rust/*_style_fix` before editing. If one has appeared since, rename its marker file as part of this phase. Stale `.clean-fix-project` entries left in any repo's `.git/info/exclude` are harmless and need no cleanup.

**Files:**
- `scripts/clean-fix/clean-fix.sh` — env vars, `LOG_DIR`, `LEGACY_LOG`, `LOG_FILE` prefix, the `:49` retention glob, `REPORT_FILE`, `agent_exec` family
- `scripts/clean-fix/clean-fix-trigger.sh` — `CLEAN_FIX_SCRIPT` -> `FIX_ORCHESTRATOR_PATH` at `:13` and the `pgrep -f` guard at `:15` that matches on its value
- `scripts/clean-fix/agent_assignments.sh` — env vars and `agents_resolve fix.<stage>`
- `scripts/clean-fix/clean-fix-usage.sh` — `MARKER`, env vars, `/agent fix …` usage lines
- `scripts/clean-fix/phase_skip.py` — `MARKER`
- `scripts/clean-fix/project_add.py` — `MARKER`
- `scripts/clean-fix/clean_fix_report_parse.py` — `LOG_DIR`, `COMPLETE_RE`, project marker. Its `*.log` enumerations need no edit; see the retention bullet in Spec
- `scripts/clean-fix/style-fix-monitor.py` — `LOG_DIR`
- `scripts/clean-fix/style-fix-manual.sh` — `LOG_DIR` and its header comment
- `scripts/clean-fix/report-render.md` — the `--latest-log` path
- `commands/style_eval.md` — the manual-run log path
- `scripts/clean-fix/style_history.py` — `PROJECT_MARKER`
- `scripts/clean-fix/style-fix-worktrees.sh` — marker write and `.git/info/exclude` entry
- `scripts/clean-fix/project_rename.py` — marker glob
- `scripts/clean-fix/style-eval-all.sh` — env vars, if referenced
- `scripts/clean-fix/style-eval-review-all.sh` — env vars, if referenced
- `scripts/worktree_delete/perform_deletion.sh` — marker read
- `commands/style_fix_review.md` — marker read
- `commands/clean_fix.md` — `/agent fix …`, marker name, report path
- `config/agents.conf` — family key and both family sections
- `docs/as-built/agent-registry.md` — family table row, consumer rows, report path
- `scripts/clean-fix/clean-fix.conf` — rewrite existing `#CLEAN_FIX_SKIP#` markers
- `scripts/agents/test_sync_codex_catalog.sh` — the `cleanfix` family key in its conf fixtures and assertions
- `scripts/clean-fix/README.md` — the `[cleanfix.<family>]` and `agents_resolve cleanfix.<stage>` references in the file table
- `scripts/clean-fix/tests/test_report_parse_phases.py` — add the three-generation completion-banner test alongside Phase 4's phase assertions

**Reservations:**
- file: `scripts/clean-fix/clean-fix.sh`
- file: `scripts/clean-fix/clean-fix-trigger.sh`
- file: `scripts/clean-fix/agent_assignments.sh`
- file: `scripts/clean-fix/clean-fix-usage.sh`
- file: `scripts/clean-fix/phase_skip.py`
- file: `scripts/clean-fix/project_add.py`
- file: `scripts/clean-fix/clean_fix_report_parse.py`
- file: `scripts/clean-fix/style-fix-monitor.py`
- file: `scripts/clean-fix/style-fix-manual.sh`
- file: `scripts/clean-fix/report-render.md`
- file: `commands/style_eval.md`
- file: `scripts/clean-fix/style_history.py`
- file: `scripts/clean-fix/style-fix-worktrees.sh`
- file: `scripts/clean-fix/project_rename.py`
- file: `scripts/clean-fix/style-eval-all.sh`
- file: `scripts/clean-fix/style-eval-review-all.sh`
- file: `scripts/worktree_delete/perform_deletion.sh`
- file: `commands/style_fix_review.md`
- file: `commands/clean_fix.md`
- file: `config/agents.conf`
- file: `docs/as-built/agent-registry.md`
- file: `scripts/clean-fix/clean-fix.conf`
- file: `scripts/agents/test_sync_codex_catalog.sh`
- file: `scripts/clean-fix/README.md`
- file: `scripts/clean-fix/tests/test_report_parse_phases.py`

**Constraints from prior phases:**
- Phases 1 through 7 removed the clean capability entirely; every file listed here is at its post-removal state, so a rename sweep will not hit deleted code.
- `commands/` is a protected path — use Edit/Write.
- The agent registry resolves through `scripts/agents/agents_config.sh` and launches through `scripts/agents/agent_exec.sh`; neither is renamed by this plan. Only the family **key** changes, so `agents_resolve` and `agent_exec` call sites change their argument, not their name.
- `docs/as-built/agent-registry.md:174` records that the pipeline scripts run under `#!/bin/bash` (3.2) and the registry scripts under `#!/usr/bin/env bash`. Do not change any shebang.
- **The helper scripts' domain types are out of scope for this phase — decided, not overlooked.** `SectionResult`, `Plan`, `PlannedMove`, `PlannedMarker`, `DetectResult`, and `CommitResult` are named for representation rather than role, and `Project.workspace_root`, `Plan.pending_path`, and `DetectResult`'s boolean-plus-`kind`-plus-empty-strings shape use optionals and flags where a tagged type belongs. All of it is real and all of it is recorded as next items. None of it lands here: this phase already rewrites identifiers across a broad file set, and a type refactor riding along turns a mechanical sweep into a design change nobody can review as either one. Rename identifiers and paths only. (The count is left unstated on purpose — it drifted once already as the sweep was refined, and a stale number in a rationale invites someone to trust it.)
- Phase 3 left `phase_skip.py` with `PASS_LABEL = "style"` (`:32`) feeding its six user-visible messages, and made the CLI's scope token optional. `PASS_LABEL` names the *style pass*, not the clean-fix brand — it is not part of this rename and its messages must stay byte-identical through this phase.
- Phase 2 rewrote `clean-fix-trigger.sh` down to a guard plus an `exec`: `CLEAN_FIX_SCRIPT` is now at `:13`, the `pgrep -f` guard at `:15`, and the `exec` at `:19`. Its idle gate and scope argument are gone.
- Phase 2 moved `clean-fix.sh`'s `LOG_DIR` to `:34`, `LEGACY_LOG` to `:36`, the completion banner to `:226`, and `REPORT_FILE` to `:231`; the file is 247 lines. `clean_fix_report_parse.py` is 1986 lines after Phase 4, and every offset into it in this Work Order was re-derived against that file — re-grep anyway rather than trusting a recorded number.
- Phase 4 cut the parser's phase model to four: `PHASES` is now `("eval", "review", "fix", "verify")` (`:43`) and `PhaseStats` (`:193-202`) no longer carries `processed` or `warnings`. `parse_clean_phase()` and `parse_warmup_phase()` are gone, and `MONITOR_FILTER_REGEX` (`:36`) no longer lists `CLEAN`, `BUILD`, `MEND`, `DONE`, or the `WARMUP` verbs. Rename identifiers only — do not reopen the phase model.
- **Phase 5 broadened `MONITOR_FILTER_REGEX`'s `=== ` alternative and it must not be re-narrowed.** It was `^=== `, anchored at line start, while `clean-fix.sh` writes the completion banner through `log()`, which prefixes `%Y-%m-%d %H:%M:%S` — so the live monitor's `grep -E` dropped the exact line `<Monitor/>`'s stop condition waits for, and an armed monitor never stopped. It is now `(^|[[:space:]])=== `, matching how every other alternative in the same regex was already anchored, and the constant spans `:36-41` rather than the single line `:36` the Delegation Context records. `=== Fix complete (…) ===` is still `=== `-prefixed and still written through `log()`, so this phase's banner rename is already covered — verified directly against a real orchestrator log. A rename sweep that "tidies" the anchor back to `^=== ` silently restores the defect, and no test would catch it.
- **The parser's own domain types are deferred to next-items 1-4 and must keep their current shape through this phase.** `Cell`, `ParseResult`, and `Warning` are named for representation rather than role; `ParseResult.running` reuses `Warning` for a non-warning; phase state and project status are free-form `str`; and a "no reason" is encoded as an empty string. All of it is real, all of it is recorded, and none of it lands here — a rename sweep that also re-tags a domain type is reviewable as neither. Rename identifiers and paths only.
- Phase 4 added `scripts/clean-fix/tests/fixtures/six-phase-run.log`, which deliberately contains `CLEAN:`, `BUILD:`, `MEND:`, `DONE:`, every `WARMUP` verb, and the retired completion banner. It is a captured historical log — **data, not code** — and renaming any string inside it destroys the regression oracle. This phase's residual-brand greps already exclude it; keep it that way and do not edit the file.
- Phase 4 added `scripts/clean-fix/tests/test_report_parse_phases.py`, which asserts `PHASES` and an exact four-phase cell baseline. A rename that reaches `PHASES` or a `Cell` field makes it fail, which is the intended alarm — update the test in the same commit rather than working around it.

**Acceptance gate:**
- **Scope every residual grep away from the plan's own text.** This plan doc and its sibling notes under `docs/plans/` quote every old name on purpose, so a bare `grep -rn … .` matches them forever and can never go quiet. Add `--exclude-dir=plans` alongside the existing exclusions to each of the three greps below, and read "no references" as "no references in maintained implementation, command, configuration, or product documentation" — never in the planning record.
- `grep -rn "CLEAN_FIX_" --include='*.sh' --include='*.py' --include='*.md' . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "cleanfix" --include='*.sh' --include='*.py' --include='*.md' --include='*.conf' . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "clean-fix-report.txt\|#CLEAN_FIX_SKIP#\|\.clean-fix-project" . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "logs/clean-fix" . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing — this is the check that catches a missed log-directory consumer.
- `grep -n "Clean-fix complete" scripts/clean-fix/clean-fix.sh` returns nothing and `grep -n "Fix complete" scripts/clean-fix/clean-fix.sh` matches.
- **Do not assert the retired wording is absent from the parser.** A source-wide `'Clean-fix complete' not in src` check fails here by design, because the historical set is exactly where that literal now belongs. Assert behavior instead, and put it in **`scripts/clean-fix/tests/test_report_parse_phases.py`** — the file Phase 4 created for exactly this file's regressions — rather than an inline `python3 -c` that vanishes with the terminal: `match_completion_banner()` returns a match for all three generations — `=== Fix complete (1m 2s) ===`, `=== Clean-fix complete (1m 2s) ===`, and `=== Clean-fix Rust clean + rebuild complete (1m 2s) ===` — and `None` for an ordinary log line.
- A run's own log proves the pair moved together: after the end-to-end run below, `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` reports it complete rather than crashed.
- `python3 scripts/clean-fix/project_add.py` against a temp conf carrying a `#FIX_SKIP#`-commented entry reports that entry as skipped rather than adding a duplicate.
- `bash scripts/clean-fix/style-fix-manual.sh` (or reading its `LOG_DIR`) writes under `~/.local/logs/fix/`, and `~/.local/logs/clean-fix` is not recreated by any script in the repository.
- `bash -c 'source scripts/clean-fix/agent_assignments.sh; cf_print_agent_assignments'` exits 0 and resolves all three stages through the `fix` family.
- `bash scripts/agents/agents_config.sh` resolution for `fix.style_eval` succeeds (invoke however the other registry tests do; `scripts/agents/test_agents_config.sh` is the reference).
- `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass. The third is the one that fails if the family rename reached `config/agents.conf` but not the test's own fixtures and assertions.
- **Snapshot the filenames; do not count them.** Before migrating, save `ls ~/.local/logs/clean-fix > /tmp/fix-log-manifest.txt`; afterwards `ls ~/.local/logs/fix` must contain every name in that manifest plus whatever this phase's own runs added. A hard total is wrong by the time it is read — the corpus was 146 when the plan was written and 145 when phase 2 finished, and the ten-minute job keeps appending while retention keeps pruning. `ls ~/.local/logs/clean-fix` reports no such directory.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --list` enumerates both the migrated `clean-fix-*` logs and any new `fix-*` log, proving the retention and enumeration globs cover both naming eras, and reports every migrated log as complete rather than in-progress, proving all three banner generations still resolve.
- `basedpyright scripts/clean-fix/ scripts/make_a_worktree/` prints `0 errors, 0 warnings, 0 notes`.
- `bash scripts/clean-fix/clean-fix.sh` exits 0 with the style stages disabled, writes its log under `~/.local/logs/fix/` with a `fix-` prefix, and `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` finds and parses it.
- **Add a durable monitor-filter regression to `scripts/clean-fix/tests/test_report_parse_phases.py`, beside the three-generation banner test.** It asserts that `re.search`-equivalent matching of `MONITOR_FILTER_REGEX` accepts a **timestamped** `2026-01-01 00:00:00 === Fix complete (1m 2s) ===` and an untimestamped `=== Done: 1 created, 0 failed ===`, and rejects an ordinary ` Compiling serde v1.0` line. Nothing has ever covered this constant: a line-start anchor survived two banner changes unnoticed and was found only by a blind reviewer in Phase 5. The banner is read by three consumers — the live monitor's `grep -E`, `parse_log()` at `:1183`, and `detect_current_phase()` at `:1799` — and this phase renames it while owning all three, which is precisely when a silent anchor regression would ship. An inline `python3 -c` is not acceptable here; it vanishes with the terminal.
- `grep -n "Clean-fix complete" commands/clean_fix.md` returns nothing and `grep -n "Fix complete" commands/clean_fix.md` matches at `<Monitor/>`'s stop condition — the check that the fifth participant moved with the emitter instead of being left on Phase 5's now-superseded wording.
- **Run every test in the directory, not the one file that used to be there:** `for t in scripts/clean-fix/tests/test_*.py; do python3 "$t" || exit 1; done` prints `OK` for each. Phase 4 added a second test file, and naming only the older one is how a parser regression ships green.

---

### Phase 9 — Rename the files and directories · status: todo

#### Work Order

**Goal:** The pipeline lives at `scripts/fix/`, the command is `/fix`, and the launchd job runs the renamed trigger.

**Spec:**

Use `git mv` for every rename so history follows. `commands/` is a protected path — `git mv` operates through git rather than a raw filesystem write, but if it is refused, use Write to the new path plus `git rm` on the old one.

**Renames**
- `commands/clean_fix.md` -> `commands/fix.md`
- `scripts/clean-fix/` -> `scripts/fix/` (the whole directory, including `tests/` and `docs/`)
- inside it: `clean-fix.sh` -> `fix.sh`; `clean-fix.conf` -> `fix.conf`; `clean-fix-usage.sh` -> `fix-usage.sh`; `clean-fix-trigger.sh` -> `fix-trigger.sh`; `clean_fix_report_parse.py` -> `fix_report_parse.py`; `clean-fix-style-flow.dot` -> `fix-style-flow.dot`; `clean-fix-style-flow.svg` -> `fix-style-flow.svg`
- `scripts/make_a_worktree/retarget_clean_fix.py` -> `scripts/make_a_worktree/retarget_fix.py`

**Then update every path reference.** These are the known ones; sweep for more:
- `scripts/fix/tests/test_report_parse_phases.py:21` — `from clean_fix_report_parse import PHASES, ParseResult, parse_log` becomes `from fix_report_parse import …`. This is the **only Python import of the renamed module** anywhere in the repository; every other reference invokes it as a script path. Make this edit deliberately rather than leaving it to the residual grep: the test inserts `parents[1]` on `sys.path` and imports by module name, so a directory rename alone will not save it — it raises `ModuleNotFoundError` and takes the parser regression suite down with it, in the same commit that moves the file it guards.
- `scripts/fix/fix.sh` — `SCRIPT_DIR` is derived, but the script names `agent_assignments.sh`, `backpopulate_settings.py`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-worktrees.sh`, and `report-render.md` relative to it (those keep their names), plus the absolute `$HOME/.claude/scripts/clean-fix/report-render.md` and `$HOME/.claude/scripts/lint/lint` paths — fix the clean-fix one.
- `scripts/fix/fix-trigger.sh` — `CLEAN_FIX_SCRIPT` was renamed in Phase 8; its **value** still points at `.../clean-fix/clean-fix.sh`. Update to `$HOME/.claude/scripts/fix/fix.sh`. The pgrep guard matches on this path, so it must be exact.
- `scripts/fix/com.natemccoy.style-fix.plist` — `ProgramArguments` names `/Users/natemccoy/.claude/scripts/clean-fix/clean-fix-trigger.sh`. Update to the new absolute path. The launchd **label** stays `com.natemccoy.style-fix`.
- `scripts/fix/setup.sh` — `SCRIPT_DIR` is derived; verify no absolute clean-fix path remains.
- `pyrightconfig.json:12` — `{ "root": "scripts/clean-fix", "extraPaths": ["scripts/clean-fix"] }` becomes `scripts/fix`.
- `.claude/settings.local.json` — 12 entries at `:7,11,12,23,53,55-59,61,75` hardcode `scripts/clean-fix/…`. Update each to `scripts/fix/…`, including the renamed script basenames. **`"Bash(pkill -f 'clean-fix.sh')"` must become `"Bash(pkill -f 'fix.sh')"`** or the kill permission stops matching the process.
- Every remaining `scripts/clean-fix/` path string in `commands/`, `scripts/`, `docs/`, and `README.md`. **This phase owns every path-dependent caller, not just the ones inside the renamed tree** — the residual sweep in Phase 10 is a prose pass, and a stale path is a broken call, not a wording problem. Verified at plan time, the callers outside `scripts/fix/` are: `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, and `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`. Sweep for more rather than trusting the list. `scripts/agents/test_sync_codex_catalog.sh` is deliberately **not** on it: it holds no `scripts/clean-fix` path at all — only the `cleanfix` family key, which Phase 8 already renamed. It stays a verification command here, not an edit target.

**Re-render the diagram in this commit.** Phase 6 rebuilt the dot source's content; renaming `clean-fix-style-flow.dot` and `.svg` here leaves `render-flow.py` pointing at basenames that no longer exist. Run `cd scripts/fix && python3 render-flow.py`, and if the script hardcodes either basename, fix it here — a rename that leaves the renderer broken is this phase's defect, not the next phase's cleanup. Change no diagram content.

**Prove the generated command skill followed the rename.** `commands/clean_fix.md` is the source for the live Codex skill at `~/.codex/skills/generated-from-claude/clean_fix/SKILL.md`, and the synchronizer removes a stale skill directory when its source command disappears. **The synchronizer is `scripts/claude_to_codex/run_sync.sh`**, which drives `scripts/claude_to_codex/sync.py` over `~/.claude/commands` and writes `~/.codex/skills/generated-from-claude/<command>/SKILL.md` (`sync.py:41`). It is not anything under `scripts/agents/`: `scripts/agents/sync_codex_catalog.sh` materializes Codex's *model catalog* into `agents.conf`, and `scripts/agents/test_sync_codex_catalog.sh` tests that — a different subsystem that shares a word. After `commands/clean_fix.md` becomes `commands/fix.md`, run `bash scripts/claude_to_codex/run_sync.sh` and confirm `generated-from-claude/fix/SKILL.md` exists and `generated-from-claude/clean_fix/` does not. Nothing else in the plan looks at this surface.

**Reload launchd** after the plist changes: `bash scripts/fix/setup.sh` detects the changed plist and re-bootstraps the agent. Run it and confirm it reports a reload rather than "Already set up".

`scripts/fix/tests/test_style_fix_prompt_comments.py` resolves its target as `Path(__file__).resolve().parents[1] / "style-fix-worktrees.sh"` — relative to itself, so the directory rename needs no edit there.

**Files:**
- `commands/clean_fix.md` — removed by the rename; its content moves to commands/fix.md
- `commands/fix.md` — the command doc under its new name
- **Every `/clean_fix` invocation moves in this commit, not in Phase 10.** `commands/style_eval.md:347` invokes `/clean_fix` with `monitor` and is the only external caller; renaming `commands/clean_fix.md` without it leaves this phase's own checkpoint with a slash command that resolves to nothing. A command's name and its call sites are one atomic change, so they land together. Phase 10 keeps the `/clean_fix` **wording** sweep in `commands/new_rust_project.md` and `commands/bevy_migration_plan.md`, which is prose about the pipeline rather than a live invocation.
- `scripts/clean-fix` — the whole directory is renamed away, tests/ and docs/ included
- `scripts/fix` — the pipeline directory under its new name, with seven files renamed inside it
- `scripts/make_a_worktree/retarget_clean_fix.py` — removed by the rename
- `scripts/make_a_worktree/retarget_fix.py` — the redirect helper under its new name
- `pyrightconfig.json` — execution-environment root
- `.claude/settings.local.json` — 12 permission entries
- `scripts/fix/render-flow.py` — dot/svg basenames if hardcoded
- `scripts/fix/tests/test_report_parse_phases.py` — the `from clean_fix_report_parse import …` line, the repository's only import of the renamed module
- `scripts/fix/fix-style-flow.svg` — re-rendered after the rename, never hand-edited
- `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` — `scripts/clean-fix/` path strings. **`scripts/lint/{invoke.sh,lint,lint_config.sh,scope.py}`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, and `scripts/agents/clean_agents_conf.sh` are deliberately absent**: verified against the tree, each carries brand wording only and not one `scripts/clean-fix/` path, so they are Phase 10's and listing them here would reserve seven files this phase never edits.
- `CLAUDE.md`, `README.md`, `config/README.md`, `docs/as-built/agent-registry.md` — `scripts/clean-fix/` path strings in prose and file tables
- `commands/focused_eval.md`, `commands/lint_config.md`, `commands/make_a_worktree.md`, `commands/style_delete.md`, `commands/style_fix_review.md`, `commands/style_rename.md`, `commands/style_usage.md`, `commands/worktree_delete.md` — `scripts/clean-fix/` path strings
- `commands/style_eval.md` — both the `scripts/clean-fix/` paths **and** the `/clean_fix` invocation at `:347`, the repository's only external caller of the command this phase renames

**Reservations:**
- file: `commands/clean_fix.md`
- file: `commands/fix.md`
- tree: `scripts/clean-fix`
- tree: `scripts/fix`
- file: `scripts/make_a_worktree/retarget_clean_fix.py`
- file: `scripts/make_a_worktree/retarget_fix.py`
- file: `pyrightconfig.json`
- file: `.claude/settings.local.json`
- file: `scripts/worktree_delete/perform_deletion.sh`
- file: `CLAUDE.md`
- file: `README.md`
- file: `config/README.md`
- file: `docs/as-built/agent-registry.md`
- file: `commands/focused_eval.md`
- file: `commands/lint_config.md`
- file: `commands/make_a_worktree.md`
- file: `commands/style_delete.md`
- file: `commands/style_fix_review.md`
- file: `commands/style_rename.md`
- file: `commands/style_usage.md`
- file: `commands/worktree_delete.md`
- file: `commands/style_eval.md`
- file: `scripts/new_rust_project/rust_generate.sh`
- file: `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`

**Constraints from prior phases:**
- Phase 8 renamed `CLEAN_FIX_SCRIPT` (`clean-fix-trigger.sh:13`) to **`FIX_ORCHESTRATOR_PATH`** but left its **value** pointing at the old path — this phase fixes the value. The name is `FIX_ORCHESTRATOR_PATH`, not `FIX_SCRIPT`: Phase 8 rejected the bare de-branding because `FIX_SCRIPT` names one of a dozen scripts in the pipeline without saying which. Do not reintroduce it. The same is true of `LOG_DIR`, which Phase 8 already repointed at `~/.local/logs/fix/`; that path is a runtime directory, not a script path, and needs no further change here.
- Phase 8 renamed the agent-registry family to `fix`, so nothing in `config/agents.conf` depends on the directory name.
- **The parser's own domain types are deferred to next-items 1-4 and must keep their current shape through this phase either.** `Cell`, `ParseResult`, and `Warning` are named for representation rather than role; `ParseResult.running` reuses `Warning` for a non-warning; phase state and project status are free-form `str`; and a "no reason" is encoded as an empty string. All of it is real, all of it is recorded, and none of it lands here — a rename sweep that also re-tags a domain type is reviewable as neither. Rename identifiers and paths only.
- Phase 1 reduced `setup.sh` to one plist, so the reload touches a single agent.
- The launchd label `com.natemccoy.style-fix` and the plist filename `com.natemccoy.style-fix.plist` deliberately keep their names — they describe the surviving style job accurately, and renaming a loaded label risks a bootstrap mistake for no gain. Same for `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style_history.py`, and `style-fix-monitor.py`.
- **Re-derive this phase's file set before starting; do not trust the Files list alone.** Phase 7 sweeps repository documentation and will delete some `scripts/clean-fix/` references outright, so the list above is the set that held old paths when Phase 5 closed. Run this phase's own path grep first and reconcile: a file the grep no longer matches needs no edit, and a file it matches that is not listed still has to be renamed, because the gate is repository-wide and does not care what the Files list predicted.
- `launchctl` must run unsandboxed.

**Acceptance gate:**
- `ls scripts/fix/fix.sh scripts/fix/fix.conf scripts/fix/fix-trigger.sh scripts/fix/fix-usage.sh scripts/fix/fix_report_parse.py` all exist; `ls scripts/clean-fix` reports no such directory.
- `ls commands/fix.md` exists; `ls commands/clean_fix.md` reports no such file.
- `grep -rn "/clean_fix" commands/ --exclude-dir=plans` returns nothing except the pipeline **wording** in `commands/new_rust_project.md` and `commands/bevy_migration_plan.md` that Phase 10 owns — no live invocation of the removed command survives. `grep -n "/fix" commands/style_eval.md` matches at its monitor hand-off.
- `git status --porcelain` shows the renames as `R` entries, not as delete-plus-add.
- `grep -rn "scripts/clean-fix\|clean-fix.sh\|clean_fix_report_parse\|clean-fix.conf\|clean-fix-trigger\|clean-fix-usage\|clean-fix-style-flow\|retarget_clean_fix" . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing. The `docs/plans/` exclusion is required: this plan quotes every old path by design and would otherwise keep the grep permanently non-empty.
- `cd scripts/fix && python3 render-flow.py` exits 0 and writes `fix-style-flow.svg`; `git diff --stat scripts/fix/fix-style-flow.svg` shows it changed, and the diagram's node and edge set is identical to the one Phase 6 produced.
- `bash scripts/claude_to_codex/run_sync.sh` exits 0; afterwards `ls ~/.codex/skills/generated-from-claude/fix/SKILL.md` exists and `ls -d ~/.codex/skills/generated-from-claude/clean_fix` reports no such directory. Run **that** script — `scripts/agents/test_sync_codex_catalog.sh` regenerates a model catalog, not a skill, and passing it proves nothing about this surface.
- `bash scripts/lint/lint --help` (or the nearest cheap invocation of each renamed caller) exits 0, proving no script is left pointing at `scripts/clean-fix/`.
- `python3 -c "import json,sys; json.load(open('.claude/settings.local.json'))"` exits 0, and `grep -c "scripts/clean-fix" .claude/settings.local.json` returns 0.
- `grep -n "pkill -f 'fix.sh'" .claude/settings.local.json` matches.
- `basedpyright scripts/fix/ scripts/make_a_worktree/` prints `0 errors, 0 warnings, 0 notes` (proving `pyrightconfig.json` still resolves the execution environment).
- `bash -n scripts/fix/fix.sh scripts/fix/fix-trigger.sh scripts/fix/fix-usage.sh scripts/fix/setup.sh` exits 0.
- `bash scripts/fix/setup.sh` exits 0 and reports the agent reloaded; `launchctl list | grep style-fix` (unsandboxed) still shows `com.natemccoy.style-fix`.
- `bash scripts/fix/fix-trigger.sh` exits 0 and executes the pipeline (or exits 0 on the pgrep guard if a run is already in flight).
- `for t in scripts/fix/tests/test_*.py; do python3 "$t" || exit 1; done` prints `OK` for each. Both test files must run: Phase 4 added the parser regression test, and it is the one that would catch a directory rename breaking how the parser resolves its own paths.
- `bash scripts/agents/test_sync_codex_catalog.sh` passes — it is untouched by this phase and must stay green across the directory rename.

---

### Phase 10 — Sweep the remaining cross-references and verify end to end · status: todo

#### Work Order

**Goal:** Every surviving `clean_fix`, `clean-fix`, or `cleanfix` reference outside session transcripts belongs to one of the four sanctioned compatibility survivals named in Constraints, and one full pipeline run plus a rendered report proves the renamed system works.

The Goal is deliberately *not* "no match remains". Four classes of match must survive this phase — the retired launchd label, the two historical completion banners, the captured pre-change fixture, and the test assertions quoting it — and a goal stated as absolute absence contradicts the Constraints below and invites the sweep to delete exactly the compatibility this plan built. The sweep's job is to leave no **unsanctioned** match.

**Spec:**

**Command cross-references** (all under the protected `commands/` path — Edit/Write only). Each of these names the `/clean_fix` command or the clean-fix brand in prose; rewrite every occurrence as `/fix` and "fix". **Do not plan on rewriting `scripts/clean-fix/` paths here.** Phase 9's acceptance gate requires every one of them to be gone already, so this phase inherits a tree with none left; if one turns up, it is a Phase 9 defect caught late — repair it here and say so in the report, rather than treating path rewriting as this phase's work:
`commands/style_fix_review.md`, `commands/style_usage.md`, `commands/style_eval.md`, `commands/focused_eval.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/clippy.md`, `commands/lint_config.md`, `commands/add_banned_word.md`, `commands/style_delete.md`, `commands/style_rename.md`, `commands/validate_and_push.md`.

Also update `commands/fix.md` itself — its own self-references (`/clean_fix run`, `/clean_fix report`, `/clean_fix monitor`, the `<DetectLog/>` paths, and the frontmatter `description`) must all say `/fix`.

**This phase is the prose and verification sweep.** Phase 9 already repointed every executable path reference, inside the renamed tree and outside it, and re-rendered the diagram. What is left here is wording — sentences that still say `/clean_fix` or "clean-fix" where nothing breaks but the text is now wrong — plus the end-to-end run that proves the whole plan. If this phase finds a *path* that still resolves to the old location, that is a Phase 9 defect being caught late: fix it here and say so, rather than filing it forward.

**Remaining docs and scripts** — `README.md:14`, `scripts/fix/README.md` (title, every path and command in the file table, the pipeline-flow diagram, the Evaluation State section, and the flowchart-generation instructions), `docs/as-built/agent-registry.md`, `config/README.md`, `config/lint.conf`, `scripts/fix/docs/candidate-enumeration-design.md`, `scripts/fix/style-fix-manual.sh`, `scripts/fix/style-eval-review-prompt.md`, `scripts/fix/rg-shim.sh`, `scripts/fix/setup.sh` (its header comment names the clean-fix agent), `scripts/fix/README.md`, and `CLAUDE.md`.

**The renamed core files still carry the brand in their prose and comments** — `fix.sh`, `fix-trigger.sh`, `fix-usage.sh`, `fix.conf`, `fix_report_parse.py`, `agent-assignments.conf`, `agent_assignments.sh`, `phase_skip.py`, `project_add.py`, `project_rename.py`, `style_history.py`, `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-monitor.py`, `report-render.md`, and `fix-style-flow.dot`. Their identifiers and paths were settled in Phases 8 and 9; what remains is comment and message text. Sweep the whole `scripts/fix/` tree rather than working from this list.

Note `scripts/agents/clean_agents_conf.sh` is named for *cleaning the agents conf file* — unrelated to clean-fix. Do not rename it; only correct any `clean-fix` path it contains.

**One match is a live bug, not a brand reference — repair it, do not rename it.** `scripts/fix/style-fix-worktrees.sh` carries `cargo +clean-fix fmt` three times: at `:820` (the `Step 8:` prompt heading), `:821`, and `:999`. The `+name` slot after `cargo` is a **toolchain selector**, and the installed toolchains are `stable`, `nightly`, and `1.96.0` — there has never been one called `clean-fix`. These lines predate this plan: `git blame` puts them at 2026-05-24 and 2026-06-14, collateral damage from the earlier `nightly` -> `clean-fix` directory rename, which swept `cargo +nightly fmt` along with everything else. They have been failing ever since, inside an agent prompt where the failure surfaces as a formatting step that quietly does nothing.

Replace all three with `cargo +nightly fmt`, keeping each line's surrounding arguments intact. A mechanical brand sweep would turn them into `cargo +fix fmt` and preserve the break under a new spelling — which is the same mistake that created them, committed a second time. This is the one place in the phase where the correct edit is not the rename.

**Memory** — rename `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` to `project_fix_10min_schedule.md`, update its body to say `/fix`, and update the pointer line and the `## Clean-fix` heading in `projects/-Users-natemccoy--claude/memory/MEMORY.md`.

**Re-render only if this phase changed the dot source.** Phase 9 already re-rendered after the rename. If a comment edit here touches `fix-style-flow.dot`, run `cd scripts/fix && python3 render-flow.py` again; otherwise leave the SVG alone.

**End-to-end verification** (the real acceptance for the whole plan):
1. Record `scripts/fix/agent-assignments.conf`'s checksum.
2. Run `bash scripts/fix/fix.sh run_once` — background it and wait for the notification; it dispatches real agents and takes minutes. **Do not touch the stage switches.** `run_once` exports `FIX_FORCE_STYLE_STAGES=1`, and all three stage scripts (`style-eval-all.sh:430`, `style-eval-review-all.sh:81`, `style-fix-worktrees.sh:211`) run when that variable is set regardless of their `enabled=` value. Disabling and restoring the switches around the run is a no-op that risks leaving the user's configuration changed if the phase is interrupted between the two edits.
3. Confirm a log appears under `~/.local/logs/fix/`, the run reaches the completion line, and the report renders to `/tmp/fix-report.txt`.
4. Confirm `agent-assignments.conf`'s checksum is unchanged — the override left the persistent configuration exactly as it found it.

**Files:**
- `commands/fix.md` — self-references and frontmatter description
- `commands/style_fix_review.md`, `commands/style_usage.md`, `commands/style_eval.md`, `commands/focused_eval.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/clippy.md`, `commands/lint_config.md`, `commands/add_banned_word.md`, `commands/style_delete.md`, `commands/style_rename.md`, `commands/validate_and_push.md` — `/fix` and `scripts/fix/`
- `scripts/fix/README.md` — full rewrite of paths and commands
- `scripts/fix/docs/candidate-enumeration-design.md`, `scripts/fix/style-fix-manual.sh`, `scripts/fix/style-eval-review-prompt.md`, `scripts/fix/rg-shim.sh` — path references
- `scripts/fix/render-flow.py` — dot/svg basenames if hardcoded
- `scripts/fix/fix-style-flow.svg` — regenerated
- `docs/as-built/agent-registry.md`, `config/README.md`, `config/lint.conf`, `README.md`, `CLAUDE.md` — remaining wording
- `commands/new_rust_project.md`, `commands/bevy_migration_plan.md` — `/clean_fix` wording Phase 7 left in place while it removed only the "nightly" claims
- `scripts/fix/style-fix-worktrees.sh` — wording, plus the three broken `cargo +clean-fix fmt` toolchain selectors at `:820,821,999`, which become `cargo +nightly fmt`
- `scripts/fix/` — a whole-tree comment and message sweep across the renamed core files, `setup.sh` header included
- `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `scripts/worktree_delete/perform_deletion.sh` — wording only; Phase 9 already fixed their paths. `scripts/agents/test_sync_codex_catalog.sh` is not here: Phase 8 renamed its `cleanfix` family key and it carries no other clean-fix spelling, so this phase only runs it
- `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md` — removed by the rename
- `projects/-Users-natemccoy--claude/memory/project_fix_10min_schedule.md` — the memory under its new name, rewritten to say /fix
- `projects/-Users-natemccoy--claude/memory/MEMORY.md` — heading and pointer line

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
- file: `commands/style_delete.md`
- file: `commands/style_rename.md`
- file: `commands/validate_and_push.md`
- tree: `scripts/fix`
- file: `commands/new_rust_project.md`
- file: `commands/bevy_migration_plan.md`
- file: `docs/as-built/agent-registry.md`
- file: `config/README.md`
- file: `config/lint.conf`
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
- file: `projects/-Users-natemccoy--claude/memory/project_clean_fix_10min_schedule.md`
- file: `projects/-Users-natemccoy--claude/memory/project_fix_10min_schedule.md`
- file: `projects/-Users-natemccoy--claude/memory/MEMORY.md`

**Constraints from prior phases:**
- Phase 9 moved everything to `scripts/fix/` and `commands/fix.md`, so every path written here is a post-rename path.
- Phase 8 renamed the registry family to `fix` and the runtime log directory to `~/.local/logs/fix/`; docs updated here must name those.
- Phase 6 removed the clean cluster from the dot source; this phase only re-renders after the file rename, and must not change the diagram's content.
- `commands/`, `CLAUDE.md`, and the memory directory are protected or gitignored — use Edit/Write throughout.
- The surviving `style-*` names are deliberate and stay: `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style_history.py`, `style-fix-monitor.py`, `style-fix-manual.sh`, the `com.natemccoy.style-fix` launchd label, `/tmp/style-fix-stdout.log`, and the `_style_fix` worktree suffix. Do not rename any of them.
- `~/rust/nate_style/.history/` is durable style state and is out of scope.
- **The parser's own domain types are deferred to next-items 1-4 and must keep their current shape through this phase.** `Cell`, `ParseResult`, and `Warning` are named for representation rather than role; `ParseResult.running` reuses `Warning` for a non-warning; phase state and project status are free-form `str`; and a "no reason" is encoded as an empty string. All of it is real, all of it is recorded, and none of it lands here — a rename sweep that also re-tags a domain type is reviewable as neither. Rename identifiers and paths only.
- `HISTORICAL_COMPLETE_RE` and `match_completion_banner()` in `fix_report_parse.py` carry the retired banner wording on purpose, so that logs written before the rename still read as finished runs. Phase 4's `tests/fixtures/six-phase-run.log` and `tests/test_report_parse_phases.py` carry it for the same kind of reason: the fixture *is* a pre-change log, and the test asserts on its exact strings. Together with `setup.sh`'s historical launchd label, these are the sanctioned survivals of the old brand; the acceptance gate below names all four as permitted exceptions rather than things to sweep. A grep-driven edit to any of them is a regression dressed as cleanup.

**Acceptance gate:**
- `grep -rn "clean_fix\|clean-fix\|cleanfix\|CLEAN_FIX\|Clean-fix" . --exclude-dir=projects --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=plans` returns **only matches belonging to the four sanctioned classes below, and nothing else**. Read the output and classify every line; an empty result is not the pass condition and would in fact mean the compatibility survivals were destroyed. The four classes are: `setup.sh`'s retired-agent cleanup block referencing the historical label `com.natemccoy.clean-fix`; the retired completion banners inside `fix_report_parse.py`'s `HISTORICAL_COMPLETE_RE` — `Clean-fix complete` and `Clean-fix Rust clean + rebuild complete` — which exist so migrated logs still parse as finished runs; `scripts/fix/tests/fixtures/six-phase-run.log`, which is a captured pre-change log and is **data, not code** — its `=== Starting clean-fix (scope: all) ===` banner, its `CLEAN:`/`BUILD:`/`MEND:`/`DONE:`/`WARMUP:` lines, and its retired completion banner are the whole reason the fixture proves anything; and the assertions in `scripts/fix/tests/test_report_parse_phases.py` that quote those same strings, including all three banner generations. Rewriting either of the last two to satisfy a grep destroys the regression oracle the plan built and leaves the highest-risk file uncovered. Confirm every surviving match falls into one of the four and nothing else; deleting any of them breaks reading the log history this plan deliberately preserved. The `plans` exclusion is deliberate and permanent — this plan and its siblings quote the old names as their subject matter, so the sweep's claim is about maintained implementation, command, configuration, and product documentation, never about the planning record.
- `grep -rn "clean_fix\|clean-fix" projects/-Users-natemccoy--claude/memory/` returns nothing.
- `bash -n` exits 0 on every touched shell script.
- `basedpyright scripts/fix/ scripts/make_a_worktree/ scripts/lint/ scripts/hooks/` prints `0 errors, 0 warnings, 0 notes`.
- `for t in scripts/fix/tests/test_*.py; do python3 "$t" || exit 1; done` prints `OK` for each — both the prompt-comment test and Phase 4's parser regression test.
- `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass.
- `cd scripts/fix && python3 render-flow.py` exits 0 and writes `fix-style-flow.svg`.
- `bash scripts/fix/fix-usage.sh` exits 0 and every command it prints reads `/fix …`.
- `grep -n "cargo +" scripts/fix/style-fix-worktrees.sh` shows `cargo +nightly fmt` and no `cargo +clean-fix` or `cargo +fix`; `rustup toolchain list` confirms `nightly` is installed while no `fix`-named toolchain exists.
- **End to end:** `bash scripts/fix/fix.sh run_once` completes without any change to the stage switches; a new `fix-*` log exists under `~/.local/logs/fix/`; `python3 scripts/fix/fix_report_parse.py --latest-log` reports the run **complete** and renders the four-column `Eval | Review | Fix | Verify` schema; and `/tmp/fix-report.txt` contains a rendered report. `scripts/fix/agent-assignments.conf` is byte-identical before and after.
- **Do not require populated cells from the live run.** The parser marks a phase present only after it sees that phase's progress markers, so a legitimate `run_once` over a tree with no eligible findings finishes with `Verify` showing `—` and `Fix` showing `SKIP:no-open-findings`. That is the pipeline working, and gating on populated cells makes this phase's outcome depend on whichever projects happen to have open findings that afternoon. The live run proves the renamed system executes and reports; **populated four-phase cell behavior is proven deterministically by Phase 4's fixture test**, which is why that fixture exists.
