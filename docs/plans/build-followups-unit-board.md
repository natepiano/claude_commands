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

#### Work Order

**Goal:** The units report shows the real number of findings built in the last 24 h on a ledger at schema 24 or 25, and `style_plan.py split-all --all` runs the split for every running style production as one fixed command.

**Depends on:** none

**Spec:**
1. The report's recent-built count reads `held_findings.built_at` when the column exists. On a ledger without the column (schema below 25), it uses the same rule migration 25 uses to fill it: for `state='built'` rows, the merged build ticket's `updated_at`, else the row's `updated_at`. One function owns this rule for both the report and migration 25's fill, so the two cannot drift. The report stays read-only: no migration, no write.
2. The ETA in the same totals line uses the same recent-built count, so a ledger with recent builds no longer reads `no ETA: nothing built in 24 h`.
3. `style_plan.py split-all --all` finds every running production whose doc carries `**Kind:** style`, from `showrunners.py list` and each production doc, and runs `split-all` for each. A production that fails is skipped with one log line naming it and the error; the others still run. With no style production, it prints one line and exits 0. While a natedev build hold is in force (`build_hold.py status`), it prints one line and exits 0 without running any split. Exit status is 0 unless the command itself cannot run.
4. `docs/as-built/style-showrunner.md` and `docs/as-built/unit-board.md` describe the fallback and the `--all` form, with the exact nightly command line.

**Files:**
- `scripts/production/style_units.py`
- `scripts/production/test_style_units.py`
- `scripts/style/style_ledger.py`
- `scripts/style/test_style_ledger.py`
- `scripts/style/style_plan.py`
- `scripts/style/test_style_plan.py`
- `docs/as-built/style-showrunner.md`
- `docs/as-built/unit-board.md`

**Workers:** 1 writer
- impl: every file above.

**Acceptance gate:**
- A test builds a schema-24 ledger with no `built_at` column and built rows updated in the last 24 h; the report's totals line counts them and gives an ETA, and the ledger file's bytes are unchanged.
- A test on a schema-25 ledger reads `built_at`.
- Tests for `split-all --all`: two style productions where one fails (the other still runs, exit 0); no style production (one line, exit 0); a build hold in force (one line, exit 0, no split runs). Tests patch the production list, the hold status and the split call; no test touches live data.
- basedpyright on every touched Python file: `0 errors, 0 warnings, 0 notes`.
- `cd scripts/production && python3 -m unittest test_style_units` and `cd scripts/style && python3 -m unittest test_style_plan test_style_ledger` pass.

**Constraints from prior phases:**
- `style_plan.split` keeps its `SplitDeadline` and bounded `record_split` write; `split-all` keeps its existing per-unit output lines.
