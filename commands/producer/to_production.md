---
description: Split greenlit phased plans into one delegate-ready plan per unit plus a production doc, so /showrunner:produce can launch the unit directors.
---

# To Production

**Purpose:** turn plans that `/producer:greenlight` judged a production into:
- one delegate-ready plan per unit;
- the production doc that `/showrunner:produce` runs from.

The words and both formats are defined in:
- `~/.claude/docs/production_format.md`;
- `~/.claude/docs/delegate_plan_format.md`.

Read both first.

**Usage:** `/producer:to_production <plan> [<plan>…] [--name <production>] [--merge-branch <branch>]`

This command writes plan docs only. It writes no code and makes no commits,
branches or worktrees; `/showrunner:produce` creates those at launch.

---

<ExecutionSteps>
**EXECUTE IN ORDER:**

**STEP 1:** <Locate/>
**STEP 2:** <Units/>
**STEP 3:** <WriteUnitPlans/>
**STEP 4:** <WriteProductionDoc/>
**STEP 5:** <Validate/>
**STEP 6:** <Report/>
</ExecutionSteps>

---

<Locate>
Resolve the plans as `/producer:greenlight` → <Locate/> does, and `${REPO}` from
their repository.

- **Name:** `--name`, or else the first source plan's file stem (for example
  `tool-based-ui`).
- **Merge branch:** `--merge-branch`, or else the current branch of `${REPO}`
  when it is not the default branch, or else `production/<name>`, which
  `/showrunner:produce` creates.

State each default in one line.
</Locate>

---

<Units>
Reuse the `/producer:greenlight` report for these plans if it is in the conversation
and no plan changed since. Otherwise read `~/.claude/commands/producer/greenlight.md`
and run its STEPS 1–5. A verdict of one `/unit:direct` run stops this command:
`Greenlight says one /unit:direct run — <reason>. Run /unit:direct <plan>.`

Apply the user's adjustments from the conversation, such as names, ownership
and merged units.
</Units>

---

<WriteUnitPlans>
1. **The source plan stays with one unit.** The unit that takes the most `todo`
   phases of a source plan keeps that file, so its `done` history and checkpoint
   numbers stay intact. Delete the `todo` phases that moved to other units.
   Resequence the rest per `/plan:to_phased_plan` → <PhaseNumbering/>. `done`
   phases never renumber.
2. **Every other unit gets a new plan** beside the source, named
   `<source-stem>-<area>.md`, for example `tool-based-ui-widget.md`. It uses the
   delegate plan format:
   - title `# <source title> — <unit>`, the delegate-ready status line, and the
     source's `As-built disposition` line when it has one;
   - the **Production line** from the production format;
   - **Delegation Context** copied from the source. Narrow **Layout**, **Key
     files**, **Test lanes** and the **Build / Test / Lint** lines to this unit's
     files and packages. Keep **Invariants** whole. Leave out **Project
     started**; the recorder sets it on the first run;
   - its phases in source order, renumbered from 1, each Work Order kept byte
     for byte except for the edits in step 3.
3. **Links between units.** In every unit plan, including the kept source:
   - a fact in **Constraints from prior phases** that comes from a phase now in
     another unit names that unit and phase, for example "from
     geometry-material-unit phase 2, on `<merge branch>` before this phase
     starts";
   - a phase that waits on another unit gets its gate line directly under its
     heading: `**Blocked by:** G<k> — <unit> phase <M> merged into
     \`<merge branch>\``;
   - every phase number mentioned in moved text is rewritten to its new number,
     or to the other unit's name and number. Substitute highest-first.
4. **Header line.** Add the Production line to the kept source plan too.
</WriteUnitPlans>

---

<WriteProductionDoc>
Write `<source-stem>-production.md` beside the first source plan, in the
production format, with status `planned`:

- **Units:**
  - **Worktree:** `<parent of ${REPO}>/<name>-<area>`, with a branch of the
    same name.
  - **Session:** the unit name. If a tmux session with that name already
    exists, add the production name as a prefix.
  - **Port:** a distinct app port per unit, only when the project launches an
    app for smoke tests. Never the user's default port. The repository's
    memory names the default.
  - **Owns:** the files greenlight gave the unit.
- **Hub files** and **Gates** are copied from the greenlight result.
- **Merge tests:** the app package whose tests catch cross-crate breaks, if
  one exists.
- **Log:** `docs/handoff/<name>-production-log.md`.
- **User zone:** the zone in memory for where the user is now, or else the
  machine's zone.
- **Updates:** every 15 minutes (user, 2026-09-28).
- **Close-out:** the source plans' production-level steps, meaning work that
  no one unit owns or that runs after the last merge. Examples: data or
  saved-state migrations, checks on other machines, final validation.
- **Production rules:** standing rules the user set for this project, from
  memory and CLAUDE.md, each with its source. Only rules the user set; a rule I
  would add myself goes in a unit's Work Order instead.
</WriteProductionDoc>

---

<Validate>
Validate every `todo` phase in every unit plan:

```sh
PYTHONPATH="$HOME/.claude/scripts" python3 -m berth.work_order \
  --repository-root "${REPO}" validate --document "<unit plan>" --phase <N>
```

Then check by hand:
- no two units own the same file;
- each hub file has one owner;
- every gate in the production doc appears as a `**Blocked by:**` line in its
  waiting phase, and the gates form no cycle.

Fix any failure before reporting.
</Validate>

---

<Report>
```markdown
| Area | Result |
| --- | --- |
| Production doc | <path> |
| Units | <unit>: <plan path>, <n> phases (source phases <list>); one row per unit |
| Merge branch | `<branch>` (<exists | produce creates it from <base>>) |
| Hub files | <file> → <owner>; or None |
| Gates | G<k>: <unit> phase <N> waits on <unit> phase <M>; or None |
| Source plan | <kept by <unit>; <n> phases moved out; resequenced <old→new>> |
| Next | In a checkout of `${REPO}` on `<merge branch>`: `/showrunner:produce <production doc>` |
```

Then stop.
</Report>

---

## Rules

- Plan docs only; no code, commits, branches or worktrees.
- Never rewrite a `done` phase and never renumber one.
- A Work Order moves whole. The only edits are cross-unit references and gate
  lines. Its Spec stays verbatim (delegate plan format rule 3).
- Splitting is packaging under `<DecisionEconomy/>`: decide it and state it.
  The user decides anything that changes what ships.
