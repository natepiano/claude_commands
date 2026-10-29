# unit-board

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up to the unit-board run (as-built: `docs/as-built/unit-board.md`): the units report counts recent builds on a ledger that has not reached schema 25, and the nightly split runs as one fixed command.

> **Production: build-followups** — unit `unit-board-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-28 18:4x EDT

The showrunner reported a defect from cargo-liner-style: the units report shows "0 built in the last 24 h · no ETA" for all 4 units, but the live cargo-liner ledger has 155 findings in state built with `updated_at` in the last 24 h. The live ledger is at schema 24, so `held_findings` has no `built_at` column; migration 25 adds it and fills it, but the report opens the ledger read-only, so no migration runs. The showrunner's words: "Fix it at the cause: the live ledger gets the built time (a migration that fills it from updated_at for rows already built), and the report reads it. Add a test against a ledger without the column." Add-on settled as current by the showrunner: `style_plan.py split-all --all` for the nightly timer.

## Delegation Context

- **Project:** `~/.claude` (claude_commands) — the units report and the nightly style split.
- **Project started:** 2026-10-16T20:40:19.366+00:00
- Repository: `~/.claude` scripts, Python 3.13, stdlib `unittest`. basedpyright must report `0 errors, 0 warnings, 0 notes` on every touched file.
- The style ledger is SQLite at `~/.local/state/style/<family>/ledger.sqlite`. `style_ledger.MIGRATIONS` holds numbered migrations; opening a `Ledger` for writing runs every migration above the stored `schema_version`. Migration 25 creates `refill_results` and adds `held_findings.built_at`, filled with `COALESCE(<merged build ticket updated_at>, held_findings.updated_at)` where `state='built'`.
- `scripts/production/style_units.py` builds the units report and its totals line (`built in the last 24 h`). It reads the ledger read-only and must never migrate or write it.
- Never write to `~/.local/state/style/` or to any plan or decision under `~/rust/`. Read live data only through read-only connections or a copy under `/tmp/claude`. Never run refill, `next_phase`, `split`, `split-all` or any `style_plan.py` write path on live data. Tests use temporary ledgers only.
- Builds and tests: check `~/.claude/scripts/lib/py ~/.claude/scripts/build_hold/build_hold.py status` first; while a natedev build hold is in force, run no tests or type checks.

## Phases

### Phase 24 — the units report counts recent builds on every ledger, and the nightly split is one command  · status: done

#### As-built

- `style_ledger.legacy_built_at_sql(finding="held_findings") -> str` returns the SQL for the build time of a built finding before schema 25: the `updated_at` of its merged build ticket, else the `updated_at` of the finding. Migration 25 fills `held_findings.built_at` with it, so the report and the migration use one rule.
- `style_units.read_snapshot` reads `h.built_at` when the column exists. Without the column, it uses `legacy_built_at_sql("h")` when the `tickets` table and the `build_ticket` column exist, else `h.updated_at`. The report stays read-only: no migration, no write. The recent-built count and the ETA in the totals line use this value.
- `style_plan.split_all_running()` runs behind `split-all --all`, which is mutually exclusive with `--production`. It checks the natedev build hold (`build_hold.natedev_holds(build_hold.holder_directory()).in_force`) before discovery and again before each production. It reads `showrunners.registered_showrunners()`, keeps the entries with a session, reads the Kind of each doc with `production_field`, and runs `split_all` for each `style` production.
- Output of `split-all --all`: a failed production prints `split-all: <slug>: failed: <cause>` and the others still run; no style production prints `no running style productions`; a hold prints `split-all: natedev build hold in force; skipped`. The exit status is 0 in all these cases. A discovery error exits 1.
- The nightly command is `~/.claude/scripts/lib/py ~/.claude/scripts/style/style_plan.py split-all --all`.

**Files:**
- `scripts/style/style_ledger.py` — `legacy_built_at_sql`; migration 25 fill uses it.
- `scripts/production/style_units.py` — `read_snapshot` built-time fallback.
- `scripts/style/style_plan.py` — `split_all_running`; `split-all --all`.
- `scripts/production/test_style_units.py` — schema-24 fallback with unchanged ledger bytes; schema-25 `built_at` precedence; legacy ledgers without ticket metadata.
- `scripts/style/test_style_ledger.py` — the fallback matches the migration 25 fill.
- `scripts/style/test_style_plan.py` — `AllStyleProductionsTests`: a failure continues, no production, hold before discovery, hold that starts between productions, discovery failure.
- `docs/as-built/style-showrunner.md`, `docs/as-built/unit-board.md` — the fallback, the `--all` form and the nightly command.

**Gotchas:** The legacy SQL reads the `tickets` table and `held_findings.build_ticket`. On a ledger without one of them that SQL fails, so the report uses `h.updated_at` there. A build hold can start while a production splits, so the hold check runs before each production, not only once.
