# Style showrunner

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A standing `<repo>-style` child production that sweeps a repo against every style rule, records coverage in a ledger, hands its work back to the branch point, and replaces the fix pipeline.

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-16 15:26 EDT

Build the approved style showrunner proposal ~/.local/state/plan-backlog/2026-10-16-style-showrunner.md (revision 3, approved by the user 2026-10-16): Phases 1-3, then the Phase 4 pilot under cargo-liner's showrunner; Phase 5 retirement and Phase 6 roll-out only after the pilot meets its pass criteria.

Mapping (style-unit's choice, 2026-10-16): the proposal's Phases 1-3 are this plan's Phases 1-11, its Phase 4 pilot is Phase 12, its Phase 5 retirement is Phase 13 and its Phase 6 roll-out is Phase 14. The proposal file stays the design record; this plan does not edit it.

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree. This plan builds the style showrunner (design: `~/.local/state/plan-backlog/2026-10-16-style-showrunner.md`, revision 3).
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 in practice through `~/.claude/scripts/lib/py` (`typing.override` throughout the tests; natedev has 3.13); basedpyright 1.39; stdlib `sqlite3` (as in `scripts/buildlog/index.py`); markdown command and doc files; zsh/bash scripts; nix in `/etc/nixos`; Rust read only (the cargo-berth source at `~/rust/cargo-liner/crates/cargo-berth/src`). Style rules are markdown with YAML-like frontmatter in `~/rust/nate_style/rust/*.md` (a separate git repo on `main`; other sessions leave uncommitted files there).
- **Layout:**
  - `scripts/fix/` — the fix pipeline; retired in Phase 13 except the pieces Phase 1 moves.
  - `scripts/style/` — new home of the style scripts: `candidate_generators.py`, `style_admin.py`, `style_rules.py`, `rules.py`, `style_ledger.py`, `style_plan.py`, `style_footprint.py`, `style_sync.py`, `add_style.py`, `style_proposals.py`, tests beside them.
  - `scripts/production/` — merge_checkpoint, merge_gate, add_unit, add_showrunner, showrunners, production_lifecycle, dailies_render, dailies_input, proposal_issue.
  - `scripts/lib/` — shared helpers (`user_zone.py`, `py`); `berth_board.py` is new in Phase 7.
  - `commands/` — top-level style and fix commands, `showrunner/`, `unit/direct.md`, `nightly_*`.
  - `docs/` — `production_format.md`, `delegate_plan_format.md`, `delegate/phase_end.md`, `style_production.md` (new), as-built and plans.
  - `config/agents.conf`, `pyrightconfig.json`, `settings.json`, `CLAUDE.md`.
  - Outside the repo: `~/rust/nate_style/rust/` (rule frontmatter); `/etc/nixos` (`configuration.nix`, `modules/common`, `modules/linux`); `~/.local/state/style/<repo>/` (ledger, new).
- **Key files:**
  - `scripts/style/candidate_generators.py` — `CandidatesSpec` :37 (kind, origin, pattern, exclude_pattern, globs, paths, paths_exempt, structs, field_type_pattern); `read_candidates_spec` :78; `GENERATORS` (15 kinds); `enumerate_candidates` :1166, returns every site (no cap).
  - `scripts/fix/style_admin.py` — 260 lines; `from style_history import HISTORY_DIR, NATE_STYLE_DIR, HistoryRow` :12; `RUST_STYLE_DIR` :14. Used by `commands/style_rename.md:23`, `commands/style_delete.md:27`.
  - `scripts/fix/style_history.py` — 2089 lines; imports candidate_generators :25; `NATE_STYLE_DIR` :28, `HISTORY_DIR` :29, `HistoryRow` :129; `normalize_guideline_id` :152, `parse_frontmatter` :180, `list_style_files` :575, `_build_units_from_files` :590, `resolve_focus_targets` :628, `focused_units` :673; `focused-eval` subparser :1949, handler :2071; `enumerate_candidates` call sites :1228, :1305, :2057.
  - `scripts/fix/style_report.py`, `scripts/fix/backpopulate_settings.py` (called from `fix.sh:219`) — retired in Phase 13.
  - `scripts/fix/fix.conf` — the fix config (there is no `config/fix.conf`); sections `[style_eval]` :12, `[style_fix]` :26, `[project_env]` :41, `[projects]` :52, `[active_checkout]` :94.
  - `scripts/fix/docs/candidate-enumeration-design.md` — generator design; enums-over-bool narrowed to struct fields at :325-326; generator table :163.
  - `scripts/fix/tests/` — fix pipeline tests, not in the merge gate.
  - `~/rust/nate_style/rust/<rule>.md` — 53 rules. Frontmatter keys today include `mechanism` (clippy 7, mend 6, rustfmt 1, llm 39), `scope: review` (6), `mode: propose` (5), `candidates:` (22), `pre_filter:` (forbidden-words). `candidates:` sub-keys indent 2 spaces, scalars may be quoted, lists are inline `[a, b]`, the block ends at the first unindented line or `---`.
  - `scripts/rust_style/load-rust-style.sh` — loads global rules plus repo-local rules from `$repo_root/docs/<member>/style/` (:73); `--list-files`.
  - `commands/focused_eval.md:36` — calls `scripts/fix/style_history.py focused-eval`.
  - `commands/style_rename.md`, `commands/style_delete.md`, `commands/style_eval.md:37`, `commands/style_usage.md:16`, `commands/fix.md`, `commands/style_fix_review.md`, `commands/lint_config.md` (fix pipeline named at :2, :52, :54, :60).
  - `commands/make_a_worktree.md` — `<OfferFixPipelineRedirect/>` :98-129, used :20; `scripts/make_a_worktree/retarget_fix.py` (`DEFAULT_CONF` :32).
  - `commands/worktree_delete.md` — `<RevertFixPipelineRedirect/>` :101-107, used :31; `scripts/worktree_delete/perform_deletion.sh:52-59` (`.fix-project`), `style_history.py discard-pending` :120-127.
  - `scripts/new_rust_project/rust_generate.sh` — fix enrollment block :153-~:207 (`CONF` :154), message :208.
  - `config/agents.conf` — `[fix.codex]` :82, `[fix.claude]` :88 (header comment :80); `style_fix_review=` :70 and :76.
  - Other fix consumers: `scripts/agents/test_agent_exec.py:111-115` (reads `scripts/fix/style-*.sh`; in the `agents` merge-gate suite), `scripts/whoami/test_quota_alert.py:61-63`, `scripts/cli_agent/cli_agent.sh:20,38,42`, `settings.json:155` (`style_usage`), `CLAUDE.md:4` (`/style_eval`), `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh:32`, `docs/as-built/agent-registry.md:318-322`, `pyrightconfig.json` (the `scripts/fix` environment).
  - `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md` — retired in Phase 13.
  - `commands/nightly_review.md` — `<Scope mode="rust">` :19-24, weight line :20; `commands/nightly_weights.md`; `scripts/nightly_review/weights.py` (`COMMAND` :22) and `test_weights.py`; `scripts/nightly_review/nightly_review.py` `MODES` :42; `commands/nightly_next.md`, `commands/showrunner/nightly.md` (both use `candidates.py`; kept).
  - `/etc/nixos/modules/common/style-fix.nix`, `/etc/nixos/modules/linux/style-fix.nix`; imports `modules/common/default.nix:20` and `configuration.nix:64`; `nightly-review.nix` imported at `configuration.nix:60` (kept). Comments citing style-fix.nix: common/{claude-to-codex-sync:10, hanadocs-prioritize:59, session-notifier:10, codex-catalog-sync:9, checkouts:158, development:65}, linux/{usage-collect:4, buildlog:11}, darwin/nate-backend.nix:11,86; `shell.nix:134` style-fix alias.
  - `scripts/accounts/role_map.py` — `Role` :26; workers map :302, :328.
  - `scripts/production/add_showrunner.py` — `account_for_launch` :108, `main` :158; takes `--production <child doc>` (:160), `--model`, `--effort`, `--check`, `--resume/--cwd/--account/--config-dir`; reads the doc through `add_unit.read_production`, starts `/showrunner:produce <doc>`, attaches the child under the caller.
  - `scripts/production/add_unit.py` — `production_field` :246, `read_production` :257 (a doc needs `- **Merge branch:**`, `**Showrunner checkout:**`, `**Log:**` (relative), a `## Units` line, and a name ending `-production.md`); `ensure_worktree` :625; berth target `branch.<b>.cargoBerthTarget` = merge branch at :638-642, only when `<checkout>/.claude/config/berth.toml` exists; `account_for_launch` :812.
  - `scripts/production/showrunners.py` — CLI `main` :500: `list` :516, `name|address <slug>` :507/:509, `parent|children|root <slug>` :517-519, `tree` :520, `attach <child> [<parent>]` :521, `detach <child>` :524; `attach` :419, `parent_of` :321, `current_address` :261, `stored_children` :464, `tell_others`, `release_edges`.
  - `scripts/production/production_lifecycle.py` — `retire_reservations` :308 (reads a top-level `reservations` list of dicts from `cargo-berth board --json`; the envelope's `reservations` is a list of ids, so it releases nothing); `refuse_running_children` :335, called in `wrap` (:376) at :383; `release_edges` :346, called at :442.
  - `scripts/production/merge_checkpoint.py` — 940 lines; `request_from` :278, refusal `unit {name} not in production doc` :285; CLI positionals `unit phase hash`, `--production`, `--started` (required), `--review-trial`/`--delivers`/`--parallel` (required for a code checkpoint) (`main` :910-929); `merge_branch_history` :333, regex :336 `^Merge (.+?) phase (\S+?)( shrink| as-built)? \(([0-9a-f]+)\)`; `merge_history` :349; `merge_subject` :539 `Merge <unit> phase <phase>[ shrink| as-built] (<hash7>) into <merge branch>`; `push_git` :677 (`git push` :687); `push_validate` :711 (`--quick` :714); `run` :856, push step :886-890, then `record_changes`, `into`, `promote`, `record`. Productions without a `**Push:**` line (cargo-liner, hana) push through validate_and_push `--quick`.
  - `scripts/production/merge_gate.py` — `TEST_SUITES` :40-45 (fixed list; the gate refuses a listed suite with no test modules); `run_suites` :279 globs `scripts/<suite>/test_*.py`; `merge_checkpoint` :459, subject :483 `Merge {unit} {phase} ({checkpoint}) into {merge_branch}`, `git push` :502; `--tests-only` :564.
  - `scripts/validate_and_push/validate_ci.sh:201` — `VALIDATE_QUICK=1` skips clippy, rustdoc and nextest; quick runs mend, rustfmt and taplo (:259).
  - `scripts/delegate/verify.sh` — the lint/check/test front end (`lint <pkg>`).
  - `scripts/production/dailies_render.py` — 1941 lines; `ChildReport` :395; `check_keys` :645 rejects unknown keys; `parse_child_row` :720; `parse_child_report` :731 with `check_keys` :734 over `{written, zone, interval, units, held, rows}`; `parse_child` :745; `child_row` :1690; `last_report` :1806 (pure).
  - `scripts/production/dailies_input.py` — `save_last_report` :784, called :1069.
  - `scripts/production/proposal_issue.py`, `proposals_table.py` — the proposal issue writer and the `/showrunner:proposals` table.
  - `scripts/message/send.py` — `send.py --to NAME|uds:…|session:… [--from NAME] [--summary] [--key KEY [--repeat-minutes N]] (--text|--file|stdin)` (docstring :7-16).
  - `commands/unit/direct.md` — 1536 lines; `<VerbosePostPhaseGate>` :1443; `<NextPhase>` :1458-1475 (no todo phase left → FinalGate, RunAsBuilt, final PeriodicCI, CICleanup, RunSummary at :1459-1461).
  - `docs/delegate/phase_end.md` — `<PhaseEnd>` :13; `<NextPhase/>` cited :39, :52.
  - `commands/showrunner/produce.md` — reads `production_format.md` :17; `PRODUCTION_DOC` :24; nested showrunners :104; "A child showrunner is never a unit. It has no Units row, and you never merge its branch" :114-115; `<CrossUnitChange>` :1070-1093 (used :539, :635).
  - `docs/production_format.md` — Production Context fields :84-117 (Merge branch :90, Proposals :114).
  - `docs/delegate_plan_format.md` — blockquote header fields :29-45.
  - `commands/showrunner/add_showrunner.md` (add_showrunner.py at :24), `add_unit.md`, `attach.md`, `proposals.md`, `proposal.md`, `dailies.md` (`expand` :29-31).
  - cargo-berth: `verb/release.rs:452` (`Active` → `checkpoint_operation`; `Outstanding` → `outstanding_operation` :455); `reservation/record.rs:423` `edit_blocking_status` (Active blocks; Outstanding blocks while `merge_extent.protection()` holds, else Clear; Released is Clear); `cli.rs` `BoardArguments` :298 (`--json`, `--reservation <id>`); README :387 (`board` reconciles the journal with Git before rendering); envelope `output.rs:366` `{output_contract_version, verb, status, exit_code, reservations[ids], blocked_by[ids], message, presentation, payload}`; `payload.data` is `BoardModel` (`board/rows.rs:81`): `targets[{ref, commit, reservations}]`, `ready_now`, `waiting`, `unresolved_overlaps`, `live_overlap_answers`, `unconstrained_reservations`, `resolved`, `alerts`, each `{journal_position, entries[]}`; reservation snapshot (:169) `reservation_id`, `target`, `holder{worktree_id, worktree_root, branch, liveness}`, `source`, `purpose`, `scopes`, `race_extent`, `merge_extent`, `lifecycle`, `integration_evidence`, `edit_blocking_status`, `visibility`, `freshness`, `ahead_behind_main`; `check` (`cli.rs:504`, paths with optional `file:`/`tree:` prefix, `--reservation <id>`) acquires (`verb/check.rs:1`).
  - Pilot and roll-out targets: cargo-liner's showrunner is production `remove-running-section` (session `cargo-liner`), doc `/home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md`, merge branch `main`, checkout `/home/natepiano/rust/cargo-liner`, has `.claude/config/berth.toml`, no `**Proposals:**` and no `**Push:**` line. Hana's is production `tool-based-ui` (session `hana`), doc `/home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md`, repository `/home/natepiano/rust/hana`, checkout `/home/natepiano/rust/hana_catalyst`, branch point `init/catalyst`, Proposals `~/rust/hanadocs/issues`.
- **Test lanes:** `scripts/style/` — `test_*.py` beside the modules (none yet); `scripts/production/` — `test_*.py` beside each module; `scripts/lib/` — beside; `scripts/accounts/`, `scripts/nightly_review/`, `scripts/message/` — beside; `scripts/fix/tests/` (retired); `scripts/make_a_worktree/`, `scripts/worktree_delete/`, `scripts/new_rust_project/`, `scripts/rust_style/` — none; markdown command and doc files — none.
- **Build:** none (Python and markdown).
- **Test:** for each script dir a phase touches, from the worktree root: `python3 -m unittest discover -s scripts/<dir> -p 'test_*.py'` (always from the root: `-m` puts the root on `sys.path` for `scripts.` imports, and `-s` adds the dir for sibling imports). Then the merge gate: `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root (runs every `TEST_SUITES` suite; runs no basedpyright).
- **Lint:** `basedpyright scripts/<dir>` from the worktree root for each script dir a phase touches; pass is its `0 errors, 0 warnings` line (it exits 3 because `pyrightconfig.json` names a `.venv` no checkout has).
- **Invariants:**
  - basedpyright reports zero errors and zero warnings; no file-level ignores; no `Any` (TypedDict for known-key dicts, annotate every `json.loads` result); line-level `# pyright: ignore[...]` only as a last resort (user, CLAUDE.md).
  - Every module has `from __future__ import annotations`; discarded results are written `_ = …`; test classes use `@final`/`@override`; sum types are small NamedTuple/dataclass variants (repo convention).
  - Scripts run through `~/.claude/scripts/lib/py`; Python ≥3.12 (the tests already use `typing.override`).
  - State lives under `~/.local/state/`; user-facing times go through `scripts/lib/user_zone.py` (America/New_York) (user, 2026-10-12).
  - Scripts message sessions only through `scripts/message/send.py`, and address a showrunner by `showrunners.py address <slug>`, never by a stored name (repo convention).
  - The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to code, comments, docs and commits; hooks block a match (user).
  - Repo-wide sweeps use `git grep`, not `grep -r` (repo convention, `fix-pipeline.md:203`).
  - Edits in `~/rust/nate_style` commit only the files the phase changed, on its `main`; other sessions' uncommitted files there stay untouched (style-unit's rule, same as `~/.claude` main).
  - Changes under `/etc/nixos` go through natedev (the build-followups showrunner session), and the user rebuilds (production rule).
  - No new launch or account code: style sessions launch through `add_showrunner.py`, `add_unit.py` and `implement.sh`, on the existing role map (design, section 2).
  - Style units own no files: they take what the berth board, live units' unmerged branches and their dirty worktree files leave free, and product work always wins a conflict (design, section 6).
  - Rules stay in `~/rust/nate_style`; the style production's procedure lives in `~/.claude/docs/style_production.md` (design, section 2).
  - A fix is applied without asking only when it is mechanical (Phase 7, `style_footprint.py`); every other change is held as a proposal and shown only when asked (design, section 7).

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, numbered from 1, contiguous, in execution order.
     Never a letter suffix (`4a`, `2b1a`) — those stop sorting and make ranges
     unreadable. Any spine edit (insert, split, merge, reorder, delete)
     resequences from the edit point to the end AND updates every cross-reference:
     substitute highest-first so `Phase 9` does not corrupt `Phase 13`, and
     re-check that each `Phases X–Y` range still spans the same set.
     Insert after the last `done` phase where possible — a done phase's number
     is baked into its checkpoint commit message and cannot be rewritten; if one
     must be renumbered, add an old→new mapping note to the doc.
     Full procedure: /plan:to_phased_plan → <PhaseNumbering/>. -->

### Phase 1 — Move the kept style scripts into scripts/style  · status: done

#### As-built

- `scripts/style/` holds `candidate_generators.py` (no 150 cap; `enumerate_candidates` returns every site), `style_admin.py` (imports `HISTORY_DIR`, `NATE_STYLE_DIR`, `HistoryRow` from `style_rules`) and the new `style_rules.py`.
- `style_rules.py` owns rule reading, the history TypedDicts, `Unit`, and the `focused-eval` CLI, whose output matches the old `style_history.py focused-eval`.
- `scripts/fix/style_history.py` deletes the moved definitions and imports them from `scripts/style` through a `sys.path` insert; it stays in `scripts/fix/` until its retirement.
- `/focused_eval`, `/style_rename` and `/style_delete` call the new paths through `~/.claude/scripts/lib/py`; `scripts/style` has its own pyright environment and is a merge-gate suite.

**Files:**
- `scripts/style/style_rules.py` — rule reading, `Unit`, `HistoryRow`/`Outcome`/`ReviewedUnit`, `focused-eval`
- `scripts/style/candidate_generators.py` — generators; `CandidatesSpec` at :37, `read_candidates_spec` at :78
- `scripts/style/style_admin.py` — rule rename and delete
- `scripts/style/test_candidate_generators.py`, `test_style_rules.py`, `test_style_admin.py` — tests for the three modules
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — `style` suite
- `pyrightconfig.json` — `scripts/style` environment; `scripts/style` in the `scripts/fix` extraPaths

**Binds later work:** `style_rules.py` holds `parse_frontmatter`, `list_style_files`, `NATE_STYLE_DIR`, `HISTORY_DIR`, `RUST_DIR`, `LOAD_STYLE_SCRIPT`, `extract_title`, `read_pre_filter`, `normalize_guideline_id`, `Unit`, `HistoryRow`/`Outcome`/`ReviewedUnit`, `build_units_from_files`, `resolve_focus_targets`, `focused_units` and the `focused-eval` CLI. `candidate_generators.py` returns every site with no cap; any limit belongs to its callers. `style_history.py` re-exports moved names with `X as X` because `style_report.py` imports them from there. `scripts/style` is a merge-gate suite.

**Gotchas:** A new merge-gate suite goes in both `TEST_SUITES` in `merge_gate.py` and `EXPECTED_SUITES` in `test_merge_gate.py`. The repo's Python floor is 3.12 in practice: `typing.override` and `enterContext` appear in 194 scripts.

**Ruled out:** Holding the new tests to Python 3.10 — the repo already uses `typing.override`.

### Phase 2 — Rule metadata: tier, family, input scope and generator surface  · status: done

#### As-built

- Every global rule in `~/rust/nate_style/rust/*.md` declares `tier` (A14/B20/C10/D7/E2), `family: names|values|types|modules` on B, C and D, and `input_scope: file|crate|manifest` (absent means `crate`; only `workspace-dependencies` is `manifest`). All 22 `candidates:` blocks declare `surface: partial` with an `uncovered` line.
- Validation: tier A iff `mechanism` is clippy, mend or rustfmt; tier B requires `candidates:` or `pre_filter:`; B, C and D require `family`, A and E forbid it; a repo-local rule must declare `tier` and `family`. Each failure raises `RuleValidationError(path, reason)`.
- `CandidatesSpec` carries `surface: Literal["whole","partial"]` and `uncovered: str`; `read_candidates_spec` raises `CandidatesSpecError(ValueError)`, naming the rule, when `kind` is missing, `surface` is not whole or partial, or partial has no `uncovered`.
- `style_rules.parse_frontmatter` returns top-level `tier`, `family`, `input_scope`, `mechanism` and `mode` as one-element lists, quotes stripped; `read_pre_filter` honors only an unindented top-level `pre_filter:` line.
- `rules.py` defines aliases `Tier`, `Family`, `InputScope`, `RuleOrigin` and frozen `Rule(name, path, tier, family, input_scope, mode_propose, candidates, pre_filter, blob, origin)`; `load_rules(repo_root) -> tuple[Rule, ...]` reads global rules plus the repo's `docs/style` and `docs/*/style` rules. `rules.py list [--repo] [--json]` exits 2 on a validation error.

**Files:**
- `scripts/style/rules.py` — `Rule`, `load_rules`, `RuleValidationError`, `list` CLI
- `scripts/style/candidate_generators.py` — `CandidatesSpec.surface`/`uncovered`, `CandidatesSpecError`
- `scripts/style/style_rules.py` — scalar metadata keys, top-level `pre_filter`
- `scripts/style/test_rules.py` — loader and validation tests; `test_candidate_generators.py` fixtures declare `surface: whole`
- `~/rust/nate_style/rust/*.md` — tier, family, input_scope, surface/uncovered

**Binds later work:** the `Rule`, `load_rules`, `CandidatesSpec` and error signatures above; nate_style metadata lives in commits bc8ca57 and a4549af, local to natedev and never pushed by this unit (nate_style main carries other sessions' commits), so another machine lacks it; the Missing rule metadata reads as unclassified phase handles that, and later work never counts an unclassified rule as current. No generator rule is whole, so every listed rule's uncovered surface needs a tier-C read in its family. `~/rust/cargo-liner`'s 11 local rules stay untagged until the pilot's bootstrap.

**Gotchas:** never push nate_style from this unit; `review-charter.md` and `rust/public-failures-return-named-errors.md` belong to other sessions and are never staged (stage nate_style files by explicit path, never `git add -A`); `rules.py` forks `git hash-object` once per rule (`--stdin-paths` would batch it).

**Ruled out:** discovery through `load-rust-style.sh` — its filters drop tool-enforced and Bevy rules, so `load_rules` reads the directory layout directly; `surface: whole` for any generator — both candidates missed cases (unlisted registrations, bare-name trait counting).

### Phase 3 — Missing rule metadata reads as unclassified  · status: done
**Depends on:** Phase 2

#### Work Order

**Goal:** the Phase 2 code runs on a machine whose `~/rust/nate_style` predates the rule metadata (the Mac until nate_style is pushed and pulled): a missing `surface` or `tier` reads as unclassified, never a crash and never a false current.

**Spec:**
- Why: nate_style's metadata commits (bc8ca57, a4549af) are local to natedev; `~/rust/nate_style` main also carries three commits from other sessions, so this unit never pushes it. `~/.claude` reaches the Mac first. There, `read_candidates_spec` raises `CandidatesSpecError` on every `candidates:` block without `surface`, which breaks `scripts/fix/style_history.py` `unit_candidates_spec` (:1349, the focused-eval path) and `enumerate-candidates` (:1805), and `rules.py list` exits 2 on the first rule without `tier`.
- `CandidatesSpec.surface` (`scripts/style/candidate_generators.py`) becomes `Literal["whole", "partial", "unclassified"]`; a block with no `surface` key reads as `unclassified` with no error. A present but invalid `surface`, a missing `kind`, and `partial` without `uncovered` stay `CandidatesSpecError`. Enumeration is unchanged for every surface value.
- `scripts/style/rules.py`: a global rule with no `tier` loads as unclassified, not an error: `Rule.tier` becomes `Tier | Literal["unclassified"]`, family and input-scope checks are skipped for it, and `rules.py list` prints `unclassified`. A global rule that declares any metadata key is validated in full, as now. A repo-local rule missing `tier` or `family` stays a named `RuleValidationError` (local rules have no older checkout to tolerate).
- Downstream readers never count an unclassified rule as current: a later phase that needs the tier of an unclassified rule treats the rule as not yet classified, and Phase 4's coverage ledger records no cell for it.

**Files:**
- `scripts/style/candidate_generators.py` — `surface` gains `unclassified`
- `scripts/style/rules.py` — unclassified global rules
- `scripts/style/test_rules.py` — tests
- `scripts/style/test_candidate_generators.py` — tests

**Workers:** 1 writer + 1 tester — the code is one small change across two modules; the tester writes the regression tests from this Spec.
- `impl` — `scripts/style/candidate_generators.py`, `scripts/style/rules.py`
- `test` — `scripts/style/test_rules.py`, `scripts/style/test_candidate_generators.py`: a temp rule dir holding pre-Phase-2 frontmatter (a `candidates:` block with no `surface`, rules with no `tier`) loads and lists as unclassified; `read_candidates_spec` on a block without `surface` enumerates the same sites as before; an invalid `surface` and a local rule without `tier` still raise named errors.

**Constraints from prior phases:** Phase 2: `CandidatesSpec` has `surface` and `uncovered`; `CandidatesSpecError` (a `ValueError`) names the rule; `rules.py` `load_rules(repo_root) -> tuple[Rule, ...]`, `RuleValidationError(path, reason)`, `Tier`, `Family`, `InputScope` aliases; `style_rules.parse_frontmatter` returns `tier`, `family`, `input_scope`, `mechanism`, `mode` as one-element lists when present; `read_pre_filter` honors only a top-level key. All 22 `candidates:` blocks in nate_style at bc8ca57/a4549af are `partial`. Never push `~/rust/nate_style`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` green; `basedpyright scripts/style` `0 errors, 0 warnings`; `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` green.
- Against nate_style as the Mac has it: `git -C ~/rust/nate_style archive origin/main rust | tar -x -C <tmp>` then `rules.py list` with `NATE_STYLE_DIR` pointed at `<tmp>` (a test may patch it) exits 0 and lists every rule as unclassified; `style_history.py enumerate-candidates` for a generator rule from that tree prints its candidates.
- `~/.claude/scripts/lib/py scripts/style/rules.py list` on the current tree still counts 53 rules, 14/20/10/7/2.

### Phase 4 — Coverage ledger: judgments, cells and the tier A lint record  · status: todo
**Depends on:** Phase 2, Phase 3

#### Work Order

**Goal:** `style_ledger.py` keeps one sqlite ledger per repo that records every check and answers, for any synced tip, which (rule, file) cells are current, stale, open or awaiting merge.

**Spec:**
- Ledger: `~/.local/state/style/<repo>/ledger.sqlite` (`<repo>` = basename of the repo's main checkout, e.g. `hana`, `cargo-liner`), written only by `scripts/style/style_ledger.py`. Schema version table; migrations by version number.
- **Judgment row** (one check): `rule`, `rule_blob`, `input_scope`, `input_fingerprint`, `path` (file, or crate dir for crate-scope tier D), `commit`, `verdict` (`clean` | `fixed` | `exception` | `held` | `waived`), `verdict_detail` (the fix commit, the exception reason, the issue path, or the user's words), `ticket`, `worker`, `recorded_at` (UTC ISO).
- **Input fingerprint** by scope: `file` — the file's blob hash at the commit; `crate` — the git tree hash of the crate's directory (the dir holding its `Cargo.toml`); `manifest` — sha256 over the sorted `(path, blob)` of every `Cargo.toml` in the workspace. Computed from git objects (`git rev-parse <commit>:<path>`, `git ls-tree`), never from the working tree.
- **Files in scope:** every tracked `.rs` file at the tip, excluding vendor code (paths under any `vendor/` dir and any path the repo's `.claude/config/berth.toml` or `rustfmt.toml` ignores — read what exists; at minimum `vendor/`). Tier D cells are per crate, not per file.
- **A cell** is (rule, file) at the last synced tip. State:
  - `current` — a row matches this rule blob and this input fingerprint;
  - `stale` — a row exists but the inputs or the rule blob changed (a rule change makes every cell of that rule stale, whatever the source did);
  - `open` — never checked;
  - `awaiting merge` — checked on the style merge branch, current once that commit reaches the branch point (row's `commit` is not an ancestor of the synced tip but its fingerprint matches the style branch's file).
  An `exception` row is a row like any other, so it expires when its inputs change. `held` and `waived` verdicts count as current while their fingerprint matches; the report shows held separately.
- **Tier B rule with `surface: whole`:** a generator run that lists no site in a file records `clean` for that file. With `surface: partial`, the file still needs its tier-C read: model the read as the pseudo-rule `<rule>#uncovered`, a tier C cell in the same family.
- **Tier A lint record:** table `lint_runs`: `tip`, `tree` (the tip's root tree hash), `tool` (`clippy` | `mend` | `rustfmt`), `scope` (workspace), `config_hash` (sha256 of the lint config files the tool reads: `clippy.toml`/`.clippy.toml`, the workspace `[lints]` tables, `rustfmt.toml`, mend's config; hash what exists), `passed`, `diagnostics_json` (file → count). A tier-A cell is current only when a `lint_runs` row for its tool exists at the synced tip's tree with the current config hash and lists no diagnostic in that file; otherwise `stale` (or `open` if no run exists). Tier A rule → tool by its `mechanism`.
- **Synced tip:** table `sync` holds the current synced branch-point tip per repo (`set-tip` writes it; Phase 5 adds processed-tip bookkeeping).
- CLI (`argparse`, every write idempotent on its natural key):
  - `style_ledger.py record --repo R --rule N --path P --commit C --verdict V [--detail D] --ticket T --worker W` — computes scope and fingerprint itself from `rules.load_rules`.
  - `style_ledger.py lint-record --repo R --tip C --tool T --passed 0|1 --diagnostics FILE.json` — computes tree and config hash.
  - `style_ledger.py set-tip --repo R --tip C`.
  - `style_ledger.py status --repo R [--rule N] [--crate K] [--json]` — counts per rule and state; with `--json` the per-cell list.
- Library API used by later phases: `open_ledger(repo) -> Ledger`, `Ledger.cells(rule=None, crate=None) -> Iterator[Cell]`, `Cell(rule, path, crate, tier, family, state)`.
- Uses `scripts/style/rules.py` (`load_rules`, `Rule.blob`, `Rule.input_scope`, `Rule.tier`).
- Tests `scripts/style/test_style_ledger.py` on a temp git repo with two crates: each cell state; rule-blob change stales every cell of that rule; crate scope stales all files of one crate on a one-file edit, not the other crate; manifest scope; exception expiring; tier A current only with a matching lint run; idempotent re-record.

**Files:**
- `scripts/style/style_ledger.py` — new
- `scripts/style/test_style_ledger.py` — new

**Workers:** 1 writer + 1 tester — one new module; the Spec fixes the CLI and cell states, so the tester writes against it.
- `impl` — `scripts/style/style_ledger.py`
- `test` — `scripts/style/test_style_ledger.py`: the temp-repo cases listed in the Spec

**Constraints from prior phases:** Phase 2: `scripts/style/rules.py` `load_rules(repo_root) -> tuple[Rule, ...]` with `tier`, `family`, `input_scope` (default `crate`; only workspace-dependencies is `manifest`), `candidates.surface` (`whole`/`partial`), `blob`, `mode_propose`. Tier D = the six `scope: review` rules + module-structure, all family `modules`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` green; `basedpyright scripts/style` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `style_ledger.py set-tip --repo cargo-liner --tip $(git -C ~/rust/cargo-liner rev-parse main)` then `status --repo cargo-liner` prints every rule with all cells `open`, in under 10 s.

### Phase 5 — Ledger work state: tickets, hand-offs, processed tips, reconcile  · status: todo
**Depends on:** Phase 4

#### Work Order

**Goal:** the ledger also holds tickets, hand-offs and processed tips with idempotent transitions, and `reconcile` makes them agree with git after any restart.

**Spec:**
- Tables in the Phase 4 ledger (schema version +1):
  - `tickets`: `id` (`<unit>-<n>`), `unit`, `kind` (`read` | `listed` | `structure`), `family`, `rule` (listed only), `crate`, `files` (JSON list), `sites` (JSON, listed only), `state` (`queued` → `assigned` → `checkpointed` → `merged`; any non-final → `dropped`), `checkpoint` (commit), `created_at`, `updated_at`.
  - `handoffs`: `checkpoint` (style merge branch commit, primary key), `state` (`pending` → `sent` → `merged`), `merge_commit`, `sent_to` (parent slug), `created_at`, `updated_at`.
  - `processed_tips`: `tip`, `processed_at`.
- Transitions: a repeat of the same transition is a no-op that exits 0; a backward or skipped transition exits 2 with the current state named. CLI:
  - `style_ledger.py ticket add|assign|checkpoint|merge|drop --repo R ...` (`add` takes the fields; the rest take `--id` and, for `checkpoint`, `--commit`);
  - `style_ledger.py ticket open --repo R --unit U` — prints the unit's one non-final, non-queued ticket (the resume point) or nothing;
  - `style_ledger.py handoff add|send|merged --repo R --checkpoint C [--to SLUG] [--merge-commit M]`;
  - `style_ledger.py handoff pending --repo R` — lists `pending` and `sent`;
  - `style_ledger.py tip seen --repo R --tip C` (exit 0 and print `new` or `seen`), `tip done --repo R --tip C`.
- `style_ledger.py reconcile --repo R --branch-point B --style-branch S` (run at style showrunner startup):
  - a hand-off becomes `merged` only when `B` contains its checkpoint commit (`git merge-base --is-ancestor`), never because a notice was sent; `merge_commit` is the first merge commit on `B` containing it;
  - a `checkpointed` ticket becomes `merged` when `S` contains its checkpoint;
  - an `assigned` ticket whose unit branch no longer exists is `dropped`;
  - prints every change it made.
- `merge` on a hand-off is accepted only after the same ancestry check (the acknowledgment message in Phase 9 calls it).
- `style_ledger.py summary --repo R --json` — the dailies object fields (Phase 10 consumes): `rules_done`, `rules_total`, `cells_current_pct`, `stale`, `proposals` (count of `held` cells' distinct issues), `waiting_to_merge_back` (hand-offs not `merged`), `oldest_waiting` (UTC ISO or null), `synced_at`, `synced_tip`. A rule is done when every in-scope cell at the synced tip is `current` (held and waived count).
- Tests in `scripts/style/test_style_ledger.py` (extend): each transition, repeats, rejects; reconcile against a temp repo with a merged and an unmerged hand-off; `ticket open` after an interruption returns the assigned ticket; `summary` keys.

**Files:**
- `scripts/style/style_ledger.py` — work-state tables, CLI, reconcile, summary
- `scripts/style/test_style_ledger.py` — extended

**Workers:** 1 writer + 1 tester — one module; the Spec's CLI and transitions are testable before the code.
- `impl` — `scripts/style/style_ledger.py`
- `test` — the new cases in `scripts/style/test_style_ledger.py`

**Constraints from prior phases:** Phase 4: ledger at `~/.local/state/style/<repo>/ledger.sqlite` written only by `scripts/style/style_ledger.py`; schema version table; cell states `current`/`stale`/`open`/`awaiting merge`; `sync` table with `set-tip`; `open_ledger(repo)`, `Ledger.cells(...)`; tier A via `lint_runs`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` green; `basedpyright scripts/style` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- On the Phase 4 cargo-liner ledger: `summary --repo cargo-liner --json` prints all keys; `reconcile` on an empty work state prints no change.

### Phase 6 — Style production bootstrap: instruction doc, format fields and /showrunner:add_style  · status: todo
**Depends on:** Phase 5

#### Work Order

**Goal:** a repo's main showrunner runs `/showrunner:add_style`, which writes the style production doc and four standing unit plans, creates the style merge branch and checkout, and starts the style showrunner under it — or attaches the standing one; `/showrunner:produce` follows `docs/style_production.md` for a `**Kind:** style` doc.

**Spec:**
- `docs/production_format.md` (Production Context fields :84-117): add after `Merge branch` (:90):
  - `**Kind:** style` — optional; absent means a product production. A style production reads `docs/style_production.md`.
  - `**Branch point:** <branch>` — style productions only: the parent's merge branch the style branch syncs from and hands back to.
  - `**Parent production:** <path to parent production doc>` — style productions only: where hand-offs go, and the doc a root style showrunner merges through (Phase 9).
  and note that `**Proposals:**` (:114) is required for a style production (held changes are filed there).
- `docs/delegate_plan_format.md` (header fields :29-45): add the optional header `> **Standing:** style_plan.py` — a standing plan never closes its run; `<NextPhase/>` refills it (Phase 8). A standing plan is git-excluded: the ledger is its record, so checkpoint and shrink never commit it.
- `commands/showrunner/produce.md`: after it reads `production_format.md` (:17), one conditional: when `PRODUCTION_DOC` carries `**Kind:** style`, read `~/.claude/docs/style_production.md` and follow it wherever it differs from this file.
- New `docs/style_production.md` (the instruction document; rules stay in `~/rust/nate_style`). Sections:
  - **Tiers** — the A-E table (A: lint run per synced tip; B: generator sites judged by a worker, the uncovered surface read as C; C: a worker reads the file; D: once per crate; E: governs how fixes are made, not swept).
  - **Families** — the four units and their rules (as in `rules.py list`); one unit director per family applies an exception decided in one crate the same way in every crate.
  - **Ticket queue** — one ticket per phase; read ticket = (family, one slice of about 5,000 lines in one module subtree, 10-25 files); listed ticket = (rule, crate) with its sites, split above about 150; structure ticket = one crate. Tier D first. A repair phase always comes before a new ticket. After an interruption the unit director resumes its open ticket (`style_ledger.py ticket open`), never takes a second one. Backlog clears cargo-liner before hana.
  - **Ledger** — cell states, what makes a cell stale, verdicts.
  - **Sync** — the five steps on a sync message (Phase 8 fills the command lines).
  - **Merging back** — the style showrunner merges each unit checkpoint into the style merge branch as soon as it arrives (that clears berth protection), then sends the hand-off (Phase 9 fills the contract).
  - **Yielding** — selection skips any file reserved on the board, changed on a live unit's unmerged branch, or dirty in its worktree; a merge conflict on a style ticket drops every finding touching that file with all its edits in other files, releases reservations no remaining finding touches, reruns the gate; product work always wins. Style units obey build holds and memory admission like any unit.
  - **Big changes** — the mechanical criteria (Phase 7) and the proposal path (Phase 11).
  - **Report** — `/showrunner:style_report` (Phase 10).
  - **Lifetime** — standing; while a root it merges its own hand-offs (Phase 9); a parent's wrap detaches it (Phase 11).
  Later phases append their command lines to these sections.
- New `scripts/style/add_style.py` and `commands/showrunner/add_style.md`. Run by a repo's main showrunner from its checkout:
  1. Reads the caller's production doc (`add_unit.read_production`, `production_field`): repository, merge branch (the branch point), `**Proposals:**` (refuse with a one-line message naming the field if absent), slug.
  2. Looks for a standing style production for (repo, branch point): `<repo main checkout>/../<repo>-style` checkout holding `docs/<repo>-style-production.md` with the same `**Branch point:**`. If found: `showrunners.py attach <repo>-style <caller slug>`, rewrites its `**Parent production:**` and `**Proposals:**` to the caller's, and re-sends every `style_ledger.py handoff pending` hand-off to the caller (Phase 9 message form; until Phase 9 lands, print them). Stops there. A main showrunner on a different branch point starts a separate style production for that branch (slug `<repo>-style-<branch point with / as ->`).
  3. Otherwise creates the style merge branch `style/<branch point with / as ->` (`style/main`, `style/init-catalyst`) from the branch point tip, and the showrunner checkout as a git worktree at `<parent dir of the repo checkout>/<repo>-style` on that branch.
  4. Writes `docs/<repo>-style-production.md` in that checkout from a template in `add_style.py`: title `# Production — <repo>-style`, status running, Production Context (`**Kind:** style`, `**Repository:**`, `**Merge branch:** style/...`, `**Branch point:**`, `**Parent production:**`, `**Showrunner checkout:**`, `**Log:** docs/<repo>-style-log.md`, `**Proposals:**`, `**Updates:** on demand`, `**Push:** git`, `**Merge tests:**` copied from the parent), a `## Units` table with the four units (`names-unit`, `values-unit`, `types-unit`, `modules-unit`), each Owns `none — takes what the board leaves free`, plans `docs/style/<unit>.md`.
  5. Writes each unit's standing plan `docs/style/<unit>.md`: title, `> **Production: <repo>-style** — unit ...`, `> **Standing:** style_plan.py`, a Delegation Context naming the repo, its `verify.sh` lines, the family's rules and the yield and mechanical rules (pointing at `docs/style_production.md`), and no phases (the refill writes them).
  6. Adds `docs/style/`, `docs/<repo>-style-production.md` and the log to the repo's `.git/info/exclude` (shared by every worktree), so nothing of the style production lands in the product repo.
  7. Runs `style_ledger.py set-tip` at the branch point tip, then `add_showrunner.py --production <style doc>` (account from the `showrunners` role), which starts `/showrunner:produce <doc>` and attaches it under the caller.
  `--check` prints every step without acting. Every step is idempotent: a rerun after a partial failure continues.
- `commands/showrunner/add_style.md`: one paragraph and the script call; when to run it (a repo's main showrunner, once per branch point).
- Tests `scripts/style/test_add_style.py`: a temp repo and a fake parent doc; new production (branch, worktree, doc fields, four plans, excludes); rerun is a no-op; existing standing production attaches instead of creating; missing Proposals refuses. Stub `add_showrunner` and `showrunners` calls the way `scripts/production/test_add_showrunner.py` does.

**Files:**
- `docs/production_format.md` — `Kind`, `Branch point`, `Parent production`; Proposals required for style
- `docs/delegate_plan_format.md` — `Standing` header
- `commands/showrunner/produce.md` — the conditional read
- `docs/style_production.md` — new
- `scripts/style/add_style.py` — new
- `scripts/style/test_add_style.py` — new
- `commands/showrunner/add_style.md` — new
- `pyrightconfig.json` — `scripts/production` in the `scripts/style` env's extraPaths (add_style imports `add_unit`, `showrunners`)

**Workers:** 2 writers — the docs split from the script.
- `impl` — `scripts/style/add_style.py`, `scripts/style/test_add_style.py`, `commands/showrunner/add_style.md`; hub: `pyrightconfig.json`
- `test` (opens as impl) — `docs/production_format.md`, `docs/delegate_plan_format.md`, `commands/showrunner/produce.md`, `docs/style_production.md`

**Constraints from prior phases:** Phase 2: `rules.py list` gives each family's rules. Phase 4: `style_ledger.py set-tip --repo R --tip C`. Phase 5: `style_ledger.py handoff pending --repo R`, `ticket open --repo R --unit U`, `reconcile`. `add_showrunner.py --production <doc>` attaches the child under the caller; `add_unit.read_production` needs Merge branch, Showrunner checkout, relative Log, a `## Units` line and a `-production.md` name. `add_unit.py` sets each unit's berth target to the production's merge branch (`style/...`) when `.claude/config/berth.toml` exists.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` and `-s scripts/production` green; `basedpyright scripts/style` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `add_style.py --check` from `/home/natepiano/rust/cargo-liner` with the remove-running-section doc prints the seven steps and refuses on the missing `**Proposals:**` line.
- `git grep -n 'Kind:\*\* style' docs/production_format.md commands/showrunner/produce.md` finds both.

### Phase 7 — Ticket selection and the mechanical-fix filter  · status: todo
**Depends on:** Phase 2, Phase 5

#### Work Order

**Goal:** `style_plan.py next-phase --unit <unit>` writes the unit's next ticket as a todo phase with a full Work Order, chosen only from files no live work touches, and `style_footprint.py` decides whether a finding's diff is mechanical.

**Spec:**
- New `scripts/lib/berth_board.py`: `read_board(repo: Path) -> Board` runs `cargo-berth board --json` in `repo` and parses the envelope (`output.rs:366`) → `payload.data` (`BoardModel`, `board/rows.rs:81`). `Board.reservations() -> tuple[Reservation, ...]` collects every entry of `targets[].reservations`, `ready_now`, `waiting`, `unresolved_overlaps`, `unconstrained_reservations` and `resolved` (each `{journal_position, entries[]}`), deduplicated by `reservation_id`; `Reservation` holds `reservation_id`, `target`, `holder_worktree_root`, `holder_branch`, `scopes` (file paths and `tree:` prefixes), `lifecycle`, `edit_blocking_status`. Exit code 4 or empty stdout → an empty board (as `production_lifecycle.py:311` treats it). `Reservation.blocks(path)` is true when `edit_blocking_status` is blocking and a scope covers the path. Typed with TypedDicts; no `Any`. Note: `board` reconciles the journal with Git before rendering (README :387); `check` acquires, so selection never calls it.
- New `scripts/style/style_plan.py`:
  - **Busy files** for a repo: every path a blocking reservation covers (`berth_board`), every path changed on a live unit's unmerged branch (`git diff --name-only <merge branch>...<unit branch>` for each Units row of the parent production and of the style production, from `scripts/production/live_units.py` or the production doc's Units table), and every dirty path in those units' worktrees (`git status --porcelain`). Style units' own open tickets count as busy for other style units.
  - **Slices:** group in-scope `.rs` files by module subtree (directory under each crate's `src/`), walk depth-first, and cut slices of about 5,000 lines and 10-25 files without splitting a directory unless it alone exceeds the bound. Deterministic for a given tip.
  - **Ticket kinds:** `structure` — one crate, family `modules`, all its tier D rules; `listed` — (rule, crate) with its generator sites from `candidate_generators.enumerate_candidates`, split into tickets of at most 150 sites (the cap moved here from the generator); `read` — (family, slice), covering the family's tier C rules and `#uncovered` pseudo-rules. A ticket covers only cells that are `open` or `stale`.
  - **Order** for one unit: its open ticket first (`style_ledger.py ticket open`; resume, never a second); then `modules` structure tickets before any other family's work in that crate (tier D first); then the crate with the most stale cells; cargo-liner's backlog clears before hana's (by scheduling: only one repo per style production, so this is the roll-out order in Phase 14).
  - A candidate ticket loses every file that is busy; a ticket left with no files is skipped. For a listed ticket, a busy file drops its sites.
  - `style_plan.py next-phase --unit U --plan <path> --repo R`: if a repair phase is todo in the plan, prints `repair first` and exits 0 without writing. Else removes the plan's `done` ticket phase (the ledger holds the record), picks the next ticket, records it `queued` then `assigned`, and appends `### Phase <n> — <ticket id>  · status: todo` with a Work Order: Goal (the rules and files), Spec (each rule's file path in `~/rust/nate_style/rust/` or the repo-local dir; for listed tickets the sites; the yield rule; the mechanical rule and that non-mechanical findings are recorded `held` for Phase 11's filing; record every judged cell with `style_ledger.py record`), Files (the ticket's files), Workers (`2 writers` splitting files by subtree when over 12 files, else `1 writer + 1 tester` with the tester opening as impl and re-reading the diff against the rules), Acceptance gate (the repo's `verify.sh check/test/lint` for each touched package, `style_footprint.py check`, and every ticket cell recorded). If no ticket is available, prints `waiting for sync` and exits 3, writing nothing.
  - `style_plan.py queue --repo R [--unit U]` prints the tickets it would make, in order, with file counts (read only).
- New `scripts/style/style_footprint.py`: `style_footprint.py check --repo R --base B --head H [--finding-map FILE.json]`. A finding is **mechanical** when all hold (design, section 7, verbatim):
  1. no file is added, deleted, renamed or moved, and no `mod` line changes;
  2. no item reachable from another crate changes name, signature, fields or variants;
  3. no type gains or loses a field, variant, generic parameter or trait impl, and no new type, trait or error type appears;
  4. a rename's sites sit in one crate and at most 10 files;
  5. the finding's diff is under 200 changed lines;
  6. behavior is preserved: no condition, literal value or control flow changes meaning, and no serialized, reflected or configured name (serde and reflect names, config keys, documented usage) changes.
  Checks 1-5 are decided on the actual diff (`git diff -M B H`; reachability for 2 = `pub` items not under a `pub(crate)`/private module path, read from the changed lines and the module's `mod` declarations; 3 from added/removed `struct`/`enum` fields and variants, `impl ... for`, new `struct|enum|trait`). Check 6 flags (does not decide) any changed line containing a condition (`if`, `match` arm guard, `while`, comparison operators), a literal, or a `#[serde(`/`#[reflect(`/`rename` attribute or string key. Output per finding: `mechanical` or `review: <checks that failed or were flagged>`; exit 0 when all mechanical, 1 otherwise. It is a conservative filter: a finding it cannot clear goes to review before it is applied. `--finding-map` maps finding ids to the files/hunks each owns (workers write it); without it the whole diff is one finding.
  - A ticket that crosses the line has those findings pulled out with every edit that depends on them, and the rest merges (unit director's procedure, in `docs/style_production.md`).
- Append to `docs/style_production.md` **Ticket queue** and **Big changes**: the `next-phase`, `queue` and `style_footprint.py check` command lines and exit codes.
- Tests: `scripts/lib/test_berth_board.py` (a recorded envelope fixture with one blocking and one clear reservation; exit 4); `scripts/style/test_style_plan.py` (temp two-crate repo, a fake board and a fake live-unit branch: busy files excluded; slicing bounds; structure before read; 150-site split; repair first; waiting for sync exit 3; resume returns the open ticket); `scripts/style/test_style_footprint.py` (one diff per check, plus a clean rename in 3 files).

**Files:**
- `scripts/lib/berth_board.py`, `scripts/lib/test_berth_board.py` — new
- `scripts/style/style_plan.py`, `scripts/style/test_style_plan.py` — new
- `scripts/style/style_footprint.py`, `scripts/style/test_style_footprint.py` — new
- `docs/style_production.md` — command lines

**Workers:** 2 writers — selection and the footprint filter are independent modules.
- `impl` — `scripts/lib/berth_board.py`, `scripts/lib/test_berth_board.py`, `scripts/style/style_plan.py`, `scripts/style/test_style_plan.py`; hub: `docs/style_production.md`
- `test` (opens as impl) — `scripts/style/style_footprint.py`, `scripts/style/test_style_footprint.py`; sends its command line to `impl` for the doc

**Constraints from prior phases:** Phase 1: `enumerate_candidates` has no cap. Phase 2: `rules.load_rules` (tier, family, input scope, surface; `#uncovered` pseudo-rules are tier C in the same family per Phase 4). Phase 4: `Ledger.cells(rule, crate)` with states; `style_ledger.py record`. Phase 5: `style_ledger.py ticket add|assign|checkpoint|merge|drop|open`. Phase 6: unit plans at `docs/style/<unit>.md` carry `> **Standing:** style_plan.py` and are git-excluded; `docs/style_production.md` exists with the sections named there; the style production doc lists four units and its parent in `**Parent production:**`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` and `-s scripts/lib` green; `basedpyright scripts/style scripts/lib` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `style_plan.py queue --repo cargo-liner` against the real board prints structure tickets first and no file the live board reserves.

### Phase 8 — Standing-plan refill in /unit:direct and the sync trigger  · status: todo
**Depends on:** Phase 6, Phase 7

#### Work Order

**Goal:** a unit director on a standing plan never closes its run — it refills from the ledger, idles as `waiting for sync`, and wakes on a sync — and every merge into a branch point messages that branch's style showrunner.

**Spec:**
- `commands/unit/direct.md` `<NextPhase>` (:1458-1475): before the "no todo phase left" branch (:1459-1461), when the plan header carries `**Standing:** style_plan.py`:
  1. run `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py next-phase --unit <unit> --plan <plan> --repo <repo>`;
  2. exit 0 → continue the loop with the new todo phase (a repair phase, if one is todo, always runs first — `next-phase` refuses to add a ticket over it);
  3. exit 3 (`waiting for sync`) → set the unit status to `waiting for sync`, report it to the showrunner once, and end the turn idle; the sync message (below) wakes the unit director, which runs step 1 again;
  4. never run FinalGate, RunAsBuilt, the final PeriodicCI, CICleanup or RunSummary for a standing plan.
  At run start (and after any compaction or restart) on a standing plan, the unit director runs `style_ledger.py ticket open --repo <repo> --unit <unit>` and, if a ticket is open, resumes that phase; it never takes a second one.
  A standing plan is git-excluded: the checkpoint and the as-built shrink skip committing the plan (the checkpoint commits the code only; `style_ledger.py ticket checkpoint` records the commit).
- `docs/delegate/phase_end.md` (`<PhaseEnd>` :13, `<NextPhase/>` at :39, :52): the same exception in one paragraph — for a standing plan, phase end runs the refill instead of closing, and the plan is never committed or shrunk.
- New `scripts/style/style_sync.py`:
  - `style_sync.py notify --repo-dir <checkout> --branch <branch> --tip <sha>`: finds a style production for (repo, branch) — the standing `<repo>-style` production doc whose `**Branch point:**` equals `branch` (same lookup as `add_style.py` step 2; share the function). None → exit 0 silently. Found → `send.py --to $(showrunners.py address <style slug>) --text 'From <caller>: branch point <branch> moved to <sha7>; sync.' --key style-sync:<slug>`. A failed send prints one line and exits 0 (a merge never fails on it).
  - `style_sync.py lint --repo R --checkout <style checkout> --tip <sha>`: runs the tier A record on the synced tip — clippy with `--all-features` and the workspace lint config, mend and rustfmt — at workspace scope through `bash ~/.claude/scripts/delegate/verify.sh lint` (read its usage for the workspace form; if it has none, add a workspace form there rather than calling cargo directly), parses per-file diagnostic counts, and calls `style_ledger.py lint-record` once per tool.
- `scripts/production/merge_checkpoint.py` `run` (:856): after the push step (:886-890) succeeds, call `style_sync.py notify` with the production's repository checkout, merge branch and new tip. `scripts/production/merge_gate.py` `merge_checkpoint` (:459): after `git push` (:502), the same call. Both through a subprocess with a 30 s timeout; any failure is one printed line.
- Append to `docs/style_production.md` **Sync**, the style showrunner's steps on a sync message, skipping a tip `style_ledger.py tip seen` reports `seen`:
  1. merge the new tip into the style merge branch and gate it (its Merge tests);
  2. `style_sync.py lint` (tier A);
  3. `style_ledger.py set-tip` then `status`: cells whose inputs changed turn stale, new files open;
  4. tell each unit director to merge the style branch at its next pause between tickets, and wake any `waiting for sync`;
  5. `style_ledger.py tip done`.
  And, for a unit merging the synced style branch mid-ticket: a clean merge → the worker rechecks that file's incoming diff before the checkpoint and the row records the new fingerprint; a conflict → drop every finding that touches that file with all its edits in other files (a multi-file rename goes as a whole), release the reservations for files no remaining finding touches, rerun the gate on what is left; the dropped findings' cells stay stale for a later ticket.
- Tests: `scripts/style/test_style_sync.py` (no style production → silent; found → the send call, with `send.py` stubbed; send failure exits 0; lint parsing from a recorded verify.sh output); `scripts/production/test_merge_checkpoint.py` and `test_merge_gate.py` (the notify call happens after a push and not when the push fails).

**Files:**
- `commands/unit/direct.md` — standing-plan refill, resume, no close
- `docs/delegate/phase_end.md` — the standing exception
- `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py` — new
- `scripts/style/add_style.py` — share the style-production lookup with `style_sync.py`
- `scripts/production/merge_checkpoint.py`, `scripts/production/test_merge_checkpoint.py` — notify after push
- `scripts/production/merge_gate.py`, `scripts/production/test_merge_gate.py` — notify after push
- `scripts/delegate/verify.sh` — workspace lint form, only if missing
- `docs/style_production.md` — Sync section

**Workers:** 2 writers — the direct.md/phase_end.md procedure splits from the scripts.
- `impl` — `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py`, `scripts/style/add_style.py`, `scripts/production/merge_checkpoint.py`, `test_merge_checkpoint.py`, `scripts/production/merge_gate.py`, `test_merge_gate.py`, `scripts/delegate/verify.sh`
- `test` (opens as impl) — `commands/unit/direct.md`, `docs/delegate/phase_end.md`; hub: `docs/style_production.md`

**Constraints from prior phases:** Phase 5: `style_ledger.py tip seen|done`, `ticket open|checkpoint`, `lint-record` (Phase 4), `set-tip`, `status`. Phase 6: standing plans carry `> **Standing:** style_plan.py`, live at `docs/style/<unit>.md`, git-excluded; `add_style.py` holds the lookup of a standing style production by (repo, branch point); `docs/style_production.md` has a Sync section to fill. Phase 7: `style_plan.py next-phase --unit U --plan P --repo R` exits 0 (phase written, or `repair first`) or 3 (`waiting for sync`).

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` and `-s scripts/production` green; `basedpyright scripts/style scripts/production` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `style_sync.py notify --repo-dir ~/rust/cargo-liner --branch main --tip HEAD` exits 0 and sends nothing (no style production yet).

### Phase 9 — Child hand-off contract  · status: todo
**Depends on:** Phase 5, Phase 6

#### Work Order

**Goal:** a parent showrunner merges a style child's hand-off through `merge_checkpoint.py --from-child`, acknowledges it, and the child's ledger marks it merged only once the branch point holds it; a root style showrunner merges its own hand-offs the same way.

**Spec:**
- `scripts/production/merge_checkpoint.py`:
  - New mode `merge_checkpoint.py --from-child <child slug> --checkpoint <hash> --production <parent doc> --started <time>`. `request_from` (:278) refuses a sender with no Units row (:285); `--from-child` takes a separate path that never reads the Units table.
  - Checks, each refusal one line: the child is attached under this production (`showrunners.py` `parent_of(child) == parent slug`), or the child itself is running it as a root (`parent_of(child)` is empty and the caller passes `--root-self`); the hash is on the child's merge branch (read from the child's production doc, found through `showrunners.py` and the child's `**Showrunner checkout:**`); it descends from the child's last merged hand-off.
  - Fixed metadata: phase `style hand-off`, no review trial, `--delivers`/`--parallel` not asked.
  - Merge subject `Merge <child slug> hand-off (<hash7>) into <branch point>`. Extend `merge_branch_history` (:333, regex :336) with a second pattern `^Merge (.+?) hand-off \(([0-9a-f]+)\)` so the last merged hand-off is read from merge history like a unit's checkpoint; existing unit entries parse unchanged.
  - Push as the production pushes today (`push_git` or `push_validate`), then the Phase 8 `style_sync.py notify` call, then an acknowledgment: `send.py --to $(showrunners.py address <child>) --text 'From the showrunner of <parent slug>: hand-off <hash7> merged as <merge7>.'`.
- `commands/showrunner/produce.md`: replace "A child showrunner is never a unit. It has no Units row, and you never merge its branch" (:114-115) with that rule plus one exception: a style child in the same repo (`**Kind:** style`) hands its merge branch off through `merge_checkpoint.py --from-child`, and every hand-off gets a correctness review (the same review a unit checkpoint gets, on the hand-off's diff against the branch point) before the merge. The parent merges on receipt of `From the showrunner of <style slug>: hand-off <hash>`.
- Append to `docs/style_production.md` **Merging back**:
  - after merging a unit checkpoint into the style merge branch, the style showrunner runs `style_ledger.py handoff add --checkpoint <style branch tip>` and sends `From the showrunner of <style slug>: hand-off <hash>` to `showrunners.py address <parent>`, then `handoff send`. Style work never waits for a sync to go home.
  - on the acknowledgment: `style_ledger.py handoff merged --checkpoint <hash> --merge-commit <m>`, which accepts only after the ancestry check (Phase 5).
  - while a root (no parent): merge its own hand-offs into the branch point with `merge_checkpoint.py --from-child <own slug> --root-self --production <the doc in **Parent production:**>`; no other production writes that branch then. Hand-offs a departing parent had not merged stay pending in the ledger and are merged this way.
  - at startup: `style_ledger.py reconcile`, then resend every `sent` hand-off whose parent changed.
- Tests in `scripts/production/test_merge_checkpoint.py`: an attached child's hand-off merges with the new subject and acknowledges; an unattached child is refused; a hash off the child's branch is refused; a non-descending hash is refused; `--root-self` with no parent merges; `merge_branch_history` reads both entry kinds.

**Files:**
- `scripts/production/merge_checkpoint.py` — `--from-child`, `--root-self`, hand-off history pattern, acknowledgment
- `scripts/production/test_merge_checkpoint.py` — the cases above
- `commands/showrunner/produce.md` — the hand-off exception
- `docs/style_production.md` — Merging back

**Workers:** 1 writer + 1 tester — one script; the Spec's checks and subject are testable first.
- `impl` — `scripts/production/merge_checkpoint.py`, `commands/showrunner/produce.md`, `docs/style_production.md`
- `test` — the new cases in `scripts/production/test_merge_checkpoint.py`

**Constraints from prior phases:** Phase 5: `style_ledger.py handoff add|send|merged|pending`, `reconcile` (merged only by ancestry). Phase 6: style docs carry `**Kind:** style`, `**Branch point:**`, `**Parent production:**`, `**Showrunner checkout:**`; `add_style.py` re-sends pending hand-offs on re-attach in the message form above. Phase 8: `merge_checkpoint.py run` calls `style_sync.py notify` after its push; the hand-off path reuses that call.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `basedpyright scripts/production` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `merge_checkpoint.py --from-child nobody --checkpoint HEAD --production docs/plans/build-followups-production.md --started now` is refused with the not-attached line.

### Phase 10 — Style report and the dailies style line  · status: todo
**Depends on:** Phase 5, Phase 6

#### Work Order

**Goal:** `/showrunner:style_report [rule|crate]` prints the coverage table, and the parent's dailies shows a one-line style summary under the child's row.

**Spec:**
- `style_ledger.py report --repo R [--rule N | --crate K]` prints, in the user zone (`scripts/lib/user_zone.py`), this format (design, section 5; the sample is the target):

  ```
  hana-style — synced init/catalyst 18dd3bf at 14:05 EDT, 1,847 files (+16 since yesterday)
  Rules done: 21 of 51 (14 by lint) · cells current 62% · open 9,118 · stale 1,402 · held 37
  Waiting to merge back: 2 checkpoints, oldest 12:40

  | Rule                           | Tier | Current       | Stale | Open | Held | Done |
  | use-narrowest-visibility       | A    | 1,847 (100%)  |     0 |    0 |    0 | yes  |
  | dont-repeat-type-name-in-fields| B    | 1,839 (99.6%) |     8 |    0 |    3 | no: 8 stale |
  | spell-out-names                | C    | 1,212 (65.6%) |   140 |  495 |    0 | no   |
  | when-to-split-a-module         | D    | 33 of 33 crates |   0 |    0 |   12 | yes, 12 held |
  ...
  Since last report: 6 rules gained, 2 lost "done" to new files; 3 proposals filed.
  ```

  A rule is done when every in-scope file at the synced tip is current for it; new files make it not done again until their tickets land. Rules counted exclude tier E. "Since last report" compares with a snapshot the command saves under `~/.local/state/style/<repo>/last_report.json`. `--rule` prints that one row plus its open tickets; `--crate` prints the same table for one crate plus the oldest open ticket.
- New `commands/showrunner/style_report.md`: run in the style showrunner or any showrunner above it; resolves the repo from the style production (below the caller via `showrunners.py children`, or the caller itself) and prints `style_ledger.py report`.
- Dailies (`dailies_render.py:731` `parse_child_report` → `check_keys` :734 rejects unknown keys; `last_report()` :1806 is pure; saving is `dailies_input.py` `save_last_report` :784, called :1069):
  - the saved report gains an optional `style` object: `rules_done`, `rules_total`, `cells_current_pct`, `stale`, `proposals`, `waiting_to_merge_back`, `oldest_waiting`, `synced_at` (the `style_ledger.py summary` keys, Phase 5);
  - `dailies_input.py` fills it when the production doc carries `**Kind:** style`, from `style_ledger.py summary --repo R --json`;
  - `parse_child_report` accepts `style` as an optional key (add it to `ChildReport` :395 as `StyleSummary | None`), and a report without it parses as today;
  - `child_row` (:1690) prints it under the child's row: `hana-style  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 14:05`. A hand-off waiting over 4 hours adds ` · hand-off waiting <h>h` as a flag.
  - `/showrunner:dailies expand hana-style` shows the four units as usual (no change; confirm in `commands/showrunner/dailies.md` :29-31).
- Append the report command to `docs/style_production.md` **Report**.
- Tests: `scripts/style/test_style_ledger.py` (report rows for each tier; done with held; since-last-report deltas; `--crate`); `scripts/production/test_dailies_render.py` (a child report with and without `style`; an unknown key inside `style` refused; the flag over 4 h); `scripts/production/test_dailies_input.py` (the `style` object saved for a style doc only).

**Files:**
- `scripts/style/style_ledger.py`, `scripts/style/test_style_ledger.py` — `report`
- `commands/showrunner/style_report.md` — new
- `scripts/production/dailies_render.py`, `scripts/production/test_dailies_render.py` — `style` parse and render
- `scripts/production/dailies_input.py`, `scripts/production/test_dailies_input.py` — `style` save
- `docs/style_production.md` — Report

**Workers:** 2 writers — the ledger report splits from the dailies changes.
- `impl` — `scripts/style/style_ledger.py`, `scripts/style/test_style_ledger.py`, `commands/showrunner/style_report.md`; hub: `docs/style_production.md`
- `test` (opens as impl) — `scripts/production/dailies_render.py`, `test_dailies_render.py`, `dailies_input.py`, `test_dailies_input.py`

**Constraints from prior phases:** Phase 4: cell states and `lint_runs`. Phase 5: `style_ledger.py summary --repo R --json` keys `rules_done`, `rules_total`, `cells_current_pct`, `stale`, `proposals`, `waiting_to_merge_back`, `oldest_waiting`, `synced_at`, `synced_tip`; hand-off states. Phase 6: style docs carry `**Kind:** style` and live in `<repo>-style` checkouts found through `showrunners.py`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` and `-s scripts/production` green; `basedpyright scripts/style scripts/production` `0 errors, 0 warnings`; merge gate `--tests-only` green.
- `style_ledger.py report --repo cargo-liner` on the Phase 4 ledger prints the header and one row per rule.

### Phase 11 — Proposal batching and wrap detaching a style child  · status: todo
**Depends on:** Phase 4, Phase 6, Phase 7, Phase 10

#### Work Order

**Goal:** held big changes become batched proposal issues in the parent's Proposals folder, and a parent's wrap detaches its style child instead of refusing.

**Spec:**
- New `scripts/style/style_proposals.py file --repo R --proposals <dir> --parent <slug>`: groups `held` cells with no issue yet into batches — one issue per (rule, crate) for renames and type changes, one per crate for a restructure (every tier D finding and every `mode: propose` rule's finding goes this way) — and writes each as a hanadocs issue through `scripts/production/proposal_issue.py` (read its CLI; tags `proposal` and `style`), with: the sites, before and after for each, the live units holding those files (from `style_plan`'s busy-file lookup), and a recommended answer. Then sets each cell's verdict detail to the issue path (`style_ledger.py record ... --verdict held --detail <issue>`).
  - Held findings come from workers: the unit's ticket Work Order (Phase 7) records a non-mechanical finding as `held` with a JSON detail (site, before, after, kind `rename|type|restructure`); `file` reads those.
  - **Asked** the way proposals already are, never unasked: the count shows in the report and the dailies line (Phase 10), `/showrunner:proposals` lists them (confirm `proposals_table.py` picks up the `style` tag; add it if the table filters by tag), `/adhoc_review` walks them.
  - **Approved:** built as a style phase — `style_plan.py` gains a `proposal` ticket kind that turns an approved issue into a ticket; when it moves files a live unit holds, the parent schedules it through `<CrossUnitChange>` (`produce.md` :1070-1093). **Declined:** `style_proposals.py decline --issue <path> --words '<the user's words>'` sets the cells to `waived <words>`; a general reason becomes a repo-local rule through `/local_style`.
- `scripts/production/production_lifecycle.py`:
  - `refuse_running_children` (:335): a running child whose production doc carries `**Kind:** style` is detached (`showrunners.detach`) and told `<parent> wrapped, so you are a root now: merge your own hand-offs (docs/style_production.md, Merging back).`; any other running child still holds the wrap.
  - `retire_reservations` (:308) reads the board through `scripts/lib/berth_board.py` (Phase 7) instead of the top-level `reservations` list, which holds ids, not dicts, so today it releases nothing. (style-unit's fix, found while planning this phase; a one-line revert if the showrunner disagrees.)
- Append to `docs/style_production.md` **Big changes** and **Lifetime**: the `file` and `decline` commands, when to run `file` (after each ticket checkpoint that recorded held cells), the approved path, and the wrap detach.
- Tests: `scripts/style/test_style_proposals.py` (batching by kind; issue fields; decline waives; with `proposal_issue.py` stubbed); `scripts/production/test_production_lifecycle.py` (a style child detached and told; a product child still refuses; reservations released from a board fixture).

**Files:**
- `scripts/style/style_proposals.py`, `scripts/style/test_style_proposals.py` — new
- `scripts/style/style_plan.py`, `scripts/style/test_style_plan.py` — `proposal` ticket kind
- `scripts/production/proposals_table.py` — the `style` tag, only if it filters
- `scripts/production/production_lifecycle.py`, `scripts/production/test_production_lifecycle.py` — detach a style child; board reader
- `docs/style_production.md` — Big changes, Lifetime

**Workers:** 2 writers — proposals split from the lifecycle change.
- `impl` — `scripts/style/style_proposals.py`, `test_style_proposals.py`, `scripts/style/style_plan.py`, `test_style_plan.py`, `scripts/production/proposals_table.py`; hub: `docs/style_production.md`
- `test` (opens as impl) — `scripts/production/production_lifecycle.py`, `test_production_lifecycle.py`

**Constraints from prior phases:** Phase 4: verdicts `held`/`waived` with `verdict_detail`; `style_ledger.py record`. Phase 6: style docs carry `**Kind:** style` and `**Proposals:**` (required). Phase 7: `scripts/lib/berth_board.py` (`read_board`, `Board.reservations()`, `Reservation.reservation_id`, `holder_worktree_root`); `style_plan.py` ticket kinds and busy-file lookup. Phase 10: the report and dailies line count proposals.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/style -p 'test_*.py'` and `-s scripts/production` green; `basedpyright scripts/style scripts/production` `0 errors, 0 warnings`; merge gate `--tests-only` green.

### Phase 12 — Pilot under cargo-liner's showrunner  · status: todo
**Depends on:** Phase 8, Phase 9, Phase 10, Phase 11

#### Work Order

**Goal:** a `cargo-liner-style` production runs under cargo-liner's showrunner with `modules-unit` on all crates, `names-unit` on `cargo-handler` and `types-unit` on one crate with a large candidate set, and every pilot pass criterion is measured and reported.

**Spec:**
- Bootstrap (through the build-followups showrunner, which relays to cargo-liner's showrunner, production `remove-running-section`, session `cargo-liner`, doc `/home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md`):
  1. that doc gains `**Proposals:**` (its showrunner picks the folder; recommend `~/rust/hanadocs/issues`, as hana uses);
  1b. cargo-liner's 11 local rules (`docs/cargo-mend/style/` 5, `docs/cargo-port/style/` 6) gain `tier` and `family` (and `input_scope` where not `crate`), committed in cargo-liner through its showrunner's normal flow, so `rules.py list --repo ~/rust/cargo-liner` loads them without a validation error;
  2. its showrunner runs `/showrunner:add_style`, which creates `style/main`, the `cargo-liner-style` checkout, doc and four standing plans, and starts the style showrunner attached under it;
  3. the style showrunner launches only `modules-unit` (all crates), `names-unit` (scope limited to crate `cargo-handler`) and `types-unit` (one crate: the one with the largest `style_plan.py queue` candidate count for the types family); `values-unit` stays unlaunched. Scope limits go into each unit plan's Delegation Context as `**Pilot scope:** <crates>`; `style_plan.py next-phase` honors that line (add it in this phase if missing).
- Measure, all before Phase 13 (design, pass criteria, verbatim):
  - **Recall:** on 20 hand-checked files per family, the family pass finds at least 95% of what one-rule passes find. Method: pick 20 files per launched family the pass has checked; rerun each family rule alone on them (one worker per rule, same model); the union of one-rule findings is the denominator.
  - **Hold time:** median claim to clear under 2 hours, none over 6. From cargo-berth: claim time to the time `edit_blocking_status` reads Clear (not to release).
  - **Quiet-branch hand-off:** a style checkpoint reaches `main` within 4 hours with no product merge in between.
  - **Restart:** the style showrunner and one unit director killed mid-ticket resume with no lost or doubled ticket and no hand-off sent twice. Kill only the pids this phase started (the user's rule: never kill by pattern); restart through `/shutdown` for the sessions.
  - **Yielding:** no product unit waits on a style reservation (cargo-berth board history and the product units' reports over the pilot).
  Also measured for the record: family recall per rule (shrink families if recall drops), which rules proved file-local (candidates for `input_scope: file`), tickets per hour per unit.
- Report: `docs/plans/build-followups-style-pilot.md` (in this worktree) with one table per criterion, pass or fail, the numbers and their source commands. Send its pass/fail line to the build-followups showrunner.
- Fixes the pilot shows are needed go into the style scripts in this worktree as their own commits in this phase, each named in the report.
- If any criterion fails: stop after the report; Phases 13 and 14 stay blocked, and the report names what failed and the proposed change.

**Files:**
- `docs/plans/build-followups-style-pilot.md` — new, the pilot report
- `scripts/style/style_plan.py`, `scripts/style/test_style_plan.py` — `**Pilot scope:**` line
- `scripts/style/*` — fixes the pilot shows, each named in the report

**Workers:** 2 writers — the recall study splits from the operational measurements.
- `impl` — the bootstrap, hold time, quiet-branch, restart and yielding measurements, `style_plan.py` scope line and its test, and any script fixes
- `test` (opens as impl) — the recall study (one-rule reruns on the 20-file samples) and its table in `docs/plans/build-followups-style-pilot.md`

**Constraints from prior phases:** Phases 1-11 merged to `~/.claude` main (the production rule: each merged phase goes to main at once), so `/showrunner:add_style`, `/showrunner:style_report`, `style_plan.py`, `style_sync.py`, `merge_checkpoint.py --from-child` and the standing refill in `/unit:direct` are live. Style units' berth target is `style/main` (`add_unit.py:638-642`; cargo-liner has `.claude/config/berth.toml`). cargo-liner pushes through validate_and_push `--quick` (no `**Push:**` line), which skips clippy; tier A comes from `style_sync.py lint`.

**Acceptance gate:**
- `docs/plans/build-followups-style-pilot.md` holds a pass or fail with numbers for all five criteria.
- `/showrunner:style_report` in cargo-liner's showrunner prints the `cargo-liner-style` table; the parent's dailies show its style line.
- Any script fixed in this phase: its folder's tests green, `basedpyright` clean, merge gate `--tests-only` green.

### Phase 13 — Retire the fix pipeline and narrow the nightly rust mode  · status: todo
**Depends on:** Phase 12
**Blocked by:** pilot pass — every criterion in `docs/plans/build-followups-style-pilot.md` reads pass, and the build-followups showrunner gives the go.

#### Work Order

**Goal:** the fix pipeline and everything that exists only for it is gone, the kept commands work from `scripts/style/`, and the nightly `rust` mode stops proposing style sweeps.

**Spec:**
- First, before any deletion: `git grep -n` every consumer of each retired file and command below (the Delegation Context lists the ones known), and run each kept command once (`/focused_eval`, `/style_rename`, `/style_delete`, `/rust_style`, `/local_style`, `load-rust-style.sh`, `/mend_fix` and `/berth_fix` help paths, `/lint_config`); record the results in the phase report.
- Retired (design, section 1, with the verified paths):
  - `scripts/fix/` whole: fix.sh, fix-trigger.sh, fix-usage.sh, fix_report_parse.py, style-eval-all.sh, style-eval-heartbeat.sh, style-eval-review-all.sh, style-eval-review-prompt.md, style-fix-worktrees.sh, style-fix-manual.sh, style-fix-monitor.py, render-flow.py, fix-style-flow.{dot,svg}, project_add.py, project_rename.py, phase_skip.py, agent_assignments.sh, agent-assignments.conf, rg-shim.sh, setup.sh, report-render.md, README.md, com.natemccoy.style-fix.plist, `fix.conf`, `style_history.py`, `style_report.py`, `backpopulate_settings.py`, `docs/`, `tests/`.
  - Commands `commands/fix.md`, `commands/style_eval.md`, `commands/style_fix_review.md`, `commands/style_usage.md`.
  - `<OfferFixPipelineRedirect/>` (`commands/make_a_worktree.md:98-129` and its use :20) and `scripts/make_a_worktree/retarget_fix.py`; `<RevertFixPipelineRedirect/>` (`commands/worktree_delete.md:101-107`, use :31) and the `.fix-project` block in `scripts/worktree_delete/perform_deletion.sh:52-59`; fix enrollment in `scripts/new_rust_project/rust_generate.sh` (:153 block and :208 message).
  - `config/agents.conf` `[fix.codex]` :82, `[fix.claude]` :88 and their header comment :80, the `style_fix_review=` lines :70 and :76; `scripts/cli_agent/cli_agent.sh:20,38,42` (style_fix_review); `scripts/whoami/test_quota_alert.py:61-63` fixture keys; `scripts/agents/test_agent_exec.py:111-115` (drop the `scripts/fix/style-*.sh` case); `settings.json:155` (`style_usage`); `CLAUDE.md:4` (`/style_eval` mention: name `/focused_eval` or the style production instead); `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh:32` comment; `docs/as-built/agent-registry.md:318-322`; `pyrightconfig.json` `scripts/fix` environment.
  - `commands/lint_config.md`: drop "fix pipeline" from :2, the mend row :52, the clippy row :54 and the consumers line :60.
  - `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md`.
  - `/nightly_weights` (`commands/nightly_weights.md`), `scripts/nightly_review/weights.py`, `test_weights.py`, once the narrowed mode no longer reads them.
- Narrowed: `commands/nightly_review.md` `<Scope mode="rust">` (:19-24): its style-sweep proposals stop and the weight line (:20) goes; its other Rust repos, style-rule quality and showrunner/delegate process checks stay. `nightly_review.py` `MODES` (:42) unchanged unless it names weights.
- Kept: `/focused_eval`, `/style_rename`, `/style_delete`, `/rust_style`, `/local_style`, `load-rust-style.sh`, `/mend_fix`, `/berth_fix`, `/lint_config`; `/nightly_review` `config` and `messages` modes, `/nightly_next`, `/showrunner:nightly`, `nightly-review.nix`.
- nix (through the build-followups showrunner, natedev, which edits `/etc/nixos`; the user rebuilds): delete `modules/common/style-fix.nix` and `modules/linux/style-fix.nix` and their imports (`modules/common/default.nix:20`, `configuration.nix:64`); reword the comments that cite style-fix.nix (common/{claude-to-codex-sync:10, hanadocs-prioritize:59, session-notifier:10, codex-catalog-sync:9, checkouts:158, development:65}, linux/{usage-collect:4, buildlog:11}, darwin/nate-backend.nix:11,86); drop the `shell.nix:134` alias. Send the list to the showrunner as one message; the Mac's `com.natemccoy.style-fix.plist` launchd job is unloaded on the Mac (`ssh mac launchctl` — through the showrunner too).
- After: `git grep -n 'scripts/fix\|style_eval\|style_fix_review\|style_usage\|fix\.conf\|fix-project\|nightly_weights'` finds nothing outside `docs/as-built/` history lines and this plan.

**Files:**
- `scripts/fix/` — deleted
- `commands/fix.md`, `commands/style_eval.md`, `commands/style_fix_review.md`, `commands/style_usage.md`, `commands/nightly_weights.md` — deleted
- `scripts/nightly_review/weights.py`, `scripts/nightly_review/test_weights.py` — deleted
- `scripts/make_a_worktree/retarget_fix.py` — deleted
- `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md` — deleted
- `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh` — fix hooks removed
- `config/agents.conf`, `scripts/cli_agent/cli_agent.sh`, `scripts/whoami/test_quota_alert.py`, `scripts/agents/test_agent_exec.py`, `settings.json`, `CLAUDE.md`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `docs/as-built/agent-registry.md`, `pyrightconfig.json`, `commands/lint_config.md` — references removed
- `commands/nightly_review.md` — rust mode narrowed

**Workers:** 2 writers — the script and config deletions split from the commands and docs.
- `impl` — `scripts/fix/`, `scripts/nightly_review/weights.py`, `test_weights.py`, `scripts/make_a_worktree/retarget_fix.py`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh`, `config/agents.conf`, `scripts/cli_agent/cli_agent.sh`, `scripts/whoami/test_quota_alert.py`, `scripts/agents/test_agent_exec.py`, `settings.json`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`; hub: `pyrightconfig.json`
- `test` (opens as impl) — `commands/fix.md`, `commands/style_eval.md`, `commands/style_fix_review.md`, `commands/style_usage.md`, `commands/nightly_weights.md`, `commands/nightly_review.md`, `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `commands/lint_config.md`, `CLAUDE.md`, `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md`, `docs/as-built/agent-registry.md`; the nix list message

**Constraints from prior phases:** Phase 1 moved `candidate_generators.py`, `style_admin.py` and the `focused-eval` code to `scripts/style/` (`style_rules.py`) and repointed `/focused_eval`, `/style_rename`, `/style_delete`; `style_history.py` imports from `scripts/style` and has no other kept consumer. Phase 12's pilot report is in `docs/plans/build-followups-style-pilot.md`.

**Acceptance gate:**
- Merge gate `--tests-only` green (the `agents` and `whoami` suites included); `basedpyright scripts` `0 errors, 0 warnings`.
- The `git grep` in the Spec finds nothing outside history lines.
- Each kept command runs once with no missing-path error (recorded in the phase report).
- The nix list is sent to the showrunner (the rebuild is the user's).

### Phase 14 — Roll-out  · status: todo
**Depends on:** Phase 12, Phase 13
**Blocked by:** pilot pass (as Phase 13).

#### Work Order

**Goal:** cargo-liner's whole backlog is swept, then hana's main showrunner runs `/showrunner:add_style` and `hana-style` runs on its own from then on.

**Spec:**
- cargo-liner: the style showrunner launches all four units with no pilot scope (remove the `**Pilot scope:**` lines). The phase watches, through `/showrunner:style_report` and the parent's dailies, until every tier A-D rule reads done at the synced tip (held counts as done), and records the date and counts in `docs/plans/build-followups-style-pilot.md` under `## Roll-out`.
- hana: ask the build-followups showrunner to relay to hana's showrunner (production `tool-based-ui`, session `hana`, doc `/home/natepiano/rust/hana_catalyst/docs/hana/tool-based-ui-production.md`, branch point `init/catalyst`, Proposals `~/rust/hanadocs/issues`) to run `/showrunner:add_style`. It creates `style/init-catalyst`, the `hana-style` production and its four units. The phase is done when `hana-style`'s first hand-off reaches `init/catalyst` and its line shows in the tool-based-ui dailies. From then on it runs forever: each sync turns affected cells stale and the next tickets recheck them (about 35 hana slices and 8 cargo-liner slices of new code a week at today's rate).
- Anything the roll-out shows is broken is fixed in `scripts/style/` in this worktree, each fix its own commit named in the phase report.

**Files:**
- `docs/plans/build-followups-style-pilot.md` — `## Roll-out`
- `scripts/style/*` — fixes the roll-out shows, each named in the phase report

**Workers:** 1 writer + 1 tester — mostly operational; nothing splits until a fix appears.
- `impl` — the roll-out steps, the report section and any script fix
- `test` (opens as impl) — tests for any script fix, beside the module

**Constraints from prior phases:** Phase 12: `cargo-liner-style` runs under `remove-running-section` with three pilot units and `**Pilot scope:**` lines; pilot report in `docs/plans/build-followups-style-pilot.md`. Phase 13: the fix pipeline is gone; `scripts/style/` is the only style tooling.

**Acceptance gate:**
- `/showrunner:style_report` for `cargo-liner-style` shows every tier A-D rule done.
- `hana-style` is attached under `tool-based-ui` (`showrunners.py parent hana-style`), and one hand-off is merged on `init/catalyst` (`git log --grep 'hand-off'`).
- Any script fixed: its folder's tests green, `basedpyright` clean, merge gate `--tests-only` green.
