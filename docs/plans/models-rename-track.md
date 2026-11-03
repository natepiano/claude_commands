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

#### Work Order

**Goal:** every launch and every restore of a member takes its name from its row, and a hand `/rename` in a unit director's pane leaves the row and the tab as they are and prints the rename_unit command.

**Spec:**

1. **Launch and restore names.** `add_unit.py`, `add_showrunner.py` and `resume_unit.py` use the row's name for `-n` and tab titles. A restore takes one snapshot, selects each row by its native resume id, and reads the current name. No launch or restart record sets or preserves `STAFF_ID`. On an unreadable snapshot, reuse each saved display name and print one problem line; on a missing row, name that entry and use its saved name. Never select a row by a saved name.
2. **A hand `/rename`.** `herdr_tab_title.py` resolves the session by its native runtime id. For a unit, keep its tab and pane on the row name and print `/showrunner:rename_unit` on a mismatch; write no staff row. The session record may keep the typed name until its next launch. Showrunner titles change only through the production rename command. Worker labels stay their short names. A session with no row keeps the existing ad hoc title behavior. Row kind, not inherited marks, separates these cases.
3. **One runtime join.** Read Claude identity from the exact process registry or hook input and Codex identity from the thread runtime. Use the Phase 3h resolver once per snapshot. A hook that runs while a clear handoff is incomplete lists that missing-row state and changes no staff record. Keep the shutdown unit's resume identity and account evidence; do not add an identity environment variable.

**Files:**
- `scripts/production/add_showrunner.py`, `scripts/production/add_unit.py`, `scripts/production/resume_unit.py` — the row's name for `-n` and the tab title; native identity lookup.
- `scripts/hooks/herdr_tab_title.py`, `scripts/hooks/test_herdr_tab_title.py` — a hand `/rename` of a unit keeps the row's name and prints the command.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — environment isolation prevents inherited identities.
- `scripts/shutdown/pane_record.py`, `scripts/shutdown/pane_restore.py`, `scripts/shutdown/test_pane_record.py`, `scripts/shutdown/test_pane_restore.py` — native resume id and restore name; the shutdown unit's files.
- `scripts/production/test_add_showrunner.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_resume_unit.py` — launch-name and native-id tests.
- `scripts/production/staff.py`, `scripts/production/staff_sources.py`, `scripts/production/staff_test_support.py`, `scripts/lib/atomic_file.py` — read only.
- `scripts/shutdown/restart.py`, `scripts/shutdown/stop_resume.py`, `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_stop_resume.py` — the launch-name audit (Phase 3h review).

**Workers:** `2 writers` — split by launch path and hook.
- `impl` — launch names and runtime joins: `add_unit.py`, `add_showrunner.py`, `resume_unit.py`, `herdr.py`, `pane_record.py`, `pane_restore.py` and their tests.
- `test` (opens as impl) — the hook (`herdr_tab_title.py`, `test_herdr_tab_title.py`).

**State:** names shown by launches, hooks and restores. No new identity state.

**Constraints from prior phases:** Phase 3h owns native identity and the shared runtime join; Phase 3r owns current rows and configuration. Shutdown owns restart and pane records. Use one snapshot, including one owner ssh read on the Mac. Use `staff_test_support.py` and the shared atomic writer. This phase does not repeat identity backfill.
- Phase 3h review (2026-11-01): native admission and most restore identity shipped in 3h; do not repeat them. Keep `pane_record.resume_launch`'s fixed-reference to current-session conversion. Test a renamed row whose current session differs from its id.

**Assertion-Change:** `test_add_unit_restore.AddUnitRestoreTests.test_restore_keeps_claude_name_and_sanitizes_herdr_agent_name` expects the row name `alpha`, not the saved session name, in the tab rename, pane rename, `--remote-control` and `-n` (unit director ruling, 2026-11-02: Spec item 1).

**Assertion-Change:** `test_resume_unit.ResumeUnitTests.assert_resumed`, `test_retired_as_built_without_a_header_is_passed_to_the_resume_launch` and `test_folder_note_resumes_in_the_start_folder_and_restores_the_unit_worktree` expect the row name `alpha`, not `alpha-unit`, as the resume launch name (unit director ruling, 2026-11-02: Spec item 1).

**Acceptance gate:** basedpyright 0/0 on changed files and callers. Launch tests prove row names and no `STAFF_ID`. Hook tests cover unit mismatch, showrunner, worker, missing row and delayed clear; no hook writes staff. Restore tests use a renamed native-id row, several Mac entries with one ssh read, remote failure and a missing row. The pane writer uses `atomic_file`; no own `os.replace` stays. A real throwaway unit rename and restart keeps its tab, pane and terminal and takes its new row name; a finished rerun changes nothing. A real hand `/rename` prints the unit rename command and keeps the row name. Full gate green.

### Phase 4m — Port audit: every unit's port is in its `.unit.toml`  · status: todo

**Depends on:** herdr-unit's 4h shrink, which writes this Work Order.

#### Work Order

herdr-unit writes it in its 4h shrink. Copy it here at phase start.

**Note for the Work Order (sdlc, 2026-11-02, from herdr's architect review):** `unit_config.write_parked` refuses a changed port. This phase adds one guarded port repair in `unit_config` that takes the old value and the settled value, with interrupt and rerun tests. Phase 4 lands first.
