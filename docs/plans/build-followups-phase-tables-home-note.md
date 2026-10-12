# phase-tables: what the vault's home note reads

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** The nightly review keeps its open candidates in one vault note, each unit's phase note says in its front matter what the unit is doing now, and `commands/plan/shrink.md` loses a word from the forbidden-words list. Asked for by the showrunner on 2026-10-12 for the hanadocs-info production, which puts both on the vault's home note.

> **As-built disposition: amend** — `docs/as-built/phase-tables.md` (the note's front matter), `docs/as-built/showrunner-automation.md` (the nightly list bullet)

> **Production: build-followups** — unit `phase-tables-unit`; production doc `docs/plans/build-followups-production.md`

## Delegation Context

- **Project:** `~/.claude` (natepiano/claude_commands) — the commands, docs and scripts every Claude and Codex session runs. Work only in the worktree `/home/natepiano/worktrees/claude-build-followups-phase-tables`, branch `build-followups-phase-tables`.
- **Project started:** 2026-10-12T23:06:39+00:00
- **Stack:** Python 3.10+ (stdlib only, `unittest`), Markdown command files. basedpyright must report 0 errors and 0 warnings; no file-level ignores; no `Any`; a dict with known keys is a `TypedDict`, a state is a named `NamedTuple`.
- **Layout:** `scripts/nightly_review/` (the nightly review and its candidate list), `scripts/delegate/` (the recorder and the phase-table script), `commands/plan/`.
- **Key files:**
  - `scripts/nightly_review/candidates.py` — prints the nightly review's open candidates as one Markdown table (`table`, 138; `ranked`, 127; `read_lines`, 120) and adds a night's report to the ledger (`record`, 187). `ROOT` (25) is the ledger's folder; `main` (209) handles no argument (print) and `record <report>`. Standalone: stdlib only, no sibling imports.
  - `scripts/nightly_review/test_candidates.py` — `CandidatesTests` (49); `setUp` (53) patches `candidates.ROOT` to a temp dir with `mock.patch.object`; `run_main(arguments)` (61) returns `(code, stdout, stderr)`.
  - `scripts/delegate/phase_table.py` — builds a `PhaseRecord` from run events, renders it and writes the vault notes. `render(record, zone)` (923) builds the bold heading at 926–932; `_note_text` (1240) writes the note: front matter `phase_table: true`, `production: <slug>`, `unit: <unit id>`, then `# <name>`, then `render(…)`. `_note_ownership(path)` (1195) reads `phase_table`, `production` and `unit` from the front matter by `line.partition(":")` and ignores every other key. `_write_note` (1294) is the atomic write: a dot-named temp file in the target's folder, mode `0o644`, `os.replace`.
  - `scripts/delegate/test_phase_table.py` — `PhaseTableTests` (32); `environment(at)` (132) sets every root to a temp path; `write_phase_note` (275) writes a three-key note by hand; `run_refresh` (388); `test_refresh_writes_exact_note_bytes_under_live_session_name` (2141) asserts a refreshed note byte for byte.
  - `scripts/delegate/test_progress_history.py` — the recorder's suite. One case reads a refreshed note: `ProgressHistoryTests.test_progress_refresh_writes_the_production_phase_note` (4737) compares the note's first eight lines at 4805–4817.
  - `commands/plan/shrink.md` — the shrink command; `<ResolvePlan/>` rule 5 is line 156.
  - `docs/as-built/phase-tables.md` and `docs/as-built/showrunner-automation.md` — how the note and the nightly list work now. Not edited by a seat: the run's as-built pass amends them.
- **Test lanes:** `scripts/nightly_review/` and `scripts/delegate/` — tests sit beside the code as `test_<module>.py` (`unittest`).
- **Build:** none (Python).
- **Test:** `python3 -m unittest discover -s scripts/nightly_review -p 'test_candidates.py'` (under a second); `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` (about six seconds); from `scripts/delegate`, `python3 -m unittest test_progress_history.ProgressHistoryTests.test_progress_refresh_writes_the_production_phase_note`. The rest of the recorder's suite (two to four minutes) runs once, at the end of the run.
- **Lint:** `basedpyright <each changed .py>` — pass is its `0 errors, 0 warnings` line (it exits 3: pyrightconfig names a `.venv` no checkout has). Run it once, after the last edit.
- **Invariants:**
  - Tests use temp dirs only: they never touch `~/rust/hanadocs`, `~/.local/state` or `~/.claude/sessions`. Every root a test reaches is set to a temp path first (`PHASE_TABLE_VAULT`, `PLAN_DELEGATE_HISTORY_DIR`, `NIGHTLY_REVIEW_NOTE`, `candidates.ROOT`).
  - A phase note is moved, replaced or removed only when its front matter carries `phase_table: true`, a `production` and a `unit`. A hand-written file is never changed.
  - Scripts own each note's format. No command file tells an agent to write, move or edit a note.
  - The ledger `~/.local/state/nightly-review/ledger.md` is the one record of the nightly candidates; the vault note is a copy a script rewrites.
  - File ownership (production doc, Units row `phase-tables-unit`): this unit owns `scripts/delegate/phase_table.py` and `scripts/delegate/progress_history.py` with their tests. `scripts/nightly_review/candidates.py` with its test and `commands/plan/shrink.md` are not this unit's: the showrunner assigned these edits on 2026-10-12, and the checkpoint notice names them.
  - Forbidden-words hooks (`~/rust/nate_style/rust/forbidden-words.md`) apply to code, comments and prose.
  - Saved run output stays under a few GB: read each run and delete it before the next.

## Phases

### Phase 1 — The nightly list and each unit's current phase reach the vault  · status: done

#### As-built

- `scripts/nightly_review/candidates.py` keeps one vault note holding its table. `write_note(body)` runs after the table prints and after every `record` that returns 0, and rewrites `~/rust/hanadocs/nightly improvement list.md` atomically: a dot-named temp file in the same folder, mode `0o644`, then `os.replace`. `NIGHTLY_REVIEW_NOTE`, read at write time, replaces the path.
- The note is a read line, a blank line, then the table as printed or `no open candidates`, and one newline. The read line is `Read <YYYY-MM-DD HH:MM> <zone label> from the nightly review's ledger on <short host>. The nightly review rewrites this note; edits made here are lost.` `now()` returns the clock in `ZONE` (America/New_York), the label comes from `%Z`, and the host is `socket.gethostname()` up to its first dot.
- `scripts/delegate/phase_table.py` has `_heading_text(record)`, the heading text without bold marks: `Phase <n> of <total> — <title>` or `No phase running — <done> of <total> done`. `render` wraps it in bold. `_note_text` writes it as the fourth front-matter line, `now: <JSON string>`, after `unit:`.
- `commands/showrunner/nightly.md` says the script rewrites the note and changes nothing else. `commands/plan/shrink.md` step 5 reads "Never pick a doc on your own".

**Files:**
- `scripts/nightly_review/candidates.py` — the note writer and its two call sites
- `scripts/nightly_review/test_candidates.py` — note bytes in EDT and EST, skips, failures; `setUp` points the note at a temp path for every case
- `scripts/delegate/phase_table.py` — `_heading_text` and the `now` line
- `scripts/delegate/test_phase_table.py`, `scripts/delegate/test_progress_history.py` — the `now` line with an open phase, with none, with a title that needs escaping, and over a three-key note
- `commands/showrunner/nightly.md`, `commands/plan/shrink.md` — wording

**Gotchas:**
- Nothing is written when the note's folder is missing or this machine has no `ledger.md`, so a machine without the ledger never replaces another machine's note.
- Every step after the path is chosen runs inside one `OSError` handler. A failure prints `candidates: cannot write <path>: <error>` on stderr and never changes the exit code; `Path.is_dir` and `Path.is_file` can raise, so the two skip checks sit inside it too.
- `_note_ownership` reads keys by name, so a three-key note this unit owns is still recognised and replaced.
- The default path is the live vault. A test of `candidates.py` keeps `NIGHTLY_REVIEW_NOTE` on a temp path.

**Ruled out:** writing the note when no ledger file exists, which would publish an empty list from a machine that holds no ledger.

