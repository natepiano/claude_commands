# unit-board

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up to the unit-board run (as-built: `docs/as-built/unit-board.md`): the units report's style table says in words what each unit does, gives ETA its own column, and names what Built and Left count.

> **Production: build-followups** — unit `unit-board-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-28 20:5x EDT

The showrunner relayed a request from the user (via the showrunner of cargo-liner-style) for the units report's style table: "(a) Step says in words what the unit does now, for example \"writing 17 renames\", \"testing on the Mac\", \"waiting for the showrunner to merge\" or \"refilling\". Never only \"impl\" or \"checkpoint sent\". (b) ETA gets its own column. (c) The Built header says what it counts: \"Findings built (24 h)\", for example \"118 (62)\". (d) The Left header says \"Findings left\". Say whether Left counts only approved findings or all filed findings; if they differ, show the approved count as well."

## Delegation Context

- **Project:** `~/.claude` (claude_commands) — the units report and the nightly style split.
- **Project started:** 2026-10-16T20:40:19.366+00:00
- Repository: `~/.claude` scripts, Python 3.13, stdlib `unittest`. basedpyright must report `0 errors, 0 warnings, 0 notes` on every touched file.
- The style ledger is SQLite at `~/.local/state/style/<family>/ledger.sqlite`. `style_ledger.MIGRATIONS` holds numbered migrations; opening a `Ledger` for writing runs every migration above the stored `schema_version`. Migration 25 creates `refill_results` and adds `held_findings.built_at`, filled with `COALESCE(<merged build ticket updated_at>, held_findings.updated_at)` where `state='built'`.
- `scripts/production/style_units.py` builds the units report and its totals line (`built in the last 24 h`). It reads the ledger read-only and must never migrate or write it.
- Never write to `~/.local/state/style/` or to any plan or decision under `~/rust/`. Read live data only through read-only connections or a copy under `/tmp/claude`. Never run refill, `next_phase`, `split`, `split-all` or any `style_plan.py` write path on live data. Tests use temporary ledgers only.
- Builds and tests: check `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py status` first; while a natedev build hold is in force, run no tests or type checks.


### Phase 25 — the units report's style table says what each unit does, and names what it counts  · status: done

#### Work Order

**Goal:** In the units report's style table, Step says in words what the unit does now, ETA has its own column, and the Built and Left headers say what they count.

**Depends on:** none

**Spec:**
1. Step says in words what the unit does now, from the unit's open run step and its ticket, for example `writing 17 renames`, `testing on the Mac`, `reviewing`, `waiting for the showrunner to merge`, `refilling`. It never shows only a raw step word such as `impl` or `checkpoint sent`. A unit with nothing open says what it waits for (for example the idle refill outcome).
2. ETA is its own column after Step, holding the unit's recorded ETA as a clock time, or `—` when there is none. Step no longer carries ` · ETA`.
3. The Built header is `Findings built (24 h)`, and its cell is `<built> (<built in the last 24 h>)`, for example `118 (62)`.
4. The Left header is `Findings left`. Left counts filed findings whose decision is approved. When the count of all filed findings differs, the cell also shows it, for example `40 approved of 55 filed`, and keeps the split detail after it. The totals line names what it counts the same way.
5. `docs/as-built/style-showrunner.md` describes the new columns and what Left counts.

**Files:**
- `scripts/production/style_units.py`
- `scripts/production/test_style_units.py`
- `docs/as-built/style-showrunner.md`

**Workers:** 1 writer
- impl: every file above.

**Acceptance gate:**
- Tests pin each Step wording for an open writing, testing, review and checkpointed step and an idle unit; the ETA column with and without an ETA; the two new headers; a Left cell where approved and filed differ and one where they match.
- basedpyright on every touched Python file: `0 errors, 0 warnings, 0 notes`.
- `cd scripts/production && python3 -m unittest test_style_units` passes, and every test module that imports `style_units` passes.

**Constraints from prior phases:**
- The report stays read-only and reads `built_at` through `style_ledger.legacy_built_at_sql` on a ledger before schema 25.
