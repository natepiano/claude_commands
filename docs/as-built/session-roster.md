# Session roster and messaging

> **Status: as-built.**

## What it is

One roster lists every session of interest on natedev and the Mac: Claude showrunners, unit directors, workers and freestanding sessions; Codex workers with their unit director and showrunner; and the user's own Codex windows on the shared Codex daemon. `scripts/message/roster.py` only lists. The existing senders take their recipients from it: `send.py` reaches any one listed session by address or unique name, on either machine, and `broadcast.py` reaches groups (showrunners, unit directors, workers, a unit's workers, agents, top-level sessions, everyone) on both machines. Each kind of session lists its own kind natively where it can and uses the roster for the rest; the instruction texts say which tool to use. Every message to the user passes one gate, `scripts/notify/pushover.py`, which refuses a message that neither states the user's action (`--action`) nor says none is needed (`--no-action`). Shutdown tests run under a guard that fails a test, instead of sending, when it reaches a real send.

## How it works

### Data flow

```
this machine's records ──► roster._local_roster ──┐
other machine: ssh roster.py --here --snapshot ──►├─► Roster(entries, problems)
                (AllMachines only)                │
                                                  ├─► roster.py           text | --json
                                                  ├─► send.py             one recipient ──► relay | codex_mesh send
                                                  │                                         | codex queue | pushover.py
                                                  │                                         | ssh send.py --here
                                                  ├─► broadcast.py        groups ──► send.py --to <address> --here per row
                                                  │                                  + ssh broadcast.py --here
                                                  └─► top_level.py        ThisMachine ──► /notify_top_level SendMessage
```

### Record sources

The roster joins records that each name one part of the picture. A session's role comes from these records, never from its name; the one value read from a name is a Claude worker's slot role, the suffix of its seats-ledger seat name.

| Source | Where | Gives |
|---|---|---|
| Claude session registry | `~/.claude/sessions/<pid>.json` (`NOTIFIER_SESSIONS_DIR`) | `pid`, `sessionId`, `name`, `messagingSocketPath`, `updatedAt`, `procStart`, `tmux` pane, `formerNames`; the roster also reads `cwd` and `status` raw |
| Showrunner registrations | notifier timers under `~/.local/state/notifier/showrunner-*` (`NOTIFIER_STATE_DIR`), via `showrunners.registered_showrunners()` | session name, socket, production slug, production doc |
| Unit marks | tmux sessions marked for a production, via `live_units.production_units(doc)` | each unit's marked pane and the live Claude in it |
| Units table | the production doc, via `add_unit.live_unit_table` | `Unit` and `Worktree` per row (fallback only) |
| Run markers | `/tmp/claude/delegate/active/<director session id>` (`ROSTER_DELEGATE_ROOT`) | first line: the run's session directory |
| Run heartbeats | `<run>/heartbeat.log` | a run is live if written within `remove_seats.LIVE_HEARTBEAT_SECS` (600 s) |
| Seats ledgers | `<run>/seats`, one `<background id>\t<seat name>` per `agent_bg.sh` launch | Claude workers |
| Mesh roster | `<run>/mesh_roster.json`, `<run>/mesh_server.json` | Codex seats: `thread_id`, `status`, `role`, `lens`, `launcher_pid`, `cwd`; server `pid` |
| Review state | `<run>/review_status[_<lens>]`, `<run>/review_pid[_<lens>]` | running blind reviewers |
| Codex daemon | `~/.codex/app-server-control/app-server-control.sock` (`ROSTER_CODEX_SOCKET`) | loaded threads: id, name, cwd, status |
| Process table | `ps -eo pid=,ppid=,comm=,args=` (`PROCESS_TABLE_PS`) | Codex agent processes, reviewer descendants, seat `--cwd` |

The mesh roster format belongs to `scripts/agents/codex_mesh.py` (see `docs/as-built/long-lived-seats.md`); the roster reads it and never writes it.

### How each row's role is derived

A live run is a run directory with a marker whose director session is running, or a heartbeat younger than 600 s (then it has no running director).

Claude rows come from `sessions.running_sessions()`, one per `sessionId` (newest `updatedAt`). First match wins:

1. **showrunner** — the session matches a registration: by messaging socket when the registration has one, else by the registration's session name. Link: `RunsProduction(slug)`.
2. **unit director under a production** — with tmux readable, from tmux only: the marked pane's live Claude, else the running non-worker record whose tmux pane id is the marked pane; a unit with no marked session has no director. Only when tmux is unreadable or absent (the Mac) does a Units row's `Worktree` equal to the session's cwd make it the director (never a seats-ledger worker). Links: `DirectsUnit(unit)` and `UnderShowrunner(slug, name, session:<id>)` when the showrunner runs, else `ProductionWithoutRunningShowrunner(slug)`.
3. **unit director by run marker** — the session id names a live run's marker. Links: `NoShowrunner`, `NoDirector`.
4. **worker** — the session id equals or starts with a seats-ledger id in a live run. `WorkerAssignment(slot_role, run dir)`, the slot role being the seat name's suffix when it is `impl`, `test`, `fix` or `review`, else `role unknown`. When the run's director runs: `UnderDirector(unit, name, session:<id>)` plus the director's showrunner link.
5. **freestanding** — everything else.

Codex rows:

- **worker** — each mesh entry of a live run whose status is not `ended` and whose server pid or `launcher_pid` is alive. Slot role from the record's `role`: `review (<lens>)` for a `review` role with a lens, `no role` when absent, `role unknown` when unrecognized. Status reads `done` → `idle`, `waiting_capacity` → `waiting for capacity`, `capacity_exhausted` → `out of capacity`, others as stored. cwd from the record, else the agent process's `--cwd`, else the director's cwd. Director and showrunner links come from the run; a heartbeat-only run gives `NoDirector` / `NoShowrunner`.
- **blind reviewer** — `review_status[_<lens>]` reads `reviewing` with a live `review_pid[_<lens>]`. Kind is `codex` for a `codex exec` descendant of that pid, else `claude` for a `claude` descendant, else `codex`. Named `<lens> reviewer` or `blind reviewer`, slot role `review (<lens>)` or `review`.
- **freestanding** — each daemon thread that is not a worker's thread (a worker's thread is listed once, as the worker).

### Domain types (`roster.py`)

- `RosterEntry` (frozen): `machine`, `kind` (`claude` | `codex`), `role` (`showrunner` | `unit director` | `worker` | `freestanding`), `name`, `status`, `reach`, `cwd`, `showrunner`, `director`, `assignment`, `thread`, `session`.
- `reach: Messageable(address) | CannotReceiveMessages(reason)`.
  - Claude: `session:<id>` with a messaging socket, else `CannotReceiveMessages("no messaging socket")`.
  - Codex worker: `seat:<run id>/<seat name>` (run id = the run directory's name) at mesh status `running` / `waiting_capacity`; otherwise `between turns; its unit director resumes it` or `between turns; no unit director is running`.
  - Daemon session: `codex:<thread id>`.
  - Blind reviewer: `CannotReceiveMessages("blind reviewer")`.
- `ShowrunnerRelationship = RunsProduction | UnderShowrunner | ProductionWithoutRunningShowrunner | NoShowrunner`; `DirectorRelationship = DirectsUnit | UnderDirector | NoDirector`; `Assignment = WorkerAssignment | NotAWorker`.
- `thread: CodexThread | NoCodexThread` (mesh record or daemon thread); `session: ClaudeSession | NoClaudeSession` (the Claude identity, kept even without a socket).
- Link addresses, text nesting, sort and `--of` use `_session_address(entry)` on `session`, so a socketless unit director still owns its workers.
- `Roster(entries, problems)`; `RosterScope = ThisMachine | AllMachines`; `roster(environment, scope) -> Roster` (scope required).
- `SenderIdentity = CodexThread | ClaudeSession | NoSession` from `sender_identity(environment)`: `CODEX_THREAD_ID` first, then `CLAUDE_CODE_SESSION_ID`. `is_sender(entry, identity)` matches a Codex row's `thread` or any row's `session`; `NoSession` matches nothing.
- `is_top_level(entry)`: a Claude row that is not a worker and not a unit director `UnderShowrunner`.
- `cannot_receive_message_text(entry)`: the one refusal text for `send.py` and `broadcast.py`, `<name> cannot take messages (<reason>)` plus a tail: a worker `UnderDirector` gets `; contact its unit director <name> or wait`, a worker with `NoDirector` none, a socketless Claude session `; wait for it to open its messaging socket, then read the roster again and send again`.

### `roster.py` command

`roster.py [--json] [--kind claude|codex] [--here]`. Without `--here` it reads `AllMachines`: `ssh -o BatchMode=yes -o ConnectTimeout=10 <other machine> '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/roster.py" --here --snapshot'` (20 s timeout, session identity variables removed) runs in parallel with the local read, and entries merge with this machine first.

- **Text** — per machine: a machine line, then showrunner → unit director → worker, unit directors with no running showrunner and their workers, `director not running: <session dir>` groups for workers with no listed director, freestanding Claude, and `your Codex sessions`. Each row: `<role> <name>[ (you)][ (context)] — <kind>, <status>[, <slot role>][, <unit>][, <cwd with ~>]`. `(you)` marks the sender's row on this machine. No pids or ports.
- **`--kind`** keeps rows of that kind and adds each listed worker's unit director and showrunner (same machine) as `(context)` rows; `--json` includes them unmarked.
- **`--json`** prints `{"entries": [...], "problems": [...]}`. Each entry is flat: `machine`, `kind`, `role`, `name`, `status`, `address`, `cwd`, `production`, `showrunner`, `showrunner_address`, `unit`, `director`, `director_address`, `slot_role`, `session_dir`, `thread` (id or `null`). Link fields are empty for no-link states and for a showrunner's or director's own row; `address` is empty for `CannotReceiveMessages`. There is no `session` field.
- **`--snapshot`** (hidden from `--help`) prints `RosterSnapshot`: tagged JSON that decodes back into the same variants, used only between machines.
- **Problems** go to stderr as `could not read: <source>: <detail>`; the other machine's are prefixed with its host; an unreachable machine, timeout or undecodable snapshot is `<host>: not reached: <why>`. A decodable snapshot is used whatever the remote exit status. Exit 0 clean, 1 with any problem.

### `send.py`: one recipient

```
send.py --to NAME|uds:<path>|session:<id>|seat:<run>/<seat>|codex:<thread>
        [--from NAME] [--summary TEXT] [--key KEY [--repeat-minutes N]]
        [--here | --machine HOST] [--codex --session-dir DIR] [--timeout SECONDS]
        (--text TEXT | --file PATH | stdin)
send.py --to user --summary TITLE [--need note|decision|blocked] (--action TEXT | --no-action) ...
send.py ack KEY | reopen KEY | pending [NAME] | retire --to ADDRESS --from SENDER --key KEY
```

The argparse boundary builds `RecipientSelection = RosterLookup(to) | MeshSeat(session_dir, seat)` and `RecipientResolutionScope = LocalOnly | LocalThenPeer | NamedMachine(host)`: `--here` is `LocalOnly`, `--machine HOST` is `NamedMachine`, neither is `LocalThenPeer`; the two together are a usage error. `Options.action: MessageUserAction = UserAction | NoUserRecipient`.

Routes (`resolve_recipient(to, scope) -> DeliveryRoute`):

| `--to` | Route |
|---|---|
| `user` | `pushover.py` here; under `--machine`, `send.py --here --to user ...` there |
| `uds:<path>` | Claude relay on this machine, even with `--machine` |
| any form, `--machine HOST` | `ssh HOST send.py --here ...` with no local roster read |
| `session:<id>` | Claude relay; resolved to the live session's `uds:` socket first; no live session → `QUEUED` |
| `seat:<run>/<seat>` | that row's `codex_mesh.py send --session-dir <run dir> --to <seat>` |
| `codex:<thread>` | `codex_daemon.queue(thread_id, text, timeout)`: `codex queue --thread <id> --message <text>` |
| bare name | roster rows with that `name`; one `Messageable` sends by its address |
| `--codex --session-dir DIR` | `MeshSeat`: `codex_mesh.py send` directly, no roster read |

Resolution for `session:`, `seat:`, `codex:` and bare names reads this machine's roster once (problems print as `send.py: roster: <problem>`). Address forms match `Messageable` addresses; names match any row.

- A match here sends here; the other machine is not asked.
- Several matching rows: exit 2, `<name> matches <N> sessions:` with each address (or its `cannot take messages` reason) and role.
- One `CannotReceiveMessages` row: exit 2 with `cannot_receive_message_text`.
- No match under `LocalThenPeer`: the other machine's roster is read. One `Messageable` row there sends through `ssh` with its address; ambiguity and `cannot take messages` apply to its rows too; a miss while it reports problems is `FAILED` with those problems. A bare name absent from a local roster that had problems asks no one: exit 2, `absence of <name> here is not proven; use an address or --machine`.
- A clean miss: `session:` queues here (exit 1), `codex:` queues to this machine's daemon, `seat:` exits 2 with `no roster session has address <address>`, a bare name goes to the Claude relay by name.

Every ssh send runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/message/send.py" --here ...` with the text on stdin. `remote()` reads the outcome word from the last stdout line there, prefixing the detail `on <host>: `; a run that prints no outcome line (usage errors, `cannot take messages`, ambiguity, `no roster session has address`, which go to stderr) returns `FAILED: ssh <host>: <reason>`. Action flags are forwarded only for `--to user`.

Outcomes print as one line and log with the full text to `$XDG_STATE_HOME/message/log.jsonl` (default `~/.local/state/message`):

| Line | Exit | Meaning |
|---|---|---|
| `SENT: <how>` | 0 | delivered |
| `SKIPPED: <why>` | 0 | key acknowledged, or inside its repeat window |
| `QUEUED: <why>` | 1 | a Claude recipient not reached; kept under its session id |
| `REFUSED: <why>` | 2 | `pushover.py` refused before sending; nothing kept |
| `FAILED: <why>` | 3 | not delivered, not kept: a Codex recipient, the user, or ssh |

Usage errors, the stderr-only refusals above, an unreadable `--file` and an empty message also exit 2.

Delivery mechanics: a Claude recipient is reached through a headless `claude -p` relay (sonnet, auto mode, tools `SendMessage` plus `ListAgents` for a name, hooks, MCP and slash commands off, `CLAUDE_*` variables removed, named `--from`); delivery is read from the `SendMessage` tool result in the stream, which stays in `STATE/relay/<to>.jsonl`. `--timeout` defaults to 40 s for every transport. Where `~/.config/claude/oauth-token.gpg` exists (the Mac, whose keychain ssh cannot open) the relay decrypts it, and queues with `gpg-agent is cold on <machine>: run github-warmup there` when it cannot. Undelivered Claude messages go to `STATE/queue/`, latest per `--key`, under the recipient's session id; `pending` prints and clears them. `--key` with `--repeat-minutes` skips a repeat within the window; `ack` skips every later send with the key until `reopen`.

### Postmaster mod (trial)

Postmaster is a local Claude Code mod in `mods/postmaster/`, loaded directly with `--plugin-dir`; its intended main-checkout path is `~/.claude/mods/postmaster`. This section records how the probe exercises the mod API on Claude Code 2.1.296 and the measured answers. The probe uses `STATE/postmaster/probe/`, where `STATE` is `$XDG_STATE_HOME/message` or, by default, `~/.local/state/message`. It reads and writes only under `STATE/postmaster/`, starts no process and makes no network call. The trial worktree is `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

The probe was removed from the tree after the measurement; commit `cd85b091` holds it, and this procedure stays as the record of how the table was measured.

The unit director runs this procedure in isolated sessions. Every recipient below is a throwaway session created for this check; no probe message goes to an existing session.

1. **Wait for all three folder trusts.** The permanent session directory is `/home/natepiano/postmaster`, decided by the showrunner on 2026-10-12. The showrunner asks the user to create it, start `command claude` there once with the `~/.claude` account, accept the folder-trust dialog and exit, then repeat with `CLAUDE_CONFIG_DIR=/home/natepiano/.claude3` and `CLAUDE_CONFIG_DIR=/home/natepiano/.claude1`. An unused directory presents the dialog with `No, exit` selected, and trust is stored per account. Start this procedure only after the showrunner reports all three trusts done. Keep this directory empty; nobody types into a pane.

2. **Start A, B, C and D.** Use one throwaway tmux session with four windows, each in `/home/natepiano/postmaster`, each with a shell as the pane process and `claude` as its child. Remove every environment variable whose name starts with `CLAUDE` before launching each child, including any restored by shell startup; C and D then receive their account variable shown below. Use `command claude` for every launch and plugin command: the interactive shell's `claude` alias adds `--remote-control` and `--settings`, and plugin testing refuses options before `plugin`. Use these launch commands:

   ```sh
   # A
   command claude -n pm-probe-a --plugin-dir /home/natepiano/worktrees/claude-build-followups-shutdown/mods/postmaster
   # B
   command claude -n pm-probe-b
   # C
   CLAUDE_CONFIG_DIR=/home/natepiano/.claude3 command claude -n pm-probe-c
   # D
   CLAUDE_CONFIG_DIR=/home/natepiano/.claude1 command claude -n pm-probe-d
   ```

   Retain the tmux session identifier and the child process identities for cleanup. Never use `--dangerously-skip-permissions`, edit a `settings.json`, read `.credentials.json` or `.claude.json`, or type into a pane. This is a folder-loaded mod, with no marketplace installation or `enabledPlugins` change.

3. **Confirm identities from records.** Run `sessions.py id pm-probe-a`, `sessions.py id pm-probe-b`, `sessions.py id pm-probe-c` and `sessions.py id pm-probe-d` through the repository's `scripts/lib/py` interpreter. `sessions.py` reads `NOTIFIER_SESSIONS_DIR`, defaulting to `~/.claude/sessions`; it never reads `CLAUDE_CONFIG_DIR`. On natedev, `~/.claude3/sessions` and `~/.claude1/sessions` link to `~/.claude/sessions`, so the same registry lookup finds all four sessions across the three accounts. Read only the matching session registry records for socket paths and process identities; locate each transcript under that session's account project directory; never identify a session from screen text. Read `STATE/postmaster/probe/started.json` for A's session id from `$.session.id` and the recorded absence of a call that gives the session's pid; obtain the pid from A's registry record for cleanup. Its `environment_read` records what `$.env.get` answered for `HOME` and `XDG_STATE_HOME`; `process_env_available` records whether `globalThis.process.env` exists in the worker. Confirm A, B, C and D are the processes just started. In the request objects below, replace `<A_ID>`, `<B_ID>`, `<C_ID>` and `<D_ID>` with these ids, and `<A_SOCKET>`, `<B_SOCKET>`, `<C_SOCKET>` and `<D_SOCKET>` with their socket paths, without a `uds:` prefix. Select `<DEAD_ID>` as a session id that no running session has.

4. **Write one request at a time from a shell.** Replace the placeholders before writing each JSON object below to `STATE/postmaster/probe/request.json`; publish it with one atomic rename of a sibling temporary file so the 0.5 s poll never reads a partial object. Wait for the line with the matching `id` and `step` in `STATE/postmaster/probe/report.jsonl` before writing the next request. Use new ids on a repeat. Each report line carries `run`, the hooks worker's start time in ms, fixed for that worker's life, along with `id`, `step`, `ok`, `detail`, `started_ms` and `ended_ms`; record a missing API as `ok: false` with its reason.

   Each `session.start` appends a line with `id: "start"`, `step: "start"`, `ok: true` and `detail` containing `session_id` (the id or null) and `already_running` (a boolean). Record the initial `run`; a second `start` line with the same `run` means the event fired twice in one worker, and a new `run` means the worker was replaced. Record either observation in the worker-replacement table row and keep evidence from different workers separate.

   An existing request file that does not parse or misses a field gets a `request-error` line with `ok: false`, `detail` giving the reason and `id` equal to the request's string id, or `request` when none exists. This is recorded once per distinct file text; an absent request file is idle and writes nothing. If no line with the request's id appears within 30 s, read the newest `request-error`, `start` and `tick` lines before writing anything else. A `request-error` with that id reports rejection, not completion of the requested step.

   Read each run's traces, captures and logs, copy the answer and evidence line into the table, and delete that run's saved output before another run. Keep saved output below a few GB. Do not remove active probe files between requests in the same run.

5. **Send by id to B, a different process.** Write:

   ```json
   {"id":"b-id-1","step":"send-by-id","to_session_id":"<B_ID>","to_socket":"<B_SOCKET>","text":"Postmaster trial b-id-1. Record receipt; no reply needed.","count":1}
   ```

   Read the `b-id-1` report line and B's transcript file to establish receipt. This tests `$.session.send({ to: { sessionId }, text })` across processes.

6. **Send by socket address to B.** Write:

   ```json
   {"id":"b-address-1","step":"send-by-address","to_session_id":"<B_ID>","to_socket":"<B_SOCKET>","text":"Postmaster trial b-address-1. Record receipt; no reply needed.","count":1}
   ```

   Read the `b-address-1` report line and B's transcript. The probe uses `to: "uds:<B_SOCKET>"`; record whether that address form succeeds.

7. **Repeat both address forms to C and D, the two other accounts on natedev.** Write the first object, read its result and C's transcript, then write the second and read its result and C's transcript:

   ```json
   {"id":"c-id-1","step":"send-by-id","to_session_id":"<C_ID>","to_socket":"<C_SOCKET>","text":"Postmaster trial c-id-1. Record receipt; no reply needed.","count":1}
   ```

   ```json
   {"id":"c-address-1","step":"send-by-address","to_session_id":"<C_ID>","to_socket":"<C_SOCKET>","text":"Postmaster trial c-address-1. Record receipt; no reply needed.","count":1}
   ```

   Read the `c-id-1` and `c-address-1` lines in `report.jsonl`. Repeat for D, reading each result and D's transcript before the next request:

   ```json
   {"id":"d-id-1","step":"send-by-id","to_session_id":"<D_ID>","to_socket":"<D_SOCKET>","text":"Postmaster trial d-id-1. Record receipt; no reply needed.","count":1}
   ```

   ```json
   {"id":"d-address-1","step":"send-by-address","to_session_id":"<D_ID>","to_socket":"<D_SOCKET>","text":"Postmaster trial d-address-1. Record receipt; no reply needed.","count":1}
   ```

   Read the `d-id-1` and `d-address-1` report lines. Record reach separately for `~/.claude3` and `~/.claude1`; both other accounts are within this check.

8. **Record the wrapper, reply route and consumption.** Read B's transcript for the exact wrapper received in steps 5 and 6, including its `from` name. A has no transcript file until it has had a turn. Record the file as absent, or note its current last line if present, then arm consumption before asking B to reply. Write:

   ```json
   {"id":"consume-1","step":"consume","to_session_id":"","to_socket":"","text":"","count":0}
   ```

   Read the `consume-1` line in `report.jsonl` to confirm the probe is armed. Its `session.receive` handler consumes the next arriving message whose text contains `consume-1`, appends all event fields to `received.jsonl` with `consumed: true` and `consume_request_id: "consume-1"`, then returns `{ consumed: <reason string> }` without calling `next(e)`. Other arriving messages are passed on. Once armed, write:

   ```json
   {"id":"b-reply-1","step":"send-by-id","to_session_id":"<B_ID>","to_socket":"<B_SOCKET>","text":"Postmaster trial b-reply-1. Reply to the sender named by this message's wrapper with the text consume-1.","count":1}
   ```

   Read the `b-reply-1` line in `report.jsonl`, then wait at most five minutes for B's reply. Read B's transcript to record whether it replied, stopped at a permission prompt, or did nothing; record that outcome in the reply-route row, together with the destination if it replied. Read A's `received.jsonl` for every field of an arriving `session.receive` event and A's transcript for any new model turn. Record where the reply actually lands, even if it does not reach A. Nobody answers a permission prompt by typing into a pane.

   Whatever B did, arm a second consumption request:

   ```json
   {"id":"consume-2","step":"consume","to_session_id":"","to_socket":"","text":"","count":0}
   ```

   Wait for the `consume-2` report line with `ok: true`, then record whether A's transcript is absent or note its current last line. From its own session, the unit director uses its SendMessage tool to send `Postmaster trial consume-2.` to `uds:<A_SOCKET>`. Read A's `received.jsonl` for the matching event with `consumed: true` and `consume_request_id: "consume-2"`, and confirm A's transcript is still absent, or has no new line. Criterion (d) passes when a message matching an armed id is consumed in A and its transcript is still absent, or has no new line; it does not depend on B completing a reply. A is the throwaway session created for this check, so the director sends nothing to an existing session.

9. **Send to a dead session.** Write:

   ```json
   {"id":"dead-1","step":"send-dead","to_session_id":"<DEAD_ID>","to_socket":"","text":"Postmaster trial dead-1.","count":1}
   ```

   Read the `dead-1` report line for whether the call throws or returns and the complete failure answer.

10. **Send to A itself.** Write:

    ```json
    {"id":"self-1","step":"send-self","to_session_id":"<A_ID>","to_socket":"<A_SOCKET>","text":"Postmaster trial self-1. Record receipt; no reply needed.","count":1}
    ```

    Read the `self-1` report line, A's `received.jsonl` and A's transcript to record the self-send result.

11. **Measure bursts of 20 and 60 sends to B.** Write the first object and read its result before writing the second:

    ```json
    {"id":"burst-20-1","step":"burst","to_session_id":"<B_ID>","to_socket":"<B_SOCKET>","text":"Postmaster trial burst-20-1. Record receipt; no reply needed.","count":20}
    ```

    ```json
    {"id":"burst-60-1","step":"burst","to_session_id":"<B_ID>","to_socket":"<B_SOCKET>","text":"Postmaster trial burst-60-1. Record receipt; no reply needed.","count":60}
    ```

    Read the matching lines in `report.jsonl` for each send's timing, returned `isDelivered` answer and refusal reason. The probe sends the same text `count` times, and B drops a text identical to the previous message from that sender: its transcript shows one arriving record per burst. Record that repeat limit alongside the send-side rate limit; transcript records cannot establish how many sends returned a delivered answer.

12. **Exercise file calls and visibility.** Have another shell process observe `STATE/postmaster/probe/file-source.txt` before writing this request:

    ```json
    {"id":"files-1","step":"files","to_session_id":"","to_socket":"","text":"","count":0}
    ```

    Read the `files-1` report line for writing, listing, reading and testing the existence of a file through `$.fs`, then writing the same path again and reading the replacement value. Its detail includes `path` identifying `file-source.txt`, `final_content` and per-operation timestamps. From the observing process, check that the file and its replacement contents are visible as soon as each write returns; retain the write-return and external-observation evidence for criterion (b). An outside reader may observe a torn value during the replacement write; record the value and timestamp when seen. Treat an unparsable value in a rewritten JSON file as not yet written. Record that `$.fs` has no rename and no remove. The mod starts no process; the unit director runs the observing shell process outside the mod.

13. **Observe ten idle minutes.** Write:

    ```json
    {"id":"timer-1","step":"timer","to_session_id":"","to_socket":"","text":"","count":0}
    ```

    Read the `timer-1` report line for the 0.5 s poll mechanism (`setInterval` in the module or an engine API) and the four baseline values: `poll_count`, `max_gap_ms`, `completed_count` and `max_completed_gap_ms`. Leave A idle for ten minutes, reading the `tick` lines appended every 60 s to `report.jsonl`. `poll_count` counts timer callbacks since the worker started; `completed_count` counts request-file checks that ran to their end since the worker started. `max_gap_ms` is the longest gap between timer callbacks inside the minute the tick closes. `max_completed_gap_ms` is the longest time between finished checks inside that minute, including the time still open when the tick is written. Both gap values start again at each tick.

    Record the elapsed interval and ten consecutive ticks for the same `run`. The gate holds when every tick has `max_gap_ms` and `max_completed_gap_ms` at most 2000, and `poll_count` and `completed_count` both rise from tick to tick. Record a new `run` or a second `start` line if either occurs during the interval.

14. **Record generated types, strict checking and the test kit.** Check whether `mods/postmaster/.claude-plugin/types/claude-code/index.d.ts` exists after A loads the mod. The engine also writes `mods/postmaster/.claude-plugin/types/tsconfig.json`, `claude-code-tools/` and `claude-code-mcp/`. Read the generated declarations and engine configuration to identify the API and compiler libraries actually available; keep generated types uncommitted. The declarations supply their own worker globals, so the mod configuration must omit the `DOM` library. `register` must use the engine's `Register`/`On` types. From the worktree, try this command and record the exact command that passes on natedev, together with its exit status and output:

    ```sh
    nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster
    ```

    Record the exit status and output, including the `hooks:` and `calls:` lines, from:

    ```sh
    command claude plugin validate --strict mods/postmaster
    command claude plugin test mods/postmaster
    ```

    Record what a `tests/*.test.ts` file must import from `claude-code/testing`, including the exports for a fake clock, `$.session.send`, an arriving `session.receive` and file calls. A run with no test file does not establish those exports; keep unsupported points unverified. Hook exceptions or the 10 s hook limit must produce recorded failures; the probe must not throw out of a hook, and each handler must have a `.catch`.

15. **Apply the four pass criteria.** Continue the trial only when all four have evidence: (a) a send by session id or `uds:` reaches B in another process; (b) the mod writes, lists, reads and tests the existence of files under its state directory, and another process sees a file as soon as the write returns; (c) ten consecutive minute ticks for the same `run` each show `max_gap_ms` and `max_completed_gap_ms` at most 2000, with `poll_count` and `completed_count` both rising from tick to tick; (d) an arriving message matching an armed consumption id is consumed in A and starts no turn there, with A's transcript still absent or showing no new line, including the director's `consume-2` check regardless of B's reply outcome. Cross-account observations cover both C (`~/.claude3`) and D (`~/.claude1`); they are recorded separately from the four gates. If any fails or cannot be verified, the unit director stops the run and reports to the showrunner. Record every other result in the table: address form, reach across accounts, sender name and wrapper, reply route and receive fields, worker replacement or repeated starts, burst limit, dead-session failure, self-send, identity reads, generated types, the TypeScript command, strict validation output and test-kit exports. Strict validation must exit 0 and TypeScript checking must pass.

16. **Clean up the live check.** After reading the evidence and filling the table, exit A, B, C and D through the supported process/session controls, end only the throwaway tmux session and remove `STATE/postmaster/probe/`. End only processes this check started, confirmed from the retained identities; never type into a pane. Delete saved traces, captures and logs before another run. Keep `/home/natepiano/postmaster` empty and available as the permanent postmaster session directory.

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
- Output: `<name> - <role> - sent | no live session: <reason> | NOT sent: <why>`, `<group> - none on this machine` per machine for a group with no member besides the sender, then `<N> sent in <S> s` and the closing line. Exit 0 when nothing is `NOT sent`, 1 otherwise, 2 for usage. Roster problems print as `broadcast: roster: <problem>` and the rest sends.
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

- `daemon_socket(environment) -> Path`: `ROSTER_CODEX_SOCKET`, else `~/.codex/app-server-control/app-server-control.sock` (on the Mac a symlink into `/private/tmp/codex-daemon-501/`).
- `loaded_sessions(socket_path) -> list[DaemonSession] | DaemonUnreadable`: an HTTP upgrade to a WebSocket on the Unix socket, JSON-RPC `initialize`, the `initialized` notification, `thread/loaded/list` (paged by `cursor` / `nextCursor`), then `thread/read {threadId, includeTurns: false}` per id. `DaemonSession(thread_id, name, cwd, status)`; name falls back to the id, status to `unknown`. One 5 s deadline covers the whole exchange (`_OperationDeadline`, `_clock = time.monotonic`). No socket file → `[]`; a repeated `nextCursor` or any failure → `DaemonUnreadable(reason)`, which the roster reports as a problem.
- `queue(thread_id, text, timeout=QUEUE_TIMEOUT_SECONDS) -> Queued | QueueFailed | QueueTimedOut` runs `codex queue --thread <id> --message <text>`; `send.py` prints `SENT:` or `FAILED:`, with no fallback.

### Messages to the user

`scripts/notify/user_action.py` holds the action as a type:

- `ActionRequired(text)`, `NoActionRequired()`, `UserAction = ActionRequired | NoActionRequired`, `ActionUnstated()`, `ActionRefused(reason)`, `ActionAllowed()`.
- `parse_user_action(action, no_action) -> UserAction | ActionUnstated | ActionRefused` refuses both flags together, empty text and placeholder text (`none`, `n/a`, `nothing`, `no action`, `-`, any case).
- `refused_at(action, priority) -> ActionAllowed | ActionRefused` refuses `NoActionRequired` above priority 0.
- `first_line(action)`: `Action: <text>` or `No action needed.`; `FIX` is the recovery text and `REFUSED_BEFORE_SENDING` the stderr marker.

`scripts/notify/pushover.py [--priority 0|1|2] [--source NAME] (--action TEXT | --no-action) TITLE MESSAGE` is the one path to the user's phone, called by `send.py --to user` and directly. Refusals come before the keys file is read, in order: `ActionRefused` from parsing, `ActionUnstated` (`no action stated`), action text over `ACTION_TEXT_MAX` (1015), `refused_at`. A refusal sends nothing, logs `refused before sending: <reason>`, prints `pushover: refused before sending: <reason>. <FIX>` and exits 2. A send opens with `first_line(action)`; over `MESSAGE_MAX` (1024) the body is cut, never the action line. Priority 2 repeats every 300 s until acknowledged, for at most 3 h. Every `~/.local/state/notify/pushover.jsonl` line names `--source`, else the parent command line (80 chars). Exit 0 sent; 1 Pushover refused or unreachable; 2 refused before sending, usage, or keys missing.

Senders:

- `send.py --to user` requires `--action` or `--no-action` and `--summary`; `--need note|decision|blocked` maps to priority 0, 1, 2, and `decision` / `blocked` require `--action`. The action flags on any other recipient are a usage error (`--action and --no-action go with --to user`). A `pushover.py` exit 2 with the `refused before sending:` marker becomes `REFUSED`; any other failure is `FAILED: the user was not reached: ...`.
- `escalate.py hold(key, summary, text, action: UserAction, minutes=15, need="decision")` raises `ValueError` for `NoActionRequired` with a `need` other than `note`; a stored hold without an action (`LegacyHeld`) is never delivered, lists as `waiting for a hold that states its action`, and the next `hold()` for its key replaces it.
- `settle.send_message(recipient, ...)` raises `ValueError` for `user`; `settle.alert_user(summary, text, action: ActionRequired, *, machine="")` is the shutdown path to the user; it takes no scope, and callers skip the call when the session record's `alerts` is `"none"`.
- `/alert_user` sends with `--need decision|blocked --action`; on the Mac it adds `--machine natedev`, since the Pushover keys exist only there. On `FAILED` it falls back to PushNotification opening with the same first line; `REFUSED` never falls back.

### Shutdown test guard (`scripts/shutdown/test_support.py`)

- `ShutdownSendLeaf(StrEnum)` names each real send leaf in `scripts/shutdown/*.py`, the last call before a process that delivers to a session, the other machine or the user: `settle._deliver_message`, `settle._retire_shutdown_messages`, `restart._retire`, `remote.run_remote`, `settle._launch_conductor`, `settle.run_notifier`.
- `ShutdownTestCase.setUp` patches every binding in `_REAL_LEAF_BINDINGS` with a stub. A reached stub records the hit and raises `AssertionError("shutdown test reached real send leaf <dotted name> for <recipient>; fake the step above it, or allow_leaf(ShutdownSendLeaf.<member>) and fake the subprocess beneath it")`, so no process starts. A cleanup fails the test for any recorded hit, so a caller that catches the error still fails. `restart._retire(dry_run=True)` passes through.
- `allow_leaf(leaf: ShutdownSendLeaf)` restores the real function for the rest of the test (class-wide when called in `setUp` after `super().setUp()`); anything but a `ShutdownSendLeaf` raises `TypeError`, and `ShutdownSendLeaf("<unknown>")` raises `ValueError`. A test that patches a leaf itself needs no `allow_leaf`.
- `registry_confirmation(ids: frozenset[str]) -> restart.RegistryConfirmation` is a test helper beside the guard: the answer for a stand-in `restart.wait_for_confirmation`, one `SessionRecord` per id in sorted order.

### Instructions: which tool each session uses

- **Claude sessions** (`commands/message.md`, "Finding sessions"): ListAgents and SendMessage for Claude sessions; `roster.py` for Codex workers, unit-director and showrunner relationships, and the other machine; `send.py --to <address>` for anything the roster lists; `broadcast.py` for groups. Scripts always send with `send.py`.
- **Codex sessions** read `~/.codex/AGENTS.md`, seats included. Codex has no scriptable list, so its text points to `roster.py`, `send.py` and `broadcast.py` for every kind. `codex/AGENTS-messaging.md` is the source: listing, sending, groups, messaging the user, "Outcomes and recovery", partly missing results, the message rules, and the sandbox rerun.
- Both texts carry the same outcome and recovery words.
- `scripts/message/install_codex_agents.py [--machine HOST] [--agents-file PATH]` writes that text into `~/.codex/AGENTS.md` between `<!-- claude-messaging: begin -->` and `<!-- claude-messaging: end -->`:
  - No markers: appends the block, creating the file and parent if needed.
  - Exactly one begin and one end, begin first: replaces only the block.
  - Any other marker state: exits 1 with the file unchanged.
  - Every byte outside the block is kept; unchanged content prints `Unchanged messaging block in <path>` and writes nothing.
  - The write is atomic: an exclusively created `.<name>.<hex>.tmp` sibling takes the old file's permission bits and moves in with `os.replace`, removed on any failure.
  - `--machine HOST` runs the installer's own source there over ssh (20 s) with the text on stdin (hidden `--from-stdin`), so the host needs only its `~/.claude/scripts/lib/py` interpreter shim, not its own installer or text; it relays the host's output and exits 0 or 1.

### Key files

- `scripts/message/roster.py` — the roster, both machines, `RosterSnapshot`, refusal text
- `scripts/message/sessions.py` — session registry readers, process-start reader
- `scripts/message/process_table.py` — the shared process table
- `scripts/message/codex_daemon.py` — daemon reader and `queue`
- `scripts/message/send.py` — one recipient, any address, either machine, the user
- `scripts/production/broadcast.py` — groups on both machines
- `scripts/message/top_level.py` — top-level Claude sessions here
- `scripts/notify/user_action.py`, `scripts/notify/pushover.py` — the message-to-user gate
- `scripts/message/escalate.py`, `scripts/shutdown/settle.py`, `scripts/shutdown/restart.py` — held and shutdown messages
- `scripts/shutdown/test_support.py` — shutdown send guard
- `codex/AGENTS-messaging.md`, `scripts/message/install_codex_agents.py`, `commands/message.md`, `commands/announce.md`, `commands/unit/announce.md`, `commands/showrunner/announce.md`, `commands/notify_top_level.md`, `commands/alert_user.md` — instructions
- Tests: `scripts/message/test_{roster,send,codex_daemon,process_table,sessions,top_level,install_codex_agents}.py`, `scripts/notify/test_pushover.py`, `scripts/production/test_{broadcast,codex_winddown}.py`, `scripts/shutdown/test_*.py` (`test_send_guard.py` proves the guard)

## Invariants

- Roles and links come from records, never names. Nesting, `--kind` context rows, broadcast membership, sender exclusion and `--of` match on `session:<id>` / thread identity, never on names.
- Everything an agent reads in the roster text is plain words: no pids or ports. `--json` is the flat public form with no session field; `--snapshot` is the lossless form between machines only.
- A source that cannot be read becomes one problem line naming it; the rest still prints. Every consumer prints `Roster.problems` and continues.
- Every address is unique on both machines: `session:<id>`, `seat:<run id>/<seat name>`, `codex:<thread id>`. A `CannotReceiveMessages` row has an empty address and takes no messages; broadcast skips it rather than send to `""`.
- A seat is `Messageable` only at mesh status `running` / `waiting_capacity`; a seat between turns is resumed only by its unit director.
- `--action` / `--no-action` go only with `--to user`; every other recipient carries `NoUserRecipient`; `broadcast.py` passes no action flags; `remote()` forwards them only for `user`.
- No message reaches the user without `--action` or `--no-action`. `REFUSED:` (exit 2) means fix the flags and resend, never a PushNotification fallback.
- Under `LocalThenPeer` a local match is sent here and the other machine is not asked. Every ssh send and every per-row broadcast send passes `--here`. `--here` keeps `roster.py`, `send.py` and `broadcast.py` to this machine.
- `--all-workers` appears in no command file or Codex text.
- `process_table` imports none of `roster`, `broadcast` or `codex_winddown`, so `broadcast.py` imports `roster.py` without a cycle.
- `running_sessions()` and `live_sessions()` select per record through their own predicate; a change to `running_session` keeps that. `sessions.process_start` is the one process-start reader. A record with a `procStart` is running only while it matches its process, socket or not, so a phantom is never a roster row, a broadcast line, an unreachable line or a cause of name ambiguity.
- A role-key or signature change in `broadcast.broadcast` carries `codex_winddown.py` and `test_codex_winddown.py`.
- Tests are hermetic: temp directories for sessions, the delegate root and the notifier state; fake `tmux`, `ps`, `ssh` and `codex`; a fake daemon socket. They never reach the real daemon, a real session, `send.py`'s relay, `pushover.py` or the other machine.
- Every `TestCase` in `scripts/shutdown/` derives from `ShutdownTestCase` and calls `super().setUp()` first. A new send leaf, or a new module that imports `run_remote` by name, gets an entry in `_REAL_LEAF_BINDINGS` and a stub in `setUp`.
- Live checks never message a real session: they use a scratch background Claude session, a scratch mesh thread in a scratch session directory and a scratch daemon thread, all removed afterwards. Listing real sessions is allowed.
- `commands/message.md` and `codex/AGENTS-messaging.md` say the same thing.

## Calibration / gotchas

**Roster records**
- A Claude seats-ledger id is `agent_bg.sh`'s background id, a prefix of the `sessionId`.
- Session records are one file per pid, indexed by `(sessionId, pid)`; the newest `updatedAt` wins. A pid-to-name map therefore cannot hold a stale name.
- Blind reviewers (`codex exec` via `agent_exec.sh`) carry no `--name` or `--session-dir`; only the review status and pid files find them.
- No launcher writes `lens` into the mesh roster; the lens lives in review artifact names and the progress and findings records.
- No running registered showrunner is a correct, common state.
- The Mac has no tmux; with no `tmux` binary the tmux problem line is left out and the Units-table worktree match decides directors.
- A worker under a heartbeat-only run is `NoDirector`, so refusal wording handles a missing unit director.

**Sending**
- Under `--machine HOST`, `remote()` keeps the outcome word from the other machine's last stdout line, so a `REFUSED:` there stays `REFUSED` (exit 2). Only stderr-only exit-2 errors (cannot take messages, ambiguous name, no roster session has address, usage) return as `FAILED: ssh <host>: <reason>` (exit 3).
- `uds:` is always this machine's socket, even with `--machine`.
- A `seat:` address of a seat between turns matches no row on either machine and reads `no roster session has address`, not `cannot take messages`.
- `codex_mesh.py send` accepts only `running` / `waiting_capacity` seats.
- `pushover.py` exit 2 also means usage or missing keys; only the `refused before sending:` stderr marker marks a refusal. A caller with no keys and no action flag gets the action refusal, not `keys missing`.
- Every roster problem on stderr carries `could not read: `, so a machine not reached reads `could not read: <host>: not reached: <why>` (exit 1). In a sandbox this usually means rerun outside it, or use `--here`.
- The ssh commands run `$HOME/.claude/scripts/...` on the other machine, its main checkout: a branch's changes reach it only once merged and pulled there. A `BROADCAST_SEND` set here is set there to the same path, and a path missing there fails the send there.

**Codex daemon**
- An ephemeral daemon thread refuses `codex queue` (-32600) and cannot be archived; live checks start threads with `ephemeral: false`.
- `thread/read` does not show a queued message; `thread/turns/list` does once the turn starts.
- `thread/list` returns every saved thread, finished workers' included, as `notLoaded`; only `thread/loaded/list` shows what is live. `codex agents` is interactive and shows only daemon sessions, not workers.

**Session liveness**
- Read `/proc/<pid>/stat` as bytes: the kernel's 15-byte `comm` limit can cut a process name inside a UTF-8 character.
- When `process_start` returns `None` (malformed stat, `ps` failure), a record with a `procStart` is not running until its start reads again, socket or not.
- The macOS `ps -o lstart=` path is covered in tests only with a faked `ps`.
- `codex_winddown.start()` keeps the first record per name, so it depends on `sessions.live_sessions()` listing the newest first.

**Tests**
- `PROCESS_TABLE_PS` is read at import: a test that sets it reloads `process_table` or patches `process_table.PS`.
- `scripts/shutdown/test_inventory.py` fakes `sessions.live_session`, so `live_sessions()` must call that predicate per record. Suites outside the message package (build_hold, model_study rerun, shutdown) read `live_sessions()`.
- Tests that fake both session readers hide the socket cases; socket fixtures build real session records, and socketless fixtures carry a real start.
- `RUN_REMOTE` stubs and restores four bindings: `remote.run_remote`, `settle.run_remote`, `restart.run_remote`, `shutdown.run_remote`.
- `_REAL_LEAF_BINDINGS` holds the real functions captured at import, so `allow_leaf` restores them even inside a case nested in another guarded case.
- An uncaught leaf hit is reported twice (the stub's error, then the cleanup's failure); assert the outcome, not a failure count.
- A guard proof case defines its `ShutdownTestCase` subclass inside the test method and runs it into a `unittest.TestResult()`; a module-level subclass would be discovered and run on its own. `ShutdownTestCase` has no `test_*` methods, so importing `test_support.py` adds no tests.
- `settle.alert_user` takes no scope and always reaches `DELIVER_MESSAGE`; a caller skips the call when the session record's `alerts` is `"none"`.
- `test_shutdown_cli.py`'s child processes run outside the guard; neither reaches a leaf.
- `test_settle.py`'s fake sender reads stdin only where `send.py` reads it (a message send, not `retire` or `pending`), so a test fails when the real sender would block on an open stdin.

**Instructions**
- `commands/message.md` "Finding sessions" and `codex/AGENTS-messaging.md` carry the same words and nothing checks that they agree: after editing either, edit the other, then run `install_codex_agents.py` here and with `--machine mac`.
- `.gitignore` is `*` plus allow entries, with none for `codex/`, so a new file under `codex/` needs `git add -f`.

**Ruled out**
- A session's role from its name (a Claude worker's slot role is the one value read from a name); a model reviewing message wording; a transport-specific route state in `broadcast.py`; name matching for membership or sender exclusion; a `--json` field for `RosterEntry.session`.
- Replaying an `--of` address or name unchanged to the other machine; running the worktree match while tmux answers.
- `live_sessions()` as `running_sessions()` filtered by the socket check; making `procStart` required.
- A module-wide ban on wind-down calling `broadcast`.
- Guarding calls that end or start sessions (`systemctl`, `launchctl`, `tmux kill-session`, `codex_mesh.py stop`, signals, `restart._add_unit_command`, the launches in `restart._run`) or the read-only `git`, `ps`, `tmux capture-pane` and `github-warm-status` probes: none delivers a message.
- Writing AGENTS.md in place.

## Why

- **One lister, the existing senders.** `roster.py` lists and never sends; `send.py` reaches one session and `broadcast.py` a group, both from the roster. One way to list, one way to send to one, one way to send to a group, and no second sender to keep in step.
- **Native for your own kind, the roster for the other.** Claude lists Claude with ListAgents and messages it with SendMessage. Codex has no scriptable list (`codex agents` is interactive and does not show workers), so Codex sessions use the roster and `send.py`. The roster lists every kind anyway: a Codex caller needs Claude rows, and a Claude caller needs a worker's unit director and showrunner.
- **The user's Codex windows are sessions of interest.** They list as freestanding Codex sessions, `send.py` reaches them through `codex queue`, and `--agents` counts them.
- **Both machines by default.** Units run on both machines, so `roster.py` and `broadcast.py` cover both; `--here` limits them. A machine that does not answer is one line, and the rest still prints.
- **Local first, and absence must be proven.** The other machine is asked only after this machine is shown to have no match. A local roster with problems cannot show that for a bare name, so the send stops and asks for an address or `--machine`; an address still goes on to the other machine. For the same reason `--of` is resolved once across both machines and refuses when the other machine is not reached: a partial roster cannot prove a director name unique, and a name or address replayed to the other machine can mean nothing or a different director there.
- **tmux decides unit directors when it answers.** The Units-table worktree match can name a director tmux does not show, so it runs only where tmux cannot be read.
- **`--snapshot` is separate from `--json`.** The flat `--json` keys stay stable for scripts; the snapshot keeps every variant, including a socketless director's identity, so its workers stay linked across the ssh hop.
- **`CODEX_THREAD_ID` wins in `sender_identity`.** A Codex shell inherits its launching Claude session's `CLAUDE_CODE_SESSION_ID`.
- **The message-to-user gate is a required field, not a model's reading.** An emergency alert once reached the user's phone saying neither what to do nor that nothing was needed. A field cannot be skipped by accident, costs no time, never fails open the way a timed-out model call would, and tests check it without a network. Only the sender knows the action, so the sender writes it. The gate lives in `pushover.py` because every path to the user ends there; emergency is for an action needed now.
- **Shutdown tests can never reach the user's phone.** Two fixture alerts once reached it from a test whose fake had gone stale. The guard fails at the last call before a real process, so a stale fake fails loudly instead of sending.
- **The installer writes atomically.** `Path.write_bytes` truncates first, so a failed in-place write would erase the user's rules outside the block and leave markers every later install refuses.
- **The process table has its own module** so `broadcast.py` can import `roster.py` without a cycle, and roster, broadcast and wind-down share one reader.
