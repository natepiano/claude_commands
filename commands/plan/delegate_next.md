# Delegate — next items

**Usage:** `/plan:delegate_next`

Type this when a phase revealed work the plan does not yet cover and nothing was
written down, or when the next-items gate was skipped at a phase boundary. It
runs inside the current session and already knows the phase, its review
outcomes, and the plan. If no delegate run is active, say so in one line and
stop.

`/plan:delegate` reads this file after shrink, at each phase boundary. It
defines `<ConsiderNextItems/>` in full. Never work from memory of an earlier
read: `Class` obedience and the one-line reporting rule are the parts that drift.

Everything below is the contract.

---

<ConsiderNextItems>
Phased plans only. The main agent performs this assessment; do not launch another
agent. After shrink, read the current `As-built` block, phase diff,
`${SESSION_DIR}/phase_review_outcomes_<phase>.md`, remaining `todo` Work Orders,
`${SESSION_DIR}/next_item_amendments_<phase>.md` when present, and
`${NEXT_ITEMS_PATH}` when it exists. Inspect targeted in-scope consumer or crate
code only when those sources cannot confirm a candidate.

A candidate must be needed for the plan's broader outcome and target an
in-scope consumer named by Delegation Context, the crate's API/implementation,
or a crate example. Exclude a current-phase defect, work already owned by a
remaining Work Order, and optional polish or an idea that is not required.
Current-phase defects return to <Synthesize/>.

Each new candidate is an `add` proposal with title, target, need, completion
condition, source phase, and `Class: apply`. Import each architect proposal with
its exact `Action`, `Current`, `Target`, `Proposed`, `Why`, source phase, and
`Class`, then delete its amendment artifact. Deduplicate all proposals by action,
target, and observable outcome against `${NEXT_ITEMS_PATH}` and
`${NEXT_ITEMS_PENDING}`.

**Obey each proposal's `Class`**, which `<NextItemAmendments/>` in
`~/.claude/commands/plan/phase_review.md` has already assigned: apply it now, or
route it to the gate below. Do not re-derive that split here. Never write a
`gate` proposal to the repository without approval.

Apply every `apply` proposal to `${NEXT_ITEMS_PATH}` now, in one edit, and report
them as a single line naming the count and the file — not as a list, and never as
a question. They never enter `${NEXT_ITEMS_PENDING}`, never appear in the block
below, and never hold an auto window open.

Only `gate` proposals reach the gate. If a bounded auto window continues after
this phase, return immediately without reporting or asking. `next N` continues
when N is greater than 1; `through X` continues until the current phase is X. At
an ordinary phase boundary or the last phase of an auto window, continue
silently when `${NEXT_ITEMS_PENDING}` is absent or empty; otherwise present all
pending candidates once:

```
## Items to consider

1. **<Amend | Remove>: <title>**
   **Target:** <in-scope consumer | crate | crate example>
   **Current:** <exact existing item>
   **Proposed:** <replacement, or removal>
   **Why:** <missing capability or changed evidence>
   **Completion condition:** <observable result; amend only>

Reply `approve <numbers>`, `revise <number>: ...`, or `reject <numbers>`.
```

Every `gate` proposal requires a disposition. Feedback revises it and requires
re-presentation; questions preserve the gate. Apply approved actions only:
replace the exact quoted item for `amend`, and delete the exact quoted item for
`remove`. If a target no longer matches, refresh and re-present it. Preserve
existing content and create this structure only when an `add` lands in a file
that does not exist:

```
# <plan title> — Next

## Items to consider

- [ ] **<title>**
  - Target: <in-scope consumer | crate | crate example>
  - Why needed: <missing capability>
  - Completion condition: <observable result>
  - Revealed by: Phase <id>
```

Remove resolved entries from `${NEXT_ITEMS_PENDING}` and delete it when empty.
A rejected add does not enter the repository; a rejected amendment leaves the
existing item unchanged. In loop/verbose, an approved file change joins the
current phase checkpoint; in `single`, it remains uncommitted. The boundary gate
occurs before the last auto phase's checkpoint, never between auto phases.
</ConsiderNextItems>
