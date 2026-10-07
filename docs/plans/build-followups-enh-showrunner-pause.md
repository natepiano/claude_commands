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
- **The whole working pause is one phase** (the user made it the priority, 2026-10-07): one round of writing and review. An independent review of that phase then found seven defects in it, so a repair phase follows it and registration follows the repair. The unit rename (`/showrunner:rename_unit`) is this plan's last phase; its Work Order is added while the pause is being built.
- **Registration is last and waits for the user.** Both hooks run only once `settings.json` lists them. The showrunner relayed the request; the unit director asks the user directly before that edit.
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

#### Work Order

**Goal:** Seven defects an independent review found in Phase 1's pause are repaired before any session runs it: a typed message is never held up by the pause, a pause that started always ends and cleans up after itself, the record lists exactly what the pause turned off, the user's five minutes to answer start when the question reaches them, and the pause leaves a session's updates alone while an `/adhoc_review` is open there. The phase's own review then found four more ways a pause could fail to end or could end early; they are repaired here too, and item H records each typed prompt for the user's alerts.

**Spec:**

Phase states after this phase, each a frozen dataclass in `scripts/hooks/conversation_pause.py`, with its `kind` in the record's JSON:
- `Replying(user_wrote_at)` — `replying`: a pausing message arrived and no reply has ended since.
- `Quiet(user_wrote_at, reply_ended_at)` — `quiet`: the first reply after that message ended.
- `QuestionPending(due_at)` — `question_pending`: the question is due and has not reached the session.
- `Asked(asked_at)` — `asked`: the question reached the session at `asked_at`.
- `KeptOff()` — `kept_off`; `Returned(returned_at)` — `returned`: as today.
`Talking` and `_optional_integer` go. Nothing has ever registered the hooks, so no stored record needs converting; a record of an unknown kind is reported on its own line as today and blocks no other session.

A. **A typed message never waits on the pause.**
- Both hook entry files arm a budget before they import the library: `signal.signal(signal.SIGALRM, <handler>)` then `signal.alarm(<budget>)`, where the handler raises `HookBudgetSpent(f"gave up after {budget} seconds")`, an exception class the entry file defines. The budget is `HOOK_BUDGET_SECONDS = 5`, or the integer in `CONVERSATION_PAUSE_HOOK_BUDGET` when set (tests use 1). The existing `except Exception` then prints the one stderr line and the hook exits 0. The prompt hook's return for a notice still comes before any file is read.
- `tick` never holds the record lock across a session lookup or a send. It lists the record files and looks each session up with no lock held; then, under the lock, re-reads each record and moves it on with the lookup's answer (a record that vanished meanwhile is skipped; one that appeared waits for the next tick); sends go out after the lock is released, as today.
- `run_notifier`'s timeout drops from 10 seconds to 5.
- Named cost: a message whose hook runs out of budget pauses nothing, or only part; the next pausing message finishes the job (item D keeps the record true meanwhile).

B. **The watcher lives while any record exists.** `tick` removes the `conversation-pause` instance only when no record file is left. So a `returned` record is deleted five minutes after the return (`TOMBSTONE_SECONDS`) with no other session's pause needed to keep the watcher alive, and a `kept_off` record whose session has ended gets its reports and footer turned back on and is deleted. Named cost: while any record exists, one `sessions.py socket` call per record per minute.

C. **A pause never starts without its watcher, and always ends.**
- `_pause_locked` calls `ensure_watcher()` before it stops anything. When that raises, nothing was stopped and no record was written. The watcher counts as present only when the notifier would run it: its `conf` and `state` both exist and `state` holds `ENABLED=1`; `ensure_watcher` removes a half-made instance and creates it again, and resumes one that is complete and disabled. The cause is repaired where it lives: `notifier.sh new` wrote `conf` before `state`, so a caller stopped between the two left an instance that never ticks. It now writes `state` first, so `conf` is the last file written, an instance is either absent or complete to every reader, and the next `new` completes one that was left without `conf`.
- A failed session lookup (`NotRunning.UNKNOWN`) stops nothing from timing out: every transition below runs as for a running session, except that nothing is sent. Only `NotRunning.GONE` restores at once. `scripts/message/sessions.py` tells the two apart: it exits 3 with one stderr line when it cannot answer (the registry directory is missing or cannot be listed, or no live match was found while a registry file could not be read), and 1 only when every registry file was read and no live session matches. `_session_lookup` reads 0 with a socket as running, 0 or 1 without one as gone, anything else as unknown.
- Transitions `tick` makes, for a record whose session is running or unknown and whose production has no review open (item F):
  - `replying`: at `now - user_wrote_at >= UNANSWERED_SECONDS` it becomes `question_pending(due_at=now)`.
  - `quiet`: at `now - reply_ended_at >= QUIET_SECONDS` it becomes `question_pending(due_at=now)`.
  - `question_pending`: at `now - due_at >= ANSWER_SECONDS` the updates return (`returned(now)`); before that, when the session is running, the question is sent (item E).
  - `asked`: at `now - asked_at >= ANSWER_SECONDS` the updates return.
  - `kept_off`: no change. `returned`: deleted at `now - returned_at >= TOMBSTONE_SECONDS`.
  So a pause the user has not answered `no` to ends at most `UNANSWERED_SECONDS + ANSWER_SECONDS` after the last pausing message, whatever fails.

D. **The record lists only what the pause turned off.** `_pause_locked` still writes a report's name into the record before `notifier stop` (a hook killed between the two must not leave a report off with no record). Two changes:
- when the stop fails, the name comes out of the record again, the record is written, the remaining reports and footers are still handled, and one error naming every report that failed is raised at the end (the hook prints it as its one stderr line). The same holds for a footer whose switch fails.
- a report that is in the record and still enabled is stopped again on the next pausing message, so a failed or interrupted stop is retried.
`resume` and the automatic return turn on only what the record lists. Both try every listed report and footer: when some fail, the record keeps its phase and lists only what failed, one error names them, and the next tick or `resume` tries those again.

E. **The user's five minutes start when the question reaches them.** When a question falls due, `tick` writes `question_pending(due_at=now)` under the lock. After the lock is released it runs the send; delivery is `send.py` exiting 0 with a first output line that begins `SENT:` (`QUEUED:` exits 1: not reached). On delivery it takes the lock again and, only when the record is still that `question_pending` with the same `due_at`, writes `asked(asked_at=now_epoch())`. Not delivered: the record stays `question_pending` and the next tick sends again (same `--key`, so a queued copy is replaced, never doubled). Just before each send, under the lock, `tick` reads that session's record again and sends only when it is still that `question_pending` with the same `due_at`; the lock is released before the send. Named cost: a message typed while a send is already under way still gets the question, and the record correctly stays `replying`. In `message_arrived`, a typed `yes` or `no` is an answer only while the record is `asked` (or `returned` inside its late window, as today); while it is `question_pending` it is typing like any other and restarts the pause as `replying`.

F. **The pause waits while a review is open.** `/adhoc_review` in a showrunner's session runs `review_pause.py pause`, whose record is `<SHOWRUNNER_STATE_DIR or ~/.local/state/showrunner>/review-paused/<slug>.json`. The typed command reaches the prompt hook first, so the pause holds the dailies and footer and the review records them as already off; the pause must then not turn them on under the review.
- `scripts/hooks/showrunner_footer.py` gains `review_pause_path(slug: str) -> Path`, the one definition of that path; `scripts/production/review_pause.py`'s `record_path` returns it.
- In `tick`, a record that lists a `showrunner-<slug>` instance or a footer `<slug>` whose `review_pause_path(slug)` exists is left exactly as it is: no question, no return, no deletion. A session that has ended is still restored and its record deleted. When the review ends, the record moves on from its existing stamps on the next tick.
- `showrunner_footer` is imported only where it already is or inside `tick`, never at the library's top, as today.

G. **Hook replies are named types.** `HookReply` becomes `ShownToUser(system_message, context) | ContextOnly(context) | NoReply` (`NoReply` a one-member enum, as `NoPauseRecord` is). `message_arrived` and `_pause_reply` return a `HookReply` and never `None`; `user-prompt-submit-conversation-pause.py` prints by variant and prints nothing for `NoReply`. The words shown to the user and the context handed to the session do not change. `status` prints the new kind names.

H. **A typed prompt is recorded for the user's alerts.** `scripts/message/escalate.py` (merged from the merge branch at `0058107`) holds news for the user and sends it to Pushover when they have not typed in a terminal in time; `escalate.typed()` writes the instant to `typed` under `ESCALATE_STATE_DIR`, else `$XDG_STATE_HOME/escalate`, else `~/.local/state/escalate`. The prompt hook calls it for every `PromptSource.TYPED` prompt, before it looks for anything to pause, so a session with nothing to pause records it too; a peer message, a scheduled prompt and a notice record nothing. The call sits inside the hook's budget and has its own handler: `HookBudgetSpent` passes through untouched (the alarm fires once, so swallowing it would leave the pause with no budget), and any other `Exception` prints one stderr line beginning `conversation-pause: typed stamp:`. So a failed stamp never skips the pause and never holds the prompt. `escalate` is imported after the budget is armed, with `scripts/message` added to `sys.path` as `scripts/whoami/five_hour.py` does; `pyrightconfig.json` gains `scripts/message` in the `scripts/hooks` environment's `extraPaths`, so the import resolves for basedpyright. Only the prompt hook's path calls it, never `tick`, the Stop hook or the command line.

I. **The stall watch never calls a registered showrunner missing.** Live case, 2026-10-07 14:49 PDT: `scripts/production/stall_watch.py` told the showrunner that `natedev`, registered with seven units, was missing from `config/showrunners.json`, with an `add natedev --zone …` command that would have replaced the entry and dropped its units. `socket_for_target` returns `None` for every failure, and `tick` calls a running showrunner missing whenever its socket is not among the configured names' sockets, so one failed lookup, or a second session sharing the name, reads as not registered. After this phase: `socket_for_target` returns a named three-way answer (a socket; no live session, which is exit 1 or empty output; could not answer, which is exit 3 or any other failure). A running showrunner whose session name the config holds is never missing. In a tick where any configured showrunner's lookup could not answer, nobody is judged missing and no `missing-*.json` file is written or removed. Every other use of a lookup treats could not answer as no socket for that tick, as today.

**Tests** (`scripts/hooks/test_conversation_pause.py`, as the existing cases: hooks and the command line run as subprocesses, every state root and outside command pointed at a temporary directory or a stub; a test for a defect fails on Phase 1's code):
- A: with `<state>/.lock` held by the test through `fcntl.flock`, the prompt hook given a typed message and budget 1 exits 0 inside 3 seconds with empty stdout and one stderr line naming `HookBudgetSpent`; the same for the Stop hook with a record present. With a notifier stub that sleeps 30 seconds on `stop`, the prompt hook with budget 1 exits 0 inside 3 seconds. With a sessions stub that writes a marker file and then sleeps 2 seconds, the test starts `tick`, waits for the marker, and takes `<state>/.lock` with `LOCK_EX | LOCK_NB` at once.
- B: after the automatic return the watcher instance still exists and the `returned` record is there at 299 seconds; a tick at 300 seconds deletes the record and removes the watcher, with no other record present. A `kept_off` record with a running session keeps the watcher; when its session is gone the listed reports are resumed, the footer is on, the record is deleted and the watcher removed.
- C: a notifier stub that fails `new`: the prompt hook stops no report and writes no record. A sessions stub that exits 3 on every call: a `quiet` record still becomes `question_pending` when due (nothing sent), then `returned` five minutes later with its reports resumed; an `asked` record past its window returns the same way; a `kept_off` record is left alone.
- D: a notifier stub that fails `stop` for one of two reports: the record lists only the other, stderr names the failed one, the hook exits 0. A second typed message with a healthy stub stops the first and the record lists both. A report listed in the record and still enabled is stopped again by the next typed message. `resume` after the failed stop resumes only the listed report.
- E: a send stub that prints `QUEUED: …` and exits 1: the record stays `question_pending`, a typed `yes` then restarts the pause as `replying` and resumes nothing; the next tick with a stub that prints `SENT: …` writes `asked` with the delivery time; `question_pending` for five minutes with no delivery returns the updates. A record that changed between the send and the second lock (a typed message made it `replying`) is not overwritten by `asked`.
- F: with `review-paused/<slug>.json` present under the temporary `SHOWRUNNER_STATE_DIR`, a tick leaves an `asked` record past its window, a `quiet` record whose question is due, and a `returned` record past its window byte-identical, sends nothing and resumes nothing; with the file removed, the next tick moves each on. A record of a session with no showrunner instance ignores the file. `scripts/production/test_review_pause.py`: one test that `review_pause.record_path(slug)` equals `showrunner_footer.review_pause_path(slug)`.
- G: the existing reply cases, rewritten to the variants; a typed message that pauses nothing new prints nothing; every existing case that names `talking` uses `replying` or `quiet`.
- H: every case's environment sets `ESCALATE_STATE_DIR` to the temporary directory, so the suite never writes the real stamp. A typed prompt writes `typed` there in a session with reports to pause and in one with none; a peer message, a scheduled prompt and a notice write none; with `ESCALATE_STATE_DIR` naming a regular file, the prompt hook exits 0, still pauses, and prints the one `typed stamp` stderr line.
- Repairs from this phase's review: a notifier root holding the watcher's `conf` and no `state`: a typed message logs `remove conversation-pause`, then `new conversation-pause …`, before any `stop`, and with a stub that fails `new` nothing is stopped and no record is written; a complete watcher with `ENABLED=0` is resumed before the `stop`. `tick` against the real `sessions.py`, with `NOTIFIER_SESSIONS_DIR` naming a missing directory, keeps a `quiet` record and its report stopped. Two sessions with questions due and a send stub that, during the first send, rewrites the second record to `replying`: one send, and the second record is still `replying`. A notifier stub that fails `resume` for one of two reports: the other report and the footer are on, the record keeps its phase and lists only the failed report, and the next tick with a healthy stub finishes the return. `scripts/message/test_sessions.py`: exit 3 for a missing registry directory, for one that cannot be listed, and for a session id whose registry file is corrupt; exit 1 for an empty readable registry. `scripts/message/test_notifier.py`: a `new` whose `state` cannot be written exits non-zero and leaves no `conf`; a `new` whose `conf` could not be written is completed by the next `new`, and the instance then ticks. `scripts/production/test_stall_watch.py` (item I): a sessions stub that exits 3 for the configured name while that showrunner runs: no `missing-*.json` appears, an existing one is kept, nothing is sent; a stub that answers the configured name with another session's socket: the showrunner whose name the config holds is not judged missing; a running showrunner whose name the config lacks, with lookups that answer, is still told as today.

**Files:**
- `scripts/hooks/conversation_pause.py` — the states, the transitions, the lock use, the reply types.
- `scripts/hooks/user-prompt-submit-conversation-pause.py` — the budget; printing by reply variant; the typed stamp (item H).
- `scripts/hooks/stop-conversation-pause.py` — the budget.
- `scripts/hooks/showrunner_footer.py` — `review_pause_path`.
- `scripts/production/review_pause.py` — `record_path` returns it.
- `scripts/hooks/test_conversation_pause.py` — the tests above.
- `scripts/production/test_review_pause.py` — the path test.
- `pyrightconfig.json` — `scripts/message` in the `scripts/hooks` environment's `extraPaths` (item H).
- `scripts/message/sessions.py` — exit 3 when it cannot answer.
- `scripts/message/test_sessions.py` — its tests.
- `scripts/message/notifier.sh` — `new` writes `state` before `conf`.
- `scripts/message/test_notifier.py` — its tests.
- `scripts/production/stall_watch.py` — the three-way lookup answer and the missing rule (item I).
- `scripts/production/test_stall_watch.py` — its tests.

**Seats:** 1 writer + 1 tester. The states and transitions are one design, so one seat writes the code and the other the tests. The repair round after the review is one seat, `impl`, holding every file under Files.
- `impl` — `scripts/hooks/conversation_pause.py`, `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py`, `scripts/hooks/showrunner_footer.py`, `scripts/production/review_pause.py`, `pyrightconfig.json`, `scripts/message/sessions.py`, `scripts/message/test_sessions.py`, `scripts/message/notifier.sh`, `scripts/message/test_notifier.py`, `scripts/production/stall_watch.py`, `scripts/production/test_stall_watch.py`.
- `test` — `scripts/hooks/test_conversation_pause.py`, `scripts/production/test_review_pause.py`: the cases under Tests, written from this Work Order while the writer works, then run against the writer's code.

**Constraints from prior phases:** Phase 1 built the library, the two hook entry files and 36 tests in `scripts/hooks/test_conversation_pause.py`. Its environment overrides, all of which the tests set: `CONVERSATION_PAUSE_STATE_DIR` (records and `.lock`), `CONVERSATION_PAUSE_NOW_EPOCH`, `CONVERSATION_PAUSE_NOTIFIER`, `CONVERSATION_PAUSE_SESSIONS`, `CONVERSATION_PAUSE_SEND`, with `NOTIFIER_STATE_DIR` and `SHOWRUNNER_STATE_DIR`. Its constants: `QUIET_SECONDS = 300`, `ANSWER_SECONDS = 300`, `UNANSWERED_SECONDS = 1800`, `TOMBSTONE_SECONDS = 300`, `WATCHER = "conversation-pause"`. The words shown to the user, `RESUME_COMMAND`, `KEEP_COMMAND` and the command line's four verbs keep their text: Phase 3 registers a permission entry that matches those command lines. `scripts/hooks/showrunner_footer.py`, `scripts/production/review_pause.py`, `pyrightconfig.json`, `scripts/message/sessions.py`, `scripts/message/test_sessions.py`, `scripts/message/notifier.sh` and `scripts/message/test_notifier.py` belong to no unit's row (the showrunner confirmed 2026-10-07 that no unit has them open): the checkpoint notice names them as `also touches`. `sessions.py` has callers in `scripts/production`, `scripts/mac_test`, `scripts/build_hold` and `scripts/message/notifier.sh`: their suites run before the checkpoint.

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/message -p 'test_*.py'` green; `basedpyright` on each changed `.py` file ends `0 errors, 0 warnings, 0 notes`. Nothing registers the hooks yet, so no live session changes.

### Phase 3 — The two hooks are registered, and the commands say what they do  · status: todo

#### Work Order

**Pending decision: register the two hooks in `settings.json`**

Actual problem:
The pause runs only once `settings.json` lists `user-prompt-submit-conversation-pause.py` under `UserPromptSubmit` and `stop-conversation-pause.py` under `Stop`. `settings.json` is the user's configuration, and the request reached this unit through the showrunner, so the unit director asks the user directly. Asked in the unit director's session 2026-10-07 about 12:45 PDT; not yet answered.

What exists now:
- `settings.json` registers no `UserPromptSubmit` hook; its `Stop` list holds three hooks.
- The live `~/.claude` checkout holds an uncommitted change to `settings.json` that is not this unit's, so the showrunner cannot promote this file until that is settled.

What should change:
- Add the two hook entries below, and one permission entry so a session can run the pause's own `resume`, `keep` and `status` commands without a permission prompt.

Recommendation:
Register both, once Phase 2's repairs have passed their review. The feature the user asked for does nothing until they are.

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

**Constraints from prior phases:** Phase 2 repaired the pause and renamed its states (`replying`, `quiet`, `question_pending`, `asked`, `kept_off`, `returned`: what `status` prints); each hook gives up after five seconds, so neither needs a `timeout` in its registration. Phase 1 built `scripts/hooks/user-prompt-submit-conversation-pause.py`, `scripts/hooks/stop-conversation-pause.py` and `scripts/hooks/conversation_pause.py` (commands `status`, `resume`, `keep`, `tick`). The question the session is asked to put is `Return to automatic updates? (yes / no) They return on their own in 5 minutes.`, sent from `conversation-pause`. The session is handed two command lines to run through Bash, `RESUME_COMMAND` and `KEEP_COMMAND` (`"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume` and `… keep`); each reads `CLAUDE_CODE_SESSION_ID`. Timings: the question five minutes after a reply ends (`QUIET_SECONDS`), thirty minutes after the user's message when no reply ended (`UNANSWERED_SECONDS`), the return five minutes after an unanswered question (`ANSWER_SECONDS`), a late yes or no for five minutes after that (`TOMBSTONE_SECONDS`).

**Acceptance gate:** `python3 -m unittest discover -s scripts/hooks -p 'test_*.py'` green; `python3 -c "import json; json.load(open('settings.json'))"` exits 0; `bash scripts/agents/test_agents_config.sh` green. Live check by the unit director after the showrunner promotes it: a typed message in a session with a running report shows the pause notice, five quiet minutes later the question arrives, a typed `yes` brings the reports back, and a sentence that means yes makes the session run the resume command with no permission prompt; a second round answered `no` leaves them off, `status` says so, and the `conversation-pause` notifier instance is gone once no record is left.

### Phase 4 — `/showrunner:rename_unit <old> <new>` renames a unit's session everywhere the showrunner reads it  · status: todo

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
- `showrunners.change("rename", …)` (lines 338-350) must be safe to run again after it failed part-way: it writes the registry file only after every stall-state rename succeeded, and `stall_watch.rename_state` (line 158) moves a stretch only when its source exists. The tests below pin both; change `showrunners.py` or `stall_watch.py` only where one fails.

D. **A renamed unit can be adopted again.** `add_unit.py` lines 301-306 refuse an existing Units row whose Session differs from the unit's name less `-unit`, and `UnitLaunch.name` (line 93) then drives tmux, remote control, systemd, the prompt and the registry (lines 508-587) from the command line's name. Replace `name` with `identity: UnitIdentity`, a named pair `(unit, session)` built once where the row is resolved: an existing row gives `session` from its Session cell; no row gives the name the command line implies, as today. Every tmux, remote-control, systemd, prompt and registry call reads `identity.session`; the row and every message about the unit read `identity.unit`. The Session comparison goes. A new row is written as today. `commit_and_push` (lines 451-463) keeps its behaviour and its git calls move into one function that takes the production, the paths and the message; step 3 calls the same function.

E. **A renamed unit is still a unit.** `scripts/message/top_level.py` `is_unit` (lines 63-67) asks tmux for the unit mark by the session name inside the record's `tmux` field, which keeps the old name after a rename (measured). Ask by the pane id instead, the part after the last `.` in that field: `tmux show-environment -t %<n> SHOWRUNNER_UNIT` answers for the pane's current session (measured 2026-10-07).

F. **State kept under the old name moves through its owner.** `scripts/production/rename_state.py` (new) is the one entry: `rename_all(old: str, new: str, scratch: Path) -> list[str]` returns one description per thing it changed and raises `RenameRefused(reason)`. It runs the three parts below in this order; each does nothing when nothing is under `old`, so a second call returns an empty list.
- **The showrunner's scratch files.** The session that runs this command is the only writer of its scratchpad and runs one command at a time, so no lock is taken; each rewrite goes to a temporary file in the same directory and then `os.replace`. A missing file or directory is skipped. Where an entry already exists under `new`, it stays and the `old` entry is dropped. A JSON file that cannot be parsed raises `RenameRefused` naming it.
  - `<scratch>/dailies_input_state/eta_seen.json` (`waiting.py` line 227, `dailies_input.py` line 307): each key `<old>|<phase>` becomes `<new>|<phase>`.
  - `<scratch>/unit_status/decisions_seen` and `<scratch>/unit_status/blocks_open` (`unit_status.sh` lines 35-36; the directory is the first argument `commands/showrunner/dailies.md` line 33 passes): a `decisions_seen` line `<old>|<text>` and a `blocks_open` line whose first tab-separated field is `<old>` take the new name.
  - `<scratch>/showrunner_state.json` (`commands/showrunner/produce.md` line 37): each `units` row whose `unit` is `<old>`.
  - `<scratch>/dailies_judgment.json` (`dailies_input.py` lines 299-306): each `units` row whose `unit` is `<old>`.
  - `<scratch>/dailies_state.json` (`dailies_render.py` `load_state`, line 893): the top-level key `<old>`.
- **The message store.** `send.rename_recipient(old: str, new: str) -> list[str]` in `scripts/message/send.py`, all of it inside one `locked()` (line 161), on `STATE` (line 59, which follows `XDG_STATE_HOME`):
  - `queue/<old>.jsonl` (`queue_path`, line 183): each entry's `"to"` becomes `new`; the entries join `queue/<new>.jsonl`, keeping the latest per key as `enqueue` does; the old file is removed.
  - `keys.json` (line 171): in every key's `last` map, the instant under `old` moves to `new`; when both exist the later instant stays.
  - `relay/<old>.jsonl` (line 394) holds the last relay stream for a recipient: it becomes `relay/<new>.jsonl` when none exists, and is removed when one does.
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

**Constraints from prior phases:** Phases 1 to 3 touch none of these files. Once Phase 3 registers the pause, typing `/showrunner:rename_unit` pauses the showrunner's dailies and footer, and the `/rename <new>` this command types into the unit's pane pauses that unit's status reports; both pauses are keyed by session id, so they survive the new name untouched and return as any pause does. No pause record is moved. `scripts/production/tmux_names.py`, `scripts/message/send.py`, `scripts/message/top_level.py`, `scripts/build_hold/build_hold.py` and their tests belong to no live unit's row: the checkpoint notice names them as `also touches`.

**Acceptance gate:** `python3 -m unittest discover -s scripts/production -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/message -p 'test_*.py'` green; `python3 -m unittest discover -s scripts/build_hold -p 'test_*.py'` green; `basedpyright` on each changed `.py` file ends `0 errors, 0 warnings, 0 notes`. Live check by the showrunner after it promotes the command: rename a throwaway unit and read the row, `tmux ls` and the next dailies.
