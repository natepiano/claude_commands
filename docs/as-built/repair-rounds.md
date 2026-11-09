# Repair rounds

## What it is

The repair tools measure work on each phase and preserve findings until the receiving work closes them.
The director uses these records to group repairs, protect accepted behavior, and route work outside the phase.
Publication uses the tested Git tree and records the producing unit for each visible change.

The [evidence study](../plans/sdlc-fix-rounds-study.md) contains the measured cohorts, root causes, proposals, and effect evaluation.
Its fixed samples do not describe the current workload.
Process changes require the user's approval through the showrunner.

## Round measurement

`scripts/history/rounds.py` exposes these pure functions:

```python
phase_rounds(events: list[dict[str, object]]) -> list[Round]
load_phases(events: list[dict[str, object]]) -> list[PhaseRounds]
```

`Round` and `PhaseRounds` are `TypedDict` records.
`Round` carries boundaries, cause, ledger numbers, worker time, passes, activities, and covered findings.
`PhaseRounds` carries the run and phase instance, plan, status, boundaries, rounds, findings, review starts, and record quality.

`load_phases` joins events by run and `phase_instance_id`.
It joins a ledger event without `run_id` only when the instance identifies one run.
It keeps recorded phase boundaries separate from observed pass boundaries.

The round builder applies these rules:

1. Join each pass start and finish by `pass_instance_id`, with a legacy key when that id is absent.
2. Group fix passes with `launch_check.fix_cause.round` by that ledger number, including later launches of the same batch.
3. Group legacy fix passes without a ledger number by overlapping intervals.
4. Match each dispatch to the first eligible group with the same ledger number or legacy `fix_pass` number.
5. Exclude an explicitly refunded batch unless a later landing restores it.

A dispatch without an eligible fix pass creates no round.
An unfinished pass contributes no elapsed worker time and cannot extend a legacy overlap interval.
Parallel workers each contribute their elapsed time, so worker time can exceed wall time.
Repeated legacy fix numbers require the dispatch-time comparison; a number alone cannot identify the batch.

Causes use this priority:

| Cause | Evidence |
| --- | --- |
| `review-ux` | A matched ledger batch covers a finding whose lens includes `ux`. |
| `review-code` | Another matched ledger batch. |
| `gate` | The repair activity matches `GATE_WORDS`. |
| `merge` | The repair activity matches `MERGE_WORDS`. |
| `live` | A matching live, shot, smoke, or UX review activity finished within 30 minutes before the repair. |
| `other` | No earlier rule matches. |

A record is `mixed` when a pass names another numeric phase, its passes span over 72 hours, or merge rounds span over 24 hours.
The report marks mixed records instead of dropping them.
Consumers must choose whether mixed records belong in their distributions.

`history.py rounds` sorts by round count, then fix time.
It supports time, status, plan, and result-count filters, with table, CSV, and JSON output.
The default result count is 25.
A phase is reviewed when its instance has a recorded review pass start.
The median and heavy-phase shares use reviewed phases only.
Heavy phases have at least three rounds.
Cause totals and total fix time still include every selected phase.
JSON retains full round details and the summary denominators in `_meta`.
The history commands do not change run records.

## Repair control and routed work

`scripts/direct/findings.py` owns the phase ledger and its batch state.
Its `round` identifies a dispatch record.
Its `repair_round` identifies the next repair round after distinct started ledger batches.
Unbatched gate, merge, or live repairs remain visible in history but do not advance this ledger counter.
A launch refusal spends no repair round.
An abandonment without landed edits refunds the attempt.

The gate returns `converged` or `dispatch`.
The first batch gates blockers and minors; later batches gate blockers only.
Nits do not gate.
The gate reads findings, so a failed command needs a finding before it affects convergence.
`dispatch --covers` must include every gating open finding that remains in the phase.

`config/direct.conf` supplies the required thresholds.
`ROUTING_FROM_ROUND=4` requires evidence for retained findings.
The kept categories are `regression`, `broken`, `gate`, and an explicit `ruling`.
Other missing criteria or work outside those categories go to an owned follow-up.
`ASTRA_AFTER_FAILED_FIXES=2` requires a new Astra approach after two failed fixes.
A gating open finding also needs that review from repair round four.
The Astra record applies to its current fix-attempt count.
An unavailable review can be recorded with its reason.
The other convergence limits produce advisories and do not stop the phase.

Every dispatch records `--accepted-checks`, or `none: <reason>` when the repair reaches no accepted behavior.
The repair prompt includes the new check and the affected accepted checks.
The director verifies both on the same tree.
Command-only verification uses the existing detached workflow without another repair worker.
Edits, fixture decisions, and merge choices remain worker work.
The authoritative instructions are [Repair rounds](../direct/repair_rounds.md) and [ProductionUnit](../production_format.md).

`route` stores the finding's owner, Work Order, due checkpoint, evidence, and latest Astra approach.
A `routed` finding remains unresolved but leaves the gating counts and batch.
`history.py routed --open` shows its destination, age, and closure state.
The receiving checkpoint records `close-route --from <run id>:<phase instance id>:<finding id> --evidence <evidence>`.
The reader joins closure events across runs by that full key.
An older route can have an empty phase instance in the key.

`trial_exposure` records P1 and P2 declarations and P3 gate exposure.
Launch events record P4 checks, and dispatch events record P5 accepted checks.
The study compares exposed phases and includes routed work when it measures retained goals.
A proposal needs 30 completed, non-mixed exposed phases after adoption before evaluation.
Each window also needs 20 completed phases, 10 exposed phases, and 12 usable review rows.
The recorded study does not establish a reduction in repair cost.
Work moved to another phase is not saved work.

## Launch and review boundaries

`progress_history.py check-launch` and `start-pass` check the named open phase and the prompt's first `Phase <id> — <title>` line.
A recorded launch needs `--phase-id`.
A fix needs a ledger batch in flight, a landed batch awaiting review, or an explicit `--fix-cause`.
The launcher records `worker_name` for recovery.
The director records phase boundaries before the next phase's first dispatch.
Historical boundaries that are missing remain uncertain.

`commands/unit/direct.md` holds the common workflow and a `TagReferenceContract` table.
Conditional contracts live under `docs/direct/`.
A stub identifies its authoritative file and the moment to read it.
Compaction recovery reads the core and the contracts whose moments apply.
Each rule has one authoritative definition.

`/module_review`, `/team_review`, and `/api_review` run their lenses in the invoking session.
Each writes its lens result before the next lens starts.
These commands do not start separate one-shot reviewers.
The phase review workflow retains its own worker path.

`review-trial --phase-id <id>` reads the selected instance's findings ledger and completed review work.
Checkpoint notices name the checkpointed phase explicitly.
Without the option, the command reports the current phase.
The [review regime record](review-regime-trial.md) defines the counts and time fields.

Fixes extend an existing test of the same behavior when one exists.
A rare-case test requires a wrong user result, damaged saved state, or lost work.
All main-path tests remain.
[WritePromptContract item 6](../direct/write_prompt_contract.md) owns these two test rules.

## Tested publication

`scripts/production/merge_checkpoint.py` merges `origin/main` before it selects and runs checkpoint tests.
`prepare_checkpoint` selects paths from the tip before the checkpoint merge through the prepared tip.
`push_git` pushes without another merge.

`tested_main_push(production, tested_tip, retest, before_merge_tip, *, follow_main=True)` protects the final publication:

1. Acquire `main_push_lock` and fetch main.
2. Yield the tested tip when it already contains `origin/main`.
3. Otherwise, release the lock, merge main, and retest the new paths.
4. Repeat the comparison before publication.

Tests run outside the lock.
The checkpoint push, interrupted promotion, and child handoff share this loop.
`push_rollback` restores the run's original tip on `Stop`, `OSError`, or `ValueError` before publication when origin still has its old tip.
The loop's yield lies outside its preparation rollback scope.
Each caller therefore retains its own push rollback policy.
A rejected resumed handoff keeps its existing unpublished merge.
The bump path pushes first and starts its background gate afterward.

A main conflict aborts the merge and reports the conflicting paths.
A malformed incoming manifest also triggers preparation rollback.
Rollback preserves unpublished work that existed before the run.
Shrink and as-built ancestry uses Git, without requiring a checkpoint event from a particular worktree.
The check excludes merge commits when it asks whether the parent has unmerged code.
Scope filtering removes an unchanged path only when the origin merge base contains it.
A deleted plan stays in scope when another origin merge base never contained that plan.

`scripts/lib/main_publish.py` shares configuration publication for `/tune` and `/zone`.
After a nonzero commit result, it compares the named file with `HEAD`.
Matching content keeps the new value and permits publication, including a rerere failure after Git created the commit.
Other failures restore the previous file content.

## Mac pulls and visible change ownership

`PendingMacPull` stores pending `MacCheckoutPull(mac, source)` entries and an optional zone notice.
The shared queue is `<ZONE_STATE>/mac.json`, or `<XDG_STATE_HOME>/zone/mac.json` by default.
Entries coalesce by Mac path.
Clearing one completed target preserves other targets and an undelivered notice.
An old queue without a `pulls` field means one `~/.claude` pull with an unrecorded source.
An explicit empty list means the pulls finished and only the notice can remain.

After its configured retries, promotion queues SSH exit 255 without a remote `rc=` line and returns `MacPullPending`.
An answering Mac whose pull fails still fails promotion.
`zone.py drain --pulled <path>` skips only the target already pulled by promotion.
It pulls the other pending targets before it sends the notice.
`main_publish.remote_path` supplies the shared remote path quoting.

`change_review.review_commits` serves the count and row writers.
It retains every checkpoint and any other unit commit without a descendant checkpoint in the same set.
`Commit.ancestry` carries the shared parent graph, including connecting merges.
This prevents a draft worker line from adding another row after its checkpoint.
A queue commit without a later checkpoint keeps its visible lines.

Producer identity comes from records, never from commit subjects.
`checkpoint_producers(record)` reads checkpoint owners once per scan through `load_events({"checkpoint"}, record=record)`.
The shared reader locks history files, tolerates partial lines, and uses the held staff snapshot.
An older event without `owner_staff_id` uses `StaffSnapshot.unit_by_worktree(machine, path)`.
The merge writers record `KnownMergeProducer(merge, producer)` when they make the merge.
The record lives at `<history root>/merge_producers/<merge>.json`.
An inner merge's second-parent history supplies ownership for a handoff commit without a checkpoint owner.
Without recorded ownership, Producer stays empty.
Production uses the producing unit's showrunner staff id.
Foreign publication history is excluded; a local gate branch is not excluded merely because its name differs.
The promoted checkout supplies the repository for a promotion-only scan.

## Related files

| File | Role |
| --- | --- |
| `scripts/history/rounds.py`, `history.py` | Pure round records and the read-only report CLI. |
| `scripts/history/routed.py` | Route and receiving-checkpoint joins. |
| `scripts/direct/findings.py`, `config/direct.conf` | Ledger transitions, evidence, thresholds, and exposure events. |
| `scripts/direct/progress_history.py` | Phase boundaries, launch checks, shared history reads, and review trial output. |
| `docs/direct/repair_rounds.md` | Repair judgment and dispatch instructions. |
| `docs/direct/write_prompt_contract.md` | Worker prompt and test contracts. |
| `scripts/production/merge_checkpoint.py` | Checkpoint preparation, tests, rollback, push, promotion, and row publication. |
| `scripts/production/change_review.py` | Commit selection, recorded producers, production routing, and repeat-safe row insertion. |
| `scripts/lib/main_publish.py`, `zone.py`, `tune.py` | Configuration publication and the shared Mac queue. |
| `scripts/production/unit_phases.py`, `live_units.py` | Staff unit tables and actionable anomaly reporting. |

The [showrunner overview](showrunner-automation.md) defines unit tables, pause records, and checkpoint callers.
The [staff accessors](staff-accessors.md) define durable identity and hold ownership.
The [worker lifecycle](long-lived-workers.md) defines worker launch and completion.
Worker lifecycle and staff hold behavior follow these current contracts.

## Why

Counting started batches prevents launch failures from consuming the repair allowance.
Preserving routed findings keeps deferred work visible until its receiving checkpoint supplies evidence.
A held staff snapshot keeps one publication operation consistent if the staff file changes during testing.
Testing outside the publication lock allows other writers to publish while the gate runs.
The final main comparison preserves that concurrency without publishing an untested merge.
