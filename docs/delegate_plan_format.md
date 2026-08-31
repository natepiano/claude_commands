# Delegate-ready plan format

The shared contract for a **delegate-ready phased implementation plan**. Five
commands read or write this format and must not drift from it:

- `/plan:to_phased_plan` — compiles a design/plan doc into this format.
- `/plan:delegate` — dispatches a phase by *assembling* its Work Order (fast path),
  not by researching the codebase.
- `/plan:phase_review` — keeps review prose temporary and folds durable learnings
  into remaining Work Orders.
- `/plan:shrink` — rewrites completed phases into their shrunk archive form,
  leaving the live zone and Delegation Context untouched.
- `/plan:to_as_built` — distills the completed plan into an as-built overview.

The single design goal: **a compacted orchestrator can dispatch any remaining
phase by copy-and-assemble, with zero codebase research.** Everything expensive
to rediscover after a context compaction lives in the doc.

---

## Document structure

```markdown
# <Feature name>

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** <one line: what this builds>

<!-- Optional; set by the originating review (e.g. /api_review). Preserved verbatim
     by /plan:to_phased_plan and /plan:phase_review; consumed by /plan:to_as_built.
     amend = on completion, fold the shipped changes into the named existing
     as-built docs — no new doc; create (or line absent) = distill a new as-built doc. -->
> **As-built disposition: <amend | create>** — <amend: name the target as-built docs>

## Delegation Context
<!-- Shared across all phases. /plan:delegate prepends this to every dispatch. -->

- **Project:** <crate / workspace member name — one-line purpose>
- **Project started:** <ISO-8601 timestamp — written once by the
  `progress_history.py start-run` recorder; if absent, the recorder derives it
  from the oldest Git commit touching this plan and persists it>
- **Stack:** <language + key frameworks/versions the work touches>
- **Layout:** <only the dirs/files phases touch, as a short map>
- **Key files:** <path — role> for each file a phase reads or modifies
- **Test lanes:** <for each crate a phase touches, its `tests/` directory or
  `none` — what a Work Order's **Seats** opens the `test` seat against>
- **Build:** <for Rust always `bash ~/.claude/scripts/delegate/verify.sh check <pkg>`;
  otherwise the project's exact build command>
- **Test:** <for Rust always `bash ~/.claude/scripts/delegate/verify.sh test <pkg>`;
  otherwise the project's exact test command>
- **Lint:** <for Rust always `bash ~/.claude/scripts/delegate/verify.sh lint <pkg>`.
  Never raw cargo commands and never the full `clippy` skill here — phase
  verification is deliberately scoped; workspace breadth and the `clippy`
  skill run once in /plan:delegate's <FinalGate/> after the last phase>
- **Style:** <for Rust use `run-end /clippy style-only auto-proceed`;
  /plan:delegate omits it from coding prompts and runs it once at <FinalGate/>,
  over the whole branch diff rather than one phase; omit for non-Rust>
- **Invariants:** <project-wide rules every phase must preserve; omit if none>

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, numbered from 1, contiguous, in execution order.
     Never a letter suffix (`4a`, `2b1a`) — those stop sorting and make ranges
     unreadable. Any spine edit (insert, split, merge, reorder, delete)
     resequences from the edit point to the end AND updates every cross-reference:
     substitute highest-first so `Phase 8` does not corrupt `Phase 12`, and
     re-check that each `Phases X–Y` range still spans the same set.
     Insert after the last `done` phase where possible — a done phase's number
     is baked into its checkpoint commit message and cannot be rewritten; if one
     must be renumbered, add an old→new mapping note to the doc.
     Full procedure: /plan:to_phased_plan → <PhaseNumbering/>. -->

### Phase N — <title>  · status: todo
<!-- status ∈ {todo, done}. /plan:phase_review flips it, then /plan:delegate
     shrinks the phase to As-built before checkpoint. Review prose never enters
     this document. -->

#### Work Order
<!-- The dispatch prompt. Self-contained against Delegation Context + named files.
     A fresh delegate session reads ONLY the files named here — no exploration. -->

**Goal:** <one line — the observable outcome of this phase>

**Spec:**
<the implementation detail, verbatim from the design where one exists: types,
signatures, APIs, patterns, edge cases. Name files with line refs where known.
This is the meat — do not paraphrase a resolved design down to a summary.>

**Files:**
- `<path>` — <what changes here>
- ...

**Seats:** <opening line: `N writers + M testers [+ reserve]` — then the split
(by crate, module, or file group) or why nothing splits>
- `impl` — <files it owns>; hub: `<path>` (<why every writer needs it>)
- `test` — <what it writes from the Spec alone, and where under `tests/`>
- `review` — reserve
<!-- A slot is an identity; the role it opens in is what its line says. A line
     without `opens as` opens in its own name. `review` is the flex seat and
     takes whatever third role the opening needs: `opens as impl`, `opens as
     test`, or `reserve`. A `test` seat with no test lane reads `opens as impl`.
     Writers hold disjoint files; every hub file has exactly one owner. Rule 7. -->

**Constraints from prior phases:** <concrete facts a delegate would otherwise
re-derive — what earlier phases built, decisions that bind this phase. Empty for
Phase 1. Maintained by /plan:phase_review.>

**Acceptance gate:** <the build/test/behavior that proves this phase done —
e.g. `bash ~/.claude/scripts/delegate/verify.sh test <pkg>` green + a named
test + an observable behavior. Gate commands are `verify.sh` lines only —
`example`/integration-test lines appear solely in phases whose Files own
them; nothing workspace-wide.>

### Phase N (transient closeout form) — exists only between review and shrink:

### Phase N — <title>  · status: done

#### Work Order
<the dispatched work order; /plan:shrink replaces this span before checkpoint>

Retrospective and review outcomes live only under the delegate session directory.
`/plan:phase_review` first propagates durable decisions into remaining Work
Orders; `/plan:shrink --closeout` then writes the archive form:

### Phase N — <title>  · status: done   ← heading byte-for-byte, including any existing commit annotation

#### As-built
<what the phase shipped, present tense: types, signatures, modules, behavior —
the Work Order's Spec corrected by temporary closeout facts>

**Files:** `<path>` — <what it holds now>
**Binds later work:** <facts remaining phases still depend on, named by fact and
by consumer *title* — never by forward phase number, which resequencing moves and
a frozen `done` phase cannot follow; omit if none>
**Gotchas:** <durable traps; omit if none>
**Ruled out:** <rejected proposals, one clause each; omit if none>
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

## Pending decisions <!-- written by /plan:phase_review (auto mode) and /plan:delegate; consumed by /plan:delegate -->

A user decision deferred by the `/plan:delegate` loop lives inside the affected
phase's Work Order as:

```markdown
**Pending decision: <concrete thing to decide>**

Actual problem:
<one or two sentences naming files/types/phases>

What exists now:
- <concrete current code/doc state>

What should change:
- <recommended direction>

Recommendation:
<direct recommendation>
```

Rules:

- `/plan:delegate` must NOT dispatch a phase whose Work Order carries an
  unresolved `**Pending decision:**` block — its pre-dispatch check presents the
  block(s) to the user first.
- Resolving a decision means editing the outcome into the Work Order's
  **Spec** / **Files** / **Acceptance gate** and deleting the block — never
  annotating the block in place.
- A pending decision on phase M never blocks dispatch of an earlier, unaffected
  phase.

---

## Rules

1. **Delegation Context is written once.** `Project started` is the one field
   the progress recorder may add later: once present it is authoritative and
   must not be recomputed from Git or selected by the agent. Per-phase Work
   Orders reference the context
   ("build/test/paths: see Delegation Context") rather than repeating it. The
   dispatch step concatenates the two.
2. **A Work Order is self-contained against named files.** A fresh delegate agent
   with no conversation history must be able to implement it by reading only the files
   the Work Order names plus the Delegation Context. If it would have to *search*
   for something, that something belongs in the Work Order or Delegation Context.
3. **Spec stays verbatim.** When the plan derives from a resolved design, copy the
   design's concrete types/signatures/constraints into the Work Order. Do not
   compress a settled decision into a one-liner — the delegate needs the detail.
4. **Live zone vs archive zone.** Remaining (`todo`) phases are dispatch-ready
   Work Orders. Every completed (`done`) phase is immediately reduced to the
   `As-built` archive form before checkpoint. Retrospective, finding, reviewer,
   and approval prose never persists in the plan. `/plan:shrink --closeout`
   changes only the current phase; earlier `As-built` blocks and remaining Work
   Orders are byte-stable during shrink.
5. **No design narrative in the plan.** Justification essays, alternatives
   considered, and resolved-decision debates do not belong in a delegate-ready
   plan. `/plan:to_phased_plan` strips them; anything load-bearing becomes a Work Order
   **Spec** line or a Delegation Context **Invariant**. The full rationale lives
   in the eventual as-built doc, not the implementation plan.
6. **Every constraint names its source.** A boundary in **Layout**, **Files**,
   **Invariants**, or a **Spec** — "read-only", "do not touch", "out of scope for
   this plan" — is a claim about what the user or the repository requires, so the
   plan states which. A scope note the author chose ("this phase does not change
   X; phase N does") says so in those words. Never write a crate, module, or
   dependency off as unchangeable because the plan happens to be aimed elsewhere:
   a defect gets fixed where it lives, and an authored boundary that hides that is
   how a workaround gets built on purpose. See `~/.claude/docs/decision_criteria.md`
   → "Where a fix goes".
7. **Seats decides the opening.** Every `todo` Work Order carries **Seats**;
   `/plan:delegate` opens its three seats from it and partitions files by it
   instead of deciding at launch. `impl` always opens as `impl` (`fix` in a
   repair). `test` opens as `test` wherever the phase has a **test lane** — a
   `tests/` directory in a touched crate (Delegation Context → **Test lanes**)
   and a Spec concrete enough to test before the implementation exists; with
   none, `test` opens as a writer and its line says `opens as impl`. `review`
   is the flex seat: a second writer, a second tester, or the cold read
   (`reserve`). Writers hold disjoint file sets. Each hub file — `lib.rs` /
   `mod.rs` re-exports, `Cargo.toml`, plugin registration, a shared types
   file — sits on exactly one writer's line as `hub:`; peers message that
   owner for the line they need. A tester's line names what the Spec alone
   lets it write; a Spec too thin for that is a reason to open the seat as a
   writer. When nothing splits, the opening line says so and `impl` takes
   every file. Three writers is a legal opening and changes the review split
   (`/plan:delegate` → `<TeamReview/>`).

