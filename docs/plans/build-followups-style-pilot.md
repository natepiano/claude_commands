# Style showrunner pilot — cargo-liner

Overall result: **pending**. Live bootstrap, operational measurements and the
recall study have not run in this dispatch. Phases 18 and 19 require the
completed pilot criteria. No live repository or vault was changed by this dispatch.

Recorded evidence: `/tmp/claude/unit/24e20a41-688b-4fff-9f5d-d243a63ee4e2/pilot_evidence.md`.
The entries below transcribe that file; pending entries have no measurement yet.

## Real-repo checks

| Check | Result | Numbers / evidence | Source command |
| --- | --- | --- | --- |
| Before step 1: missing Proposals refused | pass | 2026-10-17 07:39 EDT; exit 2, `add_style: production doc lacks Proposals` | From `/home/natepiano/rust/cargo-liner`: `add_style.py --production /home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md --model claude-opus-5-5 --effort high --check` |
| After step 1b: rule classification | pass | Main `f65032bb`, about 07:43 EDT; exit 0, 64 rules, 0 unclassified; 11 local rows tagged | `rules.py list --repo ~/rust/cargo-liner` |
| After step 1b: initialize ledger | pass | Exit 0 at main `f65032bb…` | `style_ledger.py set-tip --repo ~/rust/cargo-liner --tip f65032bb…` (abbreviated recorded tip) |
| After step 1b: open coverage under 10 seconds | pass | Exit 0; all rule cells open, current 0, no unclassified line; 1.48 seconds | `style_ledger.py status --repo ~/rust/cargo-liner` |
| After step 1b: summary contract | pass | Exit 0; all 10 keys present; `rules_total` 62 (two tier E rules excluded) | `style_ledger.py summary --repo ~/rust/cargo-liner --json` |
| Step 1c: types crate | pass | Selected **cargo-port**; types cells not current: `crates/cargo-port` 3,529; `tui_pane` 2,301; `cargo-mend` 1,681; `cargo-berth` 1,462; `cargo-handler` 699; `cargo-tile` 559; `cargo-berth-test-support` 71. Status universe: 42,552 cells | `style_ledger.py status --repo ~/rust/cargo-liner --json`, group cells by crate and family, count types not current |
| Bootstrap scoped units | pending | Expected modules on all crates, names on cargo-handler, types on cargo-port; values has no Units row | `/showrunner:add_style … --pilot modules-unit --pilot names-unit=cargo-handler --pilot types-unit=cargo-port` through cargo-liner's showrunner |
| Queue respects the real board | pending | Structure tickets first; no reserved file; unavailable board stops pilot | `style_plan.py queue --repo ~/rust/cargo-liner` after bootstrap |
| First proposal resolves its issue base | pending | No `no issue base for` line; project and return link resolve to cargo-liner's base | `style_proposals.py file --repo <style checkout> --proposals <current folder> --parent <current parent>` |
| Coverage report rows | pending | Header and one row per classified tier A–D rule | `coverage_report.py --repo ~/rust/cargo-liner` |

The recorded summary keys are `cells_current_pct`, `oldest_waiting`, `proposals`,
`rules_done`, `rules_total`, `stale`, `synced_at`, `synced_tip`,
`unclassified_rules`, and `waiting_to_merge_back`.

## Recall

Pass criterion: at least 95% recall on 20 hand-checked files per launched family,
against the union of one-rule findings using the same model.

| Family | Result | Numbers | Source command |
| --- | --- | --- | --- |
| modules | pending | Sample, family findings and one-rule denominator pending | One-rule reruns on 20 files already checked by modules-unit |
| names | pending | Sample, family findings and one-rule denominator pending | One-rule reruns on 20 files already checked by names-unit |
| types | pending | Sample, family findings and one-rule denominator pending | One-rule reruns on 20 files already checked by types-unit |

Per-rule recall and candidate file-local rules: pending.

## Hold time

Pass criterion: median claim to Clear under two hours, none over six hours.
Clear means cargo-berth `edit_blocking_status` Clear, rather than release.

| Result | Numbers | Source command |
| --- | --- | --- |
| pending | Sample size, median and maximum pending | cargo-berth board/journal history: claim time to first Clear |

## Quiet-branch hand-off

Pass criterion: a style checkpoint reaches main within four hours, with no
product merge between checkpoint and integration.

| Result | Numbers | Source command |
| --- | --- | --- |
| pending | Checkpoint, integration commit and elapsed time pending | `git log main` and ledger hand-off timestamps |

## Restart

Pass criterion: a style showrunner and one unit director resume mid-ticket
without a lost or doubled ticket; each checkpoint is integrated once.

| Result | Numbers | Source command |
| --- | --- | --- |
| pending | Started PIDs, ticket id, restart records and merge count pending | `/shutdown` for pilot-started sessions; startup `style_sync.py resume` reconcile lines; `git log main` for `Merge <slug> hand-off (<hash7>)` |

## Yielding

Pass criterion: no product unit waits on a style reservation.

| Result | Numbers | Source command |
| --- | --- | --- |
| pending | Observed product waits and pilot interval pending | cargo-berth board history and product units' reports |

Tickets per hour per unit: pending.

## Ticket-gate evidence

Pending: approved proposal non-mechanical edit passes both gates and reaches a
recorded checkpoint; an equivalent coverage-selected edit returns to repair.
Record ticket ids, gate output and checkpoint evidence.

## Phase 10 live evidence

Pending: standing refill, fast-forward synchronization, reservation target,
checkpoint release confirmation and merge-wait wake-up traces.

## /showrunner:style_report

Pending: live coverage header, classified rows, held proposals and hand-off counts.

## --from-child trace

Pending: parent receipt, review, merge, push, promotion and acknowledgment;
record the checkpoint, merge commit and reconcile evidence.

## Dailies

Pending: parent's saved style child summary and rendered style line.

## Fixes

- Pending: live-pilot fixes and their commit ids. The `--pilot` bootstrap and
  plan-scope reader are this dispatch's implementation, awaiting verification.
