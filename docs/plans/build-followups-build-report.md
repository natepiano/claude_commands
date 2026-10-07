# build-report

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The build report (`/builds`, every 4 hours) runs in the build-report session, not the showrunner session.

> **Production: build-followups** — unit `build-report-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-06 17:00 PDT

for the build report - when it is running, i want it also be a sonnet 5.5 xhigh - switch it so the build report only runs there and not here in the showrunner session

## Delegation Context

- **Project:** `~/.claude` — the user's Claude Code configuration (commands, scripts, docs); this unit moves the 4-hourly build report out of natedev's session.
- **Project started:** 2026-10-07T00:04:36.121+00:00
- **Stack:** Markdown command files; `scripts/message/notifier.sh` (zsh) is called, never edited.
- **Layout:** `commands/watcher.md`, `commands/builds.md`, `scripts/message/notifier.sh` (read only: it is what the commands call), `~/.local/state/notifier/report-builds/` (live notifier state, never edited by hand).
- **Key files:** `commands/watcher.md` — natedev's standing-session command; its STEP 5 creates `report-builds`. `commands/builds.md` — what the report runs. `scripts/message/notifier.sh` — `new` on an existing instance rewrites its conf and keeps its clock.
- **Test lanes:** none — documentation-only phase.
- **Build:** none (no code).
- **Test:** `python3 -m unittest discover -s scripts/buildlog -p 'test_*.py'` (the production's Merge tests; no Python changes expected).
- **Lint:** `basedpyright scripts/buildlog` — passes on its `0 errors, 0 warnings` line (it exits 3 because pyrightconfig names a `.venv` no checkout has).
- **Invariants:** Command files follow `~/.claude/commands/succinct_style.md`. The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to every word written. The report's text stays exactly `Scheduled report (builds, every 4 hours): run /builds with no argument and show its output unchanged. Do nothing else for this message.`

## Phases

### Phase 1 — The build report runs in the build-report session  · status: done

#### As-built

The `report-builds` notifier instance sends its 4-hourly `/builds` (every 240 min, from `report-builds`) to the build-report session, whose id the owner set with `notifier.sh new report-builds --to "session:$CLAUDE_CODE_SESSION_ID" …`. `/watcher` STEP 5 only runs `notifier.sh status report-builds` and says the build report belongs to the build-report session, so a watcher run never retargets it. The command that points the schedule at the running session lives once, in `commands/builds.md`.

**Files:**
- `commands/watcher.md` — STEP 5 shows the status only; the Standing reports rule points to `commands/builds.md` for `report-builds`
- `commands/builds.md` — "Owner session" section, last in the file, holding the retarget command and the status check

**Gotchas:** A repeated `new` on an existing instance rewrites its conf and keeps its clock (`next_due` unchanged). A session restart changes the session id, so the owner re-runs the Owner session command. `scripts/delegate/implement.sh` is not executable; run it as `bash <path>`.

**Ruled out:** Keeping the retarget in `/watcher` (every run pulls the report back to natedev). Placing the Owner session section before `Otherwise:` in `commands/builds.md` (it breaks the list that follows).
