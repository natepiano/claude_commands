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
  - `scripts/production/add_unit.py` — `tmux_live` 507, `preflight` 532, `prompt_for` 665, `launch_session` 701 (systemd-run scope + `tmux new-session -d -e` marks, `claude --model --effort [--resume] --remote-control <name> -n <name> --settings '{"disableAgentView": true}' <prompt>` with `account_prefix(selected)`), `launch_environment` 739, `launched_unit` 762, `wait_for_remote_control` 770 (polls `capture-pane` for `/remote-control is active`), `main` 800 (restore and kill-session 869–899, prints `tmux attach -t` 924, 929). `--tmux-session` is the restore flag `restart.py` passes.
  - `scripts/production/add_showrunner.py` — `claude_json` 57, `checked` 62, `account_for_launch` 113, `launch` 148, `main` 190.
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
- Stop sends no tmux command and records no tmux issue for a herdr unit or host; restart makes a herdr unit or host a manual resume (prints the command, launches nothing, schedules no prompt); a tmux unit keeps `--tmux-session`. Shutdown text: `herdr <session>`, and `unit <production>/<unit> (herdr <session>)` for a herdr unit.
- `pyrightconfig.json`: `scripts/herdr` in the production, message and shutdown environments; the copied-script fixtures in `test_unit_status.py` and `test_update_registration.py` copy `scripts/herdr`.
- The `test_waiting.py` quota case moved to Phase 5 (held by two-accounts Phase 14). Tests need no `HERDR_BIN` override on a machine with real herdr.

### Phase 3 — Trust checked before every launch  · status: moved

Moved to unit `herdr-trust-unit`, run in parallel with Phase 5; shipped as `docs/as-built/herdr-adoption.md` → "Claude trust before every launch". Phase 7 waits for it.

### Phase 4 — Typing into and reading sessions on either host  · status: moved

Moved to `docs/plans/build-followups-herdr-panes.md` (unit `herdr-panes-unit`), run in parallel with Phase 5. Phase 7 waits for it.

### Phase 5 — Shutdown and restart for herdr sessions  · status: done

#### As-built

- `scripts/shutdown/stop.py`: `_stop_unit_session` branches on `session_host`. A tmux unit goes through `_stop_unit_tmux`; a herdr unit's session is stopped with `herdr.stop_session(<session_name>)` after its Claude exits, and judged stopped only when `herdr.live_sessions()` no longer lists it. A session still running is the issue `unit session left running {host: "tmux" | "herdr", session_name, cause}`; `now --only` retries its cleanup.
- `scripts/shutdown/record.py`: `UnitSessionLeftRunning` holds that issue. The old kind `unit tmux session left running` with `tmux_session` (`UnitTmuxLeftRunning`) still parses and is read as `host: "tmux"`.
- `scripts/shutdown/settle.py`: issue text `<host> session <name> left running: <cause>`.
- `scripts/shutdown/restart.py`: `_resume_parts` returns `ResumeParts(cwd, environment, claude_arguments, opening_prompt, account)`, with `account` a `RetainResumeAccount(config_dir)` or `MoveResumeAccount(target)`; `claude_arguments` holds Claude's arguments only, never the `claude` binary or the prompt. `_resume_shell` renders the tmux and desktop shell string unchanged. `_herdr_launch(parts: ResumeParts, session: Session, *, dry_run: bool)` restarts a standalone herdr host: it checks trust with `herdr.trusted_by_claude`, starts the server, refuses a recorded pane that still holds a live agent, opens a pane with the account's config dir variables, and starts Claude with one opening prompt. It refuses a `MoveResumeAccount`. A herdr unit still restarts as a manual resume line (`ManualRestartRequired`); a unit restart passes `--host-session <session_name>` to `add_unit.py`.
- `scripts/production/add_unit.py`: `--host-session`, with `--tmux-session` kept as its alias.
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

**Depends on:** Phase 5; Phase 1 of `docs/plans/build-followups-herdr-trust.md` (herdr-trust-unit)

**User sees:** before — on the Mac, a session whose prompt text holds `TMUX_PANE=%41` is listed by `/shutdown` as tmux-hosted, a herdr launch given the `claude` binary sends it as a stray first prompt, and the restart suite fails on a loaded machine; after — the Mac inventory reads only the real environment, every herdr launch gets one prompt, a herdr restart refuses an untrusted folder with the one command that grants trust, and the suites pass under load.

#### Work Order

**Goal:** quota reaching a herdr unit is proven, the Mac inventory keeps only real environment entries, `_herdr_launch` checks trust through the trust unit's types, `start_agent` takes only the agent's extra arguments, and the six-launch restart test proves overlap without a wall-clock bound.

**Spec:**

1. `scripts/production/test_waiting.py`: a test proves `waiting.py quota` reaches a unit whose place is herdr (`unit_lookup.HerdrPlace`, printed `herdr:<session>:<pane>`), the same way the existing quota tests (913–1016) prove it for a tmux pane. `waiting.py` is not changed. `quota` finds units through `unit_lookup.marked_units(slug)` (`units_from_doc`, `waiting.py` 183) and relays with `send.py --to <claude name>`, so the test needs a herdr-marked unit with a live Claude record: `HERDR_BIN` names `scripts/herdr/fake_herdr.py` (the session listed as running), `HERDR_PROC` a fake `/proc` root whose Claude process carries `HERDR_PANE_ID`, `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` under a herdr server ancestor, and the session record has an empty `tmux` field. Copy the herdr fixture of `scripts/production/test_unit_lookup.py` (`herdr_session`, 59; environment 45–47). Name the test for the behavior it pins. If quota cannot reach a herdr unit without changing `waiting.py`, the test shows the gap, `waiting.py` stays unchanged, and the phase report says so.
2. `scripts/shutdown/inventory.py` `_process_environment` Darwin branch (941–956): it runs `ps eww -o command= -p <pid>` and keeps every token holding `=`, so argument text such as `TMUX_PANE=%41` in a prompt makes a session look tmux-hosted. Keep only real environment entries: read the argv alone with `ps -o command= -p <pid>`, require the `eww` output to start with it, and parse only the text after that prefix; a mismatch (the process changed or vanished between the two calls) or a failed call returns `{}`. Linux `/proc/<pid>/environ` is unchanged. herdr-panes-unit fixes the same pattern in its `session_pane.py` (sibling work, not a dependency).
3. `scripts/shutdown/test_restart.py` `test_six_tmux_launches_finish_in_under_one_second` (1459): it fails under load (1.06 s and 1.28 s at load average 60; it passes alone). Rename it `test_six_tmux_launches_run_at_once`; the fake launch waits on one `threading.Barrier(6)` with a 10 s timeout in place of `time.sleep(0.2)`, and the `assertLess(..., 1.0)` goes. A broken barrier fails the test, so it passes only when all six launches are in flight together. Keep the assertions on `RESTART_LAUNCH_WORKERS`, six distinct threads, the main thread absent and the empty session records.
4. `scripts/shutdown/restart.py` `_herdr_launch` trust check (974, today reading `.accepted` and `.project_key`): herdr-trust-unit Phase 1 gives `herdr.claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile` and `herdr.trust_command(folder, account: ClaudeLaunchAccount)`. Only exact keys count: the folder, its `git rev-parse --show-toplevel`, its main checkout; an ancestor folder never counts. `_herdr_launch` checks trust only through `claude_trust`, never a check of its own; `Untrusted` and `UnreadableTrustFile` each raise `OSError` before `start_server` and before any herdr call, with `trust_command`'s text for the account the resume runs on. `test_restart.py`'s trust cases (`test_herdr_restart_refuses_an_untrusted_folder_before_server_start` 580 and the fixtures that write `.claude.json`) move to the new types and add the unreadable-file case; each asserts the printed trust command and an empty fake herdr log.
5. `scripts/herdr/herdr.py` `Herdr.start_agent` (292) and module `start_agent` (559): `herdr agent start <name> --kind <kind> --pane <p> -- <args>` adds the kind's binary itself, so a caller passing `["claude", ...]` launches `claude claude ...` and the extra word reaches Claude as a stray first prompt. Rename the parameter `argv` to `agent_arguments` (the agent's extra arguments only) in both, and refuse with `ValueError` a first element equal to the kind (`claude` for kind `claude`) before any herdr call. `restart.py` `_herdr_launch` (984) passes `agent_arguments=list(parts.claude_arguments)`. Phase 8 builds its `OpeningPrompt | NoOpeningPrompt` change on this signature.

**Files:**
- `scripts/production/test_waiting.py` — quota reaches a herdr unit.
- `scripts/shutdown/inventory.py` — Darwin environment without argument text.
- `scripts/shutdown/test_inventory.py` — argument text with fake assignments.
- `scripts/shutdown/restart.py` — trust through `claude_trust` and `trust_command`; `agent_arguments` at the `start_agent` call.
- `scripts/shutdown/test_restart.py` — six launches overlap; trust cases on the new types.
- `scripts/herdr/herdr.py` — `start_agent(..., agent_arguments, ...)`, binary refused.
- `scripts/herdr/test_herdr.py` — exactly one `claude` in the launched command.

**Seats:** 2 writers (no test lane; `test` opens as impl); split the library and inventory fixes against restart and the quota test.
- `impl` — `herdr.py`, `test_herdr.py`, `inventory.py`, `test_inventory.py`; hub: `scripts/herdr/herdr.py` (the `start_agent` parameter name `restart.py` calls with).
- `test` (opens as impl) — `restart.py`, `test_restart.py`, `test_waiting.py`.

**Constraints from prior phases:** this phase's checkpoint carries Phase 5's code together with the trust conversion; the production's merge branch takes both at that checkpoint. `_herdr_launch(parts: ResumeParts, session: Session, *, dry_run: bool)` (`restart.py` 952) checks trust before `start_server`. `unit_lookup.every_marked_unit` reads herdr through `herdr.herdr_processes()` (marks `SHOWRUNNER_UNIT`, `SHOWRUNNER_UNIT_ID`) in running sessions from `live_sessions()`; a Claude record with an empty `tmux` field joins its place through `herdr_place_of(pid)`. `inventory._host` reads `TMUX_PANE` from `_process_environment`, then `herdr_place_of`. `fake_herdr.py` start-up can pass 100 ms under load: herdr waits in tests use ≥10 000 ms.

**Acceptance gate:** `python3 -m unittest discover -s <dir> -p 'test_*.py'` green for `scripts/production`, `scripts/shutdown` and `scripts/herdr`; `basedpyright scripts/production/test_waiting.py scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Tests cover a herdr unit reached by `quota`; Darwin argument text with `TMUX_PANE=%41` and `CLAUDE_CONFIG_DIR=/x` yielding neither while a real entry after the argv is kept; six launches meeting at the barrier; `Untrusted` and `UnreadableTrustFile` refused with the trust command before any herdr call; a launched command (fake herdr log) holding exactly one `claude`, nothing after `--` naming the binary, and `agent_arguments` starting with `claude` refused before any herdr call.

### Phase 7 — Units and child showrunners launch in herdr  · status: todo

**Depends on:** Phase 6; the herdr-trust and herdr-panes units' phases landed on the production's merge branch (`docs/plans/build-followups-herdr-trust.md`, `docs/plans/build-followups-herdr-panes.md`); two-accounts-unit's `prepared_account` (Spec 7) and its Phase 17 landed before Spec 6

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
2. **Phase 5's live check, after the rebuild and before Specs 3–7 are installed** (not a test; it runs against the installed Phase 5 and 6 code):
   - A throwaway Claude in herdr session `herdr-stopcheck` in a trusted scratch folder; `shutdown.py down <account> --here --only herdr-stopcheck --restart`; it stops, comes back in `herdr-stopcheck` and answers a question about its earlier turn. Stop the herdr server by hand (`herdr --session herdr-stopcheck server stop`) and confirm `claude --resume <id>` in a new herdr session restores the conversation without herdr's Claude integration.
   - A scratch unit's Claude in its own herdr session (opened with `herdr.open_pane` carrying both marks, then `herdr.start_agent`): `shutdown.py down <account> --here --only <name> --restart` stops its session and prints the manual resume line with its session id, cwd and account.
   - Stop and delete both sessions. A failure stops the phase and goes to the showrunner.
3. `add_unit.py` `launch_session` becomes herdr-only for new launches: `start_server(<session name>)`; `open_pane(<session name>, <session name>, session_cwd, environment)` where environment is the marks, `ENABLE_TOOL_SEARCH=true`, and `named_account_environment(dir)` for a named account (none for the default); `start_agent(..., kind="claude", agent_arguments=["--model", m, "--effort", e?, "--resume", id?, "--remote-control", name, "-n", name, "--settings", '{"disableAgentView": true}'], opening_prompt=prompt_for(request))`. `agent_arguments` holds Claude arguments only; herdr adds the `claude` binary itself (Phase 6). `AgentNotReady` exits 1 with the screen text and sends nothing. The caller's `CLAUDE_*` variables never reach the pane (`launch_environment`).
   - Refusals: a running herdr session of that name not marked as this unit (as `tmux_live` today).
   - Restore of a marked unit with no live Claude: a herdr place is ended with `stop_session`, a tmux place with `kill-session` (existing), then relaunched in herdr under `--host-session`.
   - `wait_for_remote_control` keeps reading the pane through `read_place` for `/remote-control is active`.
   - Printed lines: `<unit> started: herdr session attach <name>` and `<unit> restore launched: herdr session attach <name>`.
4. `add_showrunner.py` `launch`: the same herdr start with no marks; the selected account's variables (`named_account_environment(dir)` for a named or retained account, none for the default) and never the caller's `CLAUDE_*`; `tmux_live` becomes a check for a running herdr session of that name; printed lines and `later` text name `herdr session attach <slug>`.
   - **Recorded account and model for a herdr place** (showrunner, 2026-10-13): store nothing new. A resume without `--account` of a herdr place asks `accounts.py` for its role's account (`role_account("showrunner")` for a showrunner, `role_account("unit")` for a unit; the group account once two-accounts' group phase lands). The model is read from the session's transcript (the model on its last assistant message), falling back to the director config's model. The tmux `@claude_config_dir`/`@claude_model` readers stay as they are for tmux places.
5. `promote_showrunner.py`: no launch code of its own; its printed lines name `herdr session attach`.
6. `restart.py`:
   - `_herdr_launch(parts, destination, session, *, dry_run)` takes `destination: NewHerdrSession(name) | RecordedHerdrPane(name, pane_id)` (frozen dataclasses) in place of reading `session["host"]`. A `HerdrHost` maps to `RecordedHerdrPane` (today's path: a pane holding a live agent is a refusal, a gone or agentless pane is replaced). `NewHerdrSession` starts the server and opens a pane; a running herdr session of that name holding a live agent is a refusal before any pane opens or agent starts. No empty pane id and no `None` stands for a new session.
   - A `tmux` host whose session is a showrunner restarts through `_herdr_launch(parts, NewHerdrSession(<tmux session name>), ...)`, so a restart moves it into herdr; the record keeps its `TmuxHost`, so the cleanup of the old tmux session works from the original host. Other tmux hosts respawn as before.
   - A unit whose `session_host` is `herdr` restarts through `add_unit.py --host-session <session_name>` like a tmux unit; remove the manual-restart guard (1003; before this phase `add_unit.py` launches only tmux, so routing a herdr unit there would move it into tmux).
   - An account move into a herdr host: remove the `MoveResumeAccount` refusal (957). `_herdr_launch` takes the pane environment from `accounts.prepared_account(target, resume=True)` (Spec 7); `AccountRefused` raises `OSError` with its reason before `start_server`; the Claude argv stays Claude arguments only (no `accounts.py exec` wrapper).
7. **Moved to two-accounts-unit** (showrunner, 2026-10-13: `accounts.py` is that unit's file and API). It builds `accounts.prepared_account(label, *, resume: bool) -> PreparedAccount | AccountRefused` beside its Phase 17, with `exec_account` behavior unchanged. `PreparedAccount` holds the variables to set and the names to remove (`CLAUDE_CODE_OAUTH_TOKEN` always; the named-account variables for the default and the default-login alias); `AccountRefused` carries exec_account's refusal text. Spec 6 consumes it once it lands. The herdr path sets the variables through `open_pane`'s environment and never passes a removed name: the server runs under the systemd user manager (`systemd-run --user`), which receives no caller environment.
8. Docs: `commands/showrunner/add_showrunner.md` restates the user's 2026-10-12 rule ("top level (L1) showrunners should make sure to use tmux to start lower level showrunners") as: an L1 showrunner starts lower showrunners with `add_showrunner.py`, which now opens each in its own herdr session; keep the quote and say the host changed with the user's 2026-10-13 move to herdr. Also `commands/showrunner/add_unit.md`, `promote_unit.md`, `units.md` (`gone` means no tmux or herdr session carries the mark), `produce.md` (lines 169–171, 325, 363, 1119), `commands/producer/to_production.md:104`, `docs/production_format.md:36,126`, and `production_lifecycle.py` line 482 (`close herdr or tmux sessions`).
9. Live check (not a test): `add_unit.py` for a throwaway standby unit of a scratch production in a trusted scratch repository; it starts in herdr, `/remote-control is active` is read, `production_lifecycle.py load` lists it live, the roster files it under the scratch showrunner, `herdr session attach <name>` opens it, `/shutdown --only <name> --restart` brings it back. Remove the scratch unit and session afterwards.

**Files:**
- `scripts/production/add_unit.py` — herdr launch and restore.
- `scripts/production/test_add_unit.py` — herdr launch argv, env, refusals, restore.
- `scripts/production/add_showrunner.py` — herdr launch.
- `scripts/production/test_add_showrunner.py` — herdr launch.
- `scripts/production/promote_showrunner.py` — printed lines.
- `scripts/production/test_promote_showrunner.py` — printed lines.
- `scripts/production/production_lifecycle.py` — wrap text.
- `scripts/production/test_production_lifecycle.py` — wrap text.
- `scripts/shutdown/restart.py` — destination types, tmux showrunner migration, herdr unit through `add_unit.py`, account move into herdr.
- `scripts/shutdown/test_restart.py` — those cases.
- `commands/showrunner/add_showrunner.md` — herdr, with the user's rule restated.
- `commands/showrunner/add_unit.md` — herdr attach line.
- `commands/showrunner/promote_unit.md` — herdr attach line.
- `commands/showrunner/units.md` — `gone` meaning.
- `commands/showrunner/produce.md` — launch and attach lines.
- `commands/producer/to_production.md` — line 104.
- `docs/production_format.md` — lines 36 and 126.

**Seats:** 2 writers (no test lane; `test` opens as impl). Early start (showrunner, 2026-10-13), before herdr-panes, two-accounts Phase 17 and the rebuild land: `impl` — `production_lifecycle.py` and its test, and the docs outside herdr-panes' files (`add_showrunner.md`, `add_unit.md`, `units.md`, `to_production.md`, `production_format.md`); `test` — `add_showrunner.py` and its test (Spec 4). After those land: `impl` — `add_unit.py`, `test_add_unit.py`, `produce.md`, `promote_unit.md` (Spec 3 and the rest of Spec 8; hub: `add_unit.py`, whose `launch_environment` and `Refusal` `add_showrunner.py` imports); `test` — `promote_showrunner.py`, `restart.py` and their tests (Specs 5 and 6).

**Constraints from prior phases:** trust is checked in `preflight` and `add_showrunner.checked` through `herdr.claude_trust(folder, claude_json) -> Trusted | Untrusted | UnreadableTrustFile` and `herdr.trust_command(folder, account: ClaudeLaunchAccount)`; `session_pane.py` and `read_place` read either host; `MarkedUnit.place` replaced `pane`; `restart.py` passes `--host-session`, which `add_unit.py` accepts beside `--tmux-session`. `restart.py`: `_herdr_launch(parts: ResumeParts, session: Session, *, dry_run: bool)` (952) takes `ResumeParts(cwd, environment, claude_arguments, opening_prompt, account: RetainResumeAccount | MoveResumeAccount)` (474–521), never a shell string, and raises `ValueError` for any host but a `HerdrHost`; it refuses `MoveResumeAccount` (957) and checks trust through `claude_trust` before `start_server` (Phase 6); a herdr unit's restart is a manual resume line (1003) until Spec 6 here. `herdr.start_agent(name, pane, kind, agent_arguments, opening_prompt, timeout_ms)` takes the agent's extra arguments only and refuses the binary as first element (Phase 6). `accounts.exec_account` (1003) pops `CLAUDE_CODE_OAUTH_TOKEN` and handles readiness and the default-login alias. `herdr.py` `start_server`, `open_pane`, `start_agent`, `stop_session`, `live_sessions`.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/herdr` prints `0 errors, 0 warnings`. Tests show the herdr argv carries no prompt, the opening prompt goes once through `agent prompt`, the pane environment holds both marks and the account variables and no `CLAUDE_*` from the caller, and `AgentNotReady` sends nothing; a tmux showrunner migrates into a new herdr session with its `TmuxHost` kept, and an occupied destination is refused before any agent starts; an account move into herdr carries the target's variables and no `CLAUDE_CODE_OAUTH_TOKEN`, drops the named variables for a default-login alias, and refuses an account not set up or logged out before `start_server`, with Claude-only argv. The live checks in Specs 2 and 9 pass.

### Phase 8 — Standalone sessions and the nightly review in herdr  · status: todo

**Depends on:** Phase 7

**User sees:** before — a standalone `claude` runs straight in a ghostty window, and the nightly review runs in tmux; after — `herdr-claude [name]` opens a standalone Claude in its own herdr session in the current terminal, `continue` does the same with `--continue`, a restart brings a ghostty standalone back in herdr in a ghostty window attached to it, and the nightly review runs in herdr session `nightly-review`.

#### Work Order

**Goal:** standalone Claude sessions and the nightly review start, restart and restore in their own herdr sessions.

**Spec:**

1. `herdr.py`:
   - `start_agent` (method and module; `agent_arguments` from Phase 6) takes `opening: OpeningPrompt | NoOpeningPrompt` (frozen dataclasses; `OpeningPrompt(text)`) in place of `opening_prompt: str`. `NoOpeningPrompt` sends no `agent prompt` and no keys; today the method calls `self.prompt(pane, opening_prompt)` unconditionally (307). Every caller passes one: `add_unit.py`, `add_showrunner.py`, `restart.py` `_herdr_launch` pass `OpeningPrompt`.
   - Session names map one-to-one to systemd units: today `server_unit` (417) maps every character outside `[A-Za-z0-9_.-]` to `-`, so `a b` and `a-b` share `herdr-a-b`. `server_unit`, `start_server` and `stop_session` refuse a name outside `[A-Za-z0-9_.-]+` with `ValueError` naming the allowed set; `server_unit` is then `herdr-<name>`.
   - `attached_clients(name) -> ClientsAttached(count) | NoClientAttached | ClientsUnreadable(detail)`: the user's processes under `HERDR_PROC` whose argv is a herdr client of that session (`herdr session attach <name>`, or herdr global options with `--session <name>` and no trailing `server`). An unreadable `/proc` is `ClientsUnreadable`.
   - `herdr_processes()` marks gain `NIGHTLY_REVIEW` beside `SHOWRUNNER_UNIT` and `SHOWRUNNER_UNIT_ID` (552).
2. `herdr_sessions.py` gains:
   - `start [--name N] [--cwd D] [-- <claude args>]`: name defaults to the cwd's basename with each character outside `[A-Za-z0-9_.-]` replaced by `-`, plus a short suffix when taken; an explicit name outside that set is refused. Refuses an untrusted folder (`claude_trust`, printing `trust_command`; the account from `CLAUDE_CONFIG_DIR` or the default). Admission as Spec 3, as new work. `start_server`, `open_pane`, `start_agent(kind="claude", agent_arguments=[*claude args, "-n", name], opening=NoOpeningPrompt())` (the user types); then execs `herdr session attach <name>`.
   - `resume <name> --session-id <id> --cwd <dir> [--config-dir <dir>] [--restore] [--prompt-file <path>] [--attach] [-- <claude args>]`: the same with `--resume <id>` and the passed Claude arguments (model, settings, Remote Control), for the agent-sessions restore and by hand. `--prompt-file` is read once and sent as `OpeningPrompt` (multiline text goes through `agent prompt`, never the start argv); without it, `NoOpeningPrompt`. `--restore` makes the admission a restore of that recorded session. No attach unless `--attach`.
3. Admission for `start` and `resume`, before any server starts, as unit and nightly launchers do: hold `launch_barrier()` and ask `launch_permission(environment, purpose)` (`scripts/shutdown/launch_permission.py`) with the selected account's environment. New work passes `NewWorkLaunchPurpose()`; `resume --restore` passes `ShutdownUnitRestoreLaunchPurpose(<session id>)` (`shutdown.py launch_blocked` 246 permits the restore of any stopped recorded session while the account is held). `BlockedByShutdown` and `ShutdownStateUnreadable` exit 1 with the reason; a barrier `OSError` is unreadable state.
4. `restart.py`:
   - A `ghostty` or `zed` host restarts into herdr in-process: `_herdr_launch(parts, NewHerdrSession(<herdr name>), ...)` with the full `ResumeParts` (recorded model, settings, Remote Control arguments, account), its opening prompt as `OpeningPrompt`, delivered once, and `_record_scheduled_restart_prompt` called once. The herdr name is the Claude session name with each character outside `[A-Za-z0-9_.-]` replaced by `-`; a running herdr session of that name is an occupied destination and a refusal. Then one ghostty window runs `herdr session attach <name>` on the recorded desktop (the existing window opener with the new command).
   - A `herdr` host (a standalone started by `start`) reopens a window when it had one: `HerdrHost` gains `presentation: "attached window" | "detached"`, recorded at inventory time from `attached_clients` (`ClientsUnreadable` and old records read as `"detached"`). On restart an `"attached window"` host gets one ghostty window running `herdr session attach <name>`; a `"detached"` host prints `herdr session attach <name>`. Today the herdr branch (1023–1025) returns without a window.
   - The Mac `terminal` host is unchanged in this phase and the next; it moves in the Mac phase.
5. `nightly_review.py`: the session runs in herdr session `nightly-review` (`start_server`, `open_pane` with the mark `NIGHTLY_REVIEW=1`, `start_agent` with `OpeningPrompt(PROMPT)`; `PROMPT` stays `/nightly_review config rust messages`). `retire` keeps both protections through explicit observations: `attached_clients(name)` (`ClientsAttached` leaves it running with the attached text) and ownership (a process in that session carries `NIGHTLY_REVIEW`, from `herdr_processes()`; none leaves it running with the foreign text). Any unreadable observation (`ClientsUnreadable`, `HerdrFailure`) leaves the session running with a line naming the failure. Only then `stop_session`. Admission (`launch_barrier`, `launch_permission`) is unchanged. `running` uses `live_sessions()`. Printed lines name `herdr session attach nightly-review`. `commands/nightly_review.md` names the same.
6. **Outside this repo (the showrunner's, in `/etc/nixos`; no seat makes these):**
   - `/etc/nixos/modules/common/shell.nix`: `herdr-claude() { ~/.claude/scripts/lib/py ~/.claude/scripts/herdr/herdr_sessions.py start "$@"; }` and `alias continue='herdr-claude -- --continue'` inside the existing `CLAUDECODE` guard.
   - `/etc/nixos/modules/linux/nightly-review.nix`: herdr in the path beside tmux, comments naming `herdr session attach nightly-review`.
   - `/etc/nixos/modules/linux/agent-sessions-snapshot.py` and `agent-sessions-restore.py`: keep their tmux branches (live tmux sessions remain until the tmux removal phase); add a herdr branch: a Claude whose ancestry ends at a herdr server is recorded with its herdr session name, and restored with `herdr_sessions.py resume <name> --session-id <id> --cwd <dir> [--config-dir <dir>] --restore` plus a ghostty window running `herdr session attach <name>`.

**Files:**
- `scripts/herdr/herdr.py` — `OpeningPrompt | NoOpeningPrompt`, name validation, `attached_clients`, `NIGHTLY_REVIEW` mark.
- `scripts/herdr/test_herdr.py` — promptless start logs no prompt or keys; `a b` refused, starting or stopping `a-b` touches only `herdr-a-b`; client observation.
- `scripts/herdr/herdr_sessions.py` — `start`, `resume`, admission.
- `scripts/herdr/test_herdr_sessions.py` — both verbs, trust refusal, name collision and invalid names, held account, unreadable shutdown state, permitted restore, all before any server starts; multiline `--prompt-file` sent once.
- `scripts/production/add_unit.py` — passes `OpeningPrompt`.
- `scripts/production/add_showrunner.py` — passes `OpeningPrompt`.
- `scripts/shutdown/restart.py` — window hosts restart into herdr; herdr host presentation.
- `scripts/shutdown/test_restart.py` — window host with a recorded model and a multiline prompt; attached and detached herdr hosts.
- `scripts/shutdown/inventory.py` — `HerdrHost.presentation`.
- `scripts/shutdown/test_inventory.py` — presentation recorded and old records parsed.
- `scripts/nightly_review/nightly_review.py` — herdr session.
- `scripts/nightly_review/test_nightly_review.py` — herdr cases, attached, foreign and unreadable observations.
- `commands/nightly_review.md` — attach line.
- `docs/as-built/shutdown-and-restart.md` — window and herdr hosts restart into herdr.

**Seats:** 2 writers (no test lane; `test` opens as impl); split the library, standalone CLI and nightly review against restart and inventory.
- `impl` — `herdr.py`, `test_herdr.py`, `herdr_sessions.py`, `test_herdr_sessions.py`, the `add_unit.py` and `add_showrunner.py` call sites, `nightly_review.py`, `test_nightly_review.py`, `commands/nightly_review.md`; hub: `scripts/herdr/herdr.py` (every caller imports the new types).
- `test` (opens as impl) — `restart.py`, `test_restart.py`, `inventory.py`, `test_inventory.py`, `shutdown-and-restart.md`.

**Constraints from prior phases:** herdr is on PATH on natedev; launchers start in herdr; `restart.py` `_herdr_launch(parts, destination: NewHerdrSession | RecordedHerdrPane, session, *, dry_run)` with `ResumeParts(cwd, environment, claude_arguments, opening_prompt, account)`; trust through `herdr.claude_trust` and `trust_command`; `herdr_sessions.py list` and `attach` exist. `launch_permission.py`: `launch_barrier`, `launch_permission`, `NewWorkLaunchPurpose`, `ShutdownUnitRestoreLaunchPurpose(session_id)`, outcomes `AllowedByShutdownState | BlockedByShutdown | ShutdownStateUnreadable`. `nightly_review.retire` (276) today leaves a session running when a client is attached or no `/nightly_review` started it.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr`, `scripts/production`, `scripts/shutdown` and `scripts/nightly_review` green; `basedpyright scripts/herdr scripts/production scripts/shutdown scripts/nightly_review` prints `0 errors, 0 warnings`. Live (not a test): `herdr_sessions.py start --name herdr-standalone-check` in a trusted folder opens an attached Claude; `/shutdown --only herdr-standalone-check --restart` brings it back in a ghostty window attached to its herdr session; `nightly_review.py launch --dry-run` (or its check mode) names the herdr session. Remove the check session afterwards.

### Phase 9 — Remove tmux from productions and sessions on Linux  · status: todo

**Depends on:** Phase 7, Phase 8

**Blocked by:** `shutdown.py tmux-free` (Spec 1) exits 0 on natedev: tmux answers and lists no session, every herdr source answers, and every account's inventory is complete with no session on a tmux or unknown host. `unit_lookup.every_marked_unit` alone is no proof: it returns partial results when one host answers (253–272).

**User sees:** before — some sessions may still run in tmux and every tool carries two hosts; after — every showrunner, unit, standalone and the nightly review runs in herdr, `tmux` appears in no production command, and the docs name only herdr.

#### Work Order

**Goal:** the tmux code paths, tests, jobs and docs are removed, leaving herdr as the only session host on Linux.

**Spec:**

1. Removal gate, built first: `shutdown.py tmux-free` exits 0 only when each observation is positively readable and empty: `tmux list-sessions` exits 0 with no rows, or reports no server running; `herdr.live_sessions()` answers; the inventory of every configured Claude account answers in full, with no session whose host is `tmux` or `unknown` (an unattributed session). Any unreadable source, tmux session or unattributed session exits 1 and prints it. Run it on natedev before Spec 3; a non-zero exit stops the phase with its output. The phase report quotes its passing output. It goes with the rest of the tmux code in Spec 3.
2. Legacy records, before removal: `UnitHost` records with no `session_host` (read as tmux) and `TmuxHost` hosts parse into `LegacyTmuxHost {kind: "legacy tmux", session_name}`; `UnitSessionLeftRunning(host="tmux")` and the old `unit tmux session left running` kind parse into `LegacyTmuxSessionLeftRunning {session_name, cause}`. Neither name is ever used as a herdr destination. `status` and `now` show both with the manual command (`tmux kill-session -t =<name>` for a leftover, the resume command for a host). Restart turns a `LegacyTmuxHost` entry into `ManualRestartRequired` with its resume command; a leftover whose cause is `stop not confirmed` stays in the record as unresolved cleanup (`_unfinished_cleanup`, `restart.py` 1561, reports it and does not clear it), so the entry becomes a manual restart naming both commands.
3. Remove: `unit_lookup.py` tmux source (`tmux_binary`, `_tmux`, `tmux_sessions`, `_marks`, `mark`, the CLI `mark`, `TmuxPlace`); `tmux_names.py` and its test, its registration in `update_registration.py` and its sender name in `conversation_pause.py`; `adopt.py` and its test; `fake_tmux.py`; tmux branches in `session_pane.py`, `add_unit.py` (`tmux_live`, `kill-session`, `--tmux-session`), `add_showrunner.py`, `promote_showrunner.py`, `unit_effort.py`, `stall_watch.py`, `settle.py`, `stop.py`, `restart.py` (`_tmux_launch`, pane state, `TmuxHost` restart), `inventory.py` (`TmuxHost` and its types), `record.py`, `shutdown.py` (with `tmux-free`), `roster.py`.
4. A `notifier.sh remove tmux-names` for each production registered with it is printed for the showrunners in the phase report; nothing removes another production's instance from a seat.
5. Docs: remove every remaining tmux line from `commands/showrunner/*.md`, `commands/shutdown.md`, `docs/production_format.md`; update as-builts `showrunner-automation`, `session-roster`, `shutdown-and-restart`, `session-notifier`, `agent-registry`, `build-followups-fn-length-hook` where they describe tmux hosts.
6. **Outside this repo (the showrunner's, in `/etc/nixos`; no seat makes these):** `nightly-review.nix` drops tmux from its path; `agent-sessions-snapshot.py` and `agent-sessions-restore.py` drop their tmux branches. The tmux package stays installed and `tmux: server` stays in earlyoom's `--avoid` (the showrunner's call): this phase removes tmux only from our scripts and launches, and the user may still use tmux by hand.

**Decided by the showrunner, 2026-10-13: move the running tmux sessions at their next idle point, showrunners first.** The showrunner tells each owning showrunner before its sessions move. The record below keeps the reasoning.

Actual problem:
This phase cannot start while any session runs in tmux. Some units are long-running (for example screenshot-unit) and might keep tmux alive for weeks.

What exists now:
- After Phase 7, `/shutdown down <account> --only <names> --restart` stops a session safely and brings it back in herdr with its conversation (units through `add_unit.py`, showrunners through `_herdr_launch`).
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
- `scripts/shutdown/inventory.py` — `TmuxHost` removed; `LegacyTmuxHost` parsed.
- `scripts/shutdown/stop.py` — herdr only.
- `scripts/shutdown/record.py` — legacy hosts and leftovers parsed into manual-recovery states.
- `scripts/shutdown/restart.py` — `_tmux_launch` removed; legacy entries become manual restarts.
- `scripts/shutdown/settle.py` — herdr only.
- `scripts/shutdown/shutdown.py` — `tmux-free`, then host text; legacy states in `status` and `now`.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/test_stop.py` — follows.
- `scripts/shutdown/test_record.py` — legacy parsing.
- `scripts/shutdown/test_restart.py` — legacy restart.
- `scripts/shutdown/test_settle.py` — follows.
- `scripts/shutdown/test_shutdown_cli.py` — `tmux-free` blocks on an unreadable source and an unattributed session.
- `scripts/shutdown/test_shutdown_status.py` — legacy states in `status` and `now`.
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
- `test` (opens as impl) — every `scripts/shutdown/`, `scripts/message/`, `scripts/nightly_review/` file, the commands and the docs; hub: `scripts/shutdown/record.py` (the legacy types every shutdown file reads).

**Constraints from prior phases:** every launcher starts in herdr; `session_pane.py`, `stop.py`, `restart.py` and `settle.py` handle `HerdrHost`; `restart.py` moves tmux showrunners and ghostty and zed standalones into herdr; `herdr_sessions.py` has `list`, `attach`, `start`, `resume`. `UnitHost.session_host` is absent in old records and read as `"tmux"`; `record.py` normalizes the old `unit tmux session left running` kind to `UnitSessionLeftRunning(host="tmux")` (123–131, 684–690); `restart._unfinished_cleanup` (1561) holds an entry whose leftover cause is `stop not confirmed`. Snapshot and restore in `/etc/nixos` carry both tmux and herdr branches until Spec 6.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/production`, `scripts/shutdown`, `scripts/message`, `scripts/hooks`, `scripts/nightly_review` and `scripts/herdr` green; `basedpyright scripts/production scripts/shutdown scripts/message scripts/hooks scripts/nightly_review scripts/herdr` prints `0 errors, 0 warnings`; `grep -rln tmux scripts/production scripts/shutdown scripts/message commands/showrunner` names only lines kept on purpose (legacy-record parsing and its manual commands), each listed in the phase report. Tests cover legacy hosts and leftovers through parsing, `status`, `now` and restart, never as herdr names and never cleared silently. The phase report quotes `tmux-free`'s passing output from before Spec 3.

### Phase 10 — herdr on the Mac  · status: todo

**Depends on:** Phase 9

**Packaging (showrunner, 2026-10-13):** when this phase is reached, split it into its own plan if it is more than one phase of work.

**Blocked by:** Phase 9 done on natedev; the showrunner may move this phase to its own plan.

**User sees:** before — Mac standalones run straight in Ghostty windows, and Mac sessions have no herdr; after — `herdr-claude` on the Mac opens a standalone in its own herdr session, and a Mac restart brings sessions back in herdr in a Ghostty window attached to them.

#### Work Order

**Goal:** the herdr library, standalone launcher and restart work on the Mac, with launchd in place of systemd.

**Spec:**

1. `herdr.py` `start_server` and `stop_session` on Darwin use `launchctl submit -l <server_unit> -- <herdr> --session <name> server` and `launchctl remove <server_unit>`; Linux keeps systemd. The platform is chosen once by `sys.platform`; tests cover both with fake programs (`LAUNCHCTL` env names the program).
2. `herdr_place_of`, `herdr_processes` and `attached_clients` on Darwin read processes through `ps` (`HERDR_PS` names the program for tests; the Mac has no `/proc`):
   - Only the current user's processes (`ps -U <uid> -o pid=,ppid=,uid=,command=`), each row's uid checked again.
   - Environment through Phase 6's rule: the `ps eww` text after the process's own `ps -o command=` argv, so argument text such as `HERDR_PANE_ID=w1:p1` in a prompt never counts. `herdr.py` holds that parse; `inventory._process_environment`'s Darwin branch calls it, so the rule lives once.
   - `cwd` per process from `lsof -a -p <pid> -d cwd -Fn` (`HERDR_LSOF` names it).
   - A process that vanishes between calls, or whose call fails, is skipped.
3. `restart.py` `terminal` host on the Mac restarts into herdr in-process (`_herdr_launch` with `NewHerdrSession` and the full `ResumeParts`, as Linux window hosts do) and opens `Ghostty.app` with `-e herdr session attach <name>`.
4. **Outside this repo (the showrunner's, in `~/nixos`, nix-darwin only; no seat makes these):** herdr in the Mac's packages; `herdr-claude` and the `continue` alias already sit in `modules/common/shell.nix` (shared with natedev, added in Phase 8).
5. Live check on the Mac (needs the user at the Mac for the Ghostty window): `herdr-claude --name herdr-mac-check`, `/shutdown --only herdr-mac-check --restart`, the conversation comes back attached. Remove the check session afterwards.

**Files:**
- `scripts/herdr/herdr.py` — launchd, `ps` and `lsof` paths, Darwin environment parse.
- `scripts/herdr/test_herdr.py` — Darwin cases: UID filtering, cwd, vanished processes, argument text with fake environment assignments.
- `scripts/shutdown/inventory.py` — Darwin environment through `herdr.py`.
- `scripts/shutdown/test_inventory.py` — follows.
- `scripts/shutdown/restart.py` — Mac terminal host into herdr.
- `scripts/shutdown/test_restart.py` — that case.

**Seats:** 2 writers (no test lane; `test` opens as impl); split the library and inventory against restart.
- `impl` — `herdr.py`, `test_herdr.py`, `inventory.py`, `test_inventory.py`; hub: `scripts/herdr/herdr.py`.
- `test` (opens as impl) — `restart.py`, `test_restart.py`.

**Constraints from prior phases:** `herdr.py` runs servers as systemd user units on Linux and finds places and clients through `/proc`; session names are `[A-Za-z0-9_.-]+` and `server_unit` is `herdr-<name>`; `start_agent` takes `OpeningPrompt | NoOpeningPrompt`; `herdr_sessions.py start|resume|list|attach`; `restart.py` restarts ghostty and zed hosts into herdr on Linux through `_herdr_launch(parts, NewHerdrSession(name), ...)` and leaves `terminal` unchanged; `inventory._process_environment` parses Darwin environment after the argv prefix (Phase 6); tmux is gone from the session tools.

**Acceptance gate:** `python3 -m unittest discover` for `scripts/herdr` and `scripts/shutdown` green on natedev and on the Mac; `basedpyright scripts/herdr scripts/shutdown` prints `0 errors, 0 warnings`. The live check in Spec 5 passes.
