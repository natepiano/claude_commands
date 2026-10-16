# herdr adoption: every production and standalone Claude session in herdr

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready. Approved by the showrunner 2026-10-13, with its calls folded in.** Every showrunner, unit director and standalone Claude session on natedev runs in its own named herdr session, and the user opens any of them with `herdr session attach <name>`. Codex workers stay headless. tmux keeps working until the last Linux phase. The Mac comes last.

> **As-built disposition: create** — a new `docs/as-built/herdr-adoption.md`

> **Production: build-followups** — unit `herdr-trial-unit` (the showrunner may assign another); production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

The user, 2026-10-13 (verbatim): "okay - i'm good with herdr - i want to use it - i want to get all of our showrunner productions into it as well as standalones".

The showrunner (build-followups), passing on the production's direction: write the plan as a new plan doc in this production. Fix-before-adopting items come first: the roster and showrunners see herdr-hosted sessions, one herdr session per unit for attach, trust prompts, quota alerts. Each phase names what the user sees before and after it.

Evidence: `docs/as-built/herdr-trial.md` (findings 1–8, gotchas, reusable and trial-only parts).

**Calls I made (each a one-line revert unless it says otherwise):**

1. **Codex workers stay headless** (`codex app-server` threads via `scripts/agents/codex_mesh.py`). Only Claude sessions move into herdr. In the trial, interactive Codex in panes caused the lost Work Orders at the trust screen and the roster blind spot; headless workers ask for no trust and the roster already finds them. The roster lists Codex panes only when some exist (one `herdr agent list` per live herdr session), and no launcher creates them. Risk: a later wish for Codex status rows in herdr needs its own plan.
2. **One herdr session per showrunner, per unit and per standalone**, named by the session's launch name. `herdr session attach <name>` opens any of them (trial finding 6: an attached window keeps its own view, so one shared session cannot do this). `herdr_sessions.py list` names the live ones. Risk: many herdr servers, one per session, as there are many tmux sessions today.
3. **tmux keeps working through the transition.** `unit_lookup.py` gains a herdr source beside tmux, so running productions are not disturbed. Launchers switch to herdr only after lookup, typing, reading, shutdown and restart all handle herdr. tmux code goes in the last Linux phase, after every live session has moved. Risk: two hosts in the code for some weeks.
4. **Phase order.** The brief put launchers before typing and shutdown. I moved typing/reading and shutdown/restart ahead of the launcher switch, so no herdr unit ever exists that a relay, `/compact`, an effort change or a shutdown cannot reach. The finished result is the same. Risk: the user waits two more phases before the first herdr unit.
5. **Quota alerts: no change.** A Claude in a herdr pane is an ordinary session, so alerts reaching it are correct (trial finding 7). `waiting.py quota` finds herdr units through the lookup. Risk: none known.
6. **Trust is checked, never answered.** Every launcher refuses to start in a folder whose Claude trust is missing on the account that will run it, and prints the one command the user runs to grant it. The shared check is `herdr.claude_trust`, with `herdr.trust_command` for that command; `add_unit.py` calls both in `preflight`, `add_showrunner.py` in `checked`. Risk: a launch the user expected now refuses until they trust the folder once.
7. **Which trust keys count is settled.** Only exact keys count: the folder, its `git rev-parse --show-toplevel` and its main checkout. An ancestor folder never counts. herdr-trust Phase 1 settled it with a read-only survey of the account files. Risk: none known.
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
  - `scripts/herdr/herdr.py` — the library (Phase 1): `Herdr(session)` (`run` raises `HerdrFailure` on a non-zero exit or a 10 s timeout, `agent_blocked` as `AgentBlocked`; `panes`, `lookup`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until`, `read`, `send_keys`, `close_pane`, `agents`), module `live_sessions`, `server_unit`, `start_server`, `stop_session`, `open_pane`, `start_agent`, `herdr_place_of`, `herdr_processes`, `repository_trust_key` and `repository_root` (run git with `GIT_*` stripped), `claude_trust`. `herdr_sessions.py` — `list`, `attach`. The trial driver is deleted; `git show a35b0312:scripts/herdr/trial.py` has it (`trusted_by_codex` 853, `pane_environment` 978, `native_arguments` 990).
  - `scripts/herdr/fake_herdr.py` — records argv to `FAKE_HERDR_LOG` as JSON lines, answers from `FAKE_HERDR_RESPONSES` keyed by space-joined argv (else `"default"`); fields `stdout`, `code`, `stderr`, `write_files`, `require_paths`, `remove_paths`; a list replays in order.
  - `scripts/production/unit_lookup.py` — `MarkedUnit(unit, pane, label, claude)` 59, `tmux_binary` 126, `tmux_sessions` 145, `_marks` 160, `_claudes` 169 (joins a Claude to a pane through the session record's `tmux` field), `_marked_sessions` 191, `every_marked_unit` 207, `marked_units` 225, `mark` 237, CLI `list|pane|mark` 245. Marks are `SHOWRUNNER_UNIT` (production slug) and `SHOWRUNNER_UNIT_ID` (unit).
  - `scripts/message/sessions.py` — `SessionRecord` 17 (`tmux` is empty outside tmux; `pid`, `sessionId`, `name`, `messagingSocketPath`, `updatedAt`), `read_session`, `live_session`.
  - `scripts/production/add_unit.py` — `tmux_live` 575, `preflight` 600, `prompt_for` 747, `host_session_name` 790, `launch_session` 799 (`herdr.start_new_server` + `open_pane` with the marks and the account environment from `named_account_environment` or `accounts.prepared_account` on resume, then `start_agent` with `claude --model --effort [--resume] --remote-control <name> -n <name> <profile flags or --settings '{"disableAgentView": true}'>` and the opening prompt; `stop_session` on `AgentExited`), `launch_environment` 850, `launched_unit` 873, `wait_for_remote_control` 881 (reads the unit's place through `session_pane.read_place` for `/remote-control is active`), `main` 1068 (a marked session whose Claude is not running is ended by tmux `kill-session` 1168 or `herdr.stop_session` 1175; prints `tmux attach -t` or `herdr session attach` 1214, 1220). `--host-session` is the restore flag `restart.py` passes (shutdown bc61b417 replaced `--tmux-session`).
  - `scripts/production/add_showrunner.py` — `claude_json` 57, `checked` 62, `account_for_launch` 113, `launch` 148, `main` 190.
  - `scripts/production/promote_showrunner.py` — capture 86, stop via send-keys and kill-session 147–160, messages 220–230.
  - `scripts/production/unit_effort.py` — `_tmux` 80, `verdict` 142, `send` 157, capture 171.
  - `scripts/production/stall_watch.py` — `TMUX` 28, capture of the last 400 joined lines 211.
  - `scripts/production/unit_status.sh` — tmux binary 31–32, `pane_claude_pid` 111, per-unit capture and display-message 140–172.
  - `scripts/production/tmux_names.py` — renames tmux sessions to Claude names and reports name faults (`fault` 146); registered by `update_registration.py` 176; sender name in `scripts/hooks/conversation_pause.py` 29.
  - `scripts/production/adopt.py` — marks existing tmux sessions as units (55–132).
  - `scripts/production/live_units.py`, `waiting.py`, `production_lifecycle.py` — consume `unit_lookup` (`live_units.py` 98 prints `unit.session.pane`; `production_lifecycle.py` 482 prints `close tmux sessions`).
  - `scripts/production/fake_tmux.py` — tmux test double (sessions, panes and marks from a JSON file); `fake_tmux.sh` (bash + jq) reads the same JSON on hot test paths.
  - `scripts/message/roster.py` — `_every_marked_unit` 611, `_production_places` 619 (gates on a tmux binary; joins a socketless director by `record["tmux"]`), `_local_roster` 1087, `RosterEntry` 173, `Messageable`/`CannotReceiveMessages` 96–105.
  - `scripts/shutdown/session_types.py` — shared wire types, re-exported by `inventory.py` and `record.py`: `UnitHost` 21, `TmuxHost` 60, `HerdrHost` 68, `Host` 113, wire parsing 331–743 (`_parse_host` 459, `parse_session` 664).
  - `scripts/shutdown/inventory.py` — `_unit_sessions` 418, `_tmux_session` 565, `_host` 751 (reads `TMUX_PANE` from the process environment).
  - `scripts/shutdown/stop.py` — `run_tmux` 133, `_stop_unit_tmux` 332, callers 413–479.
  - `scripts/shutdown/record.py` — `UnitTmuxLeftRunning` 122, parsing 650–672.
  - `scripts/shutdown/restart.py` — unit argv with `--host-session` 587, tmux pane state 686–770, `_tmux_launch` 802, host dispatch 895–935.
  - `scripts/shutdown/settle.py` — `_screen` 377 (capture-pane), issue text 768.
  - `scripts/shutdown/shutdown.py` — host text 138.
  - `scripts/nightly_review/nightly_review.py` — `tmux` 73, `has-session` 81, attach and kill checks 124–129, spawn 150–160.
  - `scripts/accounts/accounts.py` — `AccountSelection` 202, `account_prefix` 1202, `trust_command(folder, selected)` 1209, `role_account`; `scripts/whoami/account.py` `named_account_environment(dir)` gives the account's variables as a dict.
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
  - **Edits to `/etc/nixos` are outside this repo.** A phase names them for the user; no worker makes them.
  - **File ownership.** Several files below sit in other units' Owns cells (`add_unit.py`, `showrunners.py`, `stall_watch.py`: enh-showrunner-unit; `scripts/message/`: shutdown-unit; `commands/showrunner/produce.md`: stalls-unit hub). The showrunner clears each before dispatch.
  - Python: basedpyright 0/0 on changed files; no file-level ignores; a `TypedDict` for every herdr JSON shape and record; no `Any`.

## Phases

### Phase 1 — The herdr library and session commands  · status: done

#### As-built

- `scripts/herdr/herdr.py` runs named herdr sessions and locates processes in them. `herdr_binary()` is `HERDR_BIN`, else `shutil.which("herdr")`, else `HerdrMissing`. `Herdr(session)` targets one session (`argv` is `[binary, "--session", session, *args]`) with `run`, `result`, `panes`, `lookup`, `pane_id`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until -> ObservedAgentState | WaitTimedOut`, `read(pane, lines, *, joined, ansi)`, `send_keys`, `close_pane`, `agents`. A non-zero exit raises `HerdrFailure` carrying herdr's `error.code`/`error.message`; `AgentBlocked` (from `prompt`, never retried) and `AgentNotReady(screen)` (from `start_agent`, no key sent) subclass it.
- Session functions: `live_sessions() -> list[SessionEntry]` (running only); `server_unit(name)` is `herdr-<name>` with characters outside `[A-Za-z0-9_.-]` replaced by `-`; `start_server(name, timeout_ms)` returns when `status server` prints `status: running`, else starts `systemd-run --user --unit=<server_unit> --collect <herdr> --session <name> server` and polls to the deadline; `stop_session(name)` runs `server stop`, then `systemctl --user stop <server_unit>` when still active. `SYSTEMD_RUN` and `SYSTEMCTL` name those programs.
- Module `open_pane(name, label, cwd, environment)` and `start_agent(name, pane, kind, argv, opening_prompt, timeout_ms)` mirror the `Herdr` methods; module `start_agent` uses the pane's label as the agent name.
- `herdr_place_of(pid) -> HerdrPlace | NotInHerdr` reads `HERDR_PANE_ID` from the process environment and walks ancestors under `HERDR_PROC` (default `/proc`) to the herdr server. `herdr_processes() -> list[HerdrProcess]` lists the user's processes holding `HERDR_PANE_ID`, each with `pid`, `place`, `marks` (`SHOWRUNNER_UNIT`, `SHOWRUNNER_UNIT_ID` only), `command`, `cwd`; unreadable or vanished processes are skipped.
- Trust lives here for every launcher: `claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile`, `trust_command(folder, account: ClaudeLaunchAccount)`, `repository_root(directory)` and `repository_trust_key(directory)` (herdr-trust Phase 1 replaced `trusted_by_claude`).
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
- Stop sends no tmux command and records no tmux issue for a herdr unit or host; restart makes a herdr unit or host a manual resume (prints the command, launches nothing, schedules no prompt); a tmux unit keeps its restore flag, now `--host-session`. Shutdown text: `herdr <session>`, and `unit <production>/<unit> (herdr <session>)` for a herdr unit.
- `pyrightconfig.json`: `scripts/herdr` in the production, message and shutdown environments; the copied-script fixtures in `test_unit_status.py` and `test_update_registration.py` copy `scripts/herdr`.
- The `test_waiting.py` quota case moved to Phase 5 (held by two-accounts Phase 14). Tests need no `HERDR_BIN` override on a machine with real herdr.

### Phase 3 — Trust checked before every launch  · status: moved

Moved to unit `herdr-trust-unit`, run in parallel with Phase 5; shipped as `docs/as-built/herdr-adoption.md` → "Claude trust before every launch". Phase 7 waits for it.

### Phase 4 — Typing into and reading sessions on either host  · status: moved

Moved to unit `herdr-panes-unit`, run in parallel with Phase 5; shipped as `docs/as-built/herdr-adoption.md` → "Typing into and reading sessions on either host". Phase 7 waits for it.

### Phase 5 — Shutdown and restart for herdr sessions  · status: done

#### As-built

- `scripts/shutdown/stop.py`: `_stop_unit_session` branches on `session_host`. A tmux unit goes through `_stop_unit_tmux`; a herdr unit's session is stopped with `herdr.stop_session(<session_name>)` after its Claude exits, and judged stopped only when `herdr.live_sessions()` no longer lists it. A session still running is the issue `unit session left running {host: "tmux" | "herdr", session_name, cause}`; `now --only` retries its cleanup.
- `scripts/shutdown/record.py`: `UnitSessionLeftRunning` holds that issue. The old kind `unit tmux session left running` with `tmux_session` (`UnitTmuxLeftRunning`) still parses and is read as `host: "tmux"`.
- `scripts/shutdown/settle.py`: issue text `<host> session <name> left running: <cause>`.
- `scripts/shutdown/restart.py`: `_resume_parts` returns `ResumeParts(cwd, environment, claude_arguments, opening_prompt, account)`, with `account` a `RetainResumeAccount(config_dir)` or `MoveResumeAccount(target)`; `claude_arguments` holds Claude's arguments only, never the `claude` binary or the prompt. `_resume_shell` renders the tmux and desktop shell string unchanged. `_herdr_launch(parts: ResumeParts, session: Session, *, dry_run: bool)` restarts a standalone herdr host: it checks trust with `herdr.trusted_by_claude`, starts the server, refuses a recorded pane that still holds a live agent, opens a pane with the account's config dir variables, and starts Claude with one opening prompt. It refuses a `MoveResumeAccount`. A herdr unit still restarts as a manual resume line (`ManualRestartRequired`); a unit restart passes `--host-session <session_name>` to `add_unit.py`.
- `scripts/production/add_unit.py`: `--host-session` (shutdown bc61b417 removed the `--tmux-session` alias).
- `commands/shutdown.md` names a herdr session left running; `docs/as-built/shutdown-and-restart.md` covers resume parts and the herdr resume.
- No herdr Claude integration is installed: resume uses the session ids in shutdown records and the agent-sessions snapshot.

**Files:**
- `scripts/shutdown/stop.py`, `record.py`, `settle.py`, `restart.py` — herdr stop, issue kind, issue text, herdr restart; `test_stop.py`, `test_record.py`, `test_settle.py`, `test_restart.py`, `test_shutdown_status.py` — their tests.
- `scripts/production/add_unit.py`, `test_add_unit.py` — `--host-session`.
- `commands/shutdown.md`, `docs/as-built/shutdown-and-restart.md`.

**Binds later work:** `_herdr_launch` checks trust through `herdr.trusted_by_claude`, which herdr-trust replaced; the follow-up phase moves it to `claude_trust` and `trust_command`. The herdr unit manual-restart guard stays until `add_unit.py` launches herdr ("Units and child showrunners launch in herdr" removes it). Every herdr launcher builds from structured parts like `ResumeParts`, never a shell string. The stop and restart live check (`herdr-stopcheck`) is not yet run; it runs after the nixos rebuild, in "Units and child showrunners launch in herdr".

**Gotchas:**
- `herdr agent start --kind claude` adds the binary itself and treats everything after `--` as Claude's arguments: never pass an executable or a wrapper there.
- A failed `herdr server stop` does not mean the session still runs; read `live_sessions()`.
- An account move cannot enter herdr: `accounts.py exec` removes credentials and checks readiness, which pane environment variables cannot do.

**Ruled out:** routing a herdr unit through `add_unit.py` before it can launch herdr — it would move the unit into tmux; installing herdr's Claude integration — it writes `~/.claude` hooks and settings that every session loads.

### Phase 6 — Phase 5 follow-ups: quota reaches herdr units, real environment on the Mac, restart trust and timing  · status: done

#### As-built

- `scripts/herdr/herdr.py`: `Herdr.start_agent(name, pane, kind, agent_arguments, opening_prompt, timeout_ms)` and module `start_agent(name, pane, kind, agent_arguments, opening_prompt, timeout_ms)` take the agent's extra arguments only. `herdr agent start --kind <kind>` adds the binary itself, so both refuse with `ValueError` an `agent_arguments` whose first element equals the kind (`_check_agent_arguments`), before any herdr call or pane lookup.
- `scripts/shutdown/inventory.py` `_process_environment` on Darwin reads the argv with `ps -o command= -p <pid>`, then `ps eww -o command= -p <pid>`, then the argv again. It parses only the `eww` text after the argv prefix, so argument text such as `TMUX_PANE=%41` in a prompt never counts as environment. A failed call, an argv that changed between the reads, an empty argv, `eww` output that does not start with the argv, or no whitespace after the prefix returns `{}`. Linux `/proc/<pid>/environ` is unchanged.
- `scripts/shutdown/restart.py` `_herdr_launch` checks trust only through `herdr.claude_trust(folder, claude_json)`: `Untrusted` and `UnreadableTrustFile` raise `OSError` before `start_server` and any herdr call, with the reason followed by `Run: <herdr.trust_command(folder, account)>`. The account is `DefaultAccount()` when `CLAUDE_CONFIG_DIR` is `~/.claude` (trust file `~/.claude.json`), else `NamedAccount(str(config_dir), config_dir)`. An ancestor folder's trust never counts. It passes `agent_arguments=list(parts.claude_arguments)` to `start_agent`.
- `scripts/production/waiting.py` is unchanged: `quota` reaches a herdr-marked unit with a live Claude record through `unit_lookup.marked_units` and `send.py --to <claude name>`.
- `test_restart.py` `test_six_tmux_launches_run_at_once`: the fake launches meet at one `threading.Barrier(6, timeout=10)`, with no wall-clock bound; a broken barrier fails the test.

**Files:**
- `scripts/herdr/herdr.py`, `test_herdr.py` — `agent_arguments`, binary refused; one `claude` in the launched command.
- `scripts/shutdown/inventory.py`, `test_inventory.py` — Darwin environment after the argv prefix.
- `scripts/shutdown/restart.py`, `test_restart.py` — trust through `claude_trust` and `trust_command`, unreadable-file and ancestor-folder cases; six launches overlap.
- `scripts/production/test_waiting.py` — `test_quota_reaches_a_live_herdr_unit_by_its_claude_name`.

**Binds later work:** every herdr launcher passes `start_agent` Claude's arguments only, never the `claude` binary ("Units and child showrunners launch in herdr"); "Standalone sessions and the nightly review in herdr" replaces `opening_prompt` with `OpeningPrompt | NoOpeningPrompt` on this signature. "herdr on the Mac" moves the Darwin after-the-argv environment parse into `herdr.py`, with `inventory._process_environment` calling it.

**Gotchas:**
- herdr adds the agent binary itself; an `agent_arguments` starting with it would reach Claude as a stray first prompt.
- A wall-clock bound on concurrent launches fails under machine load; prove overlap with a barrier.

### Phase 7 — Units and child showrunners launch in herdr  · status: done

#### As-built

- `add_unit.launch_session(request, director, prompt, selected, *, target="", model="")` starts every new or restored unit in its own herdr session: `herdr.start_new_server(name, timeout_ms)`, `open_pane(name, name, session_cwd, environment)` with both marks, `ENABLE_TOOL_SEARCH=true`, the account variables (`accounts.prepared_account(target, resume=True)` on a move, `named_account_environment` for a named account) and `PROBE_ENV` only for a `--probe` restore, then `start_agent(..., kind="claude", agent_arguments=[--model, --effort?, --resume?, --remote-control, -n, --settings...], opening_prompt=prompt)`. The caller's `CLAUDE_*` never reaches the pane. Restore of a marked unit with no live Claude ends a herdr place with `herdr.stop_session`, a tmux place with `kill-session`, and relaunches in herdr. Printed lines end `herdr session attach <name>`.
- `add_unit.account_for_launch` takes the units group's account through `accounts.account_for_group_launch("unit", ...)`; a herdr place stores no account or model of its own.
- `add_showrunner.py` launches the same way with no marks; `promote_showrunner.py` has no launch code and prints `herdr session attach` lines.
- `herdr.start_new_server` is exclusive: it raises `SessionTaken` when a server or systemd unit of that name already answers, and both launchers turn that into a refusal. `start_server` and `start_new_server` clear workspaces herdr restored on spawn (`_clear_restored_workspaces`), so an attach shows one workspace.
- `restart.py`: `_herdr_launch(parts: ResumeParts, destination: NewHerdrSession | RecordedHerdrPane, session, *, dry_run)`. A `HerdrHost` maps to `RecordedHerdrPane(herdr_session, pane_id)` (a pane left at its shell is closed and replaced; a live agent or running command is refused); a tmux showrunner restarts into `NewHerdrSession(<tmux name>)` with its record keeping `TmuxHost`; a live agent in the destination is refused before any pane opens. A `MoveResumeAccount` takes its environment from `accounts.prepared_account(target, resume=True)` (`AccountRefused` raises `OSError`), and trust is checked before `start_server`. Every unit, herdr or tmux, restarts through `add_unit.py --host-session <name>`.

**Files:**
- `scripts/herdr/herdr.py` — `start_new_server`, `SessionTaken`, `_clear_restored_workspaces`, `server_environment`, busy-pane retry in `start_agent`.
- `scripts/production/add_unit.py` — herdr launch and restore, `--host-session`, `--probe`.
- `scripts/production/add_showrunner.py` — herdr launch of child showrunners.
- `scripts/shutdown/restart.py` — destination types, tmux showrunner migration, unit restart via `add_unit.py`, account move into herdr.
- `commands/showrunner/*.md`, `commands/producer/to_production.md`, `docs/production_format.md` — herdr attach lines; the L1 rule now reads that `add_showrunner.py` opens each lower showrunner in its own herdr session.

**Binds later work:** `herdr.start_new_server` is exclusive and raises on a taken name. `restart.py` passes `--host-session` to `add_unit.py`, its only restore flag. `start_agent` retries `agent_pane_busy` to one shared deadline, never passing a per-try timeout at or below `_AGENT_START_TIMEOUT_MINIMUM_MS` (3000). `_herdr_launch` calls `herdr.server_environment(name)` after `start_server` and refuses when the server's inherited environment holds a must-be-unset variable (`CLAUDE_CONFIG_DIR`/`CLAUDE_CODE_OAUTH_TOKEN` for the default account, or a `PreparedAccount.remove` name). Restored workspaces are cleared on every server spawn. Destination types are `ResumeParts`, `NewHerdrSession`, `RecordedHerdrPane`. tmux launch paths survive only for restoring old tmux records.

**Gotchas:**
- A new pane's zsh takes about 2.3 s to start (direnv, gpg-connect-agent in the foreground); `agent start` before then fails `agent_pane_busy`. Retry on that error only.
- herdr rejects an agent-start timeout at or below 3000 ms.
- Each try's subprocess gets 10 s of grace past its timeout, so the shared deadline may overrun by up to 10 s.
- A unit restored onto an account other than the units group's account is refused; tests configure the group to match.
- natedev's `claude` alias already adds flags; launching through it duplicates them.

**Ruled out:** probing pane process info to detect shell readiness (herdr sends no distinguishing shape); killing an in-flight `agent start` at the deadline (leaves a half-started agent); copying the caller's environment into a restored pane (only named marks pass).

### Phase 8 — Standalone sessions in herdr  · status: done

#### As-built
- `herdr.py`: `start_agent(..., opening: OpeningPrompt | NoOpeningPrompt)` (frozen dataclasses; `NoOpeningPrompt` sends no prompt or keys); session names follow herdr's contract (`[A-Za-z0-9_.-]{1,64}`, never `.`/`..`), checked by `_validate_session_name`, and `server_unit(name)` is `herdr-<name>.service`; `generated_session_name(text, suffix="")` maps other characters to `-` and fits `-<suffix>` in 64 bytes; `attached_clients(name) -> ClientsAttached(count) | NoClientAttached | ClientsUnreadable(detail)` counts interactive `session attach` and TUI clients (argv `--session` first, then `HERDR_SESSION`); `open_pane` adds `HERDR_AGENT_PANE=1` to every pane.
- Discovery names a server's session by the listening socket it owns, never its argv; handoff survivors come from the dedicated unit cgroup, or in a shared terminal scope from server-reported shell pids (`_pane_shell_pids`, via `pane list` and `pane process-info`).
- `herdr_sessions.py start [--name N] [--cwd D] [-- args]` and `resume <name> --session-id --cwd [--config-dir] [--restore] [--prompt-file] [--attach] [-- args]`: trust check, `launch_barrier` plus `launch_permission` (`NewWorkLaunchPurpose`, or `RecordedSessionRestoreLaunchPurpose(session_id)` with `--restore`), exclusive `start_new_server` (a generated name retries the next suffix), must-be-unset check against `server_environment`, `start_agent` with `-n <name>`; `start` then execs `herdr session attach`.
- `restart.py`: a ghostty or zed host restarts into `NewHerdrSession(generated_session_name(<Claude name>))`; a live ghostty supervisor runs `herdr session attach <name>` in the same window, otherwise a new ghostty window opens. A herdr host with `presentation: "attached window"` gets a window only when `attached_clients` reports none; `"detached"` or unreadable prints the attach command. A failed window open appends to the record's `restart_issues` and leaves the agent launched.
- `shutdown.py`: `down`, `restart` and `status` run with no account; `restart` covers every account with a session record or an account hold on either machine, the other verbs every configured Claude login. `agents <verb>` on PATH is the terminal form.

**Files:**
- `scripts/herdr/herdr.py` — prompt types, name contract, `generated_session_name`, `attached_clients`, socket and handoff discovery, `HERDR_AGENT_PANE`.
- `scripts/herdr/herdr_sessions.py` — `start`, `resume`, admission.
- `scripts/shutdown/{launch_permission,shutdown,restart,inventory,record,settle}.py` — `RecordedSessionRestoreLaunchPurpose`, account-optional verbs, herdr restarts, `HerdrHost.presentation`, hold registration and ownership.
- `scripts/production/{unit_lookup,add_unit,add_showrunner}.py`, `commands/{shutdown,nightly_review}.md`, `docs/as-built/shutdown-and-restart.md` — pid placement fallback, `OpeningPrompt`, `agents`, attach lines.

**Binds later work:**
- `herdr.server_unit(name)` is `herdr-<name>.service`; any name outside the contract raises `ValueError` before a server starts or stops.
- Session identity comes from the listening socket; handoff survivors are found through server-reported shell pids and the dedicated unit cgroup, never by reading every process environment.
- `RecordedSessionRestoreLaunchPurpose` permits a restore only for a record in a restorable progress; with no record the account hold alone decides.
- Account holds carry `request_ids` and `registering_request_ids`; `begin()` registers each request before inserting records, nothing releases a hold while a registration is pending, and only the owning `request_id` releases it (joined requests protect it).
- Every pane carries `HERDR_AGENT_PANE=1`; callers pick `OpeningPrompt` or `NoOpeningPrompt` explicitly; restart names come from `generated_session_name` with no suffix retry; records carry `restart_issues`; restart reads `attached_clients` outcomes before opening any window.

**Gotchas:**
- systemd appends `.service` to a bare unit name, so the suffix is explicit or `a` and `a.service` share a unit; an attach-started server's argv says `default`, so only the socket names its session.
- Discovery skips a process whose cwd no longer resolves, so `unit_lookup` places a live registry pid with `herdr_place_of(pid)`; `HERDR_SESSION` selects a bare `herdr` client's session.

**Ruled out:**
- Matching pane ids from environments, because sessions sharing one scope can reuse the same ids.
- Treating membership in a hold's `request_ids` as permission to release it; opening a window on every attached herdr restart, since the existing client already shows the session.

### Phase 9 — One restore: agents restart after a crash  · status: done

#### As-built
- `inventory.snapshot_inventory() -> CompleteInventory | PartialInventory(failures)` reads every source once per pass, read-only, and reports each failure: the registry (`sessions.py` returns unreadable paths), account attribution, unit marks (`every_marked_unit(every_host=True)`, faults fail the pass), tmux and herdr hosts, the boot id. It never runs the vault snapshotter or `conversation_pause.release`.
- Shared wire types (`Session`, the hosts, `SnapshotClaudeSession`, `SnapshotCodexSession`, `RecoveryHostProcess`, `KnownBoot`, `CompleteInventory`) and host parsing live in `session_types.py`, re-exported by `inventory.py` and `record.py`.
- `record.RecoverySnapshot {machine, login, label, taken_at, boot_id?, sessions}`: one file per login at `<shutdown state>/snapshots/<login>.json`. Entries carry `state: "running" | "unresolved"`, `last_seen`, `boot_id`, `host_process`, `crash: "reboot" | "host gone"`. `shutdown.py snapshot` on a partial pass keeps every file, prints `snapshot incomplete: <sources>` and exits 1; `merge_snapshots` keeps a vanished session as `unresolved` only with a crash signal (boot id changed, or host gone) and drops it when its host is alive.
- `agents restart` (also `_up` and a peer's `up --here --json`) merges per session id: a shutdown record wins; a Claude entry confirmed by `RecoveryCrashCheck` (`CrashConfirmed | NoCrashSignal | CrashEvidenceUnreadable`) becomes a `SessionShutdown` through `materialize_snapshot` (`FromSnapshot {kind, taken_at}`, `request_id` `recovery-<taken_at>`, `progress: already gone`) and restarts as `RecordedSessionRestoreLaunchPurpose(<session id>)`; a Codex entry (`CodexLaunch {home, resume}`) restores straight into a new herdr session through `_launch_codex_snapshot` and stops that server if its start fails. An entry leaves only on a successful restart or `agents forget <name|id>`.
- Session ids are deduplicated across accounts before launch; a session running anywhere is skipped, conflicting account evidence refuses, and an incomplete record recovers only a positively dead owner. Lines name the source: `from shutdown record` or `from snapshot taken <time> (<age>)`. `--only` finds snapshot-only sessions; `status` lists unresolved entries with `last_seen`. Snapshots are machine-local.
- `herdr_sessions.py start` resolves its account with `accounts.account_for_unscoped_launch(os.environ)` (delivered by two-accounts).
- Spec 5, in `/etc/nixos` (the showrunner's, pending a rebuild): the timer runs `shutdown.py snapshot` as its own command, the vault snapshotter names `agents restart`, `agent-sessions-restore` goes, `agents` gains `forget`.

**Files:**
- `scripts/shutdown/{session_types,inventory,record,restart,shutdown}.py` — wire types, `snapshot_inventory`, snapshot store and materialization, restore, `snapshot`/`forget`/`status`.
- `scripts/message/sessions.py`, `scripts/herdr/herdr_sessions.py`, `commands/shutdown.md`, `docs/as-built/shutdown-and-restart.md` — unreadable registry paths, account resolution, docs.

**Binds later work:**
- `FromSnapshot` carries no crash signal or `last_seen`, and `CrashConfirmed` names no signal (crash restore wording follow-up).
- `merge_snapshots` returns `[]` for a partial pass; `all_snapshots` reads every `.json` under `snapshots/` as a login file (Snapshot outcome in agents status).
- After the accounts cutover a session on bare `~/.claude` is unattributed and makes `snapshot_inventory()` partial (Remove tmux from productions and sessions on Linux).

**Gotchas:**
- Losing the messaging socket does not mean the Claude exited; restore checks the saved pid and start time.
- Completed restore history (`record.restarted_history()`) is account evidence, so dropping the newest snapshot cannot revive an obsolete account copy.

**Ruled out:**
- A second parser of the vault's markdown; restoring an `agents down` session from the snapshot instead of its record.

### Phase 10 — Phase 9 follow-ups: crash restore wording, status heading, failed herdr start  · status: done

#### As-built
- `shutdown.py status` prints `_unresolved_snapshot_lines` inside the per-report loop only for `index == 0`, the local machine's section; recovery snapshots are machine-local, so a remote section never carries them.
- `record.FromSnapshot {kind, taken_at, recovery}` carries `RecoveryContext = RebootRecovery | LostHostRecovery | RecoveryEvidenceNotRecorded` (`kind` `reboot` / `host gone` with `last_seen`, or `not recorded`), normalized at parsing: an older record without `recovery` reads as `not recorded`. `restart.CrashConfirmed` carries `signal: Literal["reboot", "host gone"]`; `_up` writes the confirmed signal into the entry (`verified_entry`) before `materialize_snapshot`, so a retried launch reads the context from the record, never from the snapshot again.
- `restart_note` opens a snapshot-origin record with `Restored after a crash: the machine rebooted|its host was lost (last seen …, restored …)`, or the not-recorded form; the pending-message and keep-waiting text is unchanged. A crash-restored showrunner gets the note in its opening prompt, and `confirm_entry` sends no second note for a snapshot origin.
- `herdr.start_server` returns `ServerCreated | ServerAdopted`; `start_new_server` (exclusive) still returns `None`. `start_agent` raises `AgentExited(HerdrFailure)` as soon as the submitted agent process is gone. `_herdr_launch` stops only a server it created (a `NewHerdrSession` destination counts as `ServerCreated`); `add_unit.launch_session`, which always calls `start_new_server`, stops the session on `AgentExited`. Readiness refusals (`AgentNotReady`) behave as before.

**Files:**
- `scripts/shutdown/shutdown.py` — unresolved snapshot lines in the local section.
- `scripts/shutdown/record.py` — `RecoveryContext` in `FromSnapshot`, parsing and normalization.
- `scripts/shutdown/restart.py` — `CrashConfirmed.signal`, `verified_entry`, crash wording in `restart_note`, created-server cleanup in `_herdr_launch`.
- `scripts/herdr/herdr.py` — `ServerCreated`, `ServerAdopted`, `AgentExited`, agent exit detection (`_startup_processes`).
- `scripts/production/add_unit.py` — `stop_session` on `AgentExited` in `launch_session`.
- Tests beside each: `test_shutdown_cli.py`, `test_record.py`, `test_restart.py`, `test_herdr.py`, `test_add_unit.py`.

**Binds later work:**
- The recovery-snapshot outcome line in `agents status` goes beside the unresolved snapshot lines in the local section (Snapshot outcome in agents status).
- An exclusive new herdr session that a standalone tmux migration starts inherits `_herdr_launch`'s created-server cleanup on `AgentExited` through the `NewHerdrSession` branch (Standalone tmux sessions move to herdr).
- A `FromSnapshot` origin always has a `recovery` context; legacy-host snapshot entries that become `ManualRestartRequired` never reach `restart_note`.

**Gotchas:**
- herdr's `launch_pending` can stay true after the agent exits, and a name-based `/proc` watch reports a false exit on a healthy start because the Nix wrapper execs and forks. Exit detection follows the agent by pid plus start ticks across exec and reparenting, reads the shell's process tree directly, and falls back to herdr observations at most twice a second.

### Phase 11 — Snapshot outcome in agents status  · status: done

#### As-built
- `record.py` keeps one outcome file per machine at `<shutdown state>/snapshot_attempt.json`, written by `record_snapshot_attempt(outcome: SnapshotTaken | SnapshotFailed, machine: str)` with `_write_record` under its own `snapshot_attempt.lock`; the caller passes `settle.local_machine()`. Types: `SnapshotTaken {kind: "taken", at, accounts}`, `SnapshotFailed {kind: "failed", at, reasons}`, `LastSuccessfulSnapshot {kind: "taken", at}`, `NoSuccessfulSnapshotRecorded {kind: "not recorded"}`, `SnapshotAttempts {machine, latest, last_success}`, `NoSnapshotAttempt` (file absent). A failure keeps the previous `last_success`, or writes `not recorded` when there is no previous file. `read_snapshot_attempts() -> SnapshotAttempts | NoSnapshotAttempt` validates `at` through `_utc_time` and raises `InvalidRecord` on a bad file.
- `all_session_records` and `all_account_holds` skip root entries named by `_is_root_metadata`: `launch.lock`, `snapshot_attempt.json`, `snapshot_attempt.lock` and `_write_record`'s `.record.*.tmp` temporary files; any other non-directory root entry still raises `InvalidRecord`.
- `shutdown.py snapshot` (`_snapshot`) records every attempt: `SnapshotFailed` with the `PartialInventory` failures, or with the error text from `merge_snapshots` (`InvalidRecord`/`OSError`, then re-raised), else `SnapshotTaken` with the account count. Output and exit codes are unchanged; a failed outcome write prints `snapshot outcome not recorded: <error>` on stderr.
- `shutdown.py status` prints `_snapshot_outcome_lines` in the local machine's section only, before the unresolved snapshot lines, in both the Claude and Codex branches: `  recovery snapshot: last taken <user-zone time> (<age> ago)` or `  recovery snapshot: no successful snapshot recorded`, plus `  recovery snapshot failed <time>: <reasons>` when the latest attempt failed, or `  recovery snapshot: outcome unreadable: <error>` (never fails `status`). The local JSON report is always a `RecoveryStatusInventory` carrying `recovery` (possibly empty) and `snapshot: SnapshotStatus` = `SnapshotOutcomeRecorded {kind: "recorded", last_success, latest}` | `NoSnapshotOutcomeRecorded {kind: "no outcome recorded"}` | `SnapshotOutcomeUnreadable {kind: "unreadable", error}`; `inventory.parse_inventory` ignores both keys, so peer reports parse unchanged.
- `settle.elapsed_text(seconds) -> str` is the shared `N ago` formatter (`s`/`m`/`h`/`d` bands, negatives clamp to 0), on `ShutdownServices`; `restart.snapshot_source` and the status outcome line use it.
- Status only: no push, no alert, no timer change.

**Files:**
- `scripts/shutdown/record.py` — outcome types, `record_snapshot_attempt`, `read_snapshot_attempts`, `_is_root_metadata`.
- `scripts/shutdown/shutdown.py` — outcome recording in `_snapshot`; `SnapshotStatus` and outcome lines in `status`.
- `scripts/shutdown/settle.py` — `elapsed_text`.
- `scripts/shutdown/restart.py` — `ShutdownServices.elapsed_text`; `snapshot_source` uses it.
- Tests: `test_record.py`, `test_shutdown_cli.py`, `test_shutdown_status.py`.

**Binds later work:**
- `status` tests for legacy tmux states expect the `recovery snapshot:` line in the local section and the always-present `recovery` and `snapshot` keys in the local JSON report (Remove tmux from productions and sessions on Linux).

**Gotchas:**
- `_write_record` stages its temporary file in the state root itself; a new root file needs `_is_root_metadata`, or both account enumerators raise `InvalidRecord`.
- An absent outcome file or `not recorded` never means no snapshot exists: older snapshots predate the store, and a snapshot can succeed while its outcome write fails.

**Ruled out:**
- A phone push or alert for a failed snapshot; the outcome file under `snapshots/` (`all_snapshots` reads every `.json` there as a login's snapshot).

### Phase 12 — Standalone tmux sessions move to herdr; the tmux-free check  · status: done

**Depends on:** Phase 7, Phase 8, Phase 9, Phase 10 (Spec 1); Spec 2 also on Phase 11, which edits the same `shutdown.py`

**User sees:** before — `/shutdown --only <name> --restart` brings a tmux standalone back in tmux, and nothing says whether any session still needs tmux. After — the same restart brings it back in its own herdr session with its conversation, and `shutdown.py tmux-free` lists every session, source or tmux leftover that still stands in the way of removing tmux.

#### Work Order

**Goal:** every kind of tmux session can be moved into herdr by an ordinary restart, and one read-only command proves when none is left; tmux parsing and observation stay as they are.

**Spec:**

1. Standalone tmux migration: today `_launch_session` (`restart.py` 1264) moves only a tmux showrunner into herdr; any other non-unit `TmuxHost` session respawns in tmux through `_tmux_launch` (1083). A non-unit `TmuxHost` session restarts through `_herdr_launch` (1158) into an exclusive new herdr session (`NewHerdrSession`, `start_new_server`; the name from `generated_session_name(<tmux session name>, ...)`, a taken name retried with the next suffix), with its conversation, account and model from `ResumeParts`; a start whose agent exits (`herdr.AgentExited`) stops that new server, as `_herdr_launch` already does for a created server. The record keeps its original `TmuxHost`. `stop._stop_unit_session` (`stop.py` 371) returns at once for non-units, so the old tmux session and its shell stay registered after the move; the owning showrunner closes it by hand (the migration record below). Tests cover a standalone with a named account and a recorded model, two Claudes from one tmux session, a taken name, and an agent exit that stops the new server.
2. Removal gate: `shutdown.py tmux-free` (a subcommand beside `snapshot`, parser at 769) exits 0 only when each observation is positively readable and empty: `tmux list-sessions` exits 0 with no rows, or reports no server running; `herdr.live_sessions()` answers; one read-only `inventory.snapshot_inventory()` call (Phase 9) is a `CompleteInventory` — a `PartialInventory` (any unreadable source, or a process it cannot attribute, such as a Claude on a bare `~/.claude` after the accounts cutover) fails — and no Claude or Codex entry in it has a tmux unit host, a `TmuxHost` or an `UnknownHost`. Any failure exits 1 and prints each cause. Tests cover a config-only home session (partial), an attributed session on an unknown host (complete, but refused), an unreadable tmux, and a still-registered tmux session holding only a shell after its Claude moved (refused). `unit_lookup.every_marked_unit` (289) alone is no proof: without `every_host=True` it returns partial results when one host answers.

3. Restart exit status (showrunner, 2026-10-16: current, a wrong exit status is a wrong result): `shutdown.py restart` exits nonzero when any attempted restore failed, exit 0 only when all succeeded or none was needed; printed output unchanged. Test: one failure among successes exits nonzero.

**Decided by the showrunner, 2026-10-13: move the running tmux sessions at their next idle point, showrunners first.** Once this phase merges and is installed, the showrunner tells each owning showrunner before its sessions move. Each session moves at its next idle point (between phases for a unit) with `/shutdown --only <name> --restart`: units through `add_unit.py`, showrunners and standalones through `_herdr_launch`. After every Claude that shared a tmux session is confirmed running in herdr, the owning showrunner checks that the old tmux session holds no remaining work and closes that exact session (`tmux kill-session -t =<name>`), never by pattern. Each restart interrupts that session for about a minute. Phase 13 starts only when `tmux-free` exits 0 on natedev. Moving at idle points rather than waiting for productions to end keeps tmux removal from waiting on the longest production (for example screenshot-unit).

**Files:**
- `scripts/shutdown/restart.py` — non-unit `TmuxHost` restarts into a new herdr session; `_tmux_launch`, `_new_window_argv` and the tmux pane helpers removed.
- `scripts/shutdown/test_restart.py` — standalone tmux migration.
- `scripts/shutdown/shutdown.py` — `tmux-free`.
- `scripts/shutdown/test_shutdown_cli.py` — `tmux-free` refuses a partial inventory, an unknown host, an unreadable tmux and a shell-only tmux session; passes when every observation is empty.
- `scripts/production/unit_lookup.py`, `test_unit_lookup.py` — `installed_tmux_binary()` resolves tmux for observation without building it; session listing and mark reads use it, and a missing tmux is unreadable rather than built.
- `scripts/shutdown/inventory.py`, `test_inventory.py` — `_tmux_session` uses `installed_tmux_binary()`.

**Workers:** 2 writers (no test lane; `test` opens as impl); the two specs share no file.
- `test` (opens as impl) — `restart.py`, `test_restart.py`; hub: `scripts/shutdown/restart.py` (Spec 1). Starts beside Phase 11.
- `impl` — `shutdown.py`, `test_shutdown_cli.py` (Spec 2). Starts once Phase 11's writer is done with `shutdown.py`.

**Constraints from prior phases:** `restart.py` moves tmux showrunners and ghostty and zed standalones into herdr through `_herdr_launch`; `herdr.generated_session_name` and the exclusive `start_new_server` exist (Phase 8). `_herdr_launch` marks a `NewHerdrSession` destination `ServerCreated` and stops that server when `start_agent` raises `herdr.AgentExited`; `herdr.start_server` returns `ServerCreated | ServerAdopted`, while the exclusive `start_new_server` returns `None`, so its successful caller owns the created server (Phase 10). Shared wire types (`Session`, `UnitHost` 21, `TmuxHost` 60, `HerdrHost`, `UnknownHost`, the snapshot entry types) and host parsing (`_parse_host` 459) live in `scripts/shutdown/session_types.py`, re-exported by `inventory.py` and `record.py` (Phase 9). `inventory.snapshot_inventory() -> CompleteInventory | PartialInventory` reads every source once, read-only, including tmux hosts (`_snapshot_tmux_hosts` 1183); account attribution goes through `account.home_account_state()` (`LegacyHomeLogin | ConfigOnlyHome`), so after the cutover a Claude on bare `~/.claude` is unattributed and makes the inventory partial (two-accounts Phase 31, merged 2d57e9ee). `shutdown.py status` shows the recovery snapshot outcome in the local machine's section (Phase 11). This phase removes `_tmux_launch`, `_new_window_argv` and the tmux pane and respawn helpers from `restart.py`: a unit on any host restarts through `_unit_argv` (`add_unit.py`, herdr only), and a non-unit tmux session through `_herdr_launch`; tmux parsing and observation stay until Phase 13.

**Acceptance gate:** `merge_gate.py --tests-only` green and `basedpyright scripts/shutdown` prints `0 errors, 0 warnings`. Live: run `shutdown.py tmux-free` on natedev and quote its output in the phase report; it exits 1 and lists each session still in tmux while any remain.

### Phase 13 — Remove tmux from productions and sessions on Linux  · status: todo

**Depends on:** Phase 12

**Blocked by:** `shutdown.py tmux-free` (Phase 12), run from the installed tree, exits 0 on natedev: tmux answers and lists no session, every herdr source answers, and every account's inventory is complete with no session on a tmux or unknown host.

**User sees:** before — every tool carries two hosts; after — every showrunner, unit, standalone and the nightly review runs in herdr, `tmux` appears in no production command, and the docs name only herdr.

#### Work Order

**Goal:** the tmux code paths, tests, jobs and docs are removed, leaving herdr as the only session host on Linux; old records and snapshots that name tmux become manual-recovery states.

**Spec:**

1. Legacy records, built first: `UnitHost` records whose `session_host` is absent or `"tmux"` (`session_types._parse_host` 459 accepts both) parse into a legacy unit host that keeps the production and unit identity, accepted by `UnitSession` validation, and restart dispatches it to manual recovery before the ordinary unit launch; `TmuxHost` hosts, in shutdown records and in Phase 9's recovery snapshots, parse into `LegacyTmuxHost {kind: "legacy tmux", session_name}`. Tests cover both unit wire forms in records and snapshots. A Codex snapshot entry (`SnapshotCodexSession`, restored by `_up` straight through `_launch_codex_snapshot`, `restart.py` 2010, never as a record) with a legacy tmux host prints a manual Codex resume command from its recorded `CODEX_HOME` and session id or picker, starts no herdr server and stays unacknowledged, tested apart from Claude records; `UnitSessionLeftRunning(host="tmux")` and the old `unit tmux session left running` kind parse into `LegacyTmuxSessionLeftRunning {session_name, cause}`. Neither name is ever used as a herdr destination. `status` and `now` show both with the manual command (`tmux kill-session -t =<name>` for a leftover, the resume command for a host). Restart turns a `LegacyTmuxHost` entry into `ManualRestartRequired` with its resume command; a leftover whose cause is `stop not confirmed` stays in the record as unresolved cleanup (`_unfinished_cleanup`, `restart.py` 1878, reports it and does not clear it), so the entry becomes a manual restart naming both commands.
2. Remove: `unit_lookup.py` tmux source (`tmux_binary`, `_tmux`, `tmux_sessions`, `_marks`, `mark`, the CLI `mark`, `TmuxPlace`); `tmux_names.py` and its test, its registration in `update_registration.py` and its sender name in `conversation_pause.py`; `adopt.py` and its test; `fake_tmux.py` and `fake_tmux.sh` (the shell fake over the same JSON state, used by `test_waiting.py` and `test_dailies_input.py`); tmux branches in `session_pane.py`, `add_unit.py` (`tmux_live`, `kill-session`), `add_showrunner.py`, `promote_showrunner.py`, `unit_effort.py`, `stall_watch.py`, `settle.py`, `stop.py`, `restart.py` (Phase 12's standalone migration branch, the `TmuxHost` restart; `_tmux_launch` and the tmux pane helpers are already gone), `session_types.py` (`TmuxHost` and its types; the tmux `session_host`), `inventory.py` (tmux observation, `_snapshot_tmux_hosts`, re-exports), `record.py`, `shutdown.py` (with Phase 12's `tmux-free`), `roster.py`; tmux wording in `production_lifecycle.py` (499, `close herdr or tmux sessions`), `rename.py` (762, `rewrites the tmux mark`) and `commands/producer/to_production.md` (107); and every test that builds a `TmuxPlace` or loads the fakes (Files).
3. A `notifier.sh remove tmux-names` for each production registered with it is printed for the showrunners in the phase report; nothing removes another production's instance from a worker.
4. Docs: remove every remaining tmux line from `commands/showrunner/*.md`, `commands/shutdown.md`, `commands/producer/to_production.md`, `docs/production_format.md`; update as-builts `showrunner-automation`, `session-roster`, `shutdown-and-restart`, `session-notifier`, `agent-registry`, `build-followups-fn-length-hook` where they describe tmux hosts.
5. **Outside this repo (the showrunner's, in `/etc/nixos`; no worker makes these):** `nightly-review.nix` drops tmux from its packages; `agent-sessions-snapshot.py` drops its remaining tmux branches. Before that, the showrunner confirms Phase 9 Spec 5 landed and was rebuilt: the timer runs `shutdown.py snapshot` as its own command and `agent-sessions-restore` is gone (at 2d57e9ee `agent-sessions.nix` 50–64 still installs it, and the timer runs only the vault snapshotter). The tmux package stays installed and `tmux: server` stays in earlyoom's `--avoid` (the showrunner's call): this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand.

**Files:**
- `scripts/production/unit_lookup.py` — tmux source removed.
- `scripts/production/test_unit_lookup.py` — tmux cases removed.
- `scripts/production/tmux_names.py` — deleted.
- `scripts/production/test_tmux_names.py` — deleted.
- `scripts/production/adopt.py` — deleted.
- `scripts/production/test_adopt.py` — deleted.
- `scripts/production/fake_tmux.py` — deleted.
- `scripts/production/fake_tmux.sh` — deleted.
- `scripts/production/update_registration.py` — no tmux-names job.
- `scripts/production/test_update_registration.py` — follows.
- `scripts/hooks/conversation_pause.py` — sender list.
- `scripts/production/session_pane.py` — herdr only.
- `scripts/production/add_unit.py` — herdr only.
- `scripts/production/add_showrunner.py` — herdr only.
- `scripts/production/promote_showrunner.py` — herdr only.
- `scripts/production/unit_effort.py` — herdr only.
- `scripts/production/stall_watch.py` — herdr only.
- `scripts/production/production_lifecycle.py` — close-out line names only herdr sessions.
- `scripts/production/rename.py` — no tmux mark wording.
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
- `scripts/production/test_production_tree.py` — no `TmuxPlace`.
- `scripts/production/test_units_table.py` — no `TmuxPlace`.
- `scripts/production/test_rename.py` — no `fake_tmux.py`.
- `scripts/production/test_dailies_input.py` — no `fake_tmux.sh`.
- `scripts/delegate/test_phase_table.py` — no `fake_tmux`.
- `scripts/shutdown/session_types.py` — `TmuxHost` and the tmux `session_host` removed; legacy unit and tmux hosts normalized at parsing; `Session` unions accept them.
- `scripts/shutdown/inventory.py` — tmux observation removed; re-exports follow.
- `scripts/shutdown/stop.py` — herdr only.
- `scripts/shutdown/record.py` — legacy hosts and leftovers parsed into manual-recovery states.
- `scripts/shutdown/restart.py` — the standalone migration branch removed; legacy entries become manual restarts.
- `scripts/shutdown/settle.py` — herdr only.
- `scripts/shutdown/shutdown.py` — `tmux-free` removed; host text; legacy states in `status` and `now`.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/test_stop.py` — follows.
- `scripts/shutdown/test_record.py` — legacy parsing.
- `scripts/shutdown/test_restart.py` — legacy restart; migration cases and the `tmux_host` / `recorded_tmux_host` helpers removed.
- `scripts/shutdown/test_account_move.py` — fixtures on herdr hosts; no `tmux_host` import.
- `scripts/shutdown/test_settle.py` — follows.
- `scripts/shutdown/test_shutdown_cli.py` — `tmux-free` tests removed.
- `scripts/shutdown/test_shutdown_status.py` — legacy states in `status` and `now`.
- `scripts/shutdown/test_remote_control.py` — follows.
- `scripts/message/roster.py` — herdr only.
- `scripts/message/test_roster.py` — follows.
- `scripts/message/test_top_level.py` — follows.
- `commands/showrunner/produce.md` — no tmux.
- `commands/showrunner/dailies.md` — no tmux.
- `commands/showrunner/effort.md` — no tmux.
- `commands/producer/to_production.md` — unit sessions named for herdr only.
- `commands/shutdown.md` — no tmux.
- `docs/production_format.md` — no tmux.
- `docs/as-built/showrunner-automation.md` — herdr hosts.
- `docs/as-built/session-roster.md` — herdr hosts.
- `docs/as-built/shutdown-and-restart.md` — herdr hosts.
- `docs/as-built/session-notifier.md` — no tmux-names job.
- `docs/as-built/agent-registry.md` — herdr hosts.
- `docs/as-built/build-followups-fn-length-hook.md` — herdr hosts.

**Workers:** 2 writers (no test lane; `test` opens as impl); split `scripts/production` and `scripts/hooks` against `scripts/shutdown`, `scripts/message`, the commands and the docs.
- `impl` — every `scripts/production/` and `scripts/hooks/` file above (`production_lifecycle.py` and `rename.py` included), and `scripts/delegate/test_phase_table.py`; hub: `scripts/production/unit_lookup.py`.
- `test` (opens as impl) — every `scripts/shutdown/` and `scripts/message/` file (`test_account_move.py` included), the commands (`commands/producer/to_production.md` included) and the docs; Spec 1 first; hub: `scripts/shutdown/session_types.py` (the host types and legacy normalization every shutdown file reads).

**Constraints from prior phases:** every launcher starts in herdr; `session_pane.py`, `stop.py`, `restart.py` and `settle.py` handle `HerdrHost`; `restart.py` moves every non-unit tmux session and ghostty and zed standalones into herdr; `herdr_sessions.py` has `list`, `attach`, `start`, `resume`; `herdr.generated_session_name` and the exclusive `start_new_server` exist (Phase 8). Shared wire types (`Session`, `UnitHost` 21, `TmuxHost` 60, `HerdrHost`, `UnknownHost`, `SessionHost`, the snapshot entry types) and host parsing (`_parse_host` 459) live in `scripts/shutdown/session_types.py`, re-exported by `inventory.py` and `record.py` (Phase 9). `UnitHost.session_host` is absent in old records and read as `"tmux"`, and an explicit `"tmux"` is accepted too; `record.py` normalizes the old `unit tmux session left running` kind to `UnitSessionLeftRunning(host="tmux")` (types 162–184, normalization 785–806); `restart._unfinished_cleanup` (1878) holds an entry whose leftover cause is `stop not confirmed`. Recovery snapshots hold the same `Session` hosts as records; Codex entries (`SnapshotCodexSession`, `TopLevelSession` host) restore through `_launch_codex_snapshot` (2010), not `_launch_session` (Phase 9). `inventory.snapshot_inventory() -> CompleteInventory | PartialInventory` reads every source once, read-only, including tmux hosts (`_snapshot_tmux_hosts` 1183); account attribution goes through `account.home_account_state()` (`LegacyHomeLogin | ConfigOnlyHome`), so after the cutover a Claude on bare `~/.claude` is unattributed and makes the inventory partial (two-accounts Phase 31, merged 2d57e9ee). `agents restart` restores from shutdown records and recovery snapshots (Phase 9); `agent-sessions-restore` stays installed until the showrunner lands Phase 9 Spec 5, and the vault snapshotter in `/etc/nixos` carries both tmux and herdr branches until Spec 7. Phase 12 shipped `shutdown.py tmux-free` and the standalone migration in `restart._launch_session` (1062, the `host["kind"] == "tmux"` branch), and removed `_tmux_launch`, `_new_window_argv` and the tmux pane helpers: units restart only through `_unit_argv` (`add_unit.py`); every live session has moved and `tmux-free` exited 0, so the removal and the legacy parsing land together with no tmux session left to restart. `add_unit.launch_session` (799) starts only herdr, through the exclusive `herdr.start_new_server`, and stops that session when `start_agent` raises `herdr.AgentExited`; its remaining tmux code is `tmux_live` (575) and the restore `kill-session` (1168). `herdr.start_server` returns `ServerCreated | ServerAdopted`; the exclusive `start_new_server` returns `None`, so its successful caller owns the created server (Phase 10). `trust_command` lives in `scripts/accounts/accounts.py` (1209); `herdr.claude_trust` stays in `herdr.py`. `scripts/shutdown/test_account_move.py` (34) imports `tmux_host` from `test_restart`, so removing that helper breaks it unless its fixtures move to herdr hosts. A `FromSnapshot` origin always carries a `recovery` context (Phase 10); a legacy-host snapshot entry that becomes `ManualRestartRequired` never reaches `restart_note`. `shutdown.py status` prints `recovery snapshot: …` lines (`_snapshot_outcome_lines`) in the local section before the unresolved snapshot lines, and the local JSON report is always a `RecoveryStatusInventory` with `recovery` and `snapshot`, so legacy-state `status` tests expect both; `settle.elapsed_text(seconds)` (on `ShutdownServices`) formats every `N ago`, and `record.record_snapshot_attempt(outcome, machine)` writes `snapshot_attempt.json` in the state root, which `record._is_root_metadata` skips with `launch.lock` and `.record.*.tmp` (Phase 11).

**Acceptance gate:** `merge_gate.py --tests-only` green (it covers `scripts/delegate`, `scripts/production`, `scripts/shutdown`, `scripts/message`, `scripts/hooks`, `scripts/nightly_review` and `scripts/herdr`); `basedpyright scripts/production scripts/shutdown scripts/message scripts/hooks scripts/nightly_review scripts/herdr` prints `0 errors, 0 warnings`; `grep -rln tmux scripts/production scripts/shutdown scripts/message commands/showrunner commands/producer` names only lines kept on purpose (legacy-record parsing and its manual commands), each listed in the phase report. Tests cover legacy hosts and leftovers in records and snapshots through parsing, `status`, `now` and restart, never as herdr names and never cleared silently. The phase report quotes `tmux-free`'s passing output, run from the installed tree before dispatch.

### Phase 14 — herdr on the Mac  · status: moved

Moved to its own unit (showrunner, 2026-10-15): `docs/plans/build-followups-herdr-mac.md`. It starts after Phase 13 merges.
