# Gate binary shots

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** A unit takes before and after shots from a gate binary with no permission prompt.

> **As-built disposition: amend** — `docs/as-built/gate-checkouts.md`

## Delegation Context

- **Project:** `~/.claude` scripts — gate checkouts (`copies.py`) and Hana shots (`/hana_shot`).
- **Project started:** 2026-11-11T20:20:00+00:00
- **Stack:** Python 3.11+ (basedpyright strict).
- **Layout:**
  - `scripts/copies/copies.py` — gate checkouts. `copies.py run --repo <repo> --commit <sha> -- <command>` makes `<TMPDIR>/<owner>-gate-<suffix>/checkout` through `standing()` and runs the command there. `checkout/target` is a symlink into the cache root `~/worktrees/gates` (`config/copies.conf`).
  - `scripts/lib/scratch.py` — `scratch.new(task, word, *, throwaway)` makes an owned folder under `/tmp/claude/scratch` with a deadline. The hourly sweep removes it after the deadline.
  - `commands/hana_shot.md` — the shot command a unit reads.
- **Key files:**
  - `scripts/copies/copies.py` — `main()` `run` branch (line ~2158) and the `run` parser (line ~2112).
  - `scripts/copies/test_copies.py` — `copies.py` tests.
  - `commands/hana_shot.md` — line ~5, "build first".
- **Test lanes:** `scripts/copies/test_copies.py`.
- **Build:** none.
- **Test:** `env -u STAFF_OPERATION_FD -u PYTHONUNBUFFERED HERDR_BIN=/nonexistent/herdr PYTHONPATH=<worktree> <worktree>/scripts/lib/py -m unittest scripts.copies.test_copies`.
- **Lint:** `uvx basedpyright <changed Python files>` — `0 errors, 0 warnings, 0 notes`.
- **Invariants:**
  - Do not change the `config/copies.conf` root or any permission setting.
  - The user adds no checks: add no guard, refusal or scan that this Work Order does not name.
  - Processes clean up after themselves: the scratch deadline removes a publication; no user cleanup step.

## Phases

### Phase 1 — Gate runs publish their binary to an allowed folder  · status: done

**Depends on:** none.

#### As-built

- `copies.py run --publish <relative path>` (repeatable) copies built files out of the gate checkout while `standing()` still holds it. `publish_files(tree, paths, command_exit)` does the work.
- All files of one run go into one `scratch.new(word="gate-binary", throwaway=True)` folder under `/tmp/claude/scratch`. Each copy follows the `target` symlink, keeps the mode, and lands by a temporary name and a rename.
- `manifest.json` in that folder holds `repository`, the full checkout `commit`, `command_exit` and the file names.
- Stdout prints `published: <absolute path> <full commit>` per file. A missing path prints `copies: publish: <path> not built` on stderr. A copy error ends the scratch folder and prints `copies: publish: <error>`. The run exit code is always the command's exit code.
- `commands/hana_shot.md` tells units to pass the `published:` path as `--binary` and a matching worktree as `--worktree`, and never to copy from a gate checkout's `target`.

**Files:**
- `scripts/copies/copies.py` — `publish_files` and the `run --publish` option.
- `scripts/copies/test_copies.py` — publish rows in the `run` command test.
- `commands/hana_shot.md` — the gate-binary paragraph.

**Gotchas:** the scratch default task carries a random suffix, so `scratch.end()` on a failed publication removes only that folder. The publication lives until the scratch deadline, then the hourly sweep removes it.

**Ruled out:** refusals for a published name that matches `.<name>.tmp` or `manifest.json` — no unasked guards; the gate publishes `target/debug/hana`.
