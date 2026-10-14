# herdr panes: typing into and reading sessions on either host

> **Status: IMPLEMENTATION PLAN — one phase, delegate-ready.** Moved out of `docs/plans/build-followups-herdr-adoption.md` (its Phase 4) so it runs in parallel with that plan's Phase 5 and the other split unit. The adoption plan's Phase 6 waits for all three.

> **As-built disposition: fold** — into `docs/as-built/herdr-adoption.md` (herdr-trial-unit creates it)

> **Production: build-followups** — unit `herdr-panes-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

The user, 2026-10-13 (verbatim): "yes you definitely should run them in parallel".

The showrunner (build-followups), 2026-10-13: adoption Phases 3 and 4 become their own units; herdr-trial-unit keeps Phase 5. Merge order is trust, panes, then herdr-trial-unit's shutdown and restart; each unit merges `origin/build-followups` before its checkpoint, and the later unit adapts in the shared files.

Design calls and evidence: `docs/plans/build-followups-herdr-adoption.md` (Calls 1–13) and `docs/as-built/herdr-trial.md`.

## Shared files

- `scripts/production/add_unit.py` and `test_add_unit.py` — herdr-trust-unit lands first (trust refusal in `preflight`); herdr-trial-unit adds `--host-session` after this unit. Touch only `wait_for_remote_control`.
- `scripts/shutdown/settle.py` — herdr-trial-unit changes the issue text after this unit; touch only `_screen` and its reason literal.
- `scripts/production/test_promote_showrunner.py` — herdr-trust-unit adds an untrusted-worktree case first; adapt to it.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree and branch are set by the showrunner at launch; name both in every dispatch.
- **Project started:** 2026-10-13T22:59:25.066+00:00
- **Stack:** Python 3 standard library, checked by basedpyright; zsh scripts; Markdown commands and docs; herdr 0.9.0 (`herdr --help`, `herdr --skill`); tmux; systemd user units.
- **Layout:** `scripts/herdr/` (new library `herdr.py`, CLI `herdr_sessions.py`, test double `fake_herdr.py`) · `scripts/production/` (unit lookup, launchers, relays, status) · `scripts/message/roster.py` · `scripts/shutdown/` (inventory, stop, record, restart, settle) · `scripts/nightly_review/nightly_review.py` · `commands/showrunner/`, `commands/shutdown.md`, `commands/producer/to_production.md` · `docs/production_format.md` · `docs/as-built/`.
- **Key files:**
  - `scripts/herdr/herdr.py` — the library (Phase 1): `Herdr(session)` (`run` raises `HerdrFailure` on a non-zero exit or a 10 s timeout, `agent_blocked` as `AgentBlocked`; `panes`, `lookup`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until`, `read`, `send_keys`, `close_pane`, `agents`), module `live_sessions`, `server_unit`, `start_server`, `stop_session`, `open_pane`, `start_agent`, `herdr_place_of`, `herdr_processes`, `repository_trust_key` (runs git with `GIT_*` stripped), `claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile` and `trust_command(folder, account)` — the only trust checks a launcher uses; exact keys only (the folder, its `--show-toplevel`, its main checkout), never an ancestor. `herdr_sessions.py` — `list`, `attach`. The trial driver is deleted; `git show a35b0312:scripts/herdr/trial.py` has it (`trusted_by_codex` 853, `pane_environment` 978, `native_arguments` 990).
  - `scripts/herdr/fake_herdr.py` — records argv to `FAKE_HERDR_LOG` as JSON lines, answers from `FAKE_HERDR_RESPONSES` keyed by space-joined argv (else `"default"`); fields `stdout`, `code`, `stderr`, `write_files`, `require_paths`, `remove_paths`; a list replays in order.
  - `scripts/production/unit_lookup.py` — `MarkedUnit(unit, pane, label, claude)` 59, `tmux_binary` 126, `tmux_sessions` 145, `_marks` 160, `_claudes` 169 (joins a Claude to a pane through the session record's `tmux` field), `_marked_sessions` 191, `every_marked_unit` 207, `marked_units` 225, `mark` 237, CLI `list|pane|mark` 245. Marks are `SHOWRUNNER_UNIT` (production slug) and `SHOWRUNNER_UNIT_ID` (unit).
  - `scripts/message/sessions.py` — `SessionRecord` 17 (`tmux` is empty outside tmux; `pid`, `sessionId`, `name`, `messagingSocketPath`, `updatedAt`), `read_session`, `live_session`.
  - `scripts/production/add_unit.py` — `tmux_live` 507, `preflight` 532, `prompt_for` 665, `launch_session` 701 (systemd-run scope + `tmux new-session -d -e` marks, `claude --model --effort [--resume] --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>` with `account_prefix(selected)`), `launch_environment` 739, `launched_unit` 762, `wait_for_remote_control` 770 (polls `capture-pane` for `/remote-control is active`), `main` 800 (restore and kill-session 869–899, prints `tmux attach -t` 924, 929). `--tmux-session` is the restore flag `restart.py` passes.
  - `scripts/production/add_showrunner.py` — `claude_json` 57, `checked` 62 (trust through `herdr.claude_trust`), `account_for_launch` 113, `recorded_account_directory` 140, `launch` 148, `main` 190.
  - `scripts/production/promote_showrunner.py` — capture 86, stop via send-keys and kill-session 147–160, messages 220–230.
  - `scripts/production/unit_effort.py` — `_tmux` 80, `verdict` 142, `send` 157, capture 171.
  - `scripts/production/stall_watch.py` — `TMUX` 28, capture of the last 400 joined lines 211.
  - `scripts/production/unit_status.sh` — tmux binary 31–32, `pane_claude_pid` 111, per-unit capture and display-message 140–172.
  - `scripts/production/tmux_names.py` — renames tmux sessions to Claude names and reports name faults (`fault` 146); registered by `update_registration.py` 176; sender name in `scripts/hooks/conversation_pause.py` 29.
  - `scripts/production/adopt.py` — marks existing tmux sessions as units (55–132).
  - `scripts/production/live_units.py`, `waiting.py`, `production_lifecycle.py` — consume `unit_lookup` (`live_units.py` 98 prints `unit.session.pane`; `production_lifecycle.py` 482 prints `close tmux sessions`).
  - `scripts/production/fake_tmux.py` — tmux test double (sessions, panes and marks from a JSON file).
  - `scripts/message/roster.py` — `_every_marked_unit` 611, `_production_places` 619 (gates on a tmux binary; joins a socketless director by `record["tmux"]`), `_local_roster` 1087, `RosterEntry` 173, `Messageable`/`CannotReceiveMessages` 96–105.
  - `scripts/shutdown/inventory.py` — `UnitHost` 40, `TmuxHost` 79, `Host` 113, wire parsing 359–460, `_unit_sessions` 820, `_tmux_session` 940, `_host` 1100 (reads `TMUX_PANE` from the process environment).
  - `scripts/shutdown/stop.py` — `run_tmux` 133, `_stop_unit_tmux` 332, callers 413–479.
  - `scripts/shutdown/record.py` — `UnitTmuxLeftRunning` 122, parsing 650–672.
  - `scripts/shutdown/restart.py` — unit argv with `--tmux-session` 587, tmux pane state 686–770, `_tmux_launch` 802, host dispatch 895–935.
  - `scripts/shutdown/settle.py` — `_screen` 377 (capture-pane), issue text 768.
  - `scripts/shutdown/shutdown.py` — host text 138.
  - `scripts/nightly_review/nightly_review.py` — `tmux` 73, `has-session` 81, attach and kill checks 124–129, spawn 150–160.
  - `scripts/accounts/accounts.py` — `AccountSelection` 202, `account_prefix` 1070, `role_account`; `scripts/whoami/account.py` `named_account_environment(dir)` gives the account's variables as a dict.
  - `pyrightconfig.json` — execution environments; `scripts/production`, `scripts/shutdown`, `scripts/message` and `scripts/nightly_review` need `scripts/herdr` in `extraPaths` once they import the library (hub: one entry per environment, also touched by other units).
- **Test lanes:** none (no `tests/` directories); tests sit beside the code as `test_*.py` and import their sibling by bare name.
- **Build:** none.
- **Test:** `python3 -m unittest discover -s <dir> -p 'test_*.py'` for each directory a phase touches (`scripts/herdr`, `scripts/production`, `scripts/message`, `scripts/shutdown`, `scripts/nightly_review`), run from the worktree root.
- **Lint:** `basedpyright <changed paths>` — pass is its `0 errors, 0 warnings` line (it exits 3 on the missing `.venv`).
- **Invariants:**
  - **Tests never start real herdr, tmux, Claude or Codex.** They use `scripts/herdr/fake_herdr.py` and `scripts/production/fake_tmux.py`, a fake `/proc` root and fake session records (herdr trial rule, carried forward).
  - **Nothing answers a trust or permission prompt.** Launchers refuse an untrusted folder; `agent_not_ready` at start logs the visible screen and sends no key (herdr trial invariant).
  - **One prompt per launch, never sent twice.** `agent start` carries no prompt (herdr refuses multi-line start arguments, trial finding 4); the opening prompt goes through `agent prompt` once the agent is ready.
  - **No kill by pattern.** Only a systemd unit the library started, by its recorded name, or a named herdr session is stopped (user memory `never-kill-by-pattern`).
  - **Running productions keep working.** Until the tmux removal phase every lookup, relay, read, stop and restart handles both hosts; tmux paths are removed only there.
  - **Edits to `/etc/nixos` are outside this repo.** A phase names them for the user; no seat makes them.
  - **File ownership.** Several files below sit in other units' Owns cells (`add_unit.py`, `showrunners.py`, `stall_watch.py`: enh-showrunner-unit; `scripts/message/`: shutdown-unit; `commands/showrunner/produce.md`: stalls-unit hub). The showrunner clears each before dispatch.
  - Python: basedpyright 0/0 on changed files; no file-level ignores; a `TypedDict` for every herdr JSON shape and record; no `Any`.

## Phases

### Phase 1 — Typing into and reading sessions on either host  · status: done

**User sees:** before — relays, `/compact`, effort changes, stall watch, `/unit:report` and the shutdown screen check work only for tmux sessions; after — they work the same for tmux and herdr sessions, and a relay to a unit blocked at a prompt is refused with a line saying so, not typed into the prompt.

#### As-built

- `session_pane.py` is the one pane I/O path. Library: `read_place(place, lines=60, *, joined=False, ansi=False) -> str`, `type_place(place, text)`, `keys_place(place, *keys)`, `where_place(place) -> str`, `session_place(session_id) -> Place`; errors `TmuxFailure(OSError)` and `NoSuchTarget`.
- CLI: `read|type|keys|where` over `unit <slug> <unit>` or `session <id>`. tmux reads use `capture-pane -p [-J] [-e]`, `type` sends `-l <text>` then `Enter` as two calls; herdr goes through `Herdr.read`, `prompt` and `send_keys`. `where` prints `tmux:<pane>` or `herdr:<session>:<pane>`.
- Exits: 0 done, 1 no such target, 2 usage, 3 host failure, 4 blocked at a prompt (`<target>: blocked at a prompt; nothing typed`).
- `MarkedUnit.pane` is gone; every caller dispatches on `unit.place` (`TmuxPlace | HerdrPlace`). `settle._screen` reads a herdr host through `read_place` and reports `"no pane host"` when none exists; `unit_status.sh` prints `CLAUDE STATE UNKNOWN` when the state cannot be read.
- `promote_showrunner.py` ends a herdr place with `herdr.stop_session(label)` after Claude exits, and reads a herdr unit's account from `whoami.account.process_config_dir(pid)`; tmux units keep `@claude_config_dir`, and a refusal reads "cannot read its live Claude's account environment".

**Files:**
- `scripts/production/session_pane.py` — the host-neutral read, type, keys and where path, library and CLI.
- `scripts/production/unit_effort.py`, `stall_watch.py`, `add_unit.py` (including the resume path, which stops a herdr unit by name), `live_units.py`, `adopt.py`, `promote_showrunner.py` — pane I/O through `session_pane`.
- `scripts/production/unit_lookup.py` — `MarkedUnit` without `pane`.
- `scripts/production/unit_status.sh` — text from `session_pane.py read unit … --lines 400 --joined`, session id from `unit_lookup.py list`.
- `scripts/shutdown/settle.py` — `_screen` reads tmux and herdr hosts.
- `commands/showrunner/produce.md`, `dailies.md`, `promote_unit.md` — relay, `/compact` idle check, go-ahead, pane reads and stop through `session_pane.py`.
- Tests: `test_session_pane.py`, `test_units_table.py`, and the herdr cases in the callers' tests and `scripts/shutdown/test_settle.py`.

**Gotchas:**
- `unit_lookup.py list`: column 4 is the Claude state, column 6 the session id.
- On Darwin the pane id comes only from the session record's tmux field (`%N`), never from `ps` output.
- A herdr unit's name never exempts a tmux session from promote's collision check.
- herdr `agent start … -- claude` adds the binary to argv itself.
- Trust checks go only through `herdr.claude_trust` / `trust_command`, exact keys only.
- `read --ansi` dimmed-suggestion detection (`\e[2m` after `❯`) has not been seen live on herdr.

**Ruled out:** parsing Darwin `ps eww` for `TMUX_PANE` (argv tokens read as environment); a second process-environment reader for accounts.
