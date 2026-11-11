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

#### As-built

- A unit director's first status line reads `<assignment> | <folder> | <branch>`, for example `unit-board unit for sdlc | ~/worktrees/claude-natedev-unit-board | natedev-unit-board`. Other roles keep their first line.
- `current_branch(payload, pwd)` in `assignment.py` reads the branch from files, with no `git` process. It starts at `workspace.current_dir`, else `cwd`, else `--arg pwd`, resolves symbolic links, and walks up to the first `.git` entry.
- A `.git` folder holds `HEAD`; a `.git` file gives `gitdir:`, relative to that file's folder unless absolute. `ref: refs/heads/<name>` gives `<name>`; a detached `HEAD` gives `detached <7 chars>`. No repository or an unreadable file gives no field.

**Files:**
- `scripts/statusline/assignment.py` — reads the branch for the `unit-director` role and passes it to `jq` as `--arg branch`.
- `scripts/statusline/statusline.jq` — `$first_line` adds a non-empty `$ARGS.named.branch` as the third field.
- `scripts/statusline/test_statusline.py` — branch rows: checkout, worktree with relative and absolute `gitdir`, subfolder, detached, no repository, symbolic link, showrunner.
- `docs/as-built/showrunner-automation.md` — the "Status line assignment" item.

**Gotchas:** The status line runs at every Claude Code refresh, so it starts no `git` process. `Path.absolute()` keeps symbolic links and can miss the repository; resolve the folder first.
