# Delegate — next items

**Usage:** `/plan:delegate_next`

Type this to review the add-ons a delegate run has accumulated: it walks
`${NEXT_ITEMS_PENDING}` through <ReviewPendingAddOns/> now. It runs inside the
current session and already knows the plan and its phases. If no delegate run is
active, say so in one line and stop.

`/plan:delegate` reads this file at each phase boundary and at every interactive
point. It defines <ConsiderNextItems/> and <ReviewPendingAddOns/> in full. Never
work from memory of an earlier read: silence in automatic mode and the
one-line reporting rule are the parts that drift.

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
condition, source phase, and `Class: gate`. Import each architect proposal with
its exact `Action`, `Current`, `Target`, `Proposed`, `Why`, source phase, and
`Class`, then delete its amendment artifact. Deduplicate all proposals by action,
target, and observable outcome against `${NEXT_ITEMS_PATH}` and
`${NEXT_ITEMS_PENDING}`.

**Obey each proposal's `Class`**, assigned by <NextItemAmendments/> in
`~/.claude/commands/plan/phase_review.md`; do not re-derive it. `apply` is an
as-built correction to an item already in `${NEXT_ITEMS_PATH}`: write every one
now, in one edit, and report them as a single line naming the count and the
file — never as a list or a question. `gate` is an add-on: append it to
`${NEXT_ITEMS_PENDING}` and continue. Nothing here asks the user, stops the
loop, or holds an auto window open, and no add-on reaches the repository
without a disposition from <ReviewPendingAddOns/>.
</ConsiderNextItems>

<ReviewPendingAddOns>
Run only when `${NEXT_ITEMS_PENDING}` is non-empty **and** the run is at an
interactive point: it is already stopped waiting on the user for a project
decision, a verbose gate outside an auto window is about to ask, the user has
just steered the run with an instruction that is more than an authorization
word, the run is ending through <RunSummary/>, or the user typed
`/plan:delegate_next`. Automatic mode never stops for this; a run the user has
walked away from accumulates and stays silent.

At a stop for a decision, resolve that decision first. Then say
`<n> add-ons accumulated since Phase <id>.` and invoke `/adhoc_review` over the
pending items with these settings: no working-doc question, since each
disposition is recorded by where the item lands; skip the shared-model step;
choice line `current (recommended — <reason>) / next / drop / elaborate`, with
the recommendation on whichever fits the item. At <RunSummary/> the plan is
closed, so the line is `next / drop / elaborate` and an item the user wants
built is a new run. A gated `remove` of an existing item offers
`remove / keep / elaborate`.

- `current`: place it in the remaining `todo` phase whose Work Order already
  owns the files or consumer it touches; otherwise the phase just before the
  first one that depends on its result; otherwise a new last phase. Edit that
  Work Order per <MaintainWorkOrders/> in `phase_review.md`, rerun its
  validation, and state the placement in one line. A phase the user names
  overrides this.
- `next`: write it to `${NEXT_ITEMS_PATH}`, creating the file with the structure
  below when absent.
- `drop` or `keep`: discard the proposal. A dropped add never enters the
  repository.

```
# <plan title> — Next

## Items to consider

- [ ] **<title>**
  - Target: <in-scope consumer | crate | crate example>
  - Why needed: <missing capability>
  - Completion condition: <observable result>
  - Revealed by: Phase <id>
```

Remove each resolved entry from `${NEXT_ITEMS_PENDING}` as it lands and delete
the file when empty. Mid-run, plan and next-file edits join the next phase
checkpoint; after <RunSummary/> in loop or verbose, commit a changed
`${NEXT_ITEMS_PATH}` alone as `docs(<plan-slug>): next items` with the session
trailer; in `single`, edits remain uncommitted.
</ReviewPendingAddOns>
