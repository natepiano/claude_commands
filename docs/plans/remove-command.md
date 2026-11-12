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

### Phase 1 — /remove_command  · status: todo

#### Work Order

**Goal:** `/remove_command <name>` gives an agent the full steps to find, remove and report a command's whole footprint.

**Spec:**

Write `commands/remove_command.md` in the format of the other top-level commands (frontmatter `description`, one-line purpose, **Usage**, numbered steps). Usage: `/remove_command <command name>`.

Steps it gives:

1. **Inventory.** Search the repository with `git grep -n -i` for the name in every spelling: `ask_a_friend`, `ask-a-friend`, `/ask_a_friend`, and the words with spaces. Then read the command file and search for each term it defines: its scripts folder, policies, targets, config keys and sections, file names and state paths. Read each hit.
2. **Classify** each hit as one of:
   - `dedicated` — exists only for the command: delete the file, key, section or test.
   - `embedded` — a case, row, branch, flag value or sentence inside a shared file: cut only that part.
   - `shared` — other callers use it: keep it; name its owner.
   - `history` — a dated plan, record or as-built history section that describes the past: leave it.
   - `permission` — an allow or deny rule in a settings file that names the command or its files: never edit it. List each one, with its file and line, as a step for the user to remove by hand.
3. **Report** the table to the caller before any deletion: path, line, class, action, owner.
4. **Remove** the `dedicated` and `embedded` parts. Ask the owner first for a file another unit owns.
5. **Verify.** Run the tests of every changed module and lint every changed Python file. Run the step 1 searches again: only `history`, `shared` and `permission` hits remain.
6. **Notify morty** with `python3 ~/.claude/scripts/message/send.py --to morty`: the command name, that it is removed, the commit, and one line on what went with it, for morty's tracking list.
7. **Change-Review line** for the commit: the command is gone from the command list.

The skill is a prompt only. It adds no script and no test.

**Files:**
- `commands/remove_command.md` — new command.

**Workers:** 1 writer; one file, nothing splits.
- `impl` — `commands/remove_command.md`.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** `commands/remove_command.md` exists and gives steps 1–7. `git diff --check` is clean.

### Phase 2 — Remove /ask_a_friend  · status: todo

#### Work Order

**Goal:** `/ask_a_friend` and every part that only it uses are gone; shared parts and permission rules stay.

**Spec:**

Follow `commands/remove_command.md` with `ask_a_friend`, step by step. Post the step 3 table on the board before step 4. Known hits, each classed by the inventory, not by this list:

- `commands/ask_a_friend.md`; `scripts/ask_a_friend/`.
- `scripts/direct/assign.py`: the `friend` policy and the `consultation-pane` target.
- `config/models.conf`: `ask_a_friend.consultation`, `[friend.*]` sections, `assignments/friend`.
- `scripts/agents/models_config.sh`, `scripts/agents/default_models.sh`: the caller-family assignment and its comments.
- Tests: `scripts/agents/test_models_config.sh`, `scripts/agents/test_agent_exec.sh`, `scripts/whoami/test_quota_alert*.py`, `scripts/whoami/quota_alert_test_support.py`, `scripts/lib/test_models_registry.conf`.
- Docs and commands: `commands/default_models.md`, the "pending friend reply" lines in `commands/unit/direct.md` and `commands/unit/worker.md`, `docs/direct/held_astra_roles.md`, `docs/as-built/agent-registry.md`.
- `scripts/herdr/side_pane.py`: keep it if any other caller uses it.
- `.claude/settings.local.json`, `templates/settings_local.json`: `permission`; leave them.

Step 6 sends the morty notice after the director commits; the worker writes its text in the reply instead of sending it.

**Files:**
- Each `dedicated` and `embedded` hit the inventory finds.

**Workers:** 1 writer; the removal crosses many small shared files, and one owner keeps them coherent.
- `impl` — every file in the inventory.

**State:** none.

**Constraints from prior phases:** `commands/remove_command.md` gives the steps and the classes.

**Acceptance gate:** the tests of every changed module pass; basedpyright is clean on every changed Python file; `git grep -n -i 'ask_a_friend\|ask-a-friend'` shows only `history` and `permission` hits.

### Phase 3 — Amend /remove_command  · status: todo

#### Work Order

**Goal:** `/remove_command` gives the steps phase 2 needed, so a second use finds the whole footprint the first time.

**Spec:**

Fold phase 2's lessons into `commands/remove_command.md`: missed spellings, missed places, classes that did not fit, the order that worked, and what the verification needed. Keep it a prompt; add a script only if phase 2 shows a step that a prompt cannot do well.

**Files:**
- `commands/remove_command.md` — amended steps.

**Workers:** 1 writer; one file.
- `impl` — `commands/remove_command.md`.

**State:** none.

**Constraints from prior phases:** phase 2's inventory table and reply list what the first use found.

**Acceptance gate:** each phase 2 lesson is a step or a line in `commands/remove_command.md`. `git diff --check` is clean.
