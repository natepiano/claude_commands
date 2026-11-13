# Remove command dry run — show what a removal would touch

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/remove_command <name> --dry-run` shows the full footprint of a command and an impact summary, and changes nothing.

## Delegation Context

- **Project:** `~/.claude` commands.
- **Project started:** 2026-11-13T00:05:00+00:00
- **Stack:** Markdown commands.
- **User request (2026-11-12, via sdlc, verbatim):** "the /remove_command needs a --dry-run argument optionally to find out what that skill or command touches so i can assess the blast radius" <!-- allow-banned: user and sdlc quoted verbatim -->
- **sdlc design (verbatim):** "1. Usage: /remove_command <command name> [--dry-run]. 2. --dry-run runs steps 1-3 (inventory, classify, report) and stops: no deletion, no edits, no test runs, no morty notice. 3. The report adds a blast-radius summary at the top: counts per class, each owner unit with its file count, the test modules that a real removal would run, and the permission rules to remove by hand. 4. A real run after a dry run repeats the inventory (the tree may have moved)." <!-- allow-banned: user and sdlc quoted verbatim -->
- **Command style:** read `/succinct_style` (`~/.claude/commands/succinct_style.md`) before a command edit.
- **Invariants:**
  - Do not change `config/copies.conf`, `config/direct.conf` or any permission setting (`settings*.json`, `templates/settings_local.json`).
  - The user adds no checks: add no guard, refusal or scan that a Work Order does not name.

## Phases

### Phase 1 — /remove_command --dry-run  · status: done

#### Work Order

**Goal:** the user types `/remove_command <name> --dry-run` in any Claude session and sees the hit table and an impact summary. No file changes.

**Spec:**

Edit `commands/remove_command.md`:

1. **Usage** becomes `/remove_command <command name> [--dry-run]`. Step 1 reads the command name and the optional `--dry-run` flag from `$ARGUMENTS`.
2. **Dry run.** With `--dry-run`, run steps 1–3 and stop after the report. Delete nothing, edit no file, run no test and send no morty notice. Say in the report that it is a dry run and that nothing changed.
3. **Impact summary.** Step 3 puts a summary above the table, for every run:
   - the count of hits in each class;
   - each owner unit with its file count;
   - the test modules that step 5 of a real removal would run;
   - the permission rules to remove by hand (the numbered steps step 3 already gives).
4. **Real run after a dry run.** A real run repeats the inventory from step 1. It does not reuse a dry-run table, because the tree can change.

Keep every other step as it is.

Update `docs/as-built/remove-command.md` to match: the usage line, the dry-run stop, the summary and the repeated inventory.

The command stays a prompt only. It adds no script and no test.

**Files:**
- `commands/remove_command.md` — the flag, the summary and the stop.
- `docs/as-built/remove-command.md` — matches the command.

**Workers:** 1 writer; two short Markdown files on one topic, nothing splits.
- `impl` — both files.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:** `commands/remove_command.md` gives the flag, the stop after step 3, the summary and the repeated inventory. `git diff --check` is clean.

**Change-Review:** `/remove_command --dry-run shows what a removal would touch | natedev, any Claude session: type /remove_command <name> --dry-run | The table and the impact summary appear, and no file changes`.
