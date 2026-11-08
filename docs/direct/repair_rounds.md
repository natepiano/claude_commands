# Repair rounds

<FindingsLedger>
Use `~/.claude/scripts/lib/py ~/.claude/scripts/direct/findings.py <command> --session-dir
"${SESSION_DIR}" --phase "<phase id>"` after <Synthesize/> confirms an issue,
including a reviewer's style finding. Each open phase has
its own ledger, and `--phase` names which one: with two phases open, a call
naming none is refused. Every launch names its phase (<PassOwnership/>), so the
launcher's own `landed` and `abandon` reach that phase's ledger.

When a daily simplify run reports items, its director performs these steps:

1. Add a phase `simplify-<date>` to the plan from the report.
2. Record every item under that phase with `--lens simplify`; discard none.
3. Send in-scope items and items that remove a copy to Codex fix workers as one repair phase.
   Use the normal checkpoint.
4. Open other items with `--held-for-decision` and include the proposed change, files, and reason.

A held item goes to the user only when it touches money, safety, product behavior the user sees, or an act that cannot be undone. Settle every other held item under DecisionEconomy and state the call in one line (user, 2026-11-02).
Record the user's answer with `decision --user-words`.

| Command | Purpose |
| --- | --- |
| `open --severity <blocker\|minor\|nit> --title <t> --file <p> [--line N] --caught-by <worker\|main\|both> [--lens <adversary\|contract\|craft\|ux\|rerun\|simplify>[,…]] [--detail <d>] [--held-for-decision]` | Create an id and record each lens. |
| `status` | read the ledger for closure review |
| `gate` | get `converged` or `dispatch`, plus batch and any advisory |
| `dispatch --covers F001,F002,... --accepted-checks "<checks or none: reason>" [--keep F001=<regression\|broken\|gate\|ruling>:<evidence>]` | record the complete kept batch; repeat `--keep` per id from repair round 4 (the user, 2026-10-28) |
| `route --id F003 --reason <spec-gap\|outside-kept> --owner <unit> --work-order "<plan path> — Phase <N> — <title>" --due "<checkpoint>" [--evidence <why>]` | leave work open in an owned follow-up; refuses in-flight, pending or accepted findings and a missing due Astra record (the user, 2026-10-28) |
| `close-route --from <run id>:<phase instance id>:<finding id> --evidence "<commit, test result, review>"` | the receiving director records closure at its checkpoint (the user, 2026-10-28) |
| `astra --id F003 --note <path> --approach "<one line>"` or `--unavailable "<reason>"` | record the approach or busy/unavailable result at the current fix-attempt count; note must exist (the user, 2026-10-28) |
| `exposure --proposal <P1\|P2> --basis "<text>" [--decision "<text>"]` | record trial exposure before the decision (the user, 2026-10-28) |
| `abandon --reason <r> [--edits-landed]` | a dispatched repair died; reopen its batch |
| `verdict --id F001 --state <accepted\|still_open\|reopened> [--evidence <e>]` | record closure evidence |
| `decision --id F001 --answer <approved\|declined> --user-words "<answer>" [--withdraw "<reason>"]` | Record the user's answer. Use `--withdraw` to replace a decided ruling and preserve its history. This command applies no change. |

`findings.py` owns batching and gating: the first round gates blockers and
minors, later rounds gate blockers, nits never gate. It rejects a partial batch,
so a round covers every gating open finding; `start-phase` resets it.
A `routed` finding remains unresolved in `status` under `routed`, outside the
batch and open counts. `history.py routed --open` shows its owner, Work Order
and due checkpoint until the receiving director records `close-route`
(the user, 2026-10-28).

**The gate never stops the run.** It answers `converged` or `dispatch` and
nothing else. Where it once stopped, it now returns that sentence in `advisory`
beside a `dispatch` verdict and the round runs. An advisory is not a gate and
never becomes one: **report it in one line** — the pattern in ordinary words,
beside the repair being dispatched — then continue. Never stop, ask permission,
re-open a decision the user has already made about this run, or edit
`~/.claude/config/direct.conf` mid-run to change what gets said.

**The gate reads only the ledger.** `converged` means that no gating finding is
open. It does not mean that the tests pass: a test result reaches the gate only
through a finding. Before each `gate` after a repair, read the worker's summary
and open a blocker for each failing test or lint line that no finding covers.
A later round gates blockers only, so a red with no finding reads as `converged`.
Count one repair round per ledger batch with a started fix pass. Relaunches of that batch add no round.
A refunded batch adds no round unless it later lands.

`dispatch` leaves its batch `repair_in_flight`.
`gate` and `verdict` refuse a finding that remains in flight.
For `herdr`, apply the completion and recovery rules in <LongLivedWorkers/>.
For an existing `mesh` pin, `assign.sh` runs `landed` after a clean exit or `abandon --edits-landed` after an error.
When that mesh launcher is gone, run `abandon --reason "<how it ended>"` before the next workflow step.
Report the interruption and the reopened findings in one line.
Pass `--edits-landed` only when repair edits exist in the tree.
Without that flag, the ledger refunds the attempt, which preserves the budget that decides
when this phase stops.
</FindingsLedger>

<PassOwnership>
Every pass is recorded by the launcher that runs it: `assign.sh` call `start-pass` and `finish-pass` around the worker they wait on,
for completion and for error alike. The unit director never calls either by hand —
a hand-written call forges a pass that never ran, and `findings.py gate` counts
passes when it decides whether a phase is converging. The recorder rejects an
unowned call.

The one exception is a launcher the unit director killed — the <DualReview/>
preemption. Close that slot's open pass with `finish-pass --status canceled
--orphaned-launcher`, which the recorder accepts only for `canceled` and only
while a pass is open. The slot comes from `UNIT_TEAM_ROLE` on the call
and never from the record — a launcher closes its own pass and nothing else, so
the unit director standing in for one names the worker it is standing in for, once
per killed launcher. A killed fix dispatch leaves its findings
`repair_in_flight` for the same reason, so `findings.py abandon` per
<FindingsLedger/> belongs beside this call.

Phase records are the unit director's, and both belong at the real boundary:
`finish-phase` for the outgoing phase and `start-phase` for the incoming one run
before that phase's first dispatch. Recording them late attributes the new
phase's work to the finished one — its title, its elapsed clock, and its pass
counts all describe the wrong phase. A phase opened beside one still running
leaves that one open: `start-phase` parks it, and `finish-phase --phase-id <id>`
closes each at its own end. Name the phase on every launch,
`assign.sh --role implement --phase <id>` and `assign.sh --role review --phase <id>`: the pass is stamped
with it, and the launcher writes its files to that phase's `${PHASE_DIR}`.
A launch without a phase is refused (the user, 2026-10-28).
When a park makes a run useless, apply <WorkerRuns/>.

Before each launch, check the prompt's phase against its active instance and title. Run start-phase and finish-phase at the real boundaries and keep parallel phases separately identified. Do not launch against a stale or absent instance. Classify planned implementation/deployment as implementation and command-only verification as an activity. A fix launch names its defect, failed gate or ledger batch. Correct active state only through supported recorder operations, preserving old evidence; never fabricate starts/finishes or rewrite historic records. Missing historic boundaries stay uncertain in analysis. (the user, 2026-10-28)

For the 30-phase accuracy trial, launchers check before any worker starts.
`assign.sh` checks each launch before it records the pass.
It refuses a missing or closed phase, a first identity line
`Phase <id> — <title>` that differs from the open phase, and a fix without an
authorized ledger round unless `assign.sh --role fix --fix-cause "<failed gate or defect>"`
names its cause. The newest round authorizes its workers while in flight and,
after its resolver lands, while covered findings await closure verdicts; its
companion can still launch. A prompt with no identity line passes; later phase
lines are content. Refusals record `launch_refused`; each `pass_started` holds
`launch_check`, and UX records `launch_checked` without opening a pass. Early
reviews check again at deferred `start-pass`; a refusal reports error. Stop on
the recorder message and correct state through supported operations
(the user, 2026-10-28).

For the 30-exposed-phase P2 trial, after `start-phase` and before the first
dispatch, record `findings.py exposure --proposal P2 --basis "<Work Order behavior>"`
when the Work Order changes visible layout, lifecycle or integration behavior,
or rule or acceptance criteria (the user, 2026-10-28).
</PassOwnership>

<Synthesize>
1. Merge the `adversary`, `contract`, armed `rerun`, UX, and main review findings.
   Combine duplicate findings and record every contributing lens with `--lens`.
   Explain each refuted finding from another lens.
   Handle daily simplify reports under <FindingsLedger/>.
   Report reviewer disagreements under <DelegationResultFormat/>.
2. Present <DelegationResultFormat/>. If the user is confused, apply
   <ExplainOnDemand/> before any choice.
3. If every remaining issue is trivial — doc-only, an agreed rename, or a
   small mechanical change whose one correct edit is evident from the finding
   itself, with no design judgment and no reviewer disputing the reading —
   the unit director applies them all directly,
   runs the implicated `verify.sh` lines, reports the edits and why they
   qualified, and records `verdict --state accepted` for each fixed issue
   with a ledger id, citing the edit and verification evidence. Then continue.
   Otherwise the whole batch follows steps 4–5.
4. Apply <DecisionRouting/> before opening findings.
5. Open every confirmed remaining issue that has no ledger id yet, then obey
   `findings.py gate`. Use its `repair_round` for the round-4 routing and kept
   evidence checks; use `round` for the ledger batch. For each `astra_review_due`
   id, run and record the Astra step in <FixDispatch/> before dispatch or routing.
   The gate records P3 exposure when a round-4-or-later decision has a gating
   open finding with a previous repair (the user, 2026-10-28):
   - `converged`: retain nits for retrospective; continue to smoke, then
     <UXReview/>.
   - `dispatch`: apply <FixDispatch/> to the complete batch, then return here.
     When the payload carries an `advisory`, say it in one line and dispatch
     anyway per <FindingsLedger/>.
6. Stop only when the plan leaves a real design choice or reviews conflict on
   intended behavior:

```
Your choice:
1. One more worker fix pass — [work and cost; recommendation with reason].
2. Stop here — preserve remaining items as written todos.
3. Talk through an item first.
```

Choice 1 applies <FixDispatch/>; choice 2 continues to smoke, then <UXReview/>;
choice 3 preserves the gate. With no gating issues, continue to
<RunApplicationSmokeTest/>, then <UXReview/>.
</Synthesize>

<FixDispatch>
Apply <LongLivedWorkers/>.
When a repair round fails or its next edit replaces a run's tree, apply <WorkerRuns/>.

Before you assign a repair, have a worker quote the failing log line and the
current source line that causes it. Put both quotes in the fix prompt. A repair
aimed at an unchecked cause costs a full round.

For a `dispatch` batch, set `${FIX_ROUND}` from the gate's `round` and create
`${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md` under
<WritePromptContract/>. Work Specification contains every batch id with concrete
file/line findings and intended behavior. Verification contains only implicated
`verify.sh` lines—usually check and test, plus lint after the worker's last
edit when the phase has a lint run, per <PhaseChecks/>.

Put known fixture/setup updates, lint, tests and merge follow-through in the original repair prompt. After handoff, inspect the remaining action. If it only runs unchanged commands and no edit or judgment is pending, the director runs the existing detached verification workflow; do not dispatch a repair worker. Preserve the commands, output, tree identity and review result. A failing command reopens or retains its finding and goes to the owning worker if an edit is needed. Fixture expectations, source edits and conflict choices remain owner work; a green command does not replace closure review. (the user, 2026-10-28)

At each repair handoff whose Verification list holds lint or test commands,
record `findings.py exposure --proposal P1 --basis "<handoff and commands>"`
before choosing the runner. Give `--decision "director ran the commands"` or
`--decision "worker: <the edit or judgment still pending>"` (the user, 2026-10-28).

A repair runs **one worker per file set no other worker touches**: when the failing
tests or findings split that way, always run as many workers as you can reasonably
manage; otherwise one. User, 2026-10-04. Use the open `impl` and `test` slots
first, preferring the worker that owns the files and reassigning another open worker
under <RoleReassignment/> when needed. Only then open `fix2`, `fix3`… when no
open worker can take another file set. Each worker's task and kind are `fix`, its
ninth argument remains its slot, and it gets its own
`fix_prompt_${FIX_ROUND}[_<slot>].md`. Each worker repairs its findings.
Cover each testable finding under the two test rules in [write_prompt_contract.md](write_prompt_contract.md), item 6.
Name the covering table row or test in the summary.
The test must fail without the repair. A finding about what is
drawn gets a test that renders and reads the pixels back, or a live pixel check
the prompt names; a test of components, constants or hand-written pointer hits
does not count. It names each test for the behavior it pins and puts the
finding id only in its summary; an id in code outlives the review that defined
it. <ClosureReview/> is the cold read, so no worker is spent
on one here. A worker's file set is its findings' files plus their test targets;
its prompt names every other worker's files as read only.

Before editing, list accepted behaviors and checks reached by the repair's planned paths, using earlier verdicts, real callers and the current suite. Carry their commands/live views into Verification beside the new regression check. Preserve assertions unless a cited approved goal change or evidence shows an expectation is wrong. Run the new and affected accepted checks on the same tree. Compare a suspected old defect against the prior accepted tree. Reopen lost accepted behavior with failing evidence. Keep cold closure review for public contracts, registration and cross-batch invariants; route unrelated old defects under item 7. (the user, 2026-10-28)

For the 30-phase P5 trial on all repairs, carry those checks in
`dispatch --accepted-checks "<accepted checks the edits can break>"`; use
`--accepted-checks "none: <why no accepted behavior is reached>"` only with that
evidence. Dispatch refuses an empty value; its event records the list. A value
without `none:` records P5 exposure (the user, 2026-10-28).

For `herdr`, dispatch repairs through `assign.sh --role fix --prompt <path> --cwd <worktree> --fix-cause "<defect>"`.
The worker command selects the worker and keeps its native session.
For an existing `mesh` pin, use `assign.sh --role fix --to <full-worker-name>` with the repair prompt and phase options.
Omit `--to` only for a new worker allowed by <LongLivedWorkers/>.
Use messages only for questions; the launcher sends the assignment prompt.

Use `history.py rounds` for repair counts. Relaunches of one ledger batch count as one repair round. From repair round 4 onward, do not dispatch a missing target, rule interpretation or acceptance case in this phase unless evidence establishes a phase-caused regression, broken working behavior or test/gate failure. A broad original permission to fix pilot defects does not override this check. A later explicit user or showrunner ruling may keep named work; cite it. Route every other missing criterion to a follow-up Work Order in the owning unit, with its target decision, implementation, tests and acceptance evidence. Do not dispatch a batch until every finding is either retained with evidence or assigned to that follow-up. Split mixed batches; keep their eligible defects here. Never mark an unmet pilot acceptance bar passed because its work was routed.

Work Orders state the acceptance observation, unit or comparison, and named real-path test or live check for changed behavior. Reuse established targets. Resolve choices through the showrunner before dependent work; do not invent a number to fill the form. A later changed goal updates the order explicitly. (the user, 2026-10-28)

Use `history.py rounds` for repair counts. Relaunches of one ledger batch count as one repair round. Before any round 4-or-later dispatch on a repeated finding, require evidence that it is a phase-caused regression, broken working behavior or test/gate failure. Earlier attempts and an open verdict alone are not that evidence. Broad original permission for pilot fixes does not override landing. Cite any later explicit user/showrunner ruling that keeps the named work. Route a repeated finding outside these categories to its owning unit's follow-up Work Order before another repair starts. Keep the finding open and link its checks, previous attempts, due checkpoint and final acceptance result. Split mixed batches and retain all eligible work. If eligibility is unknown, diagnose it rather than silently route or count it as a certain saving.

For retained work after two completed failed fixes, record the failure, approaches tried, cause diagnosis and next distinguishing test before another approach. Interrupted workers and account moves remain continuations, not completed rejected patches. (the user, 2026-10-28)

At each fix limit, try something different. When a phase reaches repair round 4 with a finding still open, or a kept finding has failed two fixes, the director writes a short note: the finding, the tries so far and why each failed. It runs `/astra_review` on the note. Astra's approach goes into the next try, or into the follow-up Work Order when the finding moves out (the user, 2026-10-28).

Before forming the batch, use the gate's `repair_round`, which counts
`history.py rounds` repair groups, for the round-4 checks; keep `round` for
`${FIX_ROUND}` and ledger dispatches. P2 is a 30-exposed-phase trial from
round 4; P3 is a 30-phase trial. For every `astra_review_due` id, write the note
and run `/astra_review --run-directory "${SESSION_DIR}" --phase <id> <note>`.
Record `findings.py astra --id <id> --note <note> --approach "<one line>"`,
or `--unavailable "<reason>"` when the review reports busy or unavailable.
Put its approach into the next prompt or receiving Work Order. A new failed
fix makes the review due again (the user, 2026-10-28).

Route each outgoing id with `findings.py route`, naming its owner, receiving
Work Order and due checkpoint, before dispatch. Routing is allowed in any
round, including item 7's landing rule. From repair round 4, give one
`--keep F003=<regression|broken|gate|ruling>:<evidence>` per covered id.
The tools refuse a missing due Astra record, kept evidence or accepted checks.
Every gating open id still belongs in `--covers`; a routed id leaves that
batch but stays unresolved until its receiving checkpoint records `close-route`
(the user, 2026-10-28).

Apply <PassOwnership/>'s phase and cause check before each launch
(the user, 2026-10-28). Run
`findings.py dispatch --covers <all kept batch ids> --accepted-checks "<checks or none: reason>" [--keep <id>=<category>:<evidence> ...]`
before launching, then:

```sh
UNIT_RESOLVES_ROUND=1 bash ~/.claude/scripts/direct/assign.sh --role fix \
  --to "<worker name from LongLivedWorkers>" --phase "<phase id>" --session-dir "${SESSION_DIR}" \
  --cwd "${WORKING_DIR}" --prompt "${SESSION_DIR}/fix_prompt_${FIX_ROUND}.md" \
  --paths "${SESSION_DIR}/fix_paths_${FIX_ROUND}.txt" \
  --fix-cause "<failed gate or defect>" --pass "${FIX_ROUND}" &
```

For an existing `mesh` pin, `UNIT_RESOLVES_ROUND=1` makes the launcher resolve the round.
For `herdr`, apply the completion and recovery rules in <LongLivedWorkers/>.
Each further worker uses `UNIT_RESOLVES_ROUND=0`, its own prompt, and its slot.
The round ends only after every worker completes.

Apply <DispatchContract/>; set `EARLY_REVIEW=none` and clear
`${EARLY_REVIEW_SLOT}` at dispatch, and close the
turn with the progress header per <DelegationResultFormat/>. While a fix runs
that will receive a worker closure review, <EarlyReviewArm/> may arm that
reviewer early.

**A repair the main read can close, closes without a worker review.**
Apply <ReviewDiffContract/> and read every repair hunk yourself.
Check each testable id's coverage under the two test rules in [write_prompt_contract.md](write_prompt_contract.md), item 6.
Record each verdict and continue to <Synthesize/> when the diff answers every id and each hunk is one of:

- a path the batch's own findings named, a new file one of those paths
  creates, or a test file holding the batch's coverage under those rules;
- a doc comment, user-facing copy, or comment change in any path, with no
  code change beside it;
- a new private or crate-private type, function, or accessor whose every
  caller is inside the repair diff.

**A repair to what is drawn is proved live before it closes.** Before closing
the round or dispatching its closure review, launch the build and probe each
repaired visible finding in the running app (BRP for a Bevy app): hover it,
crop the shot, read the drawn pixels or layout. In Hana, shoot with
`/hana_shot` (a stored view or `--target`), every view in one call, never a
camera worked out by hand. A visible finding that fails a second time gets a live
diagnosis of its cause before another repair turn, and the next prompt carries
the measured cause.

Dispatch the normal <DualReview/> closure review only when the diff cannot
answer the question: a public API or signature change with a caller outside
the diff, a registration, transition, or invariant reaching past the batch, an
id whose verdict reads unclear, a claim the worker says it could not verify and
the main read cannot verify either, or a new defect the repair introduced.
Uncertainty routes to the reviewer; a hunk fully read and fully understood
does not. A closure review costs a stage of the phase, so it is spent on a
question, never on a path. Use the unit's own reviewer under <TeamReview/> and dispatch with
`assign.sh --role review --prompt <closure prompt> --cwd <worktree>`.

On completion, read the dispatch's status and summary paths.
A successful dispatch continues as above. An error applies <WorkerLaunchFailure/>.
If the error remains, apply <RetainPhaseReservation/> and report the fix log.
Record the error, clear the session marker, and stop.
The launcher records either outcome in the ledger.
For an existing `mesh` pin, if a dispatch ends without a terminal status, resolve its round with `findings.py abandon` under <FindingsLedger/>.
For `herdr`, apply the recovery in <LongLivedWorkers/> first, because the pane worker can still finish the round.
Run `abandon` only when that recovery cannot finish it.
Then apply <RetainPhaseReservation/> before review, another dispatch, or a report about the round.
</FixDispatch>
