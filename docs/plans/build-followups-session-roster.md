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

#### As-built

`sessions.process_start(pid: int) -> str | None` is the one process-start reader: on Linux it reads `/proc/<pid>/stat` as bytes, splits after the last `)` and returns field 22 when it is all digits; on macOS it runs `ps -o lstart= -p <pid>` under `LC_ALL=C` and `TZ=UTC` with a 5 s timeout. `SessionRecord.procStart` is `NotRequired[str]`, and `read_session` fills it (empty when the record has none). `running_session(record)` holds when the pid exists and either the messaging socket exists or a nonempty `procStart` equals `process_start(pid)`; `live_session` is unchanged. A socketless record whose pid now runs another process, or that has no start, is not a roster row, a `broadcast.py` "not told" line, a `top_level.py` unreachable line or a cause of name ambiguity in `send.py`. `scripts/shutdown/inventory.py` imports the reader as `_process_start`, so its patch point stays.

**Files:**
- `scripts/message/sessions.py` — session registry readers and the process-start reader
- `scripts/message/test_sessions.py` — start mismatch, missing and empty start, socket record with no start, Linux field-22 and macOS formats, truncated UTF-8 names, malformed stat
- `scripts/message/test_roster.py`, `scripts/message/test_top_level.py` — phantom exclusion; socketless fixtures carry a real start
- `scripts/shutdown/inventory.py`, `scripts/shutdown/test_inventory.py` — use `sessions.process_start`; the macOS format test lives in `test_sessions.py`

**Binds later work:** a socketless record is running only while its `procStart` matches its process, so a phantom never takes part in local-first resolution; `sessions.process_start` is the one reader. The macOS `ps -o lstart=` path is covered only with a faked `ps`, so the both-machines roster's Mac live check is its first native run.

**Gotchas:** read `/proc/<pid>/stat` as bytes: the kernel's 15-byte `comm` limit can cut a process name inside a UTF-8 character. `procStart` is `NotRequired` because `production/test_codex_winddown.py` builds records without it; callers read `record.get("procStart", "")`. When `process_start` returns `None` (malformed stat, `ps` failure), a socketless record is not running until its start reads again.

**Ruled out:** making `procStart` required (it reaches `production/test_codex_winddown.py` to remove a spelling no caller observes).

### Phase 10 — Both machines · status: done

#### As-built

- `roster.py`: `RosterScope = ThisMachine | AllMachines` (frozen dataclasses); `roster(environment, scope: RosterScope) -> Roster`, `scope` required. The command reads `AllMachines`, `--here` reads `ThisMachine`. `AllMachines` runs `roster.py --here --snapshot` on `remote.other_machine()` over `ssh -o BatchMode=yes -o ConnectTimeout=10` (20 s timeout) in parallel with the local read and merges the entries, this machine first. `--snapshot` (hidden from `--help`) prints `RosterSnapshot`, a lossless tagged JSON form decoded into the same variants (`CannotReceiveMessages.reason`, `RosterEntry.session`, each thread variant), so a socketless unit director there keeps its linked workers; `--json` stays the flat public form with no session field. The remote run has no session env, so its sender is `NoSession` and it leaves nobody out. ssh failure, timeout or an undecodable snapshot is one problem line `<host>: not reached: <why>`, exit 1; a snapshot that decodes is used whatever the remote exit status, its problems prefixed by the machine's name.
- Unit directors: with tmux readable they come from tmux only — the marked pane's live Claude, else the running non-worker record in that marked pane; a unit with no marked session has no director. The Units-row worktree match runs only when tmux is unreadable or absent (the Mac); with no `tmux` binary the tmux problem line is left out.
- `send.py`: the argparse boundary builds `RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine(host)` and `RecipientSelection = RosterLookup(to) | MeshSeat(session_dir, seat)` (frozen dataclasses). `--here` is `LocalOnly`, `--machine HOST` is `NamedMachine(HOST)`, neither is `LocalThenPeer`, both together is a usage error; `--codex --session-dir` still work. Under `LocalThenPeer` a match here is sent here and the other machine is not asked. An address with no row here, or a bare name with no match in a clean local roster, is looked up there: one `Messageable` row sends through `NamedMachine(host)` with its address (action flags forwarded only for `user`); several rows are the ambiguity; only a `CannotReceiveMessages` row gives `cannot take messages` with that machine's reason; unreachable is the `not reached` line and the send fails; a clean miss gives each form's local outcome (`session:` queued here exit 1, `codex:` to this machine's daemon, `seat:` `no roster session has address` exit 2, bare name no-match). A bare name with no match here while the local roster has problems asks no other machine: the problems print and it exits 2 with `absence of <name> here is not proven; use an address or --machine`. Every ssh send passes `--here`. A `MeshSeat` never asks the other machine: delivered here, or forwarded over ssh with `--here` under `NamedMachine`.
- One function builds the refusal text for `send.py` (exit 2) and `broadcast.py` (reported, not a failure). Its tail follows the row: a worker under a director gets `contact its unit director <name> or wait`; a worker with `NoDirector` gets none; a Claude session with `no messaging socket` gets `wait for it to open its messaging socket, then read the roster again and send again`.
- `broadcast.py`: `broadcast()` (called by `codex_winddown.announce()`) and the command line run one orchestration — the local groups plus `broadcast.py --here` on the other machine over ssh with the same groups and texts, its lines printed under the host name. The replay spells an all-workers group with the internal `--all-workers` flag (`main` accepts it, `USAGE` omits it) so it does not inherit a preceding unit scope. `--of` is resolved once across both machines before any delivery: one director gives its machine and resolved identity; a name on both is refused with `<target> matches more than one unit director:` and their addresses; none is the unknown-unit error. Every per-row `send.py` call passes `--here`, so a recipient gone since the roster read is a failed delivery here. An unreachable machine is one `NOT sent` line; `<group> - none on this machine` prints per machine, the other's under its host; the closing line names both machines; `--here` runs no ssh. A `BROADCAST_SEND` set here is set on the other machine to the same path; a path missing there fails the send there.
- `top_level.py` reads `ThisMachine` and runs no ssh.

**Files:**
- `scripts/message/roster.py` — the roster across both machines, `RosterScope`, `RosterSnapshot`, director identification
- `scripts/message/send.py` — local-first routing across machines, the shared refusal text
- `scripts/production/broadcast.py` — groups across both machines, the `--here` replay
- `scripts/message/top_level.py` — this machine only
- `scripts/message/test_roster.py`, `scripts/message/test_send.py`, `scripts/message/test_top_level.py`, `scripts/production/test_broadcast.py`, `scripts/production/test_codex_winddown.py` — fake `ssh` that records each call

**Binds later work:** the lines "Claude and Codex sessions know which tool to use" quotes, with their recovery: `<host>: not reached: <why>` (exit 1; rerun outside the sandbox or with `--here`) and machine-prefixed remote problems; `absence of <name> here is not proven; use an address or --machine` (exit 2; send again with an address or `--machine`); the `--machine` `FAILED` return (Gotchas); `<target> matches more than one unit director:` (pass one address); the three refusal tails; `NOT sent`, `<host>: <group> - none on this machine` and the two-machine closing line. `roster.py` lists both machines unless `--here`; `--here` keeps `roster.py`, `send.py` and `broadcast.py` to this machine. `--all-workers` appears in no command file or Codex text.

**Gotchas:** a remote usage error or refusal under `--machine` returns as `FAILED: ssh <host>: <reason>` exit 3, because `remote()` reads only the last stdout line's outcome word. `uds:` is always this machine's socket, even with `--machine`. A `CannotReceiveMessages` row has no address, so a `seat:` address of a seat between turns matches no row on either machine. The Mac has no tmux. The ssh command runs `$HOME/.claude/scripts/...`, the other machine's main checkout, so a branch's changes reach the other machine only once merged and pulled there.

**Ruled out:** replaying a director's `--of` address or name unchanged to the other machine (it can mean nothing or a different director there); running the worktree match while tmux answers (it names a director tmux does not show).

### Phase 11 — Claude and Codex sessions know which tool to use · status: todo

#### Work Order

**Goal:** a Claude session reads in `/message` when to use ListAgents and SendMessage and when to use the roster. Every Codex session on both machines reads in its `AGENTS.md` how to list and message any session, and how to message the user.

**Spec:**
- `commands/message.md` gains a short "Finding sessions" section:
  - ListAgents and SendMessage for Claude sessions;
  - `roster.py` for Codex workers, for who is whose unit director and showrunner, and for the other machine;
  - `send.py --to <address>` for anything the roster lists;
  - `broadcast.py` and its groups for many at once.

  The Sending bullet's `--codex --session-dir` wording becomes "by its `seat:` address or seat name". The section also says what each refusal means and what to do, and how to read results that are partly missing, in the same words as `codex/AGENTS-messaging.md` below.
- `commands/announce.md` (`--all`: showrunners, unit directors, Codex workers and every other agent), `commands/unit/announce.md` (`--units`: unit directors) and `commands/showrunner/announce.md` (`--showrunners`: showrunners) each say the message reaches both machines; each description names exactly the members its flag selects, so only `commands/announce.md` names Codex workers. `commands/notify_top_level.md` keeps its steps and its `uds:` wording; Phase 7 kept `top_level.py`'s output. Its "Unit directors are left out" becomes: unit directors under a showrunner are left out, since their showrunner passes the message on, and a unit director with no showrunner is included.
- `codex/AGENTS-messaging.md` (new, in this repo) is the Codex text, short:
  - list every session with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py`;
  - message one with `send.py --to <address> --from <your session name>`, a group with `broadcast.py`;
  - message the user with `send.py --to user --summary … --text …` and `--action "<what the user does>"` or `--no-action`; emergency only when the user must act now; `REFUSED:` (exit 2) means fix the action flags as the line says and send again, with no PushNotification fallback; action flags go only with `--to user`;
  - what each refusal means and what to do: `cannot take messages (<reason>)` (exit 2) — a seat between turns is resumed only by its unit director, so contact that director or wait; `cannot take messages (no messaging socket)` (exit 2) — a Claude session that has not opened its messaging socket yet, so wait, read the roster again and send again; a name matching several sessions (exit 2) lists their addresses, so send again with one of them; `no roster session has address` (exit 2) — the address is stale or its seat is between turns, so read the roster again; `FAILED:` (exit 3) from a Codex queue failure or timeout gets no fallback and is reported; `absence of <name> here is not proven; use an address or --machine` (exit 2) — this machine's roster had problems, so the other machine was not asked and nothing was sent: send again with an address or `--machine HOST`; with `--machine HOST`, a refusal or an ambiguous name on that machine comes back as `FAILED: ssh <host>: <reason>` (exit 3): act on the reason as for the same refusal here; `broadcast.py --of <unit>` matching unit directors on both machines is refused before anything is delivered with `<unit> matches more than one unit director:` and their addresses (exit 2): send again with `--of` and one of those addresses;
  - reading results that are partly missing: `roster.py` still prints every row it read when a machine is not reached (one stderr line `<host>: not reached: <why>`, exit 1), so use the rows and say which machine is missing; `broadcast.py` prints one line per recipient ending in `sent`, `no live session` or `NOT sent: <why>`, the other machine's lines under its host name, and `<group> - none on this machine` when a machine has no member, which is not a failure; report delivery only from `sent` lines; `--here` keeps `roster.py`, `send.py` and `broadcast.py` to this machine, `--of` included;
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
- `send.py` exit codes: 0 sent, 1 queued for a Claude session, 2 a usage error, an ambiguous name, a session that cannot take messages, an address no roster session has, or a refusal (`REFUSED:`), 3 failed. `--here` keeps a send to this machine; `--machine HOST` sends from that machine; neither looks here first, then on the other machine. With `--machine HOST`, an exit-2 outcome on that machine (refusal, ambiguous name, usage error) returns here as `FAILED: ssh <host>: <reason>`, exit 3 (`send.py` `remote()`). `uds:<path>` is always this machine's socket, even with `--machine`. A bare name with no match here while this machine's roster has problems exits 2 with `absence of <name> here is not proven; use an address or --machine` and asks no other machine. `--to user` needs `--action TEXT` or `--no-action`; `--need decision` and `--need blocked` need `--action`.
- `broadcast.py` groups: `--showrunners`, `--units`, `--agents`, `--workers` (with `--of <unit>` before it, resolved once across both machines), `--top-level`, `--all`; both machines unless `--here`. It leaves its sender out by `CODEX_THREAD_ID`, else `CLAUDE_CODE_SESSION_ID`, so a Codex session needs no flag for that. `--of <unit>` matching unit directors on both machines exits 2 before any delivery with `<unit> matches more than one unit director:` and their addresses. Output: one line per recipient, `<name> - <role> - sent | no live session | NOT sent: <why>`, the other machine's lines prefixed `<host>: `, `<group> - none on this machine` for a machine with no member, then `<N> sent in <S> s` and a closing line naming the machines. `--all-workers` is the internal spelling of the replay to the other machine; it is not in `USAGE` and no command file or Codex text names it.
- `roster.py` lists both machines unless `--here`; a machine not reached is one stderr line `<host>: not reached: <why>` and exit 1, and the rows read still print; the other machine's problems come prefixed with its name; `--json` gives `{"entries": [...], "problems": [...]}`.
- Phase 10 is on main (merged 14f2669) and pulled on both machines; a plain `roster.py` from natedev's main checkout listed a live Mac session (2026-10-10). So `~/.claude/scripts/message/` on each machine is the Phase 10 code the installed text points at.

**Acceptance gate:** the Test and Lint lines green; `commands/message.md` and `codex/AGENTS-messaging.md` both say what `REFUSED:` means and that it gets no fallback, and both name `cannot take messages`, `cannot take messages (no messaging socket)`, the ambiguous-name address list, `no roster session has address`, recovery of a seat between turns through its unit director, and `FAILED:` with no fallback, `absence of <name> here is not proven`, a `--machine` refusal returning as `FAILED: ssh <host>:`, and the `--of` name matching unit directors on both machines, each with its recovery; both say how to read partly missing results (roster rows with a machine not reached; `sent`, `NOT sent` and `none on this machine` in broadcast output) and that `--here` keeps each tool to this machine, and neither names `--all-workers`; each announce command's description names exactly the members its flag selects, and `commands/notify_top_level.md` says which unit directors it leaves out; the block installed on both machines, with the Mac's Rust rules unchanged (`diff` of the text outside the block is empty). Live: a scratch Codex daemon thread on natedev, started with `ephemeral: false` (an ephemeral thread refuses queued messages and cannot be archived), asked to list every Claude session and its role, runs `roster.py`, and its answer matches `roster.py --json` at that moment, saying so when no showrunner is registered. A queued request is checked in `thread/turns/list` once the turn starts, since `thread/read` does not show it. Then it is archived.
