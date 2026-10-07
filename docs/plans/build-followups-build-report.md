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

#### Work Order

**Goal:** `report-builds` sends its 4-hourly `/builds` to the build-report session, and nothing in `/watcher` moves it back.

**Spec:**
Why (natedev, 2026-10-06, from the user's brief): `report-builds` today targets natedev's session (`session:96b016bf-…`) and the report must run only in the build-report session (Sonnet 5.5, xhigh). `commands/watcher.md` STEP 5 re-runs `notifier.sh new report-builds --to "session:$CLAUDE_CODE_SESSION_ID" …` in the watcher session, so every `/watcher` run, compaction included, would pull the report back. Scope note: STEP 5's change is the director's decision (approved by natedev 2026-10-06), not a user constraint.

1. `commands/watcher.md` STEP 5: replace the `new` command with `zsh ~/.claude/scripts/message/notifier.sh status report-builds` and one sentence: the build report belongs to the build-report session, so the watcher never retargets it. STEP 6 keeps "the build report's next send from STEP 5", read from that status.
2. `commands/watcher.md`, Standing reports rule: "each is a `~/.claude/scripts/message/notifier.sh` instance named `report-<name>`, made as in STEP 5" now points to `commands/builds.md` for `report-builds`. Everything else in the rule is unchanged.
3. `commands/builds.md`: add a short section "Owner session" after the empty-argument paragraphs: the one command that points the schedule at the running session, `zsh ~/.claude/scripts/message/notifier.sh new report-builds --to "session:$CLAUDE_CODE_SESSION_ID" --every 240 --from report-builds --command 'Scheduled report (builds, every 4 hours): run /builds with no argument and show its output unchanged. Do nothing else for this message.'`, then `zsh ~/.claude/scripts/message/notifier.sh status report-builds`. Say that a repeated `new` retargets without moving the clock, and that a session restart changes the id, so the owner re-runs it.

Not the seat's: the live retarget and its `status` check are the unit director's, run after the seat's edits.

**Files:**
- `commands/watcher.md` — STEP 5 shows status only; Standing reports pointer
- `commands/builds.md` — new "Owner session" section holding the retarget command

**Seats:** 2 writers; no test lane, so the `test` seat opens as a writer. The two files are independent and each seat's text is fixed by the Spec.
- `impl` — `commands/watcher.md`
- `test` — `commands/builds.md`; opens as `impl`

**Constraints from prior phases:** none.

**Acceptance gate:** Both files read as specified and `grep -n "report-builds" commands/watcher.md commands/builds.md` shows no `new` command in watcher.md and exactly one in builds.md; the Merge tests above are green; after the checkpoint the director runs the Owner-session command in this session and `notifier.sh status report-builds` shows `session:$CLAUDE_CODE_SESSION_ID` with `next_due` still 23:54 EDT.
