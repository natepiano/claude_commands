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
  - `scripts/herdr/herdr.py` — the library (Phase 1): `Herdr(session)` (`run` raises `HerdrFailure` on a non-zero exit or a 10 s timeout, `agent_blocked` as `AgentBlocked`; `panes`, `lookup`, `open_pane`, `start_agent`, `readiness`, `prompt`, `wait_until`, `read`, `send_keys`, `close_pane`, `agents`), module `live_sessions`, `server_unit`, `start_server`, `stop_session`, `open_pane`, `start_agent`, `herdr_place_of`, `herdr_processes`, `repository_trust_key` and `repository_root` (run git with `GIT_*` stripped), `claude_trust`, `trust_command`. `herdr_sessions.py` — `list`, `attach`. The trial driver is deleted; `git show a35b0312:scripts/herdr/trial.py` has it (`trusted_by_codex` 853, `pane_environment` 978, `native_arguments` 990).
  - `scripts/herdr/fake_herdr.py` — records argv to `FAKE_HERDR_LOG` as JSON lines, answers from `FAKE_HERDR_RESPONSES` keyed by space-joined argv (else `"default"`); fields `stdout`, `code`, `stderr`, `write_files`, `require_paths`, `remove_paths`; a list replays in order.
  - `scripts/production/unit_lookup.py` — `MarkedUnit(unit, pane, label, claude)` 59, `tmux_binary` 126, `tmux_sessions` 145, `_marks` 160, `_claudes` 169 (joins a Claude to a pane through the session record's `tmux` field), `_marked_sessions` 191, `every_marked_unit` 207, `marked_units` 225, `mark` 237, CLI `list|pane|mark` 245. Marks are `SHOWRUNNER_UNIT` (production slug) and `SHOWRUNNER_UNIT_ID` (unit).
  - `scripts/message/sessions.py` — `SessionRecord` 17 (`tmux` is empty outside tmux; `pid`, `sessionId`, `name`, `messagingSocketPath`, `updatedAt`), `read_session`, `live_session`.
  - `scripts/production/add_unit.py` — `tmux_live` 507, `preflight` 532, `prompt_for` 665, `launch_session` 701 (systemd-run scope + `tmux new-session -d -e` marks, `claude --model --effort [--resume] --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>` with `account_prefix(selected)`), `launch_environment` 739, `launched_unit` 762, `wait_for_remote_control` 770 (polls `capture-pane` for `/remote-control is active`), `main` 800 (restore and kill-session 869–899, prints `tmux attach -t` 924, 929). `--host-session` is the restore flag `restart.py` passes (shutdown bc61b417 replaced `--tmux-session`).
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
  - `scripts/shutdown/inventory.py` — `UnitHost` 40, `TmuxHost` 79, `Host` 113, wire parsing 359–460, `_unit_sessions` 820, `_tmux_session` 940, `_host` 1100 (reads `TMUX_PANE` from the process environment).
  - `scripts/shutdown/stop.py` — `run_tmux` 133, `_stop_unit_tmux` 332, callers 413–479.
  - `scripts/shutdown/record.py` — `UnitTmuxLeftRunning` 122, parsing 650–672.
  - `scripts/shutdown/restart.py` — unit argv with `--host-session` 587, tmux pane state 686–770, `_tmux_launch` 802, host dispatch 895–935.
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

**Depends on:** Phase 8

**User sees:** before — after a crash or reboot the user runs `agent-sessions-restore`, which reads the vault's markdown; after — `agents restart` brings back every session that was running, from its shutdown record or the latest recovery snapshot, naming the source of each, and `agent-sessions-restore` is gone.

#### Work Order

**Goal:** a timer keeps a recovery snapshot per account in shutdown's own state, and `agents restart` restores crashed sessions through the shutdown restart path.

**One restore command (showrunner, 2026-10-14; the user's words are in Phase 8).** `agents restart` is the one command that brings sessions back after a shutdown, a crash or a reboot. A timer takes the inventory a shutdown takes and keeps it per account; restore turns its sessions into shutdown records and runs the restart path a record runs. No second parser of the vault's markdown is written.

- Codex standalones are in scope (showrunner, 2026-10-15): shutdown's inventory and the recovery snapshot include Codex sessions run by hand, `agents restart` resumes them, and `agent-sessions-restore.py` goes once this phase's live check passes.

**Spec:**

1. Complete observations (`inventory.py`). `snapshot_inventory() -> CompleteInventory(sessions by login) | PartialInventory(failures)`; every source reports success or failure explicitly:
   - the session registry: `sessions._readable_records` (`scripts/message/sessions.py` 152) skips an unreadable directory or record silently; `sessions.py` gains a read that returns unreadable paths beside the records;
   - account attribution of each process;
   - production registrations and unit marks: `inventory._unit_sessions` (946) swallows `OSError` and drops faulted productions, so a unit could be recorded as a standalone; the snapshot path calls `unit_lookup.every_marked_unit(every_host=True)` and treats its `faults` as failures;
   - both host lookups (tmux and herdr).
   Shared observations are collected once per pass for every account. The desktop map comes from the existing vault snapshot (`_snapshot_desktops(refresh=False)`, 1234): one pass never runs `agent-sessions-snapshot`, whose run refreshes account notes, prunes and commits. Timer enablement and pause and footer restoration intent are read without `conversation_pause.release` (`settle._pause_timers` 364 releases) and without stopping a timer. A partial observation never replaces recovery data, and unreadable liveness never means a session is absent.
2. Recovery snapshot (`record.py`): `RecoverySnapshot {machine, login (canonical), taken_at, sessions: list[SnapshotSession]}`, one file per login at `<shutdown state>/snapshots/<login>.json` (temporary file, then rename; `_safe_login` applies). `SnapshotSession` holds the owner `Session`, its workers, its timer restores, `last_seen` and `state: "running" | "unresolved"`.
   - `shutdown.py snapshot`: a `PartialInventory` keeps every previous file and exits 1 naming the sources. A complete inventory rewrites each login's file: sessions seen now are `running`; a previous session not seen now becomes `unresolved` with its `last_seen` kept. A readable empty inventory never drops a populated snapshot's sessions: they stay `unresolved`, across reboot (the state directory is on disk).
   - **Crash signal (showrunner, option 1, 2026-10-15).** Only a session that vanished with a crash signal is restored: the boot id changed, or its host is gone (the herdr or tmux server, or the terminal window process). A session whose host is still alive ended on its own and is dropped. Sessions stopped by `agents down` come back through shutdown's own record, never through the snapshot; the two paths stay separate. Tests cover reboot, host loss and a clean exit.
   - A session leaves the snapshot only when acknowledged: `agents restart` restored it (Spec 3), or `agents forget <name|id>` (`shutdown.py forget`) retired it. `agents status` lists unresolved sessions with their `last_seen` in the user zone.
   - Running `snapshot` changes nothing in any session.
3. `shutdown.py restart` and internal `up` (`restart._up`, 1855) choose sources per session id:
   - An existing shutdown record takes precedence for its session. Eligible snapshot sessions with no record merge in. One stopped session never hides another crashed session on the same account.
   - Materialize before launch: each eligible absent snapshot session is written atomically as a persisted `SessionShutdown` (`record.py`), never through `update_session` (1286), which edits existing records only. Mapping: `login`, `label`, `machine` from the snapshot; `session`, `workers`, `timers` from the `SnapshotSession`; `requested_at` = `taken_at`; `request_id` = `recovery-<taken_at>`; `requested_by` a snapshot origin; `hold: "session"` (no account hold is written); `then` restart; `progress: already gone` (the kind shutdown gives an owner found gone at stop time, already restartable in `RESTARTABLE_PROGRESS` and in `launch_blocked`); empty `trail` and `stop_issues`. Its snapshot entry is removed after the restart succeeds; a failed restart keeps both.
   - Admission passes `RecordedSessionRestoreLaunchPurpose(<session id>)` (Phase 8), and the materialized record is what permits it; the absence of a record or hold is never taken as authorization.
   - An incomplete shutdown (a record in `waiting`, `ready`, `held by usage limit`, `reaping` or `stopping`, which `_up` leaves out of `eligible`): owner liveness (`_session_liveness`, 373) and outstanding cleanup are established first. Only a positively dead owner moves to a restartable progress, keeping its `target`, timers, pause note and stop issues. `SessionLivenessUnknown` refuses recovery with the reason. Snapshot import never overwrites that record's request.
   - Across accounts: session ids are deduplicated over every account's records and snapshots before any launch. A session id running now on any account (current processes, checked independently of the snapshot's account) is skipped and named. A confirmed account move (a record with `target` that restarted) or a newer observation on another account decides which copy is current; conflicting account evidence refuses the session and names both accounts, never resuming an obsolete copy in its recorded directory (`_resume_parts`, 540).
   - The rest run the restart path a record runs: units through `add_unit.py --host-session`, showrunners and herdr hosts through `_herdr_launch`, window hosts through Phase 8 Spec 4.
   - Printed lines name the source of each session: `from shutdown record` or `from snapshot taken <time in the user zone> (<age>)`. Neither present: `nothing to restore for <account>`. Phase 8's account-optional `restart` also covers every account with a snapshot.
4. Name resolution: `resolve_sessions` (`shutdown.py` 378) reads local recovery snapshots beside membership and shutdown records, so `--only <name|id>` finds a snapshot-only session. A peer restart runs `up <login> --here --json` (`restart.py` 2203); that `up` calls the same source selection, keeping its JSON protocol and `--here`. Snapshots stay machine-local; this phase adds no Mac snapshot producer.
5. **Outside this repo (the showrunner's, in `/etc/nixos`; no worker makes these):**
   - `/etc/nixos/modules/linux/agent-sessions.nix`: the snapshot timer also runs `~/.claude/scripts/lib/py ~/.claude/scripts/shutdown/shutdown.py snapshot`, as its own command, so a failed vault snapshot never skips it.
   - `agent-sessions-snapshot.py` keeps writing the vault notes and adds a herdr branch so the vault names a herdr session's attach command; its generated instructions (635) name `agents restart` in place of `agent-sessions-restore`.
   - `agent-sessions-restore.py` and its wrapper (`agent-sessions.nix` 50–64) are removed once this phase merges and its live check passes.
   - The `agents` wrapper gains `forget`.

6. **Standalone launches choose an account (two-accounts, 2026-10-15).** After two-accounts' Phase 32 cutover `~/.claude` holds no login. `herdr_sessions.py start` calls `accounts.account_for_unscoped_launch(os.environ)` (two-accounts Phase 31, 69952e72) before any herdr server or pane starts: `AccountsError` refuses with its message; `LegacyHomeLaunch` sets nothing; otherwise the pane gets `named_account_environment(selected.dir)` and trust is checked against that account's folder. An explicit `--config-dir` keeps priority. Delivered by two-accounts in its merge 7288c1b5 (resolving Phase 31 against Phases 7–8), which reached build-followups beside 69952e72; this branch takes it by merging build-followups right after the Phase 9 checkpoint, and that merge's gate runs both units' tests. two-accounts holds `cutover-home` until that merge lands.

**Files:**
- `scripts/message/sessions.py` — registry read with unreadable paths.
- `scripts/herdr/herdr_sessions.py`, `scripts/herdr/test_herdr_sessions.py` — account resolution through the shared resolver (Spec 6).
- `scripts/message/test_sessions.py` — an unreadable directory and record reported.
- `scripts/shutdown/inventory.py` — `snapshot_inventory`, shared observations once, read-only timer and pause intent.
- `scripts/shutdown/test_inventory.py` — each source failure makes a `PartialInventory`; a faulted production never yields a standalone; no `release` and no snapshotter run.
- `scripts/shutdown/record.py` — `RecoverySnapshot`, `SnapshotSession`, write and read, materialization into `SessionShutdown`.
- `scripts/shutdown/test_record.py` — round trip; partial inventory keeps the file; empty inventory keeps sessions unresolved; acknowledgement removes one; field mapping.
- `scripts/shutdown/restart.py` — per-session sources, materialization, incomplete-shutdown recovery, cross-account deduplication, source lines.
- `scripts/shutdown/test_restart.py` — record beside snapshot on one account; running session skipped; dead owner in `stopping` recovered with its target and timers; unknown liveness refused; account conflict refused; confirmed move followed; admission as a restore; snapshot entry removed only after success.
- `scripts/shutdown/shutdown.py` — `snapshot`, `forget`, snapshot names in `resolve_sessions`, unresolved sessions in `status`.
- `scripts/shutdown/test_shutdown_cli.py` — `snapshot`, `forget`, `--only` finding a snapshot-only session, peer `up --json` unchanged.
- `commands/shutdown.md` — restore after a crash, `forget`.
- `docs/as-built/shutdown-and-restart.md` — recovery snapshots and restore.

**Workers:** 2 writers (no test lane; `test` opens as impl); split observation and storage against restore.
- `impl` — `sessions.py`, `test_sessions.py`, `inventory.py`, `test_inventory.py`, `record.py`, `test_record.py`; hub: `scripts/shutdown/record.py` (the snapshot and record types restore reads).
- `test` (opens as impl) — `restart.py`, `test_restart.py`, `shutdown.py`, `test_shutdown_cli.py`, `commands/shutdown.md`, `shutdown-and-restart.md`.

**Constraints from prior phases:** `RecordedSessionRestoreLaunchPurpose(session_id)` and its documented hold cases (Phase 8); `agents` runs `shutdown.py` verbs, and `down`, `restart` and `status` take an optional account (Phase 8); window hosts restart into herdr, and a `HerdrHost` records `presentation` (Phase 8). `restart._up` filters `eligible` by `RESTARTABLE_PROGRESS` (136) and refuses not-stopped records under `--only`; `record.update_session` edits an existing record under the login's lock; `settle.unrecorded_local_sessions` names live sessions without a record as `no shutdown`.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/message` and `scripts/shutdown` green; `basedpyright scripts/message scripts/shutdown` prints `0 errors, 0 warnings`. Live (not a test), after the user's rebuild: `shutdown.py snapshot` writes one file per login and changes no session; a throwaway herdr session `herdr-crash-check` (`herdr-claude --name herdr-crash-check` in a trusted scratch folder) is in it; with no shutdown record, stopping its herdr server by hand (`herdr --session herdr-crash-check server stop`, the crash) and running `snapshot` again leaves it `unresolved`; `agents restart` brings it back in herdr, prints `from snapshot taken ...`, answers a question about its earlier turn, and leaves it out of the snapshot. Remove the check session afterwards.

### Phase 10 — Phase 9 follow-ups: crash restore wording, status heading, failed herdr start  · status: todo

**Depends on:** Phase 9

**User sees:** before — `agents status` prints a crashed natedev session under the Mac's heading; a session restored after a crash is told it was restarted after `/shutdown`; a restore whose Claude exits at once waits 90 s and leaves an empty herdr session behind. After — the crashed session is listed under its own machine, the restored session is told it came back after a crash, and a failed start is reported at once with its new herdr session removed.

Found by the Phase 9 live check (2026-10-15) after the third repair round; moved here under the landing rule.

#### Work Order

**Goal:** the three Phase 9 restore defects above are fixed, each with a regression test.

**Spec:**
1. `shutdown.py status` (text form) prints `_unresolved_snapshot_lines` for the local machine inside the local machine's section (the report at index 0), not after the last machine's section. Recovery snapshots are machine-local, so a remote section never carries them.
2. `restart.restart_note` gives a record materialized from a recovery snapshot (`requested_by` kind `snapshot`) its own opening: restored after a crash, naming the crash signal the snapshot recorded (reboot, or its host was lost) and the last-seen time in the user zone, never "after /shutdown" or a stop time. The rest of the note (pending messages, keep waiting on the user) stays.
3. A herdr restart whose agent process exits before startup completes is reported as soon as it exits, not after the start timeout, and the herdr server that restart opened for it is stopped, as the Codex restore already does for its failed start (Phase 9 F005). A server the session already had is left alone.

**Files:**
- `scripts/shutdown/shutdown.py`, `scripts/shutdown/test_shutdown_cli.py` — Spec 1.
- `scripts/shutdown/restart.py`, `scripts/shutdown/test_restart.py` — Specs 2 and 3.
- `scripts/herdr/herdr.py`, `scripts/herdr/test_herdr.py` — Spec 3, if the early exit is detected in `start_agent`.

**Workers:** 1 writer; one file set.

**Acceptance gate:** `merge_gate.py --tests-only` green and `basedpyright scripts/message scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Live: repeat Phase 9's crash check and read the restored session's first prompt and `agents status` placement; restore a snapshot session whose transcript is missing and see the failure reported within seconds with no herdr session left.

### Phase 11 — Remove tmux from productions and sessions on Linux  · status: todo

**Depends on:** Phase 7, Phase 8, Phase 9, Phase 10

**Blocked by:** `shutdown.py tmux-free` (Spec 2) exits 0 on natedev: tmux answers and lists no session, every herdr source answers, and every account's inventory is complete with no session on a tmux or unknown host. `unit_lookup.every_marked_unit` alone is no proof: it returns partial results when one host answers (253–272).

**User sees:** before — some sessions may still run in tmux and every tool carries two hosts; after — every showrunner, unit, standalone and the nightly review runs in herdr, `tmux` appears in no production command, and the docs name only herdr.

#### Work Order

**Goal:** the tmux code paths, tests, jobs and docs are removed, leaving herdr as the only session host on Linux.

**Spec:**

1. Standalone tmux migration, built and tested before any owner is told to move: today `_launch_session` (`restart.py` 1098) moves only a tmux showrunner into herdr; any other non-unit `TmuxHost` session respawns in tmux through `_tmux_launch`. A non-unit `TmuxHost` session restarts through `_herdr_launch` into an exclusive new herdr session (`start_new_server`; the name from `generated_session_name(<tmux session name>, ...)`, a taken name retried with the next suffix), with its conversation, account and model from `ResumeParts`; the record keeps its `TmuxHost`, so the old tmux session's cleanup works from the original host. Tests cover a standalone with a named account and a recorded model, two Claudes from one tmux session, and a taken name.
2. Removal gate, built before any removal: `shutdown.py tmux-free` exits 0 only when each observation is positively readable and empty: `tmux list-sessions` exits 0 with no rows, or reports no server running; `herdr.live_sessions()` answers; the inventory of every configured Claude account answers in full, with no session whose host is `tmux` or `unknown` (an unattributed session). Any unreadable source, tmux session or unattributed session exits 1 and prints it. Run it on natedev before Spec 4; a non-zero exit stops the phase with its output. The phase report quotes its passing output. It goes with the rest of the tmux code in Spec 4.
3. Legacy records, before removal: `UnitHost` records with no `session_host` (read as tmux) and `TmuxHost` hosts, in shutdown records and in Phase 9's recovery snapshots, parse into `LegacyTmuxHost {kind: "legacy tmux", session_name}`; `UnitSessionLeftRunning(host="tmux")` and the old `unit tmux session left running` kind parse into `LegacyTmuxSessionLeftRunning {session_name, cause}`. Neither name is ever used as a herdr destination. `status` and `now` show both with the manual command (`tmux kill-session -t =<name>` for a leftover, the resume command for a host). Restart turns a `LegacyTmuxHost` entry into `ManualRestartRequired` with its resume command; a leftover whose cause is `stop not confirmed` stays in the record as unresolved cleanup (`_unfinished_cleanup`, `restart.py` 1821, reports it and does not clear it), so the entry becomes a manual restart naming both commands.
4. Remove: `unit_lookup.py` tmux source (`tmux_binary`, `_tmux`, `tmux_sessions`, `_marks`, `mark`, the CLI `mark`, `TmuxPlace`); `tmux_names.py` and its test, its registration in `update_registration.py` and its sender name in `conversation_pause.py`; `adopt.py` and its test; `fake_tmux.py` and `fake_tmux.sh` (the shell fake over the same JSON state, used by `test_waiting.py` and `test_dailies_input.py`); tmux branches in `session_pane.py`, `add_unit.py` (`tmux_live`, `kill-session`), `add_showrunner.py`, `promote_showrunner.py`, `unit_effort.py`, `stall_watch.py`, `settle.py`, `stop.py`, `restart.py` (`_tmux_launch`, pane state, `TmuxHost` restart), `inventory.py` (`TmuxHost` and its types), `record.py`, `shutdown.py` (with `tmux-free`), `roster.py`.
5. A `notifier.sh remove tmux-names` for each production registered with it is printed for the showrunners in the phase report; nothing removes another production's instance from a worker.
6. Docs: remove every remaining tmux line from `commands/showrunner/*.md`, `commands/shutdown.md`, `docs/production_format.md`; update as-builts `showrunner-automation`, `session-roster`, `shutdown-and-restart`, `session-notifier`, `agent-registry`, `build-followups-fn-length-hook` where they describe tmux hosts.
7. **Outside this repo (the showrunner's, in `/etc/nixos`; no worker makes these):** `nightly-review.nix` drops tmux from its packages; `agent-sessions-snapshot.py` drops its remaining tmux branches (`agent-sessions-restore.py` is already gone in Phase 9, and restore runs through shutdown). The tmux package stays installed and `tmux: server` stays in earlyoom's `--avoid` (the showrunner's call): this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand.

**Decided by the showrunner, 2026-10-13: move the running tmux sessions at their next idle point, showrunners first.** The showrunner tells each owning showrunner before its sessions move. The record below keeps the reasoning.

Actual problem:
This phase cannot start while any session runs in tmux. Some units are long-running (for example screenshot-unit) and might keep tmux alive for weeks.

What exists now:
- After Phase 7, `/shutdown down <account> --only <names> --restart` stops a session safely and brings it back in herdr with its conversation (units through `add_unit.py`, showrunners through `_herdr_launch`).
- Each such restart interrupts that session for about a minute.
- A non-unit standalone in tmux still respawns in tmux until Spec 1 lands.

What should change:
- Once Spec 1 passes its tests, move each tmux session at its next idle point (between phases for a unit) with `/shutdown --only <name> --restart`, starting with the showrunners.

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
- `scripts/shutdown/inventory.py` — `TmuxHost` removed; `LegacyTmuxHost` parsed.
- `scripts/shutdown/stop.py` — herdr only.
- `scripts/shutdown/record.py` — legacy hosts and leftovers parsed into manual-recovery states.
- `scripts/shutdown/restart.py` — standalone tmux migration into herdr (Spec 1); then `_tmux_launch` removed; legacy entries become manual restarts.
- `scripts/shutdown/settle.py` — herdr only.
- `scripts/shutdown/shutdown.py` — `tmux-free`, then host text; legacy states in `status` and `now`.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/test_stop.py` — follows.
- `scripts/shutdown/test_record.py` — legacy parsing.
- `scripts/shutdown/test_restart.py` — standalone tmux migration; legacy restart.
- `scripts/shutdown/test_settle.py` — follows.
- `scripts/shutdown/test_shutdown_cli.py` — `tmux-free` blocks on an unreadable source and an unattributed session.
- `scripts/shutdown/test_shutdown_status.py` — legacy states in `status` and `now`.
- `scripts/message/roster.py` — herdr only.
- `scripts/message/test_roster.py` — follows.
- `scripts/message/test_top_level.py` — follows.
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

**Workers:** 2 writers (no test lane; `test` opens as impl); split `scripts/production` and `scripts/hooks` against `scripts/shutdown`, `scripts/message` and the docs.
- `impl` — every `scripts/production/` and `scripts/hooks/` file above; hub: `scripts/production/unit_lookup.py`.
- `test` (opens as impl) — every `scripts/shutdown/` and `scripts/message/` file, the commands and the docs; Spec 1 first; hub: `scripts/shutdown/record.py` (the legacy types every shutdown file reads).

**Constraints from prior phases:** every launcher starts in herdr; `session_pane.py`, `stop.py`, `restart.py` and `settle.py` handle `HerdrHost`; `restart.py` moves tmux showrunners and ghostty and zed standalones into herdr; `herdr_sessions.py` has `list`, `attach`, `start`, `resume`; `herdr.generated_session_name` and the exclusive `start_new_server` exist (Phase 8). `UnitHost.session_host` is absent in old records and read as `"tmux"`; `record.py` normalizes the old `unit tmux session left running` kind to `UnitSessionLeftRunning(host="tmux")` (123–131, 684–690); `restart._unfinished_cleanup` (1821) holds an entry whose leftover cause is `stop not confirmed`. Recovery snapshots in `record.py` hold the same `Session` hosts as records (Phase 9). `agent-sessions-restore` is gone and restore runs through `agents restart` (Phase 9); the vault snapshotter in `/etc/nixos` carries both tmux and herdr branches until Spec 7.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown`, `scripts/message`, `scripts/hooks`, `scripts/nightly_review` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/message scripts/hooks scripts/nightly_review scripts/herdr` prints `0 errors, 0 warnings`; `grep -rln tmux scripts/production scripts/shutdown scripts/message commands/showrunner` names only lines kept on purpose (legacy-record parsing and its manual commands), each listed in the phase report. Tests cover the standalone tmux migration (Spec 1) and legacy hosts and leftovers in records and snapshots through parsing, `status`, `now` and restart, never as herdr names and never cleared silently. The phase report quotes `tmux-free`'s passing output from before Spec 4.

### Phase 12 — herdr on the Mac  · status: moved

Moved to its own unit (showrunner, 2026-10-15): `docs/plans/build-followups-herdr-mac.md`. It starts after Phase 11 merges.
