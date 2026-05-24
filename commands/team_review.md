# Team Review

**Purpose:** Launch a team of expert agents to analyze a topic from multiple dimensions across one or more refinement cycles. Mechanical findings are auto-recorded to a working doc; judgment-call findings accumulate as proposed user decisions that later cycles can add to, sharpen, or drop. After the final cycle, the surviving proposed decisions are surfaced through `/adhoc_review`.

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

`<RunReviewCycles/>` repeats `<LaunchExpertTeam/>`, `<SynthesizeFindings/>`, `<RecordMechanicalFindings/>`, and `<ReconcileProposedDecisions/>` once per cycle.
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
</DetermineReviewScope>

---

<LaunchExpertTeam>
**Goal:** Launch parallel expert agents to analyze the topic from different dimensions.

Launch **3-5 agents in parallel** using the Agent tool, each with a distinct analytical lens. Choose dimensions appropriate to ${REVIEW_TOPIC}. Common dimensions include:

- **Correctness & Completeness** — Is the approach correct? What's missing? Are there logic errors, edge cases, or gaps?
- **Architecture & Design** — Is the structure sound? Are responsibilities well-separated? Will it scale? Are there simpler alternatives?
- **Risk & Failure Modes** — What can go wrong? What are the assumptions? Where are the fragile points? What happens under unexpected conditions?
- **Implementation Quality** — Is the code clean? Are there performance concerns? Dead code? Unnecessary complexity?
- **Type System & Changeability** (Rust projects) — As an advanced Rust type system expert, evaluate: Are types encoding invariants that the compiler can enforce? Could newtypes, enums, or marker types replace runtime checks? Are state transitions modeled in the type system? Would generics or trait bounds make the code more flexible without sacrificing clarity? Are there stringly-typed patterns or primitive obsession that types could eliminate? Focus on how type-level design affects readability and how easy it is to change the code safely.
- **User Impact & Ergonomics** — How does this affect the end user or developer experience? Is the API intuitive? Are error messages helpful?

**Each agent prompt must include:**
1. The ${REVIEW_TOPIC} and relevant context (file paths, code patterns, architectural decisions)
2. Their specific analytical dimension
3. Instruction to return findings as a structured list where each finding has:
   - **Title**: Brief name for the finding
   - **Problem**: What is wrong or concerning — explain the actual issue with enough context that someone unfamiliar would understand
   - **Impact**: Why it matters (severity: critical / important / minor)
   - **Recommendation**: Specific, actionable suggestion

**Agent configuration:**
- subagent_type: "Explore"
- Each agent should read relevant files to ground their analysis in actual code/content
</LaunchExpertTeam>

---

<SynthesizeFindings>
**Goal:** Merge and deduplicate findings from all agents into a single ordered list.

1. Collect all findings from returned agents
2. Deduplicate — if multiple agents found the same issue, merge into one finding and note the consensus
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

If the user picks `none` (case 3 only), ${WORKING_DOC} is unset and the accumulator lives in conversation instead — workable for a single cycle, but with ${ITERATIONS} > 1 a doc is better so each cycle can build on the last.
</EstablishWorkingDoc>

---

<RecordMechanicalFindings>
**Goal:** Record strictly mechanical findings into ${WORKING_DOC} with no prompt.

A finding is **mechanical** only when its recommendation needs no judgment: one deterministic, low-risk action with a single correct outcome — a typo, a dead import, a naming-consistency rename, a formatting fix, a refactor with one valid result. Anything with a tradeoff, more than one valid approach, behavioral/API impact, or any risk is a **decision** (Step 6). When unsure, treat it as a decision.

Incorporate each mechanical finding into ${WORKING_DOC} where it belongs — fold it into the appropriate existing section, or collect it under a dedicated `## Mechanical (auto-recorded)` section when there is no natural home. Record finding id, title, recommendation, marked accepted. Skip any finding an earlier cycle already recorded. Do not edit source code — this records the decision in the doc; applying it to code is a separate step. If ${WORKING_DOC} is unset, list them inline in the conversation instead.
</RecordMechanicalFindings>

---

<RunReviewCycles>
**Goal:** Refine findings across ${ITERATIONS} cycles before surfacing anything to the user.

Repeat for cycle `i` from 1 to ${ITERATIONS}:

1. Execute <LaunchExpertTeam/>. On cycle 2+, include the current ${WORKING_DOC} contents in every agent prompt — the recorded mechanical findings and the running **Proposed user decisions** — and instruct agents to build on them: confirm, sharpen, merge, refute, or supersede prior proposed decisions, and surface anything new. Agents may argue that a proposed decision is unnecessary.
2. Execute <SynthesizeFindings/>.
3. Execute <RecordMechanicalFindings/>.
4. Execute <ReconcileProposedDecisions/>.
5. Report one line: `Cycle ${i}/${ITERATIONS}: ${M} mechanical recorded, ${D} proposed decisions (${added} added, ${dropped} dropped this cycle).` Do not surface decisions for review yet.

Never invoke `/adhoc_review` inside the loop — surfacing happens once, after the final cycle.
</RunReviewCycles>

---

<ReconcileProposedDecisions>
**Goal:** Maintain one evolving set of proposed user decisions in ${WORKING_DOC} across cycles.

Keep proposed decisions under a `## Proposed user decisions` section — or inline under the appropriate section of the doc when that reads better. Each entry carries: a stable id, title, severity, source dimension, the concrete problem, impact, recommendation, and a status (`proposed` / `superseded` / `dropped`).

For this cycle's judgment findings, reconcile against the existing entries:
- **New** (not already represented) → add as `proposed`.
- **Duplicate or refinement** of an existing entry → merge into it, keeping the sharper wording.
- **Contradicts or obsoletes** an existing entry → mark that entry `dropped` (or `superseded`) with a one-line reason. A later cycle marking an earlier proposed decision unnecessary is expected and allowed.

Only entries still `proposed` after the final cycle get surfaced; keep `dropped`/`superseded` ones in the doc with their reason so a future run does not relitigate them. If ${WORKING_DOC} is unset, hold this set in conversation instead.
</ReconcileProposedDecisions>

---

<SurfaceDecisions>
**Goal:** After the final cycle, surface the surviving proposed user decisions, reusing /adhoc_review.

The decision set = every `## Proposed user decisions` entry still marked `proposed` (dropped/superseded entries are not surfaced).

- 0 surviving decisions → skip; tell the user every finding was mechanical or was dropped across cycles, all recorded in ${WORKING_DOC}.
- Otherwise → invoke `/adhoc_review` on the surviving decisions with ${WORKING_DOC} already in scope, so it records each decision there.

Each handed-off item must carry title, severity, source dimension (the expert lens that found it), the concrete problem, impact, and a recommendation — so adhoc_review can show its summary, expand on `elaborate`, and mark the recommended choice. Do not run a separate walkthrough; adhoc_review owns the per-item interaction.
</SurfaceDecisions>
