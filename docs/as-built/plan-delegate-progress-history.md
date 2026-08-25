# Plan-delegate progress history

`scripts/delegate/progress_history.py` is the shared progress recorder for
Claude and Codex implementations of `/plan:delegate`. It writes append-only
per-run JSONL files under:

```text
~/.local/state/plan-delegate/runs/<run-id>.jsonl
```

The live session cache remains in
`/tmp/claude/delegate/<run-id>/progress_history_state.json`. Deleting `/tmp`
does not remove the durable event stream.

## Event model

Schema version 1 records these event types:

- `run_started` / `run_finished`
- `phase_started` / `phase_finished`
- `pass_started` / `pass_finished`
- `activity_started` / `activity_finished`
- `progress_reported`
- `finding_opened` / `finding_batch_dispatched` / `finding_verdict` /
  `finding_gate`, appended by `scripts/delegate/findings.py` to the same stream

Every event repeats its query dimensions: run and phase identity, worktree,
branch, working directory, plan doc, project/phase/pass timestamps, current pass
kind and fix count, main-agent identity, and called-agent identity. Only the
window that is *open* stamps an event with its identity. A finished pass stays
in the session state so the next launcher can be told what it replaced, and
stamping it unconditionally attributed every activity — and every progress
report made during one — to whichever delegate happened to run before it. Progress
events add the current activity, separate project and phase percentages, each
assessment's unchanged duration, the phase's raw, suggested, and reported
percentages, decision source, override reason, each elapsed clock, and the
calibration evidence used for that report. The phase decision also repeats the
historical bias, suggested adjustment, and chosen adjustment so downstream
analysis does not need to unpack the calibration snapshot.

## Project clock

`start-run` resolves the project clock without an agent-supplied timestamp. For
a supplied plan, the recorder uses its timezone-qualified `Project started`
field. If absent, it persists the oldest Git commit time for that plan, or the
run start when the plan has no history. For ad hoc work without a plan, it
reuses the most recent plan-backed clock for the exact working directory and
branch. With no such history, the project clock starts with the run.

The live state and every event record `project_start_source` and
`project_plan_doc`. `progress` resolves these fields for legacy active runs that
do not have them, so an ad hoc fallback is corrected on its next report. The
agent invokes the recorder; it does not calculate, pass, or edit the timestamp.

Every duration below one day renders as `HH:MM:SS`, including the leading hour
field. Longer durations render as `<days> day(s) HH:MM:SS`.

`phase_started` also carries the Work Order's size when `start-phase` is given
`--work-order-file`: `work_order_lines` (nonblank), `work_order_words`,
`work_order_file_targets` (distinct backticked strings holding a `/` or a file
extension), and `work_order_top_level_bullets`. Nothing enforces a threshold on
these. They accumulate so a later release can answer, from real runs, what size
of phase actually converges — and only then set a limit in
`/plan:to_phased_plan`.

The `finding_*` events are written by `findings.py`, which reads
`progress_history_state.json` for the run's `history_file` and the active
phase's identity. A findings ledger without progress state still works; it just
keeps no durable history. Each `finding_*` event repeats `phase_instance_id` and
`phase_id`, so the fix loop's rounds join to the phase they belong to.

The main agent's exact family, session id, model, and effort come from the
active Claude or Codex transcript. `implement.sh` and `review.sh` provide the
called task, family, model, and effort after resolving `config/agents.conf`.

Each recorder command takes a session lock around its state transition. Each
append also takes an exclusive history-file lock, flushes, and calls `fsync`;
aggregate reads take a shared history-file lock. Different runs have different
files, while multiple processes participating in one run share its locked
stream and live-state cache.

## Percentage calibration

Only phases with a `phase_finished` event whose status is `completed` become
calibration samples. For each consecutive percentage streak, aggregation knows:

- when that percentage was first reported;
- how long it remained unchanged;
- when the phase actually finished;
- the phase's elapsed fraction at report time;
- the raw estimate and the final reported estimate.

This produces remaining-time, unchanged-duration, percentage-bias, and
raw-versus-calibrated absolute-error distributions. It does not claim that work
advances linearly with time; the time fraction is a consistent outcome signal
for correcting repeated systematic optimism or pessimism.

Each progress event classifies the choice automatically:

- `raw`: insufficient applicable history, so the raw estimate was shown;
- `calibrated`: the statistically suggested percentage was shown;
- `override`: the main agent chose another percentage from stronger current
  evidence.

An override requires a non-empty reason naming that evidence. Aggregation keeps
raw, suggested, and reported error separate, counts each decision source, and
reports whether the suggested and final reported adjustments improved on the
raw estimate. This allows later changes to the bias formula to be evaluated
against both accepted suggestions and rejected ones.

## Stage caps

A percentage is an estimate; a stage is a fact. `progress` therefore requires
`--cap-stage` on dual-layout calls — the script refuses an uncapped one — and
clamps the reported phase percentage to that stage's ceiling:

| `--cap-stage` | Phase cap |
|---|---|
| `implementation` | 75 |
| `initial_review` | 85 |
| `open_findings` | 90 |
| `closure` | 95 |
| `checkpoint` | 98 |
| `complete` | 100 |

Project percent is clamped to 99 at every stage except `complete`.

The clamp is applied **after** calibration, and the decision fields
(`raw` / `calibrated` / `override`, the bias, the suggested adjustment) are all
computed on the uncapped values. Calibration therefore keeps learning from what
the agent actually estimated rather than from what the ceiling allowed. Each
event records both: `phase_percent` / `project_percent` are the reported capped
values, and `phase_uncapped_percent`, `project_uncapped_percent`, `cap_stage`,
and `phase_percent_capped_by` preserve what was clamped and by which ceiling.

Percentages moving backwards is correct behavior here: a phase reporting 92 at
`closure` that reopens findings drops to 90 at `open_findings`.

Before a progress report, `calibrate` first tries history matching the main and
called model/effort plus pass kind. It falls back through called-agent/pass,
pass-only, and global samples. A suggestion requires at least five completed
percentage streaks with the same raw estimate (or within five percentage points
when exact history is sparse). The raw estimate is always retained even when
the reported estimate uses the suggestion, so later aggregation can measure
whether the calibration helped and continue tuning that original estimate.

## Findings ledger

`scripts/delegate/findings.py` bounds `/plan:delegate`'s fix loop. It replaced a
`FIX_PASS < 10` counter, which could not converge: the re-review after each fix
was a fresh blind review of a now-larger diff, with no memory of what had already
been accepted, so it always returned something.

State lives in `findings_state.json` beside the progress state and resets itself
when the progress state's active `phase.instance_id` changes. Findings get stable
ids (`F001…`) that survive across rounds, so "the same finding came back" is a
fact the script can check rather than a judgment the orchestrator has to make.
Each finding is `open`, `fixed_pending_review`, or `accepted`.

`gate` returns one of two verdicts and the orchestrator follows it — it never
decides on its own whether to run another round:

- `converged` — nothing gating is open; go to the smoke test.
- `dispatch` — repair the whole returned `batch` in one round.

There is no third verdict. The gate does not stop the run, and it holds no
override, because there is nothing to override.

Three rules are enforced by refusal, not by prose:

- **Severity narrows.** Round 1 gates blockers and minors; every later round
  gates blockers only. Nits never gate, and are returned as `non_gating_open` so
  they can still be reported.
- **One batch per round.** `dispatch --covers` refuses a partial batch, so a
  round cannot repair one finding, re-review, and rediscover the rest.
- **No gate with verdicts outstanding.** `gate` refuses while any finding is
  `fixed_pending_review`; reopening an `accepted` finding requires `--evidence`
  naming the hunk that invalidated it.

Advisories, checked in this order and reported in `advisory` beside a `dispatch`
verdict: a finding reopened `MAX_REOPENS` times after being accepted; a finding
that failed to close after `MAX_FIX_ATTEMPTS` repair attempts; `STALLED_ROUNDS`
consecutive rounds with no decrease in the gating-open count;
`MAX_CONSECUTIVE_SAME_KIND_PASSES` passes of one kind in a row; more than
`MAX_REVIEW_CANCELLATIONS` blind-review cancellations; a spent repair budget; the
`RUNAWAY_ROUNDS` backstop.

Each of these used to stop the phase and hand it to the user. They stopped too
much: the stall test in particular fires on the honest pattern where every round
repairs exactly what it was handed and the next gate finds something genuinely
new, which reads as one open blocker round after round. The count cannot tell
that apart from a repair that will not take. So the tests were kept for what they
are good at — naming the shape of a phase that is not converging — and the
enforcement was dropped. The orchestrator reports the sentence and dispatches the
round; the user watches the run and stops it themselves when it warrants.

The repair budget is the first round's gating count times
`REPAIR_ROUNDS_PER_FINDING`, never below `MIN_REPAIR_BUDGET`. All of these
limits are set in `~/.claude/config/delegate.conf`, read at startup by
`findings.py` and required there — an absent or unusable value exits 2 rather
than falling back. They now decide when an advisory is worth printing rather than
when a phase stops, so the shipped values set where a phase starts being reported
as slow, not where it is cut off.

## Commands

```text
findings.py open --session-dir <dir> --severity blocker|minor|nit --title <text> --caught-by delegate|main|both [--file <path>] [--line <N>] [--detail <text>]
findings.py verdict --session-dir <dir> --id <F00N> --state accepted|still_open|reopened [--evidence <hunk>]
findings.py gate --session-dir <dir>
findings.py dispatch --session-dir <dir> --covers F001,F002,...
findings.py status --session-dir <dir>
```

```text
progress_history.py start-run --session-dir <dir> --working-dir <dir> [--plan-doc <path>]
progress_history.py start-phase --session-dir <dir> --phase-id <id> --phase-title <title> [--work-order-file <path>]
progress_history.py start-pass ...        # launcher only
progress_history.py finish-pass ...       # launcher only, or --orphaned-launcher --status canceled
progress_history.py start-activity --session-dir <dir> --label <label> --activity <text>
progress_history.py finish-activity --session-dir <dir> [--status completed|error|canceled|interrupted] [--result <outcome>]
progress_history.py calibrate --session-dir <dir> --candidate-percent <N>
progress_history.py progress --session-dir <dir> --project-raw-percent <N> --project-percent <N> --phase-raw-percent <N> --phase-percent <N> --cap-stage <stage> --activity <text> [--phase-override-reason <evidence>]
progress_history.py finish-phase ...
progress_history.py finish-run ...
progress_history.py timeline --session-dir <dir> [--phase <id>]
progress_history.py phase-count --plan-doc <path> [--phase-percent <N>]
progress_history.py aggregate [--percent <N>]
```

`implement.sh` and `review.sh` own pass lifecycle: they set
`PLAN_DELEGATE_PASS_OWNER=launcher` on their own `start-pass` / `finish-pass`
calls, per invocation so the agent subprocess never inherits it. The recorder
rejects an unowned call, because a hand-written pass forges one that never ran
and `findings.py gate` counts passes to decide whether a phase is converging.
The single exception is a launcher the orchestrator killed, whose pass stays
open: `finish-pass --status canceled --orphaned-launcher` closes it, and only
that status, and only while a pass is open.

`progress` writes the event and prints two tables under a line naming the
worktree, the branch, and — when the plan's headings can be counted — the
position of the phase in flight, `phase N of M`. That position is the finished
count plus one, off the same headings the project percentage derives from, so
the line and the table can never disagree; a plan that cannot be counted keeps
the short worktree-and-branch form. The first table holds the clocks: a project
row and a phase row carrying the reported percentage, elapsed, ETA, unchanged,
— when the plan's headings can be counted — how many phases are done, and last
the best and worst arrival the percentage still allows. The second is the stage
table, one row per window the phase has opened,
oldest first: the stage, the main agent that orchestrated it, the delegate that
ran it, its start time, its elapsed, and its result. Under the table
sits the running stage's activity sentence, which no fixed-width column can
carry, and then the wall clock.

Stage names come from the pass kind and its position among that kind in the
phase — `Impl`, `Review 1`, `Review 2`, `Fix 1` — and a fix carries the round
the ledger dispatched, so its number is the one convergence counts. An
activity's name is its `--label`, and its `Delegate` cell is empty: the main
agent ran that window itself. Both identity columns are read per window rather
than per run, and `start-pass` and `start-activity` re-detect the orchestrator
as they open, so a main agent that changes model or effort part way through
shows the change on the rows after it. Detection that comes back unknown leaves
the stored identity alone, because a window that cannot answer must not erase
the answer already recorded.

Results come from the same event stream, over the interval that starts at one
window and ends at the next: findings opened after a review make it `N found`,
a landed repair batch makes a fix `N landed`, verdicts recorded after a closure
review make it `N fixed` plus `M open` or `M new` where those apply, an activity
shows the `--result` its own launcher recorded, and any non-completed status
shows itself. The interval is what does the attributing — the main agent opens,
dispatches, and settles findings in the gap after a window closes, not while it
runs.

Project and phase percentages have independent unchanged timers. Each also
carries an ETA: the elapsed clock extended across what the displayed percentage
says remains, added to the current time, and printed as an arrival —
`today HH:MM`, `tomorrow HH:MM`, or `YYYY-MM-DD HH:MM` further out, always on a
24-hour clock. A remaining duration has to be added to the clock by hand every
time it is read, and the answer changes with every report; an arrival is that
addition already done. It is omitted at 0 and 100, where there is no rate to
extend or nothing left to extend it over. Because the projection reads the same
capped number the row displays, it never contradicts the percentage beside it,
and it inherits that number's accuracy: derived phase counts for the project,
the reporter's estimate for the phase.

`ETA low` and `ETA high` close the row with the two arrivals that same
percentage still allows, each followed by its own distance from the ETA as
`(-HH:MM)` and `(+HH:MM)` so the swing reads without subtracting clock times by
hand. Both come from re-running the ETA's own projection over a percentage moved
a plausible distance each way, which makes the band asymmetric on purpose: at
20% a few points of optimism cost far more hours than the same few points of
pessimism save, and a symmetric band would hide exactly that. The distance is
the phase estimate's own measured error when a calibration cleared its sample
floor, and ten percentage points otherwise; the project row always takes the
default, where it reads as the phases left being worth more or less time than
their share of the count. Neither end may stray past double or half the reported
rate whatever the measured error says — a spread wider than the percentage
itself would otherwise push the pessimistic end to 1% and quote an arrival
ninety-nine times the elapsed clock. Both cells are blank wherever the ETA is.

`timeline` renders the stage table alone, for one phase or for every phase of
the run, and needs no open window. It answers the questions asked after the
fact — how many fix passes a phase took, how long each review ran — without
reading the event stream by hand.

The last line is the wall clock: `now <local timestamp>` and, when it can be
resolved, `next report <time>` — the date is repeated there only when the next
tick lands on a later day. The elapsed and unchanged columns are durations, which
say how long but never when, and an ETA says when the work lands rather than when
this report was made; this line is what tells a reader whether the report in
front of them is current and how long until the next one. The next tick comes
from `${SESSION_DIR}/progress_timer` when a timer is armed and its deadline is
still ahead, and otherwise from `PLAN_DELEGATE_PROGRESS_INTERVAL_SECONDS` in
`delegate.conf` — the same key `progress_timer.sh` reads, so the reported time and
the timer that fires cannot disagree. At the usual reporting moment the marker is
already gone, because the timer clears it as it ticks and the next one is armed
only at the end of the turn, so the interval is the ordinary source. An unusable
interval drops the clause instead of stopping the header: a report is not a timer,
and `progress_timer.sh` is the caller that fails loudly on that key.
`aggregate` calibrates phase estimates by
raw percentage and pass kind and emits raw/suggested/reported error plus
decision-source counts as JSON suitable for further analysis.
