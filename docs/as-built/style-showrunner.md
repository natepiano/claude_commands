# Style showrunner

## What it is

A style production is a standing child production named `<repo>-style`. A repo's main showrunner starts it with `/showrunner:add_style`. It has four family units (`<style slug>-names-unit`, `<style slug>-values-unit`, `<style slug>-types-unit`, `<style slug>-modules-unit`). They sweep the repo against every style rule in `~/rust/nate_style` and the repo's local rule folders. A per-repo sqlite ledger records which (rule, file or crate) cells are covered at the synced branch-point tip. Units apply mechanical fixes. They hold every other change as a finding, and the style showrunner files findings as style decisions. The style showrunner merges unit work into a style merge branch and hands each style-branch tip back to the parent, which merges it into the branch point. The style production replaced the old fix pipeline (`scripts/fix/`, `/fix`, `/style_eval`, `/style_fix_review`, `/style_usage`, `/nightly_weights`), which is deleted. The runtime procedure the style showrunner follows is `docs/style_production.md`. That doc takes precedence over `commands/showrunner/produce.md` for a `**Kind:** style` production.

## How it works

### Key files

| File | Role |
| --- | --- |
| `scripts/style/rules.py` | Rule catalog: `Rule`, `load_rules`, rule ids, reach, `workspace_members`, `style_family`, `list` CLI |
| `scripts/style/style_rules.py` | Frontmatter reading, `NATE_STYLE_DIR`, `HISTORY_DIR`, `Unit`, `focused-eval` CLI |
| `scripts/style/candidate_generators.py` | `CandidatesSpec`, 15 generator kinds, `enumerate_candidates` (no cap) |
| `scripts/style/style_ledger.py` | The ledger: cells, judgments, lint runs, tickets, hand-offs, held findings, declaration follow, CLI |
| `scripts/style/style_plan.py` | Refill (`next-phase`), ticket gate (`gate`), `queue`, ticket selection, phase text |
| `scripts/style/style_footprint.py` | Mechanical-fix filter (`check`), lexical Rust tokenizer |
| `scripts/style/style_sync.py` | `notify`, `lint`, `resume`, `send-handoff`, `wake-waiters` |
| `scripts/style/add_style.py` | Bootstrap and reattach; `standing_style_production` lookup |
| `scripts/style/style_proposals.py` | Style decisions: `file`, `follow`, `withdraw`, `complete`, `decline`, `unwaive`, `reopen`, `migrate`, more |
| `scripts/style/approved_renames.py` | `applied_check`, rename pairs, `FileRename` |
| `scripts/style/declarations.py` | Rust item reading for the declaration step |
| `scripts/style/unit_files.py` | Live-unit file lookup (`unit_worktrees`, `unit_file_holders`, `changed_files`, `file_text`); breaks the `style_plan`/`style_proposals` import cycle |
| `scripts/style/coverage_report.py` | The style report CLI |
| `scripts/style/style_admin.py` | Rule rename and delete (`/style_rename`, `/style_delete`) |
| `scripts/lib/berth_board.py` | `cargo-berth board --json` reader |
| `scripts/production/merge_checkpoint.py` | `--from-child` hand-off merge; `notify_style` |
| `scripts/production/proposal_issue.py` | Issue writer: `create`, `approve`, `unapprove`, `start`, `close`, `reopen`, `move-approved`, `area-style`, `rules`, more |
| `scripts/production/proposals_table.py`, `footer_counts.py` | Proposal states and the footer count |
| `scripts/production/dailies_input.py`, `dailies_render.py` | The style line in the parent's dailies |
| `scripts/production/production_lifecycle.py` | Wrap detaches style children |
| `commands/unit/direct.md` `<StandingPlanRefill>`, `commands/unit/checkpoint.md` | Unit director side of refill and checkpoint |
| `commands/showrunner/add_style.md`, `style_report.md`, `proposal.md` | Showrunner commands |
| `docs/style_production.md` | Runtime procedure (Tiers, Families, Ticket queue, Ledger, Sync, Merging back, Yielding, Big changes, Lifetime, Report) |

`scripts/style` is a merge-gate suite (`TEST_SUITES` in `merge_gate.py`, `EXPECTED_SUITES` in `test_merge_gate.py`) and has its own pyright environment in `pyrightconfig.json`.

Import direction: `declarations → approved_renames → style_footprint`. `style_footprint` imports neither. `style_ledger` never imports `style_proposals` (two module copies under `__main__`), so vault-aware checks live in `style_proposals.py`, and the ledger takes approval as a callable.

### Rule metadata and loading

- Each global rule's frontmatter declares `tier` (A-E), `family: names|values|types|modules` (B, C and D only) and `input_scope: file|crate|manifest` (absent means `crate`). Each `candidates:` block declares `surface: whole|partial`, and `partial` needs an `uncovered:` line.
- Validation (`RuleValidationError(path, reason)`): tier A iff `mechanism` is clippy, mend or rustfmt; tier B needs `candidates:` or `pre_filter:`; B, C, D need `family`; A, E forbid it. A repo-local rule must declare `tier` and `family`.
- A global rule that declares none of `tier`, `family`, `input_scope` loads as `tier="unclassified"` (family None, scope `crate`) with no checks. Any one of those keys opts it into full validation. Repo-local rules are always fully validated. A classified rule with `candidates:` must give `surface` whole or partial.
- `CandidatesSpec.surface: Literal["whole","partial","unclassified"]`, default `unclassified` (no key). An empty, invalid or explicit `unclassified` value raises `CandidatesSpecError(ValueError)`, as do a missing `kind` and `partial` without `uncovered`.
- `rules.py`: frozen `Rule(name, path, tier, family, input_scope, mode_propose, candidates, pre_filter, blob, origin, rule_id, reach)`. `load_rules(repo_root) -> tuple[Rule, ...]` reads global rules plus `docs/style` and `docs/*/style`. It does not use `load-rust-style.sh`.
- `Rule.rule_id` is `<origin>:<path relative to its rule root>`: `global:rust/foo.md`, `local:docs/style/foo.md`, `local:docs/<member>/style/foo.md`. Every ledger row and every `--rule` uses the id, never the stem.
- `Rule.reach: RepoReach | MemberReach(member, crate_dir)`. A `docs/<member>/style` rule reaches the workspace member whose directory name or package name is `<member>`, else `RuleValidationError`.
- `workspace_members(manifests) -> tuple[WorkspaceMember(directory, package_name), ...]`, deepest first: explicit members plus in-tree path dependencies, recursively, minus `exclude`. The loader and the ledger share it.
- `rules.py list [--repo] [--json]` prints `rule_id` first and shows unclassified rules. Exit 2 on a validation error. `STYLE_HISTORY_NATE_STYLE_DIR` points the loader at another rule folder.
- `style_rules.parse_frontmatter` returns `tier`, `family`, `input_scope`, `mechanism`, `mode` as one-element lists, quotes stripped. `read_pre_filter` reads only an unindented top-level `pre_filter:`.

### Coverage ledger

**Location.** `~/.local/state/style/<repo>/ledger.sqlite`, where `<repo>` is the main checkout's basename through `--git-common-dir`, so every worktree shares one ledger. `STYLE_LEDGER_STATE_DIR` replaces the root. Only `style_ledger.py` writes it. `open_ledger(repo) -> Ledger` (context manager); `preview_ledger(repo)` migrates a temp copy and leaves the real file unchanged.

**Schema.** `schema_version` drives numbered `MIGRATIONS` 1-20; a newer version than the script knows raises `CheckCouldNotRun`. Current tables:

| Table | Holds |
| --- | --- |
| `judgments` | rule, rule_blob, input_scope, input_fingerprint, path, commit, verdict, verdict_detail, ticket, worker, recorded_at, declared_scope, read_files (`'null'` or a sorted JSON list of normalized paths); unique on (rule, path, commit, rule_blob, input_scope, input_fingerprint, ticket, worker) |
| `lint_runs` | tip, tree, tool, scope (`workspace`), config_hash, passed, diagnostics_json, rule_blobs_json; unique on (tree, tool, scope, config_hash) |
| `sync` | one row: tip, style_branch, synced_at |
| `listed_rules`, `listed_sites` | one marker row per enumerated (rule, rule_blob, tip), and its sites (path, line, text); cascade delete |
| `tickets` | id `<unit>-<n>`, unit, kind, family, rule, crate, files, sites, rules, rule_blobs, state, branch, checkpoint, base, proposal, cells, finding_ids, timestamps; one `assigned`-or-`checkpointed` ticket per unit (unique index) |
| `rule_texts` | blob → pinned rule bytes |
| `handoffs` | checkpoint, state, merge_commit, sent_to, timestamps |
| `processed_tips` | tips the sync finished |
| `held_findings` | finding_id, rule, path, line, text, kind, before, after, rule_blob, ticket, state, proposal, build_ticket, moved_from, withdrawn_by, person_read_by, duplicate_of, unwaived_by, followed_from, followed_from_line, followed_at, declaration_gone_at, waive_provenance, waived_by, timestamps |
| `reconciliation` | one row: branch_point |
| `sync_waits` | unit, since, token: units waiting for sync |
| `ticket_yields` | (ticket, path) files a ticket gave up |
| `rule_candidates` | off-rule ideas: ticket, unit, path, line, idea |

**Cell universe** at a commit, read from git objects only: tracked `.rs` files, minus any path under a `vendor/` folder and the ignore lists in `.claude/config/berth.toml`, `rustfmt.toml`, `.rustfmt.toml`. A file's crate is the deepest member directory that holds it, else `.`. A `MemberReach` rule has cells only in its member.

| Tier | Cells |
| --- | --- |
| A | one per (rule, file); state from lint runs |
| B | listed component: one file cell per file with a listed site (`.rs` or not); a `partial` rule adds `<rule id>#uncovered`, a tier C pseudo-rule with one cell per `.rs` file in reach |
| C | one per (rule, file) |
| D | one per (rule, file) over included `.rs` files plus listed-site paths, at `module` input scope; a `manifest` rule keeps one cell per crate |
| E, unclassified | none |

**Fingerprints** (`CellInputScope`): `file` is the blob. `module` is sha256 over the (path, blob) pairs of the `.rs` files in the cell's folder plus the parent module file `<folder>.rs`. `crate` is the member folder's tree hash, or for crate `.` sha256 over its own files. `manifest` is sha256 over the root `Cargo.toml` and each member's. A listed cell on a non-`.rs` file uses `file` scope whatever the rule says.

**Declared input scope.** `record --input-scope file|rule`. A `clean`, `exception` or `held` verdict on a `crate` or `module` cell of an included Rust file must say which. `file` fingerprints that file alone, so the verdict stays current when other files change. `file` scope is refused for other verdicts and for manifest or crate cells.

**Read files.** `record --reads <path> [<path> ...]` names the other files a rule-scope verdict rests on. The ledger accepts it only with `--input-scope rule`, a verdict in `SCOPED_VERDICTS` (`clean`, `exception`, `held`), and a crate or module cell of an included Rust file. It normalizes each path like `--path`, then sorts the paths and removes duplicates; the cell's own path changes nothing. A judgment has one of two shapes: `ReadFiles(paths)` (an empty tuple is refused) or `NoReadFiles` (`NO_READ_FILES`). `Judgment` and `TicketJudgment` carry it in `reads`. `_read_fingerprint(tip, path, reads)` is SHA-256 over the sorted `(path, blob)` pairs of the cell file plus the read files, from `tip.blobs`. `record` stores this digest as the judgment's `input_fingerprint`, and refuses a path with no file at the commit (`<path>: no file at <commit>`). Without `--reads`, a rule-scope verdict uses the whole crate or module fingerprint, so the cell comes back after any change in its crate or module. The ticket text tells a unit to use `--input-scope rule --reads <files>`, and tells it that a cell without `--reads` comes back after any change in its crate or module.

**Input comparison.** `_judgment_inputs_match(row, tip, scope, path, family)` is the one comparison of a judgment with a tip. A judgment with reads compares only by `_read_fingerprint` at the checked tip, and a read file that is missing at the tip is a mismatch. A judgment without reads compares its declared scope's fingerprint (`_comparison_scope`). When the fingerprints differ and `family` is `modules`, `_only_comments_changed` compares the `TreeEntry(mode, kind, object)` maps at the judged commit and at the tip (`Ledger._scope_inputs` over `_inputs`, or `_read_inputs` for a judgment with reads; `RepositoryTip.entries` holds every `ls-tree -r` entry, submodules included, read once per commit): the same paths, and each changed entry is a `.rs` regular file (`REGULAR_FILE_MODES`) with the same mode at both commits and an equal `RustCode`. `RustCode` is the code tokens from `style_footprint.tokens` plus each `//!` and `/*!` doc keyed by the count of code tokens before it, so a doc that moves to another `mod` block differs. A changed mode, a changed submodule, a changed non-`.rs` blob, a blob that is not UTF-8 or that `token_spans` cannot place (`NotTokenized`), and a fingerprint change with no changed entry do not match. `Ledger._rust_code` reads each blob once. Callers pass the rule's family (`Cell.family` in `style_merge_changes`). Current state, awaiting merge, `_repeated_judgments`, `judged_at_base` and the decline cell check all go through it.

**Cell state** comes from the newest judgment rows (`ORDER BY recorded_at DESC, id DESC`) whose `rule_blob` equals the rule's current blob and whose declared scope equals the newest row's. "The inputs match" below means `_judgment_inputs_match` is true:

| State | Meaning |
| --- | --- |
| `current` | the row's commit is an ancestor of the synced tip and the inputs match there |
| `awaiting merge` | the commit is not in the synced tip, but is in the stored style branch's current tip and the inputs match there |
| `stale` | some row exists, but none qualifies; with no stored style branch, unmerged rows read stale |
| `open` | no row |

Verdicts: `clean | fixed | exception | held | waived`. Every verdict counts as current while its inputs match, so an exception expires with its inputs. A rule is done when every cell of every component is current; a partial B rule needs both components.

**Tier A.** The tool is `parse_frontmatter(rule.path)["mechanism"][0]`. A cell is current only with a run for its tool at the synced tip's tree and current config hash, `passed` true, zero diagnostics for the file, and the rule's blob as recorded in the run. No run at all reads open, else stale. The config hash covers the tool's config files, each `Cargo.toml`'s `[workspace.lints]`, and for mend `[package.metadata.mend]`, `.cargo/mend.toml` and `~/.config/cargo-mend/config.toml`.

**Listed sites.** Listed rules are classified tier B or D rules with `candidates:` and tier B rules with `pre_filter:`. `_export_blobs` writes the tip's committed blobs (`git cat-file --batch`) into a temp folder, so the working tree and git attributes never matter. Generator rules take `enumerate_candidates(rule.candidates, <export>)` minus untracked, vendor, ignored and out-of-reach paths. A `pre_filter`-only rule lists each matching line of the `.rs` universe plus each non-`.rs` file some generator names. Every coverage read (`cells`, `rule_counts`, `status`, `listed_sites`, `record`) first compares the `listed_rules` markers with the current (rule id, blob) set and re-enumerates every listed rule at that tip on any difference. A rule edit therefore gets fresh sites with no new `set-tip`. Zero-site markers tell "no sites" from "never enumerated".

**Readers.**
- `Ledger.cells(rule=None, crate=None) -> Iterator[Cell]`, `Cell(rule, path, crate, tier, family, state)`.
- `Ledger.rule_counts(scope: CoverageScope) -> tuple[RuleCounts, ...]`, `CoverageScope = WholeRepository | CrateCoverage(crate)`. Frozen `RuleCounts(rule_id, tier, current, stale, open, awaiting_merge, held, input_scope)` with `total` and `complete`. It is the one public per-rule reader: one `_coverage` pass per call, `#uncovered` folded into its parent, unclassified skipped, tier E rows present with zero cells.
- `Ledger.listed_sites(rule, crate=None) -> tuple[ListedSite(rule, path, crate, line, text), ...]`.
- `Ledger.summary() -> StyleLedgerSummary` is `summary_of(rule_counts(WholeRepository()))`. `summary --json` keys: `rules_done`, `rules_total` (classified tier A-D only), `unclassified_rules`, `cells_current_pct`, `stale`, `proposals` (distinct decision paths of filed findings), `waiting_to_merge_back`, `oldest_waiting` (UTC ISO or null), `synced_at`, `synced_tip`.
- `Ledger.unclassified_rules()`, `rust_files(crate)`, `synced_at()`, `synced_commit()`.

**CLI** (`style_ledger.py`, every verb takes `--repo`; errors exit 2 as `style_ledger: <message>`; writes upsert on their natural key; a repeat of the current transition exits 0, a backward or skipped one exits 2 naming the state):

- `record --rule ID --path P --commit C [--rule-blob B] --verdict V [--detail D] --ticket T --worker W [--input-scope file|rule] [--reads P [P ...]]`. `ID` may be `<rule id>#uncovered` for a partial B rule. Refuses: an unknown or unclassified rule; tier A or E; a path outside reach; a tier B path with no listed site at the synced tip; a tier D path that is not a crate, included Rust file or listed-site path; a tier C path outside the universe; a cell outside the ticket's owned `cells`; a path the ticket yielded; a `--rule-blob` that differs from the ticket's pin; `held` when no `unfiled` or `filed` finding maps to the cell (the error lists the cell's waived, built and withdrawn findings); `--reads` without `--input-scope rule`, with another verdict, on another kind of cell, or naming a path with no file at the commit.
- `lint-record --tip C --tool clippy|mend|rustfmt --passed 0|1 --diagnostics FILE.json` (object of path → nonnegative count).
- `set-tip --tip C [--style-branch NAME]`: stores the tip and `refs/heads/NAME`; omitting the flag keeps the stored branch. Writes `synced_at` every time.
- `status [--rule ID] [--crate K] [--json]`: `unclassified: <n> rules — pull nate_style` when n > 0, then `rule current stale open awaiting merge held done`. Under `--crate`, `done` is crate-scoped.
- `summary [--json]`, `reconcile --branch-point B --style-branch S`, `candidate --ticket T --site <path>:<line> --idea I`.
- `ticket add|assign --branch B|checkpoint --commit C|merge|drop|yield --path P --id ID`, `ticket open --unit U`.
- `unit rename --from OLD --to NEW` prints `moved <n> tickets and <n> sync waits`.
- `handoff add|send --to SLUG|reroute --to SLUG|merged [--merge-commit M] --checkpoint C`, `handoff pending`.
- `tip seen --tip C` (prints `new` or `seen`), `tip done --tip C`.
- `held add ...` (prints the finding id), `held file --id F --proposal PATH`, `held waive --id F (--ticket T | --person NAME)`, `held replace --id F --after TEXT [--person NAME]`, `held built --id F [--id F ...] [--commit <merge> [--by <who>]]`, `held list [--state S] [--json]`.

### Tickets, hand-offs, processed tips, reconcile

- **Ticket kinds:** `read`, `listed`, `structure`, `proposal`. **States:** `queued` → `assigned` → `checkpointed` → `merged`; any non-final → `dropped`. `merged` and `dropped` are final.
- `ticket_add(id, unit, kind, family, crate, files, rules, rule="", sites=None, proposal="", cells=(), finding_ids=())` pins each rule's blob in `rule_blobs` and its bytes in `rule_texts` once `git hash-object --stdin` equals the pin, else `LedgerError("<rule id>: rule changed while pinning; rerun")`. A pseudo-rule pins its parent's blob. A new id must be `<unit>-<n>`. A repeat add of an existing ticket with the same fields succeeds before that check, so a ticket that a unit rename moved keeps its old prefix. `pinned_rule_texts(id)`, `queued_tickets(unit)` (by `created_at, id`), `next_ticket_id(unit)` (highest n + 1 over every state, counting only ids with the `<unit>-` prefix, so the first new ticket after a rename is `<new id>-1`), `ticket_base(id)` (stored once from the branch tip while `assigned`; then returned in any state).
- `record --ticket` and `held add --ticket` take the pinned blob, so a judgment on a ticket whose rule changed stays stale against the new text.
- `ticket checkpoint` refuses while any owned cell is unjudged. `ticket merge` needs state `checkpointed` and the checkpoint inside the stored style branch tip. It marks a proposal ticket's findings `built` in the same transaction (only on `AppliedInCode`), prints a `not built` line per other finding, and calls `style_sync.py wake-waiters`. `ticket merge` checks ancestry only against the style branch; `reconcile` merges tickets by containment.
- `ticket yield --id T --path P` records a dropped file in `ticket_yields` and clears the ticket's verdicts and unfiled held findings on it.
- Checkpoint restamp: a ticket checkpoint copies its family's verdicts onto files that only it changed (`checkpoint_restamps`), so its own edit does not stale them. For a judgment with reads, the `rev-list` that finds changing commits runs over the cell file plus the read files, and the carried fingerprint is `_read_fingerprint` at the checkpoint.
- **Hand-offs:** `pending` → `sent` → `merged`. `handoff send` to a second parent exits 2; `handoff reroute` moves a `sent` row to the new parent and keeps `checkpoint` and `created_at` (same parent: no-op; `pending`, `merged` or unknown: exit 2). `handoff merged` refuses `pending`, refuses before any `reconcile` stored the branch point, and needs the branch point to contain the checkpoint.
- `reconcile --branch-point B --style-branch S`: stores `refs/heads/S` through `set_tip` only when it differs and never moves the synced tip; stores B; marks a hand-off `merged` when B contains it (`merge_commit` = first first-parent merge on B that contains it, else B's tip); merges a `checkpointed` ticket that S contains; drops an `assigned` ticket whose branch ref is gone; prints one line per change, plus `not built` lines.
- Branch-point reads use `pushed_branch_point_tip`: `origin/<branch point>` when origin exists (a missing ref says `fetch origin first`), else the local branch. The local branch can hold a parent merge whose gate still runs, so it is never trusted when origin exists.

### Bootstrap: `add_style.py` and the production format

- `add_style.py --production <parent doc> --model <m> --effort <e> [--pilot <unit>[=<crate>,...]]... [--check]`, run by the repo's main showrunner (`commands/showrunner/add_style.md`).
- `_request` derives everything from the parent doc: main checkout through `--git-common-dir` on `**Repository:**`; slug `<repo>-style`; branch point = parent merge branch; style merge branch `style/<branch point with / as ->`; checkout `<repo parent>/<repo>-style`; doc `docs/<repo>-style-production.md`. A missing `**Proposals:**` refuses.
- `standing_style_production(repo_dir) -> StandingStyleProduction | NoStyleProduction` (frozen; `checkout`, `doc`, `slug`, `branch_point`) is the one shared lookup. `style_plan.style_document`, `style_sync.notify` and `add_style` use it. A new caller that resolves from a repo folder must use it.
- **Existing production** = doc present and slug in `showrunners.timer_slugs()`. `_attach` detaches from any old parent, attaches under the caller, rewrites `**Parent production:**` and `**Proposals:**`, then runs `style_sync.py resume`. A doc on another branch point raises `StyleBranchPointTaken` (exit 2) with no write. A doc with no timer is an interrupted bootstrap and reruns the create path, which also rewrites the two fields.
- **Create path** (each step idempotent): branch from the branch-point tip and worktree; production doc with Units table `Unit | Plan | Worktree | Branch | Port | Owns` (Unit `<slug>-<family>-unit`, Worktree `<repo parent>/<slug>-<family>`, Branch `<slug>-<family>`, Port `—`, Owns `none — takes what the board leaves free`); four standing plans `docs/style-production/units/<slug>-<family>-unit.md` with a `**Family:**` snapshot of rule ids and tiers and an empty `## Phases`; `.git/info/exclude` entries for `/docs/style-production/`, the doc and the log; `set_tip(<stored synced tip, else the style branch commit>, style_branch)`; then `add_showrunner.py`. Launcher exit 1 or 3 prints `_open_session_guidance` and returns that code. A live herdr session named `<slug>` with no timer is refused before any write.
- `--pilot` (create path only; else exit 2 `--pilot applies only while creating <slug>`) lists only the named units in the Units table; all four plans are still written. `=<crates>` writes `- **Pilot scope:** <packages>` into that unit's showrunner-copy plan. `--pilot` accepts a family, an old `<family>-unit` id or a full `<slug>-<family>-unit` id. `--check` prints seven steps with full ids and acts on nothing.
- Format fields (`docs/production_format.md`): `**Kind:** style` (absent = product production), `**Branch point:**`, `**Parent production:**`; `**Proposals:**` required for style. Plan header (`docs/direct_plan_format.md`): `> **Standing:** style_plan.py`. A standing plan never closes and is git-excluded.
- `add_unit._is_excluded_standing_plan` accepts a plan absent on the merge branch when the showrunner copy is git-ignored and carries `> **Production:` and `> **Standing:**`. `ensure_worktree` copies it into the unit worktree only when the unit copy is missing. After first launch, the unit copy is the only one that counts.

### Ticket selection, busy files and the berth board

**Board.** `berth_board.read_board(repo) -> Board | BoardUnavailable(reason)` runs `cargo-berth board --json` and parses the envelope first. Status `unconfigured` with payload kind `no_facts`, or no reservation, is an empty `Board`. `ledger_unreadable`, empty or non-JSON output, a non-`board` envelope, missing fields, conflicting snapshots of one id, or a reference to an unknown id is `BoardUnavailable`. `Board.reservations()` collects `targets[].reservations` and the `ready_now`, `waiting`, `unresolved_overlaps`, `unconstrained_reservations` and `resolved` sections, deduplicated. `Reservation.blocks(path)` is true only when `edit_blocking_status == "blocking"` and a scope matches (`file:<path>` or bare path exactly, `tree:<root>` by prefix, `tree:.` all). Never persisted.

**Busy files.** `busy_files(...) -> BusyFiles(board, paths)`; `contains(path)` is membership or a blocking reservation. `unit_file_holders` collects, for every Units row of the parent production and of the style production: `git diff --name-only --no-renames <merge branch>...<branch>` when the branch exists, every `git status --porcelain` path in its worktree (both sides of a rename, untracked included), and other style units' open-ticket files.

**`style_plan.py next-phase --unit U --plan P --repo R`** (`next_phase -> NextPhase`):

1. Find the style doc and the unit's Units row. `--plan` must equal `<row Worktree>/<Plan cell>`, else exit 2. A rename keeps the Plan cell path.
2. A `### Phase` heading with `repair` and `status: todo` returns `repair first` before any write.
3. Run `style_proposals.complete`. Discard each ticket phase whose state is `merged` or `dropped` (`discarded <id>: <state>`).
4. An open ticket: `checkpointed` → exit 5; `assigned` → rebuild its phase, `resume <id>`. Neither reads the board.
5. When the pushed branch-point tip is not in the synced tip, record a sync wait and exit 3.
6. Read the board (unavailable → exit 4, no ticket). Run `_select_phase`. On no work, record a sync wait, read the board again and select once more, so a wake that arrives between the check and the wait is not lost.

**Refill lock.** `style_ledger.refill_lock(repo)` is a context manager beside `open_ledger`. It opens `refill.lock` (mode `"a"`) in the ledger folder, makes the folder when it is absent, and holds an exclusive `fcntl.flock` for the `with` body. One ledger gives one lock per style production. The lock blocks and has no timeout. `next_phase` takes it once for each selection pass, in two separate `with` blocks. Each block covers the board read, the busy-files read, `_select_phase` with its ticket add and assignment, the sync-wait clear and the return. Both ticket writes commit before the lock is released. So the next unit's `busy_files` reads those files as busy through `unit_file_holders`, and units that refill at the same time get disjoint files. The rule catalog read (`rules.load_rules`) runs once before the first block and serves both passes. The sync wait runs between the two blocks. Both are outside the lock. `queue` takes no lock.

`_select_phase` order: the oldest queued ticket is assigned under its own id when its files are free and in scope, and every other queued ticket is dropped (`dropped <id>: files busy|outside pilot scope`). Then approved decisions for the unit: the declaration step runs once, findings whose file is gone or whose site moved to another file are waived with the actor `WaiveByLedger()` and print a waive line, and each decision gives one `proposal` ticket of free, in-scope files (structure limits when a restructure finding is present). Then coverage through `_tickets_from`. No candidate: exit 6 when a stored style branch would change left-out cells, else `waiting for structure` or `waiting for sync`.

| Exit | Output |
| --- | --- |
| 0 | `phase written: <id>`, `resume <id>`, `repair first` |
| 2 | `style_plan: <error>` on stderr (`OSError`, `ValueError` incl. `LedgerError`, `Refusal`, `sqlite3.Error`) |
| 3 | `waiting for sync`; `waiting for structure: <crates>`; `waiting for sync: <branch point> <tip7> is not in the synced tip <tip7>`; stderr `unclassified: <n> rules — pull nate_style` when n > 0 |
| 4 | `board unavailable: <reason>` |
| 5 | `waiting for merge: <id> checkpointed at <commit7>` |
| 6 | `merge the style branch first: <n> cells judged at <tip7> wait for <style branch>` |

`queue --repo R [--unit U]` iterates the style doc's Units rows and is a read-only preview: `<unit>: <kind> <crate> <rules> — <n> files: <files>`; exit 4 on an unavailable board. It applies no pilot scope.

**Coverage candidates** (`_tickets_from`) use only `open` and `stale` cells. `_refill_cells` leaves out two kinds of stale cell: a held cell whose file blob did not change since its held judgment and whose live findings are all filed on open decisions; and a unit cell whose newest judgment is in the unit tip and the style branch and saw the inputs the unit tip has (`judged_at_base`), because a new ticket would record the same verdict. Ranking and structure blocking still count both.

- Crates are ordered by most stale cells, then name. The modules unit orders structure crates first, by other-family pending cells they block inside each family's pilot scope.
- **Structure** (modules unit): batches of tier D file cells (and the crate's manifest cells, keyed on `<crate>/Cargo.toml`) cut by `read_slices` with `BatchLimits(6, 3000, 5)`. Each ticket records its owned `cells`.
- Other families: a file cell waits only for open or stale tier D cells at its own path; a crate cell waits for every one in its crate (`blocked_by_structure`).
- **Listed:** per tier B rule, whole files in path order packed up to 150 sites; a file over 150 sites gets a ticket alone. A file's sites never split.
- **Read:** per tier C rule set (`#uncovered` included). `name-bindings-to-match-parameters` gets its own read tickets. `**First files:**` files get their own slices and rank first.
- `read_slices` groups files by folder, splits a folder over 25 files or 5,000 lines per file, then `balanced_slices` cuts contiguous groups (bound 25 files / 5,000 lines) minimizing slices under the floor of 10 files, then slice count, then the sum of squared file counts. Line counts come from `git show <tip>:<path>`, so slicing is deterministic per tip.

**Plan settings** from the unit copy's Delegation Context: `**Pilot scope:**` (`AllCrates | PilotCrates`, workspace package names; more than one line or an unknown package refuses) and `**First files:**` (repo-relative paths; ranks, filters nothing).

**Phase text.** `phase_text` builds `### Phase <n> — <unit>-<n>  · status: todo`, a Work Order with the plan's Delegation Context, each rule id with its pinned file `<plan>.parent.parent/rules/<blob>.md`, sites as sorted JSON, `uncovered:` lines, per-file read windows of at most 300 lines (the top-level test module apart), owned cells, already-held and waived findings, proposal cells and rename pairs, the `--input-scope` and `--reads` lines, Workers, and the Acceptance gate. Workers: two blind judges for a names read ticket (not name-bindings); `2 writers` over 12 files; else `1 writer + 1 tester`. Inputs are the ticket record, pinned texts and Delegation Context only, so a rebuild is byte-identical. `write_phase` restores any pin file whose blob differs, then replaces the phase in place or appends under `## Phases`. All writes go through `atomic_write` (temp file, fsync, `os.replace`). `_waived_ticket_findings` reads tier and scope from the pinned rule texts, not the live catalog.
- **Goal** by ticket kind: a structure ticket gets `Judge these N files of <crate> against every pinned rule below.`; a read or listed ticket gets `Judge the <kind> ticket for <family> in <crate> ...`; a proposal ticket (`approved = ticket["kind"] == "proposal"`) gets `Apply the approved findings of <issue> in N files of <crate>.` (`file` when the ticket has 1 file).
- A proposal ticket's Spec states its build scope as plain fact: `This ticket builds the approved proposal <issue>: its approved findings are this ticket's work. Read the issue for its sites and before/after changes. Hold every other non-mechanical finding. Package verification remains required.` It overrides no copied invariant.

**Gate.** `style_plan.py gate --repo . --ticket <id>` serves `<StandingPlanRefill>` and checkpoint step 6. Exit 0 permits, 1 returns to repair (`review:` or `unjudged: <rule> <path>`), 2 could not run (`error: <reason>`).
- Coverage ticket (`SelectedByCoverage(base)`): refuse unjudged owned cells, then `style_footprint.py check --base <base> --worktree`. The base is the later of the stored `base` and the merge-base of `HEAD` with the style branch, so style-branch work the unit took in is never judged.
- Proposal ticket (`ApprovedProposal(issue, unit, finding_ids)`): the decision must still be open and approved for this unit, with each finding filed to it and built by this ticket. Every changed file must belong to a finding, except approved reference edits of qualified rename pairs and an approved module file rename. The new file of a file rename must be the old file at HEAD with only the rename's edits (`<new> (renamed from <old>: change is not only the rename)`), unless another finding of the ticket has its old or new path. Each rename must be applied in the worktree (`review: <id>: <before> -> <after> not applied in <path>`).

### Mechanical-fix filter: `style_footprint.py`

- `check --repo R --base B (--head H | --worktree) [--finding-map FILE.json]`. One line per finding: `mechanical` or `review: <reason>; ...`, with `<finding id>: ` only under a map. Exit 0 all mechanical, 1 review (a map error prints `review: <error>`), 2 `error: <reason>` (git or file failure, `CheckCouldNotRun`).
- `--worktree` runs `git add -A` into a temp `GIT_INDEX_FILE`, `write-tree`, `commit-tree --no-gpg-sign -p HEAD`, and judges that commit. The real index never changes; the temp index is removed on every path.
- Base fold: each first-parent merge in `B..H`, oldest first, folds its other parents into the base with `git merge-tree --write-tree` and an unreferenced `commit-tree`. The judged diff is `git diff -M <folded base> H`. A conflicting fold is `review: merge in range`.
- Checks: **1** no file added, deleted, renamed or moved and no `mod` line change; **2** no externally reachable item changes shape (glob re-exports count as public); **3** no type gains or loses a field, variant, generic or trait impl (derives included; struct, enum, union); **4** a rename stays in one crate and at most 10 files; **5** under `FILE_CHANGED_LINE_LIMIT = 200` changed lines per file; **6** conditions, literals, control flow, any change inside a macro token tree (spacing included), serde/reflect/`rename =` attributes, documented names, non-Rust and binary changes, and any token change not proved equivalent.
- Proved equivalences: a rename whose definition and every use change in the finding's own hunks inside check 4's bounds; import, variant-rename, local `let` rename and lint-attribute edits under the rules in `docs/style_production.md` **Big changes**; a removed import whose name stays as a plain word (`removed_name_stays_bound`, per removed module-level `use` item and module; a removed block-level `use` keeps the strict rule: the removed path is the parent of an import added in the same module, so it names a module; snake case, not in `PRELUDE_NAMES`, no glob `use`, outside attributes and macro token trees, no `::` beside it or `!` after it — phase 49, after reviews found constant patterns that become bindings and macro arguments that change namespace); an unused `_name: T` → `_: T` parameter when the body never reads `_name` (`function_parameters`, `prove_parameter_erasures`, `erase_proven_parameters`; `r#_name`, macro arguments, `{_name}` captures and any literal with `\` and `{` count as reads).
- Trailing commas: before the check 6 comparison, `judge` drops from `old_tokens` and `new_tokens` the commas that `trailing_commas` returns for each sequence's own words after the rewrites (`without_trailing_commas`). So a rustfmt join or split that adds or drops the comma at the end of a call, a parameter list, an array or a brace list is equal (phase 50, after cargo-liner-style-values 941ef90b joined three calls). Check 3, the macro check and `flagged_hunks` do not drop commas. `trailing_commas` and `RUST_KEYWORDS` live in `style_footprint.py`; `approved_renames` and `style_plan.canonical_code` use them from there. A paren list's comma drops only after a name that is not a keyword or a lifetime or label, a `)` that does not end `pub(...)`, a `]` that does not end an attribute, or a `>` that ends a turbofish or a `fn` or `struct` name's generics (`closes_named_generics`). Any other paren list can be a tuple: `a > (b,)`, `&'a (u8,)`, `break 'a (x,)`, `#[cfg(x)] (a,) =>`, `pub(crate) (u8,)`.
- `--finding-map` maps each id to a file list or `{files, hunks: [{path, old_start, new_start}]}`; each hunk needs exactly one owner.

### Standing-plan refill and sync

**Unit side** (`commands/unit/direct.md` `<StandingPlanRefill>`, `commands/unit/checkpoint.md`): refill runs `style_plan.py next-phase --unit <unit> --plan <unit worktree>/<Plan cell> --repo <unit worktree>` at startup, after compaction or restart, and at each `<NextPhase/>`. It first resumes checkpoint step 7 for any reservation record in `checkpoint_committed_awaiting_release_confirmation`. Exit 3, 4, 5 report once and end the turn blocked (`waiting on the showrunner: sync | board | ticket <id> merge`). A sync message, any showrunner message (board), or `ticket <id> merged; refill.` wakes it. Exit 6: `git fetch origin <style branch> && git merge --ff-only origin/<style branch>`, then refill; a refused or empty fast-forward goes to the showrunner. A standing plan never runs FinalGate, RunAsBuilt, final PeriodicCI, CICleanup, RunSummary or shrink.
- Units sync at phase start with that `--ff-only` merge (the `docs/production_format.md` item 5 exception).
- Checkpoint: gate exit 0 stages only code paths from `git status --porcelain` (rename pairs together), commits once, then `ticket checkpoint`. The plan, `docs/style-production/` and pinned rules never appear in `git add`. Exit 1 keeps the reservation and returns to repair; exit 2 keeps the reservation and reruns the gate, and stops when the same error comes back.
- No-change ticket (no tracked diff): no commit; `git merge-base --is-ancestor HEAD refs/heads/<style branch>` (else report, ticket stays assigned); `ticket checkpoint --commit HEAD`; `ticket merge`; the committed-awaiting-release record with that `HEAD`; report `From <unit>: ticket <id> closed with no code change; <n> held.`; refill at once.
- Sync waits: refill writes a `sync_waits` row before its last check; `ticket merge` runs `style_sync.py wake-waiters`, which sends `From the showrunner of <slug>: ticket <id> merged; refill.` to each waiting unit director found through `message/roster.py --json` and clears only a delivered wait (`acknowledge_sync_wait` keys on the wait token).

**Sync trigger.** `merge_checkpoint.notify_style` and `merge_gate.notify_style` (two copies; 30 s subprocess timeout; one-line failure) call `style_sync.py notify --repo-dir <checkout> --branch <b> --tip <sha>` after each successful push, including hand-off merges. It is silent with no style production on `b`; else `send.py --timeout 10` of `From <caller>: branch point <b> moved to <sha7>; sync.` keyed `style-sync:<slug>`, bounded at 35 s. A failure prints one line and exits 0.

**Sync steps** (style showrunner; skipped when `tip seen` prints `seen`): merge the tip into the style branch and gate; `style_sync.py lint --repo R --checkout C --tip T` (`verify.sh lint --workspace --no-cache --check` in a disposable checkout; one `lint-record` per tool; exit 0 clean, 1 diagnostics or a failed tool (continue), 2 error (stop)); `set-tip --style-branch`, then `status` (report a nonzero unclassified count to the parent with "pull nate_style"); tell unit directors to merge at their next pause and wake sync waiters; `tip done`. The listed-site refresh runs on every read regardless.

### Child hand-off merge

- Style showrunner, after each unit checkpoint merge: `merge_checkpoint.py --production <style doc> <unit> <phase> <hash> --started …` with one `--also <path>` per entry of the ticket's `files` (Owns parses as `('none',)`); `ticket merge`; then `style_sync.py send-handoff --repo R --slug S --checkpoint <style branch tip>` (`handoff add`, notice `From the showrunner of <slug>: hand-off <hash>` to `showrunners.py parent S`, `handoff send`). A root prints `root: merge <c7> with --root-self`.
- `style_sync.py resume --repo --slug --branch-point --style-branch` runs first at startup and on reattach: `reconcile` lines, then oldest first: `pending` → notice and `send`; `sent` to the current parent → notice again; `sent` elsewhere → notice and `reroute`. A failed send prints one line and leaves the row (exit 1).
- Parent: correctness-review the diff against the branch point, then `merge_checkpoint.py --from-child <child> --checkpoint <hash> --production <parent doc> --started <time> [--root-self]`. It reads no Units table. Refusals: child not attached (`parent_of(child) == parent`; `--root-self` needs no parent), hash not on the child's merge branch, not descended from the last merged hand-off, staged changes in the parent tree, or a unit-only flag (`--regime` other than after, `--holds`, `--merge-defects`, `--excluded`, `--scratch`). Fixed metadata: phase `style hand-off`, no review trial.
- Run: `checkpoint_tests` (`merge_<c7>_test.log`), merge `Merge <child> hand-off (<hash7>) into <branch point>`, push, `notify_style`, promote (label `<child> style hand-off`). Then `record_handoff_merge` runs `style_ledger.py handoff merged --repo <child checkout> --checkpoint <hash> --merge-commit <merge>` (30 s timeout; a failure prints `hand-off ledger: <detail>`, and the style showrunner then runs `handoff merged` by hand), and `acknowledge_handoff` sends `From the showrunner of <parent slug>: hand-off <hash7> merged as <merge7>.` (a failure prints `hand-off acknowledgment: <error>`). A root prints `root: run style_ledger.py reconcile to mark <h7> merged` and does neither.
- Delivery state is computed from git each run, never stored: `NotMerged | MergedNotPushed(merge_head) | MergedAndPushed(merge_head)`. A retry pushes an unpushed merge before any acknowledgment; an already merged hand-off prints `hand-off <hash7> already merged as <merge7>` and acknowledges again (exit 0). `undo_failed_push` aborts a half merge and resets only when origin is unchanged.
- History: `merge_branch_history` also matches `^Merge (.+?) hand-off \(([0-9a-f]+)\)` into `ChildHandoffEntry(child, short, merge_branch, merge_hash)`. `entries: tuple[MergeEntry | ChildHandoffEntry, ...]`. Unit readers (`last_for_unit`, `code_merge_count`, CI points, lifecycle, dailies) guard with `isinstance(entry, MergeEntry)`; `last_handoff_for_child` and `last_merge()` read hand-offs.
- Wrap: `production_lifecycle.wrap` detaches running style children (`refuse_running_children -> RunningStyleChildren`, `detach_style_children`) only after promotion, unit retirement and notifier removal, and tells each `<parent> wrapped, so you are a root now: run style_sync.py resume and merge your own hand-offs (docs/style_production.md, Merging back).` Any other running child holds the wrap. `retire_reservations` reads `read_board`, prints `retire skipped: board unavailable (<reason>)` on `BoardUnavailable`, and releases nothing for an unconfigured repo.

### Style report and dailies line

- `coverage_report.py --repo R [--rule <id or unique stem> | --crate <dir or package>]` (`/showrunner:style_report [rule|crate]` runs it for each style production at or below the caller). Header from `summary_of(repo rows)`: `<slug> — synced <branch point> <tip> at HH:MM <zone>, <n> files (+<d> since yesterday)`; `Rules done: <d> of <t> (<n> by lint) · cells current <p>% · open · stale · awaiting merge · held`; the unclassified line; `Waiting to merge back: ...`; table `| Rule | Tier | Current | Stale | Open | Awaiting merge | Held | Done |` (classified tier A-D only; `#uncovered` folded in; stem, or full id when two rules share a stem); `Since last report: ...`. `rule_counts(WholeRepository())` runs once, plus `rule_counts(CrateCoverage(crate))` under `--crate`. `--rule` adds open tickets; `--crate` adds the oldest open ticket.
- Snapshot `ReportSnapshot(rules, proposals, files_by_day)` is written atomically to `ledger.database.parent / "last_report.json"`; `previous_report -> FirstReport | PreviousReport`.
- Dailies: `dailies_input` saves a `style` key only for a `**Kind:** style` doc (raw `summary --json`, or `{"unavailable": "<reason>"}`). `ChildReport.style: StyleSummary | NoStyleSummary | StyleSummaryUnavailable`; unknown keys are refused. `child_row` puts `<child>  rules done 21/51 · cells 62% · stale 1,402 · proposals 3 · synced 14:05` in `Row.lines_below`, plus ` · unclassified <n> — pull nate_style` and ` · hand-off waiting <h>h` when the oldest pending hand-off is more than 4 h old. `cells` is `math.floor(pct)`. `ChildReport.cadence: ReportsEvery(minutes) | ReportsOnDemand`.

### Style decisions (proposals)

**Model.** One decision per (rule, crate, rule blob). Its answer (approved, declined, unanswered) also covers later findings under the same rule text. The ledger keeps which decision holds each finding (`proposal`, `moved_from`). The issue path comes from the key (`batch_name`, a crate's `/` shown as `+`), not from the P number. Category `refactor`, tags `[issue, proposal, style]`, area `[[style]]` (`STYLE_AREA`), frontmatter `style_rule`, `style_crate`, `style_rule_blob`, `style_rules` links. Each decision shows counts and up to three examples from different files; `style_proposals.py sites --issue` gives the full list.

**Held states.** `Unfiled | Filed(proposal) | Waived | Built(proposal) | Withdrawn(proposal | NoDecision, by)`, `WithdrawnBy = ExceptionVerdict | JudgedAgain | DuplicateOf | DeclarationGone`. Side records: `build_ticket: NoBuildTicket | BuildTicket`, `reading: NotReadByPerson | ReadByPerson(by)`, `waive_undo: NoWaiveUndo | WaiveUndoneBy(by)`, `follow: NotFollowed | FollowedDeclaration(from_path, from_line, commit)`, `waive_provenance: WaiveProvenance`, `waive_actor: WaiveActor | NoRecordedWaiveActor`. `finding_id` is the first 16 hex of sha256 over (rule, path, line, text, rule_blob) and never changes after a follow.

**Waive actors.** Every waive records who made it. `WaiveActor = WaiveByUnit(ticket) | WaiveByPerson(by) | WaiveByLedger()`. `WaiveProvenance` is `unrecorded`, `unit`, `person` or `ledger`; the row stores it in `waive_provenance` (default `unrecorded`) and the name in `waived_by` (default empty). `_waive_actor_record(actor)` maps an actor to its stored `(provenance, waived_by)` pair: the ticket, the person's name, or `ledger`. It refuses an empty ticket or name. Every waive write goes through it, so every new waive row has a non-empty `waived_by`. Old rows stay `unrecorded`, and `_held` reads them as `NoRecordedWaiveActor`.
- `held waive --id F` takes exactly one of `--ticket T` or `--person NAME` (an argparse group of exclusive options). `waive_held(finding_id, actor)` refuses a ticket that did not judge the finding. A unit's waive does not change a finding that is already waived, and no waive changes a withdrawn finding. A person's waive writes over a unit's waive.
- `DeclineActor = WaiveByPerson | WaiveByLedger`. `decline_findings(finding_ids, words, actor)` refuses any other actor with `decline requires a person or the ledger` before any write. A decline over a unit's waive replaces that actor with the decline actor. `decline` and `apply_answer` in `style_proposals.py` pass `WaiveByPerson("user")`; the refill waive in `style_plan.py` for a gone file or a moved site passes `WaiveByLedger()`.
- `held replace --id F --after TEXT [--person NAME]` (`Ledger.replace_held`) corrects a waived finding. It sets the new after text and keeps the id and judging ticket, sets the finding to unfiled, clears its proposal, build ticket and person reading, and sets a newest `waived` cell judgment back to `held` (`_restore_held_findings`, which `unwaive_held` also uses). It prints the id. It replaces only a unit's waive: it refuses a finding that is not waived, a ledger waive (`cannot replace a ledger waive`) and a person's waive. An unrecorded waive needs `--person`, and the ledger records that name in `unwaived_by`, as unwaive does. Plain `held add` keeps an existing waive, because it does nothing on an existing finding id.

**Approval and progress** (`proposal_issue.py`): `proposal_progress(issue) -> AwaitingApproval | InProgress`. Approved, or unanswered at `stage: active`, is `InProgress`; all else (open declined included) is `AwaitingApproval`. `approve`, `create` with inherited approval, and `reopen` of an approved decision write `stage: active` (`IN_PROGRESS_STAGE`). The footer counts `AwaitingApproval` only; `proposals_table` shows `waiting for your review` rows first, then `in progress by <unit>`. `move-approved <folder> [--dry-run]` moves approved `backlog` proposals to `active` by hand (exit 2 on a refused file). `approve --by user|showrunner` writes `approved_by` and `approved_in`, the production slug of the calling showrunner (`calling_showrunner`: `showrunners.production_of` on `CLAUDE_CODE_SESSION_ID`; exit 2 outside a showrunner). `approver(issue)` is `showrunner` only when `approved_by` says so, so an approval from before the field is the user's. Under the lock, `--by showrunner` over an approved issue whose approver is the user leaves `approved_by` and `approved_in` as they are; `--by user` over a showrunner approval sets them, so it prints `set approved_by`, not `unchanged`. `create` inherits both fields (`INHERITED_FIELDS`). `unapprove <issue> [--dry-run]` undoes a showrunner approval: under the lock it refuses (exit 2) an issue that is not open, has no `proposal` tag, is not `ApprovedDecision` (declined included), whose approver is the user, or whose `approved_in` is not the calling showrunner; else it sets `APPROVAL_FIELDS` (`showrunner`, `unit`, `plan`, `approved_by`, `approved_in`) to `""` and `stage: backlog`, so it reads `AwaitingApproval`. A dry run makes the same checks and writes nothing. Every write holds `_issue_lock`, rereads under it and writes only on a byte change. `_issue_lock` takes its lock file from the fixed folder `~/.local/state/proposal-issue/locks` (from `Path.home()`) and creates the folder on use. It reads no environment variable (`XDG_RUNTIME_DIR`, `TMPDIR`), so every run on the machine shares one lock per issue.

**`style_proposals.py` verbs** (most take `--repo <style checkout>`; dry runs print the same lines and write nothing):

| Verb | Does |
| --- | --- |
| `file --proposals <dir> --parent <slug>` | runs the declaration step, then withdrawal, then batches unfiled findings into decisions; crash recovery marks findings an existing decision lists; exit 1 on a skipped decision |
| `follow --proposals [--dry-run]` | the declaration step alone; exit 1 on a left finding or skipped decision |
| `withdraw --proposals [--dry-run]` | the withdrawal step alone |
| `complete` | closes approved decisions whose findings are all built or waived (also with no merged ticket); renumbers |
| `decline --issue <path> --words '<words>'` | stores the answer, waives filed findings in one transaction with the actor `WaiveByPerson("user")`, closes with `declined: <words>` |
| `unwaive --proposals --id <id>... --by <who>` | waived → unfiled; refuses a declined decision, changed rule text, open ticket, blank `--by` |
| `reopen --issue <p> --parent <s> [--finding <id>... \| --applied <id>... --by <who>] [--dry-run]`, `reopen --all [--parent <s>]` | files built renames that are not in the code again; `cannot check` lines exit 1 |
| `migrate --proposals --parent [--dry-run]` | moves old style issues into decisions; `skipped: off-rule <path>` for `/issue` filings |
| `drop-duplicates [--dry-run]` | withdraws bare duplicates of qualified renames |
| `rank`, `link-rules`, `candidates`, `drop-candidate --idea`, `sites --issue` | ranking, rule links, off-rule rule candidates, full site list |

`issue_project(repo, crate, vault) -> IssueProject(short, project)` reads each `home-base/issues - <short>.base` filter (package name first, then repo name); no match prints `no issue base for <name>: run /new_issue_base <name> in hanadocs`.

**Join action** (`join_action(ledger, issue) -> JoinAction`): declined → `WaiveWithAnswer` (`waived <n> findings in <decision>: declined: <words>`); open → `JoinOpenDecision`; closed and approved with nothing left (`left_to_build` keeps every state except `Built` and `Waived`) → `ReopenBuiltDecision`; closed unanswered reopens only when its reason starts with `built in ` or `nothing left to build: `. Refusals: `closed decision has no approval`, `closed decision has unbuilt findings` (open it with `proposal_issue.py reopen`).

**Filing race.** `proposal_issue.create` reads the decision path through `_decision_path_state` before and again inside `_folder_lock`. `ALREADY_FILED` (same rule, crate, blob) returns False and writes nothing; a different key raises `Refusal("<path>: different decision key")`. In `_create_numbered`, a `FileExistsError` from `os.link(temporary, path)` reads the path again through `_decision_path_state`: the same key returns False and keeps the other run's file, and a different key raises the same `Refusal`. The `.decision-*` temp file is always removed.

**`area-style <folder> [--dry-run]`** sets `area: "[[style]]"` on issues with `style_rule` or `style_rules` whose area is missing or `[[unfiled]]` on the key's first line; it prints `area-style <name>: area [[style]]`; exit 2 on a folder or file error.

**Withdrawal step** (`withdraw_superseded`): unfiled and filed findings whose rule text changed, minus unfiled findings of open tickets. The newest judgment at the current cell across current and original blob must use the current blob and postdate the finding (and its `updated_at` after a follow). Any verdict withdraws an unfiled finding (`JudgedAgain`); only `exception` withdraws a filed one (`ExceptionVerdict`). An open build keeps it (`KeptForBuild`). A decision with all findings withdrawn closes with a reason that names them.

**Building.** `applied_check(kind, before, after, path, read) -> AppliedInCode | RenameNotApplied | NoAppliedCheck`. A rename with one `<parent>::<name>` pair is applied when only the new name is declared under its parent. `paste!`, `include!`, non-std derives on the parent, keyword file names and child-module folders give no answer. `FileRename` covers `Rename <a>.rs to <b>.rs`. `ticket merge` builds on `AppliedInCode`; `RenameNotApplied` loses its build ticket; `NoAppliedCheck` keeps its merged ticket and a person runs `held built --id`. `held built --commit <merge> [--by]` (`mark_built_at_merge`) marks filed findings built after a plain merge that is in the synced tip.

### Declaration follow, renames, trait identity, outside-rename proof

The declaration step (`Ledger.declaration_reading(ids, approved)`, `follow_declarations(ids, at, approved)`) reads each unfiled or filed `.rs` finding at its read commit (last follow commit, else `judged_commit` = judging ticket's checkpoint, else base) when that commit is in the synced tip. `declarations.read_declarations(text) -> DeclaredFile`; `Declaration(keyword, name, owner: ModuleItem | ImplOrTraitItem(name))`; inline module names are not part of identity.

Outcomes (`DeclarationOutcome`): `DeclarationNotRead`, `DeclarationInPlace`, `DeclarationMoved`, `DeclarationRemoved`, `DeclarationRenamed` (filed approved own rename → built), `RenameAwaitingApproval`, `KeptForOpenBuild`, and the left set `DeclarationLeft = DeclaredInSeveralFiles | DeclaredInAnotherCrate | DeclarationGoneFromFile | TraitIdentityUncertain | OutsideRenameUnresolved`. `style_plan`'s match over `DeclarationLeft` is exhaustive through `assert_never`.

Order in `_declaration_outcome`: trait-identity uncertainty and same-identity moves; one gaining file follows (another crate leaves a filed finding, follows an unfiled one); several leave it; a filed rename's own approved rename (`_renamed_site`); built-rename chains from approved decisions in the same crate (`_built_renaming -> RenamedByBuilds | NoBuiltRename | RefusedBuiltRename | TraitIdentityUncertain`, `RenameScope = UnqualifiedName | ModuleQualified | SelfQualified | TypeQualified`; chains stop at a fork or a return to an earlier name); the outside-rename proof; then `DeclarationGoneFromFile` (filed, file still there) or `DeclarationRemoved` (withdraw with `DeclarationGone(<tip>)`). The open-build check is last over every outcome.

**Trait identity.** `ImplementedTrait(path, arguments, position)`, `ImplPosition = FileLevelImpl | BlockScopedImpl(header_word) | UnprovedImplPosition`. Equal paths with equal arguments are the same trait; different final names or arguments differ; otherwise rooted module paths must resolve to declared traits (one declaration of the name in the crate). Plain unconditional file imports and `as` aliases prove paths; conditional imports, globs, re-exports and visible block imports block proofs. `_file_module` proves a module chain from `src/lib.rs` or `src/main.rs` through plain `mod name;` links (`#[path]`, custom targets, `src/bin`, two chains, outside `src/` give none). `_relative_trait_path` resolves file-level `self::`/`super::`. Alias expansion stops at 32 bindings and detects cycles by visited (file, binding) pairs. Uncertain identity keeps the row unchanged.

**Outside-rename proof** (`OutsideRenameProven` → `DeclarationMoved`, never built): (a) the old name is declared nowhere in the crate at the synced tip; (b) a zero-context diff maps the site to a same-keyword, same-owner, same-trait declaration under a new name (`MappedSiteProof`), or exactly one changed Rust file in the crate gained one such declaration (`GainedFileProof`; the original file counts; two items in one file are ambiguous); (c) body test `2 * unchanged >= old_nonblank_count` after the identifier swap, inside the item's own delimiters. A child (method, associated item, `let`, closure parameter) follows only through a proved owner. A failed search after (a) gives `OutsideRenameUnresolved` with evidence and keeps the row.

All follow writes run in one `BEGIN IMMEDIATE` transaction. The write refuses when the synced tip moved since the read (`...; rerun`), and reads `approved` again under the lock. Output lines: `follow`, `built`, `withdraw`, `left`, `kept`, `close`, `totals: follow <n>, built <n>, withdraw <n>, left <n>, kept <n>, close <n>`. Refill prints `waiting <decision>: <reason>` for left findings and makes no ticket for them.

### Held-verdict refusal

`record --verdict held` needs a live (`unfiled` or `filed`) finding at the cell (`_cell_findings` maps sites through `_finding_cell`, so a manifest rule maps to its crate). For a cell whose only findings are waived, record `waived`. Structure tickets list waived findings under `Waived by the user: do not hold these again. Record waived for a cell whose only findings are these.` A `waived` verdict expires with its inputs. `unwaive_held` and `replace_held` turn a newest `waived` verdict back into `held` without going through `record` (`_restore_held_findings`).

## Invariants

- basedpyright reports 0 errors and 0 warnings on every touched script folder; no file-level ignores; no `Any`. Every module has `from __future__ import annotations`; discarded results are `_ = …`; tests use `@final`/`@override`; sum types are small frozen dataclass variants. Scripts run through `~/.claude/scripts/lib/py`.
- State lives under `~/.local/state/`. User-facing times go through `scripts/lib/user_zone.py`.
- Scripts message sessions only through `scripts/message/send.py`, and find a showrunner by `showrunners.py address <slug>`.
- Rules stay in `~/rust/nate_style`; the procedure stays in `docs/style_production.md`. Standing plans never go under `docs/style/` or `docs/*/style/`, since `load_rules` reads every `.md` there.
- Style units own no files. They take what the board, live units' branches and dirty worktrees leave free. Product work always wins.
- A fix applies without asking only when `style_footprint.py` calls it mechanical. Every other change is held and shown only when asked.
- Any code that matches tiers handles `"unclassified"`. An unclassified rule has no cells or tickets and never counts as current or done.
- `candidate_generators` returns every site. Any limit belongs to its callers.
- `rules.style_family(unit)` maps old and full unit ids to `Family | NotStyleUnit`. Plan readers use the Units row's Plan cell relative to the unit worktree; rename keeps that path. `Ledger.rename_unit(old, new) -> UnitRenamed(tickets, waits)` and `unit rename --repo <checkout> --from <old> --to <new>` move current ticket owners and sync waits in one transaction. Ticket ids and history stay unchanged; reapplying moves zero rows. The target must own no current rows, and both ids must name the same family. New ticket numbers use only their own id prefix. `rename_unit.py` runs this move for a `Kind: style` production (`docs/as-built/unit-rename.md`). `board_json.py` finds a unit's current phase through `phase_table.build_run` records, so a moved ticket with the old prefix still shows.
- A style unit id already starts with `<style slug>-`. Code that builds a worktree, branch or herdr host name from a unit stem goes through `unit_names.branch` (or `worktree`), which returns the stem unchanged when it starts with `<slug>-`; else the prefix doubles. `add_unit.host_session_name` and the restart record's herdr host names use this rule.
- Every ledger row and `--rule` uses `Rule.rule_id`. Map a unit to its branch through the Units row, never by name derivation.
- Every `set-tip` caller that expects awaiting-merge cells passes `--style-branch`. `reconcile` never moves the synced tip.
- Read committed trees only through git objects (`ls-tree`, `cat-file --batch`), never `git archive`.
- `rule_counts` is the one per-rule reader. The report's header numbers come from `summary_of`; the report defines no second count.
- Ledger writers do git, file and catalog work before `BEGIN IMMEDIATE`, recheck their rows under the lock, and use `retry_on_change`.
- `refill_lock` covers only the board read, busy-files read, selection and ticket writes of one refill pass. It never covers the catalog read, the sync wait, a judge run or any work after `next_phase` returns.
- `phase_text` takes only the ticket record, pinned texts and Delegation Context, so a rebuild is byte-identical. Pinned text comes from the ledger, never the live catalog.
- A proposal ticket's Work Order gives a build Goal (`Apply the approved findings ...`, never `Judge`) and states its scope as plain fact, never as an override of a copied invariant.
- A unit director builds each prompt only from its own Work Order, plan and conversation, never from another unit's prompt files under `/tmp/claude/unit/*` (`docs/delegate/compose_work_order.md` **Fallback**). A missing build fact goes to the showrunner.
- Plan and pin writes use `atomic_write`.
- The style showrunner's unit-checkpoint merge keeps the unit tip an ancestor, so the unit's next `--ff-only` merge succeeds. Standing units never make `--no-ff` merges.
- Every reader of merge history handles `ChildHandoffEntry`.
- No acknowledgment of a hand-off precedes a successful push.
- `proposal_issue.create` keeps both `_decision_path_state` checks, before and inside the folder lock.
- Every state reader compares a judgment with a tip through `_judgment_inputs_match`. A judgment with reads compares only by `_read_fingerprint`.
- Every waive write picks a `WaiveActor` variant and passes it through `_waive_actor_record`. A new actor kind extends `WaiveActor` and `WaiveProvenance`, and `_held` maps it back from `waive_provenance` and `waived_by`. `decline_findings` takes only a `DeclineActor`. `replace_held` replaces only a unit's waive, and an unrecorded waive only with `--person`.
- Readers count proposals by the approval record, not only the stage. Never write stages on read.
- `style_footprint.tokens`, `closing` and `attribute_spans` keep their behavior; `declarations.py` and `style_ledger.py` use them.
- A new `DeclarationLeft` outcome needs a refill case in `style_plan`.
- Each proof rule in the declaration step refuses when unsure. Widen refusal; never resolve more.
- A held finding's `finding_id` never changes, a follow included.
- Never use `git hash-object -w` objects or private refs to keep state: `git gc` prunes loose objects, and private refs keep dropped commits reachable.

## Calibration and gotchas

| Value | Where |
| --- | --- |
| 150 sites | listed ticket pack bound; a larger file goes alone |
| 25 files / 5,000 lines, floor 10 | read ticket slicing (`_READ_LIMITS`) |
| 6 files / 3,000 lines, floor 5 | structure and restructure ticket slicing (`_STRUCTURE_LIMITS`) |
| 300 lines | read window size in the Work Order |
| 12 files | over this, a ticket gets `2 writers` |
| 10 files | footprint check 4 rename bound |
| 200 changed lines | `FILE_CHANGED_LINE_LIMIT`, check 5, per file |
| 30 s | `BUSY_TIMEOUT_SECONDS` on every ledger connection; `notify_style` subprocess timeout |
| 3 | `LOCK_ATTEMPTS` in `retry_on_change`; the third change refuses `...; rerun` |
| 32 | alias expansion bound in trait-path resolution |
| 4 h | dailies `hand-off waiting <h>h` flag |
| 10 s / 35 s | `send.py --timeout` / subprocess bound (`SEND_RUN_SECONDS`) in `style_sync.py` |
| 16 hex | `finding_id` length |
| half | outside-rename body test: at least half the old non-blank lines remain |

- A `--reads` judgment keeps declared scope `rule`, so the newest-declared-scope filter of cell state treats it as a rule-scope verdict.
- A comment-only change keeps a `modules` judgment current (`_only_comments_changed`, called from `_judgment_inputs_match`). Before it, a names ticket that changed 2 comments in cargo-berth's `board/rows.rs` sent 22 modules cells back to judgment, and none gave a new result. Inner docs (`//!`, `/*!`) still count, because `when-to-split-a-module.md` counts module docs as a reason for a facade. Other families keep the byte compare: names rules judge comment text. Stored fingerprints do not change, so the check reads the inputs at the judged commit, and only after the fingerprint compare fails.
- Tests that file proposals must patch `Path.home`: the lock folder reads no environment variable, so a patched `XDG_RUNTIME_DIR` or `TMPDIR` does not move it.
- cargo-berth exits 4 for an unconfigured repo and for an unreadable ledger; only the envelope status tells them apart.
- Refill does not fetch. It reads the local `origin/<style branch>` and `origin/<branch point>` refs, so a stale tracking ref gives exit 3 until a fetch updates it. A branch point that is not pushed reads as missing.
- `RepairFirst` returns before the discard, so finished phases stay until the repair clears. `board unavailable` records no ticket, but the discard may have already rewritten the plan.
- `rules.load_rules` takes 4.5 s warm and 21 s cold on cargo-liner. Keep it, and every other read of no shared state, outside the refill lock: work inside the lock delays the refill of every other unit.
- A todo phase that a unit plan already holds is not written again. Only a resume with no todo phase for the open ticket rebuilds it from `phase_text`, so a `phase_text` change reaches an open ticket only through such a rebuild.
- `ticket_base` refuses every state but `assigned` until a base is stored.
- A `checkpointed` ticket blocks the unit's next `assign` until `ticket merge`.
- `git add` on a path under the shared exclude exits 1 without `-f`; stage from `git status --porcelain`.
- `add_unit` copies a standing plan only when the unit copy is missing; later showrunner-copy edits never reach a launched unit.
- Every style Units row's Owns parses as `('none',)`, so a unit checkpoint merge needs one `--also` per ticket file.
- A root's own hand-off stays `pending`; `reconcile` after each `--root-self` merge marks it merged by ancestry.
- Former child slugs go through `showrunners.canonical_slug` before the doc lookup.
- The footprint filter is lexical and fails closed: a private rename with a test module's `use super::*`, an unrelated edit on a macro line, or an attribute on an unknown item goes to review. It reads committed revisions only; `--worktree` exists for that reason.
- The tokenizer keeps a string literal as one token, so the `{_name}` capture regex runs over literal tokens. When `token_spans` is empty, the original change is kept and the answer stays review.
- Pilot scope lines name workspace packages, not member folders; `status` groups by member folder.
- `StyleSummary.synced_at` is naive local time in the report zone; `oldest_pending_since` is aware UTC.
- The parent reads only the child's saved report, so the dailies line is as fresh as the child's last report. A grandparent's `expand` shows no style line.
- A percentage prints one decimal with a trailing `.0` dropped; counts use thousands separators.
- The proposals delta in the report compares filed decision identities, not the count.
- Declaration identity collides across files often (test helpers, `fn main`, two impls of one trait), so a file counts only when it gained the declaration since the read commit.
- `declaration_at(line)` picks the last item on a shared line; a line with several items is an unproved position.
- The ledger stores absolute proposal paths; a dry run on a copied issues folder needs those paths rewritten in a copy ledger.
- A vault base's short name differs from its project (diegetic → hana_diegetic), so the project is never derived from the file name.
- `/close_issue` on a style decision leaves its findings held. A decision's approval never carries over to a new rule blob.
- `_key_span` includes comment lines after a key; `area-style` matches the area value on the key's first line only.
- The issues folder is shared, so `move-approved` moves every production's approved proposals.
- A child Python process does not inherit a changed `sys.path`; tests that spawn one pass `PYTHONPATH`.
- `load-rust-style.sh` is zsh; bash fails on it.
- A file rename's `after` may say that code does not move ("no existing definition moves"). `names_other_change` accepts only closed negation forms (`NEGATED_VERB`, `NEGATED_SUBJECT`, `NEGATED_NOUN`, `NEGATED_OBJECT`) and refuses every other move word. `NEGATED_OBJECT` takes only the clause end after its noun: a place word there can start an exception (`move no other helpers into ledger/error.rs except LockError`). Keep the forms closed: in phase 45, each open word list (any negation word, an exception list after `no`) let a review find a real move that passed as negated, over 5 rounds. A false refusal is safe, because the row can be reworded. The phase 47 diff floor does not make a negation form redundant: the text check guards a partial build (the finding asks for a rename and a move, the worker does only the rename, the diff is a pure rename and the gate marks the larger request done), and dropping a form only sends more rows to a person. Residual: a request for a move that the text rules do not catch (a verb outside `CODE_MOVE`, as in `only LockError goes into ledger/error.rs`, or a phrasing the closed forms let through) passes as a file rename; when the worker builds only the rename, the gate marks the finding built. A full build of that move is refused by the diff floor.
- `renamed_text` makes a Rust file's HEAD text with the approved rename edits (module sites of each `RenamedModuleScan`, qualified references of rename pairs, `renamed_comment` in comments); `RenamedText.matches` compares its `canonical_code` with the worktree text's. `approved_module_file` (a file outside the findings, which must name a renamed module) and `approved_renamed_file` (the new file against HEAD's old file, scanned at the old path so `super::lock` and `crate::ledger::lock` resolve) both call it. The renamed file had no check before phase 47: a definition could move into or out of it. Canonical order sorts only `use`/`mod` runs, so it moves no definition between files. `style_footprint.trailing_commas` keeps every comma inside a macro token tree (`macro_spans`, `macro_rules!` included) or an attribute: a macro arm or an attribute macro can match `[1,]` and not `[1]`. `canonical_code` is one sequence of pieces: each word outside a run with the comment lines before it (a dropped trailing comma gives its lines to the next word), and each item of a run (`run_items`) with the lines before its first word and inside it, each keyed by the count of the item's words before it. In an item only the `use` tree sorts (`sorted_use_groups`, which drops no word); an attribute, the visibility and a `mod` item keep their words, so every dropped comma comes from `trailing_commas`. With these keys, a comment that moves to another place changes the sequence, and a run's comments stay with their own item when the run sorts (`/// Alpha API.` cannot move from `pub mod alpha;` to `pub mod beta;`). The gap before a run stays in place up to its first outer doc comment (`outer_doc`), because rustfmt leaves a plain comment above the first item; a comment after the run's last word belongs to the next word. Residual: a comment at the end of a run line (`use a; // note`) belongs to the next item, so a rustfmt reorder that moves it with its line is refused. A comment inside a `use` tree group keeps its word-count key while the group's entries sort, so a rustfmt sort of that group can be refused. A byte-identical new file passes `approved_renamed_file` with no scan, because `renamed_text` needs a crate root and `tests/` files have none. The gate skips this check when another finding of the ticket has the old or the new path: a finding path is free, and the renamed file moves under that approval (an item rename declared in the renamed file needs this, because a declaration is not a qualified reference).
- `approved_module_file` judges a file rename's edits in files outside the findings. `RenamedModuleScan.renames` gives the new word of each site: a `mod` name, a path segment, or a `#[path]` string that, read from the file's own folder, is the old file (only its file name changes; a `mod` with a `path` word in any other form is not a site). A site is only at an item start outside every macro token tree (`code_item_starts`, also used by `module_items`): HEAD refused `stringify!(#[path = "ledger/lock.rs"] mod lock;)` in `lib.rs` only because `lib.rs` is not the parent module, and accepted `stringify!{mod lock;}` and `macro_rules!` bodies in the parent. In comments, `renamed_comment` changes only `crate::` paths: a comment has no certain scope (an outer doc on an inline `mod` can resolve inside or outside it), so the gate refuses a change to a `self::` or `super::` path in a comment.
- The repo base already carries two basedpyright errors (missing `bpy` and `PIL`), so a gate reads as "no new diagnostics".
- Retired fix-pipeline names still exist outside history: `config/agents.conf` (header comment, `style_fix_review=` lines, `[fix.codex]`, `[fix.claude]`), `settings.json` (`nightly_weights`, `style_usage`), `CLAUDE.md:4` (`/style_eval`), and `.claude/settings.local.json` (`scripts/fix/` permissions). These edits wait for the user.

## Why

- **Rule ids, not stems:** two rules can share a stem (a global and a member-local rule).
- **A refill lock, not a judge tie-break:** without the lock, units that refill at the same time all read the ledger before any of them adds a ticket. They then get the same files, and their judges yield to each other's reservations. With the lock, two units cannot select the same file, so judges need no tie-break rule.
- **`load_rules` reads the folder layout:** `load-rust-style.sh` filters drop tool-enforced and Bevy rules.
- **No generator is `surface: whole`:** the generators miss cases (unlisted registrations, bare-name trait counting), so each listed rule needs a tier C read of its uncovered surface.
- **Unclassified, not an error:** a machine whose nate_style lacks metadata must still load and list, but never count coverage.
- **No per-site progress in a listed cell:** tickets split at file boundaries instead, so a file cell holds one judgment.
- **Blob export, not `git archive`:** `export-ignore` drops files and `export-subst` rewrites them.
- **Pinned text in a ledger table:** `git gc` can prune loose objects; private refs outlive terminal tickets.
- **No `cargo-berth check` in selection:** it acquires reservations.
- **Lint in check mode:** a check never touches the tree, so no rewrite-and-rollback is needed.
- **`--ff-only` for standing units:** a `--no-ff` merge breaks no-change ticket ancestry. No-change tickets make no empty commits, which would carry empty merges into the branch point.
- **Delivery state from git, not stored:** a single local ancestry check would acknowledge an unpushed merge after a failed push.
- **A hand-off is not a unit phase:** it is the child's work landing, so it counts toward no unit CI point. Style files go in as `--also`, so no scope code changes.
- **`style_plan.py gate` serves both gates:** one place reads `SelectedByCoverage.base` and the proposal rules.
- **Per-file structure cells and per-file blocking:** a crate-wide structure cell held every other family off a whole crate; per-file cells let values and names work start as soon as a file's own structure check is done.
- **Two blind judges on names read tickets, and a when-to-split step on structure tickets:** one judge, or no split question, missed the 95% recall bar in the pilot.
- **One decision per rule, crate and blob:** an answer covers later findings under the same text, so the user answers once. Per-batch issues made the user answer the same question many times.
- **Ledger owns finding-to-decision state:** frontmatter alone cannot prove a closed decision's findings are resolved.
- **`withdrawn` is its own state:** it is not the user's waive, and it records the judgment or reason that withdrew it.
- **No refill skip keyed on waived findings:** refill compares only the file blob, so such a skip could hide a cell whose rule text or context changed.
- **Approval read twice in the follow step:** a decision that loses approval between plan and write must not build.
- **Named outcomes, not bools:** each declaration outcome is its own type, so each consumer must handle it.
- **No leaf-name trait match, no fallback past an unsupported import, no general name resolution:** each gave wrong follows. Resolving `extern crate` aliases gave two wrong follows live.
- **No best-score pick among outside-rename candidates and no prose proof:** only a unique, body-checked destination is a proof.
- **Decision path check before and inside the lock:** a check before the lock alone lets two same-key runs pass, and the second fails at `os.link`. `os.replace` would overwrite the other run's decision.
- **`--reads` digest for rule-scope cells:** a rule-scope cell keyed on its crate or module fingerprint came back after any change there, and each repeat spent a Codex ticket on no work. A digest of only the files the verdict rests on brings it back only when one of them changes.
- **Fixed lock folder:** a lock folder read from the environment can differ between two runs, so two `style_proposals.py file` runs held different locks on one issue and the second failed at `os.link` (Errno 17). A fixed folder gives every run one lock, and a same-key `FileExistsError` at the link ends the run without error.
- **Replace takes only a unit's waive:** a person's waive is the user's decision, and a ledger waive marks a site that can no longer build; only a unit may correct its own finding's text. An old waive with no record needs a named person, as unwaive does.
- **`active` stage for in progress:** the vault already ranks `active` as in progress, so no new stage value is needed.
- **Wrap detaches style children last:** the style child must stay attached until the parent's close-out steps finish.
- **A build Goal and a plain scope line for proposal tickets:** the old proposal Work Order said `Judge the proposal ticket ...` and that the approved proposal `overrides the mechanical-only invariant in the copied context`. With no build model, a unit director copied and rewrote another unit's `/tmp/claude/unit` prompt files, and auto mode refused the dispatches as a bypass. The build Goal gives the director the build model, and a scope fact gives it no override to work around.
- **Off-rule ideas become rule candidates:** an off-rule build ticket has no rule to judge; approved off-rule work builds as plain code phases.
