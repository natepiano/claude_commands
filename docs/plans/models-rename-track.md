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

#### Work Order

**Goal:** `unit_config.write` and `write_parked` emit the text that `taplo fmt` gives under the target repository's own config, and a fill rerun rewrites each existing file that differs; a second rerun changes nothing.

**Spec:**
1. `unit_config._publish` (`unit_config.py:239`) compares formatted bytes, not parsed values, so a file with the right values in the wrong format is rewritten.
2. The report says `changed` or `unchanged` from that byte comparison.
3. The format comes from the repository's own `taplo.toml`; a parked file whose worktree is gone uses the repository config recorded for it, else the default.

**Files:**
- `scripts/production/unit_config.py`, `scripts/production/staff_sources.py` — formatting and publication.
- `scripts/production/test_unit_config.py`, `scripts/production/test_staff_sources.py` — tests and aligning-config fixtures.

**Workers:** `1 writer + 1 tester`
- `impl` — `unit_config.py` and `staff_sources.py`.
- `test` — the tests and fixtures.

**State:** `.unit.toml` files in each unit worktree and parked files under `~/.local/state/staff/units/`; the rewrite is rerun-safe.

**Constraints from prior phases:** herdr Phase 3h review (2026-11-01): Phase 3q `UnitConfig`; isolation gate.

**Acceptance gate:** basedpyright 0/0 on changed files and callers; `test_unit_config*` and `test_staff_sources*` pass, with a test that runs taplo on the writer's output under an aligning config; the live rerun rewrites the files, `taplo fmt --check .unit.toml` passes in each filled worktree, and a second rerun prints `unchanged` with no byte changed.

### Phase 4 — Member names come from the staff row  · status: todo

**Depends on:** herdr-unit's 4h checkpoint merged into `sdlc`. Must land before devtools-unit starts 3t and before herdr-unit's 4e. After herdr-unit's 3a lands, it also touches `scripts/shutdown/restart.py`.

#### Work Order

Copy the Work Order of herdr plan Phase 4 (`herdr-worker-panes.md`, "Phase 4 — Member names come from the staff row") into this section at phase start, from the herdr plan as it stands then.

### Phase 4c-port — Port audit for herdr Phase 4c  · status: todo

**Depends on:** herdr-unit's 4h shrink, which writes this Work Order.

#### Work Order

herdr-unit writes it in its 4h shrink. Copy it here at phase start.
