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

#### As-built

- A style unit id is `<style slug>-<family>-unit`; the old form `<family>-unit` stays valid. `rules.style_family(unit: str) -> Family | NotStyleUnit` is the one mapping from id to family; `style_plan.py`, `add_style.py` and `coverage_report.py` use it. `style_plan.py` reads a unit's plan from `<unit worktree>/<Plan cell>` of its Units row, and the queue loop iterates the style doc's Units rows.
- `add_style.py` writes Units rows `<style slug>-<family>-unit`, plans at `docs/style-production/units/<unit id>.md`, worktree and branch `<style slug>-<family>`. The style slug is `<repo>-style`, never the parent production name. `--pilot` takes `names`, `names-unit` or the full id, each with `=crate,...`; `--check` prints full ids.
- `Ledger.rename_unit(old: str, new: str) -> UnitRenamed(tickets, waits)` and the CLI `style_ledger.py unit rename --repo <checkout> --from <old> --to <new>` move, in one transaction, the `unit` of `queued`, `assigned` and `checkpointed` tickets and of every `sync_waits` row. Merged and dropped tickets, `rule_candidates`, `held_findings` and `ticket_yields` keep the old id. It refuses a target that owns a current ticket or sync wait, or that maps to a different family. A second run moves nothing and succeeds. Ticket ids never change: `next_ticket_id` numbers only ids with the `<unit>-` prefix, so the first new ticket after a rename is `<new id>-1`, and `ticket_add` accepts a moved ticket with the old prefix.
- `board_json.py` finds a unit's current phase through `phase_table.build_run` records, not a `<unit name>-\d+` prefix, so a moved ticket still shows.
- `unit_names.branch(slug, unit_stem)` returns the stem unchanged when it already starts with `<slug>-`; `worktree`, `add_unit.host_session_name`, herdr host names in the restart record and the refill instruction in `commands/unit/direct.md` use the same rule. In `rename_unit.py`, a new stem equal to the old branch or worktree name keeps that path, and verify expects the kept paths. So `names-unit` → `cargo-liner-style-names-unit` moves no branch or worktree, and a second rename of a full id works.
- For a production doc with `- **Kind:** style`, `rename_unit.py` treats the Plan cell as the plan, rewrites the unit id on its `> **Production` line in both the unit worktree and the showrunner checkout copy, and keeps the file name. `_commit_doc` commits `chore(<slug>): rename unit <old> to <new>` with `--allow-empty`, because the style doc and plans are git-excluded. `_move_state` calls the ledger rename; `plan` and `verify` read the ledger through `preview_ledger` to refuse early and to check current rows. `_unit_proposals` also moves proposal issues whose `unit` is the old id and whose proposer is an owner of this production.
- `unit_rename_sweep.STATE_HISTORY` includes `style/`, because closed tickets keep the old id.

**Files:**
- `scripts/style/rules.py` — `NotStyleUnit`, `style_family`; `style_plan.py`, `add_style.py`, `coverage_report.py` use it.
- `scripts/style/style_ledger.py` — `UnitRenamed`, `Ledger.rename_unit`, the `unit rename` CLI, `next_ticket_id`, `ticket_add`, `preview_ledger`.
- `scripts/production/unit_names.py` — the slug prefix rule in `branch` and `worktree`; `add_unit.py` calls it.
- `scripts/production/rename_unit.py` — kept paths, style plan header, allow-empty commit, ledger and proposal moves.
- `scripts/direct/board_json.py`, `scripts/production/unit_rename_sweep.py` — phase by `build_run`; `style/` in `STATE_HISTORY`.
- `commands/showrunner/add_style.md`, `docs/style_production.md`, `docs/as-built/style-showrunner.md`, `docs/as-built/unit-rename.md`, `commands/showrunner/rename_unit.md` — the id form, stem rule and style rename steps.

**Gotchas:**
- A style unit id already starts with `<style slug>-`; any code that builds a worktree, branch or herdr host name from the stem must go through `unit_names.branch`, or the prefix doubles.
- Issue folders can be shared between productions; a style rename moves only proposals whose proposer is an owner of this production.
- Refill reads the plan from `<unit worktree>/<Plan cell>`; a rename keeps that path.

**Ruled out:**
- Renaming worktrees and branches when only the production prefix is added: keeping them keeps history and open checkouts valid.
