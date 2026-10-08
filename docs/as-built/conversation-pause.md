# Conversation pause

## What it is

Conversation pause keeps a session quiet while the user talks to it. It stops that session's scheduled reports and, for a showrunner, its dailies footer. After the reply ends and five quiet minutes pass, the session asks whether automatic updates may return. A yes restores them, a no leaves them off, and no answer restores them after another five minutes. The related contracts keep closing unit work visible, record each unit director's status for the stall watch, allow corrections to stale as-built docs, and send every phone alert through `scripts/message/send.py --to user`.

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

`PromptSource` is `TYPED`, `PEER`, `SCHEDULED` or `NOTICE`. Every typed prompt pauses its own session. A peer message also pauses a unit director, but not a showrunner, whose routine unit traffic would otherwise keep dailies off. Scheduled prompts, notifier senders, task notices and system reminders do not pause anything. The Stop hook records `session_crons[].prompt`; the prompt hook recognizes the next scheduled prompt by exact text or its recorded clipped prefix. These records expire after `SCHEDULED_PROMPT_RETENTION_SECONDS`, eight days.

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
    footers: tuple[str, ...],
    phase: PausePhase,
)
```

`PausePhase` is `Replying | Quiet | QuestionPending | Asked | KeptOff | Returned`. `Asked` contains `QuestionNotRead | QuestionRead(replies_ended)`. The record lists only notifier instances and footers that this pause switched off.

`session_reports(session_id)` finds every notifier instance whose `conf` has `TARGET=session:<id>` and no `RUN=`. That rule covers `delegate-*` status reports, `showrunner-*` dailies, `report-builds`, and later scheduled reports. A matching `showrunner-*` instance also identifies the production footer through `scripts/hooks/showrunner_footer.py`. The pause stops enabled instances with `notifier.sh stop` and turns those footers off.

`ensure_watcher()` creates or repairs the run-only `conversation-pause` notifier instance:

```text
notifier.sh new conversation-pause --every 1 \
  --run "$HOME/.claude/scripts/lib/py $HOME/.claude/scripts/hooks/conversation_pause.py tick"
```

`tick(now: int) -> tuple[str, ...]` looks up each session with `scripts/message/sessions.py`, observes open review records from `scripts/production/review_pause.py`, asks the return question through `scripts/message/send.py`, restores updates after timeouts or a session exit, and removes the watcher when no pause record remains. `sessions.read_session(path) -> SessionRecord | UnreadableSessionRecord`; `sessions.py socket` exits 0 for a live match, 1 for no live match and 3 when the registry cannot answer. An uncertain lookup never counts as a dead session. `scripts/production/broadcast.py` also skips an unreadable session record instead of dropping the rest of a broadcast.

The timers are:

```python
QUIET_SECONDS = 300
ANSWER_SECONDS = 300
UNANSWERED_SECONDS = 1800
TOMBSTONE_SECONDS = 300
```

Five minutes after a reply ends, `tick` sends the question as `conversation-pause`. Delivery changes `QuestionPending` to `Asked`. A typed `yes` runs the equivalent of `resume`; a typed `no` changes the record to `KeptOff`. Five unanswered minutes restore the recorded items. If no reply ever ends, the question becomes due after 30 minutes. While an `/adhoc_review` record is open, `tick` neither asks nor restores.

`notifier.sh resume <instance>` re-enables an instance without changing `NEXT_DUE`; missed work runs on the next tick and future work keeps its former time. Resume failures leave only the unrestored items in the record for the next tick. The footer uses `showrunner_footer.set_footer_state(slug, FooterState.ON | OFF)`.

### Bare yes and no

The return question is identified by the cross-session sender name `conversation-pause`, not by its text. Its arrival changes the reading state to `QuestionRead(0)`. The reply that presents the question to the user raises the count to one. If a newer user prompt and assistant reply raise it above one, a later bare `yes` or `no` belongs to that newer exchange: `message_arrived` returns `NoReply.NOTHING`, leaves `asked_at` unchanged, and lets the prompt reach the session.

A yes or no remains associated with the pause while the question is unread or the count is zero or one. Any other answer reaches the session with context that lets it run:

```text
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" resume
"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/hooks/conversation_pause.py" keep
```

For five minutes after automatic return, a late yes reports that updates already returned; a late no pauses them again and records `KeptOff`. After that tombstone expires, the prompt starts a new conversation pause.

### Closing progress

`scripts/delegate/progress_history.py` represents the report target as:

```python
ReportedPassWindow | ReportedActivityWindow | NoReportedWindow
```

When the last phase is closed but an activity remains open, `progress` calls `_print_last_recorded(...)`. It prints the scope table and round table from that phase's last recorded report, then shows the activity as the running row. Command-line percentages, command-line activity text and `pending_calibration` do not revise the closed phase, and this path appends no `progress_reported` event. An older state file missing its project clock is backfilled before the report. `No active phase to report` is reserved for a missing or closed phase with no open activity. As on every `progress` call, the unit notifier is restarted before state is read.

### Unit status and stall watch

`scripts/production/showrunners.py` represents registry units as:

```python
RunningUnitDirector(session)
RunFinishedUnitDirector(session)
StandingByUnitDirector(session)
```

Together they form `RegisteredUnitDirector`. The stored form is one object per unit:

```json
{"session": "<unit-session>", "status": "running|run-finished|standing-by"}
```

`load_settings()` reads the configured registry; `load_settings_from(path)` reads a named file. The older string list plus `standby` list remains readable, and the next write emits the object form.

The status command is:

```text
showrunners.py status <showrunner-session> \
  --unit <unit-session> \
  --state running|run-finished|standing-by
```

It calls `set_unit_status(...)` under the registry lock. It refuses an absent or ambiguous showrunner or unit, exits 1 with one error line, and performs no write when the status is unchanged. `add` preserves an existing unit's status, `ready` changes only standing-by units to running, `rename` preserves the status variant, and `list` prints each unit as `<session>:<status>`. A unit sets itself to `running` when a run starts and to `run-finished` after its as-built work is complete.

`scripts/production/stall_watch.py` reads these variants directly. It skips `RunFinishedUnitDirector`, `StandingByUnitDirector`, and retired production rows, and clears their saved stretch state. It does not inspect a unit row for `run done`. Finished units remain in the live-unit set so the registry retains their identity.

### As-built corrections and phone alerts

`docs/production_format.md`, `commands/showrunner/produce.md` and `docs/delegate/final_gate_commit.md` allow any unit or showrunner to correct a file under `docs/as-built/` when it contradicts the code. No approval from the user, showrunner or owning unit is required. A unit still names the file as `also touches` in its checkpoint notice, and the showrunner does not delay a merge because of that correction.

Every repository phone alert calls:

```text
scripts/message/send.py --to user \
  --need note|decision|blocked \
  --summary "<title>" \
  --text "<message>"
```

`send.py` owns the user channel and maps `note`, `decision` and `blocked` to priorities 0, 1 and 2. Callers do not invoke `scripts/notify/pushover.py` themselves. On the Mac they add `--machine natedev`.

The direct Python callers are `ci_points.review_watch` with `--need decision`, `rust_release.send_release_text(title, message)` with `--need note`, and the phone branch of `sweep.send_floor_alert` with `--need note`. The showrunner, build-report and fix instructions use the same command.

## Invariants

- A pause affects only the session that received the prompt.
- Typed input always pauses. Peer input pauses a unit director but not a showrunner. Scheduled prompts and automated notices never pause.
- The record contains only items this pause switched off, and resume restores only those items.
- `notifier.sh resume` never moves an instance's schedule.
- The pause question is recognized by sender identity, never by copied or relayed text.
- A bare yes or no is consumed only while it can still answer the return question.
- The answer window begins when the question reaches the session. A failed delivery still reaches automatic return five minutes after the question became due.
- An open review suspends question and return actions. Each pause mechanism restores only what it changed.
- Both hooks have a five-second budget, print at most one error line, and exit 0 so a prompt or reply cannot fail because the pause failed.
- The registry status, not prose in a production row, is authoritative for whether the stall watch monitors a unit.
- Correcting an as-built doc that contradicts the code is always allowed; cross-unit notice rules still apply.
- Every scripted phone alert enters `scripts/message/send.py --to user`; channel choice stays inside `send.py`.

## Gotchas

- The two pause hooks and their permission entry are registered in the repository's `settings.json` by the run's closing commit. The pause does not run in a live session until the showrunner promotes that file with the user's go.
- Not yet checked live: the whole pause through the registered hooks in a real session, and the permission entry answering with no prompt. A scratch session exercised the bare yes/no protection.
- A unit that had already finished when the registry first recorded statuses is recorded `running` until it is set once with `showrunners.py status <showrunner-session> --unit <unit-session> --state run-finished`.
- A question arrives about two seconds before `send.py` finishes; the question's prompt-hook arrival is the delivery proof.
- An interrupted assistant reply emits no normal Stop. `UNANSWERED_SECONDS` prevents that state from lasting forever.
- An older `asked` record without `reading` is read as `QuestionRead(1)`.
- A message after `KeptOff` begins another conversation cycle. When that exchange becomes quiet, the question can return.
- An unreadable session record means "unknown," not "ended." A registry-list failure also prevents dead-session cleanup for that tick.
- `progress` still restarts the unit notifier on the closed-phase reporting path. Its tables are read-only, but the next report time moves.
- The older registry layout cannot identify units that had already finished; it treats non-standby entries as running until an explicit status update.
- `send.py` prints `FAILED: ...` on stdout and exits 3 for an undelivered user alert. Callers reporting the cause must inspect stdout as well as stderr.

## Why

- Pausing every targeted notifier instance gives status reports, dailies and build reports one rule, including later report types.
- Recording only changed instances and footers prevents one pause mechanism from undoing another person's or feature's choice.
- Keeping `NEXT_DUE` on resume avoids shifting a four-hour report by four hours after every conversation.
- Sender identity protects the return question from wording changes and relays.
- The reply-count guard keeps a bare answer attached to the most recent question the user sees.
- A bounded hook favors an occasional report over delaying the user's message.
- Named registry states let the stall watch act on durable machine data instead of parsing descriptive text.
- The closed-phase report keeps final verification, documentation and other closing work visible without rewriting completed progress.
- One user-message command centralizes urgency, remote delivery, logging, failure behavior and the phone channel.
- Allowing stale as-built docs to be corrected keeps documentation aligned with shipped behavior wherever the contradiction is found.
