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
| modules | FAIL | modules-unit-8 (cargo-tile, crate-level pass) found 21 of 36 one-rule findings on 20 files: 58.3%, 16 fully covered. By rule: module-roots 1/1, module-structure 10/12, types-live-with-their-behavior 4/6, name-submodules 3/5, when-to-split 2/4, split-by-type-ownership 1/8. Seven of the 15 misses are on four files where the unit held nothing. | Six fresh one-rule blind passes at the ticket base `d1ced22c`, scored against the unit's held findings in the ledger (read only) |
| names | FAIL | names-unit-6 and names-unit-11 (cargo-handler, 20 files) found 37 of 117 one-rule findings: 31.6%. By rule: comments-name-code-constructs 18/43, prefer-type-named-fields-and-bindings 18/66, name-bindings-to-match-parameters 0/6, dont-repeat-enum-domain-in-variant-names 1/1, spell-out-names 0/1; the two field-affix rules found nothing on the sample. The unit holds the first defects of a file and stops: misses cluster in long files (render.rs, accounts.rs, terminal.rs) and in test files. Each read file already has its own cell, so the gap is depth within a file, not files skipped. | Seven fresh one-rule blind passes at each ticket's commit (`d1ced22c`, `8d6e25d1`), scored against the unit's 67 held findings in the ledger (read only) |
| types | pending | Sample, family findings and one-rule denominator pending | One-rule reruns on 20 files already checked by types-unit |

Per-rule recall and candidate file-local rules: pending.

## Hold time

Pass criterion: median claim to Clear under two hours, none over six hours.
Clear means cargo-berth `edit_blocking_status` Clear, rather than release.

| Result | Numbers | Source command |
| --- | --- | --- |
| pass | 4 style reservations, 0 still open: median 1.06 h, maximum 1.41 h (modules-unit), none over 6 h | Read-only replay of the cargo-berth journal `claim`, `checkpoint` and `holder_merge_extent_observed` entries, 12:00Z to 15:09Z; Clear is Outstanding with an empty merge extent (`reservation/record.rs:423-430`) |

All four reservations were Clear before their release. Reservation-to-ticket links
are time correlations: the ledger has no reservation column on a ticket.

## Quiet-branch hand-off

Pass criterion: a style checkpoint reaches main within four hours, with no
product merge between checkpoint and integration.

| Result | Numbers | Source command |
| --- | --- | --- |
| fail | One hand-off, `28db0785` (Merge cargo-liner-style hand-off (8d6e25d) into main) at 15:09:24Z. It carried one new ticket checkpoint, names-unit-6's `c2a3160a` from 14:40:46Z: 0.48 h, under four hours, but tile-fixes-unit phase 21 merged to main at 14:55:35Z (`d2a10b35`) in between | `git log --first-parent --merges` on cargo-liner main; `git rev-list 28db0785^1..28db0785^2` |

The other 16 merged tickets record checkpoint `d1ced22c`, already on main before
the first ticket: they changed no code and carried nothing to hand off. From the
`style/main` aggregate commit `8d6e25d1` to main took 0.04 h. The ledger's
`handoffs` row still read `sent` after git showed the merge, and so did the
`f091ed48` row, with no merge commit recorded. cargo-liner's style showrunner
recorded both at 16:45:17Z; a read-only ledger check on 2026-10-17 shows
both rows `merged`, `8d6e25d1` with merge commit `28db0785` and `f091ed48`
with `ac95d94d`. Fix K makes the parent's merge record this itself.

The showrunner counts this as a fail and changes nothing until two more
hand-offs are measured. For each, record whether a product merge landed between
checkpoint and hand-off, and whether `style/main` took it in cleanly; the
criterion is then judged from three points.

| Hand-off | Ticket checkpoint | Elapsed | Product merge between | `style/main` took it in cleanly |
| --- | --- | --- | --- | --- |
| `28db0785` | names-unit-6 `c2a3160a` | 0.48 h | yes, `d2a10b35` | yes: `8d6e25d1` merges the ticket onto `d2a10b35`, and its tree equals the automatic merge |
| `ac95d94d` | names-unit-12 `96da4419` at 16:26:13Z, merged to main at 16:31:20Z | 0.09 h | no: main's last product merge, `ec3297e9`, landed at 15:16:13Z, before the checkpoint | yes: `f091ed48` merges the ticket onto `ec3297e9`, and its tree equals the automatic merge |
| third | pending | pending | pending | pending |

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
| partial | 12:00Z to 15:09Z: 0 recorded overlap answers naming a style holder, 0 recorded incursions naming a style blocker, 0 `ticket_yields` rows | Read-only scan of the 3,033 journal entries in the interval |

A refused check or claim is not journaled (`verb/check.rs:226-232`,
`verb/claim/mod.rs:1473-1477`, `ledger/handle.rs:748-765`), so a product unit
that waited and retried leaves no record. Zero recorded overlaps passes for what
is recorded; the criterion itself cannot be shown from records.

Open item the criterion waits on: cargo-berth journaling each refused check or
claim (requester, paths, holder), placed as cargo-liner berth-parallel-unit's
Phase 10, after its Phase 9. The yielding criterion can be measured once that
phase merges.

Tickets per hour per unit, merged tickets over first ticket created to last
merged: modules 7 in 2.41 h (2.90/h), names 6 in 1.88 h (3.19/h), types 4 in
0.98 h (4.08/h). At 15:09Z each unit held one assigned ticket (modules-unit-8,
names-unit-7, types-unit-5) and none waited in any other state.

16 of the 17 merged tickets carried no edit: their checkpoint is `d1ced22c`,
already on main, and only names-unit-6 changed code. The retirement call weighs
how many tickets changed code, not only how many merged.

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
nonzero hand-off counts remain pending; the first hand-off, `28db0785`, merged at
15:09Z.

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
- **Fix D — a ticket closes only when every cell it owns is judged** (commit `452c5aec`):
  modules-unit-4 (cargo-port structure, 10 rules) recorded verdicts for seven;
  `ticket merge` accepted it and refill reissued the other three as
  modules-unit-5 at 13:42:52Z. Cargo-port stayed closed to types-unit, which
  returned exit 3 again after its 09:44 EDT wake. The ledger now lists a ticket's
  owned cells with no verdict under it; the gate and `ticket checkpoint` refuse
  while any remain, naming each one. A ticket that drops a file on a conflict
  records it with `ticket yield`, which clears its verdicts and unfiled held
  findings there and leaves the file's cells for later work.
- **Fix E — tier and family** (commits `202614d8`, `1385595f`):
  `rules.py` makes a local tier A or E rule impossible: local requires a family,
  while A and E forbid one. A types-family tier D rule was also classified,
  though tier D belongs to modules alone. Settled with cargo-liner's style
  showrunner: E1 requires a family for B, C and D and forbids it for A and E,
  whatever the origin (E1 commit `202614d8`). E2 refuses tier D outside modules (`1385595f`),
  after cargo-liner re-tiered trait-tutorial-format as E (`a351f6be`) and moved
  readme-diagnostic-section to the modules family (`707bb4c6`).
- **Fix F — structure tickets list every finding** (commit `2c5e4d5f`, merged as `f71d71ae`):
  the crate pass held 2/12 known defects (16.7%); the file-by-file rerun held
  10/12 (83.3%) on the same four cargo-berth files, six pinned rules and commit.
  The cause was crate-level summarizing, not rule sharing. Structure Work Orders
  now judge file by file after reading each file's siblings and direct callers,
  hold every distinct defect separately and a cross-file defect once, and record
  crate verdicts only after every file is judged.
- **Fix G — refill waits for sync when the branch point is not in the synced tip** (commit `67ed4f25`):
  two docs commits pushed straight to cargo-liner `main` bypassed
  `merge_checkpoint`, so no sync notice went out and `next-phase` kept handing
  out tickets against a base the style branch had not absorbed. Settled with the
  showrunner: no git hook. Before selecting a new ticket, refill checks that the
  branch point's tip is an ancestor of the ledger's synced tip (ancestry, since a
  good sync merges the branch point and the tips then differ). When it is not,
  refill records a sync wait and exits 3 with `waiting for sync: <branch point>
  <tip7> is not in the synced tip <synced7>`. An assigned or checkpointed ticket
  still resumes or waits for merge.
- **Fix I — filing waits until the ticket that held a finding closes** (commit `5c8e823b`):
  approving 15d2bb1b would have filed every unfiled held finding, including
  those of modules-unit-8 and types-unit-7, whose tickets were still open and
  could still revise them. Filing now leaves the findings of an assigned or
  checkpointed ticket unfiled, prints `held for open ticket <id>: <count>
  findings` for each such ticket, and files them on the first run after the
  ticket closes.
- **Fix J — the ticket gate measures from the style branch the ticket absorbed** (commit `fb08135a`):
  modules-unit-8 was assigned at `d1ced22c` and fast-forwarded to style/main
  `8d6e25d1` with no edit of its own. Its gate still exited 1 with `non-Rust
  change requires review`: the four docs commits pushed straight to main sit on
  the first-parent chain, and the footprint fold removes only merged-in side
  parents. The coverage gate now measures from the unit's merge-base with the
  style merge branch when the recorded start is its ancestor, and from the
  recorded start otherwise (no style branch, unresolvable, or unrelated
  history). The ledger's recorded base is unchanged, and the ticket's own edits
  are still judged.
- **Fix K — the parent's hand-off merge records itself in the style ledger** (commit `b5bf8336`):
  hand-offs `8d6e25d1` (merged as `28db0785`) and `f091ed48` (merged as
  `ac95d94d`) both left their handoffs rows `sent` with no merge commit. The
  row became merged only when the style showrunner acted on the parent's
  acknowledgment message. The first acknowledgment reached a stale session, and
  the second has no delivery record. The parent's merge now runs
  `style_ledger.py handoff merged` with the merge commit before it
  acknowledges, both on the fresh merge and on a repeated notice. The
  acknowledgment is now a notice only. A ledger failure prints one `hand-off
  ledger:` line, and the merge and acknowledgment still go through.

- **Fix L — modules tickets own small file batches, each file with its own verdict cell** (commit `23fd3edf`):
  the modules recall sample above found 21 of 36 defects, and 7 of the misses
  sat on files where the unit held nothing: a modules ticket owned a whole
  crate, and the ledger asked for one crate verdict per rule, so nothing made
  the worker judge every file. Modules rules now have one verdict cell per
  file (the workspace-dependencies rules keep one per crate). A new modules
  ticket owns a batch of at most 6 files and 3000 lines, directories kept
  together, and its gate refuses the ticket while any file lacks a verdict.
  The Work Order lists the batch, the cells it owns and the findings already
  held on its files. Tickets opened before this change keep their crate
  cells until they merge. Every crate's modules files start unjudged, so the
  other families wait on each crate's batches; fix M orders the pilot crates
  first.

- **Fix M — modules takes first the crates that block a pilot unit** (commit `22fe6550`):
  the types unit (pilot scope cargo-port) and the names unit (cargo-handler)
  both waited on those crates' structure cells while refill handed modules
  tui_pane, which sits in neither scope but held the most pending cells of
  other families. A pending cell now counts toward a crate's place in the
  structure order only when its family has a unit whose pilot scope holds
  that crate, and a family with no unit counts nothing. Each unit's scope is
  read from its own plan copy. `queue` shows the same order refill uses.

- **Fix N — refill names the crates whose structure cells hold a unit back** (commit `425c27e2`):
  while modules worked through the pilot crates, the types and names units
  reported a bare `waiting for sync`, which reads as a missing sync and not
  as work queued behind modules. A unit other than modules with nothing to
  take now prints `waiting for structure: <crates>`, listing each in-scope
  crate that holds both a pending structure cell and a pending cell of its
  own family. It still exits 3 and still registers its sync wait, so a
  modules merge wakes it as before.

- **Windowed names recall sample:** the same 20 cargo-handler files were
  judged again by two blind family judges, all seven names rules at once,
  as a names ticket does, with one change: each file was read in windows of
  at most about 300 lines, and each window's findings were written down
  before the next window opened. Recall rose from 37/117 (31.62%) to 100/117
  (85.47%). comments-name-code-constructs rose from 18 to 38 of 43, and
  prefer-type-named-fields-and-bindings from 18 to 61 of 66. The misses in
  long files and test files are gone. name-bindings-to-match-parameters
  stayed at 0 of 6 and spell-out-names at 0 of 1: windows fix how deep a
  judge reads, not whether it traces a binding into the parameter it feeds.
  Three of the five comment misses left are module docs at lines 1–3. Fix O
  makes windows the read-ticket procedure; a dedicated name-bindings pass
  follows it.

Fixes A and B are merged to `~/.claude` main as `b8b8e74c`, fix G as `31e7bae8`,
fix I as `89f24e01`, fix J as `9b71ca5b`, fix K as `598232d8`, fixes L and M as
`a8ae4d2a`.
Live verification of the fixes and subsequent measurements remain pending.
