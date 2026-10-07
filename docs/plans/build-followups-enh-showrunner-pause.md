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
- **`no` keeps the updates off** until the user asks for them, or until their next conversation in that session goes quiet and the question comes round again.
- **Turning a report back on must not move its schedule.** `notifier.sh start` schedules a whole interval from now, which would push a four-hour build report four hours past every conversation. The notifier gains a `resume` verb; a defect is fixed where it lives.
- **The whole working pause is one phase** (the user made it the priority, 2026-10-07): one round of writing and review. Registration follows it. The unit rename (`/showrunner:rename_unit`) is this plan's last phase; its Work Order is added while the pause is being built.
- **Registration is last and waits for the user.** Both hooks run only once `settings.json` lists them. The showrunner relayed the request; the unit director asks the user directly before that edit.
- **`review_pause.py` is unchanged.** Each of the two features restores only what it turned off itself.
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
  - `scripts/message/sessions.py` — `sessions.py socket session:<id>` prints a live session's socket and exits 0, or exits 1.
  - `scripts/message/send.py` — `send.py --to uds:<socket> --from <name> --key <key> --text <text>`; `scripts/production/stall_watch.py` lines 189-198 (`socket_for_target`, `delivery`) are the model for a script that sends.
  - `scripts/production/update_registration.py` lines 180-183 — how a run-only instance is created: `new <name> --every 1 --run "<home>/.claude/scripts/lib/py <home>/.claude/scripts/production/<script>"`.
  - `settings.json` — hook registrations (lines 102-220); the `Stop` list is lines 203-219. A Python hook's command is `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/<name>.py"`.
- **Port:** none.
- **Test lanes:** `scripts/hooks/`, `scripts/message/` and `scripts/production/` (tests sit beside the scripts as `test_*.py`).
- **Test:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'`, from the worktree root; the same form with `-s scripts/message` or `-s scripts/production`. While iterating, one file: `-p '<test_file>.py'`.
- **Lint:** `basedpyright <each changed .py file>` passes when its output ends `0 errors, 0 warnings, 0 notes`. It exits 3 in every checkout, so the exit status says nothing.
- **Invariants:**
  - Tests never start a real `claude`, `tmux`, `systemd-run` or `ssh`, never write `~/.claude/config/` or `~/.local/state/`, and never push anywhere (source: `docs/as-built/showrunner-automation.md`). Every state root and every outside command has an environment override, and tests set all of them.
  - Other units run scripts from the live `~/.claude` checkout while this plan edits the worktree's copies. An existing notifier verb keeps its behavior and its output (source: this plan's author; other units parse `next_due`).
  - A hook never fails a prompt or a reply: any error is one stderr line and exit 0. A prompt hook returns before reading any file when the prompt is a notice, because it runs on every prompt of every session (source: this plan's author; the cost is named in Phase 1).
  - Python is typed throughout, with no `Any` and no file-level type ignores. A state is a named type, never a bare optional.
  - Words shown to the user say "automatic updates", the user's own phrase.

## Phases

### Phase 1 — A session's automatic updates pause when it is written to, and return on the user's yes or after five quiet minutes  · status: todo

#### Work Order

**Goal:** When a message is typed into a session, that session's scheduled reports stop and its footer turns off, and the session records exactly what it turned off. Five minutes after the session's reply the session asks `Return to automatic updates? (yes / no)`. A typed `yes` turns them back on, a typed `no` keeps them off, and with neither in five minutes they come back on their own. The notifier gains the verb that turns an instance on without moving its schedule.

**Spec:**

Constants in `scripts/hooks/conversation_pause.py`: `QUIET_SECONDS = 300` (user's message answered, then silence), `ANSWER_SECONDS = 300` (the user, 2026-10-07: "time out … after 5 minutes"), `TOMBSTONE_SECONDS = 3600`, `WATCHER = "conversation-pause"`.

*A. The notifier verb `resume`* (`scripts/message/notifier.sh`). `resume <instance>` sets `ENABLED=1`, writes the state, and prints the `next_due` line; it never calls `schedule`, so `NEXT_DUE` is what `stop` left. A slot that came due while the instance was stopped therefore fires on the next tick, and one still ahead fires on time. On an instance that is already enabled it changes nothing. Add it to `cmd_state`, to the `start|stop|restart)` dispatch arm and to `usage`. In `docs/as-built/session-notifier.md` add its row to the verb table and add `resume` to both lists of verbs that print `next_due`.

*B. The library* (`scripts/hooks/conversation_pause.py`, new; a library and a command line in one file, as `showrunner_footer.py` is).

- Roots and overrides: state root `CONVERSATION_PAUSE_STATE_DIR` or `~/.local/state/conversation-pause`; the notifier root as `showrunner_footer.targeted_instances` reads it (`NOTIFIER_STATE_DIR`); the notifier command is the one executable named by `CONVERSATION_PAUSE_NOTIFIER`, else `zsh <this file's directory>/../message/notifier.sh`; the clock is `CONVERSATION_PAUSE_NOW_EPOCH`, else the current time in whole seconds.
- `class PromptSource(Enum)`: `TYPED`, `PEER`, `SCHEDULED`, `NOTICE`. `prompt_source(prompt: str, scheduled_senders: Callable[[], frozenset[str]]) -> PromptSource` reads the text with leading whitespace dropped:
  - empty → `NOTICE`;
  - begins `<cross-session-message` → read `from-name="…"` from the opening tag. A name in `JOB_SENDERS = frozenset({"conversation-pause", "stall-watch", "tmux-names", "quota_alert"})`, or in `scheduled_senders()`, → `SCHEDULED`; any other name, or none → `PEER`. `scheduled_senders()` is called only here; the real one returns the `FROM=` value of every notifier instance that has no `RUN=` line (`delegate-<run id>`, `showrunner-timer-<slug>`, `report-builds`);
  - matches `^<[A-Za-z][A-Za-z0-9-]*[\s>]` (any other tag wrapper, such as `<task-notification>` or `<agent-message …>`), or begins `Another Claude session sent a message` or `[SYSTEM NOTIFICATION` → `NOTICE`;
  - anything else → `TYPED`: prose, a slash command, a line the showrunner typed.
- `pauses(source: PromptSource, showrunner_session: bool) -> bool`: `TYPED` always; `PEER` unless the session is a showrunner's (`showrunner_footer.targeted_instances(session_id)` is not empty); `SCHEDULED` and `NOTICE` never.
- The record, one file per session at `<state root>/<session id>.json`, written by temporary file and `os.replace` as `review_pause.write_record` does:
  `{"session_id": "<id>", "instances": ["<notifier instance>", …], "footers": ["<production slug>", …], "phase": {"kind": "talking", "user_wrote_at": <epoch>, "answered_at": <epoch or null>}}`.
  In Python the phase is a union of frozen dataclasses: `Talking(user_wrote_at: int, answered_at: int | None)`, `Asked(asked_at: int)`, `KeptOff()`, `Returned(returned_at: int)`, with JSON kinds `talking`, `asked`, `kept_off`, `returned`. `PauseRecord(session_id, instances: tuple[str, ...], footers: tuple[str, ...], phase)`.
- One lock for every read-then-write of a record: `fcntl.flock` on `<state root>/.lock`, held only around file work and notifier calls.
- `session_reports(session_id) -> list[Report]`: every directory under the notifier root whose `conf` has the line `TARGET=session:<session id>` and no line beginning `RUN=`. `Report(name: str, enabled: bool)`, where enabled means its `state` has the line `ENABLED=1`. An unreadable instance is skipped.
- `pause(session_id, now) -> Paused | NothingToPause`, under the lock:
  1. For each enabled report not already in the record: add its name to `instances`, write the record, then run `<notifier> stop <name>`. The record is written first so a failure in between can only cause a harmless `resume` of an instance that was never stopped.
  2. For each report named `showrunner-<slug>` whose `footer_state(slug)` is `ON` and whose slug is not in `footers`: add the slug, write the record, then `set_footer_state(slug, FooterState.OFF)`.
  3. With nothing in `instances` and nothing in `footers`, no record file is left and the result is `NothingToPause()`. Otherwise the phase becomes `Talking(user_wrote_at=now, answered_at=None)`, the record is written, and the result is `Paused(newly: tuple[str, ...])`, the user's words for what this call turned off (empty when the conversation was already paused).
  A report the user had already stopped (`ENABLED=0`) and a footer already off are never recorded, so they are never turned on by this feature.
- The user's words for a paused thing, in this order with repeats dropped: `dailies` (a `showrunner-*` instance), `footer` (a footer slug), `status reports` (a `delegate-*` instance), `build report` (`report-builds`), then any other instance by its name, sorted.
- `resume(session_id) -> tuple[str, ...]`, under the lock: for each recorded instance whose directory still exists run `<notifier> resume <name>`; for each recorded slug `set_footer_state(slug, FooterState.ON)`; delete the record; return the user's words for what it turned on. With no record it returns an empty tuple and does nothing.
- Command line, session from `CLAUDE_CODE_SESSION_ID`: `conversation_pause.py status` prints `paused: dailies, footer (talking)`, with the phase's JSON kind in the brackets, or `not paused`; `conversation_pause.py resume` prints `automatic updates on: dailies, footer` or `nothing was paused`; `conversation_pause.py keep` sets the phase `KeptOff` and prints `automatic updates stay off`, or `nothing was paused` with no record; `conversation_pause.py tick` (section F; it needs no session id) runs one pass and prints one line per action taken. Exit 0; a usage error prints `usage: conversation_pause.py status|resume|keep|tick` to stderr and exits 2; `status`, `resume` and `keep` with no session id print `no session` to stderr and exit 1.

*C. The prompt hook* (`scripts/hooks/user-prompt-submit-conversation-pause.py`, new), modelled on `stop-showrunner-footer.py`. It reads the payload from stdin and returns silently when the payload has `agent_id`, has no `session_id`, or `pauses(…)` is false. A `NOTICE` returns before any file is read; a cross-session prompt reads the notifier root's `conf` files once. Otherwise it calls `message_arrived` (section E) and prints the reply it returns as one JSON object. The pause notice, the reply for a `pause` that turned something off:

```json
{"systemMessage": "Automatic updates paused while we talk: dailies, footer.",
 "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
  "additionalContext": "Automatic updates for this session are paused while the user talks to you: dailies, footer. Leave them off; the session asks the user before they return. If the user asks for them back sooner, run: \"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/conversation_pause.py\" resume"}}
```

with the list replaced by the real words. A reply with no `systemMessage` leaves that key out; with no reply the hook prints nothing. Any exception prints `conversation-pause: <type>: <message>` to stderr; the exit status is always 0.

*Cost, named:* the hook adds one process start to every prompt. For a notice it does no file work. For a typed or peer prompt it lists the notifier root (about twenty small files today) and starts `notifier.sh` only for a report it turns off.

*D. The reply's end* (`scripts/hooks/stop-conversation-pause.py`, new). It returns silently when the payload has `agent_id` or no `session_id`, or when `<state root>/<session id>.json` does not exist (checked before the library is imported). Otherwise it calls `mark_answered(session_id, now)`: under the lock, a `Talking` phase whose `answered_at` is `None` gets `answered_at=now`; any other phase is left alone. So the quiet time counts from the first reply that ends after the user's last message, and later replies do not move it. A message typed while the session is still answering counts from that running reply's end, because the hook fires when it is typed (measured); accepted. It prints nothing and exits 0 always.

*E. A pausing message, by phase* (`message_arrived(session_id, source, prompt, now) -> HookReply | None`, called for a prompt that `pauses`; `HookReply(system_message: str | None, context: str)`). Only a `TYPED` prompt can be an answer: for a `PEER` prompt `answer` is `NotAnAnswer`. `answer(prompt) -> Yes | No | NotAnAnswer` reads the text stripped of surrounding whitespace and of trailing `.` and `!`, lowercased: `yes` and `y` are `Yes`, `no` and `n` are `No`. `RESUME` and `KEEP` below stand for the command lines `"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume` and `… keep`.

| Phase on arrival | The message | What happens | `systemMessage` | `additionalContext` |
| --- | --- | --- | --- | --- |
| none, `Talking`, `KeptOff` | anything | `pause` | the pause notice (section C) when something was turned off; else no reply | the same |
| `Asked` | `Yes` | `resume` | `Automatic updates are back on: <words>.` | `The user answered yes to "Return to automatic updates?". They are back on: <words>. Confirm it in one line. That yes answers only this question.` |
| `Asked` | `No` | phase becomes `KeptOff` | `Automatic updates stay off.` | `The user answered no to "Return to automatic updates?". They stay off until the user asks for them; then run: RESUME. Confirm it in one line. That no answers only this question.` |
| `Asked` | anything else | `pause` (phase becomes `Talking`) | none | `You asked the user "Return to automatic updates? (yes / no)" and they wrote something else. If their message answers that question, run RESUME for yes or KEEP for no. Otherwise answer them and say nothing of the question; it comes back when they go quiet.` |
| `Returned` | `Yes` | the record is deleted | none | `Automatic updates already returned on their own. Tell the user that in one line.` |
| `Returned` | `No` | `pause`, then phase becomes `KeptOff` | `Automatic updates are off again: <words>.` | the `Asked` + `No` text |
| `Returned` | anything else | the record is deleted, then `pause` | the pause notice when something was turned off; else no reply | the same |

A `pause` that finds a record keeps what it lists and adds what has been turned on since.

*F. The watcher.* `pause` ends, still under the lock, with `ensure_watcher()`: when `<notifier root>/conversation-pause/conf` is missing it runs `<notifier> new conversation-pause --every 1 --run "<home>/.claude/scripts/lib/py <home>/.claude/scripts/hooks/conversation_pause.py tick"`, the form `update_registration.py` uses. `conversation_pause.py tick` then runs each minute. `tick(now)` takes the lock, decides, writes, releases the lock, and only then sends, so a slow send never holds up a prompt. For each record:

1. The session is not running (`sessions.py socket session:<id>` prints nothing): turn on what the record lists (as `resume` does) and delete the record. A session that is gone is not in a conversation.
2. `Talking` with `answered_at` set and `now - answered_at >= QUIET_SECONDS`: the phase becomes `Asked(asked_at=now)` and the question is queued for sending.
3. `Asked` with `now - asked_at >= ANSWER_SECONDS`: turn on what the record lists; the record keeps empty lists and the phase `Returned(returned_at=now)`, so a late `yes` or `no` is read correctly.
4. `Returned` with `now - returned_at >= TOMBSTONE_SECONDS`: delete the record.
5. `KeptOff`: nothing.

After the pass, with no record left in `Talking` or `Asked`, it runs `<notifier> remove conversation-pause` before releasing the lock. The question goes out as `send.py --to uds:<socket> --from conversation-pause --key conversation-pause-<session id> --text <text>` (`CONVERSATION_PAUSE_SEND` and `CONVERSATION_PAUSE_SESSIONS` replace the two scripts in tests, as `STALL_WATCH_SEND` and `STALL_WATCH_SESSIONS` do in `stall_watch.py`). A failed send is one stderr line; the phase stays `Asked`, so the updates still return on time. The text:

`conversation-pause: the user has been quiet here for 5 minutes. Ask them this, word for word, and nothing else: Return to automatic updates? (yes / no) They return on their own in 5 minutes. Their yes or no is handled when they type it.`

*G. Tests.*
- `scripts/message/test_notifier.py`: `resume` after `stop` leaves `NEXT_DUE` unchanged and sets `ENABLED=1`, both when the slot is still ahead and when it has passed; a tick at a time past that slot then sends once; `resume` on an enabled instance changes nothing; `start`, `stop` and `restart` behave as before.
- `scripts/hooks/test_conversation_pause.py` (new), with `HOME`, `NOTIFIER_STATE_DIR`, `SHOWRUNNER_STATE_DIR` and `CONVERSATION_PAUSE_STATE_DIR` in a temporary directory and a stub notifier (`CONVERSATION_PAUSE_NOTIFIER`) that logs its arguments and rewrites the instance's `ENABLED=` line:
  - the classifier, one row each: prose, prose with leading spaces, `<3 thanks`, `/unit:report`, `/compact` and `From the user (via the showrunner): go on` → `TYPED`; the measured cross-session wrapper from `natedev` → `PEER`; the same wrapper from `stall-watch`, and from `delegate-abc` when an instance's `conf` has `FROM=delegate-abc` → `SCHEDULED`; `<task-notification>`, `<agent-message from="a1">` and the empty string → `NOTICE`;
  - `pauses`: a peer message pauses a unit director's session and leaves a showrunner's alone; typing pauses both; a scheduled message and a notice pause neither;
  - a showrunner session (instance `showrunner-demo` enabled, footer on): a typed prompt stops the instance, creates the footer switch file, records both, and prints the notice with `dailies, footer`; a second typed prompt calls the notifier for nothing, prints nothing, and moves `user_wrote_at`;
  - a unit director's session (`delegate-abc`) reads `status reports`; `report-builds` reads `build report`;
  - an instance already stopped is not recorded and `resume` leaves it stopped; an instance aimed at another session and a run-only instance are untouched;
  - a scheduled tick (`/unit:report` in the wrapper, from the instance's own sender) and a task notice change nothing and print nothing; a typed slash command pauses; a payload with `agent_id` changes nothing; a session with no reports leaves no record and prints nothing;
  - stdin that is not JSON exits 0 with one stderr line;
  - `resume` on the command line calls the notifier's `resume` for each recorded instance that still exists, skips one whose directory is gone, turns the footer on, and deletes the record; `status` prints both forms; `keep` sets `kept_off`.
- More cases in `scripts/hooks/test_conversation_pause.py`; the clock comes from `CONVERSATION_PAUSE_NOW_EPOCH`, and stub `sessions` and `send` commands log their arguments:
  - the Stop hook sets `answered_at` once and a second Stop leaves it; with no record it touches nothing; a payload with `agent_id` changes nothing;
  - a tick before five quiet minutes sends nothing; at five it sends the question once, with the exact text, to the session's socket, and the phase is `Asked`; a tick while `answered_at` is `None` sends nothing however long ago the user wrote;
  - every row of the table in E, including `Yes.`, ` y ` and `NO!`; a peer message whose text is `yes` is not an answer and keeps the pause going;
  - an unanswered question: five minutes on, the tick calls the notifier's `resume` for each recorded instance, turns the footer on, and leaves a `Returned` record; a typed `yes` after that deletes it and pauses nothing; a typed `no` pauses again and reads `kept_off`;
  - a `KeptOff` record is left alone by the tick however long it sits; a typed message moves it to `Talking`;
  - a record whose session is not running is turned back on and deleted;
  - the first pause creates the watcher with the exact `new` arguments; a second does not; the tick that leaves no `Talking` or `Asked` record removes it;
  - a failed send leaves `Asked` and exits 0.

**Files:**
- `scripts/message/notifier.sh` — the `resume` verb.
- `scripts/message/test_notifier.py` — its tests.
- `docs/as-built/session-notifier.md` — the verb's row and the two verb lists.
- `scripts/hooks/conversation_pause.py` — new: the library and command line.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — new: the prompt hook.
- `scripts/hooks/stop-conversation-pause.py` — new: the Stop hook.
- `scripts/hooks/test_conversation_pause.py` — new: the tests.

**Seats:** 1 writer + 1 tester. The library and the two hooks are one design, so one seat writes the code and the other the tests.
- `impl` — `scripts/message/notifier.sh`, `docs/as-built/session-notifier.md`, `scripts/hooks/conversation_pause.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py`.
- `test` — `scripts/message/test_notifier.py` (the `resume` cases) and `scripts/hooks/test_conversation_pause.py`: the cases under Tests, written from this Work Order while the writer works, then run against the writer's code.

**Constraints from prior phases:** none.

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/message -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/production -p 'test_review_pause.py'` green (it shares the footer switch); `basedpyright` on each changed `.py` file ends `0 errors, 0 warnings, 0 notes`. Nothing registers the hooks yet, so no live session changes.

### Phase 2 — The two hooks are registered, and the commands say what they do  · status: todo

#### Work Order

**Pending decision: register the two hooks in `settings.json`**

Actual problem:
The pause runs only once `settings.json` lists `user-prompt-submit-conversation-pause.py` under `UserPromptSubmit` and `stop-conversation-pause.py` under `Stop`. `settings.json` is the user's configuration, and the request reached this unit through the showrunner, so the unit director asks the user directly. Asked in the unit director's session 2026-10-07 about 12:45 PDT; not yet answered.

What exists now:
- `settings.json` registers no `UserPromptSubmit` hook; its `Stop` list holds three hooks.
- The live `~/.claude` checkout holds an uncommitted change to `settings.json` that is not this unit's, so the showrunner cannot promote this file until that is settled.

What should change:
- Add the two entries below.

Recommendation:
Register both. The feature the user asked for does nothing until they are.

**Goal:** Every session runs the two hooks, and the showrunner and unit director commands say what happens when the user writes.

**Spec:**
- `settings.json`, in `hooks`: a new `"UserPromptSubmit"` list holding one group whose one hook is `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/user-prompt-submit-conversation-pause.py\""}`; and, appended to the existing `Stop` group's `hooks` list, `{"type": "command", "command": "\"$HOME/.claude/scripts/lib/py\" \"$HOME/.claude/scripts/hooks/stop-conversation-pause.py\""}`. Nothing else in the file changes.
- `commands/showrunner/produce.md`, beside the line `The dailies keep their own switch.`: a short paragraph. When the user types a message here, the dailies and the footer pause on their own; a `conversation-pause:` message later asks you to put one question to the user, word for word; never answer it for them; their typed yes or no is handled without you.
- `commands/unit/delegate.md`, in the progress contract beside `If the user stops updates, use /unit:report off`: one sentence saying a message the user types pauses the status reports on its own and the same `conversation-pause:` message follows.
- `scripts/hooks/test_conversation_pause.py`: one test that reads `settings.json` and finds both commands, each exactly once.

**Files:**
- `settings.json` — the two registrations.
- `commands/showrunner/produce.md` — the paragraph.
- `commands/unit/delegate.md` — the sentence.
- `scripts/hooks/test_conversation_pause.py` — the registration test.

**Seats:** 2 writers. Nothing here has a test lane worth a seat of its own: the one test is three lines.
- `impl` — `settings.json`, `scripts/hooks/test_conversation_pause.py`.
- `test` — opens as impl: `commands/showrunner/produce.md`, `commands/unit/delegate.md`.

**Constraints from prior phases:** Phase 1 built `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py` and `scripts/hooks/conversation_pause.py` (commands `status`, `resume`, `keep`, `tick`). The question the session is asked to put is `Return to automatic updates? (yes / no) They return on their own in 5 minutes.`, sent from `conversation-pause`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -c "import json; json.load(open('settings.json'))"` exits 0; `bash scripts/agents/test_agents_config.sh` green. Live check by the unit director after the showrunner promotes it: a typed message in a session with a running report shows the pause notice, and five quiet minutes later the question arrives.
