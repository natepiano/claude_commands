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
  - `scripts/clean-fix/clean_fix_report_parse.py` — 2125 lines. `LOG_DIR` `:27`; `MONITOR_FILTER_REGEX` `:36`; `PHASES` `:44`; `COMPLETE_RE` `:97`; `HISTORICAL_COMPLETE_RE` `:100-103`; `match_completion_banner()` `:105-107`; `PhaseStats` `:199-203` (`processed`, `warnings`, `footer_ok`, `footer_fail`, `footer_total`); conf project map `:378`; phase-boundary detection `:661`; `parse_clean_phase()` `:744`; `parse_warmup_phase()` `:807`; `detect_current_phase()` backward walk `:1938`; `.clean-fix-project` marker `:1369`
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
  - `commands/clean_fix.md` — 351 lines. Dispatch `:17`; scopes `:21-42`; notes `:89-95`; add/rename `:115,143`; log detect `:171`; phase sentinels `:197`; monitor note `:230`; monitor stop condition `:244`; stage config `:289-314`; skip `:321-349`
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

### Phase 4 — Remove the clean and warmup phases from the report parser · status: todo

#### Work Order

**Goal:** `clean_fix_report_parse.py` models four phases (eval, review, fix, verify), a checked-in fixture log proves the surviving boundaries still parse identically, and `report-render.md` declares the same four-phase row contract.

**Spec:**

This is the highest-risk edit in the plan: phase-boundary detection is positional and the four phases are interlocked. It needs a regression oracle, and **the plan's original oracle does not exist.** Verified against the live corpus: every retained log under `~/.local/logs/clean-fix/` is a skip-only run (145 at plan time; retention moves the count, never the conclusion) — every one ends `SKIP: style eval disabled` / `SKIP: style eval review disabled` / `SKIP: style fix disabled` and then the completion line. There is not a single `CLEAN:`, `WARMUP:`, `EVAL:`, `REVIEW:`, or `FIX:` line anywhere in the retention window, because the three style stages have been switched off for its whole duration. Copying "the newest log with eval, review, and fix activity" is impossible, and a baseline taken from a skip-only log would exercise none of the code this phase is cutting into.

**Build the oracle instead, and build it before editing the parser.**

1. **Write a fixture log** at `scripts/clean-fix/tests/fixtures/six-phase-run.log`, representative of a full pre-change run: the `=== Starting clean-fix (scope: all) ===` banner, the settings back-population block, then activity lines for all six of the phases the parser currently models — clean, warmup, eval, review, fix, and verify — for at least two projects, ending in the completion line. Include at least one skipped project and one warning so `SkipReason` and `ToolWarning` are exercised too.

   **The clean and warmup emitters no longer exist in the tree** — Phase 2 deleted them from `clean-fix.sh` and deleted `clean-fix-warmup.sh` outright — so there is nothing live to derive their line shapes from, and no retained log carries them. Their exact vocabulary, read out of commit `342f13c` (the Phase 1 checkpoint, the last tree that still had every emitter), is therefore stated here rather than left to be found:

   | Emitter | Exact line body (after `log`'s timestamp prefix) |
   | --- | --- |
   | run banner | `=== Starting clean-fix (scope: all) ===` |
   | clean stage disabled | `SKIP: clean/build disabled in $CLEAN_FIX_AGENT_ASSIGNMENTS_FILE` |
   | no manifest | `SKIP: <project> (no Cargo.toml at <dir>)` |
   | unchanged since last run | `SKIP: <project> (not modified since last run)` |
   | clean | `CLEAN: <project>` |
   | build | `BUILD: <project>` |
   | mend | `MEND: <project>` |
   | mend failure | `WARNING: cargo mend failed for <project>` |
   | mend switched off | `SKIP: mend for <project> — mend=off in config/lint.conf` |
   | project finished | `DONE: <project>` |
   | not in the build allowlist | `SKIP: <project> (not listed in [build])` |
   | warmup start | `WARMUP: <name>` |
   | warmup success | `WARMUP OK: <name> (pid <pid>, BRP responding on port 15799)` |
   | warmup teardown | `WARMUP KILLING: <name> (pid <pid>)` |
   | warmup timeout | `WARMUP FAIL: <name> (BRP never responded within 120s)` |
   | warmup skipped | `WARMUP SKIP: <name> (screen locked)` |
   | completion | `=== Clean-fix Rust clean + rebuild complete (<M>m <S>s) ===` |

   `WARMUP KILLING` matters and is not decoration: `detect_phase_boundaries` detects warmup with `"WARMUP:" in line and "WARMUP KILLING" not in line`, so a fixture without it never exercises that guard.

   For the four surviving phases the emitters are live and unchanged — read `style-eval-all.sh`, `style-eval-review-all.sh`, and `style-fix-worktrees.sh` for the eval, review, fix, and verify lines, and `clean-fix.sh`'s `log()` for the timestamp prefix every line carries. Cross-check the whole fixture against the parser's own detection regexes so each line lands in the phase intended. If any pre-change line shape is still unclear, `git show 342f13c:scripts/clean-fix/clean-fix.sh` and `git show 342f13c:scripts/clean-fix/clean-fix-warmup.sh` are the sources this table was read from — quote the commit, never a working-tree path that no longer holds them. Note that the fixture is written against the **pre-Phase-2** log vocabulary on purpose — it is the artifact that proves the deleted parsers were unnecessary rather than merely unexercised, which no post-Phase-2 log can do. Two details follow from that and are not optional. The banner scope is `all`, not `style`: a run carrying clean, warmup **and** style activity could only have been `all`, so a `style` banner would describe a run that never existed. And the completion line uses the retired wording `=== Clean-fix Rust clean + rebuild complete (…) ===`, which `HISTORICAL_COMPLETE_RE` still recognizes — so the fixture parses as a finished run rather than a crashed one.
2. **Capture the pre-edit baseline** by running the current parser against the fixture and saving its eval, review, fix, and verify per-project cells.
3. **Write the regression test** as a new `unittest` file, `scripts/clean-fix/tests/test_report_parse_phases.py`, asserting that parsing the fixture yields exactly those four phases, that the per-project eval/review/fix/verify cells equal the captured baseline, and that no `clean` or `warmup` phase appears in the result. This is the durable form of the oracle: it lives in the repository, it runs with the existing test suite, and it gives the plan's highest-risk file the coverage it has never had.

Only then edit `scripts/clean-fix/clean_fix_report_parse.py`:

- `:44` — `PHASES: tuple[str, ...] = ("clean", "warmup", "eval", "review", "fix", "verify")` becomes `("eval", "review", "fix", "verify")`.
- `:661-741` — the phase-boundary block (`detect_phase_boundaries`). Delete `clean_start`, `warmup_start`, `clean_end`, and `warmup_end` and everything computing them: the `"WARMUP:" in line and "WARMUP KILLING" not in line` detection, the clean-end scan, the warmup-end scan, the `CLEAN: <project>` guard on registering the clean phase, and both `bounds[...]` assignments. `eval` becomes the first phase, so its start is the first line of the log rather than the successor of a clean or warmup boundary. Trace the existing eval-start computation and make it independent of `clean_end` / `warmup_end`.
- `:744-805` — delete `parse_clean_phase()` in full.
- `:807-833` — delete `parse_warmup_phase()` in full.
- Delete both call sites wherever the parser dispatches per phase.
- `:199-200` — `PhaseStats.processed` and `PhaseStats.warnings`, both clean-only. Verified: `processed` is written once, at `:804` inside `parse_clean_phase`, and `warnings` once, at `:780` in the same function; both are read only by the emitter branch below. Delete both fields. `ParseResult.warnings` is a different member — the cross-phase list of `Warning` records — and stays.
- `:1980-1982` — the emitter's `if phase == "clean":` branch, which appends `processed=` and `warnings=` to the `PHASE` line. It has no surviving phase to fire for; delete it together with the two fields.
- `:36-42` — `MONITOR_FILTER_REGEX`. Drop `CLEAN|BUILD|MEND|DONE` from the first alternation and delete the `WARMUP (OK|FAIL|SKIP):` alternation outright. This regex is what `--monitor-filter` hands a live monitor, so retired verbs left in it make the monitor watch for lines nothing emits any more.
- `:1938-1946` — `detect_current_phase`'s backward walk. Delete the `if "WARMUP" in line:` arm returning `warmup` and the `CLEAN:|BUILD:|MEND:|DONE:` arm returning `clean+rebuild`.
- `:62` — the comment on the always-excluded set says it covers directories not opted into the `[build]` / `[projects]` allowlists. Rewrite for `[projects]` alone.
- `:378-407` — the conf reader returning the style-eval project-name to project-root mapping. Confirm it parses only `[projects]` / `[active_checkout]`; if it also scans `[build]`, remove that.
- `:97-107` `COMPLETE_RE`, `HISTORICAL_COMPLETE_RE`, and `match_completion_banner()` — Phase 2 built all three. **Leave every one of them exactly as it is**, including the completion check at `:1931` that routes through the helper. `HISTORICAL_COMPLETE_RE` is deliberate compatibility surface for the retained pre-Phase-2 logs, not a residue this sweep should reach; and `match_completion_banner()` returning `re.Match[str] | None` is the `re` module's own boundary form, not a domain optional to replace with a semantic type.
- `:1369` `.clean-fix-project` — leave it. Renamed in Phase 8.

Keep every `SkipReason`, `ToolWarning`, `Warning`, `AgentLimit`, and `Cell` mechanism intact; only the two deleted phases lose their producers.

**`scripts/clean-fix/report-render.md`** — the parser's output schema and the prompt that consumes it change together, in this phase, because nothing else keeps them honest. The render agent is prompted from this file and fed this parser's rows; a commit that ships a four-cell parser against a six-cell contract is a mismatch resting on an untested assumption about how tolerant the agent is.

- `:26` — the `ROW` contract line drops `clean=<cell>` and `warmup=<cell>`, leaving `ROW <project>  eval=<cell> review=<cell> fix=<cell> verify=<cell> reason="…" [phase_now="…"]`.
- `:27` — the `ALWAYS_EXCLUDED` gloss references "the relevant allowlist ([build] / [projects])". Reduce to `[projects]`.
- `:156` — the example `phases not in this log: clean,warmup,eval,review` uses deleted phase names. Rewrite with surviving ones (for example `eval,review`).
- Sweep the rest of the file for any other clean/warmup/build phase reference and correct it; those three line numbers are the known ones, not necessarily the only ones. Leave the `~/.local/logs/clean-fix/` path at `:11` alone — Phase 8 moves the log directory and rewrites that line with every other consumer of the path.

**Pending decision: whether `PhaseStats`'s three footer fields keep their bare `int | None` shape or become a semantic phase-progress type**

Actual problem:
`PhaseStats` (`clean_fix_report_parse.py:199-203`) is a domain-owned dataclass, and this phase already opens it to delete `processed` and `warnings`. The three fields left behind — `footer_ok`, `footer_fail`, `footer_total` — are `int | None`, and the `None` is not absence but a state: `footer_total is None` is read at `:927` and `:987` as "this phase emitted no footer, so it is still running". The type design contract calls that exactly the case a semantic type should replace, and a reader of the field name alone cannot tell a phase that reported zero failures from one that reported nothing at all.

What exists now:
- `footer_ok: int | None = None`, `footer_fail: int | None = None`, `footer_total: int | None = None`
- Two `phase_running = result.status == "in-progress" and stats.footer_total is None` derivations
- Three assignment sites (`:917-919`, `:983-985`, `:1096`) that set all three together from one regex match

What should change:
- Replace the three fields with one member holding either "no footer seen" or a footer's three counts together, so the running/finished distinction is the type rather than a `None` check, and the three counts cannot drift out of sync.
- Alternatively, leave them and record the exemption: they are pre-existing, this plan's subject is the clean capability, and the Phase 2 as-built already exempted pre-existing optionals once.

Recommendation:
Defer, and keep the fields as they are for this phase. The plan's subject is removing the clean capability; `footer_*` predates it, is untouched by these deletions, and reshaping it pulls three assignment sites and two derivations into the plan's highest-risk file in the same commit that cuts its phase model in half. It is a real finding and belongs in a follow-up once the rename lands — not inside the edit that most needs to stay reviewable.

**Files:**
- `scripts/clean-fix/clean_fix_report_parse.py` — drop the clean and warmup phases, their parsers, and their boundary detection
- `scripts/clean-fix/tests/fixtures/six-phase-run.log` — new; the representative pre-change run the regression test parses
- `scripts/clean-fix/tests/test_report_parse_phases.py` — new; asserts the four surviving phases against the captured baseline
- `scripts/clean-fix/report-render.md` — four-phase `ROW` contract and `[projects]`-only gloss

**Reservations:**
- file: `scripts/clean-fix/clean_fix_report_parse.py`
- file: `scripts/clean-fix/tests/fixtures/six-phase-run.log`
- file: `scripts/clean-fix/tests/test_report_parse_phases.py`
- file: `scripts/clean-fix/report-render.md`

**Constraints from prior phases:**
- Phase 2 changed `COMPLETE_RE` (`:97`) to match `=== Clean-fix complete (…) ===` and removed `CLEAN`, `BUILD`, and `MEND` from `clean-fix.sh`'s report activity grep. A log produced after Phase 2 therefore contains no `CLEAN:`, `BUILD:`, `MEND:`, or `WARMUP:` lines. That is one of the two reasons the oracle is a hand-built fixture rather than a captured log; the other is that no captured log has any style activity either.
- Phase 2 also added `HISTORICAL_COMPLETE_RE` (`:100-103`) for the retired `=== Clean-fix Rust clean + rebuild complete (…) ===` wording, and routed both regexes through a single `match_completion_banner()` helper (`:105-107`) used by `parse_log` (`:1309`) and `detect_current_phase` (`:1931`). The reason is that retained logs outlive the code that wrote them: 142 of the 145 currently on disk still carry the old banner, and without the historical pattern every one of them reads as a run that never finished.
- That resolves the fixture question this Work Order previously left open. The fixture writes its completion line in the pre-Phase-2 form **and** parses as a completed run, so the test asserts both the phase boundaries and run completion; no tradeoff between the two remains, and the test docstring records that rather than a choice.
- `HISTORICAL_COMPLETE_RE`, `match_completion_banner()`, and the retired banner literal inside the regex are load-bearing compatibility surface. Do not delete, inline, or tidy any of the three, and keep the acceptance gate's residual-string greps away from them.
- The `.clean-fix-project` marker read at `:1357` is renamed in Phase 8, not here.
- `scripts/clean-fix/tests/` currently holds one `unittest` file and no fixtures directory; create `fixtures/` as part of this phase.
- **Three surfaces are knowingly stale between here and Phase 7, and are not this phase's to repair.** Phase 3 deleted the conf's `[build]` section and `retarget_clean_fix.py`'s build-set add/revert, which leaves `commands/make_a_worktree.md:101,107-108`, `commands/worktree_delete.md:103`, and `scripts/new_rust_project/rust_generate.sh:180` describing or writing a section that no longer exists — `rust_generate.sh` would recreate `[build]` in the conf if `/new_rust_project` ran before Phase 7. Phase 7 owns all three, line by line. `commands/clean_fix.md:310`'s reference to the deleted `cf_print_stage_enabled` is Phase 5's for the same reason. None of the four is reachable from the report parser, so none of them changes what this phase must do; leave them alone rather than widening the diff.

**Acceptance gate:**
- `basedpyright scripts/clean-fix/` prints `0 errors, 0 warnings, 0 notes`.
- `python3 -m py_compile scripts/clean-fix/clean_fix_report_parse.py` exits 0.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --list` exits 0 and enumerates the logs under `~/.local/logs/clean-fix/`.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` exits 0.
- **The regression test is the oracle:** `python3 scripts/clean-fix/tests/test_report_parse_phases.py` prints `OK`. It must fail if the parser is reverted to six phases and fail if any eval/review/fix/verify cell drifts from the captured baseline — demonstrate both by temporarily breaking one assertion and observing the failure before committing.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --phase-detect scripts/clean-fix/tests/fixtures/six-phase-run.log` exits 0 and reports `done`. The fixture ends in a completion banner, and `detect_current_phase` short-circuits on that before it ever walks backwards looking for a phase signal — expecting one of the four active phases here would assert the opposite of correct behavior.
- `grep -nE "parse_clean_phase|parse_warmup_phase|clean_start|warmup_start|clean_end|warmup_end|WARMUP|CLEAN:|BUILD:|MEND:|clean\+rebuild|s\.processed|s\.warnings" scripts/clean-fix/clean_fix_report_parse.py` returns nothing. The widened alternation is the point: the original list caught the two deleted parsers and `WARMUP`, but not the monitor filter's `CLEAN|BUILD|MEND|DONE` verbs, the `clean+rebuild` arm in `detect_current_phase`, or the two clean-only stats fields. The fixture log still contains those strings by design; it is data, not code.
- `grep -n "HISTORICAL_COMPLETE_RE\|match_completion_banner" scripts/clean-fix/clean_fix_report_parse.py` still matches — the compatibility path survives this phase intact.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --list` still reports every retained log as complete rather than in-progress, which is the check that the historical banner still resolves after this phase's deletions.
- `grep -nE "clean=|warmup=|\[build\]|clean\+rebuild" scripts/clean-fix/report-render.md` returns nothing.
- `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` prints `OK`.

---

### Phase 5 — Update the user-facing surface: usage screen and command doc · status: todo

#### Work Order

**Goal:** `/clean_fix` offers no clean scope, no clean stage switch, and no clean skip list; the usage screen and the command doc describe the three surviving stages.

**Spec:**

**`scripts/clean-fix/clean-fix-usage.sh`**
- `:33-35` — the `clean_fix run [project]` description says "Clean/build/warmup and style eval/review/fix"; rewrite as style eval/review/fix. Delete the `clean_fix run clean [project]` line (`:34`) **and** the `clean_fix run style [project]` line (`:35`). Both scope words have to go, not just the clean one: Phase 2 deleted the `clean|style|all` case arm, so any non-`run_once` first argument is now a project filter. `clean_fix run style` today means "style pass filtered to a project literally named style", which matches nothing and reports nothing — a usage screen that still advertises it is documenting a silent misfire.
- `:43` — delete the `clean_fix clean` status line.
- `:47-50` — `clean_fix on` / `off` say "the clean stage and all style stages"; rewrite as all style stages. The `eval on` / `eval off` lines say "Also works for clean, review, and fix"; drop `clean`.
- `:37` — the `clean_fix add` description says it adds to "clean and style allowlists"; rewrite for the single allowlist.
- `:51-56` — six skip lines, alternating clean and style. Delete the three `clean_fix skip clean …` lines (`:51`, `:53`, `:55`) and **rewrite the three surviving style lines in the short form**: `clean_fix skip` (show what is skipped), `clean_fix skip <target>...`, and `clean_fix skip enable <target>...`, keeping their existing descriptions minus the word "style" where it only distinguished them from the clean pass. Phase 3 made the scope token optional, so a usage screen still printing `clean_fix skip style` advertises a word the user never needs to type — and once the clean pass is gone, "style" no longer distinguishes anything.
- `:17,232,238-262,298,323,347-348,461` — delete the `CLEAN_STATUSES` array and every use: its initialization, the `if [[ "$column" == "clean" ]]` branch in `set_project_status`, the `tmp_clean` swap in the sort, the `[[ "$section" == "build" ]]` branch in the conf scan (`:308,320`), the `set_project_status … "clean" …` call, the `"clean"` key in the `--json` emitter, and the comment at `:461` about the clean/build pass carrying only a status. The project table loses its clean column; keep the style column and the header alignment consistent.

**`commands/clean_fix.md`** (edit with the Edit/Write tools — `commands/` is a protected path):
- `:17` dispatch line — remove `clean` from the `<StyleAgentConfig/>` token list.
- `:21-42` — delete the `## run [clean | style] [project]` heading variant and every scope-word form. The remaining scopes are a bare project filter and `run_once`. Rewrite `:29-30` (the scope bullet list) and delete all four lines `:38-41` — `run clean`, `run clean <project>`, `run style`, and `run style <project>`. Deleting only the `clean` pair leaves the `style` pair documenting a filter that will not match.
- `:73-76` — the shell block still shows `~/.claude/scripts/clean-fix/clean-fix.sh [clean|style] [project]`. Rewrite that line as `… clean-fix.sh [project]`; leave the `run_once` line alone.
- `:89-95` — the notes. `:93` says `run_once` "does not run clean/build/warmup"; that contrast is now meaningless — rewrite it to say `run_once` is a one-time style-stage override that does not persistently enable any stage.
- `:115` and `:143` — `<Add/>` and `<Rename/>` say the helpers touch `[build]`; correct both to `[projects]` (and `[active_checkout]` plus keyed sections for rename).
- `:171` — remove `/tmp/cargo-clean-stdout.log` from the `<DetectLog/>` candidate list.
- `:197` — the `PHASE <name>` sentinel list drops `clean+rebuild` and `warmup`.
- `:230` — the example "clean → eval → fix all in one orchestrator run" becomes an eval → review → fix example.
- `:244` — `<Monitor/>`'s stop condition waits for `=== Clean-fix Rust clean + rebuild complete`, a banner the orchestrator stopped writing in Phase 2. An armed monitor therefore never sees the run end and never calls TaskStop. Point it at the line the orchestrator emits today, `=== Clean-fix complete` (`clean-fix.sh:226`), and leave the `=== Done:` alternative beside it alone.
- `:271` — the log-sink sentence names both plists and both `/tmp` sinks; reduce to `com.natemccoy.style-fix` and `/tmp/style-fix-stdout.log`.
- `:289` — heading `## clean|eval|review|fix [on|off]` becomes `## eval|review|fix [on|off]`.
- `:299` — delete the `**clean**` bullet in full, including its description of the mend gate and the `SKIP: clean/build disabled` log line.
- `:309` — step 2 maps a first token to a section; drop `clean` -> `[clean]`.
- `:310` — step 3 describes `/clean_fix clean` calling `cf_print_stage_enabled`; delete that sentence. `cf_print_stage_enabled` no longer exists.
- `:311` — step 4 lists invalid project-name forms; drop the `/clean_fix clean <project>` form.
- `:312` — step 5's example list drops `clean off`.
- `:313` — step 6 says `/clean_fix on` and `off` set "all four `enabled=` values — `[clean]` included" and explains the pre-`[clean]`-switch history. Rewrite for three stages and delete the history sentence.
- `:321-349` — `<Skip/>`. Delete the `clean` scope: the `## skip clean|style` heading becomes `## skip style` (or the bare form if Phase 3 made the scope optional), and the `**clean**` bullet at `:327` goes. The `**style**` bullet at `:328` ends "A style skip leaves clean untouched" — drop that clause. `:349`'s description of the `#CLEAN_FIX_SKIP#` marker stays; the marker is renamed in Phase 8.

**Files:**
- `scripts/clean-fix/clean-fix-usage.sh` — drop the clean usage lines and the `CLEAN_STATUSES` column
- `commands/clean_fix.md` — drop the clean scope, stage switch, and skip list

**Reservations:**
- file: `scripts/clean-fix/clean-fix-usage.sh`
- file: `commands/clean_fix.md`

**Constraints from prior phases:**
- Phase 4 reduced the parser's `PHASES` to `("eval", "review", "fix", "verify")` **and** rewrote `report-render.md`'s `ROW` contract to match. That file is finished for this plan's clean removal; do not re-edit it here.
- Phase 3 deleted the `[clean]` section from `agent-assignments.conf` and Phase 2 deleted `cf_load_stage_enabled clean`, so `/clean_fix clean` has nothing to read; Phase 3 also deleted `cf_print_stage_enabled`, so the command doc's reference to it at `:310` is a dangling call.
- Phase 3 made `phase_skip.py`'s scope argument optional while still accepting `style`. Either form works in the rewritten `<Skip/>` section; prefer the short form and say so.
- Phase 2 replaced `clean-fix.sh`'s `SCOPE` variable with a `run_once` flag and deleted the `clean|style|all` case arm, so `clean`, `style`, and `all` are no longer scope words that fail loudly — each is now read as a project filter that silently matches nothing. That is why every one of them has to leave the usage screen and the command doc in this phase, not just the clean ones.
- Phase 2 set the orchestrator's completion banner to `=== Clean-fix complete (…) ===` (`clean-fix.sh:226`), which is what this phase's monitor fix targets. Phase 8 changes that banner again, to `=== Fix complete (…) ===`, and owns updating this doc's stop condition the second time.
- `#CLEAN_FIX_SKIP#` and `/tmp/clean-fix-report.txt` keep their current names through this phase; Phase 8 renames them.
- `commands/` is a protected path — use Edit/Write, never shell redirection or `sed -i`.

**Acceptance gate:**
- `bash -n scripts/clean-fix/clean-fix-usage.sh` exits 0.
- `bash scripts/clean-fix/clean-fix-usage.sh` exits 0 and its output contains no `run clean`, no `skip clean`, and no clean column in the project table.
- `bash scripts/clean-fix/clean-fix-usage.sh --json | python3 -m json.tool` exits 0, and the parsed JSON has no `clean` key on any project object.
- `grep -nE "CLEAN_STATUSES|run clean|run style|run all|skip clean|skip style|== \"clean\"|== \"build\"" scripts/clean-fix/clean-fix-usage.sh` returns nothing. `skip style` is in this list on purpose: the short skip form is the deliverable, not a preference, so a surviving scope word is a failed edit rather than a stylistic leftover.
- `bash scripts/clean-fix/clean-fix-usage.sh` prints a `clean_fix skip <target>...` line, proving the short form actually reached the screen rather than the scope-word lines simply being deleted.
- `python3 scripts/clean-fix/phase_skip.py status` and `python3 scripts/clean-fix/phase_skip.py style status` still produce identical output — the short form is the advertised one, but Phase 3 kept the scope token accepted, and a user's muscle memory must not start erroring.
- `python3 scripts/clean-fix/project_add.py <some-existing-project>` prints **exactly one** `[projects]` result line. `<Add/>` at `:115` relays this helper's output verbatim to the user, and Phase 3 reduced it from a per-section list to a single result; a second line here means a caller is still formatting for the retired two-allowlist shape.
- `grep -nE "run clean|run style|run all|\[clean\|style\]|skip clean|cargo-clean|cf_print_stage_enabled|clean\+rebuild|\[build\]|\[clean\]" commands/clean_fix.md` returns nothing. Every retired leading token is now read as a project filter, so each one has to be gone from the doc — not only the `clean` spellings.
- `grep -n "Clean-fix Rust clean + rebuild complete" commands/clean_fix.md` returns nothing, and `grep -n "Clean-fix complete" commands/clean_fix.md` matches at the monitor stop condition — the check that the monitor now waits on a banner something actually writes.
- `/clean_fix` with no arguments renders the usage screen without error (invoke the usage script directly; do not require a live agent run).

---

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
- Everything here keeps its current `clean-fix` / `clean_fix` naming; Phases 8 through 10 rename.

**Acceptance gate:**
- `grep -rniE "nightly|cargo.clean|warmup|4:00 AM|\[build\]" scripts/clean-fix/README.md` returns nothing.
- `grep -rn "clean-fix" config/README.md config/lint.conf` returns nothing.
- `grep -rn "\[build\]\|build_add\|build_already" commands/ scripts/ docs/ README.md CLAUDE.md config/` returns nothing. The two JSON key names are in this grep because a doc can promise a key without ever spelling the section: `make_a_worktree.md:108` tells the agent to report `build_add`, which `retarget_clean_fix.py` stopped emitting in Phase 3, and a `[build]`-only sweep walks straight past it.
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
- `CLEAN_FIX_SCRIPT` -> `FIX_ORCHESTRATOR_PATH` (`clean-fix-trigger.sh:28`; it holds the orchestrator's absolute path and the pgrep guard matches on it)

Find every occurrence first (`grep -rn "CLEAN_FIX_" --include='*.sh' --include='*.py' --include='*.md' .`) and rename all of them together; a partial rename silently breaks stage loading. The `cf_*` bash function prefix is cosmetic — leave it alone.

**Agent registry family** `cleanfix` -> `fix`:
- `config/agents.conf` — `:21` `cleanfix=codex` becomes `fix=codex`; `:55` the section comment; `:56` `[cleanfix.codex]` and `:62` `[cleanfix.claude]` become `[fix.codex]` and `[fix.claude]`.
- Every `agents_resolve cleanfix.<stage>` call site — `scripts/clean-fix/agent_assignments.sh:93` and the `agent_exec.sh cleanfix.report` call in `clean-fix.sh:237`.
- `docs/as-built/agent-registry.md` — `:33` the family/sub-task table row, and `:140-147` the consumer rows, plus every `/agent cleanfix …` example.
- `commands/clean_fix.md` — every `/agent cleanfix <family>` and `/agent cleanfix.<stage>` reference.
- `scripts/clean-fix/clean-fix-usage.sh:45-46` — the two `/agent cleanfix …` usage lines.
- `scripts/clean-fix/README.md:18-19` — the file table describes assignments living under `[cleanfix.<family>]` and resolving through `agents_resolve cleanfix.<stage>`. Both spellings change with the family key; the surrounding prose is Phase 7's and Phase 10's.
- `scripts/agents/test_sync_codex_catalog.sh` — **this is a live test, not documentation.** Its `agents.conf` fixture heredocs (`:67,69,126,128,160,162,187,189`) and its four assertion strings (`:210,212,214,216`) spell the family key `cleanfix`. Rename it in all twelve places and re-run the test in this phase: the assertions match on the registry's own diagnostic text, so a renamed family with an un-renamed test fails on the mismatch — which is the test doing its job, not a flake to work around.

**Runtime brand strings — one atomic edit, five participants.** Completion recognition is not a matched pair any more. Phase 2 made it four things: the emitter (`clean-fix.sh:226`, writing `=== Clean-fix complete (…) ===`), `COMPLETE_RE` (`clean_fix_report_parse.py:97`) for that current wording, `HISTORICAL_COMPLETE_RE` (`:100-103`) for the retired `=== Clean-fix Rust clean + rebuild complete (…) ===`, and `match_completion_banner()` (`:105-107`), the one helper both call sites go through — `parse_log` (`:1309`) and `detect_current_phase` (`:1931`).

Phase 5 adds the fifth: `<Monitor/>`'s stop condition in `commands/clean_fix.md:244`, which Phase 5 repoints at `=== Clean-fix complete` because the banner it waited on before that had already been retired. It is not a parser and not a regex, which is exactly why it gets missed — but it is the line that decides when an armed monitor calls TaskStop, so leaving it on the superseded wording strands the monitor watching a run that has already ended. It moves with the emitter, in this commit.

In this commit: change the emitter to `=== Fix complete (…) ===`, change `COMPLETE_RE` to match it, repoint the command doc's monitor stop condition at `=== Fix complete`, and **add the wording being replaced to the historical set** rather than dropping it. After this phase there are three generations — `Fix complete` current, `Clean-fix complete` and `Clean-fix Rust clean + rebuild complete` historical — because the log directory carries roughly a day of logs across the change and every one of them must still read as a finished run. Keep all three behind `match_completion_banner()`: a second copy of this literal went stale once already, which is why that helper exists.

Splitting the emitter and the current regex across commits makes every report in between call a healthy run crashed. Phase 10's residual sweep names these compatibility constants as a permitted exception — they are the one place the retired brand is load-bearing rather than left over.

**Runtime paths**
- Log directory `~/.local/logs/clean-fix/` -> `~/.local/logs/fix/`, and the legacy single-file symlink `~/.local/logs/clean-fix.log` -> `~/.local/logs/fix.log`. **Migrate the existing directory**: `mv ~/.local/logs/clean-fix ~/.local/logs/fix` and remove the stale `~/.local/logs/clean-fix.log` symlink, so `/clean_fix report` and `list` keep seeing history.
- **Quiesce the job before the directory moves.** `com.natemccoy.style-fix` is loaded and fires every 600 seconds, so a run can begin at any point during this phase. One that is mid-append when the directory moves out from under it either loses its output or recreates `~/.local/logs/clean-fix/` behind the migration, and the phase then passes its greps while the old path is quietly back. The order is: `launchctl bootout gui/$(id -u)/com.natemccoy.style-fix` (unsandboxed), confirm no orchestrator is running with the same `pgrep -f` guard the trigger uses, migrate and edit, then `bash scripts/clean-fix/setup.sh` to bootstrap it back and confirm it reports a reload. Leaving the job booted out silently stops the whole pipeline, so verify the reload before this phase reports done.
- **Own every reader of that directory in the same commit.** Verified at plan time, these are all of them: `clean-fix.sh:34` (`LOG_DIR`) and `:36` (`LEGACY_LOG`); `clean_fix_report_parse.py:27` (`LOG_DIR`) and its `:7` usage docstring; `style-fix-monitor.py:38` (`LOG_DIR` — the monitor waits on this directory, so a missed rename leaves it watching a path nothing writes to); `style-fix-manual.sh:3,25` (`LOG_DIR` — a manual run would otherwise recreate the old directory the moment someone uses it); `report-render.md:11`; and `commands/style_eval.md:334`. `commands/clean_fix.md` names the path at `:72,82,172,173` and is already in this phase's Files.
- **Log filenames follow the directory.** The orchestrator writes `clean-fix-YYYYMMDD-HHMMSS.log` and `style-fix-manual.sh` writes `style-fix-manual-*.log`. Rename the orchestrator's prefix to `fix-YYYYMMDD-HHMMSS.log`, leave the manual prefix alone (it is named for the surviving style job), and update the two globs that read them: `clean_fix_report_parse.py`'s log enumeration and `commands/clean_fix.md:82,173`. **Retention must match both**: whatever prunes or enumerates logs has to cover the new `fix-*` names *and* the migrated `clean-fix-*` files, which keep their old names after the `mv`. A glob that matches only one of the two either strands the history or never prunes it — say in the code comment which of the two patterns each glob is for.
- Report file `/tmp/clean-fix-report.txt` -> `/tmp/fix-report.txt` — `clean-fix.sh`'s `REPORT_FILE`, `docs/as-built/agent-registry.md:142`, and `commands/clean_fix.md`.
- Leave `/tmp/style-fix-stdout.log` and `/tmp/style-fix-stderr.log` alone; they are named for the surviving launchd job and are already correct.

**Skip marker** `#CLEAN_FIX_SKIP#` -> `#FIX_SKIP#`:
- Readers — all four, verified at plan time: `scripts/clean-fix/phase_skip.py:28` (`MARKER`, and its module docstring at `:8-9`), `scripts/clean-fix/project_add.py:19` (`MARKER`), `scripts/clean-fix/clean-fix-usage.sh:8` (`MARKER`), and the description in `commands/clean_fix.md`. `project_add.py` uses the marker to tell a deliberately skipped entry from an absent one; leaving it on the old spelling makes it read every skipped project as missing and insert a duplicate active entry beside it. It changes and is tested in this commit with the rest.
- **Migrate the data in the same commit.** Any line already commented out in `clean-fix.conf` carries the old marker; rewrite those occurrences in the conf too. Skipping this leaves temporarily-skipped entries invisible to `enable-all`, which silently strands them.

**Project marker** `.clean-fix-project` -> `.fix-project`:
- Writers and readers: `scripts/clean-fix/style-fix-worktrees.sh:630,635-637` (writes the file and adds it to `.git/info/exclude`), `scripts/clean-fix/style_history.py:462` (`PROJECT_MARKER`), `scripts/clean-fix/project_rename.py:303` (the `*_style_fix/.clean-fix-project` glob), `scripts/clean-fix/clean_fix_report_parse.py:1369`, `scripts/worktree_delete/perform_deletion.sh:40,46-47`, and `commands/style_fix_review.md:229-233`.
- **No data migration is needed**: verified at plan time that no `~/rust/*_style_fix` worktree exists. Re-verify with `ls -d ~/rust/*_style_fix` before editing. If one has appeared since, rename its marker file as part of this phase. Stale `.clean-fix-project` entries left in any repo's `.git/info/exclude` are harmless and need no cleanup.

**Pending decision: whether this plan also runs a type-design pass over the three conf-helper scripts, or stops at the de-branding**

Actual problem:
This phase is a sweep across identifiers, so it opens every helper that owns the pipeline's domain types — and those types fail the type design contract on two counts at once. Their names state representation or how a value was obtained rather than the role it plays, and three of their members use a bare optional where the `None` is a distinct state rather than an absent value. Nothing about removing the clean capability requires touching either, which is why they have survived this far; but a rename sweep is the moment the cost of fixing them is lowest, and also the moment a reviewer will ask why they were left.

What exists now:
- `project_add.py:36` `SectionResult`; `project_rename.py:38,46,51` `PlannedMove`, `PlannedMarker`, `Plan`; `retarget_clean_fix.py:41,178` `DetectResult`, `CommitResult` — six names describing a result, a plan, or a detection rather than what the value is for
- `project_add.py:32` `workspace_root: Path | None`, valid only when `kind == "workspace_member"` and checked that way at `:305` — the discriminant and the payload are two fields that must agree
- `project_rename.py:59` `pending_path: Path | None`, meaning "this rename has pending state to migrate" and read as a presence test at `:435,471`
- `DetectResult` is a `TypedDict` carrying a `match` boolean, a free-form `kind` string, and fields that are empty strings when `match` is false — match state encoded three ways at once

What should change:
- Rename toward roles: a project-allowlist change, a project-rename migration plan, a history-state move, a worktree-redirect match, a configuration-commit outcome.
- Replace `workspace_root` and `kind` with one tagged project-role member, `pending_path` with a pending-migration variant, and `DetectResult`'s boolean-plus-`kind`-plus-empty-strings with a tagged match type.

Recommendation:
Take the two `Path | None` members and `DetectResult`'s tagged shape; leave the six names alone in this plan. The optionals are correctness surface — `workspace_root` and `kind` can disagree today, and `DetectResult` can claim `match: true` with empty payload fields — and each is a contained edit inside one file. The renames are not: they touch every call site across three scripts on top of a sweep that is already rewriting every identifier in the tree, and **renaming is the one operation the user does faster than any agent** — a global rename in the editor is exact and instant, so the right move is to hand them over as a list rather than spend a phase on them. If the whole item is deferred instead, it is a follow-up, not a defect: none of it is reachable from the clean removal.

**Files:**
- `scripts/clean-fix/clean-fix.sh` — env vars, `LOG_DIR`, `LEGACY_LOG`, `REPORT_FILE`, `agent_exec` family
- `scripts/clean-fix/agent_assignments.sh` — env vars and `agents_resolve fix.<stage>`
- `scripts/clean-fix/clean-fix-usage.sh` — `MARKER`, env vars, `/agent fix …` usage lines
- `scripts/clean-fix/phase_skip.py` — `MARKER`
- `scripts/clean-fix/project_add.py` — `MARKER`
- `scripts/clean-fix/clean_fix_report_parse.py` — `LOG_DIR`, log-filename glob, `COMPLETE_RE`, project marker
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
- Phase 3 left `phase_skip.py` with `PASS_LABEL = "style"` (`:32`) feeding its six user-visible messages, and made the CLI's scope token optional. `PASS_LABEL` names the *style pass*, not the clean-fix brand — it is not part of this rename and its messages must stay byte-identical through this phase.
- Phase 2 rewrote `clean-fix-trigger.sh` down to a guard plus an `exec`: `CLEAN_FIX_SCRIPT` is now at `:13`, the `pgrep -f` guard at `:15`, and the `exec` at `:19`. Its idle gate and scope argument are gone.
- Phase 2 moved `clean-fix.sh`'s `LOG_DIR` to `:34`, `LEGACY_LOG` to `:36`, the completion banner to `:226`, and `REPORT_FILE` to `:231`; the file is 247 lines. `clean_fix_report_parse.py` is 2125 lines before Phase 4 and shorter after it; re-grep rather than trusting any line number in this Work Order that points into that file.

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
- `scripts/clean-fix` — the whole directory is renamed away, tests/ and docs/ included
- `scripts/fix` — the pipeline directory under its new name, with seven files renamed inside it
- `scripts/make_a_worktree/retarget_clean_fix.py` — removed by the rename
- `scripts/make_a_worktree/retarget_fix.py` — the redirect helper under its new name
- `pyrightconfig.json` — execution-environment root
- `.claude/settings.local.json` — 12 permission entries
- `scripts/fix/render-flow.py` — dot/svg basenames if hardcoded
- `scripts/fix/fix-style-flow.svg` — re-rendered after the rename, never hand-edited
- `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` — `scripts/clean-fix/` path strings

**Reservations:**
- file: `commands/clean_fix.md`
- file: `commands/fix.md`
- tree: `scripts/clean-fix`
- tree: `scripts/fix`
- file: `scripts/make_a_worktree/retarget_clean_fix.py`
- file: `scripts/make_a_worktree/retarget_fix.py`
- file: `pyrightconfig.json`
- file: `.claude/settings.local.json`
- file: `scripts/lint/invoke.sh`
- file: `scripts/lint/lint`
- file: `scripts/lint/lint_config.sh`
- file: `scripts/lint/scope.py`
- file: `scripts/delegate/verify.sh`
- file: `scripts/hooks/banned_words_lib.py`
- file: `scripts/agents/clean_agents_conf.sh`
- file: `scripts/worktree_delete/perform_deletion.sh`
- file: `scripts/new_rust_project/rust_generate.sh`
- file: `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`

**Constraints from prior phases:**
- Phase 8 renamed `CLEAN_FIX_SCRIPT` (`clean-fix-trigger.sh:13`) to **`FIX_ORCHESTRATOR_PATH`** but left its **value** pointing at the old path — this phase fixes the value. The name is `FIX_ORCHESTRATOR_PATH`, not `FIX_SCRIPT`: Phase 8 rejected the bare de-branding because `FIX_SCRIPT` names one of a dozen scripts in the pipeline without saying which. Do not reintroduce it. The same is true of `LOG_DIR`, which Phase 8 already repointed at `~/.local/logs/fix/`; that path is a runtime directory, not a script path, and needs no further change here.
- Phase 8 renamed the agent-registry family to `fix`, so nothing in `config/agents.conf` depends on the directory name.
- Phase 1 reduced `setup.sh` to one plist, so the reload touches a single agent.
- The launchd label `com.natemccoy.style-fix` and the plist filename `com.natemccoy.style-fix.plist` deliberately keep their names — they describe the surviving style job accurately, and renaming a loaded label risks a bootstrap mistake for no gain. Same for `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style_history.py`, and `style-fix-monitor.py`.
- `launchctl` must run unsandboxed.

**Acceptance gate:**
- `ls scripts/fix/fix.sh scripts/fix/fix.conf scripts/fix/fix-trigger.sh scripts/fix/fix-usage.sh scripts/fix/fix_report_parse.py` all exist; `ls scripts/clean-fix` reports no such directory.
- `ls commands/fix.md` exists; `ls commands/clean_fix.md` reports no such file.
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

**Goal:** No file outside session transcripts refers to `clean_fix`, `clean-fix`, or `cleanfix`, and one full pipeline run plus a rendered report proves the renamed system works.

**Spec:**

**Command cross-references** (all under the protected `commands/` path — Edit/Write only). Each of these names the `/clean_fix` command or the clean-fix brand in prose; rewrite every occurrence as `/fix` and "fix". **Do not plan on rewriting `scripts/clean-fix/` paths here.** Phase 9's acceptance gate requires every one of them to be gone already, so this phase inherits a tree with none left; if one turns up, it is a Phase 9 defect caught late — repair it here and say so in the report, rather than treating path rewriting as this phase's work:
`commands/style_fix_review.md`, `commands/style_usage.md`, `commands/style_eval.md`, `commands/focused_eval.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/clippy.md`, `commands/lint_config.md`, `commands/add_banned_word.md`, `commands/style_delete.md`, `commands/style_rename.md`, `commands/validate_and_push.md`.

Also update `commands/fix.md` itself — its own self-references (`/clean_fix run`, `/clean_fix report`, `/clean_fix monitor`, the `<DetectLog/>` paths, and the frontmatter `description`) must all say `/fix`.

**This phase is the prose and verification sweep.** Phase 9 already repointed every executable path reference, inside the renamed tree and outside it, and re-rendered the diagram. What is left here is wording — sentences that still say `/clean_fix` or "clean-fix" where nothing breaks but the text is now wrong — plus the end-to-end run that proves the whole plan. If this phase finds a *path* that still resolves to the old location, that is a Phase 9 defect being caught late: fix it here and say so, rather than filing it forward.

**Remaining docs and scripts** — `README.md:14`, `scripts/fix/README.md` (title, every path and command in the file table, the pipeline-flow diagram, the Evaluation State section, and the flowchart-generation instructions), `docs/as-built/agent-registry.md`, `config/README.md`, `config/lint.conf`, `scripts/fix/docs/candidate-enumeration-design.md`, `scripts/fix/style-fix-manual.sh`, `scripts/fix/style-eval-review-prompt.md`, `scripts/fix/rg-shim.sh`, `scripts/fix/setup.sh` (its header comment names the clean-fix agent), `scripts/fix/README.md`, and `CLAUDE.md`.

**The renamed core files still carry the brand in their prose and comments** — `fix.sh`, `fix-trigger.sh`, `fix-usage.sh`, `fix.conf`, `fix_report_parse.py`, `agent-assignments.conf`, `agent_assignments.sh`, `phase_skip.py`, `project_add.py`, `project_rename.py`, `style_history.py`, `style-fix-worktrees.sh`, `style-eval-all.sh`, `style-eval-review-all.sh`, `style-fix-monitor.py`, `report-render.md`, and `fix-style-flow.dot`. Their identifiers and paths were settled in Phases 8 and 9; what remains is comment and message text. Sweep the whole `scripts/fix/` tree rather than working from this list.

Note `scripts/agents/clean_agents_conf.sh` is named for *cleaning the agents conf file* — unrelated to clean-fix. Do not rename it; only correct any `clean-fix` path it contains.

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
- `HISTORICAL_COMPLETE_RE` and `match_completion_banner()` in `fix_report_parse.py` carry the retired banner wording on purpose, so that logs written before the rename still read as finished runs. Phase 4's `tests/fixtures/six-phase-run.log` and `tests/test_report_parse_phases.py` carry it for the same kind of reason: the fixture *is* a pre-change log, and the test asserts on its exact strings. Together with `setup.sh`'s historical launchd label, these are the sanctioned survivals of the old brand; the acceptance gate below names all four as permitted exceptions rather than things to sweep. A grep-driven edit to any of them is a regression dressed as cleanup.

**Acceptance gate:**
- `grep -rn "clean_fix\|clean-fix\|cleanfix\|CLEAN_FIX\|Clean-fix" . --exclude-dir=projects --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=plans` returns nothing, with **four** permitted classes of exception: `setup.sh`'s retired-agent cleanup block referencing the historical label `com.natemccoy.clean-fix`; the retired completion banners inside `fix_report_parse.py`'s `HISTORICAL_COMPLETE_RE` — `Clean-fix complete` and `Clean-fix Rust clean + rebuild complete` — which exist so migrated logs still parse as finished runs; `scripts/fix/tests/fixtures/six-phase-run.log`, which is a captured pre-change log and is **data, not code** — its `=== Starting clean-fix (scope: all) ===` banner, its `CLEAN:`/`BUILD:`/`MEND:`/`DONE:`/`WARMUP:` lines, and its retired completion banner are the whole reason the fixture proves anything; and the assertions in `scripts/fix/tests/test_report_parse_phases.py` that quote those same strings, including all three banner generations. Rewriting either of the last two to satisfy a grep destroys the regression oracle the plan built and leaves the highest-risk file uncovered. Confirm every surviving match falls into one of the four and nothing else; deleting any of them breaks reading the log history this plan deliberately preserved. The `plans` exclusion is deliberate and permanent — this plan and its siblings quote the old names as their subject matter, so the sweep's claim is about maintained implementation, command, configuration, and product documentation, never about the planning record.
- `grep -rn "clean_fix\|clean-fix" projects/-Users-natemccoy--claude/memory/` returns nothing.
- `bash -n` exits 0 on every touched shell script.
- `basedpyright scripts/fix/ scripts/make_a_worktree/ scripts/lint/ scripts/hooks/` prints `0 errors, 0 warnings, 0 notes`.
- `for t in scripts/fix/tests/test_*.py; do python3 "$t" || exit 1; done` prints `OK` for each — both the prompt-comment test and Phase 4's parser regression test.
- `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass.
- `cd scripts/fix && python3 render-flow.py` exits 0 and writes `fix-style-flow.svg`.
- `bash scripts/fix/fix-usage.sh` exits 0 and every command it prints reads `/fix …`.
- **End to end:** `bash scripts/fix/fix.sh run_once` completes without any change to the stage switches; a new `fix-*` log exists under `~/.local/logs/fix/`; `python3 scripts/fix/fix_report_parse.py --latest-log` parses it with eval, review, fix, and verify cells populated and reports the run complete; and `/tmp/fix-report.txt` contains a rendered report. `scripts/fix/agent-assignments.conf` is byte-identical before and after.
