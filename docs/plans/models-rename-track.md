# models-unit: rename-track phases from herdr-unit's plan

> **Production:** sdlc
> **Source:** `docs/plans/herdr-worker-panes.md` on `natedev-herdr` (herdr-unit). sdlc moved these three items to models-unit on 2026-11-02 08:2x EST, on the user's ask to make rename land sooner.

## Delegation Context

- **Project:** `~/.claude` scripts — the agent tooling.
- **Project started:** 2026-11-02T13:25:43.957+00:00
- **Stack:** Python 3.11+ through the `scripts/lib/py` shim, `unittest`, basedpyright; taplo 0.10.
- **Source plan:** read its Delegation Context in `/home/natepiano/worktrees/claude-natedev-herdr/docs/plans/herdr-worker-panes.md`, mainly **Staff design (2026-10-31)**, **One identity join**, **No hard-coded config**, **Rerun-safe tools**, **One file writer** and **Isolation gate**. They apply to every phase here unchanged.
- **Files this unit holds:** `scripts/production/staff_sources.py` and `scripts/production/staff_test_support.py`, for 3k and Phase 4 (sdlc, 2026-11-02).
- **Landing:** each phase lands alone through its own gate.
- **Order (sdlc, 2026-11-02):** Phase 4 is on the rename path (devtools-unit's 3t waits for it); 3k is needed only before herdr 4c. When herdr-unit's 4h merges into `sdlc`, Phase 4 goes ahead of whatever is left of 3k.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED -u REFUSAL_WATCH_INVENTORY HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py ~/.claude/scripts/production/merge_gate.py --tests-only --checkout <tree>`, run from `~/.claude` main's copy after a merge of `origin/sdlc`.
- **Lint:** `basedpyright <changed Python files> <every caller module of a changed file>`; pass is `0 errors, 0 warnings`.
- **The four sdlc lines (2026-10-30):** good engineering first; no copies; when many things break the same way, the copy is the cause; fix at the cause.

## Phases

### Phase 3k — `.unit.toml` writers emit taplo-formatted text  · status: done

**Depends on:** herdr Phase 3q (landed). Lands before the configuration-completeness audits (herdr 4c, 4d). Live repair (sdlc, 2026-10-31): the 16:24 fill wrote `.unit.toml` in a format that `taplo fmt --check` rejects under each repository's own `taplo.toml` (`align_entries`, `reorder_keys`).

#### As-built

- `unit_config.write`, `write_parked` and `park` emit the text that `taplo fmt --no-auto-config [--config <file>] --stdin-filepath <path> -` gives, and return `Published | AlreadyCurrent`.
- `_publish` compares formatted bytes, so a file with the right values in the wrong format is rewritten. `_formatted` checks that the formatted text decodes to the same values.
- `repository_formatting(worktree)` reads the repository's `.taplo.toml` or `taplo.toml` into `RepositoryTaploConfig(text)`, else `DefaultTaploConfig`. `UnitConfig.formatting` holds it (`compare=False`); parked files save it under the `taplo_config` key.
- A parked file takes its format from the live recorded start folder's repository config, else its saved `RepositoryTaploConfig`, else the caller's config.
- The staff fill prints `changed` or `unchanged` from the `Published` result, and rewrites existing live configs through `write`. `retirement_plan(..., check_format=...)` checks format only on dry runs.
- A taplo failure raises to the caller; unformatted text is never written.

**Files:**
- `scripts/production/unit_config.py` — formatting, `RepositoryTaploConfig`, `DefaultTaploConfig`, `Published | AlreadyCurrent`.
- `scripts/production/staff_sources.py` — fill report and rewrite.
- `scripts/production/unit_config_format_test_support.py` — aligning-config fixtures and a counting taplo wrapper.
- `scripts/production/test_unit_config.py`, `scripts/production/test_staff_sources.py` — tests that run real taplo on writer output.

**Binds later work:** parked files carry `taplo_config`; a reader older than this change rejects that key.

**Gotchas:** `unit_config` calls taplo through its own `run_taplo` alias of `subprocess.run`. `add_unit_test_support` fakes `subprocess.run` for every command that is not git, and the alias keeps that fake away from taplo.

### Phase 4 — Member names come from the staff row  · status: done

**Depends on:** herdr-unit's 4h checkpoint merged into `sdlc`. Must land before devtools-unit starts 3t and before herdr-unit's 4e. After herdr-unit's 3a lands, it also touches `scripts/shutdown/restart.py`.

#### As-built

- `add_unit.py`, `add_showrunner.py` and `resume_unit.py` take the staff row's name for `-n`, the tab title and the pane title.
- `scripts/shutdown/launch_names.py` holds `LaunchNames`: one staff snapshot per restore or restart, rows selected by native resume id, one owner ssh read on the Mac. An unreadable snapshot reuses each saved display name and prints one problem line. A missing row names the entry and keeps its saved name. No row is selected by a saved name.
- `pane_restore.py`, `restart.py` and `stop_resume.py` title a resumed pane and its tab from that snapshot inside `herdr.restore_staff(...)`. An empty name never clears a title. An existing pane is retitled when its tab title is stale, also when its pane label already matches.
- `pane_record.without_legacy_identity` is public; no launch or restart record sets or keeps `STAFF_ID`.
- `herdr.py` drops `STAFF_ID`, `CLAUDE_CODE_SESSION_ID` and `CODEX_THREAD_ID` from every agent environment. `copy_claude_trust` writes through `write_text_atomic(new_file_mode=0o600)`; `atomic_file.write_text_atomic` takes an optional `new_file_mode`.
- `herdr_tab_title.py` resolves a session by its native runtime id. A unit keeps its row name on the tab and pane, and a mismatch prints the `/showrunner:rename_unit` command. Showrunners and workers keep their row titles; a session with no row keeps the ad hoc title. The hook writes no staff row and never stops the turn. It prints the missing-row line only when an incomplete clear is proved.

**Files:**
- `scripts/production/add_unit.py`, `add_showrunner.py`, `resume_unit.py` — row names at launch and resume.
- `scripts/shutdown/launch_names.py` — `LaunchNames` and its read states.
- `scripts/shutdown/pane_record.py`, `pane_restore.py`, `restart.py`, `stop_resume.py` — restore names and titles from one snapshot.
- `scripts/herdr/herdr.py`, `scripts/lib/atomic_file.py` — identity-free agent environment; mode for a new atomic file.
- `scripts/hooks/herdr_tab_title.py` — the hand `/rename` rule.
- Tests: `test_add_unit.py`, `test_add_unit_restore.py`, `test_add_showrunner.py`, `test_resume_unit.py`, `test_herdr.py`, `test_atomic_file.py`, `test_herdr_tab_title.py`, `test_pane_record.py`, `test_pane_restore.py`, `test_restart.py`, `test_stop_resume.py`, `test_unit_watchdog.py`.

**Binds later work:** a launched agent gets no identity variable; its identity comes from the native runtime join. Launch titles go in the `NewTab` destination of `herdr.launch_prepared_agent`.

**Gotchas:** `pane_record` must not import `session_staff` (import cycle); `LaunchNames` lives in its own module for that reason. A test that patches `Herdr.retitle` must expect the pane id and the row name.

**Ruled out:** selecting a restore row by its saved name; writing a staff row from the title hook.

### Phase 4m — Port audit: every unit's port is in its `.unit.toml`  · status: done

**Depends on:** herdr-unit's 4h shrink, which writes this Work Order.

#### Work Order

Copied from herdr-worker-panes.md Phase 4m at phase start (2026-11-02).

**Goal:** before 4c deletes the Units table, every live and retired unit has its port in its `.unit.toml`, from a recorded source, never from a likely value.

**Spec:**
1. List each staff unit row with its configuration port and the Port cell from its bound production document.
2. Read active configuration from the worktree and retired configuration from its parked file; use `cleanup=False` for every audit read.
3. Read remote configuration on its recorded machine; record the source path and bound-document evidence.
4. Report `AppPort`, valid `NoAppPort`, `ConfigMissing`, or `unavailable: <cause>`; never treat missing configuration as `NoAppPort`.
5. List a missing or different port for sdlc; the audit writes nothing and guesses no value.
6. Add one guarded port repair in `unit_config`, taking the recorded old value and sdlc's settled value.
7. Require the recorded old value before publication; accept an already settled value as `unchanged`, and refuse any third value.
8. Use this repair for active and parked configuration; preserve retirement facts, responsibilities, and repository formatting.
9. Publish through the shared atomic writer; a rerun after interruption finishes once.
10. After repair, a rerun of the audit prints no gap and exits 0.

**Files:**
- `scripts/production/audit_unit_ports.py` — new: the read-only audit command.
- `scripts/production/unit_config.py` — the guarded port repair.
- `scripts/production/test_audit_unit_ports.py`, `scripts/production/test_unit_config.py` — tests and fixtures.
- `scripts/production/staff.py`, `scripts/production/production_doc.py`, `scripts/shutdown/remote.py` — read only.

**Workers:** `1 writer + 1 tester`
- `impl` — the audit command and guarded repair; hub: `scripts/production/unit_config.py`.
- `test` — their tests and fixtures.

**Acceptance gate:** the audit prints no gap on natedev and the Mac; a rerun changes nothing and exits 0; full gate green.

**Acceptance gate (port evidence):** test valid `NoAppPort`, missing configuration, bound-document evidence, parked files, and unavailable remote reads.
Interrupt the guarded repair before and after publication; reruns preserve retirement facts and responsibilities.
Test unchanged reruns and refusal when the recorded old value differs.

**Note (sdlc, 2026-11-02, user rule):** readers read the staff page `~/.local/state/staff/staff.json` directly, not `staff.py show --json --live`. A change to the staff.json format bumps its version and tells cargo-liner.

**Note for the Work Order (sdlc, 2026-11-02, from herdr's architect review):** `unit_config.write_parked` refuses a changed port. This phase adds one guarded port repair in `unit_config` that takes the old value and the settled value, with interrupt and rerun tests. Phase 4 lands first.
