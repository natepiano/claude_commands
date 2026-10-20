# Style showrunner

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A standing `<repo>-style` child production that sweeps a repo against every style rule, records coverage in a ledger, hands its work back to the branch point, and replaces the fix pipeline.

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-16 15:26 EDT

Build the approved style showrunner proposal ~/.local/state/plan-backlog/2026-10-16-style-showrunner.md (revision 3, approved by the user 2026-10-16): Phases 1-3, then the Phase 4 pilot under cargo-liner's showrunner; Phase 5 retirement and Phase 6 roll-out only after the pilot meets its pass criteria.

Mapping (style-unit's choice, 2026-10-16): the proposal's Phases 1-3 are this plan's Phases 1-15, its Phase 4 pilot is Phases 16-17 (16 the `--pilot` code, 17 the live run; split 2026-10-17), its Phase 5 retirement is Phase 18 and its Phase 6 roll-out is Phase 19. The proposal file stays the design record; this plan does not edit it.

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree. This plan builds the style showrunner (design: `~/.local/state/plan-backlog/2026-10-16-style-showrunner.md`, revision 3).
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 in practice through `~/.claude/scripts/lib/py` (`typing.override` throughout the tests; natedev has 3.13); basedpyright 1.39; stdlib `sqlite3` (as in `scripts/buildlog/index.py`); markdown command and doc files; zsh/bash scripts; nix in `/etc/nixos`; Rust read only (the cargo-berth source at `~/rust/cargo-liner/crates/cargo-berth/src`). Style rules are markdown with YAML-like frontmatter in `~/rust/nate_style/rust/*.md` (a separate git repo on `main`; other sessions leave uncommitted files there).
- **Layout:**
  - `scripts/fix/` — the fix pipeline; retired in Phase 18 except the pieces Phase 1 moves.
  - `scripts/style/` — new home of the style scripts: `candidate_generators.py`, `style_admin.py`, `style_rules.py`, `rules.py`, `style_ledger.py`, `style_plan.py`, `style_footprint.py`, `style_sync.py`, `add_style.py`, `style_proposals.py`, tests beside them.
  - `scripts/production/` — merge_checkpoint, merge_gate, add_unit, add_showrunner, showrunners, production_lifecycle, dailies_render, dailies_input, proposal_issue.
  - `scripts/lib/` — shared helpers (`user_zone.py`, `py`); `berth_board.py` is new in Phase 8.
  - `commands/` — top-level style and fix commands, `showrunner/`, `unit/direct.md`, `nightly_*`.
  - `docs/` — `production_format.md`, `delegate_plan_format.md`, `delegate/phase_end.md`, `style_production.md` (new), as-built and plans.
  - `config/agents.conf`, `pyrightconfig.json`, `settings.json`, `CLAUDE.md`.
  - Outside the repo: `~/rust/nate_style/rust/` (rule frontmatter); `/etc/nixos` (`configuration.nix`, `modules/common`, `modules/linux`); `~/.local/state/style/<repo>/` (ledger, new).
- **Key files:**
  - `scripts/style/candidate_generators.py` — `CandidatesSpec` :37 (kind, origin, pattern, exclude_pattern, globs, paths, paths_exempt, structs, field_type_pattern); `read_candidates_spec` :78; `GENERATORS` (15 kinds); `enumerate_candidates` :1166, returns every site (no cap).
  - `scripts/fix/style_admin.py` — 260 lines; `from style_history import HISTORY_DIR, NATE_STYLE_DIR, HistoryRow` :12; `RUST_STYLE_DIR` :14. Used by `commands/style_rename.md:23`, `commands/style_delete.md:27`.
  - `scripts/fix/style_history.py` — 2089 lines; imports candidate_generators :25; `NATE_STYLE_DIR` :28, `HISTORY_DIR` :29, `HistoryRow` :129; `normalize_guideline_id` :152, `parse_frontmatter` :180, `list_style_files` :575, `_build_units_from_files` :590, `resolve_focus_targets` :628, `focused_units` :673; `focused-eval` subparser :1949, handler :2071; `enumerate_candidates` call sites :1228, :1305, :2057.
  - `scripts/fix/style_report.py`, `scripts/fix/backpopulate_settings.py` (called from `fix.sh:219`) — retired in Phase 18.
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
  - `config/agents.conf` — `[fix.codex]` :82, `[fix.claude]` :88 (header comment :81); `style_fix_review=` :70 and :76.
  - Other fix consumers: `scripts/agents/test_agent_exec.py:111-115` (reads `scripts/fix/style-*.sh`; in the `agents` merge-gate suite), `scripts/whoami/test_quota_alert.py:61-63`, `scripts/cli_agent/cli_agent.sh:20,38,42`, `settings.json:155` (`style_usage`), `CLAUDE.md:4` (`/style_eval`), `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh:32`, `docs/as-built/agent-registry.md:318-322`, `pyrightconfig.json` (the `scripts/fix` environment).
  - `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md` — retired in Phase 18.
  - `commands/nightly_review.md` — `<Scope mode="rust">` :19-24, weight line :20; `commands/nightly_weights.md`; `scripts/nightly_review/weights.py` (`COMMAND` :22) and `test_weights.py`; `scripts/nightly_review/nightly_review.py` `MODES` :42; `commands/nightly_next.md`, `commands/showrunner/nightly.md` (both use `candidates.py`; kept).
  - `/etc/nixos/modules/common/style-fix.nix`, `/etc/nixos/modules/linux/style-fix.nix`; imports `modules/common/default.nix:20` and `configuration.nix:64`; `nightly-review.nix` imported at `configuration.nix:60` (kept). Comments citing style-fix.nix: common/{claude-to-codex-sync:10, hanadocs-prioritize:59, session-notifier:10, codex-catalog-sync:9, checkouts:158, development:65}, linux/{usage-collect:4, buildlog:11}, darwin/nate-backend.nix:11,86; `modules/common/shell.nix:136-138` style-fix alias.
  - `scripts/accounts/role_map.py` — `Role` :26; workers map :302, :328.
  - `scripts/production/add_showrunner.py` — `account_for_launch` :108, `main` :158; takes `--production <child doc>` (:160), `--model`, `--effort`, `--check`, `--resume/--cwd/--account/--config-dir`; reads the doc through `add_unit.read_production`, starts `/showrunner:produce <doc>`, attaches the child under the caller.
  - `scripts/production/add_unit.py` — `production_field` :246, `read_production` :257 (a doc needs `- **Merge branch:**`, `**Showrunner checkout:**`, `**Log:**` (relative), a `## Units` line, and a name ending `-production.md`); `ensure_worktree` :625; berth target `branch.<b>.cargoBerthTarget` = merge branch at :638-642, only when `<checkout>/.claude/config/berth.toml` exists; `account_for_launch` :812.
  - `scripts/production/showrunners.py` — CLI `main` :500: `list` :516, `name|address <slug>` :507/:509, `parent|children|root <slug>` :517-519, `tree` :520, `attach <child> [<parent>]` :521, `detach <child>` :524; `attach` :419, `parent_of` :321, `current_address` :261, `stored_children` :464, `tell_others`, `release_edges`.
  - `scripts/production/production_lifecycle.py` — `retire_reservations` :308 (reads a top-level `reservations` list of dicts from `cargo-berth board --json`; the envelope's `reservations` is a list of ids, so it releases nothing); `refuse_running_children` :335, called in `wrap` (:376) at :383; `release_edges` :346, called at :442.
  - `scripts/production/merge_checkpoint.py` — 940 lines; `request_from` :278, refusal `unit {name} not in production doc` :285; CLI positionals `unit phase hash`, `--production`, `--started` (required), `--review-trial`/`--delivers`/`--parallel` (required for a code checkpoint) (`main` :910-929); `merge_branch_history` :333, regex :336 `^Merge (.+?) phase (\S+?)( shrink| as-built)? \(([0-9a-f]+)\)`; `merge_history` :349; `merge_subject` :539 `Merge <unit> phase <phase>[ shrink| as-built] (<hash7>) into <merge branch>`; `push_git` :677 (`git push` :687); `push_validate` :711 (`--quick` :714); `run` :856, push step :886-890, then `record_changes`, `into`, `promote`, `record`. Productions without a `**Push:**` line (cargo-liner, hana) push through validate_and_push `--quick`.
  - `scripts/production/merge_gate.py` — `TEST_SUITES` :40-45 (fixed list; the gate refuses a listed suite with no test modules); `run_suites` :279 globs `scripts/<suite>/test_*.py`; `merge_checkpoint` :459, subject :483 `Merge {unit} {phase} ({checkpoint}) into {merge_branch}`, `git push` :502; `--tests-only` :564.
  - `scripts/validate_and_push/validate_ci.sh:201` — `VALIDATE_QUICK=1` skips clippy, rustdoc and nextest; quick runs mend, rustfmt and taplo (:259).
  - `scripts/direct/verify.sh` — the lint/check/test front end (`lint <pkg>`).
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
  - A fix is applied without asking only when it is mechanical (Phase 9, `style_footprint.py`); every other change is held as a proposal and shown only when asked (design, section 7).

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

#### As-built

- A nate_style checkout without rule metadata (the Mac's until nate_style is pushed and pulled) loads, lists and enumerates without error; its rules read as unclassified, never as current.
- `CandidatesSpec.surface: Literal["whole", "partial", "unclassified"]`, default `"unclassified"`. `read_candidates_spec` reads a block with no `surface` key as unclassified; a present `surface` that is empty, invalid or `unclassified` raises `CandidatesSpecError`, as do a missing `kind` and `partial` without `uncovered`. Enumeration is the same for every surface.
- `Rule.tier: Tier | Literal["unclassified"]`. A global rule that declares none of `tier`, `family`, `input_scope` loads as unclassified with `family` None and `input_scope` `"crate"`, skipping tier, family and scope checks. Declaring any of those three keys opts a global rule into full validation; repo-local rules are always fully validated.
- A fully validated rule with a `candidates:` block must declare `surface` whole or partial, else `RuleValidationError`.
- `RuleListing` carries tier and surface with `unclassified`; `rules.py list` (text and `--json`) prints unclassified rules. `STYLE_HISTORY_NATE_STYLE_DIR` points the loader at another rule directory.

**Files:**
- `scripts/style/candidate_generators.py` — `surface` with `unclassified` default
- `scripts/style/rules.py` — unclassified global rules, opt-in full validation, surface check on classified rules
- `scripts/style/test_rules.py`, `scripts/style/test_candidate_generators.py` — legacy-frontmatter and validation tests

**Binds later work:** any code matching tiers A–E handles `"unclassified"`; the coverage ledger records no cell for an unclassified rule and never counts it current, and ticket selection and the style report treat it as not yet classified.

**Gotchas:** `surface`/`uncovered` alone, and legacy `mechanism`/`mode`, do not opt a global rule into full validation; only `tier`, `family` or `input_scope` do.

**Ruled out:** tolerating a surface-less `candidates:` block on a classified rule.

### Phase 4 — Coverage ledger: judgments, cells and the tier A lint record  · status: done

#### As-built

- `scripts/style/style_ledger.py` keeps one sqlite ledger per repo at `~/.local/state/style/<repo>/ledger.sqlite` (`<repo>` = basename of the main checkout, found through the git common dir, so every worktree shares it; `STYLE_LEDGER_STATE_DIR` replaces the root). A `schema_version` table drives numbered migrations (current: 2). Tables: `judgments`, `lint_runs`, `sync` (one row: `tip`, `style_branch`).
- **Rule identity and reach** (`rules.py`): `Rule.rule_id` is `<origin>:<path relative to its rule root>` (`global:rust/foo.md`, `local:docs/style/foo.md`, `local:docs/cargo-mend/style/foo.md`); every ledger row and CLI `--rule` uses it, never the stem. `Rule.reach: RepoReach | MemberReach(member, crate_dir)`: global and root `docs/style` rules reach the whole repo; `docs/<member>/style` reaches the workspace member whose directory name or `[package] name` equals `<member>` (the root member matches the checkout directory name or the root package name), else `RuleValidationError(path, "member-local rule names no workspace member <member>")`. `rules.workspace_members(manifests: dict[str, str]) -> tuple[WorkspaceMember, ...]` (`WorkspaceMember(directory, package_name)`, deepest first) resolves explicit members plus in-tree path dependencies of members, recursively, minus `exclude`; the rule loader and the ledger share it. `rules.py list` prints `rule_id` first (`--json` key `rule_id`).
- **Cell universe** at a commit, read from git objects only: tracked `.rs` files, minus any path under a `vendor/` dir and the ignore/exclude lists in `.claude/config/berth.toml`, `rustfmt.toml`, `.rustfmt.toml`. A file's crate is the deepest member directory holding it, else `.`. Cells per classified rule: tier A and C one per (rule, file); tier D one per (rule, crate); tier B and E none (B cells land with listed sites, and B never reads done until then). A `MemberReach` rule's cells are only its member's. Unclassified rules have no cells.
- **Fingerprints:** `file` — the blob; `crate` — the member directory's tree hash, or for crate `.` sha256 over the (path, blob) pairs of its own files only; `manifest` — sha256 over the (path, blob) of the root `Cargo.toml` plus each member's.
- **Cell state** from the newest judgment rows (`ORDER BY recorded_at DESC, id DESC`) whose `rule_blob` equals the rule's current blob: `current` when the row's commit is an ancestor of the synced tip and its fingerprint matches there; `awaiting merge` when the commit is not an ancestor of the synced tip but is an ancestor of `sync.style_branch`'s current tip and the fingerprint matches at that tip; otherwise `stale` if any row exists, `open` if none. No stored style branch → unmerged rows read stale. Every verdict (`clean | fixed | exception | held | waived`) is current while its inputs match, so an exception expires with them; `status` counts held separately.
- **Tier A:** `lint_runs(tip, tree, tool, scope='workspace', config_hash, passed, diagnostics_json, rule_blobs_json)`, unique on (tree, tool, scope, config_hash). The tool is `style_rules.parse_frontmatter(rule.path)["mechanism"][0]`. A cell is current only with a run for its tool at the synced tip's tree and current config hash, `passed` true, zero diagnostics for the file, and the rule's blob as recorded in that run; open when the tool has no run at all, else stale. The config hash covers the tool's config files, each `Cargo.toml`'s `[workspace.lints]`, and for mend `[package.metadata.mend]` and `~/.config/cargo-mend/config.toml`.
- **Done:** a classified rule (not tier B) whose cells are all current; an unclassified rule never is.
- CLI (errors exit 2 with `style_ledger: <message>`; writes upsert on their natural key):
  - `record --repo R --rule ID --path P --commit C --rule-blob B --verdict V [--detail D] --ticket T --worker W` — computes scope and fingerprint at `C`; refuses an unknown id, an unclassified rule, tier A/B/E, a path outside reach, a non-universe file, or (tier D) a path that is not a crate with Rust files. Unique key: (rule, path, commit, rule_blob, input_scope, input_fingerprint, ticket, worker).
  - `lint-record --repo R --tip C --tool clippy|mend|rustfmt --passed 0|1 --diagnostics FILE.json` (object of path → nonnegative count).
  - `set-tip --repo R --tip C [--style-branch NAME]` — stores the tip and `refs/heads/NAME`; omitting the flag keeps the stored branch.
  - `status --repo R [--rule ID] [--crate K] [--json]` — `unclassified: <n> rules — pull nate_style` when n > 0, then `rule current stale open awaiting merge held done`; `--json` gives `cells` and `unclassified_rules`.
- Library: `open_ledger(repo) -> Ledger` (context manager), `Ledger.cells(rule=None, crate=None) -> Iterator[Cell]`, `Cell(rule, path, crate, tier, family, state)`, `Ledger.rules() -> Iterator[RuleProgress]` with `RuleProgress(rule_id, tier, family, cells_current, cells_total, done)` (classified only), `Ledger.unclassified_rules() -> tuple[str, ...]`, plus `set_tip`, `record`, `lint_record` matching the CLI.

**Files:**
- `scripts/style/style_ledger.py` — the ledger, cell states, tier A lint record and CLI
- `scripts/style/rules.py` — `Rule.rule_id`, `Rule.reach` (`RepoReach`, `MemberReach`), `WorkspaceMember`, `workspace_members`, the id column in `list`
- `scripts/style/test_style_ledger.py` — temp two-crate git repo with a temp `STYLE_HISTORY_NATE_STYLE_DIR`: cell states, fingerprints per scope, rule-blob staling, awaiting merge, migration, tier A runs, legacy and mixed catalogs, same-stem rules
- `scripts/style/test_rules.py` — rule id and reach tests

**Binds later work:** every `set-tip` caller that expects awaiting-merge cells (bootstrap, sync, `reconcile`) passes `--style-branch` with the style merge branch, stored in `sync.style_branch`. Listed-site cells reuse the `_tip` cell universe, crate `.` mapping and `workspace_members`; `#uncovered` ids and non-`.rs` cells are not yet accepted by `record`. Ticket selection skips `awaiting merge` cells; the style report shows an Awaiting merge column. The unclassified count is user-actionable (pull nate_style) and reaches the sync line and the report.

**Gotchas:** `Ledger._tip` resolves a moving ref such as `HEAD` to a commit before caching; a re-recorded judgment keeps its row id, so newest-first ordering uses `recorded_at`. The real cargo-liner `status` check waits for the pilot bootstrap that tags its local rules.

**Ruled out:** inferring the style branch from ref names; per-site progress inside a listed file cell — tickets split at file boundaries instead.

### Phase 5 — Listed-site coverage: listed sites, partial components and non-.rs cells  · status: done
**Depends on:** Phase 4

#### As-built

- Migration 3 (schema version 3) adds `listed_rules(rule, rule_blob, tip)`, one marker row per enumerated rule, so "refreshed, no sites" differs from "never enumerated", and `listed_sites(rule, rule_blob, tip, path, line, text)`, whose foreign key to `listed_rules` cascades on delete.
- **Listed sites** (`Ledger._refresh_sites`): the listed rules are classified tier B or D rules with `candidates:` and tier B rules with `pre_filter:`. `_export_blobs` writes the tip's committed blobs (the `ls-tree` blob map of `_tip`, read through `git cat-file --batch`) into a temp dir, so the working tree and export attributes never matter and submodules are skipped. Generator rules take the sites of `candidate_generators.enumerate_candidates(rule.candidates, <export>)`, minus paths not tracked at the tip, vendor or berth/rustfmt-ignored paths, and sites outside a `MemberReach`. A rule with only `pre_filter:` (Python `re`) lists every matching line of the `.rs` cell universe plus every non-`.rs` file a generator site names. A bad generator or regex raises `LedgerError("<rule id>: …")`.
- **Refresh by rule blob:** `set_tip`, `Ledger.listed_sites`, `record` (tier B, and tier D with `candidates:`) and every coverage read (`cells`, `rules`, `status`) first compare the `listed_rules` markers at the synced tip with the current (rule id, `Rule.blob`) set; any difference re-enumerates every listed rule at that tip in one transaction. A changed generator, a newly classified rule or a removed rule therefore refreshes at the same tip without a new `set-tip`.
- **Components and cells:** a tier B rule's **listed** component has one file cell per file holding a listed site (`.rs` or not); zero sites means an empty, complete component. A tier B rule whose `candidates.surface` is `partial` adds the **uncovered** component: pseudo-rule `<rule id>#uncovered`, built with `dataclasses.replace(rule, rule_id=…, tier="C")`, one cell per `.rs` file in reach, in the parent's family. A tier D rule has no `#uncovered`: its crate cells cover both components and also include crates that hold only listed sites. A listed cell on a non-`.rs` file fingerprints at `file` scope whatever the rule's `input_scope`.
- **Done:** `RuleProgress` counts `#uncovered` cells under the parent rule, and done means every cell is current, so a partial rule is done only when both components are; pseudo-rules never appear in `Ledger.rules()`, and tier B is no longer excluded from done.
- `record --rule ID`: `ID` is a rule id or `<rule id>#uncovered` (accepted only for a partial tier B rule with `candidates:`; otherwise `unknown rule`). Tier B requires the path to hold a listed site of that rule at the synced tip (`holds no listed site at the synced tip`) and to exist at `--commit`; tier D accepts a crate with included files. Tier A and E are still refused. `status --rule <id>#uncovered` filters to that component; the `status` table counts both components on the parent row.
- Library: `Ledger.listed_sites(rule, crate=None) -> tuple[ListedSite, ...]`, `ListedSite(rule, path, crate, line, text)` (frozen), sorted by path, line, text; an unknown id raises `LedgerError`.

**Files:**
- `scripts/style/style_ledger.py` — migration 3, `_export_blobs`, `_refresh_sites`, `listed_sites`, `_component_rule`, listed and uncovered components, non-`.rs` cells, tier B and `#uncovered` in `record`
- `scripts/style/test_style_ledger.py` — zero-site partial rule, both-component done, tier D without `#uncovered`, forbidden-word and shell-script sites, export from synced objects (untracked, vendor, ignored paths, `export-ignore`/`export-subst`), member reach, same-tip refresh, zero-site markers, migration from version 2

**Binds later work:** ticket selection and the style report read sites only through `Ledger.listed_sites`, which always returns the refreshed set. Listed file cells hold one judgment per (rule, file) with no per-site progress, so a listed ticket packs whole files. Cells for `<rule id>#uncovered` carry tier `C` and that rule id; consumers group them under the parent.

**Gotchas:** never read a committed tree through `git archive`: `export-ignore` drops files and `export-subst` rewrites them. A pre_filter-only rule sees a non-`.rs` file only when some generator rule names it.

**Ruled out:** `git archive | tar -x` for the tip export (attribute-sensitive); per-site judgment progress inside a listed file cell.

### Phase 6 — Ledger work state: tickets, hand-offs, processed tips, reconcile  · status: done

#### As-built

- Migration 4 (schema version 4) adds `sync.synced_at` (UTC ISO, written by every `Ledger.set_tip`, a repeat at the same tip included; read by `Ledger.synced_at() -> datetime`), and the tables `tickets`, `handoffs`, `processed_tips`, `held_findings` and the one-row `reconciliation(id=1, branch_point)`.
- `tickets`: `id` (`<unit>-<n>`), `unit`, `kind` (`read` | `listed` | `structure`), `family`, `rule` (listed only, canonical id), `crate`, `files`, `sites`, `rules` (JSON, `#uncovered` pseudo-rules included), `rule_blobs` (rule id → blob pinned at `ticket add`; a pseudo-rule pins its parent's blob), `state` (`queued` → `assigned` → `checkpointed` → `merged`, any non-final → `dropped`), `branch` (`refs/heads/<B>`), `checkpoint`, timestamps. A unique index allows one `assigned`-or-`checkpointed` ticket per unit. `record --ticket T` and `held add --ticket T` on a ledger ticket take the blob from the pin; a differing `--rule-blob` exits 2, so a judgment on a ticket whose rule changed after `ticket add` stays stale against the new text.
- `handoffs`: `checkpoint` (primary key), `state` (`pending` → `sent` → `merged`), `merge_commit`, `sent_to`, timestamps. `held_findings`: `finding_id` (first 16 hex of sha256 over rule, path, line, text, rule_blob), `rule`, `path`, `line`, `text`, `kind` (`rename` | `type` | `restructure`), `before`, `after`, `rule_blob`, `ticket`, `state` (`unfiled` | `filed` | `waived`), `proposal`, timestamps; one row per finding, so two in one cell both persist.
- Library: `HeldFinding(finding_id, rule, path, line, text, kind, before, after, rule_blob, ticket, state)` with `state: Unfiled | Filed(proposal: Path) | Waived`; `Ledger.hold(...) -> HeldFinding`, `held_findings(state=None)`, `file_held(finding_id, proposal)`, `waive_held(finding_id)`; `Ledger.summary() -> StyleLedgerSummary` with `waiting: PendingHandoffs(count, oldest_pending_since) | NoPendingHandoffs` (all frozen dataclasses), measured from `handoff add`.
- Transitions are idempotent: a repeat of the current transition exits 0 (a repeat with a different branch, commit, proposal path or parent exits 2); a backward or skipped one exits 2 naming the current state. `hold` of an existing finding returns it; `unfiled` → `filed` → `waived` and `unfiled` → `waived` are allowed. `ticket add` and `hold` refuse an unknown or unclassified rule id (exit 2).
- CLI (`style_ledger.py`, all with `--repo R`): `ticket add|assign --branch B|checkpoint --commit C|merge|drop --id ID`, `ticket open --unit U` (prints the unit's assigned or checkpointed ticket as JSON, or nothing); `handoff add|send --to SLUG|merged [--merge-commit M] --checkpoint C`, `handoff pending` (pending and sent as JSON, or `no pending hand-offs`); `tip seen --tip C` (prints `new` or `seen`, exit 0), `tip done --tip C`; `held add ...` (prints the finding id), `held file --id F --proposal PATH`, `held waive --id F`, `held list [--state S] [--json]`; `reconcile`; `summary --json`.
- `reconcile --branch-point B --style-branch S`: stores `refs/heads/S` through `set_tip(<stored synced tip, else B's tip>, S)` only when it differs, never moving the synced tip; stores B in `reconciliation`; marks a non-merged hand-off `merged` when B contains its checkpoint (`merge_commit` = first first-parent merge on B containing it, else B's tip for a fast-forward); merges a `checkpointed` ticket when S contains its checkpoint; drops an `assigned` ticket whose `branch` ref is gone; prints one line per change.
- `handoff merged` refuses `pending`, refuses before any `reconcile` has stored B, and requires B to contain the checkpoint; a given `--merge-commit` must contain the checkpoint and sit on B.
- `summary --json` keys: `rules_done`, `rules_total` (classified tier A-D only; tier E, unclassified and `#uncovered` never count), `unclassified_rules`, `cells_current_pct`, `stale`, `proposals` (distinct `proposal` paths over filed held findings), `waiting_to_merge_back` (pending and sent), `oldest_waiting` (UTC ISO, null with `NoPendingHandoffs`), `synced_at`, `synced_tip`. An all-legacy catalog reads 0 of 0 with `unclassified_rules` n.

**Files:**
- `scripts/style/style_ledger.py` — migration 4, `HeldFinding` and summary types, ticket/hand-off/tip/held CLI, `reconcile`, `summary`
- `scripts/style/test_style_ledger.py` — transitions, repeats and rejects, pin staleness, reconcile against a temp repo, summary keys and catalogs, migration 3 → 4

**Binds later work:** The style plan's next-phase step passes `ticket assign --branch` from the unit's Units row. The style showrunner's startup runs `reconcile` before any hand-off acknowledgment calls `handoff merged`. Dailies read the `summary --json` keys above.

**Gotchas:**
- Unit names (`<name>-unit`) and branches (`<production>-<name>`) differ by construction in `add_unit.py`; map a unit to its branch through the Units row, never by derivation.
- A `checkpointed` ticket blocks the unit's next `assign` until `ticket merge`.
- `ticket merge` checks no ancestry; only `reconcile` merges a ticket by containment.
- `handoff send` to a second parent exits 2.

**Ruled out:**
- `git hash-object -w` for pinned rule text: `git gc` may prune loose objects; pinned text goes in a ledger `rule_texts` table.
- Relaxing `handoff send` to accept a second parent; rerouting is a separate verb.

### Phase 7 — Style production bootstrap: instruction doc, format fields and /showrunner:add_style  · status: done

#### As-built

- `docs/production_format.md` carries `**Kind:** style` (absent means a product production), `**Branch point:**` and `**Parent production:**` (style only); `**Proposals:**` is required for a style production. `docs/delegate_plan_format.md` carries the optional header `> **Standing:** style_plan.py`: a standing plan never closes its run and is git-excluded, so checkpoint and shrink never commit it. `commands/showrunner/produce.md` reads `docs/style_production.md` for a `**Kind:** style` doc and follows it where the two differ.
- `docs/style_production.md` is the instruction doc (rules stay in `~/rust/nate_style`), with sections Tiers, Families, Ticket queue, Ledger, Sync, Merging back, Yielding, Big changes, Report, Lifetime; later work appends command lines to them.
- `add_style.py --production <parent doc> --model <m> --effort <e> [--check]`, run by a repo's main showrunner. `_request` derives everything from the parent doc: the main checkout through `git rev-parse --git-common-dir` on `**Repository:**`, slug `<repo>-style`, branch point = parent merge branch, style merge branch `style/<branch point with / as ->`, checkout `<repo parent>/<repo>-style`, doc `docs/<repo>-style-production.md`. Missing `**Proposals:**` refuses naming the field.
- Existing production = doc present AND slug in `showrunners.timer_slugs()`; then `_attach` detaches from any old parent, attaches under the caller, rewrites `**Parent production:**` and `**Proposals:**`, and prints `json.dumps` of each `ledger.pending_handoffs()` entry. A doc on another branch point raises `StyleBranchPointTaken` (exit 2) and writes, creates and attaches nothing. A doc with no timer is an interrupted bootstrap and reruns the create path.
- Create path (every step idempotent): branch from the branch-point tip and worktree; production doc with Units table `Unit | Plan | Worktree | Branch | Port | Owns` (four units `names-unit`, `values-unit`, `types-unit`, `modules-unit`; Worktree `<repo parent>/<slug>-<family>`, Branch `<slug>-<family>`, Port `—`, Owns `none — takes what the board leaves free`); four standing plans `docs/style-production/units/<family>-unit.md` whose Delegation Context holds `**Family:**` with a bootstrap snapshot of rule ids and tiers, the live `rules.py list --repo <checkout>` line, and an empty `## Phases`; `.git/info/exclude` entries for `/docs/style-production/`, the doc and the log; in-process `style_ledger.open_ledger(checkout)` then `set_tip(<stored synced tip if a sync exists, else branch-point tip>, style_branch)`; then `add_showrunner.py`. Launcher exit 1 or 3 prints `_open_session_guidance` and returns that code; a live herdr session named `<slug>` with no timer is refused (exit 2) before any write.
- `--check` prints seven steps without acting; on an existing production steps 3-6 read `skipped: <slug> already exists` and step 7 `update Parent production and Proposals, print pending hand-offs; synced tip unchanged`.
- `add_unit._is_excluded_standing_plan(production, plan)` accepts a plan absent on the merge branch when the showrunner checkout's copy is git-ignored and carries `> **Production:` and `> **Standing:**`; `ensure_worktree` copies it into the unit worktree only when the unit's copy is missing.

**Files:**
- `docs/production_format.md` — `Kind`, `Branch point`, `Parent production`; Proposals required for style
- `docs/delegate_plan_format.md` — `Standing` header
- `commands/showrunner/produce.md` — the style conditional read
- `docs/style_production.md` — the style instruction doc
- `scripts/style/add_style.py` — the bootstrap and attach
- `scripts/style/test_add_style.py` — bootstrap, rerun, attach, refusal, `--check`, retry and real-preflight tests
- `commands/showrunner/add_style.md` — the command, with `--model` and `--effort`
- `scripts/production/add_unit.py` — excluded standing plan acceptance and copy-on-missing
- `scripts/production/test_add_unit.py` — standing plan launch tests
- `pyrightconfig.json` — `scripts/production` in the `scripts/style` env's extraPaths

**Binds later work:** a unit's `--plan` is its own worktree copy, so `<plan>.parent.parent / "rules"` is the unit worktree's git-ignored `docs/style-production/rules/`; ticket phases go under the empty `## Phases`. No shared style-production lookup function exists; one resolving from a repo dir must go through `--git-common-dir` as `_request` does. The `_attach` hand-off print, `--check` step 7 text, and tests `test_existing_production_reattaches_updates_destination_and_prints_every_handoff` and `test_check_existing_production_describes_attachment_and_preserves_all_state` pin the pending hand-off output. The berth target block sits in `ensure_worktree`.

**Gotchas:**
- `git add` on any path under the shared exclude exits 1 without `-f`; stage from `git status --porcelain`, never naming excluded paths.
- `add_unit` copies a standing plan only when the unit's copy is missing; showrunner-copy edits after a unit's first launch never reach it.
- The interrupted-bootstrap create path never rewrites `**Parent production:**` or `**Proposals:**` (`_write_missing` skips an existing doc), so a second parent on the same branch point in that window leaves the first parent's fields.
- Owns `none — takes what the board leaves free` parses as `('none',)`; merges need `--also`.
- Bootstrap tests stub `add_showrunner`; only `test_bootstrapped_standing_plan_passes_real_unit_preflight` crosses into `add_unit.preflight`.
- Standing plans never go under `docs/style/` or `docs/*/style/`: `rules.load_rules` reads every `.md` there as a local rule.

**Ruled out:**
- Empty checkpoint commits for no-change tickets — they carry empty merges into the branch point's history.
- Picking the pilot types crate from `style_plan.py queue` before bootstrap — its busy-file lookup needs `**Parent production:**`.
- A manual launch hold for the pilot — `produce.md` launches every Units row, so only rows absent at launch stay unlaunched.

### Phase 8 — Ticket selection: the berth board reader and style_plan.py  · status: done

#### As-built

- `berth_board.read_board(repo: Path) -> Board | BoardUnavailable` runs `cargo-berth board --json` in `repo` and parses the envelope before anything else. Status `unconfigured` with payload kind `no_facts` is an empty `Board`, as is a valid envelope holding no reservation; status `ledger_unreadable`, empty stdout, non-JSON, a non-`board` envelope, missing fields, conflicting snapshots of one `reservation_id`, or a reference to an id with no snapshot is `BoardUnavailable(reason)` (frozen). The result is per call, never persisted.
- `Board(entries).reservations() -> tuple[Reservation, ...]` collects `targets[].reservations` and the `ready_now`, `waiting`, `unresolved_overlaps`, `unconstrained_reservations` and `resolved` sections, deduplicated by `reservation_id`. `Reservation` (frozen): `reservation_id, target, holder_worktree_root, holder_branch, scopes, lifecycle, edit_blocking_status: "blocking" | "clear"`; `blocks(path)` is true only when blocking and a scope matches (`file:<path>` or a bare path exactly, `tree:<root>` by prefix, `tree:.` everything).
- `style_plan.py next-phase --unit U --plan P --repo R`: `next_phase` returns `PhaseWritten | Resumed | RepairFirst | WaitingForMerge(ticket_id, checkpoint) | WaitingForSync(unclassified) | BoardUnavailable`, which `main` prints: `phase written: <id>` 0, `resume <id>` 0, `repair first` 0, `waiting for sync` 3 (plus stderr `unclassified: <n> rules — pull nate_style` when n > 0), `board unavailable: <reason>` 4, `waiting for merge: <id> checkpointed at <commit7>` 5, and `style_plan: <error>` on stderr 2 for `OSError`, `ValueError` (`LedgerError` included), `Refusal` or `sqlite3.Error`.
- `next_phase` order: `style_document(repo)` (main checkout through `--git-common-dir`, then `<main parent>/<main>-style/docs/<main>-style-production.md`) and the unit's Units row; `--plan` must equal `<row Worktree>/docs/style-production/units/<unit>.md`, else exit 2. A `### Phase` heading holding `repair` (any case) and `status: todo` returns `RepairFirst`, writing nothing. Every later path first discards each ticket phase whose ledger state is `merged` or `dropped` (`discarded <id>: <state>`). An open ticket: `checkpointed` → `WaitingForMerge`; `assigned` → `write_phase`, `Resumed`; neither reads the board. Then the board (unavailable → 4, no ticket recorded). Queued tickets oldest first: the oldest is assigned under its own id when no file is busy and every other queued ticket is dropped (`dropped <id>: files busy`); a busy oldest is dropped with the rest and selection runs fresh. A new ticket takes the first candidate, `next_ticket_id`, then `ticket_add`, `ticket_assign(id, <Units Branch column>)`, `write_phase`. No candidate → `WaitingForSync(len(unclassified_rules()))`.
- `busy_files(...) -> BusyFiles(board, paths)`; `contains(path)` is membership or any blocking reservation. Paths come from every non-retired Units row of the parent production (`**Parent production:**`) and of the style production: `git diff --name-only --no-renames <merge branch>...<branch>` when the branch exists (a retained row with a branch counts as live), every `git status --porcelain` path in its worktree (both sides of a rename, untracked included), and other style units' open-ticket files.
- `candidate_tickets(repo, ledger, busy, family)` uses only `open`/`stale` cells, crates by most stale cells then name. A crate with a pending tier-D cell yields one `modules` `structure` ticket (files `Ledger.rust_files(crate)` plus the D rules' listed-site files, minus busy; those rules' sites in the kept files) and no other family's ticket. Otherwise: `listed` per tier-B rule, whole files packed in path order up to 150 sites (a file over 150 alone, a busy file's sites dropped); `read` per tier-C rule set (`#uncovered` included) through `read_slices`: files grouped by directory, a directory over 25 files or 5,000 lines split per file, then `balanced_slices` cuts contiguous groups (bound 25 files / 5,000 lines) minimizing slices under 10 files, then slice count, then the sum of squared file counts. Line counts come from `git show <tip>:<path>`, so slicing is deterministic per tip.
- `phase_text` builds `### Phase <n> — <unit>-<n>  · status: todo`, `#### Work Order`, the plan's Delegation Context, Goal, Spec (each rule id with its absolute `<plan>.parent.parent/rules/<blob>.md` and blob; sites as sorted JSON; `uncovered:` lines from the pinned texts for structure tickets and `#uncovered` rules; held, mechanical, yield and `record --ticket` instructions), Files, Workers (`2 writers` over 12 files, else `1 writer + 1 tester`), and an Acceptance gate of `verify.sh check|test|lint <package>`, `style_footprint.py check --repo . --base <literal base commit> --head HEAD`, and every cell recorded. Its inputs are the ticket record, pinned texts and Delegation Context only, so a rebuild is byte-identical.
- `write_phase` reads `pinned_rule_texts` and `ticket_base`, rewrites each pin file that is missing or whose `git hash-object` differs from its blob, then replaces the id's existing phase in place (written only on change) or appends under `## Phases`. Every plan and pin write is `atomic_write` (temp file in the same directory, fsync, `os.replace`).
- `queue --repo R [--unit U]` reads the board first (unavailable → 4), then prints `<unit>: <kind> <crate> <rule or rules> — <n> files: <files>` per family unit; read only, exit 0.
- Ledger schema 5: `rule_texts(blob TEXT PRIMARY KEY, text BLOB NOT NULL)` and `tickets.base TEXT NOT NULL DEFAULT ''`; `TicketRecord` gains `base`. `ticket_add(id, unit, kind, family, crate, files, rules, rule="", sites=None)` names its insert columns, takes `sites` on `listed` and `structure` (never `read`), and in its transaction stores each rule file's bytes (insert-or-ignore by blob) once `git hash-object --stdin` equals the pin, else `LedgerError("<rule id>: rule changed while pinning; rerun")`. `pinned_rule_texts(ticket_id) -> tuple[PinnedRuleText(rule_id, blob, text: bytes), ...]`, one per rule id, else `LedgerError("<ticket>: no pinned text for <blob>")`. `queued_tickets(unit)`: queued only, by `created_at, id`. `next_ticket_id(unit)`: `<unit>-<highest n + 1>` across every state. `ticket_base(id)`: the stored base, else (state `assigned` only) `git rev-parse --verify <branch>^{commit}` stored once. `rust_files(crate)`: the synced tip's `.rs` files in that crate. No CLI verb added.

**Files:**
- `scripts/lib/berth_board.py`, `scripts/lib/test_berth_board.py` — board reader and its envelope fixtures
- `scripts/style/style_plan.py`, `scripts/style/test_style_plan.py` — busy files, slicing, candidates, `next-phase`, `queue`
- `scripts/style/style_ledger.py`, `scripts/style/test_style_ledger.py` — migration 5, pinned texts, queued and next ids, `ticket_base`, `rust_files`, structure sites
- `scripts/style/add_style.py`, `scripts/style/test_add_style.py` — `production_field` imported from `production_units`
- `docs/style_production.md` — Ticket queue command lines, exit codes, pins, queued recovery, discard, atomic writes, `base`

**Binds later work:** the mechanical-fix filter's `style_footprint.py check` receives a literal base commit, and a mid-ticket merge of the synced style branch puts foreign work inside base..HEAD, so `check` folds merged-in parents out of it. Standing-plan refill consumes the printed lines and exit codes as shipped; `style_plan.style_document` is a third copy of the `--git-common-dir` derivation and joins the shared style-production lookup. Discard keys on ledger state `merged`, set by the style showrunner's `ticket merge`; the next migration is 6. Proposal batching reads the `read_board` states as shipped, and `production_lifecycle.retire_reservations` treats an unconfigured repo as an empty board, not a skip. The pilot gets exit 4 only for an unavailable board; `queue` needs the style doc, so it runs after bootstrap; `**Pilot scope:**` does not exist.

**Gotchas:**
- cargo-berth exits 4 for an unconfigured repo and for an unreadable ledger; only the envelope status tells them apart.
- `ticket_base` refuses every state but `assigned` until a base is stored; once stored it returns it in any state, after a branch delete included.
- `subprocess.CalledProcessError` from `ticket_add`'s `git hash-object` is not mapped to exit 2.
- `board unavailable` records no ticket, but the finished-phase discard has already rewritten the plan.
- `RepairFirst` returns before the discard, so finished phases stay in the plan until the repair phase clears.

**Ruled out:**
- Private `refs/style-tickets/<ticket>/base` refs — they outlived terminal tickets and kept dropped tickets' commits reachable.
- Pinned texts as `git hash-object -w` objects — `git gc` may prune loose objects.
- `cargo-berth check` during selection — it acquires reservations.
- A test reading the footprint CLI from plan prose — a shrink rewrites that prose.

### Phase 9 — Mechanical-fix filter: style_footprint.py  · status: done

#### As-built

- `style_footprint.py check --repo R --base B --head H [--finding-map FILE.json]` prints one line per finding: `mechanical` or `review: <reason>; <reason>`, prefixed `<finding id>: ` only under `--finding-map`. Exit 0 only when every finding is mechanical, else 1. A map error prints `review: <error>`, exit 1; a git or file error prints `error: <error>`, exit 2.
- Checks, each reason tagged `check N:`: **1** no file added, deleted, renamed or moved, no `mod` line change; **2** no externally reachable item changes shape (glob re-exports `pub use x::*` count as public reach); **3** no type gains or loses a field, variant, generic or trait impl, `struct`/`enum`/`union` alike, derive changes included; **4** a rename stays in one crate and at most 10 files; **5** under 200 changed lines in each file; **6** flags conditions, literals and control flow, any changed line inside a macro invocation's token tree (spacing included), serde/reflect/`rename =` attributes and documented names (multi-line and block doc comments included), non-Rust and binary or metadata-only changes, and any token change not proven equivalent.
- The one proven equivalence is a rename whose definition and every use change together in the finding's own hunks, inside check 4's bounds; `cache.clear()` → `cache.flush()` reads `review`.
- Base fold: for each first-parent merge in `B..H`, oldest first, non-first parents fold into the base via `git merge-tree --write-tree` plus an unreferenced `git commit-tree` commit (the next fold needs ancestry); the judged diff is `git diff -M <folded base> H`. A conflicting fold prints `review: merge in range`, exit 1. A `--base` given as a literal commit id works after its branch is deleted.
- `--finding-map` maps each id to a file list or `{files, hunks: [{path, old_start, new_start}]}`; every hunk needs exactly one owner, and an unowned or doubly owned hunk is an error. Without the map the whole diff is one finding.

**Files:**
- `scripts/style/style_footprint.py` — the `check` CLI, lexical Rust tokenizer, base fold, six checks
- `scripts/style/test_style_footprint.py` — temp-repo cases per check
- `docs/style_production.md` — **Big changes** carries the command line and exit codes

**Binds later work:** the ticket gate line from `style_plan.py` `phase_text` calls this CLI with no `--finding-map`; approved-proposal tickets replace that gate line; the pilot treats a `review` verdict a reviewer later clears as expected, not a failure.

**Gotchas:**
- The filter is lexical and fails closed: a private rename in a file whose test module has `use super::*`, an unrelated edit on a macro invocation's line, and an attribute on an unrecognized item all go to review.
- It reads committed revisions only, so a gate on `--head HEAD` before the code commit judges an empty diff.

**Ruled out:** a separate persisted state for checker errors (they share the review route by design).

### Phase 10 — Standing-plan refill in /unit:direct and the sync trigger  · status: done

#### As-built

- `commands/unit/direct.md` `<StandingPlanRefill>` runs for a plan headed `**Standing:** style_plan.py` at startup, after compaction or restart, and at every `<NextPhase/>` boundary. It first resumes `<CheckpointCommit/>` step 7 for any `phases/*/delegated_phase_reservation_state.json` in `checkpoint_committed_awaiting_release_confirmation` (an unsuccessful recovery stops refill), then runs `style_plan.py next-phase --unit <unit> --plan <unit-worktree>/docs/style-production/units/<unit>.md --repo <unit-worktree>`. Exit 0 continues (a todo repair first); 3, 4, 5 set `waiting for sync` / `waiting for board` / `waiting for merge: <id>`, report once and end the turn with `— blocked: waiting on the showrunner: sync` / `: board` / `: ticket <id> merge`; a sync message, any showrunner message (board), or `ticket <id> merged; refill.` reruns refill. A standing plan never closes: no FinalGate, RunAsBuilt, final PeriodicCI, CICleanup or RunSummary, and no shrink (`run_phase_review.md`, `phase_end.md`).
- Workers judge only the pinned `docs/style-production/rules/<blob>.md` files the ticket names; `record --ticket` keeps the pinned blob after a live rule edit, leaving the new rule's cell stale until a later ticket.
- Ticket gate: `style_footprint.py check --repo . --base <tickets.base> --worktree`, rerun at checkpoint step 6 before staging; exit 1 keeps the reservation and returns to repair; exit 2 (the gate could not run) keeps the reservation and reruns the gate. Exit 0 stages only the phase's code paths from `git status --porcelain` (renames as pairs), commits once, then `style_ledger.py ticket checkpoint --id <ticket> --commit <code commit>` before the phase flips done. The excluded plan, `docs/style-production/` and pinned rules never appear in any `git add`; step 2 expects no plan doc and skips the done-phase-with-Work-Order refusal.
- No-change ticket (every cell `clean`, `exception` or `held`, no tracked diff): no commit; captured `HEAD` must pass `git merge-base --is-ancestor HEAD refs/heads/<style merge branch>` (else one line to the style showrunner, ticket stays assigned), then `ticket checkpoint --commit <HEAD>`, `ticket merge`, the `CheckpointCommittedAwaitingReleaseConfirmation` record carrying that `HEAD`, step 7 release unchanged, no push or merge request, `From <unit>: ticket <id> closed with no code change; <n> held.`, and immediate refill.
- A standing style unit syncs at phase start with `git fetch origin <style merge branch> && git merge --ff-only origin/<style merge branch>` (`docs/production_format.md` item 5 exception), so a no-change ticket's `HEAD` stays in the style branch's history; a refused fast-forward goes to the style showrunner as an error.
- `add_style.standing_style_production(repo_dir) -> StandingStyleProduction | NoStyleProduction` (frozen; both expose `checkout` and `doc`) resolves the main checkout through `--git-common-dir`, slug `<main checkout name>-style`, and reads `branch_point` from the doc; it writes nothing. `_request`, `_check_existing`, `style_plan.style_document` and `style_sync.notify` share it.
- `style_footprint.py check` takes exactly one of `--head <rev>` / `--worktree` (else exit 2). `--worktree` runs `git add -A` into a temp copy of the index (`GIT_INDEX_FILE`; ignored and excluded paths never count), `write-tree`, `commit-tree --no-gpg-sign -p HEAD`, and judges that unreferenced commit; the real index is untouched and the temp index removed on every path.
- `style_sync.py notify --repo-dir <checkout> --branch <b> --tip <sha>`: silent exit 0 without a style production on branch point `b`; else `send.py --timeout 10` (15 s deadline) of `From <caller>: branch point <b> moved to <sha7>; sync.` keyed `style-sync:<slug>`; a failed send prints one line, exit 0. `merge_checkpoint.notify_style` and `merge_gate.notify_style` call it after a successful push (30 s subprocess timeout, one-line failure).
- `style_sync.py lint --repo R --checkout C --tip T` runs `verify.sh lint --workspace --no-cache --check` (mend report mode with `--fail-on-warn`, `cargo +nightly fmt --all --check`, clippy, taplo check; never rewrites, runs every tool after a failure) and calls `style_ledger.py lint-record` per tool with per-file counts. Exit 0 all pass, 1 diagnostics or a nonpassing tool (sync continues), 2 error (sync stops).
- `docs/style_production.md` **Sync** (skipped for a `tip seen` tip): merge the tip into the style merge branch and gate; `style_sync.py lint`; `set-tip --style-branch` then `status` (unclassified count to the parent with "pull nate_style"); tell unit directors to merge at their next pause and wake `waiting for sync`; `tip done`. Listed-site refresh by rule blob runs on every read regardless. Mid-ticket merge: clean → recheck the file's incoming diff; conflict → drop every finding touching that file (a multi-file rename as a whole), release freed reservations, regate.

**Files:**
- `commands/unit/direct.md` — `<StandingPlanRefill>`
- `commands/unit/checkpoint.md` — standing staging, gate rerun, no-change ticket, no shrink commit
- `docs/delegate/phase_end.md`, `docs/delegate/run_phase_review.md` — the standing exception
- `docs/production_format.md` — item 5 ff-only exception for standing style units
- `docs/style_production.md` — Sync steps, notify line, plan copies, no-change tickets
- `scripts/style/style_sync.py`, `scripts/style/test_style_sync.py` — `notify`, `lint`
- `scripts/style/add_style.py`, `scripts/style/test_add_style.py` — `standing_style_production`
- `scripts/style/style_plan.py`, `scripts/style/style_footprint.py` and their tests — shared lookup, `--worktree` gate
- `scripts/style/test_standing_checkpoint.py` — temp-repo standing-checkpoint acceptance case
- `scripts/direct/verify.sh` — `lint --workspace --check`
- `scripts/production/merge_checkpoint.py`, `scripts/production/merge_gate.py` and their tests — `notify_style`

**Binds later work:** `ticket <id> merged; refill.` is the only wake for the merge wait; the child hand-off path's push calls the same `notify_style`; new hand-off subcommands join the `style_sync.py` CLI (which already inserts `scripts/production` on `sys.path`); the style showrunner's unit-checkpoint merge must keep the unit's tip an ancestor so the unit's next `--ff-only` merge succeeds; the proposal-ticket approved-file gate replaces the gate in both `<StandingPlanRefill>` and checkpoint step 6.

**Gotchas:**
- The unit's plan copy is its own record; the showrunner checkout's copy is only the first-launch template, so a change meant for a launched unit goes into the unit's copy.
- `merge_gate.notify_style` and `merge_checkpoint.notify_style` are two module copies; merge_gate finds `style_sync.py` through the override env, PATH, then the sibling path.
- checkpoint.md step 2's done-phase-with-Work-Order refusal fires under parallel non-standing phases; no script enforces it.
- review-trial reports the wrong phase without `--phase-id` when two phases are open.

**Ruled out:** rewrite-and-rollback lint in the sync (the check form never touches the tree); a `--no-ff` merge commit for standing units (it breaks no-change ticket ancestry).

### Phase 11 — Child hand-off contract  · status: done
**Depends on:** Phase 6, Phase 7, Phase 10

#### As-built

- `merge_checkpoint.py --from-child <child> --checkpoint <hash> --production <parent doc> --started <time> [--root-self]` merges a style child's hand-off without reading the Units table. One-line refusals: the child is not attached (`parent_of(child) == parent`, or `--root-self` with no parent; `--root-self` on a child that still has a parent is refused), the hash is not on the child's merge branch (found through `showrunners.py` and the child doc's `**Showrunner checkout:**`), it does not descend from the child's last merged hand-off, the parent tree has staged changes (`diff --cached` only; untracked files pass), or a unit-only flag is given (`--regime` other than after, `--holds`, `--merge-defects`, `--excluded`, `--scratch`). Fixed metadata: phase `style hand-off`, no review trial, no `--delivers`/`--parallel`.
- The run (`run_handoff` → `finish_handoff` → `acknowledge_handoff`) runs `checkpoint_tests` (writes `merge_<c7>_test.log` in scratch), merges with subject `Merge <child> hand-off (<hash7>) into <branch point>`, pushes as the production pushes, calls `notify_style`, promotes (`promote(request: MergeRequest | ChildHandoffRequest, tip)`, label `<child> style hand-off`), then sends `From the showrunner of <parent slug>: hand-off <hash7> merged as <merge7>.` to `showrunners.py address <child>`. A root prints `root: run style_ledger.py reconcile to mark <h7> merged` and sends nothing.
- Retries are duplicate-safe: each run computes `NotMerged | MergedNotPushed(merge_head) | MergedAndPushed(merge_head)` from git and the merge history (`handoff_merge_evidence`, `pushed_state`; never stored), retries a failed push before any acknowledgment, and on an already merged hand-off prints `hand-off <hash7> already merged as <merge7>`, acknowledges again and exits 0. `undo_failed_push` aborts a half merge and resets only when origin is unchanged.
- `merge_branch_history` also matches `^Merge (.+?) hand-off \(([0-9a-f]+)\)`; `MergeBranchHistory.entries` is `tuple[MergeEntry | ChildHandoffEntry, ...]` with `ChildHandoffEntry(child, short, merge_branch, merge_hash)`. `last_for_unit`, `last_code_for_unit`, `code_merge_count`, `has_code_merge` skip hand-offs; `last_handoff_for_child(child) -> ChildHandoffEntry | NoMerge` is new; `last_merge()` returns either kind. `ci_points.py` (`due`, `ci collect`, `ci start` label, `notice clear`), `production_lifecycle.py` and `dailies_input.py` guard with `isinstance(entry, MergeEntry)`; the dailies merge-branch topic reads `last merge: <child> style hand-off (<hash7>)`.
- `style_ledger.py`: `ticket merge` refuses any state but checkpointed (`<id>: cannot become merged from <state>`) and requires the checkpoint inside the resolved `sync.style_branch` tip (`<id>: checkpoint <c7> is not on <style branch>`, exit 2). `handoff reroute --repo R --checkpoint C --to SLUG` (`Ledger.handoff_reroute`) moves a `sent` row to SLUG keeping `checkpoint` and `created_at`; the same parent is a no-op exit 0; `pending` (`; use handoff send`), `merged` or unknown exit 2. `ticket_add`'s `git hash-object` failure exits 2 as a `LedgerError`.
- `style_sync.py send-handoff --repo R --slug S --checkpoint C` runs `handoff add`, the notice to `showrunners.py parent S`, then `handoff send`. `style_sync.py resume --repo --slug --branch-point --style-branch` runs `reconcile` and prints its change lines first, then oldest first over pending hand-offs: `pending` → notice and `send`; `sent` to the current parent → notice again, row unchanged; `sent` elsewhere → notice and `reroute`; root → `root: merge <c7> with --root-self`. A failed send prints one line and leaves the row. `add_style.py _attach` calls `resume` in place of the JSON print; `--check` step 7 reads `update Parent production and Proposals, run style_sync.py resume; synced tip unchanged` and runs nothing. The interrupted-bootstrap create path rewrites `**Parent production:**` and `**Proposals:**` to the caller's through the helper `_attach` shares; `--check` there prints step 4 `set Parent production and Proposals in existing production doc <doc>`.
- `produce.md`: a child showrunner is never a unit, except a same-repo style child (`**Kind:** style`), whose hand-off gets the unit checkpoint correctness review on its diff against the branch point and merges through `--from-child` on receipt of `From the showrunner of <style slug>: hand-off <hash>`. `docs/style_production.md` **Merging back** holds the style showrunner's side: `send-handoff` after each unit checkpoint merge, `handoff merged` on the acknowledgment, `ticket merge` then `ticket <id> merged; refill.` to the unit director, `--root-self` merges while a root, `resume` first at startup, and a no-change ticket merges nothing and sends no hand-off or `refill.`.

**Files:**
- `scripts/production/merge_checkpoint.py` — `--from-child`, `--root-self`, delivery state, `ChildHandoffEntry`, promotion and acknowledgment
- `scripts/production/ci_points.py`, `scripts/production/dailies_input.py`, `scripts/production/production_lifecycle.py` — `MergeEntry` guards, the style hand-off label
- `scripts/style/style_ledger.py` — `handoff reroute`, `ticket merge` state and ancestry checks, `ticket_add` error path
- `scripts/style/style_sync.py` — `resume`, `send-handoff`
- `scripts/style/add_style.py` — `resume` on reattach, step 7 line, shared destination rewrite
- `commands/showrunner/produce.md` — the style child exception
- `docs/style_production.md` — Merging back
- `scripts/production/test_merge_checkpoint.py`, `test_ci_points.py`, `test_dailies_input.py`, `scripts/style/test_style_ledger.py`, `test_style_sync.py`, `test_add_style.py` — mixed-history, refusal, delivery-state, reroute, resume and bootstrap cases

**Binds later work:** the cargo-liner pilot's hand-off step runs `merge_checkpoint.py --from-child` and expects tests, merge, push, promotion, then the child acknowledgment (that showrunner is not a root, so it receives the ack); this merge path is what clears the parent dailies `hand-off waiting <n>h` flag. Every later reader of merge history handles `ChildHandoffEntry`.

**Gotchas:**
- Former child slugs go through `showrunners.canonical_slug` before the doc lookup.
- Every style Units row's Owns parses as `('none',)`, so an ordinary style-unit checkpoint is refused `outside Owns: <path>` unless it passes one `--also <path>` per entry of the ticket's `files` (`ticket open --unit <unit>`).
- A root's own hand-off stays `pending` (`handoff merged` refuses `pending`); `reconcile` after each `--root-self` merge marks it merged by ancestry. `handoff merged` before any `reconcile` exits 2, which `resume` running first prevents.

**Ruled out:**
- A single local ancestry check before acknowledging: a failed push would acknowledge an unpushed merge.
- Counting a hand-off toward unit CI points or as a unit phase: it is the style child's work landing.
- Changing `merge_checkpoint.py` scope code for style units: the ticket's files go in as `--also`.

### Phase 12 — Style report: the ledger report command and /showrunner:style_report  · status: done

#### As-built

- `scripts/style/coverage_report.py --repo R [--rule N | --crate K]` reads the ledger through `style_ledger.Ledger` and prints in the user zone: `<slug> — synced <branch point> <tip> at HH:MM <zone>, <n> files (+<d> since yesterday)`; `Rules done: <d> of <t> (<n> by lint) · cells current <p>% · open <o> · stale <s> · awaiting merge <a> · held <h>` (n = done tier A rules); `Unclassified: <n> rules — pull nate_style` when `Ledger.unclassified_rules()` is nonempty; `Waiting to merge back: <n> checkpoints, oldest HH:MM` or `Waiting to merge back: none`; the table `| Rule | Tier | Current | Stale | Open | Awaiting merge | Held | Done |`; `Since last report: <g> rules gained, <l> lost "done"[ to new files]; <p> proposals filed.`
- Rows cover classified tier A-D rules only; tier E and unclassified rules never count, and an all-legacy catalog prints `Rules done: 0 of 0`, the unclassified line and an empty table. `#uncovered` cells count in their parent rule's Current, Stale and Open, never as a row. A rule is done when every cell of every component is current (held and waived count); a partial rule needs its listed and `#uncovered` components both complete. An `awaiting merge` cell prints in the `Awaiting merge` column and aggregate, never in Current, and turns current once the style branch merges into the branch point and `set-tip` moves past it.
- The Rule column prints the stem; a stem two rules share prints the full rule id (`global:rust/foo.md`, `local:docs/<member>/style/foo.md`) on both rows.
- `--rule` takes a rule id or a unique stem (an ambiguous stem exits 2 listing the ids) and prints that row plus its open tickets; `--crate` takes a directory or package name and prints the table for that crate plus its oldest open ticket; an unknown or ambiguous crate, and `--rule` with `--crate`, are refused.
- `ReportSnapshot(rules: {rule_id: {done, cells_total}}, proposals: sorted paths of held_findings("filed"), files_by_day)` is written atomically after each full report; `previous_report` returns `FirstReport | PreviousReport` and refuses a malformed snapshot.
- `/showrunner:style_report [rule|crate]` finds every style production at or below the caller (`showrunners.py children`, or the caller itself) and prints the script's output unchanged.

**Files:**
- `scripts/style/coverage_report.py` — the report CLI, `coverage()`, `CoverageCounts`, `ReportSnapshot`, `previous_report`
- `scripts/style/test_coverage_report.py` — tier rows, held, partial and `#uncovered`, deltas, `--crate`, legacy and mixed catalogs, shared stems, awaiting merge, snapshot location, proposal identities, formatting
- `commands/showrunner/style_report.md` — the command
- `docs/style_production.md` — the Report section

**Binds later work:** `coverage()` parses `Ledger.status()` stdout captured with `redirect_stdout` into `CoverageCounts(current, stale, open, awaiting_merge, held)`; the per-rule counts phase replaces it with a public ledger reader (`rule_counts()`, called once, twice under `--crate`), removes `coverage()`, `CoverageCounts` and the `redirect_stdout` import, and drops the `ledger.rules()` call. Header numbers (rules done, current percentage, stale, proposals, waiting) come from `Ledger.summary()`; the report defines no second count. The snapshot lives at `ledger.database.parent / "last_report.json"`, so `STYLE_LEDGER_STATE_DIR` moves it too. The real cargo-liner report runs in the pilot phase.

**Gotchas:**
- Percentages print one decimal with a trailing `.0` dropped (`62`, `61.8`); counts use thousands separators and a singular noun only for 1.
- The proposals delta compares filed proposal identities, not `summary.proposals`: a waived proposal replaced by a new filing at an unchanged count reads as a new filing.

**Ruled out:** a reader in `style_ledger.py` in this phase, since another phase edited that file at the same time; the per-rule counts phase adds it.

### Phase 13 — Dailies style line: the style summary states in the parent's dailies  · status: done

#### As-built

- `dailies_input.run` adds a `style` key to the saved last report only when `is_style_production(production)` (doc carries `**Kind:** style`): `style_summary(production)` runs `style_ledger.py summary --repo <checkout> --json`, validates it through `parse_style_summary`, and saves the raw JSON so every summary key survives; a failed run or a non-summary object saves `{"unavailable": "<reason>"}`. `save_last_report(slug, saved)` is unchanged.
- `ChildReport.style: StyleSummary | NoStyleSummary | StyleSummaryUnavailable` (frozen): no `style` key parses to `NoStyleSummary` (product children and older reports), `{"unavailable"}` to `StyleSummaryUnavailable(reason)`, the summary object (keys through `check_keys`, unknown keys refused) to `StyleSummary` with `waiting: PendingHandoffs(count, oldest_pending_since) | NoPendingHandoffs` and `synced_tip`.
- `ChildReport.cadence: ReportsEvery(minutes) | ReportsOnDemand` replaces `interval`; JSON `interval` null ↔ `ReportsOnDemand`, a positive integer ↔ `ReportsEvery`; saved JSON is byte-equal.
- `child_row(child, now, zone: ZoneInfo)` puts the style line in `Row.lines_below` (printed under the row, outside chart width fitting): `<child>  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 14:05`, plus ` · unclassified <n> — pull nate_style` when n > 0 and ` · hand-off waiting <h>h` when `oldest_pending_since` is more than 4 h before now (an unsent `pending` hand-off counts). `cells` is `math.floor(pct)`, so only 100.0 reads `100%`; `synced` is `range_clock` (bare time today, `Thu 10:05` otherwise). `StyleSummaryUnavailable` prints `<child>  style summary unavailable: <reason>`; `NoStyleSummary` prints nothing.

**Files:**
- `scripts/production/dailies_render.py` — style types, `parse_style_summary`, cadence, `Row.lines_below`, the style line in `child_row`
- `scripts/production/dailies_input.py` — `is_style_production`, `style_summary`, the `style` save
- `scripts/production/test_dailies_render.py`, `scripts/production/test_dailies_input.py` — render states, unknown-key refusal, save and real-summary round trip

**Binds later work:** the style line travels with the child's row, so detaching a style child needs no dailies change; a live dailies check generates the child report first, since the parent reads only the saved one.

**Gotchas:**
- `StyleSummary.synced_at` is naive local in the report zone; `PendingHandoffs.oldest_pending_since` is aware UTC, and the hand-off age is computed from aware instants.
- The parent reads only the child's saved report, and a style production reports on demand, so the line is as fresh as the child's last report.
- A middle production's saved report does not carry its style child's summary, so a grandparent's `expand` shows no style line; `commands/showrunner/dailies.md` "Productions below" does not describe the line.

### Phase 14 — Per-rule counts: one public ledger reader for the style report  · status: done

#### As-built

- `style_ledger.py`: frozen `RuleCounts(rule_id, tier, current, stale, open, awaiting_merge, held)` with properties `total` (current + stale + open + awaiting merge) and `complete` (current == total over the cells counted); `held` counts current cells whose verdict is `held`. Scope selector `CoverageScope = WholeRepository | CrateCoverage(crate: str)` (frozen dataclasses).
- `Ledger.rule_counts(scope: CoverageScope) -> tuple[RuleCounts, ...]` makes one `_coverage` pass per call through the private fold `_count_rows(coverage)`: tier `unclassified` is skipped, `#uncovered` cells fold into their rule, tier E rows stay (zero cells, complete). It is the one public per-rule reader; `RuleProgress`, `Ledger.rules()` and `_rule_progress` no longer exist.
- `Ledger.status(rule: str | None = None, scope: CoverageScope = _WHOLE_REPOSITORY, as_json: bool = False)`: the CLI turns an omitted or empty `--crate` into `WholeRepository()`; only the private `_crate_filter` yields the `str | None` crate filter `_coverage` takes. Text counts come from `_count_rows(self._coverage(rule, crate))`, `done` from `rule_counts(WholeRepository())`; a whole-repo `status` reuses one pass, a selected one makes two. `status --rule <id>#uncovered` counts the uncovered component alone under the parent id with the parent's repo-wide `done`. Text and `--json` output match the pre-phase CLI byte for byte.
- `Ledger.summary_of(repository_counts) -> StyleLedgerSummary` builds the summary from whole-repo rows in hand (`rules_done`/`rules_total` over tiers A-D, `cells_current_pct`, `stale`); `summary()` is `summary_of(rule_counts(WholeRepository()))`, JSON unchanged.
- `coverage_report.py`: `coverage()`, its `redirect_stdout` text parse and `CoverageCounts` are gone. `render_report` calls `rule_counts(WholeRepository())` once, plus `rule_counts(CrateCoverage(crate))` under `--crate`, and builds header, table and snapshot from those rows and `summary_of(repo_counts)`. The table's `done` column reads `complete` from the whole-repo rows under `--crate`, matching `status`.

**Files:**
- `scripts/style/style_ledger.py` — `RuleCounts`, `CoverageScope`, `rule_counts`, `_count_rows`, `summary_of`, scope-typed `status`, `_crate_filter`
- `scripts/style/coverage_report.py` — report rows from `rule_counts`; header and summary from the whole-repo pass
- `scripts/style/test_style_ledger.py`, `scripts/style/test_coverage_report.py`, `scripts/style/test_style_plan.py`, `scripts/style/test_add_style.py` — exact-output `status --crate` and `#uncovered` tests, a counting stub on `Ledger._coverage` (one pass whole-repo, two under `--crate`), removed-API tests ported to `rule_counts(WholeRepository()).complete`

**Binds later work:** `done` under `--crate` is currently whole-repository in both `status` text and `coverage_report.table_row` (a rule complete in `alpha` and open in `beta` prints `done` `false` under `--crate alpha`); the proposal-batching phase makes it crate-scoped in both and flips those tests, while the header's `Rules done: X of Y` and `summary_of` stay whole-repository. `Cell.family` remains in `status --json` for grouping cells by `crate` and `family`.

**Gotchas:**
- A call in a default initializer is a basedpyright warning, so the `status` scope default is the module-private frozen `_WHOLE_REPOSITORY` instance.
- `RuleCounts` carries no `family`; `Cell.family` stays for the planner and `status --json`.

**Ruled out:** a `family: Family | None` field on `RuleCounts` — an unnamed absence state with no consumer; `status(crate: str | None)` as the public API — the scope type carries the choice; a second public reader beside `rule_counts` (`rules()`/`RuleProgress`) — two readers of one fact.

### Phase 15 — Proposal batching and wrap detaching a style child  · status: done

#### As-built

- `style_proposals.py file --repo R --proposals <dir> --parent <slug>` batches `unfiled` held findings into hanadocs issues by kind: one per (rule, crate) for `rename` and `type`, one per crate for `restructure` (tier D and `mode: propose` findings). Each issue is `style <rule stem or crate> <batch id8>.md` (sha256 over sorted finding ids), category `refactor`, tags `[issue, proposal, style]`, area `unfiled`, `style_findings: [<ids>]`, written through `proposal_issue.py create`; `file` first marks `filed` any finding an existing `style` issue lists (crash recovery), skips a finding whose `rule_blob` differs from the rule's current blob (`skipped <finding id>: rule changed since judged`), and exits 1 with one line on a git or ledger failure. `--proposals` and `--parent` are read fresh from the style doc and `showrunners.py parent` at each run.
- Phase 19 replaced these batches with style decisions: one decision per rule, crate and rule blob, whose answer also covers later findings, with `style_findings` moved to the ledger. `docs/style_production.md` describes the current filing.
- `issue_project(repo, crate, vault) -> IssueProject(short, project)` reads each `home-base/issues - <short>.base` file's first `project.contains(link("<project>"))` filter, matching the package name first, then the repository name; no match files under the repository name and prints `no issue base for <name>: run /new_issue_base <name> in hanadocs`.
- `style_proposals.py complete --repo R` closes each merged proposal's issue (`proposal_issue.py close --reason 'built in <ticket id>'`); `decline --issue <path> --words '<words>'` calls `Ledger.decline_findings(finding_ids, words) -> dict[str, DeclineOutcome]` once for the filed findings (one transaction, one catalog load; `DeclineOutcome = CellWaived | CellStillHeld | InputChangedSinceJudged | NoHeldJudgment`), re-recording the newest held cell row `waived` only when the cell is current and every other held finding in it is waived (else `not re-recorded <id>: input changed since judged`), then closes with `--reason 'declined: <words>'`. Both rerun `renumber.py --apply` on any run that sees a merged ticket or an already-closed issue; reruns are otherwise no-ops.
- Ledger: `TicketKind` gains `proposal` (migration 6, `tickets.proposal`); held state gains `Built()`, set in the same transaction as `ticket merge` of a proposal ticket; `Ledger.ticket_origin(id) -> ApprovedProposal(issue, unit, finding_ids) | SelectedByCoverage(base)`; `Ledger.held_findings(selection: AllHeldFindings | HeldFindingsInState)`, the CLI mapping an omitted `--state` to `AllHeldFindings()`.
- `style_plan.next_phase` assigns the oldest approved `style` issue for the unit before coverage selection (`proposal_issue.py start` at assign); when its files are busy it prints `waiting <issue>: files busy` and falls through to coverage, the issue staying approved for the next refill. `style_plan.py gate --repo . --ticket <id>` (exit 0 permits, 1 returns to repair, 2 could not run) runs `style_footprint.py check --base <SelectedByCoverage.base> --worktree` for coverage tickets and, for `ApprovedProposal`, requires every changed file (`git status --porcelain`) to belong to an approved finding; a proposal Work Order overrides the mechanical-only lines for exactly those findings.
- Under `--crate`, `status` text and the report table's `done` column read the crate's rows; the header's `Rules done` and a `--rule`-only `status` stay whole-repository.
- `production_lifecycle.wrap` detaches running style children only after promotion, unit retirement and notifier removal (`refuse_running_children -> RunningStyleChildren`, `detach_style_children`, beside `release_edges`), telling each `<parent> wrapped, so you are a root now: run style_sync.py resume and merge your own hand-offs (docs/style_production.md, Merging back).`; any other running child holds the wrap. `retire_reservations` reads `berth_board.read_board`, printing `retire skipped: board unavailable (<reason>)` on `BoardUnavailable`; an unconfigured repository releases nothing silently.

**Files:**
- `scripts/style/style_proposals.py` — `file`, `complete`, `decline`
- `scripts/style/unit_files.py` — live-unit file lookup (`UnitWorktree`, `git`, `unit_worktrees`, `unit_file_holders`, `changed_files`), no CLI
- `scripts/style/style_plan.py` — proposal tickets, approved-first selection, `gate`
- `scripts/style/style_ledger.py`, `scripts/style/coverage_report.py` — proposal kind, `built`, `ticket_origin`, `decline_findings`, held selection, crate-scoped `done`
- `scripts/production/proposal_issue.py` — `create`, `close --reason`
- `scripts/production/production_lifecycle.py` — style child detach, board reader
- `commands/showrunner/proposal.md` — style issues decline through `style_proposals.py decline`
- `commands/unit/direct.md` (`<StandingPlanRefill>`), `commands/unit/checkpoint.md` (step 6) — both ticket gates call `style_plan.py gate`
- `docs/style_production.md` — Big changes, Lifetime: `file`, `decline`, the approved path, wrap detach

**Binds later work:** proposals are filed with category `refactor`, tag `style`, and declined only through `style_proposals.py decline`; `next_phase` assigns the oldest approved style issue before coverage, and an approved proposal with busy files waits and falls through to coverage; `style_plan.py gate` serves both ticket gates, reading `SelectedByCoverage.base`; a held wrap keeps its style child attached and untold; `issue_project` falls back to the repository name and prints the setup line when no base matches.

**Gotchas:** a vault base's short name differs from its project (diegetic → hana_diegetic, bei → bevy_enhanced_input), so the project is never derived from the file name; `unit_files.py` exists to break the `style_plan`/`style_proposals` import cycle, so the lookup stays there; cargo-liner has no issue base in hanadocs, so its proposals print the setup line until `/new_issue_base cargo-liner` runs; `/close_issue` on a style issue leaves its findings held.

**Ruled out:** an `improvement` category (the vault has none); a `holders` CLI verb for the busy-file lookup; detaching style children before the wrap's close-out steps.

### Phase 16 — Pilot bootstrap flag and plan scope  · status: done

#### As-built

- `add_style.py --pilot <unit>[=<crate>[,<crate>…]]` (repeatable, create path only) bootstraps a style production whose Units table lists only the named units; every family's standing plan is still written, so an unnamed unit (`values-unit`) has a plan and no row. A `=<crates>` value writes `- **Pilot scope:** <crates>` into that unit's showrunner-copy plan Delegation Context before the first launch, so `add_unit.py`'s first-launch copy carries it. An interrupted-bootstrap rerun rewrites rows and scope lines only for units whose worktree does not exist yet. `--check --pilot` lists the pilot units and scopes in steps 4-5. On an existing production the flag exits 2 `add_style: --pilot applies only while creating <slug>`.
- `style_plan.parse_pilot_scope` reads the scope from the `--plan` copy `next_phase` is given: no line is `AllCrates()`, a line is `PilotCrates` of workspace package names; more than one line refuses `pilot scope has more than one scope line`, an unknown package `pilot scope names unknown crate`. `candidate_tickets` skips crates outside the scope; an approved proposal with a finding outside it prints `waiting <issue>: outside pilot scope`, stays approved and unchanged, and coverage selection proceeds, as for busy files.
- `docs/plans/build-followups-style-pilot.md` is the pilot report skeleton: the real-repo checks before bootstrap are recorded (missing `**Proposals:**` refused; at cargo-liner `f65032bb`, rules list with 0 unclassified, ledger status, summary keys); the live criteria are pending.

**Files:**
- `scripts/style/add_style.py` — `--pilot` (`_pilot_units`, `_configure_pilot`, `_pilot_steps`)
- `scripts/style/style_plan.py` — `parse_pilot_scope`, `AllCrates` / `PilotCrates`, scope filter in `candidate_tickets` and the approved-proposal branch of `next_phase`
- `scripts/style/test_add_style.py`, `scripts/style/test_style_plan.py` — pilot rows, scope lines, refusal, rerun, differing showrunner and unit copies
- `commands/showrunner/add_style.md`, `docs/style_production.md` — `--pilot` usage; scope placement (showrunner copy before the first launch, the unit copy after)
- `docs/plans/build-followups-style-pilot.md` — pilot report skeleton

**Binds later work:** the pilot is a fresh bootstrap (`--pilot` refuses an existing production); after a unit's first launch its own copy (`<main checkout parent>/<slug>-<family>/docs/style-production/units/<family>-unit.md`) is authoritative and the only place its scope changes; a unit with no scope line reads `AllCrates`; `style_plan.py queue` and `coverage_report.py` apply no pilot scope.

**Gotchas:** scope lines name workspace packages, not member directories — `style_ledger.py status` groups by member directory (`crates/cargo-port`) while `--pilot` takes the package (`cargo-port`); a repo-local rule can follow the last `**Family:**` snapshot entry, so the last entry is not always global; the per-finding `crate_for` lookup runs git even under `AllCrates`.

**Ruled out:** a manual launch hold in place of an `add_style` flag.

### Phase 17 — Live pilot under cargo-liner's showrunner  · status: done

#### As-built

- `cargo-liner-style` runs attached under cargo-liner's `remove-running-section` showrunner. Three units run: `modules-unit` on all crates, `names-unit` on `cargo-handler`, `types-unit` on `cargo-port`. No `values-unit` runs.
- All five pass criteria pass: recall, hold time, quiet-branch hand-off, restart and yielding. The real-repo checks, scoped assignments and ticket gate also pass live.
- Pilot fixes A to AJ are in `scripts/style/`, one commit each. `name-bindings` has its own read ticket, and the names read ticket runs two blind judges. Modules structure tickets ask the when-to-split question of every file. Coverage skips a held cell with open proposals while it stays unchanged. A ticket checkpoint re-stamps its family's verdicts on files only it changed. Style issues carry a proposed ranking. Proposals read the crate layout once per run.

**Files:**
- `docs/plans/build-followups-style-pilot.md` — the pilot report: real-repo checks, recall (per rule, candidate file-local rules), hold time, quiet-branch hand-off, restart, yielding, ticket gate, refill and sync live evidence, style report, `--from-child` trace, dailies, fixes.
- `scripts/style/style_plan.py`, `style_ledger.py`, `style_proposals.py`, `style_sync.py` — the fixes above.

**Binds later work:**
- A repository's branch point must be on origin before its style tickets read it. This applies to hana in the roll-out.
- The showrunner applies edits to `config/agents.conf`, `settings.json` and `CLAUDE.md` as exact line edits. It gets them in one message with the nix list and the Mac launchd unload. This branch does not change those files.
- 13 rules proved file-local. Their `input_scope: file` change in nate_style waits on the user's go.
- The approved ranking backfill filled 279 hanadocs style issues. Git does not track them.

**Gotchas:**
- Style tools read `origin/<branch point>`. A branch point that is not pushed reads as missing.
- Refill exit 4 (unavailable board) and the failed-lint sync path did not occur live. Only tests cover them.

**Ruled out:**
- One names read ticket with one judge: it misses the 95% recall bar.
- Modules structure tickets without the when-to-split step: they miss the recall bar.

### Phase 18 — Retire the fix pipeline and narrow the nightly rust mode  · status: done

#### As-built
- The fix pipeline is gone: `scripts/fix/`, the commands `/fix`, `/style_eval`, `/style_fix_review`, `/style_usage` and `/nightly_weights`, the weights code, the worktree redirect hooks and the new-project enrollment.
- The nightly review `rust` mode proposes no style sweeps and shows no weight line. Its other Rust repo, style-rule quality and process checks stay, as do the `config` and `messages` modes.
- The kept commands work from `scripts/style/` with no missing-path error: `/focused_eval`, `/style_rename`, `/style_delete`, `/rust_style`, `/local_style`, `load-rust-style.sh`, `/mend_fix`, `/berth_fix`, `/lint_config`.
- Outside history lines and the plan, the retired names stay only in `config/agents.conf`, `settings.json`, `CLAUDE.md` and `.claude/settings.local.json`. This branch edits none of them. Their exact line edits, the nix module deletions and comment rewording, and the Mac `com.natemccoy.style-fix.plist` launchd unload went to the build-followups showrunner as one message for the user to apply.

**Files:**
- `scripts/fix/` — deleted
- `commands/fix.md`, `commands/style_eval.md`, `commands/style_fix_review.md`, `commands/style_usage.md`, `commands/nightly_weights.md` — deleted
- `scripts/nightly_review/weights.py`, `scripts/nightly_review/test_weights.py`, `scripts/make_a_worktree/retarget_fix.py` — deleted
- `docs/as-built/fix-pipeline.md`, `docs/plans/fix-pipeline-deferred.md` — deleted
- `commands/nightly_review.md` — rust mode without style sweeps or a weight line
- `commands/make_a_worktree.md`, `commands/worktree_delete.md`, `scripts/worktree_delete/perform_deletion.sh`, `scripts/new_rust_project/rust_generate.sh` — no fix-pipeline hooks
- `commands/lint_config.md`, `scripts/lint/*`, `scripts/direct/verify.sh` — consumers without the fix pipeline
- `scripts/cli_agent/cli_agent.sh`, `scripts/whoami/test_quota_alert.py`, `scripts/agents/test_agent_exec.py`, `scripts/bevy_migration_plan/bevy_migration_ensure_repo.sh`, `docs/as-built/agent-registry.md` — fix references removed
- `pyrightconfig.json` — no `scripts/fix` environment

**Gotchas:**
- The four held files still name the retired pipeline until the user applies the edits.
- `load-rust-style.sh` is a zsh script; bash fails on it by design.
- The base already carries two basedpyright errors (missing `bpy` and `PIL` imports), so a `0 errors` gate reads as "no new diagnostics".

### Phase 19 — Roll-out  · status: done

#### As-built

- The roll-out runs as a stream of `scripts/style/` fixes that the cargo-liner roll-out showed, each its own checkpoint, merged as it lands. `docs/plans/build-followups-style-pilot.md` has no `## Roll-out` section; the checkpoints are the record.
- Style decisions: `style_proposals.py file` makes one decision per rule, crate and rule blob. Its answer (approved, declined or unanswered) also covers later findings under the same rule text, and the ledger keeps which decision holds each finding (migration 12, `moved_from`). Refill builds approved decisions in small tickets; `decline` waives the filed findings in one transaction; off-rule finds become rule candidates (`drop-candidate` removes one). Decision names show `/` in a crate path as `+`. `complete` and refill close an approved decision whose findings are all built or waived.
- `style_proposals.py migrate [--dry-run]` moves open style issues into decisions: `plan_moves` writes nothing, `apply_moves` writes, and a rerun is safe. An off-rule issue (filed by a style unit through `/issue`, no `style ` name prefix, no held finding) is never written; each prints `skipped: off-rule <path>`. The live cargo-liner run gave create 64, move 9306, close 500, off-rule 9.
- `style_ledger.py record` refuses a clean, exception or held verdict on a crate or module cell of an included Rust file without `--input-scope file` (the verdict rests on that file and stays current when other files change) or `--input-scope rule`. Local rules follow the same path.
- `style_plan.py gate` and `style_footprint.py check` print `error: <reason>` and exit 2 when the check could not run (`CheckCouldNotRun`, a `LedgerError`); `review:` with exit 1 is a real review result. Every ledger connection uses `BUSY_TIMEOUT_SECONDS = 30`. Check 5 counts changed lines per file (`FILE_CHANGED_LINE_LIMIT = 200`), reason `check 5: <path>: 200 or more changed lines (size only)`.
- `approved_renames.py`: `applied_check(kind, before, after, path, read) -> AppliedInCode | RenameNotApplied | NoAppliedCheck`. A merged ticket marks a rename finding built only on `AppliedInCode`; `RenameNotApplied` stays filed and loses its build ticket; `NoAppliedCheck` keeps its merged ticket and a person settles it with `held built --id`. `ticket merge` prints a `not built` line for each finding left open. `reopen --issue --parent [--dry-run]` refiles the built findings of an approved issue whose rename is not in the code. `FileRename` lets the gate accept one approved module file rename, its `mod` line and the paths that name the module.
- Ledger writers (`set_tip`, `ticket_add`, the ticket moves, `ticket_base`, `mark_built`, `decline_findings`) do git, file and catalog work before `BEGIN IMMEDIATE`, check the rows again under the lock, and retry up to 3 times when they changed (`retry_on_change`); the third failure refuses with `...; rerun`.
- Held state `withdrawn` (migration 13, `withdrawn_by` records the judgment row): `style_proposals.py withdraw --repo --proposals [--dry-run]`, also run first by `file`, withdraws a filed finding when a judgment newer than the finding records exception at its cell. A finding an open ticket builds is kept and listed. A decision whose findings are all withdrawn closes with a reason that names them.
- Earlier fixes: the gate proves import, variant-rename, local-rename and lint-attribute edits as mechanical; an approved qualified rename may update its uses in other files; proposals carry `style_rules` links (`proposal_issue.py --rules`, `rules` verb, `style_proposals.py link-rules`); ranking takes the project's most common `backlog_goal`; a unit plan's `**First files:**` line ranks those files first; `/new_style` writes a rule with complete metadata and `/local_style` runs it.

**Files:**
- `scripts/style/style_proposals.py` — `file`, `rank`, `link-rules`, `complete`, `candidates`, `sites`, `reopen`, `withdraw`, `migrate`, `drop-candidate`, `decline`
- `scripts/style/style_ledger.py` — decisions, `withdrawn`, input scope, busy wait, `retry_on_change`, migrations 12-13
- `scripts/style/style_plan.py` — gate exit codes, applied check at gate and merge, first files
- `scripts/style/style_footprint.py` — per-file size check, error exit 2
- `scripts/style/approved_renames.py` — applied check and file renames
- `scripts/production/proposal_issue.py` — `--rules`, `rules`
- `docs/style_production.md`, `commands/unit/checkpoint.md`, `commands/unit/direct.md` — input scope flag, exit 2 handling (rerun the gate, keep the reservation, stop if the same error comes back)
- `commands/new_style.md`, `commands/local_style.md` — rule creation

**Gotchas:**
- The ledger stores absolute proposal paths: a dry run on a copied issues folder needs those paths rewritten in the copy ledger.
- The applied check gives a firm result only when it is certain; `paste!`, `include!`, a non-std derive on the parent, a keyword file name or a folder of child modules give no answer.
- A decision's approval does not carry over to a new decision; `reopen` refuses a declined or unanswered decision, an open ticket and unsynced merged tickets.
- hana's `/showrunner:add_style` roll-out stays with the showrunners.

**Ruled out:**
- An off-rule issue build ticket: approved proposals build as plain numbered code phases outside the style ledger.
- A restructure split command: style decisions replace it.

### Phase 20 — Values work starts per file  · status: done

#### As-built

- `blocked_by_structure` (`scripts/style/style_plan.py`): a cell at a file waits only for open or stale tier-D cells at its own path; a crate-level cell (path == crate) waits for every one in its crate. Busy files still block. `waiting for structure` names only crates where every own cell is still blocked.
- Move-in: a file whose content changes goes stale through its file-scope fingerprint (no new code). Refill's waive loop also waives a filed finding with no build ticket whose one-line text left its file and went into exactly one file that gained the line since the finding was judged, with reason `<file> site moved to <file> in synced commit <tip>`. `style_proposals.MovedSites` (one per refill; one `git diff` per judged commit), `Ledger.judged_commit -> JudgedCommit | NoJudgedCommit` (judging ticket's checkpoint, else base), `unit_files.file_text` (bytes decoded with `errors="replace"`).

**Files:**
- `scripts/style/style_plan.py`, `scripts/style/style_proposals.py`, `scripts/style/style_ledger.py`, `scripts/style/unit_files.py`, `scripts/style/test_style_plan.py`, `docs/style_production.md`

**Gotchas:**
- A finding text that is a description, not a source line, never matches a destination, so it stays filed.
- Only approved decisions of the refilling unit are checked; unapproved decisions wait for approval.

### Phase 21 — Reopen every false built rename  · status: done

#### As-built

- `style_proposals.py reopen --all [--dry-run]` (`reopen_all`; the per-decision body is `file_unapplied_again`): every approved decision that holds a built finding, found from the ledger's built rows (closed decisions too), gets the same check and refusals as `--issue`; a refused decision prints a skip line and the run exits 1; `totals: reopen N, filed again M, skipped K`; one renumber after real reopens. `--parent` is needed only with `--issue`.
- `held add` returning a built row whose rename is not in the code at the synced tip prints `built, but not in the code at <tip7>: run style_proposals.py reopen --all (decision <path>)` to stderr and changes no state; a check that cannot run leaves the hint out. `Built` carries `proposal`.

**Files:**
- `scripts/style/style_proposals.py`, `scripts/style/style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_ledger.py`, `docs/style_production.md`

**Gotchas:**
- A built finding whose decision file is missing was skipped without a line; phase 22 prints `skipped <issue>: missing file`.

### Phase 22 — reopen --all covers old-form issues  · status: done

#### As-built

- `reopen --all [--parent <slug>]` takes every issue that holds a built finding: approved decisions and approved old-form issues. An old issue moves under the parent its own `Parent:` line names (`old_issue_parent`); `--parent` is used only for an old issue with no such line, else it is skipped with `no Parent line; pass --parent`.
- Each issue with a built finding that the run leaves out prints `skipped <issue>: <reason>` (`missing file`, `not approved`, `closed without answer`, or the reopen refusal) and counts in `skipped`; stderr says `skipped N issues`.
- `run_moves` refreshes a decision it did not create with that decision's own parent, read before the first write, as withdraw does. `join_action` turns a missing generated body into a `LedgerError`, so it is refused before any write and the dry run matches the real run.
- The `held add` hint says `(issue <path>)`.

**Files:**
- `scripts/style/style_proposals.py`, `scripts/style/style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_ledger.py`, `docs/style_production.md`

**Gotchas:**
- The issue's `showrunner` field is a session name, not a parent slug; the body's `Parent:` line is the source.

### Phase 23 — reopen names unchecked built findings and takes a person's override  · status: done

#### As-built

- reopen (`--issue`, `--all`) prints `cannot check <id> in <path>: <before> -> <after>; pass --finding <id> or --applied <id>` for each built rename the applied check cannot judge, and exits 1; restructure and type rows count only in `restructure not checked R`. `--all` totals: `reopen N, filed again M, cannot check K, restructure not checked R, skipped S`.
- `reopen --issue <p> --parent <s> --finding <id>... --by <who>` files built findings again without the check; `--applied <id>... --by <who>` keeps them built (refused when the check finds the rename not applied). Unique prefixes; blank names, `--by` alone, and either with `--all` are refused; `--by` is stripped. All refusals come before the first write.
- Migration 14: `held_findings.person_read_by`; `PersonReading = NotReadByPerson | ReadByPerson(by)`; the state shows the verdict; `held list` (and `--json` field `reading`) shows it. A move into built (ticket merge AppliedInCode, `mark_built`) clears it. `held add` leaves its reopen hint out for a row a person read.

**Files:**
- `scripts/style/style_proposals.py`, `scripts/style/style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_ledger.py`, `docs/style_production.md`, `commands/showrunner/proposal.md`

**Gotchas:**
- A non-std derive (for example serde `Serialize`) on the parent gives no applied check; in production, 3 built renames in old issue 1d00e8b8 (57b9f36a, 00093229, 0cdf4372) are false built and need `--finding`.

### Phase 24 — proposal gate takes the pairs it has; duplicates and plain-merge builds  · status: done

#### As-built

- `approved_rename_pairs` skips a rename finding with no `<parent>::<name>` pair (bare name, dot-form field, prose); a type or restructure finding still returns no pairs. Approval stays exact per token.
- Bare duplicates: migrate and reopen print `drop <id> in <decision>: duplicate of <id>` and never move one beside its qualified finding. `style_proposals.py drop-duplicates --repo <repo> [--dry-run]` withdraws every bare duplicate that is filed or built, regenerates each decision body (also a stale one), and exits 1 on any skip. Migration 15: `held_findings.duplicate_of`; `WithdrawnBy = ExceptionVerdict(judgment) | DuplicateOf(finding)`.
- A proposal ticket's phase text lists the exact cells to record (`Ledger.proposal_cells`, through the same `_check_cell_path` checks as record) and names the rename pairs; a rename with no pair is applied in its own file only. A git failure while listing cells stops the command.
- `style_ledger.py held built --id <id>... --commit <merge> [--by <who>]` (`mark_built_at_merge`, `BuiltAtMerge`) marks filed findings built after a plain merge. The applied check runs at `<merge>`; a rename not applied is refused; a finding the check cannot prove needs `--by` and records `person_read_by`. All refusals come before the first write.

**Files:**
- `scripts/style/style_plan.py`, `scripts/style/style_proposals.py`, `scripts/style/style_ledger.py`, their `test_*.py`, `docs/style_production.md`, `commands/showrunner/proposal.md`

**Gotchas:**
- `held built --commit` takes the code repo's merge, and that merge must be an ancestor of the ledger's synced tip and not older than each finding's judged commit; a merge on the style branch that the last sync did not take is refused until the next sync.

### Phase 25 — refill never reissues a ticket that cannot change a cell  · status: done

#### As-built

- Refill leaves a stale cell out of ticket selection when its newest judgment (current rule blob, newest declared scope, scope at the synced tip) is in the unit branch tip and in the style branch and saw the inputs the unit tip has (`Ledger.judged_at_base`, through `_repeated_judgments` and `_judgment_inputs_match`). Only the unit's own ticketable cells are checked; ranking and structure blocking still see every cell. `RefillCells` is built once per selection.
- `next-phase` exit 6, `MergeStyleBranchFirst(cell_count, unit_tip, style_branch)`: `merge the style branch first: <n> cells judged at <tip7> wait for <style branch>`, only when a style branch is stored (`StoredStyleBranch | NoStoredStyleBranch`), the unit tip is in the pushed style tip, and that tip changes at least one left-out cell (`Ledger.style_merge_changes`). Otherwise refill waits for sync. A style branch missing on origin stops refill with the `fetch origin first` error. The unit runs `git fetch origin <style merge branch> && git merge --ff-only origin/<style merge branch>`, then refills; a refused or empty fast-forward ends blocked on the showrunner.
- The held-cell skip ignores waived, built and withdrawn findings and needs at least one remaining finding, each filed on an open proposal.

**Files:**
- `scripts/style/style_plan.py`, `scripts/style/style_ledger.py`, `scripts/style/test_style_plan.py`, `scripts/style/test_style_ledger.py`, `docs/style_production.md`, `commands/unit/direct.md`

**Gotchas:**
- Refill does not fetch: it reads the local `origin/<style>` ref, so a stale tracking ref gives exit 3 until a fetch or push updates it.

### Phase 26 — a waived finding can be filed again  · status: done

#### As-built

- `style_proposals.py unwaive --repo <style checkout> --proposals <folder> --id <id>... --by <who>` moves each named waived finding back to unfiled (no proposal, build ticket or move) and prints `unwaived <id> by <who>` per finding. Ids take unique prefixes (`Ledger.named_held`). It refuses before any write when an id names no finding or several, the finding is not waived, its rule text changed since it was judged (`rule_changed_since_judged`), an open ticket judges or builds it, its decision is declined, or `--by` is blank.
- `Ledger.unwaive_held` runs every check and the one write in a single transaction; a cell whose newest judgment is 'waived' goes back to 'held' with detail `unwaived by <who>`. The command adds only the decline check.
- Migration 16 adds `held_findings.unwaived_by`, read as `WaiveUndo = NoWaiveUndo | WaiveUndoneBy` (`HeldFinding.waive_undo`). `held list` shows `unwaived by <who>`, `--json` carries `unwaived_by`; no later change clears it, a new waive included.
- `file_decision` returns its `JoinAction`; `file` prints `waived <n> findings in <decision>: declined: <words>`.

**Files:**
- `scripts/style/style_ledger.py` — schema, `WaiveUndo`, `unwaive_held`, `named_held`
- `scripts/style/style_proposals.py` — `unwaive` command, decline check, `file` output
- `scripts/style/test_style_ledger.py`, `scripts/style/test_style_proposals.py`
- `docs/style_production.md` — waived line and unwaive paragraph

**Gotchas:**
- `style_ledger.py` cannot import `style_proposals` (two module copies under `__main__`), so vault-aware checks live in `style_proposals.py`.

**Ruled out:**
- The command in `style_ledger.py` without the decline check: a declined decision waived an unwaived finding again at the next `file`, with no message.

### Phase 27 — a held finding follows its declaration  · status: done

#### As-built

- `declarations.py` reads Rust items: `Declaration(keyword, name, owner)` with `owner` `ModuleItem()` (file top level, inline module or fn body) or `ImplOrTraitItem(name)` (impl type's last segment, after `for` for a trait impl; trait name in a trait body). Inline module names are not part of the identity, so a split of `mod a { … }` into `a.rs` follows. `read_declarations(text)` gives a `DeclaredFile` with `declared_items` and `declaration_at(line) -> DeclaredItem | NoDeclarationAtLine` (innermost item; a blank or comment line reads as the next token line; an impl or `use` line is `NoDeclarationAtLine`); `moved_site_line` gives the new line (same stripped text in the item's own span, else its keyword line).
- The ledger declaration step reads each unfiled or filed `.rs` finding at its read commit (the judged commit, or the last follow's commit) when that commit is an ancestor of the synced tip. Outcomes, computed with no write (`declaration_reading`): `DeclarationNotRead | DeclarationInPlace | DeclarationMoved | DeclarationRemoved | DeclaredInSeveralFiles | DeclaredInAnotherCrate | KeptForOpenBuild`. A destination is a file that gained the declaration since the read commit; one destination follows, none withdraws with `DeclarationGone(<tip>)`, several are left. A filed finding whose destination is in another crate is left; an unfiled one follows. A move or withdrawal of a finding that an open ticket builds is kept.
- `follow_declarations` writes every follow and withdrawal in one `BEGIN IMMEDIATE` transaction and rechecks the synced commit and each row under the lock (`retry_on_change`); a git failure stops before any write.
- Migration 17 adds `followed_from`, `followed_from_line`, `followed_at`, `declaration_gone_at`. `HeldFinding.follow: NotFollowed | FollowedDeclaration`; a second follow overwrites, so the record is the last move and `finding_id` never changes. `WithdrawnBy` gains `DeclarationGone`; `Withdrawn.proposal` is `Path | NoDecision` (unfiled when withdrawn). `held list` shows `followed from <path>:<line> at <commit7>`; `--json` adds `followed_from`, `followed_at`, `declaration_gone_at`.
- `Ledger.hold` of an absent id returns a followed unfiled or filed row with the same rule, path, text, kind, before, after and blob, nearest line, not a new row.
- `style_proposals.py follow --repo --proposals [--dry-run]` (`run_follow`) prints `follow`, `withdraw`, `left`, `kept`, `close` and `totals` lines, then closes a decision whose findings are all withdrawn (`plan_withdrawal` close) or refreshes it with its own `Parent:` line. A decision with no generated body or `Parent:` line prints `skipped <issue>: <reason>` and its findings stay unchanged. Exit 1 on a left finding or a skipped decision. Every run refreshes open decisions holding followed findings, so a rerun finishes a stopped run.
- `file` runs the step first, before the withdrawal step; left lines do not change its exit code. Refill runs it once per proposals folder before its decision loop; followed findings ticket at their new path, a left finding prints `waiting <decision>: <id> declared in <n> files`, and a decision the step skips waits. The missing-file and moved-site waives apply only to findings the step could not read. `reopen` does not run it.

**Files:**
- `scripts/style/declarations.py` — Rust item reading
- `scripts/style/style_ledger.py` — the step, migration 17, `hold` fallback, caches
- `scripts/style/style_proposals.py` — `run_follow`, `follow` command, `file` wiring
- `scripts/style/style_plan.py` — refill wiring
- `scripts/style/test_declarations.py`, `test_style_ledger.py`, `test_style_proposals.py`, `test_style_plan.py`
- `docs/style_production.md`, `commands/showrunner/proposal.md` — the step, its lines and the command

**Gotchas:**
- Declaration identity (keyword, name, owner) collides across files often (test helpers, `fn main` in bins, two trait impls on one type), so a file counts only when it gained the declaration since the read commit.
- Judged commits and declared files are cached per `Ledger` by commit; rows are read fresh so the lock recheck sees other writers.
- The `nate_style` test fixture has no origin, so a decision refresh in plan tests can fail with no error.

**Ruled out:**
- Merging `KeptForOpenBuild` into `KeptForBuild`: different outcome families.
- A `--to` override for a left finding: a person waives it or changes the code until a real case needs one.

### Phase 28 — a renamed item keeps its approved finding  · status: done

#### As-built

- New declaration outcomes in `style_ledger.py`: `DeclarationRenamed(finding, declaration, renamed, path, line)`, `RenameAwaitingApproval(finding)`, `DeclarationGoneFromFile(finding, declaration)`, and the private `NoRenamedSite()`. All but `NoRenamedSite` are in `DeclarationOutcome`. The alias `DeclarationLeft = DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationGoneFromFile` is the one set that `follow_totals`, the `follow` exit and the refill `left` dict use.
- `_declaration_outcome` tail order: several gaining files; one gaining file (another crate for a filed finding, else moved); for a `Filed` finding with `kind == "rename"`, the rename test `_renamed_site(...)`; then a `Filed` finding whose `finding.path in tip.blobs` gives `DeclarationGoneFromFile`; else `DeclarationRemoved` (unfiled, or file gone). The open-build check stays last over every outcome. The rename test takes the last `::` segment of `before` and `after`, needs `old_name == declaration.name`, a changed name and an `IDENTIFIER` match, and looks for `Declaration(keyword, new_name, owner)` gained since the read commit: in the finding's own file first, then in changed `.rs` files from `_files_naming`. Several files or another crate leave the finding, as a move does. `applied_check` at the site must not be `RenameNotApplied`. A decision that `approved` rejects gives `RenameAwaitingApproval`.
- `declaration_reading(ids, approved)` and `follow_declarations(ids, at: str, approved: Callable[[Path], bool])`. `follow_declarations` refuses with no write (`the synced tip moved from <at> since the declarations were read; rerun`) when the synced tip is no longer `at`, and reads `approved` again under `BEGIN IMMEDIATE`. A rename at its own file writes `state='built',person_read_by=''`; a rename into another file also writes the move columns, so `held list` shows `followed from …`. `DeclarationGoneFromFile` and `RenameAwaitingApproval` write nothing.
- `style_proposals.approved_decision(issue: Path) -> bool` is the `approved` callable. `follow` prints `built <site>: <keyword> <name> renamed to <new> in <path>:<line>` and `left <site>: <keyword> <name> declaration gone; waive it or declare it again`; `RenameAwaitingApproval` prints no line. Totals read `totals: follow <n>, built <n>, withdraw <n>, left <n>, kept <n>, close <n>`. Exit 1 on a left finding or a skipped decision; `file` prints the same lines and ignores left lines for its exit.
- `close_finished_decision` with no merged ticket: removed files give `nothing left to build: <files> not in synced commit <c>`; else any `Built` finding gives `built in synced commit <c>`; else `nothing left to build: every finding is waived`.
- Refill prints `waiting <decision>: no longer approved` for a `RenameAwaitingApproval` and `waiting <decision>: <finding> declaration gone` for a `DeclarationGoneFromFile` (not ticketed, still `Filed`), and closes a decision on `gone or withdrew or built`.

**Files:**
- `scripts/style/style_ledger.py` — the new outcomes, `DeclarationLeft`, `_renamed_site`, `_commit_file` cache, the follow transaction
- `scripts/style/style_proposals.py` — `run_follow`, `approved_decision`, `follow_line`, `follow_totals`, the close reason
- `scripts/style/style_plan.py` — refill waiting lines and close on built
- `scripts/style/test_style_ledger.py`, `test_style_proposals.py`, `test_style_plan.py`
- `docs/style_production.md`, `commands/showrunner/proposal.md` — built, left and withdrawn cases, totals shape, refill lines

**Gotchas:**
- The approval answer is read twice, at the plan and under the lock. A decision that loses approval between them gives `RenameAwaitingApproval`, never built.
- `applied_check` reads files through `_commit_file` (cache per commit and path) and file presence comes from `tip.blobs`, so the recheck under the lock runs no git.
- `style_ledger.py` cannot import `style_proposals`, so the approval check comes in as a callable.
- `reopen` does not change: a built bare rename has no applied check, so `reopen --all` lists it as `cannot check` and exits 1. A person passes `--applied <id> --by <who>`.
- `Built(proposal)` keeps the decision path. A built finding does not refresh its decision in the step; `complete` and refill close it.

**Ruled out:**
- `Ledger.mark_built`: it needs a merged build ticket, and these renames have none.
- `mark_built_at_merge`: `applied_check` is `NoAppliedCheck` for every bare pair, so most findings would need `--by`, and it runs outside the step's lock.
- Building a rename whose `after` name its file declared before the read commit: that is not an applied rename.

### Phase 29 — an approved proposal leaves the waiting count  · status: done

#### As-built

- The footer's `N proposals` and the `/showrunner:proposals` rows `waiting for your review` count only open proposals the user has not approved. The rule is the same for every proposal kind: plain proposals and style decisions.
- `proposal_issue.py` holds `IN_PROGRESS_STAGE = "active"`, `AwaitingApproval`, `InProgress`, `ProposalProgress = AwaitingApproval | InProgress` and `proposal_progress(issue) -> ProposalProgress`. `ApprovedDecision` gives `InProgress`. `UnansweredDecision` at `stage: active` gives `InProgress`. All else gives `AwaitingApproval`, also an open declined decision.
- Three writes set `stage: active`: `approve` (fields `showrunner, unit, plan, stage`), `create` with an inherited approval in `--initial-fields`, and `reopen` of an issue whose `decision_answer` is `ApprovedDecision`. `start` writes the same constant. `close` keeps the stage.
- States: waiting for approval is `open/backlog`; in progress is `open/active`; built is `closed`.
- `proposal_issue.py move-approved <folder> [--dry-run]` moves each open, tagged proposal with a recorded approval at `stage: backlog` to `stage: active`. It leaves `stage: waiting` alone. Each file has the outcome `Moved | LeftAlone | MoveRefused`. A refused file prints `refused` and the run exits 2. A folder that is not a directory exits 2. A rerun prints only `move-approved: 0 moved` and changes no byte or mtime. It runs by hand, never on a read.
- `proposals_table.issue_state` gives `waiting for your review`, `in progress by <unit>`, or `in progress` (`IN_PROGRESS_STATE`) when `unit` is empty. Waiting rows come first, then in-progress rows, each oldest first.

**Files:**
- `scripts/production/proposal_issue.py` — stage writes, `proposal_progress`, `move_approved`
- `scripts/production/footer_counts.py` — `proposals` counts `AwaitingApproval` only
- `scripts/production/proposals_table.py` — states and row order from `proposal_progress`
- `scripts/production/issues.py` — `PROPOSAL_TAG`; `proposals_folder.py` re-exports it
- `commands/showrunner/proposal.md`, `commands/showrunner/proposals.md`, `docs/style_production.md` — state table, approve sets active, `move-approved` usage
- `docs/as-built/showrunner-automation.md`, `docs/as-built/build-followups-fn-length-hook.md` — new states and count

**Binds later work:** Reopen of a built style decision (`ReopenBuiltDecision`) relies on `proposal_issue.py reopen` keeping the approval and setting `stage: active`. Readers count by the approval record, not only the stage, so the count is correct before and after `move-approved`.

**Gotchas:**
- `proposals_folder.py` is a leaf for the footer and must not import `proposal_issue`. `footer_counts` and `proposals_table` import `proposal_issue` directly.
- Every `proposal_issue` write, `move-approved` included, holds `_issue_lock` for its read, change and write, reads the file again under the lock, and writes only when bytes change. A file that changed after the scan is left alone.
- The issues folder is shared, so `move-approved` moves the approved proposals of every production.

**Ruled out:** a new stage value (the vault already ranks `active` as in progress); writing stages on read in the footer or table; changing `dailies_counts.py` `issues` counts or the style ledger's `proposals <n>` line.

### Phase 30 — a finding on a renamed item follows the rename  · status: done

#### As-built

- The declaration step follows unfiled and filed findings whose item, impl type or trait has a new name from built rename or type findings of approved decisions in the same crate. `DeclarationMoved` keeps the finding's id and state and records its new path, line and follow commit; a rerun reads the followed declaration in place.
- `ItemRename` carries `RenameScope = UnqualifiedName | ModuleQualified | SelfQualified | TypeQualified`; `BuiltRename` carries the pair, path and decision. `_item_rename` accepts rename pairs with an optional note after `;`, or type findings that change only the item name. Field forms give `NoItemRename`.
- `Ledger._built_renaming` returns `BuiltRenaming = RenamedByBuilds | NoBuiltRename`. Item and owner names follow successive approved renames, stopping before a fork or a return to an earlier name. A finding's own rename retains precedence; a filed method rename can become `DeclarationRenamed` and built at its renamed impl type, with the original owner checked first.
- Module qualifiers restrict module items to that module's files and reach impl items through a type declared there. `_module_name` handles `mod.rs` and maps `src/lib.rs` and `src/main.rs` to `crate`. Type qualifiers match impl or trait owners under any name in their rename chain; a built `Self::` qualifier resolves at the built finding's recorded line.
- `_gaining_files` requires the new declaration to be gained since the read commit and checks the finding's own file first. Built rows are collected once per declaration reading and read afresh with approvals under the write lock before changes persist.

**Files:**
- `scripts/style/style_ledger.py` — rename scopes, parsing, chains, gaining-file search and declaration follow/build outcomes
- `scripts/style/test_style_ledger.py` — follow persistence and reruns, chains, qualifiers, renamed owners, approval rechecks and move limits
- `docs/style_production.md` — declaration-follow rules and qualifier limits
- `commands/showrunner/proposal.md` — filing follows an item's new name after a built rename

**Binds later work:** `follow` and `file` expose renamed-item outcomes through their existing output lines. `_built_renaming`, `_renamed_site(..., after_builds, ...)` and `_followed_rename(..., RenamedByBuilds, ...)` separate rename resolution from destination search; `NoRenamedSite` retains its filed-rename meaning.

**Gotchas:**
- A trait-qualified path does not reach the items of the trait's impls — phase 33.
- `self::` and `super::` module paths are not resolved; such a rename names no item.
- Multiple gaining files, or a filed finding's destination in another crate, leave the finding for a person as a move does; an unfiled finding can follow across crates.
- The built-pair parser accepts notes after `;`; a filed rename's own applied-name check keeps its strict parse.

**Ruled out:** equality-based “no rename” checks and bool outcomes; named sum types distinguish the outcomes.

### Phase 31 — a closed approved decision takes a new finding  · status: done

#### As-built

- `join_action(ledger: style_ledger.Ledger, issue: Path) -> JoinAction` returns `ReopenBuiltDecision` for a closed approved decision with nothing left to build, whatever its close reason. Filing or migration reopens it with its approval, makes it active and files the later finding to it; filing refreshes its counts, examples and ranking in the same run.
- `left_to_build(findings: tuple[style_ledger.HeldFinding, ...]) -> tuple[style_ledger.HeldFinding, ...]` keeps every state except `Built` and `Waived`. Its callers pass `issue_findings`, which excludes withdrawn findings. `join_action` and `close_finished_decision` share this test.
- A closed decision with no answer reopens only when its reason starts with `built in ` or `nothing left to build: ` and nothing remains to build; it keeps no answer and stays in backlog. `as_built` alone does not qualify it. A declined decision returns `WaiveWithAnswer` before the closed-decision checks; an open decision returns `JoinOpenDecision` after body validation.
- Closed-decision validation checks for a missing approval before unbuilt findings, then calls `require_generated_body`. Refusals are `closed decision has no approval` or `closed decision has unbuilt findings`; `file` prints `skipped <decision>: <reason>` and exits 1, while migration prints `style_proposals: <decision>: <reason>` and exits 1 before any write.

**Files:**
- `scripts/style/style_proposals.py` — shared unfinished-findings test, ledger-aware join action and its filing, migration and completion callers
- `scripts/style/test_style_proposals.py` — seven cases covering hand-closed approved decisions, built/waived findings, filing and migration refusals, and unanswered withdrawal closes
- `docs/style_production.md` — reopening rules, refusal lines and migration behavior
- `commands/showrunner/proposal.md` — closing, later findings and recovery from an early close

**Binds later work:** `file_decision` and `plan_moves` pass the ledger to `join_action`; completion uses the same `left_to_build` rule. A closed decision's frontmatter alone cannot establish that its findings are resolved.

**Gotchas:**
- Join errors start with `<decision>: `; `skip_line` strips that prefix for filing, while migration retains it. Generated-body validation follows the state checks, and migration refuses before writes in both dry and real runs.
- A decision closed with a filed finding needs `proposal_issue.py reopen <decision>` before filing again; reopening preserves approval and sets an approved decision active.

**Ruled out:** automatically reopening a closed decision that still has a filed finding; it remains a refusal.

### Phase 32 — an old-text finding is withdrawn once judged again  · status: done

#### As-built

- `_changed_rule_findings` reads unfiled and filed findings whose rule text changed, excluding unfiled findings held by assigned or checkpointed tickets. `_superseded` selects the newest judgment at the current cell across the current and original rule blobs, ordered by `recorded_at` then id; it must use the current blob and postdate the finding's creation. Any verdict withdraws an unfiled finding; only `exception` withdraws a filed one. An unfinished build keeps its finding as `KeptForBuild`.
- `SupersedingJudgment(judgment_id, verdict, rule_blob)` carries the qualifying judgment. `JudgedAgain(judgment_id)` extends `WithdrawnBy`; a withdrawn row with no decision reads as `Withdrawn(NoDecision(), JudgedAgain(...))`, while an exception withdrawal with a decision retains `ExceptionVerdict`. Existing duplicate and declaration-gone reasons retain precedence; no migration is needed.
- Filing runs declaration follow, withdrawal, then batching. An unfiled withdrawal changes no decision and no longer prints the stale-rule skip line. Filing uses `superseded_line` to print `withdraw <id> <rule> <path>:<line>: judgment <row> <verdict> under <blob8>`; `held list` shows `judged again in judgment <row>`, and JSON retains `withdrawn_by`. Dry runs print the withdrawal lines and totals without writes; reruns find no further withdrawal.
- `withdraw_superseded` plans before `BEGIN IMMEDIATE`, rechecks the same eligibility and synced commit under the lock, and retries on change. Tests cover all verdicts, read-back, dry runs, restored text, open tickets, moved findings and unrelated-cell judgments.

**Files:**

- `scripts/style/style_ledger.py` — withdrawal reasons, judgment selection, follow timing guards, row decoding and JSON output
- `scripts/style/style_proposals.py` — verdict-bearing withdrawal lines and withdrawal-step documentation
- `scripts/style/test_style_ledger.py` — `JudgedAgain` read-back and text/JSON listing
- `scripts/style/test_style_proposals.py` — filing, withdrawal limits and moved-finding regressions
- `docs/style_production.md` — stale-text filing, withdrawal and held-list behavior
- `commands/showrunner/proposal.md` — filing withdraws unfiled findings judged again

**Binds later work:** A moved finding withdraws only by a judgment newer than its row's `updated_at`, in addition to the creation-time and current-cell checks. A judgment between its own judgment and its move, or before a later row edit, can therefore leave it in place; this conservative limit avoids closing a decision too soon. Refill still runs only declaration follow; the next filing runs withdrawal against the followed location.

**Gotchas:**

- Declaration following changes the cell used by the judgment query; an older judgment at the destination must not withdraw either an unfiled or a filed finding, whose withdrawal can close its decision.
- Original-text judgments at other cells, even from the same ticket, do not order this cell and must not block its withdrawal. The newest-row comparison stays local to the current cell and preserves restored-text protection.
- Creation time alone cannot order a finding against a later re-judgment of its original cell. `followed_at` stores a commit, not a timestamp; `updated_at` and judgment `recorded_at` share UTC ISO timestamps, so the follow guard compares those instead.

**Ruled out:** ticket-wide original-judgment guards; timestamp comparisons against commit-valued `followed_at`; a verdict-column migration for withdrawal reasons already distinguished by decision presence.

### Phase 33 — a trait-qualified rename follows to the trait's impl methods  · status: done

**Depends on:** Phase 30, Phase 32. Run after Phase 32 to avoid concurrent ownership of `style_ledger.py` and its tests.

#### Work Order

**Goal:** A finding on a method renamed by a built, approved `Trait::old` → `Trait::new` finding follows the corresponding method of each implementing type. It keeps its id and existing filed or unfiled state, and records its follow location.

**Spec:**

All references below are at HEAD `e7a45401`; use the named functions after intervening phases move their lines.

1. **Read the implemented trait without replacing the implementing type.**

   `scripts/style/declarations.py` currently represents both inherent and trait-implementation members as `ImplOrTraitItem(type_name)` (:25-31). `_impl_name` (:106-126) identifies a top-level `for`, but discards the preceding trait path; `read_declarations` (:170-202) consequently records no implemented-trait relationship.

   Add immutable declaration metadata with explicit states:

   - `ImplementedTrait(path: tuple[str, ...])`: the readable, nonempty trait path of the enclosing trait implementation.
   - `NoImplementedTrait`: the item is not directly owned by a trait implementation.
   - `UnreadableImplementedTrait`: an enclosing trait implementation exists, but its trait path cannot be read safely.

   Store this metadata on `DeclaredItem` (:51-57), separately from `Declaration.owner`. Preserve `Declaration("fn", "method", ImplOrTraitItem("Type"))`, `declaration_line`, and existing output formats.

   Factor the header splitting used by `_impl_name` so the same parse identifies the implementing type and implemented trait. Skip leading impl generic parameters; distinguish the trait/type separator from `for<'a>`; use `approved_renames.type_path` (:235-242) for readable paths. `approved_renames.impl_blocks` (:269-312) demonstrates this distinction, but is an applied-rename scan with additional constraints, not a replacement declaration reader.

   Attach metadata only to directly owned members. A nested function inside a method must not inherit the implementation’s membership. Preserve current behavior for unreadable implementing types.

2. **Match trait-qualified built renames to implementation members.**

   Keep `_item_rename` (`style_ledger.py:689-717`) and the existing `RenameScope` forms. `Trait::old` remains `TypeQualified("Trait")`; the declaration reader supplies the relationship needed to interpret it.

   Change `_built_renaming` (:2370-2432) to receive the original `DeclaredItem`, rather than only its `Declaration`; update its caller at :2315. In addition to the current owner-name match, permit a type-qualified member rename when its qualifier matches the member’s readable implemented trait.

   Keep the destination owner as the implementing type, including any name Phase 30’s owner-rename chain gave that type. A `Trait::old` rename must not turn `ImplOrTraitItem("Type")` into `ImplOrTraitItem("Trait")`.

   Preserve the existing approved-decision and same-crate filters, own-rename exclusion, chain traversal, fork stopping and cycle stopping. If an approved built rename also renamed the trait, follow its readable name history using the existing scope restrictions; do not infer a trait relationship from unrelated occurrences of its name.

3. **Carry the relationship through declaration searches.**

   `_declaration_outcome` currently tests in-place and moved declarations by keyword/name/owner (:2290-2304). `_gaining_files` (:2451-2472) uses that same identity and selects the first matching item. Merely adding a branch to `_built_renaming` would therefore be insufficient.

   For trait-implementation members, use the recorded implemented trait when checking the old declaration and identifying a renamed destination. An inherent method or another trait’s method on the same type is not the same declaration.

   Carry any renamed-trait requirement through `RenamedByBuilds`, `_followed_rename` (:2434-2449), and the relevant `_renamed_site` searches (:2327-2368). Represent unrestricted versus required trait membership with named variants, not `None`, an empty string, or a boolean.

   Preserve own-file preference only among matching implementation members. Keep the existing gained-since-read check, changed-file restriction, crate checks and ambiguity handling; never resolve an ambiguous implementation match by taking the first item. Unsupported or ambiguous trait identification grants no new automatic follow.

4. **Preserve collection, transitions and output.**

   Keep built-rename collection in `_declaration_reading` (:2474-2492), including resolution of `SelfQualified` at the built finding’s read or follow commit. `Self::` in an inherent implementation must not become a rename across every implementation of a trait.

   A dependent finding uses `DeclarationMoved` and remains filed or unfiled. A filed finding describing its own completed rename still uses `_renamed_site` and `DeclarationRenamed`, subject to its existing approval and applied checks.

   Retain open-ticket protection, planning before `BEGIN IMMEDIATE`, rechecking decisions and rows under the lock, synced-tip refusal, caching and idempotence. No schema migration is needed.

   Existing consumers observe the result through `follow` and `file`, using `follow_line` (`style_proposals.py:1331-1351` at HEAD), and through `held list` follow metadata. Preserve their exit-code and line contracts.

5. **Document the changed limit.**

   Replace the statement at `docs/style_production.md:478` that a trait path does not reach implementation items with the supported trait-qualified behavior and its conservative parsing limits. Retain the separate `self::`/`super::` limitation.

   Phase 30’s successful-follow outcome already has an external surface and tests; this phase extends that outcome rather than adding another command. Unresolved paths remain subject to the existing declaration outcomes and person-facing recovery instructions.

**Files:**

- `scripts/style/declarations.py` — implemented-trait metadata and header reading.
- `scripts/style/style_ledger.py` — trait-aware rename matching and declaration searches.
- `scripts/style/test_declarations.py` — parser and ownership tests.
- `scripts/style/test_style_ledger.py` — follow, isolation and transition tests.
- `scripts/style/test_style_proposals.py` — observable dry-run and real-run behavior.
- `docs/style_production.md` — replace the trait-path limitation.

**Workers:** `1 writer + 1 tester`.

- `impl` — `declarations.py`, `style_ledger.py`, `docs/style_production.md`; hubs: `declarations.py` owns the metadata types, and `style_ledger.py` owns their matching semantics.
- `test` — `test_declarations.py`, `test_style_ledger.py`, `test_style_proposals.py`, from the Spec. Keep all preceding phases’ tests.

**Constraints from prior phases:**

- Phase 30’s `RenameScope`, `ItemRename`, `BuiltRename`, `RenamedByBuilds` and `NoBuiltRename` remain semantic states. Extend them only where the new relationship needs to travel.
- Do not replace an implementation’s owner with its trait, or broadly change `Declaration` equality to achieve the new match.
- Preserve Phase 31’s in-progress proposal changes and Phase 32’s withdrawal changes. Neither phase is redundant or invalidated by Phase 30.
- Phase 32 (done, `d0a3f9ec`) renamed `SupersedingException` to `SupersedingJudgment` (it carries the verdict) and added `JudgedAgain`. `_superseded` withdraws an unfiled finding on any newer verdict at its current cell and a filed one only on an exception, and the judgment must be newer than the finding's `created_at`; for a followed finding (`followed_at` set; it holds a commit) it must also be newer than the row's `updated_at`. A follow that this phase adds writes `updated_at` in the same update, which keeps that bound correct; keep it.
- `file` runs declaration following before withdrawal and batching. Refill runs declaration following only; preserve that ordering.
- `style_ledger.py` must not import `style_proposals`.
- No compiler, dependency resolution or general Rust name-resolution service is introduced. Unsupported aliases and ambiguous paths must not establish trait membership.
- The next-items entries remain separate, unsatisfied work; none requires amendment because of Phase 30 or this phase.

**Acceptance gate:**

Write behavior tests first, using temporary repositories and isolated ledgers. Reuse `commit_tip` (:861), `hold_parent` (:872), `judge_parent` (:887), `follow_approved` (:896) and `built_finding` (:1192) in `test_style_ledger.py`.

Required cases:

1. The declaration reader distinguishes inherent members, trait-body members and trait-implementation members while preserving existing owner declarations. Include qualified trait paths, generic impl headers, higher-ranked bounds and nested functions.
2. A filed restructure finding inside `impl Trait for Type` follows `Trait::old` → `Trait::new`, keeps its id and decision, and records the exact follow commit and line.
3. Two implementing types follow independently, including implementations in different files of the same crate.
4. An unfiled dependent finding follows without being withdrawn; a second run changes nothing.
5. A matching inherent method and another trait’s same-named method neither conceal the source’s removal nor receive the destination finding. Test both source and destination collisions.
6. A renamed implementing type, and a readable renamed trait, compose with the method rename.
7. A destination that already existed at the read commit is not treated as newly gained. Ambiguous destinations are not selected arbitrarily.
8. Unapproved built findings, built findings from another crate, forks, cycles and open build tickets preserve their existing protections. Losing approval before the lock prevents the write.
9. `follow --dry-run` prints the expected `follow … fn Type::new` line and writes nothing; the real run prints that action and persists it. A rerun is idempotent.
10. Existing filed-own-rename behavior still builds only an approved, applied rename.

The new successful trait-follow tests must fail on `e7a45401` because the finding does not follow. Collision tests must exercise competing declarations, rather than merely checking an absent match.

From the worktree root `<W>`:

```bash
env -C <W> PYTHONPATH=<W>/scripts/style:<W>/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_declarations
env -C <W> PYTHONPATH=<W>/scripts/style:<W>/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_style_proposals scripts.style.test_style_plan scripts.style.test_style_ledger
basedpyright scripts/style
```

Both unittest commands pass; basedpyright reports `0 errors, 0 warnings`. All verification uses temporary fixtures and touches no live repository, proposal store or ledger.

### Phase 34 — a finding follows a rename made outside the ledger  · status: todo

**Depends on:** Phase 30, Phase 32, Phase 33

#### Work Order

**Goal:** The declaration step follows a filed or unfiled finding through a proved item or owner rename made outside the ledger. It keeps the finding and its decision. A same-crate cross-file rename may also follow through one newly gaining file. An uncertain match leaves the row and names the mapped declaration and candidates.

**Spec:**

The showrunner ruled this current on 2026-10-20, after Phase 33. A wrong follow puts a decision on code it was not made for. A missed follow only leaves the row. No manual command is added. Read the working tree after the dependencies; references below may move while Phase 32 workers edit it. The current ruling includes a rename into another file of the same crate. The separate cross-crate phase was dropped; it gives this phase no cross-crate authority.

1. **Prove an outside rename in the declaration step.** Extend `Ledger._declaration_outcome` in `scripts/style/style_ledger.py` (:2292 in the researched tree). Use the finding's last follow commit, else `judged_commit`, as today. When its old declaration is gone, permit a rename without a built decision only when all these checks hold:
   - (a) The old name is declared nowhere in the finding's crate at the synced tip. Search declarations, not name occurrences in calls, comments or strings. Read all tracked Rust files in that crate, including test items and other modules. A same-named declaration elsewhere prevents this proof; do not narrow this check to the original module or owner. The current `RepositoryTip.crate` supplies the crate boundary. A declaration in another crate does not, by itself, fail this check.
   - (b) For a rename in the same file, the line to which the source diff maps the finding's site holds a declaration with the same keyword and owner, under a different name. Keep Phase 33's implemented-trait identity as well. Establish this mapping before choosing the renamed declaration; `moved_site_line` cannot itself prove a target selected in advance. A rename into another file instead uses the gained-file proof below; a Git hunk mapping into that other file is not required.
   - (c) At least half of the old body's non-blank lines are unchanged after its old name is replaced by its new name. Count preserved lines once, in order, with their multiplicity. A repeated brace or line must not credit several old lines against one new line. The threshold is inclusive: `2 * unchanged >= old_nonblank_count`.
   - **Cross-file proof in the same crate:** When the mapped-site proof does not clear the row, inspect every changed tracked Rust file in the finding's crate. A target must have a new name, the same keyword and owner kind, a body that passes (c), and a declaration identity that its file did not have at the judged/last-follow commit. The target file must therefore have gained that declaration, rather than merely editing a pre-existing twin. Include the original file in the whole-crate count; give it no priority. A `GainedFileProof` must name a path different from the source path, exactly one file may qualify, and its destination declaration/child site must be unambiguous. Counting the original file can expose ambiguity; it cannot bypass the mapped-site requirement for a rename that stays there. Two passing declarations in one file also need a unique proved item; file cardinality alone cannot pick one.
   - For impl or trait members, matching the owner kind is only the first filter. Keep an unchanged owner identity and Phase 33 trait metadata, or prove the changed owner by the owner rule below. Never treat two impl types as one merely because both are impl owners.
   - Count files after the keyword, owner, gain and body checks. Keep examined candidates and their failed checks for diagnostics too. With zero or two or more passing files, a failed body check or uncertain evidence, leave the row and name the candidates; never choose the best score or first file. No candidate in another crate can qualify through this new rule. Do not use `before`/`after` prose as rename proof.

2. **Define the site and body evidence.** Put reusable Rust source reading and line mapping in `scripts/style/declarations.py`, beside `read_declarations` and `moved_site_line`; ledger state stays in `style_ledger.py`.
   - Read a deterministic zero-context source diff from the read commit to the synced tip. Map an unchanged source line by the hunk offsets. A replacement hunk may supply a bounded candidate line for inspection; it does not establish item identity on its own. A deletion supplies only a diagnostic boundary, not a destination proof. If the mapping has several plausible owning declarations, is outside the destination, or is unreadable, leave it unresolved. A uniquely mapped file rename may supply a path. The independent same-crate gained-file proof may also supply one. This phase gives no new cross-crate authority; retain prior cross-crate and approved-move rules.
   - `moved_site_line` currently needs both items before it runs and then searches equal stripped site text, else returns the new keyword line. It is a final site locator, not an independent rename detector. Use it only after the mapped-site or unique gained-file proof, to carry a finding inside the proved destination; a child still needs its own binding/owner proof. A row on an attribute or item line may use the corresponding first-owned/keyword line. Share an independent mapping helper where needed.
   - For this draft, body means the source inside the item's own delimiters. Exclude its attributes, docs before the item, signature and outer opening/closing braces. Count comments inside the body, nested source and ordinary whitespace exactly; discard only blank lines and line-ending differences. Replace the old identifier by the new identifier at identifier boundaries before comparing. Do not normalize changed literals, callees, other names or indentation into equal lines. In particular, a one-line function's header and closing brace cannot turn a changed sole body line into a passing score. Keep source fragments for a body on its header line, rather than treating it as empty.
   - An empty body, a declaration with no readable body, an unsupported binding or an ambiguous owner supplies no proof. For a constant/type alias with only an initializer or a semicolon, do not infer a braced body from a nearby function. These are conservative limits chosen here to implement the ruling's doubt rule.
   - Use named immutable states: `OutsideRenameProven` carries the new declaration, path, site line and `MappedSiteProof | GainedFileProof`; `OutsideRenameNotProven` carries mapped-site evidence, candidate evidence and failed or uncertain checks. Use `NoOutsideCandidates | OutsideCandidates` for the candidate set; the latter holds a non-empty, sorted set of paths/items, gain evidence and body counts. Distinguish zero passing files, several passing files, a failed body and ambiguous items with named reasons. Represent mapped-site evidence as `MappedDeclaration(path, line, declaration) | MappedNoDeclaration(path, line) | UnmappedSite(reason)`. Failure reasons form a closed named set. Do not encode these outcomes with a bool, `None`, an empty string or an optional field whose absence has domain meaning.

3. **Follow a child only through a proved owner.** A child row is a method, associated item or local binding inside a function. Its own unchanged name must still name that child inside the renamed owner. Apply the body test to the owner that was renamed, not to the local binding's line.
   - Extend declaration metadata with the lexical owner relationship and readable body spans needed for this proof, while keeping `Declaration.owner` and Phase 33's trait metadata intact. The reader currently returns a containing function for a parameter, `let` or closure-parameter line; it does not return local bindings. Add a narrow binding read sufficient to confirm a child's own name and scope, including the `let rows` and `move |answers|` forms in the live evidence. A token use or a field projection is not a binding declaration. Leave unsupported patterns unresolved.
   - A function owner must pass (a), (c), and either the independent mapped-site proof or the unique same-crate gained-file proof under its new name. The owner may move to another file; resolve retained children inside that proved destination, not in an arbitrary matching file. A renamed impl type or trait must pass its own proof before a member can inherit that owner change. An old owner type that still exists is not renamed just because a method now sits under another type. If both owner and member change names, prove both links; apply the relevant body check at each renamed owner/item. Resolve an unchanged child by its own name within the proved new owner and its mapped lexical scope. Duplicate or shadowed names do not permit an arbitrary first match.
   - Local bindings do not become ordinary entries in the existing declared-item identity search. Preserve existing `declaration_at` behavior used by moves, Phase 30 and Phase 33. Keep the additional binding/lexical data separate. Do not start reading previously unread module comments, `use` items or impl headers as new findings.

4. **Preserve the established transitions.** Same-identity moves, an approved applied filed finding's own completed rename, and Phase 30/33 approved built-rename follows retain their existing precedence. `_renamed_site` still uses `DeclarationRenamed` to mark an approved applied own rename built. Its approval wait is not an approval from this new proof. An outside follow itself never marks a finding built, approves a decision or rewrites its requested change.
   - After those existing paths find no authoritative result, an `OutsideRenameProven` becomes `DeclarationMoved` with the new declaration and site. No new public successful outcome is needed; expand that type's docstring to name the outside proof. Keep the finding id, rule/blob, kind, text, before/after, ticket, proposal, state and person-reading history. Write path, line and the existing `followed_from`, `followed_from_line`, `followed_at` fields. No migration or stored crate column is needed.
   - An open judging ticket still prevents reading an unfiled finding. An open build ticket still keeps the row. Do not use outside proof to bypass an existing ambiguous-move or cross-crate refusal. Attach mapped-site and candidate diagnostics to that left outcome without changing its precedence. Preserve an existing several-files or cross-crate refusal even if a new outside candidate looks attractive.
   - The widened ruling says that a refused cross-file rename leaves the row. After old-name absence is established and the outside search is attempted, zero/multiple candidates or a failed/uncertain body must therefore return a new `OutsideRenameUnresolved(finding, declaration, evidence)` left outcome. Keep its filed or unfiled state even if its source file disappeared. This explicitly replaces today's `DeclarationRemoved` fallthrough for these search refusals; do not withdraw them or let refill waive them. For paths not entering this new search, preserve current declaration-removal transitions. Add the new type to `DeclarationOutcome`, `DeclarationLeft` and every consumer union/match. Keep `DeclaredInSeveralFiles` and `DeclaredInAnotherCrate` where they already apply, and keep the existing filed `DeclarationGoneFromFile` outcome when the outside proof is blocked before the gained-file search. Carry mapped/candidate evidence on existing left/removal outcomes where the outside check read them. Do not overload `NoRenamedSite`, whose existing meaning is a filed-own-rename check. The new type names a refused outside rename, not a completed rename or a built finding.
   - Keep planning before `BEGIN IMMEDIATE`, the fresh row/decision recheck under the lock, synced-tip refusal, `retry_on_change` and Git-error refusal before any write. Cache parsed sources, source diffs and crate-name scans by the read/tip/path or tip/crate/name they use, so many findings in one owner do not repeat those scans. Reruns read a followed row at its follow commit and change nothing.

5. **Expose the proof and the reason it failed through existing consumers.** A successful mapped-site or same-crate cross-file outside rename uses the existing `follow <id> <rule> <old path>:<old line> -> <path>:<line>: <keyword> <new name>` line and increments `follow`, not `built`. `held list` uses existing follow provenance. Retain exit codes and totals.
   - Append `; mapped site <path>:<line> declares <keyword> <qualified name>; outside rename not proved: <checks>` to an existing `left` line when it has a mapped declaration. Name no declaration or an unresolved mapping explicitly for the other evidence variants. Preserve the existing reason and recovery instruction before that suffix. The two `draw` rows must still name their several gaining files and also the mapped `fn draw_sorted`.
   - `OutsideRenameUnresolved` prints `left <id> <rule> <path>:<line>: outside rename not proved: <reason>; candidates: <sorted candidate paths/declarations and unchanged/old body counts>`, followed by mapped-site evidence when available. With no examined target print `candidates: none`; do not omit a zero-candidate result. Distinguish examined-but-failed candidates from passing candidates. List every candidate file deterministically and name each failed check. These outcomes increment `left`, never `withdrawn` or `built`. A removal outside this new search keeps its existing withdrawal line/state, with mapped evidence if read. An existing approval-wait outcome retains its silent wait.
   - Update `follow_line` in `scripts/style/style_proposals.py` and the waiting lines in `scripts/style/style_plan.py` so refill names the mapped declaration and candidate files too. Extend `DeclarationChange`, `DeclarationLeft`, `follow_totals`, close/refill filtering and their exhaustive matches for `OutsideRenameUnresolved`; unresolved rows must not close a decision or enter fallback site-text waives. `run_follow` already validates open decisions, refreshes followed members and respects stored proposal membership; reuse it. No new command, approval source or decision file is added. `file` still follows before withdrawal and batching; refill follows before ticket selection.
   - Update the declaration-follow section of `docs/style_production.md` and the filing procedure in `commands/showrunner/proposal.md` with both proofs, the child rule, gained-file cardinality, refusal retention and candidate lines. Retain Phase 32's withdrawal text and Phase 33's trait support and path limits.

6. **Use the verified result for all 22 live rows, not the rename descriptions.** At synced tip `19da700c6ff026c7be79b9cff9d638543a50b0dc`, this rule newly follows exactly `5855309d6998e256` and `e4c8fa3ac1ae2891`, both in `crates/cargo-handler/src/census/schedule.rs`, to lines 1427 and 1439. They remain filed to the same `style prefer-type-named-fields-and-bindings crates+cargo-handler d00b0104.md` decision. Their owner changes from `uncached_completed_rows_arrive_before_hierarchy_discovery_is_released` to `uncached_first_publication_respects_its_budget_and_holds_claude_rows`; the child names `rows` and `answers` remain. The old owner's body retains 43 of 52 non-blank lines.
   - The other 17 original rows must not follow under this rule. In particular, `renders_primary_marker` was removed and its logic absorbed into an already-existing `worktree_entry_label`; `device_line` changed its sole body line; the CPU test retains only 3 of 8 body lines; the account names remain elsewhere in the crate; and `LocalAnswers` still exists. The research table below names each check and mapped site.
   - The three added modules rows also do not follow at this tip: `fff079fb7af8cf21` and `72fc92aca6fc9706` have one relevant gained struct, `CratesIoRelease` in `crates/cargo-port/src/project/cargo/crates_io_release.rs:4`, but only 3/7 old non-blank body lines remain. All three field lines match; all four old internal doc-comment lines changed. Do not silently ignore those lines to turn 3/7 into 3/3. `62ca205b24b40dc6` has a mapped `enum CratesIoSection` in `crates/cargo-port/src/tui/panes/pane_data/detail_data.rs:484`, which changes keyword and preserves 0/9 body lines; no gained struct passes instead. These are body/identity limits, not proof of a cross-crate move.
   - Repeat the read-only audit after Phases 32/33 and at the current synced tip before declaring the live gate met. Report any changed result by id and evidence. Do not apply a live follow as a test of this phase.

**Files:**

- `scripts/style/declarations.py` — independent line-mapping evidence, lexical owner/binding reads and readable body spans; preserve Phase 33 metadata.
- `scripts/style/style_ledger.py` — mapped-site and gained-file proofs, crate-wide name scan, explicit unresolved outcome, candidate diagnostics and follow persistence.
- `scripts/style/style_proposals.py` — follow/left unions, candidate lines and left totals; preserve proposal refresh and close behavior for unresolved rows.
- `scripts/style/style_plan.py` — mapped-declaration waiting text and existing refill integration.
- `scripts/style/test_declarations.py` — mapping, body and lexical child tests.
- `scripts/style/test_style_ledger.py` — mapped/cross-file proof, gained-file uniqueness, owner follow, conservative refusal retention and persistence tests in temporary two-crate repositories.
- `scripts/style/test_style_proposals.py` — dry/real follow and file output, decision preservation and refresh tests.
- `scripts/style/test_style_plan.py` — refill observes the followed site and waits with mapped evidence on a failed proof.
- `scripts/style/approved_renames.py` — read existing item/header helpers; no new applied-rename authority is added here.
- `scripts/style/style_footprint.py` — read tokenizer, token lines and brace matching used by the declaration reader; change only if the source-span defect is in that shared helper.
- `docs/style_production.md` — outside-rename rule, child proof, conservative limits and output suffix.
- `commands/showrunner/proposal.md` — filing's automatic outside-rename follow and unresolved-site output.

**Workers:** `1 writer + 1 tester`. One writer owns the declaration model and its ledger/CLI consumers; the tester writes behavior tests from this Spec in separate files.

- `impl` — `scripts/style/declarations.py`, `scripts/style/style_ledger.py`, `scripts/style/style_proposals.py`, `scripts/style/style_plan.py`, `docs/style_production.md`, `commands/showrunner/proposal.md`; also the named shared helper files if the span fix belongs there. Hubs: `scripts/style/declarations.py` owns source/proof metadata; `scripts/style/style_ledger.py` owns outcome transitions. Each has one writer.
- `test` — `scripts/style/test_declarations.py`, `scripts/style/test_style_ledger.py`, `scripts/style/test_style_proposals.py`, `scripts/style/test_style_plan.py`. Reuse `commit_tip`, `hold_parent`, `judge_parent`, `follow_approved`, `built_finding` and the existing two-crate fixtures in ledger tests; `judge`, `follow`, `held_list` in proposal tests; `judged_findings` and `refill_decisions` in plan tests. Test names below state the behavior. Keep dependency coverage; change old assertions only where the new successful follows or the widened refusal-retention rule explicitly change that behavior. Name those changed expectations in the implementation summary.

**Constraints from prior phases:**

- Phase 30 separates rename resolution from destination search with `_built_renaming`, `_renamed_site`, `_followed_rename` and `_gaining_files`. Its built rename pairs, scope checks, approval checks, chains, fork/cycle stops and gained-since-read checks keep their meaning. The new proof has no built pair and must not manufacture one.
- Phase 33 keeps an impl member's owner as the implementing type and stores implemented-trait metadata separately. Preserve this distinction in source and destination matching. Its unsupported and ambiguous trait paths grant no proof. `self::`/`super::` module resolution is not added here.
- Phase 32's current-text judgment withdrawal remains separate and runs after declaration follow during filing. Phase 31's decision membership, unfinished-findings test and close/reopen rules remain unchanged. An outside follow is neither completion nor a new decision.
- The showrunner requires no manual command and conservative proof. Its latest widening permits newly gained destinations inside the same crate only, and explicitly keeps rows when that search fails. The dropped cross-crate phase supplies no authority. Phase 30's `_gaining_files` requires a known target identity and gives the original file priority; the new discovery search must neither require a known after name nor reuse that priority to hide a second passing file. Keep old approved-rename callers unchanged.
- The Type Design Contract requires closed named outcomes. Keep source mapping, proof failure and readable/unreadable body or owner states explicit; do not add an optional domain field or bool that a reader must interpret. `style_ledger.py` must not import `style_proposals`.
- All implementation verification uses isolated temporary repositories, ledgers and decision stores. No live repository, proposal, ledger, rule store or style session is edited by these gates.

**Acceptance gate:**

Write these eight behavior tests (subtests may cover the named cases). A new positive test must fail before this change; a retained negative case is a limit, not evidence that the feature was already present.

1. `test_outside_rename_source_evidence_excludes_headers_and_reads_local_children` (`test_declarations.py`) — map unchanged and replaced site lines independently of a chosen target; read function/type/trait owners, `let rows` and closure parameter `answers`; extract real bodies, including a body on a signature line. Header/attribute/outer-brace matches must not inflate a 0-of-1 body. Deleted, ambiguous and unsupported sites return named unresolved states. This exercises the source metadata used by the follow, not imports or class names.
2. `test_a_filed_and_an_unfiled_finding_follow_a_proven_outside_rename` (`test_style_ledger.py`) — no built rename rows; subtests for a same-file mapped rename and a rename into one newly gaining file of the same crate, each with exactly half of four old body lines preserved, follow both states. Include a second file that already had a twin at the read commit; it is not gained and must not create ambiguity. Check exact id, path, site, state, decision, before/after and follow commit after reopening the isolated database. A rerun writes nothing. Include a source-line replacement so this does not pass only for unchanged site text.
3. `test_an_outside_rename_needs_every_check_and_reports_its_mapped_declaration` (`test_style_ledger.py`, proof and limit cases) — subtests for old name still declared elsewhere in the same crate, old name only used but not declared, changed keyword, wrong owner/implemented trait, 1-of-3 body lines, empty/unreadable body, repeated-line overcount, deleted site and ambiguous mapping. The name in a different crate alone must not block a proved same-crate destination. Add cross-file cases with zero passing files, two passing files, two plausible items in one file, an unchanged pre-existing destination, and a destination gained only in another crate. After old-name absence permits this search, assert `OutsideRenameUnresolved`, unchanged filed/unfiled state (also when the source file is deleted), candidate paths/body counts and unchanged database rows. Keep existing earlier ambiguous-move outcomes. A refused search must not fall through to withdrawal. Assert mapped/candidate evidence, not merely a non-follow.
4. `test_children_follow_only_when_their_renamed_owner_is_proven` (`test_style_ledger.py`) — a method and associated item under a renamed impl type/trait, and `let`/closure children under a renamed function, including an owner newly gained in another file, follow only through proved owners and retained child names. Use a real passing owner body. Limits: owner type still declared, child gone or renamed, and competing same-name child scopes. Include the two schedule-test binding forms and exact site relocation. Do not claim a call-use token is a retained binding.
5. `test_outside_follow_preserves_existing_follow_authority_and_write_rechecks` (`test_style_ledger.py`, mixed success/limits) — retain approved applied own-rename builds and approval waits, built-rename/trait-qualified follows, ambiguous moves, open judging/build tickets and cross-crate limits. Put the new outside proof through the existing synced-tip and changed-before-lock tests; a row or source-tip change must prevent a stale follow. Reuse those existing tests where that avoids duplicating their setup.
6. `test_follow_and_file_keep_the_decision_after_an_outside_rename` (`test_style_proposals.py`) — real temporary-repository rename; `follow --dry-run` prints the same follow as the real run but writes nothing. The real run keeps the proposal, refreshes its example site, counts follow rather than built, and `held list` shows provenance. Check the mapped-declaration/candidate line for a failed body check and for two passing files; both leave rows and decisions open, count `left` and retain existing totals/exit codes. `file` performs the same follow before its other steps; a rerun is idempotent.
7. `test_refill_uses_an_outside_follow_and_names_an_unproved_mapped_declaration` (`test_style_plan.py`) — a successful proof tickets the current site and original finding id under its original decision; a failed cross-file proof waits with mapped declaration/candidate paths and is not withdrawn, waived by the fallback site-text heuristic, or treated as a reason to close its decision. Exercise both through real fixture commits.
8. `test_live_outside_rename_examples_follow_only_the_two_retained_children` (`test_style_ledger.py`) — small faithful source fixtures for the live classes of evidence: the schedule owner retaining 43/52 body lines with `rows`/`answers`; the account names still declared elsewhere; retained `LocalAnswers`; deleted `renders_primary_marker` beside pre-existing `worktree_entry_label`; `Device` to `Total` with 0/1; changed CPU-test assertions with 3/8; `CratesIoInfo -> CratesIoRelease` in a gained file with unchanged fields but 3/7 source body lines; and `CratesIoFields -> CratesIoSection` with a changed keyword and 0/9. Expect only the two child follows, with others' mapped-site diagnostics. Do not mock the proof or copy its implementation into the assertions.

Run from worktree `<W>` without changing directories:

```bash
env -C <W> PYTHONPATH=<W>/scripts/style:<W>/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest scripts.style.test_style_ledger scripts.style.test_style_proposals scripts.style.test_style_plan scripts.style.test_declarations
env -C <W> basedpyright scripts/style
```

The suites pass and basedpyright reports `0 errors, 0 warnings`. The known missing-venv exit status does not replace that diagnostic requirement.

Observable live result at the researched synced tip: newly follow exactly `5855309d6998e256` from `crates/cargo-handler/src/census/schedule.rs:818` to `:1427`, and `e4c8fa3ac1ae2891` from the same file `:825` to `:1439`, retaining their filed state and decision. There are no other newly eligible live rows at this snapshot. The other 20 evidence rows do not follow. Keep existing early refusal outcomes; rows that enter and fail the new cross-file search use `OutsideRenameUnresolved` and remain filed. In particular, do not report the two `CratesIoInfo` rows as successes under this body definition. A read-only audit after dependencies must show these exact ids/destinations or name changes in tip, source or state that explain a different result. Do not run a writing live command to meet this gate.
