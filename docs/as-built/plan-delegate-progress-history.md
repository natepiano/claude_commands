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

Every event repeats its query dimensions: run and phase identity, worktree,
branch, working directory, plan doc, project/phase/pass timestamps, current pass
kind and fix count, main-agent identity, and called-agent identity. Progress
events add the current activity, raw percentage, calibrated/reported percentage,
suggested percentage, decision source, override reason, unchanged-percentage
duration, each elapsed clock, and the calibration evidence used for that report.
The decision row also repeats the historical bias, suggested adjustment, and
chosen adjustment so downstream analysis does not need to unpack the calibration
snapshot.

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

Before a progress report, `calibrate` first tries history matching the main and
called model/effort plus pass kind. It falls back through called-agent/pass,
pass-only, and global samples. A suggestion requires at least five completed
percentage streaks with the same raw estimate (or within five percentage points
when exact history is sparse). The raw estimate is always retained even when
the reported estimate uses the suggestion, so later aggregation can measure
whether the calibration helped and continue tuning that original estimate.

## Commands

```text
progress_history.py start-run ...
progress_history.py start-phase ...
progress_history.py start-pass ...
progress_history.py finish-pass ...
progress_history.py calibrate --session-dir <dir> --candidate-percent <N>
progress_history.py progress --session-dir <dir> --raw-percent <N> --percent <N> --activity <text> [--override-reason <evidence>]
progress_history.py finish-phase ...
progress_history.py finish-run ...
progress_history.py aggregate [--percent <N>]
```

`progress` writes the event and prints the exact five-line Markdown header used
by `/plan:delegate`. `aggregate` groups by raw percentage and pass kind, reads
every durable run, and emits raw/suggested/reported error plus decision-source
counts as JSON suitable for further analysis.
