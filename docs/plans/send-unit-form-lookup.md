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

### Phase 2 — A failed lookup tells the owner  · status: done

#### As-built

- `failed_lookup.report(sender, failed_name, reason_line="")` records each failure in `failed_lookups.json` under `repeat_guard.message_state()`, under a file lock with an atomic write. Each sender and name keeps `count`, `last_notice` and an optional `notice_started`.
- A notice goes on the first failure, then again only 24 h after the last confirmed notice. It names the failed name, the sender, the count since the last notice, the caller command line, each `.conf`, `--config` or `--config-file` path in that command line, and the printed reason.
- The count stays until `send.py --from send.py` confirms the notice as sent, queued or kept. A `notice_started` mark blocks a second notice for one send budget. A failed send clears the mark and keeps the count.
- Owner: a worker's parent row; any other staff sender's first showrunner ancestor; both as a staff id, so two units with one name stay distinct. A sender with no staff row uses `failed_lookup_owners` (`sender=owner` pairs) in `config/send.conf`, else `UNIT_ANOMALIES_TO`.
- Sender `send.py` records nothing. A detector error prints one `failed lookup notice:` line on stderr and never changes the failed send's exit or output.
- `send.py` reports both failure paths: a missing name with healthy rosters raises `RecipientNameMissing(SystemExit)` (exit 2), which `_send` reports and re-raises; a `RecipientLookupFailed` route reports and exits 3. The printed reasons stay as before.

**Files:**
- `scripts/message/failed_lookup.py` — the detector.
- `scripts/message/send.py` — `RecipientNameMissing` and the two `report` calls.
- `config/send.conf` — `failed_lookup_owners=aws-spending-guard=builds`.
- `scripts/message/test_failed_lookup.py` — owner, daily count, interrupted notice, failure during delivery, config paths, `send.py` sender.
- `scripts/message/test_send.py` — `missing-name` and `missing-peer` rows with one `report` call each.
- `docs/as-built/session-roster.md` — the failed-lookup paragraph.

**Gotchas:** `test_send.py` `setUpModule` patches `failed_lookup.send_notice`, so no send test delivers a real notice.

**Ruled out:** a search of the disk for the sender's config file; only the caller's arguments and the owner entry name it.
