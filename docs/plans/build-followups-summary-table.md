# The summary table in every /unit:report

> **Status: APPROVED 2026-10-19 — 1 phase.** Every `/unit:report` shows the summary table again: one row for the project and one for the current phase.
> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

Source: the user, via berth-parallel and the showrunner, 2026-10-19: "i want to see the summary table when i run /unit:report". The showrunner's scope, same message: since 67fd6c7e (shorter reports, 2026-10-16), `progress_history.py` prints the summary table only with `--tables`. Bring it back in every `/unit:report`, in the dual (unit) layout only. The round table stays behind `--tables`. The legacy layout stays as it is.

## Delegation Context

- **Project:** claude_commands (`~/.claude`): the slash commands, delegate contracts and scripts that run units and productions
- **Project started:** 2026-10-19T05:05:00+00:00
- **Stack:** Markdown prompt contracts; Python 3.13 scripts under `scripts/`
- **Key files:** `scripts/delegate/progress_history.py` (`_summary_table` at about line 3933; `_print_last_recorded` at about line 4214; the progress path's `summary = _summary_table(...) if tables else []` at about line 4465); `scripts/delegate/test_progress_history.py` (the short-report tests from about line 4560); `commands/unit/report.md` steps 5 and 6
- **Test:** `python3 -m unittest scripts.delegate.test_progress_history` from the repo root; the gate is `merge_gate --tests-only` from the repo root
- **Lint:** basedpyright on touched Python: 0 errors, 0 warnings (it exits 3 on success; read the summary line)
- **Invariants:** the forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) holds in every edited line; Simplified Technical English in all prose

## What runs today

- `progress` in the dual layout prints the phase heading, the ETA line, the stage line and the worker lines. `--tables` adds the summary table (`_summary_table`: a Project row and a Phase row, with percent, elapsed, unchanged, ETA, low and high, and a Phases column when the plan can be counted) and the round table with its `Earlier:` line.
- `_print_last_recorded` (between windows, and for an activity open after its phase closed) does the same: the summary table only under `--tables`.
- `commands/unit/report.md` step 5 tells the director to copy the tables only for `/unit:report tables`; step 6 puts them after the ETA line only for `tables`.
- Four short-report tests assert that no line starts with `|`.

## Phases

### Phase 1 — The summary table in every report  · status: done

#### As-built

- In the dual (unit) layout, `progress` prints the summary table (`_summary_table`: a Project row and a Phase row) after the ETA line, with or without `--tables`. The progress path computes `summary` when `uses_dual_layout or tables`; the legacy layout keeps `if tables`.
- `_print_last_recorded` (between windows, and for an activity open after its phase closed) always prints the summary table with the last recorded clocks. Its `_phase_section(..., tables=tables)` call is unchanged, so the round table and its `Earlier:` line still need `--tables`.
- The `--tables` help text is `include the round table`.
- `/unit:report` step 5 copies the summary table byte for byte in every report; step 6 puts it after the ETA line. `tables` adds the round table after the summary table, plus `Earlier:` and the verification gate lines.

**Files:**
- `scripts/delegate/progress_history.py` — the summary table in every dual-layout report; docstrings and `--tables` help
- `scripts/delegate/test_progress_history.py` — short-report tests require the summary header and its Project and Phase rows and no round table; a between-windows test without `--tables`; a legacy-layout test with no table
- `commands/unit/report.md` — steps 5 and 6 and the `tables` sentence
