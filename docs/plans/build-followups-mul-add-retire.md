# Retire the multiply-add edit hook

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Removes the multiply-add edit hook from Claude and Codex. The lint step's multiply-add rewrite stays and does the work.

> **As-built disposition: amend** — `docs/as-built/build-followups-mul-add.md`

> **Production: build-followups** — unit `mul_add-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The showrunner, 2026-10-18: "Retire the multiply-add edit hook, and do it in your own unit (mul_add). Send me any settings.json lines it needs as exact lines; I take them to the user."

The 24-hour re-measure of the lint step's rewrite rounds (2026-10-17 16:59:51 EDT to 2026-10-18 16:59:51 EDT) passed both limits: 0 of 147 final clippy verdicts failed on `clippy::suboptimal_flops`, and the sole-cause cost was 0.000 worker-hours per day. The as-built doc records the method and result.

## Decisions (unit director, 2026-10-18, under the decision criteria)

- **The settings.json line is the user's edit.** The showrunner takes the exact lines to the user; this plan never edits `settings.json`. The lines went to the showrunner on 2026-10-18.
- **One checkpoint, merged with the settings change.** The checkpoint deletes `post-tool-use-mul-add.py`. A live settings entry that names a missing script fails on every Claude edit, so the showrunner merges the checkpoint only with or after the user's settings change. Risk: the checkpoint waits on the user. One revert undoes it.
- **The Codex installer removes the installed handler.** Users do not edit `~/.codex` by hand.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-mul-add` on branch `build-followups-mul-add` (unit `mul_add-unit` of production `build-followups`).
- **Project started:** 2026-10-18T22:44:42+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:**
  - `scripts/hooks/post-tool-use-mul-add.py` — the hook entry for Claude `Edit|MultiEdit|Write` and Codex `apply_patch`; deleted by this plan
  - `scripts/hooks/mul_add_lib.py` — the detector; only the hook entry and its tests import it; deleted by this plan
  - `scripts/hooks/test_mul_add.py` — the hook's tests; deleted by this plan
  - `scripts/hooks/fn_length_lib.py` — the shared edit parser, Rust scanner and Cargo lint reader; the function-length hook keeps using it
  - `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py` — install and trust the Codex `apply_patch` handlers; the list holds `CodexHook("mul_add", "post-tool-use-mul-add.py")` at line 39
  - `settings.json` — the Claude hook registration (line 196); not edited by this plan
  - `scripts/delegate/verify.sh`, `scripts/lint/invoke.sh`, `scripts/lint/apply_mul_add.py` — the lint step's rewrite rounds; they stay unchanged
- **Key files:** `docs/as-built/build-followups-mul-add.md`; `docs/as-built/build-followups-fn-length-hook.md` (the Codex hook facts and the shared parser).
- **Test lanes:** `scripts/hooks/` — `test_*.py` beside the scripts.
- **Build:** none in the repository.
- **Test:** from the worktree root: `python3 -m unittest discover -s scripts/hooks -p 'test_fn_length.py'`; `python3 -m unittest discover -s scripts/hooks -p 'test_codex_hooks.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes` (it exits 3 in every checkout; the status says nothing). Run it once after the edits, fix what it names, and never re-run it on an unchanged tree.
- **Style:** none — not Rust in the repository.
- **Invariants:**
  - Tests never write the real `~/.codex` or `~/.local/state`, and never run the real `codex` or `cargo`: fixtures are synthetic.
  - `clippy::suboptimal_flops` stays enabled in Cargo (the user, 2026-10-06). The lint step's rewrite is unchanged.
  - The function-length hook's behavior does not change, and `test_fn_length.py` passes.
  - Python is typed throughout with no `Any` and no file-level type ignores (the user's `CLAUDE.md`).
  - The words in `~/rust/nate_style/rust/forbidden-words.md` never appear in code, comments or docs.
  - Write prose in Simplified Technical English (the user, 2026-10-18).

## Phases

### Phase 1 — Remove the hook  · status: done

#### Work Order

**Goal:** no Claude or Codex edit runs the multiply-add hook, and no code for it remains.

**Spec:**
- Delete `scripts/hooks/post-tool-use-mul-add.py`, `scripts/hooks/mul_add_lib.py` and `scripts/hooks/test_mul_add.py`.
- In `scripts/hooks/codex_hooks.py`, remove the `mul_add` handler from the installed list. The installer also removes an already installed `mul_add` handler (matched by its script name `post-tool-use-mul-add.py`) from the Codex hook configuration it manages, and leaves every other handler as it is. A second run changes nothing.
- In `scripts/hooks/fn_length_lib.py`, remove code that only the multiply-add hook used, such as the `suboptimal_flops` lint scope. Keep everything the function-length hook uses. Use grep to prove each removed name has no caller left.
- Remove the multiply-add hook from comments and docstrings that name it, in `scripts/hooks/`.
- Do not edit `settings.json`.

**Files:**
- `scripts/hooks/post-tool-use-mul-add.py`, `scripts/hooks/mul_add_lib.py`, `scripts/hooks/test_mul_add.py` — deleted
- `scripts/hooks/codex_hooks.py`, `scripts/hooks/test_codex_hooks.py`
- `scripts/hooks/fn_length_lib.py`, `scripts/hooks/test_fn_length.py`

**Workers:** 1 writer + 1 adversary review.
- `impl` — every file above

**Acceptance gate:** `test_codex_hooks.py` covers three cases with a synthetic Codex configuration: a fresh install lists no `mul_add` handler; an install over a configuration that holds the old `mul_add` handler removes it and keeps the others; a second install changes nothing. `test_fn_length.py` and `test_codex_hooks.py` pass. `basedpyright` reports `0 errors, 0 warnings, 0 notes` on each changed file. `grep -rn -E 'mul_add_lib|post-tool-use-mul-add' scripts` finds only the installer's stale-handler match.
