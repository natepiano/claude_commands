# Stall watch login limit

> **Status: IMPLEMENTATION PLAN — phased, direct-ready.** stall-watch names an expired login once and stops bumping that session until the login returns.

> **As-built disposition: amend** — `docs/as-built/showrunner-automation.md` records the result.

## Source

The sdlc showrunner, 2026-11-10: "a stall-watch fault from hana. The claude1 login expired at about 21:42 EST. Every claude1 session answers 'Login reached its time limit · Please run /login'. stall-watch still bumps fastest every 7-8 minutes as 'idle', and its status line is stale (Phase 65). Wanted: when a session shows the login limit, stall-watch names the login cause once and sends no more bumps until the login returns. Fix at the source, add rows to the existing stall-watch tests (no live units), and send the hash."

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts that run the user's productions.
- **Project started:** 2026-11-10T14:50:24.642+00:00
- **Stack:** Python 3.13, standard library only.
- **Layout:** `scripts/production/`, `config/`.
- **Key files:**
  - `scripts/production/stall_watch.py` — `watch_pass` (line 1231) reads each unit's pane (`read_place`, line 1344), handles a permission prompt (line 1364), then reads the idle stretch and sends the `bump` to the unit and the `tell` to the showrunner (lines 1388–1441); `Stretch` (line 117) is the saved idle state; `send_all` (line 1125) marks `bump_sent` and `tell_sent` after delivery
  - `scripts/production/test_stall_watch.py` — the existing stall-watch tests, with fixtures and no live units
  - `config/stall_watch.conf` — the stall-watch patterns, read at use
- **Test lanes:** none (Python unittest modules beside the code).
- **Build:** none.
- **Test:** `scripts/lib/py -m unittest scripts.production.test_stall_watch`, from the worktree root.
- **Lint:** `basedpyright <the changed Python files>` passes when its output has the `0 errors, 0 warnings, 0 notes` line.
- **Invariants:**
  - Tests use fixtures. They never read live units or live state.
  - Python is typed throughout, with no `Any` and no file-level type ignores.
  - Tunable text lives in `config/stall_watch.conf`, read at use.
  - All text uses Simplified Technical English.

## Phases

### Phase 1 — An expired login gets one notice and no bumps  · status: done

**Depends on:** none

#### Work Order

**Goal:** when a watched unit's pane shows the Claude login limit, stall-watch tells the showrunner the login cause once and sends no bump. When the login returns, the normal idle watch starts again with a fresh stretch.

**Spec:**

1. Add a `login_limit_pattern` to `config/stall_watch.conf` that matches `Login reached its time limit`. Read it at use, the same way as the other status patterns.
2. In `watch_pass`, after the permission-prompt branch and before the idle stretch, detect the login limit in the pane text. A pane shows the limit only when the match is in the newest part of the pane, after the last assistant turn-end on screen. Old text from before a later successful turn does not count.
3. While the pane shows the limit:
   - Send no `bump` to the unit.
   - Send one `tell` to the showrunner that names the unit and the login cause: the Claude login reached its time limit and needs `/login`.
   - Record that tell in the saved stretch, so later passes send nothing more while the limit stays on screen.
   - Keep the idle clock reset, so the unit gets no bump at the moment the login returns.
   - The per-unit log line reads `inspected <showrunner name>/<unit>: login limit`.
4. When the pane no longer shows the limit, clear the login mark. The unit then follows the normal idle path.
5. Add rows to the existing stall-watch tests: a pane at the login limit gets one tell and no bump across two passes; a pane where the login returned gets the normal bump after the stall time. Add no other check.

**Files:**
- `scripts/production/stall_watch.py` — the login-limit branch and the saved mark
- `scripts/production/test_stall_watch.py` — the new rows
- `config/stall_watch.conf` — `login_limit_pattern`

**Workers:** 1 writer — one small change in one module.
- `impl` — the files above.

**State:** the saved stretch file gains a login mark — a saved stretch from before the change must still read.

**Constraints from prior phases:** none.

**Acceptance gate:**
- The Test command above passes.
- `basedpyright scripts/production/stall_watch.py scripts/production/test_stall_watch.py` gives `0 errors, 0 warnings, 0 notes`.
