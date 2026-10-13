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

### Phase 3 — The mod delivers outbox entries and writes a result for each  · status: done

#### As-built

- State lives under `STATE/postmaster/` (`STATE` is `$XDG_STATE_HOME/message`, else `$HOME/.local/state/message`): `outbox/<id>.json`, `claimed/<id>.json`, `results/<id>.json`, `heartbeat.json`, `errors/<at_ms>-<n>.json` (`{at_ms, error}`). A write creates its directory; `send.py` creates `outbox/`. Entry ids are 32 hex characters, either case; names starting with `.` and non-`.json` files are never read.
- `register.ts` keeps one worker per hooks process (`worker` state `waiting | started`): the first `session.start` builds `PostmasterCalls` from direct `$.noun.method(...)` sites (literal `$.env.get` names, `every` over `globalThis.setInterval`, `now` = `Date.now`) and calls `startOutbox`; a failed start goes to `reportOutboxError` and the worker returns to `waiting`.
- `startOutbox(calls: PostmasterCalls): Promise<Outbox>` resolves the state directory, names its `run` (`<now>-<uuid>`) and starts a poll every `POLL_MS` (500); `Outbox` is `{ poll(): Promise<void>; cancel(): void }`. A poll never overlaps another. Each poll writes the heartbeat (`{session_id, at_ms, version}`), retries unwritten results, marks every claim from another `run` without a result `abandoned` (no send), then takes at most `BATCH` (20) new outbox entries oldest-first by mtime, rewriting the heartbeat before each.
- Per entry: read it, write the claim (`{id, claimed_ms, run}`), then check `outbox/<id>.json` still exists. Gone → `withdrawn`; malformed → `failed` / `unreadable entry`; `deadline_ms` past → `expired`; else one `calls.session.send({ to: { sessionId }, text })` with the text byte for byte. `isDelivered: true` → `delivered`; `isDelivered: false` → `failed` with `reason` in `detail`; a throw before the send → `failed`, from the send → `abandoned`, error text in `detail`.
- A result is `{id, outcome, detail, sha256, claimed_ms, finished_ms}`; `sha256` is the mod's own hash of the text it passed (SHA-256 of empty text for an unreadable entry). An unwritten result stays in memory and is rewritten on later polls without a second send. Every handler and the poll body catch, write one `errors/` file and never rethrow.

**Files:**
- `mods/postmaster/hooks/protocol.ts` — `OutboxEntry`, `OutboxClaim`, `OutboxResult`, `Heartbeat`, `PostmasterCalls`, `Outbox`, their validators, `POLL_MS`, `BATCH`, `VERSION`, `PATHS`.
- `mods/postmaster/hooks/outbox.ts` — `startOutbox`, `reportOutboxError(calls, error)`; never touches `$`.
- `mods/postmaster/hooks/register.ts` — `session.start` wiring and worker state.
- `mods/postmaster/tests/outbox.test.ts`, `tests/protocol.test.ts` — hand-written call-set fake (files in a `Map`, recording `send`, movable `now`, recording `every`), including every order of claim/check against the sender's remove/check.
- `docs/as-built/session-roster.md` — notes the probe left the tree and the live-check procedure stays as the measurement record.

**Binds later work:** the claim-then-check order is the withdrawal rule `send.py` relies on (it removes the entry, then checks for a claim). The mod removes nothing: `send.py` removes the entry, claim and result, and the client removes leftovers. `failed` also means a file step failed before the send, so `send.py` relays at once on it. The `started` branch of `register.ts` is the one place a worker starts, and `startOutbox` is called only there.

**Gotchas:** a recipient drops a text equal to the previous one from the same sender while the send still answers delivered, so each entry's text must differ. A heartbeat overwrite can be read torn by another process; a reader re-reads before treating it as stale. The engine rejects a read or write over 4 MiB, so every record is its own file. After about 32 quick sends to one session the engine answers `isDelivered: false` (then about one per 2 s), which surfaces as `failed`.

**Ruled out:** `$.clock.every` (not measured live); pacing sends in the mod; a sweep in the mod (it has no remove or rename); any file rewritten whole as it grows.

### Phase 4 — Shutdown stops a session held by a usage limit without waiting for it  · status: done

#### As-built

- The screen: `_screen(session) -> ScreenText | ScreenUnreadable` (frozen dataclasses; `ScreenUnreadable.reason` is `no tmux host`, `no pane id` or `capture failed`) reads the owner's pane by pane id. It reads the Claude Code session record (`sessions.read_session(sessions.sessions_dir() / f"{pid}.json")`), accepts it only when its `sessionId` equals the record's `session_id`, takes the `%N` after the last `.` of its `tmux` field, and runs `capture-pane -p -t %N` with a 5 s timeout. Hosts other than `tmux`/`unit` are `no tmux host`. `_showing_form(session)` keeps its signature, reads `_screen`, and treats `ScreenUnreadable` as no form.
- Detection: `_held_by_usage_limit(screen)` is false for `ScreenUnreadable`, and otherwise matches case-insensitively any phrase of the module tuple `USAGE_LIMIT_PATTERNS` (`hit your session limit`, `hit your usage limit`, `hit your weekly limit`, `usage limit reached`, `wait for limit to reset`) in the last 15 non-empty lines only. `_usage_limit_shown(session)` is false for a `busy` session whatever its screen shows.
- The hold: `_conduct_owned`, after `fresh_session` and before `next_step`, computes `_usage_limit_change(saved, owner) -> EnterUsageLimitHold | LeaveUsageLimitHold | NoUsageLimitChange` at every check for a matching session with `force == "wait for ready"`. Enter: progress `waiting`/`ready` and the limit shown; it writes `{"kind": "held by usage limit", "at": record_time()}` and no settle message is sent. Leave: progress `held by usage limit` and the limit not shown (cleared, `busy`, or pane unreadable); it writes `{"kind": "waiting"}` and sets `settle_message` to `not sent` in the same locked write, so the owner is asked again. `_apply_usage_limit_change(saved, change) -> SessionShutdown | UsageLimitChangeRefused` writes through `store.update_session` under the record lock only when the current record has `saved`'s `request_id`, `force == "wait for ready"` and the expected prior progress; otherwise, or on `store.NoLiveRecord`, it writes nothing and the conductor skips that owner until its next check, so a cancel, a replaced request or a concurrent `now` wins. The returned record goes to `next_step`, the holdout check and any `stop_session`, so the held progress is on disk before the stop worker claims it.
- `next_step`: a held record stops on the `ready` rule (session and every seat `idle`, `blocker` `clear`) without `_showing_form`; an absent or identity-lost owner and `force == "now"` stop; an unreadable account holds; otherwise `Hold(hold_reason(...))`. `SendSettleMessage` is for `waiting` only; `_done` is unchanged. A held session is not a holdout: `_reserve_holdout_alert` and its dispatch keep `{"waiting", "ready"}`. `ready()` accepts a held record and `now` forces one. `_hold_details` takes the progress kind (from `hold_reason` and `status_record_lines`) and skips the form read for a held record, so status shows no `showing a form` detail; a busy seat reads `Codex seat <name> is still running`.
- `SessionHeldByUsageLimit` (`{"kind": "held by usage limit", "at"}`) is in `SessionRestartPrior`/`SessionProgress`, `_PROGRESS_WITH_TIME`, `NotReadyToStop.progress` and its parse check, and `_withdraw_session`'s cancel whitelist; old records parse as before. `stop.py` claims a held record and stops it without `force_now`, writing `NotReadyToStop` with progress `held by usage limit` when a seat is busy. `shutdown.py` `_status_actions` and `main`'s status treat it as `waiting` (counted for the conductor line, listed under `take out` and `stop now`). `restart.py` `RestartNotStopped.progress`, `_up` and `parse_peer_result` refuse it locally and from the peer: `<name> is held by usage limit, not stopped; nothing to restart`, with the exit a `waiting` refusal gives.

**Files:**
- `scripts/shutdown/settle.py` — `ScreenText`, `ScreenUnreadable`, `_screen`, `USAGE_LIMIT_PATTERNS`, `_held_by_usage_limit`, `_usage_limit_shown`, the three change types, `UsageLimitChangeRefused`, `_usage_limit_change`, `_apply_usage_limit_change`, and the held kind in `_conduct_owned`, `next_step`, `_hold_details`, `hold_reason`, `status_record_lines`, `now`, `ready`.
- `scripts/shutdown/record.py` — `SessionHeldByUsageLimit` and the progress lists.
- `scripts/shutdown/stop.py` — the claim whitelist and the stoppable prior.
- `scripts/shutdown/shutdown.py` — `_status_actions`, `main`'s status.
- `scripts/shutdown/restart.py` — `RestartNotStopped`, `_up`, `parse_peer_result`.
- `scripts/shutdown/test_settle.py`, `test_stop.py`, `test_record.py`, `test_shutdown_status.py`, `test_shutdown_cli.py`, `test_restart.py` — the held-session cases.
- `docs/as-built/shutdown-and-restart.md` — the `held by usage limit` state and its paragraph under `### Settling and selected actions`.

**Binds later work:** `RestartNotStopped.progress` and the peer refusal parse carry `held by usage limit`, and the `test_settle.py` / `test_stop.py` fixtures build held records; `_screen` reads the owner's pane by the `%N` pane id from the session registry record.

**Gotchas:** basedpyright exits 3 in every checkout because `pyrightconfig.json` names an absent `.venv`; the 0 errors / 0 warnings count is the gate. The limit menu reads as a form, so a held session must stop without the form check. Ghostty, zed and terminal hosts have no screen read and are never held. `test_form_blocked_owner_holds_while_two_others_stop_and_return_in_one_pass` depends on worker completion order (one `wait_for_confirmation` per owner) and failed once in 60+ runs.

**Ruled out:** rechecking the pane when a queued stop is admitted (the session ran no turn in between and the worker rechecks idle status); holdout alerts for held sessions.

### Phase 5 — The mod consumes replies so no turn starts in its session  · status: done

#### As-built

- `OutboxEntry` carries `sender` (the `--from` name, non-empty) and `reply_to_session_id` (the one live session with that name as `send.py` resolved it, `""` when none). `replies.ts` turns it into `ReplyDestination = { kind: "session"; sessionId } | { kind: "unroutable" }` at route registration; neither field enters the delivered text. The mod reads no session registry and stays inside `STATE/postmaster/`.
- `deliver` in `outbox.ts` calls `replies.sending(entry, calls.now())` before `calls.session.send`; it keeps `DeliveryRoute = { key, socket, sender, destination, sessionId, atMs }` in memory and writes `delivered/<key>.json` (`key` = `<at_ms>-<n>`) as `{route: "open", socket, sender, reply_to_session_id, session_id, at_ms}`, returning `RouteToken = { key }`. `isDelivered: false` → `notDelivered(token)` drops that one route and overwrites its file with `route: "withdrawn"`; a failed `withdrawn` write stays pending (`pendingWithdrawals`; outbox `RouteWithdrawal` = `awaiting durable write | durable`) and `flushWithdrawals` retries it each poll before results. A refused send is always `failed` with the engine's reason; a throwing send is `abandoned` and keeps its route; a throwing `sending` gives `failed` and no send. Withdrawn, expired and unreadable entries register no route.
- `startReplies(calls: PostmasterCalls): Promise<Replies>` lists `delivered/` and restores `open` routes whose `at_ms` is inside `REPLY_WINDOW_MS` (30 min), skipping `withdrawn`, unparsable and invalid files; the route counter resumes above the largest listed count. A missing `delivered/` is an empty start; a listing failure on a present one rejects the start.
- `Replies.receive(e): Promise<ReplyDisposition>` reads only `origin.kind === "peer"` events whose text is the wrapper (first line `<cross-session-message from="uds:/…" from-name="…" …>`, body, last line `</cross-session-message>`). A `from` socket with a route inside the window is consumed (`{ kind: "consumed", reason: "postmaster reply" }`, returned by `register.ts` as `{ consumed }`, so no turn starts), logged as `replies/<at_ms>-<n>.json` = `{at_ms, socket, name, session_id, senders, body, forwarded}`, and sent to each distinct `session` destination as `Message from <name> (their answer to <sender> | one of <a>, <b>, passed on by postmaster) [<at_ms>-<n>]:`, a blank line, the body. Everything else, and any handler throw, passes on with `next(e)`.
- `register.ts`: `worker` is `{ status: "waiting" } | { status: "started"; calls; services: Promise<WorkerServices> }` with `WorkerServices = { outbox, replies }`. `session.start` and `session.receive` both call `startWorker(calls)`, which sets `started` before any `await` and builds replies then outbox; a rejected start goes to `reportOutboxError` and resets to `waiting` only if `worker` still holds that value, so the next hook of either kind retries. Both handlers build their call set through one `callsFor($)`.

**Files:** `mods/postmaster/hooks/{protocol,replies,outbox,register}.ts`; `mods/postmaster/tests/{replies,outbox,protocol,register}.test.ts` (hand-written call set for replies/outbox/protocol, kit-loaded plugin for register).

**Binds later work:** the route-file format above and the reply-log format with `senders` a non-empty list in first-route order and `forwarded` the session ids whose send answered delivered. `REPLY_WINDOW_MS` is 30 minutes and a new worker restores `open` routes inside it, so `postmaster.py` cleanup removes `delivered/` files only after 30 minutes. `send.py` writes the entry's `sender` and `reply_to_session_id`. The live trial check reads `replies/` for consumed and forwarded replies.

**Gotchas:** a worker replaced before a pending withdrawal's retry lands restores that `open` route, bounded by the 30-minute window. A refused or throwing forward is left out of `forwarded`, never retried, and writes no `errors/` file. With several senders to one socket in the window each gets the answer (a second copy beats a lost one). The `[<at_ms>-<n>]` tag exists because a recipient drops a text equal to the previous one. Unmeasured: whether a `$` taken in `session.receive` keeps the outbox poll timer alive after a receive-first start. A throwing kit `fs.list` hook is skipped by the kit, so the caller sees an implementation failure instead.

**Ruled out:** removing `delivered/` files at 10 minutes (drops routes the mod still restores); a durable withdrawal-recovery change in the mod; a session-registry lookup in the mod.

### Phase 6 — send.py tries the postmaster first and relays on every other path  · status: done

#### As-built

- `scripts/message/postmaster.py` is the client: `WAIT_SECONDS` 3.0, `CLAIM_GRACE_SECONDS` 2.0, `HEARTBEAT_MAX_AGE_SECONDS` 2.0, `POLL_SECONDS` 0.05, `RELAY_ONLY_SENDERS = frozenset({"shutdown"})`. `OutboxEntry`, `OutboxResult` and `Heartbeat` are `TypedDict`s matching the mod's JSON; `is_outbox_entry` applies the mod's validator rules (32-hex `id`, 64 lowercase-hex `sha256`, non-empty `session_id` and `sender`, `deadline_ms` an `int` from 0 through 9007199254740991, `bool` and `float` rejected). `reply_destination(sender, live) -> ReplyToSession | ReplyUnroutable` is the one live session with that name, else unroutable; `wire_entry(...)` writes `reply_to_session_id` as that session id or `""`.
- `attempt(state, session, sender, entry_id, text, live) -> Delivered(seconds, verbatim) | RelayRequired(reason: PostmasterMiss, seconds)`; `PostmasterMiss` is `absent | stale | self | unclaimed | outbox-error | failed | expired | claimed-no-result | relay-only-sender`. A relay-only sender returns before any read. The heartbeat is read up to three times 20 ms apart while unparsable and is fresh only when at most 2 s old with its `session_id` in `live`; absent, stale or self return having written nothing. Otherwise the entry goes to `outbox/.<id>.json.tmp`, `os.replace`d to `outbox/<id>.json` with `deadline_ms` now + 3 s, and `results/<id>.json` is polled to the deadline: `delivered` → `Delivered` (`verbatim` when the hashes match), `failed`/`expired` → that reason, `withdrawn` → `unclaimed`, `abandoned` → `claimed-no-result`; an unreadable result keeps polling.
- At the deadline the entry is renamed to `outbox/.<id>.withdrawn` and removed, then `claimed/<id>.json` is checked: none → `unclaimed`; a claim → 2 s more of polling, else `claimed-no-result`. The mod writes its claim then checks the entry, and `attempt` removes the entry then checks the claim, so both cannot send. Every return after the write removes the entry first and, only if that succeeds, the temp, withdrawn, claim and result files; it then prunes `outbox/`, `claimed/` and `results/` past 10 minutes (a claim or result whose entry still exists stays) and `delivered/` past 30 minutes.
- `send.send`'s Claude branch reads `live_sessions()` once and resolves the address to `RegisteredClaudeRecipient(session) | RecipientNotInRegistry()`. A registered recipient gets `attempt(STATE, session, message.sender, entry_id, delivered_text(message, entry_id), live)` with `entry_id = new_entry_id()`; `Delivered` → `SENT: postmaster; verbatim|NOT VERBATIM; session:<id>`. Every `RelayRequired` and `RecipientNotInRegistry` goes through `relay_and_queue` as before (enqueue on `queued`, a fresh `live_sessions()` read for the queued explanation), with timeout `max(1.0, options.timeout - attempt.seconds)` after a miss and `options.timeout` otherwise: one budget for both paths.
- `delivered_text` is `Message from <sender> via postmaster: <summary> [<entry_id[:8]>]`, a blank line, the text; the relay keeps `-n <sender>` and the unchanged text. `log()` takes `Route = NoClaudeRoute | ClaudeRoute(via, postmaster, seconds)`; only `ClaudeRoute` adds `via` (`postmaster | relay`), `postmaster` (`delivered`, a miss reason, or `unresolved` for an address with no live session) and `seconds` (monotonic, 3 decimals) to `log.jsonl`.
- `postmaster.py status` prunes as above plus `errors/` past 7 days, prints `postmaster: <running, heartbeat N.N s old | not running>; errors: <n>; replies: <n>` and a `today: via …; postmaster …` count line from `log.jsonl` in the user zone, and exits 0 when the heartbeat is fresh, 1 otherwise.

**Files:**
- `scripts/message/postmaster.py` — types, validators, `attempt`, `status`.
- `scripts/message/send.py` — the Claude branch, `delivered_text`, `Route`, the three log fields, the Delivery docstring paragraph.
- `scripts/message/test_postmaster.py`, `test_send.py` — a fake mod over a temp state directory and a fake clock: every claim/check order, each miss reason relayed once, validator rules, retention.
- `pyrightconfig.json` — `scripts/lib` on the `scripts/message` path (`status` imports `user_zone`).

**Binds later work:** the live check reads `via`, `postmaster` and `seconds` in `log.jsonl` and the `postmaster.py status` line. The relay after a miss gets `--timeout` less the attempt's seconds, at least 1 s. `RELAY_ONLY_SENDERS` is one line to remove once recipients are shown to read the real sender from the first line.

**Gotchas:** `quota_alert.py` gives a send 45 s in all; the shared budget keeps 5 s of attempt, the relay and 10 s of kill grace inside it. `claimed-no-result` is the one path where a second copy can arrive. A withdrawal that fails on a filesystem error returns `claimed-no-result`; the deadline has passed, so the mod answers `expired` instead of sending. A failed entry removal leaves the claim and result in place, since they stop a second send by the mod. The `[<id8>]` tag exists because a recipient drops a text equal to the previous one from the same sender.

**Ruled out:** raising `quota_alert.py`'s timeout (the budget belongs in `send.py`); `enqueue` instead of a relay on `claimed-no-result` (a lost message is worse than a second copy).

### Phase 7 — The live start-up, the log check, the receiving rule and the as-built  · status: todo

#### Work Order

**Goal:** the postmaster session runs on natedev and delivers a throwaway message end to end; the log holds what step 2 reads; the documents say what shipped and how to start, check and stop it.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`.

- `commands/message.md`, loaded after `/succinct_style`. "Receiving" gains one rule: a message from `postmaster` names its real sender in its first line: `Message from <name> via postmaster: <summary> [<tag>]` for an outbox delivery, or `Message from <name> (their answer to …, passed on by postmaster) [<at_ms>-<n>]:` for a forwarded reply; answer that name, never `postmaster`. "Sending" says `send.py` delivers through the postmaster session when one runs and relays otherwise. `codex/AGENTS-messaging.md` is not changed: a Codex recipient is never reached through the postmaster, and the shared "Finding sessions" words do not change.
- `docs/as-built/session-roster.md`, the amend:
  - `### send.py: one recipient`: the delivery paragraph names the postmaster attempt, the 3 s wait, each miss reason and the relay fallback; the outcome table's `SENT` row notes `postmaster; …`.
  - `### Postmaster mod (trial)`: what the mod is and where it lives; the state directory and its JSON formats (entry, claim, result, heartbeat, route file, reply log, error); the claim file and why a message is delivered once; the `claimed-no-result` path and the choice made there; reply handling; the measured API table from Phase 2; the start, check and stop procedure below; the log fields.
  - Key files, Invariants (the outbox rule; no settings change; the mod touches only `STATE/postmaster/`; tests never write the real outbox) and gotchas (the `tsc` command; the generated types folder; `--plugin-dir` hot-reloads the folder, so a pull on main reloads the running mod and the relay covers the pause; after a Claude Code update run `claude plugin validate --strict` and `claude plugin test`; a stopped postmaster session is started again by hand, and until then every message relays; `/home/natepiano/postmaster` is outside the repo and trusted per account, so a new account or a removed directory meets the folder-trust dialog; a refused send whose withdrawal write also fails leaves an `open` route on disk, and a worker replaced before the retry lands (a pull on main reloads the mod) restores it, so a message from that recipient inside 30 minutes is forwarded to that sender: accepted for the trial, since it needs a failed write and a reload within one 0.5 s poll, and `REPLY_WINDOW_MS` bounds it; `forwarded` holds only confirmed destination ids, so an empty list means an unroutable sender, a refused forward or a throwing forward, none of which is retried or written to `errors/`: read the logged body, find the intended sender and resend by hand).
  - Not verified, and written as such: whether `/shutdown restart` brings the postmaster session back with its `--plugin-dir` flag.
- The live procedure, run by the unit director after both seats are idle and after this branch is on main, since the session loads `~/.claude/mods/postmaster`. It messages no real session.
  1. Check: `postmaster.py status` prints `not running` and exits 1.
  2. Start: in a new tmux session named `postmaster`, in `/home/natepiano/postmaster`, with every `CLAUDE*` variable removed, run a shell whose child is `command claude -n postmaster --plugin-dir /home/natepiano/.claude/mods/postmaster`. Confirm with `sessions.py id postmaster`. Pass: `postmaster.py status` exits 0 within 10 s.
  3. Start one throwaway recipient, `command claude -n pm-check`, in a second window of a throwaway tmux session in the same directory.
  4. Send three messages with the same summary and text, `send.py --here --to pm-check --from pm-trial --text …`. Pass: each prints `SENT: postmaster; verbatim; …` and exits 0; each returns in under 1.5 s; `pm-check`'s transcript shows three arrivals, each first line naming `pm-trial` with its own tag (equal texts would be dropped as repeats); `STATE/relay/` gained no file; `log.jsonl` has three lines with `via: postmaster`; `postmaster.py status` prints `errors: 0`.
  5. Fallback: end the postmaster session's `claude` process with `SIGTERM` by its own pid, send one more message. Pass: it prints `SENT: mode auto; verbatim; …` through the relay, the log line says `via: relay`, `postmaster: stale` or `absent`, and `pm-check` shows it once.
  6. Reply: start the postmaster again as in step 2, and a second throwaway session, `command claude -n pm-origin`, in a third window. Send `pm-check` a message `--from pm-origin` whose text asks it to answer the session named `postmaster`, and one `--from pm-trial` (no live session) asking the same. Wait at most five minutes for each. For an answer to `pm-origin`'s message the pass is: its `replies/` file lists `pm-origin` in `senders` and `pm-origin`'s session id in `forwarded`, `pm-origin`'s transcript shows the answer once, and the postmaster session's transcript shows no new turn. For an answer to `pm-trial`'s message: its `replies/` file has `forwarded: []` and the postmaster session shows no new turn. If no answer arrives, record that; reply handling then stands on Phase 5's tests and Phase 2's criterion (d). Either way `postmaster.py status` still prints `errors: 0`; a non-zero count fails the step, and each `errors/` file's text goes into the as-built.
  7. Reload, receive first: with the postmaster idle, `touch` (mtime only) `/home/natepiano/.claude/mods/postmaster/hooks/register.ts` so `--plugin-dir` reloads the hooks, and read `postmaster.py status` 5 s later. Fresh: the reload ran `session.start`; record that receive-first is not reachable live. Stale: the new worker waits for its first hook; send `pm-check` (through the relay) a message asking it to answer `postmaster`, so its answer starts the worker through `session.receive`. Pass either way: within 10 s of the first hook `status` exits 0; 30 s later the heartbeat is still under 2 s old, so the poll timer outlives the handler that started it; one more `send.py --to pm-check` prints `SENT: postmaster; …`; `errors: 0`. Record which path ran, apart from ordinary start-up.
  8. Leave the postmaster session running: this is the trial's start. End `pm-check`, `pm-origin` and their tmux session. Record the start time, in the user's zone after reading the clock, in the as-built.
- Log fields step 2 reads, all in `STATE/log.jsonl`: `time`, `outcome`, `detail`, `via`, `postmaster`, `seconds`, `from`, `to`. From them: the share delivered by the mod, relays a day against the baseline of about 600, delivery time against the relay's 4.5 s median, `queued` and `failed` counts, and the count of `claimed-no-result`. The `replies/` directory gives replies consumed, and its `forwarded` lists give replies passed on. The as-built lists the one-line command for each number; nothing in this plan computes them.
- The checkpoint notice to the showrunner states the trial's start time and that step 2's two-day window begins there.

**Files:**
- `commands/message.md` — the receiving rule and the sending line.
- `docs/as-built/session-roster.md` — the amend and the mod's section, with the live results.

**Seats:** 2 writers — one document each; neither has a test lane.
- `impl` — `commands/message.md`.
- `test` — opens as impl: `docs/as-built/session-roster.md`.

**Constraints from prior phases:** Phase 1 wrote `### Postmaster mod (trial)` with the probe procedure; Phase 2 filled its measured table and recorded the `tsc` command; Phase 3 removed the probe and marked the procedure as the record of how the table was measured. Phases 3 and 5 built the mod: outbox, claim, result, heartbeat, reply handling, under `mods/postmaster/hooks/`. Phase 5 shipped the reply log as `replies/<at_ms>-<n>.json` = `{at_ms, socket, name, session_id, senders, body, forwarded}`: `senders` is a non-empty list of distinct, non-empty names in first-route order (not a `sender` string), and `forwarded` lists only the session ids a forward was confirmed to. Route files are `delivered/<at_ms>-<n>.json` = `{route: "open" | "withdrawn", socket, sender, reply_to_session_id, session_id, at_ms}`; the mod restores `open` routes younger than `REPLY_WINDOW_MS` (30 minutes) when a worker starts. A forwarded reply's first line is `Message from <name> (their answer to <sender | one of a, b>, passed on by postmaster) [<at_ms>-<n>]:`. Phase 6 built `scripts/message/postmaster.py` (`new_entry_id`, `attempt`, the miss reasons, `status` with its `errors:` and `replies:` counts), the `SENT: postmaster; …` line, `delivered_text` with its tag, the three log fields and `RELAY_ONLY_SENDERS`, and gives the relay after a miss `--timeout` less the attempt's `seconds` (at least 1 s), so a send keeps its caller's time limit; the as-built names the set, the reason, and that step 2's share is counted without those messages. Phase 2 measured that the recipient sees the postmaster session's name, so the receiving rule is added. Spell the CLI `command claude` in every command a seat or a script runs.

**Acceptance gate:** the live procedure's eight steps passed and their results are in the as-built; `commands/message.md` names the rule; the as-built's `send.py` section, invariants and gotchas describe what shipped; the message suite green; `claude plugin validate --strict mods/postmaster` and `claude plugin test mods/postmaster` green on main.

**Verification:** `cd scripts/message && HOME=<scratch home> python3 -m unittest test_postmaster test_send test_sessions test_roster` · `command claude plugin validate --strict /home/natepiano/.claude/mods/postmaster` · `command claude plugin test /home/natepiano/.claude/mods/postmaster` · `python3 scripts/message/postmaster.py status` exits 0 after the live procedure.

### Phase 8 — Restart finds a session's tmux pane and tmux session after a rename  · status: todo

#### Work Order

**Goal:** a restarted session comes back in its own pane when that pane is still there and free, and otherwise in a new window of the tmux session it ran in, also when that tmux session was renamed after the shutdown record was written; the shutdown as-built states the times measured on natedev.

**Spec:**

Worktree `/home/natepiano/worktrees/claude-build-followups-shutdown`, branch `build-followups-shutdown`. This phase is outside the mod and `scripts/message/`: it repairs a defect the natedev shutdown live check found on 2026-10-12 (assigned to this unit by the showrunner). It touches only a `TmuxHost`; a `UnitHost` keeps its own route.

- The defect. `scripts/production/tmux_names.py` renames tmux sessions while they run. `scripts/shutdown/restart.py` then loses the session's place twice: `_pane_is_reusable` (687) rejects the recorded pane when its current `#{session_name}` differs from the recorded `tmux_session` (708), and the fallbacks in `_tmux_launch` (756–805) address the tmux session by its recorded name (`has-session -t =<name>`, `_new_window_argv`, `new-session -s <name>`). In the live check the tmux session was renamed between the record and the restart, the pane had closed with its process, and the session came back in a new tmux session.
- The pane. A pane id (`%N`) does not change when its session is renamed. `_pane_is_reusable` no longer compares the session name: the recorded pane is this session's pane when its id answers and, for a live pane, its `pane_pid` equals the recorded one. A dead pane, or a live one with the recorded pid and no child process, is respawned as today.
- The tmux session. `inventory.py` records the tmux session's id (`#{session_id}`, `$N`), which a rename does not change, beside its name: `_tmux_session` (864) asks one `display -p -t <pane>` for `#{session_name}`, `#{session_id}` and `#{pane_pid}`. `TmuxHost` (60) gains `session_ref: TmuxSessionId | TmuxSessionIdNotRecorded`, in the pattern of `TmuxPane | PaneNotRecorded`: `{"kind": "session id", "session_id": "$N"}` or `{"kind": "not recorded"}`. `_parse_host` (356) reads it; a record written before this phase has no such key and parses as not recorded, so a shutdown record that spans the install still restarts.
- Where a new window goes, first match wins: (1) the recorded pane exists but is in use: the pane's current tmux session, read by the same probe (`#{session_id}`); (2) the recorded session id names a running tmux session (`has-session -t '$N'`); (3) the recorded name names one (`has-session -t =<name>`), as today; (4) none: a new tmux session under the recorded name, as today, with today's recovery when another launch created it first. `_new_window_argv` takes the target it is given (`$N:` or `=<name>:`).
- `restart.py` line 585 passes `host["tmux_session"]` for a `UnitHost`; that call does not change.
- A timing fault in an existing test. `test_settle.py` `test_form_blocked_owner_holds_while_two_others_stop_and_return_in_one_pass` asserts one `wait_for_confirmation` call for both owners, but the conductor batches the launches it has collected when it submits a confirmation, so when the two stop workers finish in different loop turns it confirms them in two calls (seen once in this unit's run and once in the fixer's, 2026-10-12). The test asserts what the behavior guarantees instead: across all `wait_for_confirmation` calls, `first` and `second` are each confirmed exactly once and `blocked` never. The conductor does not change.
- Tests, hermetic as the existing ones are (a fake `tmux` through `SHUTDOWN_TMUX`, no real server): the tmux session was renamed and the pane is free: `respawn-pane` on the recorded pane id; renamed and the pane is in use: a new window in the pane's current session; the pane is gone and the session was renamed: a new window addressed by the recorded session id; the pane and the session id are gone and the name answers: a new window by name; nothing answers: `new-session` under the recorded name; a record without `session_ref` parses and takes routes (3) and (4); the inventory records the id, and records not recorded when `display` gives none. The existing cases `test_tmux_respawns_a_reusable_live_or_dead_pane` (609), `test_tmux_respawns_recorded_pane_when_session_name_has_outer_spaces` (637), `test_tmux_falls_back_when_a_recorded_pane_is_in_use_or_gone` (654) and `test_parallel_tmux_creation_recovers_by_opening_a_window` (559) hold, changed only where they asserted the name comparison.
- `docs/as-built/shutdown-and-restart.md`: the restart section says how the pane and the tmux session are found and in which order. `### Results` gets the natedev result of 2026-10-12, 21:29 to 21:39 EDT: both requests passed on two throwaway sessions; request 1 (stop and return): the idle session came back on its own with the same session id, 13 s from stopped to running and 2 min 36 s from the request, and the session held at a question never stopped; request 2 (stop only): the idle session stayed stopped until `restart --only`, which reported 2.7 s; each `down` call took 52 to 59 s to return, and the idle session needed about another minute to report ready. The pass rule that says the session is back within 30 s is rewritten to count from the stop, with the measured figures beside it. The Mac line stays as it is.
- After this phase's checkpoint is on main the unit director runs the natedev leg of that document's live-check procedure again, on two throwaway sessions in `/home/natepiano/postmaster`, renaming the throwaway tmux session by hand (`tmux rename-session -t =<name> <other>`) between the stop and the restart of request 2, and timing the `down` call against the message log. Pass: the idle session comes back in the renamed tmux session and no new tmux session appears. The result, and one line on where the `down` call's time goes, enter the as-built at the run's close-out. This step is no gate of the phase.

**Files:**
- `scripts/shutdown/inventory.py` — `TmuxSessionId`, `TmuxSessionIdNotRecorded`, `TmuxHost.session_ref`, `_tmux_session`, `_parse_host`.
- `scripts/shutdown/restart.py` — `_pane_is_reusable`, `_new_window_argv`, `_tmux_launch`.
- `scripts/shutdown/test_restart.py` — the route cases.
- `scripts/shutdown/test_inventory.py` — the recorded id and the old-record case.
- `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py` — their `TmuxHost` fixtures gain `session_ref` (not recorded); `test_settle.py` also gets the confirmation-count fix to the order-dependent test.
- `docs/as-built/shutdown-and-restart.md` — how restart finds the pane and the tmux session; the natedev result and the measured times.

**Seats:** 1 writer + 1 tester — the Spec fixes the record format and the four routes, so the tests are written from it.
- `impl` — `scripts/shutdown/inventory.py`, `scripts/shutdown/restart.py`, `docs/as-built/shutdown-and-restart.md`; hub: `scripts/shutdown/inventory.py` (the tester builds records from its types).
- `test` — `scripts/shutdown/test_restart.py`, `scripts/shutdown/test_inventory.py`, `scripts/shutdown/test_settle.py`, `scripts/shutdown/test_stop.py`.

**Constraints from prior phases:** the code is the per-session shutdown rewrite (as-built `docs/as-built/shutdown-and-restart.md`). Phase 4 added the progress kind `held by usage limit` to `restart.py` (`RestartNotStopped`, `_up`'s refusal, `parse_peer_result`) and to `test_restart.py`, `test_settle.py` and `test_stop.py`, and made settle.py read the owner's pane by its pane id (`_screen` reads the session registry record by pid, accepts it only when its `sessionId` matches, and captures the `%N` after the last `.` of its `tmux` field); the line numbers in this Work Order were read before Phase 4 and may have moved. `SessionRecord.tmux` in `scripts/message/sessions.py` holds `<tmux session name at start>:@<window>.%<pane>`, and only its pane id stays true. `restart.py` reads the `tmux` binary from `SHUTDOWN_TMUX`, and runs its probes through `_probe` and its launches through `_run`, both of which take `dry_run`. Tests never start a real tmux server, never message a session, and never run `shutdown.py`, `restart.py`, `settle.py` or `stop.py` against real state. Python: basedpyright 0 errors and 0 warnings on changed files, no `Any`, no file-level ignores. `basedpyright` exits 3 in every checkout because `pyrightconfig.json` names an absent `.venv`; the 0 errors / 0 warnings count is the gate.

**Acceptance gate:** the shutdown suite green with the named cases; basedpyright clean on the six Python files; a test shows a renamed tmux session with a free pane respawned in place, and one shows a closed pane in a renamed tmux session reopened as a window of that same tmux session; a record without `session_ref` still restarts.

**Verification:** `cd scripts/shutdown && HOME=<scratch home> python3 -m unittest test_restart test_inventory test_record test_settle test_stop test_shutdown_cli test_shutdown_status test_remote test_send_guard` · `basedpyright scripts/shutdown/inventory.py scripts/shutdown/restart.py scripts/shutdown/test_restart.py scripts/shutdown/test_inventory.py scripts/shutdown/test_settle.py scripts/shutdown/test_stop.py`.
