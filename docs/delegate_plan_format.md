# Delegate-ready plan format

The shared contract for a **delegate-ready phased implementation plan**. Four
commands read or write this format and must not drift from it:

- `/plan:to_phased_plan` — compiles a design/plan doc into this format.
- `/plan:delegate` — dispatches a phase by *assembling* its Work Order (fast path),
  not by researching the codebase.
- `/plan:phase_review` — folds per-phase learnings back in, keeping every remaining
  Work Order dispatch-ready.
- `/plan:to_as_built` — distills the completed plan into an as-built overview.

The single design goal: **a compacted orchestrator can dispatch any remaining
phase by copy-and-assemble, with zero codebase research.** Everything expensive
to rediscover after a context compaction lives in the doc.

---

## Document structure

```markdown
# <Feature name>

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** <one line: what this builds>

## Delegation Context
<!-- Shared across all phases. /plan:delegate prepends this to every dispatch. -->

- **Project:** <crate / workspace member name — one-line purpose>
- **Stack:** <language + key frameworks/versions the work touches>
- **Layout:** <only the dirs/files phases touch, as a short map>
- **Key files:** <path — role> for each file a phase reads or modifies
- **Build:** <exact build command>
- **Test:** <exact test command>
- **Lint:** <clippy / type-check / fmt command, if any>
- **Style:** <the style-loader line, e.g. `zsh ~/.claude/scripts/rust_style/load-rust-style.sh --project-root <dir>`; omit for non-Rust>
- **Invariants:** <project-wide rules every phase must preserve; omit if none>

## Phases

### Phase N — <title>  · status: todo
<!-- status ∈ {todo, done}. /plan:phase_review flips it and appends a Retrospective. -->

#### Work Order
<!-- The dispatch prompt. Self-contained against Delegation Context + named files.
     A fresh codex session reads ONLY the files named here — no exploration. -->

**Goal:** <one line — the observable outcome of this phase>

**Spec:**
<the implementation detail, verbatim from the design where one exists: types,
signatures, APIs, patterns, edge cases. Name files with line refs where known.
This is the meat — do not paraphrase a resolved design down to a summary.>

**Files:**
- `<path>` — <what changes here>
- ...

**Constraints from prior phases:** <concrete facts a delegate would otherwise
re-derive — what earlier phases built, decisions that bind this phase. Empty for
Phase 1. Maintained by /plan:phase_review.>

**Acceptance gate:** <the build/test/behavior that proves this phase done —
e.g. `cargo nextest run` green + a named test + an observable behavior.>

### Phase N (completed) — collapses to the archive form once done:

### Phase N — <title>  · status: done (`<commit>`)

#### Work Order
<kept verbatim — the record of what was dispatched>

#### Retrospective
**What worked:** ...
**What deviated:** ...
**Surprises:** ...
**Implications for remaining phases:** ...   ← /plan:phase_review acts on these
```

---

## Forward-propagation <!-- shared rule; cited by /plan:phase_review and /plan:to_phased_plan -->

**Propagate-Forward.** Whenever a phase is added, edited, or completed, the
concrete facts it produces — new types/signatures, file paths, decisions that now
bind — must be pushed into the **Constraints from prior phases** of every later
phase that would otherwise re-derive them. This is the single mechanism that lets
the next `/plan:delegate` assemble its prompt with zero codebase research. After
propagation, each remaining Work Order must still be implementable from its named
**Files** + **Delegation Context** alone; if a change widened scope, update
**Files** and **Spec** to match.

---

## Rules

1. **Delegation Context is written once.** Per-phase Work Orders reference it
   ("build/test/paths: see Delegation Context") rather than repeating it. The
   dispatch step concatenates the two.
2. **A Work Order is self-contained against named files.** A fresh codex with no
   conversation history must be able to implement it by reading only the files
   the Work Order names plus the Delegation Context. If it would have to *search*
   for something, that something belongs in the Work Order or Delegation Context.
3. **Spec stays verbatim.** When the plan derives from a resolved design, copy the
   design's concrete types/signatures/constraints into the Work Order. Do not
   compress a settled decision into a one-liner — the delegate needs the detail.
4. **Live zone vs archive zone.** Remaining (`todo`) phases are the live zone the
   orchestrator reads to dispatch. Completed (`done`) phases + their
   retrospectives are the archive zone — kept in the doc for the record, skipped
   at dispatch time. Keep the archive below the live phases or clearly marked so
   the live zone stays small.
5. **No design narrative in the plan.** Justification essays, alternatives
   considered, and resolved-decision debates do not belong in a delegate-ready
   plan. `/plan:to_phased_plan` strips them; anything load-bearing becomes a Work Order
   **Spec** line or a Delegation Context **Invariant**. The full rationale lives
   in the eventual as-built doc, not the implementation plan.
