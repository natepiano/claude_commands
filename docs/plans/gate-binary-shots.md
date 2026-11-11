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

#### Work Order

**Goal:** `copies.py run --publish <relative path>` copies a file the gate built into a scratch folder under `/tmp/claude/scratch` and prints its path. A unit launches shots from that path with no permission prompt.

**Spec:**

Cause: a unit copied `<gate checkout>/target/debug/hana`. `target` is a symlink into `~/worktrees/gates`, which is outside every unit's allowed folders, so the copy asked for permission.

1. Add `--publish <relative path>` to the `run` parser. It may repeat.
2. After the command returns, while `standing()` still holds the checkout, for each `--publish` path that exists under the tree: make one folder with `scratch.new(word="gate-binary", throwaway=True)` for the run. Copy each file into it with its base name, following the symlink and keeping the executable mode. Copy to a temporary name, then rename.
3. Write `manifest.json` in that folder: `repository`, full `commit` (the checkout `HEAD`), `command_exit` and the published file names.
4. Print one line per file to stdout: `published: <absolute path> <full commit>`. A path that does not exist prints `copies: publish: <path> not built` to stderr. The run exit code stays the command's exit code.
5. A failed copy removes the folder with `scratch.end()` and prints the error on stderr. The run exit code stays the command's exit code.
6. `commands/hana_shot.md`: add one short paragraph. A shot from a gate build uses the `published:` path with `--binary`, and assets from a matching worktree with `--worktree`. Never copy from a gate checkout's `target`.
7. Add no other guard, refusal or scan (user rule).

**Files:**
- `scripts/copies/copies.py` — `--publish` on `run`.
- `scripts/copies/test_copies.py` — publish rows.
- `commands/hana_shot.md` — the gate-binary paragraph.

**Workers:** 1 writer + 1 tester.
- `impl` — `scripts/copies/copies.py`, `commands/hana_shot.md`.
- `test` — `scripts/copies/test_copies.py`. Rows use a temporary repository and a temporary scratch root. A `run --publish target/debug/x` whose command writes `target/debug/x` through a symlinked `target` prints a `published:` path outside the checkout, the file is a real file with the same bytes and mode, and `manifest.json` names the full commit. A missing path prints the `not built` line and keeps the command's exit code. The publication survives the checkout removal.

**State:** none.

**Constraints from prior phases:** none.

**Acceptance gate:**
1. The unittest line passes for `scripts.copies.test_copies`, `scripts.copies.test_copy_guard_sources` and `scripts.lib.test_scratch`.
2. basedpyright over the changed Python files reads `0 errors, 0 warnings, 0 notes`.
