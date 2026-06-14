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

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** Execute <Locate/>
**STEP 2:** Execute <GatherContext/>
**STEP 3:** Execute <Restructure/>
**STEP 4:** Execute <Rewrite/>
**STEP 5:** Execute <Report/>
</ExecutionSteps>

---

<Locate>
Resolve the plan doc path (argument → conversation inference → ask). Read it.
State the path in one line: `Compiling <relative/path> into a delegate-ready plan.`

Read `~/.claude/docs/delegate_plan_format.md` — it is the target format and the
contract `/plan:delegate`, `/plan:phase_review`, and `/plan:to_as_built` all depend on.
</Locate>

---

<GatherContext>
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
  5. **Build / Test / Lint** — the exact commands this project uses (read
     `Cargo.toml`/`package.json`/`justfile`/CI config; do not invent).
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
</Restructure>

---

<Rewrite>
Rewrite the doc **in place** to the format-doc structure:

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
Produce a succinct markdown table:

```markdown
| Area | Result |
| --- | --- |
| Compiled | <plan path → delegate-ready; N phases> |
| Live phases | <count of todo phases, with titles> |
| Archived | <count of done phases preserved, or None> |
| Stripped | <what design narrative was removed and folded where> |
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
