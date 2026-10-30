# builds-report

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** `/build:queue` opens a pane from the unit-board mod, with no model turn, as `/accounts-board` does; and the build log index stops rewriting its source logs, so a stop cannot lose records.

> **As-built disposition: amend** — `docs/as-built/unit-board.md` (the shared table pane and `/build:queue`); `docs/as-built/build-memory-admission.md` (Queue API); `docs/as-built/buildlog-memory-stalls.md` (index reads old field names)

> **Production: builds** — unit `report-unit`; production doc `docs/plans/builds-production.md`

## Source

2026-10-29 21:00 EDT (the user, to report-unit)

build queue - i need you to make it work  like /accounts-board - so that it can load quickly without going through a skill - if it can share code to do this claude code modifiction that would be fine

2026-10-29 20:58 EDT (the showrunner)

the user-approved rerun sweep (docs/plans/sdlc-rerun-sweep.md, main 4dfc4dce) has 1 row for report-unit. Plan it as a small phase, with a test that stops at the named line and then reruns.

The row: `scripts/buildlog/index.py:689` (Q3). Interrupt the source-log rewrite after it writes only part of the new bytes. The rerun keeps malformed lines during migration and skips them during indexing; the original records have been overwritten and cannot be restored from this file.

## Delegation Context

- **Project:** `~/.claude` — the build log (`scripts/buildlog/`), the build queue (`scripts/build_queue/`) and the unit-board Claude Code mod (`mods/unit-board/`).
- **Ownership:** `mods/unit-board/` belongs to natedev's unit-board-unit. The user asked report-unit for this change and allowed shared code. The showrunner is told before Phase 1 merges, so unit-board-unit sees the shared pane code.
- **Phases 1 and 2 touch no common file.** They run side by side.

## Phase 1 — `/build:queue` is a mod pane

**Goal.** `/build:queue` opens pane `build-queue` (title `Build queue`) at once, with no model turn, the way `/accounts-board` opens `Accounts`. The pane shows the Running table, then the Waiting table, then the totals line and any roster problem, with the same columns, names and times as today's text. It refreshes every 10 s while open. `/build:queue close`, Escape or the close mark closes it. `/build:queue bump <selector>` runs the bump, replies with its one result line, and refreshes an open pane. No model turn in any of these.

**Design.**
- `build_queue.py` gains `--json`: one object with the running rows, the waiting rows, the totals and the roster problems, each cell already formatted in the user's zone as the text tables show it. The text output stays as it is, and the text and JSON come from the same row values, so they cannot disagree. A test proves it.
- The mod answers `command.run` on `{ command: 'build:queue' }`, the same way it answers `showrunner:unit_board`. It runs `[~/.claude/scripts/lib/py, ~/.claude/scripts/build_queue/build_queue.py, "--json"]` and never parses the text table.
- Shared code: the accounts pane lifecycle in `hooks/register.tsx` (open, `loading`, one run per tick, busy guard, 10 s timer, reload restart from `$.ui.panes()`, close, fixed failure reason) becomes one table-pane helper. `/accounts-board` and `/build:queue` both use it. `/accounts-board` behaves exactly as before; its tests stay green unchanged.
- A failed run shows a fixed reason (`Build queue could not be read.`), never process output.
- `commands/build/queue.md` keeps its frontmatter. Its body still runs the script, so a session without the mod still gets the tables; the body also names `/reload-plugins` for a session started before the mod change.

**Tests.** `build_queue.py --json` against the existing fakes: every cell equals the text table's cell. Mod tests in `mods/unit-board/tests/`: open shows `loading` then both tables; empty tables show `empty` rows; refresh; close by command and by Escape; bump replies and refreshes; a failed run shows the fixed reason; a reload with the pane open restarts its refresh; `/accounts-board` tests unchanged.

**Live check.** After `/reload-plugins`, `/build:queue` opens the pane with no model turn, and its rows match `python3 ~/.claude/scripts/build_queue/build_queue.py` run at the same time.

## Phase 2 — the index reads old field names, and never rewrites a source log

**Goal.** `index.py` no longer rewrites a `.jsonl` source log. `migrate_worker_fields` is removed. While it indexes a `step` or `call` record, the index reads the old names (`seat`, `delegate_session`) as the new names (`worker`, `unit_session`), using `FORMER_FIELDS`. The source logs stay append-only, as `store.py` says: "appended and never edited". A stop at any line cannot change a source log.

**Tests.** The test stops the index update at the line the row names: it raises inside indexing after the first record, where the rewrite ran before. Then it reruns the update and checks three things: every record is in the index under its new field names, the source file's bytes are unchanged, and a second rerun adds nothing. An old-name record and a new-name record give the same index row.
