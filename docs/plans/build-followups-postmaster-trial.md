# Postmaster trial: send.py delivers through a mod, with the relay as fallback

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** On natedev, one idle Claude session loaded with the `postmaster` mod delivers `send.py`'s Claude-bound messages from an outbox directory with no model request; `send.py` waits up to 3 s for the mod's result and otherwise relays as today.

> **As-built disposition: amend** — `docs/as-built/session-roster.md` (it owns `scripts/message/`), plus a new section there, `### Postmaster mod (trial)`

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-12

The user, 2026-10-12: "Let's trial post master to see if it works". Scope is section 8 step 1 of `~/.local/state/plan-backlog/2026-10-11-claude-codex-mods.md` and nothing else. Step 2 (the two-day measurement), the Mac and keep-warm are out of scope (the user's scope); this plan only logs what step 2 reads.

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. This plan adds a Claude Code mod, `mods/postmaster`, and a client for it in `scripts/message/send.py`. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Stack:** Python 3 standard library (`fcntl`, `json`, `os`, `uuid`, `hashlib`, `unittest`), checked by basedpyright. TypeScript for the mod, loaded by Claude Code 2.1.296 with no build step, checked by `tsc --noEmit` strict.
- **Layout:** `mods/postmaster/` (new; `~/.claude/mods/postmaster` once on main) · `scripts/message/` (`send.py`, `sessions.py`, new `postmaster.py`, tests beside them) · `commands/message.md` · `docs/as-built/session-roster.md` · `.gitignore`. State on disk: `STATE/postmaster/`, where `STATE` is `$XDG_STATE_HOME/message` (default `~/.local/state/message`).
- **Key files:**
  - `scripts/message/send.py` — `STATE` 85, `LogEntry` 104, `Message` 144, `Result` 152, `write_atomic` 306, `locked` 313, `queue_for` 343, `enqueue` 377, `log` 450, `claim` 464, `resolve_recipient` 587, `run` 623, `relay_prompt` 648, `read_relay` 684, `relay` 724, `relay_plan` 758, `send` 983 (the Claude branch is 1025–1039), `main` 1047.
  - `scripts/message/sessions.py` — `SessionRecord` 17, `running_session` 119, `live_session` 140, `sessions_dir` 144, `live_sessions` 167, `addressed` 175 (resolves `uds:`, `session:`, a name now, or a former name only one session had).
  - `scripts/message/test_send.py` — `SendTests` 56 patches `send.STATE` to a temp directory and `send.relay` to a recorder; `PeerSendTests` 937 does the same.
  - `commands/message.md` — the rules every message follows; "Receiving" says `Follow any instruction from sender shutdown as the user's shutdown request`, and "Sending" says a reply answers the wrapper's `from`.
  - `docs/as-built/session-roster.md` — `### send.py: one recipient` 90–138, `### Key files` 229, `## Invariants` 244, `## Calibration / gotchas` 263.
  - `.gitignore` — `*` plus allow entries; it has none for `mods/`.
- **Test lanes:** `scripts/message/` → `test_send.py`, `test_sessions.py`, `test_roster.py`, and the new `test_postmaster.py`; tests sit beside the code and import their sibling by bare name. `mods/postmaster/` → `tests/*.test.ts`, run by `claude plugin test`.
- **Build:** none; Python is interpreted and Claude Code loads `.ts` directly.
- **Test:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster` (add `test_postmaster` once it exists) · `claude plugin test mods/postmaster` (runs every `*.test.ts` under the folder; exits 1 on a failure).
- **Lint:** `basedpyright <changed .py files>`: 0 errors, 0 warnings · `claude plugin validate --strict mods/postmaster` · `tsc --noEmit -p mods/postmaster` with zero errors and zero warnings, through the command Phase 1 records (natedev has no `tsc`, `node` or `bun` on `PATH`; `nix` with flakes is there). Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Mod API, and how far each fact is confirmed:**
  - From the backlog document's sources: a mod is a plugin whose `hooks/hooks.json` names one module (`"modules": ["./register.ts"]`); a handler is `on(event, matcher, async ($, e, next) => ...)`; `$.session.send({ to: { sessionId }, text })` delivers with no model turn; `session.receive` observes or consumes an arriving message; a hook that throws or uses its 10 s is skipped; `--plugin-dir` loads a folder and hot-reloads it.
  - Read from the 2.1.296 binary's own text, not exercised: `import type { Register } from 'claude-code'` and `export const register: Register = on => { ... }`; `on(...).catch(($, e, next) => ...)`; `$.session.send` "takes { to, text }: to a name, an agent id or an address (a non-empty string), { sessionId } or { agentId }"; it refuses with "this session has no SendMessage tool" where messaging is off, and reports "not sent" or "not delivered"; a `session.receive` hook can consume a delivery ("consumed by a hook; not queued"); the names `$.clock.now`, `$.fs.write`, `$.process.run`, `$.store.set`, `$.state.get`; the engine writes the type declarations to `<mod>/.claude-plugin/types/claude-code/index.d.ts` once it has loaded the mod from its folder; one hooks worker serves every plugin and is replaced when it dies.
  - Confirmed from the CLI's help: `claude plugin validate [--strict] [--json] <path>`; `claude plugin test [dir]`, whose test files import their kit from `claude-code/testing`; `claude --plugin-dir <path>`; `claude -n <name>`.
  - Everything else about the mod API is a point Phase 1 verifies. No Work Order may treat an unverified point as a fact; each later Work Order states what it assumes.
- **Invariants:**
  - With no fresh heartbeat, `send.py` behaves exactly as today: same outcome line, exit code, queue files and relay. This holds at the end of every phase.
  - No message is lost: every path without a confirmed `delivered` result from the mod ends in the relay or in today's queue.
  - One rename decides who delivers. The mod sends an entry only after it renamed it out of `outbox/`; `send.py` relays only after it renamed the same entry out of `outbox/`, or after the mod reported that it did not deliver.
  - Never a marketplace mod. No `enabledPlugins`. No edit to any `settings.json` (the user's rule for this trial).
  - The mod reads and writes only under `STATE/postmaster/`. It starts no process and makes no network call.
  - Never use `--dangerously-skip-permissions`. Never read `.credentials.json` or `.claude.json`. Never type into a pane.
  - Tests are hermetic (`docs/as-built/session-roster.md`, Invariants): temp state, no real session, no real relay, no real outbox.
  - Python: basedpyright 0 errors and 0 warnings on changed files; no file-level ignores; no `Any` (a `TypedDict` per JSON format).
  - TypeScript: `strict`, no `any`, no `@ts-ignore`, no `@ts-expect-error`.
  - Live steps belong to the unit director, never to a seat, and never message a real session.

## Phases

### Phase 1 — The probe mod and the live check of the mod API  · status: todo

#### Work Order

**Goal:** a probe mod loads in one test session, and a written record says, for Claude Code 2.1.296, what each unverified mod API point does.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- The mod folder. `mods/postmaster/.claude-plugin/plugin.json` (name `postmaster`, a version, a description; no marketplace field), `mods/postmaster/hooks/hooks.json` (`{"modules": ["./register.ts"]}`), `mods/postmaster/tsconfig.json` (`strict`, `noEmit`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`; the path mapping for `claude-code` points at `.claude-plugin/types/claude-code/index.d.ts`), `mods/postmaster/hooks/register.ts` (exports `register` and wires the probe). `.gitignore` gains `!mods/`, `!mods/**` and an ignore for `mods/**/.claude-plugin/types/` (plan author's call: the engine rewrites the types at each load, so a committed copy goes stale at each Claude Code update; one-line revert).
- The probe, `mods/postmaster/hooks/probe.ts`. It is driven by files, since nobody types into a pane. Its directory is `STATE/postmaster/probe/`. On `session.start` it writes `started.json` (its own session id and pid, and how it read each), then begins polling `request.json` every 0.5 s. A request is `{"id": string, "step": string, "to_session_id": string, "to_socket": string, "text": string, "count": number}`. For each new `id` it runs the step and appends one JSON line to `report.jsonl` with `id`, `step`, `ok`, `detail`, `started_ms` and `ended_ms`. It also appends one line per arriving `session.receive` event to `received.jsonl`, with every field the event carries, and passes the event on with `next(e)`.
- Steps the probe implements, one per point:
  1. `send-by-id`: `$.session.send({ to: { sessionId }, text })` to a session in another process. Section 7 point 1.
  2. `send-by-address`: the same text with `to` as the string `uds:<socket>`.
  3. `send-dead`: a send to a session id no session has. Records whether the call throws or returns, and what the answer says.
  4. `send-self`: a send to the probe's own session id.
  5. `burst`: `count` sends in a row to one session, each timed. Section 7 point 4.
  6. `files`: create, list, read, rename and remove a file under the probe directory, and say which API did it (`node:fs`, `$.fs`, or `$.process.run`). The rename must be one atomic call.
  7. `timer`: say which mechanism fires the 0.5 s poll (`setInterval` in the module, or an engine API) and append a `tick` line every 60 s so a ten-minute idle run shows whether it keeps firing.
- A step whose API does not exist is recorded as `ok: false` with the reason; the probe never throws out of a hook, and every handler has a `.catch`.
- The live procedure, run by the unit director after both seats are idle. It is written into the as-built section before it is run.
  - The directory is `/home/natepiano/postmaster` (decided 2026-10-12 by the showrunner, who asks the user to do it): the user creates it and starts `claude` there once under each natedev account (`~/.claude`, then `CLAUDE_CONFIG_DIR=~/.claude3`), accepts the folder-trust dialog each time and exits. A directory no session has run in shows that dialog with the default answer `No, exit`, trust is stored per account, and nobody types into a pane. The directory stays empty and is the postmaster session's permanent directory. The live procedure starts only after the showrunner says both trusts are done.
  - Start three throwaway sessions, each in its own window of one throwaway tmux session, each in `/home/natepiano/postmaster`, each with every `CLAUDE*` variable removed from its environment, each pane a shell with `claude` as its child. A: `claude -n pm-probe-a --plugin-dir <worktree>/mods/postmaster`. B: `claude -n pm-probe-b`. C: `CLAUDE_CONFIG_DIR=/home/natepiano/.claude3 claude -n pm-probe-c` (the other account on natedev). Confirm each with `sessions.py id <name>`; never by screen text.
  - Write requests into `request.json` from a shell, one at a time, and read `report.jsonl`. Run `send-by-id` and `send-by-address` to B, then to C; `send-dead`; `send-self`; `burst` with `count` 20 and then 60 to B; `files`; `timer` for ten idle minutes.
  - For points 2 and 3, read B's transcript file (never the pane): record the wrapper B received and the `from` it names (section 7 point 2). Ask B, in the text of one probe message, to answer the sender; record where the answer arrives, what A's `received.jsonl` holds for it, and whether a turn started in A (section 7 point 3).
  - Record whether A's `.claude-plugin/types/claude-code/index.d.ts` now exists, and the exact command under which `tsc --noEmit -p mods/postmaster` passes on natedev (first candidate: `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster`).
  - Record what `claude plugin validate --strict mods/postmaster` prints, with its `hooks:` and `calls:` lines, and what `claude plugin test mods/postmaster` needs from a test file (the kit's exports that fake the clock, `$.session.send`, an arriving `session.receive` and the file calls).
  - Clean up: exit A, B and C, end the throwaway tmux session, remove `STATE/postmaster/probe/`. End only processes this check started.
- Pass criteria. The trial goes on only if all four hold; otherwise the unit director stops the run and reports to the showrunner: (a) `send-by-id` or `send-by-address` reaches B, a session in another process; (b) a file rename is one atomic call from inside the mod; (c) the poll fires for ten idle minutes with no gap over 2 s; (d) an arriving message can be consumed in A with no turn started there. Every other point is recorded and changes later phases through `/plan:phase_review`: the address form, whether C is reached (if not, `send.py` uses the mod only for recipients of the postmaster's own account), the sender name B sees, the burst limit, the failure answer, the `tsc` command, the test kit.
- The record. `docs/as-built/session-roster.md` gains `### Postmaster mod (trial)` with the procedure above and a table `Mod API on Claude Code 2.1.296, measured <date>`: one row per point, with the answer and the evidence line.

**Files:**
- `.gitignore` — allow `mods/`, ignore the generated types folder.
- `mods/postmaster/.claude-plugin/plugin.json` — the manifest.
- `mods/postmaster/hooks/hooks.json` — names `./register.ts`.
- `mods/postmaster/hooks/register.ts` — `register`, wiring the probe.
- `mods/postmaster/hooks/probe.ts` — the probe steps and its files.
- `mods/postmaster/tsconfig.json` — strict settings.
- `docs/as-built/session-roster.md` — the new section: procedure, then the measured table.

**Seats:** 2 writers — the mod folder, and the document.
- `impl` — `.gitignore`, `mods/postmaster/.claude-plugin/plugin.json`, `mods/postmaster/hooks/hooks.json`, `mods/postmaster/hooks/register.ts`, `mods/postmaster/hooks/probe.ts`, `mods/postmaster/tsconfig.json`; hub: `.gitignore` (the mod's files are untracked without it).
- `test` — opens as impl: `docs/as-built/session-roster.md`. No test lane yet: the kit's API is one of the points this phase measures.

**Constraints from prior phases:** none.

**Acceptance gate:** `claude plugin validate --strict mods/postmaster` exits 0; the live procedure ran and its four pass criteria hold; the as-built table has an answer and an evidence line for every point; `tsc --noEmit -p mods/postmaster` passes under the recorded command; the message suite is unchanged and green.

**Verification:** `claude plugin validate --strict mods/postmaster` · the recorded `tsc` command · `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster` · `git status --short` shows no file outside this phase's Files.

### Phase 2 — The mod delivers outbox entries and writes a result for each  · status: todo

#### Work Order

**Goal:** a session loaded with the mod delivers each entry in `STATE/postmaster/outbox/` once, writes its result, and keeps a heartbeat.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. This phase changes no Python; `send.py` writes no entry yet.

- Directories under `STATE/postmaster/`: `outbox/`, `claimed/`, `results/`. The mod creates them at start. `STATE` is `$XDG_STATE_HOME/message`, else `~/.local/state/message`.
- An entry is `outbox/<id>.json`: `{"id": string, "session_id": string, "socket": string, "text": string, "sha256": string, "deadline_ms": number}`. `id` is 32 hex characters. `text` is delivered byte for byte; the mod adds nothing. `deadline_ms` is the Unix time after which the sender has stopped waiting. Files whose name starts with `.` are writers' temp files and are never read.
- The poll, every 500 ms (`POLL_MS`), through the mechanism Phase 1 recorded:
  1. Write `heartbeat.json` through a temp file and one rename: `{"session_id": string, "pid": number, "at_ms": number, "version": string}`.
  2. List `outbox/`. For each `<id>.json`, oldest first: rename it to `claimed/<id>.json`. A failed rename means the sender withdrew it; skip it with no result.
  3. Read the claimed entry. Unreadable or missing a field: result `failed`, detail `unreadable entry`. `deadline_ms` already past: result `expired`, no send.
  4. Otherwise call `$.session.send` once with the address form Phase 1 recorded. A returned success is `delivered`; a thrown or refused send is `failed` with the engine's text in `detail`.
  5. Write `results/<id>.json` through a temp file and one rename: `{"id": string, "outcome": "delivered" | "failed" | "expired", "detail": string, "sha256": string, "claimed_ms": number, "finished_ms": number}`, where `sha256` is the hash of the text the mod passed to `$.session.send`. Then remove `claimed/<id>.json`.
- At start, and at each poll, an entry found in `claimed/` that this run did not claim gets result `failed`, detail `abandoned by an earlier run`, and is never sent. This is what keeps a replaced hooks worker from sending a stale entry.
- Entries are handled one at a time in one poll, at most `BATCH` (20) per poll, and the pace respects the burst limit Phase 1 measured. A second poll never starts while one runs.
- The mod sweeps `results/` files older than 10 minutes at each poll; a sender removes its own result when it reads it.
- Every handler and the poll body have a `.catch` that writes the error to `STATE/postmaster/errors.jsonl` and never rethrows; one bad entry never stops the poll.
- `mods/postmaster/hooks/probe.ts` is deleted in this phase; `register.ts` wires the poll instead.
- Source layout: `hooks/protocol.ts` (the three JSON types, their validators, the path names), `hooks/outbox.ts` (poll, claim, deliver, result, heartbeat, sweep), `hooks/register.ts` (wiring only).
- Tests under `mods/postmaster/tests/`, with the kit's fake clock and fake send: an entry is delivered once and its result says `delivered` with the right hash; an entry withdrawn before the claim gets no send and no result; an expired entry is not sent; a refused send gives `failed` with the engine's text; a malformed entry gives `failed` and the next entry still goes; an entry left in `claimed/` by an earlier run is not sent; the heartbeat is rewritten each poll; two entries for one session arrive in file order.

**Files:**
- `mods/postmaster/hooks/protocol.ts` — entry, result and heartbeat types with validators.
- `mods/postmaster/hooks/outbox.ts` — the poll and its steps.
- `mods/postmaster/hooks/register.ts` — wires the poll at `session.start`.
- `mods/postmaster/hooks/probe.ts` — deleted.
- `mods/postmaster/tests/outbox.test.ts` — the cases above.
- `mods/postmaster/tests/protocol.test.ts` — validator cases.

**Seats:** 1 writer + 1 tester — the Spec fixes every file format and outcome, so the tests are written from it.
- `impl` — `mods/postmaster/hooks/protocol.ts`, `mods/postmaster/hooks/outbox.ts`, `mods/postmaster/hooks/register.ts`, `mods/postmaster/hooks/probe.ts`; hub: `mods/postmaster/hooks/protocol.ts` (the tester imports its types).
- `test` — `mods/postmaster/tests/outbox.test.ts`, `mods/postmaster/tests/protocol.test.ts`.

**Constraints from prior phases:** Phase 1 built the mod folder, its manifest and `tsconfig.json`, and recorded in `docs/as-built/session-roster.md` → `### Postmaster mod (trial)` the facts this phase uses. Assumed here, and replaced by Phase 1's measured answer where it differs: (1) a mod can poll every 500 ms while its session is idle; (2) a mod can list, read, rename and remove files, and the rename is atomic; (3) `$.session.send` reaches a session in another process and answers a failed send with a throw or a refusal the mod can read; (4) the mod can read its own session id and pid; (5) the test kit can fake the clock, the send and the files. If Phase 1 found that only recipients of the postmaster's own account are reached, nothing changes here: `send.py` decides which entries to write.

**Acceptance gate:** `claude plugin test mods/postmaster` green with the named cases; `claude plugin validate --strict mods/postmaster` exits 0 and its `calls:` lines name only the send, the clock and the file calls; `tsc --noEmit -p mods/postmaster` clean; the message suite green and unchanged.

**Verification:** `claude plugin test mods/postmaster` · `claude plugin validate --strict mods/postmaster` · the recorded `tsc` command · `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster`.

### Phase 3 — The mod consumes replies so no turn starts in its session  · status: todo

#### Work Order

**Goal:** a reply that lands at the postmaster session is consumed and logged, starts no turn there, and is forwarded when its target is known and live; any other arriving message passes on unchanged.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- The mod keeps, per recipient session id, the last delivery it made there: `{"sender": string, "at_ms": number}`. The entry gains one field for this, `"sender": string` (the `--from` name), added to `hooks/protocol.ts`; the mod never puts it into the delivered text. The map is kept in memory and saved to `STATE/postmaster/delivered.json` after each delivery, so a replaced hooks worker reads it back.
- `session.receive` handler:
  - The sender of the arriving message is a session the mod delivered to within `REPLY_WINDOW_MS` (30 minutes): this is a reply. Append one line to `STATE/postmaster/replies.jsonl` (`at_ms`, the replying session id and name, the original sender, the text, `forwarded`), and consume the event so nothing is queued and no turn starts.
  - Forwarding: when the original sender is the name of exactly one live session other than the postmaster, send that session `Message from <replying session name> (their answer to <original sender>, passed on by postmaster):`, a blank line, then the reply text, and log `forwarded: true`. A script sender such as `quota_alert` has no session; log `forwarded: false` and drop it, as its answer has nowhere to go today.
  - Anything else passes on with `next(e)` unchanged: a message from a session the mod never delivered to, a relay's message, the user's own prompt. An account-wide `/shutdown` and a broadcast therefore reach the postmaster session as they reach any idle session.
- Live sessions are read from the registry files in `~/.claude/sessions/` (`pid`, `sessionId`, `name`) through the file calls Phase 1 recorded; a name that matches no file, or more than one, is not forwarded.
- The handler has a `.catch` that passes the event on with `next(e)`: a failing handler must never swallow a message.
- Tests: a reply from a delivered-to session is consumed, logged and forwarded to the one live session with the sender's name; a reply whose sender is a script name is consumed and logged with `forwarded: false`; a message from a session never delivered to is passed on untouched; a reply after the window is passed on; a throwing forward still consumes and logs; the saved map is read back at start.

**Files:**
- `mods/postmaster/hooks/protocol.ts` — the `sender` field; the delivered-map and reply-log types.
- `mods/postmaster/hooks/replies.ts` — the `session.receive` handler, the map, the forward.
- `mods/postmaster/hooks/outbox.ts` — records each delivery in the map.
- `mods/postmaster/hooks/register.ts` — wires the handler.
- `mods/postmaster/tests/replies.test.ts` — the cases above.

**Seats:** 1 writer + 1 tester — one handler and its map; the Spec fixes every outcome.
- `impl` — `mods/postmaster/hooks/protocol.ts`, `mods/postmaster/hooks/replies.ts`, `mods/postmaster/hooks/outbox.ts`, `mods/postmaster/hooks/register.ts`; hub: `mods/postmaster/hooks/protocol.ts`.
- `test` — `mods/postmaster/tests/replies.test.ts`.

**Constraints from prior phases:** Phase 2 built `hooks/protocol.ts` (entry, result, heartbeat), `hooks/outbox.ts` (poll, claim, deliver, result) and the `.catch` rule. Assumed here, and replaced by Phase 1's measured answer where it differs: (1) a reply to a mod-sent message arrives at the postmaster session as a `session.receive` event; (2) that event names the replying session by id; (3) returning without `next(e)` consumes it with no turn; (4) the recipient sees the postmaster's name as the sender. If Phase 1 found that the recipient sees the real sender's name, replies never land here: this phase then ships only the pass-through handler, its log line and its tests.

**Acceptance gate:** `claude plugin test mods/postmaster` green with the named cases; `claude plugin validate --strict mods/postmaster` exits 0; `tsc --noEmit -p mods/postmaster` clean; the message suite green and unchanged.

**Verification:** `claude plugin test mods/postmaster` · `claude plugin validate --strict mods/postmaster` · the recorded `tsc` command · `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster`.

### Phase 4 — send.py tries the postmaster first and relays on every other path  · status: todo

#### Work Order

**Goal:** `send.py` delivers a Claude-bound message through the outbox when the heartbeat is fresh, relays on every path without a `delivered` result, never causes two deliveries, and logs which path each message took.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- New module `scripts/message/postmaster.py`; `send.py` imports it. Constants: `WAIT_SECONDS = 3.0`, `CLAIM_GRACE_SECONDS = 2.0`, `HEARTBEAT_MAX_AGE_SECONDS = 2.0`, `POLL_SECONDS = 0.05`. `TypedDict`s `Entry`, `ModResult` and `Heartbeat` match Phase 2's JSON field for field.
- `attempt(state: Path, session: sessions.SessionRecord, sender: str, text: str, live: list[sessions.SessionRecord]) -> Attempt`, where `Attempt = Delivered | NotDelivered`. `Delivered(seconds: float, verbatim: bool)`. `NotDelivered(reason: PostmasterMiss, seconds: float)`, and `PostmasterMiss` is a `Literal` of: `absent` (no heartbeat file), `stale` (heartbeat older than the limit, unreadable, or its session is not in `live`), `self` (the recipient is the postmaster session), `unclaimed` (withdrawn at the deadline), `failed`, `expired`, `claimed-no-result`, `relay-only-sender`.
- `RELAY_ONLY_SENDERS = frozenset({"shutdown"})` (decided 2026-10-12 by the showrunner). `commands/message.md` tells every session to follow instructions from sender `shutdown`, and a recipient reads that name from the message wrapper, which names `postmaster` for a mod-sent message. A session that does not accept one never settles, and the user is paged after 5 minutes. So these messages keep the relay during the trial. The set is one line to remove once step 2 has shown that recipients read the real sender from the first line.
- Steps of `attempt`:
  0. `sender` is in `RELAY_ONLY_SENDERS`: return `relay-only-sender` at once, reading and writing nothing.
  1. Read `state / "postmaster" / "heartbeat.json"`. Absent, stale or self: return at once, writing nothing. This is the whole cost when the mod is not running: one file read.
  2. Write the entry to `outbox/.<id>.json.tmp`, then `os.replace` it to `outbox/<id>.json`. `id` is `uuid.uuid4().hex`; `deadline_ms` is now plus `WAIT_SECONDS`; `sha256` is the hash of `text`.
  3. Poll `results/<id>.json` every `POLL_SECONDS` until the deadline. `delivered`: remove the result and return `Delivered`, with `verbatim` true when the result's hash equals the entry's. `failed` or `expired`: remove the result and return `NotDelivered` with that reason.
  4. At the deadline, rename `outbox/<id>.json` to `outbox/.<id>.withdrawn` and remove it. The rename succeeded: the mod never claimed the entry and never will; return `unclaimed`.
  5. The rename failed with `FileNotFoundError`: the mod claimed the entry. Poll for the result until `CLAIM_GRACE_SECONDS` more have passed and follow it as in step 3. No result: return `claimed-no-result`.
- Why a message is never delivered twice: an entry leaves `outbox/` by exactly one rename. If the mod's rename wins, the mod sends and `send.py` relays only on a `failed` or `expired` result, which the mod writes only when it did not deliver. If `send.py`'s rename wins, the mod never sees the entry. A replaced hooks worker never sends an entry it finds in `claimed/` (Phase 2).
- The one path where a second copy can exist is `claimed-no-result`: the mod claimed the entry and wrote no result within 5 s, which means the hooks worker died or hung between its send call and its result write. `send.py` relays there (plan author's call: a lost message is the worse outcome, and the relay's own timeout already allows a second copy through the queue today; one-line revert to `enqueue`). The log names the reason, so step 2 counts it.
- In `send.send`, the Claude branch (1025–1039) becomes: resolve `relay_plan(message)` as today. For a `RelayAttempt`, read `live = sessions.live_sessions()` once and `session = sessions.addressed(plan.message.to, live)`. When `session` is not `None`, call `postmaster.attempt(STATE, session, message.sender, delivered_text(message), live)`. `Delivered` → `Result("sent", f"postmaster; {'verbatim' if verbatim else 'NOT VERBATIM'}; session:{session['sessionId']}")`. `NotDelivered`, or no live session for the address (a bare name only ListAgents knows) → `relay(plan.message, options.timeout)` and everything after it exactly as today, including `enqueue` on `queued`.
- `delivered_text(message)` is `f"Message from {message.sender} via postmaster: {message.summary}\n\n{message.text}"`. The mod sends under its own name, so the first line carries the real sender and stands alone as the preview. The relay path keeps `-n message.sender` and the unchanged text.
- The log (`LogEntry`) gains three `NotRequired` fields, written only for a message that reached the Claude branch: `"via"`: `"postmaster"` or `"relay"`; `"postmaster"`: `"delivered"`, a `PostmasterMiss`, or `"unresolved"` (no live session for the address); `"seconds"`: wall time from the start of the Claude branch to its result, to 0.001 s. `log()` takes them as one optional argument. Existing fields, their order and every other log line are unchanged.
- `send.py`'s docstring "Delivery" paragraph says the postmaster is tried first and names the fallback.
- `postmaster.py status`, run as a script, prints one line: `postmaster: <running since …, heartbeat N s old | not running>` and today's counts from `log.jsonl` by `via` and by `postmaster` reason. It exits 0 when the heartbeat is fresh and 1 otherwise.
- Tests. `test_postmaster.py`, with a fake mod that is a function acting on the temp state directory and a fake clock, no sleep: each `PostmasterMiss` reason; sender `shutdown` with a fresh heartbeat returns `relay-only-sender` and reads no file; `Delivered` with a matching and a non-matching hash; the withdraw rename winning; the claim winning then a late `delivered`; `claimed-no-result`; an absent heartbeat writes no file anywhere. `test_send.py`: with no heartbeat every existing test passes untouched; a fresh heartbeat plus a `delivered` result gives `SENT: postmaster; …`, no relay call, exit 0, and a log line with `via: postmaster`; each miss reason calls the relay once with the unchanged message and logs `via: relay` with the reason; a `queued` relay after a miss still enqueues under the session id; a recipient that is the postmaster session relays; a message from sender `shutdown` with a fresh heartbeat calls the relay once, writes no outbox entry and logs `via: relay`, `postmaster: relay-only-sender`; a keyed send delivered by the postmaster retires its queued copy.

**Files:**
- `scripts/message/postmaster.py` — the client: types, `attempt`, `status`.
- `scripts/message/send.py` — the Claude branch, `delivered_text`, the three log fields, the docstring.
- `scripts/message/test_postmaster.py` — the client's cases.
- `scripts/message/test_send.py` — the routing and log cases.

**Seats:** 1 writer + 1 tester — the client and its one call site, tested from the Spec's formats and outcome lines.
- `impl` — `scripts/message/postmaster.py`, `scripts/message/send.py`.
- `test` — `scripts/message/test_postmaster.py`, `scripts/message/test_send.py`.

**Constraints from prior phases:** Phase 2 fixed the entry, result and heartbeat JSON and the rule that the mod sends only what it renamed into `claimed/`; Phase 3 added the entry's `sender` field. Assumed here, and replaced by Phase 1's measured answer where it differs: (1) the mod reaches a recipient under either natedev account; if it reaches only its own account's sessions, `attempt` returns a new reason `other-account` for the rest, read from the recipient's registry record; (2) the recipient sees `postmaster` as the sender, which is why `delivered_text` adds a first line; if it sees the real sender, `delivered_text` returns the text unchanged and `RELAY_ONLY_SENDERS` is not added; (3) the mod's send is quick enough that 3 s covers the 0.5 s poll and the send.

**Acceptance gate:** the message suite green, `test_postmaster` included; basedpyright clean on the four files; with `STATE/postmaster/` absent, `send.py` gives the same outcome line, exit code, queue file and relay call as before this phase, shown by the untouched existing tests; a test shows a `delivered` result with no relay call; a test shows each miss reason relayed once, `relay-only-sender` among them.

**Verification:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_postmaster test_send test_sessions test_roster` · `basedpyright scripts/message/postmaster.py scripts/message/send.py scripts/message/test_postmaster.py scripts/message/test_send.py` · `cd scripts/whoami && HOME=<scratch home> python3 -m unittest test_quota_alert test_five_hour` (both import `send`).

### Phase 5 — The live start-up, the log check, the receiving rule and the as-built  · status: todo

#### Work Order

**Goal:** the postmaster session runs on natedev and delivers a throwaway message end to end; the log holds what step 2 reads; the documents say what shipped and how to start, check and stop it.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- `commands/message.md`, loaded after `/succinct_style`. "Receiving" gains one rule: a message whose sender is `postmaster` was sent by a script; its first line, `Message from <name> via postmaster:`, names the real sender; answer that name and never `postmaster`. "Sending" says `send.py` delivers through the postmaster session when one runs and relays otherwise. `codex/AGENTS-messaging.md` is not changed: a Codex recipient is never reached through the postmaster, and the shared "Finding sessions" words do not change.
- `docs/as-built/session-roster.md`, the amend:
  - `### send.py: one recipient`: the delivery paragraph names the postmaster attempt, the 3 s wait, each miss reason and the relay fallback; the outcome table's `SENT` row notes `postmaster; …`.
  - `### Postmaster mod (trial)`: what the mod is and where it lives; the state directory and its three JSON formats; the claim rename and why a message is delivered once; the `claimed-no-result` path and the choice made there; reply handling; the measured API table from Phase 1; the start, check and stop procedure below; the log fields.
  - Key files, Invariants (the outbox rule; no settings change; the mod touches only `STATE/postmaster/`; tests never write the real outbox) and gotchas (the `tsc` command; the generated types folder; `--plugin-dir` hot-reloads the folder, so a pull on main reloads the running mod and the relay covers the pause; after a Claude Code update run `claude plugin validate --strict` and `claude plugin test`; a stopped postmaster session is started again by hand, and until then every message relays; `/home/natepiano/postmaster` is outside the repo and trusted per account, so a new account or a removed directory meets the folder-trust dialog).
  - Not verified, and written as such: whether `/shutdown restart` brings the postmaster session back with its `--plugin-dir` flag.
- The live procedure, run by the unit director after both seats are idle and after this branch is on main, since the session loads `~/.claude/mods/postmaster`. It messages no real session.
  1. Check: `postmaster.py status` prints `not running` and exits 1.
  2. Start: in a new tmux session named `postmaster`, in `/home/natepiano/postmaster`, with every `CLAUDE*` variable removed, run a shell whose child is `claude -n postmaster --plugin-dir /home/natepiano/.claude/mods/postmaster`. Confirm with `sessions.py id postmaster`. Pass: `postmaster.py status` exits 0 within 10 s.
  3. Start one throwaway recipient, `claude -n pm-check`, in a second window of a throwaway tmux session in the same directory.
  4. Send three messages with `send.py --here --to pm-check --from pm-trial --text …`. Pass: each prints `SENT: postmaster; verbatim; …` and exits 0; each returns in under 1.5 s; `pm-check`'s transcript shows each text once, with the first line naming `pm-trial`; `STATE/relay/` gained no file; `log.jsonl` has three lines with `via: postmaster`.
  5. Fallback: end the postmaster session's `claude` process with `SIGTERM` by its own pid, send one more message. Pass: it prints `SENT: mode auto; verbatim; …` through the relay, the log line says `via: relay`, `postmaster: stale` or `absent`, and `pm-check` shows it once.
  6. Reply: start the postmaster again as in step 2, send `pm-check` a message asking for an answer. Pass: the answer is in `replies.jsonl`, and the postmaster session's transcript shows no new turn.
  7. Leave the postmaster session running: this is the trial's start. End `pm-check` and its tmux session. Record the start time, in the user's zone after reading the clock, in the as-built.
- Log fields step 2 reads, all in `STATE/log.jsonl`: `time`, `outcome`, `detail`, `via`, `postmaster`, `seconds`, `from`, `to`. From them: the share delivered by the mod, relays a day against the baseline of about 600, delivery time against the relay's 4.5 s median, `queued` and `failed` counts, and the count of `claimed-no-result`. `replies.jsonl` gives replies consumed and forwarded. The as-built lists the one-line command for each number; nothing in this plan computes them.
- The checkpoint notice to the showrunner states the trial's start time and that step 2's two-day window begins there.

**Files:**
- `commands/message.md` — the receiving rule and the sending line.
- `docs/as-built/session-roster.md` — the amend and the mod's section, with the live results.

**Seats:** 2 writers — one document each; neither has a test lane.
- `impl` — `commands/message.md`.
- `test` — opens as impl: `docs/as-built/session-roster.md`.

**Constraints from prior phases:** Phase 1 wrote `### Postmaster mod (trial)` with the probe procedure and the measured table, and recorded the `tsc` command. Phases 2 and 3 built the mod: outbox, claim, result, heartbeat, reply handling, under `mods/postmaster/hooks/`. Phase 4 built `scripts/message/postmaster.py` (`attempt`, the miss reasons, `status`), the `SENT: postmaster; …` line, `delivered_text` and the three log fields. Phase 4 holds `RELAY_ONLY_SENDERS` unless Phase 1 found the recipient sees the real sender; when the set exists, the as-built names it, the reason, and that step 2's share is counted without those messages. If Phase 1 found that the recipient sees the real sender's name, the receiving rule is not added and step 4's first-line check reads the wrapper instead.

**Acceptance gate:** the live procedure's seven steps passed and their results are in the as-built; `commands/message.md` names the rule; the as-built's `send.py` section, invariants and gotchas describe what shipped; the message suite green; `claude plugin validate --strict mods/postmaster` and `claude plugin test mods/postmaster` green on main.

**Verification:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_postmaster test_send test_sessions test_roster` · `claude plugin validate --strict /home/natepiano/.claude/mods/postmaster` · `claude plugin test /home/natepiano/.claude/mods/postmaster` · `python3 scripts/message/postmaster.py status` exits 0 after the live procedure.
