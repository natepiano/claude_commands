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
| Queue respects the real board | pass | Exit 0 in 33.9 s (saved timing 33.953 s); seven structure tickets, then one values listed ticket; no unavailable-board error. Live board read on 2026-10-18: 321 reservations, 7 of them blocking edits. The queue listed no reserved file, and it gave the same tickets with and without the board. No blocking reservation covered a file the queue would list, so the skip itself ran only in tests. A skipped file gets no line in the queue output. | `style_plan.py queue --repo ~/rust/cargo-liner`; saved `pilot/queue.txt`; read-only board comparison on a ledger snapshot |
| First proposal resolves its issue base | pass | First filed issue, 2026-10-17 12:52Z, `style crate 825516b4.md` (a root `Cargo.toml` finding from modules-unit-1): project `cargo-liner`, return link `issues - cargo-liner.base`. All 279 style issues name a base that exists: a crate with its own base files there (cargo-port 228, cargo-berth 1, cargo-mend 2), and the rest file under cargo-liner (48). Two defects in the filed text, recorded under Fix AH below | `style_proposals.py file --repo <style checkout> --proposals <current folder> --parent <current parent>` |
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
`crates/cargo-berth`, base `d1ced22c` (dispatch evidence). The first names
ticket was `names-unit-1` at 13:14Z, crate `crates/cargo-handler`; the first
types ticket was `types-unit-1` at 13:59Z, crate `crates/cargo-port`. Both had
base `d1ced22c`. All 45 names tickets and all 102 types tickets stay in their
scope crate, and every file they list is inside it. Modules, with no scope
line, took all seven crates. The one approved proposal, `style cargo-handler
30d569f3.md`, is inside the names scope and was built as `names-unit-12`. No
proposal outside a scope was approved during the pilot, so the live run did
not reach `waiting <issue>: outside pilot scope`. Phase 16's tests cover that
branch.

The recorded summary keys are `cells_current_pct`, `oldest_waiting`, `proposals`,
`rules_done`, `rules_total`, `stale`, `synced_at`, `synced_tip`,
`unclassified_rules`, and `waiting_to_merge_back`.

## Recall

Pass criterion: at least 95% recall on 20 hand-checked files per launched family,
against the union of one-rule findings using the same model.

| Family | Result | Numbers | Source command |
| --- | --- | --- | --- |
| modules | PASS | modules-unit-8 (cargo-tile, crate-level pass) found 21 of 36 one-rule findings on 20 files: 58.3%, 16 fully covered. By rule: module-roots 1/1, module-structure 10/12, types-live-with-their-behavior 4/6, name-submodules 3/5, when-to-split 2/4, split-by-type-ownership 1/8. Seven of the 15 misses are on four files where the unit held nothing. Re-sample with fix L's batches (one blind judge per batch): 30 of 36, 83.3%; split-by-type-ownership 7/8, module-structure 9/12, when-to-split 2/4 (`resample_modules_table.md`). Two blind judges per batch, findings combined: 32 of 36, 88.9%, still below the bar; module-structure 11/12, when-to-split 2/4. The four defects neither judge found sit in the three largest files, two of them when-to-split (`resample2_modules_table.md`). Fix AC corrects the reference to 35 and asks the when-to-split question of every file. After it, one blind judge per batch finds 34 of 35, 97.1%, which meets the bar. The one miss is a reciprocal sibling dependency in birth_stamp (split-by-type-ownership). A blind check rules all 15 findings outside the reference real (`rerunac_modules_*.json`). | Six fresh one-rule blind passes at the ticket base `d1ced22c`, scored against the unit's held findings in the ledger (read only) |
| names | PASS | names-unit-6 and names-unit-11 (cargo-handler, 20 files) found 37 of 117 one-rule findings: 31.6%. By rule: comments-name-code-constructs 18/43, prefer-type-named-fields-and-bindings 18/66, name-bindings-to-match-parameters 0/6, dont-repeat-enum-domain-in-variant-names 1/1, spell-out-names 0/1; the two field-affix rules found nothing on the sample. The unit holds the first defects of a file and stops: misses cluster in long files (render.rs, accounts.rs, terminal.rs) and in test files. Each read file already has its own cell, so the gap is depth within a file, not files skipped. Re-sample with the current read-ticket text (fixes O to X, two blind judges, windows): 63 of 117, 53.8%, below the 100 of 117 a windowed pass reached before fixes P to X; type-named bindings 30/66 and comments 29/43 fell as the name-bindings paragraph drew the judges (`resample_names_table.md`). Rerun 7, after fixes Y and Z, on the six rules left in the names ticket (111 findings; name-bindings moved to its own ticket and met its own bar, 36 of 37): runs of 104, 99 and 104, a median of 104 of 111, 93.7%; comments 36 to 40 of 43, type-named 62 to 64 of 66 (`rerun7_names_table.md`). Two runs' findings together reach 108 to 109 of 111 in every pairing, and a blind re-judging of every finding outside the reference found 0.6% to 0.8% of a two-run union false, against a bar of 10% (`rerun7_names_precision_table.md`); fix AA gives the names read ticket two blind judges, so the family passes on that union. | Seven fresh one-rule blind passes at each ticket's commit (`d1ced22c`, `8d6e25d1`), scored against the unit's 67 held findings in the ledger (read only) |
| types | PASS | types-unit (cargo-port, 20 files) found 50 of 61 in-scope one-rule findings: 82.0% (50 of 71, 70.4%, counting ten `pub` items of a binary crate that the rule exempts). By rule: enums-over-bool-for-owned-booleans 39/43, if-else-chains-signal-missing-types 7/13, prefer-from-impl-over-named-constructors 3/4, public-failures-return-named-errors 1/11; the other three rules found nothing on the sample, and the five Bevy rules cannot apply to cargo-port. Re-sample with the current read-ticket text (fixes O to X, two blind judges, windows): 55 of 61, 90.2%; owned bools 43/43, if-else 8/13, From 3/4, public failures 1/1, and none of the ten out-of-scope `pub` sites flagged (`resample_types_table.md`). Rerun 7, after fixes Y and Z: 59 of 61, 96.7%, which meets the bar; if-else 12/13, From 3/4 (`rerun7_types_table.md`). | Seven fresh one-rule blind passes at the commits the unit judged (`d2a10b35`, `28db0785`, `ec3297e9`), scored against the unit's held findings in the ledger (read only) |

### Per-rule recall

The last pass of each family, one row per rule that has one-rule findings.

| Family | Rule | Found | Recall |
| --- | --- | --- | --- |
| modules | module-roots-as-table-of-contents | 1 of 1 | 100% |
| modules | module-structure | 12 of 12 | 100% |
| modules | name-submodules-after-anchor-types | 5 of 5 | 100% |
| modules | split-by-type-ownership | 6 of 7 | 85.7% |
| modules | types-live-with-their-behavior | 6 of 6 | 100% |
| modules | when-to-split-a-module | 4 of 4 | 100% |
| names | comments-name-code-constructs | 36 to 40 of 43 | 83.7% to 93.0% |
| names | prefer-type-named-fields-and-bindings | 62 to 64 of 66 | 93.9% to 97.0% |
| names | dont-repeat-enum-domain-in-variant-names | 1 of 1 | 100% |
| names | spell-out-names | 0 of 1 | 0% |
| names | name-bindings-to-match-parameters (own ticket) | 36 of 37 | 97.3% |
| types | enums-over-bool-for-owned-booleans | 43 of 43 | 100% |
| types | if-else-chains-signal-missing-types | 12 of 13 | 92.3% |
| types | prefer-from-impl-over-named-constructors | 3 of 4 | 75.0% |
| types | public-failures-return-named-errors | 1 of 1 | 100% |

Names ranges are the three single-judge runs of rerun 7. The names read
ticket runs two blind judges (fix AA), and every pairing of two runs finds 108
or 109 of 111. No family is shrunk: each family passes as a whole. The rules
under 95% have few findings each, except comments-name-code-constructs, whose
misses do not repeat between judges. The one names miss that every run repeats
is `CONFIG_DIRNAME` in `constants.rs` (spell-out-names).

### Candidate file-local rules

Thirteen rules judge one file at a time, but they read `input_scope: crate`.
So an edit anywhere in a crate makes every one of their verdicts in that crate
stale. On cargo-liner this reissued cells that were already judged and
unchanged: names-unit-33 relisted 17 forbidden-word sites that five earlier
tickets had judged clean. The 13 are `forbidden-words`,
`dont-repeat-type-name-in-fields`, `dont-repeat-enum-domain-in-variant-names`,
`avoid-repeated-field-affixes`, `spell-out-names`, `prefer-events-over-messages`,
`extract-observer-run-conditions`, `enums-over-bool-for-owned-booleans`,
`if-else-chains-signal-missing-types`, `prefer-from-impl-over-named-constructors`,
`name-bindings-to-match-parameters`, `prefer-type-named-fields-and-bindings` and
`comments-name-code-constructs`. The change sets `input_scope: file` on each,
in `~/rust/nate_style`. It waits on the user's go.

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
| `8f9e8260` | names-unit-15 `82493d60` at 05:01:50Z, merged to main at 05:15:25Z | 0.23 h | no: main's last product merge, `d0d35e1b`, landed at 04:43:22Z, before the checkpoint | yes: `d9795213` merges the ticket onto `d0d35e1b`, and its tree equals the automatic merge |

Judged from three points: every hand-off reached main within an hour, and
`style/main` took each in cleanly. Two of three had no product merge between;
the one that did merged without conflict. The criterion passes.

## Restart

Pass criterion: a style showrunner and one unit director resume mid-ticket
without a lost or doubled ticket; each checkpoint is integrated once.

| Result | Numbers | Source command |
| --- | --- | --- |
| pass, one defect | The style showrunner restarted at 15:51Z (modules-unit-8 open), 18:32Z (modules-unit-14 open) and 04:38Z. The units restarted after the account move at 04:33Z. names-unit-15 was open across it, then checkpointed as `82493d60` and merged once as `8f9e8260`. No ticket was lost or doubled. On main's first-parent line, 8 hand-off merges carry 8 distinct checkpoints and match the 8 ledger rows. One checkpoint, `0c01c6c`, was merged twice; see Fix AI | Ledger `tickets` and `handoffs`; `git log main --grep hand-off`; showrunner and unit transcripts |

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

Pass. names-unit-12 is the approved proposal `style cargo-handler 30d569f3.md`.
Its edit is not mechanical. It passed the candidate gate as `approved proposal`
at 16:18:26Z on 2026-10-17. The 411 cargo-handler tests passed at 16:23:33Z.
It passed the final gate at 16:24:04Z, and the scope audit matched the nine
approved paths. It checkpointed as `96da4419` with 9 `fixed` judgments, and it
reached main as `ac95d94d` through hand-off `f091ed48`.

A coverage-selected edit went back to repair. names-unit-24 got gate exit 1 at
09:49:38Z on 2026-10-18 (`review: check 6`). Its director reverted two test
files and kept the four renames. The rerun passed as `mechanical` at
09:49:47Z, checkpointed as `4f5b749d`, and reached main as `48d41365`.
modules-unit-19 exited 1 with `unjudged:` and was dropped. names-unit-21 was
held, then failed a merge test, then merged as `0c01c6c9`.

## Phase 10 live evidence

Partial. Seen live:

- Refill exit 3 many times: `waiting for sync`, `waiting for structure:
  crates/cargo-handler` (17:43:43Z), and `main 5db7f52 is not in the synced tip`.
- Refill exit 5 (`waiting for merge`) five times in names.
- The scripted sync notify reached the style showrunner nine times, from
  12:33Z on 2026-10-17 to 12:18Z on 2026-10-18.
- Scripted merge wakes (`ticket <id> merged; refill.`) reached names for 161
  tickets, types for 12 and modules for 3. One wake to names, at 17:58:06Z,
  did not arrive.
- One refused `--ff-only`, in names at 06:52:06Z on 2026-10-18. Its director
  merged with `--no-ff` as `54a9f18d`.

Not seen live: refill exit 4 (board not available), and a lint step that exits
1 while the run continues. All 39 recorded lint runs passed. The style
showrunner ran the fast-forward steps by hand, not as a script.

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
Fix A below.

At 17:30 EDT on 2026-10-18, the same script ran on a snapshot of the live
ledger. It exited 0 in 10.3 seconds and printed 61 rows: A 14, B 20, C 14,
D 13. Its header was:

```text
cargo-liner-style — synced main 22a2514 at 16:23 EDT, 959 files
Rules done: 14 of 61 (14 by lint) · cells current 31.7% · open 30,173 · stale 3,874 · awaiting merge 0 · held 260
Waiting to merge back: none
```

The held count is now nonzero. No hand-off waited at that time, so the
awaiting-merge count was 0 again; eight hand-offs had merged (see Restart).
The script ran on a snapshot because it saves its last-report comparison
beside the ledger. The slash command itself runs only in a showrunner
session, and this report did not run it there.

## --from-child trace

Pass. All 8 hand-offs went through `--from-child`. The trace of hand-off
`852380e8` (UTC, 2026-10-18):

| Step | Time | Evidence |
| --- | --- | --- |
| Style side merges the checkpoint | 09:54:47Z | `852380e8` on style/main |
| Parent receives the hand-off | 09:55:03Z | parent transcript |
| Parent reviews the diff | 09:55:08Z | parent transcript |
| `merge_checkpoint.py --from-child` starts | 09:55:14Z | log: tests ok, 0 red |
| Push | before 10:02:42Z | `48d41365` on origin main |
| Promotion | — | `promote: ok — no promotion configured` |
| Scripted notify, ledger row merged | 10:02:42Z, 10:02:46Z | ledger `handoffs` |
| Acknowledgment | 10:02:52Z scripted, 10:03:07Z by hand | style showrunner transcript |
| Style side fast-forwards, sets the tip | 10:03:36Z | `852380e8..48d41365` |

## Dailies

Not run. Both productions registered `on demand: no scheduled dailies`, and no
dailies run happened during the pilot. The parent's last saved dailies are from
2026-10-16 and carry no style entry. This criterion needs one dailies run on
demand.

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
  held on its files. A ticket opened before this change records crate
  verdicts, which cover none of the new file cells, so its crate's files
  are judged again in batches; each batch lists the findings it already
  holds. Every crate's modules files start unjudged, so the
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

- **Fix O — read tickets judge each file in line windows** (commit `41cb588e`):
  the windowed names sample showed depth within a file was the gap, so a
  read ticket's Work Order now lists each file's line windows, computed
  from the file at the ticket's base: at most 300 lines each, balanced,
  with the file's test module in windows of its own. The Spec tells the
  worker to judge window by window, every rule, line by line, starting a
  file with its module docs, to hold each window's findings before opening
  the next, and to record a file's verdicts after its last window. The
  test module is found past any attributes or visibility between
  `#[cfg(test)]` and `mod`, which about half the workspace's test modules
  carry. Structure, listed and proposal tickets are unchanged.

- **Types recall sample:** seven blind one-rule judges, one per types rule,
  read 20 cargo-port files the types unit had judged, whole files, no
  windows. The unit held 50 of their 71 findings (70.4%). Owned booleans
  hold up (39 of 43), and the unit found sites the judges passed over.
  If/else chains are the weak rule (7 of 13): half the misses are one
  decision taken again at several points in a file, the rest short chains
  over an unnamed set of states. Public failures turns on scope: cargo-port
  is a binary crate, so none of its `pub` functions is reachable from
  outside it, and the rule exempts functions not public outside the crate.
  The judges read `pub` as public and flagged ten send-and-discard sites;
  the unit read the exemption and held one. Read the binary-crate way,
  recall is 50 of 61 (82.0%). The showrunner settled that reading: the
  rule's own words exempt the ten, so 50 of 61 is the score, and fix P
  states a binary crate's scope in its Work Order. The misses are not deep in
  long files, but these findings came from read tickets, so the sample is
  re-judged once fix O's windows land.

- **Dedicated name-bindings pass:** one blind judge, holding only the
  name-bindings rule, read the 20 cargo-handler files in fix O's windows
  and traced every argument that comes from a local binding into the
  parameter it fills. It caught 3 of the 6 one-rule findings (50%), and
  23 more the one-rule judge missed, mostly test fixtures named for where
  they came from (`state` filling `natedev`, `root` filling `row`). It
  traced all three misses and dropped them. Two are a binding that fills
  several parameters with different names (`text` in settings.rs, `cell`
  in footer_color_tests.rs), where no one name fits and the one-rule fix
  adds a new binding the rule does not ask for; read that way the score is
  3 of 4 (75%). The third is a real miss: a binding passed through a `From`
  conversion takes the outer parameter's name, as the rule's own example
  passes through `map_or`, but the judge stopped at the conversion's own
  parameter name. The showrunner settled the first reading: a binding
  that fills differently named parameters is out of scope, so the score is
  3 of 4. Four findings are too few to call pass or fail; the rule text is
  right and only the judges miss the conversion case, so a names ticket's
  Work Order states it, and the pass runs again on a sample holding at
  least 20 in-scope findings.

- **Fix P — a binary crate's Work Order says its `pub` items are not
  public** (commit `d6818851`): the types sample showed judges reading
  `pub` as public in a crate with no library target. The planner now reads
  the crate's manifest and `src/lib.rs` at the ticket's base, and a ticket
  for a package with no library target states that none of its items is
  public outside the crate. A crate with a library, and a workspace root
  with no package of its own, get no line.

- **Fix R — a name-bindings ticket says a conversion passes through**
  (commit `8dab0170`): the dedicated name-bindings pass stopped at a `From`
  impl's own parameter name. A ticket that pins the name-bindings rule now
  states that a conversion between the binding and the call (`map_or`,
  `From::from`, `.into()`) passes through, so the binding is named after
  the parameter the converted value fills. Tickets without that rule are
  unchanged.

- **Fix Q — a file opens to other families once its own structure cells
  are judged** (commit `0710fb55`): a pending structure cell closed its
  whole crate to the names and types units, so they waited on the modules
  unit's slowest file. Now only the files with a pending structure cell
  stay closed, and a pending crate-wide structure cell still closes the
  crate. The other families refill from the crate's open files, and wait
  for structure only when every cell they have left is on a closed file.
  The modules unit's own selection is unchanged.

- **Name-bindings rerun:** 120 cargo-liner files in six groups of 20, read
  in fix O's windows. For each group, one blind judge held the rule alone and
  another held the rule, fix R's conversion line and the trace. The traced
  pass caught 34 of the 38 in-scope one-rule findings (89.5%), under the 95%
  bar, and found many sites the one-rule pass missed. Out of scope, as for
  the first pass: a binding that fills differently named parameters, a
  general-purpose library parameter (`Path::join(path)`, `fs::write(path)`,
  `Write::write_all(buf)`), a name already bound in the scope, a conversion
  that ends in a struct field or return value, a value built from the
  binding (a `format!` message, an enum variant holding it), and one element
  filling a plural collection parameter. Fix R held for `From`, `into` and
  `map_or`; the four losses are two other gaps. Two are conversions the judge
  did not pass through: an accessor (`temp.path()`) and a method that returns
  the value in another form (`.to_local_def_id()`). Two are bindings dropped
  as filling several parameters when they fill one: a second loop that binds
  the same name, and a value derived from the binding
  (`directory.path().join("config")`). Fix S states both in the ticket.

- **Fix S — a name-bindings ticket names accessors and counts one value per
  target** (commit `96395dde`): fix R's conversion line now also passes
  through an accessor (`.path()`, `.as_str()`) and a method that returns the
  same value in another form (`.to_local_def_id()`). It says a binding fills
  several parameters only when that same value fills them: a value derived
  from it, or a later binding of the same name, is another value. Tickets
  without the name-bindings rule are unchanged. A second rerun of the traced
  pass on the same 120 files, under the same scope reading, measures it
  against the 95% bar.
- **Name-bindings rerun 2 — fix S misses the bar**: 33 of the 38 settled
  findings caught (87%), against 34 (89%) in the first rerun; the bar allows
  one loss. Three earlier losses are now caught (`visit.rs:37`,
  `target.rs:404`, `unused_pub.rs:5`), and four earlier catches are lost.
  Two come from the traced pass's own exclusions: a call inside
  `tracing::trace!` (`disk_usage.rs:153`) and a value wrapped in an enum
  variant (`dispatch.rs:803`). One is a later binding inside a loop read as
  a collision when it would only shadow (`refresh.rs:403`). One is the
  several-targets drop, reached now that `.path()` passes through
  (`presentation.rs:103`). The type-prefix drop at `actions.rs:835` holds as
  in the first rerun. The showrunner ruled `presentation.rs:103` out of
  scope, so the settled reference is 37 findings: both reruns caught 33 of
  37 (89.2%).
- **Fix T — a name-bindings ticket follows macros and enum variants and
  tells shadowing from collision** (commit `3b422e81`): the ticket line now
  says a call inside a macro's arguments counts, an enum tuple variant
  wrapping the binding (`Pane::App(id)`) passes through like a conversion,
  and a same-named binding declared later in an inner block only shadows
  the renamed binding, so it is no collision. That answers three of the four
  rerun 2 losses; `actions.rs:835` stays a miss, so the best a third rerun
  can reach is 36 of 37 (97.3%). A third rerun on the same files measures it.
- **Name-bindings rerun 3 — fix T meets the bar**: 36 of the 37 settled
  findings caught (97.3%), the best fix T allows. The three rerun 2 losses
  it targeted are caught (`disk_usage.rs:153`, `refresh.rs:403`,
  `dispatch.rs:803`); `actions.rs:835` stays the one miss, as ruled. The
  pass raised 375 findings against 346 in rerun 2. That rise is net: 57
  findings are new at their place and 26 earlier ones dropped out. A blind
  check of the 57 against the rule decides whether the new ones hold, under
  the same 10% bar.
- **Rerun 3 false-positive check — fails the 10% bar**: three blind judges,
  reading only the rule, called 16 of the 57 new findings false (28%); one
  judge rejected every finding whose change adds a binding, and the other
  two accepted 8 of that shape, so read consistently 24 of 57 are false
  (42%). Twenty-two of them propose adding a binding instead of renaming
  one: naming an inline expression (a literal path, a slice, an array, a
  setting's `.get()`) or opening a scoped alias that keeps the old name.
  The rule only renames a binding that already feeds the parameter. Part
  of that shape came from the measurement itself: since the first run the
  pilot's traced procedure told judges to count an inline expression the
  change would bind first, a sentence production tickets never carried.
  Two more are single cases: fix T's shadowing sentence let a rename hide
  an inner binding that the loop compares with the renamed one
  (`local_use.rs:96`), and fix S's accessor clause let a lookup of another
  value pass as a conversion (`provenance.rs:98`).
- **Fix V — a name-bindings ticket asks only for renames, and narrows
  accessors and shadowing** (commit `a1af8705`): the ticket line now says
  the rule renames a binding that already exists and a finding whose change
  only adds a binding is none, that a method taking arguments or looking up
  another value is no accessor, and that a later inner binding is no
  collision only when that inner block does not also read the renamed
  binding. All 37 settled findings rename an existing binding, so the
  change costs no recall by construction. Rerun 4 drops the traced
  procedure's inline sentence and is measured on recall, on a blind check
  of its new findings, and on a blind check of 30 findings rerun 2 already
  had, each against its bar.
- **Fix U — structure file cells are keyed by their module, not their
  crate** (commit `07d86ebe`): a structure verdict on a file inside a crate
  was keyed by the whole crate, so an edit anywhere in it made every such
  verdict stale and the planner queued them all again. The key now covers
  the Rust files in the file's own directory and the module file that
  declares that directory; a crate root keeps the crate key and manifest
  rules keep theirs. Rows recorded under the old key are not rewritten: a
  held verdict carries over when the module key at its own commit still
  matches, so an existing ledger needs no migration. Accepted risk: a
  structure verdict that depends on a caller outside the module's own
  directory no longer goes stale when that caller changes. Ticket 19 in the
  modules pass waits on it.
- **Name-bindings rerun 4 — recall holds, new findings fail the 10% bar**:
  36 of the 37 settled findings caught (97.3%). The pair that moved cancel:
  `actions.rs:835` is caught for the first time, and `overview.rs:72` is
  lost because its judge read `lines.join("\n")` under fix V's accessor
  sentence as a method that takes arguments. The pass raised 331 findings;
  52 sit where rerun 2 had none. Three blind judges called none of a random
  30 of the 279 older findings false (0%), so fix V removed the shape that
  may have predated fix S. Of the 52 new ones they called 8 false (15%):
  four rename `column` and `row` after `Position::new(x, y)`, whose
  parameters are single letters that give no name to match; two name one
  element of an array literal (`&[item]`, `[path.as_path()]`) after the
  collection parameter it sits in; one renames `key` after
  `CFString::from_static_str(string)`; one binding fills two parameters
  with different names.
- **Fix W — a name-bindings ticket skips single-letter parameters and
  array elements** (commit `45438d3d`): the ticket line now says a
  parameter with a single-letter name gives no name to match, and a binding
  placed as one element of an array or slice literal does not fill the
  collection parameter. Fix V's accessor sentence stays word for word, so
  `overview.rs:72` stays lost; the bar is 36 of 37.
- **Name-bindings rerun 5 — new findings pass, recall fails**: 33 of the
  37 settled findings caught (89.2%). Besides `overview.rs:72`, three
  findings rerun 4 caught were lost to one judge mistake, reading a
  conversion's own parameter as the target or as a second target:
  `usize::from(saved_width)` into `push_display_padded(width)` was read as
  a derived value, `ThemeId::new(name)` into `find(id)` was judged to match
  at `new(name)`, and `WorktreeRootDisplay::from(item)` beside `Some(item)`
  was counted as a second target, although `from` takes the generic
  `value`. None of the four involves fix W's sentences. The pass raised 311
  findings; 47 sit where rerun 2 had none. Three blind judges called 1 of
  the 47 false, and that one (`actions.rs:835`) is a settled finding, so
  none are false after the hand-check (bar 10%). Of a random 30 of the 264
  older findings they called 1 false (3.3%), a binding that keeps a
  type-named prefix.
- **Fix X — a name-bindings ticket never targets a conversion's own
  parameter** (commit `8cd73ea4`): the ticket line now says a conversion's
  own parameter, such as the `value` of `From::from` or the `name` of a
  newtype constructor, is never the target and never a second target, and
  the judge follows the converted value to the parameter it fills. Rerun 6
  is measured on both bars. From rerun 6 on, a failed rerun whose misses
  share no cause the ticket leaves uncovered runs the same ticket twice
  more, and the median is judged against the bar.
- **Name-bindings rerun 6 — both bars met**: 36 of the 37 settled findings
  caught (97.3%), the bar. Only `overview.rs:72` is lost, as expected; the
  three sites rerun 5 lost to the conversion-parameter mistake are all
  caught again. The pass raised 321 findings; 47 sit where rerun 2 had
  none. Three blind judges called none of the 47 false (bar 10%), and none
  of a random 30 of the 274 older findings. The name-bindings pass is done.
- **Types and names re-sample — both fail, names by a wide margin**: the
  sample files of both families, judged as one read ticket per family
  through the ticket text the planner writes now, by two blind judges per
  family. Types: 55 of 61 (90.2%, bar 95%), up from the ledger's 50. Owned
  bools are complete at 43 of 43, and fix P holds: no judge flagged the ten
  out-of-scope `pub` sites. If-else stays weak at 8 of 13: two of its misses
  were seen and recorded only under the owned-bool rule, three are
  decisions found only by comparing branches across a file, and
  `identity.rs:169` was kept under the const exception although no caller
  is a const context. Names: 63 of 117 (53.8%), down from the 100 of 117
  the windowed pass reached before fixes P to X. The judges raised 73
  name-bindings findings, against none in the windowed pass, while
  type-named bindings fell to 30 of 66 and comments to 29 of 43: the long
  name-bindings paragraph draws the pass to one rule of seven. Some lost
  type-named sites were recorded under name-bindings instead.
- **Fix Y — a read ticket holds a site under every rule it breaks**
  (commit `781ff3ef`): the re-sample's judges held a defect that breaks two
  pinned rules under one of them only, so the other rule's cell was
  recorded clean. The read-ticket procedure now says a site that breaks
  more than one rule is held under each rule it breaks, once per rule, even
  when one change fixes both. Structure tickets keep their own rule: a
  defect several rules name is held once, under the rule it breaks most
  directly.
- **Fix Z — name-bindings gets its own read ticket** (commit `c79c21be`):
  the name-bindings rule needs a long paragraph of its own, and inside the
  names ticket it drew the pass away from the other six rules. The planner
  now splits a file's read work so the name-bindings rule goes to a read
  ticket of its own and the family's other rules stay together. From here
  the names family is scored on its other six rules (111 one-rule
  findings; the re-sample caught 60 of them), and name-bindings keeps its
  own bar of 36 of 37.
- **Rerun 7 — types and name-bindings pass, names falls short**: both
  families' samples judged again through the ticket text after fixes Y and
  Z, by fresh blind judges on the same halves. Types: 59 of 61 (96.7%, bar
  95%). The two defects held under one rule only are now held under both,
  owned bools stay complete at 43 of 43, and `identity.rs:169` is again
  kept under the const exception (`rerun7_types_table.md`). Name-bindings
  in its own ticket: 36 of 37, the bar, so fix Z does not undo rerun 6
  (`rerun7_bindings_table.md`). Names, six rules: run 1 caught 104 of 111
  (93.7%), with misses that share no cause, so the ticket ran twice more;
  runs 2 and 3 caught 99 and 104, a median of 104 (93.7%, bar 106). Fixes
  Y and Z lifted names from 60 to 104: type-named bindings are back to 62
  to 64 of 66 and comments to 36 to 40 of 43. Only two sites are missed in
  every run (`render.rs:132`, `constants.rs:8`); every other miss is
  caught by another run. Combining two runs' findings passes in every
  pairing, 108 to 109 of 111 (`rerun7_names_table.md`).
- **Fix AA — two blind judges on the names read ticket** (commit `befeb10a`):
  one judge on the six-rule names ticket reaches 99 to 104 of 111, short
  of the bar, while two runs' findings together reach 108 to 109. Before
  adopting two judges, every finding the three rerun 7 runs held outside
  the reference was judged again, blind, with 30 reference findings mixed
  in as controls (all 30 judged real). One run held 1 false finding and
  the other two held 2 each, of about 255; a two-run union holds 3 or 4
  false of about 510, 0.6% to 0.8% of its findings, against a bar of 10%.
  The five false findings share no cause: three fall under a stated
  exception, one site already meets the rule, one sits under the wrong
  rule (`rerun7_names_precision_table.md`). The names read ticket now has
  two judges who read every file blind to each other and hold nothing
  themselves; the unit director holds the union, one finding per defect,
  and records a cell only after both have judged it, clean only when both
  call it clean. Reading the names rules costs twice what it did. The
  name-bindings ticket keeps one judge, since it meets its bar alone.
- **Fix AB — modules refill reads listed sites once per crate** (commit `6e2975f4`):
  building the modules queue took 228 s, 220 s of it in 1318 reads of a
  rule's listed sites: each read reloads the rule catalog, and the
  structure branch read it once per rule for every batch. It now reads each
  rule's sites once per crate and filters them per batch. The queue builds
  in 25 s instead of 182 s with identical tickets, about a third of each
  modules ticket's cycle. A test pins the read count and three exact
  tickets; it fails before the fix. The unit director closed the review on
  their own read of the change.
- **Fix AC — modules structure tickets ask the when-to-split question of every file** (commit `2c1c3f54`):
  two blind judge sets on the 20-file modules sample found 33 of the 35
  reference findings between them. Both misses were files that should
  split, which no judge asked of a file once other rules held findings on
  it. When `when-to-split-a-module` is pinned, the ticket now asks it of
  every file. The reference is corrected to 35: the wire.rs over-split
  finding is dropped, because `parallel_cases.rs` is shared with an
  integration test and cannot fold back. The ChildReportFailure finding is
  credited, because moving the hook suite fixes it. A test pins the line;
  it fails before the fix. After it, one blind judge per batch finds 34
  of the 35 references (97.1%; the miss is a reciprocal sibling dependency
  in birth_stamp). A blind check of the 15 findings outside the reference,
  the four new splits first, rules all 15 real: the step adds no
  over-splitting.
- **Fix AD — tickets pinning `comments-name-code-constructs` keep every stated reason** (commit `8b0f18b0`):
  on cargo-liner, names writers replaced reason-giving comments with
  narration of the code. Neither the rule nor the ticket said to keep the
  reason. The ticket now does, marked temporary until the rule text
  carries the same sentence, which waits on the user. A test covers every
  ticket kind; it fails before the fix.
- **Fix AE — a when-to-split finding names the split alone** (commit `dc484424`):
  after fix AC one judge held four defects in shared_capture.rs as one
  split finding, a wrong count with no before and after per move. The
  when-to-split line now says each piece that belongs with another owner
  is its own finding under the rule that places it. The test pins the
  sentence; it fails before the fix.
- **Fix AF — refill skips held cells while their proposals stay open and their files are unchanged** (commit `8000d78d`):
  on cargo-liner the names planner issued five tickets for the same four
  held sites, each closing with nothing to do, because a crate-scope edit
  elsewhere made the cells stale. Refill now skips a cell whose latest
  verdict is held, whose findings all sit under open proposals, and whose
  own file is unchanged since; it reissues when either changes, and such
  cells no longer hold other families back. Two other reported reissues
  (clean forbidden-word and census cells) trace to crate-scope rules and
  wait on the input_scope change. Seven tests; four fail before the fix.
- **Fix AG — a ticket's checkpoint re-stamps its own family's verdicts on files only it changed** (commit `ceef89b6`):
  on cargo-liner names-unit-34 reissued agent_cell.rs cells that
  names-unit-15 had judged, because names-unit-15's own checkpoint edited
  one comment in that file after the verdicts. At checkpoint, the ticket's
  own family's verdicts are now re-stamped when every change to the file
  since the verdict is the ticket's own work after its start. An outside
  edit still reissues, and another family's verdict on the same file stays
  stale: a names edit can break forbidden-words. Three tests, one per case.
- **Fix AH — style issues propose a ranking, and a root-crate issue has a title** (commit `260e4550`):
  all 279 style issues on cargo-liner had an empty `Proposed ranking:` line,
  so the user had no values to confirm at review. A finding in the root crate
  got the title `# Style restructure: ` with no name. The filer now derives
  the five values from each batch, and a root-crate batch takes the
  repository name. After the fix merges, the same writer fills the open
  issues in one vault commit. The showrunner approved the fix and the backfill.
- **Fix AI — style tools read the pushed branch point, not the local one** (commit `4253334f`):
  refill, reconcile and the hand-off merge check read the local `main` ref.
  The checkouts share one repository, so that ref holds the parent's merge
  while its gate runs. On 2026-10-18 at 07:01Z, the parent merged hand-off
  `0c01c6c` as `b194ce45`, and its tests failed. While they ran, two units
  reported `b194ce4` as the tip to sync. The style showrunner fast-forwarded
  style/main to it at 07:15Z and pushed. The parent's second merge,
  `a779ebde`, then reached main beside it. The trees are equal, so no code is
  wrong, but main holds two merges of one hand-off. The showrunner approved the fix and keeps the merge already on
  main, because the trees are equal.
- **Fix AJ — style proposals read the crate layout once per run** (commit `6b246818`):
  the filer and the ranker read the crate layout again for each finding, at
  3.7 seconds a read on cargo-liner. The backfill over about 4,000 held
  findings would have taken about four hours. Each run now reads the layout
  and the held findings once. With the fix, the backfill dry run took 19
  seconds. The showrunner approved the fix.
- **Ranking backfill:** on 2026-10-18, the ranker filled all 279 open style
  issues in the vault and skipped none. Only the empty `Proposed ranking:`
  lines changed, and one empty root title became `# Style restructure:
  cargo-liner`. No vault commit was made: no style issue was ever committed,
  so a commit would add all 279 files for the first time. The showrunner
  chose to leave them untracked, as the rest of the folder is.

Fixes A and B are merged to `~/.claude` main as `b8b8e74c`, fix G as `31e7bae8`,
fix I as `89f24e01`, fix J as `9b71ca5b`, fix K as `598232d8`, fixes L and M as
`a8ae4d2a`, fix N as `5f63b4cf`, fix O as `e4906bb9`, fixes P, Q and R as
`adfa7c47`, fixes S and T as `b2714928`, fix U as `ede97508`, fix V as `6d67a552`, fixes W and X as `2eb57a74`, fixes Y and Z as `0cb8edca`, fix AA as `cf8f044e`, fix AB as `50e72f7f`, fixes AC and AD as `d988aa8a`, fixes AE and AF as `cf738406`, fix AG as `c87d2bec`, fix AH as `053fa900`, fix AI as `050815fb`, fix AJ as `77af41b4`.
Live verification of the fixes and subsequent measurements remain pending.
