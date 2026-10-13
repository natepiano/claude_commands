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

### Phase 2 — Unit lookup, roster and shutdown inventory see herdr sessions  · status: todo

**User sees:** before — a Claude unit in herdr reads `session: gone` in `production_lifecycle.py load`, `freestanding` in the roster, is missed by `waiting.py quota`, and shows as an unknown host in `/shutdown`; after — all four list herdr-hosted units and sessions correctly, while tmux units read as before.

#### Work Order

**Goal:** every tool that finds units or sessions through `unit_lookup`, the roster or the shutdown inventory finds them in herdr as well as in tmux.

**Spec:**

1. `unit_lookup.py`:
   - New frozen dataclasses `TmuxPlace(pane: str)` and `HerdrPlace` (imported from `herdr.py`); `Place = TmuxPlace | HerdrPlace`. `MarkedUnit` gains `place: Place`. `pane` stays for this phase as the tmux pane id, `""` for a herdr place (Phase 4 moves every consumer to `place` and removes `pane`). `label` is the tmux session label or the herdr session name.
   - `_claudes()` returns two maps: by tmux pane (as now) and by `HerdrPlace`, filled through `herdr.herdr_place_of(record["pid"])` for each live record whose `tmux` is empty.
   - New `_herdr_marked()` yields `(slug, MarkedUnit)` for each herdr pane whose process marks carry both `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` (`herdr.herdr_processes()`; one entry per place, the pane's shell or its Claude). Its Claude is the live record at that place, else `ClaudeUnknown`/`ClaudeNotRunning` as for tmux.
   - `every_marked_unit()` reads both sources. No tmux binary, or no tmux server, is not an error once herdr answered; it raises `OSError` only when neither source can say. A unit marked in both hosts is a fault like two tmux sessions with one mark (`tmux session X and herdr session Y both carry the mark of U`).
   - CLI `list` prints the place as `tmux:%41` or `herdr:<session>:<pane>` in column 2; `pane` prints the same form. `mark` stays tmux-only (herdr marks are set at launch).
2. `scripts/message/roster.py` `_production_places`: drop the `has_tmux` gate (the lookup decides); a socketless director is matched by its tmux pane or by `herdr_place_of(record["pid"]) == place`. New `_herdr_codex_panes(machine)`: for each `herdr.live_sessions()` entry, `Herdr(name).agents()` rows with `agent == "codex"` become `RosterEntry(kind="codex", role="freestanding", name=f"herdr {session}:{pane}", status=agent_status, reach=CannotReceiveMessages("interactive Codex in a herdr pane"), cwd=<process cwd or "">, showrunner=NoShowrunner(), director=NoDirector(), assignment=NotAWorker())`; `_local_roster` appends them. A herdr failure adds a problem line, never fails the roster.
3. `scripts/shutdown/inventory.py`:
   - New `HerdrHost(TypedDict)`: `kind: Literal["herdr"]`, `herdr_session: str`, `pane_id: str`. Add it to `Host` and `SessionHost`.
   - `UnitHost.tmux_session` is renamed `session_name`; `UnitMarks.tmux_session` likewise. The wire parser accepts the old key `tmux_session` for records written before this phase.
   - `_host`: after the unit branch, `TMUX_PANE` as now; else `herdr.herdr_place_of(pid)` gives a `HerdrHost`; else the window and terminal branches as now.
   - `_unit_sessions` uses `marked_unit.label` for `session_name` for both hosts.
4. `scripts/shutdown/record.py` and `scripts/shutdown/shutdown.py`: parse and print `HerdrHost` (`herdr <session>`); `restart.py` and `stop.py` treat a `herdr` host as a manual restart with its printed command until Phase 5 (one `case "herdr"` each).
5. `scripts/production/waiting.py` is not changed; a test proves `quota` reaches a herdr unit.
6. `pyrightconfig.json`: add `scripts/herdr` to `extraPaths` of the `scripts/production`, `scripts/message` and `scripts/shutdown` environments. Each importer inserts `scripts/herdr` on `sys.path` as `unit_lookup.py` does for `scripts/message`.

**Files:**
- `scripts/production/unit_lookup.py` — herdr source, `place`, CLI place form.
- `scripts/production/test_unit_lookup.py` — herdr and mixed-host cases.
- `scripts/production/test_waiting.py` — quota reaches a herdr unit.
- `scripts/production/test_live_units.py` — a herdr unit listed.
- `scripts/message/roster.py` — places through herdr, Codex panes.
- `scripts/message/test_roster.py` — herdr director placed, Codex pane row, herdr failure is a problem line.
- `scripts/shutdown/inventory.py` — `HerdrHost`, `session_name`.
- `scripts/shutdown/record.py` — parse `HerdrHost` and the old key.
- `scripts/shutdown/shutdown.py` — host text.
- `scripts/shutdown/restart.py` — `herdr` host is a manual restart for now.
- `scripts/shutdown/stop.py` — `herdr` host stop leaves the session, as an unknown host does.
- `scripts/shutdown/test_inventory.py` — herdr host and unit.
- `scripts/shutdown/test_record.py` — old and new keys.
- `pyrightconfig.json` — `scripts/herdr` in three environments.

**Seats:** 2 writers (no test lane; `test` opens as impl); split production/message against shutdown.
- `impl` — `unit_lookup.py`, `roster.py`, `pyrightconfig.json` and their tests (`test_unit_lookup.py`, `test_waiting.py`, `test_live_units.py`, `test_roster.py`); hub: `scripts/production/unit_lookup.py` (`MarkedUnit` and `Place` are what the other writer reads).
- `test` (opens as impl) — `inventory.py`, `record.py`, `shutdown.py`, `restart.py`, `stop.py`, `test_inventory.py`, `test_record.py`.

**Constraints from prior phases:** `scripts/herdr/herdr.py` gives `Herdr(session)`, `live_sessions()`, `herdr_place_of(pid) -> HerdrPlace | NotInHerdr`, `herdr_processes() -> list[HerdrProcess]` (`pid`, `place`, `marks`, `command`, `cwd`), `HerdrPlace(session, pane)`, `HerdrFailure`, `HerdrMissing`; `HERDR_BIN` and `HERDR_PROC` point tests at `fake_herdr.py` and a fake `/proc`. Measured in Phase 1: a herdr 0.9.0 pane sets `HERDR_PANE_ID`, `HERDR_SESSION`, `HERDR_SOCKET_PATH`, `HERDR_TAB_ID`, `HERDR_WORKSPACE_ID`, `HERDR_BIN_PATH` and `HERDR_ENV=1`; the library takes the session from the ancestor server's argv (global options, then exactly `server`), never from `HERDR_SESSION`, so a herdr client run inside a pane is not a server. `HerdrProcess.marks` holds only `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID`. `pyrightconfig.json` already has a `scripts/herdr` environment (extraPaths `scripts/herdr`, `scripts/message`, `scripts/production`, `scripts/lib`); this phase adds `scripts/herdr` to the other three. `fake_herdr.py` is a Python script whose start can pass 100 ms under load: a test that bounds a herdr call gives it at least 10 000 ms.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'`, `-s scripts/message`, `-s scripts/shutdown` and `-s scripts/herdr` green; `basedpyright scripts/production scripts/message scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Tests cover a herdr unit found with a live Claude and with none, a tmux-only machine unchanged, no tmux binary with herdr units found, a unit marked in both hosts as a fault, the roster placing a herdr director, `HerdrHost` from a process with a herdr ancestor, and an old record with `tmux_session` parsing.

### Phase 3 — Trust checked before every launch  · status: todo

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

### Phase 4 — Typing into and reading sessions on either host  · status: todo

**User sees:** before — relays, `/compact`, effort changes, stall watch, `/unit:report` and the shutdown screen check work only for tmux sessions; after — they work the same for tmux and herdr sessions, and a relay to a unit blocked at a prompt is refused with a line saying so, not typed into the prompt.

#### Work Order

**Goal:** one host-neutral command types into, sends keys to and reads any unit or session, and every relay, compact, effort, stall, status and settle path uses it.

**Spec:**

1. New `scripts/production/session_pane.py`:
   - Targets: `unit <slug> <unit>` (through `unit_lookup.marked_units`) or `session <session id>` (the live record's pid: `TMUX_PANE` from its environment, else `herdr.herdr_place_of`).
   - `read <target> [--lines N] [--joined] [--ansi]`: tmux `capture-pane -p [-J] [-e] -S -N -t <pane>`; herdr `Herdr(session).read(pane, N, joined=..., ansi=...)`. Default 60 lines.
   - `type <target> <text>`: tmux `send-keys -t <pane> -l <text>` then `send-keys -t <pane> Enter` as two calls; herdr `prompt(pane, text)`. An `AgentBlocked` refusal exits 4 with `<target>: blocked at a prompt; nothing typed`.
   - `keys <target> <key>...`: tmux `send-keys -t <pane> <keys>`; herdr `send_keys(pane, *keys)`.
   - `where <target>` prints `tmux:<pane>` or `herdr:<session>:<pane>`.
   - Exit 0 done, 1 no such target, 2 usage, 3 host unreadable, 4 blocked. Library functions `read_place`, `type_place`, `keys_place` take a `Place` for Python callers.
2. Python callers move from `.pane` to `place` and these functions:
   - `unit_effort.py`: capture and `send` through `read_place(..., ansi=True)` and `type_place`; `verdict` unchanged.
   - `stall_watch.py`: the 400-line joined capture through `read_place(place, 400, joined=True)`; drop `TMUX`.
   - `promote_showrunner.py`: capture and `/exit` through `session_pane`; the tmux `kill-session` stays for a tmux place, and a herdr place is ended with `herdr.stop_session(label)` after Claude is gone.
   - `add_unit.py` `wait_for_remote_control`: reads through `read_place`.
   - `live_units.py` prints the place form.
   - `scripts/shutdown/settle.py` `_screen`: a `tmux` or `unit` host reads as now; a `herdr` host reads through `read_place(HerdrPlace(...), 60)`; the reason literal `"no tmux host"` becomes `"no pane host"`.
   - Remove `MarkedUnit.pane` (every consumer now reads `place`).
3. `unit_status.sh`: per unit, text from `session_pane.py read unit <slug> <unit> --lines 400 --joined` and the Claude session id from `unit_lookup.py list` (column 5); `pane_claude_pid`, `display-message` and the tmux binary lookup go.
4. Docs, each tmux command line replaced by the `session_pane.py` line:
   - `commands/showrunner/produce.md`: the relay (`type unit <slug> <unit> "From the user (via the showrunner): <words>"`, and on exit 4 tell the user the unit is at a prompt), `/compact` with its idle check (`read --ansi`: a suggestion after `❯` is dimmed, `\e[2m`, in both hosts), the go-ahead typing, and the hourly pane read.
   - `commands/showrunner/dailies.md` (`read --ansi` for the dimmed suggestion), `commands/showrunner/promote_unit.md` (stop through `session_pane.py type session <id> /exit`).
5. Live check (not a test): with `HERDR_BIN` set, a throwaway herdr session `herdr-iocheck` running `claude` in a trusted scratch folder: `read --ansi` shows `\e[2m` before a prompt suggestion; `type` delivers text; while a permission prompt is up, `type` exits 4. Stop and delete the session afterwards.

**Files:**
- `scripts/production/session_pane.py` — new.
- `scripts/production/test_session_pane.py` — new.
- `scripts/production/unit_lookup.py` — remove `pane`.
- `scripts/production/test_unit_lookup.py` — follow the field removal.
- `scripts/production/unit_effort.py` — host-neutral reads and typing.
- `scripts/production/test_unit_effort.py` — herdr unit case.
- `scripts/production/stall_watch.py` — host-neutral read.
- `scripts/production/test_stall_watch.py` — herdr unit case.
- `scripts/production/promote_showrunner.py` — host-neutral stop.
- `scripts/production/test_promote_showrunner.py` — herdr unit case.
- `scripts/production/add_unit.py` — `wait_for_remote_control` read.
- `scripts/production/test_add_unit.py` — follow the read change.
- `scripts/production/live_units.py` — place form.
- `scripts/production/test_live_units.py` — place form.
- `scripts/production/unit_status.sh` — reads through `session_pane.py`.
- `scripts/production/test_unit_status.py` — herdr unit case.
- `scripts/production/adopt.py` — read `place` (tmux only).
- `scripts/production/tmux_names.py` — follow the field removal (tmux only).
- `scripts/shutdown/settle.py` — herdr screen read.
- `scripts/shutdown/test_settle.py` — herdr screen case.
- `commands/showrunner/produce.md` — relay, compact, go-ahead, pane reads.
- `commands/showrunner/dailies.md` — dimmed suggestion read.
- `commands/showrunner/promote_unit.md` — stop step.

**Seats:** 2 writers (no test lane; `test` opens as impl); split the new command and its Python callers against status, settle and docs.
- `impl` — `session_pane.py`, `unit_lookup.py`, `unit_effort.py`, `stall_watch.py`, `promote_showrunner.py`, `add_unit.py`, `live_units.py`, `adopt.py`, `tmux_names.py` and their tests; hub: `scripts/production/session_pane.py`.
- `test` (opens as impl) — `unit_status.sh`, `test_unit_status.py`, `settle.py`, `test_settle.py`, `produce.md`, `dailies.md`, `promote_unit.md`.

**Constraints from prior phases:** `MarkedUnit.place: TmuxPlace | HerdrPlace` exists and `pane` is `""` for herdr; `unit_lookup.py list|pane` print `tmux:<pane>` or `herdr:<session>:<pane>`; `HerdrHost {kind, herdr_session, pane_id}` exists in `inventory.py`; `herdr.py` gives `read(pane, lines, joined=, ansi=)`, `prompt` (raises `AgentBlocked`), `send_keys`, `herdr_place_of`, `stop_session`.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Tests cover each verb on each host through the two fakes, exit 4 on `agent_blocked` with nothing else sent, and `unit_status.sh` output for one tmux and one herdr unit. `grep -n "tmux send-keys\|capture-pane" commands/showrunner/*.md` prints nothing. The live check in Spec 5 passes.

### Phase 5 — Shutdown and restart for herdr sessions  · status: todo

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

**Constraints from prior phases:** `HerdrHost {kind: "herdr", herdr_session, pane_id}`; `UnitHost.session_name` (old key `tmux_session` still parsed); `herdr.py` `start_server`, `open_pane`, `start_agent`, `stop_session`, `AgentNotReady`; `session_pane.py` reads and types on either host; `settle._screen` reads herdr hosts.

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

**Constraints from prior phases:** trust is checked in `preflight` and `add_showrunner.checked` through `herdr.claude_trust`; `session_pane.py` and `read_place` read either host; `MarkedUnit.place` replaced `pane`; `restart.py` has `_herdr_launch` and passes `--host-session`, which `add_unit.py` accepts beside `--tmux-session`; `herdr.py` `start_server`, `open_pane`, `start_agent`, `stop_session`, `live_sessions`.

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
