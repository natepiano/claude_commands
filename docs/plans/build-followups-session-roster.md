# Session roster: list and message every session, Claude and Codex

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Adds one script that any Claude or Codex session can run. It lists every session of interest: Claude showrunners, unit directors, workers and freestanding sessions with their roles, and Codex workers across every unit with their unit director and showrunner, plus the user's own Codex sessions. The existing senders then reach any of those sessions by name, singly or by group, on both machines.

> **As-built disposition: create**

> **Production: build-followups** — unit `shutdown-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-10 09:0x PDT: "claude can do a listagents so claude can just do it - we need an ability for claude to simply list via a script all of the workers and who is their unit director and showrunner, and also for any codex session to list all the claude sessions (unit, showrunner, freestanding - and their roles if possible) i assume codex can list their own sessions but find out - you udnerstand i hoep - i'm looking for the ability to extend our messaging infra to simply list all sessions of interest across agents and send messages to all types we've already defined or to individauls within and across agents"

The user, via natedev, a few minutes later: "it's fine if our skills instruct agents how to do different activities -- what i mean is if a codex needs to list other codex - it can do that itself and if it needs to talk to other claudes, it can use our script / and vice versa, right?"

natedev, 2026-10-10: each kind of agent lists its own kind natively and uses the script for the other kind, and the skills say which to use. Claude keeps ListAgents for Claude sessions. The gap to cover: Codex has no native list it can run from a script across units, so the script lists Codex workers across units for Claude and Codex alike. Boundary: model-study owns `scripts/agents/codex_mesh.py` and the seat launcher (`docs/plans/build-followups-long-lived-seats.md`). Read their records, never edit them, and ask model-study for any field.

natedev, 2026-10-10 09:5x PDT, inserting Phase 2 (urgent; packaging is natedev's), with the user's words relayed by hana: "Any message in the message infra being sent to a user should go through a message review to make sure that it tells the user the action he needs to take or that there is no action required. Send this to natedev session to address in the messaging infra." Trigger, 09:38 PDT: a widget seat sent an Emergency-priority Pushover, 'widget-enhancements-impl: Mac test paused. The shared Mac gate needs five minutes with no local input and no user Hana process; all other implementation gates have passed.' The user asked how he was supposed to respond; no action was needed. natedev: the gate belongs in the infra, not per sender; every path to the user is `scripts/notify/pushover.py` (called directly, seats included), `send.py --to user` and `commands/alert_user.md`; a message that says neither the action nor that none is needed is refused back to its sender with the reason; Emergency only when the user must act now; `pushover.py` and `alert_user.md` join shutdown-unit's Owns; tests never push.

## What exists today (checked 2026-10-10 by the plan author)

- **Claude sessions.** `~/.claude/sessions/<pid>.json` holds `pid`, `procStart`, `sessionId`, `name`, `cwd`, `kind`, `status` (`busy`, `idle`, `shell`), `messagingSocketPath`, `tmux` and `formerNames`. `scripts/message/sessions.py` reads and validates these records (`read_session`, `live_session`, `live_sessions`, `addressed`).
- **Claude roles are written down in four places, and nothing joins them:**
  - Showrunners: the update timers under `~/.local/state/notifier/showrunner-*`. `scripts/production/showrunners.py` `registered_showrunners()` gives each one's session name, slug and production doc.
  - Unit directors under a production: the doc's Units table, plus the tmux session marked `SHOWRUNNER_UNIT` / production. `scripts/production/live_units.py` `production_units(doc)` and `unit_lookup.marked_units(slug)` read these. tmux runs only on natedev.
  - Any `/unit:direct` run: `prepare_session.sh` writes `/tmp/claude/delegate/active/<director's Claude session id>`, holding the run's session directory. `end_session.sh` removes it. A marker from a director that died stays behind; 12 markers exist today, some from 2026-10-06.
  - Claude workers: every `agent_bg.sh` launch appends `<session id>\t<name>` to its run's `seats` ledger. `scripts/delegate/remove_seats.py` already decides which runs are live (`live_runs`: a marker whose director is still listed, or a `heartbeat.log` written in the last `LIVE_HEARTBEAT_SECS` = 600).
- **Codex workers** are threads on one app-server per run. `<session dir>/mesh_roster.json` maps each seat name to `thread_id`, `status` (`starting`, `running`, `done`, `failed`, `waiting_capacity`, `capacity_exhausted`), `turn_id`, `launcher_pid` and `port`. `mesh_server.json` holds the server's `pid` and `port`. `codex_mesh.py list --session-dir` prints one run's roster only. Seat names are `<worktree>-<slot>`, from `scripts/delegate/seat_name.sh`. Running blind reviewers (`codex exec`, launched by `review.sh` through `agent_exec.sh`) have no roster entry, and their command line carries no seat name or session directory, so `broadcast.py` `codex_seats()` does not find them. `review.sh` writes `<session dir>/review_status[_<lens>]` (`reviewing` while it runs) and `review_pid[_<lens>]` (its own pid).
- **Coming from model-study** (long-lived seats Phase 1, in flight; model-study, 2026-10-10):
  - Each roster entry gets `role` (`impl`, `test`, `fix` or `review`), written on every dispatch.
  - Long-lived seats Phase 2 adds `lens` (`adversary`, `contract`, `craft`), present only while `role` is `review`.
  - `status: "ended"` is written by `codex_mesh.py end` for one seat and by `stop` for every seat of the run. `done` then means open and idle.
  - `context_tokens` holds the context size at the last turn.
  - A thread that dies without `end` or `stop` keeps its last status, so a pid fallback stays necessary.
- **Codex's own listing is not scriptable.** `codex agents` is an interactive browser. It shows only the sessions on the shared daemon, which does not hold the workers.
- **A script can read the shared daemon** (codex-cli 0.162.0, daemon 0.162.1; probed by the plan author 2026-10-10):
  - It answers on `~/.codex/app-server-control/app-server-control.sock`. On the Mac that path is a symlink into `/private/tmp/codex-daemon-501/`.
  - The protocol is the app-server JSON-RPC over a WebSocket on that Unix socket: an HTTP upgrade, then `initialize`, the `initialized` notification, and calls.
  - `thread/loaded/list` returned 4 ids. `thread/read {threadId}` gave each one's `name`, `cwd`, `status.type` (`idle`, …) and `originator: codex-tui`. All 4 were the user's own Codex windows.
  - `thread/list` returns every saved thread, workers' finished threads included, as `notLoaded`. It does not show what is live.
  - `codex app-server proxy` passes raw bytes through, so the same handshake is needed through it.
  - `codex queue --thread <id or exact name> --message <text>` queues a message for a daemon session.
- **Senders.**
  - `scripts/message/send.py` sends one message: to a Claude session by name, `uds:` or `session:` (through a `claude -p` relay), to a Codex seat with `--codex --session-dir`, to the user with `--to user`, or from the other machine with `--machine HOST`. It keeps what does not arrive.
  - `scripts/production/broadcast.py` sends to groups on this machine (`--all`, `--showrunners`, `--units`, `--agents`), each role with its own text and a closing line saying who was sent the message. `/announce`, `/showrunner:announce`, `/unit:announce` and `/codex_winddown` call it.
  - `scripts/message/top_level.py` lists every live Claude session except this one and the unit directors a showrunner launched, for `/notify_top_level`.
- **Two machines.** natedev and the Mac; `scripts/shutdown/remote.py` `other_machine()` maps each to the other (`mac`, `natedev`). The Mac has no tmux. The Mac's `~/.codex/AGENTS.md` is a regular file with the user's Rust rules (1,395 bytes). natedev has no `~/.codex/AGENTS.md`.
- **Instructions.** `commands/message.md` (`/message`) holds the rules for Claude sessions and names `send.py`. Codex sessions read `~/.codex/AGENTS.md`, seats included.

## Decisions (unit director)

- **One lister, the existing senders.** `scripts/message/roster.py` lists and does not send. `send.py` stays the way to reach one session, and `broadcast.py` the way to reach a group; both take their recipients from the roster. That gives one way to list, one way to send to one session and one way to send to a group, with no second sender to keep in step. One-line revert of the split if the user wants a single script.
- **Native for your own kind, the roster for the other** (user). Claude lists Claude with ListAgents and messages it with SendMessage. Codex lists and messages Codex workers through the roster and `send.py`, since Codex has no scriptable list. The roster lists every kind anyway, because a Codex caller needs Claude rows and a Claude caller needs a worker's unit director and showrunner.
- **The user's own Codex windows are sessions of interest.** They are listed as `freestanding` Codex sessions, `send.py` reaches them through `codex queue`, and `broadcast.py --agents` counts them as other agents. One-line revert.
- **Both machines by default.** Units run on both machines, and the showrunner tells the Mac's unit directors by hand today. `roster.py` lists both machines, and `broadcast.py` reaches both. `--here` limits either to this machine. A machine that does not answer gets one line saying so, and the rest still prints.
- **Roles come from records, never from names.** Showrunner from its timer; unit director from the Units table and tmux mark, or from a run marker; worker from a seats ledger or a mesh roster; everything else is freestanding. A Codex worker's slot role comes from the roster's `role` field once model-study's Phase 1 lands, and reads `role unknown` before then.
- **The message-to-user gate is a required field, not a model's reading** (Phase 2). Every send names its action with `--action` or says `--no-action`, and `pushover.py` refuses one that does neither. A field cannot be skipped by accident, costs no time, and never fails open the way a model call that times out would; tests check it without a network. The sender writes the action because only the sender knows it. One-phase revert to a model review if the user wants judgment on the wording too.
- **Live checks never message a real session.** They use scratch recipients only: a scratch background Claude session, a scratch mesh thread in a scratch session directory, and a scratch daemon thread. All are removed after the check. Listing real sessions is read-only and allowed.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills and scripts. This plan adds the session roster (Phase 1), a gate that makes every message to the user say what the user does (Phase 2), sends to any roster address or name (Phase 3), moves the process table into its own module (Phase 4), sends to roster groups (Phase 5), covers both machines (Phase 6), teaches Claude and Codex sessions which tool to use (Phase 7), and refuses a message to the user that states no action (Phase 8). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-shutdown` on branch `build-followups-shutdown` (unit `shutdown-unit` of production `build-followups`).
- **Project started:** 2026-10-10T16:30:07.804+00:00
- **Stack:** Python 3.13 standard library only; bash, zsh; codex-cli 0.162.0 (daemon 0.162.1).
- **Layout:**
  - `scripts/message/roster.py` — the roster (new, Phase 1; unique addresses Phase 3; both machines Phase 6)
  - `scripts/message/process_table.py` — the process table the roster, `broadcast.py` and wind-down read (Phase 4)
  - `scripts/message/codex_daemon.py` — reads the shared Codex daemon's live sessions; queues to one (new, Phase 1; queue Phase 3)
  - `scripts/notify/pushover.py`, `scripts/notify/user_action.py` — the message-to-user gate and its action types (Phase 2; refusal Phase 8)
  - `scripts/message/send.py` — `--action` / `--no-action` for the user (Phase 2); any roster address or name (Phase 3); the other machine (Phase 6)
  - `scripts/production/broadcast.py` — groups from the roster (Phase 5); both machines (Phase 6)
  - `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py` — instructions (Phase 7)
- **Key files (read, never edited here):** `scripts/agents/codex_mesh.py` (model-study: roster format, `send`), `scripts/delegate/remove_seats.py` (`live_runs`, `LIVE_HEARTBEAT_SECS`, the `seats` ledger), `scripts/delegate/prepare_session.sh` (the run marker), `scripts/production/showrunners.py`, `scripts/production/live_units.py`, `scripts/production/unit_lookup.py`, `scripts/shutdown/remote.py` (`other_machine`).
- **Test lanes:** `scripts/message/test_*.py`, `scripts/production/test_broadcast.py`.
- **Test:** `python3 -m unittest discover -s scripts/message -p 'test_*.py'`; `python3 -m unittest discover -s scripts/production -p 'test_broadcast.py'`.
- **Lint:** `basedpyright scripts/message` and `basedpyright scripts/production` report 0 errors, 0 warnings, 0 notes (exit 3 is the missing `.venv` notice and expected); `bash -n` on changed shell scripts.
- **Invariants:**
  - Tests are hermetic. They use temp directories for sessions, the delegate root and the notifier state, fake `tmux`, `ps`, `ssh` and `codex`, and a fake daemon socket. They never reach the real daemon, a real session, `send.py`'s relay, `pushover.py` or the other machine.
  - Everything an agent reads in the roster is plain words. No pids or ports in the text form; the `--json` form carries what a script needs.
  - A source that cannot be read becomes one line on stderr naming it, and the rest of the roster still prints.

## Phases

### Phase 1 — `roster.py` lists every session of interest on this machine · status: done

#### As-built

- `scripts/message/roster.py` (standard library only) lists every live Claude and Codex session on this machine: `roster(environment) -> Roster` (`entries: list[RosterEntry]`, `problems: list[str]`), `main(argv) -> int`, CLI `[--json] [--kind claude|codex]`.
- `RosterEntry` is a frozen dataclass of 15 `str` fields: `machine`, `kind`, `role` (`showrunner` | `unit director` | `worker` | `freestanding`), `name`, `status`, `address`, `cwd`, `production`, `showrunner`, `showrunner_address`, `unit`, `director`, `director_address`, `slot_role`, `session_dir`. `address` is `session:<id>` (Claude), the seat name (Codex worker), `codex:<thread id>` (daemon session), or empty (blind reviewer).
- Roles come from records, first match wins: showrunner by its registered timer (matched by messaging socket when the registration has one); unit director by the tmux-marked session of a Units row, the row's Worktree = cwd when tmux is unreadable (never a seats-ledger worker), or a run marker in `/tmp/claude/delegate/active/`; worker by a seats-ledger id, a `mesh_roster.json` entry, or a running blind reviewer; else freestanding.
- A live run sits under `/tmp/claude/delegate/` (`ROSTER_DELEGATE_ROOT`) with a live director's marker or a heartbeat younger than `remove_seats.LIVE_HEARTBEAT_SECS`. Codex workers are its non-`ended` mesh entries with a live server or launcher pid; `slot_role` is the record's `role` (`review (<lens>)` with a lens), else `role unknown`. Blind reviewers come from `review_status[_<lens>]` = `reviewing` with a live `review_pid[_<lens>]`, kind from the `codex exec` or `claude` process under it.
- `scripts/message/codex_daemon.py`: `loaded_sessions(socket_path) -> list[DaemonSession] | DaemonUnreadable` reads the daemon socket (`daemon_socket(environment)`: `ROSTER_CODEX_SOCKET`, else `~/.codex/app-server-control/app-server-control.sock`) under one 5 s deadline for the whole exchange (`_OperationDeadline`, `_clock = time.monotonic`). No socket → `[]`; a repeated `nextCursor` or any failure → `DaemonUnreadable(reason)`. A thread that is a Codex worker's is listed once, as the worker.
- Text output nests showrunner → unit director → worker per machine, then `director not running: <session dir>` groups, freestanding Claude, and `your Codex sessions`; `--kind` adds `(context)` parent rows; `--json` prints `{"entries": [...], "problems": [...]}`. Problems go to stderr as `could not read: …`; exit 0 clean, 1 with problems.

**Files:**
- `scripts/message/roster.py` — the roster
- `scripts/message/codex_daemon.py` — read-only client for the Codex daemon's loaded sessions
- `scripts/message/test_roster.py`, `scripts/message/test_codex_daemon.py` — hermetic fixtures (temp session records, fake `ps`/`tmux`, fake daemon socket)
- `pyrightconfig.json` — `scripts/production` and `scripts/delegate` on the `scripts/message` environment

**Binds later work:**
- Nesting, `--kind` context rows and showrunner placement match on `showrunner_address` / `director_address` (`session:<id>`), never names; "`broadcast.py` takes its groups from the roster" groups by the same fields.
- A Codex worker's seat-name address is not unique across runs; "`send.py` reaches any session the roster lists" makes it unique.
- A blind reviewer row (empty `address`, possibly kind `claude`) takes no messages.
- `roster.py` reads the process table through `broadcast.processes()`; "Process listing gets its own module" moves it.
- Consumers print `Roster.problems` (a `DaemonUnreadable` reason included) and continue.

**Gotchas:**
- A Claude seats-ledger id is `agent_bg.sh`'s background id, a prefix of the `sessionId`.
- Session records are `~/.claude/sessions/<pid>.json`, indexed once by `(sessionId, pid)`; the newest `updatedAt` wins.
- Blind reviewers (`codex exec` via `agent_exec.sh`) carry no `--name` or `--session-dir`; only the review status and pid files find them.
- Codex workers read `role unknown` until their mesh records carry `role` and `lens`.
- No running registered showrunner is a correct, common state, not a defect.

**Ruled out:** a session's role from its seat-name suffix — roles come from records only.

### Phase 2 — Every message to the user says what the user does · status: todo

#### Work Order

**Goal:** every sender in this repo says the action the user takes, or that no action is needed, before a message reaches the user's phone, and a message that states its action wrongly is refused with the reason and the exact fix. Direct `pushover.py` calls that state nothing are still sent, with a warning and a log line naming their source, until the refusal phase. Emergency priority is kept for messages the user must act on now.

**Spec:**
- **The action is a type, not a string.** `scripts/notify/user_action.py` (new, standard library only) holds it, and `pushover.py`, `send.py` and `escalate.py` all use it:
  - `ActionRequired(text: str)` and `NoActionRequired()`, frozen dataclasses; `UserAction = ActionRequired | NoActionRequired`.
  - `ActionUnstated()`: neither flag was given. `ActionRefused(reason: str)`: the flags were given wrongly.
  - `parse_user_action(action: str | None, no_action: bool) -> UserAction | ActionUnstated | ActionRefused`. Both flags is refused. `--action` text that is empty, or only a placeholder (`none`, `n/a`, `nothing`, `no action`, `-`, any case), is refused: use `--no-action`.
  - `refused_at(action: UserAction, priority: int) -> ActionRefused | None`: `NoActionRequired` above priority 0 is refused. Priority 1 (high) and 2 (emergency, repeats until acknowledged) need `ActionRequired`; emergency is for an action the user must take now, and the docstring says so.
  - `first_line(action: UserAction) -> str`: `Action: <text>` or `No action needed.`
  - `FIX`: `Say what the user does with --action "<what they do>", or pass --no-action when nothing is needed (priority 0 only).`
- **The gate lives in `scripts/notify/pushover.py`**, the one channel every path ends in: `send.py --to user`, `escalate.py`, `/alert_user`, and seats or nix scripts that call `pushover.py` directly.
  - Usage becomes `pushover.py [--priority 0|1|2] [--source NAME] (--action TEXT | --no-action) TITLE MESSAGE`.
  - **`ActionUnstated` is still sent in this phase**, with the stderr line `pushover: warning: no --action or --no-action; a message that states neither will be refused. <FIX>` and log outcome `sent without an action line`. Two senders outside this repo (`/etc/nixos/modules/linux/ups.nix`, `disk-floor.nix`) call `pushover.py` from the nix store and change only when the user rebuilds; the UPS alert must never be refused. The refusal phase turns this case into a refusal once natedev confirms the rebuilt generation passes the flags.
  - **`ActionRefused`, and a `refused_at` result, refuse.** Nothing is sent. Exit 2, one stderr line `pushover: refused before sending: <reason>. <FIX>`, and the attempt is logged with outcome `refused before sending: <reason>`.
  - The posted message's first line is `first_line(action)`, then the message. When the whole is over `MESSAGE_MAX`, the message body is cut, never the action line.
  - **Every log entry names its source**: `--source NAME` when given, else the parent process's command line (first 80 characters, read with `ps -o args= -p <ppid>`), so a direct caller that passes nothing is still identifiable in the refusal phase's audit.
- **`scripts/message/send.py --to user`** takes the same `--action TEXT` or `--no-action`, one of them required: `ActionUnstated` or `ActionRefused` is a usage error (exit 2) with `FIX` in its text. `--need decision` and `--need blocked` require `ActionRequired`; `--need note` takes either.
  - Its options carry `action: UserAction`, never `str | None` and a boolean.
  - It passes the action and `--source <its --from>` to `pushover.py`, and `remote()` forwards the action to the other machine.
  - Its `Outcome` gains `refused`: `pushover.py` exit 2 prints `REFUSED: <reason>` and exits 2, not `FAILED` (exit 3). Nothing is kept, and the sender's PushNotification fallback does not fire for it. The module docstring's outcome table gains the line.
- **`scripts/message/escalate.py`** stores the action with the held message.
  - CLI: `hold` takes `--action TEXT` or `--no-action`, one required, as `send.py` does.
  - Python: `hold(key, summary, text, action: UserAction, minutes: int = 15, need: Need = "decision") -> bool`. `NoActionRequired` with a `need` other than `note` raises `ValueError`, a caller's bug that its tests catch.
  - The record gains `action`: `{"kind": "required", "text": <text>}` or `{"kind": "none"}`. `read(path)` returns `HeldMessage | LegacyHeld | UnreadableHeld | NoHeld` in place of `Held | None`: `LegacyHeld` is a readable record with no `action` (held before this phase).
  - A `LegacyHeld` record is never delivered; `list` shows it as `waiting for a hold that states its action`. `hold()` replaces a `LegacyHeld` record and returns true; a held `HeldMessage` still returns false.
  - `deliver()` passes the stored action to `send.py`.
- **Every sender in this repo states its action.** Each one passes `--action` with the action its message already names, or `--no-action` when the message asks nothing of the user. A `--no-action` sender sends at `--need note`. The writer lists each sender's choice in its summary:
  - `scripts/buildlog/rust_release.py` (a release note);
  - `scripts/lint/sweep.py` (the disk-floor alert);
  - `scripts/production/ci_points.py` (the review watch);
  - `scripts/production/codex_winddown.py` (its text names the action: reset Codex, then `/codex_winddown clear`);
  - `scripts/shutdown/settle.py` and `scripts/shutdown/restart.py` (an account is down: the `/shutdown restart` action; an account is back: no action);
  - `scripts/whoami/five_hour.py` (`escalate.hold` with `NoActionRequired()` and `need="note"`: the 5-hour news asks nothing of the user).
- **Instructions.** `commands/alert_user.md`, `commands/builds.md`, `commands/fix.md`, `commands/watcher.md` and `commands/showrunner/produce.md` show `--action` or `--no-action` in every command. Each says: every message says what the user does or that nothing is needed; emergency (`--need blocked`) only when the user must act now. The PushNotification fallback on `FAILED` carries the same `Action:` or `No action needed.` first line. In `produce.md`, edit only inside `<Notify/>` (natedev, 2026-10-10: showrunner-fixer is editing its StartUpdates).
- **Imports.** `send.py` and `escalate.py` put `scripts/notify` on `sys.path` the way `five_hour.py` puts `scripts/message` there. `pyrightconfig.json`: every environment whose `extraPaths` holds `scripts/message` also gets `scripts/notify`.
- **Tests never push.** Every test fakes the post or the subprocess, and no test reads the real keys file. Named cases: each `parse_user_action` and `refused_at` outcome; the cut keeps the action line; the log names `--source` and, without it, the parent command; `send.py` `REFUSED` exit 2 keeps nothing; a `LegacyHeld` record is replaced by the next `hold` and never delivered; a held current record is not replaced; `five_hour` holds with `NoActionRequired` at `note`.

**Files:**
- `scripts/notify/user_action.py` — new: the action types and checks.
- `scripts/notify/pushover.py`, `scripts/notify/test_pushover.py`
- `scripts/message/send.py`, `scripts/message/test_send.py`
- `scripts/message/escalate.py`, `scripts/message/test_escalate.py`
- `scripts/whoami/five_hour.py`, `scripts/whoami/test_five_hour.py` — test new.
- `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`
- `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`, `scripts/production/test_dailies_input.py`
- `scripts/production/codex_winddown.py`, `scripts/production/test_codex_winddown.py`
- `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py`
- `commands/alert_user.md`, `commands/builds.md`, `commands/fix.md`, `commands/watcher.md`, `commands/showrunner/produce.md` — `produce.md` inside `<Notify/>` only.
- `pyrightconfig.json` — `scripts/notify` on the environments that reach `send.py` or `escalate.py`.

**Seats:** `2 writers` — the gate and its users split from the senders that adopt it.
- `impl` — `user_action.py`, `pushover.py`, `send.py`, `escalate.py`, `pyrightconfig.json` and their tests; hub: `user_action.py` (the action types every sender passes).
- `test` — opens as `impl`; every sender migration (`rust_release.py`, `sweep.py`, `ci_points.py`, `codex_winddown.py`, `settle.py`, `restart.py`, `five_hour.py`) with its tests, and every command file.

**Constraints from prior phases:** none from the roster; this phase touches no roster file. Later phases change `send.py` again (any roster name, then the other machine); they keep this phase's `UserAction` options and `refused` outcome.

**Acceptance gate:** every test line green: `python3 -m unittest discover -s <dir> -p 'test_*.py'` for `scripts/notify`, `scripts/message`, `scripts/shutdown`, and `-p` for `test_five_hour.py` (`scripts/whoami`), `test_rust_release.py` (`scripts/buildlog`), `test_sweep.py` (`scripts/lint`), `test_ci_points.py`, `test_codex_winddown.py` and `test_dailies_input.py` (`scripts/production`). basedpyright on every changed directory reports 0/0/0. Live, with nothing sent to the phone: `--priority 2 --no-action` and `--action none` each exit 2 with the reason and log a `refused before sending` line naming its source; `send.py --to user --need blocked --no-action …` exits 2.

### Phase 3 — `send.py` reaches any session the roster lists · status: todo

#### Work Order

**Goal:** `send.py --to <address or name>` delivers to any roster row that takes messages: a Codex worker by its unique address or its seat name, a user's Codex session by `codex:<thread id>` or exact name, and a Claude session as today. A row that cannot take messages says so and sends nothing.

**Spec:**
- **Every roster address is unique on both machines** (`scripts/message/roster.py`):
  - Claude session: `session:<session id>`; daemon session: `codex:<thread id>` (unchanged).
  - Codex worker: `seat:<run id>/<seat name>`, where the run id is the name of the run's session directory (a UUID). Two live runs with one seat name now have two addresses.
  - In memory, `RosterEntry.address: str` becomes `RosterEntry.reach: Messageable | CannotReceiveMessages`: `Messageable(address: str)`, `CannotReceiveMessages(reason: str)`. A blind reviewer is `CannotReceiveMessages("blind reviewer")`. `--json` keeps the `address` key as its serialization (empty for `CannotReceiveMessages`) and adds no key; the text form is unchanged.
- **Resolution in `send.py`**, before its Claude path, by the form of `--to`:
  1. `user` → the user path (Phase 2). `uds:` and `session:` → the Claude path, unchanged.
  2. `seat:<run id>/<seat name>` → the roster row with that address; sent through the existing `codex()` path with that row's `session_dir`, so `--codex --session-dir` is no longer needed. Both are still accepted, and they skip the lookup.
  3. `codex:<thread id>` → `codex_daemon.queue`, no roster read.
  4. A bare name → the roster rows whose `name` equals it:
     - one `Messageable` row: sent by its address, as above;
     - one `CannotReceiveMessages` row: exit 2, `send.py: <name> cannot take messages (<reason>); contact its unit director <director> or wait`, with no delivery attempt;
     - more than one row: exit 2, `send.py: <name> matches <n> sessions:` followed by each one's address and role, and nothing sent;
     - none: the Claude path, unchanged.
- **The roster is read once per send**, and only for forms 2 and 4. Each roster problem prints to stderr as `send.py: roster: <problem>`, and resolution continues on the rows that were read. A roster that cannot be read at all leaves the Claude path as it is today; a `seat:` address then fails with the roster's problem line.
- **`codex_daemon.queue(thread_id: str, text: str, timeout: float = QUEUE_TIMEOUT_SECONDS) -> Queued | QueueFailed | QueueTimedOut`**, `QUEUE_TIMEOUT_SECONDS = 30`. It runs `codex queue --thread <id> --message <text>` with that timeout. `Queued()` on exit 0; `QueueFailed(reason)` with codex's first stderr line, or `exit <n>`; `QueueTimedOut(seconds)`. `send.py` prints `SENT: queued on Codex session <name or id>` (exit 0) for `Queued`, and `FAILED: <reason>` (exit 3) for the other two; nothing is kept, as for a seat. The module docstring says it reads the shared daemon's live sessions and queues one message to one of them.
- The docstring and `--help` name the address forms.
- **Tests** fake `codex` and `codex_mesh.py` the way `test_send.py` already fakes its subprocesses, and build roster fixtures as `test_roster.py` does. Named cases: two live runs with one seat name, each reached by its `seat:` address, and the bare name ambiguous (exit 2, both addresses listed); a blind reviewer by name exits 2 with no subprocess run; a roster problem line printed while a send to a usable row succeeds; `queue` success, nonzero exit and timeout; `roster.py --json` writes `address` empty for a reviewer.

**Files:**
- `scripts/message/roster.py` — `seat:` addresses, `reach`.
- `scripts/message/test_roster.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/message/codex_daemon.py` — `queue`.
- `scripts/message/test_codex_daemon.py`

**Seats:** `2 writers` — addressing and sending split from the daemon queue.
- `impl` — `roster.py`, `send.py`, `test_roster.py`, `test_send.py`; hub: `roster.py` (the address forms and `reach`).
- `test` — opens as `impl`; `codex_daemon.queue` and `test_codex_daemon.py`; hub: `codex_daemon.py` (the queue result types `send.py` matches on).

**Constraints from prior phases:**
- Phase 1's `roster(environment: Mapping[str, str]) -> Roster` (`entries`, `problems`) and `main(argv) -> int`. `RosterEntry` has 15 string fields: `machine`, `kind`, `role`, `name`, `status`, `address`, `cwd`, `production`, `showrunner`, `showrunner_address`, `unit`, `director`, `director_address`, `slot_role`, `session_dir`. `showrunner_address` and `director_address` are `session:<id>` addresses and are what nesting and `--kind` context match on; names are display only.
- A Codex worker's `address` is its seat name today, and a blind reviewer's is empty; this phase replaces both.
- `codex_daemon.loaded_sessions(socket_path) -> list[DaemonSession] | DaemonUnreadable` reads under one 5 s deadline for the whole read (`_OperationDeadline`, module clock `_clock = time.monotonic`); `daemon_socket(environment)` gives the socket path. `queue` is a subprocess call and takes its own timeout.
- Phase 2's `send.py` options carry `action: UserAction`, and its `refused` outcome is exit 2; the user path is unchanged here.

**Acceptance gate:** the Test and Lint lines green. Live, scratch recipients only:
- a scratch mesh thread in a scratch session directory gets `send.py --to seat:<run id>/<seat name>`, and again by its bare seat name, without `--session-dir`;
- a scratch daemon thread (started with `thread/start`, `ephemeral: true`, over the socket, then archived) gets `send.py --to codex:<id>` and shows the queued message in `thread/read` or its queue;
- an ambiguous name exits 2.

### Phase 4 — Process listing gets its own module · status: todo

#### Work Order

**Goal:** the process table that the roster, `broadcast.py` and Codex wind-down read comes from one module that imports none of them, so `broadcast.py` can read the roster next without an import cycle and wind-down keeps working.

**Spec:**
- `scripts/message/process_table.py` (new, standard library only) holds what `scripts/production/broadcast.py` defines today, moved without a change in behavior:
  - `Process(pid, parent, command, arguments)`, a `NamedTuple`;
  - `snapshot() -> dict[int, Process]` (today's `processes()`), running `ps -eo pid=,ppid=,comm=,args=`, overridable by `PROCESS_TABLE_PS` (replacing `BROADCAST_PS`);
  - `is_codex_agent(process: Process) -> bool`, unchanged.
- `roster.py` imports `process_table` and no longer imports `broadcast`.
- `broadcast.py` imports `Process`, `snapshot` and `is_codex_agent` from `process_table` and deletes its own copies. Its recipient code is otherwise unchanged; the next phase replaces it.
- `codex_winddown.py` reads the process table through `process_table`, and its pid-to-name map from `sessions.live_sessions()` (`{record["pid"]: record["name"] for record in sessions.live_sessions() if record["name"]}`) in place of `broadcast.live_sessions()`. It still sends through `broadcast.send`.
- Every test that set `BROADCAST_PS` sets `PROCESS_TABLE_PS`. `is_codex_agent`'s cases move to `scripts/message/test_process_table.py`.

**Files:**
- `scripts/message/process_table.py` — new.
- `scripts/message/test_process_table.py` — new.
- `scripts/message/roster.py`
- `scripts/message/test_roster.py`
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/production/codex_winddown.py`
- `scripts/production/test_codex_winddown.py`

**Seats:** `2 writers` — the roster side split from the production side.
- `impl` — `process_table.py`, `test_process_table.py`, `roster.py`, `test_roster.py`; hub: `process_table.py` (the one process table every caller reads).
- `test` — opens as `impl`; `broadcast.py`, `codex_winddown.py` and their tests.

**Constraints from prior phases:**
- `roster.py` reads the process table in `_process_cwds`, `_process_words`, the ancestry walk for blind reviewers and the worker liveness check, all typed on `broadcast.Process`; Phase 3 left these unchanged.
- `codex_winddown.py` uses `broadcast.live_sessions()`, `broadcast.processes()`, `broadcast.is_codex_agent()`, `broadcast.send` and `broadcast.MESSAGE`; Phase 2 changed only its user message's flags.
- `sessions.live_sessions() -> list[SessionRecord]` returns every live session, newest record first.

**Acceptance gate:** the Test and Lint lines green, `python3 -m unittest discover -s scripts/production -p 'test_codex_winddown.py'` included. `grep -n "import broadcast" scripts/message/roster.py` prints nothing. Live read-only: `roster.py` lists the same sessions as before the phase.

### Phase 5 — `broadcast.py` takes its groups from the roster · status: todo

#### Work Order

**Goal:** every group send reaches the roster's members of that group, Claude and Codex, and two new groups exist: workers (all, or one unit's) and top level.

**Spec:**
- `recipients()` reads `roster.roster()` in place of its own session, unit and `codex_seats` code, which is removed, `live_sessions()` included.
- **Membership comes from identity, never from names.** A unit's workers are the rows whose `director_address` is that unit director's address; a showrunner's units are the rows whose `showrunner_address` is that showrunner's. The sender is left out by identity: the row whose address is `session:$CLAUDE_CODE_SESSION_ID`, and no other row that happens to share its name.
- Groups:
  - `--showrunners`: role showrunner.
  - `--units`: role unit director.
  - `--agents`: every worker and every freestanding session, Claude and Codex, the user's Codex sessions included.
  - `--workers TEXT`: every worker. With `--of <unit, unit director name, or session: address>` placed before it, only that unit's workers. An unknown unit is a usage error naming the known ones; a name matching more than one unit director is a usage error listing each one's address.
  - `--top-level TEXT`: every Claude session that is not a unit director under a showrunner and not a worker, matching `top_level.py`'s set.
  - `--all`: every role. A role's own flag after `--all` still gives that role its own text.
- Every recipient is sent through `send.py --to <address>`. A `CannotReceiveMessages` row (a blind reviewer) is reported `<name>: cannot take messages; contact its unit director <director> or wait`, is not sent, and is not counted as a failure.
- Each roster problem prints as `broadcast: roster: <problem>`, and the send continues to the rows that were read.
- The closing line still says who was sent the message, using the new role names.
- **`top_level.py`** takes its set from `roster.roster()`: Claude rows that are not unit directors under a showrunner, not workers, and not this session by `session:` identity. Its output stays `<name>\tuds:<socket path>`, the socket path read from that session's record through `sessions.py`, because `/notify_top_level` hands it to SendMessage. The `build_hold.py record-recipient --session-id <id> --name <name>` call takes the id from the row's `session:` address. Two live sessions sharing a name are two lines. A row whose record has no messaging socket prints the `not reachable:` line, as today.
- **Tests** (`test_broadcast.py`, `test_top_level.py`) fake the roster or build its fixtures. Named cases: two workers sharing a seat name under different directors, each sent once to its own unit by `--of`; `--of` matching two directors is a usage error; a session sharing the sender's name is still sent; a reviewer reported and not counted; a roster problem printed while the rest sends; `top_level.py` two same-name sessions, two lines and two `record-recipient` calls.

**Files:**
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/message/top_level.py`
- `scripts/message/test_top_level.py`

**Seats:** `2 writers` — group sending split from the top-level list.
- `impl` — `broadcast.py`, `test_broadcast.py`; hub: `broadcast.py` (recipient and group assembly).
- `test` — opens as `impl`; `top_level.py`, `test_top_level.py`.

**Constraints from prior phases:**
- Phase 1's `RosterEntry` fields and `roster(environment) -> Roster` (`entries`, `problems`); `showrunner_address` and `director_address` are `session:<id>`.
- Phase 3's addresses: `session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`, each unique; `RosterEntry.reach: Messageable(address) | CannotReceiveMessages(reason)`; `send.py --to <address>` reaches each, and exits 2 for a `CannotReceiveMessages` name or an ambiguous name.
- Phase 4's `process_table` module: `roster.py` no longer imports `broadcast`, so `broadcast.py` may import `roster`. `codex_winddown.py` still uses `broadcast.send` and `broadcast.MESSAGE`, which stay.
- `/notify_top_level` reads `top_level.py`'s `uds:` addresses and hands them to SendMessage.

**Acceptance gate:** the Test and Lint lines green, `test_top_level.py` included. A live dry check (`BROADCAST_SEND` pointed at a script that records its arguments) lists the recipients for each flag on natedev, with no message sent, and each flag's list equals the rows `roster.py --json` gives for that group at that moment. A group with no members (no showrunner registered, for one) lists none and says so.

### Phase 6 — Both machines · status: todo

#### Work Order

**Goal:** `roster.py` lists natedev and the Mac in one run; `send.py` reaches any address or name found only on the other machine; `broadcast.py` reaches both machines, each recipient once.

**Spec:**
- **Scope is named.** `RosterScope = ThisMachine | AllMachines` (frozen dataclasses in `roster.py`); `roster(environment, scope: RosterScope) -> Roster`, with `scope` required.
  - `roster.py` (the command) reads `AllMachines`; `--here` reads `ThisMachine`.
  - `broadcast.py` and `top_level.py` read `ThisMachine`: `broadcast.py` reaches the other machine by running itself there, and `top_level.py` lists this machine only (`/notify_top_level` adds other-machine peers from ListAgents).
  - `send.py` reads `ThisMachine` first and the other machine only when nothing here matches.
- **`AllMachines`** runs `roster.py --here --json` on `remote.other_machine()` over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --json'`, in parallel with the local read, and merges the entries, this machine first.
  - ssh failure, a timeout (20 s) or unparseable JSON is one problem line, `<host>: not reached: <why>`, and exit 1.
  - The other machine's own problems come through, prefixed by its name.
- **Routing in `send.py`.** Every address form is unique on both machines (Phase 3). An address or a bare name that matches no row here is looked up in the other machine's roster; exactly one row there sends through the existing `--machine <host>` path with that row's address. Rows on both machines are Phase 3's ambiguity. A `CannotReceiveMessages` row there gives Phase 3's `cannot take messages` line.
- **`broadcast.py`** runs `broadcast.py --here` on the other machine over ssh with the same flags and texts, and prints its lines under the host's name. The closing line names both machines. `--here` keeps it to this machine and runs no ssh. An unreachable machine is one `NOT sent` line for that machine.
- The Mac has no tmux: there, unit directors come from the worktree match, and the tmux problem line is left out on a machine with no `tmux` binary.
- **Tests** fake `ssh`, recording each call. Named cases: a Claude session, a mesh worker and a daemon session each found only on the other machine are sent through `--machine` with their address; one delivery per recipient when both machines answer; `--here` on `roster.py`, `broadcast.py` and `send.py` makes no ssh call; `top_level.py` makes no ssh call; an unreachable machine is one problem line and the local rows still print.

**Files:**
- `scripts/message/roster.py`
- `scripts/message/test_roster.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/message/top_level.py` — passes `ThisMachine`.
- `scripts/message/test_top_level.py`

**Seats:** `2 writers` — the roster and single sends split from the group senders.
- `impl` — `roster.py`, `send.py` and their tests; hub: `roster.py` (`RosterScope` and the aggregate read).
- `test` — opens as `impl`; `broadcast.py`, `top_level.py` and their tests.

**Constraints from prior phases:**
- Phase 3's addresses (`session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`), `RosterEntry.reach`, and `send.py`'s resolution order and exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, or a refusal, 3 failed.
- Phase 4's `process_table` module; `roster.py` does not import `broadcast`.
- Phase 5's groups (`--showrunners`, `--units`, `--agents`, `--workers` with `--of`, `--top-level`, `--all`), membership by `director_address` and `showrunner_address`, the sender left out by `session:` identity, roster problems printed while the rest sends, and `top_level.py`'s `<name>\tuds:<socket path>` lines.
- `scripts/shutdown/remote.py` `other_machine()` maps `natedev` and `mac` to each other.

**Acceptance gate:** the Test and Lint lines green. Live: `roster.py` on natedev lists the Mac's sessions; `roster.py --here` on the Mac exits 0 or 1 with no ssh. `broadcast.py`'s dry check covers both machines, each recipient once. One scratch background Claude session on the Mac receives `send.py --to <its name>` from natedev and is then removed.

### Phase 7 — Claude and Codex sessions know which tool to use · status: todo

#### Work Order

**Goal:** a Claude session reads in `/message` when to use ListAgents and SendMessage and when to use the roster. Every Codex session on both machines reads in its `AGENTS.md` how to list and message any session, and how to message the user.

**Spec:**
- `commands/message.md` gains a short "Finding sessions" section:
  - ListAgents and SendMessage for Claude sessions;
  - `roster.py` for Codex workers, for who is whose unit director and showrunner, and for the other machine;
  - `send.py --to <address>` for anything the roster lists;
  - `broadcast.py` and its groups for many at once.

  The Sending bullet's `--codex --session-dir` wording becomes "by its `seat:` address or seat name".
- `commands/announce.md`, `commands/unit/announce.md` and `commands/showrunner/announce.md` say the message reaches both machines and Codex workers. `commands/notify_top_level.md` keeps its steps and its `uds:` wording; Phase 5 kept `top_level.py`'s output.
- `codex/AGENTS-messaging.md` (new, in this repo) is the Codex text, short:
  - list every session with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py`;
  - message one with `send.py --to <address> --from <your session name>`, a group with `broadcast.py`;
  - message the user with `send.py --to user --summary … --text …` and `--action "<what the user does>"` or `--no-action`; emergency only when the user must act now;
  - the first-line, context and content-not-approval rules from `/message`;
  - the roster needs network for the other machine and a socket for the daemon, so a sandboxed command that fails is rerun outside the sandbox the way the user's AGENTS.md rules say.
- `scripts/message/install_codex_agents.py [--machine HOST]` writes that text into `~/.codex/AGENTS.md` between `<!-- claude-messaging: begin -->` and `<!-- claude-messaging: end -->`.
  - It creates the file when absent, and replaces only the block when present.
  - Everything outside the block is kept byte for byte, so the Mac's Rust rules stay.
  - Running it twice changes nothing.
  - It is run once on natedev and once with `--machine mac`.

**Files:**
- `commands/message.md`
- `commands/announce.md`
- `commands/unit/announce.md`
- `commands/showrunner/announce.md`
- `commands/notify_top_level.md`
- `codex/AGENTS-messaging.md` — new.
- `scripts/message/install_codex_agents.py` — new.
- `scripts/message/test_install_codex_agents.py` — new.

**Seats:** `2 writers` — the installer split from the text it installs.
- `impl` — `install_codex_agents.py` and `test_install_codex_agents.py`.
- `test` — opens as `impl`; every command file and `codex/AGENTS-messaging.md`; hub: `codex/AGENTS-messaging.md` (the text the installer writes).

**Constraints from prior phases:**
- Address forms: `session:<id>` (Claude), `seat:<run id>/<seat name>` (Codex worker), `codex:<thread id>` (the user's Codex sessions), or a bare name; a name matching several rows exits 2 and lists their addresses.
- `send.py` exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, or a refusal (`REFUSED:`), 3 failed. `--to user` needs `--action TEXT` or `--no-action`; `--need decision` and `--need blocked` need `--action`.
- `broadcast.py` groups: `--showrunners`, `--units`, `--agents`, `--workers` (with `--of <unit>` before it), `--top-level`, `--all`; both machines unless `--here`.
- `roster.py` lists both machines unless `--here`; a machine not reached is one stderr line and exit 1; `--json` gives `{"entries": [...], "problems": [...]}`.

**Acceptance gate:** the Test and Lint lines green; the block installed on both machines, with the Mac's Rust rules unchanged (`diff` of the text outside the block is empty). Live: a scratch Codex daemon thread on natedev, asked to list every Claude session and its role, runs `roster.py`, and its answer matches `roster.py --json` at that moment, saying so when no showrunner is registered. Then it is archived.

### Phase 8 — A message with no action stated is refused · status: todo

**Blocked by:** natedev confirming that the rebuilt `/etc/nixos` generation's `ups.nix` and `disk-floor.nix` pass `--action` or `--no-action` to `pushover.py` (natedev makes the edit; the rebuild is the user's). Resequence earlier the moment natedev confirms.

#### Work Order

**Goal:** a message to the user that says neither its action nor that none is needed does not leave: Phase 2's transition warning becomes the refusal.

**Spec:**
- `pushover.py` refuses `ActionUnstated` like Phase 2's other cases: nothing sent, exit 2, the stderr line `pushover: refused before sending: no action stated. <FIX>`, and log outcome `refused before sending: no action stated`. The warning line and the `sent without an action line` outcome are removed.
- Before dispatch, the unit director reads the pushover log since the Phase 2 checkpoint for `sent without an action line` entries. Each entry names its source; any source still in that list is updated in this phase when it is in this repo, or named to natedev when it is not.

**Files:**
- `scripts/notify/pushover.py`
- `scripts/notify/test_pushover.py`

**Seats:** `1 writer + 1 tester` — nothing splits.
- `impl` — `pushover.py`.
- `test` — `test_pushover.py`, from this Spec alone.

**Constraints from prior phases:** Phase 2's `scripts/notify/user_action.py` (`parse_user_action` → `UserAction | ActionUnstated | ActionRefused`, `refused_at`, `first_line`, `FIX`), `pushover.py`'s `--source` and its log entries naming their source, and its refusal line and exit 2. `send.py --to user` and `escalate.py hold` already treat `ActionUnstated` as a usage error, so neither changes here.

**Acceptance gate:** `python3 -m unittest discover -s scripts/notify -p 'test_*.py'` green; basedpyright `scripts/notify` 0/0/0; live: `pushover.py "t" "m"` exits 2 and logs the refusal with its source, with nothing sent.
