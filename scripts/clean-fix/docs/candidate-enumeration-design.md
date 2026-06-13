# Helper-Enumerated Candidates — Design + Guideline Audit

2026-06-12. Follow-up to the TTL re-sampling work (quota=6, ttl=3d, fingerprint
gating, finalize refusal — all shipped in `style_history.py`).

## Problem

A style-eval unit today is "agent, go find violations of rule X in this
project." The agent both **enumerates** candidate sites and **judges** them.
Enumeration is the unreliable half: re-running the same unit on an unchanged
project finds new results, because the agent's search is a sample, not a
sweep. TTL re-sampling compensates by re-drawing samples over time, but recall
stays probabilistic for every rule where it is possible to do better.

## Design

Split the two halves. The helper enumerates; the agent only judges.

1. **Generator per unit.** Each guideline that admits one gets a *candidate
   generator* — a deterministic procedure that lists every site the rule could
   apply to. The generator must produce a **superset of violations**: false
   candidates are fine (the agent dismisses them), missed violations are not.
2. **`next-unit` returns candidates.** For a generator-backed unit, the unit
   payload gains a `candidates` array (`{file, line, text}` or `{file}` for
   file-level rules) plus `candidate_source` (how it was produced, for
   auditability).
3. **Agent dispositions, never enumerates.** For each candidate the agent
   returns `violation` or `exception: <clause>`. Its output is a closed list
   keyed by candidate index — there is nothing to "search", so there is
   nothing to under-search.
4. **`record-unit` enforces closure.** The helper re-runs the generator
   (deterministic, cheap) and refuses the record if any candidate lacks a
   disposition. An early-quitting agent cannot silently narrow coverage; this
   composes with the finalize-refusal gate already in place.

Rules with no feasible generator keep today's flow: agent enumeration,
unreliable recall, TTL re-sampling as the recall mechanism. The design's
value is shrinking that set to the rules where it is genuinely irreducible.

## Generator classes

The audit below assigns each eval-pool unit one class:

| Class | Generator | Determinism |
|---|---|---|
| **lint** | run the named clippy/mend lint, capture flagged sites | total — tool output is the candidate list |
| **regex** | `rg` pattern (today's `pre_filter`, promoted to enumerator) plus mechanical line-level excludes | total |
| **parse** | small structured generator in `style_history.py` — brace/field-aware Python, or `toml`/`cargo metadata` for manifest rules | total |
| **semantic** | none feasible — surface is meaning, not text shape | n/a — keeps agent enumeration + TTL |
| **process** | none — rule governs agent behavior during *fixes*, not a sweepable code surface | n/a — should leave the eval pool |

`lint`-class units in `mode: flag`/`propose` are the cheapest wins: the
detection tool already exists; the work is piping its output through the unit
payload instead of asking the agent to re-derive it.

## Frontmatter contract

The generator spec lives in the guideline file, next to the prose it
formalizes (same placement rule as `pre_filter` today). `pre_filter` remains
valid for skip-gating; `candidates` supersedes it when present.

```yaml
# regex class — pattern is the enumerator
candidates:
  kind: regex
  pattern: '\bMessage(Reader|Writer)<'

# lint class — tool output is the enumerator
candidates:
  kind: lint            # uses existing `mechanism` + `lint:` keys

# parse class — named generator implemented in style_history.py
candidates:
  kind: literals        # e.g. for no-magic-values
  include: [numeric, string, char]
  exclude: [const_def_line, comment, cfg_test_span, identity_literal,
            range_start_zero, format_body]
  paths_exempt: [examples/, benches/, build.rs]
```

A `kind` without an implementation in `style_history.py` is a loud config
error at `next-unit` time, not a silent fallback to agent enumeration.

## Volume control (the no-magic-values problem)

Measured on cargo-port: all literals ≈ **9,000** lines; numerics of 2+ digits
outside `const` lines ≈ **900**. Per-candidate disposition at that volume is
infeasible, so broad rules need two tiers:

1. **Mechanical excludes in the generator** — every Exceptions clause that is
   textual or path-shaped is applied before the agent sees the list
   (`const` definition lines, comments, `examples/`/`benches/`/`build.rs`,
   identity literals, `0..n` range starts, format-string bodies,
   `include_str!` paths, `#[cfg(test)]` spans via brace-aware filtering).
2. **Agent judgment on the remainder** — the surviving sites (tens, not
   hundreds) carry the genuinely semantic exceptions (match-arm enum labels,
   English connectives, factory-alias consts) and get per-candidate
   dispositions.

Excludes must stay conservative: an exclude that can suppress a real
violation breaks the superset guarantee and is wrong even if it shrinks the
list. When in doubt, leave the candidate in and let the agent dismiss it.

## Payload and enforcement protocol

`next-unit` (generator-backed unit):

```json
{
  "unit_id": "rust/never-prefix-unused-fields-or-variables-with.md",
  "candidates": [
    {"index": 0, "file": "src/drag.rs", "line": 14, "text": "    _offset: Vec2,"},
    {"index": 1, "file": "src/timer.rs", "line": 9,  "text": "let _timer = Timer::new(\"analyze\");"}
  ],
  "candidate_source": "regex:'(let|[,(]\\s*)_[a-z]\\w+'",
  "candidate_count": 2
}
```

Agent's `record-unit` input gains:

```json
"dispositions": [
  {"index": 0, "verdict": "violation"},
  {"index": 1, "verdict": "exception", "clause": "RAII guards"}
]
```

`record-unit` re-runs the generator; mismatched count or missing indices →
refuse with a nonzero exit, same loud-failure posture as
`finalize-no-findings` (`EXIT_INCOMPLETE_RUN`). Zero candidates → the unit
records as a pre-filter-style skip with no agent involvement at all (extends
today's free pre_filter skip to every generator-backed unit).

## Audit — all 61 shared guidelines + cargo-port overlays

Pool mechanics, confirmed against `style_history.py` and
`load-rust-style.sh`:

- `mechanism: clippy|mend|rustfmt` + `mode: auto` (14 files) — excluded from
  the loader entirely; tooling auto-fixes. Never eval units.
- `non-negotiable` tag (2 files) — loaded as context, `budget_cost=0`, not
  eval units.
- Everything else is an eval unit. cargo-port: 41 shared in pool + 5 local
  − 2 non-negotiable = **44 reviewable units** (matches `due-units`).
- Bevy-only guidelines join the pool only for bevy projects.

### Eval-pool units (shared guide, 45)

| Unit | Class | Proposed generator | Notes |
|---|---|---|---|
| agent-must-review-allows | regex | `#\[allow\(` lines lacking `reason` | reason-bearing allows pre-excluded per the rule's own exception |
| always-use-nextest | regex | `cargo test\b` across scripts/CI/docs | tiny surface |
| avoid-repeated-field-affixes | parse | struct-field groups sharing a prefix/suffix | field grouping needs brace-aware parse; 6 exception clauses → agent judges |
| backtick-names-in-comments | lint | clippy `doc_markdown` | flag mode — lint output = candidates |
| bevy-plugin-ownership | regex | `impl Plugin for` sites | per-site judgment on delegation |
| bevy-reflection-registration | regex | existing pre_filter `\.register_type::<` | each match judged generic vs not |
| comments-name-code-constructs | semantic | — | vagueness has no textual shape |
| derive-test-values-from-production-constants | parse | literals inside `#[cfg(test)]` spans | reuses the no-magic-values literal machinery; agent judges "derived from a constant" |
| dont-create-traits-for-single-implementations | parse | trait decls + cross-file impl count ≤ 1 | counting is mechanical |
| dont-repeat-enum-domain-in-variant-names | parse | enum variants sharing the enum's leading word | fixed-compound-term exception judged |
| dont-repeat-type-name-in-fields | parse | fields prefixed with struct's snake_case | trigger_test frontmatter already states the mechanical rule |
| enums-over-bool-for-owned-booleans | regex | `\bbool\b` in field/arg/return/`let mut` positions | "not owned" exceptions judged |
| exception-std-paths-are-allowed-inline | lint | mend `inline_path_qualified_type` | body says mend already detects + auto-fixes — **consider flipping to `mechanism: mend, mode: auto` and exiting the pool** |
| extract-observer-run-conditions | parse | observer fns opening with early-return guard | brace-aware; propose mode |
| fix-root-causes-never-workarounds | process | — | no sweepable surface; burns a unit every run — **tag out of pool** |
| if-else-chains-signal-missing-types | regex | `else if` chains (2+) | superset; agent judges missing-type signal |
| import-constants-at-the-top | regex | inline `module::UPPER_CASE` paths | CamelCase qualifier (assoc consts) mechanically excluded |
| leaf-module-visibility | lint | mend `suspicious_pub`, `unused_pub`, `internal_parent_pub_use_facade` | flag mode; structural-exposure exceptions judged |
| make-functions-const-fn-when-possible | lint | clippy `missing_const_for_fn` | |
| methods-that-dont-use-self… | lint | clippy `unused_self` | |
| module-roots-as-table-of-contents | parse | module roots containing items beyond `mod`/`use`/`pub use` | file-level candidates; 3 exception clauses judged |
| module-structure | semantic | — | intra-file ordering judgment; partial mechanization possible later |
| name-bindings-to-match-parameters | semantic | — | needs call-site parameter names (type info), not text shape |
| name-submodules-after-anchor-types | parse | filenames on the anti-pattern list + parent-stutter names | catches the worst violations mechanically; anchor-name fit stays judgment |
| never-allow-clippy-too-many-lines | lint | clippy `too_many_lines` | trivial-exhaustive-match exception judged |
| never-bare-allowdeadcode | lint | clippy `allow_attributes_without_reason` | |
| never-prefix-unused-fields-or-variables-with | regex | `_`-prefixed bindings/fields/params | RAII-guard + unit-type exceptions judged |
| never-use-pub-mod | lint | mend `review_pub_mod` (pre_filter exists too) | every match a candidate |
| no-magic-values | parse | `literals` generator with tiered excludes | the volume case — see Volume control |
| no-pub-in-path | lint | mend `forbidden_pub_in_crate` (pre_filter exists) | |
| no-wildcard-reexports | lint | mend `wildcard_parent_pub_use` | |
| prefer-events-over-messages | regex | existing pre_filter `\bMessage(Reader\|Writer)<` | propose mode; batch/timing exceptions judged |
| prefer-from-impl-over-named-constructors | regex | `fn from_\w+\(` associated fns | 4 named-constructor exceptions judged |
| prefer-functional-patterns | semantic | — | "arms are simple expressions" is judgment |
| prefer-observers-over-polling | semantic | — | per-frame-polling detection is judgment |
| prefer-type-named-fields-and-bindings | parse | field name ≠ snake_case(field type) | mechanically checkable from struct fields; "reads naturally" judged |
| spell-out-names | semantic | — | abbreviation-compound heuristics too false-positive-heavy to keep superset useful |
| split-by-type-ownership | semantic | — | module-boundary judgment |
| test-module-allow-boilerplate | parse | allows on `#[cfg(test)]` modules; verify lint vs `.unwrap(`/`.expect(`/`panic!(` matches in scope | **near-fully mechanizable** — the rule's own "verify before flagging" section is an algorithm |
| types-live-with-their-behavior | semantic | — | relocation check needs whole-codebase behavioral reasoning |
| use-a-context-struct-when-arguments-exceed-7 | lint | clippy `too_many_arguments` (threshold 7) | **migration candidate: flip to `mechanism: clippy, mode: flag`** |
| use-bevy_kana-in-all-bevy-crates | parse | Cargo.toml dep presence + `bevy_kana` types in pub positions | dep check trivial; API-leak check parse |
| when-to-split-a-module | semantic | — | composite criteria; mechanical pre-signal (>500L non-test) can seed but not decide |
| workspace-dependencies | parse | member `[dependencies]` entries with `version=` instead of `workspace = true` | **near-fully mechanizable** via toml parse |

### Non-negotiable (context-only, not eval units)

| Unit | Class | Notes |
|---|---|---|
| forbidden-words | regex | pre_filter regex is already a full generator; enforced via /rust_style + /style_eval prompts |
| public-api-changes-require-explicit-approval | process | governs fix behavior; correctly outside the pool |

### cargo-port local overlays (5)

| Unit | Class | Proposed generator |
|---|---|---|
| adding-a-keybinding | regex | `KeyCode::` matches outside `src/keymap.rs` |
| detail-pane-fields | parse | `String`-typed fields on `PackageData`/`GitData` |
| frontend-boundaries | parse | imports of TUI-internal types (`App`, `PaneId`, ratatui, crossterm) from `src/*` outside `src/tui/` |
| trait-tutorial-format | process | governs doc-writing, not code — **tag out of pool** |
| vim-mode-reserves-hjkl | regex | bare h/j/k/l action bindings in keymap defaults |

### Class totals (45 shared eval units)

| Class | Count | Recall after this design |
|---|---|---|
| lint | 12 | total — tool enumerates |
| regex | 10 | total — pattern enumerates, agent dismisses |
| parse | 13 | total — generator enumerates, agent dismisses |
| semantic | 9 | unchanged — agent enumeration + TTL re-sampling |
| process | 1 | should leave the pool |

**36 of 45 units (80%) can have deterministic enumeration.** The semantic
residue (9 units) is exactly the set TTL re-sampling exists for.

## Review decisions (2026-06-12)

1. **lint class (12 units): DELETED from the style guide entirely**
   (executed 2026-06-12 via `style_admin.py delete`; ~1,450 history entries
   cleaned, see_also/wikilinks rewritten). Rationale: clippy + cargo mend run
   in every fix worktree (`style-fix-worktrees.sh`) and in all manual
   validation workflows, so tool-detectable rules are enforced wherever work
   happens; tracking them as style units is redundant. Deleted:
   backtick-names-in-comments, make-functions-const-fn-when-possible,
   methods-that-dont-use-self…, never-allow-clippy-too-many-lines,
   never-bare-allowdeadcode, used-underscore-binding…, leaf-module-visibility,
   never-use-pub-mod, no-pub-in-path, no-wildcard-reexports,
   exception-std-paths-are-allowed-inline,
   use-a-context-struct-when-arguments-exceed-7. The last relies on clippy's
   warn-by-default `too_many_arguments`; its "Bevy systems exempt" nuance now
   lives only in git history. Shared eval pool: 45 → 33 units. This makes the
   item-5 mechanism flips moot.

2. **regex class (10 units): APPROVED.** Promote `pre_filter` patterns to
   candidate enumerators; author patterns for the 7 units that lack one.
   Every match becomes a candidate the agent dispositions; `record-unit`
   refuses undispositioned matches; zero-match runs record free.

3. **parse class (13 units): APPROVED.** Build the structured generators in
   `style_history.py` per the rollout order: `workspace-dependencies` and
   `test-module-allow-boilerplate` first (near-total automation), then the
   field/enum naming rules on a shared struct-parse helper, then the tiered
   `literals` generator for `no-magic-values` last (the volume case, with
   mechanical exception tiers applied before agent judgment).

4. **semantic class (9 units): ACCEPTED as-is.** No generator — the judgment
   is the detection, so a mechanical enumerator would flag everything or
   nothing. These keep agent enumeration; TTL=3d re-sampling is their recall
   mechanism. The only units whose coverage still depends on agent diligence.

5. **process class (2 units): APPROVED — tag out of the eval pool.** Add the
   `non-negotiable` tag to `fix-root-causes-never-workarounds` (shared) and
   `trait-tutorial-format` (cargo-port local) so they get `budget_cost=0`:
   still loaded as context by `/rust_style`, never eval units. They govern
   agent behavior during work, not sweepable code state. Shared pool 33 → 32;
   cargo-port locals 5 → 4. The mechanism-flip proposals originally in this
   item are moot (those files were deleted in decision 1).

## Rollout

Updated 2026-06-12 to reflect the review decisions: the lint class was deleted
from the style guide outright (decision 1), so there is no lint wiring step
and no mechanism flips.

**Generator placement:** parse-class generators live in a new sibling module
`~/.claude/scripts/clean-fix/candidate_generators.py`, imported by
`style_history.py`. Single entry point
`enumerate_candidates(unit, project_dir) -> list[Candidate]`, dispatched on
the unit's `candidates.kind` frontmatter — so `next-unit` and `record-unit`
share the identical enumeration code path. The regex kind needs no Python:
its pattern lives in guideline frontmatter and the dispatcher runs it
directly. Keeping generators out of `style_history.py` (already ~1,800 lines
of run/pool/history mechanics) keeps them pure functions, unit-testable
without history-file state.

1. **Pool hygiene (no generator work):** add the `non-negotiable` tag to
   `fix-root-causes-never-workarounds` and `trait-tutorial-format` so they
   leave the eval pool (decision 5). Shared pool 33 → 32; cargo-port locals
   5 → 4.
2. **Promote regex class (10 units):** `pre_filter` → `candidates.kind: regex`;
   author patterns for the 7 that lack one (decision 2).
3. **Parse class, narrow first:** `workspace-dependencies` and
   `test-module-allow-boilerplate` (near-total automation), then field/enum
   naming rules (shared struct-parse helper), then the `literals` generator
   for `no-magic-values` last (the volume case) (decision 3).
4. **Semantic units:** no change; TTL re-sampling is their recall mechanism
   (decision 4).

Each unit flips independently when its spec lands — no big-bang migration.
`record-unit` treats a unit without a `candidates` spec exactly as today.

## Implementation status (2026-06-12)

Implemented same day as the review. `candidate_generators.py` (new module) +
wiring in `style_history.py` (`next-unit` attaches `candidates`/
`candidate_source`/`candidate_count`; `record-unit` re-runs the generator and
refuses undispositioned records with exit 3; zero-candidate units free-skip
with `skipped_by: candidates`; new `enumerate-candidates` debug subcommand).
`/style_eval` documents the judge-only protocol and the `dispositions` array.
Generator output above `MAX_CANDIDATES = 150` is a loud config error.

Smoke-tested every generator against nateroids, obsidian_knife, cargo-mend,
cargo-port, hana, and bevy_brp_bevy_update. Deviations from the audit's
class assignments, all volume-driven (counts are cargo-port):

- **`prefer-type-named-fields-and-bindings`: reverted to semantic.** The
  mechanical surface is every typed field/binding whose name isn't
  snake(type) — 1,376 candidates. "Reads naturally" is the whole rule; no
  conservative mechanical narrowing keeps the superset property.
- **`no-magic-values`: generator implemented but not enabled.** Even with the
  full exclude tiers, numerics+strings enumerate 3,386 sites. The `literals`
  kind stays registered; enabling it waits on disposition persistence (or
  per-file aggregation). The unit stays agent-enumerated + TTL.
- **`enums-over-bool-for-owned-booleans`: narrowed to struct fields**
  (`struct_fields` kind, `field_type_pattern: '^bool$'`). The full
  fields+args+returns regex surface was 505 sites; fields-only is 55.
  Args/returns/accumulators fall back to the generator-drift mitigation.
- **`agent-must-review-allows`: parse kind `allows_without_reason`,** not
  regex — multi-line `#[allow(...)]` attrs put `reason =` on its own line,
  which a line-based exclude can't see (98 false candidates → 0).
- **`avoid-repeated-field-affixes`: group must cover all fields** of the
  struct, matching the rule's "sibling fields all repeat" (177 → 9).
- **`derive-test-values-from-production-constants`: assert/comparison lines
  only** (158 → 16) — the sites where a drifted constant breaks silently.
- **`adding-a-keybinding` (cargo-port): paths_exempt extended** to
  `src/tui/keymap`, `src/tui/keymap_ui`, tests, and integration suites — the
  keymap system itself and simulated key presses can't be dispatch
  violations (191 → 38).

Result: 21 shared units + 4 cargo-port locals are generator-backed; the
shared eval pool is 32 units (21 generator-backed, 11 agent-enumerated).
Every guideline file edited today re-enters the due pool once via the
file-mtime trigger — one full re-review sweep after this migration is
expected.

## Open questions

- **Quota weighting.** A 60-candidate unit and a 2-candidate unit both cost 1
  toward the 6-unit quota. Ship flat first; revisit with real run-time data.
- **Disposition persistence.** Per-candidate verdicts could be stored on
  history rows, making "exception" decisions durable across runs (a dismissed
  candidate at an unchanged file:line need not be re-judged). Deferred —
  interacts with the fingerprint/TTL machinery.
- **Generator drift.** If a generator regex/parse rule misses a violation
  class, recall is silently capped. Mitigation: semantic-style free-search
  remains available via TTL re-sampling on a slow cadence even for
  generator-backed units, or a periodic "completeness critic" pass.
