# Ask A Friend

**Purpose:** Have a back-and-forth consultation with Codex CLI (OpenAI) on design decisions, bugs, or architectural questions. Each round sends a question to Codex, presents both perspectives, and lets you continue the dialog or wrap up with a final synthesis.

**Usage:** `/ask_a_friend [optional question or elaboration]`

**Arguments:**
- $ARGUMENTS (optional): Specific question or additional context for Codex. If empty, the agent infers the question from the current conversation.

SESSION_DIR = /tmp/claude/ask_a_friend
SCRIPT_PATH = ~/.claude/scripts/ask_a_friend/ask_a_friend.sh
HISTORY_FILE = /tmp/claude/ask_a_friend/history.md
ROUND_NUMBER = 1

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSessionDirectory/>
**STEP 2:** Execute <ComposeInitialQuestion/>
**STEP 3:** Execute <AskCodex/>
**STEP 4:** Execute <PresentRound/>
**STEP 5:** Execute <PromptForFollowUp/>
</ExecutionSteps>

---

<PrepareSessionDirectory>
**Goal:** Create a clean session directory for this consultation.

1. Run: `bash ~/.claude/scripts/ask_a_friend/prepare_session.sh` using Bash with `dangerouslyDisableSandbox: true`
2. Identify the current working directory (the project the user is working in)
3. Store as ${WORKING_DIR} for later use
</PrepareSessionDirectory>

---

<ComposeInitialQuestion>
**Goal:** Write a well-formed question file that gives Codex enough context to provide a useful answer.

**If $ARGUMENTS provided:** Use it as the primary question. Still add conversation context if relevant.

**If $ARGUMENTS empty:** Infer the question from the current conversation — identify the design decision, bug, or architectural question being discussed.

**Write the question file** to ${SESSION_DIR}/question.md using the **Write tool** (NOT Bash heredoc) with this structure:

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
</ComposeInitialQuestion>

---

<AskCodex>
**Goal:** Launch Codex, wait for the answer, and return it.

1. Run `bash ~/.claude/scripts/ask_a_friend/ask_a_friend.sh "/tmp/claude/ask_a_friend" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "Consulting with Codex (round ${ROUND_NUMBER})..."
3. Poll ${SESSION_DIR}/status using the **Read tool**:
   - **If "asking":** Wait a few seconds, check again. Repeat until status changes.
   - **If "answered":** Read ${SESSION_DIR}/answer.txt using the **Read tool**. Store as ${CODEX_ANSWER}. Continue.
   - **If "error":** Read ${SESSION_DIR}/codex.log using the **Read tool**. Show the user the error and stop execution.
</AskCodex>

---

<PresentRound>
**Goal:** Show Codex's answer with Claude's commentary, and record the round in history.

1. **Append to ${HISTORY_FILE}** — read the current contents with Read, then rewrite the full file with the Write tool (NOT Bash heredoc):

```
## Round ${ROUND_NUMBER}

### Question
[The question that was asked this round]

### Codex
[${CODEX_ANSWER}]

### Claude
[Your brief take on Codex's answer]
```

2. **Present to the user:**

```
## Round ${ROUND_NUMBER}

### Codex Says
[Codex's answer, quoted or closely paraphrased. Keep it faithful.]

### My Take
[Brief commentary — where you agree, disagree, or see gaps. Be honest, not diplomatic.]
```
</PresentRound>

---

<PromptForFollowUp>
**Goal:** Let the user decide whether to continue or wrap up.

Present:
```
**follow up** — ask Codex a follow-up question (or just type your question)
**done** — wrap up the consultation
```

**STOP and wait for user response.**

**Handle response:**

- **"done"** (also: "wrap up", "finish", "end", "wrap"): Execute <FinalSynthesis/>

- **Anything else** — treat the user's response as a follow-up question/direction:
  1. Increment ${ROUND_NUMBER}
  2. Execute <ComposeFollowUp/> using the user's message
  3. Execute <AskCodex/>
  4. Execute <PresentRound/>
  5. Execute <PromptForFollowUp/>
</PromptForFollowUp>

---

<ComposeFollowUp>
**Goal:** Write a follow-up question that includes conversation history so Codex has full context.

Write to ${SESSION_DIR}/question.md using the **Write tool** (NOT Bash heredoc):

```
You are being consulted as a second opinion on a software engineering question.
This is a continuing conversation. You have the full history below.
Give a direct, opinionated answer. Be specific and concrete.

## Conversation History

[Contents of ${HISTORY_FILE}]

## Follow-Up

[The user's follow-up message, with any additional context Claude adds to make the question clearer for Codex]
```

**Key principles:**
- Include the full history so Codex can reference prior rounds
- Keep Claude's additions minimal — the user's follow-up is the primary content
- If the user's message is vague (e.g. "what about testing?"), add enough context from the conversation to make it answerable
</ComposeFollowUp>

---

<FinalSynthesis>
**Goal:** Produce a final synthesized recommendation from the full dialog.

**If only 1 round was conducted:** Present a standard synthesis:

```
## Consultation Complete (1 round)

### Codex Says
[Codex's answer, faithful to their response]

### My Take
[Your perspective — agreements, disagreements, additional considerations]

### Recommendation
[Clear, synthesized recommendation. If both agree, say so. If they diverge, explain tradeoffs and give your honest take.]
```

**If multiple rounds were conducted:** Synthesize across all rounds:

```
## Consultation Complete (${ROUND_NUMBER} rounds)

### Key Points from Codex
[Most important points Codex raised across all rounds — not a per-round recap, but a distilled summary]

### Key Points from Claude
[Most important points you raised across all rounds]

### Recommendation
[Clear, synthesized recommendation incorporating the full dialog. Note where consensus was reached and any unresolved disagreements.]
```

**Principles:**
- Be honest about disagreements — don't manufacture consensus
- If Codex raised something you hadn't considered, acknowledge it
- If Codex was wrong about something, explain why
- The user wants the best answer, not diplomatic politeness
</FinalSynthesis>
