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

#### As-built

- The units report's style table has the header `| Unit | Ticket | Step | ETA | Findings built (24 h) | Findings left |`.
- `style_units.format_step(step, ticket, idle)` gives Step in words. It uses the run step's activity text when that text is more specific than a generic word. Otherwise it maps `impl` → `writing <ticket work>`, `fix` → `fixing review findings`, `review` → `reviewing`, `test` → `testing`, `checkpoint sent` → `waiting for the showrunner to merge`. An assigned ticket with no open step gives `starting <ticket work>`, a checkpointed one gives `waiting for the showrunner to merge`, and a unit with no ticket gives `idle` or `idle — <refill outcome>`. `ticket_work` gives `<n> files|sites|findings` for both Ticket and Step. `NoTicket` names a unit without an open ticket.
- `eta_cell` shows the recorded ETA as a clock time in the user's zone, or `—`.
- `FamilyCounts.filed` counts filed findings per family. `left_cell(approved, filed, split, now)` shows `<approved>`, or `<approved> approved of <filed> filed` when they differ, then the split detail.
- `LedgerSnapshot.total_filed` counts every filed finding in a ledger, also findings with no resolvable family. The totals line reads `All families: <built> findings built · <approved> approved findings left[ of <filed> filed] · <recent> built in the last 24 h · ETA …`.

**Files:**
- `scripts/production/style_units.py` — Step words, ETA cell, filed counts, headers, totals
- `scripts/production/test_style_units.py` — Step wordings, ETA column, headers, Left and totals counts
- `scripts/production/test_units_table.py` — row assertions for the new layout
- `scripts/style/test_style_plan.py` — report-table assertions; the locked-ledger split deadline test controls `style_plan`'s clock
- `docs/as-built/style-showrunner.md` — the table's columns and what Left counts

**Gotchas:** The split deadline test with a 0.25 s real deadline failed under a full-suite load, because scan setup used the time before the write. It now freezes `style_plan.time.monotonic` and patches `split_time_limit`.
