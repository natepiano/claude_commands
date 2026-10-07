# Automatic updates pause while the user talks; rename a unit

> **Status: IMPLEMENTATION PLAN — phased, delegate-ready.** A session's scheduled reports and its footer pause when the user writes to it, and return when the user says yes or five minutes after the session asks; a showrunner command renames a unit.

> **As-built disposition: create** — `docs/as-built/conversation-pause.md`; the rename command is folded into `docs/as-built/showrunner-automation.md`

> **Production: build-followups** — unit `enh-showrunner-unit`; production doc `docs/plans/build-followups-production.md`

## Source

The user, via natedev, 2026-10-07 12:2x PDT (relayed in the showrunner's words):

- "another enhancement - i don't want to manage dailies, footers, bulid reports, unit director reports - when i talk to a phase i want a hook that detrmines how to pause that specific phases automated reporting so that we can converse on what I am talking aobut right then. and then when i stop - it can ask me if it can return to automatic updates mode and i can say yes or no. It will time out that after 5 minutes without a yes or no from me, it can return to automatic updates anyway."

The user, typed to the unit director, 2026-10-07 about 13:10 PDT, on whose message pauses:

- "if a unit director has words typed into it, or if theyr'e receiving communication - they can go ahead and start a pause or keep one going - that's useful for me to because i will always catch inter-session communication - so no need to discern - all typing means stop - and doesn't start till the user returns and i will see this - over time i will dtermine if it's important to tighten the net here"

The user, via natedev, 2026-10-07 12:5x PDT, on priority:

- "how quickly can we get the stop comunication when the user types work done? can we pause the mac test work and insert the comunication pause to get it done first? that's more important right now"

The user, via natedev, 2026-10-07 12:4x PDT:

- "make a /showrunner:rename_unit old new - add it to showrunner-fixer session after its current work"

## Decisions (unit director)

Each is the unit director's own call unless it names the user or the showrunner.

- **"A phase" is the session the user types into** (the showrunner's reading): a showrunner or a unit director. Only that session's reports pause.
- **What pauses:** every scheduled message aimed at the session (a notifier instance whose `conf` holds `TARGET=session:<id>` and no `RUN=`) and the footer of a production that targets it. Today that is the unit status reports (`delegate-*`), the dailies (`showrunner-*`) and the build report (`report-builds`). One rule, so a scheduled report added later pauses too.
- **What starts a pause or keeps one going** (the user, 2026-10-07: "no need to discern - all typing means stop"): anything typed into the session, whoever typed it, a slash command and a line the showrunner types included; and, in a session that is not a showrunner's, a message from another session.
- **What never does,** because a report would otherwise pause itself: a scheduled message, a message from an automated job (the stall watch, quota alerts, this feature's own question), and a background command's notice.
- **A showrunner's session pauses on typing only.** Unit notices reach it every few minutes, so its dailies would never run if they counted.
- **The five-minute return stays** as the user first asked. "doesn't start till the user returns" is read as: the question waits on screen for the user; told to the user 2026-10-07, theirs to correct.
- **"Stopped" is five quiet minutes** after the first reply that ends after the user's last message. The user gave five minutes for the unanswered question; the quiet time takes the same number.
- **A reply the user interrupts never ends,** so nothing marks it answered (measured: an interrupt fires no `Stop`). Thirty minutes after the user's message with no reply ended, the question is asked anyway, so a pause always ends.
- **A late `yes` or `no` counts for five minutes** after the updates return on their own. After that it is typing like any other; reports have resumed by then and a bare answer most likely belongs to them.
- **`no` keeps the updates off** until the user asks for them, or until their next conversation in that session goes quiet and the question comes round again.
- **Turning a report back on must not move its schedule.** `notifier.sh start` schedules a whole interval from now, which would push a four-hour build report four hours past every conversation. The notifier gains a `resume` verb; a defect is fixed where it lives.
- **The whole working pause is one phase** (the user made it the priority, 2026-10-07): one round of writing and review. An independent review of that phase then found seven defects in it, so a repair phase follows it and registration follows the repair. The unit rename (`/showrunner:rename_unit`) runs before the registration, which waits on the user (unit director, 2026-10-07 15:25 PDT; it was the last phase until then); its Work Order is added while the pause is being built.
- **Registration comes after the yes/no protection (Phase 5), and the user approved it.** Both hooks run only once `settings.json` lists them. The showrunner relayed the request, so the unit director asked the user directly; the user typed "yes i want the auto pause" in the unit director's session on 2026-10-07 about 15:45 PDT. The live `~/.claude` checkout still holds an uncommitted change to `settings.json` that is not this unit's; the showrunner settles it before it promotes the file.
- **The pause waits while a review is open.** `/adhoc_review` typed into a showrunner's session reaches the pause first, so the pause holds the dailies and footer and the review finds them already off. While that review is open the pause neither asks its question nor returns the updates; its clock runs on from the last reply once the review ends. Each of the two features still restores only what it turned off itself.
- **A typed message waits at most five seconds on the pause.** Past that the hook gives up for that one message and the next message finishes the pause. A message arriving late costs the user more than one report slipping through.
- **A question that cannot reach the session still ends the pause.** The user's five minutes to answer start when the question arrives; when it never arrives, the updates return five minutes after it fell due, the same wait as an unanswered question.
- **A rename changes the session's name only.** The Unit cell, plan, branch and worktree keep theirs, as with the four sessions the user renamed on 2026-10-07. `/showrunner:rename_unit` also finishes a rename the user already typed in the unit's session.
- **Codex sessions are out of scope here:** a Codex unit has no scheduled status report to pause.
- **Other files' owners.** `scripts/message/notifier.sh`, `scripts/message/test_notifier.py`, `docs/as-built/session-notifier.md` and `settings.json` belong to other units' rows or to none. Each checkpoint notice names them as `also touches <path> (owner …), tested against <owner tip>`.

## Measured (2026-10-07, in a throwaway session with probe hooks)

- `UserPromptSubmit` receives `session_id`, `prompt`, `prompt_id`, `session_title`, `transcript_path`, `cwd`, `permission_mode`, `scratchpad_dir`, `hook_event_name`. No field says who sent the prompt. The hook's environment holds `CLAUDE_CODE_SESSION_ID`, equal to `session_id`.
- A typed message, and one typed by `tmux send-keys`: `prompt` is the raw text.
- A cross-session message (a peer, a notifier tick, `send.py`): the hook fires and `prompt` is `<cross-session-message from="uds:…" from-name="…" from-mode="…">`, a newline, the text, a newline, `</cross-session-message>`.
- A typed slash command: the hook fires with `prompt` equal to the raw text, `/name args`.
- A message typed while the session is still answering: the hook fires at once, when it is typed, and not again when the queued message is taken up. Two `Stop` events follow: the running reply's, then the queued message's.
- `Stop` receives `session_id`, `stop_hook_active` (false on a first attempt), `last_assistant_message`, `prompt_id`.
- When `UserPromptSubmit` runs, the transcript does not yet hold the prompt's entry.
- A background command's finish notice: the hook fires and `prompt` begins `<task-notification>`. A background helper's report did not fire it.

## Delegation Context

- **Project:** `~/.claude` — Claude Code commands, skills, hooks and scripts. Work in the worktree `/home/natepiano/worktrees/claude-build-followups-enh-showrunner` on branch `build-followups-enh-showrunner` (unit `enh-showrunner-unit` of production `build-followups`).
- **Project started:** 2026-10-07T20:02:08.366+00:00
- **Stack:** Python 3.13, standard library only; zsh for `notifier.sh`.
- **Layout:**
  - `scripts/hooks/` — hook entry files (`<event>-<what>.py`), their libraries and `test_*.py`
  - `scripts/message/` — the notifier, `send.py`, `sessions.py` and their `test_*.py`
  - `scripts/production/` — production scripts and their `test_*.py`
  - `commands/showrunner/`, `commands/unit/` — the commands
  - `docs/as-built/` — as-built docs
- **Key files:**
  - `scripts/message/notifier.sh` — the scheduler. State root `NOTIFIER_STATE_DIR` or `~/.local/state/notifier`; one directory per instance holding `conf` (`TARGET=`, `EVERY=`, `COMMAND=` or `PROMPT_FILE=` or `RUN=`, `FROM=`, `CHECK=`, `HOLD=`, `ALIGN=`, `TIMEOUT=`) and `state` (`ENABLED=`, `NEXT_DUE=`, `LAST_SENT=`, `LAST_RESTART=`, `LAST_TARGET=`). `cmd_state` (lines 168-196) holds `start` (`ENABLED=1; schedule`), `stop` (`ENABLED=0`) and `restart`; the verb table is lines 459-509; `usage` is line 18. A run-only instance (`--run`) takes no `--to`, `--from` or `--check` (lines 138-140) and its run inherits only `HOME`, `PATH` and the `NOTIFIER_*` variables (lines 412-424).
  - `scripts/message/test_notifier.py` — `NotifierTests`; `run_cli`, `successful` and `new` (lines 76-100) run the script with `NOTIFIER_STATE_DIR`, `NOTIFIER_SESSIONS_DIR`, `NOTIFIER_SEND` and `NOTIFIER_NOW_EPOCH`; `test_start_stop_restart_and_interval_schedules` (line 134) is the model for a verb test.
  - `docs/as-built/session-notifier.md` — the verb table (lines 80-91) and the two lists of verbs that print `next_due` (lines 94 and 165).
  - `scripts/hooks/showrunner_footer.py` — `state_root()` (`SHOWRUNNER_STATE_DIR` or `~/.local/state/showrunner`), `footer_state(slug) -> FooterState`, `set_footer_state(slug, state)`, `FooterState.ON | OFF`, `key_values(path) -> dict[str, str]`, `targeted_instances(session_id)` (showrunner instances only). It imports `zoneinfo` and `subprocess`, so a hook imports it only after its cheap checks.
  - `scripts/hooks/stop-showrunner-footer.py` — the model for a hook entry file: typed payload, silent return for a subagent, the library imported late, any error printed as one stderr line, exit 0 always.
  - `scripts/hooks/test_stop_showrunner_footer.py` — the model for hook tests: the hook runs as a subprocess with the payload on stdin and `HOME`, `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR` pointed at a temporary directory; `SETTINGS` (line 24) reads `settings.json`.
  - `scripts/production/review_pause.py` and `test_review_pause.py` — the model for "record what was changed, restore only that" and for a stub notifier that rewrites an instance's `ENABLED=` line.
  - `scripts/message/sessions.py` — `sessions.py socket session:<id>` prints a live session's socket and exits 0, exits 1 when no live session has the name, and exits 3 when it cannot answer (the registry cannot be listed, or a record cannot be read).
  - `scripts/message/send.py` — `send.py --to uds:<socket> --from <name> --key <key> --text <text>`; `scripts/production/stall_watch.py` lines 202-228 (`socket_for_target`, `delivery`) are the model for a script that sends.
  - `scripts/production/update_registration.py` lines 180-183 — how a run-only instance is created: `new <name> --every 1 --run "<home>/.claude/scripts/lib/py <home>/.claude/scripts/production/<script>"`.
  - `settings.json` — hook registrations (lines 102-220); the `Stop` list is lines 203-219. A Python hook's command is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/<name>.py"`.
- **Port:** none.
- **Test lanes:** `scripts/hooks/`, `scripts/message/` and `scripts/production/` (tests sit beside the scripts as `test_*.py`).
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'`, from the worktree root; the same form with `-s scripts/message` or `-s scripts/production`. While iterating, one file: `-p '<test_file>.py'`.
- **Lint:** `basedpyright <directory>` on each whole directory a change can reach (`scripts/production`, `scripts/message`, `scripts/hooks`, `scripts/build_hold`, `scripts/mac_test`), never on the changed files alone: an importer elsewhere breaks unseen. It passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/` or `~/.local/state/`, and never push anywhere (source: `docs/as-built/showrunner-automation.md`). Every state root and every outside command has an environment override, and tests set all of them.
  - Other units run scripts from the live `~/.claude` checkout while this plan edits the worktree's copies. An existing notifier verb keeps its behavior and its output (source: this plan's author; other units parse `next_due`).
  - A hook never fails a prompt or a reply: any error is one stderr line and exit 0. A prompt hook returns before reading any file when the prompt is a notice, because it runs on every prompt of every session (source: this plan's author; the cost is named in Phase 1).
  - Python is typed throughout, with no `Any` and no file-level type ignores. A state is a named type, never a bare optional.
  - Words shown to the user say "automatic updates", the user's own phrase.

## Phases

### Phase 1 — A session's automatic updates pause when it is written to, and return on the user's yes or after five quiet minutes  · status: done

#### As-built

A session's automatic reports stop when it is written to and come back on the user's `yes` or after five unanswered minutes.

- `scripts/hooks/conversation_pause.py` is the library and its command line (`tick`, `status`, `resume`, `keep`). `message_arrived(session_id, source, prompt, now)` classifies a prompt as `PromptSource.TYPED`, `PEER`, `SCHEDULED` or `NOTICE`; a typed or peer message stops every notifier instance that targets the session and its showrunner footer, and writes one record per session id listing what it stopped. A notice or a scheduled prompt pauses nothing, and that return comes before any file is read.
- The record's phase is one of `Talking`, `Asked`, `KeptOff`, `Returned`. The Stop hook stamps the end of a reply; `tick(now)`, run each minute by the notifier instance `conversation-pause`, asks the session whether updates may return once it has been quiet for `QUIET_SECONDS`, and turns them back on when no answer comes within `ANSWER_SECONDS`. A typed `yes` runs `resume`; a typed `no` runs `keep`, which leaves them off, with no further question, until the user next types there or the session ends.
- `scripts/message/notifier.sh` has a `resume` verb that turns a stopped instance back on with its schedule intact.
- Nothing registers the two hooks, so no session runs them.

**Files:**
- `scripts/hooks/conversation_pause.py` — the library and command line.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — the prompt hook.
- `scripts/hooks/stop-conversation-pause.py` — the Stop hook.
- `scripts/hooks/test_conversation_pause.py` — 36 tests; hooks and the command line run as subprocesses against stubs.
- `scripts/message/notifier.sh`, `scripts/message/test_notifier.py` — the `resume` verb and its tests.
- `docs/as-built/session-notifier.md` — the verb's row and the two verb lists.

**Binds later work:** the environment overrides every test sets: `CONVERSATION_PAUSE_STATE_DIR` (records and `.lock`), `CONVERSATION_PAUSE_NOW_EPOCH`, `CONVERSATION_PAUSE_NOTIFIER`, `CONVERSATION_PAUSE_SESSIONS`, `CONVERSATION_PAUSE_SEND`, with `NOTIFIER_STATE_DIR` and `SHOWRUNNER_STATE_DIR`. The constants `QUIET_SECONDS = 300`, `ANSWER_SECONDS = 300`, `UNANSWERED_SECONDS = 1800`, `TOMBSTONE_SECONDS = 300`, `WATCHER = "conversation-pause"`. `RESUME_COMMAND` and `KEEP_COMMAND` are `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume` and the same line ending `keep`; the permission entry that lets a session run them matches that prefix.

**Gotchas:**
- `send.py` exits 0 with a first line `SENT:` on delivery and exits 1 with `QUEUED:` when the session was not reached; a resend with the same `--key` replaces the queued copy.
- `review_pause.py` keeps its record at `<showrunner state>/review-paused/<slug>.json`, and `/adhoc_review` ends with `resume none` when it paused nothing.
- This code holds its record lock across session lookups and notifier calls, writes `Asked` before the question is delivered, removes the watcher while `Returned` and `KeptOff` records remain, and knows nothing of an open `/adhoc_review`; the repair that follows changes each of these before any session runs the hooks.

**Ruled out:** converting stored records (none exist: the hooks were never registered); a pause that tells typed text from inter-session messages (every message pauses, by the user's word).

### Phase 2 — The pause never holds up a prompt, always ends, and stays out of an open review  · status: done

#### As-built

The pause is safe to register: it cannot delay a prompt, cannot stay on for ever, and leaves an open `/adhoc_review` alone.

- Each hook gives up after five seconds (`HookBudgetSpent`), prints one stderr line and exits 0; the next message finishes the pause. Hook replies are named types.
- `tick` holds the record lock only to read and write a record, never across a session lookup, a notifier call or a send. Before each send it reads the record again and sends only when the record is still `QuestionPending` with the same `due_at`.
- The record's states are `replying`, `quiet`, `question_pending`, `asked`, `kept_off` and `returned`, which is what `status` prints. The record lists only the instances and the footer this pause switched off. The five minutes to answer start when the question is delivered; a question that cannot be delivered returns the updates five minutes after it fell due. A pause ends at most 35 minutes after the last message.
- `ensure_watcher()` keeps the notifier instance `conversation-pause` alive while any record exists: complete and enabled is left alone, disabled is resumed, half-made (only `conf` or only `state`) is removed and created again. No pause starts without it.
- Resume and the automatic return switch on every instance and the footer they can, write the record back with only what failed, and raise `RuntimeError`; the next minute retries what is left.
- While `review_pause.py` has an open review record for the production, the pause neither asks nor returns; its clock runs on from the last reply once the review ends.
- The prompt hook calls `escalate.typed()` for every prompt classified `PromptSource.TYPED`; a failure there is one stderr line and never stops the hook.
- `scripts/message/sessions.py`: `read_session(path) -> SessionRecord | UnreadableSessionRecord`; the command exits 0 with the answer, 1 when no live session has the name, and 3 when it cannot answer (the registry cannot be listed, or a record cannot be read and no live record matched).
- `scripts/message/notifier.sh new` writes `state` before `conf`, both through helpers that report a failed write, so an instance is absent or complete; a later `new` completes one that stopped between the two.
- `scripts/production/stall_watch.py`: `socket_for_target` returns `_SessionSocket | _NoLiveSession | _SessionLookupUnavailable`. A showrunner whose session name is in the registry is never reported missing, and a minute with a lookup that could not answer judges nobody missing and removes no earlier report.

**Files:**
- `scripts/hooks/conversation_pause.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py` — the pause.
- `scripts/hooks/test_conversation_pause.py` — its tests; hooks and the command line run as subprocesses against stubs.
- `scripts/hooks/showrunner_footer.py`, `scripts/production/review_pause.py`, `scripts/production/test_review_pause.py` — the shared footer and review-record readers.
- `scripts/message/sessions.py`, `scripts/message/test_sessions.py` — the three exit codes.
- `scripts/message/notifier.sh`, `scripts/message/test_notifier.py` — the write order of `new`.
- `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py` — the missing rule.
- `pyrightconfig.json` — the `scripts/hooks` environment also reads `scripts/message`.

**Binds later work:** the words shown to the user, `RESUME_COMMAND`, `KEEP_COMMAND` and the command line's four verbs keep their text, so a permission entry can match those command lines character for character; neither hook needs a `timeout` in its registration. Registering the prompt hook also starts the typed-prompt record. Every importer of `sessions.read_session` must handle `UnreadableSessionRecord`.

**Gotchas:**
- A registry record that parses but lacks a field counts as unreadable.
- A type check on the changed files alone misses an importer elsewhere: `scripts/production/broadcast.py` broke this way. Check each whole directory.
- Other callers of `sessions.py` still read exit 3 as "not running": `notifier.sh`'s send path, `showrunners.py` `socket_for`, `build_hold.py`, `unit_status.sh`, `mac_test.py`.
- The pre-send re-check costs one more lock and read per question.

**Ruled out:** repairing around `notifier.sh`'s write order from the pause alone (a defect is fixed where it lives); treating an unreadable registry as "session ended".

### Phase 3 — The broadcast skips a session record it cannot read  · status: done

#### As-built

`scripts/production/broadcast.py` `live_sessions()` skips a registry file that `sessions.read_session` reports as `UnreadableSessionRecord`, so a broadcast reaches every live session when a registry file is not JSON or lacks a field.

**Files:**
- `scripts/production/broadcast.py` — the skip.
- `scripts/production/test_broadcast.py` — `test_a_registry_file_that_cannot_be_read_is_skipped`.

**Gotchas:** `broadcast.py` and `codex_winddown.py` are the only importers of `sessions` under `scripts/`; `codex_winddown.py` imports `SessionRecord` alone.

### Phase 4 — `/showrunner:rename_unit <old> <new>` renames a unit's session everywhere the showrunner reads it  · status: done

#### Work Order

**Goal:** One showrunner command gives a unit's session a new name: the Claude session, its tmux session, the registry, the production doc's Session cell and every store that keeps state under the old name. A run that stops part-way is finished by running the same command again. A unit renamed this way, or by the user typing `/rename` in it, is still a unit to every script.

**Spec:**

A. **The command.** `commands/showrunner/rename_unit.md`: frontmatter `description` and `argument-hint: <old> <new>`; a `**Usage:** /showrunner:rename_unit <old> <new>` line; it runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/rename_unit.py" --production <PRODUCTION_DOC> --scratch <SCRATCH> <old> <new>`, with `<SCRATCH>` as `commands/showrunner/produce.md` defines it (this showrunner session's scratchpad), then `Show the script's output. If it exits nonzero, show its message and stop.` Model: `commands/showrunner/add_unit.md`.

B. **`scripts/production/rename_unit.py`**, the one file that orders the work. It imports `add_unit` (for `read_production`, `unit_rows`, `live_unit_rows`, `cell_value`, `git`, `remote_head`, `Refusal`, and the shared commit function of item D), `tmux_names`, `showrunners` and `rename_state` (item F). A refusal prints `rename_unit: <reason>` to stderr and exits 1 with nothing changed.

The unit is recognised by its Claude session id and its tmux pane, never by a name alone, so preflight gives the same answer before the first step and after any step. Two named types carry what preflight found:
- `ClaudeSide = AwaitingRename(session_id, pane) | RenamedInClaude(session_id, pane)`.
  - `AwaitingRename`: exactly one live Claude session is named `<old>` and none is named `<new>`.
  - `RenamedInClaude`: exactly one live Claude session is named `<new>`, its record's `formerNames` holds an entry whose `name` is `<old>`, and no live Claude session is named `<old>`. `formerNames` is a list of `{name, until, sessionId}` objects (measured 2026-10-07 in a live record), never a list of strings.
  Live sessions come from `tmux_names.live_sessions()` (item C adds the session id to what it returns).
- `RowSide = RowNamesOld(row) | RowNamesNew(row)`: the one live Units row whose Session cell is `<old>` (`live_unit_rows`; Session is the fifth cell, read with `cell_value`); or, only when the Claude side is `RenamedInClaude` and no live row has `<old>`, the one live row whose Session cell is `<new>`.

Refusals, checked in this order before anything changes:
- `<new>` does not match `[A-Za-z0-9][A-Za-z0-9_-]*` (the pattern at `add_unit.py` line 184), or equals `<old>`.
- the showrunner's checkout is not on the merge branch (as `add_unit.preflight` checks).
- the Claude side is neither state above; the message names what was found.
- no row fits `RowSide`, or more than one does, or a second Units row, live or retired, has `<old>` or `<new>` as its Session.
- `<new>` already names something that is not this unit: a tmux session named `<new>` that does not host `pane`; a showrunner session named `<new>` in the registry (`showrunners.load_settings()`); a registry unit named `<new>` while the registry also holds a unit named `<old>`, or while the Claude side is `AwaitingRename`.

Steps. Each brings one thing in line with `<new>`, prints `renamed: <what>` when it changed something, and does nothing when that thing is already in line:
1. `AwaitingRename` only: `tmux send-keys -t <pane> -l "/rename <new>"`, then `tmux send-keys -t <pane> Enter` as its own call (never C-c or Escape). Then read the session record with that `session_id` once a second for up to 30 seconds until its `name` is `<new>`; if it never is, exit 1 with `rename_unit: the session did not take the name within 30 seconds; nothing else was changed`. The wait's length and its sleep come from one injectable pair so tests do not sleep.
2. `tmux_names.rename_session(pane, <old>, <new>)` (item C). On `RenameIncomplete` exit 1 with what it names.
3. `RowNamesOld` only: rewrite the row's Session cell: the name inside it becomes `<new>`, and its backticks and any commentary after the name stay. Then, for either row state, commit the production doc when it differs from `HEAD`, as `production(<slug>): <unit>'s session is now <new>`, and push the merge branch when origin differs, both through the shared commit function of item D. A rerun after the commit therefore only pushes.
4. `rename_state.rename_all(<old>, <new>, <scratch>)` (item F); print `renamed: <each description it returns>`. On `rename_state.RenameRefused` exit 1 with its reason.
5. Print `left: <what> — <why>` for what a running session cannot change: its remote-control name and its systemd scope (both fixed at launch), and the production LOG's `added <unit> …, tmux <old>` line (history).

C. **One rename operation in `scripts/production/tmux_names.py`, shared with the minute job.**
- `ClaudeSession` gains `session_id` (the record's `sessionId`).
- `rename_session(pane: str, old: str, new: str) -> SessionRenamed | RenameIncomplete`, where `RenameIncomplete` carries one sentence naming what is left. The registry goes first: when the registry holds `old` as a showrunner session or a unit, it calls `showrunners.change("rename", old, "", [], new)`; an `OSError` or `ValueError` there returns `RenameIncomplete` with the tmux session untouched. Then tmux: when `pane`'s session is named `old` it runs `tmux rename-session -t =<old> <new>`; named `new`, nothing; any other name, or a failed rename, returns `RenameIncomplete`.
- `tick()` calls `rename_session` for each session it renames today and prints an incomplete result as its one stderr line. Today it renames tmux first and only prints a registry failure, and the next minute sees equal names and never tries again (lines 100-112); with the registry first, a failed registry rename leaves tmux on `<old>` and the next minute tries again. `tick()`'s two faults (more than one Claude session, name already taken) stay as they are.
- `showrunners.change("rename", …)` (lines 338-350) must be safe to run again after it failed part-way: it writes the registry file only after every stall-state rename succeeded, and `stall_watch.rename_state` (line 171) moves a stretch only when its source exists. The tests below pin both; change `showrunners.py` or `stall_watch.py` only where one fails.

D. **A renamed unit can be adopted again.** `add_unit.py` lines 301-306 refuse an existing Units row whose Session differs from the unit's name less `-unit`, and `UnitLaunch.name` (line 95) then drives tmux, remote control, systemd, the prompt and the registry (lines 508-587) from the command line's name. Replace `name` with `identity: UnitIdentity`, a named pair `(unit, session)` built once where the row is resolved: an existing row gives `session` from its Session cell; no row gives the name the command line implies, as today. Every tmux, remote-control, systemd, prompt and registry call reads `identity.session`; the row and every message about the unit read `identity.unit`. The Session comparison goes. A new row is written as today. `commit_and_push` (lines 451-463) keeps its behaviour and its git calls move into one function that takes the production, the paths and the message; step 3 calls the same function.

E. **A renamed unit is still a unit.** `scripts/message/top_level.py` `is_unit` (lines 63-67) asks tmux for the unit mark by the session name inside the record's `tmux` field, which keeps the old name after a rename (measured). Ask by the pane id instead, the part after the last `.` in that field: `tmux show-environment -t %<n> SHOWRUNNER_UNIT` answers for the pane's current session (measured 2026-10-07).

F. **State kept under the old name moves through its owner.** `scripts/production/rename_state.py` (new) is the one entry: `rename_all(old: str, new: str, scratch: Path) -> list[str]` returns one description per thing it changed and raises `RenameRefused(reason)`. It runs the three parts below in this order; each does nothing when nothing is under `old`, so a second call returns an empty list.
- **The showrunner's scratch files.** The session that runs this command is the only writer of its scratchpad and runs one command at a time, so no lock is taken; each rewrite goes to a temporary file in the same directory and then `os.replace`. A missing file or directory is skipped. Where an entry already exists under `new`, it stays and the `old` entry is dropped. A JSON file that cannot be parsed raises `RenameRefused` naming it.
  - `<scratch>/dailies_input_state/eta_seen.json` (`waiting.py` line 227, `dailies_input.py` line 307): each key `<old>|<phase>` becomes `<new>|<phase>`.
  - `<scratch>/unit_status/decisions_seen` and `<scratch>/unit_status/blocks_open` (`unit_status.sh` lines 35-36; the directory is the first argument `commands/showrunner/dailies.md` line 33 passes): a `decisions_seen` line `<old>|<text>` and a `blocks_open` line whose first tab-separated field is `<old>` take the new name.
  - `<scratch>/showrunner_state.json` (`commands/showrunner/produce.md` line 37): each `units` row whose `unit` is `<old>`.
  - `<scratch>/dailies_judgment.json` (`dailies_input.py` lines 299-306): each `units` row whose `unit` is `<old>`.
  - `<scratch>/dailies_state.json` (`dailies_render.py` `load_state`, line 925): the top-level key `<old>`.
- **The message store.** `send.rename_recipient(old: str, new: str) -> list[str]` in `scripts/message/send.py`, all of it inside one `locked()` (line 172), on `STATE` (line 65, which follows `XDG_STATE_HOME`):
  - `queue/<old>.jsonl` (`queue_path`, line 194): each entry's `"to"` becomes `new`; the entries join `queue/<new>.jsonl`, keeping the latest per key as `enqueue` does; the old file is removed.
  - `keys.json` (line 182): in every key's `last` map, the instant under `old` moves to `new`; when both exist the later instant stays.
  - `relay/<old>.jsonl` (line 405) holds the last relay stream for a recipient: it becomes `relay/<new>.jsonl` when none exists, and is removed when one does.
- **The build hold.** `build_hold.rename_holder(old: str, new: str) -> list[str]` in `scripts/build_hold/build_hold.py`, inside `release_lock()` (line 425): the holder file moves from `holder_path(directory, old)` to `holder_path(directory, new)` (line 297) with the `holder` field inside it changed (line 403); in the stored cycle, the `holders` key, each `recipients` value and each `entries[].name` equal to `old` take the new name. When holder files exist under both names it raises `ValueError` naming both, which `rename_all` turns into `RenameRefused`.
`rename_state.py` reaches `send` and `build_hold` the way `dailies_input.py` reaches the build hold (line 22). None of the three functions gets a command-line verb.

G. **Tests.** Every state root points at a temporary directory (`XDG_STATE_HOME`, `BUILD_HOLD_DIR`, `NOTIFIER_SESSIONS_DIR`, `TMUX_NAMES_PROC_DIR`, the registry's and the stall watch's overrides), tmux is the stub on `PATH`, and git is a temporary repository with a bare origin as `test_add_unit.py` uses (lines 18-46 and 136).

Orchestration:
- `scripts/production/test_rename_unit.py`:
  - each refusal leaves the doc byte-identical, makes no commit and sends no keys; one case per collision in the last refusal;
  - the typed path: the stub records `-l "/rename <new>"` and `Enter` as two calls; when the record takes the name, tmux, the registry, the row (backticks and commentary kept) and the commit message are as Spec B says, and the bare origin has the commit;
  - the record never takes the name: exit 1, the doc and the registry unchanged;
  - `RenamedInClaude` (a `formerNames` list of objects): no keys sent, the rest done; a second full run changes nothing, prints no `renamed:` line and exits 0;
  - a failure injected after each boundary (the typed rename, the registry rename, the tmux rename, the cell written, the commit, the push, the state stores), then the same command again: the end state equals the uninterrupted run's;
  - step 4 through a recording stand-in for `rename_state.rename_all`, plus one case against the real module once it exists.
- `scripts/production/test_tmux_names.py`: a failing registry rename leaves the tmux session on `<old>` and returns `RenameIncomplete`, and the next `tick()` finishes both; a failing tmux rename after the registry succeeded is finished by the next call; `rename_session` touches no other pane's session.
- `scripts/production/test_showrunners.py`: `change("rename", …)` with a failing stall-state rename leaves the registry file byte-identical and a second call completes; the call twice in a row changes nothing the second time.
- `scripts/production/test_add_unit.py`: an existing row whose Session differs from the unit's name is adopted, and the tmux, systemd, remote-control and registry calls all carry the row's Session.
- `scripts/message/test_top_level.py`: a record whose `tmux` field holds a stale session name and a live pane id is a unit.

State stores:
- `scripts/production/test_rename_state.py`: each scratch file renamed; a missing file and a missing directory skipped; an entry already under `<new>` kept and the `<old>` one dropped; an unparsable JSON file refused by name; a second call returns an empty list and leaves every file byte-identical.
- `scripts/message/test_send.py`: queue entries' `"to"`, the merge keeping the latest per key, the old file gone; `keys.json` with one name and with both; the relay file in both cases; `XDG_STATE_HOME` honoured.
- `scripts/build_hold/test_build_hold.py`: the holder file and its `holder` field, the cycle's `holders`, `recipients` and `entries`; both names holding refused with nothing changed; no holder under `<old>` changes nothing.

**Files:**
- `commands/showrunner/rename_unit.md` — the command (new).
- `scripts/production/rename_unit.py` — preflight and the five steps (new).
- `scripts/production/test_rename_unit.py` — its tests (new).
- `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py` — `rename_session`, the session id, `tick()` through it.
- `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `scripts/production/test_showrunners.py` — the repeat-safety tests; code only where one fails.
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py` — `UnitIdentity`, the shared commit function, the adoption case.
- `scripts/message/top_level.py`, `scripts/message/test_top_level.py` — `is_unit` by pane id.
- `scripts/production/rename_state.py`, `scripts/production/test_rename_state.py` — `rename_all` and the scratch files (new).
- `scripts/message/send.py`, `scripts/message/test_send.py` — `rename_recipient`.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — `rename_holder`.

**Seats:** 2 writers, split by who owns the state: one orders the rename, the other moves what each store keeps. Each writes the tests for its own files.
- `impl` — orchestration: `commands/showrunner/rename_unit.md`, `scripts/production/rename_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py`, `scripts/production/showrunners.py`, `scripts/production/stall_watch.py`, `scripts/production/test_showrunners.py`, `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`, `scripts/message/top_level.py`, `scripts/message/test_top_level.py`; hub: `scripts/production/rename_unit.py` (the only caller of both writers' functions).
- `test` — opens as impl; the state stores: `scripts/production/rename_state.py`, `scripts/production/test_rename_state.py`, `scripts/message/send.py`, `scripts/message/test_send.py`, `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py`. Item F fixes the three signatures the other writer calls.

**Constraints from prior phases:** Phase 1 touches none of these files. Phase 2 changed `scripts/production/stall_watch.py` and `scripts/production/test_stall_watch.py`: `socket_for_target` returns `_SessionSocket | _NoLiveSession | _SessionLookupUnavailable`, and `tick` never reports a showrunner whose session name is in the registry as missing, and judges nobody missing in a minute when a lookup could not answer; `rename_state` is unchanged. `scripts/message/sessions.py socket` exits 3 when it cannot answer and 1 when no live session has the name. Phase 3 touches only `scripts/production/broadcast.py` and its test. This phase runs before the registration (Phase 6). Once Phase 6 registers the pause, typing `/showrunner:rename_unit` pauses the showrunner's dailies and footer, and the `/rename <new>` this command types into the unit's pane pauses that unit's status reports; both pauses are keyed by session id, so they survive the new name untouched and return as any pause does. No pause record is moved. `scripts/production/tmux_names.py`, `scripts/message/send.py`, `scripts/message/top_level.py`, `scripts/build_hold/build_hold.py` and their tests belong to no live unit's row: the checkpoint notice names them as `also touches`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/message -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; `basedpyright` on each whole directory `scripts/production`, `scripts/message`, `scripts/hooks`, `scripts/build_hold` and `scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`. Live check by the showrunner after it promotes the command: rename a throwaway unit and read the row, `tmux ls` and the next dailies.

### Phase 5 — A bare yes or no meant for another question is left to the session  · status: todo

#### Work Order

**Goal:** While the pause's return question is open, a bare `yes` or `no` the user types in answer to a newer question from the session is no longer taken as the answer to the return question. The user, 2026-10-07 about 16:00 PDT: "yes that's fine if you protect this - but make sure to test it with a scratch session you run first". This phase runs before the registration so the hooks every session gets are the protected ones (the unit director's ordering).

**Spec:**
- `scripts/hooks/conversation_pause.py`: once the question has been put to the user, the pause counts the session's replies that end after it (the Stop hook sees each). The reply that carries the question is the first. While only that reply has ended, a bare `yes` or `no` is the answer, as today. Once a later reply has ended, a bare `yes` or `no` is ordinary typing: it goes to the session untouched, the question stays open, and it still times out into the return after `ANSWER_SECONDS`.
- The count lives in the pause record's `asked` state as a named field, never a bare optional; a record written before this change reads as "only the question's reply has ended".
- The session can still answer for the user through `RESUME_COMMAND` and `KEEP_COMMAND` when the user's sentence means yes or no about the updates; that path is unchanged.
- `status` says when a newer reply has taken the bare answer away, in one clause.
- `scripts/hooks/test_conversation_pause.py`: the question asked, its reply ended, `yes` returns the updates; the question asked, a second reply ended, `yes` reaches the session, the record stays `asked`, and the timeout still returns the updates; the same for `no`; an old record without the field behaves as before.
- The unit director reads `message_arrived`, `answer`, `mark_answered` and the Stop hook at this phase's start and tightens this Spec before dispatch.

**Files:**
- `scripts/hooks/conversation_pause.py` — the count and the rule.
- `scripts/hooks/stop-conversation-pause.py` — counts a reply that ends after the question, where the change needs it.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — only where the rule needs it.
- `scripts/hooks/test_conversation_pause.py` — the cases above.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/hooks/conversation_pause.py`, `scripts/hooks/stop-conversation-pause.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py`.
- `test` — `scripts/hooks/test_conversation_pause.py`.

**Constraints from prior phases:** Phase 2 named the states (`replying`, `quiet`, `question_pending`, `asked`, `kept_off`, `returned`) and made each hook give up after five seconds; neither may hold up a prompt. Phase 1 built the three scripts. The hooks are not registered yet (Phase 6), so nothing live runs this code. Tests never start a real `claude` or `tmux` and never write `~/.local/state/`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `basedpyright scripts/hooks` ends `0 errors, 0 warnings, 0 notes`. Then, before the checkpoint, the user's live check, run by the unit director in a scratch Claude session it starts itself: the two hooks registered for that session alone (a scratch settings file, the pause's state under a temporary directory, short timings), never in `settings.json`. In it: the question arrives, the session asks a question of its own, a typed `yes` reaches the session and the updates stay paused; in a second round a typed `yes` straight after the question returns the updates. The checkpoint notice states what the scratch session showed.

### Phase 6 — The two hooks are registered, and the commands say what they do  · status: todo

#### Work Order

**Goal:** Every session runs the two hooks, and the showrunner and unit director commands say what happens when the user writes.

**Spec:**
- `settings.json`, in `hooks`: a new `"UserPromptSubmit"` list holding one group whose one hook is `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/user-prompt-submit-conversation-pause.py\""}`; and, appended to the existing `Stop` group's `hooks` list, `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/stop-conversation-pause.py\""}`. In `permissions.allow`, one entry: `Bash("$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" *)` (the quotes escaped as JSON requires); it matches the command lines the hook hands the session, `RESUME_COMMAND` and `KEEP_COMMAND` in `conversation_pause.py`, character for character. Nothing else in the file changes.
- `commands/showrunner/produce.md`, beside the line `The dailies keep their own switch.`: a short paragraph. When the user types a message here, the dailies and the footer pause on their own; a `conversation-pause:` message later asks you to put one question to the user, word for word; never answer it for them; their typed yes or no is handled without you. The question comes five minutes after your reply ends, or thirty minutes after their message when the reply was interrupted; unanswered for five minutes, the updates return on their own. While an `/adhoc_review` is open here the question waits for the review to end. `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" status` says what is paused.
- `commands/unit/delegate.md`, in the progress contract beside `If the user stops updates, use /unit:report off`: one sentence saying a message the user types pauses the status reports on its own and the same `conversation-pause:` message follows.
- `scripts/hooks/test_conversation_pause.py`: one test that reads `settings.json` and finds both hook commands and the permission entry, each exactly once, and that the permission entry is exactly `f"Bash({RESUME_COMMAND.rsplit(' ', 1)[0]} *)"`, with `KEEP_COMMAND.rsplit(' ', 1)[0]` equal to the same prefix.

**Files:**
- `settings.json` — the two registrations and the permission entry.
- `commands/showrunner/produce.md` — the paragraph.
- `commands/unit/delegate.md` — the sentence.
- `scripts/hooks/test_conversation_pause.py` — the registration test.

**Seats:** 2 writers, one for the registration and its test, one for the two command files. Nothing here has a test lane worth a seat of its own: the one test is three lines.
- `impl` — `settings.json`, `scripts/hooks/test_conversation_pause.py`.
- `test` — opens as impl: `commands/showrunner/produce.md`, `commands/unit/delegate.md`.

**Constraints from prior phases:** Phase 3 (the broadcast repair) and Phase 4 (the rename command) touch none of this phase's files. Phase 5 changed `scripts/hooks/conversation_pause.py` and `scripts/hooks/test_conversation_pause.py` so a bare `yes` or `no` is left to the session once the session has written since the question; the registration test goes beside that phase's tests, and the hooks registered here are the protected ones. Since Phase 2 the prompt hook also calls `escalate.typed()` (`scripts/message/escalate.py`) for every typed prompt, so registering it starts that record too. Phase 2 repaired the pause and renamed its states (`replying`, `quiet`, `question_pending`, `asked`, `kept_off`, `returned`: what `status` prints); each hook gives up after five seconds, so neither needs a `timeout` in its registration. Phase 1 built `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py` and `scripts/hooks/conversation_pause.py` (commands `status`, `resume`, `keep`, `tick`). The question the session is asked to put is `Return to automatic updates? (yes / no) They return on their own in 5 minutes.`, sent from `conversation-pause`. The session is handed two command lines to run through Bash, `RESUME_COMMAND` and `KEEP_COMMAND` (`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume` and `… keep`); each reads `CLAUDE_CODE_SESSION_ID`. Timings: the question five minutes after a reply ends (`QUIET_SECONDS`), thirty minutes after the user's message when no reply ended (`UNANSWERED_SECONDS`), the return five minutes after an unanswered question (`ANSWER_SECONDS`), a late yes or no for five minutes after that (`TOMBSTONE_SECONDS`).

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -c "import json; json.load(open('settings.json'))"` exits 0; `bash scripts/agents/test_agents_config.sh` green. Live check by the unit director after the showrunner promotes it: a typed message in a session with a running report shows the pause notice, five quiet minutes later the question arrives, a typed `yes` brings the reports back, and a sentence that means yes makes the session run the resume command with no permission prompt; a second round answered `no` leaves them off, `status` says so, and the `conversation-pause` notifier instance is gone once no record is left.

### Phase 7 — A progress report shows the closing work of a unit's last piece of work  · status: todo

#### Work Order

**Goal:** A `/unit:report` that arrives while a unit runs its closing steps (the shrink of its last phase, the final gate) prints both progress tables, with a row for the step that is running. Today it prints `No active phase to report`, so the user gets a report without tables at the point where they want to know how close the unit is to done. Added by the user, 2026-10-07 about 15:55 PDT: "add it to the end of your current work orders and do it then".

**Spec:**
- `scripts/delegate/progress_history.py`, the `progress` command: the recorder already keeps a row for an activity opened after its phase closed, and `timeline` already lists it (checked in a scratch run, 2026-10-07). While such an activity is open and no phase is active, `progress` prints the scope table and the round table for the phase that closed last, from that phase's last recorded values, with the open activity as the running row. It prints them in the form it uses between windows: the `as of` line, both tables, the wall clock line.
- With no phase active and no activity open, `progress` still answers `No active phase to report`, as today.
- With a phase active, nothing changes: every existing `progress` output stays byte for byte the same.
- `calibrate` is accepted in the same state, or `progress` needs no calibration there; the writer picks the one that leaves `commands/unit/report.md` step 5 runnable as written, and says which in its summary.
- `scripts/delegate/test_progress_history.py`: a run whose last phase is finished, then an activity opened (`start-activity --label shrink`): `progress` exits 0 and prints both tables with the activity's row running; after `finish-activity`, `progress` answers `No active phase to report`; a run with a phase active prints what it printed before this change.

**Files:**
- `scripts/delegate/progress_history.py` — `progress` while an activity is open on a closed phase.
- `scripts/delegate/test_progress_history.py` — the three cases.

**Seats:** 1 writer + 1 tester.
- `impl` — `scripts/delegate/progress_history.py`.
- `test` — `scripts/delegate/test_progress_history.py`.

**Constraints from prior phases:** No earlier phase of this plan touches `scripts/delegate/`. The two files belong to no live unit's row: the checkpoint notice names them as `also touches`. Other units run `progress_history.py` from the live `~/.claude` checkout on every status report, so every existing command keeps its output; tests point `--session-dir` at a temporary directory and never read a real run's state.

**Acceptance gate:** `python3 -m unittest discover -s scripts/delegate -p 'test_*.py'` green; `basedpyright scripts/delegate` reports no more problems than the count the unit director measures on this phase's starting commit, and none in the two files' changed lines.

### Phase 8 — The registry records each unit director's status, and the stall watch reads it there  · status: todo

#### Work Order

**Goal:** Each unit director's current status (running, run finished, standing by) is recorded in the registry the showrunners already have, `config/showrunners.json`, and the stall watch reads a finished run from that record, never from words in a production doc. A live unit whose Plan cell mentions `run done` inside a description is still watched. Added by the user, 2026-10-07 about 16:00 PDT: "current is fine", then, on how a finished run is told: "have a configuration file for unit directors (don't we already have one going for showrunners? maybe we could add it to that) that shows the current status of unit directors", "probably that's better".

**Spec:**
- `scripts/production/showrunners.py`: each unit entry in the registry carries a status, a named type with one value per state the scripts tell apart today (running, run finished, standing by; the writer reads `stall_watch.py` and `add_unit.py` for the full set). A registry written before this change reads as running. One `change` verb sets a unit's status, safe to run twice.
- `scripts/production/stall_watch.py` `finished_run_units`: a unit is finished when the registry says so. The `re.search(r"\brun done\b", cells[2])` over the Plan cell goes; `plan_cell_is_retired` stays for retired rows unless the registry status covers it too.
- The instructions name the one command a unit director runs when its run ends, and nothing asks it to write a phrase: `docs/production_format.md` (<ProductionUnit/>) and `docs/delegate/final_gate_commit.md`, one sentence each. `add_unit.py` writes `running` when it launches or adopts a unit.
- Tests: a Plan cell that mentions `run done` leaves the unit watched; a unit the registry marks finished is skipped; an old registry file without the field reads every unit as running; setting the status twice changes nothing the second time.
- The unit director researches the registry's present layout and its readers at this phase's start and tightens this Spec before dispatch.

**Files:**
- `scripts/production/showrunners.py`, `scripts/production/test_showrunners.py` — the status and its verb.
- `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py` — the reader.
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py` — the status at launch.
- `docs/production_format.md`, `docs/delegate/final_gate_commit.md` — the command a unit director runs.

**Seats:** 2 writers, each writing the tests for its own files.
- `impl` — `scripts/production/showrunners.py`, `scripts/production/test_showrunners.py`, `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`.
- `test` — opens as impl: `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`, `docs/production_format.md`, `docs/delegate/final_gate_commit.md`.

**Constraints from prior phases:** Phase 2 changed `scripts/production/stall_watch.py` and its test. Phase 4 changes `showrunners.py`, `add_unit.py` and `stall_watch.py` for the rename (`UnitIdentity`, repeat-safe `change("rename", …)`); build on what it leaves. The two docs are instructions every unit director reads from the live checkout: add the sentences, change nothing else. Tests never write `~/.claude/config/`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `basedpyright` on each whole directory `scripts/production`, `scripts/message`, `scripts/hooks`, `scripts/build_hold` and `scripts/mac_test` ends `0 errors, 0 warnings, 0 notes`.

### Phase 9 — Showrunners and unit directors are told they may always correct an out-of-date as-built doc  · status: todo

#### Work Order

**Goal:** No showrunner or unit director asks before correcting an as-built doc that no longer matches the code, whichever unit owns the doc. The user, 2026-10-07 about 16:10 PDT: "you never have to ask about correcting out of date as-built's - add a note to follow up and fix the instructions taht guide showrunners and that guides unit directors that they should always be free to correct as-built documentation".

**Spec:**
- `docs/production_format.md`, <ProductionUnit/> item 9 (files you do not own): one sentence. Correcting an as-built doc under `docs/as-built/` that contradicts the code is always allowed, in any unit's doc, without asking the user, the showrunner or the owner; the checkpoint notice still names the file as `also touches`.
- `commands/showrunner/produce.md`, in its rules: the same rule for the showrunner, and that it never holds a merge because a unit corrected another unit's as-built doc.
- `docs/delegate/final_gate_commit.md`, where the as-built pass is defined: the same rule for the as-built pass.
- Each sentence cites the user and the date. Nothing else in the three files changes.

**Files:**
- `docs/production_format.md` — the rule for unit directors.
- `commands/showrunner/produce.md` — the rule for showrunners.
- `docs/delegate/final_gate_commit.md` — the rule in the as-built pass.

**Seats:** 1 writer. The unit director makes these edits directly: three sentences of instruction text, no code.
- `impl` — `docs/production_format.md`, `commands/showrunner/produce.md`, `docs/delegate/final_gate_commit.md`.

**Constraints from prior phases:** Phase 6 adds a paragraph to `commands/showrunner/produce.md`; Phase 8 adds a sentence to `docs/production_format.md` and to `docs/delegate/final_gate_commit.md`. Add beside them, change none of them. Every unit reads these three files from the live checkout.

**Acceptance gate:** each of the three files states the rule once; `git diff --stat` for the phase names only those three files and the plan doc.

### Phase 10 — Every phone alert goes through the one command that reaches the user  · status: todo

#### Work Order

**Goal:** Nothing sends a phone alert directly. Every script and command that reaches the user's phone does it through `send.py --to user`, so the urgency levels and anything else that command does apply everywhere. The user, 2026-10-07 about 15:55 PDT: "they should all be updated to use the one path to communicate with me". Asked first by the showrunner (natedev) the same day.

**Spec:**
- `scripts/message/send.py --to user --summary TITLE [--need note|decision|blocked] --text MESSAGE` is the one way (main `49573a8`); need maps to priority: note 0, decision 1, blocked 2. Each caller below stops calling `scripts/notify/pushover.py` and calls it, keeping its title, its message and its urgency (priority 0 → `note`, 1 → `decision`, 2 → `blocked`), and keeping what it does today when the send fails.
- `scripts/production/ci_points.py` (near line 234), `scripts/buildlog/rust_release.py` (near line 157), `scripts/lint/sweep.py` (near line 1052): the call and its test, each test proving the command line the script runs, with a stub in place of `send.py`.
- `commands/showrunner/produce.md` (the section near line 710 that tells the showrunner to run `pushover.py`) and `commands/builds.md`: the instruction names `send.py --to user`, as `commands/alert_user.md` already does, the Mac's `--machine natedev` included where the command can run on the Mac.
- A caller that can run on the Mac passes `--machine natedev`: the keys exist only on natedev.
- `scripts/notify/pushover.py` stays: `send.py` calls it.
- The unit director reads each call site and `send.py`'s user path at this phase's start and tightens this Spec before dispatch.

**Files:**
- `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`
- `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`
- `commands/showrunner/produce.md`, `commands/builds.md`

**Seats:** 2 writers, each writing the tests for its own files.
- `impl` — `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`, `commands/showrunner/produce.md`, `commands/builds.md`.
- `test` — opens as impl: `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`, `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`.

**Constraints from prior phases:** Phases 6 and 9 each add text to `commands/showrunner/produce.md`; change only the phone-alert instruction there. None of these files is in this unit's row: the checkpoint notice names each as `also touches`, with its owner where the production doc gives one. Tests never send a real alert and never read `~/.config/pushover/env`.

**Acceptance gate:** `python3 -m unittest discover -s <dir> -p 'test_*.py'` green for `scripts/production`, `scripts/buildlog`, `scripts/lint` and `scripts/message`; `basedpyright scripts/production` ends `0 errors, 0 warnings, 0 notes`, and `basedpyright` on `scripts/buildlog` and `scripts/lint` reports no more problems than the unit director measures on this phase's starting commit; `grep -rn "pushover.py" scripts commands` names only `scripts/notify/`, `scripts/message/` and `commands/alert_user.md`.
