# Run phase review

Read at the point of use from `/unit:delegate`. Defines `<RunPhaseReview/>` and
`<RunPhaseShrink/>` in full.

**Read when:** after smoke, once per phase, before the checkpoint.

<RunPhaseReview>
For phased plans, read `~/.claude/commands/plan/phase_review.md` in full and
apply it with this run's `SESSION_DIR` and `WORKING_DIR`; its arguments are `auto`
in loop/verbose, plus `skip-architect` as below. Make `${NEXT_ITEMS_PATH}`
available.
Its retrospective, review outcomes, and proposed next-item amendments are
temporary session files, never plan sections. It may edit only remaining `todo`
Work Orders; earlier `done` phases remain byte-identical. Later user choices
become Pending decision blocks.

Dispatch its architect review only when any trigger holds. Every trigger below
asks one question: **does something still ahead of the run now read wrong?** It
never asks whether this phase did something notable. A phase that shipped
exactly what its Work Order described, leaving every remaining Work Order and
next item still accurate, needs no architect review however much it built.

- implementation deviated from the Work Order;
- phases or remaining Work Orders were changed;
- a later Pending decision was added;
- the phase changed a semantic state, transition, failure, availability,
  recovery condition, diagnostic, or externally observable lifecycle that a
  remaining Work Order or `${NEXT_ITEMS_PATH}` item now describes wrongly.
  Introducing one that nothing ahead depends on is not a trigger, and neither is
  changing one that every remaining item still describes correctly;
- a changed type/API/registration/path is named by a remaining Work Order or
  `${NEXT_ITEMS_PATH}` **and** the change invalidates what that item says about
  it — a rename, a changed signature or ownership, a moved responsibility, a
  removed affordance. Merely touching a file or type a later item also names is
  not a trigger;
- the ledger returned a convergence advisory; or
- three phases completed since the prior architect review.

The last trigger is the floor, and it is meant to carry most runs: a plan whose
phases land as written reaches the architect every third phase and no more
often. Name the trigger that fired in the one-line report, so a run that
dispatches it every phase is visible as the drift it represents.

Otherwise pass `skip-architect`. When dispatched, focus real-code checking on
affected phases and next items, then give the rest a consistency pass. It uses
`review.sh`, this session, and the next review index. Ad hoc work skips this
section.
</RunPhaseReview>

<RunPhaseShrink>
For a phased plan, read `~/.claude/commands/plan/shrink.md` in full and apply it
with arguments `"${PLAN_DOC}" --phases <current-id> --closeout "${SESSION_DIR}"`
after phase review and before checkpoint. This is
the final plan mutation for the phase. It replaces only the current phase's
`Work Order` with `As-built`; prior `done` phases must remain byte-identical and
remaining `todo` phases must retain the forward edits from phase review.

Require a successful structural check and a current phase containing no Work
Order, Retrospective, or Phase Review heading. Failure blocks checkpoint. Ad hoc
work skips this section.
</RunPhaseShrink>
