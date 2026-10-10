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
- **The refusal runs right after Phase 3** (2026-10-10). natedev's generation 206 (`/etc/nixos` 9ef892a) passes `--action` or `--no-action` from `ups.nix` and `disk-floor.nix`, checked against the running system, so the phase that refuses a message with no action stated moved up from last to Phase 4, per its own resequence rule. Packaging only; one-line revert.
- **Live checks never message a real session.** They use scratch recipients only: a scratch background Claude session, a scratch mesh thread in a scratch session directory, and a scratch daemon thread. All are removed after the check. Listing real sessions is read-only and allowed.
- **Shutdown tests can never reach the user's phone** (2026-10-10). The user approved a guard after two fixture alerts reached their phone from a test whose fake had gone stale; it runs as Phase 6, right after the process-table move.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills and scripts. This plan adds the session roster (Phase 1), a gate that makes every message to the user say what the user does (Phase 2), sends to any roster address or name (Phase 3), refuses a message to the user that states no action (Phase 4), moves the process table into its own module (Phase 5), makes a shutdown test that reaches a real send fail instead of sending (Phase 6), sends to roster groups (Phase 7), lets the shutdown inventory tests see their sessions again (Phase 8), lists a session with no messaging socket only while its own process runs (Phase 9), covers both machines (Phase 10), and teaches Claude and Codex sessions which tool to use (Phase 11). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-shutdown` on branch `build-followups-shutdown` (unit `shutdown-unit` of production `build-followups`).
- **Project started:** 2026-10-10T16:30:07.804+00:00
- **Stack:** Python 3.13 standard library only; bash, zsh; codex-cli 0.162.0 (daemon 0.162.1).
- **Layout:**
  - `scripts/message/roster.py` — the roster (new, Phase 1; unique addresses Phase 3; both machines Phase 10)
  - `scripts/message/sessions.py` — the Claude session registry readers (Phase 8); a socketless record's process identity (Phase 9)
  - `scripts/message/process_table.py` — the process table the roster, `broadcast.py` and wind-down read (Phase 5)
  - `scripts/message/codex_daemon.py` — reads the shared Codex daemon's live sessions; queues to one (new, Phase 1; queue Phase 3)
  - `scripts/notify/pushover.py`, `scripts/notify/user_action.py` — the message-to-user gate and its action types (Phase 2; refusal Phase 4)
  - `scripts/message/send.py` — `--action` / `--no-action` for the user (Phase 2); any roster address or name (Phase 3); the other machine (Phase 10)
  - `scripts/production/broadcast.py` — groups from the roster (Phase 7); both machines (Phase 10)
  - `scripts/shutdown/test_support.py` — the base every shutdown test derives from; stubs every real send leaf (new, Phase 6)
  - `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py` — instructions (Phase 11)
- **Key files (read, never edited here):** `scripts/agents/codex_mesh.py` (model-study: roster format, `send`), `scripts/delegate/remove_seats.py` (`live_runs`, `LIVE_HEARTBEAT_SECS`, the `seats` ledger), `scripts/delegate/prepare_session.sh` (the run marker), `scripts/production/showrunners.py`, `scripts/production/live_units.py`, `scripts/production/unit_lookup.py`, `scripts/shutdown/remote.py` (`other_machine`).
- **Test lanes:** `scripts/message/test_*.py`, `scripts/production/test_*.py`, `scripts/notify/test_*.py`, `scripts/shutdown/test_*.py`.
- **Test:** `python3 -m unittest discover -s <directory> -p '<pattern>' </dev/null` for each of: `scripts/message` `test_*.py`; `scripts/shutdown` `test_*.py`; `scripts/build_hold` `test_*.py`; `scripts/model_study` `test_rerun.py`; and in `scripts/production` each of `test_broadcast.py`, `test_codex_winddown.py`, `test_tmux_names.py`, `test_unit_status.py`, `test_unit_lookup.py`, `test_adopt.py`, `test_live_units.py`, `test_waiting.py` and `test_stall_watch.py`. Together they are every suite that reads `sessions.py`; some fakes read stdin, hence `</dev/null`. Run each under a scratch `HOME`.
- **Lint:** `basedpyright scripts/message scripts/shutdown scripts/production` reports 0 errors, 0 warnings, 0 notes (exit 3 is the missing `.venv` notice and expected); `bash -n` on changed shell scripts.
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
- Codex workers read `role unknown` until their mesh records carry `role` and `lens`.
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

#### As-built

- `scripts/shutdown/test_support.py` defines `class ShutdownSendLeaf(StrEnum)`, one member per real send leaf in `scripts/shutdown/*.py` (the last call before a process that delivers to a session, the other machine or the user), valued at its dotted name. The recipient each stub names follows the dash:
  - `DELIVER_MESSAGE = "settle._deliver_message"` — runs `send.py`; `settle.send_message`, `settle.alert_user` and `restart.py`'s note and alert all end here — the `recipient` argument.
  - `RETIRE_SHUTDOWN_MESSAGES = "settle._retire_shutdown_messages"` — `send.py retire` — `session:<session_id>`.
  - `RESTART_RETIRE = "restart._retire"` — the same retire through `restart._run` — `session:<session_id>`. A `dry_run=True` call passes through to the real function, which only prints.
  - `RUN_REMOTE = "remote.run_remote"` — `shutdown.py <args>` over ssh — `<other machine>: shutdown.py <args>`.
  - `LAUNCH_CONDUCTOR = "settle._launch_conductor"` — a detached `shutdown.py conduct <login>` — `conductor for <login>`.
  - `RUN_NOTIFIER = "settle.run_notifier"` — the progress notifier's `start` / `stop` — `notifier <instance>`.
- `class ShutdownTestCase(unittest.TestCase)`: `setUp` patches every binding in `_REAL_LEAF_BINDINGS` with a stub. A reached stub records the hit and raises `AssertionError("shutdown test reached real send leaf <dotted name> for <recipient>; fake the step above it, or allow_leaf(ShutdownSendLeaf.<member>) and fake the subprocess beneath it")`, so no process starts. A cleanup registered in `setUp` fails the test for any recorded hit, so a caller that catches the error (`settle.py`'s `except Exception` around `alert_user` and `stop_work.stop`) still fails it.
- `allow_leaf(leaf: ShutdownSendLeaf) -> None` restores the real function for the rest of the test; called in a subclass `setUp` after `super().setUp()`, it covers the class. It raises `TypeError` for anything but a `ShutdownSendLeaf`; text becomes a leaf only through `ShutdownSendLeaf("<dotted name>")`, which raises `ValueError` for an unknown name. A test that patches a leaf itself replaces the stub for that patch's span and needs no `allow_leaf`.
- Every `TestCase` in `scripts/shutdown/test_*.py` derives from `ShutdownTestCase` and calls `super().setUp()` first. `SettleTests` allows `DELIVER_MESSAGE`, `RETIRE_SHUTDOWN_MESSAGES` and `RUN_NOTIFIER` class-wide (its `SHUTDOWN_SEND` and `SHUTDOWN_NOTIFIER` stub scripts); `RemoteTests` allows `RUN_REMOTE` class-wide (a fake `ssh` on `PATH`); single tests in `RestartTests`, `SettleTests` and `ShutdownStatusTests` allow the one leaf they reach over a faked subprocess.

**Files:**
- `scripts/shutdown/test_support.py` — `ShutdownSendLeaf`, `ShutdownTestCase`, the leaf stubs, `_REAL_LEAF_BINDINGS`, `allow_leaf`.
- `scripts/shutdown/test_send_guard.py` — each leaf blocked with no `subprocess.run` / `Popen` call, a caught error still failing, `dry_run=True` passing, `allow_leaf` in a test and in `setUp`, a test's own patch, `RUN_REMOTE` restoring all four bindings, an unknown leaf name refused.
- `scripts/shutdown/test_inventory.py`, `test_record.py`, `test_remote.py`, `test_restart.py`, `test_settle.py`, `test_shutdown_cli.py`, `test_shutdown_status.py`, `test_stop.py` — derive from `ShutdownTestCase`.

**Binds later work:** A new `TestCase` in `scripts/shutdown/` derives from `ShutdownTestCase` and calls `super().setUp()` first.

**Gotchas:**
- `settle`, `restart` and `shutdown` import `run_remote` by name, so `RUN_REMOTE` stubs and restores four bindings: `remote.run_remote`, `settle.run_remote`, `restart.run_remote`, `shutdown.run_remote`. A new module that imports `run_remote` by name, or a new send leaf, needs an entry in `_REAL_LEAF_BINDINGS` and a stub in `setUp`, or it goes unguarded.
- `_REAL_LEAF_BINDINGS` holds the real functions captured when `test_support` is imported, so `allow_leaf` restores them even inside a case nested in another guarded case.
- An uncaught hit is reported twice (the stub's error, then the cleanup's failure); assert the outcome, not a failure count.
- A proof case defines its `ShutdownTestCase` subclass inside the test method and runs it into a `unittest.TestResult()`; a module-level subclass would be discovered and run on its own. `ShutdownTestCase` has no `test_*` methods, so `discover` importing `test_support.py` adds no tests.
- `alert_user` returns before sending for a `selected` scope; only an account-wide scope (`record.AllAccountSessions`) reaches `DELIVER_MESSAGE`.
- `test_shutdown_cli.py`'s child processes (`shutdown.py launch-blocked`, `launch_permission.py exec-new-work`) run outside the guard; neither reaches a leaf.
- `test_settle.py`'s fake send script reads stdin on every call, `retire` included, so with an open stdin its rollback tests each wait out the 60 s retire timeout.

**Ruled out:** guarding calls that end or start sessions (`stop.py`'s `systemctl` / `launchctl` / `tmux kill-session` / `codex_mesh.py stop` and signals, `restart._add_unit_command`, the launches in `restart._run`) or the read-only `git`, `ps`, `tmux capture-pane` and `github-warm-status` probes — none delivers a message.

### Phase 7 — `broadcast.py` takes its groups from the roster · status: done

#### As-built

- `broadcast.py` selects recipients from `roster.roster()` rows by identity, never by name: a unit's workers carry `UnderDirector(<that director's address>)`, a showrunner's units `UnderShowrunner(<its address>)`; `RunsProduction`, `DirectsUnit`, `ProductionWithoutRunningShowrunner` and `NoDirector` rows join no such group.
- `BroadcastGroup` (closed, in `broadcast.py`) is not the roster's `Role`, because flags overlap. `broadcast.broadcast(sender, texts: Mapping[BroadcastGroup, str])`; `codex_winddown.announce()` passes `BroadcastGroup` keys. Flags: `--showrunners`; `--units`; `--agents` (every worker and freestanding session, Claude and Codex); `--workers TEXT`, narrowed by a preceding `--of <unit, director name, or session: address>` (unknown: usage error naming the known units; two matching directors: exit 2 listing their addresses); `--top-level TEXT` (Claude sessions neither a unit director under a showrunner nor a worker); `--all`, after which a role flag keeps its own text. A row several flags select gets one delivery, with the last selecting flag's text.
- Delivery is `send.py --to <address>` from the `Messageable` reach, never `--action`/`--no-action`. A `CannotReceiveMessages` row prints `send.py`'s line (reason, then its unit director or that none runs) and is neither sent nor a failure. A roster problem prints `broadcast: roster: <problem>` and the rest sends. Each selected group with no member besides the sender prints `<group> - none on this machine` before `N sent in S s`.
- `roster.sender_identity(env) -> SenderIdentity = CodexThread | ClaudeSession | NoSession` (frozen dataclasses). `CODEX_THREAD_ID` wins, since a Codex shell inherits its launching session's `CLAUDE_CODE_SESSION_ID`. `CodexThread` matches a row's `thread`, `ClaudeSession` its `session` (a socketless sender is still left out), `NoSession` nothing. `broadcast.py`, `top_level.py` and the roster's "this session" mark use it.
- `RosterEntry` gains `thread: CodexThread | NoCodexThread` (from the mesh record or `codex:` address; `--json` `thread`, id or `null`) and `session: ClaudeSession | NoClaudeSession` (newest running session record; no `--json` field). Link addresses, nesting, sort and `--of` use `roster._session_address(entry)` on `session`, so a socketless unit director still owns its workers.
- A running Claude session whose newest record has no messaging socket is a row with `CannotReceiveMessages("no messaging socket")`: `send.py` refuses it by name (exit 2), `top_level.py` prints `not reachable:`.
- `sessions.py` adds `running_session(record)` (pid exists), `messaging_socket_exists(record)` and `running_sessions()` (newest first); `live_session`/`live_sessions()` still mean running with a socket.
- `top_level.py` lists Claude rows that are not unit directors under a showrunner, not workers, not the sender (`NoSession` leaves nobody out), as `<name>\tuds:<socket path>` for `/notify_top_level`'s SendMessage; `build_hold.py record-recipient` gets the row's session id. Same-name sessions are two lines; one conversation in two processes is one line, from the newest record. Roster problems go to stderr as `top_level: roster: <problem>`, exit 0.

**Files:**
- `scripts/production/broadcast.py` — `BroadcastGroup`, selection and delivery
- `scripts/message/roster.py` — `SenderIdentity`, `RosterEntry.thread`/`.session`, socketless rows, link addresses
- `scripts/message/sessions.py` — running and live session readers
- `scripts/message/top_level.py` — top-level set from the roster
- `scripts/production/codex_winddown.py` — `announce()` with `BroadcastGroup` keys
- tests: `scripts/production/test_{broadcast,codex_winddown}.py`, `scripts/message/test_{roster,top_level,sessions}.py`

**Binds later work:**
- `running_session` checks only the pid, so a stale socketless record with a reused pid reads as running; "a session with no messaging socket is listed only while its own process runs" proves it by `procStart`.
- Both machines: `--snapshot` carries `session` and `thread`; the remote run is `NoSession`; the empty-group line prints per machine; `send.py`'s director tail on a refusal fits only workers.
- `docs/as-built/build-memory-admission.md:178` still describes `top_level.py`'s old reader and its usage error.

**Gotchas:**
- Tests that fake both session readers hide the socket cases; socket fixtures build real session records.
- `scripts/shutdown/test_inventory.py` fakes `sessions.live_session`, so `live_sessions()` must select through that predicate.

**Ruled out:** name matching for membership or sender exclusion; a `--json` field for `RosterEntry.session`.

### Phase 8 — The shutdown inventory tests see their sessions again · status: done

#### As-built

`sessions._readable_records()` yields every readable registry record. `running_sessions()` keeps those `running_session` accepts and `live_sessions()` those `live_session` accepts, each newest `updatedAt` first. `live_session` is `running_session(record) and messaging_socket_exists(record)`, so the pid check lives in `running_session` alone. Production results are unchanged.

**Files:**
- `scripts/message/sessions.py` — the session registry readers
- `scripts/message/test_sessions.py` — regression test that `live_sessions()` selects through a patched `live_session`

**Binds later work:** a change to `running_session` keeps `_readable_records()` and per-predicate selection in both readers.

**Gotchas:** `scripts/shutdown/test_inventory.py` fakes `sessions.live_session` to admit dead-pid fixtures, so `live_sessions()` must call that predicate per record. Suites outside the message package (build_hold, model_study rerun, shutdown) read `live_sessions()`.

**Ruled out:** `live_sessions()` as `running_sessions()` filtered by the socket check (hides the predicate from fakes).

### Phase 9 — A session with no messaging socket is listed only while its own process runs · status: done

#### Work Order

**Goal:** a leftover Claude session record whose pid now belongs to another process is not a roster row, while a running Claude session with no messaging socket still is.

**Spec:**
- Phase 7 made a running Claude session with no messaging socket a roster row, `CannotReceiveMessages("no messaging socket")`. `running_session(record)` checks only that the pid exists, so a record left by a session that died without removing it becomes such a row once another process takes its pid. That phantom is reported by `broadcast.py` as a session that was not told, is listed by `top_level.py` as not reachable, and makes a bare name ambiguous in `send.py` (exit 2) when a new session takes the dead one's name.
- `sessions.py` owns the process-start reader, moved from `scripts/shutdown/inventory.py` `_process_start`: `process_start(pid: int) -> str | None` reads field 22 of `/proc/<pid>/stat` on Linux, and `ps -o lstart= -p <pid>` under `LC_ALL=C` and `TZ=UTC` on macOS, the form Claude Code writes `procStart` in. `inventory.py` uses it in place of its copy, with the same behavior; one copy of the reader remains.
- `SessionRecord` keeps `procStart`. `running_session(record)`: the pid exists, and either the record's messaging socket exists or its `procStart` equals `process_start(pid)`. A record with no socket and a missing or empty `procStart` is not running. `live_session` stays `running_session(record) and messaging_socket_exists(record)`, so `live_sessions()` and every caller that needs a socket keep today's results.
- Cost: on Linux one `/proc` read per socketless record; on the Mac one `ps` per socketless record, usually none.
- Regression tests, each named for the behavior it pins and each failing on the tree before the repair: a socketless record whose pid runs with a different start is in neither `running_sessions()` nor the roster; `top_level.py` lists no such phantom. Also: a socketless record whose start matches is a `CannotReceiveMessages("no messaging socket")` row, and a record with a socket and no `procStart` is still live. The macOS start-format test moves to `test_sessions.py` with the reader if it no longer exercises `inventory.py`.

**Files:**
- `scripts/message/sessions.py`
- `scripts/message/test_sessions.py`
- `scripts/message/test_roster.py` — fixtures for socketless rows carry a matching start.
- `scripts/message/test_top_level.py` — the same.
- `scripts/shutdown/inventory.py` — uses `sessions.process_start`.
- `scripts/shutdown/test_inventory.py` — only to move the macOS start-format test.

**Seats:** `1 writer`.
- `impl` — every file above; hub: `sessions.py`.

**Constraints from prior phases:**
- Phase 8's readers: one private reader of readable records; `running_sessions()` selects through `running_session` and `live_sessions()` through `live_session`, both newest `updatedAt` first.
- `scripts/shutdown/test_inventory.py` patches `inventory._process_start`, `sessions.live_session` and `unit_lookup.live_session`; those patches keep working. Every shutdown `TestCase` derives from `test_support.ShutdownTestCase`.
- Phase 7's socketless row is `CannotReceiveMessages("no messaging socket")`, and `RosterEntry.session` comes from the newest running record.

**Acceptance gate:** the Test and Lint lines green. Live, read only: on natedev `live_sessions()`, `running_sessions()` and `roster.py --json` give the same sessions as on the tree before the phase, so no running session is lost. No Pushover post during the run.

### Phase 10 — Both machines · status: todo

#### Work Order

**Goal:** `roster.py` lists natedev and the Mac in one run; `send.py` reaches any address or name found only on the other machine; `broadcast.py` reaches both machines, each recipient once.

**Spec:**
- **Scope is named.** `RosterScope = ThisMachine | AllMachines` (frozen dataclasses in `roster.py`); `roster(environment, scope: RosterScope) -> Roster`, with `scope` required.
  - `roster.py` (the command) reads `AllMachines`; `--here` reads `ThisMachine`.
  - `broadcast.py` and `top_level.py` read `ThisMachine`: `broadcast.py` reaches the other machine by running itself there, and `top_level.py` lists this machine only (`/notify_top_level` adds other-machine peers from ListAgents).
  - `send.py` reads `ThisMachine` first and the other machine only when nothing here matches.
- **`AllMachines`** runs `roster.py --here --snapshot` on `remote.other_machine()` over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --snapshot'`, in parallel with the local read, and merges the entries, this machine first.
  - `--snapshot` prints `RosterSnapshot`, a lossless tagged JSON form of `Roster` used only between machines and hidden from `--help`: every reach, relationship, thread and session variant carries its tag and its fields, `CannotReceiveMessages.reason` and `RosterEntry.session` (`ClaudeSession | NoClaudeSession`) included. It is decoded at once into the same variants, so a remote `RosterEntry` equals the one that machine built, and a socketless unit director there keeps its linked workers. `--json` stays the flat public form, unchanged, with no session field.
  - The remote run has neither `CODEX_THREAD_ID` nor `CLAUDE_CODE_SESSION_ID`, so its sender is `NoSession` and it leaves nobody out: the sender is not on that machine.
  - ssh failure, a timeout (20 s) or output that does not decode as a snapshot is one problem line, `<host>: not reached: <why>`, and exit 1.
  - A snapshot that decodes is used whatever the remote exit status: a remote exit 1 with problems still gives its rows, and each of its problems comes through prefixed by the machine's name.
- **Routing in `send.py`.** The argparse boundary builds `RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine(host)` (frozen dataclasses), replacing `Options.machine: str | None`: `--here` is `LocalOnly`, `--machine HOST` is `NamedMachine(HOST)`, neither is `LocalThenPeer`, and `--here` with `--machine` is a usage error. Every send this machine makes on the other over ssh passes `--here`, so the other machine never asks back. `uds:` is always this machine's socket and never asks the other machine.
  - `LocalThenPeer`, this machine first for every form: a match here is sent here and the other machine is not asked, so a bare name on both machines reaches this machine's session, and `--machine <host>` or the address reaches the other one. An address that matches no row here, or a bare name that matches no row in a clean local roster (no `Roster.problems`), is looked up in the other machine's roster; exactly one `Messageable` row there sends through `NamedMachine(host)` with its address (action flags forwarded only for `user`); several rows there are Phase 3's ambiguity; a bare name matching only a `CannotReceiveMessages` row there gives Phase 3's `cannot take messages` line with that machine's reason; an unreachable machine is the `not reached` problem line and the send fails.
  - A bare name with no match here while the local roster has problems is not looked up on the other machine, because absence here is not proven: the problems are printed and the send exits 2 asking for an address or `--machine`.
  - A `CannotReceiveMessages` row has no address, so a `seat:` address of a seat between turns matches no row on either machine and keeps Phase 3's `no roster session has address` line.
  - **One refusal text.** `send._cannot_receive_message()` and `broadcast._unreachable()` both append unit-director advice to every refusal today, including a Claude session with no messaging socket, for which no director is expected. One function builds the text for both. Its tail follows the row: a worker under a director gets `contact its unit director <name> or wait`; a worker with `NoDirector` gets no tail, because its reason already says no unit director is running; a Claude session with `no messaging socket` gets `wait for it to open its messaging socket, then read the roster again and send again`. `send.py` refuses with exit 2; `broadcast.py` reports the line and does not fail.
  - **Recipient selection is named.** `Options.session_dir: str | None` with `codex: bool` becomes, at the argparse boundary, `RecipientSelection = RosterLookup(to) | MeshSeat(session_dir, seat)` (frozen dataclasses) beside `RecipientResolutionScope`; the legacy `--codex --session-dir` flags keep working. A `MeshSeat` names one machine's session directory, so it never asks the other machine: under `LocalOnly` or `LocalThenPeer` it is delivered here, and under `NamedMachine(host)` it is forwarded over ssh with `--here`.
  - A clean miss on the other machine falls back to Phase 3's local outcome for that form: a `session:` address is kept in the queue here (exit 1), a `codex:` address goes to this machine's daemon, a `seat:` address is `no roster session has address` (exit 2), and a bare name gets Phase 3's no-match result.
- **`broadcast.py`** runs `broadcast.py --here` on the other machine over ssh with the same groups and texts, and prints its lines under the host's name. The closing line names both machines. `--here` keeps it to this machine and runs no ssh. An unreachable machine is one `NOT sent` line for that machine. When `BROADCAST_SEND` is set here, the ssh command sets it on the other machine to the same path, so a recorder selected here is selected there too; a path missing there fails the send there and delivers nothing.
  - **One orchestration for both entry points.** `broadcast.broadcast()`, which `codex_winddown.announce()` calls, and the command line run the same local-plus-other-machine path, so wind-down announcements reach both machines; `codex_winddown.py` itself is unchanged.
  - **`--of` is resolved once across both machines.** `--of session:<id>` or a director's name is matched against both machines' unit directors before anything is delivered: exactly one director gives that director's machine and its resolved identity, and the run on that machine selects by that identity; a name matching directors on both machines is refused before any delivery, listing their addresses; no match is the unknown-unit error. A director's address or name is never replayed unchanged to the other machine, where it can mean nothing or a different director.
  - **Each delivery stays on the roster row's machine.** Every per-row `send.py` call passes `--here`, so a recipient that leaves between the roster read and its send is a failed delivery here and never a lookup on the other machine.
  - `<group> - none on this machine` is printed per machine, the other machine's under its host name.
- The Mac has no tmux: there, unit directors come from the worktree match, and the tmux problem line is left out on a machine with no `tmux` binary.
- **Tests** fake `ssh`, recording each call. Named cases: a Claude session, a mesh worker and a daemon session each found only on the other machine are sent through `--machine` with their address; one delivery per recipient when both machines answer; a name on both machines is sent here with no ssh call; `--here` on `roster.py`, `broadcast.py` and `send.py` makes no ssh call; `top_level.py` makes no ssh call; an unreachable machine is one problem line and the local rows still print; a remote snapshot with a problem and exit 1 gives its rows and its prefixed problem; every reach and relationship state round-trips through `--snapshot`, a remote refusal reason included; each `RecipientResolutionScope` case, and `--here` with `--machine` as a usage error; every ssh send carries `--here`; a clean miss on the other machine gives each form's Phase 3 local outcome; a bare name absent from a local roster with problems asks no other machine and delivers to no same-named session there; a `seat:` address of a seat between turns on the other machine says `no roster session has address`; the refusal tail for a worker under a director, a worker with `NoDirector` (no tail) and a Claude session with no messaging socket, through `send.py` (exit 2) and `broadcast.py` (reported, not a failure); `BROADCAST_SEND` reaches the ssh command only when set here. Also: `--of` for a director only on this machine, one only on the other, and two directors sharing a name across the machines (refused before any delivery); `codex_winddown.announce()` reaching both machines for each of its groups through fake ssh; every per-row delivery carries `--here`, and a recipient that disappears after the roster read causes no lookup on the other machine; a socketless unit director with linked workers, `RosterEntry.session` and each thread variant round-trip through `--snapshot`; the remote run leaves nobody out; each machine's `<group> - none on this machine` line under its host; each `RecipientSelection` with each scope.

**Files:**
- `scripts/message/roster.py`
- `scripts/message/test_roster.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/production/test_codex_winddown.py` — wind-down announcements reach both machines.
- `scripts/message/top_level.py` — passes `ThisMachine`.
- `scripts/message/test_top_level.py`

**Seats:** `1 writer + 1 tester`.
- `impl` — `roster.py`, `send.py`, `broadcast.py`, `top_level.py`; hub: `roster.py` (`RosterScope`, `RosterSnapshot` and the aggregate read).
- `test` — `test_roster.py`, `test_send.py`, `test_broadcast.py`, `test_codex_winddown.py`, `test_top_level.py`, from the Spec alone.

**Constraints from prior phases:**
- Phase 3's addresses (`session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`), `RosterEntry.reach`, and `send.py`'s resolution order and exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, or a refusal, 3 failed.
- Phase 5's `process_table` module; `roster.py` does not import `broadcast`.
- Phase 7's groups (`--showrunners`, `--units`, `--agents`, `--workers` with `--of`, `--top-level`, `--all`) as `BroadcastGroup`, membership by `UnderDirector` / `UnderShowrunner` addresses, the sender left out by `SenderIdentity` (`CODEX_THREAD_ID`, else `CLAUDE_CODE_SESSION_ID`), `RosterEntry.thread`, roster problems printed while the rest sends, and `top_level.py`'s `<name>\tuds:<socket path>` lines.
- Phase 7's `RosterEntry.session: ClaudeSession | NoClaudeSession`, from the newest running record (`sessions.running_sessions()`, newest first); the sender is matched on it, and link addresses (`UnderDirector.address`, the nesting, `--of`) come from it through `roster._session_address(entry)`, so a socketless unit director still owns its workers. A running Claude session with no messaging socket is a row with `CannotReceiveMessages("no messaging socket")`. A selected group with no member besides the sender prints `<group> - none on this machine` before the count. `top_level.py` with no session id leaves nobody out.
- Phase 9: a socketless record is running only when its `procStart` matches its process, so a phantom never takes part in local-first resolution.
- `scripts/shutdown/remote.py` `other_machine()` maps `natedev` and `mac` to each other.

**Acceptance gate:** the Test and Lint lines green. Live: `roster.py` on natedev lists the Mac's sessions; `roster.py --here` on the Mac exits 0 or 1 with no ssh. `broadcast.py`'s dry check copies a recorder that records its arguments to the same scratch path on both machines and sets `BROADCAST_SEND` to it; both recorders capture their machine's recipients, each recipient once across the two, no message is sent, and the closing line names both machines. One scratch background Claude session on the Mac receives `send.py --to <its name>` from natedev and is then removed.

### Phase 11 — Claude and Codex sessions know which tool to use · status: todo

#### Work Order

**Goal:** a Claude session reads in `/message` when to use ListAgents and SendMessage and when to use the roster. Every Codex session on both machines reads in its `AGENTS.md` how to list and message any session, and how to message the user.

**Spec:**
- `commands/message.md` gains a short "Finding sessions" section:
  - ListAgents and SendMessage for Claude sessions;
  - `roster.py` for Codex workers, for who is whose unit director and showrunner, and for the other machine;
  - `send.py --to <address>` for anything the roster lists;
  - `broadcast.py` and its groups for many at once.

  The Sending bullet's `--codex --session-dir` wording becomes "by its `seat:` address or seat name". The section also says what each refusal means and what to do, in the same words as `codex/AGENTS-messaging.md` below.
- `commands/announce.md` (`--all`: showrunners, unit directors, Codex workers and every other agent), `commands/unit/announce.md` (`--units`: unit directors) and `commands/showrunner/announce.md` (`--showrunners`: showrunners) each say the message reaches both machines; each description names exactly the members its flag selects, so only `commands/announce.md` names Codex workers. `commands/notify_top_level.md` keeps its steps and its `uds:` wording; Phase 7 kept `top_level.py`'s output. Its "Unit directors are left out" becomes: unit directors under a showrunner are left out, since their showrunner passes the message on, and a unit director with no showrunner is included.
- `codex/AGENTS-messaging.md` (new, in this repo) is the Codex text, short:
  - list every session with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py`;
  - message one with `send.py --to <address> --from <your session name>`, a group with `broadcast.py`;
  - message the user with `send.py --to user --summary … --text …` and `--action "<what the user does>"` or `--no-action`; emergency only when the user must act now; `REFUSED:` (exit 2) means fix the action flags as the line says and send again, with no PushNotification fallback; action flags go only with `--to user`;
  - what each refusal means and what to do: `cannot take messages (<reason>)` (exit 2) — a seat between turns is resumed only by its unit director, so contact that director or wait; `cannot take messages (no messaging socket)` (exit 2) — a Claude session that has not opened its messaging socket yet, so wait, read the roster again and send again; a name matching several sessions (exit 2) lists their addresses, so send again with one of them; `no roster session has address` (exit 2) — the address is stale or its seat is between turns, so read the roster again; `FAILED:` (exit 3) from a Codex queue failure or timeout gets no fallback and is reported;
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
- `broadcast.py` groups: `--showrunners`, `--units`, `--agents`, `--workers` (with `--of <unit>` before it, resolved once across both machines), `--top-level`, `--all`; both machines unless `--here`. It leaves its sender out by `CODEX_THREAD_ID`, else `CLAUDE_CODE_SESSION_ID`, so a Codex session needs no flag for that.
- `roster.py` lists both machines unless `--here`; a machine not reached is one stderr line and exit 1; `--json` gives `{"entries": [...], "problems": [...]}`.

**Acceptance gate:** the Test and Lint lines green; `commands/message.md` and `codex/AGENTS-messaging.md` both say what `REFUSED:` means and that it gets no fallback, and both name `cannot take messages`, `cannot take messages (no messaging socket)`, the ambiguous-name address list, `no roster session has address`, recovery of a seat between turns through its unit director, and `FAILED:` with no fallback, each with its recovery; each announce command's description names exactly the members its flag selects, and `commands/notify_top_level.md` says which unit directors it leaves out; the block installed on both machines, with the Mac's Rust rules unchanged (`diff` of the text outside the block is empty). Live: a scratch Codex daemon thread on natedev, started with `ephemeral: false` (an ephemeral thread refuses queued messages and cannot be archived), asked to list every Claude session and its role, runs `roster.py`, and its answer matches `roster.py --json` at that moment, saying so when no showrunner is registered. A queued request is checked in `thread/turns/list` once the turn starts, since `thread/read` does not show it. Then it is archived.
