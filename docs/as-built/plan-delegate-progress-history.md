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
- `progress_reported`
- `finding_opened` / `finding_batch_dispatched` / `finding_verdict` /
  `finding_gate`, appended by `scripts/delegate/findings.py` to the same stream

Every event repeats its query dimensions: run and phase identity, worktree,
branch, working directory, plan doc, project/phase/pass timestamps, current pass
kind and fix count, main-agent identity, and called-agent identity. Progress
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

`gate` returns one of three verdicts and the orchestrator follows it — it never
decides on its own whether to run another round:

- `converged` — nothing gating is open; go to the smoke test.
- `dispatch` — repair the whole returned `batch` in one round.
- `stop` — hand the run to the user with `stop_reason`.

Three rules are enforced by refusal, not by prose:

- **Severity narrows.** Round 1 gates blockers and minors; every later round
  gates blockers only. Nits never gate, and are returned as `non_gating_open` so
  they can still be reported.
- **One batch per round.** `dispatch --covers` refuses a partial batch, so a
  round cannot repair one finding, re-review, and rediscover the rest.
- **No gate with verdicts outstanding.** `gate` refuses while any finding is
  `fixed_pending_review`; reopening an `accepted` finding requires `--evidence`
  naming the hunk that invalidated it.

Stop conditions, checked in this order: a finding reopened `MAX_REOPENS` times
after being accepted; a finding that failed to close after `MAX_FIX_ATTEMPTS`
repair attempts; `STALLED_ROUNDS` consecutive rounds with no decrease in the
gating-open count; `MAX_CONSECUTIVE_SAME_KIND_PASSES` passes of one kind in a
row; more than `MAX_REVIEW_CANCELLATIONS` blind-review cancellations; a spent
repair budget; the `RUNAWAY_ROUNDS` backstop. The convergence tests come first
so a run grinding through eight rounds of genuinely new defects is never
interrupted, because progress is measured, not counted.

The repair budget is the first round's gating count times
`REPAIR_ROUNDS_PER_FINDING`, never below `MIN_REPAIR_BUDGET`. All of these
limits are set in `~/.claude/config/delegate.conf`, read at startup by
`findings.py` and required there — an absent or unusable value exits 2 rather
than falling back. The shipped values give a phase 3 automatic fix rounds at
minimum and 5 at most.

## Commands

```text
findings.py open --session-dir <dir> --severity blocker|minor|nit --title <text> --caught-by delegate|main|both [--file <path>] [--line <N>] [--detail <text>]
findings.py verdict --session-dir <dir> --id <F00N> --state accepted|still_open|reopened [--evidence <hunk>]
findings.py gate --session-dir <dir>
findings.py dispatch --session-dir <dir> --covers F001,F002,...
findings.py status --session-dir <dir>
findings.py override --session-dir <dir> --reason <the user's own words>
```

`override` is the correction path for a `stop` whose inputs were wrong — an
aborted launcher, a pass recorded outside a launcher, a count carried across a
mislabeled phase boundary. It never edits the run history that produced the
stop; it appends beside it, names the one stop reason it clears, and is spent by
the fix round it authorizes. It refuses a reason under 20 characters and refuses
outright when the gate is not stopping.

```text
progress_history.py start-run --session-dir <dir> --working-dir <dir> [--plan-doc <path>]
progress_history.py start-phase --session-dir <dir> --phase-id <id> --phase-title <title> [--work-order-file <path>]
progress_history.py start-pass ...        # launcher only
progress_history.py finish-pass ...       # launcher only, or --orphaned-launcher --status canceled
progress_history.py calibrate --session-dir <dir> --candidate-percent <N>
progress_history.py progress --session-dir <dir> --project-raw-percent <N> --project-percent <N> --phase-raw-percent <N> --phase-percent <N> --cap-stage <stage> --activity <text> [--phase-override-reason <evidence>]
progress_history.py finish-phase ...
progress_history.py finish-run ...
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

`progress` writes the event and prints the project section, one blank line, then
the phase/pass section used by `/plan:delegate`. Project and phase percentages
have independent unchanged timers. Each also carries an `eta`, the elapsed clock
extended across what the displayed percentage says remains; it is omitted at 0
and 100, where there is no rate to extend or nothing left to extend it over. The
pass line has none — a fix is a sub-phase with no percentage of its own. Because
the projection reads the same capped number the line displays, it never
contradicts the percentage beside it, and it inherits that number's accuracy:
derived phase counts for the project, the reporter's estimate for the phase.

The last line is the wall clock: `now <local timestamp>` and, when it can be
resolved, `next report <time>` — the date is repeated there only when the next
tick lands on a later day. Every other number in the header is a duration, which
says how long but never when; this line is what tells a reader whether the report
in front of them is current and how long until the next one. The next tick comes
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
