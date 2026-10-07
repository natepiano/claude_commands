# Build report: rebuilds and real waits

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The daily build report shows where compile time goes (cold builds, cascades, edited-crate rebuilds, nextest compile against run) and splits the build-folder wait into a seat's wait behind its own call and behind another seat.

> **As-built disposition: amend** — `docs/as-built/build-memory-admission.md` (daily report order), `docs/as-built/build-followups.md` (report section list)

> **Production: build-followups** — unit `build-report-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

The user (2026-10-07, after the build analysis): "yes do all of this - understanding builds deeply and finding ways to speed them up is one of the most important things we can  do"

## Delegation Context

- **Project:** `~/.claude` configuration repo; `scripts/buildlog/` is the build log (records, sqlite index, daily report).
- **Project started:** 2026-10-06T20:10:48-04:00
- **Stack:** Python 3 standard library (`sqlite3`, `json`, `unittest`), checked by basedpyright.
- **Layout:** `scripts/buildlog/report.py` builds the `buildlog report [day]` markdown; `scripts/buildlog/test_report.py` tests it against an in-memory index.
- **Key files:** `scripts/buildlog/report.py` — `report()` (section order), `waiting_section()` and `wait_row()`, `seconds()`, `nearest_rank()`, `table()`, `fetch()`, `ON_DAY`; `scripts/buildlog/test_report.py` — `ReportTests` and its fixtures; `scripts/buildlog/index.py` — the `steps` and `calls` schemas (read only; `buildlog schema` prints them).
- **Test lanes:** `scripts/buildlog/test_report.py`.
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` (from the worktree root; capture the exit status without a pipe).
- **Lint:** `basedpyright scripts/buildlog` — pass is `0 errors, 0 warnings`.
- **Invariants:** no `Any` (annotate every signature; `TypedDict` or a dataclass for known shapes; a line-level `# pyright: ignore[reportAny]` only as a last resort); never a file-level pyright ignore. The report stays read-only on the index. Every time it prints is local (the caller sets `TZ`). Banned-word hooks check every edit: plain words only. Work only in `/home/natepiano/worktrees/claude-build-followups-build-report`.

## Phases

### Phase 1 — The report shows where compile time goes, cold builds included  · status: done

#### As-built

- `rebuilds_section(connection, day)` in `scripts/buildlog/report.py` emits `### Rebuilds`; `report()` calls it directly after `waiting_section(...)`. `known_crate_steps` reads the day's (`ON_DAY`) non-sweep `steps` with a known `crates_compiled`, and `rebuild_bin` sorts each into `none` (0), edited crate (1–3), cascade (4–49) or cold (50+).
- Bin edges are the constants `NO_REBUILD_CRATES`, `EDITED_CRATE_MAX_CRATES`, `CASCADE_MAX_CRATES` and the derived `*_MIN_CRATES`; the labels `NO_REBUILD_LABEL`, `EDITED_CRATE_LABEL`, `CASCADE_LABEL`, `COLD_BUILD_LABEL` (in order in `REBUILD_LABELS`) are built from those edges, and code compares against the named labels, never a tuple index.
- Section layout, top to bottom: the lead line `nextest: <compile> compiling, <other> running tests (<n>% compiling).` over the nextest steps (omitted with its blank line when the day has none); the table `Rebuild | Steps | Compile | Other | Total | Share of compile | Share of time`, all four bins always present (an empty bin shows `0` and `—`), then its `Source:` line; then, only when edited-crate nextest steps exist, the title `Edited-crate rebuilds by package (nextest):`, the table `Package | Steps | p50 | p95 | Compile` (top 5 by compile, ties by name) and its `Source:` line.
- Compile is cargo's `finished_s` (NULL counts as 0); Other is duration minus compile, floored at 0 per step (`RebuildTiming`); shares are whole percent via `whole_percent`. The package is the first `PACKAGE_PATTERN` (`package\(([^)&|\s]+)\)`) match in argv, else `(unknown)`; p50 and p95 come from `nearest_rank()` over steps with a known compile time (`PackageRebuilds`), `—` when there are none.

**Files:**
- `scripts/buildlog/report.py` — the bin constants and labels, `PACKAGE_PATTERN`, the `KnownCrateStep`, `RebuildTiming` and `PackageRebuilds` dataclasses, `rebuild_bin`, `known_crate_steps`, `rebuild_timings`, `rebuild_rows`, `whole_percent`, `nextest_rebuild_lines`, `rebuild_package`, `package_rebuild_rows`, `rebuilds_section`, and its call in `report()`.
- `scripts/buildlog/test_report.py` — tests for the Rebuilds section.

**Binds later work:** `### Rebuilds` sits directly after `### Waiting` in `report()`. Every report table is followed by exactly one `Source:` line and nothing else beneath it, so any explanation of a table goes in that line. `rebuild_bin` and its edge constants are the one definition of rebuild bins.

**Gotchas:** the `scripts/buildlog/buildlog` shim loads the main checkout's `cli.py`, so a worktree check runs `python3 scripts/buildlog/cli.py`. basedpyright exits 3 with a missing-`.venv` note in a worktree while reporting `0 errors, 0 warnings`; the diagnostic line is the result, not the exit status.

**Ruled out:** a separate cold-build line under the table, since the cold row carries the cold-build count, time and shares.

### Phase 2 — The build-folder wait separates a seat's own queue from waiting on another seat  · status: todo

#### Work Order

**Worktree:** `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.

**Goal:** the Waiting table replaces its one `Build-folder turn` row with two: `Build-folder turn, behind another seat` and `Build-folder turn, behind its own call`, so the first measures real blocking.

**Spec:**
- The cargo token is held per delegate session (`verify.sh` acquires it on the session's board), so a call's token wait can only be caused by calls with the same non-null `delegate_session`.
- For each call on the day with `token_wait_s > 0`: its wait interval is `[started_at + wait_s - token_wait_s, started_at + wait_s]`. Each other call of the same `delegate_session` with an `ended_at` holds the token over `[started_at + wait_s, ended_at]` (holders may start the day before; fetch calls whose `ended_at` falls on the day or later). Overlap seconds with holders of the same `seat` count as own; overlap with any other seat counts as another seat. Any part of the wait no holder covers counts as another seat, so no wait is hidden. Cap the two parts at the call's `token_wait_s`.
- A call with no `delegate_session` puts its whole wait in the another-seat row.
- Each row goes through the existing `wait_row()` with that call's part as the duration (worktree as owner and name, as now); `Waited` counts calls with a positive part in that row. The other-seat row comes first.
- Add the Waiting table's one line, directly under it (the report's style: one `Source:` line under each table, nothing else): `Source: verify.sh calls, memory-gated steps and CI jobs; a seat's own calls run one at a time, so waiting behind its own call adds no delay, behind another seat does.`
- Keep functions under the function-length hook.

**Files:**
- `scripts/buildlog/report.py` — `waiting_section()` and a new attribution helper.
- `scripts/buildlog/test_report.py` — tests for the split; update the existing Waiting tests to the two row labels.

**Seats:** `1 writer + 1 tester` — one module and its test file.
- `impl` — `scripts/buildlog/report.py`.
- `test` — `scripts/buildlog/test_report.py`, from this Spec alone: a wait fully behind the same seat, fully behind another seat, split across both, with an uncovered remainder, a call with no delegate session, a holder from another session ignored, a holder that started the previous day, and both rows present when nothing waited.

**Constraints from prior phases:** Phase 1 put `### Rebuilds` directly after `### Waiting`; leave its order alone. Every report table is followed by exactly one `Source:` line and nothing else. The `scripts/buildlog/buildlog` shim loads the main checkout, so worktree checks call `python3 scripts/buildlog/cli.py`. In this worktree basedpyright prints a missing-`.venv` note and exits 3; the gate is its `0 errors, 0 warnings` line.

**Acceptance gate:** the Test command green and the Lint command at `0 errors, 0 warnings`. On `TZ=America/Los_Angeles python3 scripts/buildlog/cli.py report 2026-10-06`, the own-call row holds most of the day's build-folder wait (the 2026-10-05/06 analysis found about three quarters).
