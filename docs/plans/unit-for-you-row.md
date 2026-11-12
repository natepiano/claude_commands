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

#### As-built

- In a unit director session, the status line For you row counts only the waiting items whose source is that unit: questions by `asked_by`, change rows by `Producer`, proposals by `proposed_by`, and efficiency ledger lines by a ` [by: <staff id>]` tag. The unrouted count is left out. A showrunner session keeps the full row.
- `footer_counts.read_counts(doc, *, questions=0, record=None, source="")` and `showrunner_footer.open_questions(productions, *, identities=None, source="")` take an optional fixed unit staff id. Without a source, every count is as before.
- `footer_counts.EFFICIENCY_LINE` captures `head` before the first ` — ` and `status` after it. `open_efficiency(line, *, source="")` matches the source tag at the end of the head, so a later ` — ` note or extra spaces do not hide a unit's line.
- `counts_row.py --unit <staff id>` reads the unit's production through its staff parent and writes `<showrunner state>/counts/<unit id>` in the `write_cache()` format, with a `source=unit:<id>` line. `statusline.sh` finds the unit row whose `current_session` (or `id` when the field is absent) equals the payload `session_id`, refreshes only that unit's cache, and refreshes a cache whose source line differs.
- `proposal_issue._create_numbered()` accepts a unit `proposed_by`. `style_proposals.DecisionWriter.create()` records the caller's staff id when the caller is a unit.
- `Candidate.by` writes ` [by: <id>]` after the gain tag and is parsed back. `candidates.record()` sets it from a unit caller and reads staff only when `staff.caller_identity()` is not empty. `commands/nightly_review.md` tells a hand-written line to add the tag.
- Deploy step: a one-off program outside the repository fills an empty `Producer` cell only from `change_review.checkpoint_producers()` or a single-producer `merge_producers/<merge hash>.json` record. Old proposals and old efficiency lines have no provable unit source and count for no unit.

**Files:**
- `scripts/production/footer_counts.py`, `scripts/hooks/showrunner_footer.py` — the source filter.
- `scripts/statusline/counts_row.py`, `scripts/statusline/statusline.sh`, `scripts/statusline/statusline.jq` — the unit cache and row.
- `scripts/production/proposal_issue.py`, `scripts/style/style_proposals.py` — a unit proposer.
- `scripts/nightly_review/candidates.py`, `commands/nightly_review.md` — the efficiency source tag.
- `docs/as-built/showrunner-automation.md` — the For you item.

**Gotchas:** A staff row can omit `current_session`; staff.py reads it as the row id. The real efficiency ledger has lines with a second ` — ` note, so the source match must use the first delimiter.

**Ruled out:** Matching the efficiency source on the last ` — ` delimiter; it misses lines with a later note.
