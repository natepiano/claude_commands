# Session roster and messaging

> **Status: as-built.**

## What it is

One roster lists every session of interest on natedev and the Mac: Claude showrunners, unit directors, workers and freestanding sessions; Codex workers with their unit director and showrunner; and the user's own Codex windows across account daemons. `scripts/message/roster.py` only lists. The existing senders take their recipients from it: `send.py` reaches any one listed session by address or unique name, on either machine, and `broadcast.py` reaches groups (showrunners, unit directors, workers, a unit's workers, agents, top-level sessions, everyone) on both machines. Each kind of session lists its own kind natively where it can and uses the roster for the rest; the instruction texts say which tool to use. Every message to the user passes one gate, `scripts/notify/pushover.py`, which refuses a message that neither states the user's action (`--action`) nor says none is needed (`--no-action`). A headless reviewer then checks each push for clarity before it is posted (`docs/as-built/message-clarity.md`). Shutdown tests run under a guard that fails a test, instead of sending, when it reaches a real send.

### Data flow

```
this machine's records ──► roster._local_roster ──┐
other machine: ssh roster.py --here --snapshot ──►├─► Roster(entries, problems)
                (AllMachines only)                │
                                                  ├─► roster.py           text | --json
                                                  ├─► send.py             one recipient ──► postmaster | relay | codex_mesh send
                                                  │                                         | thread/queue/add | pushover.py
                                                  │                                         | ssh send.py --here
                                                  ├─► broadcast.py        groups ──► send.py --to <address> --here per row
                                                  │                                  + ssh broadcast.py --here
                                                  └─► top_level.py        ThisMachine ──► /notify_top_level SendMessage
```

### Record sources

The roster joins records that each name one part of the picture. A session's role comes from these records, never from its name; the one value read from a name is a Claude worker's slot role, the suffix of its worker ledger worker name.

| Source | Where | Gives |

|---|---|---|

| Showrunner registrations | notifier timers under `~/.local/state/notifier/showrunner-*` (`NOTIFIER_STATE_DIR`), via `showrunners.registered_showrunners()` | session name, socket, production slug, production doc |

| Units table | the production doc, via `production_units.unit_table` without retired rows (`production_units.unit_is_retired`) | `Unit` and `Worktree` per row (fallback only) |

| Run markers | `/tmp/claude/unit/active/<director session id>`, and the same under `/tmp/claude/delegate/` for a run started before the folder rename (`ROSTER_RUN_ROOT` replaces both) | first line: the run's session directory |

| Run heartbeats | `<run>/heartbeat.log` | a run is live if written within `unit_runs.LIVE_HEARTBEAT_SECS` (600 s) |

| Workers ledgers | `<run>/workers`, one `<background id>\t<worker name>` per `agent_bg.sh` launch | Claude workers |

| Mesh roster | `<run>/mesh_roster.json`, `<run>/mesh_server.json` | Codex workers: `thread_id`, `status`, `role`, `lens`, `launcher_pid`, `cwd`; server `pid` |

| Held role store | `~/.local/state/astra/design-roles/<design id>/design.json` and its `mesh/mesh_roster.json` (`design_roles.state_root()`, `DESIGN_ROLES_STATE`) | held Astra designers and reviewers: holder, lifecycle, thread, mesh entry |

| Review state | `<run>/review_status[_<lens>]`, `<run>/review_pid[_<lens>]` | running blind reviewers |

| Codex account daemons | each account home from `agent_accounts.codex_homes`, at `<home>/app-server-control/app-server-control.sock` (`ROSTER_CODEX_SOCKET` replaces the list for tests) | loaded threads: id, name, cwd, status, owning daemon |

| Process table | `ps -eo pid=,ppid=,comm=,args=` (`PROCESS_TABLE_PS`) | Codex agent processes, reviewer descendants, worker `--cwd` |

The mesh roster format belongs to `scripts/agents/codex_mesh.py` (see `docs/as-built/long-lived-workers.md`); the roster reads it and never writes it.

### How each row's role is derived

A live run is a run directory with a marker whose director session is running, or a heartbeat younger than 600 s (then it has no running director).

Claude rows come from `sessions.running_sessions()`, one per `sessionId` (newest `updatedAt`). A headless `claude -p` run, whose record's `entrypoint` starts with `sdk-`, is never running there. The relay is one: it takes its sender's name with `-n`, so without this every live relay was a row named like its sender. First match wins:

Codex rows:

- **worker** — each mesh entry of a live run whose status is not `ended` and whose server pid or `launcher_pid` is alive. Slot role from the record's `role`: `review (<lens>)` for a `review` role with a lens, `no role` when absent, `role unknown` when unrecognized. Status reads `done` → `idle`, `waiting_capacity` → `waiting for capacity`, `capacity_exhausted` → `out of capacity`, others as stored. cwd from the record, else the agent process's `--cwd`, else the director's cwd. Director and showrunner links come from the run; a heartbeat-only run gives `NoDirector` / `NoShowrunner`.
- **held Astra role** (`_durable_roles`) — each open held role in the held role store (`docs/as-built/long-lived-workers.md#lasting-roles`), skipping a role not opened or ended and a thread fenced by an adoption (`transferring_role`, `role_redirect`, `design_roles.transfer_route`). Role `astra-designer` or `astra-reviewer`, name the instance name, status the mesh entry's, else the lifecycle name; cwd the design's checkout. A unit holder gives `UnderDirector`; a showrunner holder gives `UnderShowrunner` and `NoDirector`. Its account reads `AccountUnread("serving home not read")`.
- **blind reviewer** — `review_status[_<lens>]` reads `reviewing` with a live `review_pid[_<lens>]`. Kind is `codex` for a `codex exec` descendant of that pid, else `claude` for a `claude` descendant, else `codex`. Named `<lens> reviewer` or `blind reviewer`, slot role `review (<lens>)` or `review`.
- **freestanding** — each daemon thread that is not a worker's or held role's thread (such a thread is listed once, as the worker or held role).

### Domain types (`roster.py`)

- `RosterEntry` (frozen): `machine`, `kind` (`claude` | `codex`), `role` (`showrunner` | `unit director` | `worker` | `astra-designer` | `astra-reviewer` | `freestanding`), `name`, `status`, `reach`, `cwd`, `showrunner`, `director`, `assignment`, `thread`, `session`, `account` (`AccountLabel`, `AccountUnread`, or `AccountAttributionUnavailable`).
- `reach: Messageable(address) | CannotReceiveMessages(reason)`.
  - Claude: `session:<id>` with a messaging socket, else `CannotReceiveMessages("no messaging socket")`.
  - Codex worker: `worker:<run id>/<worker name>` (run id = the run directory's name) at mesh status `running` / `waiting_capacity`; otherwise `between turns; its unit director resumes it` or `between turns; no unit director is running`.
  - Daemon session: `codex:<thread id>`.
  - Blind reviewer: `CannotReceiveMessages("blind reviewer")`.
  - Held Astra role: `CannotReceiveMessages("held role; its holder adds pending messages to an authorized assignment")`.
- `ShowrunnerRelationship = RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`; `DirectorRelationship = DirectsUnit | UnderDirector | NoDirector`; `Assignment = WorkerAssignment | NotAWorker`.
- `thread: CodexThread | NoCodexThread` (mesh record or daemon thread); `session: ClaudeSession | NoClaudeSession` (the Claude identity, kept even without a socket).
- Link addresses, text nesting, sort and `--of` use `_session_address(entry)` on `session`, so a socketless unit director still owns its workers.
- `Roster(entries, problems)`; `RosterScope = ThisMachine | AllMachines`; `roster(environment, scope) -> Roster` (scope required).
- `SenderIdentity = CodexThread | ClaudeSession | NoSession` from `sender_identity(environment)`: `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`. `is_sender(entry, identity)` matches a Codex row's `thread` or any row's `session`; `NoSession` matches nothing.
- `is_top_level(entry)`: a Claude row that is not a worker and not a unit director `UnderShowrunner`.
- `cannot_receive_message_text(entry)`: the one refusal text for `send.py` and `broadcast.py`, `<name> cannot take messages (<reason>)` plus a tail: a worker `UnderDirector` gets `; contact its unit director <name> or wait`, a worker with `NoDirector` none, a socketless Claude session `; wait for it to open its messaging socket, then read the roster again and send again`.

### Unit labels (`roster.py`)

`roster.py` holds the one rule that names a session's showrunner and unit for build views and copy folders. Callers never derive these names themselves.

- `UnitLabels(session, showrunner="—", unit="—")` is frozen, and each field is whitespace-collapsed. Showrunner is the production slug and unit is the unit id without its `-unit` suffix, read through `_production_of` and `_unit_of`, the same helpers `shown_name` uses through `_production(entry)` and `_unit(entry)`; `_short_unit` is the one place that strips `-unit`. A production with no running showrunner still names its slug. These are the short names herdr shows (production `builds`, unit `report-unit` gives `builds`, `report`), never a session name. `—` marks a missing part.
- `unit_labels(entries) -> dict[str, UnitLabels]` keys a full roster's entries by Claude session id and Codex thread id. `/build:queue` uses it (`docs/as-built/build-memory-admission.md`, "Queue API").
- `unit_labels_for(session_ids, environment) -> dict[str, UnitLabels]` is the fast path for known Claude session ids. It reads the session records, the showrunner registry through `showrunners.registered_showrunners(known_sessions=...)`, and unit marks through `unit_lookup.session_unit_marks(records, environment)`, which reads only each record's `/proc/<pid>/environ` (`HERDR_PROC` replaces `/proc`). It never reads the process table, Codex meshes or accounts. A session whose `CLAUDE_CODE_SESSION_ID` names another known session resolves under that director and takes its production and unit. Unknown ids are left out, and any error returns `{}`. `/build:status` uses it (`docs/as-built/build-targets.md`).
- Both paths group marks through `unit_lookup.group_marked_units`, the one slug-rename and duplicate-mark rule; a test pins that both paths give the same labels.
- `UnitLabels.folder_name() -> str` names the copies folder `/tmp/claude/copies/<showrunner>-<unit>/<phase>-<purpose>`: showrunner `hana`, unit `lfo` gives `hana-lfo`. A unit name equal to the showrunner name, or one that starts with the showrunner name and `-`, is the folder name alone (`cargo-liner-style` + `cargo-liner-style-types` gives `cargo-liner-style-types`). The prefix rule needs the hyphen: `hana` + `hanami` gives `hana-hanami`. A `—` part is left out with its hyphen (`builds`, `lfo`); with both parts `—` the session name is the folder name. A caller looks up labels with `unit_labels_for([<session id from build_hold.owning_session()>], environment)`, so a Codex worker resolves to its unit director. No caller uses `folder_name` yet.
- Imports: `roster.py` imports `scripts.shutdown.remote` by package and does not put `scripts/shutdown` on `sys.path`; `account`, `agent_accounts` and `note_file` load lazily, so a label-only caller does not load account discovery. `scripts/shutdown/inventory.py` appends its own folder after existing `sys.path` entries for its sibling imports.

### `roster.py` command

`roster.py [--json] [--kind claude|codex] [--here]`. Without `--here` it reads `AllMachines`: `ssh -o BatchMode=yes -o ConnectTimeout=10 <other machine> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --snapshot'` (20 s timeout, session identity variables removed) runs in parallel with the local read, and entries merge with this machine first.

- **Text** — per machine: a machine line, then showrunner → unit director → worker, unit directors with no running showrunner and their workers, `director not running: <session dir>` groups for workers with no listed director, freestanding Claude, and `your Codex sessions`. Each row: `<role> <name>[ (you)][ (context)] — <kind>, <status>[, <slot role>][, <unit>][, <cwd with ~>]`. `(you)` marks the sender's row on this machine. No pids or ports.
- **`--kind`** keeps rows of that kind and adds each listed worker's unit director and showrunner (same machine) as `(context)` rows; `--json` includes them unmarked.
- **`--json`** prints `{"entries": [...], "problems": [...]}`. Each entry is flat: `machine`, `kind`, `role`, `name`, `status`, `address`, `cwd`, `production`, `showrunner`, `showrunner_address`, `unit`, `director`, `director_address`, `slot_role`, `session_dir`, `thread` (id or `null`). Link fields are empty for no-link states and for a showrunner's or director's own row; `address` is empty for `CannotReceiveMessages`. There is no `session` field.
- **`--snapshot`** (hidden from `--help`) prints `RosterSnapshot`: tagged JSON that decodes back into the same variants, used only between machines.
- **Problems** go to stderr as `could not read: <source>: <detail>`; the other machine's are prefixed with its host; an unreachable machine, timeout or undecodable snapshot is `<host>: not reached: <why>`. A decodable snapshot is used whatever the remote exit status. Exit 0 clean, 1 with any problem.

### `send.py`: one recipient

```
send.py --to NAME|uds:<path>|session:<id>|worker:<run>/<worker>|codex:<thread>
        [--from NAME] [--summary TEXT] [--key KEY [--repeat-minutes N]]
        [--here | --machine HOST] [--codex --session-dir DIR] [--timeout SECONDS]
        (--text TEXT | --file PATH | stdin)
send.py --to user --summary TITLE [--need note|decision|blocked] (--action TEXT | --no-action) ...
send.py ack KEY | reopen KEY | pending [NAME] | retire --to ADDRESS --from SENDER --key KEY
send.py drain-sockets
```

The argparse boundary builds `RecipientSelection = RosterLookup(to) | MeshWorker(session_dir, worker)` and `RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine(host)`: `--here` is `LocalOnly`, `--machine HOST` is `NamedMachine`, neither is `LocalThenPeer`; the two together are a usage error. `Options.action: MessageUserAction = UserAction | NoUserRecipient`.

Routes (`resolve_recipient(to: str, scope: RecipientResolutionScope, observed: roster.Roster) -> DeliveryRoute`). `observed` holds the roster already read for this send:

| `--to` | Route |
|---|---|
| `user` | `pushover.py` here; under `--machine`, `send.py --here --to user ...` there |
| `uds:<path>` | Claude delivery on this machine, even with `--machine`; a missing relay socket or a delivery connect returning `ENOENT` or `ECONNREFUSED` resolves its saved sender name to a live session, else `FAILED` (exit 3); postmaster first when registered and available, else relay |
| any form, `--machine HOST` | `ssh HOST send.py --here ...` with no local roster read |
| `session:<id>` | Claude delivery; resolved to the live session's `uds:` socket first; postmaster first when available, else relay; no live session → `QUEUED` |
| `worker:<run>/<worker>` | that row's `codex_mesh.py send --session-dir <run dir> --to <worker>` |
| `codex:<thread>` | the loaded roster row carries its account daemon; `codex_daemon.queue(daemon, thread_id, text, timeout)` calls `thread/queue/add` on that socket. An unloaded thread fails (exit 3), naming the homes checked; multiple owners are ambiguous (exit 2), listing their accounts |
| bare name | roster rows whose names are equal after `str.casefold()`; one `Messageable` sends by its address |
| `--codex --session-dir DIR` | `MeshWorker`: `codex_mesh.py send` directly, no roster read |

Resolution for `session:`, `worker:`, `codex:` and bare names reads this machine's roster once (problems print as `send.py: roster: <problem>`). Address forms match `Messageable` addresses exactly; names match any row without regard to letter case, with no preference for an exact-case match. `send.py` imports `roster` only on that path, so acknowledgements (`ack`, `reopen`, `pending`, `retire`), `uds:` sockets and `--machine` sends start without it.

`post-tool-use-sendmessage-miss.py` runs after SendMessage. Only a failed "No agent named … is reachable" result for a name reads the roster. The hook and send.py share `matching_recipients(to, scope, observed)`: casefold matches here first, then on the peer after a proven local miss. The hook adds context with each matching session's kind, account, machine and address, plus the send.py command to run with the sandbox off. Multiple matches require one listed address; a clean miss or roster error is stated. The hook never sends or blocks; successes, socket addresses and other failures do not read the roster.

- A match here sends here; the other machine is not asked.
- Several matching rows: exit 2, `<name> matches <N> sessions:` with each address (or its `cannot take messages` reason) and role; Codex daemon rows also name their account and home.
- One `CannotReceiveMessages` row: exit 2 with `cannot_receive_message_text`.
- No match under `LocalThenPeer`: the other machine's roster is read. One `Messageable` row there sends through `ssh` with its address; ambiguity and `cannot take messages` apply to its rows too; a miss while it reports problems is `FAILED` with those problems. A bare name absent from a local roster that had problems asks no one: exit 2, `absence of <name> here is not proven; use an address or --machine`.
- A clean miss: `session:` queues here (exit 1), `codex:` fails with `no live Codex daemon has thread <id> loaded; checked <homes>`, `worker:` exits 2 with `no roster session has address <address>`, a bare name goes to the Claude relay by name.

Every ssh send runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" --here ...` with the text on stdin. `remote()` reads the outcome word from the last stdout line there, prefixing the detail `on <host>: `; a run that prints no outcome line (usage errors, `cannot take messages`, ambiguity, `no roster session has address`, which go to stderr) returns `FAILED: ssh <host>: <reason>`. Action flags are forwarded only for `--to user`.

Outcomes print as one line and log with the full text to `$XDG_STATE_HOME/message/log.jsonl` (default `~/.local/state/message`):

| Line | Exit | Meaning |
|---|---|---|
| `SENT: <how>` | 0 | delivered; the outbox path reports `postmaster; verbatim` or `postmaster; NOT VERBATIM`, then `session:<id>` |
| `SKIPPED: <why>` | 0 | key acknowledged, or inside its repeat window |
| `QUEUED: <why>` | 1 | a Claude recipient not reached; kept under its session id |
| `REFUSED: <why>` | 2 | `pushover.py` refused before sending; nothing kept |
| `HELD: <why>` | 4 | a user push not sent: it failed review (revise and resend), or another sender's claim on a legacy hold is live |
| `REPLACED: <why>` | 0 | a newer version replaced this user push; nothing was sent |
| `FAILED: <why>` | 3 | not delivered, not kept: an unresolved dead socket, a Codex recipient, the user, or ssh; for the user it ends with a `fallback text:` line |

The `--to user` outcomes in full, including `SENT: … rewritten:` and `sent despite failed review:`, are in `docs/as-built/message-clarity.md`.

Usage errors, the stderr-only refusals above, an unreadable `--file` and an empty message also exit 2.

Delivery mechanics: for a Claude recipient found in the live registry, `send.py` first tries the local postmaster. It waits up to 3 s for a result, withdrawing the entry before checking for a claim; an existing claim allows another 2 s for a result. A confirmed `delivered` result reports `SENT: postmaster; …`. Each miss requires the relay: `absent` (no heartbeat), `stale` (unreadable, older than 2 s or no live postmaster identity), `self` (recipient is the postmaster), `unclaimed` (withdrawn without a delivery), `outbox-error` (entry write failed), `failed`, `expired`, `claimed-no-result` (delivery unknown), and `relay-only-sender` (`shutdown`). A recipient not in the live registry uses the relay directly and logs `unresolved`. With no fresh heartbeat, the relay outcome, queue and text stay as before. After an attempt, the relay receives `max(1.0, options.timeout - attempt.seconds)`; a recipient not in the registry gets the original timeout.

The fallback is a headless `command claude -p` relay (sonnet, auto mode, tools `SendMessage` plus `ListAgents` for a name, hooks, MCP and slash commands off, `CLAUDE_*` variables removed, named `--from`); delivery is read from the `SendMessage` tool result in the stream, which stays in `STATE/relay/<to>.jsonl`. `--timeout` defaults to 40 s for every transport. Where `~/.config/claude/oauth-token.gpg` exists (the Mac, whose keychain ssh cannot open) the relay decrypts it, and queues with `gpg-agent is cold on <machine>: run github-warmup there` when it cannot. Undelivered Claude messages go to `STATE/queue/`, latest per `--key`, under the recipient's session id; `pending` prints and clears them. `--key` with `--repeat-minutes` skips a repeat within the window; `ack` skips every later send with the key until `reopen`.

The relay's launcher atomically publishes its sender in `STATE/relay_senders/<pid>` before execing Claude. Exec preserves the PID used in Claude's `cc-socks/<pid>.sock` address. A reply to a missing socket, or a delivery connect returning `ENOENT` or `ECONNREFUSED`, looks up that sender with `sessions.addressed(name, sessions.live_sessions())`. Send and drain use the same resolver; no extra connect probes a live socket. No record or no live named recipient produces a failed result and a stderr line naming the socket and sender. The queue boundary rejects raw sockets without a live session; sends carry the session ID found at their queue decision so a later session exit cannot create a raw socket queue.

`send.py drain-sockets` recovers raw `uds__*.sock.jsonl` queue files under a separate `.drain-sockets.lock`, so its relays do not block normal sends. It delivers each message in a recent file (newest entry under 24 hours old) to the live session, marked `Stuck message from <original time>:`. A saved relay sender takes precedence over queued recipient metadata. Each stable recovery ID gets a flushed and fsynced attempt in `STATE/socket-recovery-attempts.jsonl` before relay, then a durable result in `STATE/log.jsonl` before the queue checkpoint. The message log keeps one row per result; attempts do not count as messages. A rerun skips confirmed sends and retries known failures without a second queued copy. An attempt with no result is archived without resending, reports `UNCONFIRMED: stuck reply from <time> to <name> may have arrived; archived, not resent`, and exits 3 once. Its archive checkpoint preserves other unsent entries, and a retry reuses an identical archive instead of copying it. Unresolved or unreadable files stay in place with a report naming the file (exit 3); the drain continues with other files. Files at least 24 hours old move intact to `STATE/socket-queue-archive/`; one line reports the count and oldest and newest message times. Archive names never replace an existing file. `UNCONFIRMED` and `SENT` lines are printed and flushed before removing the queue entry; a rerun may repeat the report without sending again. The old-file summary (`archiving <n> socket queue files: …`) is printed and flushed before any of its files move, so an interrupted move cannot hide the count; a rerun names only the files still to move. `pending` keeps its existing print-and-clear behavior.

### Postmaster mod

Postmaster is a local Claude Code mod in `mods/postmaster/`, loaded directly with `--plugin-dir`; the trial start procedure loads `/home/natepiano/.claude/mods/postmaster` from main. It uses a Claude session named `postmaster` in `/home/natepiano/postmaster`. It reads and writes only under `STATE/postmaster/`, starts no process and makes no network call. `STATE` is `$XDG_STATE_HOME/message`, defaulting to `~/.local/state/message`.

The mod folder holds `.claude-plugin/plugin.json` (name `postmaster`, version 0.1.0, no marketplace field), `hooks/hooks.json` (`{"modules": ["./register.ts"]}`), `hooks/{register,protocol,outbox,replies}.ts`, `tests/*.test.ts` and `tsconfig.json`. `register.ts` alone touches `$`: `callsFor($)` builds `PostmasterCalls` from direct `$.noun.method(...)` call sites (`env.get` with the literal names `HOME` and `XDG_STATE_HOME`; `fs.read`, `write`, `exists`, `list`; `session.id`, `send`), `now` as `Date.now` and `every` over `globalThis.setInterval`. `outbox.ts` (`startOutbox(calls, replies): Promise<Outbox>`) and `replies.ts` (`startReplies(calls): Promise<Replies>`) work only through that call set, so the tests hand them a fake one. `protocol.ts` holds the JSON types, their validators, `POLL_MS` (500), `BATCH` (20), `VERSION`, `REPLY_WINDOW_MS` (30 min) and `PATHS`.

**Files and formats**

Each record is one JSON file. The engine refuses reads and writes over 4 MiB, so the mod never rewrites a growing log. `$.fs` has no rename, so a rewritten file can be read torn; the reply and forward records are each written once, so a reader that sees one after its write finished reads it complete. Timestamps ending in `_ms` are non-negative safe integer milliseconds; entry ids are 32 hex characters, either case, and hashes are 64 lowercase hex characters. A write creates its directory; the client creates `outbox/`. The mod lists only regular files named `<32 hex>.json`, so the client's `.<id>.json.tmp` and `.<id>.withdrawn` files are never read.

| Path under `STATE/postmaster/` | JSON fields and meaning |
|---|---|
| `outbox/<id>.json` | `{id, session_id, socket, text, sha256, deadline_ms, sender, reply_to_session_id}`; recipient id and `uds:` socket come from its live registry record; `sender` is non-empty |
| `claimed/<id>.json` | `{id, claimed_ms, run}`; `run` identifies the hooks worker as `<at_ms>-<uuid>` |
| `results/<id>.json` | `{id, outcome, detail, sha256, claimed_ms, finished_ms}`; outcome is `delivered`, `failed`, `expired`, `withdrawn` or `abandoned`; `sha256` is the mod's own hash of the text it passed to the send (of empty text for an unreadable entry) |
| `heartbeat.json` | `{session_id, at_ms, version, run}`; written each poll and before each send; version is `0.1.0`; non-empty `run` is the worker's `<at_ms>-<uuid>`, also carried by its claims |
| `delivered/<at_ms>-<n>.json` | `{route, socket, sender, reply_to_session_id, session_id, at_ms}`; `route` is `open` or `withdrawn`; names one delivery route |
| `replies/<at_ms>-<n>.json` | `{at_ms, socket, name, session_id, senders, body, targets}`; replying identity, distinct original sender names in first-route order, body and intended destination ids. Written once, before the first forward |
| `forwards/<at_ms>-<n>.json` | `{forwarded}`; confirmed forward destination ids for the reply of the same name. Written once, after the last forward; it exists only when forwarding is done |
| `errors/<at_ms>-<n>.json` | `{at_ms, error}`; file/start/poll errors, including failed durable route withdrawal |

The client resolves the original sender by its current name: exactly one live session gives `ReplyToSession(session_id)`, otherwise `ReplyUnroutable()`. Only the wire entry uses `reply_to_session_id: ""` for that state. The mod converts it into `ReplyDestination` (`session` or `unroutable`) when registering or restoring a route; it reads no session registry. The recipient gets `Message from <sender> via postmaster: <summary> [<first eight id characters>]`, a blank line and the original text. Distinct tags prevent the recipient's repeat-text filter from dropping equal messages. The mod adds neither entry routing field to the text itself.

**Worker, claim and fallback**

Either `session.start` or `session.receive` starts one shared worker promise, restoring replies before starting the outbox; concurrent hooks await the same start. A failed start writes an error and permits the next hook to retry. The 0.5 s poll never overlaps itself. Each poll writes the heartbeat, retries pending route withdrawals and unwritten results, marks a claim from an earlier worker without a result `abandoned` without sending again, then takes at most 20 new entries oldest-first by mtime, rewriting the heartbeat before each. The poll body, a failed start and each hook's `.catch` write one `errors/` file and never rethrow; a reply handler that throws writes none. Either way the hook hands the event on with `next(e)`.

`$.fs` has no rename or remove, so a claim file decides who delivers and the mod removes nothing. Per entry the mod reads it, writes `claimed/<id>.json`, then checks that `outbox/<id>.json` still exists. Gone → `withdrawn`; malformed → `failed` (`unreadable entry`); `deadline_ms` past → `expired`; else the mod registers the reply route and makes one `session.send({ to: { sessionId }, text })` with the text byte for byte. `isDelivered: true` → `delivered`; `isDelivered: false` → `failed` with the engine's reason in `detail`; a throw before the send → `failed`, a throw from the send → `abandoned`, with the error text in `detail`. `failed` therefore also covers a file step that failed before any send, and the client relays at once on it. An unwritten result stays in memory and is rewritten on later polls without a second send.

The client writes `outbox/.<id>.json.tmp` and publishes it as `outbox/<id>.json` with `os.replace`, `deadline_ms` now + 3 s, then polls `results/<id>.json` every 50 ms. At its deadline it takes the entry out (renamed to `outbox/.<id>.withdrawn`, then removed) before checking the claim; a claim check that fails on a filesystem error counts as a claim. When no claim exists the mod cannot send it; when the mod sees the entry gone it writes `withdrawn`, which sends the client to the relay. These two orders give one delivery through the mod or the relay when a result arrives. A send throw (`abandoned`), a claim with no result after the 2 s grace period, or a withdrawal that fails on a filesystem error becomes `claimed-no-result`: the client relays, accepting a possible second copy to avoid a lost message. After a failed withdrawal the deadline has passed, so the mod answers `expired` instead of sending. An explicit refused send becomes `failed`; its route is removed from memory, its file becomes `withdrawn`, and a failed file write is retried on later polls.

A delivered result's hash sets `verbatim`; a mismatched hash is still reported as sent with `NOT VERBATIM`. Unreadable result files remain pending. An unparsable heartbeat is read up to three times 20 ms apart, since an overwrite through `$.fs.write` can be read torn; one that parses but fails validation, a heartbeat without a non-empty string `run` included, counts as `stale` at once, so a mod older than the `run` field reads as not running. Every return after the heartbeat and `self` checks attempts entry cleanup before temp, withdrawn, claim and result cleanup. If entry removal fails, the client keeps the claim and result to prevent another mod send. Each such attempt also prunes `outbox/`, `claimed/` and `results/` files older than 10 minutes (a claim or result whose entry still exists stays) and `delivered/` files older than 30 minutes, since a new worker restores `open` routes inside `REPLY_WINDOW_MS`. `postmaster.py status` performs that cleanup and removes errors older than seven days; its first line reads `postmaster: running, heartbeat <age> s old, worker <run>; errors: <count>; replies: <count>`, with age to one decimal, or `postmaster: not running; errors: <count>; replies: <count>`. It also prints today's route counts in `user_zone()`, exiting 0 for a fresh live heartbeat and 1 otherwise.

`RELAY_ONLY_SENDERS = frozenset({"shutdown"})` bypasses all postmaster file access. Shutdown instructions depend on the wrapper's sender name, which a mod delivery changes to `postmaster`; these messages keep the relay during the trial. The mod delivery share below excludes them. Removing `shutdown` from the set is the one change once recipients are shown to read the real sender from the first line.

**Replies**

Only a `peer` event with a complete cross-session wrapper and a `from` socket having a route inside the 30-minute window is a reply. The handler reads the first line's `from` and `from-name` and the body between the opening and closing lines. It writes one reply file that names the socket, body and intended destinations, forwards to each distinct session destination registered for that socket, writes the confirmed forwards to a `forwards/` file of the same name, and returns `{consumed: "postmaster reply"}`, starting no turn in the postmaster session. Script senders with no destination still have their replies consumed and logged. Unknown sockets, other origins, ordinary prompts, relay messages and broadcasts pass to `next(e)` unchanged; a failed handler also passes the event on.

A forward begins `Message from <name> (their answer to <sender>, passed on by postmaster) [<at_ms>-<n>]:`, then a blank line and the body. Several sender names produce `their answer to one of <a>, <b>` and each distinct session destination gets it. Each forward has a new tag. The Claude receiving rule answers the real sender named on the first line, never the wrapper's `postmaster` name. Routes are registered before the outbox send returns, so a quick reply is recognized; refusing a later delivery removes only that delivery's route. A throwing send keeps its route because delivery is unknown. A replaced worker restores valid `open` route files inside the window; malformed, invalid and withdrawn files are skipped.

**Start, check and stop procedure**

The unit director runs this on natedev, from main, since the session loads `~/.claude/mods/postmaster`. No worker runs it. All message recipients are throwaway sessions created for the check. The permanent directory is outside the repo and must already be trusted by each account used. Every launch removes all environment variables whose names begin `CLAUDE`, including variables restored by shell startup, and uses a shell whose child is `command claude`. Retain each new session's registry identity and pid; end only those processes. No pane input, settings edit or marketplace installation is part of the procedure.

**Log checks for step 2**

`STATE/log.jsonl` keeps its fields (`time`, `machine`, `from`, `to`, `key`, `summary`, `text`, `outcome`, `detail`); a local Claude branch adds `via` (`postmaster`, `relay` or `queue`), `postmaster` (`delivered`, a miss reason or `unresolved`) and `seconds` (monotonic elapsed branch time rounded to three decimals). A `session:` address with no live registry record is queued without any send and logged as `via: queue`, `postmaster: unresolved`. `via: relay` always means a relay ran. Other transports and skipped messages have none of these fields. The `seconds` value includes any relay fallback. Replies are recorded separately under `replies/`.

From the repo root, define `pm_rows` once; it selects today's local Claude rows using the user's zone, without computing the trial's results here:

```sh
pm_rows() { python3 -c 'import datetime,json,os,sys; from pathlib import Path; sys.path.insert(0,"scripts/lib"); from user_zone import user_zone; z=user_zone(); today=datetime.datetime.now(z).date(); p=Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))/"message/log.jsonl"; rows=(json.loads(s) for s in p.read_text().splitlines() if s.strip()) if p.exists() else []; [print(json.dumps(r)) for r in rows if "via" in r and datetime.datetime.fromisoformat(r["time"]).astimezone(z).date()==today]'; }
```

Each following line reads those rows without changing state. The delivery-share denominator includes `queue` rows as messages not delivered by the mod; the relay count includes only `via: relay`. Compare the daily relay count to the baseline of about 600 and the elapsed-time median to the relay baseline of 4.5 s; use the trial start timestamp to select the two-day interval for the final comparison.

| Number | One-line command |
|---|---|
| Mod delivery share, excluding `shutdown` | `pm_rows \| python3 -c 'import json,sys; r=[json.loads(s) for s in sys.stdin]; r=[x for x in r if x["postmaster"]!="relay-only-sender"]; print(sum(x["via"]=="postmaster" for x in r)/len(r) if r else 0)'` |
| Relays today | `pm_rows \| python3 -c 'import json,sys; print(sum(json.loads(s)["via"]=="relay" for s in sys.stdin))'` |
| Delivery time, median seconds by route | `pm_rows \| python3 -c 'import json,statistics,sys; r=[json.loads(s) for s in sys.stdin]; [print(v,statistics.median(x["seconds"] for x in r if x["via"]==v and x["outcome"]=="sent")) for v in ("postmaster","relay") if any(x["via"]==v and x["outcome"]=="sent" for x in r)]'` |
| Queued and failed counts | `pm_rows \| python3 -c 'import collections,json,sys; c=collections.Counter(json.loads(s)["outcome"] for s in sys.stdin if s.strip()); print("queued",c["queued"],"failed",c["failed"])'` |
| `claimed-no-result` count | `pm_rows \| python3 -c 'import json,sys; print(sum(json.loads(s)["postmaster"]=="claimed-no-result" for s in sys.stdin))'` |
| Replies consumed | `python3 -c 'import os; from pathlib import Path; p=Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))/"message/postmaster/replies"; print(sum(f.is_file() for f in p.glob("*.json")))'` |
| Replies passed on, and confirmed forwards | `python3 -c 'import json,os; from pathlib import Path; p=Path(os.environ.get("XDG_STATE_HOME",str(Path.home()/".local/state")))/"message/postmaster/forwards"; r=[json.loads(f.read_text()) for f in p.glob("*.json")]; print(sum(bool(x["forwarded"]) for x in r),sum(len(x["forwarded"]) for x in r))'` |

Reply counts cover retained files, not just today; use `at_ms` and the trial start to select the same measurement interval as the log. A reply can have several confirmed forwards.

Step 2 results (natedev, 2026-10-13 10:07 to 2026-10-15 10:07 EDT, 701 rows): 533 went through postmaster, 167 through the relay, and 1 was queued with no send because no session was live. Excluding the 125 `relay-only-sender` (`shutdown`) rows, the mod delivered 533 of 576 (92.5%). Relays were 61 in the first 24 h and 106 in the second, against a baseline of about 600 a day. The median `sent` time was 0.67 s through postmaster (p90 1.71 s) and 7.09 s through the relay, which includes the wait before falling back. All 31 `unclaimed` rows came from three broadcasts sent in the same second (4, 8 and 19 recipients), and each was sent by the relay. The rest of the misses were 2 `claimed-no-result`, 6 `self` and 4 `unresolved`; 5 rows ended queued (that 1, plus 4 relays whose recipient had ended) and 0 failed. 10 replies were consumed and none was forwarded. No `errors/` file was written. Adopted 2026-10-15.

**API measurement record**

A probe mod driven by files in `STATE/postmaster/probe/` measured the API; it is no longer in the tree. Commit `cd85b091` holds its code and the earlier version of this document holds the full probe procedure. The table below preserves the measured evidence; it is separate from the start procedure above.

**Mod API on Claude Code 2.1.296, measured 2026-10-12**

Each evidence cell names the request id and the report, receive or transcript line, or the exact command output, and quotes the deciding text. Line numbers refer to the measured copies; the text below preserves the evidence after those copies are removed.

| Point | Answer | Evidence |
|---|---|---|
| Send by session id to another process | Works across processes; B received the message. | `b-id-1`, `report.jsonl` line 2: `"disposition":"returned","answer":{"isDelivered":true}` (128 ms); B's transcript line 8: `Postmaster trial b-id-1. Record receipt; no reply needed.` |
| Send by `uds:` address | Works; B received the message addressed to its socket. | `b-address-1`, `report.jsonl` line 3: `{"isDelivered":true}` (82 ms); B's transcript line 33: `Postmaster trial b-address-1. Record receipt; no reply needed.` |
| Reach of sessions under the other accounts on natedev | Both `~/.claude3` (C) and `~/.claude1` (D) are reachable by id and socket address. | `c-id-1`, `c-address-1`, `d-id-1`, `d-address-1`, `report.jsonl` lines 4–7: each `{"isDelivered":true}` (147, 85, 124, 89 ms). C and D transcript line 8 respectively contains `Postmaster trial c-id-1.` and `Postmaster trial d-id-1.` |
| Sender name the recipient sees and the wrapper | The sender is the mod's session, `pm-probe-a`; the wrapper also names the plugin. | `b-id-1`, B's transcript line 8: `Another Claude session sent a message:` then `<cross-session-message from="uds:/run/user/1000/cc-socks/1788523.sock" from-name="pm-probe-a" from-mode="prompting" from-plugin="postmaster">`, the body, `</cross-session-message>` and the usual peer-message note. |
| Where a reply lands and what `session.receive` carries | B replied to A without a permission prompt. The event has `origin` and `text`; the sender's name and address are inside the wrapper text. | `b-reply-1`, B's transcript line 59: SendMessage `{"to":"uds:/run/user/1000/cc-socks/1788523.sock","message":"consume-1"}`. `received.jsonl` line 1: `"origin":{"kind":"peer"}` and `"text":"<cross-session-message from=\"uds:/run/user/1000/cc-socks/1788501.sock\" from-name=\"pm-probe-b\" from-mode=\"prompting\">\nconsume-1\n</cross-session-message>"`. |
| Whether consuming the event starts no turn | Both matching messages were consumed; A had no transcript before or after either and started no turn. | `consume-1` and `consume-2`, `report.jsonl` lines 10 and 13: `"status":"armed"`. `received.jsonl` lines 1–2: `"consumed":true` with `"consume_request_id":"consume-1"` / `"consume-2"`. The second wrapper has `from-name="shutdown"`. `a_transcript_positions.txt` lines 1–3: `absent` before, after `consume-1`, after `consume-2`. |
| Burst limit at 20 and at 60 | All 20 sends delivered in 2915 ms (84–292 ms each). The following 60-send burst delivered 14 and refused 46 in 6112 ms: its first 12 delivered, reaching 32 consecutive deliveries; two more delivered about 2 s apart. Refusals return an answer. | `burst-20-1` / `burst-60-1`, `report.jsonl` lines 18–19: delivered sends answer `{"isDelivered":true}`; refusal answers `"isDelivered":false` with `Too many messages to this session just now: 32 were sent recently and more would be dropped by its rate limit, so this one was not sent. Batch what remains into one message, or wait a little before sending more.` |
| Send to a dead session | Returns a refusal in 7 ms; does not throw. | `dead-1`, `report.jsonl` line 14: `"disposition":"returned","answer":{"isDelivered":false,"reason":"no live session on this machine has id 00000000-dead-4000-8000-000000000000 (a session that exited, or another machine's; a Remote Control or cloud session is addressed by its session_... id)"}`. |
| Send to self | Sending to A's own id returns a refusal in 2 ms. | `self-1`, `report.jsonl` line 15: `"disposition":"returned","answer":{"isDelivered":false,"reason":"no live session on this machine has id 1168e59f-292b-4a4f-954d-e3b42d44e716 (a session that exited, or another machine's; a Remote Control or cloud session is addressed by its session_... id)"}`. |
| Own session id, pid and environment | `$.session.id()` matches A's registry id; no mod call supplies the session pid. `process.env` is absent. Engine environment calls supply HOME and report XDG_STATE_HOME unset. | `started.json`: `"session_id":"1168e59f-292b-4a4f-954d-e3b42d44e716"`, `"source":"$.session.id()"`; `"pid":null`, `"reason":"globalThis.process.pid is unavailable; no mod call gives the session pid"`, `"process_env_available":false`. `environment_read`: HOME `"answer":"/home/natepiano"`, XDG_STATE_HOME `"answer":{"status":"undefined"}`. |
| File write/list/read/exists, overwrite and visibility in another process; no rename or remove | All six file operations succeed. Write returns undefined; list returns entries with `name`, `kind`, `size`, `mtimeMs`, `isLink`; read returns text and exists returns true. Write creates missing parent directories. Contents are visible by the write's return or the observer's next sample; overwrites can expose a torn value. | `files-1`, `report.jsonl` line 22: `"ok":true`, writes `"answer":{"status":"undefined"}`, exists `"answer":true`, rename/remove `"status":"unavailable"`. `files_observer.txt` lines 2–4: first content at 1791854021802 ms (write ended 1791854021820); final content at 1791854021860 (replacement write ended 1791854021859). |
| Poll mechanism and ten idle minutes: `poll_count`, `max_gap_ms`, `completed_count`, `max_completed_gap_ms` | `globalThis.setInterval` polls every 500 ms; tick interval is 60000 ms. Twelve consecutive idle ticks share one worker; both counts rise by 120 per tick. Worst callback gap is 535 ms and worst completed-check gap is 644 ms, below 2000. Engine calls remain usable after the start hook returns. | `timer-1`, `report.jsonl` line 24: `"mechanism":"globalThis.setInterval","interval_ms":500,"tick_interval_ms":60000`. Tick lines 25–36: first `"poll_count":1079,"completed_count":1061`; last `"poll_count":2399,"completed_count":2381,"max_completed_gap_ms":644`; maximum `"max_gap_ms":535`. |
| Whether the hooks worker is replaced, or starts twice, during a session | Neither occurred during the measured session; this does not establish restart behavior. | `report.jsonl` line 1 is the only `start`: `"already_running":false`. Every line carries `"run":1791853586367`, including the final tick at `"started_ms":1791854789653`. |
| Whether the types file exists after a load | Yes. The engine generates `claude-code/index.d.ts`, its own `types/tsconfig.json`, and `claude-code-tools/` / `claude-code-mcp/`; all are ignored by Git. | At A's load `claude-code/index.d.ts` was written with 621847 bytes and 15990 lines. Generated configuration: target/lib `es2023`, no `DOM`, `moduleResolution: bundler`, `strict`, `noUncheckedIndexedAccess`, `skipLibCheck`, `typeRoots: .`, types `claude-code`, `claude-code-tools`, `claude-code-mcp`; include `../../hooks`, `../../types`, `../../tests`. |
| The `tsc` command | Passes with exit 0 using the generated engine types. The mod configuration uses ES2023 without `DOM` and includes tests; engine declarations already supply the worker globals. Hook and call types come from the engine, including `Register` and `On`. | `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster`: `exit 0`. Initial `step14.txt` result was `exit 2`: `TS2717` / `TS2403` on duplicate DOM globals and `TS2322` on the custom registrar. Final check reports `engine-derived hook and call types, ES2023 without DOM, tests included`. |
| What `command claude plugin validate --strict` prints | Strict validation exits 0. | `step14.txt`, strict validation output: `./register.ts hooks: session.start, session.receive`; `gating hook with .catch: session.receive`; `calls: $.env.get, $.fs.exists, $.fs.list, $.fs.read, $.fs.write, $.session.id, $.session.send`; `env writes: nothing`; `env reads: HOME, XDG_STATE_HOME`; `✔ Validation passed`, `exit 0`. |
| What the test kit exports | Declarations export values `describe`, `expect`, `mock`, `test`, `tier`. `mock.clock`, `store`, `env`, `session` cover engine clock, store, environment and session append; there is no ready send/file fake. Tests answer `session.send` and file calls with their own bottom hooks and raise `session.receive` through the engine. `mods/postmaster/tests/kit.test.ts` holds one case for each: the fake clock, a send answered by a test hook, file calls answered from memory, and a raised receive that is consumed or passed on. The bottom `session.send` hook receives the destination as the bare id string, also when the caller passed `{ sessionId }`; file hooks answer with `{ value }`. | `testing_module.d.ts` lines 567–609 declare the four mocks; lines 1041–1058 give the engine `$` and test `on` hooks beneath plugins. `command claude plugin test mods/postmaster` in `step14.txt`: `claude plugin test: no *.test.ts or *.test.tsx under /home/natepiano/worktrees/claude-build-followups-shutdown/mods/postmaster`, `exit 1` before a test file existed. With `tests/kit.test.ts`: `4 pass`, `0 fail`, `Ran 4 tests across 1 file.`, exit 0. |
| Recipient drops identical text from the same sender | A delivered answer does not ensure a turn: B drops a message equal to the previous text from that sender. Each delivered entry needs distinct text, with the real sender and entry id on its first line. | `burst-20-1`, recorded screen line: `Dropped a peer message from @pm-probe-a (uds:/run/user/1000/cc-socks/1788523.sock): identical to the previous message from this sender.` B's transcript lines 67 and 74 hold one arriving record per burst despite 20 and 14 delivered answers in report lines 18–19. |
| Atomicity of an overwrite through `$.fs.write` | An outside reader can see mixed old/new content. Readers of rewritten JSON must retry an unparsable value. | `files-1`, `files_observer.txt` line 3 at 1791854021860 ms: `'postmaster file trial\n second write\n'`; line 4, the same ms: `'postmaster file trial, second write\n'`. The replacement write ended at 1791854021859. |
| Engine clock calls and fake-clock scope | The engine declares `$.clock.now`, `sleep`, `after`, `every`. The kit's fake clock moves those calls; it cannot advance the probe's `globalThis.setInterval`. | `testing_module.d.ts` lines 569–570: `Answers $.clock from a clock in memory that moves only when the test moves it`; lines 632–633: advance resolves waits from `$.clock.sleep`, `after`, `every`; `timer-1`, report line 24: `"mechanism":"globalThis.setInterval"`. |

### `broadcast.py`: groups on both machines

```
broadcast.py [--here] --from NAME (--all TEXT | --showrunners TEXT | --units TEXT |
             --agents TEXT | [--of UNIT] --workers TEXT | --top-level TEXT)...
```

`BroadcastGroup = AllRoles | Showrunners | UnitDirectors | Agents | Workers | WorkersOfUnit(address, unit, machine) | TopLevel`, closed and separate from the roster's `Role` because flags overlap.

| Flag | Members |
|---|---|
| `--all` | every row |
| `--showrunners` | role `showrunner` |
| `--units` | role `unit director` |
| `--agents` | every worker and freestanding session, Claude and Codex |
| `--workers` | every worker; after `--of <unit, director name, or session: address>`, workers whose `director` is `UnderDirector(<that director's address>)` |
| `--top-level` | `roster.is_top_level` rows |

- Selection is by identity: the sender is left out by `is_sender`, and unit membership by director address, never by name. `RunsProduction`, `DirectsUnit`, `ProductionWithoutRunningShowrunner` and `NoDirector` rows join no unit group.
- A row several flags select gets one delivery, carrying the last selecting flag's text, so a role flag after `--all` keeps its own text.
- Each message is `<text>`, a blank line, and the closing line `Sent directly to <groups> on <machine> and <other machine>; do not pass it on.` (`on this machine` under `--here`).
- Delivery is `send.py --to <address> --from <sender> --here --text ...` per `Messageable` row, 24 at once (`AT_ONCE`), never with action flags. A recipient gone since the roster read is a failed delivery here. A `CannotReceiveMessages` row prints `send.py`'s refusal line and is neither sent nor a failure.
- Without `--here`, `ssh <other machine> env -u CODEX_THREAD_ID -u CLAUDE_CODE_SESSION_ID [BROADCAST_SEND=...] BROADCAST_CLOSING_MACHINES=... broadcast.py --here --from <sender> <same groups>` runs in parallel (120 s timeout); its lines print under `<host>: `. The replay spells an all-workers group `--all-workers` (accepted by `main`, absent from `USAGE`) so it does not inherit an earlier `--of`. ssh exit 255, a timeout, or a nonzero exit with no `NOT sent` line yields `<host>: NOT sent: <why>`.
- `--of` is resolved once across both machines before any delivery: one director gives its machine and address; several give exit 2 with `<target> matches more than one unit director:` and their addresses; none gives `unknown unit <target>; known units:`. If the other machine is not reached, `--of` sends nothing: one `NOT sent` line, exit 1. `--of` without a following `--workers` is a usage error.
- Output: `<shown name> [<address>] - <outcome>[: <detail>]`. The outcomes are `sent`, `no live session` and `NOT sent`. The address appears in brackets only when it differs from the shown name. The shown name includes the job, so there is no role column. A group with no member besides the sender prints `<group> - none on this machine` on each machine. The report ends with `<N> sent in <S> s` and the closing line. Exit 0 when nothing is `NOT sent`, 1 otherwise, 2 for usage. Roster problems print as `broadcast: roster: <problem>`; delivery to the other rows continues.
- Library API: `broadcast(sender, texts: Mapping[BroadcastGroup, str], *, scope=ALL_MACHINES) -> list[Delivery]` runs the same orchestration as the command; `report(deliveries) -> bool`; `send(session, sender, text, *options) -> str` (empty on success). `codex_winddown.announce()` calls `broadcast.broadcast(sender, {Showrunners(): ..., UnitDirectors(): ...})` and `broadcast.report`.

Callers: `/announce` (`--all`), `/unit:announce` (`--units`), `/showrunner:announce` (`--showrunners`), each on both machines, and `/codex_winddown`.

### `top_level.py`

Reads the `ThisMachine` roster and runs no ssh. It prints `<name>\tuds:<socket path>` for each `is_top_level` Claude row other than the sender, from the newest running record per session id, so same-name sessions are two lines and one conversation in two processes is one. A row without a socket prints `not reachable: <name> [<session id>] has no messaging socket` on stderr. Each recipient is recorded with `build_hold.py record-recipient --session-id --name` (a failure exits 1). Roster problems print as `top_level: roster: <problem>`; any argument is a usage error (exit 2). `/notify_top_level` sends to each line with SendMessage, adds ListAgents peers on the other machine unless `--here`, and asks showrunners to pass the message to their unit directors.

### `sessions.py`

- `read_session(path) -> SessionRecord | UnreadableSessionRecord`; `procStart` is `NotRequired[str]`, filled with `""` when absent (callers read `record.get("procStart", "")`).
- `process_start(pid) -> str | None` is the one process-start reader: Linux reads `/proc/<pid>/stat` as bytes, splits after the last `)` and returns field 22 when all digits; macOS runs `ps -o lstart= -p <pid>` under `LC_ALL=C`, `TZ=UTC`, 5 s timeout. `scripts/shutdown/inventory.py` imports it as `_process_start`.
- `running_session(record)`: the pid exists and a nonempty `procStart` equals `process_start(pid)`, socket or not; a record with no `procStart` is running when its messaging socket exists. `messaging_socket_exists(record)`; `live_session(record) = running_session and messaging_socket_exists`.
- `_readable_records()` yields every readable record; `running_sessions()` and `live_sessions()` each filter it through their own predicate per record, newest `updatedAt` first.
- `addressed(to, records)` resolves `uds:`, `session:`, a current name, or a former name only one session had.

### `process_table.py`

`Process(pid, parent, command, arguments)` (`NamedTuple`); `snapshot() -> dict[int, Process]` runs `PS` (from `PROCESS_TABLE_PS`, read at import); `is_codex_agent(process)` is true for `codex exec` or a Python `codex_mesh.py start` / `follow`. Standard library only, importing no other script; `roster.py`, `broadcast.py` and `codex_winddown.py` read it.

### `codex_daemon.py`

- `daemons(environment) -> list[CodexDaemon]`: each `CodexDaemon(home, socket)` comes from `agent_accounts.codex_homes(agent_accounts.observe_workers_home())`, using `<home>/app-server-control/app-server-control.sock`. `ROSTER_CODEX_SOCKET` replaces the whole list with one endpoint whose home is its socket's `parent.parent`. The caller's `CODEX_HOME` does not select a route. Missing sockets are quiet; an existing socket that fails to answer produces a problem naming its home. Each freestanding roster row carries the typed owner and the account from `_codex_home_account(owner.home)`; snapshots preserve that owner.
- `loaded_sessions(socket_path) -> list[DaemonSession] | DaemonUnreadable`: an HTTP upgrade to a WebSocket on the Unix socket, JSON-RPC `initialize`, the `initialized` notification, `thread/loaded/list` (paged by `cursor` / `nextCursor`), then `thread/read {threadId, includeTurns: false}` per id. `DaemonSession(thread_id, name, cwd, status)`; name falls back to the id, status to `unknown`. One 5 s deadline covers the whole exchange (`_OperationDeadline`, `_clock = time.monotonic`). No socket file → `[]`; a repeated `nextCursor` or any failure → `DaemonUnreadable(reason)`, which the roster reports as a problem.
- `read_daemons(environment) -> list[DaemonRead]`: reads all account sockets concurrently under one shared 5 s deadline, within the peer roster's 20 s timeout. Each result carries its owner and either loaded sessions or its own failure; results stay in account-list order, so a stalled account does not hide a healthy account's rows. Standalone `loaded_sessions` calls start their own deadline; this reader passes the shared deadline through the same socket exchange.
- `queue(daemon, thread_id, text, timeout=QUEUE_TIMEOUT_SECONDS) -> Queued | QueueFailed | QueueTimedOut` uses `_queue_socket` for JSON-RPC `thread/queue/add` through the recipient's owning daemon. `send.py` prints `SENT:` only after the daemon answers; an unloaded thread or queue failure prints `FAILED:` and writes no offline queue. One thread loaded in two daemons has two rows and is refused as ambiguous.

### Messages to the user

`scripts/notify/user_action.py` holds the action as a type:

- `ActionRequired(text)`, `NoActionRequired()`, `UserAction = ActionRequired | NoActionRequired`, `ActionUnstated()`, `ActionRefused(reason)`, `ActionAllowed()`.
- `parse_user_action(action, no_action) -> UserAction | ActionUnstated | ActionRefused` refuses both flags together, empty text and placeholder text (`none`, `n/a`, `nothing`, `no action`, `-`, any case).
- `refused_at(action, priority) -> ActionAllowed | ActionRefused` refuses `NoActionRequired` above priority 0.
- `first_line(action, session)`: `Action: <text>` or `No action needed.`, with an optional `Session: <name>` line above it (`SessionLine | NoSessionLine`); `FIX` is the recovery text and `REFUSED_BEFORE_SENDING` the stderr marker.

`scripts/notify/pushover.py [--priority 0|1|2] [--source NAME] (--action TEXT | --no-action) TITLE MESSAGE`, or `--text-id ID --value k=v…` for a catalog text, is the one path to the user's phone, called by `send.py --to user` and directly. Refusals come before the keys file is read, in order: `ActionRefused` from parsing, `ActionUnstated` (`no action stated`), action text over `ACTION_TEXT_MAX` (1015), `refused_at`. A refusal sends nothing, logs `refused before sending: <reason>`, prints `pushover: refused before sending: <reason>. <FIX>` and exits 2. After the refusal checks, `review.review()` checks the push unless the user's `review_off` switch is set. A send opens with `first_line(action, session)`; it ends with a `Sent by: <sender>` line and a last `Template: <text id>` line (`Template: none (free text)` with no `--text-id`). Over `MESSAGE_MAX` (1024) the body is cut, never the action line or those two lines; an empty sender is refused. Priority 2 repeats every 300 s until acknowledged, for at most 3 h. Every `~/.local/state/notify/pushover.jsonl` line names `--source`, else the parent command line (80 chars). Exit 0 sent; 1 Pushover refused or unreachable; 2 refused before sending, usage, or keys missing; 4 another sender's hold claim is live; 5 replaced; 6 not sent (failed review or withheld). The review and these outcomes are in `docs/as-built/message-clarity.md`.

Senders:

- `send.py --to user` requires `--action` or `--no-action` and `--summary`; `--need note|decision|blocked` maps to priority 0, 1, 2, and `decision` / `blocked` require `--action`. The action flags on any other recipient are a usage error (`--action and --no-action go with --to user`). A `pushover.py` exit 2 with the `refused before sending:` marker becomes `REFUSED`; any other failure is `FAILED: the user was not reached: ...`. With `CLAUDE_PROBE_SESSION=1` in its environment (`PROBE_ENV`, `scripts/lib/probe_session.py`; set by `accounts.py exec --probe`), `--to user` prints `not sent: probe sessions never push to the phone` and exits 0 before review, logging or delivery.
- `escalate.py hold(key, summary, text, action: UserAction, minutes=15, need="decision")` raises `ValueError` for `NoActionRequired` with a `need` other than `note`; a stored hold without an action (`LegacyHeld`) is never delivered, lists as `waiting for a hold that states its action`, and the next `hold()` for its key replaces it.
- `restart_support.send_message(recipient, ...)` raises `ValueError` for `user`; `restart_support.alert_user(push: RenderedPush, *, machine="")` is the shutdown path to the user; the catalog render supplies title, body and action; it takes no scope, and callers skip the call when the session record's `alerts` is `"none"`.
- `/alert_user` sends with `--need decision|blocked --action`; on the Mac it adds `--machine natedev`, since the Pushover keys exist only there. On `FAILED` it falls back to PushNotification with the exact `fallback text:` line `send.py` printed; the PreToolUse gate `scripts/hooks/pre-tool-use-push-gate.py` allows nothing else. `REFUSED` and `HELD` never fall back.

### Shutdown test guard (`scripts/shutdown/test_support.py`)

- `ShutdownSendLeaf(StrEnum)` names each real send leaf in `scripts/shutdown/*.py`, the last call before a process that delivers to a session, the other machine or the user: `restart_support._deliver_message`, `restart_support.retire_shutdown_messages`, `restart._retire`, `remote.run_remote`, `remote_control.launch_fixer`, `restart_support.run_notifier`. The fixer launch remains guarded because it starts a detached service; Remote Control recovery has no phone-send path.
- `ShutdownTestCase.setUp` patches every binding in `_REAL_LEAF_BINDINGS` with a stub. A reached stub records the hit and raises `AssertionError("shutdown test reached real send leaf <dotted name> for <recipient>; fake the step above it, or allow_leaf(ShutdownSendLeaf.<member>) and fake the subprocess beneath it")`, so no process starts. A cleanup fails the test for any recorded hit, so a caller that catches the error still fails. `restart._retire(dry_run=True)` passes through.
- `allow_leaf(leaf: ShutdownSendLeaf)` restores the real function for the rest of the test (class-wide when called in `setUp` after `super().setUp()`); anything but a `ShutdownSendLeaf` raises `TypeError`, and `ShutdownSendLeaf("<unknown>")` raises `ValueError`. A test that patches a leaf itself needs no `allow_leaf`.
- `registry_confirmation(ids: frozenset[str]) -> restart.RegistryConfirmation` is a test helper beside the guard: the answer for a stand-in `restart.wait_for_confirmation`, one `SessionRecord` per id in sorted order.

### Instructions: which tool each session uses

- **Claude sessions** (`commands/message.md`, "Finding sessions"): ListAgents and SendMessage for Claude sessions; `roster.py` for Codex workers, unit-director and showrunner relationships, and the other machine; `send.py --to <address>` for anything the roster lists; `broadcast.py` for groups. Scripts always send with `send.py`.
- **Codex sessions** read `~/.codex/AGENTS.md`, workers included. Codex has no scriptable list, so its text points to `roster.py`, `send.py` and `broadcast.py` for every kind. `codex/AGENTS-messaging.md` is the source: listing, sending, groups, messaging the user, "Outcomes and recovery", partly missing results, the message rules, and the sandbox rerun.
- Both texts carry the same outcome and recovery words.
- `scripts/message/install_codex_agents.py [--machine HOST] [--agents-file PATH]` writes that text into `~/.codex/AGENTS.md` between `<!-- claude-messaging: begin -->` and `<!-- claude-messaging: end -->`:
  - No markers: appends the block, creating the file and parent if needed.
  - Exactly one begin and one end, begin first: replaces only the block.
  - Any other marker state: exits 1 with the file unchanged.
  - Every byte outside the block is kept; unchanged content prints `Unchanged messaging block in <path>` and writes nothing.
  - The write is atomic: an exclusively created `.<name>.<hex>.tmp` sibling takes the old file's permission bits and moves in with `os.replace`, removed on any failure.
  - `--machine HOST` runs the installer's own source there over ssh (20 s) with the text on stdin (hidden `--from-stdin`), so the host needs only its `~/.claude/scripts/lib/py` interpreter shim, not its own installer or text; it relays the host's output and exits 0 or 1.

### Key files

- `scripts/message/roster.py` — the roster, both machines, `RosterSnapshot`, refusal text, unit labels
- `scripts/message/sessions.py` — session registry readers, process-start reader
- `scripts/message/process_table.py` — the shared process table
- `scripts/message/codex_daemon.py` — daemon reader and `queue`
- `scripts/message/send.py` — one recipient, any address, either machine, the user; tagged outbox text and route log fields
- `scripts/message/postmaster.py` — outbox client, named attempt/reply/recipient states, cleanup and status
- `mods/postmaster/hooks/{protocol,outbox,replies,register}.ts` — JSON validators, polling, reply routes and shared hook startup; `mods/postmaster/.claude-plugin/plugin.json`, `hooks/hooks.json`, `tsconfig.json` — manifest, module list, strict type check
- `scripts/production/broadcast.py` — groups on both machines
- `scripts/message/top_level.py` — top-level Claude sessions here
- `scripts/notify/user_action.py`, `scripts/notify/pushover.py` — the message-to-user gate; `scripts/notify/review.py` — the clarity review
- `scripts/message/escalate.py`, `scripts/shutdown/restart_support.py`, `scripts/shutdown/restart.py` — held and shutdown messages
- `scripts/shutdown/test_support.py` — shutdown send guard
- `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py`, `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `commands/alert_user.md` — instructions
- Tests: `scripts/message/test_{roster,send,postmaster,codex_daemon,process_table,sessions,top_level,install_codex_agents}.py`, `scripts/notify/test_pushover.py`, `scripts/production/test_{broadcast,codex_winddown}.py`, `scripts/shutdown/test_*.py` (`test_send_guard.py` proves the guard); `mods/postmaster/tests/*.test.ts` exercise fake call sets and kit-loaded hooks

## Invariants

- Roles and links come from records, never names. Nesting, `--kind` context rows, broadcast membership, sender exclusion and `--of` match on `session:<id>` / thread identity, never on names.

- Everything an agent reads in the roster text is plain words: no pids or ports. `--json` is the flat public form with no session field; `--snapshot` is the lossless form between machines only.

- A source that cannot be read becomes one problem line naming it; the rest still prints. Every consumer prints `Roster.problems` and continues.

- Every address is unique on both machines: `session:<id>`, `worker:<run id>/<worker name>`, `codex:<thread id>`. A `CannotReceiveMessages` row has an empty address and takes no messages; broadcast skips it rather than send to `""`.

- A worker is `Messageable` only at mesh status `running` / `waiting_capacity`; a worker between turns is resumed only by its unit director.

- `--action` / `--no-action` go only with `--to user`; every other recipient carries `NoUserRecipient`; `broadcast.py` passes no action flags; `remote()` forwards them only for `user`.

- No message reaches the user without `--action` or `--no-action`. `REFUSED:` (exit 2) means fix the flags and resend, never a PushNotification fallback.

- Under `LocalThenPeer` a local match is sent here and the other machine is not asked. Every ssh send and every per-row broadcast send passes `--here`. `--here` keeps `roster.py`, `send.py` and `broadcast.py` to this machine.

- `--all-workers` appears in no command file or Codex text.

- `process_table` imports none of `roster`, `broadcast` or `codex_winddown`, so `broadcast.py` imports `roster.py` without a cycle.

- `running_sessions()` and `live_sessions()` select per record through their own predicate; a change to `running_session` keeps that. `sessions.process_start` is the one process-start reader. A record with a `procStart` is running only while it matches its process, socket or not, so a phantom is never a roster row, a broadcast line, an unreachable line or a cause of name ambiguity. A headless run (`sessions.headless`) is never running either, so a relay is never a roster row and no name lookup finds it.

- A role-key or signature change in `broadcast.broadcast` carries `codex_winddown.py` and `test_codex_winddown.py`.

- The mod writes its claim before checking the outbox entry; the client takes the entry out before checking its claim. No confirmed delivery means relay or the existing queue, with a possible second copy on `claimed-no-result`.

- Postmaster is folder-loaded, with no marketplace installation or settings change. Its file access stays under `STATE/postmaster/`, and it starts no process and makes no network call.

- Tests never write the real outbox: scratch HOME/state, fake clocks and mod/file/send calls only.

- Every `TestCase` in `scripts/shutdown/` derives from `ShutdownTestCase` and calls `super().setUp()` first. A new send leaf, or a new module that imports `run_remote` by name, gets an entry in `_REAL_LEAF_BINDINGS` and a stub in `setUp`.

- Live checks never message a real session: they use a scratch background Claude session, a scratch mesh thread in a scratch session directory and a scratch daemon thread, all removed afterwards. The postmaster check uses throwaway recipients and leaves only the service session running for the trial. Listing real sessions is allowed.

- `commands/message.md` and `codex/AGENTS-messaging.md` share roster discovery, outcome/recovery rules and `<HeldRoleMessages>` (a held Astra role takes no direct message; its text is queued for the holder's next authorized assignment, `docs/as-built/long-lived-workers.md#lasting-roles`). Only the Claude receiving rules describe postmaster; Codex recipients bypass it.

## Calibration / gotchas

**Roster records**

- A Claude worker ledger id is `agent_bg.sh`'s background id, a prefix of the `sessionId`.

- Session records are one file per pid, indexed by `(sessionId, pid)`; the newest `updatedAt` wins. A pid-to-name map therefore cannot hold a stale name.

- Blind reviewers (`codex exec` via `agent_exec.sh`) carry no `--name` or `--session-dir`; only the review status and pid files find them.

- No launcher writes `lens` into the mesh roster; the lens lives in review artifact names and the progress and findings records.

- No running registered showrunner is a correct, common state.

- A worker under a heartbeat-only run is `NoDirector`, so refusal wording handles a missing unit director.

**Sending**
- Under `--machine HOST`, `remote()` keeps the outcome word from the other machine's last stdout line, so a `REFUSED:` there stays `REFUSED` (exit 2). Only stderr-only exit-2 errors (cannot take messages, ambiguous name, no roster session has address, usage) return as `FAILED: ssh <host>: <reason>` (exit 3).
- `uds:` is always this machine's socket, even with `--machine`.
- A `worker:` address of a worker between turns matches no row on either machine and reads `no roster session has address`, not `cannot take messages`.
- `codex_mesh.py send` accepts only `running` / `waiting_capacity` workers.
- `pushover.py` exit 2 also means usage or missing keys; only the `refused before sending:` stderr marker marks a refusal. A caller with no keys and no action flag gets the action refusal, not `keys missing`.
- Every roster problem on stderr carries `could not read: `, so a machine not reached reads `could not read: <host>: not reached: <why>` (exit 1). In a sandbox this usually means rerun outside it, or use `--here`.
- The ssh commands run `$HOME/.claude/scripts/...` on the other machine, its main checkout: a branch's changes reach it only once merged and pulled there. A `BROADCAST_SEND` set here is set there to the same path, and a path missing there fails the send there.

**Postmaster**
- Strict checks from the repo root: `command claude plugin validate --strict mods/postmaster`, `command claude plugin test mods/postmaster`, and `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster` (natedev has no `tsc`, `node` or `bun` on `PATH`). Repeat validation and the kit after a Claude Code update. The bare `claude` name is a shell alias, so commands and launches spell `command claude`.
- The validator accepts only direct `$.noun.method(...)` call sites and literal `$.env.get` names, and refuses `import()` and a re-exported `register`; the mod imports only its own files with import declarations. A new engine call goes into `callsFor($)` in `register.ts`, never into `outbox.ts` or `replies.ts`.
- Engine-generated `mods/postmaster/.claude-plugin/types/` stays ignored; it supplies `Register`, the hook and call types and the worker globals, so the mod uses ES2023 without `DOM`. The engine writes it only when a live session loads the folder; in a fresh checkout `tsc` reports `TS2307` for `claude-code` until then.
- `--plugin-dir` hot-reloads the folder. A pull on main replaces the running mod's worker 19–23 s after `register.ts` changes; the relay covers a stale-heartbeat pause. A stopped postmaster must be started by hand. Restart's resume command carries no `--plugin-dir`, so whether `/shutdown restart` brings the postmaster back with its mod loaded is unverified.
- After about 32 quick sends to one session the engine answers `isDelivered: false` (then about one per 2 s); the mod does not pace, so these surface as `failed` and the client relays them.
- A reply only counts when the recipient answers the postmaster session with SendMessage. A recipient that answers through `send.py --to postmaster` sends through the relay, whose socket holds no route, so the postmaster takes an ordinary turn.
- `quota_alert.py` gives a session send 45 s in all (`RELAY_TIMEOUT` 40 plus 5); its phone sends wait `review.user_send_seconds()` plus kill grace instead, since a push may be reviewed twice. One budget covers both paths: the relay after a miss gets `--timeout` less the attempt's seconds, at least 1 s, which keeps the 5 s worst-case attempt, the relay and 10 s of kill grace inside it.
- Unmeasured: whether a `$` taken in `session.receive` keeps the outbox poll timer alive after a receive-first start. The live check could not reach receive-first; a hook reload starts the new worker through `session.start`.
- The test kit skips a throwing `fs.list` hook, so a test that makes the listing throw sees the kit's own implementation failure instead.
- `/home/natepiano/postmaster` is outside the repo and trusted per account. A new account or a removed/recreated directory can meet the folder-trust dialog.
- A refused send whose route withdrawal write also fails leaves `open` on disk. If the worker is replaced before a retry succeeds, it restores that route and a message from that recipient within 30 minutes can forward to the sender. This is accepted for the trial: it requires a failed write and replacement before the next 0.5 s poll retry, and `REPLY_WINDOW_MS` bounds it.
- `forwarded` lists only confirmed destination ids. An empty list can mean an unroutable sender, a refused forward or a throwing forward. These forwards are neither retried nor written to `errors/`; read the logged body, identify the intended sender and resend by hand.
- Routes accumulate by recipient socket. A later script message does not remove an earlier sender's route; replies cannot be assigned to a single message while several routes are live.

**Codex daemon**
- An ephemeral daemon thread refuses `thread/queue/add` (-32600) and cannot be archived; live checks start threads with `ephemeral: false`.
- `thread/read` does not show a queued message; `thread/turns/list` does once the turn starts.
- `thread/list` returns every saved thread, finished workers' included, as `notLoaded`; only `thread/loaded/list` shows what is live. `codex agents` is interactive and shows only daemon sessions, not workers.

**Session liveness**
- Read `/proc/<pid>/stat` as bytes: the kernel's 15-byte `comm` limit can cut a process name inside a UTF-8 character.
- When `process_start` returns `None` (malformed stat, `ps` failure), a record with a `procStart` is not running until its start reads again, socket or not.
- The macOS `ps -o lstart=` path is covered in tests only with a faked `ps`.
- `codex_winddown.start()` keeps the first record per name, so it depends on `sessions.live_sessions()` listing the newest first.

**Imports**
- `roster.py`'s module-level `sys.path` edits reach every importer: a change there must also run the herdr and shutdown folder suites.
- Buildlog uses `record.py`; shutdown uses `shutdown_record.py`. Their bare imports have distinct module names even when both folders are on `sys.path`. Shutdown has no old-name alias or shim. `roster.py` imports shutdown's `remote` by package.

**Tests**
- `PROCESS_TABLE_PS` is read at import: a test that sets it reloads `process_table` or patches `process_table.PS`.
- `scripts/shutdown/test_inventory.py` fakes `sessions.live_session`, so `live_sessions()` must call that predicate per record. Suites outside the message package (build_hold, model_study rerun, shutdown) read `live_sessions()`.
- Tests that fake both session readers hide the socket cases; socket fixtures build real session records, and socketless fixtures carry a real start.
- `RUN_REMOTE` stubs and restores four bindings: `remote.run_remote`, `restart_support.run_remote`, `restart.run_remote`, `shutdown.run_remote`.
- `_REAL_LEAF_BINDINGS` holds the real functions captured at import, so `allow_leaf` restores them even inside a case nested in another guarded case.
- An uncaught leaf hit is reported twice (the stub's error, then the cleanup's failure); assert the outcome, not a failure count.
- A guard proof case defines its `ShutdownTestCase` subclass inside the test method and runs it into a `unittest.TestResult()`; a module-level subclass would be discovered and run on its own. `ShutdownTestCase` has no `test_*` methods, so importing `test_support.py` adds no tests.
- `restart_support.alert_user` takes no scope and always reaches `DELIVER_MESSAGE`; a caller skips the call when the session record's `alerts` is `"none"`.
- `test_shutdown_cli.py`'s child processes run outside the guard; neither reaches a leaf.
- `test_restart_support.py`'s fake sender reads stdin only where `send.py` reads it (a message send, not `retire` or `pending`), so a test fails when the real sender would block on an open stdin.

**Instructions**
- `commands/message.md` "Finding sessions" and `codex/AGENTS-messaging.md` carry the same words and nothing checks that they agree: after editing either, edit the other, then run `install_codex_agents.py` here and with `--machine mac`.
- `.gitignore` is `*` plus allow entries, with none for `codex/`, so a new file under `codex/` needs `git add -f`. `!mods/` and `!mods/**` allow the mod folders, and `mods/**/.claude-plugin/types/` keeps the engine-generated types out.

**Ruled out**

- A session's role from its name (a Claude worker's slot role is the one value read from a name); a transport-specific route state in `broadcast.py`; name matching for membership or sender exclusion; a `--json` field for `RosterEntry.session`.

- `live_sessions()` as `running_sessions()` filtered by the socket check; making `procStart` required.

- A module-wide ban on wind-down calling `broadcast`.

- Writing AGENTS.md in place.

- In the postmaster: a rename deciding who delivers (`$.fs` has none; a claim file decides); reaching `node:fs` through a dynamic import (the validator refuses it); `$.clock.every` for the poll (not measured live, `setInterval` was); pacing sends in the mod; a sweep or a session-registry lookup in the mod; a durable withdrawal-recovery step in the mod; any file rewritten whole as it grows.

- In the client: removing `delivered/` files at 10 minutes (drops routes a new worker still restores); `enqueue` instead of a relay on `claimed-no-result` (a lost message is worse than a second copy); raising `quota_alert.py`'s timeout (the budget belongs in `send.py`).

## Why

- **One lister, the existing senders.** `roster.py` lists and never sends; `send.py` reaches one session and `broadcast.py` a group, both from the roster. One way to list, one way to send to one, one way to send to a group, and no second sender to keep in step.

- **Native for your own kind, the roster for the other.** Claude lists Claude with ListAgents and messages it with SendMessage. Codex has no scriptable list (`codex agents` is interactive and does not show workers), so Codex sessions use the roster and `send.py`. The roster lists every kind anyway: a Codex caller needs Claude rows, and a Claude caller needs a worker's unit director and showrunner.

- **The user's Codex windows are sessions of interest.** They list as freestanding Codex sessions from every account daemon. `send.py` reaches a loaded thread through its owning daemon's control socket; an unloaded thread fails, and `--agents` counts the loaded sessions.

- **Both machines by default.** Units run on both machines, so `roster.py` and `broadcast.py` cover both; `--here` limits them. A machine that does not answer is one line, and the rest still prints.

- **Local first, and absence must be proven.** The other machine is asked only after this machine is shown to have no match. A local roster with problems cannot show that for a bare name, so the send stops and asks for an address or `--machine`; an address still goes on to the other machine. For the same reason `--of` is resolved once across both machines and refuses when the other machine is not reached: a partial roster cannot prove a director name unique, and a name or address replayed to the other machine can mean nothing or a different director there.

- **`--snapshot` is separate from `--json`.** The flat `--json` keys stay stable for scripts; the snapshot keeps every variant, including a socketless director's identity, so its workers stay linked across the ssh hop.

- **`CODEX_THREAD_ID` wins in `sender_identity`.** A Codex shell inherits its launching Claude session's `CLAUDE_CODE_SESSION_ID`.

- **The message-to-user gate is a required field, not a model's reading.** An emergency alert once reached the user's phone saying neither what to do nor that nothing was needed. A field cannot be skipped by accident, costs no time, never fails open the way a timed-out model call would, and tests check it without a network. Only the sender knows the action, so the sender writes it. The gate lives in `pushover.py` because every path to the user ends there; emergency is for an action needed now.

- **Shutdown tests can never reach the user's phone.** Two fixture alerts once reached it from a test whose fake had gone stale. The guard fails at the last call before a real process, so a stale fake fails loudly instead of sending.

- **The installer writes atomically.** `Path.write_bytes` truncates first, so a failed in-place write would erase the user's rules outside the block and leave markers every later install refuses.

- **The process table has its own module** so `broadcast.py` can import `roster.py` without a cycle, and roster, broadcast and wind-down share one reader.

Account attribution is read on the owning machine and travels in the snapshot. Claude rows use their process configuration; Codex workers use their serving server's home, including retired servers while a turn finishes. Daemon rows use the socket home; reviewers and herdr panes require a readable process environment. Missing attribution from an older peer is distinct from an unreadable account. Account notes retain that peer's rows until attribution returns.

`peer_snapshot(environment)` returns a `Roster` or `PeerNotReached(host, reason)`; `peer_roster` retains its existing `Roster` interface. Snapshots accept the optional tagged `account` field: a missing field becomes `AccountAttributionUnavailable("peer sent no account")`, while a read failure is `AccountUnread`. Worker attribution reads `mesh_server.json` and `mesh_retired.json` by the worker's port, using the current server only between turns. It never infers a serving home from the workers' configured role.

## Herdr placement

Unit marks come from `unit_lookup.every_marked_unit()`. A marked live Claude
identifies its director; a socketless registry record joins by its pid's herdr
place. Worktree ownership is a fallback when marks cannot be read, never for
a worker ledger entry. Interactive herdr Codex panes are included.
