# Postmaster trial: send.py delivers through a mod, with the relay as fallback

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** On natedev, one idle Claude session loaded with the `postmaster` mod delivers `send.py`'s Claude-bound messages from an outbox directory with no model request; `send.py` waits up to 3 s for the mod's result and otherwise relays as today.

> **As-built disposition: amend** — `docs/as-built/session-roster.md` (it owns `scripts/message/`), plus a new section there, `### Postmaster mod (trial)`

> **Production: build-followups** — unit `shutdown-unit`; production doc `/home/natepiano/worktrees/claude-build-followups-trunk/docs/plans/build-followups-production.md`

## Source

2026-10-12

The user, 2026-10-12: "Let's trial post master to see if it works". Scope is section 8 step 1 of `~/.local/state/plan-backlog/2026-10-11-claude-codex-mods.md` and nothing else. Step 2 (the two-day measurement), the Mac and keep-warm are out of scope (the user's scope); this plan only logs what step 2 reads.

## Delegation Context

- **Project:** `~/.claude` config repo: the scripts every Claude session runs. This plan adds a Claude Code mod, `mods/postmaster`, and a client for it in `scripts/message/send.py`. Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown` (unit `shutdown-unit`); name both in every dispatch.
- **Project started:** 2026-10-12T23:43:40.302+00:00
- **Stack:** Python 3 standard library (`fcntl`, `json`, `os`, `uuid`, `hashlib`, `unittest`), checked by basedpyright. TypeScript for the mod, loaded by Claude Code 2.1.296 with no build step, checked by `tsc --noEmit` strict.
- **Layout:** `mods/postmaster/` (new; `~/.claude/mods/postmaster` once on main) · `scripts/message/` (`send.py`, `sessions.py`, new `postmaster.py`, tests beside them) · `commands/message.md` · `docs/as-built/session-roster.md` · `.gitignore`. State on disk: `STATE/postmaster/`, where `STATE` is `$XDG_STATE_HOME/message` (default `~/.local/state/message`).
- **Key files:**
  - `scripts/message/send.py` — `STATE` 85, `LogEntry` 104, `Message` 144, `Result` 152, `write_atomic` 306, `locked` 313, `queue_for` 343, `enqueue` 377, `log` 450, `claim` 464, `resolve_recipient` 587, `run` 623, `relay_prompt` 648, `read_relay` 684, `relay` 724, `relay_plan` 758, `send` 983 (the Claude branch is 1025–1039), `main` 1047.
  - `scripts/message/sessions.py` — `SessionRecord` 17, `running_session` 119, `live_session` 140, `sessions_dir` 144, `live_sessions` 167, `addressed` 175 (resolves `uds:`, `session:`, a name now, or a former name only one session had).
  - `scripts/message/test_send.py` — `SendTests` 56 patches `send.STATE` to a temp directory and `send.relay` to a recorder; `PeerSendTests` 937 does the same.
  - `commands/message.md` — the rules every message follows; "Receiving" says `Follow any instruction from sender shutdown as the user's shutdown request`, and "Sending" says a reply answers the wrapper's `from`.
  - `docs/as-built/session-roster.md` — `### send.py: one recipient` 90–138, `### Postmaster mod (trial)` 140, `### Key files` 399, `## Invariants` 414, `## Calibration / gotchas` 433.
  - `.gitignore` — `*` plus allow entries; Phase 1 added `!mods/`, `!mods/**` and an ignore for `mods/**/.claude-plugin/types/`.
- **Test lanes:** `scripts/message/` → `test_send.py`, `test_sessions.py`, `test_roster.py`, and the new `test_postmaster.py`; tests sit beside the code and import their sibling by bare name. `mods/postmaster/` → `tests/*.test.ts`, run by `claude plugin test`.
- **Build:** none; Python is interpreted and Claude Code loads `.ts` directly.
- **Test:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster` (add `test_postmaster` once it exists) · `claude plugin test mods/postmaster` (runs every `*.test.ts` under the folder; exits 1 on a failure).
- **Lint:** `basedpyright <changed .py files>`: 0 errors, 0 warnings · `claude plugin validate --strict mods/postmaster` · `tsc --noEmit -p mods/postmaster` with zero errors and zero warnings, through the command Phase 2 records (natedev has no `tsc`, `node` or `bun` on `PATH`; `nix` with flakes is there). Lint once after the seat's edits; on failure fix what it names, then lint once.
- **Mod API, and how far each fact is confirmed:**
  - From the backlog document's sources: a mod is a plugin whose `hooks/hooks.json` names one module (`"modules": ["./register.ts"]`); a handler is `on(event, matcher, async ($, e, next) => ...)`; `$.session.send({ to: { sessionId }, text })` delivers with no model turn; `session.receive` observes or consumes an arriving message; a hook that throws or uses its 10 s is skipped; `--plugin-dir` loads a folder and hot-reloads it.
  - Read from the 2.1.296 binary's own text, not exercised: `import type { Register } from 'claude-code'` and `export const register: Register = on => { ... }`; `on(...).catch(($, e, next) => ...)`; `$.session.send` "takes { to, text }: to a name, an agent id or an address (a non-empty string), { sessionId } or { agentId }"; it refuses with "this session has no SendMessage tool" where messaging is off, and reports "not sent" or "not delivered"; a `session.receive` hook can consume a delivery ("consumed by a hook; not queued"); the names `$.clock.now`, `$.fs.write`, `$.process.run`, `$.store.set`, `$.state.get`; the engine writes the type declarations to `<mod>/.claude-plugin/types/claude-code/index.d.ts` once it has loaded the mod from its folder; one hooks worker serves every plugin and is replaced when it dies.
  - Confirmed from the CLI's help: `claude plugin validate [--strict] [--json] <path>`; `claude plugin test [dir]`, whose test files import their kit from `claude-code/testing`; `claude --plugin-dir <path>`; `claude -n <name>`.
  - Shown by `claude plugin validate --strict` on Phase 1's probe, 2026-10-12: a mod's calls are direct `$.noun.method(...)` sites; `$.env.get` takes a literal name; `$.fs` has `exists`, `list`, `read`, `write`, `stat` and `ancestors`, and no rename or remove; `import()` and a re-exported `register` are refused, so a mod imports only its own files with import declarations; `$.session.id` gives the session id, and nothing gives the session's pid.
  - Everything else about the mod API is a point Phase 2 measures. No Work Order may treat an unverified point as a fact; each later Work Order states what it assumes.
- **Invariants:**
  - With no fresh heartbeat, `send.py` behaves exactly as today: same outcome line, exit code, queue files and relay. This holds at the end of every phase.
  - The guarantee is at least once, never lost: every path without a confirmed `delivered` result from the mod ends in the relay or in today's queue. When both sides decline (the mod finds the entry gone, `send.py` finds a claim), the mod writes `withdrawn` and `send.py` relays; with no result at all, `send.py` relays after the grace time. A second copy is possible only on that last path, `claimed-no-result`.
  - A claim file decides who delivers, since a mod cannot rename or delete a file. The mod writes `claimed/<id>.json`, then checks that `outbox/<id>.json` still exists, and sends only if it does. `send.py` relays only after it took the entry out of `outbox/` and then found no claim, or after the mod reported that it did not deliver.
  - Never a marketplace mod. No `enabledPlugins`. No edit to any `settings.json` (the user's rule for this trial).
  - The mod reads and writes only under `STATE/postmaster/`. It starts no process and makes no network call.
  - Never use `--dangerously-skip-permissions`. Never read `.credentials.json` or `.claude.json`. Never type into a pane.
  - Tests are hermetic (`docs/as-built/session-roster.md`, Invariants): temp state, no real session, no real relay, no real outbox.
  - Python: basedpyright 0 errors and 0 warnings on changed files; no file-level ignores; no `Any` (a `TypedDict` per JSON format).
  - TypeScript: `strict`, no `any`, no `@ts-ignore`, no `@ts-expect-error`.
  - Live steps belong to the unit director, never to a seat, and never message a real session.

## Phases

### Phase 1 — The probe mod and its written live-check procedure  · status: done

#### As-built

- `mods/postmaster` is a Claude Code mod folder loaded only with `--plugin-dir` (no marketplace field); `claude plugin validate --strict mods/postmaster` exits 0. Its probe is driven by files in `STATE/postmaster/probe/` (`STATE` is `$XDG_STATE_HOME/message`, default `~/.local/state/message`). On `session.start` the probe writes `started.json` (the session id from `$.session.id`, the hooks worker's pid, how each was read, what `$.env.get` answered for `HOME` and `XDG_STATE_HOME`, whether the worker has a process environment), appends a `start` line to `report.jsonl`, then checks `request.json` every 0.5 s on `globalThis.setInterval`.
- A request is `{"id": string, "step": string, "to_session_id": string, "to_socket": string, "text": string, "count": number}`. Steps: `send-by-id` (`$.session.send({ to: { sessionId }, text })`), `send-by-address` (`to` is the string `uds:<socket>`), `send-dead` (an id no session has; records throw or return, and the answer), `send-self`, `burst` (`count` timed sends in a row to one session), `files` (write, list, read, exists, then a second write and read of the same path, through `$.fs`), `timer` (detail `mechanism: "globalThis.setInterval"`, `interval_ms: 500`, `tick_interval_ms: 60000` and the four tick values), `consume`. A step whose API does not exist is recorded `ok: false` with the reason.
- Each `report.jsonl` line carries `run`, `id`, `step`, `ok`, `detail`, `started_ms`, `ended_ms`; `run` is the hooks worker's start time in ms, the same on every line that worker writes. Line kinds: `start` (detail `{"session_id", "already_running"}`; a second with the same `run` means the event fired twice in one worker, a new `run` means the worker was replaced); `tick` every 60 s with `poll_count` (timer callbacks since the worker started), `max_gap_ms` (longest gap between two timer callbacks inside the minute the tick closes), `completed_count` (request-file checks finished since the worker started) and `max_completed_gap_ms` (longest time between two finished checks inside that minute, counting a gap still open at the tick); `request-error` (`ok: false`; id is the request's own string id, else `request`; once per distinct file text; an absent file writes nothing); `hook-error`; one result line per request `id`.
- Invariants: a new worker reads `report.jsonl` first and never reruns a request an earlier worker reported; a result whose line fails to write is kept and written at the next check, so a step never runs twice; the probe never throws out of a hook, and every handler has a `.catch`.
- `received.jsonl` gets one line per arriving `session.receive` event with every field the event carries, and the event goes on through `next(e)`. Once `consume` arms it (in memory, per worker), the handler takes the next arriving message whose text contains the request's `id`: it appends the event with `consumed: true` and `consume_request_id`, returns `{ consumed: <reason> }` and does not call `next(e)`.

**Files:**
- `.gitignore` — `!mods/` and `!mods/**` allow the folder; `mods/**/.claude-plugin/types/` is ignored (the engine writes the types there at a live load).
- `mods/postmaster/.claude-plugin/plugin.json` — manifest: name `postmaster`, version 0.1.0, a description.
- `mods/postmaster/hooks/hooks.json` — `{"modules": ["./register.ts"]}`.
- `mods/postmaster/hooks/register.ts` — `register: Register`; builds the typed call set from direct `$` call sites inside the `session.start` handler; the `session.receive` handler returns `{ consumed }` or calls `next`.
- `mods/postmaster/hooks/probe.ts` — `start(api: ProbeCalls, event: unknown): Promise<void>`, `receive(event: unknown): Promise<ReceiveDisposition>`, `failed(event: unknown): Promise<void>`; exports `ProbeCalls` (`env.get`; `fs.read`, `write`, `exists`, `list`; `session.id`, `send`) and `SessionDestination = string | { sessionId: string }`.
- `mods/postmaster/tsconfig.json` — `strict`, `noEmit`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`; `claude-code` mapped to `.claude-plugin/types/claude-code/index.d.ts`.
- `docs/as-built/session-roster.md` — `### Postmaster mod (trial)`: the 16-step live-check procedure, its four pass criteria (step 15), and the table `Mod API on Claude Code 2.1.296, measured <date>` with every row `not yet measured`.

**Binds later work:**
- The live check runs the procedure and reads `started.json`, `report.jsonl` and `received.jsonl` as its evidence; its timer criterion reads the `tick` values of one `run` (ten ticks in a row, `max_gap_ms` and `max_completed_gap_ms` at most 2000, both counts rising).
- `register.ts` builds the typed call set inside the `session.start` handler from direct call sites and hands it to the module. The module that delivers outbox entries and the reply handler follow that pattern and never touch `$`.
- With no rename in `$.fs`, a claim file decides who delivers, in the delivery module and in the `send.py` change: claim, then check; a `withdrawn` result; at least once, never lost.
- Whether `$` stays usable inside timer callbacks after the `session.start` handler returns is unmeasured until the live check; the delivery module's poll depends on it.
- The local hook and call types in `register.ts` are not yet checked against the generated `Register` type; the live check does that.

**Gotchas:**
- The validator accepts only direct `$.noun.method(...)` call sites and literal `$.env.get` names; `import()` and a re-exported `register` are refused.
- `$.fs` has `exists`, `list`, `read`, `write`, `stat`, `ancestors`; no rename, no remove.
- Nothing gives the session's pid; `started.json` holds the hooks worker's pid.
- The generated declarations appear only after a live load, so until then `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster` reports only `TS2307` for `claude-code`.
- A `session.receive` hook that answers without `next` consumes the delivery; `consumed` is the reason text.
- The probe is temporary and has no committed tests: it is removed when the outbox delivery module replaces it.

**Ruled out:** a rename deciding who delivers (no rename in `$.fs`; a claim file decides instead); reaching `node:fs` through a dynamic import (refused by the validator); building later work ahead of the live facts (a failed pass criterion stops the trial).

### Phase 2 — The live check of the mod API  · status: done

#### As-built

- `docs/as-built/session-roster.md` → `### Postmaster mod (trial)` holds the measured mod API table for Claude Code 2.1.296 (20 rows, each with an answer and an evidence line) and the corrected live procedure. The measured run used four sessions; the fourth, D, is on account claude 1.
- Measured and holding: a send by session id or `uds:` address reaches a session in another process; the mod writes, lists, reads and tests files under its state directory, and another process sees a file when the write returns; ten minute ticks in a row for one `run` show `max_gap_ms` and `max_completed_gap_ms` at most 2000 with `poll_count` and `completed_count` rising; an arriving message that matches an armed consumption id is consumed and starts no turn.
- `session.receive` carries only `origin` and `text`; the sender sits inside the wrapper. A refused send (dead session, self, the rate limit of about 32) is a returned answer with `isDelivered` false, never a throw.
- `register.ts` and `probe.ts` take their hook and call types from the engine's generated declarations in `mods/postmaster/.claude-plugin/types/` (ignored by git). `tsconfig.json` checks strictly against those types, ES2023 without DOM, tests included.
- `tests/kit.test.ts` has four cases: the fake clock, a send answered by a test hook, file calls served from memory, and a raised `session.receive` either consumed or let through. The kit has no ready fake for a send or for files; the test's own hooks answer them.

**Files:**
- `docs/as-built/session-roster.md` — the measured table and the corrected procedure.
- `mods/postmaster/hooks/register.ts`, `mods/postmaster/hooks/probe.ts` — the probe, typed by the engine's declarations.
- `mods/postmaster/tsconfig.json` — strict check against the generated types.
- `mods/postmaster/tests/kit.test.ts` — what the kit provides, one case each.

**Binds later work:**
- The outbox poll (`startOutbox`) sends by session id, treats `isDelivered` false as failed, does not pace, keeps `setInterval`, and is tested through a hand-written call set.
- The reply handler (`startReplies`) keys replies by the wrapper's socket and tags forwards.
- `send.py`'s postmaster client (`new_entry_id`) tags every delivered text with eight characters of the entry id, reads a torn heartbeat again, and needs no account check; its test has two equal sends write different texts.
- The receiving rule quotes the new first line, and its live step sends three equal texts that must all arrive.
- The restart fix locates the pane and the tmux session after a rename.

**Gotchas:**
- The bare `claude` name is a shell alias: spell the CLI `command claude`.
- The type check is `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster`.
- A recipient drops a text equal to the previous one from the same sender, while the send still answers delivered.
- An overwrite through `$.fs.write` can be read torn by another process.
- The kit's fake clock moves `$.clock` only, not `setInterval`.

**Ruled out:** `$.clock.every` for the poll — not measured live; `setInterval` was.

### Phase 3 — The mod delivers outbox entries and writes a result for each  · status: todo

#### Work Order

**Goal:** a session loaded with the mod delivers each entry in `STATE/postmaster/outbox/` once, writes its result, and keeps a heartbeat.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. This phase changes no Python; `send.py` writes no entry yet.

- Directories under `STATE/postmaster/`: `outbox/`, `claimed/`, `results/`. A write creates its directory, and `send.py` creates `outbox/`. `STATE` is `$XDG_STATE_HOME/message`, else `~/.local/state/message`.
- An entry is `outbox/<id>.json`: `{"id": string, "session_id": string, "socket": string, "text": string, "sha256": string, "deadline_ms": number}`. `id` is 32 hex characters. `text` is delivered byte for byte; the mod adds nothing. The writer of an entry makes each text differ from the one before it (Phase 5 puts the real sender and a tag from the entry id on the first line): a recipient drops a message whose text equals the previous one from the same sender while the send still answers delivered (measured). `deadline_ms` is the Unix time after which the sender has stopped waiting. Files whose name starts with `.` are writers' temp files and are never read.
- The poll, every 500 ms (`POLL_MS`), started with `calls.every(POLL_MS, fn)`, which `register.ts` wires to `globalThis.setInterval`; `calls.now` is `Date.now`. That is the mechanism Phase 2 measured through twelve idle minutes; `$.clock.every` was not measured live and is not used:
  1. Write `heartbeat.json` with one `$.fs.write`: `{"session_id": string, "at_ms": number, "version": string}`. A mod has no rename and its overwrite can be read torn by another process (measured), so a reader that cannot parse the heartbeat reads it again before it treats it as stale (Phase 5).
  2. List `outbox/`. For each `<id>.json` that has no `claimed/<id>.json`, oldest first: read the entry, write `claimed/<id>.json` (`{"id": string, "claimed_ms": number, "run": string}`, where `run` names this worker's start), then check that `outbox/<id>.json` still exists. Gone: the sender withdrew it; write result `withdrawn` and do not send. The order is the rule: the claim is written before the check.
  3. An entry that is unreadable or misses a field: result `failed`, detail `unreadable entry`. `deadline_ms` already past: result `expired`, no send.
  4. Otherwise call `calls.session.send({ to: { sessionId: entry.session_id }, text: entry.text })` once. The answer `{ isDelivered: true }` is `delivered`. `{ isDelivered: false, reason }` is `failed` with `reason` in `detail`: a dead session, the mod's own session and the rate limit all answer this way without a throw (measured). A throw says nothing about whether the message went out, so its result is `abandoned` with the error's text in `detail`.
  5. Write `results/<id>.json` with one `$.fs.write`: `{"id": string, "outcome": "delivered" | "failed" | "expired" | "withdrawn" | "abandoned", "detail": string, "sha256": string, "claimed_ms": number, "finished_ms": number}`, where `sha256` is the hash of the text the mod passed to `$.session.send`. The mod removes nothing: `send.py` removes the entry, the claim and the result.
- At each poll, a claim whose `run` is not this worker's and that has no result gets result `abandoned`, and its entry is never sent: the earlier worker may have sent it and died before writing its result, so the delivery is unknown. This is what keeps a replaced hooks worker from sending a stale entry.
- Entries are handled one at a time in one poll, at most `BATCH` (20) per poll. The mod does not pace its sends: once about 32 sends went to one session in a short time the engine refuses the next ones (then lets about one through every 2 s, measured) with `isDelivered: false`, which is a `failed` result, and `send.py` relays that message. A second poll never starts while one runs. The heartbeat is rewritten before each entry of a batch, so a slow send never leaves it older than that one send.
- The mod sweeps nothing, since it cannot remove a file. `send.py` removes what it reads, and Phase 5's client removes leftovers.
- Every handler and the poll body have a `.catch` that writes the error as one file, `STATE/postmaster/errors/<at_ms>-<n>.json` (`<n>` counts this worker's errors), and never rethrows; one bad entry never stops the poll.
- `mods/postmaster/hooks/probe.ts` is deleted in this phase; `register.ts` wires the poll instead. `probe.ts` holds working code for finding the state directory; the writer carries it into `outbox.ts` first. No file that grows is ever rewritten whole: the engine rejects a read or a write over 4 MiB, so each record is its own file. The as-built gets the one sentence named under Files.
- Source layout: `hooks/protocol.ts` (the three JSON types, their validators, the path names, and the types `PostmasterCalls` and `Outbox`), `hooks/outbox.ts` (poll, claim, deliver, result, heartbeat), `hooks/register.ts` (wiring only).
- The interface the tester writes against, fixed here so both seats start at once. `PostmasterCalls` is built from the engine's own types the way `ProbeCalls` in `probe.ts` is today: `fs` (`write`, `exists`, `list`, and a text `read`), `session` (`id`, `send`), `env` (`get`), plus `now(): number` and `every(ms: number, fn: () => void): { cancel(): void }`. `hooks/outbox.ts` exports `startOutbox(calls: PostmasterCalls): Promise<Outbox>`. `Outbox` has `poll(): Promise<void>`, which runs one poll and is what the timer calls, and `cancel(): void`. `startOutbox` finds the state directory through `calls.env.get` (`XDG_STATE_HOME`, else `HOME`), names its `run`, and starts the timer.
- Tests under `mods/postmaster/tests/` import `describe`, `test` and `expect` from `claude-code/testing` and call `startOutbox` with a call set written by hand in the test file, since the kit has no ready fake for a send or for files: files in a `Map`, a `send` that records its argument and answers what the case sets, a `now` the case moves, and an `every` that records its function. A case runs a poll with `outbox.poll()`. Cases: an entry is delivered once and its result says `delivered` with the right hash; an entry withdrawn after the claim is written gets no send and result `withdrawn`; with the mod's steps named t1 (write the claim) and t2 (check the entry) and the sender's s1 (take the entry out) and s2 (check the claim), every order of the four is run against a fake sender, and in each the mod sends exactly when the entry was present at t2 and writes `withdrawn` otherwise; an expired entry is not sent; a send answered `isDelivered: false` gives `failed` with the reason in `detail`; a send that records its delivery and then throws gives `abandoned` with the error's text; a malformed entry gives `failed` and the next entry still goes; an entry whose claim an earlier run wrote is not sent; the heartbeat is rewritten each poll, and before each entry of a batch whose sends each take 1.5 s; a claim an earlier run wrote with no result gets `abandoned` and no send; two entries for one session arrive in file order.

**Files:**
- `mods/postmaster/hooks/protocol.ts` — entry, result and heartbeat types with validators.
- `mods/postmaster/hooks/outbox.ts` — the poll and its steps.
- `mods/postmaster/hooks/register.ts` — wires the poll at `session.start`.
- `mods/postmaster/hooks/probe.ts` — deleted.
- `docs/as-built/session-roster.md` — one sentence above the live-check procedure in `### Postmaster mod (trial)`: the probe was removed from the tree after the measurement, commit `cd85b091` holds it, and the procedure stays as the record of how the table was measured.
- `mods/postmaster/tests/outbox.test.ts` — the cases above.
- `mods/postmaster/tests/protocol.test.ts` — validator cases.

**Seats:** 1 writer + 1 tester — the Spec fixes every file format and outcome, so the tests are written from it.
- `impl` — `mods/postmaster/hooks/protocol.ts`, `mods/postmaster/hooks/outbox.ts`, `mods/postmaster/hooks/register.ts`, `mods/postmaster/hooks/probe.ts`, `docs/as-built/session-roster.md`; hub: `mods/postmaster/hooks/protocol.ts` (the tester imports its types).
- `test` — `mods/postmaster/tests/outbox.test.ts`, `mods/postmaster/tests/protocol.test.ts`.

**Constraints from prior phases:** Phase 1 built the mod folder, its manifest and `tsconfig.json`. Phase 2 measured the mod API on Claude Code 2.1.296; the table is in `docs/as-built/session-roster.md` → `### Postmaster mod (trial)`. What this phase uses: (1) `globalThis.setInterval` at 500 ms ran through twelve idle minutes in one hooks worker, worst gap 644 ms, and engine calls stay usable after the start hook returns; (2) `$.fs` writes (creating parent directories), lists, reads and tests files, has no rename or remove, and its overwrite can be read torn by another process; (3) `$.session.send({ to: { sessionId }, text })` reaches a session in another process under all three natedev accounts in 80 to 300 ms and answers `{ isDelivered: true }`, while a dead session, the mod's own session and the rate limit answer `{ isDelivered: false, reason }` without a throw; (4) `$.session.id()` gives the mod's session id, `process.env` is absent, and `$.env.get` reads `HOME` and `XDG_STATE_HOME`; (5) the kit (`claude-code/testing`) exports `describe`, `expect`, `mock`, `test` and `tier` and has no ready fake for a send or for files, so this phase's tests fake the call set by hand, and `tests/kit.test.ts` stays as it is; (6) a recipient drops a message whose text equals the previous one from the same sender. Phase 2 left `register.ts` typed by the engine's own `Register` (`export const register: Register = (on) => { … }`, handler types inferred): it builds `ProbeCalls` inside the `session.start` handler from direct `$.noun.method(...)` sites with literal `$.env.get` names and passes it to the module, and its `session.receive` handler has a `.catch`. `hooks/outbox.ts` takes the call set and never touches `$`. `tsconfig.json` uses the generated types (`typeRoots` `./.claude-plugin/types`), ES2023 without DOM, and includes `tests/**/*.ts`. Spell the CLI `command claude`: the bare name is a shell alias that adds options.

**Acceptance gate:** `command claude plugin test mods/postmaster` green with the named cases; `command claude plugin validate --strict mods/postmaster` exits 0 and its `calls:` line names only `$.env.get`, `$.fs.exists`, `$.fs.list`, `$.fs.read`, `$.fs.write`, `$.session.id` and `$.session.send`; the `tsc` command under Verification exits 0; the message suite green and unchanged.

**Verification:** `command claude plugin test mods/postmaster` · `command claude plugin validate --strict mods/postmaster` · `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster` · `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster`.

### Phase 4 — The mod consumes replies so no turn starts in its session  · status: todo

#### Work Order

**Goal:** a reply that lands at the postmaster session is consumed and logged, starts no turn there, and is forwarded when its target is known and live; any other arriving message passes on unchanged.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- The mod keeps, per recipient socket address (the entry's `socket`, `uds:` plus a path), every delivery it made there inside `REPLY_WINDOW_MS`: `{"sender": string, "reply_to_session_id": string, "session_id": string, "at_ms": number}`. The socket is the key because it is what an arriving reply names. The entry gains two fields for this, added to `hooks/protocol.ts`: `"sender": string` (the `--from` name) and `"reply_to_session_id": string` (the one live session with that name, resolved by `send.py`; empty for a script sender or a name no single session has). The mod never puts either into the delivered text. The map is kept in memory and saved to `STATE/postmaster/delivered.json` after each delivery, so a replaced hooks worker reads it back.
- `session.receive` handler:
  - Only an event whose `origin.kind` is `"peer"` is read as a reply; the same wrapper text under any other origin passes on.
  - What arrives (measured): the event has only `origin` and `text`. For a message from another session `origin.kind` is `"peer"` and `text` is the wrapper: a first line `<cross-session-message from="uds:/…sock" from-name="<name>" from-mode="…">` (a mod-sent message also has `from-plugin="…"`), the body, then a last line `</cross-session-message>`. The handler reads `from` and `from-name` from the first line and takes the body between the two lines. A text that does not start with that opening tag passes on.
  - The wrapper's `from` is a socket the mod delivered to within `REPLY_WINDOW_MS` (30 minutes): this is a reply. Write one file, `STATE/postmaster/replies/<at_ms>-<n>.json` (`at_ms`, the replying session's socket, name and session id, the original sender, the body, `forwarded`), and consume the event by returning `{ consumed: "postmaster reply" }`, so nothing is queued and no turn starts (measured).
  - Forwarding: to each distinct `reply_to_session_id` among the deliveries to that session inside the window, send `Message from <replying session name> (their answer to <sender>, passed on by postmaster) [<at_ms>-<n>]:`, a blank line, then the body. `<n>` counts this worker's forwards, and the tag keeps two equal answers from being dropped as a repeat. The send goes to `{ sessionId: <reply_to_session_id> }`, and an answer of `isDelivered: false` leaves that id out of `forwarded`. With more than one sender in the window the mod cannot say which one the answer is for, so each gets it and the first line says `their answer to one of <sender>, <sender>`: a second copy is the lesser fault, a lost answer the greater. Log `forwarded` as the list of session ids sent to. An empty `reply_to_session_id` (a script sender such as `quota_alert`) has nowhere to go today; log it with an empty list.
  - Anything else passes on with `next(e)` unchanged: a message from a session the mod never delivered to, a relay's message, the user's own prompt. An account-wide `/shutdown` and a broadcast therefore reach the postmaster session as they reach any idle session.
- The mod reads no session registry: it stays inside `STATE/postmaster/`. `send.py` resolves the sender to a session and puts its id in the entry.
- The handler has a `.catch` that passes the event on with `next(e)`: a failing handler must never swallow a message.
- The interface the tester writes against: `hooks/replies.ts` exports `startReplies(calls: PostmasterCalls): Promise<Replies>`. `Replies` (declared in `protocol.ts`) has `sending(entry: Entry, atMs: number): Promise<void>`, which the outbox calls before the send so a quick answer finds its route, `notDelivered(entry: Entry): Promise<void>`, which it calls when the send was not delivered and which takes that route out again, and `receive(e: SessionReceiveInput): Promise<{ consumed: string } | { pass: true }>`. `startOutbox` gains a second argument, the `Replies` it tells of each send. `register.ts` returns the consumed answer or `next(e)`. Each handler builds the call set from its own `$`, and one `Replies` serves a hooks worker: whichever handler runs first starts it, reading `delivered.json` back, and the outbox and the receive handler share it, so a reply is recognized whether or not `session.start` ran in this worker.
- Tests call `startReplies` with a call set written by hand, as Phase 3's tests do. Cases: a reply that arrives while the fake send is still running is consumed; a receive before and one after a further delivery both see the same map; the wrapper of a delivered-to socket under an origin other than `peer` is passed on; a text with no wrapper is passed on; a forward answered `isDelivered: false` is logged without that session; two equal replies forwarded in a row carry different first lines; a reply from a delivered-to session is consumed, logged and forwarded to the one live session with the sender's name; a reply whose sender is a script name is consumed and logged with `forwarded: []`; a message from a session never delivered to is passed on untouched; a reply after the window is passed on; a throwing forward still consumes and logs; the saved map is read back at start; with two senders to one session inside the window, each gets the reply and its first line names both; an entry with an empty `reply_to_session_id` is logged with an empty list.

**Files:**
- `mods/postmaster/hooks/protocol.ts` — the `sender` field; the delivered-map and reply-log types.
- `mods/postmaster/hooks/replies.ts` — the `session.receive` handler, the map, the forward.
- `mods/postmaster/hooks/outbox.ts` — records each delivery in the map.
- `mods/postmaster/hooks/register.ts` — wires the handler.
- `mods/postmaster/tests/replies.test.ts` — the cases above.
- `mods/postmaster/tests/outbox.test.ts` — the second `startOutbox` argument, and the route registered before the send.
- `mods/postmaster/tests/protocol.test.ts` — the two new entry fields.

**Seats:** 1 writer + 1 tester — one handler and its map; the Spec fixes every outcome.
- `impl` — `mods/postmaster/hooks/protocol.ts`, `mods/postmaster/hooks/replies.ts`, `mods/postmaster/hooks/outbox.ts`, `mods/postmaster/hooks/register.ts`; hub: `mods/postmaster/hooks/protocol.ts`.
- `test` — `mods/postmaster/tests/replies.test.ts`, `mods/postmaster/tests/outbox.test.ts`, `mods/postmaster/tests/protocol.test.ts`.

**Constraints from prior phases:** Phase 3 built `hooks/protocol.ts` (entry, result, heartbeat, `PostmasterCalls`, `Outbox`), `hooks/outbox.ts` (`startOutbox`, poll, claim, deliver, result) and the `.catch` rule. `register.ts` builds the typed call set inside a handler from direct `$.noun.method(...)` sites and passes it to the module; `hooks/replies.ts` takes that call set and never touches `$`. Measured by Phase 2 on Claude Code 2.1.296: (1) a session answered a mod-sent message by sending to the mod session's `uds:` address with no permission prompt, and it arrived there as a `session.receive` event with `origin: { kind: "peer" }`; (2) the event carries only `origin` and `text`, and the sender's socket and name are inside the wrapper text; (3) returning `{ consumed: reason }` consumed it and started no turn; (4) the recipient sees the mod session's name with `from-plugin="postmaster"`, never the original sender, so replies do land at the postmaster session and the full handler ships; (5) a send answers `{ isDelivered: false, reason }` without a throw for a dead session and for the rate limit; (6) a recipient drops a message whose text equals the previous one from the same sender; (7) whether a replaced hooks worker gets a new `session.start` was not measured. Spell the CLI `command claude`.

**Acceptance gate:** `command claude plugin test mods/postmaster` green with the named cases; `command claude plugin validate --strict mods/postmaster` exits 0; the `tsc` command under Verification exits 0; the message suite green and unchanged.

**Verification:** `command claude plugin test mods/postmaster` · `command claude plugin validate --strict mods/postmaster` · `nix shell nixpkgs#typescript --command tsc --noEmit -p mods/postmaster` · `cd scripts/message && HOME=<scratch home> python3 -m unittest test_send test_sessions test_roster`.

### Phase 5 — send.py tries the postmaster first and relays on every other path  · status: todo

#### Work Order

**Goal:** `send.py` delivers a Claude-bound message through the outbox when the heartbeat is fresh, relays on every path without a `delivered` result, never causes two deliveries, and logs which path each message took.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- New module `scripts/message/postmaster.py`; `send.py` imports it. Constants: `WAIT_SECONDS = 3.0`, `CLAIM_GRACE_SECONDS = 2.0`, `HEARTBEAT_MAX_AGE_SECONDS = 2.0`, `POLL_SECONDS = 0.05`. `TypedDict`s `Entry`, `ModResult` and `Heartbeat` match Phase 3's JSON field for field.
- `new_entry_id() -> str` returns `uuid.uuid4().hex`; `send.py` calls it first, because the delivered text carries a tag from the id. `attempt(state: Path, session: sessions.SessionRecord, sender: str, entry_id: str, text: str, live: list[sessions.SessionRecord]) -> Attempt`, where `Attempt = Delivered | NotDelivered`. `Delivered(seconds: float, verbatim: bool)`. `NotDelivered(reason: PostmasterMiss, seconds: float)`, and `PostmasterMiss` is a `Literal` of: `absent` (no heartbeat file), `stale` (heartbeat older than the limit, unreadable, or its session is not in `live`), `self` (the recipient is the postmaster session), `unclaimed` (withdrawn at the deadline), `outbox-error` (the entry could not be written), `failed`, `expired`, `claimed-no-result`, `relay-only-sender`.
- `RELAY_ONLY_SENDERS = frozenset({"shutdown"})` (decided 2026-10-12 by the showrunner). `commands/message.md` tells every session to follow instructions from sender `shutdown`, and a recipient reads that name from the message wrapper, which names `postmaster` for a mod-sent message. A session that does not accept one never settles, and the user is paged after 5 minutes. So these messages keep the relay during the trial. The set is one line to remove once step 2 has shown that recipients read the real sender from the first line.
- Steps of `attempt`:
  0. `sender` is in `RELAY_ONLY_SENDERS`: return `relay-only-sender` at once, reading and writing nothing.
  1. Read `state / "postmaster" / "heartbeat.json"`. A file that does not parse is read again, up to three reads 20 ms apart, because the mod's overwrite can be seen torn (measured); one that still does not parse counts as stale. Absent, stale or self: return at once, writing nothing. This is the whole cost when the mod is not running: one file read.
  2. Write the entry to `outbox/.<id>.json.tmp`, then `os.replace` it to `outbox/<id>.json`. `id` is `entry_id`; `session_id` is the recipient record's `sessionId` and `socket` is `uds:` plus its `messagingSocketPath`; `deadline_ms` is now plus `WAIT_SECONDS`; `sha256` is the hash of `text`; `reply_to_session_id` is the `sessionId` of the one session in `live` whose name is `sender`, else the empty string.
     An `OSError` while writing the entry returns `outbox-error` after removing what was written. A removal that fails after a `delivered` result is ignored: the answer stays `Delivered`.
  3. Poll `results/<id>.json` every `POLL_SECONDS` until the deadline. `delivered`: remove the result and return `Delivered`, with `verbatim` true when the result's hash equals the entry's. `failed` or `expired`: remove the result and return `NotDelivered` with that reason. `abandoned`: return `claimed-no-result`, since the delivery is unknown. A result file that cannot be read or parsed yet counts as not written: keep polling.
  4. At the deadline, rename `outbox/<id>.json` to `outbox/.<id>.withdrawn` and remove it, then look for `claimed/<id>.json`. The order is the rule: the entry is gone before the check. No claim: the mod will not send this entry; return `unclaimed`.
  5. A claim exists: the mod may be sending. Poll for the result until `CLAIM_GRACE_SECONDS` more have passed and follow it as in step 3; `withdrawn` returns `unclaimed`. No result: return `claimed-no-result`.
  6. Before returning from any step after step 2, remove this id's entry, claim and result files that exist. `postmaster.py status` also removes entries, claims and results older than 10 minutes, and files under `errors/` older than 7 days, since the mod cannot remove a file; it prints the count of files under `errors/` and `replies/`. The mod may write a claim and a `withdrawn` result after `attempt` has returned, so every `attempt` that wrote an entry ends by removing files older than 10 minutes from `outbox/`, `claimed/` and `results/`.
- Why a message is never delivered twice: the mod writes its claim and then checks the entry; `send.py` withdraws the entry and then checks the claim. The mod sends only if the entry was still there after its claim was written, and `send.py` relays only if no claim was there after the entry was gone. Both cannot hold, because each side's check follows the other side's write. When the mod backs off it writes `withdrawn`, and `send.py` relays. A replaced hooks worker never sends an entry whose claim an earlier run wrote (Phase 3).
- The one path where a second copy can exist is `claimed-no-result`: the mod claimed the entry and wrote no result within 5 s, which means the hooks worker died or hung between its send call and its result write. `send.py` relays there (plan author's call: a lost message is the worse outcome, and the relay's own timeout already allows a second copy through the queue today; one-line revert to `enqueue`). The log names the reason, so step 2 counts it. A claim an earlier worker left with no result comes back as `abandoned` and takes the same path under the same log reason.
- In `send.send`, the Claude branch (1025–1039) becomes: resolve `relay_plan(message)` as today. For a `RelayAttempt`, read `live = sessions.live_sessions()` once and `session = sessions.addressed(plan.message.to, live)`. When `session` is not `None`, make `entry_id = postmaster.new_entry_id()` and call `postmaster.attempt(STATE, session, message.sender, entry_id, delivered_text(message, entry_id), live)`. `Delivered` → `Result("sent", f"postmaster; {'verbatim' if verbatim else 'NOT VERBATIM'}; session:{session['sessionId']}")`. `NotDelivered`, or no live session for the address (a bare name only ListAgents knows) → `relay(plan.message, options.timeout)` and everything after it exactly as today, including `enqueue` on `queued`.
- `delivered_text(message, entry_id)` is `f"Message from {message.sender} via postmaster: {message.summary} [{entry_id[:8]}]\n\n{message.text}"`. The mod sends under its own name, so the first line carries the real sender and stands alone as the preview. The tag, the first eight characters of the entry id, makes every delivered text differ from the one before it: a recipient drops a message whose text equals the previous one from the same sender while the mod's send still answers delivered (measured), which would be a lost message reported as `SENT` (decided 2026-10-12 by the showrunner: the first line names the real sender and the entry id). The relay path keeps `-n message.sender` and the unchanged text.
- The log (`LogEntry`) gains three `NotRequired` fields, written only for a message that reached the Claude branch: `"via"`: `"postmaster"` or `"relay"`; `"postmaster"`: `"delivered"`, a `PostmasterMiss`, or `"unresolved"` (no live session for the address); `"seconds"`: wall time from the start of the Claude branch to its result, to 0.001 s. `log()` takes a `Route`: `NoClaudeRoute()` for a message that never reached the Claude branch, or `ClaudeRoute(via, postmaster, seconds)`, which alone adds the three fields. Existing fields, their order and every other log line are unchanged.
- `send.py`'s docstring "Delivery" paragraph says the postmaster is tried first and names the fallback.
- `postmaster.py status`, run as a script, prints one line: `postmaster: <running since …, heartbeat N s old | not running>` and today's counts from `log.jsonl` by `via` and by `postmaster` reason. It exits 0 when the heartbeat is fresh and 1 otherwise.
- Tests. `test_postmaster.py`, with a fake mod that is a function acting on the temp state directory and a fake clock, no sleep: each `PostmasterMiss` reason; an outbox that cannot be written gives `outbox-error` and one relay; a failed removal after `delivered` still returns `Delivered`; a heartbeat that fails to parse once and then parses counts as fresh, and one that never parses is `stale`; the entry's `session_id` and `socket` come from the recipient's record; sender `shutdown` with a fresh heartbeat returns `relay-only-sender` and reads no file; `Delivered` with a matching and a non-matching hash; the withdrawal finding no claim; a claim found then a late `delivered`; a claim found then `withdrawn`; `claimed-no-result`; every order of the mod's t1 (write the claim) and t2 (check the entry) against the sender's s1 (take the entry out) and s2 (check the claim), each ending in exactly one delivery, by the mod or by the relay, and never in none; every return after step 2 leaves no file for its id; an absent heartbeat writes no file anywhere; an `abandoned` result relays as `claimed-no-result`; a half-written result is read again and then followed; a claim and a `withdrawn` result left by a late mod write are removed, once older than 10 minutes, by the next attempt that writes an entry. `test_send.py`: with no heartbeat every existing test passes untouched; a fresh heartbeat plus a `delivered` result gives `SENT: postmaster; …`, no relay call, exit 0, and a log line with `via: postmaster`; each miss reason calls the relay once with the unchanged message and logs `via: relay` with the reason; a `queued` relay after a miss still enqueues under the session id; a recipient that is the postmaster session relays; a message from sender `shutdown` with a fresh heartbeat calls the relay once, writes no outbox entry and logs `via: relay`, `postmaster: relay-only-sender`; a keyed send delivered by the postmaster retires its queued copy; two sends with the same sender, summary and text write entries whose texts differ in the tag.

**Files:**
- `scripts/message/postmaster.py` — the client: types, `attempt`, `status`.
- `scripts/message/send.py` — the Claude branch, `delivered_text`, the three log fields, the docstring.
- `scripts/message/test_postmaster.py` — the client's cases.
- `scripts/message/test_send.py` — the routing and log cases.

**Seats:** 1 writer + 1 tester — the client and its one call site, tested from the Spec's formats and outcome lines.
- `impl` — `scripts/message/postmaster.py`, `scripts/message/send.py`.
- `test` — `scripts/message/test_postmaster.py`, `scripts/message/test_send.py`.

**Constraints from prior phases:** Phase 3 fixed the entry, result and heartbeat JSON and the rule that the mod sends only an entry still in `outbox/` after its claim is written; Phase 4 added the entry's `sender` and `reply_to_session_id` fields and keys its reply map by the entry's `socket`. Measured by Phase 2 on Claude Code 2.1.296 (table in `docs/as-built/session-roster.md` → `### Postmaster mod (trial)`): (1) the mod reaches recipients under all three natedev accounts, which share the one registry `sessions.py` reads (`NOTIFIER_SESSIONS_DIR`, default `~/.claude/sessions`), so there is no account check; (2) the recipient sees the postmaster session's name with `from-plugin="postmaster"`, never the real sender, so `delivered_text` adds its first line and `RELAY_ONLY_SENDERS` stays; (3) a mod send takes 80 to 300 ms, so 3 s covers the 0.5 s poll and the send; (4) a send to a dead session, to the mod's own session, or past the rate limit of about 32 recent sends to one session comes back as a `failed` result, and `send.py` relays it; (5) a recipient drops a message whose text equals the previous one from the same sender, which the tag prevents; (6) the mod's overwrite of a file can be read torn, so an unparsable heartbeat or result is read again.

**Acceptance gate:** the message suite green, `test_postmaster` included; basedpyright clean on the four files; with `STATE/postmaster/` absent, `send.py` gives the same outcome line, exit code, queue file and relay call as before this phase, shown by the untouched existing tests; a test shows a `delivered` result with no relay call; a test shows each miss reason relayed once, `relay-only-sender` among them.

**Verification:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_postmaster test_send test_sessions test_roster` · `basedpyright scripts/message/postmaster.py scripts/message/send.py scripts/message/test_postmaster.py scripts/message/test_send.py` · `cd scripts/whoami && HOME=<scratch home> python3 -m unittest test_quota_alert test_five_hour` (both import `send`).

### Phase 6 — The live start-up, the log check, the receiving rule and the as-built  · status: todo

#### Work Order

**Goal:** the postmaster session runs on natedev and delivers a throwaway message end to end; the log holds what step 2 reads; the documents say what shipped and how to start, check and stop it.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- `commands/message.md`, loaded after `/succinct_style`. "Receiving" gains one rule: a message whose sender is `postmaster` was sent by a script; its first line, `Message from <name> via postmaster: <summary> [<tag>]`, names the real sender; answer that name and never `postmaster`. "Sending" says `send.py` delivers through the postmaster session when one runs and relays otherwise. `codex/AGENTS-messaging.md` is not changed: a Codex recipient is never reached through the postmaster, and the shared "Finding sessions" words do not change.
- `docs/as-built/session-roster.md`, the amend:
  - `### send.py: one recipient`: the delivery paragraph names the postmaster attempt, the 3 s wait, each miss reason and the relay fallback; the outcome table's `SENT` row notes `postmaster; …`.
  - `### Postmaster mod (trial)`: what the mod is and where it lives; the state directory and its three JSON formats; the claim file and why a message is delivered once; the `claimed-no-result` path and the choice made there; reply handling; the measured API table from Phase 2; the start, check and stop procedure below; the log fields.
  - Key files, Invariants (the outbox rule; no settings change; the mod touches only `STATE/postmaster/`; tests never write the real outbox) and gotchas (the `tsc` command; the generated types folder; `--plugin-dir` hot-reloads the folder, so a pull on main reloads the running mod and the relay covers the pause; after a Claude Code update run `claude plugin validate --strict` and `claude plugin test`; a stopped postmaster session is started again by hand, and until then every message relays; `/home/natepiano/postmaster` is outside the repo and trusted per account, so a new account or a removed directory meets the folder-trust dialog).
  - Not verified, and written as such: whether `/shutdown restart` brings the postmaster session back with its `--plugin-dir` flag.
- The live procedure, run by the unit director after both seats are idle and after this branch is on main, since the session loads `~/.claude/mods/postmaster`. It messages no real session.
  1. Check: `postmaster.py status` prints `not running` and exits 1.
  2. Start: in a new tmux session named `postmaster`, in `/home/natepiano/postmaster`, with every `CLAUDE*` variable removed, run a shell whose child is `command claude -n postmaster --plugin-dir /home/natepiano/.claude/mods/postmaster`. Confirm with `sessions.py id postmaster`. Pass: `postmaster.py status` exits 0 within 10 s.
  3. Start one throwaway recipient, `command claude -n pm-check`, in a second window of a throwaway tmux session in the same directory.
  4. Send three messages with the same summary and text, `send.py --here --to pm-check --from pm-trial --text …`. Pass: each prints `SENT: postmaster; verbatim; …` and exits 0; each returns in under 1.5 s; `pm-check`'s transcript shows three arrivals, each first line naming `pm-trial` with its own tag (equal texts would be dropped as repeats); `STATE/relay/` gained no file; `log.jsonl` has three lines with `via: postmaster`.
  5. Fallback: end the postmaster session's `claude` process with `SIGTERM` by its own pid, send one more message. Pass: it prints `SENT: mode auto; verbatim; …` through the relay, the log line says `via: relay`, `postmaster: stale` or `absent`, and `pm-check` shows it once.
  6. Reply: start the postmaster again as in step 2, send `pm-check` a message whose text asks it to answer the session named `postmaster`. Wait at most five minutes. If an answer arrives, the pass is: it is in the `replies/` directory, and the postmaster session's transcript shows no new turn. If none arrives, record that; reply handling then stands on Phase 4's tests and Phase 2's criterion (d). Skip this step when Phase 4 shipped only the pass-through handler.
  7. Leave the postmaster session running: this is the trial's start. End `pm-check` and its tmux session. Record the start time, in the user's zone after reading the clock, in the as-built.
- Log fields step 2 reads, all in `STATE/log.jsonl`: `time`, `outcome`, `detail`, `via`, `postmaster`, `seconds`, `from`, `to`. From them: the share delivered by the mod, relays a day against the baseline of about 600, delivery time against the relay's 4.5 s median, `queued` and `failed` counts, and the count of `claimed-no-result`. the `replies/` directory gives replies consumed and forwarded. The as-built lists the one-line command for each number; nothing in this plan computes them.
- The checkpoint notice to the showrunner states the trial's start time and that step 2's two-day window begins there.

**Files:**
- `commands/message.md` — the receiving rule and the sending line.
- `docs/as-built/session-roster.md` — the amend and the mod's section, with the live results.

**Seats:** 2 writers — one document each; neither has a test lane.
- `impl` — `commands/message.md`.
- `test` — opens as impl: `docs/as-built/session-roster.md`.

**Constraints from prior phases:** Phase 1 wrote `### Postmaster mod (trial)` with the probe procedure; Phase 2 filled its measured table and recorded the `tsc` command; Phase 3 removed the probe and marked the procedure as the record of how the table was measured. Phases 3 and 4 built the mod: outbox, claim, result, heartbeat, reply handling, under `mods/postmaster/hooks/`. Phase 5 built `scripts/message/postmaster.py` (`new_entry_id`, `attempt`, the miss reasons, `status`), the `SENT: postmaster; …` line, `delivered_text` with its tag, the three log fields and `RELAY_ONLY_SENDERS`; the as-built names the set, the reason, and that step 2's share is counted without those messages. Phase 2 measured that the recipient sees the postmaster session's name, so the receiving rule is added. Spell the CLI `command claude` in every command a seat or a script runs.

**Acceptance gate:** the live procedure's seven steps passed and their results are in the as-built; `commands/message.md` names the rule; the as-built's `send.py` section, invariants and gotchas describe what shipped; the message suite green; `claude plugin validate --strict mods/postmaster` and `claude plugin test mods/postmaster` green on main.

**Verification:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_postmaster test_send test_sessions test_roster` · `command claude plugin validate --strict /home/natepiano/.claude/mods/postmaster` · `command claude plugin test /home/natepiano/.claude/mods/postmaster` · `python3 scripts/message/postmaster.py status` exits 0 after the live procedure.

### Phase 7 — Restart finds a session's tmux pane and tmux session after a rename  · status: todo

#### Work Order

**Goal:** a restarted session comes back in its own pane when that pane is still there and free, and otherwise in a new window of the tmux session it ran in, also when that tmux session was renamed after the shutdown record was written; the shutdown as-built states the times measured on natedev.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. This phase is outside the mod and `scripts/message/`: it repairs a defect the natedev shutdown live check found on 2026-10-12 (assigned to this unit by the showrunner). It touches only a `TmuxHost`; a `UnitHost` keeps its own route.

- The defect. `scripts/production/tmux_names.py` renames tmux sessions while they run. `scripts/shutdown/restart.py` then loses the session's place twice: `_pane_is_reusable` (687) rejects the recorded pane when its current `#{session_name}` differs from the recorded `tmux_session` (708), and the fallbacks in `_tmux_launch` (756–805) address the tmux session by its recorded name (`has-session -t =<name>`, `_new_window_argv`, `new-session -s <name>`). In the live check the tmux session was renamed between the record and the restart, the pane had closed with its process, and the session came back in a new tmux session.
- The pane. A pane id (`%N`) does not change when its session is renamed. `_pane_is_reusable` no longer compares the session name: the recorded pane is this session's pane when its id answers and, for a live pane, its `pane_pid` equals the recorded one. A dead pane, or a live one with the recorded pid and no child process, is respawned as today.
- The tmux session. `inventory.py` records the tmux session's id (`#{session_id}`, `$N`), which a rename does not change, beside its name: `_tmux_session` (864) asks one `display -p -t <pane>` for `#{session_name}`, `#{session_id}` and `#{pane_pid}`. `TmuxHost` (61) gains `session_ref: TmuxSessionId | TmuxSessionIdNotRecorded`, in the pattern of `TmuxPane | PaneNotRecorded`: `{"kind": "session id", "session_id": "$N"}` or `{"kind": "not recorded"}`. `_parse_host` (366) reads it; a record written before this phase has no such key and parses as not recorded, so a shutdown record that spans the install still restarts.
- Where a new window goes, first match wins: (1) the recorded pane exists but is in use: the pane's current tmux session, read by the same probe (`#{session_id}`); (2) the recorded session id names a running tmux session (`has-session -t '$N'`); (3) the recorded name names one (`has-session -t =<name>`), as today; (4) none: a new tmux session under the recorded name, as today, with today's recovery when another launch created it first. `_new_window_argv` takes the target it is given (`$N:` or `=<name>:`).
- `restart.py` line 587 passes `host["tmux_session"]` for a `UnitHost`; that call does not change.
- Tests, hermetic as the existing ones are (a fake `tmux` through `SHUTDOWN_TMUX`, no real server): the tmux session was renamed and the pane is free: `respawn-pane` on the recorded pane id; renamed and the pane is in use: a new window in the pane's current session; the pane is gone and the session was renamed: a new window addressed by the recorded session id; the pane and the session id are gone and the name answers: a new window by name; nothing answers: `new-session` under the recorded name; a record without `session_ref` parses and takes routes (3) and (4); the inventory records the id, and records not recorded when `display` gives none. The existing cases `test_tmux_respawns_a_reusable_live_or_dead_pane` (609), `test_tmux_respawns_recorded_pane_when_session_name_has_outer_spaces` (637), `test_tmux_falls_back_when_a_recorded_pane_is_in_use_or_gone` (654) and `test_parallel_tmux_creation_recovers_by_opening_a_window` (559) hold, changed only where they asserted the name comparison.
- `docs/as-built/shutdown-and-restart.md`: the restart section says how the pane and the tmux session are found and in which order. `### Results` gets the natedev result of 2026-10-12, 21:29 to 21:39 EDT: both requests passed on two throwaway sessions; request 1 (stop and return): the idle session came back on its own with the same session id, 13 s from stopped to running and 2 min 36 s from the request, and the session held at a question never stopped; request 2 (stop only): the idle session stayed stopped until `restart --only`, which reported 2.7 s; each `down` call took 52 to 59 s to return, and the idle session needed about another minute to report ready. The pass rule that says the session is back within 30 s is rewritten to count from the stop, with the measured figures beside it. The Mac line stays as it is.
- After this phase's checkpoint is on main the unit director runs the natedev leg of that document's live-check procedure again, on two throwaway sessions in `/home/natepiano/postmaster`, renaming the throwaway tmux session by hand (`tmux rename-session -t =<name> <other>`) between the stop and the restart of request 2, and timing the `down` call against the message log. Pass: the idle session comes back in the renamed tmux session and no new tmux session appears. The result, and one line on where the `down` call's time goes, enter the as-built at the run's close-out. This step is no gate of the phase.

**Files:**
- `scripts/shutdown/inventory.py` — `TmuxSessionId`, `TmuxSessionIdNotRecorded`, `TmuxHost.session_ref`, `_tmux_session`, `_parse_host`.
- `scripts/shutdown/restart.py` — `_pane_is_reusable`, `_new_window_argv`, `_tmux_launch`.
- `scripts/shutdown/test_restart.py` — the route cases.
- `scripts/shutdown/test_inventory.py` — the recorded id and the old-record case.
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py` — their `TmuxHost` fixtures gain `session_ref` (not recorded).
- `docs/as-built/shutdown-and-restart.md` — how restart finds the pane and the tmux session; the natedev result and the measured times.

**Seats:** 1 writer + 1 tester — the Spec fixes the record format and the four routes, so the tests are written from it.
- `impl` — `scripts/shutdown/inventory.py`, `scripts/shutdown/restart.py`, `docs/as-built/shutdown-and-restart.md`; hub: `scripts/shutdown/inventory.py` (the tester builds records from its types).
- `test` — `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py`.

**Constraints from prior phases:** none from this plan's phases: the code is the per-session shutdown rewrite (as-built `docs/as-built/shutdown-and-restart.md`). `SessionRecord.tmux` in `scripts/message/sessions.py` holds `<tmux session name at start>:@<window>.%<pane>`, and only its pane id stays true. `restart.py` reads the `tmux` binary from `SHUTDOWN_TMUX`, and runs its probes through `_probe` and its launches through `_run`, both of which take `dry_run`. Tests never start a real tmux server, never message a session, and never run `shutdown.py`, `restart.py`, `settle.py` or `stop.py` against real state. Python: basedpyright 0 errors and 0 warnings on changed files, no `Any`, no file-level ignores.

**Acceptance gate:** the shutdown suite green with the named cases; basedpyright clean on the six Python files; a test shows a renamed tmux session with a free pane respawned in place, and one shows a closed pane in a renamed tmux session reopened as a window of that same tmux session; a record without `session_ref` still restarts.

**Verification:** `cd scripts/shutdown && HOME=<scratch home> python3 -m unittest test_restart test_inventory test_record test_settle test_stop test_shutdown_cli test_shutdown_status test_remote test_send_guard` · `basedpyright scripts/shutdown/inventory.py scripts/shutdown/restart.py scripts/shutdown/test_restart.py scripts/shutdown/test_inventory.py scripts/shutdown/test_settle.py scripts/shutdown/test_stop.py`.
