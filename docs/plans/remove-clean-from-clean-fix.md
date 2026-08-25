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
  - Outside the repo: `~/Library/LaunchAgents/` (plist symlinks), `~/.local/logs/clean-fix/` (run logs), `~/.local/state/clean-fix/` (build timestamps), `~/rust/nate_style/.history/` (durable style state)
- **Key files:**
  - `scripts/clean-fix/clean-fix.sh` — orchestrator, 425 lines. Scope parsing `:22-49`; `TIMESTAMP_DIR` `:58,68`; `project_env_for()` `:96-121`; `checkout_root()` `:124`; build-target helpers `:129-192`; `project_filter_key()` `:165`; conf parse + `BUILD_TARGETS` `:191-250`; `cf_load_stage_enabled clean` `:261`; settings back-populate `:277`; clean block `:279-361`; style stages `:364-400`; completion message `:409`; report activity grep `:412`
  - `scripts/clean-fix/clean-fix-trigger.sh` — launchd wrapper, 45 lines. Scope arg `:21-25`; `IDLE_THRESHOLD_SECONDS` `:27`; pgrep guard `:31-33`; idle gate `:35-43`; exec `:45`
  - `scripts/clean-fix/clean-fix-warmup.sh` — 162 lines, Bevy app warmup. Reads `[settings]`, `[cargo_run]`, `[examples]` from the conf; writes `~/.local/state/clean-fix/` at `:107-108`
  - `scripts/clean-fix/style-fix-monitor.py` — `LOG_DIR` `:38`; watches the run-log directory
  - `scripts/clean-fix/style-fix-manual.sh` — `LOG_DIR` `:25`; header comment `:3`
  - `scripts/clean-fix/tests/` — one `unittest` file today; Phase 4 adds a fixture log and a parser regression test
  - `scripts/clean-fix/com.natemccoy.style-fix.plist` — the only launchd job; `ProgramArguments` `:11-15` still passes the retired `style` scope word
  - `scripts/clean-fix/setup.sh` — installs the one surviving plist. `PLIST_NAMES` `:9-11`; runtime dirs `:16`; the retired-pre-split `OLD_LABEL` cleanup block near `:70` is unrelated history hygiene and stays
  - `scripts/clean-fix/agent_assignments.sh` — stage loading. `cf_load_stage_enabled` `:49`; `cf_load_stage_assignment` `:93-94`; `cf_print_stage_enabled` `:110-116`; `cf_print_agent_assignments` `:118-120`
  - `scripts/clean-fix/agent-assignments.conf` — stage enablement. `[clean]`, `[style_eval]`, `[style_eval_review]`, `[style_fix]`
  - `scripts/clean-fix/clean-fix.conf` — `[settings]`, `[style_eval]`, `[style_fix]`, `[project_env]`, `[build]`, `[projects]`, `[active_checkout]`, `[cargo_run]`, `[examples]`
  - `scripts/clean-fix/clean_fix_report_parse.py` — 2113 lines. `PHASES` `:44`; `COMPLETE_RE` `:97`; `processed` field `:187`; conf project map `:367`; phase-boundary detection `:656-721`; `parse_clean_phase()` `:732-795`; `parse_warmup_phase()` `:795-820`; `.clean-fix-project` marker `:1357`
  - `scripts/clean-fix/clean-fix-usage.sh` — 590 lines. `MARKER` `:8`; `CLEAN_STATUSES` `:17,232,238-262,298,323,347-348`; usage lines `:33-55`; clean row `:461`
  - `scripts/clean-fix/report-render.md` — report prompt. `ROW` format `:26`; `[build]` `:27`; phases note `:156`
  - `scripts/clean-fix/phase_skip.py` — `SCOPE_SECTION` `:31`; `MARKER` `:28`; build branch `:143`; `pass_name` `:268`; scope loop `:288`
  - `scripts/clean-fix/project_add.py` — `[build]` add `:303`
  - `scripts/clean-fix/project_rename.py` — `[build]` handling `:249-275`; keyed sections `:321`; marker glob `:369`
  - `scripts/clean-fix/style-fix-worktrees.sh` — its own `project_env_for()` `:409-420`; marker write `:630,635-637`
  - `scripts/clean-fix/style_history.py` — `PROJECT_MARKER` `:462`
  - `scripts/clean-fix/clean-fix-style-flow.dot` — `cluster_build` `:43-53`; edge chain `:109`
  - `scripts/clean-fix/render-flow.py` — renders the dot to SVG via `neato -n2`
  - `commands/clean_fix.md` — 351 lines. Dispatch `:17`; scopes `:21-42`; notes `:89-95`; add/rename `:115,143`; log detect `:171`; phase sentinels `:197`; monitor note `:230`; stage config `:289-314`; skip `:321-349`
  - `scripts/make_a_worktree/retarget_clean_fix.py` — `[build]` add/revert `:12,16-17,128,163-165,172,181`
  - `scripts/worktree_delete/perform_deletion.sh` — marker read `:40,46-47`
  - `scripts/new_rust_project/rust_generate.sh` — conf enrollment heredoc `:148-183`, `ensure('build', rootdir)` `:181`
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

### Phase 2 — Strip the clean pass from the orchestrator and trigger · status: todo

#### Work Order

**Goal:** `clean-fix.sh` runs only the style pipeline; the trigger has no scope argument and no idle gate; launchd stops passing one; the warmup script and its state directory are gone.

**Spec:**

**`scripts/clean-fix/clean-fix-warmup.sh`** — delete the file. It is called from exactly one place (`clean-fix.sh:358`, removed below) and nothing else reads it.

**Delete the build-timestamp directory** — `rm -rf ~/.local/state/clean-fix`. Verified at plan time that only `clean-fix.sh:307` and `clean-fix-warmup.sh:107-108` read or write it; no style-stage script touches it.

**`scripts/clean-fix/clean-fix-trigger.sh`** — collapses to a concurrency guard plus an exec:

- `:2-17` header comment — rewrite. It currently documents two scopes and the idle gate. It should now say: launchd wrapper for `clean-fix.sh`, invoked every `StartInterval` seconds by `com.natemccoy.style-fix`, with no idle gate so every firing keeps the eval/review/fix queue full; and keep the existing explanation of why the pgrep guard is accurate (the orchestrator runs synchronously start-to-finish).
- `:21-25` — delete `SCOPE="${1:-style}"` and the `case` validating it. The script takes no arguments.
- `:27` — delete `IDLE_THRESHOLD_SECONDS`.
- `:35-43` — delete the entire `if [[ "$SCOPE" == "clean" ]]` block, including the `ioreg`/`awk` HID idle read and its `|| true` SIGPIPE comment.
- `:31-33` — **keep** the `pgrep -f "$CLEAN_FIX_SCRIPT"` guard verbatim.
- `:45` — `exec "$CLEAN_FIX_SCRIPT"` with no scope argument.

**`scripts/clean-fix/com.natemccoy.style-fix.plist`** — launchd still hands the trigger the retired scope word, and this phase is what makes that argument meaningless:

- `:14` — delete the `<string>style</string>` element from `ProgramArguments`. The array keeps `/bin/bash` and the trigger path. Bash would silently ignore the extra argument once the trigger stops reading `$1`, which is exactly why it has to go now rather than being noticed later as a mystery in the residual sweep.
- **Reload the agent explicitly, unsandboxed.** `setup.sh` compares symlink targets, not plist contents, so it reports "Already set up" after an in-place edit and launchd keeps running the version it loaded at boot. Run `launchctl bootout gui/$(id -u)/com.natemccoy.style-fix` followed by `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.natemccoy.style-fix.plist`, both with `dangerouslyDisableSandbox: true`. Tolerate a non-zero `bootout` if the agent is momentarily absent; the `bootstrap` must succeed.
- Leave the XML comment and every other key alone — `StartInterval`, both `/tmp` log paths, and the `com.natemccoy.style-fix` label are all still correct.

**`scripts/clean-fix/clean-fix.sh`** — remove the clean scope entirely (425 → roughly 250 lines):

- `:1-18` header comment — rewrite for the surviving surface. Usage is now `clean-fix.sh [project]` and `clean-fix.sh run_once`. Drop the `clean` and `style` scope descriptions, the 4 AM calendar note, the mend/`lint.conf` paragraph, and the sentence about two launchd triggers sharing one pgrep guard (there is one trigger now).
- `:22-49` scope parsing — collapse. Only two forms remain: an optional bare project filter, and the literal `run_once`. Delete the `clean|style|all` case arm. Keep the `run_once` arm, including `export CLEAN_FIX_FORCE_STYLE_STAGES=1`. Keep `PROJECT_FILTER`. **No variable named `SCOPE` survives this phase.** With one scope left there is no scope to hold, only a single question — was this invocation the one-time override? — so replace it with `RUN_ONCE_REQUESTED`, a `true`/`false` string tested with `==` in the file's existing idiom. Every remaining `$SCOPE` comparison is rewritten to read as the question it is actually asking, not left dangling. Update both usage-error strings.
- `:58` and `:68` — delete `TIMESTAMP_DIR` and its `mkdir -p`.
- `:96-121` — delete `project_env_for()`. `style-fix-worktrees.sh:409-420` carries its own independent copy; this one served only the clean pass.
- `:129-192` — delete `active_redirect_index_for_build_target()`, `project_identity_for_build_target()`, `project_display_for_build_target()`, and `build_target_matches_filter()`. All four exist only to resolve `[build]` entries.
- **Keep** `project_key()`, `checkout_root()` (`:124`), and `project_filter_key()` (`:165`) — the style dispatch at `:367` calls `project_filter_key`, which calls `checkout_root` and `project_key`.
- Conf parser — delete the `BUILD_TARGETS=()` declaration and the `build)` case arm. Keep the `active_checkout)` arm and the `style_eval)` / `style_fix)` stale-setting guards.
- Delete `CLEAN_ENABLED=""` and `:261` `cf_load_stage_enabled clean CLEAN_ENABLED || exit 1`.
- `:277` — **keep** the `backpopulate_settings.py --apply` call and its comment. It runs in every scope and the style agents depend on the permissions it writes. Reword the comment only if it references scopes that no longer exist.
- `:279-361` — delete the whole clean block: the `SKIP: clean/build disabled` log, the `if [[ ... "$CLEAN_ENABLED" == "true" ]]` wrapper, the empty-allowlist guard, `matched_clean_target`, the per-project loop (Cargo.toml check, timestamp skip, `proj_env`, `cargo clean`, `cargo build`, the `lint_config.sh enabled mend` branch and its `cargo mend` call, `touch "$timestamp_file"`, `DONE:`), the `not listed in [build]` log, the `clean-fix-warmup.sh` invocation, and the closing `fi  # SCOPE != style`.
- `:364-400` — unwrap the style section. Delete the `if [[ "$SCOPE" != "clean" ]]` wrapper and the trailing `elif [[ "$SCOPE" == "clean" ]]` / `SKIP: style scope not selected` arm. The three stage blocks (eval, review, fix) and their `|| "$SCOPE" == "run_once"` force conditions stay, rewritten against `RUN_ONCE_REQUESTED`.
- `:409` — the completion log currently reads `=== Clean-fix Rust clean + rebuild complete (${MINUTES}m ${SECS}s) ===`. Change it to `=== Clean-fix complete (${MINUTES}m ${SECS}s) ===`. Keep the `Clean-fix` brand word here: this phase removes the capability, not the name, and Phase 8 changes this string and the regex that matches it together in one commit. Do not anticipate that rename.
- `:412` — the report activity grep is `'(^|[[:space:]])(OK|FAILED|ERROR|TIMEOUT|RECOVERED|Launched|CLEAN|BUILD|MEND):'`. Drop `CLEAN`, `BUILD`, and `MEND` from the alternation; those prefixes can no longer appear.

**`scripts/clean-fix/clean_fix_report_parse.py`** — one coupled edit, and only this one:

- `:97` `COMPLETE_RE = re.compile(r"=== Clean-fix Rust clean \+ rebuild complete \(([^)]+)\) ===")` must be updated in the same phase to match the new completion line: `re.compile(r"=== Clean-fix complete \(([^)]+)\) ===")`. Leaving it for a later phase breaks run-status detection in every report between the two commits.

Do **not** make any other change to `clean_fix_report_parse.py` here — the structural phase surgery is Phase 4.

**Files:**
- `scripts/clean-fix/clean-fix-warmup.sh` — deleted
- `scripts/clean-fix/clean-fix-trigger.sh` — drop the scope argument and the idle gate; keep the pgrep guard
- `scripts/clean-fix/clean-fix.sh` — remove the clean scope, its helpers, and the warmup call
- `scripts/clean-fix/clean_fix_report_parse.py` — `COMPLETE_RE` only, to track the new completion line
- `scripts/clean-fix/com.natemccoy.style-fix.plist` — drop the retired scope argument from `ProgramArguments`

**Reservations:**
- file: `scripts/clean-fix/clean-fix-warmup.sh`
- file: `scripts/clean-fix/clean-fix-trigger.sh`
- file: `scripts/clean-fix/clean-fix.sh`
- file: `scripts/clean-fix/clean_fix_report_parse.py`
- file: `scripts/clean-fix/com.natemccoy.style-fix.plist`

**Constraints from prior phases:**
- Phase 1 removed `com.natemccoy.cargo-clean` (unloaded, symlink deleted, plist deleted) and reduced `setup.sh`'s `PLIST_NAMES` to `com.natemccoy.style-fix.plist` alone. Nothing invokes `clean-fix-trigger.sh` with a `clean` argument any more, so dropping the scope parameter breaks no caller.
- Phase 1 also stopped `setup.sh` from creating `~/.local/state/clean-fix`, so deleting that directory here leaves nothing to recreate it.
- Phase 1's review also rewrote the XML comment in `com.natemccoy.style-fix.plist` so it reads "skips the firing if any clean-fix.sh run is still in flight" — the parenthetical naming the nightly clean job is gone. That comment is already accurate for a single-scope trigger; do not edit it again here. Phase 1 deliberately did not touch `ProgramArguments`, which is why the retired `style` argument is still there for this phase to remove.
- Phase 1 established that an unsandboxed `launchctl` pair is how this repository loads a changed agent, and that `setup.sh` alone will not do it after an in-place plist edit — it printed "Already set up — nothing to do." for exactly that reason during Phase 1's own verification.
- `[clean] enabled=false` is still present in `agent-assignments.conf` and `[build]` is still present in `clean-fix.conf`. Both are removed in Phase 3. `cf_load_stage_enabled clean` is deleted here, so the leftover `[clean]` section is inert between the two phases — that is expected, not a bug.

**Acceptance gate:**
- `bash -n scripts/clean-fix/clean-fix.sh` and `bash -n scripts/clean-fix/clean-fix-trigger.sh` both exit 0.
- `python3 -m py_compile scripts/clean-fix/clean_fix_report_parse.py` exits 0.
- `basedpyright scripts/clean-fix/` prints `0 errors, 0 warnings, 0 notes`.
- `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` prints `OK`.
- `ls scripts/clean-fix/clean-fix-warmup.sh` reports no such file; `ls ~/.local/state/clean-fix` reports no such directory.
- `grep -nE "warmup|BUILD_TARGETS|CLEAN_ENABLED|TIMESTAMP_DIR|cargo clean|cargo build|matched_clean_target|project_env_for|build_target_matches_filter" scripts/clean-fix/clean-fix.sh` returns nothing.
- `grep -n "SCOPE" scripts/clean-fix/clean-fix-trigger.sh` returns nothing; `grep -n "pgrep" scripts/clean-fix/clean-fix-trigger.sh` still matches.
- `grep -n "SCOPE" scripts/clean-fix/clean-fix.sh` returns nothing, and `grep -n "RUN_ONCE_REQUESTED" scripts/clean-fix/clean-fix.sh` matches.
- `plutil -lint scripts/clean-fix/com.natemccoy.style-fix.plist` prints `OK`, and `grep -n "<string>style</string>" scripts/clean-fix/com.natemccoy.style-fix.plist` returns nothing.
- `launchctl list | grep style-fix` (unsandboxed) still shows `com.natemccoy.style-fix` after the reload, and `launchctl print gui/$(id -u)/com.natemccoy.style-fix | grep -A4 arguments` shows two arguments rather than three — proving launchd picked up the edited plist rather than the copy it loaded at boot.
- End-to-end: with all three style stages disabled in `agent-assignments.conf`, `bash scripts/clean-fix/clean-fix.sh` exits 0, and the newest log under `~/.local/logs/clean-fix/` contains the three `SKIP: style … disabled` lines, the new `=== Clean-fix complete (…) ===` line, and no `CLEAN:`, `BUILD:`, `MEND:`, or `WARMUP:` line.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` exits 0 against that log and reports the run as complete rather than crashed (proving `COMPLETE_RE` tracks the new message).

---

### Phase 3 — Drop the clean stage, the build/warmup config, and their helper code · status: todo

#### Work Order

**Goal:** The conf files describe one allowlist and three agent-backed stages, and every script that reads or writes `clean-fix.conf` knows only about the sections that still exist.

**Spec:**

The config deletion and the helper-script cleanup are one commit because splitting them leaves a crashing tree. Verified against the current sources: `project_add.py:303` reaches `[build]` through `add_to_section` -> `section_bounds`, which raises `ValueError("section [build] not found")`; `project_rename.py:273` and `retarget_clean_fix.py:165,181` reach it through `last_content_index` / `_last_content_index`, which call the same bounds helper. None of the three degrades to a no-op on a missing section — all three raise. Deleting `[build]` from the conf in one commit and its readers in the next would break `/clean_fix add`, `/clean_fix rename`, and worktree retargeting for the length of that gap, so both halves land together.

#### Part A — configuration

**`scripts/clean-fix/agent-assignments.conf`** — delete the `[clean]` section (`enabled=false`) together with the three-line comment above it that describes the nightly clean + build + mend pass. The file keeps `[style_eval]`, `[style_eval_review]`, and `[style_fix]`, and its top comment about agent assignments living in `~/.claude/config/agents.conf` is unchanged.

**`scripts/clean-fix/agent_assignments.sh`**
- `:46-48` — the comment above `cf_load_stage_enabled` explains that the clean/build pass runs no agent and so has only a switch. Rewrite it to describe what the helper does now: read the `enabled=` switch for a stage, used internally by `cf_load_stage_assignment` before it resolves the agent row.
- `:49` — **keep** `cf_load_stage_enabled` itself. `cf_load_stage_assignment:93` still calls it for all three style stages.
- `:110-116` — delete `cf_print_stage_enabled`. Its only purpose was rendering the agentless `[clean]` row.
- `:120` — delete the `cf_print_stage_enabled clean` call inside `cf_print_agent_assignments`. The surrounding function and its three `cf_print_stage_assignment` calls stay.

**`scripts/clean-fix/clean-fix.conf`**
- `:1-14` header — rewrite from the two-allowlist model to one. It should state: allowlist model, nothing runs unless listed, no deny list; `[projects]` is the set of projects to style eval/review/fix and supplies each project's identity and history key; `[active_checkout]` optionally redirects a `[projects]` entry's eval/fix at a worktree while identity and history stay with the entry. Delete every reference to `[build]`, to nightly clean/build/mend, and to keeping the two allowlists in sync.
- Delete `[settings]` entirely — its only keys are `warmup_timeout` and `warmup_run_seconds`, both read solely by the deleted warmup script.
- Delete `[build]` and all six entries (`bevy_brp`, `hana`, `cargo-liner`, `nateroids`, `obsidian_knife`, `hana_clerestory_recovery`) plus its explanatory comment block.
- Delete `[cargo_run]` and its two entries, and `[examples]` and its comment. Both were warmup target lists.
- **Keep** `[style_eval]`, `[style_fix]`, `[project_env]`, `[projects]`, and `[active_checkout]` with all their entries and tunables.
- `[project_env]` comment `:47-56` — it currently says the environment is applied to "the clean/build/mend pass, the style-fix agent, and the style-fix build-gate." Drop the clean/build/mend clause. Keep the `RUSTC_BOOTSTRAP=1` / `rustc_private` explanation and the `cargo-mend` note, but drop the sentence about the build pass calling cargo with `--manifest-path` from elsewhere, which described the deleted loop. The remaining consumer is `style-fix-worktrees.sh:409-420`, which looks up the last path segment of the `[projects]` entry — say that.

#### Part B — helper scripts

**`scripts/clean-fix/phase_skip.py`** — the module skips or re-enables targets per phase; with one phase left, the scope argument is vestigial.
- `:31` — delete `SCOPE_SECTION = {"clean": "build", "style": "projects"}` outright. A map with one key is indirection with nothing to resolve. Replace its four read sites (`:155,178,200,218`) with a module constant `PROJECTS_SECTION = "projects"`.
- `:143` — delete the `if section == "build":` branch inside the member-line helper; only the `projects` path remains.
- `:268` — `pass_name = "clean+build" if scope == "clean" else "style eval/fix"` collapses to the constant `"style eval/fix"`.
- `:288` — the `for scope_name in ("clean", "style")` loop over both scopes becomes a single `style` call.
- `:280` — `CliArgs.action: str | None = None` carries `None` only because argparse leaves the subparser dest unset, and `:306` immediately erases it with `args.action or "status"`. Give the field a closed type and resolve the default once at the boundary: declare `PhaseSkipAction = Literal["skip", "enable", "enable-all", "status"]`, type the field `action: PhaseSkipAction` with default `"status"`, and convert argparse's `str | None` into it where the namespace is read — the external-API boundary conversion the type contract allows. No `| None` survives into the module's own code, and `:306`'s `or "status"` disappears with it.
- `:2,7,14-15` — docstring and usage strings: drop the `clean` -> `[build]` line and rewrite the two usage lines.
- Make the scope argument **optional**: `phase_skip.py [skip|enable|enable-all|status] [project ...]` with `style` still accepted as a leading token for backward compatibility with `commands/clean_fix.md`'s existing call sites. Phase 5 updates the command doc to the short form; accepting both keeps the two phases independently green.
- `:28` `MARKER = "#CLEAN_FIX_SKIP#"` — leave it alone. It is renamed in Phase 8.

**`scripts/clean-fix/project_add.py`**
- `:303` — currently `out, build = add_to_section(lines, "build", project, unique_key=False)` followed by a second call for `projects`. Delete the `build` call and its result handling, and update whatever summary text reports which sections were touched so it names only `[projects]`.
- Update the module docstring if it claims the helper adds to both allowlists.
- `:19` `MARKER = "#CLEAN_FIX_SKIP#"` — leave it alone. It is renamed in Phase 8, together with every other reader.

**`scripts/clean-fix/project_rename.py`**
- `:249-275` — delete the entire `[build]` block: `build_indices`, the rename-in-place path, the duplicate-removal path, the append path, and the three `changes.append(f"[build] …")` lines.
- `:321` — `for section in ("project_env", "cargo_run", "examples"):` becomes `for section in ("project_env",):`. Those two sections no longer exist in the conf.
- `:369` — the `*_style_fix/.clean-fix-project` marker glob stays as-is; it is renamed in Phase 8.
- Update the module docstring so it no longer claims to update `[build]`.

**`scripts/make_a_worktree/retarget_clean_fix.py`** — becomes redirect-only.
- `:12` — the docstring line "W is also added to [build] so the worktree builds nightly (build everything)" is deleted.
- `:16-17` — the `apply` and `revert` usage lines drop "+ [build] add" and "+ [build] entry".
- `:128` — delete `build_entries`.
- `:163-165` — delete the insertion of `result["build_add"]` into `[build]`, and stop populating `build_add` and `build_already` in whatever produces `result`.
- `:172` — the `revert` docstring drops the `[build]` entry mention.
- `:181` — delete the loop that removes the worktree's `[build]` entry.
- The `[active_checkout]` redirect logic — detect, apply, revert — is untouched. `--commit` still commits only `clean-fix.conf`.

**Files:**
- `scripts/clean-fix/agent-assignments.conf` — delete the `[clean]` section and its comment
- `scripts/clean-fix/agent_assignments.sh` — delete `cf_print_stage_enabled` and its call site; reword the `cf_load_stage_enabled` comment
- `scripts/clean-fix/clean-fix.conf` — delete `[settings]`, `[build]`, `[cargo_run]`, `[examples]`; rewrite the header and the `[project_env]` comment
- `scripts/clean-fix/phase_skip.py` — single section constant, closed action type, optional scope argument
- `scripts/clean-fix/project_add.py` — add to `[projects]` only
- `scripts/clean-fix/project_rename.py` — drop `[build]`, `cargo_run`, and `examples` handling
- `scripts/make_a_worktree/retarget_clean_fix.py` — redirect-only; no `[build]` add or revert

**Reservations:**
- file: `scripts/clean-fix/agent-assignments.conf`
- file: `scripts/clean-fix/agent_assignments.sh`
- file: `scripts/clean-fix/clean-fix.conf`
- file: `scripts/clean-fix/phase_skip.py`
- file: `scripts/clean-fix/project_add.py`
- file: `scripts/clean-fix/project_rename.py`
- file: `scripts/make_a_worktree/retarget_clean_fix.py`

**Constraints from prior phases:**
- Phase 2 deleted `cf_load_stage_enabled clean` from `clean-fix.sh`, deleted the `build)` arm from its conf parser, and deleted `clean-fix-warmup.sh`. After this phase nothing anywhere reads `[clean]`, `[build]`, `[settings]`, `[cargo_run]`, or `[examples]` except `clean-fix-usage.sh`, which Phase 5 corrects — its `[build]` scan is a read that skips a missing section rather than a bounds lookup that raises, so it stays green in the interim.
- `cf_load_stage_enabled` survives Phase 2 because `cf_load_stage_assignment:93` calls it; do not delete it here.
- `#CLEAN_FIX_SKIP#` (`phase_skip.py:28`, `project_add.py:19`, `clean-fix-usage.sh:8`) and `.clean-fix-project` (`project_rename.py:369`) are deliberately left unrenamed until Phase 8, which renames them together with every reader in one commit.
- `commands/clean_fix.md` still invokes `phase_skip.py` with an explicit `clean` or `style` scope. Accepting `style` as an optional leading token keeps the command working until Phase 5 rewrites it.

**Acceptance gate:**
- `bash -n scripts/clean-fix/agent_assignments.sh` exits 0.
- `bash -c 'source scripts/clean-fix/agent_assignments.sh; cf_print_agent_assignments'` exits 0 and prints exactly three stage rows — `style_eval`, `style_eval_review`, `style_fix` — with no clean row.
- `grep -n "\[clean\]\|cf_print_stage_enabled" scripts/clean-fix/agent-assignments.conf scripts/clean-fix/agent_assignments.sh` returns nothing.
- `grep -n "^\[build\]\|^\[settings\]\|^\[cargo_run\]\|^\[examples\]" scripts/clean-fix/clean-fix.conf` returns nothing.
- `grep -c "^\[projects\]\|^\[active_checkout\]\|^\[project_env\]\|^\[style_eval\]\|^\[style_fix\]" scripts/clean-fix/clean-fix.conf` returns 5, and the 31 `[projects]` entries are unchanged (`git diff` shows no deletion inside that section).
- `grep -rin "whitelist\|blacklist" scripts/clean-fix/clean-fix.conf` returns nothing.
- `basedpyright scripts/clean-fix/ scripts/make_a_worktree/` prints `0 errors, 0 warnings, 0 notes`.
- `python3 scripts/clean-fix/phase_skip.py status` and `python3 scripts/clean-fix/phase_skip.py style status` both exit 0 and print the same project list.
- `python3 scripts/make_a_worktree/retarget_clean_fix.py detect --repo hana --worktree hana_nonexistent` exits without traceback.
- **The three formerly-crashing helpers, against the trimmed conf** — on a temp copy of the edited `clean-fix.conf`: `project_add.py` adds the new entry to `[projects]` and creates no `[build]` section; `project_rename.py --conf <tmp>` renames a `[projects]` entry and reports no `[build]` change; `retarget_clean_fix.py apply` writes only the `[active_checkout]` redirect. All three exit 0 with no `ValueError` and no traceback — this is the check that proves the merge did its job.
- `grep -rn "\"build\"\|'build'\|\[build\]\|cargo_run\|SCOPE_SECTION" scripts/clean-fix/phase_skip.py scripts/clean-fix/project_add.py scripts/clean-fix/project_rename.py scripts/make_a_worktree/retarget_clean_fix.py` returns nothing.
- `bash scripts/clean-fix/clean-fix.sh` still exits 0 with the style stages disabled, and its log shows the three `SKIP` lines and the completion line.
- `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` prints `OK`.

---

### Phase 4 — Remove the clean and warmup phases from the report parser · status: todo

#### Work Order

**Goal:** `clean_fix_report_parse.py` models four phases (eval, review, fix, verify), a checked-in fixture log proves the surviving boundaries still parse identically, and `report-render.md` declares the same four-phase row contract.

**Spec:**

This is the highest-risk edit in the plan: phase-boundary detection is positional and the four phases are interlocked. It needs a regression oracle, and **the plan's original oracle does not exist.** Verified against the live corpus: all 146 retained logs under `~/.local/logs/clean-fix/` are skip-only runs — every one ends `SKIP: style eval disabled` / `SKIP: style eval review disabled` / `SKIP: style fix disabled` and then the completion line. There is not a single `CLEAN:`, `WARMUP:`, `EVAL:`, `REVIEW:`, or `FIX:` line anywhere in the retention window, because the three style stages have been switched off for its whole duration. Copying "the newest log with eval, review, and fix activity" is impossible, and a baseline taken from a skip-only log would exercise none of the code this phase is cutting into.

**Build the oracle instead, and build it before editing the parser.**

1. **Write a fixture log** at `scripts/clean-fix/tests/fixtures/six-phase-run.log`, representative of a full pre-change run: the `=== Starting clean-fix (scope: style) ===` banner, the settings back-population block, then activity lines for all six of the phases the parser currently models — clean, warmup, eval, review, fix, and verify — for at least two projects, ending in the completion line. Derive every line's exact shape from the emitters rather than inventing one: `clean-fix.sh` for the banner, `CLEAN:`/`BUILD:`/`MEND:`/`DONE:`/`WARMUP:` lines and the completion line; `style-eval-all.sh`, `style-eval-review-all.sh`, and `style-fix-worktrees.sh` for the eval, review, fix, and verify lines; and the parser's own detection regexes as the cross-check that each line lands in the phase intended. Include at least one skipped project and one warning so `SkipReason` and `ToolWarning` are exercised too. Note that the fixture is written against the **pre-Phase-2** log vocabulary on purpose — it is the artifact that proves the deleted parsers were unnecessary rather than merely unexercised, which no post-Phase-2 log can do.
2. **Capture the pre-edit baseline** by running the current parser against the fixture and saving its eval, review, fix, and verify per-project cells.
3. **Write the regression test** as a new `unittest` file, `scripts/clean-fix/tests/test_report_parse_phases.py`, asserting that parsing the fixture yields exactly those four phases, that the per-project eval/review/fix/verify cells equal the captured baseline, and that no `clean` or `warmup` phase appears in the result. This is the durable form of the oracle: it lives in the repository, it runs with the existing test suite, and it gives the plan's highest-risk file the coverage it has never had.

Only then edit `scripts/clean-fix/clean_fix_report_parse.py`:

- `:44` — `PHASES: tuple[str, ...] = ("clean", "warmup", "eval", "review", "fix", "verify")` becomes `("eval", "review", "fix", "verify")`.
- `:656-721` — the phase-boundary block. Delete `clean_start`, `warmup_start`, `clean_end`, and `warmup_end` and everything computing them: the `"WARMUP:" in line and "WARMUP KILLING" not in line` detection, the clean-end scan, the warmup-end scan, the `CLEAN: <project>` guard on registering the clean phase, and both `bounds[...]` assignments. `eval` becomes the first phase, so its start is the first line of the log rather than the successor of a clean or warmup boundary. Trace the existing eval-start computation and make it independent of `clean_end` / `warmup_end`.
- `:732-795` — delete `parse_clean_phase()` in full.
- `:795-820` — delete `parse_warmup_phase()` in full.
- Delete both call sites wherever the parser dispatches per phase.
- `:187` — the `processed: int = 0  # clean phase` field on the stats dataclass. Remove it if nothing else reads it; if another phase reads `processed`, keep the field and delete only the trailing comment. Check before deleting.
- `:62` — the comment on the always-excluded set says it covers directories not opted into the `[build]` / `[projects]` allowlists. Rewrite for `[projects]` alone.
- `:367` — the conf reader returning the style-eval project-name to project-root mapping. Confirm it parses only `[projects]` / `[active_checkout]`; if it also scans `[build]`, remove that.
- `:97` `COMPLETE_RE` — already updated in Phase 2. Leave it.
- `:1357` `.clean-fix-project` — leave it. Renamed in Phase 8.

Keep every `SkipReason`, `ToolWarning`, `Warning`, `AgentLimit`, and `Cell` mechanism intact; only the two deleted phases lose their producers.

**`scripts/clean-fix/report-render.md`** — the parser's output schema and the prompt that consumes it change together, in this phase, because nothing else keeps them honest. The render agent is prompted from this file and fed this parser's rows; a commit that ships a four-cell parser against a six-cell contract is a mismatch resting on an untested assumption about how tolerant the agent is.

- `:26` — the `ROW` contract line drops `clean=<cell>` and `warmup=<cell>`, leaving `ROW <project>  eval=<cell> review=<cell> fix=<cell> verify=<cell> reason="…" [phase_now="…"]`.
- `:27` — the `ALWAYS_EXCLUDED` gloss references "the relevant allowlist ([build] / [projects])". Reduce to `[projects]`.
- `:156` — the example `phases not in this log: clean,warmup,eval,review` uses deleted phase names. Rewrite with surviving ones (for example `eval,review`).
- Sweep the rest of the file for any other clean/warmup/build phase reference and correct it; those three line numbers are the known ones, not necessarily the only ones. Leave the `~/.local/logs/clean-fix/` path at `:11` alone — Phase 8 moves the log directory and rewrites that line with every other consumer of the path.

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
- Phase 2 already changed `COMPLETE_RE` (`:97`) to match `=== Clean-fix complete (…) ===` and removed `CLEAN`, `BUILD`, and `MEND` from `clean-fix.sh`'s report activity grep. A log produced after Phase 2 therefore contains no `CLEAN:`, `BUILD:`, `MEND:`, or `WARMUP:` lines. That is one of the two reasons the oracle is a hand-built fixture rather than a captured log; the other is that no captured log has any style activity either.
- The fixture's banner and completion line must match the **pre-Phase-2** forms, since it represents a run from before that phase. `COMPLETE_RE` now matches only the post-Phase-2 completion line, so either write the fixture's completion line in the new form or have the test assert phases rather than run-completion — whichever keeps the test asserting the phase boundaries this phase actually changes. Say which was chosen in the test's docstring.
- The `.clean-fix-project` marker read at `:1357` is renamed in Phase 8, not here.
- `scripts/clean-fix/tests/` currently holds one `unittest` file and no fixtures directory; create `fixtures/` as part of this phase.

**Acceptance gate:**
- `basedpyright scripts/clean-fix/` prints `0 errors, 0 warnings, 0 notes`.
- `python3 -m py_compile scripts/clean-fix/clean_fix_report_parse.py` exits 0.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --list` exits 0 and enumerates the logs under `~/.local/logs/clean-fix/`.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` exits 0.
- **The regression test is the oracle:** `python3 scripts/clean-fix/tests/test_report_parse_phases.py` prints `OK`. It must fail if the parser is reverted to six phases and fail if any eval/review/fix/verify cell drifts from the captured baseline — demonstrate both by temporarily breaking one assertion and observing the failure before committing.
- `python3 scripts/clean-fix/clean_fix_report_parse.py --phase-detect scripts/clean-fix/tests/fixtures/six-phase-run.log` exits 0 and names a phase from the new four-phase set.
- `grep -nE "parse_clean_phase|parse_warmup_phase|clean_start|warmup_start|clean_end|warmup_end|WARMUP" scripts/clean-fix/clean_fix_report_parse.py` returns nothing (the fixture log still contains those strings by design; it is data, not code).
- `grep -nE "clean=|warmup=|\[build\]|clean\+rebuild" scripts/clean-fix/report-render.md` returns nothing.
- `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` prints `OK`.

---

### Phase 5 — Update the user-facing surface: render prompt, usage screen, command doc · status: todo

#### Work Order

**Goal:** `/clean_fix` offers no clean scope, no clean stage switch, and no clean skip list; the usage screen and report prompt describe four phases.

**Spec:**

**`scripts/clean-fix/clean-fix-usage.sh`**
- `:33-34` — the `clean_fix run [project]` description says "Clean/build/warmup and style eval/review/fix"; rewrite as style eval/review/fix. Delete the `clean_fix run clean [project]` line entirely.
- `:43` — delete the `clean_fix clean` status line.
- `:47-50` — `clean_fix on` / `off` say "the clean stage and all style stages"; rewrite as all style stages. The `eval on` / `eval off` lines say "Also works for clean, review, and fix"; drop `clean`.
- `:37` — the `clean_fix add` description says it adds to "clean and style allowlists"; rewrite for the single allowlist.
- `:51-55` — delete all four `clean_fix skip clean …` lines. Keep the `skip style` lines and, if the scope word is now optional per Phase 3, present the short form.
- `:17,232,238-262,298,323,347-348,461` — delete the `CLEAN_STATUSES` array and every use: its initialization, the `if [[ "$column" == "clean" ]]` branch in `set_project_status`, the `tmp_clean` swap in the sort, the `[[ "$section" == "build" ]]` branch in the conf scan (`:308,320`), the `set_project_status … "clean" …` call, the `"clean"` key in the `--json` emitter, and the comment at `:461` about the clean/build pass carrying only a status. The project table loses its clean column; keep the style column and the header alignment consistent.

**`commands/clean_fix.md`** (edit with the Edit/Write tools — `commands/` is a protected path):
- `:17` dispatch line — remove `clean` from the `<StyleAgentConfig/>` token list.
- `:21-42` — delete the `## run [clean | style] [project]` heading variant and the `run clean` forms. The remaining scopes are a bare project filter and `run_once`. Rewrite `:29-30` (the scope bullet list) and delete `:38-39`.
- `:89-95` — the notes. `:93` says `run_once` "does not run clean/build/warmup"; that contrast is now meaningless — rewrite it to say `run_once` is a one-time style-stage override that does not persistently enable any stage.
- `:115` and `:143` — `<Add/>` and `<Rename/>` say the helpers touch `[build]`; correct both to `[projects]` (and `[active_checkout]` plus keyed sections for rename).
- `:171` — remove `/tmp/cargo-clean-stdout.log` from the `<DetectLog/>` candidate list.
- `:197` — the `PHASE <name>` sentinel list drops `clean+rebuild` and `warmup`.
- `:230` — the example "clean → eval → fix all in one orchestrator run" becomes an eval → review → fix example.
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
- `#CLEAN_FIX_SKIP#` and `/tmp/clean-fix-report.txt` keep their current names through this phase; Phase 8 renames them.
- `commands/` is a protected path — use Edit/Write, never shell redirection or `sed -i`.

**Acceptance gate:**
- `bash -n scripts/clean-fix/clean-fix-usage.sh` exits 0.
- `bash scripts/clean-fix/clean-fix-usage.sh` exits 0 and its output contains no `run clean`, no `skip clean`, and no clean column in the project table.
- `bash scripts/clean-fix/clean-fix-usage.sh --json | python3 -m json.tool` exits 0, and the parsed JSON has no `clean` key on any project object.
- `grep -nE "CLEAN_STATUSES|run clean|skip clean|== \"clean\"|== \"build\"" scripts/clean-fix/clean-fix-usage.sh` returns nothing.
- `grep -nE "run clean|skip clean|cargo-clean|cf_print_stage_enabled|clean\+rebuild|\[build\]|\[clean\]" commands/clean_fix.md` returns nothing.
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

**`commands/make_a_worktree.md`** (protected path) — `:101` the offer text asks "Point style eval/fix at this worktree (and add it to the nightly build set)?"; drop the parenthetical. `:107` says the helper "adds `[worktree-name]` to `[build]` and writes the `[active_checkout]` redirect(s)"; rewrite for the redirect alone. Check `:82-96` for any other build-set claim.

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
- `grep -rn "\[build\]" commands/ scripts/ docs/ README.md CLAUDE.md config/` returns nothing.
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
- Every `agents_resolve cleanfix.<stage>` call site — `scripts/clean-fix/agent_assignments.sh:94` and the `agent_exec.sh cleanfix.report` call in `clean-fix.sh`.
- `docs/as-built/agent-registry.md` — `:33` the family/sub-task table row, and `:140-147` the consumer rows, plus every `/agent cleanfix …` example.
- `commands/clean_fix.md` — every `/agent cleanfix <family>` and `/agent cleanfix.<stage>` reference.
- `scripts/clean-fix/clean-fix-usage.sh:45-46` — the two `/agent cleanfix …` usage lines.

**Runtime brand strings — one atomic edit.** `clean-fix.sh`'s completion line and `clean_fix_report_parse.py`'s `COMPLETE_RE` are a matched pair: the script writes `=== Clean-fix complete (…) ===` (set in Phase 2) and the parser's regex is the only thing that recognizes a finished run. Change both here, in this commit, to `=== Fix complete (…) ===` and the regex that matches it. Splitting them across commits makes every report in between call a healthy run crashed. Phase 10's residual sweep will not accept the old wording, so this is the phase that owns it.

**Runtime paths**
- Log directory `~/.local/logs/clean-fix/` -> `~/.local/logs/fix/`, and the legacy single-file symlink `~/.local/logs/clean-fix.log` -> `~/.local/logs/fix.log`. **Migrate the existing directory**: `mv ~/.local/logs/clean-fix ~/.local/logs/fix` and remove the stale `~/.local/logs/clean-fix.log` symlink, so `/clean_fix report` and `list` keep seeing history.
- **Own every reader of that directory in the same commit.** Verified at plan time, these are all of them: `clean-fix.sh:55` (`LOG_DIR`) and `:57` (`LEGACY_LOG`); `clean_fix_report_parse.py:27` (`LOG_DIR`) and its `:7` usage docstring; `style-fix-monitor.py:38` (`LOG_DIR` — the monitor waits on this directory, so a missed rename leaves it watching a path nothing writes to); `style-fix-manual.sh:3,25` (`LOG_DIR` — a manual run would otherwise recreate the old directory the moment someone uses it); `report-render.md:11`; and `commands/style_eval.md:334`. `commands/clean_fix.md` names the path at `:72,82,172,173` and is already in this phase's Files.
- **Log filenames follow the directory.** The orchestrator writes `clean-fix-YYYYMMDD-HHMMSS.log` and `style-fix-manual.sh` writes `style-fix-manual-*.log`. Rename the orchestrator's prefix to `fix-YYYYMMDD-HHMMSS.log`, leave the manual prefix alone (it is named for the surviving style job), and update the two globs that read them: `clean_fix_report_parse.py`'s log enumeration and `commands/clean_fix.md:82,173`. **Retention must match both**: whatever prunes or enumerates logs has to cover the new `fix-*` names *and* the 146 migrated `clean-fix-*` files, which keep their old names after the `mv`. A glob that matches only one of the two either strands the history or never prunes it — say in the code comment which of the two patterns each glob is for.
- Report file `/tmp/clean-fix-report.txt` -> `/tmp/fix-report.txt` — `clean-fix.sh`'s `REPORT_FILE`, `docs/as-built/agent-registry.md:142`, and `commands/clean_fix.md`.
- Leave `/tmp/style-fix-stdout.log` and `/tmp/style-fix-stderr.log` alone; they are named for the surviving launchd job and are already correct.

**Skip marker** `#CLEAN_FIX_SKIP#` -> `#FIX_SKIP#`:
- Readers — all four, verified at plan time: `scripts/clean-fix/phase_skip.py:28` (`MARKER`), `scripts/clean-fix/project_add.py:19` (`MARKER`), `scripts/clean-fix/clean-fix-usage.sh:8` (`MARKER`), and the description in `commands/clean_fix.md`. `project_add.py` uses the marker to tell a deliberately skipped entry from an absent one; leaving it on the old spelling makes it read every skipped project as missing and insert a duplicate active entry beside it. It changes and is tested in this commit with the rest.
- **Migrate the data in the same commit.** Any line already commented out in `clean-fix.conf` carries the old marker; rewrite those occurrences in the conf too. Skipping this leaves temporarily-skipped entries invisible to `enable-all`, which silently strands them.

**Project marker** `.clean-fix-project` -> `.fix-project`:
- Writers and readers: `scripts/clean-fix/style-fix-worktrees.sh:630,635-637` (writes the file and adds it to `.git/info/exclude`), `scripts/clean-fix/style_history.py:462` (`PROJECT_MARKER`), `scripts/clean-fix/project_rename.py:369` (the `*_style_fix/.clean-fix-project` glob), `scripts/clean-fix/clean_fix_report_parse.py:1357`, `scripts/worktree_delete/perform_deletion.sh:40,46-47`, and `commands/style_fix_review.md:229-233`.
- **No data migration is needed**: verified at plan time that no `~/rust/*_style_fix` worktree exists. Re-verify with `ls -d ~/rust/*_style_fix` before editing. If one has appeared since, rename its marker file as part of this phase. Stale `.clean-fix-project` entries left in any repo's `.git/info/exclude` are harmless and need no cleanup.

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

**Constraints from prior phases:**
- Phases 1 through 7 removed the clean capability entirely; every file listed here is at its post-removal state, so a rename sweep will not hit deleted code.
- `commands/` is a protected path — use Edit/Write.
- The agent registry resolves through `scripts/agents/agents_config.sh` and launches through `scripts/agents/agent_exec.sh`; neither is renamed by this plan. Only the family **key** changes, so `agents_resolve` and `agent_exec` call sites change their argument, not their name.
- `docs/as-built/agent-registry.md:174` records that the pipeline scripts run under `#!/bin/bash` (3.2) and the registry scripts under `#!/usr/bin/env bash`. Do not change any shebang.

**Acceptance gate:**
- **Scope every residual grep away from the plan's own text.** This plan doc and its sibling notes under `docs/plans/` quote every old name on purpose, so a bare `grep -rn … .` matches them forever and can never go quiet. Add `--exclude-dir=plans` alongside the existing exclusions to each of the three greps below, and read "no references" as "no references in maintained implementation, command, configuration, or product documentation" — never in the planning record.
- `grep -rn "CLEAN_FIX_" --include='*.sh' --include='*.py' --include='*.md' . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "cleanfix" --include='*.sh' --include='*.py' --include='*.md' --include='*.conf' . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "clean-fix-report.txt\|#CLEAN_FIX_SKIP#\|\.clean-fix-project" . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing.
- `grep -rn "logs/clean-fix" . | grep -v '^./projects/' | grep -v '^./docs/plans/'` returns nothing — this is the check that catches a missed log-directory consumer.
- `grep -n "Clean-fix complete" scripts/clean-fix/clean-fix.sh` returns nothing and `grep -n "Fix complete" scripts/clean-fix/clean-fix.sh` matches; `python3 -c "import re,pathlib; src=pathlib.Path('scripts/clean-fix/clean_fix_report_parse.py').read_text(); assert 'Clean-fix complete' not in src"` exits 0.
- A run's own log proves the pair moved together: after the end-to-end run below, `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` reports it complete rather than crashed.
- `python3 scripts/clean-fix/project_add.py` against a temp conf carrying a `#FIX_SKIP#`-commented entry reports that entry as skipped rather than adding a duplicate.
- `bash scripts/clean-fix/style-fix-manual.sh` (or reading its `LOG_DIR`) writes under `~/.local/logs/fix/`, and `~/.local/logs/clean-fix` is not recreated by any script in the repository.
- `bash -c 'source scripts/clean-fix/agent_assignments.sh; cf_print_agent_assignments'` exits 0 and resolves all three stages through the `fix` family.
- `bash scripts/agents/agents_config.sh` resolution for `fix.style_eval` succeeds (invoke however the other registry tests do; `scripts/agents/test_agents_config.sh` is the reference).
- `bash scripts/agents/test_agents_config.sh` and `bash scripts/agents/test_agent_exec.sh` both pass.
- `ls -d ~/.local/logs/fix` exists and contains the migrated history — all 146 files — and `ls ~/.local/logs/clean-fix` reports no such directory. `python3 scripts/clean-fix/clean_fix_report_parse.py --list` enumerates both the migrated `clean-fix-*` logs and any new `fix-*` log, proving the retention and enumeration globs cover both naming eras.
- `basedpyright scripts/clean-fix/ scripts/make_a_worktree/` prints `0 errors, 0 warnings, 0 notes`.
- `bash scripts/clean-fix/clean-fix.sh` exits 0 with the style stages disabled, writes its log under `~/.local/logs/fix/` with a `fix-` prefix, and `python3 scripts/clean-fix/clean_fix_report_parse.py --latest-log` finds and parses it.
- `python3 scripts/clean-fix/tests/test_style_fix_prompt_comments.py` prints `OK`.

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
- Every remaining `scripts/clean-fix/` path string in `commands/`, `scripts/`, `docs/`, and `README.md`. **This phase owns every path-dependent caller, not just the ones inside the renamed tree** — the residual sweep in Phase 10 is a prose pass, and a stale path is a broken call, not a wording problem. Verified at plan time, the callers outside `scripts/fix/` are: `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, and `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`. Sweep for more rather than trusting the list.

**Re-render the diagram in this commit.** Phase 6 rebuilt the dot source's content; renaming `clean-fix-style-flow.dot` and `.svg` here leaves `render-flow.py` pointing at basenames that no longer exist. Run `cd scripts/fix && python3 render-flow.py`, and if the script hardcodes either basename, fix it here — a rename that leaves the renderer broken is this phase's defect, not the next phase's cleanup. Change no diagram content.

**Prove the generated command skill followed the rename.** `commands/clean_fix.md` is the source for the live Codex skill at `~/.codex/skills/generated-from-claude/clean_fix/SKILL.md`, and the synchronizer removes a stale skill directory when its source command disappears. After `commands/clean_fix.md` becomes `commands/fix.md`, run whatever regenerates that catalog (`scripts/agents/test_sync_codex_catalog.sh` is the reference for how it is invoked) and confirm `generated-from-claude/fix/SKILL.md` exists and `generated-from-claude/clean_fix/` does not. Nothing else in the plan looks at this surface.

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
- `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh` — `scripts/clean-fix/` path strings

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
- file: `scripts/agents/test_sync_codex_catalog.sh`
- file: `scripts/worktree_delete/perform_deletion.sh`
- file: `scripts/new_rust_project/rust_generate.sh`
- file: `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`

**Constraints from prior phases:**
- Phase 8 renamed `CLEAN_FIX_SCRIPT` to `FIX_SCRIPT` but left its **value** pointing at the old path — this phase fixes the value. The same is true of `LOG_DIR`, which Phase 8 already repointed at `~/.local/logs/fix/`; that path is a runtime directory, not a script path, and needs no further change here.
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
- `ls ~/.codex/skills/generated-from-claude/fix/SKILL.md` exists and `ls -d ~/.codex/skills/generated-from-claude/clean_fix` reports no such directory.
- `bash scripts/lint/lint --help` (or the nearest cheap invocation of each renamed caller) exits 0, proving no script is left pointing at `scripts/clean-fix/`.
- `python3 -c "import json,sys; json.load(open('.claude/settings.local.json'))"` exits 0, and `grep -c "scripts/clean-fix" .claude/settings.local.json` returns 0.
- `grep -n "pkill -f 'fix.sh'" .claude/settings.local.json` matches.
- `basedpyright scripts/fix/ scripts/make_a_worktree/` prints `0 errors, 0 warnings, 0 notes` (proving `pyrightconfig.json` still resolves the execution environment).
- `bash -n scripts/fix/fix.sh scripts/fix/fix-trigger.sh scripts/fix/fix-usage.sh scripts/fix/setup.sh` exits 0.
- `bash scripts/fix/setup.sh` exits 0 and reports the agent reloaded; `launchctl list | grep style-fix` (unsandboxed) still shows `com.natemccoy.style-fix`.
- `bash scripts/fix/fix-trigger.sh` exits 0 and executes the pipeline (or exits 0 on the pgrep guard if a run is already in flight).
- `python3 scripts/fix/tests/test_style_fix_prompt_comments.py` prints `OK`.

---

### Phase 10 — Sweep the remaining cross-references and verify end to end · status: todo

#### Work Order

**Goal:** No file outside session transcripts refers to `clean_fix`, `clean-fix`, or `cleanfix`, and one full pipeline run plus a rendered report proves the renamed system works.

**Spec:**

**Command cross-references** (all under the protected `commands/` path — Edit/Write only). Each of these names `/clean_fix` or a `scripts/clean-fix/` path; update every occurrence to `/fix` and `scripts/fix/`:
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
- `scripts/lint/invoke.sh`, `scripts/lint/lint`, `scripts/lint/lint_config.sh`, `scripts/lint/scope.py`, `scripts/delegate/verify.sh`, `scripts/hooks/banned_words_lib.py`, `scripts/agents/clean_agents_conf.sh`, `scripts/agents/test_sync_codex_catalog.sh`, `scripts/new_rust_project/rust_generate.sh`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `scripts/worktree_delete/perform_deletion.sh` — wording only; Phase 9 already fixed their paths
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
- file: `scripts/agents/test_sync_codex_catalog.sh`
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

**Acceptance gate:**
- `grep -rn "clean_fix\|clean-fix\|cleanfix\|CLEAN_FIX\|Clean-fix" . --exclude-dir=projects --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ --exclude-dir=plans` returns nothing, with one permitted class of exception: `setup.sh`'s retired-agent cleanup block referencing the historical label `com.natemccoy.clean-fix`. Confirm each surviving match is that block and nothing else. The `plans` exclusion is deliberate and permanent — this plan and its siblings quote the old names as their subject matter, so the sweep's claim is about maintained implementation, command, configuration, and product documentation, never about the planning record.
- `grep -rn "clean_fix\|clean-fix" projects/-Users-natemccoy--claude/memory/` returns nothing.
- `bash -n` exits 0 on every touched shell script.
- `basedpyright scripts/fix/ scripts/make_a_worktree/ scripts/lint/ scripts/hooks/` prints `0 errors, 0 warnings, 0 notes`.
- `python3 scripts/fix/tests/test_style_fix_prompt_comments.py` prints `OK`.
- `bash scripts/agents/test_agents_config.sh`, `bash scripts/agents/test_agent_exec.sh`, and `bash scripts/agents/test_sync_codex_catalog.sh` all pass.
- `cd scripts/fix && python3 render-flow.py` exits 0 and writes `fix-style-flow.svg`.
- `bash scripts/fix/fix-usage.sh` exits 0 and every command it prints reads `/fix …`.
- **End to end:** `bash scripts/fix/fix.sh run_once` completes without any change to the stage switches; a new `fix-*` log exists under `~/.local/logs/fix/`; `python3 scripts/fix/fix_report_parse.py --latest-log` parses it with eval, review, fix, and verify cells populated and reports the run complete; and `/tmp/fix-report.txt` contains a rendered report. `scripts/fix/agent-assignments.conf` is byte-identical before and after.
