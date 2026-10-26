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

#### As-built

- `bevy_migration_count_pattern.sh` counts regex matches (`rg --count-matches --no-filename`), sums them across files and leaves out `vendor/` and `target/` at any depth. rg exit 1 is a count of 0; any other rg failure keeps rg's stderr and exits non-zero before any count or JSON is printed. `--multiple` and `--verify` JSON-escape each pattern key, and `--multiple` puts a comma before `"_total"`.
- `commands/bevy_migration_plan.md` names the Agent tool, has no TodoWrite steps, counts guides from `GUIDES_DIR` (`GUIDE_COUNT`), writes the dependency output under `/tmp/claude/`, and promises only the migration plan. Pass 2 spreads the applicable guides over at most 10 agents, each returning one `<Pass2OutputTemplate/>` section per guide. Its two `rg` searches carry the counter's exclusions.
- `PRODUCTION_DOC` is the one `*-production.md` under the project's `docs/`. The `/showrunner:bevy_upgrade` watch is open when `bevy_upgrade.py --production <doc> status` prints a record; its table is the plan's named dependency source. The last step, `<ProductionHandoff/>`, runs `/plan:to_phased_plan` for a project in a production.
- The dependency check tests run from the repository root and from their own folder; `bevy_migration_plan` is a `merge_gate.py` `TEST_SUITES` suite.

**Files:**
- `scripts/bevy_migration_plan/bevy_migration_count_pattern.sh` — the counter.
- `scripts/bevy_migration_plan/test_bevy_migration_count_pattern.py` — counter tests on temporary trees.
- `scripts/bevy_migration_plan/test_bevy_migration_dependency_check.py` — adds its own folder to `sys.path` before the sibling import.
- `commands/bevy_migration_plan.md` — the command text.
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — the suite lists.
- `docs/as-built/bevy-upgrade.md`, `docs/as-built/merge-gate-and-test-speed.md`, `docs/as-built/showrunner-automation.md` — as-built text.

**Gotchas:** `bevy_upgrade.py status` exits 0 also when no watch is tracked; read its output.
