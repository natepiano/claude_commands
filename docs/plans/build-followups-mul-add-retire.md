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

#### As-built

- No Claude or Codex edit runs the multiply-add hook. `post-tool-use-mul-add.py`, `mul_add_lib.py` and `test_mul_add.py` are gone. The lint step's multiply-add rewrite is unchanged.
- `codex_hooks.py` installs and trusts only the fn-length and berth handlers. On install it removes each handler whose `command` equals `RETIRED_COMMAND` exactly (the command the old installer wrote, any timeout). It keeps each group in place, even when the group is empty, so other groups keep their indexes. It prints one stderr warning, which names the file, when another handler still names the script. The warning does not change the exit status.
- A removal that moves a sibling handler carries that handler's trust to its new key. Before the rewrite, the installer lists the trusted, enabled entries of the file by event, matcher, command, timeout and hash. After the rewrite, it writes one `hooks.state` upsert for each entry that is now untrusted and matches one of them. An install that removes nothing makes the same app-server calls as before.
- `fn_length_lib.py` no longer holds the replacement payload or the `no_std` helpers. The function-length hook still gets the final Rust paths in payload order.

**Files:**
- `scripts/hooks/codex_hooks.py` — installs and trusts the Codex `apply_patch` handlers, and removes the retired handler
- `scripts/hooks/test_codex_hooks.py` — install, trust and cleanup tests against a fake app-server
- `scripts/hooks/fn_length_lib.py`, `scripts/hooks/test_fn_length.py` — the shared edit parser, Rust scanner and Cargo lint reader

**Gotchas:**
- A Codex trust key holds the group and handler index, but the trusted hash does not. A removal inside a group moves the handlers after it to new keys.
- `settings.json` names the deleted script until the user edits it. The checkpoint merges only with that edit or after it.

**Ruled out:** a script-name match on any argument, because it removes unrelated commands and misses a shell suffix.
