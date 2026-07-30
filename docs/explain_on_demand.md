# ExplainOnDemand

Shared by `/plan:delegate` and `/plan:phase_review`. Both reference this file
rather than carrying their own copy, so the method cannot drift between them.

**Trigger:** the user says they do not understand, asks what something means,
asks for a reframe, or answers a gate or decision with confusion instead of an
answer. This fires at any gate — a pending decision, a pre-phase briefing, a
review synthesis, a completed-phase report, an inline decision — and preserves
that gate. Explaining never authorizes anything and never counts as approval.

This is the one place the terse default is wrong. Do not compress; do not
re-emit the same summary with softer words. Rebuild the explanation from the
bottom.

**Clear does not mean non-technical.** Strip the shorthand, keep the substance.
Name the real type, trait, macro, and signature — a reader who cannot see the
codebase still wants the actual thing named, not an analogy standing in for it.

## Structure, in order

1. **What is being changed, and from what.** One paragraph of ordinary
   sentences. The reader may not know what exists today; say it before saying
   what replaces it.
2. **Why it is hard.** Name each obstacle separately. One paragraph each, no
   lists of nouns.
3. **The mechanism, then immediately a code example.** Every abstract claim gets
   a concrete one under it. An unillustrated abstraction is the failure mode
   this block exists to prevent — if a sentence describes a mechanism and no
   code follows, the explanation is not finished.
4. **What is actually being decided**, as a short numbered list with a
   recommendation per item.

## Rules for the code examples

- Show the problem before the fix. Code that fails to compile, or does the wrong
  thing, teaches the constraint faster than prose about the constraint. Say
  which it is in a trailing comment (`// no common type — will not compile`).
- Quote real signatures from the real source. Open the crate and read it rather
  than reconstructing from memory; a wrong signature in a teaching example is
  worse than no example.
- Keep each block short enough to read in one pass. Elide with `…` rather than
  padding to realism.
- Label anything not yet decided as a sketch, in the surrounding prose, so it is
  never mistaken for settled design.
- If an earlier summary was imprecise and the rebuild exposes it, correct it in
  one sentence and move on.

Afterwards, restate the pending question and wait. The gate is unchanged.
