# Conversation pause

## What it is

Conversation pause keeps a session quiet while the user talks to it. It stops that session's scheduled reports, a showrunner's dailies among them. After the reply ends and fifteen quiet minutes pass, the session asks whether automatic updates may return. A yes restores them, a no leaves them off, and no answer restores them after another five minutes. The related contracts keep closing unit work visible, read each unit's run state for the stall watch from its run records, allow corrections to stale as-built docs, and send every phone alert through `scripts/message/send.py --to user`.

## How it works

### Pause state and hooks

`scripts/hooks/conversation_pause.py` owns the state machine and the `status|resume|keep|tick` command line. Its main entry point is:

```python
message_arrived(
    session_id: str,
    source: PromptSource,
    prompt: str,
    now: int,
) -> HookReply
```

`PromptSource` is `TYPED`, `PEER`, `SCHEDULED` or `NOTICE`. Only a typed prompt pauses, and only its own session (`pauses(source)`). A peer message, a tick or a relay never pauses, and a peer's answer to the return question changes nothing: until 2026-10-13 a peer message paused a unit director, and a review unit's hourly health tick, relayed by another session, paused its timer and put the return question on screen six times while the user never typed in that pane. Scheduled prompts, notifier senders, task notices and system reminders do not pause anything; `JOB_SENDERS` includes `shutdown`, so `/shutdown` messages never pause a session. `/shutdown restart` and `add_unit.py` record a restored session's first prompt with `record_scheduled_prompts` before launch, so it is read as scheduled. The Stop hook records `session_crons[].prompt`; the prompt hook recognizes the next scheduled prompt by exact text or its recorded clipped prefix. These records expire after `SCHEDULED_PROMPT_RETENTION_SECONDS`, eight days.

**Only the user's own prompt is `TYPED`** (2026-10-22). A `TYPED` prompt in a showrunner or unit director also runs `auto_silence.py active`, which ends an auto silence. At 04:34 AST on 2026-10-22 a Claude Code `[Cross-session idle notice]` in the style showrunner ended one and pushed "You are back" while the user slept; four of the seven ends from 2026-10-20 20:21 to 2026-10-22 08:26 followed such a notice. Two rules now hold. Claude Code's own prompts, `[Cross-session <kind> notice]` (`HARNESS_NOTICE`) and the usage-reset continuation, are `NOTICE`. `Herdr.prompt`, the one call every script types a prompt through (`session_pane.py type`, launch and restore opening prompts), first records the pane and the words in `scripts/lib/script_prompts.py`; the prompt hook reads its own `HERDR_PANE_ID` and reads a recorded prompt as `SCHEDULED` for `KEEP_SECONDS`, six hours. The hook input cannot tell the user's keys from a script's: Claude Code sends only `prompt` and `session_title`, and a prompt typed into the pane is `promptSource: typed` in the transcript too.

`scripts/hooks/user-prompt-submit-conversation-pause.py` classifies the prompt, stamps typed activity through `escalate.typed()`, and calls `message_arrived`. `scripts/hooks/stop-conversation-pause.py` calls:

```python
mark_reply_ended(session_id: str, now: int) -> None
```

It also maintains the scheduled-prompt record. Both hooks skip subagents. A retried Stop with `stop_hook_active` does not mark another reply end.

The pause record lives at `${CONVERSATION_PAUSE_STATE_DIR:-~/.local/state/conversation-pause}/<session-id>.json`:

```python
PauseRecord(
    session_id: str,
    instances: tuple[str, ...],
    phase: PausePhase,
)
```

`release(session_id) -> PauseRecord | NoPauseRecord` deletes a session's record under `record_lock()` and returns it without resuming anything, naming only the timers that still carry the pause's mark; `/shutdown` calls it so the tick does not restore a stopped session's instances, and records what the pause had switched off itself.

`PausePhase` is `Replying | Quiet | QuestionPending | Asked | KeptOff | Returned`. `Asked` contains `QuestionNotRead | QuestionRead(replies_ended)`. The record lists only notifier instances that this pause switched off. A record written while reply footers existed (before 2026-10-18) also has `footers`, which nothing reads now.

`session_reports(session_id)` finds every notifier instance whose `conf` has `TARGET=session:<id>` and no `RUN=`. That rule covers `unit-*` and `worker-*` status reports, `showrunner-*` dailies, and other scheduled reports. The pause stops enabled instances with `notifier.sh stop`.

`ensure_watcher()` creates or repairs the run-only `conversation-pause` notifier instance:

```text
notifier.sh new conversation-pause --every 1 \
  --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/hooks/conversation_pause.py tick"
```

`tick(now: int) -> tuple[str, ...]` looks up each session with `scripts/message/sessions.py`, observes open review records from `scripts/production/review_pause.py`, asks the return question through `scripts/message/send.py`, restores updates after timeouts or a session exit, and removes the watcher when no pause record remains. `sessions.read_session(path) -> SessionRecord | UnreadableSessionRecord`; `sessions.py socket` exits 0 for a live match, 1 for no live match and 3 when the registry cannot answer. An uncertain lookup never counts as a dead session. `scripts/production/broadcast.py` also skips an unreadable session record instead of dropping the rest of a broadcast.

The timers are:

```python
QUIET_SECONDS = 900
ANSWER_SECONDS = 300
UNANSWERED_SECONDS = 1800
TOMBSTONE_SECONDS = 300
```

Fifteen minutes after a reply ends, `tick` sends the question as `conversation-pause`. Delivery changes `QuestionPending` to `Asked`. A typed `yes` runs the equivalent of `resume`; a typed `no` changes the record to `KeptOff`. Five unanswered minutes restore the recorded items. If no reply ever ends, the question becomes due after 30 minutes. While an `/adhoc_review` record is open, `tick` neither asks nor restores.

The pause stops a timer with `notifier.sh pause <instance>`, which writes `PAUSED=1` into its state, and resumes it with `notifier.sh unpause <instance>`, which re-enables it without changing `NEXT_DUE` only while that mark is there; missed work runs on the next tick and future work keeps its former time. Any explicit `start`, `resume`, `stop` or `restart` clears the mark. Resume failures leave only the unrestored items in the record for the next tick.

### Bare yes and no

The return question is identified by the cross-session sender name `conversation-pause`, not by its text. Its arrival changes the reading state to `QuestionRead(0)`. The reply that presents the question to the user raises the count to one. If a newer user prompt and assistant reply raise it above one, a later bare `yes` or `no` belongs to that newer exchange: `message_arrived` returns `NoReply.NOTHING`, leaves `asked_at` unchanged, and lets the prompt reach the session.

A yes or no remains associated with the pause while the question is unread or the count is zero or one. Any other answer reaches the session with context that lets it run:

```text
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" keep
```

For five minutes after automatic return, a late yes reports that updates already returned; a late no pauses them again and records `KeptOff`. After that tombstone expires, the prompt starts a new conversation pause.

### Closing progress

`scripts/direct/progress_history.py` represents the report target as:

```python
ReportedPassWindow | ReportedActivityWindow | NoReportedWindow
```

When the last phase is closed but an activity remains open, `progress` calls `_print_last_recorded(...)`. It prints the short report from that phase's last recorded report (clocks table, phase heading, ETA line, worker lines), with the round table only under `--tables`, then shows the activity as the stage line. Command-line percentages, command-line activity text and `pending_calibration` do not revise the closed phase, and this path appends no `progress_reported` event. An older state file missing its project clock is backfilled before the report. `No active phase to report` is reserved for a missing or closed phase with no open activity. As on every `progress` call, the unit notifier is restarted before state is read.

### Unit status and stall watch

A unit's run state is stored nowhere. `unit_lookup.read_run_states()` reads each record's first line and each worktree's newest record's last JSON object into one snapshot per tick. Its `state(worktree) -> UnitState` gives `RUNNING`, `RUN_FINISHED` or `STANDING_BY`; `run_state(worktree)` remains a one-call wrapper. `scripts/production/stall_watch.py` watches a unit only while that state is `RUNNING`; it skips every other unit and retired production rows, and clears their saved stretch state. It does not inspect a unit row for `run done`. `showrunners.py status` and `showrunners.py ready` parse their arguments and do nothing. The full account is in [showrunner automation](showrunner-automation.md), "Unit status" and "Unit lookup".

### As-built corrections and phone alerts

`docs/production_format.md`, `commands/showrunner/produce.md` and `docs/direct/final_gate_commit.md` allow any unit or showrunner to correct a file under `docs/as-built/` when it contradicts the code. No approval from the user, showrunner or owning unit is required. A unit still names the file as `also touches` in its checkpoint notice, and the showrunner does not delay a merge because of that correction.

Every repository phone alert goes through `send.py --to user`. A session writes its own text:

```text
scripts/message/send.py --to user \
  --need note|decision|blocked \
  (--action "<what the user does>" | --no-action) \
  --summary "<title>" \
  --text "<message>"
```

`send.py` owns the user channel and maps `note`, `decision` and `blocked` to priorities 0, 1 and 2. A user message names the user's action or says none is needed: without `--action` or `--no-action` `send.py` exits 2 with a usage error, and `decision` and `blocked` require `--action`. The gate itself is `scripts/notify/pushover.py` (`docs/as-built/session-roster.md`, "Messages to the user"), and every push is reviewed for clarity before it is posted (`docs/as-built/message-clarity.md`); callers do not invoke `pushover.py` themselves. On the Mac they add `--machine natedev`.

A script never writes a literal phone text. It renders a catalog entry from `scripts/notify/push_catalog.py` and sends it with `push_texts.user_send_argv`, which becomes `send.py --to user --text-id ID --value k=v…`; the entry sets the need and the action. The script callers are `rust_release.send_release_text(push)` (`rust.*`), the phone branch of `sweep.send_floor_alert` (`sweep.disk_floor`), `codex_winddown` (`codex.reset_ready`), `quota_alert` (`quota.*`) and `restart_support.alert_user` (`shutdown.*`, the only phone path in `scripts/shutdown/`). `escalate.py`'s held messages send with the hold's own `--need` and action. `ci_points.review_watch` writes its alert to the production log and sends no push. The showrunner, build-report and fix instructions use the session command.

## Invariants

- A pause affects only the session that received the prompt.
- Typed input always pauses. Peer input, scheduled prompts, prompts a script typed and automated notices never pause.
- The record contains only items this pause switched off, and resume restores only those items that nobody stopped, started or switched since.
- `notifier.sh resume` and `unpause` never move an instance's schedule.
- The pause question is recognized by sender identity, never by copied or relayed text.
- A bare yes or no is consumed only while it can still answer the return question.
- The answer window begins when the question reaches the session. A failed delivery still reaches automatic return five minutes after the question became due.
- An open review suspends question and return actions. Each pause mechanism restores only what it changed.
- Both hooks have a five-second budget, print at most one error line, and exit 0 so a prompt or reply cannot fail because the pause failed.
- `unit_lookup.read_run_states().state(worktree)`, not prose in a production row, is authoritative for whether the stall watch monitors a unit.
- Correcting an as-built doc that contradicts the code is always allowed; cross-unit notice rules still apply.
- Every scripted phone alert enters `scripts/message/send.py --to user`, with `--action` or `--no-action` or with a catalog `--text-id`; channel choice stays inside `send.py`.

## Gotchas

- The two pause hooks and their permission entry are registered in the repository's `settings.json` by the run's closing commit. The pause does not run in a live session until the showrunner promotes that file with the user's go.
- Checked live 2026-10-07 through the registered hooks, in a scratch session and one real unit session: a typed message paused the session's report, the question arrived after five quiet minutes, a worded yes ran the resume command with no permission prompt, and an unanswered question returned the report by timeout. Both sessions ran in auto mode, where a missing permission entry would show as a refusal and not as a prompt.
- Not yet checked live: a bare yes or no typed after registration (a scratch session exercised that protection before it), and renaming a unit that is running.
- A question arrives about two seconds before `send.py` finishes; the question's prompt-hook arrival is the delivery proof.
- An interrupted assistant reply emits no normal Stop. `UNANSWERED_SECONDS` prevents that state from lasting forever.
- An older `asked` record without `reading` is read as `QuestionRead(1)`.
- A message after `KeptOff` begins another conversation cycle. When that exchange becomes quiet, the question can return.
- An unreadable session record means "unknown," not "ended." A registry-list failure also prevents dead-session cleanup for that tick.
- `progress` still restarts the unit notifier on the closed-phase reporting path. Its tables are read-only, but the next report time moves.
- `send.py` prints `FAILED: ...` on stdout and exits 3 for an undelivered user alert, `REFUSED: ...` with exit 2 when `pushover.py` refused it before sending, and `HELD: ...` with exit 4 when a session push failed review and was not sent. Callers reporting the cause must inspect stdout as well as stderr.

## Why

- Pausing every targeted notifier instance gives status reports, dailies and build reports one rule, including later report types.
- Recording only changed instances prevents one pause mechanism from undoing another person's or feature's choice.
- Keeping `NEXT_DUE` on resume avoids shifting a four-hour report by four hours after every conversation.
- Sender identity protects the return question from wording changes and relays.
- The reply-count guard keeps a bare answer attached to the most recent question the user sees.
- A bounded hook favors an occasional report over delaying the user's message.
- Run records let the stall watch act on durable machine data instead of parsing descriptive text.
- The closed-phase report keeps final verification, documentation and other closing work visible without rewriting completed progress.
- One user-message command centralizes urgency, remote delivery, logging, failure behavior and the phone channel.
- Allowing stale as-built docs to be corrected keeps documentation aligned with shipped behavior wherever the contradiction is found.

**On demand.** Before a tick advances a pause, `_prune_unscheduled` drops each instance whose notifier folder is gone. With nothing left it deletes the record and asks nothing, so a production switched to on demand is never asked "Return to automatic updates?". A pause that has already returned keeps its record for late answers.

**Stopped on purpose stays stopped.** The pause marks what it stops (`PAUSED=1` in the timer's state), and only a marked item comes back, by `conversation_pause.py resume`, a typed yes, the timeout return, the session ending or `/shutdown`'s restore through `release`. A timer a showrunner stopped with `notifier.sh stop` during the pause therefore stays stopped. `_prune_unscheduled` also drops an unmarked item on the next tick, so the explicit stop takes it out of the record. A record with no marks restarts nothing. Tests in `scripts/hooks/test_conversation_pause.py` cover a stop during the pause and one before it, the timeout return, the prune, `release` and an old record; `test_unpause_restarts_only_a_timer_the_pause_stopped_and_nobody_stopped_since` in `scripts/message/test_notifier.py` covers the notifier verbs.
