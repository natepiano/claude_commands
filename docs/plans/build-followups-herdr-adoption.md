# herdr adoption: every production and standalone Claude session in herdr

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready. Approved by the showrunner 2026-10-13, with its calls folded in.** Every showrunner, unit director and standalone Claude session on natedev runs in its own named herdr session, and the user opens any of them with `herdr session attach <name>`. Codex seats stay headless. tmux keeps working until the last Linux phase. The Mac comes last.

> **As-built disposition: create** — a new `docs/as-built/herdr-adoption.md`

> **Production: build-followups** — unit `herdr-trial-unit` (the showrunner may assign another); production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

The user, 2026-10-13 (verbatim): "okay - i'm good with herdr - i want to use it - i want to get all of our showrunner productions into it as well as standalones".

The showrunner (build-followups), passing on the production's direction: write the plan as a new plan doc in this production. Fix-before-adopting items come first: the roster and showrunners see herdr-hosted sessions, one herdr session per unit for attach, trust prompts, quota alerts. Each phase names what the user sees before and after it.

Evidence: `docs/as-built/herdr-trial.md` (findings 1–8, gotchas, reusable and trial-only parts).

**Calls I made (each a one-line revert unless it says otherwise):**

1. **Codex seats stay headless** (`codex app-server` threads via `scripts/agents/codex_mesh.py`). Only Claude sessions move into herdr. In the trial, interactive Codex in panes caused the lost Work Orders at the trust screen and the roster blind spot; headless seats ask for no trust and the roster already finds them. The roster lists Codex panes only when some exist (one `herdr agent list` per live herdr session), and no launcher creates them. Risk: a later wish for Codex status rows in herdr needs its own plan.
2. **One herdr session per showrunner, per unit and per standalone**, named by the session's launch name. `herdr session attach <name>` opens any of them (trial finding 6: an attached window keeps its own view, so one shared session cannot do this). `herdr_sessions.py list` names the live ones. Risk: many herdr servers, one per session, as there are many tmux sessions today.
3. **tmux keeps working through the transition.** `unit_lookup.py` gains a herdr source beside tmux, so running productions are not disturbed. Launchers switch to herdr only after lookup, typing, reading, shutdown and restart all handle herdr. tmux code goes in the last Linux phase, after every live session has moved. Risk: two hosts in the code for some weeks.
4. **Phase order.** The brief put launchers before typing and shutdown. I moved typing/reading and shutdown/restart ahead of the launcher switch, so no herdr unit ever exists that a relay, `/compact`, an effort change or a shutdown cannot reach. The finished result is the same. Risk: the user waits two more phases before the first herdr unit.
5. **Quota alerts: no change.** A Claude in a herdr pane is an ordinary session, so alerts reaching it are correct (trial finding 7). `waiting.py quota` finds herdr units through the lookup. Risk: none known.
6. **Trust is checked, never answered.** Every launcher refuses to start in a folder whose Claude trust is missing on the account that will run it, and prints the one command the user runs to grant it. `add_unit.py` has no trust check today; `add_showrunner.py` has one (`trusted`, `_project_keys`), which moves into the shared check. Risk: a launch the user expected now refuses until they trust the folder once.
7. **Which trust keys count is measured first.** `docs/plans/build-followups-two-accounts.md:151` says worktrees inherit trust from `/home/natepiano`; the G1 live check (`docs/as-built/showrunner-automation.md`, nested showrunners phase 4) and the herdr trial ("Ruled out") say a trusted ancestor folder does not count. The trust phase surveys the account files read-only before it writes the rule. Risk: the survey may show the main-checkout rule is wrong too; the phase then stops and reports.
8. **Linux first; Mac last.** The Mac has no tmux and no systemd; its phase uses launchd and may move to its own plan. Risk: Mac standalones stay outside herdr until then.
9. **earlyoom.** Each herdr server must be in earlyoom's `--avoid` list (`/etc/nixos/modules/linux/memory.nix`, the regex with `tmux: server`). That file is in the nixos repo, so the launcher phase names the edit for the user and is blocked until the rebuild. Risk: until then a herdr server is an ordinary earlyoom target.
10. **No client window from launchers.** Launchers start only the herdr server, as a systemd user unit. The user attaches from any terminal, as with `tmux attach`. The trial's ghostty and kdotool client stays out. Risk: none; a window opener can be added later.
11. **The trial driver goes.** `scripts/herdr/trial.py`, `scaffold_files.py` and `test_trial.py` are trial-only and are deleted when the library lands; `fake_herdr.py` stays and grows. The trial's daemon-scan and teardown gotchas leave with them, because the library starts no Codex and stops only units it started. Risk: the trial cannot be rerun from this tree; git history keeps it.
12. **Standalone launcher name** is `herdr-claude` (a zsh function in `/etc/nixos/modules/common/shell.nix`, named for the user). Risk: a rename is one line there.
13. **Resume after a herdr server loss uses the session ids we already record** (shutdown records and the agent-sessions snapshot), not herdr's Claude integration. Installing the integration writes `~/.claude` hooks and settings, so it is a Pending decision in the shutdown phase.

## Delegation Context

- **Project:** `~/.claude` config repo. Worktree `/home/natepiano/worktrees/claude-build-followups-herdr-trial`, branch `build-followups-herdr-trial` (unit `herdr-trial-unit` unless the showrunner assigns another); name both in every dispatch.
- **Project started:** 2026-10-13T19:45:42.848+00:00
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

### Phase 1 — The herdr library and session commands  · status: done

#### As-built

- `scripts/herdr/herdr.py` runs named herdr sessions and locates processes in them. `herdr_binary()` is `HERDR_BIN`, else `shutil.which("herdr")`, else `HerdrMissing`. `Herdr(session)` targets one session (`argv` is `[binary, "--session", session, *args]`) with `run`, `result`, `panes`, `lookup`, `pane_id`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until -> ObservedAgentState | WaitTimedOut`, `read(pane, lines, *, joined, ansi)`, `send_keys`, `close_pane`, `agents`. A non-zero exit raises `HerdrFailure` carrying herdr's `error.code`/`error.message`; `AgentBlocked` (from `prompt`, never retried) and `AgentNotReady(screen)` (from `start_agent`, no key sent) subclass it.
- Session functions: `live_sessions() -> list[SessionEntry]` (running only); `server_unit(name)` is `herdr-<name>` with characters outside `[A-Za-z0-9_.-]` replaced by `-`; `start_server(name, timeout_ms)` returns when `status server` prints `status: running`, else starts `systemd-run --user --unit=<server_unit> --collect <herdr> --session <name> server` and polls to the deadline; `stop_session(name)` runs `server stop`, then `systemctl --user stop <server_unit>` when still active. `SYSTEMD_RUN` and `SYSTEMCTL` name those programs.
- Module `open_pane(name, label, cwd, environment)` and `start_agent(name, pane, kind, argv, opening_prompt, timeout_ms)` mirror the `Herdr` methods; module `start_agent` uses the pane's label as the agent name.
- `herdr_place_of(pid) -> HerdrPlace | NotInHerdr` reads `HERDR_PANE_ID` from the process environment and walks ancestors under `HERDR_PROC` (default `/proc`) to the herdr server. `herdr_processes() -> list[HerdrProcess]` lists the user's processes holding `HERDR_PANE_ID`, each with `pid`, `place`, `marks` (`SHOWRUNNER_UNIT`, `SHOWRUNNER_UNIT_ID` only), `command`, `cwd`; unreadable or vanished processes are skipped.
- Trust lives here for every launcher: `repository_trust_key(directory)` and `trusted_by_claude(directory, claude_json)`.
- Every timeout is integer milliseconds passed as an argument; the library reads no `HERDR_TRIAL_*` and no account variable — callers pass the pane environment.
- `scripts/herdr/herdr_sessions.py`: `list` prints `<name>\t<role>\t<claude name>` per running session (role `unit <slug>/<unit>`, `showrunner` or `standalone`; claude name is the newest live session record `herdr_place_of` puts there); `attach <name>` execs `herdr session attach <name>` and refuses a name not running, listing the running ones. Exit 0 done, 1 not found, 2 usage, 3 herdr unreadable.

**Files:**
- `scripts/herdr/herdr.py`, `scripts/herdr/herdr_sessions.py` — library and CLI; `test_herdr.py`, `test_herdr_sessions.py` — their tests; `fake_herdr.py` — the fake herdr.
- `scripts/herdr/trial.py`, `scaffold_files.py`, `test_trial.py` — deleted.
- `docs/as-built/herdr-trial.md` — notes the removed trial driver.
- `pyrightconfig.json` — a `scripts/herdr` execution environment (extra paths `scripts/herdr`, `scripts/message`, `scripts/production`, `scripts/lib`).

**Binds later work:** herdr 0.9.0 sets these pane variables: `HERDR_PANE_ID`, `HERDR_SESSION`, `HERDR_SOCKET_PATH`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID`, `HERDR_BIN_PATH`, `HERDR_ENV=1`. Callers map `HerdrFailure` to their "herdr unreadable" exit.

**Gotchas:**
- The session comes from the ancestor server's argv — a `herdr` argv whose global options (`--session`, `--remote`, `--remote-keybindings`) are followed by exactly `server`; no `--session` means `default`. Never `HERDR_SESSION`: it is inherited. A "server" substring test misfiles herdr clients.
- `repository_trust_key` strips inherited `GIT_*` variables and runs git with `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`.
- `Herdr.run` turns a subprocess timeout into `HerdrFailure`.
- `fake_herdr.py` is a Python script whose startup can pass 100 ms under load: tests that bound a herdr call need ≥10 000 ms.

**Ruled out:** a store-path fallback in `herdr_binary` — a garbage-collected path fails late.

### Phase 2 — Unit lookup, roster and shutdown inventory see herdr sessions  · status: done

#### As-built

- `scripts/production/unit_lookup.py`: `TmuxPlace(pane)` and `HerdrPlace` make `Place`; `MarkedUnit` is a frozen dataclass with `place` (`pane` is the tmux pane id, empty for herdr, until every consumer reads `place`). `_claudes()` returns live Claudes by tmux pane and by `HerdrPlace` (records with an empty `tmux` go through `herdr_place_of`). `_herdr_marked` yields marked herdr panes once per place from `herdr_processes()` in running sessions. `every_marked_unit(*, include_tmux=True)` reads both sources and raises `OSError` only when every included source fails; a unit marked in both hosts is a production fault. CLI `pane`/`list` print a tmux unit as the bare pane id (`%41`, unchanged, so `tmux send-keys -t` keeps working) and a herdr unit as `herdr:<session>:<pane>` (`place_text`).
- `scripts/message/roster.py`: keeps its tmux-binary check and passes it as `include_tmux`, so a machine without tmux never builds one through nix; a socketless director is matched by tmux pane or `herdr_place_of(pid)`; the worktree fallback reuses the read marks (`_ProductionUnitMarks`). `_herdr_codex_panes` lists interactive Codex panes as freestanding rows that cannot receive messages; a herdr failure is a problem line.
- `scripts/shutdown/inventory.py`: `HerdrHost {kind, herdr_session, pane_id}` joins `Host` and `SessionHost` (from `herdr_place_of(pid)` after `TMUX_PANE`). `UnitHost` has `session_name` (old key `tmux_session` parsed) and `session_host: "tmux" | "herdr"` from the mark's place (absent in old records, read as `"tmux"`; anything else is refused).
- Stop sends no tmux command and records no tmux issue for a herdr unit or host; restart makes a herdr unit or host a manual resume (prints the command, launches nothing, schedules no prompt); a tmux unit keeps `--tmux-session`. Shutdown text: `herdr <session>`, and `unit <production>/<unit> (herdr <session>)` for a herdr unit.
- `pyrightconfig.json`: `scripts/herdr` in the production, message and shutdown environments; the copied-script fixtures in `test_unit_status.py` and `test_update_registration.py` copy `scripts/herdr`.
- The `test_waiting.py` quota case moved to Phase 5 (held by two-accounts Phase 14). Tests need no `HERDR_BIN` override on a machine with real herdr.

### Phase 3 — Trust checked before every launch  · status: moved

Moved to `docs/plans/build-followups-herdr-trust.md` (unit `herdr-trust-unit`), run in parallel with Phase 5. Phase 6 waits for it.

### Phase 4 — Typing into and reading sessions on either host  · status: moved

Moved to `docs/plans/build-followups-herdr-panes.md` (unit `herdr-panes-unit`), run in parallel with Phase 5. Phase 6 waits for it.

### Phase 5 — Shutdown and restart for herdr sessions  · status: done

**User sees:** before — `/shutdown` lists a herdr session but leaves it running and prints a manual restart line; after — `/shutdown` stops herdr sessions safely and `restart` brings each back in its own herdr session with its conversation, tmux sessions as before.

#### Work Order

**Goal:** the shutdown conductor stops and restarts sessions hosted in herdr, units and others, with the same safety as tmux.

**Spec:**

1. `stop.py`: a unit in a herdr session is stopped as a tmux unit is (Claude exits first through the usual stop), then `herdr.stop_session(<session_name>)`; a session still running afterwards is the issue `unit session left running` with `host: "tmux" | "herdr"` and `session_name`. `record.py` parses the old kind `unit tmux session left running` with `tmux_session` as `host: "tmux"`.
2. `restart.py`:
   - A unit restart passes `--host-session <session_name>` to `add_unit.py` (`--tmux-session` is still accepted by `add_unit.py` until the tmux removal phase).
   - New `_herdr_launch(command, session, *, dry_run)` for a `herdr` host: `start_server(name)`; when the recorded pane is gone or holds no agent, `open_pane(name, name, cwd, environment)` with the session's config dir variables; then `start_agent(..., kind="claude", argv=<resume argv without the prompt>, opening_prompt=<restart prompt>)`. A pane still holding a live agent is a refusal, as an occupied tmux pane is today.
   - `case "herdr"` in the host dispatch calls it; the Phase 2 manual-restart stand-in is removed.
3. `settle.py` issue text and `shutdown.py` host text cover the herdr forms. `commands/shutdown.md` line 33 names herdr sessions beside tmux sessions.
4. Live check (not a test): a throwaway Claude in herdr session `herdr-stopcheck` in a trusted scratch folder; `shutdown.py down <account> --here --only herdr-stopcheck --restart`; it stops, comes back in `herdr-stopcheck` and answers a question about its earlier turn. Then stop the herdr server by hand (`herdr --session herdr-stopcheck server stop`), and confirm `claude --resume <id>` in a new herdr session restores the conversation without herdr's Claude integration. Stop and delete the session.
5. Carried from Phase 2, once the showrunner says two-accounts Phase 14 landed (merge `origin/build-followups` first): `scripts/production/test_waiting.py` proves `waiting.py quota` reaches a herdr unit; `waiting.py` itself is not changed.

**Decided by the showrunner, 2026-10-13: no herdr Claude integration.** Resume by session id works, and the install would write `~/.claude` settings and hooks. The record below keeps the reasoning.

Actual problem:
herdr offers `herdr integration install` for Claude, which would let herdr itself resume Claude after a server loss. It writes into `~/.claude/hooks/` and `settings.json`, which every session on the machine loads.

What exists now:
- Shutdown records keep each session's id, cwd, account and host; `restart.py` resumes from them.
- The nixos agent-sessions snapshot keeps session ids for a reboot.
- The integration is not installed (herdr trial finding 8).

What should change:
- Nothing installed. This phase's live check proves resume by session id after a server stop; if it fails, this decision comes back with the failure.

Recommendation:
Do not install the integration. Our own records already resume every session, and the install changes hooks and settings for every session on the machine.

**Files:**
- `scripts/production/test_waiting.py` — quota reaches a herdr unit (carried from Phase 2).
- `scripts/shutdown/stop.py` — herdr unit stop.
- `scripts/shutdown/record.py` — generalized issue kind, old form parsed.
- `scripts/shutdown/restart.py` — `_herdr_launch`, `--host-session`.
- `scripts/shutdown/settle.py` — issue text.
- `scripts/shutdown/shutdown.py` — host text.
- `scripts/shutdown/test_stop.py` — herdr unit stop and leftover.
- `scripts/shutdown/test_record.py` — old and new issue forms.
- `scripts/shutdown/test_restart.py` — herdr host restart, occupied pane refused.
- `scripts/shutdown/test_settle.py` — issue text.
- `scripts/production/add_unit.py` — accept `--host-session` beside `--tmux-session`.
- `scripts/production/test_add_unit.py` — the new flag.
- `commands/shutdown.md` — herdr sessions named.
- `docs/as-built/shutdown-and-restart.md` — herdr host.

**Seats:** 2 writers (no test lane; `test` opens as impl); split stop and record against restart.
- `impl` — `restart.py`, `add_unit.py` and their tests, `shutdown-and-restart.md`; hub: `scripts/shutdown/restart.py`.
- `test` (opens as impl) — `stop.py`, `record.py`, `settle.py`, `shutdown.py`, `commands/shutdown.md` and their tests.

**Constraints from prior phases:** `HerdrHost {kind: "herdr", herdr_session, pane_id}`; `UnitHost.session_name` (old key `tmux_session` still parsed) and `UnitHost.session_host: "tmux" | "herdr"` (absent in old records, read as `"tmux"`) — branch on `session_host`, never on the name; today a herdr unit's stop sends no tmux command and records no tmux issue, and its restart is a manual resume, both of which this phase replaces. `session_pane.py` and the `settle._screen` herdr read come from herdr-panes-unit (`docs/plans/build-followups-herdr-panes.md`), which lands before this phase's checkpoint: merge `origin/build-followups` before relying on them. `herdr.py` `start_server`, `open_pane`, `start_agent`, `stop_session`, `AgentNotReady`; `session_pane.py` reads and types on either host; `settle._screen` reads herdr hosts.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/shutdown`, `scripts/production` and `scripts/herdr` green; `basedpyright scripts/shutdown scripts/production scripts/herdr` prints `0 errors, 0 warnings`. Tests cover a herdr unit stopped and its session stopped, a herdr session left running reported, restart into a gone pane and refusal on an occupied one, and old shutdown records parsing. The live check in Spec 4 passes.

### Phase 6 — Units and child showrunners launch in herdr  · status: todo

**Blocked by:** the user's nixos rebuild with herdr installed on natedev and herdr servers in earlyoom's `--avoid` list (Spec 1).

**User sees:** before — new units and child showrunners start in tmux and the user opens them with `tmux attach -t <name>`; after — they start in their own herdr session, the launch line ends `herdr session attach <name>`, and `herdr_sessions.py list` names them; tmux sessions already running are untouched.

#### Work Order

**Goal:** every launcher starts its Claude in its own named herdr session with its marks, and every restore relaunches there.

**Spec:**

1. **For the user, outside this repo (no seat makes these):**
   - `/etc/nixos`: add herdr from the locked nixpkgs-unstable input (the trial used rev `8ce4ef6cb6f871616146b9fe26d2a5ae594e94fe`, `#herdr`) to natedev's home packages.
   - `/etc/nixos/modules/linux/memory.nix`: add the herdr server's process name to the `--avoid` regex beside `tmux: server`. Measure the name first with `ps -o comm= -p <server pid>` on a running herdr server.
   - Then `nixos-rebuild switch`. The phase starts when `command -v herdr` answers.
   - These `/etc/nixos` edits are the showrunner's. When Phase 5 checkpoints, the unit sends it the exact `--avoid` regex and the package attribute, and the showrunner has them ready for the user's rebuild before this phase starts.
2. `add_unit.py` `launch_session` becomes herdr-only for new launches: `start_server(<session name>)`; `open_pane(<session name>, <session name>, session_cwd, environment)` where environment is the marks, `ENABLE_TOOL_SEARCH=true`, and `named_account_environment(dir)` for a named account (none for the default); `start_agent(..., kind="claude", argv=["--model", m, "--effort", e?, "--resume", id?, "--remote-control", name, "-n", name, "--settings", '{"disableAgentView": true}'], opening_prompt=prompt_for(request))`. `AgentNotReady` exits 1 with the screen text and sends nothing. The caller's `CLAUDE_*` variables never reach the pane (`launch_environment`).
   - Refusals: a running herdr session of that name not marked as this unit (as `tmux_live` today).
   - Restore of a marked unit with no live Claude: a herdr place is ended with `stop_session`, a tmux place with `kill-session` (existing), then relaunched in herdr under `--host-session`.
   - `wait_for_remote_control` keeps reading the pane through `read_place` for `/remote-control is active`.
   - Printed lines: `<unit> started: herdr session attach <name>` and `<unit> restore launched: herdr session attach <name>`.
3. `add_showrunner.py` `launch`: the same herdr start with no marks and no account variables (the showrunner account); `tmux_live` becomes a check for a running herdr session of that name; printed lines and `later` text name `herdr session attach <slug>`.
4. `promote_showrunner.py`: no launch code of its own; its printed lines name `herdr session attach`.
5. `restart.py`: a `tmux` host whose session is a showrunner is relaunched through `_herdr_launch` into a herdr session of the same name, so a restart moves it; other tmux hosts respawn as before.
   - A unit whose `session_host` is `herdr` restarts through `add_unit.py --host-session <session_name>` like a tmux unit; remove the manual-restart guard Phase 5 keeps for it (before this phase `add_unit.py` launches only tmux, so routing a herdr unit there would move it into tmux).
6. Docs: `commands/showrunner/add_showrunner.md` restates the user's 2026-10-12 rule ("top level (L1) showrunners should make sure to use tmux to start lower level showrunners") as: an L1 showrunner starts lower showrunners with `add_showrunner.py`, which now opens each in its own herdr session; keep the quote and say the host changed with the user's 2026-10-13 move to herdr. Also `commands/showrunner/add_unit.md`, `promote_unit.md`, `units.md` (`gone` means no tmux or herdr session carries the mark), `produce.md` (lines 169–171, 325, 363, 1119), `commands/producer/to_production.md:104`, `docs/production_format.md:36,126`, and `production_lifecycle.py` line 482 (`close herdr or tmux sessions`).
7. Live check (not a test): `add_unit.py` for a throwaway standby unit of a scratch production in a trusted scratch repository; it starts in herdr, `/remote-control is active` is read, `production_lifecycle.py load` lists it live, the roster files it under the scratch showrunner, `herdr session attach <name>` opens it, `/shutdown --only <name> --restart` brings it back. Remove the scratch unit and session afterwards.

**Files:**
- `scripts/production/add_unit.py` — herdr launch and restore.
- `scripts/production/test_add_unit.py` — herdr launch argv, env, refusals, restore.
- `scripts/production/add_showrunner.py` — herdr launch.
- `scripts/production/test_add_showrunner.py` — herdr launch.
- `scripts/production/promote_showrunner.py` — printed lines.
- `scripts/production/test_promote_showrunner.py` — printed lines.
- `scripts/production/production_lifecycle.py` — wrap text.
- `scripts/production/test_production_lifecycle.py` — wrap text.
- `scripts/shutdown/restart.py` — tmux showrunner restarts into herdr.
- `scripts/shutdown/test_restart.py` — that case.
- `commands/showrunner/add_showrunner.md` — herdr, with the user's rule restated.
- `commands/showrunner/add_unit.md` — herdr attach line.
- `commands/showrunner/promote_unit.md` — herdr attach line.
- `commands/showrunner/units.md` — `gone` meaning.
- `commands/showrunner/produce.md` — launch and attach lines.
- `commands/producer/to_production.md` — line 104.
- `docs/production_format.md` — lines 36 and 126.

**Seats:** 2 writers (no test lane; `test` opens as impl); split unit launch against showrunner launch, restart and docs.
- `impl` — `add_unit.py`, `test_add_unit.py`, `production_lifecycle.py`, `test_production_lifecycle.py`; hub: `scripts/production/add_unit.py` (`launch_environment` and `Refusal` are imported by `add_showrunner.py`).
- `test` (opens as impl) — `add_showrunner.py`, `promote_showrunner.py`, `restart.py` and their tests, and the seven docs.

**Constraints from prior phases:** trust is checked in `preflight` and `add_showrunner.checked` through `herdr.claude_trust`; `session_pane.py` and `read_place` read either host; `MarkedUnit.place` replaced `pane`; `restart.py` has `_herdr_launch` and passes `--host-session`, which `add_unit.py` accepts beside `--tmux-session`; a herdr unit's restart is still a manual resume line until Spec 5 here; `herdr.py` `start_server`, `open_pane`, `start_agent`, `stop_session`, `live_sessions`.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Tests show the herdr argv carries no prompt, the opening prompt goes once through `agent prompt`, the pane environment holds both marks and the account variables and no `CLAUDE_*` from the caller, and `AgentNotReady` sends nothing. The live check in Spec 7 passes.

### Phase 7 — Standalone sessions and the nightly review in herdr  · status: todo

**User sees:** before — a standalone `claude` runs straight in a ghostty window, and the nightly review runs in tmux; after — `herdr-claude [name]` opens a standalone Claude in its own herdr session in the current terminal, `continue` does the same with `--continue`, a restart brings a ghostty standalone back in herdr in a ghostty window attached to it, and the nightly review runs in herdr session `nightly-review`.

#### Work Order

**Goal:** standalone Claude sessions and the nightly review start, restart and restore in their own herdr sessions.

**Spec:**

1. `herdr_sessions.py` gains:
   - `start [--name N] [--cwd D] [-- <claude args>]`: name defaults to the cwd's basename plus a short suffix when taken; refuses an untrusted folder (`claude_trust`, the account from `CLAUDE_CONFIG_DIR` or the default); `start_server`, `open_pane`, `start_agent(kind="claude", argv=[*claude args, "-n", name])` with no opening prompt (the user types); then execs `herdr session attach <name>`.
   - `resume <name> --session-id <id> --cwd <dir> [--config-dir <dir>]`: the same with `--resume <id>`, for the agent-sessions restore and by hand; no attach unless `--attach`.
2. `restart.py`: a `ghostty`, `zed` or `terminal` host restarts through `herdr_sessions.py resume <name> ...` and then opens one ghostty window running `herdr session attach <name>` on the recorded desktop (the existing window opener with the new command).
3. `nightly_review.py`: the session runs in herdr session `nightly-review` (`start_server`, `open_pane`, `start_agent` with the `/nightly_review config rust` opening prompt); its has-session, attached-client and kill checks use `live_sessions()`, `agents()` and `stop_session`. Printed lines name `herdr session attach nightly-review`. `commands/nightly_review.md` names the same.
4. **Outside this repo (the showrunner's, in `/etc/nixos`):** `nightly-review.nix` drops tmux from its path; `agent-sessions-snapshot.py` and `agent-sessions-restore.py` drop their tmux branches. **The tmux package stays installed, and `tmux: server` stays in earlyoom's `--avoid`:** this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand.
   - `/etc/nixos/modules/common/shell.nix`: `herdr-claude() { ~/.claude/scripts/lib/py ~/.claude/scripts/herdr/herdr_sessions.py start "$@"; }` and `alias continue='herdr-claude -- --continue'` inside the existing `CLAUDECODE` guard.
   - `/etc/nixos/modules/linux/nightly-review.nix`: herdr in the path beside tmux, comments naming `herdr session attach nightly-review`.
   - `/etc/nixos/modules/linux/agent-sessions-snapshot.py` and `agent-sessions-restore.py`: a Claude whose ancestry ends at a herdr server is recorded with its herdr session name, and restored with `herdr_sessions.py resume <name> --session-id <id> --cwd <dir> [--config-dir <dir>]` plus a ghostty window running `herdr session attach <name>`.

**Files:**
- `scripts/herdr/herdr_sessions.py` — `start`, `resume`.
- `scripts/herdr/test_herdr_sessions.py` — both verbs, trust refusal, name collision.
- `scripts/shutdown/restart.py` — window hosts restart into herdr.
- `scripts/shutdown/test_restart.py` — window host case.
- `scripts/nightly_review/nightly_review.py` — herdr session.
- `scripts/nightly_review/test_nightly_review.py` — herdr cases.
- `commands/nightly_review.md` — attach line.
- `docs/as-built/shutdown-and-restart.md` — window hosts restart into herdr.

**Seats:** 2 writers (no test lane; `test` opens as impl); split standalone and restart against the nightly review.
- `impl` — `herdr_sessions.py`, `restart.py`, their tests and `shutdown-and-restart.md`; hub: `scripts/herdr/herdr_sessions.py`.
- `test` (opens as impl) — `nightly_review.py`, its test, `commands/nightly_review.md`.

**Constraints from prior phases:** herdr is on PATH on natedev; launchers start in herdr; `restart.py` has `_herdr_launch`; `herdr.claude_trust` and `trust_command`; `herdr_sessions.py list` and `attach` exist.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr`, `scripts/shutdown` and `scripts/nightly_review` green; `basedpyright scripts/herdr scripts/shutdown scripts/nightly_review` prints `0 errors, 0 warnings`. Live (not a test): `herdr_sessions.py start --name herdr-standalone-check` in a trusted folder opens an attached Claude; `/shutdown --only herdr-standalone-check --restart` brings it back in a ghostty window attached to its herdr session; `nightly_review.py launch --dry-run` (or its check mode) names the herdr session. Remove the check session afterwards.

### Phase 8 — Remove tmux from productions and sessions on Linux  · status: todo

**Blocked by:** no live session on natedev runs in tmux (`unit_lookup.py` and the shutdown inventory find no tmux host).

**User sees:** before — some sessions may still run in tmux and every tool carries two hosts; after — every showrunner, unit, standalone and the nightly review runs in herdr, `tmux` appears in no production command, and the docs name only herdr.

#### Work Order

**Goal:** the tmux code paths, tests, jobs and docs are removed, leaving herdr as the only session host on Linux.

**Spec:**

1. Remove: `unit_lookup.py` tmux source (`tmux_binary`, `_tmux`, `tmux_sessions`, `_marks`, `mark`, the CLI `mark`, `TmuxPlace`); `tmux_names.py` and its test, its registration in `update_registration.py` and its sender name in `conversation_pause.py`; `adopt.py` and its test; `fake_tmux.py`; tmux branches in `session_pane.py`, `add_unit.py` (`tmux_live`, `kill-session`, `--tmux-session`), `add_showrunner.py`, `promote_showrunner.py`, `unit_effort.py`, `stall_watch.py`, `settle.py`, `stop.py`, `restart.py` (`_tmux_launch`, pane state, `TmuxHost` restart), `inventory.py` (`TmuxHost` and its types), `record.py`, `shutdown.py`, `roster.py`. `record.py` keeps reading an old record with a tmux host as a manual restart.
2. A `notifier.sh remove tmux-names` for each production registered with it is printed for the showrunners in the phase report; nothing removes another production's instance from a seat.
3. Docs: remove every remaining tmux line from `commands/showrunner/*.md`, `commands/shutdown.md`, `docs/production_format.md`; update as-builts `showrunner-automation`, `session-roster`, `shutdown-and-restart`, `session-notifier`, `agent-registry`, `build-followups-fn-length-hook` where they describe tmux hosts.
4. **Outside this repo (the showrunner's, in `/etc/nixos`):** `nightly-review.nix` drops tmux from its path; `agent-sessions-snapshot.py` and `agent-sessions-restore.py` drop their tmux branches. **The tmux package stays installed, and `tmux: server` stays in earlyoom's `--avoid`:** this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand. `/etc/nixos/modules/linux/memory.nix` drops `tmux: server` from `--avoid`; `nightly-review.nix` drops tmux from its path; `agent-sessions-snapshot.py` and `agent-sessions-restore.py` drop their tmux branches. Whether tmux stays installed for the user's own use is the user's choice.

**Decided by the showrunner, 2026-10-13: move the running tmux sessions at their next idle point, showrunners first.** The showrunner tells each owning showrunner before its sessions move. The record below keeps the reasoning.

Actual problem:
This phase cannot start while any session runs in tmux. Some units are long-running (for example screenshot-unit) and might keep tmux alive for weeks.

What exists now:
- After Phase 6, `/shutdown down <account> --only <names> --restart` stops a session safely and brings it back in herdr with its conversation (units through `add_unit.py`, showrunners through `_herdr_launch`).
- Each such restart interrupts that session for about a minute.

What should change:
- Move each tmux session at its next idle point (between phases for a unit) with `/shutdown --only <name> --restart`, starting with the showrunners.

Recommendation:
Move them at their next idle points rather than waiting for productions to end, so tmux removal does not wait on the longest production.

**Files:**
- `scripts/production/unit_lookup.py` — tmux source removed.
- `scripts/production/test_unit_lookup.py` — tmux cases removed.
- `scripts/production/tmux_names.py` — deleted.
- `scripts/production/test_tmux_names.py` — deleted.
- `scripts/production/adopt.py` — deleted.
- `scripts/production/test_adopt.py` — deleted.
- `scripts/production/fake_tmux.py` — deleted.
- `scripts/production/update_registration.py` — no tmux-names job.
- `scripts/production/test_update_registration.py` — follows.
- `scripts/hooks/conversation_pause.py` — sender list.
- `scripts/production/session_pane.py` — herdr only.
- `scripts/production/add_unit.py` — herdr only.
- `scripts/production/add_showrunner.py` — herdr only.
- `scripts/production/promote_showrunner.py` — herdr only.
- `scripts/production/unit_effort.py` — herdr only.
- `scripts/production/stall_watch.py` — herdr only.
- `scripts/production/unit_status.sh` — no tmux mention.
- `scripts/production/test_add_unit.py` — follows.
- `scripts/production/test_add_showrunner.py` — follows.
- `scripts/production/test_promote_showrunner.py` — follows.
- `scripts/production/test_unit_effort.py` — follows.
- `scripts/production/test_stall_watch.py` — follows.
- `scripts/production/test_session_pane.py` — follows.
- `scripts/production/test_codex_winddown.py` — follows the fake's removal.
- `scripts/production/test_showrunners.py` — follows the fake's removal.
- `scripts/production/test_live_units.py` — follows.
- `scripts/production/test_waiting.py` — follows.
- `scripts/production/test_production_lifecycle.py` — follows.
- `scripts/production/test_unit_status.py` — follows.
- `scripts/shutdown/inventory.py` — `TmuxHost` removed.
- `scripts/shutdown/stop.py` — herdr only.
- `scripts/shutdown/record.py` — old tmux records read as manual restart.
- `scripts/shutdown/restart.py` — `_tmux_launch` removed.
- `scripts/shutdown/settle.py` — herdr only.
- `scripts/shutdown/shutdown.py` — host text.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/test_stop.py` — follows.
- `scripts/shutdown/test_record.py` — follows.
- `scripts/shutdown/test_restart.py` — follows.
- `scripts/shutdown/test_settle.py` — follows.
- `scripts/message/roster.py` — herdr only.
- `scripts/message/test_roster.py` — follows.
- `scripts/message/test_top_level.py` — follows.
- `scripts/nightly_review/nightly_review.py` — no tmux.
- `commands/showrunner/produce.md` — no tmux.
- `commands/showrunner/dailies.md` — no tmux.
- `commands/showrunner/effort.md` — no tmux.
- `commands/shutdown.md` — no tmux.
- `docs/production_format.md` — no tmux.
- `docs/as-built/showrunner-automation.md` — herdr hosts.
- `docs/as-built/session-roster.md` — herdr hosts.
- `docs/as-built/shutdown-and-restart.md` — herdr hosts.
- `docs/as-built/session-notifier.md` — no tmux-names job.
- `docs/as-built/agent-registry.md` — herdr hosts.
- `docs/as-built/build-followups-fn-length-hook.md` — herdr hosts.

**Seats:** 2 writers (no test lane; `test` opens as impl); split `scripts/production` and `scripts/hooks` against `scripts/shutdown`, `scripts/message`, `scripts/nightly_review` and the docs.
- `impl` — every `scripts/production/` and `scripts/hooks/` file above; hub: `scripts/production/unit_lookup.py`.
- `test` (opens as impl) — every `scripts/shutdown/`, `scripts/message/`, `scripts/nightly_review/` file, the commands and the docs.

**Constraints from prior phases:** every launcher starts in herdr; `session_pane.py`, `stop.py`, `restart.py` and `settle.py` handle `HerdrHost`; `restart.py` moves tmux showrunners and window standalones into herdr; `herdr_sessions.py` has `list`, `attach`, `start`, `resume`.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown`, `scripts/message`, `scripts/hooks`, `scripts/nightly_review` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/message scripts/hooks scripts/nightly_review scripts/herdr` prints `0 errors, 0 warnings`; `grep -rln tmux scripts/production scripts/shutdown scripts/message commands/showrunner` names only lines kept on purpose (old-record parsing), each listed in the phase report.

### Phase 9 — herdr on the Mac  · status: todo

**Packaging (showrunner, 2026-10-13):** when this phase is reached, split it into its own plan if it is more than one phase of work.

**Blocked by:** Phase 8 done on natedev; the showrunner may move this phase to its own plan.

**User sees:** before — Mac standalones run straight in Ghostty windows, and Mac sessions have no herdr; after — `herdr-claude` on the Mac opens a standalone in its own herdr session, and a Mac restart brings sessions back in herdr in a Ghostty window attached to them.

#### Work Order

**Goal:** the herdr library, standalone launcher and restart work on the Mac, with launchd in place of systemd.

**Spec:**

1. `herdr.py` `start_server` and `stop_session` on Darwin use `launchctl submit -l <server_unit> -- <herdr> --session <name> server` and `launchctl remove <server_unit>`; Linux keeps systemd. The platform is chosen once by `sys.platform`; tests cover both with fake programs (`LAUNCHCTL` env names the program).
2. `herdr_place_of` and `herdr_processes` on Darwin read process ancestry, argv and environment through `ps -E -o pid=,ppid=,command=` (the Mac has no `/proc`); `HERDR_PS` names the program for tests.
3. `restart.py` `terminal` host on the Mac restarts through `herdr_sessions.py resume` and opens `Ghostty.app` with `-e herdr session attach <name>`.
4. **Outside this repo (the showrunner's, in `/etc/nixos`):** `nightly-review.nix` drops tmux from its path; `agent-sessions-snapshot.py` and `agent-sessions-restore.py` drop their tmux branches. **The tmux package stays installed, and `tmux: server` stays in earlyoom's `--avoid`:** this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand. `~/nixos` (nix-darwin): herdr in the Mac's packages, and `herdr-claude` and the `continue` alias in `modules/common/shell.nix` (shared with natedev, already added in Phase 7).
5. Live check on the Mac (needs the user at the Mac for the Ghostty window): `herdr-claude --name herdr-mac-check`, `/shutdown --only herdr-mac-check --restart`, the conversation comes back attached. Remove the check session afterwards.

**Files:**
- `scripts/herdr/herdr.py` — launchd and `ps` paths.
- `scripts/herdr/test_herdr.py` — Darwin cases.
- `scripts/shutdown/restart.py` — Mac terminal host into herdr.
- `scripts/shutdown/test_restart.py` — that case.

**Seats:** 2 writers (no test lane; `test` opens as impl); split the library against restart.
- `impl` — `herdr.py`, `test_herdr.py`; hub: `scripts/herdr/herdr.py`.
- `test` (opens as impl) — `restart.py`, `test_restart.py`.

**Constraints from prior phases:** `herdr.py` runs servers as systemd user units on Linux and finds places through `/proc`; `herdr_sessions.py start|resume|list|attach`; `restart.py` restarts window hosts into herdr on Linux; tmux is gone from the session tools.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr` and `scripts/shutdown` green on natedev and on the Mac; `basedpyright scripts/herdr scripts/shutdown` prints `0 errors, 0 warnings`. The live check in Spec 5 passes.
