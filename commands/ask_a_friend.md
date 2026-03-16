# Ask A Friend

**Purpose:** Consult with Codex CLI (OpenAI) as a second opinion on the current design discussion or a specific question. Launches Codex non-interactively, polls for its response, then synthesizes the best answer from both perspectives.

**Usage:** `/ask_a_friend [optional question or elaboration]`

**Arguments:**
- $ARGUMENTS (optional): Specific question or additional context for Codex. If empty, the agent infers the question from the current conversation.

SESSION_DIR = /tmp/claude/ask_a_friend
SCRIPT_PATH = ~/.claude/scripts/ask_a_friend/ask_a_friend.sh

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSessionDirectory/>
**STEP 2:** Execute <ComposeQuestion/>
**STEP 3:** Execute <LaunchCodex/>
**STEP 4:** Execute <PollForAnswer/>
**STEP 5:** Execute <SynthesizeResponse/>
**STEP 6:** Execute <StructureFindings/>
**STEP 7:** Execute <FindingsSummaryTable/> from @~/.claude/shared/findings_walkthrough.md
**STEP 8:** Execute <FindingsWalkthrough/> from @~/.claude/shared/findings_walkthrough.md
**STEP 9:** Execute <FindingsCompletion/> from @~/.claude/shared/findings_walkthrough.md
</ExecutionSteps>

---

<PrepareSessionDirectory>
**Goal:** Create a clean session directory for this consultation.

1. Run: `rm -rf /tmp/claude/ask_a_friend && mkdir -p /tmp/claude/ask_a_friend` using Bash with `dangerouslyDisableSandbox: true`
2. Identify the current working directory (the project the user is working in)
3. Store as ${WORKING_DIR} for later use
</PrepareSessionDirectory>

---

<ComposeQuestion>
**Goal:** Write a well-formed question file that gives Codex enough context to provide a useful answer.

**If $ARGUMENTS provided:** Use it as the primary question. Still add conversation context if relevant.

**If $ARGUMENTS empty:** Infer the question from the current conversation — identify the design decision, bug, or architectural question being discussed.

**Write the question file** using Bash with `dangerouslyDisableSandbox: true` (e.g. `cat > /tmp/claude/ask_a_friend/question.md << 'QUESTION_EOF' ... QUESTION_EOF`) with this structure:

```
You are being consulted as a second opinion on a software engineering question.
Give a direct, opinionated answer. Be specific and concrete. If you disagree
with an approach, say so and explain why.

## Context

[Brief description of the project, relevant tech stack, and what the user is working on.
Include key file paths, type names, or architectural decisions that are relevant.]

## Question

[The specific question — either from $ARGUMENTS or inferred from conversation.
Frame it as a clear, answerable question.]

## Current Thinking

[Summarize the current direction or approach being considered in the conversation,
so Codex can agree, disagree, or suggest alternatives.]
```

**Key principles:**
- Be concise but include enough context for a useful answer
- Frame as a specific, answerable question — not a vague "what do you think?"
- Include the current thinking so Codex can push back on it if warranted
- Do NOT include full file contents — summarize relevant code patterns
</ComposeQuestion>

---

<LaunchCodex>
**Goal:** Launch Codex in the background and return control immediately.

1. Run `bash ~/.claude/scripts/ask_a_friend/ask_a_friend.sh "/tmp/claude/ask_a_friend" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true` (Codex CLI requires unsandboxed access to macOS system APIs)
2. Inform the user: "Consulting with Codex... I'll check back shortly."
</LaunchCodex>

---

<PollForAnswer>
**Goal:** Wait for Codex to finish and retrieve the answer.

1. Read the file `/tmp/claude/ask_a_friend/status` using Bash with `dangerouslyDisableSandbox: true`
2. **If "asking":** Wait a few seconds, then check status again. Repeat until status changes.
3. **If "answered":** Read `/tmp/claude/ask_a_friend/answer.txt` using Bash with `dangerouslyDisableSandbox: true` — proceed to <SynthesizeResponse/>
4. **If "error":** Read `/tmp/claude/ask_a_friend/codex.log` using Bash with `dangerouslyDisableSandbox: true` for diagnostics. Inform the user:
   ```
   Codex consultation failed. Log output:
   [relevant log lines]
   ```
   Stop execution.
</PollForAnswer>

---

<SynthesizeResponse>
**Goal:** Present a synthesized answer combining Codex's perspective with your own.

**Format:**

```
## Codex Says

[Codex's answer, quoted or closely paraphrased. Keep it faithful to their response.]

## My Take

[Your own perspective on the same question. Where do you agree? Disagree?
What additional considerations does Codex miss or what do they add that you hadn't considered?]

## Recommendation

[A clear, synthesized recommendation. If both agree, say so confidently.
If they diverge, explain the tradeoffs and give your honest recommendation.]
```

**Principles:**
- Be honest about disagreements — don't just rubber-stamp Codex's answer
- If Codex raises a point you hadn't considered, acknowledge it
- If Codex is wrong about something, explain why
- The user wants the best answer, not diplomatic consensus
</SynthesizeResponse>

---

<StructureFindings>
**Goal:** Extract actionable points from the synthesized response into ${FINDINGS_LIST} format for the shared walkthrough.

Analyze the Recommendation section from <SynthesizeResponse/> and decompose into individual findings:

1. For each distinct actionable point, create a finding with:
   - `id`: Sequential (F1, F2, F3...)
   - `title`: Brief name for the point
   - `severity`: critical / important / minor — based on how strongly both perspectives agree
   - `problem`: The issue or consideration being raised
   - `impact`: Why it matters for the current decision
   - `recommendation`: The specific actionable suggestion
   - `source`: "Codex", "Claude", or "Both" depending on origin

2. Order by severity: critical first, then important, then minor

3. Store as ${FINDINGS_LIST}

4. Set ${SOURCE_SUMMARY} = "Codex and Claude perspectives synthesized"
5. Set ${REVIEW_TOPIC} = the question that was asked

**If Codex's answer is a single coherent recommendation with no decomposable points** (e.g. a straightforward yes/no answer, or a single design recommendation), skip the walkthrough entirely and end with the synthesis output from <SynthesizeResponse/>.
</StructureFindings>
