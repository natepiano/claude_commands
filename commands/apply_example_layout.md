---
description: Reorganize a crate example so its primary-API code reads first and supporting/decorative code comes last
---

# Apply Example Layout

Reorganize a crate example (`examples/<name>.rs`) so the code that demonstrates
the example's primary API reads top-to-bottom first, and supporting code (UI/HUD
panels, scene scaffolding, decorative animation) comes last — with banner-delimited
sections and behavior-focused comments. **Behavior must not change**; this only
moves items and improves comments.

**Arguments**: `$ARGUMENTS` — the example to reorganize (name or path). Empty =
use the example file already in context, or ask.

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**
**STEP 1:** Execute <LoadStyle/>
**STEP 2:** Execute <ResolveTarget/>
**STEP 3:** Execute <IdentifyPurpose/>
**STEP 4:** Execute <Reorganize/>
**STEP 5:** Execute <Verify/>
</ExecutionSteps>

<LoadStyle>
This edits Rust, so load the style guide first: run
`zsh ~/.claude/scripts/load-rust-style.sh` and read the full persisted output.
Follow it for naming, comment style, and formatting.
</LoadStyle>

<ResolveTarget>
Resolve which `examples/*.rs` file to act on:
- If `$ARGUMENTS` names an example, find `examples/<name>.rs` under the current
  crate (or the given path).
- If empty, use the example already in context; if none, ask which one.

Read the **whole file** before moving anything.
</ResolveTarget>

<IdentifyPurpose>
Decide what the example exists to demonstrate — the API or feature whose use is
the point (for a camera example, spawning and driving the camera; for a shader
example, the material/shader wiring; for an ECS example, the systems/queries).
That code is **primary**.

**Write down the demonstrated API by name** — the specific types, triggers,
components, traits, or systems whose use is the point (e.g. "`ZoomToFit`,
`LookAt`, `LookAtAndZoomToFit` triggers" or "`OrbitCam` with `RenderTarget` +
per-camera viewport override"). The module doc, the primary section banner, and
(if the flow spans systems) the section's "How it works" paragraph must all name
this API. A reader skimming any one of them should be able to answer "what does
this example teach?"

Everything that only supports the demo is **supporting**: UI/HUD panels, scene
scaffolding (ground, lights, placeholder meshes), decorative animation, and input
plumbing not specific to the API. When unsure which bucket a piece falls in, ask
rather than guess.
</IdentifyPurpose>

<Reorganize>
Target order, top to bottom:
1. **Module doc** — a short paragraph on what the example shows, naming the
   demonstrated API from `<IdentifyPurpose>` by name, plus the controls. Do
   **not** add a "Code layout" / "this section is below" navigation note —
   the named banner below already does that job, and meta-commentary about
   the file's structure is noise.
2. **Imports.**
3. **`main()`** — the entry point / app wiring, so the whole app is visible at a
   glance. Comment the non-obvious wiring (system ordering, observers).
4. **Primary section(s)** — the code demonstrating the API. The banner must name
   the demonstrated API from `<IdentifyPurpose>` (not just the subsystem — e.g.
   `CAMERA TRIGGERS — ZoomToFit / LookAt / LookAtAndZoomToFit`, not just
   `CAMERA`). If the demonstrated flow spans more than one system (a startup
   spawn + an observer + an update system, for instance), open the section with
   a short **"How it works"** paragraph immediately under the banner that
   sketches the order in which they fire and what each step contributes — the
   runtime story the per-fn doc comments can't tell on their own. Lead with
   setup/spawn, then the systems/observers that drive it, then local helpers.
5. **Supporting section(s)** — UI panels and scaffolding, then decorative animation
   last.

**Constants placement** depends on whether the example has more than one banner
section below `main()`:
- **Multi-section examples** (banner-delimited subsystems like CAMERA / GROUND /
  GIZMO): constants live at the top of the section that owns them, just above
  that section's types. A reader hitting a constant for the first time has the
  surrounding context. `main()` can reference them by name — Rust resolves
  forward refs without issue.
- **Single-subsystem examples** (one camera, one target, a few tunables — no
  banner sections below `main()`): constants in one block above `main()`,
  grouped with `//` section headers (camera, cube, labels, timing).

Within every section *below* `main()`, order items: constants (when section-local)
→ components/resources/types → spawn/startup systems → update systems & observers
→ helper fns.

Delimit each section with a banner comment that names the section and, in one
clause, says what it does — nothing more. e.g.:

```rust
// ═════════════════════════════════════════════════════════════════════════════
// CAMERA — spawning the OrbitCam, the per-engine axis, and the fly between views.
// ═════════════════════════════════════════════════════════════════════════════
```

Do **not** add editorial framing about importance ("this is the part to read",
"not essential to the demonstrated API", "visualization aid"). The section
ordering already conveys primary vs. supporting; restating it is noise. The
banner title plus its one-line description is the whole banner.

Comments: explain **behavior** — what the code does and the effect it produces —
where a reader would otherwise have to guess. Do **not** write comments that
justify one design over another; that rationale belongs in the commit/PR.

Rules:
- **Move and comment only.** Do not change logic, names, or signatures. Rust item
  order is free, so reordering `fn`/`type`/`const`/`impl` is behavior-preserving —
  but watch for ordering that actually matters (macro invocations, `cfg`-gated
  items, attribute scope).
- If moving leaves a genuinely dead item (e.g. a now-unused `Default` impl), remove
  it and say so. Surface any larger removal for the user to decide rather than
  doing it silently.
- Respect the forbidden-words list in every comment you write.
- If a rename would help readability, ask the user (their editor renames fast)
  rather than renaming inline.
</Reorganize>

<Verify>
Run, in order, from the crate dir:
- `cargo build --example <name>` (add `-p <package>` in a workspace if needed)
- `cargo clippy --example <name>` (the workspace denies warnings — must be clean)
- `cargo +nightly fmt`

The reorg is behavior-preserving, so the build must pass with no new warnings. If
anything fails, fix it (clippy may flag a pattern exposed by a move, e.g. a
`needless_range_loop`) with an equivalent rewrite, never a behavior change.

Report the final section order and any item you removed as dead.
</Verify>
