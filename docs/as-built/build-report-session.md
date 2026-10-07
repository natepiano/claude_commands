# Build report session

## What it is

The 4-hourly build report (`/builds` with no argument) runs in the build-report session, not in natedev's `/watcher` session. A notifier instance, `report-builds`, sends the report prompt to the build-report session every 240 minutes, and that session shows the `/builds` output unchanged. The user asked for this on 2026-10-06 and wanted that session on Sonnet 5.5 at xhigh effort; the session's model is set on the session itself, and no command selects it.

## How it works

`report-builds` is an ordinary instance of the session notifier (`docs/as-built/session-notifier.md`). Its `conf`:

| Field | Value |
| --- | --- |
| `TARGET` | `session:<id>`, the build-report session's `$CLAUDE_CODE_SESSION_ID` |
| `EVERY` | `240` |
| `FROM` | `report-builds` |
| `COMMAND` | `Scheduled report (builds, every 4 hours): run /builds with no argument and show its output unchanged. Do nothing else for this message.` |

There is no `CHECK`, no hold and no alignment, so the clock is the notifier's default: the next whole minute, 240 minutes out.

The build-report session owns the retarget. The "Owner session" section, last in `commands/builds.md`, holds the one command that points the schedule at the running session (`notifier.sh new report-builds --to "session:$CLAUDE_CODE_SESSION_ID" --every 240 --from report-builds --command '<the text above>'`), then `notifier.sh status report-builds`.

`/watcher` STEP 5 (natedev only) runs `notifier.sh status report-builds` and nothing else. STEP 6 reports the next send from that status. The Standing reports rule in `commands/watcher.md` tells a watcher to run what a `report-<name>` message asks, show its output unchanged, and do nothing else, and points to `commands/builds.md` for `report-builds`.

## Invariants

- The watcher never retargets `report-builds`. The retarget command lives once, in `commands/builds.md`.
- The report text stays exactly `Scheduled report (builds, every 4 hours): run /builds with no argument and show its output unchanged. Do nothing else for this message.`
- A repeated `new` on an existing instance keeps the clock: it rewrites `conf` and leaves `state` (`NEXT_DUE`, `ENABLED`) alone, and prints nothing.

## Calibration and gotchas

- **A session restart changes the session id.** The owner re-runs the Owner session command, which points `TARGET` at the new id.
- **The owner session must be live at the due time.** Otherwise the tick logs `skip session not running` in `fire.log`, and the skip uses up the slot: the next try is four hours later.
- **Re-run the whole command.** `new` writes `conf` from the flags given alone, so a shortened command resets every field it leaves out to its default.
- **Verify against `notifier.sh`.** `cmd_new` (conf rewritten, state kept when `state` exists) and `instance_tick` (target resolved through `sessions.py socket` on every send) are the source for these rules.

## Why

STEP 5 used to create and retarget `report-builds` to the watcher's own session. A retarget on every `/watcher` run, in a session that is re-run after each compaction, would have pulled the report back to natedev each time. Moving the command to the build-report session and reducing STEP 5 to a status read keeps the report where the user wants it, and a `/watcher` run cannot change that.
