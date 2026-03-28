# Ask A Friend

**Purpose:** Have a back-and-forth consultation with Codex CLI (OpenAI) on design decisions, bugs, or architectural questions. Each round sends a question to Codex, presents both perspectives, and lets you continue the dialog or wrap up with a final synthesis.

**Usage:** `/ask_a_friend [optional question or elaboration]`

**Arguments:**
- $ARGUMENTS (optional): Specific question or additional context for Codex. If empty, the agent infers the question from the current conversation.

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSessionDirectory)
SCRIPT_PATH = ~/.claude/scripts/ask_a_friend/ask_a_friend.sh
HISTORY_FILE = ${SESSION_DIR}/history.md
ROUND_NUMBER = 1

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSessionDirectory/>
**STEP 2:** Execute <ComposeInitialQuestion/>
**STEP 3:** Execute <AskCodex/>
**STEP 4:** Execute <PresentRound/>
**STEP 5:** Execute <PromptForFollowUp/>

Note: When the consultation concludes via "done", <FinalSynthesis/> will determine whether to offer implementation. If the consultation was about **how to code something** (design, implementation approach, bug fix strategy, etc.), it will offer <OfferImplementation/>. If it was purely conceptual (architecture discussion, tradeoff analysis, technology choice), it ends after synthesis.
</ExecutionSteps>

---

<PrepareSessionDirectory>
**Goal:** Create a clean session directory for this consultation.

1. Run: `bash ~/.claude/scripts/ask_a_friend/prepare_session.sh` using Bash with `dangerouslyDisableSandbox: true`
2. **Capture ${SESSION_DIR}** from the last line of output (format: `Session ready at <path>`) — extract the path after "Session ready at "
3. Set ${HISTORY_FILE} = ${SESSION_DIR}/history.md
4. Identify the current working directory (the project the user is working in)
5. Store as ${WORKING_DIR} for later use
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

1. Run `bash ~/.claude/scripts/ask_a_friend/ask_a_friend.sh "${SESSION_DIR}" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true`
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

**After presenting the synthesis:** Determine whether the consultation was about **how to code something** — i.e., the question involved implementation approach, writing code, fixing a bug, building a feature, or refactoring. If so, execute <OfferImplementation/>. If the consultation was purely conceptual (architecture comparison, technology choice, design philosophy), stop here.
</FinalSynthesis>

---

<OfferImplementation>
**Goal:** Ask the user whether they want Claude or Codex to implement the recommendation.

Present:
```
### Who should implement this?

**me** — I'll implement it now
**friend** — Codex implements it, then I review the code
```

**STOP and wait for user response.**

**Handle response:**

- **"me"** (also: "you", "claude", "you do it", "go ahead"): Stop the ask_a_friend skill. The user expects you to proceed with normal implementation in the main conversation.

- **"friend"** (also: "codex", "them", "the friend"):
  1. Execute <PrepareImplementationPrompt/>
  2. Execute <LaunchCodexImplementation/>
  3. Execute <ReviewCodexImplementation/>
</OfferImplementation>

---

<PrepareImplementationPrompt>
**Goal:** Write a comprehensive implementation prompt that gives Codex everything it needs to code the agreed-upon solution.

Write to ${SESSION_DIR}/implementation_prompt.md using the **Write tool** (NOT Bash heredoc):

```
You are implementing a code change based on a prior design consultation.
Write the code. Make the changes directly in the codebase.
Do not ask questions — implement the agreed-upon approach.
After making all changes, summarize what you did: which files you created/modified and why.

## Project Context

[Project description, tech stack, relevant directory structure.
Include key file paths that Codex will need to read or modify.]

## What to Implement

[Clear, specific description of the code to write. This should be the synthesized
recommendation from the consultation — not a vague summary, but concrete instructions.
Include:
- Which files to create or modify
- The approach to use (from the consultation consensus)
- Any specific patterns, types, or APIs to follow
- Edge cases or constraints discussed during consultation]

## Consultation Summary

[Key decisions from the consultation that inform the implementation:
- What both Claude and Codex agreed on
- Any specific suggestions from Codex that should be incorporated
- Any warnings or pitfalls identified during the discussion]
```

**Key principles:**
- Be specific and concrete — Codex should be able to implement without ambiguity
- Include file paths it will need to touch
- Reference the consultation consensus, not just one perspective
- Include enough context about surrounding code patterns that Codex writes idiomatic code
- Do NOT dump entire files — summarize relevant patterns and point to files Codex can read itself
</PrepareImplementationPrompt>

---

<LaunchCodexImplementation>
**Goal:** Launch Codex to implement the code, then wait for completion.

1. Run `bash ~/.claude/scripts/ask_a_friend/codex_implement.sh "${SESSION_DIR}" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "Codex is implementing... I'll review the code when it's done."
3. **Wait for the background Bash task notification.** Do NOT poll in a loop.
4. When the notification arrives:
   - Read ${SESSION_DIR}/impl_status
   - **If "implemented":** Read ${SESSION_DIR}/impl_summary.txt. Store as ${IMPL_SUMMARY}. Continue to <ReviewCodexImplementation/>.
   - **If "error":** Read ${SESSION_DIR}/impl_codex.log. Show the user the error and stop.
</LaunchCodexImplementation>

---

<ReviewCodexImplementation>
**Goal:** Review every change Codex made to ensure correctness, then present findings.

**Step 1 — Understand what changed:**
1. Run `git diff` and `git diff --staged` in ${WORKING_DIR} to see all modifications
2. Run `git status` to see new/untracked files
3. Read ${IMPL_SUMMARY} for Codex's own description of what it did

**Step 2 — Review against the consultation:**
For each file Codex created or modified:
1. Read the full file (or the changed sections for large files)
2. Verify the implementation matches the agreed-upon approach from the consultation
3. Check for:
   - Correctness — does the code do what was discussed?
   - Completeness — did Codex implement everything, or miss parts?
   - Code quality — idiomatic patterns, proper error handling, no obvious bugs
   - Consistency — does it match the existing codebase style?

**Step 3 — Present the review:**

```
## Codex Implementation Review

### What Codex Did
[${IMPL_SUMMARY}, or your own summary if Codex's is incomplete]

### Files Changed
[List each file with a brief description of the change]

### Verdict

[One of:]
- **Looks good** — implementation matches what we discussed, code quality is solid
- **Mostly good, minor issues** — list specific issues and offer to fix them
- **Needs changes** — list what's wrong and what needs to be different

### Issues Found (if any)
[For each issue:]
- **File:** path
- **Problem:** what's wrong
- **Fix:** what it should be
```

**Step 4 — Offer to fix issues (if any):**
If you found issues, ask:
```
Want me to fix these issues, or should I ask the friend to take another pass?
```

**If no issues:** Inform the user the implementation looks good and ask if they want to review the diff themselves or proceed.
</ReviewCodexImplementation>
