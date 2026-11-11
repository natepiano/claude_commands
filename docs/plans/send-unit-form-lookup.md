# Send unit-form lookup

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** `send.py --to <name>-unit` finds the unit whose staff name is `<name>`. Workers' messages to their director stop failing. A failed recipient lookup tells the sender's owner at once.

## Source

2026-11-10, the sdlc showrunner: "send.py fails 'recipient lookup failed' for unit names in the '-unit' form. Since 11-09: 26 sends to 'queue-unit' (from queue's workers adapter and impl2, the last at 19:15 EST), plus 'aws-unit' and 'port-unit'. Those workers' messages to their director were lost. copies.unit_owner is the one rule for the '-unit' form; send.py name lookup should accept it."

2026-11-10, the user via builds and the sdlc showrunner: "the user wants a detector for a message sent again and again to a wrong place. Case: aws-spending-guard sent one notice 31 times in 18 h to the dead name claude-natedev-builds; every try failed 'recipient lookup failed', and nobody was told. send_repeat_guard missed it (3 repeats in 1800 s; these came about 35 min apart)." The showrunner's design: on a failed lookup, tell the sender's owner at once; a sender with no staff row goes to the owner a config key names, else to `UNIT_ANOMALIES_TO`; one notice per sender and name per day, with the sender, the failed name, the count and the config file or caller when known; the failed send's exit and log stay as now; tests use a temporary staff file and message state.

## What exists today (2026-11-10)

- `send.resolve_staff_recipient` matches a plain name against each staff row's name, or a `/` path against the names chain. Staff names of units are bare (`queue`, `aws`, `port`).
- Production docs and copy owners use the `<name>-unit` form (`queue-unit`). Workers read that form and send to it. No row has that name, so the send fails with `recipient lookup failed`.
- `copies.unit_owner(name)` is the one rule for the form: `name.removesuffix("-unit") + "-unit"`. `session_staff.unit_match` already uses it as a last step after an exact name match fails.
- The 26 sends to `claude-natedev-builds` come from `config/aws_target.conf` `notification_recipient`. That is a config value; sdlc sends it to builds. It is not in this plan.
- A failed lookup returns `RecipientLookupFailed` from `send.resolve_recipient`, prints its reason and exits non-zero. Nobody else is told.
- `scripts/message/repeat_guard.py` tells an owner about a repeated text (3 in 1800 s). It keeps state under `repeat_guard.message_state()` (`~/.local/state/message`). It does not see sends 35 minutes apart.
- `config/production.conf` `UNIT_ANOMALIES_TO=sdlc` names where unit anomalies go.

## Decisions

- **Exact name first** (plan author). The unit form applies only when the exact name lookup finds no row. A row whose staff name ends in `-unit` keeps its exact match, and no lookup that works today changes.
- **One rule** (sdlc, memory `copy-owner-unit-form`). The lookup calls `copies.unit_owner`. It writes no second suffix rule. The import of `copies` happens only on the fallback path, so a normal send pays nothing.
- **Unit rows only** (plan author). The unit form matches only rows of kind `unit`. A worker or showrunner name never matches through it.
- **Failed-lookup owner** (sdlc showrunner). A worker sender's owner is its unit director. A unit director's owner is its showrunner. Any other staff sender's owner is its parent showrunner. A sender with no staff row uses the new `config/send.conf` key `failed_lookup_owners` (`sender=owner` pairs); with no pair, `UNIT_ANOMALIES_TO`. A staff sender with no parent also uses `UNIT_ANOMALIES_TO`.
- **New key, first value** (plan author). `failed_lookup_owners=aws-spending-guard=builds`. A new key needs no user go (memory `config-change-needs-user-go`).
- **One module** (plan author). The detector lives in a new `scripts/message/failed_lookup.py`. `send.py` calls it once, on the failure path only, so a good send pays nothing.
- **Count** (plan author). The record keeps each sender and name's failure count and last notice time. A notice goes on the first failure, and again only when 24 h passed since the last notice for that pair. Each notice names the count since the last notice.
- **Caller** (plan author). The caller is the parent process's command line, read once on the failure path. The config file is named only when the sender's `failed_lookup_owners` entry or the caller's command line holds it; send.py does not search for it.
- **No loop** (plan author). The notice goes through the normal send path with sender `send.py`. A failed lookup for sender `send.py` sends no notice.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-11T01:34:46.000+00:00
- **Stack:** Python 3.13, standard library only.
- **Key files:**
  - `scripts/message/send.py` — `resolve_staff_recipient` (name and path match).
  - `scripts/copies/copies.py` — `unit_owner`, the one rule.
  - `scripts/production/session_staff.py` — `unit_match`, the same fallback for unit exclusions.
- **Test lanes:** `test_*.py` beside each script.
- **Test:** targeted test: `~/.claude/scripts/lib/py scripts/production/targeted_test.py` from the worktree root.
- **Lint:** `basedpyright scripts/message` reports `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Tests use their own staff snapshot. They never read or write live staff or live units (user, 2026-11-05).
  - Python is typed throughout, with no `Any` and no file-level type ignores (user, `~/.claude/CLAUDE.md`).
  - Add no check or scan that the Work Order does not name (user, 2026-11-03).
  - All text uses Simplified Technical English (user, 2026-10-18).

## Phases

### Phase 1 — Send finds a unit by its unit form  · status: done

#### As-built

- `send.resolve_staff_recipient` matches a `<name>-unit` target, or the last part of a `/` path, to a row of kind `unit` when no exact row matches. The match uses `copies.unit_owner`, imported only on that fallback path. The parent chain must still match the earlier path parts. More than one match keeps the ambiguous-row failure.
- `resolve_recipient.lookup` runs the same fallback after the live-name search finds nothing. It runs before the `LocalRosterIncomplete` and `RecipientSearchFailed` branches, so a roster failure does not skip it.
- Internal callers (`matching_recipients`, `resolve_recipient.lookup`, `_send`) use the private exact-only `_staff_recipient_match`.

**Files:**
- `scripts/message/send.py` — the unit-form fallback and the exact-only helper.
- `scripts/message/test_send.py` — unit-form rows, exact-name priority rows, worker exclusion, roster-failure rows.
- `docs/as-built/session-roster.md` — one sentence on the `<name>-unit` form.

**Binds later work:** a `RecipientLookupFailed` comes only after the exact name, the live roster and the unit form all fail.

**Gotchas:** a live session label with the exact name keeps priority over the unit form.

### Phase 2 — A failed lookup tells the owner  · status: todo

**Depends on:** Phase 1 (same file, `scripts/message/send.py`).

#### Work Order

**Goal:** when `send.py` cannot find a recipient, the sender's owner gets one notice at once. It names the sender, the failed name, the count and the caller. The same sender and name gets at most one notice per day.

**Spec:** change no file outside Files without a line in the summary.
1. New `scripts/message/failed_lookup.py`:
   - `report(sender: str, failed_name: str) -> None`: record the failure; when a notice is due, send it.
   - Record: one JSON file under `repeat_guard.message_state()`, `failed_lookups.json`, keyed by sender and failed name, with the count since the last notice and the last notice time. Write it atomically under a file lock, as `repeat_guard` does.
   - Owner: per the Decisions above (worker → unit director; unit → showrunner; other staff → parent showrunner; no row → `failed_lookup_owners`, else `UNIT_ANOMALIES_TO`).
   - Notice text, in STE: sender, failed name, count, caller command line, config file when known, and the reason line the send printed.
   - Send the notice through `send.py`'s normal path with sender `send.py`. A failed lookup whose sender is `send.py` records nothing and sends nothing.
   - A failure inside the detector prints one line on stderr and never changes the failed send's exit or output.
2. `send.py`: call `failed_lookup.report` once where a `RecipientLookupFailed` route ends the send. Keep the exit code and the printed reason as today.
3. `config/send.conf`: add `failed_lookup_owners=aws-spending-guard=builds` with a comment line in the file's style.
4. Tests in a new `scripts/message/test_failed_lookup.py`, with a temporary staff file (`STAFF_FILE`) and a temporary `XDG_STATE_HOME`. They never send a real message:
   - A worker sender's notice goes to its unit director; a unit sender's goes to its showrunner.
   - A sender with no row goes to its `failed_lookup_owners` owner, else to `UNIT_ANOMALIES_TO`.
   - A second failure of the same pair inside 24 h sends no notice and raises the count; after 24 h the notice names the count.
   - A sender `send.py` sends nothing.
   - Add one row to the existing `send.py` lookup-failure test that shows the exit and reason stay the same and `report` was called once.
5. Docs: one paragraph in `docs/as-built/session-roster.md` beside the Phase 1 sentence.

**Constraints from prior phases:**
- `resolve_recipient.lookup` tries the unit form before its `LocalRosterIncomplete` and `RecipientSearchFailed` branches. A `RecipientLookupFailed` means the exact name, the live roster and the unit form all failed.

**Files:**
- `scripts/message/failed_lookup.py` — the detector.
- `scripts/message/send.py` — the one call.
- `config/send.conf` — the new key.
- `scripts/message/test_failed_lookup.py` — the detector tests.
- `scripts/message/test_send.py` — the one row.
- `docs/as-built/session-roster.md` — the paragraph.

**Workers:** 1 writer + 1 tester.
- `impl`: `scripts/message/failed_lookup.py`, `scripts/message/send.py`, `config/send.conf`, `docs/as-built/session-roster.md`.
- `test`: `scripts/message/test_failed_lookup.py`, `scripts/message/test_send.py`.

**Acceptance gate:**
- The targeted test passes.
- basedpyright on each slot's files reports 0 errors, 0 warnings, 0 notes.
- A test run with a temporary state shows one notice for 3 failures of one pair in one day.
