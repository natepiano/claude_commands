# Build report session

## What it is

The build report (`/build:report` with no argument) runs only on demand: a session shows it when the user or a teammate asks. No notifier schedules it. The build-report session is the usual place to ask, and its model is set on the session itself; no command selects it.

## Invariants

- No session creates a `report-builds` notifier instance, and `/watcher` reads no build report schedule.
- `commands/build/report.md` says the report is on demand only, under "On demand only".

## Why

From 2026-10-06 a `report-builds` notifier sent the report prompt to the build-report session every 240 minutes, and `/watcher` STEP 5 read its status. On 2026-10-16 the user asked: "stop having the builder report be automatic - i want to only have it called on demand". The instance was removed, the Owner session command that created it was dropped from `commands/build/report.md`, and the watcher step and the conversation pause's "build report" wording went with it.
