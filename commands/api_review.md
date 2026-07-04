---
description: Multi-agent API review — ergonomics, simplicity, duplication removal, module structure/naming, and judicious trait/generic use — across a crate or workspace member's public + internal API. Four review agents, an adversarial validation pass, then a delegate-compatible phased implementation plan written to the docs directory.
---

**IMPORTANT**: Do NOT modify any source code. This command produces a review doc with an implementation plan — implementation happens via `/plan:to_phased_plan` + `/plan:delegate` or a follow-up turn.

## Arguments

```
/api_review [target]
```

- `target` — what to review: a crate root, workspace-member path, module path, or a free-text topic (e.g. `crates/hana_conduit`, `the viewport API`). If omitted, infer from the current conversation topic; if inference fails, ask.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** Execute <ResolveTarget/>
**STEP 2:** Execute <InventoryApi/>
**STEP 3:** Execute <ReviewPass/>
**STEP 4:** Execute <ValidationPass/>
**STEP 5:** Execute <WriteDoc/>
**STEP 6:** Execute <Present/>
</ExecutionSteps>

---

<ResolveTarget>
**Goal:** pin down the API under review and where the doc lands.

- Resolve `target` to `${CRATE_ROOT}` (directory containing the `Cargo.toml`) and, if narrower than the whole crate, the module/feature subset `${FOCUS}` (e.g. `src/viewport/`, "the conduit connection API"). When `${FOCUS}` is set, the review is scoped to it — the rest of the crate is context, not review subject.
- Detect workspace membership by walking up from `${CRATE_ROOT}` for a `[workspace]` `Cargo.toml`. Capture `${CRATE_NAME}` from `[package] name`.
- **Doc path convention** (this is how the user organizes docs across all projects):
  - Workspace member → `<workspace_root>/docs/${CRATE_NAME}/api_review_<topic>.md` (e.g. `~/rust/bevy_hana/crates/hana_conduit` → `~/rust/bevy_hana/docs/hana_conduit/api_review_<topic>.md`)
  - Standalone crate / binary → `${CRATE_ROOT}/docs/api_review_<topic>.md` (e.g. `~/rust/nateroids/docs/api_review_<topic>.md`)
  - `<topic>` — short kebab slug: the crate name when reviewing the whole crate, otherwise the module/feature name (e.g. `api_review_viewport.md`).
- Create the docs directory if missing. If `${DOC_PATH}` already exists, ask: `Doc already exists at <path>. Overwrite / append / cancel?` On cancel, stop.
- Detect whether this is a Bevy project (`bevy` in dependencies) → `${IS_BEVY}`.

State one line: `Reviewing <target> API (crate: ${CRATE_NAME}). Review will be written to ${DOC_PATH}.`
</ResolveTarget>

---

<InventoryApi>
**Goal:** one shared picture of the API surface so every agent reviews the same thing.

- Enumerate the surface in scope: `pub` items (types, traits, functions, methods, events, plugins) AND `pub(crate)`/module-internal items — internal APIs are in scope for duplication and simplicity. When `${FOCUS}` is set, enumerate only items within it; items elsewhere in the crate enter the inventory only as **callers or context** for the focused surface, marked as such.
- Capture **usage evidence**: examples/, tests/, doc comments, and callers of the in-scope items. For workspace members, find external callers via `rg "use ${CRATE_NAME}(::|;|\s+as\s+)" --type rust` across the workspace (excluding `${CRATE_ROOT}`).
- If `${IS_BEVY}`: list Events, Observers, Plugins, and which events are BRP-reachable (registered for reflection, `Serialize`/`Deserialize` or `Reflect`-constructible, triggerable via `world.trigger`).
- Capture the **module tree** of the in-scope surface with per-file line counts (`wc -l`), noting sibling directories that implement the same trait or role.

Capture as `${INVENTORY}` — item list plus file paths. Every agent prompt gets it verbatim.
</InventoryApi>

---

<ReviewPass>
**Goal:** four agents review the API along four lenses.

Launch **4 Explore agents in parallel**. Each prompt includes this preamble verbatim:

```
Before evaluating, load the Rust style guide:
  zsh ~/.claude/scripts/rust_style/load-rust-style.sh
If the output mentions a saved file path, Read that file. Apply the loaded rules — including forbidden-words.md — to every word of every finding you return.
```

Then the per-agent body, with `${INVENTORY}` and this shared context:

> The API serves three audiences: the author, AI agents, and other developers. Optimize for intuitive, low-ceremony use — a caller should be able to guess the right call from the names alone.
>
> Internal APIs (`pub(crate)`, module-to-module) get the same scrutiny as public ones — a "caller" is whoever invokes the item, including sibling modules. Internal messiness leaks: when a public item is awkward because of the internal design behind it (a leaked internal type in a signature, an option struct mirroring internal plumbing, an error type exposing internals), say so — the finding names the internal root cause, not just the public symptom.
>
> The review subject is exactly the in-scope items in the inventory (when a focus is set, that subset — not the whole crate; state the focus here). Items marked as callers/context inform findings (they show real usage) but do not get findings of their own.

Shared output format per finding: Title / Where (paths + item names) / Observation (concrete — show the current call site) / Severity (critical / important / minor) / Recommendation (with a before/after sketch of the call site where it clarifies).

**Agent A — Ergonomics**
> Evaluate call-site ergonomics across `${INVENTORY}` — public and internal entry points alike. For each: how many steps/imports/type annotations does a typical use take? Judge naming (guessable? consistent verb/noun conventions?), defaults (`Default` impls, builder vs N-arg constructors), parameter types (accept `impl Into<T>`/`AsRef` where it removes caller ceremony), error types (can the caller do something with the error?), and discoverability (does the crate root re-export the things users need, or must callers know deep paths?). Ground truth is real call sites: examples/ and tests/ for public items, sibling-module callers for internal ones — flag anywhere the calling code is awkward.
>
> If `${IS_BEVY}`: the user prefers **observers** as the programming model. Flag APIs that force system-scheduling ceremony where an event + observer would be simpler. Flag work that agents or developers would want to trigger externally but that cannot be fired via a BRP `world.trigger` call (event not registered/reflectable, or the operation is only reachable through a system). Recommend the event-triggers-observer shape where it fits; don't force it onto genuinely per-frame logic.

**Agent B — Simplicity and duplication**
> Find surface to remove. Near-identical functions/methods/types (name the pairs and diff them), parallel entry points that do the same job (which one survives?), pass-through wrappers that add no behavior, config/options structs with fields nothing reads, items that are `pub` but have zero external callers per the inventory's usage evidence (narrow to `pub(crate)` or delete), and concepts a caller must learn that could be folded into an existing one. For each duplication finding, state the single surviving API and what migrates to it.

**Agent C — Trait and generic opportunities (two-sided)**
> Evaluate where traits or generics would genuinely help — and where existing ones hurt. Rules:
> - A trait is only justified with **more than one available implementer** (existing in the codebase, or a second one this review's plan concretely creates). One implementer = concrete type, no trait.
> - A generic is justified only when it removes real duplication or widens usability without adding caller-side type ceremony (turbofish, unresolvable inference).
> - Side 1 — opportunities: duplicated impls a trait would unify, copy-paste functions a generic would collapse, sealed-trait or extension-trait patterns that would clean the surface.
> - Side 2 — over-abstraction: existing traits with one implementer, generics with one instantiation, `where`-clause walls. Recommend concretizing.
> Don't manufacture findings on either side — "the current balance is right" is a valid verdict.

**Agent D — Module structure and naming**
> Evaluate the module tree of the in-scope surface. Apply the loaded style rules — especially `name-submodules-after-anchor-types.md` and `when-to-split-a-module.md` — and:
> - **Names match behavior.** A module name says what its contents *do* or names the anchor type they revolve around — never the *kind* of item inside. Flag names like `components`, `observers`, `systems`, `events`, `helpers`, `utils`, `types` and propose behavior/anchor-type names.
> - **Parallel implementers, parallel structure.** Where multiple implementers of a trait (or same-role peers) each live in their own directory/`mod.rs`, their submodule layouts and names should match — the same concern gets the same filename in every implementer directory. Diff the sibling directories and flag divergences (same concern, different name; concern present in one, buried in another's `mod.rs`).
> - **Cohesion.** Things that change together live together. Flag types/functions split across modules that every change touches in tandem, and unrelated items sharing a module.
> - **Size.** A module over ~500 non-test lines is a split indicator (use `wc -l` plus `awk '/^#\[cfg\(test\)\]/{print NR; exit}'` to exclude inline tests). For each over-size module, propose the split along its natural seams — anchor types first — citing `when-to-split-a-module.md`.

Collect findings as `${FINDINGS}`.
</ReviewPass>

---

<ValidationPass>
**Goal:** adversarially stress-test the findings before they become a plan. Merge and dedupe `${FINDINGS}` first, then launch **2 Explore agents in parallel** (same style-guide preamble), each given the merged findings and `${INVENTORY}`:

**Agent E — Feasibility and blast radius**
> For each recommendation: count actual call sites that break (`rg` for the item across crate + workspace), name semver/behavior hazards, and check the before/after sketch actually compiles conceptually (ownership, lifetimes, object safety for proposed traits). Verify every proposed trait's implementer count — kill any that lands at one. For module moves/renames/splits, count broken `use` paths and flag visibility shifts (`pub(super)`/`pub(crate)` items that stop resolving). Rank surviving recommendations by benefit-to-churn.

**Agent F — Over-engineering check**
> Attack each recommendation as a skeptic: does it make the common call site simpler, or just different? Does it add a concept callers must learn? Would the author's own examples get shorter? Kill or demote anything that trades caller simplicity for internal elegance. "Reject" is a valid verdict per finding.

Apply the verdicts: drop killed findings silently, adjust demoted ones. Conflicts between agents: caller-side simplicity wins.
</ValidationPass>

---

<WriteDoc>
**Goal:** write `${DOC_PATH}` as a review + implementation plan that `/plan:to_phased_plan` can compile without rework.

```markdown
# ${CRATE_NAME} API review — <topic>

> **As-built disposition: <amend | create>** — <amend: name the existing as-built
> docs the completed changes fold into; create: this stands up a subsystem no
> existing as-built covers.>

<one paragraph: the API's purpose and the review's overall verdict>

## Findings

<surviving findings, ordered by severity. Each: title, where, observation with
current call site, recommendation with before/after sketch. No dismissed
alternatives, no agent attribution — this is the recommendation.>

## Implementation plan

<Ordered phases. Each phase is a separable, independently buildable commit that
leaves the tree green. Per phase:>

### Phase N — <title>
- **Goal** — one line, the observable outcome.
- **Changes** — concrete: types, signatures, renames, deletions. Copy resolved
  designs verbatim (real signatures, not summaries).
- **Files** — paths touched.
- **Acceptance** — build/test/behavior proving the phase done
  (`cargo build -p ${CRATE_NAME}` + `cargo nextest run -p ${CRATE_NAME}` for
  workspace members; bare commands otherwise; plus any behavior check).
```

**Set the disposition from the inventory.** Check the sibling `as-built/` directory next to `${DOC_PATH}`: if existing as-built docs already cover the reviewed surface (an API review of existing code almost always lands here), stamp `amend` and name those target docs — the finished work updates them in place; a separate `api_review_*` as-built would fragment the same subject across two docs. Stamp `create` only when the plan stands up a subsystem no existing doc covers. `/plan:to_phased_plan` preserves this line verbatim; `/plan:to_as_built` branches on it.

Order phases so mechanical/low-risk changes (renames, re-exports, visibility narrowing) land before structural ones (trait extraction, entry-point merges). Renames of types/functions: note them as candidates for the user's editor-driven global rename rather than scripting them.
</WriteDoc>

---

<Present>
One concise message:
- Overall verdict in a sentence.
- Findings by severity (title-level, not full detail).
- The phase list.
- What the validation pass killed or changed — if nothing, say so.
- Close with: `Next: /plan:to_phased_plan ${DOC_PATH}` (or note it's small enough to implement directly, if it is).

Stop. The doc at `${DOC_PATH}` is authoritative. Do not start implementing.
</Present>
