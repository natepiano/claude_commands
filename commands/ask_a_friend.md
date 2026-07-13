# Ask A Friend

**Purpose:** Have a back-and-forth consultation with an external CLI agent on design decisions, bugs, or architectural questions. Each round sends a question to your friend, presents both perspectives, and lets you continue the dialog or wrap up with a final synthesis.

**Usage:** `/ask_a_friend [optional question or elaboration]`

**Arguments:**
- $ARGUMENTS (optional): Specific question or additional context for your friend. If empty, the agent infers the question from the current conversation.

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSessionDirectory)
SCRIPT_PATH = ~/.claude/scripts/ask_a_friend/ask_a_friend.sh
HISTORY_FILE = ${SESSION_DIR}/history.md
ROUND_NUMBER = 1

The consultation and implementation scripts resolve model/effort from the
`[ask_a_friend.<family>]` rows in `~/.claude/config/agents.conf`; `/agent` switches the family.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSessionDirectory/>
**STEP 2:** Execute <ComposeInitialQuestion/>
**STEP 3:** Execute <AskFriend/>
**STEP 4:** Execute <PresentRound/>
**STEP 5:** Execute <PromptForFollowUp/>

Note: <PromptForFollowUp/> now offers all options (follow-up, implement, quit) in a single survey after each round. When the user chooses "quit", <FinalSynthesis/> presents the final summary. Implementation choices (options 2 and 3) are handled directly from <PromptForFollowUp/>.
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
**Goal:** Write a well-formed question file that gives your friend enough context to provide a useful answer.

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
so your friend can agree, disagree, or suggest alternatives.]
```

**Key principles:**
- Be concise but include enough context for a useful answer
- Frame as a specific, answerable question — not a vague "what do you think?"
- Include the current thinking so your friend can push back on it if warranted
- Do NOT include full file contents — summarize relevant code patterns
</ComposeInitialQuestion>

---

<AskFriend>
**Goal:** Launch your friend, wait for the answer, and return it.

1. Run `bash ~/.claude/scripts/ask_a_friend/ask_a_friend.sh "${SESSION_DIR}" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "Consulting with your friend (round ${ROUND_NUMBER})..."
3. Poll ${SESSION_DIR}/status using the **Read tool**:
   - **If "asking":** Wait a few seconds, check again. Repeat until status changes.
   - **If "answered":** Read ${SESSION_DIR}/answer.txt using the **Read tool**. Store as ${FRIEND_ANSWER}. Continue.
   - **If "error":** Read ${SESSION_DIR}/agent.log using the **Read tool**. Show the user the error and stop execution.
</AskFriend>

---

<PresentRound>
**Goal:** Show your friend's answer with Claude's commentary, and record the round in history.

1. **Append to ${HISTORY_FILE}** — read the current contents with Read, then rewrite the full file with the Write tool (NOT Bash heredoc):

```
## Round ${ROUND_NUMBER}

### Question
[The question that was asked this round]

### Your Friend
[${FRIEND_ANSWER}]

### Claude
[Your brief take on your friend's answer]
```

2. **Present to the user:**

```
## Round ${ROUND_NUMBER}

### Your Friend Says
[Your friend's answer, quoted or closely paraphrased. Keep it faithful.]

### My Take
[Brief commentary — where you agree, disagree, or see gaps. Be honest, not diplomatic.]
```
</PresentRound>

---

<PromptForFollowUp>
**Goal:** Let the user decide what to do next.

Present using a numbered survey:
```
What next?

1. **Ask a follow-up** — type your question below
2. **You implement** — I'll implement the recommendation now
3. **Let your friend implement** — your friend implements it, then I review the code
4. **Quit** — end the consultation
```

**STOP and wait for user response.**

**Handle response:**

- **1 or any follow-up question text** — treat the user's response as a follow-up question/direction:
  1. Increment ${ROUND_NUMBER}
  2. Execute <ComposeFollowUp/> using the user's message
  3. Execute <AskFriend/>
  4. Execute <PresentRound/>
  5. Execute <PromptForFollowUp/>

- **2** (also: "you", "you implement", "claude", "you do it", "go ahead"): Stop the ask_a_friend skill. The user expects you to proceed with normal implementation in the main conversation.

- **3** (also: "friend", "agent", "codex", "them", "the friend", "let your friend"):
  1. Execute <PrepareImplementationPrompt/>
  2. Execute <LaunchFriendImplementation/>
  3. Execute <ReviewFriendImplementation/>

- **4** (also: "quit", "done", "wrap up", "finish", "end", "wrap"): Execute <FinalSynthesis/>
</PromptForFollowUp>

---

<ComposeFollowUp>
**Goal:** Write a follow-up question that includes conversation history so your friend has full context.

Write to ${SESSION_DIR}/question.md using the **Write tool** (NOT Bash heredoc):

```
You are being consulted as a second opinion on a software engineering question.
This is a continuing conversation. You have the full history below.
Give a direct, opinionated answer. Be specific and concrete.

## Conversation History

[Contents of ${HISTORY_FILE}]

## Follow-Up

[The user's follow-up message, with any additional context Claude adds to make the question clearer for your friend]
```

**Key principles:**
- Include the full history so your friend can reference prior rounds
- Keep Claude's additions minimal — the user's follow-up is the primary content
- If the user's message is vague (e.g. "what about testing?"), add enough context from the conversation to make it answerable
</ComposeFollowUp>

---

<FinalSynthesis>
**Goal:** Produce a final synthesized recommendation from the full dialog.

**If only 1 round was conducted:** Present a standard synthesis:

```
## Consultation Complete (1 round)

### Your Friend Says
[Your friend's answer, faithful to their response]

### My Take
[Your perspective — agreements, disagreements, additional considerations]

### Recommendation
[Clear, synthesized recommendation. If both agree, say so. If they diverge, explain tradeoffs and give your honest take.]
```

**If multiple rounds were conducted:** Synthesize across all rounds:

```
## Consultation Complete (${ROUND_NUMBER} rounds)

### Key Points from Your Friend
[Most important points your friend raised across all rounds — not a per-round recap, but a distilled summary]

### Key Points from Claude
[Most important points you raised across all rounds]

### Recommendation
[Clear, synthesized recommendation incorporating the full dialog. Note where consensus was reached and any unresolved disagreements.]
```

**Principles:**
- Be honest about disagreements — don't manufacture consensus
- If your friend raised something you hadn't considered, acknowledge it
- If your friend was wrong about something, explain why
- The user wants the best answer, not diplomatic politeness

**After presenting the synthesis:** Stop. The consultation is complete. (Implementation options were already offered in <PromptForFollowUp/> during the conversation.)
</FinalSynthesis>

---

<PrepareImplementationPrompt>
**Goal:** Write a comprehensive implementation prompt that gives your friend everything it needs to code the agreed-upon solution.

Write to ${SESSION_DIR}/implementation_prompt.md using the **Write tool** (NOT Bash heredoc):

```
You are implementing a code change based on a prior design consultation.
Write the code. Make the changes directly in the codebase.
Do not ask questions — implement the agreed-upon approach.
After making all changes, summarize what you did: which files you created/modified and why.

## Project Context

[Project description, tech stack, relevant directory structure.
Include key file paths that your friend will need to read or modify.]

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
- What both Claude and your friend agreed on
- Any specific suggestions from your friend that should be incorporated
- Any warnings or pitfalls identified during the discussion]
```

**Key principles:**
- Be specific and concrete — your friend should be able to implement without ambiguity
- Include file paths it will need to touch
- Reference the consultation consensus, not just one perspective
- Include enough context about surrounding code patterns that your friend writes idiomatic code
- Do NOT dump entire files — summarize relevant patterns and point to files your friend can read itself
</PrepareImplementationPrompt>

---

<LaunchFriendImplementation>
**Goal:** Launch your friend to implement the code, then wait for completion.

1. Run `bash ~/.claude/scripts/ask_a_friend/implement.sh "${SESSION_DIR}" "${WORKING_DIR}"` using the Bash tool with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "Your friend is implementing... I'll review the code when it's done."
3. **Wait for the background Bash task notification.** Do NOT poll in a loop.
4. When the notification arrives:
   - Read ${SESSION_DIR}/impl_status
   - **If "implemented":** Read ${SESSION_DIR}/impl_summary.txt. Store as ${IMPL_SUMMARY}. Continue to <ReviewFriendImplementation/>.
   - **If "error":** Read ${SESSION_DIR}/impl_agent.log. Show the user the error and stop.
</LaunchFriendImplementation>

---

<ReviewFriendImplementation>
**Goal:** Review every change your friend made to ensure correctness, then present findings.

**Step 1 — Understand what changed:**
1. Run `git diff` and `git diff --staged` in ${WORKING_DIR} to see all modifications
2. Run `git status` to see new/untracked files
3. Read ${IMPL_SUMMARY} for your friend's own description of what it did

**Step 2 — Review against the consultation:**
For each file your friend created or modified:
1. Read the full file (or the changed sections for large files)
2. Verify the implementation matches the agreed-upon approach from the consultation
3. Check for:
   - Correctness — does the code do what was discussed?
   - Completeness — did your friend implement everything, or miss parts?
   - Code quality — idiomatic patterns, proper error handling, no obvious bugs
   - Consistency — does it match the existing codebase style?

**Step 3 — Present the review:**

```
## Friend Implementation Review

### What Your Friend Did
[${IMPL_SUMMARY}, or your own summary if your friend's is incomplete]

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
</ReviewFriendImplementation>
