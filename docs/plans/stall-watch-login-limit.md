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

#### As-built

- `config/stall_watch.conf` holds `login_limit_pattern`, read at use.
- `watch_pass` in `scripts/production/stall_watch.py` checks the pane text after the last turn end. While the limit shows and the login is not back, it sends one tell to the showrunner, sends no bump, keeps the idle clock reset and logs `login limit`.
- The saved stretch keeps a `login_since` mark. An old stretch file without the mark still reads.
- `login_restored` reads the usage store only for a pane at the limit. A successful read for the session's login that is newer than the session's last transcript activity means the login is back. The unit then follows the normal idle path.
- Rows in `test_a_resumed_holding_unit_gets_a_fresh_idle_stretch_before_its_bump` cover the limit, the return on a new turn and the return through the usage store.
