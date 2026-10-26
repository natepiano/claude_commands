# Fix the /bevy_migration_plan skill for Hana's Bevy 0.20 plan

> Asked 2026-10-26 17:3x AST by the user, through hana and the build-followups showrunner: "the plan gnerator was written along time ago - im not sure if it is still the best way to go or not - i guess bevy-upgrade unit is part of natedev so ask it to ask bevy-upgrade to do a review of the existing /bevy_migration_plan skill to make sure it is in good shape". Review sent 17:35 AST; the showrunner approved one fix phase ("go"), ETA 20:00 AST. <!-- allow-banned: quotes the user verbatim -->

> **Production: build-followups** — unit `bevy-upgrade-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** the `~/.claude` repository — the `/bevy_migration_plan` command and its helper scripts, which write a Bevy migration plan for a Rust project.
- **Project started:** 2026-10-26T21:39:38.059+00:00
- **Stack:** zsh/bash scripts with ripgrep (`rg`), Python 3 (stdlib `unittest`), Markdown command files.
- **Layout:**
  - `commands/bevy_migration_plan.md` — the command text an agent follows.
  - `scripts/bevy_migration_plan/` — its helper scripts and their tests.
  - `docs/as-built/bevy-upgrade.md` — the as-built doc that describes the migration check.
- **Key files:**
  - `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh` — counts pattern hits in a codebase; single, `--multiple` and `--verify` modes.
  - `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — the dependency check's tests.
  - `scripts/bevy_migration_plan/bevy_migration_get_tranche.py` — splits the guides into the Pass 1 tranches (read only).
  - `scripts/production/merge_gate.py` — the repository's test gate (read only).
  - `pyrightconfig.json` — the `scripts/bevy_migration_plan` execution environment (read only).
- **Test lanes:** `scripts/bevy_migration_plan/` (tests sit beside the scripts as `test_*.py`).
- **Build:** none.
- **Test:** `cd scripts/bevy_migration_plan && python3 -m unittest test_bevy_migration_count_pattern test_bevy_migration_dependency_check`, and from the repository root `python3 -m unittest scripts.bevy_migration_plan.test_bevy_migration_dependency_check scripts.bevy_migration_plan.test_bevy_migration_count_pattern`.
- **Lint:** `basedpyright <the Python files the slot owns>`; only the footer counts and it must read `0 errors, 0 warnings, 0 notes`.
- **Invariants:** (user rules) basedpyright 0 errors and 0 warnings, no file-level ignores, no `Any`; temporary files go under `/tmp/claude`; never `pip install` (use `uv pip install`).

## Phases

### Phase 1 — The migration plan skill counts correctly, uses current tools, and hands off a phased plan  · status: done

#### Work Order

**Goal:** `/bevy_migration_plan` gives correct counts and valid JSON on Hana, names only tools that exist, keeps Pass 2 to at most 10 agents, and ends with a hand-off to `/plan:to_phased_plan`.

**Spec:**
1. `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh`
   - A pattern with zero matches gives 0 in every mode. Today `set -e`, `pipefail` and rg's exit 1 cut off the `--multiple` and `--verify` JSON.
   - `--multiple` and `--verify` print valid JSON: put the comma before `"_total"`, and JSON-escape every pattern key, including backslashes and quotes.
   - A bad regex or a missing path exits non-zero with rg's error on stderr, and never prints 0 for it. rg exit 2 is an error; rg exit 1 is no match.
   - Count matches, not lines (`rg --count-matches`). Measured on Hana: `Commands` gives 3180 lines but 3193 matches.
   - Leave out `vendor/` (Hana keeps patched Bevy crates there: 61 of 70 `ExtractSchedule` hits) and keep leaving out `target/`.
   - Keep the current command-line interface and output keys, so the command text needs no new syntax.
2. `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` imports its module by a bare name, so it fails with ModuleNotFoundError when run from the repository root, as `merge_gate.py --tests-only` runs it. Make it run both from the repository root and from its own folder. basedpyright must stay clean: under the `scripts/bevy_migration_plan` execution environment in `pyrightconfig.json` the sibling import resolves and the `scripts.bevy_migration_plan` package path does not (as-built note). Follow how other `scripts/` test modules that `merge_gate.py` runs import their module.
3. `commands/bevy_migration_plan.md`
   - Name the Agent tool, not the "Task tool". Remove TodoWrite and `<UpdateTodoProgress/>`: these tools no longer exist.
   - Count the guides from `GUIDES_DIR` instead of the hard-coded "114". Use `_release-content/` in the example paths. Use 0.20.0 in the examples.
   - Write the dependency output under `/tmp/claude/`, not `/tmp/`.
   - Remove the promised `bevy-{version}-guides.md` output, because no step writes it.
   - Pass 2: give each agent several guides, at most 10 agents (0.20 has 74 guides). Each agent returns one `<Pass2OutputTemplate/>` section for each guide, and the merge step reads them as before.
   - Next Steps: run the project's own gate (`verify.sh` where the project has one) instead of a bare `cargo nextest run`.
   - Add a last step: for a project in a production, turn the plan into a phased plan with `/plan:to_phased_plan`. When a `/showrunner:bevy_upgrade` watch is open for that production, its dependency table is the current source for dependency state, and the plan says so.
4. `docs/as-built/bevy-upgrade.md`: update the parts that describe the migration check and its command file (the "Command file" and "Execution environments" notes, the file table) so they match the new code and text.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh` — the counter fixes.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — the import fix.
- `scripts/bevy_migration_plan/test_bevy_migration_count_pattern.py` — new tests for the counter.
- `commands/bevy_migration_plan.md` — the command text update.
- `docs/as-built/bevy-upgrade.md` — the as-built update.

**Workers:** `1 writer + 1 tester` — the tests split from the code by file.
- `impl` — `bevy_migration_count_pattern.sh`, `test_bevy_migration_dependency_check.py`, `commands/bevy_migration_plan.md`, `docs/as-built/bevy-upgrade.md`.
- `test` — `scripts/bevy_migration_plan/test_bevy_migration_count_pattern.py`, written from the Spec alone. It runs the script on a temporary tree under `/tmp/claude` and covers: zero matches in all three modes, valid JSON with a backslash pattern and a quote pattern, a bad regex (non-zero exit, no 0), a missing path, matches-not-lines, the `vendor/` and `target/` exclusion. It runs from the repository root and from its own folder.

**Constraints from prior phases:** none.

**Acceptance gate:**
- Both Test lines green.
- `basedpyright scripts/bevy_migration_plan` reads `0 errors, 0 warnings, 0 notes`.
- The full gate, run by the director: `HERDR_BIN=/nonexistent/herdr python3 scripts/production/merge_gate.py --repo <tree> --tests-only`.
- Live check, read only, by the director: the counter on `/home/natepiano/rust/hana_catalyst` with `ExtractSchedule` (single and `--verify` mode), and a zero-match pattern in `--multiple` mode. The JSON parses and the `vendor/` hits are gone.

**Change-Review:** `/bevy_migration_plan` counts correctly on Hana and uses current tools | natedev, any Claude session at ~/.claude: `~/.claude/scripts/bevy_migration_plan/bevy_migration_count_pattern.sh --multiple ExtractSchedule zzNoMatch -- ~/rust/hana_catalyst rust` | valid JSON, `zzNoMatch` is 0, and `ExtractSchedule` has about 9 matches (no vendor/ hits)
