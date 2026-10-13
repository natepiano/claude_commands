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

### Phase 1 — Trust checked before every launch  · status: todo

**User sees:** before — a new unit can open on Claude's folder-trust prompt and sit there until someone notices; after — a launch in an untrusted folder stops at once with one line naming the folder, the account and the command to run, and a trusted folder launches as before.

#### Work Order

**Goal:** `add_unit.py`, `add_showrunner.py`, `promote_showrunner.py` and `nightly_review.py` refuse an untrusted start folder through one shared check, with the rule for which keys count measured first.

**Spec:**

1. **Measure first (read-only).** New `scripts/herdr/trust_survey.py`: for each account file (`~/.claude.json` and `<dir>/.claude.json` for every `~/.claude[0-9]` dir) and each project key it holds, print `<account>\t<key>\t<accepted>\t<kind>`, where kind is `main checkout`, `worktree of <main>`, `plain folder` or `ancestor of <n> repos`. Then for every directory under `~/worktrees` print whether its own key, its `--show-toplevel` key or its main checkout's key is accepted on each account. It writes nothing and prints no other field of the file. Run it once and put the counts in the phase report.
2. **The rule.** From the survey: a folder is trusted on an account when `projects[k].hasTrustDialogAccepted is True` for k in {the folder, its `--show-toplevel`, its main checkout (parent of `--git-common-dir`)}, the rule `add_showrunner._project_keys` uses today. An ancestor folder never counts (G1 live check 2026-10-13; herdr trial). If the survey finds a worktree that ran unprompted with none of those keys accepted, stop and report to the showrunner before step 3: the rule is then wrong.
3. `herdr.py` gains `claude_trust(folder, claude_json) -> Trusted | Untrusted(folder, account_file, keys_tried)` with that rule, and `trust_command(folder, account: AccountSelection) -> str`, the one line the user runs: `cd <folder> && <account prefix>claude`, then accept the prompt and type `/exit`. An unreadable file refuses with its path and the error's class name only.
4. Callers, each reading the account file of the account that will run the session (`<CLAUDE_CONFIG_DIR>/.claude.json` for a named account, `~/.claude.json` for the default):
   - `add_unit.py` `preflight`: refuse (exit 2) with `<unit>: Claude is not trusted in <folder> on <account label>. Run: <trust_command>`; the folder is the session cwd (`request.session_cwd`), checked after the worktree path is known and before anything is written or launched. A new worktree is checked by its main checkout's key, since the worktree folder does not exist yet.
   - `add_showrunner.py`: `trusted` and `_project_keys` are removed; `checked` calls `claude_trust` with the default account file (`ADD_SHOWRUNNER_CLAUDE_JSON` still overrides it for tests) and prints the same refusal line.
   - `promote_showrunner.py`: no change in code (it goes through `add_showrunner.checked`); its test gains an untrusted-worktree case if none exists.
   - `nightly_review.py`: refuse a launch in an untrusted folder with the same line, on the account it launches with.
5. Codex trust is not checked: no launcher starts interactive Codex (Call 1).
6. Correct `docs/plans/build-followups-two-accounts.md:151` to the measured rule (owner two-accounts-unit; the showrunner clears the edit).

**Files:**
- `scripts/herdr/trust_survey.py` — new read-only survey.
- `scripts/herdr/herdr.py` — `claude_trust`, `trust_command`.
- `scripts/herdr/test_herdr.py` — trust rule cases.
- `scripts/production/add_unit.py` — trust refusal in `preflight`.
- `scripts/production/test_add_unit.py` — refusal before any write.
- `scripts/production/add_showrunner.py` — shared check.
- `scripts/production/test_add_showrunner.py` — refusal text.
- `scripts/production/test_promote_showrunner.py` — untrusted worktree.
- `scripts/nightly_review/nightly_review.py` — trust refusal.
- `scripts/nightly_review/test_nightly_review.py` — refusal case.
- `pyrightconfig.json` — `scripts/herdr` in the `scripts/nightly_review` environment.
- `docs/plans/build-followups-two-accounts.md` — line 151.
- `commands/showrunner/add_unit.md` — the refusal and what the user runs.

**Seats:** 2 writers (no test lane; `test` opens as impl); split library against callers.
- `impl` — `trust_survey.py`, `herdr.py`, `test_herdr.py`, the two-accounts line; hub: `scripts/herdr/herdr.py`.
- `test` (opens as impl) — `add_unit.py`, `add_showrunner.py`, `nightly_review.py`, `pyrightconfig.json`, `add_unit.md` and their tests.

**Constraints from prior phases:** `herdr.py` already holds `repository_trust_key` and `trusted_by_claude` (exact main-checkout key only); this phase replaces `trusted_by_claude` with `claude_trust`. `pyrightconfig.json` already lists `scripts/herdr` for production, message and shutdown.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr`, `scripts/production` and `scripts/nightly_review` green; `basedpyright scripts/herdr scripts/production scripts/nightly_review` prints `0 errors, 0 warnings`. Tests cover each of the three keys accepted, an ancestor-only entry refused, an unreadable file refused without its contents, and `add_unit.py` refusing before it writes a stub, a row or a worktree. The survey ran and its counts are in the phase report.
