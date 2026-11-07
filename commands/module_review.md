---
description: Review module structure in the invoking session.
---

**IMPORTANT**: Do NOT modify any source code. This command produces a planning doc only — implementation lives in a separate command or follow-up turn.

## Arguments

`$ARGUMENTS` is two whitespace-separated tokens, both optional:

```
/module_review [scope] [doc-path]
```

- `scope` — directory to evaluate, relative to repo root. Accepts either an `src/` directory or a workspace-member root (in which case `src/` is appended automatically).
  - Single-crate examples: `src/`, `crates/foo/src/`.
  - Workspace-member examples: `crates/bevy_diegetic`, `crates/bevy_diegetic/src/`.
- `doc-path` — where the plan is written. Examples: `docs/source_modules.md`, `crates/bevy_diegetic/docs/modules.md`.

If either is missing, infer from the current conversation; if inference fails, ask.

If `doc-path` already exists, ask the user before writing:

```
Doc already exists at <path>. Overwrite / append / cancel?
```

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <ResolveScope/>
**STEP 2:** Execute <InventoryScope/>
**STEP 3:** Execute <Pass1Discovery/>
**STEP 4:** Execute <WritePlacementPlan/>
**STEP 5:** Execute <Pass2SplitPlanning/>
**STEP 6:** Execute <Pass3Validation/>
**STEP 7:** Execute <RefinePlan/>
**STEP 8:** Execute <PresentFinal/>
</ExecutionSteps>

---

<ResolveScope>
**Goal:** Pin down what is being reviewed, detect workspace context, and decide where the plan lands.

- Resolve `scope` to an absolute directory under the repo. If the user passed a workspace-member root (a directory containing `Cargo.toml` but no `src/` suffix), append `src/`. Verify the final directory exists.
- Detect workspace membership: starting from `scope`, walk up looking for a `Cargo.toml`. If the nearest `Cargo.toml` is a workspace member (its parent or some ancestor has a `[workspace]` `Cargo.toml`), capture:
  - `${CRATE_ROOT}` — directory containing the member `Cargo.toml`.
  - `${CRATE_NAME}` — value of `name = "..."` in `[package]`.
  - `${IS_WORKSPACE_MEMBER}` — `true`.
  Otherwise set `${IS_WORKSPACE_MEMBER}` to `false` and leave the other two empty.
- Resolve `doc-path`. If missing, propose one and ask the user to confirm:
  - Workspace member → `${CRATE_ROOT}/docs/modules.md`.
  - Single crate → `docs/source_modules.md` (or `docs/<segment>_modules.md` when `scope` has a clearer name segment).
- If `doc-path` exists, ask: `Doc already exists at <path>. Overwrite / append / cancel?` and act accordingly. On cancel, stop.

State one line: `Reviewing <scope>[ (crate: <CRATE_NAME>)]. Plan will be written to <doc-path>.`

Store `${SCOPE}`, `${DOC_PATH}`, `${CRATE_NAME}`, `${CRATE_ROOT}`, and `${IS_WORKSPACE_MEMBER}` for later steps.
</ResolveScope>

---

<InventoryScope>
**Goal:** Build one file inventory for all lenses.

Run `find ${SCOPE} -type f -name "*.rs" | sort` and capture the output as `${INVENTORY}`. Also capture per-file line counts via `wc -l`.

Identify immediate subdirectories with a `mod.rs` and singleton `.rs` files at each layer.
Use this inventory for every lens.
</InventoryScope>

---

<Pass1Discovery>
**Goal:** Review module structure through three lenses.

The invoking session runs each lens itself, one after the other: A, B, then C.

1. Create an absolute `${SESSION_DIR}` under `/tmp/claude/module_review/<uuid>/` for the findings.

Run this preamble once, before lens A.

```
Before evaluating:
1. Read ~/rust/nate_style/review-charter.md — its ranked values, hard rules, and finding schema govern every finding you return.
2. Load the Rust style guide: zsh ~/.claude/scripts/rust_style/load-rust-style.sh

If the output mentions a saved file path, Read that file. Apply the loaded rules — especially `when-to-split-a-module.md` for the over-large-file criterion (~500 lines non-test + at least one other signal) — when forming findings. Cite the rule filename for each finding it informs.

The style guide includes `forbidden-words.md`. Apply that file's rules to every word of every finding you return.
```


**Lens A — Cohesion**
> Evaluate **module cohesion** across `${SCOPE}`. For each module (top-level and each subdirectory), answer: do the files belong together? Name split candidates, merge candidates, and misplaced singletons. Output findings as numbered list: Title / Where (file paths) / Observation (concrete — name types/functions) / Severity (critical / important / minor) / Recommendation (move / merge / split / rename / leave-as-is with target path).

**Lens B — Coupling and boundaries**
> Evaluate **module coupling and boundary discipline** across `${SCOPE}`. Map who-imports-whom. Surface unhealthy directions (domain → UI, leaf → aggregator). For library/binary boundaries: identify generic code stuck in app-specific homes and app-specific code leaked into generic homes. Output findings same format as Lens A; cite actual `use` paths.
>
> If `${IS_WORKSPACE_MEMBER}` is true, also search **sibling workspace crates** for inbound imports via `rg "use ${CRATE_NAME}(::|;|\s+as\s+)" --type rust` across the whole repo (excluding `${CRATE_ROOT}`). Flag public API surface that's wider than its actual external use, and inbound paths that reach into deep submodules instead of crate-root re-exports.

**Lens C — Root-vs-submodule placement**
> Evaluate **root-vs-submodule placement** across `${SCOPE}`. Policy: every file lives either at the crate root (genuinely top-level concerns) or inside a directory submodule that groups files by responsibility. No flat sprawl of unrelated single-file modules at any layer.
>
> **Hard rule — singleton budget of 6 per layer.** If any layer has more than 6 singleton `.rs` files, you MUST propose organizing principles that bring the count down. "Top-level concern" is the *exception*, not the default — most files have peers along some responsibility axis if you look.
>
> Method:
> 1. **Enumerate.** For every singleton at every layer, list its filename plus a one-line statement of what it *does* (read the top 30 lines if the name is ambiguous — names like `runner.rs`, `selection.rs`, `outcome.rs` rarely tell you enough).
> 2. **Try every axis.** Walk through these axes and group singletons that share one:
>    - **Entry & orchestration** — main, lib, top-level runner, exit-code/outcome types
>    - **CLI surface** — argv parsing, flag types, mode enums
>    - **Configuration & constants** — config loading, constants, env var names, feature flags
>    - **Domain types** — the central nouns the rest of the code passes around (findings, reports, the core enum the whole crate revolves around)
>    - **I/O & rendering** — JSON output, human-readable output, filters, formatters
>    - **Persistence & caching** — disk layout, serde formats, cache invalidation
>    - **External-process drivers** — wrappers around cargo, rustc, git, the shell
>    - **Generic utilities** — path helpers, string helpers, byte-offset helpers, AST helpers
>    - **Cross-cutting metadata** — diagnostic codes, severity, support tables consulted from many places
>    - **Tests-vs-production** — test fixtures, golden files, snapshot infrastructure
> 3. **Propose groupings.** Whenever two or more singletons share an axis, propose a directory. Aim for the singleton count at every layer to drop to ≤6 after grouping. If it can't, say why explicitly.
> 4. **Defend any remaining singleton.** A file may stay at root only if it satisfies BOTH: (a) it is referenced directly from `main.rs` or the crate-root `lib.rs`, AND (b) it has no peer along any axis in the same layer. Generic "this is a top-level concern" is not a defense — name the axis the file is alone on, and confirm no other root-level file shares it.
>
> Output findings same format. End with a final tree sketch of the proposed layout (directories only, plus one or two example files per directory). The tree MUST show ≤6 singletons at every layer that started above the threshold, OR include an explicit per-file defense for every singleton above the budget.

After lens C, list every over-large file in `${SCOPE}` once.
Use the inventory line counts and the `awk` test-block check.
Cite `when-to-split-a-module.md` and the criteria met.

1. Write `${SESSION_DIR}/pass1_findings_<letter>.txt` after each lens, before the next lens.
2. Write an explicit no-findings result when the lens finds no issue.
3. Collect the three results as `${PASS1_FINDINGS}` and the flagged files as `${OVERSIZE_FILES}`.
4. Report any incomplete lens before synthesis.

Never treat an incomplete review as a no-findings result.
</Pass1Discovery>

---

<WritePlacementPlan>
**Goal:** Synthesize pass 1 into a confident placement plan and write it as Phase 1 of `${DOC_PATH}`.

Merge and dedupe findings. Resolve disagreements with judgment — pick the cleanest layout, not the union of all proposals. Do **not** list dismissed alternatives. The plan is the recommendation.

**Singleton-count gate (blocking).** Before writing the plan, count singletons at every layer of the proposed tree, including the root. If any layer has more than 6, the placement work is unfinished:

- Return to Lens C's per-axis enumeration and pick at least one more grouping until the count drops to ≤6, **or**
- For each remaining singleton above the budget, write an explicit defense in the doc citing BOTH criteria: (a) referenced from `main.rs`/`lib.rs`, AND (b) no responsibility-peer along any axis in that layer. The defense names the axis the file is alone on.

A blanket "the remaining files are top-level concerns" sentence is not acceptable. If the gate fails, do not proceed to write the doc — re-do the synthesis with stronger grouping pressure first.

The proposed tree is a hypothesis the gate is testing. Treat a layer with >6 singletons after gating as a finding that needs an answer, never as the resting state.

Write `${DOC_PATH}` with this skeleton:

```markdown
# <Title derived from scope> restructure plan

<one-paragraph target statement>

## Phase overview

| Phase | What | Risk | Rough size |
|-------|------|------|------------|
| 1 | Placement — group files into directories | Low | <est imports>, one commit |
| (phases 2+ filled in by pass 2 if over-large files exist) |

## Phase 1 — Placement

### Proposed layout
<full tree>

### Moves, with rationale
<one subsection per new directory: what moves in, why these belong together>

### What stays where
<files that look misplaced but aren't, with the reason>

### Module re-exports
<one `mod.rs` block per new directory>

### Sequencing
<numbered steps; explain dependency ordering. Steps are in-flight
checkpoints — run the cargo checkpoint commands between them — and
the whole phase lands as a single commit once green. Use
`cargo build -p ${CRATE_NAME}` + `cargo nextest run -p ${CRATE_NAME}`
when reviewing a workspace member; bare `cargo build` +
`cargo nextest run` otherwise.>
```

Do not cite lens reports. Do not include "options A vs B". State the recommendation.
</WritePlacementPlan>

---

<Pass2SplitPlanning>
**Goal:** For each over-large file flagged in pass 1, design a split and append it as a follow-on phase.

**If `${OVERSIZE_FILES}` is empty:** print one line — `No over-large files in scope. Skipping pass 2.` — and proceed to pass 3.

Otherwise:

1. Review all `${OVERSIZE_FILES}` through lenses D, E, then F.
2. Use the pass 1 finding format for each lens.
3. Write `${SESSION_DIR}/pass2_findings_<letter>.txt` before the next lens.

**Lens D — Production boundaries**
> For each file in `${OVERSIZE_FILES}`: group top-level items by responsibility. Propose a submodule layout (`<file>/mod.rs` + N submodules), citing line ranges, type names, and function clusters. Apply `name-submodules-after-anchor-types.md` and `split-by-type-ownership.md`. Output per file: target layout, what goes where (current line ranges + items), sequencing for the split (leaves first).

**Lens E — Production-vs-test ratio**
> For each file in `${OVERSIZE_FILES}`: compute production-line vs inline-test-line counts (use `awk '/^#\[cfg\(test\)\]/{print NR; exit}'` to find the test block boundary). If a file is >50% tests, the right action is test extraction (move tests to a `tests/` directory), not a module split. Flag any such file and propose the test-extraction target path. For genuine production-heavy files, confirm the split is warranted by `when-to-split-a-module.md`.

**Lens F — Call-site impact** (uses D's findings)
> For each file in `${OVERSIZE_FILES}` and each proposed submodule in D's recorded proposed layouts: count caller `use` statements that reference the file and predict which submodule each caller now needs.
>
> Search both intra-crate and (when applicable) external workspace callers:
> - Intra-crate: `rg "use crate::<path>::<file>" --type rust` inside `${CRATE_ROOT}` (or repo root for single-crate projects).
> - External workspace (only when `${IS_WORKSPACE_MEMBER}` is true): `rg "use ${CRATE_NAME}::<path>::<file>" --type rust` across the whole repo, excluding `${CRATE_ROOT}`. Also check for re-exports at the crate root that would shield external callers from the move.
>
> Identify dependencies between proposed submodules (which import which) to determine extraction order. Surface any cycle or visibility hazard.

Read all three findings files before synthesis.
Apply the pass 1 rule for no-findings results and incomplete lenses.

Synthesize: append a `## Phase N — Split <file>` section to `${DOC_PATH}` for each file, in order of size or risk. Each phase contains:
- Target layout (directory tree).
- What goes where (line ranges + items).
- Sequencing (leaves first, with dependency reasoning). Steps are in-flight checkpoints — run the cargo checkpoint commands between them — and the whole phase lands as a single commit once green.
  - When `${IS_WORKSPACE_MEMBER}` is true, the checkpoint commands are `cargo build -p ${CRATE_NAME}` + `cargo nextest run -p ${CRATE_NAME}`.
  - Otherwise, `cargo build` + `cargo nextest run`.

For test-extraction-only files (Lens E's verdict), the phase has no internal sequencing steps — it's a straight test move. Say so.

Update the phase-overview table at the top of the doc to include the new phases. Each row's "Rough size" column should end with "one commit".
</Pass2SplitPlanning>

---

<Pass3Validation>
**Goal:** Validate the complete plan through three lenses.

1. Run lenses G, H, then I yourself, one after the other.
2. Read `${DOC_PATH}` once, before lens G. Compare it with the current code in each lens.
3. Use the pass 1 finding format.
4. Write `${SESSION_DIR}/pass3_findings_<letter>.txt` before the next lens.

**Lens G — Completeness**
> Walk every `.rs` file in `${SCOPE}` (the inventory from step 2). Confirm each is either explicitly moved, explicitly kept where it is, or covered by a "stays where" section. Check rename consistency across the doc. Check that each new directory's `mod.rs` re-export block accounts for every type currently imported via the old path. Flag sequencing chicken-and-egg cases (e.g. directory A imports from directory B but is sequenced first).

**Lens H — Risk**
> For each proposed move and split, count broken `use` statements via `rg`. Search both `use crate::...` (intra-crate) and, when `${IS_WORKSPACE_MEMBER}` is true, `use ${CRATE_NAME}::...` across sibling crates. Surface `pub(super)` visibility shifts when files move into nested directories, and `pub(crate)` items that need to become `pub` to remain reachable from external workspace callers (or vice versa). Identify hidden cycles, deferred items that actually block earlier phases, and tests that reach into module internals via paths that will move.

**Lens I — Two-sided membership audit**
> Apply symmetric pressure: question overgroupings AND missed groupings. The plan's conservatism bias is to leave root sprawl in place — counter it.
>
> **Side 1 — overgroupings.** For each new directory proposed: does it justify itself, or is it two files that happen to share a vibe? Read each member's header (top 30 lines) and judge if the grouping name describes what they all *do*, not what they all *are not*. Propose dissolutions, membership changes, or rename when a clearer home exists.
>
> **Side 2 — missed groupings.** Count singletons at every layer of the proposed tree. If any layer still has more than 6, that is a finding, not a passing grade. For each such layer:
> - List every remaining singleton with its one-line responsibility.
> - Walk the responsibility axes from Lens C's instructions (entry/CLI/config/domain/I-O/persistence/external-drivers/utilities/cross-cutting).
> - Propose at least one further grouping that drops the count, OR write a per-file defense citing both gate criteria from `WritePlacementPlan` (referenced from `main.rs`/`lib.rs` AND no axis-peer).
>
> Do not manufacture dissent on side 1, but do not pull punches on side 2. "Keep as proposed" is only a valid verdict when both sides pass — overgroupings hold AND every layer is within the singleton budget.

Read all three findings files and collect them as `${PASS3_FINDINGS}` before refinement.
Apply the pass 1 rule for no-findings results and incomplete lenses.
</Pass3Validation>

---

<RefinePlan>
**Goal:** Apply pass 3 findings directly to `${DOC_PATH}`. No per-finding walkthrough; the command produces a finished plan, not a decision queue.

For each finding:
- If it's a placement / membership change, edit the tree, the rationale, and the sequencing.
- If it's a re-export gap, add the missing entry to the `mod.rs` block.
- If it's a risk that requires an extra step (e.g. fix a `pub(super)` before the move), insert that step into the sequencing.
- If it's "keep as proposed" — do nothing.

Resolve conflicts with the style guide.
</RefinePlan>

---

<PresentFinal>
**Goal:** One concise message to the user.

Include:
- The final tree (collapsed — directories with one-line annotations, not full file listings).
- One-sentence summary per new directory: what cohesion justifies it.
- The phase list (Phase 1 placement, plus any Phase 2+ splits added by pass 2).
- What pass 3 changed from the initial draft, so the user sees the refinement was real. If pass 3 changed nothing, say so explicitly.
- Anything deferred, named with severity. No "options" framing.

Do not offer follow-up commands unless a deferred item is concrete enough to schedule.

Stop. The doc at `${DOC_PATH}` is authoritative.
</PresentFinal>
