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

- **Project:** `~/.claude` — Claude Code commands, skills and scripts. This plan adds the session roster (Phase 1), a gate that makes every message to the user say what the user does (Phase 2), sends to any roster name (Phase 3), sends to roster groups (Phase 4), covers both machines (Phase 5), and teaches Claude and Codex sessions which tool to use (Phase 6). Work in the worktree `/home/natepiano/worktrees/claude-build-followups-shutdown` on branch `build-followups-shutdown` (unit `shutdown-unit` of production `build-followups`).
- **Project started:** 2026-10-10T16:30:07.804+00:00
- **Stack:** Python 3.13 standard library only; bash, zsh; codex-cli 0.162.0 (daemon 0.162.1).
- **Layout:**
  - `scripts/message/roster.py` — the roster (new, Phase 1; both machines Phase 5)
  - `scripts/message/codex_daemon.py` — reads the shared Codex daemon's live sessions; queues to one (new, Phase 1; queue Phase 3)
  - `scripts/notify/pushover.py` — the message-to-user gate (Phase 2)
  - `scripts/message/send.py` — `--action` / `--no-action` for the user (Phase 2); any roster name (Phase 3); the other machine by name (Phase 5)
  - `scripts/production/broadcast.py` — groups from the roster (Phase 4); both machines (Phase 5)
  - `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py` — instructions (Phase 6)
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

#### Work Order

**Goal:** `roster.py` prints, for this machine, every showrunner, unit director, worker and freestanding session, Claude and Codex. Each worker shows its unit director, and each unit director its showrunner. `--json` gives the same as records.

**Spec:**
- `scripts/message/roster.py`, standard library only. It is runnable as `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py [--json] [--kind claude|codex]` and importable: `roster(environment) -> Roster`, where `Roster` holds `entries: list[RosterEntry]` and `problems: list[str]`.
- `RosterEntry`, a frozen dataclass:
  - `machine` (short hostname), `kind` (`claude` | `codex`), `role` (`showrunner` | `unit director` | `worker` | `freestanding`).
  - `name`, `status` (Claude: the record's `status`; Codex worker: `running`, `idle` for `done`, `failed`, `starting`, `waiting for capacity`, `out of capacity`; daemon session: its `status.type`).
  - `address`: what `send.py --to` takes; Phase 3 makes every one of them work. Claude: `session:<id>`. Codex worker: its seat name. Daemon session: `codex:<thread id>`.
  - `cwd`.
  - `production` (slug, or empty), `showrunner` (its session name now, or empty), `unit` (the Units row id, or empty), `director` (the unit director's session name, for a worker).
  - `slot_role` (a worker's `impl` / `test` / `fix` / `review`, plus `lens` when present, or `role unknown`).
  - `session_dir` (a worker's run directory, or empty).
- **Claude sessions:** every live record (`sessions.live_sessions()`, one row per `sessionId`, the newest record winning), this session included and marked `(you)` in text. Role, first match wins:
  1. **showrunner:** its `sessionId` is a registered showrunner's (`showrunners.registered_showrunners()`); `production` is its slug.
  2. **unit director:**
     - under a production: a Units row of a registered showrunner's doc has the session as its live Claude (`live_units.production_units(doc)`). `unit`, `production` and `showrunner` come from that row.
     - when tmux cannot be asked (`OSError`): a Units row whose Worktree cell equals the session's `cwd`, with a stderr problem line that tmux was not read.
     - with no production: a run marker in `/tmp/claude/delegate/active/` named for its `sessionId`; `production`, `unit` and `showrunner` stay empty.
  3. **worker:** a live run's `seats` ledger (below) holds its background id, which `agent_bg.sh` writes as `<id>\t<seat name>`; the id is the start of the `sessionId`, matched as `agent_bg.sh` matches it; `director` is that run's director, and `unit`, `production` and `showrunner` are the director's. `slot_role` is the seat name's last `-` part.
  4. **freestanding:** anything else.
- **Live runs:** the session directories under `/tmp/claude/delegate/` (overridable by `ROSTER_DELEGATE_ROOT`) whose `active/` marker names a director that is a live Claude session, or whose `heartbeat.log` changed within `remove_seats.LIVE_HEARTBEAT_SECS`. A run's director is the live session the marker names, or none.
- **Codex workers:** each entry of a live run's `mesh_roster.json` whose `status` is not `ended`, and whose run server (`mesh_server.json` `pid`) or `launcher_pid` is alive. `director`, `unit`, `production` and `showrunner` come from the run's director. A running blind reviewer is a worker with `slot_role` `review` (plus its lens) and an empty `address`; it takes no messages. It is found from a live run's `review_status[_<lens>]` reading `reviewing` with a live `review_pid[_<lens>]`; its kind is that of the `codex exec` or `claude` process under that pid.
- **Codex daemon sessions:** `scripts/message/codex_daemon.py` `loaded_sessions(socket_path) -> list[DaemonSession] | DaemonUnreadable`.
  - `DaemonSession` holds `thread_id`, `name`, `cwd` and `status`.
  - It runs the WebSocket handshake over the Unix socket at `~/.codex/app-server-control/app-server-control.sock` (overridable by `ROSTER_CODEX_SOCKET`), then `initialize` with `clientInfo {name: "roster", version: "1"}`, the `initialized` notification, `thread/loaded/list` (following `nextCursor`), and `thread/read` for each id. 5 s timeout in all.
  - No socket means none. Any other failure is a `DaemonUnreadable(reason)` problem line.
  - Each loaded thread whose id is not a Codex worker's `thread_id` is a `freestanding` Codex session.
- **Text form**, grouped by machine, one line per session, indented two spaces per level:
  - Each showrunner, then under it each of its unit directors, then under each director its workers.
  - Then unit directors with no showrunner, with their workers.
  - Then workers whose director is not running, under the line `director not running: <session dir>`.
  - Then freestanding Claude sessions, then freestanding Codex sessions under the line `your Codex sessions`.
  - The line format is `<role> <name> — <kind>, <status>[, <slot_role>][, <unit>][, <cwd shown with ~>]`.
  - Each problem line, prefixed `could not read: `, goes to stderr.
  - Exit 0 when every source was read; exit 1 when the roster printed with problems.
- `--kind` keeps only that kind's rows. A Claude or Codex parent row needed to place a worker still prints, marked `(context)`. `--json` prints `{"entries": [...], "problems": [...]}`.
- Tests (`scripts/message/test_roster.py`, `scripts/message/test_codex_daemon.py`) build fixtures in temp directories: session records with a live pid (the test's own) and a socket file, showrunner timers, a production doc with a Units table, run markers, seats ledgers, mesh rosters (with and without `role`, `ended` entries, a dead server pid), a fake `tmux` and `ps` on `PATH`, and a fake daemon (a Unix socket server in a thread that speaks the handshake and the three calls). Cases:
  - every role;
  - a worker under its director and showrunner;
  - an `ended` seat left out;
  - a dead run's seats left out;
  - a stale marker of a dead director;
  - a daemon thread that is also a worker's thread, listed once as the worker;
  - no daemon socket;
  - a daemon that closes mid-call (a problem line, exit 1);
  - tmux unavailable (falling back to the worktree match);
  - `--kind codex` with context rows;
  - `--json`.

**Files:**
- `scripts/message/roster.py` — new.
- `scripts/message/codex_daemon.py` — new.
- `scripts/message/test_roster.py` — new.
- `scripts/message/test_codex_daemon.py` — new.
- `pyrightconfig.json` — `scripts/production` and `scripts/delegate` on the `scripts/message` environment's `extraPaths`, if the imports need it.

**Seats:** `1 writer + 1 tester` — `impl` writes `roster.py` and `codex_daemon.py`. `test` writes both test files from this Spec alone (no test lane outside `scripts/message`).

**Acceptance gate:** the Test and Lint lines green. A live read-only check on natedev: `roster.py` exits 0 or 1 and lists natedev as showrunner with its units under it, at least one Codex worker under its unit director (or none when no run has seats open, which the check reports), and the user's open Codex windows under `your Codex sessions`. `--json` parses.

### Phase 2 — Every message to the user says what the user does · status: todo

#### Work Order

**Goal:** no message reaches the user's phone unless it says the action the user takes, or says that no action is needed. A message that does neither is refused before it leaves, and its sender gets the reason and the exact fix. Emergency priority is kept for messages the user must act on now.

**Spec:**
- **The gate lives in `scripts/notify/pushover.py`**, the one channel every path ends in: `send.py --to user`, `escalate.py`, `/alert_user`, and seats that call `pushover.py` directly.
  - Usage becomes `pushover.py [--priority 0|1|2] (--action TEXT | --no-action) TITLE MESSAGE`. Passing both is refused.
  - **A message with neither is still sent in this phase**, with stderr line `pushover: warning: no --action or --no-action; this will be refused once Phase 7 lands` and log outcome `sent without an action line`. Two senders outside this repo (`/etc/nixos/modules/linux/ups.nix`, `disk-floor.nix`) call `pushover.py` from the nix store and change only when the user rebuilds; the UPS alert must never be refused. Phase 7 turns the missing case into a refusal once natedev confirms the rebuilt generation passes the flags.
  - `--action TEXT` is what the user does, in their words: "Run github-warmup on natedev". Text that is empty, or only a placeholder (`none`, `n/a`, `nothing`, `no action`, `-`, any case), is refused: use `--no-action`.
  - Priority: `--no-action` sends at priority 0 only. Priority 1 (high) and 2 (emergency, repeats until acknowledged) need `--action`. Emergency is for an action the user must take now; the docstring says so.
  - The posted message's first line is `Action: <TEXT>` or `No action needed.`, then the message. When the whole is over `MESSAGE_MAX`, the message body is cut, never the action line.
  - Every other check refuses in this phase: an empty or placeholder `--action`, `--no-action` above priority 0, both flags. A refused message is not sent. It exits 2, prints one stderr line, `pushover: refused before sending: <reason>. Say what the user does with --action "<what they do>", or pass --no-action when nothing is needed (priority 0 only).`, and logs the attempt with outcome `refused before sending: <reason>`, so the showrunner can see who was refused.
- **`scripts/message/send.py --to user`** takes the same `--action TEXT` or `--no-action`, one of them required (a usage error, exit 2, with the same fix in its text). `--need decision` and `--need blocked` require `--action`; `--need note` takes either. It passes the choice to `pushover.py`, and `remote()` forwards it to the other machine. A refusal is exit 2, not `FAILED` (exit 3): nothing is kept, and the sender's PushNotification fallback does not fire for it.
- **`scripts/message/escalate.py hold`** takes `--action TEXT` or `--no-action` and stores it with the held message. `deliver()` passes it to `send.py`. A record held before this phase has neither: it is refused at delivery like any other message and stays held, so the caller's next `hold`, which carries the action, replaces it.
- **Every sender in this repo states its action.** Each one passes `--action` with the action its message already names, or `--no-action` when the message asks nothing of the user. A `--no-action` sender sends at `--need note`. The writer lists each sender's choice in its summary:
  - `scripts/buildlog/rust_release.py` (a release note);
  - `scripts/lint/sweep.py` (the disk-floor alert);
  - `scripts/production/ci_points.py` (the review watch);
  - `scripts/production/codex_winddown.py` (its text names the action: reset Codex, then `/codex_winddown clear`);
  - `scripts/shutdown/settle.py` and `scripts/shutdown/restart.py` (an account is down: the `/shutdown restart` action; an account is back: no action).
- **Instructions.** `commands/alert_user.md`, `commands/builds.md`, `commands/fix.md`, `commands/watcher.md` and `commands/showrunner/produce.md` (`<Notify/>`) show `--action` or `--no-action` in every command. Each says: every message says what the user does or that nothing is needed; emergency (`--need blocked`) only when the user must act now. The PushNotification fallback on `FAILED` carries the same `Action:` or `No action needed.` first line.
- **Tests never push.** Every test fakes the post or the subprocess, and no test reads the real keys file.

**Files:**
- `scripts/notify/pushover.py`, `scripts/notify/test_pushover.py`
- `scripts/message/send.py`, `scripts/message/test_send.py`
- `scripts/message/escalate.py`, `scripts/message/test_escalate.py`
- `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`
- `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`, `scripts/production/test_dailies_input.py`
- `scripts/production/codex_winddown.py`, `scripts/production/test_codex_winddown.py`
- `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py`
- `commands/alert_user.md`, `commands/builds.md`, `commands/fix.md`, `commands/watcher.md`, `commands/showrunner/produce.md`

**Seats:** `1 writer + 1 tester` — `impl` writes every script and command above; `test` writes every test file above, from this Spec alone.

**Constraints from prior phases:** none; this phase touches no roster file. Phase 3 and Phase 5 change `send.py` after this one.

**Acceptance gate:** every test line green: `python3 -m unittest discover -s <dir> -p 'test_*.py'` for `scripts/notify`, `scripts/message`, `scripts/shutdown`, and `-p` for `test_rust_release.py` (`scripts/buildlog`), `test_sweep.py` (`scripts/lint`), `test_ci_points.py`, `test_codex_winddown.py` and `test_dailies_input.py` (`scripts/production`). basedpyright on every changed directory reports 0/0/0. Live, with nothing sent to the phone: `--priority 2 --no-action` and `--action none` each exit 2 with the reason and log a `refused before sending` line; `send.py --to user --need blocked --no-action …` exits 2.

### Phase 3 — `send.py` reaches any session the roster lists · status: todo

#### Work Order

**Goal:** `send.py --to <address or name>` delivers to a Codex worker by seat name and to a user's Codex session by `codex:<thread id>` or exact name, as it already does to a Claude session.

**Spec:**
- Before its Claude path, `send.py` resolves `--to` through `roster.roster()` when the address is not `user`, `uds:` or `session:`. Resolution order:
  1. a Codex worker whose seat name equals `--to`: it is sent through the existing `codex()` path with that worker's `session_dir`, so `--codex --session-dir` is no longer needed. Both are still accepted, and they skip the lookup.
  2. `codex:<thread id>`, or a daemon session whose `name` equals `--to`: sent by `codex_daemon.queue(thread_id, text)`, which runs `codex queue --thread <id> --message <text>` with a timeout. Exit 0 → `SENT: queued on Codex session <name>`. Otherwise `FAILED` (exit 3) with codex's first stderr line; nothing is kept, as for a seat.
  3. otherwise the Claude path, unchanged.
- One name matching more than one row (two live runs with one seat name, a worker and a daemon session): exit 2, `send.py: <name> matches <n> sessions:` followed by each one's address and role, and nothing sent.
- The roster is read once per send. A roster that cannot be read leaves the Claude path as it is today, and a Codex name then fails with the roster's problem line.
- The docstring and `--help` name the new address forms.

**Files:**
- `scripts/message/send.py`
- `scripts/message/codex_daemon.py` — `queue`.
- `scripts/message/test_send.py`
- `scripts/message/test_codex_daemon.py`

**Seats:** `1 writer + 1 tester` — `impl` writes `send.py` and `codex_daemon.queue`; `test` writes the tests, faking `codex` and `codex_mesh.py` the way `test_send.py` already fakes its subprocesses.

**Constraints from prior phases:** Phase 1's `roster()`, `RosterEntry.address` and `codex_daemon` module; Phase 2's `--action` / `--no-action` in `send.py`'s user path.

**Acceptance gate:** the Test and Lint lines green. Live, scratch recipients only:
- a scratch mesh thread in a scratch session directory gets `send.py --to <seat name>` without `--session-dir`;
- a scratch daemon thread (started with `thread/start`, `ephemeral: true`, over the socket, then archived) gets `send.py --to codex:<id>` and shows the queued message in `thread/read` or its queue;
- an ambiguous name exits 2.

### Phase 4 — `broadcast.py` takes its groups from the roster · status: todo

#### Work Order

**Goal:** every group send reaches the roster's members of that group, Claude and Codex, and two new groups exist: workers (all, or one unit's) and top level.

**Spec:**
- `recipients()` reads `roster.roster()` in place of its own session, unit and `codex_seats` code, which is removed.
- Groups:
  - `--showrunners`: role showrunner.
  - `--units`: role unit director.
  - `--agents`: every worker and every freestanding session, Claude and Codex, the user's Codex sessions included.
  - `--workers TEXT`: every worker. With `--of <unit, or unit director name>` placed before it, only that unit's workers; an unknown unit is a usage error naming the known ones.
  - `--top-level TEXT`: every Claude session that is not a unit director under a showrunner and not a worker, matching `top_level.py`'s set.
  - `--all`: every role. A role's own flag after `--all` still gives that role its own text.
- Every recipient is sent through `send.py --to <address>`, so a Codex worker goes by seat name and a daemon session by `codex:<id>`. A row with an empty address (a blind reviewer) is reported `cannot take messages` and is not counted as a failure.
- The closing line still says who was sent the message, using the new role names.
- `top_level.py` lists from `roster.roster()`, with the same output and the same `build_hold.py record-recipient` call, so the two never disagree.

**Files:**
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`
- `scripts/message/top_level.py`
- `scripts/message/test_top_level.py`

**Seats:** `1 writer + 1 tester` — `impl` writes `broadcast.py` and `top_level.py`; `test` writes both tests.

**Constraints from prior phases:** Phase 1's roles and addresses; Phase 3's `send.py` address forms.

**Acceptance gate:** the Test and Lint lines green; a live dry check (`BROADCAST_SEND` pointed at a script that records its arguments) lists the recipients for each flag on natedev, with no message sent.

### Phase 5 — Both machines · status: todo

#### Work Order

**Goal:** `roster.py` lists natedev and the Mac in one run; `send.py` reaches a name found only on the other machine; `broadcast.py` reaches both machines.

**Spec:**
- `roster.py` runs itself on `remote.other_machine()` over `ssh -o BatchMode=yes -o ConnectTimeout=10 <host> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --json'`, in parallel with the local read. It merges the entries, this machine first. `--here` skips the other machine.
  - ssh failure, a timeout (20 s) or unparseable JSON is one problem line, `<host>: not reached: <why>`, and exit 1.
  - The other machine's own problems come through, prefixed by its name.
- `send.py --to <name>` with no row here and exactly one row on the other machine sends through the existing `--machine` path. Rows on both machines are the ambiguity of Phase 3.
- `broadcast.py` runs `broadcast.py --here` on the other machine over ssh with the same flags and texts, and prints its lines under the host's name. The closing line names both machines. `--here` keeps it to this machine. An unreachable machine is one `NOT sent` line for that machine.
- The Mac has no tmux: there, unit directors come from the worktree match, and the tmux problem line is left out on a machine with no `tmux` binary.

**Files:**
- `scripts/message/roster.py`
- `scripts/message/test_roster.py`
- `scripts/message/send.py`
- `scripts/message/test_send.py`
- `scripts/production/broadcast.py`
- `scripts/production/test_broadcast.py`

**Seats:** `1 writer + 1 tester` — `impl` writes the three scripts; `test` writes the three tests with `ssh` faked.

**Constraints from prior phases:** Phases 1–4.

**Acceptance gate:** the Test and Lint lines green. Live: `roster.py` on natedev lists the Mac's sessions; `roster.py --here` on the Mac exits 0 or 1 with no ssh. `broadcast.py`'s dry check covers both machines. One scratch background Claude session on the Mac receives `send.py --to <its name>` from natedev and is then removed.

### Phase 6 — Claude and Codex sessions know which tool to use · status: todo

#### Work Order

**Goal:** a Claude session reads in `/message` when to use ListAgents and SendMessage and when to use the roster. Every Codex session on both machines reads in its `AGENTS.md` how to list and message any session.

**Spec:**
- `commands/message.md` gains a short "Finding sessions" section:
  - ListAgents and SendMessage for Claude sessions;
  - `roster.py` for Codex workers, for who is whose unit director and showrunner, and for the other machine;
  - `send.py --to <address>` for anything the roster lists;
  - `broadcast.py` and its groups for many at once.

  The Sending bullet's `--codex --session-dir` wording becomes "by seat name".
- `commands/announce.md`, `commands/unit/announce.md` and `commands/showrunner/announce.md` say the message reaches both machines and Codex workers. `commands/notify_top_level.md` keeps its steps; only its recipients line changes, if Phase 4 changed what `top_level.py` prints.
- `codex/AGENTS-messaging.md` (new, in this repo) is the Codex text, short:
  - list every session with `~/.claude/scripts/lib/py ~/.claude/scripts/message/roster.py`;
  - message one with `send.py --to <address> --from <your session name>`, a group with `broadcast.py`;
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

**Seats:** `2 writers` — `impl` writes the install script and its test; `test` writes the command text and the Codex text.

**Constraints from prior phases:** Phases 1–5: the address forms, groups, `--here`, and exit codes.

**Acceptance gate:** the Test and Lint lines green; the block installed on both machines, with the Mac's Rust rules unchanged (`diff` of the text outside the block is empty). Live: a scratch Codex daemon thread on natedev, asked to list every Claude session and its role, runs `roster.py` and answers with the showrunner and its units. Then it is archived.

### Phase 7 — A message with no action stated is refused · status: todo

**Blocked by:** natedev confirming that the rebuilt `/etc/nixos` generation's `ups.nix` and `disk-floor.nix` pass `--action` or `--no-action` to `pushover.py` (natedev makes the edit; the rebuild is the user's). Resequence earlier the moment natedev confirms.

#### Work Order

**Goal:** the transition warning from Phase 2 becomes the refusal: a message to the user that says neither its action nor that none is needed does not leave.

**Spec:**
- `pushover.py` with neither `--action` nor `--no-action` is refused like Phase 2's other cases: exit 2, the same stderr fix line, log outcome `refused before sending: no action stated`. The warning line and the `sent without an action line` outcome are removed.
- `send.py --to user` with neither is a usage error (exit 2), the same fix in its text.
- Before dispatch, the unit director checks the pushover log since the Phase 2 checkpoint for `sent without an action line` entries; any sender still in that list is updated in this phase or named to natedev.

**Files:** `scripts/notify/pushover.py`, `scripts/notify/test_pushover.py`, `scripts/message/send.py`, `scripts/message/test_send.py`

**Seats:** `1 writer + 1 tester` — `impl` writes the two scripts; `test` writes the two tests.

**Constraints from prior phases:** Phase 2's flags and refusal text.

**Acceptance gate:** `python3 -m unittest discover -s scripts/notify -p 'test_*.py'` and `-s scripts/message` green; basedpyright 0/0/0 on both; live: `pushover.py "t" "m"` exits 2 and logs the refusal, with nothing sent.
