# Team Review

**Purpose:** Launch a team of expert agents to conduct a dimensional analysis of a topic, then walk the user through findings interactively with approve/fix/discuss/deny decisions. Accumulates decisions for optional plan generation.

**Usage:** `/team_review [topic or question to review]`

**Arguments:**
- $ARGUMENTS (optional): The topic, file, design, or code area to review. If empty, infer from the current conversation.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <DetermineReviewScope/>
**STEP 2:** Execute <LaunchExpertTeam/>
**STEP 3:** Execute <SynthesizeFindings/>
**STEP 4:** Execute <FindingsSummaryTable/> from @~/.claude/shared/findings_walkthrough.md
**STEP 5:** Execute <FindingsWalkthrough/> from @~/.claude/shared/findings_walkthrough.md
**STEP 6:** Execute <FindingsCompletion/> from @~/.claude/shared/findings_walkthrough.md
</ExecutionSteps>

---

<DetermineReviewScope>
**Goal:** Establish what is being reviewed and confirm with the user.

**If $ARGUMENTS provided:**
- Use as the review topic
- Inform the user: "Launching team review of: $ARGUMENTS"

**If $ARGUMENTS empty:**
- Summarize the current conversation topic in 1-2 sentences
- Inform the user: "Launching team review of the current topic: [summary]"

Store the review topic as ${REVIEW_TOPIC}.
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

<TeamReviewContext>
**Context for shared walkthrough:**
- ${SOURCE_SUMMARY} = "Reviewed by ${N} expert agents"
- Each finding's `source` field should name the expert dimension that identified it
</TeamReviewContext>
