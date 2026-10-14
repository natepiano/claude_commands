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

**Depends on:** none

#### Work Order

**Goal:** the build log records natedev's power profile once a minute, and `/build:report` shows how like-with-like build steps changed across each profile change. Source: the user, 2026-10-13 ("treat this as an inflection point to measure against"); the showrunner's three design calls above.

**Spec:**
- `sample.py`: a `POWER_PROFILE_STATE = Path("/var/lib/power-profiles-daemon/state.ini")` constant, overridable for tests the way the other paths are patched. `read_power_profile(path) -> PowerProfile | Unmeasured`, where `PowerProfile(name: str, since_epoch: float)`. It does one `os.stat` and one `read_text`, finds `Profile=` under `[State]` with a plain line scan, and returns `Unmeasured.VALUE` on `OSError`, a missing section or a missing or empty value. `sample()` writes `power_profile` and `power_profile_since` (`store.utc_iso(since_epoch)`), or `None` for both.
- `index.py`: the two columns at the end of `SAMPLE_COLUMNS`, with descriptions ("power-profiles-daemon profile; NULL when not recorded", "UTC ISO mtime of the daemon's state file: when the profile was set"). `SCHEMA_VERSION` + 1.
- `report.py`: `ProfileChange` (NamedTuple: `at: str`, `profile: str`), `profile_changes(connection)`, `power_profile_section(connection, day)`, and `MIN_PROFILE_RUNS = 10`, all as in the Design. The section goes after `test_builds_section` in `report`. Times print in the user's zone as `YYYY-MM-DD HH:MM:SS <zone>`.
- `commands/build/report.md`: one sentence saying the Power profile section compares passing natedev steps before and after each profile change in the last 7 days.
- Tests:
  - `test_sample.py`: state-file fixtures for `performance`, `power-saver`, a missing file, a file without `[State]` and an empty `Profile=`. A full `sample()` run with a fixture state file writes both fields, and without one writes `null` for both and still writes the sample.
  - `test_index.py`: a sample line with both fields indexes them; an old line without them gives NULL.
  - `test_report.py`:
    - Change points: NULL samples then `performance` gives one change at its `power_profile_since`. A rewrite with the same profile and a newer `since` is not a change. `performance` → `balanced` gives two.
    - The section: one row per kind, caller and rebuild class; counts on both sides; `—` below 10 runs on either side; a row with no before-side runs left out; the "profile not recorded" label; the before window cut at the previous change; a change outside the 7 days gives only the `Profile now:` line; no profiled sample omits the section; Mac steps and failed steps excluded.

**Files:**
- `scripts/buildlog/sample.py`
- `scripts/buildlog/index.py`
- `scripts/buildlog/report.py`
- `scripts/buildlog/test_sample.py`
- `scripts/buildlog/test_index.py`
- `scripts/buildlog/test_report.py`
- `commands/build/report.md`

**Seats:** 1 writer + 1 tester.

**Acceptance gate:** the buildlog lane passes, and basedpyright reports 0 errors and 0 warnings on every changed file. A live check runs the worktree's `cli.py sample` with `BUILDLOG_DIR` set to a scratch folder and shows the written sample carries `performance` and a since time of 2026-10-14T02:29:56Z. It times 1000 calls of `read_power_profile` on the real state file and gives the cost per read for the showrunner's notice. A scratch copy of the live log, with that scratch sample appended, then gives a report whose Power profile section shows the 22:29:56 EDT change with before and after rows.
