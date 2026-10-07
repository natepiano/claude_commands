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

#### As-built

- `/showrunner:rename_unit <old> <new>` renames a running unit's session in one command: the Claude session (it types `/rename <new>` into the unit's pane and waits up to 30 seconds for the session record to take the name), the tmux session and the registry entry together, the production doc's Units row (committed and pushed to the merge branch), and everything kept under the old name: queued messages, the relay, build holds and the scratch status files. It also finishes a rename the user already typed in the unit's session.
- A rename changes the session's name only. The Unit cell, plan, branch and worktree keep theirs.
- A rerun after a stop at any step finishes the same rename and changes nothing twice. It prints one `renamed: …` line for each thing it changed and three `left: …` lines for what it leaves alone: the remote-control name and the systemd scope (both fixed at launch) and the old LOG entry (history).
- `scripts/production/rename_unit.py` holds the order: `preflight` → `RenamePlan(production, claude, row, unit)`, where `claude` is `AwaitingRename | RenamedInClaude` and `row` is `RowNamesOld | RowNamesNew`; then the typed rename and `_wait_for_claude(session_id, new, wait: RenameWait)`; then `tmux_names.rename_session(pane, old, new) -> SessionRenamed | RenameIncomplete` (registry first, then tmux); then the row commit through `add_unit.commit_paths_and_push`; then `rename_state.rename_all(old, new, scratch)`, which raises `RenameRefused` naming the file it could not move.
- `scripts/production/add_unit.py` carries a unit's name and its session's name as one value: `UnitIdentity(unit, session)`. `launch_request` returns `RequestedUnitLaunch` (`requested_name`, `unit`); `preflight` builds the one `UnitLaunch` with `identity: UnitIdentity`, taking the session name from an existing row's Session cell, else the requested name; every later step reads `request.identity.unit` or `request.identity.session`.

**Files:**
- `commands/showrunner/rename_unit.md` — the command.
- `scripts/production/rename_unit.py`, `scripts/production/test_rename_unit.py` — preflight, the typed rename and its wait, the row rewrite and commit, the printed lines.
- `scripts/production/rename_state.py`, `scripts/production/test_rename_state.py` — the scratch status files and the state stores.
- `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py` — `rename_session`.
- `scripts/production/showrunners.py`, `scripts/production/test_showrunners.py` — `change("rename", …)`, safe to repeat; it returns without writing when the registry does not hold the old name.
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_waiting.py` — `UnitIdentity`, `RequestedUnitLaunch`, `UnitLaunch`, `commit_paths_and_push`.
- `scripts/message/send.py`, `scripts/message/test_send.py` — `rename_recipient`, `_rename_queue`, `_merge_queues`.
- `scripts/message/top_level.py`, `scripts/message/test_top_level.py` — `is_unit` reads `SHOWRUNNER_UNIT` from the pane.
- `scripts/build_hold/build_hold.py`, `scripts/build_hold/test_build_hold.py` — `rename_holder`.

**Binds later work:** `add_unit.record()` registers `request.identity.session`, so that is where a unit enters the registry. `showrunners.change("rename", …)` rebuilds each typed registry entry as `type(unit)(new_session)`: an entry type with one session field keeps its variant through a rename. `send.py`'s `USER_CHANNEL`, `user()`, the `--to user` parsing, the need-to-priority mapping and the exit contract are as they were before the rename command.

**Gotchas:**
- Only a session record whose process is alive is read (`os.kill(pid, 0)`): a resumed unit leaves a dead record carrying the same session id.
- `blocks_open` is keyed by its first tab field: an old-name row is dropped when a new-name row exists, else renamed. `decisions_seen` is keyed by the whole line.
- A queue move skips entries the new queue already holds, writes the merged queue atomically, then removes the old one, so a rerun never doubles a message.
- No test renames a live unit: the Claude session name, tmux and the next dailies need live services, and that live check has not been run.

**Ruled out:** Refusing a rename when the registry does not hold the old name; the registry change returns without writing, so a rerun after a partial rename still finishes.

### Phase 5 — A bare yes or no meant for another question is left to the session  · status: todo

#### Work Order

**Goal:** While the pause's return question is open, a bare `yes` or `no` the user types in answer to a newer question from the session is no longer taken as the answer to the return question. The user, 2026-10-07 about 16:00 PDT: "yes that's fine if you protect this - but make sure to test it with a scratch session you run first". This phase runs before the registration so the hooks every session gets are the protected ones (the unit director's ordering).

**Spec:**
- `scripts/hooks/conversation_pause.py`: once the question has been put to the user, the pause counts the session's replies that end after it (the Stop hook sees each). The reply that carries the question is the first. While only that reply has ended, a bare `yes` or `no` is the answer, as today. Once a later reply has ended, a bare `yes` or `no` is ordinary typing: it goes to the session untouched, the question stays open, and it still times out into the return after `ANSWER_SECONDS`.
- The count lives in the pause record's `asked` state as a named field, never a bare optional; a record written before this change reads as "only the question's reply has ended".
- The session can still answer for the user through `RESUME_COMMAND` and `KEEP_COMMAND` when the user's sentence means yes or no about the updates; that path is unchanged.
- `status` says when a newer reply has taken the bare answer away, in one clause.
- `scripts/hooks/test_conversation_pause.py`: the question asked, its reply ended, `yes` returns the updates; the question asked, a second reply ended, `yes` reaches the session, the record stays `asked`, and the timeout still returns the updates; the same for `no`; an old record without the field behaves as before.
- The transitions, checked against the code: `Asked` becomes `Asked(asked_at, replies_ended_since_question)`. `tick` creates it with 0; `_phase_from_json` reads an `asked` record written before this change, without the field, as 1; `_phase_json` always writes the field. `mark_answered` is renamed `mark_reply_ended`: the Stop hook calls it, it keeps the `Replying` → `Quiet` transition, and while `Asked` it adds one to `replies_ended_since_question`. `message_arrived` treats a typed `yes` or `no` as the pause's answer only while that count is 0 or 1; above 1 it returns `NoReply.NOTHING` without calling `_pause_locked`, so the prompt reaches the session and the `Asked` record and its `asked_at` timeout are unchanged. `status` appends "a newer reply ended; bare yes/no belongs to the session" while the count is above 1.
- Tests call the Stop hook once and twice, cover `yes` and `no`, the old-record default, the unchanged `RESUME_COMMAND`/`KEEP_COMMAND` path, and the timeout counted from the original `asked_at`.

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

**Constraints from prior phases:** Phase 3 (the broadcast repair) and Phase 4 (the rename command) touch none of this phase's files. Phase 5 changed `scripts/hooks/conversation_pause.py` and `scripts/hooks/test_conversation_pause.py` so a bare `yes` or `no` is left to the session once a second reply after the question has ended; the registration test goes beside that phase's tests, and the hooks registered here are the protected ones. Since Phase 2 the prompt hook also calls `escalate.typed()` (`scripts/message/escalate.py`) for every typed prompt, so registering it starts that record too. Phase 2 repaired the pause and renamed its states (`replying`, `quiet`, `question_pending`, `asked`, `kept_off`, `returned`: what `status` prints); each hook gives up after five seconds, so neither needs a `timeout` in its registration. Phase 1 built `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py` and `scripts/hooks/conversation_pause.py` (commands `status`, `resume`, `keep`, `tick`). The question the session is asked to put is `Return to automatic updates? (yes / no) They return on their own in 5 minutes.`, sent from `conversation-pause`. The session is handed two command lines to run through Bash, `RESUME_COMMAND` and `KEEP_COMMAND` (`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume` and `… keep`); each reads `CLAUDE_CODE_SESSION_ID`. Timings: the question five minutes after a reply ends (`QUIET_SECONDS`), thirty minutes after the user's message when no reply ended (`UNANSWERED_SECONDS`), the return five minutes after an unanswered question (`ANSWER_SECONDS`), a late yes or no for five minutes after that (`TOMBSTONE_SECONDS`).

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -c "import json; json.load(open('settings.json'))"` exits 0; `bash scripts/agents/test_agents_config.sh` green. Live check by the unit director after the showrunner promotes it: a typed message in a session with a running report shows the pause notice, five quiet minutes later the question arrives, a typed `yes` brings the reports back, and a sentence that means yes makes the session run the resume command with no permission prompt; a second round answered `no` leaves them off, `status` says so, and the `conversation-pause` notifier instance is gone once no record is left.

### Phase 7 — A progress report shows the closing work of a unit's last piece of work  · status: todo

#### Work Order

**Goal:** A `/unit:report` that arrives while a unit runs its closing steps (the shrink of its last phase, the final gate) prints both progress tables, with a row for the step that is running. Today it prints `No active phase to report`, so the user gets a report without tables at the point where they want to know how close the unit is to done. Added by the user, 2026-10-07 about 15:55 PDT: "add it to the end of your current work orders and do it then".

**Spec:**
- `scripts/delegate/progress_history.py`, the `progress` command: the recorder already keeps a row for an activity opened after its phase closed, and `timeline` already lists it (checked in a scratch run, 2026-10-07). While such an activity is open and no phase is active, `progress` prints the scope table and the round table for the phase that closed last, from that phase's last recorded values, with the open activity as the running row. It prints them in the form it uses between windows: the `as of` line, both tables, the wall clock line.
- With no phase active and no activity open, `progress` still answers `No active phase to report`, as today.
- With a phase active, nothing changes: every existing `progress` output stays byte for byte the same.
- `calibrate` stays accepted after a phase closes, as it is today. While that closed phase has an open activity, `progress` ignores `pending_calibration`, writes no progress event and changes no recorder state, renders the phase's last recorded percentages and clocks, and includes the live activity row; `commands/unit/report.md` step 5 runs unchanged.
- `_reported_window` returns a named result (`ReportedPassWindow | ReportedActivityWindow | NoReportedWindow`) in place of `tuple[str, dict[str, object] | None]`, because this phase changes that function. The other optionals already in the report path stay as they are unless this change touches them (the unit director's call: the script runs under every live run, so the change stays small).
- The closed-phase tests also prove that `calibrate` succeeds, that `progress` appends no `progress_reported` event and leaves the recorder state unchanged, and that the output for an active phase is byte for byte what it was.
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
- `scripts/production/showrunners.py` replaces `WorkingUnit(name) | StandbyUnit(name)` with `RunningUnitDirector(session) | RunFinishedUnitDirector(session) | StandingByUnitDirector(session)`, named together `RegisteredUnitDirector`. The registry file stores each unit as an object `{"session": "<unit session>", "status": "running" | "run-finished" | "standing-by"}`. `load_settings()` reads `CONFIG` and `load_settings_from(path: Path)` reads a path a test or caller names, in place of today's `Path | None` parameter. Both also read the old layout (`units: [str]` plus `standby: [str]`): an entry in the standby list is `StandingByUnitDirector`, every other one `RunningUnitDirector`. `stored_settings` writes only the object form.
- A new verb: `showrunners.py status <showrunner-session> --unit <unit-session> --state running|run-finished|standing-by`. `--state` is parsed at the command line into the named variant and handed to one `set_unit_status` operation. A unit that is absent or ambiguous is refused; setting the state a unit already has returns without rewriting the file.
- `add` creates a normal entry as running and a `--standby` entry as standing by, and keeps an existing entry's status, so an import never brings a finished run back. `ready` changes only standing by to running. `add_unit.record()` sets standing by for a standby request and running for every real launch or adoption, an already registered finished session included.
- Readers use the field `session`, and `showrunners.py list` prints every unit's status. `live_units.py` still includes a finished unit: finished quiets the stall alerts, it does not retire the unit or take it out of the showrunner's status reports.
- `scripts/production/stall_watch.py` takes a finished run from the registry and standing by from the typed entry. The `re.search(r"\brun done\b", cells[2])` over the Plan cell goes; the production doc is still read for the Plan cells `plan_cell_is_retired` recognises.
- `docs/production_format.md` (<ProductionUnit/>) and `docs/delegate/final_gate_commit.md`, one sentence each: a production unit runs `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/production/showrunners.py" status "<showrunner session>" --unit "<this row's Session cell>" --state run-finished` only after <RunAsBuilt/> and, where it applies, <AsBuiltCommit/> are complete, and before <RunSummary/>. A run that stopped, failed or waits on a decision does not mark itself finished, and nothing asks for `run done` text in the Plan cell.
- Tests: a Plan cell that mentions `run done` leaves the unit watched; a registry entry that is finished or standing by is skipped; an old-layout standby entry stays standing by and every other old entry reads as running; setting a status twice leaves the file byte for byte the same; an import keeps every existing status; a launch or adoption sets a finished entry back to running; `list` shows every status; `live_units` keeps a finished entry; a rename keeps each of the three variants.

**Files:**
- `scripts/production/showrunners.py`, `scripts/production/test_showrunners.py` — both layouts read, the status types, the `status` verb, `list`, the rename keeping a status.
- `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py` — the status at launch, adoption and standby.
- `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py` — the reader, and the retired-row handling that stays.
- `scripts/production/rename_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py` — the `session` field, and the tests' raw registry contents.
- `scripts/production/live_units.py`, `scripts/production/test_live_units.py` — the `session` field, and the rule that a finished unit stays listed.
- `scripts/production/test_update_registration.py` — the tests' raw registry contents; `update_registration.py` changes only if the suite requires it.
- `docs/production_format.md`, `docs/delegate/final_gate_commit.md` — the command a unit director runs, and when.

**Seats:** 2 writers, each writing the tests for its own files; `showrunners.py` is the one hub file.
- `impl` — `scripts/production/showrunners.py`, `scripts/production/test_showrunners.py`, `scripts/production/add_unit.py`, `scripts/production/test_add_unit.py`, `scripts/production/test_update_registration.py`; it settles the registry types and the reader interface first and posts them on the board.
- `test` — opens as impl, on the readers once that interface is posted: `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`, `scripts/production/rename_unit.py`, `scripts/production/test_rename_unit.py`, `scripts/production/tmux_names.py`, `scripts/production/test_tmux_names.py`, `scripts/production/live_units.py`, `scripts/production/test_live_units.py`, `docs/production_format.md`, `docs/delegate/final_gate_commit.md`.

**Constraints from prior phases:** Phase 2 changed `scripts/production/stall_watch.py` and its test. Phase 4 is committed as `8adab2c`: `RequestedUnitLaunch` carries `requested_name` and `unit`; `preflight` returns `ReadyToLaunch.launch`, a `UnitLaunch` with `identity: UnitIdentity(unit, session)`; `add_unit.record()` registers `request.identity.session`. `showrunners.change("rename", …)` rebuilds each typed entry as `type(unit)(new_session)`, so the three status variants keep one `session` field and a rename keeps the variant; `scripts/production/test_showrunners.py` pins that. The two docs are instructions every unit director reads from the live checkout: add the sentences, change nothing else. Other units run these scripts from the live checkout, so a registry in the old layout must keep reading. Tests never write `~/.claude/config/`.

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
- The moves, checked against the code: `ci_points.review_watch` calls `send.py --to user --need decision --summary "Hana: review watch" --text <notice>` and keeps its `required(...)` failure handling. `rust_release.send_release_text` calls `--need note --summary <title> --text <message>` and still returns true only on exit 0. `sweep.send_floor_alert` keeps its `--to natedev` delivery and replaces only the phone command, with `--to user --need note --summary "natedev: disk under its floor" --text <message>`; either channel succeeding still counts as delivered. `commands/showrunner/produce.md` maps priorities 0/1/2 to note/decision/blocked, keeps its fallback when the send fails, and adds `--machine natedev` when run on the Mac. `commands/builds.md` uses `--need decision` with its present title and line, and adds `--machine natedev` on the Mac.
- `test_ci_points.py` and `test_dailies_input.py` stub `send.py` and assert that exact review-watch command (`dailies_input.py` imports `ci_points.review_watch`, and its test asserts today's Pushover stub); the rust-release and sweep tests assert their exact commands and the unchanged failure outcomes.

**Files:**
- `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`, `scripts/production/test_dailies_input.py`
- `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`
- `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`
- `commands/showrunner/produce.md`, `commands/builds.md`

**Seats:** 2 writers, each writing the tests for its own files.
- `impl` — `scripts/production/ci_points.py`, `scripts/production/test_ci_points.py`, `scripts/production/test_dailies_input.py`, `commands/showrunner/produce.md`, `commands/builds.md`.
- `test` — opens as impl: `scripts/buildlog/rust_release.py`, `scripts/buildlog/test_rust_release.py`, `scripts/lint/sweep.py`, `scripts/lint/test_sweep.py`.

**Constraints from prior phases:** Phase 4 added `rename_recipient`, `_rename_queue` and `_merge_queues` to `scripts/message/send.py`; its `USER_CHANNEL`, `user()`, the `--to user` parsing, the need-to-priority mapping and the exit contract were not changed. This phase treats `send.py` as an existing interface and does not edit it. Phases 6 and 9 each add text to `commands/showrunner/produce.md`; change only the phone-alert instruction there. None of these files is in this unit's row: the checkpoint notice names each as `also touches`, with its owner where the production doc gives one. Tests never send a real alert and never read `~/.config/pushover/env`.

**Acceptance gate:** `python3 -m unittest discover -s <dir> -p 'test_*.py'` green for `scripts/production`, `scripts/buildlog`, `scripts/lint` and `scripts/message`; `basedpyright scripts/production` ends `0 errors, 0 warnings, 0 notes`, and `basedpyright` on `scripts/buildlog` and `scripts/lint` reports no more problems than the unit director measures on this phase's starting commit; `rg -n 'pushover\.py' scripts commands -g '!scripts/notify/**' -g '!scripts/message/send.py' -g '!commands/alert_user.md' -g '!**/test_*.py'` prints nothing: the phone script is named only inside `scripts/notify/`, `scripts/message/send.py`, `commands/alert_user.md` and test fixtures.
