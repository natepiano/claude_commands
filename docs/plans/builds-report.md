# builds-report

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 10:07 EDT

Own what the build log records and what its reports show: the build log, /build:report, /build:mac_report, and the /build:queue and /build:status tables. The user has build report changes coming; they come to this unit. Until they arrive, stand by.

## Plan

**Status: standing by — no phases yet.** The Source gives no work to do now: the user's build report changes have not arrived. This plan has no phases until they do, so `/unit:direct` has nothing to run.

When the user's changes arrive (from the user or through the showrunner):

1. Record the user's words, with the date and time, as a new entry in Source.
2. Write the phases for that work here: one Work Order for each phase, with the worktree `/home/natepiano/worktrees/claude-natedev-builds-report` and branch `builds-report`.
3. Send the phased plan to the showrunner. Start `/unit:direct docs/plans/builds-report.md` only after the showrunner approves it.

The start point for that work is the surface in this unit's Owns cell in `docs/plans/builds-production.md`, and these as-built docs: `docs/as-built/build-followups.md`, `docs/as-built/buildlog-memory-stalls.md`, `docs/as-built/buildlog-tests-per-edit-rust-release.md` and `docs/as-built/build-report-session.md`.
