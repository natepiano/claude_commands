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

**Depends on:** none

#### Work Order

**Goal:** two or more style units of one style production that run `style_plan.py next-phase` at the same time get tickets on disjoint files.

**Spec:**
- Add `refill_lock(repo: Path) -> Generator[None]` (a `@contextmanager`) to `style_ledger.py`, beside `open_ledger`. It opens `refill.lock` in the folder of `ledger_location(repo).database` (one ledger, so one lock, per style production), creating the folder when absent, with mode `"a"`, and holds `fcntl.flock(..., fcntl.LOCK_EX)` for the `with` body. Follow `proposal_issue._issue_lock`. The lock blocks; it has no timeout.
- In `style_plan.next_phase`, hold `refill_lock(repo)` around each selection pass: from `berth_board.read_board` through `busy_files`, `_select_phase` and its return. Two passes, two separate `with` blocks: the first around :1783-1791, the second around :1795-1803. `wait_for_sync`, `synced_tip`, `discard_finished`, the open-ticket resume path and everything before :1783 stay outside the lock. So a second unit reads `busy_files` only after the first unit's `ticket_add` and `ticket_assign` are committed, and its selection skips those files through `unit_file_holders`.
- The lock never spans a judge run or any work after `next_phase` returns. The `queue` command is read only and takes no lock.
- No judge yield tie-break rule (showrunner, 2026-10-22): with the lock, two units cannot select the same file.
- Update `docs/as-built/style-showrunner.md` where it describes refill selection: one lock per style production (`refill.lock` beside the ledger) covers the board read, the busy-files read, selection and the ticket add of each refill pass.

**Files:**
- `scripts/style/style_ledger.py` — `refill_lock`.
- `scripts/style/style_plan.py` — the two lock blocks in `next_phase`.
- `scripts/style/test_style_ledger.py` — `refill_lock` test.
- `scripts/style/test_style_plan.py` — the two-refills-at-once test.
- `docs/as-built/style-showrunner.md` — refill lock line.

**Workers:** 1 writer + 1 tester; the code and its tests split by file.
- `impl`: `scripts/style/style_ledger.py`, `scripts/style/style_plan.py`, `docs/as-built/style-showrunner.md`.
- `test` opens as `test`: `scripts/style/test_style_ledger.py`, `scripts/style/test_style_plan.py`. From the Spec alone: (1) `refill_lock` — a second process (or thread with its own file handle) that takes the lock waits until the first `with` body exits; the lock file is beside the ledger under `STYLE_LEDGER_STATE_DIR`. (2) Two refills at once — in the `StylePlanTests` fixture, two units of different families whose candidates share a file; start both `next_phase` calls together (threads or processes, each with its own ledger connection), with the first unit's selection slowed (patch `_select_phase` or `busy_files` to wait on an event after its read) so that without the lock both would read before either adds; assert both return, the second unit's ticket files do not overlap the first's, and the second's `busy_files` ran after the first's `ticket_add`. Without the lock the test must fail.

**Constraints from prior phases:**
- `unit_file_holders` already counts another unit's `assigned`/`checkpointed` ticket files as busy (phase 42 as-built); the lock only orders the reads after the writes.
- `ticket_add` and `ticket_assign` each commit their own transaction, so both are committed before the `with` block exits.

**Acceptance gate:**
- `env -C /home/natepiano/worktrees/claude-build-followups-style PYTHONPATH=/home/natepiano/worktrees/claude-build-followups-style/scripts/style:/home/natepiano/worktrees/claude-build-followups-style/scripts HERDR_BIN=/nonexistent/herdr ~/.claude/scripts/lib/py -m unittest test_style_ledger test_style_plan` passes.
- The two-refills-at-once test fails with the lock blocks removed from `next_phase`, and passes with them.
- `basedpyright -p /home/natepiano/worktrees/claude-build-followups-style scripts/style/style_ledger.py scripts/style/style_plan.py scripts/style/test_style_ledger.py scripts/style/test_style_plan.py` reports `0 errors, 0 warnings`.
- `~/.claude/scripts/lib/py scripts/production/merge_gate.py --tests-only` from the worktree root passes.
