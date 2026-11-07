---
description: Review a topic through lenses in the invoking session.
---

# Team Review

**Purpose:** Review a topic through expert lenses in the invoking session across one or more refinement cycles.
Record mechanical findings in a working document.
Retain proposed user decisions across cycles and refine them with each review.
After the final cycle, present the remaining decisions through `/adhoc_review`.

**Critical execution guards:**
- The working doc established by `<EstablishWorkingDoc/>` is final for this workflow and for the `/adhoc_review` handoff. Do not ask where to record decisions again.
- Hard-filter proposed decisions before surfacing, through `<DecisionEconomy/>`. If a finding has converged into a concrete in-intent plan refinement with no meaningful user choice left, record it in `${WORKING_DOC}` and do not send it to `/adhoc_review`.
- Surface only unresolved product/design choices where the user must choose among plausible alternatives or explicitly approve a scope/behavior change.

`<DecisionEconomy/>` is defined by this import, shared with every session and with `/unit:direct`:

@~/.claude/docs/decision_criteria.md

Apply `<DecisionEconomy/>` to the review's findings.
Use `<IntentFirewall/>` to classify findings across cycles.
Treat each finding as evidence for a decision, not as a completed decision.
Agreement across lenses does not settle a judgment call.
An objection does not create a user decision when `<DecisionEconomy/>` already determines the result.

When the topic is a design/plan/spec, the review is **bound to that design's stated intent**: by default it strengthens the design to achieve that intent and does *not* relitigate whether to pursue it. Challenges to the premise are quarantined, not run as the default mode — see `<IntentFirewall/>`. This exists because an unbound multi-cycle review will, given enough cycles, talk itself into "should this exist?" and bury the work that was actually asked for.

**Usage:** `/team_review [topic or question to review] [N]`

**Arguments:**
- $ARGUMENTS (optional): The topic, file, design, or code area to review. If empty, infer from the current conversation. A standalone integer N (leading or trailing) sets the number of review cycles to run before surfacing anything to the user — default 1. Example: `/team_review 3` runs 3 cycles, then one `/adhoc_review` over the refined set.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <DetermineReviewScope/>
**STEP 2:** Execute <EstablishWorkingDoc/>
**STEP 3:** Execute <RunReviewCycles/>
**STEP 4:** Execute <SurfaceDecisions/>

`<RunReviewCycles/>` repeats `<ReviewLenses/>`, `<SynthesizeFindings/>`, `<RecordMechanicalFindings/>`, and `<ReconcileProposedDecisions/>` once per cycle.

`<IntentFirewall/>` is established in STEP 1 and governs every cycle and the surfacing step whenever ${REVIEW_POSTURE} = `strengthen`.
</ExecutionSteps>

---

<DetermineReviewScope>
**Goal:** Establish what is being reviewed, how many cycles to run, and confirm with the user.

**Parse the cycle count first:** if $ARGUMENTS contains a standalone integer N (leading or trailing token), set ${ITERATIONS}=N and strip it from the topic text. Otherwise ${ITERATIONS}=1.

**If a topic remains in $ARGUMENTS:**
- Use it as ${REVIEW_TOPIC}
- Inform the user: "Launching team review of: ${REVIEW_TOPIC} (${ITERATIONS} cycle(s))"

**If no topic remains:**
- Summarize the current conversation topic in 1-2 sentences as ${REVIEW_TOPIC}
- Inform the user: "Launching team review of the current topic: [summary] (${ITERATIONS} cycle(s))"

**Capture the stated intent and posture (this governs the whole review).**
If ${REVIEW_TOPIC} is a design/plan/spec (a doc, or one in the conversation), read its goal and set:
- ${REVIEW_INTENT} = the design's stated objective in the document's own words — what it is trying to achieve and the approach it has committed to.
- ${REVIEW_POSTURE}:
  - **strengthen** (default) — the approach is decided; the review's job is to make the design correctly and robustly achieve ${REVIEW_INTENT}. Challenges to whether/what to do are quarantined (see `<IntentFirewall/>`).
  - **open** — the document or the user explicitly invites deciding whether to do it at all, or choosing among approaches.

Choose **strengthen** unless the doc or the user explicitly says the premise is open. State both back in one line and accept a one-word correction:
> `Reviewing ${REVIEW_TOPIC} — intent: <one line>; posture: strengthen. (Premise-challenges are quarantined; reply "open" to relitigate the approach.)`

If there is no committed design to extract (e.g. an open-ended "review this code area"), set ${REVIEW_POSTURE}=open and ${REVIEW_INTENT}=the topic itself.
</DetermineReviewScope>

---

<IntentFirewall>
**Goal:** Keep the review serving the design's stated intent; stop premise-relitigation from hijacking cycles or becoming the headline. Applies whenever ${REVIEW_POSTURE} = **strengthen**.

Every finding is one of three classes:
1. **auto-recorded** — single correct outcome that doesn't change plan structure or intended behavior; includes mechanical fixes *and* determined correctness/bug fixes (see `<RecordMechanicalFindings/>`).
2. **design-improvement** — an in-intent judgment call: the design is committed to ${REVIEW_INTENT}; this makes it more correct, more robust, simpler *within the chosen approach*, or safer. This is the review's job and the bulk of every cycle. It is surfaced to the user only if it changes plan structure or intended behavior; a correctness fix that doesn't is auto-recorded, not surfaced.
3. **premise-challenge** — asserts the approach is wrong, unnecessary, or should be deferred/abandoned, or proposes a *different* approach to the same intent.

Rules under **strengthen**:
- A premise-challenge is admissible **only** with decisive, evidence-backed proof that the committed design *cannot* achieve ${REVIEW_INTENT} — a concrete correctness or feasibility defect, not a preference. The following are **not** admissible grounds and must be dropped on sight: "a simpler alternative exists," "the benefit is marginal/cosmetic," "the original motivating bug is already fixed," "it is more code / more risk," "the scope is large." Those are tradeoffs the *author* already weighed by committing to the intent — they are not the review's call.
- At most **one** premise-challenge survives a cycle; merge any others into it. Label it `PREMISE-CHALLENGE`; never weave it through design-improvement findings.
- A premise-challenge must **never** be presented as, or folded into, the recommendation for a design-improvement decision (no "defer" / "don't do it" recommendation attached to an in-intent item).
- Keep each lens within the committed approach.
  Treat questions about worth, deferral, or a different approach as premise-challenges under the rule above.
  Do not increase their weight in the next cycle.

If a premise-challenge looks decisive, do **not** silently act on it and do **not** let it consume the remaining cycles. Carry it to `<SurfaceDecisions/>` in its own flagged slot for the user to rule on.

Under ${REVIEW_POSTURE} = **open**: premise-challenges are first-class; this firewall does not apply.
</IntentFirewall>

---

<ReviewLenses>
**Goal:** Review the topic through distinct lenses.

The invoking session runs each lens itself, one after the other, in the chosen order.

1. Choose 3–5 lenses for each cycle.
2. On the first cycle, create an absolute `${SESSION_DIR}` under `/tmp/claude/team_review/<uuid>/` for the findings.

On the first cycle, run this preamble before the first lens.

```
Before evaluating, Read ~/rust/nate_style/review-charter.md — its ranked values, hard rules, and finding schema govern every finding you return. Follow its style-guide loading rule when the subject is Rust.
```

Choose dimensions appropriate to `${REVIEW_TOPIC}`.
For code and designs that shape code, cover simple, fast, beautiful, ergonomics, and type-system design.
One lens can cover two dimensions.
Common dimensions include:

- **Correctness & Completeness** — Does the design correctly and completely achieve ${REVIEW_INTENT}? What's missing, what edge cases or gaps would stop it working as intended? (Whether the *approach itself* is correct is a premise-challenge — see `<IntentFirewall/>`.)
- **Architecture & Design** — Given the committed approach, is the structure sound, are responsibilities well-separated, does it achieve ${REVIEW_INTENT} cleanly, and will it scale *within that approach*? A "simpler alternative" or "don't do this" is a premise-challenge — raise it only under `<IntentFirewall/>`, not as a default lens.
- **Risk & Failure Modes** — What can go wrong? What are the assumptions? Where are the fragile points? What happens under unexpected conditions?
- **Implementation Quality** — Is the code clean? Are there performance concerns? Dead code? Unnecessary complexity?
- **Type System & Changeability** (Rust projects) — As an advanced Rust type system expert, evaluate: Are types encoding invariants that the compiler can enforce? Could newtypes, enums, or marker types replace runtime checks? Are state transitions modeled in the type system? Would generics or trait bounds make the code more flexible without sacrificing clarity? Are there stringly-typed patterns or primitive obsession that types could eliminate? Focus on how type-level design affects readability and how easy it is to change the code safely. Trait/generic recommendations are bound by the loaded style rules (single-implementer traits and their extension-point exception included) — no caller-side type ceremony.
- **Performance** — What runtime cost does the design impose: allocation or clones at boundaries, dynamic dispatch where static works, per-call work that setup could amortize, per-frame churn (Bevy)? Per the charter, a recommendation that adds cost must name it; performance ceremony with no hot-path justification is equally a finding.
- **User Impact & Ergonomics** — How does this affect the end user or developer experience? Is the API intuitive? Are error messages helpful?

**For each lens:**

Under `strengthen`, apply this mandate: _"This intent and approach are a given. Your job is to make the design correctly and robustly achieve it — not to decide whether to do it or to propose a different approach. Only if you have decisive proof the design cannot achieve the intent, raise exactly one finding labeled PREMISE-CHALLENGE (see firewall). 'A simpler alternative exists', 'marginal benefit', 'the original bug is already fixed', and 'more code/risk' are not grounds to challenge."_

Use this finding schema:
   - **Title**: Brief name for the finding
   - **Where**: File paths + item names (or doc section) the finding is about
   - **Class**: `design-improvement` or `PREMISE-CHALLENGE` (omit for mechanical findings)
   - **Problem**: What is wrong or concerning — explain the actual issue with enough context that someone unfamiliar would understand
   - **Impact**: Why it matters (severity: critical / important / minor)
   - **Recommendation**: Specific, actionable suggestion

Read the relevant files once each cycle, before the first lens.
Check each lens's findings against them.

1. Write `${SESSION_DIR}/cycle${CYCLE_NUMBER}_findings_N.txt` for lens N, after each lens, before the next lens.
2. Write an explicit no-findings result when the lens finds no issue.
3. Report any incomplete lens before synthesis.

Never treat an incomplete review as a no-findings result.
</ReviewLenses>

---

<SynthesizeFindings>
**Goal:** Merge and deduplicate the lens findings into one ordered list.

1. Read each `${SESSION_DIR}/cycle${CYCLE_NUMBER}_findings_N.txt` from the current cycle.
2. Merge duplicate findings and note which lenses found the issue.
3. Order by severity: critical first, then important, then minor
4. Assign each finding a sequential number (F1, F2, F3...)
5. Store as ${FINDINGS_LIST}
</SynthesizeFindings>

---

<EstablishWorkingDoc>
**Goal:** Settle which doc to work in. It holds the auto-recorded mechanical findings and the evolving proposed user decisions, and carries state across cycles — so settle it before the first cycle. **Default to the doc already in scope; do not spin up a separate file.**

Find ${WORKING_DOC} in this order:
1. ${REVIEW_TOPIC} is itself a doc (a file path the review was pointed at) → that file is ${WORKING_DOC}.
2. A doc the user has been editing or named this session → use it.
3. Neither → suggest one and ask once:
   > `Team review of ${REVIEW_TOPIC}, ${ITERATIONS} cycle(s). No doc in scope — record findings and proposed decisions in .claude/reviews/team-review-${brief-topic}.md, a different path, or none?`

When a doc is already in scope (cases 1-2): do **not** create a separate file and do **not** ask — just confirm `Working in <path>.` and record everything into that doc (inline in the appropriate sections, or a dedicated section where that reads better).

This working-doc choice also governs `<SurfaceDecisions/>`. If `/adhoc_review` is invoked by this workflow, skip its document-selection/setup question and use `${WORKING_DOC}` directly.

If the user picks `none` (case 3 only), ${WORKING_DOC} is unset and the accumulator lives in conversation instead — workable for a single cycle, but with ${ITERATIONS} > 1 a doc is better so each cycle can build on the last.
</EstablishWorkingDoc>

---

<RecordMechanicalFindings>
**Goal:** Record strictly mechanical findings into ${WORKING_DOC} with no prompt.

A finding is **auto-recorded** (no prompt) when its recommendation has a single correct outcome *and* does not change the plan's structure or its intended behavior. This covers both (a) mechanical fixes — typo, dead import, naming-consistency rename, formatting, a refactor with one valid result — and (b) **correctness/bug fixes** where the right fix is determined even if finding it took judgment (a compile break, a missing guard, a wrong read path, a stale doc reference). A finding becomes a **decision** carried into `<ReconcileProposedDecisions/>` *only* when it changes the plan's structure or the intended behavior — more than one valid answer, a new API/feature, or a scope/ordering change.

When the classification is unclear, apply the `<DecisionEconomy/>` test and its settled categories.

Incorporate each mechanical finding into ${WORKING_DOC} where it belongs — fold it into the appropriate existing section, or collect it under a dedicated `## Mechanical (auto-recorded)` section when there is no natural home. Record finding id, title, recommendation, marked accepted. Skip any finding an earlier cycle already recorded. Do not edit source code — this records the decision in the doc; applying it to code is a separate step. If ${WORKING_DOC} is unset, list them inline in the conversation instead.
</RecordMechanicalFindings>

---

<RunReviewCycles>
**Goal:** Refine findings across ${ITERATIONS} cycles before surfacing anything to the user.

Repeat for `${CYCLE_NUMBER}` from 1 to `${ITERATIONS}`:

1. Execute <ReviewLenses/>. On cycle 2+, confirm, sharpen, merge, refute, or supersede earlier proposed decisions, and identify new findings.
2. Execute <SynthesizeFindings/>.
3. Execute <RecordMechanicalFindings/>.
4. Execute <ReconcileProposedDecisions/>.
5. Report one line: `Cycle ${CYCLE_NUMBER}/${ITERATIONS}: ${M} mechanical recorded, ${D} proposed decisions (${added} added, ${dropped} dropped this cycle).` Do not surface decisions for review yet.

Never invoke `/adhoc_review` inside the loop — surfacing happens once, after the final cycle.
</RunReviewCycles>

---

<ReconcileProposedDecisions>
**Goal:** Maintain one evolving set of proposed user decisions in ${WORKING_DOC} across cycles.

Keep proposed decisions under a `## Proposed user decisions` section — or inline under the appropriate section of the doc when that reads better. Each entry carries: a stable id, title, severity, source dimension, **class** (`design-improvement` / `premise-challenge`), the concrete problem, impact, recommendation, and a status (`proposed` / `superseded` / `dropped`).

Under `strengthen`, keep premise-challenges in their own short sub-list, capped at one (merge or drop the rest per `<IntentFirewall/>`), and never let a premise-challenge attach a "defer / don't do it" recommendation to a `design-improvement` entry.

For this cycle's judgment findings, reconcile against the existing entries:
- **New** (not already represented) → add as `proposed`.
- **Duplicate or refinement** of an existing entry → merge into it, keeping the sharper wording.
- **Contradicts or obsoletes** an existing entry → mark that entry `dropped` (or `superseded`) with a one-line reason. A later cycle marking an earlier proposed decision unnecessary is expected and allowed.

Before the final surface step, apply `<DecisionEconomy/>` as the hard decision gate. Every entry still marked `proposed` must survive it:
- **Record, do not surface:** anything `<DecisionEconomy/>` already decides, plus converged refinements, correctness fixes, implementation constraints, acceptance-test additions, and API clarifications that have one sensible in-intent outcome after review. Record the call and the risk it carries in one line in `${WORKING_DOC}`; a call the user never sees was taken from them, not made for them.
- **Surface:** only what cannot be cheaply undone — unresolved choices where the user must pick among plausible alternatives, approve a scope or behavior change, or accept a tradeoff the team could not settle, and where being wrong costs lost work, a published artifact, or the wrong thing built.
- **Drop/supersede:** items made redundant by sharper later-cycle wording.

Only entries still `proposed` after this gate get surfaced; keep `dropped`/`superseded` ones in the doc with their reason so a future run does not relitigate them. If ${WORKING_DOC} is unset, hold this set in conversation instead.
</ReconcileProposedDecisions>

---

<SurfaceDecisions>
**Goal:** After the final cycle, surface the surviving proposed user decisions, reusing /adhoc_review.

The decision set = every `## Proposed user decisions` entry still marked `proposed` (dropped/superseded entries are not surfaced).

- 0 surviving decisions → skip; tell the user every finding was mechanical or was dropped across cycles, all recorded in ${WORKING_DOC}.
- Otherwise → invoke `/adhoc_review` on the surviving `design-improvement` decisions with ${WORKING_DOC} already in scope, so it records each decision there. This is a handoff, not a fresh command invocation: do **not** run `adhoc_review`'s document-selection/setup prompt, and do **not** ask where to record decisions.

**A surviving `premise-challenge` (strengthen posture) is surfaced separately, not mixed into the `/adhoc_review` list.** Present it once, explicitly flagged: `This contradicts the document's stated intent (${REVIEW_INTENT}). It is admitted only because <decisive evidence>. Confirm the premise is open before I treat it as actionable.` Do not give it a recommendation that presumes the answer, and do not let it override the in-intent decisions. If the user does not open the premise, it stays recorded as a flagged note, not an action.

Each handed-off item must carry title, severity, source dimension (the expert lens that found it), the concrete problem, impact, and a recommendation — so adhoc_review can show its summary, expand on `elaborate`, and mark the recommended choice. Start the handoff at `adhoc_review`'s item walkthrough using `${WORKING_DOC}`; do not run a separate walkthrough and do not repeat adhoc setup.
</SurfaceDecisions>
