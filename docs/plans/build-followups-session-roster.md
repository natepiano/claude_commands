# Session roster: list and message every session, Claude and Codex

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** Adds one script that any Claude or Codex session can run. It lists every session of interest: Claude showrunners, unit directors, workers and freestanding sessions with their roles, and Codex workers across every unit with their unit director and showrunner, plus the user's own Codex sessions. The existing senders then reach any of those sessions by name, singly or by group, on both machines.

> **As-built disposition: create**

> **Production: build-followups** — unit `shutdown-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-10 09:0x PDT: "claude can do a listagents so claude can just do it - we need an ability for claude to simply list via a script all of the workers and who is their unit director and showrunner, and also for any codex session to list all the claude sessions (unit, showrunner, freestanding - and their roles if possible) i assume codex can list their own sessions but find out - you udnerstand i hoep - i'm looking for the ability to extend our messaging infra to simply list all sessions of interest across agents and send messages to all types we've already defined or to individauls within and across agents"

The user, via natedev, a few minutes later: "it's fine if our skills instruct agents how to do different activities -- what i mean is if a codex needs to list other codex - it can do that itself and if it needs to talk to other claudes, it can use our script / and vice versa, right?"

natedev, 2026-10-10: each kind of agent lists its own kind natively and uses the script for the other kind, and the skills say which to use. Claude keeps ListAgents for Claude sessions. The gap to cover: Codex has no native list it can run from a script across units, so the script lists Codex workers across units for Claude and Codex alike. Boundary: model-study owns `scripts/agents/codex_mesh.py` and the seat launcher (`docs/as-built/long-lived-seats.md`). Read their records, never edit them, and ask model-study for any field.

natedev, 2026-10-10 09:5x PDT, inserting Phase 2 (urgent; packaging is natedev's), with the user's words relayed by hana: "Any message in the message infra being sent to a user should go through a message review to make sure that it tells the user the action he needs to take or that there is no action required. Send this to natedev session to address in the messaging infra." Trigger, 09:38 PDT: a widget seat sent an Emergency-priority Pushover, 'widget-enhancements-impl: Mac test paused. The shared Mac gate needs five minutes with no local input and no user Hana process; all other implementation gates have passed.' The user asked how he was supposed to respond; no action was needed. natedev: the gate belongs in the infra, not per sender; every path to the user is `scripts/notify/pushover.py` (called directly, seats included), `send.py --to user` and `commands/alert_user.md`; a message that says neither the action nor that none is needed is refused back to its sender with the reason; Emergency only when the user must act now; `pushover.py` and `alert_user.md` join shutdown-unit's Owns; tests never push.

## What exists today (checked 2026-10-10 by the plan author)

- **Claude sessions.** `~/.claude/sessions/<pid>.json` holds `pid`, `procStart`, `sessionId`, `name`, `cwd`, `kind`, `status` (`busy`, `idle`, `shell`), `messagingSocketPath`, `tmux` and `formerNames`. `scripts/message/sessions.py` reads and validates these records (`read_session`, `live_session`, `live_sessions`, `addressed`).
- **Claude roles are written down in four places, and nothing joins them:**
  - Showrunners: the update timers under `~/.local/state/notifier/showrunner-*`. `scripts/production/showrunners.py` `registered_showrunners()` gives each one's session name, slug and production doc.
  - Unit directors under a production: the doc's Units table, plus the tmux session marked `SHOWRUNNER_UNIT` / production. `scripts/production/live_units.py` `production_units(doc)` and `unit_lookup.marked_units(slug)` read these. tmux runs only on natedev.
  - Any `/unit:direct` run: `prepare_session.sh` writes `/tmp/claude/delegate/active/<director's Claude session id>`, holding the run's session directory. `end_session.sh` removes it. A marker from a director that died stays behind; 12 markers exist today, some from 2026-10-06.
  - Claude workers: every `agent_bg.sh` launch appends `<session id>\t<name>` to its run's `seats` ledger. `scripts/delegate/remove_seats.py` already decides which runs are live (`live_runs`: a marker whose director is still listed, or a `heartbeat.log` written in the last `LIVE_HEARTBEAT_SECS` = 600).
- **Codex workers** are threads on one app-server per run. `<session dir>/mesh_roster.json` maps each seat name to `thread_id`, `status` (`starting`, `running`, `done`, `failed`, `waiting_capacity`, `capacity_exhausted`), `turn_id`, `launcher_pid` and `port`. `mesh_server.json` holds the server's `pid` and `port`. `codex_mesh.py list --session-dir` prints one run's roster only. Seat names are `<worktree>-<slot>`, from `scripts/delegate/seat_name.sh`. Inside a `/unit:direct` run, reviews go to open Codex seats (`review.sh --to <seat>` or `--slot <name>`), which are roster entries. A review outside a run still runs a blind reviewer (`codex exec`, launched by `review.sh` through `agent_exec.sh`); such a reviewer has no roster entry, and their command line carries no seat name or session directory, so `broadcast.py` `codex_seats()` does not find them. `review.sh` writes `<session dir>/review_status[_<lens>]` (`reviewing` while it runs) and `review_pid[_<lens>]` (its own pid).
- **From model-study** (long-lived seats, shipped; `docs/as-built/long-lived-seats.md`):
  - Each roster entry gets `role` (`impl`, `test`, `fix` or `review`), written on every dispatch.
  - No launcher writes `lens` into the mesh roster; the lens is recorded in review artifact names and in the progress and findings records.
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
- **The refusal runs right after Phase 3** (2026-10-10). natedev's generation 206 (`/etc/nixos` 9ef892a) passes `--action` or `--no-action` from `ups.nix` and `disk-floor.nix`, checked against the running system, so the phase that refuses a message with no action stated moved up from last to Phase 4, per its own resequence rule. Packaging only; one-line revert.
- **Live checks never message a real session.** They use scratch recipients only: a scratch background Claude session, a scratch mesh thread in a scratch session directory, and a scratch daemon thread. All are removed after the check. Listing real sessions is read-only and allowed.
- **Shutdown tests can never reach the user's phone** (2026-10-10). The user approved a guard after two fixture alerts reached their phone from a test whose fake had gone stale; it runs as Phase 6, right after the process-table move.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills and scripts. This plan adds the session roster (Phase 1), a gate that makes every message to the user say what the user does (Phase 2), sends to any roster address or name (Phase 3), refuses a message to the user that states no action (Phase 4), moves the process table into its own module (Phase 5), makes a shutdown test that reaches a real send fail instead of sending (Phase 6), sends to roster groups (Phase 7), covers both machines (Phase 8), and teaches Claude and Codex sessions which tool to use (Phase 9). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-shutdown` on branch `build-followups-shutdown` (unit `shutdown-unit` of production `build-followups`).
- **Project started:** 2026-10-10T16:30:07.804+00:00
- **Stack:** Python 3.13 standard library only; bash, zsh; codex-cli 0.162.0 (daemon 0.162.1).
- **Layout:**
  - `scripts/message/roster.py` — the roster (new, Phase 1; unique addresses Phase 3; both machines Phase 8)
  - `scripts/message/process_table.py` — the process table the roster, `broadcast.py` and wind-down read (Phase 5)
  - `scripts/message/codex_daemon.py` — reads the shared Codex daemon's live sessions; queues to one (new, Phase 1; queue Phase 3)
  - `scripts/notify/pushover.py`, `scripts/notify/user_action.py` — the message-to-user gate and its action types (Phase 2; refusal Phase 4)
  - `scripts/message/send.py` — `--action` / `--no-action` for the user (Phase 2); any roster address or name (Phase 3); the other machine (Phase 8)
  - `scripts/production/broadcast.py` — groups from the roster (Phase 7); both machines (Phase 8)
  - `scripts/shutdown/test_support.py` — the base every shutdown test derives from; stubs every real send leaf (new, Phase 6)
  - `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py` — instructions (Phase 9)
- **Key files (read, never edited here):** `scripts/agents/codex_mesh.py` (model-study: roster format, `send`), `scripts/delegate/remove_seats.py` (`live_runs`, `LIVE_HEARTBEAT_SECS`, the `seats` ledger), `scripts/delegate/prepare_session.sh` (the run marker), `scripts/production/showrunners.py`, `scripts/production/live_units.py`, `scripts/production/unit_lookup.py`, `scripts/shutdown/remote.py` (`other_machine`).
- **Test lanes:** `scripts/message/test_*.py`, `scripts/production/test_*.py`, `scripts/notify/test_*.py`, `scripts/shutdown/test_*.py`.
- **Test:** `python3 -m unittest discover -s scripts/message -p 'test_*.py'`; `python3 -m unittest discover -s scripts/production -p 'test_broadcast.py'`.
- **Lint:** `basedpyright scripts/message` and `basedpyright scripts/production` report 0 errors, 0 warnings, 0 notes (exit 3 is the missing `.venv` notice and expected); `bash -n` on changed shell scripts.
- **Invariants:**
  - Tests are hermetic. They use temp directories for sessions, the delegate root and the notifier state, fake `tmux`, `ps`, `ssh` and `codex`, and a fake daemon socket. They never reach the real daemon, a real session, `send.py`'s relay, `pushover.py` or the other machine.
  - From Phase 6, every shutdown `TestCase` derives from `test_support.ShutdownTestCase`, which fails a test that reaches a real send leaf.
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
- Mesh records carry `role`; a Codex worker with an unrecognized role reads `role unknown`. No launcher writes `lens` into the mesh roster yet.
- No running registered showrunner is a correct, common state, not a defect.

**Ruled out:** a session's role from its seat-name suffix — roles come from records only.

### Phase 2 — Every message to the user says what the user does · status: done

#### As-built

- `scripts/notify/user_action.py` (standard library only) holds the action as a type: frozen `ActionRequired(text)` and `NoActionRequired()`, `UserAction = ActionRequired | NoActionRequired`, `ActionUnstated()`, `ActionRefused(reason)`. `parse_user_action(action: str | None, no_action: bool) -> UserAction | ActionUnstated | ActionRefused` refuses both flags and empty or placeholder text (`none`, `n/a`, `nothing`, `no action`, `-`, any case); `refused_at(action, priority) -> ActionRefused | None` refuses `NoActionRequired` above priority 0 (2 is emergency, for an action needed now); `first_line(action)` gives `Action: <text>` or `No action needed.`; plus `FIX` and `REFUSED_BEFORE_SENDING`.
- `scripts/notify/pushover.py [--priority 0|1|2] [--source NAME] (--action TEXT | --no-action) TITLE MESSAGE` is the gate every path ends in. An `ActionRefused`, a `refused_at` result, or action text over `ACTION_TEXT_MAX` (1015) sends nothing: exit 2, stderr `pushover: refused before sending: <reason>. <FIX>`. `ActionUnstated` is still sent, with a stderr warning and log outcome `sent without an action line`, because `/etc/nixos/modules/linux/ups.nix` and `disk-floor.nix` call it unflagged from the nix store and the UPS alert must never be refused. The post opens with `first_line(action)`; over `MESSAGE_MAX` the body is cut, never the action line. Every `~/.local/state/notify/pushover.jsonl` entry names `--source`, else the parent command line (80 chars, `ps -o args= -p <ppid>`).
- `scripts/message/send.py`: `Options.action: MessageUserAction = UserAction | NoUserRecipient`. `--to user` requires `--action` or `--no-action` (usage error, exit 2, with `FIX`); on any other recipient they are a usage error (`--action and --no-action go with --to user`); `--need decision|blocked` need `ActionRequired`. It passes the action and `--source <--from>` to `pushover.py`, and `remote()` forwards the action. Outcome `refused`: `REFUSED: <reason>`, exit 2, nothing kept, no PushNotification fallback.
- `scripts/message/escalate.py`: `hold(key, summary, text, action: UserAction, minutes=15, need="decision") -> bool` (CLI `hold --action | --no-action`); `NoActionRequired` with a `need` other than `note` raises `ValueError`. `HeldMessage.action` is a `UserAction` (stored JSON private); `read(path) -> HeldMessage | LegacyHeld | UnreadableHeld | NoHeld`. A `LegacyHeld` record (no action) is never delivered, lists as `waiting for a hold that states its action`, and the next `hold()` replaces it; `deliver()` passes the stored action.
- Senders `rust_release`, `sweep`, `ci_points`, `codex_winddown`, `settle` (holdout, partial stop) and `restart` (incomplete restart) pass `--action` naming the step; `five_hour` holds `NoActionRequired()` at `need="note"`. `settle.alert_user` takes `ActionRequired`; `settle.send_message(..., action: UserAction)` requires one for every recipient, so session sends pass an ignored `NoActionRequired()`.

**Files:**
- `scripts/notify/user_action.py` — action types and checks; `scripts/notify/pushover.py` — the gate
- `scripts/message/send.py`, `scripts/message/escalate.py` — user path and held messages; both put `scripts/notify` on `sys.path`, and `pyrightconfig.json` adds it to every environment holding `scripts/message`
- `scripts/{buildlog/rust_release,lint/sweep,production/ci_points,production/codex_winddown,shutdown/settle,shutdown/restart,whoami/five_hour}.py` — senders; every `test_*.py` fakes the post or subprocess and never reads the real keys file
- `commands/{alert_user,builds,fix,watcher}.md`, `commands/showrunner/produce.md` (`<Notify/>` only) — every command shows the flags, `--need blocked` only when the user must act now, and the `FAILED` PushNotification fallback carries the same first line

**Binds later work:**
- Every non-user recipient carries `NoUserRecipient`; action flags stay a usage error off `--to user`, and `remote()` forwards them only for `--to user` — "`send.py` reaches any session the roster lists" and other-machine sends keep this; `broadcast.py` passes no action flags.
- `REFUSED:` (exit 2) means fix the flags and resend, never a PushNotification fallback, and the messaging instructions name it. "A message with no action stated is refused" refuses `ActionUnstated` with `REFUSED_BEFORE_SENDING`, so `send.py` reports it as `REFUSED` unchanged; the `sent without an action line` log lines are its audit input.

**Gotchas:**
- `pushover.py` exit 2 also means missing keys; only the `refused before sending:` stderr marker marks a refusal.
- `test_dailies_input.py` asserts the review-watch command line, so `ci_points.py` flag changes reach it.

**Ruled out:** a model reviewing message wording — the action is a required field.

### Phase 3 — `send.py` reaches any session the roster lists · status: done

#### As-built

- `roster.py` gives every row an address unique on both machines: `session:<id>` (Claude session), `seat:<run id>/<seat name>` (Codex worker; the run id is the run's session directory name), `codex:<thread id>` (daemon session). `RosterEntry.reach: Messageable(address) | CannotReceiveMessages(reason)`: a blind reviewer is `CannotReceiveMessages("blind reviewer")`, and a seat whose mesh status is not `running` / `waiting_capacity` is `CannotReceiveMessages` with a reason naming whether a unit director runs. Links are `ShowrunnerRelationship = RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner` and `DirectorRelationship = DirectsUnit | UnderDirector | NoDirector`. `RosterEntry.assignment: WorkerAssignment | NotAWorker`. A roleless mesh entry reads `no role`.
- `--json` flattens these states at one boundary into the old flat keys, empty for no-link states and for `CannotReceiveMessages`; the text form is unchanged.
- `send.py --to` resolves by form: `user` → user path; `uds:` / `session:` → Claude path; `seat:` → that roster row, sent through `codex()` with the row's `session_dir` (`--codex --session-dir` still works and skips the lookup); `codex:<thread id>` → `codex_daemon.queue`, no roster read; a bare name → roster rows with that `name` (one `Messageable` sends by its address; one `CannotReceiveMessages` exits 2 with the reason and the unit director to contact, or that none runs; several exit 2 listing each address and role; none falls to the Claude path). The roster is read once, only for `seat:` and bare names; each roster problem prints `send.py: roster: <problem>` and resolution continues on the rows read.
- Every recipient other than `user` carries `NoUserRecipient`, so `--action` / `--no-action` is a usage error for `seat:`, `codex:` and bare-name sends.
- `codex_daemon.queue(thread_id, text, timeout=QUEUE_TIMEOUT_SECONDS)` (30 s) runs `codex queue --thread <id> --message <text>`; nothing is kept on failure.
- `settle.send_message` sends to a session with no action parameter and raises `ValueError` for `user`; `settle.alert_user(..., action)` is the only user path. `restart.py` session callers pass no action.

**Files:**
- `scripts/message/roster.py` — address forms, `reach`, link states, flat JSON boundary.
- `scripts/message/send.py` — resolution order, refusals, `codex:` route, key retirement under name and address.
- `scripts/message/codex_daemon.py` — `queue` to a daemon thread.
- `scripts/notify/user_action.py`, `scripts/notify/pushover.py` — `ActionAllowed | ActionRefused`; `pushover.py` matches on it.
- `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py` — session sends without an action; user alerts with one.
- Tests: `scripts/message/test_roster.py`, `test_send.py`, `test_codex_daemon.py`; `scripts/notify/test_pushover.py`; `scripts/shutdown/test_settle.py`, `test_restart.py`, `test_stop.py`.

**Binds later work:**
- Address forms: `session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`, bare names, `uds:`, `user`. `RosterEntry.reach: Messageable(address) | CannotReceiveMessages(reason)`; `ShowrunnerRelationship = RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`; `DirectorRelationship = DirectsUnit | UnderDirector | NoDirector`.
- `--json` keeps flat keys (production, showrunner, showrunner_address, unit, director, director_address, slot_role, session_dir, address). A showrunner's and a unit director's own rows (`RunsProduction` / `DirectsUnit`) carry empty link fields, and a `CannotReceiveMessages` row has an empty `address`: broadcast groups select members by `UnderShowrunner` / `UnderDirector` and skip those rows rather than send to `""`, taking the refusal line from `send.py`.
- A seat is Messageable only at mesh status `running` / `waiting_capacity`; a seat between turns is resumed only by its unit director. Sends to the other machine route `seat:` and `codex:` through that machine's `codex_mesh` / daemon, and a `seat:` address of a seat between turns keeps `no roster session has address` there too.
- `send.py` exit codes: 0 sent, 1 queued for a Claude session, 2 usage error / ambiguous name / cannot take messages / no roster session has address / refusal, 3 failed. `codex_daemon.queue` → `Queued | QueueFailed | QueueTimedOut`, printed `SENT:` / `FAILED:`; `FAILED:` has no fallback. The messaging instructions (`commands/message.md`, `codex/AGENTS-messaging.md`) name each exit-2 refusal and its recovery.
- `user_action.refused_at` → `ActionAllowed | ActionRefused`; `settle.alert_user` always passes `--action` and `settle.send_message` refuses `user`. The refusal of messages with no action stated builds on these.

**Gotchas:** An ephemeral daemon thread refuses `codex queue` (-32600) and cannot be archived; live checks start threads with `ephemeral: false`. `thread/read` does not show a queued message; `thread/turns/list` does once the turn starts. `codex_mesh send` accepts only `running` / `waiting_capacity` seats. A worker under a heartbeat-only run is `NoDirector`, so refusal wording handles a missing unit director.

**Ruled out:** A transport-specific route state in `broadcast.py` (`send.py` already picks the transport); a `cannot take messages` line for a `seat:` address of a seat between turns (such a row has no address).

### Phase 4 — A message with no action stated is refused · status: done

#### As-built

- `pushover.py` refuses `ActionUnstated` (neither `--action` nor `--no-action`) through the same `refuse()` path as its other refusals: nothing sent, exit 2, stderr `pushover: refused before sending: no action stated. <FIX>`, and one log line with outcome `refused before sending: no action stated` and its source.
- Every refusal comes before the keys file is read, in this order: `ActionRefused` from parsing, `ActionUnstated`, `--action` text over `ACTION_TEXT_MAX`, then `refused_at`. The transition warning and the `sent without an action line` outcome are gone; a send logs `sent` or `sent, receipt <r>`.
- The module docstring says messages without `--action` or `--no-action` are refused before sending, and that exit 2 means refused before sending, usage, or keys missing.

**Files:**
- `scripts/notify/pushover.py` — the gate every user message ends in.
- `scripts/notify/test_pushover.py` — `test_unstated_action_is_refused_before_posting_and_logged`.

**Binds later work:** No message reaches the user without `--action` or `--no-action`. Callers see a refusal as exit 2 and the stderr line naming `FIX`; the recovery is to add the flag and resend, which the messaging instructions give as the meaning of `REFUSED:` (exit 2). No caller of the removed outcome remains: the log holds no `sent without an action line` entry since the phase that added the warning, and natedev's `ups.nix` and `disk-floor.nix` pass the flags.

**Gotchas:** A caller with no keys and no action flag gets the action refusal, not `keys missing`.

### Phase 5 — Process listing gets its own module · status: done

#### As-built

- `scripts/message/process_table.py` (standard library only, imports no other script) holds the process table: `Process(pid, parent, command, arguments)` (`NamedTuple`), `snapshot() -> dict[int, Process]` running `PROCESS_TABLE_PS` (default `ps -eo pid=,ppid=,comm=,args=`), and `is_codex_agent(process: Process) -> bool` (a `codex exec`, or a `codex_mesh.py start` / `follow` turn).
- `roster.py` and `broadcast.py` read the table from `process_table`. `roster.py` no longer imports `broadcast`; `broadcast.py`'s own `Process`, `processes()`, `is_codex_agent` and `BROADCAST_PS` are gone.
- `codex_winddown.py` reads the table from `process_table`, and its pid-to-name map and `start()`'s showrunner records from `sessions.live_sessions()` (named records only; `start()` keeps the newest record per name). It still sends through `broadcast.broadcast`, `broadcast.report`, `broadcast.send` and `broadcast.MESSAGE`.
- A unit's completion projection is `Projection = CompletionExpectedAt(epoch_seconds) | CompletionUnmeasured | ProjectionAnswerOwed` (frozen dataclasses). `read_projections()` decodes the stored JSON (number / `"unmeasured"` / `null`) into them and `_stored_projection()` encodes each back to that shape, so `projections.json` and the count line text are unchanged.

**Files:**
- `scripts/message/process_table.py` — the one process table every reader shares.
- `scripts/message/roster.py`, `scripts/production/broadcast.py` — read the table through `process_table`.
- `scripts/production/codex_winddown.py` — named projection states; session records from `sessions.py`.
- Tests: `scripts/message/test_process_table.py` (`snapshot`, the `is_codex_agent` cases); `scripts/production/test_codex_winddown.py` (each projection state, a save-and-reload round trip of every state, `start()` reading no `broadcast.live_sessions`); `test_roster.py` patches `process_table`, `test_broadcast.py` sets `PROCESS_TABLE_PS`.

**Binds later work:**
- `process_table` imports none of `roster`, `broadcast` or `codex_winddown`, so `broadcast.py` can import `roster.py` without a cycle; "`broadcast.py` takes its groups from the roster" relies on this. `broadcast.live_sessions()` has no caller outside `broadcast.py`.
- Wind-down's `announce()` calls `broadcast.broadcast(sender, {"showrunner": …, "unit director": …})` and `broadcast.report`; a role-key or signature change there carries `codex_winddown.py` and `test_codex_winddown.py`.

**Gotchas:**
- `PROCESS_TABLE_PS` is read at import, so a test that sets it reloads `process_table` or patches `process_table.PS`.
- `start()` keeps the first record per name, so it depends on `sessions.live_sessions()` listing the newest record first.
- Session records are one file per pid (`~/.claude/sessions/<pid>.json`), so a pid-to-name map cannot hold a stale name.

**Ruled out:** a module-wide ban on wind-down calling `broadcast` — it copies the group send into wind-down.

### Phase 6 — A shutdown test that reaches the real sender fails instead of sending · status: done

#### Work Order

**Goal:** a shutdown test whose fake has gone stale fails, naming the real send leaf it reached and the recipient, and nothing reaches a session, the other machine or the user's phone.

**Spec:**
- **Why (fact).** 2026-10-10 11:43 PDT: a seat split the shutdown sender and ran `scripts/shutdown/test_stop.py` before updating its patches. Two tests still faking the old sender reached the real path (`settle.alert_user` → `settle._deliver_message` → a `send.py --to user` subprocess → Pushover) and posted their fixture alerts ("claude 2: stop partial") to the user's phone. Each test fakes its own send, and nothing catches a fake that has gone stale.
- **The real send leaves in `scripts/shutdown/*.py`**, each the last call before a process that delivers to a session, the other machine or the user. Each stub names the leaf and the recipient shown here:
  - `settle._deliver_message(command, recipient, text)` — runs `send.py` (`settle._send_command()`: `SHUTDOWN_SEND`, else `scripts/message/send.py`). `settle.send_message` (sessions, `--machine` included) and `settle.alert_user` (`--to user`, ending in `pushover.py`) both end here, and `restart._send_restart_note` and `restart.py`'s "is not fully back" alert reach it through them. Recipient: `recipient`.
  - `settle._retire_shutdown_messages(login, session_id)` — runs `send.py retire --to session:<id>` (`settle.retire_command`). Recipient: `session:<session_id>`.
  - `restart._retire(record, entry, *, dry_run)` — runs the same `send.py retire` through `restart._run`. Recipient: `session:<entry["session"]["session_id"]>`. A dry run only prints the command, so the stub passes `dry_run=True` through to the real function.
  - `remote.run_remote(args, stdin="", limit=STANDARD_TIME_LIMIT)` — runs `shutdown.py <args>` on `remote.other_machine()` over ssh, where that machine's own leaves send. `settle`, `restart` and `shutdown` import it by name, so the stub replaces all four bindings: `remote.run_remote`, `settle.run_remote`, `restart.run_remote`, `shutdown.run_remote`. Recipient: `<other machine>: shutdown.py <args>`.
  - `settle._launch_conductor(login, label, here)` — starts a detached `shutdown.py conduct <login>` under `systemd-run --user` or `launchctl submit`, which messages sessions and alerts the user outside the test process, where no stub reaches. Recipient: `conductor for <login>`.
  - `settle.run_notifier(verb, instance)` — runs the progress notifier (`settle._notifier_command()`: `SHUTDOWN_NOTIFIER`, else `unit_notifier.sh`) with `start` or `stop`. `start` turns a real timer back on, and its later ticks message sessions outside the test process. `settle.py` and `restart.py` reach it as `settle.run_notifier`. Recipient: `notifier <instance>`.
  - Not leaves: `stop.py`'s `systemctl` / `launchctl` / `tmux kill-session` / `codex_mesh.py stop` and signals (they end sessions), `restart._add_unit_command` and the session launches in `restart._run` (they start sessions), and the read-only `git`, `ps`, `tmux capture-pane` and `github-warm-status` probes.
- **`scripts/shutdown/test_support.py`** (new, standard library only) defines `class ShutdownSendLeaf(StrEnum)`, one member per leaf above whose value is its dotted name (`DELIVER_MESSAGE = "settle._deliver_message"`, `RETIRE_SHUTDOWN_MESSAGES`, `RESTART_RETIRE = "restart._retire"`, `RUN_REMOTE = "remote.run_remote"`, `LAUNCH_CONDUCTOR`, `RUN_NOTIFIER`), and `class ShutdownTestCase(unittest.TestCase)`:
  - `setUp` replaces every leaf above with a stub. A reached stub raises `AssertionError("shutdown test reached real send leaf <dotted name> for <recipient>; fake the step above it, or allow_leaf(ShutdownSendLeaf.<member>) and fake the subprocess beneath it")`, so nothing starts, and records the hit. A cleanup registered in `setUp` fails the test for any recorded hit, so a caller that catches the error (`settle.py` wraps `alert_user` and `stop_work.stop` in `except Exception`) still fails the test.
  - `allow_leaf(leaf: ShutdownSendLeaf) -> None` restores the real function for that leaf for the rest of the test (`ShutdownSendLeaf.RUN_REMOTE` restores all four bindings). Text becomes a leaf only through `ShutdownSendLeaf("<dotted name>")`, which raises `ValueError` for an unknown name. Called in a subclass's `setUp` after `super().setUp()`, it covers that class; called in a test body, that test.
  - A test that patches a leaf itself (`patch.object(settle, "_deliver_message", ...)`) replaces the stub for that patch's span and needs no `allow_leaf`.
  - `discover` imports `test_support.py` as a test module; `ShutdownTestCase` has no `test_*` methods, so it adds no tests.
- **Every `TestCase` in `scripts/shutdown/test_*.py` derives from `ShutdownTestCase`**, and each `setUp` calls `super().setUp()` first: `InventoryTests` and `DarwinProcessStartTests` (`test_inventory.py`), `RecordTests`, `RemoteTests`, `RestartTests`, `SettleTests`, `ShutdownLaunchCliTests`, `ShutdownStatusTests`, `StopTests`.
  - A test that exercises a leaf on purpose calls `allow_leaf` and fakes the subprocess beneath: `RemoteTests` allows `remote.run_remote` (its fake `ssh` on `PATH`); `SettleTests` allows `settle._deliver_message` and `settle._retire_shutdown_messages` (the `SHUTDOWN_SEND` stub script its `setUp` sets) and `settle.run_notifier` (its `SHUTDOWN_NOTIFIER` stub); `RestartTests` patches `settle.run_notifier` itself where it reaches it; `RestartTests` allows `restart._retire` only in the tests that record `retire` commands through a `restart._run` fake.
  - A test the guard fails has a stale fake: it gets a fake of the step it meant to fake, or `allow_leaf` with the subprocess beneath faked. No test is deleted, skipped or weakened.
  - `test_shutdown_cli.py`'s child processes (`shutdown.py launch-blocked`, `launch_permission.py exec-new-work`) run outside the guard; neither reaches a leaf.
- **Proof test** `scripts/shutdown/test_send_guard.py` (new). Each case defines its `ShutdownTestCase` subclass inside the test method (a module-level one would be discovered and run on its own), runs it into a `unittest.TestResult()`, and asserts on the result. The failing cases patch `subprocess.run` and `subprocess.Popen` to record calls and assert there are none.
  - `settle.alert_user(record.AllAccountSessions(kind="all account sessions"), "summary", "text", ActionRequired("act"))` with no fake: the test fails (`result.wasSuccessful()` is false) and its failure text names `settle._deliver_message` and `user`; no subprocess call. An uncaught hit is reported by the stub and again by the cleanup, so the case does not assert a failure count. `alert_user` returns before sending for a `selected` scope, so the account-wide scope is the one that reaches the leaf.
  - The same call inside `try: … except Exception: pass` still fails the test.
  - `settle.send_message("session:x", …)` and `settle._retire_shutdown_messages(login, "x")` each fail naming `session:x`; each of `remote.run_remote`, `settle.run_remote`, `restart.run_remote` and `shutdown.run_remote` fails naming the other machine; `settle._launch_conductor` fails naming the login; `settle.run_notifier("start", "x")` fails naming `notifier x`; `restart._retire(…, dry_run=False)` fails and `dry_run=True` passes.
  - `allow_leaf(ShutdownSendLeaf.DELIVER_MESSAGE)` with `SHUTDOWN_SEND` pointed at a script that records its arguments: the call reaches the script and the case passes.
  - `ShutdownSendLeaf("no.such_leaf")` raises `ValueError`.

**Files:**
- `scripts/shutdown/test_support.py` — new: `ShutdownSendLeaf`, `ShutdownTestCase`, the leaf stubs, `allow_leaf`.
- `scripts/shutdown/test_send_guard.py` — new: the proof test.
- `scripts/shutdown/test_inventory.py`
- `scripts/shutdown/test_record.py`
- `scripts/shutdown/test_remote.py` — allows `remote.run_remote`.
- `scripts/shutdown/test_restart.py` — allows `restart._retire` where it records `retire` through `restart._run`.
- `scripts/shutdown/test_settle.py` — allows `settle._deliver_message` and `settle._retire_shutdown_messages` under its `SHUTDOWN_SEND` stub, and `settle.run_notifier` under its `SHUTDOWN_NOTIFIER` stub.
- `scripts/shutdown/test_shutdown_cli.py`
- `scripts/shutdown/test_shutdown_status.py`
- `scripts/shutdown/test_stop.py`

**Seats:** `1 writer + 1 tester`.
- `impl` — `test_support.py` and the eight test-module conversions; hub: `test_support.py` (the base every shutdown test derives from).
- `test` — `test_send_guard.py`, from the Spec alone.

**Constraints from prior phases:**
- Phase 3's `settle.alert_user(scope, summary, text, action: ActionRequired, *, machine="")` always passes `--action` (with `--need decision`) and returns without sending for a `selected` scope; `settle.send_message(recipient, summary, text, *, machine="", key="") -> MessageDelivery` refuses `user` with `ValueError`. Both build their command from `settle._send_command()` and run it in `settle._deliver_message`.
- Phase 4's `pushover.py` refuses only a message with no action stated, so a real `alert_user` from a test is sent, not refused.
- Phase 2: every `test_*.py` fakes the post or subprocess and never reads the real keys file.

**Acceptance gate:** `python3 -m unittest discover -s scripts/shutdown -p 'test_*.py'` green, `test_send_guard.py` included. `basedpyright scripts/shutdown` reports 0 errors, 0 warnings, 0 notes (exit 3 is the missing `.venv` notice and expected). Note the line count of `~/.local/state/notify/pushover.jsonl` before the suite run; every line added by the end of the run names a sender outside the suite in its `source`, so none came from a shutdown test's fixture. Never read `~/.config/pushover/env`.

### Phase 7 — `broadcast.py` takes its groups from the roster · status: todo

#### Work Order

**Goal:** every group send reaches the roster's members of that group, Claude and Codex, and two new groups exist: workers (all, or one unit's) and top level.

**Spec:**
- `recipients()` reads `roster.roster()` in place of its own session, unit and `codex_seats` code, which is removed, `live_sessions()` included.
- **Membership comes from identity, never from names.** A unit's workers are the rows whose `director` is `UnderDirector` with that unit director's address; a showrunner's units are the rows whose `showrunner` is `UnderShowrunner` with that showrunner's address. A showrunner's own row (`RunsProduction`), a unit director's own row (`DirectsUnit`), a production with no running showrunner (`ProductionWithoutRunningShowrunner`) and a worker with `NoDirector` belong to no such group; tests cover each. The sender is left out by identity, never by name: a session sharing the sender's name is still sent. Membership matches on Phase 3's link states (`UnderDirector.address`, `UnderShowrunner.address`).
- **The sender's identity** is read once from the environment as `SenderIdentity = CodexThread(thread_id) | ClaudeSession(session_id) | NoSession` (frozen dataclasses in `roster.py`). `CODEX_THREAD_ID` wins: a Codex shell inherits the `CLAUDE_CODE_SESSION_ID` of the Claude session that launched it, so that variable alone would name the unit director. `CodexThread` matches the Codex row carrying that thread; `ClaudeSession` the row whose reach is `Messageable("session:<id>")`; `NoSession` (a plain shell) matches no row and leaves nobody out. `roster.py`'s own "this session" mark in its text output uses the same identity.
- **`RosterEntry` gains `thread: CodexThread | NoCodexThread`**: a seat's thread id from its mesh record, a daemon session's from its `codex:` address, `NoCodexThread` for Claude rows and for a seat whose record names no thread. `--json` adds it as `thread`, the id or `null`.
- **`BroadcastGroup`** (closed, in `broadcast.py`) names what each flag selects. It is not the roster's `Role`: `--agents`, `--workers` and `--top-level` overlap, and roles do not. `--workers --of` carries the chosen unit director's address. The closing line names the groups sent: showrunners, unit directors, agents, workers (or workers of <unit>), top level. `broadcast.broadcast(sender, texts)` takes `Mapping[BroadcastGroup, str]`.
- Groups:
  - `--showrunners`: role showrunner.
  - `--units`: role unit director.
  - `--agents`: every worker and every freestanding session, Claude and Codex, the user's Codex sessions included.
  - `--workers TEXT`: every worker. With `--of <unit, unit director name, or session: address>` placed before it, only that unit's workers. An unknown unit is a usage error naming the known ones. A value matching more than one unit director, by director name or by one unit name under two productions, is a usage error (exit 2) listing each director's address.
  - `--top-level TEXT`: every Claude session that is not a unit director under a showrunner and not a worker, matching `top_level.py`'s set.
  - `--all`: every role. A role's own flag after `--all` still gives that role its own text.
  - A row selected by several flags gets one delivery, with the text of the last flag on the command line that selects it.
- Every recipient is sent through `send.py --to <address>` from its `Messageable` reach, never with `--action` or `--no-action`. `send.py --to <address>` already picks the Claude or Codex transport, so `broadcast.py` keeps no route state of its own: `Messageable` means deliver, `CannotReceiveMessages` means report, and the `codex = ""` sentinel goes. A `CannotReceiveMessages` row (a blind reviewer, or a seat between turns) is reported with the line `send.py` gives for it (its reason, then its unit director to contact, or that none is running), is not sent, and is not counted as a failure; tests cover both director states.
- Each roster problem prints as `broadcast: roster: <problem>`, and the send continues to the rows that were read.
- **`top_level.py`** takes its set from `roster.roster()`: Claude rows that are not unit directors under a showrunner, not workers, and not this session by `session:` identity. Its output stays `<name>\tuds:<socket path>`, the socket path read from that session's record through `sessions.py`, because `/notify_top_level` hands it to SendMessage. The `build_hold.py record-recipient --session-id <id> --name <name>` call takes the id from the row's `Messageable("session:<id>")` reach. Two live sessions sharing a name are two lines; one conversation open in two processes is one line, from the record updated last, as today. A row whose record has no messaging socket prints the `not reachable:` line, as today. Each roster problem prints to stderr as `top_level: roster: <problem>`, the rows that were read still print, and the exit status is 0.
- **Tests** (`test_broadcast.py`, `test_top_level.py`, `test_roster.py`, `test_codex_winddown.py`) fake the roster or build its fixtures. Named cases: a Codex seat sending, with its `CODEX_THREAD_ID` and its director's inherited `CLAUDE_CODE_SESSION_ID`, is left out while its director is sent; a daemon Codex session sending is left out; a plain shell leaves nobody out; a seat row and a daemon row carry their thread in `--json`; wind-down's announcement reaches the showrunners and unit directors groups; two workers sharing a seat name under different directors, each sent once to its own unit by `--of`; `--of` matching two directors is a usage error; a session sharing the sender's name is still sent; a reviewer reported and not counted; a roster problem printed while the rest sends; `top_level.py` two same-name sessions, two lines and two `record-recipient` calls; `top_level.py` one conversation in two processes, one line; `top_level.py` a roster problem on stderr while the rows print; `--all --top-level --showrunners` sends each row once with the last selecting flag's text.

**Files:**
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/message/top_level.py`
- `scripts/message/test_top_level.py`
- `scripts/message/roster.py` — `SenderIdentity`, `RosterEntry.thread`, the "this session" mark.
- `scripts/message/test_roster.py`
- `scripts/production/codex_winddown.py` — `announce()` passes `BroadcastGroup` keys.
- `scripts/production/test_codex_winddown.py`

**Seats:** `1 writer + 1 tester`.
- `impl` — `broadcast.py`, `top_level.py`, `roster.py`, `codex_winddown.py`; hub: `broadcast.py` (recipient and group assembly).
- `test` — `test_broadcast.py`, `test_top_level.py`, `test_roster.py`, `test_codex_winddown.py`, from the Spec alone.

**Constraints from prior phases:**
- Phase 3's `RosterEntry`: `machine`, `kind`, `role`, `name`, `status`, `reach: Messageable | CannotReceiveMessages`, `cwd`, `showrunner: RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`, `director: DirectsUnit | UnderDirector | NoDirector`, `assignment: WorkerAssignment | NotAWorker`; `roster(environment) -> Roster` (`entries`, `problems`). Link addresses are `session:<id>`; match on the variants, never on flat JSON fields.
- Phase 3's addresses: `session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`, each unique; `RosterEntry.reach: Messageable(address) | CannotReceiveMessages(reason)`; `send.py --to <address>` reaches each, and exits 2 for a `CannotReceiveMessages` name or an ambiguous name. `--action`/`--no-action` on a non-user send is a usage error (Phase 2).
- Phase 5's `process_table` module: `roster.py` no longer imports `broadcast`, so `broadcast.py` may import `roster`. `codex_winddown.py` reads sessions through `sessions.live_sessions()` and still uses `broadcast.send`, `broadcast.MESSAGE`, and, for its announcement to every showrunner and unit director, `broadcast.broadcast(sender, {"showrunner": text, "unit director": text})` and `broadcast.report(deliveries)`. `send`, `MESSAGE` and `report` stay as they are; `broadcast` takes `BroadcastGroup` keys in this phase, so `announce()` and its test change with it.
- `/notify_top_level` reads `top_level.py`'s `uds:` addresses and hands them to SendMessage.

**Acceptance gate:** the Test and Lint lines green, `test_top_level.py`, `test_roster.py` and `test_codex_winddown.py` included. A live dry check (`BROADCAST_SEND` pointed at a script that records its arguments) lists the recipients for each flag on natedev, with no message sent, and each flag's list equals the rows `roster.py --json` gives for that group at that moment. A group with no members (no showrunner registered, for one) lists none and says so.

### Phase 8 — Both machines · status: todo

#### Work Order

**Goal:** `roster.py` lists natedev and the Mac in one run; `send.py` reaches any address or name found only on the other machine; `broadcast.py` reaches both machines, each recipient once.

**Spec:**
- **Scope is named.** `RosterScope = ThisMachine | AllMachines` (frozen dataclasses in `roster.py`); `roster(environment, scope: RosterScope) -> Roster`, with `scope` required.
  - `roster.py` (the command) reads `AllMachines`; `--here` reads `ThisMachine`.
  - `broadcast.py` and `top_level.py` read `ThisMachine`: `broadcast.py` reaches the other machine by running itself there, and `top_level.py` lists this machine only (`/notify_top_level` adds other-machine peers from ListAgents).
  - `send.py` reads `ThisMachine` first and the other machine only when nothing here matches.
- **`AllMachines`** runs `roster.py --here --snapshot` on `remote.other_machine()` over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --snapshot'`, in parallel with the local read, and merges the entries, this machine first.
  - `--snapshot` prints `RosterSnapshot`, a lossless tagged JSON form of `Roster` used only between machines and hidden from `--help`: every reach, relationship and thread variant carries its tag and its fields, `CannotReceiveMessages.reason` included. It is decoded at once into the same variants, so a remote `RosterEntry` equals the one that machine built. `--json` stays the flat public form, unchanged.
  - ssh failure, a timeout (20 s) or output that does not decode as a snapshot is one problem line, `<host>: not reached: <why>`, and exit 1.
  - A snapshot that decodes is used whatever the remote exit status: a remote exit 1 with problems still gives its rows, and each of its problems comes through prefixed by the machine's name.
- **Routing in `send.py`.** The argparse boundary builds `RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine(host)` (frozen dataclasses), replacing `Options.machine: str | None`: `--here` is `LocalOnly`, `--machine HOST` is `NamedMachine(HOST)`, neither is `LocalThenPeer`, and `--here` with `--machine` is a usage error. Every send this machine makes on the other over ssh passes `--here`, so the other machine never asks back. `uds:` is always this machine's socket and never asks the other machine.
  - `LocalThenPeer`, this machine first for every form: a match here is sent here and the other machine is not asked, so a bare name on both machines reaches this machine's session, and `--machine <host>` or the address reaches the other one. An address that matches no row here, or a bare name that matches no row in a clean local roster (no `Roster.problems`), is looked up in the other machine's roster; exactly one `Messageable` row there sends through `NamedMachine(host)` with its address (action flags forwarded only for `user`); several rows there are Phase 3's ambiguity; a bare name matching only a `CannotReceiveMessages` row there gives Phase 3's `cannot take messages` line with that machine's reason; an unreachable machine is the `not reached` problem line and the send fails.
  - A bare name with no match here while the local roster has problems is not looked up on the other machine, because absence here is not proven: the problems are printed and the send exits 2 asking for an address or `--machine`.
  - A `CannotReceiveMessages` row has no address, so a `seat:` address of a seat between turns matches no row on either machine and keeps Phase 3's `no roster session has address` line.
  - The refusal for a row with `NoDirector` says once that no unit director is running; today the reason and the tail both say it.
  - A clean miss on the other machine falls back to Phase 3's local outcome for that form: a `session:` address is kept in the queue here (exit 1), a `codex:` address goes to this machine's daemon, a `seat:` address is `no roster session has address` (exit 2), and a bare name gets Phase 3's no-match result.
- **`broadcast.py`** runs `broadcast.py --here` on the other machine over ssh with the same flags and texts, and prints its lines under the host's name. The closing line names both machines. `--here` keeps it to this machine and runs no ssh. An unreachable machine is one `NOT sent` line for that machine. When `BROADCAST_SEND` is set here, the ssh command sets it on the other machine to the same path, so a recorder selected here is selected there too; a path missing there fails the send there and delivers nothing.
- The Mac has no tmux: there, unit directors come from the worktree match, and the tmux problem line is left out on a machine with no `tmux` binary.
- **Tests** fake `ssh`, recording each call. Named cases: a Claude session, a mesh worker and a daemon session each found only on the other machine are sent through `--machine` with their address; one delivery per recipient when both machines answer; a name on both machines is sent here with no ssh call; `--here` on `roster.py`, `broadcast.py` and `send.py` makes no ssh call; `top_level.py` makes no ssh call; an unreachable machine is one problem line and the local rows still print; a remote snapshot with a problem and exit 1 gives its rows and its prefixed problem; every reach and relationship state round-trips through `--snapshot`, a remote refusal reason included; each `RecipientResolutionScope` case, and `--here` with `--machine` as a usage error; every ssh send carries `--here`; a clean miss on the other machine gives each form's Phase 3 local outcome; a bare name absent from a local roster with problems asks no other machine and delivers to no same-named session there; a `seat:` address of a seat between turns on the other machine says `no roster session has address`; the `NoDirector` refusal names the missing director once; `BROADCAST_SEND` reaches the ssh command only when set here.

**Files:**
- `scripts/message/roster.py`
- `scripts/message/test_roster.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/message/top_level.py` — passes `ThisMachine`.
- `scripts/message/test_top_level.py`

**Seats:** `1 writer + 1 tester`.
- `impl` — `roster.py`, `send.py`, `broadcast.py`, `top_level.py`; hub: `roster.py` (`RosterScope`, `RosterSnapshot` and the aggregate read).
- `test` — `test_roster.py`, `test_send.py`, `test_broadcast.py`, `test_top_level.py`, from the Spec alone.

**Constraints from prior phases:**
- Phase 3's addresses (`session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`), `RosterEntry.reach`, and `send.py`'s resolution order and exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, or a refusal, 3 failed.
- Phase 5's `process_table` module; `roster.py` does not import `broadcast`.
- Phase 7's groups (`--showrunners`, `--units`, `--agents`, `--workers` with `--of`, `--top-level`, `--all`) as `BroadcastGroup`, membership by `UnderDirector` / `UnderShowrunner` addresses, the sender left out by `SenderIdentity` (`CODEX_THREAD_ID`, else `CLAUDE_CODE_SESSION_ID`), `RosterEntry.thread`, roster problems printed while the rest sends, and `top_level.py`'s `<name>\tuds:<socket path>` lines.
- `scripts/shutdown/remote.py` `other_machine()` maps `natedev` and `mac` to each other.

**Acceptance gate:** the Test and Lint lines green. Live: `roster.py` on natedev lists the Mac's sessions; `roster.py --here` on the Mac exits 0 or 1 with no ssh. `broadcast.py`'s dry check copies a recorder that records its arguments to the same scratch path on both machines and sets `BROADCAST_SEND` to it; both recorders capture their machine's recipients, each recipient once across the two, no message is sent, and the closing line names both machines. One scratch background Claude session on the Mac receives `send.py --to <its name>` from natedev and is then removed.

### Phase 9 — Claude and Codex sessions know which tool to use · status: todo

#### Work Order

**Goal:** a Claude session reads in `/message` when to use ListAgents and SendMessage and when to use the roster. Every Codex session on both machines reads in its `AGENTS.md` how to list and message any session, and how to message the user.

**Spec:**
- `commands/message.md` gains a short "Finding sessions" section:
  - ListAgents and SendMessage for Claude sessions;
  - `roster.py` for Codex workers, for who is whose unit director and showrunner, and for the other machine;
  - `send.py --to <address>` for anything the roster lists;
  - `broadcast.py` and its groups for many at once.

  The Sending bullet's `--codex --session-dir` wording becomes "by its `seat:` address or seat name". The section also says what each refusal means and what to do, in the same words as `codex/AGENTS-messaging.md` below.
- `commands/announce.md`, `commands/unit/announce.md` and `commands/showrunner/announce.md` say the message reaches both machines and Codex workers. `commands/notify_top_level.md` keeps its steps and its `uds:` wording; Phase 7 kept `top_level.py`'s output.
- `codex/AGENTS-messaging.md` (new, in this repo) is the Codex text, short:
  - list every session with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py`;
  - message one with `send.py --to <address> --from <your session name>`, a group with `broadcast.py`;
  - message the user with `send.py --to user --summary … --text …` and `--action "<what the user does>"` or `--no-action`; emergency only when the user must act now; `REFUSED:` (exit 2) means fix the action flags as the line says and send again, with no PushNotification fallback; action flags go only with `--to user`;
  - what each refusal means and what to do: `cannot take messages (<reason>)` (exit 2) — a seat between turns is resumed only by its unit director, so contact that director or wait; a name matching several sessions (exit 2) lists their addresses, so send again with one of them; `no roster session has address` (exit 2) — the address is stale or its seat is between turns, so read the roster again; `FAILED:` (exit 3) from a Codex queue failure or timeout gets no fallback and is reported;
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

**Seats:** `1 writer + 1 tester`.
- `impl` — `install_codex_agents.py`, every command file and `codex/AGENTS-messaging.md`; hub: `codex/AGENTS-messaging.md` (the text the installer writes).
- `test` — `test_install_codex_agents.py`, from the Spec alone.

**Constraints from prior phases:**
- Address forms: `session:<id>` (Claude), `seat:<run id>/<seat name>` (Codex worker), `codex:<thread id>` (the user's Codex sessions), or a bare name; a name matching several rows exits 2 and lists their addresses.
- `send.py` exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, an address no roster session has, or a refusal (`REFUSED:`), 3 failed. `--here` keeps a send to this machine; `--machine HOST` sends from that machine; neither looks here first, then on the other machine. `--to user` needs `--action TEXT` or `--no-action`; `--need decision` and `--need blocked` need `--action`.
- `broadcast.py` groups: `--showrunners`, `--units`, `--agents`, `--workers` (with `--of <unit>` before it), `--top-level`, `--all`; both machines unless `--here`. It leaves its sender out by `CODEX_THREAD_ID`, else `CLAUDE_CODE_SESSION_ID`, so a Codex session needs no flag for that.
- `roster.py` lists both machines unless `--here`; a machine not reached is one stderr line and exit 1; `--json` gives `{"entries": [...], "problems": [...]}`.

**Acceptance gate:** the Test and Lint lines green; `commands/message.md` and `codex/AGENTS-messaging.md` both say what `REFUSED:` means and that it gets no fallback, and both name `cannot take messages`, the ambiguous-name address list, `no roster session has address`, recovery of a seat between turns through its unit director, and `FAILED:` with no fallback, each with its recovery; the block installed on both machines, with the Mac's Rust rules unchanged (`diff` of the text outside the block is empty). Live: a scratch Codex daemon thread on natedev, started with `ephemeral: false` (an ephemeral thread refuses queued messages and cannot be archived), asked to list every Claude session and its role, runs `roster.py`, and its answer matches `roster.py --json` at that moment, saying so when no showrunner is registered. A queued request is checked in `thread/turns/list` once the turn starts, since `thread/read` does not show it. Then it is archived.
