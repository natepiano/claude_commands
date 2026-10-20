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
- `scripts/delegate/verify.sh` — `lint --workspace --check`
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
- `commands/lint_config.md`, `scripts/lint/*`, `scripts/delegate/verify.sh` — consumers without the fix pipeline
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
- A built finding whose decision file is missing is skipped without a line.
