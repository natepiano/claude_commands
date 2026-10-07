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

### Phase 2 — The build-folder wait separates a seat's own queue from waiting on another seat  · status: done

#### As-built

- The `### Waiting` table opens with two rows, always present: `Build-folder turn, behind another seat`, then `Build-folder turn, behind its own call`. Each goes through `wait_row()` with the call's part as the duration and the worktree as owner and name; `Waited` counts calls with a positive part in that row. The one line under the table: `Source: verify.sh calls, memory-gated steps and CI jobs; a seat's own calls run one at a time, so waiting behind its own call adds no delay, behind another seat does.`
- `build_folder_waits(connection, day)` returns `(another_waits, own_waits)`, two `ReportedWait` lists (`tuple[float, str, str, str]`: duration, owner, name, started_at). It reads the day's `tool = 'verify.sh'` calls and the token holders, the verify.sh calls with a `delegate_session` and an `ended_at` on the day or later. `token_holders()` turns each into `TokenHolder(call_id, delegate_session, seat, starts_at, ends_at)` over `[started_at + wait_s, ended_at]`, and holders are grouped by session so each call scans only its own session's.
- The cargo token is held per delegate session, so only calls of the same `delegate_session` can cause a token wait. `attribute_token_wait()` runs only for a positive `token_wait_s` on a call with a session; a call with no `delegate_session` puts its whole wait in the another-seat row.
- `attribute_token_wait(call_id, delegate_session, seat, starts_at, wait_s, token_wait_s, holders) -> TokenWaitAttribution(behind_another_seat_s, behind_own_call_s)` takes the wait interval `[started_at + wait_s - token_wait_s, started_at + wait_s]` and splits it at the boundaries of the overlapping same-session holders other than the call itself. A segment is own only when every holder covering it has the call's seat; every other segment, uncovered included, is another seat. Own is capped at `token_wait_s`, and another seat is the remainder.

**Files:**
- `scripts/buildlog/report.py` — `ReportedWait`, the `TokenHolder` and `TokenWaitAttribution` dataclasses, `call_time`, `token_holders`, `attribute_token_wait`, `build_folder_waits`, and the two rows and `Source:` line in `waiting_section`.
- `scripts/buildlog/test_report.py` — tests for the split (own seat, another seat, both, uncovered remainder, no session, another session's holder, a holder from the previous day), the port-lint exclusion, and the Waiting `Source:` line.

**Gotchas:** the `calls` table also holds port-lint calls, so every query over the call population, counted calls and holders alike, filters `tool = 'verify.sh'`. A NULL seat is coalesced to `(unknown seat)`; session calls with no seat never wait but can hold the token, and a seat waiting behind one counts as another seat. Holders are fetched by `ended_at`, not `started_at`, so a holder that began the previous day is found.

**Ruled out:** counting uncovered wait as own, since that would hide waits with no recorded holder.
