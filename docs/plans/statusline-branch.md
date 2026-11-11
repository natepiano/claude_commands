# Status line branch

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A unit director's status line shows its Git branch after the worktree folder.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md`

The user's words, from the sdlc showrunner on 2026-11-11:

"the footer for all unit directors should, on the first line follow the worktree directory with the current branch name it is operating in"

## Delegation Context

- **Project:** the `~/.claude` scripts — the Claude Code status line (`scripts/statusline/`).
- **Project started:** 2026-11-11T20:50:00+00:00
- **Stack:** Python 3.13, jq, POSIX sh, basedpyright, `unittest`.
- **Layout:**
  - `scripts/statusline/statusline.sh` — the status line command; it reads config and execs `assignment.py`.
  - `scripts/statusline/assignment.py` — reads the staff row, gives the assignment label and color role (`unit-director` for a unit), then execs `jq` with `--arg` values.
  - `scripts/statusline/statusline.jq` — builds the lines. `$first_line` is `[assignment, $path] | join(" | ")`; `$path` is the session folder with `$HOME` shown as `~`.
  - `scripts/statusline/test_statusline.py` — its tests. `status_lines(...)` and `first_line(...)` run the real command.
  - `docs/as-built/showrunner-automation.md` — the "Status line assignment" item (about line 537).
- **Key files:** as in Layout.
- **Test lanes:** `scripts/statusline/test_statusline.py`.
- **Build:** `basedpyright scripts/statusline` must read `0 errors, 0 warnings, 0 notes`.
- **Test:** `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest scripts.statusline.test_statusline`.
- **Lint:** none beyond basedpyright.
- **Invariants:**
  - The status line runs at every Claude Code refresh. It starts no `git` process; it reads files only.
  - Fields on a line join with ` | `.
  - Showrunner, worker and unassigned lines do not change.

## Phases

### Phase 1 — Branch after the worktree folder  · status: done

#### Work Order

**Goal:** A unit director's first status line reads `<assignment> | <folder> | <branch>`.

**Spec:**

1. When the role is `unit-director`, the first line adds the current branch as a third field after the folder: `unit-board unit for sdlc | ~/worktrees/claude-natedev-unit-board | natedev-unit-board`.
2. Read the branch from files, with no `git` process:
   - Start at the session folder the line shows: `workspace.current_dir`, else `cwd`, else the process folder (`--arg pwd`). Walk up to the first folder with a `.git` entry.
   - A `.git` folder holds `HEAD`. A `.git` file holds `gitdir: <path>` (a worktree); read `HEAD` in that path, relative to the `.git` file's folder when not absolute.
   - `ref: refs/heads/<name>` gives `<name>`.
   - Any other `HEAD` (detached) gives `detached <first 7 characters>`.
   - No `.git`, or an unreadable file, gives no branch field.
3. Other roles keep their first line unchanged.
4. Update the "Status line assignment" item in `docs/as-built/showrunner-automation.md` in the same commit.

**Files:**
- `scripts/statusline/assignment.py` — read the branch for a unit director and pass it to `jq` as `--arg branch`.
- `scripts/statusline/statusline.jq` — add `$ARGS.named.branch` to `$first_line` when not empty.
- `scripts/statusline/test_statusline.py` — rows for each rule.
- `docs/as-built/showrunner-automation.md` — the status line text.

**Workers:** `1 writer + 1 tester`
- `impl` — `assignment.py`, `statusline.jq`, the as-built doc.
- `test` — `test_statusline.py`: a unit row in a normal checkout, in a worktree (`.git` file with a relative and an absolute `gitdir`), from a subfolder, detached `HEAD`, and no repository; a showrunner row that shows no branch. Update existing unit first-line expectations.

**State:** none

**Acceptance gate:**
1. `basedpyright scripts/statusline`: 0 errors, 0 warnings, 0 notes.
2. `env HERDR_BIN=/nonexistent/herdr PYTHONPATH="$PWD" scripts/lib/py -m unittest scripts.statusline.test_statusline` passes.
3. Live: this unit's status line first line ends with `| natedev-unit-board`.
