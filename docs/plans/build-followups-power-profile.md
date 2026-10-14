# Power profile: the build log records natedev's power profile, and the report compares builds across each change

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Each 60 s machine sample records natedev's power profile and when it was set. `/build:report` gets a Power profile section that compares like-with-like build steps before and after every profile change the samples show.

> **As-built disposition: amend** `docs/as-built/buildlog-memory-stalls.md` (the sampler and the samples table) and `docs/as-built/build-targets.md` (the report section)

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-13, EDT, the user to build-report-unit directly: "i turned on this 'powerprofilesctl set performance' we were running in power-saver or some other mode - i just turned it on 5 minutes ago so you should mark this in your build analysis somehow so you can treat this as an inflection point to measure against"

Measured 2026-10-13 at 22:41 EDT: `powerprofilesctl get` prints `performance`. `/var/lib/power-profiles-daemon/state.ini` (mode 644, 168 bytes) holds `Profile=performance` and `CpuDriver=amd_pstate`, and its mtime is 22:29:56 EDT, the moment of the switch. `PlatformDriver=placeholder`, so `/sys/firmware/acpi/platform_profile` is not driven by the daemon and does not show the change. NixOS sets no profile, so the state file survives a reboot. The profile before the switch was not recorded.

2026-10-13, the showrunner (natedev), approving both parts: "(1) The sampler reads the profile from a file (the power-profiles-daemon state file whose mtime you already used, or /sys/firmware/acpi/platform_profile), never by forking `powerprofilesctl` or opening D-Bus every 60 s; say in the notice what the read costs. (2) Store it the simplest way to query: one column on each sample is fine; no separate hand-kept marker for this switch, the samples are the record. Backfill nothing before the first sample that carries it; the report labels that side "before 2026-10-13 22:29:56 EDT (profile not recorded)". (3) The before/after section compares like with like only (same step kind, caller and rebuild class), shows the count on each side, and prints no difference for a row with too few runs to mean anything, as the Test builds baseline rows do. It keys on every profile change the samples show, not on this one date."

## Design

**The sample.** `sample()` reads `/var/lib/power-profiles-daemon/state.ini` once: one `stat` and one read of a file under 1 KiB, no process and no D-Bus. Each sample gains two fields:

- `power_profile`: the `Profile=` value under `[State]` (`performance`, `balanced`, `power-saver`).
- `power_profile_since`: the file's mtime as UTC ISO, the moment the daemon last wrote it.

A missing or unreadable file, or one with no `Profile=` line, gives `null` for both, and the sample is still written. That covers the Mac and any machine without the daemon. The second field is what lets the samples alone carry the switch time: the first sample written after this ships says `performance` since 22:29:56 EDT, so no hand-kept marker is needed (the showrunner allowed one column; the second comes from the same `stat`, unit director's call, one-line revert).

**The index.** `SAMPLE_COLUMNS` gains `power_profile TEXT` and `power_profile_since TEXT`. `SCHEMA_VERSION` goes up by one, which rebuilds the index from the JSON lines. Older samples have neither field, so both columns are NULL there. Nothing is backfilled.

**Change points.** `profile_changes(connection)` reads natedev's samples in time order and returns one `ProfileChange(at, profile)` per run of samples with the same non-NULL profile, where `at` is the first sample's `power_profile_since`. If `power_profile_since` is NULL, `at` is that sample's `at`. A later sample with the same profile and a newer `power_profile_since` is the daemon rewriting its file, not a change, and is ignored. The period before the first change point is "profile not recorded".

**The report section.** `### Power profile` follows Test builds:

```
### Power profile

Profile now: performance, since 2026-10-13 22:29:56 EDT.

Before 2026-10-13 22:29:56 EDT (profile not recorded) vs after (performance):

| Kind | Caller | Rebuild | Before runs | Before p50 | After runs | After p50 | Change |
|---|---|---|--:|--:|--:|--:|--:|
| nextest | verify.sh (agents) | edited crate (1–3 crates) | 412 | 1.6 min | 37 | 1.2 min | −25% |
| clippy | verify.sh (agents) | cascade (4–49 crates) | 61 | 2.1 min | 4 | 1.4 min | — |
Source: natedev steps that passed, the 7 days before the change against the time since, up to the next change; p50 is the nearest rank; Change is shown only with at least 10 runs on each side. Load and time of day are not matched.
```

- One comparison per change point whose `at` falls in the 7 days ending at the end of the report day, newest first.
- **Before:** steps that started in the 7 days before the change point, or since the previous change point when that is later. **After:** steps from the change point up to the next change point or the end of the report day.
- **Rows:** natedev steps (`natedev_step_predicate`) with `status = 0`, `step <> 'sweep'`. Each row is one (step kind, caller, rebuild class). Rebuild class is `rebuild_bin(crates_compiled)`, or `—` where `crates_compiled` is NULL. Caller is `caller_label`. A row appears only when it has runs on both sides, the busiest after-side first.
- **Measure:** p50 of `duration_s`, by `nearest_rank`. **Change:** `(after − before) / before` as a whole percent with a sign. It is shown only when both sides have at least `MIN_PROFILE_RUNS = 10` runs; otherwise `—`.
- **Labels:** a side's label is its profile. The side before the first change point reads `before <local time> (profile not recorded)`.
- **No change in the window:** only the `Profile now:` line. **No sample has a profile yet:** the section is omitted.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-build-report`, branch `build-followups-build-report` (unit `build-report-unit`). Name both in every dispatch.
- **Project started:** 2026-10-14T02:55:00+00:00
- **Stack:** Python 3 standard library only, checked by basedpyright.
- **Layout:** `scripts/buildlog/` (`sample.py`, `index.py`, `report.py`, tests), `commands/build/report.md`, `docs/as-built/`.
- **Key files:**
  - `scripts/buildlog/sample.py`: path constants at 13, `Unmeasured` 24, `parse_boot_id` 82, `sample` 116 (the `record` dict at 130).
  - `scripts/buildlog/index.py`: `SCHEMA_VERSION` 33, `SAMPLE_COLUMNS` 224, the samples table at 274, `add_sample` insert at 489.
  - `scripts/buildlog/report.py`: `ON_DAY` 65, `seconds` 149, `nearest_rank` 173, `table` 364, `fetch` 376, `caller_label` 397, `rebuild_bin` 578, `test_builds_section` 862, `day_range` 898, `report` 1530 (where sections are assembled). `natedev_step_predicate` is in `mac_outcome.py`. Local times print through `user_zone` (`scripts/lib/user_zone.py`).
- **Test lanes:** `scripts/buildlog/` → `test_sample.py`, `test_index.py`, `test_report.py`.
- **Build:** none; the code is interpreted.
- **Test:** from the worktree root, `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'`.
- **Lint:** `basedpyright <files>`: a pass ends `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - `buildlog sample` never forks a process or opens D-Bus, and a missing state file never stops a sample.
  - Old samples stay as written; nothing is backfilled.
  - The `scripts/buildlog/buildlog` wrapper runs the installed copy. Worktree code is checked with `~/.claude/scripts/lib/py scripts/buildlog/cli.py` and `BUILDLOG_DIR` on a scratch copy of the log, never against the live log.
  - Saved run output stays under a few GB; 300 GiB stays free on `/`.

## Phases

### Phase 1 — Each sample records natedev's power profile, and the report compares builds across every profile change  · status: done

#### As-built

- `sample.py` holds `POWER_PROFILE_STATE = Path("/var/lib/power-profiles-daemon/state.ini")` (patched in tests like the other paths), `PowerProfile(name: str, since_epoch: float)` and `read_power_profile(path) -> PowerProfile | Unmeasured`. The read opens the file once, takes `os.fstat` on the descriptor, then reads that same handle. It scans lines for `Profile=` under `[State]`. It returns `Unmeasured.VALUE` on `OSError`, a missing section, a missing or empty value, undecodable text, or a file of 1 KiB or more.
- Each sample line carries `power_profile` and `power_profile_since` (`store.utc_iso(since_epoch)`), both `None` when unmeasured. No process, no D-Bus; a missing file (the Mac) never stops a sample, and nothing is backfilled.
- `index.py` appends the two columns to `SAMPLE_COLUMNS` with descriptions and bumps `SCHEMA_VERSION` (now 15); an old line without them indexes as NULL.
- `report.py` has `ProfileChange(at: str, profile: str)`, `profile_changes(connection)`, `power_profile_section(connection, day)` and `MIN_PROFILE_RUNS = 10`, placed after `test_builds_section` in `report`. A change is the first natedev sample of each distinct profile run, timed at its `power_profile_since`; a rewrite of the same profile with a newer mtime is not a change.
- The section prints `Profile now: <profile>, since <YYYY-MM-DD HH:MM:SS zone>.`, then one block per change inside the last 7 days. Each block is a table of passing natedev steps (Mac and failed steps excluded) with one row per kind, caller and rebuild class that has runs on both sides, showing before and after counts and p50 (nearest rank). Change is `—` below 10 runs on either side. The before window is 7 days, cut at the previous change; its profile reads "profile not recorded" when none precedes. The after window runs to the next change. A comparison with no shared row prints one line instead of an empty table. The section is omitted when no sample carries a profile; a change older than 7 days gives only the `Profile now:` line.
- Read cost measured at 21.9 µs per call.

**Files:**
- `scripts/buildlog/sample.py` — profile constant, `PowerProfile`, `read_power_profile`, the two sample fields.
- `scripts/buildlog/index.py` — the two sample columns and the schema version.
- `scripts/buildlog/report.py` — `ProfileChange`, `profile_changes`, `profile_step_durations`, `power_profile_section`.
- `scripts/buildlog/test_sample.py`, `test_index.py`, `test_report.py` — state-file fixtures, index round trip, change points and section cases.
- `commands/build/report.md` — one sentence describing the Power profile section.

**Gotchas:** The daemon replaces the state file by rename, so a path stat followed by a separate read can pair a new profile with the old mtime; keep one open, fstat on the descriptor, one read of the same handle. basedpyright exits 3 in every worktree because the configured `.venv` is absent while diagnostics are clean.

**Ruled out:** forking `powerprofilesctl` or opening D-Bus every 60 s (cost and a process per sample); a hand-kept marker for the switch (the file mtime already records it); `/sys/firmware/acpi/platform_profile` (this daemon does not drive it, `PlatformDriver=placeholder`).
