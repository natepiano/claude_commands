---
description: Delegate coding work to a configured CLI agent — the main agent orchestrates and the delegate agent codes. Each phase runs implement → dual blind review → auto-routed fixes → phase review → checkpoint commit. Phased plans run automatically by default; pass `verbose` for a pre-phase explanation and approval gate before every phase, or `single` for one phase with no commit.
---

# Delegate

**Purpose:** The main agent (the session running this command) owns design and orchestration; the configured delegate agent does all coding. This command composes an implementation work order, dispatches it, runs a dual review (a fresh blind delegate session + the main agent's own analysis), synthesizes the results, and routes fixes back to the delegate agent.

**Usage:** `/plan:delegate [plan-doc-path] [phase N] [single|verbose] [auto next N phases|auto through phase X] [free-text instructions]`

**Arguments** — all optional; `single` and `verbose` are mutually exclusive:
- A path to a design/plan/implementation doc → the work spec
- A phase/section identifier (e.g. `phase 3`, `## Migration` section name) → the starting phase
- `single` → run exactly one phase, then stop (no checkpoint commit, no auto-continue)
- `verbose` → before every phase, explain why it exists, the work it will do,
  and the important types/APIs it will introduce or change; wait for explicit
  approval, then run and checkpoint that phase, output only its reviewed result,
  and wait for `continue` before showing the next phase's briefing
- `auto next N phases` / `auto through phase X` → with `verbose`, temporarily
  auto-run a bounded phase range and then restore the verbose approval gate
- Free text → direct instructions, or amendments/narrowing on top of the doc
- Empty → infer the work from the current conversation (the design just discussed is the work)

SESSION_DIR = (captured from prepare_session.sh output — see PrepareSession)
WORKING_DIR = current project directory (often a worktree checkout, sometimes main — use it as-is; never create a worktree or switch branches)
FIX_PASS = 0 (max 4 per phase; resets in <NextPhase/>)
IMPLEMENTATION_TASK = implementation
APPLICATION_SMOKE_RESULT = not_run (resets in <NextPhase/>)
MODE = single when `single` was passed or the work is not phased; verbose when
`verbose` was passed for a phased plan; otherwise loop
AUTO_WINDOW = none (verbose mode only; may become `next N` or `through <phase>`
at a <VerbosePrePhaseGate/>, then returns to none automatically)

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

## Delegate heartbeat

Every dispatch in a session — implementation, blind review, fix passes,
escalations — shares one `${SESSION_DIR}/heartbeat.log`, so a single read shows
the whole session's timeline. Beat lines: `<ISO time> [wrapper|agent]
<message>`.

- **Header block** — each launcher (`implement.sh`, `review.sh`) opens its run
  with `---- <ISO time> [<role> (<family>/<model>:<effort>)] ----`
  (an empty resolved effort shows as `unset`, never silently) followed by
  the responsibility text the main agent passed as the launcher's 5th
  argument: 1-2 lines saying what this specific run is responsible for (e.g.
  `Phase 3 — implement the parser Work Order` or `Fix pass 2 — clippy
  findings from the dual review`). Always pass it; the scripts' fallback text
  is generic.
- `[wrapper]` lines come from the launcher: one beat every 60s while the
  delegate process is alive, tagged with the role. They prove liveness only.
- `[agent]` lines are the delegate's own narration, written via
  `~/.claude/scripts/agents/heartbeat.sh` immediately before each new activity
  ("implementing the parser changes", "running clippy for verification"). The
  delegate cannot write during a blocking command, so an agent line's age
  measures how long the last-named activity has been running — it is not
  staleness.
- **Handoff rule:** a delegate only narrates if its prompt names the concrete
  heartbeat file path — delegates have no `${SESSION_DIR}` variable. Every
  write-mode prompt (work order and fix prompts) must carry the heartbeat
  paragraph with the path substituted. Review prompts must NOT: the reviewer's
  read-only sandbox cannot write, so reviews contribute header + `[wrapper]`
  beats only, via `review.sh`.

Reading rules — the **Background wait invariant** stands unchanged:

- Read the file on demand, as a single read. Never in a wait loop, and never as
  a completion signal; the background task notification remains the only wait
  mechanism.
- Read it when: the user interjects during a wait and asks what is happening
  (report the last few lines in real words); when resuming after context
  compaction (one read to re-establish where the delegate is); or when a
  delegate has run far longer than its scope suggests (one staleness check).
- Interpretation: fresh `[wrapper]` lines + an old `[agent]` line mean the
  delegate is alive and has been in the named activity that long — flag it to
  the user only if that duration is implausible for the activity. `[wrapper]`
  lines older than ~150s (2.5x the 60s cadence) mean the delegate process is
  dead — expect the task notification imminently; do not act before it arrives.
  Read entries below the most recent header block as belonging to that run.

---

## Multi-phase modes

When the work comes from a phased plan doc, auto/loop mode is the default: run the
named phase (or the first `todo` phase if none was named), then continue
phase-after-phase until the plan is exhausted or a blocking decision stops the
run. `verbose` opts into a checkpointed human gate before every phase, including
the selected starting phase; `single` opts out of multi-phase execution and
checkpoint commits entirely.

### Verbose mode

`verbose` does not authorize the selected phase immediately. After assembling
the selected phase's work order, run <VerbosePrePhaseGate/> before any delegate
dispatch and wait. Approval authorizes exactly that phase's implementation,
dual review, fixes, phase review, and checkpoint commit. After completion, run
<VerbosePostPhaseReport/> and, outside a bounded-auto window, wait at
<VerbosePostPhaseGate/>. The post-phase report contains only the completed
phase's reviewed summary. `continue` advances only far enough to assemble and
show the next phase's <VerbosePrePhaseGate/>; it never authorizes that phase.
Even a completely correct phase never authorizes the next phase.

At the gate, accept these controls:

- `proceed` or `approved` — run only the phase currently described,
  then return to another verbose gate before the next phase.
- `auto next N phases` — run the currently described phase and enough
  subsequent phases to total the positive integer N automatically, then return
  to verbose mode after the Nth checkpoint.
- `auto through phase X` — run the currently described phase through X inclusive, then
  return to verbose mode after X's checkpoint.
- `stop` — emit <RunSummary/> and end without starting the described phase.

The user may include amendments with `proceed`; append the remaining text to the
current phase's assembled implementation prompt before dispatch. Post-phase
`continue` never amends or dispatches a phase. Validate an auto-window target
before dispatch: it must identify the current or a later todo phase in the current plan. Plan
exhaustion, a blocking decision, or an error still ends/stops the window early.
An auto window changes only phase-to-phase advancement; every implementation,
review, fix, phase-review, and checkpoint rule remains active.

The same bounded-auto controls may accompany the initial `verbose` invocation.
There, the selected starting phase counts as the first `auto next N` phase, and
`auto through phase X` includes both the selected phase and X. Examples:

```
/plan:delegate docs/hana/tool-graph.md phase 0a verbose
/plan:delegate docs/hana/tool-graph.md phase 0a verbose auto through phase 0j
```

**Commit authorization.** Invoking this command in loop mode IS the user's
explicit request for checkpoint commits. In verbose mode, approval at
<VerbosePrePhaseGate/> authorizes exactly one implementation and checkpoint, or
the phases named by an explicit bounded-auto control. Each completed phase gets
exactly one commit via <CheckpointCommit/>, never a push, no other commits. This
is the sole exception to the global never-commit rule.

**Dirty-tree guard.** Before the first dispatch in loop or verbose mode, run
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
  pre-dispatch check in <ComposeWorkOrder/> stops either multi-phase mode when
  that phase actually comes up.

---

<ExecutionSteps>
**EXECUTE THESE STEPS IN ORDER:**

**STEP 1:** Execute <PrepareSession/>
**STEP 2:** Execute <ComposeWorkOrder/> (starts with the pending-decision pre-dispatch check)
**STEP 3:** In verbose mode with no active auto window, execute
<VerbosePrePhaseGate/> and wait
**STEP 4:** Execute <SelectTask/>
**STEP 5:** Execute <LaunchImplementation/>
**STEP 6:** Execute <DualReview/>
**STEP 7:** Execute <Synthesize/>
**STEP 8:** Execute <RunApplicationSmokeTest/>
**STEP 9:** Execute <RunPhaseReview/> (required for phased plans; pass `auto` in either multi-phase mode)
**STEP 10:** Execute <CheckpointCommit/> (loop and verbose modes only)
**STEP 11:** In verbose mode execute <VerbosePostPhaseReport/>
**STEP 12:** In verbose mode execute <VerbosePostPhaseGate/> when applicable
**STEP 13:** Execute <NextPhase/> (loop and verbose modes only) — auto-continues,
returns to STEP 2 for the next pre-phase gate, or ends with <RunSummary/>
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

1. Parse $ARGUMENTS into: doc path (if any), phase/section (if any), `single`
   token, `verbose` token, optional bounded-auto control (`auto next N phases`
   or `auto through phase X`), and free-text instructions. Remove recognized
   controls from the free text. Parse the complete bounded-auto phrase before
   interpreting standalone `phase X`, because `auto through phase X` contains a
   phase token that is not the starting phase. If both `single` and `verbose`
   appear, or if a bounded-auto control appears without `verbose`, STOP and ask which execution
   contract the user wants. Require a positive N. If all arguments are empty,
   infer the work from the conversation.

2. If a doc path was given, Read it. Decide whether it is a **delegate-ready plan** (per `~/.claude/docs/delegate_plan_format.md`): it has a `## Delegation Context` section **and** the target phase has a `#### Work Order`. Branch:

   `verbose` requires a phased delegate-ready plan. If the target has no phased
   Work Orders, STOP and explain that there is no next-phase boundary to gate;
   offer `single` for that work instead.

   Validate an initial bounded-auto range against the plan before dispatch. For
   `auto next N phases`, set `AUTO_WINDOW = next N` and count the selected phase
   as the first. For `auto through phase X`, require X to be the selected or a
   later todo phase and set `AUTO_WINDOW = through X`.

**FAST PATH — delegate-ready plan. Assemble; do NOT research the codebase.**
`/plan:to_phased_plan` already paid the research cost and baked it into the doc. Build `${SESSION_DIR}/implementation_prompt.md` by copy-and-assemble:
- **Project Context** = the doc's `## Delegation Context` block verbatim + the target phase's **Constraints from prior phases**.
- **Work Specification** = the target phase's **Goal**, **Spec**, and **Files** verbatim + any free-text the user added on the command line.
- **Style Requirements** = the standard block (see fallback template), included only if Delegation Context names a **Style** line.
- **Verification** = Delegation Context **Build / Test / Lint / Run / Smoke**
  entries that exist + the phase **Acceptance gate**. When the **Lint** line
  names the `clippy` skill, write it into the prompt as "run the `clippy` skill
  with `auto-proceed`" — a delegate session has no user to answer the skill's
  batch-approval gate. The main agent still owns the mandatory live application
  smoke test in <RunApplicationSmokeTest/>; delegate verification never
  substitutes for it.

Prepend the boilerplate header (the first two paragraphs of the template below, with the concrete ${SESSION_DIR} path substituted into the heartbeat command) and write the file with the **Write tool**. Do not open codebase files to fill gaps — if a needed fact is absent, the *plan* is at fault: name the gap in one line, proceed with what the doc gives, and let the review catch the rest. This path should cost a few thousand tokens, not tens of thousands.

**FALLBACK PATH — no Work Order (free-text, conversation-inferred, or a pre-`/plan:to_phased_plan` doc).**
Research and compose. If a doc path was given, extract the applicable phase/section. Write `${SESSION_DIR}/implementation_prompt.md` with the **Write tool** (NOT Bash heredoc) using this template:

```
You are implementing a code change. Write the code. Make the changes directly
in the codebase. Do not ask questions — implement the spec below.
Do NOT commit. Do NOT create branches. Do NOT touch files outside this task's scope.
After making all changes, summarize what you did: which files you
created/modified and why, and any deviations from the spec with reasons.

Heartbeat: immediately before each new activity (reading code, editing a file,
running build/lint/tests), run
  bash ~/.claude/scripts/agents/heartbeat.sh <SESSION_DIR>/heartbeat.log agent "<what you are about to do>"
with a short present-tense phrase of real words naming the activity (e.g.
"implementing the parser changes", "running clippy for verification"). One
line per activity change. Never read the heartbeat file — it is for the
orchestrator only.

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
- Include the heartbeat paragraph with the concrete ${SESSION_DIR} path substituted — the delegate has no ${SESSION_DIR} variable

3. In single/loop mode or a verbose bounded-auto window, tell the user in one
   line what is being dispatched and the prompt path:
   `Dispatching <scope summary> to the delegate agent — prompt at ${SESSION_DIR}/implementation_prompt.md`
   In verbose mode with no active auto window, do not announce dispatch yet;
   <VerbosePrePhaseGate/> explains the assembled phase and waits for approval.

   If this was the fast path, add: `(assembled from <plan>'s Phase N Work Order — no research)`.
</ComposeWorkOrder>

---

<VerbosePrePhaseGate>
**Verbose mode only, before every phase when no bounded-auto window is active.**

**Goal:** Explain the phase before any delegate work starts, including the
load-bearing types and APIs the plan expects, then wait for explicit approval.

Build the briefing only from the target phase's Work Order and Delegation
Context. Do not research the codebase on the delegate-ready fast path, invent
types the plan does not name, or describe planned behavior as already working.
Use the assembled implementation prompt to ensure any command-line amendments
are reflected.

```
## Phase N ready — <phase title>

### Why this phase exists
[The point of the phase in 2-4 sentences: what capability or foundation it adds,
what later work depends on it, and what remains deliberately outside it.]

### Work to be done
[A behavior-focused summary of the planned implementation, including the main
state transitions, ownership boundaries, and user-visible effect.]

### Important types and APIs this phase will introduce or change
| Type / trait / API | Planned role | How it will work with the rest of the system |
| --- | --- | --- |
[Only load-bearing types, traits, resources, enums, events, and public methods
explicitly named by the Work Order. Explain expected ownership, inputs/outputs,
lifecycle, and persistence/runtime boundaries where specified. If the Work
Order names none, say "No new load-bearing types or APIs are specified for this
phase" instead of manufacturing entries.]

### Files and verification
[Name the files or modules that will change, the acceptance gate, and meaningful
build/test/lint checks.]
```

Then ask exactly one authorization question and wait:

`Start Phase N? Reply \`proceed\` to run only this phase, \`auto next N phases\`,
\`auto through phase X\`, or \`stop\`.`

- `proceed` or `approved`: keep `AUTO_WINDOW = none`; append any
  trailing text to `${SESSION_DIR}/implementation_prompt.md` as current-phase
  free-text instructions; announce the dispatch and continue to <SelectTask/>.
- `auto next N phases`: require positive N, set `AUTO_WINDOW = next N`, announce
  the inclusive phase range when it can be determined, and dispatch the current
  phase. The current phase counts as one.
- `auto through phase X`: require X to be the current or a later todo phase, set
  `AUTO_WINDOW = through X`, announce the inclusive range, and dispatch the
  current phase.
- `stop`: emit <RunSummary/> with `user stopped before phase N` and end.
- `continue`: this control is reserved for <VerbosePostPhaseGate/> and does not
  authorize implementation; preserve this gate and ask for `proceed`.
- A question or discussion without an explicit authorization control does not
  authorize the phase. Answer it, preserve the gate, and ask again.
</VerbosePrePhaseGate>

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

1. Run `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/implementation_prompt.md" "${IMPLEMENTATION_TASK}" "<responsibility>"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true` — `<responsibility>` is 1-2 lines naming what this run implements (for a phased plan: phase number, title, and the Work Order's goal in a few words)
2. Inform the user: "The delegate agent is implementing... (heartbeat: ${SESSION_DIR}/heartbeat.log)"
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
Run `bash ~/.claude/scripts/delegate/review.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/review_prompt.md" review "<responsibility>"` using Bash with `run_in_background: true` and `dangerouslyDisableSandbox: true` — `<responsibility>` is 1-2 lines naming what is under review (e.g. `Blind review of the phase 3 diff against its Work Order; no implementer summary provided`).

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
menu and continue to <RunApplicationSmokeTest/>.

If even one remaining issue is substantial, or the reviews disagree, the
exception does not apply — everything routes through the normal choice menu
below. This exception is only available after <DualReview/> has run; it never
applies to the initial implementation.

4. **AUTO-ROUTE.** Otherwise (confirmed blocker or minor issues remain), route
without asking:

   **Defer first** — if an issue is really a decision that affects only later
   phases and the current phase's acceptance gate passes without it, apply the
   blocking-vs-deferrable rule (see Multi-phase modes): record it as a
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
   intended behavior) and ${FIX_PASS} < 4: increment ${FIX_PASS}, write
   ${SESSION_DIR}/fix_prompt_${FIX_PASS}.md (same structure as the work order,
   spec = the confirmed issues table with file/line specifics, same no-commit
   rules, heartbeat instruction, and style requirements), select `${FIX_TASK}` — `mechanical` only
   when every confirmed issue is documentation, formatting, lint guidance, a
   trivial rename, or an equivalently behavior-preserving edit; `escalation`
   when review found incorrect behavior, numerical/transform math, unresolved
   architecture, or a prior fix failed; otherwise `implementation` — then run
   `bash ~/.claude/scripts/delegate/implement.sh "${SESSION_DIR}" "${WORKING_DIR}" "${SESSION_DIR}/fix_prompt_${FIX_PASS}.md" "${FIX_TASK}" "<responsibility>"`
   (background, unsandboxed) — `<responsibility>` is 1-2 lines naming the fix
   pass and the confirmed issues it addresses (e.g. `Fix pass 2 — restore the
   error path dropped from the parser; both reviews flagged it`). Tell the
   user in one line what is being fixed.
   Re-execute <DualReview/> and <Synthesize/> scoped to the new changes.

   **STOP** — when any remaining issue needs a design decision the plan does
   not answer, when the two reviews conflict on *intended behavior* (not just
   severity), or when ${FIX_PASS} >= 4 with blockers remaining. Present the
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

   Do not surface internal bookkeeping (`fix pass 1 of 4 used`) as the headline;
   if the cap is relevant, state it without jargon inside option 1. **Wait for
   the user.**

   - **1:** An explicit user choice overrides the cap — increment ${FIX_PASS}
     and dispatch as in the auto fix pass above.
   - **2:** Continue to <RunApplicationSmokeTest/>.
   - **3:** Discuss; afterwards re-offer the options.

If there are no issues (or nits only), state that and continue to
<RunApplicationSmokeTest/>.

**RULE:** The main agent does not write or edit implementation code in this command unless the user explicitly says so. All fixes route to the delegate agent by default. Sole exception: the direct-fix exception above (doc-only or trivial post-review fixes both reviews agree on).
</Synthesize>

---

<RunApplicationSmokeTest>
**Required after every phase, after review fixes and before phase review or a
checkpoint.**

**Goal:** Demonstrate that the repository's runnable product still starts and
that the runtime behavior added or changed by the phase works without a panic,
fatal error, or immediate shutdown.

1. Determine the runnable target and command from, in order: the Delegation
   Context's **Run** or **Smoke** entry, the phase Acceptance gate, repository
   instructions, and the relevant package manifest. This narrow inspection is
   required even on the delegate-ready fast path. Do not treat a successful
   build, test binary, static example build, or delegate claim as an application
   smoke test.
2. Launch the real application or executable directly from ${WORKING_DIR} with
   backtraces and useful runtime logging enabled. Keep the process attached and
   capture its output. For a repository with a primary application, run that
   application after every phase, including library-only phases. If the
   repository genuinely has no runnable product, run the closest executable or
   example that integrates the changed code and record why it is the applicable
   target.
3. Exercise the runtime path added or changed by the phase. Merely reaching the
   first frame is sufficient only when the phase has no changed runtime behavior
   to invoke. For GUI, input, hardware, networking, persistence, or other
   interactive work, perform the relevant action and continue the application
   long enough to observe its result. Automate the action when safe. If the
   environment cannot perform a required real interaction, keep the bounded
   smoke run attached, ask the user to perform that exact action, and wait. Do
   not report a pass with an unexercised runtime path.
4. Close the application cleanly after the exercised behavior remains stable.
   Set ${APPLICATION_SMOKE_RESULT} to a concise record of the command, exercised
   behavior, and observed result.
5. A panic, fatal log, unexpected exit, or incorrect exercised behavior is a
   blocker for the current phase. Capture the backtrace and relevant logs, route
   the confirmed issue through the same automatic delegate fix logic in
   <Synthesize/>, then rerun <DualReview/>, <Synthesize/>, and this smoke test.
   Never run <RunPhaseReview/>, <CheckpointCommit/>, or a verbose completion
   report until the smoke test passes.
6. If no applicable executable can be found or a required interaction cannot be
   performed by either the main agent or the user, STOP with the phase
   incomplete. Environment limits are not a successful application smoke test.
</RunApplicationSmokeTest>

---

<RunPhaseReview>
**Only when the work came from a phased plan doc.** A phase review is mandatory — do not ask, do not offer to skip.

Tell the user in one line: `Phased plan — running /plan:phase_review to update <plan doc> (retrospective + remaining-phase re-evaluation).` Then invoke the `plan:phase_review` skill immediately — **in loop or verbose mode pass `auto`**, so user decisions are deferred into the affected Work Orders as `**Pending decision:**` blocks instead of asked inline; either multi-phase mode stops for them at that phase's pre-dispatch check. When writing the retrospective, include relevant facts from ${AGENT_REVIEW} and the fix passes (e.g. what the blind reviewer caught, what deviated from spec).

If the work was not from a phased plan, skip this step silently and end.
</RunPhaseReview>

---

<CheckpointCommit>
**Loop and verbose modes only** — `single` runs end after <RunPhaseReview/>
without committing.

1. Confirm ${APPLICATION_SMOKE_RESULT} records a passing live application run
   that exercised the current phase's changed runtime path. If it is `not_run`,
   incomplete, or failed, STOP; do not commit.
2. Run `git status --short` in ${WORKING_DIR} and confirm the changes are this
   phase's implementation plus the plan doc. Anything unexpected → STOP and ask.
3. Stage everything and commit with this message shape:

   ```
   checkpoint(<plan-slug>): phase N — <phase title>

   <one line: what the phase built>

   Claude-Session: <session url>
   ```

4. Edit the phase's status line in the plan doc to ``status: done (`<short hash>`)``,
   then `git add <plan doc> && git commit --amend --no-edit`.
5. Report one line: `Checkpoint <short hash> — phase N: <title>.`

Never push. Never commit anything outside this step.
</CheckpointCommit>

---

<VerbosePostPhaseReport>
**Verbose mode only, after every completed phase, including phases inside a
bounded-auto window.**

**Goal:** Output only what the reviewed phase actually delivered. Do not include
the next phase's purpose, planned work, types, files, verification, or any other
pre-phase briefing content.

Build this summary from the phase Work Order, the reviewed diff captured in
<DualReview/>, ${IMPL_SUMMARY}, accepted fixes, the phase retrospective, and the
checkpoint. The diff and review are authoritative when an implementation detail
differs from the original plan. Do not merely repeat the Work Order or the
delegate's claims.

```
## Phase N complete — <phase title>

### Why this phase exists
[The point of the phase in 2-4 sentences: what capability or foundation it adds,
what later work depends on it, and what remains deliberately outside it.]

### What now works
[A concise behavior-focused summary of the reviewed implementation.]

### Important types and APIs
| Type / trait / API | Role | How it works with the rest of the system |
| --- | --- | --- |
[Only load-bearing new or materially changed types, traits, resources, enums,
and public methods. Explain ownership, inputs/outputs, important lifecycle, and
persistence/runtime boundaries where relevant. If none were introduced, say
"No new load-bearing types in this phase" instead of manufacturing entries.]

### Verification and review
[Acceptance gate result, meaningful tests/lint, review outcome, fixes, and the
mandatory live application smoke command, exercised behavior, and result from
${APPLICATION_SMOKE_RESULT}.]

**Checkpoint:** `<short hash>`

```

Do not include the next phase's briefing or ask whether to start it in this
report. <VerbosePostPhaseGate/> separately waits for `continue` before the next
briefing when no bounded-auto window is active.
</VerbosePostPhaseReport>

---

<VerbosePostPhaseGate>
**Verbose mode only, after <VerbosePostPhaseReport/>.**

If no `todo` phase remains, skip this gate and let <NextPhase/> finish with
<RunSummary/>. If a bounded-auto window is active, skip this gate and let
<NextPhase/> continue or end the window according to its existing rules.

Otherwise, show exactly this control line after the completed-phase summary and
wait:

`Reply \`continue\` when you are ready to review the next phase's pre-phase briefing, or \`stop\` to end the run.`

- `continue` — advance to <NextPhase/>. This authorizes only composing and
  displaying the next phase's <VerbosePrePhaseGate/>; it does not authorize a
  delegate dispatch, implementation, review, fix, phase review, or checkpoint.
- `stop` — emit <RunSummary/> with `user stopped after phase N` and end without
  composing the next phase's briefing.
- `proceed`, `approved`, a question, or discussion without `continue` does not
  advance. Answer any discussion using only the completed phase's report and
  preserve this gate.
</VerbosePostPhaseGate>

---

<NextPhase>
**Loop and verbose modes only.**

1. Find the next `todo` phase in the plan. If none remains, run <RunSummary/>
   and end. The final verbose phase already received <VerbosePostPhaseReport/>.
2. Reset ${FIX_PASS} = 0, ${IMPLEMENTATION_TASK} = implementation, and
   ${APPLICATION_SMOKE_RESULT} = not_run.
3. If MODE = loop, announce `Continuing to phase N — <title>.` and loop to
   <ComposeWorkOrder/> (STEP 2).
4. If MODE = verbose and `AUTO_WINDOW = none`, announce
   `Preparing the Phase N briefing — <title>.` and loop to STEP 2.
   <VerbosePrePhaseGate/> waits before dispatch.
5. If MODE = verbose and `AUTO_WINDOW = next N`, decrement N for the phase just
   completed. If N is now zero, clear the window, announce the next phase's
   briefing, and loop to STEP 2. Otherwise announce the next phase and loop to
   STEP 2; STEP 3 skips the gate while the window remains active.
6. If MODE = verbose and `AUTO_WINDOW = through X`, compare the phase just
   completed with X. If it is X, clear the window, announce the next phase's
   briefing, and loop to STEP 2. Otherwise announce the next phase and loop to
   STEP 2; STEP 3 skips the gate while the window remains active.

Every path back to STEP 2 still runs the pending-decision pre-dispatch check.
</NextPhase>

---

<RunSummary>
Emitted whenever a multi-phase run ends — plan exhausted, verbose user stop,
blocking stop, or error.

```
## Run Summary

| Phase | Commit | Fix passes | Notes |
| --- | --- | --- | --- |

**Deferred decisions still open:** [one line each, naming the phase that owns it — or "none"]
**Why the run stopped:** [plan complete / user stopped before phase N / pending
decision on phase N / fix-pass cap on phase N / delegate error]
```

Same translation rules as <Synthesize/>: no reviewer vocabulary, no bare codes —
every line must stand on its own for a reader who has not seen the plan.
</RunSummary>

---

## Rules

- ${WORKING_DIR} is whatever the current project directory is — often a worktree checkout. Never create a worktree or switch branches. The only commits are <CheckpointCommit/> checkpoints in loop or verbose mode — one per completed phase, never a push.
- All delegate-launching scripts run with `dangerouslyDisableSandbox: true` and `run_in_background: true`.
- The **Background wait invariant** is mandatory. No active delegate terminal may outlive the primary-agent turn that launched it.
- `${SESSION_DIR}/heartbeat.log` is for on-demand status only (see **Delegate heartbeat**): a single read when the user asks what is happening, once after compaction, or one staleness check on an overdue delegate — never a wait loop, never a completion signal.
- The delegate reviewer is always a fresh session and always blind to the implementer's summary.
- Delegate launchers record task, family, agent, and effort in the session directory. Never rely on an empty effort silently becoming `xhigh`.
- Select `escalation` from the actual Work Order or review outcome, never keyword matching.
- The main agent orchestrates and reviews; the delegate agent codes. The main agent touches implementation code only on explicit user instruction — except post-review doc-only or trivial fixes that both reviews agree on (see the direct-fix exception in <Synthesize>), which the main agent applies itself and reports.
- Max 4 delegate fix passes per phase before stopping for the user; an explicit user choice of another pass overrides the cap.
- Auto/loop mode stops only for: an unresolved `**Pending decision:**` on the phase being dispatched, a fix that needs a design decision the plan does not answer, reviews conflicting on intended behavior, the fix-pass cap with blockers remaining, or a delegate/environment error. Everything else auto-routes or defers.
- Verbose mode has all of those stops plus a mandatory <VerbosePrePhaseGate/>
  before every phase outside an active bounded-auto window, a
  <VerbosePostPhaseReport/> after every completed phase, and a separate
  <VerbosePostPhaseGate/> that waits for `continue` before showing the next
  briefing. A successful phase and the post-phase `continue` never imply
  authorization for the next implementation.
- Work orders that name the `clippy` skill as the lint gate must instruct the delegate agent to run it with `auto-proceed`; the main agent likewise passes `auto-proceed` when it runs the `clippy` skill inside either multi-phase mode.
- Every phase must pass <RunApplicationSmokeTest/> before phase review,
  checkpoint, or completion reporting. The main agent must run the actual
  product and exercise the phase's changed runtime path; builds, automated
  tests, and an untested startup screen do not satisfy this gate.
