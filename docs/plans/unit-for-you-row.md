# Per-unit For you row

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A unit director's status line shows a For you row that counts only the items that unit filed.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words, from the sdlc showrunner on 2026-11-12:

"for you row in the status footer - i would like to see it contextually accurate under unit directors if they are the source of a question, a change, a proposal or an efficiency then show the "for you" in their row when you are on their session. If we don't have the data to pull this together for older items, that is fine - they just don't show - but going forward we should make it possible - and if we can simply backfill items so they do show in a valid unit director then that is also fine"

The showrunner's scope: in a unit director session, the For you row counts only items whose source is that unit (questions it filed, change rows with its Producer id, proposals and efficiency items it filed). Record the source unit staff id on every new item. Backfill old items where the source is provable; leave the rest out. Build on the devtools partialcounts change (`9d8ddfd58`, `scripts/statusline/counts_row.py`).

## Delegation Context

- **Project:** the `~/.claude` scripts — the status line For you row (`scripts/statusline/`, `scripts/production/`).
- **Project started:** 2026-11-12T17:30:00+00:00
- **Stack:** Python 3.13, basedpyright, `unittest`, POSIX shell and jq for the status line.
- **Layout:**
  - `scripts/statusline/statusline.sh` — about line 51 selects staff rows with `kind == "showrunner"`; lines 52–79 refresh each showrunner's counts cache and pass it with `TARGET=session:<reference>`.
  - `scripts/statusline/statusline.jq` — about line 139 selects the counts whose reference equals the payload `.session_id`.
  - `scripts/statusline/assignment.py` — `assignment()` resolves the payload `session_id` with `StaffSnapshot.by_current_session()`.
  - `scripts/statusline/counts_row.py` — `row()`, `cache_path(name)` (`<showrunner state>/counts/<staff id>`), `write()`, `write_cache()`.
  - `scripts/production/footer_counts.py` — `read_counts()` combines `change_counts()`, `proposals()`, the supplied questions, `efficiency()` and the unrouted count; `counts_line()` leaves out zero counts.
  - `scripts/hooks/showrunner_footer.py` — `open_questions()` counts due items from `update_registration.read_open_fields()`.
  - Questions: `scripts/production/update_registration.py`; each record has `asked_by: <asker staff id>`, written by `ask()`.
  - Change rows: `scripts/production/change_review.py`; the `Producer` column holds the source staff id, written by `append_rows()`.
  - Proposals: `scripts/production/proposal_issue.py` `create()` → `_create_numbered()`; `proposed_by` holds the proposer's staff id. `_create_numbered()` resolves it with `staff_reference(proposed_by, "showrunner")`, which refuses a unit. `scripts/style/style_proposals.py` `DecisionWriter.create()` (about line 393) passes the standing style production's name. `proposals_table.scope_issues()` and `waiting_entries()` give the waiting proposals.
  - Efficiency items: `scripts/nightly_review/candidates.py`; `Candidate` (`night`, `scope`, `kind`, `title`, `gain_text`, `state`) has no source field. `Candidate.line` writes `- <night> <scope>: <title> [gain/week: <gain>] — proposed`. `record()` appends; `footer_counts.efficiency()` counts lines whose status starts with `proposed` or `deferred`. `commands/nightly_review.md` (about line 47) tells the nightly agent to add lines by hand.
  - Staff: `scripts/production/staff.py` — `StaffSnapshot.by_current_session(session_id)`, `by_member_reference(ref)`, `caller_identity()`. A unit row has `kind == "unit"` and a fixed UUID `id`.
- **Test lanes:** `scripts.statusline.test_statusline`, `scripts.production.test_footer_counts`, `scripts.hooks.test_showrunner_footer`, `scripts.production.test_proposal_issue`, `scripts.style.test_style_proposals`, `scripts.nightly_review.test_candidates`, `scripts.production.test_change_review`.
- **Build:** `basedpyright scripts/statusline scripts/production scripts/hooks scripts/nightly_review scripts/style` must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest` with the test lane modules.
- **Invariants:**
  - The showrunner's For you row does not change.
  - Sources are fixed staff ids, never names or session ids.
  - An item with no provable source counts for no unit.
  - Add no guard, refusal or check that this plan does not ask for.

## Phases

### Phase 1 — Unit directors see their own For you row  · status: done

#### Work Order

**Goal:** In a unit director session, the status line shows a For you row that counts the waiting questions, change rows, proposals and efficiency items whose source is that unit. New proposals and efficiency items record their source unit.

**Spec:**

1. **Source on new proposals.** `proposal_issue._create_numbered()` accepts a unit staff reference for `proposed_by` as well as a showrunner. `style_proposals.DecisionWriter.create()` passes the caller's fixed staff id (from `staff.caller_identity()`) when the caller is a unit; otherwise it keeps its current value.
2. **Source on new efficiency items.** `Candidate` gets `by: str` (a fixed staff id, empty when unknown). `Candidate.line` writes ` [by: <id>]` after the gain part when `by` is set. The parser reads it back. `candidates.record()` sets `by` from `staff.caller_identity()` when the caller is a unit. `commands/nightly_review.md` tells a hand-written line to add ` [by: <staff id>]`. `footer_counts.efficiency()` keeps its count rule.
3. **Questions and change rows** already record `asked_by` and `Producer`. No writer change.
4. **Source filter in the counts.** `footer_counts.read_counts()` takes an optional `source: str` (a fixed unit staff id). With a source, each part counts only items with that source: questions by `asked_by`, change rows by `Producer`, proposals by `proposed_by`, efficiency lines by `by`. With a source, the unrouted count is left out. `showrunner_footer.open_questions()` takes the same optional source. Without a source, every count is as before.
5. **Unit cache and row.** `counts_row.py` gets a unit form: `counts_row.py --unit <staff id>` reads the unit's production through its staff parent, calls `read_counts(..., source=<unit id>)`, and writes `<showrunner state>/counts/<unit staff id>` with the same `write_cache()` format and watched paths. Build on the partialcounts retry already in `counts_row.py`.
6. **Status line.** `statusline.sh` resolves the payload session with the existing `assignment.py` lookup. When that row has `kind == "unit"`, it refreshes only that unit's cache with the existing staleness rule and passes it to jq as `session:<current session>`. It does not loop over other units. A showrunner session keeps its current path.
7. **Docs.** Update the For you item in `docs/as-built/showrunner-automation.md` in the same commit.
8. **Backfill (deploy step, not repository code).** Write a one-off program outside the repository, run it once at deploy, then delete it. It fills an empty `Producer` cell in a change review row only from an exact single-unit record: `change_review.checkpoint_producers()` or a `merge_producers/<merge hash>.json` file with one producer. It changes no other row. Questions keep their `asked_by`; old proposals and old efficiency lines have no provable unit source and stay out. Name the steps in the checkpoint notice.

**Files:**
- `scripts/production/proposal_issue.py`, `scripts/style/style_proposals.py` — a unit proposer.
- `scripts/nightly_review/candidates.py`, `commands/nightly_review.md` — the efficiency source.
- `scripts/production/footer_counts.py`, `scripts/hooks/showrunner_footer.py` — the source filter.
- `scripts/statusline/counts_row.py`, `scripts/statusline/statusline.sh`, `scripts/statusline/statusline.jq` — the unit row.
- `docs/as-built/showrunner-automation.md` — the For you item.
- `scripts/production/test_proposal_issue.py`, `scripts/style/test_style_proposals.py`, `scripts/nightly_review/test_candidates.py`, `scripts/production/test_footer_counts.py`, `scripts/hooks/test_showrunner_footer.py`, `scripts/statusline/test_statusline.py` — rows.

**Workers:** `2 writers + 1 tester`
- `impl` — the counts and the status line: `footer_counts.py`, `showrunner_footer.py`, `counts_row.py`, `statusline.sh`, `statusline.jq`, the as-built doc.
- `impl2` — the sources: `proposal_issue.py`, `style_proposals.py`, `candidates.py`, `commands/nightly_review.md`.
- `test` — rows in the existing test classes: a unit proposer is accepted; a style proposal from a unit records its id; an efficiency line round-trips `by`; each count part filters by source; no source keeps today's counts; a unit session draws its own row and a showrunner session draws the full row; an item without a source counts for no unit.

**State:** a unit counts cache file per unit staff id under `<showrunner state>/counts/`.

**Acceptance gate:**
1. Build: 0 errors, 0 warnings, 0 notes.
2. The Test command passes for the test lanes.
3. Live: in this unit-board session, the status line shows a For you row with only unit-board's items; the sdlc showrunner session's row is unchanged.
