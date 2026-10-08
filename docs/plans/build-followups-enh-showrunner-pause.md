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

### Phase 5 — A bare yes or no meant for another question is left to the session  · status: done

#### As-built

- While the pause's return question is open, a bare `yes` or `no` is the pause's answer only until a newer reply from the session has ended. `Asked` is `Asked(asked_at, reading)`, `reading` being `QuestionNotRead | QuestionRead(replies_ended)`. The answer is the pause's under `QuestionNotRead` and under `QuestionRead` with 0 or 1 replies ended; above 1 `message_arrived` returns `NoReply.NOTHING`, the prompt reaches the session, and the `Asked` record and its `asked_at` timeout (`ANSWER_SECONDS`) are unchanged.
- `is_return_question(prompt)` recognises the question by its sender, a cross-session message whose `from-name` is `WATCHER`, never by its text. Its arrival writes `Asked(now, QuestionRead(0))` from `QuestionPending`, or `QuestionRead(0)` from `Asked` with `QuestionNotRead`; `tick`'s write after the send applies only while the phase is still `QuestionPending`.
- `mark_reply_ended` (formerly `mark_answered`) keeps `Replying` → `Quiet` and adds one only under `QuestionRead`. The Stop hook does nothing for an event with `stop_hook_active` true or an `agent_id`.
- `status` appends "a newer reply ended; bare yes/no belongs to the session" while the count is above 1. `RESUME_COMMAND` and `KEEP_COMMAND` are unchanged.
- `tmux_names.live_sessions()` returns `list[ClaudeSession] | TmuxServerUnavailable` and counts a Claude session only when the socket path in its `TMUX` variable is the one `tmux display-message -p '#{socket_path}'` reports. `tick()` does nothing on `TmuxServerUnavailable`; `rename_unit.py` refuses with "tmux could not be asked: …".

**Files:**
- `scripts/hooks/conversation_pause.py` — the reading states, the rule, the sender match.
- `scripts/hooks/stop-conversation-pause.py` — calls `mark_reply_ended`; ignores a retried Stop.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — hands the watcher's prompt to `message_arrived`.
- `scripts/hooks/test_conversation_pause.py` — the cases for each rule above.
- `scripts/production/tmux_names.py`, `scripts/production/rename_unit.py`, `scripts/production/test_tmux_names.py`, `scripts/production/test_rename_unit.py` — the server match and its tests.

**Binds later work:** the hooks to register are these protected ones; the showrunner's instruction text says a bare yes or no reaches the session once it has replied again. Code that edits the tmux-name job or the rename command handles `TmuxServerUnavailable`, and their test stubs answer the socket query and give each fake session a `TMUX` value.

**Gotchas:** an idle session receives the question about two seconds before `send.py` returns, so arrival is the proof of delivery. A Stop that another hook blocks is retried with `stop_hook_active` true. Pane ids repeat across tmux servers. An `asked` record without `reading` reads as `QuestionRead` with one reply ended. Inside a Claude session a `sleep` through Bash is refused; a long written reply is what holds a turn open in a live check. Proved in a scratch Claude session with the hooks registered for that session alone: idle arrival, arrival in mid-reply, a blocked and retried Stop, a yes for the session's own question, the timeout from the original question, yes straight after, and no.

**Ruled out:** matching the question by its text (a model relays it); a bare count field on `asked` (the question can queue behind a running reply).

### Phase 6 — The two hooks are registered, and the commands say what they do  · status: done

#### As-built

- `settings.json` registers `user-prompt-submit-conversation-pause.py` under a `UserPromptSubmit` list of its own and `stop-conversation-pause.py` in the existing `Stop` group, straight after `stop-delegate-continue.py`. `permissions.allow` holds `Bash("$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" *)`, which matches `RESUME_COMMAND` and `KEEP_COMMAND` character for character.
- `commands/showrunner/produce.md` (beside "The dailies keep their own switch.") and `commands/unit/delegate.md` (in the progress contract) say that a typed message pauses the automatic updates on its own, and that a later `conversation-pause:` message asks the session to put its question to the user word for word and never to answer it for them.
- A scheduled prompt (`CronCreate`, `/loop`, `ScheduleWakeup`) does not pause reports. `UserPromptSubmit` carries only the prompt's text, so the Stop hook records the session's `session_crons[].prompt` list at `<state>/scheduled-prompts/<session id>.json` (`record_scheduled_prompts`; an empty list removes the file, a payload without the key leaves it), also for an event with `stop_hook_active` true. `prompt_source(prompt, senders, scheduled_prompts)` answers `SCHEDULED` for a prompt equal to a recorded one, or to the prefix of one clipped at 1000 characters (`CLIPPED_PROMPT_MARKER`).
- Only a prompt opening with `<task-notification` or `<system-reminder` is a notice by its tag (`NOTICE_TAG`). A typed message that opens with a paste or any other tag pauses the reports.
- `tick` removes a scheduled-prompts file older than `SCHEDULED_PROMPT_RETENTION_SECONDS` (eight days); `status` and the pause records ignore those files.
- The Stop hook loads the pause module only when the session has a schedule, a scheduled-prompts file to clear, or a pause record.

**Files:**
- `settings.json` — the two registrations and the permission entry.
- `commands/showrunner/produce.md`, `commands/unit/delegate.md` — the instructions.
- `scripts/hooks/conversation_pause.py` — the scheduled-prompts record, `NOTICE_TAG`, `prompt_source` with the scheduled lookup, the pruning in `tick`.
- `scripts/hooks/stop-conversation-pause.py` — records the scheduled prompts on each Stop that carries the list.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — passes the scheduled lookup.
- `scripts/hooks/test_conversation_pause.py` — `ConversationPauseRegistrationTests` and the scheduled-prompt and notice-tag cases.

**Binds later work:** the paragraph in `commands/showrunner/produce.md` stays as written; later rules go beside it. The registration takes effect in every session only once the showrunner promotes it to the live checkout.

**Gotchas:** a scheduled prompt is told apart only by equality with the list the last `Stop` carried; it fires only between turns, so a `Stop` always precedes it. `scripts/hooks/test_stop_showrunner_footer.py` requires the banned-words and footer hooks to be the last two of the `Stop` group. Proved in a scratch Claude session: a `CronCreate` prompt and a `/loop` iteration both arrive as their text alone and both classify as scheduled, and the typed `/loop …` line classifies as typed. Not proved before promotion: the whole pause through the registered hooks in a real session, and the resume command running with no permission prompt.

**Ruled out:** putting the Stop hook at the tail of its group (an existing test pins the last two hooks; hooks of one group run on the same event, so the order changes no behavior); treating every prompt that opens with a tag as a notice (a paste opens with one).

### Phase 7 — A progress report shows the closing work of a unit's last piece of work  · status: done

#### As-built

- `progress_history.py progress`, with the last phase closed and an activity open, prints the scope table and the round table from that phase's last recorded values, under the `as of` line, with the activity as the running row and `▸ **<stage> - <activity>**` where the between-windows report says `No pass or activity is open.`
- That report ignores the percentages and the activity text passed on the command line and any `pending_calibration`; it appends no `progress_reported` event and writes no recorder state.
- With no phase active and no activity open the answer is still `No active phase to report: phase <status>.`; with a phase active every output is unchanged.
- `calibrate` is accepted after a phase closes; `start-phase` clears the `pending_calibration` it leaves.
- `_reported_window(state)` returns `ReportedPassWindow | ReportedActivityWindow | NoReportedWindow`, three frozen dataclasses; `_print_last_recorded` takes `ReportedActivityWindow | NoReportedWindow` and prints either footer.

**Files:**
- `scripts/delegate/progress_history.py` — the closed-phase report and the named window result.
- `scripts/delegate/test_progress_history.py` — `test_a_closed_phase_with_a_running_activity_reports_its_last_tables`, `test_a_closed_phase_with_a_finished_activity_has_no_active_report`, `test_an_active_phase_progress_report_keeps_its_exact_output`.

**Binds later work:** `commands/unit/report.md` step 5 and `commands/unit/delegate.md` `<DelegationResultFormat/>` still name the refusal as the only answer once no phase is active; the work that tells showrunners and unit directors they may correct an as-built doc carries that correction. `docs/as-built/plan-delegate-progress-history.md` and `docs/as-built/showrunner-automation.md` hold the same stale sentence.

**Gotchas:** `_ensure_project_timing` runs before every report and writes `project_started_at`, `project_start_source` and `project_plan_doc` into a state that lacks them; `start-run` has written them since 2026-08-11, so only an older state file is touched. `_restart_unit_notifier` runs on every `progress` call, the closed-phase one included, so the next tick is counted from the report. While a plan still has work to start, the next phase opens before the finished one's closing steps, so the closed-phase report appears only after the plan's last phase.

**Ruled out:** skipping the project-clock backfill on the closed-phase path (unreachable for a run started since 2026-08-11, and the between-windows report shares it).

### Phase 8 — The registry records each unit director's status, and the stall watch reads it there  · status: done

#### As-built

The showrunner registry records one status for each unit director, and the stall watch reads finished and standing-by units from it.

- `RunningUnitDirector(session) | RunFinishedUnitDirector(session) | StandingByUnitDirector(session)`, together `RegisteredUnitDirector`, are the registry's unit types in `scripts/production/showrunners.py`.
- The registry stores one object per unit, `{"session": …, "status": "running" | "run-finished" | "standing-by"}`. The older layout (names plus a `standby` list) is still read; the next write stores the new one.
- `load_settings()` reads the configured registry; `load_settings_from(path)` reads a named file.
- `showrunners.py status <showrunner-session> --unit <unit-session> --state <state>` is the one way a status changes, through `set_unit_status` under the registry lock; setting the status a unit already has writes nothing. A refusal exits 1 with one line.
- `add` keeps an existing unit's status and refuses an empty unit session; `ready` moves only a standing-by unit to running; `list` prints `<session>:<status>` for every unit; `rename` keeps the variant.
- `add_unit.record()` runs `add`, then `status` with standing-by or running, so relaunching a finished unit records it running.
- The stall watch skips a unit recorded run-finished or standing-by and a retired plan cell; it reads no "run done" text from a plan cell. The live-unit list keeps a finished unit.
- A unit director sets running at the start of a run and run-finished after the as-built commit.

**Files:**
- `scripts/production/showrunners.py` — the registry, its types and its commands.
- `scripts/production/add_unit.py` — a launch records the unit's status.
- `scripts/production/stall_watch.py` — reads finished and standing-by from the registry.
- `scripts/production/live_units.py`, `scripts/production/rename_unit.py`, `scripts/production/tmux_names.py` — read `unit.session`.
- `scripts/whoami/quota_alert.py` — loads the registry with `load_settings_from(CONFIG)`.
- `docs/production_format.md`, `docs/delegate/final_gate_commit.md` — say when a unit director sets its status.

**Gotchas:**
- A unit that had already finished when the older layout is first read is recorded running; set it run-finished once with the `status` verb, or the stall watch bumps it.
- `scripts/whoami` imports the registry module, so a change to the registry's public functions is checked there too (`python3 -m unittest discover -s scripts/whoami -p 'test_*.py'`, `basedpyright scripts/whoami`).
- `load_settings_from` rejects two entries with one session, so `set_unit_status`'s ambiguous-unit refusal is reachable only for settings built in memory.
- `add_unit.registry_has_unit` stays because `test_waiting` uses it.

**Ruled out:** moving "run done" plan cells into the registry on first read — the registry module does not read production docs, and the showrunner sets the few finished units once.

### Phase 9 — Showrunners and unit directors are told they may always correct an out-of-date as-built doc  · status: done

#### As-built

Three instruction files say that an out-of-date as-built doc may always be corrected, and two say what a progress report shows after the last phase closes.

- `docs/production_format.md` <ProductionUnit/> item 9, the Rules of `commands/showrunner/produce.md` and <RunAsBuilt/> in `docs/delegate/final_gate_commit.md` each say: correcting an as-built doc under `docs/as-built/` that contradicts the code is always allowed, in any unit's doc, without asking the user, the showrunner or the owner; the checkpoint notice still names the file as `also touches`. Each cites the user, 2026-10-07.
- The showrunner's rule adds that a merge is never held because a unit corrected another unit's as-built doc.
- `commands/unit/report.md` step 5 and <DelegationResultFormat/> in `commands/unit/delegate.md` say: when the last phase is closed but an activity is still open, `progress` prints both tables from that phase's last recorded values with the activity as the running row; `No active phase to report` remains only when no phase is active and no activity is open.

**Files:**
- `docs/production_format.md` — the rule for unit directors.
- `commands/showrunner/produce.md` — the rule for showrunners.
- `docs/delegate/final_gate_commit.md` — the rule in the as-built pass.
- `commands/unit/report.md`, `commands/unit/delegate.md` — the closed-phase report rule.

**Gotchas:** production tests read these instruction files (`test_production_lifecycle.py`, `test_merge_checkpoint.py` and others under `scripts/production`); run them after any edit to these files.

**Ruled out:** running the registration as a last phase — the as-built pass needs every phase done, so it is a closing commit after the as-built commit.

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

**Constraints from prior phases:** Phase 4 added `rename_recipient`, `_rename_queue` and `_merge_queues` to `scripts/message/send.py`; its `USER_CHANNEL`, `user()`, the `--to user` parsing, the need-to-priority mapping and the exit contract were not changed. This phase treats `send.py` as an existing interface and does not edit it. Phase 9 adds text to `commands/showrunner/produce.md`; change only the phone-alert instruction there. None of these files is in this unit's row: the checkpoint notice names each as `also touches`, with its owner where the production doc gives one. Tests never send a real alert and never read `~/.config/pushover/env`. The merge branch has changed `scripts/lint/sweep.py` since this branch last merged it (`send_floor_alert` is at a different line there): find each function by name, never by line, and trial-merge the merge branch before the checkpoint notice. Phase 8 (`c265820`) changed a registry function and its listed gate missed an importer in another directory: before the gate, search the whole repository for every importer and caller of `review_watch`, `send_release_text` and `send_floor_alert` (`rg -n 'review_watch|send_release_text|send_floor_alert' scripts commands`) and run the tests of each directory that has one. The registration left the stack in `de6fd83` (the showrunner's landing call, 2026-10-07): `settings.json` is the merge branch's copy, and Phase 6's paragraph in `commands/showrunner/produce.md`, its sentence in the progress contract of `commands/unit/delegate.md` and its registration test are out until the run's closing commit puts them back (`## Closing commit`). Do not re-add them here.

**Acceptance gate:** `python3 -m unittest discover -s <dir> -p 'test_*.py'` green for `scripts/production`, `scripts/buildlog`, `scripts/lint` and `scripts/message`; `basedpyright scripts/production` ends `0 errors, 0 warnings, 0 notes`, and `basedpyright` on `scripts/buildlog` and `scripts/lint` reports no more problems than the unit director measures on this phase's starting commit; `rg -n 'pushover\.py' scripts commands -g '!scripts/notify/**' -g '!scripts/message/send.py' -g '!commands/alert_user.md' -g '!**/test_*.py'` prints nothing: the phone script is named only inside `scripts/notify/`, `scripts/message/send.py`, `commands/alert_user.md` and test fixtures.

## Closing commit

Not a phase: it runs after the run's as-built commit, as the last commit of the run, so that everything before it merges without waiting on the user (the showrunner's landing call, 2026-10-07). The user, 2026-10-07: "yes i want the auto pause".

- `git revert --no-commit de6fd83` restores four things: the two hook entries and the permission entry in `settings.json`; `ConversationPauseRegistrationTests` and its `SETTINGS` constant in `scripts/hooks/test_conversation_pause.py`; the paragraph in `commands/showrunner/produce.md` that tells a showrunner how the pause's question reaches it; the sentence in the progress contract of `commands/unit/delegate.md` that tells a unit director the same.
- Where a later phase edited the lines around a restored paragraph, keep both: the later text and the restored paragraph, each once.
- `settings.json` means the worktree's copy, never `~/.claude/settings.json`. The showrunner merges this commit and rewrites the live file only on the user's own go; this unit never promotes, restores or edits the live file.
- Check before the commit: `git diff de6fd83^ HEAD -- settings.json` is empty once committed; `python3 -m unittest discover -s scripts/hooks -p 'test_conversation_pause.py'` is green with the registration test among them; `basedpyright scripts/hooks` ends `0 errors, 0 warnings, 0 notes`; each restored paragraph appears once in its file.
- Commit: `checkpoint(build-followups-enh-showrunner-pause): registration — the two pause hooks are registered`, pushed, with a notice to the showrunner that it waits for the user's go.
- The live check of the whole pause through the registered hooks stays unperformed until the showrunner switches it on, and the run summary says so.
