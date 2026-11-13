# Producer role transfer

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A running Claude or Codex session takes a producer role with one command.

> **As-built disposition: amend** — `docs/as-built/message-clarity.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/staff-accessors.md`, `docs/as-built/shutdown-and-restart.md`

## Delegation Context

- **Project:** `~/.claude` scripts — agent sessions, the staff record and message delivery.
- **Stack:** Python 3 with `unittest`; basedpyright with zero errors and zero warnings.
- **Layout:** `scripts/message/` (delivery and the morty mark), `scripts/production/` (staff record and units table),
  `scripts/agents/` (session verbs), `scripts/lib/` (transcripts), `commands/` (operator commands), `docs/as-built/`.
- **Key files:**
  - `scripts/message/morty_identity.py` — the one test for the morty authority mark.
  - `scripts/message/send.py` — stamps each message with the sender native id and the mark.
  - `scripts/production/staff_sources.py` — the units table drops morty's process from its anomaly lines.
  - `scripts/production/staff.py` — the staff record; `update_current_session`, `set_home`, `ensure`.
  - `scripts/agents/stop_resume.py`, `scripts/agents/restart.py`, `scripts/agents/shutdown.py` — producer resume and the `agents` verbs.
  - `scripts/lib/transcripts.py` — the one path to transcript files.
  - `config/models.conf` (`[producers]`), `config/send.conf`.
- **Test lanes:** none (tests sit beside each module as `test_<module>.py`).
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/<folder> -p '<test file>'` from the worktree root.
  Checkpoint gate: `env HERDR_BIN=/nonexistent/herdr python3 ~/.claude/scripts/production/targeted_test.py --checkout <tree>`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output has the `0 errors, 0 warnings` line.
- **Invariants:**
  - Tests use fixtures only: no live staff file, no live config, no live panes (user rule, relayed by sdlc 2026-11-13).
  - No hand edit of the staff record. Each staff change goes through a `staff.py` writer (sdlc).
  - Config changes are limited to the `[producers]` value and the removal of `MORTY_NATIVE_ID` (sdlc).
  - A staff copy that is not the live record grants no authority mark (existing rule for the recovered config value).
  - The branch lands through the merge gate. No push to `sdlc` or `main` (sdlc).

## Phases

### Phase 1 — Morty authority from the staff row  · status: todo

#### Work Order

**Goal:** The `Authority: morty (speaks for the user)` mark follows the producer row named `morty`; `MORTY_NATIVE_ID` is gone.

**Spec:**
- `morty_identity.holder()` returns the `current_session` of the one producer row named `morty` from `staff.snapshot()`.
  It returns `""` when the record is unreadable or absent, or when the row count is not one.
- `morty_identity.matches(native_id)` accepts `session:<id>` and `codex:<id>` and compares `<id>` with `holder()`.
  The staff record holds no family for a producer, so the match does not test the family.
- `send.py` evaluates the mark at each render, as before. A queued message gets the mark of the row at delivery.
- `send.py` drops the `--to morty` native-address exception. Without a row, `morty` is refused as any other name.
- The stderr note for a false `--from morty` reads `send.py: sender 'morty' does not hold the morty staff row`.
- `staff_sources.CodexProcessThreads` reads `holder()` one time for each report and compares each process identity with it.
  `session_staff.morty_thread` is deleted.
- Delete `MORTY_NATIVE_ID` from `config/send.conf`.
- Update the as-built documents and `commands/showrunner/units.md` in the same commit.

**Files:**
- `scripts/message/morty_identity.py` — staff read in place of the config read.
- `scripts/message/send.py` — docstring, `_native_address_matches`, the stderr note.
- `scripts/production/session_staff.py`, `scripts/production/staff_sources.py` — one `holder()` read for each report.
- `config/send.conf` — remove the key.
- `scripts/message/test_send.py`, `scripts/message/test_morty_identity.py`, `scripts/production/test_session_staff.py` — staff fixtures.
- `docs/as-built/message-clarity.md`, `docs/as-built/showrunner-automation.md`, `docs/as-built/staff-accessors.md`, `commands/showrunner/units.md`.

**Workers:** nothing splits; the director writes in no-worker mode.
- `impl` — every file.

**State:** none

**Constraints from prior phases:** none.

**Acceptance gate:** `test_morty_identity.py`, `test_send.py`, `test_session_staff.py`, `test_staff_sources.py` and `test_staff_accessors.py` pass.
The checkpoint gate passes. basedpyright reports zero errors and zero warnings. No file outside this plan names `MORTY_NATIVE_ID`.

### Phase 2 — Catch-up reader for the earlier conversation  · status: todo

#### Work Order

**Goal:** One command prints a producer's earlier conversation from its last compaction, for Claude and Codex transcripts.

**Spec:**
- New module `scripts/agents/catch_up.py`. It finds the transcript by native id through `scripts/lib/transcripts.py` only.
- Codex: read `rollout-*-<id>.jsonl[.zst]` in each home from `codex_transcript_homes()`. Start after the last `compacted` record.
- Claude: read `<id>.jsonl[.zst]` below each `~/.claude*/projects`. Start at the last compaction summary record.
- Print each user message, assistant message and tool call as one block with its time. Shorten each tool output to its first lines.
- `--since-compaction` is the default. `--tail N` prints the last N blocks. A missing transcript prints one line and exits 1.
- The reader streams the file. It does not hold the transcript in memory (morty's transcript is 138 MB).

**Files:**
- `scripts/agents/catch_up.py`, `scripts/agents/test_catch_up.py` — reader and fixture transcripts for the two families.
- `docs/as-built/shutdown-and-restart.md` — name the reader.

**Workers:** nothing splits; the director writes in no-worker mode.
- `impl` — every file.

**State:** none

**Constraints from prior phases:** none.

**Acceptance gate:** `test_catch_up.py` and `scripts/lib/test_transcript_reader_guard.py` pass. The checkpoint gate passes.
basedpyright reports zero errors and zero warnings.

### Phase 3 — `agents transfer NAME`  · status: todo

#### Work Order

**Goal:** A running session that has no staff row takes the named producer role and restarts as that producer.

**Spec:**
The session that takes the role runs `agents transfer NAME`. The steps run in this order:

1. Prove the caller: native id, family, account home and pane. Refuse a caller that has a staff row. Refuse a `NAME` with no producer row.
2. Stop the old producer conversation and close its tab. Keep its transcript.
3. Write `[producers] NAME=<family>:<model>:<effort>` through a publishing writer. `pane_launch._publish_producer` compares the backend with this value, so this write comes before step 4.
4. Set `current_session` and `home` in one staff transaction. The fixed id, the document and the staff queue stay.
5. Move each message kept under the old native id to the staff queue.
6. Print `Authority: NAME is now <native id>` when `NAME` is `morty`, and report the same line as an anomaly to `natedev`.
7. Start a detached process that stops the caller and resumes it through the producer resume path. The resume supplies the name, the tab in the `producers` workspace, the model and the pane record.

Other rules:
- Each step is safe to run again after a failure of a later step.
- First admission of a producer named `morty`, and each staff write that changes the native id of the `morty` row, prints and reports the line of step 6 (sdlc).
- `commands/producer/morty.md`, `brain.md` and `dwight.md` each get a `Transfer` section: run the verb, then read the role command, the role documents and the output of `catch_up.py`, then run `send.py pending`.
- `commands/agents.md` documents the verb.

**Files:**
- `scripts/agents/shutdown.py` — the `transfer` verb.
- `scripts/agents/stop_resume.py`, `scripts/agents/restart.py` — the transfer steps on the producer resume path.
- `scripts/production/staff.py` — one writer for `current_session` with `home`; the authority line on a native id change.
- The `[producers]` writer beside `default_models.sh`.
- Tests beside each module, with fixtures only.
- `commands/agents.md`, `commands/producer/morty.md`, `commands/producer/brain.md`, `commands/producer/dwight.md`.
- `docs/as-built/shutdown-and-restart.md`, `docs/as-built/staff-accessors.md`.

**Workers:** nothing splits; the director writes in no-worker mode.
- `impl` — every file.

**State:** the staff row (`current_session`, `home`), `config/models.conf`, the message queues, the old tab and the new pane record.

**Constraints from prior phases:**
- Start after the panes phase 53b and the admin-console director-only resume are on `origin/sdlc`; merge `origin/sdlc` first (sdlc holds `herdr.py`, `restart.py`, `stop_resume.py` until then).
- Phase 1 makes the mark follow the row, so step 4 moves the authority with no config write.
- Phase 2 supplies `catch_up.py` for the `Transfer` sections.

**Acceptance gate:** The test file of each changed module passes. The checkpoint gate passes.
basedpyright reports zero errors and zero warnings. A fixture transfer moves a Codex producer to a Claude session and back.
