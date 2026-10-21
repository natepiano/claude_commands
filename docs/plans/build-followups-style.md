# Style showrunner follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up phases for the style showrunner after its as-built (`docs/as-built/style-showrunner.md`).

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-21, the user: "the cargo-liner style session picked names for its unit directors that will clash with other unit directors for hana or whatever other project we create a style showrunner for - i am pretty sure we're going to want to create names that can be unique so they can be messaged by their session name". Then: "yes write the rules into the style unit setup then - ask cargo-liner-style to rename them". After style-unit found that the names live in the code and that a rename doubles the branch name, the user: "yes do this".

Naming call (style-unit): a style unit's id is `<style slug>-<family>-unit`, e.g. `cargo-liner-style-names-unit`. Its session, herdr session, worktree folder and branch then all read `cargo-liner-style-names`: one name everywhere, unique by construction, and never the same as a feature unit of the same project. After this phase merges, style-unit asks cargo-liner-style to rename its four units; the user approves there.

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree.
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 through `~/.claude/scripts/lib/py`; basedpyright 1.39; stdlib `sqlite3`; markdown docs.
- **Layout:** `scripts/style/` — style production setup, ledger and refill, tests beside them; `scripts/production/` — unit naming, unit rename and its sweep, tests beside them; `scripts/direct/board_json.py` — the unit board's ticket lookup.
- **Key files:**
  - `scripts/style/add_style.py` — `FAMILIES` :26, `PLAN_DIRECTORY` :27, `_pilot_units` :52-75 (matches `f"{family}-unit"` :64), `standing_style_production` :126-135 (`slug = f"{repository.name}-style"`), `_unit_row` :180-185 (id, plan, worktree `<slug>-<family>`, branch `<slug>-<family>`), `- **Kind:** style` :195, `_plan_text` :212, `_configure_pilot` :238-285 (:252, :270), `_pilot_steps` :288-297, git exclude :300-307, plan write :411.
  - `scripts/style/style_plan.py` — `FAMILIES` :44, `family_plan_texts` :122-133 (:127 match, :128 plan path from the unit id), `family_plan_settings` :136-141, `_select_phase` :1556 (`family = unit.removesuffix("-unit")` :1582-1584), `next_ticket_id`/`ticket_add` callers :1684-1690, :1718-1723, `next_phase` :1727-1735 (plan path from the unit id), queue loop :1822-1832.
  - `scripts/style/coverage_report.py` — fallback unit list :69, declared units :74-81, `ticket_open(unit)` :221.
  - `scripts/style/style_ledger.py` — `tickets` (migration 4, :1461-1467, unique open index `tickets_one_open` on `unit`), `sync_waits` :1489, `rule_candidates` :1502-1507, `ticket_add` :1691 (id must fullmatch `{unit}-\d+` :1696), `next_ticket_id` :1800-1805 (`int(id.removeprefix(unit + "-"))`), `ticket_open` :2081-2085, sync waits :2037-2057, CLI `ticket` parsers :4714-4830.
  - `scripts/style/rules.py` — `Family` Literal :20.
  - `scripts/production/unit_names.py` — `name` :18-22, `unit_id` :25-26, `stem` :29-31, `branch` :34-36, `worktree` :39-40, `swap_stem` :43-45.
  - `scripts/production/rename_unit.py` — `_swap` :137-141, `_ids` :156-161, `_build` :164-199 (new branch :189, new worktree :190), `_doc_text` :304-335, `_plans` :338-343 (via `unit_move.plan_paths`, `docs/plans/*.md` only), `_plan_text` :346-349, `_commit_doc` :396-419, `_move_state` :436-453, `_unit_proposals` :663-672, verify :698-784, `STEPS` :868-880.
  - `scripts/production/unit_move.py` — `plan_paths` :71-86.
  - `scripts/production/unit_rename_sweep.py` — `STATE_HISTORY` :17-25, the `rg -uuu` sweep :80-87.
  - `scripts/production/add_unit.py` — `launch_request` :289-301, `host_session_name` :685-701 (`{slug}-{name}` unless the name starts with `{slug}-`).
  - `scripts/direct/board_json.py` — `tickets WHERE unit = ?` :295-297, phase title match `{unit_name}-\d+` :346.
  - `scripts/production/test_rename_unit.py` — fake production in `setUp` :71-133, `production_doc()` :135-149, `seed_state` :151.
  - Live state (read only): the cargo-liner-style production doc `~/rust/cargo-liner-style/docs/cargo-liner-style-production.md`; ledger `~/.local/state/style/cargo-liner/ledger.sqlite` (copy before reading).
- **Test lanes:** `scripts/style/`, `scripts/production/` and `scripts/direct/` — `test_*.py` beside each module.
- **Build:** none (Python and markdown).
- **Test:** from the worktree root, `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest <modules>`; then the merge gate `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the root.
- **Lint:** `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style <files>`; pass is `0 errors, 0 warnings`.
- **Invariants:**
  - basedpyright reports zero errors and zero warnings; no file-level ignores; no `Any`; line-level ignores only as a last resort (user, CLAUDE.md).
  - Every module has `from __future__ import annotations`; discarded results are written `_ = …`; test classes use `@final`/`@override` (repo convention).
  - Never write the live ledger or a live production doc; read them only from a copy (style-unit rule).
  - Unit rename rule (docs/as-built/unit-rename.md): store only what cannot be looked up, and never rewrite history.
  - The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to code, comments, docs and commits (user).

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, numbered from 1, contiguous, in execution order.
     Never a letter suffix. Phases 1-41 are in docs/as-built/style-showrunner.md;
     this doc continues the same numbers. -->

### Phase 42 — a style unit's name carries its production, so every unit can be messaged by name  · status: done

**Depends on:** none

#### Work Order

**Goal:** A new style production names its units `<style slug>-<family>-unit`, and a live style unit can be renamed to that form without a doubled branch or worktree name and without losing its open tickets, sync waits or approved proposals.

**Spec:**

Part 1 — one family mapping (style side).
- Add one function that maps a unit id to its family: `<family>-unit` (the old form) or any id that ends with `-<family>-unit` gives that family; anything else gives a typed "not a style unit" value (no `None`). Every place that today builds `family + "-unit"` to match a row, or strips `-unit` to get a family, uses it: `style_plan.py` :127, :141, :1582, :1829; `add_style.py` :64, :252; `coverage_report.py` :69-81. No second copy of the rule.
- `style_plan.py` reads a unit's plan from the Units row's Plan cell (relative to the unit worktree), not from `units/<unit id>.md` (:128, :1733). A rename keeps the plan file where it is.
- The queue loop (:1822-1832) iterates the Units rows of the style doc and maps each to its family, so a renamed unit keeps its queue.

Part 2 — new productions (style side).
- `add_style.py` names each unit `<style slug>-<family>-unit` (`_unit_row`, `_plan_text`), writes its plan at `docs/style-production/units/<unit id>.md`, and keeps worktree and branch `<style slug>-<family>`. The style slug stays `<repo>-style` (:126-135); do not take the name from the parent production, which can change (`_attach`).
- `--pilot` accepts a family (`names`), the old id (`names-unit`) or the full id (`cargo-liner-style-names-unit`), each followed by `=crate,...` as today. `--check` prints the full ids.

Part 3 — ledger (style side).
- New ledger method and CLI `style_ledger.py unit rename --repo <checkout> --from <old id> --to <new id>`: in one transaction it moves the `unit` value of current rows only — tickets in `queued`, `assigned` or `checkpointed`, and every `sync_waits` row. `merged` and `dropped` tickets, `rule_candidates`, `held_findings` and `ticket_yields` keep the old id (history). It refuses when `--to` already owns a current ticket or a sync wait, and when the two ids map to different families. It prints how many tickets and waits moved. Running it again moves nothing and succeeds.
- Ticket ids are never rewritten. `next_ticket_id(unit)` numbers only the ids that start with `<unit>-`, so after a rename the first new ticket is `<new id>-1`; a moved ticket keeps its old id. `ticket_add` keeps its rule that a new id fullmatches `<unit>-\d+`, and accepts an existing moved ticket whose id has the old prefix (idempotent re-add).
- `scripts/direct/board_json.py` finds a unit's current phase by the ticket id from the ledger, not by a `<unit name>-\d+` prefix (:346), so a moved ticket still shows.

Part 4 — rename (production side).
- One naming rule in `unit_names.py`: a stem that already starts with `<slug>-` does not get the slug again. `branch(slug, stem)` returns the stem itself in that case; the worktree rule follows the same idea. `add_unit.host_session_name` already applies this rule; make it call the same function.
- `rename_unit.py` `_build`: when the new stem equals the old branch name, the branch stays; when it equals the old worktree folder name, the worktree stays. Otherwise the existing `_swap` applies. So `names-unit` → `cargo-liner-style-names-unit` under slug `cargo-liner-style` keeps branch and worktree `cargo-liner-style-names`, and plan lists no git move. The move steps do nothing for a path that does not change, and verify expects the kept paths.
- Style plans: when the production doc has `- **Kind:** style`, rename treats the Units row's Plan cell (under `docs/style-production/units/`) as the unit's plan. It rewrites the unit id on the plan's `> **Production` line and keeps the file name. `unit_move.plan_paths` stays as it is for other productions.
- The style doc and plans are git-excluded (`add_style.py` :300-307). `_commit_doc` then writes the doc and makes the `chore(<slug>): rename unit <old> to <new>` commit with `--allow-empty`, so the control record that verify and `merge_branch_history` read is still there.
- `_move_state` for a style production calls the Part 3 ledger rename with the style production's repository. `_unit_proposals` also finds proposal issues whose `unit` is the old id when the production is a style production (they carry no plan match today), so approved decisions follow the unit.
- `unit_rename_sweep.py`: add the style ledger folder `style/` to `STATE_HISTORY`, because closed tickets keep the old id by design.

Part 5 — docs.
- `commands/showrunner/add_style.md` (the `--pilot` example :19 and the families table) and `docs/style_production.md` (:24-29 table, :94, :166) use the new ids and say a unit id is `<style slug>-<family>-unit`.
- `docs/as-built/unit-rename.md` :9-11 and `commands/showrunner/rename_unit.md` :8-9: the stem rule, the kept branch and worktree, and the style steps (plan header, allow-empty commit, ledger move, proposals).
- `docs/as-built/style-showrunner.md` :5 and :146: the unit ids.

**Files:**
- `scripts/style/rules.py` — the family mapping beside `Family`; `add_style.py`, `style_plan.py` and `coverage_report.py` import it.
- `scripts/style/style_plan.py` — Part 1: family lookups, the plan from the Plan cell, the queue loop.
- `scripts/style/add_style.py` — Part 2: unit ids, plan path, `--pilot` forms.
- `scripts/style/coverage_report.py` — Part 1: units from the mapping.
- `scripts/style/style_ledger.py` — Part 3: `Ledger.rename_unit`, the `unit rename` CLI, `next_ticket_id`, `ticket_add`.
- `scripts/direct/board_json.py` — the current phase by ticket id.
- `scripts/production/unit_names.py` — the slug prefix rule.
- `scripts/production/rename_unit.py` — Part 4: kept paths, style plan, allow-empty commit, ledger and proposal moves.
- `scripts/production/unit_rename_sweep.py` — `style/` in `STATE_HISTORY`.
- `scripts/production/add_unit.py` — `host_session_name` calls the shared rule.
- `scripts/style/test_style_plan.py` — tests for Part 1.
- `scripts/style/test_add_style.py` — tests for Part 2.
- `scripts/style/test_style_ledger.py` — tests for Part 3.
- `scripts/style/test_coverage_report.py` — tests for the units list.
- `scripts/direct/test_board_json.py` — a moved ticket still shows.
- `scripts/production/test_rename_unit.py` — the style rename and the unchanged feature rename.
- `scripts/production/test_unit_names.py` — new: the slug prefix rule.
- `scripts/production/test_add_unit.py` — `host_session_name` through the shared rule.
- `commands/showrunner/add_style.md` — the unit ids and `--pilot` forms.
- `docs/style_production.md` — the families table and plan paths.
- `docs/as-built/unit-rename.md` — the stem rule and the style steps.
- `commands/showrunner/rename_unit.md` — the stem rule.
- `docs/as-built/style-showrunner.md` — the unit ids.

**Workers:**
2 writers + 1 tester; the work splits by folder: style side (Parts 1-3) and production side (Part 4). The production side calls the ledger rename from Part 3 through its method name, agreed in this Work Order: `Ledger.rename_unit(old: str, new: str) -> UnitRenamed`.
- `impl` — `scripts/production/` (Part 4), `scripts/direct/board_json.py`, `scripts/production/test_rename_unit.py`, `scripts/production/test_unit_names.py`, `scripts/production/test_add_unit.py`, `scripts/direct/test_board_json.py`, and the docs `docs/as-built/unit-rename.md`, `commands/showrunner/rename_unit.md`. hub: `scripts/production/unit_names.py`.
- `impl2` — `scripts/style/` code (Parts 1-3, the mapping in `rules.py`) and the docs `commands/showrunner/add_style.md`, `docs/style_production.md`, `docs/as-built/style-showrunner.md`. hub: `scripts/style/style_ledger.py`.
- `test` — the style tests: `test_style_plan.py`, `test_add_style.py`, `test_style_ledger.py`, `test_coverage_report.py`, written from the Spec while `impl2` builds. Existing tests that use the old ids keep them: the old form stays valid.

**Constraints from prior phases:**
- Phase 41: every waive row has a provenance and a non-empty `waived_by`; `held_findings.ticket` and `build_ticket` hold ticket ids and are history.
- Phase 40: the proposal filer lock folder is `~/.local/state/proposal-issue/locks` from `Path.home()`; tests that file proposals patch `Path.home`.

**Acceptance gate:**
- A new style production (`add_style.py` on a test repo) writes Units rows `<slug>-<family>-unit` with plan `docs/style-production/units/<id>.md`, worktree and branch `<slug>-<family>`; `--pilot names`, `--pilot names-unit` and `--pilot <slug>-names-unit` select the same unit.
- `style_plan.py next-phase`, `queue` and `family_plan_texts` work for a unit named in either form, and read the plan from the Plan cell.
- `style_ledger.py unit rename` moves only current tickets and sync waits, refuses a target that owns current rows or a different family, and a second run moves nothing; after a rename `next_ticket_id` gives `<new id>-1`.
- `rename_unit.py plan` on a fake style production renames `names-unit` to `<slug>-names-unit` with no branch or worktree move; `apply` rewrites the plan header, commits with an untracked doc, moves the ledger rows and proposal issues, and `verify` passes with the ledger in place.
- A feature unit rename (`alpha-unit` → `beta-unit`) behaves as before.
- `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_style_plan scripts.style.test_add_style scripts.style.test_style_ledger scripts.style.test_coverage_report scripts.production.test_rename_unit scripts.production.test_add_unit scripts.direct.test_board_json`
- `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root.
- `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style` on every changed Python file: `0 errors, 0 warnings`.
