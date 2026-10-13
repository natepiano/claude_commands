# herdr trust: trust checked before every launch

> **Status: IMPLEMENTATION PLAN — one phase, delegate-ready.** Moved out of `docs/plans/build-followups-herdr-adoption.md` (its Phase 3) so it runs in parallel with that plan's Phase 5 and the other split unit. The adoption plan's Phase 6 waits for all three.

> **As-built disposition: fold** — into `docs/as-built/herdr-adoption.md` (herdr-trial-unit creates it)

> **Production: build-followups** — unit `herdr-trust-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

The user, 2026-10-13 (verbatim): "yes you definitely should run them in parallel".

The showrunner (build-followups), 2026-10-13: adoption Phases 3 and 4 become their own units; herdr-trial-unit keeps Phase 5. Merge order is trust, panes, then herdr-trial-unit's shutdown and restart; each unit merges `origin/build-followups` before its checkpoint, and the later unit adapts in the shared files.

Design calls and evidence: `docs/plans/build-followups-herdr-adoption.md` (Calls 1–13) and `docs/as-built/herdr-trial.md`.

## Shared files

- `scripts/production/add_unit.py` and `test_add_unit.py` — herdr-panes-unit changes `wait_for_remote_control`, herdr-trial-unit adds `--host-session`; this unit lands first and touches only `preflight`.
- `scripts/production/test_promote_showrunner.py` — herdr-panes-unit adds a herdr unit case after this unit lands.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree and branch are set by the showrunner at launch; name both in every dispatch.
- **Project started:** 2026-10-13T22:59:12.058+00:00
- **Stack:** Python 3 standard library, checked by basedpyright; zsh scripts; Markdown commands and docs; herdr 0.9.0 (`herdr --help`, `herdr --skill`); tmux; systemd user units.
- **Layout:** `scripts/herdr/` (new library `herdr.py`, CLI `herdr_sessions.py`, test double `fake_herdr.py`) · `scripts/production/` (unit lookup, launchers, relays, status) · `scripts/message/roster.py` · `scripts/shutdown/` (inventory, stop, record, restart, settle) · `scripts/nightly_review/nightly_review.py` · `commands/showrunner/`, `commands/shutdown.md`, `commands/producer/to_production.md` · `docs/production_format.md` · `docs/as-built/`.
- **Key files:**
  - `scripts/herdr/herdr.py` — the library (Phase 1): `Herdr(session)` (`run` raises `HerdrFailure` on a non-zero exit or a 10 s timeout, `agent_blocked` as `AgentBlocked`; `panes`, `lookup`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until`, `read`, `send_keys`, `close_pane`, `agents`), module `live_sessions`, `server_unit`, `start_server`, `stop_session`, `open_pane`, `start_agent`, `herdr_place_of`, `herdr_processes`, `repository_trust_key` (runs git with `GIT_*` stripped), `trusted_by_claude`. `herdr_sessions.py` — `list`, `attach`. The trial driver is deleted; `git show a35b0312:scripts/herdr/trial.py` has it (`trusted_by_codex` 853, `pane_environment` 978, `native_arguments` 990).
  - `scripts/herdr/fake_herdr.py` — records argv to `FAKE_HERDR_LOG` as JSON lines, answers from `FAKE_HERDR_RESPONSES` keyed by space-joined argv (else `"default"`); fields `stdout`, `code`, `stderr`, `write_files`, `require_paths`, `remove_paths`; a list replays in order.
  - `scripts/production/unit_lookup.py` — `MarkedUnit(unit, pane, label, claude)` 59, `tmux_binary` 126, `tmux_sessions` 145, `_marks` 160, `_claudes` 169 (joins a Claude to a pane through the session record's `tmux` field), `_marked_sessions` 191, `every_marked_unit` 207, `marked_units` 225, `mark` 237, CLI `list|pane|mark` 245. Marks are `SHOWRUNNER_UNIT` (production slug) and `SHOWRUNNER_UNIT_ID` (unit).
  - `scripts/message/sessions.py` — `SessionRecord` 17 (`tmux` is empty outside tmux; `pid`, `sessionId`, `name`, `messagingSocketPath`, `updatedAt`), `read_session`, `live_session`.
  - `scripts/production/add_unit.py` — `tmux_live` 507, `preflight` 532, `prompt_for` 665, `launch_session` 701 (systemd-run scope + `tmux new-session -d -e` marks, `claude --model --effort [--resume] --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>` with `account_prefix(selected)`), `launch_environment` 739, `launched_unit` 762, `wait_for_remote_control` 770 (polls `capture-pane` for `/remote-control is active`), `main` 800 (restore and kill-session 869–899, prints `tmux attach -t` 924, 929). `--tmux-session` is the restore flag `restart.py` passes.
  - `scripts/production/add_showrunner.py` — `_project_keys` 53, `trusted` 66, `checked` 87, `launch` 120, `main` 160.
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

### Phase 1 — Trust checked before every launch  · status: done

#### As-built

- `herdr.py` holds the one Claude trust check: `claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile`. A folder is trusted when `projects[k].hasTrustDialogAccepted is True` for k in {the folder, its `--show-toplevel`, its main checkout (`repository_trust_key`, parent of `--git-common-dir`)}. `Untrusted` carries `keys_tried`; `UnreadableTrustFile` prints `cannot read <file> to check Claude trust (<ErrorClass>)`.
- `trust_command(folder, account: ClaudeLaunchAccount) -> str` is the line the user runs before accepting the prompt and typing `/exit`: `cd <folder> && <account prefix> claude`, where the default account's prefix is `env -u CLAUDE_CONFIG_DIR -u CLAUDE_CODE_REMOTE_MEMORY_DIR`.
- `add_unit.py` `preflight`, `add_showrunner.checked` (and through it `promote_showrunner.py`) and `nightly_review.py` refuse before any write or launch with `<unit | child slug | session>: Claude is not trusted in <folder> on <account label>. Run: <trust_command>`, or `<prefix>: <UnreadableTrustFile text>`. Launchers pick their account by room, and each check reads that account's file: `~/.claude.json` for the default, `<dir>/.claude.json` for a named account (`ADD_SHOWRUNNER_CLAUDE_JSON` overrides it in tests).
- A unit worktree not yet created is checked by its main checkout's key, and its trust command runs in the main checkout.
- `trust_survey.py` (read-only) prints, per account file, `<account> <key> <accepted> <kind>` (main checkout, worktree of <main>, plain folder, ancestor of <n> repos), then per `~/worktrees` folder whether its own, toplevel or main key is accepted. On 2026-10-13, 22 of 23 worktrees per account were covered by the main-checkout key and none by their own or toplevel key.

**Files:**
- `scripts/herdr/herdr.py` — `claude_trust`, its three outcome types, `trust_command`, `repository_trust_key`.
- `scripts/herdr/trust_survey.py` — the read-only trust survey.
- `scripts/production/add_unit.py`, `scripts/production/add_showrunner.py`, `scripts/nightly_review/nightly_review.py` — trust refusal before launch.
- `pyrightconfig.json` — `.` and `scripts/herdr` in the herdr and nightly_review environments, for `scripts.accounts` imports.
- `commands/showrunner/add_unit.md` — the refusal and the command the user runs.

**Binds later work:** the herdr panes unit and herdr adoption's launch work call `claude_trust` and `trust_command` from `herdr.py`, not a check of their own.

**Gotchas:** Trust counts only on an exact key (the folder, its `--show-toplevel`, or its main checkout), never an ancestor. The survey prints only the key, accepted and kind fields of `.claude.json`, and an unreadable file reports only its path and the error's class name. Absent keys alone do not show a folder launched without a prompt; that needs runtime evidence such as a Claude project dir. When `nightly_review.py` picks the default account but inherits `CLAUDE_CONFIG_DIR`, it checks trust on the inherited dir.

**Ruled out:** ancestor keys as trust (refused in the 2026-10-13 live check); `trusted_by_claude` and `add_showrunner._project_keys`, replaced by `claude_trust`; a Codex trust check, since no launcher starts interactive Codex.
