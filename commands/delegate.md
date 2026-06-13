---
description: Delegate coding work to a Codex CLI session — Claude orchestrates, codex codes. Pass a plan doc, a phase, free-text instructions, or any combination. After implementation, a fresh blind codex session reviews the diff in parallel with Claude's own review; Claude synthesizes both and presents a verdict.
---

# Delegate

**Purpose:** Claude is the design/orchestration brain; codex does all coding. This command composes an implementation work order, dispatches it to codex, runs a dual review (a fresh blind codex session + Claude's own analysis), synthesizes the results, and routes fixes back to codex.

**Usage:** `/delegate [plan-doc-path] [phase N] [free-text instructions]`

**Arguments** — all optional, all combinable:
- A path to a design/plan/implementation doc → the work spec
- A phase/section identifier (e.g. `phase 3`, `## Migration` section name) → narrows the doc to this session's scope
- Free text → direct instructions, or amendments/narrowing on top of the doc
- Empty → infer the work from the current conversation (the design just discussed is the work)

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSession)
WORKING_DIR = current project directory (often a worktree checkout, sometimes main — use it as-is; never create a worktree or switch branches)
FIX_PASS = 0 (max 2)

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSession/>
**STEP 2:** Execute <ComposeWorkOrder/>
**STEP 3:** Execute <LaunchImplementation/>
**STEP 4:** Execute <DualReview/>
**STEP 5:** Execute <Synthesize/>
**STEP 6:** Execute <OfferPhaseReview/> (only when working from a phased plan)
</ExecutionSteps>

---

<PrepareSession>
**Goal:** Create a clean session directory.

1. Run: `bash ~/.claude/scripts/delegate/prepare_session.sh` using Bash with `dangerouslyDisableSandbox: true`
2. **Capture ${SESSION_DIR}** from the last line of output (format: `Session ready at <path>`)
3. Store the current project directory as ${WORKING_DIR}
</PrepareSession>

---

<ComposeWorkOrder>
**Goal:** Write an implementation prompt that lets codex implement without ambiguity or questions.

1. Parse $ARGUMENTS into: doc path (if any), phase/section (if any), free-text instructions (if any). If all empty, infer the work from the conversation.
2. If a doc path was given, Read it. Extract the applicable phase/section.
3. Write ${SESSION_DIR}/implementation_prompt.md using the **Write tool** (NOT Bash heredoc):

```
You are implementing a code change. Write the code. Make the changes directly
in the codebase. Do not ask questions — implement the spec below.
Do NOT commit. Do NOT create branches. Do NOT touch files outside this task's scope.
After making all changes, summarize what you did: which files you
created/modified and why, and any deviations from the spec with reasons.

## Project Context

[Project description, tech stack, relevant directory structure, key file paths
codex will need to read or modify. For a phased plan: one-line summaries of
already-completed phases and any retrospective facts that constrain this phase.]

## Work Specification

[The applicable plan section quoted VERBATIM, plus the user's free-text
instructions. If inferred from conversation: a concrete, complete spec —
files to create/modify, the approach, types/APIs/patterns to follow,
edge cases and constraints discussed.]

## Style Requirements   ← include this section only for Rust work

Before writing any code, run:
  zsh ~/.claude/scripts/load-rust-style.sh --project-root <WORKING_DIR>
Read every style file marked [non-negotiable] in the loaded checklist and any
guideline files relevant to the code you are changing (full paths are shown in
the checklist, e.g. ~/rust/nate_style/rust/<rule>.md or repo-local docs/style/*.md).
Follow them in all code you write.

## Verification

[How codex should verify its work before summarizing: build command, test
command, clippy, etc. — match the project's conventions.]
```

**Key principles:**
- Quote the plan section verbatim — do not paraphrase the spec
- Be specific enough that codex never has to guess; it cannot ask questions
- Point to files codex can read itself rather than dumping file contents
- Include the no-commit / no-branch rules verbatim — codex must leave the tree dirty for review

4. Tell the user in one line what is being dispatched and the prompt path:
   `Dispatching <scope summary> to codex — prompt at ${SESSION_DIR}/implementation_prompt.md`
   Do NOT ask for confirmation — invoking the command is the authorization.
</ComposeWorkOrder>

---

<LaunchImplementation>
**Goal:** Run codex and wait for completion.

1. Run `bash ~/.claude/scripts/delegate/codex_implement.sh "${SESSION_DIR}" "${WORKING_DIR}"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "Codex is implementing..."
3. **Wait for the background task notification.** Do NOT poll in a loop.
4. When it arrives, read ${SESSION_DIR}/impl_status:
   - **"implemented":** Read ${SESSION_DIR}/impl_summary.txt → ${IMPL_SUMMARY}. Continue.
   - **"error":** Read ${SESSION_DIR}/impl_codex.log, show the user the error, stop.
</LaunchImplementation>

---

<DualReview>
**Goal:** Two independent reviews of the diff — a fresh blind codex session and Claude's own — running concurrently.

**Step 1 — Capture the diff:**
Run `git diff` and `git status --short` in ${WORKING_DIR}. For untracked new files, read them and include their contents.

**Step 2 — Compose the review prompt.** Write ${SESSION_DIR}/review_prompt.md using the **Write tool**:

```
You are reviewing a code change you did not write. You have the specification
it was implemented from and the full diff. Review independently and critically.
You have read-only access to the codebase — read surrounding code as needed.

Report findings as a numbered list. Each finding: one-line title, 1-3 sentence
body naming file and line, and a severity tag:
- blocker  — wrong behavior, spec violation, or missing required work
- minor    — works but has a real defect (error handling, edge case, quality)
- nit      — style/polish only

End with a one-line verdict: APPROVE, APPROVE WITH FIXES, or REQUEST CHANGES.
If you find nothing, say so explicitly — do not invent findings.

## Specification

[The same work spec sent to the implementer — verbatim]

## Diff

[git diff output + contents of new untracked files]

## Review Questions

1. Does the implementation match the specification — complete and correct?
2. Any bugs, missed edge cases, or broken error handling?
3. Anything implemented that the spec did not ask for?
4. Does it fit the existing codebase's patterns?
```

**BLINDNESS RULE:** the review prompt must NOT contain ${IMPL_SUMMARY} or any hint of what the implementer claims it did. Spec + diff only.

**Step 3 — Launch the codex review:**
Run `bash ~/.claude/scripts/delegate/codex_review.sh "${SESSION_DIR}" "${WORKING_DIR}"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`.

**Step 4 — Claude's own review, while codex reviews:**
(Claude MAY read ${IMPL_SUMMARY} — only the codex reviewer is blind.)
1. Read every changed file (or changed sections for large files)
2. Verify against the spec: correctness, completeness, nothing extra
3. Check codebase consistency and — for Rust — style-guide conformance
4. Note where ${IMPL_SUMMARY}'s claims diverge from what the diff actually shows
5. Record your own findings with the same severity scale

**Step 5 — Collect:** when the background task notification arrives, read ${SESSION_DIR}/review_status:
- **"reviewed":** Read ${SESSION_DIR}/review_findings.txt → ${CODEX_REVIEW}
- **"error":** Read ${SESSION_DIR}/review_codex.log, tell the user the codex review failed, and proceed on Claude's review alone (say so explicitly).
</DualReview>

---

<Synthesize>
**Goal:** Merge both reviews and present one verdict.

1. Merge ${CODEX_REVIEW} with your own findings. Dedupe — one entry per real issue, tagged with who caught it (codex / claude / both). Discard codex findings you can refute by reading the code; say which and why.

2. Present:

```
## Delegation Result

### What Codex Built
[Concise summary — from the diff, cross-checked against ${IMPL_SUMMARY}]

### Verdict
[Looks good / Mostly good, minor issues / Needs changes]

### Confirmed Issues (if any)
| # | Severity | File | Problem | Caught by |

### Reviewer Disagreements (if any)
[Where codex's review and yours diverge — give your honest take, don't manufacture consensus]
```

3. If there are blocker or minor issues, offer:

```
What next?

1. **Codex fix pass** — I'll send the confirmed issues back to a codex session
2. **Accept as-is** — leave the remaining issues
3. **Discuss** — talk through specific findings first
```

**STOP and wait for the user.**

- **1:** If ${FIX_PASS} >= 2, tell the user the fix-pass cap is reached and recommend either accepting, discussing, or explicitly authorizing Claude to fix directly. Otherwise increment ${FIX_PASS}, write ${SESSION_DIR}/fix_prompt_${FIX_PASS}.md (same structure as the work order, spec = the confirmed issues table with file/line specifics, same no-commit rules and style requirements), run `codex_implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_PASS}.md"` (background, unsandboxed), then re-execute <DualReview/> and <Synthesize/> scoped to the new changes.
- **2:** Continue to <OfferPhaseReview/>.
- **3:** Discuss; afterwards re-offer the options.

If there are no issues (or nits only), state that and continue to <OfferPhaseReview/>.

**RULE:** Claude does not write or edit implementation code in this command unless the user explicitly says so. All fixes route to codex by default.
</Synthesize>

---

<OfferPhaseReview>
**Only when the work came from a phased plan doc.** Ask the user:

`Run /phase_review to update <plan doc> (retrospective + remaining-phase re-evaluation)?`

If yes, invoke the `phase_review` skill. When writing the retrospective, include relevant facts from ${CODEX_REVIEW} and the fix passes (e.g. what the blind reviewer caught, what deviated from spec).

If the work was not from a phased plan, skip this step silently and end.
</OfferPhaseReview>

---

## Rules

- ${WORKING_DIR} is whatever the current project directory is — often a worktree checkout. Never create a worktree, switch branches, or commit.
- All codex-launching scripts run with `dangerouslyDisableSandbox: true` and `run_in_background: true`.
- The codex reviewer is always a fresh session and always blind to the implementer's summary.
- Claude orchestrates and reviews; codex codes. Claude touches implementation code only on explicit user instruction.
- Max 2 codex fix passes per delegation before escalating to the user.
