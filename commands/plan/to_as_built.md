---
description: Turn a fully-implemented phased plan into an as-built doc — an overview of the shipped feature for future implementers. Strips Work Order / phase scaffolding, keeps the load-bearing context (architecture, types, invariants, gotchas, rationale), and relocates it into the repo's as-built directory. Plans stamped `As-built disposition: amend` create no new doc — the shipped changes are folded into the existing as-built docs and the plan is deleted.
---

# Plan → As-Built

**Purpose:** Once every phase of a delegate-ready plan is implemented, convert the
plan into an **as-built doc**: a standalone overview of the feature as it actually
exists, written for the next implementer who has to touch this code — not a
process record. Then move it into the repo's as-built directory.

Plans stamped `As-built disposition: amend` (set by the originating review, e.g.
`/api_review`) invert the emphasis: the shipped work refactored surface that
existing as-built docs already describe, so the end state is those docs updated
in place — no new doc, no fragment describing "the cleanup" as if it were a
feature.

**Usage:** `/plan:to_as_built [plan-doc-path]`

**Argument:** the implemented plan doc. If omitted, infer the single plan doc in
conversation; if none, ask. Do not guess.

This command does not change code and does not commit.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** Execute <Verify/>
**STEP 2:** Execute <Distill/>
**STEP 3:** Execute <ProposeDestination/>
**STEP 4:** Execute <Relocate/>
**STEP 5:** Execute <ReconcileAsBuilt/>
**STEP 6:** Execute <Report/>
</ExecutionSteps>

---

<Verify>
Resolve the plan path. Read it. Confirm **every phase is `done`**.

If any phase is still `todo`, stop and tell the user which phases remain — an
as-built doc describes shipped code, so it is premature. Do not proceed.

**Read the disposition.** An `As-built disposition:` line under the Status line
sets ${DISPOSITION}: `amend` (fold changes into the existing as-built docs it
names — no new doc) or `create`. Line absent → `create` (the default flow).
State the mode in one line. Every later step branches on it.
</Verify>

---

<Distill>
**Amend mode:** no new doc is distilled. Instead assemble the **change surface**
from the plan you already read — per phase: the types, signatures, renames,
deletions, invariants, and behavior the Work Orders shipped, corrected by their
Retrospectives. Capture it as ${CHANGE_SURFACE} and skip to `<ProposeDestination/>`.

**Create mode** (the rest of this step):

**Goal:** an as-built overview, drafted without burning orchestrator tokens on a
codebase re-read.

Dispatch ONE `Explore` (or `general-purpose`) subagent. Its prompt must include:

- The absolute plan-doc path.
- A directive to read the plan **and** the key shipped files it names (the
  Delegation Context **Key files** and each Work Order's **Files**), so the
  overview matches what actually exists, not only what was planned.
- The instruction to return an **as-built overview** that **keeps**:
  - **What it is** — one-paragraph summary of the feature and the problem it solves.
  - **How it works** — the architecture and data flow as built; the concrete
    types, signatures, and the key files/modules, with their roles.
  - **Invariants** — the rules future changes must preserve (carried from the
    plan's invariants + anything the retrospectives surfaced).
  - **Calibration / gotchas** — magic numbers, budgets, edge cases, and the
    surprises recorded in the phase retrospectives that a future implementer needs.
  - **Why** — the rationale behind load-bearing decisions, reconstructed as
    "why it is this way" (the useful residue of the resolved-decision record),
    not a debate transcript.
- The instruction to **drop**: Work Order scaffolding (Goal/Spec/Files/Acceptance
  gate phrasing), phase sequencing, commit refs, Delegation Context
  build/test/style boilerplate, and process retrospective framing. The reader is
  someone modifying the feature later, not someone re-running the project.
- Output: the finished as-built markdown (title + the sections above). It reads as
  an overview of a feature, with no trace that it was once a phased plan.

Capture the draft. Review it yourself for accuracy against the plan; tighten if
needed. Do not pad — keep it to load-bearing content.
</Distill>

---

<ProposeDestination>
**Amend mode:** the destination is the existing docs. Resolve the target list:
the as-built docs the disposition line names; if it names none, the sibling
`as-built/` docs whose subjects ${CHANGE_SURFACE} touches. Then ask the user,
phrased to stand alone:

1. One plain-language line: "Fold the finished `<plan>` plan into the existing
   reference docs it changed, then delete the plan."
2. The operations, in human terms — Edit: each target doc (updated to match the
   shipped code). Delete: `<plan path>` (its content now lives in those docs).
3. A clear choice: confirm / keep the plan doc / adjust the target list.

On confirm, skip `<Relocate/>` and go to `<ReconcileAsBuilt/>`; the plan doc is
deleted there, after the edits are applied.

**Create mode** (the rest of this step):

Determine the repo flavor and propose the as-built directory:

- **Flavor A — workspace** (root `Cargo.toml` has `[workspace]`, members under
  `crates/*`, and docs are organized per-project as `docs/<project>/…`): the
  plan lives at `docs/<project>/<plan>.md`, so the destination is
  `docs/<project>/as-built/`.
- **Flavor B — single package** (one crate, flat `docs/`): the destination is
  `docs/as-built/`.

In practice the as-built dir is `as-built/` as a sibling of the plan doc's
location (matching how delegate-ready plans already link `as-built/…`). Examine
the repo to confirm which flavor applies.

**Then ask the user to confirm — and write the question so it stands on its own.**
The user may not remember what `/plan:to_as_built` does and has no view into your
STEP 2 work. Phrase the confirmation like this:

1. **One plain-language line of context first:** what this command is about to do
   — "Convert the finished `<plan>` plan into a clean reference doc for whoever
   edits this code next, then file it under `as-built/`." No domain jargon.
2. **The two file operations, stated plainly** with the human consequence of each,
   not a code-level description:
   - Create: `<destination>/<filename>.md` (the new reference doc).
   - Delete: `<old plan path>` (the original plan, now replaced by the doc above).
3. **A clear choice:** confirm both, keep the old plan, or use a different folder.

**Do NOT** put your STEP 2 fact-check details into this question — symbol names,
type signatures, magic numbers, what you corrected against the shipped tree. That
work is internal; surfacing it here buries the actual question and confuses the
reader. If the distillation needed corrections worth mentioning, save them for the
final STEP 6 report, not the confirmation prompt.

Moving and deleting files is the user's call — do not relocate before they confirm.
</ProposeDestination>

---

<Relocate>
**Amend mode:** skipped — nothing is created; the plan doc is deleted at the end
of `<ReconcileAsBuilt/>`.

**Create mode** — after the user confirms the destination:

1. Create the as-built directory if it does not exist.
2. Write the distilled as-built doc at `<destination>/<filename>.md`.
3. Remove the original plan doc from its docs location (it has been superseded by
   the as-built). If the plan named a predecessor as-built to delete (e.g. an
   `as-built/<old>.md` the feature replaced), remove it too — but only what the
   plan explicitly marks for deletion.
4. Fix any in-repo links that pointed at the old plan path, if you can find them
   cheaply; otherwise note them in the report for the user to update.

Do not commit.
</Relocate>

---

<ReconcileAsBuilt>
**Amend mode: this step is the primary act.** The subagent's directive changes
from "check siblings for contradictions" to "fold ${CHANGE_SURFACE} into the
target docs": for each target doc from `<ProposeDestination/>`, rewrite the
stale types/signatures/invariants/behavior to the shipped state and integrate
what the plan added — as current design, not as a changelog. A change with no
home doc gets a new section appended to the closest existing as-built; only if
genuinely nothing fits, return it in a `needs_new_doc` list (suggested path +
reason) for the orchestrator to confirm — never create a file unilaterally.
The subagent prompt gets the target-doc list and ${CHANGE_SURFACE} in place of
the "new as-built doc" input (there is none). The sibling/peer contradiction
scan below still runs after the targeted folds. When the subagent returns and
the edits spot-check clean against ${CHANGE_SURFACE}, delete the plan doc (the
user already confirmed in `<ProposeDestination/>`) and fix any in-repo links
that pointed at it.

**Both modes:**

The new feature may have invalidated docs in the same docs directory as the
source plan (and/or in the as-built directory). If this feature changed a type,
signature, invariant, behavior, or ownership model, nearby docs may now be stale.
Reconcile them so the as-built corpus and surrounding docs stay accurate.

Dispatch ONE `general-purpose` subagent (it must be able to edit). Its prompt
must include:

- The absolute path of the **source plan** doc.
- The absolute path of the **new** as-built doc just written.
- The plan's **change surface**: the types, signatures, invariants, and behavior
  this feature added or altered (carry these from the plan / the distilled
  overview — do not make the subagent re-derive them from scratch).
- The source plan directory path and as-built directory path. Directive:
  - read each **sibling** as-built doc in the as-built directory and decide
    whether this feature contradicts anything it states;
  - read each **peer markdown** doc in the source plan directory (except the
    source plan doc itself) and do the same check for contradicting or stale
    statements.
- For each affected doc, **apply the correction in place** — edit the stale
  types/signatures/invariants/behavior so the doc matches what now exists. Fix,
  do not flag. This is a **correct-everything pass**: an `Archived`/`Superseded`
  banner does **not** exempt a doc.
- **As-built means as-built: describe only the current design.** An as-built doc
  is not a history. Do **not** preserve retrospectives, phase-by-phase records,
  "today's model (what gets replaced)" sections, dated decision logs, or
  narration of an old design that a later design superseded. When you hit that
  material, delete it or rewrite it to the current state — do not keep it as
  "history." The **only** reason to retain something about an old approach is if
  it would help a developer working in this code today (e.g. a still-live
  migration shim they must not remove, or a gotcha that still bites); that is
  rare. When in doubt, cut it. If stripping the historical narrative leaves a
  doc that is wholly about a replaced/removed design, treat it as `obsolete`
  (return path + reason for the orchestrator to confirm deletion) rather than
  keeping a hollow record.
- If a doc is rendered **wholly obsolete** (its feature was replaced/removed),
  do **not** delete it. Return its path plus a one-line reason in an
  `obsolete` list for the orchestrator to confirm.
- Output: a structured result — `edited` (paths + one-line summary of each fix),
  `peer_docs_scanned` (paths checked in the source plan directory), and
  `obsolete` (paths + reason).

After the subagent returns:
- The content edits are already applied; spot-check them against the change
  surface for accuracy.
- For each `obsolete` doc, **state the path and reason and ask the user to
  confirm deletion** before removing it. Deleting a file is the user's call.
</ReconcileAsBuilt>

---

<Report>
Produce a succinct markdown table:

```markdown
| Area | Result |
| --- | --- |
| Mode | <create / amend> |
| As-built | <create: new path / amend: target docs updated, one line each> |
| Removed | <old plan path; any predecessor + confirmed-obsolete as-built deleted, or None> |
| Reconciled | <sibling as-built and peer source-doc edits made, one line each, or None> |
| Flavor | <A workspace / B package; create mode only> |
| Links to fix | <paths needing a manual link update, or None> |
```

Then stop.
</Report>

---

## Rules

- Verify all phases `done` before distilling — never write an as-built for an
  unfinished plan.
- Offload the code re-read to the subagent (`<Distill/>`); the orchestrator must
  not re-explore the repo itself.
- The as-built is for a future implementer: keep architecture, types, invariants,
  gotchas, and rationale; drop phase/Work Order/process scaffolding.
- Get explicit user confirmation before relocating or deleting any file. Write
  that confirmation so it stands alone — one plain line of context + the two file
  operations in human terms. Never dump distillation fact-check internals (symbols,
  signatures, magic numbers) into the question.
- Reconcile sibling as-built docs and peer docs in the source-doc directory after
  writing the new one: apply content fixes in place (fix, do not flag), but
  confirm with the user before deleting an obsolete doc.
- `amend` disposition: no new doc and no distillation — fold the plan's change
  surface into the existing as-built docs, then delete the plan (user-confirmed).
  `needs_new_doc` items require explicit user confirmation before any file is
  created.
- Do not change code and do not commit.
