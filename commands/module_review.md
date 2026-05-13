---
description: Three-pass module-structure evaluation. Three agents review a directory tree (cohesion / coupling / placement). The agent writes a restructure plan to a doc. If pass 1 surfaced over-large files (per the style guide), three more agents design split phases. Three final agents stress-test the complete plan. The agent refines and presents.
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
**Goal:** Build the file inventory once so every agent sees the same picture.

Run `find ${SCOPE} -type f -name "*.rs" | sort` and capture the output as `${INVENTORY}`. Also capture per-file line counts via `wc -l`.

Identify the existing directory submodules under `${SCOPE}` (immediate children that are directories with a `mod.rs`) and the singleton `.rs` files at each layer. This is the picture every agent's prompt will reference.
</InventoryScope>

---

<Pass1Discovery>
**Goal:** Three agents evaluate the structure along three lenses, with the Rust style guide loaded.

Launch **3 Explore agents in parallel** using the Agent tool. Each agent's prompt must include this preamble verbatim:

```
Before evaluating, load the Rust style guide:
  zsh ~/.claude/scripts/load-rust-style.sh

If the output mentions a saved file path, Read that file. Apply the loaded rules — especially `when-to-split-a-module.md` for the over-large-file criterion (~500 lines non-test + at least one other signal) — when forming findings. Cite the rule filename for each finding it informs.

The style guide includes `forbidden-words.md`. Apply that file's rules to every word of every finding you return.
```

Then the per-agent body. Pass `${INVENTORY}` verbatim into each prompt.

**Agent A — Cohesion**
> Evaluate **module cohesion** across `${SCOPE}`. For each module (top-level and each subdirectory), answer: do the files belong together? Name split candidates, merge candidates, and misplaced singletons. Output findings as numbered list: Title / Where (file paths) / Observation (concrete — name types/functions) / Severity (critical / important / minor) / Recommendation (move / merge / split / rename / leave-as-is with target path).

**Agent B — Coupling and boundaries**
> Evaluate **module coupling and boundary discipline** across `${SCOPE}`. Map who-imports-whom. Surface unhealthy directions (domain → UI, leaf → aggregator). For library/binary boundaries: identify generic code stuck in app-specific homes and app-specific code leaked into generic homes. Output findings same format as Agent A; cite actual `use` paths.
>
> If `${IS_WORKSPACE_MEMBER}` is true, also search **sibling workspace crates** for inbound imports via `rg "use ${CRATE_NAME}(::|;|\s+as\s+)" --type rust` across the whole repo (excluding `${CRATE_ROOT}`). Flag public API surface that's wider than its actual external use, and inbound paths that reach into deep submodules instead of crate-root re-exports.

**Agent C — Root-vs-submodule placement**
> Evaluate **root-vs-submodule placement** across `${SCOPE}`. Policy: every file lives either at the crate root (genuinely top-level concerns) or inside a directory submodule that groups files by responsibility. No flat sprawl of unrelated single-file modules at any layer. For each layer, name singletons that should move into existing subdirs, files that should form a new subdir together, and directories too small to justify themselves. Output findings same format. End with a final tree sketch of the proposed layout (directories only, plus one or two example files per directory).

**Each agent must additionally:**
- List every over-large file it finds in `${SCOPE}`, citing `when-to-split-a-module.md` and which criteria are met. Use `wc -l` plus an `awk` test-block boundary check (`/^#\[cfg\(test\)\]/`) to separate production-line count from inline tests.

Collect findings as `${PASS1_FINDINGS}` and the union of flagged over-large files as `${OVERSIZE_FILES}`.
</Pass1Discovery>

---

<WritePlacementPlan>
**Goal:** Synthesize pass 1 into a confident placement plan and write it as Phase 1 of `${DOC_PATH}`.

Merge and dedupe findings. Resolve disagreements with judgment — pick the cleanest layout, not the union of all proposals. Do **not** list dismissed alternatives. The plan is the recommendation.

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

Do not cite agent reports. Do not include "options A vs B". State the recommendation.
</WritePlacementPlan>

---

<Pass2SplitPlanning>
**Goal:** For each over-large file flagged in pass 1, design a split and append it as a follow-on phase.

**If `${OVERSIZE_FILES}` is empty:** print one line — `No over-large files in scope. Skipping pass 2.` — and proceed to pass 3.

Otherwise, launch **3 Explore agents in parallel**, each loading the style guide (same preamble as pass 1). Each agent reviews **all** over-large files (not one agent per file) through a distinct lens.

**Agent D — Production seams**
> For each file in `${OVERSIZE_FILES}`: group top-level items by responsibility. Propose a submodule layout (`<file>/mod.rs` + N submodules), citing line ranges, type names, and function clusters. Apply `name-submodules-after-anchor-types.md` and `split-by-type-ownership.md`. Output per file: target layout, what goes where (current line ranges + items), sequencing for the split (leaves first).

**Agent E — Production-vs-test ratio**
> For each file in `${OVERSIZE_FILES}`: compute production-line vs inline-test-line counts (use `awk '/^#\[cfg\(test\)\]/{print NR; exit}'` to find the test block boundary). If a file is >50% tests, the right action is test extraction (move tests to a `tests/` directory), not a module split. Flag any such file and propose the test-extraction target path. For genuine production-heavy files, confirm the split is warranted by `when-to-split-a-module.md`.

**Agent F — Call-site impact**
> For each file in `${OVERSIZE_FILES}` and each proposed submodule from Agent D: count caller `use` statements that reference the file and predict which submodule each caller now needs.
>
> Search both intra-crate and (when applicable) external workspace callers:
> - Intra-crate: `rg "use crate::<path>::<file>" --type rust` inside `${CRATE_ROOT}` (or repo root for single-crate projects).
> - External workspace (only when `${IS_WORKSPACE_MEMBER}` is true): `rg "use ${CRATE_NAME}::<path>::<file>" --type rust` across the whole repo, excluding `${CRATE_ROOT}`. Also check for re-exports at the crate root that would shield external callers from the move.
>
> Identify dependencies between proposed submodules (which import which) to determine extraction order. Surface any cycle or visibility hazard.

Synthesize: append a `## Phase N — Split <file>` section to `${DOC_PATH}` for each file, in order of size or risk. Each phase contains:
- Target layout (directory tree).
- What goes where (line ranges + items).
- Sequencing (leaves first, with dependency reasoning). Steps are in-flight checkpoints — run the cargo checkpoint commands between them — and the whole phase lands as a single commit once green.
  - When `${IS_WORKSPACE_MEMBER}` is true, the checkpoint commands are `cargo build -p ${CRATE_NAME}` + `cargo nextest run -p ${CRATE_NAME}`.
  - Otherwise, `cargo build` + `cargo nextest run`.

For test-extraction-only files (Agent E's verdict), the phase has no internal sequencing steps — it's a straight test move. Say so.

Update the phase-overview table at the top of the doc to include the new phases. Each row's "Rough size" column should end with "one commit".
</Pass2SplitPlanning>

---

<Pass3Validation>
**Goal:** Three agents stress-test the complete multi-phase plan.

Launch **3 Explore agents in parallel**, each loading the style guide. Each agent reads `${DOC_PATH}` and validates against the current code on disk.

**Agent G — Completeness**
> Walk every `.rs` file in `${SCOPE}` (the inventory from step 2). Confirm each is either explicitly moved, explicitly kept where it is, or covered by a "stays where" section. Check rename consistency across the doc. Check that each new directory's `mod.rs` re-export block accounts for every type currently imported via the old path. Flag sequencing chicken-and-egg cases (e.g. directory A imports from directory B but is sequenced first).

**Agent H — Risk**
> For each proposed move and split, count broken `use` statements via `rg`. Search both `use crate::...` (intra-crate) and, when `${IS_WORKSPACE_MEMBER}` is true, `use ${CRATE_NAME}::...` across sibling crates. Surface `pub(super)` visibility shifts when files move into nested directories, and `pub(crate)` items that need to become `pub` to remain reachable from external workspace callers (or vice versa). Identify hidden cycles, deferred items that actually block earlier phases, and tests that reach into module internals via paths that will move.

**Agent I — Simpler alternatives**
> For each new directory: does it earn its weight, or is it two files that happen to share a vibe? Read each member's header (top 30 lines) and judge if the grouping name describes what they all *do*, not what they all *are not*. Propose dissolutions, membership changes, or rename when a clearer home exists. Do not manufacture dissent — say "keep as proposed" when right.

Collect findings as `${PASS3_FINDINGS}`.
</Pass3Validation>

---

<RefinePlan>
**Goal:** Apply pass 3 findings directly to `${DOC_PATH}`. No per-finding walkthrough; the command produces a finished plan, not a decision queue.

For each finding:
- If it's a placement / membership change, edit the tree, the rationale, and the sequencing.
- If it's a re-export gap, add the missing entry to the `mod.rs` block.
- If it's a risk that requires an extra step (e.g. fix a `pub(super)` before the move), insert that step into the sequencing.
- If it's "keep as proposed" — do nothing.

Conflicts between agents: pick the recommendation that best matches the style guide, not the loudest one.
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
