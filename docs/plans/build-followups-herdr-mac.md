# herdr on the Mac

> **Status: IMPLEMENTATION PLAN — one phase, delegate-ready.** Moved out of `docs/plans/build-followups-herdr-adoption.md` (its Phase 12) by the showrunner, 2026-10-15. The herdr library, standalone launcher and restart work on the Mac, with launchd in place of systemd.

> **As-built disposition: update** — `docs/as-built/herdr-adoption.md` gains a "herdr on the Mac" section

> **Production: build-followups** — unit to be named by the showrunner at `/showrunner:add_unit --plan`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

The showrunner (build-followups), 2026-10-15: the Mac phase of the herdr adoption becomes its own unit. The design is the parent plan's, carried over with its architect review's finding on Linux-only paths applied (port both server starts, their ownership checks and restored-workspace clearing to launchd; Darwin `server_environment` verified before Mac herdr restart is enabled).

## Ordering

**Starts after** the parent plan's Phase 11 (tmux removed from productions and sessions on Linux) has merged into build-followups. Phase 8 there adds `start_new_server`, `herdr_sessions.py` and `herdr-claude`; Phase 11 is the last change to `restart.py` and `herdr.py` before this one. Both machines share these scripts, so this unit merges build-followups before it starts.

## Phases

### Phase 1 — herdr on the Mac  · status: todo

**User sees:** before — Mac standalones run straight in Ghostty windows, and Mac sessions have no herdr; after — `herdr-claude` on the Mac opens a standalone in its own herdr session, and a Mac restart brings sessions back in herdr in a Ghostty window attached to them.

#### Work Order

**Goal:** the herdr library, standalone launcher and restart work on the Mac, with launchd in place of systemd.

**Spec:**

1. `herdr.py` server control on Darwin; Linux keeps systemd. The platform is chosen once by `sys.platform`; tests cover both with fake programs (`LAUNCHCTL` env names the program).
   - Both the adopting `start_server` and the exclusive `start_new_server` use `launchctl submit -l <server_unit> -- <herdr> --session <name> server`; `stop_session` uses `launchctl remove <server_unit>`. Each keeps its service-ownership check (today `_server_unit_active` runs `systemctl --user is-active`; on Darwin `launchctl list <server_unit>`) and restored-workspace clearing (`_clear_restored_workspaces`). `start_new_server` raises `SessionTaken` for a label launchd already holds or a server it did not start.
   - `server_environment(name)` (696) reads `/proc/net/unix` to find the process listening on the session's `herdr.sock`; `_herdr_launch` calls it on every herdr restart (restart.py 1068). The Darwin form finds that listener through `lsof` (`HERDR_LSOF`) and reads its environment through Spec 2's `ps eww` parse. It is verified on the Mac (Spec 5) before Mac herdr restart is enabled; until then a Mac `terminal` host keeps today's path. An unreadable environment and a server carrying a must-be-unset variable stay refusals.
2. `herdr_place_of`, `herdr_processes` and `attached_clients` on Darwin read processes through `ps` (`HERDR_PS` names the program for tests; the Mac has no `/proc`):
   - Only the current user's processes (`ps -U <uid> -o pid=,ppid=,uid=,command=`), each row's uid checked again.
   - Environment through Phase 6's rule: the `ps eww` text after the process's own `ps -o command=` argv, so argument text such as `HERDR_PANE_ID=w1:p1` in a prompt never counts. `herdr.py` holds that parse; `inventory._process_environment`'s Darwin branch calls it, so the rule lives once.
   - `cwd` per process from `lsof -a -p <pid> -d cwd -Fn` (`HERDR_LSOF` names it).
   - A process that vanishes between calls, or whose call fails, is skipped.
3. `restart.py` `terminal` host on the Mac restarts into herdr in-process (`_herdr_launch` with `NewHerdrSession` and the full `ResumeParts`, as Linux window hosts do) and opens `Ghostty.app` with `-e herdr session attach <name>`.
4. **Outside this repo (the showrunner's, in `~/nixos`, nix-darwin only; no worker makes these):** herdr in the Mac's packages; `herdr-claude` and the `continue` alias already sit in `modules/common/shell.nix` (shared with natedev, added in Phase 8).
5. Live check on the Mac (needs the user at the Mac for the Ghostty window): `herdr-claude --name herdr-mac-check`; `server_environment("herdr-mac-check")` returns the server's environment; a second `herdr-claude --name herdr-mac-check` is refused; `/shutdown --only herdr-mac-check --restart`, the conversation comes back attached. Remove the check session afterwards.

**Files:**
- `scripts/herdr/herdr.py` — launchd for both server starts and stop, ownership check, `ps` and `lsof` paths, Darwin environment parse, Darwin `server_environment`.
- `scripts/herdr/test_herdr.py` — Darwin cases: adopting and exclusive starts, a taken label, restored workspaces cleared, UID filtering, cwd, vanished processes, argument text with fake environment assignments, server environment found by its socket and unreadable.
- `scripts/shutdown/inventory.py` — Darwin environment through `herdr.py`.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/restart.py` — Mac terminal host into herdr.
- `scripts/shutdown/test_restart.py` — that case.

**Workers:** 2 writers (no test lane; `test` opens as impl); split the library and inventory against restart.
- `impl` — `herdr.py`, `test_herdr.py`, `inventory.py`, `test_inventory.py`; hub: `scripts/herdr/herdr.py`.
- `test` (opens as impl) — `restart.py`, `test_restart.py`.

**Constraints from prior phases:** `herdr.py` runs servers as systemd user units on Linux (`start_server` adopts, `start_new_server` is exclusive) and finds places, clients and server environments through `/proc`; session names follow herdr's contract (`[A-Za-z0-9_.-]+`, at most 64 bytes, not `.` or `..`) and `server_unit` is `herdr-<name>`; `start_agent` takes `OpeningPrompt | NoOpeningPrompt`; `herdr_sessions.py start|resume|list|attach`; `restart.py` restarts ghostty and zed hosts into herdr on Linux through `_herdr_launch(parts, NewHerdrSession(name), ...)` and leaves `terminal` unchanged; `inventory._process_environment` parses Darwin environment after the argv prefix (Phase 6); tmux is gone from the session tools.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr` and `scripts/shutdown` green on natedev and on the Mac; `basedpyright scripts/herdr scripts/shutdown` prints `0 errors, 0 warnings`. The live check in Spec 5 passes.
