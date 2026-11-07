---
description: Assess plans for parallel production.
---

# Greenlight

**Purpose:** decide whether phased plans run faster as a **production** than as
one `/unit:direct` run. The words and the production doc are defined in
`~/.claude/docs/production_format.md`; read it first.

**Usage:** `/producer:greenlight <plan> [<plan>…]`

**Argument:** one or more direct-ready plans. If omitted, infer the single plan
in the conversation; if there is none, ask for the path. A second plan joins as
its own unit unless its phases split further.

This command reads only. It writes no file and creates no branch.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** <Locate/>
**STEP 2:** <ReadPhases/>
**STEP 3:** <FormUnits/>
**STEP 4:** <Capacity/>
**STEP 5:** <Verdict/>
**STEP 6:** <Report/>
</ExecutionSteps>

---

<Locate>
Each plan must be direct-ready: it has a `## Delegation Context` section and
the `Status: IMPLEMENTATION PLAN — phased, direct-ready` line. A plan that is
not stops the command with `Run /plan:to_phased_plan <path> first.`

Resolve `${REPO}` from each plan's Git repository. Plans from different
repositories cannot share a merge branch; say so and stop.
</Locate>

---

<ReadPhases>
Only `todo` phases are split. `done` phases stay in their plan as history.

For each `todo` phase, read its **Files** through the shared parser, never by
reading the Markdown yourself:

```sh
set -o pipefail
~/.claude/scripts/lib/py -m scripts.berth.work_order \
  --repository-root "${REPO}" resolve --document "<plan>" --phase <N> \
  | jq -r '.work_order.files[].path'
```

Also read each phase's **Goal**, **Constraints from prior phases**, and its dependencies.
</ReadPhases>

---

<FormUnits>
1. **Order between phases.** Phase B comes after phase A when any of these hold:
   - B's **Constraints from prior phases** names something A builds: a type, a
     file, a behavior;
   - A and B list the same file, unless it is a hub file (step 2).
2. **Hub files.** A hub file is one that phases in otherwise independent groups
   all list: `lib.rs` or `mod.rs` re-exports, `Cargo.toml`, `Cargo.lock`,
   plugin registration, a shared types file, a shared example. A hub file does
   not tie groups together. Give it to the unit that changes it most, and let
   the others go through cargo-berth.
3. **Chains.** Drop the hub-file links. Each connected group of phases is a
   candidate unit. A group of one small phase joins the unit whose files it
   touches most.
4. **Order.** Record start-order waits as owed actions in the showrunner's notes.
   Use `cargo-berth sequence` for merge order.
5. **Names.** Name each unit `<area>` after the crate or module it owns:
   short, lowercase, hyphenated. A unit that keeps the source plan's main line
   may be `trunk`.
</FormUnits>

---

<Capacity>
Each unit builds and tests in its own worktree and target directory, so the
machine carries every unit's build at once. Read `nproc` and `free -g`. Check
memory for load-sensitive flakes in this repository; they are the known cost of
parallel builds. With no measured production on this machine, say the ceiling
is unmeasured. Never state a speedup that no measured run supports.
</Capacity>

---

<Verdict>
**Production** when all of these hold:

- there are two or more units;
- every unit has at least one phase that can run while another unit's phase
  runs;
- each hub file has exactly one owner, and the units' owned files do not
  overlap;
- units that change what users see can each launch the app on their own port.

Otherwise it is **one `/unit:direct` run**. Give the reason in one line, for
example "every phase builds on the one before it".
</Verdict>

---

<Report>
```markdown
| Area | Result |
| --- | --- |
| Verdict | Production of <n> units / One `/unit:direct` run — <one-line reason> |
| Units | <unit>: phases <source numbers> — owns <dirs/files>; one row per unit |
| Hub files | <file> → <owner unit>; or None |
| Capacity | <cores, memory; known load-sensitive flakes; ceiling measured or unmeasured> |
| Next | `/producer:to_production <plan>…` or `/unit:direct <plan>` |
```

Then stop.
</Report>

---

## Rules

- Read only. No file edits, commits, branches or worktrees.
- Work from each Work Order's **Files** and **Constraints**, never from searching
  the code. A phase whose Files are too vague to place is a plan defect: name it
  and point to `/plan:to_phased_plan`.
- Proposals are packaging, not product choices. Make each call under
  `<DecisionEconomy/>` and state it in one line. The user adjusts at
  `/producer:to_production`.
