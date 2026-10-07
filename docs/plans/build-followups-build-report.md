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

#### Work Order

**Worktree:** `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.

**Goal:** `buildlog report` has a `### Rebuilds` section right after `### Waiting` that splits the day's step time by how much each step compiled, names cold builds, gives nextest's compile and run hours, and lists the packages whose edited-crate rebuilds cost most.

**Spec:**
- New function `rebuilds_section(connection, day) -> list[str]`, called in `report()` directly after `waiting_section(...)`.
- Rows come from `steps` on the day (`ON_DAY`) with `step <> 'sweep'` and `crates_compiled IS NOT NULL`. Bin each step by `crates_compiled`: `0` → `none`; `1–3` → `edited crate (1–3 crates)`; `4–49` → `cascade (4–49 crates)`; `>= 50` → `cold (50+ crates)`. Name the bin edges as module constants.
- Layout follows the report's one style (as-built `build-followups.md`): exactly one `Source:` line directly under each table, nothing else under it. The section opens with the nextest line, then Table 1 and its source line, then the package table and its source line.
- Lead line, directly under the heading's blank line: `nextest: <compile> compiling, <other> running tests (<compile share>% compiling).`, from the same steps restricted to `step = 'nextest'`; compile share is compile over compile plus other. Omit the line (and its blank line) when the day has no nextest step.
- Table 1, head `Rebuild | Steps | Compile | Other | Total | Share of compile | Share of time`, rows in the order above (a bin with no steps still shows, with `0` steps and `—` elsewhere). Compile is `sum(coalesce(finished_s, 0))`; Other is `sum(duration_s) - Compile`, floored at 0 per step; Total is `sum(duration_s)`; Share of compile is the bin's Compile over all bins' Compile, Share of time the bin's Total over all bins' Total, both whole percent. Durations print with `seconds()`. The cold row carries the cold-build count, time and share, so no separate cold line. Directly under it: `Source: steps with a known crate count; compile is cargo's own "Finished … in" time, other is the rest of the step.`
- Bin labels are built from the edge constants, and code names each bin rather than indexing a label tuple.
- Package table, title line `Edited-crate rebuilds by package (nextest):` above it, head `Package | Steps | p50 | p95 | Compile`: nextest steps in the edited-crate bin, grouped by the package named in `argv` by the first `package(<name>)` match (regex `package\(([^)&|\s]+)\)` on the argv text; none → `(unknown)`). p50 and p95 of `finished_s` with `nearest_rank()`; Compile is their sum. Top 5 by Compile, descending, ties by name. Directly under it: `Source: nextest steps that compiled 1–3 crates, by the first package(…) in the test filter; p50 and p95 are of compile time.` Omit the title, table and source line when there are no such steps.
- Keep functions under the repo's function-length hook; split into small helpers.

**Files:**
- `scripts/buildlog/report.py` — `rebuilds_section` and helpers; the call in `report()`.
- `scripts/buildlog/test_report.py` — tests for the section.

**Seats:** `1 writer + 1 tester` — one module and its test file; nothing splits further.
- `impl` — `scripts/buildlog/report.py`.
- `test` — `scripts/buildlog/test_report.py`, written from this Spec alone: bin edges (0, 1, 3, 4, 49, 50), an empty bin's row, Share of time, the nextest lead line and its omission, exactly one `Source:` line directly under each table, package parsing (`-E package(hana)`, `package(hana) & (test(a) | test(b))`, no package), top-5 ordering, sweep steps and NULL crate counts excluded, and the section sitting directly after `### Waiting`.

**Constraints from prior phases:** none.

**Acceptance gate:** the Test command green and the Lint command at `0 errors, 0 warnings`. `TZ=America/Los_Angeles scripts/buildlog/buildlog report 2026-10-06` shows the section; its nextest line reads about 65% compiling, and `hana` heads the package table.

### Phase 2 — The build-folder wait separates a seat's own queue from waiting on another seat  · status: todo

#### Work Order

**Worktree:** `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report`.

**Goal:** the Waiting table replaces its one `Build-folder turn` row with two: `Build-folder turn, behind another seat` and `Build-folder turn, behind its own call`, so the first measures real blocking.

**Spec:**
- The cargo token is held per delegate session (`verify.sh` acquires it on the session's board), so a call's token wait can only be caused by calls with the same non-null `delegate_session`.
- For each call on the day with `token_wait_s > 0`: its wait interval is `[started_at + wait_s - token_wait_s, started_at + wait_s]`. Each other call of the same `delegate_session` with an `ended_at` holds the token over `[started_at + wait_s, ended_at]` (holders may start the day before; fetch calls whose `ended_at` falls on the day or later). Overlap seconds with holders of the same `seat` count as own; overlap with any other seat counts as another seat. Any part of the wait no holder covers counts as another seat, so no wait is hidden. Cap the two parts at the call's `token_wait_s`.
- A call with no `delegate_session` puts its whole wait in the another-seat row.
- Each row goes through the existing `wait_row()` with that call's part as the duration (worktree as owner and name, as now); `Waited` counts calls with a positive part in that row. The other-seat row comes first.
- Add a line under the Waiting table: `Build-folder turn: a seat's own calls run one at a time, so waiting behind its own call adds no delay; behind another seat does.`
- Keep functions under the function-length hook.

**Files:**
- `scripts/buildlog/report.py` — `waiting_section()` and a new attribution helper.
- `scripts/buildlog/test_report.py` — tests for the split; update the existing Waiting tests to the two row labels.

**Seats:** `1 writer + 1 tester` — one module and its test file.
- `impl` — `scripts/buildlog/report.py`.
- `test` — `scripts/buildlog/test_report.py`, from this Spec alone: a wait fully behind the same seat, fully behind another seat, split across both, with an uncovered remainder, a call with no delegate session, a holder from another session ignored, a holder that started the previous day, and both rows present when nothing waited.

**Constraints from prior phases:** Phase 1 put `### Rebuilds` directly after `### Waiting`; leave its order alone.

**Acceptance gate:** the Test command green and the Lint command at `0 errors, 0 warnings`. On `TZ=America/Los_Angeles scripts/buildlog/buildlog report 2026-10-06`, the own-call row holds most of the day's build-folder wait (the 2026-10-05/06 analysis found about three quarters).
