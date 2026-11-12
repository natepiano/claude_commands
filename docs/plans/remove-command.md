# Remove a command — /remove_command, then /ask_a_friend

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A `/remove_command` skill finds everything a command owns, removes it, and tells morty. Its first use removes `/ask_a_friend`.

## Delegation Context

- **Project:** `~/.claude` commands and scripts.
- **Project started:** 2026-11-12T17:25:00+00:00
- **Stack:** Markdown commands, Python 3.11+ (basedpyright strict), zsh and bash scripts.
- **User request (2026-11-12, via sdlc):** remove `/ask_a_friend`, its dedicated scripts, its config, its tests and its embedded test cases. Make `/remove_command` first. It assesses the blast area, removes it, and notifies morty, who tracks the removal on its list. Run it on `/ask_a_friend`, learn, and amend it. Keep script and test support small.
- **Ownership:**
  - panes owns `scripts/herdr/*`. Tell the director before a change to a shared herdr file; the director asks sdlc. Remove only parts that only `/ask_a_friend` uses.
  - models owns the model registry (`config/models.conf`, `scripts/lib/models_registry.py`, `scripts/agents/models_config.sh`).
- **User go for config:** "any config specific to ask_a_friend can be removed".
- **morty:** a Codex session. Reach it only with `python3 ~/.claude/scripts/message/send.py --to morty`.
- **Command style:** read `/succinct_style` (`~/.claude/commands/succinct_style.md`) before a command edit.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest <modules>`; shell tests run with `bash <file>`.
- **Lint:** `uvx basedpyright <changed Python files>` — `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Do not change `config/copies.conf`, `config/direct.conf` or any permission setting (`settings*.json`, `templates/settings_local.json`).
  - The user adds no checks: add no guard, refusal or scan that a Work Order does not name.
  - Processes clean up after themselves.

## Phases

### Phase 1 — /remove_command  · status: done

#### As-built

- `commands/remove_command.md` (`/remove_command <command name>`) is a prompt only, with no script and no test.
- It gives seven steps: inventory, classify, report, remove, verify, notify morty, and the Change-Review line.
- The inventory uses `git grep -n -i --untracked` for every spelling. It also searches the ignored local settings (`.claude/settings*.json`, `templates/settings*.json`).
- The classes are `dedicated`, `embedded`, `shared`, `history` and `permission`.
- A `permission` rule is never edited. The report gives each one to the user as a numbered step, with its file, line and exact text.
- The morty notice goes through `send.py --to morty` after the removal commit exists.
- The Change-Review line has three fields: `/<command name> is removed | <machine>, any session: type /<command name> | The command is not in the command list`.

**Files:**
- `commands/remove_command.md` — the command.

**Binds later work:** Remove /ask_a_friend follows the seven steps and the five classes. Amend /remove_command edits this file.

### Phase 2 — Remove /ask_a_friend  · status: done

#### As-built

- `/ask_a_friend` is gone: `commands/ask_a_friend.md` and `scripts/ask_a_friend/prepare_session.sh` are deleted.
- `config/models.conf` has no `ask_a_friend` assignment, no consultation sections and no `caller` assignment mode.
- `scripts/agents/models_config.sh` and `scripts/agents/default_models.sh` have no caller-family mode (`AGENTS_CALLER_*` and its helpers).
- `scripts/direct/assign.py` accepts `--policy astra-reviewer|astra-designer` only. The `consultation-pane` target stays, because Astra reviews use it.
- `scripts/herdr/side_pane.py` has no `open` CLI subcommand. Its `open()` function stays for `assign.py`. `test_side_pane.py` tests `side_pane.open` directly through an `astra-reviewer` policy fixture.
- Tests, fixtures and docs lost their friend and caller cases. `test_models_registry.py` uses `direct=claude` in place of the removed `caller` value.
- Five permission rules that name the command stay for the user to remove by hand: `.claude/settings.local.json` and `templates/settings_local.json`.

**Files:**
- `config/models.conf`, `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh` — no caller mode.
- `scripts/direct/assign.py`, `scripts/herdr/side_pane.py` — no friend policy, no `open` subcommand.
- Tests and fixtures under `scripts/agents/`, `scripts/herdr/`, `scripts/lib/`, `scripts/whoami/`, `scripts/direct/` — friend and caller cases removed.
- `commands/default_models.md`, `commands/unit/direct.md`, `commands/unit/worker.md`, `docs/direct/held_astra_roles.md`, `docs/as-built/*` — references removed.

**Gotchas:** a test of a removed CLI path can also cover a live function; such tests move to the function.

### Phase 3 — Amend /remove_command  · status: done

#### Work Order

**Goal:** `/remove_command` gives the steps phase 2 needed, so a second use finds the whole footprint the first time.

**Spec:**

Fold phase 2's lessons into `commands/remove_command.md`: missed spellings, missed places, classes that did not fit, the order that worked, and what the verification needed. Keep it a prompt; add a script only if phase 2 shows a step that a prompt cannot do well.

Add the showrunner's rule (sdlc, 2026-11-12): a dead reference in another unit's file needs no go. Delete it, then send the showrunner one line that names the file. Ask the owner first only for a change to live behavior in that file.

**Files:**
- `commands/remove_command.md` — amended steps.

**Workers:** 1 writer; one file.
- `impl` — `commands/remove_command.md`.

**State:** none.

**Constraints from prior phases:** phase 2's inventory table and reply list what the first use found.

**Acceptance gate:** each phase 2 lesson is a step or a line in `commands/remove_command.md`. `git diff --check` is clean.
