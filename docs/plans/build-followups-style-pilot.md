# Style showrunner pilot — cargo-liner

Overall result: **pending**. Bootstrap is recorded below; operational measurements
and the recall study remain pending. Phases 18 and 19 require the
completed pilot criteria. No live repository or vault was changed by this dispatch.

Recorded evidence: `/tmp/claude/unit/24e20a41-688b-4fff-9f5d-d243a63ee4e2/pilot_evidence.md`.
Bootstrap evidence also comes from the Phase 17 dispatch and its saved
`pilot/queue.txt` and `pilot/coverage.txt` files in the same session directory.
Pending entries have no measurement yet.

## Real-repo checks

| Check | Result | Numbers / evidence | Source command |
| --- | --- | --- | --- |
| Before step 1: missing Proposals refused | pass | 2026-10-17 07:39 EDT; exit 2, `add_style: production doc lacks Proposals` | From `/home/natepiano/rust/cargo-liner`: `add_style.py --production /home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md --model claude-opus-5-5 --effort high --check` |
| After step 1b: rule classification | pass | Main `f65032bb`, about 07:43 EDT; exit 0, 64 rules, 0 unclassified; 11 local rows tagged | `rules.py list --repo ~/rust/cargo-liner` |
| After step 1b: initialize ledger | pass | Exit 0 at main `f65032bb…` | `style_ledger.py set-tip --repo ~/rust/cargo-liner --tip f65032bb…` (abbreviated recorded tip) |
| After step 1b: open coverage under 10 seconds | pass | Exit 0; all rule cells open, current 0, no unclassified line; 1.48 seconds | `style_ledger.py status --repo ~/rust/cargo-liner` |
| After step 1b: summary contract | pass | Exit 0; all 10 keys present; `rules_total` 62 (two tier E rules excluded) | `style_ledger.py summary --repo ~/rust/cargo-liner --json` |
| Step 1c: types crate | pass | Selected **cargo-port**; types cells not current: `crates/cargo-port` 3,529; `tui_pane` 2,301; `cargo-mend` 1,681; `cargo-berth` 1,462; `cargo-handler` 699; `cargo-tile` 559; `cargo-berth-test-support` 71. Status universe: 42,552 cells | `style_ledger.py status --repo ~/rust/cargo-liner --json`, group cells by crate and family, count types not current |
| Bootstrap scoped units | pass | 2026-10-17; check and create both exit 0; `style/main` from `d1ced22c`; names, types and modules rows only; four sessions running, no values session | Cargo-liner's showrunner: `add_style.py --production /home/natepiano/rust/cargo-liner/docs/cargo-port/remove-running-section-production.md --model claude-opus-5-5 --effort xhigh --pilot modules-unit --pilot names-unit=cargo-handler --pilot types-unit=cargo-port`, first with `--check`, then without |
| Queue respects the real board | partial | Exit 0 in 33.9 s (saved timing 33.953 s); seven structure tickets, then one values listed ticket; no unavailable-board error. The reserved-file check is unverified from the saved file and waits on a live board read. | `style_plan.py queue --repo ~/rust/cargo-liner`; saved `pilot/queue.txt` |
| First proposal resolves its issue base | pending | No `no issue base for` line; project and return link resolve to cargo-liner's base | `style_proposals.py file --repo <style checkout> --proposals <current folder> --parent <current parent>` |
| Coverage report rows | pass | Exit 0 in 1.39 s (saved timing 1.386 s); 62 rows: A 14, B 20, C 14, D 14; 64 catalog rules less E 2 | `coverage_report.py --repo ~/rust/cargo-liner`; saved `pilot/coverage.txt` |

The bootstrap created checkout `/home/natepiano/rust/cargo-liner-style` and
`docs/cargo-liner-style-production.md` inside it. The Units table contains
`names-unit`, `types-unit` and `modules-unit`. Before the first ticket, the names
plan at `docs/style-production/units/names-unit.md:27` carried
`- **Pilot scope:** cargo-handler`; the types plan at `types-unit.md:31` carried
`- **Pilot scope:** cargo-port`; modules carried no scope line. Running sessions
were `cargo-liner-style`, `cargo-liner-style-modules`, `cargo-liner-style-names`
and `cargo-liner-style-types`; there was no values-unit session. These are the
bootstrap observations supplied in the dispatch; scoped ticket assignments
beyond the first modules ticket remain pending.

The saved queue's structure order was `crates/cargo-berth` (118 files),
`crates/cargo-berth-test-support` (6), `crates/cargo-handler` (42),
`crates/cargo-mend` (160), `crates/cargo-port` (289), `crates/cargo-tile` (37),
and `crates/tui_pane` (206). Its last ticket was values-unit, listed, crate `.`,
rule `global:rust/always-use-nextest.md`, with the single file
`docs/cargo-handler/as-built/hierarchy.md`. Queue is an unscoped preview across
all four families, so this values ticket does not indicate a launched values
unit. The queue read cargo-liner's board, rather than this phase worktree's
unconfigured board. It returned tickets after the live-board exclusion filter;
the saved file contains ticket paths and timing, with no board status or
reservation list to independently reconstruct the excluded paths.

The first ledger ticket was `modules-unit-1`, kind `structure`, crate
`crates/cargo-berth`, base `d1ced22c` (dispatch evidence). Names/types first
assignments and the out-of-scope approved-proposal case remain pending.

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

The Fix C dispatch records names-unit returning exit 3 (`waiting for sync`)
at about 08:31 EDT, then returning exit 3 again after the `d1ced22` sync woke
it at about 08:38 EDT: cargo-handler's structure cells were still open.
Modules-unit-3 merged at 13:12:53Z; the style showrunner woke names-unit by
hand about a minute later, and names-unit-1 was assigned at 13:14:47Z.
Names waited about 44 minutes, with the crate free for about two minutes.
Types-unit (cargo-port) returned exit 3 from about 08:31 EDT.
Modules-unit-4 merged in the ledger at 13:42:05Z; the hand wake came at
09:44 EDT, about two minutes after the merge and about 73 minutes after
its first exit 3. These are dispatch observations; verification of the
scripted wake remains pending.

## /showrunner:style_report

The saved `coverage_report.py --repo ~/rust/cargo-liner` output exited 0 in
1.39 seconds and printed 62 classified rows. Its header was:

```text
cargo-liner-style — synced main f65032b at 08:28 EDT, 948 files
Rules done: 0 of 62 (0 by lint) · cells current 0% · open 42,552 · stale 0 · awaiting merge 0 · held 0
Waiting to merge back: none
```

Source: saved `pilot/coverage.txt`. The old sync tip is the defect described in
Fix A below. The slash-command invocation itself, later held proposals and
nonzero hand-off counts remain pending.

## --from-child trace

Pending: parent receipt, review, merge, push, promotion and acknowledgment;
record the checkpoint, merge commit and reconcile evidence.

## Dailies

Pending: parent's saved style child summary and rendered style line.

## Fixes

- **Fix A — sync at the style branch's commit** (commit `06b5f03b`):
  the pre-bootstrap ledger retained `f65032bb` while main, style/main and the
  first ticket base were `d1ced22c`. Creation now refreshes a missing or mismatched
  sync row to the style branch's actual commit and this production's style
  branch; an exact match is untouched. A retry keeps the existing branch in
  place even if the branch point has advanced. Reconcile initializes a
  missing/different stored style branch at that branch's commit, falling back
  to the branch-point tip only if the ref is absent, and leaves a matching
  branch's synced tip alone.
  Neither path writes lint results; tier A still needs the next lint sync.
- **Fix B — structure first where it unlocks the most work** (commit `adb480b0`):
  alphabetical structure selection took cargo-berth while scoped names and types
  units waited for cargo-handler and cargo-port. Modules now orders structure
  crates by other-family open/stale, non-D cells blocked, descending, then stale
  count descending and crate name. No-structure crates follow in their previous
  order; other families and pilot scope filtering keep their behavior. Counts
  come from the existing cells traversal, without another ledger read.
- **Fix C — ticket merges wake units waiting for sync** (commit `87a22ef5`):
  names-unit stayed blocked after cargo-handler's structure ticket merged and
  needed the hand wake recorded above; types-unit also needed a hand wake
  about two minutes after modules-unit-4 merged, after waiting about 73 minutes.
  The ledger now records sync waits, refill clears them when work resumes, and
  `ticket merge` sends each waiter `ticket <id> merged; refill.`. Only successful
  delivery clears a wait; failed delivery keeps it for the next merge, including
  an idempotent retry. No-change tickets use the same merge command.
  The first draft addressed the unit by name, which the roster does not resolve;
  this was caught before review and replaced with its director's roster address.
  Review found two wake races: a merge between refill's look and its wait, and
  an older wake clearing a newer wait. Refill now looks again after recording
  its wait, and each wake acknowledges only its per-wait token.
- **Fix D — a ticket closes only when every cell it owns is judged** (commit `pending`):
  modules-unit-4 (cargo-port structure, 10 rules) recorded verdicts for seven;
  `ticket merge` accepted it and refill reissued the other three as
  modules-unit-5 at 13:42:52Z. Cargo-port stayed closed to types-unit, which
  returned exit 3 again after its 09:44 EDT wake. The ledger now lists a ticket's
  owned cells with no verdict under it; the gate and `ticket checkpoint` refuse
  while any remain, naming each one. A ticket that drops a file on a conflict
  records it with `ticket yield`, which clears its verdicts and unfiled held
  findings there and leaves the file's cells for later work.
- **Fix E — tier and family** (commit `pending`):
  `rules.py` makes a local tier A or E rule impossible: local requires a family,
  while A and E forbid one. A types-family tier D rule was also classified,
  though tier D belongs to modules alone. Settled with cargo-liner's style
  showrunner: E1 requires a family for B, C and D and forbids it for A and E,
  whatever the origin (E1 commit `202614d8`). E2 refuses tier D outside modules;
  it is held until cargo-liner re-tiers trait-tutorial-format as E.

Fixes A and B are merged to `~/.claude` main as `b8b8e74c`.
Live verification of the fixes and subsequent measurements remain pending.
