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

#### Work Order

**Goal:** `candidates.py` keeps one vault note holding its table, every phase note's front matter says what the unit is doing now, and `commands/plan/shrink.md` holds no word from the forbidden-words list.

**Spec:**

Do Part A first: another production's phase waits on that note existing.

**Part A — the nightly list note (`scripts/nightly_review/candidates.py`).**

1. **Path.** The note is the path in the environment variable `NIGHTLY_REVIEW_NOTE` when it is set and not empty (`Path(value).expanduser()`), else `Path.home() / "rust" / "hanadocs" / "nightly improvement list.md"`. Read the environment when the note is written, not at import, so a test can set it.
2. **Content.** Exactly: the read line, one blank line, the body, one closing newline. No heading and no front matter (the page that embeds the note shows the note's name). The read line is

   `Read <YYYY-MM-DD HH:MM> <zone label> from the nightly review's ledger on <host>. The nightly review rewrites this note; edits made here are lost.`

   - The time is now in `ZoneInfo("America/New_York")`, to the minute. The zone label is that moment's `%Z` (`EDT` in summer, `EST` in winter), never a literal.
   - `<host>` is `socket.gethostname()` up to its first dot (`natedev` on natedev), so the line stays true on whichever machine wrote it.
   - Keep one small clock function (for example `def now() -> datetime: return datetime.now(ZONE)`) so a test can fix the moment with `mock.patch.object`, as `setUp` already does for `ROOT`.
3. **When.** One function writes the note, called from two places:
   - `main([])`, after it prints: the body is exactly the text it printed (the table, or `no open candidates`).
   - `record`, on each return of 0, both when it appended lines and when it found nothing new: the body is the table of the ledger as it then stands (`table(ranked(read_lines(ledger)))`, or `no open candidates` when none is open).
   - `record`'s failures (exit 1) and the usage error (exit 2) write nothing.
4. **Skips.** Write nothing, and print nothing about it, when the note's folder is not an existing directory (never create it), or when this machine has no ledger file (`(ROOT / "ledger.md").is_file()` is false): a machine with no nightly review must not replace natedev's table.
5. **Atomic write.** Write to a temp file in the same folder whose name starts with a dot (`tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=folder, prefix=f".{note.name}.", delete=False)`), set mode `0o644`, then `os.replace` it over the note, so a reader never sees half a table. Remove the temp file when the write fails. `phase_table.py`'s `_write_note` (1294) is the model; do not import it.
6. **A failed write.** An `OSError` prints `candidates: cannot write <path>: <error>` to stderr and changes no exit code: the printed table and the ledger are the result, the note is a copy.
7. **Docstring.** Add the note to the module docstring in two or three lines: its path, the variable that replaces it, and that both commands rewrite it.

Tests (`scripts/nightly_review/test_candidates.py`), written from this Spec:
- `setUp` sets `NIGHTLY_REVIEW_NOTE` to a path under the temp root for every case (`mock.patch.dict(os.environ, …)`), so no case, old or new, can write into `~/rust/hanadocs`. Fix the clock and the host name the same way where a case reads the read line.
- Printing the table writes the note: with the clock at 2026-10-12 19:30 in New York and the host `natedev`, the note's text is exactly `Read 2026-10-12 19:30 EDT from the nightly review's ledger on natedev. The nightly review rewrites this note; edits made here are lost.`, a blank line, the printed table and one newline; the table part equals stdout; no dot-named file is left in the folder.
- A January clock gives `EST`.
- A ledger with nothing open writes the read line and `no open candidates`.
- `record` rewrites the note from the ledger it just changed, and again when nothing was new.
- Nothing is written when the note's folder is missing, when the ledger file is missing, when `record` refuses a report, or on the usage error; each exit code stays what it was.
- A write that fails (for example the note path is an existing directory) prints the `cannot write` line, leaves no dot-named file, and keeps exit code 0.

**Part B — `now` in each phase note (`scripts/delegate/phase_table.py`).**

1. Factor the heading text out of `render` (926–932) into one function (for example `_heading_text(record: PhaseRecord) -> str`) that returns it without the bold marks: `Phase {current.phase} of {len(record.phases)} — {current.title}` for an open phase, else `No phase running — {done} of {len(record.phases)} done`. `render` wraps that text in `**…**`; its output does not change by one byte.
2. `_note_text` (1240) writes a fourth front-matter line, directly after `unit:`: `now: "<text>"`. The value is a YAML double-quoted scalar: a backslash is written `\\` and a double quote `\"`; every other character, the em dash included, is written as it is (`json.dumps(text, ensure_ascii=False)` produces exactly this).
3. `_note_ownership` (1195) is not changed, and neither is any other reader. A note written before this change (three keys) is still owned and is replaced at its next `refresh`; a note with `now` is owned; a title holding `: `, a double quote or a backslash does not change who owns the note. The JSON record does not carry `now`.

Tests (`scripts/delegate/test_phase_table.py`, plus one assertion in `scripts/delegate/test_progress_history.py`), written from this Spec:
- Update `test_refresh_writes_exact_note_bytes_under_live_session_name` (2141) for the fourth line, `now: "Phase 1 of 1 — Current delivery"`.
- A run with no open phase writes `now: "No phase running — <done> of <total> done"`, and the text inside the quotes equals the bold heading in the note's body without its `**`.
- A phase title holding a double quote and a backslash is escaped in `now`, the heading in the body keeps the title as written, and a second `refresh` replaces the note without a refusal.
- A three-key note written by `write_phase_note` (275) for the same production and unit is replaced by a four-key note at `refresh`.
- `ProgressHistoryTests.test_progress_refresh_writes_the_production_phase_note` (4737): its `splitlines()[:8]` comparison at 4805–4817 gains the `now` line (compare nine lines).

**Part C — `commands/plan/shrink.md`.** Line 156 begins `5. **Ask.** Never` and its next word is on the forbidden-words list. Replace lines 156–157 with:

```
5. **Ask.** Never pick a doc on your own, and never shrink a doc the user did
   not name or that the run record did not produce.
```

Change nothing else in the file.

**Files:**
- `scripts/nightly_review/candidates.py` — the note writer, its two call sites, the docstring
- `scripts/nightly_review/test_candidates.py` — `setUp` points every case at a temp note; the note cases
- `scripts/delegate/phase_table.py` — the heading function; the `now` line in `_note_text`
- `scripts/delegate/test_phase_table.py` — the exact-bytes case and the `now` cases
- `scripts/delegate/test_progress_history.py` — one comparison in one case
- `commands/plan/shrink.md` — lines 156–157

**Seats:** 1 writer + 1 tester — split by code and tests.
- `impl` — `scripts/nightly_review/candidates.py` first, then `scripts/delegate/phase_table.py`, then `commands/plan/shrink.md`
- `test` — `scripts/nightly_review/test_candidates.py` first, then `scripts/delegate/test_phase_table.py` and the one comparison in `scripts/delegate/test_progress_history.py`, each from the Spec alone

**Constraints from prior phases:** None in this plan. From this unit's earlier work, recorded in `docs/as-built/phase-tables.md`: the note's front matter was exactly three keys in the order above, `NoteOwnership(production, unit)` is read from them, and every scan of the vault is `vault_root.glob("*/*.md")`.

**Acceptance gate:**
- `python3 -m unittest discover -s scripts/nightly_review -p 'test_candidates.py'` green.
- `python3 -m unittest discover -s scripts/delegate -p 'test_phase_table.py'` green.
- From `scripts/delegate`: `python3 -m unittest test_progress_history.ProgressHistoryTests.test_progress_refresh_writes_the_production_phase_note` green.
- `basedpyright scripts/nightly_review/candidates.py scripts/nightly_review/test_candidates.py scripts/delegate/phase_table.py scripts/delegate/test_phase_table.py scripts/delegate/test_progress_history.py` prints `0 errors, 0 warnings`.
- `git status --short` lists only the six files above.
- Observable: with `NIGHTLY_REVIEW_NOTE` set to a file in a scratch folder and `XDG_STATE_HOME` set to a scratch folder holding `nightly-review/ledger.md`, `python3 scripts/nightly_review/candidates.py` prints the table and the scratch note holds the read line, a blank line and the same table. No run in this phase writes to `~/rust/hanadocs`.
