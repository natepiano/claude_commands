---
description: Compile a design/plan doc into a delegate-ready phased implementation plan — strip design narrative, bake in the codebase context every phase needs, and front each phase with a self-contained Work Order so /plan:delegate can dispatch by assembly with zero research.
---

# Phase Plan

**Purpose:** Turn a plan/design doc into a **delegate-ready implementation plan**
(format: `~/.claude/docs/delegate_plan_format.md`). After this runs, a compacted
orchestrator can hand any phase to `/plan:delegate` by copy-and-assemble — the
expensive codebase research is paid **once**, here, and baked into the doc.

**Usage:** `/plan:to_phased_plan [plan-doc-path]`

**Argument:** the plan doc path. If omitted, infer the single plan doc in
conversation; if none, ask for the path. Do not guess.

This command rewrites the doc **in place**. It does not write code.

This command is **idempotent**. Run it on a fresh design (compile mode) or on an
already-compiled, in-progress plan to bring a hand-inserted or edited phase up to
Work Order standard (reconcile mode). `<Locate/>` picks the mode; every later step
branches on it.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** Execute <Locate/> — sets ${MODE} ∈ {compile, reconcile}
**STEP 2:** Execute <GatherContext/>
**STEP 3:** Execute <Restructure/>
**STEP 4:** Execute <Rewrite/>
**STEP 5:** Execute <Report/>
</ExecutionSteps>

---

<Locate>
Resolve the plan doc path (argument → conversation inference → ask). Read it.

Read `~/.claude/docs/delegate_plan_format.md` — it is the target format and the
contract `/plan:delegate`, `/plan:phase_review`, and `/plan:to_as_built` all depend on.

**Detect the mode.** The doc is already delegate-ready if it has both a
`## Delegation Context` section and the `Status: IMPLEMENTATION PLAN — phased,
delegate-ready` line.

- Not delegate-ready → **${MODE} = compile** (the original full compile).
  State: `Compiling <relative/path> into a delegate-ready plan.`
- Already delegate-ready → **${MODE} = reconcile**. The plan is in flight; the job
  is to bring uncompiled/edited phases up to standard without disturbing the rest.
  State: `Reconciling <relative/path> — already delegate-ready; compiling uncompiled phases only.`

In **reconcile** mode, also identify the **target phases**: every `todo` phase
whose body is *not* a well-formed `#### Work Order` (rough narrative, a stub, a
hand-inserted phase like `4a`), plus any phase `$ARGUMENTS` names explicitly.
`done` phases and `todo` phases that already carry a complete Work Order are **not**
targets — they are preserved byte-for-byte. List the target phases in one line.
</Locate>

---

<GatherContext>
**Reconcile mode:** the `## Delegation Context` block already exists — do **not**
regenerate it and do **not** run the full sweep below. Reuse it as-is. Only if a
target phase names files absent from its **Key files** list, dispatch a *narrow*
Explore for just those files (return `path — role` rows to append), leaving the
rest of the block untouched. Then skip to `<Restructure/>`.

**Compile mode** (the rest of this step):

**Goal:** produce the **Delegation Context** block without spending orchestrator
tokens on exploration.

Dispatch ONE `Explore` (or `general-purpose`) subagent with a self-contained
prompt. Its job is to return the codebase facts every phase needs, as a compact
block — nothing else. The prompt must include:

- The absolute plan-doc path (the subagent reads it to learn which files/areas
  the phases touch).
- A directive to determine and return, terse:
  1. **Project** — the crate / workspace-member name and one-line purpose.
  2. **Stack** — language + key frameworks/versions the work touches.
  3. **Layout** — a short map of only the dirs/files the phases touch.
  4. **Key files** — `path — role` for each file a phase reads or modifies, with
     line refs where the plan already cites them.
  5. **Build / Test / Lint** — the exact commands/workflows this project uses
     (read `Cargo.toml`/`package.json`/`justfile`/CI config; do not invent).
     For Rust repos with the local `clippy` skill/workflow available, record the
     lint gate as the full `clippy` skill, not a hand-expanded subset of
     `cargo clippy`, `cargo mend`, `cargo doc`, or individual `lint ...`
     commands. The `clippy` skill owns cache checking, `lint mend`, style review,
     `lint clippy`, `lint doc`, and `lint fmt`.
  6. **Style** — for Rust, the `load-rust-style.sh` line with the project root;
     omit for non-Rust.
  7. **Invariants** — project-wide rules every phase must preserve (from the plan
     and from obvious code constraints).
- Output format: exactly the `## Delegation Context` bullet block from the format
  doc. No prose, no findings list.

The subagent does not edit anything. Capture its block as ${DELEGATION_CONTEXT}.
</GatherContext>

---

<Restructure>
**Goal:** decide the phase set and what each Work Order contains. This is
orchestrator work (you hold the design intent) — but it is reading + structuring,
not codebase searching.

1. **Identify existing phases.** If the doc already has phases, use them as the
   spine. If it is an unphased design, decompose it into **separable, substantive
   commits** — each independently buildable and reviewable, ordered so each
   leaves the tree green. Prefer the smallest set of phases that are each a real
   chunk of work; do not over-split.

   **Right-size each phase — split signals.** A phase must be **delegate-sized**:
   one fresh implementer (no prior context) builds it and gets the tree green in a
   single pass. Whether the spine is inherited or freshly decomposed, test every
   phase against these signals **before** drafting its Work Order. If any fires,
   split the phase into delegate-sized units — the usual seam is **types →
   systems/wiring → tests**, or one subsystem per file group.

   - **Gate breadth:** the Acceptance gate would enumerate more than ~8–10
     distinct checks / test requirements. A long gate is the compile-time tell
     that one phase number is wearing several units of work.
   - **New subsystem:** the phase stands up a *new* multi-file subsystem — a new
     module plus its own cross-cutting surface (new schema/config, a new
     API/protocol boundary, a generated/compiled artifact such as a shader or
     migration, a new render/IPC/service path) — rather than extending an existing
     one. Standing it up and fully testing it are separate units.
   - **File span:** the Spec **creates** more than ~2–3 files, or expects a single
     new/edited file to grow by many hundreds of lines.
   - **Mixed kinds:** the phase bundles work that could each land and be reviewed
     on its own — defining data types, wiring systems/routing, and proving with
     tests — under one number.
   - **Compound goal:** the Goal joins two outcomes with "and" that need not ship
     together.

   **Counter-signal — do NOT split** when the pieces are not independently
   buildable (one won't compile or pass without the other) or a split would leave
   an intermediate phase red. Cohesion beats count: never break one atomic change
   just to lower a number, and never merge unrelated work just to raise one.

2. **For each phase, draft a Work Order** per the format doc:
   - **Goal** — one line, the observable outcome.
   - **Spec** — the implementation detail. Where the source doc has a resolved
     design (concrete types, signatures, constraints), copy it **verbatim** into
     the Spec. Do NOT compress a settled decision to a summary — the delegate
     needs the detail to implement without searching.
   - **Files** — the files to create/modify, with line refs the doc already cites.
   - **Constraints from prior phases** — facts later phases need from earlier ones
     (what got built, decisions that bind). Empty for Phase 1.
   - **Acceptance gate** — the build/test/behavior proving the phase done.

3. **Fold design history into the phases, then drop the narrative.** Justification
   essays ("why this exists", "what's wrong with the old model"), alternatives,
   and resolved-decision debates (e.g. `D1–D6`) do NOT survive as prose. Each
   load-bearing fact becomes either a Work Order **Spec** line in the phase it
   constrains or a Delegation Context **Invariant**. Nothing useful is lost; the
   debate format is.

4. **Preserve completed phases.** If a phase is already `done`, keep its status,
   commit ref, Work Order, and any existing Retrospective as the archive zone. Do
   not rewrite shipped history. Only compile the remaining `todo` phases into
   Work Orders.

**Reconcile mode** narrows steps 1–3 to the **target phases** from `<Locate/>`:

- Do **not** re-decompose the plan or touch the phase spine — the insert/edit
  already set it — **except** to apply the **Right-size** check (step 1) to the
  remaining `todo` phases. Run that check now: any `todo` phase that trips a split
  signal is split into delegate-sized sub-phases (e.g. `4` → `4a/4b/4c`),
  preserving order and leaving every `done` phase untouched. This is the one
  spine edit reconcile may make, and only on un-started `todo` phases. The newly
  created sub-phases join the target set.
- Compile **only** the target phases into Work Orders (steps 2–3 above, applied to
  each target). Already-complete `todo` Work Orders and all `done` phases are left
  byte-for-byte unchanged — no churn, no drift.
- **Forward-propagate.** For each freshly compiled target phase, apply the
  **Propagate-Forward** rule from the format doc (`~/.claude/docs/delegate_plan_format.md`
  → "Forward-propagation"): push the facts it produces into the **Constraints from
  prior phases** of every *later* phase that depends on them. Inserting `4a` between
  done-`4` and todo-`5` means `5+` may now depend on what `4a` builds — this step is
  what keeps them dispatch-ready. This is the only edit reconcile mode makes to a
  non-target phase, and it touches only the **Constraints from prior phases** field.
</Restructure>

---

<Rewrite>
**Reconcile mode:** make **surgical Edits only** — never a full-doc rewrite. Edit
each target phase's body into a `#### Work Order`, append any new **Key files** rows
to the existing Delegation Context, and edit the **Constraints from prior phases**
of the later phases the propagation step touched. Leave every other byte (title,
status line, done phases, untouched todo phases, links) exactly as found. Then go to
`<Report/>`.

**Compile mode:** rewrite the doc **in place** to the format-doc structure:

1. Title + a one-line `> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.**`
2. `## Delegation Context` = ${DELEGATION_CONTEXT}.
3. `## Phases` — each phase with its `#### Work Order`. Completed phases keep their
   archive form (status `done`, Work Order, Retrospective) below or after the live
   `todo` phases.
4. Remove the stripped narrative sections entirely.

Use Edit/Write. Preserve any relative links the doc relies on (e.g. `as-built/…`).
Do not change code. Do not commit.
</Rewrite>

---

<Report>
**Compile mode** — produce a succinct markdown table:

```markdown
| Area | Result |
| --- | --- |
| Compiled | <plan path → delegate-ready; N phases> |
| Live phases | <count of todo phases, with titles> |
| Archived | <count of done phases preserved, or None> |
| Stripped | <what design narrative was removed and folded where> |
| Next | `/plan:delegate <plan path> phase <first todo N>` |
```

**Reconcile mode** — report what changed vs what was left alone:

```markdown
| Area | Result |
| --- | --- |
| Mode | Reconcile (already delegate-ready) |
| Compiled (new) | <target phases brought up to Work Order, with titles> |
| Propagated | <later phases whose Constraints from prior phases were updated, or None> |
| Preserved | <count of done + already-compiled todo phases left untouched> |
| Next | `/plan:delegate <plan path> phase <first todo N>` |
```

Then stop. Do not start implementing a phase.
</Report>

---

## Rules

- This command edits the plan doc only — never implementation code, never commits.
- Offload codebase research to the subagent (`<GatherContext/>`); the orchestrator
  must not explore the repo itself — that is the token cost this whole design
  exists to remove.
- Every remaining Work Order must satisfy the format doc's self-containment rule:
  a fresh codex implements it from the named files + Delegation Context, no search.
- Never delete completed-phase history. Strip design *narrative*, not shipped facts.
- Every phase must be **delegate-sized** (Restructure step 1): a fresh implementer
  builds it green in one pass. Apply the split signals at compile time — an
  over-size phase is a decomposition miss, not the delegate's problem to absorb.
- Reconcile mode is **additive and surgical**: it compiles uncompiled/edited phases
  and propagates their facts forward (format doc → "Forward-propagation"). It never
  re-runs the full Explore sweep and never rewrites a `done` phase or an
  already-complete `todo` Work Order. It may split an over-size **un-started `todo`**
  phase into sub-phases (Restructure step 1 check) — the only spine edit it makes.
