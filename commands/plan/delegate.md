---
description: Delegate coding work to a Codex CLI session — the main agent orchestrates, codex codes. Pass a plan doc, a phase, free-text instructions, or any combination. After implementation, a fresh blind codex session reviews the diff in parallel with the main agent's own review; the main agent synthesizes both and presents a verdict.
---

# Delegate

**Purpose:** The main agent (the session running this command) is the design/orchestration brain; codex does all coding. This command composes an implementation work order, dispatches it to codex, runs a dual review (a fresh blind codex session + the main agent's own analysis), synthesizes the results, and routes fixes back to codex.

**Usage:** `/plan:delegate [plan-doc-path] [phase N] [free-text instructions]`

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
**STEP 6:** Execute <RunPhaseReview/> (required when working from a phased plan)
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

2. If a doc path was given, Read it. Decide whether it is a **delegate-ready plan** (per `~/.claude/docs/delegate_plan_format.md`): it has a `## Delegation Context` section **and** the target phase has a `#### Work Order`. Branch:

**FAST PATH — delegate-ready plan. Assemble; do NOT research the codebase.**
`/plan:to_phased_plan` already paid the research cost and baked it into the doc. Build `${SESSION_DIR}/implementation_prompt.md` by copy-and-assemble:
- **Project Context** = the doc's `## Delegation Context` block verbatim + the target phase's **Constraints from prior phases**.
- **Work Specification** = the target phase's **Goal**, **Spec**, and **Files** verbatim + any free-text the user added on the command line.
- **Style Requirements** = the standard block (see fallback template), included only if Delegation Context names a **Style** line.
- **Verification** = Delegation Context **Build / Test / Lint** + the phase **Acceptance gate**.

Prepend the boilerplate header (the first paragraph of the template below) and write the file with the **Write tool**. Do not open codebase files to fill gaps — if a needed fact is absent, the *plan* is at fault: name the gap in one line, proceed with what the doc gives, and let the review catch the rest. This path should cost a few thousand tokens, not tens of thousands.

**FALLBACK PATH — no Work Order (free-text, conversation-inferred, or a pre-`/plan:to_phased_plan` doc).**
Research and compose. If a doc path was given, extract the applicable phase/section. Write `${SESSION_DIR}/implementation_prompt.md` with the **Write tool** (NOT Bash heredoc) using this template:

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
  zsh ~/.claude/scripts/rust_style/load-rust-style.sh --project-root <WORKING_DIR>
Read every style file marked [non-negotiable] in the loaded checklist and any
guideline files relevant to the code you are changing (full paths are shown in
the checklist, e.g. ~/rust/nate_style/rust/<rule>.md or repo-local docs/style/*.md).
Follow them in all code you write.

## Verification

[How codex should verify its work before summarizing: build command, test
command, lint workflow, etc. — match the project's conventions. For Rust repos
with the local `clippy` skill available, use the full `clippy` skill as the lint
gate rather than a partial list of Cargo or `lint ...` commands.]
```

**Key principles (fallback):**
- Quote the plan section verbatim — do not paraphrase the spec
- Be specific enough that codex never has to guess; it cannot ask questions
- Point to files codex can read itself rather than dumping file contents
- Include the no-commit / no-branch rules verbatim — codex must leave the tree dirty for review

3. Tell the user in one line what is being dispatched and the prompt path:
   `Dispatching <scope summary> to codex — prompt at ${SESSION_DIR}/implementation_prompt.md`
   Do NOT ask for confirmation — invoking the command is the authorization.

   If this was the fast path, add: `(assembled from <plan>'s Phase N Work Order — no research)`.
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
**Goal:** Two independent reviews of the diff — a fresh blind codex session and the main agent's own — running concurrently.

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

**Step 4 — the main agent's own review, while codex reviews:**
(The main agent MAY read ${IMPL_SUMMARY} — only the codex reviewer is blind.)
1. Read every changed file (or changed sections for large files)
2. Verify against the spec: correctness, completeness, nothing extra
3. Check codebase consistency and — for Rust — style-guide conformance
4. Note where ${IMPL_SUMMARY}'s claims diverge from what the diff actually shows
5. Record your own findings with the same severity scale

**Step 5 — Collect:** when the background task notification arrives, read ${SESSION_DIR}/review_status:
- **"reviewed":** Read ${SESSION_DIR}/review_findings.txt → ${CODEX_REVIEW}
- **"error":** Read ${SESSION_DIR}/review_codex.log, tell the user the codex review failed, and proceed on the main agent's review alone (say so explicitly).
</DualReview>

---

<Synthesize>
**Goal:** Merge both reviews and present one verdict.

1. Merge ${CODEX_REVIEW} with your own findings. Dedupe — one entry per real issue, tagged with who caught it (codex / claude / both). Discard codex findings you can refute by reading the code; say which and why.

**TRANSLATE — do not pass reviewer vocabulary through.** The user has not read
the plan, the diff, or the two reviews. Every line you present must stand on its
own. A reference the user has never seen is invisible to them — replace it with
what it *does*. **Banned unless defined in the same sentence:** bare finding
numbers from the reviewers, plan-decision codes (`D2`, `I3`), test / guard /
tripwire / file identifiers used as if the user knows them, and tooling terms
(`headless` → "the automated tests on this machine can't drive a real screen";
`binding` / `bind group` → say what the data connection actually is). If you
cannot say what a finding *means in behavior terms*, you do not understand it
well enough to present it — read the code until you can.

**Never use the word "plain" or any variant** (`plain terms`, `plain language`,
`plain English`, `in plain terms`) anywhere in the output — not in a header, a
label, or a sentence. Just write the summary that way; do not announce that you
are. This is absolute.

2. Present in two layers — readable summary first, technical reference second:

```
## Delegation Result

### Where things stand
[2-4 sentences, no jargon: what codex actually built (what it does, not the type
names), whether anything visibly changed yet, and that the important parts work
/ pass tests. Written for someone who has not seen the code.]

### What's left
[One numbered item per confirmed issue. For EACH, with no jargon:
- A title naming the real problem (not the reviewer's title).
- 1-2 sentences: what the actual behavior or risk is, what breaks or could break.
- How much it matters: does it happen in normal use, is it a rare fallback, or
  is the code already correct and this only guards against a future edit?
- Cost to fix: cheap vs. involved, and any hard limit (e.g. "can only be written
  correctly, not proven here, because it needs a real screen").
If there are no issues, say so in one sentence and skip the table below.]

### Reference (file/line for the fix pass)
| # | Severity | File:line | Problem (technical) | Caught by |

### Reviewer disagreements (if any)
[Where codex's review and yours diverge — give your take without jargon, don't
manufacture consensus.]
```

The numbered items in **What's left** and the rows in **Reference** must use the
same numbers so the user can cross-walk if they want detail.

3. **DIRECT-FIX EXCEPTION (post-review only).** Before offering choices, check
whether every remaining confirmed issue is one of:
- a **documentation-only update** (doc comments, markdown, plan docs — no code
  behavior change), or
- a **trivial change** — a fix so small and mechanical that dispatching a codex
  session would cost more than the fix itself (a one-line correction, a typo, a
  rename already agreed on). Not trivial: anything touching logic, error
  handling, or more than a couple of lines.

If ALL remaining issues qualify AND both reviews agree on them (the codex
reviewer flagged it or its review is consistent with it, and the main agent's
own review confirms it), the main agent applies the fixes directly — do NOT ask the user, do NOT
dispatch a fix pass to codex. Then tell the user in one or two sentences exactly
what was changed and why it qualified (doc-only / trivial). Skip the choice
menu and continue to <RunPhaseReview/>.

If even one remaining issue is substantial, or the reviews disagree, the
exception does not apply — everything routes through the normal choice menu
below. This exception is only available after <DualReview/> has run; it never
applies to the initial implementation.

4. Otherwise, if there are blocker or minor issues, offer the choices the same way — each
option one sentence, no jargon, with a recommendation and the reason for it:

```
Your choice:

1. One more codex fix pass — [name what gets fixed and the cost].
   ([Recommended / not] because [reason].)
2. Stop here — the parts that matter work; the leftover items become written-down
   todos for later.
3. Talk through any item first.
```

Do not surface internal bookkeeping (`fix pass 1 of 2 used`) as the headline; if
the cap is relevant, state it without jargon inside option 1.

**STOP and wait for the user.**

- **1:** If ${FIX_PASS} >= 2, tell the user the fix-pass cap is reached and recommend either accepting, discussing, or explicitly authorizing the main agent to fix directly. Otherwise increment ${FIX_PASS}, write ${SESSION_DIR}/fix_prompt_${FIX_PASS}.md (same structure as the work order, spec = the confirmed issues table with file/line specifics, same no-commit rules and style requirements), run `codex_implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_PASS}.md"` (background, unsandboxed), then re-execute <DualReview/> and <Synthesize/> scoped to the new changes.
- **2:** Continue to <RunPhaseReview/>.
- **3:** Discuss; afterwards re-offer the options.

If there are no issues (or nits only), state that and continue to <RunPhaseReview/>.

**RULE:** The main agent does not write or edit implementation code in this command unless the user explicitly says so. All fixes route to codex by default. Sole exception: the direct-fix exception above (doc-only or trivial post-review fixes both reviews agree on).
</Synthesize>

---

<RunPhaseReview>
**Only when the work came from a phased plan doc.** A phase review is mandatory — do not ask, do not offer to skip.

Tell the user in one line: `Phased plan — running /plan:phase_review to update <plan doc> (retrospective + remaining-phase re-evaluation).` Then invoke the `plan:phase_review` skill immediately. When writing the retrospective, include relevant facts from ${CODEX_REVIEW} and the fix passes (e.g. what the blind reviewer caught, what deviated from spec).

If the work was not from a phased plan, skip this step silently and end.
</RunPhaseReview>

---

## Rules

- ${WORKING_DIR} is whatever the current project directory is — often a worktree checkout. Never create a worktree, switch branches, or commit.
- All codex-launching scripts run with `dangerouslyDisableSandbox: true` and `run_in_background: true`.
- The codex reviewer is always a fresh session and always blind to the implementer's summary.
- The main agent orchestrates and reviews; codex codes. The main agent touches implementation code only on explicit user instruction — except post-review doc-only or trivial fixes that both reviews agree on (see the direct-fix exception in <Synthesize>), which the main agent applies itself and reports.
- Max 2 codex fix passes per delegation before escalating to the user.
