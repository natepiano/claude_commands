# Run phase review

Read at the point of use from `/unit:direct`. Defines `<RunPhaseReview/>` and
`<RunPhaseShrink/>` in full.
Phase-end order: `~/.claude/docs/direct/phase_end.md` → <PhaseEnd/>.

**Read when:** at the phase review or shrink step; in `single`, after smoke and
`<UXReview/>`.

<RunPhaseReview>
For phased plans, read `~/.claude/commands/plan/phase_review.md` in full and
apply it with this run's `SESSION_DIR` and `WORKING_DIR`; its arguments are `auto`
in loop/verbose, plus `skip-architect` as below. Make `${NEXT_ITEMS_PATH}`
available. Run the retrospective, state audit, architect review when triggered,
and forward edits.
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

Otherwise pass `skip-architect`.
Check affected phases and next items against the code, then check the remaining text for consistency.
Choose a retained worker that did not write the affected files when one is available.
Otherwise have their writer review its choices without relying on its earlier reasoning.
Apply <RoleReassignment/> and <ReviewPromptContract/>.
Use `assign.sh --role review` with this session and the next review index.
For `herdr`, use `worker.py list` and pass the short name with `--to` for a retained worker.
If all pane workers are busy, pass `--new-worker <free review name>` to `assign.sh --role review`.
For a phase review outside a unit run, omit both worker options.
Otherwise omit `--to` only for a new worker allowed by <LongLivedWorkers/>.
The prompt keeps the inline Three Gods sentence and Type Design Contract or its permitted pointer.
Ad hoc work skips this section.
</RunPhaseReview>

<RunPhaseShrink>
Standing plans (`**Standing:** style_plan.py`) skip shrink and its structural
check: refill discards merged or dropped ticket phases. Never commit the plan
or pinned rule files.

For a phased plan, read `~/.claude/commands/plan/shrink.md` in full and apply it
with arguments `"${PLAN_DOC}" --phases <current-id> --closeout "${SESSION_DIR}"`
at the shrink step. Shrink replaces only the completed phase's `Work Order`
with `As-built`; prior `done` phases must remain byte-identical. Remaining
`todo` phases must retain the forward edits from phase review and
<ConsiderNextItems/>.

Require a successful structural check and a current phase containing no Work
Order, Retrospective, or Phase Review heading. Ad hoc work skips this section.
</RunPhaseShrink>
