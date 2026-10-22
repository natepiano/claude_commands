# Style showrunner follow-ups

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Follow-up phases for the style showrunner after its as-built (`docs/as-built/style-showrunner.md`).

> **Production: build-followups** — unit `style-unit`; production doc `docs/plans/build-followups-production.md`

## Source

2026-10-22, cargo-liner-style (style showrunner of cargo-liner), defect report: "When one sync cue starts all four style units' refill at the same time, style_plan selection gives two or three units overlapping files. Each unit's judges then see the other units' blocking reservations and yield, so nobody judges the file." Seen twice: names-unit-8 and types-unit-8 on cargo-port `http/client.rs`; names-unit-9, types and values on `lint/runtime/command.rs`.

style-unit checked the code: selection already skips files on another unit's open ticket (`unit_files.unit_file_holders`), but `style_plan.next_phase` reads `busy_files`, selects and calls `ticket_add` with no lock across the three steps, so units that start together all read the ledger before any of them adds a ticket.

2026-10-22, natedev (showrunner of build-followups): "go, as a new phase, current (it fixes a wrong result). One lock per style production from the busy_files read to ticket_add, with the two-refills-at-once test. No judge tie-break rule. Keep the lock short: hold it only for the read, select and add, never across a judge run."

## Delegation Context

- **Project:** `~/.claude` (claude_commands), worktree `/home/natepiano/worktrees/claude-build-followups-style`, branch `build-followups-style`. Every Work Order runs in that worktree.
- **Project started:** 2026-10-16T19:43:43.983+00:00
- **Stack:** Python ≥3.12 through `~/.claude/scripts/lib/py`; basedpyright 1.39; stdlib `sqlite3`, `fcntl`; markdown docs.
- **Layout:** `scripts/style/` — style ledger and refill, tests beside them; `docs/as-built/style-showrunner.md` — the style showrunner as-built.
- **Key files:**
  - `scripts/style/style_plan.py` — `busy_files` :264-268, `_select_phase` :1564-1733 (queued-ticket assign :1570-1589, `ticket_add` :1693 and :1727, `ticket_assign` :1730), `next_phase` :1735-1806 (first pass: `berth_board.read_board` :1783, `busy_files` :1786, `_select_phase` :1787; second pass after `wait_for_sync` :1793-1803), `queue` command :1825-1844 (read only).
  - `scripts/style/style_ledger.py` — `ledger_location` :4559-4564 (one ledger per main repo, `<STYLE_LEDGER_STATE_DIR or ~/.local/state/style>/<main repo name>/ledger.sqlite`), `open_ledger` :4567-4569, `preview_ledger` :4572-4584, `ticket_add` :1696-1785 (commits in its own `BEGIN IMMEDIATE` transaction), `ticket_open` :2114-2118 (`assigned` and `checkpointed` only).
  - `scripts/style/unit_files.py` — `unit_file_holders` :51-68 (other units' open-ticket files :64-66).
  - `scripts/production/proposal_issue.py` — `_issue_lock` :155-163, the repo's `fcntl.flock` pattern (a lock file opened `"a"`, `LOCK_EX`, held for the `with` body).
  - `scripts/style/test_style_plan.py` — `StylePlanTests.setUp` :47 (temp root, `STYLE_LEDGER_STATE_DIR` :58), `next_phase` tests :220-240, :553, :2367-2395.
  - `docs/as-built/style-showrunner.md` — the refill section to update.
- **Test lanes:** `scripts/style/` — `test_*.py` beside each module.
- **Build:** none (Python and markdown).
- **Test:** from the worktree root, `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest <modules>`; then the merge gate `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the root.
- **Lint:** `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style <files>`; pass is `0 errors, 0 warnings`.
- **Invariants:**
  - basedpyright reports zero errors and zero warnings; no file-level ignores; no `Any`; line-level ignores only as a last resort (user, CLAUDE.md).
  - Every module has `from __future__ import annotations`; discarded results are written `_ = …`; test classes use `@final`/`@override` (repo convention).
  - Never write the live ledger or a live production doc; read them only from a copy (style-unit rule).
  - The forbidden-words list (`~/rust/nate_style/rust/forbidden-words.md`) applies to code, comments, docs and commits (user).

## Phases

<!-- PHASE NUMBERING — binds every command that edits this doc.
     N is a bare integer, contiguous, in execution order. Never a letter suffix.
     Phases 1-42 are in docs/as-built/style-showrunner.md; this doc continues the same numbers. -->

### Phase 43 — style units that refill at the same time never get the same file · status: done

#### As-built

- `style_ledger.refill_lock(repo: Path) -> Generator[None]` is a `@contextmanager` beside `open_ledger`. It opens `refill.lock` with mode `"a"` in the folder of `ledger_location(repo).database`, and makes the folder when it is absent. It holds `fcntl.flock(..., fcntl.LOCK_EX)` for the `with` body. One ledger gives one lock per style production. The lock blocks and has no timeout.
- `style_plan.next_phase` holds `refill_lock(repo)` across each of its two selection passes, in two separate `with` blocks. Each block covers `berth_board.read_board`, `busy_files`, `_select_phase` (with its `ticket_add` and `ticket_assign`), `clear_sync_wait` and the return. Both ticket writes commit before the lock is released. So the next unit's `busy_files` sees those files as busy through `unit_file_holders`, and two units that refill at the same time get disjoint files.
- The rule catalog read (`rules.load_rules`) runs once before the first lock block and serves both passes. `ledger.wait_for_sync` runs between the two blocks, outside the lock. The `queue` command takes no lock.

**Files:**
- `scripts/style/style_ledger.py` — `refill_lock`.
- `scripts/style/style_plan.py` — the two lock blocks in `next_phase`.
- `scripts/style/test_style_ledger.py` — `test_refill_lock_blocks_another_worktree_until_the_body_exits`: a second worktree waits on the lock until the first `with` body exits.
- `scripts/style/test_style_plan.py` — `test_concurrent_refills_read_committed_tickets_and_choose_disjoint_files`: two `next_phase` calls start together, on each selection pass, and get disjoint files. The test fails when the lock blocks are removed.
- `docs/as-built/style-showrunner.md` — the refill lock paragraph in refill selection.

**Gotchas:** `rules.load_rules` takes 4.5 s warm and 21 s cold on cargo-liner. Keep it, and any other read of no shared state, outside the refill lock: work inside the lock delays the refill of every other unit. The lock never covers a judge run or any work after `next_phase` returns.

**Ruled out:** a judge yield tie-break rule — with the lock, two units cannot select the same file.
