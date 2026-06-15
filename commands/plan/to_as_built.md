---
description: Turn a fully-implemented phased plan into an as-built doc — an overview of the shipped feature for future implementers. Strips Work Order / phase scaffolding, keeps the load-bearing context (architecture, types, invariants, gotchas, rationale), and relocates it into the repo's as-built directory.
---

# Plan → As-Built

**Purpose:** Once every phase of a delegate-ready plan is implemented, convert the
plan into an **as-built doc**: a standalone overview of the feature as it actually
exists, written for the next implementer who has to touch this code — not a
process record. Then move it into the repo's as-built directory.

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
</Verify>

---

<Distill>
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
Determine the repo flavor and propose the as-built directory:

- **Flavor A — workspace** (root `Cargo.toml` has `[workspace]`, members under
  `crates/*`, and docs are organized per-project as `docs/<project>/…`): the
  plan lives at `docs/<project>/<plan>.md`, so the destination is
  `docs/<project>/as-built/`.
- **Flavor B — single package** (one crate, flat `docs/`): the destination is
  `docs/as-built/`.

In practice the as-built dir is `as-built/` as a sibling of the plan doc's
location (matching how delegate-ready plans already link `as-built/…`). Examine
the repo to confirm which flavor applies, then **state the proposed destination
path and the as-built filename in one line and ask the user to confirm or
redirect.** Moving and deleting files is the user's call — do not relocate before
they confirm.
</ProposeDestination>

---

<Relocate>
After the user confirms the destination:

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
The new feature may have invalidated **other** as-built docs in the same
directory — changed a type they describe, broken an invariant they assert, or
made one wholly obsolete. Reconcile them so the as-built corpus stays accurate.

Dispatch ONE `general-purpose` subagent (it must be able to edit). Its prompt
must include:

- The absolute path of the **new** as-built doc just written.
- The plan's **change surface**: the types, signatures, invariants, and behavior
  this feature added or altered (carry these from the plan / the distilled
  overview — do not make the subagent re-derive them from scratch).
- The as-built directory path. Directive: read each **sibling** as-built doc and
  decide whether this feature contradicts anything it states.
- For each affected doc, **apply the correction in place** — edit the stale
  types/signatures/invariants/behavior so the doc matches what now exists. Fix,
  do not flag.
- If a doc is rendered **wholly obsolete** (its feature was replaced/removed),
  do **not** delete it. Return its path plus a one-line reason in a
  `obsolete` list for the orchestrator to confirm.
- Output: a structured result — `edited` (paths + one-line summary of each fix)
  and `obsolete` (paths + reason).

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
| As-built | <new path> |
| Removed | <old plan path; any predecessor + confirmed-obsolete as-built deleted, or None> |
| Reconciled | <sibling as-built docs edited, one line each, or None> |
| Flavor | <A workspace / B package> |
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
- Get explicit user confirmation before relocating or deleting any file.
- Reconcile sibling as-built docs after writing the new one: apply content fixes
  in place (fix, do not flag), but confirm with the user before deleting an
  obsolete doc.
- Do not change code and do not commit.
