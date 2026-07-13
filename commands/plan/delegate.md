---
description: Delegate coding work to a configured CLI agent — the main agent orchestrates and the delegate agent codes. Pass a plan doc, a phase, free-text instructions, or any combination. Each phase runs implement → dual blind review → auto-routed fixes → phase review → checkpoint commit; phased plans loop phase-to-phase automatically, stopping only for decisions that block continued implementation. Pass `single` to run one phase with no commit.
---

# Delegate

**Purpose:** The main agent (the session running this command) owns design and orchestration; the configured delegate agent does all coding. This command composes an implementation work order, dispatches it, runs a dual review (a fresh blind delegate session + the main agent's own analysis), synthesizes the results, and routes fixes back to the delegate agent.

**Usage:** `/plan:delegate [plan-doc-path] [phase N] [single] [free-text instructions]`

**Arguments** — all optional, all combinable:
- A path to a design/plan/implementation doc → the work spec
- A phase/section identifier (e.g. `phase 3`, `## Migration` section name) → the starting phase
- `single` → run exactly one phase, then stop (no checkpoint commit, no auto-continue)
- Free text → direct instructions, or amendments/narrowing on top of the doc
- Empty → infer the work from the current conversation (the design just discussed is the work)

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSession)
WORKING_DIR = current project directory (often a worktree checkout, sometimes main — use it as-is; never create a worktree or switch branches)
FIX_PASS = 0 (max 2 per phase; resets in <NextPhase/>)
IMPLEMENTATION_TASK = implementation
MODE = loop when the work comes from a phased plan doc and `single` was not passed; otherwise single

---

## Background wait invariant

This is a hard control-flow rule for every implementation, review, and fix-pass
launcher in this workflow:

- When a background launch returns a task or terminal handle, immediately attach
  the environment's background-wait mechanism to that handle and keep the
  current primary-agent turn open until completion notification arrives.
- Never send a final response, yield the turn back to the user, or substitute
  status-file/process polling while any delegated terminal is still active.
- `run_in_background: true` permits the main agent to do the concurrent work
  named by this workflow; it does not permit the main agent to end its turn.
- When phases or reviews launch in parallel, retain every handle and wait on all
  of them in the same turn. Process each completion while remaining waits stay
  attached. Continue the workflow immediately after the final completion.

Context compaction does not relax this invariant: resume the same active waits
and control flow after compaction.

---

## Loop mode

When the work comes from a phased plan doc, loop mode is the default: run the
named phase (or the first `todo` phase if none was named), then continue
phase-after-phase until the plan is exhausted or a blocking decision stops the
run. `single` opts out.

**Commit authorization.** Invoking this command in loop mode IS the user's
explicit request for checkpoint commits — exactly one per completed phase via
<CheckpointCommit/>, never a push, no other commits. This is the sole exception
to the global never-commit rule.

**Dirty-tree guard.** Before the first dispatch in loop mode, run
`git status --short` in ${WORKING_DIR}. If the tree already has uncommitted
changes, STOP and ask the user how to proceed — a checkpoint must contain one
phase's work and nothing else. Exception: if the selected phased plan document
is the only dirty path, continue without asking. Treat its existing changes as
part of the delegation run and include them in the first phase's checkpoint
commit with that phase's implementation and phase-review updates.

**Blocking vs. deferrable decisions.** Whenever a user decision surfaces (from
reviews, fix passes, or phase review):
- It is purely a sequencing defect — the plan already specifies the required
  behavior and acceptance criteria, but correct work must move, merge, or run
  earlier/later so a phase is usable, testable, or lint-clean → do not treat it
  as a user decision. Edit the phased plan to resequence the existing work,
  preserve every requirement and test owner, recompose the affected Work
  Order, and continue automatically. The main agent may merge phases, split a
  phase, or renumber later integer phases when that is the smallest coherent
  correction. Report the resequencing in one line.
- It blocks the *current* phase's correctness or acceptance gate → STOP and
  present it.
- It affects only later phases and the current phase can complete safely
  without it → write a `**Pending decision:**` block (format:
  `~/.claude/docs/delegate_plan_format.md`) into the affected phase's Work
  Order, tell the user in one line that it was deferred, and continue. The
  pre-dispatch check in <ComposeWorkOrder/> stops the loop when that phase
  actually comes up.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSession/>
**STEP 2:** Execute <ComposeWorkOrder/> (starts with the pending-decision pre-dispatch check)
**STEP 3:** Execute <SelectTask/>
**STEP 4:** Execute <LaunchImplementation/>
**STEP 5:** Execute <DualReview/>
**STEP 6:** Execute <Synthesize/>
**STEP 7:** Execute <RunPhaseReview/> (required when working from a phased plan; pass `auto` in loop mode)
**STEP 8:** Execute <CheckpointCommit/> (loop mode only)
**STEP 9:** Execute <NextPhase/> (loop mode only) — loops back to STEP 2 or ends with <RunSummary/>
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
**Goal:** Write an implementation prompt that lets the delegate agent implement without ambiguity or questions.

0. **Pre-dispatch check (phased plans).** Scan the target phase's Work Order for
   `**Pending decision:**` blocks. If any exist, STOP: present each block to the
   user (it already carries the decision template — problem, current state,
   recommendation), wait for the resolution, edit the resolved outcome into the
   Work Order (Spec / Files / Acceptance gate, then delete the block), and only
   then proceed. Never dispatch a phase carrying an unresolved pending decision.

1. Parse $ARGUMENTS into: doc path (if any), phase/section (if any), `single` token (if present — sets MODE = single), free-text instructions (if any). If all empty, infer the work from the conversation.

2. If a doc path was given, Read it. Decide whether it is a **delegate-ready plan** (per `~/.claude/docs/delegate_plan_format.md`): it has a `## Delegation Context` section **and** the target phase has a `#### Work Order`. Branch:

**FAST PATH — delegate-ready plan. Assemble; do NOT research the codebase.**
`/plan:to_phased_plan` already paid the research cost and baked it into the doc. Build `${SESSION_DIR}/implementation_prompt.md` by copy-and-assemble:
- **Project Context** = the doc's `## Delegation Context` block verbatim + the target phase's **Constraints from prior phases**.
- **Work Specification** = the target phase's **Goal**, **Spec**, and **Files** verbatim + any free-text the user added on the command line.
- **Style Requirements** = the standard block (see fallback template), included only if Delegation Context names a **Style** line.
- **Verification** = Delegation Context **Build / Test / Lint** + the phase **Acceptance gate**. When the **Lint** line names the `clippy` skill, write it into the prompt as "run the `clippy` skill with `auto-proceed`" — a delegate session has no user to answer the skill's batch-approval gate.

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
the delegate agent will need to read or modify. For a phased plan: one-line summaries of
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

[How the delegate agent should verify its work before summarizing: build command, test
command, lint workflow, etc. — match the project's conventions. For Rust repos
with the local `clippy` skill available, use the full `clippy` skill as the lint
gate rather than a partial list of Cargo or `lint ...` commands, and instruct
the delegate agent to run it with `auto-proceed` — no user is present to answer its batch
gate.]
```

**Key principles (fallback):**
- Quote the plan section verbatim — do not paraphrase the spec
- Be specific enough that the delegate agent never has to guess; it cannot ask questions
- Point to files the delegate agent can read itself rather than dumping file contents
- Include the no-commit / no-branch rules verbatim — the delegate agent must leave the tree dirty for review

3. Tell the user in one line what is being dispatched and the prompt path:
   `Dispatching <scope summary> to the delegate agent — prompt at ${SESSION_DIR}/implementation_prompt.md`
   Do NOT ask for confirmation — invoking the command is the authorization.

   If this was the fast path, add: `(assembled from <plan>'s Phase N Work Order — no research)`.
</ComposeWorkOrder>

---

<SelectTask>
Choose the implementation task deliberately; do not scan the prompt for
keywords:

- `implementation` — the default for ordinary feature work.
- `escalation` — use when the Work Order contains genuinely ambiguous
  architecture, numerical/transform mathematics, or a prior behavioral attempt
  failed review.

State the selected task in the dispatch update. `~/.claude/config/agents.conf`
owns both agent and effort in its `[delegate.<family>]` rows; switch the active
delegate family with `/agent`.
</SelectTask>

---

<LaunchImplementation>
**Goal:** Run the delegate agent and wait for completion.

1. Run `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/implementation_prompt.md" "${IMPLEMENTATION_TASK}"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`
2. Inform the user: "The delegate agent is implementing..."
3. Apply the **Background wait invariant**: keep this turn visibly attached to
   the returned handle and wait for the background task notification. Do NOT
   poll status files or end the turn.
4. When it arrives, read ${SESSION_DIR}/impl_status:
   - **"implemented":** Read ${SESSION_DIR}/impl_summary.txt → ${IMPL_SUMMARY}. Continue.
   - **"error":** Read ${SESSION_DIR}/impl_agent.log, show the user the error, stop.
</LaunchImplementation>

---

<DualReview>
**Goal:** Two independent reviews of the diff — a fresh blind delegate session and the main agent's own — running concurrently.

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

**Step 3 — Launch the delegate review:**
Run `bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/review_prompt.md" review` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true`.

Retain the returned handle. The main agent performs Step 4 while the review
runs, then applies the **Background wait invariant** until that handle completes.

**Step 4 — the main agent's own review, while the delegate agent reviews:**
(The main agent MAY read ${IMPL_SUMMARY} — only the delegate reviewer is blind.)
1. Read every changed file (or changed sections for large files)
2. Verify against the spec: correctness, completeness, nothing extra
3. Check codebase consistency and — for Rust — style-guide conformance
4. Note where ${IMPL_SUMMARY}'s claims diverge from what the diff actually shows
5. Record your own findings with the same severity scale

**Step 5 — Collect:** when the background task notification arrives, read ${SESSION_DIR}/review_status:
- **"reviewed":** Read ${SESSION_DIR}/review_findings.txt → ${AGENT_REVIEW}
- **"error":** Read ${SESSION_DIR}/review_agent.log, tell the user the delegate review failed, and proceed on the main agent's review alone (say so explicitly).
</DualReview>

---

<Synthesize>
**Goal:** Merge both reviews and present one verdict.

1. Merge ${AGENT_REVIEW} with your own findings. Dedupe — one entry per real issue, tagged with who caught it (delegate / main agent / both). Discard delegate findings you can refute by reading the code; say which and why.

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
[2-4 sentences, no jargon: what the delegate agent actually built (what it does, not the type
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
[Where the delegate review and yours diverge — give your take without jargon, don't
manufacture consensus.]
```

The numbered items in **What's left** and the rows in **Reference** must use the
same numbers so the user can cross-walk if they want detail.

3. **DIRECT-FIX EXCEPTION (post-review only).** Before offering choices, check
whether every remaining confirmed issue is one of:
- a **documentation-only update** (doc comments, markdown, plan docs — no code
  behavior change), or
- a **trivial change** — a fix so small and mechanical that dispatching a delegate
  session would cost more than the fix itself (a one-line correction, a typo, a
  rename already agreed on). Not trivial: anything touching logic, error
  handling, or more than a couple of lines.

If ALL remaining issues qualify AND both reviews agree on them (the delegate
reviewer flagged it or its review is consistent with it, and the main agent's
own review confirms it), the main agent applies the fixes directly — do NOT ask the user, do NOT
dispatch a fix pass to the delegate agent. Then tell the user in one or two sentences exactly
what was changed and why it qualified (doc-only / trivial). Skip the choice
menu and continue to <RunPhaseReview/>.

If even one remaining issue is substantial, or the reviews disagree, the
exception does not apply — everything routes through the normal choice menu
below. This exception is only available after <DualReview/> has run; it never
applies to the initial implementation.

4. **AUTO-ROUTE.** Otherwise (confirmed blocker or minor issues remain), route
without asking:

   **Defer first** — if an issue is really a decision that affects only later
   phases and the current phase's acceptance gate passes without it, apply the
   blocking-vs-deferrable rule (see Loop mode): record it as a
   `**Pending decision:**` block on the affected phase, tell the user in one
   line, and drop it from this phase's issue list.

   **Resequence before deciding** — if the remaining issue changes only when
   already-specified work happens, apply the pure-sequencing rule from Loop
   mode immediately. Update the plan document, including phase numbering,
   dependencies, Files, Acceptance gates, and test ownership where affected;
   do not ask the user. Re-evaluate the current diff against the revised Work
   Order, then auto-dispatch whatever additional implementation or correction
   is needed for that revised phase. A sequencing change is pure only when it
   preserves product behavior, public API, scope, invariants, and required
   verification. If any of those must change, it remains a real decision and
   follows the normal blocking/deferral rules.

   **Auto fix pass** — when every remaining confirmed issue has an unambiguous
   correct fix (the spec answers it and the two reviews do not conflict on
   intended behavior) and ${FIX_PASS} < 2: increment ${FIX_PASS}, write
   ${SESSION_DIR}/fix_prompt_${FIX_PASS}.md (same structure as the work order,
   spec = the confirmed issues table with file/line specifics, same no-commit
   rules and style requirements), select `${FIX_TASK}` — `mechanical` only
   when every confirmed issue is documentation, formatting, lint guidance, a
   trivial rename, or an equivalently behavior-preserving edit; `escalation`
   when review found incorrect behavior, numerical/transform math, unresolved
   architecture, or a prior fix failed; otherwise `implementation` — then run
   `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_PASS}.md" "${FIX_TASK}"`
   (background, unsandboxed). Tell the user in one line what is being fixed.
   Re-execute <DualReview/> and <Synthesize/> scoped to the new changes.

   **STOP** — when any remaining issue needs a design decision the plan does
   not answer, when the two reviews conflict on *intended behavior* (not just
   severity), or when ${FIX_PASS} >= 2 with blockers remaining. Present the
   two-layer result above plus the choices — each option one sentence, no
   jargon, with a recommendation and the reason for it:

```
Your choice:

1. One more delegate fix pass — [name what gets fixed and the cost].
   ([Recommended / not] because [reason].)
2. Stop here — the parts that matter work; the leftover items become written-down
   todos for later.
3. Talk through any item first.
```

   Do not surface internal bookkeeping (`fix pass 1 of 2 used`) as the headline;
   if the cap is relevant, state it without jargon inside option 1. **Wait for
   the user.**

   - **1:** An explicit user choice overrides the cap — increment ${FIX_PASS}
     and dispatch as in the auto fix pass above.
   - **2:** Continue to <RunPhaseReview/>.
   - **3:** Discuss; afterwards re-offer the options.

If there are no issues (or nits only), state that and continue to <RunPhaseReview/>.

**RULE:** The main agent does not write or edit implementation code in this command unless the user explicitly says so. All fixes route to the delegate agent by default. Sole exception: the direct-fix exception above (doc-only or trivial post-review fixes both reviews agree on).
</Synthesize>

---

<RunPhaseReview>
**Only when the work came from a phased plan doc.** A phase review is mandatory — do not ask, do not offer to skip.

Tell the user in one line: `Phased plan — running /plan:phase_review to update <plan doc> (retrospective + remaining-phase re-evaluation).` Then invoke the `plan:phase_review` skill immediately — **in loop mode pass `auto`**, so user decisions are deferred into the affected Work Orders as `**Pending decision:**` blocks instead of asked inline; the loop stops for them at that phase's pre-dispatch check. When writing the retrospective, include relevant facts from ${AGENT_REVIEW} and the fix passes (e.g. what the blind reviewer caught, what deviated from spec).

If the work was not from a phased plan, skip this step silently and end.
</RunPhaseReview>

---

<CheckpointCommit>
**Loop mode only** — `single` runs end after <RunPhaseReview/> without committing.

1. Run `git status --short` in ${WORKING_DIR} and confirm the changes are this
   phase's implementation plus the plan doc. Anything unexpected → STOP and ask.
2. Stage everything and commit with this message shape:

   ```
   checkpoint(<plan-slug>): phase N — <phase title>

   <one line: what the phase built>

   Claude-Session: <session url>
   ```

3. Edit the phase's status line in the plan doc to ``status: done (`<short hash>`)``,
   then `git add <plan doc> && git commit --amend --no-edit`.
4. Report one line: `Checkpoint <short hash> — phase N: <title>.`

Never push. Never commit anything outside this step.
</CheckpointCommit>

---

<NextPhase>
**Loop mode only.**

1. Find the next `todo` phase in the plan. None left → <RunSummary/> and end.
2. Reset ${FIX_PASS} = 0 and ${IMPLEMENTATION_TASK} = implementation.
3. Announce in one line: `Continuing to phase N — <title>.`
4. Loop back to <ComposeWorkOrder/> (STEP 2). Its pre-dispatch check stops the
   loop if the next phase carries an unresolved `**Pending decision:**` block.
</NextPhase>

---

<RunSummary>
Emitted whenever the loop ends — plan exhausted, blocking stop, or error.

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Deferred decisions still open:** [one line each, naming the phase that owns it — or "none"]
**Why the run stopped:** [plan complete / pending decision on phase N / fix-pass cap on phase N / delegate error]
```

Same translation rules as <Synthesize/>: no reviewer vocabulary, no bare codes —
every line must stand on its own for a reader who has not seen the plan.
</RunSummary>

---

## Rules

- ${WORKING_DIR} is whatever the current project directory is — often a worktree checkout. Never create a worktree or switch branches. The only commits are <CheckpointCommit/> checkpoints in loop mode — one per completed phase, never a push.
- All delegate-launching scripts run with `dangerouslyDisableSandbox: true` and `run_in_background: true`.
- The **Background wait invariant** is mandatory. No active delegate terminal may outlive the primary-agent turn that launched it.
- The delegate reviewer is always a fresh session and always blind to the implementer's summary.
- Delegate launchers record task, family, agent, and effort in the session directory. Never rely on an empty effort silently becoming `xhigh`.
- Select `escalation` from the actual Work Order or review outcome, never keyword matching.
- The main agent orchestrates and reviews; the delegate agent codes. The main agent touches implementation code only on explicit user instruction — except post-review doc-only or trivial fixes that both reviews agree on (see the direct-fix exception in <Synthesize>), which the main agent applies itself and reports.
- Max 2 delegate fix passes per phase before stopping for the user; an explicit user choice of another pass overrides the cap.
- Loop mode stops only for: an unresolved `**Pending decision:**` on the phase being dispatched, a fix that needs a design decision the plan does not answer, reviews conflicting on intended behavior, the fix-pass cap with blockers remaining, or a delegate/environment error. Everything else auto-routes or defers.
- Work orders that name the `clippy` skill as the lint gate must instruct the delegate agent to run it with `auto-proceed`; the main agent likewise passes `auto-proceed` when it runs the `clippy` skill inside the loop.
